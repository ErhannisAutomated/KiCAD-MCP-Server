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

    def test_load_discovers_net_via_long_wire_chain(self):
        """Regression: load_session must discover a pin's net through
        an arbitrarily long wire chain, including chains joined by
        mid-segment T-junctions.  Surfaced on buckboost where C23/2's
        BB_SW1 connection was > 5 wire hops from the nearest BB_SW1
        label and got dropped, causing apply+rewire to leave C23/2
        permanently orphan."""
        from commands.autoplacer import load_session
        from commands.connection_schematic import ConnectionManager

        with tempfile.TemporaryDirectory() as tmp:
            sch = Path(tmp) / "long_chain.kicad_sch"
            # Build a chain of 8 wire segments between two resistors,
            # with a BB_SW1 label only at the far end.  Pre-fix
            # _bfs_label gave up at depth 5; the chain is 8 hops, so
            # R1/2's net assignment was missed.
            wires = []
            for i in range(8):
                x1 = 100.0 + i * 5.0
                x2 = x1 + 5.0
                wires.append(f'  (wire (pts (xy {x1} 100.0) (xy {x2} 100.0)) '
                             f'(stroke (width 0) (type default)) '
                             f'(uuid 11111111-1111-1111-1111-111111111111))')
            wires_block = "\n".join(wires)
            sch.write_text(textwrap.dedent(f"""\
                (kicad_sch (version 20250114) (generator "test")
                  (lib_symbols
                    (symbol "Device:R" (pin_numbers hide) (pin_names (offset 0))
                      (symbol "R_1_1"
                        (pin passive line (at 0 3.81 270) (length 1.27)
                          (name "~") (number "1"))
                        (pin passive line (at 0 -3.81 90) (length 1.27)
                          (name "~") (number "2"))
                      )
                    )
                  )
                  (symbol (lib_id "Device:R") (at 100.0 96.19 0) (unit 1)
                    (property "Reference" "R1" (at 100.0 96.19 0))
                    (instances (project "test" (path "/" (reference "R1") (unit 1))))
                  )
                  (symbol (lib_id "Device:R") (at 140.0 96.19 0) (unit 1)
                    (property "Reference" "R2" (at 140.0 96.19 0))
                    (instances (project "test" (path "/" (reference "R2") (unit 1))))
                  )
                {wires_block}
                  (label "FAR" (at 140.0 100.0 0)
                    (effects (font (size 1.27 1.27)))
                    (uuid 22222222-2222-2222-2222-222222222222))
                  (sheet_instances (path "/" (page "1")))
                )
            """))
            # Sanity: the file is well-formed and the label is reachable
            # from R1/2 via wires.
            assert ConnectionManager.get_pin_net(sch, "R1", "2") == "FAR"
            assert ConnectionManager.get_pin_net(sch, "R2", "2") == "FAR"

            sess = load_session(sch)
            # The autoplacer's session must know R1/2 and R2/2 are on
            # net FAR.  Pre-fix it would have dropped both.
            assert "FAR" in sess.nets, (
                f"net FAR not discovered; sess.nets={list(sess.nets)}"
            )
            far_pins = {(k, p) for k, p in sess.nets["FAR"].pins}
            assert ("R1__u1", "2") in far_pins
            assert ("R2__u1", "2") in far_pins


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

    def test_pinwise_attraction_uses_pin_coords_not_centres(self):
        """Pin-aware attraction: a's pin1 toward b's pin1, even when
        the components' centres are aligned but the pins offset.
        """
        from commands.autoplacer import _attractive_force_pinwise, Component, Pin

        # A at (100, 100), pin "1" at lib offset (0, +3.81) → world (100, 96.19).
        a = Component(
            "A", 1, "Device:R", x=100, y=100, rotation=0,
            mirror_x=False, mirror_y=False,
            pins={"1": Pin(number="1", name="~", local_x=0, local_y=3.81, lib_angle=270)},
        )
        # B at (100, 100) (same centre as A), but pin "1" at lib (0, -3.81)
        # → world (100, 103.81).  Centre-to-centre attraction would
        # produce (0, 0) force; pin-aware attraction should pull a's
        # pin1 (96.19) DOWN toward b's pin1 (103.81), so fy > 0.
        b = Component(
            "B", 1, "Device:R", x=100, y=100, rotation=0,
            mirror_x=False, mirror_y=False,
            pins={"1": Pin(number="1", name="~", local_x=0, local_y=-3.81, lib_angle=90)},
        )
        fx, fy = _attractive_force_pinwise(a, "1", b, "1", k=1.0)
        # Pin coordinates differ only in y by +7.62 (a.pin at 96.19,
        # b.pin at 103.81 in screen coords), so attraction is +y.
        assert abs(fx) < 1e-6, fx
        assert fy > 0, f"expected +y pinwise attraction, got fy={fy}"

    def test_pinwise_attraction_falls_back_when_pins_unknown(self):
        """If a pin number isn't in the component's pin map, fall back
        to centre-to-centre attraction so the model still has *some*
        connection on incomplete data."""
        from commands.autoplacer import _attractive_force_pinwise, Component

        a = Component("A", 1, "Device:R", x=100, y=100, rotation=0,
                      mirror_x=False, mirror_y=False)  # no pins
        b = Component("B", 1, "Device:R", x=110, y=100, rotation=0,
                      mirror_x=False, mirror_y=False)  # no pins
        fx, fy = _attractive_force_pinwise(a, "1", b, "1", k=1.0)
        # Falls back to centre-to-centre: +x direction since b is to the right of a.
        assert fx > 0, fx


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


