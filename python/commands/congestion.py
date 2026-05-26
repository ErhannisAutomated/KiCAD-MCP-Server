"""Routing-congestion analyzer.

Identifies WHERE on the board the placement is blocking routing, so the
caller can move components informedly rather than guessing. Divides the
board into a grid of square cells and computes per-cell:

  * pad density (count of pad bounding boxes overlapping the cell)
  * ratsnest density (count of straight-line ratsnest segments crossing
    the cell)
  * combined congestion score = pad density × ratsnest density

The ratsnest input is the DRC ``unconnected_items`` list — these ARE the
required-but-not-yet-routed connections. Pass ``drcViolationsPath`` to
re-use an existing ``power_module_drc_violations.json`` from a prior
``get_drc_violations`` run; if omitted, the caller must run DRC first
(this tool is read-only and does not invoke kicad-cli itself).

The tool also returns per-net difficulty (the max congestion score along
each unrouted net's straight ratsnest), so the caller can prioritize the
nets most likely to require re-placement.

Why this exists: real PCB autoplacers are infeasible to build in OSS
(2-3 weeks of code for ~zero advantage over commercial tools); manual
placement guided by congestion data is the pragmatic alternative. Filed
as task #179 after task #178 (auto pin-escape) revealed the routing
density on the power_module restart was the symptom, not the cause.
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pcbnew

logger = logging.getLogger("kicad_interface")


def _board_bbox_nm(board: Any) -> Tuple[int, int, int, int]:
    """Compute Edge.Cuts bounding box in nanometres (left, top, right, bottom).

    Fallback: aggregated footprint bbox if Edge.Cuts is empty.
    """
    edge_layer = board.GetLayerID("Edge.Cuts")
    left = top = math.inf
    right = bottom = -math.inf
    found = False
    for drawing in board.GetDrawings():
        if drawing.GetLayer() != edge_layer:
            continue
        bb = drawing.GetBoundingBox()
        left = min(left, bb.GetLeft())
        top = min(top, bb.GetTop())
        right = max(right, bb.GetRight())
        bottom = max(bottom, bb.GetBottom())
        found = True
    if not found:
        # Fallback: union of footprint bboxes
        for fp in board.GetFootprints():
            bb = fp.GetBoundingBox(False)
            left = min(left, bb.GetLeft())
            top = min(top, bb.GetTop())
            right = max(right, bb.GetRight())
            bottom = max(bottom, bb.GetBottom())
    return (int(left), int(top), int(right), int(bottom))


def _cells_overlapping_bbox(
    pad_left: int, pad_top: int, pad_right: int, pad_bottom: int,
    bbox_left: int, bbox_top: int, cell_size_nm: int,
) -> List[Tuple[int, int]]:
    """Return (col, row) cells overlapping the given pad bbox."""
    c0 = (pad_left - bbox_left) // cell_size_nm
    c1 = (pad_right - bbox_left) // cell_size_nm
    r0 = (pad_top - bbox_top) // cell_size_nm
    r1 = (pad_bottom - bbox_top) // cell_size_nm
    return [(c, r) for c in range(int(c0), int(c1) + 1) for r in range(int(r0), int(r1) + 1)]


def _cells_along_segment(
    x0_nm: int, y0_nm: int, x1_nm: int, y1_nm: int,
    bbox_left: int, bbox_top: int, cell_size_nm: int,
) -> Set[Tuple[int, int]]:
    """Return the set of (col, row) cells crossed by the segment.

    Uses a Bresenham-style supercover by stepping in small increments
    (cell_size_nm / 4). Cheap and sufficient for ratsnest density.
    """
    cells: Set[Tuple[int, int]] = set()
    dx = x1_nm - x0_nm
    dy = y1_nm - y0_nm
    length = max(abs(dx), abs(dy), 1)
    n_steps = max(1, int(length / (cell_size_nm / 4)))
    for i in range(n_steps + 1):
        t = i / n_steps
        x = x0_nm + dx * t
        y = y0_nm + dy * t
        c = int((x - bbox_left) // cell_size_nm)
        r = int((y - bbox_top) // cell_size_nm)
        cells.add((c, r))
    return cells


def _load_ratsnest_from_drc(drc_path: Path) -> List[Dict[str, Any]]:
    """Extract ratsnest segments from a DRC violations JSON file.

    Each unconnected_items violation has 2+ items; the first two are the
    endpoints of one ratsnest line. Returns list of dicts with
    {net, from_xy_nm, to_xy_nm, from_desc, to_desc}.
    """
    if not drc_path.exists():
        return []
    try:
        data = json.loads(drc_path.read_text())
    except json.JSONDecodeError as e:
        logger.warning(f"DRC JSON parse failed: {e}")
        return []
    out = []
    for v in data.get("violations", []):
        if v.get("type") != "unconnected_items":
            continue
        items = v.get("items") or []
        if len(items) < 2:
            continue
        a, b = items[0], items[1]
        ap = a.get("pos") or {}
        bp = b.get("pos") or {}
        if not (ap and bp):
            continue
        out.append({
            "net": a.get("net") or b.get("net") or "?",
            "from_x_nm": int(ap.get("x", 0) * 1_000_000),
            "from_y_nm": int(ap.get("y", 0) * 1_000_000),
            "to_x_nm": int(bp.get("x", 0) * 1_000_000),
            "to_y_nm": int(bp.get("y", 0) * 1_000_000),
            "from_desc": a.get("description", ""),
            "to_desc": b.get("description", ""),
        })
    return out


def _pad_layer_names(pad: Any, board: Any) -> List[str]:
    """Return the copper layer names the pad sits on (e.g. ['F.Cu'] for
    a top SMD pad, ['F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu'] for a PTH)."""
    out: List[str] = []
    try:
        layer_set = pad.GetLayerSet()
    except Exception:
        return out
    for lid in range(64):  # PCB_LAYER_ID enum range, copper IDs are small
        try:
            if not layer_set.Contains(lid):
                continue
            name = board.GetLayerName(lid)
            if name.endswith(".Cu"):
                out.append(name)
        except Exception:
            continue
    return out


def analyze_congestion(
    board: Any,
    cell_size_mm: float = 5.0,
    top_n: int = 15,
    drc_violations_path: Optional[str] = None,
    net_difficulty_top_n: int = 20,
    layer: Optional[str] = None,
) -> Dict[str, Any]:
    """Compute pad-density × ratsnest-density congestion grid.

    Returns top-N congested cells (with member components) plus per-net
    difficulty (max congestion score along each unrouted net's
    ratsnest). Read-only.

    Layer filtering (#181): when `layer` is provided (e.g. ``"F.Cu"``),
    the pad-density component counts only pads present on that layer.
    PTH pads count toward every copper layer. Hotspots and net
    difficulty are scored against the filtered density, so the result
    reflects routing pressure on that specific layer rather than the
    summed board-wide pressure. When ``layer`` is omitted, behaviour is
    unchanged from v1 (any-layer pad density).  Every hotspot
    additionally carries ``pad_count_by_layer`` so the caller can see
    the per-layer breakdown in a single call.
    """
    cell_size_nm = int(cell_size_mm * 1_000_000)
    bbox = _board_bbox_nm(board)
    bleft, btop, bright, bbot = bbox

    if not math.isfinite(bleft) or bright <= bleft:
        return {
            "success": False,
            "message": "Could not compute board bounding box "
                       "(no Edge.Cuts and no footprints?).",
        }

    n_cols = (bright - bleft) // cell_size_nm + 1
    n_rows = (bbot - btop) // cell_size_nm + 1

    # grid[(col, row)] -> {pads, pads_by_layer, rats, components}
    grid: Dict[Tuple[int, int], Dict[str, Any]] = {}

    # Pad density: count each pad once per cell its bbox overlaps.
    # Track per-layer counts so the caller can inspect layer-specific
    # pressure (and we can derive the filtered score for `layer`).
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        for pad in fp.Pads():
            pb = pad.GetBoundingBox()
            cells = _cells_overlapping_bbox(
                pb.GetLeft(), pb.GetTop(), pb.GetRight(), pb.GetBottom(),
                bleft, btop, cell_size_nm,
            )
            pad_layers = _pad_layer_names(pad, board)
            for col, row in cells:
                cell = grid.setdefault(
                    (col, row),
                    {
                        "pads": 0,
                        "pads_by_layer": {},
                        "rats": 0,
                        "components": set(),
                    },
                )
                cell["pads"] += 1
                cell["components"].add(ref)
                for lname in pad_layers:
                    cell["pads_by_layer"][lname] = (
                        cell["pads_by_layer"].get(lname, 0) + 1
                    )

    # Ratsnest density: count each unrouted segment once per cell it crosses.
    rats: List[Dict[str, Any]] = []
    if drc_violations_path:
        rats = _load_ratsnest_from_drc(Path(drc_violations_path))

    # Per-net cell traversal for difficulty calc.
    net_max_score: Dict[str, int] = {}
    for r in rats:
        cells = _cells_along_segment(
            r["from_x_nm"], r["from_y_nm"],
            r["to_x_nm"], r["to_y_nm"],
            bleft, btop, cell_size_nm,
        )
        for col, row in cells:
            cell = grid.setdefault(
                (col, row),
                {
                    "pads": 0,
                    "pads_by_layer": {},
                    "rats": 0,
                    "components": set(),
                },
            )
            cell["rats"] += 1
        # Note: net_max_score is computed AFTER all rats are counted, below.

    # Compute per-cell score using either total pad density (when no
    # `layer` filter is set) or the per-layer pad count for the
    # requested layer.  Layer-filtered scores reflect the routing
    # pressure on that specific copper layer, which is what matters
    # when picking which side to route a particular net on.
    for cell in grid.values():
        if layer:
            pads_eff = cell["pads_by_layer"].get(layer, 0)
        else:
            pads_eff = cell["pads"]
        cell["score"] = pads_eff * cell["rats"]
        cell["pads_for_score"] = pads_eff

    for r in rats:
        cells = _cells_along_segment(
            r["from_x_nm"], r["from_y_nm"],
            r["to_x_nm"], r["to_y_nm"],
            bleft, btop, cell_size_nm,
        )
        max_s = 0
        for cr in cells:
            c = grid.get(cr)
            if c and c["score"] > max_s:
                max_s = c["score"]
        prev = net_max_score.get(r["net"], 0)
        if max_s > prev:
            net_max_score[r["net"]] = max_s

    # Hotspots ranked by score.
    hotspots: List[Dict[str, Any]] = []
    for (col, row), cell in grid.items():
        if cell["score"] <= 0:
            continue
        x_mm = (bleft + col * cell_size_nm) / 1_000_000.0
        y_mm = (btop + row * cell_size_nm) / 1_000_000.0
        hotspots.append({
            "cell": {
                "col": col, "row": row,
                "x_mm": round(x_mm, 2),
                "y_mm": round(y_mm, 2),
                "size_mm": cell_size_mm,
            },
            "pad_count": cell["pads"],
            "pad_count_by_layer": dict(cell["pads_by_layer"]),
            "ratsnest_count": cell["rats"],
            "score": cell["score"],
            "components": sorted(cell["components"]),
        })
    hotspots.sort(key=lambda h: (-h["score"], -h["pad_count"]))

    net_difficulty = [
        {"net": n, "max_cell_score": s}
        for n, s in net_max_score.items() if s > 0
    ]
    net_difficulty.sort(key=lambda d: -d["max_cell_score"])

    return {
        "success": True,
        "cell_size_mm": cell_size_mm,
        "layer": layer,
        "board_bbox_mm": {
            "left": bleft / 1_000_000.0,
            "top": btop / 1_000_000.0,
            "right": bright / 1_000_000.0,
            "bottom": bbot / 1_000_000.0,
        },
        "grid_dims": {"cols": int(n_cols), "rows": int(n_rows)},
        "ratsnest_segments": len(rats),
        "active_cells": sum(1 for c in grid.values() if c["score"] > 0),
        "hotspots": hotspots[:top_n],
        "net_difficulty": net_difficulty[:net_difficulty_top_n],
        "message": (
            f"analyze_congestion: {len(rats)} ratsnest segments, "
            f"{sum(1 for c in grid.values() if c['score'] > 0)} active cells, "
            f"top score {hotspots[0]['score'] if hotspots else 0}"
            + (f" (filtered to layer {layer})" if layer else "")
        ),
    }
