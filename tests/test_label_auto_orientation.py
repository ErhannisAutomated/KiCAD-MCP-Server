"""
Tests for the auto-orientation behavior in add_schematic_net_label.

When a caller supplies componentRef + pinNumber to snap a label to a pin
endpoint, the label orientation defaults to the pin's outward direction
(0=right, 90=up, 180=left, 270=down).  Reason: a label that always reads
rightward looks fine on a right-facing pin but overlaps the symbol body
on left/up/down-facing pins, which is exactly the readability complaint
that surfaced 2026-05-09.

Explicit `orientation=N` still wins; auto-orientation only fills in when
the caller didn't pass one.
"""
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

PYTHON_DIR = Path(__file__).parent.parent / "python"
TEMPLATES_DIR = PYTHON_DIR / "templates"
sys.path.insert(0, str(PYTHON_DIR))


@pytest.mark.integration
class TestLabelAutoOrientation:
    """End-to-end: place a known-pin-direction component, attach a label,
    inspect the resulting orientation."""

    def _make_sch_with_resistor(self, tmp: Path, rotation: int = 0) -> Path:
        """Drop a Device:R at (100, 100) with the given rotation.  Pin 1
        outward angle is 90 + rotation; pin 2 is 270 + rotation (mod 360)."""
        from commands.dynamic_symbol_loader import DynamicSymbolLoader

        sch = tmp / "test.kicad_sch"
        shutil.copy(TEMPLATES_DIR / "empty.kicad_sch", sch)
        DynamicSymbolLoader().add_component(
            sch, "Device", "R",
            reference="R1", value="10k",
            x=100, y=100, rotation=rotation,
        )
        return sch

    def test_pin_1_default_label_orientation_matches_pin_angle(self):
        """Device:R with rotation=0 has pin 1 facing UP (lib angle 270 + Y-flip).
        Auto-orientation should produce 90 (up)."""
        from kicad_interface import KiCADInterface

        with tempfile.TemporaryDirectory() as tmp:
            sch = self._make_sch_with_resistor(Path(tmp))
            iface = KiCADInterface()
            result = iface._handle_add_schematic_net_label({
                "schematicPath": str(sch),
                "netName": "VCC",
                "componentRef": "R1",
                "pinNumber": "1",
            })
            assert result["success"], result.get("message")
            # Pin 1 of an unrotated Device:R points UP (+y in screen = -y math).
            # Outward angle should be 90 (up).
            assert result["orientation"] == 90, (
                f"Expected orientation=90 for upward-facing pin, got "
                f"{result['orientation']}"
            )
            assert result.get("auto_orientation") is True

    def test_pin_2_default_label_orientation_matches_pin_angle(self):
        """Pin 2 faces DOWN → orientation 270."""
        from kicad_interface import KiCADInterface

        with tempfile.TemporaryDirectory() as tmp:
            sch = self._make_sch_with_resistor(Path(tmp))
            iface = KiCADInterface()
            result = iface._handle_add_schematic_net_label({
                "schematicPath": str(sch),
                "netName": "GND",
                "componentRef": "R1",
                "pinNumber": "2",
            })
            assert result["success"], result.get("message")
            assert result["orientation"] == 270, (
                f"Expected orientation=270 for downward-facing pin, got "
                f"{result['orientation']}"
            )

    def test_explicit_orientation_overrides_auto(self):
        """When the caller passes orientation=N, the auto behavior must
        defer to it — even if pin angle disagrees."""
        from kicad_interface import KiCADInterface

        with tempfile.TemporaryDirectory() as tmp:
            sch = self._make_sch_with_resistor(Path(tmp))
            iface = KiCADInterface()
            result = iface._handle_add_schematic_net_label({
                "schematicPath": str(sch),
                "netName": "VCC",
                "componentRef": "R1",
                "pinNumber": "1",
                "orientation": 0,  # explicit override; pin actually faces up (90)
            })
            assert result["success"]
            assert result["orientation"] == 0, (
                "Explicit orientation=0 was overridden by auto-orientation"
            )
            assert result.get("auto_orientation") is None

    def test_position_only_call_keeps_default_orientation_zero(self):
        """If caller provides position [x, y] but no componentRef, there's
        no pin angle to read — orientation stays at the legacy default of 0."""
        from kicad_interface import KiCADInterface

        with tempfile.TemporaryDirectory() as tmp:
            sch = Path(tmp) / "empty.kicad_sch"
            shutil.copy(TEMPLATES_DIR / "empty.kicad_sch", sch)
            iface = KiCADInterface()
            result = iface._handle_add_schematic_net_label({
                "schematicPath": str(sch),
                "netName": "FOO",
                "position": [50, 50],
            })
            assert result["success"]
            assert result["orientation"] == 0
            assert result.get("auto_orientation") is None
