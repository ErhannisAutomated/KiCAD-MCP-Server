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


def _build_overshoot_session(*, force_step_damping: float):
    """Build a Session where component A has two springs each pulling
    it toward x=0 with effective K=2, so without damping it
    oscillates period-2 around x=0.  Targets placed far enough away
    that the co-located-components 1/r² fallback nudge doesn't fire."""
    from commands.autoplacer import (
        Component, Net, Pin, Session,
    )
    sess = Session(schematic_path=Path("pcb://test"))
    comp = Component(
        ref="A", unit=1, lib_id="x:y",
        x=1.0, y=0.0, rotation=0.0,
        mirror_x=False, mirror_y=False,
        bbox_w=0.1, bbox_h=0.1,
        coord_system="pcb", layer="F.Cu",
    )
    comp.pins["1"] = Pin(number="1", name="", local_x=0.0, local_y=0.0, lib_angle=0.0)
    comp.pins["2"] = Pin(number="2", name="", local_x=0.0, local_y=0.0, lib_angle=0.0)
    sess.components[comp.key] = comp
    # Targets ABOVE / BELOW A's path so the system is 1-D in x but
    # targets aren't co-located with A's expected resting position.
    for ref, ty in (("T1", 50.0), ("T2", -50.0)):
        t = Component(
            ref=ref, unit=1, lib_id="x:y",
            x=0.0, y=ty, rotation=0.0,
            mirror_x=False, mirror_y=False,
            bbox_w=0.1, bbox_h=0.1, pinned=True,
            coord_system="pcb", layer="F.Cu",
        )
        t.pins["1"] = Pin(number="1", name="", local_x=0.0, local_y=0.0, lib_angle=0.0)
        sess.components[t.key] = t
    sess.nets["N1"] = Net(name="N1", pins=[(comp.key, "1"), ("T1__u1", "1")])
    sess.nets["N2"] = Net(name="N2", pins=[(comp.key, "2"), ("T2__u1", "1")])

    p = sess.params
    p.use_spring_classes = True
    p.attraction_k = 1.0
    p.repulsion_k = 0.0
    p.boundary_k = 0.0
    p.polarity_k = 0.0
    p.rotation_k = 0.0
    p.pinwise_torque_k = 0.0
    p.force_step_damping = force_step_damping
    sess.temperature = 1000.0    # huge so step cap never engages
    return sess, comp


@pytest.mark.unit
class TestForceStepDamping:
    def test_undamped_oscillates_near_equilibrium(self):
        """Without damping, a component near equilibrium with effective
        restoring stiffness >= 2 in the x direction oscillates with
        period 2."""
        from commands.autoplacer import iterate
        sess, comp = _build_overshoot_session(force_step_damping=1.0)
        # x=1 with K_eff=2 → after 1 iter, x = 1 - 2 = -1 (overshoot
        # by 1).  Next iter: x = -1 + 2 = +1.  Period 2 oscillation.
        x_history = []
        for _ in range(6):
            iterate(sess, n=1)
            x_history.append(comp.x)
        sign_flips = sum(
            1 for i in range(len(x_history) - 1)
            if x_history[i] * x_history[i+1] < 0
        )
        assert sign_flips >= 3, (
            f"expected period-2 x-axis oscillation; got x_history={x_history}"
        )

    def test_damping_eliminates_oscillation(self):
        """With damping=0.5, the same setup converges monotonically."""
        from commands.autoplacer import iterate
        sess, comp = _build_overshoot_session(force_step_damping=0.5)
        x_history = []
        for _ in range(6):
            iterate(sess, n=1)
            x_history.append(comp.x)
        sign_flips = sum(
            1 for i in range(len(x_history) - 1)
            if x_history[i] * x_history[i+1] < 0
        )
        assert sign_flips == 0, (
            f"with damping=0.5, x should converge monotonically; "
            f"got x_history={x_history}"
        )
        assert abs(x_history[-1]) < 0.5, (
            f"expected x to approach 0; got final x={x_history[-1]}"
        )


