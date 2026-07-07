"""Schematic-wide metadata storage via a singleton symbol (#230).

Convention
==========

Project-wide metadata that used to live in `.kicad_pro` as
`mcp_constraint_version`, `mcp_spring_classes`, and
`mcp_expected_netclass_patterns` is now authored on a singleton
**`Schematic_Metadata`** symbol placed on the schematic. The user can
inspect and edit the metadata through eeschema's symbol-properties
dialog — no special tooling required.

The singleton is identified by a marker property:

    Schematic_Metadata_Marker = mcp/v1

so it survives reference renames. The symbol itself is inline-defined
in the schematic's `lib_symbols` section, has `in_bom no` and
`on_board no`, and carries the metadata as additional properties:

    Schematic_Metadata_Marker      = mcp/v1
    mcp_constraint_version         = 2
    mcp_spring_classes             = { ... JSON blob ... }
    mcp_expected_netclass_patterns = { ... JSON blob ... }

Multiple singletons
-------------------

If a schematic contains more than one symbol bearing the marker
property (e.g. user duplicated by mistake), `read_metadata` merges
their properties in deterministic order: alphabetical by
Reference, last-wins for duplicate keys. A warning is logged listing
the affected references. `write_metadata_key` always targets the
*first* singleton (alphabetical by Reference).

Zero singletons
---------------

`read_metadata` returns an empty dict (callers apply their own
defaults). `write_metadata_key` will create a singleton on first call
— see `_ensure_singleton`.

"""
from __future__ import annotations

import json
import logging
import uuid as _uuid_mod
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import sexpdata

logger = logging.getLogger(__name__)


MARKER_PROPERTY = "Schematic_Metadata_Marker"
MARKER_VALUE = "mcp/v1"
SINGLETON_LIB_ID = "mcp_meta:Schematic_Metadata"
SINGLETON_VALUE = "mcp/v1"
SINGLETON_REFERENCE_PREFIX = "META"

# Properties that are not "metadata" in the user-facing sense — these
# are part of the symbol mechanics (Reference, Value) or the marker
# itself, and shouldn't be returned as metadata or written through
# write_metadata_key.
_NON_METADATA_PROPERTIES = frozenset({
    "Reference", "Value", "Footprint", "Datasheet", "Description",
    MARKER_PROPERTY,
})


# ---------------------------------------------------------------------------
# s-expr helpers
# ---------------------------------------------------------------------------


def _is_sym(x: Any, name: str) -> bool:
    return isinstance(x, sexpdata.Symbol) and x.value() == name


def _node_name(node: Any) -> Optional[str]:
    if isinstance(node, list) and node and isinstance(node[0], sexpdata.Symbol):
        return node[0].value()
    return None


def _find_child(node: list, name: str) -> Optional[list]:
    for child in node[1:] if isinstance(node, list) else []:
        if _node_name(child) == name:
            return child
    return None


def _find_children(node: list, name: str) -> List[list]:
    return [
        c for c in (node[1:] if isinstance(node, list) else [])
        if _node_name(c) == name
    ]


def _str_arg(node: list, idx: int = 1) -> Optional[str]:
    """Return the idx-th element of node coerced to str, or None."""
    if not isinstance(node, list) or len(node) <= idx:
        return None
    v = node[idx]
    if isinstance(v, sexpdata.Symbol):
        return v.value()
    return str(v)


def _load(sch_path: Path) -> list:
    return sexpdata.loads(Path(sch_path).read_text(encoding="utf-8"))


# We do NOT serialise the modified s-expr back through sexpdata —
# sexpdata.dumps doesn't match KiCad's formatting (KiCad puts the
# leading symbol on the same line as the opening paren, uses tab
# indent, etc.) and kicad-cli refuses the round-tripped file. All
# mutations therefore operate on the source TEXT, using sexpdata
# only to parse-and-locate.


# ---------------------------------------------------------------------------
# Singleton discovery + read
# ---------------------------------------------------------------------------


def _symbol_properties(sym: list) -> Dict[str, str]:
    """Return all (name, value) properties of a (symbol …) node as a dict."""
    out: Dict[str, str] = {}
    for prop in _find_children(sym, "property"):
        name = _str_arg(prop, 1)
        value = _str_arg(prop, 2)
        if name is not None and value is not None:
            out[name] = value
    return out


