"""Tests for get_board_2d_view dispatch + helpers.

The actual rendering needs real pcbnew (not the stub from conftest), so the
end-to-end render checks are gated behind an `integration` marker that skips
when pcbnew is the test stub.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


def _real_pcbnew_available() -> bool:
    import pcbnew  # type: ignore
    return getattr(pcbnew, "GetBuildVersion", lambda: "")() != "9.0.0-stub"


# ----------------------------------------------------------------------
# Pure-Python helpers — testable without real pcbnew
# ----------------------------------------------------------------------


@pytest.mark.unit
class TestSvgHelpers:
    def test_set_viewbox_replaces_existing(self) -> None:
        from commands.board.view import BoardViewCommands

        svg = '<svg width="100mm" height="50mm" viewBox="0 0 297 210"><g/></svg>'
        out = BoardViewCommands._svg_set_viewbox(svg, (10.0, 20.0, 30.0, 40.0), 0.05)
        # margin = max(30, 40) * 0.05 = 2 → viewBox "8 18 34 44"
        assert 'viewBox="8.0 18.0 34.0 44.0"' in out
        # width/height should be stripped
        assert "width=" not in out
        assert "height=" not in out

    def test_set_viewbox_no_existing_viewbox(self) -> None:
        from commands.board.view import BoardViewCommands

        svg = "<svg><g/></svg>"
        out = BoardViewCommands._svg_set_viewbox(svg, (0.0, 0.0, 10.0, 10.0), 0.1)
        # No existing viewBox → nothing matched, but no crash
        assert out == svg or "viewBox=" not in out

    def test_recolor_replaces_black(self) -> None:
        from commands.board.view import BoardViewCommands

        svg = '<g style="fill:#000000; stroke:#000000;"><path/></g>'
        out = BoardViewCommands._svg_recolor(svg, "#FF0000")
        assert "fill:#FF0000" in out
        assert "stroke:#FF0000" in out
        assert "#000000" not in out

    def test_recolor_preserves_white_and_none(self) -> None:
        from commands.board.view import BoardViewCommands

        svg = '<g style="fill:none; stroke:#ffffff;"><path/></g>'
        out = BoardViewCommands._svg_recolor(svg, "#FF0000")
        assert "fill:none" in out
        assert "stroke:#ffffff" in out

    def test_color_for_layer_default_palette(self) -> None:
        from commands.board.view import BoardViewCommands

        v = BoardViewCommands.__new__(BoardViewCommands)
        # Known layers map to the configured colours.
        assert v._color_for_layer("F.Cu") == "#C83737"
        assert v._color_for_layer("B.Cu") == "#3771C8"
        assert v._color_for_layer("Edge.Cuts") == "#D0D0B0"
        # Unknown layer falls through to fallback palette (not crash).
        c = v._color_for_layer("User.5")
        assert c.startswith("#") and len(c) == 7


# ----------------------------------------------------------------------
# Dispatch — exercises the param routing without rendering
# ----------------------------------------------------------------------


@pytest.mark.unit
class TestDispatch:
    def test_no_board_returns_error(self) -> None:
        from commands.board.view import BoardViewCommands
        v = BoardViewCommands(board=None)
        r = v.get_board_2d_view({})
        assert r["success"] is False
        assert "No board" in r["message"] or "no board" in r["message"].lower()

    def test_legacy_path_taken_when_both_flags_false(self) -> None:
        from commands.board.view import BoardViewCommands
        v = BoardViewCommands(board=MagicMock(name="board"))
        v._legacy_plot = MagicMock(return_value={"success": True, "imageData": "x", "format": "png"})
        v._composite_plot = MagicMock(return_value={"success": True, "imageData": "y", "format": "png"})
        r = v.get_board_2d_view({"cropToBoard": False, "colored": False})
        v._legacy_plot.assert_called_once()
        v._composite_plot.assert_not_called()
        assert r["imageData"] == "x"

    def test_composite_path_default(self) -> None:
        from commands.board.view import BoardViewCommands
        v = BoardViewCommands(board=MagicMock(name="board"))
        v._legacy_plot = MagicMock()
        v._composite_plot = MagicMock(return_value={"success": True, "imageData": "y", "format": "png"})
        r = v.get_board_2d_view({})  # no flags → default true,true → composite
        v._legacy_plot.assert_not_called()
        v._composite_plot.assert_called_once()
        kwargs = v._composite_plot.call_args.kwargs
        assert kwargs["crop_to_board"] is True
        assert kwargs["colored"] is True
        assert r["imageData"] == "y"


# ----------------------------------------------------------------------
# End-to-end — needs real pcbnew + a sample .kicad_pcb
# ----------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(
    not _real_pcbnew_available(), reason="real pcbnew not available in test env"
)
class TestEndToEndRender:
    SAMPLE_PCB = Path(
        "/home/vagrant/projects/kicad_agent/projects/rgb_switches_v2/rgb_switches_v2.kicad_pcb"
    )

    @pytest.fixture
    def board_view(self) -> object:
        if not self.SAMPLE_PCB.exists():
            pytest.skip("sample PCB not present")
        import pcbnew  # type: ignore
        from commands.board.view import BoardViewCommands
        return BoardViewCommands(pcbnew.LoadBoard(str(self.SAMPLE_PCB)))

    def test_default_returns_png(self, board_view: object) -> None:
        r = board_view.get_board_2d_view({"width": 800, "height": 600})
        assert r["success"] is True
        assert r["format"] == "png"
        # Base64-decoded PNG must start with the PNG signature.
        import base64
        data = base64.b64decode(r["imageData"])
        assert data.startswith(b"\x89PNG\r\n\x1a\n")

    def test_legacy_returns_png(self, board_view: object) -> None:
        r = board_view.get_board_2d_view(
            {"width": 800, "height": 600, "cropToBoard": False, "colored": False}
        )
        assert r["success"] is True
        assert r["format"] == "png"

    def test_composite_image_is_larger(self, board_view: object) -> None:
        """Cropped+colored image carries more board pixels → bigger PNG byte
        size than the whole-page monochrome plot at the same resolution."""
        import base64
        new_r = board_view.get_board_2d_view({"width": 1200, "height": 900})
        old_r = board_view.get_board_2d_view(
            {"width": 1200, "height": 900, "cropToBoard": False, "colored": False}
        )
        new_bytes = len(base64.b64decode(new_r["imageData"]))
        old_bytes = len(base64.b64decode(old_r["imageData"]))
        assert new_bytes > old_bytes, (new_bytes, old_bytes)

    def test_svg_format(self, board_view: object) -> None:
        r = board_view.get_board_2d_view({"format": "svg"})
        assert r["success"] is True
        assert r["format"] == "svg"
        assert r["imageData"].lstrip().startswith("<svg")
