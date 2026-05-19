"""Force-directed PCB placement relaxation (relax_placement).

Pulls connected components together (springs along ratsnest segments)
while pushing overlapping ones apart (pair repulsion), keeping anchored
components fixed (connectors, cell holders) and respecting a keep-in
rectangle (default: the board edge bbox).

This is NOT a full autoplacer — it does not invent placement from
scratch. It assumes the caller has already done a rough by-group
placement (manually or via place_near) and just needs to shake the
result until the ratsnest stops crossing itself. That matches the
workflow described in the power_module project notes: rough
group-by-group → relax → metrics → fix problems.

Algorithm (one iteration):

  forces = 0
  for each ratsnest segment (a, b):       # spring attract
      v = pos[b] - pos[a]
      forces[a] += k_attract * v
      forces[b] -= k_attract * v

  for each component pair (a, b):         # hard repulsion on overlap
      overlap = bbox_overlap(a, b, padding=min_gap)
      if overlap > 0:
          push = overlap_direction(a, b) * overlap / 2
          forces[a] -= push
          forces[b] += push

  for each non-anchored component:
      proposed = pos + forces * step_size
      proposed = clamp_to_bbox(proposed, keep_in)
      pos = proposed

  step_size *= damping

Forces are computed on component CENTERS, not pad positions. A pad-aware
v2 would route around fan-out direction; v1's "pull centers toward each
other" is a useful approximation that converges fast.

Anchors default: J*, BAT1, SW1, and any footprint flagged as
``fp_through_hole`` (connectors, mounting holes). Override via
``lockedRefs``.

Validation: returns before/after ratsnest length + crossing count so
the caller knows whether the relax helped. If it didn't, the caller
can revert (no commit until the user accepts).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pcbnew

logger = logging.getLogger("kicad_interface")


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass
class PCBComp:
    ref: str
    x_mm: float
    y_mm: float
    w_mm: float            # bbox half-width
    h_mm: float            # bbox half-height
    layer: str             # F.Cu / B.Cu
    anchored: bool = False


@dataclass
class RatsSeg:
    """One ratsnest segment, with endpoints already resolved to component refs."""
    net: str
    a_ref: str
    b_ref: str
    a_xy: Tuple[float, float]   # absolute pad xy (used for crossing detection,
    b_xy: Tuple[float, float]   #   re-computed each iteration as comp center)


@dataclass
class Params:
    k_attract: float = 0.02          # spring constant (per mm of error)
    k_repulse_step: float = 1.0      # how much of overlap to push per iter
    min_gap_mm: float = 0.30         # padding around bboxes for repulsion
    step_mm: float = 1.0             # max move per iteration (cap)
    max_iters: int = 200
    damping: float = 0.99
    keep_in: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)  # (l,t,r,b)


# ---------------------------------------------------------------------------
# Load board state
# ---------------------------------------------------------------------------


_DEFAULT_ANCHOR_PREFIXES = ("J", "SW", "BAT")  # connectors, switches, cell holders


def _load_comps(board: Any, locked_refs: Optional[Set[str]]) -> Dict[str, PCBComp]:
    """Read footprints into PCBComp records.

    A footprint is anchored if its ref is in ``locked_refs`` OR (when
    locked_refs is None) starts with one of ``_DEFAULT_ANCHOR_PREFIXES``
    OR is a through-hole-mostly footprint (treats connectors / mounting
    holes / cell holders correctly even when their ref doesn't match).
    """
    comps: Dict[str, PCBComp] = {}
    scale = 1_000_000.0
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        pos = fp.GetPosition()
        bb = fp.GetBoundingBox(False)
        layer = board.GetLayerName(fp.GetLayer())

        # Through-hole-dominant footprint = anchor by default.
        nb_smd = sum(1 for p in fp.Pads() if p.GetAttribute() == pcbnew.PAD_ATTRIB_SMD)
        nb_tht = sum(1 for p in fp.Pads()
                     if p.GetAttribute() in (pcbnew.PAD_ATTRIB_PTH,
                                              pcbnew.PAD_ATTRIB_NPTH))

        if locked_refs is not None:
            anchored = ref in locked_refs
        else:
            anchored = (
                ref.startswith(_DEFAULT_ANCHOR_PREFIXES)
                or (nb_tht > 0 and nb_tht >= nb_smd)
            )

        comps[ref] = PCBComp(
            ref=ref,
            x_mm=pos.x / scale,
            y_mm=pos.y / scale,
            w_mm=bb.GetWidth() / (2 * scale),
            h_mm=bb.GetHeight() / (2 * scale),
            layer=layer,
            anchored=anchored,
        )
    return comps


def _load_rats(drc_path: Path, comps: Dict[str, PCBComp]) -> List[RatsSeg]:
    """Parse DRC unconnected_items into ratsnest segments scoped to
    component refs we know about."""
    import json, re
    if not drc_path.exists():
        return []
    try:
        data = json.loads(drc_path.read_text())
    except json.JSONDecodeError:
        return []
    pad_re = re.compile(r"[Pp]ad\s+(\S+)\s+\[[^\]]*\]\s+of\s+(\S+)")
    out: List[RatsSeg] = []
    for v in data.get("violations", []):
        if v.get("type") != "unconnected_items":
            continue
        items = v.get("items") or []
        if len(items) < 2:
            continue
        a, b = items[0], items[1]
        a_desc = a.get("description", "")
        b_desc = b.get("description", "")
        am = pad_re.search(a_desc)
        bm = pad_re.search(b_desc)
        if not (am and bm):
            continue
        a_ref = am.group(2)
        b_ref = bm.group(2)
        if a_ref == b_ref or a_ref not in comps or b_ref not in comps:
            continue
        a_pos = a.get("pos") or {}
        b_pos = b.get("pos") or {}
        out.append(RatsSeg(
            net=a.get("net") or "?",
            a_ref=a_ref, b_ref=b_ref,
            a_xy=(float(a_pos.get("x", 0)), float(a_pos.get("y", 0))),
            b_xy=(float(b_pos.get("x", 0)), float(b_pos.get("y", 0))),
        ))
    return out


# ---------------------------------------------------------------------------
# Force computations
# ---------------------------------------------------------------------------


def _spring_force(rats: List[RatsSeg], comps: Dict[str, PCBComp], k: float
                  ) -> Dict[str, Tuple[float, float]]:
    """Pull connected component centers toward each other."""
    forces: Dict[str, Tuple[float, float]] = {r: (0.0, 0.0) for r in comps}
    for seg in rats:
        a = comps[seg.a_ref]
        b = comps[seg.b_ref]
        dx = b.x_mm - a.x_mm
        dy = b.y_mm - a.y_mm
        fx, fy = k * dx, k * dy
        ax, ay = forces[seg.a_ref]
        bx, by = forces[seg.b_ref]
        forces[seg.a_ref] = (ax + fx, ay + fy)
        forces[seg.b_ref] = (bx - fx, by - fy)
    return forces


def _repulse_force(comps: Dict[str, PCBComp], forces: Dict[str, Tuple[float, float]],
                   min_gap: float, step: float) -> int:
    """Push overlapping component pairs apart. Same-layer only. Returns
    the number of overlapping pairs found."""
    refs = list(comps)
    overlaps = 0
    for i in range(len(refs)):
        a = comps[refs[i]]
        for j in range(i + 1, len(refs)):
            b = comps[refs[j]]
            if a.layer != b.layer:
                continue
            # Compute overlap depth along each axis
            dx = b.x_mm - a.x_mm
            dy = b.y_mm - a.y_mm
            ox = (a.w_mm + b.w_mm + min_gap) - abs(dx)
            oy = (a.h_mm + b.h_mm + min_gap) - abs(dy)
            if ox <= 0 or oy <= 0:
                continue
            overlaps += 1
            # Push along the shallower axis (cheaper to resolve)
            if ox < oy:
                push = ox * step * 0.5 * (-1 if dx >= 0 else 1)
                ax, ay = forces[a.ref]; bx, by = forces[b.ref]
                forces[a.ref] = (ax + push, ay)
                forces[b.ref] = (bx - push, by)
            else:
                push = oy * step * 0.5 * (-1 if dy >= 0 else 1)
                ax, ay = forces[a.ref]; bx, by = forces[b.ref]
                forces[a.ref] = (ax, ay + push)
                forces[b.ref] = (bx, by - push)
    return overlaps


def _apply_forces(comps: Dict[str, PCBComp], forces: Dict[str, Tuple[float, float]],
                  step_cap: float, keep_in: Tuple[float, float, float, float]) -> None:
    """Move non-anchored comps by their force vector, clamping to step_cap
    in magnitude and the keep_in bbox."""
    kl, kt, kr, kb = keep_in
    for ref, comp in comps.items():
        if comp.anchored:
            continue
        fx, fy = forces[ref]
        mag = math.hypot(fx, fy)
        if mag > step_cap:
            fx *= step_cap / mag
            fy *= step_cap / mag
        nx = comp.x_mm + fx
        ny = comp.y_mm + fy
        # Clamp to keep-in (half-bbox so we don't poke out)
        if kr > kl:
            nx = max(kl + comp.w_mm, min(kr - comp.w_mm, nx))
            ny = max(kt + comp.h_mm, min(kb - comp.h_mm, ny))
        comp.x_mm = nx
        comp.y_mm = ny


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _total_ratsnest_length(rats: List[RatsSeg], comps: Dict[str, PCBComp]) -> float:
    """Use component centers as proxy for pad positions (after relax,
    pads at the edge of the bbox move with the center)."""
    total = 0.0
    for seg in rats:
        a = comps[seg.a_ref]
        b = comps[seg.b_ref]
        total += math.hypot(b.x_mm - a.x_mm, b.y_mm - a.y_mm)
    return total


def _seg_intersect(p1, p2, p3, p4) -> bool:
    x1, y1 = p1; x2, y2 = p2; x3, y3 = p3; x4, y4 = p4
    denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(denom) < 1e-9:
        return False
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / denom
    u = -((x1 - x2) * (y1 - y3) - (y1 - y2) * (x1 - x3)) / denom
    return 1e-6 < t < 1 - 1e-6 and 1e-6 < u < 1 - 1e-6


def _crossing_count(rats: List[RatsSeg], comps: Dict[str, PCBComp]) -> int:
    """Pairwise crossing count using component centers as ratsnest endpoints."""
    pts = [
        (seg.net,
         (comps[seg.a_ref].x_mm, comps[seg.a_ref].y_mm),
         (comps[seg.b_ref].x_mm, comps[seg.b_ref].y_mm))
        for seg in rats
    ]
    n = len(pts)
    count = 0
    for i in range(n):
        for j in range(i + 1, n):
            if pts[i][0] == pts[j][0]:
                continue
            if _seg_intersect(pts[i][1], pts[i][2], pts[j][1], pts[j][2]):
                count += 1
    return count


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def relax_placement(
    board: Any,
    drc_violations_path: Optional[str],
    locked_refs: Optional[List[str]] = None,
    max_iters: int = 200,
    k_attract: float = 0.02,
    k_repulse_step: float = 1.0,
    min_gap_mm: float = 0.30,
    step_mm: float = 1.0,
    damping: float = 0.99,
    keep_in_bbox: Optional[Tuple[float, float, float, float]] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Run force-directed relaxation. Returns before/after metrics + moves.

    If ``dry_run`` is True, computes the final positions but does NOT
    write them back to the board — useful for trying parameters.
    Otherwise mutates the board in place (auto-save will persist it).
    """
    if not drc_violations_path:
        return {
            "success": False,
            "message": "drcViolationsPath required",
            "errorDetails": "Run get_drc_violations or run_drc first.",
        }

    locked = set(locked_refs) if locked_refs is not None else None
    comps = _load_comps(board, locked)
    rats = _load_rats(Path(drc_violations_path), comps)

    if not rats:
        return {
            "success": False,
            "message": "No ratsnest segments found.",
            "errorDetails": "Empty DRC unconnected_items or no parsable pad refs.",
        }

    # Default keep-in: board edge bbox.
    if keep_in_bbox is None:
        edge_id = board.GetLayerID("Edge.Cuts")
        l = t = math.inf
        r = b = -math.inf
        for d in board.GetDrawings():
            if d.GetLayer() != edge_id:
                continue
            bb = d.GetBoundingBox()
            l = min(l, bb.GetLeft() / 1_000_000.0)
            t = min(t, bb.GetTop() / 1_000_000.0)
            r = max(r, bb.GetRight() / 1_000_000.0)
            b = max(b, bb.GetBottom() / 1_000_000.0)
        if math.isfinite(l):
            # Tighten by 1mm so trace stubs don't poke the edge.
            keep_in_bbox = (l + 1.0, t + 1.0, r - 1.0, b - 1.0)
        else:
            keep_in_bbox = (0.0, 0.0, 0.0, 0.0)

    before_length = _total_ratsnest_length(rats, comps)
    before_crossings = _crossing_count(rats, comps)
    before_positions = {ref: (c.x_mm, c.y_mm) for ref, c in comps.items()}

    step = step_mm
    final_overlaps = 0
    for it in range(max_iters):
        forces = _spring_force(rats, comps, k_attract)
        final_overlaps = _repulse_force(comps, forces, min_gap_mm, k_repulse_step)
        _apply_forces(comps, forces, step, keep_in_bbox)
        step *= damping

    after_length = _total_ratsnest_length(rats, comps)
    after_crossings = _crossing_count(rats, comps)

    moves: List[Dict[str, Any]] = []
    for ref, comp in comps.items():
        if comp.anchored:
            continue
        ox, oy = before_positions[ref]
        dx = comp.x_mm - ox
        dy = comp.y_mm - oy
        if math.hypot(dx, dy) < 0.01:
            continue
        moves.append({
            "ref": ref,
            "from": {"x": round(ox, 3), "y": round(oy, 3)},
            "to": {"x": round(comp.x_mm, 3), "y": round(comp.y_mm, 3)},
            "delta_mm": round(math.hypot(dx, dy), 3),
        })
    moves.sort(key=lambda m: -m["delta_mm"])

    # Apply to board unless dry-run
    if not dry_run:
        for ref, comp in comps.items():
            if comp.anchored:
                continue
            fp = board.FindFootprintByReference(ref)
            if fp is None:
                continue
            fp.SetPosition(pcbnew.VECTOR2I_MM(comp.x_mm, comp.y_mm))

    n_anchored = sum(1 for c in comps.values() if c.anchored)
    return {
        "success": True,
        "dry_run": dry_run,
        "params": {
            "max_iters": max_iters,
            "k_attract": k_attract,
            "k_repulse_step": k_repulse_step,
            "min_gap_mm": min_gap_mm,
            "step_mm": step_mm,
            "damping": damping,
            "keep_in_bbox": list(keep_in_bbox),
        },
        "components_total": len(comps),
        "components_anchored": n_anchored,
        "components_moved": len(moves),
        "ratsnest_segments": len(rats),
        "before": {
            "total_length_mm": round(before_length, 2),
            "crossing_count": before_crossings,
        },
        "after": {
            "total_length_mm": round(after_length, 2),
            "crossing_count": after_crossings,
            "remaining_overlaps_last_iter": final_overlaps,
        },
        "delta": {
            "total_length_mm": round(after_length - before_length, 2),
            "crossing_count": after_crossings - before_crossings,
        },
        "top_moves": moves[:20],
        "message": (
            f"relax_placement: len {before_length:.0f} -> {after_length:.0f} mm "
            f"({after_length - before_length:+.0f}), "
            f"crossings {before_crossings} -> {after_crossings} "
            f"({after_crossings - before_crossings:+d})"
        ),
    }
