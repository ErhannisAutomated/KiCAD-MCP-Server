"""Tests for the layer-filter addition to analyze_congestion (#181).

* Unit tests cover the `_pad_layer_names` helper against mocked
  LayerSet objects.
* Integration tests build a real pcbnew board with a mix of F.Cu and
  B.Cu SMD pads + one PTH pad, and assert that a layer-filtered call
  scores only the requested side.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from commands.congestion import _pad_layer_names, analyze_congestion  # noqa: E402


def _real_pcbnew_available() -> bool:
    import pcbnew  # type: ignore
    return getattr(pcbnew, "GetBuildVersion", lambda: "")() != "9.0.0-stub"


# ---------------------------------------------------------------------------
# Unit tests — mocked pcbnew.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPadLayerNames:
    def _pad_with_layers(self, accepted_lids, layer_name_map):
        """Construct a mock pad whose LayerSet.Contains(lid) is True
        iff lid is in `accepted_lids`."""
        pad = MagicMock(name="PAD")
        layer_set = MagicMock(name="LayerSet")
        layer_set.Contains.side_effect = lambda lid: lid in accepted_lids
        pad.GetLayerSet.return_value = layer_set

        board = MagicMock(name="BOARD")
        board.GetLayerName.side_effect = lambda lid: layer_name_map.get(
            lid, f"unknown-{lid}"
        )
        return pad, board

    def test_smd_pad_one_layer(self):
        pad, board = self._pad_with_layers(
            {0}, {0: "F.Cu", 31: "B.Cu", 1: "In1.Cu", 2: "In2.Cu"}
        )
        assert _pad_layer_names(pad, board) == ["F.Cu"]

    def test_pth_pad_all_copper_layers(self):
        pad, board = self._pad_with_layers(
            {0, 1, 2, 31},
            {0: "F.Cu", 1: "In1.Cu", 2: "In2.Cu", 31: "B.Cu", 50: "F.Mask"},
        )
        result = _pad_layer_names(pad, board)
        assert set(result) == {"F.Cu", "In1.Cu", "In2.Cu", "B.Cu"}

    def test_non_copper_layers_excluded(self):
        """Pad layer set may include solder mask / paste — only .Cu
        layers should make it into the per-layer pad count."""
        pad, board = self._pad_with_layers(
            {0, 50, 51},
            {0: "F.Cu", 50: "F.Mask", 51: "F.Paste"},
        )
        assert _pad_layer_names(pad, board) == ["F.Cu"]


# ---------------------------------------------------------------------------
# Integration tests — need real pcbnew.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(
    not _real_pcbnew_available(),
    reason="needs real pcbnew swig module",
)
class TestAnalyzeCongestionLayerFilter:
    SCALE = 1_000_000

    def _build_two_layer_board(self):
        """40×30 mm board with three pads:

        * F1 — SMD pad at (10, 15) on F.Cu
        * B1 — SMD pad at (10, 15) on B.Cu  (same XY, opposite layer)
        * P1 — PTH pad at (10, 15)  (counts on every Cu layer)

        Plus an Edge.Cuts rectangle for the bounding box.
        """
        import pcbnew

        board = pcbnew.BOARD()

        # Edge.Cuts rectangle 0,0 → 40,30
        edge_layer = board.GetLayerID("Edge.Cuts")
        for (x0, y0), (x1, y1) in [
            ((0, 0), (40, 0)),
            ((40, 0), (40, 30)),
            ((40, 30), (0, 30)),
            ((0, 30), (0, 0)),
        ]:
            seg = pcbnew.PCB_SHAPE(board)
            seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
            seg.SetLayer(edge_layer)
            seg.SetStart(pcbnew.VECTOR2I(int(x0 * self.SCALE), int(y0 * self.SCALE)))
            seg.SetEnd(pcbnew.VECTOR2I(int(x1 * self.SCALE), int(y1 * self.SCALE)))
            board.Add(seg)

        def add_fp(ref, layer, pad_shape, x_mm, y_mm):
            fp = pcbnew.FOOTPRINT(board)
            fp.SetReference(ref)
            fp.SetLayer(layer)
            fp.SetPosition(pcbnew.VECTOR2I(
                int(x_mm * self.SCALE), int(y_mm * self.SCALE)
            ))
            pad = pcbnew.PAD(fp)
            pad.SetNumber("1")
            pad.SetShape(pcbnew.PAD_SHAPE_RECT)
            pad.SetSize(pcbnew.VECTOR2I(
                int(1.0 * self.SCALE), int(1.0 * self.SCALE)
            ))
            pad.SetPosition(pcbnew.VECTOR2I(
                int(x_mm * self.SCALE), int(y_mm * self.SCALE)
            ))
            if pad_shape == "smd_f":
                pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
                pad.SetLayerSet(pad.SMDMask())
            elif pad_shape == "smd_b":
                pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
                pad.SetLayerSet(pad.SMDMask())
                fp.Flip(fp.GetPosition(), False)
            elif pad_shape == "pth":
                pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
                pad.SetDrillSize(pcbnew.VECTOR2I(
                    int(0.4 * self.SCALE), int(0.4 * self.SCALE)
                ))
                pad.SetLayerSet(pad.PTHMask())
            fp.Add(pad)
            board.Add(fp)
            return fp

        add_fp("F1", pcbnew.F_Cu, "smd_f", 10, 15)
        add_fp("B1", pcbnew.F_Cu, "smd_b", 10, 15)
        add_fp("P1", pcbnew.F_Cu, "pth", 10, 15)
        return board

    def test_layer_filter_excludes_other_side_smd(self):
        """F.Cu-filtered call should count F1 (F.Cu SMD) + P1 (PTH on
        every layer), and NOT B1 (B.Cu SMD).  B.Cu-filtered does the
        opposite."""
        board = self._build_two_layer_board()

        # Need at least one ratsnest segment for any score > 0. Synth
        # a DRC JSON with a fake unconnected_items entry in the
        # hotspot cell.
        import json
        import os
        import tempfile

        drc_data = {
            "violations": [{
                "type": "unconnected_items",
                "items": [
                    {"pos": {"x": 10, "y": 15}, "net": "TEST",
                     "description": "Pad F1.1"},
                    {"pos": {"x": 12, "y": 15}, "net": "TEST",
                     "description": "Pad B1.1"},
                ],
            }],
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(drc_data, f)
            drc_path = f.name
        try:
            f_result = analyze_congestion(
                board, cell_size_mm=5.0,
                drc_violations_path=drc_path, layer="F.Cu",
            )
            b_result = analyze_congestion(
                board, cell_size_mm=5.0,
                drc_violations_path=drc_path, layer="B.Cu",
            )

            assert f_result["success"]
            assert b_result["success"]
            # The (10, 15) cell should appear in both layer queries —
            # both layers have at least one pad there (F1+P1 on F.Cu;
            # B1+P1 on B.Cu).
            assert f_result["layer"] == "F.Cu"
            assert b_result["layer"] == "B.Cu"

            # Find the hotspot covering (10, 15).
            def covers(h):
                c = h["cell"]
                x, y = c["x_mm"], c["y_mm"]
                size = c["size_mm"]
                return x <= 10 <= x + size and y <= 15 <= y + size

            f_hotspot = next(h for h in f_result["hotspots"] if covers(h))
            b_hotspot = next(h for h in b_result["hotspots"] if covers(h))

            # F.Cu sees F1 + P1 = 2 pads; B.Cu sees B1 + P1 = 2 pads.
            # (Filter excludes the other-side SMD.)
            assert f_hotspot["pad_count_by_layer"].get("F.Cu") == 2
            assert b_hotspot["pad_count_by_layer"].get("B.Cu") == 2

            # And the *score* equals per-layer pads × rats.  Both
            # filtered calls should see the same per-layer pad count
            # (2) and the same single ratsnest segment crossing.
            assert f_hotspot["score"] == b_hotspot["score"]
        finally:
            os.unlink(drc_path)

    def test_unfiltered_call_includes_all_pads(self):
        """Default call (no `layer`) returns the legacy any-layer pad
        count, which sums all three pads at that cell."""
        board = self._build_two_layer_board()
        import json
        import os
        import tempfile

        drc_data = {
            "violations": [{
                "type": "unconnected_items",
                "items": [
                    {"pos": {"x": 10, "y": 15}, "net": "TEST",
                     "description": "Pad F1.1"},
                    {"pos": {"x": 12, "y": 15}, "net": "TEST",
                     "description": "Pad B1.1"},
                ],
            }],
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(drc_data, f)
            drc_path = f.name
        try:
            result = analyze_congestion(
                board, cell_size_mm=5.0, drc_violations_path=drc_path,
            )
            assert result["success"]
            assert result["layer"] is None
            # The hotspot's any-layer pad_count is 3 (F1 + B1 + P1
            # all overlap (10,15)).
            def covers(h):
                c = h["cell"]
                x, y = c["x_mm"], c["y_mm"]
                size = c["size_mm"]
                return x <= 10 <= x + size and y <= 15 <= y + size
            hot = next(h for h in result["hotspots"] if covers(h))
            assert hot["pad_count"] == 3
        finally:
            os.unlink(drc_path)
