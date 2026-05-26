"""Tests for the width- and clearance-aware obstacle detection added
in #177.

Two flavours:

* Unit tests for `_resolve_route_clearance` use the conftest pcbnew
  stub.
* Integration tests for `_iter_route_obstacles` need real pcbnew
  (PCB_TRACK / PAD shapes / etc.) and are skipped when only the stub
  is available.

The motivating bug: a 1.5 mm V12_OUT trace exited U4.13 with
`checkObstacles=True` and the check passed even though the trace edge
overlapped neighbouring pad U4.14 — the centerline alone didn't cross
U4.14, so the legacy detector missed it. The new tests assert the
detector now catches the same edge-clipping case.
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
# Unit tests for the clearance resolver (mock pcbnew is fine).
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestResolveRouteClearance:
    def _routing_with(
        self,
        *,
        current_track_width_iu: int = 200_000,  # 0.2 mm
        netclass_clearance_iu: int = 150_000,  # 0.15 mm
        net_present: bool = True,
    ) -> RoutingCommands:
        board = MagicMock(name="BOARD")
        bds = MagicMock(name="BOARD_DESIGN_SETTINGS")
        bds.GetCurrentTrackWidth.return_value = current_track_width_iu
        default_nc = MagicMock(name="DEFAULT_NC")
        default_nc.GetClearance.return_value = netclass_clearance_iu
        bds.GetDefault.return_value = default_nc
        board.GetDesignSettings.return_value = bds

        netinfo = MagicMock(name="NETINFO")
        nets_map = MagicMock(name="nets_map")
        nets_map.has_key.return_value = net_present
        net_obj = MagicMock(name="NET")
        net_class = MagicMock(name="NETCLASS")
        net_class.GetClearance.return_value = netclass_clearance_iu
        net_obj.GetNetClass.return_value = net_class
        nets_map.__getitem__.return_value = net_obj
        netinfo.NetsByName.return_value = nets_map
        board.GetNetInfo.return_value = netinfo

        r = RoutingCommands(board=board)
        return r

    def test_explicit_width_and_clearance_override(self):
        r = self._routing_with()
        w, c = r._resolve_route_clearance(0.5, 0.3, "FOO")
        assert w == 500_000
        assert c == 300_000

    def test_defaults_from_board_and_netclass(self):
        r = self._routing_with(
            current_track_width_iu=400_000,
            netclass_clearance_iu=200_000,
        )
        w, c = r._resolve_route_clearance(None, None, "FOO")
        assert w == 400_000
        assert c == 200_000

    def test_missing_net_falls_back_to_default_clearance(self):
        r = self._routing_with(
            current_track_width_iu=200_000,
            netclass_clearance_iu=100_000,
            net_present=False,
        )
        w, c = r._resolve_route_clearance(None, None, "DOES_NOT_EXIST")
        assert w == 200_000
        # The net wasn't found, so look-up fell through to board-default
        # netclass clearance.
        assert c == 100_000

    def test_explicit_zero_clearance_is_honoured(self):
        r = self._routing_with(netclass_clearance_iu=300_000)
        w, c = r._resolve_route_clearance(0.2, 0.0, "FOO")
        assert c == 0  # not the netclass fallback


# ---------------------------------------------------------------------------
# Integration tests — need real pcbnew so PCB_TRACK / PAD shapes / etc.
# behave realistically.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(
    not _real_pcbnew_available(),
    reason="needs real pcbnew swig module",
)
class TestEdgeClipDetection:
    """Edge-clipping cases the centerline-only detector used to miss."""

    SCALE = 1_000_000  # nm per mm

    def _board_with_foreign_net(self):
        import pcbnew

        board = pcbnew.BOARD()
        # Define two nets so same-net copper isn't an obstacle for the
        # candidate trace we'll route on net "TRACE".
        for name in ("TRACE", "FOREIGN"):
            net = pcbnew.NETINFO_ITEM(board, name)
            board.Add(net)
        return board

    def _add_pad_at(self, board, x_mm: float, y_mm: float,
                    w_mm: float = 1.0, h_mm: float = 0.5,
                    net_name: str = "FOREIGN"):
        """Add a footprint with a single SMD pad centered at (x_mm, y_mm).
        Returns the footprint."""
        import pcbnew

        fp = pcbnew.FOOTPRINT(board)
        fp.SetReference("FP1")
        fp.SetPosition(pcbnew.VECTOR2I(int(x_mm * self.SCALE),
                                        int(y_mm * self.SCALE)))
        pad = pcbnew.PAD(fp)
        pad.SetNumber("1")
        pad.SetShape(pcbnew.PAD_SHAPE_RECT)
        pad.SetSize(pcbnew.VECTOR2I(int(w_mm * self.SCALE),
                                     int(h_mm * self.SCALE)))
        pad.SetPosition(pcbnew.VECTOR2I(int(x_mm * self.SCALE),
                                         int(y_mm * self.SCALE)))
        pad.SetLayerSet(pad.SMDMask())
        nets = board.GetNetInfo().NetsByName()
        if nets.has_key(net_name):
            pad.SetNet(nets[net_name])
        fp.Add(pad)
        board.Add(fp)
        return fp, pad

    def test_centerline_only_misses_edge_clip(self):
        """With trace_width=0, a centerline route that grazes a pad
        edge but doesn't cross its center should NOT be flagged.
        Regression guard for the legacy behaviour."""
        import pcbnew

        board = self._board_with_foreign_net()
        # Pad center at (10, 0.4) → 1.0×0.5 → top edge at Y=0.15.
        self._add_pad_at(board, 10.0, 0.4)

        r = RoutingCommands(board=board)
        layer_id = board.GetLayerID("F.Cu")
        start = pcbnew.VECTOR2I(0, 0)
        end = pcbnew.VECTOR2I(int(20.0 * self.SCALE), 0)
        # trace_width_iu=0 → legacy centerline check
        obs = r._find_route_obstacles(start, end, layer_id, "TRACE",
                                       trace_width_iu=0,
                                       min_clearance_iu=0)
        assert obs == [], f"Centerline should not hit pad: {obs}"

    def test_wide_trace_clips_pad_edge(self):
        """Same geometry — a 1.0 mm-wide trace's edge extends 0.5 mm
        either side, which clips the pad at Y=0.15. The detector
        should flag the pad."""
        import pcbnew

        board = self._board_with_foreign_net()
        self._add_pad_at(board, 10.0, 0.4)

        r = RoutingCommands(board=board)
        layer_id = board.GetLayerID("F.Cu")
        start = pcbnew.VECTOR2I(0, 0)
        end = pcbnew.VECTOR2I(int(20.0 * self.SCALE), 0)
        obs = r._find_route_obstacles(
            start, end, layer_id, "TRACE",
            trace_width_iu=1_000_000,   # 1.0 mm
            min_clearance_iu=0,
        )
        assert obs, "Edge-clipping pad should be detected"
        assert any("FP1" in s for s in obs), obs

    def test_clearance_inflates_detection_zone(self):
        """A 0.2 mm centerline trace that wouldn't touch the pad even
        with its width can still be flagged when min_clearance is wide
        enough to bring the swept region into the pad."""
        import pcbnew

        board = self._board_with_foreign_net()
        # Same pad as above; trace edge would still be 0.05 mm clear
        # at trace_width=0.2 mm.
        self._add_pad_at(board, 10.0, 0.4)

        r = RoutingCommands(board=board)
        layer_id = board.GetLayerID("F.Cu")
        start = pcbnew.VECTOR2I(0, 0)
        end = pcbnew.VECTOR2I(int(20.0 * self.SCALE), 0)
        obs = r._find_route_obstacles(
            start, end, layer_id, "TRACE",
            trace_width_iu=200_000,     # 0.2 mm (edge ±0.1 mm)
            min_clearance_iu=200_000,   # +0.2 mm clearance
        )
        assert obs, "Clearance margin should flag pad"


@pytest.mark.integration
@pytest.mark.skipif(
    not _real_pcbnew_available(),
    reason="needs real pcbnew swig module",
)
class TestTrackVsTrackInflation:
    """Foreign-net track that runs parallel close to (but not crossing)
    the candidate path. Width-aware detection should flag it."""

    SCALE = 1_000_000

    def _board(self):
        import pcbnew

        board = pcbnew.BOARD()
        for name in ("TRACE", "FOREIGN"):
            board.Add(pcbnew.NETINFO_ITEM(board, name))
        return board

    def _add_track(self, board, p1_mm, p2_mm, width_mm, layer_id, net_name):
        import pcbnew

        t = pcbnew.PCB_TRACK(board)
        t.SetStart(pcbnew.VECTOR2I(int(p1_mm[0] * self.SCALE),
                                    int(p1_mm[1] * self.SCALE)))
        t.SetEnd(pcbnew.VECTOR2I(int(p2_mm[0] * self.SCALE),
                                  int(p2_mm[1] * self.SCALE)))
        t.SetLayer(layer_id)
        t.SetWidth(int(width_mm * self.SCALE))
        nets = board.GetNetInfo().NetsByName()
        if nets.has_key(net_name):
            t.SetNet(nets[net_name])
        board.Add(t)
        return t

    def test_parallel_track_close_but_not_crossing(self):
        """Foreign-net track 0.2 mm away; centerline check passes,
        width-aware check flags it."""
        import pcbnew

        board = self._board()
        layer_id = board.GetLayerID("F.Cu")
        # Foreign track from (5, 0.2) to (15, 0.2), 0.2 mm wide.
        self._add_track(board, (5, 0.2), (15, 0.2), 0.2, layer_id, "FOREIGN")

        r = RoutingCommands(board=board)
        # Candidate route along Y=0, X from 0 to 20.
        start = pcbnew.VECTOR2I(0, 0)
        end = pcbnew.VECTOR2I(int(20.0 * self.SCALE), 0)

        legacy = r._find_route_obstacles(start, end, layer_id, "TRACE",
                                          0, 0)
        assert legacy == [], "Parallel track should not cross centerline"

        # 0.3 mm trace + 0.15 mm clearance → swept Y range
        # [-0.3, +0.3] vs other track at Y∈[0.1, 0.3]; intersects.
        wide = r._find_route_obstacles(start, end, layer_id, "TRACE",
                                        300_000, 150_000)
        assert wide, "Width-aware check should flag the near-parallel track"
