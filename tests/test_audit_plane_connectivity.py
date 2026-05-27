"""Integration tests for audit_plane_connectivity (#236).

Uses the real power_module project — has BAT+ zones on F.Cu + In2.Cu
and GND zones on F.Cu / B.Cu / In1.Cu, so we can verify multi-island
reporting against a known-good board.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from commands.routing import RoutingCommands  # noqa: E402


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
class TestAuditPlaneConnectivity:
    def _load(self):
        import pcbnew
        return RoutingCommands(board=pcbnew.LoadBoard(str(POWER_MODULE_PCB)))

    def test_succeeds_with_net_filter(self):
        r = self._load().audit_plane_connectivity({"net": "BAT+"})
        assert r["success"], r
        assert r["netCount"] == 1
        assert r["nets"][0]["net"] == "BAT+"

    def test_islands_have_expected_fields(self):
        r = self._load().audit_plane_connectivity({"net": "BAT+"})
        net = r["nets"][0]
        assert net["islandCount"] >= 1
        for isl in net["islands"]:
            for k in ("layer", "polyIndex", "bbox", "areaMm2",
                      "padCount", "viaCount", "pads", "vias"):
                assert k in isl, f"island missing key {k}"

    def test_bat_plus_plane_has_a_large_inner_plane(self):
        """power_module has BAT+ on In2.Cu — should be one big island."""
        r = self._load().audit_plane_connectivity({"net": "BAT+"})
        net = r["nets"][0]
        in2_islands = [
            isl for isl in net["islands"] if isl["layer"] == "In2.Cu"
        ]
        assert in2_islands, "no In2.Cu BAT+ island found"
        biggest = max(in2_islands, key=lambda x: x["areaMm2"])
        # In2.Cu plane should cover most of the 95×77mm board
        assert biggest["areaMm2"] > 1000, (
            f"In2.Cu BAT+ plane seems too small: {biggest['areaMm2']}mm²"
        )

    def test_no_net_filter_scans_all_zoned_nets(self):
        r = self._load().audit_plane_connectivity({})
        assert r["success"]
        net_names = {n["net"] for n in r["nets"]}
        # power_module has zones on GND, BAT+, V12_OUT, etc.
        assert "GND" in net_names
        assert "BAT+" in net_names

    def test_pads_listed_with_ref_and_padnum(self):
        r = self._load().audit_plane_connectivity({"net": "BAT+"})
        net = r["nets"][0]
        # Every island.pads entry has ref/padNum/position
        for isl in net["islands"]:
            for p in isl["pads"]:
                assert "ref" in p
                assert "padNum" in p
                assert "position" in p
                assert "x" in p["position"]
                assert "y" in p["position"]

    def test_out_of_fill_items_have_type(self):
        r = self._load().audit_plane_connectivity({})
        for net in r["nets"]:
            for item in net["outOfFill"]:
                assert item["type"] in ("pad", "via")
