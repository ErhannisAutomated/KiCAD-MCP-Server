"""Region-scoped copper cleanup (``scrub_region``).

After a re-placement or incremental re-route, a moved component leaves
stale copper behind. Net-name stripping (``_remove_net_routing``) is
all-or-nothing, so it can't remove "the charger's slice of USB_VBUS"
without wiping the whole rail. ``scrub_region`` adds the missing
geometric scope: given target components, delete copper that is either

  1. on a **target-only net** (every pad on the net belongs to a target)
     — anywhere, any layer (the private routing); or
  2. on a **shared net** (touches a target *and* a non-target) — only the
     portion inside the targets' convex hull.

Then a recursive **dead-end prune** sweeps anything left dangling.

Design rationale, params, and the full per-element decision table live in
``docs/SCRUB_REGION_PLAN.md``. Deletions use ``BOARD.RemoveNative`` (the
``Remove()`` SWIG corruption lesson, commit 5fc25b3).

The geometry, net-classification, and prune helpers are pure functions so
they unit-test without pcbnew.
"""
from __future__ import annotations

import logging
import math
import os
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import pcbnew

logger = logging.getLogger("kicad_interface")

Point = Tuple[float, float]

# Quantization for endpoint-coincidence in the dead-end prune (nm). KiCad
# coords are integer nm; 1 µm tolerance absorbs rounding without merging
# genuinely distinct endpoints.
_COINCIDENCE_EPS_NM = 1000


# --------------------------------------------------------------------------
# Pure geometry helpers
# --------------------------------------------------------------------------
def _convex_hull(points: Sequence[Point]) -> List[Point]:
    """Andrew's monotone chain. Returns the hull in CCW order. For 0–2
    unique points returns them as-is (degenerate hull = point or segment)."""
    pts = sorted(set((float(x), float(y)) for x, y in points))
    if len(pts) <= 2:
        return pts

    def cross(o: Point, a: Point, b: Point) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: List[Point] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: List[Point] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _point_in_poly(pt: Point, poly: Sequence[Point]) -> bool:
    """Ray-casting point-in-polygon. Polygon must have >= 3 vertices."""
    if len(poly) < 3:
        return False
    x, y = pt
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / (yj - yi) + xi
        ):
            inside = not inside
        j = i
    return inside


def _dist_point_to_segment(p: Point, a: Point, b: Point) -> float:
    px, py = p
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)


def _dist_point_to_hull(pt: Point, hull: Sequence[Point]) -> float:
    """0 if pt is inside a polygonal hull, else distance to the nearest
    boundary. Degenerate hulls (point / segment) use point/segment
    distance directly."""
    if not hull:
        return float("inf")
    if len(hull) == 1:
        return math.hypot(pt[0] - hull[0][0], pt[1] - hull[0][1])
    if len(hull) == 2:
        return _dist_point_to_segment(pt, hull[0], hull[1])
    if _point_in_poly(pt, hull):
        return 0.0
    n = len(hull)
    return min(
        _dist_point_to_segment(pt, hull[i], hull[(i + 1) % n]) for i in range(n)
    )


def _within_hull(pt: Point, hull: Sequence[Point], margin: float) -> bool:
    """True if pt is inside the hull or within `margin` of its boundary.
    This is the margin-as-distance trick: "inside hull inflated by m" ==
    distance(pt, hull) <= m, with no polygon-offset construction."""
    return _dist_point_to_hull(pt, hull) <= margin


def _segments_cross(p1: Point, p2: Point, p3: Point, p4: Point) -> bool:
    def orient(a: Point, b: Point, c: Point) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    d1 = orient(p3, p4, p1)
    d2 = orient(p3, p4, p2)
    d3 = orient(p1, p2, p3)
    d4 = orient(p1, p2, p4)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return True
    return False


def _segment_intersects_hull(
    p1: Point, p2: Point, hull: Sequence[Point], margin: float
) -> bool:
    """True if the segment p1-p2 touches the (margin-inflated) hull —
    either endpoint inside, the segment crosses a hull edge, or the
    segment passes within `margin` of a hull edge."""
    if _within_hull(p1, hull, margin) or _within_hull(p2, hull, margin):
        return True
    if len(hull) < 2:
        return False
    n = len(hull)
    edges = (
        [(hull[i], hull[(i + 1) % n]) for i in range(n)]
        if n >= 3
        else [(hull[0], hull[1])]
    )
    for e1, e2 in edges:
        if _segments_cross(p1, p2, e1, e2):
            return True
        # near-miss within margin (segment-to-segment min distance)
        if min(
            _dist_point_to_segment(p1, e1, e2),
            _dist_point_to_segment(p2, e1, e2),
            _dist_point_to_segment(e1, p1, p2),
            _dist_point_to_segment(e2, p1, p2),
        ) <= margin:
            return True
    return False


