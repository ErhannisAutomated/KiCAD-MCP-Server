"""Tests for verify_netclass_patterns (#207).

KiCAD's GUI can silently strip netclass_patterns entries on save when
it normalises the project file across version upgrades (the
power_module CELL1_TOP/CELL2_TOP regression in commit 14a143a). This
tool snapshots the expected pattern set into an
mcp_expected_netclass_patterns section that KiCAD won't touch, and
lets us detect/restore drift.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.netclass_patterns import (  # noqa: E402
    MCP_EXPECTED_KEY,
    bootstrap_expected_patterns,
    verify_netclass_patterns,
)


def _make_pro(tmp_path: Path, patterns: list) -> Path:
    p = tmp_path / "test.kicad_pro"
    p.write_text(json.dumps({
        "net_settings": {
            "netclass_patterns": patterns,
        },
    }))
    return p


@pytest.mark.unit
class TestBootstrap:
    def test_first_call_seeds_expected_from_current(self, tmp_path: Path):
        pro = _make_pro(tmp_path, [
            {"netclass": "POWER_4A", "pattern": "BAT+"},
            {"netclass": "POWER_4A", "pattern": "BAT-"},
        ])
        result = verify_netclass_patterns(pro)
        assert result["success"]
        assert result["bootstrapped"] is True
        assert result["drifted"] is False
        data = json.loads(pro.read_text())
        expected = data[MCP_EXPECTED_KEY]
        assert {(p["netclass"], p["pattern"]) for p in expected} == {
            ("POWER_4A", "BAT+"),
            ("POWER_4A", "BAT-"),
        }

    def test_bootstrap_is_idempotent(self, tmp_path: Path):
        pro = _make_pro(tmp_path, [
            {"netclass": "POWER_4A", "pattern": "BAT+"},
        ])
        assert bootstrap_expected_patterns(pro) is True
        # Second call must NOT mutate the file (returns False).
        assert bootstrap_expected_patterns(pro) is False


@pytest.mark.unit
class TestDriftDetection:
    def test_missing_pattern_flagged(self, tmp_path: Path):
        pro = _make_pro(tmp_path, [
            {"netclass": "POWER_4A", "pattern": "BAT+"},
            {"netclass": "POWER_4A", "pattern": "CELL1_TOP"},
        ])
        verify_netclass_patterns(pro)  # bootstrap

        # Simulate KiCAD GUI strip — remove CELL1_TOP from current list
        data = json.loads(pro.read_text())
        data["net_settings"]["netclass_patterns"] = [
            {"netclass": "POWER_4A", "pattern": "BAT+"},
        ]
        pro.write_text(json.dumps(data))

        result = verify_netclass_patterns(pro)
        assert result["success"]
        assert result["bootstrapped"] is False
        assert result["drifted"] is True
        assert result["missing"] == [
            {"netclass": "POWER_4A", "pattern": "CELL1_TOP"}
        ]
        assert result["extra"] == []

    def test_extra_pattern_flagged(self, tmp_path: Path):
        pro = _make_pro(tmp_path, [
            {"netclass": "POWER_4A", "pattern": "BAT+"},
        ])
        verify_netclass_patterns(pro)  # bootstrap

        # User added a new pattern after bootstrap — show as extra
        data = json.loads(pro.read_text())
        data["net_settings"]["netclass_patterns"].append(
            {"netclass": "POWER_4A", "pattern": "BAT-"}
        )
        pro.write_text(json.dumps(data))

        result = verify_netclass_patterns(pro)
        assert result["drifted"] is True
        assert result["missing"] == []
        assert result["extra"] == [
            {"netclass": "POWER_4A", "pattern": "BAT-"}
        ]

    def test_no_drift_when_matched(self, tmp_path: Path):
        pro = _make_pro(tmp_path, [
            {"netclass": "POWER_4A", "pattern": "BAT+"},
            {"netclass": "POWER_2A", "pattern": "USB_VBUS"},
        ])
        verify_netclass_patterns(pro)
        result = verify_netclass_patterns(pro)
        assert result["drifted"] is False
        assert result["missing"] == []
        assert result["extra"] == []


@pytest.mark.unit
class TestRestore:
    def test_restore_re_adds_missing_only(self, tmp_path: Path):
        pro = _make_pro(tmp_path, [
            {"netclass": "POWER_4A", "pattern": "BAT+"},
            {"netclass": "POWER_4A", "pattern": "CELL1_TOP"},
        ])
        verify_netclass_patterns(pro)  # bootstrap

        # Strip CELL1_TOP
        data = json.loads(pro.read_text())
        data["net_settings"]["netclass_patterns"] = [
            {"netclass": "POWER_4A", "pattern": "BAT+"},
        ]
        pro.write_text(json.dumps(data))

        result = verify_netclass_patterns(pro, restore=True)
        assert result["drifted"] is True
        assert result["restored"] == [
            {"netclass": "POWER_4A", "pattern": "CELL1_TOP"}
        ]

        # Verify the file now has both
        data2 = json.loads(pro.read_text())
        current = {
            (p["netclass"], p["pattern"])
            for p in data2["net_settings"]["netclass_patterns"]
        }
        assert current == {
            ("POWER_4A", "BAT+"),
            ("POWER_4A", "CELL1_TOP"),
        }

    def test_restore_doesnt_strip_extras(self, tmp_path: Path):
        """`restore` is additive only — never deletes patterns the
        user may have intentionally added since the bootstrap."""
        pro = _make_pro(tmp_path, [
            {"netclass": "POWER_4A", "pattern": "BAT+"},
        ])
        verify_netclass_patterns(pro)  # bootstrap

        # Drop BAT+, add BAT-
        data = json.loads(pro.read_text())
        data["net_settings"]["netclass_patterns"] = [
            {"netclass": "POWER_4A", "pattern": "BAT-"},
        ]
        pro.write_text(json.dumps(data))

        result = verify_netclass_patterns(pro, restore=True)
        # Both missing-restored and extra-flagged.
        assert result["restored"] == [
            {"netclass": "POWER_4A", "pattern": "BAT+"}
        ]
        assert result["extra"] == [
            {"netclass": "POWER_4A", "pattern": "BAT-"}
        ]

        # Final state: both present
        data2 = json.loads(pro.read_text())
        current = {
            (p["netclass"], p["pattern"])
            for p in data2["net_settings"]["netclass_patterns"]
        }
        assert current == {
            ("POWER_4A", "BAT+"),
            ("POWER_4A", "BAT-"),
        }


@pytest.mark.unit
class TestEdgeCases:
    def test_missing_pro_file_errors_gracefully(self, tmp_path: Path):
        result = verify_netclass_patterns(tmp_path / "no_such.kicad_pro")
        assert result["success"] is False

    def test_corrupt_json_errors_gracefully(self, tmp_path: Path):
        pro = tmp_path / "test.kicad_pro"
        pro.write_text("{not valid json")
        result = verify_netclass_patterns(pro)
        assert result["success"] is False
