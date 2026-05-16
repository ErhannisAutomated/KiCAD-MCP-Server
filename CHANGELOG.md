# Changelog

All notable changes to the KiCAD MCP Server project are documented here.

## [Unreleased]

### place_near: reject candidates that would short a foreign-net track (develop, 2026-05-17)

`place_near` only checked candidate positions against other footprint
bboxes. On power_module, placing C6 near U1.8 landed C6's GND pad on
top of an existing REGOUT trace, creating two `shorting_items` and two
`solder_mask_bridge` errors. Bbox-only collision is not enough when
the board is already routed.

Add a per-pad track-collision check: for each candidate offset, every
pad's translated bbox is tested against existing tracks on the pad's
copper layer. Tracks on the pad's own net are skipped (they
electrically belong); tracks on a different net cause rejection. Vias
are not yet checked — they span every layer and over-rejection on
small targets is the immediate risk; revisit if via-induced shorts
appear. When all candidates are rejected, the `skipped` message
includes the most recent rejection reason (bbox vs track, and the net
involved) so the caller can diagnose quickly.

### run_drc: auto-save in-memory board before invoking kicad-cli (develop, 2026-05-17)

`run_drc` ran `kicad-cli pcb drc` against `self.board.GetFileName()`
— the on-disk file — so any in-memory mutations made via
`place_near`, `move_component`, etc. were invisible. The first
post-placement DRC silently returned the pre-placement baseline,
concealing the C6 short until a manual `save_project` was called.

Save `self.board` to its source path immediately before the
kicad-cli invocation. Failures are logged and the run continues so a
read-only filesystem doesn't abort DRC entirely. The same gap likely
affects every other kicad-cli tool (autoroute, export_gerber, …);
they will be fixed as they bite.

### decoupling_audit: ~40× faster on hierarchical schematics (develop, 2026-05-17)

`discover_decoupling_pairs` previously called `get_connections_for_net`
once per labelled net, and every call re-loaded each sheet's sexp tree,
re-built wire adjacency, re-parsed labels, and re-walked symbol
instances. On power_module (4 sheets, 55 nets) that was ~57 s — over
the MCP request timeout, so `decoupling_audit` could not return.

Fix: new bulk helpers in `wire_connectivity.py`:

- **`_process_single_sheet_all_nets`** — does the heavy per-sheet setup
  once, runs one BFS per labelled net against the cached graph, and
  walks symbol pins exactly once to assign membership across all nets
  in a single sweep.
- **`get_all_net_connections`** — bulk equivalent of
  `get_connections_for_net`; returns `{net_name: [{component, pin}]}`
  merged across hierarchical sheets.

`discover_decoupling_pairs` now consumes the bulk map. On the same
power_module fixture: 57 s → 1.3 s (44×), with zero mismatches across
all 55 nets.

Tests: `tests/test_wire_connectivity_bulk.py` (6) — equivalence vs
`get_connections_for_net` on synthetic on-disk fixtures covering label-
at-pin, label-via-wire, multi-net same-symbol, shared rail, empty
sheet, and floating label. All 108 tests across the changed area still
pass.

### check_pcb_integrity: silent-corruption detector (develop, 2026-05-16)

New MCP tool that catches a class of bug DRC will NOT catch — positions
look correct to every automated check (DRC ratsnest uses pad centres,
freerouting uses pad centres) but the SHAPES are wrong. Both the
2026-05-14 apply_positions.py pad-rotation incident and the
C24-inside-L2 placement bug would have been caught.

Three subchecks (selectable via `checks=[...]`):

- **`pad_rotation`** — for footprints with 2+ instances of the same
  lib_id, the per-pad orientation relative to the footprint's own
  orientation must be consistent across instances. **Uniform drift**
  (all pads off by the same delta) → warning (cosmetic; pad layout
  geometrically valid). **Mixed drift** (pads off by different deltas)
  → error (the catastrophic shape-corruption case).

- **`footprint_overlap`** — bbox-overlap detection on the SAME copper
  layer, distinguishing pad-copper overlap (real short risk) from
  courtyard-only overlap (assembly concern). Refined twice on
  2026-05-16 per user feedback: first from centre-inside to
  bbox-overlap, then to also compute pad-extent overlap separately.
  Reports both `bbox_overlap_mm2` (courtyard) and `pad_overlap_mm2`
  (copper) so callers can see which kind. Severity:
    * **error** if pads overlap (real short risk) OR one centre is
      inside the other's bbox (full nesting, the C24-inside-L2 bug).
    * **warning** if only courtyards overlap (parts inside each
      other's manufacturing keep-out; assembly concern, not an
      electrical bug).
  Edge-touch slivers below `min_overlap_mm2` (default 0.01) skipped.
  Same-layer filter prevents front/back false positives.

- **`stacked_pads`** — within a single footprint, ≥2 differently-
  numbered pads on DIFFERENT nets at the same XY. Skip rules: same
  pad number (thermal pad copies), empty pad number (mounting pads),
  same net (USB-C A1/B12 GND pair convention). What's left is the
  real bug.

Implementation in `python/commands/integrity.py`. Tested on
power_module v11c: 0 errors, 5 warnings — 3 courtyard overlaps
(R10/L1, L2/C25, R25/J1 — bboxes touch by 0.15-0.26 mm², pads don't)
and 2 cosmetic pad-rotation drifts (R26/R27, remnants of past
`apply_positions.py` damage, electrically benign for symmetric
resistor pads).

Tests: 19 in `tests/test_pcb_integrity.py` (mocked BOARD/FP/PAD).
1086 total passing.

### get_drc_violations: include unconnected_items, add filters (develop, 2026-05-16)

Two changes that compose: a real bug fix and a long-requested filter surface.

- **`run_drc` now consolidates `unconnected_items` into the violations
  list.** kicad-cli writes `unconnected_items` in a SEPARATE top-level
  array alongside `violations`; the previous handler iterated only
  `violations`, silently dropping every unconnected rat from the saved
  report. Worst-affected case: the v11c power_module reported "98
  violations" but had 5 hidden unconnects (BAT+, BQ_PH, CC1, CHG_OUT,
  USB_VBUS) that were nowhere in the JSON. Now they appear as
  `type: "unconnected_items"`, `severity: "error"`.

- **Per-item structured fields**. Each item under a violation now
  carries `net`, `layer`, `length_mm`, and `ref` extracted from
  kicad-cli's free-text descriptions (e.g. "Track [BAT+] on F.Cu,
  length 0.6500 mm" → `{net: "BAT+", layer: "F.Cu", length_mm: 0.65}`).
  Best-effort; unparseable fields stay null.

- **`get_drc_violations` gained `type` / `net` / `summaryOnly` /
  `useCachedReport` filters.** Examples:
    - `{type: "shorting_items"}` — show only shorts.
    - `{net: "BAT+"}` — show only findings on BAT+.
    - `{summaryOnly: true}` — counts by type+severity, no items.
    - `{useCachedReport: true}` — skip the kicad-cli re-run (~2-5s);
      read the previous violations file.

Backward-compatible: existing `severity` param still works; the
response keeps `violations` and `violationsFile` fields. New top-level
fields: `total`, `summary`, `filters`.

Tests: 14 new in `tests/test_drc_violations.py` (parser + filter
matrix). 1067 total passing.

### Placement-constraint v1: decoupling_audit + place_near (develop, 2026-05-15)

Two new MCP tools land the schematic-resident placement-constraint
design agreed in the previous session.

- **`decoupling_audit`** — walks the schematic for cap↔IC power-pin
  pairs (auto-discovered by net analysis OR explicit
  `Placement_Anchor` property), measures each cap's PCB pad-to-pad
  distance to its target IC pin, and flags any over the anchor's
  `within=Nmm`. Per-cap, the closest target is the "primary"; others
  on a shared rail are reported as "secondary" and not flagged
  (avoids the noise where one cap on BAT+ would otherwise flag against
  every IC pin on the rail). Auto-discovery covers `power_in` and
  `power_out` IC pins; ground-net recognition handles GND, VSS, AGND,
  DGND, PGND and standard suffix patterns.

- **`place_near(refs, target, maxDist)`** — snaps PCB footprints to
  within `maxDist` mm of a target pad (`U1.10`) or footprint body
  (`U1`). Searches a 1mm-resolution polar grid out to `maxDist` and
  picks the closest non-overlapping spot. Honors `skipIfWithin`
  (default true), filters by copper layer (back-side parts don't
  block front-side placement), and uses the silk-excluding bbox
  (`GetBoundingBox(False)`) so a long reference designator doesn't
  artificially inflate the collision box.

- **`Placement_Anchor` schematic property + rename propagation.**
  Property grammar: `<REF>[.<PIN>]/within=<N>mm[; ...]`. Plain text
  (no JSON-in-string) so it's human-editable in the KiCad UI.
  `edit_schematic_component(newReference=…)` and `annotate_schematic`
  now scan every `.kicad_sch` in the project and rewrite any
  `Placement_Anchor` values referencing the renamed component;
  unresolved refs are surfaced in the response.

- **`mcp_constraint_version: 1` in `.kicad_pro`** — added by
  `decoupling_audit` / `place_near` callers; lets future tool
  versions evolve the property grammar with explicit compatibility.

Implementation lives in `python/commands/placement_constraints.py`
(pure helpers — parser, rename rewriter, .kicad_pro version helper,
multi-sheet component walker, decoupling-pair discovery, pad-to-pad
distance). Tests: `tests/test_placement_constraints.py` (30 unit
tests on the helpers) and `tests/test_placement_constraint_propagation.py`
(5 integration tests on a minimal kicad-skip fixture).

TypeScript schemas in `src/tools/placement.ts`; Python schemas
synced in `python/schemas/tool_schemas.py`.

### Root-cause fix for delete_trace SWIG corruption (this branch: develop, 2026-05-15)

- **Switched every `board.Remove(item)` call site to
  `board.RemoveNative(item)`** in `commands/routing.py` and
  `commands/component.py`. The previous subprocess workaround in
  `_bulk_strip_via_subprocess` and the `_BULK_STRIP_SUBPROCESS` script
  are removed. The dispatcher's `_reload_required` handling is kept
  (harmless, may be useful to future tools) but no command currently
  sets the flag.

- **Root cause**: pcbnew's `BOARD::Remove(item)` corrupts
  *process-global* SWIG type state after a few hundred mass-removals
  (threshold observed ~650 on power_module). Symptom: subsequent
  method calls on the BOARD — and even a fresh `pcbnew.LoadBoard()` in
  the same process — return bare `SwigPyObject` instead of their
  typed wrappers, breaking every downstream operation until process
  restart. `BOARD::RemoveNative()` skips the listener/undo path that
  triggers the corruption and is the in-process-safe alternative.
  `Delete()` and `DeleteNative()` work too; tested in
  `tests/test_remove_native_does_not_corrupt.py` (integration-gated).
  The 791-track strip used to error mid-flow; now it just works.

### autoroute layerOrder + bulk-strip fix (this branch: develop, 2026-05-15)

- **`autoroute` and `export_dsn` accept `layerOrder`.** Reorders the
  DSN's `(layer ...)` blocks before invoking freerouting, biasing
  freerouting's per-layer cost so it prefers earlier layers. On
  4-layer boards the default is `["F.Cu","B.Cu","In2.Cu","In1.Cu"]`
  (outer first, GND-on-In1.Cu last). Verified on power_module:
  switching from the as-exported `[F.Cu,In1.Cu,In2.Cu,B.Cu]` order to
  the new default dropped inner-layer cut length from 912 mm → 453 mm
  (50% reduction) with only +2 unrouted nets. Helper
  `_rewrite_dsn_layer_order` validates the requested order is a
  permutation of the layers actually present. Tests in
  `tests/test_dsn_layer_order.py` (8 unit tests, no pcbnew needed).

