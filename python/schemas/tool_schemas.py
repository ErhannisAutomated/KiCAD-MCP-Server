"""
Comprehensive tool schema definitions for all KiCAD MCP commands

Following MCP 2025-06-18 specification for tool definitions.
Each tool includes:
- name: Unique identifier
- title: Human-readable display name
- description: Detailed explanation of what the tool does
- inputSchema: JSON Schema for parameters
- outputSchema: Optional JSON Schema for return values (structured content)
"""

from typing import Any, Dict

# =============================================================================
# PROJECT TOOLS
# =============================================================================

PROJECT_TOOLS = [
    {
        "name": "create_project",
        "title": "Create New KiCAD Project",
        "description": "Creates a new KiCAD project with PCB board file and optional project configuration. Automatically creates project directory and initializes board with default settings.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "projectName": {
                    "type": "string",
                    "description": "Name of the project (used for file naming)",
                    "minLength": 1,
                },
                "path": {
                    "type": "string",
                    "description": "Directory path where project will be created (defaults to current working directory)",
                },
                "template": {
                    "type": "string",
                    "description": "Optional path to template board file to copy settings from",
                },
            },
            "required": ["projectName"],
        },
    },
    {
        "name": "open_project",
        "title": "Open Existing KiCAD Project",
        "description": "Opens an existing KiCAD project file (.kicad_pro or .kicad_pcb) and loads the board into memory for manipulation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Path to .kicad_pro or .kicad_pcb file",
                }
            },
            "required": ["filename"],
        },
    },
    {
        "name": "save_project",
        "title": "Save Current Project",
        "description": (
            "Saves the loaded PCB to disk. Schematic operations (add_schematic_component, "
            "connect_to_net, etc.) already auto-save their .kicad_sch after each call, so this "
            "tool's job is to flush the PCB — and ONLY the PCB by default. "
            "The .kicad_sch is left untouched unless you pass schematicPath AND flushSchematic=true; "
            "see #220 for why (kicad-skip's round-trip drops the lib_symbols cache)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Optional new path for the board file (renames board on save)",
                },
                "schematicPath": {
                    "type": "string",
                    "description": (
                        "Path to .kicad_sch. Only used when flushSchematic=true. "
                        "Ignored otherwise — schematic ops persist on each call."
                    ),
                },
                "flushSchematic": {
                    "type": "boolean",
                    "description": (
                        "Force a kicad-skip round-trip save of the schematic. Default false. "
                        "NOT recommended: the round-trip strips the lib_symbols cache section "
                        "from .kicad_sch (#220). Only enable if you have in-memory schematic "
                        "state that isn't already persisted."
                    ),
                },
            },
        },
    },
    {
        "name": "snapshot_project",
        "title": "Snapshot Project (Checkpoint)",
        "description": "Copies the entire project folder to a new timestamped snapshot directory so you can resume from this checkpoint later without redoing earlier steps. Call this after every successfully completed design step (e.g. after Step 1 schematic, after Step 2 PCB layout) before asking for user confirmation to proceed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "step": {
                    "type": "string",
                    "description": "Step number or name to include in snapshot folder name, e.g. '1' or '2'",
                },
                "label": {
                    "type": "string",
                    "description": "Optional short label, e.g. 'schematic_ok' or 'layout_ok'",
                },
                "projectPath": {
                    "type": "string",
                    "description": "Project directory path. Auto-detected from loaded board if omitted.",
                },
            },
        },
    },
    {
        "name": "get_project_info",
        "title": "Get Project Information",
        "description": "Retrieves metadata and properties of the currently open project including name, paths, and board status.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

# =============================================================================
# BOARD TOOLS
# =============================================================================

BOARD_TOOLS = [
    {
        "name": "set_board_size",
        "title": "Set Board Dimensions",
        "description": "Sets the PCB board dimensions. The board outline must be added separately using add_board_outline.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "width": {
                    "type": "number",
                    "description": "Board width in millimeters",
                    "minimum": 1,
                },
                "height": {
                    "type": "number",
                    "description": "Board height in millimeters",
                    "minimum": 1,
                },
            },
            "required": ["width", "height"],
        },
    },
    {
        "name": "add_board_outline",
        "title": "Add Board Outline",
        "description": "Adds a board outline shape (rectangle, rounded_rectangle, circle, or polygon) on the Edge.Cuts layer. By default the board top-left corner is placed at (0, 0) so all coordinates are positive. Use x/y to set a different top-left corner position.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "shape": {
                    "type": "string",
                    "enum": ["rectangle", "rounded_rectangle", "circle", "polygon"],
                    "description": "Shape type for the board outline",
                },
                "width": {
                    "type": "number",
                    "description": "Width in mm (for rectangle/rounded_rectangle)",
                    "minimum": 1,
                },
                "height": {
                    "type": "number",
                    "description": "Height in mm (for rectangle/rounded_rectangle)",
                    "minimum": 1,
                },
                "x": {
                    "type": "number",
                    "description": "X coordinate of the top-left corner in mm (default: 0). Board extends from x to x+width.",
                },
                "y": {
                    "type": "number",
                    "description": "Y coordinate of the top-left corner in mm (default: 0). Board extends from y to y+height.",
                },
                "radius": {
                    "type": "number",
                    "description": "Corner radius in mm for rounded_rectangle, or radius for circle",
                    "minimum": 0,
                },
                "points": {
                    "type": "array",
                    "description": "Array of {x, y} point objects in mm (for polygon shape only)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "x": {"type": "number"},
                            "y": {"type": "number"},
                        },
                        "required": ["x", "y"],
                    },
                    "minItems": 3,
                },
            },
            "required": ["shape"],
        },
    },
    {
        "name": "add_layer",
        "title": "Add Custom Layer",
        "description": "Adds a new custom layer to the board stack (e.g., User.1, User.Comments).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "layerName": {
                    "type": "string",
                    "description": "Name of the layer to add",
                },
                "layerType": {
                    "type": "string",
                    "enum": ["signal", "power", "mixed", "jumper"],
                    "description": "Type of layer (for copper layers)",
                },
            },
            "required": ["layerName"],
        },
    },
    {
        "name": "set_active_layer",
        "title": "Set Active Layer",
        "description": "Sets the currently active layer for drawing operations.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "layerName": {
                    "type": "string",
                    "description": "Name of the layer to make active (e.g., F.Cu, B.Cu, Edge.Cuts)",
                }
            },
            "required": ["layerName"],
        },
    },
    {
        "name": "get_layer_list",
        "title": "List Board Layers",
        "description": "Returns a list of all layers in the board with their properties.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_board_info",
        "title": "Get Board Information",
        "description": "Retrieves comprehensive board information including dimensions, layer count, component count, and design rules.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_board_2d_view",
        "title": "Render Board Preview",
        "description": "Generates a 2D visual representation of the current board state as a PNG image.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "width": {
                    "type": "number",
                    "description": "Image width in pixels (default: 800)",
                    "minimum": 100,
                    "default": 800,
                },
                "height": {
                    "type": "number",
                    "description": "Image height in pixels (default: 600)",
                    "minimum": 100,
                    "default": 600,
                },
            },
        },
    },
    {
        "name": "get_board_extents",
        "title": "Get Board Bounding Box",
        "description": "Returns the bounding box extents of the PCB board including all edge cuts, components, and traces.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "unit": {
                    "type": "string",
                    "enum": ["mm", "inch"],
                    "description": "Unit for returned coordinates (default: mm)",
                    "default": "mm",
                }
            },
        },
    },
    {
        "name": "add_mounting_hole",
        "title": "Add Mounting Hole",
        "description": "Adds a mounting hole (non-plated through hole) at the specified position with given diameter.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": {"type": "number", "description": "X coordinate in millimeters"},
                "y": {"type": "number", "description": "Y coordinate in millimeters"},
                "diameter": {
                    "type": "number",
                    "description": "Hole diameter in millimeters",
                    "minimum": 0.1,
                },
            },
            "required": ["x", "y", "diameter"],
        },
    },
    {
        "name": "import_svg_logo",
        "title": "Import SVG Logo to PCB",
        "description": "Imports an SVG file as filled graphic polygons onto a KiCAD PCB layer (default F.SilkS). Curves are linearised automatically. Supports path, rect, circle, ellipse, polygon and group transforms.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pcbPath": {
                    "type": "string",
                    "description": "Path to the .kicad_pcb file",
                },
                "svgPath": {
                    "type": "string",
                    "description": "Path to the SVG logo file",
                },
                "x": {
                    "type": "number",
                    "description": "X position of the logo top-left corner in mm",
                },
                "y": {
                    "type": "number",
                    "description": "Y position of the logo top-left corner in mm",
                },
                "width": {
                    "type": "number",
                    "description": "Target width of the logo in mm (height scaled to preserve aspect ratio)",
                    "minimum": 0.1,
                },
                "layer": {
                    "type": "string",
                    "description": "PCB layer name, e.g. F.SilkS or B.SilkS (default: F.SilkS)",
                    "default": "F.SilkS",
                },
                "strokeWidth": {
                    "type": "number",
                    "description": "Outline stroke width in mm (0 = no outline, default 0)",
                    "default": 0,
                },
                "filled": {
                    "type": "boolean",
                    "description": "Fill polygons with solid layer colour (default true)",
                    "default": True,
                },
            },
            "required": ["pcbPath", "svgPath", "x", "y", "width"],
        },
    },
    {
        "name": "add_board_text",
        "title": "Add Text to Board",
        "description": "Adds text annotation to the board on a specified layer (e.g., F.SilkS for top silkscreen).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Text content to add",
                    "minLength": 1,
                },
                "x": {"type": "number", "description": "X coordinate in millimeters"},
                "y": {"type": "number", "description": "Y coordinate in millimeters"},
                "layer": {
                    "type": "string",
                    "description": "Layer name (e.g., F.SilkS, B.SilkS, F.Cu)",
                    "default": "F.SilkS",
                },
                "size": {
                    "type": "number",
                    "description": "Text size in millimeters",
                    "minimum": 0.1,
                    "default": 1.0,
                },
                "thickness": {
                    "type": "number",
                    "description": "Text thickness in millimeters",
                    "minimum": 0.01,
                    "default": 0.15,
                },
            },
            "required": ["text", "x", "y"],
        },
    },
]

# =============================================================================
# COMPONENT TOOLS
# =============================================================================

