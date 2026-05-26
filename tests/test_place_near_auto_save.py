"""Regression guard for #174: PCB-mutating tools must opt into auto-save.

The bug: tools like `place_near` update `self.board` in memory but never
call `self.board.Save()`. External readers (kicad-cli, fresh
`pcbnew.LoadBoard`, an out-of-process render script) see the previous
state and silently drift from MCP's view.

The fix: add every PCB-mutating tool to
`KiCADInterface._BOARD_MUTATING_COMMANDS`, which triggers
`_auto_save_board()` after the SWIG dispatch.

This is a structural test — it asserts the membership of the set rather
than running real pcbnew (which is stubbed by conftest). A full
end-to-end run would need a non-mocked pcbnew; do that manually with
`python3 -c 'from kicad_interface import KiCADInterface; ...'` if the
underlying behaviour is in doubt.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from kicad_interface import KiCADInterface


# Every tool that mutates self.board (or a loaded copy) but does NOT
# explicitly call .Save() inside its own handler. Missing any of these
# causes the #174 silent-disk-drift bug.
PCB_MUTATORS_REQUIRING_AUTO_SAVE = {
    # component.py
    "place_component",
    "move_component",
    "rotate_component",
    "delete_component",
    "edit_component",
    "duplicate_component",
    "place_component_array",
    "align_components",
    # placement
    "place_near",
    # routing.py
    "route_trace",
    "route_pad_to_pad",
    "route_differential_pair",
    "add_via",
    "stitch_pour_vias",
    "pair_via",
    "delete_trace",
    "dedupe_traces",
    "modify_trace",
    "copy_routing_pattern",
    "add_net",
    "add_copper_pour",
    "refill_zones",
    # board geometry
    "add_board_outline",
    "add_mounting_hole",
    "add_text",
    "add_board_text",
    "import_svg_logo",
    # schematic-to-board sync (mutates board)
    "sync_schematic_to_board",
    "connect_passthrough",
    "connect_to_net",
    "connect_pins",
    "connect_component_to_nets",
}


def test_all_pcb_mutators_are_in_auto_save_set():
    """Every PCB-mutating tool must be in _BOARD_MUTATING_COMMANDS."""
    missing = PCB_MUTATORS_REQUIRING_AUTO_SAVE - KiCADInterface._BOARD_MUTATING_COMMANDS
    assert not missing, (
        f"PCB mutators missing from auto-save set: {sorted(missing)}. "
        f"Without auto-save, their changes only live in self.board until "
        f"someone calls save_project explicitly. See #174."
    )


def test_place_near_in_auto_save_set():
    """The specific trigger case for #174."""
    assert "place_near" in KiCADInterface._BOARD_MUTATING_COMMANDS
