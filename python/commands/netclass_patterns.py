"""Verify and (optionally) restore netclass-pattern assignments in
``.kicad_pro``.

KiCAD's GUI has been observed to silently strip ``netclass_patterns``
entries on save when it re-formats the project file across version
upgrades (see commit ``14a143a`` on the power_module repo, which lost
the ``CELL1_TOP`` / ``CELL2_TOP`` -> ``POWER_4A`` patterns). Once
gone, those nets fall back to the Default netclass and routing tools
silently pick the wrong width.

The "expected" pattern list lives on the schematic's
Schematic_Metadata singleton under ``mcp_expected_netclass_patterns``
(#230). This module reads it from there and compares against the
live ``net_settings.netclass_patterns`` in ``.kicad_pro``; restore
writes back to ``.kicad_pro`` because that's what KiCad actually
consumes.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("kicad_interface")


MCP_EXPECTED_KEY = "mcp_expected_netclass_patterns"


def _read_kicad_pro(pro_path: Path) -> Optional[Dict[str, Any]]:
    if not pro_path.exists():
        return None
    try:
        return json.loads(pro_path.read_text())
    except json.JSONDecodeError as e:
        logger.warning(f"Could not parse {pro_path}: {e}")
        return None


def _write_kicad_pro(pro_path: Path, data: Dict[str, Any]) -> None:
    pro_path.write_text(json.dumps(data, indent=2))


def _current_patterns(data: Dict[str, Any]) -> List[Dict[str, str]]:
    """Pull the live netclass_patterns from net_settings."""
    return list(data.get("net_settings", {}).get("netclass_patterns", []))


def _normalise_patterns(
    patterns: List[Dict[str, str]],
) -> List[Tuple[str, str]]:
    """Reduce pattern dicts to (netclass, pattern) tuples, sorted, for
    set arithmetic. Anything missing one field is dropped."""
    out = []
    for p in patterns:
        nc = p.get("netclass")
        pat = p.get("pattern")
        if nc and pat:
            out.append((nc, pat))
    return sorted(set(out))


def _read_expected_patterns(
    sch_path: Path,
) -> Optional[List[Any]]:
    """Return the expected-patterns list from the schematic's
    Schematic_Metadata singleton, or None if the singleton doesn't
    carry the key (or doesn't exist yet).
    """
    if not Path(sch_path).exists():
        return None
    try:
        from commands.schematic_metadata import read_metadata_json
        return read_metadata_json(Path(sch_path), MCP_EXPECTED_KEY)
    except Exception as e:
        logger.warning(
            f"singleton read failed for {sch_path}: {e}"
        )
        return None


def _write_expected_patterns(
    sch_path: Path, patterns: List[Any],
) -> bool:
    """Write the expected-patterns list to the schematic singleton.
    Returns True on success.
    """
    try:
        from commands.schematic_metadata import write_metadata_key
        r = write_metadata_key(Path(sch_path), MCP_EXPECTED_KEY, patterns)
        return bool(r.get("success"))
    except Exception as e:
        logger.warning(
            f"singleton write raised {e} for {sch_path}"
        )
        return False


def bootstrap_expected_patterns(
    pro_path: Path, sch_path: Path,
) -> bool:
    """If the schematic singleton doesn't carry
    ``mcp_expected_netclass_patterns``, seed it from the current
    ``net_settings.netclass_patterns`` in ``.kicad_pro``. Returns True
    if something was written.
    """
    existing = _read_expected_patterns(sch_path)
    if existing is not None:
        return False
    data = _read_kicad_pro(pro_path)
    if data is None:
        return False
    current = _current_patterns(data)
    wrote = _write_expected_patterns(sch_path, list(current))
    if wrote:
        logger.info(
            f"Bootstrapped {MCP_EXPECTED_KEY} on schematic singleton "
            f"with {len(current)} entries"
        )
    return wrote


def verify_netclass_patterns(
    pro_path: Path,
    restore: bool = False,
    sch_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Compare the live ``netclass_patterns`` in ``.kicad_pro`` against
    the expected set stored on the schematic's Schematic_Metadata
    singleton. Optionally restore missing entries.

    ``sch_path`` is required — the singleton is the only canonical
    source. If omitted, it's derived from ``pro_path``'s sibling
    ``.kicad_sch``.

    Returns a dict with:
      success (bool)
      bootstrapped (bool) — true if the expected set was just
        initialised on this call by seeding the singleton from the
        current state (nothing to verify yet, no drift).
      drifted (bool) — true if any pattern is missing or unexpectedly
        extra.
      missing (list) — patterns in expected but not in current.
      extra (list) — patterns in current but not in expected.
      restored (list) — only present when restore=True; the entries
        that were added back to net_settings.netclass_patterns.
    """
    data = _read_kicad_pro(pro_path)
    if data is None:
        return {
            "success": False,
            "message": f"Could not read {pro_path}",
        }

    if sch_path is None:
        sch_path = pro_path.with_suffix(".kicad_sch")
    if not sch_path.exists():
        return {
            "success": False,
            "message": (
                f"Could not find sibling schematic {sch_path}; the "
                f"Schematic_Metadata singleton lives on it. Pass "
                f"sch_path= explicitly if the schematic is elsewhere."
            ),
        }

    expected_raw = _read_expected_patterns(sch_path)

    if expected_raw is None:
        # First-time encounter — seed the singleton from the current
        # net_settings.netclass_patterns.
        current = list(_current_patterns(data))
        wrote = _write_expected_patterns(sch_path, current)
        return {
            "success": wrote,
            "bootstrapped": wrote,
            "drifted": False,
            "missing": [],
            "extra": [],
            "message": (
                f"Seeded Schematic_Metadata singleton with "
                f"{len(current)} pattern(s) from current "
                f"net_settings.netclass_patterns."
                if wrote else
                f"Could not write expected patterns to schematic "
                f"singleton at {sch_path}."
            ),
        }

    expected_tuples = set(_normalise_patterns(expected_raw))
    current_list = _current_patterns(data)
    current_tuples = set(_normalise_patterns(current_list))

    missing = sorted(expected_tuples - current_tuples)
    extra = sorted(current_tuples - expected_tuples)
    drifted = bool(missing or extra)

    result: Dict[str, Any] = {
        "success": True,
        "bootstrapped": False,
        "drifted": drifted,
        "missing": [{"netclass": nc, "pattern": p} for nc, p in missing],
        "extra": [{"netclass": nc, "pattern": p} for nc, p in extra],
    }

    if restore and missing:
        # Append the missing entries back to net_settings.netclass_patterns.
        # We do not touch `extra` — those may be intentional new additions
        # the user wants to keep. Restoring just rebuilds the floor.
        # Restoration writes to .kicad_pro since that's where KiCad
        # reads the live netclass_patterns from.
        current_list.extend(
            {"netclass": nc, "pattern": p} for nc, p in missing
        )
        data.setdefault("net_settings", {})["netclass_patterns"] = current_list
        _write_kicad_pro(pro_path, data)
        result["restored"] = result["missing"]
        result["message"] = (
            f"Restored {len(missing)} missing pattern(s) to "
            f"net_settings.netclass_patterns."
        )
    elif drifted:
        result["message"] = (
            f"Netclass-pattern drift: {len(missing)} missing, "
            f"{len(extra)} extra. Pass restore=true to re-add missing."
        )
    else:
        result["message"] = (
            f"All {len(expected_tuples)} expected pattern(s) present."
        )

    return result
