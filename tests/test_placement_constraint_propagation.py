"""Integration test for Placement_Anchor rename propagation.

Builds a minimal schematic in a temp dir with two components, sets a
Placement_Anchor property on one referencing the other, then renames
the target. Verifies the anchor value is updated automatically.

These tests need kicad-skip (`pip install kicad-skip`) and pcbnew (for
the dispatcher), so they're a thin wrapper around the helpers in
``commands.placement_constraints`` rather than calling the full MCP
interface.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


_MINIMAL_SCH_TEMPLATE = """(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(uuid "11111111-1111-1111-1111-111111111111")
\t(paper "A4")
\t(lib_symbols
\t\t(symbol "Device:C"
\t\t\t(pin_numbers (hide yes))
\t\t\t(pin_names (offset 0.254))
\t\t\t(exclude_from_sim no)
\t\t\t(in_bom yes)
\t\t\t(on_board yes)
\t\t\t(property "Reference" "C" (at 0.635 2.54 0) (effects (font (size 1.27 1.27))))
\t\t\t(property "Value" "C" (at 0.635 -2.54 0) (effects (font (size 1.27 1.27))))
\t\t\t(symbol "C_0_1" (polyline (pts (xy -2.032 -0.762) (xy 2.032 -0.762)) (stroke (width 0.508) (type default)) (fill (type none))))
\t\t\t(symbol "C_1_1"
\t\t\t\t(pin passive line (at 0 3.81 270) (length 2.794) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
\t\t\t\t(pin passive line (at 0 -3.81 90) (length 2.794) (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
\t\t\t)
\t\t)
\t)
\t(symbol (lib_id "Device:C") (at 50 50 0) (unit 1)
\t\t(in_bom yes) (on_board yes)
\t\t(uuid "22222222-2222-2222-2222-222222222222")
\t\t(property "Reference" "C1" (at 52 47.46 0))
\t\t(property "Value" "100nF" (at 52 52.54 0))
\t\t(property "Footprint" "" (at 50 50 0) (effects (hide yes)))
\t\t(property "Datasheet" "~" (at 50 50 0) (effects (hide yes)))
\t\t(property "Placement_Anchor" "{anchor}" (at 50 50 0) (effects (hide yes)))
\t\t(instances (project "test" (path "/" (reference "C1") (unit 1))))
\t)
\t(symbol (lib_id "Device:C") (at 70 50 0) (unit 1)
\t\t(in_bom yes) (on_board yes)
\t\t(uuid "33333333-3333-3333-3333-333333333333")
\t\t(property "Reference" "U1" (at 72 47.46 0))
\t\t(property "Value" "BMS" (at 72 52.54 0))
\t\t(property "Footprint" "" (at 70 50 0) (effects (hide yes)))
\t\t(property "Datasheet" "~" (at 70 50 0) (effects (hide yes)))
\t\t(instances (project "test" (path "/" (reference "U1") (unit 1))))
\t)
\t(sheet_instances (path "/" (page "1")))
)
"""


def _build_project(tmp_path: Path, anchor: str = "U1.10/within=3mm") -> Path:
    """Write minimal .kicad_pro + .kicad_sch into tmp_path. Returns sch path."""
    proj = tmp_path / "test.kicad_pro"
    proj.write_text('{"meta": {}}\n')
    sch = tmp_path / "test.kicad_sch"
    sch.write_text(_MINIMAL_SCH_TEMPLATE.format(anchor=anchor))
    return sch


class TestPropagateRename:
    def test_simple_rename_rewrites_anchor(self, tmp_path: Path):
        from commands.placement_constraints import (
            iter_components,
            propagate_rename,
        )

        sch = _build_project(tmp_path, anchor="U1.10/within=3mm")

        # Verify pre-state.
        comps = {c.reference: c for c in iter_components(sch)}
        assert comps["C1"].anchor == "U1.10/within=3mm"

        # Propagate U1 -> U7.
        result = propagate_rename(sch, {"U1": "U7"})

        assert result["sheetsTouched"] == [str(sch.resolve())]
        assert len(result["updated"]) == 1
        upd = result["updated"][0]
        assert upd["ref"] == "C1"
        assert upd["old"] == "U1.10/within=3mm"
        assert upd["new"] == "U7.10/within=3.0mm"

        # Re-read to verify persistence.
        comps = {c.reference: c for c in iter_components(sch)}
        assert comps["C1"].anchor == "U7.10/within=3.0mm"

    def test_dangling_ref_reported(self, tmp_path: Path):
        from commands.placement_constraints import propagate_rename

        # Anchor references U99 which is not in the project.
        sch = _build_project(tmp_path, anchor="U99.10/within=3mm")
        result = propagate_rename(sch, {"U1": "U7"})
        # No rewrite (U99 not in rename_map), but dangling reported.
        assert len(result["updated"]) == 0
        assert len(result["dangling"]) == 1
        assert result["dangling"][0]["owner"] == "C1"
        assert "U99" in result["dangling"][0]["reason"]

    def test_unrelated_rename_no_change(self, tmp_path: Path):
        from commands.placement_constraints import propagate_rename

        sch = _build_project(tmp_path, anchor="U1.10/within=3mm")
        # Rename a different ref entirely.
        result = propagate_rename(sch, {"C9": "C99"})
        assert result["updated"] == []
        assert result["sheetsTouched"] == []

    def test_find_top_schematic(self, tmp_path: Path):
        from commands.placement_constraints import find_top_schematic

        sch = _build_project(tmp_path)
        # Even when given a sub-path, the helper should find the top.
        # Here there's only one .kicad_sch, but the function should
        # locate the .kicad_pro sibling and return the matching .sch.
        assert find_top_schematic(sch) == sch.resolve()


class TestConstraintVersionEnsure:
    def test_can_be_written_alongside_existing_keys(self, tmp_path: Path):
        from commands.placement_constraints import (
            CONSTRAINT_VERSION,
            ensure_constraint_version,
            get_constraint_version,
        )
        import json

        proj = tmp_path / "test.kicad_pro"
        proj.write_text(json.dumps({"board": {"design_settings": {}}}))

        assert get_constraint_version(proj) is None
        assert ensure_constraint_version(proj) is True
        assert get_constraint_version(proj) == CONSTRAINT_VERSION

        data = json.loads(proj.read_text())
        assert data["board"]["design_settings"] == {}
        assert data["mcp_constraint_version"] == CONSTRAINT_VERSION
