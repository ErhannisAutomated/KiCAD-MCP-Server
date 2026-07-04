"""Interactive autoplacer tuning script — copy cells into a Jupyter/IPython
session.  Each block between `# %%` markers is a stand-alone cell (works
directly in VS Code's Jupyter mode, or paste into a Jupyter notebook cell).

Prereqs (one-off in the same env):
    pip install matplotlib
    # inside jupyter: %matplotlib qt5   # or 'tk' / 'qtagg'

The visualization opens a live matplotlib window that redraws whenever you
call `viz.update()`.  Force lines: red = attraction, blue = repulsion,
green stub = per-component net force direction.

Sheets available in v2alt:
    /home/vagrant/projects/kicad_agent/projects/power_module_v2alt/bms.kicad_sch
    /home/vagrant/projects/kicad_agent/projects/power_module_v2alt/charger.kicad_sch
    /home/vagrant/projects/kicad_agent/projects/power_module_v2alt/input.kicad_sch
    /home/vagrant/projects/kicad_agent/projects/power_module_v2alt/buckboost.kicad_sch

Two current fixes that changed autoplace behaviour vs. the last time this
notebook was touched:
    ed1f8e2 — global_labels now survive rewire (BAT+/GND/VBUS_9V restored)
    54d788f — pin_world stored unrounded so FET-gate-shaped pins don't drop

The tuning problem still open (task #66): the built-in run_staged_anneal
recipe over-spreads on hand-laid starting grids.  Example bboxes with the
compact profile (repulsion_k=200, attraction_k=0.4, polarity_k=0.2,
rotation_k=4.0, initial_temperature=25):
    charger    (19 comp): 110×56 (hand) → 152×67 (autoplace) — 1.38x
    buckboost  (23 comp): 156×57         → 230×110           — 1.47x
    input      (11 comp): 116×60         → 156×61            — 1.35x
    bms        (29 comp): 245×156        → 182×127           — 0.74x (yesterday's
                                                                   raw was over-spread)
Target from `feedback_autoplacer_params` memory is ~46×105 on BMS.
"""

# %% Cell 1 — session bootstrap (run once per kernel start)

# Uncomment if in Jupyter and you haven't set the backend yet:
# %matplotlib qt5
# %load_ext autoreload
# %autoreload 2

import sys
sys.path.insert(0, "/home/vagrant/projects/kicad_agent/KiCAD-MCP-Server/python")

from pathlib import Path
import shutil

from commands.autoplacer import PLACER, load_session, iterate
from commands.autoplacer_viz import AutoplacerViz

# Pick a sheet.  Swap the value to switch what you're tuning.
SOURCE = "/home/vagrant/projects/kicad_agent/projects/power_module_v2alt/bms.kicad_sch"

# Working copy — kept in /tmp so the committed .kicad_sch isn't touched.
WORK_DIR = Path("/tmp/work_kicad")
WORK_DIR.mkdir(exist_ok=True)
target = str(WORK_DIR / "work.kicad_sch")


def reload_sheet(source=SOURCE):
    """Reset the working copy from source and re-load the session.
    Returns a fresh AutoplacerViz."""
    shutil.copy(source, target)
    PLACER.sessions.clear()
    info = PLACER.load(target)
    print(f"Loaded {Path(source).name}: {info['n_components']} components, {info['n_nets']} nets")
    return AutoplacerViz(PLACER.get(target))


viz = reload_sheet()


def bbox():
    """Current bbox of the placement in mm."""
    sess = PLACER.get(target)
    xs = [c.x for c in sess.components.values()]
    ys = [c.y for c in sess.components.values()]
    return (max(xs) - min(xs), max(ys) - min(ys))


print(f"Starting bbox: {bbox()[0]:.1f} x {bbox()[1]:.1f} mm")


# %% Cell 2 — cluster stage (attraction only, no repulsion)
# Pulls same-net pins together with rotation enabled.  No repulsion means
# things pile up; that's fine — spread stage separates them.

PLACER.set_params(target,
                  repulsion_k=0,
                  attraction_k=0.2,
                  polarity_k=0.0,
                  rotation_k=4.0,
                  polarity_torque_k=0.0,
                  initial_temperature=50.0)

for _ in range(100):
    PLACER.get(target).temperature = 5.0  # clamp so early moves don't overshoot
    PLACER.iterate(target, n=1)
    viz.update()

print(f"After cluster: bbox {bbox()[0]:.1f} x {bbox()[1]:.1f}, "
      f"max_force {PLACER.get(target).last_max_force:.2f}")


# %% Cell 3 — spread stage (repulsion ramps up geometrically)
# Repulsion goes from 0.05 → 0.05 * 2^10 ≈ 51 over 11 iterations of 10 steps.
# The slow ramp lets clusters separate without losing the grouping from
# stage 1.  Watch the viz: components should fan outward.
# Compact profile expects repulsion peak around 200 — see cell 4 for
# a stronger variant.

