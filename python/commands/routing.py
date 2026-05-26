"""
Routing-related command implementations for KiCAD interface
"""

import logging
import math
import os
from typing import Any, Dict, List, Optional, Tuple

import pcbnew

logger = logging.getLogger("kicad_interface")


# pcbnew's SWIG `BOARD::Remove(item)` corrupts process-global SWIG type
# state after a few hundred mass-removals — methods on the BOARD then
# return bare SwigPyObject instead of their real types, and even a fresh
# pcbnew.LoadBoard returns SwigPyObject. `BOARD::RemoveNative(item)` is
# the in-process-safe alternative (skips the listener/undo path that
# triggers the corruption) and is what every Remove() call site below
# uses. See tests/test_remove_native_does_not_corrupt.py.


class RoutingCommands:
    """Handles routing-related KiCAD operations"""

    def __init__(self, board: Optional[pcbnew.BOARD] = None):
        """Initialize with optional board instance"""
        self.board = board

    def add_net(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Add a new net to the PCB"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            name = params.get("name")
            net_class = params.get("class")

            if not name:
                return {
                    "success": False,
                    "message": "Missing net name",
                    "errorDetails": "name parameter is required",
                }

            # Create new net
            netinfo = self.board.GetNetInfo()
            nets_map = netinfo.NetsByName()
            if nets_map.has_key(name):
                net = nets_map[name]
            else:
                net = pcbnew.NETINFO_ITEM(self.board, name)
                self.board.Add(net)

            # Set net class if provided
            if net_class:
                net_classes = self.board.GetNetClasses()
                if net_classes.Find(net_class):
                    net.SetClass(net_classes.Find(net_class))

            return {
                "success": True,
                "message": f"Added net: {name}",
                "net": {
                    "name": name,
                    "class": net_class if net_class else "Default",
                    "netcode": net.GetNetCode(),
                },
            }

        except Exception as e:
            logger.error(f"Error adding net: {str(e)}")
            return {
                "success": False,
                "message": "Failed to add net",
                "errorDetails": str(e),
            }

    def route_pad_to_pad(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Route a trace directly from one component pad to another.

        Looks up pad positions automatically, then creates a trace.
        Convenience wrapper around route_trace that eliminates the need
        for separate get_pad_position calls.

        Optional pin-escape support (#178): pass
        `escapeFromWidth`/`escapeFromLength` (and/or the symmetric
        `escapeToWidth`/`escapeToLength`) to break the route into a
        narrow stub exiting the pad followed by a wider trunk segment.
        Use this when a fat trunk trace can't physically fit out of a
        tight IC pin pitch. The stub direction is perpendicular to the
        pad's pin row (computed from footprint center → pad center).
        Currently same-layer only — cross-layer routes ignore escape
        params.
        """
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            from_ref = params.get("fromRef")
            from_pad = str(params.get("fromPad", ""))
            to_ref = params.get("toRef")
            to_pad = str(params.get("toPad", ""))
            layer = params.get("layer", "F.Cu")
            width = params.get("width")
            net = params.get("net")  # optional override
            check_obstacles = params.get("checkObstacles", True)
            clearance = params.get("clearance")  # mm; default = netclass
            escape_from_w = params.get("escapeFromWidth")
            escape_from_l = params.get("escapeFromLength")
            escape_to_w = params.get("escapeToWidth")
            escape_to_l = params.get("escapeToLength")

            if not from_ref or not from_pad or not to_ref or not to_pad:
                return {
                    "success": False,
                    "message": "Missing parameters",
                    "errorDetails": "fromRef, fromPad, toRef, toPad are all required",
                }

            # Pin-escape params must come as width+length pairs — silently
            # ignoring one would mask typos.
            if bool(escape_from_w) != bool(escape_from_l):
                return {
                    "success": False,
                    "message": "Incomplete escape params",
                    "errorDetails": (
                        "escapeFromWidth and escapeFromLength must both "
                        "be set (or both omitted)"
                    ),
                }
            if bool(escape_to_w) != bool(escape_to_l):
                return {
                    "success": False,
                    "message": "Incomplete escape params",
                    "errorDetails": (
                        "escapeToWidth and escapeToLength must both be "
                        "set (or both omitted)"
                    ),
                }

            scale = 1000000  # nm to mm

            # Find pads
            footprints = {fp.GetReference(): fp for fp in self.board.GetFootprints()}

            for ref in [from_ref, to_ref]:
                if ref not in footprints:
                    return {
                        "success": False,
                        "message": f"Component not found: {ref}",
                        "errorDetails": f"'{ref}' does not exist on the board",
                    }

            def find_pad(ref: str, pad_num: str) -> Any:
                fp = footprints[ref]
                for pad in fp.Pads():
                    if pad.GetNumber() == pad_num:
                        return pad
                return None

            start_pad = find_pad(from_ref, from_pad)
            end_pad = find_pad(to_ref, to_pad)

            if not start_pad:
                return {
                    "success": False,
                    "message": f"Pad not found: {from_ref} pad {from_pad}",
                    "errorDetails": f"Check pad number for {from_ref}",
                }
            if not end_pad:
                return {
                    "success": False,
                    "message": f"Pad not found: {to_ref} pad {to_pad}",
                    "errorDetails": f"Check pad number for {to_ref}",
                }

            start_pos = start_pad.GetPosition()
            end_pos = end_pad.GetPosition()

            # Use net from start pad if not overridden
            if not net:
                net = start_pad.GetNetname() or end_pad.GetNetname() or ""

            # Detect if pads are on different copper layers → need via.
            # SMD pad.GetLayer() reports F.Cu even on flipped B.Cu footprints in
            # KiCAD 9 SWIG. Use footprint.GetLayer() instead — it always reflects
            # the actual placed layer after Flip().
            fp_start = footprints[from_ref]
            fp_end = footprints[to_ref]
            start_layer = self.board.GetLayerName(fp_start.GetLayer())
            end_layer = self.board.GetLayerName(fp_end.GetLayer())
            copper_layers = {"F.Cu", "B.Cu"}
            needs_via = (
                start_layer in copper_layers
                and end_layer in copper_layers
                and start_layer != end_layer
            )

            def _obstacle_error(obs: list) -> Dict[str, Any]:
                shown = "; ".join(obs[:8])
                if len(obs) > 8:
                    shown += f" (+{len(obs) - 8} more)"
                return {
                    "success": False,
                    "message": f"Route blocked by {len(obs)} obstacle(s)",
                    "errorDetails": (
                        "The path would cross foreign-net copper: "
                        + shown
                        + ". route_pad_to_pad only draws straight segments "
                        "(+ optional pin-escape stubs) — try escapeFromWidth/"
                        "escapeFromLength to narrow the trace at an IC pad, "
                        "or use route_trace with intermediate waypoints to "
                        "route around the obstacle, or pass "
                        "checkObstacles=false to override."
                    ),
                    "obstacles": obs,
                }

            if needs_via:
                if escape_from_w or escape_to_w:
                    return {
                        "success": False,
                        "message": "Pin escape not supported on cross-layer routes",
                        "errorDetails": (
                            "v1 pin-escape only handles same-layer "
                            "pad-to-pad routes. For a cross-layer "
                            "fan-out, route the narrow stub to a via "
                            "point with route_trace, then continue with "
                            "find_via_lane or route_pad_to_pad on the "
                            "destination layer."
                        ),
                    }
                # Place via directly below the start pad (same X).
                # Using the geometric midpoint X causes all vias to stack at
                # the same X when pads are back-to-back mirrored (e.g. J1/J2
                # on F.Cu/B.Cu): midpoint is always the board center.
                via_x = start_pos.x / scale
                via_y = (start_pos.y + end_pos.y) / 2 / scale

                if check_obstacles:
                    via_pt = pcbnew.VECTOR2I(int(via_x * scale), int(via_y * scale))
                    trace_width_iu, min_clearance_iu = self._resolve_route_clearance(
                        width, clearance, net
                    )
                    obs = self._find_route_obstacles(
                        start_pos, via_pt, self.board.GetLayerID(start_layer), net,
                        trace_width_iu, min_clearance_iu,
                    ) + self._find_route_obstacles(
                        via_pt, end_pos, self.board.GetLayerID(end_layer), net,
                        trace_width_iu, min_clearance_iu,
                    )
                    if obs:
                        return _obstacle_error(obs)

                # Trace on start layer: start_pad → via
                # checkObstacles=False — we already verified the full path above
                r1 = self.route_trace(
                    {
                        "start": {"x": start_pos.x / scale, "y": start_pos.y / scale, "unit": "mm"},
                        "end": {"x": via_x, "y": via_y, "unit": "mm"},
                        "layer": start_layer,
                        "width": width,
                        "net": net,
                        "checkObstacles": False,
                    }
                )
                # Via connecting both layers
                self.add_via(
                    {
                        "position": {"x": via_x, "y": via_y, "unit": "mm"},
                        "net": net,
                        "from_layer": start_layer,
                        "to_layer": end_layer,
                    }
                )
                # Trace on end layer: via → end_pad
                r2 = self.route_trace(
                    {
                        "start": {"x": via_x, "y": via_y, "unit": "mm"},
                        "end": {"x": end_pos.x / scale, "y": end_pos.y / scale, "unit": "mm"},
                        "layer": end_layer,
                        "width": width,
                        "net": net,
                        "checkObstacles": False,
                    }
                )
                success = r1.get("success") and r2.get("success")
                result = {
                    "success": success,
                    "message": f"Routed {from_ref}.{from_pad} → via → {to_ref}.{to_pad} (net: {net}, via at {via_x:.2f},{via_y:.2f})",
                    "via_added": True,
                    "via_position": {"x": via_x, "y": via_y},
                }
            else:
                # Same layer — direct trace (with optional pin-escape stubs)
                seg_layer = layer if layer else start_layer
                layer_id = self.board.GetLayerID(seg_layer)

                from_has_escape = bool(escape_from_w and escape_from_l)
                to_has_escape = bool(escape_to_w and escape_to_l)

                # Build a list of (start, end, width_mm) segments. Default
                # is a single direct trace; pin-escape inserts a narrow
                # stub at each escaping end and a wider trunk in between.
                segments = []  # list of (pcbnew.VECTOR2I, pcbnew.VECTOR2I, float)
                trunk_start = start_pos
                trunk_end = end_pos

                if from_has_escape:
                    dx, dy = self._pad_outward_unit_vec(start_pad, fp_start)
                    escape_iu = int(float(escape_from_l) * scale)
                    wp_from = pcbnew.VECTOR2I(
                        int(start_pos.x + dx * escape_iu),
                        int(start_pos.y + dy * escape_iu),
                    )
                    segments.append((start_pos, wp_from, float(escape_from_w)))
                    trunk_start = wp_from

                if to_has_escape:
                    dx, dy = self._pad_outward_unit_vec(end_pad, fp_end)
                    escape_iu = int(float(escape_to_l) * scale)
                    wp_to = pcbnew.VECTOR2I(
                        int(end_pos.x + dx * escape_iu),
                        int(end_pos.y + dy * escape_iu),
                    )
                    trunk_end = wp_to

                # Trunk segment between (possibly) escape endpoints.
                segments.append((trunk_start, trunk_end, float(width) if width else None))

                if to_has_escape:
                    segments.append((trunk_end, end_pos, float(escape_to_w)))

                # Obstacle check each segment individually with its own
                # per-segment width — so the narrow stub doesn't get
                # rejected against pin pitch that a fat trace couldn't
                # clear.
                if check_obstacles:
                    for seg_start, seg_end, seg_w in segments:
                        tw_iu, mc_iu = self._resolve_route_clearance(
                            seg_w, clearance, net
                        )
                        obs = self._find_route_obstacles(
                            seg_start, seg_end, layer_id, net,
                            tw_iu, mc_iu,
                        )
                        if obs:
                            return _obstacle_error(obs)

                # Commit each segment.  route_trace is called with
                # checkObstacles=False since the full path was verified
                # above.
                sub_results = []
                for seg_start, seg_end, seg_w in segments:
                    sub = self.route_trace(
                        {
                            "start": {
                                "x": seg_start.x / scale,
                                "y": seg_start.y / scale,
                                "unit": "mm",
                            },
                            "end": {
                                "x": seg_end.x / scale,
                                "y": seg_end.y / scale,
                                "unit": "mm",
                            },
                            "layer": seg_layer,
                            "width": seg_w,
                            "net": net,
                            "checkObstacles": False,
                        }
                    )
                    sub_results.append(sub)

                # Surface the last segment as `result` (its keys mirror
                # the existing single-segment shape used downstream).
                result = sub_results[-1]
                if not all(r.get("success") for r in sub_results):
                    result = {
                        "success": False,
                        "message": (
                            "Failed to commit one or more pin-escape "
                            "segments"
                        ),
                        "subResults": sub_results,
                    }
                elif from_has_escape or to_has_escape:
                    result = {
                        "success": True,
                        "message": (
                            f"Routed {from_ref}.{from_pad} → "
                            f"{to_ref}.{to_pad} via "
                            f"{len(segments)} segment(s) "
                            f"(pin-escape applied)"
                        ),
                        "segmentCount": len(segments),
                    }

            if result.get("success"):
                result["fromPad"] = {
                    "ref": from_ref,
                    "pad": from_pad,
                    "x": start_pos.x / scale,
                    "y": start_pos.y / scale,
                }
                result["toPad"] = {
                    "ref": to_ref,
                    "pad": to_pad,
                    "x": end_pos.x / scale,
                    "y": end_pos.y / scale,
                }

            return result

        except Exception as e:
            logger.error(f"Error in route_pad_to_pad: {str(e)}")
            return {
                "success": False,
                "message": "Failed to route pad to pad",
                "errorDetails": str(e),
            }

    def route_trace(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Route a trace between two points or pads"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            start = params.get("start")
            end = params.get("end")
            layer = params.get("layer", "F.Cu")
            width = params.get("width")
            net = params.get("net")
            via = params.get("via", False)
            check_obstacles = params.get("checkObstacles", True)
            clearance = params.get("clearance")  # mm; default = netclass

            if not start or not end:
                return {
                    "success": False,
                    "message": "Missing parameters",
                    "errorDetails": "start and end points are required",
                }

            # Get layer ID
            layer_id = self.board.GetLayerID(layer)
            if layer_id < 0:
                return {
                    "success": False,
                    "message": "Invalid layer",
                    "errorDetails": f"Layer '{layer}' does not exist",
                }

            # Get start point
            start_point = self._get_point(start)
            end_point = self._get_point(end)

            # Obstacle check before committing the segment. Same helper that
            # route_pad_to_pad uses — refuses when the proposed segment would
            # cross foreign-net copper. Default on; pass checkObstacles=false
            # to override (e.g. when intentionally routing through a region
            # that DRC will tolerate, or when restoring a known-good trace).
            if check_obstacles and net:
                trace_width_iu, min_clearance_iu = self._resolve_route_clearance(
                    width, clearance, net
                )
                obs = self._find_route_obstacles(
                    start_point, end_point, layer_id, net,
                    trace_width_iu, min_clearance_iu,
                )
                if obs:
                    shown = "; ".join(obs[:8])
                    if len(obs) > 8:
                        shown += f" (+{len(obs) - 8} more)"
                    return {
                        "success": False,
                        "message": f"Route blocked by {len(obs)} obstacle(s)",
                        "errorDetails": (
                            "The straight path would cross foreign-net copper: "
                            + shown
                            + ". Route around the obstacles with intermediate "
                            "waypoints, or pass checkObstacles=false to override."
                        ),
                        "obstacles": obs,
                    }

            # Create track segment
            track = pcbnew.PCB_TRACK(self.board)
            track.SetStart(start_point)
            track.SetEnd(end_point)
            track.SetLayer(layer_id)

            # Set width (default to board's current track width)
            if width:
                track.SetWidth(int(width * 1000000))  # Convert mm to nm
            else:
                track.SetWidth(self.board.GetDesignSettings().GetCurrentTrackWidth())

            # Set net if provided
            if net:
                netinfo = self.board.GetNetInfo()
                nets_map = netinfo.NetsByName()
                if nets_map.has_key(net):
                    net_obj = nets_map[net]
                    track.SetNet(net_obj)

            # Add track to board
            self.board.Add(track)

            # Add via if requested and net is specified
            if via and net:
                via_point = end_point
                self.add_via(
                    {
                        "position": {
                            "x": via_point.x / 1000000,
                            "y": via_point.y / 1000000,
                            "unit": "mm",
                        },
                        "net": net,
                    }
                )

            return {
                "success": True,
                "message": "Added trace",
                "trace": {
                    "start": {
                        "x": start_point.x / 1000000,
                        "y": start_point.y / 1000000,
                        "unit": "mm",
                    },
                    "end": {
                        "x": end_point.x / 1000000,
                        "y": end_point.y / 1000000,
                        "unit": "mm",
                    },
                    "layer": layer,
                    "width": track.GetWidth() / 1000000,
                    "net": net,
                },
            }

        except Exception as e:
            logger.error(f"Error routing trace: {str(e)}")
            return {
                "success": False,
                "message": "Failed to route trace",
                "errorDetails": str(e),
            }

    def add_via(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Add a via at the specified location"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            position = params.get("position")
            size = params.get("size")
            drill = params.get("drill")
            net = params.get("net")
            from_layer = params.get("from_layer", "F.Cu")
            to_layer = params.get("to_layer", "B.Cu")

            if not position:
                return {
                    "success": False,
                    "message": "Missing position",
                    "errorDetails": "position parameter is required",
                }

            # Create via
            via = pcbnew.PCB_VIA(self.board)

            # Set position
            scale = 1000000 if position["unit"] == "mm" else 25400000  # mm or inch to nm
            x_nm = int(position["x"] * scale)
            y_nm = int(position["y"] * scale)
            via.SetPosition(pcbnew.VECTOR2I(x_nm, y_nm))

            # Set size and drill (default to board's current via settings)
            design_settings = self.board.GetDesignSettings()
            via.SetWidth(int(size * 1000000) if size else design_settings.GetCurrentViaSize())
            via.SetDrill(int(drill * 1000000) if drill else design_settings.GetCurrentViaDrill())

            # Set layers
            from_id = self.board.GetLayerID(from_layer)
            to_id = self.board.GetLayerID(to_layer)
            if from_id < 0 or to_id < 0:
                return {
                    "success": False,
                    "message": "Invalid layer",
                    "errorDetails": "Specified layers do not exist",
                }
            via.SetLayerPair(from_id, to_id)

            # Set net if provided
            if net:
                netinfo = self.board.GetNetInfo()
                nets_map = netinfo.NetsByName()
                if nets_map.has_key(net):
                    net_obj = nets_map[net]
                    via.SetNet(net_obj)

            # Add via to board
            self.board.Add(via)

            return {
                "success": True,
                "message": "Added via",
                "via": {
                    "position": {
                        "x": position["x"],
                        "y": position["y"],
                        "unit": position["unit"],
                    },
                    "size": via.GetWidth(pcbnew.F_Cu) / 1000000,
                    "drill": via.GetDrill() / 1000000,
                    "from_layer": from_layer,
                    "to_layer": to_layer,
                    "net": net,
                },
            }

        except Exception as e:
            logger.error(f"Error adding via: {str(e)}")
            return {
                "success": False,
                "message": "Failed to add via",
                "errorDetails": str(e),
            }

    def dedupe_traces(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Remove exact-duplicate tracks (and optionally vias) left over
        from autoroute SES re-imports and similar.

        Two tracks are duplicates iff they share (layer, width, net) and
        their endpoints match (in either order — direction-insensitive).
        Two vias are duplicates iff they share (position, drill, width,
        net). Endpoint comparison uses 1 IU tolerance to absorb float
        round-trip artifacts.

        Default mode is dry-run (preview-only); pass apply=true to
        actually remove. Optional net filter to limit scope. Returns the
        list of removable UUIDs and per-group counts.
        """
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            apply = bool(params.get("apply", False))
            net_filter = params.get("net")
            include_vias = bool(params.get("includeVias", True))

            # Bucket tracks by their canonical key. Endpoints canonicalised
            # by sorting so (A→B) and (B→A) hash equal.
            groups: Dict[tuple, List[Any]] = {}
            for item in list(self.board.Tracks()):
                if net_filter and item.GetNetname() != net_filter:
                    continue
                is_via = item.Type() == pcbnew.PCB_VIA_T
                if is_via and not include_vias:
                    continue
                if is_via:
                    pos = item.GetPosition()
                    try:
                        width = item.GetWidth(pcbnew.F_Cu)
                    except TypeError:
                        width = item.GetWidth()
                    key = (
                        "via",
                        item.GetNetname(),
                        int(pos.x),
                        int(pos.y),
                        int(item.GetDrill()),
                        int(width),
                    )
                else:
                    s, e = item.GetStart(), item.GetEnd()
                    a = (int(s.x), int(s.y))
                    b = (int(e.x), int(e.y))
                    if a > b:
                        a, b = b, a
                    key = (
                        "track",
                        item.GetLayer(),
                        item.GetNetname(),
                        int(item.GetWidth()),
                        a,
                        b,
                    )
                groups.setdefault(key, []).append(item)

            # First in each group survives; the rest are duplicates.
            duplicates: List[Any] = []
            duplicate_groups: List[Dict[str, Any]] = []
            for key, members in groups.items():
                if len(members) <= 1:
                    continue
                extras = members[1:]
                duplicates.extend(extras)
                duplicate_groups.append(
                    {
                        "kind": key[0],
                        "net": key[2] if key[0] == "via" else key[2],
                        "duplicate_count": len(extras),
                        "kept_uuid": str(members[0].m_Uuid.AsString()),
                        "removed_uuids": [
                            str(x.m_Uuid.AsString()) for x in extras
                        ],
                    }
                )

            removed_count = 0
            if apply and duplicates:
                for item in duplicates:
                    # RemoveNative — same SWIG note as delete_trace.
                    self.board.RemoveNative(item)
                    removed_count += 1
                self.board.SetModified()

            return {
                "success": True,
                "applied": apply,
                "duplicateGroupCount": len(duplicate_groups),
                "duplicateItemCount": len(duplicates),
                "removedCount": removed_count if apply else 0,
                "groups": duplicate_groups,
                "message": (
                    f"{'Removed' if apply else 'Found'} "
                    f"{len(duplicates)} duplicate item(s) across "
                    f"{len(duplicate_groups)} group(s)"
                    + ("" if apply else " (dry-run; pass apply=true to delete)")
                ),
            }

        except Exception as e:
            logger.error(f"Error in dedupe_traces: {str(e)}")
            return {
                "success": False,
                "message": "Failed to dedupe traces",
                "errorDetails": str(e),
            }

    def delete_trace(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Delete a trace from the PCB"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            trace_uuid = params.get("traceUuid")
            position = params.get("position")
            net_name = params.get("net")
            layer = params.get("layer")
            include_vias = params.get("includeVias", False)

            if not trace_uuid and not position and not net_name:
                return {
                    "success": False,
                    "message": "Missing parameters",
                    "errorDetails": "One of traceUuid, position, or net must be provided",
                }

            # Delete by net name (bulk delete), use "*" to delete all tracks
            if net_name:
                tracks_to_remove = []
                for track in list(self.board.Tracks()):
                    if net_name != "*" and track.GetNetname() != net_name:
                        continue

                    # Skip vias if not requested
                    is_via = track.Type() == pcbnew.PCB_VIA_T
                    if is_via and not include_vias:
                        continue

                    # Filter by layer if specified (only for non-vias)
                    if layer and not is_via:
                        layer_id = self.board.GetLayerID(layer)
                        if track.GetLayer() != layer_id:
                            continue

                    tracks_to_remove.append(track)

                deleted_count = len(tracks_to_remove)
                for track in tracks_to_remove:
                    # RemoveNative — see module-level note: BOARD.Remove()
                    # corrupts SWIG state after a few hundred mass-removals.
                    self.board.RemoveNative(track)
                tracks_to_remove.clear()
                self.board.SetModified()

                return {
                    "success": True,
                    "message": f"Deleted {deleted_count} traces on net '{net_name}'",
                    "deletedCount": deleted_count,
                }

            # Find track by UUID
            if trace_uuid:
                track = None
                for item in list(self.board.Tracks()):
                    if item.m_Uuid.AsString() == trace_uuid:
                        track = item
                        break

                if not track:
                    return {
                        "success": False,
                        "message": "Track not found",
                        "errorDetails": f"Could not find track with UUID: {trace_uuid}",
                    }

                self.board.RemoveNative(track)
                track = None
                self.board.SetModified()
                return {"success": True, "message": f"Deleted track: {trace_uuid}"}

            # No valid parameters provided
            if not position:
                return {
                    "success": False,
                    "message": "No valid search parameter provided",
                    "errorDetails": "Provide traceUuid, position, or net parameter",
                }

            # Find track by position
            if position:
                scale = 1000000 if position["unit"] == "mm" else 25400000  # mm or inch to nm
                x_nm = int(position["x"] * scale)
                y_nm = int(position["y"] * scale)
                point = pcbnew.VECTOR2I(x_nm, y_nm)

                # Find closest track
                closest_track = None
                min_distance = float("inf")
                for track in list(self.board.Tracks()):
                    dist = self._point_to_track_distance(point, track)
                    if dist < min_distance:
                        min_distance = dist
                        closest_track = track

                if closest_track and min_distance < 1000000:  # Within 1mm
                    self.board.RemoveNative(closest_track)
                    closest_track = None
                    self.board.SetModified()
                    return {
                        "success": True,
                        "message": "Deleted track at specified position",
                    }
                else:
                    return {
                        "success": False,
                        "message": "No track found",
                        "errorDetails": "No track found near specified position",
                    }

        except Exception as e:
            logger.error(f"Error deleting trace: {str(e)}")
            return {
                "success": False,
                "message": "Failed to delete trace",
                "errorDetails": str(e),
            }
        return {
            "success": False,
            "message": "No action taken",
            "errorDetails": "No matching trace found for given parameters",
        }

    def get_nets_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Get a list of all nets in the PCB, optionally with routing stats."""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            include_stats = params.get("includeStats", False)
            unit = params.get("unit", "mm")
            scale = 1000000.0 if unit == "mm" else 25400000.0

            # Tally per-net track count, via count and routed length in one pass.
            stats: Dict[int, Dict[str, Any]] = {}
            if include_stats:
                for track in self.board.Tracks():
                    code = track.GetNetCode()
                    s = stats.setdefault(
                        code, {"trackCount": 0, "viaCount": 0, "totalLength": 0.0}
                    )
                    if track.Type() == pcbnew.PCB_VIA_T:
                        s["viaCount"] += 1
                    else:
                        s["trackCount"] += 1
                        s["totalLength"] += track.GetLength() / scale

            nets = []
            netinfo = self.board.GetNetInfo()
            for net_code in range(netinfo.GetNetCount()):
                net = netinfo.GetNetItem(net_code)
                if net:
                    entry = {
                        "name": net.GetNetname(),
                        "code": net.GetNetCode(),
                        "class": net.GetNetClassName(),
                    }
                    if include_stats:
                        s = stats.get(
                            net.GetNetCode(),
                            {"trackCount": 0, "viaCount": 0, "totalLength": 0.0},
                        )
                        entry["trackCount"] = s["trackCount"]
                        entry["viaCount"] = s["viaCount"]
                        entry["totalLength"] = round(s["totalLength"], 4)
                    nets.append(entry)

            result = {"success": True, "nets": nets}
            if include_stats:
                result["unit"] = unit
            return result

        except Exception as e:
            logger.error(f"Error getting nets list: {str(e)}")
            return {
                "success": False,
                "message": "Failed to get nets list",
                "errorDetails": str(e),
            }

    def query_traces(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Query traces by net, layer, or bounding box"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            # Get filter parameters
            net_name = params.get("net")
            layer = params.get("layer")
            bbox = params.get("boundingBox")  # {x1, y1, x2, y2, unit}
            include_vias = params.get("includeVias", False)

            scale = 1000000  # nm to mm conversion factor
            traces = []
            vias = []

            # Process tracks
            for track in list(self.board.Tracks()):
                try:
                    # Check if it's a via
                    is_via = track.Type() == pcbnew.PCB_VIA_T

                    if is_via and not include_vias:
                        continue

                    # Filter by net
                    if net_name and track.GetNetname() != net_name:
                        continue

                    # Filter by layer (only for tracks, not vias)
                    if layer and not is_via:
                        layer_id = self.board.GetLayerID(layer)
                        if track.GetLayer() != layer_id:
                            continue

                    # Filter by bounding box
                    if bbox:
                        bbox_unit = bbox.get("unit", "mm")
                        bbox_scale = scale if bbox_unit == "mm" else 25400000
                        x1 = int(bbox.get("x1", 0) * bbox_scale)
                        y1 = int(bbox.get("y1", 0) * bbox_scale)
                        x2 = int(bbox.get("x2", 0) * bbox_scale)
                        y2 = int(bbox.get("y2", 0) * bbox_scale)

                        if is_via:
                            pos = track.GetPosition()
                            if not (x1 <= pos.x <= x2 and y1 <= pos.y <= y2):
                                continue
                        else:
                            start = track.GetStart()
                            end = track.GetEnd()
                            # Check if either endpoint is within bbox
                            start_in = x1 <= start.x <= x2 and y1 <= start.y <= y2
                            end_in = x1 <= end.x <= x2 and y1 <= end.y <= y2
                            if not (start_in or end_in):
                                continue

                    if is_via:
                        pos = track.GetPosition()
                        vias.append(
                            {
                                "uuid": track.m_Uuid.AsString(),
                                "position": {
                                    "x": pos.x / scale,
                                    "y": pos.y / scale,
                                    "unit": "mm",
                                },
                                "net": track.GetNetname(),
                                "netCode": track.GetNetCode(),
                                "diameter": track.GetWidth() / scale,
                                "drill": track.GetDrillValue() / scale,
                            }
                        )
                    else:
                        start = track.GetStart()
                        end = track.GetEnd()
                        traces.append(
                            {
                                "uuid": track.m_Uuid.AsString(),
                                "net": track.GetNetname(),
                                "netCode": track.GetNetCode(),
                                "layer": self.board.GetLayerName(track.GetLayer()),
                                "width": track.GetWidth() / scale,
                                "start": {
                                    "x": start.x / scale,
                                    "y": start.y / scale,
                                    "unit": "mm",
                                },
                                "end": {
                                    "x": end.x / scale,
                                    "y": end.y / scale,
                                    "unit": "mm",
                                },
                                "length": track.GetLength() / scale,
                            }
                        )
                except Exception as track_err:
                    logger.warning(f"Skipping invalid track object: {track_err}")
                    continue

            result = {"success": True, "traceCount": len(traces), "traces": traces}

            if include_vias:
                result["viaCount"] = len(vias)
                result["vias"] = vias

            return result

        except Exception as e:
            logger.error(f"Error querying traces: {str(e)}")
            return {
                "success": False,
                "message": "Failed to query traces",
                "errorDetails": str(e),
            }

    def audit_plane_cuts(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Report signal traces routed on inner copper layers that double as
        power/GND planes — long traces there "cut" the plane and break
        image-current return paths above F.Cu signals.

        Returns per-net cut length, per-layer breakdown, and the longest
        single-trace offenders ranked for ripup + retry on an outer layer.
        """
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            layers = params.get("layers", ["In1.Cu", "In2.Cu"])
            min_length = float(params.get("minLength", 1.0))
            unit = params.get("unit", "mm")
            scale = 1000000.0 if unit == "mm" else 25400000.0

            layer_ids = {}
            for ln in layers:
                lid = self.board.GetLayerID(ln)
                if lid < 0:
                    return {
                        "success": False,
                        "message": f"Unknown layer: {ln}",
                        "errorDetails": f"GetLayerID('{ln}') returned {lid}",
                    }
                layer_ids[lid] = ln

            offenders = []
            per_net: Dict[str, Dict[str, Any]] = {}

            for t in self.board.Tracks():
                if t.Type() == pcbnew.PCB_VIA_T:
                    continue
                if t.GetLayer() not in layer_ids:
                    continue
                length = t.GetLength() / scale
                if length < min_length:
                    continue
                net = t.GetNetname() or "<no net>"
                layer = layer_ids[t.GetLayer()]
                start, end = t.GetStart(), t.GetEnd()
                offenders.append(
                    {
                        "uuid": t.m_Uuid.AsString(),
                        "net": net,
                        "layer": layer,
                        "length": round(length, 4),
                        "width": round(t.GetWidth() / scale, 4),
                        "start": {
                            "x": round(start.x / scale, 4),
                            "y": round(start.y / scale, 4),
                            "unit": unit,
                        },
                        "end": {
                            "x": round(end.x / scale, 4),
                            "y": round(end.y / scale, 4),
                            "unit": unit,
                        },
                    }
                )
                ns = per_net.setdefault(
                    net, {"total": 0.0, "byLayer": {}, "segments": 0}
                )
                ns["total"] += length
                ns["segments"] += 1
                ns["byLayer"][layer] = round(ns["byLayer"].get(layer, 0.0) + length, 4)

            offenders.sort(key=lambda r: r["length"], reverse=True)
            for ns in per_net.values():
                ns["total"] = round(ns["total"], 4)

            net_ranking = sorted(
                (
                    {
                        "net": n,
                        "total": v["total"],
                        "segments": v["segments"],
                        "byLayer": v["byLayer"],
                    }
                    for n, v in per_net.items()
                ),
                key=lambda r: r["total"],
                reverse=True,
            )

            return {
                "success": True,
                "unit": unit,
                "layersAudited": layers,
                "minLength": min_length,
                "totalCutSegments": len(offenders),
                "totalCutLength": round(sum(o["length"] for o in offenders), 4),
                "netRanking": net_ranking,
                "longestTraces": offenders[:25],
            }

        except Exception as e:
            logger.error(f"Error in audit_plane_cuts: {str(e)}")
            return {
                "success": False,
                "message": "Failed to audit plane cuts",
                "errorDetails": str(e),
            }

    def modify_trace(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Modify properties of an existing trace

        Allows changing trace width, layer, and net assignment.
        Find trace by UUID or position.
        """
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            # Identification parameters
            # Accept both 'uuid' (legacy) and 'traceUuid' (TS schema name,
            # matching delete_trace) so callers don't have to guess.
            trace_uuid = params.get("traceUuid") or params.get("uuid")
            position = params.get("position")  # {x, y, unit}

            # Modification parameters
            new_width = params.get("width")  # in mm
            new_layer = params.get("layer")
            new_net = params.get("net")

            if not trace_uuid and not position:
                return {
                    "success": False,
                    "message": "Missing trace identifier",
                    "errorDetails": "Provide either 'uuid' or 'position' to identify the trace",
                }

            scale = 1000000  # nm to mm conversion

            # Find the track
            track = None

            if trace_uuid:
                for item in list(self.board.Tracks()):
                    if item.m_Uuid.AsString() == trace_uuid:
                        track = item
                        break
            elif position:
                pos_unit = position.get("unit", "mm")
                pos_scale = scale if pos_unit == "mm" else 25400000
                x_nm = int(position["x"] * pos_scale)
                y_nm = int(position["y"] * pos_scale)
                point = pcbnew.VECTOR2I(x_nm, y_nm)

                # Find closest track
                min_distance = float("inf")
                for item in list(self.board.Tracks()):
                    dist = self._point_to_track_distance(point, item)
                    if dist < min_distance:
                        min_distance = dist
                        track = item

                # Only accept if within 1mm
                if min_distance >= 1000000:
                    track = None

            if not track:
                return {
                    "success": False,
                    "message": "Track not found",
                    "errorDetails": "Could not find track with specified identifier",
                }

            # Check if it's a via (some modifications don't apply)
            is_via = track.Type() == pcbnew.PCB_VIA_T
            modifications = []

            # Apply modifications
            if new_width is not None:
                width_nm = int(new_width * scale)
                track.SetWidth(width_nm)
                modifications.append(f"width={new_width}mm")

            if new_layer and not is_via:
                layer_id = self.board.GetLayerID(new_layer)
                if layer_id < 0:
                    return {
                        "success": False,
                        "message": "Invalid layer",
                        "errorDetails": f"Layer '{new_layer}' not found",
                    }
                track.SetLayer(layer_id)
                modifications.append(f"layer={new_layer}")

            if new_net:
                netinfo = self.board.GetNetInfo()
                net = netinfo.GetNetItem(new_net)
                if not net:
                    return {
                        "success": False,
                        "message": "Invalid net",
                        "errorDetails": f"Net '{new_net}' not found",
                    }
                track.SetNet(net)
                modifications.append(f"net={new_net}")

            if not modifications:
                return {
                    "success": False,
                    "message": "No modifications specified",
                    "errorDetails": "Provide at least one of: width, layer, net",
                }

            return {
                "success": True,
                "message": f"Modified trace: {', '.join(modifications)}",
                "uuid": track.m_Uuid.AsString(),
                "modifications": modifications,
            }

        except Exception as e:
            logger.error(f"Error modifying trace: {str(e)}")
            return {
                "success": False,
                "message": "Failed to modify trace",
                "errorDetails": str(e),
            }

    def copy_routing_pattern(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Copy routing pattern from source components to target components

        This enables routing replication between identical component groups.
        The pattern is copied with a translation offset calculated from
        the position difference between source and target components.
        """
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            source_refs = params.get("sourceRefs", [])  # e.g., ["U1", "U2", "U3"]
            target_refs = params.get("targetRefs", [])  # e.g., ["U4", "U5", "U6"]
            include_vias = params.get("includeVias", True)
            trace_width = params.get("traceWidth")  # Optional override

            if not source_refs or not target_refs:
                return {
                    "success": False,
                    "message": "Missing component references",
                    "errorDetails": "Provide both 'sourceRefs' and 'targetRefs' arrays",
                }

            if len(source_refs) != len(target_refs):
                return {
                    "success": False,
                    "message": "Mismatched component counts",
                    "errorDetails": f"sourceRefs has {len(source_refs)} items, targetRefs has {len(target_refs)}",
                }

            scale = 1000000  # nm to mm conversion

            # Get footprints
            footprints = {fp.GetReference(): fp for fp in self.board.GetFootprints()}

            # Validate all references exist
            for ref in source_refs + target_refs:
                if ref not in footprints:
                    return {
                        "success": False,
                        "message": "Component not found",
                        "errorDetails": f"Component '{ref}' not found on board",
                    }

            # Calculate offset from first source to first target component
            source_fp = footprints[source_refs[0]]
            target_fp = footprints[target_refs[0]]
            source_pos = source_fp.GetPosition()
            target_pos = target_fp.GetPosition()

            offset_x = target_pos.x - source_pos.x
            offset_y = target_pos.y - source_pos.y

            # Build mapping from source refs to target refs
            ref_mapping = dict(zip(source_refs, target_refs))

            # Collect all nets connected to source components
            source_nets = set()
            source_pad_positions = []  # (x, y) in nm for geometric fallback
            for ref in source_refs:
                fp = footprints[ref]
                for pad in fp.Pads():
                    net_name = pad.GetNetname()
                    if net_name and net_name != "":
                        source_nets.add(net_name)
                    pos = pad.GetPosition()
                    source_pad_positions.append((pos.x, pos.y))

            # Build bounding box around source pads (with 5mm tolerance in nm)
            TOLERANCE_NM = int(5 * scale)
            if source_pad_positions:
                xs = [p[0] for p in source_pad_positions]
                ys = [p[1] for p in source_pad_positions]
                bbox_x1 = min(xs) - TOLERANCE_NM
                bbox_x2 = max(xs) + TOLERANCE_NM
                bbox_y1 = min(ys) - TOLERANCE_NM
                bbox_y2 = max(ys) + TOLERANCE_NM
            else:
                # Fall back to component position ± 25mm
                sp = source_fp.GetPosition()
                bbox_x1 = sp.x - int(25 * scale)
                bbox_x2 = sp.x + int(25 * scale)
                bbox_y1 = sp.y - int(25 * scale)
                bbox_y2 = sp.y + int(25 * scale)

            def point_in_bbox(px: int, py: int) -> bool:
                return bbox_x1 <= px <= bbox_x2 and bbox_y1 <= py <= bbox_y2

            # Collect traces: by net name (if available) OR by geometric proximity
            use_net_filter = len(source_nets) > 0
            traces_to_copy = []
            vias_to_copy = []

            for track in list(self.board.Tracks()):
                is_via = track.Type() == pcbnew.PCB_VIA_T

                if use_net_filter:
                    # Primary: net-based filter
                    if track.GetNetname() not in source_nets:
                        continue
                else:
                    # Fallback: geometric filter – trace start OR end inside source bbox
                    if is_via:
                        pos = track.GetPosition()
                        if not point_in_bbox(pos.x, pos.y):
                            continue
                    else:
                        s = track.GetStart()
                        e = track.GetEnd()
                        if not (point_in_bbox(s.x, s.y) or point_in_bbox(e.x, e.y)):
                            continue

                if is_via:
                    if include_vias:
                        vias_to_copy.append(track)
                else:
                    traces_to_copy.append(track)

            filter_method = "net-based" if use_net_filter else "geometric (pads have no nets)"
            logger.info(
                f"copy_routing_pattern: {len(traces_to_copy)} traces, "
                f"{len(vias_to_copy)} vias selected via {filter_method}"
            )

            # Create new traces with offset
            created_traces = 0
            created_vias = 0

            for track in traces_to_copy:
                start = track.GetStart()
                end = track.GetEnd()

                # Create new track
                new_track = pcbnew.PCB_TRACK(self.board)
                new_track.SetStart(pcbnew.VECTOR2I(start.x + offset_x, start.y + offset_y))
                new_track.SetEnd(pcbnew.VECTOR2I(end.x + offset_x, end.y + offset_y))
                new_track.SetLayer(track.GetLayer())

                # Set width (use override or original)
                if trace_width:
                    new_track.SetWidth(int(trace_width * scale))
                else:
                    new_track.SetWidth(track.GetWidth())

                # Try to find corresponding target net
                # This is a simplification - more sophisticated mapping would be needed
                # for complex designs
                self.board.Add(new_track)
                created_traces += 1

            for via in vias_to_copy:
                pos = via.GetPosition()

                # Create new via
                new_via = pcbnew.PCB_VIA(self.board)
                new_via.SetPosition(pcbnew.VECTOR2I(pos.x + offset_x, pos.y + offset_y))
                new_via.SetWidth(via.GetWidth(pcbnew.F_Cu))
                new_via.SetDrill(via.GetDrillValue())
                new_via.SetViaType(via.GetViaType())

                self.board.Add(new_via)
                created_vias += 1

            result = {
                "success": True,
                "message": f"Copied routing pattern: {created_traces} traces, {created_vias} vias",
                "filterMethod": filter_method,
                "offset": {"x": offset_x / scale, "y": offset_y / scale, "unit": "mm"},
                "createdTraces": created_traces,
                "createdVias": created_vias,
                "sourceComponents": source_refs,
                "targetComponents": target_refs,
            }

            return result

        except Exception as e:
            logger.error(f"Error copying routing pattern: {str(e)}")
            return {
                "success": False,
                "message": "Failed to copy routing pattern",
                "errorDetails": str(e),
            }

    def create_netclass(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new net class with specified properties"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            name = params.get("name")
            clearance = params.get("clearance")
            track_width = params.get("trackWidth")
            via_diameter = params.get("viaDiameter")
            via_drill = params.get("viaDrill")
            uvia_diameter = params.get("uviaDiameter")
            uvia_drill = params.get("uviaDrill")
            diff_pair_width = params.get("diffPairWidth")
            diff_pair_gap = params.get("diffPairGap")
            nets = params.get("nets", [])

            if not name:
                return {
                    "success": False,
                    "message": "Missing netclass name",
                    "errorDetails": "name parameter is required",
                }

            # Get net classes
            net_classes = self.board.GetNetClasses()

            # Create new net class if it doesn't exist
            if not net_classes.Find(name):
                netclass = pcbnew.NETCLASS(name)
                net_classes.Add(netclass)
            else:
                netclass = net_classes.Find(name)

            # Set properties
            scale = 1000000  # mm to nm
            if clearance is not None:
                netclass.SetClearance(int(clearance * scale))
            if track_width is not None:
                netclass.SetTrackWidth(int(track_width * scale))
            if via_diameter is not None:
                netclass.SetViaDiameter(int(via_diameter * scale))
            if via_drill is not None:
                netclass.SetViaDrill(int(via_drill * scale))
            if uvia_diameter is not None:
                netclass.SetMicroViaDiameter(int(uvia_diameter * scale))
            if uvia_drill is not None:
                netclass.SetMicroViaDrill(int(uvia_drill * scale))
            if diff_pair_width is not None:
                netclass.SetDiffPairWidth(int(diff_pair_width * scale))
            if diff_pair_gap is not None:
                netclass.SetDiffPairGap(int(diff_pair_gap * scale))

            # Add nets to net class
            netinfo = self.board.GetNetInfo()
            nets_map = netinfo.NetsByName()
            for net_name in nets:
                if nets_map.has_key(net_name):
                    net = nets_map[net_name]
                    net.SetClass(netclass)

            return {
                "success": True,
                "message": f"Created net class: {name}",
                "netClass": {
                    "name": name,
                    "clearance": netclass.GetClearance() / scale,
                    "trackWidth": netclass.GetTrackWidth() / scale,
                    "viaDiameter": netclass.GetViaDiameter() / scale,
                    "viaDrill": netclass.GetViaDrill() / scale,
                    "uviaDiameter": netclass.GetMicroViaDiameter() / scale,
                    "uviaDrill": netclass.GetMicroViaDrill() / scale,
                    "diffPairWidth": netclass.GetDiffPairWidth() / scale,
                    "diffPairGap": netclass.GetDiffPairGap() / scale,
                    "nets": nets,
                },
            }

        except Exception as e:
            logger.error(f"Error creating net class: {str(e)}")
            return {
                "success": False,
                "message": "Failed to create net class",
                "errorDetails": str(e),
            }

    def add_copper_pour(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Add a copper pour (zone) to the PCB"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            layer = params.get("layer", "F.Cu")
            net = params.get("net")
            clearance = params.get("clearance")
            min_width = params.get("minWidth", 0.2)
            points = params.get("outline", params.get("points", []))
            priority = params.get("priority", 0)
            fill_type = params.get("fillType", "solid")  # solid or hatched

            # If no outline provided, use board outline
            if not points or len(points) < 3:
                board_box = self.board.GetBoardEdgesBoundingBox()
                if board_box.GetWidth() > 0 and board_box.GetHeight() > 0:
                    scale = 1000000  # nm to mm
                    x1 = board_box.GetX() / scale
                    y1 = board_box.GetY() / scale
                    x2 = (board_box.GetX() + board_box.GetWidth()) / scale
                    y2 = (board_box.GetY() + board_box.GetHeight()) / scale

                    # Detect corner radius from Edge.Cuts arcs so the zone rectangle
                    # stays inside the rounded board corners (avoids zone visually
                    # extending outside Edge.Cuts before refill)
                    corner_radius = 0.0
                    edge_layer_id = self.board.GetLayerID("Edge.Cuts")
                    for item in self.board.GetDrawings():
                        if item.GetLayer() == edge_layer_id and item.GetClass() == "PCB_ARC":
                            r = item.GetRadius() / scale
                            if r > corner_radius:
                                corner_radius = r
                    # Inset the zone rectangle by the corner radius so its corners
                    # lie on the straight portions of the board edge.
                    inset = corner_radius
                    points = [
                        {"x": x1 + inset, "y": y1 + inset},
                        {"x": x2 - inset, "y": y1 + inset},
                        {"x": x2 - inset, "y": y2 - inset},
                        {"x": x1 + inset, "y": y2 - inset},
                    ]
                else:
                    return {
                        "success": False,
                        "message": "Missing outline",
                        "errorDetails": "Provide an outline array or add a board outline first",
                    }

            # Get layer ID
            layer_id = self.board.GetLayerID(layer)
            if layer_id < 0:
                return {
                    "success": False,
                    "message": "Invalid layer",
                    "errorDetails": f"Layer '{layer}' does not exist",
                }

            # Create zone
            zone = pcbnew.ZONE(self.board)
            zone.SetLayer(layer_id)

            # Set net if provided
            if net:
                netinfo = self.board.GetNetInfo()
                nets_map = netinfo.NetsByName()
                if nets_map.has_key(net):
                    net_obj = nets_map[net]
                    zone.SetNet(net_obj)

            # Set zone properties
            scale = 1000000  # mm to nm
            zone.SetAssignedPriority(priority)

            if clearance is not None:
                zone.SetLocalClearance(int(clearance * scale))

            zone.SetMinThickness(int(min_width * scale))

            # Set fill type
            if fill_type == "hatched":
                zone.SetFillMode(pcbnew.ZONE_FILL_MODE_HATCH_PATTERN)
            else:
                zone.SetFillMode(pcbnew.ZONE_FILL_MODE_POLYGONS)

            # Create outline
            outline = zone.Outline()
            outline.NewOutline()  # Create a new outline contour first

            # Add points to outline
            for point in points:
                scale = 1000000 if point.get("unit", "mm") == "mm" else 25400000
                x_nm = int(point["x"] * scale)
                y_nm = int(point["y"] * scale)
                outline.Append(pcbnew.VECTOR2I(x_nm, y_nm))  # Add point to outline

            # Add zone to board
            self.board.Add(zone)

            # Fill zone
            # Note: Zone filling can cause issues with SWIG API
            # Comment out for now - zones will be filled when board is saved/opened in KiCAD
            # filler = pcbnew.ZONE_FILLER(self.board)
            # filler.Fill(self.board.Zones())

            return {
                "success": True,
                "message": "Added copper pour",
                "pour": {
                    "layer": layer,
                    "net": net,
                    "clearance": clearance,
                    "minWidth": min_width,
                    "priority": priority,
                    "fillType": fill_type,
                    "pointCount": len(points),
                },
            }

        except Exception as e:
            logger.error(f"Error adding copper pour: {str(e)}")
            return {
                "success": False,
                "message": "Failed to add copper pour",
                "errorDetails": str(e),
            }

    def route_differential_pair(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Route a differential pair between two sets of points or pads"""
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            start_pos = params.get("startPos")
            end_pos = params.get("endPos")
            net_pos = params.get("netPos")
            net_neg = params.get("netNeg")
            layer = params.get("layer", "F.Cu")
            width = params.get("width")
            gap = params.get("gap")

            if not start_pos or not end_pos or not net_pos or not net_neg:
                return {
                    "success": False,
                    "message": "Missing parameters",
                    "errorDetails": "startPos, endPos, netPos, and netNeg are required",
                }

            # Get layer ID
            layer_id = self.board.GetLayerID(layer)
            if layer_id < 0:
                return {
                    "success": False,
                    "message": "Invalid layer",
                    "errorDetails": f"Layer '{layer}' does not exist",
                }

            # Get nets
            netinfo = self.board.GetNetInfo()
            nets_map = netinfo.NetsByName()

            net_pos_obj = nets_map[net_pos] if nets_map.has_key(net_pos) else None
            net_neg_obj = nets_map[net_neg] if nets_map.has_key(net_neg) else None

            if not net_pos_obj or not net_neg_obj:
                return {
                    "success": False,
                    "message": "Nets not found",
                    "errorDetails": "One or both nets specified for the differential pair do not exist",
                }

            # Get start and end points
            start_point = self._get_point(start_pos)
            end_point = self._get_point(end_pos)

            # Calculate offset vectors for the two traces
            # First, get the direction vector from start to end
            dx = end_point.x - start_point.x
            dy = end_point.y - start_point.y
            length = math.sqrt(dx * dx + dy * dy)

            if length <= 0:
                return {
                    "success": False,
                    "message": "Invalid points",
                    "errorDetails": "Start and end points must be different",
                }

            # Normalize direction vector
            dx /= length
            dy /= length

            # Get perpendicular vector
            px = -dy
            py = dx

            # Set default gap if not provided
            if gap is None:
                gap = 0.2  # mm

            # Convert to nm
            gap_nm = int(gap * 1000000)

            # Calculate offsets
            offset_x = int(px * gap_nm / 2)
            offset_y = int(py * gap_nm / 2)

            # Create positive and negative trace points
            pos_start = pcbnew.VECTOR2I(
                int(start_point.x + offset_x), int(start_point.y + offset_y)
            )
            pos_end = pcbnew.VECTOR2I(int(end_point.x + offset_x), int(end_point.y + offset_y))
            neg_start = pcbnew.VECTOR2I(
                int(start_point.x - offset_x), int(start_point.y - offset_y)
            )
            neg_end = pcbnew.VECTOR2I(int(end_point.x - offset_x), int(end_point.y - offset_y))

            # Create positive trace
            pos_track = pcbnew.PCB_TRACK(self.board)
            pos_track.SetStart(pos_start)
            pos_track.SetEnd(pos_end)
            pos_track.SetLayer(layer_id)
            pos_track.SetNet(net_pos_obj)

            # Create negative trace
            neg_track = pcbnew.PCB_TRACK(self.board)
            neg_track.SetStart(neg_start)
            neg_track.SetEnd(neg_end)
            neg_track.SetLayer(layer_id)
            neg_track.SetNet(net_neg_obj)

            # Set width
            if width:
                trace_width_nm = int(width * 1000000)
                pos_track.SetWidth(trace_width_nm)
                neg_track.SetWidth(trace_width_nm)
            else:
                # Get default width from design rules or net class
                trace_width = self.board.GetDesignSettings().GetCurrentTrackWidth()
                pos_track.SetWidth(trace_width)
                neg_track.SetWidth(trace_width)

            # Add tracks to board
            self.board.Add(pos_track)
            self.board.Add(neg_track)

            return {
                "success": True,
                "message": "Added differential pair traces",
                "diffPair": {
                    "posNet": net_pos,
                    "negNet": net_neg,
                    "layer": layer,
                    "width": pos_track.GetWidth() / 1000000,
                    "gap": gap,
                    "length": length / 1000000,
                },
            }

        except Exception as e:
            logger.error(f"Error routing differential pair: {str(e)}")
            return {
                "success": False,
                "message": "Failed to route differential pair",
                "errorDetails": str(e),
            }

    def _get_point(self, point_spec: Dict[str, Any]) -> pcbnew.VECTOR2I:
        """Convert point specification to KiCAD point"""
        if "x" in point_spec and "y" in point_spec:
            scale = 1000000 if point_spec.get("unit", "mm") == "mm" else 25400000
            x_nm = int(point_spec["x"] * scale)
            y_nm = int(point_spec["y"] * scale)
            return pcbnew.VECTOR2I(x_nm, y_nm)
        elif "pad" in point_spec and "componentRef" in point_spec:
            module = self.board.FindFootprintByReference(point_spec["componentRef"])
            if module:
                pad = module.FindPadByName(point_spec["pad"])
                if pad:
                    return pad.GetPosition()
        raise ValueError("Invalid point specification")

    def _point_to_track_distance(self, point: pcbnew.VECTOR2I, track: pcbnew.PCB_TRACK) -> float:
        """Calculate distance from point to track segment"""
        start = track.GetStart()
        end = track.GetEnd()

        # Vector from start to end
        v = pcbnew.VECTOR2I(end.x - start.x, end.y - start.y)
        # Vector from start to point
        w = pcbnew.VECTOR2I(point.x - start.x, point.y - start.y)

        # Length of track squared
        c1 = v.x * v.x + v.y * v.y
        if c1 == 0:
            return self._point_distance(point, start)

        # Projection coefficient
        c2 = float(w.x * v.x + w.y * v.y) / c1

        if c2 < 0:
            return self._point_distance(point, start)
        elif c2 > 1:
            return self._point_distance(point, end)

        # Point on line
        proj = pcbnew.VECTOR2I(int(start.x + c2 * v.x), int(start.y + c2 * v.y))
        return self._point_distance(point, proj)

    def _point_distance(self, p1: pcbnew.VECTOR2I, p2: pcbnew.VECTOR2I) -> float:
        """Calculate distance between two points"""
        dx = p1.x - p2.x
        dy = p1.y - p2.y
        return (dx * dx + dy * dy) ** 0.5

    def find_via_lane(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Propose a via-jumper route when the direct same-layer path is
        blocked by foreign-net copper. Tries (in order):

          A. Direct: straight segment on fromLayer. If clear → done.
          B. Via-jumper: place via1 in fromLayer's clear zone near source
             and via2 in fromLayer's clear zone near target; connect them
             with a straight segment on viaLayer.
          C. Via-jumper with waypoint: same vias, but search for a
             perpendicular-offset waypoint on viaLayer that lets a
             2-segment route around the blocking obstacle.
          D. 2D grid waypoint search (1 mm steps within ±waypoint_max,
             sorted by Manhattan distance from the midpoint).
          E. Axis-aligned 2-waypoint L-shape (HVH or VHV) with blind
             1 mm extension sweep within ±waypoint_max.
          F. Obstacle-bbox-aware L-shape (v3): compute the union bbox of
             obstacles blocking the straight via-layer segment, then try
             4 L-shapes — one extending past each bbox edge + clearance
             margin. Bypasses long blockers (e.g. a 20 mm horizontal
             trace) that the blind ±waypoint_max sweep can't escape.
          G. 4-corner Z-shape (v4): when via1 and via2 straddle the bbox
             (one on each side of the blocker) AND the L-shape east/west
             legs hit secondary obstacles near the bbox, route around a
             bbox CORNER with a 4-segment Z-shape (HVHV or VHVH pattern,
             4 corners × 2 patterns = 8 candidates).

        Via clearance: the walker that picks via1/via2 is now
        clearance-aware (v4.1, Strategy H) — at each sample position
        it checks BOTH (a) the F.Cu approach segment is clear AND (b)
        a through-via at the sample would clear all foreign-net copper
        on every layer by minClearance. So via1/via2 land at the
        furthest position that's safe for both checks. After placement,
        a final defense-in-depth verification still runs; if it ever
        fails, the tool refuses with `via_clearance_violation` and
        lists what's too close.

        Inputs:
          from, to        — {x, y, unit} points OR {ref, pad} pad lookups
          net             — net name (required)
          fromLayer       — default F.Cu
          viaLayer        — default B.Cu
          width           — trace width mm (default 0.2)
          viaDiameter     — via outer diameter mm (default 0.6)
          viaDrill        — via drill mm (default 0.3)
          safetyMargin    — pull-back from first obstacle on fromLayer
                            when placing vias, mm (default 0.5)
          waypointSearchMax — max perpendicular offset for strategy C,
                              mm (default 10)
          apply           — if true, commit the proposed route; else preview

        Returns {success, strategy, path, vias} on success, or
        {success: false, strategy, obstacles, ...} on failure with the
        diagnostic of what blocked which leg.
        """
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            net = params.get("net")
            if not net:
                return {
                    "success": False,
                    "message": "Missing net",
                    "errorDetails": "net parameter is required",
                }

            from_layer = params.get("fromLayer", "F.Cu")
            via_layer = params.get("viaLayer", "B.Cu")
            from_id = self.board.GetLayerID(from_layer)
            via_id = self.board.GetLayerID(via_layer)
            if from_id < 0 or via_id < 0:
                return {
                    "success": False,
                    "message": "Invalid layer",
                    "errorDetails": f"Bad fromLayer/viaLayer: {from_layer}/{via_layer}",
                }

            width_mm = float(params.get("width", 0.2))
            via_diam_mm = float(params.get("viaDiameter", 0.6))
            via_drill_mm = float(params.get("viaDrill", 0.3))
            margin_mm = float(params.get("safetyMargin", 0.5))
            margin_iu = int(margin_mm * 1_000_000)
            waypoint_max_mm = float(params.get("waypointSearchMax", 10.0))
            min_stub_mm = float(params.get("minimumStubLength", 0.0))
            min_stub_iu = int(min_stub_mm * 1_000_000)
            min_clearance_mm = float(params.get("minClearance", 0.15))
            min_clearance_iu = int(min_clearance_mm * 1_000_000)
            via_diam_iu = int(via_diam_mm * 1_000_000)
            apply = bool(params.get("apply", False))

            from_pt = self._resolve_route_endpoint(params.get("from"))
            to_pt = self._resolve_route_endpoint(params.get("to"))
            if from_pt is None or to_pt is None:
                return {
                    "success": False,
                    "message": "Could not resolve from/to",
                    "errorDetails": (
                        "Each of `from` and `to` must be either {x, y, unit} "
                        "or {ref, pad} with a real footprint pad."
                    ),
                }

            def _pt_dict(pt) -> Dict[str, Any]:
                return {"x": pt.x / 1e6, "y": pt.y / 1e6, "unit": "mm"}

            # --- Strategy A: direct on fromLayer ---
            obs_a = self._find_route_obstacles(from_pt, to_pt, from_id, net)
            if not obs_a:
                path = [{
                    "kind": "track", "layer": from_layer, "width": width_mm,
                    "net": net,
                    "start": _pt_dict(from_pt), "end": _pt_dict(to_pt),
                }]
                if apply:
                    self.route_trace({
                        "start": _pt_dict(from_pt), "end": _pt_dict(to_pt),
                        "layer": from_layer, "width": width_mm, "net": net,
                        "checkObstacles": False,
                    })
                return {
                    "success": True, "strategy": "direct", "applied": apply,
                    "path": path, "vias": [],
                    "message": "Direct same-layer route is clear; no via needed.",
                }

            # --- Strategy B prep: find safe via insertion points on fromLayer ---
            # via_diam_iu + min_clearance_iu enable Strategy H — the
            # walker integrates via-vs-copper clearance, so via1/via2
            # naturally land at the furthest position that's safe both
            # for the F.Cu approach segment AND for the through-via's
            # interaction with foreign copper on every other layer.
            via1 = self._find_safe_via_point(
                from_pt, to_pt, from_id, net, margin_iu,
                via_diam_iu=via_diam_iu,
                min_clearance_iu=min_clearance_iu,
            )
            via2 = self._find_safe_via_point(
                to_pt, from_pt, from_id, net, margin_iu,
                via_diam_iu=via_diam_iu,
                min_clearance_iu=min_clearance_iu,
            )
            if via1 is None or via2 is None:
                # Distinguish failure mode: did the F.Cu segment hit an
                # obstacle, or did via-clearance fail along the walk?
                # Sample a few points near the failing endpoint and
                # report what's actually blocking the via.
                endpoint = from_pt if via1 is None else to_pt
                _diag = self._via_clearance_violations(
                    endpoint, via_diam_iu, net, min_clearance_iu
                )[:3]
                return {
                    "success": False, "strategy": "no_safe_via_zone",
                    "errorDetails": (
                        "No safe via insertion point on fromLayer "
                        f"near {'source' if via1 is None else 'target'}. "
                        "Either the F.Cu approach segment hits an "
                        "obstacle within safetyMargin, OR a through-via "
                        "anywhere along the walk shorts to nearby copper "
                        f"on another layer (minClearance={min_clearance_mm} "
                        f"mm, viaDiameter={via_diam_mm} mm). Hand-route a "
                        "longer stub away from the dense area, reduce "
                        "viaDiameter (microvia), or lower minClearance."
                    ),
                    "obstaclesFromLayer": obs_a,
                    "viaClearanceAtEndpoint": _diag,
                }

            # minimumStubLength: refuse if via1 too close to source pad or
            # via2 too close to target pad. Default 0 (off); set ≥1 mm when
            # source/target are real component pads to keep vias off them.
            if min_stub_iu > 0:
                def _dist(a, b):
                    return ((a.x - b.x) ** 2 + (a.y - b.y) ** 2) ** 0.5
                if _dist(from_pt, via1) < min_stub_iu:
                    return {
                        "success": False,
                        "strategy": "stub_too_short_source",
                        "errorDetails": (
                            f"Safe via insertion point is "
                            f"{_dist(from_pt, via1) / 1e6:.3f} mm from source, "
                            f"below minimumStubLength={min_stub_mm} mm. "
                            "This would put the via inside the source pad's "
                            "footprint area. Hand-route a longer stub out of "
                            "the pad column first, or lower minimumStubLength."
                        ),
                        "via1": _pt_dict(via1),
                    }
                if _dist(to_pt, via2) < min_stub_iu:
                    return {
                        "success": False,
                        "strategy": "stub_too_short_target",
                        "errorDetails": (
                            f"Safe via insertion point is "
                            f"{_dist(to_pt, via2) / 1e6:.3f} mm from target, "
                            f"below minimumStubLength={min_stub_mm} mm."
                        ),
                        "via2": _pt_dict(via2),
                    }

            # --- Via clearance check (v3.1) ---
            # _find_safe_via_point only checked SEGMENT-vs-copper clearance.
            # Verify the via itself (a 0.6 mm default through via touches
            # every copper layer) clears all foreign-net copper by at least
            # via_radius + minClearance. Catches the "via lands on the right
            # X/Y line but its diameter overlaps an adjacent QFN pin / pad
            # row / nearby via" case (forcing function: CHG_OUT U3.10).
            via1_clear = self._via_clearance_violations(
                via1, via_diam_iu, net, min_clearance_iu
            )
            via2_clear = self._via_clearance_violations(
                via2, via_diam_iu, net, min_clearance_iu
            )
            if via1_clear or via2_clear:
                return {
                    "success": False,
                    "strategy": "via_clearance_violation",
                    "errorDetails": (
                        "Proposed via overlaps foreign-net copper (the "
                        "segment is clear but the via diameter isn't). "
                        "Increase minimumStubLength to push the via further "
                        "from the pad, reduce viaDiameter (e.g. microvia), "
                        "or hand-route. minClearance="
                        f"{min_clearance_mm} mm, viaDiameter={via_diam_mm} mm."
                    ),
                    "via1": _pt_dict(via1),
                    "via2": _pt_dict(via2),
                    "via1Violations": via1_clear,
                    "via2Violations": via2_clear,
                }

            # --- Strategy B: straight via-jumper on viaLayer ---
            obs_b = self._find_route_obstacles(via1, via2, via_id, net)
            if not obs_b:
                return self._emit_via_jumper(
                    from_pt, via1, via2, to_pt, [], from_layer, via_layer,
                    width_mm, via_diam_mm, via_drill_mm, net, apply,
                    strategy="via_jumper",
                )

            # --- Strategy C: single-waypoint perpendicular-offset search ---
            # Quick first pass — handles the common "obstacle blocks one
            # side only" case in O(N) waypoint checks.
            for offset_mm in [
                x * 0.5 for x in range(1, int(waypoint_max_mm * 2) + 1)
            ]:
                for sign in (+1, -1):
                    wp = self._perpendicular_offset_midpoint(
                        via1, via2, offset_mm * sign
                    )
                    if self._find_route_obstacles(via1, wp, via_id, net):
                        continue
                    if self._find_route_obstacles(wp, via2, via_id, net):
                        continue
                    return self._emit_via_jumper(
                        from_pt, via1, via2, to_pt, [wp],
                        from_layer, via_layer, width_mm,
                        via_diam_mm, via_drill_mm, net, apply,
                        strategy="via_jumper_with_waypoint",
                    )

            # --- Strategy D: 2D grid waypoint search ---
            # For cases where the perpendicular-only search missed because
            # the clear region is offset in both axes. Sweep ±waypoint_max
            # in 1 mm steps, sorted by Manhattan distance from midpoint so
            # we find the closest clear waypoint first.
            mx = (via1.x + via2.x) / 2
            my = (via1.y + via2.y) / 2
            grid_range = int(waypoint_max_mm)
            offsets = []
            for ox in range(-grid_range, grid_range + 1):
                for oy in range(-grid_range, grid_range + 1):
                    if ox == 0 and oy == 0:
                        continue
                    offsets.append((ox, oy, abs(ox) + abs(oy)))
            offsets.sort(key=lambda o: o[2])
            for ox, oy, _ in offsets:
                wp = pcbnew.VECTOR2I(
                    int(mx + ox * 1_000_000), int(my + oy * 1_000_000)
                )
                if self._find_route_obstacles(via1, wp, via_id, net):
                    continue
                if self._find_route_obstacles(wp, via2, via_id, net):
                    continue
                return self._emit_via_jumper(
                    from_pt, via1, via2, to_pt, [wp],
                    from_layer, via_layer, width_mm,
                    via_diam_mm, via_drill_mm, net, apply,
                    strategy="via_jumper_with_grid_waypoint",
                )

            # --- Strategy E: 2-waypoint axis-aligned L-shape ---
            # Genuine HVH or VHV detour around a blocking obstacle that
            # spans the whole single-waypoint search region (e.g.
            # BB_BOOT2-style long horizontal traces). Sweep the extension
            # axis in 1 mm steps within ±waypoint_max.
            for ext_mm in range(1, int(waypoint_max_mm) + 1):
                for sign in (+1, -1):
                    # HVH (horizontal extension): c1 = (via1.x + ext, via1.y),
                    # c2 = (via1.x + ext, via2.y)
                    ext_x = int(via1.x + sign * ext_mm * 1_000_000)
                    c1 = pcbnew.VECTOR2I(ext_x, via1.y)
                    c2 = pcbnew.VECTOR2I(ext_x, via2.y)
                    if (not self._find_route_obstacles(via1, c1, via_id, net)
                        and not self._find_route_obstacles(c1, c2, via_id, net)
                        and not self._find_route_obstacles(c2, via2, via_id, net)):
                        return self._emit_via_jumper(
                            from_pt, via1, via2, to_pt, [c1, c2],
                            from_layer, via_layer, width_mm,
                            via_diam_mm, via_drill_mm, net, apply,
                            strategy="via_jumper_hvh_lshape",
                        )
                    # VHV (vertical extension): c1 = (via1.x, via1.y + ext),
                    # c2 = (via2.x, via1.y + ext)
                    ext_y = int(via1.y + sign * ext_mm * 1_000_000)
                    c1 = pcbnew.VECTOR2I(via1.x, ext_y)
                    c2 = pcbnew.VECTOR2I(via2.x, ext_y)
                    if (not self._find_route_obstacles(via1, c1, via_id, net)
                        and not self._find_route_obstacles(c1, c2, via_id, net)
                        and not self._find_route_obstacles(c2, via2, via_id, net)):
                        return self._emit_via_jumper(
                            from_pt, via1, via2, to_pt, [c1, c2],
                            from_layer, via_layer, width_mm,
                            via_diam_mm, via_drill_mm, net, apply,
                            strategy="via_jumper_vhv_lshape",
                        )

            # --- Strategy F (v3): obstacle-bbox-aware L-shape ---
            # The blind sweeps above step in 1 mm increments within
            # ±waypoint_max from the midpoint, so they can't escape a
            # long obstacle (e.g. a horizontal trace spanning 20 mm)
            # unless waypoint_max happens to exceed half its length.
            # v3: compute the union bbox of obstacles blocking via1→via2,
            # then propose 4 candidate L-shapes — each extends past one
            # bbox edge + clearance margin — so the detour is sized to
            # the obstacle's actual extent, not a blind search radius.
            obstacle_items = list(self._iter_route_obstacles(
                via1, via2, via_id, net
            ))
            bbox = self._obstacle_union_bbox(obstacle_items)
            tried_bbox = []
            if bbox is not None:
                xmin, ymin, xmax, ymax = bbox
                # Clearance: via radius + 0.25 mm pad (covers default
                # min-clearance for POWER_2A/etc). Hard-coded; expose
                # later if a real case needs tuning.
                clear_iu = int((via_diam_mm / 2 + 0.25) * 1_000_000)
                edges = [
                    ("east_hvh",  int(xmax + clear_iu), None),
                    ("west_hvh",  int(xmin - clear_iu), None),
                    ("north_vhv", None, int(ymin - clear_iu)),
                    ("south_vhv", None, int(ymax + clear_iu)),
                ]
                for label, ext_x, ext_y in edges:
                    if ext_x is not None:
                        c1 = pcbnew.VECTOR2I(ext_x, via1.y)
                        c2 = pcbnew.VECTOR2I(ext_x, via2.y)
                    else:
                        c1 = pcbnew.VECTOR2I(via1.x, ext_y)
                        c2 = pcbnew.VECTOR2I(via2.x, ext_y)
                    leg1 = self._find_route_obstacles(via1, c1, via_id, net)
                    leg2 = self._find_route_obstacles(c1, c2, via_id, net)
                    leg3 = self._find_route_obstacles(c2, via2, via_id, net)
                    if not leg1 and not leg2 and not leg3:
                        return self._emit_via_jumper(
                            from_pt, via1, via2, to_pt, [c1, c2],
                            from_layer, via_layer, width_mm,
                            via_diam_mm, via_drill_mm, net, apply,
                            strategy=f"via_jumper_bbox_{label}",
                        )
                    tried_bbox.append({
                        "edge": label,
                        "leg1_blocked": leg1[:3],
                        "leg2_blocked": leg2[:3],
                        "leg3_blocked": leg3[:3],
                    })

            # --- Strategy G (v4): 4-segment Z-shape around bbox corners ---
            # When via1 and via2 straddle the bbox (one on each side of
            # the blocker) AND the L-shape east/west legs hit secondary
            # obstacles in the bbox vicinity, the single-bend HVH can't
            # dodge them. A 4-segment Z-shape goes around a bbox CORNER
            # instead of past a single edge — extending past BOTH an
            # x-edge AND a y-edge of the bbox simultaneously. 4 corners
            # × 2 patterns (HVHV starting vertical, VHVH starting
            # horizontal) = 8 candidate detours.
            tried_z = []
            if bbox is not None:
                xmin, ymin, xmax, ymax = bbox
                # Wider clearance than Strategy F — corner detours often
                # need to dodge end-of-trace vias that sit at the bbox
                # edge. Pad the safety margin to include min_clearance
                # plus an extra 0.4 mm to be on the safe side.
                clear_g_iu = int(
                    (via_diam_mm / 2 + min_clearance_mm + 0.4) * 1_000_000
                )
                corners = [
                    ("ne", int(xmax + clear_g_iu), int(ymin - clear_g_iu)),
                    ("nw", int(xmin - clear_g_iu), int(ymin - clear_g_iu)),
                    ("se", int(xmax + clear_g_iu), int(ymax + clear_g_iu)),
                    ("sw", int(xmin - clear_g_iu), int(ymax + clear_g_iu)),
                ]
                for corner_label, ext_x, ext_y in corners:
                    # HVHV: via1 → (via1.x, ext_y) → (ext_x, ext_y)
                    #     → (ext_x, via2.y) → via2
                    c1 = pcbnew.VECTOR2I(via1.x, ext_y)
                    c2 = pcbnew.VECTOR2I(ext_x, ext_y)
                    c3 = pcbnew.VECTOR2I(ext_x, via2.y)
                    legs = [
                        self._find_route_obstacles(via1, c1, via_id, net),
                        self._find_route_obstacles(c1, c2, via_id, net),
                        self._find_route_obstacles(c2, c3, via_id, net),
                        self._find_route_obstacles(c3, via2, via_id, net),
                    ]
                    if not any(legs):
                        return self._emit_via_jumper(
                            from_pt, via1, via2, to_pt, [c1, c2, c3],
                            from_layer, via_layer, width_mm,
                            via_diam_mm, via_drill_mm, net, apply,
                            strategy=f"via_jumper_z_{corner_label}_hvhv",
                        )
                    tried_z.append({
                        "corner": corner_label, "pattern": "hvhv",
                        "leg1_blocked": legs[0][:2],
                        "leg2_blocked": legs[1][:2],
                        "leg3_blocked": legs[2][:2],
                        "leg4_blocked": legs[3][:2],
                    })
                    # VHVH: via1 → (ext_x, via1.y) → (ext_x, ext_y)
                    #     → (via2.x, ext_y) → via2
                    c1 = pcbnew.VECTOR2I(ext_x, via1.y)
                    c2 = pcbnew.VECTOR2I(ext_x, ext_y)
                    c3 = pcbnew.VECTOR2I(via2.x, ext_y)
                    legs = [
                        self._find_route_obstacles(via1, c1, via_id, net),
                        self._find_route_obstacles(c1, c2, via_id, net),
                        self._find_route_obstacles(c2, c3, via_id, net),
                        self._find_route_obstacles(c3, via2, via_id, net),
                    ]
                    if not any(legs):
                        return self._emit_via_jumper(
                            from_pt, via1, via2, to_pt, [c1, c2, c3],
                            from_layer, via_layer, width_mm,
                            via_diam_mm, via_drill_mm, net, apply,
                            strategy=f"via_jumper_z_{corner_label}_vhvh",
                        )
                    tried_z.append({
                        "corner": corner_label, "pattern": "vhvh",
                        "leg1_blocked": legs[0][:2],
                        "leg2_blocked": legs[1][:2],
                        "leg3_blocked": legs[2][:2],
                        "leg4_blocked": legs[3][:2],
                    })

            return {
                "success": False, "strategy": "blocked_on_via_layer",
                "errorDetails": (
                    f"viaLayer ({via_layer}) is blocked: tried straight, "
                    f"perpendicular-offset waypoint, 2D grid search, "
                    f"axis-aligned L-shape within ±{waypoint_max_mm} mm, "
                    "obstacle-bbox-aware L-shape, and 4-corner Z-shape — "
                    "all hit foreign-net copper. Try a different viaLayer, "
                    "increase waypointSearchMax, or hand-route around."
                ),
                "via1": _pt_dict(via1),
                "via2": _pt_dict(via2),
                "obstaclesViaLayerStraight": obs_b,
                "obstacleBbox": (
                    {"xmin": bbox[0] / 1e6, "ymin": bbox[1] / 1e6,
                     "xmax": bbox[2] / 1e6, "ymax": bbox[3] / 1e6,
                     "unit": "mm"}
                    if bbox else None
                ),
                "bboxLshapesTried": tried_bbox,
                "zShapesTried": tried_z,
            }

        except Exception as e:
            logger.error(f"Error in find_via_lane: {str(e)}")
            return {
                "success": False,
                "message": "Failed to find via lane",
                "errorDetails": str(e),
            }

    def _resolve_route_endpoint(self, spec):
        """Resolve a from/to spec to a VECTOR2I. spec is either
        {x, y, unit} or {ref, pad}."""
        if not spec:
            return None
        # XY form
        if "x" in spec and "y" in spec:
            return self._get_point(spec)
        # Pad form
        ref = spec.get("ref")
        pad_num = spec.get("pad")
        if ref is None or pad_num is None:
            return None
        fp = self.board.FindFootprintByReference(str(ref))
        if not fp:
            return None
        for pad in fp.Pads():
            if pad.GetNumber() == str(pad_num):
                return pad.GetPosition()
        return None

    def _find_safe_via_point(self, from_pt, to_pt, layer_id, net,
                              margin_iu, n_samples: int = 40,
                              via_diam_iu: int = 0,
                              min_clearance_iu: int = 0):
        """Walk from from_pt toward to_pt on layer; return the furthest
        point along the line where (a) the segment from_pt→point is
        clear on layer_id, AND (b) a through-via placed at point clears
        all foreign-net copper on every copper layer by at least
        min_clearance_iu. Pull-back by margin_iu along the direction.

        When via_diam_iu=0 or min_clearance_iu=0 the via-clearance gate
        is disabled and behavior matches the pre-Strategy-H walker
        (segment check only).

        Strategy H (v4.1): the via-clearance gate is what lets via1/
        via2 walk past nearby-but-different-layer copper that would
        otherwise short to the through via (e.g. C26 via1 vs a B.Cu
        BB_BOOT2 track 0.13 mm away — same-layer F.Cu walk was clear,
        but the via would have shorted). The loop continues past via-
        clearance failures (doesn't break) because a later sample,
        further from the offending trace, may pass.
        """
        dx = to_pt.x - from_pt.x
        dy = to_pt.y - from_pt.y
        total = (dx * dx + dy * dy) ** 0.5
        if total == 0:
            return from_pt
        check_via_clearance = via_diam_iu > 0 and min_clearance_iu > 0
        best_t = 0.0
        for i in range(1, n_samples + 1):
            t = i / n_samples
            px = int(from_pt.x + t * dx)
            py = int(from_pt.y + t * dy)
            pt = pcbnew.VECTOR2I(px, py)
            # Segment check: same-layer obstacles from from_pt → pt.
            if self._find_route_obstacles(from_pt, pt, layer_id, net):
                break  # Once the segment hits an obstacle, all later
                       # samples also cross it. Stop.
            # Via clearance check: would a through via at pt short to
            # nearby foreign-net copper on any layer? If yes, skip this
            # sample but keep walking (clearance failures aren't
            # monotone — further samples may clear the nearby trace).
            if check_via_clearance and self._via_clearance_violations(
                pt, via_diam_iu, net, min_clearance_iu
            ):
                continue
            best_t = t
        if best_t == 0.0:
            return None
        best_len = best_t * total
        if best_len <= margin_iu:
            return None
        pull_t = (best_len - margin_iu) / total
        return pcbnew.VECTOR2I(
            int(from_pt.x + pull_t * dx),
            int(from_pt.y + pull_t * dy),
        )

    def _perpendicular_offset_midpoint(self, a, b, offset_mm: float):
        """Midpoint of segment a→b shifted by offset_mm perpendicular to
        the segment. Positive offset_mm = left side (rot +90° from a→b)."""
        dx = b.x - a.x
        dy = b.y - a.y
        seg_len = (dx * dx + dy * dy) ** 0.5
        if seg_len == 0:
            return a
        # Perpendicular unit (rot +90°): (-dy, dx) / seg_len
        offset_iu = offset_mm * 1_000_000
        pdx = -dy / seg_len * offset_iu
        pdy = dx / seg_len * offset_iu
        mx = (a.x + b.x) / 2 + pdx
        my = (a.y + b.y) / 2 + pdy
        return pcbnew.VECTOR2I(int(mx), int(my))

    def _emit_via_jumper(self, from_pt, via1, via2, to_pt, waypoints,
                          from_layer, via_layer, width_mm,
                          via_diam_mm, via_drill_mm, net, apply,
                          strategy: str) -> Dict[str, Any]:
        """Build the path/vias result for a via-jumper route, and commit
        the segments+vias if apply=true. waypoints is the list of
        intermediate viaLayer points between via1 and via2 (empty for
        straight, [wp] for 1-bend)."""
        def _pt_dict(pt):
            return {"x": pt.x / 1e6, "y": pt.y / 1e6, "unit": "mm"}

        path = [
            {"kind": "track", "layer": from_layer, "width": width_mm,
             "net": net, "start": _pt_dict(from_pt), "end": _pt_dict(via1)},
        ]
        prev = via1
        for wp in waypoints:
            path.append(
                {"kind": "track", "layer": via_layer, "width": width_mm,
                 "net": net, "start": _pt_dict(prev), "end": _pt_dict(wp)}
            )
            prev = wp
        path.append(
            {"kind": "track", "layer": via_layer, "width": width_mm,
             "net": net, "start": _pt_dict(prev), "end": _pt_dict(via2)}
        )
        path.append(
            {"kind": "track", "layer": from_layer, "width": width_mm,
             "net": net, "start": _pt_dict(via2), "end": _pt_dict(to_pt)}
        )
        vias = [
            {"position": _pt_dict(via1), "diameter": via_diam_mm,
             "drill": via_drill_mm, "net": net,
             "from_layer": from_layer, "to_layer": via_layer},
            {"position": _pt_dict(via2), "diameter": via_diam_mm,
             "drill": via_drill_mm, "net": net,
             "from_layer": via_layer, "to_layer": from_layer},
        ]

        if apply:
            for seg in path:
                r = self.route_trace({
                    "start": seg["start"], "end": seg["end"],
                    "layer": seg["layer"], "width": seg["width"],
                    "net": seg["net"], "checkObstacles": False,
                })
                if not r.get("success"):
                    return {
                        "success": False,
                        "message": "Failed to commit segment",
                        "errorDetails": r.get("errorDetails", str(r)),
                        "partialPath": path, "partialVias": vias,
                    }
            for v in vias:
                self.add_via({
                    "position": v["position"], "size": v["diameter"],
                    "drill": v["drill"], "net": v["net"],
                    "from_layer": v["from_layer"], "to_layer": v["to_layer"],
                })

        return {
            "success": True, "strategy": strategy, "applied": apply,
            "path": path, "vias": vias,
            "message": (
                f"{'Committed' if apply else 'Proposed'} via-jumper: "
                f"{len(path)} segments + {len(vias)} vias on net '{net}'"
            ),
        }

    def check_route_segment(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Pre-flight check: would a straight segment from start to end on
        the given layer (for the given net) cross foreign-net copper?

        Returns {clear: bool, obstacles: [...]} without mutating the board.
        Useful for plan-first workflows: enumerate candidate routes, pick a
        clear one, then commit with route_trace. Same obstacle detection as
        route_trace's checkObstacles default, just without the commit.
        """
        try:
            if not self.board:
                return {
                    "success": False,
                    "message": "No board is loaded",
                    "errorDetails": "Load or create a board first",
                }

            start = params.get("start")
            end = params.get("end")
            layer = params.get("layer", "F.Cu")
            net = params.get("net")
            width = params.get("width")
            clearance = params.get("clearance")

            if not start or not end:
                return {
                    "success": False,
                    "message": "Missing parameters",
                    "errorDetails": "start and end points are required",
                }
            if not net:
                return {
                    "success": False,
                    "message": "Missing parameters",
                    "errorDetails": (
                        "net is required (obstacles are computed relative "
                        "to the net you intend to route — same-net copper "
                        "isn't an obstacle)"
                    ),
                }

            layer_id = self.board.GetLayerID(layer)
            if layer_id < 0:
                return {
                    "success": False,
                    "message": "Invalid layer",
                    "errorDetails": f"Layer '{layer}' does not exist",
                }

            start_pt = self._get_point(start)
            end_pt = self._get_point(end)
            trace_width_iu, min_clearance_iu = self._resolve_route_clearance(
                width, clearance, net
            )
            obstacles = self._find_route_obstacles(
                start_pt, end_pt, layer_id, net,
                trace_width_iu, min_clearance_iu,
            )
            return {
                "success": True,
                "clear": not obstacles,
                "obstacleCount": len(obstacles),
                "obstacles": obstacles,
                "start": {"x": start_pt.x / 1e6, "y": start_pt.y / 1e6, "unit": "mm"},
                "end": {"x": end_pt.x / 1e6, "y": end_pt.y / 1e6, "unit": "mm"},
                "layer": layer,
                "net": net,
            }
        except Exception as e:
            logger.error(f"Error in check_route_segment: {str(e)}")
            return {
                "success": False,
                "message": "Failed to check route segment",
                "errorDetails": str(e),
            }

    def _pad_outward_unit_vec(self, pad, footprint) -> Tuple[float, float]:
        """Unit vector pointing from the footprint body center to the pad.
        Used to pick a pin-escape direction perpendicular to the pin row
        (works for QFN/TSSOP/QFP/LGA where pads sit on the perimeter).
        Falls back to +x if the pad coincides with the footprint center
        (e.g. thermal pads or BGA balls)."""
        pp = pad.GetPosition()
        fp = footprint.GetPosition()
        dx = float(pp.x - fp.x)
        dy = float(pp.y - fp.y)
        mag = (dx * dx + dy * dy) ** 0.5
        if mag < 1.0:  # < 1 nm — effectively coincident
            return (1.0, 0.0)
        return (dx / mag, dy / mag)

    def _resolve_route_clearance(
        self,
        width_mm: Optional[float],
        clearance_mm: Optional[float],
        net_name: str,
    ) -> Tuple[int, int]:
        """Compute (trace_width_iu, min_clearance_iu) for obstacle checks.

        `width_mm` and `clearance_mm` are explicit caller overrides; either
        may be None. When width is None, falls back to the board's current
        track width. When clearance is None, looks up the net's netclass
        clearance, then the design rules default, then 0 as a last resort.
        Returned values are in pcbnew internal units (nm).
        """
        SCALE = 1_000_000

        if width_mm is not None:
            trace_width_iu = int(float(width_mm) * SCALE)
        else:
            try:
                trace_width_iu = int(
                    self.board.GetDesignSettings().GetCurrentTrackWidth()
                )
            except Exception:
                trace_width_iu = 0

        if clearance_mm is not None:
            min_clearance_iu = int(float(clearance_mm) * SCALE)
        else:
            min_clearance_iu = 0
            try:
                nets_map = self.board.GetNetInfo().NetsByName()
                if nets_map.has_key(net_name):
                    nc = nets_map[net_name].GetNetClass()
                    if nc is not None:
                        min_clearance_iu = int(nc.GetClearance())
            except Exception:
                pass
            if min_clearance_iu <= 0:
                try:
                    bds = self.board.GetDesignSettings()
                    default_nc = bds.GetDefault() if hasattr(
                        bds, "GetDefault"
                    ) else None
                    if default_nc is not None:
                        min_clearance_iu = int(default_nc.GetClearance())
                except Exception:
                    pass

        return max(0, int(trace_width_iu)), max(0, int(min_clearance_iu))

    def _iter_route_obstacles(
        self,
        start: pcbnew.VECTOR2I,
        end: pcbnew.VECTOR2I,
        layer_id: int,
        net_name: str,
        trace_width_iu: int = 0,
        min_clearance_iu: int = 0,
    ):
        """Yield the foreign-net copper objects that a proposed trace
        start->end on layer_id would collide with. Each yielded item is
        a tuple of (kind, obj[, extra]):

          ("via", pcbnew.PCB_VIA)
          ("track", pcbnew.PCB_TRACK)
          ("pad", pcbnew.PAD, footprint_ref_str)

        With `trace_width_iu=0` the test degenerates to centerline-only
        crossing (legacy behaviour for callers that don't know the
        width). With a non-zero width the trace is treated as a stadium
        of half-width = `trace_width_iu/2 + min_clearance_iu`; obstacles
        within that swept distance are reported. This catches the
        edge-clipping case where the centerline misses a neighbouring
        pad but the trace edge does not (#177).

        Shared core for both `_find_route_obstacles` (string output) and
        `_obstacle_union_bbox` (bbox geometry).
        """
        sx, sy, ex, ey = start.x, start.y, end.x, end.y
        trace_half = trace_width_iu // 2

        def seg_pt_dist(px: float, py: float) -> float:
            vx, vy = ex - sx, ey - sy
            c1 = vx * vx + vy * vy
            if c1 == 0:
                return ((px - sx) ** 2 + (py - sy) ** 2) ** 0.5
            t = max(0.0, min(1.0, ((px - sx) * vx + (py - sy) * vy) / c1))
            qx, qy = sx + t * vx, sy + t * vy
            return ((px - qx) ** 2 + (py - qy) ** 2) ** 0.5

        def ccw(ax, ay, bx, by, cx, cy) -> bool:
            return (cy - ay) * (bx - ax) > (by - ay) * (cx - ax)

        def segs_cross(cx, cy, dx, dy) -> bool:
            return ccw(sx, sy, cx, cy, dx, dy) != ccw(ex, ey, cx, cy, dx, dy) and ccw(
                sx, sy, ex, ey, cx, cy
            ) != ccw(sx, sy, ex, ey, dx, dy)

        def seg_seg_min_dist(
            ax: float, ay: float, bx: float, by: float,
            cx: float, cy: float, dx: float, dy: float,
        ) -> float:
            """Minimum distance between segment AB and segment CD in 2D.
            Returns 0 if they cross; otherwise the smallest endpoint-to-
            opposite-segment distance."""
            if (ccw(ax, ay, cx, cy, dx, dy) != ccw(bx, by, cx, cy, dx, dy)
                    and ccw(ax, ay, bx, by, cx, cy) != ccw(ax, ay, bx, by, dx, dy)):
                return 0.0

            def pt_seg(px, py, x1, y1, x2, y2):
                vx, vy = x2 - x1, y2 - y1
                ll = vx * vx + vy * vy
                if ll == 0:
                    return ((px - x1) ** 2 + (py - y1) ** 2) ** 0.5
                t = max(0.0, min(1.0, ((px - x1) * vx + (py - y1) * vy) / ll))
                qx, qy = x1 + t * vx, y1 + t * vy
                return ((px - qx) ** 2 + (py - qy) ** 2) ** 0.5

            return min(
                pt_seg(ax, ay, cx, cy, dx, dy),
                pt_seg(bx, by, cx, cy, dx, dy),
                pt_seg(cx, cy, ax, ay, bx, by),
                pt_seg(dx, dy, ax, ay, bx, by),
            )

        seg_len = ((ex - sx) ** 2 + (ey - sy) ** 2) ** 0.5

        # Tracks and vias
        for t in self.board.Tracks():
            if t.GetNetname() == net_name:
                continue
            if t.Type() == pcbnew.PCB_VIA_T:
                pos = t.GetPosition()
                # KiCad 9 PCB_VIA.GetWidth() needs a layer arg (per-layer
                # widths); a through via is uniform so F.Cu is fine.
                try:
                    via_w = t.GetWidth(pcbnew.F_Cu)
                except TypeError:
                    via_w = t.GetWidth()
                # Inflate the via radius by trace half-width + clearance so
                # the swept trace stadium is what's tested (not just the
                # centerline). With trace_half=0 + min_clearance_iu=0 this
                # reduces to the legacy radius-only check.
                threshold = via_w / 2.0 + trace_half + min_clearance_iu
                if seg_pt_dist(pos.x, pos.y) < threshold:
                    yield ("via", t)
            else:
                if t.GetLayer() != layer_id:
                    continue
                ts, te = t.GetStart(), t.GetEnd()
                if trace_half == 0 and min_clearance_iu == 0:
                    # Legacy centerline cross detection; preserves existing
                    # callers (find_via_lane diagnostic strings, etc.).
                    if segs_cross(ts.x, ts.y, te.x, te.y):
                        yield ("track", t)
                else:
                    other_half = t.GetWidth() / 2.0
                    threshold = trace_half + other_half + min_clearance_iu
                    if seg_seg_min_dist(
                        sx, sy, ex, ey, ts.x, ts.y, te.x, te.y
                    ) < threshold:
                        yield ("track", t)

        # Pads — sample the segment through the pad's real shape. With a
        # non-zero accuracy, pcbnew's PAD.HitTest returns True when the
        # pad's shape comes within `accuracy` iu of the sample point;
        # combined with centerline sampling, that detects edge-clipping
        # without polygon-vs-stadium geometry.
        accuracy = trace_half + min_clearance_iu
        steps = max(2, int(seg_len / 100000))  # ~0.1mm sampling
        for fp in self.board.GetFootprints():
            for pad in fp.Pads():
                if pad.GetNetname() == net_name:
                    continue
                if not pad.IsOnLayer(layer_id):
                    continue
                pc = pad.GetPosition()
                bb = pad.GetBoundingBox()
                half_diag = (bb.GetWidth() ** 2 + bb.GetHeight() ** 2) ** 0.5 / 2.0
                # Inflate quick-reject so an off-centerline pad still
                # makes it to the precise HitTest pass.
                if seg_pt_dist(pc.x, pc.y) > half_diag + accuracy:
                    continue
                for i in range(steps + 1):
                    f = i / steps
                    px = int(sx + f * (ex - sx))
                    py = int(sy + f * (ey - sy))
                    try:
                        inside = pad.HitTest(pcbnew.VECTOR2I(px, py), accuracy)
                    except (TypeError, Exception):
                        try:
                            inside = pad.HitTest(pcbnew.VECTOR2I(px, py))
                        except Exception:
                            inside = bb.Contains(pcbnew.VECTOR2I(px, py))
                    if inside:
                        yield ("pad", pad, fp.GetReference())
                        break

    def _find_route_obstacles(
        self,
        start: pcbnew.VECTOR2I,
        end: pcbnew.VECTOR2I,
        layer_id: int,
        net_name: str,
        trace_width_iu: int = 0,
        min_clearance_iu: int = 0,
    ) -> list:
        """Return human-readable descriptions of foreign-net copper that a
        straight segment start->end on layer_id would collide with.

        Checks tracks (same-layer segment intersection), vias (all layers,
        centre within via radius of the segment) and pads (segment sampled
        through the pad's real shape via HitTest). With non-zero
        `trace_width_iu` and/or `min_clearance_iu`, the check accounts
        for the trace's swept width + clearance margin instead of only the
        centerline (#177). Empty list = clear path.
        """
        out: list = []
        for item in self._iter_route_obstacles(
            start, end, layer_id, net_name, trace_width_iu, min_clearance_iu
        ):
            kind = item[0]
            if kind == "via":
                t = item[1]
                pos = t.GetPosition()
                out.append(
                    f"via on net '{t.GetNetname() or '<no net>'}' "
                    f"at ({pos.x / 1e6:.2f},{pos.y / 1e6:.2f})"
                )
            elif kind == "track":
                t = item[1]
                ts = t.GetStart()
                out.append(
                    f"track on net '{t.GetNetname() or '<no net>'}' "
                    f"crossing near ({ts.x / 1e6:.2f},{ts.y / 1e6:.2f})"
                )
            elif kind == "pad":
                pad, fp_ref = item[1], item[2]
                pc = pad.GetPosition()
                out.append(
                    f"pad {fp_ref}-{pad.GetNumber()} on net "
                    f"'{pad.GetNetname() or '<no net>'}' "
                    f"at ({pc.x / 1e6:.2f},{pc.y / 1e6:.2f})"
                )
        return out

    def _via_clearance_violations(
        self,
        pos: pcbnew.VECTOR2I,
        via_diameter_iu: int,
        net_name: str,
        min_clearance_iu: int,
    ) -> list:
        """For a proposed THROUGH via centered at `pos` with the given
        diameter, return human-readable descriptions of all foreign-net
        copper that comes within (via_radius + min_clearance) of the via's
        edge. Through vias touch every copper layer, so this checks all
        copper layers; track segments are checked on their own layer only.

        Used by find_via_lane to validate via1/via2 before committing —
        the segment-clearance check in _find_safe_via_point doesn't catch
        the case where the via itself (0.6 mm default) overlaps adjacent
        pads/vias/tracks even though the *segment* approaching the via
        point is clear. Returns empty list when the via placement passes.
        """
        via_radius_iu = via_diameter_iu // 2
        px, py = pos.x, pos.y
        out: list = []

        for t in self.board.Tracks():
            if t.GetNetname() == net_name:
                continue
            if t.Type() == pcbnew.PCB_VIA_T:
                tp = t.GetPosition()
                try:
                    tw = t.GetWidth(pcbnew.F_Cu)
                except TypeError:
                    tw = t.GetWidth()
                center_dist = ((tp.x - px) ** 2 + (tp.y - py) ** 2) ** 0.5
                gap = center_dist - via_radius_iu - tw / 2.0
                if gap < min_clearance_iu:
                    out.append(
                        f"via on net '{t.GetNetname() or '<no net>'}' "
                        f"at ({tp.x / 1e6:.2f},{tp.y / 1e6:.2f}) "
                        f"({gap / 1e6:.3f} mm gap, "
                        f"need ≥{min_clearance_iu / 1e6:.3f} mm)"
                    )
            else:
                if not pcbnew.IsCopperLayer(t.GetLayer()):
                    continue
                ts, te = t.GetStart(), t.GetEnd()
                # Point-to-segment distance (clamped)
                vx, vy = te.x - ts.x, te.y - ts.y
                seg_len_sq = vx * vx + vy * vy
                if seg_len_sq == 0:
                    sd = ((px - ts.x) ** 2 + (py - ts.y) ** 2) ** 0.5
                else:
                    tparam = max(0.0, min(
                        1.0,
                        ((px - ts.x) * vx + (py - ts.y) * vy) / seg_len_sq,
                    ))
                    qx = ts.x + tparam * vx
                    qy = ts.y + tparam * vy
                    sd = ((px - qx) ** 2 + (py - qy) ** 2) ** 0.5
                tw = t.GetWidth()
                gap = sd - via_radius_iu - tw / 2.0
                if gap < min_clearance_iu:
                    out.append(
                        f"track on net '{t.GetNetname() or '<no net>'}' "
                        f"on {self.board.GetLayerName(t.GetLayer())} "
                        f"near ({ts.x / 1e6:.2f},{ts.y / 1e6:.2f}) "
                        f"({gap / 1e6:.3f} mm gap)"
                    )

        # Pads — distance to pad bbox (conservative for non-rect shapes)
        for fp in self.board.GetFootprints():
            for pad in fp.Pads():
                if pad.GetNetname() == net_name:
                    continue
                has_cu = any(
                    pcbnew.IsCopperLayer(lid)
                    and self.board.IsLayerEnabled(lid)
                    for lid in pad.GetLayerSet().Seq()
                )
                if not has_cu:
                    continue
                bb = pad.GetBoundingBox()
                tl = bb.GetOrigin()
                br = bb.GetEnd()
                dx = max(tl.x - px, 0, px - br.x)
                dy = max(tl.y - py, 0, py - br.y)
                bbox_dist = (dx * dx + dy * dy) ** 0.5
                gap = bbox_dist - via_radius_iu
                if gap < min_clearance_iu:
                    pp = pad.GetPosition()
                    out.append(
                        f"pad {fp.GetReference()}-{pad.GetNumber()} on net "
                        f"'{pad.GetNetname() or '<no net>'}' "
                        f"at ({pp.x / 1e6:.2f},{pp.y / 1e6:.2f}) "
                        f"({gap / 1e6:.3f} mm gap, "
                        f"need ≥{min_clearance_iu / 1e6:.3f} mm)"
                    )

        return out

    def _obstacle_union_bbox(self, items) -> Optional[tuple]:
        """Union bounding box of obstacle copper extents (IU, axis-aligned).

        items: iterable of tuples from `_iter_route_obstacles`. Returns
        (xmin, ymin, xmax, ymax) or None if items is empty. Track widths
        and via diameters are factored in; pads use their real bbox.
        Used by find_via_lane Strategy F to size detour L-shapes.
        """
        xs, ys = [], []
        for item in items:
            kind = item[0]
            if kind == "track":
                t = item[1]
                s, e = t.GetStart(), t.GetEnd()
                w = t.GetWidth()
                xs.extend([s.x - w / 2, s.x + w / 2, e.x - w / 2, e.x + w / 2])
                ys.extend([s.y - w / 2, s.y + w / 2, e.y - w / 2, e.y + w / 2])
            elif kind == "via":
                t = item[1]
                p = t.GetPosition()
                try:
                    w = t.GetWidth(pcbnew.F_Cu)
                except TypeError:
                    w = t.GetWidth()
                xs.extend([p.x - w / 2, p.x + w / 2])
                ys.extend([p.y - w / 2, p.y + w / 2])
            elif kind == "pad":
                pad = item[1]
                bb = pad.GetBoundingBox()
                tl, br = bb.GetOrigin(), bb.GetEnd()
                xs.extend([tl.x, br.x])
                ys.extend([tl.y, br.y])
        if not xs:
            return None
        return (min(xs), min(ys), max(xs), max(ys))
