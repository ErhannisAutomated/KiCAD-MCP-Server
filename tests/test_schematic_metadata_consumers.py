"""Phase 2 of #230: consumers prefer the Schematic_Metadata singleton
over `.kicad_pro` for `mcp_*` keys, with `.kicad_pro` fallback when
the singleton is missing.

Covers:
- netclass_patterns.verify_netclass_patterns: expectedSource field +
  singleton precedence
- pcb_autoplacer._load_spring_classes_from_project: prefers singleton

Pure-Python schematic I/O — no pcbnew needed for these tests.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from commands import schematic_metadata as sm  # noqa: E402
from commands.netclass_patterns import verify_netclass_patterns  # noqa: E402
from commands.pcb_autoplacer import (  # noqa: E402
    _load_spring_classes_from_project,
    DEFAULT_SPRING_CLASSES,
)


_MIN_SCH = '''(kicad_sch
\t(version 20250114)
\t(generator "eeschema")
\t(generator_version "9.0")
\t(uuid "11111111-2222-3333-4444-555555555555")
\t(paper "A4")
\t(lib_symbols
\t\t(symbol "Device:R"
\t\t\t(pin_names (offset 0))
\t\t\t(exclude_from_sim no)
\t\t\t(in_bom yes)
\t\t\t(on_board yes)
\t\t\t(property "Reference" "R"
\t\t\t\t(at 0 0 0)
\t\t\t\t(effects (font (size 1.27 1.27)))
\t\t\t)
\t\t\t(property "Value" "R"
\t\t\t\t(at 0 0 0)
\t\t\t\t(effects (font (size 1.27 1.27)))
\t\t\t)
\t\t)
\t)
\t(embedded_fonts no)
)
'''


def _make_project(tmp_path: Path, pro_data: dict) -> tuple[Path, Path]:
    """Create a minimal schematic + .kicad_pro pair."""
    sch = tmp_path / "test.kicad_sch"
    pro = tmp_path / "test.kicad_pro"
    sch.write_text(_MIN_SCH, encoding="utf-8")
    pro.write_text(json.dumps(pro_data), encoding="utf-8")
    return sch, pro


class TestNetclassPatternsConsumer:
    def test_reads_expected_from_singleton(self, tmp_path):
        sch, pro = _make_project(tmp_path, {
            "net_settings": {
                "netclass_patterns": [
                    {"netclass": "POWER_4A", "pattern": "BAT+"},
                ],
            },
        })
        sm.write_metadata_key(sch, "mcp_expected_netclass_patterns", [
            {"netclass": "POWER_4A", "pattern": "BAT+"},
        ])
        r = verify_netclass_patterns(pro, sch_path=sch)
        assert r["success"], r
        assert not r["drifted"]

    def test_drift_detected_against_singleton(self, tmp_path):
        sch, pro = _make_project(tmp_path, {
            "net_settings": {
                "netclass_patterns": [
                    {"netclass": "POWER_4A", "pattern": "BAT+"},
                    # BAT- silently dropped by KiCAD
                ],
            },
        })
        sm.write_metadata_key(sch, "mcp_expected_netclass_patterns", [
            {"netclass": "POWER_4A", "pattern": "BAT+"},
            {"netclass": "POWER_4A", "pattern": "BAT-"},
        ])
        r = verify_netclass_patterns(pro, sch_path=sch)
        assert r["drifted"]
        assert {"netclass": "POWER_4A", "pattern": "BAT-"} in r["missing"]

    def test_bootstrap_writes_to_singleton(self, tmp_path):
        sch, pro = _make_project(tmp_path, {
            "net_settings": {
                "netclass_patterns": [
                    {"netclass": "POWER_4A", "pattern": "BAT+"},
                ],
            },
        })
        r = verify_netclass_patterns(pro, sch_path=sch)
        assert r["bootstrapped"]
        # And the singleton now carries the key.
        md = sm.read_metadata_json(sch, "mcp_expected_netclass_patterns")
        assert md == [{"netclass": "POWER_4A", "pattern": "BAT+"}]
        # .kicad_pro should NOT have grown the key.
        after = json.loads(pro.read_text())
        assert "mcp_expected_netclass_patterns" not in after

    def test_pro_mcp_expected_is_ignored(self, tmp_path):
        """Phase 3: .kicad_pro mcp_expected_netclass_patterns no longer read."""
        sch, pro = _make_project(tmp_path, {
            "net_settings": {
                "netclass_patterns": [
                    {"netclass": "POWER_4A", "pattern": "BAT+"},
                ],
            },
            "mcp_expected_netclass_patterns": [
                {"netclass": "POWER_4A", "pattern": "BAT-"},  # would drift
            ],
        })
        # The singleton has no key — verify bootstraps it from current
        # patterns (the .kicad_pro key is ignored entirely).
        r = verify_netclass_patterns(pro, sch_path=sch)
        assert r["bootstrapped"]
        md = sm.read_metadata_json(sch, "mcp_expected_netclass_patterns")
        assert md == [{"netclass": "POWER_4A", "pattern": "BAT+"}]

    def test_missing_sch_errors_clearly(self, tmp_path):
        _, pro = _make_project(tmp_path, {
            "net_settings": {"netclass_patterns": []},
        })
        # Delete the sch
        (tmp_path / "test.kicad_sch").unlink()
        r = verify_netclass_patterns(pro)  # no sch_path
        assert not r["success"]
        assert "schematic" in r["message"].lower()


class TestSpringClassesConsumer:
    def test_loads_from_singleton(self, tmp_path):
        sch, _ = _make_project(tmp_path, {})
        sm.write_metadata_key(sch, "mcp_spring_classes", {
            "classes": {"CUSTOM_HARD": {"spring_k": 10.0}},
            "nets": {"BAT+": "PLANE"},
        })
        classes, nets = _load_spring_classes_from_project(sch)
        assert "CUSTOM_HARD" in classes
        assert classes["CUSTOM_HARD"].spring_k == 10.0
        assert nets.get("BAT+") == "PLANE"

    def test_falls_back_to_defaults_when_singleton_missing(
        self, tmp_path,
    ):
        sch, _ = _make_project(tmp_path, {})
        classes, nets = _load_spring_classes_from_project(sch)
        # Default classes are present
        assert "DECOUPLING" in classes
        assert "LOCAL_SIGNAL" in classes
        assert nets == {}

    def test_no_sch_path_returns_defaults(self, tmp_path):
        classes, nets = _load_spring_classes_from_project(None)
        assert "DECOUPLING" in classes
        assert nets == {}

    def test_pro_keys_are_ignored(self, tmp_path):
        """Phase 3: .kicad_pro mcp_spring_classes are no longer read."""
        sch, pro = _make_project(tmp_path, {
            "mcp_spring_classes": {
                "classes": {"FROM_PRO": {"spring_k": 99.0}},
                "nets": {"BAT+": "PLANE"},
            },
        })
        classes, nets = _load_spring_classes_from_project(sch)
        assert "FROM_PRO" not in classes
        assert nets == {}
