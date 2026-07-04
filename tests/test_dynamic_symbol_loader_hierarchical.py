"""Regression tests for `_resolve_instance_path` — the helper that decides
what `(instances (project ...))` block to emit when a component is
placed via DynamicSymbolLoader.

Bug it fixes: prior code always wrote `(project "<own_stem>" (path "/<own_uuid>"))`.
On a hierarchical sub-sheet that's the wrong path — KiCad's per-sheet
annotation lookup expects `/{parent_root_uuid}/{this_sheet_uuid_as_it_appears_in_root}`
and falls back to `?` refs when it can't resolve.
"""
from __future__ import annotations

import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

PYTHON_DIR = Path(__file__).resolve().parents[1] / "python"
sys.path.insert(0, str(PYTHON_DIR))


@pytest.mark.unit
class TestResolveInstancePath:
    def _make_project(self, tmp: Path):
        """Standard 1-root + 1-sub-sheet layout with known UUIDs.

        Returns (root_sch, sub_sch, root_uuid, sub_sheet_uuid_in_root)."""
        root_uuid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        sub_sheet_uuid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        sub_own_uuid = "cccccccc-cccc-cccc-cccc-cccccccccccc"

        # Minimal .kicad_pro so _find_parent_project_sheet's glob succeeds.
        (tmp / "power_module.kicad_pro").write_text("{}")

        root_sch = tmp / "power_module.kicad_sch"
        root_sch.write_text(textwrap.dedent(f"""\
            (kicad_sch (version 20250114) (generator "test")
              (uuid "{root_uuid}")
              (lib_symbols)
              (sheet (at 40 40) (size 20 20) (fields_autoplaced yes)
                (stroke (width 0.1524) (type solid))
                (fill (color 0 0 0 0.0000))
                (uuid "{sub_sheet_uuid}")
                (property "Sheetname" "child" (at 40 39 0))
                (property "Sheetfile" "child.kicad_sch" (at 40 61 0))
              )
              (sheet_instances (path "/" (page "1")))
            )
        """))

        sub_sch = tmp / "child.kicad_sch"
        sub_sch.write_text(textwrap.dedent(f"""\
            (kicad_sch (version 20250114) (generator "test")
              (uuid "{sub_own_uuid}")
              (lib_symbols)
              (sheet_instances (path "/" (page "1")))
            )
        """))

        return root_sch, sub_sch, root_uuid, sub_sheet_uuid, sub_own_uuid

    def test_sub_sheet_resolves_to_hierarchical_path(self):
        """Placement on a sub-sheet inside a project should use
        /{root_uuid}/{sub_sheet_uuid_in_root} and the PARENT's stem."""
        from commands.dynamic_symbol_loader import _resolve_instance_path

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            _root, sub_sch, root_uuid, sub_sheet_uuid, _ = self._make_project(tmp)

            content = sub_sch.read_text()
            project_name, hier_path = _resolve_instance_path(sub_sch, content)

            assert project_name == "power_module", (
                f"project_name should be parent's stem 'power_module', got {project_name!r}"
            )
            assert hier_path == f"/{root_uuid}/{sub_sheet_uuid}", (
                f"expected /{root_uuid}/{sub_sheet_uuid}, got {hier_path!r}"
            )

    def test_root_sheet_resolves_to_standalone(self):
        """Placement on the root .kicad_sch itself (same stem as the
        .kicad_pro) should fall back to standalone form: /own_uuid + own
        stem.  Otherwise a symbol placed on root would incorrectly get a
        two-level hierarchical path.
        """
        from commands.dynamic_symbol_loader import _resolve_instance_path

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            root_sch, _, root_uuid, _, _ = self._make_project(tmp)

            content = root_sch.read_text()
            project_name, hier_path = _resolve_instance_path(root_sch, content)

            assert project_name == "power_module"
            assert hier_path == f"/{root_uuid}", (
                f"root sheet should get standalone path, got {hier_path!r}"
            )

    def test_bare_standalone_no_project(self):
        """Placement on a .kicad_sch that isn't part of any project (no
        .kicad_pro anywhere in the ancestor tree) uses standalone form
        with the file's own stem as project name."""
        from commands.dynamic_symbol_loader import _resolve_instance_path

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            sch = tmp / "orphan.kicad_sch"
            own_uuid = "dddddddd-dddd-dddd-dddd-dddddddddddd"
            sch.write_text(textwrap.dedent(f"""\
                (kicad_sch (version 20250114) (generator "test")
                  (uuid "{own_uuid}")
                  (lib_symbols)
                  (sheet_instances (path "/" (page "1")))
                )
            """))
            content = sch.read_text()
            project_name, hier_path = _resolve_instance_path(sch, content)
            assert project_name == "orphan"
            assert hier_path == f"/{own_uuid}"

    def test_sub_sheet_not_referenced_by_root_falls_back_to_standalone(self):
        """A sub-sheet .kicad_sch that lives next to a .kicad_pro but
        isn't referenced by the root .kicad_sch (edge case: file created
        before it was wired into the hierarchy) should fall back to
        standalone form rather than raise or emit a nonsense path."""
        from commands.dynamic_symbol_loader import _resolve_instance_path

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            _root, _sub, _, _, sub_own_uuid = self._make_project(tmp)

            # Create a THIRD .kicad_sch not referenced by root.
            orphan = tmp / "not_wired_in.kicad_sch"
            orphan_uuid = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
            orphan.write_text(textwrap.dedent(f"""\
                (kicad_sch (version 20250114) (generator "test")
                  (uuid "{orphan_uuid}")
                  (lib_symbols)
                  (sheet_instances (path "/" (page "1")))
                )
            """))
            content = orphan.read_text()
            project_name, hier_path = _resolve_instance_path(orphan, content)
            # Standalone fallback — project name = own stem, path = own uuid.
            assert project_name == "not_wired_in"
            assert hier_path == f"/{orphan_uuid}"


