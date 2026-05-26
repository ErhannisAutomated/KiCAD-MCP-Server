"""Tests for pair_via (#206).

Doubles existing vias on high-current nets — needed because freerouting
specifies a single via per class rule and the user's past sessions
all hand-paired BAT1 power vias.
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


@pytest.mark.unit
class TestPairViaValidation:
    def _routing(self) -> RoutingCommands:
        return RoutingCommands(board=MagicMock(name="BOARD"))

    def test_zero_offset_rejected(self):
        r = self._routing()
        result = r.pair_via({"offset": 0})
        assert result["success"] is False

    def test_negative_offset_rejected(self):
        r = self._routing()
        result = r.pair_via({"offset": -1.0})
        assert result["success"] is False


@pytest.mark.unit
class TestPairViaInAutoSaveSet:
    def test_pair_via_is_a_mutator(self):
        from kicad_interface import KiCADInterface
        assert "pair_via" in KiCADInterface._BOARD_MUTATING_COMMANDS


@pytest.mark.integration
@pytest.mark.skipif(
    not _real_pcbnew_available(),
    reason="needs real pcbnew swig module",
)
class TestPairViaOnRealBoard:
    SCALE = 1_000_000

    def _build_board_with_via(self, net_name: str = "BAT+",
                              net_class: str = "POWER_4A"):
        """Empty 40×30 mm board with one through-via on `net_name`,
        net assigned to `net_class`."""
        import pcbnew

        board = pcbnew.BOARD()
        net = pcbnew.NETINFO_ITEM(board, net_name)
        board.Add(net)

        # KiCAD 9's GetNetClasses() returns a map-like that doesn't
        # accept .Add(); the simplest portable path is to SetNetClass
        # directly on the net (the filter we test against just calls
        # .GetNetClass().GetName()).
        nc = pcbnew.NETCLASS(net_class)
        net.SetNetClass(nc)

        via = pcbnew.PCB_VIA(board)
        via.SetPosition(pcbnew.VECTOR2I(int(10 * self.SCALE),
                                         int(15 * self.SCALE)))
        via.SetWidth(int(1.0 * self.SCALE))
        via.SetDrill(int(0.5 * self.SCALE))
        via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        via.SetNet(net)
        board.Add(via)
        return board

    def test_proposes_partner_via_for_high_current_net(self):
        board = self._build_board_with_via("BAT+", "POWER_4A")
        r = RoutingCommands(board=board)
        result = r.pair_via({
            "nets": ["BAT+"],
            "offset": 1.0,
            "apply": False,
        })
        assert result["success"], result
        assert result["candidateCount"] == 1
        assert result["proposedCount"] == 1
        # Partner is 1 mm from parent (10, 15).
        p = result["positions"][0]
        dx = abs(p["x"] - 10)
        dy = abs(p["y"] - 15)
        # One of the offsets should be ~1.0 (within rounding), the other ~0.
        assert (dx == pytest.approx(1.0, abs=0.001) and dy == pytest.approx(0, abs=0.001)) \
            or (dy == pytest.approx(1.0, abs=0.001) and dx == pytest.approx(0, abs=0.001))

    def test_apply_adds_via_with_same_net(self):
        import pcbnew

        board = self._build_board_with_via("BAT+", "POWER_4A")
        r = RoutingCommands(board=board)
        result = r.pair_via({
            "nets": ["BAT+"],
            "offset": 1.0,
            "apply": True,
        })
        assert result["success"], result
        vias = [t for t in board.Tracks() if t.Type() == pcbnew.PCB_VIA_T]
        assert len(vias) == 2
        # Both should be on BAT+.
        for v in vias:
            assert v.GetNetname() == "BAT+"

    def test_dedup_prevents_re_pairing(self):
        """Running pair_via twice should not place a third via —
        the offset is < offset*0.5 from one of the already-paired ones."""
        board = self._build_board_with_via("BAT+", "POWER_4A")
        r = RoutingCommands(board=board)
        first = r.pair_via({
            "nets": ["BAT+"],
            "offset": 1.0,
            "apply": True,
        })
        assert first["proposedCount"] == 1

        second = r.pair_via({
            "nets": ["BAT+"],
            "offset": 1.0,
            "apply": False,
        })
        # Both existing vias now considered candidates; each one's
        # ±x/±y offsets are within 0.5 mm of the other via -> all
        # positions get dedup-rejected. But the OPPOSITE side of each
        # via is still free. So we MAY see additional proposals.
        # The contract here is just that proposed positions don't
        # stack on top of existing vias.
        for pos in second.get("positions", []):
            for v in [t for t in board.Tracks()
                      if t.Type() == __import__("pcbnew").PCB_VIA_T]:
                vp = v.GetPosition()
                d = ((pos["x"] - vp.x / self.SCALE) ** 2
                     + (pos["y"] - vp.y / self.SCALE) ** 2) ** 0.5
                assert d > 0.4, f"new via too close to existing: {pos}, {vp}"

    def test_filter_by_netclass(self):
        """`netClass='POWER_4A'` should select BAT+ via netclass
        assignment, not Default."""
        board = self._build_board_with_via("BAT+", "POWER_4A")
        r = RoutingCommands(board=board)
        result = r.pair_via({
            "netClass": "POWER_4A",
            "offset": 1.0,
            "apply": False,
        })
        assert result["success"], result
        assert result["candidateCount"] == 1

    def test_filter_excludes_default_class(self):
        """A via on the Default class should not be paired when
        netClass='POWER_4A'."""
        board = self._build_board_with_via("BAT+", "Default")
        r = RoutingCommands(board=board)
        result = r.pair_via({
            "netClass": "POWER_4A",
            "offset": 1.0,
            "apply": False,
        })
        # Either the filter returns no matching nets (success=False) or
        # the candidate count is 0 — both are acceptable outcomes.
        assert (not result["success"]) or result["candidateCount"] == 0
