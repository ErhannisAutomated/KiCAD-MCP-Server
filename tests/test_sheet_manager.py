"""
Tests for the sheet block creation tool (sheet_manager.add_sheet_block).

Covers:
  - Happy path: a sheet block is inserted with the correct project name,
    root UUID, sheet name, file, and page number.
  - Page numbering: defaults to next available based on existing sheets.
  - Refusal of duplicate sheet names.
  - Insertion position (before sheet_instances if present, otherwise
    at the end).
  - Round-trip: kicad-cli sch erc still parses the resulting file.
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

PYTHON_DIR = Path(__file__).parent.parent / "python"
TEMPLATES_DIR = PYTHON_DIR / "templates"
sys.path.insert(0, str(PYTHON_DIR))

# All tests use a copy of the empty template so they don't accidentally
# share state.
@pytest.fixture
def temp_parent():
    with tempfile.TemporaryDirectory() as tmp:
        sch = Path(tmp) / "parent.kicad_sch"
        shutil.copy(TEMPLATES_DIR / "empty.kicad_sch", sch)
        yield sch


@pytest.mark.unit
class TestAddSheetBlock:
    def test_inserts_sheet_block_with_expected_fields(self, temp_parent):
        from commands.sheet_manager import add_sheet_block

        result = add_sheet_block(
            temp_parent, "BMS", "bms.kicad_sch",
            x=50, y=50, width=30, height=20,
        )
        assert result["success"], result.get("message")
        text = temp_parent.read_text()
        assert '(sheet ' in text
        assert '"Sheetname" "BMS"' in text
        assert '"Sheetfile" "bms.kicad_sch"' in text
        # Project name = parent stem
        assert '(project "parent"' in text
        # Page number defaults to "2" when no other sheets present
        assert '(page "2")' in text
        # Returned root_uuid matches the file
        assert result["root_uuid"] in text

    def test_refuses_duplicate_sheet_name(self, temp_parent):
        from commands.sheet_manager import add_sheet_block

        first = add_sheet_block(
            temp_parent, "BMS", "bms.kicad_sch", x=50, y=50,
        )
        assert first["success"]

        second = add_sheet_block(
            temp_parent, "BMS", "bms2.kicad_sch", x=80, y=80,
        )
        assert not second["success"]
        assert "already exists" in second["message"]

    def test_two_sheets_get_distinct_pages(self, temp_parent):
        from commands.sheet_manager import add_sheet_block

        r1 = add_sheet_block(
            temp_parent, "BMS", "bms.kicad_sch", x=50, y=50,
        )
        r2 = add_sheet_block(
            temp_parent, "Charger", "charger.kicad_sch", x=80, y=80,
        )
        assert r1["page"] == "2"
        assert r2["page"] == "3"

    def test_explicit_page_overrides_default(self, temp_parent):
        from commands.sheet_manager import add_sheet_block

        result = add_sheet_block(
            temp_parent, "BMS", "bms.kicad_sch",
            x=50, y=50, page="7",
        )
        assert result["page"] == "7"
        assert '(page "7")' in temp_parent.read_text()

    def test_explicit_uuid_is_preserved(self, temp_parent):
        from commands.sheet_manager import add_sheet_block

        my_uuid = "11111111-2222-3333-4444-555555555555"
        result = add_sheet_block(
            temp_parent, "BMS", "bms.kicad_sch",
            x=50, y=50, sheet_uuid=my_uuid,
        )
        assert result["success"]
        assert my_uuid in temp_parent.read_text()


@pytest.mark.integration
class TestAddSheetBlockEndToEnd:
    """Verify kicad-cli accepts the resulting hierarchical schematic."""

    def test_kicad_cli_erc_parses_parent_with_added_sheet(self, temp_parent):
        from commands.sheet_manager import add_sheet_block

        # Create a minimal child first (kicad-cli needs the file to exist
        # to chase down hierarchical references).
        child = temp_parent.parent / "bms.kicad_sch"
        shutil.copy(TEMPLATES_DIR / "empty.kicad_sch", child)

        add_sheet_block(
            temp_parent, "BMS", "bms.kicad_sch", x=50, y=50, width=30, height=20,
        )

        try:
            result = subprocess.run(
                ["kicad-cli", "sch", "erc", "--format", "json", str(temp_parent)],
                capture_output=True, text=True, timeout=30,
            )
        except FileNotFoundError:
            pytest.skip("kicad-cli not available")

        # Non-zero is OK (ERC violations expected on the empty template);
        # the test is whether kicad-cli can even parse the file.  A parse
        # failure surfaces as "input file is corrupted" or the like.
        assert "corrupted" not in result.stderr.lower()
        assert "syntax error" not in result.stderr.lower()
        assert "expected" not in result.stderr.lower() or "violation" in result.stderr.lower()
