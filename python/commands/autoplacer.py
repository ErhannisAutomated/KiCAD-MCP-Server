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
    lib_id: str
    x: float             # mm; placed-symbol position (Y-down screen coords)
    y: float
    rotation: float      # degrees, free during iteration; snapped at apply
    mirror_x: bool
    mirror_y: bool
    pins: Dict[str, Pin] = field(default_factory=dict)
    bbox_w: float = 7.62  # default ~3 grid units; refined later if needed
    bbox_h: float = 7.62
    pinned: bool = False  # if True, position locked

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


def _extract_lib_pins(sexp_data, lib_id: str) -> Dict[str, Pin]:
    """Find the symbol's pin definitions in lib_symbols and return them
    as a dict of pin_number -> Pin (lib coords)."""
    for top in sexp_data:
        if not (isinstance(top, list) and top and top[0] == Symbol("lib_symbols")):
            continue
        for sym in top[1:]:
            if not (isinstance(sym, list) and len(sym) > 1 and sym[0] == Symbol("symbol")):
                continue
            if sym[1] != lib_id:
                continue
            return _walk_pins(sym)
    return {}


def _walk_pins(sym_node) -> Dict[str, Pin]:
    pins: Dict[str, Pin] = {}

    def visit(node):
        if not isinstance(node, list) or not node:
            return
        if node[0] == Symbol("pin") and len(node) >= 3:
            x = y = ang = 0.0
            name = num = ""
            for sub in node[2:]:
                if not isinstance(sub, list) or not sub:
                    continue
                if sub[0] == Symbol("at") and len(sub) >= 3:
                    try:
                        x = float(sub[1])
                        y = float(sub[2])
                        if len(sub) >= 4:
                            ang = float(sub[3])
                    except (TypeError, ValueError):
                        pass
                elif sub[0] == Symbol("name") and len(sub) >= 2:
                    name = str(sub[1]).strip('"')
                elif sub[0] == Symbol("number") and len(sub) >= 2:
                    num = str(sub[1]).strip('"')
            if num:
                pins[num] = Pin(num, name, x, y, ang)
            return
        for sub in node[1:] if isinstance(node, list) else []:
            visit(sub)

    visit(sym_node)
    return pins