# --------------------------------------------------------------------------
# Pure net classification
# --------------------------------------------------------------------------
def _classify_nets(
    net_to_refs: Dict[str, Set[str]], target_refs: Set[str]
) -> Tuple[Set[str], Set[str]]:
    """Partition nets into (target_only, shared) relative to the targets.

    target_only: net touches >=1 target and *no* non-target pad.
    shared:      net touches >=1 target *and* >=1 non-target.
    Nets touching no target are returned in neither set (never scrubbed).
    """
    target_only: Set[str] = set()
    shared: Set[str] = set()
    for net, refs in net_to_refs.items():
        if not net or not (refs & target_refs):
            continue
        if refs <= target_refs:
            target_only.add(net)
        else:
            shared.add(net)
    return target_only, shared


# --------------------------------------------------------------------------
# Pure recursive dead-end prune
# --------------------------------------------------------------------------
def _prune_dead_ends(
    segments: List[Dict[str, Any]],
    vias: List[Dict[str, Any]],
    pads: Set[Tuple[str, int, int]],
    zone_bboxes: List[Tuple[str, float, float, float, float]],
    eps: int = _COINCIDENCE_EPS_NM,
) -> Tuple[Set[Any], Set[Any]]:
    """Iteratively remove dangling copper on the involved nets to a fixpoint.

    A track is dangling when one of its endpoints is *free* — coincident
    with no same-net pad, no other active track endpoint, no active via,
    and not inside a same-net zone (pours connect copper without a
    coincident endpoint, so zone bboxes are a conservative "connected"
    proxy that errs toward keeping tracks).

    A via is pruned when it has < 2 same-net connections (active track
    endpoints + pad + zone-containment), i.e. it stitches nothing.

    Inputs are *post-primary-delete* (already exclude the geometric/net
    kill set). Returns (pruned_segment_ids, pruned_via_ids).
    """
    def q(v: float) -> int:
        return int(round(v / eps))

    seg_active = {s["id"]: True for s in segments}
    via_active = {v["id"]: True for v in vias}
    pad_keys = {(net, q(x), q(y)) for (net, x, y) in pads}

    def in_zone(net: str, x: float, y: float) -> bool:
        for znet, x0, y0, x1, y1 in zone_bboxes:
            if znet == net and x0 <= x <= x1 and y0 <= y <= y1:
                return True
        return False

    def key(net: str, p: Point) -> Tuple[str, int, int]:
        return (net, q(p[0]), q(p[1]))

    changed = True
    while changed:
        changed = False

        # Occupancy from currently-active segs + vias (pads are permanent).
        occ: Dict[Tuple[str, int, int], int] = {}
        for s in segments:
            if not seg_active[s["id"]]:
                continue
            for e in (s["a"], s["b"]):
                occ[key(s["net"], e)] = occ.get(key(s["net"], e), 0) + 1
        for v in vias:
            if not via_active[v["id"]]:
                continue
            occ[key(v["net"], v["pos"])] = occ.get(key(v["net"], v["pos"]), 0) + 1

        # Deactivate tracks with a free endpoint.
        for s in segments:
            if not seg_active[s["id"]]:
                continue
            free = False
            for e in (s["a"], s["b"]):
                k = key(s["net"], e)
                others = occ.get(k, 0) - 1  # subtract this seg's own contribution
                connected = (
                    others > 0
                    or k in pad_keys
                    or in_zone(s["net"], e[0], e[1])
                )
                if not connected:
                    free = True
                    break
            if free:
                seg_active[s["id"]] = False
                changed = True

        if changed:
            continue  # recompute occupancy before touching vias

        # Vias that stitch < 2 same-net connections.
        for v in vias:
            if not via_active[v["id"]]:
                continue
            k = key(v["net"], v["pos"])
            conns = occ.get(k, 0) - 1  # exclude the via's own contribution
            if k in pad_keys:
                conns += 1
            if in_zone(v["net"], v["pos"][0], v["pos"][1]):
                conns += 2  # pour on both sides — treat as fully stitched
            if conns < 2:
                via_active[v["id"]] = False
                changed = True

    pruned_segs = {s["id"] for s in segments if not seg_active[s["id"]]}
    pruned_vias = {v["id"] for v in vias if not via_active[v["id"]]}
    return pruned_segs, pruned_vias


