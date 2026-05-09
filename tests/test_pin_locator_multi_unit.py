"""
Regression tests for PinLocator multi-unit pin lookup.

Bug surfaced 2026-05-09 building the autoplacer: when a schematic places
two units of the same multi-unit symbol (e.g. Q1 unit 1 + Q1 unit 2 of
a dual N-FET), PinLocator found the FIRST placed instance and returned
its `(at)` for *every* pin number on that reference — even the ones
that belong only to unit 2.  This tests the fix: the locator must
identify which unit owns each pin (via lib_symbols sub-symbol naming
`<base>_<unit>_<convert>`) and transform the pin against THAT unit's
placed instance.
"""
from __future__ import annotations

import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

PYTHON_DIR = Path(__file__).parent.parent / "python"
sys.path.insert(0, str(PYTHON_DIR))


# Synthetic two-unit symbol: unit 1 has pins "1", "2"; unit 2 has pins "3", "4".
# The lib pin local Y is +3.81 / -3.81 (matches Device:R style) so we can
# recognise where the world coord came from.  Each unit's placed instance is
# at a different (at).
def _make_multi_unit_sch(tmp: Path) -> Path:
    sch = tmp / "multi.kicad_sch"
    sch.write_text(textwrap.dedent("""\
        (kicad_sch (version 20250114) (generator "test")
          (uuid 11111111-2222-3333-4444-555555555555)
          (lib_symbols
            (symbol "Test:DualR" (pin_numbers hide) (pin_names (offset 0))
              (symbol "DualR_1_1"
                (pin passive line (at 0 3.81 270) (length 1.27)
                  (name "~" (effects (font (size 1.27 1.27))))
                  (number "1" (effects (font (size 1.27 1.27))))
                )
                (pin passive line (at 0 -3.81 90) (length 1.27)
                  (name "~" (effects (font (size 1.27 1.27))))
                  (number "2" (effects (font (size 1.27 1.27))))
                )
              )
              (symbol "DualR_2_1"
                (pin passive line (at 0 3.81 270) (length 1.27)
                  (name "~" (effects (font (size 1.27 1.27))))
                  (number "3" (effects (font (size 1.27 1.27))))
                )
                (pin passive line (at 0 -3.81 90) (length 1.27)
                  (name "~" (effects (font (size 1.27 1.27))))
                  (number "4" (effects (font (size 1.27 1.27))))
                )
              )
            )
          )
          (symbol (lib_id "Test:DualR") (at 100 100 0) (unit 1)
            (property "Reference" "Q1" (at 100 92 0))
            (property "Value" "DualR" (at 100 108 0))
            (instances (project "test" (path "/" (reference "Q1") (unit 1))))
          )
          (symbol (lib_id "Test:DualR") (at 200 150 0) (unit 2)
            (property "Reference" "Q1" (at 200 142 0))
            (property "Value" "DualR" (at 200 158 0))
            (instances (project "test" (path "/" (reference "Q1") (unit 2))))
          )
          (sheet_instances (path "/" (page "1")))
        )
    """))
    return sch