- **`delete_trace(net="*")` no longer corrupts the MCP's pcbnew
  state.** Stripping every track in-process leaves SWIG in a broken
  state where `board.GetDesignSettings()` and even fresh
  `pcbnew.LoadBoard` start returning bare `SwigPyObject` — meaning
  subsequent `add_via` / `import_ses` calls fail with attribute errors
  until the MCP is restarted. Fix: when `net=="*"`, the handler now
  runs the actual `b.Remove(track)` loop in a one-shot subprocess
  and returns `_reload_required` so the dispatcher hot-reloads
  `self.board` and refreshes every command handler's reference.
  Verified end-to-end: `open → delete_trace(net="*") → add_via` now
  succeeds without restart. The dispatcher also skips its auto-save
  in this path so it doesn't clobber the freshly-emptied file.

### New tool: `audit_plane_cuts` (this branch: develop, 2026-05-15)

- **`audit_plane_cuts`** reports signal traces routed on inner copper
  layers that double as power/GND planes — long traces there break
  image-current return paths above F.Cu signals. Returns per-net total
  cut length, per-layer breakdown, and the longest single-trace
  offenders sorted for ripup + retry on an outer layer. Params:
  `layers` (default `["In1.Cu","In2.Cu"]`), `minLength` (default 1mm),
  `unit`. Found on power_module v7 that ~912mm of signal was cutting
  the planes (CELL_TOP nets alone: 174mm, with a single 47mm slice
  through the GND plane). Tests in `tests/test_audit_plane_cuts.py`
  (integration-gated on real pcbnew). Schemas synced in
  `src/tools/routing.ts` and `python/schemas/tool_schemas.py`.

### Bug fixes + tooling (this branch: develop, 2026-05-15)

- **`route_pad_to_pad` now refuses to draw through foreign-net copper.**
  The straight-segment routing had zero obstacle awareness — a single
  call through a dense IC pin field could produce 25+ DRC violations.
  New `_find_route_obstacles` helper does seg/seg intersection for
  tracks, centre-within-radius for vias, and segment-sampled
  `PAD.HitTest` for pads (the sampling avoids false positives on
  fine-pitch adjacent same-net pads). Refusal returns an obstacle list
  with locations and nets; new `checkObstacles=false` overrides.
  Surfaced on the power_module re-route session. Commit `902e7da`.

- **`open_project` now refreshes `.kicad_pro` settings on re-open.**
  KiCad's `SETTINGS_MANAGER` caches the project (netclass clearances/
  widths, design rules) for the life of the process. A plain
  `LoadBoard` on re-open reused that cache, so out-of-band edits to
  `.kicad_pro` were silently ignored — most visibly, `export_dsn` then
  emitted stale netclass clearances. `open_project` now calls
  `SETTINGS_MANAGER.UnloadProject(proj, False)` before `LoadBoard`.
  Verified end-to-end (edit clearance on disk → re-open → fresh value).
  Commit `0f3fb93`.

- **`get_nets_list` `includeStats` is implemented.** Was schema-exposed
  but the handler ignored it; now returns per-net `trackCount`,
  `viaCount`, `totalLength` (+ `unit`).

- **Schema/discoverability fixes** in `src/tools/routing.ts` and
  `python/schemas/tool_schemas.py`:
  - `query_traces` exposes `includeVias` (the Python handler always
    returned vias with UUIDs but the param was missing from the
    schema, so via-delete by UUID was unreachable).
  - `delete_trace` documents `net="*"` (strips every track) in the
    schema description; `traceUuid` renamed from `uuid` in the Python
    schema to match the TS schema and the handler.
  - `route_pad_to_pad` adds `checkObstacles` and a NOTE about
    straight-segment behavior to its top-level description.

### Housekeeping (this branch: fixes/improvements_2, 2026-05-13 part 8)

- **New MCP tool: `find_unrelated_wire_crossings`.**  Moved the
  `_scan_unrelated_wire_crossings` implementation from
  `commands.autoplacer` to `commands.schematic_inspect` and exposed
  it as a standalone MCP tool so post-routing crossing checks can
  be invoked outside the autoplacer's rewire flow.  `autoplacer.py`
  keeps a thin alias for backwards compatibility with `rewire_session`'s
  `unrelated_crossings` return field.  Three tests in
  `TestFindUnrelatedWireCrossings`; route registration locked in.
- Added `*-erc.json` to `.gitignore` and removed the regenerable
  `parent-erc.json` from the worktree (kicad-cli's ERC drops these
  next to the schematic by default).

### Bug Fixes (this branch: fixes/improvements_2, 2026-05-13 part 7)

- **Issue #74 fixed: cross-net merge in WireManager wire splits.**
  `_break_wires_at_point` and `_existing_endpoints_on_segment` were
  net-blind — splitting an existing wire at a point that became a
  junction would silently fuse the wire's net into the new wire's
  net, regardless of intent.  Surfaced earlier on the BMS sheet's
  VC1 + VC2 merge before the Phase 5 + load_session + duplicate-pad
  fixes neutralised the symptom.

  Fix: `WireManager.add_wire` and `WireManager.add_polyline_wire`
  now accept an optional `expected_net=` argument.  When set, the
  two helpers use `walk_wire_chain` to inspect each candidate
  existing wire's chain labels; if any label is foreign (any label
  other than `expected_net`), the split is refused.  The new wire
  then sits on the foreign wire's interior with no junction →
  KiCad's connectivity engine treats it as not connected, which is
  the safe failure mode.  All four call sites pass the expected
  net:
    - `connect_pins` Phase 1-2 (resolved_net per net).
    - `connect_pins` Phase 5 `_try_branch_stub_at_corner`.
    - `connect_to_net` (target net).
    - `_handle_add_schematic_net_label`'s auto-stub.
  Default `expected_net=None` preserves the pre-#74 net-blind
  behavior for callers that don't know the net.

  Two regression tests in `tests/test_wire_manager_cross_net.py`:
    - `test_add_wire_does_not_silently_merge_two_unrelated_nets`
      (was xfail; now passes — checks the kicad_sch file directly
      for a junction at the crossing point).
    - `test_add_wire_without_expected_net_preserves_old_split_behavior`
      (new — locks in the backwards-compat default).

  End-to-end on the three power_module child sheets after this fix:
  netlist equivalence and 0 DUP / 0 CROSS / clean LOOP counts
  preserved.

### Bug Fixes (this branch: fixes/improvements_2, 2026-05-13 part 6)

- **WireManager.add_wire is now idempotent.**  Two mechanisms were
  producing duplicate wire entries in autorouted schematics:

  1. *Convergent route final segments.*  When two `route_pair` calls
     terminate at the same pin coord, the routes can end up sharing
     their final segment.  Both calls then invoked `add_wire` with
     identical `(start, end)` args, stacking parallel wire entries.
     Surfaced as 3x copies of the same wire near U3/1 on charger.
     Fix: outer-call dedupe before the split logic — if a wire
     with the same endpoints (modulo direction) is already on the
     sheet, return success without re-laying.

  2. *Sub-segment duplicates from split logic.*  When the new wire
     passes through an existing endpoint, the split logic divides
     it into sub-segments.  One sub-segment can exactly duplicate
     an existing wire (e.g., new wire (10,10)→(10,0) split at
     (10,5) produces (10,10)→(10,5) which IS the existing
     (10,5)→(10,10) wire).  Fix: post-split dedupe — skip any
     sub-segment whose endpoint pair is already present.

  Across the three power_module child sheets, this brought the
  duplicate-wire count from {12, 7, 4} → {0, 0, 0}.  Netlist
  equivalence + chain pathology checks unchanged.

  - One existing test
    (`test_new_wire_through_existing_endpoint_splits_at_endpoint`)
    asserted a wire-count of ≥4 after the T-junction split case;
    that's now 3 because the redundant upper-half sub-segment gets
    deduplicated against the existing corner wire.  Test rewritten
    to assert the semantic invariant — wire endpoint at (10,5) and
    a junction at (10,5) — instead of counting raw wire entries.
  - One new test
    (`test_add_wire_is_idempotent_for_identical_repeated_calls`)
    in `tests/test_wire_manager_cross_net.py`.

### Bug Fixes (this branch: fixes/improvements_2, 2026-05-13 part 5)

- **Autoplacer: `_scan_unrelated_wire_crossings` resolves nets via
  the wire-graph BFS, not just endpoint-positioned labels.**  The
  previous `_wire_label(w)` shortcut only inspected labels at a
  wire's own two endpoints; it missed labels positioned anywhere
  else on the wire's chain.  Result: when the autorouter laid two
  segments of one labelled chain that happened to cross each other,
  `_scan_unrelated_wire_crossings` reported "1 unrelated crossing"
  even though both wires were the same net.  Surfaced on the
  charger sheet (USB_VBUS, two segments crossing NW of C17).  Fix:
  replace the endpoint-only check with `walk_wire_chain` (cached
  per wire endpoint, so the BFS runs at most once per distinct
  wire).  Two new regression tests in
  ``TestScanUnrelatedCrossings``: same-net crossing not flagged,
  cross-net crossing still flagged.

### Documentation + Reproducer (this branch: fixes/improvements_2, 2026-05-13 part 4)

