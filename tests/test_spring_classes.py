"""Unit tests for the spring-class system (v2 placement constraints).

Five-level resolution hierarchy:
    connection-specific > pad-general > net > component > default

For each pair (A, B), exactly one SpringClass governs the spring —
Newton's third law requires both endpoints share a constant.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PYTHON_DIR = Path(__file__).parent.parent / "python"
sys.path.insert(0, str(PYTHON_DIR))


def _make_comp(ref, *, spring_class=None, pin_classes=None):
    from commands.autoplacer import Component
    return Component(
        ref=ref, unit=1, lib_id="Device:R",
        x=0.0, y=0.0, rotation=0.0,
        mirror_x=False, mirror_y=False,
        spring_class=spring_class,
        pin_classes=pin_classes or {},
    )


@pytest.mark.unit
class TestSpringClassResolution:
    def test_default_when_nothing_assigned(self):
        from commands.autoplacer import (
            DEFAULT_SPRING_CLASSES, resolve_pair_class,
        )
        a = _make_comp("R1")
        b = _make_comp("R2")
        cls = resolve_pair_class(
            dict(DEFAULT_SPRING_CLASSES), {}, a, "1", b, "2", "SIG"
        )
        assert cls.name == "LOCAL_SIGNAL"

    def test_component_level_beats_default(self):
        from commands.autoplacer import (
            DEFAULT_SPRING_CLASSES, resolve_pair_class,
        )
        a = _make_comp("C1", spring_class="DECOUPLING")
        b = _make_comp("U1")
        cls = resolve_pair_class(
            dict(DEFAULT_SPRING_CLASSES), {}, a, "1", b, "7", "VCC"
        )
        assert cls.name == "DECOUPLING"

    def test_net_beats_component(self):
        from commands.autoplacer import (
            DEFAULT_SPRING_CLASSES, Net, resolve_pair_class,
        )
        a = _make_comp("C1", spring_class="DECOUPLING")
        b = _make_comp("U1")
        nets = {"VCC": Net(name="VCC", spring_class="INTER_GROUP")}
        cls = resolve_pair_class(
            dict(DEFAULT_SPRING_CLASSES), nets, a, "1", b, "7", "VCC"
        )
        assert cls.name == "INTER_GROUP"

    def test_pad_general_beats_net(self):
        from commands.autoplacer import (
            DEFAULT_SPRING_CLASSES, Net, resolve_pair_class,
        )
        a = _make_comp("C1", pin_classes={"1": "DECOUPLING"})
        b = _make_comp("U1")
        nets = {"VCC": Net(name="VCC", spring_class="INTER_GROUP")}
        cls = resolve_pair_class(
            dict(DEFAULT_SPRING_CLASSES), nets, a, "1", b, "7", "VCC"
        )
        assert cls.name == "DECOUPLING"

    def test_pad_general_with_dict_form(self):
        """Dict form with "*" key = pad-general."""
        from commands.autoplacer import (
            DEFAULT_SPRING_CLASSES, Net, resolve_pair_class,
        )
        a = _make_comp("C1", pin_classes={"1": {"*": "LOCAL_SIGNAL"}})
        b = _make_comp("U1")
        # Make U1's component-level DECOUPLING — should LOSE to A's pad-general.
        b.spring_class = "DECOUPLING"
        cls = resolve_pair_class(
            dict(DEFAULT_SPRING_CLASSES), {}, a, "1", b, "7", "VCC"
        )
        assert cls.name == "LOCAL_SIGNAL"

    def test_connection_specific_beats_pad_general(self):
        """C1.1 → U1.4 specifically gets DECOUPLING; other targets use SIGNAL."""
        from commands.autoplacer import (
            DEFAULT_SPRING_CLASSES, resolve_pair_class,
        )
        a = _make_comp(
            "C1",
            pin_classes={"1": {"*": "LOCAL_SIGNAL", "U1.4": "DECOUPLING"}},
        )
        u1 = _make_comp("U1")
        # Pairing with U1.4 → connection-specific DECOUPLING.
        cls = resolve_pair_class(
            dict(DEFAULT_SPRING_CLASSES), {}, a, "1", u1, "4", "VCC"
        )
        assert cls.name == "DECOUPLING"
        # Pairing with U1.7 → falls through to pad-general "*" = LOCAL_SIGNAL.
        cls = resolve_pair_class(
            dict(DEFAULT_SPRING_CLASSES), {}, a, "1", u1, "7", "VCC"
        )
        assert cls.name == "LOCAL_SIGNAL"

    def test_users_R1_C1_U1_example(self):
        """User's stated example (verbatim from spec):
          R1:1, C1:1, U1:4 on VCC; C1:1 is DECOUPLING for U1:4 only;
          R1 has pad-general SIGNAL on pin 1.
        """
        from commands.autoplacer import (
            DEFAULT_SPRING_CLASSES, resolve_pair_class,
        )
        # SIGNAL is just an alias for our LOCAL_SIGNAL here.
        r1 = _make_comp("R1", pin_classes={"1": "LOCAL_SIGNAL"})
        c1 = _make_comp(
            "C1", pin_classes={"1": {"U1.4": "DECOUPLING"}},
        )
        u1 = _make_comp("U1")
        classes = dict(DEFAULT_SPRING_CLASSES)

        # R1:1 ↔ C1:1  (C1's spec targets U1.4, doesn't apply; R1 has pad-gen)
        assert resolve_pair_class(classes, {}, r1, "1", c1, "1", "VCC").name == "LOCAL_SIGNAL"
        # R1:1 ↔ U1:4  (R1 has pad-gen)
        assert resolve_pair_class(classes, {}, r1, "1", u1, "4", "VCC").name == "LOCAL_SIGNAL"
        # C1:1 ↔ U1:4  (connection-specific DECOUPLING fires)
        assert resolve_pair_class(classes, {}, c1, "1", u1, "4", "VCC").name == "DECOUPLING"

    def test_tie_same_specificity_max_strength_wins(self):
        """A=PAD_GENERAL DECOUPLING, B=PAD_GENERAL PLANE → DECOUPLING wins
        (5.0 > 0.0)."""
        from commands.autoplacer import (
            DEFAULT_SPRING_CLASSES, resolve_pair_class,
        )
        a = _make_comp("C1", pin_classes={"1": "DECOUPLING"})
        b = _make_comp("U1", pin_classes={"4": "PLANE"})
        cls = resolve_pair_class(
            dict(DEFAULT_SPRING_CLASSES), {}, a, "1", b, "4", "VCC"
        )
        assert cls.name == "DECOUPLING"

    def test_force_is_equal_and_opposite(self):
        """Resolution returns the SAME class regardless of pair order —
        ensures Newton's third law (one k per pair)."""
        from commands.autoplacer import (
            DEFAULT_SPRING_CLASSES, resolve_pair_class,
        )
        a = _make_comp("C1", pin_classes={"1": {"U1.4": "DECOUPLING"}})
        b = _make_comp("U1")
        classes = dict(DEFAULT_SPRING_CLASSES)
        forward = resolve_pair_class(classes, {}, a, "1", b, "4", "VCC")
        reverse = resolve_pair_class(classes, {}, b, "4", a, "1", "VCC")
        assert forward.name == reverse.name == "DECOUPLING"
        assert forward.spring_k == reverse.spring_k

    def test_unknown_class_name_falls_through(self):
        """A typo'd class name shouldn't crash — it just gets ignored
        and the next-most-specific level wins."""
        from commands.autoplacer import (
            DEFAULT_SPRING_CLASSES, resolve_pair_class,
        )
        a = _make_comp("R1", pin_classes={"1": "TYPO_CLASS"})
        b = _make_comp("R2")
        cls = resolve_pair_class(
            dict(DEFAULT_SPRING_CLASSES), {}, a, "1", b, "2", "SIG"
        )
        assert cls.name == "LOCAL_SIGNAL"  # falls through to default

    def test_default_classes_have_expected_strengths(self):
        from commands.autoplacer import DEFAULT_SPRING_CLASSES
        assert DEFAULT_SPRING_CLASSES["DECOUPLING"].spring_k == 5.0
        assert DEFAULT_SPRING_CLASSES["LOCAL_SIGNAL"].spring_k == 1.0
        assert DEFAULT_SPRING_CLASSES["INTER_GROUP"].spring_k == 0.3
        assert DEFAULT_SPRING_CLASSES["PLANE"].spring_k == 0.0
