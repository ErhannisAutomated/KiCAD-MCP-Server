"""Real-time visualization of the autoplacer simulation state.

Designed for an iPython session next to a `PLACER` instance.  Components
draw as bbox rectangles + pin dots + ref labels; forces draw as colour-
intensity-scaled lines:

  * Red lines: pin-pair attraction (one line per active net edge).
  * Blue lines: component-pair repulsion (top-k by magnitude — N(N-1)/2
    pairs is too noisy for a typical 30-component sheet).
  * Green stub from each component centre: the SUM of forces acting on
    that component (effective acceleration direction).

Open with ``viz = AutoplacerViz(sess)``; call ``viz.update()`` to redraw.
The figure stays in matplotlib interactive mode so you can iterate the
sim and refresh on demand without blocking iPython.

Usage::

    %matplotlib qt5  # or 'tk' / 'qtagg' depending on what's installed
    from commands.autoplacer import PLACER
    from commands.autoplacer_viz import AutoplacerViz

    PLACER.load(path)
    viz = AutoplacerViz(PLACER.get(path))
    PLACER.iterate(path, n=20)
    viz.update()  # refresh after each batch

Headless / test environments: the module can be imported without a
display.  Construction succeeds under the ``Agg`` backend (which never
opens a window); ``update()`` works there too — useful for snapshotting
state to PNG via ``viz.save("/tmp/state.png")``.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

# matplotlib is imported lazily so a non-viz import of the autoplacer
# package doesn't pay the cost.  AutoplacerViz construction triggers it.
_MPL_LOAD_ERROR: Optional[Exception] = None


def _import_matplotlib():
    """Lazy import; raises a helpful message if matplotlib is missing."""
    global _MPL_LOAD_ERROR
    try:
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle
        return plt, Rectangle
    except Exception as e:
        _MPL_LOAD_ERROR = e
        raise RuntimeError(
            "matplotlib is required for autoplacer_viz; install with "
            "`pip install matplotlib` and try again"
        ) from e


from commands.autoplacer import (
    Session,
    _attractive_force_pinwise,
    _boundary_force,
    _component_pair_force,
    _polarity_force,
    _torque_for_pin_orientation,
)


class AutoplacerViz:
    """Live view of an autoplacer Session backed by matplotlib.

    Construction opens a figure (interactive mode); ``update()``
    redraws based on the current sess state.  The displayed forces
    are computed on-the-fly from ``sess`` and the same helpers
    ``iterate()`` uses, so the viz reflects whatever the placer
    *would* do on the next step.
    """

    BG = "#101010"
    BBOX_COLOR = "#404040"
    PIN_COLOR = "#00ccff"
    LABEL_COLOR = "#cccccc"
    SUM_FORCE_COLOR = "#00ff00"
    SHEET_BBOX_COLOR = "#202028"

    def __init__(
        self,
        sess: Session,
        *,
        figsize: Tuple[float, float] = (13, 9),
        max_repulsion_lines: int = 30,
        show_attraction: bool = True,
        show_repulsion: bool = True,
        show_sum_force: bool = True,
        show_pin_labels: bool = False,
    ):
        self.sess = sess
        self.max_repulsion_lines = max_repulsion_lines
        self.show_attraction = show_attraction
        self.show_repulsion = show_repulsion
        self.show_sum_force = show_sum_force
        self.show_pin_labels = show_pin_labels

        plt, _ = _import_matplotlib()
        plt.ion()
        self.fig, self.ax = plt.subplots(figsize=figsize)
        self.fig.patch.set_facecolor(self.BG)
        self._configure_axes()
        self.update()
        # Some backends need an explicit show; non-interactive ones (Agg)
        # silently no-op.
        try:
            plt.show(block=False)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Public surface

    def update(self) -> None:
        """Redraw the figure based on current sess state."""
        self.ax.clear()
        self._configure_axes()

        comps = list(self.sess.components.values())
        if not comps:
            self.fig.canvas.draw_idle()
            return

        self._draw_sheet_bbox()
        for c in comps:
            self._draw_component(c)

        forces, _torques = self._compute_forces()
        if self.show_attraction:
            self._draw_attraction()
        if self.show_repulsion:
            self._draw_repulsion()
        if self.show_sum_force:
            self._draw_sum_forces(forces)

        self._set_view(comps)
        self._set_title()

        self.fig.canvas.draw_idle()
        try:
            self.fig.canvas.flush_events()
        except Exception:
            pass

    def save(self, path: str, dpi: int = 150) -> None:
        """Write the current figure to a PNG file."""
        self.fig.savefig(path, dpi=dpi, facecolor=self.fig.get_facecolor())

    def close(self) -> None:
        """Close the figure window."""
        plt, _ = _import_matplotlib()
        plt.close(self.fig)

    # ------------------------------------------------------------------
    # Drawing helpers

    def _configure_axes(self) -> None:
        self.ax.set_facecolor(self.BG)
        self.ax.set_aspect("equal")
        # Schematic Y is down (KiCad convention), so we invert to match
        # what KiCad shows.
        if not self.ax.yaxis_inverted():
            self.ax.invert_yaxis()
        self.ax.tick_params(colors=self.LABEL_COLOR)
        for spine in self.ax.spines.values():
            spine.set_color(self.LABEL_COLOR)

    def _draw_sheet_bbox(self) -> None:
        _, Rectangle = _import_matplotlib()
        p = self.sess.params
        rect = Rectangle(
            (p.sheet_x_min, p.sheet_y_min),
            p.sheet_x_max - p.sheet_x_min,
            p.sheet_y_max - p.sheet_y_min,
            fill=False, edgecolor=self.SHEET_BBOX_COLOR, linewidth=0.6,
            linestyle="--",
        )
        self.ax.add_patch(rect)

    def _draw_component(self, c) -> None:
        _, Rectangle = _import_matplotlib()
        rect = Rectangle(
            (c.x - c.bbox_w / 2, c.y - c.bbox_h / 2),
            c.bbox_w, c.bbox_h,
            fill=False, edgecolor=self.BBOX_COLOR, linewidth=0.8,
        )
        self.ax.add_patch(rect)
        self.ax.text(
            c.x, c.y, f"{c.ref}{f'.{c.unit}' if c.unit > 1 else ''}",
            color=self.LABEL_COLOR, ha="center", va="center", fontsize=7,
        )
        for pn in c.pins:
            wp = c.world_pin_xy(pn)
            if wp is None:
                continue
            self.ax.plot(wp[0], wp[1], "o", color=self.PIN_COLOR, markersize=2.5)
            if self.show_pin_labels:
                self.ax.text(
                    wp[0], wp[1], f" {pn}",
                    color=self.PIN_COLOR, fontsize=5, ha="left", va="center",
                )

    def _compute_forces(
        self,
    ) -> Tuple[Dict[str, Tuple[float, float]], Dict[str, float]]:
        """Mirror what ``iterate()`` does but return per-component forces
        and torques without applying them.  Used to colour the viz."""
        p = self.sess.params
        forces: Dict[str, Tuple[float, float]] = {}
        torques: Dict[str, float] = {}
        comps = list(self.sess.components.values())
        for c in comps:
            fx = fy = 0.0
            for other in comps:
                if other is c:
                    continue
                rfx, rfy = _component_pair_force(c, other, p.repulsion_k)
                fx += rfx
                fy += rfy
            bfx, bfy = _boundary_force(c, p)
            fx += bfx
            fy += bfy
            pfx, pfy = _polarity_force(c, self.sess)
            fx += pfx
            fy += pfy
            forces[c.key] = (fx, fy)
            torques[c.key] = _torque_for_pin_orientation(c, self.sess)

        for net in self.sess.nets.values():
            pin_list = net.pins
            if len(pin_list) < 2:
                continue
            for i, (key_a, pin_a) in enumerate(pin_list):
                for key_b, pin_b in pin_list[i + 1 :]:
                    if key_a == key_b:
                        continue
                    a = self.sess.components.get(key_a)
                    b = self.sess.components.get(key_b)
                    if a is None or b is None:
                        continue
                    afx, afy = _attractive_force_pinwise(
                        a, pin_a, b, pin_b, p.attraction_k,
                    )
                    forces[key_a] = (
                        forces[key_a][0] + afx, forces[key_a][1] + afy,
                    )
                    forces[key_b] = (
                        forces[key_b][0] - afx, forces[key_b][1] - afy,
                    )
        return forces, torques

    def _draw_attraction(self) -> None:
        """Pin-pair attraction lines, intensity scaled to magnitude."""
        p = self.sess.params
        pairs: List[Tuple[Tuple[float, float], Tuple[float, float], float]] = []
        for net in self.sess.nets.values():
            pin_list = net.pins
            if len(pin_list) < 2:
                continue
            for i, (key_a, pin_a) in enumerate(pin_list):
                for key_b, pin_b in pin_list[i + 1 :]:
                    if key_a == key_b:
                        continue
                    a = self.sess.components.get(key_a)
                    b = self.sess.components.get(key_b)
                    if a is None or b is None:
                        continue
                    pa = a.world_pin_xy(pin_a)
                    pb = b.world_pin_xy(pin_b)
                    if pa is None or pb is None:
                        continue
                    fx, fy = _attractive_force_pinwise(
                        a, pin_a, b, pin_b, p.attraction_k,
                    )
                    pairs.append((pa, pb, math.hypot(fx, fy)))
        if not pairs:
            return
        max_mag = max(m for _, _, m in pairs) or 1.0
        for pa, pb, mag in pairs:
            t = mag / max_mag
            color = (0.35 + 0.65 * t, 0.0, 0.0)
            self.ax.plot(
                [pa[0], pb[0]], [pa[1], pb[1]],
                color=color, alpha=0.55, linewidth=0.6 + 1.0 * t,
            )

    def _draw_repulsion(self) -> None:
        """Top-k component-pair repulsion lines."""
        p = self.sess.params
        comps = list(self.sess.components.values())
        pairs = []
        for i, a in enumerate(comps):
            for b in comps[i + 1 :]:
                fx, fy = _component_pair_force(a, b, p.repulsion_k)
                pairs.append((a, b, math.hypot(fx, fy)))
        if not pairs:
            return
        pairs.sort(key=lambda x: -x[2])
        topk = pairs[: self.max_repulsion_lines]
        max_mag = topk[0][2] or 1.0
        for a, b, mag in topk:
            t = mag / max_mag
            color = (0.0, 0.0, 0.35 + 0.65 * t)
            self.ax.plot(
                [a.x, b.x], [a.y, b.y],
                color=color, alpha=0.45, linewidth=0.5 + 0.8 * t,
            )

    def _draw_sum_forces(
        self, forces: Dict[str, Tuple[float, float]]
    ) -> None:
        """Per-component summed-force arrow from the centre."""
        if not forces:
            return
        max_mag = max(0.001, max(math.hypot(fx, fy) for fx, fy in forces.values()))
        # Map the largest sum-force vector to ~1.5 grid units (1.91 mm)
        # so arrows are visible without obscuring nearby components.
        scale = 8.0 / max_mag
        for key, (fx, fy) in forces.items():
            c = self.sess.components.get(key)
            if c is None:
                continue
            mag = math.hypot(fx, fy)
            if mag < 1e-3:
                continue
            ex = c.x + fx * scale
            ey = c.y + fy * scale
            self.ax.plot(
                [c.x, ex], [c.y, ey],
                color=self.SUM_FORCE_COLOR, alpha=0.75, linewidth=1.0,
            )
            self.ax.plot(
                [ex], [ey], "o",
                color=self.SUM_FORCE_COLOR, markersize=3,
            )

    def _set_view(self, comps) -> None:
        xs = [c.x for c in comps]
        ys = [c.y for c in comps]
        bbox_max = max((c.bbox_w + c.bbox_h) / 2 for c in comps)
        margin = max(20, bbox_max + 10)
        self.ax.set_xlim(min(xs) - margin, max(xs) + margin)
        # Y is inverted (schematic convention): set top first, bottom second.
        self.ax.set_ylim(max(ys) + margin, min(ys) - margin)

    def _set_title(self) -> None:
        title = (
            f"iter={self.sess.iteration}  "
            f"T={self.sess.temperature:.2f}  "
            f"max_F={self.sess.last_max_force:.3f}  "
            f"({len(self.sess.components)} comps, {len(self.sess.nets)} nets)"
        )
        self.ax.set_title(title, color=self.LABEL_COLOR, fontsize=10)


def show_session(schematic_path) -> AutoplacerViz:
    """Convenience: open a viz on the PLACER session for `schematic_path`.

    Errors if no session is loaded — call ``PLACER.load(path)`` first.
    """
    from commands.autoplacer import PLACER

    sess = PLACER.get(str(schematic_path))
    if sess is None:
        raise ValueError(
            f"No autoplacer session loaded for {schematic_path}; "
            "call PLACER.load(path) first."
        )
    return AutoplacerViz(sess)