COMPONENT_TOOLS = [
    {
        "name": "place_component",
        "title": "Place Component",
        "description": "Places a component with specified footprint at given coordinates on the board.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "reference": {
                    "type": "string",
                    "description": "Component reference designator (e.g., R1, C2, U3)",
                },
                "footprint": {
                    "type": "string",
                    "description": "Footprint library:name (e.g., Resistor_SMD:R_0805_2012Metric)",
                },
                "x": {"type": "number", "description": "X coordinate in millimeters"},
                "y": {"type": "number", "description": "Y coordinate in millimeters"},
                "rotation": {
                    "type": "number",
                    "description": "Rotation angle in degrees (0-360)",
                    "minimum": 0,
                    "maximum": 360,
                    "default": 0,
                },
                "layer": {
                    "type": "string",
                    "enum": ["F.Cu", "B.Cu"],
                    "description": "Board layer (top or bottom)",
                    "default": "F.Cu",
                },
            },
            "required": ["reference", "footprint", "x", "y"],
        },
    },
    {
        "name": "move_component",
        "title": "Move Component",
        "description": "Moves an existing component to a new position on the board.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "reference": {
                    "type": "string",
                    "description": "Component reference designator",
                },
                "x": {
                    "type": "number",
                    "description": "New X coordinate in millimeters",
                },
                "y": {
                    "type": "number",
                    "description": "New Y coordinate in millimeters",
                },
            },
            "required": ["reference", "x", "y"],
        },
    },
    {
        "name": "rotate_component",
        "title": "Rotate Component",
        "description": "Rotates a component by specified angle. Rotation is cumulative with existing rotation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "reference": {
                    "type": "string",
                    "description": "Component reference designator",
                },
                "angle": {
                    "type": "number",
                    "description": "Rotation angle in degrees (positive = counterclockwise)",
                },
            },
            "required": ["reference", "angle"],
        },
    },
    {
        "name": "delete_component",
        "title": "Delete Component",
        "description": "Removes a component from the board.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "reference": {
                    "type": "string",
                    "description": "Component reference designator",
                }
            },
            "required": ["reference"],
        },
    },
    {
        "name": "edit_component",
        "title": "Edit Component Properties",
        "description": "Modifies properties of an existing component (value, footprint, etc.).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "reference": {
                    "type": "string",
                    "description": "Component reference designator",
                },
                "value": {"type": "string", "description": "New component value"},
                "footprint": {
                    "type": "string",
                    "description": "New footprint library:name",
                },
            },
            "required": ["reference"],
        },
    },
    {
        "name": "get_component_properties",
        "title": "Get Component Properties",
        "description": "Retrieves detailed properties of a specific component.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "reference": {
                    "type": "string",
                    "description": "Component reference designator",
                }
            },
            "required": ["reference"],
        },
    },
    {
        "name": "get_component_list",
        "title": "List All Components",
        "description": "Returns a list of all components on the board with their properties.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "find_component",
        "title": "Find Components",
        "description": "Searches for components matching specified criteria. Supports partial matching on reference, value, or footprint patterns.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "reference": {
                    "type": "string",
                    "description": "Reference designator pattern to match (e.g., 'R1', 'U', 'C2')",
                },
                "value": {
                    "type": "string",
                    "description": "Value pattern to match (e.g., '10k', '100nF')",
                },
                "footprint": {
                    "type": "string",
                    "description": "Footprint pattern to match (e.g., '0805', 'SOIC')",
                },
            },
        },
    },
    {
        "name": "get_component_pads",
        "title": "Get Component Pads",
        "description": (
            "Returns all pads for a component with positions, net "
            "connections, sizes, shapes, and copper layers. Each pad's "
            "`layers` field lists which Cu layer(s) the pad sits on — "
            "SMD pads return a single layer (F.Cu or B.Cu); routing on "
            "the wrong side produces a dangling track + unconnected_items "
            "DRC error. Through-hole/NPTH pads return the full Cu stack."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "reference": {
                    "type": "string",
                    "description": "Component reference designator (e.g., U1, R5)",
                }
            },
            "required": ["reference"],
        },
    },
    {
        "name": "get_pad_position",
        "title": "Get Pad Position",
        "description": "Returns the position and properties of a specific pad on a component.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "reference": {
                    "type": "string",
                    "description": "Component reference designator",
                },
                "pad": {
                    "type": "string",
                    "description": "Pad number or name (e.g., '1', '2', 'A1')",
                },
                "unit": {
                    "type": "string",
                    "enum": ["mm", "inch"],
                    "description": "Unit for coordinates (default: mm)",
                },
            },
            "required": ["reference", "pad"],
        },
    },
    {
        "name": "place_component_array",
        "title": "Place Component Array",
        "description": "Places multiple copies of a component in a grid or circular pattern.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "referencePrefix": {
                    "type": "string",
                    "description": "Reference prefix (e.g., 'R' for R1, R2, R3...)",
                },
                "startNumber": {
                    "type": "integer",
                    "description": "Starting number for references",
                    "minimum": 1,
                    "default": 1,
                },
                "footprint": {
                    "type": "string",
                    "description": "Footprint library:name",
                },
                "pattern": {
                    "type": "string",
                    "enum": ["grid", "circular"],
                    "description": "Array pattern type",
                },
                "count": {
                    "type": "integer",
                    "description": "Total number of components to place",
                    "minimum": 1,
                },
                "startX": {
                    "type": "number",
                    "description": "Starting X coordinate in millimeters",
                },
                "startY": {
                    "type": "number",
                    "description": "Starting Y coordinate in millimeters",
                },
                "spacingX": {
                    "type": "number",
                    "description": "Horizontal spacing in mm (for grid pattern)",
                },
                "spacingY": {
                    "type": "number",
                    "description": "Vertical spacing in mm (for grid pattern)",
                },
                "radius": {
                    "type": "number",
                    "description": "Circle radius in mm (for circular pattern)",
                },
                "rows": {
                    "type": "integer",
                    "description": "Number of rows (for grid pattern)",
                    "minimum": 1,
                },
                "columns": {
                    "type": "integer",
                    "description": "Number of columns (for grid pattern)",
                    "minimum": 1,
                },
            },
            "required": [
                "referencePrefix",
                "footprint",
                "pattern",
                "count",
                "startX",
                "startY",
            ],
        },
    },
    {
        "name": "align_components",
        "title": "Align Components",
        "description": "Aligns multiple components horizontally or vertically.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "references": {
                    "type": "array",
                    "description": "Array of component reference designators to align",
                    "items": {"type": "string"},
                    "minItems": 2,
                },
                "direction": {
                    "type": "string",
                    "enum": ["horizontal", "vertical"],
                    "description": "Alignment direction",
                },
                "spacing": {
                    "type": "number",
                    "description": "Spacing between components in mm (optional, for even distribution)",
                },
            },
            "required": ["references", "direction"],
        },
    },
    {
        "name": "duplicate_component",
        "title": "Duplicate Component",
        "description": "Creates a copy of an existing component with new reference designator.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sourceReference": {
                    "type": "string",
                    "description": "Reference of component to duplicate",
                },
                "newReference": {
                    "type": "string",
                    "description": "Reference designator for the new component",
                },
                "offsetX": {
                    "type": "number",
                    "description": "X offset from original position in mm",
                    "default": 0,
                },
                "offsetY": {
                    "type": "number",
                    "description": "Y offset from original position in mm",
                    "default": 0,
                },
            },
            "required": ["sourceReference", "newReference"],
        },
    },
]

# =============================================================================
# ROUTING TOOLS
# =============================================================================