@pytest.mark.unit
class TestNormalizeSpringForceByDegree:
    def test_many_pin_component_no_longer_oscillates(self):
        """A 10-pin component with one spring per pin would, unnormalized,
        feel K_eff = 10 — well past the K_eff = 2 that damping=0.5 was
        designed for.  With normalize_spring_force_by_degree=True,
        the per-component motion is independent of pin count."""
        from commands.autoplacer import (
            Component, Net, Pin, Session, iterate,
        )
        sess = Session(schematic_path=Path("pcb://test"))
        # IC at x=1.0 with 10 pads, all on different nets to maximize
        # spring count.  Each pad connects to a pinned target far off
        # in y so the closest-pair nudge can't fire.
        comp = Component(
            ref="U1", unit=1, lib_id="x:y",
            x=1.0, y=0.0, rotation=0.0,
            mirror_x=False, mirror_y=False,
            bbox_w=0.5, bbox_h=2.0,
            coord_system="pcb", layer="F.Cu",
        )
        for n in range(1, 11):
            comp.pins[str(n)] = Pin(
                number=str(n), name="",
                local_x=0.0, local_y=n * 0.1, lib_angle=0.0,
            )
        sess.components[comp.key] = comp
        for n in range(1, 11):
            ref = f"T{n}"
            t = Component(
                ref=ref, unit=1, lib_id="x:y",
                x=0.0, y=50.0 + n, rotation=0.0,
                mirror_x=False, mirror_y=False,
                bbox_w=0.1, bbox_h=0.1, pinned=True,
                coord_system="pcb", layer="F.Cu",
            )
            t.pins["1"] = Pin(number="1", name="", local_x=0.0, local_y=0.0, lib_angle=0.0)
            sess.components[t.key] = t
            sess.nets[f"N{n}"] = Net(
                name=f"N{n}",
                pins=[(comp.key, str(n)), (f"T{n}__u1", "1")],
            )

        p = sess.params
        p.use_spring_classes = True
        p.attraction_k = 1.0
        p.repulsion_k = 0.0
        p.boundary_k = 0.0
        p.polarity_k = 0.0
        p.rotation_k = 0.0
        p.pinwise_torque_k = 0.0
        p.force_step_damping = 0.5
        p.normalize_spring_force_by_degree = True   # ← the fix
        sess.temperature = 1000.0

        x_history = []
        for _ in range(8):
            iterate(sess, n=1)
            x_history.append(comp.x)
        sign_flips = sum(
            1 for i in range(len(x_history) - 1)
            if x_history[i] * x_history[i+1] < 0
        )
        assert sign_flips == 0, (
            f"normalized: expected monotonic convergence; "
            f"got x={x_history}, sign_flips={sign_flips}"
        )

    def test_many_pin_component_oscillates_unnormalized(self):
        """Same setup, normalize off → period-2 oscillation."""
        from commands.autoplacer import (
            Component, Net, Pin, Session, iterate,
        )
        sess = Session(schematic_path=Path("pcb://test"))
        comp = Component(
            ref="U1", unit=1, lib_id="x:y",
            x=1.0, y=0.0, rotation=0.0,
            mirror_x=False, mirror_y=False,
            bbox_w=0.5, bbox_h=2.0,
            coord_system="pcb", layer="F.Cu",
        )
        for n in range(1, 11):
            comp.pins[str(n)] = Pin(
                number=str(n), name="",
                local_x=0.0, local_y=n * 0.1, lib_angle=0.0,
            )
        sess.components[comp.key] = comp
        for n in range(1, 11):
            ref = f"T{n}"
            t = Component(
                ref=ref, unit=1, lib_id="x:y",
                x=0.0, y=50.0 + n, rotation=0.0,
                mirror_x=False, mirror_y=False,
                bbox_w=0.1, bbox_h=0.1, pinned=True,
                coord_system="pcb", layer="F.Cu",
            )
            t.pins["1"] = Pin(number="1", name="", local_x=0.0, local_y=0.0, lib_angle=0.0)
            sess.components[t.key] = t
            sess.nets[f"N{n}"] = Net(
                name=f"N{n}",
                pins=[(comp.key, str(n)), (f"T{n}__u1", "1")],
            )

        p = sess.params
        p.use_spring_classes = True
        p.attraction_k = 1.0
        p.repulsion_k = 0.0
        p.boundary_k = 0.0
        p.polarity_k = 0.0
        p.rotation_k = 0.0
        p.pinwise_torque_k = 0.0
        p.force_step_damping = 0.5
        p.normalize_spring_force_by_degree = False   # ← bug surface
        sess.temperature = 1000.0

        x_history = []
        for _ in range(8):
            iterate(sess, n=1)
            x_history.append(comp.x)
        # With 10 springs each k=1, K_eff = 10.  damping=0.5 gives
        # effective step = 5x — diverges, not just oscillates.
        # We just check there's no monotonic convergence.
        sign_flips = sum(
            1 for i in range(len(x_history) - 1)
            if x_history[i] * x_history[i+1] < 0
        )
        assert sign_flips >= 3, (
            f"unnormalized: expected oscillation/divergence; "
            f"got x={x_history}, sign_flips={sign_flips}"
        )