- ``docs/AUTOPLACER_GUIDE.md`` — new standalone guide for the
  autoplacer.  Covers the four-stage recipe, the per-stage knobs,
  the Jupyter + viz workflow, ``compare_netlists`` / ``diagnose_chains``
  as post-apply validation, and the two known limitations
  (multi-unit duplicate pads, issue #74).
- ``docs/TOOL_INVENTORY.md`` — added the 8 autoplacer tools
  (previously unlisted) and the 2 new validation tools.
  Updated counts.
- ``docs/INDEX.md`` — linked the new guide; tool-count bump.
- ``tests/test_wire_manager_cross_net.py`` — new xfail test
  reproducing the cross-net merge in
  ``WireManager._break_wires_at_point`` (issue #74).  Fails today
  with a clear description in the xfail reason; once add_wire
  refuses net-blind splits, remove the marker.

### New MCP Tools (this branch: fixes/improvements_2, 2026-05-13 part 3)

- **`diagnose_chains`** — enumerate physical wire chains in a
  .kicad_sch, with per-chain labels, pins, bbox, and pathology flags
  (``DUPLICATE_LABELS`` / ``CROSS_NET`` / ``LOOP``).  Uses the same
  ``walk_wire_chain``-based BFS as ``connect_pins`` Phase 5 so chain
  detection is consistent across tools.  Optional ``filterNets``
  param narrows the result to chains carrying any of those names
  (plus any CROSS_NET chains regardless of filter).

- **`compare_netlists`** — given two .kicad_sch files (a "before"
  and "after"), assert every component pin's named-net assignment is
  preserved.  Reports ``missing_pins``, ``extra_pins``, and per-net
  ``lost``/``added`` mismatches.  Use as a regression guard after any
  layout-mutating operation; this is the check that caught the
  C23/BB_SW1 dropped-pin regression in load_session.

Both live in the new ``commands.schematic_inspect`` module and have
unit tests in ``tests/test_schematic_inspect.py`` (10 tests).
Originally developed as throwaway /tmp scripts during the BMS-sheet
debug cycle; promoted here so they're durable + LLM-callable.

### Bug Fixes (this branch: fixes/improvements_2, 2026-05-13 part 2)

- **connect_pins: multi-unit duplicate-pad dedupe.**  Two related bugs
  on nets that span multi-unit symbols with duplicate pads (FDS9926A
  pins 5+6 on a source pad; J1 USB-C's A1+A12+B1+B12 GND pads at one
  symbol pin):

  - *Phase 1-2 union on zero-distance pairs.*  When the MST picked a
    pair at distance 0 (two pin keys resolving to the same world
    coord), the existing degenerate-route handling skipped the wire
    but never `_union`'d the two MST nodes.  Subsequent MST candidates
    from either of those pins then got tried as separate edges, laying
    parallel wires to the same destination — the LOOP source on
    FET_MID and similar nets.
  - *Phase 3 dedupe by IU coord.*  When Phase 3 stub-and-labels each
    unwired pin separately, N duplicate-pad pins at one coord
    produced N stacked overlapping labels (DUPLICATE_LABELS).  Now
    Phase 3 tracks a ``covered_pin_ius`` set (seeded from
    ``wired_pin_set``'s pin coords) and skips any pin whose coord is
    already covered.
  - Side effect: ``pin_endpoints`` is now populated for every
    ``try_wire`` call, not just non-power-net ones, so Phase 3's
    coord dedupe also catches GND chains under #PWR_GND (multiple
    GND pads at one symbol coord).

  End-to-end on the three power_module child sheets: all three now
  pass netlist-equivalence + chain-pathology validation
  (``diagnose_chains.py``) with 0 DUPLICATE_LABELS and 0 CROSS_NET.

  New regression test:
  ``TestPhase5ChainFinder::test_duplicate_pad_pins_dedupe_in_phase3``.

### Bug Fixes (this branch: fixes/improvements_2, 2026-05-13)

- **Autoplacer: net discovery via the real wire graph (T-junction
  aware, no depth limit).**  ``load_session`` was using a hand-rolled
  BFS over wire endpoints with a depth-5 hop limit and no T-junction
  handling.  Pins whose nearest label was more than five wire-segments
  away — or reachable only through a mid-segment T-junction — got no
  net assignment, were missing from the session's net membership,
  and then dropped permanently when ``apply_to_schematic`` stripped
  the wiring before ``rewire_session`` re-routed.  Surfaced on the
  buckboost sheet where C23/2 (bootstrap cap on BB_SW1) was 8 hops
  from any BB_SW1 label and ended up disconnected in the rewired
  file.  Replaced the BFS with ``walk_wire_chain`` so net discovery
  uses the same robust wire-graph traversal as Phase 5 in
  ``connect_pins``.  One new test
  (``TestLoad.test_load_discovers_net_via_long_wire_chain``).

### New MCP Tools (this branch: fixes/improvements_2, 2026-05-13)

- **`autoplacer_recipe`** — runs a four-stage anneal (cluster →
  spread → polarize → settle) on a loaded session.  Tuned on the BMS
  sheet: stage 1 uses attraction-only to cluster components by their
  net connections, stage 2 ramps repulsion up geometrically over
  11 sub-stages to fan clusters apart without losing the grouping,
  stage 3 steps repulsion down two notches while turning on polarity
  bias + polarity torque, stage 4 lets the configuration settle
  under natural temperature decay.  Throughout stages 1-3 the
  per-iteration temperature cap is clamped to a small fixed value
  (default 5 mm) so displacement control comes from force scheduling
  rather than the default cooling schedule.  Defaults match the
  user's BMS-tested recipe; all twelve knobs (cluster_iters,
  spread_stages, polarize_stages, settle_iters, iters_per_stage,
  step_temperature, base_attraction_k, base_rotation_k,
  repulsion_base, repulsion_growth, polarity_k, polarity_torque_k)
  are optional MCP overrides.

  Python API: `commands.autoplacer.run_staged_anneal(sess, **kw)` and
  `PLACER.recipe(schematic_path, on_step=..., **kw)` (the latter
  threads a per-iteration callback for matplotlib viz hookup).

### Bug Fixes (this branch: fixes/improvements_2, 2026-05-12 part 2)

- **connect_pins Phase 5: stub-style label now triggers on total
  chain count, not just orphan count.**  Previously
  ``use_stub_style = len(orphan_chains) >= 2``, which missed the case
  where Phase 3 had already stubbed one of the net's chains.  Result
  on a net like VC3 (multi-pin chain at U1+C4 plus an R4 single-pin
  stub Phase 3 contributed): the multi-pin chain got an in-line label
  on a free wire endpoint, the R4 chain had a proper Phase-3 stub
  label, and the two read asymmetrically.  Fix:
  ``use_stub_style = len(this_net_chains) >= 2`` where
  ``this_net_chains`` includes both orphan AND already-labelled-with-
  resolved_net chains.

- **connect_pins Phase 5: merge guard in branch-stub placement.**
  ``_try_branch_stub_at_corner`` could lay a 2.54 mm perpendicular
  stub whose end-point coincided with another disjoint orphan chain
  of the same target net — KiCad's sync_junctions then added a
  junction and the chains merged.  Result on REGOUT / SRP on the
  BMS sheet: two separate 4-6 pin sub-chains got branch-stub labels,
  then merged into a single chain carrying both labels (a LOOP-flagged
  duplicate-label state).  Fix: before laying a candidate stub, walk
  the wire chain at the stub-end position; if it returns a chain
  whose ``points`` set is disjoint from our chain's points, refuse
  and try another perpendicular direction.

- **connect_pins Phase 5: re-walk each chain just before labelling
  it.**  An earlier iteration's branch-stub may have merged this
  chain with another or labelled it through some unforeseen path.
  Re-walking with the freshest file state catches both cases and
  lets us skip a chain that no longer needs a label (or that has
  acquired a foreign-net label, indicating cross-net contamination
  to be left for ERC).

Two new tests:
``TestPhase5ChainFinder::test_stub_style_kicks_in_when_phase3_already_stubbed_one_chain``,
``TestPhase5ChainFinder::test_branch_stub_refuses_to_bridge_to_another_chain``.
Diagnostic helper at ``/tmp/claude-1000/claude/diagnose_chains.py``
enumerates chains/labels/pins per chain and flags
``DUPLICATE_LABELS``/``CROSS_NET``/``LOOP`` — handy for verifying
fixes on real schematics.

### Bug Fixes (this branch: fixes/improvements_2, 2026-05-12)

- **connect_pins Phase 5: rewrite on top of the real wire graph.**  The
  previous Phase 5 auto-label code did chain grouping by union-find over
  `wired_pairs` segment endpoints — a proxy that missed mid-segment
  T-junctions and produced two labels per net when two routed pairs
  connected only through a wire-interior hit.  A `phase3_future_labels`
  lookahead was added as a band-aid but didn't cover all cases.
  Replaced with a new public helper
  `commands.wire_connectivity.walk_wire_chain(point_mm, schematic_path)`
  that BFSes the actual wire graph (reusing the T-junction-aware
  `_build_adjacency`).  Phase 5 now runs AFTER Phase 3 (so the file's
  wire graph already reflects every label Phase 3 added) and walks
  chains directly from each wired pin endpoint, deduplicating by
  `wire_indices`.  Single orphan chains get an in-line label on a free
  wire endpoint; multi-orphan-chain nets get a perpendicular branch-stub
  at a corner.  Defensive: a chain that already carries a foreign-net
  label is skipped (don't compound a cross-net merge defect — see issue
  #74).  Fixes the duplicate-label cases observed on BMS-sheet runs
  (FET_MID, REGOUT, VC3 double-labels).  10 new tests
  (`TestWalkWireChain` × 9, `TestPhase5ChainFinder` × 4); the union-find
  pair-grouping path and the `phase3_future_labels` lookahead are gone.

### New MCP Tools (this branch: fixes/improvements_2, 2026-05-09)

- **Schematic autoplacer** (7 tools: `autoplacer_load`, `autoplacer_set_params`,
  `autoplacer_iterate`, `autoplacer_run`, `autoplacer_state`,
  `autoplacer_preview`, `autoplacer_apply`).  Force-directed
  Fruchterman-Reingold with schematic-specific extras: real bbox
  computation from `lib_symbols` graphics, pin-orientation torque,
  polarity bias (GND-down, V+-up by name pattern), per-unit
  components for multi-unit symbols (Q1's two FET units move
  independently).  Lifecycle: load schematic → tune params →
  iterate / run-until-stable → preview to a temp file → apply
  (snap-to-grid, write back, re-route via stub+label per pin).
  Implementation in `commands/autoplacer.py`.  9 tests in
  `tests/test_autoplacer.py`.  Restart MCP server to pick up.

  As of 2026-05-10 the `apply` step's re-routing uses
  `connect_pins(style="auto")` per net, so the autorouter draws real
  inter-pin wires when feasible and falls back to label-with-stub on
  each pin otherwise.  This is gated on the multi-unit PinLocator fix
  below — without that, pin coords for unit-2 pins on multi-unit
  symbols resolved to unit-1's location.

- **`add_schematic_sheet`** — instantiates a hierarchical sheet block on a
  parent .kicad_sch that references a child .kicad_sch file. Until now
  the hierarchical-schematic story had a hole: `add_sheet_pin` adds a pin
  to an existing sheet block, `add_schematic_hierarchical_label` adds a
  label to a sub-sheet, and `create_schematic` creates an empty
  .kicad_sch — but no tool placed the actual sheet rectangle on the
  parent. Doing it required hand-editing the .kicad_sch. The new tool
  takes parent path, sheet name, child filename, position, optional
  size/page/uuid; computes the parent's project name + root UUID; emits
  the full S-expression with the per-page `(instances …)` entry;
  refuses duplicates by name. Implementation in
  `commands/sheet_manager.py`. Tests in `tests/test_sheet_manager.py`
  (5 unit + 1 kicad-cli round-trip).

### Tool Enhancements (this branch: fixes/improvements_2, 2026-05-09)

- **`add_schematic_net_label` auto-rotates the label to the pin's
  outward direction by default.** Previously the label always read
  rightward (orientation 0), which is fine for right-facing pins but
  overlaps the symbol body on left/up/down-facing pins — making the
  schematic harder to trace by eye than it had to be. Now when the
  caller supplies `componentRef`+`pinNumber` and doesn't pass an
  explicit `orientation`, the handler reads the pin's outward angle
  via `PinLocator.get_pin_angle` and uses that for the label.
  Explicit `orientation=N` still wins. Response gains
  `orientation` (the angle that was applied) and
  `auto_orientation: true` when the default kicked in. Tests in
  `tests/test_label_auto_orientation.py` (4 cases: pin 1 / pin 2 /
  explicit override / position-only fallback).

- **`get_schematic_view` now crops to placed-content bbox + drops the
  page frame by default.** Mirror of the recent `get_board_2d_view`
  treatment — previously the SVG was the whole A4 page including title
  block, leaving the schematic as a tiny island in a sea of whitespace.
  New defaults (`cropToContent=true`, `margin=0.05`) compute the bbox
  of placed symbols / wires / labels / sheet blocks (sheets contribute
  their full rectangle, not just the top-left anchor) and pass
  `--exclude-drawing-sheet` to kicad-cli so the page frame doesn't
  render outside the crop. Pass `cropToContent=false` for the legacy
  full-page render. Also fixed: kicad-cli on a hierarchical design
  writes one SVG per sheet (`<stem>.svg` for top, `<stem>-<sub>.svg`
  for each child); the handler now picks the bare-stem file by name
  instead of `glob.glob[0]` which was returning a child arbitrarily.

### Tool Enhancements (this branch: fixes/improvements_2, 2026-05-10)

- **Autoplacer: multi-unit pin/stub-end collision safety.**
  `snap_positions`'s same-ref blanket exemption let two units of one
  multi-unit symbol overlap, and the pin-coord-collision pass only
  checked pin endpoints (not stub-end positions).  Surfaced on a BMS
  run where Q1 unit 1's drain stub-end (pin endpoint + 2.54mm outward,
  where Phase 3 lays the label) landed exactly on Q1 unit 2's source
  pin endpoint, which was on net SRP — so the rewire merged FET_MID
  and SRP nets through the stub.  Two changes:
  1. Bbox-overlap and pin-coord passes now exempt pairs by `(ref,
     unit)` rather than `ref` alone.  Different units of one symbol
     are physically distinct components and must separate.
  2. Pin-coord pass now considers each pin's *stub-end* position
     (pin + 2.54mm in the outward direction) in addition to the pin
     endpoint, so endpoint↔stub-end collisions are caught.
  Test in `test_autoplacer.py::TestSnapPositions::
  test_multi_unit_pin_stub_end_collision_resolved` (verified to fail
  on the pre-fix code).

- **Autoplacer real-time visualizer** (`commands/autoplacer_viz.py`):
  matplotlib-backed live view of an autoplacer Session.  Designed for
  interactive parameter tuning in an iPython session — open with
  `viz = AutoplacerViz(sess)`, iterate the placer, call `viz.update()`
  to redraw.  Components render as bbox rectangles with the ref text
  in the centre and pins as cyan dots.  Force overlays:
  - Red lines per pin-pair attraction edge, intensity scaled to
    magnitude (so weak pulls fade and strong ones are bright).
  - Blue lines per component-pair repulsion, top-30 by magnitude
    (configurable; N(N−1)/2 pairs would be too noisy on a 30-comp
    sheet).
  - Green sum-of-forces stub from each component centre, scaled so
    the largest force vector reads ~8 mm.
  Toggleable per-layer at construction time
  (`show_attraction=False`, `show_repulsion=False`, etc.).  Schematic
  Y-axis is inverted so the view matches what KiCad shows.  Works
  headless under the Agg backend (`viz.save("/tmp/state.png")`) for
  CI / batch runs.  `matplotlib>=3.7` added to requirements.txt as an
  optional viz-only dep; the import is lazy so non-viz callers
  aren't affected.  5 smoke tests in `tests/test_autoplacer_viz.py`.

- **Schematic router rule 7: stub-zone reservation.**  Every pin now
  has an implicit 2.54 mm "stub zone" running outward from its
  endpoint along the pin's outward angle.  Routes for OTHER nets
  can't traverse the stub zone — they'd otherwise create the "wire
  crosses stub" pattern when Phase 3 of `connect_pins` lays the
  label-stub.  Implementation:
  1. `Obstacles` gains a `pin_angles: Dict[(int_um_x, int_um_y), float]`
     field.  `_collect_pin_endpoints` populates it via
     `PinLocator.get_pin_angle` for every collected pin.
  2. `_build_grid_obstacles` blocks 1 and 2 cells outward of every
     non-own pin — A* routes can't enter the stub zone.
  3. `check_spurious_connections` adds rule 7: candidate segment
     must not strictly cross any pin's outward stub segment.  Covers
     the shape-router (straight/L/U) which doesn't go through the A*
     grid.  Tests in
     `tests/test_schematic_router.py::TestCheckSpuriousRule7_StubZoneCrossing`
     (4 cases: cross-rejected, far-allowed, no-angle-skips, own-pin-exempt).

  Knock-on: `connect_to_net` (Phase 3 of connect_pins) now runs the
  prospective stub through the spurious-connection guard before
  laying it.  If a 2.54 mm stub would create a crossing, lengths up
  to 6.35 mm are tried in turn; falls back to 2.54 mm with an info
  log if no length fits (pre-existing behaviour preserved).

  Also: `rewire_session` now scans the final schematic for
  unrelated-net wire crossings without junctions and returns a list
  of findings as `unrelated_crossings` in the result dict.  Each
  is electrically harmless on its own (KiCad treats no-junction
  crossings as not connected) but flags places where any future
  endpoint landing at the crossing point would silently merge two
  unrelated nets.  Verified end-to-end: BMS, charger, and buckboost
  all produce 0 crossings post-fix (down from 4 / 1 / 1).

- **Autoplacer: preserve no_connect markers + centre bbox on page.**
  Two complementary tweaks at apply time:

  1. *no_connect preservation*: `apply_to_schematic` strips every
     `(no_connect)` marker along with wires/labels/junctions, so
     deliberately-unconnected pins (NC pins on ICs) come back as
     ERC "Pin not connected" errors after the placer runs.  Fix:
     in `load_session`, find every no_connect's pin owner (by world-
     coord match) and store `(comp_key, pin)` in
     `Session.no_connects`.  In `rewire_session`, after the per-net
     wiring, re-emit a no_connect marker at each (now-translated)
     pin's world coord.  Coincident markers (FDS9926A duplicate-pad
     pins 5/6 and 7/8) are deduped so KiCad doesn't see redundant
     entries.

  2. *centre on page*: new `center_components_on_page` runs after
     `snap_positions` in `PLACER.apply` (gated on `center_on_page=True`
     by default).  Applies a rigid translation to ALL components
     (including pinned) so the bbox of the placed assembly is centred
     on the A4 page centre (≈ 148.59, 104.78 mm).  The translation
     preserves relative positions, so pinned components shift along
     with mobile ones — without that, a pinned connector at the
     schematic's original anchor would stay put while the rest moves
     to the page centre, breaking routing across them.

  Tests in `tests/test_autoplacer.py`:
  `test_no_connect_marker_recorded_on_load`,
  `test_apply_re_emits_no_connect_at_new_pin_position`,
  `test_centers_bbox_on_page`,
  `test_rigid_translation_preserves_relative_positions`.

