"""Tests for the pin-escape support added to route_pad_to_pad (#178).

`route_pad_to_pad` can now emit a narrow stub at one or both endpoint
pads, widening to the trunk width in between. The stub points
perpendicular to the pad's pin row (computed as the unit vector from
the footprint center to the pad). This unblocks the common case where
a fat POWER_4A trunk physically cannot fit out of a tight IC pin
pitch — the trunk would short to neighbouring pads.

Two flavours:

* Unit tests use the stub pcbnew and assert validation behaviour (param
  pairing) and the helper `_pad_outward_unit_vec` math.
* Integration tests need real pcbnew so they can stand up a real
  BOARD / FOOTPRINT / PAD and observe the segments that get committed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from commands.routing import RoutingCommands  # noqa: E402


def _real_pcbnew_available() -> bool:
    import pcbnew  # type: ignore
    return getattr(pcbnew, "GetBuildVersion", lambda: "")() != "9.0.0-stub"


# ---------------------------------------------------------------------------
# Unit tests — mock pcbnew is fine.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPadOutwardUnitVec:
    def _make_pad_fp(self, pad_xy: tuple, fp_xy: tuple):
        pad = MagicMock(name="PAD")
        pad_pos = MagicMock(name="pad_pos")
        pad_pos.x, pad_pos.y = pad_xy
        pad.GetPosition.return_value = pad_pos

        fp = MagicMock(name="FOOTPRINT")
        fp_pos = MagicMock(name="fp_pos")
        fp_pos.x, fp_pos.y = fp_xy
        fp.GetPosition.return_value = fp_pos
        return pad, fp

    def _routing(self) -> RoutingCommands:
        return RoutingCommands(board=MagicMock(name="BOARD"))

    def test_pad_east_of_center(self):
        r = self._routing()
        # Pad at (5_000_000, 0); footprint at (0, 0). Outward is +x.
        pad, fp = self._make_pad_fp((5_000_000, 0), (0, 0))
        dx, dy = r._pad_outward_unit_vec(pad, fp)
        assert dx == pytest.approx(1.0)
        assert dy == pytest.approx(0.0)

    def test_pad_north_of_center(self):
        # Pad above the FP body (y negative in screen-down coords).
        # Whatever the sign convention, the vector points away from
        # the body center along the y axis.
        r = self._routing()
        pad, fp = self._make_pad_fp((0, -3_000_000), (0, 0))
        dx, dy = r._pad_outward_unit_vec(pad, fp)
        assert dx == pytest.approx(0.0)
        assert dy == pytest.approx(-1.0)

    def test_pad_diagonal(self):
        r = self._routing()
        # 3-4-5 triangle gives a clean unit-vector check.
        pad, fp = self._make_pad_fp((3_000_000, 4_000_000), (0, 0))
        dx, dy = r._pad_outward_unit_vec(pad, fp)
        assert dx == pytest.approx(0.6)
        assert dy == pytest.approx(0.8)

    def test_pad_coincident_with_center_falls_back(self):
        """Thermal pad / BGA-style center pin — no outward direction
        recoverable; fall back to +x instead of dividing by zero."""
        r = self._routing()
        pad, fp = self._make_pad_fp((0, 0), (0, 0))
        dx, dy = r._pad_outward_unit_vec(pad, fp)
        assert (dx, dy) == (1.0, 0.0)


@pytest.mark.unit
class TestEscapeParamValidation:
    """Width and length must be supplied as a pair; either both or
    neither. Otherwise the typo is silent and the LLM thinks pin-escape
    was applied when it wasn't."""

    def _routing(self) -> RoutingCommands:
        return RoutingCommands(board=MagicMock(name="BOARD"))

    def test_from_width_without_length_errors(self):
        r = self._routing()
        result = r.route_pad_to_pad({
            "fromRef": "U1", "fromPad": "1",
            "toRef": "U2", "toPad": "1",
            "escapeFromWidth": 0.3,
        })
        assert result["success"] is False
        assert "escapeFromWidth and escapeFromLength" in result["errorDetails"]

    def test_from_length_without_width_errors(self):
        r = self._routing()
        result = r.route_pad_to_pad({
            "fromRef": "U1", "fromPad": "1",
            "toRef": "U2", "toPad": "1",
            "escapeFromLength": 2.0,
        })
        assert result["success"] is False
        assert "escapeFromWidth and escapeFromLength" in result["errorDetails"]

    def test_to_width_without_length_errors(self):
        r = self._routing()
        result = r.route_pad_to_pad({
            "fromRef": "U1", "fromPad": "1",
            "toRef": "U2", "toPad": "1",
            "escapeToWidth": 0.3,
        })
        assert result["success"] is False
        assert "escapeToWidth and escapeToLength" in result["errorDetails"]

    def test_to_length_without_width_errors(self):
        r = self._routing()
        result = r.route_pad_to_pad({
            "fromRef": "U1", "fromPad": "1",
            "toRef": "U2", "toPad": "1",
            "escapeToLength": 2.0,
        })
        assert result["success"] is False
        assert "escapeToWidth and escapeToLength" in result["errorDetails"]


