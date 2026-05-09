"""
Schematic Router — Phase 4.

Draws real wires (polylines) between pin endpoints when geometry is friendly,
falling back to net labels via the caller when it isn't. ``route_pair`` tries
straight (0 bends), L-shape (1 bend), U-shape (2 bend), and finally A* on a
1.27 mm grid with symbol-bbox + unrelated-net-wire obstacles.

Phase 4 adds same-net detection: existing wires/labels that already belong
to ``target_net`` are NOT obstacles, and A* may tee into them as a valid
termination (multi-goal). Same-net classification is by label match plus
"connected to one of the caller's own pins".

The spurious-connection guard is the single most important correctness check
and is applied for every Phase.
"""

from __future__ import annotations

import heapq
import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

import sexpdata
from sexpdata import Symbol

logger = logging.getLogger("kicad_interface")

# Tolerance for comparing schematic-grid coordinates (mm). KiCad's default
# wire grid is 1.27 mm, so 1 µm is more than tight enough.
EPS = 1e-3

Point = Tuple[float, float]
Segment = Tuple[Point, Point]


@dataclass
class RouteResult:
    """Outcome of a single pair-routing attempt."""

    success: bool
    segments: List[Segment] = field(default_factory=list)
    reason: str = ""
    style: str = ""  # "straight" (Phase 1); "L"/"U"/"astar" reserved for later phases.


# Default power/ground nets to keep on labels even in auto mode. Power nets
# tend to fan out widely; routing them as wires nearly always loses to labels
# on readability.
DEFAULT_POWER_NETS: FrozenSet[str] = frozenset(
    {
        "VBUS",
        "VCC",
        "VDD",
        "VSS",
        "GND",
        "AGND",
        "DGND",
        "PGND",
        "AVCC",
        "AVSS",
        "+3V3",
        "+3.3V",
        "+5V",
        "+12V",
        "-12V",
        "+24V",
        "+1V8",
        "+1.8V",
    }
)


def is_power_net(name: str, custom: Optional[Sequence[str]] = None) -> bool:
    """True if *name* should default to label routing."""
    if name in DEFAULT_POWER_NETS:
        return True
    if custom and name in custom:
        return True
    # Treat anything starting with '+' or '-' followed by a digit as a rail
    # (covers "+3.3V", "-5V0", etc.).
    if name and name[0] in "+-" and len(name) > 1 and name[1].isdigit():
        return True
    return False


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def _approx(a: float, b: float) -> bool:
    return abs(a - b) < EPS


def _point_on_segment(px: float, py: float, a: Point, b: Point, *, strict: bool = False) -> bool:
    """True if (px, py) lies on the orthogonal segment a-b.

    strict=True excludes both endpoints; strict=False includes them.
    """
    ax, ay = a
    bx, by = b
    if _approx(ay, by):  # horizontal
        if not _approx(py, ay):
            return False
        lo, hi = min(ax, bx), max(ax, bx)
        if strict:
            return lo + EPS < px < hi - EPS
        return lo - EPS <= px <= hi + EPS
    if _approx(ax, bx):  # vertical
        if not _approx(px, ax):
            return False
        lo, hi = min(ay, by), max(ay, by)
        if strict:
            return lo + EPS < py < hi - EPS
        return lo - EPS <= py <= hi + EPS
    return False  # diagonal segments unsupported (we route orthogonally)


def _segments_collinear_overlap(s1: Segment, s2: Segment) -> bool:
    """True if two orthogonal segments lie on the same line and their projections overlap."""
    (a1x, a1y), (b1x, b1y) = s1
    (a2x, a2y), (b2x, b2y) = s2
    # both horizontal at same y
    if _approx(a1y, b1y) and _approx(a2y, b2y) and _approx(a1y, a2y):
        lo1, hi1 = min(a1x, b1x), max(a1x, b1x)
        lo2, hi2 = min(a2x, b2x), max(a2x, b2x)
        return max(lo1, lo2) + EPS < min(hi1, hi2)
    # both vertical at same x
    if _approx(a1x, b1x) and _approx(a2x, b2x) and _approx(a1x, a2x):
        lo1, hi1 = min(a1y, b1y), max(a1y, b1y)
        lo2, hi2 = min(a2y, b2y), max(a2y, b2y)
        return max(lo1, lo2) + EPS < min(hi1, hi2)
    return False


def _segments_strictly_cross(s1: Segment, s2: Segment) -> bool:
    """True iff two orthogonal segments cross at a point strictly interior
    to BOTH segments.

    Used by ``check_spurious_connections`` rule 6 to reject candidate wires
    that would visually pass through an unrelated wire (a perpendicular
    crossing without a junction).  Endpoint touches and collinear overlaps
    return False — those are caught by other rules (T-junction rule 3,
    collinear rule 4) so we don't double-count them here.
    """
    (a1x, a1y), (b1x, b1y) = s1
    (a2x, a2y), (b2x, b2y) = s2

    s1_horiz = _approx(a1y, b1y)
    s1_vert = _approx(a1x, b1x)
    s2_horiz = _approx(a2y, b2y)
    s2_vert = _approx(a2x, b2x)

    # Both must be orthogonal AND in opposite orientations to cross.
    if s1_horiz and s2_vert:
        y0 = a1y
        x0 = a2x
        x_lo, x_hi = sorted((a1x, b1x))
        y_lo, y_hi = sorted((a2y, b2y))
        return x_lo + EPS < x0 < x_hi - EPS and y_lo + EPS < y0 < y_hi - EPS
    if s1_vert and s2_horiz:
        x0 = a1x
        y0 = a2y
        y_lo, y_hi = sorted((a1y, b1y))
        x_lo, x_hi = sorted((a2x, b2x))
        return y_lo + EPS < y0 < y_hi - EPS and x_lo + EPS < x0 < x_hi - EPS
    return False


def _segment_length(s: Segment) -> float:
    (ax, ay), (bx, by) = s
    return math.hypot(bx - ax, by - ay)


def _collinear_and_facing(p1: Point, a1: float, p2: Point, a2: float) -> bool:
    """True iff the two pins lie on a horizontal/vertical line AND their outward
    angles point at each other.

    Pin angles are in lib (Y-up) convention as returned by
    ``PinLocator.get_pin_angle``: 0=right, 90=up, 180=left, 270=down. On the
    Y-down screen, "up" is smaller y and "down" is larger y.
    """
    if _approx(p1[0], p2[0]) and _approx(p1[1], p2[1]):
        return False  # same point
    if _approx(p1[1], p2[1]):  # horizontal
        if a1 not in (0, 180) or a2 not in (0, 180):
            return False
        if p2[0] > p1[0]:
            return a1 == 0 and a2 == 180
        return a1 == 180 and a2 == 0
    if _approx(p1[0], p2[0]):  # vertical
        if a1 not in (90, 270) or a2 not in (90, 270):
            return False
        if p2[1] > p1[1]:  # p2 below p1 on screen
            return a1 == 270 and a2 == 90
        return a1 == 90 and a2 == 270
    return False