@pytest.mark.unit
class TestPolarityTorque:
    def _make_resistor_with_gnd(self, rotation: float):
        """One resistor whose pin 2 is on GND.  At rotation=0, pin 2's
        outward angle is 270° (down) — already pointing the right way
        for GND.  At rotation=180, pin 2 ends up facing UP, so the
        polarity torque should rotate it back toward 270."""
        from commands.autoplacer import Component, Net, Pin, Session

        sess = Session(schematic_path=Path("/tmp/synthetic.kicad_sch"))
        sess.components["R1__u1"] = Component(
            ref="R1", unit=1, lib_id="Device:R", x=100.0, y=100.0,
            rotation=rotation, mirror_x=False, mirror_y=False,
            pins={
                "1": Pin("1", "~", local_x=0, local_y=3.81, lib_angle=270),
                "2": Pin("2", "~", local_x=0, local_y=-3.81, lib_angle=90),
            },
            bbox_w=12.7, bbox_h=12.7,
        )
        sess.nets["GND"] = Net(name="GND", pins=[("R1__u1", "2")])
        return sess

    def test_already_oriented_gives_zero_torque(self):
        """Pin 2 outward angle at rotation=0 is 270° — already pointing
        down (GND target), so polarity torque must be (approximately)
        zero."""
        from commands.autoplacer import _torque_polarity_orientation

        sess = self._make_resistor_with_gnd(rotation=0)
        torque = _torque_polarity_orientation(sess.components["R1__u1"], sess)
        assert abs(torque) < 1e-3, torque

    def test_misoriented_gives_nonzero_torque_toward_target(self):
        """At rotation=180, pin 2's outward angle is 90° (up).  Target
        for GND is 270° (down).  Shortest rotation: ±180.  Torque sign
        is whichever way the diff lands; magnitude should be nonzero
        and proportional to polarity_torque_k."""
        from commands.autoplacer import _torque_polarity_orientation

        sess = self._make_resistor_with_gnd(rotation=180)
        torque = _torque_polarity_orientation(sess.components["R1__u1"], sess)
        assert abs(torque) > 0, torque
        # Doubling polarity_torque_k must double the torque.
        sess.params.polarity_torque_k *= 2
        torque2 = _torque_polarity_orientation(sess.components["R1__u1"], sess)
        assert abs(abs(torque2) - 2 * abs(torque)) < 1e-6, (torque, torque2)

    def test_v_plus_pin_targets_up(self):
        """A pin on a top-polarity net targets 90° (up).  At rotation=0
        with pin 2 facing 270° (down), torque should be nonzero pulling
        toward 90."""
        from commands.autoplacer import _torque_polarity_orientation

        sess = self._make_resistor_with_gnd(rotation=0)
        # Switch the net from GND to a top-polarity name.
        sess.nets.pop("GND")
        from commands.autoplacer import Net
        sess.nets["+3V3"] = Net(name="+3V3", pins=[("R1__u1", "2")])
        torque = _torque_polarity_orientation(sess.components["R1__u1"], sess)
        # Pin 2 outward = 270, target = 90, signed shortest = -180 (or 180).
        assert abs(torque) > 0, torque

    def test_pin_orientation_torque_skips_power_nets(self):
        """`_torque_for_pin_orientation` must NOT contribute torque on
        power/excluded nets — those are handled by polarity torque
        instead.  Without this, GND with N pins scattered around the
        sheet drags every component's GND pin toward the centroid of
        all the GND pins, which is meaningless and overwhelms torque
        from the small signal nets."""
        from commands.autoplacer import (
            Component, Net, Pin, Session, _torque_for_pin_orientation,
        )

        # R1 with a GND pin connection to a far-away GND endpoint.
        sess = Session(schematic_path=Path("/tmp/synthetic.kicad_sch"))
        sess.components["R1__u1"] = Component(
            ref="R1", unit=1, lib_id="Device:R", x=100.0, y=100.0, rotation=0,
            mirror_x=False, mirror_y=False,
            pins={
                "1": Pin("1", "~", local_x=0, local_y=3.81, lib_angle=270),
                "2": Pin("2", "~", local_x=0, local_y=-3.81, lib_angle=90),
            },
            bbox_w=12.7, bbox_h=12.7,
        )
        sess.components["U1__u1"] = Component(
            ref="U1", unit=1, lib_id="Device:U", x=300.0, y=100.0, rotation=0,
            mirror_x=False, mirror_y=False,
            pins={
                "1": Pin("1", "~", local_x=0, local_y=0, lib_angle=180),
            },
            bbox_w=12.7, bbox_h=12.7,
        )
        sess.nets["GND"] = Net(
            name="GND", pins=[("R1__u1", "2"), ("U1__u1", "1")],
        )
        torque = _torque_for_pin_orientation(sess.components["R1__u1"], sess)
        assert torque == 0, (
            f"power-net torque should be zero (handled by polarity "
            f"torque); got {torque}"
        )


