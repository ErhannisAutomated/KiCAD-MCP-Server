# Placement Constraints Reference

The auto-placer (`relax_placement`) is a force-directed engine: pin-to-
pin springs pull connected components together, body-body repulsion
pushes overlapping footprints apart, and a per-pad **spring class**
controls how strong the per-pin spring is.

Most pins resolve to a sensible default and need no annotation. This
reference covers the cases where the default produces a layout you'd
have to fix by hand afterwards — bypass caps that should sit right on
their IC pin, long-distance signals that should pull only weakly, etc.

## Where constraints live

`Pin_Spring_Class:<padNum>` is a property authored on the **schematic
symbol**. The auto-placer reads from the schematic at session-load
time (matching PCB footprints by Reference), so the schematic is
the single source of truth — no PCB-side sync needed, no drift
risk (#228).

The property name carries the *pad number* as a suffix, not the pad
*name*: `Pin_Spring_Class:1`, `Pin_Spring_Class:2`, etc. This
matches how KiCad numbers pads internally and lets a single component
hold per-pin overrides.

## Class table

| Class           | Spring constant | When to use                                                                                                                  |
| --------------- | --------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `DECOUPLING`    | 5.0 (strong)    | Bypass / decoupling caps that must sit directly next to their IC pin. The engine parks them at <1 mm.                         |
| `LOCAL_SIGNAL`  | 1.0 (default)   | Standard signal connections — engine picks a reasonable distance based on neighbouring repulsion.                              |
| `INTER_GROUP`   | 0.3 (weak)      | Cross-group connections you don't want pulling components toward each other (status LEDs, debug headers, far-side connectors). |
| `PLANE`         | 0.0 (none)      | Connections through a copper pour. Applied automatically to power nets (GND, BAT+, V12_OUT, etc.); rarely set manually.        |

## Value grammar

A spring class answers "how hard should this pad be pulled toward the
thing it connects to?" — and *the thing it connects to* is usually one
specific pin, not "everything on the net." So the per-target form is
the normal way to express decoupling; the bare form is the special
case.

**Per-target JSON (recommended for decoupling)** — strength keyed by
the neighbour pin:

```
Pin_Spring_Class:1 = {"U3.4": "DECOUPLING"}
```

This pulls the pad hard toward **U3 pin 4 only**. Every other
connection from the pad (to the bulk cap, the regulator, sibling
bypass caps on the same rail…) falls through to its own default —
exactly what you want, because a decoupling cap should sit on *its*
IC pin, not get tugged toward everything else sharing the rail.

Keys are `REF.PIN` with a **dot** (`U3.4`, not `U3:4`). A mistyped
colon key silently never matches and the override is lost.

Add a `"*"` key only when you want a non-default fallback for the
pad's *other* connections — e.g. a pin that serves multiple roles:

```
Pin_Spring_Class:4 = {"*": "LOCAL_SIGNAL", "U1.4": "DECOUPLING", "J1.3": "INTER_GROUP"}
```

**Bare class name** — applies to *every* connection from the pad:

```
Pin_Spring_Class:1 = DECOUPLING
```

Use this only when the pad genuinely has a single role — e.g. a
two-pin cap whose power pad connects to exactly one IC pin and nothing
else. On a shared rail, a bare `DECOUPLING` pulls the cap toward every
neighbour on the net at once (a tug-of-war), and — because pad-level
annotations outrank the net-level `PLANE` default (see Resolution
order) — it even overrides the power-plane exclusion. Prefer the
per-target form unless you're sure the pad has one neighbour.

Malformed JSON or non-string values are logged and ignored — the
engine continues with defaults rather than failing the run.

## Resolution order

When the engine needs the spring class for a `(padA, padB)` pair, it
consults sources in this order:

1. `Pin_Spring_Class:<padA>` JSON value with key `"<refB>.<padNumB>"`
2. `Pin_Spring_Class:<padB>` JSON value with key `"<refA>.<padNumA>"`
3. `Pin_Spring_Class:<padA>` bare-string value (pad-general)
4. `Pin_Spring_Class:<padB>` bare-string value
5. Component-level `Spring_Class` property (rare; pad-A's component, then pad-B's)
6. Engine default: `LOCAL_SIGNAL`

Connections on **power nets** (anything matching `GND`, `BAT+`,
`V12_*`, `PWR_*`, etc.) are auto-assigned `PLANE` — but as a
*net-level* default (step 3 above), **not** an override of the whole
chain. A pad-level annotation (steps 1–2) still wins. That matters in
practice: a bare `DECOUPLING` on a cap pad that sits on a power rail
will beat the rail's `PLANE` and pull the cap along the net — usually
not what you want. The per-target form sidesteps this: only the named
IC pin gets the strong pull, and the rest of the rail stays on
`PLANE`. You almost never want the auto-placer pulling components
together along the GND net.

## Suggested workflow

1. **First pass — annotate the obvious**: every bypass / decoupling
   cap. The schematic's comment text usually flags them ("100nF bypass
   for U3 pin 4", "1 µF VCC decoupling near Q2", etc.) — and that
   comment names the pin the cap is *for*. Encode that pin in the value
   so the strong pull lands only on it. Set the property on the
   schematic component (not the PCB footprint):

   ```
   Set Pin_Spring_Class:1 on C29 to {"U3.4": "DECOUPLING"}.
   Set Pin_Spring_Class:1 on C30 to {"U3.13": "DECOUPLING"}.
   Set Pin_Spring_Class:1 on C3 to {"Q2.3": "DECOUPLING"}.
   ```

   (Reach for the bare `Pin_Spring_Class:1 = DECOUPLING` form only when
   the cap pad connects to exactly one pin and nothing else.)

2. **Second pass — flag known stragglers**: cross-board signals that
   the auto-placer might otherwise pull awkwardly:

   ```
   Set Pin_Spring_Class:1 on D1 to INTER_GROUP.
   Set Pin_Spring_Class:1 on J3 to INTER_GROUP.
   ```

3. **Run `relax_placement`** and visually inspect. The engine reads
   the schematic at load time, picks up your annotations, and reports
   per-class kinetic energy in its result; high `DECOUPLING` energy
   means a cap couldn't reach its target pin (usually a missing or
   wrong-numbered annotation, or a body-repulsion collision blocking
   the path).

## Related properties

- **`Spring_Class`** (component-level, fallback) — applies a default
  to every pin on a component. Rare; mostly useful when you've got a
  small array of identical decoupling caps.
- **`Body_Margin`** (component-level) — extra mm to add to the
  footprint's bbox during repulsion. Used to keep tall parts (relays,
  transformers) breathing room.
- **`Placement_Anchor`** (component-level, design memo only) —
  describes hard placement requirements (lock to a specific
  reference, within N mm of a pad). Implementation is partial; see
  `feedback_placement_constraints_design` memory + the design memo.
- **`Pin_Spring_Class:*`** is per-pin and overrides `Spring_Class`.

## Visual debugging

`pcb_autoplacer_viz` (in `relax_placement` when `viz=true`) colour-codes
springs by class:

- `DECOUPLING` — bright red (strong)
- `LOCAL_SIGNAL` — blue-gray (default)
- `INTER_GROUP` — darker blue (weak)
- `PLANE` — invisible (no force)

The colours make it easy to spot caps that *should* be DECOUPLING but
got left at the default — they'll show up as blue-gray springs
stretched out to where the auto-placer parked the cap.
