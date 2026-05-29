# scrub_region — Region-Scoped Copper Cleanup (Design Plan)

Status: **planned / in progress** (task #251). Branch: `develop`.

## Problem

After a re-placement (`relax_placement`) or an incremental re-route
(`autoroute(nets=…)`, #242/#248), a moved component leaves behind stale
copper that no longer fits its new position:

- **Private routing** on the component's own nets (`BQ_*`, `CHG_OUT`) is
  handled today — `autoroute(nets=…)` strips those target nets by name
  before re-routing (`_remove_net_routing`, net-name-only, board-wide).
- **Shared-rail copper** (USB_VBUS, GND, BAT+) is *not* handled. Net-name
  stripping is all-or-nothing, so you can't strip "the charger's slice of
  USB_VBUS" without nuking the entire rail. The leftover slice wraps around
  the moved IC, blocks pins, and shorts to fresh tracks. Plus little
  orphaned stubs survive from prior layouts.

`_remove_net_routing` has no geometric scope; that's the gap. **scrub_region
adds the geometric scope**: clean *all* copper in a spatial region, but only
on nets that actually involve the targeted components.

This is the tool form of the recurring "hand-cleaned these stubs twice now"
problem — see memory `feedback_tooling_over_manual`.

## Core idea

Given a list of **target components**, compute a **convex hull** of their
geometry (per copper layer), then delete copper that is either:

1. on a **target-only net** (every pad on the net belongs to a target) —
   delete anywhere, any layer (this is the private routing); or
2. on a **shared net** (touches a target *and* a non-target) — delete only
   the portion inside the hull.

Then a **recursive dead-end prune** sweeps anything left dangling.

Bias: **prefer over-deletion to under-deletion.** Stray copper severely
borks a board and is painful to debug; missing copper is just an open net we
re-route afterward (the validated #242/#248 incremental cadence). The
recursive prune and the opt-in `intersect_hull` mode both lean this way
deliberately.

## The hull and the margin

- **Hull source per target footprint:** courtyard polygon if present, else
  pad-bbox union, else footprint bbox. (Courtyard captures body extent that
  tiny pads miss.) The hull is the convex hull of all those corner points.
- **Per side / per layer.** Copper on different layers is independent — never
  delete a B.Cu track because of an F.Cu hull. Build:
  - `F.Cu` hull from target pads/courtyard on the front,
  - `B.Cu` hull from the back,
  - inner layers (if in scope) use the **combined** hull (all target
    geometry projected to XY — the hull is fundamentally an XY region; layers
    are just Z).
- **Vias** have one XY but span layers, so they're matched **once** against
  the combined hull, not in the per-layer loop.
- **Margin without polygon offset.** "Inside the hull inflated by *m*" ≡
  `distance(point, hull) ≤ m`, where the distance is 0 for points inside and
  otherwise the distance to the nearest edge. So the margin is just a `≤ m`
  slack on a point-to-polygon distance — **no Minkowski offset, no
  rounded-corner construction.** `margin_mm` default small (≈1 mm). All
  inside/coincidence tests use a small epsilon (KiCad coords are integer nm).

## Per-element decision rule (all layers)

For each copper item, find its net, then:

| Net relationship to targets        | Track                                                    | Via                                  | Zone                                                              |
| ---------------------------------- | -------------------------------------------------------- | ------------------------------------ | ----------------------------------------------------------------- |
| touches **no** target pad          | never touched                                            | never touched                        | never touched                                                     |
| **target-only** (no non-target pad)| delete (any layer/geometry) — `target-only-net`          | delete — `target-only-net`           | delete — `target-only-net`                                        |
| **shared** (target + non-target)   | delete if endpoint in layer hull — `endpoint-in-hull`; (opt-in) if segment crosses hull — `intersects-hull` | delete if XY in combined hull — `in-hull` | delete if within hull — `in-hull`; if it extends outside AND connects to non-targets → **flag + leave** |

Then: **recursive dead-end prune** over involved nets — iteratively delete
any involved-net track with a *free* endpoint (coincident with no pad / other
track endpoint / via, within epsilon) and any via left with ≤1 connection,
to fixpoint (`dead-end-prune`). Scoped to involved nets by default (not
whole-board) to stay conservative. This is what catches the leaky inner-layer
remnant: once the F/B and target-net portions are cut, a winding inner trace
dangles and gets swept.

## Inner layers

`layers` defaults to `["F.Cu","B.Cu"]` — front/back only, which automatically
protects the In1/In2 power pours (they're never in scope) without
special-casing zones. Pass inner layers explicitly to extend.

The three mechanisms compose to cover inner layers *without* needing a
component hull there (there are no components on inner layers):
target-only-net deletion (geometry-free) sweeps private inner routing; the
projected combined hull scopes shared-net inner traces; the recursive prune
mops up remnants. A via-based inner hull was considered and rejected — it's
leaky (a winding trace between two vias escapes).

Caveat to remember: with the default `["F.Cu","B.Cu"]`, inner-layer copper is
never cleaned — correct *while* inner layers are pour-only planes; revisit if
signals are ever routed on In1/In2.

## Toggles & params

- `targets: [refs]` — the components defining the region. **Required.**
- `layers: [str]` = `["F.Cu","B.Cu"]`.
- `margin_mm: float` ≈ 1.0.
- `affect_tracks / affect_zones / affect_vias: bool` = all true.
- `intersect_hull: bool` = false (opt-in segment-crosses-hull for tracks).
- `prune_dead_ends: bool` = true (recursive sweep over involved nets).
- `dry_run: bool` = **true** (must explicitly pass false to delete).
- `viz: bool` = true (write debug PNG).

## Output (dry-run is authoritative)

The **text payload is the authoritative record**; the viz is the human aid.
Every would-delete item carries a **reason code**
(`target-only-net` | `endpoint-in-hull` | `intersects-hull` | `in-hull` |
`dead-end-prune`) so the kill-list can be audited for mis-scope before
anything is deleted.

```
{
  "dryRun": bool,
  "targets": [refs], "layers": [...], "marginMm": ...,
  "wouldDelete": {
    "tracks": [{uuid, net, layer, start, end, reason}],
    "vias":   [{uuid, net, pos, reason}],
    "zones":  [{uuid, net, layer, reason}],
  },
  "interlopers": [{ref, insideHullLayers, affectedNets}],   # non-targets in hull + their nets
  "flagged":     [{type, uuid, net, reason}],               # spared-but-noteworthy (mixed-net zones, etc.)
  "nowOpenNets": [...],   # nets left with open connections — feed straight to autoroute(nets=…)
  "counts": {...},
  "vizPath": "/tmp/claude-1000/scrub_region_preview.png"
}
```

`nowOpenNets` closes the loop into the re-route. Debug viz renders: target
bbox rects, the hull, the hull+margin ring, and matched/unmatched copper,
color-coded — reusing the `pcb_autoplacer_viz` matplotlib overlay pattern.

## Pipeline

```
relax_placement
  → scrub_region(dry_run=true)        # inspect kill-list + viz
  → snapshot_project / commit board
  → scrub_region(dry_run=false)       # delete
  → autoroute(nets=<nowOpenNets>)     # #242/#248 incremental re-route
  → refill_zones + stitch_pour_vias   # repair GND/pours
  → run_drc
```

GND over-deletion is the cheapest case: mostly pour, repaired by
`refill_zones` + `stitch_pour_vias`, not freerouting.

## Implementation / integration points

- **Engine:** new `python/commands/scrub_region.py`, class
  `ScrubRegionCommands` (mirrors `freerouting`/`congestion`/`integrity`
  modules). Deletions via `BOARD.RemoveNative(item)` for tracks/vias **and**
  zones (the `Remove()` SWIG global-state corruption lesson, commit 5fc25b3);
  verify `RemoveNative` is valid for `ZONE` before relying on it.
- **Handler:** construct `self.scrub_region_commands` in `kicad_interface.py`,
  add `"scrub_region"` to `command_routes`, refresh `.board` in
  `_update_command_handlers`. **Exclude from the `_auto_save_board` set** —
  save explicitly inside the command only when `dry_run=false` (so dry-run
  never touches disk).
- **Schemas:** `python/schemas/tool_schemas.py` + a TS tool
  (`src/tools/routing.ts` or a new file) + `src/tools/registry.ts`. Build with
  `node_modules/typescript/bin/tsc`. Add `scrub_region` to
  `longRunningCommands` only if needed (it shouldn't be slow).
- **Tests:** `tests/test_scrub_region.py` — hull + point-in-hull-with-margin
  geometry; net classification; per-element reason codes; dead-end-prune
  fixpoint; dry-run does not mutate; per-side scoping protects other-layer
  copper. FakeBoard/FakePcbnew where pcbnew isn't available.
- **Docs:** this file + `docs/INDEX.md`; cross-ref the pipeline into
  `FREEROUTING_GUIDE.md`; note in `CHANGELOG.md`.
- MCP server caches modules at startup — **restart `/mcp` to pick up** before
  the live charger run.

## Open / deferred

- **`dryRun` is not a standardized MCP convention.** Destructive tools
  (`delete_trace`, `delete_component`, the `delete_schematic_*` family) act
  immediately; preview-like behavior only exists incidentally in read-only
  audits. scrub_region is the first instance of an explicit `dryRun` gate;
  worth promoting to a cross-cutting convention later.
- Default `layers` (front/back vs all): left at `["F.Cu","B.Cu"]` for now;
  the mechanism supports inner layers, so the default can change later if we
  hit issues.
- **hull vs. union-of-bboxes:** chose convex hull deliberately — the
  misbehaving copper runs through the *gaps between* the target parts, and
  the hull fills those gaps while a union-of-bboxes would leave them open.
