"""Unit tests for placement-constraint primitives.

Covers anchor parsing, rename propagation rewriting,
Schematic_Metadata singleton version-marker management, and
ground-net classification. These are pure-Python tests; no pcbnew
needed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.placement_constraints import (  # noqa: E402
    ANCHOR_PROPERTY,
    CONSTRAINT_VERSION,
    AnchorClause,
    _is_gnd,
    ensure_constraint_version,
    get_constraint_version,
    parse_anchor_value,
    rewrite_anchor_value,
)


class TestParseAnchorValue:
    def test_single_clause_with_pin(self):
        clauses, errors = parse_anchor_value("U1.10/within=3mm")
        assert errors == []
        assert clauses == [AnchorClause("U1", "10", 3.0)]

    def test_single_clause_no_pin(self):
        clauses, errors = parse_anchor_value("U1/within=8mm")
        assert errors == []
        assert clauses == [AnchorClause("U1", None, 8.0)]

    def test_decimal_distance(self):
        clauses, errors = parse_anchor_value("U1.10/within=1.5mm")
        assert errors == []
        assert clauses[0].max_dist_mm == 1.5

    def test_multiple_clauses(self):
        clauses, errors = parse_anchor_value("U1.10/within=3mm; U1.9/within=3mm")
        assert errors == []
        assert len(clauses) == 2
        assert clauses[0].target_ref == "U1" and clauses[0].target_pin == "10"
        assert clauses[1].target_ref == "U1" and clauses[1].target_pin == "9"

    def test_whitespace_tolerance(self):
        clauses, errors = parse_anchor_value("  U1.10  /  within = 3 mm  ")
        assert errors == []
        assert clauses == [AnchorClause("U1", "10", 3.0)]

    def test_empty_value(self):
        assert parse_anchor_value("") == ([], [])
        assert parse_anchor_value(None) == ([], [])

    def test_malformed_clause_collected(self):
        clauses, errors = parse_anchor_value("U1.10/within=3mm; garbage; U2/within=5mm")
        assert len(clauses) == 2
        assert errors == ["garbage"]

    def test_units_required(self):
        # No "mm" suffix -> reject
        clauses, errors = parse_anchor_value("U1.10/within=3")
        assert clauses == []
        assert errors == ["U1.10/within=3"]

    def test_str_roundtrip(self):
        a = AnchorClause("U1", "10", 3.0)
        assert str(a) == "U1.10/within=3.0mm"
        clauses, _ = parse_anchor_value(str(a))
        assert clauses == [a]


class TestRewriteAnchorValue:
    def test_simple_rename(self):
        new, replaced = rewrite_anchor_value("U1.10/within=3mm", {"U1": "U7"})
        assert new == "U7.10/within=3.0mm"
        assert replaced == ["U1"]

    def test_no_change_when_ref_absent(self):
        new, replaced = rewrite_anchor_value("U1.10/within=3mm", {"U9": "U99"})
        assert "U1.10" in new
        assert replaced == []

    def test_preserves_malformed_clauses(self):
        new, replaced = rewrite_anchor_value("U1.10/within=3mm; garbage", {"U1": "U2"})
        assert "U2.10" in new
        assert "garbage" in new
        assert replaced == ["U1"]

    def test_multiple_clauses(self):
        new, replaced = rewrite_anchor_value(
            "U1.10/within=3mm; U2/within=5mm", {"U1": "X1", "U2": "X2"}
        )
        assert "X1.10" in new
        assert "X2/" in new
        assert set(replaced) == {"U1", "U2"}

    def test_empty_mapping(self):
        new, replaced = rewrite_anchor_value("U1.10/within=3mm", {})
        assert new == "U1.10/within=3mm"
        assert replaced == []


class TestConstraintVersion:
    """As of #233, the canonical store for `mcp_constraint_version`
    is the Schematic_Metadata singleton in the .kicad_sch — the
    .kicad_pro path is just sugar that resolves to its sibling .sch.
    Tests therefore work against a real .kicad_sch fixture."""

    # Reuse the minimal-schematic fixture from test_schematic_metadata
    # rather than duplicating it. Imported lazily to keep this test
    # module's top-level imports unchanged.
    @staticmethod
    def _write_sch(tmp_path: Path, name: str = "p") -> Path:
        from tests.test_schematic_metadata import _MIN_SCH
        sch = tmp_path / f"{name}.kicad_sch"
        sch.write_text(_MIN_SCH, encoding="utf-8")
        return sch

    def test_round_trip(self, tmp_path: Path):
        sch = self._write_sch(tmp_path)
        proj = tmp_path / "p.kicad_pro"
        proj.write_text(json.dumps({"foo": "bar"}))

        # Accepts either .kicad_sch or .kicad_pro (sugar — maps to
        # the .sch sibling under the hood).
        assert get_constraint_version(proj) is None
        assert ensure_constraint_version(proj) is True
        assert get_constraint_version(proj) == CONSTRAINT_VERSION

        # Second call is a no-op (singleton already at the version).
        assert ensure_constraint_version(proj) is False

        # The .kicad_pro is untouched — the singleton owns the key.
        data = json.loads(proj.read_text())
        assert data == {"foo": "bar"}
        assert "mcp_constraint_version" not in data

        # The singleton in the .kicad_sch carries the value.
        from commands import schematic_metadata as sm
        md = sm.read_metadata(sch)
        assert md.get("mcp_constraint_version") == str(CONSTRAINT_VERSION)

    def test_missing_sch(self, tmp_path: Path):
        # No .kicad_sch sibling — both helpers gracefully no-op.
        assert get_constraint_version(tmp_path / "missing.kicad_pro") is None
        assert ensure_constraint_version(tmp_path / "missing.kicad_pro") is False

    def test_corrupt_sch(self, tmp_path: Path):
        sch = tmp_path / "p.kicad_sch"
        sch.write_text("(not valid s-expression")
        assert get_constraint_version(sch) is None
        # ensure_ may write a fresh singleton or fail — either is fine,
        # just don't crash on corrupt input.
        try:
            ensure_constraint_version(sch)
        except Exception as e:
            pytest.fail(f"corrupt sch should not raise: {e}")


class TestIsGnd:
    @pytest.mark.parametrize("net,expected", [
        ("GND", True),
        ("VSS", True),
        ("AGND", True),
        ("DGND", True),
        ("PGND", True),
        ("GND_ANALOG", True),
        ("DIG_GND", True),
        ("CHIP_VSS", True),
        ("BAT+", False),
        ("V12_OUT", False),
        ("CC1", False),
        ("", False),
    ])
    def test_classification(self, net, expected):
        assert _is_gnd(net) is expected


def test_anchor_property_name_constant():
    # Locked-in v1 contract — changing this requires a constraint
    # version bump.
    assert ANCHOR_PROPERTY == "Placement_Anchor"
    # v2 added the mcp_spring_classes section (load + bootstrap I/O).
    assert CONSTRAINT_VERSION == 2