# ---------------------------------------------------------------------------
# Phase-2 candidate generators (L-shape, U-shape)
# ---------------------------------------------------------------------------
#
# Pin angle convention (from PinLocator.get_pin_angle, lib Y-up):
#   0 = right   (+x screen)
#   90 = up     (smaller y screen)
#   180 = left  (-x screen)
#   270 = down  (larger y screen)
#
# A wire must EXIT each pin along that pin's outward angle, otherwise it
# would pass through the symbol body. Candidate generators below filter on
# both pins' angles and return only paths whose first segment leaves p1
# along d1 and whose final segment arrives at p2 from along d2.

# Default offset for U-shape "side trips" when both pins face the same way.
# 5.08 mm = 4× the default 1.27 mm wire grid; large enough to clear pin
# stubs but small enough to keep U-shapes compact.
_U_OFFSET = 5.08


def _l_shape_candidates(
    p1: Point, d1: float, p2: Point, d2: float
) -> List[List[Segment]]:
    """Return up to two valid one-bend L-shape paths between *p1* and *p2*.

    Each candidate is a list of two segments meeting at a right-angle corner.
    Empty list if neither orientation is consistent with the pin angles.
    """
    x1, y1 = p1
    x2, y2 = p2
    if _approx(x1, x2) or _approx(y1, y2):
        return []  # collinear → straight or degenerate; not an L

    out: List[List[Segment]] = []

    # Candidate A: corner at (x2, y1) — horizontal then vertical
    #   First seg p1 → corner: direction (sign(x2-x1), 0)
    #   Last seg corner → p2: direction (0, sign(y2-y1)); outward at p2 is opposite
    a_d1 = 0.0 if x2 > x1 else 180.0
    a_d2 = 90.0 if y1 < y2 else 270.0  # screen y-down; corner above p2 → outward up
    if _approx(d1, a_d1) and _approx(d2, a_d2):
        corner: Point = (x2, y1)
        out.append([(p1, corner), (corner, p2)])

    # Candidate B: corner at (x1, y2) — vertical then horizontal
    b_d1 = 270.0 if y2 > y1 else 90.0
    b_d2 = 180.0 if x1 < x2 else 0.0
    if _approx(d1, b_d1) and _approx(d2, b_d2):
        corner = (x1, y2)
        out.append([(p1, corner), (corner, p2)])

    return out


def _u_shape_candidates(
    p1: Point, d1: float, p2: Point, d2: float, *, offset: float = _U_OFFSET
) -> List[List[Segment]]:
    """Return candidate two-bend U-shape paths.

    Two topologies:
      H-V-H (xm bridge): both pins must face horizontal (0 or 180).
      V-H-V (ym bridge): both pins must face vertical (90 or 270).
    """
    x1, y1 = p1
    x2, y2 = p2
    out: List[List[Segment]] = []

    # H-V-H: vertical bridge at x=xm. Both pins horizontal-facing; need y1 != y2.
    if d1 in (0.0, 180.0) and d2 in (0.0, 180.0) and not _approx(y1, y2):
        xm: Optional[float] = None
        if d1 == 0.0 and d2 == 0.0:
            xm = max(x1, x2) + offset
        elif d1 == 180.0 and d2 == 180.0:
            xm = min(x1, x2) - offset
        elif d1 == 0.0 and d2 == 180.0 and x1 < x2:
            xm = (x1 + x2) / 2.0
        elif d1 == 180.0 and d2 == 0.0 and x2 < x1:
            xm = (x1 + x2) / 2.0
        if xm is not None and not _approx(xm, x1) and not _approx(xm, x2):
            c1: Point = (xm, y1)
            c2: Point = (xm, y2)
            out.append([(p1, c1), (c1, c2), (c2, p2)])

    # V-H-V: horizontal bridge at y=ym. Both pins vertical-facing; need x1 != x2.
    if d1 in (90.0, 270.0) and d2 in (90.0, 270.0) and not _approx(x1, x2):
        ym: Optional[float] = None
        if d1 == 90.0 and d2 == 90.0:  # both face up (smaller screen y)
            ym = min(y1, y2) - offset
        elif d1 == 270.0 and d2 == 270.0:
            ym = max(y1, y2) + offset
        elif d1 == 90.0 and d2 == 270.0 and y2 < y1:
            ym = (y1 + y2) / 2.0
        elif d1 == 270.0 and d2 == 90.0 and y1 < y2:
            ym = (y1 + y2) / 2.0
        if ym is not None and not _approx(ym, y1) and not _approx(ym, y2):
            c1 = (x1, ym)
            c2 = (x2, ym)
            out.append([(p1, c1), (c1, c2), (c2, p2)])

    return out


def _path_length(segments: Sequence[Segment]) -> float:
    return sum(_segment_length(s) for s in segments)


# ---------------------------------------------------------------------------
# Phase-3 A* over a 1.27 mm grid
# ---------------------------------------------------------------------------

_GRID = 1.27  # mm; KiCad's default schematic wire grid

# Direction codes — kept consistent with PinLocator's lib-Y-up convention:
#   0 = E (right, +x)
#   1 = S (down screen, +y)
#   2 = W (left, -x)
#   3 = N (up screen, -y)
_DIR_VECTORS: Tuple[Tuple[int, int], ...] = ((1, 0), (0, 1), (-1, 0), (0, -1))


def _angle_to_dir(deg: float) -> int:
    """Map PinLocator's outward angle (0/90/180/270) to direction code."""
    d = round(deg) % 360
    return {0: 0, 90: 3, 180: 2, 270: 1}.get(d, 0)


def _opposite_dir(d: int) -> int:
    return (d + 2) % 4


@dataclass(frozen=True)
class CostModel:
    """Per-step costs for A*. Corners cost a lot more than length so the
    search prefers fewer bends; crossings sit between."""

    straight: float = 1.0
    corner: float = 5.0
    crossing: float = 3.0


def _normalize_edge(
    a: Tuple[int, int], b: Tuple[int, int]
) -> Tuple[Tuple[int, int], Tuple[int, int]]:
    """Edge identity is undirected, so canonicalise endpoint order."""
    return (a, b) if a <= b else (b, a)


