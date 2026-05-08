"""
Tests for JLCPCB category derivation and backfill.

The jlcsearch /components/list.json endpoint doesn't return a per-row
category, so the `category` column was always empty after import.  Now
JLCPCBPartsManager._derive_category_from_description pattern-matches the
description into a category, applied both during import_jlcsearch_parts
and via the backfill_categories method for pre-existing rows.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


@pytest.mark.unit
class TestCategoryDerivation:
    def test_resistor_description(self) -> None:
        from commands.jlcpcb_parts import JLCPCBPartsManager
        cat, sub = JLCPCBPartsManager._derive_category_from_description(
            "100mW 150Ω 75V Thick Film Resistor ±1% ±100ppm/℃ 0603 Chip Resistor"
        )
        assert cat == "Resistors"
        assert sub == ""

    def test_capacitor_description(self) -> None:
        from commands.jlcpcb_parts import JLCPCBPartsManager
        cat, _ = JLCPCBPartsManager._derive_category_from_description(
            "10uF ±10% 25V X5R 0603 Multilayer Ceramic Capacitor MLCC"
        )
        assert cat == "Capacitors"

    def test_specific_diodes_outrank_generic(self) -> None:
        """Schottky/Zener/TVS get their own subcategory rather than 'Diodes'."""
        from commands.jlcpcb_parts import JLCPCBPartsManager
        derive = JLCPCBPartsManager._derive_category_from_description
        assert derive("40V 1A Schottky Diode SOD-123")[0] == "Diodes / Schottky"
        assert derive("3.3V 0.5W Zener Diode SOD-323")[0] == "Diodes / Zener"
        assert derive("5V Bidirectional TVS Diode SOT-23")[0] == "Diodes / TVS"
        assert derive("100V 1A Standard Recovery Diode DO-41")[0] == "Diodes"

    def test_ferrite_bead_outranks_inductor(self) -> None:
        from commands.jlcpcb_parts import JLCPCBPartsManager
        cat, _ = JLCPCBPartsManager._derive_category_from_description(
            "600Ω@100MHz 200mA 0603 Ferrite Beads"
        )
        assert cat == "Inductors / Ferrite Beads"

    def test_microcontroller_keyword(self) -> None:
        from commands.jlcpcb_parts import JLCPCBPartsManager
        cat, _ = JLCPCBPartsManager._derive_category_from_description(
            "STM32F103C8T6 ARM Microcontroller LQFP-48"
        )
        assert cat == "ICs / Microcontrollers"

    def test_no_match_returns_empty(self) -> None:
        from commands.jlcpcb_parts import JLCPCBPartsManager
        cat, sub = JLCPCBPartsManager._derive_category_from_description(
            "completely opaque description with no recognised keywords"
        )
        assert cat == ""
        assert sub == ""

    def test_empty_description(self) -> None:
        from commands.jlcpcb_parts import JLCPCBPartsManager
        cat, sub = JLCPCBPartsManager._derive_category_from_description("")
        assert (cat, sub) == ("", "")


@pytest.mark.integration
class TestImportPopulatesCategory:
    def _make_manager_with_temp_db(self, tmp_path: Path) -> object:
        from commands.jlcpcb_parts import JLCPCBPartsManager
        return JLCPCBPartsManager(db_path=str(tmp_path / "test.db"))

    def test_import_jlcsearch_parts_fills_category_from_description(self, tmp_path: Path) -> None:
        from commands.jlcpcb_parts import JLCPCBPartsManager
        mgr = self._make_manager_with_temp_db(tmp_path)
        mgr.import_jlcsearch_parts([
            {
                "lcsc": 1234,
                "description": "10kΩ 0603 Thick Film Resistor",
                "is_basic": True,
            },
            {
                "lcsc": 5678,
                "description": "100uF 16V Aluminum Electrolytic Capacitor",
                "is_basic": False,
            },
            {
                "lcsc": 9012,
                "category": "Pre-existing Category",  # upstream-supplied wins
                "description": "10uF 0603 MLCC Capacitor",
            },
        ])

        rows = mgr.conn.execute(
            "SELECT lcsc, category FROM components ORDER BY lcsc"
        ).fetchall()
        assert dict(rows) == {
            "C1234": "Resistors",
            "C5678": "Capacitors",
            "C9012": "Pre-existing Category",
        }


@pytest.mark.unit
class TestQueryUnitNormalization:
    def test_ohms_to_omega(self) -> None:
        from commands.jlcpcb_parts import JLCPCBPartsManager
        n = JLCPCBPartsManager._normalize_query_units
        assert n("150ohms 0603") == "150Ω 0603"
        assert n("150 ohms") == "150Ω"
        assert n("150 ohm") == "150Ω"

    def test_metric_prefixes(self) -> None:
        from commands.jlcpcb_parts import JLCPCBPartsManager
        n = JLCPCBPartsManager._normalize_query_units
        assert n("1kohm") == "1kΩ"
        assert n("4.7Kohms") == "4.7kΩ"
        assert n("2.2Mohm") == "2.2MΩ"

    def test_capacitance_inductance(self) -> None:
        from commands.jlcpcb_parts import JLCPCBPartsManager
        n = JLCPCBPartsManager._normalize_query_units
        assert n("10uF 16V") == "10μF 16V"
        assert n("100uH 0805") == "100μH 0805"

    def test_no_unit_passthrough(self) -> None:
        from commands.jlcpcb_parts import JLCPCBPartsManager
        n = JLCPCBPartsManager._normalize_query_units
        assert n("BQ25895 IC") == "BQ25895 IC"
        assert n("0603 Resistor") == "0603 Resistor"

    def test_search_finds_omega_data_via_ascii_query(self, tmp_path: Path) -> None:
        from commands.jlcpcb_parts import JLCPCBPartsManager
        mgr = JLCPCBPartsManager(db_path=str(tmp_path / "test.db"))
        mgr.import_jlcsearch_parts([
            {"lcsc": 1, "description": "150Ω 75V 0603 Thick Film Resistor",
             "is_basic": True, "stock": 1000},
            {"lcsc": 2, "description": "10kΩ 0805 Resistor",
             "is_basic": False, "stock": 1000},
        ])
        # ASCII query must hit Unicode data (in_stock filter on by default,
        # so the fixtures set stock>0).
        results = mgr.search_parts(query="150ohms 0603")
        assert len(results) == 1
        assert results[0]["lcsc"] == "C1"
        results = mgr.search_parts(query="10kohm")
        assert len(results) == 1
        assert results[0]["lcsc"] == "C2"

    def test_search_baseline_with_omega_query_still_works(self, tmp_path: Path) -> None:
        """Sanity: queries already using Ω must continue to work."""
        from commands.jlcpcb_parts import JLCPCBPartsManager
        mgr = JLCPCBPartsManager(db_path=str(tmp_path / "test.db"))
        mgr.import_jlcsearch_parts([
            {"lcsc": 1, "description": "150Ω 75V 0603 Thick Film Resistor",
             "is_basic": True, "stock": 1000},
        ])
        results = mgr.search_parts(query="150Ω 0603")
        assert len(results) == 1


@pytest.mark.integration
class TestBackfillCategories:
    def test_backfill_only_touches_empty_category(self, tmp_path: Path) -> None:
        from commands.jlcpcb_parts import JLCPCBPartsManager
        mgr = JLCPCBPartsManager(db_path=str(tmp_path / "test.db"))

        # Insert rows directly bypassing import (simulates existing DB).
        cur = mgr.conn.cursor()
        cur.executemany(
            "INSERT INTO components (lcsc, category, description) VALUES (?, ?, ?)",
            [
                ("C1", "", "10kΩ 0603 Thick Film Resistor"),
                ("C2", "", "100uF MLCC Capacitor"),
                ("C3", "Existing", "10uF 0603 Capacitor"),  # already has category
                ("C4", "", "completely opaque part with no keywords"),  # won't match
            ],
        )
        mgr.conn.commit()

        result = mgr.backfill_categories()

        rows = dict(mgr.conn.execute("SELECT lcsc, category FROM components").fetchall())
        assert rows["C1"] == "Resistors"
        assert rows["C2"] == "Capacitors"
        assert rows["C3"] == "Existing", "must not overwrite existing values"
        assert rows["C4"] == ""  # no keywords matched, stays empty
        assert result["updated"] == 2  # C1 and C2
