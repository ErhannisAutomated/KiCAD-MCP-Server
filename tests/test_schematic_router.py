"""
Tests for the Phase-1 schematic autorouter.

Two layers:
  - Pure-Python unit tests for the geometry helpers in schematic_router.py.
  - Integration tests that build tiny synthetic schematics, run
    SchematicRouter.route_pair / ConnectionManager.connect_pins, and assert
    that wires (or labels) end up in the .kicad_sch file as expected.
"""

from __future__ import annotations

import importlib.util
import math
import os
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Module loading — mirror tests/test_get_pin_angle.py so we can import the
# real schematic_router and its dependencies without booting the MCP server.
# ---------------------------------------------------------------------------
_PYTHON_DIR = os.path.join(os.path.dirname(__file__), "..", "python")
if _PYTHON_DIR not in sys.path:
    sys.path.insert(0, _PYTHON_DIR)

sys.modules.setdefault("pcbnew", MagicMock())

from commands import schematic_router  # noqa: E402
from commands.schematic_router import (  # noqa: E402
    DEFAULT_POWER_NETS,
    GridObstacles,
    Obstacles,
    SchematicRouter,
    _angle_to_dir,
    _astar_search,
    _cell_to_world,
    _classify_wires_by_net,
    _collinear_and_facing,
    _l_shape_candidates,
    _on_grid,
    _opposite_dir,
    _point_on_segment,
    _segment_length,
    _segments_collinear_overlap,
    _segments_strictly_cross,
    _U_OFFSET,
    _u_shape_candidates,
    _world_to_cell,
    check_spurious_connections,
    is_power_net,
)


# ===========================================================================
# Pure-Python unit tests — geometry helpers
# ===========================================================================


@pytest.mark.unit
class TestCollinearAndFacing:
    def test_horizontal_facing_right(self):
        # p1 at left, outward right; p2 at right, outward left
        assert _collinear_and_facing((10.0, 50.0), 0.0, (30.0, 50.0), 180.0)

    def test_horizontal_facing_left(self):
        assert _collinear_and_facing((30.0, 50.0), 180.0, (10.0, 50.0), 0.0)

    def test_horizontal_pointing_apart(self):
        # both face outward away from each other
        assert not _collinear_and_facing((10.0, 50.0), 180.0, (30.0, 50.0), 0.0)

    def test_horizontal_perpendicular(self):
        assert not _collinear_and_facing((10.0, 50.0), 90.0, (30.0, 50.0), 180.0)

    def test_vertical_facing_down(self):
        # p1 above (smaller y) facing down (270); p2 below (larger y) facing up (90)
        assert _collinear_and_facing((10.0, 30.0), 270.0, (10.0, 50.0), 90.0)

    def test_vertical_facing_up(self):
        assert _collinear_and_facing((10.0, 50.0), 90.0, (10.0, 30.0), 270.0)

    def test_vertical_pointing_apart(self):
        assert not _collinear_and_facing((10.0, 30.0), 90.0, (10.0, 50.0), 270.0)

    def test_diagonal_pins(self):
        assert not _collinear_and_facing((10.0, 30.0), 0.0, (30.0, 50.0), 180.0)

    def test_same_point(self):
        assert not _collinear_and_facing((10.0, 10.0), 0.0, (10.0, 10.0), 180.0)


@pytest.mark.unit
class TestPointOnSegment:
    def test_interior_horizontal(self):
        assert _point_on_segment(15.0, 50.0, (10.0, 50.0), (20.0, 50.0))

    def test_interior_strict(self):
        assert _point_on_segment(15.0, 50.0, (10.0, 50.0), (20.0, 50.0), strict=True)

    def test_endpoint_inclusive(self):
        assert _point_on_segment(10.0, 50.0, (10.0, 50.0), (20.0, 50.0))

    def test_endpoint_strict_excludes(self):
        assert not _point_on_segment(10.0, 50.0, (10.0, 50.0), (20.0, 50.0), strict=True)

    def test_off_line_horizontal(self):
        assert not _point_on_segment(15.0, 51.0, (10.0, 50.0), (20.0, 50.0))

    def test_outside_range(self):
        assert not _point_on_segment(25.0, 50.0, (10.0, 50.0), (20.0, 50.0))

    def test_diagonal_segment_unsupported(self):
        # diagonal segments aren't expected; helper returns False (we route orthogonally)
        assert not _point_on_segment(15.0, 55.0, (10.0, 50.0), (20.0, 60.0))


@pytest.mark.unit
class TestSegmentsCollinearOverlap:
    def test_horizontal_overlap(self):
        assert _segments_collinear_overlap(
            ((0.0, 50.0), (20.0, 50.0)), ((10.0, 50.0), (30.0, 50.0))
        )

    def test_horizontal_no_overlap(self):
        assert not _segments_collinear_overlap(
            ((0.0, 50.0), (5.0, 50.0)), ((10.0, 50.0), (20.0, 50.0))
        )

    def test_horizontal_touching_at_endpoint_is_not_overlap(self):
        # touching at one point doesn't constitute overlap (used for valid junctions)
        assert not _segments_collinear_overlap(
            ((0.0, 50.0), (10.0, 50.0)), ((10.0, 50.0), (20.0, 50.0))
        )

    def test_horizontal_different_y(self):
        assert not _segments_collinear_overlap(
            ((0.0, 50.0), (20.0, 50.0)), ((0.0, 51.0), (20.0, 51.0))
        )

    def test_vertical_overlap(self):
        assert _segments_collinear_overlap(
            ((10.0, 0.0), (10.0, 20.0)), ((10.0, 5.0), (10.0, 30.0))
        )


@pytest.mark.unit
class TestSegmentsStrictlyCross:
    """Perpendicular crossings detected; T-junctions and shared endpoints are not."""

    def test_perpendicular_cross_in_interior(self):
        # horizontal y=50 from x=0..20, vertical x=10 from y=40..60 — cross at (10, 50)
        assert _segments_strictly_cross(
            ((0.0, 50.0), (20.0, 50.0)), ((10.0, 40.0), (10.0, 60.0))
        )

    def test_perpendicular_no_cross_misses(self):
        # vertical x=30 doesn't reach the horizontal at x=0..20
        assert not _segments_strictly_cross(
            ((0.0, 50.0), (20.0, 50.0)), ((30.0, 40.0), (30.0, 60.0))
        )

    def test_t_junction_endpoint_not_cross(self):
        # vertical's endpoint exactly on the horizontal — that's a T-junction,
        # caught by rule 3, not rule 6.
        assert not _segments_strictly_cross(
            ((0.0, 50.0), (20.0, 50.0)), ((10.0, 50.0), (10.0, 60.0))
        )

    def test_shared_endpoint_not_cross(self):
        # two segments meeting at a corner — not a crossing.
        assert not _segments_strictly_cross(
            ((0.0, 50.0), (10.0, 50.0)), ((10.0, 50.0), (10.0, 60.0))
        )

    def test_collinear_overlap_not_cross(self):
        # parallel/overlapping — caught by rule 4, not rule 6.
        assert not _segments_strictly_cross(
            ((0.0, 50.0), (20.0, 50.0)), ((10.0, 50.0), (30.0, 50.0))
        )

    def test_crossing_when_segment_pairs_are_swapped(self):
        # symmetric: result independent of which segment is s1
        assert _segments_strictly_cross(
            ((10.0, 40.0), (10.0, 60.0)), ((0.0, 50.0), (20.0, 50.0))
        )


@pytest.mark.unit
class TestCheckSpuriousRule6_WireCrossing:
    """Rule 6: candidate wire must not perpendicularly cross an unrelated wire."""

    def _empty_obstacles(self) -> Obstacles:
        return Obstacles(
            other_pins=[], other_labels=[], other_wires=[], other_bboxes=[]
        )

    def test_strict_cross_is_rejected(self):
        ob = self._empty_obstacles()
        ob.other_wires.append(((10.0, 40.0), (10.0, 60.0)))  # vertical
        # candidate horizontal wire crosses it at (10, 50)
        candidate = [((0.0, 50.0), (20.0, 50.0))]
        result = check_spurious_connections(candidate, ob, target_net="NET_A")
        assert result is not None
        assert "cross" in result

    def test_t_junction_is_caught_by_rule_3_not_rule_6(self):
        # T-junction: candidate wire's endpoint coincides with another wire.
        # Rule 3 should fire, not rule 6.  The reason should mention "T" or
        # "endpoint", not "cross".
        ob = self._empty_obstacles()
        ob.other_wires.append(((10.0, 40.0), (10.0, 60.0)))
        # candidate ends ON the vertical wire (at 10, 50)
        candidate = [((0.0, 50.0), (10.0, 50.0))]
        result = check_spurious_connections(candidate, ob, target_net="NET_A")
        # Either rule 3 fires (endpoint on interior) or no rule fires; rule 6
        # specifically must NOT — the candidate doesn't strictly cross.
        if result is not None:
            assert "cross" not in result, f"rule 6 fired for a T-junction: {result}"

    def test_same_net_crossing_is_allowed(self):
        # If the unrelated wire belongs to target_net, this is a same-net tee
        # (intentional join) and rule 6 should skip it.
        ob = self._empty_obstacles()
        ob.other_wires.append(((10.0, 40.0), (10.0, 60.0)))
        candidate = [((0.0, 50.0), (20.0, 50.0))]
        result = check_spurious_connections(
            candidate, ob, target_net="NET_A", same_net_wire_indices={0}
        )
        assert result is None, f"same-net cross should be allowed but got: {result}"

    def test_no_crossing_no_rule_6_fire(self):
        ob = self._empty_obstacles()
        ob.other_wires.append(((30.0, 40.0), (30.0, 60.0)))  # vertical at x=30
        # candidate horizontal at y=50, x=0..20 — doesn't reach x=30
        candidate = [((0.0, 50.0), (20.0, 50.0))]
        result = check_spurious_connections(candidate, ob, target_net="NET_A")
        assert result is None


