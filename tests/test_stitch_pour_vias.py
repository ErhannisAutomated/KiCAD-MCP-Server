"""Tests for stitch_pour_vias (#176).

* Unit tests with mocked pcbnew cover parameter validation and the
  no-zones early-return.
* Integration tests with real pcbnew build a small board with a
  filled GND zone and assert proposals land inside the zone, respect
  the grid, and dedupe against existing vias.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from commands.routing import RoutingCommands  # noqa: E402


def _real_pcbnew_available() -> bool:
    import pcbnew  # type: ignore
    return getattr(pcbnew, "GetBuildVersion", lambda: "")() != "9.0.0-stub"


# ---------------------------------------------------------------------------
# Unit tests — mock pcbnew.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStitchPourViasValidation:
    def _routing(self) -> RoutingCommands:
        return RoutingCommands(board=MagicMock(name="BOARD"))

    def test_missing_net_errors(self):
        r = self._routing()
        result = r.stitch_pour_vias({"gridPitch": 2.0})
        assert result["success"] is False
        assert "net is required" in result["errorDetails"]

    def test_missing_grid_pitch_errors(self):
        r = self._routing()
        result = r.stitch_pour_vias({"net": "GND"})
        assert result["success"] is False
        assert "gridPitch" in result["errorDetails"]

    def test_zero_grid_pitch_errors(self):
        r = self._routing()
        result = r.stitch_pour_vias({"net": "GND", "gridPitch": 0})
        assert result["success"] is False
        assert "gridPitch" in result["errorDetails"]

    def test_no_zones_on_net_errors(self):
        """If the user asks to stitch a net that has no copper pour,
        return a clear actionable error rather than silently succeeding
        with zero proposals."""
        r = self._routing()
        # Mock the board returning no zones on the requested net.
        r.board.Zones.return_value = []
        result = r.stitch_pour_vias({"net": "GND", "gridPitch": 2.0})
        assert result["success"] is False
        assert "No zones on net 'GND'" in result["message"]


@pytest.mark.unit
class TestStitchPourViasInAutoSaveSet:
    """Adding stitching vias mutates the board, so the tool must be in
    the auto-save set or external readers (kicad-cli, fresh
    pcbnew.LoadBoard) see the previous state."""

    def test_stitch_pour_vias_is_a_mutator(self):
        from kicad_interface import KiCADInterface
        assert "stitch_pour_vias" in KiCADInterface._BOARD_MUTATING_COMMANDS


# ---------------------------------------------------------------------------
# Integration tests — need real pcbnew.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(
    not _real_pcbnew_available(),
    reason="needs real pcbnew swig module",
)
class TestStitchPourViasOnRealBoard:
    SCALE = 1_000_000

    def _build_board_with_gnd_zone(self):
        """40×30 mm board with a single rectangular GND zone on F.Cu
        covering the full area. The implementation uses HitTest on the
        outline (not HitTestFilledArea) so no `ZONE_FILLER.Fill()` is
        needed — that call has a known SWIG segfault risk."""
        import pcbnew

        board = pcbnew.BOARD()
        net = pcbnew.NETINFO_ITEM(board, "GND")
        board.Add(net)

        zone = pcbnew.ZONE(board)
        zone.SetLayer(pcbnew.F_Cu)
        zone.SetNet(board.GetNetInfo().NetsByName()["GND"])
        outline = zone.Outline()
        outline.NewOutline()
        for x_mm, y_mm in [(0, 0), (40, 0), (40, 30), (0, 30)]:
            outline.Append(
                int(x_mm * self.SCALE),
                int(y_mm * self.SCALE),
            )
        board.Add(zone)
        return board

    def test_proposes_vias_inside_zone(self):
        board = self._build_board_with_gnd_zone()
        r = RoutingCommands(board=board)
        result = r.stitch_pour_vias({
            "net": "GND",
            "gridPitch": 5.0,
            "apply": False,
        })
        assert result["success"], result
        assert result["proposedCount"] > 0
        # All proposed positions should be within the 0..40 / 0..30 zone.
        for p in result["positions"]:
            assert 0 <= p["x"] <= 40, p
            assert 0 <= p["y"] <= 30, p

    def test_interior_grid_points_get_proposed(self):
        """Regression for the original ship: stitch_pour_vias was using
        ZONE.HitTest, which only matches the outline boundary. Interior
        points like (20, 15) inside a 40×30 zone were silently skipped.
        With Outline().Contains(), they should be proposed."""
        board = self._build_board_with_gnd_zone()
        r = RoutingCommands(board=board)
        result = r.stitch_pour_vias({
            "net": "GND",
            "gridPitch": 5.0,
            "apply": False,
        })
        # Strong interior point — far from any edge.
        interior_xy = (20.0, 15.0)
        positions = [(p["x"], p["y"]) for p in result["positions"]]
        assert interior_xy in positions, (
            f"interior point {interior_xy} missing from positions; "
            f"got {positions}"
        )

    def test_off_grid_outside_point_not_proposed(self):
        """A point well outside the zone bbox cannot be proposed."""
        board = self._build_board_with_gnd_zone()
        r = RoutingCommands(board=board)
        result = r.stitch_pour_vias({
            "net": "GND",
            "gridPitch": 5.0,
            "apply": False,
        })
        for p in result["positions"]:
            assert not (p["x"] > 40 or p["y"] > 30 or p["x"] < 0 or p["y"] < 0), p

    def test_apply_adds_vias_to_board(self):
        import pcbnew

        board = self._build_board_with_gnd_zone()
        r = RoutingCommands(board=board)
        before = sum(1 for t in board.Tracks() if t.Type() == pcbnew.PCB_VIA_T)
        result = r.stitch_pour_vias({
            "net": "GND",
            "gridPitch": 5.0,
            "apply": True,
        })
        assert result["success"], result
        after = sum(1 for t in board.Tracks() if t.Type() == pcbnew.PCB_VIA_T)
        assert after - before == result["proposedCount"]

    def test_dedupes_against_existing_same_net_via(self):
        """A second invocation with the same grid_pitch should propose
        zero new vias because every candidate is too close to one we
        already added."""
        board = self._build_board_with_gnd_zone()
        r = RoutingCommands(board=board)
        first = r.stitch_pour_vias({
            "net": "GND",
            "gridPitch": 5.0,
            "apply": True,
        })
        assert first["proposedCount"] > 0

        second = r.stitch_pour_vias({
            "net": "GND",
            "gridPitch": 5.0,
            "apply": False,
        })
        assert second["proposedCount"] == 0
        assert second["skippedDedup"] > 0

    def test_max_vias_caps_output(self):
        board = self._build_board_with_gnd_zone()
        r = RoutingCommands(board=board)
        result = r.stitch_pour_vias({
            "net": "GND",
            "gridPitch": 1.0,   # tight grid → would propose lots
            "maxVias": 5,
        })
        assert result["success"], result
        assert result["proposedCount"] <= 5
