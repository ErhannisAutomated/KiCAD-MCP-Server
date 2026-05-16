"""PCB integrity checks — silent-corruption detector.

These checks catch a class of bug where the PCB looks fine to every
automated check (DRC ratsnest uses pad centres, freerouting cost
function uses pad centres, etc.) but the SHAPES are wrong. Real
incidents the tool prevents:

  * 2026-05-14: ``apply_positions.py`` regex-rewrote footprint
    ``(at X Y rot)`` lines without going through
    ``SetOrientationDegrees()``. 57 of 71 footprints ended up with
    correct pad CENTRES but wrong pad SHAPES (USB-C 16P pads stacked
    on top of each other; HTSSOP-28 pad rows pointed into the body).
    Invisible to DRC; only visual review caught it.

  * 2026-05-14: C24 placed inside L2's bbox. Invisible to DRC until
    routing failed.

Subchecks:

  * ``pad_rotation``: For each lib_id with 2+ instances, the
    per-pad orientation relative to the footprint's own orientation
    must be consistent across instances. Catches the apply_positions
    bug whenever the affected footprint type appears twice on the
    board.

  * ``footprint_overlap``: One footprint's centre falls inside
    another's silk-excluded bbox AND they're on the same copper
    layer. Catches the C24-inside-L2 bug.

  * ``stacked_pads``: Within a single footprint, ≥2 pads at the
    exact same XY position. Catches the USB-C 16P symptom directly.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import pcbnew

logger = logging.getLogger(__name__)


# How close two pad positions must be to count as "stacked" (nm).
_STACK_THRESHOLD_NM = 50  # 50 µm — well under any real pitch


def _fp_layer_name(board: Any, fp: Any) -> str:
    return board.GetLayerName(fp.GetLayer())


def _relative_pad_orientations(fp: Any) -> Dict[str, float]:
    """For each pad on the footprint, return its orientation relative
    to the footprint's own orientation (degrees, mod 360)."""
    fp_orient = fp.GetOrientation().AsDegrees()
    out: Dict[str, float] = {}
    for pad in fp.Pads():
        pad_orient = pad.GetOrientation().AsDegrees()
        rel = (pad_orient - fp_orient) % 360
        out[pad.GetNumber()] = rel
    return out


def check_pad_rotation(board: Any) -> List[Dict[str, Any]]:
    """Find footprints whose per-pad relative orientations don't match
    other instances of the same lib_id on the board.

    Only reports a finding when the same lib_id has ≥2 instances (so
    we can compare). Single-instance footprints need the deep library
    check for now.
    """
    findings: List[Dict[str, Any]] = []
    by_lib: Dict[str, List[Any]] = {}
    for fp in board.GetFootprints():
        lib_id = fp.GetFPID().GetUniStringLibId()
        by_lib.setdefault(lib_id, []).append(fp)

    for lib_id, fps in by_lib.items():
        if len(fps) < 2:
            continue
        # Use the first instance as the reference.
        ref_fp = fps[0]
        ref_rels = _relative_pad_orientations(ref_fp)
        for other in fps[1:]:
            other_rels = _relative_pad_orientations(other)
            mismatches = []
            for pad_num, ref_rel in ref_rels.items():
                other_rel = other_rels.get(pad_num)
                if other_rel is None:
                    continue
                diff = (other_rel - ref_rel) % 360
                if diff > 0.1 and diff < 359.9:
                    mismatches.append({
                        "pad": pad_num,
                        "ref_rel_deg": round(ref_rel, 2),
                        "other_rel_deg": round(other_rel, 2),
                        "diff_deg": round(diff, 2),
                    })
            if mismatches:
                pos = other.GetPosition()
                # Uniform drift (all pads off by the same amount) is
                # cosmetic — the shape is just rotated as a whole, the
                # pad layout is geometrically equivalent. Mixed drifts
                # mean pad shapes are individually misoriented, which
                # is the dangerous case (USB-C 16P symptom).
                deltas = {m["diff_deg"] for m in mismatches}
                if len(deltas) == 1:
                    severity = "warning"
                    note = (
                        " All pads differ by the same delta — likely a "
                        "cosmetic rotation drift; the pad layout is still "
                        "geometrically valid."
                    )
                else:
                    severity = "error"
                    note = (
                        " Pads have DIFFERENT deltas — pad shapes are "
                        "individually misoriented; routing may fail."
                    )
                findings.append({
                    "type": "pad_rotation_mismatch",
                    "severity": severity,
                    "ref": other.GetReference(),
                    "lib_id": lib_id,
                    "compared_to": ref_fp.GetReference(),
                    "position": {
                        "x": pos.x / 1_000_000,
                        "y": pos.y / 1_000_000,
                        "unit": "mm",
                    },
                    "uniform_drift": len(deltas) == 1,
                    "mismatches": mismatches,
                    "message": (
                        f"{other.GetReference()} ({lib_id}) has pad rotations "
                        f"inconsistent with {ref_fp.GetReference()} — "
                        f"{len(mismatches)} pad(s) differ." + note
                    ),
                })
    return findings


