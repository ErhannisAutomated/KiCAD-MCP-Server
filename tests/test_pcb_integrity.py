"""Tests for the check_pcb_integrity subchecks.

The subchecks operate on a pcbnew BOARD object, so we mock just enough
of the BOARD/FOOTPRINT/PAD/VECTOR2I surface to exercise each rule.
Avoids needing real pcbnew (which isn't always available in CI)."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

# Stub pcbnew before importing the module under test.
sys.modules.setdefault("pcbnew", MagicMock())

from commands.integrity import (  # noqa: E402
    check_footprint_overlap,
    check_pad_rotation,
    check_stacked_pads,
    run_integrity_checks,
)


def _mk_pos(x_mm: float, y_mm: float):
    return SimpleNamespace(x=int(x_mm * 1_000_000), y=int(y_mm * 1_000_000))


def _mk_bbox(left_mm: float, top_mm: float, right_mm: float, bottom_mm: float):
    nm = 1_000_000
    return SimpleNamespace(
        GetLeft=lambda: int(left_mm * nm),
        GetTop=lambda: int(top_mm * nm),
        GetRight=lambda: int(right_mm * nm),
        GetBottom=lambda: int(bottom_mm * nm),
        GetWidth=lambda: int((right_mm - left_mm) * nm),
        GetHeight=lambda: int((bottom_mm - top_mm) * nm),
    )


def _mk_pad(number: str, pos_mm: tuple, rot_deg: float = 0.0, net: str = ""):
    pad = MagicMock()
    pad.GetNumber.return_value = number
    pad.GetPosition.return_value = _mk_pos(*pos_mm)
    pad.GetNetname.return_value = net
    orient = MagicMock()
    orient.AsDegrees.return_value = rot_deg
    pad.GetOrientation.return_value = orient
    return pad


def _mk_fp(ref: str, pos_mm: tuple, rot_deg: float = 0.0,
           pads: list = None, lib_id: str = "Foo:Bar",
           layer: int = 0, bbox: tuple = None):
    fp = MagicMock()
    fp.GetReference.return_value = ref
    fp.GetPosition.return_value = _mk_pos(*pos_mm)
    orient = MagicMock()
    orient.AsDegrees.return_value = rot_deg
    fp.GetOrientation.return_value = orient
    fp.Pads.return_value = pads or []
    fp.GetLayer.return_value = layer
    if bbox is None:
        # Default bbox: small box around the centre.
        cx, cy = pos_mm
        bbox = (cx - 1, cy - 1, cx + 1, cy + 1)
    fp.GetBoundingBox.return_value = _mk_bbox(*bbox)
    fpid = MagicMock()
    fpid.GetUniStringLibId.return_value = lib_id
    fp.GetFPID.return_value = fpid
    return fp


def _mk_board(footprints: list, layer_names: dict = None):
    board = MagicMock()
    board.GetFootprints.return_value = footprints
    layer_names = layer_names or {0: "F.Cu", 31: "B.Cu"}
    board.GetLayerName.side_effect = lambda lid: layer_names.get(lid, f"layer{lid}")
    return board


class TestCheckPadRotation:
    def test_no_findings_when_only_one_instance(self):
        # Single instance can't be compared multi-instance — should
        # produce no findings (deepLibraryCheck would, but it's not
        # exposed in v1).
        fp = _mk_fp("R1", (10, 10), rot_deg=90, pads=[
            _mk_pad("1", (10, 10), rot_deg=90),
            _mk_pad("2", (12, 10), rot_deg=90),
        ])
        board = _mk_board([fp])
        assert check_pad_rotation(board) == []

    def test_uniform_drift_is_warning(self):
        # R12: fp_rot=90, pad_rot=90 -> rel=0
        # R26: fp_rot=0, pad_rot=270 -> rel=270 (same delta on both pads)
        r12 = _mk_fp("R12", (0, 0), rot_deg=90, pads=[
            _mk_pad("1", (0, 0), rot_deg=90),
            _mk_pad("2", (2, 0), rot_deg=90),
        ])
        r26 = _mk_fp("R26", (10, 0), rot_deg=0, pads=[
            _mk_pad("1", (10, 0), rot_deg=270),
            _mk_pad("2", (12, 0), rot_deg=270),
        ])
        board = _mk_board([r12, r26])
        findings = check_pad_rotation(board)
        assert len(findings) == 1
        assert findings[0]["ref"] == "R26"
        assert findings[0]["severity"] == "warning"
        assert findings[0]["uniform_drift"] is True

    def test_mixed_drift_is_error(self):
        # The catastrophic case: different pads rotated differently.
        good = _mk_fp("U1", (0, 0), rot_deg=0, pads=[
            _mk_pad("1", (0, 0), rot_deg=0),
            _mk_pad("2", (2, 0), rot_deg=0),
            _mk_pad("3", (4, 0), rot_deg=0),
        ])
        bad = _mk_fp("U2", (10, 0), rot_deg=0, pads=[
            _mk_pad("1", (10, 0), rot_deg=90),    # delta=90
            _mk_pad("2", (12, 0), rot_deg=180),   # delta=180
            _mk_pad("3", (14, 0), rot_deg=270),   # delta=270
        ])
        board = _mk_board([good, bad])
        findings = check_pad_rotation(board)
        assert len(findings) == 1
        assert findings[0]["ref"] == "U2"
        assert findings[0]["severity"] == "error"
        assert findings[0]["uniform_drift"] is False
        assert len(findings[0]["mismatches"]) == 3

    def test_matching_instances_no_finding(self):
        r1 = _mk_fp("R1", (0, 0), rot_deg=0, pads=[_mk_pad("1", (0, 0), 0)])
        r2 = _mk_fp("R2", (10, 0), rot_deg=0, pads=[_mk_pad("1", (10, 0), 0)])
        board = _mk_board([r1, r2])
        assert check_pad_rotation(board) == []


class TestCheckFootprintOverlap:
    def test_c24_inside_l2_case(self):
        # L2: inductor at (62, 80), bbox 5×5 mm
        l2 = _mk_fp("L2", (62, 80), bbox=(59.5, 77.5, 64.5, 82.5))
        # C24 placed inside L2's bbox — the 2026-05-14 bug.
        c24 = _mk_fp("C24", (62, 80), bbox=(61, 79, 63, 81))
        board = _mk_board([l2, c24])
        findings = check_footprint_overlap(board)
        # Both detect each other since both centres are inside the other's bbox.
        refs = sorted([(f["ref"], f["inside_of"]) for f in findings])
        assert ("C24", "L2") in refs

    def test_same_layer_required(self):
        # Front-side cap "inside" back-side battery holder is fine.
        bat = _mk_fp("BAT1", (50, 50), layer=31, bbox=(0, 0, 100, 100))
        cap = _mk_fp("C1", (50, 50), layer=0, bbox=(49, 49, 51, 51))
        board = _mk_board([bat, cap])
        assert check_footprint_overlap(board) == []

    def test_no_overlap_when_disjoint(self):
        a = _mk_fp("R1", (0, 0), bbox=(-1, -1, 1, 1))
        b = _mk_fp("R2", (10, 0), bbox=(9, -1, 11, 1))
        board = _mk_board([a, b])
        assert check_footprint_overlap(board) == []


class TestCheckStackedPads:
    def test_usb_c_gnd_pads_not_flagged(self):
        # USB-C convention: A1 and B12 both on GND, deliberately stacked.
        fp = _mk_fp("J1", (0, 0), pads=[
            _mk_pad("A1", (0, 0), net="GND"),
            _mk_pad("B12", (0, 0), net="GND"),
        ])
        board = _mk_board([fp])
        assert check_stacked_pads(board) == []

    def test_thermal_pad_copies_not_flagged(self):
        # Multiple pads numbered "29" at different positions = OK.
        # Multiple "29" pads at the same position = OK (same number).
        fp = _mk_fp("U4", (0, 0), pads=[
            _mk_pad("29", (0, 0), net="GND"),
            _mk_pad("29", (0, 0), net="GND"),
        ])
        board = _mk_board([fp])
        assert check_stacked_pads(board) == []

    def test_mounting_pads_not_flagged(self):
        # Empty pad number = mounting/silk pad.
        fp = _mk_fp("U4", (0, 0), pads=[
            _mk_pad("", (0, 0), net=""),
            _mk_pad("", (0, 0), net=""),
        ])
        board = _mk_board([fp])
        assert check_stacked_pads(board) == []

    def test_real_corruption_flagged(self):
        # 2 differently-numbered pads on different nets at the same XY.
        # This only happens via the 2026-05-14 bug.
        fp = _mk_fp("U99", (0, 0), pads=[
            _mk_pad("1", (0, 0), net="VCC"),
            _mk_pad("2", (0, 0), net="GND"),
        ])
        board = _mk_board([fp])
        findings = check_stacked_pads(board)
        assert len(findings) == 1
        assert findings[0]["ref"] == "U99"
        assert findings[0]["severity"] == "error"
        assert findings[0]["pad_a"] == "1"
        assert findings[0]["pad_b"] == "2"

    def test_distinct_positions_no_finding(self):
        fp = _mk_fp("R1", (0, 0), pads=[
            _mk_pad("1", (0, 0)),
            _mk_pad("2", (2, 0)),
        ])
        board = _mk_board([fp])
        assert check_stacked_pads(board) == []


class TestRunIntegrityChecks:
    def test_default_runs_all(self):
        fp = _mk_fp("R1", (0, 0), pads=[_mk_pad("1", (0, 0))])
        board = _mk_board([fp])
        result = run_integrity_checks(board)
        assert result["success"] is True
        assert set(result["checks_run"]) == {"pad_rotation", "footprint_overlap", "stacked_pads"}

    def test_subset_filter(self):
        fp = _mk_fp("R1", (0, 0), pads=[_mk_pad("1", (0, 0))])
        board = _mk_board([fp])
        result = run_integrity_checks(board, checks=["stacked_pads"])
        assert result["checks_run"] == ["stacked_pads"]
        assert "pad_rotation" not in result["summary"]["by_check"]

    def test_unknown_check_rejected(self):
        board = _mk_board([])
        result = run_integrity_checks(board, checks=["bogus"])
        assert result["success"] is False
        assert "Unknown" in result["message"]
        assert "available" in result

    def test_summary_counts(self):
        # Provoke one warning via pad_rotation uniform drift.
        r1 = _mk_fp("R1", (0, 0), rot_deg=90, pads=[_mk_pad("1", (0, 0), rot_deg=90)])
        r2 = _mk_fp("R2", (10, 0), rot_deg=0, pads=[_mk_pad("1", (10, 0), rot_deg=270)])
        board = _mk_board([r1, r2])
        result = run_integrity_checks(board)
        assert result["summary"]["by_severity"]["warning"] == 1
        assert result["summary"]["by_severity"]["error"] == 0
        assert result["total"] == 1
