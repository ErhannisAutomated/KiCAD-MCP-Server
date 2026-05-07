# Schematic Autorouter — Design Plan

Status: **Phase 2 SHIPPED (2026-05-07).**
- `schematic_router.py` now generates straight, L-shape (1 bend), and
  U-shape (2 bend) candidates. The first candidate that passes
  `check_spurious_connections`, `max_len`, and `max_bends` wins.
- `RouteResult.style` is now one of `"straight"`, `"L"`, or `"U"`.
- `max_bends` is enforced (Phase 1 ignored it). Bumping below 1 or 2
  reverts to straight-only / no L+U.
- New helpers: `_l_shape_candidates`, `_u_shape_candidates`. Both filter
  by pin outward angle so the polyline always exits each pin along its
  outward direction. U-shape uses a 5.08 mm side-trip offset when both
  pins face the same way.
- Tests: `tests/test_schematic_router.py` (85 cases). Coverage includes
  L/U candidate generators, L-shape route, U-V-H-V route, U-H-V-H route,
  multi-segment polyline written to disk via `connect_pins`, and the
  `max_bends=0` short-circuit.
- **Restart the MCP server** to pick up Phase 2.

Phase 1 (shipped 2026-05-06): straight-line + spurious-connection guard +
`connect_pins(style=...)`. See git history for details.

> **Pre-req that broke during Phase 1 work:** `PinLocator.get_pin_angle` had a
> rotation-sign bug that was masked by rgb_switches being 100% rot=0. The
> original session's `+180` fix worked for rot=0 only; for rot=90 and rot=270
> it produced 180°-wrong stubs (going INWARD into the symbol body). Phase 1
> caught this because the router's `_collinear_and_facing` check uses pin
> angles from `get_pin_angle`. Fix is in `pin_locator.py`:
> `(pin_def_angle + rotation + 180) % 360` (was `- rotation`). Test
> `tests/test_get_pin_angle.py` updated — its previous "expected" reference
> was also computing INWARD instead of OUTWARD, masking the bug.

## Motivation

`connect_to_net` is currently the only practical way to wire pins together. It
adds a 2.54 mm wire stub at the pin and places a net label at the stub end. This
is robust (idempotent, hard to short to the wrong pin) but produces schematics
where every pin has a label and there are no visible "wires" at all. For small
designs that's harder for a human reviewer to read than a traditional KiCad
schematic with explicit polylines connecting nearby pins.

The goal of the autorouter is to draw real wire segments between pins when the
geometry is friendly, and fall back to net labels when it isn't — without ever
introducing an unintended electrical connection.

## API surface

Add one new MCP tool, leave existing tools untouched.

```
connect_pins(
  schematicPath,
  pairs:    [(ref, pinNumber), ...],     # 2 or more pins, all on the same net
  netName:  str,                          # required — used for label fallback
  style:    "auto" | "wire" | "label",   # default "auto"
  maxLen:   float = 80.0,                 # mm; longer paths fall back to label
  maxBends: int = 4,                      # corners allowed in a wire path
)
```

Behavior per `style`:

- **wire**: try to draw a polyline between each consecutive pair of pins. If
  routing fails for any segment, return the failure — DO NOT silently fall back.
- **label**: behave exactly like the current `connect_to_net` for each pin
  (stub + label).
- **auto** (default): for each consecutive pair, attempt `wire`; on failure or
  if the path is over `maxLen` / `maxBends`, drop to `label` for the failing
  segment(s). Mixed result is fine — a single net can have some pin-pairs
  connected by wires and others by labels.

`connect_to_net` stays as the always-label primitive. `connect_pins` is the
mixed-mode tool.

## Routing engine — overview

For each pin pair `(p1, p2)` on the same intended net:

1. **Get exact pin endpoints** via `PinLocator.get_pin_location` (already snapped
   to the schematic grid).
2. **Get outward angles** via `get_pin_angle` (now returns true outward direction
   after the 2026-05-06 fix). Each pin must be approached from its outward
   direction — entering a pin from its inward direction means the wire would
   pass *through* the symbol body.
3. **Build the obstacle map** (see below).
4. **Run an A\* / orthogonal-router search** over the 1.27 mm grid, starting at
   `p1` exiting along its outward angle, ending at `p2` entering along its
   outward angle.
5. **Validate the candidate path** against the spurious-connection rules
   (see below). If it fails validation, treat as no path found.
6. **Emit wire segments** as `(wire (pts (xy x1 y1) (xy x2 y2)) ...)` records.

## Obstacle map

Two layers:

1. **Symbol bodies** — for each placed symbol, compute its bounding box from the
   library symbol's graphics + the schematic-side rotation/mirror/position.
   The router cannot enter a body's interior except along a pin's path
   (the short stretch from the pin endpoint into the body).

2. **Existing wires of *other* nets** — every wire segment that does not belong
   to the same net as the pair being routed is an obstacle. Wires of the *same*
   net are not obstacles (a tee-junction is a valid connection).

   Determining "same net" requires walking the existing wire/label graph. We
   already have most of this in `wire_connectivity.py`'s connected-component
   logic; reuse it.

The router can cross same-net wires (creating a junction is fine, though we
should explicitly add a `(junction (at x y))` record at the crossover point so
KiCad renders the dot). The router can cross other-net wires *only* when the
existing wire is deemed acceptable to cross — and crossings are always allowed
in KiCad as long as there is no junction. So crossings of unrelated-net wires
are *visually* allowed but **must not** introduce a junction.

The user explicitly noted that "sometimes wires crossing is clearer than
labels" — so do not penalise crossings heavily. Penalise corners much more.

## Spurious-connection guard

