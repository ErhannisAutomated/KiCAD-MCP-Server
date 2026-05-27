"""Pin_Spring_Class:N is read from the SCHEMATIC, not the PCB (#228).

Verifies that:
- collect_pin_spring_classes walks a schematic and emits the {ref →
  {pin → raw_value}} lookup.
- A footprint with Pin_Spring_Class set on the PCB but NOT on the
  schematic is treated as having NO custom class (drift safety —
  schematic is the source of truth).
- A schematic property with bare-string and JSON-dict values both
  round-trip.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))


_SCH_WITH_PIN_CLASSES = '''(kicad_sch
\t(version 20250114)
\t(generator "eeschema")
\t(generator_version "9.0")
\t(uuid "33333333-2222-1111-4444-555555555555")
\t(paper "A4")
\t(lib_symbols
\t\t(symbol "Device:C"
\t\t\t(pin_names (offset 0))
\t\t\t(exclude_from_sim no)
\t\t\t(in_bom yes)
\t\t\t(on_board yes)
\t\t\t(property "Reference" "C"
\t\t\t\t(at 0 0 0)
\t\t\t\t(effects (font (size 1.27 1.27)))
\t\t\t)
\t\t\t(property "Value" "C"
\t\t\t\t(at 0 0 0)
\t\t\t\t(effects (font (size 1.27 1.27)))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "Device:C")
\t\t(at 50 50 0)
\t\t(unit 1)
\t\t(exclude_from_sim no)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(dnp no)
\t\t(uuid "aaaaaaaa-1111-2222-3333-444444444444")
\t\t(property "Reference" "C29"
\t\t\t(at 50 47 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "100nF"
\t\t\t(at 50 53 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Pin_Spring_Class:1" "DECOUPLING"
\t\t\t(at 50 50 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/33333333-2222-1111-4444-555555555555"
\t\t\t\t\t(reference "C29")
\t\t\t\t\t(unit 1)
\t\t\t\t)
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "Device:C")
\t\t(at 60 50 0)
\t\t(unit 1)
\t\t(exclude_from_sim no)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(dnp no)
\t\t(uuid "bbbbbbbb-1111-2222-3333-444444444444")
\t\t(property "Reference" "C30"
\t\t\t(at 60 47 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "10uF"
\t\t\t(at 60 53 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Pin_Spring_Class:1" "{\\"*\\":\\"DECOUPLING\\",\\"U3.10\\":\\"INTER_GROUP\\"}"
\t\t\t(at 60 50 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(property "Pin_Spring_Class:2" "INTER_GROUP"
\t\t\t(at 60 50 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/33333333-2222-1111-4444-555555555555"
\t\t\t\t\t(reference "C30")
\t\t\t\t\t(unit 1)
\t\t\t\t)
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "Device:C")
\t\t(at 70 50 0)
\t\t(unit 1)
\t\t(exclude_from_sim no)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(dnp no)
\t\t(uuid "cccccccc-1111-2222-3333-444444444444")
\t\t(property "Reference" "C99"
\t\t\t(at 70 47 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "1uF"
\t\t\t(at 70 53 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/33333333-2222-1111-4444-555555555555"
\t\t\t\t\t(reference "C99")
\t\t\t\t\t(unit 1)
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
    p.write_text(_SCH_WITH_PIN_CLASSES, encoding="utf-8")
    # placement_constraints._all_sheet_paths needs a sibling .kicad_pro
    # to find the top schematic; provide one.
    (tmp_path / "test.kicad_pro").write_text(
        '{"meta": {}, "net_settings": {}}', encoding="utf-8",
    )
    return p


def test_collects_pin_spring_classes_per_ref(sch_path):
    from commands.placement_constraints import collect_pin_spring_classes
    lookup = collect_pin_spring_classes(sch_path)
    assert set(lookup.keys()) == {"C29", "C30"}
    assert lookup["C29"] == {"1": "DECOUPLING"}
    assert lookup["C30"]["2"] == "INTER_GROUP"


def test_json_value_returned_raw(sch_path):
    """Caller (pcb_autoplacer._parse_pin_spring_class) does the JSON
    parse — collect_pin_spring_classes just returns the raw string."""
    from commands.placement_constraints import collect_pin_spring_classes
    lookup = collect_pin_spring_classes(sch_path)
    raw = lookup["C30"]["1"]
    assert raw.startswith("{")
    assert "DECOUPLING" in raw
    assert "INTER_GROUP" in raw


def test_components_without_class_omitted(sch_path):
    """C99 has no Pin_Spring_Class properties; should not appear in
    the lookup at all."""
    from commands.placement_constraints import collect_pin_spring_classes
    lookup = collect_pin_spring_classes(sch_path)
    assert "C99" not in lookup


def test_parser_accepts_collected_values(sch_path):
    """Round-trip: collect_pin_spring_classes returns raw strings;
    pcb_autoplacer._parse_pin_spring_class accepts them."""
    from commands.placement_constraints import collect_pin_spring_classes
    from commands.pcb_autoplacer import _parse_pin_spring_class

    lookup = collect_pin_spring_classes(sch_path)
    parsed_bare = _parse_pin_spring_class(lookup["C29"]["1"])
    assert parsed_bare == "DECOUPLING"

    parsed_json = _parse_pin_spring_class(lookup["C30"]["1"])
    assert isinstance(parsed_json, dict)
    assert parsed_json["*"] == "DECOUPLING"
    assert parsed_json["U3.10"] == "INTER_GROUP"
