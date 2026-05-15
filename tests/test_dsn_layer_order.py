"""Tests for the DSN-layer-order rewrite helper used by autoroute /
export_dsn to bias freerouting's per-layer preference.

These are pure string-rewrite tests — no pcbnew required.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


SAMPLE_DSN = """\
(pcb power_module.dsn
  (parser
    (string_quote ")
  )
  (resolution mm 1000000)
  (unit mm)
  (structure
    (layer F.Cu
      (type signal)
      (property
        (index 0)
      )
    )
    (layer In1.Cu
      (type signal)
      (property
        (index 1)
      )
    )
    (layer In2.Cu
      (type signal)
      (property
        (index 2)
      )
    )
    (layer B.Cu
      (type signal)
      (property
        (index 3)
      )
    )
    (boundary
      (path pcb 0  92168.3 -2.36)
    )
  )
)
"""


def test_helper_promotes_bcu_when_swapped():
    from commands.freerouting import _rewrite_dsn_layer_order

    out = _rewrite_dsn_layer_order(
        SAMPLE_DSN, ["F.Cu", "B.Cu", "In1.Cu", "In2.Cu"]
    )
    # Order in the rewritten DSN should be F.Cu, B.Cu, In1.Cu, In2.Cu
    layers_in_order = re.findall(r"\(layer (\S+)\n      \(type signal\)", out)
    assert layers_in_order == ["F.Cu", "B.Cu", "In1.Cu", "In2.Cu"], layers_in_order


def test_helper_renumbers_indices_to_match_position():
    from commands.freerouting import _rewrite_dsn_layer_order

    out = _rewrite_dsn_layer_order(
        SAMPLE_DSN, ["F.Cu", "B.Cu", "In2.Cu", "In1.Cu"]
    )
    # Each block's (index N) should match its position 0..3
    blocks = re.findall(
        r"\(layer (\S+)\n      \(type signal\)\n      \(property\n"
        r"        \(index (\d+)\)",
        out,
    )
    assert blocks == [
        ("F.Cu", "0"),
        ("B.Cu", "1"),
        ("In2.Cu", "2"),
        ("In1.Cu", "3"),
    ], blocks


def test_helper_preserves_non_layer_content():
    from commands.freerouting import _rewrite_dsn_layer_order

    out = _rewrite_dsn_layer_order(SAMPLE_DSN, ["B.Cu", "F.Cu", "In1.Cu", "In2.Cu"])
    # Header and boundary must survive unchanged
    assert "(pcb power_module.dsn" in out
    assert "(resolution mm 1000000)" in out
    assert "(boundary" in out
    assert "(path pcb 0  92168.3 -2.36)" in out


def test_helper_rejects_non_permutation():
    from commands.freerouting import _rewrite_dsn_layer_order

    with pytest.raises(ValueError, match="permutation"):
        _rewrite_dsn_layer_order(SAMPLE_DSN, ["F.Cu", "B.Cu"])  # missing inners

    with pytest.raises(ValueError, match="permutation"):
        _rewrite_dsn_layer_order(
            SAMPLE_DSN, ["F.Cu", "B.Cu", "In1.Cu", "In2.Cu", "Extra.Cu"]
        )


def test_helper_rejects_dsn_without_layers():
    from commands.freerouting import _rewrite_dsn_layer_order

    with pytest.raises(ValueError, match="no \\(layer"):
        _rewrite_dsn_layer_order("(pcb foo (structure (boundary)))", ["F.Cu"])


def test_resolve_layer_order_uses_default_on_4_layer():
    from commands.freerouting import _resolve_layer_order, _DEFAULT_4LAYER_ORDER

    class FakeBoard:
        def GetCopperLayerCount(self):
            return 4

    assert _resolve_layer_order(FakeBoard(), None) == _DEFAULT_4LAYER_ORDER


def test_resolve_layer_order_respects_explicit_request():
    from commands.freerouting import _resolve_layer_order

    class FakeBoard:
        def GetCopperLayerCount(self):
            return 4

    explicit = ["B.Cu", "F.Cu", "In2.Cu", "In1.Cu"]
    assert _resolve_layer_order(FakeBoard(), explicit) == explicit


def test_resolve_layer_order_no_op_on_2_layer():
    from commands.freerouting import _resolve_layer_order

    class FakeBoard:
        def GetCopperLayerCount(self):
            return 2

    assert _resolve_layer_order(FakeBoard(), None) is None
