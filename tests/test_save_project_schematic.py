"""
Regression test for save_project's schematic-save crash.

History: the schematic-save path used `sch.to_file()` which doesn't exist
on kicad-skip's Schematic class.  Every call emitted
"Schematic save failed: 'Schematic' object has no attribute 'to_file'".
Fix: use the documented `sch.write(fpath)` API.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


@pytest.mark.unit
class TestSaveProjectSchematic:
    def _iface(self) -> object:
        with patch("kicad_interface.USE_IPC_BACKEND", False):
            from kicad_interface import KiCADInterface
            return KiCADInterface.__new__(KiCADInterface)

    def test_schematic_save_calls_write_not_to_file(self, tmp_path: Path) -> None:
        sch = tmp_path / "x.kicad_sch"
        sch.write_text("(kicad_sch)")  # exists() check

        iface = self._iface()
        iface.board = None  # No PCB; only the schematic-save path runs.

        with patch("commands.project.pcbnew") as _mock_pcbnew:
            from commands.project import ProjectCommands
            cmds = ProjectCommands(board=None)
            with patch("skip.Schematic") as fake_skip_cls:
                fake_sch = MagicMock(spec=["write"])
                fake_skip_cls.return_value = fake_sch
                result = cmds.save_project({"schematicPath": str(sch)})

        assert result.get("success") is True, result
        assert result.get("warnings") in (None, []), (
            f"unexpected warnings (regression?): {result.get('warnings')}"
        )
        fake_sch.write.assert_called_once_with(str(sch))

    def test_schematic_save_real_skip_no_warnings(self, tmp_path: Path) -> None:
        """End-to-end against real kicad-skip — round-trips an empty schematic."""
        try:
            import skip  # noqa: F401
        except ImportError:
            pytest.skip("kicad-skip not available")

        # Use the empty template so skip parses real content.
        template = Path(__file__).parent.parent / "python" / "templates" / "empty.kicad_sch"
        sch = tmp_path / "x.kicad_sch"
        sch.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")

        from commands.project import ProjectCommands
        cmds = ProjectCommands(board=None)
        result = cmds.save_project({"schematicPath": str(sch)})
        assert result.get("success") is True
        assert not result.get("warnings"), result.get("warnings")
        assert sch.exists()
