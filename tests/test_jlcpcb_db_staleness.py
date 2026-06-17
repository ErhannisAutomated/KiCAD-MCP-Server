"""
Tests for JLCPCBPartsManager.get_db_age_days + the STALE_AGE_DAYS threshold.

The local parts DB has no auto-refresh, so search/get_part responses surface a
staleness warning when the snapshot is older than STALE_AGE_DAYS. These tests
cover the age computation that drives that warning.
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.jlcpcb_parts import JLCPCBPartsManager


def _make_manager(tmp_path):
    return JLCPCBPartsManager(db_path=str(tmp_path / "jlcpcb_test.db"))


def test_fresh_db_is_not_stale(tmp_path):
    mgr = _make_manager(tmp_path)
    age = mgr.get_db_age_days()
    assert age is not None
    assert age < 1.0  # just created
    assert age <= mgr.STALE_AGE_DAYS


def test_backdated_db_is_stale(tmp_path):
    mgr = _make_manager(tmp_path)
    old = time.time() - (mgr.STALE_AGE_DAYS + 5) * 86400
    os.utime(mgr.db_path, (old, old))
    age = mgr.get_db_age_days()
    assert age is not None
    assert age > mgr.STALE_AGE_DAYS


def test_age_none_when_file_missing(tmp_path):
    mgr = _make_manager(tmp_path)
    mgr.conn.close()
    os.remove(mgr.db_path)
    assert mgr.get_db_age_days() is None