This is the single most important correctness check. After the router has a
candidate path, before writing it:

1. **No segment endpoint or interior coincides with an unrelated pin endpoint.**
   For every pin endpoint `p_other` of every component (excluding the two we're
   intentionally connecting), assert `p_other` does not lie on any of our new
   segments. KiCad merges any two wires sharing an endpoint, and any pin endpoint
   that lands on a wire interior creates an automatic connection.

2. **No segment endpoint or interior coincides with an unrelated-net label.**
   Same check, against existing labels.

3. **No segment endpoint coincides with an unrelated-net wire endpoint** (would
   cause an unintended T-junction).

4. **Segment crossings of unrelated-net wires must not occur at an existing
   junction or label** (would change the existing net's topology).

If any of these fire, throw out the candidate and try the next A\* path. After
N attempts, give up on `wire` and fall back to `label`.

The validator should produce a clear diagnostic when it rejects a path — useful
for the agent to understand why the autorouter declined a particular pair.

## A\* cost model

Tunable, but a starting point:

- Straight step: 1.0
- Corner (turn): 5.0  (corners cost much more than length)
- Crossing an unrelated-net wire: 3.0
- Length over `maxLen`: hard reject

Penalise corners over crossings because the user has indicated visual clarity
is sometimes better with a crossing than with extra zig-zag. `maxBends` is a
hard cap; over it → reject.

## Special cases

- **Power and ground** (`VBUS`, `GND`, `+3V3`, etc.): default to `label` even in
  `auto` mode. These are usually best as labels (or proper power symbols)
  because they fan out widely. Configurable via a small builtin power-net list
  + a `treatAsPower` parameter.

- **Two pins on the same horizontal/vertical line, no obstacles**: shortcut to
  a single straight segment. Don't run A\* for the trivial case.

- **L-shape with one bend**: also a shortcut. Try both bend orderings (vertical-
  first vs. horizontal-first) and pick the one without obstacle hits.

## Integration with existing code

- `PinLocator.get_pin_angle` (fixed 2026-05-06 to return outward direction).
- `WireManager.add_wire` (already exists, takes two endpoints).
- `wire_connectivity.py` for net-membership of existing wires/labels.
- `connection_schematic.py` is where the new `connect_pins` lives next to
  `connect_to_net`.

The router itself should be its own module — `python/commands/schematic_router.py`
— with a small public interface (route_one, route_pairs) that the
`connect_pins` handler calls.

## Phased implementation

**Phase 1 — straight-line shortcut** ✅ DONE 2026-05-06:
- `connect_pins(style='auto'|'wire'|'label')` with the trivial collinear-
  and-facing straight-line case wired up.
- Falls back to per-pin `connect_to_net`-style label in `auto` mode for
  anything non-trivial; `wire` mode hard-fails instead.
- `WireManager.add_wire` already calls `sync_junctions()` so multi-pair
  routings produce KiCAD junction dots automatically.
- Spurious-connection guard (`check_spurious_connections`) ships from day
  one — 4 rules: no other-pin on segment, no other-net label on segment,
  no existing wire endpoint on segment interior (no T-junction), no
  collinear overlap with existing wire. Re-collects the obstacle map after
  each successful pair so subsequent pairs see the new wire as an obstacle.
- Power nets default to label even in `auto` (built-in list: VBUS, GND,
  +3V3, +5V, etc.; rail patterns like `+1V8` and `-12V` matched by prefix;
  caller can extend via `powerNets`).

**Phase 2 — L-shape and U-shape paths** ✅ DONE 2026-05-07:
- One-bend L candidates (corner at (x2,y1) or (x1,y2)) emitted only when
  consistent with both pin outward angles.
- Two-bend U candidates: H-V-H bridge for horizontal pins (xm chosen by
  midpoint when pins face each other, or `±_U_OFFSET` past both pins when
  pins face the same way); V-H-V bridge for vertical pins, mirrored.
- `route_pair` tries straight → L → U in order; first valid candidate wins.
- `max_bends` is enforced as a hard cap. `max_bends=0` rejects all L/U;
  `max_bends=1` allows only straight + L; default `max_bends=4` permits
  everything Phase 2 can produce.
- Still no full A\*. Most short pairs that aren't blocked by symbol
  obstacles will route with these shapes.

**Phase 3 — A\* with obstacle avoidance** (~1–2 days):
- Full grid search.
- Symbol-bbox obstacles.
- Other-net wire obstacles.
- Crossing penalty, corner penalty.

**Phase 4 — agent feedback loop** (~few hours):
- `connect_pins` returns enough diagnostic info for the agent to retry with
  hints (e.g. "force this segment through point (X, Y)" or "use a label
  instead").
- Optional `viaPoints` param to manually thread paths.

Phases 1 and 2 are usable on their own — most simple test boards (like
rgb_switches) would be readable just from straight + L paths.

## Open questions for next session

1. Should `connect_pins` accept *more* than two pins per net in one call?
   E.g. `pairs=[("R1","2"), ("D1","1"), ("D2","1")]` would route a 3-way net.
   Probably yes, but A\* on multi-terminal nets is more complex (Steiner tree).
   For phase 1/2, treat it as N-1 independent pair routings between adjacent
   pins in the list.

2. How aggressive is the corner penalty? User suggested raising the cap from 2
   to 4 — do that, but keep the per-corner *cost* high enough that A\* still
   prefers fewer corners.

3. Should the router move components when wiring fails badly? Probably no —
   placement is the agent's job, the router only routes.

4. Failure surface: when `auto` falls back to label for some pin-pairs, the
   response should make that explicit so the agent knows the schematic
   contains a mix.
