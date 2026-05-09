"""Force-directed schematic autoplacer.

Top-level API is the AutoPlacer singleton.  The MCP handlers wrap its
methods so a session can be built up across multiple tool calls without
re-loading the schematic each time.

Design (Fruchterman-Reingold-style, with schematic-specific additions):
  - Each component is a node; each connection between two pins on the same
    net contributes an attractive force between the two components.
  - All component pairs repel each other.
  - Components also feel a boundary force keeping them inside the sheet.
  - "Polarity" nets (GND, BAT-, etc.) bias their connected components
    toward the bottom of the sheet; rails like BAT+, V+, VCC bias upward.
    Optional and tunable.
  - Each connection also produces a small torque on each end-component
    to align the relevant pin's outward direction toward the other
    endpoint.  This is the lever that gets pin orientations right.

The iterate loop is purely in-memory — no schematic I/O between steps.
Snap-to-grid + write-back happen only when `apply()` is called.
"""
from __future__ import annotations

import logging
import math
import re
import uuid as uuid_lib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import sexpdata
from sexpdata import Symbol

logger = logging.getLogger("kicad_interface")


# ----------------------------------------------------------------------
# Data model
# ----------------------------------------------------------------------


@dataclass
class Pin:
    number: str
    name: str
    local_x: float       # in lib coords (Y-up)
    local_y: float
    lib_angle: float     # outward angle in lib convention (degrees)


@dataclass
class Component:
    ref: str
    unit: int                # 1, 2, … (multi-unit components get one Component per unit)
    lib_id: str
    x: float                 # mm; placed-symbol position (Y-down screen coords)
    y: float
    rotation: float          # degrees, free during iteration; snapped at apply
    mirror_x: bool
    mirror_y: bool
    pins: Dict[str, Pin] = field(default_factory=dict)
    bbox_w: float = 7.62     # default ~3 grid units; refined from lib at load
    bbox_h: float = 7.62
    pinned: bool = False     # if True, position locked

    @property
    def key(self) -> str:
        """Synthetic key used to address this component+unit in the placer
        model.  The bare reference designator is what KiCad uses to
        identify the SHARED footprint on PCB; the unit suffix
        differentiates the per-unit instances on the schematic."""
        return f"{self.ref}__u{self.unit}"

    def world_pin_xy(self, pn: str) -> Optional[Tuple[float, float]]:
        """World-coord of pin pn given the component's current x/y/rot."""
        pin = self.pins.get(pn)
        if pin is None:
            return None
        # Mirror the WireDragger.pin_world_xy formula but accept floats:
        # lib Y-up → screen Y-down via initial flip, then optional mirror,
        # then rotation (math-CCW negated to match eeschema's screen-CCW).
        lx = pin.local_x
        ly = -pin.local_y  # Y-flip
        if self.mirror_x:
            ly = -ly
        if self.mirror_y:
            lx = -lx
        rad = math.radians(-self.rotation)  # negate: math-CCW → screen-CCW
        rx = lx * math.cos(rad) - ly * math.sin(rad)
        ry = lx * math.sin(rad) + ly * math.cos(rad)
        return self.x + rx, self.y + ry

    def world_pin_outward_angle(self, pn: str) -> Optional[float]:
        """The outward-pointing direction of pin pn in world coords (degrees)."""
        pin = self.pins.get(pn)
        if pin is None:
            return None
        # Lib pin angle is the INWARD direction (toward symbol body) by
        # KiCad convention; outward = lib_angle + 180.  Rotation adds.
        return (pin.lib_angle + self.rotation + 180) % 360


@dataclass
class Net:
    name: str
    pins: List[Tuple[str, str]] = field(default_factory=list)  # [(component_ref, pin_number), ...]


@dataclass
class Params:
    """Force tunables.  Default values aim for a starting layout the
    user can polish; see the autoplacer docs for guidance on per-design
    tweaks."""

    repulsion_k: float = 800.0       # component-component
    attraction_k: float = 0.05       # connection (per net edge)
    boundary_k: float = 5.0          # boundary repulsion
    polarity_k: float = 0.5          # polarity bias (V+ up, GND down)
    rotation_k: float = 4.0          # pin-orientation torque

    initial_temperature: float = 30.0  # max displacement per iteration (mm)
    cooling: float = 0.95              # temperature *= cooling per iter
    min_temperature: float = 0.05

    # Sheet bounding box.  Components are pulled back inside if outside.
    sheet_x_min: float = 25.0
    sheet_y_min: float = 25.0
    sheet_x_max: float = 270.0
    sheet_y_max: float = 180.0

    # Polarity rules: nets matching these patterns bias toward the
    # bottom (GND-like) or top (V+-like) of the sheet.
    bottom_polarity_nets: Tuple[str, ...] = ("GND", "BAT-", "VSS", "VEE")
    top_polarity_nets: Tuple[str, ...] = ("BAT+", "V+", "VCC", "VDD", "+3V3", "+5V")

    def is_bottom_polarity(self, name: str) -> bool:
        return name in self.bottom_polarity_nets

    def is_top_polarity(self, name: str) -> bool:
        return name in self.top_polarity_nets


@dataclass
class Session:
    schematic_path: Path
    components: Dict[str, Component] = field(default_factory=dict)
    nets: Dict[str, Net] = field(default_factory=dict)
    params: Params = field(default_factory=Params)
    iteration: int = 0
    temperature: float = 30.0
    last_max_force: float = 0.0


