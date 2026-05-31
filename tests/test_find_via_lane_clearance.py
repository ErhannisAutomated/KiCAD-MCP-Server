"""Regression tests for find_via_lane's via-layer clearance check.

Bug observed live on power_module 2026-05-30: find_via_lane's Strategy B
(straight via-jumper on viaLayer) issued an `_find_route_obstacles` call
without trace-width or clearance args, falling through to the legacy
centerline-only crossing test. A foreign-net track that ran *parallel*
to the proposed via-layer segment (but didn't cross its centerline) was
not flagged. The tool returned strategy="via_jumper" with `success=true`
and silently shorted USB_VBUS to BAT-.

These tests assert that find_via_lane's via-layer obstacle detection now
catches parallel foreign-net tracks via the swept-stadium check.
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
class TestFindViaLaneViaLayerClearance:
    SCALE = 1_000_000  # nm per mm

    def _board_with_nets(self, names):
        import pcbnew
        board = pcbnew.BOARD()
        for n in names:
            board.Add(pcbnew.NETINFO_ITEM(board, n))
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

    def test_strategy_b_rejects_parallel_via_layer_track(self):
        """A foreign-net B.Cu track parallel to the straight via-jumper
        path (close enough that a 0.2 mm trace + 0.15 mm clearance
        would overlap it, but not crossing the centerline) must NOT be
        accepted as strategy="via_jumper".

        Old buggy behaviour: returned success with strategy="via_jumper".
        Fixed behaviour: Strategy B fails the obstacle check; the tool
        either falls through to a detour strategy or returns a failure.
        Either is acceptable — what's NOT acceptable is silent acceptance
        of a clearly-overlapping route.
        """
        import pcbnew

        board = self._board_with_nets(["TRACE", "FOREIGN_F", "FOREIGN_B"])
        f_cu = board.GetLayerID("F.Cu")
        b_cu = board.GetLayerID("B.Cu")

        # F.Cu blocker: a short vertical foreign-net track at X=10
        # forces find_via_lane to skip Strategy A (direct on F.Cu).
        self._add_track(board, (10, 4), (10, 6), 0.5, f_cu, "FOREIGN_F")

        # B.Cu parallel-track shortener: spans the entire X range where
        # a straight via-jumper on B.Cu would run, offset by 0.15 mm in
        # Y. With a 0.2 mm proposed trace (edges ±0.1 mm) and 0.15 mm
        # clearance, the swept stadium overlaps Y ∈ [0.05, 0.25] vs
        # the foreign track at Y ∈ [0.05, 0.25] (0.2 mm wide centered
        # at Y=0.15 mm above the centerline). Old centerline-only
        # check missed this; fixed code catches it.
        self._add_track(board, (4, 5.15), (16, 5.15), 0.2, b_cu, "FOREIGN_B")

        r = RoutingCommands(board=board)
        result = r.find_via_lane({
            "from": {"x": 5.0, "y": 5.0, "unit": "mm"},
            "to":   {"x": 15.0, "y": 5.0, "unit": "mm"},
            "net": "TRACE",
            "fromLayer": "F.Cu",
            "viaLayer": "B.Cu",
            "width": 0.2,
            "viaDiameter": 0.6,
            "viaDrill": 0.3,
            "safetyMargin": 0.5,
            "minClearance": 0.15,
            "waypointSearchMax": 4.0,
            "apply": False,
        })

        # The straight via-jumper (Strategy B, label="via_jumper") would
        # short the proposed B.Cu segment to FOREIGN_B. The fix must
        # reject it. Detour strategies (C/D/E/F/G — these *do* avoid
        # FOREIGN_B by going south past Y=5) are fine; what we forbid
        # is the bare "via_jumper" strategy that runs straight through.
        strategy = result.get("strategy")
        assert strategy != "via_jumper", (
            f"find_via_lane returned the straight via_jumper despite a "
            f"parallel foreign-net track 0.15 mm from the route "
            f"centerline — this is the silent-short bug. Result: {result}"
        )


@pytest.mark.integration
@pytest.mark.skipif(
    not _real_pcbnew_available(),
    reason="needs real pcbnew swig module",
)
class TestFindViaLaneMaxPathLength:
    """maxPathLength budget rejects pathological multi-cm detours."""

    SCALE = 1_000_000

    def _board_with_nets(self, names):
        import pcbnew
        board = pcbnew.BOARD()
        for n in names:
            board.Add(pcbnew.NETINFO_ITEM(board, n))
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

    def test_clear_route_below_budget_succeeds(self):
        """Sanity: an unobstructed direct route under the budget still
        succeeds — the budget gate must not kill the easy cases."""
        import pcbnew

        board = self._board_with_nets(["TRACE"])
        r = RoutingCommands(board=board)
        result = r.find_via_lane({
            "from": {"x": 5.0, "y": 5.0, "unit": "mm"},
            "to":   {"x": 15.0, "y": 5.0, "unit": "mm"},
            "net": "TRACE",
            "width": 0.2,
            "maxPathLength": 30.0,  # 10 mm direct fits easily
            "apply": False,
        })
        assert result.get("success") is True
        assert result.get("strategy") == "direct"

    def test_direct_route_over_budget_is_rejected(self):
        """When maxPathLength is shorter than the manhattan distance,
        even Strategy A (direct) should be skipped — the result must
        not claim success with strategy='direct'."""
        import pcbnew

        board = self._board_with_nets(["TRACE"])
        r = RoutingCommands(board=board)
        result = r.find_via_lane({
            "from": {"x": 5.0, "y": 5.0, "unit": "mm"},
            "to":   {"x": 15.0, "y": 5.0, "unit": "mm"},  # 10 mm direct
            "net": "TRACE",
            "width": 0.2,
            "maxPathLength": 5.0,  # tighter than the direct distance
            "apply": False,
        })
        # Either a non-direct strategy succeeded (none can — there are
        # no obstacles to force a detour, so the budget rules out
        # everything), or the result is a failure that surfaces the
        # over-budget rejection. What we forbid: success with
        # strategy='direct' when the direct length > budget.
        if result.get("success"):
            assert result.get("strategy") != "direct"
        else:
            assert result.get("overBudgetAttempts"), (
                f"Failure should record over-budget attempts: {result}"
            )

    def test_budget_check_records_attempt_length(self):
        """overBudgetAttempts diagnostic should record what was tried
        and at what length, so the user can decide whether to raise
        the budget."""
        import pcbnew

        board = self._board_with_nets(["TRACE"])
        r = RoutingCommands(board=board)
        result = r.find_via_lane({
            "from": {"x": 5.0, "y": 5.0, "unit": "mm"},
            "to":   {"x": 15.0, "y": 5.0, "unit": "mm"},
            "net": "TRACE",
            "width": 0.2,
            "maxPathLength": 2.0,  # very tight, kills direct
            "apply": False,
        })
        assert not result.get("success")
        rejects = result.get("overBudgetAttempts") or []
        assert rejects, f"Expected over-budget records: {result}"
        # Direct = 10 mm; recorded length should reflect that.
        direct = next((r for r in rejects if r["strategy"] == "direct"),
                      None)
        assert direct is not None, rejects
        assert abs(direct["lengthMm"] - 10.0) < 0.01, direct
