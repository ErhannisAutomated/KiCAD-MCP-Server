"""Tests for delete_trace scoped position-search + deleted list (#223)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from commands.routing import RoutingCommands  # noqa: E402


def _real_pcbnew_available() -> bool:
    import pcbnew  # type: ignore
    return getattr(pcbnew, "GetBuildVersion", lambda: "")() != "9.0.0-stub"


@pytest.mark.integration
@pytest.mark.skipif(
    not _real_pcbnew_available(),
    reason="needs real pcbnew swig module",
)
class TestDeleteTraceScoped:
    SCALE = 1_000_000

    def _make_board(self):
        import pcbnew
        board = pcbnew.BOARD()
        for name in ("GND", "FOO"):
            board.Add(pcbnew.NETINFO_ITEM(board, name))
        return board

    def _add_track(self, board, net_name, x1, y1, x2, y2, layer=None, w=0.25):
        import pcbnew
        if layer is None:
            layer = pcbnew.F_Cu
        net = board.GetNetInfo().NetsByName()[net_name]
        seg = pcbnew.PCB_TRACK(board)
        seg.SetStart(pcbnew.VECTOR2I(
            int(x1 * self.SCALE), int(y1 * self.SCALE),
        ))
        seg.SetEnd(pcbnew.VECTOR2I(
            int(x2 * self.SCALE), int(y2 * self.SCALE),
        ))
        seg.SetWidth(int(w * self.SCALE))
        seg.SetLayer(layer)
        seg.SetNet(net)
        board.Add(seg)
        return seg

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

    def test_position_delete_returns_deleted_item(self):
        board = self._make_board()
        self._add_track(board, "GND", 10, 10, 20, 10)
        r = RoutingCommands(board=board)
        result = r.delete_trace({
            "position": {"x": 15.0, "y": 10.0, "unit": "mm"},
        })
        assert result["success"], result
        assert len(result["deleted"]) == 1
        d = result["deleted"][0]
        assert d["type"] == "track"
        assert d["net"] == "GND"
        assert d["layer"] == "F.Cu"

    def test_position_net_filter_disambiguates(self):
        """Two tracks of different nets meet at (15, 10). With net='FOO',
        the GND track must remain."""
        board = self._make_board()
        self._add_track(board, "GND", 10, 10, 20, 10)
        self._add_track(board, "FOO", 15, 5, 15, 15)
        r = RoutingCommands(board=board)
        result = r.delete_trace({
            "position": {"x": 15.0, "y": 10.0, "unit": "mm"},
            "net": "FOO",
        })
        assert result["success"], result
        assert result["deleted"][0]["net"] == "FOO"
        remaining = [t for t in board.Tracks()]
        assert len(remaining) == 1
        assert remaining[0].GetNetname() == "GND"

    def test_position_kind_via_picks_only_via(self):
        board = self._make_board()
        self._add_track(board, "GND", 10, 10, 20, 10)
        self._add_via(board, "GND", 15, 10)
        r = RoutingCommands(board=board)
        result = r.delete_trace({
            "position": {"x": 15.0, "y": 10.0, "unit": "mm"},
            "kind": "via",
        })
        assert result["success"], result
        assert result["deleted"][0]["type"] == "via"

    def test_position_layer_filter(self):
        import pcbnew
        board = self._make_board()
        self._add_track(board, "GND", 10, 10, 20, 10, layer=pcbnew.F_Cu)
        self._add_track(board, "GND", 10, 10, 20, 10, layer=pcbnew.B_Cu)
        r = RoutingCommands(board=board)
        result = r.delete_trace({
            "position": {"x": 15.0, "y": 10.0, "unit": "mm"},
            "layer": "B.Cu",
        })
        assert result["success"], result
        assert result["deleted"][0]["layer"] == "B.Cu"

    def test_bulk_by_net_reports_each_deleted(self):
        board = self._make_board()
        self._add_track(board, "GND", 10, 10, 20, 10)
        self._add_track(board, "GND", 20, 10, 30, 10)
        self._add_track(board, "FOO", 0, 0, 5, 5)
        r = RoutingCommands(board=board)
        result = r.delete_trace({"net": "GND"})
        assert result["success"], result
        assert result["deletedCount"] == 2
        assert len(result["deleted"]) == 2
        assert all(d["net"] == "GND" for d in result["deleted"])

    def test_uuid_delete_returns_deleted_item(self):
        board = self._make_board()
        t = self._add_track(board, "FOO", 10, 10, 20, 10)
        uuid = t.m_Uuid.AsString()
        r = RoutingCommands(board=board)
        result = r.delete_trace({"traceUuid": uuid})
        assert result["success"], result
        assert len(result["deleted"]) == 1
        assert result["deleted"][0]["uuid"] == uuid

    def test_invalid_kind_rejected(self):
        board = self._make_board()
        r = RoutingCommands(board=board)
        result = r.delete_trace({
            "position": {"x": 0, "y": 0, "unit": "mm"},
            "kind": "bogus",
        })
        assert not result["success"]
        assert "kind must be" in result["errorDetails"]

    def test_scoped_position_failure_describes_filters(self):
        board = self._make_board()
        self._add_track(board, "FOO", 10, 10, 20, 10)
        r = RoutingCommands(board=board)
        result = r.delete_trace({
            "position": {"x": 100.0, "y": 100.0, "unit": "mm"},
            "net": "GND",
            "kind": "via",
        })
        assert not result["success"]
        err = result["errorDetails"]
        assert "net='GND'" in err
        assert "kind='via'" in err
