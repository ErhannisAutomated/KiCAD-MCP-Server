"""
Tests for ``get_all_net_connections`` — the bulk equivalent of
``get_connections_for_net`` introduced to keep ``decoupling_audit`` fast
on hierarchical schematics (was O(nets * sheets) sexp reparse).

The bulk function must return the same answer as calling
``get_connections_for_net`` once per net, just much faster.  These tests
exercise that equivalence on a real on-disk fixture so the heavy code
paths (sexp parse, wire adjacency, label parse, symbol-instance walk)
are all hit.
"""

import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.schematic import SchematicManager  # noqa: E402
from commands.wire_connectivity import (  # noqa: E402
    get_all_net_connections,
    get_connections_for_net,
)


_LIB_R = textwrap.dedent("""\
      (lib_symbols
        (symbol "TestLib:R"
          (symbol "TestLib:R_1_1"
            (pin passive line (at -1.27 0 0) (length 0)
              (name "~" (effects (font (size 1.27 1.27))))
              (number "1" (effects (font (size 1.27 1.27))))
            )
            (pin passive line (at 1.27 0 180) (length 0)
              (name "~" (effects (font (size 1.27 1.27))))
              (number "2" (effects (font (size 1.27 1.27))))
            )
          )
        )
      )
""")


def _sym_block(ref: str, x: float, y: float, idx: int) -> str:
    uuid = f"00000000-0000-0000-0000-{idx:012d}"
    return (
        f'  (symbol (lib_id "TestLib:R") (at {x} {y} 0) (unit 1)\n'
        f"    (uuid {uuid})\n"
        f'    (property "Reference" "{ref}" (at {x} {y - 2.54} 0))\n'
        f'    (property "Value" "~" (at {x} {y + 2.54} 0))\n'
        f'    (pin "1" (uuid {uuid[:-1]}a))\n'
        f'    (pin "2" (uuid {uuid[:-1]}b))\n'
        "  )\n"
    )


def _label_block(name: str, x: float, y: float, idx: int) -> str:
    uuid = f"11111111-1111-1111-1111-{idx:012d}"
    return (
        f'  (label "{name}" (at {x} {y} 0)\n'
        f"    (effects (font (size 1.27 1.27)))\n"
        f"    (uuid {uuid})\n"
        "  )\n"
    )


def _wire_block(x1: float, y1: float, x2: float, y2: float, idx: int) -> str:
    uuid = f"22222222-2222-2222-2222-{idx:012d}"
    return (
        f'  (wire (pts (xy {x1} {y1}) (xy {x2} {y2}))\n'
        f"    (stroke (width 0) (type default))\n"
        f"    (uuid {uuid})\n"
        "  )\n"
    )


def _write_sheet(
    path: Path,
    symbols: "list[tuple[str, float, float]]" = (),
    labels: "list[tuple[str, float, float]]" = (),
    wires: "list[tuple[float, float, float, float]]" = (),
) -> None:
    parts = ['(kicad_sch (version 20231120)', _LIB_R]
    for i, (ref, x, y) in enumerate(symbols, start=1):
        parts.append(_sym_block(ref, x, y, i))
    for i, (name, x, y) in enumerate(labels, start=1):
        parts.append(_label_block(name, x, y, i))
    for i, w in enumerate(wires, start=1):
        parts.append(_wire_block(*w, idx=i))
    parts.append(")\n")
    path.write_text("".join(parts))


def _normalise(pin_list):
    return frozenset((p["component"], str(p["pin"])) for p in pin_list)


