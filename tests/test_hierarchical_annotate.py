"""Tests for hierarchical annotation.

kicad-cli's BOM / netlist tools warn "schematic has annotation errors"
when two sub-sheets in a hierarchical project both use the same local
reference (typical after per-sheet annotation).  We suffix every
sub-sheet's refs with `_{SHEETNAME}{instance_num}` so refs are globally
unique.  Local `(property "Reference")` and every
`(instances (project (path (reference ...))))` entry both get rewritten.
"""

from __future__ import annotations

import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

PYTHON_DIR = Path(__file__).resolve().parents[1] / "python"
sys.path.insert(0, str(PYTHON_DIR))


ROOT_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
BMS_SHEET_UUID = "bb111111-1111-1111-1111-111111111111"
CHARGER_SHEET_UUID = "cc222222-2222-2222-2222-222222222222"
BMS_OWN_UUID = "b0000000-0000-0000-0000-000000000000"
CHARGER_OWN_UUID = "c0000000-0000-0000-0000-000000000000"


def _sym_block(ref: str, value: str, comp_uuid: str, path: str) -> str:
    """A minimally-complete (symbol ...) block that SchematicManager can round-trip."""
    return textwrap.dedent(f"""\
        (symbol (lib_id "Device:R") (at 100 100 0) (unit 1)
          (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no) (fields_autoplaced yes)
          (uuid "{comp_uuid}")
          (property "Reference" "{ref}" (at 100 90 0)
            (effects (font (size 1.27 1.27))))
          (property "Value" "{value}" (at 100 110 0)
            (effects (font (size 1.27 1.27))))
          (property "Footprint" "Resistor_SMD:R_0603_1608Metric" (at 100 100 0)
            (effects (font (size 1.27 1.27)) hide))
          (property "Datasheet" "" (at 100 100 0)
            (effects (font (size 1.27 1.27)) hide))
          (property "Description" "" (at 100 100 0)
            (effects (font (size 1.27 1.27)) hide))
          (pin "1" (uuid "{comp_uuid[:-2]}p1"))
          (pin "2" (uuid "{comp_uuid[:-2]}p2"))
          (instances
            (project "power_module" (path "{path}"
              (reference "{ref}") (unit 1)))))
    """)


def _root_sch(tmp: Path) -> Path:
    """Write a root .kicad_sch with 2 sub-sheets (bms + charger).  Returns
    the root path.  Also writes a .kicad_pro at the same stem so
    _find_parent_project_sheet finds it.
    """
    (tmp / "power_module.kicad_pro").write_text("{}")
    root_sch = tmp / "power_module.kicad_sch"
    root_sch.write_text(textwrap.dedent(f"""\
        (kicad_sch (version 20250114) (generator "test")
          (uuid "{ROOT_UUID}")
          (lib_symbols
            (symbol "Device:R" (pin_numbers hide) (pin_names (offset 0))
              (symbol "R_1_1"
                (pin passive line (at 0 3.81 270) (length 1.27) (name "~") (number "1"))
                (pin passive line (at 0 -3.81 90) (length 1.27) (name "~") (number "2")))))
          (sheet (at 40 40) (size 60 50) (fields_autoplaced yes)
            (stroke (width 0.1524) (type solid))
            (fill (color 0 0 0 0.0000))
            (uuid "{BMS_SHEET_UUID}")
            (property "Sheetname" "bms" (at 40 39 0))
            (property "Sheetfile" "bms.kicad_sch" (at 40 91 0)))
          (sheet (at 130 40) (size 60 50) (fields_autoplaced yes)
            (stroke (width 0.1524) (type solid))
            (fill (color 0 0 0 0.0000))
            (uuid "{CHARGER_SHEET_UUID}")
            (property "Sheetname" "charger" (at 130 39 0))
            (property "Sheetfile" "charger.kicad_sch" (at 130 91 0)))
          (sheet_instances (path "/" (page "1"))))
    """))
    return root_sch