@pytest.mark.unit
class TestAttractionExclusion:
    def _make_two_resistors_on_net(self, net_name: str):
        from commands.autoplacer import Component, Net, Pin, Session

        sess = Session(schematic_path=Path("/tmp/synthetic.kicad_sch"))
        for ref, x in (("R1", 100.0), ("R2", 200.0)):
            sess.components[f"{ref}__u1"] = Component(
                ref=ref, unit=1, lib_id="Device:R", x=x, y=100.0, rotation=0,
                mirror_x=False, mirror_y=False,
                pins={
                    "1": Pin("1", "~", local_x=0, local_y=3.81, lib_angle=270),
                    "2": Pin("2", "~", local_x=0, local_y=-3.81, lib_angle=90),
                },
                bbox_w=12.7, bbox_h=12.7,
            )
        sess.nets[net_name] = Net(
            name=net_name, pins=[("R1__u1", "2"), ("R2__u1", "1")],
        )
        return sess

    def test_signal_net_pulls_components_together(self):
        """Sanity: a non-excluded net should produce attraction; the
        components should move toward each other after one iteration."""
        from commands.autoplacer import iterate

        sess = self._make_two_resistors_on_net("SIG")
        sess.params.attraction_k = 1.0  # crank up so one step is visible
        sess.params.repulsion_k = 0.0   # zero out repulsion to isolate
        sess.params.boundary_k = 0.0
        sess.temperature = 5.0
        x_before = (sess.components["R1__u1"].x, sess.components["R2__u1"].x)
        iterate(sess, n=1)
        x_after = (sess.components["R1__u1"].x, sess.components["R2__u1"].x)
        # R1 should move +x, R2 should move -x (toward each other).
        assert x_after[0] > x_before[0], (x_before, x_after)
        assert x_after[1] < x_before[1], (x_before, x_after)

    def test_power_net_does_not_pull_components_together(self):
        """A net classified as power (default behaviour) is excluded from
        attraction.  With no other forces, the components shouldn't move
        toward each other."""
        from commands.autoplacer import iterate

        sess = self._make_two_resistors_on_net("GND")
        sess.params.attraction_k = 1.0
        sess.params.repulsion_k = 0.0
        sess.params.boundary_k = 0.0
        sess.params.polarity_k = 0.0  # disable polarity bias too
        sess.params.rotation_k = 0.0
        sess.temperature = 5.0
        x_before = (sess.components["R1__u1"].x, sess.components["R2__u1"].x)
        iterate(sess, n=1)
        x_after = (sess.components["R1__u1"].x, sess.components["R2__u1"].x)
        # Components shouldn't have moved.
        assert x_after == x_before, (
            f"GND should be excluded from attraction; got {x_before} → {x_after}"
        )

    def test_user_added_excluded_net_is_skipped(self):
        """The user-supplied attraction_excluded_nets tuple adds names
        on top of the power-net default."""
        from commands.autoplacer import iterate

        sess = self._make_two_resistors_on_net("ALERT")
        sess.params.attraction_k = 1.0
        sess.params.repulsion_k = 0.0
        sess.params.boundary_k = 0.0
        sess.params.polarity_k = 0.0
        sess.params.rotation_k = 0.0
        sess.params.attraction_excluded_nets = ("ALERT",)
        sess.temperature = 5.0
        x_before = (sess.components["R1__u1"].x, sess.components["R2__u1"].x)
        iterate(sess, n=1)
        x_after = (sess.components["R1__u1"].x, sess.components["R2__u1"].x)
        assert x_after == x_before, (
            f"ALERT should be excluded; got {x_before} → {x_after}"
        )

    def test_disabling_power_exclusion_lets_power_nets_pull(self):
        """Setting exclude_power_nets_from_attraction=False makes the
        default GND/VCC/etc. NOT excluded — useful for debugging."""
        from commands.autoplacer import iterate

        sess = self._make_two_resistors_on_net("GND")
        sess.params.attraction_k = 1.0
        sess.params.repulsion_k = 0.0
        sess.params.boundary_k = 0.0
        sess.params.polarity_k = 0.0
        sess.params.rotation_k = 0.0
        sess.params.exclude_power_nets_from_attraction = False
        sess.temperature = 5.0
        x_before = (sess.components["R1__u1"].x, sess.components["R2__u1"].x)
        iterate(sess, n=1)
        x_after = (sess.components["R1__u1"].x, sess.components["R2__u1"].x)
        assert x_after[0] > x_before[0], x_after
        assert x_after[1] < x_before[1], x_after


