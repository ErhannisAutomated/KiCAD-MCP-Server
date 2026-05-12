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


def _has_junction_at(sch_path: Path, x: float, y: float) -> bool:
    """Inspect the file for a (junction (at X Y) …) entry at the
    given coord.  KiCad's connectivity engine only treats T-junction
    points as electrically connected when a junction marker is
    present; absence of one means the wires visually cross but are
    NOT on the same net."""
    import sexpdata
    from sexpdata import Symbol
    sexp = sexpdata.loads(sch_path.read_text())
    for item in sexp:
        if not (isinstance(item, list) and item and item[0] == Symbol("junction")):
            continue
        for sub in item[1:]:
            if (isinstance(sub, list) and sub and sub[0] == Symbol("at")
                    and len(sub) >= 3):
                if (abs(float(sub[1]) - x) < 1e-6
                        and abs(float(sub[2]) - y) < 1e-6):
                    return True
    return False


def _count_wires_split_at(sch_path: Path, x: float, y: float) -> int:
    """How many wires have an endpoint EXACTLY at (x, y).  Used to
    detect whether ``_break_wires_at_point`` actually split."""
    import sexpdata
    from sexpdata import Symbol
    sexp = sexpdata.loads(sch_path.read_text())
    n = 0
    for item in sexp:
        if not (isinstance(item, list) and item and item[0] == Symbol("wire")):
            continue
        for sub in item:
            if isinstance(sub, list) and sub and sub[0] == Symbol("pts"):
                for xy in sub[1:]:
                    if (isinstance(xy, list) and len(xy) >= 3
                            and xy[0] == Symbol("xy")
                            and abs(float(xy[1]) - x) < 1e-6
                            and abs(float(xy[2]) - y) < 1e-6):
                        n += 1
    return n


@pytest.mark.unit
def test_add_wire_does_not_silently_merge_two_unrelated_nets(
    tmp_path: Path,
) -> None:
    """Issue #74: with ``expected_net`` passed,
    ``_break_wires_at_point`` refuses to split an existing wire whose
    chain carries a foreign-net label.  Without the split the new
    wire's endpoint sits on the foreign wire's interior without a
    junction — KiCad treats the two wires as not electrically
    connected, which is the safe outcome.

    Verification via the kicad_sch file directly: no junction at the
    crossing point, and the original NET_A wire is intact (no split
    endpoint at (105, 100))."""
    sch = _write_with_one_labelled_wire(tmp_path)

    # New wire from (105, 95) → (105, 100); the endpoint (105, 100)
    # lands on the interior of the existing NET_A wire.  With
    # expected_net="NET_B", the split should be refused.
    assert WireManager.add_wire(
        sch, [105.0, 95.0], [105.0, 100.0],
        expected_net="NET_B",
    )
    assert WireManager.add_label(
        sch, "NET_B", [105.0, 95.0], label_type="label",
    )

    # KiCad-faithful check: no junction at the crossing point.
    assert not _has_junction_at(sch, 105.0, 100.0), (
        "A junction at (105, 100) means the foreign NET_A wire was "
        "split there and KiCad will treat both nets as merged.  "
        "Issue #74."
    )
    # And the NET_A wire endpoints are still (100, 100) and (110, 100)
    # only — no extra wire endpoint at the would-be split point.
    assert _count_wires_split_at(sch, 105.0, 100.0) <= 1, (
        "Existing NET_A wire was split at (105, 100) — _break_wires_at_point "
        "didn't refuse the cross-net split."
    )


@pytest.mark.unit
def test_add_wire_without_expected_net_preserves_old_split_behavior(
    tmp_path: Path,
) -> None:
    """Default ``expected_net=None`` preserves the pre-#74 behavior:
    ``_break_wires_at_point`` splits regardless of net.  Locks in the
    backwards-compatibility contract — callers that don't opt into the
    net-aware refusal still see the old (net-blind) splits.  Any
    cross-net merge that results is the caller's responsibility."""
    sch = _write_with_one_labelled_wire(tmp_path)

    assert WireManager.add_wire(sch, [105.0, 95.0], [105.0, 100.0])

    # Pre-#74: the split happened and sync_junctions added a junction.
    assert _has_junction_at(sch, 105.0, 100.0), (
        "Without expected_net the pre-#74 net-blind split should still "
        "happen and produce a junction; if not, the default behavior "
        "has changed unexpectedly."
    )


@pytest.mark.unit
def test_add_wire_is_idempotent_for_identical_repeated_calls(
    tmp_path: Path,
) -> None:
    """Calling WireManager.add_wire twice with the same endpoints
    (modulo direction) must result in only ONE wire entry in the
    file.  Regression for the duplicate-wire bug surfaced 2026-05-13
    on charger: two pair routes converging on a shared pin coord
    were both calling add_wire with identical final-segment args,
    leaving 3x copies of the same wire in the .kicad_sch."""
    p = tmp_path / "idempotent.kicad_sch"
    p.write_text(textwrap.dedent(
        """\
        (kicad_sch (version 20250114) (generator "test")
          (uuid 11111111-1111-1111-1111-111111111111)
          (paper "A4")
          (sheet_instances (path "/" (page "1")))
        )
        """
    ))

    # First call: lays the wire.
    assert WireManager.add_wire(p, [10.0, 10.0], [20.0, 10.0])
    # Second call with identical args: no-op (still returns True).
    assert WireManager.add_wire(p, [10.0, 10.0], [20.0, 10.0])
    # Third call with reversed direction: also no-op.
    assert WireManager.add_wire(p, [20.0, 10.0], [10.0, 10.0])

    text = p.read_text()
    n_wires = text.count("(wire ")
    assert n_wires == 1, (
        f"Idempotent add_wire produced {n_wires} entries; expected 1"
    )