@pytest.mark.unit
class TestSchematicTorqueSkipsPCB:
    def test_torque_for_pin_orientation_returns_zero_for_pcb(self):
        """`_torque_for_pin_orientation` and `_torque_polarity_orientation`
        rely on per-pin outward angles that don't exist for PCB pads
        (load_pcb_session sets lib_angle=0 on every pad).  Without
        this guard, every pad on a PCB footprint reports the same
        outward direction and the schematic torque tries to align ONE
        direction with the SUM of all pad targets — produces noise
        on PCB and fights the lever-arm torque.  Symptom in the field:
        manual tuning script with rotation_k > 0 made PCB components
        rotate to wrong orientations even with the lever-arm torque
        sign correct."""
        from commands.autoplacer import (
            Component, Net, Pin, Session,
            _torque_for_pin_orientation, _torque_polarity_orientation,
        )
        sess = Session(schematic_path=Path("pcb://test"))
        c = Component(
            ref="R1", unit=1, lib_id="x:y",
            x=0.0, y=0.0, rotation=45.0,
            mirror_x=False, mirror_y=False,
            bbox_w=2.0, bbox_h=2.0,
            coord_system="pcb", layer="F.Cu",
        )
        c.pins["1"] = Pin(number="1", name="", local_x=-1.0, local_y=0.0,
                          lib_angle=0.0)
        c.pins["2"] = Pin(number="2", name="", local_x=+1.0, local_y=0.0,
                          lib_angle=0.0)
        sess.components[c.key] = c
        # Add some nets with off-center targets to exercise both
        # torque functions.
        other = Component(
            ref="X", unit=1, lib_id="x:y",
            x=10.0, y=-10.0, rotation=0.0,
            mirror_x=False, mirror_y=False,
            bbox_w=1.0, bbox_h=1.0, pinned=True,
            coord_system="pcb", layer="F.Cu",
        )
        other.pins["1"] = Pin(number="1", name="", local_x=0.0, local_y=0.0,
                              lib_angle=0.0)
        sess.components[other.key] = other
        sess.nets["GND"] = Net(name="GND", pins=[(c.key, "1"), (other.key, "1")])
        sess.nets["SIG"] = Net(name="SIG", pins=[(c.key, "2"), (other.key, "1")])

        sess.params.rotation_k = 4.0           # what the user had set
        sess.params.polarity_torque_k = 3.0

        # Both schematic torques should return zero for PCB components.
        assert _torque_for_pin_orientation(c, sess) == 0.0
        assert _torque_polarity_orientation(c, sess) == 0.0

    def test_schematic_torque_still_active_for_schematic(self):
        """Sanity: the PCB skip doesn't accidentally kill the schematic
        torque for schematic-flow components."""
        from commands.autoplacer import (
            Component, Net, Pin, Session,
            _torque_for_pin_orientation,
        )
        sess = Session(schematic_path=Path("/tmp/none.kicad_sch"))
        c = Component(
            ref="R1", unit=1, lib_id="Device:R",
            x=0.0, y=0.0, rotation=0.0,
            mirror_x=False, mirror_y=False,
            bbox_w=7.62, bbox_h=7.62,
            # coord_system defaults to "schematic"
        )
        c.pins["1"] = Pin(number="1", name="", local_x=0.0, local_y=3.81,
                          lib_angle=270.0)  # outward upward in lib
        sess.components[c.key] = c
        other = Component(
            ref="R2", unit=1, lib_id="Device:R",
            x=20.0, y=0.0, rotation=0.0,
            mirror_x=False, mirror_y=False,
        )
        other.pins["1"] = Pin(number="1", name="", local_x=0.0, local_y=0.0,
                              lib_angle=0.0)
        sess.components[other.key] = other
        sess.nets["SIG"] = Net(name="SIG", pins=[(c.key, "1"), (other.key, "1")])
        sess.params.rotation_k = 4.0

        # Schematic component → torque computed normally.
        t = _torque_for_pin_orientation(c, sess)
        assert t != 0.0, "schematic torque should still fire for schematic components"


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


