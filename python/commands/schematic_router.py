"""
Schematic Router — Phase 2.

Draws real wires (polylines) between pin endpoints when geometry is friendly,
falling back to net labels via the caller when it isn't. Phase 2 enumerates
straight, single-bend (L), and double-bend (U) candidate paths. Full A* is
Phase 3 — see docs/SCHEMATIC_AUTOROUTER_PLAN.md.

The spurious-connection guard is the single most important correctness check
and is applied for every Phase.
"""

from __future__ import annotations

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


def collect_obstacles(
    schematic_path: Path, exclude_pins: Set[Tuple[str, str]]
) -> Obstacles:
    return Obstacles(
        other_pins=_collect_pin_endpoints(schematic_path, exclude_pins),
        other_labels=_collect_labels(schematic_path),
        other_wires=_collect_wires(schematic_path),
    )


# ---------------------------------------------------------------------------
# Spurious-connection guard
# ---------------------------------------------------------------------------


def check_spurious_connections(
    segments: List[Segment],
    obstacles: Obstacles,
    target_net: str,
    *,
    own_endpoints: Sequence[Point] = (),
) -> Optional[str]:
    """Return None if *segments* are safe to add; else a short reason string.

    *own_endpoints* are the pin endpoints we are intentionally connecting; they
    are exempt from the "no pin on segment" rule (a wire MUST touch them).
    """

    def _is_own(pt: Point) -> bool:
        return any(_approx(pt[0], q[0]) and _approx(pt[1], q[1]) for q in own_endpoints)

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
        for (we1, we2) in obstacles.other_wires:
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
        for ow in obstacles.other_wires:
            if _segments_collinear_overlap(seg, ow):
                return "candidate wire collinearly overlaps an existing wire"

    return None


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


class SchematicRouter:
    """Phase-2 router: straight, L-shape, and U-shape candidates with the
    spurious-connection guard applied to every shape."""

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
    ) -> RouteResult:
        """Try to route a polyline between two pins.

        Tries straight (0 bends), then L-shapes (1 bend), then U-shapes (2
        bends). The first candidate that is within ``max_len``/``max_bends``
        and passes ``check_spurious_connections`` wins. Returns
        ``RouteResult(success=False)`` with a short reason string when nothing
        works; callers (typically ``ConnectionManager.connect_pins``) decide
        whether to fall back to net labels.
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
        last_reject: str = ""

        # (style_label, candidate_paths). Each path is a List[Segment].
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
                    path, obstacles, target_net, own_endpoints=own_endpoints
                )
                if bad is None:
                    return RouteResult(True, path, "", style=style_label)
                last_reject = f"spurious:{bad}"

        if last_reject:
            return RouteResult(False, [], last_reject)
        return RouteResult(False, [], "no_candidate_path")
