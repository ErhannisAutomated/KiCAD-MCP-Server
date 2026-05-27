"""
Project-related command implementations for KiCAD interface
"""

import logging
import os
import shutil
from typing import Any, Dict, Optional

import pcbnew  # type: ignore

logger = logging.getLogger("kicad_interface")


class ProjectCommands:
    """Handles project-related KiCAD operations"""

    def __init__(self, board: Optional[pcbnew.BOARD] = None):
        """Initialize with optional board instance"""
        self.board = board

    def create_project(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new KiCAD project"""
        try:
            # Accept both 'name' (from MCP tool) and 'projectName' (legacy)
            project_name = params.get("name") or params.get("projectName", "New_Project")
            path = params.get("path", os.getcwd())
            template = params.get("template")

            # Generate the full project path
            project_path = os.path.join(path, project_name)
            if not project_path.endswith(".kicad_pro"):
                project_path += ".kicad_pro"

            # Create project directory if it doesn't exist
            os.makedirs(os.path.dirname(project_path), exist_ok=True)

            # Create a new board
            board = pcbnew.BOARD()

            # Set project properties
            board.GetTitleBlock().SetTitle(project_name)

            # Set current date with proper parameter
            from datetime import datetime

            current_date = datetime.now().strftime("%Y-%m-%d")
            board.GetTitleBlock().SetDate(current_date)

            # If template is specified, try to load it
            if template:
                template_path = os.path.expanduser(template)
                if os.path.exists(template_path):
                    template_board = pcbnew.LoadBoard(template_path)
                    # Copy settings from template
                    board.SetDesignSettings(template_board.GetDesignSettings())
                    board.SetLayerStack(template_board.GetLayerStack())

            # Save the board
            board_path = project_path.replace(".kicad_pro", ".kicad_pcb")
            board.SetFileName(board_path)
            pcbnew.SaveBoard(board_path, board)

            # Create schematic from template (use expanded template with symbol definitions)
            schematic_path = project_path.replace(".kicad_pro", ".kicad_sch")
            template_sch_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "..",
                "templates",
                "template_with_symbols_expanded.kicad_sch",
            )

            if os.path.exists(template_sch_path):
                # Copy template schematic
                shutil.copy(template_sch_path, schematic_path)

                # Regenerate UUID to ensure uniqueness for each created project
                import re
                import uuid as uuid_module

                with open(schematic_path, "r", encoding="utf-8") as f:
                    content = f.read()
                new_uuid = str(uuid_module.uuid4())
                content = re.sub(
                    r"\(uuid [0-9a-fA-F-]+\)",
                    f"(uuid {new_uuid})",
                    content,
                    count=1,  # Only replace first (schematic) UUID
                )
                with open(schematic_path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(content)

                logger.info(f"Created schematic from template: {schematic_path}")
            else:
                # Fallback: create minimal schematic
                logger.warning(
                    f"Template not found at {template_sch_path}, creating minimal schematic"
                )
                import uuid as uuid_module

                schematic_uuid = str(uuid_module.uuid4())
                with open(schematic_path, "w", encoding="utf-8", newline="\n") as f:
                    f.write('(kicad_sch (version 20250114) (generator "KiCAD-MCP-Server")\n\n')
                    f.write(f"  (uuid {schematic_uuid})\n\n")
                    f.write('  (paper "A4")\n\n')
                    f.write("  (lib_symbols\n  )\n\n")
                    f.write('  (sheet_instances\n    (path "/" (page "1"))\n  )\n')
                    f.write(")\n")

            # Create project file with schematic reference
            with open(project_path, "w") as f:
                f.write("{\n")
                f.write('  "board": {\n')
                f.write(f'    "filename": "{os.path.basename(board_path)}"\n')
                f.write("  },\n")
                f.write('  "sheets": [\n')
                f.write(f'    ["root", "{os.path.basename(schematic_path)}"]\n')
                f.write("  ]\n")
                f.write("}\n")

            self.board = board

            return {
                "success": True,
                "message": f"Created project: {project_name}",
                "project": {
                    "name": project_name,
                    "path": project_path,
                    "boardPath": board_path,
                    "schematicPath": schematic_path,
                },
            }

        except Exception as e:
            logger.error(f"Error creating project: {str(e)}")
            return {
                "success": False,
                "message": "Failed to create project",
                "errorDetails": str(e),
            }

    def open_project(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Open an existing KiCAD project"""
        try:
            filename = params.get("filename")
            if not filename:
                return {
                    "success": False,
                    "message": "No filename provided",
                    "errorDetails": "The filename parameter is required",
                }

            # Expand user path and make absolute
            filename = os.path.abspath(os.path.expanduser(filename))

            # If it's a project file, get the board file
            if filename.endswith(".kicad_pro"):
                board_path = filename.replace(".kicad_pro", ".kicad_pcb")
            else:
                board_path = filename

            # KiCad's SETTINGS_MANAGER caches the project (including netclass
            # clearances/widths and design rules) for the life of the process.
            # A plain LoadBoard on a re-open reuses that cache, so out-of-band
            # edits to .kicad_pro are silently ignored — e.g. export_dsn then
            # emits stale netclass clearances. Unload the cached project first
            # so LoadBoard re-reads .kicad_pro fresh.
            project_path = os.path.splitext(board_path)[0] + ".kicad_pro"
            try:
                sm = pcbnew.GetSettingsManager()
                if sm.IsProjectOpen():
                    proj = None
                    try:
                        proj = sm.GetProject(project_path)
                    except Exception:
                        try:
                            proj = sm.GetProject()
                        except Exception:
                            proj = None
                    if proj is not None:
                        sm.UnloadProject(proj, False)  # False = don't save
            except Exception as cache_err:
                logger.warning(f"Could not refresh project settings cache: {cache_err}")

            # Load the board
            board = pcbnew.LoadBoard(board_path)
            self.board = board

            return {
                "success": True,
                "message": f"Opened project: {os.path.basename(board_path)}",
                "project": {
                    "name": os.path.splitext(os.path.basename(board_path))[0],
                    "path": filename,
                    "boardPath": board_path,
                },
            }

        except Exception as e:
            logger.error(f"Error opening project: {str(e)}")
            return {
                "success": False,
                "message": "Failed to open project",
                "errorDetails": str(e),
            }

    def save_project(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Save the current KiCAD project (PCB and/or schematic).

        The PCB save always runs when a board is loaded.

        Schematic save is OPT-IN as of #220. Schematic-mutating MCP
        tools (set_schematic_component_property, add_schematic_wire,
        etc.) already write to disk on each call, so there's no
        in-memory schematic state to flush — save_project doesn't
        need to touch the .kicad_sch. The previous behaviour
        auto-derived a schematic path from the board path and ran a
        kicad-skip round-trip "confirmation save", which DROPPED the
        lib_symbols section because kicad-skip's serialiser doesn't
        perfectly preserve it. The .kicad_sch went from ~1449 lines
        to ~278 on a typical project — requiring a manual
        ``git checkout HEAD -- *.kicad_sch`` after every save.

        Pass ``schematicPath`` explicitly + ``flushSchematic=true``
        to force the old round-trip behaviour (kept for the rare
        case where some future tool actually does need it; not
        recommended).
        """
        try:
            filename = params.get("filename")
            schematic_path = params.get("schematicPath")
            flush_schematic = bool(params.get("flushSchematic", False))
            saved = []
            warnings = []

            # --- PCB save ---
            if self.board:
                if filename:
                    filename = os.path.abspath(os.path.expanduser(filename))
                    self.board.SetFileName(filename)
                pcbnew.SaveBoard(self.board.GetFileName(), self.board)
                saved.append(self.board.GetFileName())

            # --- Schematic save (opt-in) ---
            # Only round-trips the schematic when the caller
            # explicitly asks. Otherwise leaves it alone — see the
            # docstring for the history.
            if schematic_path and flush_schematic:
                schematic_path = os.path.abspath(os.path.expanduser(schematic_path))
                if os.path.exists(schematic_path):
                    try:
                        import skip  # type: ignore

                        sch = skip.Schematic(schematic_path)
                        sch.write(schematic_path)
                        saved.append(schematic_path)
                        warnings.append(
                            "flushSchematic=true ran a kicad-skip "
                            "round-trip on the .kicad_sch; verify "
                            "lib_symbols hasn't been stripped — see #220."
                        )
                    except Exception as sch_err:
                        warnings.append(f"Schematic save failed: {sch_err}")
                else:
                    warnings.append(f"Schematic path not found: {schematic_path}")

            if not saved:
                return {
                    "success": False,
                    "message": "Nothing to save",
                    "errorDetails": (
                        "No board is loaded and no schematicPath was provided. "
                        "Note: schematic operations auto-save to disk after each call."
                    ),
                }

            result = {
                "success": True,
                "message": f"Saved: {', '.join(os.path.basename(p) for p in saved)}",
                "saved": saved,
            }
            if warnings:
                result["warnings"] = warnings
            return result

        except Exception as e:
            logger.error(f"Error saving project: {str(e)}")
            return {
                "success": False,
                "message": "Failed to save project",
                "errorDetails": str(e),
            }

    def get_project_info(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Get information about the current project"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            title_block = self.board.GetTitleBlock()
            filename = self.board.GetFileName()

            return {
                "success": True,
                "project": {
                    "name": os.path.splitext(os.path.basename(filename))[0],
                    "path": filename,
                    "title": title_block.GetTitle(),
                    "date": title_block.GetDate(),
                    "revision": title_block.GetRevision(),
                    "company": title_block.GetCompany(),
                    "comment1": title_block.GetComment(0),
                    "comment2": title_block.GetComment(1),
                    "comment3": title_block.GetComment(2),
                    "comment4": title_block.GetComment(3),
                },
            }

        except Exception as e:
            logger.error(f"Error getting project info: {str(e)}")
            return {
                "success": False,
                "message": "Failed to get project information",
                "errorDetails": str(e),
            }
