"""Hierarchical annotation for multi-sheet KiCad projects.

Kicad-cli's BOM/netlist tools warn `schematic has annotation errors, please
use the schematic editor to fix them` when two sheets both use the same
local reference (e.g. every sub-sheet's first IC becomes U1).  The KiCad
GUI's stock annotator disambiguates by renumbering — R1, R2, R3, ... —
which is unstable under sheet re-instantiation.  We follow the convention
the human uses: suffix every ref with `_{SHEETNAME}{instance_num}`.

  R3 on bms.kicad_sch (used once)         → R3_BMS1
  R3 on bms.kicad_sch (re-instantiated)   → R3_BMS1 and R3_BMS2

Both the local `(property "Reference")` field and every
`(instances (project (path (reference ...))))` entry are rewritten.  If a
sub-sheet is instantiated multiple times, the local Reference falls back
to the base name (template) — KiCad reads the per-instance path's
reference as the source of truth, and each path gets its own suffix.

The pass is idempotent: refs already ending in the recognised suffix
pattern are stripped back to the base before re-suffixing, so re-running
after adding new components doesn't produce `R3_BMS1_BMS1`.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("kicad_interface")


_REF_SPLIT = re.compile(r"^(?P<prefix>[A-Za-z_]+)(?P<num>\d+)$")
_SUFFIX = re.compile(r"^(?P<base>[A-Za-z_]+\d+)_(?P<sheet>[A-Z][A-Z0-9_]*?)(?P<idx>\d+)$")


def _strip_suffix(ref: str) -> str:
    """Return `ref` with any trailing `_{SHEETNAME}{N}` removed."""
    m = _SUFFIX.match(ref)
    return m.group("base") if m else ref


def _sanitize_sheetname(name: str) -> str:
    """Normalise a sheet name for use as a ref suffix: uppercase, replace
    non-alphanumeric with underscore, strip leading/trailing underscores.
    Empty → "SHEET".  Leading digit → prepend "S".
    """
    s = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()
    if not s:
        return "SHEET"
    if s[0].isdigit():
        return "S" + s
    return s


def _get_root_uuid(sch_path: Path) -> str:
    content = sch_path.read_text(encoding="utf-8")
    m = re.search(r'\(uuid\s+"?([0-9a-fA-F-]+)"?\)', content)
    if not m:
        raise ValueError(f"no root uuid in {sch_path}")
    return m.group(1)


def _find_sheet_instances_in_root(root_sch_path: Path) -> Dict[str, Tuple[str, str]]:
    """Parse `root_sch_path` and return
    `{sheet_uuid_in_root: (sheetname, sheetfile)}` for every `(sheet ...)`
    block in the root.  Sub-sheets that themselves host `(sheet ...)` blocks
    aren't followed here — this pass only handles direct children of the
    root, which matches how our current designs are laid out.  Multi-level
    hierarchies would need a recursive extension.
    """
    import sexpdata

    Sym = sexpdata.Symbol

    result: Dict[str, Tuple[str, str]] = {}
    content = root_sch_path.read_text(encoding="utf-8")
    sexp = sexpdata.loads(content)

    for node in sexp:
        if not (isinstance(node, list) and node and node[0] == Sym("sheet")):
            continue
        this_uuid, sheetname, sheetfile = None, None, None
        for sub in node[1:]:
            if not isinstance(sub, list):
                continue
            head = sub[0]
            if head == Sym("uuid") and len(sub) >= 2:
                this_uuid = str(sub[1]).strip('"')
            if head == Sym("property") and len(sub) >= 3:
                pname = str(sub[1]).strip('"')
                pval = str(sub[2]).strip('"')
                if pname == "Sheetname":
                    sheetname = pval
                elif pname == "Sheetfile":
                    sheetfile = pval
        if this_uuid and sheetname and sheetfile:
            result[this_uuid] = (sheetname, sheetfile)
    return result


def _rewrite_sub_sheet(
    sub_path: Path,
    sheet_uuid_map: Dict[str, Tuple[str, int]],
    renamed: List[Dict[str, str]],
) -> None:
    """Rewrite refs in one sub-sheet file.

    `sheet_uuid_map` maps `sheet_uuid_in_root → (SHEETNAME_normalised,
    instance_index)` for every root-level instantiation of THIS sub-sheet.
    A single-instance sub-sheet has one entry; a re-used one has two or
    more.
    """
    from commands.schematic import SchematicManager

    sch = SchematicManager.load_schematic(str(sub_path))
    if sch is None:
        logger.warning("Could not load sub-sheet for hierarchical annotate: %s", sub_path)
        return

    is_single_instance = len(sheet_uuid_map) == 1
    (single_sheetname, single_idx) = next(iter(sheet_uuid_map.values())) if is_single_instance else ("", 0)

    for symbol in sch.symbol:
        if not hasattr(symbol.property, "Reference"):
            continue
        current_local_ref = symbol.property.Reference.value
        if current_local_ref.startswith("#") or current_local_ref.startswith("_TEMPLATE"):
            continue
        base = _strip_suffix(current_local_ref)

        if not hasattr(symbol, "instances"):
            continue

        # Rewrite each (project (path (reference ...))) entry.
        for project in symbol.instances.getElementsByEntityType("project"):
            for path in project.getElementsByEntityType("path"):
                path_str = path.value if hasattr(path, "value") else ""
                parts = path_str.strip("/").split("/")
                sheet_uuid_in_root = parts[-1] if parts else ""
                if sheet_uuid_in_root not in sheet_uuid_map:
                    # Path from a different project (e.g. schematic used
                    # in multiple .kicad_pro).  Leave alone.
                    continue
                sheetname_norm, idx = sheet_uuid_map[sheet_uuid_in_root]
                new_inst_ref = f"{base}_{sheetname_norm}{idx}"
                for r in path.getElementsByEntityType("reference"):
                    old = r.value
                    if old != new_inst_ref:
                        r.value = new_inst_ref
                        renamed.append(
                            {
                                "sheet": str(sub_path.name),
                                "oldRef": old,
                                "newRef": new_inst_ref,
                                "sheetUuidInRoot": sheet_uuid_in_root,
                            }
                        )

        # Local (property "Reference"): if only one instance, mirror it;
        # otherwise keep the base as the template.
        new_local = (
            f"{base}_{single_sheetname}{single_idx}" if is_single_instance else base
        )
        if symbol.property.Reference.value != new_local:
            symbol.property.Reference.value = new_local

    SchematicManager.save_schematic(sch, str(sub_path))


def hierarchical_disambiguate(root_sch_path: str) -> Dict[str, Any]:
    """Suffix every sub-sheet symbol's ref with `_{SHEETNAME}{instance_num}`.
    Idempotent.

    Returns `{success, message, renamed: [{sheet, oldRef, newRef,
    sheetUuidInRoot}, ...]}`.
    """
    root = Path(root_sch_path).resolve()
    if not root.exists():
        return {"success": False, "message": f"Root schematic not found: {root}"}

    try:
        _get_root_uuid(root)
    except ValueError as e:
        return {"success": False, "message": str(e)}

    sheet_instances = _find_sheet_instances_in_root(root)
    if not sheet_instances:
        return {
            "success": True,
            "message": "Schematic has no sub-sheets; no hierarchical disambiguation needed.",
            "renamed": [],
        }

    # Group root-level instances by their sheetfile so we can number
    # them stably (by sheet_uuid_in_root ascending).
    by_file: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    for sheet_uuid, (name, fname) in sheet_instances.items():
        by_file[fname].append((sheet_uuid, name))

    # sheet_uuid_in_root → (SHEETNAME_normalised, instance_index, sheetfile)
    inst_map: Dict[str, Tuple[str, int, str]] = {}
    for fname, entries in by_file.items():
        entries.sort(key=lambda x: x[0])
        for i, (sheet_uuid, name) in enumerate(entries, start=1):
            inst_map[sheet_uuid] = (_sanitize_sheetname(name), i, fname)

    renamed: List[Dict[str, str]] = []
    processed_files: set = set()
    for sheet_uuid, (_sn, _i, fname) in inst_map.items():
        if fname in processed_files:
            continue
        processed_files.add(fname)
        sub_path = (root.parent / fname).resolve()
        if not sub_path.exists():
            logger.warning("Sub-sheet file missing during hier-annotate: %s", sub_path)
            continue
        sub_map: Dict[str, Tuple[str, int]] = {
            u: (inst_map[u][0], inst_map[u][1])
            for u in inst_map
            if inst_map[u][2] == fname
        }
        _rewrite_sub_sheet(sub_path, sub_map, renamed)

    return {
        "success": True,
        "message": (
            f"Hierarchical disambiguation: rewrote {len(renamed)} ref(s) "
            f"across {len(processed_files)} sub-sheet(s)."
        ),
        "renamed": renamed,
    }
