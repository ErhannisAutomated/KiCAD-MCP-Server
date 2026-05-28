# Freerouting Integration Guide

**Added in:** v2.2.3 (PR #68, contributor: @jflaflamme)

Freerouting is an open-source autorouter that can automatically route PCB traces. This integration lets you run Freerouting directly from MCP tools without leaving your AI-assisted design workflow.

---

## How It Works

The autorouter uses the Specctra DSN/SES interchange format:

1. Export the current PCB to Specctra DSN format
2. Run Freerouting CLI on the DSN file
3. Import the routed SES result back into the PCB
4. Save the board

The `autoroute` tool performs all four steps in a single call.

---

## Prerequisites

### Freerouting JAR

Download the Freerouting executable JAR:

```bash
mkdir -p ~/.kicad-mcp
curl -L -o ~/.kicad-mcp/freerouting.jar \
  https://github.com/freerouting/freerouting/releases/download/v2.0.1/freerouting-2.0.1-executable.jar
```

The default location is `~/.kicad-mcp/freerouting.jar`. You can override this with:

- The `freeroutingJar` parameter on any tool call
- The `FREEROUTING_JAR` environment variable

### Java Runtime (Option A -- Direct Execution)

Freerouting 2.x requires Java 21 or higher.

```bash
# Ubuntu/Debian
sudo apt install openjdk-21-jre

# Verify
java -version
```

### Docker or Podman (Option B -- No Java Install Needed)

If you do not have Java 21+ installed, the integration automatically falls back to Docker or Podman using the `eclipse-temurin:21-jre` image.

```bash
# Pull the image (one-time)
docker pull eclipse-temurin:21-jre

# Or with Podman
podman pull eclipse-temurin:21-jre
```

### Automatic Runtime Detection

The autorouter checks for runtimes in this order:

1. Local Java 21+ (direct execution, fastest)
2. Docker (container execution)
3. Podman (container execution)

If none are available, an error is returned with installation instructions.

---

## Tools Reference

### `check_freerouting`

Verify that prerequisites are installed before running the autorouter.

**Parameters:**
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `freeroutingJar` | string | No | Path to freerouting.jar to check |

**Returns:** Java availability, version, Docker status, JAR location

**Example:**

```
Check if Freerouting is ready on my system.
```

### `autoroute`

Run the full autorouting workflow (export DSN, route, import SES).

**Parameters:**
| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `boardPath` | string | No | Current board | Path to .kicad_pcb file |
| `freeroutingJar` | string | No | ~/.kicad-mcp/freerouting.jar | Path to freerouting.jar |
| `maxPasses` | number | No | 20 | Maximum routing passes |
| `timeout` | number | No | 300 | Timeout in seconds |
| `nets` | string[] | No | (whole board) | **Incremental mode (#242).** Route ONLY these nets and copy their tracks/vias onto the board; all other existing copper is left untouched. Each named net is cleared and cleanly re-routed. Omit for a whole-board (replace-style) route. See "Incremental routing" below. |
| `autoDedupe` | boolean | No | true | After importing the SES result, run `dedupe_traces(apply=true, includeVias=true)` to drop duplicates. `pcbnew.ImportSpecctraSES` **appends** tracks rather than replacing them, so re-routing the same nets used to silently leave doubled traces — sometimes hundreds of them on iterative re-routes. Set false only when chaining `import_ses` calls and dedupe is intentionally deferred. Response carries `autoDedupeRemovedCount` so callers can spot leakage (#227). Ignored in incremental mode (the named nets are cleared first, so no duplicates arise). |
| `forceImport` | boolean | No | false | Bypass the SES import safety guard. The guard aborts the import when the routed session has 0 wires, or fewer than half the board's current track count, because the import is replace-like and would wipe/decimate existing routing. Only set true when you genuinely intend to replace the routing (e.g. routing a freshly-stripped board where a low count is expected). (#241) In incremental mode only the 0-wire check applies. |

**Example:**

```
Autoroute the current board using Freerouting with a 5-minute timeout.
```

#### Incremental routing (`nets` parameter, #242)

Pass `nets=["NET_A", "NET_B", …]` to route only those nets while leaving
**all other existing copper untouched** — the way to route a freshly-placed
sub-circuit without disturbing hand-routed work.

How it works (and why it's safe):

1. The board is exported to DSN and freerouting runs as usual. Because the
   already-routed nets appear as existing wiring in the DSN, freerouting
   only has to route the open (target) ratsnest.
2. The replace-like `ImportSpecctraSES` is applied to a **scratch copy** of
   the board, never the live one — so the board-wipe failure mode can't
   reach your work.
3. Only the target nets' tracks/vias are then lifted off the scratch board
   and reconstructed on the live board (in native coordinates, re-bound to
   the live nets by name). The named nets are cleared on the live board
   first so they get a clean replacement.

The result reports `routedNets`, `unroutedNets` (asked-for but freerouting
produced no copper — still open), `perNetCounts`, `removedExistingCount`,
`addedTracks`, `addedVias`, and `unknownNets` (names not on the board).

**Caveat.** Freerouting's optimization passes operate on the scratch copy's
*whole* board, so it may nudge an existing net to make room for a target
net. Since only the target nets are copied back, a nudged-but-not-copied
existing net keeps its original position on the live board — which can
produce a clash. Always `run_drc` after an incremental route and resolve any
new violations. Keep `maxPasses` modest to limit optimization churn.

This supersedes the removed `preserveExistingTraces` experiment (#240): that
marked existing wiring `(type fix)`, which made freerouting return an
*empty* SES. The scratch-copy approach sidesteps that entirely.

The **surgical per-net tools** (`route_pad_to_pad`, `route_trace`,
`find_via_lane`) remain available for hand-finishing individual stubborn
nets, but incremental `autoroute` is the preferred automated path — prefer
it over hand-routing.

**Import safety guard (#241).** `pcbnew.ImportSpecctraSES` is replace-like —
the board ends up with whatever the session contains. So autoroute and
`import_ses` refuse to import a session with **0 routes**, or with **fewer
than half** the board's current track count, and leave the board untouched
(reported via `sesWireCount` / `boardTracksBefore`). This prevents the
board-wipe that occurs when freerouting fails to route (e.g. no routable
layer) and returns an empty/degenerate SES. Pass `forceImport=true` only
when a low route count is intended (routing a stripped board, or a
deliberate full clear).

### `export_dsn`

Export the PCB to Specctra DSN format for manual routing workflows.

**Parameters:**
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `boardPath` | string | No | Path to .kicad_pcb file (default: current board) |
| `outputPath` | string | No | Output DSN file path (default: same directory as board) |

### `import_ses`

Import a routed Specctra SES file back into the PCB.

**Parameters:**
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `sesPath` | string | Yes | Path to the .ses file to import |
| `boardPath` | string | No | Path to .kicad_pcb file (default: current board) |
| `autoDedupe` | boolean | No | Default true. Same behaviour as `autoroute.autoDedupe` (#227) — drops the duplicate tracks/vias `ImportSpecctraSES` appends. |
| `forceImport` | boolean | No | Bypass the SES import safety guard (default false). The guard aborts when the session has 0 wires or fewer than half the board's current track count, since the replace-like import would wipe/decimate existing routing (#241). Only set true to intentionally replace the routing. |

---

## Workflows

### Automated (Recommended)

A single tool call handles everything:

```
1. Open the project
2. Check Freerouting dependencies
3. Run autoroute with max 10 passes
4. Run DRC to verify the result
5. Export Gerbers
```

### Manual DSN/SES Workflow

For advanced users or external autorouters:

```
1. Export the board to Specctra DSN format
2. (Run Freerouting GUI or another autorouter externally)
3. Import the routed SES file
```

This is useful when you want to:

- Use the Freerouting GUI for interactive routing
- Use a different autorouter that supports DSN/SES
- Route the board on a different machine

---

## Configuration

### Environment Variable

Set `FREEROUTING_JAR` in your MCP client configuration to avoid specifying the path on every call:

```json
{
  "mcpServers": {
    "kicad": {
      "command": "node",
      "args": ["/path/to/KiCAD-MCP-Server/dist/index.js"],
      "env": {
        "FREEROUTING_JAR": "/path/to/freerouting.jar"
      }
    }
  }
}
```

---

## Troubleshooting

### "Neither Java 21+ nor Docker found"

Install either Java 21+ or Docker/Podman. See the Prerequisites section above.

### "Java found but version < 21"

Freerouting 2.x requires Java 21+. Either:

- Upgrade your Java installation
- Install Docker as a fallback

### Timeout Errors

For complex boards, increase the timeout:

```
Autoroute with timeout 600 and max passes 30.
```

### Routing Quality

If the autorouter does not route all connections:

- Increase `maxPasses` (default: 20)
- Check that your design rules allow the autorouter enough clearance
- Run DRC after autorouting to identify any violations
- Consider routing critical traces manually first, then autorouting the rest

### Docker Permission Errors

If Docker reports permission errors:

```bash
# Add your user to the docker group
sudo usermod -aG docker $USER
# Log out and back in for the change to take effect
```

---

## Source Files

- TypeScript tool definitions: `src/tools/freerouting.ts`
- Python implementation: `python/commands/freerouting.py`
- Tests: `python/tests/test_freerouting.py`