# ----------------------------------------------------------------------
# I/O — load schematic into model, write model back to schematic
# ----------------------------------------------------------------------


def _parse_at(node) -> Optional[Tuple[float, float, float]]:
    """Extract (x, y, rotation) from an (at x y [rot]) sexp."""
    if not (isinstance(node, list) and len(node) >= 3 and node[0] == Symbol("at")):
        return None
    try:
        x = float(node[1])
        y = float(node[2])
        rot = float(node[3]) if len(node) >= 4 else 0.0
        return x, y, rot
    except (TypeError, ValueError):
        return None


def _find_lib_symbol(sexp_data, lib_id: str):
    """Return the lib_symbols (symbol "<lib_id>" …) node, or None."""
    for top in sexp_data:
        if not (isinstance(top, list) and top and top[0] == Symbol("lib_symbols")):
            continue
        for sym in top[1:]:
            if (
                isinstance(sym, list) and len(sym) > 1
                and sym[0] == Symbol("symbol") and sym[1] == lib_id
            ):
                return sym
    return None


_SUBSYM_UNIT_RE = re.compile(r"_(\d+)_\d+$")


def _pins_per_unit(sym_node) -> Dict[int, Dict[str, Pin]]:
    """Walk a lib_symbols (symbol "Foo" …) node and return
    {unit_number: {pin_number: Pin}}.

    KiCad lib_symbols groups pins by sub-symbol named like
    "<base>_<unit>_<convert>" (e.g. "FDS6890A_1_1", "FDS6890A_2_1").
    The unit number on each placed (symbol …) block selects one of
    these.  Single-unit symbols use unit 1 with sub-symbol name like
    "R_0_1" / "R_1_1"; convert is body style for symbols with
    de-Morgan variants.

    Sub-symbols whose unit field is "0" are common-to-all-units
    (graphics shared across units) — their pins (rare) are assigned
    to every unit's dict.
    """
    by_unit: Dict[int, Dict[str, Pin]] = {}

    def collect_pins(sub):
        out: Dict[str, Pin] = {}
        for child in sub[2:] if isinstance(sub, list) and len(sub) > 2 else []:
            if not isinstance(child, list) or not child:
                continue
            if child[0] != Symbol("pin") or len(child) < 3:
                continue
            x = y = ang = 0.0
            name = num = ""
            for sp in child[2:]:
                if not isinstance(sp, list) or not sp:
                    continue
                if sp[0] == Symbol("at") and len(sp) >= 3:
                    try:
                        x = float(sp[1])
                        y = float(sp[2])
                        if len(sp) >= 4:
                            ang = float(sp[3])
                    except (TypeError, ValueError):
                        pass
                elif sp[0] == Symbol("name") and len(sp) >= 2:
                    name = str(sp[1]).strip('"')
                elif sp[0] == Symbol("number") and len(sp) >= 2:
                    num = str(sp[1]).strip('"')
            if num:
                out[num] = Pin(num, name, x, y, ang)
        return out

    if not isinstance(sym_node, list):
        return by_unit
    for sub in sym_node[2:]:
        if not (isinstance(sub, list) and len(sub) > 1 and sub[0] == Symbol("symbol")):
            continue
        sub_name = str(sub[1]) if isinstance(sub[1], str) else ""
        m = _SUBSYM_UNIT_RE.search(sub_name)
        unit = int(m.group(1)) if m else 1
        pins = collect_pins(sub)
        if not pins:
            continue
        by_unit.setdefault(unit, {}).update(pins)

    # Pins on "unit 0" sub-symbols are shared — copy into every other unit.
    if 0 in by_unit:
        shared = by_unit.pop(0)
        for u in by_unit:
            for pn, p in shared.items():
                by_unit[u].setdefault(pn, p)
    if not by_unit:
        # Fallback: no sub-symbols matched — treat all pins as unit 1.
        all_pins = {}
        for sub in sym_node[2:] if isinstance(sym_node, list) else []:
            if isinstance(sub, list) and len(sub) > 1 and sub[0] == Symbol("symbol"):
                all_pins.update(collect_pins(sub))
        if all_pins:
            by_unit[1] = all_pins
    return by_unit


def _bbox_from_lib(sym_node) -> Tuple[float, float]:
    """Compute approximate body bbox (width, height) in mm from
    rectangle / polyline graphics in the lib_symbols definition.
    Conservative — falls back to 7.62 × 7.62 if nothing useful is
    found.  Used to size repulsion proportional to actual symbol size.
    """
    xs: List[float] = []
    ys: List[float] = []

    def visit(node):
        if not isinstance(node, list) or not node:
            return
        head = node[0]
        if head == Symbol("rectangle"):
            # (rectangle (start x y) (end x y) ...)
            for sp in node[1:]:
                if isinstance(sp, list) and sp[0] in (Symbol("start"), Symbol("end")):
                    if len(sp) >= 3:
                        try:
                            xs.append(float(sp[1]))
                            ys.append(float(sp[2]))
                        except (TypeError, ValueError):
                            pass
            return
        if head == Symbol("polyline"):
            for sp in node[1:]:
                if isinstance(sp, list) and sp[0] == Symbol("pts"):
                    for xy in sp[1:]:
                        if (
                            isinstance(xy, list) and xy[0] == Symbol("xy")
                            and len(xy) >= 3
                        ):
                            try:
                                xs.append(float(xy[1]))
                                ys.append(float(xy[2]))
                            except (TypeError, ValueError):
                                pass
            return
        if head == Symbol("circle"):
            cx = cy = 0.0
            r = 0.0
            for sp in node[1:]:
                if isinstance(sp, list) and sp[0] == Symbol("center") and len(sp) >= 3:
                    try:
                        cx, cy = float(sp[1]), float(sp[2])
                    except (TypeError, ValueError):
                        pass
                elif isinstance(sp, list) and sp[0] == Symbol("radius") and len(sp) >= 2:
                    try:
                        r = float(sp[1])
                    except (TypeError, ValueError):
                        pass
            xs.extend([cx - r, cx + r])
            ys.extend([cy - r, cy + r])
            return
        for sub in node[1:] if isinstance(node, list) else []:
            visit(sub)

    visit(sym_node)
    if not xs or not ys:
        return 7.62, 7.62
    w = max(xs) - min(xs)
    h = max(ys) - min(ys)
    # Pad by 2 lead-lengths (2 × 2.54 mm = 5.08 mm) on each side so
    # the placer's overlap-resolution leaves room for the wire stubs
    # the rewire step adds at every pin.  Total padding 10.16 mm.
    return max(w + 10.16, 12.7), max(h + 10.16, 12.7)


