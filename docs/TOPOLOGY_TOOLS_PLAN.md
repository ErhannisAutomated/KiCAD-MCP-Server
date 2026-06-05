# Routing Topology Tools — Design Plan

Status: **Phases 1+2 implemented** on `develop` (2026-05-31). Phases 3–4
still planned. Implementation lives in `python/commands/topology.py`;
TS bindings in `src/tools/placement.ts`. See `## Effort` for the phase
table. Phase 2 picks the *max-bottleneck* path for
`max_width_between` / `max_parallel_traces`, not the shortest-path
bottleneck (the engineering question is "where could I route the bus
to get the widest gap?", not "what's the worst squeeze on the
straightest route?").

## Problem

Today's routing-helper tools (`route_pad_to_pad`, `find_via_lane`,
`autoroute`, `scrub_region`) all *try to route* and report success/failure.
When they fail, the failure message lists *obstacles* — a useful
local-geometry artifact, but not an answer to the actual question:

> Is this pad pair geometrically reachable at this trace width? If not, where
> is the bottleneck, and how wide can a trace through this corridor be?

Without that answer, every failure forces a reverse-engineering exercise from
the obstacle list back to the underlying topology. The placement and netclass
decisions that *cause* the failure are invisible to the tool.

The topology tools described here expose the underlying **trace-width
configuration space** directly, so questions like "can I get there at all,"
"how wide a trace fits," and "how many parallel traces can I bus through this
gap" become first-class queries rather than reverse engineering.

Compared to a Freerouting fork: Freerouting is a *router* (it solves
routing); these tools are *analysis* (they tell you whether and where you
*can* route). Different output, different machinery. The router fork would
inherit a JVM dependency we already use sparingly; the analysis tools are
small enough to live in-process in the MCP server.

## Core primitive: the trace-width configuration space

For a fixed `(layer, trace_width, clearance)`, the **free space** is the
board area where the center of a trace of that width could exist without
violating clearance against foreign-net copper:

```
free_space(layer, W, C) = board_area
                       \ (foreign_obstacles(layer) ⊕ disk(W/2 + C))
```

where `⊕` is Minkowski sum (obstacle dilation by `W/2 + C`). The **connected
components** of `free_space` are the regions of the board reachable by a
moving trace of that width. Two pads are routable on this layer iff they fall
into the same connected component.

Everything else falls out of this primitive.

## Derived tools

| Tool | What it returns | Question it answers |
|---|---|---|
| `analyze_routable_regions(layer, width)` | List of connected components (polygons + area + bbox + pad refs inside each) | "How is this layer partitioned by my trace width?" |
| `rooms_and_corridors(layer, room_width, corridor_width)` | Two-level region list: regions enumerated at `room_width`, plus the corridors of the smaller width connecting them, each annotated with bottleneck width | "Where can my trunk fit, and what corridors bridge to the cap nests?" |
| `check_pad_routability(fromPad, toPad, layer, width)` | `{reachable, bottleneck_width, path_xy[]}` | "Can I connect these two pads at this width, and if so what's the tightest squeeze along the way?" |
| `max_width_between(fromPad, toPad, layer)` | Single width value — the **maximum** trace width that still leaves the pads connected | Binary search on `check_pad_routability` |
| `count_distinct_paths(fromPad, toPad, layer, width)` | Number of topologically distinct paths (homotopy classes) at this width | "Are there multiple independent routes, or one bottleneck I can't avoid?" |
| `max_parallel_traces(fromPad, toPad, layer, width)` | Integer count = `bottleneck_width // (width + 2*clearance)` | "Can I bus 5 parallel signals through this corridor?" |
| `routability_heatmap(fromPad, layer, width)` | Image — distance-to-pad on the eroded free space; unreachable = max-value / different color | Visual "where can I get from here" |
| `routability_report(layer, width)` | All-pairs feasibility matrix (per netclass) | "Which ratlines are geometrically impossible at their declared netclass widths before I even start routing?" |

The non-obvious one is `count_distinct_paths`: the number of topologically
distinct paths between two points in the free space equals 1 + (number of
"obstacle islands" your path can choose to go on either side of) along the
shortest route. The free space has genus G iff there are 2^(G+1) distinct
homotopy classes between two reachable points (in principle; constrained by
which classes actually fit). This tells you whether a failed route has
genuine alternatives or is geometrically forced through one bottleneck.

## The natural data structure: medial axis

