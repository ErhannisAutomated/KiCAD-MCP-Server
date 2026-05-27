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


def _read_expected_patterns(
    pro_path: Path, sch_path: Optional[Path] = None,
) -> Tuple[Optional[List[Any]], str]:
    """Return the expected-patterns list and the source it came from.

    Source precedence (#230 phase 2):
      1. Schematic_Metadata singleton's ``mcp_expected_netclass_patterns``
         property — if ``sch_path`` is given and the singleton has the
         key.
      2. The ``.kicad_pro``'s top-level ``mcp_expected_netclass_patterns``
         key — legacy storage for projects that haven't migrated.

    Returns ``(patterns, source)`` where source is "singleton",
    "pro", or "missing". Patterns is ``None`` only when missing.
    """
    if sch_path is not None and Path(sch_path).exists():
        try:
            from commands.schematic_metadata import read_metadata_json
            from_sch = read_metadata_json(Path(sch_path), MCP_EXPECTED_KEY)
            if from_sch is not None:
                return from_sch, "singleton"
        except Exception as e:
            logger.debug(
                f"singleton read failed for {sch_path}: {e}; "
                f"falling back to .kicad_pro"
            )
    data = _read_kicad_pro(pro_path)
    if data and MCP_EXPECTED_KEY in data:
        return data[MCP_EXPECTED_KEY], "pro"
    return None, "missing"


def _write_expected_patterns(
    pro_path: Path,
    sch_path: Optional[Path],
    patterns: List[Any],
    prefer_singleton: bool = True,
) -> str:
    """Write the expected-patterns list. Returns where it was stored:
    "singleton" or "pro".

    If a schematic path is given AND the singleton is the preferred
    sink (default), writes to the singleton. Otherwise updates
    ``.kicad_pro``.
    """
    if prefer_singleton and sch_path is not None and Path(sch_path).exists():
        try:
            from commands.schematic_metadata import write_metadata_key
            r = write_metadata_key(Path(sch_path), MCP_EXPECTED_KEY, patterns)
            if r.get("success"):
                return "singleton"
            logger.warning(
                f"singleton write failed ({r.get('errorDetails')}); "
                f"falling back to .kicad_pro"
            )
        except Exception as e:
            logger.warning(
                f"singleton write raised {e}; falling back to .kicad_pro"
            )
    data = _read_kicad_pro(pro_path) or {}
    data[MCP_EXPECTED_KEY] = patterns
    _write_kicad_pro(pro_path, data)
    return "pro"


def bootstrap_expected_patterns(
    pro_path: Path, sch_path: Optional[Path] = None,
) -> bool:
    """If neither the schematic singleton nor ``.kicad_pro`` carries
    ``mcp_expected_netclass_patterns``, seed it from the current
    ``net_settings.netclass_patterns`` and write to whichever source
    the project prefers (singleton when ``sch_path`` is given).
    Returns True if something was written.
    """
    existing, source = _read_expected_patterns(pro_path, sch_path)
    if existing is not None:
        return False
    data = _read_kicad_pro(pro_path)
    if data is None:
        return False
    current = _current_patterns(data)
    sink = _write_expected_patterns(pro_path, sch_path, list(current))
    logger.info(
        f"Bootstrapped {MCP_EXPECTED_KEY} in {sink} "
        f"with {len(current)} entries"
    )
    return True


def verify_netclass_patterns(
    pro_path: Path,
    restore: bool = False,
    sch_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Compare the live ``netclass_patterns`` against the expected set
    stored in the schematic's Schematic_Metadata singleton (preferred)
    or in ``.kicad_pro`` (legacy). Optionally restore missing entries.

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
      expectedSource (str) — "singleton", "pro", or "missing"; tells
        the caller which source supplied the expected set.
    """
    data = _read_kicad_pro(pro_path)
    if data is None:
        return {
            "success": False,
            "message": f"Could not read {pro_path}",
        }

    expected_raw, expected_source = _read_expected_patterns(pro_path, sch_path)

    if expected_raw is None:
        # First-time encounter — seed it from the current state and
        # write to the preferred source (singleton when sch given).
        current = list(_current_patterns(data))
        sink = _write_expected_patterns(pro_path, sch_path, current)
        return {
            "success": True,
            "bootstrapped": True,
            "drifted": False,
            "missing": [],
            "extra": [],
            "expectedSource": sink,
            "message": (
                f"No {MCP_EXPECTED_KEY} on schematic or in .kicad_pro; "
                f"seeded {sink} from current "
                f"net_settings.netclass_patterns ({len(current)} entries)."
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
        "expectedSource": expected_source,
    }

    if restore and missing:
        # Append the missing entries back to net_settings.netclass_patterns.
        # We do not touch `extra` — those may be intentional new additions
        # the user wants to keep. Restoring just rebuilds the floor.
        # Restoration always writes to .kicad_pro since that's where
        # KiCad reads the live netclass_patterns from.
        current_list.extend(
            {"netclass": nc, "pattern": p} for nc, p in missing
        )
        data.setdefault("net_settings", {})["netclass_patterns"] = current_list
        _write_kicad_pro(pro_path, data)
        result["restored"] = result["missing"]
        result["message"] = (
            f"Restored {len(missing)} missing pattern(s) to "
            f"net_settings.netclass_patterns "
            f"(expected source: {expected_source})."
        )
    elif drifted:
        result["message"] = (
            f"Netclass-pattern drift: {len(missing)} missing, "
            f"{len(extra)} extra (expected source: {expected_source}). "
            f"Pass restore=true to re-add missing."
        )
    else:
        result["message"] = (
            f"All {len(expected_tuples)} expected pattern(s) present "
            f"(source: {expected_source})."
        )

    return result