def load_session(schematic_path: Path) -> Session:
    """Build an in-memory placement session from a .kicad_sch."""
    text = Path(schematic_path).read_text()
    sexp = sexpdata.loads(text)

    sess = Session(schematic_path=Path(schematic_path))

    # Pass 1: components.  Each placed (symbol …) block becomes its own
    # Component (multi-unit symbols already appear as multiple blocks).
    for top in sexp:
        if not (isinstance(top, list) and len(top) > 1 and top[0] == Symbol("symbol")):
            continue
        # placed symbol — has lib_id, at, instances
        lib_id = None
        x = y = 0.0
        rot = 0.0
        mx = my = False
        ref = None
        unit = 1
        for sub in top[1:]:
            if not isinstance(sub, list):
                continue
            if sub[0] == Symbol("lib_id") and len(sub) >= 2:
                lib_id = sub[1]
            elif sub[0] == Symbol("at"):
                at = _parse_at(sub)
                if at:
                    x, y, rot = at
            elif sub[0] == Symbol("mirror"):
                if len(sub) >= 2:
                    mtype = sub[1]
                    if mtype == Symbol("x"):
                        mx = True
                    elif mtype == Symbol("y"):
                        my = True
            elif sub[0] == Symbol("unit") and len(sub) >= 2:
                try:
                    unit = int(sub[1])
                except (TypeError, ValueError):
                    unit = 1
            elif sub[0] == Symbol("property") and len(sub) >= 3:
                if sub[1] == "Reference":
                    ref = sub[2]
        if not (lib_id and ref):
            continue
        # Pin per-unit pin set + bbox from lib_symbols.
        lib_node = _find_lib_symbol(sexp, lib_id)
        if lib_node is None:
            logger.warning(f"autoplacer: lib_id {lib_id} not found in lib_symbols; skipping")
            continue
        pins_per_unit = _pins_per_unit(lib_node)
        unit_pins = pins_per_unit.get(unit) or pins_per_unit.get(1, {})
        bbox_w, bbox_h = _bbox_from_lib(lib_node)
        # Pinned heuristics:
        #   - Connectors (J*): user-facing edges placed deliberately.
        #   - Power flag pseudosymbols (#FLG*): they sit on a specific
        #     net by position, so we must keep them in the model
        #     (otherwise rewire strips their labels and they go
        #     dangling) but also mustn't move them.
        is_pinned = (
            ref.upper().startswith("J")
            or ref.startswith("#")
        )
        comp = Component(
            ref=ref, unit=unit, lib_id=lib_id,
            x=x, y=y, rotation=rot,
            mirror_x=mx, mirror_y=my,
            pins=dict(unit_pins),
            bbox_w=bbox_w, bbox_h=bbox_h,
            pinned=is_pinned,
        )
        sess.components[comp.key] = comp

    # Pass 2: connection graph from labels.  Walk all (label/global_label/
    # hierarchical_label) at world coords; for each, find the pin it's
    # attached to (via direct coincidence or wire-graph BFS).
    label_positions: List[Tuple[Tuple[float, float], str]] = []
    for top in sexp:
        if not (isinstance(top, list) and len(top) >= 2):
            continue
        head = top[0]
        if not isinstance(head, Symbol):
            continue
        if str(head) not in ("label", "global_label", "hierarchical_label"):
            continue
        at = _parse_at(top[2] if (len(top) >= 3 and isinstance(top[2], list) and top[2][0] == Symbol("at")) else None)
        # The (at ...) is usually right after the name string; look for it explicitly.
        for sub in top[1:]:
            if isinstance(sub, list) and sub and sub[0] == Symbol("at"):
                at = _parse_at(sub)
                break
        if at is None:
            continue
        label_positions.append(((round(at[0], 2), round(at[1], 2)), top[1]))

    # Collect wire segments.
    wires: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []
    for top in sexp:
        if not (isinstance(top, list) and top and top[0] == Symbol("wire")):
            continue
        pts = []
        for sub in top[1:]:
            if isinstance(sub, list) and sub and sub[0] == Symbol("pts"):
                for xy in sub[1:]:
                    if isinstance(xy, list) and xy[0] == Symbol("xy") and len(xy) >= 3:
                        try:
                            pts.append((round(float(xy[1]), 2), round(float(xy[2]), 2)))
                        except (TypeError, ValueError):
                            pass
        if len(pts) == 2:
            wires.append((pts[0], pts[1]))

    # World pin positions, per component (key includes unit so multi-
    # unit components don't collide on the same dict key).
    pin_world: Dict[Tuple[str, str], Tuple[float, float]] = {}
    for comp in sess.components.values():
        for pn in comp.pins:
            wp = comp.world_pin_xy(pn)
            if wp is not None:
                pin_world[(comp.key, pn)] = (round(wp[0], 2), round(wp[1], 2))

    # For each (ref, pn), figure out its net by:
    #   1) Direct: any label at that exact coord?
    #   2) BFS via wires up to depth 4: any label encountered?
    def _bfs_label(start: Tuple[float, float]) -> Optional[str]:
        # Direct label at the start point
        for (lpos, lname) in label_positions:
            if abs(lpos[0] - start[0]) < 0.5 and abs(lpos[1] - start[1]) < 0.5:
                return lname
        visited = {start}
        queue = [(start, 0)]
        while queue:
            cur, depth = queue.pop(0)
            if depth >= 5:
                continue
            for (a, b) in wires:
                for (here, there) in ((a, b), (b, a)):
                    if abs(here[0] - cur[0]) < 0.5 and abs(here[1] - cur[1]) < 0.5:
                        for (lpos, lname) in label_positions:
                            if abs(lpos[0] - there[0]) < 0.5 and abs(lpos[1] - there[1]) < 0.5:
                                return lname
                        if there not in visited:
                            visited.add(there)
                            queue.append((there, depth + 1))
        return None

    for (comp_key, pn), wp in pin_world.items():
        net_name = _bfs_label(wp)
        if not net_name:
            continue
        sess.nets.setdefault(net_name, Net(name=net_name)).pins.append((comp_key, pn))

    return sess