Beyond the rasterized free space, the **medial axis** of the obstacle
boundary (the locus of points equidistant from at least two nearest
obstacles) is the natural graph for almost all topology queries:

- Nodes: medial-axis branch points + pad-anchor points.
- Edges: segments of the medial axis between branch points.
- Edge weight: local clearance (twice the distance from the medial point to
  the nearest obstacle).

Then:
- `check_pad_routability` ≡ graph reachability + min-edge-weight along path.
- `max_width_between` ≡ widest-bottleneck path (max-min) in the graph.
- `count_distinct_paths` ≡ count of homotopy-distinct paths in the graph.
- `find_bottleneck` ≡ the min-weight edge on the shortest path.

`scikit-image`'s `medial_axis` gives the skeleton on a raster in one call;
turning it into a graph with edge weights is straightforward (each skeleton
pixel has a distance-to-boundary value already).

## Implementation sketch

**Rasterized first.** Convert the layer's foreign-net copper into a binary
image (1 = obstacle, 0 = free) at grid size `g`. Erode by ceil((W/2 + C)/g)
to get the trace's free space. Run `scipy.ndimage.label` for connected
components. `scipy.ndimage.distance_transform_edt` for distance-to-obstacle
(needed for medial axis and bottleneck queries).

**Grid size.** Default to **1/4 of the smallest feature** = 0.05 mm for
typical 0.2 mm signal traces (matches user's instinct from this discussion).
For a 100 × 100 mm board that's 2000 × 2000 = 4 M cells/layer — well within
fast numpy territory. A `resolution_mm` param exposes this. Provide a
`convergence_check` mode that runs at `g` and `g/2` and reports whether
results agree — defensible without forcing the user to guess.

**Layer interactions.** Each query runs per-layer by default. Via support is
phase 2: a via at point `p` connecting layers `L1` and `L2` means *if a trace
can reach `p` on `L1` and a trace can reach `p` on `L2`, the two regions are
connected through the via*. For the multi-layer routability question we
compute per-layer free spaces, then build a meta-graph where each via is an
edge bridging its two layer-regions.

**Pour (zone) handling.** A pour on the trace's net is *not* an obstacle —
the pour will hand off connectivity at fill time. A pour on a foreign net IS
an obstacle (with whatever clearance the zone declares). Pre-process: union
the same-net zones into the trace's "free addition" region, union
foreign-net zones into the obstacle set.

**Polygon-exact mode (phase 3).** Same algorithms but on `shapely` polygon
sets: `unary_union(foreign_polygons).buffer(W/2 + C)` for the obstacle
dilation; `Polygon.difference` for the free space. Exact (no grid
quantization), but slower on dense boards. Worth it for the final
"is-it-really-routable" question after the raster mode says yes.

## How existing tools change

- **`route_pad_to_pad` / `find_via_lane`:** failure messages become
  *informative*. "Route blocked by foreign copper near (X, Y)" → "Pads are
  in different W-mm connected components on F.Cu; the closest pair of
  components is separated by a Z-mm gap at (X, Y). Try via-jumper or widen
  the corridor."
- **`scrub_region`:** before recommending the autoroute pipeline, run
  `routability_report` on `nowOpenNets` and warn if any nets would be
  unroutable post-scrub.
- **`relax_placement`:** score candidate placements by routability — sum of
  `check_pad_routability` over each ratsnest edge at its netclass width. A
  placement that breaks routability for high-pin-count IC corridors gets a
  hard penalty.
- **New first-class workflow tool:** `pre_route_audit` — run before any
  autoroute, returns per-ratline feasibility + the "this connection is
  geometrically impossible at netclass W; widen the corridor or change the
  netclass" diagnostics. Saves long freerouting runs that were doomed from
  the start.

## Open questions

- **Pad escape.** A trace can't physically exit from a pad if the pad's
  neighbouring pads (same component) leave no gap for the trace width. This
  is the QFN-internal-escape problem we hit live on `BQ_TS U3.4` / `BQ_VREF
  U3.2`. The free-space analysis catches this naturally (the pad isn't in
  *any* free-space component because its escape lane is too tight), but the
  failure mode "pad has no escape" deserves its own reason code in the
  report.
- **Multi-net interaction.** The per-net analysis is one-at-a-time; in
  reality routing one net consumes space and changes the topology for the
  next. A meaningful `pre_route_audit` over all ratlines should re-evaluate
  in some priority order (high-current trunks first, narrow signals last) to
  catch "if I route A first, can I still route B?" correctly. Phase 4.
- **Vias-as-bridges with clearance.** A via not only bridges layers but also
  consumes clearance on both layers. The meta-graph in the layer-bridging
  case needs to respect that placing a "phantom via" at a candidate bridge
  point would itself need clearance from existing copper. This is what
  `find_via_lane` already does pointwise; we'd need to do it as a feasibility
  test over candidate via positions along the medial axis.
- **Pour fill-time uncertainty.** Pours are filled by KiCad's filler; our
  analysis assumes the same fill behavior. Edge cases (thermal reliefs,
  zones-in-zones) might disagree. Mitigation: run our queries on
  pre-filled-zone state and warn if any answer changes after a `refill_zones`.

## Effort

- **Phase 1 — core primitive + region enum:** rasterize + erode + label,
  expose `analyze_routable_regions` and `check_pad_routability`.
  **Shipped 2026-05-31** on `develop`.
- **Phase 2 — max-width binary search + parallel-trace count +
  reachability heatmap:** built on the same data. **Shipped 2026-05-31**
  on `develop`. (Medial-axis graph deferred; the EDT-threshold +
  connected-components approach turned out to be enough for the Phase 2
  questions and avoids a scikit-image dependency. If we need
  `count_distinct_paths` in Phase 3, medial axis comes back.)
- **Phase 3 — via bridges + multi-layer routability + `routability_report`:**
  meta-graph construction. ~1 session.
- **Phase 4 — polygon-exact mode + integration with existing tools' failure
  messages + `pre_route_audit`:** plumbing. ~1-2 sessions.

Total: ~5-7 sessions for a useful first cut with rich integration.

### How to resume Phase 3

Entry point for the next session: implement multi-layer routability via
a **per-via meta-graph**. The Phase 1 EDT already exists per-layer;
Phase 3 needs to *bridge* per-layer free spaces wherever a via could
land.

Concrete first move:
1. In `python/commands/topology.py`, lift the per-layer
   `_compute_obstacle_state` call into a loop over all copper layers,
   so the per-layer EDT cache is built once for a multi-layer query.
2. Build a via-candidacy predicate: a pixel `(x, y)` on layer pair
   `(L1, L2)` admits a via iff a disk of `viaSize/2 + clearance` at
   that point fits the free-space of **both** layers. That's two
   EDT-threshold checks AND-ed together — same primitive as the rest of
   the module, just intersected across layers.
3. The meta-graph: per-layer connected components (already from
   `scipy_label`), plus one undirected edge between two components
   whenever the via-candidacy mask intersects both. Union-find on the
   resulting graph answers "are these pads connected across all layers
   the trace could traverse?".
4. Public surface: `check_pad_routability_multilayer(fromPad, toPad,
   widthMm, viaSize, ...)` and `routability_report(widthMmDefault,
   viaSize)` for the all-ratlines feasibility matrix.

Pour handling is the trickiest sub-task — pre-zone-fill state is what
the analysis sees, but the user routes against the post-fill state.
Mitigation discussed in "Pour fill-time uncertainty" above (run queries
both ways and warn on divergence).

Phase 2 surfaced one bug worth remembering: the free-space mask must
exclude obstacles (`(dist_px >= radius_px) & (~mask)`) to handle the
W=C=0 degenerate case correctly. Phase 3's via-candidacy mask has the
same structure — apply the same correction up-front.

## Comparison vs. freerouting fork

| | Freerouting fork | In-MCP topology tools |
|---|---|---|
| Output | Solved routing | Analysis of what's possible |
| Dependencies | JVM | numpy / scipy / shapely |
| Round-trip cost | DSN export + JVM start + SES import | None (in-process) |
| Per-query latency | Seconds (DSN export + freerouting init) | Milliseconds (cached free-space) |
| Reusable across MCP tools | Awkward (subprocess) | Native |
| Maintainer burden | Forked Java + our patches | Pure Python |

The case for a fork would be inheriting freerouting's well-tested geometry
engine — but the analysis tools here are simple enough that re-implementing
in numpy/shapely is less work than maintaining a fork.

## Out of scope

- Routing the board (use `autoroute` / `route_pad_to_pad` / `find_via_lane`
  for that).
- Predicting freerouting's exact path choices.
- DRC replacement — KiCad's DRC is authoritative for the final check.