ROUTING_TOOLS = [
    {
        "name": "add_net",
        "title": "Create Electrical Net",
        "description": "Creates a new electrical net for signal routing.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "netName": {
                    "type": "string",
                    "description": "Name of the net (e.g., VCC, GND, SDA)",
                    "minLength": 1,
                },
                "netClass": {
                    "type": "string",
                    "description": "Optional net class to assign (must exist first)",
                },
            },
            "required": ["netName"],
        },
    },
    {
        "name": "route_trace",
        "title": "Route PCB Trace",
        "description": (
            "Routes a single copper trace segment between two XY points on a "
            "fixed layer. Refuses by default (checkObstacles) when the proposed "
            "trace would cross or come within clearance of foreign-net copper. "
            "The obstacle check is width- and clearance-aware: it inflates the "
            "trace centerline by half its width plus the net's netclass "
            "clearance (overridable with `clearance`), so an edge-clipping "
            "case where a fat trace exits an IC pin grazes the neighbouring "
            "pad is caught. Does not handle layer changes — use "
            "route_pad_to_pad for inter-layer pad-to-pad routes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "start": {
                    "type": "object",
                    "description": "Start position",
                    "properties": {
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "unit": {"type": "string"},
                    },
                    "required": ["x", "y"],
                },
                "end": {
                    "type": "object",
                    "description": "End position",
                    "properties": {
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "unit": {"type": "string"},
                    },
                    "required": ["x", "y"],
                },
                "layer": {
                    "type": "string",
                    "description": "PCB layer (e.g., F.Cu, B.Cu, In1.Cu)",
                    "default": "F.Cu",
                },
                "width": {
                    "type": "number",
                    "description": "Trace width in mm",
                    "minimum": 0.05,
                },
                "net": {
                    "type": "string",
                    "description": "Net name for this trace",
                },
                "checkObstacles": {
                    "type": "boolean",
                    "description": (
                        "Refuse the route if the swept trace (width + "
                        "clearance) would touch foreign-net tracks, vias or "
                        "pads (default: true). Set false to force the trace "
                        "— useful when restoring a deleted segment by "
                        "coordinates."
                    ),
                },
                "clearance": {
                    "type": "number",
                    "description": (
                        "Minimum gap in mm between the trace edge and any "
                        "foreign-net copper (only used when checkObstacles "
                        "is true). Defaults to the net's netclass clearance, "
                        "falling back to the board default."
                    ),
                    "minimum": 0,
                },
            },
            "required": ["start", "end", "layer", "width", "net"],
        },
    },
    {
        "name": "find_via_lane",
        "title": "Find Via Lane",
        "description": (
            "Propose a via-jumper route when the direct same-layer path is "
            "blocked by foreign-net copper. Tries (in order): direct on "
            "fromLayer → straight via-jumper on viaLayer → single "
            "perpendicular-offset waypoint → 2D grid waypoint search → "
            "axis-aligned L-shape (blind sweep) → obstacle-bbox-aware "
            "L-shape (v3, escapes long blockers a blind sweep can't). "
            "Returns the first strategy that clears. Default is preview "
            "(proposed segments + via points); pass apply=true to commit."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "from": {
                    "type": "object",
                    "description": (
                        "Source: either {x, y, unit} XY or {ref, pad} pad lookup."
                    ),
                },
                "to": {
                    "type": "object",
                    "description": (
                        "Target: either {x, y, unit} XY or {ref, pad} pad lookup."
                    ),
                },
                "net": {
                    "type": "string",
                    "description": "Net name (required).",
                },
                "fromLayer": {
                    "type": "string",
                    "description": "Primary layer (default F.Cu).",
                },
                "viaLayer": {
                    "type": "string",
                    "description": "Layer to jump through (default B.Cu).",
                },
                "width": {
                    "type": "number",
                    "description": "Trace width in mm (default 0.2).",
                },
                "viaDiameter": {
                    "type": "number",
                    "description": "Via outer diameter mm (default 0.6).",
                },
                "viaDrill": {
                    "type": "number",
                    "description": "Via drill mm (default 0.3).",
                },
                "safetyMargin": {
                    "type": "number",
                    "description": (
                        "Pull-back from first obstacle on fromLayer when "
                        "placing vias, mm (default 0.5)."
                    ),
                },
                "minimumStubLength": {
                    "type": "number",
                    "description": (
                        "Refuse when the safe via insertion point is closer "
                        "than this distance to source or target pad, mm. "
                        "Set ≥1 mm when source/target are component pads to "
                        "keep vias out of the pad footprint. Default 0 (off)."
                    ),
                },
                "minClearance": {
                    "type": "number",
                    "description": (
                        "Minimum gap (mm) between the proposed via's edge "
                        "and any foreign-net copper on any layer. Refuses "
                        "with `via_clearance_violation` if the via diameter "
                        "would overlap or come within this distance of an "
                        "adjacent pad/via/track. Default 0.15."
                    ),
                },
                "waypointSearchMax": {
                    "type": "number",
                    "description": (
                        "Max perpendicular offset for waypoint search, mm "
                        "(default 10)."
                    ),
                },
                "apply": {
                    "type": "boolean",
                    "description": (
                        "Commit the proposed route (default false = preview)."
                    ),
                },
            },
            "required": ["from", "to", "net"],
        },
    },
    {
        "name": "check_route_segment",
        "title": "Check Route Segment",
        "description": (
            "Pre-flight check: would a straight segment from start to end on "
            "the given layer cross foreign-net copper? Returns "
            "{clear, obstacles[]} without committing the route. Same "
            "obstacle detection as route_trace's default checkObstacles — "
            "use this to enumerate candidate paths before committing one."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "start": {
                    "type": "object",
                    "description": "Start position",
                    "properties": {
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "unit": {"type": "string"},
                    },
                    "required": ["x", "y"],
                },
                "end": {
                    "type": "object",
                    "description": "End position",
                    "properties": {
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "unit": {"type": "string"},
                    },
                    "required": ["x", "y"],
                },
                "layer": {
                    "type": "string",
                    "description": "PCB layer (e.g., F.Cu, B.Cu)",
                    "default": "F.Cu",
                },
                "net": {
                    "type": "string",
                    "description": (
                        "Net name you intend to route — same-net copper "
                        "isn't counted as an obstacle."
                    ),
                },
                "width": {
                    "type": "number",
                    "description": (
                        "Planned trace width in mm. Inflates the obstacle "
                        "check by half this width so edge-clipping is "
                        "caught. Defaults to the board's current track "
                        "width."
                    ),
                    "minimum": 0,
                },
                "clearance": {
                    "type": "number",
                    "description": (
                        "Minimum gap in mm between the trace edge and "
                        "foreign-net copper. Defaults to the net's netclass "
                        "clearance, falling back to the board default."
                    ),
                    "minimum": 0,
                },
            },
            "required": ["start", "end", "layer", "net"],
        },
    },
    {
        "name": "add_via",
        "title": "Add Via",
        "description": "Adds a via (plated through-hole) to connect traces between layers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": {"type": "number", "description": "X coordinate in millimeters"},
                "y": {"type": "number", "description": "Y coordinate in millimeters"},
                "diameter": {
                    "type": "number",
                    "description": "Via diameter in millimeters",
                    "minimum": 0.1,
                },
                "drill": {
                    "type": "number",
                    "description": "Drill diameter in millimeters",
                    "minimum": 0.1,
                },
                "netName": {
                    "type": "string",
                    "description": "Net name to assign to this via",
                },
            },
            "required": ["x", "y", "diameter", "drill"],
        },
    },
    {
        "name": "bridge_same_net_pins",
        "title": "Bridge Same-Net Pins With Zone",
        "description": (
            "Create a small filled zone covering two same-net pads, "
            "replacing a thin sub-min-width trace that would violate "
            "the POWER netclass track-width rule. The zone uses solid "
            "(direct) connection by default — appropriate for "
            "current-carrying bridges where thermal-relief spokes "
            "would bottleneck. Both pads must already be on the same "
            "net. Default preview; pass apply=true to commit."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "padA": {
                    "type": "object",
                    "description": "First pad: {ref, pad}.",
                    "properties": {
                        "ref": {"type": "string"},
                        "pad": {"type": ["string", "number"]},
                    },
                    "required": ["ref", "pad"],
                },
                "padB": {
                    "type": "object",
                    "description": "Second pad: {ref, pad}.",
                    "properties": {
                        "ref": {"type": "string"},
                        "pad": {"type": ["string", "number"]},
                    },
                    "required": ["ref", "pad"],
                },
                "layer": {
                    "type": "string",
                    "description": "Copper layer (default 'F.Cu').",
                },
                "marginMm": {
                    "type": "number",
                    "description": (
                        "Margin in mm around the pad-union bbox "
                        "(default 0.1)."
                    ),
                    "minimum": 0,
                },
                "connection": {
                    "type": "string",
                    "enum": ["solid", "thermal"],
                    "description": (
                        "Pad connection mode (default 'solid' — full "
                        "bond, no thermal-relief spokes)."
                    ),
                },
                "apply": {
                    "type": "boolean",
                    "description": (
                        "Commit the zone (default false = preview "
                        "outline only)."
                    ),
                },
            },
            "required": ["padA", "padB"],
        },
    },
    {
        "name": "pair_via",
        "title": "Pair Via for High-Current Doubling",
        "description": (
            "Propose (and optionally apply) a parallel partner via "
            "next to every existing via on the given net(s). Doubles "
            "the current-carrying capacity and ~halves inductance for "
            "high-current power vias. Default: nets matching the "
            "POWER_4A netclass. The first ±x/±y offset that clears "
            "`minClearance` from foreign-net copper wins; vias with "
            "no clear offset are reported as `skippedNoClearance`."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "nets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Explicit net list (e.g. [\"BAT+\",\"BAT-\"]). "
                        "Overrides netClass when set."
                    ),
                },
                "netClass": {
                    "type": "string",
                    "description": (
                        "Netclass name to filter by (default "
                        "\"POWER_4A\"). Ignored if `nets` is set."
                    ),
                },
                "offset": {
                    "type": "number",
                    "description": (
                        "Distance (mm) from the parent via center to "
                        "the partner via (default 1.0)."
                    ),
                    "minimum": 0.1,
                },
                "minClearance": {
                    "type": "number",
                    "description": (
                        "Minimum gap (mm) between the partner via edge "
                        "and foreign-net copper (default 0.2)."
                    ),
                    "minimum": 0,
                },
                "viaDiameter": {
                    "type": "number",
                    "description": (
                        "Partner via diameter (mm). Defaults to the "
                        "parent via's diameter."
                    ),
                    "minimum": 0.1,
                },
                "viaDrill": {
                    "type": "number",
                    "description": (
                        "Partner via drill (mm). Defaults to the parent "
                        "via's drill."
                    ),
                    "minimum": 0.1,
                },
                "apply": {
                    "type": "boolean",
                    "description": (
                        "Commit the proposed partner vias (default "
                        "false = preview)."
                    ),
                },
                "maxPairs": {
                    "type": "number",
                    "description": (
                        "Safety cap on the number of partners to "
                        "propose (default 200)."
                    ),
                    "minimum": 1,
                },
            },
        },
    },
    {
        "name": "stitch_pour_vias",
        "title": "Stitch Pour Vias",
        "description": (
            "Propose (and optionally apply) a grid of stitching vias "
            "on a copper pour net. Each candidate must sit inside a "
            "filled zone on the net, clear `minClearance` from any "
            "foreign-net copper on any layer, and not duplicate an "
            "existing same-net via. Through-via, F.Cu ↔ B.Cu."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "net": {
                    "type": "string",
                    "description": (
                        "Net to stitch (must have at least one zone)."
                    ),
                },
                "gridPitch": {
                    "type": "number",
                    "description": "Grid spacing between candidate vias, mm.",
                    "minimum": 0.1,
                },
                "viaDiameter": {
                    "type": "number",
                    "description": "Via outer diameter, mm (default 0.6).",
                    "minimum": 0.1,
                },
                "viaDrill": {
                    "type": "number",
                    "description": "Via drill diameter, mm (default 0.3).",
                    "minimum": 0.1,
                },
                "minClearance": {
                    "type": "number",
                    "description": (
                        "Minimum gap mm between via edge and foreign-net "
                        "copper on any layer (default 0.2)."
                    ),
                    "minimum": 0,
                },
                "apply": {
                    "type": "boolean",
                    "description": (
                        "Commit the proposed vias (default false = preview)."
                    ),
                },
                "maxVias": {
                    "type": "number",
                    "description": (
                        "Safety cap on the number of vias proposed "
                        "(default 200)."
                    ),
                    "minimum": 1,
                },
            },
            "required": ["net", "gridPitch"],
        },
    },
    {
        "name": "widen_return_paths",
        "title": "Widen Return Paths",
        "description": (
            "Widen the GND/return-net stubs of high-current components. "
            "Walks each return-net pad's traces along the same net until "
            "hitting a same-net via, then widens cleared segments to the "
            "high-current netclass width. Each candidate is clearance-"
            "checked via swept-trace test; segments that would short are "
            "skipped. Optional pairedVias adds an in-line partner past "
            "each stub's terminating via."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "netClass": {
                    "type": "string",
                    "description": (
                        "Netclass identifying high-current components "
                        "(default 'POWER_4A')."
                    ),
                },
                "returnNets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Nets to widen (default ['GND'])."
                    ),
                },
                "width": {
                    "type": "number",
                    "description": (
                        "Explicit target width mm. Default: netclass "
                        "track width."
                    ),
                    "minimum": 0.05,
                },
                "minClearance": {
                    "type": "number",
                    "description": (
                        "Minimum clearance mm against foreign-net copper "
                        "(default 0.15)."
                    ),
                    "minimum": 0,
                },
                "pairedVias": {
                    "type": "boolean",
                    "description": (
                        "Place an in-line partner via past each stub's "
                        "terminating via (default false)."
                    ),
                },
                "apply": {
                    "type": "boolean",
                    "description": (
                        "Commit the widening (default false = preview)."
                    ),
                },
            },
        },
    },
    {
        "name": "via_orphan_pads",
        "title": "Via Orphan Pads",
        "description": (
            "Drop a via adjacent to every F.Cu/B.Cu pad on a plane net "
            "(GND, BAT+, etc.) that isn't already connected to a same-net "
            "via or track. Post-autoroute step: freerouting respects "
            "(type power) plane layers by NOT placing landing vias on "
            "them, leaving SMD pads floating relative to the inner pour. "
            "Via-near-pad with a short stub trace; no via-in-pad."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "net": {
                    "type": "string",
                    "description": (
                        "Plane net to via (e.g. 'GND', 'BAT+')."
                    ),
                },
                "layer": {
                    "type": "string",
                    "description": (
                        "Which pad side to target: 'F.Cu', 'B.Cu', or "
                        "'both' (default 'F.Cu')."
                    ),
                },
                "viaDiameter": {
                    "type": "number",
                    "description": "Via outer diameter mm (default 0.6).",
                    "minimum": 0.1,
                },
                "viaDrill": {
                    "type": "number",
                    "description": "Via drill mm (default 0.3).",
                    "minimum": 0.1,
                },
                "viaOffset": {
                    "type": "number",
                    "description": (
                        "Gap between pad edge and via edge mm (default 0.6)."
                    ),
                    "minimum": 0,
                },
                "stubWidth": {
                    "type": "number",
                    "description": (
                        "Width of stub trace from pad to via mm (default "
                        "0.25)."
                    ),
                    "minimum": 0.05,
                },
                "minClearance": {
                    "type": "number",
                    "description": (
                        "Minimum gap mm between via edge and foreign-net "
                        "copper (default 0.15)."
                    ),
                    "minimum": 0,
                },
                "apply": {
                    "type": "boolean",
                    "description": (
                        "Commit the vias (default false = preview)."
                    ),
                },
                "maxVias": {
                    "type": "number",
                    "description": "Cap on proposed vias (default 200).",
                    "minimum": 1,
                },
            },
            "required": ["net"],
        },
    },
    {
        "name": "dedupe_traces",
        "title": "Dedupe Traces",
        "description": (
            "Remove exact-duplicate tracks (and optionally vias) left over "
            "from autoroute SES re-imports or scripted re-routes. Two "
            "tracks match if they share (layer, width, net) and their "
            "endpoints coincide in either order; vias match by (position, "
            "drill, width, net). Default is dry-run — pass apply=true to "
            "actually delete the extras."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "apply": {
                    "type": "boolean",
                    "description": "Actually delete duplicates (default false = dry-run preview).",
                },
                "net": {
                    "type": "string",
                    "description": "Optional net filter — only dedupe tracks on this net.",
                },
                "includeVias": {
                    "type": "boolean",
                    "description": "Also dedupe vias (default true).",
                },
            },
        },
    },
    {
        "name": "delete_trace",
        "title": "Delete Trace",
        "description": "Removes traces from the board. Can delete by UUID, position, or bulk-delete all traces on a net.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "traceUuid": {
                    "type": "string",
                    "description": "UUID of a specific trace to delete",
                },
                "position": {
                    "type": "object",
                    "description": "Delete trace nearest to this position",
                    "properties": {
                        "x": {"type": "number", "description": "X coordinate"},
                        "y": {"type": "number", "description": "Y coordinate"},
                        "unit": {
                            "type": "string",
                            "enum": ["mm", "inch"],
                            "default": "mm",
                        },
                    },
                    "required": ["x", "y"],
                },
                "net": {
                    "type": "string",
                    "description": 'Delete all traces on this net (bulk delete). Pass "*" to delete every track on the board.',
                },
                "layer": {
                    "type": "string",
                    "description": "Filter by layer when using net-based deletion",
                },
                "includeVias": {
                    "type": "boolean",
                    "description": 'Include vias in net-based deletion (use with net="*" to strip the whole board)',
                    "default": False,
                },
            },
        },
    },
    {
        "name": "query_traces",
        "title": "Query Traces",
        "description": "Queries traces on the board with optional filters by net, layer, or bounding box. Returns trace details including UUID, positions, width, and length.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "net": {
                    "type": "string",
                    "description": "Filter by net name (e.g., 'GND', 'VCC')",
                },
                "layer": {
                    "type": "string",
                    "description": "Filter by layer name (e.g., 'F.Cu', 'B.Cu')",
                },
                "boundingBox": {
                    "type": "object",
                    "description": "Filter by bounding box region",
                    "properties": {
                        "x1": {"type": "number", "description": "Left X coordinate"},
                        "y1": {"type": "number", "description": "Top Y coordinate"},
                        "x2": {"type": "number", "description": "Right X coordinate"},
                        "y2": {"type": "number", "description": "Bottom Y coordinate"},
                        "unit": {
                            "type": "string",
                            "enum": ["mm", "inch"],
                            "default": "mm",
                        },
                    },
                },
                "includeVias": {
                    "type": "boolean",
                    "description": "Also return vias (with their UUIDs) in a separate 'vias' array. Needed to get via UUIDs for reliable delete_trace by UUID.",
                    "default": False,
                },
            },
        },
    },
    {
        "name": "audit_plane_cuts",
        "title": "Audit Plane Cuts",
        "description": "Report signal traces routed on inner copper layers that double as power/GND planes (e.g. In1.Cu GND, In2.Cu PWR). Long traces there break image-current return paths above F.Cu signals. Returns per-net total cut length, per-layer breakdown, and the longest single-trace offenders sorted for ripup + retry on an outer layer.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "layers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": 'Inner layers to audit (default ["In1.Cu", "In2.Cu"])',
                },
                "minLength": {
                    "type": "number",
                    "description": "Minimum trace length (mm) to include; filters short via-fanout stubs",
                    "default": 1.0,
                },
                "unit": {
                    "type": "string",
                    "enum": ["mm", "inch"],
                    "description": "Length unit (default mm)",
                    "default": "mm",
                },
            },
        },
    },
    {
        "name": "relax_placement",
        "title": "Relax Placement (Force-Directed, v2 Unified)",
        "description": "v2 unified force-directed PCB autoplacer (shares the schematic autoplacer engine). Pin-wise springs (strength per spring class: DECOUPLING strong, LOCAL_SIGNAL default, INTER_GROUP weak, PLANE zero) + OBB inverse-cube body repulsion. Anchored components (J*/SW*/BAT* and through-hole-dominant by default; override with lockedRefs) stay fixed. Schedule: spread (repulsion ramps in) -> snap (rotation snap ramps in) -> hard-snap. Power-plane nets auto-classify as PLANE.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "lockedRefs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Refs to keep fixed (overrides default).",
                },
                "marginMm": {"type": "number", "description": "Body-repulsion margin (default 1.0 mm).", "default": 1.0},
                "springK": {"type": "number", "description": "Base attraction spring constant (default 1.0).", "default": 1.0},
                "repulsionKStart": {"type": "number", "description": "Repulsion strength at start of spread phase (default 1e-6). Ramps geometrically to repulsionKPeak under the inverse-cube formula; a small start gives a gentler early spread.", "default": 1e-6},
                "repulsionKPeak": {"type": "number", "description": "Peak repulsion strength at end of spread phase (default 0.1 under the inverse-cube formula; the historical cubic-ramp default was 30.0).", "default": 0.1},
                "rotationSnapPeak": {"type": "number", "description": "Peak rotation-snap torque (default 30.0). Set 0 for free rotation.", "default": 30.0},
                "pinwiseTorqueK": {"type": "number", "description": "Lever-arm torque coupling for pin-wise springs (default 1.0). Without this, off-center spring forces don't rotate components.", "default": 1.0},
                "clusterIters": {"type": "number", "description": "Phase 1 (springs only) iterations (default 50 — letting the spring topology settle before repulsion acts puts parts on the correct side of their IC far more reliably).", "default": 50},
                "spreadIters": {"type": "number", "description": "Phase 2 (repulsion ramps in) iterations (default 200).", "default": 200},
                "snapIters": {"type": "number", "description": "Phase 3 (rotation snap ramps in) iterations (default 100).", "default": 100},
                "relaxIters": {"type": "number", "description": "Phase 4 (full snap, low temp) iterations (default 0 — skipped because the hard-snap step gives a cleaner final state).", "default": 0},
                "crossLayerSprings": {"type": "boolean", "description": "Apply springs between pads on different copper layers (default true). Set false when a B.Cu anchor (cell holder) shouldn't pull F.Cu parts onto its pads.", "default": True},
                "forceStepDamping": {"type": "number", "description": "Damping factor on the force-as-displacement step (default 0.3). Below 1.0 prevents period-2 oscillation when components are near equilibrium and force < temperature.", "default": 0.3},
                "normalizeSpringForceByDegree": {"type": "boolean", "description": "Divide each component's spring force/torque sum by its number of spring contributions (default true). Without this, K_eff scales with N pins — a 28-pin IC has 14x the restoring stiffness of a 2-pin resistor and bucks in dense clusters under the damping that's critical for the resistor.", "default": True},
                "normalizeByIntentGroup": {"type": "boolean", "description": "Alternative spring normalization (default false). Three-level average: within each intent group (connections sharing a matched annotation target), then across a pin's groups, then across the component's pins. A named DECOUPLING target gets its own group so it competes on equal footing with the rest of a high-fan-out net (fixing decoupling caps mis-oriented by power-rail pull) without needing PLANE. Supersedes normalizeSpringForceByDegree when true.", "default": False},
                "boundaryK": {"type": "number", "description": "Soft boundary force during iteration when a component drifts past the Edge.Cuts keep-in (default 1.0). Linear restoring force; complementary to the final post-clamp.", "default": 1.0},
                "enforceRotationSnap": {"type": "boolean", "description": "After the soft snap phase, hard-round each non-anchored rotation to the nearest 90° multiple (default true). Soft snap pulls close but residual lever-arm torque holds some components slightly askew.", "default": True},
                "autoClassifyPlanes": {"type": "boolean", "description": "Auto-classify power/ground nets as PLANE (default true).", "default": True},
                "dryRun": {"type": "boolean", "description": "Compute without applying.", "default": False},
                "boardPath": {"type": "string", "description": "Path to .kicad_pcb."},
            },
        },
    },
    {
        "name": "get_ratsnest",
        "title": "Get Ratsnest",
        "description": "Read-only inspection of the ratsnest (unrouted pad-pair connections). Returns per-segment endpoints with parsed refs/pads, net, length, plus pairwise geometric crossing detection on different nets. Source: DRC unconnected_items cache from a prior get_drc_violations/run_drc call (auto-discovered).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "netFilter": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Restrict to these nets.",
                },
                "refFilter": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Restrict to segments touching these refs.",
                },
                "includeSegments": {
                    "type": "boolean",
                    "description": "Emit per-segment list (default true).",
                    "default": True,
                },
                "includeCrossings": {
                    "type": "boolean",
                    "description": "Detect segment crossings on different nets (default true, O(N²)).",
                    "default": True,
                },
                "maxSegments": {
                    "type": "number",
                    "description": "Cap on segment list size. Default 1000.",
                    "default": 1000,
                },
                "topNCrossingsPerRef": {
                    "type": "number",
                    "description": "Cap on per-ref crossing-contributors list. Default 10.",
                    "default": 10,
                },
                "drcViolationsPath": {
                    "type": "string",
                    "description": "Explicit path to DRC JSON. Defaults to project-dir cache.",
                },
                "boardPath": {
                    "type": "string",
                    "description": "Path to .kicad_pcb. Defaults to currently-loaded board.",
                },
            },
        },
    },
    {
        "name": "analyze_congestion",
        "title": "Analyze Routing Congestion",
        "description": "Grid-based routing-congestion analyzer. Divides the board into cells (default 5 mm) and computes per-cell pad density × ratsnest density. Returns top-N hotspots (each with member components — those are the candidates to move) plus per-net difficulty (max cell score along the ratsnest, so you can prioritise nets most likely to need re-placement). Each hotspot also carries `pad_count_by_layer` so the per-side breakdown is visible without a second call. Pass `layer` (e.g. \"F.Cu\") to filter pad density to that layer so the score reflects pressure on the side you intend to route on. Read-only. Ratsnest data comes from the DRC unconnected_items cache — run get_drc_violations or run_drc first; the tool auto-finds the default cache JSON.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cellSizeMm": {
                    "type": "number",
                    "description": "Grid cell size in mm. Smaller = finer but noisier. Default 5.0.",
                    "default": 5.0,
                },
                "topN": {
                    "type": "number",
                    "description": "Number of hotspot cells to return. Default 15.",
                    "default": 15,
                },
                "netDifficultyTopN": {
                    "type": "number",
                    "description": "Cap on the per-net difficulty list. Default 20.",
                    "default": 20,
                },
                "layer": {
                    "type": "string",
                    "description": "Copper layer name (e.g. \"F.Cu\", \"B.Cu\", \"In1.Cu\"). When set, pad-density is filtered to that layer so the score reflects routing pressure on that side. PTH pads count toward every copper layer. Omit for the legacy any-layer score.",
                },
                "drcViolationsPath": {
                    "type": "string",
                    "description": "Explicit path to a DRC violations JSON. Defaults to the project-dir cache file from a prior get_drc_violations/run_drc call.",
                },
                "boardPath": {
                    "type": "string",
                    "description": "Path to .kicad_pcb. Defaults to currently-loaded board.",
                },
            },
        },
    },
    {
        "name": "check_pcb_integrity",
        "title": "Check PCB Integrity",
        "description": "Silent-corruption detector that runs subchecks DRC will NOT catch: pad_rotation (multi-instance per-pad orientation drift; uniform=warning, mixed=error), footprint_overlap (centre inside another's bbox on same layer), stacked_pads (≥2 differently-numbered pads on different nets at the same XY).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "checks": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["pad_rotation", "footprint_overlap", "stacked_pads"],
                    },
                    "description": "Subset of subchecks to run. Default: all three.",
                },
                "boardPath": {
                    "type": "string",
                    "description": "Path to .kicad_pcb. Defaults to currently-loaded board.",
                },
            },
        },
    },
    {
        "name": "verify_netclass_patterns",
        "title": "Verify Netclass Patterns",
        "description": (
            "Compare the live net_settings.netclass_patterns in "
            ".kicad_pro against the expected set captured in the "
            "mcp_expected_netclass_patterns section (which KiCAD's GUI "
            "leaves alone). KiCAD has been observed to silently strip "
            "patterns on save when normalising the project across "
            "version upgrades — this catches that drift. First call on "
            "a project bootstraps the expected section from the "
            "current state (no drift reported). Pass restore=true to "
            "re-add missing patterns back into net_settings."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "proPath": {
                    "type": "string",
                    "description": (
                        "Path to the .kicad_pro. Defaults to the "
                        "currently-loaded board's sibling .kicad_pro."
                    ),
                },
                "restore": {
                    "type": "boolean",
                    "description": (
                        "Re-add missing patterns into "
                        "net_settings.netclass_patterns. Default false "
                        "(report-only)."
                    ),
                    "default": False,
                },
            },
        },
    },
    {
        "name": "decoupling_audit",
        "title": "Decoupling Audit",
        "description": "Find decoupling caps (or any constrained component) too far from their target IC pin on the PCB. Reads Placement_Anchor properties on schematic symbols AND auto-discovers cap↔IC power-pin pairs by net analysis. Property grammar: 'Placement_Anchor = \"U1.10/within=3mm[; U1.9/within=3mm]\"'. The property lives on the SCHEMATIC symbol (PCB sync drops most custom props). Controlled by 'mcp_constraint_version: 1' in .kicad_pro.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Path to the top-level .kicad_sch (hierarchical sub-sheets are auto-walked).",
                },
                "boardPath": {
                    "type": "string",
                    "description": "Path to the .kicad_pcb. Defaults to the currently-loaded board.",
                },
                "maxDist": {
                    "type": "number",
                    "description": "Default max distance (mm) for auto-discovered pairs. Default 5.0.",
                    "default": 5.0,
                },
                "includeAutoDiscovered": {
                    "type": "boolean",
                    "description": "Auto-discover cap↔IC pairs via net analysis. Default true.",
                    "default": True,
                },
                "includeExplicit": {
                    "type": "boolean",
                    "description": "Honor explicit Placement_Anchor properties. Default true.",
                    "default": True,
                },
            },
            "required": ["schematicPath"],
        },
    },
    {
        "name": "place_near",
        "title": "Place Near",
        "description": "Snap PCB footprints to within maxDist mm of a target pad/footprint, respecting bbox collisions AND foreign-net tracks (within clearanceMargin mm). Pairs with decoupling_audit.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Component references to move (e.g. ['C9', 'C10']).",
                },
                "target": {
                    "type": "string",
                    "description": "Anchor as 'REF' (footprint body) or 'REF.PIN' (specific pad). Example: 'U1.10'.",
                },
                "maxDist": {
                    "type": "number",
                    "description": "Max distance in mm. Default 5.0.",
                    "default": 5.0,
                },
                "skipIfWithin": {
                    "type": "boolean",
                    "description": "Leave components already within maxDist alone. Default true.",
                    "default": True,
                },
                "clearanceMargin": {
                    "type": "number",
                    "description": "Safety margin (mm) added to each pad bbox before checking foreign-net tracks. Default 0.15 (clears the POWER_2A 0.13 mm rule with 20 µm).",
                    "default": 0.15,
                },
                "boardPath": {
                    "type": "string",
                    "description": "Path to .kicad_pcb. Defaults to currently-loaded board.",
                },
                "savePath": {
                    "type": "string",
                    "description": "Path to save the modified board. Defaults to boardPath.",
                },
            },
            "required": ["refs", "target"],
        },
    },
    {
        "name": "modify_trace",
        "title": "Modify Trace",
        "description": "Modifies properties of an existing trace. Find trace by UUID or position, then change width, layer, or net assignment.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "uuid": {
                    "type": "string",
                    "description": "UUID of the trace to modify",
                },
                "position": {
                    "type": "object",
                    "description": "Find trace nearest to this position",
                    "properties": {
                        "x": {"type": "number", "description": "X coordinate"},
                        "y": {"type": "number", "description": "Y coordinate"},
                        "unit": {
                            "type": "string",
                            "enum": ["mm", "inch"],
                            "default": "mm",
                        },
                    },
                    "required": ["x", "y"],
                },
                "width": {"type": "number", "description": "New trace width in mm"},
                "layer": {
                    "type": "string",
                    "description": "New layer name (e.g., 'F.Cu', 'B.Cu')",
                },
                "net": {"type": "string", "description": "New net name to assign"},
            },
        },
    },
    {
        "name": "copy_routing_pattern",
        "title": "Copy Routing Pattern",
        "description": "Copies routing pattern from source components to target components. Enables routing replication between identical component groups by calculating and applying position offset.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sourceRefs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Source component references (e.g., ['U1', 'U2', 'U3'])",
                },
                "targetRefs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Target component references (e.g., ['U4', 'U5', 'U6'])",
                },
                "includeVias": {
                    "type": "boolean",
                    "description": "Include vias in the pattern copy",
                    "default": True,
                },
                "traceWidth": {
                    "type": "number",
                    "description": "Override trace width in mm (uses original if not specified)",
                },
            },
            "required": ["sourceRefs", "targetRefs"],
        },
    },
    {
        "name": "get_nets_list",
        "title": "List All Nets",
        "description": "Returns a list of all electrical nets defined on the board, optionally with per-net routing statistics.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "includeStats": {
                    "type": "boolean",
                    "description": "Include per-net trackCount, viaCount and totalLength",
                    "default": False,
                },
                "unit": {
                    "type": "string",
                    "enum": ["mm", "inch"],
                    "description": "Unit for length measurements (default mm)",
                    "default": "mm",
                },
            },
        },
    },
    {
        "name": "create_netclass",
        "title": "Create Net Class",
        "description": "Defines a net class with specific routing rules (trace width, clearance, etc.).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Net class name",
                    "minLength": 1,
                },
                "traceWidth": {
                    "type": "number",
                    "description": "Default trace width in millimeters",
                    "minimum": 0.1,
                },
                "clearance": {
                    "type": "number",
                    "description": "Clearance in millimeters",
                    "minimum": 0.1,
                },
                "viaDiameter": {
                    "type": "number",
                    "description": "Via diameter in millimeters",
                },
                "viaDrill": {
                    "type": "number",
                    "description": "Via drill diameter in millimeters",
                },
            },
            "required": ["name", "traceWidth", "clearance"],
        },
    },
    {
        "name": "add_copper_pour",
        "title": "Add Copper Pour",
        "description": "Creates a copper pour/zone (typically for ground or power planes).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "netName": {
                    "type": "string",
                    "description": "Net to connect this copper pour to (e.g., GND, VCC)",
                },
                "layer": {
                    "type": "string",
                    "description": "Layer for the copper pour (e.g., F.Cu, B.Cu)",
                },
                "priority": {
                    "type": "integer",
                    "description": "Pour priority (higher priorities fill first)",
                    "minimum": 0,
                    "default": 0,
                },
                "clearance": {
                    "type": "number",
                    "description": "Clearance from other objects in millimeters",
                    "minimum": 0.1,
                },
                "outline": {
                    "type": "array",
                    "description": "Array of [x, y] points defining the pour boundary",
                    "items": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 2,
                        "maxItems": 2,
                    },
                    "minItems": 3,
                },
            },
            "required": ["netName", "layer", "outline"],
        },
    },
    {
        "name": "route_differential_pair",
        "title": "Route Differential Pair",
        "description": "Routes a differential signal pair with matched lengths and spacing.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "positiveName": {
                    "type": "string",
                    "description": "Positive signal net name",
                },
                "negativeName": {
                    "type": "string",
                    "description": "Negative signal net name",
                },
                "layer": {"type": "string", "description": "Layer to route on"},
                "width": {
                    "type": "number",
                    "description": "Trace width in millimeters",
                },
                "gap": {
                    "type": "number",
                    "description": "Gap between traces in millimeters",
                },
                "points": {
                    "type": "array",
                    "description": "Waypoints for the pair routing",
                    "items": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 2,
                        "maxItems": 2,
                    },
                    "minItems": 2,
                },
            },
            "required": ["positiveName", "negativeName", "width", "gap", "points"],
        },
    },
]