- **Autoplacer: pin-aware attraction + multi-pass snap with pin-collision
  safety check.** Three related improvements that change how the
  placer thinks about connected components:

  1. *Pin-aware attraction*: the attractive force on a net edge now
     pulls the SPECIFIC pins on each end together (via
     `_attractive_force_pinwise`), not the component centres.  Without
     this, decoupling caps on a large IC's perimeter pile on the IC's
     centre.  With it, components naturally line up edge-to-edge with
     connections short and visible — verified on the BMS sheet where
     the run-time wires went from ~17 to ~23 (out of 19 named nets,
     so multi-pin nets are now mostly fully wired).
  2. *Multi-pass `snap_positions`*: the bbox-overlap nudge sweep now
     runs until a full pass produces no movement (vs single-pass
     before).  Single-pass missed chain reactions where pair (i, j)
     nudged j into a new overlap with k where k < i — k had already
     been processed, so the overlap was never resolved.
  3. *Pin-coord-collision safety pass*: after bbox resolution, scan
     every pin's world coord and nudge any component whose pin lands
     on another component's pin.  This is the last line of defence
     against net merges: connect_pins(auto) wires same-net pins
     together, and if two different-net pins sit at the same coord
     the wires merge those nets.  Verified on the BMS sheet where the
     pre-fix polish run produced spurious BAT+/CELL1_TOP and
     REGOUT/VC2 net merges; post-fix produces 0 merges.

  Tests in `tests/test_autoplacer.py`: `test_pinwise_attraction_*`,
  `test_pin_coord_collision_resolved`,
  `test_multipass_resolves_chained_overlaps`.

- **Autoplacer `snap_positions` exempts same-ref multi-unit pairs from
  overlap resolution.** Without this exemption, two units of the same
  multi-unit symbol (e.g. Q1 unit 1 + Q1 unit 2) could trip the
  bbox-aware overlap resolution and get nudged apart even though
  they're meant to occupy distinct lib coordinates by design.  Now the
  resolver `continue`s on `a.ref == b.ref`, leaving the per-unit
  components where the iteration force model placed them.

### Bug Fixes (this branch: fixes/improvements_2, 2026-05-10)

- **`connect_pins(auto)` lost the net label entirely on duplicate-pad
  multi-unit pins.**  `connect_pins` for a net like FET_MID
  (FDS9926A drain pads where pin 7 = pin 8 at the same lib coord)
  returned `success=True` for the degenerate Q1/7→Q1/8 route with
  `segments=[]`, then added both pins to `wired_pin_set`.  Phase 3
  skipped them ("already wired"), and Phase 5 had no segments to
  operate on (`if not segs: continue`).  Net result: the entire
  FET_MID name vanished from the schematic.  Same root cause hit
  charger USB_VBUS where J1's A4/A9/B4/B9 are all at the same coord.
  Fix: when `result.success and not result.segments`, `continue`
  without adding pins to `wired_pin_set`; Phase 3 then labels each
  pin individually so the net name is preserved.

- **Schematic router could route through its own component's bbox.**
  `_build_grid_obstacles` skipped the route's own ref-pair from the
  bbox-blocking pass, so a wire leaving pin A of a tall symbol could
  double back through the symbol body on its way to pin B.  Drop the
  skip — the actual pin-endpoint cells are still re-allowed at the
  end of the function (`discard(cell_p1)` / `discard(cell_p2)`), so
  start/goal stay reachable but the body proper now blocks traversal.

- **`WireManager.add_wire` didn't split a new wire at existing wire
  endpoints on its interior.** The existing logic split *existing*
  wires at the new wire's endpoints, but the symmetric case — a new
  wire passing through an existing endpoint — wasn't handled.  The
  new wire stayed one long segment passing over the existing endpoint
  with no T-junction marker.  KiCad's wire-graph then treated the
  wires as crossing-not-joining (visually "touching but not
  connected"), and sync_junctions never saw ≥3 endpoints at the
  crossing point so it didn't add a junction either.  Repro on
  `projects/autoplacer_test` D_BUS: pair-1's bus corner at
  (152.4, 90.17) was on the interior of pair-2's vertical run.  Fix:
  in `add_wire`, after splitting existing wires at the new endpoints,
  collect every existing wire/pin endpoint that falls strictly on the
  new segment's interior and emit the new wire as multiple segments
  meeting at those points — sync_junctions then sees ≥3 endpoints
  there and adds the junction.  New helpers
  `_existing_endpoints_on_segment` and `_segments_split_at`.  Test:
  `tests/test_wire_junction_changes.py::TestSyncJunctionsIntegration::
  test_new_wire_through_existing_endpoint_splits_at_endpoint`.

- **`add_schematic_net_label` only added a wire stub for connector
  refs.** Every other pin got a bare label at the pin endpoint —
  KiCad ERC then reported "Label not connected to anything" because
  it couldn't see the pin↔label connection without a wire segment
  between them.  Surfaced 2026-05-10 on `projects/autoplacer_test`
  while verifying the multi-unit fix (5 of 6 ERC errors were this).
  Fix: always emit the 2.54mm outward stub and put the label at the
  stub's far end (the logic that already existed for `J*` refs,
  applied to every snap-to-pin call).  Falls back to bare-label-at-
  pin only when `get_pin_angle` returns None.  Tests in
  `tests/test_net_label_pin_snapping.py` (3 cases: stub position+wire
  call, stub respects pin angle, fallback when angle unknown).

