"""Tests for the get_drc_violations filter logic and item-description
parser. The handler itself shells out to kicad-cli + pcbnew, so we test
the filter pipeline by mocking the on-disk violations file."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

# Stub pcbnew before importing design_rules.
sys.modules.setdefault("pcbnew", MagicMock())

from commands.design_rules import (  # noqa: E402
    DesignRuleCommands,
    _parse_item_description,
)


class TestParseItemDescription:
    def test_track_with_net_layer_length(self):
        out = _parse_item_description("Track [BAT+] on F.Cu, length 0.6500 mm")
        assert out == {"net": "BAT+", "layer": "F.Cu", "length_mm": 0.65, "ref": None}

    def test_track_with_multi_part_net(self):
        out = _parse_item_description("Track [USB_VBUS] on B.Cu, length 12.3 mm")
        assert out["net"] == "USB_VBUS"
        assert out["layer"] == "B.Cu"
        assert out["length_mm"] == 12.3

    def test_footprint_only(self):
        out = _parse_item_description("Footprint R27")
        assert out == {"net": None, "layer": None, "length_mm": None, "ref": "R27"}

    def test_pad_of_component(self):
        out = _parse_item_description("Pad 1 of C9")
        assert out["ref"] == "C9"
        assert out["net"] is None

    def test_empty(self):
        out = _parse_item_description("")
        assert out == {"net": None, "layer": None, "length_mm": None, "ref": None}

    def test_via_on_inner_layer(self):
        out = _parse_item_description("Via [GND] on In1.Cu")
        assert out["net"] == "GND"
        assert out["layer"] == "In1.Cu"


@pytest.fixture
def fake_report(tmp_path: Path) -> Path:
    """Write a representative violations file as run_drc would have."""
    report = tmp_path / "test_drc_violations.json"
    report.write_text(json.dumps({
        "violations": [
            {
                "type": "lib_footprint_mismatch",
                "severity": "warning",
                "message": "Footprint mismatch",
                "items": [
                    {"description": "Footprint R27", "pos": {"x": 88.0, "y": 90.0}, "uuid": "uuid-r27",
                     "net": None, "layer": None, "length_mm": None, "ref": "R27"}
                ],
                "location": {"x": 88.0, "y": 90.0, "unit": "mm"},
            },
            {
                "type": "silk_overlap",
                "severity": "warning",
                "message": "Silk overlap",
                "items": [],
                "location": {"x": 0, "y": 0, "unit": "mm"},
            },
            {
                "type": "shorting_items",
                "severity": "error",
                "message": "Items shorting two nets",
                "items": [
                    {"description": "Track [BAT+] on F.Cu, length 0.65 mm",
                     "pos": {"x": 75.0, "y": 82.8}, "uuid": "uuid-bat1",
                     "net": "BAT+", "layer": "F.Cu", "length_mm": 0.65, "ref": None}
                ],
                "location": {"x": 75.0, "y": 82.8, "unit": "mm"},
            },
            {
                "type": "unconnected_items",
                "severity": "error",
                "message": "Missing connection between items",
                "items": [
                    {"description": "Track [BAT+] on F.Cu, length 0.65 mm",
                     "pos": {"x": 75.0, "y": 82.8}, "uuid": "uuid-u1",
                     "net": "BAT+", "layer": "F.Cu", "length_mm": 0.65, "ref": None},
                    {"description": "Track [BAT+] on F.Cu, length 1.2 mm",
                     "pos": {"x": 65.0, "y": 92.1}, "uuid": "uuid-u2",
                     "net": "BAT+", "layer": "F.Cu", "length_mm": 1.2, "ref": None}
                ],
                "location": {"x": 75.0, "y": 82.8, "unit": "mm"},
            },
            {
                "type": "unconnected_items",
                "severity": "error",
                "message": "Missing connection between items",
                "items": [
                    {"description": "Pad 1 of U3", "pos": {"x": 50, "y": 50}, "uuid": "u3p1",
                     "net": "CC1", "layer": "F.Cu", "length_mm": None, "ref": "U3"}
                ],
                "location": {"x": 50, "y": 50, "unit": "mm"},
            },
        ]
    }))
    return report


class TestGetDrcViolationsFilters:
    """Use useCachedReport=True so we don't actually invoke kicad-cli."""

    def _setup_handler(self, fake_report: Path) -> DesignRuleCommands:
        # board.GetFileName() must point at a path whose
        # *_drc_violations.json sibling is our fixture.
        board = MagicMock()
        board.GetFileName.return_value = str(fake_report.parent / "test.kicad_pcb")
        return DesignRuleCommands(board)

    def test_default_returns_all_findings(self, fake_report: Path):
        h = self._setup_handler(fake_report)
        out = h.get_drc_violations({"useCachedReport": True})
        assert out["success"] is True
        assert out["total"] == 5
        assert out["summary"]["by_severity"] == {"error": 3, "warning": 2, "info": 0}
        assert out["summary"]["by_type"]["unconnected_items"] == 2

    def test_severity_filter(self, fake_report: Path):
        h = self._setup_handler(fake_report)
        out = h.get_drc_violations({"severity": "error", "useCachedReport": True})
        assert out["total"] == 3
        assert all(v["severity"] == "error" for v in out["violations"])

    def test_type_filter_string(self, fake_report: Path):
        h = self._setup_handler(fake_report)
        out = h.get_drc_violations({"type": "unconnected_items", "useCachedReport": True})
        assert out["total"] == 2
        assert all(v["type"] == "unconnected_items" for v in out["violations"])

    def test_type_filter_list(self, fake_report: Path):
        h = self._setup_handler(fake_report)
        out = h.get_drc_violations(
            {"type": ["shorting_items", "lib_footprint_mismatch"], "useCachedReport": True}
        )
        assert out["total"] == 2
        types = {v["type"] for v in out["violations"]}
        assert types == {"shorting_items", "lib_footprint_mismatch"}

    def test_net_filter(self, fake_report: Path):
        h = self._setup_handler(fake_report)
        out = h.get_drc_violations({"net": "BAT+", "useCachedReport": True})
        # shorting_items + 1st unconnected_items both have BAT+ items
        assert out["total"] == 2
        assert out["summary"]["by_type"] == {"shorting_items": 1, "unconnected_items": 1}

    def test_combined_filters(self, fake_report: Path):
        h = self._setup_handler(fake_report)
        out = h.get_drc_violations(
            {"severity": "error", "type": "unconnected_items", "net": "CC1", "useCachedReport": True}
        )
        assert out["total"] == 1
        assert out["violations"][0]["items"][0]["ref"] == "U3"

    def test_summary_only_drops_items(self, fake_report: Path):
        h = self._setup_handler(fake_report)
        out = h.get_drc_violations({"summaryOnly": True, "useCachedReport": True})
        assert out["total"] == 5
        assert "violations" not in out
        assert out["summary"]["by_severity"]["error"] == 3

    def test_filters_echoed_in_response(self, fake_report: Path):
        h = self._setup_handler(fake_report)
        out = h.get_drc_violations(
            {"severity": "error", "type": ["x", "y"], "net": "Z", "useCachedReport": True}
        )
        assert out["filters"]["severity"] == "error"
        assert set(out["filters"]["type"]) == {"x", "y"}
        assert out["filters"]["net"] == "Z"
