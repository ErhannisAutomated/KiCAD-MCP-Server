"""Tests for BoardLayerCommands.add_layer.

Regression: in KiCad 9 the PCB_LAYER_ID enum spaces copper layers two slots
apart (F_Cu=0, B_Cu=2, In1_Cu=4, In2_Cu=6, …) and the odd slots hold
non-copper layers (F.Silkscreen=5, B.Silkscreen=7, …).  The old formula
`In1_Cu + (number-1)` mapped inner=2 to id 5 (F.Silkscreen), silently
corrupting the layer table and leaving orphan zones on F.Silkscreen
after save.  See `KiCAD-MCP-Server/python/commands/board/layers.py`.
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
class TestAddLayerInnerCopper:
    """Verify inner-layer number maps to the correct PCB_LAYER_ID."""

    def test_inner_1_maps_to_in1_cu(self) -> None:
        import pcbnew

        from commands.board.layers import BoardLayerCommands

        board = pcbnew.BOARD()
        cmds = BoardLayerCommands(board=board)
        res = cmds.add_layer({"name": "GND", "type": "signal", "position": "inner", "number": 1})
        assert res["success"], res
        assert board.GetLayerName(pcbnew.In1_Cu) == "GND"

    def test_inner_2_maps_to_in2_cu_not_silkscreen(self) -> None:
        import pcbnew

        from commands.board.layers import BoardLayerCommands

        board = pcbnew.BOARD()
        cmds = BoardLayerCommands(board=board)
        res = cmds.add_layer({"name": "PWR", "type": "signal", "position": "inner", "number": 2})
        assert res["success"], res
        # The bug: number=2 mapped to In1_Cu+1 = 5 = F.Silkscreen
        assert board.GetLayerName(pcbnew.In2_Cu) == "PWR"
        # F.Silkscreen must NOT have been renamed
        assert board.GetLayerName(5) == "F.Silkscreen"

    def test_inner_3_maps_to_in3_cu(self) -> None:
        import pcbnew

        from commands.board.layers import BoardLayerCommands

        board = pcbnew.BOARD()
        cmds = BoardLayerCommands(board=board)
        res = cmds.add_layer({"name": "GND2", "type": "signal", "position": "inner", "number": 3})
        assert res["success"], res
        assert board.GetLayerName(pcbnew.In3_Cu) == "GND2"

    def test_inner_number_zero_rejected(self) -> None:
        import pcbnew

        from commands.board.layers import BoardLayerCommands

        board = pcbnew.BOARD()
        cmds = BoardLayerCommands(board=board)
        res = cmds.add_layer({"name": "X", "type": "signal", "position": "inner", "number": 0})
        assert not res["success"]
        assert "1" in res.get("errorDetails", "") or "1" in res.get("message", "")

    def test_top_and_bottom_unaffected(self) -> None:
        import pcbnew

        from commands.board.layers import BoardLayerCommands

        board = pcbnew.BOARD()
        cmds = BoardLayerCommands(board=board)
        r_top = cmds.add_layer({"name": "TOP", "type": "signal", "position": "top"})
        r_bot = cmds.add_layer({"name": "BOT", "type": "signal", "position": "bottom"})
        assert r_top["success"] and r_bot["success"]
        assert board.GetLayerName(pcbnew.F_Cu) == "TOP"
        assert board.GetLayerName(pcbnew.B_Cu) == "BOT"