@pytest.mark.unit
class TestSequentialApply:
    """Sequential (Gauss-Seidel) update mode: each component sees the
    just-updated positions of those processed earlier in the same iter.
    """

    def _make_pair(self):
        from commands.autoplacer import Component, Net, Pin, Session
        sess = Session(schematic_path=Path("pcb://test"))
        for ref, x in (("A", 0.0), ("B", 10.0)):
            c = Component(
                ref=ref, unit=1, lib_id="x:y",
                x=x, y=0.0, rotation=0.0,
                mirror_x=False, mirror_y=False,
                bbox_w=2.0, bbox_h=2.0,
                coord_system="pcb", layer="F.Cu",
            )
            c.pins["1"] = Pin(number="1", name="", local_x=0.0, local_y=0.0,
                              lib_angle=0.0)
            sess.components[c.key] = c
        sess.nets["X"] = Net(name="X", pins=[("A__u1", "1"), ("B__u1", "1")])
        p = sess.params
        p.use_spring_classes = True
        p.attraction_k = 0.5
        p.repulsion_k = 0.0
        p.boundary_k = 0.0
        p.polarity_k = 0.0
        p.rotation_k = 0.0
        p.pinwise_torque_k = 0.0
        sess.temperature = 5.0
        p.cooling = 1.0
        p.force_step_damping = 1.0
        return sess

    def test_sequential_flag_runs_without_error(self):
        from commands.autoplacer import iterate
        sess = self._make_pair()
        sess.params.sequential_apply = True
        iterate(sess, n=5)
        # Both should have moved toward each other.
        ax = sess.components["A__u1"].x
        bx = sess.components["B__u1"].x
        assert ax > 0.05 and bx < 9.95

    def test_parallel_and_sequential_both_converge(self):
        """Two springs pulling toward each other should converge in
        both modes (different trajectory, same general direction)."""
        from commands.autoplacer import iterate
        # Parallel
        sess_p = self._make_pair()
        sess_p.params.sequential_apply = False
        iterate(sess_p, n=30)
        gap_p = sess_p.components["B__u1"].x - sess_p.components["A__u1"].x
        # Sequential
        sess_s = self._make_pair()
        sess_s.params.sequential_apply = True
        iterate(sess_s, n=30)
        gap_s = sess_s.components["B__u1"].x - sess_s.components["A__u1"].x
        # Both should be near zero gap (pins co-located).
        assert abs(gap_p) < 1.0
        assert abs(gap_s) < 1.0


@pytest.mark.unit
class TestSnapRotations:
    """`snap_rotations` rounds non-anchored rotations to multiples
    of `period`, skipping pinned components and no-op cases."""

    def _make_comp(self, ref, rotation, pinned=False):
        from commands.autoplacer import Component
        return Component(
            ref=ref, unit=1, lib_id="x:y",
            x=0.0, y=0.0, rotation=rotation,
            mirror_x=False, mirror_y=False,
            bbox_w=2.0, bbox_h=2.0,
            pinned=pinned,
            coord_system="pcb", layer="F.Cu",
        )

    def test_rounds_to_nearest_period_multiple(self):
        from commands.autoplacer import Session
        from commands.pcb_autoplacer import snap_rotations
        sess = Session(schematic_path=Path("pcb://test"))
        a = self._make_comp("A", rotation=89.5)
        b = self._make_comp("B", rotation=137.0)
        c = self._make_comp("C", rotation=315.5)
        sess.components[a.key] = a
        sess.components[b.key] = b
        sess.components[c.key] = c
        n = snap_rotations(sess, period=90.0)
        assert n == 3
        assert a.rotation == 90.0
        assert b.rotation == 180.0
        assert c.rotation == 0.0    # 315.5 → 360 → 0 modulo 360

    def test_skips_pinned(self):
        from commands.autoplacer import Session
        from commands.pcb_autoplacer import snap_rotations
        sess = Session(schematic_path=Path("pcb://test"))
        a = self._make_comp("A", rotation=89.5, pinned=True)
        b = self._make_comp("B", rotation=89.5, pinned=False)
        sess.components[a.key] = a
        sess.components[b.key] = b
        n = snap_rotations(sess, period=90.0)
        assert n == 1
        assert a.rotation == 89.5  # unchanged
        assert b.rotation == 90.0

    def test_no_op_when_already_aligned(self):
        from commands.autoplacer import Session
        from commands.pcb_autoplacer import snap_rotations
        sess = Session(schematic_path=Path("pcb://test"))
        for rot in (0.0, 90.0, 180.0, 270.0):
            c = self._make_comp(f"C{int(rot)}", rotation=rot)
            sess.components[c.key] = c
        n = snap_rotations(sess, period=90.0)
        assert n == 0


