"""Tests for the SES import safety guard (#241).

`pcbnew.ImportSpecctraSES` is replace-like: the board ends up with
whatever the session contains. A degenerate session (0 routes, or far
fewer than the board already has) would therefore wipe/decimate existing
routing on import. `_ses_import_guard` refuses such imports unless
forceImport is set. Pure-logic tests — no pcbnew required.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


def test_empty_session_is_blocked():
    from commands.freerouting import _ses_import_guard

    guard = _ses_import_guard(ses_wires=0, board_tracks_before=599, force_import=False)
    assert guard is not None
    assert guard["success"] is False
    assert "0 routes" in guard["message"]


def test_empty_session_blocked_even_on_empty_board():
    """0 routes is always a failure signal, regardless of board state."""
    from commands.freerouting import _ses_import_guard

    guard = _ses_import_guard(ses_wires=0, board_tracks_before=0, force_import=False)
    assert guard is not None


def test_far_fewer_routes_is_blocked():
    from commands.freerouting import _ses_import_guard

    # 30 routed wires vs 599 existing tracks -> would discard most routing.
    guard = _ses_import_guard(ses_wires=30, board_tracks_before=599, force_import=False)
    assert guard is not None
    assert "far fewer" in guard["message"]


def test_full_reroute_passes():
    """A session with ~as many routes as the board is a normal reroute."""
    from commands.freerouting import _ses_import_guard

    assert _ses_import_guard(610, 599, force_import=False) is None
    assert _ses_import_guard(580, 599, force_import=False) is None  # within 50%


def test_stripped_board_full_route_passes():
    """Routing a freshly-stripped board (0 tracks before) is fine."""
    from commands.freerouting import _ses_import_guard

    assert _ses_import_guard(450, 0, force_import=False) is None


def test_small_board_below_floor_not_fraction_checked():
    """Below the guard floor, only the empty-session check applies."""
    from commands.freerouting import _ses_import_guard

    # 3 wires vs 10 existing: under the floor, fraction check is skipped.
    assert _ses_import_guard(3, 10, force_import=False) is None


def test_force_import_bypasses_guard():
    from commands.freerouting import _ses_import_guard

    assert _ses_import_guard(0, 599, force_import=True) is None
    assert _ses_import_guard(1, 599, force_import=True) is None


def test_read_error_does_not_block():
    """ses_wires < 0 signals a read error; that's surfaced elsewhere and
    must not, by itself, block the import."""
    from commands.freerouting import _ses_import_guard

    assert _ses_import_guard(-1, 599, force_import=False) is None


def test_incremental_skips_fraction_check():
    """Incremental mode (#242) copies only the target nets onto the live
    board — the import isn't replace-like against it, so a session with
    far fewer routes than the board has must NOT be blocked."""
    from commands.freerouting import _ses_import_guard

    # Whole-board would block this; incremental must allow it.
    assert _ses_import_guard(30, 599, force_import=False) is not None
    assert _ses_import_guard(30, 599, force_import=False, incremental=True) is None


def test_incremental_still_blocks_empty_session():
    """An empty SES means freerouting routed nothing — even in
    incremental mode there's nothing to copy, so still abort."""
    from commands.freerouting import _ses_import_guard

    guard = _ses_import_guard(0, 599, force_import=False, incremental=True)
    assert guard is not None
    assert "0 routes" in guard["message"]


def test_incremental_force_import_bypasses():
    from commands.freerouting import _ses_import_guard

    assert _ses_import_guard(0, 599, force_import=True, incremental=True) is None


def test_count_ses_wires(tmp_path):
    from commands.freerouting import _count_ses_wires

    # Space form: "(wire (path …)" — the inline style.
    ses = tmp_path / "x.ses"
    ses.write_text(
        "(session x\n  (routes\n    (network_out\n"
        "      (net BAT+ (wire (path F.Cu 250 0 0 10 0)))\n"
        "      (net GND (wire (path B.Cu 250 0 0 10 0))(wire (path B.Cu 250 0 0 5 5)))\n"
        "    )\n  )\n)\n"
    )
    assert _count_ses_wires(str(ses)) == 3

    empty = tmp_path / "empty.ses"
    empty.write_text("(session x\n  (routes (network_out))\n)\n")
    assert _count_ses_wires(str(empty)) == 0

    assert _count_ses_wires(str(tmp_path / "nope.ses")) == -1


def test_count_ses_wires_newline_form(tmp_path):
    """Freerouting v2 pretty-prints `(wire\\n  (path …)` — the wire token
    is followed by a newline, not a space. The counter must catch this;
    counting the literal `"(wire "` returned 0 and made the guard falsely
    abort every real session (caught live on power_module 2026-05-28)."""
    from commands.freerouting import _count_ses_wires

    ses = tmp_path / "nl.ses"
    ses.write_text(
        "(session x\n  (routes\n    (network_out\n"
        "      (net GND\n"
        "        (wire\n          (path F.Cu 250 0 0 10 0)\n        )\n"
        "        (wire\n          (path B.Cu 250 0 0 5 5)\n        )\n"
        "      )\n    )\n  )\n)\n"
    )
    assert _count_ses_wires(str(ses)) == 2

    # `(wiring` (a DSN token) must NOT be counted as a wire.
    dsn_ish = tmp_path / "w.ses"
    dsn_ish.write_text("(wiring\n  (wire\n    (path F.Cu 250 0 0 1 1)\n  )\n)\n")
    assert _count_ses_wires(str(dsn_ish)) == 1
