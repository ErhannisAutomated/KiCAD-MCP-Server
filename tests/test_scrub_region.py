"""Tests for scrub_region (#251).

Two layers of coverage:
  * pure geometry / classification / prune helpers (no pcbnew), and
  * a fake-board integration test of the per-element decision rule
    (target-only vs shared vs no-target, per-side scoping, reasons,
    dry-run-does-not-mutate).

See docs/SCRUB_REGION_PLAN.md.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

MM = 1_000_000  # nm per mm


# ---------------------------------------------------------------------------
# Pure geometry helpers
# ---------------------------------------------------------------------------
def test_convex_hull_of_square_drops_interior_point():
    from commands.scrub_region import _convex_hull

    hull = _convex_hull([(0, 0), (10, 0), (10, 10), (0, 10), (5, 5)])
    assert len(hull) == 4
    assert (5, 5) not in hull


def test_within_hull_margin_is_distance_slack():
    from commands.scrub_region import _convex_hull, _within_hull

    hull = _convex_hull([(0, 0), (10, 0), (10, 10), (0, 10)])
    # inside -> always within
    assert _within_hull((5, 5), hull, 0)
    # 2 units outside the right edge
    assert not _within_hull((12, 5), hull, 1)
    assert _within_hull((12, 5), hull, 3)


def test_within_degenerate_hull_uses_point_and_segment_distance():
    from commands.scrub_region import _within_hull

    # single point hull
    assert _within_hull((1, 0), [(0, 0)], 2)
    assert not _within_hull((5, 0), [(0, 0)], 2)
    # segment hull
    assert _within_hull((5, 1), [(0, 0), (10, 0)], 2)
    assert not _within_hull((5, 5), [(0, 0), (10, 0)], 2)


def test_segment_intersects_hull_crossing_and_nearmiss():
    from commands.scrub_region import _convex_hull, _segment_intersects_hull

    hull = _convex_hull([(0, 0), (10, 0), (10, 10), (0, 10)])
    # chord crossing the box with both endpoints outside
    assert _segment_intersects_hull((-5, 5), (15, 5), hull, 0)
    # fully outside, beyond margin
    assert not _segment_intersects_hull((20, 20), (30, 30), hull, 1)
    # near-miss within margin
    assert _segment_intersects_hull((12, 0), (12, 10), hull, 3)


# ---------------------------------------------------------------------------
# Net classification
# ---------------------------------------------------------------------------
def test_classify_nets_target_only_vs_shared_vs_none():
    from commands.scrub_region import _classify_nets

    n2r = {
        "BQ_SW": {"U3", "L1"},        # all targets -> target-only
        "USB_VBUS": {"U3", "J1"},     # mixed -> shared
        "GND": {"U3", "J1", "U4"},    # mixed -> shared
        "OTHER": {"J1", "U4"},        # no target -> neither
    }
    target_only, shared = _classify_nets(n2r, {"U3", "L1", "C16"})
    assert target_only == {"BQ_SW"}
    assert shared == {"USB_VBUS", "GND"}


# ---------------------------------------------------------------------------
# Recursive dead-end prune
# ---------------------------------------------------------------------------
def test_prune_keeps_pad_to_pad_track():
    from commands.scrub_region import _prune_dead_ends

    segs = [{"id": "A", "net": "N", "a": (0, 0), "b": (10 * MM, 0)}]
    pads = {("N", 0, 0), ("N", 10 * MM, 0)}
    pruned_s, pruned_v = _prune_dead_ends(segs, [], pads, [])
    assert pruned_s == set()
    assert pruned_v == set()


def test_prune_recursively_removes_dangling_chain():
    from commands.scrub_region import _prune_dead_ends

    # pad -> A -> B(free). Removing B leaves A dangling off the pad -> both go.
    segs = [
        {"id": "A", "net": "N", "a": (0, 0), "b": (10 * MM, 0)},
        {"id": "B", "net": "N", "a": (10 * MM, 0), "b": (20 * MM, 0)},
    ]
    pads = {("N", 0, 0)}
    pruned_s, _ = _prune_dead_ends(segs, [], pads, [])
    assert pruned_s == {"A", "B"}


def test_prune_keeps_stub_into_same_net_pour():
    from commands.scrub_region import _prune_dead_ends

    # A connects a pad to a point sitting inside a same-net zone (pour) —
    # the pour-end is "connected", so the stub is kept.
    segs = [{"id": "A", "net": "GND", "a": (0, 0), "b": (10 * MM, 0)}]
    pads = {("GND", 0, 0)}
    zones = [("GND", 5 * MM, -1 * MM, 15 * MM, 1 * MM)]
    pruned_s, _ = _prune_dead_ends(segs, [], pads, zones)
    assert pruned_s == set()


def test_prune_removes_single_connection_via_keeps_stitching():
    from commands.scrub_region import _prune_dead_ends

    # via with only one track on it -> pruned
    segs = [{"id": "A", "net": "N", "a": (0, 0), "b": (5 * MM, 0)}]
    vias = [{"id": "V", "net": "N", "pos": (5 * MM, 0)}]
    pruned_s, pruned_v = _prune_dead_ends(segs, vias, {("N", 0, 0)}, [])
    assert "V" in pruned_v

    # via sitting inside a same-net pour -> kept (stitching)
    vias2 = [{"id": "V", "net": "GND", "pos": (5 * MM, 0)}]
    zones = [("GND", 0, -MM, 10 * MM, MM)]
    _, pruned_v2 = _prune_dead_ends([], vias2, set(), zones)
    assert pruned_v2 == set()


# ---------------------------------------------------------------------------
# Fake-board integration
# ---------------------------------------------------------------------------
class _Uuid:
    def __init__(self, s):
        self.s = s

    def AsString(self):
        return self.s


class _Box:
    def __init__(self, l, t, r, b):
        self.l, self.t, self.r, self.b = l, t, r, b

    def GetLeft(self):
        return self.l

    def GetTop(self):
        return self.t

    def GetRight(self):
        return self.r

    def GetBottom(self):
        return self.b


class _Vec:
    def __init__(self, x, y):
        self.x, self.y = x, y


class _Pad:
    def __init__(self, net, x=0, y=0):
        self._net = net
        self._p = _Vec(x, y)

    def GetNetname(self):
        return self._net

    def GetPosition(self):
        return self._p


class _Fp:
    def __init__(self, ref, layer, box, pads):
        self._ref, self._layer, self._box = ref, layer, box
        self._pads = pads
        self._pos = _Vec((box.l + box.r) // 2, (box.t + box.b) // 2)

    def GetReference(self):
        return self._ref

    def GetLayer(self):
        return self._layer

    def GetBoundingBox(self, _inc):
        return self._box

    def Pads(self):
        return self._pads

    def GetPosition(self):
        return self._pos


_VIA_T = 2


class _Track:
    def __init__(self, uuid, net, layer, start, end, typ=1, top=0, bottom=2):
        self.m_Uuid = _Uuid(uuid)
        self._net, self._layer = net, layer
        self._s, self._e = _Vec(*start), _Vec(*end)
        self._typ, self._top, self._bottom = typ, top, bottom
        self.removed = False

    def Type(self):
        return self._typ

    def GetNetname(self):
        return self._net

    def GetLayer(self):
        return self._layer

    def GetStart(self):
        return self._s

    def GetEnd(self):
        return self._e

    def GetPosition(self):
        return self._s

    def IsOnLayer(self, lid):
        return lid in (self._top, self._bottom)

    def TopLayer(self):
        return self._top

    def BottomLayer(self):
        return self._bottom


class _Board:
    def __init__(self, fps, tracks):
        self._fps, self._tracks = fps, tracks
        self._layers = {"F.Cu": 0, "B.Cu": 2, "In2.Cu": 6}
        self._names = {v: k for k, v in self._layers.items()}
        self.removed = []

    def GetFootprints(self):
        return self._fps

    def Tracks(self):
        return [t for t in self._tracks if not t.removed]

    def Zones(self):
        return []

    def GetLayerID(self, name):
        return self._layers.get(name, -1)

    def GetLayerName(self, lid):
        return self._names.get(lid, str(lid))

    def RemoveNative(self, item):
        item.removed = True
        self.removed.append(item)

    def SetModified(self):
        pass

    def GetFileName(self):
        return ""


def _make_board():
    u3 = _Fp("U3", 0, _Box(0, 0, 10 * MM, 10 * MM),
             [_Pad("BQ_SW", MM, MM), _Pad("USB_VBUS", 2 * MM, MM),
              _Pad("GND", 3 * MM, MM)])
    l1 = _Fp("L1", 0, _Box(12 * MM, 0, 14 * MM, 2 * MM), [_Pad("BQ_SW", 13 * MM, MM)])
    j1 = _Fp("J1", 0, _Box(40 * MM, 40 * MM, 42 * MM, 42 * MM),
             [_Pad("USB_VBUS", 41 * MM, 41 * MM), _Pad("GND", 41 * MM, 42 * MM),
              _Pad("OTHER", 40 * MM, 41 * MM)])
    u4 = _Fp("U4", 0, _Box(45 * MM, 45 * MM, 47 * MM, 47 * MM),
             [_Pad("GND", 46 * MM, 46 * MM), _Pad("OTHER", 45 * MM, 46 * MM)])

    tracks = [
        _Track("t1", "BQ_SW", 0, (50 * MM, 50 * MM), (51 * MM, 50 * MM)),
        _Track("t2", "USB_VBUS", 0, (5 * MM, 5 * MM), (6 * MM, 5 * MM)),
        _Track("t3", "USB_VBUS", 0, (40 * MM, 40 * MM), (41 * MM, 40 * MM)),
        _Track("t4", "OTHER", 0, (5 * MM, 6 * MM), (6 * MM, 6 * MM)),
        _Track("t5", "USB_VBUS", 2, (5 * MM, 5 * MM), (6 * MM, 5 * MM)),
        _Track("v1", "BQ_SW", 0, (50 * MM, 50 * MM), (50 * MM, 50 * MM), typ=_VIA_T),
        _Track("v2", "USB_VBUS", 0, (5 * MM, 5 * MM), (5 * MM, 5 * MM), typ=_VIA_T),
        _Track("v3", "USB_VBUS", 0, (40 * MM, 40 * MM), (40 * MM, 40 * MM), typ=_VIA_T),
    ]
    return _Board([u3, l1, j1, u4], tracks)


def _patch_pcbnew(monkeypatch):
    import commands.scrub_region as sr

    fake = types.SimpleNamespace(PCB_VIA_T=_VIA_T, SaveBoard=lambda *a, **k: None)
    monkeypatch.setattr(sr, "pcbnew", fake)
    return sr


def test_scrub_region_dry_run_matches_expected_kill_list(monkeypatch):
    sr = _patch_pcbnew(monkeypatch)
    board = _make_board()
    cmd = sr.ScrubRegionCommands(board)

    res = cmd.scrub_region({
        "targets": ["U3", "L1"],
        "layers": ["F.Cu", "B.Cu"],
        "pruneDeadEnds": False,
        "viz": False,
        "dryRun": True,
    })

    assert res["success"] is True
    assert res["dryRun"] is True
    # nothing actually removed on a dry run
    assert board.removed == []

    deleted_tracks = {d["uuid"]: d["reason"] for d in res["wouldDelete"]["tracks"]}
    deleted_vias = {d["uuid"]: d["reason"] for d in res["wouldDelete"]["vias"]}

    # t1 target-only (deleted despite being far outside the hull)
    assert deleted_tracks["t1"] == "target-only-net"
    # t2 shared, inside hull
    assert deleted_tracks["t2"] == "endpoint-in-hull"
    # t3 shared, outside hull -> kept; t4 no-target -> kept;
    # t5 shared but on B.Cu with no back targets (empty back hull) -> kept
    assert "t3" not in deleted_tracks
    assert "t4" not in deleted_tracks
    assert "t5" not in deleted_tracks

    assert deleted_vias["v1"] == "target-only-net"
    assert deleted_vias["v2"] == "in-hull"
    assert "v3" not in deleted_vias

    assert set(res["nowOpenNets"]) == {"BQ_SW", "USB_VBUS"}


def test_scrub_region_commit_removes_items(monkeypatch):
    sr = _patch_pcbnew(monkeypatch)
    board = _make_board()
    cmd = sr.ScrubRegionCommands(board)

    res = cmd.scrub_region({
        "targets": ["U3", "L1"],
        "pruneDeadEnds": False,
        "viz": False,
        "dryRun": False,
    })

    assert res["success"] is True
    assert res["dryRun"] is False
    removed = {t.m_Uuid.AsString() for t in board.removed}
    assert removed == {"t1", "t2", "v1", "v2"}


def test_scrub_region_rejects_unknown_target(monkeypatch):
    sr = _patch_pcbnew(monkeypatch)
    board = _make_board()
    cmd = sr.ScrubRegionCommands(board)
    res = cmd.scrub_region({"targets": ["NOPE"], "viz": False})
    assert res["success"] is False
    assert "NOPE" in res["errorDetails"]