@pytest.mark.unit
class TestPCBScheduleDefaults:
    """Sanity check that the tuned defaults are still wired in."""

    def test_defaults_reflect_tuning(self):
        from commands.pcb_autoplacer import PCBSchedule
        s = PCBSchedule()
        # Repulsion regime appropriate for the 1/r³ formula
        assert s.spring_k == 1.0
        assert s.repulsion_k_start == pytest.approx(1e-4)
        assert s.repulsion_k_peak == pytest.approx(0.1)
        assert s.rotation_snap_peak == 30.0
        assert s.pinwise_torque_k == 1.0
        assert s.force_step_damping == 0.3
        assert s.enforce_rotation_snap is True
        assert s.boundary_k == 1.0


@pytest.mark.unit
class TestParsePinSpringClass:
    """The _parse_pin_spring_class helper accepts two formats:
    bare-string (pad-general) and JSON dict (with per-target overrides).
    Malformed input drops to None (logged) so a typo can't crash a run.
    """

    def test_bare_string(self):
        from commands.pcb_autoplacer import _parse_pin_spring_class
        assert _parse_pin_spring_class("DECOUPLING") == "DECOUPLING"

    def test_whitespace_stripped(self):
        from commands.pcb_autoplacer import _parse_pin_spring_class
        assert _parse_pin_spring_class("  SIGNAL  ") == "SIGNAL"

    def test_empty_returns_none(self):
        from commands.pcb_autoplacer import _parse_pin_spring_class
        assert _parse_pin_spring_class("") is None
        assert _parse_pin_spring_class("   ") is None

    def test_json_dict(self):
        from commands.pcb_autoplacer import _parse_pin_spring_class
        result = _parse_pin_spring_class('{"*":"SIGNAL","U1.4":"DECOUPLING"}')
        assert result == {"*": "SIGNAL", "U1.4": "DECOUPLING"}

    def test_malformed_json_returns_none(self):
        from commands.pcb_autoplacer import _parse_pin_spring_class
        assert _parse_pin_spring_class('{bad json}') is None

    def test_json_with_non_string_values_returns_none(self):
        from commands.pcb_autoplacer import _parse_pin_spring_class
        assert _parse_pin_spring_class('{"*": 5}') is None

    def test_json_list_not_dict_returns_none(self):
        from commands.pcb_autoplacer import _parse_pin_spring_class
        assert _parse_pin_spring_class('["DECOUPLING"]') is None


# Spring-class project I/O tests moved to
# tests/test_schematic_metadata_consumers.py (#230 phase 3 dropped
# the .kicad_pro path that this class exercised).


