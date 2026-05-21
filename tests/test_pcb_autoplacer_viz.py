"""Smoke tests for the PCB autoplacer visualizer.

Runs headless (Agg backend) so a CI environment without a display
still exercises construction + update + save without errors.  Live
window behavior (qt5/tk backends, in-place redraw, no flicker)
needs to be verified by hand in Jupyter.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PYTHON_DIR = Path(__file__).parent.parent / "python"
sys.path.insert(0, str(PYTHON_DIR))

# Force headless backend before any matplotlib import.
import matplotlib  # noqa: E402
matplotlib.use("Agg")


def _make_pcb_session():
    """Build a tiny synthetic Session in 'pcb' coord mode for viz tests."""
    from commands.autoplacer import Component, Net, Pin, Session
    sess = Session(schematic_path=Path("pcb://test"))
    sess.params.use_obb_repulsion = True
    sess.params.use_spring_classes = True
    sess.params.attraction_k = 0.1
    sess.params.repulsion_k = 30.0
    # Two anchored connectors + two passive components on a net.
    for ref, pinned in (("J1", True), ("U1", False), ("C1", False), ("R1", False)):
        c = Component(
            ref=ref, unit=1, lib_id="Test:x",
            x=10.0 + ord(ref[0]) % 50, y=20.0 + ord(ref[-1]) % 30,
            rotation=0.0, mirror_x=False, mirror_y=False,
            bbox_w=4.0, bbox_h=2.0, pinned=pinned,
            layer="F.Cu", coord_system="pcb",
        )
        c.pins["1"] = Pin(number="1", name="", local_x=-2.0, local_y=0.0, lib_angle=180.0)
        c.pins["2"] = Pin(number="2", name="", local_x=2.0,  local_y=0.0, lib_angle=0.0)
        sess.components[c.key] = c
    # One signal net pulling them together.
    sess.nets["SIG"] = Net(
        name="SIG",
        pins=[("J1__u1", "1"), ("U1__u1", "1"), ("C1__u1", "1"), ("R1__u1", "1")],
    )
    # One GND net classified PLANE.
    sess.nets["GND"] = Net(
        name="GND", spring_class="PLANE",
        pins=[("J1__u1", "2"), ("U1__u1", "2"), ("C1__u1", "2"), ("R1__u1", "2")],
    )
    return sess


@pytest.mark.unit
class TestPCBAutoplacerViz:
    def test_construct_and_update(self):
        from commands.pcb_autoplacer_viz import PCBAutoplacerViz
        sess = _make_pcb_session()
        viz = PCBAutoplacerViz(
            sess, keep_in_bbox=(0.0, 0.0, 100.0, 80.0), figsize=(8, 6),
        )
        viz.update()
        viz.close()

    def test_save_png(self, tmp_path):
        from commands.pcb_autoplacer_viz import PCBAutoplacerViz
        sess = _make_pcb_session()
        viz = PCBAutoplacerViz(sess, keep_in_bbox=(0.0, 0.0, 100.0, 80.0))
        out = tmp_path / "state.png"
        viz.save(str(out), dpi=80)
        assert out.exists() and out.stat().st_size > 0
        viz.close()

    def test_empty_session_does_not_crash(self):
        from commands.autoplacer import Session
        from commands.pcb_autoplacer_viz import PCBAutoplacerViz
        sess = Session(schematic_path=Path("pcb://empty"))
        viz = PCBAutoplacerViz(sess)
        viz.update()
        viz.close()

    def test_force_options_toggleable(self):
        from commands.pcb_autoplacer_viz import PCBAutoplacerViz
        sess = _make_pcb_session()
        viz = PCBAutoplacerViz(
            sess, keep_in_bbox=(0.0, 0.0, 100.0, 80.0),
            show_sum_force=False, show_repulsion=True,
            show_ratsnest=False, show_pins=False,
        )
        viz.update()
        viz.close()

    def test_compute_forces_matches_iterate(self):
        """The viz's force snapshot should equal what iterate() applies
        on the first step (no mutation in between)."""
        from commands.autoplacer import iterate
        from commands.pcb_autoplacer_viz import PCBAutoplacerViz
        sess = _make_pcb_session()
        viz = PCBAutoplacerViz(sess)
        forces_before_iter = viz._compute_forces()
        # The component positions don't change just from reading forces.
        positions_before = {k: (c.x, c.y) for k, c in sess.components.items()}
        # Iterate one step and check that the previously-computed forces
        # were the ones that moved unanchored components.
        iterate(sess, n=1)
        for key, (ox, oy) in positions_before.items():
            c = sess.components[key]
            if c.pinned:
                assert (c.x, c.y) == (ox, oy)
            else:
                fx, fy = forces_before_iter[key]
                # Movement direction should match force direction (sign).
                if abs(fx) > 1e-3:
                    assert (c.x - ox) * fx >= 0
                if abs(fy) > 1e-3:
                    assert (c.y - oy) * fy >= 0
        viz.close()