def _bms_sub(tmp: Path, r1_value: str = "100") -> Path:
    """bms sub-sheet with R1 (Reference collision candidate)."""
    path = f"/{ROOT_UUID}/{BMS_SHEET_UUID}"
    sub = tmp / "bms.kicad_sch"
    sub.write_text(textwrap.dedent(f"""\
        (kicad_sch (version 20250114) (generator "test")
          (uuid "{BMS_OWN_UUID}")
          (lib_symbols
            (symbol "Device:R" (pin_numbers hide) (pin_names (offset 0))
              (symbol "R_1_1"
                (pin passive line (at 0 3.81 270) (length 1.27) (name "~") (number "1"))
                (pin passive line (at 0 -3.81 90) (length 1.27) (name "~") (number "2")))))
    """) + _sym_block("R1", r1_value, "bbbbbbb1-0000-0000-0000-000000000001", path) + "\n)\n")
    return sub


def _charger_sub(tmp: Path, r1_value: str = "10k") -> Path:
    """charger sub-sheet with a colliding R1."""
    path = f"/{ROOT_UUID}/{CHARGER_SHEET_UUID}"
    sub = tmp / "charger.kicad_sch"
    sub.write_text(textwrap.dedent(f"""\
        (kicad_sch (version 20250114) (generator "test")
          (uuid "{CHARGER_OWN_UUID}")
          (lib_symbols
            (symbol "Device:R" (pin_numbers hide) (pin_names (offset 0))
              (symbol "R_1_1"
                (pin passive line (at 0 3.81 270) (length 1.27) (name "~") (number "1"))
                (pin passive line (at 0 -3.81 90) (length 1.27) (name "~") (number "2")))))
    """) + _sym_block("R1", r1_value, "ccccccc1-0000-0000-0000-000000000001", path) + "\n)\n")
    return sub


@pytest.mark.unit
class TestFindSheetInstancesInRoot:
    def test_two_sheets_discovered(self):
        from commands.hierarchical_annotate import _find_sheet_instances_in_root

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            root = _root_sch(tmp)
            result = _find_sheet_instances_in_root(root)
            assert set(result.keys()) == {BMS_SHEET_UUID, CHARGER_SHEET_UUID}
            assert result[BMS_SHEET_UUID] == ("bms", "bms.kicad_sch")
            assert result[CHARGER_SHEET_UUID] == ("charger", "charger.kicad_sch")