@dataclass
class GridObstacles:
    """Precomputed obstacle bitmap for A* search.

    Cells in *blocked_cells* may not be entered (except start/goal exemptions
    handled by the caller). Edges in *forbidden_edges* may not be traversed —
    these are edges that overlap an unrelated-net wire (collinear use would
    short two nets together).
    """

    blocked_cells: Set[Tuple[int, int]] = field(default_factory=set)
    forbidden_edges: Set[Tuple[Tuple[int, int], Tuple[int, int]]] = field(
        default_factory=set
    )


def _world_to_cell(
    pt: Point, origin: Point, snap: float = _GRID
) -> Tuple[int, int]:
    return (
        int(round((pt[0] - origin[0]) / snap)),
        int(round((pt[1] - origin[1]) / snap)),
    )


def _cell_to_world(
    cell: Tuple[int, int], origin: Point, snap: float = _GRID
) -> Point:
    return (origin[0] + cell[0] * snap, origin[1] + cell[1] * snap)


def _on_grid(p: Point, origin: Point, snap: float = _GRID) -> bool:
    """True iff *p* lies on the snap-grid anchored at *origin*."""
    return (
        abs(((p[0] - origin[0]) / snap) - round((p[0] - origin[0]) / snap)) < 1e-3
        and abs(((p[1] - origin[1]) / snap) - round((p[1] - origin[1]) / snap)) < 1e-3
    )


def _build_grid_obstacles(
    schematic_path: Path,
    p1: Point,
    p2: Point,
    target_net: str,
    obstacles: Obstacles,
    *,
    origin: Point,
    snap: float = _GRID,
    pad_cells: int = 25,
    own_refs: Tuple[str, str] = ("", ""),
    same_net_wire_indices: Optional[Set[int]] = None,
) -> Tuple[GridObstacles, Tuple[int, int], Tuple[int, int]]:
    """Build a `GridObstacles` covering the bbox of (p1, p2) plus *pad_cells*
    of margin in each direction.

    Returns (obstacles, bounds_min_cell, bounds_max_cell) where bounds are
    inclusive integer cell coordinates.
    """
    cell_p1 = _world_to_cell(p1, origin, snap)
    cell_p2 = _world_to_cell(p2, origin, snap)
    min_x = min(cell_p1[0], cell_p2[0]) - pad_cells
    max_x = max(cell_p1[0], cell_p2[0]) + pad_cells
    min_y = min(cell_p1[1], cell_p2[1]) - pad_cells
    max_y = max(cell_p1[1], cell_p2[1]) + pad_cells

    grid = GridObstacles()

    # 1) Symbol bodies — block all cells inside any other symbol's bbox.
    try:
        from commands.schematic_analysis import (
            _compute_symbol_bbox_direct,
            _extract_lib_symbols,
            _load_sexp,
            _parse_symbols,
        )
    except Exception as e:
        logger.warning(f"_build_grid_obstacles: bbox helpers unavailable: {e}")
    else:
        try:
            sexp_data = _load_sexp(schematic_path)
            lib_defs = _extract_lib_symbols(sexp_data)
            for sym in _parse_symbols(sexp_data):
                ref = sym.get("reference", "")
                if not ref or ref.startswith("_TEMPLATE"):
                    continue
                if ref in own_refs:
                    continue  # own symbols' bodies aren't obstacles for their own pins
                lib_data = lib_defs.get(sym.get("lib_id", ""), {})
                pin_defs = lib_data.get("pins", {})
                graphics_points = lib_data.get("graphics_points", [])
                if not pin_defs:
                    continue
                bbox = _compute_symbol_bbox_direct(
                    sym, pin_defs, graphics_points=graphics_points
                )
                if bbox is None:
                    continue
                bx1, by1, bx2, by2 = bbox
                # Convert bbox to cell range, inclusive.
                cx1 = int(math.floor((bx1 - origin[0]) / snap))
                cx2 = int(math.ceil((bx2 - origin[0]) / snap))
                cy1 = int(math.floor((by1 - origin[1]) / snap))
                cy2 = int(math.ceil((by2 - origin[1]) / snap))
                for ix in range(max(cx1, min_x), min(cx2, max_x) + 1):
                    for iy in range(max(cy1, min_y), min(cy2, max_y) + 1):
                        grid.blocked_cells.add((ix, iy))
        except Exception as e:
            logger.warning(f"_build_grid_obstacles: bbox pass failed: {e}")

    # 2) Other-component pin endpoints — block their cell.
    for pin_pt in obstacles.other_pins:
        cell = _world_to_cell(pin_pt, origin, snap)
        if min_x <= cell[0] <= max_x and min_y <= cell[1] <= max_y:
            grid.blocked_cells.add(cell)

    # 3) Other-net labels — block the cell. Same-net labels are fine.
    for (lpos, lname) in obstacles.other_labels:
        if lname == target_net:
            continue
        cell = _world_to_cell(lpos, origin, snap)
        if min_x <= cell[0] <= max_x and min_y <= cell[1] <= max_y:
            grid.blocked_cells.add(cell)

    # 4) Existing wires — every grid edge that overlaps an *unrelated* wire is
    # forbidden (collinear overlap would short two nets together). Same-net
    # wires (Phase 4) skip this rule because tee/overlap with them is the
    # desired join behaviour.
    same_net_set = same_net_wire_indices or set()
    for idx, ((wx1, wy1), (wx2, wy2)) in enumerate(obstacles.other_wires):
        if idx in same_net_set:
            continue
        wp1 = _world_to_cell((wx1, wy1), origin, snap)
        wp2 = _world_to_cell((wx2, wy2), origin, snap)
        if wp1 == wp2:
            continue
        if wp1[0] == wp2[0]:  # vertical
            lo, hi = min(wp1[1], wp2[1]), max(wp1[1], wp2[1])
            for iy in range(lo, hi):
                a = (wp1[0], iy)
                b = (wp1[0], iy + 1)
                grid.forbidden_edges.add(_normalize_edge(a, b))
        elif wp1[1] == wp2[1]:  # horizontal
            lo, hi = min(wp1[0], wp2[0]), max(wp1[0], wp2[0])
            for ix in range(lo, hi):
                a = (ix, wp1[1])
                b = (ix + 1, wp1[1])
                grid.forbidden_edges.add(_normalize_edge(a, b))

    # The two pins we are intentionally connecting must be valid endpoints,
    # so make sure they are not in blocked_cells. (They might have been added
    # via the other_pins or symbol-bbox passes if the caller didn't exclude
    # them.) Re-allow them.
    grid.blocked_cells.discard(cell_p1)
    grid.blocked_cells.discard(cell_p2)

    return grid, (min_x, min_y), (max_x, max_y)


