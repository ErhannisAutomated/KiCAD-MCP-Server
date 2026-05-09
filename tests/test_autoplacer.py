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
            assert "R1" in sess.components
            assert "R2" in sess.components
            assert not sess.components["R1"].pinned

    def test_load_multi_unit_pins_are_marked(self):
        """Multi-unit symbols (multiple placed blocks sharing a ref) must
        be marked pinned so the placer doesn't drag both units onto
        each other."""
        from commands.autoplacer import load_session

        # Build a synthetic multi-unit schematic by repeating a Device:R
        # block with the same Reference (matching what KiCad does for
        # FDS9926A's two N-FET units).
        with tempfile.TemporaryDirectory() as tmp:
            sch = Path(tmp) / "multi.kicad_sch"
            R_LIB = textwrap.dedent("""\
                (lib_symbols
                  (symbol "Device:R" (pin_numbers hide) (pin_names (offset 0))
                    (symbol "R_1_1"
                      (pin passive line (at 0 3.81 270) (length 1.27)
                        (name "~" (effects (font (size 1.27 1.27))))
                        (number "1" (effects (font (size 1.27 1.27))))
                      )
                    )
                  )
                )
            """)
            sch.write_text(textwrap.dedent(f"""\
                (kicad_sch (version 20250114) (generator "test")
                  (uuid aaaa-bbbb)
                  {R_LIB}
                  (symbol (lib_id "Device:R") (at 100 100 0) (unit 1)
                    (property "Reference" "Q1" (at 100 100 0))
                    (instances (project "test" (path "/" (reference "Q1") (unit 1))))
                  )
                  (symbol (lib_id "Device:R") (at 150 100 0) (unit 2)
                    (property "Reference" "Q1" (at 150 100 0))
                    (instances (project "test" (path "/" (reference "Q1") (unit 2))))
                  )
                  (sheet_instances (path "/" (page "1")))
                )
            """))
            sess = load_session(sch)
            assert "Q1" in sess.components
            assert sess.components["Q1"].pinned is True

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
            assert sess.components["J1"].pinned is True


@pytest.mark.unit
class TestForceMath:
    def test_repulsion_pushes_apart(self):
        from commands.autoplacer import _component_pair_force, Component

        a = Component("A", "Device:R", x=100, y=100, rotation=0,
                      mirror_x=False, mirror_y=False)
        b = Component("B", "Device:R", x=110, y=100, rotation=0,
                      mirror_x=False, mirror_y=False)
        # Force on A from B: should point away from B (i.e. -x direction).
        fx, fy = _component_pair_force(a, b, k=100.0)
        assert fx < 0, f"expected -x repulsion, got fx={fx}"

    def test_attraction_pulls_together(self):
        from commands.autoplacer import _attractive_force, Component

        a = Component("A", "Device:R", x=100, y=100, rotation=0,
                      mirror_x=False, mirror_y=False)
        b = Component("B", "Device:R", x=110, y=100, rotation=0,
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
            sess.components["R1"].x = 200.0
            sess.components["R1"].y = 50.0
            apply_to_schematic(sess, target_path=None)
            text = sch.read_text()
            assert "200" in text and "50" in text

    def test_apply_strips_wires_and_labels(self):
        from commands.autoplacer import load_session, apply_to_schematic

        with tempfile.TemporaryDirectory() as tmp:
            sch = _make_two_r_with_net(Path(tmp), net_label_at_r1_pin1=True)
            assert '(label "SIG"' in sch.read_text()  # sanity
            sess = load_session(sch)
            apply_to_schematic(sess, target_path=None, strip_connections=True)
            assert '(label "SIG"' not in sch.read_text()
