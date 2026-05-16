"""
Design rules command implementations for KiCAD interface
"""

import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import pcbnew

logger = logging.getLogger("kicad_interface")

# Patterns for extracting structured fields from kicad-cli's per-item
# description strings (e.g. "Track [BAT+] on F.Cu, length 0.6500 mm",
# "Pad 1 of C9", "Footprint R27").
_NET_RE = re.compile(r"\[([^\]]+)\]")
_LAYER_RE = re.compile(r"\bon\s+([A-Za-z0-9_]+\.[A-Za-z0-9_]+)")
_LENGTH_RE = re.compile(r"length\s+([\d.]+)\s*mm", re.IGNORECASE)
_REF_RE = re.compile(r"\b(?:Pad\s+\S+\s+of\s+|Footprint\s+)([A-Z][A-Z0-9_]*\d+)")


def _parse_item_description(desc: str) -> Dict[str, Any]:
    """Pull net, layer, length_mm, and component ref out of an item
    description string. Best-effort; missing fields return None."""
    if not desc:
        return {"net": None, "layer": None, "length_mm": None, "ref": None}
    net_m = _NET_RE.search(desc)
    layer_m = _LAYER_RE.search(desc)
    length_m = _LENGTH_RE.search(desc)
    ref_m = _REF_RE.search(desc)
    return {
        "net": net_m.group(1) if net_m else None,
        "layer": layer_m.group(1) if layer_m else None,
        "length_mm": float(length_m.group(1)) if length_m else None,
        "ref": ref_m.group(1) if ref_m else None,
    }


