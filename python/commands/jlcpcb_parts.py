"""
JLCPCB Parts Database Manager

Manages local SQLite database of JLCPCB parts for fast searching
and component selection.
"""

import json
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("kicad_interface")


class JLCPCBPartsManager:
    """
    Manages local database of JLCPCB parts

    Provides fast parametric search, filtering, and package-to-footprint mapping.
    """

    def __init__(self, db_path: Optional[str] = None):
        """
        Initialize parts database manager

        Args:
            db_path: Path to SQLite database file (default: data/jlcpcb_parts.db)
        """
        if db_path is None:
            # Default to data directory in project root
            project_root = Path(__file__).parent.parent.parent
            data_dir = project_root / "data"
            data_dir.mkdir(exist_ok=True)
            db_path = str(data_dir / "jlcpcb_parts.db")

        self.db_path = db_path
        self.conn: Optional[sqlite3.Connection] = None
        self._init_database()

    def _init_database(self) -> None:
        """Initialize SQLite database with schema"""
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row  # Return rows as dicts

        cursor = self.conn.cursor()

        # Create components table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS components (
                lcsc TEXT PRIMARY KEY,
                category TEXT,
                subcategory TEXT,
                mfr_part TEXT,
                package TEXT,
                solder_joints INTEGER,
                manufacturer TEXT,
                library_type TEXT,
                description TEXT,
                datasheet TEXT,
                stock INTEGER,
                price_json TEXT,
                last_updated INTEGER
            )
        """)

        # Create indexes for fast searching
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_category ON components(category, subcategory)"
        )
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_package ON components(package)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_manufacturer ON components(manufacturer)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_library_type ON components(library_type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_mfr_part ON components(mfr_part)")

        # Full-text search index for descriptions
        cursor.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS components_fts USING fts5(
                lcsc,
                description,
                mfr_part,
                manufacturer,
                content=components
            )
        """)

        self.conn.commit()
        logger.info(f"Initialized JLCPCB parts database at {self.db_path}")

    def import_parts(
        self, parts: List[Dict], progress_callback: Optional[Callable[..., Any]] = None
    ) -> None:
        """
        Import parts into database from JLCPCB API response

        Args:
            parts: List of part dicts from JLCPCB API
            progress_callback: Optional callback(current, total, message)
        """
        cursor = self.conn.cursor()
        imported = 0
        skipped = 0

        for i, part in enumerate(parts):
            try:
                # Extract price breaks
                price_json = json.dumps(part.get("prices", []))

                # Determine library type
                library_type = self._determine_library_type(part)

                cursor.execute(
                    """
                    INSERT OR REPLACE INTO components (
                        lcsc, category, subcategory, mfr_part, package,
                        solder_joints, manufacturer, library_type, description,
                        datasheet, stock, price_json, last_updated
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        part.get("componentCode"),  # lcsc
                        part.get("firstSortName"),  # category
                        part.get("secondSortName"),  # subcategory
                        part.get("componentModelEn"),  # mfr_part
                        part.get("componentSpecificationEn"),  # package
                        part.get("soldPoint"),  # solder_joints
                        part.get("componentBrandEn"),  # manufacturer
                        library_type,  # library_type
                        part.get("describe"),  # description
                        part.get("dataManualUrl"),  # datasheet
                        part.get("stockCount", 0),  # stock
                        price_json,  # price_json
                        int(datetime.now().timestamp()),  # last_updated
                    ),
                )

                imported += 1

                if progress_callback and (i + 1) % 1000 == 0:
                    progress_callback(i + 1, len(parts), f"Imported {imported} parts...")

            except Exception as e:
                logger.error(f"Error importing part {part.get('componentCode')}: {e}")
                skipped += 1

        # Update FTS index
        cursor.execute("""
            INSERT INTO components_fts(components_fts, rowid, lcsc, description, mfr_part, manufacturer)
            SELECT 'rebuild', rowid, lcsc, description, mfr_part, manufacturer FROM components
        """)

        self.conn.commit()
        logger.info(f"Import complete: {imported} parts imported, {skipped} skipped")

    def _determine_library_type(self, part: Dict) -> str:
        """Determine if part is Basic, Extended, or Preferred"""
        # JLCPCB API should provide this, but if not, we infer from assembly type
        assembly_type = part.get("assemblyType", "")

        if "Basic" in assembly_type or part.get("libraryType") == "base":
            return "Basic"
        elif "Extended" in assembly_type:
            return "Extended"
        elif "Prefer" in assembly_type:
            return "Preferred"
        else:
            return "Extended"  # Default to Extended

    # Order matters: more specific keywords first (so e.g. "Ferrite Bead"
    # outranks "Inductor" patterns we don't have, and "Schottky" doesn't
    # get gobbled up by a generic "Diode" sweep — which it would here too,
    # so we put the specific entry first).
    _CATEGORY_KEYWORDS: List[Tuple[str, str]] = [
        ("Ferrite Bead", "Inductors / Ferrite Beads"),
        ("Schottky", "Diodes / Schottky"),
        ("Zener", "Diodes / Zener"),
        ("TVS", "Diodes / TVS"),
        ("LED", "LEDs"),
        ("Diode", "Diodes"),
        ("MOSFET", "Transistors / MOSFET"),
        ("Transistor", "Transistors"),
        ("Resistor", "Resistors"),
        ("Capacitor", "Capacitors"),
        ("Inductor", "Inductors"),
        ("Crystal", "Crystals & Oscillators"),
        ("Oscillator", "Crystals & Oscillators"),
        ("Operational Amplifier", "ICs / Op-Amps"),
        ("Op-Amp", "ICs / Op-Amps"),
        ("Voltage Regulator", "ICs / Power Management"),
        ("LDO", "ICs / Power Management"),
        ("Buck Converter", "ICs / Power Management"),
        ("Boost Converter", "ICs / Power Management"),
        ("Microcontroller", "ICs / Microcontrollers"),
        ("MCU", "ICs / Microcontrollers"),
        ("EEPROM", "ICs / Memory"),
        ("Flash", "ICs / Memory"),
        ("Memory", "ICs / Memory"),
        ("Logic Gate", "ICs / Logic"),
        ("Connector", "Connectors"),
        ("Switch", "Switches"),
        ("Relay", "Relays"),
        ("Fuse", "Circuit Protection"),
        ("Sensor", "Sensors"),
    ]

    # ASCII → Unicode unit aliases for query rewriting.
    # FTS5's unicode61 tokenizer treats e.g. "150Ω" as a single token, so
    # users typing "150ohms" or "150 ohm" never match — the data has no
    # ASCII alias.  Rewrite the query so unit-bearing values land on the
    # tokens that actually exist in the index.
    _QUERY_UNIT_SUBS: List[Tuple[str, str]] = [
        # Resistance: handle prefix + unit, then bare unit, longer first so
        # "kohms" doesn't get partially substituted before "ohms".
        (r"(\d+(?:\.\d+)?)\s*[Mm]ohms?\b", r"\1MΩ"),
        (r"(\d+(?:\.\d+)?)\s*[Kk]ohms?\b", r"\1kΩ"),
        (r"(\d+(?:\.\d+)?)\s*ohms?\b", r"\1Ω"),
        # Capacitance / inductance prefixes.
        (r"(\d+(?:\.\d+)?)\s*[Uu][Ff]\b", r"\1μF"),
        (r"(\d+(?:\.\d+)?)\s*[Uu][Hh]\b", r"\1μH"),
    ]

    @classmethod
    def _normalize_query_units(cls, query: str) -> str:
        """Rewrite ASCII unit suffixes in a search query to the Unicode forms
        that appear in JLC descriptions, so e.g. "150ohms 0603" becomes
        "150Ω 0603" (which actually matches the indexed tokens)."""
        import re as _re
        out = query
        for pattern, repl in cls._QUERY_UNIT_SUBS:
            out = _re.sub(pattern, repl, out)
        return out

    @classmethod
    def _derive_category_from_description(cls, description: str) -> Tuple[str, str]:
        """Pattern-match a free-text description into (category, subcategory).

        jlcsearch's /components/list.json endpoint returns parts without
        category fields, so the `category` column was always blank.  Doing
        keyword sweeps over the description recovers ~87% on a random
        50K-row sample.  Returns ("", "") if nothing matches; callers can
        leave the column empty in that case.
        """
        if not description:
            return ("", "")
        desc_lower = description.lower()
        for keyword, category in cls._CATEGORY_KEYWORDS:
            if keyword.lower() in desc_lower:
                return (category, "")
        return ("", "")

    def import_jlcsearch_parts(
        self, parts: List[Dict], progress_callback: Optional[Callable[..., Any]] = None
    ) -> None:
        """
        Import parts into database from JLCSearch API response

        Args:
            parts: List of part dicts from JLCSearch API
            progress_callback: Optional callback(current, total, message)
        """
        cursor = self.conn.cursor()
        imported = 0
        skipped = 0

        for i, part in enumerate(parts):
            try:
                # JLCSearch format is different from official API
                # LCSC is an integer, we need to add 'C' prefix
                lcsc = part.get("lcsc")
                if isinstance(lcsc, int):
                    lcsc = f"C{lcsc}"

                # Build price JSON from jlcsearch single price
                price = part.get("price") or part.get("price1")
                price_json = json.dumps([{"qty": 1, "price": price}] if price else [])

                # Determine library type from is_basic flag
                library_type = "Basic" if part.get("is_basic") else "Extended"
                if part.get("is_preferred"):
                    library_type = "Preferred"

                # Extract description from various fields
                description_parts = []
                if "resistance" in part:
                    description_parts.append(f"{part['resistance']}Ω")
                if "capacitance" in part:
                    description_parts.append(f"{part['capacitance']}F")
                if "tolerance_fraction" in part:
                    tol = part["tolerance_fraction"] * 100
                    description_parts.append(f"±{tol}%")
                if "power_watts" in part:
                    description_parts.append(f"{part['power_watts']}mW")
                if "voltage" in part:
                    description_parts.append(f"{part['voltage']}V")

                description = part.get("description", " ".join(description_parts))

                # jlcsearch /components/list.json doesn't return per-row
                # category; derive from description so the `category` column
                # isn't uselessly empty.  Upstream value (if any) wins.
                upstream_cat = part.get("category", "")
                upstream_subcat = part.get("subcategory", "")
                if not upstream_cat:
                    upstream_cat, upstream_subcat = (
                        self._derive_category_from_description(description)
                        if not upstream_subcat
                        else (
                            self._derive_category_from_description(description)[0],
                            upstream_subcat,
                        )
                    )

                cursor.execute(
                    """
                    INSERT OR REPLACE INTO components (
                        lcsc, category, subcategory, mfr_part, package,
                        solder_joints, manufacturer, library_type, description,
                        datasheet, stock, price_json, last_updated
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        lcsc,  # lcsc with C prefix
                        upstream_cat,  # category (upstream or derived)
                        upstream_subcat,  # subcategory
                        part.get("mfr", ""),  # mfr_part
                        part.get("package", ""),  # package
                        0,  # solder_joints (not in jlcsearch)
                        part.get("manufacturer", ""),  # manufacturer
                        library_type,  # library_type
                        description,  # description
                        "",  # datasheet (not in jlcsearch)
                        part.get("stock", 0),  # stock
                        price_json,  # price_json
                        int(datetime.now().timestamp()),  # last_updated
                    ),
                )

                imported += 1

                if progress_callback and (i + 1) % 1000 == 0:
                    progress_callback(i + 1, len(parts), f"Imported {imported} parts...")

            except Exception as e:
                logger.error(f"Error importing part {part.get('lcsc')}: {e}")
                skipped += 1

        # Update FTS index
        cursor.execute("""
            INSERT INTO components_fts(components_fts)
            VALUES('rebuild')
        """)

        self.conn.commit()
        logger.info(f"Import complete: {imported} parts imported, {skipped} skipped")

    def backfill_categories(
        self, batch_size: int = 5000, progress_callback: Optional[Callable[..., Any]] = None
    ) -> Dict[str, int]:
        """Populate the `category` column for existing rows where it is empty,
        deriving values from the `description` column.

        Pre-existing databases imported before category derivation was wired
        in have category='' for every row, which makes
        `search_jlcpcb_parts(category='Resistors', …)` match nothing.  This
        method patches them in place.

        Returns {"updated": int, "scanned": int}.
        """
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM components WHERE (category IS NULL OR category = '') AND description IS NOT NULL"
        )
        total = cursor.fetchone()[0]
        updated = 0
        scanned = 0

        # Stream rows in batches to keep memory bounded on large DBs (~7M rows).
        last_lcsc = ""
        while True:
            cursor.execute(
                "SELECT lcsc, description FROM components "
                "WHERE (category IS NULL OR category = '') "
                "AND description IS NOT NULL AND description != '' "
                "AND lcsc > ? ORDER BY lcsc LIMIT ?",
                (last_lcsc, batch_size),
            )
            rows = cursor.fetchall()
            if not rows:
                break

            updates: List[Tuple[str, str]] = []
            for lcsc, desc in rows:
                cat, _ = self._derive_category_from_description(desc)
                if cat:
                    updates.append((cat, lcsc))
                last_lcsc = lcsc

            if updates:
                cursor.executemany(
                    "UPDATE components SET category = ? WHERE lcsc = ?", updates
                )
                self.conn.commit()
            updated += len(updates)
            scanned += len(rows)

            if progress_callback:
                progress_callback(scanned, total, f"Backfilled {updated} categories")

        return {"updated": updated, "scanned": scanned}

    # Whitelist of allowed ORDER BY clauses, indexed by user-facing key.
    # Never interpolate raw user input into SQL — only values from this map are used.
    _ORDER_BY_CLAUSES = {
        "stock_desc": "ORDER BY stock DESC",
        "stock_asc": "ORDER BY stock ASC",
        "none": "",
    }

    def search_parts(
        self,
        query: Optional[str] = None,
        category: Optional[str] = None,
        package: Optional[str] = None,
        library_type: Optional[str] = None,
        manufacturer: Optional[str] = None,
        in_stock: bool = True,
        limit: int = 20,
        order_by: str = "stock_desc",
    ) -> List[Dict]:
        """
        Search for parts with filters

        Args:
            query: Free-text search (searches description, mfr part, LCSC)
            category: Filter by category name
            package: Filter by package type
            library_type: Filter by "Basic", "Extended", or "Preferred"
            manufacturer: Filter by manufacturer name
            in_stock: Only return parts with stock > 0
            limit: Maximum number of results
            order_by: One of "stock_desc" (default), "stock_asc", "none".
                "stock_desc" surfaces high-volume parts first as a proxy for ongoing
                availability. Unknown values raise ValueError.

        Returns:
            List of matching parts
        """
        if order_by not in self._ORDER_BY_CLAUSES:
            raise ValueError(
                f"Invalid order_by={order_by!r}; expected one of "
                f"{sorted(self._ORDER_BY_CLAUSES)}"
            )

        cursor = self.conn.cursor()

        # Build query
        sql_parts = ["SELECT * FROM components WHERE 1=1"]
        params = []

        if query:
            # Use FTS for text search
            # First rewrite ASCII unit suffixes ("150ohms" → "150Ω") so the
            # query lands on tokens that actually exist in the index.
            normalized_query = self._normalize_query_units(query)
            # Add prefix wildcard to each term for partial matching
            # (e.g., "BQ25895" becomes "BQ25895*" so FTS matches "BQ25895RTWR")
            fts_query = " ".join(
                f"{term}*" if not term.endswith("*") else term
                for term in normalized_query.strip().split()
            )
            sql_parts.append("""
                AND lcsc IN (
                    SELECT lcsc FROM components_fts
                    WHERE components_fts MATCH ?
                )
            """)
            params.append(fts_query)

        if category:
            sql_parts.append("AND category LIKE ?")
            params.append(f"%{category}%")

        if package:
            sql_parts.append("AND package LIKE ?")
            params.append(f"%{package}%")

        if library_type:
            sql_parts.append("AND library_type = ?")
            params.append(library_type)

        if manufacturer:
            sql_parts.append("AND manufacturer LIKE ?")
            params.append(f"%{manufacturer}%")

        if in_stock:
            sql_parts.append("AND stock > 0")

        order_clause = self._ORDER_BY_CLAUSES[order_by]
        if order_clause:
            sql_parts.append(order_clause)

        sql_parts.append("LIMIT ?")
        params.append(limit)

        sql = " ".join(sql_parts)

        try:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Search error: {e}")
            return []

    def get_part_info(self, lcsc_number: str) -> Optional[Dict]:
        """
        Get detailed information for specific LCSC part

        Args:
            lcsc_number: LCSC part number (e.g., "C25804")

        Returns:
            Part info dict or None if not found
        """
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM components WHERE lcsc = ?", (lcsc_number,))
        row = cursor.fetchone()

        if row:
            part = dict(row)
            # Parse price JSON
            if part.get("price_json"):
                try:
                    part["price_breaks"] = json.loads(part["price_json"])
                except:
                    part["price_breaks"] = []
            return part
        return None

    def get_database_stats(self) -> Dict:
        """Get statistics about the database"""
        cursor = self.conn.cursor()

        cursor.execute("SELECT COUNT(*) as total FROM components")
        total = cursor.fetchone()["total"]

        cursor.execute("SELECT COUNT(*) as basic FROM components WHERE library_type = 'Basic'")
        basic = cursor.fetchone()["basic"]

        cursor.execute(
            "SELECT COUNT(*) as extended FROM components WHERE library_type = 'Extended'"
        )
        extended = cursor.fetchone()["extended"]

        cursor.execute("SELECT COUNT(*) as in_stock FROM components WHERE stock > 0")
        in_stock = cursor.fetchone()["in_stock"]

        return {
            "total_parts": total,
            "basic_parts": basic,
            "extended_parts": extended,
            "in_stock": in_stock,
            "db_path": self.db_path,
        }

    def map_package_to_footprint(self, package: str) -> List[str]:
        """
        Map JLCPCB package name to KiCAD footprint(s)

        Args:
            package: JLCPCB package name (e.g., "0603", "SOT-23")

        Returns:
            List of possible KiCAD footprint library refs
        """
        # Load mapping from JSON file or use defaults
        mappings = {
            "0402": [
                "Resistor_SMD:R_0402_1005Metric",
                "Capacitor_SMD:C_0402_1005Metric",
                "LED_SMD:LED_0402_1005Metric",
            ],
            "0603": [
                "Resistor_SMD:R_0603_1608Metric",
                "Capacitor_SMD:C_0603_1608Metric",
                "LED_SMD:LED_0603_1608Metric",
            ],
            "0805": ["Resistor_SMD:R_0805_2012Metric", "Capacitor_SMD:C_0805_2012Metric"],
            "1206": ["Resistor_SMD:R_1206_3216Metric", "Capacitor_SMD:C_1206_3216Metric"],
            "SOT-23": ["Package_TO_SOT_SMD:SOT-23", "Package_TO_SOT_SMD:SOT-23-3"],
            "SOT-23-5": ["Package_TO_SOT_SMD:SOT-23-5"],
            "SOT-23-6": ["Package_TO_SOT_SMD:SOT-23-6"],
            "SOIC-8": ["Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"],
            "SOIC-16": ["Package_SO:SOIC-16_3.9x9.9mm_P1.27mm"],
            "QFN-20": ["Package_DFN_QFN:QFN-20-1EP_4x4mm_P0.5mm_EP2.5x2.5mm"],
            "QFN-32": ["Package_DFN_QFN:QFN-32-1EP_5x5mm_P0.5mm_EP3.45x3.45mm"],
        }

        # Normalize package name
        package_normalized = package.strip().upper()

        for key, footprints in mappings.items():
            if key.upper() in package_normalized:
                return footprints

        return []

    def suggest_alternatives(self, lcsc_number: str, limit: int = 5) -> List[Dict]:
        """
        Find alternative parts similar to the given LCSC number

        Prioritizes: cheaper price, higher stock, Basic library type

        Args:
            lcsc_number: Reference LCSC part number
            limit: Maximum alternatives to return

        Returns:
            List of alternative parts
        """
        part = self.get_part_info(lcsc_number)
        if not part:
            return []

        # Search for parts in same category with same package
        alternatives = self.search_parts(
            category=part["subcategory"], package=part["package"], in_stock=True, limit=limit * 3
        )

        # Filter out the original part
        alternatives = [p for p in alternatives if p["lcsc"] != lcsc_number]

        # Sort by: Basic first, then by price, then by stock
        def sort_key(p: Dict[str, Any]) -> Tuple[int, float, int]:
            is_basic = 1 if p.get("library_type") == "Basic" else 0
            try:
                prices = json.loads(p.get("price_json", "[]"))
                price = float(prices[0].get("price", 999)) if prices else 999
            except:
                price = 999
            stock = p.get("stock", 0)

            return (-is_basic, price, -stock)

        alternatives.sort(key=sort_key)

        return alternatives[:limit]

    def close(self) -> None:
        """Close database connection"""
        if self.conn:
            self.conn.close()


if __name__ == "__main__":
    # Test the parts manager
    logging.basicConfig(level=logging.INFO)

    manager = JLCPCBPartsManager()

    # Get stats
    stats = manager.get_database_stats()
    print(f"\nDatabase Statistics:")
    print(f"  Total parts: {stats['total_parts']}")
    print(f"  Basic parts: {stats['basic_parts']}")
    print(f"  Extended parts: {stats['extended_parts']}")
    print(f"  In stock: {stats['in_stock']}")
    print(f"  Database: {stats['db_path']}")

    if stats["total_parts"] > 0:
        print("\nSearching for '10k resistor'...")
        results = manager.search_parts(query="10k resistor", limit=5)
        for part in results:
            print(
                f"  {part['lcsc']}: {part['mfr_part']} - {part['description']} ({part['library_type']})"
            )
