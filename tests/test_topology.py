"""Tests for Phase 1 of the topology tools (#TBD).

Covers the rasterize + erode + label primitive behind
`analyze_routable_regions` / `check_pad_routability`. Integration tests
build a small synthetic board with a known free-space topology so the
expected component count + bottleneck width are predictable from
geometry alone.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from commands.topology import (  # noqa: E402
    _bfs_path,
    _nearest_free_pixel,
    _stamp_disk,
    _stamp_rect_aa,
    _stamp_segment,
)


def _real_pcbnew_available() -> bool:
    import pcbnew  # type: ignore

    return getattr(pcbnew, "GetBuildVersion", lambda: "")() != "9.0.0-stub"


# ---------------------------------------------------------------------------
# Unit tests — pure rasterizer / BFS helpers (no pcbnew).
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRasterPrimitives:
    def test_disk_stamp_center(self):
        m = np.zeros((10, 10), dtype=bool)
        _stamp_disk(m, 5, 5, 2)
        # Center plus 4-neighbours plus diagonals at r=2: cardinal r=2
        # pixels included; corners just past r=2 excluded.
        assert m[5, 5]
        assert m[5, 7]
        assert m[7, 5]
        assert not m[7, 7]  # diagonal distance = sqrt(8) > 2

    def test_disk_stamp_offscreen_no_crash(self):
        m = np.zeros((5, 5), dtype=bool)
        _stamp_disk(m, -3, -3, 2)
        assert not m.any()

    def test_segment_stamp_axis_aligned(self):
        m = np.zeros((20, 20), dtype=bool)
        _stamp_segment(m, (3, 10), (16, 10), 1.5)
        # Every pixel along y=10 in [3,16] should be set.
        assert m[10, 3:17].all()
        # A pixel 3 rows above the segment is too far at half_w=1.5.
        assert not m[7, 10]

    def test_rect_aa_axis_aligned(self):
        m = np.zeros((50, 50), dtype=bool)
        # 4mm × 2mm rect centered at world (5, 5) mm with grid origin (0, 0)
        # mm and g=0.5 → 8 × 4 pixels centered at pixel (10, 10).
        _stamp_rect_aa(m, 5.0, 5.0, 4.0, 2.0, 0.0, x0=0.0, y0=0.0, g=0.5)
        assert m[10, 10]
        # Corners of the 8x4 rect.
        assert m[8, 7]
        assert m[12, 13]
        # Outside the rect.
        assert not m[14, 10]

    def test_rect_aa_rotated_90(self):
        m = np.zeros((50, 50), dtype=bool)
        # 90° rotation swaps width/height in world frame.
        _stamp_rect_aa(m, 5.0, 5.0, 4.0, 2.0, 90.0, x0=0.0, y0=0.0, g=0.5)
        # Long axis now along y → pixel offset 3 rows from center should
        # hit, but offset 3 columns should not (post-rotation the
        # "width" is along y).
        assert m[7, 10]
        assert not m[10, 7]


@pytest.mark.unit
class TestNearestFreePixel:
    def test_returns_self_when_free(self):
        free = np.ones((5, 5), dtype=bool)
        assert _nearest_free_pixel(free, 2, 2, 1) == (2, 2)

    def test_finds_neighbour_when_center_blocked(self):
        free = np.ones((5, 5), dtype=bool)
        free[2, 2] = False
        # Closest free pixel is any 4-neighbour at distance 1.
        result = _nearest_free_pixel(free, 2, 2, 1)
        assert result in {(1, 2), (3, 2), (2, 1), (2, 3)}

    def test_returns_none_when_window_empty(self):
        free = np.zeros((5, 5), dtype=bool)
        assert _nearest_free_pixel(free, 2, 2, 1) is None


@pytest.mark.unit
class TestBfsPath:
    def test_straight_path(self):
        free = np.ones((5, 10), dtype=bool)
        path = _bfs_path(free, (2, 0), (2, 9))
        assert path is not None
        assert path[0] == (2, 0)
        assert path[-1] == (2, 9)

    def test_no_path_when_split(self):
        free = np.ones((5, 10), dtype=bool)
        # Vertical wall at column 5.
        free[:, 5] = False
        assert _bfs_path(free, (2, 0), (2, 9)) is None

    def test_path_around_wall(self):
        free = np.ones((10, 10), dtype=bool)
        # Wall that doesn't reach the bottom.
        free[0:8, 5] = False
        path = _bfs_path(free, (2, 2), (2, 8))
        assert path is not None
        assert path[0] == (2, 2)
        assert path[-1] == (2, 8)


# ---------------------------------------------------------------------------
# Integration tests — need real pcbnew.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(
    not _real_pcbnew_available(),
    reason="needs real pcbnew swig module",
)
class TestAnalyzeRegionsIntegration:
    SCALE = 1_000_000

    def _build_split_board(self):
        """20 × 10 mm board with a single F.Cu track running vertically
        down the middle (x=10) at width 2 mm — splits F.Cu into two
        free-space components for any trace width > 0.

        Two SMD F.Cu pads: left at (5, 5), right at (15, 5).
        """
        import pcbnew

        board = pcbnew.BOARD()
        edge_layer = board.GetLayerID("Edge.Cuts")
        for (x0, y0), (x1, y1) in [
            ((0, 0), (20, 0)),
            ((20, 0), (20, 10)),
            ((20, 10), (0, 10)),
            ((0, 10), (0, 0)),
        ]:
            seg = pcbnew.PCB_SHAPE(board)
            seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
            seg.SetLayer(edge_layer)
            seg.SetStart(pcbnew.VECTOR2I(int(x0 * self.SCALE), int(y0 * self.SCALE)))
            seg.SetEnd(pcbnew.VECTOR2I(int(x1 * self.SCALE), int(y1 * self.SCALE)))
            board.Add(seg)

        # The dividing wall: a 2 mm-wide track on F.Cu from (10, 0) to (10, 10).
        wall = pcbnew.PCB_TRACK(board)
        wall.SetStart(pcbnew.VECTOR2I(int(10 * self.SCALE), int(0 * self.SCALE)))
        wall.SetEnd(pcbnew.VECTOR2I(int(10 * self.SCALE), int(10 * self.SCALE)))
        wall.SetWidth(int(2.0 * self.SCALE))
        wall.SetLayer(pcbnew.F_Cu)
        board.Add(wall)

        def add_smd(ref, x_mm, y_mm):
            fp = pcbnew.FOOTPRINT(board)
            fp.SetReference(ref)
            fp.SetLayer(pcbnew.F_Cu)
            fp.SetPosition(pcbnew.VECTOR2I(int(x_mm * self.SCALE), int(y_mm * self.SCALE)))
            pad = pcbnew.PAD(fp)
            pad.SetNumber("1")
            pad.SetShape(pcbnew.PAD_SHAPE_RECT)
            pad.SetSize(pcbnew.VECTOR2I(int(1.0 * self.SCALE), int(1.0 * self.SCALE)))
            pad.SetPosition(pcbnew.VECTOR2I(int(x_mm * self.SCALE), int(y_mm * self.SCALE)))
            pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            pad.SetLayerSet(pad.SMDMask())
            fp.Add(pad)
            board.Add(fp)
            return fp

        add_smd("L1", 5, 5)
        add_smd("R1", 15, 5)
        return board

    def test_wall_creates_two_components(self):
        from commands.topology import analyze_routable_regions

        board = self._build_split_board()
        r = analyze_routable_regions(
            board, layer="F.Cu", width_mm=0.2, resolution_mm=0.1
        )
        assert r["success"] is True
        # A vertical 2 mm wall down the middle splits F.Cu into a left
        # half and a right half. Pads are well within each half, so we
        # expect exactly two components big enough to host them.
        big_regions = [reg for reg in r["regions"] if reg["areaMm2"] > 20]
        assert len(big_regions) == 2

        # Each big region should claim exactly one pad.
        pad_refs = {p["ref"] for reg in big_regions for p in reg["pads"]}
        assert pad_refs == {"L1", "R1"}

        # No pad-escape failures.
        assert r["padsWithoutEscape"] == []

    def test_pads_unreachable_across_wall(self):
        from commands.topology import check_pad_routability

        board = self._build_split_board()
        r = check_pad_routability(
            board,
            from_ref="L1", from_pad="1",
            to_ref="R1", to_pad="1",
            layer="F.Cu", width_mm=0.2, resolution_mm=0.1,
        )
        assert r["success"] is True
        assert r["reachable"] is False
        # The 2 mm wall plus 0 clearance leaves no corridor: 'different
        # components' is the expected reason code.
        assert r["reason"] == "different_components"
        assert r["fromComponent"] != r["toComponent"]

    def test_convergence_check_returns_block(self):
        from commands.topology import analyze_routable_regions

        board = self._build_split_board()
        r = analyze_routable_regions(
            board, layer="F.Cu", width_mm=0.2,
            resolution_mm=0.1, convergence_check=True,
        )
        assert "convergence" in r
        assert r["convergence"]["fineGridMm"] == pytest.approx(0.05)
        # Wall is wide (2 mm) relative to the grid — both resolutions
        # should give the same partition.
        assert r["convergence"]["agrees"] is True

    def test_max_width_between_unreachable(self):
        from commands.topology import max_width_between

        board = self._build_split_board()
        r = max_width_between(
            board,
            from_ref="L1", from_pad="1",
            to_ref="R1", to_pad="1",
            layer="F.Cu", resolution_mm=0.1,
        )
        assert r["success"] is True
        # 2 mm wall + 0 clearance leaves no path at any positive width.
        assert r["reachable"] is False
        assert r["reason"] == "different_components"

    def _build_open_board(self):
        """20 × 10 mm board with no internal copper — just two SMD pads
        at (5, 5) and (15, 5). Free space is wide open."""
        import pcbnew

        board = pcbnew.BOARD()
        edge_layer = board.GetLayerID("Edge.Cuts")
        for (x0, y0), (x1, y1) in [
            ((0, 0), (20, 0)),
            ((20, 0), (20, 10)),
            ((20, 10), (0, 10)),
            ((0, 10), (0, 0)),
        ]:
            seg = pcbnew.PCB_SHAPE(board)
            seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
            seg.SetLayer(edge_layer)
            seg.SetStart(pcbnew.VECTOR2I(int(x0 * self.SCALE), int(y0 * self.SCALE)))
            seg.SetEnd(pcbnew.VECTOR2I(int(x1 * self.SCALE), int(y1 * self.SCALE)))
            board.Add(seg)

        def add_smd(ref, x_mm, y_mm):
            fp = pcbnew.FOOTPRINT(board)
            fp.SetReference(ref)
            fp.SetLayer(pcbnew.F_Cu)
            fp.SetPosition(pcbnew.VECTOR2I(int(x_mm * self.SCALE), int(y_mm * self.SCALE)))
            pad = pcbnew.PAD(fp)
            pad.SetNumber("1")
            pad.SetShape(pcbnew.PAD_SHAPE_RECT)
            pad.SetSize(pcbnew.VECTOR2I(int(1.0 * self.SCALE), int(1.0 * self.SCALE)))
            pad.SetPosition(pcbnew.VECTOR2I(int(x_mm * self.SCALE), int(y_mm * self.SCALE)))
            pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            pad.SetLayerSet(pad.SMDMask())
            fp.Add(pad)
            board.Add(fp)
            return fp

        add_smd("L1", 5, 5)
        add_smd("R1", 15, 5)
        return board

    def test_max_width_between_open_board(self):
        from commands.topology import max_width_between

        board = self._build_open_board()
        r = max_width_between(
            board,
            from_ref="L1", from_pad="1",
            to_ref="R1", to_pad="1",
            layer="F.Cu", resolution_mm=0.1,
        )
        assert r["success"] is True
        assert r["reachable"] is True
        # Pads are 1 mm × 1 mm at y=5 on a 10 mm-tall board. Pad center
        # is 5 mm from each long edge, so the path corridor's width is
        # dominated by the pad-to-edge gap (~5 mm). The max trace can be
        # at least a couple of mm wide.
        assert r["maxWidthMm"] > 2.0

    def test_max_parallel_traces_open_board(self):
        from commands.topology import max_parallel_traces

        board = self._build_open_board()
        r = max_parallel_traces(
            board,
            from_ref="L1", from_pad="1",
            to_ref="R1", to_pad="1",
            layer="F.Cu", width_mm=0.5, clearance_mm=0.0,
            resolution_mm=0.1,
        )
        assert r["success"] is True
        # With an empty 20×10 mm board and 1 mm pads, plenty of 0.5 mm
        # traces should fit through the inter-pad corridor.
        assert r["maxParallelTraces"] >= 2

    def test_routability_heatmap_writes_png(self):
        from commands.topology import routability_heatmap

        board = self._build_open_board()
        out_path = "/tmp/claude-1000/test_heatmap.png"
        r = routability_heatmap(
            board,
            from_ref="L1", from_pad="1",
            layer="F.Cu", width_mm=0.25,
            resolution_mm=0.1, output_path=out_path,
        )
        assert r["success"] is True
        assert r["reachable"] is True
        assert r["vizPath"] == out_path
        from pathlib import Path
        assert Path(out_path).exists()
        assert Path(out_path).stat().st_size > 0
        assert r["reachableAreaMm2"] > 0
        assert r["maxReachMm"] > 0