class DesignRuleCommands:
    """Handles design rule checking and configuration"""

    def __init__(self, board: Optional[pcbnew.BOARD] = None):
        """Initialize with optional board instance"""
        self.board = board

    def set_design_rules(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Set design rules for the PCB"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            design_settings = self.board.GetDesignSettings()

            # Convert mm to nanometers for KiCAD internal units
            scale = 1000000  # mm to nm

            # Set clearance
            if "clearance" in params:
                design_settings.m_MinClearance = int(params["clearance"] * scale)

            # KiCAD 9.0: Use SetCustom* methods instead of SetCurrent* (which were removed)
            # Track if we set any custom track/via values
            custom_values_set = False

            if "trackWidth" in params:
                design_settings.SetCustomTrackWidth(int(params["trackWidth"] * scale))
                custom_values_set = True

            # Via settings
            if "viaDiameter" in params:
                design_settings.SetCustomViaSize(int(params["viaDiameter"] * scale))
                custom_values_set = True
            if "viaDrill" in params:
                design_settings.SetCustomViaDrill(int(params["viaDrill"] * scale))
                custom_values_set = True

            # KiCAD 9.0: Activate custom track/via values so they become the current values
            if custom_values_set:
                design_settings.UseCustomTrackViaSize(True)

            # Set micro via settings (use properties - methods removed in KiCAD 9.0)
            if "microViaDiameter" in params:
                design_settings.m_MicroViasMinSize = int(params["microViaDiameter"] * scale)
            if "microViaDrill" in params:
                design_settings.m_MicroViasMinDrill = int(params["microViaDrill"] * scale)

            # Set minimum values
            if "minTrackWidth" in params:
                design_settings.m_TrackMinWidth = int(params["minTrackWidth"] * scale)
            if "minViaDiameter" in params:
                design_settings.m_ViasMinSize = int(params["minViaDiameter"] * scale)

            # KiCAD 9.0: m_ViasMinDrill removed - use m_MinThroughDrill instead
            if "minViaDrill" in params:
                design_settings.m_MinThroughDrill = int(params["minViaDrill"] * scale)

            if "minMicroViaDiameter" in params:
                design_settings.m_MicroViasMinSize = int(params["minMicroViaDiameter"] * scale)
            if "minMicroViaDrill" in params:
                design_settings.m_MicroViasMinDrill = int(params["minMicroViaDrill"] * scale)

            # KiCAD 9.0: m_MinHoleDiameter removed - use m_MinThroughDrill
            if "minHoleDiameter" in params:
                design_settings.m_MinThroughDrill = int(params["minHoleDiameter"] * scale)

            # KiCAD 9.0: Added hole clearance settings
            if "holeClearance" in params:
                design_settings.m_HoleClearance = int(params["holeClearance"] * scale)
            if "holeToHoleMin" in params:
                design_settings.m_HoleToHoleMin = int(params["holeToHoleMin"] * scale)

            # Build response with KiCAD 9.0 compatible properties
            # After UseCustomTrackViaSize(True), GetCurrent* returns the custom values
            response_rules = {
                "clearance": design_settings.m_MinClearance / scale,
                "trackWidth": design_settings.GetCurrentTrackWidth() / scale,
                "viaDiameter": design_settings.GetCurrentViaSize() / scale,
                "viaDrill": design_settings.GetCurrentViaDrill() / scale,
                "microViaDiameter": design_settings.m_MicroViasMinSize / scale,
                "microViaDrill": design_settings.m_MicroViasMinDrill / scale,
                "minTrackWidth": design_settings.m_TrackMinWidth / scale,
                "minViaDiameter": design_settings.m_ViasMinSize / scale,
                "minThroughDrill": design_settings.m_MinThroughDrill / scale,
                "minMicroViaDiameter": design_settings.m_MicroViasMinSize / scale,
                "minMicroViaDrill": design_settings.m_MicroViasMinDrill / scale,
                "holeClearance": design_settings.m_HoleClearance / scale,
                "holeToHoleMin": design_settings.m_HoleToHoleMin / scale,
                "viasMinAnnularWidth": design_settings.m_ViasMinAnnularWidth / scale,
            }

            return {
                "success": True,
                "message": "Updated design rules",
                "rules": response_rules,
            }

        except Exception as e:
            logger.error(f"Error setting design rules: {str(e)}")
            return {
                "success": False,
                "message": "Failed to set design rules",
                "errorDetails": str(e),
            }

    def get_design_rules(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Get current design rules - KiCAD 9.0 compatible"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            design_settings = self.board.GetDesignSettings()
            scale = 1000000  # nm to mm

            # Build rules dict with KiCAD 9.0 compatible properties
            rules = {
                # Core clearance and track settings
                "clearance": design_settings.m_MinClearance / scale,
                "trackWidth": design_settings.GetCurrentTrackWidth() / scale,
                "minTrackWidth": design_settings.m_TrackMinWidth / scale,
                # Via settings (current values from methods)
                "viaDiameter": design_settings.GetCurrentViaSize() / scale,
                "viaDrill": design_settings.GetCurrentViaDrill() / scale,
                # Via minimum values
                "minViaDiameter": design_settings.m_ViasMinSize / scale,
                "viasMinAnnularWidth": design_settings.m_ViasMinAnnularWidth / scale,
                # Micro via settings
                "microViaDiameter": design_settings.m_MicroViasMinSize / scale,
                "microViaDrill": design_settings.m_MicroViasMinDrill / scale,
                "minMicroViaDiameter": design_settings.m_MicroViasMinSize / scale,
                "minMicroViaDrill": design_settings.m_MicroViasMinDrill / scale,
                # KiCAD 9.0: Hole and drill settings (replaces removed m_ViasMinDrill and m_MinHoleDiameter)
                "minThroughDrill": design_settings.m_MinThroughDrill / scale,
                "holeClearance": design_settings.m_HoleClearance / scale,
                "holeToHoleMin": design_settings.m_HoleToHoleMin / scale,
                # Other constraints
                "copperEdgeClearance": design_settings.m_CopperEdgeClearance / scale,
                "silkClearance": design_settings.m_SilkClearance / scale,
            }

            return {"success": True, "rules": rules}

        except Exception as e:
            logger.error(f"Error getting design rules: {str(e)}")
            return {
                "success": False,
                "message": "Failed to get design rules",
                "errorDetails": str(e),
            }

    def run_drc(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Run Design Rule Check using kicad-cli"""
        import json
        import platform
        import shutil
        import subprocess
        import tempfile

        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            report_path = params.get("reportPath")

            # Get the board file path
            board_file = self.board.GetFileName()
            if not board_file or not os.path.exists(board_file):
                return {
                    "success": False,
                    "message": "Board file not found",
                    "errorDetails": "Cannot run DRC without a saved board file",
                }

            # Find kicad-cli executable
            kicad_cli = self._find_kicad_cli()
            if not kicad_cli:
                return {
                    "success": False,
                    "message": "kicad-cli not found",
                    "errorDetails": "KiCAD CLI tool not found in system. Install KiCAD 8.0+ or set PATH.",
                }

            # Create temporary JSON output file
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
                json_output = tmp.name

            try:
                # Build command
                cmd = [
                    kicad_cli,
                    "pcb",
                    "drc",
                    "--format",
                    "json",
                    "--output",
                    json_output,
                    "--units",
                    "mm",
                    board_file,
                ]

                logger.info(f"Running DRC command: {' '.join(cmd)}")

                # Run DRC
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=600,  # 10 minute timeout for large boards (21MB PCB needs time)
                )

                if result.returncode != 0:
                    logger.error(f"DRC command failed: {result.stderr}")
                    return {
                        "success": False,
                        "message": "DRC command failed",
                        "errorDetails": result.stderr,
                    }

                # Read JSON output
                with open(json_output, "r", encoding="utf-8") as f:
                    drc_data = json.load(f)

                # Parse violations from kicad-cli output. kicad-cli writes
                # violations and unconnected_items in SEPARATE top-level
                # arrays; both are real DRC findings and both belong in our
                # consolidated response.
                violations = []
                violation_counts: dict[str, int] = {}
                severity_counts = {"error": 0, "warning": 0, "info": 0}

                def _normalise(raw: Dict[str, Any], default_type: str) -> Dict[str, Any]:
                    vtype = raw.get("type", default_type)
                    vseverity = raw.get("severity", "error")
                    items_raw = raw.get("items", []) or []
                    items_out: List[Dict[str, Any]] = []
                    for it in items_raw:
                        pos = it.get("pos") or {}
                        item_desc = it.get("description", "") or ""
                        parsed = _parse_item_description(item_desc)
                        items_out.append(
                            {
                                "description": item_desc,
                                "pos": {
                                    "x": pos.get("x", 0),
                                    "y": pos.get("y", 0),
                                    "unit": "mm",
                                },
                                "uuid": it.get("uuid", ""),
                                **parsed,
                            }
                        )
                    loc_x, loc_y = 0, 0
                    if items_out:
                        loc_x = items_out[0]["pos"]["x"]
                        loc_y = items_out[0]["pos"]["y"]
                    return {
                        "type": vtype,
                        "severity": vseverity,
                        "message": raw.get("description", ""),
                        "items": items_out,
                        "location": {"x": loc_x, "y": loc_y, "unit": "mm"},
                    }

                for raw in drc_data.get("violations", []):
                    v = _normalise(raw, default_type="unknown")
                    violations.append(v)
                    violation_counts[v["type"]] = violation_counts.get(v["type"], 0) + 1
                    if v["severity"] in severity_counts:
                        severity_counts[v["severity"]] += 1

                # Unconnected items live in a sibling array but ARE DRC
                # findings (severity "error", type "unconnected_items").
                for raw in drc_data.get("unconnected_items", []):
                    v = _normalise(raw, default_type="unconnected_items")
                    violations.append(v)
                    violation_counts[v["type"]] = violation_counts.get(v["type"], 0) + 1
                    if v["severity"] in severity_counts:
                        severity_counts[v["severity"]] += 1

                # Determine where to save the violations file
                board_dir = os.path.dirname(board_file)
                board_name = os.path.splitext(os.path.basename(board_file))[0]
                violations_file = os.path.join(board_dir, f"{board_name}_drc_violations.json")

                # Always save violations to JSON file (for large result sets)
                with open(violations_file, "w", encoding="utf-8") as f:
                    json.dump(
                        {
                            "board": board_file,
                            "timestamp": drc_data.get("date", "unknown"),
                            "total_violations": len(violations),
                            "violation_counts": violation_counts,
                            "severity_counts": severity_counts,
                            "violations": violations,
                        },
                        f,
                        indent=2,
                    )

                # Save text report if requested
                if report_path:
                    report_path = os.path.abspath(os.path.expanduser(report_path))
                    cmd_report = [
                        kicad_cli,
                        "pcb",
                        "drc",
                        "--format",
                        "report",
                        "--output",
                        report_path,
                        "--units",
                        "mm",
                        board_file,
                    ]
                    subprocess.run(cmd_report, capture_output=True, timeout=600)

                # Return summary only (not full violations list)
                return {
                    "success": True,
                    "message": f"Found {len(violations)} DRC violations",
                    "summary": {
                        "total": len(violations),
                        "by_severity": severity_counts,
                        "by_type": violation_counts,
                    },
                    "violationsFile": violations_file,
                    "reportPath": report_path if report_path else None,
                }

            finally:
                # Clean up temp JSON file
                if os.path.exists(json_output):
                    os.unlink(json_output)

        except subprocess.TimeoutExpired:
            logger.error("DRC command timed out")
            return {
                "success": False,
                "message": "DRC command timed out",
                "errorDetails": "Command took longer than 600 seconds (10 minutes)",
            }
        except Exception as e:
            logger.error(f"Error running DRC: {str(e)}")
            return {
                "success": False,
                "message": "Failed to run DRC",
                "errorDetails": str(e),
            }

    def _find_kicad_cli(self) -> Optional[str]:
        """Find kicad-cli executable"""
        import platform
        import shutil

        # Try system PATH first
        cli_name = "kicad-cli.exe" if platform.system() == "Windows" else "kicad-cli"
        cli_path = shutil.which(cli_name)
        if cli_path:
            return cli_path

        # Try common installation paths (version-specific)
        if platform.system() == "Windows":
            common_paths = [
                r"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe",
                r"C:\Program Files\KiCad\9.0\bin\kicad-cli.exe",
                r"C:\Program Files\KiCad\8.0\bin\kicad-cli.exe",
                r"C:\Program Files (x86)\KiCad\10.0\bin\kicad-cli.exe",
                r"C:\Program Files (x86)\KiCad\9.0\bin\kicad-cli.exe",
                r"C:\Program Files (x86)\KiCad\8.0\bin\kicad-cli.exe",
                r"C:\Program Files\KiCad\bin\kicad-cli.exe",
            ]
            for path in common_paths:
                if os.path.exists(path):
                    return path
        elif platform.system() == "Darwin":  # macOS
            common_paths = [
                "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
                "/usr/local/bin/kicad-cli",
            ]
            for path in common_paths:
                if os.path.exists(path):
                    return path
        else:  # Linux
            common_paths = [
                "/usr/bin/kicad-cli",
                "/usr/local/bin/kicad-cli",
            ]
            for path in common_paths:
                if os.path.exists(path):
                    return path

        return None

    def get_drc_violations(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Return the consolidated list of DRC findings (violations +
        unconnected items), with optional filtering.

        Params:
          severity: "error" | "warning" | "info" | "all" (default "all")
          type: str | list[str] — keep only findings whose ``type``
                matches (e.g. "shorting_items", "unconnected_items",
                ["track_dangling", "shorting_items"]).
          net: str — keep only findings where some item is on this net
               (parsed from kicad-cli item descriptions like
               "Track [BAT+] on F.Cu").
          summaryOnly: bool — return counts only, no individual items.
                       Default false.
          useCachedReport: bool — skip the kicad-cli re-run; read the
                           previous violations file if it exists.
                           Default false (always re-runs for freshness).
        """
        import json

        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            severity = params.get("severity", "all")
            type_filter = params.get("type")
            net_filter = params.get("net")
            summary_only = bool(params.get("summaryOnly", False))
            use_cached = bool(params.get("useCachedReport", False))

            if isinstance(type_filter, str):
                type_set = {type_filter}
            elif isinstance(type_filter, (list, tuple)):
                type_set = set(type_filter)
            else:
                type_set = None

            board_file = self.board.GetFileName()
            board_dir = os.path.dirname(board_file)
            board_name = os.path.splitext(os.path.basename(board_file))[0]
            violations_file = os.path.join(board_dir, f"{board_name}_drc_violations.json")

            if use_cached and os.path.exists(violations_file):
                with open(violations_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                logger.info(f"get_drc_violations: using cached report {violations_file}")
            else:
                drc_result = self.run_drc({})
                if not drc_result.get("success"):
                    return drc_result
                violations_file = drc_result.get("violationsFile")
                if not violations_file or not os.path.exists(violations_file):
                    return {
                        "success": False,
                        "message": "Violations file not found",
                        "errorDetails": "run_drc did not create violations file",
                    }
                with open(violations_file, "r", encoding="utf-8") as f:
                    data = json.load(f)

            all_violations = data.get("violations", [])

            def _matches(v: Dict[str, Any]) -> bool:
                if severity != "all" and v.get("severity") != severity:
                    return False
                if type_set is not None and v.get("type") not in type_set:
                    return False
                if net_filter:
                    for it in v.get("items", []):
                        if it.get("net") == net_filter:
                            return True
                    return False
                return True

            filtered = [v for v in all_violations if _matches(v)]

            # Compose summary counts from the filtered set.
            counts_by_type: Dict[str, int] = {}
            counts_by_severity: Dict[str, int] = {"error": 0, "warning": 0, "info": 0}
            for v in filtered:
                counts_by_type[v["type"]] = counts_by_type.get(v["type"], 0) + 1
                sev = v.get("severity", "error")
                counts_by_severity[sev] = counts_by_severity.get(sev, 0) + 1

            response: Dict[str, Any] = {
                "success": True,
                "violationsFile": violations_file,
                "total": len(filtered),
                "summary": {
                    "by_type": counts_by_type,
                    "by_severity": counts_by_severity,
                },
                "filters": {
                    "severity": severity,
                    "type": list(type_set) if type_set else None,
                    "net": net_filter,
                },
            }
            if not summary_only:
                response["violations"] = filtered
            return response

        except Exception as e:
            logger.error(f"Error getting DRC violations: {str(e)}")
            return {
                "success": False,
                "message": "Failed to get DRC violations",
                "errorDetails": str(e),
            }
