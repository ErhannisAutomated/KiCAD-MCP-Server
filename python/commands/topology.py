"""Routing-topology analysis (`analyze_routable_regions`, `check_pad_routability`).

Phase 1 of the topology-tools plan (docs/TOPOLOGY_TOOLS_PLAN.md). Exposes
the trace-width configuration space directly so callers can answer
"how is this layer partitioned at width W" and "are these two pads
geometrically reachable at width W, and where's the bottleneck?" without
having to reverse-engineer a router's failure messages.

Core primitive (per the plan):

    free_space(layer, W, C) = board_area \\ (foreign_copper(layer) ⊕ disk(W/2 + C))

Implementation: rasterize foreign copper into a binary mask at grid size
``g`` (default 0.05 mm), take the Euclidean distance transform once, and
derive both the free-space mask (``dist >= (W/2 + C)/g``) and the
bottleneck width along any path (``2*(dist - C/g)*g``) from the same
array.

Pads of the trace's net are NOT obstacles — they're the trace's
terminals; foreign pads are. With no ``net`` set, all copper is treated
as obstacle.

Convergence check (``convergence_check=True``) reruns the same analysis
at ``g/2`` and reports whether the answers agree; this exposes the
grid-quantization error so the caller doesn't have to guess.
"""
from __future__ import annotations

import logging
import math
import os
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pcbnew
from scipy.ndimage import (
    binary_dilation,
    distance_transform_edt,
    label as scipy_label,
)

logger = logging.getLogger("kicad_interface")

SCALE = 1_000_000  # nm per mm — pcbnew internal unit


# --------------------------------------------------------------------------
# Board extents
# --------------------------------------------------------------------------
def _board_bbox_mm(board: Any) -> Tuple[float, float, float, float]:
    """Return (xmin, ymin, xmax, ymax) in mm from Edge.Cuts; falls back to
    the union of footprint bboxes if Edge.Cuts is empty."""
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
        for fp in board.GetFootprints():
            bb = fp.GetBoundingBox(False)
            left = min(left, bb.GetLeft())
            top = min(top, bb.GetTop())
            right = max(right, bb.GetRight())
            bottom = max(bottom, bb.GetBottom())
    if not math.isfinite(left):
        raise ValueError("Board has no Edge.Cuts and no footprints — cannot compute extents")
    return (left / SCALE, top / SCALE, right / SCALE, bottom / SCALE)


# --------------------------------------------------------------------------
# Clearance lookup (matches routing._resolve_route_clearance semantics)
# --------------------------------------------------------------------------
def _resolve_clearance_mm(board: Any, net_name: Optional[str], override_mm: Optional[float]) -> float:
    """Return the clearance in mm: caller override → net's netclass → design
    default → 0.

    NOTE: `pcbnew.NETINFO_ITEM.GetNetClass()` returns a raw `SwigPyObject`
    that lacks `.GetClearance()` — the silent-AttributeError trap that
    burned this function for the entire Phases 1–4 of the topology
    project. Use `m_NetSettings.GetEffectiveNetClass(net_name)` instead
    (returns a proper NETCLASS). Same gotcha is documented in
    TOPOLOGY_TOOLS_PLAN.md's "bug log".
    """
    if override_mm is not None:
        return float(override_mm)
    clearance_iu = 0
    if net_name:
        try:
            ns = board.GetDesignSettings().m_NetSettings
            nc = ns.GetEffectiveNetClass(net_name)
            if nc is not None:
                clearance_iu = int(nc.GetClearance())
        except Exception as e:
            logger.warning(
                f"_resolve_clearance_mm: GetEffectiveNetClass('{net_name}') "
                f"failed ({type(e).__name__}: {e}); falling back to design "
                f"default."
            )
    if clearance_iu <= 0:
        try:
            ns = board.GetDesignSettings().m_NetSettings
            default_nc = ns.GetDefaultNetclass()
            if default_nc is not None:
                clearance_iu = int(default_nc.GetClearance())
        except Exception as e:
            logger.warning(
                f"_resolve_clearance_mm: default-netclass lookup failed "
                f"({type(e).__name__}: {e}); using clearance = 0."
            )
    return max(0.0, clearance_iu / SCALE)


# --------------------------------------------------------------------------
# Pad discovery + anchor (handles the F.Cu/B.Cu flip ambiguity)
# --------------------------------------------------------------------------
def _pad_is_on_layer(pad: Any, layer_id: int) -> bool:
    """True iff the pad has copper on `layer_id`. SMD pad.GetLayer() is
    unreliable on flipped footprints (see RoutingCommands notes); use
    IsOnLayer which inspects the full layerset."""
    try:
        return bool(pad.IsOnLayer(layer_id))
    except Exception:
        try:
            return layer_id in [lid for lid in pad.GetLayerSet().Seq()]
        except Exception:
            return False


def _find_pad(board: Any, ref: str, pad_num: str) -> Optional[Any]:
    for fp in board.GetFootprints():
        if fp.GetReference() != ref:
            continue
        for pad in fp.Pads():
            if pad.GetNumber() == pad_num:
                return pad
    return None


# --------------------------------------------------------------------------
# Rasterizer
# --------------------------------------------------------------------------
def _xy_mm_to_px(x_mm: float, y_mm: float, x0: float, y0: float, g: float) -> Tuple[int, int]:
    """World mm → grid (col, row). Cols index x, rows index y."""
    return (int(round((x_mm - x0) / g)), int(round((y_mm - y0) / g)))


