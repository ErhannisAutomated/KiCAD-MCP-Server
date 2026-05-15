"""Tests for RoutingCommands.audit_plane_cuts.

The audit groups signal traces on inner copper layers (typically the
GND / PWR plane layers) by net and sorts them by total length, so a
caller can target the worst plane-cuts for ripup + retry on an outer
layer.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


def _real_pcbnew_available() -> bool:
    import pcbnew  # type: ignore

    return getattr(pcbnew, "GetBuildVersion", lambda: "")() != "9.0.0-stub"


@pytest.mark.skipif(
    not _real_pcbnew_available(),
    reason="real pcbnew not available (test stub in use)",
)
class TestAuditPlaneCuts:
    """Synthetic board + a handful of segments across layers."""

    def _build_board(self):
        import pcbnew

        board = pcbnew.BOARD()
        # Lay down four segments: one each on F.Cu, B.Cu, In1.Cu, In2.Cu.
        # Only the inner two should be picked up by the default audit.
        for name, layer_id, p1, p2 in (
            ("outer-f", pcbnew.F_Cu, (10, 10), (50, 10)),
            ("outer-b", pcbnew.B_Cu, (10, 20), (50, 20)),
            ("inner-1", pcbnew.In1_Cu, (10, 30), (110, 30)),  # 100 mm
            ("inner-2", pcbnew.In2_Cu, (10, 40), (60, 40)),  # 50 mm
        ):
            t = pcbnew.PCB_TRACK(board)
            t.SetStart(pcbnew.VECTOR2I(p1[0] * 1_000_000, p1[1] * 1_000_000))
            t.SetEnd(pcbnew.VECTOR2I(p2[0] * 1_000_000, p2[1] * 1_000_000))
            t.SetLayer(layer_id)
            t.SetWidth(200_000)
            board.Add(t)
        return board

    def test_only_inner_layers_audited(self) -> None:
        import pcbnew  # noqa: F401
        from commands.routing import RoutingCommands

        rc = RoutingCommands(self._build_board())
        r = rc.audit_plane_cuts({})
        assert r["success"], r
        assert r["totalCutSegments"] == 2  # only inner-1 and inner-2
        # 100 + 50 mm total
        assert abs(r["totalCutLength"] - 150.0) < 0.01

    def test_longest_first(self) -> None:
        from commands.routing import RoutingCommands

        rc = RoutingCommands(self._build_board())
        r = rc.audit_plane_cuts({})
        assert r["success"], r
        traces = r["longestTraces"]
        assert traces[0]["layer"] == "In1.Cu"
        assert traces[0]["length"] > traces[1]["length"]

    def test_min_length_filters_short(self) -> None:
        from commands.routing import RoutingCommands

        rc = RoutingCommands(self._build_board())
        r = rc.audit_plane_cuts({"minLength": 75.0})
        assert r["success"], r
        # only the 100mm In1.Cu trace clears the 75mm threshold
        assert r["totalCutSegments"] == 1
        assert r["longestTraces"][0]["layer"] == "In1.Cu"

    def test_custom_layers(self) -> None:
        from commands.routing import RoutingCommands

        rc = RoutingCommands(self._build_board())
        # Audit only In2.Cu — should see just the 50 mm trace
        r = rc.audit_plane_cuts({"layers": ["In2.Cu"]})
        assert r["success"], r
        assert r["totalCutSegments"] == 1
        assert r["longestTraces"][0]["layer"] == "In2.Cu"

    def test_unknown_layer_errors(self) -> None:
        from commands.routing import RoutingCommands

        rc = RoutingCommands(self._build_board())
        r = rc.audit_plane_cuts({"layers": ["Not.A.Layer"]})
        assert r["success"] is False
        assert "Unknown layer" in r["message"]