@pytest.mark.unit
class TestAddComponentEndToEnd:
    """Round-trip: place a component on a sub-sheet and confirm the
    written (instances (project ...)) block has the hierarchical
    path."""

    def test_add_component_writes_hierarchical_instances_block(self):
        from commands.dynamic_symbol_loader import DynamicSymbolLoader

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            root_uuid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
            sub_sheet_uuid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
            sub_own_uuid = "cccccccc-cccc-cccc-cccc-cccccccccccc"

            (tmp / "power_module.kicad_pro").write_text("{}")
            (tmp / "power_module.kicad_sch").write_text(textwrap.dedent(f"""\
                (kicad_sch (version 20250114) (generator "test")
                  (uuid "{root_uuid}")
                  (lib_symbols)
                  (sheet (at 40 40) (size 20 20) (fields_autoplaced yes)
                    (stroke (width 0.1524) (type solid))
                    (fill (color 0 0 0 0.0000))
                    (uuid "{sub_sheet_uuid}")
                    (property "Sheetname" "child" (at 40 39 0))
                    (property "Sheetfile" "child.kicad_sch" (at 40 61 0))
                  )
                  (sheet_instances (path "/" (page "1")))
                )
            """))

            # Minimal sub-sheet with a lib_symbols slot the loader can inject into.
            sub = tmp / "child.kicad_sch"
            sub.write_text(textwrap.dedent(f"""\
                (kicad_sch (version 20250114) (generator "test")
                  (uuid "{sub_own_uuid}")
                  (lib_symbols
                    (symbol "Device:R" (pin_numbers hide) (pin_names (offset 0))
                      (symbol "R_1_1"
                        (pin passive line (at 0 3.81 270) (length 1.27)
                          (name "~") (number "1"))
                        (pin passive line (at 0 -3.81 90) (length 1.27)
                          (name "~") (number "2"))
                      )
                    )
                  )
                  (sheet_instances (path "/" (page "1")))
                )
            """))

            loader = DynamicSymbolLoader()
            loader.create_component_instance(
                sub, "Device", "R",
                reference="R1", value="10k", x=100, y=100, unit=1,
            )

            text = sub.read_text()
            expected_path = f'(path "/{root_uuid}/{sub_sheet_uuid}"'
            assert expected_path in text, (
                f"expected hierarchical path {expected_path!r} in output; "
                f"actual instances snippet: "
                f"{text[text.find('(instances'):text.find('(instances')+300]!r}"
            )
            # Project name should be the parent's stem, NOT the sub-sheet's own.
            assert '(project "power_module"' in text, (
                "project name should be parent's stem 'power_module'"
            )
            assert '(project "child"' not in text, (
                "must NOT use sub-sheet's own stem as project name"
            )