_MIN_OVERLAP_MM2 = 0.01  # below this, just edges touching — skip


def check_footprint_overlap(board: Any, min_overlap_mm2: float = _MIN_OVERLAP_MM2) -> List[Dict[str, Any]]:
    """Find footprints whose silk-excluded bboxes overlap on the SAME
    copper layer.

    Returns one finding per overlapping pair, with the overlap area
    reported so callers can judge severity. Overlaps below
    ``min_overlap_mm2`` (default 0.01 mm² — basically just edges
    touching) are skipped.

    Severity scaling:
      * **error** if overlap area > 0.1 mm² (meaningful collision) OR
        one footprint's centre is inside the other's bbox (full nesting,
        like the 2026-05-14 C24-inside-L2 bug).
      * **warning** otherwise (small overlap; usually a tight-but-legal
        placement worth a look).

    Same-layer filter prevents the obvious false positive of front-side
    components nested under a back-side battery holder.
    """
    findings: List[Dict[str, Any]] = []
    fps_info = []
    for fp in board.GetFootprints():
        pos = fp.GetPosition()
        bb = fp.GetBoundingBox(False)
        fps_info.append({
            "ref": fp.GetReference(),
            "centre": (pos.x, pos.y),
            "bbox": (bb.GetLeft(), bb.GetTop(), bb.GetRight(), bb.GetBottom()),
            "layer": fp.GetLayer(),
            "layer_name": _fp_layer_name(board, fp),
        })

    n = len(fps_info)
    for i in range(n):
        a = fps_info[i]
        al, at, ar, ab = a["bbox"]
        for j in range(i + 1, n):
            b = fps_info[j]
            if a["layer"] != b["layer"]:
                continue
            bl, bt, br, bb_ = b["bbox"]
            ix0 = max(al, bl)
            iy0 = max(at, bt)
            ix1 = min(ar, br)
            iy1 = min(ab, bb_)
            if ix0 >= ix1 or iy0 >= iy1:
                continue
            area_mm2 = ((ix1 - ix0) * (iy1 - iy0)) / 1_000_000_000_000
            if area_mm2 < min_overlap_mm2:
                continue
            # Detect full nesting — either centre inside the other's bbox.
            ax, ay = a["centre"]
            bx, by = b["centre"]
            a_in_b = bl <= ax <= br and bt <= ay <= bb_
            b_in_a = al <= bx <= ar and at <= by <= ab
            full_nesting = a_in_b or b_in_a
            severity = "error" if (area_mm2 > 0.1 or full_nesting) else "warning"
            nesting_note = ""
            if a_in_b:
                nesting_note = f" {a['ref']} centre is INSIDE {b['ref']}."
            elif b_in_a:
                nesting_note = f" {b['ref']} centre is INSIDE {a['ref']}."
            findings.append({
                "type": "footprint_bbox_overlap",
                "severity": severity,
                "ref_a": a["ref"],
                "ref_b": b["ref"],
                "layer": a["layer_name"],
                "overlap_mm2": round(area_mm2, 3),
                "overlap_w_mm": round((ix1 - ix0) / 1_000_000, 2),
                "overlap_h_mm": round((iy1 - iy0) / 1_000_000, 2),
                "full_nesting": full_nesting,
                "position": {
                    "x": (ix0 + ix1) / 2 / 1_000_000,
                    "y": (iy0 + iy1) / 2 / 1_000_000,
                    "unit": "mm",
                },
                "message": (
                    f"{a['ref']} and {b['ref']} bboxes overlap by "
                    f"{area_mm2:.3f} mm² on {a['layer_name']}." + nesting_note
                ),
            })
    return findings