# --------------------------------------------------------------------------
# Board-driven command
# --------------------------------------------------------------------------
class ScrubRegionCommands:
    """Handles the ``scrub_region`` PCB-cleanup operation."""

    def __init__(self, board: Optional["pcbnew.BOARD"] = None):
        self.board = board

    # -- small board helpers ------------------------------------------------
    def _layer_name(self, layer_id: int) -> str:
        try:
            return self.board.GetLayerName(layer_id)
        except Exception:
            return str(layer_id)

    def _fp_corners(self, fp: Any) -> List[Point]:
        """Hull-source corners for a footprint: courtyard-inclusive bbox
        (GetBoundingBox(False) excludes text but includes pads/courtyard)."""
        bb = fp.GetBoundingBox(False)
        return [
            (float(bb.GetLeft()), float(bb.GetTop())),
            (float(bb.GetRight()), float(bb.GetTop())),
            (float(bb.GetRight()), float(bb.GetBottom())),
            (float(bb.GetLeft()), float(bb.GetBottom())),
        ]

    def scrub_region(self, params: Dict[str, Any]) -> Dict[str, Any]:
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            targets = params.get("targets") or []
            if not targets:
                return {
                    "success": False,
                    "message": "Missing parameters",
                    "errorDetails": "targets (a list of component references) is required",
                }
            target_refs: Set[str] = set(targets)

            layer_names = params.get("layers") or ["F.Cu", "B.Cu"]
            margin_mm = float(params.get("marginMm", 1.0))
            margin_nm = margin_mm * 1e6
            affect_tracks = bool(params.get("affectTracks", True))
            affect_vias = bool(params.get("affectVias", True))
            affect_zones = bool(params.get("affectZones", True))
            intersect_hull = bool(params.get("intersectHull", False))
            prune_dead_ends = bool(params.get("pruneDeadEnds", True))
            dry_run = bool(params.get("dryRun", True))
            do_viz = bool(params.get("viz", True))

            scope_layer_ids: Set[int] = set()
            for ln in layer_names:
                lid = self.board.GetLayerID(ln)
                if lid >= 0:
                    scope_layer_ids.add(lid)

            footprints = list(self.board.GetFootprints())
            fp_by_ref = {fp.GetReference(): fp for fp in footprints}
            missing = [r for r in target_refs if r not in fp_by_ref]
            if missing:
                return {
                    "success": False,
                    "message": "Unknown target component(s)",
                    "errorDetails": f"Not found on board: {', '.join(sorted(missing))}",
                }

            # --- net classification --------------------------------------
            net_to_refs: Dict[str, Set[str]] = {}
            for fp in footprints:
                ref = fp.GetReference()
                for pad in fp.Pads():
                    net = pad.GetNetname()
                    if net:
                        net_to_refs.setdefault(net, set()).add(ref)
            target_only, shared = _classify_nets(net_to_refs, target_refs)
            involved = target_only | shared

            # --- hulls ---------------------------------------------------
            front_pts: List[Point] = []
            back_pts: List[Point] = []
            all_pts: List[Point] = []
            for ref in target_refs:
                fp = fp_by_ref[ref]
                corners = self._fp_corners(fp)
                all_pts.extend(corners)
                if self._layer_name(fp.GetLayer()) == "B.Cu":
                    back_pts.extend(corners)
                else:
                    front_pts.extend(corners)
            hull_front = _convex_hull(front_pts)
            hull_back = _convex_hull(back_pts)
            hull_all = _convex_hull(all_pts)

            def hull_for(layer_name: str) -> List[Point]:
                if layer_name == "F.Cu":
                    return hull_front
                if layer_name == "B.Cu":
                    return hull_back
                return hull_all

            # --- pass 1: net/geometry match (with reasons) ---------------
            del_tracks: List[Tuple[Any, str]] = []  # (item, reason)
            del_vias: List[Tuple[Any, str]] = []
            del_zones: List[Tuple[Any, str]] = []
            flagged: List[Dict[str, Any]] = []

            for t in self.board.Tracks():
                tp = t.Type()
                net = t.GetNetname()
                if net not in involved:
                    continue
                if tp == pcbnew.PCB_VIA_T:
                    if not affect_vias:
                        continue
                    if not any(t.IsOnLayer(lid) for lid in scope_layer_ids):
                        continue
                    if net in target_only:
                        del_vias.append((t, "target-only-net"))
                        continue
                    pos = t.GetPosition()
                    if _within_hull((pos.x, pos.y), hull_all, margin_nm):
                        del_vias.append((t, "in-hull"))
                    continue
                # track or arc
                if not affect_tracks:
                    continue
                if t.GetLayer() not in scope_layer_ids:
                    continue
                if net in target_only:
                    del_tracks.append((t, "target-only-net"))
                    continue
                hull = hull_for(self._layer_name(t.GetLayer()))
                s, e = t.GetStart(), t.GetEnd()
                if _within_hull((s.x, s.y), hull, margin_nm) or _within_hull(
                    (e.x, e.y), hull, margin_nm
                ):
                    del_tracks.append((t, "endpoint-in-hull"))
                elif intersect_hull and _segment_intersects_hull(
                    (s.x, s.y), (e.x, e.y), hull, margin_nm
                ):
                    del_tracks.append((t, "intersects-hull"))

            for z in self.board.Zones():
                net = z.GetNetname()
                if net not in involved:
                    continue
                if not affect_zones:
                    continue
                if z.GetLayer() not in scope_layer_ids:
                    continue
                if net in target_only:
                    del_zones.append((z, "target-only-net"))
                    continue
                # shared-net zone: delete only if fully inside the hull;
                # if it straddles the boundary (connects to non-targets
                # outside), flag and leave.
                hull = hull_for(self._layer_name(z.GetLayer()))
                bb = z.GetBoundingBox()
                corners = [
                    (float(bb.GetLeft()), float(bb.GetTop())),
                    (float(bb.GetRight()), float(bb.GetTop())),
                    (float(bb.GetRight()), float(bb.GetBottom())),
                    (float(bb.GetLeft()), float(bb.GetBottom())),
                ]
                inside = [_within_hull(c, hull, margin_nm) for c in corners]
                if all(inside):
                    del_zones.append((z, "in-hull"))
                elif any(inside):
                    flagged.append({
                        "type": "zone",
                        "uuid": z.m_Uuid.AsString(),
                        "net": net,
                        "layer": self._layer_name(z.GetLayer()),
                        "reason": "shared-net zone straddles hull boundary — spared",
                    })

            primary_track_ids = {t.m_Uuid.AsString() for t, _ in del_tracks}
            primary_via_ids = {v.m_Uuid.AsString() for v, _ in del_vias}

            # --- pass 2: recursive dead-end prune ------------------------
            prune_reason: Dict[str, str] = {}
            if prune_dead_ends:
                segments: List[Dict[str, Any]] = []
                vias_p: List[Dict[str, Any]] = []
                obj_by_uuid: Dict[str, Any] = {}
                for t in self.board.Tracks():
                    net = t.GetNetname()
                    if net not in involved:
                        continue
                    uid = t.m_Uuid.AsString()
                    if t.Type() == pcbnew.PCB_VIA_T:
                        if uid in primary_via_ids:
                            continue
                        pos = t.GetPosition()
                        vias_p.append({"id": uid, "net": net, "pos": (pos.x, pos.y)})
                        obj_by_uuid[uid] = t
                    else:
                        if uid in primary_track_ids:
                            continue
                        s, e = t.GetStart(), t.GetEnd()
                        segments.append({
                            "id": uid, "net": net,
                            "a": (s.x, s.y), "b": (e.x, e.y),
                        })
                        obj_by_uuid[uid] = t
                pads_p: Set[Tuple[str, int, int]] = set()
                for fp in footprints:
                    for pad in fp.Pads():
                        net = pad.GetNetname()
                        if net in involved:
                            pp = pad.GetPosition()
                            pads_p.add((net, pp.x, pp.y))
                zone_bb: List[Tuple[str, float, float, float, float]] = []
                surviving_zone_ids = {
                    z.m_Uuid.AsString() for z, _ in del_zones
                }
                for z in self.board.Zones():
                    net = z.GetNetname()
                    if net not in involved:
                        continue
                    if z.m_Uuid.AsString() in surviving_zone_ids:
                        continue  # will be deleted — not a connectivity anchor
                    bb = z.GetBoundingBox()
                    zone_bb.append((
                        net, float(bb.GetLeft()), float(bb.GetTop()),
                        float(bb.GetRight()), float(bb.GetBottom()),
                    ))
                pruned_segs, pruned_vias = _prune_dead_ends(
                    segments, vias_p, pads_p, zone_bb
                )
                for uid in pruned_segs:
                    del_tracks.append((obj_by_uuid[uid], "dead-end-prune"))
                    prune_reason[uid] = "dead-end-prune"
                for uid in pruned_vias:
                    del_vias.append((obj_by_uuid[uid], "dead-end-prune"))
                    prune_reason[uid] = "dead-end-prune"

            # --- interlopers: non-targets inside a hull ------------------
            interlopers: List[Dict[str, Any]] = []
            for fp in footprints:
                ref = fp.GetReference()
                if ref in target_refs:
                    continue
                pos = fp.GetPosition()
                in_layers = []
                for ln in ("F.Cu", "B.Cu"):
                    if ln in layer_names and _within_hull(
                        (pos.x, pos.y), hull_for(ln), margin_nm
                    ):
                        in_layers.append(ln)
                if not in_layers and _within_hull(
                    (pos.x, pos.y), hull_all, margin_nm
                ):
                    in_layers.append("combined")
                if in_layers:
                    fp_nets = {
                        pad.GetNetname() for pad in fp.Pads() if pad.GetNetname()
                    }
                    interlopers.append({
                        "ref": ref,
                        "insideHullLayers": in_layers,
                        "affectedNets": sorted(fp_nets & involved),
                    })

            # --- build payload -------------------------------------------
            now_open: Set[str] = set()
            track_descs = []
            for t, reason in del_tracks:
                track_descs.append(self._describe(t, reason))
                now_open.add(t.GetNetname())
            via_descs = []
            for v, reason in del_vias:
                via_descs.append(self._describe(v, reason))
                now_open.add(v.GetNetname())
            zone_descs = [
                {
                    "uuid": z.m_Uuid.AsString(),
                    "net": z.GetNetname(),
                    "layer": self._layer_name(z.GetLayer()),
                    "reason": reason,
                }
                for z, reason in del_zones
            ]

            viz_path = None
            if do_viz:
                try:
                    viz_path = _render_scrub_viz(
                        self.board, target_refs, hull_front, hull_back,
                        hull_all, layer_names, margin_nm,
                        del_tracks, del_vias,
                    )
                except Exception as ex:  # viz is best-effort
                    logger.warning(f"scrub_region viz failed: {ex}")

            result = {
                "success": True,
                "dryRun": dry_run,
                "targets": sorted(target_refs),
                "layers": layer_names,
                "marginMm": margin_mm,
                "wouldDelete": {
                    "tracks": track_descs,
                    "vias": via_descs,
                    "zones": zone_descs,
                },
                "counts": {
                    "tracks": len(track_descs),
                    "vias": len(via_descs),
                    "zones": len(zone_descs),
                    "targetOnlyNets": len(target_only),
                    "sharedNets": len(shared),
                },
                "interlopers": interlopers,
                "flagged": flagged,
                "nowOpenNets": sorted(now_open),
                "vizPath": viz_path,
            }

            if dry_run:
                result["message"] = (
                    f"DRY RUN — would delete {len(track_descs)} tracks, "
                    f"{len(via_descs)} vias, {len(zone_descs)} zones across "
                    f"{len(now_open)} nets. Pass dryRun=false to apply."
                )
                return result

            # --- commit: delete via RemoveNative -------------------------
            for t, _ in del_tracks:
                self.board.RemoveNative(t)
            for v, _ in del_vias:
                self.board.RemoveNative(v)
            for z, _ in del_zones:
                self.board.RemoveNative(z)
            self.board.SetModified()
            board_path = self.board.GetFileName()
            if board_path:
                pcbnew.SaveBoard(board_path, self.board)
            result["message"] = (
                f"Deleted {len(track_descs)} tracks, {len(via_descs)} vias, "
                f"{len(zone_descs)} zones across {len(now_open)} nets. "
                f"Re-route nowOpenNets, then refill_zones + run_drc."
            )
            return result

        except Exception as e:
            logger.exception("scrub_region failed")
            return {
                "success": False,
                "message": "scrub_region failed",
                "errorDetails": str(e),
            }

    def _describe(self, item: Any, reason: str) -> Dict[str, Any]:
        is_via = item.Type() == pcbnew.PCB_VIA_T
        pos = item.GetPosition()
        out: Dict[str, Any] = {
            "uuid": item.m_Uuid.AsString(),
            "type": "via" if is_via else "track",
            "net": item.GetNetname() or "",
            "reason": reason,
            "position": {"x": pos.x / 1e6, "y": pos.y / 1e6, "unit": "mm"},
        }
        if is_via:
            try:
                out["fromLayer"] = self._layer_name(item.TopLayer())
                out["toLayer"] = self._layer_name(item.BottomLayer())
            except Exception:
                pass
        else:
            out["layer"] = self._layer_name(item.GetLayer())
            try:
                s, e = item.GetStart(), item.GetEnd()
                out["start"] = {"x": s.x / 1e6, "y": s.y / 1e6, "unit": "mm"}
                out["end"] = {"x": e.x / 1e6, "y": e.y / 1e6, "unit": "mm"}
            except Exception:
                pass
        return out