# ---------------------------------------------------------------------------
# Integration tests — need real pcbnew.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(
    not _real_pcbnew_available(),
    reason="needs real pcbnew swig module",
)
class TestPinEscapeSegmentEmission:
    SCALE = 1_000_000  # nm per mm

    def _build_board_with_two_pads(self):
        """Two minimal footprints, each with one SMD pad.

        FP1 at (10, 10) with pad "1" at (12, 10) — pad east of center,
        outward vector +x.
        FP2 at (30, 10) with pad "1" at (28, 10) — pad west of center,
        outward vector -x.

        Net "TRACE" connects both pads. Route fp1.1 → fp2.1 on F.Cu.
        """
        import pcbnew

        board = pcbnew.BOARD()
        net = pcbnew.NETINFO_ITEM(board, "TRACE")
        board.Add(net)
        nets = board.GetNetInfo().NetsByName()

        def add_fp(ref, fp_xy_mm, pad_xy_mm):
            fp = pcbnew.FOOTPRINT(board)
            fp.SetReference(ref)
            fp.SetPosition(pcbnew.VECTOR2I(
                int(fp_xy_mm[0] * self.SCALE),
                int(fp_xy_mm[1] * self.SCALE),
            ))
            pad = pcbnew.PAD(fp)
            pad.SetNumber("1")
            pad.SetShape(pcbnew.PAD_SHAPE_RECT)
            pad.SetSize(pcbnew.VECTOR2I(
                int(0.5 * self.SCALE),
                int(0.5 * self.SCALE),
            ))
            pad.SetPosition(pcbnew.VECTOR2I(
                int(pad_xy_mm[0] * self.SCALE),
                int(pad_xy_mm[1] * self.SCALE),
            ))
            pad.SetLayerSet(pad.SMDMask())
            pad.SetNet(nets["TRACE"])
            fp.Add(pad)
            board.Add(fp)
            return fp, pad

        add_fp("FP1", (10, 10), (12, 10))
        add_fp("FP2", (30, 10), (28, 10))
        return board

    def test_from_side_escape_emits_two_segments(self):
        board = self._build_board_with_two_pads()
        r = RoutingCommands(board=board)
        result = r.route_pad_to_pad({
            "fromRef": "FP1",
            "fromPad": "1",
            "toRef": "FP2",
            "toPad": "1",
            "layer": "F.Cu",
            "width": 1.5,
            "escapeFromWidth": 0.3,
            "escapeFromLength": 2.0,
            "checkObstacles": False,
        })
        assert result["success"], result
        assert result["segmentCount"] == 2

        # Inspect committed tracks: should be 0.3 mm stub starting at
        # FP1.1 (12, 10) going +x by 2 mm (to 14, 10), then 1.5 mm
        # trunk from (14, 10) to FP2.1 at (28, 10).
        import pcbnew

        tracks = [t for t in board.Tracks() if t.Type() != pcbnew.PCB_VIA_T]
        widths_mm = sorted(t.GetWidth() / self.SCALE for t in tracks)
        assert widths_mm == [0.3, 1.5], widths_mm

        # The 0.3 mm stub should span exactly 2 mm.
        stub = next(t for t in tracks if t.GetWidth() == int(0.3 * self.SCALE))
        s, e = stub.GetStart(), stub.GetEnd()
        length_mm = ((e.x - s.x) ** 2 + (e.y - s.y) ** 2) ** 0.5 / self.SCALE
        assert length_mm == pytest.approx(2.0, abs=0.001)

    def test_both_sides_escape_emits_three_segments(self):
        board = self._build_board_with_two_pads()
        r = RoutingCommands(board=board)
        result = r.route_pad_to_pad({
            "fromRef": "FP1",
            "fromPad": "1",
            "toRef": "FP2",
            "toPad": "1",
            "layer": "F.Cu",
            "width": 1.5,
            "escapeFromWidth": 0.3,
            "escapeFromLength": 2.0,
            "escapeToWidth": 0.4,
            "escapeToLength": 1.5,
            "checkObstacles": False,
        })
        assert result["success"], result
        assert result["segmentCount"] == 3

        import pcbnew

        tracks = [t for t in board.Tracks() if t.Type() != pcbnew.PCB_VIA_T]
        widths_mm = sorted(t.GetWidth() / self.SCALE for t in tracks)
        assert widths_mm == [0.3, 0.4, 1.5], widths_mm

    def test_no_escape_single_segment_legacy(self):
        """Without escape params, behaviour matches the pre-#178
        single-segment trace at the trunk width."""
        board = self._build_board_with_two_pads()
        r = RoutingCommands(board=board)
        result = r.route_pad_to_pad({
            "fromRef": "FP1",
            "fromPad": "1",
            "toRef": "FP2",
            "toPad": "1",
            "layer": "F.Cu",
            "width": 1.0,
            "checkObstacles": False,
        })
        assert result["success"], result

        import pcbnew

        tracks = [t for t in board.Tracks() if t.Type() != pcbnew.PCB_VIA_T]
        assert len(tracks) == 1
        assert tracks[0].GetWidth() == int(1.0 * self.SCALE)