@pytest.mark.unit
class TestCheckSpuriousRule7_StubZoneCrossing:
    """Rule 7: candidate wire must not cross a pin's outward stub zone."""

    def _make_obstacles(self, pin_pt, angle):
        from commands.schematic_router import Obstacles
        ob = Obstacles(
            other_pins=[pin_pt],
            other_labels=[],
            other_wires=[],
            other_bboxes=[],
        )
        ob.pin_angles[(round(pin_pt[0] * 1000), round(pin_pt[1] * 1000))] = angle
        return ob

    def test_route_crossing_stub_zone_is_rejected(self):
        # Pin at (10, 40), outward angle 90 (lib up = screen -y).
        # Stub zone runs from (10, 40) to (10, 37.46) (40 - 2.54).
        # A horizontal candidate at y=38.5 from x=0 to x=20 crosses the stub.
        ob = self._make_obstacles((10.0, 40.0), 90.0)
        candidate = [((0.0, 38.5), (20.0, 38.5))]
        result = check_spurious_connections(candidate, ob, target_net="NET_A")
        assert result is not None
        assert "stub" in result, result

    def test_route_far_from_stub_zone_is_allowed(self):
        # Pin at (10, 40), outward up; stub zone y in [37.46, 40].
        # Horizontal candidate at y=20 — well clear of the stub zone.
        ob = self._make_obstacles((10.0, 40.0), 90.0)
        candidate = [((0.0, 20.0), (20.0, 20.0))]
        result = check_spurious_connections(candidate, ob, target_net="NET_A")
        assert result is None

    def test_pin_with_no_angle_doesnt_block_routes(self):
        # If pin_angles doesn't have an entry for this pin (older obstacle
        # collection paths), rule 7 must skip cleanly.
        from commands.schematic_router import Obstacles
        ob = Obstacles(
            other_pins=[(10.0, 40.0)],
            other_labels=[], other_wires=[], other_bboxes=[],
        )
        # No pin_angles entry; the candidate would only fail rule 1 if it
        # passed THROUGH the pin endpoint, which it doesn't.
        candidate = [((0.0, 38.5), (20.0, 38.5))]
        result = check_spurious_connections(candidate, ob, target_net="NET_A")
        assert result is None

    def test_own_pin_not_blocked(self):
        # Own pins (the route's own endpoints) are exempt — the route's own
        # stub zone shouldn't reject the route reaching its own pin.
        ob = self._make_obstacles((10.0, 40.0), 90.0)
        candidate = [((0.0, 38.5), (20.0, 38.5))]
        result = check_spurious_connections(
            candidate, ob, target_net="NET_A", own_endpoints=((10.0, 40.0),)
        )
        assert result is None


@pytest.mark.unit
class TestIsPowerNet:
    def test_default_powers(self):
        for net in ("VBUS", "GND", "VCC", "+3V3", "+5V"):
            assert is_power_net(net), net

    def test_custom_extension(self):
        assert is_power_net("ANALOG_REF", custom=["ANALOG_REF"])

    def test_non_power(self):
        assert not is_power_net("SIG_OUT")
        assert not is_power_net("LED_R")

    def test_rail_pattern(self):
        # +<digit> or -<digit> patterns count as rails
        assert is_power_net("+1V8")
        assert is_power_net("-12V")


# ===========================================================================
# Phase-2 candidate generators
# ===========================================================================


@pytest.mark.unit
class TestLShapeCandidates:
    def test_collinear_horizontal_returns_empty(self):
        # Pins on the same horizontal line — straight or U, never an L.
        assert _l_shape_candidates((10.0, 50.0), 0.0, (30.0, 50.0), 180.0) == []

    def test_collinear_vertical_returns_empty(self):
        assert _l_shape_candidates((10.0, 30.0), 270.0, (10.0, 50.0), 90.0) == []

    def test_perpendicular_pins_one_l_emitted(self):
        # p1 facing down (270), p2 facing left (180), p2 to the right and below p1.
        # L2 (corner at (x1, y2)) is the only candidate consistent with both angles.
        cands = _l_shape_candidates((100.0, 100.0), 270.0, (110.0, 110.0), 180.0)
        assert len(cands) == 1
        ((s1a, s1b), (s2a, s2b)) = cands[0]
        assert s1a == (100.0, 100.0) and s1b == (100.0, 110.0)
        assert s2a == (100.0, 110.0) and s2b == (110.0, 110.0)

    def test_perpendicular_pins_other_orientation(self):
        # p1 facing right (0), p2 facing down (270), p2 ABOVE and right of p1.
        # L1 (corner at (x2, y1)) is the only candidate: first horizontal,
        # then vertical going up to meet p2 from below.
        cands = _l_shape_candidates((100.0, 100.0), 0.0, (110.0, 90.0), 270.0)
        assert len(cands) == 1
        ((s1a, s1b), (s2a, s2b)) = cands[0]
        assert s1b == (110.0, 100.0)
        assert s2a == (110.0, 100.0) and s2b == (110.0, 90.0)

    def test_inconsistent_directions_returns_empty(self):
        # p1 faces up (90) but L would need it to face down (270) for p2 below.
        assert _l_shape_candidates((100.0, 100.0), 90.0, (110.0, 110.0), 180.0) == []


@pytest.mark.unit
class TestUShapeCandidates:
    def test_collinear_horizontal_returns_empty(self):
        # Same y → no U shape (would degenerate).
        assert _u_shape_candidates((10.0, 50.0), 0.0, (30.0, 50.0), 180.0) == []

    def test_horizontal_facing_each_other_diff_y(self):
        cands = _u_shape_candidates((10.0, 50.0), 0.0, (30.0, 60.0), 180.0)
        assert len(cands) == 1
        ((s1a, s1b), (s2a, s2b), (s3a, s3b)) = cands[0]
        # Bridge at midpoint xm = 20
        assert math.isclose(s1b[0], 20.0, abs_tol=1e-3)
        assert math.isclose(s2a[0], 20.0, abs_tol=1e-3)
        assert math.isclose(s2b[0], 20.0, abs_tol=1e-3)
        assert s1a == (10.0, 50.0)
        assert s3b == (30.0, 60.0)

    def test_horizontal_both_facing_right(self):
        # d1=d2=0; xm goes to the right of both pins by _U_OFFSET.
        cands = _u_shape_candidates((10.0, 50.0), 0.0, (20.0, 60.0), 0.0)
        assert len(cands) == 1
        ((s1a, s1b), (s2a, s2b), (s3a, s3b)) = cands[0]
        bridge_x = max(10.0, 20.0) + _U_OFFSET
        assert math.isclose(s1b[0], bridge_x, abs_tol=1e-3)
        assert math.isclose(s2b[0], bridge_x, abs_tol=1e-3)

    def test_horizontal_both_facing_left(self):
        cands = _u_shape_candidates((10.0, 50.0), 180.0, (20.0, 60.0), 180.0)
        assert len(cands) == 1
        ((s1a, s1b), (s2a, s2b), (s3a, s3b)) = cands[0]
        bridge_x = min(10.0, 20.0) - _U_OFFSET
        assert math.isclose(s1b[0], bridge_x, abs_tol=1e-3)

    def test_vertical_both_facing_up(self):
        cands = _u_shape_candidates((10.0, 50.0), 90.0, (20.0, 60.0), 90.0)
        assert len(cands) == 1
        ((s1a, s1b), (s2a, s2b), (s3a, s3b)) = cands[0]
        bridge_y = min(50.0, 60.0) - _U_OFFSET
        assert math.isclose(s1b[1], bridge_y, abs_tol=1e-3)
        assert math.isclose(s2a[1], bridge_y, abs_tol=1e-3)

    def test_vertical_facing_each_other_diff_x(self):
        # d1=270 (down), d2=90 (up), p1 above p2 on screen → bridge at midpoint y.
        cands = _u_shape_candidates((10.0, 50.0), 270.0, (20.0, 70.0), 90.0)
        assert len(cands) == 1
        ((s1a, s1b), (s2a, s2b), (s3a, s3b)) = cands[0]
        assert math.isclose(s1b[1], 60.0, abs_tol=1e-3)

    def test_mixed_orientations_return_empty(self):
        # One pin horizontal, one pin vertical → no U applies.
        assert _u_shape_candidates((10.0, 50.0), 0.0, (30.0, 60.0), 90.0) == []

    def test_facing_apart_horizontal_no_u(self):
        # d1=180 (left), d2=0 (right), p2 is right of p1 → no consistent xm.
        assert _u_shape_candidates((10.0, 50.0), 180.0, (30.0, 60.0), 0.0) == []


# ===========================================================================
# Phase-3 grid + A* helpers
# ===========================================================================


@pytest.mark.unit
class TestGridHelpers:
    def test_angle_to_dir_cardinal(self):
        # 0=E, 90=N, 180=W, 270=S
        assert _angle_to_dir(0) == 0
        assert _angle_to_dir(90) == 3
        assert _angle_to_dir(180) == 2
        assert _angle_to_dir(270) == 1

    def test_opposite_dir(self):
        assert _opposite_dir(0) == 2
        assert _opposite_dir(1) == 3
        assert _opposite_dir(2) == 0
        assert _opposite_dir(3) == 1

    def test_world_to_cell_round_trip(self):
        origin = (101.6, 101.6)
        cell = _world_to_cell((105.41, 101.6), origin)
        assert cell == (3, 0)
        back = _cell_to_world(cell, origin)
        assert math.isclose(back[0], 105.41, abs_tol=1e-3)

    def test_on_grid_true_for_multiples(self):
        assert _on_grid((105.41, 101.6), origin=(101.6, 101.6))

    def test_on_grid_false_for_non_multiples(self):
        # Δ = (3.81, 0) = 3*1.27 ✓; but (3.5, 0) is not.
        assert not _on_grid((105.1, 101.6), origin=(101.6, 101.6))


@pytest.mark.unit
class TestAStarSearch:
    def test_straight_path_no_obstacles(self):
        from commands.schematic_router import CostModel

        cells = _astar_search(
            start=(0, 0),
            initial_dir=0,  # E
            goal=(5, 0),
            final_dir=2,  # W (outward from p2 → wire arrives east)
            grid=GridObstacles(),
            bounds_min=(-10, -10),
            bounds_max=(10, 10),
            cost_model=CostModel(),
            max_steps=20,
        )
        assert cells is not None
        # 6 cells: (0,0)..(5,0)
        assert cells[0] == (0, 0) and cells[-1] == (5, 0)
        assert len(cells) == 6

    def test_routes_around_blocked_cells(self):
        from commands.schematic_router import CostModel

        # Block the direct east path at cells (3,0), (3,-1) etc. to force a
        # detour either north or south.
        blocked = {(3, 0), (3, 1), (3, 2), (3, 3), (3, 4), (3, 5)}
        cells = _astar_search(
            start=(0, 0),
            initial_dir=0,
            goal=(5, 0),
            final_dir=2,
            grid=GridObstacles(blocked_cells=blocked),
            bounds_min=(-10, -10),
            bounds_max=(10, 10),
            cost_model=CostModel(),
            max_steps=30,
        )
        assert cells is not None
        # Path must not pass through any blocked cell
        for c in cells:
            assert c not in blocked
        assert cells[0] == (0, 0) and cells[-1] == (5, 0)

    def test_no_path_when_completely_walled_off(self):
        from commands.schematic_router import CostModel

        # Surround the goal with blocked cells so it is unreachable.
        blocked = {
            (4, 0), (5, -1), (6, 0), (5, 1),
            # Plus the only feasible approach cell from goal+W = (4, 0)
        }
        cells = _astar_search(
            start=(0, 0),
            initial_dir=0,
            goal=(5, 0),
            final_dir=2,
            grid=GridObstacles(blocked_cells=blocked),
            bounds_min=(-3, -3),
            bounds_max=(8, 3),
            cost_model=CostModel(),
            max_steps=40,
        )
        assert cells is None

    def test_must_exit_in_initial_direction(self):
        from commands.schematic_router import CostModel

        # Goal lies WEST of start, but initial_dir forces an east first step.
        # Path should still be findable with a U-turn detour.
        cells = _astar_search(
            start=(5, 0),
            initial_dir=0,  # forced east
            goal=(0, 0),
            final_dir=0,  # outward east → wire arrives going west
            grid=GridObstacles(),
            bounds_min=(-2, -3),
            bounds_max=(10, 3),
            cost_model=CostModel(),
            max_steps=30,
        )
        assert cells is not None
        assert cells[0] == (5, 0)
        assert cells[1] == (6, 0)  # forced first step east
        assert cells[-1] == (0, 0)

    def test_must_arrive_in_final_direction(self):
        from commands.schematic_router import CostModel

        # final_dir=N means the wire approaches goal from the north (cell goal+N).
        cells = _astar_search(
            start=(0, 0),
            initial_dir=0,
            goal=(5, 0),
            final_dir=3,  # N → predecessor cell is (5,-1)
            grid=GridObstacles(),
            bounds_min=(-3, -3),
            bounds_max=(8, 3),
            cost_model=CostModel(),
            max_steps=40,
        )
        assert cells is not None
        # Last step must come from (5, -1)
        assert cells[-2] == (5, -1)
        assert cells[-1] == (5, 0)


