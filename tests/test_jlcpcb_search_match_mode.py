"""
Tests for search_parts(match_mode=...) — AND / OR / auto matching.

- "and"  : every query token must match (legacy precise behaviour).
- "or"   : match ANY token, re-ranked by DISTINCT-term coverage then stock.
- "auto" : AND first; fall back to OR only when AND finds nothing.

Parts are loaded via import_jlcsearch_parts so the external-content FTS index
is populated (a bare INSERT into `components` would not be searchable).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.jlcpcb_parts import JLCPCBPartsManager


def _mgr(tmp_path):
    m = JLCPCBPartsManager(db_path=str(tmp_path / "mm.db"))
    m.import_jlcsearch_parts(
        [
            # matches buck + boost + converter  (cov 3), low stock
            {"lcsc": 1, "description": "Buck-Boost DC-DC Converter IC",
             "is_basic": True, "stock": 100},
            # matches boost + converter         (cov 2), high stock
            {"lcsc": 2, "description": "Boost Converter IC",
             "is_basic": False, "stock": 5000},
            # matches none of buck/boost/converter; has "regulator"
            {"lcsc": 3, "description": "Linear Regulator LDO",
             "is_basic": False, "stock": 9000},
        ]
    )
    return m


def _lcscs(rows):
    return [r["lcsc"] for r in rows]


def test_and_requires_all_terms(tmp_path):
    rows = _mgr(tmp_path).search_parts(query="buck boost converter", match_mode="and")
    assert _lcscs(rows) == ["C1"]  # only the part with all three terms


def test_and_no_fallback_when_empty(tmp_path):
    # No single part has both "buck" and "regulator" → strict AND returns nothing.
    rows = _mgr(tmp_path).search_parts(query="buck regulator", match_mode="and")
    assert rows == []


def test_or_matches_any_ranked_by_coverage(tmp_path):
    rows = _mgr(tmp_path).search_parts(query="buck boost converter", match_mode="or")
    # P1 (cov 3) outranks P2 (cov 2) despite P2's much higher stock.
    assert _lcscs(rows) == ["C1", "C2"]


def test_or_coverage_tie_breaks_on_stock(tmp_path):
    # "buck" hits P1 only, "regulator" hits P3 only → both cov 1 → higher stock first.
    rows = _mgr(tmp_path).search_parts(query="buck regulator", match_mode="or")
    assert _lcscs(rows) == ["C3", "C1"]  # C3 stock 9000 > C1 stock 100


def test_auto_uses_and_when_it_has_hits(tmp_path):
    rows, meta = _mgr(tmp_path).search_parts(
        query="buck boost converter", match_mode="auto", return_meta=True
    )
    assert meta["match_mode_used"] == "and"
    assert _lcscs(rows) == ["C1"]


def test_auto_falls_back_to_or(tmp_path):
    rows, meta = _mgr(tmp_path).search_parts(
        query="buck regulator", match_mode="auto", return_meta=True
    )
    assert meta["match_mode_used"] == "or"
    assert _lcscs(rows) == ["C3", "C1"]


def test_no_query_reports_mode_none(tmp_path):
    rows, meta = _mgr(tmp_path).search_parts(
        category="Power Management", return_meta=True
    )
    assert meta["match_mode_used"] == "none"


def test_invalid_match_mode_raises(tmp_path):
    with pytest.raises(ValueError):
        _mgr(tmp_path).search_parts(query="buck", match_mode="xor")


def test_return_meta_false_returns_plain_list(tmp_path):
    rows = _mgr(tmp_path).search_parts(query="buck boost converter")
    assert isinstance(rows, list)
    assert _lcscs(rows) == ["C1"]  # auto → AND hit
