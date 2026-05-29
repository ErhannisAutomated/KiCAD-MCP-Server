"""Tests for the intent-group spring normalization helpers (#249).

`_intent_group_key` buckets a pin's connections (named target -> own group,
everything else -> the pin's catch-all). `_intent_group_reduce` does the
three-level average: within group -> across a pin's groups -> across the
component's pins. Pure functions — no pcbnew needed.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


class FakeComp:
    def __init__(self, ref, pin_classes=None):
        self.ref = ref
        self.pin_classes = pin_classes or {}


# --- _intent_group_key ------------------------------------------------------

def test_named_target_gets_own_group():
    from commands.autoplacer import _intent_group_key

    c16 = FakeComp("C16", {"1": {"U3.1": "DECOUPLING"}})
    u3 = FakeComp("U3")
    # Connection to the named target U3.1 -> its own group.
    assert _intent_group_key(c16, "1", u3, "1") == ("1", "t:U3.1")


def test_unnamed_connection_falls_to_catch_all():
    from commands.autoplacer import _intent_group_key

    c16 = FakeComp("C16", {"1": {"U3.1": "DECOUPLING"}})
    j1 = FakeComp("J1")
    # A different USB_VBUS pad not named in the annotation -> catch-all.
    assert _intent_group_key(c16, "1", j1, "5") == ("1", "*")


def test_pad_general_and_unannotated_are_catch_all():
    from commands.autoplacer import _intent_group_key

    other = FakeComp("U2")
    # bare-string pad-general annotation -> one catch-all group
    c = FakeComp("C1", {"1": "DECOUPLING"})
    assert _intent_group_key(c, "1", other, "3") == ("1", "*")
    # no annotation at all -> catch-all
    c2 = FakeComp("C2", {})
    assert _intent_group_key(c2, "2", other, "3") == ("2", "*")


# --- _intent_group_reduce ---------------------------------------------------

def test_empty_is_zero():
    from commands.autoplacer import _intent_group_reduce
    assert _intent_group_reduce({}) == (0.0, 0.0, 0.0)


def test_single_connection_passthrough():
    from commands.autoplacer import _intent_group_reduce
    buckets = {"1": {("1", "t:U3.1"): [(5.0, 0.0, 0.0)]}}
    assert _intent_group_reduce(buckets) == (5.0, 0.0, 0.0)


def test_decoupling_group_beats_aggregate_of_generic_pulls():
    """The crux (#249): one DECOUPLING connection (west) vs 14 generic
    rail pulls (each east). Summed, the 14 (14*0.3=4.2) rival the one
    (5.0) and flip the resultant; grouped+averaged, the decoupling group
    keeps half the budget and wins decisively."""
    from commands.autoplacer import _intent_group_reduce

    # Worst case: all 14 generic pulls point the SAME way (−x), opposing
    # the decoupling pull (+x).
    generic = [(-0.3, 0.0, 0.0)] * 14
    buckets = {
        "1": {
            ("1", "t:U3.1"): [(5.0, 0.0, 0.0)],   # DECOUPLING, +x
            ("1", "*"): generic,                   # 14 generic, −x
        }
    }
    fx, fy, tq = _intent_group_reduce(buckets)
    # group avgs: decoupling=(5,0), generic=(−0.3,0); pin = avg = (2.35,0)
    assert abs(fx - 2.35) < 1e-9
    assert fx > 0.0      # decoupling wins; NOT flipped by the 14 generic


def test_total_force_independent_of_pin_count():
    """Your 'mass' assertion: adding pins with the same per-pin force must
    not make the component move faster — the across-pins average holds the
    total at one pin's worth."""
    from commands.autoplacer import _intent_group_reduce

    one_pin = {"1": {("1", "*"): [(10.0, 0.0, 0.0)]}}
    two_pin = {
        "1": {("1", "*"): [(10.0, 0.0, 0.0)]},
        "2": {("2", "*"): [(10.0, 0.0, 0.0)]},
    }
    assert _intent_group_reduce(one_pin) == _intent_group_reduce(two_pin) == (10.0, 0.0, 0.0)


def test_larger_net_does_not_pull_harder():
    """Within a group, more connections are averaged, not summed — a
    10-member generic group pulls the same as a 2-member one (same dir)."""
    from commands.autoplacer import _intent_group_reduce

    small = {"1": {("1", "*"): [(1.0, 0.0, 0.0), (1.0, 0.0, 0.0)]}}
    big = {"1": {("1", "*"): [(1.0, 0.0, 0.0)] * 10}}
    assert _intent_group_reduce(small) == _intent_group_reduce(big) == (1.0, 0.0, 0.0)
