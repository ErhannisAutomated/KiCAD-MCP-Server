"""
Regression test: PinLocator cache must invalidate when the schematic file
changes on disk.

Bug: ConnectionManager._pin_locator is a class-level singleton holding a
PinLocator instance with three caches keyed by file path
(_schematic_cache, _sexp_cache, pin_definition_cache). Once populated for
a given path, the caches were never refreshed — so a subsequent write
made through a different code path (DynamicSymbolLoader called from a
script while the server is running, kicad-skip reload outside the
locator, etc.) would leave the locator looking at the *old* component
list. connect_pins, which depends on get_pin_net → get_pin_location,
then reported "N failed" for every newly-added pin even though the
file on disk was correct and the same pin was directly resolvable
through MCP add_schematic_net_label.

Fix: PinLocator now stat()s schematic_path at every public entry point
and clears all path-keyed cache entries when mtime advances. Verified
end-to-end here.
"""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest

PYTHON_DIR = Path(__file__).parent.parent / "python"
TEMPLATES_DIR = PYTHON_DIR / "templates"
sys.path.insert(0, str(PYTHON_DIR))


@pytest.mark.integration
class TestPinLocatorCacheInvalidation:
    """File-mtime-driven cache invalidation."""

    def _bump_mtime(self, path: Path) -> None:
        """Advance the file's mtime by 2 s so it's > any cached value."""
        st = path.stat()
        os.utime(path, (st.st_atime, st.st_mtime + 2))

    def test_pin_added_externally_is_resolved_after_cache_invalidation(self):
        from commands.dynamic_symbol_loader import DynamicSymbolLoader
        from commands.pin_locator import PinLocator

        with tempfile.TemporaryDirectory() as tmp:
            sch_path = Path(tmp) / "test.kicad_sch"
            shutil.copy(TEMPLATES_DIR / "template_with_symbols.kicad_sch", sch_path)

            locator = PinLocator()

            # Prime the cache by looking up a known pin on the template.
            # The template has _TEMPLATE_R placed; this populates _schematic_cache,
            # _sexp_cache, and pin_definition_cache for sch_path.
            primed = locator.get_pin_location(sch_path, "_TEMPLATE_R", "1")
            assert primed is not None, "Template should already have _TEMPLATE_R"

            sch_key = str(sch_path)
            assert sch_key in locator._schematic_cache
            assert sch_key in locator._sexp_cache
            assert any(
                k.startswith(f"{sch_key}:") for k in locator.pin_definition_cache
            )

            # Add a new component out-of-band, the way place_charger.py does:
            # straight through DynamicSymbolLoader, no MCP round-trip, no
            # invalidation hint to the locator.
            DynamicSymbolLoader().add_component(
                sch_path, "Device", "C",
                reference="C99", value="100nF",
                x=200, y=200, rotation=0,
            )
            self._bump_mtime(sch_path)

            # Without invalidation, this returns None: the locator's cached
            # Schematic object has no C99. With the fix, the mtime advance
            # triggers cache reload and the new pin is found.
            result = locator.get_pin_location(sch_path, "C99", "1")
            assert result is not None, (
                "PinLocator did not pick up C99 added after the cache was primed; "
                "mtime-based invalidation is not firing"
            )

    def test_cache_persists_when_file_unchanged(self):
        """Negative case: cache must NOT be flushed when mtime is unchanged."""
        from commands.pin_locator import PinLocator

        with tempfile.TemporaryDirectory() as tmp:
            sch_path = Path(tmp) / "test.kicad_sch"
            shutil.copy(TEMPLATES_DIR / "template_with_symbols.kicad_sch", sch_path)

            locator = PinLocator()
            primed = locator.get_pin_location(sch_path, "_TEMPLATE_R", "1")
            assert primed is not None

            sch_key = str(sch_path)
            cached_schematic = locator._schematic_cache.get(sch_key)
            cached_sexp = locator._sexp_cache.get(sch_key)
            assert cached_schematic is not None
            assert cached_sexp is not None

            # Subsequent call without modification: same objects must be re-used.
            locator.get_pin_location(sch_path, "_TEMPLATE_R", "1")
            assert locator._schematic_cache.get(sch_key) is cached_schematic
            assert locator._sexp_cache.get(sch_key) is cached_sexp