@pytest.mark.unit
class TestMultiUnitPinLocation:
    def test_unit1_pins_resolve_at_unit1_position(self):
        from commands.pin_locator import PinLocator

        with tempfile.TemporaryDirectory() as tmp:
            sch = _make_multi_unit_sch(Path(tmp))
            loc = PinLocator()

            # Unit 1 at (100, 100); pin 1 at lib (0, +3.81) → screen (0, -3.81)
            # → world (100, 100-3.81) = (100, 96.19).
            p1 = loc.get_pin_location(sch, "Q1", "1")
            assert p1 is not None
            assert p1[0] == pytest.approx(100.0)
            assert p1[1] == pytest.approx(96.19)
            # Pin 2 at lib (0, -3.81) → screen (0, +3.81) → world (100, 103.81).
            p2 = loc.get_pin_location(sch, "Q1", "2")
            assert p2 is not None
            assert p2[0] == pytest.approx(100.0)
            assert p2[1] == pytest.approx(103.81)

    def test_unit2_pins_resolve_at_unit2_position(self):
        """The bug: pin 3 (owned by unit 2) used to return unit 1's
        coords because PinLocator picked the first placed instance.
        """
        from commands.pin_locator import PinLocator

        with tempfile.TemporaryDirectory() as tmp:
            sch = _make_multi_unit_sch(Path(tmp))
            loc = PinLocator()

            # Unit 2 at (200, 150); pin 3 at lib (0, +3.81) → world (200, 146.19).
            p3 = loc.get_pin_location(sch, "Q1", "3")
            assert p3 is not None, "PinLocator returned None for unit-2 pin"
            assert p3[0] == pytest.approx(200.0), (
                f"pin 3 X={p3[0]}: PinLocator picked the wrong instance "
                f"(unit 1 is at x=100, unit 2 at x=200)"
            )
            assert p3[1] == pytest.approx(146.19), f"pin 3 Y wrong: {p3[1]}"
            # Pin 4 at lib (0, -3.81) → world (200, 153.81).
            p4 = loc.get_pin_location(sch, "Q1", "4")
            assert p4 is not None
            assert p4[0] == pytest.approx(200.0)
            assert p4[1] == pytest.approx(153.81)

    def test_get_all_symbol_pins_returns_unit_aware_coords(self):
        """get_all_symbol_pins iterates pin numbers; each one must
        resolve against its OWN unit's placement, not all against the
        first."""
        from commands.pin_locator import PinLocator

        with tempfile.TemporaryDirectory() as tmp:
            sch = _make_multi_unit_sch(Path(tmp))
            loc = PinLocator()

            all_pins = loc.get_all_symbol_pins(sch, "Q1")
            assert set(all_pins.keys()) >= {"1", "2", "3", "4"}
            assert all_pins["1"][0] == pytest.approx(100.0)
            assert all_pins["3"][0] == pytest.approx(200.0)

    def test_pin_angle_uses_owning_unit_rotation(self):
        """Place unit 1 at rotation 0 and unit 2 at rotation 90.
        Pin 3's outward angle must reflect unit 2's rotation, not
        unit 1's."""
        from commands.pin_locator import PinLocator

        with tempfile.TemporaryDirectory() as tmp:
            sch = Path(tmp) / "rot.kicad_sch"
            sch.write_text(textwrap.dedent("""\
                (kicad_sch (version 20250114) (generator "test")
                  (uuid 11111111-2222-3333-4444-555555555555)
                  (lib_symbols
                    (symbol "Test:DualR" (pin_numbers hide) (pin_names (offset 0))
                      (symbol "DualR_1_1"
                        (pin passive line (at 0 3.81 270) (length 1.27)
                          (name "~") (number "1"))
                      )
                      (symbol "DualR_2_1"
                        (pin passive line (at 0 3.81 270) (length 1.27)
                          (name "~") (number "3"))
                      )
                    )
                  )
                  (symbol (lib_id "Test:DualR") (at 100 100 0) (unit 1)
                    (property "Reference" "Q1" (at 100 92 0))
                    (instances (project "test" (path "/" (reference "Q1") (unit 1))))
                  )
                  (symbol (lib_id "Test:DualR") (at 200 150 90) (unit 2)
                    (property "Reference" "Q1" (at 200 142 0))
                    (instances (project "test" (path "/" (reference "Q1") (unit 2))))
                  )
                  (sheet_instances (path "/" (page "1")))
                )
            """))
            loc = PinLocator()

            a1 = loc.get_pin_angle(sch, "Q1", "1")
            a3 = loc.get_pin_angle(sch, "Q1", "3")
            assert a1 is not None and a3 is not None
            # With unit 2 rotated 90 vs unit 1's 0, the outward angles
            # must differ by 90°.  The actual values follow the rule
            # `(pin_def_angle + symbol_rotation + 180) % 360` from
            # get_pin_angle's implementation; we just assert they're not
            # the same — the bug returned identical coords/angles.
            assert (a3 - a1) % 360 == pytest.approx(90.0), (
                f"pin 1 angle={a1}, pin 3 angle={a3}; expected 90° offset "
                f"because the units have different rotations"
            )
