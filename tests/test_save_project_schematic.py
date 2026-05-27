"""
Regression tests for save_project's schematic handling.

History:
- The schematic-save path originally called `sch.to_file()` which doesn't exist
  on kicad-skip's Schematic class.  Fix: use `sch.write(fpath)`.
- Worse, save_project ran that kicad-skip round-trip on EVERY save by
  auto-deriving the schematic path from the loaded board.  kicad-skip's
  serialiser doesn't preserve the lib_symbols cache section, so a PCB-only
  save silently truncated the .kicad_sch from ~1449 lines to ~278.  See #220.

Current contract:
- PCB save runs whenever a board is loaded.
- Schematic save is OPT-IN: caller must pass `schematicPath` AND
  `flushSchematic=true`.  Otherwise the .kicad_sch is left untouched.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


@pytest.mark.unit
class TestSaveProjectSchematic:
    def test_pcb_only_save_does_not_touch_schematic(self, tmp_path: Path) -> None:
        """Regression for #220: PCB-only save must not invoke kicad-skip."""
        with patch("commands.project.pcbnew") as mock_pcbnew:
            from commands.project import ProjectCommands
            fake_board = MagicMock()
            fake_board.GetFileName.return_value = str(tmp_path / "x.kicad_pcb")
            cmds = ProjectCommands(board=fake_board)

            # Create a schematic next to the board so the OLD auto-derive
            # path would have picked it up.
            sch = tmp_path / "x.kicad_sch"
            sch.write_text("(kicad_sch)")

            with patch("skip.Schematic") as fake_skip_cls:
                result = cmds.save_project({})
                fake_skip_cls.assert_not_called()

        assert result.get("success") is True, result
        mock_pcbnew.SaveBoard.assert_called_once()

    def test_explicit_flush_runs_kicad_skip(self, tmp_path: Path) -> None:
        """Opt-in flushSchematic=true still calls sch.write()."""
        sch = tmp_path / "x.kicad_sch"
        sch.write_text("(kicad_sch)")

        with patch("commands.project.pcbnew"):
            from commands.project import ProjectCommands
            cmds = ProjectCommands(board=None)
            with patch("skip.Schematic") as fake_skip_cls:
                fake_sch = MagicMock(spec=["write"])
                fake_skip_cls.return_value = fake_sch
                result = cmds.save_project(
                    {"schematicPath": str(sch), "flushSchematic": True}
                )

        assert result.get("success") is True, result
        fake_sch.write.assert_called_once_with(str(sch))
        warnings = result.get("warnings") or []
        assert any("#220" in w for w in warnings), warnings

    def test_schematic_path_without_flush_is_no_op(self, tmp_path: Path) -> None:
        """Passing schematicPath alone (no flushSchematic) must NOT round-trip."""
        sch = tmp_path / "x.kicad_sch"
        sch.write_text("(kicad_sch)")

        with patch("commands.project.pcbnew"):
            from commands.project import ProjectCommands
            cmds = ProjectCommands(board=None)
            with patch("skip.Schematic") as fake_skip_cls:
                result = cmds.save_project({"schematicPath": str(sch)})
                fake_skip_cls.assert_not_called()

        # No board, no flush -> "nothing to save"
        assert result.get("success") is False, result

    def test_explicit_flush_real_skip_no_warnings(self, tmp_path: Path) -> None:
        """End-to-end against real kicad-skip — round-trips an empty schematic."""
        try:
            import skip  # noqa: F401
        except ImportError:
            pytest.skip("kicad-skip not available")

        template = Path(__file__).parent.parent / "python" / "templates" / "empty.kicad_sch"
        sch = tmp_path / "x.kicad_sch"
        sch.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")

        from commands.project import ProjectCommands
        cmds = ProjectCommands(board=None)
        result = cmds.save_project(
            {"schematicPath": str(sch), "flushSchematic": True}
        )
        assert result.get("success") is True
        assert sch.exists()