# ----------------------------------------------------------------------
# Force computation
# ----------------------------------------------------------------------


def _component_pair_force(c1: Component, c2: Component, k: float) -> Tuple[float, float]:
    """Repulsive Coulomb-like force on c1 due to c2.  Falls off as 1/r²."""
    dx = c1.x - c2.x
    dy = c1.y - c2.y
    dist2 = dx * dx + dy * dy
    if dist2 < 0.01:
        # Avoid singularity — give them a small nudge in a deterministic
        # direction so two co-located components separate.
        return 5.0, 5.0
    inv_dist = 1.0 / math.sqrt(dist2)
    # Force magnitude ∝ 1/r²; project onto unit direction (dx, dy)/r.
    mag = k / dist2
    return mag * dx * inv_dist, mag * dy * inv_dist


def _attractive_force(c1: Component, c2: Component, k: float) -> Tuple[float, float]:
    """Linear spring-like attraction along c1→c2 (component centres).
    Used as the fallback when pin numbers aren't available — see
    `_attractive_force_pinwise` for the version the iterator uses.
    """
    dx = c2.x - c1.x
    dy = c2.y - c1.y
    return k * dx, k * dy


def _attractive_force_pinwise(
    c1: Component, pin1: str, c2: Component, pin2: str, k: float
) -> Tuple[float, float]:
    """Attraction force pulling c1's specific pin toward c2's specific
    pin (rather than centre-to-centre).  Without this, decoupling
    capacitors on a big IC's perimeter pile on the IC's *centre* —
    every cap's centre wants to overlap U1's centre because that's the
    nearest point.  Pin-aware attraction places adjacent pins near
    each other, so the components naturally line up edge-to-edge with
    connections short and visible.

    Returns the force on c1 (b's reaction is the negation, applied
    by the caller).
    """
    p1 = c1.world_pin_xy(pin1)
    p2 = c2.world_pin_xy(pin2)
    if p1 is None or p2 is None:
        return _attractive_force(c1, c2, k)
    return k * (p2[0] - p1[0]), k * (p2[1] - p1[1])


def _boundary_force(c: Component, p: Params) -> Tuple[float, float]:
    """Linear restoring force when component is outside the sheet bbox."""
    fx = fy = 0.0
    if c.x < p.sheet_x_min:
        fx += p.boundary_k * (p.sheet_x_min - c.x)
    elif c.x > p.sheet_x_max:
        fx -= p.boundary_k * (c.x - p.sheet_x_max)
    if c.y < p.sheet_y_min:
        fy += p.boundary_k * (p.sheet_y_min - c.y)
    elif c.y > p.sheet_y_max:
        fy -= p.boundary_k * (c.y - p.sheet_y_max)
    return fx, fy


def _polarity_force(c: Component, sess: Session) -> Tuple[float, float]:
    """Bias components on GND-like nets downward, V+-like nets upward.
    Magnitude scales with how many polarity-net connections the
    component has (so a chip with 1 GND pin gets less bias than one
    with 4)."""
    p = sess.params
    cy_target = (p.sheet_y_min + p.sheet_y_max) / 2.0
    fy = 0.0
    n = 0
    for net in sess.nets.values():
        belongs = any(comp_key == c.key for comp_key, _ in net.pins)
        if not belongs:
            continue
        if p.is_bottom_polarity(net.name):
            fy += p.polarity_k * (p.sheet_y_max - c.y)
            n += 1
        elif p.is_top_polarity(net.name):
            fy += p.polarity_k * (p.sheet_y_min - c.y)
            n += 1
    return 0.0, fy