def _symbol_is_singleton(sym: list) -> bool:
    """True iff this (symbol …) instance is a metadata singleton."""
    props = _symbol_properties(sym)
    return props.get(MARKER_PROPERTY) == MARKER_VALUE


def find_singletons(sch_path: Path) -> List[Dict[str, Any]]:
    """Return all metadata-singleton instances in the schematic.

    Each entry: {
      "reference": "META1",
      "uuid": "...",
      "properties": {key: value, ...}  # ALL props including marker/reference
    }
    """
    sch = _load(sch_path)
    out: List[Dict[str, Any]] = []
    for sym in _find_children(sch, "symbol"):
        if not _symbol_is_singleton(sym):
            continue
        props = _symbol_properties(sym)
        uuid_node = _find_child(sym, "uuid")
        out.append({
            "reference": props.get("Reference", ""),
            "uuid": _str_arg(uuid_node, 1) if uuid_node else "",
            "properties": props,
        })
    # Deterministic order: alphabetical by reference.
    out.sort(key=lambda e: e["reference"])
    return out


def read_metadata(sch_path: Path) -> Dict[str, str]:
    """Return the merged metadata dict from all singletons on the
    schematic.

    Excludes non-metadata properties (Reference/Value/etc + the marker
    itself). On >1 singleton: merges alphabetically by reference,
    last-wins for duplicate keys, logs a warning naming the
    references. On 0 singletons: returns {} (caller applies defaults).
    """
    singletons = find_singletons(sch_path)
    if not singletons:
        return {}
    if len(singletons) > 1:
        refs = [s["reference"] for s in singletons]
        logger.warning(
            "Schematic_Metadata: found %d singletons (%s); merging "
            "alphabetically by reference, last-wins for duplicate keys. "
            "Consider consolidating into a single instance.",
            len(singletons), ", ".join(refs),
        )
    merged: Dict[str, str] = {}
    for s in singletons:
        for k, v in s["properties"].items():
            if k in _NON_METADATA_PROPERTIES:
                continue
            merged[k] = v
    return merged


