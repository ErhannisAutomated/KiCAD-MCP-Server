/**
 * Routing tools for KiCAD MCP server
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";

export function registerRoutingTools(server: McpServer, callKicadScript: Function) {
  // Add net tool
  server.tool(
    "add_net",
    "Create a new net on the PCB",
    {
      name: z.string().describe("Net name"),
      netClass: z.string().optional().describe("Net class name"),
    },
    async (args: { name: string; netClass?: string }) => {
      const result = await callKicadScript("add_net", args);
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(result, null, 2),
          },
        ],
      };
    },
  );

  // Route trace tool
  server.tool(
    "route_trace",
    "Route a trace segment between two XY points on a fixed layer. By default refuses (checkObstacles) when the proposed trace would cross or come within clearance of foreign-net copper. The check is width-aware: it inflates the trace centerline by half the trace width plus the net's netclass clearance (overridable with the `clearance` param) so an edge-clipping case where a fat trace exits an IC pin grazes the neighbouring pad is caught. Pass checkObstacles=false to override (e.g. restoring a known-good trace by coordinates). WARNING: Does NOT handle layer changes — if start and end are on different copper layers, use route_pad_to_pad instead, which automatically inserts a via.",
    {
      start: z
        .object({
          x: z.number(),
          y: z.number(),
          unit: z.string().optional(),
        })
        .describe("Start position"),
      end: z
        .object({
          x: z.number(),
          y: z.number(),
          unit: z.string().optional(),
        })
        .describe("End position"),
      layer: z.string().describe("PCB layer"),
      width: z.number().describe("Trace width in mm"),
      net: z.string().describe("Net name"),
      checkObstacles: z
        .boolean()
        .optional()
        .describe(
          "Refuse the route if the swept trace (width + clearance) would touch foreign-net tracks, vias or pads (default: true). Set false to force the trace anyway — useful when restoring a previously-deleted segment by coordinates, or routing through a region you've verified is clear via other means.",
        ),
      clearance: z
        .number()
        .optional()
        .describe(
          "Minimum gap in mm between the trace edge and any foreign-net copper (used only when checkObstacles is true). Defaults to the net's netclass clearance, falling back to the board default.",
        ),
    },
    async (args: any) => {
      const result = await callKicadScript("route_trace", args);
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(result, null, 2),
          },
        ],
      };
    },
  );

  // Find via lane tool — proposes via-jumper around F.Cu blockers
  server.tool(
    "find_via_lane",
    "Propose a via-jumper route when the direct same-layer path is blocked by foreign-net copper. Tries (in order): direct on fromLayer → straight via-jumper on viaLayer → single perpendicular-offset waypoint → 2D grid waypoint search → axis-aligned L-shape (blind sweep) → obstacle-bbox-aware L-shape (v3: routes past the union bbox of via-layer blockers, so it escapes long obstacles a blind ±waypointSearchMax sweep can't). Returns the first strategy that clears. Default is preview (proposed segments + via points); pass apply=true to commit. Source/target can be either explicit XY or {ref, pad} pad lookup. Use this when route_pad_to_pad returns 'Route blocked' and the obstacles can't be cleared with a same-layer waypoint.",
    {
      from: z
        .object({
          x: z.number().optional(),
          y: z.number().optional(),
          unit: z.string().optional(),
          ref: z.string().optional(),
          pad: z.union([z.string(), z.number()]).optional(),
        })
        .describe("Source: either {x, y, unit} XY or {ref, pad} pad lookup."),
      to: z
        .object({
          x: z.number().optional(),
          y: z.number().optional(),
          unit: z.string().optional(),
          ref: z.string().optional(),
          pad: z.union([z.string(), z.number()]).optional(),
        })
        .describe("Target: either {x, y, unit} XY or {ref, pad} pad lookup."),
      net: z.string().describe("Net name (required)."),
      fromLayer: z.string().optional().describe("Primary layer (default F.Cu)."),
      viaLayer: z.string().optional().describe("Layer to jump through (default B.Cu)."),
      width: z.number().optional().describe("Trace width mm (default 0.2)."),
      viaDiameter: z.number().optional().describe("Via outer diameter mm (default 0.6)."),
      viaDrill: z.number().optional().describe("Via drill mm (default 0.3)."),
      safetyMargin: z
        .number()
        .optional()
        .describe(
          "Pull-back from first obstacle on fromLayer when placing vias, mm (default 0.5).",
        ),
      minimumStubLength: z
        .number()
        .optional()
        .describe(
          "Refuse when the safe via insertion point is closer than this to source or target pad, mm. Set ≥1 mm when from/to are component pads to keep vias out of the pad footprint. Default 0 (off).",
        ),
      minClearance: z
        .number()
        .optional()
        .describe(
          "Minimum gap (mm) required between the proposed via's edge and any foreign-net copper on any layer. Validated after via placement — refuses with `via_clearance_violation` if the via diameter would overlap (or come within this distance of) an adjacent pad/via/track. Default 0.15.",
        ),
      waypointSearchMax: z
        .number()
        .optional()
        .describe("Max perpendicular offset for waypoint search, mm (default 10)."),
      apply: z
        .boolean()
        .optional()
        .describe("Commit the proposed route (default false = preview)."),
    },
    async (args: any) => {
      const result = await callKicadScript("find_via_lane", args);
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(result, null, 2),
          },
        ],
      };
    },
  );

  // Check route segment tool (pre-flight, no commit)
  server.tool(
    "check_route_segment",
    "Pre-flight check: would a straight segment from start to end on the given layer (for the given net) cross or come within clearance of foreign-net copper? Returns {clear, obstacles[]} without committing the route. Same width- and clearance-aware obstacle detection as route_trace's default checkObstacles — pass `width` (defaults to the board's current track width) and optionally `clearance` (defaults to netclass) to size the swept-trace test. Useful for plan-first workflows where you want to enumerate candidate paths before committing one. Cheaper than route_trace + run_drc + delete_trace round-trips when iterating.",
    {
      start: z
        .object({
          x: z.number(),
          y: z.number(),
          unit: z.string().optional(),
        })
        .describe("Start position"),
      end: z
        .object({
          x: z.number(),
          y: z.number(),
          unit: z.string().optional(),
        })
        .describe("End position"),
      layer: z.string().describe("PCB layer (e.g., F.Cu, B.Cu)"),
      net: z
        .string()
        .describe(
          "Net name you intend to route — same-net copper isn't counted as an obstacle.",
        ),
      width: z
        .number()
        .optional()
        .describe(
          "Planned trace width in mm. Inflates the obstacle check by half this width so edge-clipping is caught. Defaults to the board's current track width.",
        ),
      clearance: z
        .number()
        .optional()
        .describe(
          "Minimum gap in mm between the trace edge and foreign-net copper. Defaults to the net's netclass clearance, falling back to the board default.",
        ),
    },
    async (args: any) => {
      const result = await callKicadScript("check_route_segment", args);
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(result, null, 2),
          },
        ],
      };
    },
  );

  // Bridge same-net pins tool
  server.tool(
    "bridge_same_net_pins",
    "Create a small filled zone covering two same-net pads, replacing a thin sub-min-width trace that would violate the POWER netclass track-width rule. Standard practice for parallel power pins on IC datasheets (BAT+ pad doublings on TSSOP/QFN devices). Both pads must already be on the same net. The zone uses solid (full bond, no thermal-relief spokes) connection by default — appropriate for current-carrying bridges. Default preview; pass apply=true to commit.",
    {
      padA: z.object({
        ref: z.string(),
        pad: z.union([z.string(), z.number()]),
      }).describe("First pad: {ref, pad}, e.g. {ref:\"U4\", pad:\"2\"}."),
      padB: z.object({
        ref: z.string(),
        pad: z.union([z.string(), z.number()]),
      }).describe("Second pad: {ref, pad}."),
      layer: z.string().optional().describe("Copper layer (default 'F.Cu')."),
      marginMm: z.number().optional().describe("Margin (mm) around the pad-union bbox (default 0.1)."),
      connection: z.enum(["solid", "thermal"]).optional().describe("Pad connection mode (default 'solid')."),
      apply: z.boolean().optional().describe("Commit the zone (default false = preview outline only)."),
    },
    async (args: any) => {
      const result = await callKicadScript("bridge_same_net_pins", args);
      return {
        content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
      };
    },
  );

  // Pair via tool (high-current via doubling)
  server.tool(
    "pair_via",
    "Propose (and optionally apply) a parallel partner via next to every existing via on the given net(s). Doubles current capacity and ~halves inductance for high-current power vias — needed for POWER_4A nets because freerouting's DSN class-rule via spec can only pick a single via diameter, not a pair. Default: nets matching the POWER_4A netclass. For each parent via, tries the four ±x/±y offset positions at `offset` mm; the first that clears `minClearance` from foreign-net copper (and isn't within offset/2 of another same-net via) wins. Default preview; pass apply=true to commit.",
    {
      nets: z.array(z.string()).optional().describe("Explicit net list (e.g. [\"BAT+\",\"BAT-\"]). Overrides netClass when set."),
      netClass: z.string().optional().describe("Netclass name to filter by (default \"POWER_4A\"). Ignored if nets is set."),
      offset: z.number().optional().describe("Distance (mm) from the parent via center to the partner via (default 1.0)."),
      minClearance: z.number().optional().describe("Minimum gap (mm) between the partner via edge and foreign-net copper (default 0.2)."),
      viaDiameter: z.number().optional().describe("Partner via diameter (mm). Defaults to the parent via's diameter."),
      viaDrill: z.number().optional().describe("Partner via drill (mm). Defaults to the parent via's drill."),
      apply: z.boolean().optional().describe("Commit the proposed partner vias (default false = preview)."),
      maxPairs: z.number().optional().describe("Safety cap on the number of partners to propose (default 200)."),
    },
    async (args: any) => {
      const result = await callKicadScript("pair_via", args);
      return {
        content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
      };
    },
  );

  // Stitch pour vias tool
  server.tool(
    "stitch_pour_vias",
    "Propose (and optionally apply) a regular grid of stitching vias on a copper pour net, e.g. to tie F.Cu GND to B.Cu GND or to improve return-current paths around high-speed traces. Walks a grid over the union bbox of all zones on the net; each candidate must (a) sit inside at least one filled zone polygon on the net, (b) pass `minClearance` from any foreign-net copper on any layer (same check find_via_lane uses), and (c) not duplicate an existing same-net via (within gridPitch*0.7). Default is preview — pass apply=true to commit. Through-via (F.Cu ↔ B.Cu).",
    {
      net: z
        .string()
        .describe("Net to stitch (e.g. 'GND'). Must have at least one zone."),
      gridPitch: z
        .number()
        .describe("Spacing between candidate stitching vias, in mm."),
      viaDiameter: z
        .number()
        .optional()
        .describe("Via outer diameter in mm (default 0.6)."),
      viaDrill: z
        .number()
        .optional()
        .describe("Via drill diameter in mm (default 0.3)."),
      minClearance: z
        .number()
        .optional()
        .describe(
          "Minimum gap in mm between the via edge and foreign-net copper on any layer (default 0.2). Candidates that fail clearance are skipped and counted in skippedClearance.",
        ),
      apply: z
        .boolean()
        .optional()
        .describe(
          "Commit the proposed stitching vias (default false = preview). When true, the board is mutated and auto-saved.",
        ),
      maxVias: z
        .number()
        .optional()
        .describe(
          "Safety cap on the number of proposed vias (default 200). The walk stops early once this is reached.",
        ),
    },
    async (args: any) => {
      const result = await callKicadScript("stitch_pour_vias", args);
      return {
        content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
      };
    },
  );

  // Add via tool
  server.tool(
    "add_via",
    "Add a via to the PCB",
    {
      position: z
        .object({
          x: z.number(),
          y: z.number(),
          unit: z.string().optional(),
        })
        .describe("Via position"),
      net: z.string().describe("Net name"),
      viaType: z.string().optional().describe("Via type (through, blind, buried)"),
    },
    async (args: any) => {
      const result = await callKicadScript("add_via", args);
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(result, null, 2),
          },
        ],
      };
    },
  );

  // Add copper pour tool
  server.tool(
    "add_copper_pour",
    "Add a copper pour (ground/power plane) to the PCB",
    {
      layer: z.string().describe("PCB layer"),
      net: z.string().describe("Net name"),
      clearance: z.number().optional().describe("Clearance in mm"),
      outline: z
        .array(z.object({ x: z.number(), y: z.number() }))
        .optional()
        .describe(
          "Array of {x, y} points defining the pour boundary. If omitted, the board outline is used.",
        ),
    },
    async (args: any) => {
      const result = await callKicadScript("add_copper_pour", args);
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(result, null, 2),
          },
        ],
      };
    },
  );

  // Dedupe traces tool
  server.tool(
    "dedupe_traces",
    "Remove exact-duplicate tracks (and optionally vias) left over from autoroute SES re-imports or scripted re-routes. Two tracks match if they share (layer, width, net) and their endpoints coincide in either order; vias match by (position, drill, width, net). Default is dry-run — pass apply=true to actually delete the extras. Use net filter to limit scope.",
    {
      apply: z
        .boolean()
        .optional()
        .describe("Actually delete duplicates (default false = dry-run preview)."),
      net: z
        .string()
        .optional()
        .describe("Optional net filter — only dedupe tracks on this net."),
      includeVias: z
        .boolean()
        .optional()
        .describe("Also dedupe vias (default true)."),
    },
    async (args: any) => {
      const result = await callKicadScript("dedupe_traces", args);
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(result, null, 2),
          },
        ],
      };
    },
  );

  // Delete trace tool
  server.tool(
    "delete_trace",
    "Delete traces from the PCB. Can delete by UUID, position, or bulk-delete all traces on a net.",
    {
      traceUuid: z.string().optional().describe("UUID of a specific trace to delete"),
      position: z
        .object({
          x: z.number(),
          y: z.number(),
          unit: z.enum(["mm", "inch"]).optional(),
        })
        .optional()
        .describe("Delete trace nearest to this position"),
      net: z
        .string()
        .optional()
        .describe(
          'Delete all traces on this net (bulk delete). Pass "*" to delete every track on the board.',
        ),
      layer: z.string().optional().describe("Filter by layer when using net-based deletion"),
      includeVias: z
        .boolean()
        .optional()
        .describe('Include vias in net-based deletion (use with net="*" to strip the whole board)'),
    },
    async (args: any) => {
      const result = await callKicadScript("delete_trace", args);
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(result, null, 2),
          },
        ],
      };
    },
  );

  // Query traces tool
  server.tool(
    "query_traces",
    "Query traces on the board with optional filters by net, layer, or bounding box.",
    {
      net: z.string().optional().describe("Filter by net name"),
      layer: z.string().optional().describe("Filter by layer name"),
      boundingBox: z
        .object({
          x1: z.number(),
          y1: z.number(),
          x2: z.number(),
          y2: z.number(),
          unit: z.enum(["mm", "inch"]).optional(),
        })
        .optional()
        .describe("Filter by bounding box region"),
      unit: z.enum(["mm", "inch"]).optional().describe("Unit for coordinates"),
      includeVias: z
        .boolean()
        .optional()
        .describe(
          "Also return vias (with their UUIDs) in a separate 'vias' array. Needed to get via UUIDs for reliable delete_trace by UUID.",
        ),
    },
    async (args: any) => {
      const result = await callKicadScript("query_traces", args);
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(result, null, 2),
          },
        ],
      };
    },
  );

  // Audit plane cuts tool
  server.tool(
    "audit_plane_cuts",
    "Report signal traces routed on inner copper layers that double as power/GND planes — long traces there break image-current return paths above F.Cu signals (slot-antenna effect). Returns per-net total cut length, per-layer breakdown, and the longest single-trace offenders sorted for ripup + retry on an outer layer.",
    {
      layers: z
        .array(z.string())
        .optional()
        .describe(
          'Inner layers to audit (default ["In1.Cu", "In2.Cu"]). Override only if your stackup labels the plane layers differently.',
        ),
      minLength: z
        .number()
        .optional()
        .describe(
          "Minimum trace length (default 1.0 mm) to include — filters out unavoidable short via-fanout stubs.",
        ),
      unit: z.enum(["mm", "inch"]).optional().describe("Length unit (default mm)"),
    },
    async (args: any) => {
      const result = await callKicadScript("audit_plane_cuts", args);
      return {
        content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
      };
    },
  );

  // Get nets list tool
  server.tool(
    "get_nets_list",
    "Get a list of all nets in the PCB with optional statistics.",
    {
      includeStats: z
        .boolean()
        .optional()
        .describe("Include statistics (track count, total length, etc.)"),
      unit: z.enum(["mm", "inch"]).optional().describe("Unit for length measurements"),
    },
    async (args: any) => {
      const result = await callKicadScript("get_nets_list", args);
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(result, null, 2),
          },
        ],
      };
    },
  );

  // Modify trace tool
  server.tool(
    "modify_trace",
    "Modify an existing trace (change width, layer, or net).",
    {
      traceUuid: z.string().describe("UUID of the trace to modify"),
      width: z.number().optional().describe("New trace width in mm"),
      layer: z.string().optional().describe("New layer name"),
      net: z.string().optional().describe("New net name"),
    },
    async (args: any) => {
      const result = await callKicadScript("modify_trace", args);
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(result, null, 2),
          },
        ],
      };
    },
  );

  // Create netclass tool
  server.tool(
    "create_netclass",
    "Create a new net class with custom design rules.",
    {
      name: z.string().describe("Net class name"),
      traceWidth: z.number().optional().describe("Default trace width in mm"),
      clearance: z.number().optional().describe("Clearance in mm"),
      viaDiameter: z.number().optional().describe("Via diameter in mm"),
      viaDrill: z.number().optional().describe("Via drill size in mm"),
    },
    async (args: any) => {
      const result = await callKicadScript("create_netclass", args);
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(result, null, 2),
          },
        ],
      };
    },
  );

  // Route differential pair tool
  server.tool(
    "route_differential_pair",
    "Route a differential pair between two sets of points.",
    {
      positivePad: z
        .object({
          reference: z.string(),
          pad: z.string(),
        })
        .describe("Positive pad (component and pad number)"),
      negativePad: z
        .object({
          reference: z.string(),
          pad: z.string(),
        })
        .describe("Negative pad (component and pad number)"),
      layer: z.string().describe("PCB layer"),
      width: z.number().describe("Trace width in mm"),
      gap: z.number().describe("Gap between traces in mm"),
      positiveNet: z.string().describe("Positive net name"),
      negativeNet: z.string().describe("Negative net name"),
    },
    async (args: any) => {
      const result = await callKicadScript("route_differential_pair", args);
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(result, null, 2),
          },
        ],
      };
    },
  );

  // Refill zones tool
  server.tool(
    "refill_zones",
    "Refill all copper zones on the board. WARNING: SWIG path has known segfault risk (see KNOWN_ISSUES.md). Prefer using IPC backend (KiCAD open) or triggering zone fill via KiCAD UI instead.",
    {},
    async (args: any) => {
      const result = await callKicadScript("refill_zones", args);
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(result, null, 2),
          },
        ],
      };
    },
  );

  // Route pad to pad tool
  server.tool(
    "route_pad_to_pad",
    "PREFERRED tool for pad-to-pad routing. Looks up pad positions automatically, detects the net from the pad, and — critically — if the two pads are on different copper layers (e.g. J1 on F.Cu and J2 on B.Cu) automatically inserts a via at the midpoint so the connection is complete. Always use this instead of route_trace when routing between named component pads. By default refuses (checkObstacles) if the swept trace (width + netclass clearance) would touch foreign-net copper; route around obstacles with route_trace waypoints in that case. Optional pin-escape (#178): supply escapeFromWidth/escapeFromLength (and/or the symmetric escapeTo* pair) to emit a narrow stub from the pad before widening into the trunk — needed when the trunk width can't physically fit through a tight IC pin pitch. Currently same-layer routes only.",
    {
      fromRef: z.string().describe("Reference of the source component (e.g. 'U2')"),
      fromPad: z
        .union([z.string(), z.number()])
        .describe("Pad number on the source component (e.g. '6' or 6)"),
      toRef: z.string().describe("Reference of the target component (e.g. 'U1')"),
      toPad: z
        .union([z.string(), z.number()])
        .describe("Pad number on the target component (e.g. '15' or 15)"),
      layer: z.string().optional().describe("PCB layer (default: F.Cu)"),
      width: z.number().optional().describe("Trace width in mm (default: board default)"),
      net: z.string().optional().describe("Net name override (default: auto-detected from pad)"),
      checkObstacles: z
        .boolean()
        .optional()
        .describe(
          "Refuse the route if the swept trace (width + clearance) would touch foreign-net tracks, vias or pads (default: true). Set false to force the trace anyway.",
        ),
      clearance: z
        .number()
        .optional()
        .describe(
          "Minimum gap in mm between the trace edge and any foreign-net copper (used only when checkObstacles is true). Defaults to the net's netclass clearance, falling back to the board default.",
        ),
      escapeFromWidth: z
        .number()
        .optional()
        .describe(
          "Width in mm for a narrow pin-escape stub at the source pad. Use this when the trunk `width` is too wide to fit out of the pad's pin pitch (e.g. 0.3 mm escape from a 0.65 mm-pitch HTSSOP, widening to a 1.5 mm POWER_4A trunk). Must be paired with escapeFromLength.",
        ),
      escapeFromLength: z
        .number()
        .optional()
        .describe(
          "Length in mm of the source-side pin-escape stub. The stub points perpendicular to the pad's pin row (computed from footprint center → pad center). Must be paired with escapeFromWidth.",
        ),
      escapeToWidth: z
        .number()
        .optional()
        .describe(
          "Width in mm for a symmetric pin-escape stub at the destination pad. Same semantics as escapeFromWidth. Must be paired with escapeToLength.",
        ),
      escapeToLength: z
        .number()
        .optional()
        .describe(
          "Length in mm of the destination-side pin-escape stub. Must be paired with escapeToWidth.",
        ),
    },
    async (args: any) => {
      const result = await callKicadScript("route_pad_to_pad", args);
      return {
        content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
      };
    },
  );

  // Copy routing pattern tool
  server.tool(
    "copy_routing_pattern",
    "Copy routing pattern (traces and vias) from a group of source components to a matching group of target components. The offset is calculated automatically from the position difference between the first source and first target component. Useful for replicating routing between identical circuit blocks.",
    {
      sourceRefs: z
        .array(z.string())
        .describe("References of the source components (e.g. ['U1', 'R1', 'C1'])"),
      targetRefs: z
        .array(z.string())
        .describe(
          "References of the target components in same order as sourceRefs (e.g. ['U2', 'R2', 'C2'])",
        ),
      includeVias: z.boolean().optional().describe("Also copy vias (default: true)"),
      traceWidth: z
        .number()
        .optional()
        .describe("Override trace width in mm (default: keep original width)"),
    },
    async (args: any) => {
      const result = await callKicadScript("copy_routing_pattern", args);
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(result, null, 2),
          },
        ],
      };
    },
  );
}
