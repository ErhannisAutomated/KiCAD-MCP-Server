"""
Pin Locator for KiCad Schematics

Discovers pin locations on symbol instances, accounting for position, rotation, and mirroring.
Uses S-expression parsing to extract pin data from symbol definitions.
"""

import logging
import math
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import sexpdata
from sexpdata import Symbol
from skip import Schematic

logger = logging.getLogger("kicad_interface")

# KiCad lib_symbols sub-symbol naming: "<base>_<unit>_<convert>".
# unit=0 is "common to all units" (rare); convert is body style for de-Morgan.
_SUBSYM_UNIT_RE = re.compile(r"_(\d+)_\d+$")


class PinLocator:
    """Locate pins on symbol instances in KiCad schematics"""

    def __init__(self) -> None:
        """Initialize pin locator with empty cache"""
        self.pin_definition_cache = {}  # Cache: "path:lib_id" -> pin_data
        self._schematic_cache: Dict[str, object] = {}  # Cache: path -> loaded Schematic
        self._sexp_cache: Dict[str, Any] = {}  # Cache: path -> parsed sexpdata (mirror-aware)
        # Per-path mtime watch; entries cleared when the file changes on disk.
        # Without this, ConnectionManager's class-level singleton holds stale state
        # across writes done outside the MCP request that populated the cache
        # (e.g. DynamicSymbolLoader.add_component called from a script while the
        # server is running, or a second tool that wrote via a different code path).
        self._mtime_cache: Dict[str, float] = {}

    def _invalidate_if_changed(self, schematic_path: Path) -> None:
        """Drop cached state for ``schematic_path`` if its mtime advanced."""
        path_key = str(schematic_path)
        try:
            mtime = schematic_path.stat().st_mtime
        except OSError:
            return  # missing file → leave caches alone, the open() will surface the error
        prev = self._mtime_cache.get(path_key)
        if prev is not None and mtime <= prev:
            return
        # File changed (or first time we've seen it post-write): clear all path-keyed entries.
        self._schematic_cache.pop(path_key, None)
        self._sexp_cache.pop(path_key, None)
        prefix = f"{path_key}:"
        for k in [k for k in self.pin_definition_cache if k.startswith(prefix)]:
            self.pin_definition_cache.pop(k, None)
        self._mtime_cache[path_key] = mtime

    @staticmethod
    def parse_pins_per_unit(symbol_def: list) -> Dict[int, Dict[str, Dict[str, Any]]]:
        """Walk a lib_symbols (symbol "Foo" …) node and return
        ``{unit_number: {pin_number: pin_data}}``.

        KiCad lib_symbols groups pins by sub-symbol named like
        ``<base>_<unit>_<convert>`` (e.g. ``FDS9926A_1_1``,
        ``FDS9926A_2_1``).  The (unit N) on each placed (symbol …)
        block selects one of these.  Single-unit symbols use unit 1
        with sub-symbol name like ``R_0_1`` / ``R_1_1``; convert is
        body style for symbols with de-Morgan variants.

        Sub-symbols whose unit field is "0" hold graphics common to
        all units — their pins (rare) are mirrored into every unit's
        dict so callers don't have to special-case them.
        """
        by_unit: Dict[int, Dict[str, Dict[str, Any]]] = {}
        if not (isinstance(symbol_def, list) and len(symbol_def) > 1):
            return by_unit

        def collect_pins_direct(sub: list) -> Dict[str, Dict[str, Any]]:
            out: Dict[str, Dict[str, Any]] = {}
            for child in sub[2:]:
                if not (isinstance(child, list) and child and child[0] == Symbol("pin")):
                    continue
                pin_data: Dict[str, Any] = {
                    "x": 0.0,
                    "y": 0.0,
                    "angle": 0.0,
                    "length": 0.0,
                    "name": "",
                    "number": "",
                    "type": str(child[1]) if len(child) > 1 else "passive",
                }
                for sp in child[2:]:
                    if not (isinstance(sp, list) and sp):
                        continue
                    if sp[0] == Symbol("at") and len(sp) >= 3:
                        try:
                            pin_data["x"] = float(sp[1])
                            pin_data["y"] = float(sp[2])
                            if len(sp) >= 4:
                                pin_data["angle"] = float(sp[3])
                        except (TypeError, ValueError):
                            pass
                    elif sp[0] == Symbol("length") and len(sp) >= 2:
                        try:
                            pin_data["length"] = float(sp[1])
                        except (TypeError, ValueError):
                            pass
                    elif sp[0] == Symbol("name") and len(sp) >= 2:
                        pin_data["name"] = str(sp[1]).strip('"')
                    elif sp[0] == Symbol("number") and len(sp) >= 2:
                        pin_data["number"] = str(sp[1]).strip('"')
                if pin_data["number"]:
                    out[pin_data["number"]] = pin_data
            return out

        for sub in symbol_def[2:]:
            if not (isinstance(sub, list) and len(sub) > 1 and sub[0] == Symbol("symbol")):
                continue
            sub_name = str(sub[1]).strip('"') if isinstance(sub[1], str) else ""
            m = _SUBSYM_UNIT_RE.search(sub_name)
            unit = int(m.group(1)) if m else 1
            sub_pins = collect_pins_direct(sub)
            if sub_pins:
                by_unit.setdefault(unit, {}).update(sub_pins)

        if 0 in by_unit:
            shared = by_unit.pop(0)
            for u in by_unit:
                for pn, pd in shared.items():
                    by_unit[u].setdefault(pn, pd)

        if not by_unit:
            # Fallback: no sub-symbol unit suffix matched (older / hand-rolled
            # symbol defs).  Treat every pin found anywhere as unit 1.
            all_pins = PinLocator.parse_symbol_definition(symbol_def)
            if all_pins:
                by_unit[1] = all_pins
        return by_unit

    @staticmethod
    def parse_symbol_definition(symbol_def: list) -> Dict[str, Dict]:
        """
        Parse a symbol definition from lib_symbols to extract pin information

        Args:
            symbol_def: S-expression list representing symbol definition

        Returns:
            Dictionary mapping pin number -> pin data:
            {
                "1": {"x": 0, "y": 3.81, "angle": 270, "length": 1.27, "name": "~", "type": "passive"},
                "2": {"x": 0, "y": -3.81, "angle": 90, "length": 1.27, "name": "~", "type": "passive"}
            }
        """
        pins: Dict[str, Dict[str, Any]] = {}

        def extract_pins_recursive(sexp: Any) -> None:
            """Recursively search for pin definitions"""
            if not isinstance(sexp, list):
                return

            # Check if this is a pin definition
            if len(sexp) > 0 and sexp[0] == Symbol("pin"):
                # Pin format: (pin type shape (at x y angle) (length len) (name "name") (number "num"))
                pin_data = {
                    "x": 0,
                    "y": 0,
                    "angle": 0,
                    "length": 0,
                    "name": "",
                    "number": "",
                    "type": str(sexp[1]) if len(sexp) > 1 else "passive",
                }

                # Extract pin attributes
                for item in sexp:
                    if isinstance(item, list) and len(item) > 0:
                        if item[0] == Symbol("at") and len(item) >= 3:
                            pin_data["x"] = float(item[1])
                            pin_data["y"] = float(item[2])
                            if len(item) >= 4:
                                pin_data["angle"] = float(item[3])

                        elif item[0] == Symbol("length") and len(item) >= 2:
                            pin_data["length"] = float(item[1])

                        elif item[0] == Symbol("name") and len(item) >= 2:
                            pin_data["name"] = str(item[1]).strip('"')

                        elif item[0] == Symbol("number") and len(item) >= 2:
                            pin_data["number"] = str(item[1]).strip('"')

                # Store by pin number
                if pin_data["number"]:
                    pins[pin_data["number"]] = pin_data

            # Recurse into sublists
            for item in sexp:
                if isinstance(item, list):
                    extract_pins_recursive(item)

        extract_pins_recursive(symbol_def)
        return pins

    def get_symbol_pins(self, schematic_path: Path, lib_id: str) -> Dict[str, Dict]:
        """
        Get pin definitions for a symbol from the schematic's lib_symbols section

        Args:
            schematic_path: Path to .kicad_sch file
            lib_id: Library identifier (e.g., "Device:R", "MCU_ST_STM32F1:STM32F103C8Tx")

        Returns:
            Dictionary mapping pin number -> pin data
        """
        self._invalidate_if_changed(schematic_path)
        # Check cache
        cache_key = f"{schematic_path}:{lib_id}"
        if cache_key in self.pin_definition_cache:
            logger.debug(f"Using cached pin data for {lib_id}")
            return self.pin_definition_cache[cache_key]

        try:
            # Read schematic
            with open(schematic_path, "r", encoding="utf-8") as f:
                sch_content = f.read()

            sch_data = sexpdata.loads(sch_content)

            # Find lib_symbols section
            lib_symbols = None
            for item in sch_data:
                if isinstance(item, list) and len(item) > 0 and item[0] == Symbol("lib_symbols"):
                    lib_symbols = item
                    break

            if not lib_symbols:
                logger.error("No lib_symbols section found in schematic")
                return {}

            # Find the specific symbol definition.
            # KiCad lib_symbols may use a different name than the instance lib_id:
            #   instance lib_id:  "stat-tis-custom:BAT_18650"
            #   lib_symbols name: "BAT_18650_3"  (prefix stripped, unit suffix added)
            # Strategy: exact match first, then bare-name prefix match.
            bare_name = lib_id.split(":")[-1] if ":" in lib_id else lib_id

            best_match = None
            for item in lib_symbols[1:]:
                if not (isinstance(item, list) and len(item) > 1 and item[0] == Symbol("symbol")):
                    continue
                symbol_name = str(item[1]).strip('"')
                if symbol_name == lib_id:
                    best_match = item
                    break
                if best_match is None:
                    sn_bare = symbol_name.split(":")[-1] if ":" in symbol_name else symbol_name
                    if sn_bare == bare_name or (
                        sn_bare.startswith(bare_name)
                        and len(sn_bare) > len(bare_name)
                        and sn_bare[len(bare_name)] == "_"
                        and sn_bare[len(bare_name) + 1 :].isdigit()
                    ):
                        best_match = item

            if best_match is not None:
                matched_name = str(best_match[1]).strip('"')
                pins = self.parse_symbol_definition(best_match)
                self.pin_definition_cache[cache_key] = pins
                if matched_name != lib_id:
                    logger.info(
                        f"Matched {lib_id} → lib_symbols '{matched_name}' ({len(pins)} pins)"
                    )
                else:
                    logger.info(f"Extracted {len(pins)} pins from {lib_id}")
                return pins

            logger.warning(f"Symbol {lib_id} not found in lib_symbols")
            return {}

        except Exception as e:
            logger.error(f"Error getting symbol pins: {e}")
            import traceback

            logger.error(traceback.format_exc())
            return {}

    def get_pins_per_unit(
        self, schematic_path: Path, lib_id: str
    ) -> Dict[int, Dict[str, Dict[str, Any]]]:
        """Cached wrapper around ``parse_pins_per_unit``: locate the
        lib_symbols definition matching ``lib_id`` and return its pins
        grouped by unit.  See ``parse_pins_per_unit`` for the format.

        Uses the same name-matching as ``get_symbol_pins`` (exact
        match, then bare-name prefix) so e.g. instance lib_id
        ``stat-tis-custom:BAT_18650`` resolves to lib_symbols entry
        ``BAT_18650_3``.
        """
        self._invalidate_if_changed(schematic_path)
        cache_key = f"{schematic_path}:per_unit:{lib_id}"
        if cache_key in self.pin_definition_cache:
            return self.pin_definition_cache[cache_key]
        try:
            with open(schematic_path, "r", encoding="utf-8") as f:
                sch_data = sexpdata.loads(f.read())
            lib_symbols = None
            for item in sch_data:
                if isinstance(item, list) and item and item[0] == Symbol("lib_symbols"):
                    lib_symbols = item
                    break
            if not lib_symbols:
                return {}
            bare_name = lib_id.split(":")[-1] if ":" in lib_id else lib_id
            best_match = None
            for item in lib_symbols[1:]:
                if not (isinstance(item, list) and len(item) > 1 and item[0] == Symbol("symbol")):
                    continue
                symbol_name = str(item[1]).strip('"')
                if symbol_name == lib_id:
                    best_match = item
                    break
                if best_match is None:
                    sn_bare = symbol_name.split(":")[-1] if ":" in symbol_name else symbol_name
                    if sn_bare == bare_name or (
                        sn_bare.startswith(bare_name)
                        and len(sn_bare) > len(bare_name)
                        and sn_bare[len(bare_name)] == "_"
                        and sn_bare[len(bare_name) + 1 :].isdigit()
                    ):
                        best_match = item
            if best_match is None:
                return {}
            by_unit = PinLocator.parse_pins_per_unit(best_match)
            self.pin_definition_cache[cache_key] = by_unit
            return by_unit
        except Exception as e:
            logger.error(f"get_pins_per_unit({lib_id}): {e}")
            return {}

    def _find_placed_instances(
        self, schematic_path: Path, symbol_reference: str
    ) -> List[Tuple[float, float, float, bool, bool, str, int]]:
        """Return every placed (symbol …) block whose Reference matches
        ``symbol_reference``, as ``(x, y, rotation, mirror_x, mirror_y,
        lib_id, unit)``.  Used to disambiguate multi-unit components
        where the same reference appears on multiple placed blocks.
        """
        self._invalidate_if_changed(schematic_path)
        sch_key = str(schematic_path)
        try:
            if sch_key not in self._sexp_cache:
                with open(schematic_path, "r", encoding="utf-8") as f:
                    self._sexp_cache[sch_key] = sexpdata.loads(f.read())
        except Exception as e:
            logger.error(f"_find_placed_instances: failed to parse {schematic_path}: {e}")
            return []

        sexp = self._sexp_cache[sch_key]
        results: List[Tuple[float, float, float, bool, bool, str, int]] = []
        for top in sexp:
            if not (isinstance(top, list) and len(top) > 1 and top[0] == Symbol("symbol")):
                continue
            ref = None
            x = y = rot = 0.0
            mx = my = False
            lib_id = ""
            unit = 1
            for sub in top[1:]:
                if not (isinstance(sub, list) and sub):
                    continue
                tag = sub[0]
                if tag == Symbol("at"):
                    if len(sub) >= 3:
                        try:
                            x = float(sub[1])
                            y = float(sub[2])
                        except (TypeError, ValueError):
                            pass
                    if len(sub) >= 4:
                        try:
                            rot = float(sub[3])
                        except (TypeError, ValueError):
                            pass
                elif tag == Symbol("lib_id") and len(sub) >= 2:
                    lib_id = str(sub[1]).strip('"')
                elif tag == Symbol("mirror") and len(sub) >= 2:
                    mv = str(sub[1])
                    if mv == "x":
                        mx = True
                    elif mv == "y":
                        my = True
                elif tag == Symbol("unit") and len(sub) >= 2:
                    try:
                        unit = int(sub[1])
                    except (TypeError, ValueError):
                        pass
                elif tag == Symbol("property") and len(sub) >= 3:
                    if str(sub[1]).strip('"') == "Reference":
                        ref = str(sub[2]).strip('"').rstrip("_")
            if ref == symbol_reference:
                results.append((x, y, rot, mx, my, lib_id, unit))
        return results

    @staticmethod
    def _resolve_pin_in_units(
        per_unit: Dict[int, Dict[str, Dict[str, Any]]], pin_number: str
    ) -> Tuple[Optional[int], Optional[str], Optional[Dict[str, Any]]]:
        """Find which (unit, resolved-pin-number, pin_data) the lookup
        key resolves to.  Tries exact pin-number match first, then pin
        name match across units.  Returns ``(None, None, None)`` if not
        found."""
        for u, pins in per_unit.items():
            if pin_number in pins:
                return u, pin_number, pins[pin_number]
        for u, pins in per_unit.items():
            for num, data in pins.items():
                if data.get("name") == pin_number:
                    return u, num, data
        return None, None, None

    @staticmethod
    def rotate_point(x: float, y: float, angle_degrees: float) -> Tuple[float, float]:
        """
        Rotate a point around the origin

        Args:
            x: X coordinate
            y: Y coordinate
            angle_degrees: Rotation angle in degrees (counterclockwise)

        Returns:
            (rotated_x, rotated_y)
        """
        if angle_degrees == 0:
            return (x, y)

        angle_rad = math.radians(angle_degrees)
        cos_a = math.cos(angle_rad)
        sin_a = math.sin(angle_rad)

        # Standard counter-clockwise rotation (math convention, Y-up).
        # Callers are responsible for any y-axis negation required to convert
        # library coordinates (y-up) to schematic coordinates (y-down) before
        # passing values here — see get_pin_location and _transform_local_point.
        rotated_x = x * cos_a - y * sin_a
        rotated_y = x * sin_a + y * cos_a

        return (rotated_x, rotated_y)

    def _get_lib_id(self, schematic_path: Path, symbol_reference: str) -> Optional[str]:
        """Helper: return the lib_id string for a placed symbol"""
        try:
            self._invalidate_if_changed(schematic_path)
            sch_key = str(schematic_path)
            if sch_key not in self._schematic_cache:
                self._schematic_cache[sch_key] = Schematic(sch_key)
            sch = self._schematic_cache[sch_key]
            for symbol in sch.symbol:
                if symbol.property.Reference.value.rstrip("_") == symbol_reference:
                    return symbol.lib_id.value if hasattr(symbol, "lib_id") else None
        except Exception:
            pass
        return None

    def _get_symbol_transform(
        self, schematic_path: Path, symbol_reference: str
    ) -> Optional[Tuple[float, float, float, bool, bool, str]]:
        """
        Read symbol position, rotation, mirror flags, and lib_id directly from the
        .kicad_sch file via sexpdata (authoritative — not kicad-skip cache, which
        does not reflect mirror/rotation changes made by rotate_schematic_component).

        Returns (x, y, rotation, mirror_x, mirror_y, lib_id) or None.
        """
        import sexpdata as _sexpdata
        from commands.wire_dragger import WireDragger

        self._invalidate_if_changed(schematic_path)
        sch_key = str(schematic_path)
        try:
            if sch_key not in self._sexp_cache:
                with open(schematic_path, "r", encoding="utf-8") as f:
                    self._sexp_cache[sch_key] = _sexpdata.loads(f.read())
        except Exception as e:
            logger.error(f"_get_symbol_transform: failed to parse {schematic_path}: {e}")
            return None

        found = WireDragger.find_symbol(self._sexp_cache[sch_key], symbol_reference)
        if found is None:
            return None

        _, sym_x, sym_y, rotation, lib_id, mirror_x, mirror_y = found
        return sym_x, sym_y, rotation, mirror_x, mirror_y, lib_id

    def get_pin_angle(
        self, schematic_path: Path, symbol_reference: str, pin_number: str
    ) -> Optional[float]:
        """
        Get the outward angle of a pin endpoint in degrees (0=right, 90=up, 180=left, 270=down).
        This is the direction a wire stub must extend to stay connected to the pin.

        Accounts for mirror flags read directly from the .kicad_sch file,
        and selects the placed unit that owns the requested pin (so
        multi-unit symbols give the right answer for unit-N pins).

        Returns angle in degrees, or None if pin not found.
        """
        try:
            instances = self._find_placed_instances(schematic_path, symbol_reference)
            if not instances:
                return None
            lib_id = next((inst[5] for inst in instances if inst[5]), None)
            if not lib_id:
                return None

            per_unit = self.get_pins_per_unit(schematic_path, lib_id)
            owning_unit, _resolved_num, pin_data = self._resolve_pin_in_units(
                per_unit, pin_number
            )
            if pin_data is None:
                # Fallback: lib_symbols had no per-unit structure we
                # could parse — use whole-symbol pin set + first instance.
                pins = self.get_symbol_pins(schematic_path, lib_id)
                if pin_number not in pins:
                    matched_num = next(
                        (num for num, data in pins.items() if data.get("name") == pin_number),
                        None,
                    )
                    if matched_num:
                        pin_number = matched_num
                    else:
                        return None
                pin_data = pins[pin_number]
                inst = instances[0]
            else:
                inst = next((i for i in instances if i[6] == owning_unit), None)
                if inst is None:
                    logger.warning(
                        f"Pin {pin_number} on {symbol_reference} is owned by unit "
                        f"{owning_unit}, which is not placed"
                    )
                    return None

            _, _, symbol_rotation, mirror_x, mirror_y, _, _ = inst
            pin_def_angle = pin_data.get("angle", 0)

            # NOTE: pin_world_xy Y-flips POSITIONS (lib Y-up → screen Y-down).
            # We do NOT Y-flip the angle here. Callers use the formula
            #   stub.x = pin.x + d * cos(angle)
            #   stub.y = pin.y - d * sin(angle)
            # The `-sin` already compensates for Y-down screen coords, so the
            # angle stays in lib Y-up convention end-to-end. (An earlier version
            # negated the angle here, which mis-cancelled the `-sin` and produced
            # wire stubs going UP for bottom pins / DOWN for top pins.)

            # eeschema (symbol.h:43-44):
            #   (mirror x) = SYM_MIRROR_X = TRANSFORM(1,0,0,-1) → negates Y →
            #     reflect angle across X axis → -angle.
            #   (mirror y) = SYM_MIRROR_Y = TRANSFORM(-1,0,0,1) → negates X →
            #     reflect angle across Y axis → 180 - angle.
            if mirror_x:
                pin_def_angle = (-pin_def_angle) % 360
            if mirror_y:
                pin_def_angle = (180 - pin_def_angle) % 360

            # In screen-stub-angle space (where dx = cos θ and dy_screen = -sin θ),
            # rotating a symbol by `symbol_rotation` adds that rotation to the
            # pin's angle. Derivation: the lib direction (cos θ, sin θ) becomes
            # screen direction (cos θ, -sin θ) after Y-flip, then `_rotate(...,
            # -rotation)` produces screen vector (cos(θ+rotation), -sin(θ+rotation)),
            # i.e. the screen-stub-angle increases by `rotation`. (An earlier
            # version subtracted the rotation; that worked for rot=0 only and
            # silently produced 180°-wrong stubs on rotated symbols.)
            absolute_angle = (pin_def_angle + symbol_rotation) % 360
            # KiCad library pin (at x y angle) places the connecting end at (x,y)
            # with `angle` pointing FROM the connecting end INTO the symbol body
            # (i.e. inward). Callers want the outward direction so a wire stub
            # extends away from the body — flip 180°.
            return (absolute_angle + 180) % 360

        except Exception:
            return None

    def get_pin_location(
        self, schematic_path: Path, symbol_reference: str, pin_number: str
    ) -> Optional[List[float]]:
        """
        Get the absolute location of a pin on a symbol instance.

        For multi-unit symbols (one reference, multiple placed (symbol …)
        blocks each with their own (unit N)), the locator identifies
        which unit owns the requested pin (via lib_symbols' sub-symbol
        naming `<base>_<unit>_<convert>`) and transforms against THAT
        unit's placed instance — not the first one found.

        Args:
            schematic_path: Path to .kicad_sch file
            symbol_reference: Symbol reference designator (e.g., "R1", "U1")
            pin_number: Pin number/identifier (e.g., "1", "2", "GND", "VCC")

        Returns:
            [x, y] absolute coordinates of the pin, or None if not found
        """
        try:
            self._invalidate_if_changed(schematic_path)
            instances = self._find_placed_instances(schematic_path, symbol_reference)
            if not instances:
                logger.error(f"Symbol {symbol_reference} not found in schematic")
                return None

            lib_id = next((inst[5] for inst in instances if inst[5]), None)
            if not lib_id:
                logger.error(f"Symbol {symbol_reference} has no lib_id")
                return None

            per_unit = self.get_pins_per_unit(schematic_path, lib_id)
            owning_unit, resolved_num, pin_data = self._resolve_pin_in_units(
                per_unit, pin_number
            )
            if pin_data is None:
                # Per-unit parse came back empty (e.g. an unusual lib_symbol);
                # fall back to whole-symbol pin set + first placed instance.
                pins = self.get_symbol_pins(schematic_path, lib_id)
                if not pins:
                    logger.error(f"No pin definitions found for {lib_id}")
                    return None
                if pin_number not in pins:
                    matched_num = next(
                        (num for num, data in pins.items() if data.get("name") == pin_number),
                        None,
                    )
                    if matched_num:
                        logger.debug(
                            f"Resolved pin name '{pin_number}' to '{matched_num}' on {symbol_reference}"
                        )
                        pin_number = matched_num
                    else:
                        logger.error(
                            f"Pin {pin_number} not found on {symbol_reference}. "
                            f"Available pins: {list(pins.keys())} "
                            f"(names: {[d.get('name','') for d in pins.values()]})"
                        )
                        return None
                pin_data = pins[pin_number]
                inst = instances[0]
            else:
                if resolved_num != pin_number:
                    logger.debug(
                        f"Resolved pin name '{pin_number}' to '{resolved_num}' on {symbol_reference}"
                    )
                    pin_number = resolved_num
                inst = next((i for i in instances if i[6] == owning_unit), None)
                if inst is None:
                    logger.warning(
                        f"Pin {pin_number} on {symbol_reference} is owned by unit "
                        f"{owning_unit}, which has no placed instance"
                    )
                    return None

            symbol_x, symbol_y, symbol_rotation, mirror_x, mirror_y, _, unit_n = inst
            logger.debug(
                f"Symbol {symbol_reference} unit {unit_n}: pos=({symbol_x}, {symbol_y}), "
                f"rot={symbol_rotation}, mirror_x={mirror_x}, mirror_y={mirror_y}, "
                f"lib_id={lib_id}"
            )

            from commands.wire_dragger import WireDragger

            abs_x, abs_y = WireDragger.pin_world_xy(
                pin_data["x"],
                pin_data["y"],
                symbol_x,
                symbol_y,
                symbol_rotation,
                mirror_x,
                mirror_y,
            )

            logger.info(
                f"Pin {symbol_reference}/{pin_number} (unit {unit_n}) located at ({abs_x}, {abs_y})"
            )
            return [abs_x, abs_y]

        except Exception as e:
            logger.error(f"Error getting pin location: {e}")
            import traceback

            logger.error(traceback.format_exc())
            return None

    def get_all_symbol_pins(
        self, schematic_path: Path, symbol_reference: str
    ) -> Dict[str, List[float]]:
        """
        Get locations of all pins on a symbol instance

        Args:
            schematic_path: Path to .kicad_sch file
            symbol_reference: Symbol reference designator (e.g., "R1", "U1")

        Returns:
            Dictionary mapping pin number -> [x, y] coordinates
        """
        try:
            # Load schematic (use cache)
            self._invalidate_if_changed(schematic_path)
            sch_key = str(schematic_path)
            if sch_key not in self._schematic_cache:
                self._schematic_cache[sch_key] = Schematic(sch_key)
            sch = self._schematic_cache[sch_key]

            # Find symbol
            target_symbol = None
            for symbol in sch.symbol:
                if symbol.property.Reference.value.rstrip("_") == symbol_reference:
                    target_symbol = symbol
                    break

            if not target_symbol:
                logger.error(f"Symbol {symbol_reference} not found")
                return {}

            # Get lib_id
            lib_id = target_symbol.lib_id.value if hasattr(target_symbol, "lib_id") else None
            if not lib_id:
                logger.error(f"Symbol {symbol_reference} has no lib_id")
                return {}

            # Get pin definitions
            pins = self.get_symbol_pins(schematic_path, lib_id)
            if not pins:
                return {}

            # Calculate location for each pin
            result = {}
            for pin_num in pins.keys():
                location = self.get_pin_location(schematic_path, symbol_reference, pin_num)
                if location:
                    result[pin_num] = location

            logger.info(f"Located {len(result)} pins on {symbol_reference}")
            return result

        except Exception as e:
            logger.error(f"Error getting all symbol pins: {e}")
            return {}


