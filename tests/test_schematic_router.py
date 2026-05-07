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
    _collinear_and_facing,
    _l_shape_candidates,
    _on_grid,
    _opposite_dir,
    _point_on_segment,
    _segment_length,
    _segments_collinear_overlap,
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

    def test_wire_crossing_perpendicular_allowed(self):
        # A perpendicular wire whose interior crosses our segment but whose
        # endpoints don't land on us is a valid visual crossing — no junction.
        seg = ((0.0, 50.0), (20.0, 50.0))
        obs = Obstacles(
            other_pins=[],
            other_labels=[],
            other_wires=[((10.0, 40.0), (10.0, 60.0))],
        )
        assert (
            check_spurious_connections(
                [seg], obs, target_net="N", own_endpoints=[(0.0, 50.0), (20.0, 50.0)]
            )
            is None
        )


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

    def test_auto_writes_wire_segment_to_file(self, tmp_path):
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
        # Confirm a wire shows up in the schematic file.
        text = sch.read_text()
        assert "(wire" in text
        # And no SIG label was added (auto preferred wire over label)
        assert '"SIG"' not in text and "(label SIG" not in text

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
        assert text.count("(wire") == 3
        # No SIG label was added — the U-shape covered the whole net.
        assert '"SIG"' not in text

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
