"""
Freerouting autoroute integration for KiCAD MCP Server.

Exports the board to Specctra DSN format, runs Freerouting CLI,
and imports the routed SES file back into the board.

Supports two execution modes:
  - Direct: java -jar freerouting.jar (requires Java 21+)
  - Docker: docker run eclipse-temurin:21-jre (requires Docker)
"""

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("kicad_interface")

# Default Freerouting JAR location
DEFAULT_FREEROUTING_JAR = os.environ.get(
    "FREEROUTING_JAR",
    os.path.join(os.path.expanduser("~"), ".kicad-mcp", "freerouting.jar"),
)

DOCKER_IMAGE = "eclipse-temurin:21-jre"


def _find_java() -> Optional[str]:
    """Find java executable on the system."""
    java = shutil.which("java")
    if java:
        return java
    for candidate in [
        "/usr/bin/java",
        "/usr/local/bin/java",
        os.path.expandvars("$JAVA_HOME/bin/java"),
    ]:
        if os.path.isfile(candidate):
            return candidate
    return None


def _find_docker() -> Optional[str]:
    """Find docker executable on the system."""
    return shutil.which("docker") or shutil.which("podman")


def _docker_available() -> bool:
    """Check if Docker/Podman is available and running."""
    docker = _find_docker()
    if not docker:
        return False
    try:
        proc = subprocess.run(
            [docker, "info"],
            capture_output=True,
            timeout=10,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _java_version_ok(java_exe: str) -> bool:
    """Check if local Java is version 21+."""
    try:
        proc = subprocess.run(
            [java_exe, "-version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        output = proc.stderr or proc.stdout
        # Parse version like: openjdk version "17.0.18"
        for line in output.split("\n"):
            if "version" in line:
                ver = line.split('"')[1] if '"' in line else ""
                major = int(ver.split(".")[0])
                return major >= 21
    except Exception:
        pass
    return False


# GND-last default for 4-layer boards. Image-current return on a signal
# trace travels on the nearest plane; cutting GND on In1.Cu detours the
# return and forms an EMI loop, so prefer outer layers, then PWR
# (In2.Cu), and use GND last. Assumes the [[feedback-pcb-stackup]]
# convention (GND@L2 = In1.Cu, PWR@L3 = In2.Cu). Callers can override
# with an explicit `layerOrder` param.
_DEFAULT_4LAYER_ORDER = ["F.Cu", "B.Cu", "In2.Cu", "In1.Cu"]


def _resolve_layer_order(board: Any, requested: Optional[List[str]]) -> Optional[List[str]]:
    """Return the effective layer routing order, or None for "leave the
    DSN alone". Requested order takes precedence; otherwise we apply the
    GND-last default for 4-layer boards and no reorder for 2-layer.
    """
    if requested:
        # Caller may give the order as ["F.Cu", "B.Cu", ...]; we trust it.
        return list(requested)
    try:
        n_cu = board.GetCopperLayerCount()
    except Exception:
        n_cu = None
    if n_cu == 4:
        return list(_DEFAULT_4LAYER_ORDER)
    return None


def _rewrite_dsn_layer_order(dsn_text: str, desired_order: List[str]) -> str:
    """Rewrite the DSN structure-block layer list to follow `desired_order`.

    The DSN exporter emits one block per copper layer of the form::

        (layer F.Cu
          (type signal)
          (property
            (index 0)
          )
        )

    Freerouting iterates layers in the order they appear here (and
    prefers earlier indices), so reordering changes its layer-cost bias.
    We require `desired_order` to be a permutation of the layers present
    in the DSN — anything else is a caller bug.
    """
    import re

    block_re = re.compile(
        r"    \(layer (\S+)\n"
        r"      \(type signal\)\n"
        r"      \(property\n"
        r"        \(index \d+\)\n"
        r"      \)\n"
        r"    \)",
        re.MULTILINE,
    )
    matches = list(block_re.finditer(dsn_text))
    if not matches:
        raise ValueError("no (layer ...) blocks found in DSN structure")
    found = [m.group(1) for m in matches]
    if set(desired_order) != set(found):
        raise ValueError(
            f"layerOrder {desired_order!r} must be a permutation of "
            f"DSN layers {found!r}"
        )

    new_blocks = []
    for i, name in enumerate(desired_order):
        new_blocks.append(
            f"    (layer {name}\n"
            f"      (type signal)\n"
            f"      (property\n"
            f"        (index {i})\n"
            f"      )\n"
            f"    )"
        )
    new_block_text = "\n".join(new_blocks)
    first = matches[0].start()
    last = matches[-1].end()
    return dsn_text[:first] + new_block_text + dsn_text[last:]


def _maybe_apply_layer_order(
    board: Any, dsn_path: str, requested: Optional[List[str]]
) -> Optional[List[str]]:
    """If a layer order applies, rewrite the DSN in place. Returns the
    order that was applied, or None if no reorder happened."""
    order = _resolve_layer_order(board, requested)
    if not order:
        return None
    try:
        with open(dsn_path, "r") as f:
            txt = f.read()
        new_txt = _rewrite_dsn_layer_order(txt, order)
        if new_txt != txt:
            with open(dsn_path, "w") as f:
                f.write(new_txt)
        return order
    except ValueError as e:
        logger.warning(f"Skipping DSN layer reorder: {e}")
        return None


_OUTER_COPPER_LAYERS = {"F.Cu", "B.Cu"}


def _rewrite_dsn_plane_layer_types(dsn_text: str) -> Tuple[str, List[str]]:
    """Mark every *inner* copper layer that hosts a (plane …) declaration
    as ``(type power)`` instead of ``(type signal)``.

    pcbnew's `ExportSpecctraDSN` always emits ``(type signal)`` for
    every copper layer, even when the layer is a continuous GND / PWR
    pour declared via ``(plane NET (polygon LAYER …))``. Freerouting
    treats signal layers as routable, so long-distance nets often get
    routed straight through the pour layer — carving up the plane and
    wrecking the intended low-impedance return paths.

    This post-process scans the DSN for plane declarations, identifies
    which layers carry a plane, and flips those layers' ``type signal``
    line to ``type power``. Freerouting then leaves those layers alone.

    **Outer layers (F.Cu / B.Cu) are never flipped**, even when they
    host a GND pour: they remain the board's primary routing layers
    (signals run *around* the pour). Flipping them caused a board-wipe
    incident (#240/#241) — when every copper layer was marked power,
    freerouting had no routable layer, produced an empty SES, and the
    replace-style import wiped all routing. Only dedicated inner planes
    (In1.Cu, In2.Cu, …) should become ``(type power)``.

    Returns (rewritten_text, list_of_flipped_layer_names).
    """
    import re

    plane_re = re.compile(
        r"\(plane\s+\S+\s*\(polygon\s+(\S+)\s",
        re.MULTILINE,
    )
    plane_layers = sorted(
        l for l in {m.group(1) for m in plane_re.finditer(dsn_text)}
        if l not in _OUTER_COPPER_LAYERS
    )
    if not plane_layers:
        return dsn_text, []

    new_text = dsn_text
    for lname in plane_layers:
        # Match e.g. "    (layer In1.Cu\n      (type signal)" — replace
        # only the matching layer block's type line.
        block_re = re.compile(
            r"(\(layer " + re.escape(lname) + r"\n\s+\(type )signal(\))",
            re.MULTILINE,
        )
        new_text = block_re.sub(r"\1power\2", new_text)
    return new_text, plane_layers


def _maybe_apply_plane_layer_types(dsn_path: str) -> List[str]:
    """Rewrite plane layers in the DSN to ``(type power)`` so freerouting
    leaves them alone. Returns the list of plane layers that were
    flipped (empty if no planes were detected)."""
    try:
        with open(dsn_path, "r") as f:
            txt = f.read()
        new_txt, layers = _rewrite_dsn_plane_layer_types(txt)
        if layers and new_txt != txt:
            with open(dsn_path, "w") as f:
                f.write(new_txt)
        return layers
    except Exception as e:
        logger.warning(f"Skipping DSN plane-layer rewrite: {e}")
        return []


def _rewrite_dsn_fix_wiring(dsn_text: str) -> Tuple[str, int]:
    """Mark every already-routed wire/via in the DSN as ``(type fix)``
    instead of ``(type route)`` so freerouting treats them as immovable
    and only fills in the open ratsnest.

    pcbnew's ``ExportSpecctraDSN`` emits every existing track and via in
    the ``(wiring …)`` section as ``(type route)`` — fair game for the
    autorouter to rip up and reroute. For an *incremental* pass (route a
    newly-placed sub-circuit, leave the rest of the board alone) that's
    the opposite of what we want: freerouting's optimiser can rework
    hand-routed nets.

    Flipping the existing wiring to ``fix`` tells freerouting to preserve
    it. NOTE the SES import is replace-like (#241): the board ends up
    with whatever the SES contains, so this feature only yields a usable
    board if freerouting echoes the fixed wires back into the session
    output. If it does not, the import guard (``_ses_import_guard``)
    refuses the import rather than wiping the existing routing — so the
    worst case is a safe abort, never a board wipe.

    ``(type route)`` only ever appears on wires/vias (copper layers are
    ``signal``/``power``, clearances are ``smd_smd``/…), so the token
    replace is unambiguous. Returns (rewritten_text, count_flipped).
    """
    count = dsn_text.count("(type route)")
    if not count:
        return dsn_text, 0
    return dsn_text.replace("(type route)", "(type fix)"), count


def _maybe_fix_existing_wiring(dsn_path: str, enabled: bool) -> int:
    """When ``enabled``, rewrite existing wiring to ``(type fix)`` in the
    DSN so freerouting preserves it and only routes the open ratsnest.
    Returns the number of wires/vias flipped (0 when disabled or none
    present)."""
    if not enabled:
        return 0
    try:
        with open(dsn_path, "r") as f:
            txt = f.read()
        new_txt, count = _rewrite_dsn_fix_wiring(txt)
        if count and new_txt != txt:
            with open(dsn_path, "w") as f:
                f.write(new_txt)
        return count
    except Exception as e:
        logger.warning(f"Skipping DSN fix-existing-wiring rewrite: {e}")
        return 0


_SES_MIN_RETENTION = 0.5
_SES_GUARD_FLOOR = 20


def _count_ses_wires(ses_path: str) -> int:
    """Count routed wires in a Specctra SES file. 0 means freerouting
    produced no routing — a failure indicator. Returns -1 if the file
    can't be read (caller should not block on a read error)."""
    try:
        with open(ses_path, "r") as f:
            return f.read().count("(wire ")
    except Exception:
        return -1


def _ses_import_guard(
    ses_wires: int, board_tracks_before: int, force_import: bool,
) -> Optional[Dict[str, Any]]:
    """Decide whether importing an SES is safe (#241).

    ``pcbnew.ImportSpecctraSES`` is replace-like: the board ends up with
    whatever the session contains. A degenerate session (no routes, or
    far fewer than the board already has) therefore *destroys* existing
    routing on import — the #240 incident wiped 599 traces when every
    copper layer got marked ``(type power)`` and freerouting returned an
    empty SES.

    Returns an abort response dict when the import should be refused, or
    None when it's safe to proceed. ``force_import`` bypasses the guard.
    A read error (ses_wires < 0) does NOT block — that's a separate
    failure surfaced elsewhere.
    """
    if force_import or ses_wires < 0:
        return None
    if ses_wires == 0:
        return {
            "success": False,
            "message": "Aborted SES import: session contains 0 routes",
            "errorDetails": (
                "ImportSpecctraSES is replace-like, so importing an empty "
                "session would wipe all board routing. Freerouting likely "
                "had no routable layer (check planeLayersFlippedToPower — "
                "if every copper layer is (type power) there's nothing to "
                "route on) or otherwise failed. The board was left "
                "untouched. Pass forceImport=true only if you intend to "
                "clear the routing."
            ),
        }
    if (
        board_tracks_before >= _SES_GUARD_FLOOR
        and ses_wires < board_tracks_before * _SES_MIN_RETENTION
    ):
        return {
            "success": False,
            "message": (
                f"Aborted SES import: session has far fewer routes "
                f"({ses_wires}) than the board already has "
                f"({board_tracks_before})"
            ),
            "errorDetails": (
                "The replace-like import would discard most existing "
                "routing. This usually means freerouting failed to route, "
                "or (with preserveExistingTraces) it did not echo the "
                "fixed wires back into the session. Board left untouched. "
                "Pass forceImport=true to override."
            ),
        }
    return None


def _build_freerouting_cmd(
    jar_path: str,
    dsn_path: str,
    ses_path: str,
    passes: int,
    use_docker: bool,
) -> List[str]:
    """Build the command to run Freerouting."""
    if use_docker:
        docker_exe = _find_docker()
        if docker_exe is None:
            raise RuntimeError("Docker/Podman executable not found")
        board_dir = os.path.dirname(dsn_path)
        dsn_name = os.path.basename(dsn_path)
        ses_name = os.path.basename(ses_path)
        jar_name = os.path.basename(jar_path)
        return [
            docker_exe,
            "run",
            "--rm",
            "-v",
            f"{jar_path}:/app/{jar_name}:ro",
            "-v",
            f"{board_dir}:/work",
            DOCKER_IMAGE,
            "java",
            "-jar",
            f"/app/{jar_name}",
            "-de",
            f"/work/{dsn_name}",
            "-do",
            f"/work/{ses_name}",
            "-mp",
            str(passes),
        ]
    else:
        java_exe = _find_java()
        if java_exe is None:
            raise RuntimeError("Java executable not found")
        return [
            java_exe,
            "-jar",
            jar_path,
            "-de",
            dsn_path,
            "-do",
            ses_path,
            "-mp",
            str(passes),
        ]


class FreeroutingCommands:
    """Handles Freerouting autoroute operations."""

    def __init__(self, board: Any = None) -> None:
        self.board = board

    def _resolve_execution_mode(self, jar_path: str) -> Dict[str, Any]:
        """Determine how to run Freerouting: direct or docker.

        Returns dict with 'mode', 'use_docker', or 'error'.
        """
        java_exe = _find_java()
        if java_exe and _java_version_ok(java_exe):
            return {"mode": "direct", "use_docker": False}

        if _docker_available():
            return {"mode": "docker", "use_docker": True}

        if java_exe:
            return {
                "mode": "error",
                "error": (
                    f"Java found at {java_exe} but version < 21. "
                    "Freerouting 2.x requires Java 21+. "
                    "Install Java 21+ or Docker."
                ),
            }
        return {
            "mode": "error",
            "error": (
                "Neither Java 21+ nor Docker found. " "Install one of them to use Freerouting."
            ),
        }

    def autoroute(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Run Freerouting autorouter on the current board.

        Flow:
        1. Export board to Specctra DSN
        2. Run Freerouting CLI on DSN -> SES
        3. Import SES back into the board
        4. Save the board
        """
        try:
            import pcbnew
        except ImportError:
            return {
                "success": False,
                "message": "pcbnew not available",
                "errorDetails": "KiCAD Python API is required",
            }

        if not self.board:
            return {
                "success": False,
                "message": "No board is loaded",
                "errorDetails": "Load or create a board first",
            }

        board_path = params.get("boardPath")
        if not board_path:
            board_path = self.board.GetFileName()

        if not board_path:
            return {
                "success": False,
                "message": "No board file path available",
                "errorDetails": ("Provide boardPath or open a project first"),
            }

        jar_path = params.get("freeroutingJar", DEFAULT_FREEROUTING_JAR)
        timeout = params.get("timeout", 300)
        passes = params.get("maxPasses", 20)

        # Validate Freerouting JAR
        if not os.path.isfile(jar_path):
            return {
                "success": False,
                "message": "Freerouting JAR not found",
                "errorDetails": (
                    f"Expected at: {jar_path}. Download from "
                    "https://github.com/freerouting/freerouting/"
                    "releases or set FREEROUTING_JAR env var."
                ),
            }

        # Determine execution mode
        exec_mode = self._resolve_execution_mode(jar_path)
        if exec_mode["mode"] == "error":
            return {
                "success": False,
                "message": "No suitable Java runtime",
                "errorDetails": exec_mode["error"],
            }

        use_docker = exec_mode["use_docker"]

        # Set up file paths
        board_dir = os.path.dirname(board_path)
        board_stem = Path(board_path).stem
        dsn_path = os.path.join(board_dir, f"{board_stem}.dsn")
        ses_path = os.path.join(board_dir, f"{board_stem}.ses")

        # Step 0: Pre-flight — verify netclass patterns haven't drifted
        # since the project was last sanity-checked. KiCAD GUI can
        # silently strip patterns on save; if that happened, the
        # DSN's (class POWER_4A …) will be missing nets and freerouting
        # will route them at Default width. Warn loudly but proceed —
        # the user may have an in-progress refactor.
        netclass_drift: Optional[Dict[str, Any]] = None
        try:
            from commands.netclass_patterns import verify_netclass_patterns

            pro_path = Path(board_path).with_suffix(".kicad_pro")
            sch_path = Path(board_path).with_suffix(".kicad_sch")
            if not sch_path.exists():
                sch_path = None
            if pro_path.exists():
                netclass_drift = verify_netclass_patterns(
                    pro_path, restore=False, sch_path=sch_path,
                )
                if netclass_drift.get("drifted"):
                    logger.warning(
                        "Netclass-pattern drift detected before autoroute: "
                        f"missing={netclass_drift.get('missing')} "
                        f"extra={netclass_drift.get('extra')}. Run "
                        f"verify_netclass_patterns with restore=true to "
                        f"fix, or accept the drift if intentional."
                    )
        except Exception as e:
            logger.debug(f"Netclass-pattern pre-flight failed (non-fatal): {e}")

        # Step 1: Export DSN
        logger.info(f"Exporting DSN to {dsn_path}")
        try:
            result = pcbnew.ExportSpecctraDSN(self.board, dsn_path)
            if result is not True and result != 0:
                return {
                    "success": False,
                    "message": "DSN export failed",
                    "errorDetails": (f"ExportSpecctraDSN returned: {result}"),
                }
        except Exception as e:
            return {
                "success": False,
                "message": "DSN export failed",
                "errorDetails": str(e),
            }

        if not os.path.isfile(dsn_path):
            return {
                "success": False,
                "message": "DSN file was not created",
                "errorDetails": f"Expected at: {dsn_path}",
            }

        dsn_size = os.path.getsize(dsn_path)
        logger.info(f"DSN exported: {dsn_size} bytes")

        # Step 1b: Reorder DSN layers so freerouting tries them in the
        # caller's preferred order (default: GND-last on 4-layer boards
        # so the closest-plane signal-return path stays intact).
        layer_order = params.get("layerOrder")
        applied_layer_order = _maybe_apply_layer_order(
            self.board, dsn_path, layer_order
        )
        if applied_layer_order:
            logger.info(f"DSN layer order set to: {applied_layer_order}")

        # Step 1c: Flip plane layers (those hosting a `(plane …)`
        # declaration) from `(type signal)` to `(type power)` so
        # freerouting leaves them alone.  pcbnew's DSN exporter marks
        # every copper layer as signal even when there's a continuous
        # pour on it; without this step long-distance signal nets get
        # routed straight through the plane, carving up the pour and
        # ruining the return-current path.
        plane_layers = _maybe_apply_plane_layer_types(dsn_path)
        if plane_layers:
            logger.info(
                f"DSN plane layers marked (type power): {plane_layers}"
            )

        # Step 1d: For an incremental pass, mark all already-routed
        # wiring as (type fix) so freerouting preserves it bit-identically
        # and only fills the open ratsnest — keeps hand-routed copper out
        # of the optimiser's reach (#240).
        preserve_existing = bool(params.get("preserveExistingTraces", False))
        existing_fixed = _maybe_fix_existing_wiring(dsn_path, preserve_existing)
        if existing_fixed:
            logger.info(
                f"Marked {existing_fixed} existing wire(s)/via(s) as "
                f"(type fix) — incremental route preserves existing copper"
            )

        # Step 2: Run Freerouting
        cmd = _build_freerouting_cmd(jar_path, dsn_path, ses_path, passes, use_docker)

        mode_label = "docker" if use_docker else "direct"
        logger.info(f"Running Freerouting ({mode_label}): {' '.join(cmd)}")
        start_time = time.time()

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=board_dir,
            )
            elapsed = round(time.time() - start_time, 1)

            if proc.returncode != 0:
                return {
                    "success": False,
                    "message": (f"Freerouting exited with code " f"{proc.returncode}"),
                    "errorDetails": proc.stderr or proc.stdout,
                    "elapsed_seconds": elapsed,
                    "mode": mode_label,
                }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "message": (f"Freerouting timed out after {timeout}s"),
                "errorDetails": ("Increase timeout or reduce board complexity"),
            }
        except Exception as e:
            return {
                "success": False,
                "message": "Failed to run Freerouting",
                "errorDetails": str(e),
            }

        # Check SES output
        if not os.path.isfile(ses_path):
            return {
                "success": False,
                "message": "Freerouting did not produce SES output",
                "errorDetails": (f"Expected at: {ses_path}. " f"Stdout: {proc.stdout[:500]}"),
                "elapsed_seconds": elapsed,
            }

        ses_size = os.path.getsize(ses_path)
        logger.info(f"SES produced: {ses_size} bytes in {elapsed}s")

        # Step 2b: Import safety guard (#241). The SES import is
        # replace-like; a degenerate session would wipe existing routing.
        ses_wires = _count_ses_wires(ses_path)
        tracks_before = sum(
            1 for t in self.board.GetTracks() if t.GetClass() != "PCB_VIA"
        )
        guard = _ses_import_guard(
            ses_wires, tracks_before, bool(params.get("forceImport", False))
        )
        if guard is not None:
            logger.warning(guard["message"])
            guard.update({
                "elapsed_seconds": elapsed,
                "sesWireCount": ses_wires,
                "boardTracksBefore": tracks_before,
                "planeLayersFlippedToPower": plane_layers,
                "existingTracesFixed": existing_fixed,
                "dsn_path": dsn_path,
                "ses_path": ses_path,
            })
            return guard

        # Step 3: Import SES
        logger.info(f"Importing SES from {ses_path}")
        try:
            result = pcbnew.ImportSpecctraSES(self.board, ses_path)
            if result is not True and result != 0:
                return {
                    "success": False,
                    "message": "SES import failed",
                    "errorDetails": (f"ImportSpecctraSES returned: {result}"),
                    "elapsed_seconds": elapsed,
                }
        except Exception as e:
            return {
                "success": False,
                "message": "SES import failed",
                "errorDetails": str(e),
                "elapsed_seconds": elapsed,
            }

        # Step 3b: Auto-dedupe (#227). pcbnew.ImportSpecctraSES APPENDS
        # tracks rather than replacing — so calling autoroute a second
        # time (or autoroute after an earlier import_ses) leaves exact
        # duplicate tracks on every routed net. Two tracks only match
        # when (layer, width, net) AND endpoints coincide, so dropping
        # one of each duplicate pair preserves connectivity; user
        # pre-routes survive untouched. Pass autoDedupe=false to skip.
        dedupe_removed = 0
        if params.get("autoDedupe", True):
            try:
                from commands.routing import RoutingCommands
                rc = RoutingCommands(board=self.board)
                dr = rc.dedupe_traces({"apply": True, "includeVias": True})
                if dr.get("success"):
                    dedupe_removed = dr.get("removedCount", 0)
                    if dedupe_removed:
                        logger.info(
                            f"autoroute auto-dedupe removed "
                            f"{dedupe_removed} duplicate item(s)"
                        )
            except Exception as e:
                logger.warning(f"autoroute auto-dedupe failed: {e}")

        # Step 4: Save board
        try:
            self.board.Save(board_path)
        except Exception as e:
            logger.warning(f"Board save after autoroute failed: {e}")

        # Collect stats
        tracks = self.board.GetTracks()
        track_count = 0
        via_count = 0
        for t in tracks:
            if t.GetClass() == "PCB_VIA":
                via_count += 1
            else:
                track_count += 1

        return {
            "success": True,
            "message": f"Autoroute completed in {elapsed}s",
            "mode": mode_label,
            "dsn_path": dsn_path,
            "ses_path": ses_path,
            "elapsed_seconds": elapsed,
            "layerOrder": applied_layer_order,
            "planeLayersFlippedToPower": plane_layers,
            "existingTracesFixed": existing_fixed,
            "netclassPatternDrift": netclass_drift,
            "autoDedupeRemovedCount": dedupe_removed,
            "board_stats": {
                "tracks": track_count,
                "vias": via_count,
            },
            "freerouting_stdout": (proc.stdout[:1000] if proc.stdout else ""),
        }

    def export_dsn(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Export the board to Specctra DSN format only."""
        try:
            import pcbnew
        except ImportError:
            return {
                "success": False,
                "message": "pcbnew not available",
                "errorDetails": "KiCAD Python API is required",
            }

        if not self.board:
            return {
                "success": False,
                "message": "No board is loaded",
                "errorDetails": "Load or create a board first",
            }

        board_path = params.get("boardPath") or self.board.GetFileName()
        output_path = params.get("outputPath")

        if not output_path:
            if board_path:
                output_path = os.path.splitext(board_path)[0] + ".dsn"
            else:
                return {
                    "success": False,
                    "message": "No output path",
                    "errorDetails": ("Provide outputPath or have a board open"),
                }

        try:
            result = pcbnew.ExportSpecctraDSN(self.board, output_path)
            if result is not True and result != 0:
                return {
                    "success": False,
                    "message": "DSN export failed",
                    "errorDetails": (f"ExportSpecctraDSN returned: {result}"),
                }
        except Exception as e:
            return {
                "success": False,
                "message": "DSN export failed",
                "errorDetails": str(e),
            }

        # Apply caller-requested (or default 4-layer) layer routing order
        layer_order = params.get("layerOrder")
        applied_layer_order = _maybe_apply_layer_order(
            self.board, output_path, layer_order
        )
        # Flip plane layers to (type power) so freerouting won't route
        # signals across them — see _rewrite_dsn_plane_layer_types.
        plane_layers = _maybe_apply_plane_layer_types(output_path)

        # Mark existing wiring (type fix) for an incremental route (#240).
        preserve_existing = bool(params.get("preserveExistingTraces", False))
        existing_fixed = _maybe_fix_existing_wiring(output_path, preserve_existing)

        file_size = os.path.getsize(output_path) if os.path.isfile(output_path) else 0
        return {
            "success": True,
            "message": f"Exported DSN to {output_path}",
            "path": output_path,
            "size_bytes": file_size,
            "layerOrder": applied_layer_order,
            "planeLayersFlippedToPower": plane_layers,
            "existingTracesFixed": existing_fixed,
        }

    def import_ses(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Import a Specctra SES file into the board."""
        try:
            import pcbnew
        except ImportError:
            return {
                "success": False,
                "message": "pcbnew not available",
                "errorDetails": "KiCAD Python API is required",
            }

        if not self.board:
            return {
                "success": False,
                "message": "No board is loaded",
                "errorDetails": "Load or create a board first",
            }

        ses_path = params.get("sesPath")
        if not ses_path:
            return {
                "success": False,
                "message": "Missing sesPath parameter",
                "errorDetails": ("Provide the path to the .ses file"),
            }

        if not os.path.isfile(ses_path):
            return {
                "success": False,
                "message": "SES file not found",
                "errorDetails": f"File not found: {ses_path}",
            }

        # Import safety guard (#241): refuse a degenerate session that
        # would wipe existing routing (the import is replace-like).
        ses_wires = _count_ses_wires(ses_path)
        tracks_before = sum(
            1 for t in self.board.GetTracks() if t.GetClass() != "PCB_VIA"
        )
        guard = _ses_import_guard(
            ses_wires, tracks_before, bool(params.get("forceImport", False))
        )
        if guard is not None:
            logger.warning(guard["message"])
            guard.update({
                "sesWireCount": ses_wires,
                "boardTracksBefore": tracks_before,
                "sesPath": ses_path,
            })
            return guard

        try:
            result = pcbnew.ImportSpecctraSES(self.board, ses_path)
            if result is not True and result != 0:
                return {
                    "success": False,
                    "message": "SES import failed",
                    "errorDetails": (f"ImportSpecctraSES returned: {result}"),
                }
        except Exception as e:
            return {
                "success": False,
                "message": "SES import failed",
                "errorDetails": str(e),
            }

        # Auto-dedupe (#227). ImportSpecctraSES APPENDS tracks; running
        # import twice doubles them all. Two tracks only match on
        # (layer, width, net) + endpoints, so dropping duplicates is
        # connectivity-preserving and leaves any user pre-routes
        # untouched. Pass autoDedupe=false to skip.
        dedupe_removed = 0
        if params.get("autoDedupe", True):
            try:
                from commands.routing import RoutingCommands
                rc = RoutingCommands(board=self.board)
                dr = rc.dedupe_traces({"apply": True, "includeVias": True})
                if dr.get("success"):
                    dedupe_removed = dr.get("removedCount", 0)
                    if dedupe_removed:
                        logger.info(
                            f"import_ses auto-dedupe removed "
                            f"{dedupe_removed} duplicate item(s)"
                        )
            except Exception as e:
                logger.warning(f"import_ses auto-dedupe failed: {e}")

        board_path = params.get("boardPath") or self.board.GetFileName()
        if board_path:
            try:
                self.board.Save(board_path)
            except Exception as e:
                logger.warning(f"Board save after SES import failed: {e}")

        tracks = self.board.GetTracks()
        track_count = sum(1 for t in tracks if t.GetClass() != "PCB_VIA")
        via_count = sum(1 for t in tracks if t.GetClass() == "PCB_VIA")

        return {
            "success": True,
            "message": f"Imported SES from {ses_path}",
            "autoDedupeRemovedCount": dedupe_removed,
            "board_stats": {
                "tracks": track_count,
                "vias": via_count,
            },
        }

    def check_freerouting(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Check if Freerouting and Java/Docker are available."""
        jar_path = params.get("freeroutingJar", DEFAULT_FREEROUTING_JAR)

        # Check local Java
        java_exe = _find_java()
        java_version = None
        java_21_ok = False
        if java_exe:
            try:
                proc = subprocess.run(
                    [java_exe, "-version"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                java_version = (proc.stderr or proc.stdout).strip().split("\n")[0]
                java_21_ok = _java_version_ok(java_exe)
            except Exception:
                pass

        # Check Docker/Podman
        docker_exe = _find_docker()
        has_docker = _docker_available()

        jar_exists = os.path.isfile(jar_path)
        ready = jar_exists and (java_21_ok or has_docker)

        mode = "none"
        if java_21_ok:
            mode = "direct"
        elif has_docker:
            mode = "docker"

        return {
            "success": True,
            "message": "Freerouting dependency check",
            "java": {
                "found": java_exe is not None,
                "path": java_exe,
                "version": java_version,
                "java_21_ok": java_21_ok,
            },
            "docker": {
                "available": has_docker,
                "path": docker_exe,
                "image": DOCKER_IMAGE,
            },
            "freerouting": {
                "jar_found": jar_exists,
                "jar_path": jar_path,
            },
            "execution_mode": mode,
            "ready": ready,
        }
