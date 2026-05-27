"""Tests for find_redundant_vias (#224)."""

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
class TestFindRedundantViasInMutatorSet:
    """delete=true mutates the board, so the handler must be in the
    auto-save set."""

    def test_find_redundant_vias_is_a_mutator(self):
        from kicad_interface import KiCADInterface
        assert "find_redundant_vias" in KiCADInterface._BOARD_MUTATING_COMMANDS


@pytest.mark.integration
@pytest.mark.skipif(
    not _real_pcbnew_available(),
    reason="needs real pcbnew swig module",
)
class TestFindRedundantViasOnRealBoard:
    SCALE = 1_000_000

    def _make_board(self):
        import pcbnew
        board = pcbnew.BOARD()
        for n in ("GND", "FOO"):
            board.Add(pcbnew.NETINFO_ITEM(board, n))
        board.GetDesignSettings().m_HoleToHoleMin = int(0.25 * self.SCALE)
        return board

    def _add_via(self, board, net_name, x, y):
        import pcbnew
        net = board.GetNetInfo().NetsByName()[net_name]
        v = pcbnew.PCB_VIA(board)
        v.SetPosition(pcbnew.VECTOR2I(int(x * self.SCALE), int(y * self.SCALE)))
        v.SetWidth(int(0.6 * self.SCALE))
        v.SetDrill(int(0.3 * self.SCALE))
        v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        v.SetNet(net)
        board.Add(v)
        return v

    def _add_pth_pad(self, board, net_name, x, y):
        import pcbnew
        fp = pcbnew.FOOTPRINT(board)
        fp.SetReference("U1")
        fp.SetPosition(pcbnew.VECTOR2I(
            int(x * self.SCALE), int(y * self.SCALE),
        ))
        pad = pcbnew.PAD(fp)
        pad.SetPosition(pcbnew.VECTOR2I(
            int(x * self.SCALE), int(y * self.SCALE),
        ))
        pad.SetNumber("1")
        pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
        pad.SetDrillSize(pcbnew.VECTOR2I(
            int(0.3 * self.SCALE), int(0.3 * self.SCALE),
        ))
        pad.SetSize(pcbnew.VECTOR2I(
            int(0.6 * self.SCALE), int(0.6 * self.SCALE),
        ))
        lset = pcbnew.LSET()
        lset.AddLayer(pcbnew.F_Cu)
        lset.AddLayer(pcbnew.B_Cu)
        pad.SetLayerSet(lset)
        pad.SetNet(board.GetNetInfo().NetsByName()[net_name])
        fp.Add(pad)
        board.Add(fp)
        return pad

    def test_clean_board_no_conflicts(self):
        board = self._make_board()
        self._add_via(board, "GND", 10, 10)
        self._add_via(board, "GND", 20, 20)
        r = RoutingCommands(board=board)
        result = r.find_redundant_vias({})
        assert result["success"]
        assert result["conflictCount"] == 0
        assert result["conflicts"] == []

    def test_stacked_same_net_vias_flagged(self):
        board = self._make_board()
        self._add_via(board, "GND", 10, 10)
        self._add_via(board, "GND", 10.4, 10)  # 0.1mm drill gap
        r = RoutingCommands(board=board)
        result = r.find_redundant_vias({})
        assert result["conflictCount"] == 1
        c = result["conflicts"][0]
        assert c["reason"].startswith("same-net stacked")
        assert c["redundantType"] == "via"
        assert c["keepType"] == "via"
        assert c["gapMm"] < 0.25

    def test_dry_run_preserves_vias(self):
        import pcbnew
        board = self._make_board()
        self._add_via(board, "GND", 10, 10)
        self._add_via(board, "GND", 10.4, 10)
        before = sum(
            1 for t in board.Tracks() if t.Type() == pcbnew.PCB_VIA_T
        )
        r = RoutingCommands(board=board)
        r.find_redundant_vias({})
        after = sum(
            1 for t in board.Tracks() if t.Type() == pcbnew.PCB_VIA_T
        )
        assert before == after == 2

    def test_delete_true_removes_redundant(self):
        import pcbnew
        board = self._make_board()
        self._add_via(board, "GND", 10, 10)
        self._add_via(board, "GND", 10.4, 10)
        r = RoutingCommands(board=board)
        result = r.find_redundant_vias({"delete": True})
        assert result["success"]
        assert result["removedCount"] == 1
        remaining = sum(
            1 for t in board.Tracks() if t.Type() == pcbnew.PCB_VIA_T
        )
        assert remaining == 1

    def test_via_vs_pth_pad_pad_always_kept(self):
        board = self._make_board()
        self._add_via(board, "GND", 10, 10)
        self._add_pth_pad(board, "GND", 10.4, 10)
        r = RoutingCommands(board=board)
        result = r.find_redundant_vias({})
        assert result["conflictCount"] == 1
        c = result["conflicts"][0]
        assert c["redundantType"] == "via"
        assert c["keepType"] == "pth_pad"

    def test_net_filter_narrows_scan(self):
        board = self._make_board()
        self._add_via(board, "GND", 10, 10)
        self._add_via(board, "GND", 10.4, 10)
        self._add_via(board, "FOO", 20, 20)
        self._add_via(board, "FOO", 20.4, 20)
        r = RoutingCommands(board=board)
        result = r.find_redundant_vias({"net": "GND"})
        assert result["conflictCount"] == 1
        assert result["conflicts"][0]["redundantNet"] == "GND"

    def test_hole_to_hole_min_override(self):
        board = self._make_board()
        self._add_via(board, "GND", 10, 10)
        self._add_via(board, "GND", 10.5, 10)  # 0.2mm drill gap
        r = RoutingCommands(board=board)
        # Default 0.25mm: conflict.
        assert r.find_redundant_vias({})["conflictCount"] == 1
        # Tighter override accepts the spacing.
        assert (
            r.find_redundant_vias({"holeToHoleMin": 0.1})["conflictCount"]
            == 0
        )

    def test_deterministic_redundant_choice(self):
        """Smaller UUID wins; redundant is the larger one."""
        board = self._make_board()
        v1 = self._add_via(board, "GND", 10, 10)
        v2 = self._add_via(board, "GND", 10.4, 10)
        uuids = sorted([v1.m_Uuid.AsString(), v2.m_Uuid.AsString()])
        r = RoutingCommands(board=board)
        c = r.find_redundant_vias({})["conflicts"][0]
        assert c["redundantUuid"] == uuids[1]
        assert c["keepUuid"] == uuids[0]
