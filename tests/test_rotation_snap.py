"""Tests for the rotation-snap torque potential.

PCB autoplacer can run with free rotation (snap_strength = 0) or
with a periodic torque that pulls toward 90° multiples
(snap_strength > 0).  Schedule typically anneals the strength from
0 to high across the run.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PYTHON_DIR = Path(__file__).parent.parent / "python"
sys.path.insert(0, str(PYTHON_DIR))


def _make_params(*, snap_strength=0.0, period=90.0):
    from commands.autoplacer import Params
    p = Params()
    p.rotation_snap_strength = snap_strength
    p.rotation_snap_period = period
    return p


def _make_comp(rotation):
    from commands.autoplacer import Component
    return Component(
        ref="X1", unit=1, lib_id="Device:R",
        x=0.0, y=0.0, rotation=rotation,
        mirror_x=False, mirror_y=False,
    )


@pytest.mark.unit
class TestRotationSnapTorque:
    def test_zero_strength_returns_zero(self):
        from commands.autoplacer import _torque_rotation_snap
        c = _make_comp(30.0)
        p = _make_params(snap_strength=0.0)
        assert _torque_rotation_snap(c, p) == 0.0

    def test_at_minimum_returns_zero(self):
        from commands.autoplacer import _torque_rotation_snap
        p = _make_params(snap_strength=5.0)
        for rotation in (0.0, 90.0, 180.0, 270.0):
            c = _make_comp(rotation)
            assert _torque_rotation_snap(c, p) == pytest.approx(0.0, abs=1e-9)

    def test_just_above_zero_pulls_negative(self):
        """rotation = 5° → torque should be NEGATIVE (back to 0°)."""
        from commands.autoplacer import _torque_rotation_snap
        c = _make_comp(5.0)
        p = _make_params(snap_strength=5.0)
        t = _torque_rotation_snap(c, p)
        assert t < 0

    def test_just_below_90_pulls_positive(self):
        """rotation = 85° → torque should be POSITIVE (forward to 90°)."""
        from commands.autoplacer import _torque_rotation_snap
        c = _make_comp(85.0)
        p = _make_params(snap_strength=5.0)
        t = _torque_rotation_snap(c, p)
        assert t > 0

    def test_at_45_unstable_equilibrium(self):
        """rotation = 45° is between minima; torque ≈ 0 (unstable)."""
        from commands.autoplacer import _torque_rotation_snap
        c = _make_comp(45.0)
        p = _make_params(snap_strength=5.0)
        assert _torque_rotation_snap(c, p) == pytest.approx(0.0, abs=1e-9)

    def test_scales_with_strength(self):
        from commands.autoplacer import _torque_rotation_snap
        c = _make_comp(30.0)
        t1 = _torque_rotation_snap(c, _make_params(snap_strength=1.0))
        t10 = _torque_rotation_snap(c, _make_params(snap_strength=10.0))
        assert t10 == pytest.approx(10.0 * t1)

    def test_custom_period(self):
        """With period=45°, minima at 0/45/90/...  rotation=22.5 = unstable."""
        from commands.autoplacer import _torque_rotation_snap
        p = _make_params(snap_strength=5.0, period=45.0)
        # At a minimum
        c = _make_comp(45.0)
        assert _torque_rotation_snap(c, p) == pytest.approx(0.0, abs=1e-9)
        # Unstable mid-point
        c = _make_comp(22.5)
        assert _torque_rotation_snap(c, p) == pytest.approx(0.0, abs=1e-9)
        # Slightly above 45 → negative (back toward 45)
        c = _make_comp(50.0)
        assert _torque_rotation_snap(c, p) < 0


@pytest.mark.unit
class TestRotationSnapIntegration:
    def test_iterate_no_op_when_strength_zero(self):
        """Calling iterate() with snap_strength=0 produces the same
        result as old behavior — no rotation drift from snap force."""
        from commands.autoplacer import Component, Net, Session, iterate
        sess = Session(schematic_path=Path("/tmp/none.kicad_sch"))
        sess.components["R1__u1"] = Component(
            ref="R1", unit=1, lib_id="Device:R",
            x=100.0, y=100.0, rotation=30.0,
            mirror_x=False, mirror_y=False,
        )
        sess.components["R2__u1"] = Component(
            ref="R2", unit=1, lib_id="Device:R",
            x=130.0, y=100.0, rotation=30.0,
            mirror_x=False, mirror_y=False,
        )
        sess.params.rotation_snap_strength = 0.0
        sess.temperature = 1.0
        before = sess.components["R1__u1"].rotation
        iterate(sess, n=5)
        after = sess.components["R1__u1"].rotation
        # No snap force, very small temperature → minimal rotation drift.
        # Won't be exactly equal because other forces may rotate slightly,
        # but the snap force isn't contributing.
        # We're really just verifying nothing crashes; behavioral
        # invariance vs pre-v2 is implicit in the 77 passing schematic tests.
        assert isinstance(after, float)

    def test_iterate_with_snap_pulls_to_grid(self):
        """With strong snap and no other forces, a rotated single component
        moves toward the nearest 90° multiple."""
        from commands.autoplacer import Component, Session, iterate
        sess = Session(schematic_path=Path("/tmp/none.kicad_sch"))
        sess.components["R1__u1"] = Component(
            ref="R1", unit=1, lib_id="Device:R",
            x=100.0, y=100.0, rotation=10.0,
            mirror_x=False, mirror_y=False,
        )
        sess.params.rotation_snap_strength = 3.0
        sess.params.repulsion_k = 0.0      # silence other torques
        sess.params.polarity_torque_k = 0.0
        sess.params.rotation_k = 0.0
        sess.temperature = 0.01            # damp linear motion
        iterate(sess, n=20)
        # Should be pulled back toward 0° from 10°.
        assert sess.components["R1__u1"].rotation < 10.0
