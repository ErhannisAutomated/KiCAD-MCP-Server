"""PCB autoplacer (relax_placement v2) — interactive tuning script.

Designed for Jupyter Lab with autoreload.  Each top-level block below
is a logical cell — split with ``# %%`` markers so VSCode-jupyter or
``jupytext`` can pick them up, or copy them sequentially into notebook
cells.

The visualizer is ``PCBAutoplacerViz`` (in
``KiCAD-MCP-Server/python/commands/pcb_autoplacer_viz.py``) — a
persistent pop-out matplotlib window that you redraw in place by
calling ``viz.update()``.  Same pattern the schematic tuning used,
just PCB-aware: OBB-rotated component rectangles, spring-class-colored
ratsnest, Edge.Cuts keep-in instead of sheet bbox.

Workflow:
    1. Edit code under KiCAD-MCP-Server/python/commands/autoplacer.py
       or pcb_autoplacer.py.
    2. Run cell ``Load board → Session`` to refresh the model.
    3. Run cell ``Open viz`` once to pop the window.
    4. Tweak params → call ap.iterate(sess, n=N) → call viz.update()
       in a loop.  Or use ``run_phase()`` helper for whole phases.
    5. When happy, run ``Save back to board``.

The script never mutates power_module.kicad_pcb directly — it
operates on power_module_v2test.kicad_pcb (a copy).
"""

# %% [markdown]
# # PCB Relax v2 — Jupyter tuning notebook
#
# Imports + autoreload.  Re-run this cell after touching any
# `autoplacer.py` / `pcb_autoplacer.py` code.

# %%
%load_ext autoreload
%autoreload 2

# Backend that supports a real pop-out window.  Try 'qt5agg' first;
# fall back to 'tkagg' if qt isn't installed.  '%matplotlib qt5' or
# '%matplotlib tk' as a notebook line magic works too.
%matplotlib qt5

import sys, math, time, importlib
sys.path.insert(0, '/home/erhannis/mods/KiCAD-MCP-Server/python')

import pcbnew
import commands.autoplacer as ap
import commands.pcb_autoplacer as pcba
import commands.pcb_autoplacer_viz as vizmod
importlib.reload(ap)
importlib.reload(pcba)
importlib.reload(vizmod)
from commands.pcb_autoplacer_viz import PCBAutoplacerViz

PCB_PATH = '/home/erhannis/projects/s3_power_module/power_module_v2test.kicad_pcb'




# %% [markdown]
# # Load board → Session
#
# Reads from disk each run.  Any positions written by a prior `Save
# back to board` cell are picked up; otherwise the board is whatever
# you last had open in KiCad.

# %%
board = pcbnew.LoadBoard(PCB_PATH)
sess = pcba.load_pcb_session(board)
keep_in = pcba.edge_cuts_bbox(board, inset_mm=1.0)

# v2 physics flags + suppress schematic-only forces
p = sess.params
p.use_obb_repulsion = True
p.use_spring_classes = True
p.polarity_k = 0.0;
p.polarity_torque_k = 0.0
p.rotation_k = 0.0
p.obb_repulsion_margin = 1.0

kl, kt, kr, kb = keep_in   # left, top, right, bottom (Y-down)
p.sheet_x_min, p.sheet_x_max = kl, kr
p.sheet_y_min, p.sheet_y_max = kt, kb
p.boundary_k = 1.0   # try 0.5–5.0; 5.0 is the schematic default

# Stability knobs added 2026-05-21 — toggle to compare behavior:
p.force_step_damping = 0.3              # <1.0 prevents period-2 overshoot
p.normalize_spring_force_by_degree = True  # bounds K_eff by N pins
# Sequential (Gauss-Seidel) apply, added 2026-05-22.  Each component
# sees the just-updated positions of those processed earlier in the
# same iter; default Jacobi mode uses a position snapshot for the
# whole iter.  Sequential resolves pair feedbacks within an iter (no
# cross-iter ping-pong) at the cost of being order-dependent.
p.sequential_apply = False
# This works, but I also wonder whether there are advantages to not snapping rotations.
do_snap_rotations = True

sess.nets['USB_VBUS'].spring_class = 'PLANE'
sess.nets['V12_OUT'].spring_class = 'INTER_GROUP'

print(f"Loaded {len(sess.components)} comps, {len(sess.nets)} nets")
print(f"Anchored: {sum(1 for c in sess.components.values() if c.pinned)}")
print(f"Keep-in: {keep_in}")
print(f"PLANE nets: {sum(1 for n in sess.nets.values() if n.spring_class=='PLANE')}")



# %% [markdown]
# # Open viz
#
# Opens a pop-out matplotlib window that stays around for the rest
# of the session.  Calling ``viz.update()`` redraws the same window
# in place — no flicker, no closing/reopening.  Toggle individual
# overlays with the attributes.

# %%
viz = PCBAutoplacerViz(
    sess,
    keep_in_bbox=keep_in,
    figsize=(13, 9),
    show_pins=True,
    show_ratsnest=True,
    show_sum_force=True,
    show_repulsion=False,   # toggle True to overlay top-30 OBB repulsion pairs
)





