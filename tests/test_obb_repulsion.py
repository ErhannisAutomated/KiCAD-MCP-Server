"""Unit tests for the OBB-via-SAT distance + cubic-ramp repulsion.

The PCB autoplacer uses ``obb_repulsion_force`` for body-aware
component repulsion.  Tests cover the physics invariants:
  - Far apart → zero force.
  - Within margin → force ramps cubically.
  - Overlapping → force ratio > 1, force magnitude scales as cube.
  - Equal and opposite (Newton's third law via two symmetric calls).
  - Rotation invariance for co-rotated pairs.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

PYTHON_DIR = Path(__file__).parent.parent / "python"
sys.path.insert(0, str(PYTHON_DIR))


@pytest.mark.unit
class TestOBBSeparation:
    def test_aabb_far_apart_positive_gap(self):
        from commands.autoplacer import obb_separation
        # Two 4x4 squares, centers (0,0) and (20,0). Gap = 20 - 2 - 2 = 16.
        gap, axis = obb_separation(0, 0, 4, 4, 0, 20, 0, 4, 4, 0)
        assert gap == pytest.approx(16.0)
        assert axis[0] == pytest.approx(1.0)
        assert axis[1] == pytest.approx(0.0, abs=1e-9)

    def test_aabb_overlapping_negative_gap(self):
        from commands.autoplacer import obb_separation
        # 4x4 at (0,0), 4x4 at (3,0). Overlap depth = 4 + 0 - 3 = 1, gap = -1.
        gap, axis = obb_separation(0, 0, 4, 4, 0, 3, 0, 4, 4, 0)
        assert gap == pytest.approx(-1.0)
        assert axis[0] == pytest.approx(1.0)

    def test_aabb_touching_zero_gap(self):
        from commands.autoplacer import obb_separation
        # 4x4 at (0,0), 4x4 at (4,0). Touching edge-to-edge.
        gap, _ = obb_separation(0, 0, 4, 4, 0, 4, 0, 4, 4, 0)
        assert gap == pytest.approx(0.0)

    def test_orientation_invariance(self):
        """Rotating BOTH shapes by the same amount doesn't change the
        separation gap (the world just rotates with them).

        Convention: ``angle_deg`` is screen-Y-down CCW (KiCad's footprint
        orientation), so a +45° rotation of (10, 0) lands at
        (10·cos45°, -10·sin45°) = (7.07, -7.07), not (+7.07, +7.07).
        """
        from commands.autoplacer import obb_separation
        gap_0, _ = obb_separation(0, 0, 4, 2, 0, 10, 0, 4, 2, 0)
        gap_45, _ = obb_separation(0, 0, 4, 2, 45, 7.07, -7.07, 4, 2, 45)
        assert gap_0 == pytest.approx(gap_45, abs=0.01)

    def test_aabb_axis_points_a_to_b(self):
        from commands.autoplacer import obb_separation
        # B is north of A
        _, axis = obb_separation(0, 0, 4, 4, 0, 0, 10, 4, 4, 0)
        assert axis[0] == pytest.approx(0.0, abs=1e-9)
        assert axis[1] == pytest.approx(1.0)
        # B is west of A
        _, axis = obb_separation(0, 0, 4, 4, 0, -10, 0, 4, 4, 0)
        assert axis[0] == pytest.approx(-1.0)
        assert axis[1] == pytest.approx(0.0, abs=1e-9)


@pytest.mark.unit
class TestOBBRotationConvention:
    """Regression: ``_obb_corners`` must use the same screen-Y-down CCW
    rotation convention as ``Component.world_pin_xy`` and the
    viz's ``angle=-c.rotation``.  Without the fix, an asymmetric
    bbox at a non-90° rotation was MIRRORED across the X axis
    relative to the drawn rectangle, so a partner sitting visibly
    inside the drawn body got reported as separated.

    Case: the L1 + R10 layout from power_module — L1 = 9.20×3.20 at
    315.5°, R10 = 4.68×1.75 at 320.3°, centers 4.36mm apart and
    R10 visibly inside L1's drawn rectangle.  Under the buggy
    convention, gap was +1.61mm; under the correct one, gap is
    negative (penetration).
    """

    def test_l1_r10_penetration_detected(self):
        from commands.autoplacer import obb_separation
        # L1: 9.20 x 3.20 at 315.5°, center (46.45, 7.66)
        # R10: 4.68 x 1.75 at 320.3°, center (48.86, 11.29)
        gap, _ = obb_separation(
            46.45, 7.66, 9.20, 3.20, 315.5,
            48.86, 11.29, 4.68, 1.75, 320.3,
        )
        assert gap < 0.0, (
            f"R10 visibly inside L1's drawn rectangle, but gap reported "
            f"as {gap:.3f} — OBB rotation convention bug regressed"
        )

    def test_corner_position_matches_viz_for_315_5_deg(self):
        """The +hw, +hh corner of a 9.20×3.20 OBB at 315.5° centered at
        (46.45, 7.66) — should match the matplotlib Rectangle drawn with
        angle=-315.5°.  Hand-computed expected: (48.61, 12.03).
        """
        from commands.autoplacer import _obb_corners
        corners = _obb_corners(46.45, 7.66, 9.20, 3.20, 315.5)
        # corners[1] is (+hw, +hh) per _obb_corners' local order
        corner_hw_hh = corners[1]
        assert corner_hw_hh[0] == pytest.approx(48.61, abs=0.05)
        assert corner_hw_hh[1] == pytest.approx(12.03, abs=0.05)


@pytest.mark.unit
class TestComponentObbCenterWorld:
    """Component.obb_center_world() rotates the (bbox_cx, bbox_cy)
    offset by c.rotation and adds to (c.x, c.y).  Same rotation
    convention as world_pin_xy (screen-Y-down CCW positive)."""

    def _make_comp(self, bbox_cx=0.0, bbox_cy=0.0, rotation=0.0,
                   x=0.0, y=0.0):
        from commands.autoplacer import Component
        return Component(
            ref="J1", unit=1, lib_id="x:y",
            x=x, y=y, rotation=rotation,
            mirror_x=False, mirror_y=False,
            bbox_w=20.0, bbox_h=3.0,
            bbox_cx=bbox_cx, bbox_cy=bbox_cy,
            coord_system="pcb", layer="F.Cu",
        )

    def test_zero_offset_returns_origin(self):
        c = self._make_comp(x=5.0, y=7.0)
        bx, by = c.obb_center_world()
        assert (bx, by) == (5.0, 7.0)

    def test_nonzero_offset_unrotated(self):
        # Pin header anchored at pin 1 (origin); body center 10mm east.
        c = self._make_comp(bbox_cx=10.0, x=5.0, y=7.0)
        bx, by = c.obb_center_world()
        assert bx == pytest.approx(15.0)
        assert by == pytest.approx(7.0)

    def test_offset_rotated_90deg(self):
        """At rotation=90° (screen-CCW), an east-pointing offset
        rotates to point... north (in screen Y-down, north is -y)."""
        c = self._make_comp(bbox_cx=10.0, rotation=90.0, x=5.0, y=7.0)
        bx, by = c.obb_center_world()
        assert bx == pytest.approx(5.0, abs=1e-6)
        assert by == pytest.approx(-3.0, abs=1e-6)  # 7 - 10

    def test_offset_rotated_180deg(self):
        c = self._make_comp(bbox_cx=10.0, rotation=180.0, x=5.0, y=7.0)
        bx, by = c.obb_center_world()
        assert bx == pytest.approx(-5.0)
        assert by == pytest.approx(7.0, abs=1e-6)


@pytest.mark.unit
class TestOffsetBboxOBBSeparation:
    """End-to-end: a pin-header-style off-center bbox produces the
    expected gap when its body overlaps a neighbor that the
    footprint origin (pin 1) wouldn't.  Pre-fix: gap calc against
    (c.x, c.y) showed huge separation while the body actually
    overlapped the neighbor."""

    def test_pin_header_body_overlap_detected(self):
        from commands.autoplacer import Component, obb_separation
        # J2: 20×3 pin header, anchored at pin 1 (origin at left edge).
        # Body extends from x=0 to x=20 in local coords; center at +10.
        j2 = Component(
            ref="J2", unit=1, lib_id="x:y",
            x=0.0, y=0.0, rotation=0.0,
            mirror_x=False, mirror_y=False,
            bbox_w=20.0, bbox_h=3.0,
            bbox_cx=10.0, bbox_cy=0.0,
            coord_system="pcb", layer="F.Cu",
        )
        # A small chip 12mm east of pin 1 — well inside J2's body.
        r = Component(
            ref="R", unit=1, lib_id="x:y",
            x=12.0, y=0.0, rotation=0.0,
            mirror_x=False, mirror_y=False,
            bbox_w=2.0, bbox_h=1.0,
            coord_system="pcb", layer="F.Cu",
        )
        # Use OBB centers, not raw (x, y).
        jx, jy = j2.obb_center_world()
        rx, ry = r.obb_center_world()
        gap, _ = obb_separation(
            jx, jy, j2.bbox_w, j2.bbox_h, j2.rotation,
            rx, ry, r.bbox_w, r.bbox_h, r.rotation,
        )
        assert gap < 0.0, (
            f"chip is inside pin header body, expected penetration; "
            f"gap reported {gap:.3f} mm"
        )


@pytest.mark.unit
class TestOBBRepulsionForce:
    def test_zero_force_when_far_apart(self):
        from commands.autoplacer import obb_repulsion_force
        fx, fy = obb_repulsion_force(
            0, 0, 4, 4, 0, 20, 0, 4, 4, 0,
            margin=1.0, k=10.0,
        )
        assert fx == 0.0 and fy == 0.0

    def test_zero_force_at_margin_boundary(self):
        from commands.autoplacer import obb_repulsion_force
        # 4x4 at (0,0), 4x4 at (4+margin, 0). Gap == margin, ratio = 0.
        fx, fy = obb_repulsion_force(
            0, 0, 4, 4, 0, 5, 0, 4, 4, 0,
            margin=1.0, k=10.0,
        )
        assert fx == pytest.approx(0.0)
        assert fy == pytest.approx(0.0, abs=1e-9)

    def test_force_at_half_margin(self):
        from commands.autoplacer import obb_repulsion_force
        # gap = 0.5, margin = 1.0 → ratio = 0.5, F = k * 0.5^3 = k * 0.125.
        fx, fy = obb_repulsion_force(
            0, 0, 4, 4, 0, 4.5, 0, 4, 4, 0,
            margin=1.0, k=10.0,
        )
        assert fx == pytest.approx(-10.0 * 0.125, abs=1e-6)  # A pushed −x
        assert fy == pytest.approx(0.0, abs=1e-9)

    def test_force_at_contact(self):
        """gap = 0, ratio = 1, F = k * 1.0^3 = k."""
        from commands.autoplacer import obb_repulsion_force
        fx, fy = obb_repulsion_force(
            0, 0, 4, 4, 0, 4, 0, 4, 4, 0,
            margin=1.0, k=10.0,
        )
        assert fx == pytest.approx(-10.0, abs=1e-6)
        assert fy == pytest.approx(0.0, abs=1e-9)

    def test_force_grows_cube_on_penetration(self):
        from commands.autoplacer import obb_repulsion_force
        # gap = -1, margin = 1 → ratio = 2, F = k * 8.
        fx, _ = obb_repulsion_force(
            0, 0, 4, 4, 0, 3, 0, 4, 4, 0,
            margin=1.0, k=10.0,
        )
        assert fx == pytest.approx(-80.0, abs=1e-6)

    def test_equal_and_opposite(self):
        """Force(A,B) == −Force(B,A) — Newton's third law."""
        from commands.autoplacer import obb_repulsion_force
        params = dict(margin=1.0, k=10.0)
        f_a = obb_repulsion_force(
            0, 0, 4, 4, 0, 4.5, 0, 4, 4, 0, **params,
        )
        f_b = obb_repulsion_force(
            4.5, 0, 4, 4, 0, 0, 0, 4, 4, 0, **params,
        )
        assert f_a[0] == pytest.approx(-f_b[0], abs=1e-9)
        assert f_a[1] == pytest.approx(-f_b[1], abs=1e-9)

    def test_corner_corner_diagonal_approach(self):
        """Two AABBs offset diagonally — SAT gives a (conservative)
        positive gap, so force kicks in within margin.  This is the
        case the user flagged would benefit from torque eventually;
        for now just verify it produces a nonzero, repulsive force."""
        from commands.autoplacer import obb_repulsion_force
        # 4x4 at (0,0), 4x4 at (5, 5). SAT gap along each axis = 5-4 = 1.
        fx, fy = obb_repulsion_force(
            0, 0, 4, 4, 0, 5, 5, 4, 4, 0,
            margin=2.0, k=10.0,
        )
        # ratio = 1 - 1/2 = 0.5; F = 10 * 0.125 = 1.25
        # Force direction: SAT axis is along one of the two axes (whichever
        # tied), say +x or +y, so force is in −x or −y on A.  Either way
        # the force is nonzero and points "away from B" (toward origin
        # quadrant).
        assert (fx <= 0 and fy <= 0)  # B is NE of A; A pushed SW
        mag = math.hypot(fx, fy)
        assert mag == pytest.approx(1.25, abs=1e-6)

    def test_rotated_pair_force_magnitude_same_as_axis_aligned(self):
        """Rotating both shapes 45° together should produce the same
        force magnitude — only direction changes with the world frame."""
        from commands.autoplacer import obb_repulsion_force
        # AABB case: gap = 0.5, F magnitude = k * 0.5^3 * k.
        f_aa = obb_repulsion_force(
            0, 0, 4, 4, 0, 4.5, 0, 4, 4, 0,
            margin=1.0, k=10.0,
        )
        mag_aa = math.hypot(*f_aa)
        # Same case, both rotated 45°.  Center of B is at distance 4.5
        # along the rotated x-axis: (4.5*cos45, 4.5*sin45).
        cx_b = 4.5 * math.cos(math.radians(45))
        cy_b = 4.5 * math.sin(math.radians(45))
        f_rot = obb_repulsion_force(
            0, 0, 4, 4, 45, cx_b, cy_b, 4, 4, 45,
            margin=1.0, k=10.0,
        )
        mag_rot = math.hypot(*f_rot)
        assert mag_aa == pytest.approx(mag_rot, abs=1e-6)