def check_stacked_pads(board: Any) -> List[Dict[str, Any]]:
    """Within a single footprint, flag pads whose centres are within
    ``_STACK_THRESHOLD_NM`` of each other AND whose net + pad-number
    combination makes the stacking suspicious.

    Skip rules (false-positive avoidance):

      * Pads with the same number (e.g. thermal-pad copies all
        numbered "29", or USB-C VBUS pads A4/B9 that are deliberately
        paralleled).  Same-number stacking is the footprint library
        author's explicit choice.
      * Pads with an empty number ("" mounting/silk pads).
      * Pads on the same net (USB-C convention pairs A1/B12 GND etc.).
        If the library author intended to short two named pads, they
        should be on the same net.

    What's left is the case the check exists for: two DIFFERENTLY
    numbered pads on DIFFERENT nets that ended up at the same XY,
    which only happens via the 2026-05-14 apply_positions-style
    corruption.
    """
    findings: List[Dict[str, Any]] = []
    for fp in board.GetFootprints():
        pads = list(fp.Pads())
        if len(pads) < 2:
            continue
        n = len(pads)
        for i in range(n):
            for j in range(i + 1, n):
                num_i = pads[i].GetNumber()
                num_j = pads[j].GetNumber()
                if not num_i or not num_j:
                    continue  # mounting/silk pad
                if num_i == num_j:
                    continue  # thermal pad copies
                net_i = pads[i].GetNetname()
                net_j = pads[j].GetNetname()
                if net_i and net_j and net_i == net_j:
                    continue  # intentionally paralleled
                pi = pads[i].GetPosition()
                pj = pads[j].GetPosition()
                dx = pi.x - pj.x
                dy = pi.y - pj.y
                d_nm = (dx * dx + dy * dy) ** 0.5
                if d_nm > _STACK_THRESHOLD_NM:
                    continue
                findings.append({
                    "type": "stacked_pads",
                    "severity": "error",
                    "ref": fp.GetReference(),
                    "pad_a": num_i,
                    "pad_b": num_j,
                    "net_a": net_i,
                    "net_b": net_j,
                    "position": {
                        "x": pi.x / 1_000_000,
                        "y": pi.y / 1_000_000,
                        "unit": "mm",
                    },
                    "separation_um": round(d_nm / 1000, 3),
                    "message": (
                        f"{fp.GetReference()} pads {num_i} ({net_i or '<no net>'}) "
                        f"and {num_j} ({net_j or '<no net>'}) are stacked "
                        f"({d_nm / 1000:.1f} µm apart) on different nets. "
                        f"Likely footprint rotation didn't propagate to pads."
                    ),
                })
    return findings


_AVAILABLE_CHECKS = {
    "pad_rotation": check_pad_rotation,
    "footprint_overlap": check_footprint_overlap,
    "stacked_pads": check_stacked_pads,
}


def run_integrity_checks(
    board: Any,
    checks: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Run the requested subchecks and return a consolidated report.

    ``checks`` is a list of subcheck names; default is all of them.
    Unknown names are reported as errors.
    """
    if checks is None:
        checks = list(_AVAILABLE_CHECKS.keys())

    unknown = [c for c in checks if c not in _AVAILABLE_CHECKS]
    if unknown:
        return {
            "success": False,
            "message": f"Unknown check(s): {unknown}",
            "available": list(_AVAILABLE_CHECKS.keys()),
        }

    all_findings: List[Dict[str, Any]] = []
    by_check: Dict[str, int] = {}
    for check_name in checks:
        fn = _AVAILABLE_CHECKS[check_name]
        try:
            findings = fn(board)
        except Exception as e:
            logger.exception(f"check {check_name} crashed")
            findings = [{
                "type": f"{check_name}_error",
                "severity": "error",
                "message": f"check {check_name} crashed: {e}",
            }]
        by_check[check_name] = len(findings)
        all_findings.extend(findings)

    by_severity: Dict[str, int] = {"error": 0, "warning": 0, "info": 0}
    for f in all_findings:
        sev = f.get("severity", "error")
        by_severity[sev] = by_severity.get(sev, 0) + 1

    return {
        "success": True,
        "total": len(all_findings),
        "summary": {
            "by_check": by_check,
            "by_severity": by_severity,
        },
        "findings": all_findings,
        "checks_run": checks,
        "message": (
            f"check_pcb_integrity: {len(all_findings)} finding(s) "
            f"across {len(checks)} check(s)"
        ),
    }