# =============================================================================
# LIBRARY TOOLS
# =============================================================================

LIBRARY_TOOLS = [
    {
        "name": "list_libraries",
        "title": "List Footprint Libraries",
        "description": "Lists all available footprint libraries accessible to KiCAD.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_footprints",
        "title": "Search Footprints",
        "description": "Searches for footprints matching a query string across all libraries.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query (e.g., '0805', 'SOIC', 'QFP')",
                    "minLength": 1,
                },
                "library": {
                    "type": "string",
                    "description": "Optional library to restrict search to",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_library_footprints",
        "title": "List Footprints in Library",
        "description": "Lists all footprints available in a specific library.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "library": {
                    "type": "string",
                    "description": "Library name (e.g., Resistor_SMD, Connector_PinHeader)",
                    "minLength": 1,
                }
            },
            "required": ["library"],
        },
    },
    {
        "name": "get_footprint_info",
        "title": "Get Footprint Details",
        "description": "Retrieves detailed information about a specific footprint including pad layout, dimensions, and description.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "library": {"type": "string", "description": "Library name"},
                "footprint": {"type": "string", "description": "Footprint name"},
            },
            "required": ["library", "footprint"],
        },
    },
]

# =============================================================================
# DESIGN RULE TOOLS
# =============================================================================

