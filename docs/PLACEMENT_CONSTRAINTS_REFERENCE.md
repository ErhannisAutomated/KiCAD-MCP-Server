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

`Pin_Spring_Class:<padNum>` is read as a property on the **PCB
footprint** (after `sync_schematic_to_board`). The intent lives on
the schematic component first — but KiCad's schematic→PCB sync does
not propagate arbitrary custom properties through by default, so for
the moment you set the property on the footprint after sync. Task
#228 tracks teaching `sync_schematic_to_board` to copy
`Pin_Spring_Class:*` (and `Placement_Anchor`) across.

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

**Bare class name** — applies to every connection from this pad:

```
Pin_Spring_Class:1 = DECOUPLING
```

**Per-target JSON** — different springs to different neighbours:

```
Pin_Spring_Class:4 = {"*": "LOCAL_SIGNAL", "U1.4": "DECOUPLING", "J1.3": "INTER_GROUP"}
```

The `"*"` key is the pad-general fallback. `"REF.PIN"` keys override
for a specific neighbouring pin. Useful when one pin serves multiple
roles (e.g. a buck-boost's VCC pin connects both to a tightly-coupled
bypass cap and to a remote enable line).

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

A separate rule overrides this whole chain: connections on **power
nets** (anything matching `GND`, `BAT+`, `V12_*`, `PWR_*`, etc.) are
forced to `PLANE` regardless of any annotation. You almost never want
the auto-placer pulling components together along the GND net.

## Suggested workflow

1. **First pass — annotate the obvious**: every bypass / decoupling
   cap. The schematic's comment text usually flags them ("100nF bypass
   for U3", "1 µF VCC decoupling near Q2", etc.).

   ```
   Set Pin_Spring_Class:1 on C29 to DECOUPLING.
   Set Pin_Spring_Class:1 on C30 to DECOUPLING.
   Set Pin_Spring_Class:1 on C3 to DECOUPLING.
   Set Pin_Spring_Class:1 on C4 to DECOUPLING.
   ```

2. **Second pass — flag known stragglers**: cross-board signals that
   the auto-placer might otherwise pull awkwardly:

   ```
   Set Pin_Spring_Class:1 on D1 to INTER_GROUP.
   Set Pin_Spring_Class:1 on J3 to INTER_GROUP.
   ```

3. **Run `relax_placement`** and visually inspect. The engine reports
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
