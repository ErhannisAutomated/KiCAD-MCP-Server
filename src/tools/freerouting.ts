/**
 * Freerouting autoroute tools for KiCAD MCP server
 *
 * Provides autorouting via Freerouting (Specctra DSN/SES workflow).
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";

export function registerFreeroutingTools(server: McpServer, callKicadScript: Function) {
  // Full autoroute: export DSN -> run Freerouting -> import SES
  server.tool(
    "autoroute",
    "Run Freerouting autorouter on the current PCB. Exports to Specctra DSN, runs Freerouting CLI, and imports the routed SES result. Requires Java 11+ and freerouting.jar (see check_freerouting). On 4-layer boards, layers are reordered in the DSN so freerouting prefers outer layers and uses GND last — see layerOrder. Auto-dedupes after import (pcbnew.ImportSpecctraSES appends rather than replaces, so re-running autoroute would otherwise leave exact-duplicate tracks on every routed net — #227). Pass `nets` to route INCREMENTALLY — only those nets are (re)routed and copied onto the board; all other existing copper is left untouched (#242). Without `nets`, the whole board is routed replace-style.",
    {
      boardPath: z.string().optional().describe("Path to .kicad_pcb file (default: current board)"),
      nets: z
        .array(z.string())
        .optional()
        .describe(
          "Incremental mode (#242): route ONLY these nets and copy their tracks/vias onto the board, leaving all existing copper untouched. Each named net is cleared and cleanly re-routed; other nets are never modified (no replace-like wipe). Use this to route a freshly-placed sub-circuit without disturbing hand-routed work. Result reports routedNets / unroutedNets / perNetCounts / removedExistingCount. Omit for a whole-board route. Unknown net names are reported in unknownNets and skipped.",
        ),
      freeroutingJar: z
        .string()
        .optional()
        .describe(
          "Path to freerouting.jar (default: ~/.kicad-mcp/freerouting.jar or FREEROUTING_JAR env)",
        ),
      maxPasses: z.number().optional().describe("Maximum routing passes (default: 20)"),
      timeout: z.number().optional().describe("Timeout in seconds (default: 300)"),
      layerOrder: z
        .array(z.string())
        .optional()
        .describe(
          'Layer-priority order to write into the DSN; freerouting iterates layers in this order and prefers earlier ones. Must be a permutation of the board\'s copper layers. Default on 4-layer boards: ["F.Cu","B.Cu","In2.Cu","In1.Cu"] (outer first, GND-on-In1.Cu last) — set explicitly only to override.',
        ),
      autoDedupe: z
        .boolean()
        .optional()
        .describe(
          "After importing SES, remove exact-duplicate tracks/vias left over by ImportSpecctraSES's append-not-replace behaviour (default: true). Pass false to keep the raw post-import state for debugging. Reported as `autoDedupeRemovedCount` in the result.",
        ),
      forceImport: z
        .boolean()
        .optional()
        .describe(
          "Bypass the SES import safety guard (default: false). The guard aborts the import when the routed session has 0 wires, or far fewer than the board already has, because ImportSpecctraSES is replace-like and would wipe/decimate existing routing (#241 — a prior empty-SES import wiped 599 traces). Only set true when you genuinely intend to replace the board's routing with the session.",
        ),
    },
    async (args: any) => {
      const result = await callKicadScript("autoroute", args);
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

  // Export DSN only
  server.tool(
    "export_dsn",
    "Export the current PCB to Specctra DSN format. Useful for manual Freerouting workflow or external autorouters. Honours the same layerOrder reordering as autoroute.",
    {
      boardPath: z.string().optional().describe("Path to .kicad_pcb file (default: current board)"),
      outputPath: z
        .string()
        .optional()
        .describe("Output DSN file path (default: same dir as board)"),
      layerOrder: z
        .array(z.string())
        .optional()
        .describe(
          'Layer-priority order to write into the DSN. Must be a permutation of the board\'s copper layers. Default on 4-layer boards: ["F.Cu","B.Cu","In2.Cu","In1.Cu"] (outer first, GND-on-In1.Cu last).',
        ),
    },
    async (args: any) => {
      const result = await callKicadScript("export_dsn", args);
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

  // Import SES
  server.tool(
    "import_ses",
    "Import a Specctra SES (session) file into the current PCB. Use after running Freerouting externally. Auto-dedupes after import (pcbnew.ImportSpecctraSES appends rather than replaces; calling import_ses twice without dedupe doubles every track — #227).",
    {
      sesPath: z.string().describe("Path to the .ses file to import"),
      boardPath: z.string().optional().describe("Path to .kicad_pcb file (default: current board)"),
      autoDedupe: z
        .boolean()
        .optional()
        .describe(
          "After importing, remove exact-duplicate tracks/vias left over by ImportSpecctraSES's append-not-replace behaviour (default: true). Pass false to keep the raw post-import state for debugging. Reported as `autoDedupeRemovedCount` in the result.",
        ),
      forceImport: z
        .boolean()
        .optional()
        .describe(
          "Bypass the SES import safety guard (default: false). The guard aborts when the session has 0 wires or far fewer than the board already has, since the import is replace-like and would wipe/decimate existing routing (#241). Only set true to intentionally replace the routing.",
        ),
    },
    async (args: any) => {
      const result = await callKicadScript("import_ses", args);
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

  // Check Freerouting dependencies
  server.tool(
    "check_freerouting",
    "Check if Java and Freerouting JAR are available on the system. Run this before autoroute to verify prerequisites.",
    {
      freeroutingJar: z.string().optional().describe("Path to freerouting.jar to check"),
    },
    async (args: any) => {
      const result = await callKicadScript("check_freerouting", args);
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