def _wire_reachable_cells_from_start(
    obstacles: Obstacles,
    same_net_indices: Set[int],
    *,
    origin: Point,
    snap: float,
    bounds_min: Tuple[int, int],
    bounds_max: Tuple[int, int],
) -> Set[Tuple[int, int]]:
    """Cells reachable from start cell (0, 0) (= p1) by walking the
    same-net wire graph: collect every wire that touches any cell in
    the current frontier and union its cells in.  Used to filter
    `extra_goal_cells` so A* doesn't "tee" onto a wire that's already
    connected to p1.  Out-of-bounds cells are still walked through but
    not returned.
    """
    def _wire_cells(idx: int) -> Set[Tuple[int, int]]:
        a, b = obstacles.other_wires[idx]
        a_cell = _world_to_cell(a, origin, snap)
        b_cell = _world_to_cell(b, origin, snap)
        cells: Set[Tuple[int, int]] = set()
        if a_cell == b_cell:
            cells.add(a_cell)
        elif a_cell[0] == b_cell[0]:
            lo, hi = min(a_cell[1], b_cell[1]), max(a_cell[1], b_cell[1])
            cells.update((a_cell[0], iy) for iy in range(lo, hi + 1))
        elif a_cell[1] == b_cell[1]:
            lo, hi = min(a_cell[0], b_cell[0]), max(a_cell[0], b_cell[0])
            cells.update((ix, a_cell[1]) for ix in range(lo, hi + 1))
        return cells

    wire_cell_sets = [_wire_cells(i) for i in same_net_indices]
    reachable: Set[Tuple[int, int]] = {(0, 0)}
    changed = True
    while changed:
        changed = False
        for wcs in wire_cell_sets:
            if wcs and (wcs & reachable) and not (wcs <= reachable):
                reachable |= wcs
                changed = True

    in_bounds: Set[Tuple[int, int]] = set()
    for cell in reachable:
        if cell == (0, 0):
            continue
        if (
            bounds_min[0] <= cell[0] <= bounds_max[0]
            and bounds_min[1] <= cell[1] <= bounds_max[1]
        ):
            in_bounds.add(cell)
    return in_bounds


def _same_net_cells_in_bounds(
    obstacles: Obstacles,
    same_net_indices: Set[int],
    target_net: str,
    *,
    origin: Point,
    snap: float,
    bounds_min: Tuple[int, int],
    bounds_max: Tuple[int, int],
) -> Set[Tuple[int, int]]:
    """Compute the set of grid cells covered by same-net wires/labels.

    Used by A* as ``extra_goal_cells``: when the search reaches one of these
    cells, it terminates with a tee-style join into the existing same-net
    geometry. Out-of-bounds cells are dropped.
    """
    out: Set[Tuple[int, int]] = set()

    def _add_if_in_bounds(cell: Tuple[int, int]) -> None:
        if (
            bounds_min[0] <= cell[0] <= bounds_max[0]
            and bounds_min[1] <= cell[1] <= bounds_max[1]
        ):
            out.add(cell)

    for idx in same_net_indices:
        a, b = obstacles.other_wires[idx]
        a_cell = _world_to_cell(a, origin, snap)
        b_cell = _world_to_cell(b, origin, snap)
        if a_cell == b_cell:
            _add_if_in_bounds(a_cell)
            continue
        if a_cell[0] == b_cell[0]:  # vertical
            lo, hi = min(a_cell[1], b_cell[1]), max(a_cell[1], b_cell[1])
            for iy in range(lo, hi + 1):
                _add_if_in_bounds((a_cell[0], iy))
        elif a_cell[1] == b_cell[1]:  # horizontal
            lo, hi = min(a_cell[0], b_cell[0]), max(a_cell[0], b_cell[0])
            for ix in range(lo, hi + 1):
                _add_if_in_bounds((ix, a_cell[1]))
        # diagonal (extremely unusual in KiCad): skip
    # Same-net labels are also valid termination cells.
    for (lpos, lname) in obstacles.other_labels:
        if lname == target_net:
            _add_if_in_bounds(_world_to_cell(lpos, origin, snap))
    return out


