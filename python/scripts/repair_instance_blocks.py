"""
Repair malformed (instances ...) blocks in .kicad_sch files written by older
versions of KiCAD-MCP-Server's add_schematic_component.

Old (broken) form:
    (instances
      (project "project"
        (path "/"
          (reference "R1") (unit 1))))

KiCad keys per-project annotation off (project "<name>") (path "/<root_sheet_uuid>");
literal "project" / "/" never match the open project, so KiCad shows everything as
"R?", "SW?", etc. This script rewrites the literals to the correct values.

Usage:
    python repair_instance_blocks.py <schematic1.kicad_sch> [<schematic2.kicad_sch> ...]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def repair_schematic(sch_path: Path) -> tuple[bool, str]:
    """Returns (changed, message)."""
    content = sch_path.read_text(encoding="utf-8")

    root_uuid_match = re.search(r"\(uuid\s+\"?([0-9a-fA-F-]+)\"?\)", content)
    if not root_uuid_match:
        return False, f"{sch_path}: no root UUID found, skipping"
    root_uuid = root_uuid_match.group(1)
    project_name = sch_path.stem

    pattern = re.compile(
        r'\(project\s+"project"\s+\(path\s+"/"',
        re.MULTILINE,
    )
    replacement = f'(project "{project_name}" (path "/{root_uuid}"'
    new_content, n = pattern.subn(replacement, content)

    if n == 0:
        return False, f"{sch_path}: no broken instance blocks found"

    sch_path.write_text(new_content, encoding="utf-8")
    return True, f"{sch_path}: repaired {n} instance block(s) -> project={project_name!r}, path=/{root_uuid}"


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    rc = 0
    for arg in argv:
        path = Path(arg)
        if not path.exists():
            print(f"{path}: not found", file=sys.stderr)
            rc = 1
            continue
        try:
            _changed, msg = repair_schematic(path)
            print(msg)
        except Exception as exc:
            print(f"{path}: error: {exc}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
