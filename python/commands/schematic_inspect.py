"""Schematic diagnostic helpers.

Two public entry points, both used by MCP tools and callable from
Python or tests:

  * :func:`diagnose_chains` — enumerate physical wire chains in a
    .kicad_sch, list their labels + pin endpoints, and flag
    ``DUPLICATE_LABELS`` / ``CROSS_NET`` / ``LOOP`` chains.  Useful
    for triaging post-route schematics.

  * :func:`compare_netlists` — given two .kicad_sch files (typically
    a "before" and "after" of a placement / routing operation),
    assert that every pin's named-net assignment is preserved.  This
    is the test that catches dropped pins like the BB_SW1 / C23
    regression where ``load_session`` mis-discovered a long wire chain
    and ``apply_to_schematic`` then stripped it permanently.

Both functions use the same wire-graph BFS (``walk_wire_chain`` and
its underlying primitives in :mod:`commands.wire_connectivity`) that
``connect_pins`` Phase 5 uses, so chain detection is consistent across
the codebase.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import sexpdata
from sexpdata import Symbol

from commands.connection_schematic import ConnectionManager
from commands.pin_locator import PinLocator
from commands.wire_connectivity import (
    _build_adjacency,
    _load_sexp,
    _parse_labels_sexp,
    _parse_wires_sexp,
)

_IU_PER_MM = 10000


def _iu_to_mm(p: Tuple[int, int]) -> Tuple[float, float]:
    return (p[0] / _IU_PER_MM, p[1] / _IU_PER_MM)


def _all_component_pin_world_coords(
    sch_path: Path,
) -> List[Tuple[str, str, Tuple[int, int]]]:
    """Return [(ref, pin_num, iu_pt), ...] for every placed non-virtual
    symbol in the file.  Excludes ``_TEMPLATE`` and the virtual
    ``#PWR``/``#FLG`` placeholder symbols (those are KiCad-internal
    label aliases, not real components)."""
    locator = PinLocator()
    sexp = _load_sexp(str(sch_path))
    out: List[Tuple[str, str, Tuple[int, int]]] = []
    for item in sexp:
        if not (isinstance(item, list) and item and item[0] == Symbol("symbol")):
            continue
        ref: Optional[str] = None
        for sub in item[1:]:
            if (
                isinstance(sub, list)
                and len(sub) >= 3
                and sub[0] == Symbol("property")
                and str(sub[1]).strip('"') == "Reference"
            ):
                ref = str(sub[2]).strip('"')
                break
        if not ref or ref.startswith("_TEMPLATE"):
            continue
        if ref.startswith("#PWR") or ref.startswith("#FLG"):
            continue
        all_pins = locator.get_all_symbol_pins(sch_path, ref) or {}
        for pn, val in all_pins.items():
            x, y = val[0], val[1]
            out.append(
                (ref, pn, (round(x * _IU_PER_MM), round(y * _IU_PER_MM)))
            )
    return out


def _label_instances_sexp(
    sch_path: Path,
) -> List[Tuple[str, Tuple[int, int]]]:
    """Return every (label | global_label | hierarchical_label) entry as
    (name, iu_position).  Each repeated label gets its own entry; this is
    what makes ``DUPLICATE_LABELS`` detection possible (the existing
    ``_parse_labels_sexp`` collapses positions per name)."""
    sexp = _load_sexp(str(sch_path))
    label_syms = {
        Symbol("label"),
        Symbol("global_label"),
        Symbol("hierarchical_label"),
    }
    out: List[Tuple[str, Tuple[int, int]]] = []
    for item in sexp:
        if not (
            isinstance(item, list)
            and len(item) >= 2
            and item[0] in label_syms
            and isinstance(item[1], str)
        ):
            continue
        name = item[1].strip('"')
        for sub in item[2:]:
            if (
                isinstance(sub, list)
                and sub
                and sub[0] == Symbol("at")
                and len(sub) >= 3
            ):
                pt = (
                    round(float(sub[1]) * _IU_PER_MM),
                    round(float(sub[2]) * _IU_PER_MM),
                )
                out.append((name, pt))
                break
    return out


def _chain_has_cycle(
    wire_indices: Set[int],
    adjacency: List[Set[int]],
) -> bool:
    """DFS detect a cycle in the chain (counting each undirected edge once)."""
    if not wire_indices:
        return False
    seen_edges: Set[Tuple[int, int]] = set()
    visited: Set[int] = set()
    start = next(iter(wire_indices))
    stack: List[Tuple[int, int]] = [(start, -1)]
    while stack:
        w, parent = stack.pop()
        if w in visited:
            return True
        visited.add(w)
        for nb in adjacency[w]:
            if nb not in wire_indices:
                continue
            edge = (w, nb) if w < nb else (nb, w)
            if edge in seen_edges:
                continue
            seen_edges.add(edge)
            if nb != parent:
                stack.append((nb, w))
    return False


def diagnose_chains(
    schematic_path: Path,
    filter_nets: Sequence[str] = (),
) -> Dict[str, Any]:
    """Enumerate connected wire chains and flag pathological ones.

    Each chain is the BFS-reachable set of wires through endpoint and
    T-junction adjacency.  Flags:

      * ``DUPLICATE_LABELS`` — same label name appears at >1 position
        on the chain.  Often legitimate for #PWR_GND-style chains
        (multiple ground stamps), but a Phase 3 regression for
        multi-unit duplicate pads if labels stack at identical coords.
      * ``CROSS_NET`` — chain carries labels for ≥2 distinct nets.
        Almost always a defect (the chain shouldn't be reachable from
        two different label names without the schematic being wrong).
      * ``LOOP`` — wire graph has a cycle.  Sometimes intentional in
        the file, but in autorouted output usually a sign of parallel-
        edge multi-unit duplicate-pad wiring.

    ``filter_nets`` (optional): if non-empty, only return chains whose
    label set intersects this list, OR chains flagged CROSS_NET.

    Returns a dict with ``n_chains``, ``chains`` (list of per-chain
    dicts), and a ``flag_counts`` summary.
    """
    sch_path = Path(schematic_path)
    sexp = _load_sexp(str(sch_path))
    all_wires = _parse_wires_sexp(sexp)
    if not all_wires:
        return {
            "success": True,
            "schematic_path": str(sch_path),
            "n_chains": 0,
            "chains": [],
            "flag_counts": {
                "DUPLICATE_LABELS": 0,
                "CROSS_NET": 0,
                "LOOP": 0,
            },
        }
    adjacency, iu_to_wires = _build_adjacency(all_wires)
    point_to_label, _ = _parse_labels_sexp(sexp)
    label_instances = _label_instances_sexp(sch_path)
    component_pins = _all_component_pin_world_coords(sch_path)

    # Flood-fill into chains.
    seen_wire: Set[int] = set()
    raw_chains: List[Dict[str, Any]] = []
    for i in range(len(all_wires)):
        if i in seen_wire:
            continue
        visited = {i}
        queue = [i]
        points: Set[Tuple[int, int]] = set()
        while queue:
            w = queue.pop()
            points.update(all_wires[w])
            for nb in adjacency[w]:
                if nb not in visited:
                    visited.add(nb)
                    queue.append(nb)
        seen_wire |= visited
        raw_chains.append({"wire_indices": set(visited), "points": points})

    # Annotate each chain.
    chains: List[Dict[str, Any]] = []
    for c in raw_chains:
        labels_on = [name for (name, pt) in label_instances if pt in c["points"]]
        distinct = sorted(set(labels_on))
        pins_on = sorted(
            (ref, pin)
            for ref, pin, pt in component_pins
            if pt in c["points"]
        )
        name_counts: Dict[str, int] = defaultdict(int)
        for n in labels_on:
            name_counts[n] += 1
        flags: List[str] = []
        if any(v > 1 for v in name_counts.values()):
            flags.append("DUPLICATE_LABELS")
        if len(distinct) >= 2:
            flags.append("CROSS_NET")
        if _chain_has_cycle(c["wire_indices"], adjacency):
            flags.append("LOOP")

        pts_mm = [_iu_to_mm(p) for p in c["points"]]
        xs = [p[0] for p in pts_mm]
        ys = [p[1] for p in pts_mm]
        bbox = (min(xs), min(ys), max(xs), max(ys))

        chains.append(
            {
                "wire_count": len(c["wire_indices"]),
                "label_count": len(labels_on),
                "labels": distinct,
                "label_positions": [
                    {
                        "name": name,
                        "x": _iu_to_mm(pt)[0],
                        "y": _iu_to_mm(pt)[1],
                    }
                    for (name, pt) in label_instances
                    if pt in c["points"]
                ],
                "pins": [{"ref": r, "pin": p} for (r, p) in pins_on],
                "bbox": list(bbox),
                "flags": flags,
            }
        )

    chains.sort(key=lambda d: tuple(d["bbox"]))
    # Assign stable ids after sort.
    for idx, c in enumerate(chains):
        c["id"] = idx

    # Filter.
    if filter_nets:
        ftrset = set(filter_nets)
        chains = [
            c for c in chains
            if (set(c["labels"]) & ftrset) or ("CROSS_NET" in c["flags"])
        ]

    flag_counts = {
        "DUPLICATE_LABELS": sum(1 for c in chains if "DUPLICATE_LABELS" in c["flags"]),
        "CROSS_NET": sum(1 for c in chains if "CROSS_NET" in c["flags"]),
        "LOOP": sum(1 for c in chains if "LOOP" in c["flags"]),
    }

    return {
        "success": True,
        "schematic_path": str(sch_path),
        "n_chains": len(chains),
        "chains": chains,
        "flag_counts": flag_counts,
    }


def compare_netlists(
    orig_path: Path,
    new_path: Path,
) -> Dict[str, Any]:
    """Compare per-pin net assignments between two .kicad_sch files.

    For each component pin in *orig_path*, compute its net via the
    same engine ``ConnectionManager.get_pin_net`` uses (wire BFS +
    label / power-symbol resolution).  Do the same for *new_path*.
    Report any pin whose named-net membership changed.

    Returns a dict with:
      * ``preserved``: bool — True iff no mismatches.
      * ``orig_n_pins`` / ``new_n_pins``
      * ``orig_n_nets`` / ``new_n_nets`` — count of distinct named nets.
      * ``missing_pins``: pins present in orig but absent in new.
      * ``extra_pins``: pins absent in orig but present in new.
      * ``net_mismatches``: list of {net, lost, added}.  ``lost`` is
        pins that were on this net in orig but aren't in new; ``added``
        is pins on this net in new that weren't in orig.

    A perfect equivalence report is ``preserved=True``, no missing /
    extra / mismatched.  Useful as a CI assertion after any layout-
    mutating operation (autoplacer apply, manual edits, etc.)."""
    orig_path = Path(orig_path)
    new_path = Path(new_path)

    def _nets(path: Path) -> Dict[Tuple[str, str], Optional[str]]:
        result: Dict[Tuple[str, str], Optional[str]] = {}
        for ref, pn, _ in _all_component_pin_world_coords(path):
            n = ConnectionManager.get_pin_net(path, ref, pn)
            result[(ref, pn)] = n
        return result

    orig_nets = _nets(orig_path)
    new_nets = _nets(new_path)

    def _partition(d: Dict) -> Dict[str, Set]:
        out: Dict[str, Set] = {}
        for pin, net in d.items():
            if net is None:
                continue
            out.setdefault(net, set()).add(pin)
        return out

    orig_parts = _partition(orig_nets)
    new_parts = _partition(new_nets)

    missing = sorted(set(orig_nets) - set(new_nets))
    extra = sorted(set(new_nets) - set(orig_nets))

    net_mismatches: List[Dict[str, Any]] = []
    for net in sorted(set(orig_parts) | set(new_parts)):
        orig_pins = orig_parts.get(net, set())
        new_pins = new_parts.get(net, set())
        if orig_pins == new_pins:
            continue
        net_mismatches.append(
            {
                "net": net,
                "lost": [{"ref": r, "pin": p} for (r, p) in sorted(orig_pins - new_pins)],
                "added": [{"ref": r, "pin": p} for (r, p) in sorted(new_pins - orig_pins)],
            }
        )

    return {
        "success": True,
        "orig_path": str(orig_path),
        "new_path": str(new_path),
        "orig_n_pins": len(orig_nets),
        "new_n_pins": len(new_nets),
        "orig_n_nets": len(orig_parts),
        "new_n_nets": len(new_parts),
        "missing_pins": [{"ref": r, "pin": p} for (r, p) in missing],
        "extra_pins": [{"ref": r, "pin": p} for (r, p) in extra],
        "net_mismatches": net_mismatches,
        "preserved": (not missing and not extra and not net_mismatches),
    }
