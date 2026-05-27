"""Placement-constraint primitives.

This module owns the parsing/resolving/rename-propagation for the
``Placement_Anchor`` property family on schematic symbols, plus the
PCB-side helpers used by ``decoupling_audit`` and ``place_near``.

Property model (v1, controlled by ``mcp_constraint_version: 1`` in
``.kicad_pro``):

    Placement_Anchor = "<REF>[.<PIN>]/within=<N>mm[; <REF>[.<PIN>]/within=<N>mm]..."

Examples::

    Placement_Anchor = "U1.10/within=3mm"
    Placement_Anchor = "U1.10/within=3mm; U1.9/within=3mm"
    Placement_Anchor = "U1/within=8mm"     # no pin: nearest body point

Distance semantics:

    * ``REF.PIN`` -> distance from the component's nearest PCB pad to the
      target pad (centre-to-centre on KiCad's pad position).
    * ``REF`` only -> distance from the component centre to the nearest
      point on the target's footprint bbox.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

CONSTRAINT_VERSION = 2   # v2 = adds mcp_spring_classes section
ANCHOR_PROPERTY = "Placement_Anchor"

# Nets we treat as ground for auto-discovery of decoupling pairs.
_GND_EXACT = {"GND", "VSS", "AGND", "DGND", "PGND", "GNDA", "GNDD", "GNDPWR"}
_GND_PATTERNS = (
    re.compile(r"^GND[_/].*$"),
    re.compile(r".*[_/]GND$"),
    re.compile(r".*[_/]VSS$"),
)

# Pin electrical types that count as "power" for IC-side anchor selection.
_POWER_PIN_TYPES = {"power_in", "power_out"}

# Single anchor clause, e.g. "U1.10/within=3mm" or "U1/within=8mm".
_ANCHOR_RE = re.compile(
    r"^\s*"
    r"(?P<ref>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?:\.(?P<pin>[A-Za-z0-9_]+))?"
    r"\s*/\s*within\s*=\s*(?P<dist>\d+(?:\.\d+)?)\s*mm"
    r"\s*$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Property grammar
# ---------------------------------------------------------------------------


@dataclass
class AnchorClause:
    """One parsed anchor: target component, optional pin, max distance (mm)."""

    target_ref: str
    target_pin: Optional[str]
    max_dist_mm: float

    def __str__(self) -> str:
        suffix = f".{self.target_pin}" if self.target_pin else ""
        return f"{self.target_ref}{suffix}/within={self.max_dist_mm}mm"


def parse_anchor_value(value: str) -> Tuple[List[AnchorClause], List[str]]:
    """Parse a ``Placement_Anchor`` property value.

    Returns ``(clauses, errors)``. ``errors`` is a list of per-clause
    parse failures so the audit can surface them to the user rather
    than silently dropping malformed entries.
    """
    if value is None:
        return [], []
    raw = str(value).strip()
    if not raw:
        return [], []
    clauses: List[AnchorClause] = []
    errors: List[str] = []
    for chunk in raw.split(";"):
        c = chunk.strip()
        if not c:
            continue
        m = _ANCHOR_RE.match(c)
        if not m:
            errors.append(c)
            continue
        clauses.append(
            AnchorClause(
                target_ref=m.group("ref"),
                target_pin=m.group("pin"),
                max_dist_mm=float(m.group("dist")),
            )
        )
    return clauses, errors


def rewrite_anchor_value(value: str, mapping: Dict[str, str]) -> Tuple[str, List[str]]:
    """Rewrite refs inside a Placement_Anchor value via ``mapping``.

    Used by rename-propagation. Returns ``(new_value, replaced_refs)``.
    Unparseable clauses are passed through verbatim.
    """
    if not value or not mapping:
        return value or "", []
    chunks = value.split(";")
    replaced: List[str] = []
    out_chunks: List[str] = []
    for chunk in chunks:
        c = chunk.strip()
        if not c:
            out_chunks.append(chunk)
            continue
        m = _ANCHOR_RE.match(c)
        if not m:
            out_chunks.append(chunk)
            continue
        old_ref = m.group("ref")
        new_ref = mapping.get(old_ref)
        if new_ref is None or new_ref == old_ref:
            out_chunks.append(chunk)
            continue
        replaced.append(old_ref)
        rebuilt = AnchorClause(
            target_ref=new_ref,
            target_pin=m.group("pin"),
            max_dist_mm=float(m.group("dist")),
        )
        out_chunks.append(str(rebuilt))
    return "; ".join(p.strip() for p in out_chunks if p.strip()), replaced


# ---------------------------------------------------------------------------
# Schematic_Metadata singleton version marker
# ---------------------------------------------------------------------------


def get_constraint_version(schematic_path: str | Path) -> Optional[int]:
    """Return ``mcp_constraint_version`` from the schematic's
    Schematic_Metadata singleton, or None if the singleton doesn't
    carry the key.

    Accepts either a ``.kicad_sch`` or a ``.kicad_pro`` path (the
    latter is mapped to its sibling .kicad_sch for backward
    compatibility with callers that still pass the project path).
    """
    p = Path(schematic_path)
    if p.suffix == ".kicad_pro":
        p = p.with_suffix(".kicad_sch")
    if not p.exists():
        return None
    try:
        from commands.schematic_metadata import read_metadata_json
        val = read_metadata_json(p, "mcp_constraint_version")
    except Exception as e:
        logger.warning(
            f"Could not read singleton for constraint version: {e}"
        )
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        try:
            return int(val)
        except ValueError:
            return None
    return None


def ensure_constraint_version(
    schematic_path: str | Path,
    version: int = CONSTRAINT_VERSION,
) -> bool:
    """Write ``mcp_constraint_version`` to the schematic's
    Schematic_Metadata singleton if not already at ``version``.
    Creates the singleton if absent. Returns True if anything was
    written.

    Accepts either a ``.kicad_sch`` or ``.kicad_pro`` path.
    """
    p = Path(schematic_path)
    if p.suffix == ".kicad_pro":
        p = p.with_suffix(".kicad_sch")
    if not p.exists():
        return False
    if get_constraint_version(p) == version:
        return False
    try:
        from commands.schematic_metadata import write_metadata_key
        r = write_metadata_key(p, "mcp_constraint_version", version)
        return bool(r.get("success"))
    except Exception as e:
        logger.warning(
            f"Could not write singleton constraint version: {e}"
        )
        return False


# ---------------------------------------------------------------------------
# Schematic traversal (multi-sheet)
# ---------------------------------------------------------------------------


def find_top_schematic(any_sch_path: str | Path) -> Path:
    """Find the top-level .kicad_sch sibling of the .kicad_pro in the
    same directory. Falls back to the given path if no .kicad_pro is
    found.

    Used by rename-propagation: a rename on a sub-sheet still needs to
    walk the whole project for Placement_Anchor refs that point at the
    renamed component.
    """
    p = Path(any_sch_path).resolve()
    parent = p.parent
    pros = list(parent.glob("*.kicad_pro"))
    if not pros:
        return p
    top = pros[0].with_suffix(".kicad_sch")
    return top if top.exists() else p


def _all_sheet_paths(top_sch: str | Path) -> List[Path]:
    """Top schematic + every hierarchical sub-sheet, deduped."""
    from commands.wire_connectivity import _discover_sub_sheets

    top = Path(top_sch).resolve()
    paths = [top]
    for sub in _discover_sub_sheets(str(top)):
        sp = Path(sub).resolve()
        if sp not in paths:
            paths.append(sp)
    return paths


@dataclass
class ComponentRecord:
    reference: str
    sheet_path: Path
    lib_id: str
    value: str
    anchor: Optional[str] = None
    properties: Dict[str, str] = field(default_factory=dict)


def iter_components(top_sch: str | Path) -> Iterable[ComponentRecord]:
    """Walk the top + sub-sheets and yield component records.

    Lazy / cheap — only string properties, no pin lookup (callers can
    use ``PinLocator`` directly).
    """
    from commands.schematic import SchematicManager

    for sheet_path in _all_sheet_paths(top_sch):
        sch = SchematicManager.load_schematic(str(sheet_path))
        if sch is None:
            continue
        for symbol in sch.symbol:
            if not hasattr(symbol.property, "Reference"):
                continue
            ref = symbol.property.Reference.value
            if ref.startswith("_TEMPLATE"):
                continue
            props: Dict[str, str] = {}
            anchor: Optional[str] = None
            for p in symbol.property:
                try:
                    pname = p.name if hasattr(p, "name") else None
                    pval = p.value if hasattr(p, "value") else None
                except Exception:
                    continue
                if pname is None:
                    continue
                props[str(pname)] = "" if pval is None else str(pval)
                if str(pname) == ANCHOR_PROPERTY and pval:
                    anchor = str(pval)
            lib_id = symbol.lib_id.value if hasattr(symbol, "lib_id") else ""
            value = props.get("Value", "")
            yield ComponentRecord(
                reference=ref,
                sheet_path=sheet_path,
                lib_id=lib_id,
                value=value,
                anchor=anchor,
                properties=props,
            )


# ---------------------------------------------------------------------------
# Rename propagation
# ---------------------------------------------------------------------------


def propagate_rename(
    top_sch: str | Path,
    rename_map: Dict[str, str],
    write_back: Callable[[Path, Dict[str, str]], None] | None = None,
) -> Dict[str, Any]:
    """Walk all sheets, rewrite Placement_Anchor values referencing any
    key in ``rename_map`` to its mapped new ref.

    ``write_back`` is called as ``write_back(sheet_path, ref_to_new_value)``
    for each sheet that needs writes; default implementation uses
    ``edit_schematic_component``-style property updates via direct file
    string replacement (the ``Placement_Anchor`` property is a plain
    string field in the s-expression).

    Returns a summary dict: ``updated``, ``dangling``, ``sheetsTouched``.
    """
    updated: List[Dict[str, str]] = []
    dangling: List[Dict[str, str]] = []
    sheets_touched: List[str] = []

    # First, collect every valid ref in the project so we can flag
    # dangling references (post-rename) in the same pass.
    valid_refs = {c.reference for c in iter_components(top_sch)}

    # Group writes by sheet so we touch each file once.
    per_sheet: Dict[Path, Dict[str, str]] = {}

    for comp in iter_components(top_sch):
        if not comp.anchor:
            continue
        new_value, replaced = rewrite_anchor_value(comp.anchor, rename_map)
        clauses, _errors = parse_anchor_value(new_value)
        for clause in clauses:
            # After rename, does the target still resolve?
            if clause.target_ref not in valid_refs and clause.target_ref not in rename_map.values():
                dangling.append(
                    {
                        "owner": comp.reference,
                        "anchor": str(clause),
                        "reason": f"target ref {clause.target_ref} not found in project",
                    }
                )
        if new_value != comp.anchor:
            per_sheet.setdefault(comp.sheet_path, {})[comp.reference] = new_value
            updated.append(
                {
                    "ref": comp.reference,
                    "sheet": str(comp.sheet_path),
                    "old": comp.anchor,
                    "new": new_value,
                    "replaced": ",".join(replaced),
                }
            )

    writer = write_back or _default_anchor_writer
    for sheet_path, ref_values in per_sheet.items():
        writer(sheet_path, ref_values)
        sheets_touched.append(str(sheet_path))

    return {
        "updated": updated,
        "dangling": dangling,
        "sheetsTouched": sheets_touched,
    }


def _default_anchor_writer(sheet_path: Path, ref_to_new_value: Dict[str, str]) -> None:
    """Rewrite Placement_Anchor property values in-place via kicad-skip.

    Falls back to verbatim regex on parse failure (defensive: kicad-skip
    occasionally trips on hand-edited s-expressions).
    """
    from commands.schematic import SchematicManager

    sch = SchematicManager.load_schematic(str(sheet_path))
    if sch is None:
        logger.warning(f"propagate_rename: could not load {sheet_path}")
        return
    changed = False
    for symbol in sch.symbol:
        if not hasattr(symbol.property, "Reference"):
            continue
        ref = symbol.property.Reference.value
        if ref not in ref_to_new_value:
            continue
        new_val = ref_to_new_value[ref]
        try:
            anchor_prop = getattr(symbol.property, ANCHOR_PROPERTY)
            anchor_prop.value = new_val
            changed = True
        except AttributeError:
            logger.warning(
                f"propagate_rename: {ref} on {sheet_path} has no {ANCHOR_PROPERTY} property"
            )
    if changed:
        SchematicManager.save_schematic(sch, str(sheet_path))


# ---------------------------------------------------------------------------
# Decoupling-pair discovery
# ---------------------------------------------------------------------------


def _is_gnd(net: str) -> bool:
    if not net:
        return False
    if net in _GND_EXACT:
        return True
    return any(pat.match(net) for pat in _GND_PATTERNS)


@dataclass
class DecouplingPair:
    cap_ref: str
    ic_ref: str
    ic_pin: str
    power_net: str
    source: str  # "explicit" | "auto"
    max_dist_mm: float


def discover_decoupling_pairs(
    top_sch: str | Path,
    default_max_dist_mm: float = 5.0,
) -> Tuple[List[DecouplingPair], List[Dict[str, str]]]:
    """Auto-discover cap↔IC power-pin pairs by net analysis.

    Pure schematic analysis — no PCB read needed. Returns (pairs, parse_errors).

    A cap (ref ``C*``) is a decoupling cap for IC pin ``Ux.PIN`` if:
      * the IC pin is electrical_type ``power_in`` or ``power_out``
      * one of the cap's two pads is on the same net as the IC pin
      * the cap's other pad is on a GND-like net
    """
    from commands.pin_locator import PinLocator
    from commands.schematic import SchematicManager
    from commands.wire_connectivity import get_all_net_connections

    pairs: List[DecouplingPair] = []
    parse_errors: List[Dict[str, str]] = []
    locator = PinLocator()

    top_path = str(Path(top_sch).resolve())
    top_sch_obj = SchematicManager.load_schematic(top_path)
    if top_sch_obj is None:
        return [], [{"ref": "(root)", "anchor": top_path, "badClause": "could not load schematic"}]

    # Build the full pin↔net map in one pass across all sheets (bulk
    # variant of get_connections_for_net — ~40× faster on power_module
    # because the per-sheet setup runs once instead of once per net).
    net_to_pins = get_all_net_connections(top_sch_obj, top_path)
    pin_net: Dict[Tuple[str, str], str] = {}
    for net, conns in net_to_pins.items():
        for conn in conns:
            key = (conn["component"], str(conn["pin"]))
            pin_net.setdefault(key, net)

    # Pin electrical types come from the symbol library — fetch via PinLocator.
    by_ref_lib: Dict[str, Tuple[Path, str]] = {}
    for comp in iter_components(top_sch):
        by_ref_lib[comp.reference] = (comp.sheet_path, comp.lib_id)
        # Surface anchor parse errors too.
        if comp.anchor:
            _, errs = parse_anchor_value(comp.anchor)
            for err in errs:
                parse_errors.append(
                    {"ref": comp.reference, "anchor": comp.anchor, "badClause": err}
                )

    # Cache symbol-pin definitions per lib_id.
    sym_pins_cache: Dict[Tuple[str, str], Dict[str, Dict]] = {}

    def _get_pin_type(ref: str, pin_num: str) -> str:
        if ref not in by_ref_lib:
            return ""
        sheet_path, lib_id = by_ref_lib[ref]
        cache_key = (str(sheet_path), lib_id)
        pins = sym_pins_cache.get(cache_key)
        if pins is None:
            pins = locator.get_symbol_pins(sheet_path, lib_id) or {}
            sym_pins_cache[cache_key] = pins
        return pins.get(pin_num, {}).get("type", "")

    # Walk every IC pin that's power_*.
    for (ref, pin_num), net in pin_net.items():
        if not ref or not net:
            continue
        ptype = _get_pin_type(ref, pin_num)
        if ptype not in _POWER_PIN_TYPES:
            continue
        # Find caps with one pad on `net` and the other on GND.
        for cap_ref in (r for r in by_ref_lib if r.startswith("C") and r[1:].isdigit()):
            pads = [(pn, n) for (rr, pn), n in pin_net.items() if rr == cap_ref]
            if len(pads) != 2:
                continue
            nets = {n for _pn, n in pads}
            if net not in nets:
                continue
            if not any(_is_gnd(n) for n in nets if n != net):
                continue
            pairs.append(
                DecouplingPair(
                    cap_ref=cap_ref,
                    ic_ref=ref,
                    ic_pin=pin_num,
                    power_net=net,
                    source="auto",
                    max_dist_mm=default_max_dist_mm,
                )
            )

    return pairs, parse_errors


# ---------------------------------------------------------------------------
# PCB-side distance helpers
# ---------------------------------------------------------------------------


def _nm_to_mm(p: Tuple[float, float]) -> Tuple[float, float]:
    return (p[0] / 1_000_000.0, p[1] / 1_000_000.0)


def distance_pad_to_pad(board: Any, ref_a: str, pin_a: Optional[str],
                       ref_b: str, pin_b: Optional[str]) -> Optional[float]:
    """Return mm distance between two pads. If a pin is None, picks the
    closest pad on that footprint to the *other* anchor point."""
    fp_a = board.FindFootprintByReference(ref_a)
    fp_b = board.FindFootprintByReference(ref_b)
    if fp_a is None or fp_b is None:
        return None

    def _pad_pos(fp, pin_num: Optional[str]) -> Optional[Tuple[float, float]]:
        if pin_num is None:
            return None
        p = fp.FindPadByNumber(str(pin_num))
        if p is None:
            return None
        pos = p.GetPosition()
        return (pos.x, pos.y)

    pa = _pad_pos(fp_a, pin_a)
    pb = _pad_pos(fp_b, pin_b)

    if pa is None and pb is None:
        # Compare centres.
        pa = (fp_a.GetPosition().x, fp_a.GetPosition().y)
        pb = (fp_b.GetPosition().x, fp_b.GetPosition().y)
    elif pa is None:
        # Pick a's pad closest to b's anchor.
        pa = _nearest_pad_pos(fp_a, pb)
        if pa is None:
            pa = (fp_a.GetPosition().x, fp_a.GetPosition().y)
    elif pb is None:
        pb = _nearest_pad_pos(fp_b, pa)
        if pb is None:
            pb = (fp_b.GetPosition().x, fp_b.GetPosition().y)

    dx = (pa[0] - pb[0]) / 1_000_000.0
    dy = (pa[1] - pb[1]) / 1_000_000.0
    return (dx * dx + dy * dy) ** 0.5


def _nearest_pad_pos(fp, target_nm: Tuple[float, float]) -> Optional[Tuple[float, float]]:
    best: Optional[Tuple[float, float]] = None
    best_d2 = float("inf")
    for pad in fp.Pads():
        pos = pad.GetPosition()
        d2 = (pos.x - target_nm[0]) ** 2 + (pos.y - target_nm[1]) ** 2
        if d2 < best_d2:
            best_d2 = d2
            best = (pos.x, pos.y)
    return best