def _astar_search(
    start: Tuple[int, int],
    initial_dir: int,
    goal: Tuple[int, int],
    final_dir: int,
    grid: GridObstacles,
    bounds_min: Tuple[int, int],
    bounds_max: Tuple[int, int],
    *,
    cost_model: CostModel,
    max_steps: int,
    extra_goal_cells: Optional[Set[Tuple[int, int]]] = None,
) -> Optional[List[Tuple[int, int]]]:
    """A* on a 4-neighbour grid with corner penalty.

    *initial_dir* forces the first step's direction (the wire must leave
    *start* along its outward angle). *final_dir* requires the path approach
    *goal* from cell goal+DIR_VECTORS[final_dir], i.e. the predecessor of
    *goal* lies in the *final_dir* outward direction.

    *extra_goal_cells* (Phase 4) are alternative termination cells — typically
    cells already covered by a same-net wire/label. Reaching any of them
    terminates the search (no direction constraint); the resulting path tees
    into the existing same-net geometry.

    Returns the cell path (start, ..., goal) or None if unreachable within
    *max_steps* total grid steps.
    """
    if start == goal:
        return [start]

    extra_goals: Set[Tuple[int, int]] = (
        set(extra_goal_cells) if extra_goal_cells else set()
    )
    # The start cell must never be a "tee target" — we'd produce a zero-length
    # path, and start is already on the same-net set in that scenario.
    extra_goals.discard(start)

    target_last_dir = _opposite_dir(final_dir)
    fdx, fdy = _DIR_VECTORS[final_dir]
    required_predecessor = (goal[0] + fdx, goal[1] + fdy)

    # Force the first step.
    sdx, sdy = _DIR_VECTORS[initial_dir]
    first_step = (start[0] + sdx, start[1] + sdy)
    if not (
        bounds_min[0] <= first_step[0] <= bounds_max[0]
        and bounds_min[1] <= first_step[1] <= bounds_max[1]
    ):
        return None
    if first_step in grid.blocked_cells and first_step != goal:
        return None
    first_edge = _normalize_edge(start, first_step)
    if first_edge in grid.forbidden_edges:
        return None

    # State: (cell, last_dir).  We seed with the cell after the initial step.
    State = Tuple[Tuple[int, int], int]
    counter = 0
    open_heap: List[Tuple[float, int, Tuple[int, int], int]] = []
    came_from: Dict[State, State] = {}
    g_score: Dict[State, float] = {}

    init_state: State = (first_step, initial_dir)
    g_score[init_state] = cost_model.straight
    h0 = (
        abs(first_step[0] - goal[0]) + abs(first_step[1] - goal[1])
    ) * cost_model.straight
    heapq.heappush(
        open_heap,
        (cost_model.straight + h0, cost_model.straight, counter, first_step, initial_dir),
    )
    counter += 1

    # If the first step lands us on the goal in the right approach direction,
    # we're done.
    if first_step == goal and initial_dir == target_last_dir:
        return [start, goal]

    # If the first step lands us on a tee target, terminate there.
    if first_step in extra_goals:
        return [start, first_step]

    max_g = max_steps * (cost_model.straight + cost_model.corner)

    while open_heap:
        _f, popped_g, _ctr, cell, last_dir = heapq.heappop(open_heap)
        state: State = (cell, last_dir)
        # Stale entry: a better path to this state was found after this one
        # was pushed — skip it.
        if popped_g > g_score.get(state, math.inf) + EPS:
            continue

        is_pin_goal = cell == goal and last_dir == target_last_dir
        is_tee_goal = cell in extra_goals
        if is_pin_goal or is_tee_goal:
            # reconstruct path
            cells = [cell]
            cur = state
            while cur in came_from:
                cur = came_from[cur]
                cells.append(cur[0])
            cells.append(start)
            cells.reverse()
            return cells

        # If we are at goal but in the wrong final direction, do not stop here;
        # we must approach from the correct cell.

        for new_dir in range(4):
            if new_dir == _opposite_dir(last_dir):
                continue  # no 180° reversals
            ddx, ddy = _DIR_VECTORS[new_dir]
            nb = (cell[0] + ddx, cell[1] + ddy)
            if not (
                bounds_min[0] <= nb[0] <= bounds_max[0]
                and bounds_min[1] <= nb[1] <= bounds_max[1]
            ):
                continue
            # Allow stepping ONTO the goal cell or any tee target even if it
            # is technically inside a (typically own) symbol's bbox.
            if nb in grid.blocked_cells and nb != goal and nb not in extra_goals:
                continue

            # Stepping onto goal is only allowed when arriving from the
            # required predecessor cell in the required direction. Tee
            # targets accept any approach direction.
            if nb == goal and nb not in extra_goals:
                if cell != required_predecessor or new_dir != target_last_dir:
                    continue

            edge = _normalize_edge(cell, nb)
            if edge in grid.forbidden_edges:
                continue

            step_cost = cost_model.straight
            if new_dir != last_dir:
                step_cost += cost_model.corner

            tentative_g = g_score[state] + step_cost
            if tentative_g > max_g:
                continue
            new_state: State = (nb, new_dir)
            if tentative_g < g_score.get(new_state, math.inf) - EPS:
                g_score[new_state] = tentative_g
                came_from[new_state] = state
                h = (
                    abs(nb[0] - goal[0]) + abs(nb[1] - goal[1])
                ) * cost_model.straight
                heapq.heappush(
                    open_heap,
                    (tentative_g + h, tentative_g, counter, nb, new_dir),
                )
                counter += 1

    return None


def _cells_to_segments(
    cells: List[Tuple[int, int]],
    p1: Point,
    p2: Point,
    origin: Point,
    snap: float = _GRID,
) -> List[Segment]:
    """Compress a cell-by-cell A* path into orthogonal segments and substitute
    the exact world endpoints for *p1* and *p2* (avoids end-point snap drift)."""
    if len(cells) < 2:
        return []
    segments: List[Segment] = []
    seg_start_cell = cells[0]
    prev_dir: Optional[Tuple[int, int]] = None
    for i in range(1, len(cells)):
        c0, c1 = cells[i - 1], cells[i]
        cur = (c1[0] - c0[0], c1[1] - c0[1])
        if prev_dir is not None and cur != prev_dir:
            segments.append((seg_start_cell, c0))
            seg_start_cell = c0
        prev_dir = cur
    segments.append((seg_start_cell, cells[-1]))

    # Convert to world coords; first segment starts at p1, last ends at p2.
    out: List[Segment] = []
    for idx, (a, b) in enumerate(segments):
        wa = p1 if idx == 0 else _cell_to_world(a, origin, snap)
        wb = p2 if idx == len(segments) - 1 else _cell_to_world(b, origin, snap)
        out.append((wa, wb))
    return out


# ---------------------------------------------------------------------------
# Obstacle collection
# ---------------------------------------------------------------------------


# Pre-interned sexpdata symbols (matching wire_manager.py's pattern).
_SYM_WIRE = Symbol("wire")
_SYM_LABEL = Symbol("label")
_SYM_GLOBAL_LABEL = Symbol("global_label")
_SYM_HIER_LABEL = Symbol("hierarchical_label")
_SYM_PTS = Symbol("pts")
_SYM_XY = Symbol("xy")
_SYM_AT = Symbol("at")


@dataclass
class Obstacles:
    other_pins: List[Point]  # pin endpoints we must NOT touch
    other_labels: List[Tuple[Point, str]]  # (pos, net_name) for every label
    other_wires: List[Segment]  # every existing wire on the sheet
    # Symbol body bounding boxes for every placed (non-template) symbol on
    # the sheet. Each entry is ((min_x, min_y, max_x, max_y), reference). The
    # caller's own symbols are filtered out at guard time by checking which
    # bboxes contain an `own_endpoint`.
    other_bboxes: List[Tuple[Tuple[float, float, float, float], str]] = field(
        default_factory=list
    )


def _collect_pin_endpoints(
    schematic_path: Path, exclude: Set[Tuple[str, str]]
) -> List[Point]:
    """Return endpoints of every pin on every component, except those in *exclude*.

    *exclude* is a set of (ref, pin_number_string) tuples — typically the two
    pins we are intentionally connecting.
    """
    # Lazy import to avoid a circular dep at module load time.
    from commands.pin_locator import PinLocator
    from skip import Schematic

    locator = PinLocator()
    out: List[Point] = []
    try:
        sch = Schematic(str(schematic_path))
    except Exception as e:
        logger.warning(f"_collect_pin_endpoints: could not load {schematic_path}: {e}")
        return out

    for symbol in getattr(sch, "symbol", []):
        if not hasattr(symbol.property, "Reference"):
            continue
        ref = symbol.property.Reference.value.rstrip("_")
        if ref.startswith("_TEMPLATE"):
            continue
        try:
            pins = locator.get_all_symbol_pins(schematic_path, ref)
        except Exception:
            continue
        for pin_num, coords in (pins or {}).items():
            if (ref, str(pin_num)) in exclude:
                continue
            out.append((float(coords[0]), float(coords[1])))
    return out