DESIGN_RULE_TOOLS = [
    {
        "name": "set_design_rules",
        "title": "Set Design Rules",
        "description": "Configures board design rules including clearances, trace widths, and via sizes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "clearance": {
                    "type": "number",
                    "description": "Minimum clearance between copper in millimeters",
                    "minimum": 0.1,
                },
                "trackWidth": {
                    "type": "number",
                    "description": "Minimum track width in millimeters",
                    "minimum": 0.1,
                },
                "viaDiameter": {
                    "type": "number",
                    "description": "Minimum via diameter in millimeters",
                },
                "viaDrill": {
                    "type": "number",
                    "description": "Minimum via drill diameter in millimeters",
                },
                "microViaD iameter": {
                    "type": "number",
                    "description": "Minimum micro-via diameter in millimeters",
                },
            },
        },
    },
    {
        "name": "get_design_rules",
        "title": "Get Current Design Rules",
        "description": "Retrieves the currently configured design rules from the board.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "run_drc",
        "title": "Run Design Rule Check",
        "description": "Executes a design rule check (DRC) on the current board and reports violations.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "includeWarnings": {
                    "type": "boolean",
                    "description": "Include warnings in addition to errors",
                    "default": True,
                }
            },
        },
    },
    {
        "name": "get_drc_violations",
        "title": "Get DRC Violations",
        "description": "Return the consolidated list of DRC findings (violations + unconnected_items) with optional filtering. Each finding includes per-item details (position, uuid, parsed net/layer/length/component-ref).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "severity": {
                    "type": "string",
                    "enum": ["error", "warning", "info", "all"],
                    "description": "Filter by severity (default 'all')",
                    "default": "all",
                },
                "type": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                    "description": "Filter by violation type (e.g. 'shorting_items', 'unconnected_items')",
                },
                "net": {
                    "type": "string",
                    "description": "Filter to findings on this net (parsed from item descriptions)",
                },
                "summaryOnly": {
                    "type": "boolean",
                    "description": "Return counts only, no individual items. Default false.",
                    "default": False,
                },
                "useCachedReport": {
                    "type": "boolean",
                    "description": "Skip the kicad-cli re-run; use the previous violations file if it exists. Default false.",
                    "default": False,
                },
            },
        },
    },
]

