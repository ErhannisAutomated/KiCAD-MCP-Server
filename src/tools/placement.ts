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

  // relax_placement
  server.tool(
    "relax_placement",
    "Force-directed PCB placement relaxation. Pulls connected components together (springs along ratsnest segments) while pushing overlapping ones apart, keeping anchored components fixed (J*/SW*/BAT* and through-hole-dominant footprints by default; override with lockedRefs). Iterates max_iters times with damping. Use dryRun=true to try parameters before committing. Reports before/after total ratsnest length and crossing count so you know whether it helped — if delta is positive (length grew), revert and tune. Source: DRC unconnected_items cache from prior get_drc_violations/run_drc call.",
    {
      lockedRefs: z.array(z.string()).optional().describe("Refs to keep fixed (overrides the J/SW/BAT default). Pass empty array to anchor nothing."),
      maxIters: z.number().optional().describe("Number of iterations (default 200)."),
      kAttract: z.number().optional().describe("Spring constant per mm of error (default 0.02). Higher = faster convergence but oscillation risk."),
      kRepulseStep: z.number().optional().describe("Repulsion strength: fraction of bbox-overlap pushed per iter (default 1.0 = resolve fully in one step)."),
      minGapMm: z.number().optional().describe("Min padding added around each bbox before computing overlap. Default 0.3 mm."),
      stepMm: z.number().optional().describe("Max movement per component per iter (force magnitude cap). Default 1.0 mm."),
      damping: z.number().optional().describe("Step-size multiplier per iter (default 0.99 — gentle anneal)."),
      keepInBbox: z.object({
        left: z.number(), top: z.number(), right: z.number(), bottom: z.number(),
      }).optional().describe("Keep-in rectangle in mm. Default: board Edge.Cuts bbox tightened by 1 mm."),
      dryRun: z.boolean().optional().describe("Compute new positions without applying. Default false."),
      drcViolationsPath: z.string().optional().describe("Path to DRC JSON. Defaults to project-dir cache."),
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
    "Read-only inspection of the ratsnest (the list of pad-pair connections still needing routes — what KiCAD draws as thin lines). Returns per-segment endpoints (with parsed component refs and pad numbers), net, length, plus pairwise geometric crossing detection on different nets. Use this to evaluate \"is this layout routable?\" and to debug placement decisions. Pairs with analyze_congestion (which shows dense regions): ratsnest shows which lines have to thread through those regions. Source: DRC unconnected_items cache from a prior get_drc_violations/run_drc call.",
    {
      netFilter: z.array(z.string()).optional().describe("Restrict to these nets (e.g. [\"BAT+\",\"V12_OUT\"])."),
      refFilter: z.array(z.string()).optional().describe("Restrict to segments touching these component refs (e.g. [\"U1\",\"U3\"])."),
      includeSegments: z.boolean().optional().describe("Emit the per-segment list (default true). Set false for summary-only."),
      includeCrossings: z.boolean().optional().describe("Detect segment-segment crossings on different nets (default true, O(N²))."),
      maxSegments: z.number().optional().describe("Cap on the segment list size (default 1000)."),
      topNCrossingsPerRef: z.number().optional().describe("Cap on the per-ref crossing-contributors list (default 10)."),
      drcViolationsPath: z.string().optional().describe("Explicit path to a DRC violations JSON. Defaults to the project-dir cache from a prior get_drc_violations/run_drc call."),
      boardPath: z.string().optional().describe("Path to the .kicad_pcb. Defaults to currently-loaded board."),
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
    "Routing-congestion analyzer. Divides the board into a grid (default 5 mm cells) and reports per-cell pad density × ratsnest density so you can see WHERE the current placement is blocking routing. Read-only. Ratsnest data comes from the DRC `unconnected_items` list — run `get_drc_violations` or `run_drc` first (the tool will auto-find the default cache JSON in the project dir). Returns the top-N most congested cells (each with its member components — those are the candidates to move) plus per-net difficulty (max cell score along each unrouted net's ratsnest, so you can prioritise nets most likely to need re-placement to route at all). Pairs with `place_near`: identify hotspots, then `place_near` the listed components to less-saturated targets.",
    {
      cellSizeMm: z.number().optional().describe("Grid cell size in mm (default 5.0). Smaller = finer resolution but noisier; 4-5 mm matches typical IC + decoupling-cap clusters."),
      topN: z.number().optional().describe("Number of hotspot cells to return (default 15)."),
      netDifficultyTopN: z.number().optional().describe("Cap on the per-net difficulty list (default 20)."),
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