def load_session(schematic_path: Path) -> Session:
    """Build an in-memory placement session from a .kicad_sch."""
    text = Path(schematic_path).read_text()
    sexp = sexpdata.loads(text)

    sess = Session(schematic_path=Path(schematic_path))

    # Pre-pass: count placed instances per reference.  Multi-unit
    # symbols (FDS9926A, op-amp packages with units A/B/C, etc.) appear
    # as multiple (symbol …) blocks sharing one ref.  The placer's
    # model is one node per ref, so we mark these as pinned — moving
    # one instance independently of the other would split the on-PCB
    # footprint.
    instance_count: Dict[str, int] = {}
    for top in sexp:
        if not (isinstance(top, list) and len(top) > 1 and top[0] == Symbol("symbol")):
            continue
        for sub in top[1:]:
            if (
                isinstance(sub, list) and len(sub) >= 3
                and sub[0] == Symbol("property") and sub[1] == "Reference"
            ):
                ref_str = sub[2]
                instance_count[ref_str] = instance_count.get(ref_str, 0) + 1
                break

    # Pass 1: components
    for top in sexp:
        if not (isinstance(top, list) and len(top) > 1 and top[0] == Symbol("symbol")):
            continue
        # placed symbol — has lib_id, at, instances
        lib_id = None
        x = y = 0.0
        rot = 0.0
        mx = my = False
        ref = None
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
            elif sub[0] == Symbol("property") and len(sub) >= 3:
                if sub[1] == "Reference":
                    ref = sub[2]
        if not (lib_id and ref):
            continue
        # Skip power flag pseudosymbols and template entries.
        if ref.startswith("#"):
            continue
        if ref in sess.components:
            # Already loaded (multi-unit component).  Skip duplicates;
            # the first-seen placement keeps its position and is
            # marked pinned below.
            continue
        # Pinned heuristics:
        #   - Multi-unit symbols: moving one unit drags the other onto it.
        #   - Connectors (J*): user-facing edges, hand-placed.
        is_pinned = (
            instance_count.get(ref, 0) > 1
            or ref.upper().startswith("J")
        )
        pins = _extract_lib_pins(sexp, lib_id)
        sess.components[ref] = Component(
            ref=ref, lib_id=lib_id,
            x=x, y=y, rotation=rot,
            mirror_x=mx, mirror_y=my,
            pins=pins,
            pinned=is_pinned,
        )

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

    # World pin positions, per component.
    pin_world: Dict[Tuple[str, str], Tuple[float, float]] = {}
    for comp in sess.components.values():
        for pn in comp.pins:
            wp = comp.world_pin_xy(pn)
            if wp is not None:
                pin_world[(comp.ref, pn)] = (round(wp[0], 2), round(wp[1], 2))

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

    for (ref, pn), wp in pin_world.items():
        net_name = _bfs_label(wp)
        if not net_name:
            continue
        sess.nets.setdefault(net_name, Net(name=net_name)).pins.append((ref, pn))

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
    """Linear spring-like attraction along c1→c2."""
    dx = c2.x - c1.x
    dy = c2.y - c1.y
    return k * dx, k * dy


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
        belongs = any(ref == c.ref for ref, _ in net.pins)
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
        my_pins = [pn for ref, pn in net.pins if ref == c.ref]
        if not my_pins:
            continue
        # Average position of all OTHER pins on this net — that's where
        # we want the pin to point.
        other_xy = []
        for ref, pn in net.pins:
            if ref == c.ref:
                continue
            other = sess.components.get(ref)
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
            forces[c.ref] = (fx, fy)
            torques[c.ref] = _torque_for_pin_orientation(c, sess)

        # Attraction along each net edge — every pair of pins on the
        # same net gets a spring force.
        for net in sess.nets.values():
            pin_list = net.pins
            if len(pin_list) < 2:
                continue
            for i, (ref_a, _) in enumerate(pin_list):
                for ref_b, _ in pin_list[i + 1 :]:
                    if ref_a == ref_b:
                        continue
                    a = sess.components.get(ref_a)
                    b = sess.components.get(ref_b)
                    if a is None or b is None:
                        continue
                    afx, afy = _attractive_force(a, b, p.attraction_k)
                    forces[ref_a] = (forces[ref_a][0] + afx, forces[ref_a][1] + afy)
                    forces[ref_b] = (forces[ref_b][0] - afx, forces[ref_b][1] - afy)

        # Apply: cap displacement at temperature.
        for c in comps:
            if c.pinned:
                continue
            fx, fy = forces[c.ref]
            mag = math.hypot(fx, fy)
            max_force_seen = max(max_force_seen, mag)
            if mag > 0:
                # Cap step at temperature (mm).
                step = min(mag, sess.temperature)
                c.x += fx / mag * step
                c.y += fy / mag * step
            # Torque (rotation update) — capped to small steps.
            t = torques[c.ref]
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
    to nearest 90°.  Resolves overlaps by spreading along x."""
    for c in sess.components.values():
        c.x = round(c.x / _GRID) * _GRID
        c.y = round(c.y / _GRID) * _GRID
        c.rotation = round(c.rotation / 90) * 90 % 360

    # Simple overlap resolution: scan in deterministic order; if a
    # component is within 5 mm of another, push the LATER one one grid step.
    refs = sorted(sess.components.keys())
    for i, ref_i in enumerate(refs):
        for ref_j in refs[i + 1 :]:
            a = sess.components[ref_i]
            b = sess.components[ref_j]
            tries = 0
            while abs(a.x - b.x) < 5.0 and abs(a.y - b.y) < 5.0 and tries < 20:
                b.x += _GRID * 4  # nudge right
                tries += 1


_STRIPPED_TYPES = {"wire", "label", "global_label", "hierarchical_label", "junction", "no_connect"}


def apply_to_schematic(sess: Session, target_path: Optional[Path] = None,
                       strip_connections: bool = True) -> Dict[str, Any]:
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

    # Replace each placed-symbol's (at).  The placer's model preserves
    # mirror flags from load, so we don't touch (mirror).  For
    # multi-unit symbols (multiple placed blocks sharing a ref), the
    # placer holds ONE position per ref so we only update the FIRST
    # placed instance.  Pinned components retain their original
    # position (placer didn't move them) so writing the same value
    # back is harmless.
    by_ref: Dict[str, Component] = sess.components
    seen_refs: set = set()
    n_updated = 0
    for top in sexp:
        if not (isinstance(top, list) and len(top) > 1 and top[0] == Symbol("symbol")):
            continue
        ref = None
        for sub in top[1:]:
            if isinstance(sub, list) and len(sub) >= 3 and sub[0] == Symbol("property"):
                if sub[1] == "Reference":
                    ref = sub[2]
                    break
        if ref is None or ref not in by_ref:
            continue
        if ref in seen_refs:
            # Multi-unit second instance — leave it alone so the unit-2
            # placement isn't dragged onto unit-1's position.
            continue
        seen_refs.add(ref)
        comp = by_ref[ref]
        for sub in top[1:]:
            if isinstance(sub, list) and sub and sub[0] == Symbol("at"):
                sub[1] = comp.x
                sub[2] = comp.y
                if len(sub) >= 4:
                    sub[3] = comp.rotation
                else:
                    sub.append(comp.rotation)
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

    dst.write_text(sexpdata.dumps(sexp))
    return {
        "wrote": str(dst),
        "n_components_updated": n_updated,
        "n_connections_stripped": n_stripped,
        "strip_connections": strip_connections,
    }


def rewire_session(sess: Session, schematic_path: Path) -> Dict[str, Any]:
    """For each net with ≥2 pins, call connect_pins(style="auto") on the
    just-written schematic so wires + labels are laid out in the new
    positions.  Returns a summary of how each net resolved.
    """
    from commands.connection_schematic import ConnectionManager

    results: Dict[str, Dict[str, Any]] = {}
    for name, net in sess.nets.items():
        if len(net.pins) < 2:
            # Single-pin net — drop a label at the pin so it's named.
            if len(net.pins) == 1:
                from commands.wire_manager import WireManager
                ref, pn = net.pins[0]
                comp = sess.components.get(ref)
                if comp:
                    wp = comp.world_pin_xy(pn)
                    if wp:
                        WireManager.add_label(schematic_path, name, list(wp))
            continue
        pins_arg = [{"ref": r, "pin": p} for r, p in net.pins]
        try:
            res = ConnectionManager.connect_pins(
                schematic_path, pins_arg, net_name=name, style="auto"
            )
            results[name] = {
                "success": res.get("success", False),
                "wired_pairs": len(res.get("wired_pairs", [])),
                "labels": len(res.get("auto_label_positions", [])),
                "failed": len(res.get("failed", [])),
            }
        except Exception as e:
            results[name] = {"success": False, "error": str(e)}
    return {"nets_rewired": len(results), "details": results}


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
            "components": {
                ref: {"x": round(c.x, 3), "y": round(c.y, 3), "rotation": round(c.rotation, 1)}
                for ref, c in sess.components.items()
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

    def apply(self, schematic_path: str, rewire: bool = True) -> Dict[str, Any]:
        sess = self.get(schematic_path)
        if sess is None:
            return {"success": False, "message": "session not loaded"}
        snap_positions(sess)
        result = apply_to_schematic(sess, None, strip_connections=True)
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