@pytest.mark.unit
class TestSnapPositions:
    def test_pin_coord_collision_resolved(self):
        """Two single-unit resistors placed so their bboxes clear but
        a single pin coord coincides — snap_positions's pin-coord
        safety pass must nudge one component until the pins separate.

        Without this, connect_pins(auto) on different nets would route
        wires that share an endpoint and merge the nets in KiCad's
        wire graph (the C1/TH1 → BAT+/CELL1_TOP merge bug).
        """
        from commands.autoplacer import Component, Pin, Session, snap_positions

        # Two horizontal resistors with pin 1 on the right side (after
        # 270° rotation).  Position them so their bboxes barely don't
        # overlap (centres far apart) but pin coords coincide.
        #   R1 at (100, 100) rot=270 → pin1 lib (0, +3.81) → world (103.81, 100)
        #   R2 at (107.62, 100) rot=90 → pin1 lib (0, +3.81) → world (103.81, 100)
        # bboxes (12.7×12.7) clear (dx=7.62 < min_dx=13.97 — actually do overlap by bbox)
        # The point: snap should keep nudging R2 until both bbox AND
        # pin coincidences clear.
        sess = Session(schematic_path=Path("/tmp/synthetic.kicad_sch"))

        def _resistor(ref: str, x: float, y: float, rot: float) -> Component:
            return Component(
                ref=ref, unit=1, lib_id="Device:R", x=x, y=y, rotation=rot,
                mirror_x=False, mirror_y=False,
                pins={
                    "1": Pin(number="1", name="~", local_x=0, local_y=3.81, lib_angle=270),
                    "2": Pin(number="2", name="~", local_x=0, local_y=-3.81, lib_angle=90),
                },
                bbox_w=12.7, bbox_h=12.7,
            )

        sess.components["R1__u1"] = _resistor("R1", 100.0, 100.0, 270)
        sess.components["R2__u1"] = _resistor("R2", 107.62, 100.0, 90)

        # Sanity: with the rotations as set, both components' pin "1"
        # land at the same world coord.
        wp1 = sess.components["R1__u1"].world_pin_xy("1")
        wp2 = sess.components["R2__u1"].world_pin_xy("1")
        assert (
            abs(wp1[0] - wp2[0]) < 0.01 and abs(wp1[1] - wp2[1]) < 0.01
        ), f"setup invariant: pin1s should coincide, got {wp1} vs {wp2}"

        snap_positions(sess)

        # After snap, no two pins from different components may share a coord.
        wp1 = sess.components["R1__u1"].world_pin_xy("1")
        wp2 = sess.components["R2__u1"].world_pin_xy("1")
        assert not (abs(wp1[0] - wp2[0]) < 0.01 and abs(wp1[1] - wp2[1]) < 0.01), (
            f"R1/1 and R2/1 still coincident at {wp1} after snap"
        )

    def test_multi_unit_pin_stub_end_collision_resolved(self):
        """Two units of one multi-unit symbol must not place pin endpoints
        OR stub-ends on top of another unit's pin endpoint.  Repro from
        the BMS run: Q1 unit 1's drain stub-end landed exactly on Q1
        unit 2's source pin (different units of Q1, both at x=59.69),
        which would have merged FET_MID and SRP nets at rewire time.
        """
        from commands.autoplacer import Component, Pin, Session, snap_positions

        sess = Session(schematic_path=Path("/tmp/synthetic.kicad_sch"))

        def _q1_unit(unit: int, x: float, y: float) -> Component:
            return Component(
                ref="Q1", unit=unit, lib_id="Test:DualFET",
                x=x, y=y, rotation=0,
                mirror_x=False, mirror_y=False,
                # Pin 7/8 (D, top — outward UP), pin 3 (S, bottom — outward DOWN).
                pins={
                    "7": Pin("7", "D", local_x=2.54, local_y=5.08, lib_angle=270),
                    "8": Pin("8", "D", local_x=2.54, local_y=5.08, lib_angle=270),
                    "3": Pin("3", "S", local_x=2.54, local_y=-5.08, lib_angle=90),
                },
                bbox_w=12.7, bbox_h=12.7,
            )

        # Place Q1__u1 12.7mm below Q1__u2.  Pin 7/8 on u1 at world
        # (x+2.54, y-5.08) = (102.54, 92.07).  Stub end (outward up by
        # 2.54mm) at (102.54, 89.53).  Pin 3 on u2 at world (x+2.54,
        # y+5.08) = (102.54, 89.53) — SAME COORD as u1's stub end.
        sess.components["Q1__u1"] = _q1_unit(1, 100.0, 97.15)
        sess.components["Q1__u2"] = _q1_unit(2, 100.0, 84.45)

        # Verify the collision exists pre-snap (sanity).
        u1_stub_end = (
            sess.components["Q1__u1"].world_pin_xy("7")[0],
            sess.components["Q1__u1"].world_pin_xy("7")[1] - 2.54,
        )
        u2_pin3 = sess.components["Q1__u2"].world_pin_xy("3")
        assert (
            abs(u1_stub_end[0] - u2_pin3[0]) < 0.01
            and abs(u1_stub_end[1] - u2_pin3[1]) < 0.01
        ), f"setup invariant: u1 stub-end {u1_stub_end} should equal u2 pin3 {u2_pin3}"

        snap_positions(sess)

        # After snap, the stub-end and pin coord must no longer coincide.
        u1_stub_end = (
            sess.components["Q1__u1"].world_pin_xy("7")[0],
            sess.components["Q1__u1"].world_pin_xy("7")[1] - 2.54,
        )
        u2_pin3 = sess.components["Q1__u2"].world_pin_xy("3")
        assert not (
            abs(u1_stub_end[0] - u2_pin3[0]) < 0.01
            and abs(u1_stub_end[1] - u2_pin3[1]) < 0.01
        ), (
            f"u1 stub-end {u1_stub_end} still coincides with u2 pin3 "
            f"{u2_pin3} after snap — would merge FET_MID and SRP at rewire"
        )

    def test_multipass_resolves_chained_overlaps(self):
        """If pair (a, b) nudges b right and the new b position now
        overlaps with a previously-OK pair (c, b) where c < a in the
        scan order, single-pass snap misses it.  Multi-pass should
        catch the chain reaction on the second pass.
        """
        from commands.autoplacer import Component, Session, snap_positions

        sess = Session(schematic_path=Path("/tmp/synthetic.kicad_sch"))
        # Three resistors lined up.  C is to the right of A; B starts
        # left of A.  Pair (A, B) nudges B right past C; pair (C, B)
        # is then a new overlap that single-pass snap would miss
        # because C was processed before A.
        for ref, x in (("A", 100.0), ("B", 95.0), ("C", 110.0)):
            sess.components[f"{ref}__u1"] = Component(
                ref=ref, unit=1, lib_id="Device:R", x=x, y=100.0, rotation=0,
                mirror_x=False, mirror_y=False,
                bbox_w=12.7, bbox_h=12.7,
            )
        snap_positions(sess)
        # Final positions: A, B, C all on grid and no pair overlaps.
        for ki in sess.components:
            for kj in sess.components:
                if ki >= kj:
                    continue
                a, b = sess.components[ki], sess.components[kj]
                if a.ref == b.ref:
                    continue
                min_dx = (a.bbox_w + b.bbox_w) / 2 + 1.27
                min_dy = (a.bbox_h + b.bbox_h) / 2 + 1.27
                assert (
                    abs(a.x - b.x) >= min_dx or abs(a.y - b.y) >= min_dy
                ), f"residual overlap {ki}↔{kj}: ({a.x}, {a.y}) vs ({b.x}, {b.y})"


