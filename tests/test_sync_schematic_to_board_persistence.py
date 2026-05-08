"""
Regression test for sync_schematic_to_board's autoImport persistence bug.

Bug history: when called against an empty board file, the handler reported
success ("Auto-imported N footprints") but the .kicad_pcb stayed at its
86-line empty-template baseline.  Cause: the handler maintained a local
`board` loaded fresh from disk while auto-import placed footprints into a
separate `self.board` (no save).  The final board.Save() then clobbered
disk with the stale empty local board.

Fix: use self.board throughout the handler.  This test guards against
regressing to the divergent-board pattern by verifying that the board
the auto-import receives is the same one that ends up being saved.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


@pytest.mark.unit
class TestSyncSchematicToBoardPersistence:
    def _make_iface(self) -> object:
        with patch("kicad_interface.USE_IPC_BACKEND", False):
            from kicad_interface import KiCADInterface
            return KiCADInterface.__new__(KiCADInterface)

    def test_autoimport_save_target_is_self_board(self, tmp_path: Path) -> None:
        """The board.Save() at the end of the handler must hit self.board (the
        same object auto-import places footprints into).  Previously a separate
        local `board` was loaded fresh from disk, so the footprints — only in
        self.board — never reached disk."""
        sch_path = tmp_path / "x.kicad_sch"
        sch_path.write_text("(kicad_sch)")  # exists() check only
        pcb_path = tmp_path / "x.kicad_pcb"
        pcb_path.write_text("")

        iface = self._make_iface()
        # Pre-load self.board with a sentinel so auto-import has somewhere to
        # place footprints; the handler must not replace it with a fresh load.
        sentinel_board = MagicMock(name="self.board")
        sentinel_board.GetFileName.return_value = str(pcb_path)
        sentinel_board.GetFootprints.return_value = []
        iface.board = sentinel_board
        iface.update_board = MagicMock()

        # Track which board object auto-import received.
        seen: dict = {}

        def fake_auto_import(board, schematic_path, board_path):
            seen["auto_import_board_id"] = id(board)
            return {"placed": ["R1"], "errors": [], "skipped": 0}

        iface._auto_import_footprints_from_schematic = fake_auto_import
        iface._build_hierarchical_pad_net_map = MagicMock(return_value=({}, []))
        iface._update_command_handlers = MagicMock()

        # Mock pcbnew.LoadBoard so we can detect if the handler still loads
        # a parallel local board (which was the bug).
        load_board_mock = MagicMock(name="LoadBoard.result")
        load_board_mock.GetFileName.return_value = str(pcb_path)
        load_board_mock.GetFootprints.return_value = []

        with patch("kicad_interface.pcbnew") as mock_pcbnew:
            mock_pcbnew.LoadBoard.return_value = load_board_mock
            mock_pcbnew.NETINFO_ITEM = MagicMock()

            # Skip board_path branch so handler uses self.board directly —
            # this is the elif branch, which already used self.board correctly.
            result = iface._handle_sync_schematic_to_board({"schematicPath": str(sch_path)})

        assert result.get("success") is True, result
        # Auto-import operated on self.board
        assert seen["auto_import_board_id"] == id(sentinel_board)
        # The final Save must have hit self.board (= sentinel_board), not a
        # parallel LoadBoard result.
        sentinel_board.Save.assert_called_with(str(pcb_path))
        load_board_mock.Save.assert_not_called()

    def test_boardpath_provided_reloads_into_self_board(self, tmp_path: Path) -> None:
        """When boardPath is provided, the handler must reload self.board (not
        keep a separate local copy) so subsequent _handle_place_component calls
        see the freshly-loaded board."""
        sch_path = tmp_path / "y.kicad_sch"
        sch_path.write_text("(kicad_sch)")
        pcb_path = tmp_path / "y.kicad_pcb"
        pcb_path.write_text("")

        iface = self._make_iface()
        iface.board = None  # No board loaded yet
        iface._auto_import_footprints_from_schematic = MagicMock(
            return_value={"placed": [], "errors": [], "skipped": 0}
        )
        iface._build_hierarchical_pad_net_map = MagicMock(return_value=({}, []))
        iface._update_command_handlers = MagicMock()

        loaded_board = MagicMock(name="LoadBoard.result")
        loaded_board.GetFileName.return_value = str(pcb_path)
        loaded_board.GetFootprints.return_value = []

        with patch("kicad_interface.pcbnew") as mock_pcbnew:
            mock_pcbnew.LoadBoard.return_value = loaded_board
            mock_pcbnew.NETINFO_ITEM = MagicMock()

            result = iface._handle_sync_schematic_to_board(
                {"schematicPath": str(sch_path), "boardPath": str(pcb_path)}
            )

        assert result.get("success") is True
        # self.board was set to the loaded board, not left alone.
        assert iface.board is loaded_board
        loaded_board.Save.assert_called_with(str(pcb_path))