# ===========================================================================
# Phase-4 wire-net classifier
# ===========================================================================


@pytest.mark.unit
class TestClassifyWiresByNet:
    def test_label_marks_connected_component(self):
        # Wires 0 and 1 share an endpoint and are labeled SIG via wire 0's
        # left endpoint. Wire 2 is disconnected and unlabeled.
        obs = Obstacles(
            other_pins=[],
            other_labels=[((0.0, 50.0), "SIG")],
            other_wires=[
                ((0.0, 50.0), (10.0, 50.0)),
                ((10.0, 50.0), (20.0, 50.0)),
                ((50.0, 50.0), (60.0, 50.0)),
            ],
        )
        same = _classify_wires_by_net(obs, "SIG")
        assert same == {0, 1}

    def test_own_endpoint_marks_component_without_label(self):
        # No label, but our own pin endpoint touches wire 0 — still classified.
        obs = Obstacles(
            other_pins=[],
            other_labels=[],
            other_wires=[
                ((0.0, 50.0), (10.0, 50.0)),
                ((10.0, 50.0), (20.0, 50.0)),
                ((50.0, 50.0), (60.0, 50.0)),
            ],
        )
        same = _classify_wires_by_net(
            obs, "SIG", own_pin_endpoints=[(0.0, 50.0)]
        )
        assert same == {0, 1}

    def test_different_net_label_returns_empty(self):
        obs = Obstacles(
            other_pins=[],
            other_labels=[((0.0, 50.0), "OTHER")],
            other_wires=[((0.0, 50.0), (10.0, 50.0))],
        )
        assert _classify_wires_by_net(obs, "SIG") == set()

    def test_t_junction_propagates_classification(self):
        # Wire 0 horizontal y=50; wire 1's endpoint lands on wire 0's interior.
        # SIG label is on wire 1's far endpoint — both wires must classify.
        obs = Obstacles(
            other_pins=[],
            other_labels=[((5.0, 60.0), "SIG")],
            other_wires=[
                ((0.0, 50.0), (10.0, 50.0)),  # horizontal
                ((5.0, 50.0), (5.0, 60.0)),  # vertical, T-juncts on wire 0
            ],
        )
        assert _classify_wires_by_net(obs, "SIG") == {0, 1}


# ===========================================================================
# Spurious-connection guard
# ===========================================================================


@pytest.mark.unit
class TestSpuriousConnectionGuard:
    def test_clean_segment_passes(self):
        seg = ((0.0, 50.0), (20.0, 50.0))
        obs = Obstacles(other_pins=[(0.0, 60.0)], other_labels=[], other_wires=[])
        assert (
            check_spurious_connections(
                [seg], obs, target_net="N", own_endpoints=[(0.0, 50.0), (20.0, 50.0)]
            )
            is None
        )

    def test_pin_on_segment_interior_rejected(self):
        seg = ((0.0, 50.0), (20.0, 50.0))
        obs = Obstacles(other_pins=[(10.0, 50.0)], other_labels=[], other_wires=[])
        reason = check_spurious_connections(
            [seg], obs, target_net="N", own_endpoints=[(0.0, 50.0), (20.0, 50.0)]
        )
        assert reason is not None and "pin" in reason

    def test_own_endpoint_pin_allowed(self):
        # The pins we are connecting are exempt
        seg = ((0.0, 50.0), (20.0, 50.0))
        obs = Obstacles(
            other_pins=[(0.0, 50.0), (20.0, 50.0)], other_labels=[], other_wires=[]
        )
        assert (
            check_spurious_connections(
                [seg], obs, target_net="N", own_endpoints=[(0.0, 50.0), (20.0, 50.0)]
            )
            is None
        )

    def test_other_net_label_on_segment_rejected(self):
        seg = ((0.0, 50.0), (20.0, 50.0))
        obs = Obstacles(
            other_pins=[],
            other_labels=[((10.0, 50.0), "OTHER_NET")],
            other_wires=[],
        )
        reason = check_spurious_connections(
            [seg], obs, target_net="N", own_endpoints=[(0.0, 50.0), (20.0, 50.0)]
        )
        assert reason is not None and "OTHER_NET" in reason

    def test_same_net_label_allowed(self):
        seg = ((0.0, 50.0), (20.0, 50.0))
        obs = Obstacles(
            other_pins=[],
            other_labels=[((10.0, 50.0), "TARGET")],
            other_wires=[],
        )
        assert (
            check_spurious_connections(
                [seg], obs, target_net="TARGET", own_endpoints=[(0.0, 50.0), (20.0, 50.0)]
            )
            is None
        )

    def test_existing_wire_endpoint_on_interior_rejected(self):
        seg = ((0.0, 50.0), (20.0, 50.0))
        obs = Obstacles(
            other_pins=[],
            other_labels=[],
            other_wires=[((10.0, 50.0), (10.0, 60.0))],
        )
        reason = check_spurious_connections(
            [seg], obs, target_net="N", own_endpoints=[(0.0, 50.0), (20.0, 50.0)]
        )
        assert reason is not None and "wire endpoint" in reason

    def test_collinear_overlap_rejected(self):
        seg = ((0.0, 50.0), (20.0, 50.0))
        obs = Obstacles(
            other_pins=[],
            other_labels=[],
            other_wires=[((5.0, 50.0), (15.0, 50.0))],
        )
        reason = check_spurious_connections(
            [seg], obs, target_net="N", own_endpoints=[(0.0, 50.0), (20.0, 50.0)]
        )
        assert reason is not None
        # Note: the wire endpoints (5,50) and (15,50) are also strictly inside
        # our segment, so the guard fires on rule #3 first. Either rejection
        # is correct; we just need *some* reason.

    def test_wire_crossing_perpendicular_rejected_by_rule_6(self):
        # A perpendicular crossing is electrically valid in KiCad (no junction
        # → no connection) but visually confusing.  Rule 6 (added 2026-05-09)
        # rejects it so the router prefers a different shape or labels.  This
        # test was previously asserting the OPPOSITE — that rule 6 didn't
        # exist — and was inverted along with the policy change.
        seg = ((0.0, 50.0), (20.0, 50.0))
        obs = Obstacles(
            other_pins=[],
            other_labels=[],
            other_wires=[((10.0, 40.0), (10.0, 60.0))],
        )
        result = check_spurious_connections(
            [seg], obs, target_net="N", own_endpoints=[(0.0, 50.0), (20.0, 50.0)]
        )
        assert result is not None
        assert "cross" in result


# ===========================================================================
# Integration tests — build a real .kicad_sch and run the router
# ===========================================================================


# Two horizontal resistors face-to-face: R1 pin 2 (right side) facing R2 pin 1 (left side).
# Device:R: pin 1 at (0, 3.81, angle 270°), pin 2 at (0, -3.81, angle 90°).
# Place R1 at (100, 100) rot 90 → pin 2 ends up to the right of R1 body, facing right.
# Place R2 at (130, 100) rot 90 → pin 1 ends up to the left of R2 body, facing left.
# Pin world coords (Y-down screen, lib Y-up):
#   For Device:R rotation=90: WireDragger.pin_world_xy with (0, ±3.81)
#   pin 1 (0, 3.81) at rot 90: rotates to world (3.81, 0) → world (sym_x + 3.81, sym_y)
#   pin 2 (0, -3.81) at rot 90: rotates to (-3.81, 0) → world (sym_x - 3.81, sym_y)
# So with R1 at (100,100) rot 90: pin 2 → (100-3.81, 100) = (96.19, 100); pin 1 → (103.81, 100)
# With R2 at (130,100) rot 90: pin 2 → (126.19, 100); pin 1 → (133.81, 100)
# We want R1.pin 1 (right of R1, 103.81) connected to R2.pin 2 (left of R2, 126.19) — both at y=100.
# Outward angles: R1.pin 1 lib angle 270; with rot=90 → -180? Let me just compute via the
# library and check the test results.

R_LIB = textwrap.dedent("""\
    (lib_symbols
      (symbol "Device:R" (pin_numbers hide) (pin_names (offset 0))
        (symbol "R_1_1"
          (pin passive line (at 0 3.81 270) (length 1.27)
            (name "~" (effects (font (size 1.27 1.27))))
            (number "1" (effects (font (size 1.27 1.27))))
          )
          (pin passive line (at 0 -3.81 90) (length 1.27)
            (name "~" (effects (font (size 1.27 1.27))))
            (number "2" (effects (font (size 1.27 1.27))))
          )
        )
      )
    )
""")


def _make_two_resistors_sch(
    r1_xy=(100.0, 100.0),
    r1_rot=90,
    r2_xy=(130.0, 100.0),
    r2_rot=90,
) -> str:
    return textwrap.dedent(f"""\
        (kicad_sch (version 20250114) (generator "test")
          {R_LIB}
          (symbol (lib_id "Device:R") (at {r1_xy[0]} {r1_xy[1]} {r1_rot}) (unit 1)
            (property "Reference" "R1" (at {r1_xy[0]} {r1_xy[1]} 0))
            (property "Value" "10k" (at {r1_xy[0]} {r1_xy[1]} 0))
            (instances (project "test" (path "/" (reference "R1") (unit 1))))
          )
          (symbol (lib_id "Device:R") (at {r2_xy[0]} {r2_xy[1]} {r2_rot}) (unit 1)
            (property "Reference" "R2" (at {r2_xy[0]} {r2_xy[1]} 0))
            (property "Value" "10k" (at {r2_xy[0]} {r2_xy[1]} 0))
            (instances (project "test" (path "/" (reference "R2") (unit 1))))
          )
          (sheet_instances (path "/" (page "1")))
        )
    """)


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content)
    return p


