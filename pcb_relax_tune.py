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
sys.path.insert(0, '/home/vagrant/projects/kicad_agent/KiCAD-MCP-Server/python')

import pcbnew
import commands.autoplacer as ap
import commands.pcb_autoplacer as pcba
import commands.pcb_autoplacer_viz as vizmod
importlib.reload(ap)
importlib.reload(pcba)
importlib.reload(vizmod)
from commands.pcb_autoplacer_viz import PCBAutoplacerViz

PCB_PATH = '/home/vagrant/projects/kicad_agent/projects/power_module/power_module_v2test.kicad_pcb'


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
p.polarity_k = 0.0; p.polarity_torque_k = 0.0
p.boundary_k = 0.0; p.rotation_k = 0.0
p.obb_repulsion_margin = 1.0
# Stability knobs added 2026-05-21 — toggle to compare behavior:
p.force_step_damping = 0.5              # <1.0 prevents period-2 overshoot
p.normalize_spring_force_by_degree = True  # bounds K_eff by N pins
# Sequential (Gauss-Seidel) apply, added 2026-05-22.  Each component
# sees the just-updated positions of those processed earlier in the
# same iter; default Jacobi mode uses a position snapshot for the
# whole iter.  Sequential resolves pair feedbacks within an iter (no
# cross-iter ping-pong) at the cost of being order-dependent.
p.sequential_apply = False

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
p.attraction_k = 0.1
p.repulsion_k = 0.0
p.rotation_snap_strength = 0.0
sess.temperature = 5.0
ap.iterate(sess, n=10)
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
            ratio = (t + 1) / n_iters
            p.repulsion_k = ramp_repulsion_from + (repulsion_k - ramp_repulsion_from) * ratio
        if ramp_snap_from is not None and rotation_snap is not None:
            ratio = (t + 1) / n_iters
            p.rotation_snap_strength = ramp_snap_from + (rotation_snap - ramp_snap_from) * ratio
        if temperature is not None:
            sess.temperature = temperature
        ap.iterate(sess, n=1)
    viz.update()


# %% Cluster: springs only, generous temperature
run_phase(30, attraction_k=0.1, repulsion_k=0.0, rotation_snap=0.0, temperature=5.0)

# %% Spread: repulsion ramps 0 → 30
run_phase(40, repulsion_k=30.0, ramp_repulsion_from=0.0, temperature=3.0)

# %% Snap: rotation snap ramps 0 → 3
run_phase(30, rotation_snap=3.0, ramp_snap_from=0.0, temperature=1.5)

# %% Relax: full snap, low temp
run_phase(20, temperature=0.5)


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


# %% [markdown]
# # Quality metrics
#
# Compare MST length and crossings before/after.  Skips PLANE-class
# nets (those route via vias-to-pour, not point-to-point).

# %%
def mst_length_and_crossings(sess, ignore_classes=('PLANE',)):
    def mst(points):
        n = len(points)
        if n < 2:
            return 0.0, []
        in_tree = [False] * n; dist = [float('inf')] * n
        parent = [-1] * n; dist[0] = 0.0; total = 0.0
        edges = []
        for _ in range(n):
            u = -1
            for i in range(n):
                if not in_tree[i] and (u == -1 or dist[i] < dist[u]):
                    u = i
            if dist[u] == float('inf'):
                break
            in_tree[u] = True; total += dist[u]
            if parent[u] >= 0:
                edges.append((points[parent[u]], points[u]))
            for v in range(n):
                if in_tree[v]: continue
                d = math.hypot(points[u][0]-points[v][0], points[u][1]-points[v][1])
                if d < dist[v]: dist[v] = d; parent[v] = u
        return total, edges

    def segs_intersect(p1, p2, p3, p4):
        x1,y1=p1; x2,y2=p2; x3,y3=p3; x4,y4=p4
        denom = (x1-x2)*(y3-y4) - (y1-y2)*(x3-x4)
        if abs(denom) < 1e-9: return False
        t = ((x1-x3)*(y3-y4) - (y1-y3)*(x3-x4)) / denom
        u = -((x1-x2)*(y1-y3) - (y1-y2)*(x1-x3)) / denom
        eps = 1e-6
        return eps < t < 1-eps and eps < u < 1-eps

    total = 0.0
    all_edges = []
    for net in sess.nets.values():
        if net.spring_class in ignore_classes:
            continue
        pts = []
        for ck, pn in net.pins:
            c = sess.components.get(ck)
            if c is None: continue
            wp = c.world_pin_xy(pn)
            if wp is not None: pts.append(wp)
        if len(pts) < 2: continue
        L, edges = mst(pts)
        total += L
        for e in edges:
            all_edges.append((net.name, e[0], e[1]))

    n = len(all_edges); cx = 0
    for i in range(n):
        for j in range(i+1, n):
            if all_edges[i][0] == all_edges[j][0]: continue
            if segs_intersect(all_edges[i][1], all_edges[i][2],
                              all_edges[j][1], all_edges[j][2]):
                cx += 1
    return total, cx

L, X = mst_length_and_crossings(sess)
print(f"MST length (non-plane nets): {L:.1f} mm   crossings: {X}")


# %% [markdown]
# # Save back to board
#
# After this you can open the file in KiCad to inspect, or run
# `get_drc_violations` / `get_ratsnest` via MCP for further checks.

# %%
n = pcba.apply_session_to_board(sess, board)
board.Save(PCB_PATH)
print(f"Saved {n} component updates to {PCB_PATH}")


# %% [markdown]
# # Run the full canned schedule (for comparison)
#
# This is what the MCP `relax_placement` handler invokes — useful as
# a baseline reference once you've tuned manually and want to fold
# your insights into the production defaults.

# %%
# Reset from disk first
board = pcbnew.LoadBoard(PCB_PATH)
sess = pcba.load_pcb_session(board)
keep_in = pcba.edge_cuts_bbox(board, inset_mm=1.0)

# Rebind the viz to the new session
viz = PCBAutoplacerViz(sess, keep_in_bbox=keep_in)

# Optional on_step callback to redraw every 5 iters during the run.
def on_step(s):
    if s.iteration % 5 == 0:
        viz.update()

sched = pcba.PCBSchedule(
    cluster_iters=30, spread_iters=40, snap_iters=30, relax_iters=20,
    spring_k=0.1, repulsion_k_peak=30.0, rotation_snap_peak=3.0,
)
metrics = pcba.run_pcb_relax(sess, sched, margin_mm=1.0, on_step=on_step)
viz.update()
print(metrics)


# %% [markdown]
# # Reset to original layout
#
# If you've made a mess, copy the untouched original back over the
# v2test board and re-run the `Load board → Session` cell.

# %%
import shutil
shutil.copy(
    '/home/vagrant/projects/kicad_agent/projects/power_module/power_module.kicad_pcb',
    PCB_PATH,
)
print(f"Reset {PCB_PATH} to original")
