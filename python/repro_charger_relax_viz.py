# Reproduce the EXACT relax_placement run that produced the charger
# re-placement on power_module (2026-05-28), with live viz added.
#
# It calls the same engine path the MCP `relax_placement` handler uses
# (load_pcb_session -> PCBSchedule -> run_pcb_relax -> keep-in clamp), so a
# divergence from your own script must be in the inputs (starting board,
# locked set, or schedule defaults) rather than the physics.
#
# ---------------------------------------------------------------------------
# STARTING BOARD — this matters:
#   My run started from the board state BEFORE the re-placement = commit
#   29e2a50 (the charger routed the prior day, 633 tracks), with the current
#   annotated schematics in the same directory (load_pcb_session reads
#   Pin_Spring_Class / mcp_spring_classes from the sibling .kicad_sch via
#   board.GetFileName()). You pushed the POST-cleanup board, so 29e2a50 is
#   now one commit back. Mirror exactly what I loaded:
#
#     cd <power_module repo>
#     mkdir -p /tmp/relax_repro
#     cp *.kicad_sch power_module.kicad_pro /tmp/relax_repro/
#     git show 29e2a50:power_module.kicad_pcb > /tmp/relax_repro/power_module.kicad_pcb
#
#   then set BOARD_PATH below to /tmp/relax_repro/power_module.kicad_pcb.
#
# In Jupyter, pick an interactive backend BEFORE importing pyplot, e.g.:
#     %matplotlib qt5      # animates live; needs Qt
#     # %matplotlib widget # inline-ish; live redraw is slower
# ---------------------------------------------------------------------------

# %% setup
import os
import sys

KICAD_MCP_PY = "/home/vagrant/projects/kicad_agent/KiCAD-MCP-Server/python"
if KICAD_MCP_PY not in sys.path:
    sys.path.insert(0, KICAD_MCP_PY)

import pcbnew
from commands.pcb_autoplacer import (
    load_pcb_session,
    edge_cuts_bbox,
    apply_session_to_board,
    run_pcb_relax,
    PCBSchedule,
)
from commands.pcb_autoplacer_viz import PCBAutoplacerViz

BOARD_PATH = "/tmp/relax_repro/power_module.kicad_pcb"  # <-- pre-re-place (29e2a50)

# The exact 57 refs I locked. Everything EXCEPT the 14-part charger core that
# was free to move: Q2, D1, L1, C10, C12, C13, C14, C15, C16, R10, R11, R12,
# R13, R14.  Note U3 is LOCKED (I kept it fixed so it wouldn't reorient or
# disturb its board-wide connections).
LOCKED_REFS = [
    "U3", "U2", "R8", "L2", "R9", "R6", "R23", "C31", "C23", "R27", "R3",
    "J2", "J3", "R22", "C4", "R1", "C22", "C20", "C9", "C7", "D2", "R25",
    "R5", "SW1", "R24", "C11", "C25", "C5", "C17", "C28", "R2", "R21", "R15",
    "R26", "C21", "J1", "C6", "C30", "Q4", "U1", "R20", "C2", "C24", "R7",
    "C26", "U4", "C8", "C27", "R16", "C1", "C29", "Q3", "Q1", "R4", "C3",
    "TH1", "BAT1",
]

VIZ_EVERY = 5            # redraw every N iterations (300 total: 200 spread + 100 snap)
SAVE_FRAMES = False      # also dump PNG frames to FRAME_DIR
FRAME_DIR = "/tmp/relax_repro/frames"

# %% load — build the Session exactly as the MCP handler does
board = pcbnew.LoadBoard(BOARD_PATH)
sess = load_pcb_session(
    board, locked_refs=set(LOCKED_REFS), auto_classify_planes=True
)
keep_in = edge_cuts_bbox(board, inset_mm=1.0)
n_anchored = sum(1 for c in sess.components.values() if c.pinned)
print(f"{len(sess.components)} comps, {n_anchored} anchored, {len(sess.nets)} nets")
print("free (should be the 14 charger-core parts):",
      sorted(c.ref for c in sess.components.values() if not c.pinned))

# %% viz init — opens the live figure (show_repulsion=True surfaces the
# repulsion vectors, the lead on the wedging behaviour)
viz = PCBAutoplacerViz(
    sess,
    keep_in_bbox=keep_in,
    show_pins=True,
    show_ratsnest=True,
    show_sum_force=True,
    show_repulsion=True,
)

# %% run — the ONLY non-default param in my run was repulsion_k_start=1e-6.
# Everything else is PCBSchedule's default (spread_iters=200, snap_iters=100,
# repulsion_k_peak=0.1, rotation_snap_peak=30, pinwise_torque_k=1.0,
# force_step_damping=0.3, step_spread=0.2, enforce_rotation_snap=True, ...).
schedule = PCBSchedule(repulsion_k_start=1e-6)

if SAVE_FRAMES:
    os.makedirs(FRAME_DIR, exist_ok=True)

_n = [0]


def on_step(s):
    _n[0] += 1
    if _n[0] % VIZ_EVERY == 0:
        viz.update()
        if SAVE_FRAMES:
            viz.save(os.path.join(FRAME_DIR, f"f{_n[0]:04d}.png"))


metrics = run_pcb_relax(
    sess, schedule, margin_mm=1.0, keep_in=keep_in, on_step=on_step
)
print("phases:", metrics["phases"])

# %% keep-in clamp — the handler post-clamps each OBB centre into the
# Edge.Cuts keep-in after the run; replicate it so the final state matches
# my MCP result exactly.
kl, kt, kr, kb = keep_in
if kr > kl and kb > kt:
    for c in sess.components.values():
        if c.pinned:
            continue
        bx, by = c.obb_center_world()
        dx, dy = bx - c.x, by - c.y
        hw, hh = c.bbox_w * 0.5, c.bbox_h * 0.5
        nbx = max(kl + hw, min(kr - hw, bx))
        nby = max(kt + hh, min(kb - hh, by))
        c.x = nbx - dx
        c.y = nby - dy
viz.update()

# %% report — final positions of the 14 moved parts
for c in sorted(sess.components.values(), key=lambda c: c.ref):
    if not c.pinned:
        print(f"{c.ref:5s} ({c.x:8.3f}, {c.y:8.3f})  rot={c.rotation:7.1f}")

# %% (optional) apply — my MCP run used dry_run=False (it wrote back). Leave
# commented to inspect only; uncomment to persist the same result.
# apply_session_to_board(sess, board)
# board.Save(BOARD_PATH)