# --------------------------------------------------------------------------
# Debug visualization (best-effort; matplotlib optional)
# --------------------------------------------------------------------------
def _render_scrub_viz(
    board: Any,
    target_refs: Set[str],
    hull_front: List[Point],
    hull_back: List[Point],
    hull_all: List[Point],
    layer_names: List[str],
    margin_nm: float,
    del_tracks: List[Tuple[Any, str]],
    del_vias: List[Tuple[Any, str]],
) -> Optional[str]:
    """Render an overlay PNG: target bboxes, hull(s), and matched vs
    unmatched copper. Returns the path or None if matplotlib is absent."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Polygon as MplPoly, Rectangle
    except Exception:
        return None

    def mm(v: float) -> float:
        return v / 1e6

    fig, ax = plt.subplots(figsize=(10, 10))
    matched_ids = {t.m_Uuid.AsString() for t, _ in del_tracks}
    matched_ids |= {v.m_Uuid.AsString() for v, _ in del_vias}

    # all copper (gray = kept, red = matched/deleted)
    for t in board.Tracks():
        uid = t.m_Uuid.AsString()
        color = "red" if uid in matched_ids else "#bbbbbb"
        lw = 1.4 if uid in matched_ids else 0.6
        if t.Type() == pcbnew.PCB_VIA_T:
            pos = t.GetPosition()
            ax.plot(mm(pos.x), mm(pos.y), "o", ms=4, color=color)
        else:
            s, e = t.GetStart(), t.GetEnd()
            ax.plot([mm(s.x), mm(e.x)], [mm(s.y), mm(e.y)], "-", lw=lw, color=color)

    # target bboxes
    fp_by_ref = {fp.GetReference(): fp for fp in board.GetFootprints()}
    for ref in target_refs:
        fp = fp_by_ref.get(ref)
        if not fp:
            continue
        bb = fp.GetBoundingBox(False)
        ax.add_patch(Rectangle(
            (mm(bb.GetLeft()), mm(bb.GetTop())),
            mm(bb.GetRight() - bb.GetLeft()),
            mm(bb.GetBottom() - bb.GetTop()),
            fill=False, edgecolor="green", lw=1.0,
        ))
        ax.text(mm(fp.GetPosition().x), mm(fp.GetPosition().y), ref,
                color="green", fontsize=7, ha="center", va="center")

    for hull, col, lbl in (
        (hull_front, "blue", "F.Cu hull"),
        (hull_back, "purple", "B.Cu hull"),
    ):
        if len(hull) >= 3:
            ax.add_patch(MplPoly(
                [(mm(x), mm(y)) for x, y in hull],
                fill=False, edgecolor=col, lw=1.5, label=lbl,
            ))

    ax.set_aspect("equal")
    ax.invert_yaxis()  # KiCad y is down
    ax.set_title(
        f"scrub_region: {len(matched_ids)} items matched  "
        f"(margin {margin_nm/1e6:.2f} mm, layers {','.join(layer_names)})"
    )
    ax.legend(loc="upper right", fontsize=7)

    out_dir = "/tmp/claude-1000"
    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception:
        out_dir = "/tmp"
    path = os.path.join(out_dir, "scrub_region_preview.png")
    fig.savefig(path, dpi=110, bbox_inches="tight")
    plt.close(fig)
    return path
