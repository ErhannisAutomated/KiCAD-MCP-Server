"""
One-shot backfill script: populate the `category` column for existing
JLCPCB parts rows that were imported before category derivation was wired
into import_jlcsearch_parts.

Usage:
    python -m scripts.backfill_jlcpcb_categories [db_path]

If db_path is omitted, uses the default project data/jlcpcb_parts.db.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from commands.jlcpcb_parts import JLCPCBPartsManager  # noqa: E402


def main(argv: list[str]) -> int:
    db_path = argv[0] if argv else None
    mgr = JLCPCBPartsManager(db_path=db_path)
    print(f"Backfilling categories in: {mgr.db_path}")
    last_print = time.time()

    def cb(scanned: int, total: int, msg: str) -> None:
        nonlocal last_print
        if time.time() - last_print > 2.0:
            print(f"  {scanned:>10,d} / {total:>10,d}  {msg}")
            last_print = time.time()

    result = mgr.backfill_categories(progress_callback=cb)
    print(f"Done: scanned={result['scanned']:,}, updated={result['updated']:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
