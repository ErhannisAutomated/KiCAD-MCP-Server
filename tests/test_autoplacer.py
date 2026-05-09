"""
Unit tests for the schematic autoplacer.

The placer is a force-directed model that keeps state across MCP tool
calls (load → iterate*→ apply).  These tests cover the data model,
force computation invariants, and round-trip writes.

End-to-end placement quality is judged by hand on the real
power_module sheets; this file just verifies the wiring is correct
and the math doesn't NaN out.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

PYTHON_DIR = Path(__file__).parent.parent / "python"
TEMPLATES_DIR = PYTHON_DIR / "templates"
sys.path.insert(0, str(PYTHON_DIR))


# Two horizontal resistors face-to-face on net "SIG" with a label at one pin.
def _make_two_r_with_net(tmp: Path, net_label_at_r1_pin1: bool = True) -> Path:
    sch = tmp / "two_r.kicad_sch"
    R_LIB = textwrap.dedent("""\
        (lib_symbols
          (symbol "Device:R" (pin_numbers hide) (pin_names (offset 0))
            (symbol "R_1_1"
              (pin passive line (at 0 3.81 270) (length 1.27)
                (name "~" (effects (font (size 1.27 1.27))))
                (number "1" (effects (font (size 1.27 1.27))))
              )
              (pin passive line (at 0 -3.81 90) (length 1.27)
                (name "~" (effects (font (size 1.27 1.27))))
                (number "2" (effects (font (size 1.27 1.27))))
              )
            )
          )
        )
    """)
    label_line = '(label "SIG" (at 96.19 100 0) (effects (font (size 1.27 1.27))))' if net_label_at_r1_pin1 else ""
    body = textwrap.dedent(f"""\
        (kicad_sch (version 20250114) (generator "test")
          (uuid 11111111-2222-3333-4444-555555555555)
          {R_LIB}
          (symbol (lib_id "Device:R") (at 100 100 90) (unit 1)
            (property "Reference" "R1" (at 100 100 0))
            (property "Value" "10k" (at 100 100 0))
            (instances (project "test" (path "/" (reference "R1") (unit 1))))
          )
          (symbol (lib_id "Device:R") (at 130 100 90) (unit 1)
            (property "Reference" "R2" (at 130 100 0))
            (property "Value" "10k" (at 130 100 0))
            (instances (project "test" (path "/" (reference "R2") (unit 1))))
          )
          {label_line}
          (sheet_instances (path "/" (page "1")))
        )
    """)
    sch.write_text(body)
    return sch


@pytest.mark.unit
class TestLoad:
    def test_load_single_unit_components(self):
        from commands.autoplacer import load_session

        with tempfile.TemporaryDirectory() as tmp:
            sch = _make_two_r_with_net(Path(tmp))
            sess = load_session(sch)
            # Single-unit components: key is ref__u1
            assert "R1__u1" in sess.components
            assert "R2__u1" in sess.components
            assert not sess.components["R1__u1"].pinned

    def test_load_multi_unit_components_each_get_own_node(self):
        """Multi-unit symbols (e.g. Q1 unit 1 + Q1 unit 2) must
        produce TWO Component nodes so each placed unit moves
        independently and its pins resolve at the right world coord.
        """
        from commands.autoplacer import load_session

        with tempfile.TemporaryDirectory() as tmp:
            sch = Path(tmp) / "multi.kicad_sch"
            # Synthetic multi-unit symbol with disjoint pin numbers per unit:
            # unit 1 has pins 1, 2; unit 2 has pins 3, 4.
            sch.write_text(textwrap.dedent("""\
                (kicad_sch (version 20250114) (generator "test")
                  (uuid aaaa-bbbb)
                  (lib_symbols
                    (symbol "Test:DualR" (pin_numbers hide) (pin_names (offset 0))
                      (symbol "DualR_1_1"
                        (pin passive line (at 0 3.81 270) (length 1.27)
                          (name "~") (number "1"))
                        (pin passive line (at 0 -3.81 90) (length 1.27)
                          (name "~") (number "2"))
                      )
                      (symbol "DualR_2_1"
                        (pin passive line (at 0 3.81 270) (length 1.27)
                          (name "~") (number "3"))
                        (pin passive line (at 0 -3.81 90) (length 1.27)
                          (name "~") (number "4"))
                      )
                    )
                  )
                  (symbol (lib_id "Test:DualR") (at 100 100 0) (unit 1)
                    (property "Reference" "Q1" (at 100 100 0))
                    (instances (project "test" (path "/" (reference "Q1") (unit 1))))
                  )
                  (symbol (lib_id "Test:DualR") (at 150 100 0) (unit 2)
                    (property "Reference" "Q1" (at 150 100 0))
                    (instances (project "test" (path "/" (reference "Q1") (unit 2))))
                  )
                  (sheet_instances (path "/" (page "1")))
                )
            """))
            sess = load_session(sch)
            assert "Q1__u1" in sess.components
            assert "Q1__u2" in sess.components
            u1 = sess.components["Q1__u1"]
            u2 = sess.components["Q1__u2"]
            # Each unit only owns its own pin numbers.
            assert set(u1.pins.keys()) == {"1", "2"}
            assert set(u2.pins.keys()) == {"3", "4"}
            # And they sit at distinct positions (placer can move them
            # independently).
            assert u1.x != u2.x

    def test_load_connectors_are_pinned(self):
        from commands.autoplacer import load_session

        with tempfile.TemporaryDirectory() as tmp:
            sch = Path(tmp) / "j.kicad_sch"
            sch.write_text(textwrap.dedent("""\
                (kicad_sch (version 20250114) (generator "test")
                  (uuid aaaa-bbbb)
                  (lib_symbols
                    (symbol "Connector_Generic:Conn_01x02" (pin_numbers hide) (pin_names hide)
                      (symbol "Conn_01x02_1_1"
                        (pin passive line (at 0 0 180) (length 2.54)
                          (name "Pin_1") (number "1")
                        )
                      )
                    )
                  )
                  (symbol (lib_id "Connector_Generic:Conn_01x02") (at 100 100 0) (unit 1)
                    (property "Reference" "J1" (at 100 100 0))
                    (instances (project "test" (path "/" (reference "J1") (unit 1))))
                  )
                  (sheet_instances (path "/" (page "1")))
                )
            """))
            sess = load_session(sch)
            assert sess.components["J1__u1"].pinned is True


@pytest.mark.unit
class TestForceMath:
    def test_repulsion_pushes_apart(self):
        from commands.autoplacer import _component_pair_force, Component

        a = Component("A", 1, "Device:R", x=100, y=100, rotation=0,
                      mirror_x=False, mirror_y=False)
        b = Component("B", 1, "Device:R", x=110, y=100, rotation=0,
                      mirror_x=False, mirror_y=False)
        # Force on A from B: should point away from B (i.e. -x direction).
        fx, fy = _component_pair_force(a, b, k=100.0)
        assert fx < 0, f"expected -x repulsion, got fx={fx}"

    def test_attraction_pulls_together(self):
        from commands.autoplacer import _attractive_force, Component

        a = Component("A", 1, "Device:R", x=100, y=100, rotation=0,
                      mirror_x=False, mirror_y=False)
        b = Component("B", 1, "Device:R", x=110, y=100, rotation=0,
                      mirror_x=False, mirror_y=False)
        # Force on A from edge to B: should point toward B (+x).
        fx, fy = _attractive_force(a, b, k=1.0)
        assert fx > 0, f"expected +x attraction, got fx={fx}"


@pytest.mark.unit
class TestIterate:
    def test_iterate_doesnt_explode(self):
        from commands.autoplacer import load_session, iterate

        with tempfile.TemporaryDirectory() as tmp:
            sch = _make_two_r_with_net(Path(tmp))
            sess = load_session(sch)
            for _ in range(20):
                r = iterate(sess, 1)
            assert r["iteration"] == 20
            # Positions are finite floats
            for c in sess.components.values():
                assert -10000 < c.x < 10000
                assert -10000 < c.y < 10000


@pytest.mark.unit
class TestRoundTrip:
    def test_apply_writes_updated_positions(self):
        from commands.autoplacer import load_session, apply_to_schematic

        with tempfile.TemporaryDirectory() as tmp:
            sch = _make_two_r_with_net(Path(tmp))
            sess = load_session(sch)
            # Force R1 to a new position
            sess.components["R1__u1"].x = 200.0
            sess.components["R1__u1"].y = 50.0
            apply_to_schematic(sess, target_path=None)
            text = sch.read_text()
            assert "200" in text and "50" in text

    def test_apply_translates_property_at_with_symbol(self):
        """When the symbol's (at) moves by (dx, dy), every property's
        (at) should follow — otherwise the Reference/Value text labels
        get left behind in the original position."""
        from commands.autoplacer import load_session, apply_to_schematic
        import re

        with tempfile.TemporaryDirectory() as tmp:
            sch = _make_two_r_with_net(Path(tmp))
            # R1 starts at (100, 100); property "Reference" at (100, 100, 0).
            sess = load_session(sch)
            sess.components["R1__u1"].x = 200.0
            sess.components["R1__u1"].y = 100.0
            apply_to_schematic(sess, target_path=None)
            text = sch.read_text()
            # The Reference property's (at) should now be at x=200
            # (translated by +100 from original 100).
            m = re.search(r'\(property\s+"Reference"\s+"R1"\s+\(at\s+([\d.-]+)\s+([\d.-]+)', text)
            assert m, "Reference property block not found"
            px = float(m.group(1))
            assert abs(px - 200.0) < 0.5, (
                f"Reference text x={px}, expected ~200 (followed symbol translation)"
            )

    def test_apply_strips_wires_and_labels(self):
        from commands.autoplacer import load_session, apply_to_schematic

        with tempfile.TemporaryDirectory() as tmp:
            sch = _make_two_r_with_net(Path(tmp), net_label_at_r1_pin1=True)
            assert '(label "SIG"' in sch.read_text()  # sanity
            sess = load_session(sch)
            apply_to_schematic(sess, target_path=None, strip_connections=True)
            assert '(label "SIG"' not in sch.read_text()