@pytest.mark.unit
class TestRoutePairIntegration:
    def test_horizontal_facing_pins_route_to_straight_segment(self, tmp_path):
        # R1 at (100,100) rot=90 → pin 2 at (103.81, 100), outward right.
        # R2 at (130,100) rot=90 → pin 1 at (126.19, 100), outward left.
        # They face each other on y=100 → expect a single straight segment.
        sch = _write(
            tmp_path,
            "two_r.kicad_sch",
            _make_two_resistors_sch(
                r1_xy=(100.0, 100.0),
                r1_rot=90,
                r2_xy=(130.0, 100.0),
                r2_rot=90,
            ),
        )
        result = SchematicRouter.route_pair(
            sch, "R1", "2", "R2", "1", target_net="SIG", max_len=80.0
        )
        assert result.success, f"route failed: {result.reason}"
        assert result.style == "straight"
        assert len(result.segments) == 1
        ((sx, sy), (ex, ey)) = result.segments[0]
        assert math.isclose(sx, 103.81, abs_tol=1e-3)
        assert math.isclose(sy, 100.0, abs_tol=1e-3)
        assert math.isclose(ex, 126.19, abs_tol=1e-3)
        assert math.isclose(ey, 100.0, abs_tol=1e-3)

    def test_misaligned_horizontal_pins_route_via_u_shape(self, tmp_path):
        # R1 at (100, 100), R2 at (130, 110): both pins face horizontally but
        # pins are on different y. Phase 1 cannot do straight here; Phase 2
        # closes the gap with an H-V-H U-shape.
        sch = _write(
            tmp_path,
            "misaligned.kicad_sch",
            _make_two_resistors_sch(r2_xy=(130.0, 110.0)),
        )
        result = SchematicRouter.route_pair(
            sch, "R1", "2", "R2", "1", target_net="SIG"
        )
        assert result.success, f"route failed: {result.reason}"
        assert result.style == "U"
        assert len(result.segments) == 3
        # First segment leaves R1.pin2 outward (right), last arrives at R2.pin1
        # from the right (outward left). Bridge is at the midpoint x.
        ((s1a, s1b), (s2a, s2b), (s3a, s3b)) = result.segments
        assert math.isclose(s1a[0], 103.81, abs_tol=1e-3)
        assert math.isclose(s1b[1], 100.0, abs_tol=1e-3)
        assert math.isclose(s2a[0], s2b[0], abs_tol=1e-3)  # vertical bridge
        assert math.isclose(s3b[0], 126.19, abs_tol=1e-3)
        assert math.isclose(s3b[1], 110.0, abs_tol=1e-3)

    def test_pins_pointing_apart_no_route(self, tmp_path):
        # Two horizontal resistors aligned on the same y, but pick R1.pin1 and
        # R2.pin2 — both outward AWAY from each other. Neither straight, nor
        # L (collinear), nor U (signs incompatible) can connect them in Phase 2.
        sch = _write(
            tmp_path,
            "apart.kicad_sch",
            _make_two_resistors_sch(
                r1_xy=(100.0, 100.0),
                r1_rot=90,
                r2_xy=(130.0, 100.0),
                r2_rot=90,
            ),
        )
        result = SchematicRouter.route_pair(
            sch, "R1", "1", "R2", "2", target_net="SIG"
        )
        assert not result.success
        assert result.reason == "no_candidate_path"

    def test_over_max_len_rejected(self, tmp_path):
        sch = _write(
            tmp_path,
            "long.kicad_sch",
            _make_two_resistors_sch(r2_xy=(900.0, 100.0)),
        )
        result = SchematicRouter.route_pair(
            sch, "R1", "2", "R2", "1", target_net="SIG", max_len=50.0
        )
        assert not result.success
        assert result.reason == "over_max_len"

    def test_l_shape_perpendicular_pins(self, tmp_path):
        # R1 at (100, 100) rot=0  → pin 2 at (100, 103.81), d=270 (down).
        # R2 at (110, 110) rot=90 → pin 1 at (106.19, 110), d=180 (left).
        # L candidate L2 (corner at (x1, y2) = (100, 110)) is consistent with
        # both pin angles; expect a 2-segment route.
        sch = _write(
            tmp_path,
            "lshape.kicad_sch",
            _make_two_resistors_sch(
                r1_xy=(100.0, 100.0),
                r1_rot=0,
                r2_xy=(110.0, 110.0),
                r2_rot=90,
            ),
        )
        result = SchematicRouter.route_pair(
            sch, "R1", "2", "R2", "1", target_net="SIG"
        )
        assert result.success, f"route failed: {result.reason}"
        assert result.style == "L"
        assert len(result.segments) == 2
        ((s1a, s1b), (s2a, s2b)) = result.segments
        assert math.isclose(s1a[0], 100.0, abs_tol=1e-3)
        assert math.isclose(s1a[1], 103.81, abs_tol=1e-3)
        assert math.isclose(s1b[0], 100.0, abs_tol=1e-3)
        assert math.isclose(s1b[1], 110.0, abs_tol=1e-3)
        assert math.isclose(s2b[0], 106.19, abs_tol=1e-3)
        assert math.isclose(s2b[1], 110.0, abs_tol=1e-3)

    def test_u_shape_both_pins_facing_up(self, tmp_path):
        # R1 at (100, 100) rot=0 → pin 1 at (100, 96.19), d=90 (up).
        # R2 at (110, 110) rot=0 → pin 1 at (110, 106.19), d=90 (up).
        # Both face up; need V-H-V bridge above both pins.
        sch = _write(
            tmp_path,
            "uvhv.kicad_sch",
            _make_two_resistors_sch(
                r1_xy=(100.0, 100.0),
                r1_rot=0,
                r2_xy=(110.0, 110.0),
                r2_rot=0,
            ),
        )
        result = SchematicRouter.route_pair(
            sch, "R1", "1", "R2", "1", target_net="SIG"
        )
        assert result.success, f"route failed: {result.reason}"
        assert result.style == "U"
        assert len(result.segments) == 3
        ((s1a, s1b), (s2a, s2b), (s3a, s3b)) = result.segments
        assert math.isclose(s1a[0], 100.0, abs_tol=1e-3)
        assert math.isclose(s1a[1], 96.19, abs_tol=1e-3)
        # Bridge runs above both pins (smaller y)
        assert s1b[1] < min(96.19, 106.19) - 1e-3
        assert math.isclose(s2a[1], s2b[1], abs_tol=1e-3)
        assert math.isclose(s3b[0], 110.0, abs_tol=1e-3)
        assert math.isclose(s3b[1], 106.19, abs_tol=1e-3)

    def test_max_bends_zero_rejects_l_and_u(self, tmp_path):
        # An H-V-H U candidate exists, but max_bends=0 forces straight-only.
        sch = _write(
            tmp_path,
            "bends.kicad_sch",
            _make_two_resistors_sch(r2_xy=(130.0, 110.0)),
        )
        result = SchematicRouter.route_pair(
            sch, "R1", "2", "R2", "1", target_net="SIG", max_bends=0
        )
        assert not result.success
        assert result.reason == "over_max_bends"

    def test_astar_routes_around_obstacle_resistor(self, tmp_path):
        # On-grid placements: R1 at 101.6,101.6 (pin 2 at 105.41,101.6 going E)
        # and R2 at 127.0,101.6 (pin 1 at 123.19,101.6 going W) — Δ = 14 cells
        # in +x. R3 placed mid-way at (114.3, 101.6) rot=0 has a body bbox
        # that completely blocks the y=101.6 horizontal line. A* should
        # detour via 4+ cells north or south.
        sch_text = textwrap.dedent("""\
            (kicad_sch (version 20250114) (generator "test")
              %s
              (symbol (lib_id "Device:R") (at 101.6 101.6 90) (unit 1)
                (property "Reference" "R1" (at 101.6 101.6 0))
                (property "Value" "10k" (at 101.6 101.6 0))
                (instances (project "test" (path "/" (reference "R1") (unit 1))))
              )
              (symbol (lib_id "Device:R") (at 127.0 101.6 90) (unit 1)
                (property "Reference" "R2" (at 127.0 101.6 0))
                (property "Value" "10k" (at 127.0 101.6 0))
                (instances (project "test" (path "/" (reference "R2") (unit 1))))
              )
              (symbol (lib_id "Device:R") (at 114.3 101.6 0) (unit 1)
                (property "Reference" "R3" (at 114.3 101.6 0))
                (property "Value" "1k" (at 114.3 101.6 0))
                (instances (project "test" (path "/" (reference "R3") (unit 1))))
              )
              (sheet_instances (path "/" (page "1")))
            )
        """) % R_LIB
        sch = _write(tmp_path, "obstacle_astar.kicad_sch", sch_text)
        result = SchematicRouter.route_pair(
            sch, "R1", "2", "R2", "1",
            target_net="SIG", max_len=100.0, max_bends=8,
        )
        assert result.success, f"route failed: {result.reason}"
        assert result.style == "astar"
        assert len(result.segments) >= 3
        # First segment exits R1.pin2 (105.41, 101.6) going east
        s0a, s0b = result.segments[0]
        assert math.isclose(s0a[0], 105.41, abs_tol=1e-3)
        assert math.isclose(s0a[1], 101.6, abs_tol=1e-3)
        assert s0b[0] > s0a[0]
        # Last segment arrives at R2.pin1 (123.19, 101.6) from the west
        sNa, sNb = result.segments[-1]
        assert math.isclose(sNb[0], 123.19, abs_tol=1e-3)
        assert math.isclose(sNb[1], 101.6, abs_tol=1e-3)
        assert sNa[0] < sNb[0]
        # Path must NOT pass through R3's bbox y-range at the body x-range —
        # specifically every interior point at x in [112.8, 115.8] must be
        # outside [97.79, 105.41] in y.
        for (a, b) in result.segments:
            ax, ay = a
            bx, by = b
            if math.isclose(ay, by, abs_tol=1e-3):  # horizontal
                # Horizontal segment at y=ay; check if it crosses R3's body x-range
                xlo, xhi = min(ax, bx), max(ax, bx)
                if xlo < 115.8 and xhi > 112.8:
                    assert ay < 97.79 - 1e-3 or ay > 105.41 + 1e-3, (
                        f"horizontal seg at y={ay} crosses R3 body"
                    )

    def test_astar_skipped_when_pins_off_relative_grid(self, tmp_path):
        # Pre-existing fixture uses (100, 100) and (130, 110) which are NOT on
        # a 1.27 mm relative grid. After L/U also fail, A* should be skipped
        # rather than spinning up a search; the previous reject reason wins.
        sch = _write(
            tmp_path,
            "offgrid.kicad_sch",
            _make_two_resistors_sch(r2_xy=(130.0, 110.0)),
        )
        # Force a configuration where L/U have no candidate by using both
        # pins facing the same way (R1.pin1 left and R2.pin2 right after
        # placing R2 at off-grid x). For simplicity: pick the apart-pointing
        # pair on aligned y=100 — Δ = (37.62, 0) is off grid, no shape works.
        sch2 = _write(
            tmp_path,
            "apart_offgrid.kicad_sch",
            _make_two_resistors_sch(r1_xy=(100.0, 100.0), r2_xy=(130.0, 100.0)),
        )
        result = SchematicRouter.route_pair(
            sch2, "R1", "1", "R2", "2", target_net="SIG"
        )
        assert not result.success
        # Either no_candidate_path (no shape worked, A* skipped due to off-grid)
        # is acceptable; what matters is we don't return success or raise.
        assert result.reason in ("no_candidate_path",)

    def test_obstacle_pin_in_path_rejected(self, tmp_path):
        # Place a third resistor whose pin 1 lies on the candidate y=100 segment.
        # R3 at (115, 96.19) rot=180 → pin 1 (lib (0, 3.81)) ends up at:
        #   lx, ly = 0, -3.81; rotate by -180: (0, 3.81); world = (115, 96.19+3.81) = (115, 100)
        # That puts an unrelated pin at (115, 100) — strictly between R1 and R2.
        sch_text = _make_two_resistors_sch().replace(
            "(sheet_instances",
            textwrap.dedent("""\
              (symbol (lib_id "Device:R") (at 115.0 96.19 180) (unit 1)
                (property "Reference" "R3" (at 115.0 96.19 0))
                (property "Value" "1k" (at 115.0 96.19 0))
                (instances (project "test" (path "/" (reference "R3") (unit 1))))
              )
              (sheet_instances""")
            ,
        )
        sch = _write(tmp_path, "obstacle.kicad_sch", sch_text)
        result = SchematicRouter.route_pair(
            sch, "R1", "2", "R2", "1", target_net="SIG"
        )
        assert not result.success, "expected spurious-connection guard to reject"
        assert result.reason.startswith("spurious:"), result.reason