def read_metadata_json(sch_path: Path, key: str) -> Optional[Any]:
    """Read a metadata key whose value is JSON-encoded; return the
    parsed object, or None if missing or malformed."""
    raw = read_metadata(sch_path).get(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(
            "Schematic_Metadata: key %r is not valid JSON (%s); "
            "ignoring.", key, e,
        )
        return None


# ---------------------------------------------------------------------------
# Singleton creation + write (text-based for KiCad round-trip safety)
# ---------------------------------------------------------------------------


_SINGLETON_LIB_DEFINITION = '''\t\t(symbol "mcp_meta:Schematic_Metadata"
\t\t\t(pin_names
\t\t\t\t(offset 0)
\t\t\t\t(hide yes)
\t\t\t)
\t\t\t(exclude_from_sim yes)
\t\t\t(in_bom no)
\t\t\t(on_board no)
\t\t\t(property "Reference" "META"
\t\t\t\t(at 0 5.08 0)
\t\t\t\t(effects
\t\t\t\t\t(font
\t\t\t\t\t\t(size 1.27 1.27)
\t\t\t\t\t)
\t\t\t\t)
\t\t\t)
\t\t\t(property "Value" "mcp/v1"
\t\t\t\t(at 0 -5.08 0)
\t\t\t\t(effects
\t\t\t\t\t(font
\t\t\t\t\t\t(size 1.27 1.27)
\t\t\t\t\t)
\t\t\t\t)
\t\t\t)
\t\t\t(property "Footprint" ""
\t\t\t\t(at 0 0 0)
\t\t\t\t(effects
\t\t\t\t\t(font
\t\t\t\t\t\t(size 1.27 1.27)
\t\t\t\t\t)
\t\t\t\t\t(hide yes)
\t\t\t\t)
\t\t\t)
\t\t\t(property "Datasheet" ""
\t\t\t\t(at 0 0 0)
\t\t\t\t(effects
\t\t\t\t\t(font
\t\t\t\t\t\t(size 1.27 1.27)
\t\t\t\t\t)
\t\t\t\t\t(hide yes)
\t\t\t\t)
\t\t\t)
\t\t\t(property "Description" "Project-wide MCP metadata (singleton). Holds mcp_* keys formerly in .kicad_pro. Not placed on the PCB."
\t\t\t\t(at 0 0 0)
\t\t\t\t(effects
\t\t\t\t\t(font
\t\t\t\t\t\t(size 1.27 1.27)
\t\t\t\t\t)
\t\t\t\t\t(hide yes)
\t\t\t\t)
\t\t\t)
\t\t\t(symbol "Schematic_Metadata_0_1"
\t\t\t\t(rectangle
\t\t\t\t\t(start -5.08 -2.54)
\t\t\t\t\t(end 5.08 2.54)
\t\t\t\t\t(stroke
\t\t\t\t\t\t(width 0.254)
\t\t\t\t\t\t(type default)
\t\t\t\t\t)
\t\t\t\t\t(fill
\t\t\t\t\t\t(type none)
\t\t\t\t\t)
\t\t\t\t)
\t\t\t)
\t\t)
'''


def _find_matching_close(text: str, open_idx: int) -> int:
    """Given the index of an `(` in `text`, return the index of the
    matching `)`. Respects strings (double-quoted) and escape sequences.
    """
    depth = 0
    i = open_idx
    in_string = False
    while i < len(text):
        ch = text[i]
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    raise ValueError("unbalanced parens; can't find matching ')'")


def _find_lib_symbols_close(text: str) -> Optional[int]:
    """Return the index of the `)` that closes the top-level
    `(lib_symbols ...)` block, or None if not found.

    KiCad uses tab indentation by default (`\t(lib_symbols`), but
    schematics that have been round-tripped through sexpdata or hand
    tools may use spaces.  Try both — either indentation is valid.
    """
    for needle in ("\t(lib_symbols", "  (lib_symbols", "\n(lib_symbols"):
        pos = text.find(needle)
        if pos >= 0:
            open_paren = text.index("(", pos)
            return _find_matching_close(text, open_paren)
    return None


def _find_kicad_sch_close(text: str) -> int:
    """Return the index of the final `)` closing `(kicad_sch ...)`."""
    open_paren = text.index("(")  # first (
    return _find_matching_close(text, open_paren)


def _has_singleton_lib(text: str) -> bool:
    return f'"{SINGLETON_LIB_ID}"' in text


def _has_singleton_instance(text: str) -> bool:
    """True iff a (symbol …) block in the schematic carries our marker."""
    # Cheap check: look for the marker property string literal anywhere
    # inside a symbol-instance block. Could yield false positives if
    # MARKER_VALUE appears outside a marker property — namespace it
    # with the property name.
    return f'(property "{MARKER_PROPERTY}" "{MARKER_VALUE}"' in text


def _build_singleton_instance_text(
    properties: Dict[str, str],
    position: Tuple[float, float],
    project_name: str,
    sheet_uuid: str,
    new_uuid: str,
) -> str:
    """Construct the (symbol …) instance text block for the singleton."""
    x, y = position
    # Property order: Reference, Value, Footprint, Datasheet,
    # Description first (KiCad standard), then marker, then extras.
    prop_order = [
        "Reference", "Value", "Footprint", "Datasheet", "Description",
        MARKER_PROPERTY,
    ]
    for extra in sorted(properties):
        if extra not in prop_order:
            prop_order.append(extra)

    lines: List[str] = []
    lines.append("\t(symbol")
    lines.append(f'\t\t(lib_id "{SINGLETON_LIB_ID}")')
    lines.append(f"\t\t(at {x} {y} 0)")
    lines.append("\t\t(unit 1)")
    lines.append("\t\t(exclude_from_sim yes)")
    lines.append("\t\t(in_bom no)")
    lines.append("\t\t(on_board no)")
    lines.append("\t\t(dnp no)")
    lines.append(f'\t\t(uuid "{new_uuid}")')
    for name in prop_order:
        value = properties.get(name, "")
        value_escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        # Reference + Value are visible; the rest are hidden.
        hide_line = "" if name in ("Reference", "Value") else "\t\t\t\t(hide yes)\n"
        lines.append(f'\t\t(property "{name}" "{value_escaped}"')
        lines.append(f"\t\t\t(at {x} {y} 0)")
        lines.append("\t\t\t(effects")
        lines.append("\t\t\t\t(font")
        lines.append("\t\t\t\t\t(size 1.27 1.27)")
        lines.append("\t\t\t\t)")
        if hide_line:
            lines.append(hide_line.rstrip("\n"))
        lines.append("\t\t\t)")
        lines.append("\t\t)")
    lines.append("\t\t(instances")
    lines.append(f'\t\t\t(project "{project_name}"')
    lines.append(f'\t\t\t\t(path "/{sheet_uuid}"')
    lines.append(f'\t\t\t\t\t(reference "{properties.get("Reference", "META1")}")')
    lines.append("\t\t\t\t\t(unit 1)")
    lines.append("\t\t\t\t)")
    lines.append("\t\t\t)")
    lines.append("\t\t)")
    lines.append("\t)")
    return "\n".join(lines) + "\n"


def _ensure_singleton_text(sch_path: Path) -> Tuple[str, str, bool]:
    """Return (modified_text, singleton_ref, created_bool).

    Does not write to disk. Mutates the in-memory text only.
    """
    text = Path(sch_path).read_text(encoding="utf-8")
    if _has_singleton_instance(text):
        # Already exists — find its Reference via the parsed model.
        sch = sexpdata.loads(text)
        for sym in _find_children(sch, "symbol"):
            if _symbol_is_singleton(sym):
                ref = _symbol_properties(sym).get("Reference", "")
                return text, ref, False
        # Marker text was present but no parsed singleton — odd; fall
        # through and treat as no singleton.

    # 1. Insert the lib_symbols definition if missing.
    if not _has_singleton_lib(text):
        close_idx = _find_lib_symbols_close(text)
        if close_idx is None:
            return text, "", False  # no lib_symbols section to extend
        # Insert before the closing `)`. The previous character is the
        # end of the last symbol; we want our block to sit indented to
        # match.
        insertion = _SINGLETON_LIB_DEFINITION
        text = text[:close_idx] + insertion + "\t" + text[close_idx:]

    # 2. Pick a Reference. Walk parsed model to find taken refs.
    sch = sexpdata.loads(text)
    existing_refs = set()
    for sym in _find_children(sch, "symbol"):
        ref = _symbol_properties(sym).get("Reference", "")
        if ref:
            existing_refs.add(ref)
    ref_idx = 1
    while f"{SINGLETON_REFERENCE_PREFIX}{ref_idx}" in existing_refs:
        ref_idx += 1
    new_ref = f"{SINGLETON_REFERENCE_PREFIX}{ref_idx}"

    # 3. Find the sheet uuid + project name for the (instances) block.
    project_name = sch_path.stem
    root_uuid_node = _find_child(sch, "uuid")
    sheet_uuid = (
        _str_arg(root_uuid_node, 1)
        if root_uuid_node else "00000000-0000-0000-0000-000000000000"
    )

    initial_props = {
        "Reference": new_ref,
        "Value": SINGLETON_VALUE,
        "Footprint": "",
        "Datasheet": "",
        "Description": "Project-wide MCP metadata (singleton).",
        MARKER_PROPERTY: MARKER_VALUE,
    }
    instance_text = _build_singleton_instance_text(
        initial_props, position=(12.7, 12.7),
        project_name=project_name, sheet_uuid=sheet_uuid,
        new_uuid=str(_uuid_mod.uuid4()),
    )

    # 4. Insert the instance just before the final `)` of the
    #    top-level (kicad_sch …).
    close_idx = _find_kicad_sch_close(text)
    text = text[:close_idx] + instance_text + text[close_idx:]
    logger.info(
        "Schematic_Metadata: created singleton '%s'.", new_ref,
    )
    return text, new_ref, True


def _set_property_in_text(text: str, symbol_ref: str, key: str, value: str) -> str:
    """Update or append `(property "key" "value")` inside the
    (symbol …) block whose Reference equals symbol_ref. Returns the
    modified text.
    """
    # Find the singleton's symbol block by walking parens looking for
    # one whose Reference property matches symbol_ref.
    sch = sexpdata.loads(text)
    target_sym = None
    for sym in _find_children(sch, "symbol"):
        if _symbol_properties(sym).get("Reference") == symbol_ref:
            target_sym = sym
            break
    if target_sym is None:
        raise RuntimeError(
            f"can't find symbol with Reference={symbol_ref!r}"
        )
    # Locate the symbol block in the text by scanning for the uuid
    # (unique identifier we can grep for).
    uuid_node = _find_child(target_sym, "uuid")
    if uuid_node is None:
        raise RuntimeError("singleton has no uuid")
    sym_uuid = _str_arg(uuid_node, 1)
    uuid_marker = f'(uuid "{sym_uuid}")'
    uuid_pos = text.find(uuid_marker)
    if uuid_pos < 0:
        raise RuntimeError(f"can't locate uuid {sym_uuid!r} in source")
    # Find the (symbol …) open-paren that contains this uuid: walk
    # backwards counting parens (we're inside a depth-1 scope when we
    # find the symbol's opening paren). Start ONE PAST the uuid's
    # opening paren — otherwise the first iteration matches that
    # paren itself and sets sym_open to the uuid block.
    depth = 0
    i = uuid_pos - 1
    in_string = False
    sym_open = -1
    while i >= 0:
        ch = text[i]
        # Naive — strings here would be uncommon in a backward scan
        # within a uuid block, but defend anyway.
        if ch == ")" and not in_string:
            depth += 1
        elif ch == "(" and not in_string:
            depth -= 1
            if depth < 0:
                sym_open = i
                break
        i -= 1
    if sym_open < 0:
        raise RuntimeError("couldn't find symbol-block opening paren")
    sym_close = _find_matching_close(text, sym_open)
    sym_block = text[sym_open:sym_close + 1]

    # Look for an existing property with this key inside the block.
    needle = f'(property "{key}" "'
    prop_idx = sym_block.find(needle)
    if prop_idx >= 0:
        # Replace the value in-place: find the start of the value
        # literal (right after the opening quote), find the closing
        # quote (respecting escapes), substitute.
        value_start = prop_idx + len(needle)
        j = value_start
        while j < len(sym_block):
            if sym_block[j] == "\\":
                j += 2
                continue
            if sym_block[j] == '"':
                break
            j += 1
        value_escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        new_block = (
            sym_block[:value_start] + value_escaped + sym_block[j:]
        )
    else:
        # Insert a new property block before the `(instances` block
        # (KiCad expects properties before instances).
        ins_marker = "\t\t(instances"
        ins_pos = sym_block.find(ins_marker)
        if ins_pos < 0:
            # No instances — insert before the closing `)`.
            ins_pos = len(sym_block) - sym_block[::-1].index(")") - 1
        value_escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        # Use the placement of the existing Value property's `(at …)`
        # as our `at` location.
        at_x, at_y, at_rot = 12.7, 12.7, 0
        prop_lines = [
            f'\t\t(property "{key}" "{value_escaped}"',
            f"\t\t\t(at {at_x} {at_y} {at_rot})",
            "\t\t\t(effects",
            "\t\t\t\t(font",
            "\t\t\t\t\t(size 1.27 1.27)",
            "\t\t\t\t)",
            "\t\t\t\t(hide yes)",
            "\t\t\t)",
            "\t\t)",
            "",
        ]
        insertion = "\n".join(prop_lines)
        new_block = sym_block[:ins_pos] + insertion + sym_block[ins_pos:]

    return text[:sym_open] + new_block + text[sym_close + 1:]


def write_metadata_key(
    sch_path: Path, key: str, value: Any,
) -> Dict[str, Any]:
    """Set a single metadata key on the schematic's singleton.

    Creates the singleton if absent. Object/array values are
    JSON-encoded. Returns {created: bool, reference: str}.
    """
    if key in _NON_METADATA_PROPERTIES:
        return {
            "success": False,
            "errorDetails": (
                f"key {key!r} is a reserved property name "
                f"({sorted(_NON_METADATA_PROPERTIES)})"
            ),
        }
    if not isinstance(value, str):
        value = json.dumps(value, separators=(",", ":"))
    sch_path = Path(sch_path)
    text, ref, created = _ensure_singleton_text(sch_path)
    text = _set_property_in_text(text, ref, key, value)
    sch_path.write_text(text, encoding="utf-8")
    return {
        "success": True,
        "created": created,
        "reference": ref,
        "key": key,
    }


