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

### pin_zone_same_net

Proactive counterpart to `bridge_same_net_pins`. Drops a single zone covering runs of contiguous same-net pins on an IC **before** autoroute, so the autorouter never creates a thin sub-min-width bridge that would have to be retroactively fixed.

**Parameters:**

| Parameter           | Type    | Required | Description                                          |
| ------------------- | ------- | -------- | ---------------------------------------------------- |
| nets                | array   | No       | Optional net filter (default: all nets).             |
| components          | array   | No       | Optional component-ref filter (default: all).        |
| marginMm            | number  | No       | Margin (mm) around the pad-union bbox (default 0.1). |
| adjacencyFactor     | number  | No       | Adjacency threshold multiplier on NN-dist (default 1.25). |
| minPinDistMm        | number  | No       | Min distance below which two pads are considered co-located, not adjacent (default 0.001). |
| absoluteMaxDistMm   | number  | No       | Absolute cap (mm) on adjacent-pair distance regardless of NN ratio (default 5.0). |
| connection          | string  | No       | `"solid"` (default) or `"thermal"`.                  |
| apply               | boolean | No       | Commit the zones (default `false` = preview).        |

**Usage Notes:**

- Adjacency heuristic: for each pad, compute distance to all other same-net pads on the same footprint+layer; NN-dist = smallest above `minPinDistMm`. Two pads are adjacent iff distance ≤ `adjacencyFactor × max(NN-dist of A, NN-dist of B)`.
- At `adjacencyFactor=1.25`, opposite-side IC pins are naturally rejected (distance ~ body width >> 1.25 × pitch); corner-adjacent QFN pins still cluster.
- `absoluteMaxDistMm` handles the 2-pad pathology: with exactly 2 pads on a net, each is the other's only NN, so the NN-based threshold always accepts them. Cap rejects far-apart pairs regardless.
- After computing each cluster's bbox, the tool scans all pads on the zone's layer. **If any non-cluster foreign-net pad's bbox intersects the zone bbox, the zone is rejected** (surfaced in `rejected` with the offending pads). Prevents zones that would bridge over inter-pin gaps onto pads on different nets.
- Result includes both `zones` (accepted) and `rejected` (with reasons), so failures are visible at the preview step.

**Example:**

```json
{ "components": ["U4"], "adjacencyFactor": 1.25, "apply": true }
```

---

### widen_return_paths

Widen GND/return-net stubs near high-current components. The power-rail trace into the component is netclass-sized (POWER_4A = 1.5 mm) but the GND return stub on the same component defaults to 0.2 mm — carrying the same current to the GND plane via. This tool fixes the thermal/IR-drop asymmetry.

**Parameters:**

| Parameter     | Type    | Required | Description                                          |
| ------------- | ------- | -------- | ---------------------------------------------------- |
| netClass      | string  | No       | Netclass identifying high-current components (default `"POWER_4A"`). |
| returnNets    | array   | No       | Nets to widen (default `["GND"]`).                   |
| width         | number  | No       | Explicit target width mm (default: netclass track width). |
| minClearance  | number  | No       | Clearance vs foreign-net copper when widening (default 0.15). |
| pairedVias    | boolean | No       | Place an in-line partner past each stub's terminating via (default false). |
| apply         | boolean | No       | Commit the widened widths (default `false` = preview). |

**Usage Notes:**

