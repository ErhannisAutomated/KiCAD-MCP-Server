# Routing Tools Reference

Added in: v1.0.0, major expansion in v2.2.0-v2.2.3 (PR #44, @Kletternaut)

This document provides comprehensive documentation for the 13 routing tools available in the KiCAD MCP Server. These tools cover basic trace routing, advanced operations like differential pairs, net management, trace operations, and copper zone management.

## Basic Routing (3 tools)

### add_net

Create a new net on the PCB.

**Parameters:**

| Parameter | Type   | Required | Description    |
| --------- | ------ | -------- | -------------- |
| name      | string | Yes      | Net name       |
| netClass  | string | No       | Net class name |

**Usage Notes:**

- Creates a new net that can be assigned to traces and pads
- If the net already exists, it will be reused
- Net class assignment is optional; defaults to "Default" if not specified

**Example:**

```json
{
  "name": "VCC_3V3",
  "netClass": "Power"
}
```

---

### route_trace

Route a trace segment between two XY points on a fixed layer.

**Parameters:**

| Parameter      | Type    | Required | Description                                 |
| -------------- | ------- | -------- | ------------------------------------------- |
| start          | object  | Yes      | Start position with x, y, and optional unit |
| end            | object  | Yes      | End position with x, y, and optional unit   |
| layer          | string  | Yes      | PCB layer                                   |
| width          | number  | Yes      | Trace width in mm                           |
| net            | string  | Yes      | Net name                                    |
| checkObstacles | boolean | No       | Refuse the route when the swept trace (width + clearance) would touch foreign-net copper. Default `true`. |
| clearance      | number  | No       | Minimum gap (mm) between trace edge and foreign-net copper. Defaults to the net's netclass clearance, falling back to the board default. |

**Usage Notes:**

- The obstacle check is **width- and clearance-aware** (#177): the trace is treated as a stadium of half-width `width/2 + clearance`, so an edge-clipping case where a fat trace exits an IC pin and grazes the neighbouring pad is caught even though the centerline misses it. Pre-#177 the check only looked at the centerline.
- WARNING: Does NOT handle layer changes
- If start and end are on different copper layers, use `route_pad_to_pad` instead, which automatically inserts a via
- Coordinates use mm by default unless unit is specified
- This is a low-level tool; prefer `route_pad_to_pad` for component-to-component routing

**Example:**

```json
{
  "start": { "x": 100.0, "y": 50.0, "unit": "mm" },
  "end": { "x": 120.0, "y": 50.0, "unit": "mm" },
  "layer": "F.Cu",
  "width": 0.25,
  "net": "GND"
}
```

---

### route_pad_to_pad

PREFERRED tool for pad-to-pad routing. Looks up pad positions automatically, detects the net from the pad, and automatically inserts a via if the two pads are on different copper layers.

**Parameters:**

| Parameter         | Type          | Required | Description                                          |
| ----------------- | ------------- | -------- | ---------------------------------------------------- |
| fromRef           | string        | Yes      | Reference of the source component (e.g. 'U2')        |
| fromPad           | string/number | Yes      | Pad number on the source component (e.g. '6' or 6)   |
| toRef             | string        | Yes      | Reference of the target component (e.g. 'U1')        |
| toPad             | string/number | Yes      | Pad number on the target component (e.g. '15' or 15) |
| layer             | string        | No       | PCB layer (default: F.Cu)                            |
| width             | number        | No       | Trunk trace width in mm (default: board default)     |
| net               | string        | No       | Net name override (default: auto-detected from pad)  |
| checkObstacles    | boolean       | No       | Refuse the route when the swept trace would touch foreign-net copper. Default `true`. |
| clearance         | number        | No       | Minimum gap (mm) between trace edge and foreign-net copper. Defaults to netclass. |
| escapeFromWidth   | number        | No       | Width (mm) of a narrow pin-escape stub at the source pad. Pair with escapeFromLength. |
| escapeFromLength  | number        | No       | Length (mm) of the source-side pin-escape stub. Direction is perpendicular to the pin row. |
| escapeToWidth     | number        | No       | Symmetric width (mm) for a pin-escape stub at the destination pad. |
| escapeToLength    | number        | No       | Symmetric length (mm) for the destination-side stub. |

**Usage Notes:**

- This is the PREFERRED tool for routing between component pads
- Automatically looks up pad positions - no need to query them separately
- Auto-detects the net from the source pad
- Critically: if pads are on different copper layers (e.g., one on F.Cu and one on B.Cu), automatically inserts a via at an appropriate position to complete the connection
- Always use this instead of `route_trace` when routing between named component pads
- Via is placed at the start pad's X coordinate to avoid stacking issues with back-to-back mirrored connectors
- **Pin-escape (#178)**: when a fat trunk trace can't physically fit out of a tight IC pin pitch (e.g. a 1.5 mm POWER_4A trunk exiting a 0.65 mm-pitch HTSSOP-28), pass `escapeFromWidth` + `escapeFromLength` to emit a narrow stub from the pad before widening into the trunk. Stub direction is computed perpendicular to the pin row (vector from footprint center to pad center). Same-layer routes only — cross-layer pin-escape rejects cleanly and points the caller at `route_trace` + `find_via_lane`.

**Example:**

```json
{
  "fromRef": "U2",
  "fromPad": "6",
  "toRef": "U1",
  "toPad": "15",
  "width": 0.25
}
```

---

## Vias (1 tool)

### pair_via

Propose (and optionally apply) a parallel partner via next to every existing via on the given net(s). Doubles current-carrying capacity and ~halves inductance for high-current power vias — needed because freerouting's DSN class-rule via spec can only pick a single via diameter per class, so high-current vias have always been hand-paired in past sessions.

**Parameters:**

| Parameter     | Type    | Required | Description                                          |
| ------------- | ------- | -------- | ---------------------------------------------------- |
| nets          | array   | No       | Explicit net list (e.g. `["BAT+","BAT-"]`). Overrides `netClass` when set. |
| netClass      | string  | No       | Netclass name to filter by (default `"POWER_4A"`). Ignored if `nets` is set. |
| offset        | number  | No       | Distance (mm) from the parent via center to the partner via (default 1.0). |
| minClearance  | number  | No       | Minimum gap (mm) between the partner via edge and foreign-net copper (default 0.2). |
| viaDiameter   | number  | No       | Partner via diameter (mm). Defaults to the parent via's. |
| viaDrill      | number  | No       | Partner via drill (mm). Defaults to the parent via's. |
| apply         | boolean | No       | Commit the proposed partner vias (default `false` = preview). |
| maxPairs      | number  | No       | Safety cap on partner-via count (default 200).       |

**Usage Notes:**

- For each parent via, tries the 4 ±x/±y offset positions; the first that clears `minClearance` from foreign-net copper AND isn't within `offset × 0.5` of an existing same-net via wins.
- Vias with no clear position are reported as `skippedNoClearance`.
- Re-running the tool is safe — dedup logic prevents stacking the partner of one via on top of another.

**Example:**

```json
{ "netClass": "POWER_4A", "offset": 1.0, "apply": true }
```

---

### bridge_same_net_pins

Create a small filled zone covering two same-net pads, replacing a thin sub-min-width trace that would violate the POWER netclass track-width rule. Standard practice for parallel power pins on IC datasheets (BAT+ pad doublings on TSSOP / QFN devices) — the zone bonds the pins with solid copper that isn't subject to `track_width` DRC.

**Parameters:**

| Parameter   | Type    | Required | Description                                          |
| ----------- | ------- | -------- | ---------------------------------------------------- |
| padA        | object  | Yes      | First pad: `{ref:"U4", pad:"2"}`.                    |
| padB        | object  | Yes      | Second pad: `{ref:"U4", pad:"3"}`.                   |
| layer       | string  | No       | Copper layer (default `"F.Cu"`).                     |
| marginMm    | number  | No       | Margin around the pad-union bbox (default 0.1 mm).   |
| connection  | string  | No       | `"solid"` (default; full bond, no relief spokes) or `"thermal"`. |
| apply       | boolean | No       | Commit the zone (default `false` = preview outline). |

**Usage Notes:**

- Both pads must already be on the same net — refuses otherwise with a clear error pointing at the mismatched assignment.
- Default `solid` connection is appropriate for current-carrying bridges where thermal-relief spokes would bottleneck. `thermal` for low-current cases where relief is desired.
- Zone priority is 100 (above the board's main pour) so it takes precedence in any overlap area.

**Example:**

```json
{
  "padA": {"ref": "U4", "pad": "2"},
  "padB": {"ref": "U4", "pad": "3"},
  "apply": true
}
```

---

### stitch_pour_vias

Propose (and optionally apply) a grid of stitching vias on a copper pour net. Each candidate must sit inside a zone outline on the net, clear `minClearance` from any foreign-net copper on any layer, and not duplicate an existing same-net via. Through-via, F.Cu ↔ B.Cu.

**Parameters:**

| Parameter     | Type    | Required | Description                                          |
| ------------- | ------- | -------- | ---------------------------------------------------- |
| net           | string  | Yes      | Net to stitch (must have at least one zone)          |
| gridPitch     | number  | Yes      | Spacing between candidate vias, mm                   |
| viaDiameter   | number  | No       | Via outer diameter, mm (default 0.6)                 |
| viaDrill      | number  | No       | Via drill diameter, mm (default 0.3)                 |
| minClearance  | number  | No       | Minimum gap (mm) between via edge and foreign-net copper (default 0.2) |
| apply         | boolean | No       | Commit the proposed vias (default `false` = preview) |
| maxVias       | number  | No       | Safety cap on the number of vias proposed (default 200) |

**Usage Notes:**

- Default is preview — review the proposed positions before committing with `apply=true`.
- Implementation uses `Zone.HitTest` (outline) rather than `HitTestFilledArea`; the latter requires `ZONE_FILLER.Fill()` which has a known SWIG segfault risk. The explicit `minClearance` check catches the foreign-copper exclusions that the filled polygon would.
- Dedup distance is `gridPitch × 0.7`; re-running on the same net at the same pitch proposes 0 new vias.
- Returns `skippedOutside`, `skippedClearance`, `skippedDedup` so you can tune `gridPitch` / `minClearance` empirically.

**Example:**

```json
{
  "net": "GND",
  "gridPitch": 3.0,
  "minClearance": 0.2,
  "apply": false
}
```

---

### add_via

Add a via to the PCB.

**Parameters:**

| Parameter | Type   | Required | Description                               |
| --------- | ------ | -------- | ----------------------------------------- |
| position  | object | Yes      | Via position with x, y, and optional unit |
| net       | string | Yes      | Net name                                  |
| viaType   | string | No       | Via type: "through", "blind", or "buried" |

**Usage Notes:**

- Through vias connect all layers (default)
- Blind vias connect an outer layer to one or more inner layers
- Buried vias connect two or more inner layers without reaching outer layers
- Position coordinates use mm by default

**Example:**

```json
{
  "position": { "x": 110.0, "y": 50.0, "unit": "mm" },
  "net": "GND",
  "viaType": "through"
}
```

---

## Advanced Routing (2 tools)

### route_differential_pair

Route a differential pair between two sets of points.

**Parameters:**

| Parameter   | Type   | Required | Description                                |
| ----------- | ------ | -------- | ------------------------------------------ |
| positivePad | object | Yes      | Positive pad with reference and pad number |
| negativePad | object | Yes      | Negative pad with reference and pad number |
| layer       | string | Yes      | PCB layer                                  |
| width       | number | Yes      | Trace width in mm                          |
| gap         | number | Yes      | Gap between traces in mm                   |
| positiveNet | string | Yes      | Positive net name                          |
| negativeNet | string | Yes      | Negative net name                          |

**Usage Notes:**

- Used for high-speed signals like USB, Ethernet, HDMI, etc.
- Maintains controlled impedance through consistent trace width and gap
- Both traces are routed in parallel with specified separation
- Pad object format: `{"reference": "U1", "pad": "1"}`

**Example:**

```json
{
  "positivePad": { "reference": "J1", "pad": "2" },
  "negativePad": { "reference": "J1", "pad": "3" },
  "layer": "F.Cu",
  "width": 0.2,
  "gap": 0.2,
  "positiveNet": "USB_DP",
  "negativeNet": "USB_DN"
}
```

---

### copy_routing_pattern

Copy routing pattern (traces and vias) from a group of source components to a matching group of target components.

**Parameters:**

| Parameter   | Type          | Required | Description                                                                               |
| ----------- | ------------- | -------- | ----------------------------------------------------------------------------------------- |
| sourceRefs  | array[string] | Yes      | References of the source components (e.g. ['U1', 'R1', 'C1'])                             |
| targetRefs  | array[string] | Yes      | References of the target components in same order as sourceRefs (e.g. ['U2', 'R2', 'C2']) |
| includeVias | boolean       | No       | Also copy vias (default: true)                                                            |
| traceWidth  | number        | No       | Override trace width in mm (default: keep original width)                                 |

**Usage Notes:**

- The offset is calculated automatically from the position difference between the first source and first target component
- Useful for replicating routing between identical circuit blocks
- Component arrays must be in matching order (sourceRefs[0] maps to targetRefs[0], etc.)
- Preserves relative routing topology from source to target
- Vias are copied by default unless includeVias is set to false
- Original trace widths are preserved unless traceWidth override is specified

**Example:**

```json
{
  "sourceRefs": ["U1", "R1", "C1"],
  "targetRefs": ["U2", "R2", "C2"],
  "includeVias": true
}
```

---

## Net Management (2 tools)

### get_nets_list

Get a list of all nets in the PCB with optional statistics.

**Parameters:**

| Parameter    | Type    | Required | Description                                          |
| ------------ | ------- | -------- | ---------------------------------------------------- |
| includeStats | boolean | No       | Include statistics (track count, total length, etc.) |
| unit         | string  | No       | Unit for length measurements: "mm" or "inch"         |

**Usage Notes:**

- Returns all nets present in the board
- Statistics include track count, via count, and total trace length
- Useful for verifying net connectivity and routing completeness
- Length measurements default to mm

**Example:**

```json
{
  "includeStats": true,
  "unit": "mm"
}
```

---

### create_netclass

Create a new net class with custom design rules.

**Parameters:**

| Parameter   | Type   | Required | Description               |
| ----------- | ------ | -------- | ------------------------- |
| name        | string | Yes      | Net class name            |
| traceWidth  | number | No       | Default trace width in mm |
| clearance   | number | No       | Clearance in mm           |
| viaDiameter | number | No       | Via diameter in mm        |
| viaDrill    | number | No       | Via drill size in mm      |

**Usage Notes:**

- Net classes define design rules for groups of nets
- Common use cases: power nets (wider traces), high-speed signals (controlled impedance)
- Once created, assign nets to the class using the netClass parameter in `add_net`
- All measurements in mm

**Example:**

```json
{
  "name": "Power",
  "traceWidth": 0.5,
  "clearance": 0.3,
  "viaDiameter": 0.8,
  "viaDrill": 0.4
}
```

---

## Trace Operations (3 tools)

### delete_trace

Delete traces from the PCB. Can delete by UUID, position, or bulk-delete all traces on a net.

**Parameters:**

| Parameter   | Type    | Required | Description                                                 |
| ----------- | ------- | -------- | ----------------------------------------------------------- |
| traceUuid   | string  | No       | UUID of a specific trace to delete                          |
| position    | object  | No       | Delete trace nearest to this position (x, y, optional unit) |
| net         | string  | No       | Delete all traces on this net (bulk delete)                 |
| layer       | string  | No       | Filter by layer when using net-based deletion               |
| includeVias | boolean | No       | Include vias in net-based deletion                          |

**Usage Notes:**

- Three deletion modes: by UUID (specific), by position (nearest), or by net (bulk)
- Position-based deletion finds the closest trace to the specified coordinates
- Net-based deletion can be filtered by layer
- Vias are excluded from net-based deletion by default unless includeVias is true

**Example (bulk delete):**

```json
{
  "net": "GND",
  "layer": "F.Cu",
  "includeVias": false
}
```

---

### query_traces

Query traces on the board with optional filters by net, layer, or bounding box.

**Parameters:**

| Parameter   | Type   | Required | Description                                                   |
| ----------- | ------ | -------- | ------------------------------------------------------------- |
| net         | string | No       | Filter by net name                                            |
| layer       | string | No       | Filter by layer name                                          |
| boundingBox | object | No       | Filter by bounding box region (x1, y1, x2, y2, optional unit) |
| unit        | string | No       | Unit for coordinates: "mm" or "inch"                          |

**Usage Notes:**

- Returns trace information including UUID, position, width, layer, and net
- Filters can be combined (e.g., specific net on specific layer)
- Bounding box uses rectangular region defined by opposite corners
- Useful for analyzing routing in specific board regions or on specific nets

**Example:**

```json
{
  "net": "VCC_3V3",
  "layer": "F.Cu"
}
```

---

### modify_trace

Modify an existing trace (change width, layer, or net).

**Parameters:**

| Parameter | Type   | Required | Description                 |
| --------- | ------ | -------- | --------------------------- |
| traceUuid | string | Yes      | UUID of the trace to modify |
| width     | number | No       | New trace width in mm       |
| layer     | string | No       | New layer name              |
| net       | string | No       | New net name                |

**Usage Notes:**

- Requires the trace UUID, which can be obtained from `query_traces`
- At least one modification parameter (width, layer, or net) must be provided
- Use with caution when changing nets - ensure electrical correctness
- Width changes are useful for adjusting impedance or current capacity

**Example:**

```json
{
  "traceUuid": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "width": 0.5
}
```

---

## Copper Zones (2 tools)

### add_copper_pour

Add a copper pour (ground/power plane) to the PCB.

**Parameters:**

| Parameter | Type          | Required | Description                                                                               |
| --------- | ------------- | -------- | ----------------------------------------------------------------------------------------- |
| layer     | string        | Yes      | PCB layer                                                                                 |
| net       | string        | Yes      | Net name                                                                                  |
| clearance | number        | No       | Clearance in mm                                                                           |
| outline   | array[object] | No       | Array of {x, y} points defining the pour boundary. If omitted, the board outline is used. |

**Usage Notes:**

- Copper pours are typically used for ground and power planes
- If no outline is specified, the pour fills the entire board area
- Custom outlines are defined as arrays of coordinate points
- Clearance defines the minimum distance from other copper features
- After adding a pour, use `refill_zones` to fill it

**Example:**

```json
{
  "layer": "B.Cu",
  "net": "GND",
  "clearance": 0.2,
  "outline": [
    { "x": 10.0, "y": 10.0 },
    { "x": 90.0, "y": 10.0 },
    { "x": 90.0, "y": 60.0 },
    { "x": 10.0, "y": 60.0 }
  ]
}
```

---

### refill_zones

Refill all copper zones on the board.

**Parameters:**

None

**Usage Notes:**

- WARNING: SWIG path has known segfault risk (see KNOWN_ISSUES.md)
- Prefer using IPC backend (KiCAD open) or triggering zone fill via KiCAD UI instead
- Required after adding or modifying copper pours to calculate the filled areas
- Recalculates all zone fills based on current board state
- May take several seconds on complex boards with many zones

**Example:**

```json
{}
```

---

## Example Workflows

### Point-to-Point Routing with route_pad_to_pad

The simplest and most robust approach for connecting component pads:

```json
// Connect pin 1 of U1 to pin 5 of R1
{
  "tool": "route_pad_to_pad",
  "params": {
    "fromRef": "U1",
    "fromPad": "1",
    "toRef": "R1",
    "toPad": "5",
    "width": 0.25
  }
}
```

This automatically:

- Looks up the exact pad positions
- Detects the net from the pads
- Creates the trace on the appropriate layer
- Inserts a via if the pads are on different copper layers

### Differential Pair Routing (USB, Ethernet)

For high-speed differential signals like USB D+ and D-:

```json
// 1. Create nets if needed
{
  "tool": "add_net",
  "params": {"name": "USB_DP"}
}
{
  "tool": "add_net",
  "params": {"name": "USB_DN"}
}

// 2. Route the differential pair
{
  "tool": "route_differential_pair",
  "params": {
    "positivePad": {"reference": "U1", "pad": "14"},
    "negativePad": {"reference": "U1", "pad": "15"},
    "layer": "F.Cu",
    "width": 0.2,
    "gap": 0.2,
    "positiveNet": "USB_DP",
    "negativeNet": "USB_DN"
  }
}
```

### Replicating Routing Patterns

For repeated circuit blocks (e.g., multiple identical LED drivers):

```json
// Route the first instance (U1, R1, C1) manually, then copy to others
{
  "tool": "copy_routing_pattern",
  "params": {
    "sourceRefs": ["U1", "R1", "C1"],
    "targetRefs": ["U2", "R2", "C2"],
    "includeVias": true
  }
}

// Copy the same pattern to a third instance
{
  "tool": "copy_routing_pattern",
  "params": {
    "sourceRefs": ["U1", "R1", "C1"],
    "targetRefs": ["U3", "R3", "C3"],
    "includeVias": true
  }
}
```

### Adding a Ground Plane

```json
// 1. Create the copper pour on bottom layer
{
  "tool": "add_copper_pour",
  "params": {
    "layer": "B.Cu",
    "net": "GND",
    "clearance": 0.2
  }
}

// 2. Fill the zones
{
  "tool": "refill_zones",
  "params": {}
}
```

Note: Use the IPC backend (keep KiCAD open) when using refill_zones to avoid potential segfaults with the SWIG backend.

---

## Source Files

- **TypeScript Tool Definitions**: `/home/chris/MCP/KiCAD-MCP-Server/src/tools/routing.ts`
- **Python Implementation**: `/home/chris/MCP/KiCAD-MCP-Server/python/commands/routing.py`