@pytest.mark.unit
class TestNoConnectPreservation:
    def test_no_connect_marker_recorded_on_load(self):
        """If the source schematic has a (no_connect) at a pin endpoint,
        load_session records (component_key, pin_number) so apply can
        re-emit it after stripping connections."""
        from commands.autoplacer import load_session

        with tempfile.TemporaryDirectory() as tmp:
            sch = Path(tmp) / "nc.kicad_sch"
            R_LIB = textwrap.dedent("""\
                (lib_symbols
                  (symbol "Device:R" (pin_numbers hide) (pin_names (offset 0))
                    (symbol "R_1_1"
                      (pin passive line (at 0 3.81 270) (length 1.27)
                        (name "~") (number "1"))
                      (pin passive line (at 0 -3.81 90) (length 1.27)
                        (name "~") (number "2"))
                    )
                  )
                )
            """)
            # R1 at (100, 100) rot=0 → pin 1 at (100, 96.19), pin 2 at (100, 103.81).
            sch.write_text(textwrap.dedent(f"""\
                (kicad_sch (version 20250114) (generator "test")
                  (uuid 11111111-2222-3333-4444-555555555555)
                  {R_LIB}
                  (symbol (lib_id "Device:R") (at 100 100 0) (unit 1)
                    (property "Reference" "R1" (at 100 100 0))
                    (property "Value" "10k" (at 100 100 0))
                    (instances (project "test" (path "/" (reference "R1") (unit 1))))
                  )
                  (no_connect (at 100 96.19) (uuid abcd-0001))
                  (sheet_instances (path "/" (page "1")))
                )
            """))
            sess = load_session(sch)
            assert ("R1__u1", "1") in sess.no_connects, (
                f"expected (R1__u1, 1) in no_connects; got {sess.no_connects}"
            )

    def test_apply_re_emits_no_connect_at_new_pin_position(self):
        """After move + apply, the (no_connect) marker should appear at
        the new pin coord, not the old."""
        from commands.autoplacer import (
            PLACER, apply_to_schematic, load_session, rewire_session,
            snap_positions,
        )
        import sexpdata as _sd

        with tempfile.TemporaryDirectory() as tmp:
            sch = Path(tmp) / "nc2.kicad_sch"
            R_LIB = textwrap.dedent("""\
                (lib_symbols
                  (symbol "Device:R" (pin_numbers hide) (pin_names (offset 0))
                    (symbol "R_1_1"
                      (pin passive line (at 0 3.81 270) (length 1.27)
                        (name "~") (number "1"))
                      (pin passive line (at 0 -3.81 90) (length 1.27)
                        (name "~") (number "2"))
                    )
                  )
                )
            """)
            # Place R1 at (100, 100), no_connect at pin 1 (100, 96.19).
            sch.write_text(textwrap.dedent(f"""\
                (kicad_sch (version 20250114) (generator "test")
                  (uuid 11111111-2222-3333-4444-555555555555)
                  {R_LIB}
                  (symbol (lib_id "Device:R") (at 100 100 0) (unit 1)
                    (property "Reference" "R1" (at 100 100 0))
                    (property "Value" "10k" (at 100 100 0))
                    (instances (project "test" (path "/" (reference "R1") (unit 1))))
                  )
                  (no_connect (at 100 96.19) (uuid abcd-0001))
                  (sheet_instances (path "/" (page "1")))
                )
            """))
            sess = load_session(sch)
            # Move R1 to (200, 100); pin 1 will be at (200, 96.19).
            sess.components["R1__u1"].x = 200.0
            sess.components["R1__u1"].y = 100.0
            apply_to_schematic(sess, target_path=None, strip_connections=True)
            rewire_session(sess, sch)

            text = sch.read_text()
            sexp = _sd.loads(text)
            ncs = []
            for top in sexp:
                if isinstance(top, list) and top and str(top[0]) == "no_connect":
                    for sub in top[1:]:
                        if (
                            isinstance(sub, list) and sub and str(sub[0]) == "at"
                            and len(sub) >= 3
                        ):
                            ncs.append((float(sub[1]), float(sub[2])))
                            break
            assert any(
                abs(p[0] - 200.0) < 0.5 and abs(p[1] - 96.19) < 0.5 for p in ncs
            ), f"expected no_connect at ~(200, 96.19), got {ncs}"


