"""
Tests for the order_by parameter on JLCPCBPartsManager.search_parts.

The point of order_by="stock_desc" (the default) is to surface high-stock parts
first as a proxy for ongoing availability — it's the standing convention captured
in the JLCPCB part-selection memory. Prior to this change, the SQL had no
ORDER BY at all, so result order was implementation-defined and the popular
"sort by stock desc, pick first relevant" workflow could not be expressed
through this tool.
"""

import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.jlcpcb_parts import JLCPCBPartsManager


def _make_manager_with_parts(tmp_path, parts):
    """Build a manager backed by a fresh SQLite DB pre-populated with `parts`."""
    db_path = tmp_path / "jlcpcb_test.db"
    manager = JLCPCBPartsManager(db_path=str(db_path))
    cursor = manager.conn.cursor()
    now = int(time.time())
    for p in parts:
        cursor.execute(
            "INSERT INTO components (lcsc, category, subcategory, mfr_part, package, "
            "solder_joints, manufacturer, library_type, description, datasheet, stock, "
            "price_json, last_updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                p["lcsc"],
                p.get("category", "Resistors"),
                p.get("subcategory", "Chip Resistor - Surface Mount"),
                p.get("mfr_part", f"MPN-{p['lcsc']}"),
                p.get("package", "0603"),
                2,
                p.get("manufacturer", "Generic"),
                p.get("library_type", "Basic"),
                p.get("description", "10kOhms 0603"),
                "",
                p["stock"],
                "[]",
                now,
            ),
        )
        # Mirror into FTS so query-based searches still work if used elsewhere.
        cursor.execute(
            "INSERT INTO components_fts (lcsc, description, mfr_part, manufacturer) "
            "VALUES (?, ?, ?, ?)",
            (
                p["lcsc"],
                p.get("description", "10kOhms 0603"),
                p.get("mfr_part", f"MPN-{p['lcsc']}"),
                p.get("manufacturer", "Generic"),
            ),
        )
    manager.conn.commit()
    return manager


@pytest.fixture
def manager(tmp_path):
    parts = [
        {"lcsc": "C001", "stock": 50},
        {"lcsc": "C002", "stock": 5000},
        {"lcsc": "C003", "stock": 100},
        {"lcsc": "C004", "stock": 1},
    ]
    m = _make_manager_with_parts(tmp_path, parts)
    yield m
    m.conn.close()


@pytest.mark.unit
class TestSearchOrderBy:
    def test_default_is_stock_desc(self, manager):
        results = manager.search_parts(category="Resistors")
        stocks = [r["stock"] for r in results]
        assert stocks == sorted(stocks, reverse=True)
        assert results[0]["lcsc"] == "C002"

    def test_stock_desc_explicit(self, manager):
        results = manager.search_parts(category="Resistors", order_by="stock_desc")
        assert [r["lcsc"] for r in results] == ["C002", "C003", "C001", "C004"]

    def test_stock_asc(self, manager):
        results = manager.search_parts(category="Resistors", order_by="stock_asc")
        assert [r["lcsc"] for r in results] == ["C004", "C001", "C003", "C002"]

    def test_order_by_none_does_not_apply_clause(self, manager):
        # We can't assert a particular order with "none", but it must not error
        # and must return all four parts.
        results = manager.search_parts(category="Resistors", order_by="none")
        assert sorted(r["lcsc"] for r in results) == ["C001", "C002", "C003", "C004"]

    def test_invalid_order_by_raises_value_error(self, manager):
        with pytest.raises(ValueError, match="Invalid order_by"):
            manager.search_parts(category="Resistors", order_by="stock; DROP TABLE components--")

    def test_in_stock_filter_combined_with_order_by(self, tmp_path):
        # stock=0 should be excluded by the default in_stock=True filter; remaining
        # rows should still be sorted by stock DESC.
        parts = [
            {"lcsc": "C100", "stock": 0},
            {"lcsc": "C101", "stock": 10},
            {"lcsc": "C102", "stock": 200},
        ]
        m = _make_manager_with_parts(tmp_path, parts)
        try:
            results = m.search_parts(category="Resistors")
            assert [r["lcsc"] for r in results] == ["C102", "C101"]
        finally:
            m.conn.close()
