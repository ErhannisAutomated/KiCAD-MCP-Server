# Autoplacer Guide

The schematic autoplacer is a force-directed component placer that
operates on a `.kicad_sch` file in three phases:

1. **Load** — parse the file into an in-memory Session (components,
   nets, no_connect markers).  No file mutation yet.
2. **Iterate** — run force-directed iterations, advancing the Session
   state.  Either driven by hand (`autoplacer_iterate`) or via the
   built-in `autoplacer_recipe` schedule.
3. **Apply** — snap positions to grid, center on page, strip the
   existing wiring + labels, write the new positions back, and
   re-route every net via `connect_pins(auto)`.

The MCP-tool surface lives in `commands/autoplacer.py`; the
matplotlib viz hook is in `commands/autoplacer_viz.py`.

## Recommended workflow: the recipe

Use `autoplacer_recipe` (or `PLACER.recipe(path, **kw)` from Python)
unless you have a specific reason to drive the placer manually.  The
recipe runs a four-stage anneal that the experiments on the
power_module child sheets converged on:

```
autoplacer_load  → parse file
autoplacer_recipe → cluster → spread → polarize → settle  (330 iters default)
autoplacer_apply → snap + center + write + rewire
```

### What each stage does

| Stage     | Iterations (default) | Forces active                                                  | Purpose |
|-----------|----------------------|---------------------------------------------------------------|---------|
| CLUSTER   | 100                  | attraction, rotation                                          | Pull components into rough connection-based groupings.  No repulsion → groups collapse tight. |
| SPREAD    | 11 × 10 = 110        | attraction, rotation; repulsion ramps geometrically 0.05 → 51.2 | Fan clusters apart without losing the grouping established in CLUSTER. |
| POLARIZE  | 2 × 10 = 20          | attraction, rotation, **polarity** + **polarity_torque**; repulsion steps down 51.2 → 25.6 | Rotate V+/GND-attached pins toward their preferred orientation; migrate GND-coupled components down and V+ ones up. |
| SETTLE    | 100                  | inherits POLARIZE end state; no more temperature clamp        | Relax: temperature decays naturally via `cooling`, components settle into final positions. |

Throughout stages 1-3 the Session's temperature is clamped to
`step_temperature` (default 5 mm) before each iteration, so
displacement is controlled by force scheduling rather than the
default cooling-based anneal.  Stage 4 lets temperature decay.

### Tuning knobs (all optional)

Pass any of these via `autoplacer_recipe` params or `PLACER.recipe(**kw)`:

| Knob                 | Default | Effect |
|----------------------|---------|--------|
| `cluster_iters`      | 100     | Iterations in stage 1.  Increase if clusters aren't tight enough. |
| `spread_stages`      | 11      | Number of repulsion sub-stages.  Increase for finer ramp. |
| `polarize_stages`    | 2       | Number of repulsion-step-down sub-stages with polarity on. |
| `settle_iters`       | 100     | Iterations in stage 4.  Increase for tighter settling. |
| `iters_per_stage`    | 10      | Iterations per spread/polarize sub-stage. |
| `step_temperature`   | 5.0     | Per-iteration displacement cap (mm).  Larger → faster but less stable. |
| `base_attraction_k`  | 0.2     | Attraction force per net edge. |
| `base_rotation_k`    | 4.0     | Pin-orientation torque (snap pins toward their connected components). |
| `repulsion_base`     | 0.05    | Initial repulsion value at stage 2 start. |
| `repulsion_growth`   | 2.0     | Per-stage multiplier for repulsion in stage 2. |
| `polarity_k`         | 0.2     | Vertical bias on polarity nets (GND down, V+ up). |
| `polarity_torque_k`  | 3.0     | Rotation force on polarity-net pins toward their preferred angle. |

## Interactive Jupyter workflow

For tuning, drive the placer interactively from Jupyter with `%autoreload 2`
and the matplotlib viz:

```python
%matplotlib qt5
%load_ext autoreload
%autoreload 2

from commands.autoplacer import PLACER
from commands.autoplacer_viz import AutoplacerViz

target = "/tmp/work_kicad/work.kicad_sch"
PLACER.load(target)

viz = AutoplacerViz(PLACER.get(target))
PLACER.recipe(target, on_step=lambda s: viz.update())

PLACER.apply(target, rewire=True)
```

