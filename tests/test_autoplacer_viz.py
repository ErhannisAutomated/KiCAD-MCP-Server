"""Smoke tests for the autoplacer visualizer.

The viz is rendered live with matplotlib in interactive use; under
test we use the Agg backend, build a synthetic session, and just
verify that construction + update + save complete without raising.
Pixel-level assertions would be brittle and noisy.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Force a non-interactive backend BEFORE pyplot is imported anywhere.
os.environ.setdefault("MPLCONFIGDIR", "/tmp/mpl-cache")
import matplotlib  # noqa: E402

matplotlib.use("Agg")

PYTHON_DIR = Path(__file__).parent.parent / "python"
sys.path.insert(0, str(PYTHON_DIR))


def _make_synthetic_session():
    """Build a tiny Session with two components on a single net."""
    from commands.autoplacer import Component, Net, Pin, Session

    sess = Session(schematic_path=Path("/tmp/synthetic.kicad_sch"))
    sess.components["R1__u1"] = Component(
        ref="R1", unit=1, lib_id="Device:R", x=120.0, y=100.0, rotation=0,
        mirror_x=False, mirror_y=False,
        pins={
            "1": Pin("1", "~", local_x=0, local_y=3.81, lib_angle=270),
            "2": Pin("2", "~", local_x=0, local_y=-3.81, lib_angle=90),
        },
        bbox_w=12.7, bbox_h=12.7,
    )
    sess.components["R2__u1"] = Component(
        ref="R2", unit=1, lib_id="Device:R", x=160.0, y=100.0, rotation=0,
        mirror_x=False, mirror_y=False,
        pins={
            "1": Pin("1", "~", local_x=0, local_y=3.81, lib_angle=270),
            "2": Pin("2", "~", local_x=0, local_y=-3.81, lib_angle=90),
        },
        bbox_w=12.7, bbox_h=12.7,
    )
    sess.nets["SIG"] = Net(
        name="SIG",
        pins=[("R1__u1", "2"), ("R2__u1", "1")],
    )
    return sess


@pytest.mark.unit
def test_viz_constructs_and_updates_without_error():
    """Smoke: constructing AutoplacerViz on a synthetic session must
    not raise, and `.update()` must redraw cleanly."""
    from commands.autoplacer_viz import AutoplacerViz

    sess = _make_synthetic_session()
    viz = AutoplacerViz(sess)
    try:
        viz.update()
    finally:
        viz.close()


@pytest.mark.unit
def test_viz_save_writes_a_png():
    """`viz.save(path)` must produce a non-empty file."""
    from commands.autoplacer_viz import AutoplacerViz

    sess = _make_synthetic_session()
    viz = AutoplacerViz(sess)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "viz.png"
            viz.save(str(out))
            assert out.exists() and out.stat().st_size > 1000, (
                f"png missing or too small: {out.stat().st_size} bytes"
            )
    finally:
        viz.close()


@pytest.mark.unit
def test_viz_with_empty_session_does_not_explode():
    """No components, no nets — viz should still construct and update."""
    from commands.autoplacer import Session
    from commands.autoplacer_viz import AutoplacerViz

    sess = Session(schematic_path=Path("/tmp/empty.kicad_sch"))
    viz = AutoplacerViz(sess)
    try:
        viz.update()
    finally:
        viz.close()


@pytest.mark.unit
def test_show_session_errors_when_no_session_loaded():
    """show_session should raise a clean error if PLACER.get() is None."""
    from commands.autoplacer import PLACER
    from commands.autoplacer_viz import show_session

    PLACER.sessions.clear()
    with pytest.raises(ValueError, match="No autoplacer session loaded"):
        show_session("/tmp/nonexistent.kicad_sch")


@pytest.mark.unit
def test_viz_toggles_force_layers_on_and_off():
    """Constructor flags for force layers should be respected."""
    from commands.autoplacer_viz import AutoplacerViz

    sess = _make_synthetic_session()
    viz = AutoplacerViz(
        sess,
        show_attraction=False,
        show_repulsion=False,
        show_sum_force=False,
    )
    try:
        viz.update()  # should still render component bboxes only
    finally:
        viz.close()
