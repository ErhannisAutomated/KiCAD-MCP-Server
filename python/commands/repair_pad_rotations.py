"""repair_pad_rotations — restore pad orientations after a regex-style
footprint rewrite that bypassed pcbnew's SetOrientationDegrees().

Background incident (2026-05-14, power_module): the regex-based
``apply_positions.py`` recovery script rewrote ``(at X Y rot)`` lines
in the .kicad_pcb directly. The footprint's orientation header
updated, but each pad's GLOBAL rotation stayed at its pre-edit
value — pad CENTRES rotated correctly with the footprint, but pad
SHAPES were drawn at the old angle. 57 of 71 footprints affected.
Invisible to DRC/ratsnest (which use pad centres); caught only on
visual inspection. USB-C 16P pads stacked, HTSSOP-28 pad rows
pointed into the body.

This tool audits the board for the same symptom and (with
``dryRun=False``) applies the repair recipe the user worked out by
hand on power_module:

    for pad in fp.Pads():
        pad.SetOrientationDegrees(
            fp.GetOrientationDegrees() + reference_rel[pad_number]
        )

where ``reference_rel`` is the per-pad rotation offset relative to
the footprint frame, sourced from a clean reference instance of the
same lib_id (cross-instance majority vote). Preserves any
legitimate library per-pad rotation offset.

Safety:
  * Dry-run is the default. Audit first; mutate explicitly.
  * Cross-instance corroboration is required by default — we only
    repair footprints where another instance of the same lib_id
    confirms a clean rel-fingerprint. Single-instance suspects are
    listed under ``suspect_single_instance`` and require
    ``force=true`` (the user should verify the library footprint's
    rel-pattern before forcing).
  * ``refs`` filter scopes both audit and repair.

Companion to ``check_pcb_integrity`` (commands/integrity.py): that
tool flags the same condition for human review; this tool applies
the fix. Detection logic is intentionally close to
``check_pad_rotation`` so reports match across tools.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import pcbnew

logger = logging.getLogger(__name__)


# Tolerance for treating two angles as the same (degrees). Float
# rotations through SWIG accumulate sub-millidegree noise; library
# pad rotations are quantised to integer degrees in practice.
_ANGLE_TOL_DEG = 0.5


def _wrap_deg(a: float) -> float:
    """Normalise to [0, 360)."""
    a = a % 360.0
    if a < 0:
        a += 360.0
    return a


def _angle_close(a: float, b: float, tol: float = _ANGLE_TOL_DEG) -> bool:
    """True if two angles are within `tol` degrees, accounting for
    the 360° wrap (i.e., 0.1° and 359.9° are 0.2° apart)."""
    d = abs(_wrap_deg(a) - _wrap_deg(b))
    return d < tol or d > 360.0 - tol


def _rel_fingerprint(fp: Any) -> Dict[str, float]:
    """Per-pad relative orientation map (number → rel-degrees)."""
    fp_rot = fp.GetOrientation().AsDegrees()
    out: Dict[str, float] = {}
    for pad in fp.Pads():
        num = pad.GetNumber()
        if not num:
            continue  # mounting / silk pads — no number, skip
        rel = _wrap_deg(pad.GetOrientation().AsDegrees() - fp_rot)
        out[num] = rel
    return out


def _fingerprints_match(
    a: Dict[str, float], b: Dict[str, float], tol: float = _ANGLE_TOL_DEG
) -> bool:
    """True if both fingerprints agree on every shared pad number
    (within `tol`). Pad numbers present in only one map are ignored —
    a footprint may have a numberless thermal pad while another instance
    omits it; the rotation question only applies to numbered pads."""
    shared = set(a.keys()) & set(b.keys())
    if not shared:
        return False
    return all(_angle_close(a[k], b[k], tol) for k in shared)


def _is_uniform_rotation_drift(
    stale: Dict[str, float], ref: Dict[str, float], tol: float = _ANGLE_TOL_DEG
) -> Optional[float]:
    """If `stale`'s pad rels are `ref`'s pad rels uniformly shifted by
    a single delta, return that delta. Otherwise None. This is the
    canonical "rotation didn't propagate" symptom: every pad off by
    the same amount."""
    shared = sorted(set(stale.keys()) & set(ref.keys()))
    if not shared:
        return None
    deltas = [_wrap_deg(stale[k] - ref[k]) for k in shared]
    # All deltas equal modulo `tol`?
    d0 = deltas[0]
    for d in deltas[1:]:
        if not _angle_close(d, d0, tol):
            return None
    return d0


class RepairPadRotationsCommands:
    """MCP handler. Audit/repair pad rotations on the current board."""

    def __init__(self, board: Optional[Any] = None):
        self.board = board

    def repair_pad_rotations(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self.board:
            return {
                "success": False,
                "message": "No board is loaded",
                "errorDetails": "Load or create a board first",
            }

        dry_run = bool(params.get("dryRun", True))
        force = bool(params.get("force", False))
        refs_filter = set(params.get("refs") or [])
        tol = float(params.get("tolerance", _ANGLE_TOL_DEG))

        fps = list(self.board.GetFootprints())
        if refs_filter:
            fps = [fp for fp in fps if fp.GetReference() in refs_filter]

        # Group by lib_id and compute fingerprints once.
        by_lib: Dict[str, List[Any]] = {}
        fingerprints: Dict[str, Dict[str, float]] = {}
        for fp in fps:
            try:
                lib_id = fp.GetFPID().GetUniStringLibId()
            except Exception:
                lib_id = "<unknown>"
            by_lib.setdefault(lib_id, []).append(fp)
            fingerprints[fp.GetReference()] = _rel_fingerprint(fp)

        confirmed_stale: List[Dict[str, Any]] = []
        suspect_single: List[Dict[str, Any]] = []
        clean_count = 0

        for lib_id, instances in by_lib.items():
            # Majority-vote for the library reference fingerprint.
            # Bucket instances by fingerprint equivalence; largest
            # bucket wins. Ties broken by first-seen order, which
            # matches the user's mental model: the reference is the
            # first matching cluster.
            buckets: List[Tuple[Dict[str, float], List[Any]]] = []
            for fp in instances:
                fp_fp = fingerprints[fp.GetReference()]
                placed = False
                for bucket_fp, members in buckets:
                    if _fingerprints_match(bucket_fp, fp_fp, tol):
                        members.append(fp)
                        placed = True
                        break
                if not placed:
                    buckets.append((fp_fp, [fp]))

            if len(instances) < 2:
                # Single-instance — no peer to corroborate. Flag as
                # suspect IF the fingerprint has any non-zero rel
                # (uniform offset from fp frame). Cannot auto-repair
                # without library access; user must force.
                fp = instances[0]
                fp_fp = fingerprints[fp.GetReference()]
                if not fp_fp:
                    clean_count += 1
                    continue
                non_zero_rels = {n: r for n, r in fp_fp.items() if not _angle_close(r, 0.0, tol)}
                if non_zero_rels:
                    # Is the non-zero rel UNIFORM across pads? That's
                    # the stale signature.
                    rels = list(non_zero_rels.values())
                    uniform = all(_angle_close(r, rels[0], tol) for r in rels)
                    suspect_single.append({
                        "ref": fp.GetReference(),
                        "lib_id": lib_id,
                        "fp_rot_deg": round(fp.GetOrientation().AsDegrees(), 2),
                        "n_pads": len(fp_fp),
                        "rel_pattern": {k: round(v, 2) for k, v in fp_fp.items()},
                        "uniform_offset_deg": round(rels[0], 2) if uniform else None,
                        "reason": (
                            "single-instance footprint with non-zero "
                            "uniform pad rotation offset — likely stale "
                            "but no peer to corroborate. Pass force=true "
                            "to repair (verify library first)."
                            if uniform
                            else "single-instance footprint with mixed "
                            "non-zero pad rotation offsets — unusual; "
                            "manual review required."
                        ),
                    })
                else:
                    clean_count += 1
                continue

            # ≥2 instances. Majority bucket is the library reference.
            # Tie-break: the bucket whose rel-fingerprint is closest to
            # zero wins, because the overwhelming library convention is
            # "all pads at rel=0". Without this bias, a 2-instance board
            # where one is clean (rel=0) and one is stale (rel=270)
            # could pick either as reference depending on iteration
            # order — and picking the stale one as reference propagates
            # the corruption during repair.
            def _bucket_priority(bucket):
                bucket_fp, members = bucket
                rels = list(bucket_fp.values())
                if not rels:
                    max_abs_rel = 180.0  # no rels — worst case
                else:
                    # Shortest angular distance from 0 per pad; take the
                    # max so a single off-zero pad penalises the bucket.
                    max_abs_rel = max(
                        min(_wrap_deg(r), 360.0 - _wrap_deg(r)) for r in rels
                    )
                # Sort: bigger size first (descending), then smaller
                # penalty first (ascending).
                return (-len(members), max_abs_rel)

            buckets.sort(key=_bucket_priority)
            ref_fp, ref_members = buckets[0]
            ref_anchor = ref_members[0]

            # Is the reference bucket itself suspect? When the
            # apply_positions.py-style corruption hits every instance
            # of a lib_id uniformly (e.g. all 7 power MOSFETs got the
            # same regex rotation), there's no peer-disagreement to
            # exploit — the cross-instance majority would happily call
            # the mass-corrupt state "clean". Detect that by checking
            # whether the reference fingerprint itself has a uniform
            # non-zero rel.
            ref_rels = list(ref_fp.values())
            ref_uniform = bool(ref_rels) and all(
                _angle_close(r, ref_rels[0], tol) for r in ref_rels
            )
            ref_is_zero = ref_uniform and _angle_close(ref_rels[0], 0.0, tol)

            for ref_member in ref_members:
                if ref_uniform and not ref_is_zero:
                    # Reference bucket is uniformly offset from
                    # rel=0 — treat all members as suspect (could be
                    # legitimate library convention OR mass
                    # corruption). Goes to suspect_single_instance
                    # since the safety semantics are the same: we
                    # can't auto-repair, the user must verify the
                    # library and pass force=true.
                    suspect_single.append({
                        "ref": ref_member.GetReference(),
                        "lib_id": lib_id,
                        "fp_rot_deg": round(ref_member.GetOrientation().AsDegrees(), 2),
                        "n_pads": len(ref_fp),
                        "rel_pattern": {k: round(v, 2) for k, v in ref_fp.items()},
                        "uniform_offset_deg": round(ref_rels[0], 2),
                        "reason": (
                            f"all {len(ref_members)} instance(s) of "
                            f"{lib_id} agree on a uniform pad-rotation "
                            f"offset of {round(ref_rels[0], 2)}° — either "
                            f"the library default OR an apply_positions-"
                            f"style mass corruption. Cross-instance vote "
                            f"can't distinguish; pass force=true to repair "
                            f"after verifying the library footprint."
                        ),
                    })
                else:
                    clean_count += 1

            # Every other bucket is a stale group (or a different
            # legitimate variant — we can't always tell, but the
            # repair only changes pad ROTATIONS, not positions, so
            # in the worst case repairing a legitimate-variant
            # footprint just makes it look like the majority).
            for bucket_fp, members in buckets[1:]:
                for fp in members:
                    delta = _is_uniform_rotation_drift(bucket_fp, ref_fp, tol)
                    pos = fp.GetPosition()
                    entry = {
                        "ref": fp.GetReference(),
                        "lib_id": lib_id,
                        "compared_to": ref_anchor.GetReference(),
                        "fp_rot_deg": round(fp.GetOrientation().AsDegrees(), 2),
                        "uniform_offset_deg": (
                            round(delta, 2) if delta is not None else None
                        ),
                        "position_mm": {
                            "x": round(pos.x / 1_000_000, 3),
                            "y": round(pos.y / 1_000_000, 3),
                        },
                        "n_pads": len(bucket_fp),
                        "reason": (
                            f"pad rotations uniformly off by "
                            f"{round(delta, 2)}° from reference instance "
                            f"{ref_anchor.GetReference()} — rotation likely "
                            f"didn't propagate to pads"
                            if delta is not None
                            else f"pad rotations differ non-uniformly from "
                            f"reference instance {ref_anchor.GetReference()} — "
                            f"shapes may be individually misoriented; manual review"
                        ),
                        "_can_auto_repair": delta is not None,
                        "_ref_fingerprint": ref_fp,
                    }
                    confirmed_stale.append(entry)

        # Repair phase.
        repaired: List[str] = []
        repair_errors: List[Dict[str, Any]] = []

        if not dry_run:
            to_repair: List[Dict[str, Any]] = []
            # Confirmed-stale with uniform drift: safe to auto-repair.
            to_repair.extend(e for e in confirmed_stale if e["_can_auto_repair"])
            # With force: also repair single-instance suspects and
            # non-uniform mismatches. The user is asserting they've
            # verified the library is clean.
            if force:
                to_repair.extend(e for e in confirmed_stale if not e["_can_auto_repair"])
                # Single-instance suspects: assume target rel = 0
                # (canonical clean library).
                for s in suspect_single:
                    if s.get("uniform_offset_deg") is not None:
                        to_repair.append({
                            "ref": s["ref"],
                            "lib_id": s["lib_id"],
                            "_ref_fingerprint": {k: 0.0 for k in s["rel_pattern"]},
                            "_force_single": True,
                        })

            ref_to_fp = {fp.GetReference(): fp for fp in self.board.GetFootprints()}
            for entry in to_repair:
                ref = entry["ref"]
                fp = ref_to_fp.get(ref)
                if fp is None:
                    repair_errors.append({"ref": ref, "error": "footprint not found"})
                    continue
                fp_rot_deg = fp.GetOrientation().AsDegrees()
                ref_fingerprint = entry["_ref_fingerprint"]
                try:
                    for pad in fp.Pads():
                        num = pad.GetNumber()
                        rel = ref_fingerprint.get(num, 0.0)
                        pad.SetOrientationDegrees(fp_rot_deg + rel)
                    repaired.append(ref)
                except Exception as e:
                    repair_errors.append({"ref": ref, "error": str(e)})

        # Strip internal-only fields before returning.
        public_stale = [
            {k: v for k, v in e.items() if not k.startswith("_")}
            for e in confirmed_stale
        ]

        return {
            "success": True,
            "dryRun": dry_run,
            "audited": len(fps),
            "clean": clean_count,
            "confirmed_stale": public_stale,
            "suspect_single_instance": suspect_single,
            "repaired": repaired,
            "repair_errors": repair_errors,
            "message": (
                f"repair_pad_rotations: audited {len(fps)} footprint(s); "
                f"{len(confirmed_stale)} confirmed-stale, "
                f"{len(suspect_single)} suspect single-instance, "
                f"{len(repaired)} repaired" + (" (dry-run)" if dry_run else "")
            ),
        }
