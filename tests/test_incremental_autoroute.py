"""Tests for the incremental-autoroute copy helpers (#242).

`_clone_net_routing` lifts only the target nets' tracks/vias off a routed
scratch board and reconstructs them on the live board, re-binding each to
the live board's net by name; `_remove_net_routing` clears the named nets
first so they get a clean replacement. These run against lightweight fakes
mimicking the pcbnew surface the helpers touch — no pcbnew required.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))


# --- pcbnew fakes -----------------------------------------------------------

F_CU = 0  # stand-in for pcbnew.F_Cu


class FakeTrack:
    def __init__(self, net, cls="PCB_TRACK", start=(0, 0), end=(1, 1),
                 width=250000, layer=0):
        self._net = net
        self._cls = cls
        self._start = start
        self._end = end
        self._width = width
        self._layer = layer
        # via-only attributes
        self._pos = start
        self._drill = 300000
        self._viatype = "through"
        self._top = 0
        self._bot = 31

    def GetNetname(self):
        return self._net

    def GetClass(self):
        return self._cls

    def GetStart(self):
        return self._start

    def GetEnd(self):
        return self._end

    def GetMid(self):
        return ((self._start[0] + self._end[0]) // 2,
                (self._start[1] + self._end[1]) // 2)

    def GetLayer(self):
        return self._layer

    def GetWidth(self, layer=None):
        # pcbnew vias take a layer arg; tracks take none. We accept both.
        return self._width

    def GetPosition(self):
        return self._pos

    def GetDrillValue(self):
        return self._drill

    def GetViaType(self):
        return self._viatype

    def TopLayer(self):
        return self._top

    def BottomLayer(self):
        return self._bot


class FakeNewItem:
    """Records the setters the helper calls so tests can assert on them."""

    def __init__(self, board):
        self.board = board
        self.net = None
        self.calls = {}

    def SetStart(self, v):
        self.calls["start"] = v

    def SetEnd(self, v):
        self.calls["end"] = v

    def SetMid(self, v):
        self.calls["mid"] = v

    def SetLayer(self, v):
        self.calls["layer"] = v

    def SetWidth(self, v):
        self.calls["width"] = v

    def SetPosition(self, v):
        self.calls["pos"] = v

    def SetDrill(self, v):
        self.calls["drill"] = v

    def SetViaType(self, v):
        self.calls["viatype"] = v

    def SetLayerPair(self, top, bot):
        self.calls["layerpair"] = (top, bot)

    def SetNet(self, net):
        self.net = net


class FakePcbnew:
    F_Cu = F_CU

    def __init__(self):
        self.created = []

    def PCB_TRACK(self, board):
        item = FakeNewItem(board)
        item.kind = "track"
        self.created.append(item)
        return item

    def PCB_VIA(self, board):
        item = FakeNewItem(board)
        item.kind = "via"
        self.created.append(item)
        return item

    def PCB_ARC(self, board):
        item = FakeNewItem(board)
        item.kind = "arc"
        self.created.append(item)
        return item


class FakeNetsMap:
    def __init__(self, names):
        self._d = {n: f"NETOBJ:{n}" for n in names}

    def has_key(self, n):
        return n in self._d

    def __getitem__(self, n):
        return self._d[n]


class FakeNetInfo:
    def __init__(self, names):
        self._map = FakeNetsMap(names)

    def NetsByName(self):
        return self._map


class FakeBoard:
    def __init__(self, tracks=None, net_names=None):
        self._tracks = list(tracks or [])
        self._netinfo = FakeNetInfo(net_names or [])
        self.added = []

    def GetTracks(self):
        return list(self._tracks)

    def GetNetInfo(self):
        return self._netinfo

    def Add(self, item):
        self.added.append(item)
        self._tracks.append(item)

    def RemoveNative(self, item):
        self._tracks.remove(item)


# --- tests ------------------------------------------------------------------

def test_remove_net_routing_only_targets():
    from commands.freerouting import _remove_net_routing

    board = FakeBoard(tracks=[
        FakeTrack("GND"),
        FakeTrack("BAT+"),
        FakeTrack("GND", cls="PCB_VIA"),
        FakeTrack("V12_OUT"),
    ])
    removed = _remove_net_routing(board, {"GND"})
    assert removed == 2
    remaining = {t.GetNetname() for t in board.GetTracks()}
    assert remaining == {"BAT+", "V12_OUT"}


def test_clone_net_routing_filters_and_counts():
    from commands.freerouting import _clone_net_routing

    src = FakeBoard(tracks=[
        FakeTrack("CHG", cls="PCB_TRACK"),
        FakeTrack("CHG", cls="PCB_TRACK"),
        FakeTrack("CHG", cls="PCB_VIA"),
        FakeTrack("OTHER", cls="PCB_TRACK"),  # must be ignored
    ])
    dest = FakeBoard(net_names=["CHG", "OTHER"])
    pcbnew = FakePcbnew()

    per_net, ntracks, nvias = _clone_net_routing(
        src, dest, {"CHG"}, pcbnew
    )

    assert ntracks == 2
    assert nvias == 1
    assert per_net == {"CHG": {"tracks": 2, "vias": 1}}
    # Only CHG items were added to dest (OTHER ignored).
    assert len(dest.added) == 3


def test_clone_binds_net_by_name_on_dest():
    """Copied items must be re-bound to the destination board's net."""
    from commands.freerouting import _clone_net_routing

    src = FakeBoard(tracks=[FakeTrack("CHG")])
    dest = FakeBoard(net_names=["CHG"])
    pcbnew = FakePcbnew()

    _clone_net_routing(src, dest, {"CHG"}, pcbnew)
    assert dest.added[0].net == "NETOBJ:CHG"


def test_clone_via_uses_layer_aware_width_and_layerpair():
    from commands.freerouting import _clone_net_routing

    via = FakeTrack("CHG", cls="PCB_VIA")
    via._top, via._bot = 0, 31
    src = FakeBoard(tracks=[via])
    dest = FakeBoard(net_names=["CHG"])
    pcbnew = FakePcbnew()

    _clone_net_routing(src, dest, {"CHG"}, pcbnew)
    new_via = dest.added[0]
    assert new_via.kind == "via"
    assert new_via.calls["layerpair"] == (0, 31)
    assert "drill" in new_via.calls and "viatype" in new_via.calls
    assert "width" in new_via.calls  # GetWidth(F_Cu) didn't raise


def test_clone_arc_preserves_mid():
    from commands.freerouting import _clone_net_routing

    src = FakeBoard(tracks=[FakeTrack("CHG", cls="PCB_ARC",
                                      start=(0, 0), end=(10, 0))])
    dest = FakeBoard(net_names=["CHG"])
    pcbnew = FakePcbnew()

    per_net, ntracks, nvias = _clone_net_routing(src, dest, {"CHG"}, pcbnew)
    assert ntracks == 1 and nvias == 0
    assert dest.added[0].kind == "arc"
    assert dest.added[0].calls.get("mid") == (5, 0)


def test_clone_unmapped_net_does_not_crash():
    """If dest lacks the net (shouldn't happen for a scratch copy), the
    item is still created but left unbound rather than raising."""
    from commands.freerouting import _clone_net_routing

    src = FakeBoard(tracks=[FakeTrack("CHG")])
    dest = FakeBoard(net_names=[])  # no CHG
    pcbnew = FakePcbnew()

    per_net, ntracks, nvias = _clone_net_routing(src, dest, {"CHG"}, pcbnew)
    assert ntracks == 1
    assert dest.added[0].net is None
