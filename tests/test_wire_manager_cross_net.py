"""Cross-net merge regression test for WireManager.add_wire.

Issue #74: WireManager._break_wires_at_point splits any existing wire
whose interior contains a new wire's endpoint, with no awareness of
which net each wire is on.  After the split, ``sync_junctions`` adds
a junction at the shared coord; KiCad's connectivity graph then sees
the two wires as electrically connected, and any labels on the
existing wire's net silently leak onto the new wire's net.

The fix surface is in ``commands/wire_manager.py``:
  - thread an optional ``expected_net`` through ``add_wire``,
  - in ``_break_wires_at_point``, compute the existing wire's
    inferred net via ``walk_wire_chain`` and refuse the split when
    it carries a label of a different net than ``expected_net``.

Until that lands, this test is ``@pytest.mark.xfail(strict=True)``.
When the fix is in place and the test passes, remove the marker.
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

PYTHON_DIR = Path(__file__).parent.parent / "python"
sys.path.insert(0, str(PYTHON_DIR))

from commands.connection_schematic import ConnectionManager
from commands.wire_manager import WireManager


def _write_with_one_labelled_wire(tmp: Path) -> Path:
    """Pre-populated schematic with exactly one wire, labelled NET_A.

    Wire: (100, 100) → (110, 100), label NET_A at (100, 100).
    """
    p = tmp / "cross_net.kicad_sch"
    p.write_text(textwrap.dedent(
        """\
        (kicad_sch (version 20250114) (generator "test")
          (uuid 11111111-1111-1111-1111-111111111111)
          (paper "A4")
          (wire (pts (xy 100.0 100.0) (xy 110.0 100.0))
            (stroke (width 0) (type default))
            (uuid 11111111-1111-1111-1111-111111111111))
          (label "NET_A" (at 100.0 100.0 0)
            (effects (font (size 1.27 1.27)))
            (uuid 22222222-2222-2222-2222-222222222222))
          (sheet_instances (path "/" (page "1")))
        )
        """
    ))
    return p


@pytest.mark.unit
@pytest.mark.xfail(
    strict=True,
    reason=(
        "Issue #74: WireManager._break_wires_at_point is net-blind. "
        "Adding a wire whose endpoint lands on the interior of a "
        "wire belonging to NET_A causes a junction to be inserted "
        "and KiCad merges the two nets.  Fix is to thread "
        "expected_net through add_wire and refuse cross-net splits."
    ),
)
def test_add_wire_does_not_silently_merge_two_unrelated_nets(
    tmp_path: Path,
) -> None:
    sch = _write_with_one_labelled_wire(tmp_path)

    # Sanity: the pre-existing wire has both endpoints on NET_A.
    # (We can't easily probe via get_pin_net without a component, so
    # we'll rely on the post-condition check after adding the new
    # wire.  This step is just for the reader.)

    # Add a new wire from (105, 95) to (105, 100).  The endpoint
    # (105, 100) lands strictly on the INTERIOR of the existing
    # (100,100)→(110,100) wire.
    #
    # The caller intends this new wire to be on NET_B (and will
    # follow up with a NET_B label at (105, 95)).  But before the
    # caller can even add that label, _break_wires_at_point will
    # split the NET_A wire at (105, 100) and sync_junctions will
    # add a junction — fusing the two endpoints onto a single
    # KiCad net.
    assert WireManager.add_wire(
        sch, [105.0, 95.0], [105.0, 100.0],
    )

    # Now add the NET_B label at the new wire's free end.  At this
    # point a correct add_wire would have prevented the merge (or
    # the new wire would have been refused, returning False, in
    # which case the assertion above would have failed first).
    assert WireManager.add_label(
        sch, "NET_B", [105.0, 95.0], label_type="label",
    )

    # Post-condition: query the inferred net for several points.
    # Any one of NET_A's original wire endpoints should remain on
    # NET_A; the new wire's free end (105, 95) should be on NET_B.
    # If the merge happened, all four query points come back on the
    # SAME net (whichever label resolves first — likely NET_A as
    # the earlier label).
    from commands.wire_connectivity import walk_wire_chain

    chain_a = walk_wire_chain((100.0, 100.0), sch)
    chain_b = walk_wire_chain((105.0, 95.0), sch)

    assert chain_a is not None and chain_b is not None
    # The whole bug: with the net-blind split, chain_a and chain_b
    # share wire indices (they're the same physical component).
    # With a net-aware add_wire, the two chains stay disjoint.
    assert chain_a.wire_indices.isdisjoint(chain_b.wire_indices), (
        "Cross-net merge: new wire ended on NET_A's interior and got "
        "junction'd to it.  See issue #74.  Once add_wire refuses "
        "this kind of split, the chains stay disjoint and this "
        "assertion passes."
    )