def _stamp_disk(mask: np.ndarray, cx: int, cy: int, r: float) -> None:
    """OR a filled disk of pixel-radius `r` into `mask` at (cx, cy)."""
    if r <= 0:
        if 0 <= cy < mask.shape[0] and 0 <= cx < mask.shape[1]:
            mask[cy, cx] = True
        return
    r_ceil = int(math.ceil(r))
    y0 = max(0, cy - r_ceil)
    y1 = min(mask.shape[0], cy + r_ceil + 1)
    x0 = max(0, cx - r_ceil)
    x1 = min(mask.shape[1], cx + r_ceil + 1)
    if y1 <= y0 or x1 <= x0:
        return
    yy, xx = np.ogrid[y0:y1, x0:x1]
    disk = (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r
    mask[y0:y1, x0:x1] |= disk


def _stamp_segment(mask: np.ndarray, p0: Tuple[int, int], p1: Tuple[int, int], half_w: float) -> None:
    """OR a stadium (thick segment) of pixel half-width `half_w` into `mask`."""
    (x0p, y0p), (x1p, y1p) = p0, p1
    r_ceil = int(math.ceil(half_w))
    # Bbox of the stadium
    xmin = max(0, min(x0p, x1p) - r_ceil)
    xmax = min(mask.shape[1] - 1, max(x0p, x1p) + r_ceil)
    ymin = max(0, min(y0p, y1p) - r_ceil)
    ymax = min(mask.shape[0] - 1, max(y0p, y1p) + r_ceil)
    if xmax < xmin or ymax < ymin:
        return
    yy, xx = np.mgrid[ymin:ymax + 1, xmin:xmax + 1]
    # Distance from each pixel to segment (x0p,y0p)-(x1p,y1p).
    dx = x1p - x0p
    dy = y1p - y0p
    L2 = dx * dx + dy * dy
    if L2 == 0:
        d2 = (xx - x0p) ** 2 + (yy - y0p) ** 2
    else:
        t = ((xx - x0p) * dx + (yy - y0p) * dy) / L2
        t = np.clip(t, 0.0, 1.0)
        px = x0p + t * dx
        py = y0p + t * dy
        d2 = (xx - px) ** 2 + (yy - py) ** 2
    inside = d2 <= half_w * half_w
    mask[ymin:ymax + 1, xmin:xmax + 1] |= inside


def _stamp_rect_aa(
    mask: np.ndarray,
    cx_mm: float, cy_mm: float,
    w_mm: float, h_mm: float,
    angle_deg: float,
    x0: float, y0: float, g: float,
) -> None:
    """OR an oriented rectangle into the mask. `angle_deg` rotates CCW
    (KiCad's PAD orientation is in 0.1° units when read raw, but we take
    degrees here)."""
    cx_px, cy_px = _xy_mm_to_px(cx_mm, cy_mm, x0, y0, g)
    half_w_px = w_mm / (2 * g)
    half_h_px = h_mm / (2 * g)
    # Bounding box of the rotated rect
    cos_a = math.cos(math.radians(angle_deg))
    sin_a = math.sin(math.radians(angle_deg))
    bb_half = abs(half_w_px * cos_a) + abs(half_h_px * sin_a)
    bb_half_v = abs(half_w_px * sin_a) + abs(half_h_px * cos_a)
    r_ceil = int(math.ceil(max(bb_half, bb_half_v))) + 1
    xmin = max(0, cx_px - r_ceil)
    xmax = min(mask.shape[1] - 1, cx_px + r_ceil)
    ymin = max(0, cy_px - r_ceil)
    ymax = min(mask.shape[0] - 1, cy_px + r_ceil)
    if xmax < xmin or ymax < ymin:
        return
    yy, xx = np.mgrid[ymin:ymax + 1, xmin:xmax + 1]
    # Rotate pixel offsets into rect-local axes.
    dx = xx - cx_px
    dy = yy - cy_px
    lx = dx * cos_a + dy * sin_a
    ly = -dx * sin_a + dy * cos_a
    inside = (np.abs(lx) <= half_w_px) & (np.abs(ly) <= half_h_px)
    mask[ymin:ymax + 1, xmin:xmax + 1] |= inside


# --------------------------------------------------------------------------
# Obstacle rasterization
# --------------------------------------------------------------------------
def _build_obstacle_mask(
    board: Any,
    layer_id: int,
    own_net: Optional[str],
    x0: float, y0: float,
    nx: int, ny: int,
    g: float,
) -> np.ndarray:
    """Rasterize foreign copper on `layer_id` into a (ny, nx) bool mask.

    Obstacles are stamped at their native dimensions; the dilation by
    `W/2 + C` happens later via the distance transform. Vias affect every
    copper layer."""
    mask = np.zeros((ny, nx), dtype=bool)

    # Tracks (segments) and vias.
    for t in board.Tracks():
        net = t.GetNetname() or ""
        if own_net is not None and net == own_net:
            continue
        if t.Type() == pcbnew.PCB_VIA_T:
            try:
                via_w = t.GetWidth(pcbnew.F_Cu)
            except TypeError:
                via_w = t.GetWidth()
            pos = t.GetPosition()
            cx, cy = _xy_mm_to_px(pos.x / SCALE, pos.y / SCALE, x0, y0, g)
            _stamp_disk(mask, cx, cy, (via_w / SCALE) / (2 * g))
        else:
            if t.GetLayer() != layer_id:
                continue
            s, e = t.GetStart(), t.GetEnd()
            p0 = _xy_mm_to_px(s.x / SCALE, s.y / SCALE, x0, y0, g)
            p1 = _xy_mm_to_px(e.x / SCALE, e.y / SCALE, x0, y0, g)
            half_w_px = (t.GetWidth() / SCALE) / (2 * g)
            _stamp_segment(mask, p0, p1, half_w_px)

    # Pads.
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if not _pad_is_on_layer(pad, layer_id):
                continue
            net = pad.GetNetname() or ""
            if own_net is not None and net == own_net:
                continue
            pos = pad.GetPosition()
            sz = pad.GetSize()
            try:
                orient_deg = pad.GetOrientation().AsDegrees()
            except AttributeError:
                orient_deg = float(pad.GetOrientation()) / 10.0  # legacy: 0.1°
            # For circular/oval pads, an oriented rect bbox is conservative
            # (slightly over-blocks the corner regions). v1 accepts that;
            # v3 (polygon-exact) addresses it.
            _stamp_rect_aa(
                mask,
                pos.x / SCALE, pos.y / SCALE,
                sz.x / SCALE, sz.y / SCALE,
                orient_deg,
                x0, y0, g,
            )

    # Zones (pours). A pour on the trace's own net is not an obstacle —
    # the fill hands off connectivity at refill time. A pour on a foreign
    # net IS an obstacle. The filled-polygons API already incorporates
    # clearance against foreign-net items, so we rasterize the actual
    # filled extent (not the zone outline).
    _stamp_zones_on_layer(mask, board, layer_id, own_net, x0, y0, g)

    return mask


def _stamp_zones_on_layer(
    mask: np.ndarray,
    board: Any,
    layer_id: int,
    own_net: Optional[str],
    x0: float, y0: float, g: float,
) -> None:
    """Rasterize foreign-net zone fills on `layer_id` into `mask`.

    KiCad's `SHAPE_POLY_SET` can represent holes either as separate
    outlines or as self-touching bridges in the main outline. We
    rasterize per Outline() (using PIL's polygon fill, which honours
    the bridge representation correctly) and subtract per Hole() —
    covering both representations without inspecting which one this
    particular zone happens to use.
    """
    try:
        n_zones = board.GetAreaCount()
    except Exception:
        return
    if n_zones <= 0:
        return
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        logger.warning(
            "_stamp_zones_on_layer: PIL not available — zones will be "
            "ignored (this used to be the silent default; now it warns)."
        )
        return

    ny, nx = mask.shape

    def _verts_for(chain: Any) -> List[Tuple[int, int]]:
        n = chain.PointCount()
        out: List[Tuple[int, int]] = []
        for i in range(n):
            v = chain.CPoint(i)
            out.append(_xy_mm_to_px(v.x / SCALE, v.y / SCALE, x0, y0, g))
        return out

    accumulated: Optional[Image.Image] = None
    for i in range(n_zones):
        try:
            zone = board.GetArea(i)
        except Exception:
            continue
        net = zone.GetNetname() or ""
        if own_net is not None and net == own_net:
            continue
        # Zones can span multiple layers (e.g. F+B for thermal pads);
        # rasterize only this layer's fill geometry.
        try:
            if not zone.IsOnLayer(layer_id):
                continue
            poly_set = zone.GetFilledPolysList(layer_id)
        except Exception:
            continue
        if poly_set is None or poly_set.IsEmpty():
            continue

        if accumulated is None:
            accumulated = Image.new("L", (nx, ny), 0)
            draw = ImageDraw.Draw(accumulated)
        # else draw was bound on the first allocation

        for outline_idx in range(poly_set.OutlineCount()):
            try:
                outer = poly_set.Outline(outline_idx)
            except Exception:
                continue
            verts = _verts_for(outer)
            if len(verts) >= 3:
                draw.polygon(verts, fill=1)
            try:
                n_holes = poly_set.HoleCount(outline_idx)
            except Exception:
                n_holes = 0
            for hole_idx in range(n_holes):
                try:
                    hole = poly_set.Hole(outline_idx, hole_idx)
                except Exception:
                    continue
                hole_verts = _verts_for(hole)
                if len(hole_verts) >= 3:
                    draw.polygon(hole_verts, fill=0)

    if accumulated is not None:
        mask |= np.array(accumulated, dtype=bool)


# --------------------------------------------------------------------------
# Shared grid + EDT (the Phase 1 / Phase 2 primitive)
# --------------------------------------------------------------------------
def _compute_obstacle_state(
    board: Any,
    layer_id: int,
    net: Optional[str],
    g: float,
) -> Dict[str, Any]:
    """Build the rasterized obstacle mask + Euclidean distance transform
    at grid step `g`. Width-independent — derived free spaces at any
    requested (W, C) come from thresholding `dist_px` later.

    Returned dict: ``{x0, y0, g, nx, ny, mask, dist_px}``."""
    xmin, ymin, xmax, ymax = _board_bbox_mm(board)
    pad_px = 2
    x0 = xmin - pad_px * g
    y0 = ymin - pad_px * g
    nx = int(math.ceil((xmax - xmin) / g)) + 2 * pad_px
    ny = int(math.ceil((ymax - ymin) / g)) + 2 * pad_px
    mask = _build_obstacle_mask(board, layer_id, net, x0, y0, nx, ny, g)
    dist_px = distance_transform_edt(~mask)
    return {
        "x0": x0, "y0": y0, "g": g, "nx": nx, "ny": ny,
        "mask": mask, "dist_px": dist_px,
    }


def _free_space(state: Dict[str, Any], width_mm: float, clearance_mm: float) -> np.ndarray:
    """Threshold the EDT to get the free-space mask at this (W, C).

    Obstacles themselves are explicitly excluded — `dist_px == 0` on
    obstacle pixels, which would otherwise satisfy `>= 0` and let the
    W=0 / C=0 case wrongly mark every pixel as free."""
    radius_px = (width_mm / 2 + clearance_mm) / state["g"]
    return (state["dist_px"] >= radius_px) & (~state["mask"])


def _pad_anchor(
    state: Dict[str, Any], free: np.ndarray, pad: Any,
    extra_halo_px: int = 0,
) -> Tuple[Optional[Tuple[int, int]], Tuple[float, float]]:
    """Snap a pad to its nearest free-space pixel.

    Search window half-side = pad's max dimension in pixels (so the
    window always extends ~one pad-width past the pad's edge) plus
    `extra_halo_px`. Foreign-net pads' own copper is an obstacle, so the
    anchor must sit at least `W/2 + C` outside the pad's edge — pass
    that as `extra_halo_px` (in pixels) when binary-searching wide widths.

    Returns ``(anchor_or_None, (cx_mm, cy_mm))``."""
    pos = pad.GetPosition()
    cx_mm = pos.x / SCALE
    cy_mm = pos.y / SCALE
    cx, cy = _xy_mm_to_px(cx_mm, cy_mm, state["x0"], state["y0"], state["g"])
    sz = pad.GetSize()
    pad_extent_px = max(1, int(math.ceil(max(sz.x, sz.y) / SCALE / state["g"])))
    search_px = pad_extent_px + max(0, int(extra_halo_px))
    return _nearest_free_pixel(free, cy, cx, search_px), (cx_mm, cy_mm)


# --------------------------------------------------------------------------
# Public: analyze_routable_regions
# --------------------------------------------------------------------------
def analyze_routable_regions(
    board: Any,
    layer: str,
    width_mm: float,
    clearance_mm: Optional[float] = None,
    net: Optional[str] = None,
    resolution_mm: float = 0.05,
    convergence_check: bool = False,
) -> Dict[str, Any]:
    """List the connected components of free space on `layer` at the given
    trace width. Each component is the region a moving trace of width
    `width_mm` could fill without violating clearance against foreign copper.

    If `net` is None, ALL copper on the layer counts as obstacle (i.e. "the
    layer's free corridors regardless of any specific net"). If `net` is
    given, that net's own copper is excluded from the obstacle set, so its
    pads anchor naturally into the regions they border.

    With `convergence_check=True`, the analysis is run again at half the
    grid step and the two component counts are reported so the caller can
    judge quantization sensitivity.
    """
    layer_id = board.GetLayerID(layer)
    if layer_id < 0:
        return {"success": False, "message": f"Unknown layer: {layer}"}

    clearance = _resolve_clearance_mm(board, net, clearance_mm)

    def _run(g: float) -> Dict[str, Any]:
        xmin, ymin, xmax, ymax = _board_bbox_mm(board)
        # Pad the grid by 2 px on each side so dilation near the edge is clean.
        pad_px = 2
        x0 = xmin - pad_px * g
        y0 = ymin - pad_px * g
        nx = int(math.ceil((xmax - xmin) / g)) + 2 * pad_px
        ny = int(math.ceil((ymax - ymin) / g)) + 2 * pad_px

        mask = _build_obstacle_mask(board, layer_id, net, x0, y0, nx, ny, g)
        # Distance to nearest obstacle, in pixels.
        dist_px = distance_transform_edt(~mask)
        radius_px = (width_mm / 2 + clearance) / g
        # Obstacle pixels have dist_px == 0; exclude them so the W=0,
        # C=0 degenerate case correctly leaves obstacles outside free.
        free = (dist_px >= radius_px) & (~mask)
        labels, n = scipy_label(free)
        return {
            "grid": {"x0": x0, "y0": y0, "nx": nx, "ny": ny, "g_mm": g},
            "mask": mask,
            "dist_px": dist_px,
            "free": free,
            "labels": labels,
            "n_components": int(n),
        }

    base = _run(resolution_mm)
    grid = base["grid"]
    labels = base["labels"]
    n = base["n_components"]
    g = grid["g_mm"]

    # Per-component summary: area, bbox, bordering pads.
    cell_area_mm2 = g * g
    region_summaries: List[Dict[str, Any]] = []
    for cid in range(1, n + 1):
        ys, xs = np.where(labels == cid)
        if ys.size == 0:
            continue
        area_mm2 = ys.size * cell_area_mm2
        x_mm_min = grid["x0"] + xs.min() * g
        x_mm_max = grid["x0"] + xs.max() * g
        y_mm_min = grid["y0"] + ys.min() * g
        y_mm_max = grid["y0"] + ys.max() * g
        region_summaries.append({
            "id": cid,
            "areaMm2": round(area_mm2, 4),
            "pixelCount": int(ys.size),
            "bbox": {
                "xMin": round(x_mm_min, 4),
                "yMin": round(y_mm_min, 4),
                "xMax": round(x_mm_max, 4),
                "yMax": round(y_mm_max, 4),
                "unit": "mm",
            },
            "pads": [],
        })

    # Attribute pads to regions. Each pad's anchor is the nearest free-
    # space pixel within the pad's own footprint extent (centers themselves
    # are usually IN copper — own-net pads when net=X, OR any pad when
    # net=None — so we search a small window around the center). Pads
    # whose footprint has no free pixel get reason="no_escape_at_width"
    # — that's the QFN-internal-escape diagnostic the plan calls out.
    pads_by_region: Dict[int, List[Dict[str, Any]]] = {}
    unrouted: List[Dict[str, Any]] = []
    free = base["free"]
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        for pad in fp.Pads():
            if not _pad_is_on_layer(pad, layer_id):
                continue
            pad_net = pad.GetNetname() or ""
            if net is not None and pad_net != net:
                # Foreign-net pads on a per-net analysis are not terminals.
                continue
            pos = pad.GetPosition()
            cx_mm = pos.x / SCALE
            cy_mm = pos.y / SCALE
            cx, cy = _xy_mm_to_px(cx_mm, cy_mm, grid["x0"], grid["y0"], g)
            if not (0 <= cy < grid["ny"] and 0 <= cx < grid["nx"]):
                continue
            sz = pad.GetSize()
            search_px = max(1, int(math.ceil(max(sz.x, sz.y) / SCALE / g)))
            anchor = _nearest_free_pixel(free, cy, cx, search_px)
            pad_info = {
                "ref": ref,
                "pad": pad.GetNumber(),
                "net": pad_net,
                "x": round(cx_mm, 4),
                "y": round(cy_mm, 4),
            }
            if anchor is None:
                pad_info["reason"] = "no_escape_at_width"
                unrouted.append(pad_info)
            else:
                cid = int(labels[anchor])
                if cid > 0:
                    pads_by_region.setdefault(cid, []).append(pad_info)
                else:
                    pad_info["reason"] = "no_escape_at_width"
                    unrouted.append(pad_info)

    for region in region_summaries:
        region["pads"] = pads_by_region.get(region["id"], [])

    region_summaries.sort(key=lambda r: -r["areaMm2"])

    out: Dict[str, Any] = {
        "success": True,
        "layer": layer,
        "widthMm": width_mm,
        "clearanceMm": round(clearance, 4),
        "net": net,
        "grid": {
            "resolutionMm": g,
            "nx": grid["nx"],
            "ny": grid["ny"],
            "origin": {"x": round(grid["x0"], 4), "y": round(grid["y0"], 4), "unit": "mm"},
        },
        "componentCount": n,
        "regions": region_summaries,
        "padsWithoutEscape": unrouted,
        "message": (
            f"Layer {layer} partitions into {n} free-space component(s) "
            f"at width {width_mm} mm + clearance {round(clearance, 3)} mm "
            f"(grid {g} mm). {len(unrouted)} pad(s) have no escape lane."
        ),
    }

    if convergence_check:
        half = _run(g / 2)
        out["convergence"] = {
            "fineGridMm": half["grid"]["g_mm"],
            "fineComponentCount": half["n_components"],
            "agrees": half["n_components"] == n,
            "note": (
                "agrees=true means component count is stable across g and "
                "g/2 — the analysis is grid-independent at this resolution. "
                "agrees=false means a corridor exists near the quantization "
                "limit; rerun at a finer resolutionMm."
            ),
        }

    return out


# --------------------------------------------------------------------------
# Public: check_pad_routability
# --------------------------------------------------------------------------
def _nearest_free_pixel(free: np.ndarray, cy: int, cx: int, search_px: int) -> Optional[Tuple[int, int]]:
    """Find the nearest True pixel to (cy, cx) within a square of half-side
    `search_px`. Returns (row, col) or None."""
    if 0 <= cy < free.shape[0] and 0 <= cx < free.shape[1] and free[cy, cx]:
        return (cy, cx)
    y0 = max(0, cy - search_px)
    y1 = min(free.shape[0], cy + search_px + 1)
    x0 = max(0, cx - search_px)
    x1 = min(free.shape[1], cx + search_px + 1)
    window = free[y0:y1, x0:x1]
    if not window.any():
        return None
    ys, xs = np.where(window)
    dists = (ys + y0 - cy) ** 2 + (xs + x0 - cx) ** 2
    i = int(np.argmin(dists))
    return (int(ys[i] + y0), int(xs[i] + x0))


def _bfs_path(free: np.ndarray, src: Tuple[int, int], dst: Tuple[int, int]) -> Optional[List[Tuple[int, int]]]:
    """8-connected BFS on a bool grid. Returns list of (row, col) from src
    to dst inclusive, or None if no path."""
    ny, nx = free.shape
    if not free[src] or not free[dst]:
        return None
    # Sentinel -1 = unvisited; encode parent as flat-index.
    parent = np.full(ny * nx, -1, dtype=np.int64)
    src_flat = src[0] * nx + src[1]
    dst_flat = dst[0] * nx + dst[1]
    parent[src_flat] = src_flat  # self → marks start
    from collections import deque
    q = deque([src_flat])
    while q:
        flat = q.popleft()
        if flat == dst_flat:
            break
        r, c = divmod(flat, nx)
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue
                nr, nc = r + dr, c + dc
                if nr < 0 or nr >= ny or nc < 0 or nc >= nx:
                    continue
                if not free[nr, nc]:
                    continue
                nflat = nr * nx + nc
                if parent[nflat] != -1:
                    continue
                parent[nflat] = flat
                q.append(nflat)
    if parent[dst_flat] == -1:
        return None
    # Reconstruct.
    path: List[Tuple[int, int]] = []
    cur = dst_flat
    while cur != src_flat:
        path.append(divmod(cur, nx))
        cur = int(parent[cur])
    path.append(src)
    path.reverse()
    return path


def check_pad_routability(
    board: Any,
    from_ref: str,
    from_pad: str,
    to_ref: str,
    to_pad: str,
    layer: str,
    width_mm: float,
    clearance_mm: Optional[float] = None,
    resolution_mm: float = 0.05,
    convergence_check: bool = False,
    mode: str = "raster",
) -> Dict[str, Any]:
    """Are these two pads in the same free-space component on `layer` at
    `width_mm`? If yes, what's the bottleneck width along a shortest path
    between them?

    Returns `{reachable, bottleneckWidthMm, pathXy[], reason}`. `pathXy` is
    a downsampled list of (x, y) mm coordinates along the BFS path through
    the free space — useful for visualisation, NOT a routing suggestion.

    `mode` (default `"raster"`): pick the engine.
    - `"raster"` — Phase-1 algorithm: rasterize → EDT → label. Fast,
      grid-quantization noise visible via `convergenceCheck=True`. Reports
      `bottleneckWidthMm` + `pathXy` because BFS + distance transform
      give them naturally.
    - `"exact"` — Phase-4 polygon-exact engine: `shapely.unary_union` +
      `buffer` + `Polygon.difference`. No grid quantization, slightly
      slower on dense boards. Use this for the final "really, really
      sure?" go/no-go after raster mode says yes-but-near-the-edge.
      Returns the reachability flag only — `bottleneckWidthMm` and
      `pathXy` are omitted (not naturally computable from the polygon
      set). For bottleneck, stay in raster mode and crank `convergenceCheck`.
    """
    if mode == "exact":
        return _check_pad_routability_exact(
            board, from_ref, from_pad, to_ref, to_pad,
            layer, width_mm, clearance_mm,
        )
    if mode != "raster":
        return {
            "success": False,
            "message": f"Unknown mode '{mode}' — use 'raster' or 'exact'.",
        }

    layer_id = board.GetLayerID(layer)
    if layer_id < 0:
        return {"success": False, "message": f"Unknown layer: {layer}"}

    pa = _find_pad(board, from_ref, from_pad)
    pb = _find_pad(board, to_ref, to_pad)
    if pa is None:
        return {"success": False, "message": f"Pad not found: {from_ref} pad {from_pad}"}
    if pb is None:
        return {"success": False, "message": f"Pad not found: {to_ref} pad {to_pad}"}

    net_a = pa.GetNetname() or ""
    net_b = pb.GetNetname() or ""
    if net_a and net_b and net_a != net_b:
        return {
            "success": True,
            "reachable": False,
            "reason": "pads_on_different_nets",
            "message": f"{from_ref}.{from_pad} is on '{net_a}' but {to_ref}.{to_pad} is on '{net_b}'.",
            "fromNet": net_a,
            "toNet": net_b,
        }
    own_net = net_a or net_b or None

    clearance = _resolve_clearance_mm(board, own_net, clearance_mm)

    def _run(g: float) -> Dict[str, Any]:
        xmin, ymin, xmax, ymax = _board_bbox_mm(board)
        pad_px = 2
        x0 = xmin - pad_px * g
        y0 = ymin - pad_px * g
        nx = int(math.ceil((xmax - xmin) / g)) + 2 * pad_px
        ny = int(math.ceil((ymax - ymin) / g)) + 2 * pad_px
        mask = _build_obstacle_mask(board, layer_id, own_net, x0, y0, nx, ny, g)
        dist_px = distance_transform_edt(~mask)
        radius_px = (width_mm / 2 + clearance) / g
        free = (dist_px >= radius_px) & (~mask)
        return {
            "g": g, "x0": x0, "y0": y0, "nx": nx, "ny": ny,
            "mask": mask, "dist_px": dist_px, "free": free,
        }

    def _query(state: Dict[str, Any]) -> Dict[str, Any]:
        g = state["g"]
        free = state["free"]
        dist_px = state["dist_px"]
        # Anchor pads at their centers; if the center isn't in free space
        # (e.g. on the foreign-net analysis, or a tight escape), search
        # a small neighbourhood inside the pad's footprint.

        def _anchor(pad: Any) -> Tuple[Optional[Tuple[int, int]], Tuple[float, float]]:
            pos = pad.GetPosition()
            cx_mm = pos.x / SCALE
            cy_mm = pos.y / SCALE
            cx, cy = _xy_mm_to_px(cx_mm, cy_mm, state["x0"], state["y0"], g)
            sz = pad.GetSize()
            search_px = max(1, int(math.ceil(max(sz.x, sz.y) / SCALE / g)))
            anchor = _nearest_free_pixel(free, cy, cx, search_px)
            return anchor, (cx_mm, cy_mm)

        anchor_a, xy_a = _anchor(pa)
        anchor_b, xy_b = _anchor(pb)
        if anchor_a is None:
            return {
                "reachable": False,
                "reason": "from_pad_no_escape",
                "message": (
                    f"{from_ref}.{from_pad} has no free-space pixel within "
                    f"its pad footprint at width {width_mm} mm + clearance "
                    f"{round(clearance, 3)} mm. Pad escape lane is too tight."
                ),
                "fromXy": {"x": xy_a[0], "y": xy_a[1], "unit": "mm"},
                "toXy": {"x": xy_b[0], "y": xy_b[1], "unit": "mm"},
            }
        if anchor_b is None:
            return {
                "reachable": False,
                "reason": "to_pad_no_escape",
                "message": (
                    f"{to_ref}.{to_pad} has no free-space pixel within "
                    f"its pad footprint at width {width_mm} mm + clearance "
                    f"{round(clearance, 3)} mm."
                ),
                "fromXy": {"x": xy_a[0], "y": xy_a[1], "unit": "mm"},
                "toXy": {"x": xy_b[0], "y": xy_b[1], "unit": "mm"},
            }

        path = _bfs_path(free, anchor_a, anchor_b)
        if path is None:
            # Diagnose: same component? — they're not, by construction.
            labels, _ = scipy_label(free)
            cid_a = int(labels[anchor_a])
            cid_b = int(labels[anchor_b])
            return {
                "reachable": False,
                "reason": "different_components",
                "message": (
                    f"Pads are in different free-space components on "
                    f"{layer} at width {width_mm} mm. Trace cannot fit "
                    f"through the corridor between them at this width."
                ),
                "fromComponent": cid_a,
                "toComponent": cid_b,
                "fromXy": {"x": xy_a[0], "y": xy_a[1], "unit": "mm"},
                "toXy": {"x": xy_b[0], "y": xy_b[1], "unit": "mm"},
            }

        # Bottleneck = min over path of 2*(dist_to_obstacle - clearance).
        path_dist_px = np.array([dist_px[r, c] for r, c in path], dtype=float)
        min_dist_mm = float(path_dist_px.min()) * g
        bottleneck_mm = max(0.0, 2.0 * (min_dist_mm - clearance))
        # Downsample for caller convenience (one point per ~0.5mm).
        step = max(1, int(round(0.5 / g)))
        path_xy = [
            {
                "x": round(state["x0"] + c * g, 4),
                "y": round(state["y0"] + r * g, 4),
                "unit": "mm",
            }
            for r, c in path[::step]
        ]
        # Always include the actual endpoints.
        if path_xy[-1] != {
            "x": round(state["x0"] + path[-1][1] * g, 4),
            "y": round(state["y0"] + path[-1][0] * g, 4),
            "unit": "mm",
        }:
            r, c = path[-1]
            path_xy.append({
                "x": round(state["x0"] + c * g, 4),
                "y": round(state["y0"] + r * g, 4),
                "unit": "mm",
            })

        return {
            "reachable": True,
            "bottleneckWidthMm": round(bottleneck_mm, 4),
            "pathXy": path_xy,
            "fromXy": {"x": xy_a[0], "y": xy_a[1], "unit": "mm"},
            "toXy": {"x": xy_b[0], "y": xy_b[1], "unit": "mm"},
            "message": (
                f"{from_ref}.{from_pad} → {to_ref}.{to_pad} on {layer} at "
                f"width {width_mm} mm: reachable. Bottleneck width = "
                f"{round(bottleneck_mm, 3)} mm along the path."
            ),
        }

    state_g = _run(resolution_mm)
    out = _query(state_g)
    out["success"] = True
    out["layer"] = layer
    out["widthMm"] = width_mm
    out["clearanceMm"] = round(clearance, 4)
    out["net"] = own_net
    out["grid"] = {"resolutionMm": resolution_mm, "nx": state_g["nx"], "ny": state_g["ny"]}

    if convergence_check:
        state_half = _run(resolution_mm / 2)
        half_out = _query(state_half)
        out["convergence"] = {
            "fineGridMm": resolution_mm / 2,
            "fineReachable": bool(half_out.get("reachable")),
            "agrees": bool(half_out.get("reachable")) == bool(out.get("reachable")),
            "note": (
                "agrees=true means reachability is stable across g and "
                "g/2. agrees=false means the answer flipped at higher "
                "resolution — the case is near the quantization limit."
            ),
        }
        if out.get("reachable") and half_out.get("reachable"):
            fine = half_out.get("bottleneckWidthMm")
            coarse = out.get("bottleneckWidthMm")
            if fine is not None and coarse is not None:
                out["convergence"]["fineBottleneckWidthMm"] = fine
                out["convergence"]["bottleneckDeltaMm"] = round(abs(fine - coarse), 4)

    return out


# --------------------------------------------------------------------------
# Public: max_width_between
# --------------------------------------------------------------------------
def _pads_connected_at(
    state: Dict[str, Any],
    pa: Any,
    pb: Any,
    width_mm: float,
    clearance_mm: float,
) -> Optional[bool]:
    """True iff the two pads anchor into the same connected component of
    free space at this (W, C). None if either pad has no anchor (i.e. the
    width is too wide to even leave its pad).

    The anchor search window is widened by the trace's own
    `(W/2 + C)` halo so the binary search in `max_width_between` can
    still find the trace's escape pixel at large widths."""
    free = _free_space(state, width_mm, clearance_mm)
    halo_px = int(math.ceil((width_mm / 2 + clearance_mm) / state["g"]))
    anc_a, _ = _pad_anchor(state, free, pa, extra_halo_px=halo_px)
    anc_b, _ = _pad_anchor(state, free, pb, extra_halo_px=halo_px)
    if anc_a is None or anc_b is None:
        return None
    labels, _ = scipy_label(free)
    la = int(labels[anc_a])
    lb = int(labels[anc_b])
    return la > 0 and la == lb


def max_width_between(
    board: Any,
    from_ref: str,
    from_pad: str,
    to_ref: str,
    to_pad: str,
    layer: str,
    clearance_mm: Optional[float] = None,
    resolution_mm: float = 0.05,
    width_tolerance_mm: Optional[float] = None,
    upper_bound_mm: Optional[float] = None,
) -> Dict[str, Any]:
    """Binary-search the largest trace width such that the two pads still
    fall in the same free-space component on `layer`. Monotone: as width
    grows, free space shrinks and the pads eventually disconnect — the
    transition width is what we report.

    `width_tolerance_mm` defaults to twice the grid step (sub-grid
    answers are noise). `upper_bound_mm` defaults to twice the maximum
    distance-to-obstacle in the rasterised layer (the absolute ceiling
    given current copper); pass it to skip a precomputation step.
    """
    layer_id = board.GetLayerID(layer)
    if layer_id < 0:
        return {"success": False, "message": f"Unknown layer: {layer}"}

    pa = _find_pad(board, from_ref, from_pad)
    pb = _find_pad(board, to_ref, to_pad)
    if pa is None:
        return {"success": False, "message": f"Pad not found: {from_ref} pad {from_pad}"}
    if pb is None:
        return {"success": False, "message": f"Pad not found: {to_ref} pad {to_pad}"}

    net_a = pa.GetNetname() or ""
    net_b = pb.GetNetname() or ""
    if net_a and net_b and net_a != net_b:
        return {
            "success": True,
            "reachable": False,
            "reason": "pads_on_different_nets",
            "fromNet": net_a, "toNet": net_b,
        }
    own_net = net_a or net_b or None
    clearance = _resolve_clearance_mm(board, own_net, clearance_mm)

    state = _compute_obstacle_state(board, layer_id, own_net, resolution_mm)

    # Lower bound: 0 mm — the pad pair is in the same component at the
    # bare unitary free space iff there's any obstacle-free path between
    # them at all.
    low = 0.0
    if _pads_connected_at(state, pa, pb, low, clearance) is not True:
        return {
            "success": True,
            "reachable": False,
            "reason": "different_components",
            "message": (
                f"Pads aren't connected on {layer} even at width 0 mm + "
                f"clearance {round(clearance, 3)} mm — the obstacle field "
                "completely separates them on this layer."
            ),
            "clearanceMm": round(clearance, 4),
            "net": own_net,
        }

    # Upper bound: twice the global max distance-to-obstacle, minus
    # twice the clearance — beyond that, the widest free pixel on the
    # layer can't host the trace's center, so nothing is connected.
    if upper_bound_mm is None:
        upper_bound_mm = max(
            0.0, 2.0 * float(state["dist_px"].max()) * resolution_mm - 2.0 * clearance,
        )
    high = float(upper_bound_mm)
    # Edge case: at the upper bound the pads might still be connected
    # (unlikely but possible for a generous bound). Widen until they're not.
    safety = 0
    while _pads_connected_at(state, pa, pb, high, clearance) is True and safety < 8:
        high *= 2.0
        safety += 1

    tol = float(width_tolerance_mm) if width_tolerance_mm is not None else 2.0 * resolution_mm

    iterations = 0
    while high - low > tol and iterations < 32:
        mid = (low + high) / 2.0
        connected = _pads_connected_at(state, pa, pb, mid, clearance)
        if connected is True:
            low = mid
        else:
            high = mid
        iterations += 1

    return {
        "success": True,
        "reachable": True,
        "maxWidthMm": round(low, 4),
        "searchToleranceMm": round(tol, 4),
        "iterations": iterations,
        "upperBoundMm": round(upper_bound_mm, 4),
        "clearanceMm": round(clearance, 4),
        "net": own_net,
        "layer": layer,
        "grid": {"resolutionMm": resolution_mm, "nx": state["nx"], "ny": state["ny"]},
        "message": (
            f"{from_ref}.{from_pad} → {to_ref}.{to_pad} on {layer}: the "
            f"widest trace that still leaves them in the same free-space "
            f"component is {round(low, 3)} mm "
            f"(±{round(tol, 3)} mm, clearance {round(clearance, 3)} mm, "
            f"{iterations} binary-search steps)."
        ),
    }


# --------------------------------------------------------------------------
# Public: max_parallel_traces
# --------------------------------------------------------------------------
def max_parallel_traces(
    board: Any,
    from_ref: str,
    from_pad: str,
    to_ref: str,
    to_pad: str,
    layer: str,
    width_mm: float,
    clearance_mm: Optional[float] = None,
    resolution_mm: float = 0.05,
) -> Dict[str, Any]:
    """How many parallel traces of `width_mm` (each with its own clearance
    margin) can run side-by-side through the widest available corridor
    between these two pads? The answer is

        N = floor(max_corridor_width / (width + 2 * clearance))

    where `max_corridor_width` is the maximum bottleneck over all paths
    (computed via `max_width_between`'s binary search), NOT the
    bottleneck along the shortest path. Picking max-bottleneck matches
    the engineering question — "where would I route the bus to get the
    widest gap to bus through?" — instead of "how narrow does the
    straightest path get?".
    """
    mw = max_width_between(
        board,
        from_ref=from_ref, from_pad=from_pad,
        to_ref=to_ref, to_pad=to_pad,
        layer=layer, clearance_mm=clearance_mm,
        resolution_mm=resolution_mm,
    )
    if not mw.get("success") or not mw.get("reachable"):
        out = dict(mw)
        out.setdefault("maxParallelTraces", 0)
        return out

    corridor = float(mw["maxWidthMm"])
    clearance = float(mw["clearanceMm"])
    pitch = width_mm + 2.0 * clearance
    n = int(corridor // pitch) if pitch > 0 else 0
    return {
        "success": True,
        "reachable": True,
        "maxParallelTraces": n,
        "pitchMm": round(pitch, 4),
        "maxCorridorWidthMm": mw["maxWidthMm"],
        "widthMm": width_mm,
        "clearanceMm": mw["clearanceMm"],
        "layer": layer,
        "net": mw.get("net"),
        "searchToleranceMm": mw.get("searchToleranceMm"),
        "message": (
            f"{from_ref}.{from_pad} → {to_ref}.{to_pad} on {layer}: "
            f"{n} parallel trace(s) of {width_mm} mm "
            f"(pitch {round(pitch, 3)} mm) fit through the widest "
            f"corridor between them ({round(corridor, 3)} mm). "
            f"Source corridor metric: max_width_between."
        ),
    }


# --------------------------------------------------------------------------
# Public: routability_heatmap
# --------------------------------------------------------------------------
def _geodesic_distance(free: np.ndarray, src: Tuple[int, int]) -> np.ndarray:
    """8-connected BFS distance from `src` through `free`. Unreachable
    pixels stay at +inf. Distance is in pixel units (diagonal = sqrt(2))."""
    ny, nx = free.shape
    INF = np.inf
    dist = np.full((ny, nx), INF, dtype=np.float64)
    if not free[src]:
        return dist
    dist[src] = 0.0
    # Use Dijkstra with a simple bucket-style heap (heapq).
    import heapq
    h: List[Tuple[float, int]] = [(0.0, src[0] * nx + src[1])]
    sqrt2 = math.sqrt(2.0)
    while h:
        d, flat = heapq.heappop(h)
        r, c = divmod(flat, nx)
        if d > dist[r, c]:
            continue
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue
                nr, nc = r + dr, c + dc
                if nr < 0 or nr >= ny or nc < 0 or nc >= nx:
                    continue
                if not free[nr, nc]:
                    continue
                step = sqrt2 if (dr and dc) else 1.0
                nd = d + step
                if nd < dist[nr, nc]:
                    dist[nr, nc] = nd
                    heapq.heappush(h, (nd, nr * nx + nc))
    return dist


def routability_heatmap(
    board: Any,
    from_ref: str,
    from_pad: str,
    layer: str,
    width_mm: float,
    clearance_mm: Optional[float] = None,
    resolution_mm: float = 0.05,
    output_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Geodesic distance from a source pad through the free space on
    `layer` at `width_mm`, rendered as a PNG. Bright = far; black =
    unreachable (different component or obstacle). Useful to see *where*
    a trace can reach from this pad and *how* the shape of the reachable
    set is constrained by the placement.

    Returns the rendered PNG's path. Falls back to a numeric summary if
    matplotlib isn't importable.
    """
    layer_id = board.GetLayerID(layer)
    if layer_id < 0:
        return {"success": False, "message": f"Unknown layer: {layer}"}
    pa = _find_pad(board, from_ref, from_pad)
    if pa is None:
        return {"success": False, "message": f"Pad not found: {from_ref} pad {from_pad}"}

    own_net = pa.GetNetname() or None
    clearance = _resolve_clearance_mm(board, own_net, clearance_mm)
    state = _compute_obstacle_state(board, layer_id, own_net, resolution_mm)
    free = _free_space(state, width_mm, clearance)
    anchor, (cx_mm, cy_mm) = _pad_anchor(state, free, pa)
    if anchor is None:
        return {
            "success": True,
            "reachable": False,
            "reason": "from_pad_no_escape",
            "message": (
                f"{from_ref}.{from_pad} has no free-space pixel within its "
                f"pad footprint at width {width_mm} mm + clearance "
                f"{round(clearance, 3)} mm — no heatmap to render."
            ),
            "fromXy": {"x": cx_mm, "y": cy_mm, "unit": "mm"},
        }

    dist = _geodesic_distance(free, anchor)
    reachable_mask = np.isfinite(dist)
    reachable_pixels = int(reachable_mask.sum())
    reachable_area_mm2 = round(reachable_pixels * resolution_mm * resolution_mm, 4)
    max_reach_mm = (
        round(float(dist[reachable_mask].max()) * resolution_mm, 4)
        if reachable_pixels > 0 else 0.0
    )

    # Render PNG.
    out_path: Optional[str] = None
    try:
        # Point MPLCONFIGDIR at the writable claude tmp dir before
        # matplotlib imports — silences the "config not writable"
        # warning when the MCP server runs without HOME write access.
        os.environ.setdefault("MPLCONFIGDIR", "/tmp/claude-1000/matplotlib")
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        if output_path is None:
            out_dir = "/tmp/claude-1000"
            try:
                os.makedirs(out_dir, exist_ok=True)
            except Exception:
                out_dir = "/tmp"
            output_path = os.path.join(
                out_dir,
                f"routability_{from_ref}_{from_pad}_{layer.replace('.', '_')}.png",
            )

        # Colour the heatmap: scale finite distances to mm; obstacles &
        # unreachable get a separate colour.
        display = np.where(reachable_mask, dist * resolution_mm, np.nan)
        fig, ax = plt.subplots(figsize=(10, 10 * state["ny"] / max(1, state["nx"])))
        extent = (
            state["x0"],
            state["x0"] + state["nx"] * resolution_mm,
            state["y0"] + state["ny"] * resolution_mm,
            state["y0"],
        )
        im = ax.imshow(
            display, extent=extent, origin="upper",
            cmap="viridis", interpolation="nearest",
        )
        # Overlay obstacles in black for orientation.
        obstacle_overlay = np.where(state["mask"], 1.0, np.nan)
        ax.imshow(
            obstacle_overlay, extent=extent, origin="upper",
            cmap="gray_r", vmin=0, vmax=1, alpha=0.7, interpolation="nearest",
        )
        ax.plot(cx_mm, cy_mm, "o", color="red", markersize=8, label=f"{from_ref}.{from_pad}")
        ax.set_title(
            f"Routability heatmap — {from_ref}.{from_pad} on {layer}, "
            f"W={width_mm} mm, C={round(clearance, 3)} mm "
            f"(grid {resolution_mm} mm). Reach {reachable_area_mm2} mm²."
        )
        ax.set_xlabel("x (mm)")
        ax.set_ylabel("y (mm)")
        ax.legend(loc="upper right", fontsize=8)
        cb = fig.colorbar(im, ax=ax, shrink=0.7)
        cb.set_label("Geodesic distance from pad (mm)")
        fig.savefig(output_path, dpi=110, bbox_inches="tight")
        plt.close(fig)
        out_path = output_path
    except Exception as e:
        logger.warning(f"routability_heatmap render failed: {e}")

    return {
        "success": True,
        "reachable": True,
        "fromXy": {"x": cx_mm, "y": cy_mm, "unit": "mm"},
        "reachableAreaMm2": reachable_area_mm2,
        "maxReachMm": max_reach_mm,
        "widthMm": width_mm,
        "clearanceMm": round(clearance, 4),
        "layer": layer,
        "net": own_net,
        "vizPath": out_path,
        "grid": {"resolutionMm": resolution_mm, "nx": state["nx"], "ny": state["ny"]},
        "message": (
            f"{from_ref}.{from_pad} on {layer} at W={width_mm} mm can reach "
            f"{reachable_area_mm2} mm² (up to {max_reach_mm} mm geodesic). "
            f"PNG: {out_path or '(matplotlib unavailable)'}."
        ),
    }


# --------------------------------------------------------------------------
# Phase 3: per-layer cache + via meta-graph
# --------------------------------------------------------------------------
def _enabled_copper_layers(board: Any) -> List[Tuple[int, str]]:
    """Return the [(layer_id, layer_name)] of enabled copper layers, in
    KiCad's stackup order (F.Cu, In*.Cu, ..., B.Cu)."""
    out: List[Tuple[int, str]] = []
    try:
        seq = board.GetEnabledLayers().Seq()
    except Exception:
        seq = []
    for lid in seq:
        try:
            name = board.GetLayerName(lid)
        except Exception:
            continue
        if isinstance(name, str) and name.endswith(".Cu"):
            out.append((int(lid), name))
    return out


def _compute_layer_cache(
    board: Any,
    own_net: Optional[str],
    g: float,
    layer_ids: Optional[List[int]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Build a `_compute_obstacle_state` per enabled copper layer (or the
    given subset). Keyed by layer name. The grid (`x0, y0, nx, ny`) is
    identical across all layers — same `_board_bbox_mm`."""
    enabled = _enabled_copper_layers(board)
    if layer_ids is not None:
        wanted = set(int(i) for i in layer_ids)
        enabled = [(lid, ln) for lid, ln in enabled if lid in wanted]
    cache: Dict[str, Dict[str, Any]] = {}
    for lid, name in enabled:
        cache[name] = _compute_obstacle_state(board, lid, own_net, g)
    return cache


def _via_candidacy_mask(
    state_a: Dict[str, Any],
    state_b: Dict[str, Any],
    via_diameter_mm: float,
    clearance_mm: float,
) -> np.ndarray:
    """Pixels where a through-via of `via_diameter_mm` (center diameter)
    fits in BOTH layers' free spaces with `clearance_mm` to foreign copper.

    Same correction as `_free_space`: AND with `~mask` on each layer so
    the via center can't land on top of an obstacle in the degenerate
    `via_diameter=clearance=0` case."""
    radius_px = (via_diameter_mm / 2 + clearance_mm) / state_a["g"]
    a_ok = (state_a["dist_px"] >= radius_px) & (~state_a["mask"])
    b_ok = (state_b["dist_px"] >= radius_px) & (~state_b["mask"])
    return a_ok & b_ok


class _UnionFind:
    """Tiny union-find for the meta-graph."""

    def __init__(self) -> None:
        self.parent: Dict[Tuple[str, int], Tuple[str, int]] = {}

    def find(self, x: Tuple[str, int]) -> Tuple[str, int]:
        # Path compression.
        root = x
        while self.parent.get(root, root) != root:
            root = self.parent[root]
        cur = x
        while self.parent.get(cur, cur) != cur:
            nxt = self.parent[cur]
            self.parent[cur] = root
            cur = nxt
        return root

    def union(self, a: Tuple[str, int], b: Tuple[str, int]) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra

    def add(self, x: Tuple[str, int]) -> None:
        self.parent.setdefault(x, x)


def _build_meta_graph(
    cache: Dict[str, Dict[str, Any]],
    width_mm: float,
    clearance_mm: float,
    via_diameter_mm: float,
    via_clearance_mm: float,
) -> Dict[str, Any]:
    """For each layer, compute the per-layer free space + labels. For
    each layer pair, walk the via-candidacy mask and union (layer_a,
    label_a[y,x]) with (layer_b, label_b[y,x]).

    Returns ``{layer_labels, layer_free, uf, via_witness}`` — the union-
    find for connectivity queries, plus per-pair witness pixels for the
    user's "where could I drop a via?" question."""
    layer_names = list(cache.keys())
    layer_free: Dict[str, np.ndarray] = {}
    layer_labels: Dict[str, np.ndarray] = {}
    layer_n: Dict[str, int] = {}
    for name, state in cache.items():
        free = _free_space(state, width_mm, clearance_mm)
        labels, n = scipy_label(free)
        layer_free[name] = free
        layer_labels[name] = labels
        layer_n[name] = int(n)

    uf = _UnionFind()
    for name, n in layer_n.items():
        for cid in range(1, n + 1):
            uf.add((name, cid))

    # Per layer-pair via witnesses. Store one representative (y, x)
    # pixel per (componentA, componentB) bridge for the caller.
    via_witness: Dict[Tuple[str, str], Dict[Tuple[int, int], Tuple[int, int]]] = {}
    for i, la in enumerate(layer_names):
        for lb in layer_names[i + 1:]:
            mask = _via_candidacy_mask(
                cache[la], cache[lb], via_diameter_mm, via_clearance_mm,
            )
            if not mask.any():
                continue
            ys, xs = np.where(mask)
            labels_a = layer_labels[la][ys, xs]
            labels_b = layer_labels[lb][ys, xs]
            # Only pixels where BOTH sides land in a real component
            # (label > 0). The free mask already excludes obstacles so
            # this is usually all pixels, but tiny corner cases at the
            # padding border can produce label==0.
            ok = (labels_a > 0) & (labels_b > 0)
            if not ok.any():
                continue
            ys = ys[ok]
            xs = xs[ok]
            labels_a = labels_a[ok]
            labels_b = labels_b[ok]

            pair_witness: Dict[Tuple[int, int], Tuple[int, int]] = {}
            for y, x, ca, cb in zip(ys.tolist(), xs.tolist(),
                                    labels_a.tolist(), labels_b.tolist()):
                key = (int(ca), int(cb))
                if key not in pair_witness:
                    pair_witness[key] = (int(y), int(x))
                uf.union((la, int(ca)), (lb, int(cb)))
            via_witness[(la, lb)] = pair_witness

    return {
        "layer_free": layer_free,
        "layer_labels": layer_labels,
        "layer_n": layer_n,
        "uf": uf,
        "via_witness": via_witness,
    }


# --------------------------------------------------------------------------
# Public: check_pad_routability_multilayer
# --------------------------------------------------------------------------
def check_pad_routability_multilayer(
    board: Any,
    from_ref: str,
    from_pad: str,
    to_ref: str,
    to_pad: str,
    width_mm: float,
    via_diameter_mm: float,
    clearance_mm: Optional[float] = None,
    via_clearance_mm: Optional[float] = None,
    layers: Optional[List[str]] = None,
    resolution_mm: float = 0.05,
) -> Dict[str, Any]:
    """Are two pads reachable across all copper layers, hopping between
    layers through vias where the via fits in both layers' free space?

    Phase 3 of TOPOLOGY_TOOLS_PLAN.md. Read-only. Through-vias only.

    `layers`: subset of copper layer names to consider (default = all
    enabled copper layers). Useful for "can I route this on F.Cu + In1.Cu
    only" surveys without an inner-layer detour.

    Returns `{reachable, sameLayerReachable, viaCount,
    viaCandidates: [...], layerComponents: [...]}`. `viaCandidates` is a
    sampling of one representative (x, y, layerA, layerB) per
    component-bridge in the meta-graph — *where you could drop a via to
    connect the two regions*, not a prescription.
    """
    pa = _find_pad(board, from_ref, from_pad)
    pb = _find_pad(board, to_ref, to_pad)
    if pa is None:
        return {"success": False, "message": f"Pad not found: {from_ref} pad {from_pad}"}
    if pb is None:
        return {"success": False, "message": f"Pad not found: {to_ref} pad {to_pad}"}

    net_a = pa.GetNetname() or ""
    net_b = pb.GetNetname() or ""
    if net_a and net_b and net_a != net_b:
        return {
            "success": True, "reachable": False,
            "reason": "pads_on_different_nets",
            "fromNet": net_a, "toNet": net_b,
        }
    own_net = net_a or net_b or None
    clearance = _resolve_clearance_mm(board, own_net, clearance_mm)
    via_clearance = (
        float(via_clearance_mm) if via_clearance_mm is not None else clearance
    )

    # Enumerate target layers.
    all_enabled = _enabled_copper_layers(board)
    if layers is not None:
        wanted = set(layers)
        target = [(lid, ln) for lid, ln in all_enabled if ln in wanted]
        unknown = wanted - {ln for _, ln in all_enabled}
        if unknown:
            return {"success": False, "message": f"Unknown layer(s): {sorted(unknown)}"}
    else:
        target = all_enabled
    if not target:
        return {"success": False, "message": "No copper layers found on board"}
    layer_ids = [lid for lid, _ in target]

    # Per-layer state cache (EDT etc.) — single pass.
    cache = _compute_layer_cache(board, own_net, resolution_mm, layer_ids=layer_ids)
    meta = _build_meta_graph(
        cache, width_mm, clearance, via_diameter_mm, via_clearance,
    )

    # Anchor each pad on every layer it has copper on.
    def _anchors_for(pad: Any) -> Dict[str, Tuple[int, int]]:
        out: Dict[str, Tuple[int, int]] = {}
        for lid, ln in target:
            if not _pad_is_on_layer(pad, lid):
                continue
            state = cache[ln]
            free = meta["layer_free"][ln]
            halo_px = int(math.ceil((width_mm / 2 + clearance) / state["g"]))
            anchor, _ = _pad_anchor(state, free, pad, extra_halo_px=halo_px)
            if anchor is not None:
                out[ln] = anchor
        return out

    anchors_a = _anchors_for(pa)
    anchors_b = _anchors_for(pb)
    if not anchors_a:
        return {
            "success": True, "reachable": False,
            "reason": "from_pad_no_escape",
            "message": (
                f"{from_ref}.{from_pad} has no escape lane on any of "
                f"{[ln for _, ln in target]} at W={width_mm} mm."
            ),
        }
    if not anchors_b:
        return {
            "success": True, "reachable": False,
            "reason": "to_pad_no_escape",
            "message": (
                f"{to_ref}.{to_pad} has no escape lane on any of "
                f"{[ln for _, ln in target]} at W={width_mm} mm."
            ),
        }

    # Translate anchors to meta-graph nodes (layer, component_label).
    def _component(layer_name: str, anchor: Tuple[int, int]) -> int:
        return int(meta["layer_labels"][layer_name][anchor])

    nodes_a = {ln: (ln, _component(ln, anc)) for ln, anc in anchors_a.items()}
    nodes_b = {ln: (ln, _component(ln, anc)) for ln, anc in anchors_b.items()}

    uf = meta["uf"]
    reachable = False
    same_layer_only = False
    for na in nodes_a.values():
        for nb in nodes_b.values():
            if uf.find(na) == uf.find(nb):
                reachable = True
                # Same-layer reachable means the two anchors land in the
                # SAME component on the SAME layer. Equal layer names is
                # not enough — the layers could still be split into
                # multiple components that only connect through a via
                # on another layer.
                if na == nb:
                    same_layer_only = True
                    break
        if same_layer_only:
            break

    if not reachable:
        return {
            "success": True, "reachable": False,
            "reason": "unreachable_any_layer",
            "fromAnchors": list(nodes_a.keys()),
            "toAnchors": list(nodes_b.keys()),
            "message": (
                f"{from_ref}.{from_pad} → {to_ref}.{to_pad}: not reachable "
                f"on {[ln for _, ln in target]} at W={width_mm} mm + "
                f"via={via_diameter_mm} mm even with via bridges."
            ),
            "layer": None,
            "widthMm": width_mm,
            "clearanceMm": round(clearance, 4),
            "net": own_net,
        }

    # Sample one representative via candidate per (layer-pair, comp-pair)
    # bridge — the caller's "where could I drop a via" answer.
    via_candidates: List[Dict[str, Any]] = []
    for (la, lb), bridges in meta["via_witness"].items():
        # Skip pairs that aren't in either pad's reachable closure to
        # keep the list useful.
        state = cache[la]
        for (ca, cb), (y, x) in bridges.items():
            via_candidates.append({
                "layerA": la, "layerB": lb,
                "componentA": ca, "componentB": cb,
                "x": round(state["x0"] + x * state["g"], 4),
                "y": round(state["y0"] + y * state["g"], 4),
                "unit": "mm",
            })

    return {
        "success": True,
        "reachable": True,
        "sameLayerReachable": same_layer_only,
        "fromAnchors": list(nodes_a.keys()),
        "toAnchors": list(nodes_b.keys()),
        "viaCandidates": via_candidates[:100],  # cap noise
        "viaCandidatesTotal": len(via_candidates),
        "layerComponents": {
            ln: meta["layer_n"][ln] for _, ln in target
        },
        "widthMm": width_mm,
        "viaDiameterMm": via_diameter_mm,
        "clearanceMm": round(clearance, 4),
        "viaClearanceMm": round(via_clearance, 4),
        "net": own_net,
        "layers": [ln for _, ln in target],
        "grid": {"resolutionMm": resolution_mm},
        "message": (
            f"{from_ref}.{from_pad} → {to_ref}.{to_pad}: reachable "
            f"({'same-layer' if same_layer_only else 'requires via bridge'}). "
            f"{len(via_candidates)} candidate via location(s) found across "
            f"{len(meta['via_witness'])} layer-pair(s)."
        ),
    }


# --------------------------------------------------------------------------
# Public: routability_report (all-pairs feasibility matrix)
# --------------------------------------------------------------------------
def routability_report(
    board: Any,
    width_mm: float,
    via_diameter_mm: float,
    clearance_mm: Optional[float] = None,
    via_clearance_mm: Optional[float] = None,
    layers: Optional[List[str]] = None,
    resolution_mm: float = 0.05,
    nets: Optional[List[str]] = None,
    max_pairs_per_net: int = 64,
) -> Dict[str, Any]:
    """All-pairs multi-layer feasibility matrix. For each net, build the
    pad pairs (capped at `max_pairs_per_net` to bound runtime on large
    nets like GND) and ask `check_pad_routability_multilayer`. The
    expensive bit — per-layer EDT + meta-graph — is built ONCE per
    distinct `(net, width)` pair: for the simple "all nets at one width"
    use case, we cache the layer state at the all-copper-is-obstacle
    setting and reuse across nets (close enough for the matrix's
    'flag the impossible ratlines' purpose; per-net refinement is a
    Phase-4 follow-up).

    Returns `{ratlines: [...], summary: {totalRatlines, reachable,
    unreachable, sameLayer, viaRequired}}`. Each ratline entry includes
    `fromRef/Pad`, `toRef/Pad`, `net`, `reachable`, `reason`,
    `sameLayerReachable`.
    """
    # Enumerate target layers.
    all_enabled = _enabled_copper_layers(board)
    if layers is not None:
        wanted = set(layers)
        target = [(lid, ln) for lid, ln in all_enabled if ln in wanted]
        unknown = wanted - {ln for _, ln in all_enabled}
        if unknown:
            return {"success": False, "message": f"Unknown layer(s): {sorted(unknown)}"}
    else:
        target = all_enabled
    if not target:
        return {"success": False, "message": "No copper layers found on board"}
    layer_ids = [lid for lid, _ in target]
    layer_names = [ln for _, ln in target]

    # Collect pad pairs per net.
    nets_to_pads: Dict[str, List[Tuple[str, str, Any]]] = {}
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            name = pad.GetNetname() or ""
            if not name:
                continue
            if nets is not None and name not in nets:
                continue
            nets_to_pads.setdefault(name, []).append(
                (fp.GetReference(), pad.GetNumber(), pad)
            )

    # Build a single layer cache + meta-graph treating "all copper as
    # obstacle" (own_net=None). This is approximate vs. per-net analysis
    # but it's the matrix's right granularity: a ratline that can't be
    # routed even WITH its own net's copper removed is the same one
    # we'd flag with per-net analysis, modulo small numerical noise.
    # The conservative bias: a ratline marked unreachable here MIGHT be
    # routable per-net. We document that and recommend
    # check_pad_routability_multilayer for the per-net follow-up.
    default_clearance = _resolve_clearance_mm(board, None, clearance_mm)
    via_clearance = (
        float(via_clearance_mm)
        if via_clearance_mm is not None else default_clearance
    )

    cache = _compute_layer_cache(board, None, resolution_mm, layer_ids=layer_ids)
    meta = _build_meta_graph(
        cache, width_mm, default_clearance, via_diameter_mm, via_clearance,
    )
    uf = meta["uf"]
    layer_free = meta["layer_free"]
    layer_labels = meta["layer_labels"]

    ratlines: List[Dict[str, Any]] = []
    counts = {
        "totalRatlines": 0, "reachable": 0, "unreachable": 0,
        "sameLayer": 0, "viaRequired": 0,
    }

    def _anchors_for(pad: Any) -> Dict[str, Tuple[int, int]]:
        out: Dict[str, Tuple[int, int]] = {}
        for lid, ln in target:
            if not _pad_is_on_layer(pad, lid):
                continue
            state = cache[ln]
            halo_px = int(math.ceil(
                (width_mm / 2 + default_clearance) / state["g"]
            ))
            anchor, _ = _pad_anchor(
                state, layer_free[ln], pad, extra_halo_px=halo_px,
            )
            if anchor is not None:
                out[ln] = anchor
        return out

    for net_name, entries in nets_to_pads.items():
        if len(entries) < 2:
            continue
        # Take entries in order, build a spanning star (entry[0] → each
        # of entry[1:]) and cap at max_pairs_per_net per net. This is
        # not a Steiner tree, but mirrors what the ratsnest cache emits.
        pairs = [
            (entries[0], entries[i])
            for i in range(1, min(len(entries), max_pairs_per_net + 1))
        ]
        for (ra, na, pad_a), (rb, nb, pad_b) in pairs:
            counts["totalRatlines"] += 1
            anchors_a = _anchors_for(pad_a)
            anchors_b = _anchors_for(pad_b)
            if not anchors_a or not anchors_b:
                ratlines.append({
                    "net": net_name,
                    "fromRef": ra, "fromPad": na,
                    "toRef": rb, "toPad": nb,
                    "reachable": False,
                    "reason": (
                        "from_pad_no_escape" if not anchors_a
                        else "to_pad_no_escape"
                    ),
                    "sameLayerReachable": False,
                })
                counts["unreachable"] += 1
                continue

            nodes_a = [(ln, int(layer_labels[ln][anc]))
                       for ln, anc in anchors_a.items()]
            nodes_b = [(ln, int(layer_labels[ln][anc]))
                       for ln, anc in anchors_b.items()]
            reach = False
            same = False
            for na_node in nodes_a:
                for nb_node in nodes_b:
                    if uf.find(na_node) == uf.find(nb_node):
                        reach = True
                        # See note in check_pad_routability_multilayer:
                        # same-layer reachable requires the anchors to
                        # land in the same component on the same layer.
                        if na_node == nb_node:
                            same = True
                            break
                if same:
                    break

            ratlines.append({
                "net": net_name,
                "fromRef": ra, "fromPad": na,
                "toRef": rb, "toPad": nb,
                "reachable": reach,
                "sameLayerReachable": reach and same,
                "reason": None if reach else "unreachable_any_layer",
            })
            if reach:
                counts["reachable"] += 1
                if same:
                    counts["sameLayer"] += 1
                else:
                    counts["viaRequired"] += 1
            else:
                counts["unreachable"] += 1

    return {
        "success": True,
        "widthMm": width_mm,
        "viaDiameterMm": via_diameter_mm,
        "clearanceMm": round(default_clearance, 4),
        "viaClearanceMm": round(via_clearance, 4),
        "layers": layer_names,
        "grid": {"resolutionMm": resolution_mm, "perLayerComponents": meta["layer_n"]},
        "summary": counts,
        "ratlines": ratlines,
        "limitations": (
            "Treats all copper as obstacle (per-net analysis would be "
            "more accurate but slower). A ratline marked unreachable "
            "here MIGHT still route once its own net's copper is "
            "excluded — confirm with check_pad_routability_multilayer "
            "per-net for any flagged ratline."
        ),
        "message": (
            f"{counts['totalRatlines']} ratlines @ W={width_mm} mm, "
            f"via={via_diameter_mm} mm: {counts['reachable']} reachable "
            f"({counts['sameLayer']} same-layer, "
            f"{counts['viaRequired']} via-required), "
            f"{counts['unreachable']} UNREACHABLE."
        ),
    }


# --------------------------------------------------------------------------
# Phase 4: pre_route_audit + remediation hints
# --------------------------------------------------------------------------
def _remediation_hint(
    reason: Optional[str],
    width_mm: float,
    clearance_mm: float,
    netclass_name: str,
) -> Optional[str]:
    """Map a Phase 3 failure-reason code to a short, actionable hint.
    Returns None for reachable ratlines."""
    if reason is None:
        return None
    w = round(width_mm, 3)
    c = round(clearance_mm, 3)
    if reason == "from_pad_no_escape":
        return (
            f"From-pad has no escape at width {w} mm + clearance {c} mm "
            f"(netclass '{netclass_name}'). Either lower the netclass "
            f"width, move foreign-net copper away from the pad, or pin-"
            f"escape with a narrow stub (route_pad_to_pad escapeFromWidth)."
        )
    if reason == "to_pad_no_escape":
        return (
            f"To-pad has no escape at width {w} mm + clearance {c} mm "
            f"(netclass '{netclass_name}'). Same remediation as above on "
            f"the destination side."
        )
    if reason == "unreachable_any_layer":
        return (
            f"Pads cannot be connected at netclass '{netclass_name}' "
            f"(W={w} mm, C={c} mm) on ANY copper layer, even with via "
            f"bridges. Move components closer, widen the corridor, or "
            f"assign a netclass with narrower width."
        )
    if reason == "pads_on_different_nets":
        return (
            "Endpoint pads are on different nets — the ratline shouldn't "
            "exist. Re-check schematic connections."
        )
    if reason == "spoke_unreachable_from_hub":
        return (
            f"This terminal can't reach the net's main hub at netclass "
            f"'{netclass_name}' (W={w} mm, C={c} mm). Likely missing a "
            f"stitching via to the main plane, or geometrically isolated "
            f"by foreign-net copper. Check the cluster grouping for "
            f"other terminals stuck in the same sub-net."
        )
    return None


# --------------------------------------------------------------------------
# Phase 5: Net-terminal abstraction (pads + zones treated uniformly)
# --------------------------------------------------------------------------
class _Terminal:
    """A net's terminal — either a pad or a zone. Carries the geometry
    needed to anchor it into the meta-graph and to choose a hub.

    Per the 2026-06-13 design discussion: zones are first-class
    terminals (large pads, basically), every spoke is checked against
    the hub via the existing union-find on the meta-graph, and a
    zone-spoke that doesn't reach the hub is a legitimate flag (it
    surfaces the unstitched-bridge-zone case directly).
    """

    __slots__ = (
        "uid", "net", "kind", "label",
        "layer_ids", "area_mm2", "centroid_mm",
        "_pad", "_zone",
    )

    def __init__(
        self,
        uid: str, net: str, kind: str, label: str,
        layer_ids: List[int], area_mm2: float,
        centroid_mm: Tuple[float, float],
        pad: Optional[Any] = None, zone: Optional[Any] = None,
    ) -> None:
        self.uid = uid
        self.net = net
        self.kind = kind  # "pad" or "zone"
        self.label = label  # human-readable for audit output
        self.layer_ids = layer_ids
        self.area_mm2 = area_mm2
        self.centroid_mm = centroid_mm
        self._pad = pad
        self._zone = zone

    def __repr__(self) -> str:
        return f"<Terminal {self.label} ({self.kind}, {self.area_mm2:.2f} mm²)>"


def _short_uuid(item: Any) -> str:
    """First 8 hex chars of an item's KiCad UUID (or 'noid' if absent)."""
    try:
        return item.m_Uuid.AsString().replace("-", "")[:8]
    except Exception:
        return "noid"


def _enumerate_terminals(
    board: Any,
    target_layer_ids: List[int],
    nets_filter: Optional[set] = None,
) -> Dict[str, List["_Terminal"]]:
    """Walk the board's pads + zones and return a per-net terminal list.

    Each terminal records the copper layer IDs it's present on (within
    `target_layer_ids`), its area in mm² (for hub selection), and a
    centroid (for the centrality tiebreaker among similar-area pads).
    Zones use the FILLED polygon — un-filled zones are skipped (same
    as the obstacle rasteriser; un-filled means no copper yet).
    """
    target_set = set(int(lid) for lid in target_layer_ids)
    out: Dict[str, List[_Terminal]] = {}

    # Pads.
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        for pad in fp.Pads():
            net = pad.GetNetname() or ""
            if not net:
                continue
            if nets_filter is not None and net not in nets_filter:
                continue
            layer_ids = [lid for lid in target_set if _pad_is_on_layer(pad, lid)]
            if not layer_ids:
                continue
            sz = pad.GetSize()
            area_mm2 = (sz.x / SCALE) * (sz.y / SCALE)
            pos = pad.GetPosition()
            term = _Terminal(
                uid=f"PAD_{ref}_{pad.GetNumber()}_{_short_uuid(pad)}",
                net=net,
                kind="pad",
                label=f"{ref}.{pad.GetNumber()}",
                layer_ids=layer_ids,
                area_mm2=area_mm2,
                centroid_mm=(pos.x / SCALE, pos.y / SCALE),
                pad=pad,
            )
            out.setdefault(net, []).append(term)

    # Zones.
    try:
        n_zones = board.GetAreaCount()
    except Exception:
        n_zones = 0
    for i in range(n_zones):
        try:
            zone = board.GetArea(i)
        except Exception:
            continue
        net = zone.GetNetname() or ""
        if not net:
            continue
        if nets_filter is not None and net not in nets_filter:
            continue
        layer_ids: List[int] = []
        for lid in target_set:
            try:
                if not zone.IsOnLayer(lid):
                    continue
                poly = zone.GetFilledPolysList(lid)
                if poly is None or poly.IsEmpty():
                    continue
                layer_ids.append(lid)
            except Exception:
                continue
        if not layer_ids:
            continue
        # Area: prefer GetFilledArea (nm²), but fall back to the
        # shoelace area of the filled polygon when it's 0 or fails —
        # ZONE_FILLER segfaults on Python-constructed boards in tests
        # so SetFilledPolysList is used directly and the internal
        # GetFilledArea cache stays 0. We can recompute from the
        # polygon we already have.
        area_mm2 = 0.0
        try:
            area_mm2 = float(zone.GetFilledArea()) / (SCALE * SCALE)
        except Exception:
            area_mm2 = 0.0
        if area_mm2 <= 0.0:
            for lid in layer_ids:
                try:
                    poly_set = zone.GetFilledPolysList(lid)
                except Exception:
                    continue
                if poly_set is None or poly_set.IsEmpty():
                    continue
                for outline_idx in range(poly_set.OutlineCount()):
                    outer = poly_set.Outline(outline_idx)
                    n = outer.PointCount()
                    if n < 3:
                        continue
                    # Shoelace formula in mm.
                    a = 0.0
                    for j in range(n):
                        k = (j + 1) % n
                        p = outer.CPoint(j)
                        q = outer.CPoint(k)
                        a += (p.x / SCALE) * (q.y / SCALE)
                        a -= (q.x / SCALE) * (p.y / SCALE)
                    area_mm2 += abs(a) / 2.0
        if area_mm2 <= 0.0:
            # Final fallback: bbox (over-counts for L-shapes etc., but
            # the zone is at least *somewhere*).
            bb = zone.GetBoundingBox()
            area_mm2 = (bb.GetWidth() / SCALE) * (bb.GetHeight() / SCALE)
        # Centroid: bbox center is a good-enough representative for hub
        # tiebreaks. (Polygon centroid would be more accurate but the
        # bbox suffices for "which hub is most central?")
        bb = zone.GetBoundingBox()
        cx_mm = (bb.GetLeft() + bb.GetWidth() / 2.0) / SCALE
        cy_mm = (bb.GetTop() + bb.GetHeight() / 2.0) / SCALE
        primary_layer_name = board.GetLayerName(layer_ids[0])
        term = _Terminal(
            uid=f"ZONE_{net}_{primary_layer_name}_{_short_uuid(zone)}",
            net=net,
            kind="zone",
            label=f"ZONE_{net}_{primary_layer_name}_{_short_uuid(zone)}",
            layer_ids=layer_ids,
            area_mm2=area_mm2,
            centroid_mm=(cx_mm, cy_mm),
            zone=zone,
        )
        out.setdefault(net, []).append(term)

    return out


def _select_hub(terminals: List["_Terminal"]) -> int:
    """Return the index of the hub terminal in `terminals`.

    Primary criterion: biggest area_mm2 (so a large pour beats any pad).
    Tiebreaker: most-central terminal — minimum sum of distances to
    every other terminal. For nets with one zone and many pads the
    zone wins on area alone; for signal nets with no zone, centrality
    picks a reasonable "center of mass" pad instead of whoever's first
    in iteration order.
    """
    if not terminals:
        return -1
    max_area = max(t.area_mm2 for t in terminals)
    candidates = [
        i for i, t in enumerate(terminals)
        if t.area_mm2 >= max_area - 1e-9
    ]
    if len(candidates) == 1:
        return candidates[0]

    # Centrality tiebreaker.
    def _sum_dist(i: int) -> float:
        cx, cy = terminals[i].centroid_mm
        return sum(
            math.hypot(cx - terminals[j].centroid_mm[0],
                       cy - terminals[j].centroid_mm[1])
            for j in range(len(terminals)) if j != i
        )
    return min(candidates, key=_sum_dist)


def _rasterize_zone_on_layer(
    zone: Any,
    layer_id: int,
    state: Dict[str, Any],
) -> Optional[np.ndarray]:
    """Rasterize a single zone's filled polygon on `layer_id` into a
    bool mask of `state["mask"]`'s shape. Same algorithm as
    `_stamp_zones_on_layer` but for one zone — used to find which
    free-space components the zone overlaps.
    """
    try:
        if not zone.IsOnLayer(layer_id):
            return None
        poly_set = zone.GetFilledPolysList(layer_id)
    except Exception:
        return None
    if poly_set is None or poly_set.IsEmpty():
        return None
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    ny, nx = state["mask"].shape
    g = state["g"]
    x0 = state["x0"]
    y0 = state["y0"]

    def _verts(chain: Any) -> List[Tuple[int, int]]:
        return [
            _xy_mm_to_px(
                chain.CPoint(i).x / SCALE, chain.CPoint(i).y / SCALE,
                x0, y0, g,
            )
            for i in range(chain.PointCount())
        ]

    img = Image.new("L", (nx, ny), 0)
    draw = ImageDraw.Draw(img)
    for outline_idx in range(poly_set.OutlineCount()):
        try:
            outer = poly_set.Outline(outline_idx)
        except Exception:
            continue
        verts = _verts(outer)
        if len(verts) >= 3:
            draw.polygon(verts, fill=1)
        try:
            n_holes = poly_set.HoleCount(outline_idx)
        except Exception:
            n_holes = 0
        for hole_idx in range(n_holes):
            try:
                hole = poly_set.Hole(outline_idx, hole_idx)
            except Exception:
                continue
            hole_verts = _verts(hole)
            if len(hole_verts) >= 3:
                draw.polygon(hole_verts, fill=0)
    return np.array(img, dtype=bool)


def _terminal_meta_nodes(
    term: "_Terminal",
    cache: Dict[str, Dict[str, Any]],
    layer_free: Dict[str, np.ndarray],
    layer_labels: Dict[str, np.ndarray],
    target_layers: List[Tuple[int, str]],
    width_mm: float,
    clearance_mm: float,
) -> set:
    """Return the set of `(layer_name, component_id)` meta-graph nodes
    a terminal contributes — one per layer for pads (when the pad has
    a free-space anchor on that layer), one *or more* per layer for
    zones (every free-space component the zone's filled mask overlaps
    on that layer).
    """
    nodes: set = set()
    layer_id_to_name = {lid: ln for lid, ln in target_layers}
    if term.kind == "pad":
        for lid in term.layer_ids:
            ln = layer_id_to_name.get(lid)
            if ln is None or ln not in cache:
                continue
            state = cache[ln]
            halo_px = int(math.ceil(
                (width_mm / 2 + clearance_mm) / state["g"]
            ))
            anchor, _ = _pad_anchor(
                state, layer_free[ln], term._pad, extra_halo_px=halo_px,
            )
            if anchor is None:
                continue
            cid = int(layer_labels[ln][anchor])
            if cid > 0:
                nodes.add((ln, cid))
    elif term.kind == "zone":
        for lid in term.layer_ids:
            ln = layer_id_to_name.get(lid)
            if ln is None or ln not in cache:
                continue
            state = cache[ln]
            zone_mask = _rasterize_zone_on_layer(term._zone, lid, state)
            if zone_mask is None:
                continue
            # Intersect with free space — same-net pour is invisible to
            # the obstacle mask, so the pour's pixels live in whatever
            # free-space component the layer has there.
            free = layer_free[ln]
            overlap = zone_mask & free
            if not overlap.any():
                continue
            labels_arr = layer_labels[ln]
            unique = np.unique(labels_arr[overlap])
            for cid in unique:
                cid_i = int(cid)
                if cid_i > 0:
                    nodes.add((ln, cid_i))
    return nodes


def pre_route_audit(
    board: Any,
    width_mm_override: Optional[float] = None,
    via_diameter_mm_override: Optional[float] = None,
    clearance_mm_override: Optional[float] = None,
    via_clearance_mm_override: Optional[float] = None,
    layers: Optional[List[str]] = None,
    resolution_mm: float = 0.05,
    nets: Optional[List[str]] = None,
    max_pairs_per_net: int = 64,
) -> Dict[str, Any]:
    """Pre-flight all-ratlines feasibility check at each net's *own*
    netclass widths — the Phase-4 workflow tool. Read-only.

    Iterates the board's netclasses. For each, builds the multi-layer
    meta-graph once at that class's (track width, clearance, via
    diameter, via clearance), then queries the ratlines of every net
    assigned to the class. The per-layer EDT cache is shared across
    netclasses (it depends only on the obstacle SET, which is the same
    `all-copper-as-obstacle` approximation `routability_report` uses).

    Pass any `*_override` to skip the per-netclass lookup and use a
    single value for all nets (useful for "what if I dropped the
    netclass width to W?" what-if surveys).

    Returns `{summary, spokes, netclassesEvaluated, limitations}`.
    Each unreachable spoke carries a `remediationHint` string with the
    actionable next move.

    **Terminal model (2026-06-13).** A "terminal" is a pad OR a zone —
    both are first-class members of a net. For each net, the audit
    picks the largest terminal by area as a *hub* (so a same-net plane
    pour dominates over the pads it serves) and asks "can each other
    terminal reach the hub?" via the existing multi-layer meta-graph.
    For plane-connected nets this collapses to "does every pad have a
    via path to the plane?" which is the engineering question, not
    "does pad A connect to pad B at netclass W trace width" which the
    old spanning-star framing forced. An unstitched bridge zone shows
    up as a real flag (its own spoke fails to reach the main pour),
    naturally surfacing the missing-stitching-via case without a
    separate tool call.
    """
    # Enumerate copper layers (same as Phase 3 tools).
    all_enabled = _enabled_copper_layers(board)
    if layers is not None:
        wanted = set(layers)
        target = [(lid, ln) for lid, ln in all_enabled if ln in wanted]
        unknown = wanted - {ln for _, ln in all_enabled}
        if unknown:
            return {"success": False, "message": f"Unknown layer(s): {sorted(unknown)}"}
    else:
        target = all_enabled
    if not target:
        return {"success": False, "message": "No copper layers found on board"}
    layer_ids = [lid for lid, _ in target]

    # Enumerate terminals (pads + zones) grouped by net.
    nets_filter = set(nets) if nets else None
    nets_to_terminals = _enumerate_terminals(board, layer_ids, nets_filter)

    # Group nets by their netclass — one meta-graph per class.
    try:
        net_settings = board.GetDesignSettings().m_NetSettings
    except Exception:
        net_settings = None
    classes_to_nets: Dict[str, List[str]] = {}
    class_to_widths: Dict[str, Dict[str, float]] = {}
    for net_name in nets_to_terminals:
        nc_name = "Default"
        if net_settings is not None:
            try:
                nc = net_settings.GetEffectiveNetClass(net_name)
                if nc is not None:
                    nc_name = nc.GetName()
            except Exception:
                pass
        classes_to_nets.setdefault(nc_name, []).append(net_name)

    # Look up each class's widths/clearances.
    all_classes = {}
    try:
        all_classes = board.GetAllNetClasses()
    except Exception:
        all_classes = {}
    for nc_name in classes_to_nets:
        nc = all_classes.get(nc_name)
        if nc is None and net_settings is not None:
            try:
                nc = net_settings.GetNetClassByName(nc_name)
            except Exception:
                nc = None
        widths = {
            "trackWidthMm": 0.2,
            "clearanceMm": 0.2,
            "viaDiameterMm": 0.6,
            "viaClearanceMm": 0.2,
        }
        if nc is not None:
            try:
                widths["trackWidthMm"] = float(nc.GetTrackWidth()) / SCALE
                widths["clearanceMm"] = float(nc.GetClearance()) / SCALE
                widths["viaDiameterMm"] = float(nc.GetViaDiameter()) / SCALE
                widths["viaClearanceMm"] = widths["clearanceMm"]
            except Exception:
                pass
        # Apply overrides.
        if width_mm_override is not None:
            widths["trackWidthMm"] = float(width_mm_override)
        if clearance_mm_override is not None:
            widths["clearanceMm"] = float(clearance_mm_override)
        if via_diameter_mm_override is not None:
            widths["viaDiameterMm"] = float(via_diameter_mm_override)
        if via_clearance_mm_override is not None:
            widths["viaClearanceMm"] = float(via_clearance_mm_override)
        class_to_widths[nc_name] = widths

    # Build the per-layer cache ONCE (depends only on the obstacle set,
    # not on width). Reused across all netclasses.
    cache = _compute_layer_cache(board, None, resolution_mm, layer_ids=layer_ids)

    # Per-netclass meta-graph + per-spoke evaluation.
    spokes_out: List[Dict[str, Any]] = []
    counts = {
        "netsEvaluated": 0,
        "totalSpokes": 0,
        "reachable": 0,
        "unreachable": 0,
        "sameLayer": 0,
        "viaRequired": 0,
        "netsWithIsolatedClusters": 0,
    }
    netclasses_evaluated: List[Dict[str, Any]] = []

    def _terminal_summary(term: "_Terminal") -> Dict[str, Any]:
        return {
            "uid": term.uid,
            "kind": term.kind,
            "label": term.label,
            "areaMm2": round(term.area_mm2, 4),
            "layers": [board.GetLayerName(lid) for lid in term.layer_ids],
        }

    # Caches we lazily build:
    # - `shared_meta_per_class`: meta-graph at all-copper-as-obstacle
    #   per netclass — reused across every net in the class that has
    #   NO same-net zones (the hub will be a pad in that case; the
    #   all-copper view is fine).
    # - per-net cache + meta-graph: built when the net has at least
    #   one zone (the hub is then likely the pour, and we MUST exclude
    #   own-net copper from obstacles or the pour disappears from the
    #   meta-graph as foreign-net obstacle).
    shared_meta_per_class: Dict[str, Dict[str, Any]] = {}

    for nc_name, net_names in classes_to_nets.items():
        widths = class_to_widths[nc_name]
        nc_counts = {
            "netsEvaluated": 0,
            "totalSpokes": 0,
            "reachable": 0,
            "unreachable": 0,
            "sameLayer": 0,
            "viaRequired": 0,
        }

        for net_name in net_names:
            terminals = nets_to_terminals.get(net_name, [])
            if len(terminals) < 2:
                continue
            # Cap spokes per net to bound runtime on large fan-out nets.
            if len(terminals) > max_pairs_per_net + 1:
                # Keep all zones (typically few) and trim pads.
                zones = [t for t in terminals if t.kind == "zone"]
                pads = [t for t in terminals if t.kind == "pad"]
                room = max(0, max_pairs_per_net + 1 - len(zones))
                terminals = zones + pads[:room]
            nc_counts["netsEvaluated"] += 1
            counts["netsEvaluated"] += 1

            # Pick the meta-graph this net will use:
            # - Net has any same-net zones → per-net analysis (the
            #   pour disappears from the obstacle field as own-net,
            #   so via candidates can land in it).
            # - Otherwise → shared all-copper-as-obstacle meta-graph
            #   for the netclass (cheap, correct for pad-only nets).
            net_has_zone = any(t.kind == "zone" for t in terminals)
            if net_has_zone:
                cache_net = _compute_layer_cache(
                    board, net_name, resolution_mm, layer_ids=layer_ids,
                )
                meta = _build_meta_graph(
                    cache_net,
                    widths["trackWidthMm"], widths["clearanceMm"],
                    widths["viaDiameterMm"], widths["viaClearanceMm"],
                )
            else:
                if nc_name not in shared_meta_per_class:
                    shared_meta_per_class[nc_name] = _build_meta_graph(
                        cache,
                        widths["trackWidthMm"], widths["clearanceMm"],
                        widths["viaDiameterMm"], widths["viaClearanceMm"],
                    )
                cache_net = cache
                meta = shared_meta_per_class[nc_name]
            uf = meta["uf"]
            layer_free = meta["layer_free"]
            layer_labels = meta["layer_labels"]

            hub_idx = _select_hub(terminals)
            hub = terminals[hub_idx]
            hub_nodes = _terminal_meta_nodes(
                hub, cache_net, layer_free, layer_labels, target,
                widths["trackWidthMm"], widths["clearanceMm"],
            )
            # Map each terminal (by index) to its UF-class root via any
            # of its meta-graph nodes. Spokes sharing a root with the
            # hub are reachable.
            terminal_nodes: List[set] = []
            terminal_roots: List[set] = []
            for term in terminals:
                nodes = _terminal_meta_nodes(
                    term, cache_net, layer_free, layer_labels, target,
                    widths["trackWidthMm"], widths["clearanceMm"],
                )
                terminal_nodes.append(nodes)
                terminal_roots.append({uf.find(n) for n in nodes})
            hub_roots = terminal_roots[hub_idx]

            # Spoke evaluation. We also group failed spokes by their UF
            # roots so the user sees clusters together (e.g. an
            # unstitched bridge zone + its embraced pads in one entry).
            failed_groups: Dict[Any, List[Dict[str, Any]]] = {}

            for i, term in enumerate(terminals):
                if i == hub_idx:
                    continue
                counts["totalSpokes"] += 1
                nc_counts["totalSpokes"] += 1
                spoke_roots = terminal_roots[i]
                reach = bool(spoke_roots & hub_roots)
                # "Same layer" reachability: any meta-graph node is
                # shared exactly between the spoke and the hub (no via
                # hop needed). Only meaningful for reachable spokes.
                same_layer = bool(
                    reach and (terminal_nodes[i] & hub_nodes)
                )
                # Reason + remediation
                if not spoke_roots:
                    reason = (
                        "spoke_no_anchor" if term.kind == "pad"
                        else "zone_no_anchor"
                    )
                elif reach:
                    reason = None
                else:
                    reason = "spoke_unreachable_from_hub"
                hint = _remediation_hint(
                    reason, widths["trackWidthMm"],
                    widths["clearanceMm"], nc_name,
                )
                entry = {
                    "net": net_name,
                    "netclass": nc_name,
                    "trackWidthMm": widths["trackWidthMm"],
                    "clearanceMm": widths["clearanceMm"],
                    "viaDiameterMm": widths["viaDiameterMm"],
                    "hub": _terminal_summary(hub),
                    "spoke": _terminal_summary(term),
                    "reachable": reach,
                    "sameLayerReachable": same_layer,
                    "reason": reason,
                    "remediationHint": hint,
                }
                spokes_out.append(entry)
                if reach:
                    counts["reachable"] += 1
                    nc_counts["reachable"] += 1
                    if same_layer:
                        counts["sameLayer"] += 1
                        nc_counts["sameLayer"] += 1
                    else:
                        counts["viaRequired"] += 1
                        nc_counts["viaRequired"] += 1
                else:
                    counts["unreachable"] += 1
                    nc_counts["unreachable"] += 1
                    # Pick a representative root for clustering (any
                    # one of the spoke's roots — they're all in the
                    # same UF class as each other within this terminal).
                    if spoke_roots:
                        key = next(iter(spoke_roots))
                        failed_groups.setdefault(key, []).append(entry)

            if failed_groups:
                counts["netsWithIsolatedClusters"] += 1
                # Attach a `clusterPeers` list onto each unreachable
                # spoke so the user sees which other terminals are
                # stuck with it.
                for group in failed_groups.values():
                    if len(group) <= 1:
                        continue
                    peer_labels = [e["spoke"]["label"] for e in group]
                    for e in group:
                        e["clusterPeers"] = [
                            lbl for lbl in peer_labels
                            if lbl != e["spoke"]["label"]
                        ]

        netclasses_evaluated.append({
            "netclass": nc_name,
            **widths,
            "netCount": len(net_names),
            "counts": nc_counts,
        })

    return {
        "success": True,
        "layers": [ln for _, ln in target],
        "grid": {"resolutionMm": resolution_mm},
        "netclassesEvaluated": netclasses_evaluated,
        "summary": counts,
        "spokes": spokes_out,
        "limitations": (
            "Hub = largest-area terminal (pad or zone) per net. Spoke "
            "= every other terminal. Reachability is asked against the "
            "multi-layer meta-graph (all-copper-as-obstacle + own-net "
            "exclusion). Failed spokes grouped by UF-class so an "
            "isolated cluster of terminals appears together — useful "
            "for spotting unstitched bridge zones. For per-pair "
            "queries use check_pad_routability_multilayer."
        ),
        "message": (
            f"Pre-route audit: {counts['totalSpokes']} spokes across "
            f"{counts['netsEvaluated']} nets in "
            f"{len(netclasses_evaluated)} netclass(es). "
            f"{counts['reachable']} reachable "
            f"({counts['sameLayer']} same-layer, "
            f"{counts['viaRequired']} via-required), "
            f"{counts['unreachable']} UNREACHABLE "
            f"({counts['netsWithIsolatedClusters']} net(s) with isolated "
            f"clusters)."
        ),
    }


# --------------------------------------------------------------------------
# Phase 4c: polygon-exact mode (shapely)
# --------------------------------------------------------------------------
def _oriented_rect_polygon(
    cx_mm: float, cy_mm: float,
    w_mm: float, h_mm: float, angle_deg: float,
) -> Any:
    """Return a shapely Polygon for an oriented rectangle."""
    from shapely.geometry import Polygon
    cos_a = math.cos(math.radians(angle_deg))
    sin_a = math.sin(math.radians(angle_deg))
    hw, hh = w_mm / 2.0, h_mm / 2.0
    corners = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]
    pts = [
        (cx_mm + cx * cos_a - cy * sin_a, cy_mm + cx * sin_a + cy * cos_a)
        for cx, cy in corners
    ]
    return Polygon(pts)


def _pad_polygon(pad: Any) -> Any:
    """Approximate a pad as an oriented rectangle (matching the raster
    rasterizer's choice). Same accuracy trade-off as Phase 1."""
    pos = pad.GetPosition()
    sz = pad.GetSize()
    try:
        angle_deg = pad.GetOrientation().AsDegrees()
    except AttributeError:
        angle_deg = float(pad.GetOrientation()) / 10.0
    return _oriented_rect_polygon(
        pos.x / SCALE, pos.y / SCALE,
        sz.x / SCALE, sz.y / SCALE,
        angle_deg,
    )


def _build_obstacle_polygons(
    board: Any, layer_id: int, own_net: Optional[str],
) -> List[Any]:
    """Return shapely polygons for foreign-net copper on `layer_id`.
    Vias affect every layer."""
    from shapely.geometry import LineString, Point

    polys: List[Any] = []
    for t in board.Tracks():
        net = t.GetNetname() or ""
        if own_net is not None and net == own_net:
            continue
        if t.Type() == pcbnew.PCB_VIA_T:
            try:
                via_w = t.GetWidth(pcbnew.F_Cu)
            except TypeError:
                via_w = t.GetWidth()
            pos = t.GetPosition()
            polys.append(
                Point(pos.x / SCALE, pos.y / SCALE)
                .buffer((via_w / SCALE) / 2.0, quad_segs=8)
            )
        else:
            if t.GetLayer() != layer_id:
                continue
            s, e = t.GetStart(), t.GetEnd()
            half_w = (t.GetWidth() / SCALE) / 2.0
            line = LineString(
                [(s.x / SCALE, s.y / SCALE), (e.x / SCALE, e.y / SCALE)]
            )
            polys.append(line.buffer(half_w, cap_style="round", quad_segs=8))

    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if not _pad_is_on_layer(pad, layer_id):
                continue
            net = pad.GetNetname() or ""
            if own_net is not None and net == own_net:
                continue
            polys.append(_pad_polygon(pad))

    # Zones (pours). Same own-net filter as tracks/pads.
    try:
        n_zones = board.GetAreaCount()
    except Exception:
        n_zones = 0
    if n_zones > 0:
        from shapely.geometry import Polygon as _Poly
        for i in range(n_zones):
            try:
                zone = board.GetArea(i)
            except Exception:
                continue
            net = zone.GetNetname() or ""
            if own_net is not None and net == own_net:
                continue
            try:
                if not zone.IsOnLayer(layer_id):
                    continue
                poly_set = zone.GetFilledPolysList(layer_id)
            except Exception:
                continue
            if poly_set is None or poly_set.IsEmpty():
                continue
            for outline_idx in range(poly_set.OutlineCount()):
                try:
                    outer = poly_set.Outline(outline_idx)
                except Exception:
                    continue
                shell = [
                    (outer.CPoint(j).x / SCALE, outer.CPoint(j).y / SCALE)
                    for j in range(outer.PointCount())
                ]
                if len(shell) < 3:
                    continue
                holes: List[List[Tuple[float, float]]] = []
                try:
                    n_holes = poly_set.HoleCount(outline_idx)
                except Exception:
                    n_holes = 0
                for hole_idx in range(n_holes):
                    try:
                        hole = poly_set.Hole(outline_idx, hole_idx)
                    except Exception:
                        continue
                    h_pts = [
                        (hole.CPoint(j).x / SCALE, hole.CPoint(j).y / SCALE)
                        for j in range(hole.PointCount())
                    ]
                    if len(h_pts) >= 3:
                        holes.append(h_pts)
                try:
                    poly = _Poly(shell, holes=holes if holes else None)
                except Exception:
                    continue
                if poly.is_empty:
                    continue
                # KiCad's bridged-outline representation creates a
                # self-touching polygon. Shapely warns; .buffer(0) is
                # the canonical "clean it up" idiom.
                if not poly.is_valid:
                    try:
                        poly = poly.buffer(0)
                    except Exception:
                        continue
                polys.append(poly)

    return polys


def _check_pad_routability_exact(
    board: Any,
    from_ref: str, from_pad: str,
    to_ref: str, to_pad: str,
    layer: str,
    width_mm: float,
    clearance_mm: Optional[float],
) -> Dict[str, Any]:
    """Polygon-exact `check_pad_routability` via shapely. Returns
    `{reachable, reason, mode}` — no bottleneck/path (those are
    raster-only)."""
    from shapely.geometry import box
    from shapely.ops import unary_union

    layer_id = board.GetLayerID(layer)
    if layer_id < 0:
        return {"success": False, "message": f"Unknown layer: {layer}"}

    pa = _find_pad(board, from_ref, from_pad)
    pb = _find_pad(board, to_ref, to_pad)
    if pa is None:
        return {"success": False, "message": f"Pad not found: {from_ref} pad {from_pad}"}
    if pb is None:
        return {"success": False, "message": f"Pad not found: {to_ref} pad {to_pad}"}

    net_a = pa.GetNetname() or ""
    net_b = pb.GetNetname() or ""
    if net_a and net_b and net_a != net_b:
        return {
            "success": True, "reachable": False, "mode": "exact",
            "reason": "pads_on_different_nets",
            "fromNet": net_a, "toNet": net_b,
        }
    own_net = net_a or net_b or None
    clearance = _resolve_clearance_mm(board, own_net, clearance_mm)

    obstacles = _build_obstacle_polygons(board, layer_id, own_net)
    xmin, ymin, xmax, ymax = _board_bbox_mm(board)
    board_poly = box(xmin, ymin, xmax, ymax)

    erosion = width_mm / 2.0 + clearance
    if obstacles:
        # Union foreign obstacles, then buffer by W/2+C to model the
        # "trace centerline can live here" set. Difference from the
        # board outline gives the exact free space.
        unioned = unary_union(obstacles)
        expanded = unioned.buffer(erosion, quad_segs=8)
        free = board_poly.difference(expanded)
    else:
        # No foreign copper on this layer — the whole board is free.
        free = board_poly

    # Enumerate components.
    try:
        from shapely.geometry import MultiPolygon
        if isinstance(free, MultiPolygon):
            components = list(free.geoms)
        elif free.is_empty:
            components = []
        else:
            components = [free]
    except Exception:
        components = [free] if not free.is_empty else []

    if not components:
        return {
            "success": True, "reachable": False, "mode": "exact",
            "reason": "different_components",
            "message": (
                f"No free space on {layer} at width {width_mm} mm + "
                f"clearance {round(clearance, 3)} mm — every pixel is "
                "within (W/2 + C) of foreign copper."
            ),
            "layer": layer, "widthMm": width_mm,
            "clearanceMm": round(clearance, 4),
            "net": own_net,
        }

    # Anchor each pad to a component. The pad's "escape zone" is the
    # pad polygon buffered by W/2 + C + ε — a trace exits the pad into
    # free space at exactly this offset. Using the escape zone (not the
    # bare pad polygon) handles both cases uniformly:
    #   own_net=X: same-net pad isn't in obstacles, escape zone touches
    #     the adjacent free-space component.
    #   own_net=None: pad IS in the obstacle set, buffered out by W/2+C;
    #     the free-space boundary sits exactly at the escape-zone edge.
    # If no free-space component intersects the escape zone, the pad's
    # escape lane is too tight at this width.
    epsilon = 1e-4  # mm — sub-µm slack for floating-point edge cases.
    pa_zone = _pad_polygon(pa).buffer(erosion + epsilon, quad_segs=8)
    pb_zone = _pad_polygon(pb).buffer(erosion + epsilon, quad_segs=8)
    pa_comp = pb_comp = None
    for i, c in enumerate(components):
        if pa_comp is None and c.intersects(pa_zone):
            pa_comp = i
        if pb_comp is None and c.intersects(pb_zone):
            pb_comp = i
        if pa_comp is not None and pb_comp is not None:
            break

    if pa_comp is None:
        return {
            "success": True, "reachable": False, "mode": "exact",
            "reason": "from_pad_no_escape",
            "message": (
                f"{from_ref}.{from_pad} has no free-space neighbour on "
                f"{layer} at width {width_mm} mm + clearance "
                f"{round(clearance, 3)} mm (polygon-exact)."
            ),
            "layer": layer, "widthMm": width_mm,
            "clearanceMm": round(clearance, 4),
            "net": own_net,
        }
    if pb_comp is None:
        return {
            "success": True, "reachable": False, "mode": "exact",
            "reason": "to_pad_no_escape",
            "message": (
                f"{to_ref}.{to_pad} has no free-space neighbour on "
                f"{layer} at width {width_mm} mm + clearance "
                f"{round(clearance, 3)} mm (polygon-exact)."
            ),
            "layer": layer, "widthMm": width_mm,
            "clearanceMm": round(clearance, 4),
            "net": own_net,
        }

    if pa_comp == pb_comp:
        return {
            "success": True, "reachable": True, "mode": "exact",
            "reason": None,
            "fromComponent": pa_comp,
            "toComponent": pb_comp,
            "componentCount": len(components),
            "layer": layer, "widthMm": width_mm,
            "clearanceMm": round(clearance, 4),
            "net": own_net,
            "message": (
                f"{from_ref}.{from_pad} → {to_ref}.{to_pad} on {layer} "
                f"at W={width_mm} mm: reachable (polygon-exact). "
                f"{len(components)} free-space component(s) on layer."
            ),
        }
    return {
        "success": True, "reachable": False, "mode": "exact",
        "reason": "different_components",
        "fromComponent": pa_comp,
        "toComponent": pb_comp,
        "componentCount": len(components),
        "layer": layer, "widthMm": width_mm,
        "clearanceMm": round(clearance, 4),
        "net": own_net,
        "message": (
            f"{from_ref}.{from_pad} and {to_ref}.{to_pad} fall in "
            f"different free-space components on {layer} at width "
            f"{width_mm} mm (polygon-exact). The placement, not the "
            "netclass, is the cause."
        ),
    }
