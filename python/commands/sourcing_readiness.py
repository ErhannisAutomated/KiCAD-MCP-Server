"""Sourcing-readiness audit.

Before `sync_schematic_to_board`, the schematic must have every
non-power symbol carrying at minimum a Footprint and (for JLCPCB SMT)
an LCSC part number.  It's easy to set LCSC + MPN in a sourcing pass
and forget the Footprint field — `sync_schematic_to_board` then
silently skips the empty ones and the resulting PCB is short a bunch
of components even though the sync call reports "success".  Worse,
subsequent sync calls only refresh nets, not footprints — so once
you're past the first sync you can end up in a state where filling in
the missing Footprints doesn't auto-import them on the next sync
(you'd have to reset the .kicad_pcb to its template state and re-sync).

This tool answers "is this schematic ready for sync?" in a single
call.  Categories:

    missing_footprint       — Footprint property is empty or absent
    missing_lcsc            — LCSC property is empty or absent
    missing_mpn             — MPN property is empty or absent
    unresolvable_footprint  — Footprint points to a project-local
                              library file that doesn't exist on disk
    already_on_pcb          — echoed count to help distinguish a
                              first-sync from a retroactive-sync
                              situation
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger("kicad_interface")


def _load_fp_lib_table(project_dir: Path) -> Dict[str, Path]:
    """Read fp-lib-table and return {lib_name: absolute directory Path}.
    Only entries with KIPRJMOD-anchored URIs are resolved to real paths —
    others (system libs) stay unresolved and are treated as trusted.
    """
    table_path = project_dir / "fp-lib-table"
    if not table_path.exists():
        return {}

    text = table_path.read_text(encoding="utf-8")
    result: Dict[str, Path] = {}
    for m in re.finditer(
        r'\(lib\s+\(name\s+"([^"]+)"\)\s*\(type\s+"[^"]+"\)\s*\(uri\s+"([^"]+)"\)',
        text,
    ):
        name, uri = m.group(1), m.group(2)
        if "${KIPRJMOD}" in uri:
            resolved = uri.replace("${KIPRJMOD}", str(project_dir))
            result[name] = Path(resolved)
        # System-lib entries (KISYSMOD or absolute paths) intentionally
        # left out — we don't check those.
    return result


def _footprint_files_on_pcb(board_path: Path) -> Set[str]:
    """Return the set of Reference designators already present as
    footprints on the .kicad_pcb.  Text-mode scan; a full parse isn't
    needed for a presence check.
    """
    if not board_path.exists():
        return set()
    text = board_path.read_text(encoding="utf-8")
    return set(re.findall(r'\(property\s+"Reference"\s+"([^"]+)"', text))


def check_sourcing_readiness(
    schematic_path: str,
    board_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Audit the schematic (root or sub-sheet) for readiness to sync to
    a PCB.  If `board_path` is provided, also computes the retroactive-
    sync gap: symbols with Footprint set but not yet on the PCB.
    Returns per-category lists.  `success` is `True` even when problems
    are found — the caller should look at the counts.
    """
    from commands.placement_constraints import find_top_schematic, iter_components

    top = find_top_schematic(schematic_path)
    project_dir = Path(top).parent

    local_libs = _load_fp_lib_table(project_dir)

    missing_footprint: List[Dict[str, str]] = []
    missing_lcsc: List[Dict[str, str]] = []
    missing_mpn: List[Dict[str, str]] = []
    unresolvable_footprint: List[Dict[str, str]] = []
    all_refs: List[str] = []

    for rec in iter_components(top):
        ref = rec.reference
        if ref.startswith("#") or ref.startswith("_TEMPLATE"):
            continue
        all_refs.append(ref)
        sheet_name = Path(rec.sheet_path).name
        value = rec.value or ""
        footprint = rec.properties.get("Footprint", "")
        lcsc = rec.properties.get("LCSC", "")
        mpn = rec.properties.get("MPN", "")

        if not footprint:
            missing_footprint.append({"sheet": sheet_name, "ref": ref, "value": value})
        else:
            # Check resolvability for project-local libraries.
            if ":" in footprint:
                lib_name, fp_name = footprint.split(":", 1)
                if lib_name in local_libs:
                    fp_file = local_libs[lib_name] / f"{fp_name}.kicad_mod"
                    if not fp_file.exists():
                        unresolvable_footprint.append(
                            {
                                "sheet": sheet_name,
                                "ref": ref,
                                "footprint": footprint,
                                "reason": f"file not found: {fp_file}",
                            }
                        )

        if not lcsc:
            missing_lcsc.append({"sheet": sheet_name, "ref": ref, "value": value})
        if not mpn:
            missing_mpn.append({"sheet": sheet_name, "ref": ref, "value": value})

    retroactive_gap: List[Dict[str, str]] = []
    on_pcb: Set[str] = set()
    if board_path:
        on_pcb = _footprint_files_on_pcb(Path(board_path))
        if on_pcb:
            missing_footprint_refs = {r["ref"] for r in missing_footprint}
            unresolvable_refs = {r["ref"] for r in unresolvable_footprint}
            for ref in all_refs:
                if ref in missing_footprint_refs or ref in unresolvable_refs:
                    continue  # already flagged elsewhere
                if ref not in on_pcb:
                    retroactive_gap.append({"ref": ref})

    warnings: List[str] = []
    if retroactive_gap and on_pcb:
        warnings.append(
            f"{len(retroactive_gap)} symbol(s) have Footprint set but are NOT on the "
            f".kicad_pcb yet — sync_schematic_to_board only auto-imports on a fresh "
            f"sync.  To pick them up: reset the .kicad_pcb to its template state "
            f"(e.g. `git checkout` if tracked, otherwise recreate empty) and rerun "
            f"sync_schematic_to_board."
        )

    ready = (
        not missing_footprint
        and not unresolvable_footprint
        and not retroactive_gap
    )

    return {
        "success": True,
        "ready": ready,
        "counts": {
            "total": len(all_refs),
            "missing_footprint": len(missing_footprint),
            "missing_lcsc": len(missing_lcsc),
            "missing_mpn": len(missing_mpn),
            "unresolvable_footprint": len(unresolvable_footprint),
            "retroactive_sync_gap": len(retroactive_gap),
            "on_pcb": len(on_pcb),
        },
        "missing_footprint": missing_footprint,
        "missing_lcsc": missing_lcsc,
        "missing_mpn": missing_mpn,
        "unresolvable_footprint": unresolvable_footprint,
        "retroactive_sync_gap": retroactive_gap,
        "warnings": warnings,
    }
