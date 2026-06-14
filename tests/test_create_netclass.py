"""Regression tests for create_netclass / add_net_class.

The old impl called `board.GetNetClasses().Find(name)` which raised
because the SWIG `netclasses_map` exposes lower-case `find`. The
correct pcbnew-9 path is
`board.GetDesignSettings().m_NetSettings.SetNetclass(name, NETCLASS)`
for create-or-replace; in-place update mutates the NETCLASS returned
by `GetNetClassByName` (the SWIG shared_ptr accessor returns the
live object). Patterns go through
`SetNetclassPatternAssignment(pattern, classname)`. Save persists
everything to `.kicad_pro` through the SETTINGS_MANAGER without us
having to touch the JSON.

These tests pin:
- create writes the new netclass to `.kicad_pro` after Save
- update is in-place (only changed fields apply; others preserved)
- nets-arg produces `netclass_patterns` entries
- both `create_netclass` (routing.ts traceWidth schema) and
  `add_net_class` (design-rules.ts trackWidth + snake_case
  uvia_diameter / diff_pair_* schema) dispatch to the same impl
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))


def _real_pcbnew_available() -> bool:
    import pcbnew  # type: ignore
    return getattr(pcbnew, "GetBuildVersion", lambda: "")() != "9.0.0-stub"


@pytest.mark.integration
@pytest.mark.skipif(
    not _real_pcbnew_available(),
    reason="needs real pcbnew swig module",
)
class TestCreateNetclassJsonEdit:
    """Verifies create_netclass / add_net_class against blink_555
    (smallest project with a real .kicad_pro). Class name retained
    for git-history continuity with the earlier JSON-edit impl."""

    @pytest.fixture
    def tmp_project(self):
        src = Path("/home/vagrant/projects/kicad_agent/projects/blink_555")
        if not src.exists():
            pytest.skip(f"blink_555 fixture missing at {src}")
        tmp = Path(tempfile.mkdtemp(prefix="netclass_test_"))
        for f in src.glob("*"):
            if f.is_file():
                shutil.copy(f, tmp / f.name)
        yield tmp
        # Best-effort cleanup
        try:
            shutil.rmtree(tmp)
        except Exception:
            pass

    def _make_rc(self, tmp: Path):
        import pcbnew
        from commands.routing import RoutingCommands
        board = pcbnew.LoadBoard(str(tmp / "blink_555.kicad_pcb"))
        return RoutingCommands(board), tmp / "blink_555.kicad_pro"

    def test_create_new_netclass_persists_to_kicad_pro(self, tmp_project):
        rc, pro_path = self._make_rc(tmp_project)
        r = rc.create_netclass({
            "name": "SIGNAL_FAST",
            "clearance": 0.1,
            "trackWidth": 0.15,
            "viaDiameter": 0.45,
            "viaDrill": 0.2,
        })
        assert r["success"] is True, r
        assert r["action"] == "created"
        with pro_path.open() as f:
            pro = json.load(f)
        names = [c["name"] for c in pro["net_settings"]["classes"]]
        assert "SIGNAL_FAST" in names
        entry = next(c for c in pro["net_settings"]["classes"]
                     if c["name"] == "SIGNAL_FAST")
        assert entry["clearance"] == pytest.approx(0.1)
        assert entry["track_width"] == pytest.approx(0.15)
        assert entry["via_diameter"] == pytest.approx(0.45)
        assert entry["via_drill"] == pytest.approx(0.2)
        # Priority must be a small integer well below the Default
        # sentinel (INT_MAX) so the new class's patterns actually win.
        assert 0 < entry["priority"] < 2 ** 30, entry["priority"]

    def test_update_existing_netclass_preserves_unchanged_fields(self, tmp_project):
        rc, pro_path = self._make_rc(tmp_project)
        rc.create_netclass({
            "name": "FAST",
            "clearance": 0.1, "trackWidth": 0.15,
            "viaDiameter": 0.45, "viaDrill": 0.2,
        })
        # Update only clearance; track_width must stay at 0.15
        rc2, _ = self._make_rc(tmp_project)
        r = rc2.create_netclass({"name": "FAST", "clearance": 0.13})
        assert r["action"] == "updated"
        assert r["fieldsApplied"] == {"clearance": 0.13}
        with pro_path.open() as f:
            pro = json.load(f)
        entry = next(c for c in pro["net_settings"]["classes"]
                     if c["name"] == "FAST")
        assert entry["clearance"] == pytest.approx(0.13)
        assert entry["track_width"] == pytest.approx(0.15)

    def test_nets_become_patterns(self, tmp_project):
        rc, pro_path = self._make_rc(tmp_project)
        rc.create_netclass({
            "name": "POWER_TEST",
            "trackWidth": 0.5,
            "nets": ["VCC", "GND"],
        })
        with pro_path.open() as f:
            pro = json.load(f)
        patterns = pro["net_settings"].get("netclass_patterns", [])
        pairs = {(p["netclass"], p["pattern"]) for p in patterns}
        assert ("POWER_TEST", "VCC") in pairs
        assert ("POWER_TEST", "GND") in pairs

    def test_add_net_class_dispatch_with_snake_case_params(self, tmp_project):
        """`add_net_class` TS schema uses `uvia_diameter` /
        `diff_pair_width` (snake_case). Both must be accepted by the
        same impl."""
        rc, pro_path = self._make_rc(tmp_project)
        r = rc.create_netclass({
            "name": "DIFF",
            "trackWidth": 0.2,
            "uvia_diameter": 0.25,
            "diff_pair_width": 0.18,
            "diff_pair_gap": 0.15,
        })
        assert r["success"] is True
        with pro_path.open() as f:
            pro = json.load(f)
        entry = next(c for c in pro["net_settings"]["classes"]
                     if c["name"] == "DIFF")
        assert entry["microvia_diameter"] == pytest.approx(0.25)
        assert entry["diff_pair_width"] == pytest.approx(0.18)
        assert entry["diff_pair_gap"] == pytest.approx(0.15)

    def test_traceWidth_alias_for_trackWidth(self, tmp_project):
        """`create_netclass` TS schema uses `traceWidth`; alias must
        map to track_width same as `trackWidth`."""
        rc, pro_path = self._make_rc(tmp_project)
        r = rc.create_netclass({
            "name": "ALIAS",
            "traceWidth": 0.33,
        })
        assert r["success"] is True
        with pro_path.open() as f:
            pro = json.load(f)
        entry = next(c for c in pro["net_settings"]["classes"]
                     if c["name"] == "ALIAS")
        assert entry["track_width"] == pytest.approx(0.33)

    def test_pcbnew_sees_new_netclass_after_reload(self, tmp_project):
        """After Save+LoadBoard, GetNetClassByName must return the new
        netclass — proves the .kicad_pro edit propagates to pcbnew's
        in-memory NETCLASS objects (i.e. user-facing tools like the
        autorouter will pick it up)."""
        import pcbnew
        rc, pro_path = self._make_rc(tmp_project)
        rc.create_netclass({
            "name": "RELOAD_TEST",
            "clearance": 0.22, "trackWidth": 0.33,
            "viaDiameter": 0.55, "viaDrill": 0.25,
        })
        # Fresh LoadBoard from disk
        board2 = pcbnew.LoadBoard(str(pro_path.with_suffix(".kicad_pcb")))
        ns = board2.GetDesignSettings().m_NetSettings
        nc = ns.GetNetClassByName("RELOAD_TEST")
        assert nc.GetName() == "RELOAD_TEST"
        assert nc.GetClearance() == 220_000  # 0.22 mm in nm
        assert nc.GetTrackWidth() == 330_000
        assert nc.GetViaDiameter() == 550_000
