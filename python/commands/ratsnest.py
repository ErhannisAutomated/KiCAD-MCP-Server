"""Ratsnest inspection — read-only window into the connectivity work-list.

Exposes the PCB's ratsnest (the list of pad-pair connections that DRC
calls ``unconnected_items``) as structured data: per-segment endpoints,
endpoint pad refs, net, length, plus geometric crossing detection
between segments of different nets.

Used (a) for human-readable analysis when judging "is this layout
routable?" and (b) as a debugging window into the autoplacer when its
moves don't help. Pairs with ``analyze_congestion`` — congestion shows
which CELLS are dense, ratsnest shows which SEGMENTS will need to
thread through them.

Source: the DRC ``unconnected_items`` cache produced by a prior
``get_drc_violations`` / ``run_drc`` call (the calling handler
auto-discovers ``<project>_drc_violations.json``). pcbnew's SWIG
``GetConnectivity().GetRatsnestForNet()`` returns opaque objects that
aren't iterable from Python, so the JSON path is the practical entry
point.

Crossing detection is straight pairwise segment-segment intersection.
``include_crossings`` is opt-in because it's O(N²) — a 200-segment
board burns ~40k checks per call, fast in Python but worth keeping
off when the caller just wants per-segment positions.
"""
from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pcbnew

logger = logging.getLogger("kicad_interface")


def _segments_intersect(
    p1: Tuple[float, float], p2: Tuple[float, float],
    p3: Tuple[float, float], p4: Tuple[float, float],
) -> Optional[Tuple[float, float]]:
    """If segments p1-p2 and p3-p4 intersect strictly (interior crossing,
    not just shared endpoints), return the intersection point; else None.

    "Strictly" means neither segment endpoint touches the other segment
    — this avoids false positives at shared pads (multi-pad fan-outs).
    """
    x1, y1 = p1; x2, y2 = p2; x3, y3 = p3; x4, y4 = p4
    denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(denom) < 1e-9:
        return None  # parallel
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / denom
    u = -((x1 - x2) * (y1 - y3) - (y1 - y2) * (x1 - x3)) / denom
    # Strict interior: ignore endpoint-touching to dodge shared-pad noise.
    eps = 1e-6
    if eps < t < 1 - eps and eps < u < 1 - eps:
        return (x1 + t * (x2 - x1), y1 + t * (y2 - y1))
    return None


def _parse_pad_ref(desc: str) -> Tuple[Optional[str], Optional[str]]:
    """Pull (component_ref, pad_number) from a DRC description like
    ``"Pad 10 [BAT+] of U1 on F.Cu"`` or ``"PTH pad 1 [V12_OUT] of J2"``.
    Returns (None, None) if it doesn't match.
    """
    # Pattern: "...pad <N> [<net>] of <REF> on..."  OR ...of <REF>$
    import re
    m = re.search(r"[Pp]ad\s+(\S+)\s+\[[^\]]*\]\s+of\s+(\S+)", desc)
    if m:
        return (m.group(2), m.group(1))
    return (None, None)


