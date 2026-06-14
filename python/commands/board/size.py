"""
Board size command implementations for KiCAD interface
"""

import logging
from typing import Any, Dict, Optional

import pcbnew

logger = logging.getLogger("kicad_interface")


class BoardSizeCommands:
    """Handles board size operations"""

    def __init__(self, board: Optional[pcbnew.BOARD] = None):
        """Initialize with optional board instance"""
        self.board = board

    def set_board_size(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Set the size of the PCB board by creating edge cuts outline.

        Semantically "SET the size" implies "this should be the
        outline" — so any existing Edge.Cuts is replaced by default.
        Pass `replace=false` to add a second outline instead (rarely
        what you want; use `add_board_outline` for cutouts and
        secondary shapes). Before this guard, set_board_size +
        add_board_outline (or two set_board_size calls) silently
        produced overlapping rectangles + DRC self-intersection
        violations at every corner.
        """
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            width = params.get("width")
            height = params.get("height")
            unit = params.get("unit", "mm")
            # Default to replacing existing Edge.Cuts — see docstring.
            replace = bool(params.get("replace", True))

            if width is None or height is None:
                return {
                    "success": False,
                    "message": "Missing dimensions",
                    "errorDetails": "Both width and height are required",
                }

            # Delegate to add_board_outline, passing replace through.
            from commands.board.outline import BoardOutlineCommands

            outline_commands = BoardOutlineCommands(self.board)
            result = outline_commands.add_board_outline(
                {
                    "shape": "rectangle",
                    "centerX": width / 2,
                    "centerY": height / 2,
                    "width": width,
                    "height": height,
                    "unit": unit,
                    "replace": replace,
                }
            )

            if not result.get("success"):
                return result

            removed = result.get("edgeCutsRemoved", 0)
            note = f" (replaced {removed} existing Edge.Cuts segment(s))" if removed else ""
            return {
                "success": True,
                "message": f"Created board outline: {width}x{height} {unit}{note}",
                "size": {"width": width, "height": height, "unit": unit},
                "edgeCutsBefore": result.get("edgeCutsBefore", 0),
                "edgeCutsRemoved": removed,
            }

        except Exception as e:
            logger.error(f"Error setting board size: {str(e)}")
            return {"success": False, "message": "Failed to set board size", "errorDetails": str(e)}
