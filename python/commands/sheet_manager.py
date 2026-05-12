"""
Sheet manager for hierarchical KiCad schematics.

Adds a sheet block on a parent .kicad_sch that references a child
.kicad_sch file.  The complement of `add_schematic_hierarchical_label`
(which goes on the child) and `add_sheet_pin` (which goes on the
parent's sheet block to expose hierarchical labels at the parent level).
The sheet block this manager creates is what gets a sheet pin attached
to it later.

Why this is its own tool: the schema is rigid (KiCad expects exactly
the right S-expression structure), and computing the parent's
project name + root UUID + next page number is a non-trivial step
that scripts should not be required to redo.
"""
from __future__ import annotations

import logging
import re
import uuid as uuid_lib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import sexpdata
from sexpdata import Symbol

logger = logging.getLogger("kicad_interface")


def _project_name_from_path(schematic_path: Path) -> str:
    """Project name = the stem of the schematic file.  Matches what
    DynamicSymbolLoader.create_component_instance derives, so the
    `(instances (project ...))` block on the new sheet matches the
    project block KiCad writes for placed symbols."""
    return schematic_path.stem


def _parent_root_uuid(content: str) -> Optional[str]:
    """Return the parent schematic's root UUID — the value of the
    top-level `(uuid ...)` declaration that KiCad uses as the root
    sheet ID."""
    m = re.search(r"\(uuid\s+\"?([0-9a-fA-F-]+)\"?\)", content)
    return m.group(1) if m else None


def _existing_sheet_count(content: str) -> int:
    """Count placed `(sheet ...)` blocks already on the parent.  Used
    to assign the next page number ('1' is the root, '2' is first sub
    sheet, etc.)."""
    # Match top-level "(sheet ..." entries.  We match the start of a
    # sheet block — KiCad puts each on its own indentation, but the
    # safest match is "(sheet" preceded by whitespace and followed by
    # whitespace or "(" to disambiguate from "(sheet_instances".
    return len(re.findall(r"(?<!\w)\(sheet[ \n]", content))


def _build_sheet_sexp(
    *,
    sheet_name: str,
    sheet_file: str,
    x: float,
    y: float,
    width: float,
    height: float,
    project_name: str,
    root_uuid: str,
    page: str,
    sheet_uuid: Optional[str] = None,
) -> str:
    """Format a sheet block S-expression.  Returns the text to insert.

    The block is generated as text (not via sexpdata) so we can match
    KiCad's preferred indentation and field ordering.  ``page`` is a
    string like ``"2"``.
    """
    sheet_uuid = sheet_uuid or str(uuid_lib.uuid4())
    name_y = y - 0.2  # name above the sheet (KiCad convention)
    file_y = y + height + 0.2  # file path below the sheet
    return (
        f'  (sheet (at {x} {y}) (size {width} {height}) (fields_autoplaced yes)\n'
        f'    (stroke (width 0.1524) (type solid))\n'
        f'    (fill (color 0 0 0 0.0000))\n'
        f'    (uuid {sheet_uuid})\n'
        f'    (property "Sheetname" "{sheet_name}" (at {x} {name_y} 0)\n'
        f'      (effects (font (size 1.27 1.27)) (justify left bottom))\n'
        f'    )\n'
        f'    (property "Sheetfile" "{sheet_file}" (at {x} {file_y} 0)\n'
        f'      (effects (font (size 1.27 1.27)) (justify left top))\n'
        f'    )\n'
        f'    (instances\n'
        f'      (project "{project_name}"\n'
        f'        (path "/{root_uuid}"\n'
        f'          (page "{page}")\n'
        f'        )\n'
        f'      )\n'
        f'    )\n'
        f'  )\n'
    )


def add_sheet_block(
    schematic_path: Path,
    sheet_name: str,
    sheet_file: str,
    *,
    x: float,
    y: float,
    width: float = 25.4,
    height: float = 25.4,
    page: Optional[str] = None,
    sheet_uuid: Optional[str] = None,
) -> Dict[str, Any]:
    """Add a hierarchical sheet block to *schematic_path*.

    ``sheet_file`` is the child .kicad_sch's path **as written into
    the Sheetfile property** — KiCad resolves it relative to the
    parent schematic's directory, so a bare filename like ``"bms.kicad_sch"``
    is the typical input when both files live in the same folder.

    ``x``, ``y``, ``width``, ``height`` define the sheet rectangle in
    mm at the parent.  Default size 25.4 × 25.4 mm.

    ``page`` defaults to the next available page number based on the
    sheets already present.

    Returns a dict with keys:
        success, message, sheet_uuid, page, project, root_uuid
    """
    if not schematic_path.exists():
        return {"success": False, "message": f"Schematic not found: {schematic_path}"}

    content = schematic_path.read_text(encoding="utf-8")

    # Refuse if a sheet with the same name already exists — silently
    # adding a duplicate would create a confusing schematic.
    if re.search(
        r'\(property\s+"Sheetname"\s+"' + re.escape(sheet_name) + r'"', content
    ):
        return {
            "success": False,
            "message": f"sheet '{sheet_name}' already exists in {schematic_path.name}",
        }

    root_uuid = _parent_root_uuid(content)
    if root_uuid is None:
        return {
            "success": False,
            "message": "could not find root (uuid ...) in parent schematic",
        }

    project_name = _project_name_from_path(schematic_path)
    if page is None:
        # Page numbering: root is "1".  Each additional sheet is "2", "3", ...
        page = str(_existing_sheet_count(content) + 2)

    sheet_text = _build_sheet_sexp(
        sheet_name=sheet_name,
        sheet_file=sheet_file,
        x=x,
        y=y,
        width=width,
        height=height,
        project_name=project_name,
        root_uuid=root_uuid,
        page=page,
        sheet_uuid=sheet_uuid,
    )

    # Insert before the (sheet_instances ...) block (the last top-level
    # block before the closing paren).  If no sheet_instances block
    # exists, insert before the final ").
    sheet_instances_match = re.search(r"(?ms)^[ \t]*\(sheet_instances\b", content)
    if sheet_instances_match:
        insert_pos = sheet_instances_match.start()
    else:
        # Fallback: insert before the last closing paren.
        insert_pos = content.rfind(")")
        if insert_pos < 0:
            return {
                "success": False,
                "message": "malformed schematic: no closing paren",
            }

    new_content = content[:insert_pos] + sheet_text + content[insert_pos:]
    schematic_path.write_text(new_content, encoding="utf-8")

    return {
        "success": True,
        "message": (
            f"Added sheet '{sheet_name}' → '{sheet_file}' on "
            f"{schematic_path.name} as page {page}"
        ),
        "sheet_uuid": sheet_uuid or _extract_uuid_from_text(sheet_text),
        "page": page,
        "project": project_name,
        "root_uuid": root_uuid,
    }


def _extract_uuid_from_text(sheet_text: str) -> Optional[str]:
    m = re.search(r"\(uuid\s+([0-9a-fA-F-]+)\)", sheet_text)
    return m.group(1) if m else None