# =============================================================================
# EXPORT TOOLS
# =============================================================================

EXPORT_TOOLS = [
    {
        "name": "export_gerber",
        "title": "Export Gerber Files",
        "description": "Generates Gerber files for PCB fabrication (industry standard format).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "outputPath": {
                    "type": "string",
                    "description": "Directory path for output files",
                },
                "layers": {
                    "type": "array",
                    "description": "List of layers to export (if not provided, exports all copper and mask layers)",
                    "items": {"type": "string"},
                },
                "includeDrillFiles": {
                    "type": "boolean",
                    "description": "Include drill files (Excellon format)",
                    "default": True,
                },
            },
            "required": ["outputPath"],
        },
    },
    {
        "name": "export_pdf",
        "title": "Export PDF",
        "description": "Exports the board layout as a PDF document for documentation or review.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "outputPath": {
                    "type": "string",
                    "description": "Path for output PDF file",
                },
                "layers": {
                    "type": "array",
                    "description": "Layers to include in PDF",
                    "items": {"type": "string"},
                },
                "colorMode": {
                    "type": "string",
                    "enum": ["color", "black_white"],
                    "description": "Color mode for output",
                    "default": "color",
                },
            },
            "required": ["outputPath"],
        },
    },
    {
        "name": "export_svg",
        "title": "Export SVG",
        "description": "Exports the board as Scalable Vector Graphics for documentation or web display.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "outputPath": {
                    "type": "string",
                    "description": "Path for output SVG file",
                },
                "layers": {
                    "type": "array",
                    "description": "Layers to include in SVG",
                    "items": {"type": "string"},
                },
            },
            "required": ["outputPath"],
        },
    },
    {
        "name": "export_3d",
        "title": "Export 3D Model",
        "description": "Exports a 3D model of the board in STEP or VRML format for mechanical CAD integration.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "outputPath": {
                    "type": "string",
                    "description": "Path for output 3D file",
                },
                "format": {
                    "type": "string",
                    "enum": ["step", "vrml"],
                    "description": "3D model format",
                    "default": "step",
                },
                "includeComponents": {
                    "type": "boolean",
                    "description": "Include 3D component models",
                    "default": True,
                },
            },
            "required": ["outputPath"],
        },
    },
    {
        "name": "export_bom",
        "title": "Export Bill of Materials",
        "description": (
            "Generates a bill of materials (BOM) listing all components with references, "
            "values, and footprints. "
            "Provide schematicPath to read component data from the .kicad_sch file — this "
            "is required to include custom properties such as LCSC part numbers, since those "
            "are stored on schematic symbols and are not automatically synced to the PCB. "
            "When groupByValue is true (default), components with the same value+footprint "
            "are merged into one row; references become a semicolon-separated list. "
            "includeAttributes lists extra property names to add as columns (e.g. ['LCSC'])."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "outputPath": {
                    "type": "string",
                    "description": "Path for output BOM file",
                },
                "schematicPath": {
                    "type": "string",
                    "description": (
                        "Path to .kicad_sch file. Required to include custom properties "
                        "(LCSC, datasheet, etc.) in the BOM."
                    ),
                },
                "format": {
                    "type": "string",
                    "enum": ["CSV", "XML", "HTML", "JSON"],
                    "description": "BOM output format",
                    "default": "CSV",
                },
                "groupByValue": {
                    "type": "boolean",
                    "description": "Group components with same value+footprint; references become semicolon-separated",
                    "default": True,
                },
                "includeAttributes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": 'Additional property names to include as columns, e.g. ["LCSC"]',
                    "default": [],
                },
            },
            "required": ["outputPath"],
        },
    },
]

# =============================================================================
# SCHEMATIC TOOLS
# =============================================================================