@pytest.mark.unit
class TestBulkMatchesPerNet:
    """get_all_net_connections must return the same pin set as the per-net call."""

    def test_label_at_pin_endpoint(self, tmp_path: Path) -> None:
        # R1 pin 1 at (10 - 1.27, 10) = (8.73, 10); label at that point.
        sch = tmp_path / "t.kicad_sch"
        _write_sheet(
            sch,
            symbols=[("R1", 10.0, 10.0)],
            labels=[("NET_A", 8.73, 10.0)],
        )
        top = SchematicManager.load_schematic(str(sch))

        bulk = {k: _normalise(v) for k, v in get_all_net_connections(top, str(sch)).items()}
        per_net = _normalise(get_connections_for_net(top, str(sch), "NET_A"))
        assert bulk["NET_A"] == per_net
        assert ("R1", "1") in bulk["NET_A"]

    def test_label_propagates_via_wire(self, tmp_path: Path) -> None:
        sch = tmp_path / "t.kicad_sch"
        _write_sheet(
            sch,
            symbols=[("R1", 10.0, 10.0)],
            labels=[("VIA_WIRE", 5.0, 10.0)],
            wires=[(5.0, 10.0, 8.73, 10.0)],
        )
        top = SchematicManager.load_schematic(str(sch))

        bulk = {k: _normalise(v) for k, v in get_all_net_connections(top, str(sch)).items()}
        per_net = _normalise(get_connections_for_net(top, str(sch), "VIA_WIRE"))
        assert bulk["VIA_WIRE"] == per_net
        assert ("R1", "1") in bulk["VIA_WIRE"]

    def test_multiple_nets_one_sheet(self, tmp_path: Path) -> None:
        """Two distinct nets at the same symbol — both pin assignments are correct."""
        sch = tmp_path / "t.kicad_sch"
        _write_sheet(
            sch,
            symbols=[("R1", 10.0, 10.0)],
            labels=[
                ("LEFT", 8.73, 10.0),    # pin 1
                ("RIGHT", 11.27, 10.0),  # pin 2
            ],
        )
        top = SchematicManager.load_schematic(str(sch))

        bulk = {k: _normalise(v) for k, v in get_all_net_connections(top, str(sch)).items()}
        for net in ("LEFT", "RIGHT"):
            ref = _normalise(get_connections_for_net(top, str(sch), net))
            assert bulk[net] == ref, f"mismatch on net {net!r}"
        assert ("R1", "1") in bulk["LEFT"]
        assert ("R1", "2") in bulk["RIGHT"]

    def test_shared_rail_across_symbols(self, tmp_path: Path) -> None:
        """Three Rs all on a shared rail via labels — all pins on net."""
        sch = tmp_path / "t.kicad_sch"
        _write_sheet(
            sch,
            symbols=[
                ("R1", 10.0, 10.0),
                ("R2", 20.0, 10.0),
                ("R3", 30.0, 10.0),
            ],
            labels=[
                ("RAIL", 8.73, 10.0),
                ("RAIL", 18.73, 10.0),
                ("RAIL", 28.73, 10.0),
            ],
        )
        top = SchematicManager.load_schematic(str(sch))

        bulk = {k: _normalise(v) for k, v in get_all_net_connections(top, str(sch)).items()}
        per_net = _normalise(get_connections_for_net(top, str(sch), "RAIL"))
        assert bulk["RAIL"] == per_net
        assert bulk["RAIL"] >= {("R1", "1"), ("R2", "1"), ("R3", "1")}

    def test_empty_sheet(self, tmp_path: Path) -> None:
        sch = tmp_path / "t.kicad_sch"
        _write_sheet(sch)
        top = SchematicManager.load_schematic(str(sch))
        assert get_all_net_connections(top, str(sch)) == {}

    def test_label_with_no_pin_nearby(self, tmp_path: Path) -> None:
        """Label that does not touch any pin endpoint → empty pin list for that net."""
        sch = tmp_path / "t.kicad_sch"
        _write_sheet(
            sch,
            symbols=[("R1", 10.0, 10.0)],
            labels=[("FLOATING", 100.0, 100.0)],
        )
        top = SchematicManager.load_schematic(str(sch))

        bulk = get_all_net_connections(top, str(sch))
        per_net = get_connections_for_net(top, str(sch), "FLOATING")
        assert bulk.get("FLOATING", []) == []
        assert per_net == []
