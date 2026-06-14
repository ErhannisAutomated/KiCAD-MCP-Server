"""Tests for repair_pad_rotations.

Two layers:
  1. Pure-logic unit tests for the rotation-math helpers. Run without
     real pcbnew via the conftest stub — these are the load-bearing
     guarantees for the detection algorithm.
  2. Integration tests through KiCADInterface.handle_command, gated on
     real pcbnew. Build a board with one clean + one corrupted
     instance of the same lib_id, audit (asserts the corruption is
     flagged), repair (asserts the fix), audit again (asserts clean).

Background: see python/commands/repair_pad_rotations.py docstring and
project memory ``mcp_server_issues`` entry "Bypassing pcbnew's
rotation API leaves pad local rotations stale".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.repair_pad_rotations import (  # noqa: E402
    _angle_close,
    _fingerprints_match,
    _is_uniform_rotation_drift,
    _wrap_deg,
)


def _real_pcbnew_available() -> bool:
    import pcbnew  # type: ignore

    return getattr(pcbnew, "GetBuildVersion", lambda: "")() != "9.0.0-stub"


class TestRotationMath:
    def test_wrap_deg_normalises_to_zero_360(self):
        assert _wrap_deg(0.0) == 0.0
        assert _wrap_deg(90.0) == 90.0
        assert _wrap_deg(360.0) == 0.0
        assert _wrap_deg(450.0) == 90.0
        assert _wrap_deg(-90.0) == 270.0
        assert _wrap_deg(-450.0) == 270.0

    def test_angle_close_handles_wrap(self):
        # Close-by-wrap: 0.1° and 359.9° are 0.2° apart, not 359.8°.
        assert _angle_close(0.1, 359.9, tol=0.5)
        assert not _angle_close(0.0, 1.0, tol=0.5)
        assert _angle_close(89.9, 90.1, tol=0.5)

    def test_angle_close_with_negative_inputs(self):
        # _wrap_deg should bring both into [0,360) first.
        assert _angle_close(-90.0, 270.0, tol=0.5)


class TestFingerprintMatching:
    def test_identical_fingerprints_match(self):
        a = {"1": 0.0, "2": 0.0, "3": 0.0}
        b = {"1": 0.0, "2": 0.0, "3": 0.0}
        assert _fingerprints_match(a, b)

    def test_uniform_drift_does_not_match(self):
        """Two instances with all pads off by the same delta should NOT
        match — that's the stale signature, not an equivalence."""
        a = {"1": 0.0, "2": 0.0, "3": 0.0}
        b = {"1": 90.0, "2": 90.0, "3": 90.0}
        assert not _fingerprints_match(a, b)

    def test_disjoint_pad_numbers_returns_false(self):
        a = {"1": 0.0, "2": 0.0}
        b = {"3": 0.0, "4": 0.0}
        assert not _fingerprints_match(a, b)

    def test_partial_overlap_uses_shared(self):
        """If only some pads are shared and they agree, match. Missing
        pad numbers (e.g. one instance dropped a thermal pad) shouldn't
        be a deal-breaker."""
        a = {"1": 0.0, "2": 0.0, "thermal": 0.0}
        b = {"1": 0.0, "2": 0.0}
        assert _fingerprints_match(a, b)


class TestUniformDriftDetection:
    def test_uniform_offset_returns_delta(self):
        ref = {"1": 0.0, "2": 0.0, "3": 0.0, "4": 0.0}
        stale = {"1": 90.0, "2": 90.0, "3": 90.0, "4": 90.0}
        delta = _is_uniform_rotation_drift(stale, ref)
        assert delta is not None
        assert _angle_close(delta, 90.0)

    def test_non_uniform_returns_none(self):
        ref = {"1": 0.0, "2": 0.0, "3": 0.0}
        stale = {"1": 90.0, "2": 180.0, "3": 90.0}
        assert _is_uniform_rotation_drift(stale, ref) is None

    def test_no_shared_pads_returns_none(self):
        ref = {"1": 0.0}
        stale = {"2": 90.0}
        assert _is_uniform_rotation_drift(stale, ref) is None

    def test_clean_state_returns_zero_delta(self):
        """Same fingerprint should report delta=0 (or close), since
        that's still 'uniform' — just with no shift."""
        ref = {"1": 0.0, "2": 0.0}
        stale = {"1": 0.0, "2": 0.0}
        assert _is_uniform_rotation_drift(stale, ref) == 0.0

    def test_offset_with_360_wrap(self):
        """Library footprint with rel=90 on every pad — rotated 270°
        cleanly, ends up at 360°≡0° rel. Should detect as uniform
        offset of 270."""
        ref = {"1": 90.0, "2": 90.0}
        stale = {"1": 0.0, "2": 0.0}  # i.e. ref shifted by 270°
        delta = _is_uniform_rotation_drift(stale, ref)
        assert delta is not None
        assert _angle_close(delta, 270.0)


