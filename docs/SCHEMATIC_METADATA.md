# Schematic Metadata Singleton

Project-wide MCP metadata (the things that used to live under
`mcp_*` keys in `.kicad_pro`) is stored on a **singleton symbol**
placed on the schematic. The user can view and edit each key
through eeschema's standard symbol-properties dialog — no special
tooling, no JSON editing.

## What the singleton looks like

A DNP symbol (`in_bom no`, `on_board no`) carrying a marker
property plus one property per metadata key:

```
Reference                      = META1
Value                          = mcp/v1
Schematic_Metadata_Marker      = mcp/v1     ← identifier (don't edit)
mcp_constraint_version         = 2
mcp_spring_classes             = {"classes":{"DECOUPLING":{"spring_k":5.0}},...}
mcp_expected_netclass_patterns = {"POWER_4A":["BAT+","BAT-",...]}
```

The reference (`META1`, `META2`, …) is auto-assigned but not
load-bearing — the marker property is what the tools look for, so
the singleton survives renames.

## Discovery rules

When tools read a metadata key:

1. **Find singletons** = every schematic symbol whose
   `Schematic_Metadata_Marker` property equals `mcp/v1`.
2. **0 singletons** → fall back to `.kicad_pro` (backwards
   compatibility for legacy projects), then to the tool's
   built-in default.
3. **1 singleton** → read normally.
4. **>1 singletons** → merge properties alphabetically by
   Reference, **last-wins** for duplicate keys. A warning is
   logged listing the offending references so the user can
   consolidate. (Multiple singletons shouldn't normally happen but
   may occur after a careless schematic copy/paste — the merge
   keeps the design working until the duplicates are cleaned up.)

## Writing

`write_metadata_key(sch_path, key, value)` always targets the
*first* singleton (alphabetical by Reference). If no singleton
exists, one is created on first call:

1. The library definition `mcp_meta:Schematic_Metadata` is appended
   to the schematic's `lib_symbols` section.
2. A symbol instance is placed at `(12.7, 12.7)` (top-left;
   user can drag it elsewhere).
3. The metadata key is added as a property on the new symbol.

Object/array values are JSON-encoded into the property string.
Primitive values (int, str, bool) are stored verbatim.

## Reserved properties

These names cannot be used as metadata keys (the tool returns an
error):

- `Reference`, `Value`, `Footprint`, `Datasheet`, `Description` —
  standard KiCad symbol properties
- `Schematic_Metadata_Marker` — the identifier itself

## Migration from `.kicad_pro`

Run once per legacy project:

```
Migrate the project's mcp_* metadata from .kicad_pro to the schematic
singleton.
```

Tool: `migrate_metadata_to_singleton`. Idempotent — running it on a
project that's already migrated is a no-op.

By default the migrated keys are also **removed from `.kicad_pro`**
so the singleton becomes the single source of truth. Pass
`removeFromPro: false` to keep both during a staged rollout (the
read fallback ensures consumers see the same value either way).

## Why a singleton instead of `.kicad_pro` keys

`.kicad_pro` keys work — KiCad round-trips unknown top-level keys
intact — but they're invisible in the eeschema UI. A
designer / reviewer reading the schematic in KiCad couldn't see
that `BAT+` was assigned to the `POWER_4A` netclass or that
`DECOUPLING` had `spring_k=5.0` without separately opening the
`.kicad_pro` file. The singleton puts that intent right in the
schematic where it's most useful.

The architectural cost is two-fold:

- The schematic carries a non-electrical "ghost" symbol the user
  must learn to leave alone. The marker property + a small
  graphic box mitigate confusion.
- KiCad's standard schematic→PCB sync ignores the singleton
  (because it's `on_board no`), so PCB-side consumers can't
  follow the schematic→PCB property chain. Today the helper
  reads from the schematic source directly; long term the
  `sync_schematic_to_board` tool (#228) can carry the singleton
  through if a use case arises.

## Where this lives in the code

`python/commands/schematic_metadata.py` — read/write/migrate.

Three public functions worth knowing:

- `read_metadata(sch_path) → Dict[str, str]` — merged metadata
  dict from all singletons. `{}` if no singleton.
- `read_metadata_with_pro_fallback(sch_path, pro_path, key)` —
  one-key lookup with `.kicad_pro` fallback. Used by consumers
  during the legacy-support phase.
- `write_metadata_key(sch_path, key, value)` — create or update.
- `migrate_from_pro(sch_path, pro_path)` — one-shot migration.