The `on_step` callback fires once per iteration with the Session, so
you can refresh the viz on every step.

### What the viz shows

- **Cyan dots**: pin endpoints in world coords.
- **Yellow dots**: polarity-net pins (GND-down, V+-up).
- **Red lines**: attraction forces between connected pins (top by
  magnitude).
- **Blue lines**: repulsion forces between component centres (top 30
  by magnitude).
- **Green arrows**: per-component net force sum.
- **Yellow arrows**: per-component polarity force.

Each layer is toggleable at construction (`show_polarity_force=False`,
`show_pin_labels=True`, etc.).  Under headless matplotlib (`Agg`
backend), `viz.save("/tmp/snap.png")` writes a PNG.

## Validation: did the placement preserve connectivity?

After `apply`, use `compare_netlists` to assert every pin's named-net
membership survived the place-and-route:

```
compare_netlists {origPath, newPath}
  → {preserved, missing_pins, extra_pins, net_mismatches: [{net, lost, added}]}
```

For visual triage of the routing output, `diagnose_chains` enumerates
the wire graph and flags pathological chains:

```
diagnose_chains {schematicPath, filterNets?}
  → {n_chains, chains: [...], flag_counts: {DUPLICATE_LABELS, CROSS_NET, LOOP}}
```

Both tools share the wire-graph BFS that `connect_pins` Phase 5 uses
(`walk_wire_chain`), so chain detection is consistent across the
codebase.

## When not to use the recipe

The recipe is opinionated.  Reach for manual tuning when:

- The recipe overshoots a particular sheet (`max_force` after stage 4
  is high → didn't settle).  Pull settings via `set_params`, then
  drive `iterate` directly.
- You want to *preserve* an existing manual layout and only optimise
  a subset of components.  The recipe always processes every
  unpinned component; pin the rest manually via `Component.pinned`.
- You're benchmarking force-constant variations across many sheets.
  The recipe rewrites params at each stage boundary, so direct
  `set_params` + `iterate` calls are easier to instrument.

## Known limitations

- **Multi-unit duplicate pads**: dual-FET source/drain pins (e.g.
  FDS9926A pins 5+6 at one source pad) sit at the same world coord.
  The placer treats each unit as its own node (so they can move
  independently in stage 1), but routing collapses to a single
  physical point.  `connect_pins` Phase 3 dedupes by coord so only
  one stub+label is emitted per coord, but the resulting wire graph
  may still flag `LOOP` if multiple wires share the merged endpoint.
- **(Fixed)** Cross-net merge defect (#74) — `WireManager.add_wire`
  now takes an optional `expected_net=` argument.  When set,
  `_break_wires_at_point` and `_existing_endpoints_on_segment` use
  `walk_wire_chain` to determine the existing wire's net before
  splitting; foreign-net splits are refused (the new wire then sits
  on the foreign wire's interior with no junction → KiCad treats
  it as not connected).  All `connect_pins` / `connect_to_net` /
  Phase 5 / `add_schematic_net_label` call sites pass
  `expected_net`.  Default `None` preserves the old net-blind
  behavior for callers that don't know the net.  Regression test
  in `tests/test_wire_manager_cross_net.py`.
- **Power nets aren't pulled together**.  Polarity bias still applies
  (GND-connected components migrate down, V+-connected up), but the
  spring attraction is off for power rails — they'd otherwise distort
  the layout without buying any routing.  Tunable via
  `Params.exclude_power_nets_from_attraction` and
  `Params.attraction_excluded_nets`.

## See also

- `docs/SCHEMATIC_TOOLS_REFERENCE.md` — full schematic tool catalogue.
- `docs/TOOL_INVENTORY.md` — high-level tool index with access modes.
- `commands/autoplacer.py` — source; module docstring covers the
  data model.
- `tests/test_autoplacer.py` — Force-math invariants, snap-positions
  multi-unit safety, the staged-anneal recipe smoke tests, etc.
