"""Tests for commands.schematic_inspect (diagnose_chains + compare_netlists)."""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

PYTHON_DIR = Path(__file__).parent.parent / "python"
sys.path.insert(0, str(PYTHON_DIR))

from commands.schematic_inspect import (
    compare_netlists,
    diagnose_chains,
    find_unrelated_wire_crossings,
)


_R_LIB = textwrap.dedent("""
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


def _write_sch(
    tmp: Path,
    name: str,
    *,
    symbols: list = (),
    wires: list = (),
    labels: list = (),
) -> Path:
    """Write a minimal .kicad_sch with given symbol blocks, wires, and labels.

    symbols: list of (ref, x, y, rotation)
    wires:   list of ((x1, y1), (x2, y2))
    labels:  list of (name, x, y) — all local labels.
    """
    parts = [
        '(kicad_sch (version 20250114) (generator "test")',
        '  (uuid 11111111-1111-1111-1111-111111111111)',
        '  (paper "A4")',
        _R_LIB,
    ]
    for ref, x, y, rot in symbols:
        parts.append(
            f'  (symbol (lib_id "Device:R") (at {x} {y} {rot}) (unit 1)'
        )
        parts.append(
            f'    (property "Reference" "{ref}" (at {x} {y} 0)'
            '      (effects (font (size 1.27 1.27))))'
        )
        parts.append(
            f'    (property "Value" "10k" (at {x} {y} 0)'
            '      (effects (font (size 1.27 1.27))))'
        )
        parts.append(
            f'    (instances (project "test" (path "/" (reference "{ref}") (unit 1))))'
        )
        parts.append('  )')
    for (x1, y1), (x2, y2) in wires:
        parts.append(
            f'  (wire (pts (xy {x1} {y1}) (xy {x2} {y2})) '
            '(stroke (width 0) (type default)) '
            '(uuid 11111111-1111-1111-1111-111111111111))'
        )
    for (label_name, x, y) in labels:
        parts.append(
            f'  (label "{label_name}" (at {x} {y} 0) '
            '(effects (font (size 1.27 1.27))) '
            '(uuid 22222222-2222-2222-2222-222222222222))'
        )
    parts.append('  (sheet_instances (path "/" (page "1")))')
    parts.append(')')
    p = tmp / name
    p.write_text("\n".join(parts))
    return p


@pytest.mark.unit
class TestDiagnoseChains:
    def test_empty_schematic_no_chains(self, tmp_path: Path):
        p = _write_sch(tmp_path, "empty.kicad_sch")
        result = diagnose_chains(p)
        assert result["success"]
        assert result["n_chains"] == 0
        assert result["chains"] == []
        assert result["flag_counts"] == {
            "DUPLICATE_LABELS": 0,
            "CROSS_NET": 0,
            "LOOP": 0,
        }

    def test_clean_single_chain_no_flags(self, tmp_path: Path):
        p = _write_sch(
            tmp_path,
            "clean.kicad_sch",
            wires=[((100.0, 100.0), (110.0, 100.0))],
            labels=[("SIG", 110.0, 100.0)],
        )
        result = diagnose_chains(p)
        assert result["n_chains"] == 1
        chain = result["chains"][0]
        assert chain["flags"] == []
        assert chain["labels"] == ["SIG"]
        assert chain["wire_count"] == 1

    def test_duplicate_labels_flagged(self, tmp_path: Path):
        p = _write_sch(
            tmp_path,
            "dup.kicad_sch",
            wires=[((100.0, 100.0), (110.0, 100.0))],
            labels=[("SIG", 100.0, 100.0), ("SIG", 110.0, 100.0)],
        )
        result = diagnose_chains(p)
        flags = result["chains"][0]["flags"]
        assert "DUPLICATE_LABELS" in flags
        assert result["flag_counts"]["DUPLICATE_LABELS"] == 1

    def test_cross_net_flagged(self, tmp_path: Path):
        p = _write_sch(
            tmp_path,
            "cross.kicad_sch",
            wires=[((100.0, 100.0), (110.0, 100.0))],
            labels=[("A", 100.0, 100.0), ("B", 110.0, 100.0)],
        )
        result = diagnose_chains(p)
        flags = result["chains"][0]["flags"]
        assert "CROSS_NET" in flags
        assert set(result["chains"][0]["labels"]) == {"A", "B"}

    def test_loop_flagged(self, tmp_path: Path):
        # Four wires forming a closed rectangle.
        p = _write_sch(
            tmp_path,
            "loop.kicad_sch",
            wires=[
                ((100.0, 100.0), (110.0, 100.0)),
                ((110.0, 100.0), (110.0, 110.0)),
                ((110.0, 110.0), (100.0, 110.0)),
                ((100.0, 110.0), (100.0, 100.0)),
            ],
        )
        result = diagnose_chains(p)
        assert result["n_chains"] == 1
        assert "LOOP" in result["chains"][0]["flags"]

    def test_filter_nets_returns_only_matching_chains(self, tmp_path: Path):
        p = _write_sch(
            tmp_path,
            "filt.kicad_sch",
            wires=[
                ((100.0, 100.0), (110.0, 100.0)),
                ((200.0, 100.0), (210.0, 100.0)),
            ],
            labels=[("KEEP", 100.0, 100.0), ("OTHER", 200.0, 100.0)],
        )
        result = diagnose_chains(p, filter_nets=["KEEP"])
        assert result["n_chains"] == 1
        assert result["chains"][0]["labels"] == ["KEEP"]


@pytest.mark.unit
class TestCompareNetlists:
    def test_identical_files_preserved(self, tmp_path: Path):
        p = _write_sch(
            tmp_path,
            "orig.kicad_sch",
            symbols=[("R1", 100.0, 100.0, 0), ("R2", 110.0, 100.0, 0)],
            wires=[((100.0, 103.81), (110.0, 103.81))],
            labels=[("SIG", 100.0, 103.81)],
        )
        result = compare_netlists(p, p)
        assert result["preserved"]
        assert result["net_mismatches"] == []
        assert result["missing_pins"] == []
        assert result["extra_pins"] == []

    def test_dropped_pin_flagged_as_mismatch(self, tmp_path: Path):
        # Orig: two R's wired together, both pin 2's on SIG.
        # (Device:R lib has pin 2 at local y=+3.81 in our test lib.)
        orig = _write_sch(
            tmp_path,
            "orig.kicad_sch",
            symbols=[("R1", 100.0, 100.0, 0), ("R2", 110.0, 100.0, 0)],
            wires=[((100.0, 103.81), (110.0, 103.81))],
            labels=[("SIG", 100.0, 103.81)],
        )
        # New: wire was lost; only R1/2 is on SIG via the label.
        new = _write_sch(
            tmp_path,
            "new.kicad_sch",
            symbols=[("R1", 100.0, 100.0, 0), ("R2", 110.0, 100.0, 0)],
            labels=[("SIG", 100.0, 103.81)],
        )
        result = compare_netlists(orig, new)
        assert not result["preserved"]
        mismatch = next(m for m in result["net_mismatches"] if m["net"] == "SIG")
        lost_pins = {(p["ref"], p["pin"]) for p in mismatch["lost"]}
        assert ("R2", "2") in lost_pins

    def test_added_pin_to_a_net_flagged(self, tmp_path: Path):
        orig = _write_sch(
            tmp_path,
            "orig.kicad_sch",
            symbols=[("R1", 100.0, 100.0, 0), ("R2", 110.0, 100.0, 0)],
            labels=[("SIG", 100.0, 103.81)],
        )
        new = _write_sch(
            tmp_path,
            "new.kicad_sch",
            symbols=[("R1", 100.0, 100.0, 0), ("R2", 110.0, 100.0, 0)],
            wires=[((100.0, 103.81), (110.0, 103.81))],
            labels=[("SIG", 100.0, 103.81)],
        )
        result = compare_netlists(orig, new)
        assert not result["preserved"]
        mismatch = next(m for m in result["net_mismatches"] if m["net"] == "SIG")
        added_pins = {(p["ref"], p["pin"]) for p in mismatch["added"]}
        assert ("R2", "2") in added_pins


@pytest.mark.unit
class TestFindUnrelatedWireCrossings:
    """Wrapper around _scan_unrelated_wire_crossings, exposed as the
    find_unrelated_wire_crossings MCP tool.  Same chain-aware net
    resolution as diagnose_chains, so same-net segments crossing each
    other don't get flagged."""

    def test_empty_schematic_no_crossings(self, tmp_path: Path):
        p = _write_sch(tmp_path, "empty.kicad_sch")
        r = find_unrelated_wire_crossings(p)
        assert r["success"]
        assert r["n_crossings"] == 0
        assert r["crossings"] == []

    def test_genuinely_unrelated_crossing_flagged(self, tmp_path: Path):
        p = _write_sch(
            tmp_path,
            "diff.kicad_sch",
            wires=[
                ((100.0, 90.0), (100.0, 110.0)),
                ((90.0, 100.0), (110.0, 100.0)),
            ],
            labels=[("NET_A", 100.0, 90.0), ("NET_B", 90.0, 100.0)],
        )
        r = find_unrelated_wire_crossings(p)
        assert r["n_crossings"] == 1
        assert r["crossings"][0]["point"] == [100.0, 100.0]
        # Each wire reports its chain labels.
        assert "NET_A" in r["crossings"][0]["wire_a_labels"]
        assert "NET_B" in r["crossings"][0]["wire_b_labels"]

    def test_same_net_crossing_not_flagged(self, tmp_path: Path):
        """Two USB_VBUS segments crossing each other — both reachable
        from one label via the wire-graph BFS — must not flag.
        Regression for the charger sheet's USB_VBUS NW-of-C17 case."""
        p = _write_sch(
            tmp_path,
            "samenet.kicad_sch",
            wires=[
                ((100.0, 90.0), (100.0, 110.0)),
                ((90.0, 100.0), (110.0, 100.0)),
                ((100.0, 90.0), (90.0, 100.0)),
                ((110.0, 100.0), (100.0, 110.0)),
            ],
            labels=[("USB_VBUS", 100.0, 90.0)],
        )
        r = find_unrelated_wire_crossings(p)
        assert r["n_crossings"] == 0


@pytest.mark.unit
class TestMcpHandlerDispatch:
    """Both new MCP tools should be routable from the interface."""

    def test_diagnose_chains_route_registered(self):
        from unittest.mock import patch
        with patch("kicad_interface.USE_IPC_BACKEND", False):
            from kicad_interface import KiCADInterface
            iface = KiCADInterface.__new__(KiCADInterface)
            iface.board = None
            KiCADInterface.__init__(iface)
        assert "diagnose_chains" in iface.command_routes
        assert "compare_netlists" in iface.command_routes
        assert "find_unrelated_wire_crossings" in iface.command_routes
