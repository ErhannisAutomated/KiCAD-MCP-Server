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

## Routing Topology Analysis (8 tools)

Read-only tools that answer "is this *geometrically* routable, and where's
the bottleneck?" *before* you call the autorouter and wait minutes for it
to fail. Both compute the trace-width configuration space directly:

```
free_space(layer, W, C) = board_area \ (foreign_copper(layer) ⊕ disk(W/2 + C))
```

— rasterized at `resolutionMm` (default 0.05 mm = ¼ of a 0.2 mm signal
trace). Pairs naturally with `analyze_congestion` (placement-stage density
heatmap) and the post-failure obstacle dumps that `route_pad_to_pad` /
`find_via_lane` produce. See `docs/TOPOLOGY_TOOLS_PLAN.md` for the full
design.

### analyze_routable_regions

Partition a copper layer into the free-space components a trace of
`widthMm` could occupy. Returns each component's area, bbox, and the pads
bordering it, plus any pads whose own escape lane can't fit the trace at
this width (the QFN-internal-escape diagnostic). With `net` set, that net's
own copper is excluded from the obstacle set so its pads anchor into the
regions they actually border. With `net` omitted, ALL copper is obstacle
— the layer-wide free-corridor map.

| Parameter         | Type    | Required | Default | Description |
| ----------------- | ------- | -------- | ------- | ----------- |
| layer             | string  | Yes      | —       | `"F.Cu"` / `"B.Cu"` / `"In1.Cu"` etc. |
| widthMm           | number  | Yes      | —       | Trace width whose configuration space is being analyzed. |
| clearanceMm       | number  | No       | netclass | Clearance to foreign copper. |
| net               | string  | No       | none    | Per-net mode: exclude this net's own copper from the obstacle set. |
| resolutionMm      | number  | No       | 0.05    | Grid step. Use ≤ widthMm/4 for stable answers. |
| convergenceCheck  | boolean | No       | false   | Also run at g/2 and compare component counts. Use this to confirm grid-independence. |
| boardPath         | string  | No       | current | Load a specific .kicad_pcb. |

**Returns:** `componentCount`, `regions[]` (`areaMm2`, `bbox`, `pads[]`),
`padsWithoutEscape[]`, optional `convergence` block.

**Two pads on the same net are routable on this layer iff they fall in
the same component.** That's the binary go/no-go answer the freerouter
can't give you without a multi-minute solve.

### check_pad_routability