@pytest.mark.integration
@pytest.mark.skipif(
    not _real_pcbnew_available(),
    reason="needs real pcbnew swig module",
)
class TestFootprintPropertyRead:
    """Pin_Spring_Class:N, Spring_Class, and Body_Margin footprint
    properties are read into Component fields at load time.  Writes
    transient SWIG-side state on one footprint, loads the session,
    asserts the value made it through, and unsets the property.
    """

    def test_pin_spring_class_bare_string(self):
        import pcbnew
        from commands.pcb_autoplacer import load_pcb_session

        BOARD = Path("/home/vagrant/projects/kicad_agent/projects/power_module/power_module.kicad_pcb")
        if not BOARD.exists():
            pytest.skip("power_module fixture not present")
        board = pcbnew.LoadBoard(str(BOARD))
        # Pick a 2-pin cap to annotate.
        target = next(
            (fp for fp in board.GetFootprints()
             if fp.GetReference().startswith("C") and len(fp.Pads()) == 2),
            None,
        )
        if target is None:
            pytest.skip("no 2-pin cap on board")
        ref = target.GetReference()
        try:
            target.SetProperty("Pin_Spring_Class:1", "DECOUPLING")
            sess = load_pcb_session(board)
            comp = sess.components[f"{ref}__u1"]
            assert comp.pin_classes.get("1") == "DECOUPLING"
        finally:
            # Best-effort cleanup; pcbnew property removal differs by version.
            try:
                target.SetProperty("Pin_Spring_Class:1", "")
            except Exception:
                pass

    def test_spring_class_component_level(self):
        import pcbnew
        from commands.pcb_autoplacer import load_pcb_session

        BOARD = Path("/home/vagrant/projects/kicad_agent/projects/power_module/power_module.kicad_pcb")
        if not BOARD.exists():
            pytest.skip("power_module fixture not present")
        board = pcbnew.LoadBoard(str(BOARD))
        target = next(iter(board.GetFootprints()), None)
        if target is None:
            pytest.skip("no footprints on board")
        ref = target.GetReference()
        try:
            target.SetProperty("Spring_Class", "INTER_GROUP")
            sess = load_pcb_session(board)
            comp = sess.components[f"{ref}__u1"]
            assert comp.spring_class == "INTER_GROUP"
        finally:
            try:
                target.SetProperty("Spring_Class", "")
            except Exception:
                pass

    def test_body_margin_property(self):
        import pcbnew
        from commands.pcb_autoplacer import load_pcb_session

        BOARD = Path("/home/vagrant/projects/kicad_agent/projects/power_module/power_module.kicad_pcb")
        if not BOARD.exists():
            pytest.skip("power_module fixture not present")
        board = pcbnew.LoadBoard(str(BOARD))
        target = next(iter(board.GetFootprints()), None)
        if target is None:
            pytest.skip("no footprints on board")
        ref = target.GetReference()
        try:
            target.SetProperty("Body_Margin", "2.5")
            sess = load_pcb_session(board)
            comp = sess.components[f"{ref}__u1"]
            assert comp.margin == pytest.approx(2.5)
        finally:
            try:
                target.SetProperty("Body_Margin", "")
            except Exception:
                pass


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
        local_w, local_h, local_cx, local_cy = _footprint_local_bbox_mm(
            u4, pos.x / 1e6, pos.y / 1e6, ang,
        )
        # Now rotate it programmatically to 0° and re-measure.
        # SWIG board mutation is in-memory only — fine for the test.
        u4.SetOrientation(pcbnew.EDA_ANGLE(0, pcbnew.DEGREES_T))
        local_w0, local_h0, local_cx0, local_cy0 = _footprint_local_bbox_mm(
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
        assert local_cx == pytest.approx(local_cx0, abs=0.05)
        assert local_cy == pytest.approx(local_cy0, abs=0.05)

    def test_pin_header_bbox_has_offset_center(self):
        """Pin headers (J*) are typically anchored at pin 1, so the
        body center should sit offset from the footprint origin.
        Asserts the offset is meaningfully nonzero for at least one
        J-prefixed footprint with more than one pin."""
        import pcbnew
        from commands.pcb_autoplacer import _footprint_local_bbox_mm

        BOARD = Path("/home/vagrant/projects/kicad_agent/projects/power_module/power_module.kicad_pcb")
        if not BOARD.exists():
            pytest.skip("power_module fixture not present")
        board = pcbnew.LoadBoard(str(BOARD))
        targets = [
            fp for fp in board.GetFootprints()
            if fp.GetReference().startswith("J") and len(fp.Pads()) >= 2
        ]
        if not targets:
            pytest.skip("no multi-pin J-prefixed footprints on this board")
        any_offset = False
        for fp in targets:
            pos = fp.GetPosition()
            ang = fp.GetOrientation().AsDegrees()
            w, h, cx, cy = _footprint_local_bbox_mm(
                fp, pos.x / 1e6, pos.y / 1e6, ang,
            )
            if max(abs(cx), abs(cy)) > 0.5:   # >0.5mm offset is meaningful
                any_offset = True
                break
        assert any_offset, (
            "expected at least one pin-header footprint to have a body "
            "center offset >0.5mm from its origin; all reported ~0 offset"
        )