@pytest.mark.unit
class TestPageCentering:
    def test_centers_bbox_on_page(self):
        """center_components_on_page translates all mobile components so
        the bbox of the placement is centred on the page (≈ 148.59, 104.78)."""
        from commands.autoplacer import (
            Component, Session, center_components_on_page, _PAGE_CENTRE,
        )

        sess = Session(schematic_path=Path("/tmp/synthetic.kicad_sch"))
        # Place 3 components clustered far from page centre (top-left corner).
        for ref, x, y in (("A", 30.0, 30.0), ("B", 50.0, 40.0), ("C", 40.0, 60.0)):
            sess.components[f"{ref}__u1"] = Component(
                ref=ref, unit=1, lib_id="Device:R", x=x, y=y, rotation=0,
                mirror_x=False, mirror_y=False,
                bbox_w=12.7, bbox_h=12.7,
            )
        center_components_on_page(sess)
        # New bbox centre should be ~ page centre.
        xs = [c.x for c in sess.components.values()]
        ys = [c.y for c in sess.components.values()]
        cx = (min(xs) + max(xs)) / 2.0
        cy = (min(ys) + max(ys)) / 2.0
        assert abs(cx - _PAGE_CENTRE[0]) < 1.27, (
            f"x centre {cx} should be near {_PAGE_CENTRE[0]}"
        )
        assert abs(cy - _PAGE_CENTRE[1]) < 1.27, (
            f"y centre {cy} should be near {_PAGE_CENTRE[1]}"
        )

    def test_rigid_translation_preserves_relative_positions(self):
        """Centering is a rigid translation — every component moves by
        the same (dx, dy), including pinned ones.  This preserves the
        relative geometry (wire routing distances, etc.) while moving
        the whole assembly to the page centre.  If pinned components
        were left behind, they'd sit far from the rest of the layout
        after centering and break routing between them."""
        from commands.autoplacer import (
            Component, Session, center_components_on_page,
        )

        sess = Session(schematic_path=Path("/tmp/synthetic.kicad_sch"))
        sess.components["J1__u1"] = Component(
            ref="J1", unit=1, lib_id="Conn", x=20.0, y=20.0, rotation=0,
            mirror_x=False, mirror_y=False, bbox_w=12.7, bbox_h=12.7,
            pinned=True,
        )
        sess.components["R1__u1"] = Component(
            ref="R1", unit=1, lib_id="Device:R", x=30.0, y=30.0, rotation=0,
            mirror_x=False, mirror_y=False, bbox_w=12.7, bbox_h=12.7,
        )
        # Original delta between J1 and R1 is (10, 10).
        center_components_on_page(sess)
        delta_x = sess.components["R1__u1"].x - sess.components["J1__u1"].x
        delta_y = sess.components["R1__u1"].y - sess.components["J1__u1"].y
        assert abs(delta_x - 10.0) < 1e-6, f"R1.x - J1.x = {delta_x}, expected 10"
        assert abs(delta_y - 10.0) < 1e-6, f"R1.y - J1.y = {delta_y}, expected 10"
        # And both should have moved (initial centre was (25,25), target ~(148.59, 104.78)).
        assert sess.components["J1__u1"].x > 100, sess.components["J1__u1"].x


