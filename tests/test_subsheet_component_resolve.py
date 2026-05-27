"""_resolve_component_sheet finds a component across sub-sheets (#234).

Uses the real power_module project (hierarchical: bms / charger /
buckboost) as the fixture — C29 lives on buckboost.kicad_sch, but
the resolver should find it given the top-level power_module.kicad_sch
path.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))


POWER_MODULE = Path(
    "/home/vagrant/projects/kicad_agent/projects/power_module"
)


def _real_project_available() -> bool:
    return (POWER_MODULE / "power_module.kicad_sch").exists()


@pytest.mark.skipif(
    not _real_project_available(),
    reason="power_module project not present",
)
class TestResolveComponentSheet:
    def _resolver(self):
        from kicad_interface import KiCADInterface
        return KiCADInterface._resolve_component_sheet

    def test_returns_given_path_when_component_on_top_sheet(self):
        resolve = self._resolver()
        top = str(POWER_MODULE / "power_module.kicad_sch")
        # J2 is on the top sheet
        assert resolve(top, "J2") == top

    def test_resolves_to_buckboost_sub_sheet(self):
        resolve = self._resolver()
        top = str(POWER_MODULE / "power_module.kicad_sch")
        # C29 is on buckboost.kicad_sch
        out = resolve(top, "C29")
        assert Path(out).name == "buckboost.kicad_sch"

    def test_resolves_to_bms_sub_sheet(self):
        resolve = self._resolver()
        top = str(POWER_MODULE / "power_module.kicad_sch")
        # C3 is on bms.kicad_sch
        out = resolve(top, "C3")
        assert Path(out).name == "bms.kicad_sch"

    def test_returns_given_path_when_component_missing(self):
        """No sheet has the reference — returns the given path so the
        downstream handler emits the standard 'not found' error."""
        resolve = self._resolver()
        top = str(POWER_MODULE / "power_module.kicad_sch")
        out = resolve(top, "NONEXISTENT_XYZ")
        assert out == top

    def test_resolves_from_a_sub_sheet_path_too(self):
        """Caller can pass any sheet in the project — even a sub-
        sheet that doesn't contain the component — and the resolver
        walks the full project."""
        resolve = self._resolver()
        # Start from buckboost (doesn't have C3); should find bms
        start = str(POWER_MODULE / "buckboost.kicad_sch")
        out = resolve(start, "C3")
        assert Path(out).name == "bms.kicad_sch"