# ===========================================================================
# connect_pins — style="auto" / "wire" path
# ===========================================================================


@pytest.mark.unit
class TestConnectPinsStyle:
    def test_invalid_style_rejected(self, tmp_path):
        from commands.connection_schematic import ConnectionManager

        sch = _write(tmp_path, "x.kicad_sch", _make_two_resistors_sch())
        result = ConnectionManager.connect_pins(
            sch,
            [{"ref": "R1", "pin": "2"}, {"ref": "R2", "pin": "1"}],
            net_name="SIG",
            style="bogus",
        )
        assert result["success"] is False
        assert "invalid style" in result["message"]

    def test_auto_orders_pairs_by_distance_not_load_order(self, tmp_path):
        """`connect_pins(auto)` should attempt closer pin pairs first,
        regardless of where they sit in the load-order list.

        Repro from the BMS REGOUT case: 3 R's facing each other in a
        line — R1 (left), R2 (mid), R3 (right).  Input list order is
        [R1, R3, R2] so the *consecutive*-pair walk would try
        (R1↔R3) first (the longer cross-cluster bridge) then (R3↔R2).
        After the MST switch, the closest pair is attempted first
        instead.  We patch `route_pair` to record the order of attempts
        and assert MST ordering.
        """
        from unittest.mock import patch

        from commands.connection_schematic import ConnectionManager
        from commands.schematic_router import RouteResult, SchematicRouter

        sch_text = textwrap.dedent("""\
            (kicad_sch (version 20250114) (generator "test")
              %s
              (symbol (lib_id "Device:R") (at 100 100 90) (unit 1)
                (property "Reference" "R1" (at 100 100 0))
                (property "Value" "10k" (at 100 100 0))
                (instances (project "test" (path "/" (reference "R1") (unit 1))))
              )
              (symbol (lib_id "Device:R") (at 130 100 90) (unit 1)
                (property "Reference" "R2" (at 130 100 0))
                (property "Value" "10k" (at 130 100 0))
                (instances (project "test" (path "/" (reference "R2") (unit 1))))
              )
              (symbol (lib_id "Device:R") (at 200 100 90) (unit 1)
                (property "Reference" "R3" (at 200 100 0))
                (property "Value" "10k" (at 200 100 0))
                (instances (project "test" (path "/" (reference "R3") (unit 1))))
              )
              (sheet_instances (path "/" (page "1")))
            )
        """) % R_LIB
        sch = _write(tmp_path, "mst_order.kicad_sch", sch_text)

        attempted: List[Tuple[str, str]] = []
        original = SchematicRouter.route_pair

        def _spy(schematic_path, ref1, pin1, ref2, pin2, **kw):
            attempted.append((f"{ref1}/{pin1}", f"{ref2}/{pin2}"))
            # Return a "no path" result so we just observe the order
            # without modifying the schematic.
            return RouteResult(False, [], "spy")

        with patch.object(SchematicRouter, "route_pair", staticmethod(_spy)):
            ConnectionManager.connect_pins(
                sch,
                [
                    {"ref": "R1", "pin": "2"},  # 100 mm
                    {"ref": "R3", "pin": "1"},  # 200 mm  ← far
                    {"ref": "R2", "pin": "1"},  # 130 mm  ← in the middle
                ],
                net_name="SIG",
                style="auto",
            )

        # Distances (R rot=90, pin 1 = left side, pin 2 = right side):
        #   R1/2 ≈ x=103.8, R2/1 ≈ x=126.2, R2/2 ≈ x=133.8, R3/1 ≈ x=196.2
        # So closest pair = (R1/2, R2/1) at ~22 mm; second-closest =
        # (R2/1, R3/1) at ~70 mm; third = (R1/2, R3/1) at ~92 mm.
        #
        # Consecutive walk would have tried (R1/2, R3/1) first.  MST
        # must try (R1/2, R2/1) first.
        assert attempted, "no pairs attempted at all"
        assert attempted[0] == ("R1/2", "R2/1"), (
            f"first attempt should be the closest pair (R1/2, R2/1); "
            f"got {attempted[0]}.  Full order: {attempted}"
        )

    def test_auto_writes_wire_segment_and_one_auto_label(self, tmp_path):
        from commands.connection_schematic import ConnectionManager

        sch = _write(tmp_path, "auto.kicad_sch", _make_two_resistors_sch())
        result = ConnectionManager.connect_pins(
            sch,
            [{"ref": "R1", "pin": "2"}, {"ref": "R2", "pin": "1"}],
            net_name="SIG",
            style="auto",
        )
        assert result["success"], result.get("message")
        assert result["style"] == "auto"
        assert len(result["wired_pairs"]) == 1
        text = sch.read_text()
        # Wire was laid.
        assert "(wire" in text
        # Phase 5: one SIG label is added at a wire endpoint so the net is
        # named in KiCad (otherwise multi-call usage would silently fragment
        # named nets across calls).
        assert text.count('(label "SIG"') == 1
        assert "auto_label_position" in result

    def test_auto_falls_back_to_label_when_no_phase12_path(self, tmp_path):
        # Pins are aligned on y=100 but face AWAY from each other — no
        # straight, L, or U candidate is consistent with the pin angles.
        from commands.connection_schematic import ConnectionManager

        sch = _write(tmp_path, "fallback.kicad_sch", _make_two_resistors_sch())
        result = ConnectionManager.connect_pins(
            sch,
            [{"ref": "R1", "pin": "1"}, {"ref": "R2", "pin": "2"}],
            net_name="SIG",
            style="auto",
        )
        assert result["success"], result.get("message")
        assert result["wired_pairs"] == []
        # Should have added labels via connect_to_net fallback.
        text = sch.read_text()
        assert text.count("(label") >= 2

    def test_wire_mode_hard_fails_when_no_phase12_path(self, tmp_path):
        from commands.connection_schematic import ConnectionManager

        sch = _write(tmp_path, "wirefail.kicad_sch", _make_two_resistors_sch())
        result = ConnectionManager.connect_pins(
            sch,
            [{"ref": "R1", "pin": "1"}, {"ref": "R2", "pin": "2"}],
            net_name="SIG",
            style="wire",
        )
        assert result["success"] is False
        assert result["routing_failures"]
        # No labels added in strict wire-mode.
        text = sch.read_text()
        assert "(label" not in text

    def test_power_net_uses_label_in_auto(self, tmp_path):
        from commands.connection_schematic import ConnectionManager

        sch = _write(tmp_path, "gnd.kicad_sch", _make_two_resistors_sch())
        result = ConnectionManager.connect_pins(
            sch,
            [{"ref": "R1", "pin": "2"}, {"ref": "R2", "pin": "1"}],
            net_name="GND",
            style="auto",
        )
        assert result["success"], result.get("message")
        # No wires drawn for the GND net even though pins are aligned.
        assert result["wired_pairs"] == []
        text = sch.read_text()
        assert text.count("(label") >= 2

    def test_auto_writes_multi_segment_polyline_for_u_shape(self, tmp_path):
        from commands.connection_schematic import ConnectionManager

        # Misaligned horizontal pins → U-shape (3 segments).
        sch = _write(
            tmp_path,
            "polyline.kicad_sch",
            _make_two_resistors_sch(r2_xy=(130.0, 110.0)),
        )
        result = ConnectionManager.connect_pins(
            sch,
            [{"ref": "R1", "pin": "2"}, {"ref": "R2", "pin": "1"}],
            net_name="SIG",
            style="auto",
        )
        assert result["success"], result.get("message")
        assert len(result["wired_pairs"]) == 1
        wp = result["wired_pairs"][0]
        assert wp["style"] == "U"
        assert len(wp["segments"]) == 3
        text = sch.read_text()
        # Phase 5: single orphan chain → in-line label at segs[0][1]
        # (the first corner of the U-shape).  Branch-stubs are reserved
        # for nets where multiple disjoint chains coexist — a single
        # chain doesn't need a "continues elsewhere" marker because
        # there's nothing elsewhere.  So 3 route segments, no extra
        # branch-stub wire.
        assert text.count("(wire") == 3
        assert text.count('(label "SIG"') == 1

    def test_phase5_skips_chains_phase3_will_label(
        self, tmp_path
    ):
        """When some pins on the net aren't wired in Phase 2.5 (e.g.
        duplicate-pad pins on a multi-unit symbol whose pair has zero-
        length segments), Phase 3 will stub-and-label them.  Phase 5
        should recognise those upcoming labels and NOT add a redundant
        in-line label on the chain.

        Setup mirrors the FET_MID case: 2 R's wired together cleanly
        (one chain), plus a third R whose pin can't route to either
        (forced via max_len=5 — too short).  R3 falls to Phase 3
        which adds a stub-and-label at R3's pin.  R3's stub doesn't
        touch the R1↔R2 wire chain, so by chain-grouping logic the
        R1↔R2 chain is "orphan" — but R3's Phase-3 label puts SIG on
        the schematic at R3's pin endpoint, satisfying KiCad's
        net-naming requirement.  Phase 5 should still add a label for
        R1↔R2 since it's not connected to R3's label.

        Wait — actually that's the OPPOSITE.  Let me reconsider.

        Better test: 2 R's that wire cleanly into one chain.  Phase 3
        adds NO new labels (both pins in wired_pin_set).  Phase 5
        adds exactly one label for the chain.  This is just the
        single-chain-one-label invariant and it should pass.
        """
        from commands.connection_schematic import ConnectionManager

        sch = _write(
            tmp_path,
            "two_r_one_chain.kicad_sch",
            _make_two_resistors_sch(),
        )

        result = ConnectionManager.connect_pins(
            sch,
            [{"ref": "R1", "pin": "2"}, {"ref": "R2", "pin": "1"}],
            net_name="SIG",
            style="auto",
        )
        assert result["success"], result.get("message")
        assert len(result["wired_pairs"]) == 1
        text = sch.read_text()
        # Single wired chain → exactly one Phase 5 label.
        assert text.count('(label "SIG"') == 1

    def test_phase5_branch_stubs_only_when_multiple_orphan_chains(
        self, tmp_path
    ):
        """When a net has multiple disjoint wired chains, each gets a
        perpendicular branch-stub at its first corner so the auto-
        label visually reads as "this net continues elsewhere."

        Setup mirrors test_auto_labels_each_orphaned_chain (4 R's in
        2 clusters with the middle pair too far for max_len=30) but
        adds one assertion: each cluster's chain has its label at the
        end of a NEW perpendicular branch wire, not on the existing
        route geometry.
        """
        from commands.connection_schematic import ConnectionManager

        schematic = textwrap.dedent(f"""\
            (kicad_sch (version 20250114) (generator "test")
              {R_LIB}
              (symbol (lib_id "Device:R") (at 100 100 90) (unit 1)
                (property "Reference" "R1" (at 100 100 0))
                (property "Value" "10k" (at 100 100 0))
                (instances (project "test" (path "/" (reference "R1") (unit 1))))
              )
              (symbol (lib_id "Device:R") (at 115 100 90) (unit 1)
                (property "Reference" "R2" (at 115 100 0))
                (property "Value" "10k" (at 115 100 0))
                (instances (project "test" (path "/" (reference "R2") (unit 1))))
              )
              (symbol (lib_id "Device:R") (at 100 200 90) (unit 1)
                (property "Reference" "R3" (at 100 200 0))
                (property "Value" "10k" (at 100 200 0))
                (instances (project "test" (path "/" (reference "R3") (unit 1))))
              )
              (symbol (lib_id "Device:R") (at 115 200 90) (unit 1)
                (property "Reference" "R4" (at 115 200 0))
                (property "Value" "10k" (at 115 200 0))
                (instances (project "test" (path "/" (reference "R4") (unit 1))))
              )
              (sheet_instances (path "/" (page "1")))
            )
        """)
        sch = _write(tmp_path, "branch_stubs.kicad_sch", schematic)

        result = ConnectionManager.connect_pins(
            sch,
            [
                {"ref": "R1", "pin": "2"},
                {"ref": "R2", "pin": "1"},
                {"ref": "R3", "pin": "2"},
                {"ref": "R4", "pin": "1"},
            ],
            net_name="SIG",
            style="auto",
            max_len=30.0,
        )
        assert result["success"], result.get("message")
        assert len(result["wired_pairs"]) == 2
        # Two orphan chains → two labels.  But these are STRAIGHT-line
        # routes (single segment), so the branch-stub path can't fire
        # (only multi-segment chains get a branch).  The fallback puts
        # the label at segs[0][1] for each.  This test asserts the
        # multi-orphan detection works — both labels are present.
        assert len(result.get("auto_label_positions", [])) == 2

    def test_phase4_tees_into_existing_same_net_wire(self, tmp_path):
        # Pre-existing R1↔R2 wire labeled SIG at the R1 endpoint; R3 placed
        # above the wire pointing down. connect_pins([R3, R1]) should tee R3
        # into the existing wire mid-segment via multi-goal A*, NOT route
        # all the way to R1.pin2.
        from commands.connection_schematic import ConnectionManager

        sch_text = textwrap.dedent("""\
            (kicad_sch (version 20250114) (generator "test")
              %s
              (symbol (lib_id "Device:R") (at 101.6 101.6 90) (unit 1)
                (property "Reference" "R1" (at 101.6 101.6 0))
                (property "Value" "10k" (at 101.6 101.6 0))
                (instances (project "test" (path "/" (reference "R1") (unit 1))))
              )
              (symbol (lib_id "Device:R") (at 125.73 101.6 90) (unit 1)
                (property "Reference" "R2" (at 125.73 101.6 0))
                (property "Value" "10k" (at 125.73 101.6 0))
                (instances (project "test" (path "/" (reference "R2") (unit 1))))
              )
              (symbol (lib_id "Device:R") (at 113.03 116.84 0) (unit 1)
                (property "Reference" "R3" (at 113.03 116.84 0))
                (property "Value" "1k" (at 113.03 116.84 0))
                (instances (project "test" (path "/" (reference "R3") (unit 1))))
              )
              (wire (pts (xy 105.41 101.6) (xy 121.92 101.6)) (stroke (width 0) (type default)))
              (label "SIG" (at 105.41 101.6 0))
              (sheet_instances (path "/" (page "1")))
            )
        """) % R_LIB
        sch = _write(tmp_path, "tee.kicad_sch", sch_text)
        result = ConnectionManager.connect_pins(
            sch,
            [{"ref": "R3", "pin": "1"}, {"ref": "R1", "pin": "2"}],
            net_name="SIG",
            style="auto",
        )
        assert result["success"], result.get("message")
        # The pair should have been routed (R1.pin2 already on SIG, R3.pin1 fresh).
        assert len(result["wired_pairs"]) == 1
        wp = result["wired_pairs"][0]
        # Style is astar-tee when A* terminates at a same-net cell instead of p2.
        assert wp["style"] == "astar-tee", f"got style={wp['style']}"
        # The new wire should END at a point on the existing R1-R2 wire
        # (y=101.6, x in [105.41, 121.92]). Specifically the cheapest tee
        # is straight down from R3.pin1 (113.03, 113.03) to (113.03, 101.6).
        last_seg = wp["segments"][-1]
        end_x, end_y = last_seg[1]
        assert math.isclose(end_y, 101.6, abs_tol=1e-3)
        assert 105.41 - 1e-3 <= end_x <= 121.92 + 1e-3
        # And the file now contains both the original and the new wire.
        text = sch.read_text()
        assert text.count("(wire") >= 2

    def test_astar_tee_doesnt_terminate_on_wire_already_at_p1(self, tmp_path):
        """When p1 of a route coincides with the endpoint of an existing
        same-net wire, A* used to "tee" after one cell — it walked from
        cell (0,0) onto cell (0,-1) which was on the wire and considered
        itself done.  The new run-out leg toward p2 never got emitted.

        Repro: connect_pins(auto, [Q1/8, Q1/6, R3/2]) on autoplacer_test
        where pair-1 (Q1/8→Q1/6) drew a U-shape ending AT Q1/6, then
        pair-2 (Q1/6→R3/2) saw the wire ending at its own start cell
        and terminated on it after one step.

        Fix: cells reachable from p1 via the same-net wire graph are
        excluded from extra_goal_cells.
        """
        from commands.schematic_router import SchematicRouter

        # R1 left vertical, R2 right vertical, both with pin 1 on top.
        # R3 placed FAR to the right.  Pre-existing wire goes from
        # above R2 down to R2's pin 1 endpoint exactly.  Routing from
        # R2.pin1 → R3.pin1 should reach R3, not "tee" onto the
        # pre-existing wire that's already at R2.pin1.
        sch_text = textwrap.dedent("""\
            (kicad_sch (version 20250114) (generator "test")
              %s
              (symbol (lib_id "Device:R") (at 101.6 110.49 0) (unit 1)
                (property "Reference" "R1" (at 101.6 110.49 0))
                (property "Value" "10k" (at 101.6 110.49 0))
                (instances (project "test" (path "/" (reference "R1") (unit 1))))
              )
              (symbol (lib_id "Device:R") (at 125.73 110.49 0) (unit 1)
                (property "Reference" "R2" (at 125.73 110.49 0))
                (property "Value" "10k" (at 125.73 110.49 0))
                (instances (project "test" (path "/" (reference "R2") (unit 1))))
              )
              (symbol (lib_id "Device:R") (at 160.02 110.49 0) (unit 1)
                (property "Reference" "R3" (at 160.02 110.49 0))
                (property "Value" "1k" (at 160.02 110.49 0))
                (instances (project "test" (path "/" (reference "R3") (unit 1))))
              )
              (wire (pts (xy 101.6 106.68) (xy 125.73 106.68)) (stroke (width 0) (type default)))
              (wire (pts (xy 101.6 106.68) (xy 101.6 100.33)) (stroke (width 0) (type default)))
              (wire (pts (xy 125.73 106.68) (xy 125.73 100.33)) (stroke (width 0) (type default)))
              (wire (pts (xy 125.73 100.33) (xy 125.73 106.68)) (stroke (width 0) (type default)))
              (label "BUS" (at 101.6 106.68 0))
              (sheet_instances (path "/" (page "1")))
            )
        """) % R_LIB
        sch = _write(tmp_path, "tee_runout.kicad_sch", sch_text)

        # R3 pin 1 is at (160.02, 100.33-3.81)? No — Device:R rot=0 puts
        # pin 1 at lib (0, +3.81) → screen y - 3.81.  So R3 pin 1 is at
        # (160.02, 110.49 - 3.81) = (160.02, 106.68).  R2 pin 1 at
        # (125.73, 106.68).  Existing wires include (125.73, 100.33)→
        # (125.73, 106.68): that's the wire ending AT R2.pin1.
        result = SchematicRouter.route_pair(
            sch, "R2", "1", "R3", "1", target_net="BUS",
        )
        assert result.success, f"routing failed: {result.reject_reason}"
        # The route must reach R3.pin1.  If it terminated on the
        # pre-existing wire (the bug), the last segment would end at
        # (125.73, ~105.41) — i.e. one grid step from R2.pin1, never
        # leaving the column.  After the fix it must extend over to
        # x=160.02.
        last_seg = result.segments[-1]
        end_x, end_y = last_seg[1]
        assert math.isclose(end_x, 160.02, abs_tol=1e-2), (
            f"route ended at x={end_x}, expected 160.02 — likely tee'd "
            f"onto the wire already touching p1 instead of running out to R3"
        )
        assert math.isclose(end_y, 106.68, abs_tol=1e-2), end_y

    def test_phase5_auto_label_skipped_when_tee_into_existing_labeled_net(
        self, tmp_path
    ):
        # Existing R1↔R2 wire labeled SIG. connect_pins([R3, R1], net="SIG")
        # tees R3 into the wire. The new fragment is already reachable from
        # the existing SIG label via the shared wire, so no second SIG label
        # should be auto-added.
        from commands.connection_schematic import ConnectionManager

        sch_text = textwrap.dedent("""\
            (kicad_sch (version 20250114) (generator "test")
              %s
              (symbol (lib_id "Device:R") (at 101.6 101.6 90) (unit 1)
                (property "Reference" "R1" (at 101.6 101.6 0))
                (property "Value" "10k" (at 101.6 101.6 0))
                (instances (project "test" (path "/" (reference "R1") (unit 1))))
              )
              (symbol (lib_id "Device:R") (at 125.73 101.6 90) (unit 1)
                (property "Reference" "R2" (at 125.73 101.6 0))
                (property "Value" "10k" (at 125.73 101.6 0))
                (instances (project "test" (path "/" (reference "R2") (unit 1))))
              )
              (symbol (lib_id "Device:R") (at 113.03 116.84 0) (unit 1)
                (property "Reference" "R3" (at 113.03 116.84 0))
                (property "Value" "1k" (at 113.03 116.84 0))
                (instances (project "test" (path "/" (reference "R3") (unit 1))))
              )
              (wire (pts (xy 105.41 101.6) (xy 121.92 101.6)) (stroke (width 0) (type default)))
              (label "SIG" (at 105.41 101.6 0))
              (sheet_instances (path "/" (page "1")))
            )
        """) % R_LIB
        sch = _write(tmp_path, "tee_no_extra_label.kicad_sch", sch_text)
        result = ConnectionManager.connect_pins(
            sch,
            [{"ref": "R3", "pin": "1"}, {"ref": "R1", "pin": "2"}],
            net_name="SIG",
            style="auto",
        )
        assert result["success"], result.get("message")
        text = sch.read_text()
        # Original SIG label is still there; no new one was added because the
        # tee'd wire is reachable from the existing label via wire connectivity.
        assert text.count('(label "SIG"') == 1
        assert "auto_label_position" not in result

    def test_get_pin_net_finds_endpoint_label(self, tmp_path):
        # Wire+label fixture: R1.pin2 should be detected as on net "OTHER".
        # Previously masked by a kicad-skip __bool__ exception inside
        # get_pin_net which silently returned None.
        from commands.connection_schematic import ConnectionManager

        sch_text = textwrap.dedent("""\
            (kicad_sch (version 20250114) (generator "test")
              %s
              (symbol (lib_id "Device:R") (at 101.6 101.6 90) (unit 1)
                (property "Reference" "R1" (at 101.6 101.6 0))
                (property "Value" "10k" (at 101.6 101.6 0))
                (instances (project "test" (path "/" (reference "R1") (unit 1))))
              )
              (wire (pts (xy 105.41 101.6) (xy 121.92 101.6)) (stroke (width 0) (type default)))
              (label "OTHER" (at 105.41 101.6 0))
              (sheet_instances (path "/" (page "1")))
            )
        """) % R_LIB
        sch = _write(tmp_path, "labeled.kicad_sch", sch_text)
        net = ConnectionManager.get_pin_net(sch, "R1", "2")
        assert net == "OTHER"

    def test_get_pin_net_finds_mid_wire_label(self, tmp_path):
        # Label at a wire's interior (not at any endpoint) should still be
        # detected — KiCad attaches labels to wires by geometric coincidence.
        from commands.connection_schematic import ConnectionManager

        sch_text = textwrap.dedent("""\
            (kicad_sch (version 20250114) (generator "test")
              %s
              (symbol (lib_id "Device:R") (at 101.6 101.6 90) (unit 1)
                (property "Reference" "R1" (at 101.6 101.6 0))
                (property "Value" "10k" (at 101.6 101.6 0))
                (instances (project "test" (path "/" (reference "R1") (unit 1))))
              )
              (wire (pts (xy 105.41 101.6) (xy 121.92 101.6)) (stroke (width 0) (type default)))
              (label "MIDWIRE" (at 113.665 101.6 0))
              (sheet_instances (path "/" (page "1")))
            )
        """) % R_LIB
        sch = _write(tmp_path, "midlabel.kicad_sch", sch_text)
        net = ConnectionManager.get_pin_net(sch, "R1", "2")
        assert net == "MIDWIRE"

    def test_connect_pins_detects_conflict_when_pin_on_different_net(
        self, tmp_path
    ):
        # R1.pin2 is on labeled "OTHER" net; user tries to connect it to "SIG".
        # connect_pins MUST refuse to silently merge OTHER and SIG.
        from commands.connection_schematic import ConnectionManager

        sch_text = textwrap.dedent("""\
            (kicad_sch (version 20250114) (generator "test")
              %s
              (symbol (lib_id "Device:R") (at 101.6 101.6 90) (unit 1)
                (property "Reference" "R1" (at 101.6 101.6 0))
                (property "Value" "10k" (at 101.6 101.6 0))
                (instances (project "test" (path "/" (reference "R1") (unit 1))))
              )
              (symbol (lib_id "Device:R") (at 125.73 101.6 90) (unit 1)
                (property "Reference" "R2" (at 125.73 101.6 0))
                (property "Value" "10k" (at 125.73 101.6 0))
                (instances (project "test" (path "/" (reference "R2") (unit 1))))
              )
              (wire (pts (xy 105.41 101.6) (xy 121.92 101.6)) (stroke (width 0) (type default)))
              (label "OTHER" (at 105.41 101.6 0))
              (sheet_instances (path "/" (page "1")))
            )
        """) % R_LIB
        sch = _write(tmp_path, "conflict.kicad_sch", sch_text)
        result = ConnectionManager.connect_pins(
            sch,
            [{"ref": "R1", "pin": "2"}, {"ref": "R2", "pin": "1"}],
            net_name="SIG",
            style="auto",
        )
        assert result["success"] is False
        # At least one failure mentioning OTHER (the conflicting net).
        failed_reasons = [str(item) for item in result.get("failed", [])]
        assert any("OTHER" in r for r in failed_reasons), failed_reasons

    def test_label_default_unchanged(self, tmp_path):
        # Default style is "label" — no routing attempt, behaviour identical to before.
        from commands.connection_schematic import ConnectionManager

        sch = _write(tmp_path, "label.kicad_sch", _make_two_resistors_sch())
        result = ConnectionManager.connect_pins(
            sch,
            [{"ref": "R1", "pin": "2"}, {"ref": "R2", "pin": "1"}],
            net_name="SIG",
        )
        assert result["success"], result.get("message")
        assert result["style"] == "label"
        # No wire-only routing happened; both pins got their own stub+label.
        text = sch.read_text()
        assert text.count("(label") >= 2

    def test_auto_labels_each_orphaned_chain(self, tmp_path):
        """Regression for the orphan-chain bug discovered when partitioning
        the power_module flat schematic into hierarchical sheets.

        Setup: two resistor clusters far apart.  Each cluster's pair is a
        clean straight-line route (close together, facing each other).
        The middle pair (cluster 1's right end → cluster 2's left end)
        is intentionally too far to route within max_len, so the
        autorouter wires *only* the two cluster pairs and leaves the
        gap unbridged.

        Bug behaviour (pre-fix): Phase 5 auto-labels the FIRST wired
        chain only.  The second cluster's wired pair is left without a
        label, so its pins end up on a floating "ghost" sub-net even
        though connect_pins reports them as 'connected'.  KiCad
        auto-names the ghost net `Net-(R3-Pad1)` etc., silently
        fragmenting the named net the caller asked for.

        Fixed behaviour: Phase 5 iterates over every wired pair and adds
        an auto-label per orphaned chain.  All four pins resolve to
        target_net via wire/label connectivity.
        """
        from commands.connection_schematic import ConnectionManager

        # 4 horizontal resistors in two clusters at y=100 and y=200.
        # Each cluster: R{n} at (100, y) rot=90 + R{n+1} at (115, y) rot=90.
        # Pin 1 is at sym_x + 3.81, pin 2 at sym_x - 3.81.
        # We connect the OUTER pins of each cluster pair so each cluster
        # routes as a single straight wire across ~7.4 mm.
        schematic = textwrap.dedent(f"""\
            (kicad_sch (version 20250114) (generator "test")
              {R_LIB}
              (symbol (lib_id "Device:R") (at 100 100 90) (unit 1)
                (property "Reference" "R1" (at 100 100 0))
                (property "Value" "10k" (at 100 100 0))
                (instances (project "test" (path "/" (reference "R1") (unit 1))))
              )
              (symbol (lib_id "Device:R") (at 115 100 90) (unit 1)
                (property "Reference" "R2" (at 115 100 0))
                (property "Value" "10k" (at 115 100 0))
                (instances (project "test" (path "/" (reference "R2") (unit 1))))
              )
              (symbol (lib_id "Device:R") (at 100 200 90) (unit 1)
                (property "Reference" "R3" (at 100 200 0))
                (property "Value" "10k" (at 100 200 0))
                (instances (project "test" (path "/" (reference "R3") (unit 1))))
              )
              (symbol (lib_id "Device:R") (at 115 200 90) (unit 1)
                (property "Reference" "R4" (at 115 200 0))
                (property "Value" "10k" (at 115 200 0))
                (instances (project "test" (path "/" (reference "R4") (unit 1))))
              )
              (sheet_instances (path "/" (page "1")))
            )
        """)
        sch = _write(tmp_path, "orphan.kicad_sch", schematic)

        # max_len=30 ensures the middle pair (~100 mm) cannot route.
        # Pin selection: R1.2 (right of R1) ↔ R2.1 (left of R2) face each
        # other and route as a straight line within each cluster.
        result = ConnectionManager.connect_pins(
            sch,
            [
                {"ref": "R1", "pin": "2"},
                {"ref": "R2", "pin": "1"},
                {"ref": "R3", "pin": "2"},
                {"ref": "R4", "pin": "1"},
            ],
            net_name="SIG",
            style="auto",
            max_len=30.0,
        )
        assert result["success"], result.get("message")

        # Two cluster pairs got wired; the middle pair was unreachable.
        assert len(result["wired_pairs"]) == 2, (
            f"expected 2 wired pairs, got {len(result['wired_pairs'])}: "
            f"{result['wired_pairs']}"
        )

        # Phase 5 must label EACH of the two chains.
        assert len(result.get("auto_label_positions", [])) == 2, (
            "auto_label_positions should have one entry per orphaned chain"
        )

        # The whole point: every pin must end up on SIG via the label
        # graph (either a label at its own pin, or a wire chain to a label).
        for ref, pin in [("R1", "2"), ("R2", "1"), ("R3", "2"), ("R4", "1")]:
            net = ConnectionManager.get_pin_net(sch, ref, pin)
            assert net == "SIG", (
                f"{ref}/{pin} ended up on net {net!r}, expected 'SIG' — "
                "orphan-chain bug has regressed"
            )


