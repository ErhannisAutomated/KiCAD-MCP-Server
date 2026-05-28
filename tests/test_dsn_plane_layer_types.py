"""Tests for `_rewrite_dsn_plane_layer_types` — flips layers hosting a
`(plane …)` declaration from `(type signal)` to `(type power)` so
freerouting doesn't route signals across power planes.

These are pure string-rewrite tests — no pcbnew required.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


SAMPLE_DSN = """\
(pcb test.dsn
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
    (layer B.Cu
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
    (layer In1.Cu
      (type signal)
      (property
        (index 3)
      )
    )
    (boundary
      (path pcb 0  0 0)
    )
    (plane GND (polygon In1.Cu 0  0 0  100 0  100 -100  0 -100  0 0))
    (plane BAT+ (polygon In2.Cu 0  0 0  100 0  100 -100  0 -100  0 0))
    (rule (width 200) (clearance 130))
  )
)
"""

SAMPLE_DSN_NO_PLANES = """\
(pcb test.dsn
  (structure
    (layer F.Cu
      (type signal)
      (property
        (index 0)
      )
    )
    (layer B.Cu
      (type signal)
      (property
        (index 1)
      )
    )
    (boundary
      (path pcb 0  0 0)
    )
  )
)
"""


def test_plane_layers_get_flipped_to_power():
    from commands.freerouting import _rewrite_dsn_plane_layer_types

    out, layers = _rewrite_dsn_plane_layer_types(SAMPLE_DSN)
    assert sorted(layers) == ["In1.Cu", "In2.Cu"]
    # Plane layers should now be (type power)
    assert re.search(r"\(layer In1.Cu\n\s+\(type power\)", out)
    assert re.search(r"\(layer In2.Cu\n\s+\(type power\)", out)
    # Signal layers stay (type signal)
    assert re.search(r"\(layer F.Cu\n\s+\(type signal\)", out)
    assert re.search(r"\(layer B.Cu\n\s+\(type signal\)", out)


def test_no_planes_means_no_rewrite():
    from commands.freerouting import _rewrite_dsn_plane_layer_types

    out, layers = _rewrite_dsn_plane_layer_types(SAMPLE_DSN_NO_PLANES)
    assert layers == []
    assert out == SAMPLE_DSN_NO_PLANES


def test_non_plane_content_preserved():
    from commands.freerouting import _rewrite_dsn_plane_layer_types

    out, _ = _rewrite_dsn_plane_layer_types(SAMPLE_DSN)
    # Plane declarations themselves stay intact
    assert "(plane GND (polygon In1.Cu" in out
    assert "(plane BAT+ (polygon In2.Cu" in out
    # Boundary and rules survive
    assert "(boundary" in out
    assert "(rule (width 200) (clearance 130))" in out
    # Indices unchanged
    for i, name in enumerate(["F.Cu", "B.Cu", "In2.Cu", "In1.Cu"]):
        assert re.search(
            r"\(layer " + re.escape(name) + r"\n\s+\(type \w+\)\n"
            r"\s+\(property\n\s+\(index " + str(i) + r"\)",
            out,
        ), f"layer {name} index {i} not preserved"


def test_idempotent_on_already_power_layers():
    """Running the rewrite twice should be a no-op the second time."""
    from commands.freerouting import _rewrite_dsn_plane_layer_types

    once, _ = _rewrite_dsn_plane_layer_types(SAMPLE_DSN)
    twice, layers = _rewrite_dsn_plane_layer_types(once)
    # Layers still detected
    assert sorted(layers) == ["In1.Cu", "In2.Cu"]
    # But text unchanged
    assert once == twice


def test_only_layer_hosting_a_plane_is_flipped():
    """If only In1.Cu has a plane, In2.Cu should stay signal."""
    from commands.freerouting import _rewrite_dsn_plane_layer_types

    one_plane = SAMPLE_DSN.replace(
        "    (plane BAT+ (polygon In2.Cu 0  0 0  100 0  100 -100  0 -100  0 0))\n",
        "",
    )
    out, layers = _rewrite_dsn_plane_layer_types(one_plane)
    assert layers == ["In1.Cu"]
    assert re.search(r"\(layer In1.Cu\n\s+\(type power\)", out)
    assert re.search(r"\(layer In2.Cu\n\s+\(type signal\)", out)


def test_outer_layers_never_flipped():
    """A GND pour on F.Cu / B.Cu must NOT flip them to power — they're the
    primary routing layers. (#241: flipping all copper layers left
    freerouting nothing to route on and wiped the board.)"""
    from commands.freerouting import _rewrite_dsn_plane_layer_types

    # Add GND pours on both outer layers in addition to the inner planes.
    with_outer = SAMPLE_DSN.replace(
        "    (plane GND (polygon In1.Cu 0  0 0  100 0  100 -100  0 -100  0 0))\n",
        "    (plane GND (polygon In1.Cu 0  0 0  100 0  100 -100  0 -100  0 0))\n"
        "    (plane GND (polygon F.Cu 0  0 0  100 0  100 -100  0 -100  0 0))\n"
        "    (plane GND (polygon B.Cu 0  0 0  100 0  100 -100  0 -100  0 0))\n",
    )
    out, layers = _rewrite_dsn_plane_layer_types(with_outer)
    # Only the inner planes flip; outer layers stay routable.
    assert sorted(layers) == ["In1.Cu", "In2.Cu"]
    assert re.search(r"\(layer F.Cu\n\s+\(type signal\)", out)
    assert re.search(r"\(layer B.Cu\n\s+\(type signal\)", out)
    assert re.search(r"\(layer In1.Cu\n\s+\(type power\)", out)
    assert re.search(r"\(layer In2.Cu\n\s+\(type power\)", out)