@pytest.mark.unit
class TestRewireMaxLenScaling:
    def test_routing_max_len_scales_with_placement_diagonal(self):
        """rewire_session should pass connect_pins a max_len that scales
        with the placement bbox.  On a tightly-packed test sheet the
        default would suffice, but on the BMS layout (~150 mm diagonal)
        the default 80 mm is too small for routes around the BMS chip."""
        from unittest.mock import patch

        from commands.autoplacer import (
            Component, Net, Pin, Session, rewire_session,
        )
        from commands.connection_schematic import ConnectionManager

        sess = Session(schematic_path=Path("/tmp/synth.kicad_sch"))
        # Two components ~150 mm apart on the diagonal.  Diagonal =
        # hypot(150, 0) = 150; routing_max_len = 2*150 + 40 = 340.
        sess.components["R1__u1"] = Component(
            ref="R1", unit=1, lib_id="Device:R", x=50.0, y=100.0, rotation=0,
            mirror_x=False, mirror_y=False,
            pins={"1": Pin("1", "~", local_x=0, local_y=3.81, lib_angle=270)},
            bbox_w=12.7, bbox_h=12.7,
        )
        sess.components["R2__u1"] = Component(
            ref="R2", unit=1, lib_id="Device:R", x=200.0, y=100.0, rotation=0,
            mirror_x=False, mirror_y=False,
            pins={"1": Pin("1", "~", local_x=0, local_y=3.81, lib_angle=270)},
            bbox_w=12.7, bbox_h=12.7,
        )
        sess.nets["SIG"] = Net(
            name="SIG", pins=[("R1__u1", "1"), ("R2__u1", "1")],
        )

        captured: dict = {}

        def _spy(schematic_path, pins, net_name=None, style="label", **kw):
            captured.setdefault("calls", []).append(kw.get("max_len"))
            return {
                "success": True, "connected": [], "wired_pairs": [],
                "routing_failures": [],
            }

        with patch.object(ConnectionManager, "connect_pins", staticmethod(_spy)):
            rewire_session(sess, sess.schematic_path)

        # Diagonal ≈ 150 mm; expected max_len = 2*150 + 40 = 340 mm.
        assert captured["calls"], "no connect_pins calls recorded"
        max_len = captured["calls"][0]
        assert max_len is not None and max_len >= 340 - 1e-6, (
            f"rewire should pass max_len that scales with placement; got {max_len}"
        )


@pytest.mark.unit
class TestRewire:
    def test_rewire_uses_connect_pins_auto(self):
        """rewire_session should drive ConnectionManager.connect_pins
        with style='auto', which adds wires when feasible and labels
        otherwise.  Two close pins on the same net should end up with
        at least one wire and at least one label.
        """
        from commands.autoplacer import (
            apply_to_schematic,
            load_session,
            rewire_session,
        )

        with tempfile.TemporaryDirectory() as tmp:
            sch = _make_two_r_with_net(Path(tmp), net_label_at_r1_pin1=True)
            sess = load_session(sch)
            # Make sure the net was discovered.
            assert "SIG" in sess.nets
            # Strip existing labels/wires (apply default), then rewire.
            apply_to_schematic(sess, target_path=None, strip_connections=True)
            result = rewire_session(sess, sch)
            assert result["method"] == "connect_pins(auto)"
            assert result["nets_rewired"] >= 1
            # connect_pins(auto) on close horizontal resistors should
            # produce at least one wire OR at least one label — the
            # net must end up with the SIG name reachable from R1/2 + R2/1.
            text = sch.read_text()
            assert '"SIG"' in text, "SIG label missing after rewire"