for t2 in range(11):
    rep = 0.05 * 2 ** t2
    PLACER.set_params(target,
                      repulsion_k=rep,
                      attraction_k=0.2,
                      polarity_k=0.0,
                      rotation_k=4.0,
                      polarity_torque_k=0.0,
                      initial_temperature=50.0)
    for _ in range(10):
        PLACER.get(target).temperature = 5.0
        PLACER.iterate(target, n=1)
        viz.update()
    print(f"  spread t={t2}: rep={rep:.2f}, bbox {bbox()[0]:.1f}x{bbox()[1]:.1f}")


# %% Cell 4 — polarize stage (repulsion steps down, polarity turns on)
# Polarity biases V+/GND-facing pins to their preferred edges.
# Repulsion drops one notch per stage; polarity + torque switch on.

for t2 in range(2):
    rep = 0.05 * 2 ** (10 - t2)
    PLACER.set_params(target,
                      repulsion_k=rep,
                      attraction_k=0.2,
                      polarity_k=0.2,
                      rotation_k=4.0,
                      polarity_torque_k=3.0,
                      initial_temperature=50.0)
    for _ in range(10):
        PLACER.get(target).temperature = 5.0
        PLACER.iterate(target, n=1)
        viz.update()
    print(f"  polarize t={t2}: rep={rep:.2f}, bbox {bbox()[0]:.1f}x{bbox()[1]:.1f}")


# %% Cell 5 — settle stage (natural temperature decay)
# No repulsion clamp; temperature decays via params.cooling.  Let the
# configuration relax into its local minimum.

PLACER.set_params(target,
                  attraction_k=0.2,
                  polarity_k=0.2,
                  rotation_k=4.0,
                  polarity_torque_k=3.0,
                  initial_temperature=50.0)

for _ in range(100):
    PLACER.iterate(target, n=1)
    viz.update()

print(f"Settled: bbox {bbox()[0]:.1f} x {bbox()[1]:.1f}, "
      f"max_force {PLACER.get(target).last_max_force:.2f}")


# %% Cell 6 — write result to disk + reroute
# preview() = positions only, keeps old wiring (visually noisy but useful
# for checking coord placement without waiting on rewire).
# apply(rewire=True) = strips + re-lays wires, labels, and (with the recent
# fix) restores global_labels.

PLACER.preview(target, "/tmp/p.kicad_sch")           # positions-only snapshot
result = PLACER.apply(target, rewire=True)          # commit + reroute
print(f"apply: {result.get('n_components_updated')} updated, "
      f"rewire={result['rewire']['nets_rewired']} nets, "
      f"{result['rewire']['pairs_wired']} pairs wired, "
      f"{result['rewire']['globals_restored']} globals restored")


# %% Cell 7 — one-shot run of the shipped staged-anneal recipe
# Compare its output against your hand-tuned sequence above.  Same defaults
# the MCP tool uses when called with no per-cell knobs.

viz = reload_sheet()  # reset

PLACER.set_params(target,
                  repulsion_k=200,     # compact profile — see feedback memory
                  attraction_k=0.4,
                  polarity_k=0.2,
                  rotation_k=4.0,
                  initial_temperature=25.0)

# Uses run_staged_anneal internally — no live viz refresh, so call once and
# then update() at the end.
result = PLACER.recipe(target,
                       # Full-length recipe (see _RECIPE_DEFAULTS):
                       # cluster_iters=100, spread_stages=11, iters_per_stage=10,
                       # polarize_stages=2, settle_iters=100
                       )
viz.update()
print(f"Recipe: bbox {bbox()[0]:.1f} x {bbox()[1]:.1f}, "
      f"final max_force {result.get('max_force', '?')}")


# %% Cell 8 — comparison sweep: try different peak repulsion values
# Prints bbox at each peak so you can see where the compact target lands.

import copy
sweeps = [50, 100, 200, 400]
results = []
for rep_peak in sweeps:
    viz2 = reload_sheet()
    PLACER.set_params(target,
                      repulsion_k=rep_peak,
                      attraction_k=0.4,
                      polarity_k=0.2,
                      rotation_k=4.0,
                      initial_temperature=25.0)
    r = PLACER.recipe(target)
    w, h = bbox()
    results.append((rep_peak, w, h, r.get('max_force')))
    print(f"  peak_rep={rep_peak:>4}: bbox {w:6.1f} x {h:6.1f}, max_force {r.get('max_force')}")

print("\nCompact_v3 memory target on BMS: ~46 x 105 mm")


# %% Cell 9 — inspect one component in detail (for debugging force balance)

sess = PLACER.get(target)
for ck, c in list(sess.components.items())[:5]:
    print(f"{c.ref}__u{c.unit}: origin ({c.x:.2f}, {c.y:.2f}) rot={c.rotation:.1f}° "
          f"pinned={c.pinned}")


# %% Cell 10 — quick net-membership check (regression from off-grid fix)

sess = PLACER.get(target)
print(f"{'net':<15} {'pins':>4}")
for name in sorted(sess.nets):
    print(f"  {name:<13} {len(sess.nets[name].pins):>4}")
print(f"\nglobal_nets: {sorted(sess.global_nets)}")
