"""Real-time visualization of the PCB autoplacer simulation state.

Parallel to ``autoplacer_viz.AutoplacerViz`` but PCB-aware: OBB-rotated
component rectangles, Edge.Cuts keep-in instead of the schematic sheet
bbox, ratsnest segments colored by resolved spring class, and force
arrows derived from the v2 physics (OBB cubic-ramp repulsion +
spring-class-modulated pin-wise springs).

Designed for a Jupyter / iPython session next to a live PCB ``Session``.
The figure stays in matplotlib interactive mode so the user can iterate
the sim and call ``viz.update()`` to refresh in place — same pop-out
window across calls, no flicker.

Usage::

    %matplotlib qt5    # or 'tk' / 'qtagg' — needs a backend that
                       # supports a real window (not Agg)
    import pcbnew
    from commands.pcb_autoplacer import load_pcb_session, edge_cuts_bbox
    from commands.pcb_autoplacer_viz import PCBAutoplacerViz
    from commands.autoplacer import iterate

    board = pcbnew.LoadBoard(path)
    sess = load_pcb_session(board)
    keep_in = edge_cuts_bbox(board, inset_mm=1.0)

    viz = PCBAutoplacerViz(sess, keep_in_bbox=keep_in)
    # Adjust params; iterate; refresh:
    sess.params.attraction_k = 0.1
    sess.params.repulsion_k = 30.0
    iterate(sess, n=20)
    viz.update()    # window updates in place

Headless / test environments: construction succeeds under the ``Agg``
backend (which never opens a window); ``update()`` works there too —
useful for snapshotting state to PNG via ``viz.save("/tmp/state.png")``.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

_MPL_LOAD_ERROR: Optional[Exception] = None


def _import_matplotlib():
    """Lazy import; raises a helpful message if matplotlib is missing."""
    global _MPL_LOAD_ERROR
    try:
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle, FancyArrowPatch
        import matplotlib.lines as mlines
        return plt, Rectangle, FancyArrowPatch, mlines
    except Exception as e:
        _MPL_LOAD_ERROR = e
        raise RuntimeError(
            "matplotlib is required for pcb_autoplacer_viz; install with "
            "`pip install matplotlib` and try again"
        ) from e


from commands.autoplacer import (
    Session,
    _attractive_force_pinwise,
    obb_repulsion_force,
    resolve_pair_class,
)


# ----------------------------------------------------------------------
# Spring-class palette (kept in one place so tuning scripts and the viz
# stay visually consistent).
# ----------------------------------------------------------------------

SPRING_CLASS_COLORS: Dict[str, str] = {
    "DECOUPLING":   "#ff3366",   # bright red — strong pull
    "LOCAL_SIGNAL": "#7799cc",   # blue-gray — default
    "INTER_GROUP":  "#445566",   # darker blue — weak
    "PLANE":        "#222222",   # nearly invisible — zero force
}


class PCBAutoplacerViz:
    """Live view of a PCB autoplacer Session backed by matplotlib.

    Construction opens a figure in interactive mode; ``update()``
    redraws the same axes based on current ``sess`` state.  The displayed
    forces are computed on-the-fly with the SAME helpers ``iterate()``
    uses, so the viz reflects whatever the next physics step would do.
    """

    BG = "#101010"
    BBOX_COLOR = "#666666"
    ANCHOR_BBOX_COLOR = "#ff5577"
    LABEL_COLOR = "#cccccc"
    ANCHOR_LABEL_COLOR = "#ffaabb"
    PIN_COLOR = "#00ccff"
    KEEP_IN_COLOR = "#406040"
    SUM_FORCE_COLOR = "#33ff33"
    REPULSION_COLOR = "#3366ff"
    ATTRACTION_COLORS = SPRING_CLASS_COLORS

    def __init__(
        self,
        sess: Session,
        *,
        keep_in_bbox: Optional[Tuple[float, float, float, float]] = None,
        figsize: Tuple[float, float] = (13, 9),
        show_pins: bool = True,
        show_ratsnest: bool = True,
        show_sum_force: bool = True,
        show_repulsion: bool = False,
        max_repulsion_lines: int = 30,
        skip_classes: Optional[set] = None,
    ):
        self.sess = sess
        self.keep_in_bbox = keep_in_bbox
        self.show_pins = show_pins
        self.show_ratsnest = show_ratsnest
        self.show_sum_force = show_sum_force
        self.show_repulsion = show_repulsion
        self.max_repulsion_lines = max_repulsion_lines
        # Spring classes to omit from the ratsnest overlay.  PLANE
        # exerts no force but contributes many segments to large
        # power/ground nets — skipping it speeds up redraws
        # noticeably on bigger boards.  Defaults to {"PLANE"} so the
        # viz stays fast out of the box; pass an empty set to draw
        # everything, or include other classes you want to mute.
        self.skip_classes = {"PLANE"} if skip_classes is None else set(skip_classes)

        plt, _, _, _ = _import_matplotlib()
        plt.ion()
        self.fig, self.ax = plt.subplots(figsize=figsize)
        self.fig.patch.set_facecolor(self.BG)
        self._configure_axes()
        self.update()
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

        self._draw_keep_in()
        for c in comps:
            self._draw_component(c)

        if self.show_ratsnest:
            self._draw_ratsnest()
        if self.show_repulsion:
            self._draw_repulsion()
        if self.show_sum_force:
            forces = self._compute_forces()
            self._draw_sum_forces(forces)

        self._set_view(comps)
        self._set_title()
        self._draw_legend()

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
        plt, _, _, _ = _import_matplotlib()
        plt.close(self.fig)

    # ------------------------------------------------------------------
    # Drawing helpers

    def _configure_axes(self) -> None:
        self.ax.set_facecolor(self.BG)
        self.ax.set_aspect("equal")
        # PCB Y is screen-down (KiCad convention); invert so the figure
        # matches what you see in pcbnew.
        if not self.ax.yaxis_inverted():
            self.ax.invert_yaxis()
        self.ax.tick_params(colors=self.LABEL_COLOR)
        for spine in self.ax.spines.values():
            spine.set_color(self.LABEL_COLOR)

    def _draw_keep_in(self) -> None:
        if not self.keep_in_bbox:
            return
        kl, kt, kr, kb = self.keep_in_bbox
        if kr <= kl or kb <= kt:
            return
        _, Rectangle, _, _ = _import_matplotlib()
        self.ax.add_patch(Rectangle(
            (kl, kt), kr - kl, kb - kt,
            fill=False, edgecolor=self.KEEP_IN_COLOR,
            linestyle="--", linewidth=1.0,
        ))

    def _draw_component(self, c) -> None:
        _, Rectangle, _, _ = _import_matplotlib()
        edge = self.ANCHOR_BBOX_COLOR if c.pinned else self.BBOX_COLOR
        lw = 1.6 if c.pinned else 0.8
        # Draw bbox at the OBB center, which equals (c.x, c.y) for
        # symmetric footprints and is offset for ones whose origin
        # isn't the body center (pin headers anchored at pin 1).
        bx, by = c.obb_center_world()
        # matplotlib's Rectangle angle is CCW in DATA coords; after
        # invert_yaxis(), that's the OPPOSITE of KiCad's screen-CCW.
        rect = Rectangle(
            (bx - c.bbox_w / 2, by - c.bbox_h / 2),
            c.bbox_w, c.bbox_h,
            angle=-c.rotation, rotation_point="center",
            fill=False, edgecolor=edge, linewidth=lw,
        )
        self.ax.add_patch(rect)
        self.ax.text(
            bx, by, c.ref,
            color=self.ANCHOR_LABEL_COLOR if c.pinned else self.LABEL_COLOR,
            ha="center", va="center", fontsize=7,
            fontweight="bold" if c.pinned else "normal",
        )
        if self.show_pins:
            for pn in c.pins:
                wp = c.world_pin_xy(pn)
                if wp is not None:
                    self.ax.plot(wp[0], wp[1], "o",
                                 color=self.PIN_COLOR, markersize=1.5)

    def _draw_ratsnest(self) -> None:
        """One line per pin-pair, colored by resolved spring class.
        PLANE-class pairs draw very faint (k=0, so no actual force)."""
        for net in self.sess.nets.values():
            if len(net.pins) < 2:
                continue
            for i, (key_a, pin_a) in enumerate(net.pins):
                for key_b, pin_b in net.pins[i + 1:]:
                    if key_a == key_b:
                        continue
                    a = self.sess.components.get(key_a)
                    b = self.sess.components.get(key_b)
                    if a is None or b is None:
                        continue
                    wpa = a.world_pin_xy(pin_a)
                    wpb = b.world_pin_xy(pin_b)
                    if wpa is None or wpb is None:
                        continue
                    cls = resolve_pair_class(
                        self.sess.spring_classes, self.sess.nets,
                        a, pin_a, b, pin_b, net.name,
                    )
                    if cls.name in self.skip_classes:
                        continue
                    color = self.ATTRACTION_COLORS.get(cls.name, "#777777")
                    alpha = 0.10 if cls.name == "PLANE" else 0.50
                    lw = 0.6 + min(1.5, cls.spring_k * 0.2)
                    self.ax.plot(
                        [wpa[0], wpb[0]], [wpa[1], wpb[1]],
                        "-", color=color, linewidth=lw, alpha=alpha,
                    )

    def _draw_repulsion(self) -> None:
        """Top-k OBB-repulsion pairs (active ones — within margin)."""
        p = self.sess.params
        comps = list(self.sess.components.values())
        pairs = []
        for i, a in enumerate(comps):
            for b in comps[i + 1:]:
                if a.layer != b.layer:
                    continue
                margin = max(
                    a.margin if a.margin is not None else p.obb_repulsion_margin,
                    b.margin if b.margin is not None else p.obb_repulsion_margin,
                )
                ax, ay = a.obb_center_world()
                bx, by = b.obb_center_world()
                fx, fy = obb_repulsion_force(
                    ax, ay, a.bbox_w, a.bbox_h, a.rotation,
                    bx, by, b.bbox_w, b.bbox_h, b.rotation,
                    margin=margin, k=p.repulsion_k,
                )
                mag = math.hypot(fx, fy)
                if mag > 1e-6:
                    pairs.append((a, b, ax, ay, bx, by, mag))
        if not pairs:
            return
        pairs.sort(key=lambda x: -x[6])
        topk = pairs[: self.max_repulsion_lines]
        max_mag = topk[0][6] or 1.0
        for a, b, ax, ay, bx, by, mag in topk:
            t = mag / max_mag
            color = (0.0, 0.0, 0.35 + 0.65 * t)
            self.ax.plot(
                [ax, bx], [ay, by],
                color=color, alpha=0.45, linewidth=0.5 + 0.8 * t,
            )

    def _compute_forces(self) -> Dict[str, Tuple[float, float]]:
        """Same physics iterate() applies, but without mutating state.
        Used for the green sum-force arrows."""
        p = self.sess.params
        forces: Dict[str, Tuple[float, float]] = {
            c.key: (0.0, 0.0) for c in self.sess.components.values()
        }
        comps = list(self.sess.components.values())

        # Body repulsion (OBB only — the PCB path).
        for i, a in enumerate(comps):
            for b in comps[i + 1:]:
                if a.layer != b.layer:
                    continue
                margin = max(
                    a.margin if a.margin is not None else p.obb_repulsion_margin,
                    b.margin if b.margin is not None else p.obb_repulsion_margin,
                )
                ax, ay = a.obb_center_world()
                bx, by = b.obb_center_world()
                fx, fy = obb_repulsion_force(
                    ax, ay, a.bbox_w, a.bbox_h, a.rotation,
                    bx, by, b.bbox_w, b.bbox_h, b.rotation,
                    margin=margin, k=p.repulsion_k,
                )
                ax_, ay_ = forces[a.key]
                forces[a.key] = (ax_ + fx, ay_ + fy)
                bx_, by_ = forces[b.key]
                forces[b.key] = (bx_ - fx, by_ - fy)

        # Pin-wise springs, scaled by resolved class.
        for net in self.sess.nets.values():
            if len(net.pins) < 2:
                continue
            for i, (key_a, pin_a) in enumerate(net.pins):
                for key_b, pin_b in net.pins[i + 1:]:
                    if key_a == key_b:
                        continue
                    a = self.sess.components.get(key_a)
                    b = self.sess.components.get(key_b)
                    if a is None or b is None:
                        continue
                    cls = resolve_pair_class(
                        self.sess.spring_classes, self.sess.nets,
                        a, pin_a, b, pin_b, net.name,
                    )
                    eff_k = p.attraction_k * cls.spring_k
                    if eff_k == 0.0:
                        continue
                    fx, fy = _attractive_force_pinwise(
                        a, pin_a, b, pin_b, eff_k,
                    )
                    ax_, ay_ = forces[a.key]
                    bx_, by_ = forces[b.key]
                    forces[a.key] = (ax_ + fx, ay_ + fy)
                    forces[b.key] = (bx_ - fx, by_ - fy)
        return forces

    def _draw_sum_forces(self, forces: Dict[str, Tuple[float, float]]) -> None:
        if not forces:
            return
        max_mag = max(
            1e-3, max(math.hypot(fx, fy) for fx, fy in forces.values())
        )
        # Map the largest sum-force to ~8 mm so arrows are visible
        # without obscuring nearby components.
        scale = 8.0 / max_mag
        for key, (fx, fy) in forces.items():
            c = self.sess.components.get(key)
            if c is None or c.pinned:
                continue
            mag = math.hypot(fx, fy)
            if mag < 1e-3:
                continue
            # Anchor the arrow at the body center (= OBB center) so it
            # reads naturally for off-center bboxes like pin headers,
            # even though the force is applied to the footprint origin.
            bx, by = c.obb_center_world()
            ex = bx + fx * scale
            ey = by + fy * scale
            self.ax.plot(
                [bx, ex], [by, ey],
                color=self.SUM_FORCE_COLOR, alpha=0.75, linewidth=1.0,
            )
            self.ax.plot([ex], [ey], "o",
                         color=self.SUM_FORCE_COLOR, markersize=3)

    def _set_view(self, comps) -> None:
        # Prefer the keep-in bbox if present (matches what KiCad shows);
        # otherwise fit components with a margin.
        if self.keep_in_bbox and self.keep_in_bbox[2] > self.keep_in_bbox[0]:
            kl, kt, kr, kb = self.keep_in_bbox
            pad = max(5.0, 0.05 * (kr - kl))
            self.ax.set_xlim(kl - pad, kr + pad)
            self.ax.set_ylim(kb + pad, kt - pad)  # inverted Y
            return
        xs = [c.x for c in comps]
        ys = [c.y for c in comps]
        bbox_max = max((c.bbox_w + c.bbox_h) / 2 for c in comps)
        margin = max(10, bbox_max + 5)
        self.ax.set_xlim(min(xs) - margin, max(xs) + margin)
        self.ax.set_ylim(max(ys) + margin, min(ys) - margin)

    def _set_title(self) -> None:
        p = self.sess.params
        title = (
            f"iter={self.sess.iteration}  "
            f"T={self.sess.temperature:.2f}  "
            f"max_F={self.sess.last_max_force:.2f}  "
            f"(k_a={p.attraction_k:.2f} k_r={p.repulsion_k:.1f} "
            f"snap={p.rotation_snap_strength:.1f})  "
            f"{len(self.sess.components)} comps / {len(self.sess.nets)} nets"
        )
        self.ax.set_title(title, color=self.LABEL_COLOR, fontsize=10)

    def _draw_legend(self) -> None:
        _, _, _, mlines = _import_matplotlib()
        items = [
            mlines.Line2D([], [], color=col, label=name, linewidth=2)
            for name, col in self.ATTRACTION_COLORS.items()
        ]
        items.append(mlines.Line2D(
            [], [], color=self.ANCHOR_BBOX_COLOR, marker="s",
            linestyle="", label="anchored",
        ))
        if self.show_sum_force:
            items.append(mlines.Line2D(
                [], [], color=self.SUM_FORCE_COLOR,
                label="net force", linewidth=2,
            ))
        self.ax.legend(
            handles=items, loc="upper right",
            facecolor="#181818", edgecolor="#444444",
            labelcolor=self.LABEL_COLOR, fontsize=8,
        )


def show_board(pcb_path: str) -> PCBAutoplacerViz:
    """Convenience: load a .kicad_pcb and open a viz on the resulting
    Session.  Useful for quick inspection without manually wiring
    LoadBoard + load_pcb_session + viz construction."""
    import pcbnew
    from commands.pcb_autoplacer import load_pcb_session, edge_cuts_bbox

    board = pcbnew.LoadBoard(pcb_path)
    sess = load_pcb_session(board)
    keep_in = edge_cuts_bbox(board, inset_mm=1.0)
    return PCBAutoplacerViz(sess, keep_in_bbox=keep_in)
