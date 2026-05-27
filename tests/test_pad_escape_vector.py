"""get_pad_position returns escape vector pointing away from IC body (#235)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from commands.component import ComponentCommands  # noqa: E402


POWER_MODULE_PCB = Path(
    "/home/vagrant/projects/kicad_agent/projects/power_module/power_module.kicad_pcb"
)


def _real_pcbnew_available() -> bool:
    import pcbnew  # type: ignore
    return getattr(pcbnew, "GetBuildVersion", lambda: "")() != "9.0.0-stub"


@pytest.mark.integration
@pytest.mark.skipif(
    not (_real_pcbnew_available() and POWER_MODULE_PCB.exists()),
    reason="needs real pcbnew + power_module fixture",
)
class TestPadEscapeVector:
    def _load(self):
        import pcbnew
        return pcbnew.LoadBoard(str(POWER_MODULE_PCB))

    def test_qfn_left_pin_escapes_left(self):
        cc = ComponentCommands(board=self._load())
        # U3 is a QFN-16. Pin 9 is on the LEFT side (per the
        # get_component_pads dump captured during routing).
        r = cc.get_pad_position({"reference": "U3", "pad": "9"})
        assert r["success"], r
        v = r["escapeVector"]
        assert v is not None
        assert v["x"] < -0.5, f"left-side pin should escape leftward: {v}"

    def test_qfn_right_pin_escapes_right(self):
        cc = ComponentCommands(board=self._load())
        # U3 pin 1 is on the RIGHT side.
        r = cc.get_pad_position({"reference": "U3", "pad": "1"})
        v = r["escapeVector"]
        assert v["x"] > 0.5, f"right-side pin should escape rightward: {v}"

    def test_two_pad_cap_pads_oppose(self):
        cc = ComponentCommands(board=self._load())
        # C29 is a two-pad cap. Pad 1 and pad 2 should escape in
        # opposite directions.
        r1 = cc.get_pad_position({"reference": "C29", "pad": "1"})
        r2 = cc.get_pad_position({"reference": "C29", "pad": "2"})
        v1, v2 = r1["escapeVector"], r2["escapeVector"]
        # Dot product < 0 → opposite directions
        dot = v1["x"] * v2["x"] + v1["y"] * v2["y"]
        assert dot < -0.5, f"two-pad cap pads should oppose: {v1} . {v2} = {dot}"

    def test_magnitude_present_and_positive(self):
        cc = ComponentCommands(board=self._load())
        r = cc.get_pad_position({"reference": "U3", "pad": "1"})
        assert r["escapeMagnitudeMm"] is not None
        assert r["escapeMagnitudeMm"] > 0.5

    def test_angle_in_range(self):
        cc = ComponentCommands(board=self._load())
        r = cc.get_pad_position({"reference": "U3", "pad": "1"})
        ang = r["escapeAngleDeg"]
        assert -180 <= ang <= 180

    def test_angle_consistent_with_vector(self):
        import math
        cc = ComponentCommands(board=self._load())
        r = cc.get_pad_position({"reference": "U3", "pad": "9"})
        v = r["escapeVector"]
        recomputed_angle = math.degrees(math.atan2(v["y"], v["x"]))
        assert abs(recomputed_angle - r["escapeAngleDeg"]) < 0.1