def _torque_for_pin_orientation(c: Component, sess: Session) -> float:
    """For each connection touching c, compute a torque that wants to
    align c's pin's outward direction with the vector toward the other
    endpoint.  Returns net torque (degrees of rotation).
    """
    total_torque = 0.0
    for net in sess.nets.values():
        my_pins = [pn for comp_key, pn in net.pins if comp_key == c.key]
        if not my_pins:
            continue
        # Average position of all OTHER pins on this net — that's where
        # we want the pin to point.
        other_xy = []
        for comp_key, pn in net.pins:
            if comp_key == c.key:
                continue
            other = sess.components.get(comp_key)
            if other is None:
                continue
            wp = other.world_pin_xy(pn)
            if wp is not None:
                other_xy.append(wp)
        if not other_xy:
            continue
        target_x = sum(p[0] for p in other_xy) / len(other_xy)
        target_y = sum(p[1] for p in other_xy) / len(other_xy)

        for pn in my_pins:
            outward = c.world_pin_outward_angle(pn)
            wp = c.world_pin_xy(pn)
            if outward is None or wp is None:
                continue
            # Vector from this pin to the target
            dx = target_x - wp[0]
            dy = -(target_y - wp[1])  # invert y to math convention for atan2
            if abs(dx) < 1e-6 and abs(dy) < 1e-6:
                continue
            target_angle = math.degrees(math.atan2(dy, dx)) % 360
            # Torque is the SHORTEST rotation toward target_angle.
            diff = (target_angle - outward + 540) % 360 - 180
            total_torque += diff
    return total_torque * sess.params.rotation_k * 0.01


# ----------------------------------------------------------------------
# Iteration
# ----------------------------------------------------------------------


def iterate(sess: Session, n: int = 1) -> Dict[str, Any]:
    """Run n force-directed iterations on the in-memory model."""
    p = sess.params
    max_force_seen = 0.0

    for _ in range(n):
        # Compute force on every component.
        forces: Dict[str, Tuple[float, float]] = {}
        torques: Dict[str, float] = {}

        comps = list(sess.components.values())
        for c in comps:
            fx = fy = 0.0
            # Repulsion from other components
            for other in comps:
                if other is c:
                    continue
                rfx, rfy = _component_pair_force(c, other, p.repulsion_k)
                fx += rfx
                fy += rfy
            # Boundary
            bfx, bfy = _boundary_force(c, p)
            fx += bfx
            fy += bfy
            # Polarity bias
            pfx, pfy = _polarity_force(c, sess)
            fx += pfx
            fy += pfy
            forces[c.key] = (fx, fy)
            torques[c.key] = _torque_for_pin_orientation(c, sess)

        # Attraction along each net edge — every pair of pins on the
        # same net gets a spring force.  Pin-aware: the force is
        # between the SPECIFIC pins on the net, not between component
        # centres.  This is what lets caps clip onto the EDGE of a
        # large IC (where its pins are) instead of piling on top of
        # the IC's centre.  Multi-unit components show up as distinct
        # nodes via the synthetic comp_key (ref + unit).
        for net in sess.nets.values():
            pin_list = net.pins
            if len(pin_list) < 2:
                continue
            for i, (key_a, pin_a) in enumerate(pin_list):
                for key_b, pin_b in pin_list[i + 1 :]:
                    if key_a == key_b:
                        continue
                    a = sess.components.get(key_a)
                    b = sess.components.get(key_b)
                    if a is None or b is None:
                        continue
                    afx, afy = _attractive_force_pinwise(
                        a, pin_a, b, pin_b, p.attraction_k,
                    )
                    forces[key_a] = (forces[key_a][0] + afx, forces[key_a][1] + afy)
                    forces[key_b] = (forces[key_b][0] - afx, forces[key_b][1] - afy)

        # Apply: cap displacement at temperature.
        for c in comps:
            if c.pinned:
                continue
            fx, fy = forces[c.key]
            mag = math.hypot(fx, fy)
            max_force_seen = max(max_force_seen, mag)
            if mag > 0:
                # Cap step at temperature (mm).
                step = min(mag, sess.temperature)
                c.x += fx / mag * step
                c.y += fy / mag * step
            # Torque (rotation update) — capped to small steps.
            t = torques[c.key]
            if abs(t) > 5.0:
                t = math.copysign(5.0, t)
            c.rotation = (c.rotation + t) % 360

        sess.iteration += 1
        sess.temperature = max(p.min_temperature, sess.temperature * p.cooling)

    sess.last_max_force = max_force_seen
    return {
        "iteration": sess.iteration,
        "temperature": round(sess.temperature, 4),
        "max_force": round(max_force_seen, 4),
        "n_components": len(sess.components),
        "n_nets": len(sess.nets),
    }


# ----------------------------------------------------------------------
# Snap + apply (write back to schematic)
# ----------------------------------------------------------------------


_GRID = 1.27