def _collect_labels(schematic_path: Path) -> List[Tuple[Point, str]]:
    """Return (position, net_name) for every (local|global|hierarchical) label."""
    out: List[Tuple[Point, str]] = []
    try:
        with open(schematic_path, "r", encoding="utf-8") as f:
            sexp = sexpdata.loads(f.read())
    except Exception as e:
        logger.warning(f"_collect_labels: could not parse {schematic_path}: {e}")
        return out
    label_syms = (_SYM_LABEL, _SYM_GLOBAL_LABEL, _SYM_HIER_LABEL)
    for item in sexp:
        if not (isinstance(item, list) and item and item[0] in label_syms):
            continue
        # (label "TEXT" (at x y rot) ...)
        text = ""
        pos: Optional[Point] = None
        if len(item) >= 2 and isinstance(item[1], str):
            text = item[1].strip('"')
        for part in item[1:]:
            if isinstance(part, list) and part and part[0] == _SYM_AT and len(part) >= 3:
                pos = (float(part[1]), float(part[2]))
                break
        if pos is not None and text:
            out.append((pos, text))
    return out


def _collect_wires(schematic_path: Path) -> List[Segment]:
    """Return ((x1,y1),(x2,y2)) for every (wire ...) on the sheet."""
    # Reuse WireManager._parse_wire to avoid duplicating logic.
    from commands.wire_manager import WireManager

    out: List[Segment] = []
    try:
        with open(schematic_path, "r", encoding="utf-8") as f:
            sexp = sexpdata.loads(f.read())
    except Exception as e:
        logger.warning(f"_collect_wires: could not parse {schematic_path}: {e}")
        return out
    for item in sexp:
        parsed = WireManager._parse_wire(item)
        if parsed is None:
            continue
        (x1, y1), (x2, y2), _, _ = parsed
        out.append(((float(x1), float(y1)), (float(x2), float(y2))))
    return out


def _collect_bboxes(
    schematic_path: Path,
) -> List[Tuple[Tuple[float, float, float, float], str]]:
    """Return [(bbox, ref), ...] for every non-template placed symbol."""
    out: List[Tuple[Tuple[float, float, float, float], str]] = []
    try:
        from commands.schematic_analysis import (
            _compute_symbol_bbox_direct,
            _extract_lib_symbols,
            _load_sexp,
            _parse_symbols,
        )
    except Exception as e:
        logger.warning(f"_collect_bboxes: helpers unavailable: {e}")
        return out
    try:
        sexp_data = _load_sexp(schematic_path)
        lib_defs = _extract_lib_symbols(sexp_data)
        for sym in _parse_symbols(sexp_data):
            ref = sym.get("reference", "")
            if not ref or ref.startswith("_TEMPLATE"):
                continue
            lib_data = lib_defs.get(sym.get("lib_id", ""), {})
            pin_defs = lib_data.get("pins", {})
            graphics_points = lib_data.get("graphics_points", [])
            if not pin_defs:
                continue
            bbox = _compute_symbol_bbox_direct(
                sym, pin_defs, graphics_points=graphics_points
            )
            if bbox is not None:
                out.append((bbox, ref))
    except Exception as e:
        logger.warning(f"_collect_bboxes: parse failed: {e}")
    return out


def collect_obstacles(
    schematic_path: Path, exclude_pins: Set[Tuple[str, str]]
) -> Obstacles:
    return Obstacles(
        other_pins=_collect_pin_endpoints(schematic_path, exclude_pins),
        other_labels=_collect_labels(schematic_path),
        other_wires=_collect_wires(schematic_path),
        other_bboxes=_collect_bboxes(schematic_path),
    )


# ---------------------------------------------------------------------------
# Same-net wire classification (Phase 4)
# ---------------------------------------------------------------------------


