"""Regression test: BOARD.RemoveNative(item) must NOT corrupt the
process-global SWIG type state, whereas the buggy BOARD.Remove(item)
does after a few hundred calls.

Symptom of the bug: after a mass-remove via b.Remove(), subsequent
b.GetDesignSettings() / b.GetNetInfo() / b.GetTracks() (and even a
fresh pcbnew.LoadBoard in the same process) return bare SwigPyObject
instead of their typed wrappers, breaking every downstream operation.
RemoveNative skips whatever listener/undo path triggers the corruption.

Routing.py and component.py both use RemoveNative for that reason.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


def _real_pcbnew_available() -> bool:
    import pcbnew  # type: ignore

    return getattr(pcbnew, "GetBuildVersion", lambda: "")() != "9.0.0-stub"


def _board_with_n_tracks(n: int):
    """Synthetic board with `n` short F.Cu tracks on the same net."""
    import pcbnew

    board = pcbnew.BOARD()
    for i in range(n):
        t = pcbnew.PCB_TRACK(board)
        t.SetStart(pcbnew.VECTOR2I(i * 1_000_000, 0))
        t.SetEnd(pcbnew.VECTOR2I(i * 1_000_000, 5_000_000))
        t.SetLayer(pcbnew.F_Cu)
        t.SetWidth(200_000)
        board.Add(t)
    return board


@pytest.mark.skipif(
    not _real_pcbnew_available(),
    reason="real pcbnew not available (test stub in use)",
)
class TestRemoveNativeDoesNotCorrupt:
    def test_remove_native_preserves_swig_types(self):
        import pcbnew

        # 700 is comfortably past the corruption threshold observed in
        # power_module (~658 tracks triggers it for plain Remove).
        board = _board_with_n_tracks(700)
        assert isinstance(board.GetDesignSettings(), pcbnew.BOARD_DESIGN_SETTINGS)
        for t in list(board.GetTracks()):
            board.RemoveNative(t)
        # Singletons must still be properly typed afterwards
        ds = board.GetDesignSettings()
        ni = board.GetNetInfo()
        assert isinstance(ds, pcbnew.BOARD_DESIGN_SETTINGS), type(ds).__name__
        assert isinstance(ni, pcbnew.NETINFO_LIST), type(ni).__name__
        # The board's track collection should still iterate
        assert len(list(board.GetTracks())) == 0

    def test_remove_native_then_add_via_in_same_process(self):
        import pcbnew

        board = _board_with_n_tracks(700)
        for t in list(board.GetTracks()):
            board.RemoveNative(t)
        # This is the call that used to fail with
        # "'SwigPyObject' object has no attribute 'GetDesignSettings'"
        v = pcbnew.PCB_VIA(board)
        v.SetPosition(pcbnew.VECTOR2I(50_000_000, 50_000_000))
        v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        ds = board.GetDesignSettings()
        v.SetWidth(ds.GetCurrentViaSize())
        board.Add(v)
        assert sum(1 for t in board.GetTracks() if t.Type() == pcbnew.PCB_VIA_T) == 1