SCHEMATIC_TOOLS = [
    {
        "name": "create_schematic",
        "title": "Create New Schematic",
        "description": "Creates a new KiCAD schematic file for circuit design.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Path for the new schematic file (.kicad_sch)",
                },
                "title": {"type": "string", "description": "Schematic title"},
            },
            "required": ["filename"],
        },
    },
    {
        "name": "load_schematic",
        "title": "Load Existing Schematic",
        "description": "Opens an existing KiCAD schematic file for editing.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Path to schematic file (.kicad_sch)",
                }
            },
            "required": ["filename"],
        },
    },
    {
        "name": "add_schematic_component",
        "title": "Add Component to Schematic",
        "description": "Places a symbol (resistor, capacitor, IC, etc.) on the schematic.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "reference": {
                    "type": "string",
                    "description": "Reference designator (e.g., R1, C2, U3)",
                },
                "symbol": {
                    "type": "string",
                    "description": "Symbol library:name (e.g., Device:R, Device:C)",
                },
                "value": {
                    "type": "string",
                    "description": "Component value (e.g., 10k, 0.1uF)",
                },
                "x": {"type": "number", "description": "X coordinate on schematic"},
                "y": {"type": "number", "description": "Y coordinate on schematic"},
            },
            "required": ["reference", "symbol", "x", "y"],
        },
    },
    {
        "name": "add_schematic_wire",
        "title": "Draw Wire Between Pins",
        "description": "Draws a wire on the schematic between two or more coordinate points. Always call get_schematic_pin_locations first to get the approximate pin coordinates, then pass them as the first and last waypoints. snapToPins (on by default) will correct any float imprecision by snapping endpoints to the exact nearest pin coordinate. To route around components, add intermediate waypoints between the start and end: e.g. [[x1,y1], [xMid,y1], [xMid,y2], [x2,y2]] routes horizontally then vertically. Intermediate waypoints are never snapped.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Path to schematic file",
                },
                "waypoints": {
                    "type": "array",
                    "description": "Array of [x, y] coordinates defining the wire path. First and last points are the pin locations (from get_schematic_pin_locations). Add intermediate points to route around obstacles.",
                    "items": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 2,
                        "maxItems": 2,
                    },
                    "minItems": 2,
                },
                "snapToPins": {
                    "type": "boolean",
                    "description": "When true, the first and last waypoints are snapped to the nearest schematic pin within snapTolerance mm. Intermediate waypoints are left unchanged. Enabled by default to correct float coordinate imprecision.",
                    "default": True,
                },
                "snapTolerance": {
                    "type": "number",
                    "description": "Maximum distance in mm to search for a nearby pin when snapToPins is enabled.",
                    "default": 1.0,
                },
            },
            "required": ["schematicPath", "waypoints"],
        },
    },
    {
        "name": "add_schematic_net_label",
        "title": "Add Net Label",
        "description": (
            "Add a net label to a schematic. "
            "PREFERRED: supply componentRef + pinNumber to snap the label to the exact pin endpoint — "
            "this guarantees an electrical connection. "
            "Alternatively supply position [x, y], but the coordinates must match the pin endpoint exactly "
            "(even a 0.01 mm offset breaks the connection). "
            "The response includes actual_position (coordinates actually used) and snapped_to_pin "
            "(present when a pin reference was resolved)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Path to schematic file",
                },
                "netName": {
                    "type": "string",
                    "description": "Name of the net (e.g., VCC, GND, SDA)",
                },
                "position": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 2,
                    "maxItems": 2,
                    "description": "Position [x, y] for the label. Required when componentRef/pinNumber are not given.",
                },
                "componentRef": {
                    "type": "string",
                    "description": "Component reference to snap label to (e.g. U1, R1). Use with pinNumber.",
                },
                "pinNumber": {
                    "type": "string",
                    "description": "Pin number or name on componentRef (e.g. '1', 'GND'). Use with componentRef.",
                },
                "labelType": {
                    "type": "string",
                    "enum": ["label", "global_label", "hierarchical_label"],
                    "description": "Label type (default: label)",
                    "default": "label",
                },
                "orientation": {
                    "type": "number",
                    "description": "Rotation angle in degrees (0, 90, 180, 270)",
                    "default": 0,
                },
            },
            "required": ["schematicPath", "netName"],
        },
    },
    {
        "name": "connect_to_net",
        "title": "Connect Pin to Net",
        "description": (
            "Connect a component pin to a named net by adding a wire stub and net label at the exact "
            "pin endpoint. The response includes pin_location (exact pin coords), label_location "
            "(where the label was placed), and wire_stub (the wire segment added) so you can confirm "
            "the placement without a separate verification call."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Path to schematic file",
                },
                "componentRef": {
                    "type": "string",
                    "description": "Component reference designator (e.g., R1, U3)",
                },
                "pinName": {
                    "type": "string",
                    "description": "Pin number or name on the component",
                },
                "netName": {
                    "type": "string",
                    "description": "Name of the net to connect to",
                },
            },
            "required": ["schematicPath", "componentRef", "pinName", "netName"],
        },
    },
    {
        "name": "connect_pins",
        "title": "Connect Pins to Net",
        "description": (
            "Connect two or more component pins to the same named net. "
            "If netName is omitted, existing net labels on any of the listed pins are "
            "detected automatically — a human-readable name takes priority over an "
            "auto-generated 'Net-(...)' name. "
            "Handles chained connections safely: connect_pins([B, C]) detects B's "
            "existing label and reuses it for C, so A (already on B's net) is not orphaned. "
            "Conflict detection: if two pins carry *different* human-readable nets the call "
            "fails and lists conflicting_nets. Pins already on the target net are skipped "
            "(idempotent). "
            "Style: 'label' (default) gives every fresh pin a stub + label (legacy "
            "behaviour). 'wire' tries to draw a real wire between each consecutive "
            "unconnected pair — Phase 1 only handles trivial collinear-and-facing cases — "
            "and hard-fails any pair that cannot be routed. 'auto' tries wire first and "
            "falls back to label per-pair on failure. Power/ground nets (VBUS, GND, +3V3 "
            "etc.) default to label even in 'auto'. "
            "Returns: net_used, connected, already_connected, failed lists; plus "
            "wired_pairs and routing_failures when style != 'label'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Path to schematic file",
                },
                "pins": {
                    "type": "array",
                    "description": "List of pins to connect, each as {ref, pin}",
                    "items": {
                        "type": "object",
                        "properties": {
                            "ref": {
                                "type": "string",
                                "description": "Component reference designator (e.g. R1, U3)",
                            },
                            "pin": {
                                "type": "string",
                                "description": "Pin number or name",
                            },
                        },
                        "required": ["ref", "pin"],
                    },
                    "minItems": 2,
                },
                "netName": {
                    "type": "string",
                    "description": (
                        "Net name to use. Optional: if omitted, auto-detected from "
                        "existing labels on the listed pins."
                    ),
                },
                "style": {
                    "type": "string",
                    "enum": ["label", "wire", "auto"],
                    "description": (
                        "How to connect each pair: 'label' (default, stub+label per pin), "
                        "'wire' (real wires only, hard-fail otherwise), 'auto' (wire when "
                        "possible, label fallback per pair)."
                    ),
                },
                "maxLen": {
                    "type": "number",
                    "description": (
                        "Max wire length in mm for a single segment "
                        "(default 80.0). Only consulted when style != 'label'."
                    ),
                },
                "maxBends": {
                    "type": "integer",
                    "description": (
                        "Max corners allowed in a wire path (default 4). Phase 1 only "
                        "emits zero-bend straight segments."
                    ),
                },
                "powerNets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Extra net names to treat as power/ground (default-to-label even "
                        "in 'auto' mode). Built-in defaults already cover VBUS, GND, "
                        "+3V3, +5V, etc."
                    ),
                },
            },
            "required": ["schematicPath", "pins"],
        },
    },
    {
        "name": "connect_component_to_nets",
        "title": "Connect Component Pins to Nets",
        "description": (
            "Connect all pins of one component to their respective nets in a single call, "
            "instead of one connect_to_net call per pin. "
            'Pass connections as a pin→net map, e.g. {"1": "GND", "8": "VCC", "3": "OUTPUT"}. '
            "Pins already on the correct net are skipped (idempotent). "
            "Pins already on a *different* net are reported in failed. "
            "Returns connected, already_connected, and failed lists."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Path to schematic file",
                },
                "componentRef": {
                    "type": "string",
                    "description": "Component reference designator (e.g. R1, U1)",
                },
                "connections": {
                    "type": "object",
                    "description": (
                        "Map of pin name/number to net name, "
                        'e.g. {"1": "GND", "8": "VCC", "3": "OUTPUT"}'
                    ),
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": ["schematicPath", "componentRef", "connections"],
        },
    },
    {
        "name": "get_net_connections",
        "title": "Get Net Connections",
        "description": "Returns all components and pins connected to a specified net.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Path to schematic file",
                },
                "netName": {
                    "type": "string",
                    "description": "Name of the net to query",
                },
            },
            "required": ["schematicPath", "netName"],
        },
    },
    {
        "name": "get_wire_connections",
        "title": "Get Wire Connections",
        "description": (
            "Returns the net name and all wires and component pins connected at a given point. "
            "Accepts either a component reference + pin number (e.g. reference='U1', pin='3') "
            "or a schematic coordinate (x, y in mm). "
            "The response includes: 'net' (label name or null for unnamed nets), "
            "'pins' (all component pins on the net), 'wires' (all wire segments on the net), "
            "and 'query_point' (the resolved coordinate used). "
            "The query point must be at a wire endpoint or junction — wire midpoints are not matched. "
            "Use get_schematic_pin_locations or list_schematic_wires to obtain exact endpoint coordinates."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Path to the schematic file (.kicad_sch)",
                },
                "reference": {
                    "type": "string",
                    "description": "Component reference (e.g. U1, R1). Pair with pin.",
                },
                "pin": {
                    "type": "string",
                    "description": "Pin number or name (e.g. '3', 'SDA'). Pair with reference.",
                },
                "x": {
                    "type": "number",
                    "description": "X coordinate of a wire endpoint in mm. Pair with y.",
                },
                "y": {
                    "type": "number",
                    "description": "Y coordinate of a wire endpoint in mm. Pair with x.",
                },
            },
            "required": ["schematicPath"],
        },
    },
    {
        "name": "get_net_at_point",
        "title": "Get Net At Point",
        "description": (
            "Returns the net name at a given (x, y) coordinate in a schematic, "
            "or null if no net label or wire endpoint is present at that position. "
            "Checks net label positions first, then wire endpoints. "
            "Useful for quickly identifying what net occupies a specific coordinate "
            "without traversing the full wire graph."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Path to the schematic file (.kicad_sch)",
                },
                "x": {
                    "type": "number",
                    "description": "X coordinate in mm",
                },
                "y": {
                    "type": "number",
                    "description": "Y coordinate in mm",
                },
            },
            "required": ["schematicPath", "x", "y"],
        },
    },
    {
        "name": "get_schematic_pin_locations",
        "title": "Get Schematic Pin Locations",
        "description": "Returns the exact absolute coordinates of all pins on a schematic component. Use this BEFORE placing net labels with add_schematic_net_label to get the correct x/y position for each pin endpoint.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Path to the schematic file",
                },
                "reference": {
                    "type": "string",
                    "description": "Component reference designator (e.g., U1, R1, J2)",
                },
            },
            "required": ["schematicPath", "reference"],
        },
    },
    {
        "name": "connect_passthrough",
        "title": "Connect Passthrough (Pin-to-Pin)",
        "description": "Connects all pins of a source connector to the matching pins of a target connector using shared net labels. Ideal for passthrough adapters where J1 pin N connects directly to J2 pin N. Each pair gets a net label '{netPrefix}_{pinNumber}'. Use this instead of calling connect_to_net 15 times for FFC/ribbon cable passthroughs. NOTE: KiCAD Connector_Generic symbols always have pin 1 at the TOP of the symbol and pin N at the BOTTOM. When assigning named nets (e.g. GND, CAM_SCL) to specific pin numbers, always use the physical pin number as shown in the connector datasheet — pin 1 = top of symbol.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Path to the schematic file",
                },
                "sourceRef": {
                    "type": "string",
                    "description": "Reference of the source connector (e.g., J1)",
                },
                "targetRef": {
                    "type": "string",
                    "description": "Reference of the target connector (e.g., J2)",
                },
                "netPrefix": {
                    "type": "string",
                    "description": "Prefix for generated net names, e.g. 'CSI' produces CSI_1, CSI_2, ... (default: PIN)",
                },
                "pinOffset": {
                    "type": "integer",
                    "description": "Add this value to the pin number when building net names (default: 0)",
                },
            },
            "required": ["schematicPath", "sourceRef", "targetRef"],
        },
    },
    {
        "name": "run_erc",
        "title": "Run Electrical Rules Check (ERC)",
        "description": "Runs the KiCAD Electrical Rules Check (ERC) on a schematic via kicad-cli and returns all violations with type, severity, and location. Use this to verify the schematic is electrically correct before generating a netlist or exporting.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Path to the .kicad_sch schematic file",
                },
                "includeNoise": {
                    "type": "boolean",
                    "description": "When true, includes suppressed cosmetic/noise violation types (endpoint_off_grid, lib_symbol_issues, lib_symbol_mismatch) in the main violations list instead of noise_violations. Default false.",
                },
            },
            "required": ["schematicPath"],
        },
    },
    {
        "name": "sync_schematic_to_board",
        "title": "Sync Schematic to PCB (F8)",
        "description": (
            "Reads net connections from the schematic and assigns them to matching component "
            "pads in the PCB board file. Equivalent to KiCAD Pcbnew F8 'Update PCB from "
            "Schematic'. "
            "When the board has no footprints yet (first-time sync), missing footprints are "
            "automatically imported from the schematic and arranged in a grid before net "
            "assignment — eliminating the need for individual place_component calls. "
            "Pass autoImport=false to suppress auto-import, or autoImport=true to always "
            "import any missing components even on subsequent syncs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Path to .kicad_sch file. If omitted, auto-detected from current board path.",
                },
                "boardPath": {
                    "type": "string",
                    "description": "Path to .kicad_pcb file. If omitted, uses currently loaded board.",
                },
                "autoImport": {
                    "type": "boolean",
                    "description": (
                        "Import footprints missing from the board before syncing nets. "
                        "Default: true when board has no footprints, false otherwise."
                    ),
                },
            },
        },
    },
    {
        "name": "generate_netlist",
        "title": "Generate Netlist (JSON)",
        "description": (
            "Returns a structured JSON netlist from the schematic: component list "
            "(reference, value, footprint) and net list (net name + all connected "
            "component/pin pairs). Uses kicad-cli internally — requires a saved "
            ".kicad_sch file. For writing to a file or exporting SPICE/Cadstar/OrcadPCB2 "
            "format, use export_netlist instead."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Absolute path to the .kicad_sch schematic file",
                },
            },
            "required": ["schematicPath"],
        },
    },
    {
        "name": "list_schematic_libraries",
        "title": "List Symbol Libraries",
        "description": "Lists all available symbol libraries for schematic design.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "searchPaths": {
                    "type": "array",
                    "description": "Optional additional paths to search for libraries",
                    "items": {"type": "string"},
                }
            },
        },
    },
    {
        "name": "export_schematic_pdf",
        "title": "Export Schematic to PDF",
        "description": "Exports the schematic as a PDF document for printing or documentation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Path to schematic file",
                },
                "outputPath": {"type": "string", "description": "Path for output PDF"},
            },
            "required": ["schematicPath", "outputPath"],
        },
    },
    # --- Schematic Analysis Tools (read-only) ---
    {
        "name": "get_schematic_view_region",
        "title": "Get Schematic View Region",
        "description": "Exports a cropped region of the schematic as an image (PNG or SVG). Specify a bounding box in schematic mm coordinates to zoom into a specific area.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Path to the .kicad_sch schematic file",
                },
                "x1": {
                    "type": "number",
                    "description": "Left X coordinate of the region in mm",
                },
                "y1": {
                    "type": "number",
                    "description": "Top Y coordinate of the region in mm",
                },
                "x2": {
                    "type": "number",
                    "description": "Right X coordinate of the region in mm",
                },
                "y2": {
                    "type": "number",
                    "description": "Bottom Y coordinate of the region in mm",
                },
                "format": {
                    "type": "string",
                    "enum": ["png", "svg"],
                    "description": "Output image format (default: png)",
                },
                "width": {
                    "type": "integer",
                    "description": "Output image width in pixels (default: 800)",
                },
                "height": {
                    "type": "integer",
                    "description": "Output image height in pixels (default: 600)",
                },
            },
            "required": ["schematicPath", "x1", "y1", "x2", "y2"],
        },
    },
    {
        "name": "find_overlapping_elements",
        "title": "Find Overlapping Elements",
        "description": "Detects spatially overlapping symbols, wires, and labels in the schematic. Finds: duplicate power symbols at the same position, collinear overlapping wire segments, and labels stacked on top of each other.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Path to the .kicad_sch schematic file",
                },
                "tolerance": {
                    "type": "number",
                    "description": "Distance threshold in mm for label proximity and wire collinearity checks. Symbol overlap uses bounding-box intersection. (default: 0.5)",
                },
            },
            "required": ["schematicPath"],
        },
    },
    {
        "name": "get_elements_in_region",
        "title": "Get Elements in Region",
        "description": "Lists all symbols, wires, and labels within a rectangular region of the schematic. Useful for understanding what is in a specific area before modifying it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Path to the .kicad_sch schematic file",
                },
                "x1": {
                    "type": "number",
                    "description": "Left X coordinate of the region in mm",
                },
                "y1": {
                    "type": "number",
                    "description": "Top Y coordinate of the region in mm",
                },
                "x2": {
                    "type": "number",
                    "description": "Right X coordinate of the region in mm",
                },
                "y2": {
                    "type": "number",
                    "description": "Bottom Y coordinate of the region in mm",
                },
            },
            "required": ["schematicPath", "x1", "y1", "x2", "y2"],
        },
    },
    {
        "name": "find_wires_crossing_symbols",
        "title": "Find Wires Crossing Symbols",
        "description": "Find all wires that cross over component symbol bodies. Wires passing over symbols are unacceptable in schematics — they indicate routing mistakes where a wire was drawn across a component instead of around it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Path to the .kicad_sch schematic file",
                }
            },
            "required": ["schematicPath"],
        },
    },
    {
        "name": "find_orphaned_wires",
        "title": "Find Orphaned Wires",
        "description": (
            "Find wire segments with at least one dangling endpoint — an endpoint not connected "
            "to a component pin, net label, or another wire. "
            "Orphaned wires cause ERC 'wire end unconnected' errors and indicate routing mistakes. "
            "Does not require the KiCad UI to be running."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Path to the .kicad_sch schematic file",
                }
            },
            "required": ["schematicPath"],
        },
    },
    {
        "name": "list_floating_labels",
        "title": "List Floating Net Labels",
        "description": (
            "Returns all net labels in the schematic that are not connected to any component pin. "
            "A label is 'floating' when no component pin's coordinate falls on the wire-network "
            "reachable from the label's anchor position. "
            "Floating labels indicate misplaced or off-grid labels that will cause ERC errors. "
            "Does not require the KiCad UI to be running."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Path to the .kicad_sch schematic file",
                }
            },
            "required": ["schematicPath"],
        },
    },
    {
        "name": "snap_to_grid",
        "title": "Snap Schematic Elements to Grid",
        "description": (
            "Snap schematic element coordinates to the nearest grid point. "
            "KiCAD eeschema uses exact integer matching (10 000 IU/mm) for connectivity, "
            "so even a sub-pixel coordinate offset will make wires appear connected visually "
            "but fail ERC checks. Running this tool before ERC eliminates that class of error. "
            "Modifies the .kicad_sch file in place. "
            "Does not require the KiCAD UI to be running."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {
                    "type": "string",
                    "description": "Path to the .kicad_sch schematic file",
                },
                "gridSize": {
                    "type": "number",
                    "description": (
                        "Grid spacing in mm (default: 1.27 — standard KiCAD schematic grid). "
                        "Do NOT use 2.54: half of all valid KiCAD pin positions are at odd "
                        "multiples of 1.27 mm and would be displaced 1.27 mm, breaking "
                        "connectivity."
                    ),
                    "default": 1.27,
                },
                "elements": {
                    "type": "array",
                    "description": (
                        "Element types to snap. "
                        'Valid values: "wires", "junctions", "labels", "components". '
                        'Defaults to ["wires", "junctions", "labels"] when omitted. '
                        '"components" is opt-in because moving a component without re-routing '
                        "its wires will create new mismatches."
                    ),
                    "items": {
                        "type": "string",
                        "enum": ["wires", "junctions", "labels", "components"],
                    },
                },
            },
            "required": ["schematicPath"],
        },
    },
    {
        "name": "set_schematic_component_property",
        "title": "Set Schematic Component Property",
        "description": (
            "Add or update a single property on a placed schematic symbol "
            "(e.g. Value, Footprint, LCSC, Datasheet). "
            "The property is created if it does not exist. "
            "To set multiple properties on one component in one call, "
            "use set_schematic_component_properties (plural) instead."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {"type": "string", "description": "Path to .kicad_sch file"},
                "reference": {"type": "string", "description": "Component reference (e.g. R1, U1)"},
                "name": {"type": "string", "description": "Property name (e.g. LCSC, Footprint)"},
                "value": {"type": "string", "description": "Property value to set"},
                "hide": {
                    "type": "boolean",
                    "description": "Hide the property label on schematic",
                    "default": True,
                },
            },
            "required": ["schematicPath", "reference", "name", "value"],
        },
    },
    {
        "name": "set_schematic_component_properties",
        "title": "Set Multiple Schematic Component Properties",
        "description": (
            "Set multiple properties on one or more schematic components in a single call. "
            "Pass a components dict mapping each reference to a {property: value} dict. "
            "Eliminates the need for one set_schematic_component_property call per field. "
            'Example: {"R1": {"Footprint": "...", "LCSC": "C21190"}, '
            '"U1": {"LCSC": "C19708138"}}. '
            "Returns per-component success/failure breakdown."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "schematicPath": {"type": "string", "description": "Path to .kicad_sch file"},
                "components": {
                    "type": "object",
                    "description": (
                        "Map of reference → {property: value} pairs. "
                        'Example: {"R1": {"Footprint": "Resistor_SMD:R_0603_1608Metric", '
                        '"LCSC": "C21190"}}'
                    ),
                    "additionalProperties": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    },
                },
                "hideNewProperties": {
                    "type": "boolean",
                    "description": "Hide newly-added property labels on the schematic (default true)",
                    "default": True,
                },
            },
            "required": ["schematicPath", "components"],
        },
    },
]