class TestHandlerDispatchWiring:
    """Membership / wiring asserts that don't need real pcbnew. Pins
    the contract: tool is registered, mutating, and the module exports
    the expected class."""

    def test_command_dispatched_in_kicad_interface(self):
        from kicad_interface import KiCADInterface

        assert "repair_pad_rotations" in KiCADInterface._BOARD_MUTATING_COMMANDS

    def test_command_in_command_routes(self):
        import inspect

        from kicad_interface import KiCADInterface

        src = inspect.getsource(KiCADInterface.__init__) + inspect.getsource(
            KiCADInterface.handle_command
        )
        # Cheap structural check: the dispatch table line for the
        # command must exist in __init__ wiring.
        # (Walking the actual command_routes dict requires instance
        # construction which pulls more dependencies than this test
        # wants to depend on.)
        assert "repair_pad_rotations" in src

    def test_repair_pad_rotations_commands_class_shape(self):
        from commands.repair_pad_rotations import RepairPadRotationsCommands

        cmds = RepairPadRotationsCommands(board=None)
        result = cmds.repair_pad_rotations({})
        assert result["success"] is False
        assert "No board" in result["message"]


@pytest.mark.skipif(
    not _real_pcbnew_available(),
    reason="real pcbnew not available (test stub in use)",
)
class TestRepairPadRotationsIntegration:
    """End-to-end via real pcbnew. Build two instances of a fake
    library footprint; rotate one cleanly via the API, rotate the
    other via the regex-style header rewrite (simulated by setting
    fp.SetOrientation directly without propagating to pads); verify
    the stale instance is flagged and that repair fixes it."""

    def _make_fp(self, board, ref: str, x_mm: float):
        """4-pad rectangular footprint, library-clean (rel=0) at
        construction time. Shared by paired and mass-corruption
        fixtures."""
        import pcbnew

        fp = pcbnew.FOOTPRINT(board)
        fp.SetFPID(pcbnew.LIB_ID("test", "rpr_test"))
        fp.SetReference(ref)
        for i, (px, py) in enumerate([(-1.0, -1.0), (1.0, -1.0), (-1.0, 1.0), (1.0, 1.0)]):
            pad = pcbnew.PAD(fp)
            pad.SetNumber(str(i + 1))
            pad.SetShape(pcbnew.PAD_SHAPE_RECT)
            pad.SetSize(pcbnew.VECTOR2I(int(0.5e6), int(0.5e6)))
            pad.SetPosition(pcbnew.VECTOR2I(int(px * 1e6), int(py * 1e6)))
            pad.SetOrientationDegrees(0.0)
            fp.Add(pad)
        fp.SetPosition(pcbnew.VECTOR2I(int(x_mm * 1e6), 0))
        board.Add(fp)
        return fp

    def _force_stale(self, fp):
        """KiCAD 9's FOOTPRINT.SetOrientationDegrees auto-propagates to
        pads, so we have to reset pad rotations after rotating the
        footprint to recreate the apply_positions.py-style symptom."""
        fp.SetOrientationDegrees(90.0)
        for pad in fp.Pads():
            pad.SetOrientationDegrees(0.0)

    def _force_clean_rotated(self, fp):
        """Footprint rotated cleanly via API — explicit pad propagation
        to make the test robust across SWIG builds."""
        fp.SetOrientationDegrees(90.0)
        for pad in fp.Pads():
            pad.SetOrientationDegrees(90.0)

    def _make_board_with_paired_instances(self, tmp_path: Path):
        import pcbnew

        b = pcbnew.BOARD()
        b.SetFileName(str(tmp_path / "rpr_test.kicad_pcb"))
        clean = self._make_fp(b, "U1", x_mm=-10.0)
        stale = self._make_fp(b, "U2", x_mm=10.0)
        self._force_clean_rotated(clean)
        self._force_stale(stale)
        return b, clean, stale

    def _make_board_with_mass_corruption(self, tmp_path: Path, n: int = 3):
        """All N instances of the same lib_id are stale — the actual
        apply_positions.py incident. Cross-instance majority vote
        can't tell good from bad; tool should flag them all as
        suspect needing force=true."""
        import pcbnew

        b = pcbnew.BOARD()
        b.SetFileName(str(tmp_path / "rpr_mass.kicad_pcb"))
        fps = [self._make_fp(b, f"U{i}", x_mm=i * 10.0) for i in range(1, n + 1)]
        for fp in fps:
            self._force_stale(fp)
        return b, fps

    def test_audit_flags_stale_instance(self, tmp_path):
        from commands.repair_pad_rotations import RepairPadRotationsCommands

        b, _, _ = self._make_board_with_paired_instances(tmp_path)
        cmds = RepairPadRotationsCommands(board=b)
        result = cmds.repair_pad_rotations({"dryRun": True})

        assert result["success"]
        assert result["dryRun"] is True
        assert result["audited"] == 2
        # Exactly one instance flagged confirmed-stale.
        stale_refs = [e["ref"] for e in result["confirmed_stale"]]
        assert stale_refs == ["U2"]
        # And the uniform-drift detector reports 270° (pads at 0,
        # footprint at 90 → rel=270 vs ref=0).
        entry = result["confirmed_stale"][0]
        assert entry["uniform_offset_deg"] is not None
        assert abs(entry["uniform_offset_deg"] - 270.0) < 0.5

    def test_repair_restores_pad_rotations(self, tmp_path):
        import pcbnew

        from commands.repair_pad_rotations import RepairPadRotationsCommands

        b, _, stale = self._make_board_with_paired_instances(tmp_path)
        cmds = RepairPadRotationsCommands(board=b)
        result = cmds.repair_pad_rotations({"dryRun": False})

        assert result["success"]
        assert result["repaired"] == ["U2"]
        # All U2 pads now at 90° (matching the footprint).
        for pad in stale.Pads():
            assert abs(pad.GetOrientation().AsDegrees() - 90.0) < 0.5

        # Re-audit: clean board.
        re_audit = cmds.repair_pad_rotations({"dryRun": True})
        assert re_audit["confirmed_stale"] == []

    def test_refs_filter_scopes_repair(self, tmp_path):
        from commands.repair_pad_rotations import RepairPadRotationsCommands

        b, _, _ = self._make_board_with_paired_instances(tmp_path)
        cmds = RepairPadRotationsCommands(board=b)
        # Audit with refs=['U1'] — U1 is clean, no findings, no peer to
        # compare against either (refs filter applied before grouping).
        result = cmds.repair_pad_rotations({"dryRun": True, "refs": ["U1"]})
        assert result["audited"] == 1
        assert result["confirmed_stale"] == []

    def test_mass_corruption_audit_flags_all_as_suspect(self, tmp_path):
        """The original apply_positions.py incident: 57 of 71
        footprints stale, in many cases ALL instances of a lib_id are
        affected. Majority vote sees consensus but the consensus is
        wrong. Tool must flag the whole bucket as suspect (not
        confirmed-stale — we can't *prove* it without library
        reference), shifting the burden to the user via force=true."""
        from commands.repair_pad_rotations import RepairPadRotationsCommands

        b, fps = self._make_board_with_mass_corruption(tmp_path, n=3)
        cmds = RepairPadRotationsCommands(board=b)
        result = cmds.repair_pad_rotations({"dryRun": True})

        assert result["success"]
        # No instance is "confirmed stale" because there's no clean
        # peer to compare against.
        assert result["confirmed_stale"] == []
        # All three instances flagged as suspect with the same
        # uniform offset.
        suspect_refs = {e["ref"] for e in result["suspect_single_instance"]}
        assert suspect_refs == {"U1", "U2", "U3"}
        for entry in result["suspect_single_instance"]:
            assert entry["uniform_offset_deg"] is not None
            assert abs(entry["uniform_offset_deg"] - 270.0) < 0.5

    def test_mass_corruption_force_repairs_all(self, tmp_path):
        import pcbnew

        from commands.repair_pad_rotations import RepairPadRotationsCommands

        b, fps = self._make_board_with_mass_corruption(tmp_path, n=3)
        cmds = RepairPadRotationsCommands(board=b)

        # Without force, dryRun=False shouldn't touch suspects.
        no_force = cmds.repair_pad_rotations({"dryRun": False})
        assert no_force["repaired"] == []
        # State unchanged.
        for fp in fps:
            for pad in fp.Pads():
                assert abs(pad.GetOrientation().AsDegrees() - 0.0) < 0.5

        # With force, all suspects get repaired to fp_rot + 0.
        forced = cmds.repair_pad_rotations({"dryRun": False, "force": True})
        assert set(forced["repaired"]) == {"U1", "U2", "U3"}
        for fp in fps:
            for pad in fp.Pads():
                assert abs(pad.GetOrientation().AsDegrees() - 90.0) < 0.5

        # Re-audit: clean.
        re_audit = cmds.repair_pad_rotations({"dryRun": True})
        assert re_audit["confirmed_stale"] == []
        assert re_audit["suspect_single_instance"] == []

    def test_rel_zero_tie_break_prefers_clean_reference(self, tmp_path):
        """With 1 clean + 1 stale (a 50/50 size tie), the bucket whose
        rel-fingerprint is closest to zero MUST win as reference.
        Without this bias, picking the stale instance as reference
        propagates the corruption during repair."""
        from commands.repair_pad_rotations import RepairPadRotationsCommands

        b, _, _ = self._make_board_with_paired_instances(tmp_path)
        cmds = RepairPadRotationsCommands(board=b)
        result = cmds.repair_pad_rotations({"dryRun": True})

        assert result["success"]
        # U1 is the clean one (rel=0), U2 is stale (rel=270). U2 must
        # be flagged — never U1, regardless of iteration order.
        stale_refs = {e["ref"] for e in result["confirmed_stale"]}
        assert stale_refs == {"U2"}, (
            f"rel-zero bias broken — expected U2 stale, got {stale_refs}"
        )
