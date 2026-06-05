/**
 * Placement-constraint tools for KiCAD MCP server.
 *
 * decoupling_audit: walk the schematic, find cap↔IC decoupling pairs
 *   (auto-discovered by net analysis OR explicit Placement_Anchor
 *   property), measure the PCB distance, and flag any that exceed the
 *   anchor's `within=Nmm`.
 *
 * place_near: snap one or more PCB footprints to within Nmm of a
 *   target pad or footprint, respecting bbox collisions.
 *
 * Property model (controlled by `mcp_constraint_version: 1` in
 * .kicad_pro):
 *
 *   Placement_Anchor = "<REF>[.<PIN>]/within=<N>mm[; ...]"
 *
 * The property lives on the SCHEMATIC symbol (KiCad's schematic→PCB
 * sync transfers only Reference/Value/Footprint/Datasheet/Description,
 * so constraints stay on the schematic side). Rename-propagation hooks
 * keep the values consistent when symbols are renamed.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";

export function registerPlacementTools(server: McpServer, callKicadScript: Function) {
  // verify_netclass_patterns
  server.tool(
    "verify_netclass_patterns",
    "Compare the live `net_settings.netclass_patterns` in `.kicad_pro` against the expected set, stored either on the schematic's Schematic_Metadata singleton (preferred, #230) or in the legacy `.kicad_pro` `mcp_expected_netclass_patterns` key. KiCAD's GUI has been observed to silently strip patterns on save when it normalises the project across version upgrades — once gone, affected nets fall back to Default netclass and routing tools silently pick the wrong width. First call on a project bootstraps the expected section from the current state (no drift reported). Pass restore=true to re-add missing patterns. The autoroute tool runs this automatically as a pre-flight check and surfaces drift in `netclassPatternDrift` on its response. The response's `expectedSource` field reports whether the expected set came from the singleton or `.kicad_pro`.",
    {
      proPath: z.string().optional().describe("Path to the .kicad_pro. Defaults to the currently-loaded board's sibling .kicad_pro."),
      schematicPath: z.string().optional().describe("Path to the .kicad_sch. Defaults to the proPath's sibling .kicad_sch. When the file exists, the Schematic_Metadata singleton is checked first for the expected-pattern set, falling back to .kicad_pro."),
      restore: z.boolean().optional().describe("Re-add missing patterns into net_settings.netclass_patterns. Default false (report-only)."),
    },
    async (args: any) => {
      const result = await callKicadScript("verify_netclass_patterns", args);
      return {
        content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
      };
    },
  );

  // get_schematic_metadata
  server.tool(
    "get_schematic_metadata",
    "Return the merged metadata dict from all Schematic_Metadata singletons on the schematic. {} when none exist (caller can author the first one via set_schematic_metadata). Excludes the singleton's own scaffolding properties (Reference / Value / Footprint / Datasheet / Description / Schematic_Metadata_Marker) — only the user-facing mcp_* keys are returned. Response also includes a singletons[] list with reference + uuid so multi-singleton merges can be inspected.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch."),
    },
    async (args: any) => {
      const result = await callKicadScript("get_schematic_metadata", args);
      return {
        content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
      };
    },
  );

  // set_schematic_metadata
  server.tool(
    "set_schematic_metadata",
    "Set a single metadata key on the schematic's Schematic_Metadata singleton (#230). Creates the singleton on first call (a DNP symbol placed at the top-left of the schematic; user can drag it elsewhere). Object/array values are JSON-encoded into the property string; primitives (int, str, bool) are stored as-is. Reserved property names (Reference, Value, Footprint, Datasheet, Description, Schematic_Metadata_Marker) are rejected. Subsequent calls with the same key update in place — idempotent.",
    {
      schematicPath: z.string().describe("Path to the .kicad_sch."),
      key: z.string().describe("Property name. Convention: prefix with `mcp_` for keys consumed by MCP tooling."),
      value: z.any().describe("Property value. Primitives stored as-is; objects/arrays serialised to JSON."),
    },
    async (args: any) => {
      const result = await callKicadScript("set_schematic_metadata", args);
      return {
        content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
      };
    },
  );

  // decoupling_audit
  server.tool(
    "decoupling_audit",
    "Find decoupling caps (or any constrained component) too far from their target IC pin on the PCB. Reads `Placement_Anchor` properties on schematic symbols AND auto-discovers cap↔IC power-pin pairs by net analysis (cap with one pad on a power_in/power_out IC pin and the other on GND). Reports pair distance vs. max, plus any unresolved or malformed anchors. Property grammar: `Placement_Anchor = \"U1.10/within=3mm[; U1.9/within=3mm]\"`.",
    {
      schematicPath: z.string().describe("Path to the top-level .kicad_sch (hierarchical sub-sheets are auto-walked)."),
      boardPath: z.string().optional().describe("Path to the .kicad_pcb. Defaults to the currently-loaded board (call open_project first if you don't pass this)."),
      maxDist: z.number().optional().describe("Default max distance in mm for auto-discovered pairs and explicit anchors missing a `within=` value. Default 5.0 mm."),
      includeAutoDiscovered: z.boolean().optional().describe("Auto-discover cap↔IC pairs from net analysis. Default true."),
      includeExplicit: z.boolean().optional().describe("Honor explicit Placement_Anchor properties. Default true."),
    },
    async (args: any) => {
      const result = await callKicadScript("decoupling_audit", args);
      return {
        content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
      };
    },
  );

  // relax_placement (v2: unified force-directed PCB autoplacer)
  server.tool(
    "relax_placement",
    "v2 unified PCB autoplacer (shares the schematic autoplacer engine). Pulls connected components together via pin-wise springs (strength per spring class: DECOUPLING strong, LOCAL_SIGNAL default, INTER_GROUP weak, PLANE zero) and pushes overlapping bodies apart via OBB inverse-cube repulsion. Anchored components (J*/SW*/BAT* refs and through-hole-dominant footprints by default; override with lockedRefs) stay fixed. Schedule: spread (repulsion ramps in geometrically) -> snap (rotation snap ramps in toward 90 degree multiples) -> hard-snap. Power-plane nets (GND/VBAT/+5V/...) auto-classify as PLANE so the engine does not waste pull on rails routed via inner pours. Use dryRun=true to try parameters before committing.",
    {
      lockedRefs: z.array(z.string()).optional().describe("Refs to keep fixed (overrides the J/SW/BAT default). Pass empty array to anchor nothing."),
      marginMm: z.number().optional().describe("Body-repulsion margin in mm (default 1.0). The 1/r^3 repulsion saturates at gap <= margin; force falls off as 1/r^3 once past the margin."),
      springK: z.number().optional().describe("Base attraction spring constant (default 1.0). Multiplied per pair by the resolved spring class spring_k (DECOUPLING=5.0, LOCAL_SIGNAL=1.0, INTER_GROUP=0.3, PLANE=0.0)."),
      repulsionKStart: z.number().optional().describe("Repulsion strength at the start of the spread phase (default 1e-6). Ramps GEOMETRICALLY to repulsionKPeak under the inverse-cube formula; a small start gives a gentler early spread."),
      repulsionKPeak: z.number().optional().describe("Peak repulsion strength reached at end of the spread phase (default 0.1 under the inverse-cube formula)."),
      rotationSnapPeak: z.number().optional().describe("Peak rotation-snap torque strength reached at end of the snap phase (default 30.0). Set 0 for free rotation."),
      pinwiseTorqueK: z.number().optional().describe("Lever-arm torque coupling for pin-wise spring forces (default 1.0). T = (r x F) * k where r is the offset from component center to the pin. Without this, off-center spring forces don't rotate components."),
      clusterIters: z.number().optional().describe("Phase 1 (springs only) iteration count (default 50 — letting the spring topology settle before repulsion acts puts parts on the correct side of their IC far more reliably)."),
      spreadIters: z.number().optional().describe("Phase 2 (repulsion ramps in) iteration count (default 200)."),
      snapIters: z.number().optional().describe("Phase 3 (rotation snap ramps in) iteration count (default 100)."),
      relaxIters: z.number().optional().describe("Phase 4 (full snap, low temp) iteration count (default 0 — skipped because the hard-snap step gives a cleaner final state)."),
      crossLayerSprings: z.boolean().optional().describe("Apply springs between pads on different copper layers (default true). Set false when a B.Cu anchor like a cell holder shouldn't pull F.Cu parts onto its pads."),
      forceStepDamping: z.number().optional().describe("Damping factor on the force-as-displacement step (default 0.3). Below 1.0 prevents period-2 oscillation when components are near equilibrium and force < temperature."),
      normalizeSpringForceByDegree: z.boolean().optional().describe("Divide each component's spring force/torque by its number of spring contributions (default true). Keeps K_eff bounded regardless of pin count — without it, a 28-pin IC has 14x the restoring stiffness of a 2-pin resistor and bucks in dense clusters under the damping that's critical for the resistor."),
      normalizeByIntentGroup: z.boolean().optional().describe("Spring normalization (default true). Three-level average: within each intent group (connections sharing a matched annotation target), then across a pin's groups, then across the component's pins. A named DECOUPLING target gets its own group so it competes on equal footing with the rest of a high-fan-out net (fixing decoupling caps mis-oriented by power-rail pull) without needing PLANE on the rail. Supersedes normalizeSpringForceByDegree when true."),
      boundaryK: z.number().optional().describe("Soft boundary force during iteration when a component drifts past the Edge.Cuts keep-in (default 1.0). Linear restoring force; complementary to the final post-clamp."),
      enforceRotationSnap: z.boolean().optional().describe("After the soft snap phase, hard-round each non-anchored rotation to the nearest 90 degree multiple (default true). Residual lever-arm torque from springs can hold rotations slightly off-axis under the soft snap alone."),
      autoClassifyPlanes: z.boolean().optional().describe("Auto-classify power/ground nets (GND, VBAT, +5V, ...) as PLANE so their springs are skipped (default true)."),
      dryRun: z.boolean().optional().describe("Compute new positions without applying. Default false."),
      boardPath: z.string().optional().describe("Path to .kicad_pcb. Defaults to currently-loaded board."),
    },
    async (args: any) => {
      const result = await callKicadScript("relax_placement", args);
      return {
        content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
      };
    },
  );

  // get_ratsnest
  server.tool(
    "get_ratsnest",
    "Read-only inspection of the ratsnest (the list of pad-pair connections still needing routes — what KiCAD draws as thin lines). Returns per-segment endpoints (with parsed component refs and pad numbers), net, length, plus pairwise geometric crossing detection on different nets. Use this to evaluate \"is this layout routable?\" and to debug placement decisions. Pairs with analyze_congestion (which shows dense regions): ratsnest shows which lines have to thread through those regions. Source: DRC unconnected_items cache from a prior get_drc_violations/run_drc call. Detects staleness automatically (returns `stale: true` + `staleReason` when the board file is newer than the DRC cache); pass `refresh: true` to auto-run DRC first.",
    {
      netFilter: z.array(z.string()).optional().describe("Restrict to these nets (e.g. [\"BAT+\",\"V12_OUT\"])."),
      refFilter: z.array(z.string()).optional().describe("Restrict to segments touching these component refs (e.g. [\"U1\",\"U3\"])."),
      includeSegments: z.boolean().optional().describe("Emit the per-segment list (default true). Set false for summary-only."),
      includeCrossings: z.boolean().optional().describe("Detect segment-segment crossings on different nets (default true, O(N²))."),
      maxSegments: z.number().optional().describe("Cap on the segment list size (default 1000)."),
      topNCrossingsPerRef: z.number().optional().describe("Cap on the per-ref crossing-contributors list (default 10)."),
      drcViolationsPath: z.string().optional().describe("Explicit path to a DRC violations JSON. Defaults to the project-dir cache from a prior get_drc_violations/run_drc call."),
      boardPath: z.string().optional().describe("Path to the .kicad_pcb. Defaults to currently-loaded board."),
      refresh: z.boolean().optional().describe("Run DRC before reading the cache (default false). Use to guarantee fresh data after mutating commands like route_trace, add_via, or delete_trace."),
    },
    async (args: any) => {
      const result = await callKicadScript("get_ratsnest", args);
      return {
        content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
      };
    },
  );

  // analyze_congestion
  server.tool(
    "analyze_congestion",
    "Routing-congestion analyzer. Divides the board into a grid (default 5 mm cells) and reports per-cell pad density × ratsnest density so you can see WHERE the current placement is blocking routing. Read-only. Ratsnest data comes from the DRC `unconnected_items` list — run `get_drc_violations` or `run_drc` first (the tool will auto-find the default cache JSON in the project dir). Returns the top-N most congested cells (each with its member components — those are the candidates to move) plus per-net difficulty (max cell score along each unrouted net's ratsnest, so you can prioritise nets most likely to need re-placement to route at all). Each hotspot includes `pad_count_by_layer` so you can see whether the pressure is one-sided. Pass `layer` (e.g. \"F.Cu\") to filter pad density to that copper layer so the score reflects routing pressure on the side you actually intend to route on — PTH pads still count toward every layer. Pairs with `place_near`: identify hotspots, then `place_near` the listed components to less-saturated targets.",
    {
      cellSizeMm: z.number().optional().describe("Grid cell size in mm (default 5.0). Smaller = finer resolution but noisier; 4-5 mm matches typical IC + decoupling-cap clusters."),
      topN: z.number().optional().describe("Number of hotspot cells to return (default 15)."),
      netDifficultyTopN: z.number().optional().describe("Cap on the per-net difficulty list (default 20)."),
      layer: z.string().optional().describe("Copper layer name (e.g. \"F.Cu\", \"B.Cu\", \"In1.Cu\"). When set, pad density is filtered to that layer so the score reflects pressure on that side. PTH pads count toward every copper layer. Omit for the legacy any-layer score."),
      drcViolationsPath: z.string().optional().describe("Explicit path to the DRC violations JSON. Defaults to the cache file in the project dir created by a prior get_drc_violations/run_drc call."),
      boardPath: z.string().optional().describe("Path to the .kicad_pcb. Defaults to the currently-loaded board."),
    },
    async (args: any) => {
      const result = await callKicadScript("analyze_congestion", args);
      return {
        content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
      };
    },
  );

  // check_pcb_integrity
  server.tool(
    "check_pcb_integrity",
    "Silent-corruption detector for PCB layouts. Runs subchecks that catch bugs DRC will NOT catch: (1) pad_rotation — for footprints with multiple instances of the same lib_id, flags any whose per-pad orientation drifts from siblings (uniform drift = warning; mixed deltas = error, the dangerous \"shape corruption\" case from 2026-05-14 apply_positions). (2) footprint_overlap — flags a footprint whose centre falls inside another's bbox on the same copper layer. (3) stacked_pads — flags ≥2 differently-numbered pads on different nets at the same XY (catches the USB-C 16P symptom directly; same-net or same-number stacking is treated as intentional).",
    {
      checks: z
        .array(z.enum(["pad_rotation", "footprint_overlap", "stacked_pads"]))
        .optional()
        .describe("Subset of subchecks to run. Default: all three."),
      boardPath: z.string().optional().describe("Path to the .kicad_pcb. Defaults to the currently-loaded board."),
    },
    async (args: any) => {
      const result = await callKicadScript("check_pcb_integrity", args);
      return {
        content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
      };
    },
  );

  // analyze_routable_regions — Phase 1 of TOPOLOGY_TOOLS_PLAN.md
  server.tool(
    "analyze_routable_regions",
    "Partition a copper layer into the free-space regions a trace of `widthMm` could occupy without violating clearance against foreign copper. Read-only analysis (rasterized at `resolutionMm`, default 0.05 mm = 1/4 of a 0.2 mm signal trace). Returns the list of connected components — each with area, bbox, and the pads bordering it — plus pads with no escape lane at this width (the QFN-internal-escape diagnostic). The free space formula is `board_area \\ (foreign_copper(layer) ⊕ disk(W/2 + C))`. Two pads on the same net are routable iff they fall in the same component. When `net` is set, that net's own copper is excluded from obstacles so its pads anchor into the regions they actually border; when `net` is omitted, ALL copper is obstacle (the layer's free corridors regardless of any specific net). Pairs with `check_pad_routability` for pair-wise queries and the existing `analyze_congestion` for placement diagnosis. Pass `convergenceCheck=true` to also run at half the grid step and confirm the component count is grid-independent.",
    {
      layer: z.string().describe("Copper layer (e.g. \"F.Cu\", \"B.Cu\", \"In1.Cu\")."),
      widthMm: z.number().describe("Trace width in mm — the smallest trace whose centerline could live in the returned regions."),
      clearanceMm: z.number().optional().describe("Clearance to foreign copper in mm. Default = net's netclass clearance, then board's design default, then 0."),
      net: z.string().optional().describe("Per-net analysis: exclude this net's own copper from obstacles. When omitted, every net's copper is treated as obstacle (layer-wide free corridors)."),
      resolutionMm: z.number().optional().describe("Grid step in mm (default 0.05 — 1/4 of a typical 0.2 mm signal trace). For a 100×100 mm board the default = 2000×2000 cells. Use a finer grid for the final go/no-go answer."),
      convergenceCheck: z.boolean().optional().describe("Also run at half the grid step and compare component counts; agrees=true means grid-independent at this resolution. Default false."),
      boardPath: z.string().optional().describe("Path to the .kicad_pcb. Defaults to the currently-loaded board."),
    },
    async (args: any) => {
      const result = await callKicadScript("analyze_routable_regions", args);
      return {
        content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
      };
    },
  );

  // check_pad_routability — Phase 1 of TOPOLOGY_TOOLS_PLAN.md
  server.tool(
    "check_pad_routability",
    "Are these two pads geometrically reachable on `layer` at `widthMm`? Returns `{reachable, bottleneckWidthMm, pathXy[]}`. Read-only. Reasons when unreachable: `pads_on_different_nets`, `different_components` (the corridor between them is too tight for this trace width — the placement or netclass is the cause, not the router), `from_pad_no_escape` / `to_pad_no_escape` (the pad's own escape lane can't fit the trace, e.g. QFN-internal-escape). `bottleneckWidthMm` is the maximum trace width that still fits at the tightest point along the BFS path — use it to back off the netclass width or pick a wider-tolerant route. `pathXy` is the path through the free-space raster (visualisation only, NOT a routing suggestion — the freerouter still picks the exact geometry). Pairs with `analyze_routable_regions` for full-layer surveys. Pass `convergenceCheck=true` to also run at half the grid step and confirm the answer is stable.",
    {
      fromRef: z.string().describe("Source component reference (e.g. \"U3\")."),
      fromPad: z.string().describe("Source pad number (e.g. \"4\")."),
      toRef: z.string().describe("Destination component reference."),
      toPad: z.string().describe("Destination pad number."),
      layer: z.string().describe("Copper layer to check on (e.g. \"F.Cu\")."),
      widthMm: z.number().describe("Trace width in mm."),
      clearanceMm: z.number().optional().describe("Clearance to foreign copper in mm. Default = netclass clearance, then design default, then 0."),
      resolutionMm: z.number().optional().describe("Grid step in mm (default 0.05). Finer = more accurate bottleneck width but slower."),
      convergenceCheck: z.boolean().optional().describe("Also run at half the grid step and compare. Default false."),
      boardPath: z.string().optional().describe("Path to the .kicad_pcb. Defaults to the currently-loaded board."),
    },
    async (args: any) => {
      const result = await callKicadScript("check_pad_routability", args);
      return {
        content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
      };
    },
  );

  // max_width_between — Phase 2 of TOPOLOGY_TOOLS_PLAN.md
  server.tool(
    "max_width_between",
    "Widest trace that still leaves these two pads in the same free-space component on `layer`. Binary search on the same rasterized obstacle field used by `check_pad_routability` (single EDT, ~10 component-label probes — typically faster than 10 freerouting attempts). Read-only. Use this to pick a netclass width before laying out a high-current rail, or to ask \"how much margin do I have on this signal at the current placement?\". Returns `{reachable, maxWidthMm, iterations, upperBoundMm}` plus the existing `pads_on_different_nets` / `different_components` reason codes when the pads can't connect at any width. `searchToleranceMm` defaults to 2 × `resolutionMm` (sub-grid answers are noise).",
    {
      fromRef: z.string().describe("Source component reference."),
      fromPad: z.string().describe("Source pad number."),
      toRef: z.string().describe("Destination component reference."),
      toPad: z.string().describe("Destination pad number."),
      layer: z.string().describe("Copper layer (e.g. \"F.Cu\")."),
      clearanceMm: z.number().optional().describe("Clearance to foreign copper. Default = netclass clearance, then design default, then 0."),
      resolutionMm: z.number().optional().describe("Grid step in mm (default 0.05). Finer = more accurate max-width but slower."),
      widthToleranceMm: z.number().optional().describe("Binary-search tolerance in mm. Default = 2 × resolutionMm."),
      upperBoundMm: z.number().optional().describe("Skip the precomputation step by passing an explicit search ceiling in mm. Default = twice the max distance-to-obstacle on the layer."),
      boardPath: z.string().optional().describe("Path to the .kicad_pcb. Defaults to the currently-loaded board."),
    },
    async (args: any) => {
      const result = await callKicadScript("max_width_between", args);
      return {
        content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
      };
    },
  );

  // max_parallel_traces — Phase 2 of TOPOLOGY_TOOLS_PLAN.md
  server.tool(
    "max_parallel_traces",
    "How many parallel traces of `widthMm` (each with its netclass clearance) fit through the bottleneck along the path between these two pads? Computes `floor(bottleneckWidthMm / (widthMm + 2 × clearanceMm))`. Read-only. Use this to size a bus (\"can I run all 5 SPI signals through this gap?\") before committing to a placement. When the pads aren't reachable at the queried width, forwards the same reason codes as `check_pad_routability` (and reports `maxParallelTraces=0`).",
    {
      fromRef: z.string().describe("Source component reference."),
      fromPad: z.string().describe("Source pad number."),
      toRef: z.string().describe("Destination component reference."),
      toPad: z.string().describe("Destination pad number."),
      layer: z.string().describe("Copper layer."),
      widthMm: z.number().describe("Per-trace width in mm."),
      clearanceMm: z.number().optional().describe("Override clearance in mm. Default = netclass / design default."),
      resolutionMm: z.number().optional().describe("Grid step in mm (default 0.05)."),
      boardPath: z.string().optional().describe("Path to the .kicad_pcb. Defaults to the currently-loaded board."),
    },
    async (args: any) => {
      const result = await callKicadScript("max_parallel_traces", args);
      return {
        content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
      };
    },
  );

  // routability_heatmap — Phase 2 of TOPOLOGY_TOOLS_PLAN.md
  server.tool(
    "routability_heatmap",
    "Render a geodesic-distance heatmap from a source pad: where can a trace of `widthMm` reach on `layer`, and how far? Bright = far reachable; black = unreachable (obstacle or different component). Writes a PNG to `/tmp/claude-1000` (or `outputPath`) and returns `{vizPath, reachableAreaMm2, maxReachMm}`. Read-only. Use this to *see* the shape of the reachable region when `check_pad_routability` reports `different_components` — the gap in the heatmap is exactly the corridor that's too tight. Falls back to a numeric summary if matplotlib isn't importable.",
    {
      fromRef: z.string().describe("Source component reference."),
      fromPad: z.string().describe("Source pad number."),
      layer: z.string().describe("Copper layer."),
      widthMm: z.number().describe("Trace width in mm."),
      clearanceMm: z.number().optional().describe("Override clearance. Default = netclass / design default."),
      resolutionMm: z.number().optional().describe("Grid step in mm (default 0.05). Heatmap pixel size = resolutionMm."),
      outputPath: z.string().optional().describe("Explicit PNG path. Default: `/tmp/claude-1000/routability_<REF>_<PAD>_<layer>.png`."),
      boardPath: z.string().optional().describe("Path to the .kicad_pcb. Defaults to the currently-loaded board."),
    },
    async (args: any) => {
      const result = await callKicadScript("routability_heatmap", args);
      return {
        content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
      };
    },
  );

  // check_pad_routability_multilayer — Phase 3 of TOPOLOGY_TOOLS_PLAN.md
  server.tool(
    "check_pad_routability_multilayer",
    "Are two pads reachable across all copper layers, hopping between them through vias where the via fits both layers' free space? Phase 3 of the topology tools — extends `check_pad_routability` from one layer to N layers via a per-via meta-graph. Read-only. Through-vias only (no blind/buried). Returns `{reachable, sameLayerReachable, viaCandidates, layerComponents}`. `viaCandidates` is one representative (x, y, layerA, layerB) per component bridge in the meta-graph — *where you could drop a via*, NOT a routing prescription. Reasons when unreachable: `pads_on_different_nets`, `from_pad_no_escape` / `to_pad_no_escape` (no escape on any considered layer), `unreachable_any_layer` (even with via bridges). Pairs with `routability_report` for the board-wide all-pairs view.",
    {
      fromRef: z.string().describe("Source component reference."),
      fromPad: z.string().describe("Source pad number."),
      toRef: z.string().describe("Destination component reference."),
      toPad: z.string().describe("Destination pad number."),
      widthMm: z.number().describe("Trace width in mm."),
      viaDiameterMm: z.number().describe("Via diameter in mm (the copper pad of the via; used as `2 × (via radius + clearance)` in the via-candidacy mask)."),
      clearanceMm: z.number().optional().describe("Trace clearance to foreign copper in mm. Default = netclass / design default."),
      viaClearanceMm: z.number().optional().describe("Separate via clearance in mm. Default = clearanceMm (same setting for trace and via)."),
      layers: z.array(z.string()).optional().describe("Subset of copper layer names to consider (e.g. [\"F.Cu\", \"In1.Cu\"]). Default = all enabled copper layers — restrict to ask 'can I do this on F.Cu + In1.Cu only?'."),
      resolutionMm: z.number().optional().describe("Grid step in mm (default 0.05). The per-layer EDT is built once and shared across the meta-graph."),
      boardPath: z.string().optional().describe("Path to the .kicad_pcb. Defaults to the currently-loaded board."),
    },
    async (args: any) => {
      const result = await callKicadScript("check_pad_routability_multilayer", args);
      return {
        content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
      };
    },
  );

  // routability_report — Phase 3 of TOPOLOGY_TOOLS_PLAN.md
  server.tool(
    "routability_report",
    "All-ratlines multi-layer feasibility matrix at the queried `widthMm` + `viaDiameterMm`. Phase 3 of the topology tools — flags ratlines that are geometrically impossible across every copper layer (and via combination) BEFORE the autoroute attempt. Read-only. Per net: builds a spanning star from the first pad to the rest (capped by `maxPairsPerNet` — GND-style large nets won't blow up runtime), then asks `check_pad_routability_multilayer` for each pair. Returns `{summary, ratlines, limitations}`. Each ratline entry: `{net, fromRef/Pad, toRef/Pad, reachable, sameLayerReachable, reason}`. Summary counts: `totalRatlines / reachable / unreachable / sameLayer / viaRequired`. CAVEAT: uses the all-copper-is-obstacle approximation for speed; a ratline flagged unreachable here MIGHT still route per-net — confirm with `check_pad_routability_multilayer` per-net.",
    {
      widthMm: z.number().describe("Trace width in mm (all ratlines queried at this width)."),
      viaDiameterMm: z.number().describe("Via diameter in mm."),
      clearanceMm: z.number().optional().describe("Trace clearance to foreign copper. Default = design default."),
      viaClearanceMm: z.number().optional().describe("Via clearance. Default = clearanceMm."),
      layers: z.array(z.string()).optional().describe("Copper layers to consider (default = all enabled)."),
      resolutionMm: z.number().optional().describe("Grid step in mm (default 0.05)."),
      nets: z.array(z.string()).optional().describe("Restrict to these net names (default = all nets with ≥2 pads)."),
      maxPairsPerNet: z.number().optional().describe("Cap pairs evaluated per net (default 64). Protects against GND-style fan-out where Σ(n choose 2) explodes."),
      boardPath: z.string().optional().describe("Path to the .kicad_pcb. Defaults to the currently-loaded board."),
    },
    async (args: any) => {
      const result = await callKicadScript("routability_report", args);
      return {
        content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
      };
    },
  );

  // place_near
  server.tool(
    "place_near",
    "Snap PCB footprints to within `maxDist` mm of a target pad or footprint, respecting bbox collisions with other components AND foreign-net tracks (a pad landing on or within `clearanceMargin` mm of a track on a different net is rejected — prevents the shorting-items + clearance-violation class of move). Pairs naturally with `decoupling_audit`: audit to find too-far caps, then `place_near(refs=[\"C9\"], target=\"U1.10\", maxDist=3)`. The placement strategy searches a 1mm-resolution polar grid out to `maxDist` and picks the closest non-rejected spot.",
    {
      refs: z.array(z.string()).describe("Component references to move (e.g. [\"C9\", \"C10\"])."),
      target: z.string().describe("Anchor as `REF` (snap near footprint body) or `REF.PIN` (snap near a specific pad). Example: \"U1.10\"."),
      maxDist: z.number().optional().describe("Max distance in mm from target. Default 5.0 mm."),
      skipIfWithin: z.boolean().optional().describe("If true (default), components already within `maxDist` are left alone."),
      clearanceMargin: z.number().optional().describe("Safety margin (mm) added around each pad bbox before checking against foreign-net tracks. Default 0.15 mm — clears the default POWER_2A 0.13 mm DRC rule with a 20 µm cushion."),
      boardPath: z.string().optional().describe("Path to the .kicad_pcb. Defaults to the currently-loaded board."),
      savePath: z.string().optional().describe("Path to save the modified board. Defaults to `boardPath` (if provided)."),
    },
    async (args: any) => {
      const result = await callKicadScript("place_near", args);
      return {
        content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
      };
    },
  );
}
