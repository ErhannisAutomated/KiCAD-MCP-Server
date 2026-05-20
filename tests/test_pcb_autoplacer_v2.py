"""Smoke tests for the v2 unified PCB autoplacer (relax_placement).

Exercises the PCBAdapter: load → relax → apply round-trip on a real
small board, checking:
  - Footprint pad world coords roundtrip through engine model.
  - relax_placement succeeds on a small board with no crashes.
  - Anchored components (J*, BAT*, SW*, through-hole-dominant) don't move.
  - Power nets auto-classify as PLANE.
  - dry_run=True doesn't mutate the board.
"""
from __future__ import annotations

import math
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

PYTHON_DIR = Path(__file__).parent.parent / "python"
sys.path.insert(0, str(PYTHON_DIR))


def _real_pcbnew_available() -> bool:
    import pcbnew  # type: ignore
    return getattr(pcbnew, "GetBuildVersion", lambda: "")() != "9.0.0-stub"


pytestmark = pytest.mark.skipif(
    not _real_pcbnew_available(),
    reason="needs the real pcbnew swig module (conftest stubs it for unit tests)",
)

import pcbnew  # noqa: E402

BLINK_PCB = Path("/home/vagrant/projects/kicad_agent/projects/blink_555/blink_555.kicad_pcb")


@pytest.fixture
def fresh_board(tmp_path):
    """Copy blink_555 board to a temp location so the test can mutate
    it without affecting the project."""
    if not BLINK_PCB.exists():
        pytest.skip(f"test fixture not present: {BLINK_PCB}")
    dst = tmp_path / "test.kicad_pcb"
    shutil.copy(BLINK_PCB, dst)
    return pcbnew.LoadBoard(str(dst))


@pytest.mark.integration
class TestPCBAdapter:
    def test_load_session_basic(self, fresh_board):
        from commands.pcb_autoplacer import load_pcb_session
        sess = load_pcb_session(fresh_board)
        assert sess.components, "should load at least one footprint"
        assert sess.nets, "should discover at least one net"
        # All loaded comps should be marked "pcb" coord system
        for c in sess.components.values():
            assert c.coord_system == "pcb"

    def test_pin_world_coord_roundtrip(self, fresh_board):
        """The engine's world_pin_xy() should reproduce pcbnew's actual
        pad world position to within sub-micron precision."""
        from commands.pcb_autoplacer import load_pcb_session
        sess = load_pcb_session(fresh_board)
        for fp in fresh_board.GetFootprints():
            comp_key = f"{fp.GetReference()}__u1"
            comp = sess.components.get(comp_key)
            if comp is None:
                continue
            for pad in fp.Pads():
                pad_num = str(pad.GetPadName() or pad.GetNumber())
                if not pad_num:
                    continue
                truth_x = pad.GetPosition().x / 1_000_000.0
                truth_y = pad.GetPosition().y / 1_000_000.0
                engine = comp.world_pin_xy(pad_num)
                assert engine is not None, f"engine couldn't compute world coord for {fp.GetReference()}.{pad_num}"
                assert abs(engine[0] - truth_x) < 0.001, (
                    f"{fp.GetReference()}.{pad_num} world X mismatch: "
                    f"truth={truth_x}, engine={engine[0]}"
                )
                assert abs(engine[1] - truth_y) < 0.001, (
                    f"{fp.GetReference()}.{pad_num} world Y mismatch: "
                    f"truth={truth_y}, engine={engine[1]}"
                )

    def test_auto_classify_planes(self, fresh_board):
        from commands.pcb_autoplacer import load_pcb_session
        sess = load_pcb_session(fresh_board, auto_classify_planes=True)
        # GND should be present and classified PLANE
        for net in sess.nets.values():
            if net.name.upper() in ("GND", "VCC", "+5V", "+3V3"):
                assert net.spring_class == "PLANE", (
                    f"net {net.name} should auto-classify as PLANE, got {net.spring_class}"
                )

    def test_anchors_default_jswbat(self, fresh_board):
        """J*/SW*/BAT* refs and through-hole-dominant footprints anchor
        by default (no explicit locked_refs)."""
        from commands.pcb_autoplacer import load_pcb_session
        sess = load_pcb_session(fresh_board)
        for c in sess.components.values():
            if c.ref.startswith(("J", "SW", "BAT")):
                assert c.pinned, f"{c.ref} should be anchored by default"


@pytest.mark.integration
class TestRelaxPlacement:
    def test_dry_run_does_not_mutate(self, fresh_board):
        from commands.pcb_autoplacer import relax_placement
        # Snapshot positions before
        before = {}
        for fp in fresh_board.GetFootprints():
            p = fp.GetPosition()
            before[fp.GetReference()] = (p.x, p.y, fp.GetOrientation().AsDegrees())

        r = relax_placement(
            fresh_board, dry_run=True,
            cluster_iters=5, spread_iters=5, snap_iters=5, relax_iters=2,
        )
        assert r["success"]
        assert r["dry_run"] is True
        assert r["components_written"] == 0

        # Positions should be unchanged.
        for fp in fresh_board.GetFootprints():
            p = fp.GetPosition()
            now = (p.x, p.y, fp.GetOrientation().AsDegrees())
            assert now == before[fp.GetReference()], (
                f"dry_run mutated {fp.GetReference()}: was {before[fp.GetReference()]}, now {now}"
            )

    def test_real_run_moves_components(self, fresh_board):
        from commands.pcb_autoplacer import relax_placement
        r = relax_placement(
            fresh_board, dry_run=False,
            cluster_iters=10, spread_iters=10, snap_iters=10, relax_iters=5,
        )
        assert r["success"]
        assert r["components_written"] > 0, (
            "non-dry run should write at least one component"
        )
        # Anchored count should match what the adapter decided.
        assert r["components_anchored"] >= 1, (
            "at least one comp should be anchored (J* by default)"
        )

    def test_anchored_components_dont_move(self, fresh_board):
        from commands.pcb_autoplacer import relax_placement
        # Capture anchor positions before.
        before = {}
        for fp in fresh_board.GetFootprints():
            if fp.GetReference().startswith(("J", "SW", "BAT")):
                p = fp.GetPosition()
                before[fp.GetReference()] = (p.x, p.y)

        if not before:
            pytest.skip("test board has no J/SW/BAT footprints")

        r = relax_placement(
            fresh_board, dry_run=False,
            cluster_iters=10, spread_iters=10, snap_iters=10, relax_iters=5,
        )
        assert r["success"]

        # Anchor positions should be exactly unchanged.
        for ref, (bx, by) in before.items():
            fp = fresh_board.FindFootprintByReference(ref)
            p = fp.GetPosition()
            assert (p.x, p.y) == (bx, by), (
                f"anchored {ref} moved: was ({bx},{by}), now ({p.x},{p.y})"
            )

    def test_no_board_returns_failure(self):
        """Empty board (no footprints) returns success=False cleanly."""
        from commands.pcb_autoplacer import relax_placement
        board = pcbnew.BOARD()
        r = relax_placement(board, dry_run=True)
        assert r["success"] is False
        assert "footprints" in r["message"].lower() or "no" in r["message"].lower()
