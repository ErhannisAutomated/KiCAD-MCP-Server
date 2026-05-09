"""
Tests for the cropped+page-frame-less schematic renderer.

The default behaviour of get_schematic_view changed in commit «pin-locator
cache fix follow-up» (2026-05-09): instead of exporting the whole A4 page
(content tiny, whitespace huge, page frame and title block included), the
SVG is now cropped to the bbox of placed content and the drawing sheet is
suppressed.  These tests cover:

  1. The bbox helper computes a correct min/max from a schematic with a
     handful of components in known positions.
  2. The bbox helper returns None for an empty schematic.
  3. The bbox helper ignores symbols defined in lib_symbols (those are
     library entries, not placed instances).
"""
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

PYTHON_DIR = Path(__file__).parent.parent / "python"
TEMPLATES_DIR = PYTHON_DIR / "templates"
sys.path.insert(0, str(PYTHON_DIR))


@pytest.mark.integration
class TestSchematicContentBbox:
    """Helper that walks the .kicad_sch and returns (x, y, w, h) in mm."""

    def test_empty_schematic_returns_none(self):
        from kicad_interface import KiCADInterface

        with tempfile.TemporaryDirectory() as tmp:
            sch = Path(tmp) / "empty.kicad_sch"
            shutil.copy(TEMPLATES_DIR / "empty.kicad_sch", sch)
            assert KiCADInterface._schematic_content_bbox_mm(str(sch)) is None

    def test_template_with_symbols_bbox_is_finite(self):
        """The shipped template_with_symbols puts placeholders at (-100, -100…-120)
        — so the bbox should be near (-100, -100) with a small extent."""
        from kicad_interface import KiCADInterface

        sch = TEMPLATES_DIR / "template_with_symbols.kicad_sch"
        bbox = KiCADInterface._schematic_content_bbox_mm(str(sch))
        assert bbox is not None
        x, y, w, h = bbox
        # padding adds 5 mm on each side, content placed at (-100, -100..-120)
        assert -110 <= x <= -100, f"x={x}, expected -110..-100"
        assert w > 0 and h > 0
        # content spans ~20mm in y (3 placeholder symbols stacked); bbox padded
        assert h >= 20, f"h={h}, expected >=20 for stacked template content"

    def test_bbox_ignores_lib_symbols_definitions(self):
        """lib_symbols entries inside (lib_symbols (symbol ...) ...) should NOT
        be counted — those are off-canvas library definitions, not placed
        instances.  Otherwise an empty schematic with lib_symbols would
        return a non-None bbox far from the page origin."""
        from kicad_interface import KiCADInterface

        sch_text = """(kicad_sch (version 20250114) (generator "test")
  (uuid 11111111-2222-3333-4444-555555555555)
  (paper "A4")
  (lib_symbols
    (symbol "Device:R"
      (pin passive line (at 1000 1000 0) (length 1) (name "1") (number "1"))
      (pin passive line (at 1000 -1000 0) (length 1) (name "2") (number "2"))
    )
  )
  (sheet_instances
    (path "/" (page "1"))
  )
)
"""
        with tempfile.TemporaryDirectory() as tmp:
            sch = Path(tmp) / "lib_only.kicad_sch"
            sch.write_text(sch_text)
            bbox = KiCADInterface._schematic_content_bbox_mm(str(sch))
            assert bbox is None, (
                f"Empty-schematic-with-lib-symbols should return None, got {bbox} — "
                "the bbox helper is leaking lib_symbols pin offsets into the bbox"
            )
