"""Regression: set_board_size + add_board_outline used to silently
overlap (two stacked rectangles, 8 Edge.Cuts segments, DRC
self-intersection at every corner). After the fix:

- set_board_size defaults to replace=True (semantic: "set" implies
  "this is the outline").
- add_board_outline keeps existing by default (semantic: "add a
  shape" — for cutouts) with optional replace=True.
- Both report edgeCutsBefore + edgeCutsRemoved so the caller sees
  exactly what happened.

Tests pin all three behaviours via real pcbnew + an empty BOARD()
fixture.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))


def _real_pcbnew_available() -> bool:
    import pcbnew  # type: ignore
    return getattr(pcbnew, "GetBuildVersion", lambda: "")() != "9.0.0-stub"


@pytest.mark.integration
@pytest.mark.skipif(
    not _real_pcbnew_available(),
    reason="needs real pcbnew swig module",
)
class TestEdgeCutsReplace:
    def _board(self):
        import pcbnew
        return pcbnew.BOARD()

    def _count_edges(self, b) -> int:
        edge_layer = b.GetLayerID("Edge.Cuts")
        return sum(1 for d in b.GetDrawings() if d.GetLayer() == edge_layer)

    def test_add_outline_keeps_existing_by_default(self):
        from commands.board.outline import BoardOutlineCommands
        b = self._board()
        oc = BoardOutlineCommands(b)
        oc.add_board_outline({"shape": "rectangle", "width": 50, "height": 40})
        # Second call without replace — both rectangles should remain
        r = oc.add_board_outline({
            "shape": "rectangle", "width": 10, "height": 8,
            "centerX": 25, "centerY": 20,
        })
        assert r["success"]
        assert r["edgeCutsBefore"] == 4
        assert r["edgeCutsRemoved"] == 0
        assert "kept 4 existing" in r["message"]
        assert self._count_edges(b) == 8  # 4 outer + 4 cutout

    def test_add_outline_replace_wipes_existing(self):
        from commands.board.outline import BoardOutlineCommands
        b = self._board()
        oc = BoardOutlineCommands(b)
        oc.add_board_outline({"shape": "rectangle", "width": 50, "height": 40})
        r = oc.add_board_outline({
            "shape": "rectangle", "width": 60, "height": 50,
            "replace": True,
        })
        assert r["success"]
        assert r["edgeCutsBefore"] == 4
        assert r["edgeCutsRemoved"] == 4
        assert "replaced 4 existing" in r["message"]
        assert self._count_edges(b) == 4  # only the new rectangle

    def test_set_board_size_replaces_by_default(self):
        """The headline bug-fix case: outline + set_board_size used to
        give 8 stacked segments. Now set_board_size auto-replaces."""
        from commands.board.outline import BoardOutlineCommands
        from commands.board.size import BoardSizeCommands
        b = self._board()
        BoardOutlineCommands(b).add_board_outline(
            {"shape": "rectangle", "width": 38, "height": 30}
        )
        r = BoardSizeCommands(b).set_board_size({"width": 40, "height": 32})
        assert r["success"]
        assert r["edgeCutsBefore"] == 4
        assert r["edgeCutsRemoved"] == 4
        assert self._count_edges(b) == 4

    def test_set_board_size_replace_false_stacks(self):
        """Explicit opt-out lets the caller keep both shapes (rarely
        what they want, but the path exists)."""
        from commands.board.outline import BoardOutlineCommands
        from commands.board.size import BoardSizeCommands
        b = self._board()
        BoardOutlineCommands(b).add_board_outline(
            {"shape": "rectangle", "width": 38, "height": 30}
        )
        r = BoardSizeCommands(b).set_board_size(
            {"width": 40, "height": 32, "replace": False}
        )
        assert r["success"]
        assert r["edgeCutsRemoved"] == 0
        assert self._count_edges(b) == 8