@pytest.mark.unit
class TestScanUnrelatedCrossings:
    """The crossings diagnostic must use the real wire-graph BFS to
    figure out each wire's net, not just labels at the wire's own
    endpoints — otherwise two segments of one labelled chain that
    happen to cross each other get flagged as 'unrelated'."""

    def _write(self, tmp: Path, name: str, wires: list, labels: list) -> Path:
        parts = [
            '(kicad_sch (version 20250114) (generator "test")',
            '  (uuid 11111111-1111-1111-1111-111111111111)',
            '  (paper "A4")',
        ]
        for (x1, y1), (x2, y2) in wires:
            parts.append(
                f'  (wire (pts (xy {x1} {y1}) (xy {x2} {y2})) '
                '(stroke (width 0) (type default)) '
                '(uuid 11111111-1111-1111-1111-111111111111))'
            )
        for name_, x, y in labels:
            parts.append(
                f'  (label "{name_}" (at {x} {y} 0) '
                '(effects (font (size 1.27 1.27))) '
                '(uuid 22222222-2222-2222-2222-222222222222))'
            )
        parts.append('  (sheet_instances (path "/" (page "1")))')
        parts.append(')')
        p = tmp / name
        p.write_text("\n".join(parts))
        return p

    def test_same_net_segments_crossing_not_flagged(self):
        """Two USB_VBUS segments that cross each other — both reachable
        from one USB_VBUS label via wire-graph BFS — must not appear
        in the unrelated-crossings findings.  Regression for the
        charger sheet's USB_VBUS crossing north-west of C17 reported
        by the user 2026-05-13."""
        from commands.autoplacer import _scan_unrelated_wire_crossings

        with tempfile.TemporaryDirectory() as tmp:
            sch = self._write(
                Path(tmp),
                "same_net.kicad_sch",
                wires=[
                    # Long vertical segment.
                    ((100.0, 90.0), (100.0, 110.0)),
                    # Long horizontal segment crossing it at (100, 100).
                    ((90.0, 100.0), (110.0, 100.0)),
                    # A connecting segment ties them on the same chain
                    # via a shared endpoint at the vertical's TOP.
                    ((100.0, 90.0), (90.0, 100.0)),
                    # Another connecting at the horizontal's RIGHT end.
                    ((110.0, 100.0), (100.0, 110.0)),
                ],
                labels=[("USB_VBUS", 100.0, 90.0)],
            )
            findings = _scan_unrelated_wire_crossings(sch)
            assert findings == [], (
                f"Same-net crossing must not flag — got {findings}"
            )

    def test_genuinely_unrelated_crossing_still_flagged(self):
        """Two perpendicular wires on DIFFERENT nets crossing without
        a junction is still a legitimate finding (would silently
        merge nets if anything ever endpoints at the crossing point)."""
        from commands.autoplacer import _scan_unrelated_wire_crossings

        with tempfile.TemporaryDirectory() as tmp:
            sch = self._write(
                Path(tmp),
                "diff_net.kicad_sch",
                wires=[
                    ((100.0, 90.0), (100.0, 110.0)),  # NET_A
                    ((90.0, 100.0), (110.0, 100.0)),  # NET_B
                ],
                labels=[
                    ("NET_A", 100.0, 90.0),
                    ("NET_B", 90.0, 100.0),
                ],
            )
            findings = _scan_unrelated_wire_crossings(sch)
            assert len(findings) == 1
            pt = findings[0]["point"]
            assert pt == [100.0, 100.0]


@pytest.mark.unit
class TestStagedAnneal:
    """Smoke tests for run_staged_anneal — the four-stage recipe."""

    def test_iteration_count_matches_schedule(self):
        from commands.autoplacer import load_session, run_staged_anneal

        with tempfile.TemporaryDirectory() as tmp:
            sch = _make_two_r_with_net(Path(tmp))
            sess = load_session(sch)
            start = sess.iteration
            # Tiny knobs so the test is fast.
            result = run_staged_anneal(
                sess,
                cluster_iters=5,
                spread_stages=3,
                polarize_stages=2,
                settle_iters=4,
                iters_per_stage=2,
            )
            # Total = cluster + spread × iters_per_stage + polarize × iters_per_stage + settle
            #       = 5 + 3*2 + 2*2 + 4 = 19
            assert sess.iteration - start == 19
            assert result["iteration"] == sess.iteration

    def test_final_params_reflect_polarize_stage(self):
        """After the recipe runs, polarity_k and polarity_torque_k must
        be ON (they're enabled in stage 3 and not unset), and
        repulsion_k must be the polarize-stage last value."""
        from commands.autoplacer import (
            _RECIPE_DEFAULTS,
            load_session,
            run_staged_anneal,
        )

        with tempfile.TemporaryDirectory() as tmp:
            sch = _make_two_r_with_net(Path(tmp))
            sess = load_session(sch)
            run_staged_anneal(
                sess,
                cluster_iters=1,
                spread_stages=3,
                polarize_stages=2,
                settle_iters=1,
                iters_per_stage=1,
                polarity_k=0.42,
                polarity_torque_k=2.71,
                repulsion_base=0.1,
                repulsion_growth=2.0,
            )
            assert sess.params.polarity_k == 0.42
            assert sess.params.polarity_torque_k == 2.71
            # Polarize stage runs t=0..1: rep = base * growth ** (spread_peak - t)
            # spread_peak = 3 - 1 = 2.  t=0 → 0.1 * 4 = 0.4.  t=1 → 0.1 * 2 = 0.2.
            assert sess.params.repulsion_k == 0.2

    def test_on_step_callback_invoked(self):
        from commands.autoplacer import load_session, run_staged_anneal

        with tempfile.TemporaryDirectory() as tmp:
            sch = _make_two_r_with_net(Path(tmp))
            sess = load_session(sch)
            calls = []
            run_staged_anneal(
                sess,
                cluster_iters=2,
                spread_stages=1,
                polarize_stages=1,
                settle_iters=2,
                iters_per_stage=1,
                on_step=lambda s: calls.append(s.iteration),
            )
            # Called once per iteration across all four stages.
            # Total iters = 2 + 1*1 + 1*1 + 2 = 6.
            assert len(calls) == 6
            # Strictly monotonic.
            assert calls == sorted(calls)