def snap_positions(sess: Session) -> None:
    """Snap each component's (x, y) to nearest 1.27 mm grid; rotation
    to nearest 90°.

    Then run a multi-pass nudge sweep:
      * **Bbox-overlap pass**: for every (a, b) pair, if their bboxes
        overlap, nudge the (mobile) one right.  Repeat until a full
        pass produces no movement — re-checks are needed because a
        nudge from pair (i, j) can land j on top of a previously-OK
        pair (k, j) where k < i.  Single-pass would miss this.
      * **Pin-coord safety pass**: scan every pin's world coord; if two
        components' pins coincide, nudge one component right.  This
        is the last line of defence against net merges — connect_pins
        wires same-net pins together, and if two different-net pins
        sit at the same coord the wires merge those nets.  Bbox-only
        resolution can miss this: two components with their bboxes
        clearing could still have one of each's pins overlapping
        because pins are offset from the centre.
    """
    for c in sess.components.values():
        c.x = round(c.x / _GRID) * _GRID
        c.y = round(c.y / _GRID) * _GRID
        c.rotation = round(c.rotation / 90) * 90 % 360

    keys = sorted(sess.components.keys())
    MAX_PASSES = 8

    def _pick_target(a: "Component", b: "Component") -> Optional["Component"]:
        if a.pinned and b.pinned:
            return None
        return b if not b.pinned else a

    # Bbox-overlap pass — repeat until stable.
    for _ in range(MAX_PASSES):
        moved = False
        for i, ki in enumerate(keys):
            for kj in keys[i + 1 :]:
                a = sess.components[ki]
                b = sess.components[kj]
                if a.ref == b.ref:
                    continue  # same-ref multi-unit — exempt
                target = _pick_target(a, b)
                if target is None:
                    continue
                min_dx = (a.bbox_w + b.bbox_w) / 2 + _GRID
                min_dy = (a.bbox_h + b.bbox_h) / 2 + _GRID
                tries = 0
                while (
                    abs(a.x - b.x) < min_dx
                    and abs(a.y - b.y) < min_dy
                    and tries < 30
                ):
                    target.x += _GRID * 4  # nudge right
                    tries += 1
                if tries > 0:
                    moved = True
        if not moved:
            break

    # Pin-coord-collision safety pass — same multi-pass shape so a
    # nudge that creates a *new* coincident pair gets resolved next.
    def _pin_world_iu(c: "Component", pn: str) -> Optional[Tuple[int, int]]:
        wp = c.world_pin_xy(pn)
        if wp is None:
            return None
        return (round(wp[0] * 1000), round(wp[1] * 1000))

    for _ in range(MAX_PASSES):
        moved = False
        # Group pin-world-coords by (x_um, y_um).
        pin_coords: Dict[Tuple[int, int], List[Tuple[str, str]]] = {}
        for c in sess.components.values():
            for pn in c.pins:
                key_iu = _pin_world_iu(c, pn)
                if key_iu is None:
                    continue
                pin_coords.setdefault(key_iu, []).append((c.key, pn))
        for occupants in pin_coords.values():
            unique_refs = {sess.components[k].ref for k, _ in occupants}
            if len(unique_refs) <= 1:
                # Either single occupant, or multiple pins of the SAME
                # symbol unit (e.g. duplicate-pad pins on FDS9926A) —
                # no net-merge risk.
                continue
            # Multiple distinct refs at the same pin coord.  Nudge
            # one of them — pick the alphabetically-last non-pinned
            # ref so the deterministic order matches the bbox pass.
            non_pinned = [
                k for k, _ in occupants if not sess.components[k].pinned
            ]
            if not non_pinned:
                continue
            target_key = sorted(non_pinned)[-1]
            sess.components[target_key].x += _GRID * 4
            moved = True
        if not moved:
            break


_STRIPPED_TYPES = {"wire", "label", "global_label", "hierarchical_label", "junction", "no_connect"}


