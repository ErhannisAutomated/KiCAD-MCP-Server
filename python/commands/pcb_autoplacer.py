"""PCB force-directed autoplacer (relax_placement).

v2 (2026-05-20): unified with the schematic autoplacer.  Loads the
pcbnew board into the engine's ``Session`` data model and runs the
shared ``iterate()`` loop with PCB-specific physics flags
(``use_obb_repulsion``, ``use_spring_classes``, ``rotation_snap_*``).

Key differences from v1 (now removed):
  - Pin-wise springs instead of component-centre springs.  Off-centre
    forces produce torques that orient parts so the active pad faces
    its target — caps land EDGE-to-IC, not centre-to-IC.
  - OBB-via-SAT cubic-ramp body repulsion replaces the AABB depth
    heuristic.  Handles rotated parts and corner-corner cases.
  - Spring classes (DECOUPLING / LOCAL_SIGNAL / INTER_GROUP / PLANE)
    set per-pair pull strength.  Power-plane nets default to PLANE
    (k=0) since vias handle their routing.
  - 4-phase springs-first schedule: cluster → spread → snap → settle.

This module is the PCB-side adapter; the engine + physics live in
``autoplacer.py``.  Anything that's adapter-agnostic (forces, torques,
spring resolution, OBB math) belongs there.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import pcbnew

from commands.autoplacer import (
    Component, Net, Pin, Session,
    DEFAULT_SPRING_CLASSES,
    iterate,
)

logger = logging.getLogger("kicad_interface")


# ---------------------------------------------------------------------------
# Pad/footprint geometry helpers
# ---------------------------------------------------------------------------


_NM_PER_MM = 1_000_000.0
_DEFAULT_ANCHOR_PREFIXES = ("J", "SW", "BAT")


def _safe_get_property(fp: Any, name: str) -> Optional[str]:
    """Read a footprint property defensively.  pcbnew may return "" or
    raise depending on version / property absence; normalize both to
    None.  Stripped of leading/trailing whitespace."""
    try:
        val = fp.GetProperty(name)
    except Exception:
        return None
    if not isinstance(val, str):
        return None
    val = val.strip()
    return val if val else None


def _parse_pin_spring_class(value: str) -> Optional[Union[str, Dict[str, str]]]:
    """Parse a ``Pin_Spring_Class:N`` property value into the format
    expected by ``Component.pin_classes`` and ``resolve_pair_class``.

    Two accepted forms (per the spring-class storage design):
      - Bare string ``"DECOUPLING"`` → pad-general assignment that
        applies to every connection from this pad.
      - JSON object ``{"*": "SIGNAL", "U1.4": "DECOUPLING"}`` → per-
        target overrides; ``"*"`` is the pad-general fallback,
        ``"REF.PIN"`` keys apply only to that specific neighbor pin.

    Malformed JSON or non-string contents → logged and dropped (the
    property is just ignored, the run continues with defaults).
    """
    value = value.strip()
    if not value:
        return None
    # JSON-like leading char → parse as JSON; anything else is bare string.
    if value[0] in "{[":
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(
                "Pin_Spring_Class: invalid JSON value %r (%s); ignoring", value, e,
            )
            return None
        if not isinstance(parsed, dict):
            logger.warning("Pin_Spring_Class: JSON value isn't an object: %r", value)
            return None
        if not all(isinstance(v, str) for v in parsed.values()):
            logger.warning(
                "Pin_Spring_Class: JSON values must all be strings: %r", value,
            )
            return None
        return parsed
    # Bare string — let resolve_pair_class log if the name's unknown.
    return value


def _pad_local_xy(pad: Any, fp_world_xy: Tuple[float, float],
                  fp_angle_deg: float) -> Tuple[float, float]:
    """Invert the footprint's world transform to get pad LOCAL coords
    (in the footprint's unrotated frame, screen Y-down convention).

    The engine's ``Component.world_pin_xy()`` reconstructs world coords
    via ``R(-fp_angle) @ (local_x, local_y) + (fp.x, fp.y)``, so
    inverting that gives ``local = R(+fp_angle) @ (pad - fp_anchor)``.
    """
    pad_world = pad.GetPosition()
    ox = pad_world.x / _NM_PER_MM - fp_world_xy[0]
    oy = pad_world.y / _NM_PER_MM - fp_world_xy[1]
    rad = math.radians(fp_angle_deg)
    lx = ox * math.cos(rad) - oy * math.sin(rad)
    ly = ox * math.sin(rad) + oy * math.cos(rad)
    return lx, ly


def _footprint_local_bbox_mm(
    fp: Any, fp_x_mm: float, fp_y_mm: float, fp_angle_deg: float,
) -> Tuple[float, float, float, float]:
    """Footprint bbox in the UNROTATED local frame, mm.

    Returns ``(w, h, cx_offset, cy_offset)`` where ``(cx_offset,
    cy_offset)`` is the offset from the footprint origin (=
    ``fp.GetPosition()``, where pin 1 typically sits for connectors)
    to the bbox center.  Callers use ``Component.obb_center_world()``
    to rotate the offset and add it to the world origin.

    Source preference:
      1. Courtyard polygon (F.CrtYd / B.CrtYd) — KiCad's explicit
         no-overlap zone, ideal for placement clearance.
      2. Convex bounds of pads + graphics on F.Fab / B.Fab and
         F.SilkS / B.SilkS, excluding text (reference/value labels
         can extend far past the body).

    Bbox is rotation-INVARIANT in local (derotated) coords; the
    engine + viz apply ``c.rotation`` to the OBB themselves.  Using
    ``fp.GetBoundingBox()`` here was wrong because it returned the
    world AABB at the current rotation.
    """
    rad = math.radians(fp_angle_deg)
    cos_a, sin_a = math.cos(rad), math.sin(rad)

    def _to_local(wx_nm: float, wy_nm: float) -> Tuple[float, float]:
        ox = wx_nm / _NM_PER_MM - fp_x_mm
        oy = wy_nm / _NM_PER_MM - fp_y_mm
        return (ox * cos_a - oy * sin_a, ox * sin_a + oy * cos_a)

    xs: List[float] = []
    ys: List[float] = []

    # ---- 1. Courtyard (preferred) ----
    try:
        for layer in (pcbnew.F_CrtYd, pcbnew.B_CrtYd):
            poly = fp.GetCourtyard(layer)
            if poly is None or poly.OutlineCount() == 0:
                continue
            for oi in range(poly.OutlineCount()):
                outline = poly.Outline(oi)
                for pi in range(outline.PointCount()):
                    pt = outline.CPoint(pi)
                    lx, ly = _to_local(pt.x, pt.y)
                    xs.append(lx)
                    ys.append(ly)
    except Exception:
        # Older pcbnew versions or missing API — fall through to fab/silk.
        pass

    # ---- 2. Fall back to pads + fab + silk graphics ----
    if not xs:
        for pad in fp.Pads():
            pw = pad.GetPosition()
            lx, ly = _to_local(pw.x, pw.y)
            size = pad.GetSize()
            r = max(size.x, size.y) / (2 * _NM_PER_MM)
            xs.extend([lx - r, lx + r])
            ys.extend([ly - r, ly + r])
        body_layers = {
            pcbnew.F_Fab, pcbnew.B_Fab, pcbnew.F_SilkS, pcbnew.B_SilkS,
        }
        for item in fp.GraphicalItems():
            # Skip text (reference/value labels can extend well past body).
            try:
                if item.Type() == pcbnew.PCB_FIELD_T or item.Type() == pcbnew.PCB_TEXT_T:
                    continue
            except Exception:
                # Type discrimination not available — best-effort skip
                # by checking for a GetText method.
                if hasattr(item, "GetText"):
                    continue
            if item.GetLayer() not in body_layers:
                continue
            bb = item.GetBoundingBox()
            # bb is in world coords (pcbnew BOX2I); convert all four corners
            # to local — for non-axis-aligned items the AABB is conservative
            # but accurate enough for body extent.
            for (wx, wy) in (
                (bb.GetLeft(),  bb.GetTop()),
                (bb.GetRight(), bb.GetTop()),
                (bb.GetRight(), bb.GetBottom()),
                (bb.GetLeft(),  bb.GetBottom()),
            ):
                lx, ly = _to_local(wx, wy)
                xs.append(lx)
                ys.append(ly)

    if not xs:
        return 1.0, 1.0, 0.0, 0.0
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    return (
        x_max - x_min,
        y_max - y_min,
        (x_min + x_max) / 2.0,
        (y_min + y_max) / 2.0,
    )


def _power_net_pattern_class(net_name: str) -> Optional[str]:
    """Auto-classify a net as PLANE if it looks like a power/ground rail.
    Conservative — only the obvious cases.  Match the schematic router's
    is_power_net() for consistency."""
    if not net_name:
        return None
    upper = net_name.upper()
    # Common ground/power names
    GROUND = ("GND", "GROUND", "AGND", "DGND", "PGND", "VSS", "VEE", "EARTH")
    POWER = ("VCC", "VDD", "VBAT", "BAT+", "+5V", "+3V3", "+3.3V", "+12V",
             "V12", "VBUS", "V+", "VS", "VPP")
    if upper in GROUND or upper in POWER:
        return "PLANE"
    # Pattern: starts with "+" then a digit (e.g. "+5V0", "+3V3")
    if upper.startswith("+") and len(upper) > 1 and upper[1].isdigit():
        return "PLANE"
    # Pattern: BAT followed by sign (BAT+, BAT-)
    if upper.startswith("BAT") and len(upper) > 3 and upper[3] in ("+", "-"):
        return "PLANE"
    return None


# ---------------------------------------------------------------------------
# Adapter: pcbnew → Session
# ---------------------------------------------------------------------------


def load_pcb_session(
    board: Any,
    *,
    locked_refs: Optional[Set[str]] = None,
    auto_classify_planes: bool = True,
) -> Session:
    """Build a placement Session from a live pcbnew Board.

    Pins are loaded with PCB pad-local coords (screen Y-down, the
    coord_system="pcb" branch of world_pin_xy()).  Components are
    anchored if their ref is in ``locked_refs`` (caller-supplied),
    or if no explicit set was passed and the footprint matches the
    default heuristic (J*/SW*/BAT* refs, or through-hole-dominant).
    """
    sess = Session(
        schematic_path=Path("pcb://" + (board.GetFileName() or "<unsaved>")),
        spring_classes=dict(DEFAULT_SPRING_CLASSES),
    )

    # ---- Components + pins ----
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        pos = fp.GetPosition()
        fp_x = pos.x / _NM_PER_MM
        fp_y = pos.y / _NM_PER_MM
        fp_angle = fp.GetOrientation().AsDegrees()
        bbox_w, bbox_h, bbox_cx, bbox_cy = _footprint_local_bbox_mm(
            fp, fp_x, fp_y, fp_angle,
        )
        layer_name = board.GetLayerName(fp.GetLayer())

        # Anchor decision: explicit set wins; otherwise honor KiCad's
        # own lock state on the footprint, then fall back to the
        # ref-prefix / through-hole-dominant heuristic.  This lets a
        # user right-click → Lock a footprint in KiCad (e.g. an IC
        # over thermal vias) and have the placer respect it without
        # passing lockedRefs explicitly.
        if locked_refs is not None:
            anchored = ref in locked_refs
        elif fp.IsLocked():
            anchored = True
        else:
            nb_smd = sum(
                1 for p in fp.Pads() if p.GetAttribute() == pcbnew.PAD_ATTRIB_SMD
            )
            nb_tht = sum(
                1 for p in fp.Pads()
                if p.GetAttribute() in (pcbnew.PAD_ATTRIB_PTH, pcbnew.PAD_ATTRIB_NPTH)
            )
            anchored = (
                ref.startswith(_DEFAULT_ANCHOR_PREFIXES)
                or (nb_tht > 0 and nb_tht >= nb_smd)
            )

        # If the footprint bbox is degenerate (no pads parsed by pcbnew
        # for some reason), give it a 1mm default so OBB-SAT doesn't
        # divide by zero.
        bbox_w = max(bbox_w, 1.0)
        bbox_h = max(bbox_h, 1.0)

        comp = Component(
            ref=ref, unit=1, lib_id=fp.GetFPID().GetUniStringLibId(),
            x=fp_x, y=fp_y, rotation=fp_angle,
            mirror_x=False, mirror_y=False,
            bbox_w=bbox_w, bbox_h=bbox_h,
            bbox_cx=bbox_cx, bbox_cy=bbox_cy,
            pinned=anchored,
            layer=layer_name,
            coord_system="pcb",
        )

        # Component-level Spring_Class property (component-wide default).
        sc_val = _safe_get_property(fp, "Spring_Class")
        if sc_val:
            comp.spring_class = sc_val

        # Per-component Body_Margin property (mm) — overrides
        # Params.obb_repulsion_margin for this component's repulsion pairs.
        bm_val = _safe_get_property(fp, "Body_Margin")
        if bm_val:
            try:
                comp.margin = float(bm_val)
            except (ValueError, TypeError):
                logger.warning(
                    "Body_Margin on %s: not a number: %r", ref, bm_val,
                )

        # Pad → Pin records.  Use the pcbnew pad name for the pin number.
        # Also read per-pad Pin_Spring_Class:N properties (bare-string =
        # pad-general; JSON dict = per-target overrides).
        for pad in fp.Pads():
            pad_num = pad.GetPadName() or pad.GetNumber()
            if not pad_num:
                continue
            pad_num = str(pad_num)
            lx, ly = _pad_local_xy(pad, (fp_x, fp_y), fp_angle)
            comp.pins[pad_num] = Pin(
                number=pad_num,
                name=pad.GetPinFunction() or "",
                local_x=lx,
                local_y=ly,
                lib_angle=0.0,   # PCB pads don't have a meaningful outward angle
            )
            pin_cls_val = _safe_get_property(fp, f"Pin_Spring_Class:{pad_num}")
            if pin_cls_val:
                parsed = _parse_pin_spring_class(pin_cls_val)
                if parsed is not None:
                    comp.pin_classes[pad_num] = parsed
        sess.components[comp.key] = comp

    # ---- Nets ----
    # Walk pads a second time to build the net topology.  Each pad's
    # GetNetname() is the source of truth (independent of routing state).
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        comp_key = f"{ref}__u1"
        if comp_key not in sess.components:
            continue
        for pad in fp.Pads():
            netname = pad.GetNetname()
            if not netname:
                continue
            pad_num = str(pad.GetPadName() or pad.GetNumber())
            if not pad_num:
                continue
            net = sess.nets.setdefault(netname, Net(name=netname))
            net.pins.append((comp_key, pad_num))

    # ---- Auto-classify power-plane nets ----
    if auto_classify_planes:
        for net in sess.nets.values():
            if net.spring_class is not None:
                continue   # already set explicitly
            cls = _power_net_pattern_class(net.name)
            if cls:
                net.spring_class = cls

    return sess


def edge_cuts_bbox(board: Any, inset_mm: float = 1.0
                   ) -> Tuple[float, float, float, float]:
    """Return ``(x_min, y_min, x_max, y_max)`` for the Edge.Cuts bbox,
    inset by ``inset_mm`` so components don't poke the board edge.
    Falls back to ``(0, 0, 0, 0)`` if no Edge.Cuts present (treat as
    "no keep-in")."""
    edge_id = board.GetLayerID("Edge.Cuts")
    l = t = math.inf
    r = b = -math.inf
    for d in board.GetDrawings():
        if d.GetLayer() != edge_id:
            continue
        bb = d.GetBoundingBox()
        l = min(l, bb.GetLeft() / _NM_PER_MM)
        t = min(t, bb.GetTop() / _NM_PER_MM)
        r = max(r, bb.GetRight() / _NM_PER_MM)
        b = max(b, bb.GetBottom() / _NM_PER_MM)
    if not math.isfinite(l):
        return (0.0, 0.0, 0.0, 0.0)
    return (l + inset_mm, t + inset_mm, r - inset_mm, b - inset_mm)


def apply_session_to_board(sess: Session, board: Any) -> int:
    """Write back component positions + rotation to live footprints.
    Returns the number of footprints updated.

    Anchored components are skipped — their position field MIGHT have
    drifted by a tiny float epsilon during iteration, and we don't
    want to dirty the board just to write the same number back.
    """
    n = 0
    for ref, comp in (
        (c.ref, c) for c in sess.components.values()
    ):
        if comp.pinned:
            continue
        fp = board.FindFootprintByReference(ref)
        if fp is None:
            continue
        fp.SetPosition(pcbnew.VECTOR2I_MM(comp.x, comp.y))
        # KiCad's SetOrientation takes either a degree value or
        # an EDA_ANGLE; in API v9 the simple form is FromDegrees:
        try:
            fp.SetOrientation(pcbnew.EDA_ANGLE(comp.rotation, pcbnew.DEGREES_T))
        except Exception:
            # Older pcbnew API fallback.
            fp.SetOrientation(comp.rotation * 10)   # 1/10 degree units
        n += 1
    return n


# ---------------------------------------------------------------------------
# Schedule: 4-phase springs-first relaxation
# ---------------------------------------------------------------------------


@dataclass
class PCBSchedule:
    """Per-phase iteration counts and force multipliers for the
    springs-first relaxation schedule.

    Phase 1 (CLUSTER) — springs only, no repulsion: components find
    their natural connection-determined neighborhoods.  Default
    skipped (cluster_iters=0); the SPREAD phase starts repulsion
    near zero so it effectively absorbs the cluster role.
    Phase 2 (SPREAD) — repulsion ramps in geometrically: bodies
    settle into non-overlapping positions.
    Phase 3 (SNAP) — rotation-snap potential ramps in: orientations
    align to 90° multiples.
    Phase 4 (RELAX) — snap at full strength, low temperature: final
    overlap cleanup with orientations fixed.  Default skipped
    (relax_iters=0); ``enforce_rotation_snap`` produces a cleaner
    final state by hard-snapping rotations rather than asking the
    soft snap to converge.

    Defaults reflect a real-board tuning pass on power_module
    (2026-05-22) under the inverse-cube repulsion formula — the
    cubic-ramp formula's larger force ranges aren't appropriate
    here.  If a board misbehaves under these, "safer" historical
    values are: spring_k=0.1, repulsion_k_peak=30, snap_peak=3,
    pinwise_torque_k=0.05, step_spread=3.0.
    """
    cluster_iters: int = 0
    spread_iters: int = 200
    snap_iters: int = 100
    relax_iters: int = 0

    # Force scales (applied as multipliers to engine defaults)
    spring_k: float = 1.0               # attraction_k during all phases
    repulsion_k_start: float = 1e-4     # start of spread phase (1/r³ formula)
    repulsion_k_peak: float = 0.1       # peak at end of spread phase
    rotation_snap_peak: float = 30.0    # peak snap strength
    # Lever-arm torque coupling for the pin-wise spring forces.
    # Falls out of off-center forces automatically — without it
    # nothing rotates even though springs pull on pad positions.
    pinwise_torque_k: float = 1.0

    # Per-iteration step caps (mm).  These are quite small under the
    # current 1/r³ repulsion + degree normalization combo — large
    # steps cause overshoot when bodies penetrate and the saturation
    # force kicks in.
    step_cluster: float = 1.0
    step_spread: float = 0.2
    step_snap: float = 0.05
    step_relax: float = 0.05

    # Skip springs between pads on different copper layers (F.Cu vs
    # B.Cu).  Useful when a back-side anchor (cell holder, B-side
    # connector) shouldn't pull front-side parts onto its pads
    # because the actual connection routes through a via.
    cross_layer_springs: bool = True
    # Damping factor on the force-as-displacement step.  PCB schedule
    # keeps temperature constant per phase, so without damping the
    # near-equilibrium step equals the full force vector — for
    # effective restoring stiffness >= 2, components oscillate.
    force_step_damping: float = 0.3
    # Normalize each component's spring force/torque by its degree
    # (number of spring contributions).  Without this, K_eff scales
    # with N pins — a 28-pin IC has 14× the restoring stiffness of
    # a 2-pin resistor and bucks under the damping that's critical
    # for the resistor.
    normalize_spring_force_by_degree: bool = True

    # Soft boundary force during iteration (linear restoring force
    # when a component drifts past the keep-in bbox).  Was disabled
    # in v1 in favor of a post-clamp; the post-clamp still runs as
    # a final guarantee, but a boundary force during iteration
    # prevents the energetic mid-run drift that the clamp can't
    # reverse (a component pushed off-board then having to be
    # un-pushed by clamping leaves it piled on the edge).
    boundary_k: float = 1.0

    # After the soft snap phase, hard-round each non-anchored
    # component's rotation to the nearest multiple of 90°.  The
    # soft snap pulls rotations close but lever-arm torque from
    # springs can hold them slightly off-axis; the hard snap
    # guarantees alignment.
    enforce_rotation_snap: bool = True


def snap_rotations(sess: Session, period: float = 90.0) -> int:
    """Round each unpinned component's rotation to the nearest multiple
    of `period` degrees.  Returns the number of components whose
    rotation was changed.  Useful as a final cleanup after the
    soft rotation-snap phase doesn't quite converge to exact
    alignment under residual lever-arm torque.
    """
    n = 0
    for c in sess.components.values():
        if c.pinned:
            continue
        snapped = (round(c.rotation / period) * period) % 360
        # Shortest-angular-delta check so 359.99° vs 0° isn't flagged.
        if abs(((c.rotation - snapped + 540) % 360) - 180) > 1e-6:
            c.rotation = snapped
            n += 1
    return n


def run_pcb_relax(
    sess: Session,
    schedule: Optional[PCBSchedule] = None,
    *,
    margin_mm: float = 1.0,
    keep_in: Optional[Tuple[float, float, float, float]] = None,
    on_step: Optional[Any] = None,
) -> Dict[str, Any]:
    """Run the 4-phase relaxation on a PCB Session.

    Sets the v2 physics flags on ``sess.params`` and runs each phase
    with the appropriate force schedule.  ``on_step`` is an optional
    per-iteration callback (gets ``sess``) for live viz.
    ``keep_in`` is the (left, top, right, bottom) Edge.Cuts bbox; if
    provided and the schedule has ``boundary_k > 0``, a soft boundary
    force keeps components on-board during iteration.

    Returns a metrics dict (phase counts + per-phase max force).
    """
    if schedule is None:
        schedule = PCBSchedule()

    p = sess.params
    p.use_obb_repulsion = True
    p.use_spring_classes = True
    p.obb_repulsion_margin = margin_mm
    p.pinwise_torque_k = schedule.pinwise_torque_k
    p.cross_layer_springs = schedule.cross_layer_springs
    p.force_step_damping = schedule.force_step_damping
    p.normalize_spring_force_by_degree = schedule.normalize_spring_force_by_degree

    # Disable schematic-only forces.
    p.polarity_k = 0.0
    p.polarity_torque_k = 0.0
    p.rotation_k = 0.0   # disable angle-based pin-orientation torque;
                          # the off-center pin-wise springs now provide
                          # rotation via lever-arm torque (pinwise_torque_k).

    # Boundary force during iteration (when keep_in is supplied).
    # The post-clamp in `relax_placement` still runs as a final
    # safety net.
    if keep_in is not None and schedule.boundary_k > 0:
        kl, kt, kr, kb = keep_in
        p.sheet_x_min, p.sheet_x_max = kl, kr
        p.sheet_y_min, p.sheet_y_max = kt, kb
        p.boundary_k = schedule.boundary_k
    else:
        p.boundary_k = 0.0

    metrics: Dict[str, Any] = {"phases": []}

    # ---- Phase 1: CLUSTER (springs only) ----
    p.attraction_k = schedule.spring_k
    p.repulsion_k = 0.0
    p.rotation_snap_strength = 0.0
    for _ in range(schedule.cluster_iters):
        sess.temperature = schedule.step_cluster
        iterate(sess, n=1)
        if on_step:
            on_step(sess)
    metrics["phases"].append({"name": "cluster", "iters": schedule.cluster_iters,
                              "max_force": sess.last_max_force})

    # ---- Phase 2: SPREAD (repulsion ramps in GEOMETRICALLY) ----
    # Linear from 0 → peak slammed components apart on the first
    # increment; the small-start exponential growth gives a gentle
    # ease-in then accelerates, matching the schematic schedule's
    # repulsion_growth pattern.
    start = max(1e-6, schedule.repulsion_k_start)
    peak = max(start * 1.01, schedule.repulsion_k_peak)
    growth = (peak / start) ** (1.0 / max(1, schedule.spread_iters - 1))
    for t in range(schedule.spread_iters):
        p.repulsion_k = start * (growth ** t)
        sess.temperature = schedule.step_spread
        iterate(sess, n=1)
        if on_step:
            on_step(sess)
    metrics["phases"].append({"name": "spread", "iters": schedule.spread_iters,
                              "max_force": sess.last_max_force,
                              "repulsion_k_final": round(p.repulsion_k, 3)})

    # ---- Phase 3: SNAP (rotation snap ramps in) ----
    p.repulsion_k = schedule.repulsion_k_peak
    for t in range(schedule.snap_iters):
        ramp = (t + 1) / schedule.snap_iters
        p.rotation_snap_strength = schedule.rotation_snap_peak * ramp
        sess.temperature = schedule.step_snap
        iterate(sess, n=1)
        if on_step:
            on_step(sess)
    metrics["phases"].append({"name": "snap", "iters": schedule.snap_iters,
                              "max_force": sess.last_max_force})

    # ---- Hard rotation snap (optional) ----
    # Soft snap pulls rotations close but residual lever-arm torque
    # from off-center springs holds them slightly askew.  A hard
    # round to the nearest period yields the final clean orientation.
    if schedule.enforce_rotation_snap:
        n_snapped = snap_rotations(sess)
        metrics["phases"].append({"name": "hard_snap", "iters": 0,
                                  "rotations_snapped": n_snapped})

    # ---- Phase 4: RELAX (full snap, low temperature) ----
    p.rotation_snap_strength = schedule.rotation_snap_peak
    for _ in range(schedule.relax_iters):
        sess.temperature = schedule.step_relax
        iterate(sess, n=1)
        if on_step:
            on_step(sess)
    if schedule.relax_iters > 0:
        metrics["phases"].append({"name": "relax", "iters": schedule.relax_iters,
                                  "max_force": sess.last_max_force})

    return metrics


# ---------------------------------------------------------------------------
# Top-level driver (backward-compatible API)
# ---------------------------------------------------------------------------


def relax_placement(
    board: Any,
    drc_violations_path: Optional[str] = None,   # accepted for compat, unused in v2
    locked_refs: Optional[List[str]] = None,
    *,
    margin_mm: float = 1.0,
    spring_k: float = 1.0,
    repulsion_k_start: float = 1e-4,
    repulsion_k_peak: float = 0.1,
    rotation_snap_peak: float = 30.0,
    pinwise_torque_k: float = 1.0,
    cluster_iters: int = 0,
    spread_iters: int = 200,
    snap_iters: int = 100,
    relax_iters: int = 0,
    cross_layer_springs: bool = True,
    force_step_damping: float = 0.3,
    normalize_spring_force_by_degree: bool = True,
    boundary_k: float = 1.0,
    enforce_rotation_snap: bool = True,
    dry_run: bool = False,
    auto_classify_planes: bool = True,
    **legacy_kwargs: Any,
) -> Dict[str, Any]:
    """v2 entry point used by the MCP handler.  Backward compatible
    with v1's positional args (``drc_violations_path`` is now unused
    but still accepted so existing callers don't break).

    Loads the board, runs the 4-phase relax, writes positions back
    unless ``dry_run=True``.
    """
    locked = set(locked_refs) if locked_refs is not None else None
    sess = load_pcb_session(
        board, locked_refs=locked, auto_classify_planes=auto_classify_planes,
    )

    if not sess.components:
        return {
            "success": False,
            "message": "No footprints found on board.",
        }

    # Keep-in: Edge.Cuts bbox.  Engine doesn't currently honor the
    # keep-in directly (boundary_k is disabled for PCB), but we
    # post-clamp here so anything that drifts out gets pulled back.
    keep_in = edge_cuts_bbox(board, inset_mm=1.0)

    # Snapshot before for reporting.
    before_positions = {
        c.ref: (c.x, c.y, c.rotation)
        for c in sess.components.values() if not c.pinned
    }

    schedule = PCBSchedule(
        cluster_iters=cluster_iters,
        spread_iters=spread_iters,
        snap_iters=snap_iters,
        relax_iters=relax_iters,
        spring_k=spring_k,
        repulsion_k_start=repulsion_k_start,
        repulsion_k_peak=repulsion_k_peak,
        rotation_snap_peak=rotation_snap_peak,
        pinwise_torque_k=pinwise_torque_k,
        cross_layer_springs=cross_layer_springs,
        force_step_damping=force_step_damping,
        normalize_spring_force_by_degree=normalize_spring_force_by_degree,
        boundary_k=boundary_k,
        enforce_rotation_snap=enforce_rotation_snap,
    )
    metrics = run_pcb_relax(sess, schedule, margin_mm=margin_mm, keep_in=keep_in)

    # Clamp to keep-in if it's non-degenerate.
    kl, kt, kr, kb = keep_in
    if kr > kl and kb > kt:
        for c in sess.components.values():
            if c.pinned:
                continue
            # Clamp the OBB center (= body center), not the footprint
            # origin.  For off-center bboxes (pin headers anchored at
            # pin 1) the two diverge; clamping the origin would let
            # the body overhang Edge.Cuts.
            bx, by = c.obb_center_world()
            dx, dy = bx - c.x, by - c.y
            hw, hh = c.bbox_w * 0.5, c.bbox_h * 0.5
            new_bx = max(kl + hw, min(kr - hw, bx))
            new_by = max(kt + hh, min(kb - hh, by))
            c.x = new_bx - dx
            c.y = new_by - dy

    # Compute moves for reporting.
    moves: List[Dict[str, Any]] = []
    for ref, (ox, oy, orot) in before_positions.items():
        c = next((cc for cc in sess.components.values() if cc.ref == ref), None)
        if c is None:
            continue
        dx, dy = c.x - ox, c.y - oy
        drot = ((c.rotation - orot + 540) % 360) - 180
        mag = math.hypot(dx, dy)
        if mag < 0.01 and abs(drot) < 0.5:
            continue
        moves.append({
            "ref": ref,
            "from": {"x": round(ox, 3), "y": round(oy, 3), "rotation": round(orot, 1)},
            "to": {"x": round(c.x, 3), "y": round(c.y, 3), "rotation": round(c.rotation, 1)},
            "delta_mm": round(mag, 3),
            "delta_rotation_deg": round(drot, 1),
        })
    moves.sort(key=lambda m: -m["delta_mm"])

    if not dry_run:
        n_written = apply_session_to_board(sess, board)
    else:
        n_written = 0

    n_anchored = sum(1 for c in sess.components.values() if c.pinned)
    return {
        "success": True,
        "dry_run": dry_run,
        "components_total": len(sess.components),
        "components_anchored": n_anchored,
        "components_moved": len(moves),
        "components_written": n_written,
        "n_nets": len(sess.nets),
        "metrics": metrics,
        "top_moves": moves[:20],
        "message": (
            f"relax_placement v2: {len(sess.components)} comps "
            f"({n_anchored} anchored), {len(sess.nets)} nets, "
            f"{len(moves)} moved"
        ),
    }
