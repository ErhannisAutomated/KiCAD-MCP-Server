"""Regression tests for the 2026-05-21 v2 fixes:

* ``_footprint_local_bbox_mm`` returns rotation-independent bbox
  dimensions (was previously using the world AABB at the current
  rotation, causing rotated footprints to display with their bbox
  visually perpendicular to their pins).
* ``cross_layer_springs=False`` skips F.Cu↔B.Cu spring forces.
* Pin-wise springs produce lever-arm torque under
  ``use_spring_classes`` + ``pinwise_torque_k > 0``.
* ``obb_separation`` axis tie-break is stable: when two separating
  axes have equal gaps, the one most aligned with the center-to-center
  direction wins so micro-drift can't flip the force direction.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

PYTHON_DIR = Path(__file__).parent.parent / "python"
sys.path.insert(0, str(PYTHON_DIR))


def _real_pcbnew_available() -> bool:
    import pcbnew  # type: ignore
    return getattr(pcbnew, "GetBuildVersion", lambda: "")() != "9.0.0-stub"


@pytest.mark.unit
class TestObbSeparationStableTieBreak:
    def test_equal_gaps_choose_center_to_center_aligned_axis(self):
        """Two squares offset diagonally — both A's axes have the same
        projection gap.  The stable tie-break must pick the axis closer
        to the A→B center-to-center direction."""
        from commands.autoplacer import obb_separation
        # 4×4 at (0,0), 4×4 at (10,10).  Both X and Y projection gaps
        # are 10 - 4 = 6.  Center-to-center is (1,1)/√2.
        _, axis = obb_separation(0, 0, 4, 4, 0, 10, 10, 4, 4, 0)
        # Either +x or +y would have equal gap; alignment dot product
        # is the same (0.707) for both, but the implementation picks the
        # first one found at the better alignment.  Just verify the
        # axis points TOWARD B (positive components).
        assert axis[0] >= 0 and axis[1] >= 0

    def test_no_flip_under_small_perturbation(self):
        """Slight rotation of one OBB shouldn't flip the separation
        axis from +x to +y (or vice versa).  The stable tie-break
        prefers the axis aligned with center-to-center direction."""
        from commands.autoplacer import obb_separation
        # B east of A, very slight diagonal offset (small dy).
        _, axis1 = obb_separation(0, 0, 4, 4, 0, 10, 0.01, 4, 4, 0)
        _, axis2 = obb_separation(0, 0, 4, 4, 0, 10, -0.01, 4, 4, 0)
        # Both should point predominantly +x.
        assert axis1[0] > 0.5 and abs(axis1[1]) < 0.5
        assert axis2[0] > 0.5 and abs(axis2[1]) < 0.5


@pytest.mark.unit
class TestLeverArmTorque:
    def test_off_center_spring_rotates_pin_toward_target(self):
        """Pin south of A center, target due east.  Body should rotate
        CCW visually (c.rotation increases) so the south pin sweeps
        toward east → toward its target.  Earlier code had a sign
        error here that rotated CW instead (away from target)."""
        from commands.autoplacer import (
            Component, Net, Pin, Session, iterate,
        )
        sess = Session(schematic_path=Path("pcb://test"))
        # A at origin, pin 1 at south (PCB coords: +Y = south in screen).
        a = Component(
            ref="A", unit=1, lib_id="x:y",
            x=0.0, y=0.0, rotation=0.0,
            mirror_x=False, mirror_y=False,
            bbox_w=2.0, bbox_h=2.0,
            coord_system="pcb", layer="F.Cu",
        )
        a.pins["1"] = Pin(number="1", name="", local_x=0.0, local_y=2.0,
                           lib_angle=0.0)
        sess.components[a.key] = a

        # B at east, pin at B's center.
        b = Component(
            ref="B", unit=1, lib_id="x:y",
            x=10.0, y=0.0, rotation=0.0,
            mirror_x=False, mirror_y=False,
            bbox_w=2.0, bbox_h=2.0, pinned=True,
            coord_system="pcb", layer="F.Cu",
        )
        b.pins["1"] = Pin(number="1", name="", local_x=0.0, local_y=0.0,
                           lib_angle=0.0)
        sess.components[b.key] = b

        sess.nets["SIG"] = Net(name="SIG", pins=[(a.key, "1"), (b.key, "1")])

        p = sess.params
        p.use_spring_classes = True
        p.pinwise_torque_k = 1.0      # large for easy detection
        p.attraction_k = 0.1
        p.repulsion_k = 0.0           # silence all other forces
        p.boundary_k = 0.0
        p.polarity_k = 0.0
        p.polarity_torque_k = 0.0
        p.rotation_k = 0.0
        sess.temperature = 0.01       # damp linear motion

        before_rot = a.rotation
        iterate(sess, n=1)
        # Pin at world (0, 2), target (10, 0).  Force on pin = +X, -Y.
        # To rotate pin from south toward east, body must rotate CCW
        # visually (positive c.rotation).  Cross product in CCW-visual
        # convention: T = r_y*F_x - r_x*F_y = 2*10 - 0*(-2) = +20.
        # POSITIVE torque → c.rotation increases.
        assert a.rotation > before_rot, (
            f"expected CCW visual rotation (pin south should rotate "
            f"toward east target), got rotation {before_rot} -> {a.rotation}"
        )

    def test_user_R23_scenario(self):
        """The user's reported case: a 2-pin resistor with target1 NE,
        target2 SW.  Starting body NW/SE (rotation 135°), it should
        rotate so pin 1 ends up at NE (rotation 225°) — the correct
        NE/SW alignment with pin 1 toward its NE target."""
        from commands.autoplacer import (
            Component, Net, Pin, Session, iterate,
        )
        sess = Session(schematic_path=Path("pcb://test"))
        # R23-like resistor: two pins on its X axis.
        r = Component(
            ref="R23", unit=1, lib_id="Resistor_SMD:R_0402",
            x=0.0, y=0.0, rotation=135.0,    # NW/SE alignment to start
            mirror_x=False, mirror_y=False,
            bbox_w=1.6, bbox_h=0.8,
            coord_system="pcb", layer="F.Cu",
        )
        r.pins["1"] = Pin(number="1", name="", local_x=-0.8, local_y=0.0,
                           lib_angle=0.0)
        r.pins["2"] = Pin(number="2", name="", local_x=+0.8, local_y=0.0,
                           lib_angle=0.0)
        sess.components[r.key] = r

        # Target 1 far NE — pin 1 should orient toward here.
        t1 = Component(
            ref="T1", unit=1, lib_id="x:y",
            x=10.0, y=-10.0, rotation=0.0,
            mirror_x=False, mirror_y=False,
            bbox_w=1.0, bbox_h=1.0, pinned=True,
            coord_system="pcb", layer="F.Cu",
        )
        t1.pins["1"] = Pin(number="1", name="", local_x=0.0, local_y=0.0,
                            lib_angle=0.0)
        sess.components[t1.key] = t1

        # Target 2 far SW — pin 2 should orient toward here.
        t2 = Component(
            ref="T2", unit=1, lib_id="x:y",
            x=-10.0, y=10.0, rotation=0.0,
            mirror_x=False, mirror_y=False,
            bbox_w=1.0, bbox_h=1.0, pinned=True,
            coord_system="pcb", layer="F.Cu",
        )
        t2.pins["1"] = Pin(number="1", name="", local_x=0.0, local_y=0.0,
                            lib_angle=0.0)
        sess.components[t2.key] = t2

        sess.nets["A"] = Net(name="A", pins=[(r.key, "1"), (t1.key, "1")])
        sess.nets["B"] = Net(name="B", pins=[(r.key, "2"), (t2.key, "1")])

        p = sess.params
        p.use_spring_classes = True
        p.pinwise_torque_k = 0.5
        p.attraction_k = 0.1
        p.repulsion_k = 0.0
        p.boundary_k = 0.0
        p.polarity_k = 0.0
        p.polarity_torque_k = 0.0
        p.rotation_k = 0.0
        sess.temperature = 0.01      # very small — almost no translation

        # Run enough iters for the body to converge orientation.
        for _ in range(200):
            iterate(sess, n=1)

        # R23 should now be near rotation 225° (NE/SW with pin 1 at NE).
        # 45° is the WRONG stable equilibrium (would mean the sign bug
        # is still present).
        rot = r.rotation
        # Normalize the angle distance to 225° vs 45°
        dist_225 = min(abs(rot - 225), abs(rot - 225 + 360),
                        abs(rot - 225 - 360))
        dist_45 = min(abs(rot - 45), abs(rot - 45 + 360),
                       abs(rot - 45 - 360))
        assert dist_225 < dist_45, (
            f"R23 should converge to rotation 225° (NE/SW with pin 1 NE), "
            f"got rotation={rot:.1f}° (closer to 45° = the sign-bug equilibrium)"
        )

    def test_no_torque_when_flag_off(self):
        """Without use_spring_classes, lever-arm torque is skipped
        (schematic flow keeps its existing angle-based torque)."""
        from commands.autoplacer import (
            Component, Net, Pin, Session, iterate,
        )
        sess = Session(schematic_path=Path("/tmp/none.kicad_sch"))
        a = Component(
            ref="A", unit=1, lib_id="x:y",
            x=0.0, y=0.0, rotation=0.0,
            mirror_x=False, mirror_y=False,
            bbox_w=2.0, bbox_h=2.0,
        )
        a.pins["1"] = Pin(number="1", name="", local_x=0.0, local_y=2.0,
                           lib_angle=0.0)
        sess.components[a.key] = a
        b = Component(
            ref="B", unit=1, lib_id="x:y",
            x=10.0, y=0.0, rotation=0.0,
            mirror_x=False, mirror_y=False,
            bbox_w=2.0, bbox_h=2.0, pinned=True,
        )
        b.pins["1"] = Pin(number="1", name="", local_x=0.0, local_y=0.0,
                           lib_angle=0.0)
        sess.components[b.key] = b
        sess.nets["SIG"] = Net(name="SIG", pins=[(a.key, "1"), (b.key, "1")])

        p = sess.params
        p.use_spring_classes = False    # ← flag OFF
        p.pinwise_torque_k = 1.0
        p.attraction_k = 0.1
        p.repulsion_k = 0.0
        p.boundary_k = 0.0
        p.polarity_k = 0.0
        p.polarity_torque_k = 0.0
        p.rotation_k = 0.0
        sess.temperature = 0.01

        before_rot = a.rotation
        iterate(sess, n=1)
        # No lever-arm torque should apply.  The schematic _torque_for_pin_orientation
        # also returns ~0 because rotation_k=0.
        assert abs(a.rotation - before_rot) < 0.01


@pytest.mark.unit
class TestCrossLayerSpringFlag:
    def test_cross_layer_skipped_when_flag_off(self):
        """Components on different layers shouldn't attract when
        cross_layer_springs=False."""
        from commands.autoplacer import (
            Component, Net, Pin, Session, iterate,
        )
        sess = Session(schematic_path=Path("pcb://test"))
        # A on F.Cu, B on B.Cu, both connected to net "X".
        for ref, layer in (("A", "F.Cu"), ("B", "B.Cu")):
            c = Component(
                ref=ref, unit=1, lib_id="x:y",
                x={"A": 0.0, "B": 20.0}[ref], y=0.0, rotation=0.0,
                mirror_x=False, mirror_y=False,
                bbox_w=2.0, bbox_h=2.0,
                coord_system="pcb", layer=layer,
            )
            c.pins["1"] = Pin(number="1", name="", local_x=0.0, local_y=0.0,
                              lib_angle=0.0)
            sess.components[c.key] = c
        sess.nets["X"] = Net(name="X", pins=[("A__u1", "1"), ("B__u1", "1")])

        p = sess.params
        p.use_spring_classes = True
        p.cross_layer_springs = False    # ← skip cross-layer
        p.attraction_k = 1.0             # strong to amplify any movement
        p.repulsion_k = 0.0
        p.boundary_k = 0.0
        p.polarity_k = 0.0
        sess.temperature = 1.0

        ax_before = sess.components["A__u1"].x
        bx_before = sess.components["B__u1"].x
        iterate(sess, n=5)
        # Neither component should move (no force applied).
        assert sess.components["A__u1"].x == pytest.approx(ax_before, abs=0.01)
        assert sess.components["B__u1"].x == pytest.approx(bx_before, abs=0.01)

    def test_cross_layer_applied_when_flag_on(self):
        from commands.autoplacer import (
            Component, Net, Pin, Session, iterate,
        )
        sess = Session(schematic_path=Path("pcb://test"))
        for ref, layer in (("A", "F.Cu"), ("B", "B.Cu")):
            c = Component(
                ref=ref, unit=1, lib_id="x:y",
                x={"A": 0.0, "B": 20.0}[ref], y=0.0, rotation=0.0,
                mirror_x=False, mirror_y=False,
                bbox_w=2.0, bbox_h=2.0,
                coord_system="pcb", layer=layer,
            )
            c.pins["1"] = Pin(number="1", name="", local_x=0.0, local_y=0.0,
                              lib_angle=0.0)
            sess.components[c.key] = c
        sess.nets["X"] = Net(name="X", pins=[("A__u1", "1"), ("B__u1", "1")])

        p = sess.params
        p.use_spring_classes = True
        p.cross_layer_springs = True
        p.attraction_k = 1.0
        p.repulsion_k = 0.0
        p.boundary_k = 0.0
        p.polarity_k = 0.0
        sess.temperature = 1.0

        iterate(sess, n=5)
        # A should move toward B (rightward).
        assert sess.components["A__u1"].x > 0.05


@pytest.mark.integration
@pytest.mark.skipif(
    not _real_pcbnew_available(),
    reason="needs real pcbnew swig module",
)
class TestLocalBbox:
    def test_rotated_footprint_local_bbox_matches_unrotated(self):
        """A 90°-rotated footprint should report the SAME local bbox
        dimensions as its unrotated counterpart."""
        import pcbnew
        from commands.pcb_autoplacer import _footprint_local_bbox_mm

        BOARD = Path("/home/vagrant/projects/kicad_agent/projects/power_module/power_module.kicad_pcb")
        if not BOARD.exists():
            pytest.skip("power_module fixture not present")
        board = pcbnew.LoadBoard(str(BOARD))
        u4 = board.FindFootprintByReference("U4")
        if u4 is None:
            pytest.skip("U4 not on board")
        pos = u4.GetPosition()
        ang = u4.GetOrientation().AsDegrees()
        local_w, local_h = _footprint_local_bbox_mm(
            u4, pos.x / 1e6, pos.y / 1e6, ang,
        )
        # Now rotate it programmatically to 0° and re-measure.
        # SWIG board mutation is in-memory only — fine for the test.
        u4.SetOrientation(pcbnew.EDA_ANGLE(0, pcbnew.DEGREES_T))
        local_w0, local_h0 = _footprint_local_bbox_mm(
            u4, pos.x / 1e6, pos.y / 1e6, 0.0,
        )
        # Restore so other tests aren't affected.
        u4.SetOrientation(pcbnew.EDA_ANGLE(ang, pcbnew.DEGREES_T))

        # Either orientation should yield the same dimensions (within
        # epsilon) because the function returns LOCAL bbox.
        assert local_w == pytest.approx(local_w0, abs=0.05), (
            f"local bbox W changed with rotation: {local_w} vs {local_w0}"
        )
        assert local_h == pytest.approx(local_h0, abs=0.05), (
            f"local bbox H changed with rotation: {local_h} vs {local_h0}"
        )