- **`connect_pins(style="auto")` astar-tee terminated after one cell
  when its start pin was already on a same-net wire.** Repro on a
  3-pin connect_pins call where pair-1's wire ends at pair-2's
  starting pin: A* walked from cell (0,0) onto cell (0,-1), saw it
  was on a same-net "tee target", and stopped — the run-out leg to
  the third pin never got emitted.  Caller saw `success: true` with
  a 1.27mm stub instead of a real route.  Surfaced 2026-05-10 on
  `projects/autoplacer_test` D_BUS (Q1/8 + Q1/6 + R3/2): pair-1
  ended at Q1/6 = pair-2's p1, pair-2 stub'd in place.  Fix: in
  `_route_pair_with_astar`, BFS the same-net wire graph from p1 and
  exclude every reachable cell from `extra_goal_cells` — the router
  can't "tee" onto a wire that's already connected to its own
  start.  New helper `_wire_reachable_cells_from_start`.  Test in
  `tests/test_schematic_router.py::TestConnectPinsStyle::
  test_astar_tee_doesnt_terminate_on_wire_already_at_p1`.

- **`PinLocator` multi-unit pin lookup returned wrong coords.** When a
  schematic placed two units of the same multi-unit symbol (e.g.
  `Q1 unit 1` + `Q1 unit 2` of a dual N-FET), `get_pin_location`
  found the first placed instance by reference and transformed every
  pin number against ITS `(at)` — even pins owned by a different
  unit's sub-symbol.  Result: queries for unit-2 pins returned
  unit-1's world coord, breaking `connect_pins(style="auto")`,
  `get_schematic_pin_locations`, and any other tool that resolves
  pins through PinLocator.  Fix: parse the lib_symbols sub-symbol
  naming `<base>_<unit>_<convert>` (new
  `PinLocator.parse_pins_per_unit` + cached `get_pins_per_unit`),
  enumerate every placed instance by reference (new
  `_find_placed_instances` returns x/y/rotation/mirror/lib_id/unit),
  match each requested pin to its owning unit, and transform against
  THAT instance's `(at)`.  `get_pin_angle` and `get_all_symbol_pins`
  use the same path.  Tests in `tests/test_pin_locator_multi_unit.py`
  (4 cases: unit-1 sanity, unit-2 unique coord, get_all_symbol_pins
  unit-aware, rotation differs per unit).  Knock-on: autoplacer's
  `rewire_session` switched back from "labels+stubs only" to
  `connect_pins(style="auto")` per net, so it draws real inter-pin
  wires when feasible.  Restart MCP server to pick up.

### Bug Fixes (this branch: fixes/improvements_2, 2026-05-09)

- **`connect_pins(style="auto")` left orphaned wired chains on a floating
  ghost sub-net.** The autorouter wires consecutive pin pairs and
  Phase 5 added one auto-label at the first wired pair. If the
  autorouter formed two disjoint chains (cluster 1 wires, middle pair
  fails to label-fallback, cluster 2 wires), only cluster 1 got a
  label — cluster 2's pins were reported as "connected" but in reality
  sat on an unlabeled sub-net which KiCad auto-named `Net-(Cn-Pad1)`,
  silently fragmenting the named net. Surfaced when partitioning the
  power_module flat schematic into hierarchical sheets — three cap-pair
  orphan chains had to be patched manually. Fix: Phase 5 iterates every
  wired pair, classifies whether its endpoints are reachable from a
  target_net label via the wire graph, and labels each orphaned chain
  (re-computing reachability after each label so we don't
  double-label). Result dict gains
  `auto_label_positions: List[List[float]]`; legacy
  `auto_label_position` preserved as the first entry. Test:
  `test_auto_labels_each_orphaned_chain`.

- **Autorouter allowed perpendicular wire crossings.**
  `check_spurious_connections` had 5 rules — pin / label / T-junction /
  collinear overlap / symbol body — but no rule against two unrelated
  wires crossing at right angles. Such crossings are electrically valid
  in KiCad (no junction → no connection) but visually confusing, and
  they made the buck-boost layout unreadable. Added rule 6 plus helper
  `_segments_strictly_cross` (orthogonal pairs intersecting strictly
  inside both segments; T-junctions and shared endpoints return False
  so other rules don't double-fire). Same-net crossings still allowed
  (the same-net relaxation set already covered tee/joins). Tests:
  `TestSegmentsStrictlyCross` (6 cases),
  `TestCheckSpuriousRule6_WireCrossing` (4 cases); the previously-
  inverted `test_wire_crossing_perpendicular_allowed` was renamed
  and flipped to `…_rejected_by_rule_6`.

- **`PinLocator` cached schematic state without invalidation.**
  `ConnectionManager._pin_locator` is a class-level singleton; the
  underlying `PinLocator` instance held three caches keyed by file
  path (`_schematic_cache`, `_sexp_cache`, `pin_definition_cache`)
  that were populated on first access and never invalidated. A
  subsequent write through a different code path (e.g.
  `DynamicSymbolLoader.add_component` from a script while the server
  is running) left the locator looking at the old component list —
  `connect_pins` then reported "N failed" for every pin on a
  freshly-added component while `add_schematic_net_label` (reads
  fresh) worked. Fix: `_invalidate_if_changed(schematic_path)` stats
  the file at every public entry point and clears all path-keyed
  cache entries when mtime advances. Tests:
  `tests/test_pin_locator_cache_invalidation.py` (positive + negative).

### Tool Enhancements (this branch: fixes/improvements_2, 2026-05-08)

- **`get_board_2d_view` now crops to the board outline and renders per-layer
  colors by default.** Previously the tool plotted the whole A4 page
  monochromatically, leaving the board itself as ~20% of the canvas — text was
  barely legible at 1400×1000 px. New default (`cropToBoard=true`,
  `colored=true`) plots each layer to its own SVG, sets the viewBox to the
  board's edge bbox + 5% margin, recolors per layer (F.Cu red, B.Cu blue,
  Edge.Cuts tan, F.SilkS white) and composites to PNG/JPG/SVG. Pass
  `cropToBoard=false, colored=false` for the legacy whole-page monochrome
  plot. Response shape unchanged (still `imageData` + `format`); no API break.

### Bug Fixes (this branch: fixes/improvements_2, 2026-05-08)

- **`add_schematic_component` wrote malformed `(instances …)` blocks** — every placed component
  had `(project "project") (path "/")` literal placeholders, so KiCad couldn't match the open
  project to the placement-time reference and showed all annotations as `R?`/`SW?`/etc. Fixed:
  project name derived from the schematic stem, root sheet UUID from the first `(uuid …)` in the
  file. `python/scripts/repair_instance_blocks.py` patches existing schematics in place.

- **`list_schematic_nets` swapped pin 1 ↔ pin 2 on rotated symbols** — `_find_pins_on_net` had an
  inline coord transform that omitted the math-CCW → screen-CCW angle negation, mirroring pin
  positions and assigning wires to the wrong pin number for rotation=90/270 (e.g. resistors).
  Wires landed on the right pixels; only the reported pin number was wrong. Fix: delegate to the
  canonical `WireDragger.pin_world_xy`.

- **Symbol bbox shifted off-position for asymmetric symbols on every rotation** — `_transform_local_point`
  used non-negated math-CCW rotation; `_compute_pin_positions_direct` had the same bug AND was
  missing the y-flip from lib y-up to schematic y-down entirely. Symmetric bodies (R, C) hid the
  bug because min/max over symmetric points is invariant. Asymmetric symbols (USB_B_Micro,
  LED_RGBK at rot=90/270, op-amps, MCUs) had bboxes shifted by up to 2.54 mm — the autorouter's
  body-crossing detection (`_collect_bboxes` → `_compute_symbol_bbox_direct`) could miss real
  crossings or fire on empty regions. Both helpers now delegate to `WireDragger.pin_world_xy`.

- **`sync_schematic_to_board` autoImport reported success but didn't persist** — handler kept a
  local `board = pcbnew.LoadBoard(board_path)` while auto-import placed footprints into a
  separate `self.board` (no save). The final `board.Save()` clobbered disk with the stale empty
  state. Fix: consolidate around `self.board` throughout — when `boardPath` is provided,
  `self.board` is reloaded from it (and command handlers refreshed) so all subsequent operations
  share state.

- **`export_bom` schema missing `schematicPath`** — Python handler accepted it, but the TS
  `server.tool()` schema didn't expose it or forward it through `callKicadScript`. Effect: BOM
  exports always missed schematic-only properties (LCSC, MPN, custom datasheet) even with the
  schematic alongside the PCB. Added the param to the Zod schema, expanded the description, and
  forwarded the value.

- **`save_project` schematic save crashed** — called the non-existent `sch.to_file()`; kicad-skip
  exposes `sch.write(fpath)`. Every save_project call against a project with a schematic emitted
  a "Schematic save failed: 'Schematic' object has no attribute 'to_file'" warning. No data loss
  (every individual schematic edit auto-saves), but the explicit save was effectively broken.

- **JLCPCB `category` column was always empty after import** — jlcsearch's `/components/list.json`
  endpoint doesn't return per-row category, and `import_jlcsearch_parts` did
  `part.get("category", "")` → empty string. Filters like `search_jlcpcb_parts(category="Resistors")`
  matched zero rows. Fix: `_derive_category_from_description` keyword-sweeps the description into
  one of ~20 categories (Resistors, Capacitors, Inductors / Ferrite Beads, Diodes / Schottky,
  ICs / Microcontrollers, etc., specific outranks generic). ~87% coverage on a 50K random sample.
  `import_jlcsearch_parts` derives when upstream is empty; new `backfill_categories` method
  patches pre-existing rows; CLI at `python/scripts/backfill_jlcpcb_categories.py`.

- **JLCPCB FTS missed ASCII unit queries** — descriptions use Unicode unit symbols (`Ω`, `μF`)
  and FTS5's unicode61 tokenizer keeps `150Ω` as a single token (lowercased to `150ω`). Queries
  like `150ohms 0603` or `1kohm` matched zero rows because no ASCII alias exists in the index.
  Fix: `_normalize_query_units` rewrites unit suffixes in the search query before FTS, so
  `150ohms 0603` → `150Ω 0603`, `1kohm` → `1kΩ`, `10uF` → `10μF`.

### Tool Enhancements (this branch: fixes/improvements_2)

- **`search_jlcpcb_parts` order_by parameter** — New `order_by` parameter on `search_jlcpcb_parts`,
  with values `"stock_desc"` (default), `"stock_asc"`, and `"none"`. Previously the SQL had no
  `ORDER BY` clause so result order was implementation-defined. The new default surfaces high-stock
  parts first, restoring the long-standing "search → sort by stock → pick first relevant" workflow
  as a proxy for ongoing availability. Allowed values are validated against a whitelist (no SQL
  injection surface). Added 6 regression tests.

### Bug Fixes (this branch: fixes/mcp-server-improvements)

- **`search_symbols` 30 s timeout** — Background cache warming writes all 224 symbol libraries
  to a persistent disk cache (`~/.cache/kicad-mcp/symbol_cache.pkl`). Cold startup warms in the
  background (~80 s); warm startups load from disk in <50 ms. Cache is invalidated by `.kicad_sym`
  file mtimes.

- **Symbol parser drops MCU libraries** — Naive parenthesis counter did not skip content inside
  quoted strings. STM32H5/H7 pin alternate-function names like `TIM1_CH4(N)` inflated the depth
  counter so entire symbol library blocks were silently discarded. Fixed with a string-aware walker.