def _classify_wires_by_net(
    obstacles: Obstacles,
    target_net: str,
    *,
    own_pin_endpoints: Sequence[Point] = (),
) -> Set[int]:
    """Return indices into ``obstacles.other_wires`` that belong to *target_net*.

    A wire is on target_net if its connected component contains either:
      - a label whose text equals *target_net*, or
      - any point in *own_pin_endpoints* (the caller's pins are by definition
        on target_net, so any wire already connected to one of them is too).

    Adjacency includes T-junctions: a wire endpoint lying on another wire's
    interior counts as a shared point.
    """
    wires = obstacles.other_wires
    if not wires:
        return set()

    def _key(pt: Point) -> Tuple[int, int]:
        # 10-micron resolution — well below KiCad's smallest snap.
        return (round(pt[0] * 100), round(pt[1] * 100))

    pt_to_wires: Dict[Tuple[int, int], Set[int]] = {}
    for i, (a, b) in enumerate(wires):
        pt_to_wires.setdefault(_key(a), set()).add(i)
        pt_to_wires.setdefault(_key(b), set()).add(i)

    # T-junctions: include endpoints landing on another wire's interior.
    seen_keys = list(pt_to_wires.keys())
    for i, (a, b) in enumerate(wires):
        ka, kb = _key(a), _key(b)
        for ek in seen_keys:
            if ek == ka or ek == kb:
                continue
            ex = ek[0] / 100.0
            ey = ek[1] / 100.0
            if _point_on_segment(ex, ey, a, b, strict=True):
                pt_to_wires[ek].add(i)

    # Union-find over wire indices.
    parent = list(range(len(wires)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    for wire_set in pt_to_wires.values():
        wlist = list(wire_set)
        for j in range(1, len(wlist)):
            union(wlist[0], wlist[j])

    same_net: Set[int] = set()

    def _mark_component(seed: int) -> None:
        root = find(seed)
        for j in range(len(wires)):
            if find(j) == root:
                same_net.add(j)

    def _wire_touches(pt: Point, a: Point, b: Point) -> bool:
        if _approx(pt[0], a[0]) and _approx(pt[1], a[1]):
            return True
        if _approx(pt[0], b[0]) and _approx(pt[1], b[1]):
            return True
        return _point_on_segment(pt[0], pt[1], a, b, strict=False)

    # Mark by target_net label coincidence.
    for (lpos, lname) in obstacles.other_labels:
        if lname != target_net:
            continue
        for i, (a, b) in enumerate(wires):
            if _wire_touches(lpos, a, b):
                _mark_component(i)
                break

    # Mark by own_pin_endpoint coincidence.
    for ep in own_pin_endpoints:
        for i, (a, b) in enumerate(wires):
            if _wire_touches(ep, a, b):
                _mark_component(i)
                break

    return same_net


# ---------------------------------------------------------------------------
# Spurious-connection guard
# ---------------------------------------------------------------------------


def _bbox_strict_intersects(
    seg: Segment, bbox: Tuple[float, float, float, float]
) -> bool:
    """True iff *seg* enters the open interior of *bbox*.

    A segment that merely touches the bbox boundary at an endpoint (own pin
    landing on a bbox edge) does not count as entering. We use a small
    epsilon shrink so a path running ALONG a bbox edge isn't flagged.
    """
    (ax, ay), (bx, by) = seg
    bx1, by1, bx2, by2 = bbox
    shrink = EPS * 10
    bx1 += shrink
    by1 += shrink
    bx2 -= shrink
    by2 -= shrink
    if bx2 <= bx1 or by2 <= by1:
        return False
    # Liang-Barsky against the (slightly shrunk) rect.
    dx = bx - ax
    dy = by - ay
    p = [-dx, dx, -dy, dy]
    q = [ax - bx1, bx2 - ax, ay - by1, by2 - ay]
    t_min = 0.0
    t_max = 1.0
    for i in range(4):
        if abs(p[i]) < EPS:
            if q[i] < 0:
                return False
        else:
            t = q[i] / p[i]
            if p[i] < 0:
                t_min = max(t_min, t)
            else:
                t_max = min(t_max, t)
            if t_min > t_max:
                return False
    return True


def check_spurious_connections(
    segments: List[Segment],
    obstacles: Obstacles,
    target_net: str,
    *,
    own_endpoints: Sequence[Point] = (),
    same_net_wire_indices: Optional[Set[int]] = None,
) -> Optional[str]:
    """Return None if *segments* are safe to add; else a short reason string.

    *own_endpoints* are the pin endpoints we are intentionally connecting; they
    are exempt from the "no pin on segment" rule (a wire MUST touch them).
    Bboxes containing an own_endpoint are treated as own and exempt from
    the body-crossing rule.

    *same_net_wire_indices* (Phase 4) marks indices into
    ``obstacles.other_wires`` that already belong to *target_net*. Tee
    junctions and collinear overlaps with these wires are intended (we ARE
    joining the same net), not spurious, so rules 3 and 4 skip them.
    """

    def _is_own(pt: Point) -> bool:
        return any(_approx(pt[0], q[0]) and _approx(pt[1], q[1]) for q in own_endpoints)

    def _bbox_contains_endpoint(bbox: Tuple[float, float, float, float]) -> bool:
        bx1, by1, bx2, by2 = bbox
        for ex, ey in own_endpoints:
            if bx1 - EPS <= ex <= bx2 + EPS and by1 - EPS <= ey <= by2 + EPS:
                return True
        return False

    same_net_wires = same_net_wire_indices or set()

    for seg in segments:
        a, b = seg
        # 1. No other-component pin endpoint may lie on the segment (interior or endpoint).
        for opin in obstacles.other_pins:
            if _is_own(opin):
                continue
            if _point_on_segment(opin[0], opin[1], a, b, strict=False):
                return f"pin at ({opin[0]:.2f},{opin[1]:.2f}) lies on candidate wire"

        # 2. No other-net label may be on the segment (would change net membership).
        for (lpos, lname) in obstacles.other_labels:
            if lname == target_net:
                continue
            if _point_on_segment(lpos[0], lpos[1], a, b, strict=False):
                return (
                    f"label '{lname}' at ({lpos[0]:.2f},{lpos[1]:.2f}) "
                    "would attach to candidate wire"
                )

        # 3. No existing wire endpoint may lie strictly on our segment interior
        #    (that would create a T-junction with an unrelated net).
        for idx, (we1, we2) in enumerate(obstacles.other_wires):
            if idx in same_net_wires:
                continue  # tee with same-net is intended, not spurious
            for we in (we1, we2):
                if _is_own(we):
                    continue
                if _point_on_segment(we[0], we[1], a, b, strict=True):
                    return (
                        f"existing wire endpoint ({we[0]:.2f},{we[1]:.2f}) "
                        "lies on candidate wire interior"
                    )

        # 4. No collinear overlap with an existing wire (would visually merge / electrically
        #    collide with another net).
        for idx, ow in enumerate(obstacles.other_wires):
            if idx in same_net_wires:
                continue
            if _segments_collinear_overlap(seg, ow):
                return "candidate wire collinearly overlaps an existing wire"

        # 5. No segment may pass through the body interior of an unrelated
        #    symbol — visually misleading even if not electrically incorrect.
        for (bbox, ref) in obstacles.other_bboxes:
            if _bbox_contains_endpoint(bbox):
                continue  # own symbol — wires are allowed to leave it
            if _bbox_strict_intersects(seg, bbox):
                return f"candidate wire crosses symbol {ref} body"

        # 6. No segment may strictly cross an unrelated wire (perpendicular
        #    crossing without a junction).  Such crossings are electrically
        #    valid in KiCad — wires only connect when a junction or coincident
        #    endpoint is present — but they're hard for a human to read.
        #    Rule 3 (T-junction) and 4 (collinear overlap) already cover the
        #    same-axis cases; this rule covers the perpendicular case.
        for idx, ow in enumerate(obstacles.other_wires):
            if idx in same_net_wires:
                continue  # crossing same-net is intended (a tee/junction goes here)
            if _segments_strictly_cross(seg, ow):
                return "candidate wire perpendicularly crosses unrelated wire"

    return None


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


class SchematicRouter:
    """Phase-4 router: straight, L-shape, U-shape, and A* candidates with the
    spurious-connection guard applied to every shape; same-net wires/labels
    can be tee'd into via multi-goal A*."""

    @staticmethod
    def route_pair(
        schematic_path: Path,
        ref1: str,
        pin1: str,
        ref2: str,
        pin2: str,
        target_net: str,
        *,
        max_len: float = 80.0,
        max_bends: int = 4,
        obstacles: Optional[Obstacles] = None,
        cost_model: Optional[CostModel] = None,
        extra_own_endpoints: Sequence[Point] = (),
    ) -> RouteResult:
        """Try to route a polyline between two pins.

        Tries straight (0 bends) → L-shapes (1 bend) → U-shapes (2 bends) →
        A* on the 1.27 mm grid with bbox + unrelated-wire obstacle avoidance.
        The first candidate within ``max_len``/``max_bends`` that passes
        ``check_spurious_connections`` wins.
        """
        from commands.connection_schematic import ConnectionManager

        locator = ConnectionManager.get_pin_locator()
        if locator is None:
            return RouteResult(False, [], "pin_locator_unavailable")

        p1_list = locator.get_pin_location(schematic_path, ref1, pin1)
        p2_list = locator.get_pin_location(schematic_path, ref2, pin2)
        if p1_list is None or p2_list is None:
            return RouteResult(False, [], "pin_location_unavailable")
        p1: Point = (float(p1_list[0]), float(p1_list[1]))
        p2: Point = (float(p2_list[0]), float(p2_list[1]))

        a1 = locator.get_pin_angle(schematic_path, ref1, pin1)
        a2 = locator.get_pin_angle(schematic_path, ref2, pin2)
        if a1 is None or a2 is None:
            return RouteResult(False, [], "pin_angle_unavailable")

        if obstacles is None:
            obstacles = collect_obstacles(
                schematic_path,
                exclude_pins={(ref1, str(pin1)), (ref2, str(pin2))},
            )

        own_endpoints = (p1, p2)
        # Phase 4 — figure out which existing wires already belong to
        # target_net. Pass own pin endpoints (the two we're connecting plus
        # any extras the caller provided, e.g. other pins in the same
        # connect_pins call) so a wire dropped between previous pairs is
        # recognised even without a label.
        all_own_endpoints = (p1, p2, *extra_own_endpoints)
        same_net_wires = _classify_wires_by_net(
            obstacles, target_net, own_pin_endpoints=all_own_endpoints
        )

        last_reject: str = ""

        def _try_shapes() -> Optional[RouteResult]:
            """Try straight / L / U candidates in order; return success or None."""
            nonlocal last_reject
            attempts: List[Tuple[str, List[List[Segment]]]] = []
            if _collinear_and_facing(p1, a1, p2, a2):
                attempts.append(("straight", [[(p1, p2)]]))
            attempts.append(("L", _l_shape_candidates(p1, a1, p2, a2)))
            attempts.append(("U", _u_shape_candidates(p1, a1, p2, a2)))
            for style_label, candidates in attempts:
                for path in candidates:
                    if not path:
                        continue
                    if (len(path) - 1) > max_bends:
                        last_reject = "over_max_bends"
                        continue
                    if _path_length(path) > max_len + EPS:
                        last_reject = "over_max_len"
                        continue
                    bad = check_spurious_connections(
                        path,
                        obstacles,
                        target_net,
                        own_endpoints=own_endpoints,
                        same_net_wire_indices=same_net_wires,
                    )
                    if bad is None:
                        return RouteResult(True, path, "", style=style_label)
                    last_reject = f"spurious:{bad}"
            return None

        # When there are no same-net opportunities, shapes give a clean direct
        # route. When same-net wires exist, A* may find a much shorter tee
        # than any shape; try A* first in that case so we don't accidentally
        # accept a shape that just runs along an existing wire.
        if not same_net_wires:
            shape_result = _try_shapes()
            if shape_result is not None:
                return shape_result

        # A* fallback. Requires both pins on the same 1.27 mm grid relative
        # to p1; if not, defer to label fallback (or shape fallback when
        # same_net_wires is set and we haven't tried shapes yet).
        if not _on_grid(p2, origin=p1):
            if same_net_wires:
                # Hadn't tried shapes yet — try them as a last resort.
                shape_result = _try_shapes()
                if shape_result is not None:
                    return shape_result
            return RouteResult(False, [], last_reject or "no_candidate_path")

        cm = cost_model or CostModel()
        max_steps = int(math.floor(max_len / _GRID))
        grid_obs, bmin, bmax = _build_grid_obstacles(
            schematic_path,
            p1,
            p2,
            target_net,
            obstacles,
            origin=p1,
            own_refs=(ref1, ref2),
            same_net_wire_indices=same_net_wires,
        )
        # Phase 4 — same-net cells are valid tee targets. Drop the goal
        # cell from this set so direction enforcement at p2 still applies.
        goal_cell = _world_to_cell(p2, origin=p1)
        extra_goals = _same_net_cells_in_bounds(
            obstacles, same_net_wires, target_net,
            origin=p1, snap=_GRID, bounds_min=bmin, bounds_max=bmax,
        )
        extra_goals.discard(goal_cell)
        # Drop cells already wire-reachable from p1: tee'ing onto a
        # same-net wire that's already connected to p1 doesn't add
        # connectivity, and it lets A* "succeed" after one step by
        # walking onto the very wire whose endpoint coincides with
        # p1.  Bug repro: connect_pins(auto, [A, B, C]) where A→B
        # wired in pair 1 ends at B; pair 2 (B→C) finds B's wire is
        # adjacent to its start cell and terminates there, never
        # reaching C.
        reachable = _wire_reachable_cells_from_start(
            obstacles, same_net_wires,
            origin=p1, snap=_GRID, bounds_min=bmin, bounds_max=bmax,
        )
        extra_goals -= reachable
        # The start cell can't be a tee target.
        extra_goals.discard((0, 0))

        cells = _astar_search(
            start=(0, 0),
            initial_dir=_angle_to_dir(a1),
            goal=goal_cell,
            final_dir=_angle_to_dir(a2),
            grid=grid_obs,
            bounds_min=bmin,
            bounds_max=bmax,
            cost_model=cm,
            max_steps=max_steps,
            extra_goal_cells=extra_goals,
        )
        if cells is None:
            if same_net_wires:
                # We skipped shapes earlier to give A* first try; fall back now.
                shape_result = _try_shapes()
                if shape_result is not None:
                    return shape_result
            return RouteResult(False, [], last_reject or "astar_no_path")
        # If A* terminated at a tee target instead of p2, the last cell isn't
        # p2's grid cell. _cells_to_segments substitutes p2 for the final
        # endpoint — for tee termination we want the actual cell location, so
        # build the final point from cell coords.
        terminated_at_goal = cells[-1] == goal_cell
        end_world = p2 if terminated_at_goal else _cell_to_world(cells[-1], p1, _GRID)
        segments = _cells_to_segments(cells, p1, end_world, origin=p1)
        if (len(segments) - 1) > max_bends:
            return RouteResult(False, [], "over_max_bends")
        if _path_length(segments) > max_len + EPS:
            return RouteResult(False, [], "over_max_len")
        bad = check_spurious_connections(
            segments,
            obstacles,
            target_net,
            own_endpoints=own_endpoints,
            same_net_wire_indices=same_net_wires,
        )
        if bad is not None:
            return RouteResult(False, [], f"spurious:{bad}")
        style = "astar-tee" if not terminated_at_goal else "astar"
        return RouteResult(True, segments, "", style=style)