- For each footprint with at least one pad on a high-current net, walks each of its return-net pads via BFS along same-net tracks, stopping at the first same-net via. The traversed segments are the "return stub."
- Each candidate segment is clearance-checked via the swept-trace test (#177) — segments that would short adjacent foreign-net copper are skipped and reported.
- With `pairedVias=true`, places an in-line partner past each stub's terminating via for current sharing and inductance symmetry (in-line is electrically equivalent to a fork because the plane absorbs current at each via).

**Example:**

```json
{ "netClass": "POWER_4A", "returnNets": ["GND"], "apply": true }
```

---

### via_orphan_pads

Drop a via adjacent to every F.Cu/B.Cu SMD pad on a plane net (GND, BAT+, V12_OUT) that isn't already plane-connected. Necessary post-autoroute step because freerouting respects `(type power)` plane layers (per #203) by *not* placing landing vias on them, leaving SMD pads floating relative to the inner-layer pour.

**Parameters:**

| Parameter     | Type    | Required | Description                                          |
| ------------- | ------- | -------- | ---------------------------------------------------- |
| net           | string  | Yes      | Plane net to via (e.g. `"GND"`, `"BAT+"`).           |
| layer         | string  | No       | Pad side: `"F.Cu"`, `"B.Cu"`, or `"both"` (default `"F.Cu"`). |
| viaDiameter   | number  | No       | Via outer diameter mm (default 0.6).                 |
| viaDrill      | number  | No       | Via drill mm (default 0.3).                          |
| viaOffset     | number  | No       | Gap between pad edge and via edge mm (default 0.6).  |
| stubWidth     | number  | No       | Stub trace width mm. Default: `max(0.25, pad netclass min track width)`. |
| minClearance  | number  | No       | Min gap mm between via edge and foreign-net copper (default 0.15). |
| apply         | boolean | No       | Commit (default `false` = preview).                  |
| maxVias       | number  | No       | Safety cap (default 200).                            |
| maxOffsetMultiplier | number | No  | When all 4 cardinals at the base `viaOffset` are blocked, retry at 2× / 3× / ... × viaOffset up to this multiplier (default 4). Pass `1` to disable the retry. Each proposed via reports its `offsetMultiplier` (#226). |

**Usage Notes:**

- Via-NEAR-pad with a short stub trace; no via-in-pad, so no special manufacturing required.
- Skips PTH/NPTH pads (already plane-connected via through-hole drill).
- Detects embedded thermal vias: an SMD thermal pad whose footprint has PTH same-net pads inside its bbox (e.g. KiCAD's `*_ThermalVias` HTSSOP footprints) is treated as plane-connected.
- Stub width defaults to the pad's netclass minimum — so a BAT+ stub is POWER_4A's 1.0 mm (1.5 mm preferred), not 0.25 mm. Prevents `track_width` DRC violations on the new stubs.
- Conservative connectivity: only same-net VIAS within pickup radius count as "already connected." Adjacent same-net tracks don't, because they may form an orphan chain (pads bonded only to each other, not to the plane).
- Both the via *position* and the stub *trace* are clearance-checked against foreign-net copper. If all 4 cardinals at the base offset fail, the tool falls back to extended offsets (`2×`, `3×`, ... up to `maxOffsetMultiplier`) before reporting `skippedNoClearance`. Each proposed via reports the `offsetMultiplier` it ended up at — `1` is the base case, higher values mean the pad was crowded and the via had to be placed further out.

**Example:**

```json
{ "net": "GND", "apply": true }
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

### find_redundant_vias

Scan the board for vias whose drill edge is within `m_HoleToHoleMin` of
another via or a PTH pad. Counterpart to `add_via`'s pre-flight
clearance check — that prevents new violations; this cleans up legacy
ones already on the board (e.g. left over from earlier autoroute or
`via_orphan_pads` runs).

**Parameters:**

| Parameter      | Type    | Required | Description                                                                                                                |
| -------------- | ------- | -------- | -------------------------------------------------------------------------------------------------------------------------- |
| holeToHoleMin  | number  | No       | Override the minimum drill-to-drill gap in mm. Defaults to the board's design-rule `m_HoleToHoleMin`.                       |
| net            | string  | No       | Net filter — only scan vias on this net. Useful for cleaning up after a single `via_orphan_pads` run.                       |
| delete         | boolean | No       | Actually remove the proposed redundant vias (default false = preview).                                                      |

**Result fields:**

- `conflictCount` — number of unique conflicts detected
- `conflicts[]` — each entry has `redundantUuid`, `redundantPosition`,
  `keepUuid`, `keepPosition`, `gapMm`, `requiredGapMm`, and `reason`
  ("same-net stacked vias", "via overlaps PTH pad drill", or
  "different-net vias too close")
- `removedCount` — number of vias actually deleted (when `delete=true`)

**Redundant-vs-keep selection:**

- For a via-vs-via pair: lexicographically-larger UUID is the redundant
  one. Deterministic so callers can reproduce the choice.
- For a via-vs-PTH-pad pair: the via is always redundant — pads can't
  be removed.

**Example:**

```json
{
  "delete": false
}
```

**Example response:**

```json
{
  "success": true,
  "conflictCount": 2,
  "removedCount": 0,
  "holeToHoleMinMm": 0.25,
  "conflicts": [
    {
      "redundantUuid": "a0ea97a6-1556-4d0d-903c-6e4f0c1917bc",
      "redundantType": "via",
      "redundantNet": "GND",
      "redundantPosition": { "x": 65.159, "y": 19.799, "unit": "mm" },
      "keepUuid": "45edb5f3-5087-4a6a-864e-be0bb527ad3a",
      "keepType": "via",
      "keepNet": "GND",
      "keepPosition": { "x": 65.159, "y": 19.299, "unit": "mm" },
      "gapMm": 0.200,
      "requiredGapMm": 0.250,
      "reason": "same-net stacked vias"
    }
  ]
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

Add a via to the PCB. By default refuses (`checkClearance=true`) when the
proposed via would conflict with foreign-net copper or violate the board's
min hole-to-hole — preventing the common "place via → DRC short → delete
via" loop.

**Parameters:**

| Parameter        | Type    | Required | Description                                                                                                                                                                |
| ---------------- | ------- | -------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| position         | object  | Yes      | Via position with x, y, and optional unit                                                                                                                                  |
| net              | string  | Yes      | Net name                                                                                                                                                                   |
| viaType          | string  | No       | Via type: "through", "blind", or "buried"                                                                                                                                  |
| checkClearance   | boolean | No       | Pre-flight check: refuse the via if it would come within netclass clearance of foreign-net copper or within `m_HoleToHoleMin` of another drilled hole (default: **true**). |
| clearance        | number  | No       | Override the copper-clearance margin (mm). Defaults to the net's netclass clearance, falling back to the board default.                                                    |

**Usage Notes:**

- Through vias connect all layers (default)
- Blind vias connect an outer layer to one or more inner layers
- Buried vias connect two or more inner layers without reaching outer layers
- Position coordinates use mm by default
- When `checkClearance` rejects a via, the response carries an `obstacles`
  list naming the foreign-net copper item and the missing gap in mm
- Pass `checkClearance: false` only when you've already verified the spot
  via another tool (e.g. `via_orphan_pads` uses the same logic internally
  and is the preferred path for bulk plane stitching)

**Example:**

```json
{
  "position": { "x": 110.0, "y": 50.0, "unit": "mm" },
  "net": "GND"
}
```

**Example — failure response:**

```json
{
  "success": false,
  "message": "Via placement blocked by 2 obstacle(s)",
  "obstacles": [
    "track on net 'BB_FB' on F.Cu near (74.02,63.12) (-0.350 mm gap, need ≥0.200 mm)",
    "via on net 'GND' at (63.58,67.82) (hole-to-hole gap 0.100 mm, need ≥0.250 mm)"
  ]
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

Delete traces from the PCB. Can delete by UUID, position, or bulk-delete
all traces on a net. Every success response includes a `deleted` array
describing each removed item (uuid, type 'track'|'via', net, layer,
position) so the caller can verify exactly what was removed — important
for position-based deletes, which pick the geometrically-nearest item
and can silently grab the wrong segment in dense areas.

**Parameters:**

| Parameter   | Type    | Required | Description                                                                                                                                                                                              |
| ----------- | ------- | -------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| traceUuid   | string  | No       | UUID of a specific trace to delete                                                                                                                                                                       |
| position    | object  | No       | Delete trace nearest to this position (x, y, optional unit)                                                                                                                                              |
| net         | string  | No       | Bulk: delete all traces on this net (use "*" for all). With `position`: scope the nearest-search to this net.                                                                                            |
| layer       | string  | No       | Filter by layer. Used in both bulk-by-net mode (tracks on this layer only) and position mode (tracks on this layer only — vias touch all layers so the filter does not apply to them).                   |
| kind        | string  | No       | Position-mode only: `"track"`, `"via"`, or `"any"` (default). Restricts the nearest-search to one kind of item — use when several segments converge at a coordinate and you need to disambiguate (#223). |
| includeVias | boolean | No       | Include vias in net-based bulk delete (use with net="*" to strip the whole board)                                                                                                                        |

**Mode selection:**

- `traceUuid` only → delete that exact item
- `position` only → nearest-search across all tracks/vias on board
- `position` + (`net`/`layer`/`kind`) → nearest-search scoped to filter
- `net` alone (no `position`/`traceUuid`) → bulk delete by net

**Usage Notes:**

- Position-based search uses a 1mm pickup radius
- When the position-search finds nothing matching the filter, the
  `errorDetails` enumerates which filters were active so callers can
  tell whether to widen the search

**Example (scoped position delete):**

```json
{
  "position": { "x": 67.4, "y": 19.8, "unit": "mm" },
  "kind": "via",
  "net": "GND"
}
```

**Example response (success):**

```json
{
  "success": true,
  "message": "Deleted track at specified position",
  "deleted": [
    {
      "uuid": "a0ea97a6-1556-4d0d-903c-6e4f0c1917bc",
      "type": "via",
      "net": "GND",
      "position": { "x": 65.158699, "y": 19.7992, "unit": "mm" },
      "fromLayer": "F.Cu",
      "toLayer": "B.Cu"
    }
  ]
}
```

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