- **`add_schematic_component` off-grid placement / ERC noise** — `x`/`y` coordinates now snapped
  to the nearest 1.27 mm (50-mil) grid before writing `(at x y rot)`. Off-grid placement caused
  ~25 ERC "pin or wire end off connection grid" warnings per component. Placement refused when
  another symbol is within one grid step. Response includes `placed_at`, `snapped_from`, `snap_note`.

- **`add_schematic_component` missing rotation** — `rotation` parameter was accepted by the schema
  but never threaded to the S-expression writer. Now writes `(at x y angle)` correctly.

- **`get_schematic_pin_locations` y-axis sign error** — `.kicad_sym` uses y-UP; `.kicad_sch` uses
  y-DOWN. The previous formula mirrored all pin positions around the symbol centre (VCC/GND, TRIG/RESET
  all transposed). Also fixes `mirror_y` which was incorrectly negating `ly` instead of `lx`.

- **`run_erc` coordinate units** — kicad-cli reports positions in 1/100 mm; now multiplied by 100
  and reported as `x_mm`/`y_mm`.

- **`run_erc` noise filtering** — `endpoint_off_grid`, `lib_symbol_issues`, and `lib_symbol_mismatch`
  moved to `noise_violations` array and excluded from `severity_counts`. New `includeNoise` boolean
  parameter re-enables all suppressed types.

- **`add_schematic_net_label` connector stubs** — When `componentRef` starts with `J`, automatically
  adds a 2.54 mm wire stub in the pin's outward direction before placing the label. Fixes ERC
  not-connected false positives on connector pins.

- **`sync_schematic_to_board` unlabeled wire clusters** — BFS only propagated from labeled seed
  points; wire clusters with no label were never named, leaving pads unmatched. Added a step that
  synthesises `Net-(ref-Padpin)` names for unlabeled clusters, mirroring KiCAD's own behaviour.

- **`export_gerber` layer files silently discarded** — `PLOT_CONTROLLER` was missing `OpenPlotfile()`
  per layer so all copper/mask/silk Gerbers were dropped. Switched to `kicad-cli pcb export gerbers`.

- **`save_project` saved PCB only** — Now auto-detects and round-trips the matching `.kicad_sch`.
  Accepts `schematicPath` for schematic-only workflows. Returns list of saved files.

- **`export_bom` custom properties unavailable** — Custom properties (LCSC, MPN, etc.) are on
  schematic symbols and not synced to PCB footprints. New `schematicPath` parameter reads directly
  from `.kicad_sch`. `includeAttributes` now works. `groupByValue` carries attributes through.
  `references` serialised as semicolon-separated string.

- **`list_schematic_labels` no net context** — Each label entry now includes `connected_pins`
  (component refs + pin numbers reachable via wire BFS).

### New MCP Tools (this branch)

- **`connect_pins`** — Connect N pins to a single net. Discovers existing labels via BFS before
  writing, avoids duplicate/orphaned labels, handles A→B→C orphan case, detects conflicts,
  idempotent. With `style="auto"` or `"wire"`, draws real polyline wires between pins instead
  of label stubs everywhere — tries straight → L → U → A\* in order, with same-net tee
  detection, a 5-rule spurious-connection guard, and an auto-label so the wire fragment is
  named in KiCad. See `docs/SCHEMATIC_AUTOROUTER_PLAN.md` for the full design.

- **`connect_component_to_nets`** — Connect all pins of one component via a `{pin: net}` map.
  Replaces N individual `connect_to_net` calls. Same guarantees as `connect_pins`.

- **`set_schematic_component_properties`** (plural) — Set multiple properties on multiple components
  in one call via a `{ref: {prop: value}}` map. The singular `set_schematic_component_property` also
  received a proper schema (was skeleton-only; its parameters were not advertised to the model).

---

### Bug Fixes

- **Schematic symbol lookup**: `get_schematic_component`,
  `edit_schematic_component`, `set_schematic_component_property`,
  `remove_schematic_component_property`, and `delete_schematic_component`
  no longer fail with `Component '<ref>' not found in schematic` when the
  placed symbol uses KiCad's rescued / locally-customised serialisation
  form `(symbol (lib_name "...") (lib_id "...") ...)`. The block-matching
  regex now accepts any opening paren after `(symbol`, and the
  parent-position lookup uses the first `(at ...)` inside the symbol
  block, so newly-added properties anchor to the symbol origin instead of
  silently falling back to `(0, 0)`. Added 7 regression tests reproducing
  the failure on a real-world user schematic.

### New MCP Tools

- `set_schematic_component_property` — Add or update a single custom property
  (BOM / sourcing field) on a placed schematic symbol. Convenience wrapper
  around `edit_schematic_component` for the common case of attaching one MPN /
  Manufacturer / DigiKey_PN / LCSC / JLCPCB_PN / Voltage / Tolerance /
  Dielectric value at a time. Newly created properties default to hidden so
  they do not clutter the schematic canvas.

- `remove_schematic_component_property` — Delete a custom property from a
  placed schematic symbol. The four built-in fields (Reference, Value,
  Footprint, Datasheet) are protected and cannot be removed; clear them by
  setting their value to `""` via `edit_schematic_component` instead.

### Tool Enhancements