# =============================================================================
# UI/PROCESS TOOLS
# =============================================================================

UI_TOOLS = [
    {
        "name": "check_kicad_ui",
        "title": "Check KiCAD UI Status",
        "description": "Checks if KiCAD user interface is currently running and returns process information.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "launch_kicad_ui",
        "title": "Launch KiCAD Application",
        "description": "Opens the KiCAD graphical user interface, optionally with a specific project loaded.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "projectPath": {
                    "type": "string",
                    "description": "Optional path to project file to open in UI",
                },
                "autoLaunch": {
                    "type": "boolean",
                    "description": "Whether to automatically launch if not running",
                    "default": True,
                },
            },
        },
    },
]

# =============================================================================
# COMBINED TOOL SCHEMAS
# =============================================================================

TOOL_SCHEMAS: Dict[str, Any] = {}

# Combine all tool categories
for tool in (
    PROJECT_TOOLS
    + BOARD_TOOLS
    + COMPONENT_TOOLS
    + ROUTING_TOOLS
    + LIBRARY_TOOLS
    + DESIGN_RULE_TOOLS
    + EXPORT_TOOLS
    + SCHEMATIC_TOOLS
    + UI_TOOLS
):
    TOOL_SCHEMAS[tool["name"]] = tool

# Total: 46 tools with comprehensive schemas
