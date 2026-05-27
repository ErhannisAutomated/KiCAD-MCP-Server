"""Tests for add_via clearance pre-check (#222).

Default behaviour: add_via refuses to commit when the proposed via would
sit within netclass clearance of foreign-net copper or within min
hole-to-hole of another drilled hole. Pass checkClearance=false to
override (matches route_trace's checkObstacles pattern).
"""

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
class TestAddViaClearance:
    SCALE = 1_000_000

    def _empty_board(self):
        """Two-net board with a default min hole-to-hole of 0.25 mm."""
        import pcbnew
        board = pcbnew.BOARD()
        for name in ("GND", "FOO"):
            net = pcbnew.NETINFO_ITEM(board, name)
            board.Add(net)
        # Force a non-zero hole-to-hole so the drill-edge check has bite.
        ds = board.GetDesignSettings()
        try:
            ds.m_HoleToHoleMin = int(0.25 * self.SCALE)
        except Exception:
            pass
        # Set a non-zero default clearance.
        try:
            bds_default = ds.GetDefault()
            if bds_default is not None:
                bds_default.SetClearance(int(0.2 * self.SCALE))
        except Exception:
            pass
        return board

    def _add_foreign_track(self, board, x_mm, y_mm, length_mm=2.0):
        """Place a thick FOO-net track at (x,y) on F.Cu so a nearby
        same-position via on a different net would short."""
        import pcbnew
        foo_net = board.GetNetInfo().NetsByName()["FOO"]
        seg = pcbnew.PCB_TRACK(board)
        seg.SetStart(pcbnew.VECTOR2I(
            int(x_mm * self.SCALE), int(y_mm * self.SCALE),
        ))
        seg.SetEnd(pcbnew.VECTOR2I(
            int((x_mm + length_mm) * self.SCALE), int(y_mm * self.SCALE),
        ))
        seg.SetWidth(int(0.5 * self.SCALE))
        seg.SetLayer(pcbnew.F_Cu)
        seg.SetNet(foo_net)
        board.Add(seg)

    def _add_foreign_via(self, board, x_mm, y_mm):
        import pcbnew
        foo_net = board.GetNetInfo().NetsByName()["FOO"]
        v = pcbnew.PCB_VIA(board)
        v.SetPosition(pcbnew.VECTOR2I(
            int(x_mm * self.SCALE), int(y_mm * self.SCALE),
        ))
        v.SetWidth(int(0.6 * self.SCALE))
        v.SetDrill(int(0.3 * self.SCALE))
        v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        v.SetNet(foo_net)
        board.Add(v)

    def _via_count(self, board):
        import pcbnew
        return sum(1 for t in board.Tracks() if t.Type() == pcbnew.PCB_VIA_T)

    def test_clean_spot_accepts(self):
        board = self._empty_board()
        r = RoutingCommands(board=board)
        result = r.add_via({
            "position": {"x": 50.0, "y": 50.0, "unit": "mm"},
            "net": "GND",
        })
        assert result["success"], result
        assert self._via_count(board) == 1

    def test_via_on_foreign_track_rejected(self):
        board = self._empty_board()
        # FOO track running through (50, 50).
        self._add_foreign_track(board, 49.0, 50.0, length_mm=3.0)
        r = RoutingCommands(board=board)
        result = r.add_via({
            "position": {"x": 50.0, "y": 50.0, "unit": "mm"},
            "net": "GND",
        })
        assert not result["success"], result
        assert "blocked" in result["message"].lower()
        assert any("FOO" in o for o in result["obstacles"])
        assert self._via_count(board) == 0  # nothing committed

    def test_checkClearance_false_overrides(self):
        board = self._empty_board()
        self._add_foreign_track(board, 49.0, 50.0, length_mm=3.0)
        r = RoutingCommands(board=board)
        result = r.add_via({
            "position": {"x": 50.0, "y": 50.0, "unit": "mm"},
            "net": "GND",
            "checkClearance": False,
        })
        assert result["success"], result
        assert self._via_count(board) == 1  # committed despite the short

    def test_via_too_close_to_other_via_hole_to_hole(self):
        board = self._empty_board()
        # Foreign via at (50,50). Proposed via at (50.4, 50). Drills 0.3mm
        # each → drill edges 0.4 - 0.15 - 0.15 = 0.1mm apart. Min h2h is
        # 0.25mm → must reject.
        self._add_foreign_via(board, 50.0, 50.0)
        r = RoutingCommands(board=board)
        result = r.add_via({
            "position": {"x": 50.4, "y": 50.0, "unit": "mm"},
            "net": "GND",
        })
        assert not result["success"], result
        assert any(
            "hole-to-hole" in o for o in result["obstacles"]
        ), result["obstacles"]
        assert self._via_count(board) == 1  # only the foreign via stayed

    def test_clearance_override_loosens_check(self):
        """A small explicit clearance lets a marginal via through that
        the default would reject."""
        board = self._empty_board()
        # FOO track 0.3mm away from proposed via edge.
        # Via radius 0.3 + foreign track half 0.25 = 0.55. Distance from
        # via center (50.0) to track centerline (50.85) = 0.85mm.
        # Copper-to-copper gap = 0.85 - 0.55 = 0.30mm. Default clearance
        # is 0.20mm so by default this PASSES. Force a 0.4mm clearance
        # and it should fail.
        self._add_foreign_track(board, 50.85, 50.0, length_mm=3.0)
        r = RoutingCommands(board=board)
        # Default clearance: should accept.
        result_ok = r.add_via({
            "position": {"x": 50.0, "y": 50.0, "unit": "mm"},
            "net": "GND",
        })
        assert result_ok["success"], result_ok
        # Now try a tighter target spot at clearance=0.4 — too tight.
        # Move the test to a fresh board so the previous via doesn't
        # interfere with the second check.
        board2 = self._empty_board()
        self._add_foreign_track(board2, 50.85, 50.0, length_mm=3.0)
        r2 = RoutingCommands(board=board2)
        result_strict = r2.add_via({
            "position": {"x": 50.0, "y": 50.0, "unit": "mm"},
            "net": "GND",
            "clearance": 0.4,
        })
        assert not result_strict["success"], result_strict