@pytest.mark.integration
@pytest.mark.skipif(
    not _real_pcbnew_available(),
    reason="needs real pcbnew swig module",
)
class TestPinEscapeCrossLayerRejected:
    """v1 explicitly rejects cross-layer routes with escape params —
    no silent ignoring."""

    SCALE = 1_000_000

    def test_cross_layer_with_escape_errors(self):
        import pcbnew

        board = pcbnew.BOARD()
        net = pcbnew.NETINFO_ITEM(board, "TRACE")
        board.Add(net)
        nets = board.GetNetInfo().NetsByName()

        # FP1 on F.Cu, FP2 on B.Cu — needs_via path.
        for ref, layer, xy in (
            ("FP1", pcbnew.F_Cu, (10, 10)),
            ("FP2", pcbnew.B_Cu, (30, 10)),
        ):
            fp = pcbnew.FOOTPRINT(board)
            fp.SetReference(ref)
            fp.SetLayer(layer)
            fp.SetPosition(pcbnew.VECTOR2I(
                int(xy[0] * self.SCALE),
                int(xy[1] * self.SCALE),
            ))
            pad = pcbnew.PAD(fp)
            pad.SetNumber("1")
            pad.SetShape(pcbnew.PAD_SHAPE_RECT)
            pad.SetSize(pcbnew.VECTOR2I(
                int(0.5 * self.SCALE),
                int(0.5 * self.SCALE),
            ))
            pad.SetPosition(pcbnew.VECTOR2I(
                int(xy[0] * self.SCALE),
                int(xy[1] * self.SCALE),
            ))
            pad.SetLayerSet(pad.SMDMask())
            pad.SetNet(nets["TRACE"])
            fp.Add(pad)
            board.Add(fp)

        r = RoutingCommands(board=board)
        result = r.route_pad_to_pad({
            "fromRef": "FP1",
            "fromPad": "1",
            "toRef": "FP2",
            "toPad": "1",
            "width": 1.5,
            "escapeFromWidth": 0.3,
            "escapeFromLength": 2.0,
        })
        assert result["success"] is False
        assert "cross-layer" in result["message"].lower()
