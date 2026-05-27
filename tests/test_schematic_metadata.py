"""Tests for the Schematic_Metadata singleton helper (#230).

Integration-only because the helper does real schematic I/O. The
test harness's conftest stubs pcbnew, but `sexpdata` is a real
import so these tests do parse and serialise actual .kicad_sch
content.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from commands import schematic_metadata as sm  # noqa: E402


# A minimal kicad_sch text for the singleton tests — single sheet,
# one lib_symbols entry (empty), one component instance. Avoids
# depending on a fixture project on disk so the test is hermetic.
_MIN_SCH = '''(kicad_sch
\t(version 20250114)
\t(generator "eeschema")
\t(generator_version "9.0")
\t(uuid "11111111-2222-3333-4444-555555555555")
\t(paper "A4")
\t(lib_symbols
\t\t(symbol "Device:R"
\t\t\t(pin_names
\t\t\t\t(offset 0)
\t\t\t)
\t\t\t(exclude_from_sim no)
\t\t\t(in_bom yes)
\t\t\t(on_board yes)
\t\t\t(property "Reference" "R"
\t\t\t\t(at 2.032 0 90)
\t\t\t\t(effects
\t\t\t\t\t(font
\t\t\t\t\t\t(size 1.27 1.27)
\t\t\t\t\t)
\t\t\t\t)
\t\t\t)
\t\t\t(property "Value" "R"
\t\t\t\t(at 0 0 90)
\t\t\t\t(effects
\t\t\t\t\t(font
\t\t\t\t\t\t(size 1.27 1.27)
\t\t\t\t\t)
\t\t\t\t)
\t\t\t)
\t\t)
\t)
\t(embedded_fonts no)
)
'''


@pytest.fixture()
def sch_path(tmp_path: Path) -> Path:
    p = tmp_path / "test.kicad_sch"
    p.write_text(_MIN_SCH, encoding="utf-8")
    return p


@pytest.fixture()
def pro_path(tmp_path: Path) -> Path:
    p = tmp_path / "test.kicad_pro"
    p.write_text(json.dumps({"meta": {}, "net_settings": {}}), encoding="utf-8")
    return p


class TestSingletonReadWrite:
    def test_no_singletons_on_clean_schematic(self, sch_path):
        assert sm.find_singletons(sch_path) == []
        assert sm.read_metadata(sch_path) == {}

    def test_write_creates_singleton_on_first_call(self, sch_path):
        r = sm.write_metadata_key(sch_path, "mcp_constraint_version", 2)
        assert r["success"], r
        assert r["created"]
        assert r["reference"] == "META1"

    def test_round_trip_read_after_write(self, sch_path):
        sm.write_metadata_key(sch_path, "mcp_constraint_version", 2)
        md = sm.read_metadata(sch_path)
        assert md == {"mcp_constraint_version": "2"}

    def test_json_value_round_trips(self, sch_path):
        payload = {
            "classes": {"DECOUPLING": {"spring_k": 5.0}},
            "nets": {"GND": "PLANE"},
        }
        sm.write_metadata_key(sch_path, "mcp_spring_classes", payload)
        parsed = sm.read_metadata_json(sch_path, "mcp_spring_classes")
        assert parsed == payload

    def test_second_write_updates_existing(self, sch_path):
        sm.write_metadata_key(sch_path, "mcp_constraint_version", 2)
        r2 = sm.write_metadata_key(sch_path, "mcp_constraint_version", 3)
        assert not r2["created"]
        assert sm.read_metadata(sch_path)["mcp_constraint_version"] == "3"
        # still exactly one singleton
        assert len(sm.find_singletons(sch_path)) == 1

    def test_reserved_keys_rejected(self, sch_path):
        for reserved in ("Reference", "Value", "Schematic_Metadata_Marker"):
            r = sm.write_metadata_key(sch_path, reserved, "x")
            assert not r["success"]
            assert "reserved" in r["errorDetails"]


class TestPrimitiveAndJsonValues:
    def test_primitive_value_returned_as_string(self, sch_path):
        sm.write_metadata_key(sch_path, "mcp_constraint_version", 7)
        md = sm.read_metadata(sch_path)
        assert md["mcp_constraint_version"] == "7"

    def test_json_value_parsed_with_read_metadata_json(self, sch_path):
        payload = {"k": [1, 2, 3]}
        sm.write_metadata_key(sch_path, "mcp_test", payload)
        assert sm.read_metadata_json(sch_path, "mcp_test") == payload

    def test_read_metadata_json_returns_none_for_missing(self, sch_path):
        assert sm.read_metadata_json(sch_path, "mcp_missing") is None
