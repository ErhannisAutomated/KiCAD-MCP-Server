"""Verify and (optionally) restore netclass-pattern assignments in
``.kicad_pro``.

KiCAD's GUI has been observed to silently strip ``netclass_patterns``
entries on save when it re-formats the project file across version
upgrades (see commit ``14a143a`` on the power_module repo, which lost
the ``CELL1_TOP`` / ``CELL2_TOP`` -> ``POWER_4A`` patterns). Once
gone, those nets fall back to the Default netclass and routing tools
silently pick the wrong width.

The "expected" pattern list is stored as ``mcp_expected_netclass_patterns``.
As of #230, the preferred home is the schematic's Schematic_Metadata
singleton — pass ``sch_path`` to read and write there. Legacy projects
that still keep the key in ``.kicad_pro`` are supported via fallback
(read) and via the ``migrate_metadata_to_singleton`` MCP tool (move).
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


def bootstrap_expected_patterns(pro_path: Path) -> bool:
    """If ``.kicad_pro`` lacks ``mcp_expected_netclass_patterns``, write
    one by copying the current ``net_settings.netclass_patterns``.
    Returns True if the file was modified."""
    data = _read_kicad_pro(pro_path)
    if data is None:
        return False
    if MCP_EXPECTED_KEY in data:
        return False
    current = _current_patterns(data)
    data[MCP_EXPECTED_KEY] = list(current)
    _write_kicad_pro(pro_path, data)
    logger.info(
        f"Bootstrapped {MCP_EXPECTED_KEY} in {pro_path.name} with "
        f"{len(current)} entries"
    )
    return True


def verify_netclass_patterns(
    pro_path: Path,
    restore: bool = False,
) -> Dict[str, Any]:
    """Compare the live ``netclass_patterns`` against the expected set
    stored in ``mcp_expected_netclass_patterns``. Optionally restore
    missing entries.

    Returns a dict with:
      success (bool)
      bootstrapped (bool) — true if the expected section was just
        initialised on this call (nothing to verify yet, no drift).
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

    if MCP_EXPECTED_KEY not in data:
        # First-time encounter — seed it from the current state.
        data[MCP_EXPECTED_KEY] = list(_current_patterns(data))
        _write_kicad_pro(pro_path, data)
        return {
            "success": True,
            "bootstrapped": True,
            "drifted": False,
            "missing": [],
            "extra": [],
            "message": (
                f"No {MCP_EXPECTED_KEY} section; seeded from current "
                f"net_settings.netclass_patterns "
                f"({len(data[MCP_EXPECTED_KEY])} entries)."
            ),
        }

    expected_tuples = set(_normalise_patterns(data[MCP_EXPECTED_KEY]))
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