def apply_to_schematic(sess: Session, target_path: Optional[Path] = None,
                       strip_connections: bool = True,
                       standalone: Optional[bool] = None) -> Dict[str, Any]:
    """Write component positions back to a schematic file.  If
    target_path is None, overwrite sess.schematic_path.

    With ``strip_connections=True`` (default) all wires, labels,
    junctions, and no_connect markers are removed — pins move with the
    components, so previously-laid wiring would be dangling.  The
    caller is expected to re-route via ``rewire_session`` (or
    connect_pins(style='auto') manually) afterward.

    With ``strip_connections=False`` only positions are updated.  Used
    for "preview" renders where seeing the orphaned labels next to
    moved components is informative for tuning.
    """
    src = sess.schematic_path
    dst = Path(target_path) if target_path else src
    text = src.read_text()
    sexp = sexpdata.loads(text)

    # Replace each placed-symbol's (at) AND each property's (at) so
    # the Reference / Value text labels follow the symbol body —
    # including the per-property offset rotating around the symbol
    # anchor when the symbol's rotation changes.  Match by (ref, unit)
    # so multi-unit symbols update each placed instance to its own
    # model position.
    n_updated = 0
    for top in sexp:
        if not (isinstance(top, list) and len(top) > 1 and top[0] == Symbol("symbol")):
            continue
        ref = None
        unit = 1
        old_x = old_y = old_rot = None
        for sub in top[1:]:
            if isinstance(sub, list) and sub and sub[0] == Symbol("at"):
                at = _parse_at(sub)
                if at:
                    old_x, old_y, old_rot = at
            elif isinstance(sub, list) and sub and sub[0] == Symbol("unit") and len(sub) >= 2:
                try:
                    unit = int(sub[1])
                except (TypeError, ValueError):
                    pass
            elif isinstance(sub, list) and len(sub) >= 3 and sub[0] == Symbol("property"):
                if sub[1] == "Reference":
                    ref = sub[2]
        if ref is None or old_x is None:
            continue
        comp_key = f"{ref}__u{unit}"
        comp = sess.components.get(comp_key)
        if comp is None:
            continue
        new_x, new_y, new_rot = comp.x, comp.y, comp.rotation
        delta_rot = ((new_rot - old_rot + 540) % 360) - 180  # signed shortest
        rad = math.radians(delta_rot)
        cos_d = math.cos(rad)
        sin_d = math.sin(rad)
        # Update the symbol's primary (at).
        for sub in top[1:]:
            if isinstance(sub, list) and sub and sub[0] == Symbol("at"):
                sub[1] = new_x
                sub[2] = new_y
                if len(sub) >= 4:
                    sub[3] = new_rot
                else:
                    sub.append(new_rot)
                break
        # For every property's (at), rotate its offset around the OLD
        # symbol anchor by delta_rot, then translate to the NEW anchor,
        # AND rotate the property's own text orientation by delta_rot.
        # Screen coords are Y-down so KiCad's CCW rotation maps to the
        # math-CW formula:
        #     new_dx = old_dx*cos(Δ) + old_dy*sin(Δ)
        #     new_dy = -old_dx*sin(Δ) + old_dy*cos(Δ)
        for sub in top[1:]:
            if not (isinstance(sub, list) and len(sub) >= 3 and sub[0] == Symbol("property")):
                continue
            for sp in sub[2:]:
                if isinstance(sp, list) and sp and sp[0] == Symbol("at") and len(sp) >= 3:
                    try:
                        old_px, old_py = float(sp[1]), float(sp[2])
                        ox = old_px - old_x
                        oy = old_py - old_y
                        # Rotate the offset around the OLD symbol
                        # anchor so the text orbits the body.  Don't
                        # touch the property's own (at angle) field
                        # — KiCad already rotates property text along
                        # with the symbol's rotation, and adding
                        # delta_rot here would double-rotate.
                        nox = ox * cos_d + oy * sin_d
                        noy = -ox * sin_d + oy * cos_d
                        sp[1] = new_x + nox
                        sp[2] = new_y + noy
                    except (TypeError, ValueError):
                        pass
                    break
        n_updated += 1

    n_stripped = 0
    if strip_connections:
        new_sexp = []
        for item in sexp:
            if (
                isinstance(item, list) and item
                and isinstance(item[0], Symbol)
                and str(item[0]) in _STRIPPED_TYPES
            ):
                n_stripped += 1
                continue
            new_sexp.append(item)
        sexp = new_sexp

    # When ``standalone`` is True, rewrite each placed-symbol's
    # (instances (project … (path …))) to point to the destination's
    # stem and root UUID — so KiCad can resolve annotations when the
    # file is opened on its own (no parent .kicad_pro hierarchy).
    # Default behaviour: auto-detect — standalone if writing to a
    # different file from the source, OR if the source itself isn't
    # the file referenced by its (instances …) blocks.  Pass
    # standalone=False explicitly to preserve hierarchical paths.
    # Default: True.  Most placer uses are tests / iterations on a
    # copy of the file, where opening the result standalone is the
    # natural way to inspect it.  Hierarchical-preserve mode (the
    # original child-sheet uses its parent's path) is opt-in via
    # standalone=False — typically used when applying placement
    # back to a real project's child sheet without breaking
    # the parent's references.
    use_standalone: bool
    if standalone is None:
        use_standalone = True
    else:
        use_standalone = standalone
    if use_standalone:
        # Need the destination's root uuid — find the first (uuid …) in
        # the (about-to-be-written) sexp.
        dst_uuid = None
        for item in sexp:
            if isinstance(item, list) and len(item) >= 2 and item[0] == Symbol("uuid"):
                dst_uuid = str(item[1]) if not isinstance(item[1], Symbol) else str(item[1])
                break
        dst_stem = dst.stem
        if dst_uuid:
            for top in sexp:
                if not (isinstance(top, list) and len(top) > 1 and top[0] == Symbol("symbol")):
                    continue
                for sub in top[1:]:
                    if not (isinstance(sub, list) and sub and sub[0] == Symbol("instances")):
                        continue
                    for proj in sub[1:]:
                        if not (isinstance(proj, list) and proj and proj[0] == Symbol("project")):
                            continue
                        # (project "name" (path "…" …))
                        if len(proj) >= 2:
                            proj[1] = dst_stem
                        for pp in proj[2:]:
                            if isinstance(pp, list) and pp and pp[0] == Symbol("path") and len(pp) >= 2:
                                pp[1] = f"/{dst_uuid}"

    dst.write_text(sexpdata.dumps(sexp))
    return {
        "wrote": str(dst),
        "n_components_updated": n_updated,
        "n_connections_stripped": n_stripped,
        "strip_connections": strip_connections,
    }


