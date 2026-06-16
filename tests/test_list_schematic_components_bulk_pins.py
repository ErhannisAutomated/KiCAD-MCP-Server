"""
Tests for the bulk-pin-location surface on list_schematic_components:
- filter.references restricts the result to a caller-supplied set of refs
- each pin entry carries angle alongside number/position/name

Replaces the previous "N round-trips to get_schematic_pin_locations" pattern.
"""

import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "python" / "templates" / "empty.kicad_sch"


def _place(loader, sch_path: Path, ref: str, x: float, y: float, rotation: float = 0) -> None:
    loader.create_component_instance(
        sch_path,
        library_name="Device",
        symbol_name="R",
        reference=ref,
        value="10k",
        x=x,
        y=y,
        rotation=rotation,
    )


@pytest.fixture
def populated_schematic(tmp_path):
    from commands.dynamic_symbol_loader import DynamicSymbolLoader

    sch = tmp_path / "bulk.kicad_sch"
    shutil.copy(TEMPLATE_PATH, sch)
    loader = DynamicSymbolLoader()
    _place(loader, sch, "R1", 50, 50, rotation=0)
    _place(loader, sch, "R2", 80, 50, rotation=90)
    _place(loader, sch, "R3", 110, 50, rotation=180)
    return sch


@pytest.mark.integration
class TestBulkPinLocationsViaListComponents:
    def _ki(self):
        from kicad_interface import KiCADInterface

        return KiCADInterface()

    def test_pins_include_angle(self, populated_schematic):
        result = self._ki()._handle_list_schematic_components(
            {"schematicPath": str(populated_schematic)}
        )
        assert result["success"] is True
        comps = {c["reference"]: c for c in result["components"]}
        for ref in ("R1", "R2", "R3"):
            assert ref in comps, f"missing {ref} in result"
            pins = comps[ref].get("pins") or []
            assert pins, f"{ref} has no pins"
            for pin in pins:
                assert "angle" in pin, f"{ref}/{pin.get('number')} pin missing angle"
                assert isinstance(pin["angle"], (int, float))

    def test_references_filter_restricts_to_listed_refs(self, populated_schematic):
        result = self._ki()._handle_list_schematic_components(
            {
                "schematicPath": str(populated_schematic),
                "filter": {"references": ["R1", "R3"]},
            }
        )
        assert result["success"] is True
        refs = {c["reference"] for c in result["components"]}
        assert refs == {"R1", "R3"}, f"unexpected refs: {refs}"

    def test_empty_references_list_is_treated_as_no_filter(self, populated_schematic):
        """An empty list is ambiguous; default to 'no filter' (return all)
        rather than 'return nothing', matching how an LLM would expect
        an unset filter to behave."""
        result = self._ki()._handle_list_schematic_components(
            {
                "schematicPath": str(populated_schematic),
                "filter": {"references": []},
            }
        )
        assert result["success"] is True
        refs = {c["reference"] for c in result["components"]}
        assert refs == {"R1", "R2", "R3"}

    def test_references_filter_composes_with_libid_filter(self, populated_schematic):
        result = self._ki()._handle_list_schematic_components(
            {
                "schematicPath": str(populated_schematic),
                "filter": {"references": ["R1", "R2"], "libId": "Device:R"},
            }
        )
        assert result["success"] is True
        refs = {c["reference"] for c in result["components"]}
        assert refs == {"R1", "R2"}
