"""Tests for via_orphan_pads (#213).

* Unit tests with mocked pcbnew cover parameter validation.
* Integration tests with real pcbnew build a small board with
  GND pads on F.Cu and verify the via-near-pad placement.
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
class TestViaOrphanPadsValidation:
    def _routing(self) -> RoutingCommands:
        return RoutingCommands(board=MagicMock(name="BOARD"))

    def test_missing_net_errors(self):
        r = self._routing()
        result = r.via_orphan_pads({})
        assert result["success"] is False
        assert "net is required" in result["message"]

    def test_invalid_layer_errors(self):
        r = self._routing()
        result = r.via_orphan_pads({"net": "GND", "layer": "In1.Cu"})
        assert result["success"] is False
        assert "layer must be" in result["message"]


@pytest.mark.unit
class TestViaOrphanPadsInAutoSaveSet:
    """Adding vias mutates the board, so via_orphan_pads must be in the
    auto-save set or external readers (kicad-cli, fresh pcbnew.LoadBoard)
    see the previous state."""

    def test_via_orphan_pads_is_a_mutator(self):
        from kicad_interface import KiCADInterface
        assert "via_orphan_pads" in KiCADInterface._BOARD_MUTATING_COMMANDS


# ---------------------------------------------------------------------------
# Integration tests — need real pcbnew.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(
    not _real_pcbnew_available(),
    reason="needs real pcbnew swig module",
)
class TestViaOrphanPadsOnRealBoard:
    SCALE = 1_000_000

    def _build_board(self, n_pads: int = 2):
        """Tiny board with `n_pads` GND F.Cu SMD pads on a single
        footprint. No tracks, no vias — every pad is an "orphan"."""
        import pcbnew

        board = pcbnew.BOARD()
        net = pcbnew.NETINFO_ITEM(board, "GND")
        board.Add(net)
        gnd_net = board.GetNetInfo().NetsByName()["GND"]

        fp = pcbnew.FOOTPRINT(board)
        fp.SetReference("U1")
        fp.SetPosition(pcbnew.VECTOR2I(int(20 * self.SCALE), int(20 * self.SCALE)))
        for i in range(n_pads):
            pad = pcbnew.PAD(fp)
            pad.SetNet(gnd_net)
            pad.SetNumber(str(i + 1))
            # Place pads on a horizontal line, 2 mm spaced.
            pad.SetPosition(
                pcbnew.VECTOR2I(
                    int((18 + 2 * i) * self.SCALE),
                    int(20 * self.SCALE),
                )
            )
            pad.SetSize(pcbnew.VECTOR2I(
                int(1.0 * self.SCALE),
                int(0.5 * self.SCALE),
            ))
            # SMD on F.Cu only.
            pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            layer_set = pcbnew.LSET()
            layer_set.AddLayer(pcbnew.F_Cu)
            pad.SetLayerSet(layer_set)
            fp.Add(pad)
        board.Add(fp)
        return board

    def test_proposes_one_via_per_orphan_pad(self):
        board = self._build_board(n_pads=2)
        r = RoutingCommands(board=board)
        result = r.via_orphan_pads({
            "net": "GND",
            "apply": False,
        })
        assert result["success"], result
        assert result["candidateCount"] == 2
        assert result["proposedCount"] == 2
        assert result["skippedAlreadyConnected"] == 0
        for entry in result["positions"]:
            # Each proposed via has a stubStart at a pad center.
            assert "stubStart" in entry
            assert "stubEnd" in entry
            assert entry["padRef"] == "U1"

    def test_apply_adds_via_and_stub(self):
        import pcbnew
        board = self._build_board(n_pads=1)
        r = RoutingCommands(board=board)
        before_vias = sum(
            1 for t in board.Tracks() if t.Type() == pcbnew.PCB_VIA_T
        )
        before_tracks = sum(
            1 for t in board.Tracks() if t.Type() != pcbnew.PCB_VIA_T
        )
        result = r.via_orphan_pads({"net": "GND", "apply": True})
        assert result["success"], result
        after_vias = sum(
            1 for t in board.Tracks() if t.Type() == pcbnew.PCB_VIA_T
        )
        after_tracks = sum(
            1 for t in board.Tracks() if t.Type() != pcbnew.PCB_VIA_T
        )
        # One pad → one via + one stub trace.
        assert after_vias - before_vias == 1
        assert after_tracks - before_tracks == 1

    def test_skips_pads_with_existing_same_net_via(self):
        """A pad that already has a same-net via within pickup radius
        should be reported as skippedAlreadyConnected — not re-via'd."""
        import pcbnew
        board = self._build_board(n_pads=1)
        gnd_net = board.GetNetInfo().NetsByName()["GND"]

        # Pre-place a via right next to the pad (at pad center + 1 mm).
        via = pcbnew.PCB_VIA(board)
        via.SetPosition(
            pcbnew.VECTOR2I(int(19 * self.SCALE), int(20 * self.SCALE))
        )
        via.SetWidth(int(0.6 * self.SCALE))
        via.SetDrill(int(0.3 * self.SCALE))
        via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        via.SetNet(gnd_net)
        board.Add(via)

        r = RoutingCommands(board=board)
        result = r.via_orphan_pads({"net": "GND", "apply": False})
        assert result["success"], result
        assert result["skippedAlreadyConnected"] == 1
        assert result["proposedCount"] == 0