def _load_ratsnest(drc_path: Path) -> List[Dict[str, Any]]:
    """Read DRC unconnected_items into structured ratsnest segments.

    Each violation lists 2+ items; we emit one segment per violation
    (items[0] -> items[1]). Returns dicts with parsed component refs
    and pad numbers.
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
        ax, ay = float(ap.get("x", 0)), float(ap.get("y", 0))
        bx, by = float(bp.get("x", 0)), float(bp.get("y", 0))
        a_ref, a_pad = _parse_pad_ref(a.get("description", ""))
        b_ref, b_pad = _parse_pad_ref(b.get("description", ""))
        length = math.hypot(bx - ax, by - ay)
        out.append({
            "net": a.get("net") or b.get("net") or "?",
            "from_xy_mm": [round(ax, 3), round(ay, 3)],
            "to_xy_mm": [round(bx, 3), round(by, 3)],
            "from_ref": a_ref, "from_pad": a_pad, "from_layer": a.get("layer"),
            "to_ref": b_ref, "to_pad": b_pad, "to_layer": b.get("layer"),
            "length_mm": round(length, 3),
        })
    return out


def get_ratsnest(
    board: Any,
    drc_violations_path: Optional[str] = None,
    net_filter: Optional[List[str]] = None,
    ref_filter: Optional[List[str]] = None,
    include_segments: bool = True,
    include_crossings: bool = True,
    max_segments: int = 1000,
    top_n_crossings_per_ref: int = 10,
) -> Dict[str, Any]:
    """Return per-segment ratsnest data + crossing-pair detection.

    Filters:
      * ``net_filter``: restrict to nets in this list (e.g. ["BAT+","V12_OUT"]).
      * ``ref_filter``: restrict to segments whose endpoint touches a ref
        in this list (e.g. ["U1","U3"] to inspect those components' nets).
      * ``include_segments``: emit the segment list (default True). Set
        False if you only want the crossing/summary numbers.
      * ``include_crossings``: O(N²) detection of segment crossings on
        different nets (default True).

    Output is always summary + (segments?) + (crossings?) + per-ref
    crossing-count rollup (top contributors).
    """
    if not drc_violations_path:
        return {
            "success": False,
            "message": "drcViolationsPath required (or auto-discovered by caller)",
            "errorDetails": "Run get_drc_violations or run_drc first.",
        }
    rats = _load_ratsnest(Path(drc_violations_path))

    if net_filter:
        nf = set(net_filter)
        rats = [r for r in rats if r["net"] in nf]
    if ref_filter:
        rf = set(ref_filter)
        rats = [r for r in rats if r["from_ref"] in rf or r["to_ref"] in rf]

    total_length = sum(r["length_mm"] for r in rats)

    # Crossings
    crossings: List[Dict[str, Any]] = []
    crossings_by_ref: Dict[str, int] = defaultdict(int)
    if include_crossings:
        n = len(rats)
        for i in range(n):
            a = rats[i]
            for j in range(i + 1, n):
                b = rats[j]
                if a["net"] == b["net"]:
                    continue
                pt = _segments_intersect(
                    tuple(a["from_xy_mm"]), tuple(a["to_xy_mm"]),
                    tuple(b["from_xy_mm"]), tuple(b["to_xy_mm"]),
                )
                if pt is None:
                    continue
                crossings.append({
                    "at_mm": [round(pt[0], 3), round(pt[1], 3)],
                    "net_a": a["net"], "net_b": b["net"],
                    "a_endpoints": f"{a['from_ref']}.{a['from_pad']} <-> {a['to_ref']}.{a['to_pad']}",
                    "b_endpoints": f"{b['from_ref']}.{b['from_pad']} <-> {b['to_ref']}.{b['to_pad']}",
                })
                for r in (a, b):
                    for ref in (r["from_ref"], r["to_ref"]):
                        if ref:
                            crossings_by_ref[ref] += 1

    # Per-ref ranked by crossing contribution
    top_refs = sorted(crossings_by_ref.items(), key=lambda kv: -kv[1])
    top_ref_list = [
        {"ref": ref, "crossing_count": n}
        for ref, n in top_refs[:top_n_crossings_per_ref] if n > 0
    ]

    # Per-net summary
    by_net_count: Dict[str, int] = defaultdict(int)
    by_net_length: Dict[str, float] = defaultdict(float)
    for r in rats:
        by_net_count[r["net"]] += 1
        by_net_length[r["net"]] += r["length_mm"]
    net_summary = [
        {"net": n, "segments": by_net_count[n], "total_length_mm": round(by_net_length[n], 2)}
        for n in by_net_count
    ]
    net_summary.sort(key=lambda d: -d["total_length_mm"])

    result: Dict[str, Any] = {
        "success": True,
        "total_segments": len(rats),
        "total_length_mm": round(total_length, 2),
        "crossing_count": len(crossings),
        "top_crossing_contributors": top_ref_list,
        "net_summary": net_summary,
        "message": (
            f"get_ratsnest: {len(rats)} segments, {round(total_length, 1)} mm total, "
            f"{len(crossings)} crossings"
        ),
    }
    if include_segments:
        # Truncate to keep payload tractable
        if len(rats) > max_segments:
            result["segments_truncated"] = True
            result["segments"] = rats[:max_segments]
        else:
            result["segments"] = rats
    if include_crossings:
        result["crossings"] = crossings
    return result
