"""Tests for bridge_same_net_pins (#205).

Creates a small filled zone covering two same-net pads, replacing the
thin trace that would otherwise violate the POWER netclass min-track-
width rule.
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
class TestBridgeValidation:
    def _routing(self) -> RoutingCommands:
        return RoutingCommands(board=MagicMock(name="BOARD"))

    def test_missing_pads_errors(self):
        r = self._routing()
        result = r.bridge_same_net_pins({})
        assert result["success"] is False
        assert "padA and padB" in result["errorDetails"]

    def test_missing_pad_field_errors(self):
        r = self._routing()
        result = r.bridge_same_net_pins({
            "padA": {"ref": "U4"},  # no pad
            "padB": {"ref": "U4", "pad": "3"},
        })
        assert result["success"] is False
        assert "ref and pad" in result["errorDetails"]


@pytest.mark.unit
class TestBridgeInAutoSaveSet:
    def test_bridge_is_a_mutator(self):
        from kicad_interface import KiCADInterface
        assert "bridge_same_net_pins" in KiCADInterface._BOARD_MUTATING_COMMANDS


@pytest.mark.integration
@pytest.mark.skipif(
    not _real_pcbnew_available(),
    reason="needs real pcbnew swig module",
)
class TestBridgeOnRealBoard:
    SCALE = 1_000_000

    def _build_board_with_two_same_net_pads(self):
        """A footprint with two SMD pads, both on net BAT+, 1mm apart."""
        import pcbnew

        board = pcbnew.BOARD()
        net = pcbnew.NETINFO_ITEM(board, "BAT+")
        board.Add(net)
        nets = board.GetNetInfo().NetsByName()

        fp = pcbnew.FOOTPRINT(board)
        fp.SetReference("U4")
        fp.SetPosition(pcbnew.VECTOR2I(int(10 * self.SCALE),
                                        int(10 * self.SCALE)))
        for num, x_off in (("2", -0.5), ("3", 0.5)):
            pad = pcbnew.PAD(fp)
            pad.SetNumber(num)
            pad.SetShape(pcbnew.PAD_SHAPE_RECT)
            pad.SetSize(pcbnew.VECTOR2I(int(0.45 * self.SCALE),
                                         int(0.95 * self.SCALE)))
            pad.SetPosition(pcbnew.VECTOR2I(
                int((10 + x_off) * self.SCALE),
                int(10 * self.SCALE),
            ))
            pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            pad.SetLayerSet(pad.SMDMask())
            pad.SetNet(nets["BAT+"])
            fp.Add(pad)
        board.Add(fp)
        return board

    def test_preview_outlines_pads(self):
        board = self._build_board_with_two_same_net_pads()
        r = RoutingCommands(board=board)
        result = r.bridge_same_net_pins({
            "padA": {"ref": "U4", "pad": "2"},
            "padB": {"ref": "U4", "pad": "3"},
            "marginMm": 0.1,
            "apply": False,
        })
        assert result["success"], result
        assert result["net"] == "BAT+"
        assert result["applied"] is False
        # Outline should span both pads + 0.1mm margin → ~1.45 mm wide
        outline = result["outline"]
        assert len(outline) == 4
        xs = [p["x"] for p in outline]
        ys = [p["y"] for p in outline]
        assert max(xs) - min(xs) == pytest.approx(1.4, abs=0.01)
        # Pad height 0.95mm + 0.2 mm margin (both sides) = 1.15 mm
        assert max(ys) - min(ys) == pytest.approx(1.15, abs=0.01)

    def test_apply_adds_zone_to_board(self):
        board = self._build_board_with_two_same_net_pads()
        r = RoutingCommands(board=board)
        before = sum(1 for _ in board.Zones())
        result = r.bridge_same_net_pins({
            "padA": {"ref": "U4", "pad": "2"},
            "padB": {"ref": "U4", "pad": "3"},
            "apply": True,
        })
        assert result["success"], result
        after = sum(1 for _ in board.Zones())
        assert after == before + 1
        # The new zone should be on net BAT+
        for z in board.Zones():
            if z.GetNetname() == "BAT+":
                break
        else:
            pytest.fail("No BAT+ zone added")

    def test_different_nets_rejected(self):
        import pcbnew

        board = pcbnew.BOARD()
        for name in ("BAT+", "GND"):
            board.Add(pcbnew.NETINFO_ITEM(board, name))
        nets = board.GetNetInfo().NetsByName()

        fp = pcbnew.FOOTPRINT(board)
        fp.SetReference("U4")
        fp.SetPosition(pcbnew.VECTOR2I(0, 0))
        for num, net_name in (("2", "BAT+"), ("3", "GND")):
            pad = pcbnew.PAD(fp)
            pad.SetNumber(num)
            pad.SetShape(pcbnew.PAD_SHAPE_RECT)
            pad.SetSize(pcbnew.VECTOR2I(int(0.45 * self.SCALE),
                                         int(0.95 * self.SCALE)))
            pad.SetPosition(pcbnew.VECTOR2I(0, 0))
            pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            pad.SetLayerSet(pad.SMDMask())
            pad.SetNet(nets[net_name])
            fp.Add(pad)
        board.Add(fp)

        r = RoutingCommands(board=board)
        result = r.bridge_same_net_pins({
            "padA": {"ref": "U4", "pad": "2"},
            "padB": {"ref": "U4", "pad": "3"},
        })
        assert result["success"] is False
        assert "different nets" in result["message"].lower()

    def test_missing_pad_returns_clean_error(self):
        board = self._build_board_with_two_same_net_pads()
        r = RoutingCommands(board=board)
        result = r.bridge_same_net_pins({
            "padA": {"ref": "U4", "pad": "2"},
            "padB": {"ref": "U4", "pad": "99"},
        })
        assert result["success"] is False
        assert "not found" in result["message"].lower()