- `edit_schematic_component`: extended with two new optional parameters that
  promote arbitrary custom properties to first-class citizens:
  - **`properties`** — map of property name to either a string value or a full
    spec object `{ value, x?, y?, angle?, hide?, fontSize? }`. Adds the
    property if it does not yet exist on the symbol, otherwise updates the
    existing value (and optionally its label position / visibility). Lets a
    single tool call attach an entire BOM / sourcing payload to a component:
    `properties: { MPN: "RC0603FR-0710KL", Manufacturer: "Yageo", Tolerance: "1%" }`.
  - **`removeProperties`** — list of custom property names to delete in the
    same call.
  - String values written through any of the property paths are now properly
    backslash-escaped so descriptions containing `"` or `\` no longer
    corrupt the .kicad_sch file.

- `get_schematic_component`: clarified description — it already returns every
  field on the symbol (built-in + custom). The tool description now spells
  this out explicitly so agents know they can use it to inspect MPN,
  Manufacturer, Distributor PN and other BOM fields without a separate call.

### New MCP Prompt

- `component_sourcing_properties` — Guides the LLM through attaching BOM and
  sourcing metadata (MPN, Manufacturer, distributor part numbers, parametric
  fields like Voltage / Tolerance / Dielectric) to schematic components. Lists
  the conventional property names recognised by downstream BOM tooling and the
  recommended call sequence (`list_schematic_components` →
  `get_schematic_component` → `set_schematic_component_property` /
  `edit_schematic_component`).

### Tests

- `tests/test_schematic_component_properties.py`: 32 new tests covering custom
  property add / update / remove (single + batched), full spec dicts, position
  defaults, `(hide yes)` defaulting, protected built-in field rejection,
  no-op removal, special-character escaping, UUID preservation, and the two
  new convenience tools.

### Removed

- `add_schematic_junction` MCP tool has been removed. Junctions are now
  inserted and removed automatically via `WireManager.sync_junctions` whenever
  wires are added, deleted, or moved.
- Junction placement is pin-aware: `sync_junctions` consults component pin
  positions so that T-junctions at component pins are correctly recognised.

---

## [2.2.3] - 2026-03-11

### Merged: PR #57 (Kletternaut/demo/rpiCSI-videotest → main)

This release incorporates 28 commits developed and live-tested during a full
Raspberry Pi CSI adapter PCB design session. All tools listed below were validated
end-to-end using Claude Desktop + KiCAD 9 on Windows.

### New MCP Tools

- `connect_passthrough` — Schematic-only tool that wires all pins of one connector
  directly to the matching pins of another (e.g. J1 pin N → J2 pin N). Creates nets
  named with a configurable prefix (`netPrefix`). Designed for FFC/ribbon cable
  passthrough adapters. **Schematic only — do not call for PCB routing.**

- `sync_schematic_to_board` — Imports all net/pad assignments from the schematic
  into the open PCB file. Required after `connect_passthrough` before routing can
  start. Returns `pads_assigned` count for verification.

- `snapshot_project` — Saves a named checkpoint of the entire project folder into a
  `snapshots/` subdirectory inside the project. Allows resuming from a known-good
  state without redoing earlier steps. Accepts `step`, `label`, and optional `prompt`
  parameters.

- `run_erc` — Runs KiCAD's Electrical Rules Check on the schematic and returns
  violations as structured JSON.

- `import_svg_logo` — Converts an SVG file to PCB silkscreen polygons and places
  them on a specified layer.

### Bug Fixes

- `route_pad_to_pad`: **Critical fix for B.Cu footprints in KiCAD 9.** `pad.GetLayerName()`
  always returned `F.Cu` for SMD pads on flipped footprints (KiCAD 9 SWIG bug).
  Fix: use `footprint.GetLayer()` instead, which correctly reflects the placed layer
  after `Flip()`. Without this fix, no vias were inserted for back-to-back connectors.

- `route_pad_to_pad`: Via was placed at the geometric midpoint between the two pads.
  For back-to-back mirrored connectors (J1 F.Cu / J2 B.Cu) this caused all 15 vias
  to stack at the same X coordinate (board center). Fix: via is now placed at the
  X coordinate of the start pad (`via_x = start_pos.x`), producing 15 parallel
  vertical traces.

- `place_component` (B.Cu footprints): `Flip()` was called before `board.Add()`,
  causing KiCAD 9 to hang for ~30 seconds. Fix: `board.Add()` first, then `Flip()`.

- `add_board_outline`: Three separate bugs fixed — incorrect cornerRadius fallback,
  wrong top-left origin default, and broken arc delegation for IPC rounded rectangles.

- `snapshot_project`: Snapshots were saved one level above the project directory,
  cluttering the parent folder. Fix: snapshots now go into `<project>/snapshots/`.

- MCP server log timestamp was always UTC/ISO. Fix: now uses local system time.

- `search_tools` (router pattern): direct tools like `snapshot_project` were invisible
  to the router. Fix: direct tool names added to the router's known-tool list.

### Developer Mode (`KICAD_MCP_DEV=1`)

Set the environment variable `KICAD_MCP_DEV=1` in your Claude Desktop config to
enable developer features:

```json
"env": {
  "KICAD_MCP_DEV": "1"
}
```

**What it does:**

- `export_gerber` automatically copies the current MCP session log into the project's
  `logs/` subdirectory as `mcp_log_<timestamp>.txt`.
- `snapshot_project` copies the MCP session log into `logs/` at every checkpoint as
  `mcp_log_step<N>_<timestamp>.txt`.
- If a `prompt` parameter is passed to `snapshot_project`, it is saved as
  `PROMPT_step<N>_<timestamp>.md` alongside the log.

**Purpose:** Makes it easy to include the full tool call history when filing a bug
report or GitHub issue — just attach the log file from the project's `logs/` folder.

> ⚠️ **Privacy warning:** The MCP session log contains the **complete conversation
> history** between Claude and the MCP server, including all tool parameters and
> responses. When sharing a project directory (e.g. as a ZIP attachment in a GitHub
> issue), **review or delete the `logs/` folder first** to avoid accidentally
> disclosing sensitive file paths, component names, or design details.

### Snapshot Logging (always active)

Regardless of dev mode, `snapshot_project` now always saves a copy of the current
MCP session log into `<project>/logs/` at each checkpoint. This means every project
automatically retains a traceable record of which tools were called and in what order.

> ⚠️ **Same privacy note applies:** the `logs/` directory inside your project folder
> contains tool call history. Do not share it publicly without reviewing its contents.

---

## [2.2.2-alpha] - 2026-03-01

### New MCP Tools

- `route_pad_to_pad` – Convenience wrapper around `route_trace` that looks up pad positions
  automatically. Accepts `fromRef`/`fromPad`/`toRef`/`toPad` instead of raw XY coordinates.
  Auto-detects net from pad assignment (overridable via `net` param). Saves ~2 tool calls per
  connection (~64 calls for a full TMC2209 board compared to the 3-step get_pad_position flow).
  Live tested: ESP32 ↔ TMC2209 STEP/DIR traces routed without prior coordinate lookup. ✅

- `copy_routing_pattern` – Now registered as MCP tool in TypeScript layer (`routing.ts`).
  Was previously implemented in Python but missing from the MCP tool registry.
  Parameters: `sourceRefs`, `targetRefs`, `includeVias?`, `traceWidth?`.

### Bug Fixes

- `add_schematic_component` / `DynamicSymbolLoader`: ignored project-local `sym-lib-table`.
  `find_library_file()` only searched global KiCAD install directories, causing "library not
  found" errors for any symbol in a project-local `.kicad_sym` file. Fix: added `project_path`
  parameter; reads project `sym-lib-table` first via new `_resolve_library_from_table()` helper
  before falling back to global dirs. `project_path` is auto-derived from the schematic path.

- `place_component`: ignored project-local `fp-lib-table`. `FootprintLibraryManager` was
  initialised once at server start without a project path, so self-created `.kicad_mod`
  footprints were never found. Fix: new `boardPath` parameter in TypeScript + Python;
  `_handle_place_component` wrapper recreates `FootprintLibraryManager(project_path=…)` whenever
  the active project changes (cached to avoid redundant recreation).

- `copy_routing_pattern`: copied 0 traces when pads had no net assignments. The filter
  `track.GetNetname() in source_nets` always returned empty when pads were placed without net
  assignment. Fix: geometric fallback using bounding box of source footprint pads ±5mm
  tolerance. Response includes `filterMethod` field indicating which mode was used
  (`"net-based"` or `"geometric (pads have no nets)"`).

- `template_with_symbols.kicad_sch`, `template_with_symbols_expanded.kicad_sch`: restored
  format version `20250114` (KiCAD 9) after upstream commit `2b38796` accidentally downgraded
  both files to `20240101`. KiCAD 9 rejects schematics with outdated version numbers.

- **CRITICAL: `template_with_symbols_expanded.kicad_sch`**: removed 7 invalid `;;` comment
  lines introduced by upstream commit `b98c94b`. KiCAD's S-expression parser does not support
  any comment syntax — it expects every non-empty, non-whitespace line to start with `(`.
  The comments (`;; PASSIVES`, `;; SEMICONDUCTORS`, `;; INTEGRATED CIRCUITS`, `;; CONNECTORS`,
  `;; POWER/REGULATORS`, `;; MISC`, `;; TEMPLATE INSTANCES (...)`) caused KiCAD 9 to reject
  every schematic created from this template with a hard parse error:

  > `Expecting '(' in <file>.kicad_sch, line 8, offset 5`
  > **Action required for existing projects:** delete every line beginning with `;;` from any
  > `.kicad_sch` file created between upstream commit `b98c94b` and this fix.

- `add_schematic_component` / `inject_symbol_into_schematic`: symbol definition in
  `lib_symbols` was never refreshed after editing via `create_symbol` / `edit_symbol`.
  If the symbol was already present in the schematic's embedded `lib_symbols` section,
  the function returned immediately — `delete + re-add` still pulled in the stale cached
  definition. Fix: always read the current definition from the `.kicad_sym` file; if a
  stale entry exists in `lib_symbols`, remove it first, then inject the fresh one.
  Verified live. ✅

- `template_with_symbols_expanded.kicad_sch`: removed 13 legacy `_TEMPLATE_*` offscreen
  instances (`_TEMPLATE_R`, `_TEMPLATE_C`, `_TEMPLATE_U`, etc.) that were placed at
  `x=-100` as clone-sources for the old `ComponentManager` approach. `DynamicSymbolLoader`
  (the current implementation) injects symbols directly and never needs these placeholders.
  They appeared as dangling reference designators in KiCAD's component navigator and in
  the schematic canvas when zoomed far out.

### Maintenance

- `.gitignore`: added `*.kicad_pcb.bak`, `*.kicad_pro.bak` alongside existing `-bak` variants;
  consolidated personal/local files under `myContribution/`.

---

## [2.2.1-alpha] - 2026-02-28

### New MCP Tools

- `edit_schematic_component` – Update properties of a placed symbol in-place (footprint,
  value, reference rename). More efficient than delete + re-add: preserves position and UUID.

### Bug Fixes

- `add_schematic_component`: `footprint` parameter was accepted but silently ignored – the
  value was never passed through to `DynamicSymbolLoader.add_component()` /
  `create_component_instance()`. All newly placed symbols always had an empty Footprint
  field. Fix: added `footprint: str = ""` to both functions and threaded it through every
  call site including the TypeScript tool schema.

- `delete_schematic_component`: only deleted the first matching instance when duplicate
  references existed (e.g. after an aborted add attempt). Root cause: loop used `break`
  after the first match. Fix: collect all matching blocks first, then delete them all back-
  to-front (to preserve line indices). Response now includes `deleted_count`.

- `templates/*.kicad_sch`, `project.py`, `schematic.py`: Update KiCAD schematic format
  version from `20230121` (KiCAD 7) to `20250114` (KiCAD 9). The MCP server targets
  KiCAD 9 exclusively (`pcbnew.pyd` compiled for KiCAD 9.0, Python 3.11.5) – generating
  files in an outdated format caused a spurious "This file was created with an older
  KiCAD version" warning on every newly created schematic.

- `template_with_symbols_expanded.kicad_sch`: Remove 13 corrupt `_TEMPLATE_*` placed-symbol
  blocks with `(lib_id -100)` – an integer caused by old sexpdata serializer (same bug
  PR #40 fixed for the add path). KiCAD crashed with a null-pointer when selecting these
  symbols. They appeared as grey `_TEMPLATE_R?`, `_TEMPLATE_U_REG?` etc. labels far
  outside the sheet boundary (~5000mm off-sheet).

  **Discovered via:** live testing on a real JLCPCB/KiCAD 9 project.
  **Affected users:** schematics created from this template before this fix contain the
  same corrupt blocks – remove all `(symbol (lib_id -100) ...)` blocks whose Reference
  starts with `_TEMPLATE_`.

---

---

## [2.2.0-alpha] - 2026-02-27

### New MCP Tools (TypeScript layer – previously Python-only)

**Routing tools:**

- `delete_trace` - Delete traces by UUID, position or net name
- `query_traces` - Query/filter traces on the board
- `get_nets_list` - List all nets with net code and class
- `modify_trace` - Modify trace width or layer
- `create_netclass` - Create or update a net class
- `route_differential_pair` - Route a differential pair between two points
- `refill_zones` - Refill all copper zones ⚠️ SWIG segfault risk, prefer IPC/UI

**Component tools:**

- `get_component_pads` - Get all pad data for a component
- `get_component_list` - List all components on the board
- `get_pad_position` - Get absolute position of a specific pad
- `place_component_array` - Place components in a grid array
- `align_components` - Align components along an axis
- `duplicate_component` - Duplicate a component with offset

### Bug Fixes

- `routing.py`: Fix SwigPyObject UUID comparison (`str()` → `m_Uuid.AsString()`)
- `routing.py`: Fix SWIG iterator invalidation after `board.Remove()` by snapshotting `list(board.Tracks())`
- `routing.py`: Add `board.SetModified()` + `track = None` after `Remove()` to prevent dangling SWIG pointer crashes
- `routing.py`: Per-track `try/except` in `query_traces()` to skip invalid objects after bulk delete
- `routing.py`: Add missing return statement (mypy)
- `library.py`: Fix `search_footprints` parameter mapping (`search_term` → `pattern`)
- `library.py`: Fix field access (`fp.name` → `fp.full_name`)
- `library.py`: Accept both `pattern` and `search_term` parameter names
- `library.py`: Fix loop variable shadowing `Path` object (mypy)
- `design_rules.py`: Add type annotation for `violation_counts` (mypy)

### New MCP Tools (cont.)

**Datasheet tools:**

- `get_datasheet_url` - Return LCSC datasheet PDF URL and product page URL for a given
  LCSC number (e.g. `C179739` → `https://www.lcsc.com/datasheet/C179739.pdf`).
  No API key required – URL is constructed directly from the LCSC number.
- `enrich_datasheets` - Scan a `.kicad_sch` file and write LCSC datasheet URLs into
  every symbol that has an `LCSC` property but an empty `Datasheet` field. After
  enrichment the URL appears natively in KiCAD's symbol properties, footprint browser
  and any other tool that reads the standard KiCAD `Datasheet` field.
  Supports `dry_run=true` for preview without writing.
  Implementation: `python/commands/datasheet_manager.py` (text-based, no `skip` writes)

**Schematic tools:**

- `delete_schematic_component` - Remove a placed symbol from a `.kicad_sch` file by
  reference designator (e.g. `R1`, `U3`).

### Bug Fixes (cont.)

- `schematic.ts` / `kicad_interface.py`: Fix missing `delete_schematic_component` MCP tool.

  **Root cause (two separate issues):**
  1. No MCP tool named `delete_schematic_component` existed. Claude had no way to call
     it, so any "delete schematic component" request fell through to the PCB-only
     `delete_component` tool, which searches `pcbnew.BOARD` and always returned
     "Component not found" for schematic symbols.
  2. `component_schematic.py::remove_component()` still used `skip` for writes.
     PR #40 rewrote `DynamicSymbolLoader` (add path) to avoid `skip`-induced schematic
     corruption, but `remove_component` (delete path) was not touched by that PR.

  **Fix:**
  - Added `delete_schematic_component` to the TypeScript tool layer (`schematic.ts`)
    with clear docstring distinguishing it from the PCB `delete_component`.
  - Implemented `_handle_delete_schematic_component` in `kicad_interface.py` using
    direct text manipulation (parenthesis-depth tracking, same approach as PR #40).
    Does not call `component_schematic.py::remove_component()` at all.
  - Error message explicitly guides the user when the wrong tool is used:
    _"note: this tool removes schematic symbols, use delete_component for PCB footprints"_

### Additional Bug Fixes

- `connection_schematic.py` / `kicad_interface.py`: Fix `generate_netlist` missing
  `schematic_path` parameter – without it `get_net_connections` always fell back to
  proximity matching which only returns one connection per component (first wire hit,
  then `break`). PinLocator was never invoked. Fix: added `schematic_path: Optional[Path]`
  to `generate_netlist` signature and threaded it through to `get_net_connections`,
  and updated `_handle_generate_netlist` in `kicad_interface.py` to pass `schematic_path`.
- `server.ts`: Fix KiCAD bundled Python (3.11.5) not being selected on Windows – the
  detection condition `process.env.PYTHONPATH?.includes("KiCad")` was fragile and failed
  in some environments, causing System Python 3.12 to be used instead. Since `pcbnew.pyd`
  is compiled for KiCAD's Python 3.11.5, this resulted in `No module named 'pcbnew'`.
  Fix: removed the condition, KiCAD bundled Python is now always preferred on Windows
  when it exists at `C:\Program Files\KiCad\9.0\bin\python.exe`.
  Also added `KICAD_PYTHON` to `claude_desktop_config.json` as explicit override.
- `pin_locator.py`: Fix `generate_netlist` timeout – `get_pin_location` and
  `get_all_symbol_pins` called `Schematic(schematic_path)` on every single pin lookup,
  causing O(nets × components × pins) schematic file loads (e.g. 400+ loads for a
  medium schematic). Fix: added `_schematic_cache` dict to `PinLocator.__init__`,
  schematic is now loaded once per path and reused.

---

## [2.1.0-alpha] - 2026-01-10

### Phase 1: Intelligent Schematic Wiring System - Core Infrastructure

**Major Features:**

- Automatic pin location discovery with rotation support
- Smart wire routing (direct, orthogonal horizontal/vertical)
- Net label management (local, global, hierarchical)
- S-expression-based wire creation
- Professional right-angle routing

**New Components:**

- `python/commands/wire_manager.py` - S-expression wire creation engine
- `python/commands/pin_locator.py` - Intelligent pin discovery with rotation
- Updated `python/commands/connection_schematic.py` - High-level connection API
- `docs/SCHEMATIC_WIRING_PLAN.md` - Implementation roadmap

**MCP Tools Enhanced:**

- `add_schematic_wire` - Create wires with stroke customization
- `add_schematic_connection` - Auto-connect pins with routing options (NEW)
- `add_schematic_net_label` - Add labels with type and orientation control (NEW)
- `connect_to_net` - Connect pins to named nets (ENHANCED)

**Technical Implementation:**

- Rotation transformation matrix for pin coordinates
- S-expression injection for guaranteed format compliance
- Pin definition caching for performance
- Orthogonal path generation for professional schematics

**Testing:**

- End-to-end integration test: 100% passing
- MCP handler integration test: 100% passing
- Pin discovery with rotation: Verified working
- KiCad-skip verification: All wires/labels correctly formed

---

### Phase 2: Power Nets & Wire Connectivity - COMPLETE

**Major Features:**

- Power symbol support (VCC, GND, +3V3, +5V, etc.) via dynamic loading
- Wire graph analysis for net connectivity tracking
- Geometric wire tracing with tolerance-based point matching
- Accurate netlist generation with component/pin connections
- Critical template mapping bug fixes

**Updates:**

- `connect_to_net()` - Migrated to WireManager + PinLocator
- `get_net_connections()` - Complete rewrite with geometric wire tracing
- `generate_netlist()` - Now uses wire graph analysis for connectivity
- `get_or_create_template()` - Fixed special character handling, auto-reload after dynamic loading
- `add_component()` - Fixed template lookup with symbol iteration

**Bug Fixes:**

- CRITICAL: Template mapping after dynamic symbol loading
- Special character handling in symbol names (+ prefix in +3V3, +5V)
- Schematic reload synchronization after S-expression injection
- Multi-format template reference detection

**Wire Graph Analysis Algorithm:**

1. Find all labels matching target net name
2. Trace wires connected to label positions (point coincidence)
3. Collect all wire endpoints and polyline segments
4. Match component pins at wire connection points using PinLocator
5. Return accurate component/pin connection pairs

**Technical Implementation:**

- Tolerance-based point matching (0.5mm for grid alignment)
- Multi-segment wire (polyline) support
- Rotation-aware pin location matching via PinLocator
- Fallback proximity detection (10mm threshold)
- Template existence checking via symbol iteration (handles special characters)

**Testing:**

- Power symbols: 4/4 loaded (VCC, GND, +3V3, +5V)
- Components: 4/4 placed
- Connections: 8/8 created successfully
- Net connectivity: 100% accurate (VCC: 2, GND: 4, +3V3: 1, +5V: 1)
- Netlist generation: 4 nets with accurate connections
- Comprehensive integration test: 100% PASSING

**Commits:**

- `c67f400` - Updated connect_to_net to use WireManager
- `b77f008` - Fixed template mapping bug (critical)
- `a5a542b` - Implemented wire graph analysis

**Addresses:**

- Issue #26 - Schematic workflow wiring functionality (Phase 2)

---

### Phase 2: JLCPCB Integration Complete

**Major Features:**

- ✅ Complete JLCPCB parts integration via JLCSearch public API
- ✅ Access to ~100k JLCPCB parts catalog
- ✅ Real-time stock and pricing data
- ✅ Parametric component search
- ✅ Cost optimization (Basic vs Extended library)
- ✅ KiCad footprint mapping
- ✅ Alternative part suggestions

**New Components:**

- `python/commands/jlcsearch.py` - JLCSearch API client (no auth required)
- `python/commands/jlcpcb_parts.py` - Enhanced with `import_jlcsearch_parts()`
- `docs/JLCPCB_INTEGRATION.md` - Comprehensive integration guide

**MCP Tools Available:**

- `download_jlcpcb_database` - Download full parts catalog
- `search_jlcpcb_parts` - Parametric search with filters
- `get_jlcpcb_part` - Part details + footprint suggestions
- `get_jlcpcb_database_stats` - Database statistics
- `suggest_jlcpcb_alternatives` - Find similar/cheaper parts

**Technical Improvements:**

- SQLite database with full-text search (FTS5)
- Package-to-footprint mapping for standard SMD packages
- Price comparison and cost optimization algorithms
- HMAC-SHA256 authentication support (for official JLCPCB API)

**Testing:**

- All integration tests passing
- Database operations validated
- Live API connectivity confirmed
- End-to-end MCP tool testing complete

**Documentation:**

- Complete API reference with examples
- Package mapping tables (0402, 0603, 0805, SOT-23, etc.)
- Best practices guide
- Troubleshooting section

---

## [2.1.0-alpha] - 2025-11-30

### Phase 1: Schematic Workflow Fix

**Critical Bug Fix:**

- ✅ Fixed completely broken schematic workflow (Issue #26)
- Created template-based symbol cloning approach
- All schematic tests now passing

**Root Cause:**

- kicad-skip library limitation: cannot create symbols from scratch, only clone existing ones

**Solution:**

- Template schematic with cloneable R, C, LED symbols
- Updated `create_project` to create both PCB and schematic
- Rewrote `add_schematic_component` to use `clone()` API
- Proper UUID generation and position setting

**Files Modified:**

- `python/commands/project.py` - Now creates schematic files
- `python/commands/schematic.py` - Uses template approach
- `python/commands/component_schematic.py` - Complete rewrite

**Files Created:**

- `python/templates/template_with_symbols.kicad_sch`
- `python/templates/empty.kicad_sch`
- `docs/SCHEMATIC_WORKFLOW_FIX.md`

**Testing:**

- Created comprehensive test suite
- All 7 tests passing
- KiCad CLI validation successful

---

## [2.0.0-alpha] - 2025-11-05

### Router Pattern & Tool Organization

**Major Architecture Change:**

- Implemented tool router pattern (70% context reduction)
- 12 direct tools, 47 routed tools in 7 categories
- Smart tool discovery system

**New Router Tools:**

- `list_tool_categories` - Browse available categories
- `get_category_tools` - View tools in category
- `search_tools` - Find tools by keyword
- `execute_tool` - Run any routed tool

**Benefits:**

- Dramatically reduced AI context usage
- Maintained full functionality (64 tools)
- Improved tool discoverability
- Better organization for users

---

## [2.0.0-alpha] - 2025-11-01

### IPC Backend Integration

**Experimental Feature:**

- KiCad 9.0 IPC API integration for real-time UI sync
- Changes appear immediately in KiCad (no manual reload)
- Hybrid backend: IPC + SWIG fallback
- 20+ commands with IPC support

**Implementation:**

- Routing operations (interactive push-and-shove)
- Component placement and modification
- Zone operations and fills
- DRC and verification

**Status:**

- Under active development
- Enable via KiCad: Preferences > Plugins > Enable IPC API Server
- Automatic fallback to SWIG when IPC unavailable

---

## [2.0.0-alpha] - 2025-10-26

### Initial JLCPCB Integration (Local Libraries)

**Features:**

- Local JLCPCB symbol library search
- Integration with KiCad Plugin and Content Manager
- Search by LCSC part number, manufacturer, description

**Credit:**

- Contributed by [@l3wi](https://github.com/l3wi)

**Components:**

- `python/commands/symbol_library.py`
- Basic library search functionality

---

## [1.0.0] - 2025-10-01

### Initial Release

**Core Features:**

- 64 fully-documented MCP tools
- JSON Schema validation for all tools
- 8 dynamic resources for project state
- Cross-platform support (Linux, Windows, macOS)
- Comprehensive error handling
- Detailed logging

**Tool Categories:**

- Project Management (4 tools)
- Board Operations (9 tools)
- Component Management (8 tools)
- Routing (6 tools)
- Export & Manufacturing (5 tools)
- Design Rule Checking (4 tools)
- Schematic Operations (6 tools)
- Symbol Library (3 tools)
- JLCPCB Integration (5 tools)

**Platform Support:**

- Linux (KiCad 7.x, 8.x, 9.x)
- Windows (KiCad 9.x)
- macOS (KiCad 9.x)

**Documentation:**

- Complete README with setup instructions
- Platform-specific guides
- Tool reference documentation
- Contributing guidelines

---

## Version Numbering

- **2.1.0-alpha**: Current development version with JLCPCB integration
- **2.0.0-alpha**: Router pattern and IPC backend
- **1.0.0**: Initial stable release

## Breaking Changes

### 2.1.0-alpha

- None (additive changes only)

### 2.0.0-alpha

- Tool execution now requires router for 47 tools
- Direct tool access limited to 12 high-frequency tools
- Schema validation stricter (catches errors earlier)

## Deprecations

### 2.1.0-alpha

- `docs/JLCPCB_USAGE_GUIDE.md` - Superseded by `docs/JLCPCB_INTEGRATION.md`
- `docs/JLCPCB_INTEGRATION_PLAN.md` - Implementation complete

## Migration Guide

### Upgrading to 2.1.0-alpha from 2.0.0-alpha

**New Dependencies:**

- No new system dependencies
- Python packages: `requests` (already in requirements.txt)

**Database Setup:**

1. Run `download_jlcpcb_database` tool (one-time, ~5-10 minutes)
2. Database created at `data/jlcpcb_parts.db`
3. Subsequent searches use local database (instant)

**API Changes:**

- All existing tools remain compatible
- 5 new JLCPCB tools available
- No breaking changes to existing functionality

### Upgrading to 2.0.0-alpha from 1.0.0

**Router Pattern:**

- Some tools now accessed via `execute_tool` instead of direct calls
- Use `list_tool_categories` to discover available tools
- Search with `search_tools` to find specific functionality

**IPC Backend (Optional):**

- Enable in KiCad: Preferences > Plugins > Enable IPC API Server
- Set `KICAD_BACKEND=ipc` environment variable
- Falls back to SWIG if unavailable

---

## Credits

- **JLCSearch API**: [@tscircuit](https://github.com/tscircuit/jlcsearch)
- **JLCParts Database**: [@yaqwsx](https://github.com/yaqwsx/jlcparts)
- **Local JLCPCB Search**: [@l3wi](https://github.com/l3wi)
- **KiCad**: KiCad Development Team
- **MCP Protocol**: Anthropic

## License

See LICENSE file for details.