# %% [markdown]
# # gaps(threshold) — list close/overlapping component pairs
#
# Returns {"REFA:REFB": gap_mm} for all same-layer pairs with OBB
# separation strictly less than ``threshold``.  Negative gap means
# penetration (overlap depth).  Output is sorted ascending so the
# worst penetrations are at the top.
#
# Example:
#     gaps(3.0)
#     -> {"R2:R3": -0.5, "U1:C7": 0.8, "R2:U1": 2.4}
#
# Same-layer only — F.Cu vs B.Cu pairs don't count.  Pinned anchors
# are included like everything else (so you can spot stuck pieces).

# %%
def gaps(threshold_mm=3.0, sess=sess):
    from commands.autoplacer import obb_separation
    result = {}
    comps = list(sess.components.values())
    for i, a in enumerate(comps):
        for b in comps[i + 1:]:
            if a.layer != b.layer:
                continue
            gap, _ = obb_separation(
                a.x, a.y, a.bbox_w, a.bbox_h, a.rotation,
                b.x, b.y, b.bbox_w, b.bbox_h, b.rotation,
            )
            if gap < threshold_mm:
                result[f"{a.ref}:{b.ref}"] = round(gap, 3)
    return dict(sorted(result.items(), key=lambda kv: kv[1]))


def snap_rotations(sess=sess, period=90.0):
    """Round each unpinned component's rotation to nearest multiple of `period`."""
    n = 0
    for c in sess.components.values():
        if c.pinned:
            continue
        snapped = round(c.rotation / period) * period % 360
        if abs(((c.rotation - snapped + 540) % 360) - 180) > 1e-6:
            c.rotation = snapped
            n += 1
    return n




# %% [markdown]
# # Manual stepping (the main loop you'll iterate)
#
# Tweak params, step some iters, call viz.update().  Re-run this
# cell freely.  The viz computes forces from the same helpers
# `iterate()` uses, so the arrows always reflect what the NEXT
# step would do.

# %%
# Example: cluster phase tune — springs only, no repulsion.
# NB: PCB rotation comes from pinwise_torque_k (lever-arm torque on
# off-center spring forces).  rotation_k is the *schematic-flavored*
# angle-based torque and is now a no-op for PCB components — it
# relied on pin outward angles that pads don't have.
p.attraction_k = 1.0
p.repulsion_k = 0.0
p.rotation_snap_strength = 0.0
p.rotation_k = 0.0
p.pinwise_torque_k = 1.0
sess.temperature = 10.0
for _ in range(50):
  ap.iterate(sess, n=1)
  viz.update()
  viz.update()




# %% [markdown]
# # Phase-by-phase helper
#
# Convenience wrapper.  Each call runs N iters with the given
# physics params and a final viz.update().  Useful for stepping
# through the schedule one phase at a time.

# %%
def run_phase(n_iters, *, attraction_k=None, repulsion_k=None,
              rotation_snap=None, temperature=None,
              ramp_repulsion_from=None, ramp_snap_from=None):
    """Step the session N iters, optionally ramping a param across.

    If ``ramp_repulsion_from`` is set, repulsion_k linearly interpolates
    from that value to ``repulsion_k`` across the N iters; same for snap.
    """
    
    if attraction_k is not None:
        p.attraction_k = attraction_k
    if repulsion_k is not None and ramp_repulsion_from is None:
        p.repulsion_k = repulsion_k
    if rotation_snap is not None and ramp_snap_from is None:
        p.rotation_snap_strength = rotation_snap

    for t in range(n_iters):
        if ramp_repulsion_from is not None and repulsion_k is not None:
            a = ramp_repulsion_from
            b = math.log(repulsion_k/ramp_repulsion_from)/(n_iters-1)
            # ratio = (t + 1) / n_iters
            # p.repulsion_k = ramp_repulsion_from + (repulsion_k - ramp_repulsion_from) * ratio
            p.repulsion_k = a * math.exp(b*t)
        if ramp_snap_from is not None and rotation_snap is not None:
            ratio = (t + 1) / n_iters
            p.rotation_snap_strength = ramp_snap_from + (rotation_snap - ramp_snap_from) * ratio
        if temperature is not None:
            sess.temperature = temperature
        ap.iterate(sess, n=1)
        viz.update()
        viz.update()


#print("Cluster: springs only, generous temperature")
#run_phase(30, attraction_k=0.1, repulsion_k=0.0, rotation_snap=0.0, temperature=1.0)

print("Spread: repulsion ramps")
run_phase(100, repulsion_k=0.001, ramp_repulsion_from=0.0001, temperature=0.2)
run_phase(100, repulsion_k=0.1, ramp_repulsion_from=0.001, temperature=0.05)

if do_snap_rotations:
  print("Snap: rotation snap ramps 0 → 3")
  run_phase(100, rotation_snap=30.0, ramp_snap_from=0.0, temperature=0.05)
  
  # print("Relax: full snap, low temp")
  # run_phase(20, temperature=0.05)
  
  print("Snap: full snap")
  snap_rotations()
  viz.update()
  viz.update()



# %% [markdown]
# # Save back to board
#
# After this you can open the file in KiCad to inspect, or run
# `get_drc_violations` / `get_ratsnest` via MCP for further checks.

# %%
n = pcba.apply_session_to_board(sess, board)
board.Save(PCB_PATH)
print(f"Saved {n} component updates to {PCB_PATH}")

