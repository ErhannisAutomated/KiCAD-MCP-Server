"""Tests for the mtime-based board freshness check.

Covers KiCADInterface._ensure_board_fresh — fires when a caller passes a
boardPath matching the cached self.board's filename and the on-disk file
is newer than what we last loaded/saved. Closes the long-standing
"self.board invisible to out-of-band file edits" issue.

Two layers:
  - Unit: behaviour of the path/mtime decision logic with a stub board
    (works in the conftest stub environment).
  - Integration: a real round-trip through handle_command with pcbnew —
    save board, hand-edit via a fresh LoadBoard + property change, call a
    read-only command with boardPath, assert the change is visible.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Optional

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


def _real_pcbnew_available() -> bool:
    import pcbnew  # type: ignore

    return getattr(pcbnew, "GetBuildVersion", lambda: "")() != "9.0.0-stub"


class _StubBoard:
    """Minimal board stand-in for unit tests — only the calls that
    _ensure_board_fresh makes (GetFileName)."""

    def __init__(self, filename: str) -> None:
        self._filename = filename

    def GetFileName(self) -> str:
        return self._filename


class TestEnsureBoardFreshUnit:
    """Decision-logic tests with stub board. No pcbnew required."""

    def _ki(self):
        from kicad_interface import KiCADInterface

        return KiCADInterface()

    def test_no_board_loaded_is_noop(self, tmp_path):
        ki = self._ki()
        assert ki.board is None
        # Should NOT raise even though _board_disk_mtime is None.
        ki._ensure_board_fresh("get_board_info", {"boardPath": str(tmp_path / "x.kicad_pcb")})

    def test_no_disk_mtime_recorded_is_noop(self, tmp_path):
        ki = self._ki()
        pcb = tmp_path / "x.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        ki.board = _StubBoard(str(pcb))
        ki._board_disk_mtime = None
        # No baseline → cannot decide. Don't crash, don't reload.
        ki._ensure_board_fresh("get_board_info", {"boardPath": str(pcb)})

    def test_boardpath_missing_uses_loaded_board_path(self, tmp_path, monkeypatch):
        """Diagnostic tools (run_drc, get_drc_violations) don't take a
        boardPath but they DO save self.board before invoking kicad-cli.
        The freshness check MUST fire on this path or those saves clobber
        out-of-band edits. (Bug hit 2026-06-16 — run_drc reverted a
        GUI-saved hand-routing pass.)"""
        ki = self._ki()
        pcb = tmp_path / "x.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        ki.board = _StubBoard(str(pcb))
        ki._board_disk_mtime = os.path.getmtime(pcb) - 100  # cached < disk

        called = {"loaded": False}

        def _fake_load(path):
            called["loaded"] = True
            return _StubBoard(path)

        import pcbnew as _pcbnew

        monkeypatch.setattr(_pcbnew, "LoadBoard", _fake_load)
        # No boardPath in params — the check must still fire because the
        # loaded board has a filename and disk is newer than cache.
        ki._ensure_board_fresh("run_drc", {})
        assert called["loaded"], "expected reload when disk is newer than cache"

    def test_boardpath_mismatch_is_noop(self, tmp_path):
        ki = self._ki()
        pcb_a = tmp_path / "a.kicad_pcb"
        pcb_b = tmp_path / "b.kicad_pcb"
        pcb_a.write_text("(kicad_pcb)")
        pcb_b.write_text("(kicad_pcb)")
        ki.board = _StubBoard(str(pcb_a))
        ki._board_disk_mtime = os.path.getmtime(pcb_a)
        # boardPath points at a DIFFERENT file → another handler will
        # decide whether to load it. _ensure_board_fresh stays out.
        ki._ensure_board_fresh("get_board_info", {"boardPath": str(pcb_b)})
        assert isinstance(ki.board, _StubBoard)  # still the stub

    def test_unchanged_mtime_is_noop(self, tmp_path):
        ki = self._ki()
        pcb = tmp_path / "x.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        ki.board = _StubBoard(str(pcb))
        ki._board_disk_mtime = os.path.getmtime(pcb)
        # Same mtime → no reload triggered. Stub board still in place.
        ki._ensure_board_fresh("get_board_info", {"boardPath": str(pcb)})
        assert isinstance(ki.board, _StubBoard)

    def test_record_board_disk_mtime_with_missing_file(self, tmp_path):
        ki = self._ki()
        # Recording for a non-existent file should clear the cache,
        # not raise.
        ki._board_disk_mtime = 12345.6
        ki._record_board_disk_mtime(str(tmp_path / "nope.kicad_pcb"))
        assert ki._board_disk_mtime is None

    def test_record_board_disk_mtime_with_none(self, tmp_path):
        ki = self._ki()
        ki._board_disk_mtime = 12345.6
        ki._record_board_disk_mtime(None)
        assert ki._board_disk_mtime is None

    def test_handle_command_invokes_ensure_board_fresh(self, monkeypatch):
        """Regression guard for the wiring: every handle_command call must
        give _ensure_board_fresh a chance to spot out-of-band writes
        before dispatching. If the call is ever moved or removed, this
        test fires."""
        ki = self._ki()
        calls = []

        def _spy(command, params):
            calls.append((command, dict(params) if isinstance(params, dict) else params))

        monkeypatch.setattr(ki, "_ensure_board_fresh", _spy)
        # Dispatch a command that's not in the routing table — exits the
        # try-block via the "Unknown command" path, AFTER the freshness
        # check has been invoked.
        result = ki.handle_command("__not_a_real_command__", {"boardPath": "/tmp/x.kicad_pcb"})
        assert result["success"] is False
        assert calls == [("__not_a_real_command__", {"boardPath": "/tmp/x.kicad_pcb"})]


@pytest.mark.skipif(
    not _real_pcbnew_available(), reason="needs real pcbnew (skipped under SWIG stub)"
)
class TestEnsureBoardFreshIntegration:
    """Real pcbnew round-trip: stamp mtime → mutate file out of band →
    next call must see the new state."""

    def _ki(self):
        from kicad_interface import KiCADInterface

        return KiCADInterface()

    def _empty_board(self, tmp_path: Path):
        import pcbnew

        b = pcbnew.BOARD()
        path = tmp_path / "freshness.kicad_pcb"
        b.SetFileName(str(path))
        pcbnew.SaveBoard(str(path), b)
        return b, path

    def test_out_of_band_edit_triggers_reload_before_next_command(self, tmp_path):
        import pcbnew

        ki = self._ki()
        b, pcb_path = self._empty_board(tmp_path)
        ki.board = b
        ki._update_command_handlers()
        ki._record_board_disk_mtime(str(pcb_path))
        first_id = id(ki.board)
        first_mtime = ki._board_disk_mtime

        # Simulate an out-of-band write: open the file in a separate
        # in-memory board, drop a footprint with a distinctive ref so we
        # can prove the next read picked it up.
        time.sleep(1.1)  # mtime resolution on some FSes is 1s
        b2 = pcbnew.LoadBoard(str(pcb_path))
        fp = pcbnew.FOOTPRINT(b2)
        fp.SetFPID(pcbnew.LIB_ID("test", "freshness_test"))
        fp.SetReference("XOOB1")
        b2.Add(fp)
        pcbnew.SaveBoard(str(pcb_path), b2)
        disk_mtime = os.path.getmtime(pcb_path)
        assert disk_mtime > first_mtime, "test setup: disk mtime did not advance"

        ki._ensure_board_fresh("get_board_info", {"boardPath": str(pcb_path)})

        assert id(ki.board) != first_id, "expected a fresh BOARD instance after reload"
        refs = {fp.GetReference() for fp in ki.board.GetFootprints()}
        assert "XOOB1" in refs, "freshly-loaded board should see the out-of-band footprint"
        assert ki._board_disk_mtime == disk_mtime