@pytest.mark.unit
class TestHierarchicalDisambiguate:
    def test_colliding_refs_get_sheetname_suffix(self):
        from commands.hierarchical_annotate import hierarchical_disambiguate

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            root = _root_sch(tmp)
            bms = _bms_sub(tmp)
            charger = _charger_sub(tmp)

            result = hierarchical_disambiguate(str(root))
            assert result["success"], result
            # Two colliding R1s → two renames.
            renamed = result["renamed"]
            new_by_sheet = {r["sheet"]: r["newRef"] for r in renamed}
            assert new_by_sheet == {
                "bms.kicad_sch": "R1_BMS1",
                "charger.kicad_sch": "R1_CHARGER1",
            }

            # File contents should reflect BOTH the local Reference and
            # the (instances (path (reference ...))) block.
            bms_txt = bms.read_text()
            charger_txt = charger.read_text()
            assert "R1_BMS1" in bms_txt and "R1_CHARGER1" in charger_txt
            # Old bare "R1" should not appear as a Reference property
            # anywhere in either sub-sheet after rewrite.
            import re
            assert not re.search(r'\(property\s+"Reference"\s+"R1"', bms_txt)
            assert not re.search(r'\(property\s+"Reference"\s+"R1"', charger_txt)

    def test_idempotent_on_second_run(self):
        from commands.hierarchical_annotate import hierarchical_disambiguate

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            root = _root_sch(tmp)
            _bms_sub(tmp)
            _charger_sub(tmp)

            first = hierarchical_disambiguate(str(root))
            assert first["success"] and len(first["renamed"]) == 2

            # Second run must not add any new renames (idempotency).
            second = hierarchical_disambiguate(str(root))
            assert second["success"]
            assert second["renamed"] == [], (
                f"expected no-op on 2nd run, got renames: {second['renamed']!r}"
            )

    def test_no_sub_sheets_is_noop(self):
        """Standalone .kicad_sch (no (sheet ...) blocks) is unaffected —
        the tool short-circuits with a friendly message."""
        from commands.hierarchical_annotate import hierarchical_disambiguate

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            leaf = tmp / "leaf.kicad_sch"
            leaf.write_text(textwrap.dedent("""\
                (kicad_sch (version 20250114) (generator "test")
                  (uuid "dddddddd-dddd-dddd-dddd-dddddddddddd")
                  (lib_symbols)
                  (sheet_instances (path "/" (page "1"))))
            """))
            r = hierarchical_disambiguate(str(leaf))
            assert r["success"] and r["renamed"] == []
            assert "no sub-sheets" in r["message"].lower()

    def test_sheetname_with_special_chars_sanitized(self):
        """A sheet named e.g. "Power In (USB)" gets suffixed as _POWER_IN_USB1."""
        from commands.hierarchical_annotate import _sanitize_sheetname
        assert _sanitize_sheetname("Power In (USB)") == "POWER_IN_USB"
        assert _sanitize_sheetname("bms") == "BMS"
        assert _sanitize_sheetname("") == "SHEET"
        # Leading digit → prepend 'S' so the ref stays a valid identifier.
        assert _sanitize_sheetname("18650_bank").startswith("S")

    def test_already_suffixed_ref_gets_base_reused(self):
        """If refs already have `_BMS1` on them and we're re-running because
        (say) a new symbol got added, the new symbol should get suffixed
        without piling `_BMS1` on the already-suffixed ones.
        """
        from commands.hierarchical_annotate import hierarchical_disambiguate

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            root = _root_sch(tmp)
            _bms_sub(tmp)
            _charger_sub(tmp)

            # First run to establish suffixed baseline.
            hierarchical_disambiguate(str(root))

            # Manually add a new bare-R2 to bms and re-run.
            bms = tmp / "bms.kicad_sch"
            txt = bms.read_text()
            # Insert a second symbol before the closing ')' of the kicad_sch.
            new_sym = _sym_block(
                "R2", "1k", "bbbbbbb2-0000-0000-0000-000000000002",
                f"/{ROOT_UUID}/{BMS_SHEET_UUID}",
            )
            # Splice in just before the final closing paren.
            insertion_point = txt.rfind(")")
            bms.write_text(txt[:insertion_point] + "\n" + new_sym + "\n" + txt[insertion_point:])

            result = hierarchical_disambiguate(str(root))
            assert result["success"]
            # R2 should be suffixed; R1 (already suffixed) should not appear
            # in renames.
            new_bms_txt = bms.read_text()
            assert "R2_BMS1" in new_bms_txt
            # Old suffixed ref survives untouched.
            assert "R1_BMS1" in new_bms_txt
            # No double-suffix ever.
            assert "_BMS1_BMS1" not in new_bms_txt


@pytest.mark.unit
class TestHandlerIntegration:
    """`_handle_annotate_schematic` on a root schematic should end up with
    globally-unique refs — the acceptance test for the whole feature."""

    def test_root_annotate_disambiguates_hierarchy(self):
        from kicad_interface import KiCADInterface

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            root = _root_sch(tmp)
            _bms_sub(tmp)
            _charger_sub(tmp)

            iface = KiCADInterface()
            result = iface._handle_annotate_schematic({"schematicPath": str(root)})
            assert result["success"], result
            disamb = result.get("hierarchicalDisambiguation", {})
            assert disamb.get("success"), disamb
            new_refs = {r["sheet"]: r["newRef"] for r in disamb["renamed"]}
            assert new_refs == {
                "bms.kicad_sch": "R1_BMS1",
                "charger.kicad_sch": "R1_CHARGER1",
            }