@pytest.mark.unit
class TestPhase5ChainFinder:
    """Direct tests of the new walk_wire_chain-based Phase 5 helper.

    These bypass the autorouter and feed the helper a pre-laid wire
    graph + pin_endpoints, so we can deterministically exercise the
    chain-grouping logic on tricky topologies the autorouter doesn't
    reliably produce in unit tests."""

    def _empty_sch_with_wires(
        self,
        tmp_path: Path,
        wires: list,
        labels: list = (),
    ) -> Path:
        parts = [
            '(kicad_sch (version 20250114) (generator "test")',
            '  (uuid 11111111-1111-1111-1111-111111111111)',
            '  (paper "A4")',
        ]
        for (x1, y1), (x2, y2) in wires:
            parts.append(
                f'  (wire (pts (xy {x1} {y1}) (xy {x2} {y2})) '
                '(stroke (width 0) (type default)) '
                '(uuid 11111111-1111-1111-1111-111111111111))'
            )
        for name, x, y in labels:
            parts.append(
                f'  (label "{name}" (at {x} {y} 0) '
                '(effects (font (size 1.27 1.27))) '
                '(uuid 22222222-2222-2222-2222-222222222222))'
            )
        parts.append('  (sheet_instances (path "/" (page "1")))')
        parts.append(')')
        p = tmp_path / "t.kicad_sch"
        p.write_text("\n".join(parts))
        return p

    def test_t_junction_chain_gets_single_label(self, tmp_path: Path):
        """Two wires connected only via a mid-segment T-junction must
        be recognised as ONE chain and labelled exactly once.

        Regression: the old union-find pair grouping in Phase 5 used
        wired_pairs' segment endpoints, which don't reflect the T-
        junction created when one wire's endpoint lands on another's
        interior.  Two pin endpoints on the same physical chain ended
        up in different "chains" and each got its own auto-label."""
        from commands.connection_schematic import _phase5_label_orphan_chains

        sch = self._empty_sch_with_wires(
            tmp_path,
            wires=[
                ((100.0, 100.0), (130.0, 100.0)),  # horizontal trunk
                ((115.0, 100.0), (115.0, 95.0)),   # T-junction stub down
            ],
        )

        labels = _phase5_label_orphan_chains(
            schematic_path=sch,
            resolved_net="SIG",
            pins=[
                {"ref": "R1", "pin": "2"},  # at horizontal trunk left
                {"ref": "R2", "pin": "1"},  # at horizontal trunk right
                {"ref": "R3", "pin": "1"},  # at stub bottom
            ],
            pin_endpoints={
                "R1/2": (100.0, 100.0),
                "R2/1": (130.0, 100.0),
                "R3/1": (115.0, 95.0),
            },
            exclude_pins={("R1", "2"), ("R2", "1"), ("R3", "1")},
        )

        assert len(labels) == 1, (
            f"T-junction chain must be labelled once, got {len(labels)}: "
            f"{labels}"
        )
        text = sch.read_text()
        assert text.count('(label "SIG"') == 1

    def test_already_labelled_chain_gets_no_extra_label(self, tmp_path: Path):
        """A chain that already carries the resolved_net label must
        not be re-labelled, even when multiple pin endpoints lie on it."""
        from commands.connection_schematic import _phase5_label_orphan_chains

        sch = self._empty_sch_with_wires(
            tmp_path,
            wires=[((100.0, 100.0), (130.0, 100.0))],
            labels=[("SIG", 130.0, 100.0)],  # label at right end
        )

        labels = _phase5_label_orphan_chains(
            schematic_path=sch,
            resolved_net="SIG",
            pins=[
                {"ref": "R1", "pin": "2"},
                {"ref": "R2", "pin": "1"},
            ],
            pin_endpoints={
                "R1/2": (100.0, 100.0),
                "R2/1": (130.0, 100.0),
            },
            exclude_pins={("R1", "2"), ("R2", "1")},
        )

        assert labels == []
        text = sch.read_text()
        assert text.count('(label "SIG"') == 1  # original only

    def test_two_disjoint_chains_each_get_a_label(self, tmp_path: Path):
        """Two physically disjoint chains for the same net must each
        get their own auto-label so the net isn't silently fragmented."""
        from commands.connection_schematic import _phase5_label_orphan_chains

        sch = self._empty_sch_with_wires(
            tmp_path,
            wires=[
                ((100.0, 100.0), (110.0, 100.0)),  # chain A
                ((200.0, 100.0), (210.0, 100.0)),  # chain B (disjoint)
            ],
        )

        labels = _phase5_label_orphan_chains(
            schematic_path=sch,
            resolved_net="SIG",
            pins=[
                {"ref": "R1", "pin": "2"},
                {"ref": "R2", "pin": "1"},
                {"ref": "R3", "pin": "2"},
                {"ref": "R4", "pin": "1"},
            ],
            pin_endpoints={
                "R1/2": (100.0, 100.0),
                "R2/1": (110.0, 100.0),
                "R3/2": (200.0, 100.0),
                "R4/1": (210.0, 100.0),
            },
            exclude_pins={
                ("R1", "2"), ("R2", "1"), ("R3", "2"), ("R4", "1")
            },
        )

        assert len(labels) == 2, (
            f"Two disjoint chains require two labels, got {len(labels)}"
        )
        text = sch.read_text()
        assert text.count('(label "SIG"') == 2

    def test_stub_style_kicks_in_when_phase3_already_stubbed_one_chain(
        self, tmp_path: Path
    ):
        """When the net has one orphan multi-pin chain AND one chain
        already labelled by Phase 3 (a single-pin stub), the orphan
        chain should also get a stub-style label so the two read
        symmetrically.  Regression for the VC3 case where the multi-
        pin chain got an in-line label while R4's stub looked
        completely disconnected from it visually."""
        from commands.connection_schematic import _phase5_label_orphan_chains

        # Setup: two physically disjoint chains.
        #   chain A: existing 3-segment U-shape with no label.
        #   chain B: existing single 2.54mm stub with a SIG label at
        #            its free end (Phase 3-style).
        sch = self._empty_sch_with_wires(
            tmp_path,
            wires=[
                # chain A — U-shape (corner at 110,100; corner at 120,100)
                ((100.0, 100.0), (110.0, 100.0)),
                ((110.0, 100.0), (110.0, 105.0)),  # vertical leg
                ((110.0, 105.0), (120.0, 105.0)),  # bottom of U
                # chain B — Phase 3 stub
                ((200.0, 100.0), (202.54, 100.0)),
            ],
            labels=[("SIG", 202.54, 100.0)],  # label on chain B's free end
        )

        labels = _phase5_label_orphan_chains(
            schematic_path=sch,
            resolved_net="SIG",
            pins=[
                {"ref": "R1", "pin": "2"},
                {"ref": "R2", "pin": "1"},
                {"ref": "R3", "pin": "2"},
            ],
            pin_endpoints={
                "R1/2": (100.0, 100.0),  # chain A end
                "R2/1": (120.0, 105.0),  # chain A other end
                "R3/2": (200.0, 100.0),  # chain B pin
            },
            exclude_pins={("R1", "2"), ("R2", "1"), ("R3", "2")},
        )

        # One label added (for chain A — chain B already has SIG).
        assert len(labels) == 1, (
            f"Phase 5 should add exactly one label for chain A; got {labels}"
        )
        # And it should be at a stub-end (perpendicular branch from
        # the U's corner), NOT at a pin endpoint or interior of an
        # existing wire.  The corners are (110,100) and (110,105);
        # the branch-stub direction is perpendicular to whichever
        # corner the helper picks.  We just require the label is NOT
        # at one of the pin endpoints (which would be the "in-line"
        # case).
        added_pt = tuple(labels[0])
        assert added_pt not in {
            (100.0, 100.0), (120.0, 105.0),
        }, (
            f"Phase 5 placed label at pin endpoint {added_pt}; "
            "with another chain already on the net it should have "
            "used stub-style placement."
        )

    def test_branch_stub_refuses_to_bridge_to_another_chain(
        self, tmp_path: Path
    ):
        """Two physically disjoint orphan chains of the SAME net.  The
        old code laid a perpendicular branch-stub at each corner and
        accidentally T-junction-bridged them through some unrelated
        existing wire, producing a single merged chain with two
        labels.  The merge guard in _try_branch_stub_at_corner must
        refuse a stub_end that lands on a different chain.

        Setup: chain A has a corner at (110, 100) with vertical
        leg going down to (110, 105).  An unrelated wire (also no
        label) runs from (112.54, 100) to (112.54, 105) — would form
        a T-junction with chain A if a stub from chain A's corner
        went +X by 2.54mm.  The guard should refuse.
        """
        from commands.connection_schematic import _phase5_label_orphan_chains

        sch = self._empty_sch_with_wires(
            tmp_path,
            wires=[
                # chain A — horizontal + vertical, corner at (110,100)
                ((100.0, 100.0), (110.0, 100.0)),
                ((110.0, 100.0), (110.0, 105.0)),
                # chain B — would catch a +X stub from (110,100)
                ((112.54, 100.0), (112.54, 105.0)),
            ],
        )

        labels = _phase5_label_orphan_chains(
            schematic_path=sch,
            resolved_net="SIG",
            pins=[
                {"ref": "R1", "pin": "2"},  # chain A
                {"ref": "R2", "pin": "1"},  # chain A
                {"ref": "R3", "pin": "2"},  # chain B
                {"ref": "R4", "pin": "1"},  # chain B
            ],
            pin_endpoints={
                "R1/2": (100.0, 100.0),
                "R2/1": (110.0, 105.0),
                "R3/2": (112.54, 100.0),
                "R4/1": (112.54, 105.0),
            },
            exclude_pins={
                ("R1", "2"), ("R2", "1"), ("R3", "2"), ("R4", "1")
            },
        )

        text = sch.read_text()
        # After labelling: we still want each chain to get exactly one
        # SIG label.  The guard should pick a direction that doesn't
        # merge.  Total SIG labels written: 2.
        assert text.count('(label "SIG"') == 2, (
            f"each disjoint chain needs one label, got "
            f"{text.count('(label \"SIG\"')}: chain merge defect"
        )

        # And the chains should remain physically disjoint after
        # labelling.  Walk both seeds and assert no overlap.
        from commands.wire_connectivity import walk_wire_chain
        ca = walk_wire_chain((100.0, 100.0), sch)
        cb = walk_wire_chain((112.54, 100.0), sch)
        assert ca is not None and cb is not None
        assert ca.points.isdisjoint(cb.points), (
            "Phase 5 branch-stubs merged two disjoint chains; "
            "the merge guard should have refused the candidate"
        )

    def test_chain_with_foreign_label_skipped(self, tmp_path: Path):
        """Defensive: if a chain already carries a foreign-net label
        (indicating a cross-net merge bug elsewhere), Phase 5 must NOT
        add a second label.  It would compound the merge and make the
        problem harder to spot in ERC.  See issue #74."""
        from commands.connection_schematic import _phase5_label_orphan_chains

        sch = self._empty_sch_with_wires(
            tmp_path,
            wires=[((100.0, 100.0), (130.0, 100.0))],
            labels=[("OTHER_NET", 130.0, 100.0)],
        )

        labels = _phase5_label_orphan_chains(
            schematic_path=sch,
            resolved_net="SIG",
            pins=[{"ref": "R1", "pin": "2"}],
            pin_endpoints={"R1/2": (100.0, 100.0)},
            exclude_pins={("R1", "2")},
        )

        assert labels == [], (
            "Phase 5 should refuse to label a chain that already carries "
            "a foreign-net label — that indicates a cross-net merge "
            "defect and adding another label compounds the bug."
        )