if __name__ == "__main__":
    # Test pin location discovery
    import shutil
    import sys
    from pathlib import Path

    from commands.component_schematic import ComponentManager
    from commands.schematic import SchematicManager

    sys.path.insert(0, str(Path(__file__).parent.parent))

    print("=" * 80)
    print("PIN LOCATOR TEST")
    print("=" * 80)

    # Create test schematic with components (cross-platform temp directory)
    test_path = Path(tempfile.gettempdir()) / "test_pin_locator.kicad_sch"
    template_path = Path(__file__).parent.parent / "templates" / "template_with_symbols.kicad_sch"

    shutil.copy(template_path, test_path)
    print(f"\n✓ Created test schematic: {test_path}")

    # Add some components
    print("\n[1/4] Adding test components...")
    sch = SchematicManager.load_schematic(str(test_path))

    # Add resistor at (100, 100), rotation 0
    r1_def = {
        "type": "R",
        "reference": "R1",
        "value": "10k",
        "x": 100,
        "y": 100,
        "rotation": 0,
    }
    ComponentManager.add_component(sch, r1_def, test_path)

    # Add capacitor at (150, 100), rotation 90
    c1_def = {
        "type": "C",
        "reference": "C1",
        "value": "100nF",
        "x": 150,
        "y": 100,
        "rotation": 90,
    }
    ComponentManager.add_component(sch, c1_def, test_path)

    SchematicManager.save_schematic(sch, str(test_path))
    print("  ✓ Added R1 and C1")

    # Test pin locator
    print("\n[2/4] Testing pin location discovery...")
    locator = PinLocator()

    # Find R1 pins
    r1_pin1 = locator.get_pin_location(test_path, "R1", "1")
    r1_pin2 = locator.get_pin_location(test_path, "R1", "2")

    print(f"  R1 pin 1: {r1_pin1}")
    print(f"  R1 pin 2: {r1_pin2}")

    # Find C1 pins (rotated 90 degrees)
    c1_pin1 = locator.get_pin_location(test_path, "C1", "1")
    c1_pin2 = locator.get_pin_location(test_path, "C1", "2")

    print(f"  C1 pin 1: {c1_pin1}")
    print(f"  C1 pin 2: {c1_pin2}")

    # Test get all pins
    print("\n[3/4] Testing get all pins...")
    r1_all_pins = locator.get_all_symbol_pins(test_path, "R1")
    print(f"  R1 all pins: {r1_all_pins}")

    c1_all_pins = locator.get_all_symbol_pins(test_path, "C1")
    print(f"  C1 all pins: {c1_all_pins}")

    # Verify results
    print("\n[4/4] Verification...")
    success = True

    if not r1_pin1 or not r1_pin2:
        print("  ✗ Failed to locate R1 pins")
        success = False
    else:
        print("  ✓ R1 pins located")

    if not c1_pin1 or not c1_pin2:
        print("  ✗ Failed to locate C1 pins")
        success = False
    else:
        print("  ✓ C1 pins located")

    # Check rotation (C1 pins should be rotated 90 degrees from R1)
    if r1_pin1 and c1_pin1:
        # R1 is not rotated, pins should be at y offset from symbol center
        # C1 is rotated 90°, pins should be at x offset from symbol center
        print(f"\n  Pin offset analysis:")
        print(f"    R1 (0°):  pin 1 y-offset = {r1_pin1[1] - 100}")
        print(f"    C1 (90°): pin 1 x-offset = {c1_pin1[0] - 150}")

    print("\n" + "=" * 80)
    if success:
        print("✅ PIN LOCATOR TEST PASSED!")
    else:
        print("❌ PIN LOCATOR TEST FAILED!")
    print("=" * 80)
    print(f"\nTest schematic saved: {test_path}")
