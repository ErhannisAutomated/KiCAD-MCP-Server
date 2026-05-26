"""Tests for pin_zone_same_net (#216)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from commands.routing import RoutingCommands  # noqa: E402


@pytest.mark.unit
class TestPinZoneSameNetValidation:
    def _routing(self) -> RoutingCommands:
        return RoutingCommands(board=MagicMock(name="BOARD"))

    def test_adjacency_factor_le_one_errors(self):
        r = self._routing()
        result = r.pin_zone_same_net({"adjacencyFactor": 1.0})
        assert result["success"] is False
        assert "adjacencyFactor" in result["message"]


@pytest.mark.unit
class TestPinZoneSameNetInAutoSaveSet:
    def test_pin_zone_same_net_is_a_mutator(self):
        from kicad_interface import KiCADInterface
        assert "pin_zone_same_net" in KiCADInterface._BOARD_MUTATING_COMMANDS
