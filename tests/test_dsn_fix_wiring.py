"""Tests for `_rewrite_dsn_fix_wiring` — flips already-routed wires/vias
from `(type route)` to `(type fix)` so freerouting preserves existing
copper and only fills the open ratsnest (#240, preserveExistingTraces).

Pure string-rewrite tests — no pcbnew required.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


SAMPLE_DSN = """\
(pcb test.dsn
  (structure
    (layer F.Cu
      (type signal)
    )
    (layer In1.Cu
      (type power)
    )
    (rule (clearance 32.5 (type smd_smd)))
  )
  (wiring
    (wire (path F.Cu 1500  10 -10  10 -20)(net BAT+)(type route))
    (wire (path B.Cu 200  30 -30  40 -30)(net SDA)(type route))
    (via "Via0_600:300_um"  28771.8 -52373.1 (net SDA)(type route))
  )
)
"""

# No (wiring) section at all — an unrouted board export.
SAMPLE_DSN_NO_WIRING = """\
(pcb test.dsn
  (structure
    (layer F.Cu
      (type signal)
    )
    (rule (clearance 32.5 (type smd_smd)))
  )
)
"""


def test_wires_and_vias_flipped_to_fix():
    from commands.freerouting import _rewrite_dsn_fix_wiring

    out, count = _rewrite_dsn_fix_wiring(SAMPLE_DSN)
    # 2 wires + 1 via
    assert count == 3
    assert "(type route)" not in out
    assert out.count("(type fix)") == 3


def test_layer_and_clearance_types_untouched():
    from commands.freerouting import _rewrite_dsn_fix_wiring

    out, _ = _rewrite_dsn_fix_wiring(SAMPLE_DSN)
    # Only wiring element types change; structural types stay put.
    assert "(type signal)" in out
    assert "(type power)" in out
    assert "(type smd_smd)" in out
    # Net names and geometry preserved.
    assert "(net BAT+)" in out
    assert "(path F.Cu 1500  10 -10  10 -20)" in out
    assert '(via "Via0_600:300_um"' in out


def test_no_wiring_means_no_rewrite():
    from commands.freerouting import _rewrite_dsn_fix_wiring

    out, count = _rewrite_dsn_fix_wiring(SAMPLE_DSN_NO_WIRING)
    assert count == 0
    assert out == SAMPLE_DSN_NO_WIRING


def test_idempotent_second_pass_is_noop():
    from commands.freerouting import _rewrite_dsn_fix_wiring

    once, c1 = _rewrite_dsn_fix_wiring(SAMPLE_DSN)
    twice, c2 = _rewrite_dsn_fix_wiring(once)
    assert c1 == 3
    assert c2 == 0
    assert once == twice