Per-pair version of `analyze_routable_regions`: are these two pads in the
same component at `widthMm`, and what's the bottleneck along the path?
Returns `{reachable, bottleneckWidthMm, pathXy[]}`. Reasons when
unreachable: `pads_on_different_nets`, `different_components` (corridor
between them too tight at this width — the placement or netclass is the
cause, not the router), `from_pad_no_escape` / `to_pad_no_escape` (the
pad's own escape lane is the problem).

| Parameter         | Type    | Required | Default  | Description |
| ----------------- | ------- | -------- | -------- | ----------- |
| fromRef           | string  | Yes      | —        | Source component reference. |
| fromPad           | string  | Yes      | —        | Source pad number. |
| toRef             | string  | Yes      | —        | Destination component reference. |
| toPad             | string  | Yes      | —        | Destination pad number. |
| layer             | string  | Yes      | —        | Copper layer. |
| widthMm           | number  | Yes      | —        | Trace width to test. |
| clearanceMm       | number  | No       | netclass | Override clearance. |
| resolutionMm      | number  | No       | 0.05     | Grid step (raster mode only). |
| convergenceCheck  | boolean | No       | false    | Re-run at g/2 and compare (raster mode only). |
| mode              | string  | No       | "raster" | Engine: `"raster"` (default) or `"exact"`. |
| boardPath         | string  | No       | current  | Load a specific board. |

`bottleneckWidthMm` is the maximum trace width that still fits at the
tightest point along the BFS path through the free-space raster. Use it
to back off the netclass width or pick a wider-tolerant route. `pathXy`
is for visualisation only — the freerouter still picks the exact geometry.

**`mode="exact"` (Phase 4c).** Polygon-exact engine via shapely
(`unary_union(obstacles).buffer(W/2 + C)` + `Polygon.difference`). No
grid quantization → authoritative reachability answer. Returns
`{reachable, reason, mode, fromComponent, toComponent, componentCount}`
only — `bottleneckWidthMm` and `pathXy` are NOT reported (not naturally
computable from the polygon set). Use the raster mode for the
bottleneck question and `mode="exact"` for the final
"really-really-sure?" verification when raster `convergenceCheck`
disagrees between `g` and `g/2`. Pad anchoring uses the pad's *escape
zone* (pad polygon buffered by W/2+C+ε) to handle both per-net and
all-copper-as-obstacle cases uniformly.

**Workflow:** placement → `analyze_routable_regions(layer, width)` →
inspect components / unrouted-pads → fix placement → re-analyze →
`autoroute`. Catches "geometrically impossible at this netclass width"
before the long autoroute run.

### max_width_between

Widest single trace that still leaves two pads in the same free-space
component on `layer`. Binary search on the same rasterized obstacle
field used by `check_pad_routability` (one EDT, ~10 component-label
probes — typically faster than 10 freerouting attempts). Read-only.

| Parameter         | Type    | Required | Default | Description |
| ----------------- | ------- | -------- | ------- | ----------- |
| fromRef/fromPad   | string  | Yes      | —       | Source pad. |
| toRef/toPad       | string  | Yes      | —       | Destination pad. |
| layer             | string  | Yes      | —       | Copper layer. |
| clearanceMm       | number  | No       | netclass | Override clearance. |
| resolutionMm      | number  | No       | 0.05    | Grid step. |
| widthToleranceMm  | number  | No       | 2×g     | Binary-search tolerance. Sub-grid answers are noise. |
| upperBoundMm      | number  | No       | derived | Skip the preflight by passing the search ceiling explicitly. |
| boardPath         | string  | No       | current | Load a specific board. |

**Returns:** `maxWidthMm` plus `iterations`, `upperBoundMm`,
`searchToleranceMm`. Reasons when unreachable: `pads_on_different_nets`,
`different_components` (the obstacle field separates the pads at any
positive width — the placement, not the netclass, is the cause).

This is the **max-bottleneck path** width, not the shortest-path
bottleneck reported by `check_pad_routability`. The two metrics differ
when a longer, wider corridor exists alongside a shorter, narrower one.

### max_parallel_traces

How many parallel traces of `widthMm` (each with its own clearance
margin) fit through the **widest** corridor between two pads? Computes
`floor(maxCorridorWidthMm / (widthMm + 2 × clearanceMm))`, where
`maxCorridorWidthMm` comes from `max_width_between` (not the shortest
path's bottleneck — see the rationale above). Read-only.

| Parameter      | Type    | Required | Default | Description |
| -------------- | ------- | -------- | ------- | ----------- |
| fromRef/fromPad| string  | Yes      | —       | Source pad. |
| toRef/toPad    | string  | Yes      | —       | Destination pad. |
| layer          | string  | Yes      | —       | Copper layer. |
| widthMm        | number  | Yes      | —       | Per-trace width. |
| clearanceMm    | number  | No       | netclass | Override clearance. |
| resolutionMm   | number  | No       | 0.05    | Grid step. |
| boardPath      | string  | No       | current | Load a specific board. |

**Returns:** `maxParallelTraces`, `pitchMm` (= W + 2C),
`maxCorridorWidthMm`. Use it to size a bus ("can I run all 5 SPI
signals through this gap?") before committing to a placement.

### routability_heatmap

Geodesic-distance heatmap from a source pad: where can a trace of
`widthMm` reach on `layer`, and how far? Bright = far reachable;
black/NaN = unreachable enclave or obstacle. Writes a PNG to
`/tmp/claude-1000` (or `outputPath`). Read-only. Use this to **see**
the shape of the reachable region when `check_pad_routability` reports
`different_components` — the gap in the heatmap is exactly the corridor
that's too tight. Falls back to a numeric summary if matplotlib isn't
importable.

| Parameter      | Type    | Required | Default | Description |
| -------------- | ------- | -------- | ------- | ----------- |
| fromRef/fromPad| string  | Yes      | —       | Source pad. |
| layer          | string  | Yes      | —       | Copper layer. |
| widthMm        | number  | Yes      | —       | Trace width. |
| clearanceMm    | number  | No       | netclass | Override clearance. |
| resolutionMm   | number  | No       | 0.05    | Grid step = pixel size. |
| outputPath     | string  | No       | derived | Explicit PNG path. |
| boardPath      | string  | No       | current | Load a specific board. |

**Returns:** `vizPath`, `reachableAreaMm2`, `maxReachMm`, the source
pad's xy in mm.

### check_pad_routability_multilayer

Multi-layer reachability with via bridges. Phase 3 of the topology
tools — extends `check_pad_routability` from one layer to N layers via
a per-via meta-graph. Through-vias only. Read-only.

For each enabled copper layer (or `layers` subset), the per-layer EDT
is built once. A *via-candidacy* mask per layer-pair marks pixels where
a via of `viaDiameterMm` fits in BOTH layers' free spaces. Union-find on
`(layer, component_id)` nodes — bridged by the via-candidacy mask —
answers "are the two pads connected across all available layers and
via positions?".

| Parameter         | Type    | Required | Default | Description |
| ----------------- | ------- | -------- | ------- | ----------- |
| fromRef/fromPad   | string  | Yes      | —       | Source pad. |
| toRef/toPad       | string  | Yes      | —       | Destination pad. |
| widthMm           | number  | Yes      | —       | Trace width. |
| viaDiameterMm     | number  | Yes      | —       | Via diameter (used as `2 × (radius + clearance)` in the candidacy mask). |
| clearanceMm       | number  | No       | netclass | Trace clearance. |
| viaClearanceMm    | number  | No       | clearanceMm | Separate via clearance. |
| layers            | array   | No       | all     | Subset of copper layers. Restrict to ask "F.Cu + In1.Cu only?". |
| resolutionMm      | number  | No       | 0.05    | Grid step. |
| boardPath         | string  | No       | current | Load a specific board. |

**Returns:** `reachable`, `sameLayerReachable` (the trace stays on one
layer iff the anchors land in the same `(layer, component)` node),
`viaCandidates[]` (one representative `(x, y, layerA, layerB,
componentA, componentB)` per component-bridge — *where* you could drop
a via, NOT a routing prescription), `viaCandidatesTotal`,
`layerComponents` (per-layer connected-component counts at the queried
width).

**Reasons when unreachable:** `pads_on_different_nets`,
`from_pad_no_escape` / `to_pad_no_escape` (no escape lane on any
considered layer), `unreachable_any_layer` (even with via bridges, the
union-find leaves the pads disconnected — the placement, not the
netclass or stackup, is the cause).

### routability_report

All-ratlines multi-layer feasibility matrix at the queried width + via
size. Phase 3 of the topology tools — flags ratlines that are
geometrically impossible BEFORE the autoroute attempt. Read-only.

Per net (≥ 2 pads): builds a spanning star from the first pad to the
rest (capped by `maxPairsPerNet` — GND-style large nets won't blow up
runtime), then asks `check_pad_routability_multilayer` for each pair.

| Parameter        | Type    | Required | Default | Description |
| ---------------- | ------- | -------- | ------- | ----------- |
| widthMm          | number  | Yes      | —       | Trace width (all ratlines queried at this width). |
| viaDiameterMm    | number  | Yes      | —       | Via diameter. |
| clearanceMm      | number  | No       | design default | Trace clearance. |
| viaClearanceMm   | number  | No       | clearanceMm | Via clearance. |
| layers           | array   | No       | all     | Copper layers to consider. |
| resolutionMm     | number  | No       | 0.05    | Grid step. |
| nets             | array   | No       | all     | Restrict to these nets. |
| maxPairsPerNet   | number  | No       | 64      | Cap pairs per net to bound runtime. |
| boardPath        | string  | No       | current | Load a specific board. |

**Returns:** `summary` (`totalRatlines`, `reachable`, `unreachable`,
`sameLayer`, `viaRequired`), `ratlines[]` (each: `net`, `fromRef/Pad`,
`toRef/Pad`, `reachable`, `sameLayerReachable`, `reason`),
`limitations` (the per-net caveat — see CAVEAT below).

**CAVEAT:** uses the all-copper-is-obstacle approximation for speed
(builds one meta-graph for the whole board, reuses across nets). A
ratline marked unreachable here MIGHT still route per-net — confirm
with `check_pad_routability_multilayer` per-net for any flagged
ratline.

### pre_route_audit

Pre-flight all-ratlines feasibility check at each net's **own**
netclass widths. The Phase-4 workflow tool — run this BEFORE the
autoroute attempt to flag impossible ratlines with actionable
remediation hints. Read-only.

Per netclass on the board: builds the multi-layer meta-graph at the
class's `(trackWidth, clearance, viaDiameter, viaClearance)`, then
queries every net assigned to the class. The per-layer EDT cache is
**shared** across netclasses (the obstacle SET is the same; only the
width/clearance erosion differs), so the cost scales with the number
of netclasses, not with the total ratline count.

| Parameter                | Type    | Required | Default | Description |
| ------------------------ | ------- | -------- | ------- | ----------- |
| widthMmOverride          | number  | No       | per-netclass | Use one width for all nets instead of the per-netclass lookup. |
| viaDiameterMmOverride    | number  | No       | per-netclass | Override via diameter. |
| clearanceMmOverride      | number  | No       | per-netclass | Override clearance. |
| viaClearanceMmOverride   | number  | No       | clearance   | Override via clearance. |
| layers                   | array   | No       | all       | Copper layers to consider. |
| resolutionMm             | number  | No       | 0.05      | Grid step. |
| nets                     | array   | No       | all       | Restrict to these nets. |
| maxPairsPerNet           | number  | No       | 64        | Cap pairs per net to bound runtime. |
| boardPath                | string  | No       | current   | Load a specific board. |

**Returns:** `summary` + `ratlines[]` (each with the standard Phase-3
shape plus `netclass`, `trackWidthMm`, `clearanceMm`, `viaDiameterMm`,
and — when `reachable=false` — a `remediationHint` string), plus
`netclassesEvaluated[]` (per-class counts of reachable / unreachable /
sameLayer / viaRequired), plus the same `limitations` caveat as
`routability_report`.

**Remediation hints:** mapped from the Phase-3 failure reasons:
- `from_pad_no_escape` / `to_pad_no_escape` → "Pad has no escape lane
  at width W mm. Lower the netclass width, move foreign-net copper
  away from the pad, or pin-escape with a narrow stub."
- `unreachable_any_layer` → "Pads cannot be connected at netclass
  'X' on ANY layer even with via bridges. Move components closer,
  widen the corridor, or assign a netclass with narrower width."

**Workflow:** placement → `pre_route_audit` → for each unreachable
ratline: investigate with `routability_heatmap` and/or
`check_pad_routability_multilayer` (per-net for the precise answer) →
fix placement/netclass → re-audit → `autoroute`.

### route_pad_to_pad failure enrichment (not a new tool — note)

When `route_pad_to_pad` fails with `Route blocked by N obstacle(s)`,
the response now also carries a `topologyHint` field with the
`check_pad_routability` / `check_pad_routability_multilayer` answer
for the same pad pair. This distinguishes "pads ARE reachable, just
not via a straight line — use route_trace or autoroute" from "pads are
geometrically separated at this width — fix the placement or
netclass" without a follow-up tool call. Best-effort: any error in
the topology analysis is silently swallowed and the raw obstacle list
remains the authoritative answer.

## Trace Operations (4 tools)

### scrub_region

Region-scoped copper cleanup after a re-placement (`relax_placement`) or
incremental re-route. Given target components, computes a per-layer convex
hull of their footprints and deletes copper that is either (a) on a
**target-only net** (every pad on the net belongs to a target) — anywhere,
any layer; or (b) on a **shared net** (touches a target AND a non-target) —
only inside the hull. A recursive **dead-end prune** then sweeps anything
left dangling. This is the geometric scope that net-name stripping
(`autoroute(nets=…)`'s clear step) lacks: it removes the charger's slice of
USB_VBUS without nuking the whole rail.

**Dry-run by default.** Returns an authoritative kill-list (each item tagged
with a reason code — `target-only-net` / `endpoint-in-hull` /
`intersects-hull` / `in-hull` / `dead-end-prune`), interlopers
(non-targets inside the hull and their affected nets), flagged-but-spared
items, a debug viz PNG path, and **`nowOpenNets`** to feed straight into
`autoroute(nets=…)`. Pass `dryRun=false` to delete.

Margin is implemented as a *distance threshold* (point-to-hull distance ≤
`marginMm`), not a polygon offset — so degenerate single-point / single-edge
hulls work naturally. Toggles: `layers` (default `["F.Cu","B.Cu"]`, which
keeps inner power pours out of scope), `marginMm` (default 1.0),
`affectTracks` / `affectVias` / `affectZones`, `intersectHull` (opt-in
segment-crosses-hull for tracks), `pruneDeadEnds` (default true). Deletions
use `RemoveNative` (SWIG-corruption-safe).

**Pipeline:** `relax_placement` → `scrub_region(dryRun=true)` (inspect
kill-list + viz) → snapshot/commit board → `scrub_region(dryRun=false)` →
`autoroute(nets=nowOpenNets)` → `refill_zones` + `via_orphan_pads` →
`run_drc`. See `docs/SCRUB_REGION_PLAN.md` for the full design rationale.

Live-validated on the power_module charger: 11 shorts → 0; every charger
DRC error mapped to a kill-list item before commit.

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

Query traces on the board with optional filters by net, layer, or bounding box. On dense boards a full per-trace dump can overflow the tool-result budget — `summarize`, `limit`, and `offset` let callers ask for only what they need (#237).

**Parameters:**

| Parameter   | Type    | Required | Description                                                                                                                                                              |
| ----------- | ------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| net         | string  | No       | Filter by net name                                                                                                                                                       |
| layer       | string  | No       | Filter by layer name                                                                                                                                                     |
| boundingBox | object  | No       | Filter by bounding box region (x1, y1, x2, y2, optional unit)                                                                                                            |
| unit        | string  | No       | Unit for coordinates: "mm" or "inch"                                                                                                                                     |
| summarize   | boolean | No       | Return per-net counts + total length + layer breakdown only, no individual trace records. ~30× smaller in measured cases (142KB → 4.7KB on a populated board).           |
| limit       | number  | No       | Cap per-trace records emitted in this response. `traceTotal` / `viaTotal` always reflect the full filtered counts, so callers can keep paging.                           |
| offset      | number  | No       | Skip the first N filtered records before emitting `limit`. Pairs with `limit` to walk a large set in chunks.                                                             |

**Usage Notes:**

- Default (no `summarize`/`limit`) preserves the original full-dump
  behaviour — passes through unchanged.
- `summarize: true` is the lightest mode and is the recommended first
  pass: tells you whether a deeper query is even worth the budget.
- When paginating, watch `traceTotal` / `viaTotal` to know when you've
  walked the full set — `summarize` is not emitted in paginated mode.
- Bounding box uses rectangular region defined by opposite corners.

**Example (full dump, single net):**

```json
{
  "net": "VCC_3V3",
  "layer": "F.Cu"
}
```

**Example (summary across all nets):**

```json
{ "summarize": true }
```

**Example (paginate dense net):**

```json
{ "net": "GND", "limit": 200, "offset": 0 }
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