def rewire_session(sess: Session, schematic_path: Path) -> Dict[str, Any]:
    """Re-route each net through ``connect_pins(style="auto")``: the
    autorouter draws real wires between same-net pins where it can
    fit (≤ max_len, ≤ max_bends, no obstacle crossings) and falls
    back to label-with-stub on each pin otherwise.

    Per-net call: every pin participates in one ``connect_pins`` call
    so the autorouter sees the whole net at once and can chain pins
    end-to-end.  Multi-unit pins (e.g. Q1/3 on a dual-FET) resolve via
    PinLocator's per-unit lookup — the model's per-unit components
    are written by ``apply_to_schematic`` before this is called, so
    PinLocator finds each unit's instance at its correct ``(at)``.

    Returns aggregated counters across all per-net calls.
    """
    from commands.connection_schematic import ConnectionManager

    pins_per_net: Dict[str, List[Dict[str, str]]] = {}
    skipped = 0
    for name, net in sess.nets.items():
        for comp_key, pn in net.pins:
            comp = sess.components.get(comp_key)
            if comp is None:
                skipped += 1
                continue
            pins_per_net.setdefault(name, []).append({"ref": comp.ref, "pin": pn})

    nets_rewired = 0
    pairs_wired = 0
    pairs_failed = 0
    pins_connected = 0
    per_net: List[Dict[str, Any]] = []
    for name, pin_dicts in pins_per_net.items():
        if not pin_dicts:
            continue
        # connect_pins requires at least 2 pins to do anything; for a
        # 1-pin "net" (e.g. an isolated label) just add a label so the
        # name doesn't get lost when the schematic is reloaded.
        if len(pin_dicts) < 2:
            result = ConnectionManager.connect_pins(
                schematic_path, pin_dicts, net_name=name, style="label",
            )
        else:
            result = ConnectionManager.connect_pins(
                schematic_path, pin_dicts, net_name=name, style="auto",
            )
        nets_rewired += 1
        pairs_wired += len(result.get("wired_pairs", []) or [])
        pairs_failed += len(result.get("routing_failures", []) or [])
        pins_connected += len(result.get("connected", []) or [])
        per_net.append({
            "net": name,
            "n_pins": len(pin_dicts),
            "connected": len(result.get("connected", []) or []),
            "wired_pairs": len(result.get("wired_pairs", []) or []),
            "failed_pairs": len(result.get("routing_failures", []) or []),
            "success": result.get("success", False),
        })
    return {
        "nets_rewired": nets_rewired,
        "pairs_wired": pairs_wired,
        "pairs_failed": pairs_failed,
        "pins_connected": pins_connected,
        "pins_skipped": skipped,
        "method": "connect_pins(auto)",
        "per_net": per_net,
    }


# ----------------------------------------------------------------------
# Top-level singleton
# ----------------------------------------------------------------------


class AutoPlacer:
    """Holds named sessions across MCP tool calls."""

    def __init__(self) -> None:
        self.sessions: Dict[str, Session] = {}

    def load(self, schematic_path: str) -> Dict[str, Any]:
        sess = load_session(Path(schematic_path))
        sess.temperature = sess.params.initial_temperature
        self.sessions[str(Path(schematic_path).resolve())] = sess
        return {
            "schematic_path": str(schematic_path),
            "n_components": len(sess.components),
            "n_nets": len(sess.nets),
            "params": _params_dict(sess.params),
        }

    def get(self, schematic_path: str) -> Optional[Session]:
        return self.sessions.get(str(Path(schematic_path).resolve()))

    def set_params(self, schematic_path: str, **kwargs: Any) -> Dict[str, Any]:
        sess = self.get(schematic_path)
        if sess is None:
            return {"success": False, "message": "session not loaded"}
        for k, v in kwargs.items():
            if hasattr(sess.params, k):
                setattr(sess.params, k, v)
        return {"success": True, "params": _params_dict(sess.params)}

    def iterate(self, schematic_path: str, n: int = 1) -> Dict[str, Any]:
        sess = self.get(schematic_path)
        if sess is None:
            return {"success": False, "message": "session not loaded"}
        return {"success": True, **iterate(sess, n)}

    def state(self, schematic_path: str) -> Dict[str, Any]:
        sess = self.get(schematic_path)
        if sess is None:
            return {"success": False, "message": "session not loaded"}
        return {
            "success": True,
            "iteration": sess.iteration,
            "temperature": round(sess.temperature, 4),
            "max_force": round(sess.last_max_force, 4),
            "components": {
                key: {
                    "ref": c.ref, "unit": c.unit,
                    "x": round(c.x, 3), "y": round(c.y, 3),
                    "rotation": round(c.rotation, 1),
                    "pinned": c.pinned,
                }
                for key, c in sess.components.items()
            },
        }

    def preview(self, schematic_path: str, target_path: str,
                strip_connections: bool = False) -> Dict[str, Any]:
        sess = self.get(schematic_path)
        if sess is None:
            return {"success": False, "message": "session not loaded"}
        # Don't snap on preview — show the raw physics state.
        return {
            "success": True,
            **apply_to_schematic(sess, Path(target_path),
                                 strip_connections=strip_connections),
        }

    def apply(self, schematic_path: str, rewire: bool = True,
              standalone: Optional[bool] = None) -> Dict[str, Any]:
        sess = self.get(schematic_path)
        if sess is None:
            return {"success": False, "message": "session not loaded"}
        snap_positions(sess)
        result = apply_to_schematic(
            sess, None, strip_connections=True, standalone=standalone,
        )
        if rewire:
            result["rewire"] = rewire_session(sess, sess.schematic_path)
        return {"success": True, **result}


def _params_dict(p: Params) -> Dict[str, Any]:
    return {
        "repulsion_k": p.repulsion_k,
        "attraction_k": p.attraction_k,
        "boundary_k": p.boundary_k,
        "polarity_k": p.polarity_k,
        "rotation_k": p.rotation_k,
        "initial_temperature": p.initial_temperature,
        "cooling": p.cooling,
        "min_temperature": p.min_temperature,
        "sheet_x_min": p.sheet_x_min,
        "sheet_y_min": p.sheet_y_min,
        "sheet_x_max": p.sheet_x_max,
        "sheet_y_max": p.sheet_y_max,
        "bottom_polarity_nets": list(p.bottom_polarity_nets),
        "top_polarity_nets": list(p.top_polarity_nets),
    }


# Module-level singleton used by MCP handlers.
PLACER = AutoPlacer()
