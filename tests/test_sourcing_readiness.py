"""Tests for the pre-sync sourcing-readiness audit.

The tool answers "is this schematic ready to sync to a PCB?" in a
single call, reporting per-category gaps that would either cause
`sync_schematic_to_board` to silently drop footprints or block JLCPCB
SMT upload.  The retroactive-sync gap is the trickiest one — a
schematic that HAS Footprints set but hasn't seeded them into the
.kicad_pcb yet (which happens whenever you fill in a missing Footprint
after a first sync), because `sync_schematic_to_board` only auto-
imports on a first-fresh sync.
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
SUB_SHEET_UUID = "bb111111-1111-1111-1111-111111111111"
SUB_OWN_UUID = "b0000000-0000-0000-0000-000000000000"


def _make_project(tmp: Path, symbols_sexpr: str):
    """Build a minimal 1-root + 1-sub-sheet project.  `symbols_sexpr` is
    inlined into the sub-sheet before its closing paren, so the caller
    controls what symbols exist and which Footprint / LCSC / MPN
    properties they carry.
    """
    (tmp / "power_module.kicad_pro").write_text("{}")
    root = tmp / "power_module.kicad_sch"
    root.write_text(textwrap.dedent(f"""\
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
            (uuid "{SUB_SHEET_UUID}")
            (property "Sheetname" "sub" (at 40 39 0))
            (property "Sheetfile" "sub.kicad_sch" (at 40 91 0)))
          (sheet_instances (path "/" (page "1"))))
    """))

    sub = tmp / "sub.kicad_sch"
    sub.write_text(textwrap.dedent(f"""\
        (kicad_sch (version 20250114) (generator "test")
          (uuid "{SUB_OWN_UUID}")
          (lib_symbols
            (symbol "Device:R" (pin_numbers hide) (pin_names (offset 0))
              (symbol "R_1_1"
                (pin passive line (at 0 3.81 270) (length 1.27) (name "~") (number "1"))
                (pin passive line (at 0 -3.81 90) (length 1.27) (name "~") (number "2")))))
    """) + symbols_sexpr + "\n)\n")
    return root


def _symbol(ref: str, value: str, footprint: str = "", lcsc: str = "", mpn: str = "",
            comp_uuid: str = "cccccccc-cccc-cccc-cccc-cccccccccccc"):
    props = [
        f'(property "Reference" "{ref}" (at 100 90 0) (effects (font (size 1.27 1.27))))',
        f'(property "Value" "{value}" (at 100 110 0) (effects (font (size 1.27 1.27))))',
    ]
    if footprint:
        props.append(f'(property "Footprint" "{footprint}" (at 100 100 0) (effects (font (size 1.27 1.27)) hide))')
    if lcsc:
        props.append(f'(property "LCSC" "{lcsc}" (at 100 100 0) (effects (font (size 1.27 1.27)) hide))')
    if mpn:
        props.append(f'(property "MPN" "{mpn}" (at 100 100 0) (effects (font (size 1.27 1.27)) hide))')

    props_str = "\n          ".join(props)
    return textwrap.dedent(f"""\
        (symbol (lib_id "Device:R") (at 100 100 0) (unit 1)
          (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no) (fields_autoplaced yes)
          (uuid "{comp_uuid}")
          {props_str}
          (pin "1" (uuid "{comp_uuid[:-2]}p1"))
          (pin "2" (uuid "{comp_uuid[:-2]}p2"))
          (instances
            (project "power_module" (path "/{ROOT_UUID}/{SUB_SHEET_UUID}"
              (reference "{ref}") (unit 1)))))
    """)


@pytest.mark.unit
class TestCheckSourcingReadiness:
    def test_fully_populated_reports_ready(self):
        from commands.sourcing_readiness import check_sourcing_readiness

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            root = _make_project(tmp, _symbol(
                "R1", "10k",
                footprint="Resistor_SMD:R_0603_1608Metric",
                lcsc="C25804",
                mpn="0603WAF1002T5E",
                comp_uuid="ccccccc1-1111-1111-1111-111111111111",
            ))
            r = check_sourcing_readiness(str(root))
            assert r["ready"] is True
            assert r["counts"]["missing_footprint"] == 0
            assert r["counts"]["missing_lcsc"] == 0
            assert r["counts"]["missing_mpn"] == 0

    def test_missing_footprint_flags_component(self):
        from commands.sourcing_readiness import check_sourcing_readiness

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            root = _make_project(tmp, _symbol(
                "R1", "10k",
                # NO footprint
                lcsc="C25804", mpn="X",
                comp_uuid="ccccccc2-2222-2222-2222-222222222222",
            ))
            r = check_sourcing_readiness(str(root))
            assert r["ready"] is False
            assert r["counts"]["missing_footprint"] == 1
            assert r["missing_footprint"][0]["ref"] == "R1"
            assert r["missing_footprint"][0]["value"] == "10k"

    def test_missing_lcsc_does_not_block_ready_but_is_reported(self):
        """LCSC is required for JLCPCB SMT but not for the .kicad_pcb to
        exist — so its absence gets counted but doesn't set ready=False."""
        from commands.sourcing_readiness import check_sourcing_readiness

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            root = _make_project(tmp, _symbol(
                "R1", "10k",
                footprint="Resistor_SMD:R_0603_1608Metric",
                # NO lcsc, NO mpn
                comp_uuid="ccccccc3-3333-3333-3333-333333333333",
            ))
            r = check_sourcing_readiness(str(root))
            assert r["ready"] is True
            assert r["counts"]["missing_lcsc"] == 1
            assert r["counts"]["missing_mpn"] == 1

    def test_unresolvable_project_local_footprint(self):
        """A footprint pointing at a project-local library file that
        doesn't exist gets flagged."""
        from commands.sourcing_readiness import check_sourcing_readiness

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            # Set up a project-local fp-lib-table pointing at a real dir,
            # but reference a footprint that ISN'T inside.
            (tmp / "libs").mkdir()
            (tmp / "libs" / "power_module_lib.pretty").mkdir()
            (tmp / "libs" / "power_module_lib.pretty" / "REAL.kicad_mod").write_text("(module REAL)")
            (tmp / "fp-lib-table").write_text(textwrap.dedent("""\
                (fp_lib_table
                  (version 7)
                  (lib (name "power_module_lib")(type "KiCad")(uri "${KIPRJMOD}/libs/power_module_lib.pretty")(options "")(descr ""))
                )
            """))

            root = _make_project(tmp, _symbol(
                "U1", "MYPART",
                footprint="power_module_lib:MISSING",  # <-- not on disk
                lcsc="C1", mpn="X",
                comp_uuid="ccccccc4-4444-4444-4444-444444444444",
            ))
            r = check_sourcing_readiness(str(root))
            assert r["ready"] is False
            assert r["counts"]["unresolvable_footprint"] == 1
            assert r["unresolvable_footprint"][0]["footprint"] == "power_module_lib:MISSING"

    def test_system_footprints_are_trusted(self):
        """A footprint referencing a system library (not in the local
        fp-lib-table) is trusted — we don't scan /usr/share/kicad, and a
        false positive would be worse than a false negative here."""
        from commands.sourcing_readiness import check_sourcing_readiness

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            # NO fp-lib-table at all → system lib untouched.
            root = _make_project(tmp, _symbol(
                "R1", "10k",
                footprint="Resistor_SMD:R_0603_1608Metric",  # system
                lcsc="C25804", mpn="X",
                comp_uuid="ccccccc5-5555-5555-5555-555555555555",
            ))
            r = check_sourcing_readiness(str(root))
            assert r["counts"]["unresolvable_footprint"] == 0

    def test_retroactive_sync_gap_when_board_missing_component(self):
        """A symbol with a Footprint set but not on the .kicad_pcb
        yet — the classic post-first-sync-fill-in-missing-footprint
        situation — gets flagged with a warning."""
        from commands.sourcing_readiness import check_sourcing_readiness

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            root = _make_project(tmp, _symbol(
                "R1", "10k",
                footprint="Resistor_SMD:R_0603_1608Metric",
                lcsc="C25804", mpn="X",
                comp_uuid="ccccccc6-6666-6666-6666-666666666666",
            ) + _symbol(
                "R2", "1k",
                footprint="Resistor_SMD:R_0603_1608Metric",
                lcsc="C21190", mpn="X",
                comp_uuid="ccccccc7-7777-7777-7777-777777777777",
            ))
            # PCB has ONLY R1 (simulates first-sync-with-R2-missing-footprint,
            # then user filled in R2's footprint but sync doesn't retro-import).
            board = tmp / "power_module.kicad_pcb"
            board.write_text(textwrap.dedent("""\
                (kicad_pcb (version 20241229) (generator "test") (general (thickness 1.6))
                  (footprint "Resistor_SMD:R_0603_1608Metric" (layer "F.Cu")
                    (uuid "aaaa1111-1111-1111-1111-111111111111")
                    (at 0 0)
                    (property "Reference" "R1" (at 0 -2 0) (layer "F.SilkS")
                      (effects (font (size 1 1))))
                    (property "Value" "10k" (at 0 2 0) (layer "F.Fab")
                      (effects (font (size 1 1))))))
            """))
            r = check_sourcing_readiness(str(root), str(board))
            assert r["counts"]["retroactive_sync_gap"] == 1
            assert r["retroactive_sync_gap"][0]["ref"] == "R2"
            assert r["ready"] is False
            assert any("retroactive" not in w or "sync" in w for w in r["warnings"])
            assert any("git checkout" in w or "reset" in w.lower() for w in r["warnings"])

    def test_pwr_flags_are_ignored(self):
        from commands.sourcing_readiness import check_sourcing_readiness

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            root = _make_project(tmp, _symbol(
                "#FLG1", "PWR_FLAG",
                comp_uuid="ccccccc8-8888-8888-8888-888888888888",
            ))
            r = check_sourcing_readiness(str(root))
            assert r["ready"] is True
            assert r["counts"]["total"] == 0
