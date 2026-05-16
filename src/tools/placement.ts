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
    "Snap PCB footprints to within `maxDist` mm of a target pad or footprint, respecting bbox collisions with other components. Pairs naturally with `decoupling_audit`: audit to find too-far caps, then `place_near(refs=[\"C9\"], target=\"U1.10\", maxDist=3)`. The placement strategy searches a 1mm-resolution polar grid out to `maxDist` and picks the closest non-overlapping spot.",
    {
      refs: z.array(z.string()).describe("Component references to move (e.g. [\"C9\", \"C10\"])."),
      target: z.string().describe("Anchor as `REF` (snap near footprint body) or `REF.PIN` (snap near a specific pad). Example: \"U1.10\"."),
      maxDist: z.number().optional().describe("Max distance in mm from target. Default 5.0 mm."),
      skipIfWithin: z.boolean().optional().describe("If true (default), components already within `maxDist` are left alone."),
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
