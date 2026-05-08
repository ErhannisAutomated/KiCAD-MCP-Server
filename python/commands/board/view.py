"""
Board view command implementations for KiCAD interface
"""

import base64
import io
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import pcbnew
from PIL import Image

logger = logging.getLogger("kicad_interface")


class BoardViewCommands:
    """Handles board viewing operations"""

    def __init__(self, board: Optional[pcbnew.BOARD] = None):
        """Initialize with optional board instance"""
        self.board = board

    def get_board_info(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Get information about the current board"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            # Get board dimensions
            board_box = self.board.GetBoardEdgesBoundingBox()
            width_nm = board_box.GetWidth()
            height_nm = board_box.GetHeight()

            # Convert to mm
            width_mm = width_nm / 1000000
            height_mm = height_nm / 1000000

            # Get layer information
            layers = []
            for layer_id in range(pcbnew.PCB_LAYER_ID_COUNT):
                if self.board.IsLayerEnabled(layer_id):
                    layers.append(
                        {
                            "name": self.board.GetLayerName(layer_id),
                            "type": self._get_layer_type_name(self.board.GetLayerType(layer_id)),
                            "id": layer_id,
                        }
                    )

            return {
                "success": True,
                "board": {
                    "filename": self.board.GetFileName(),
                    "size": {"width": width_mm, "height": height_mm, "unit": "mm"},
                    "layers": layers,
                    "title": self.board.GetTitleBlock().GetTitle(),
                    # Note: activeLayer removed - GetActiveLayer() doesn't exist in KiCAD 9.0
                    # Active layer is a UI concept not applicable to headless scripting
                },
            }

        except Exception as e:
            logger.error(f"Error getting board info: {str(e)}")
            return {
                "success": False,
                "message": "Failed to get board information",
                "errorDetails": str(e),
            }

    # Layer color map for the per-layer composite render.  Tuned for legibility
    # against a near-black background.  Order is bottom-to-top in the composite.
    _DEFAULT_LAYER_COLORS: List[Tuple[str, str, float]] = [
        ("F.Cu", "#C83737", 0.85),       # red, top copper
        ("B.Cu", "#3771C8", 0.55),       # blue, bottom copper (semi-transparent)
        ("Edge.Cuts", "#D0D0B0", 1.0),   # tan, board outline
        ("F.SilkS", "#F0F0F0", 0.9),     # white-ish, top silkscreen
    ]

    def get_board_2d_view(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Render a 2D image of the PCB.

        New default (cropToBoard=True, colored=True) produces a board-cropped,
        per-layer recolored composite.  The legacy whole-page monochrome
        plot is reachable via cropToBoard=False, colored=False.

        Params:
            width, height: pixel dimensions.  When cropToBoard is True, the
                larger of the two becomes the long-axis size and aspect ratio
                is preserved (so a 42×30 mm board renders without squashing).
            format: "png" | "jpg" | "svg" (default "png").
            layers: optional list of layer names to include.  When omitted,
                colored mode uses _DEFAULT_LAYER_COLORS; uncolored mode plots
                all enabled layers as before.
            cropToBoard: bool, default True.  Crop the SVG viewBox to the
                board's edge bbox + 5% margin.  Set False for the legacy
                whole-page output.
            colored: bool, default True.  Recolor per layer for legibility.
                Set False for the legacy monochrome plot.
            margin: bbox margin as a fraction of the long axis (default 0.05).
        """
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            width = params.get("width", 1600)
            height = params.get("height", 1200)
            format = params.get("format", "png")
            layers = params.get("layers", []) or []
            crop_to_board = params.get("cropToBoard", True)
            colored = params.get("colored", True)
            margin_frac = params.get("margin", 0.05)

            # Legacy path: single-SVG monochrome whole-page plot.
            if not crop_to_board and not colored:
                return self._legacy_plot(layers, width, height, format)

            # New path: per-layer plot, optional crop + recolor + composite.
            return self._composite_plot(
                layers=layers,
                width=width,
                height=height,
                format=format,
                crop_to_board=crop_to_board,
                colored=colored,
                margin_frac=margin_frac,
            )

        except Exception as e:
            logger.error(f"Error getting board 2D view: {str(e)}")
            return {
                "success": False,
                "message": "Failed to get board 2D view",
                "errorDetails": str(e),
            }

    def _legacy_plot(
        self, layers: List[str], width: int, height: int, format: str
    ) -> Dict[str, Any]:
        """Original whole-page, all-layers-into-one-SVG plot."""
        plotter = pcbnew.PLOT_CONTROLLER(self.board)
        plot_opts = plotter.GetPlotOptions()
        plot_opts.SetOutputDirectory(os.path.dirname(self.board.GetFileName()))
        plot_opts.SetScale(1)
        plot_opts.SetMirror(False)
        plot_opts.SetPlotFrameRef(False)
        plot_opts.SetPlotValue(True)
        plot_opts.SetPlotReference(True)

        plotter.OpenPlotfile("temp_view", pcbnew.PLOT_FORMAT_SVG, "Temporary View")
        if layers:
            for layer_name in layers:
                layer_id = self.board.GetLayerID(layer_name)
                if layer_id >= 0 and self.board.IsLayerEnabled(layer_id):
                    plotter.SetLayer(layer_id)
                    plotter.PlotLayer()
        else:
            for layer_id in range(pcbnew.PCB_LAYER_ID_COUNT):
                if self.board.IsLayerEnabled(layer_id):
                    plotter.SetLayer(layer_id)
                    plotter.PlotLayer()
        temp_svg = plotter.GetPlotFileName()
        plotter.ClosePlot()

        return self._svg_file_to_response(temp_svg, width, height, format, cleanup=True)

    def _composite_plot(
        self,
        layers: List[str],
        width: int,
        height: int,
        format: str,
        crop_to_board: bool,
        colored: bool,
        margin_frac: float,
    ) -> Dict[str, Any]:
        """Plot each layer to its own SVG, optionally crop to board bbox and
        recolor, then composite into one image."""
        import re
        import tempfile

        from cairosvg import svg2png

        # Determine layers + colors.
        if layers:
            # Caller-specified layer list.  When colored, use defaults if
            # listed, else fall back to a generic palette.
            layer_specs = [
                (n, self._color_for_layer(n), self._opacity_for_layer(n))
                for n in layers
            ]
        else:
            layer_specs = list(self._DEFAULT_LAYER_COLORS)

        # Compute board bbox once.
        bbox_mm = self._board_bbox_mm()

        # Output dimensions: aspect-correct when cropping to board.
        if crop_to_board:
            x_mm, y_mm, w_mm, h_mm = bbox_mm
            margin = max(w_mm, h_mm) * margin_frac
            aspect = (w_mm + 2 * margin) / (h_mm + 2 * margin)
            long_side = max(width, height)
            if aspect >= 1:
                out_w, out_h = long_side, max(1, int(long_side / aspect))
            else:
                out_w, out_h = max(1, int(long_side * aspect)), long_side
        else:
            out_w, out_h = width, height

        # SVG output: when caller asks for SVG, produce a single SVG with one
        # <g> per layer rather than a rasterised composite.
        if format == "svg":
            return self._composite_svg_response(
                layer_specs, bbox_mm, crop_to_board, colored, margin_frac
            )

        composite = Image.new("RGBA", (out_w, out_h), (24, 24, 24, 255))

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = tmp
            for layer_name, color, opacity in layer_specs:
                svg_path = self._plot_layer_to_svg(layer_name, tmp_dir)
                if svg_path is None:
                    continue
                with open(svg_path, "r", encoding="utf-8") as f:
                    svg_text = f.read()
                if crop_to_board:
                    svg_text = self._svg_set_viewbox(svg_text, bbox_mm, margin_frac)
                if colored:
                    svg_text = self._svg_recolor(svg_text, color)
                png_bytes = svg2png(
                    bytestring=svg_text.encode("utf-8"),
                    output_width=out_w,
                    output_height=out_h,
                )
                layer_img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
                if opacity < 1.0:
                    a = layer_img.split()[3]
                    a = a.point(lambda v: int(v * opacity))
                    layer_img.putalpha(a)
                composite.alpha_composite(layer_img)

        if format == "jpg":
            buf = io.BytesIO()
            composite.convert("RGB").save(buf, format="JPEG", quality=92)
            data = buf.getvalue()
            return {
                "success": True,
                "imageData": base64.b64encode(data).decode("utf-8"),
                "format": "jpg",
            }
        else:
            buf = io.BytesIO()
            composite.convert("RGB").save(buf, format="PNG")
            data = buf.getvalue()
            return {
                "success": True,
                "imageData": base64.b64encode(data).decode("utf-8"),
                "format": "png",
            }

    # ------------------------------------------------------------------
    # SVG / plot helpers
    # ------------------------------------------------------------------

    def _color_for_layer(self, name: str) -> str:
        for n, c, _op in self._DEFAULT_LAYER_COLORS:
            if n == name:
                return c
        # Fallback palette by hash of name → keeps ad-hoc layers distinguishable.
        palette = ["#C83737", "#3771C8", "#37C857", "#C8B937", "#9637C8", "#37C8B9"]
        return palette[hash(name) % len(palette)]

    def _opacity_for_layer(self, name: str) -> float:
        for n, _c, op in self._DEFAULT_LAYER_COLORS:
            if n == name:
                return op
        return 0.85

    def _board_bbox_mm(self) -> Tuple[float, float, float, float]:
        nm = 1_000_000
        box = self.board.GetBoardEdgesBoundingBox()
        return (box.GetX() / nm, box.GetY() / nm, box.GetWidth() / nm, box.GetHeight() / nm)

    def _plot_layer_to_svg(self, layer_name: str, out_dir: str) -> Optional[str]:
        layer_id = self.board.GetLayerID(layer_name)
        if layer_id < 0 or not self.board.IsLayerEnabled(layer_id):
            return None
        plotter = pcbnew.PLOT_CONTROLLER(self.board)
        opts = plotter.GetPlotOptions()
        opts.SetOutputDirectory(out_dir)
        opts.SetScale(1)
        opts.SetMirror(False)
        opts.SetPlotFrameRef(False)
        opts.SetPlotValue(True)
        opts.SetPlotReference(True)
        plotter.OpenPlotfile(
            layer_name.replace(".", "_"), pcbnew.PLOT_FORMAT_SVG, layer_name
        )
        plotter.SetLayer(layer_id)
        plotter.PlotLayer()
        path = plotter.GetPlotFileName()
        plotter.ClosePlot()
        return path if os.path.exists(path) else None

    @staticmethod
    def _svg_set_viewbox(
        svg_text: str, bbox_mm: Tuple[float, float, float, float], margin_frac: float
    ) -> str:
        import re
        x_mm, y_mm, w_mm, h_mm = bbox_mm
        margin = max(w_mm, h_mm) * margin_frac
        vb = (
            f'viewBox="{x_mm - margin} {y_mm - margin} '
            f'{w_mm + 2 * margin} {h_mm + 2 * margin}"'
        )
        svg_text = re.sub(r'viewBox="[^"]*"', vb, svg_text, count=1)
        # Drop fixed width/height on the root <svg> so the viewer scales to viewBox.
        svg_text = re.sub(r'<svg([^>]*?)\bwidth="[^"]*"', r'<svg\1', svg_text, count=1)
        svg_text = re.sub(r'<svg([^>]*?)\bheight="[^"]*"', r'<svg\1', svg_text, count=1)
        return svg_text

    @staticmethod
    def _svg_recolor(svg_text: str, color: str) -> str:
        import re
        # KiCad's plot uses CSS in `style="fill:#000000; ...; stroke:#000000; ..."`.
        # Replace any non-white explicit fill / stroke colour with our chosen one.
        def repl(m: "re.Match[str]") -> str:
            prop, val = m.group(1), m.group(2).lower()
            if val in ("none", "#ffffff", "white"):
                return m.group(0)
            return f"{prop}:{color}"
        return re.sub(
            r"(fill|stroke):(#[0-9a-fA-F]{6}|[A-Za-z]+)", repl, svg_text
        )

    def _composite_svg_response(
        self,
        layer_specs: List[Tuple[str, str, float]],
        bbox_mm: Tuple[float, float, float, float],
        crop_to_board: bool,
        colored: bool,
        margin_frac: float,
    ) -> Dict[str, Any]:
        """Build a single SVG with each layer wrapped in a <g> (with opacity)."""
        import re
        import tempfile

        groups: List[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            for layer_name, color, opacity in layer_specs:
                svg_path = self._plot_layer_to_svg(layer_name, tmp)
                if svg_path is None:
                    continue
                with open(svg_path, "r", encoding="utf-8") as f:
                    svg_text = f.read()
                if colored:
                    svg_text = self._svg_recolor(svg_text, color)
                # Extract the inside of <svg> ... </svg>.
                m = re.search(r"<svg[^>]*>(.*)</svg>", svg_text, re.S)
                inner = m.group(1) if m else ""
                groups.append(
                    f'<g id="{layer_name}" opacity="{opacity}">{inner}</g>'
                )

        x_mm, y_mm, w_mm, h_mm = bbox_mm
        if crop_to_board:
            margin = max(w_mm, h_mm) * margin_frac
            vb = f"{x_mm - margin} {y_mm - margin} {w_mm + 2 * margin} {h_mm + 2 * margin}"
        else:
            # Use the un-cropped page; KiCad emits 297×210 mm typical.
            vb = f"0 0 297 210"
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}" '
            f'style="background:#181818">'
            f'{"".join(groups)}'
            f'</svg>'
        )
        return {"success": True, "imageData": svg, "format": "svg"}

    def _svg_file_to_response(
        self, temp_svg: str, width: int, height: int, format: str, cleanup: bool = True
    ) -> Dict[str, Any]:
        """Convert a single SVG file to the requested image format response."""
        if format == "svg":
            with open(temp_svg, "r") as f:
                svg_data = f.read()
            if cleanup and os.path.exists(temp_svg):
                os.remove(temp_svg)
            return {"success": True, "imageData": svg_data, "format": "svg"}

        from cairosvg import svg2png
        png_data = svg2png(url=temp_svg, output_width=width, output_height=height)
        if cleanup and os.path.exists(temp_svg):
            os.remove(temp_svg)

        if format == "jpg":
            img = Image.open(io.BytesIO(png_data))
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=92)
            data = buf.getvalue()
            return {
                "success": True,
                "imageData": base64.b64encode(data).decode("utf-8"),
                "format": "jpg",
            }
        return {
            "success": True,
            "imageData": base64.b64encode(png_data).decode("utf-8"),
            "format": "png",
        }

    def _get_layer_type_name(self, type_id: int) -> str:
        """Convert KiCAD layer type constant to name"""
        type_map = {
            pcbnew.LT_SIGNAL: "signal",
            pcbnew.LT_POWER: "power",
            pcbnew.LT_MIXED: "mixed",
            pcbnew.LT_JUMPER: "jumper",
        }
        # Note: LT_USER was removed in KiCAD 9.0
        return type_map.get(type_id, "unknown")

    def get_board_extents(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Get the bounding box extents of the board"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            # Get unit preference (default to mm)
            unit = params.get("unit", "mm")
            scale = 1000000 if unit == "mm" else 25400000  # nm to mm or inch

            # Get board bounding box
            board_box = self.board.GetBoardEdgesBoundingBox()

            # Extract bounds in nanometers, then convert
            left = board_box.GetLeft() / scale
            top = board_box.GetTop() / scale
            right = board_box.GetRight() / scale
            bottom = board_box.GetBottom() / scale
            width = board_box.GetWidth() / scale
            height = board_box.GetHeight() / scale

            # Get center point
            center_x = board_box.GetCenter().x / scale
            center_y = board_box.GetCenter().y / scale

            return {
                "success": True,
                "extents": {
                    "left": left,
                    "top": top,
                    "right": right,
                    "bottom": bottom,
                    "width": width,
                    "height": height,
                    "center": {"x": center_x, "y": center_y},
                    "unit": unit,
                },
            }

        except Exception as e:
            logger.error(f"Error getting board extents: {str(e)}")
            return {
                "success": False,
                "message": "Failed to get board extents",
                "errorDetails": str(e),
            }
