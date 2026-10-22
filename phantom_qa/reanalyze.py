"""Recompute stored analyses from the source files the app already keeps.

The point: **you never have to re-upload anything.** Every analysis keeps its
original file under ``data/uploads/``, so when the algorithm or the phantom
definition improves, the stored results can be brought up to date in place.

Two modes, because they answer different questions:

``results``  recompute the numbers from the geometry the user already confirmed.
             Manual ROI adjustments are preserved. This is what you want after
             an ALGORITHM change.
``full``     re-detect the phantom and all ROIs, then compute. Manual
             adjustments are discarded. This is what you want after a PHANTOM
             DEFINITION change.

Labels, validation, baseline flag and the audit trail are always preserved.
Analyses that an administrator has signed off are skipped unless explicitly
included — re-running changes numbers somebody put their name to.
"""

from __future__ import annotations

import os
import math

from . import ALGO_VERSION, ingest, pipeline
from .logging_setup import audit, get_logger
from .phantom_def import PhantomDef
from .store import AnalysisConflict

log = get_logger("reanalyze")


def is_outdated(rec: dict, pdef: PhantomDef) -> tuple[bool, str]:
    """Was this analysis produced by a different algorithm or definition?"""
    reasons = []
    if (rec.get("algo_version") or "") != ALGO_VERSION:
        reasons.append(f"algorithm {rec.get('algo_version') or '?'} "
                       f"-> {ALGO_VERSION}")
    if (rec.get("pdef_version") or "") != pdef.version:
        reasons.append(f"phantom definition {rec.get('pdef_version') or '?'} "
                       f"-> {pdef.version}")
    return bool(reasons), "; ".join(reasons)


def find_outdated(store, pdef: PhantomDef) -> list[dict]:
    out = []
    for item in store.list_all():
        rec = store.get(item["id"])
        if not rec or not rec.get("results"):
            continue
        stale, why = is_outdated(rec, pdef)
        if stale:
            out.append({"id": rec["id"], "site": rec.get("site", ""),
                        "phantom": rec.get("phantom", ""),
                        "acquired_at": rec.get("acquired_at", ""),
                        "validation_status": rec.get("validation_status", ""),
                        "reason": why})
    return out


def reanalyze_one(store, pdef: PhantomDef, aid: str, mode: str = "results",
                  dry_run: bool = False, user: str = "cli",
                  include_validated: bool = False) -> dict:
    """Recompute one analysis. Returns a summary of what changed."""
    rec = store.get(aid)
    if rec is None:
        return {"id": aid, "status": "not_found"}
    sid_mm = rec.get("sid_mm") if rec.get("sid_mm") is not None else 1000.0
    if not math.isfinite(sid_mm) or sid_mm <= 0:
        return {"id": aid, "status": "invalid_parameters",
                "message": "Stored SID must be a finite positive distance. Correct it in Analyze first."}
    if rec.get("validation_status") and not include_validated and not dry_run:
        # The module's promise — signed-off analyses are skipped unless
        # explicitly included — has to hold on THIS path too. Naming an id
        # used to bypass it silently, recomputing numbers an administrator
        # had put their name to. The web endpoints refuse the same way.
        return {"id": aid, "status": "skipped_validated",
                "site": rec.get("site", ""), "phantom": rec.get("phantom", ""),
                "message": f"signed off by "
                           f"{rec.get('validated_by') or 'someone'}; "
                           f"re-run with --include-validated to recompute it"}
    if mode == "results":
        # Results mode replays the stored geometry, whose pixel coordinates
        # only mean anything together with the transform that produced them.
        # Without either, the honest answer is to say so rather than quietly
        # re-detect and discard what the user confirmed.
        missing = [name for name, key in (("geometry", "geometry"),
                                          ("registration", "reg"))
                   if not rec.get(key)]
        if missing:
            return {"id": aid, "status": "skipped",
                    "site": rec.get("site", ""), "phantom": rec.get("phantom", ""),
                    "message": f"no stored {' or '.join(missing)}; "
                               f"re-run with --full to detect it again"}

    integrity = store.verify_integrity(aid)
    if integrity["status"] != "ok":
        # Refuse rather than compute new numbers from a file that no longer
        # matches what the original results were derived from.
        return {"id": aid, "status": "integrity_failed",
                "message": integrity["message"]}

    path = store.upload_path(aid)
    if not os.path.exists(path):
        return {"id": aid, "status": "source_missing",
                "site": rec.get("site", ""), "phantom": rec.get("phantom", ""),
                "message": "the stored source file is gone; nothing to "
                           "recompute from"}
    with open(path, "rb") as f:
        data = f.read()
    try:
        scans = ingest.load_any_bytes(data, rec["source_name"])
    except Exception as e:
        # One unreadable file must not abort a whole batch run.
        log.error("cannot decode stored file for %s: %s", aid, e)
        return {"id": aid, "status": "unreadable",
                "site": rec.get("site", ""), "phantom": rec.get("phantom", ""),
                "message": f"stored file could not be decoded ({e})"}
    scan = next((s for s in scans if s.sha256 == rec["sha256"]), scans[0])

    old_status = rec.get("status")
    old_results = rec.get("results") or {}

    if mode == "full":
        reg = pipeline.run_stage_a(scan, pdef)
        ctx = pipeline.build_ctx(scan, pdef, reg,
                                 {"scan_meta": scan.meta,
                                  "sid_mm": sid_mm})
        geometry = pipeline.propose_all(ctx)
    else:
        reg = pipeline.registration_from_dict(rec["reg"])
        ctx = pipeline.build_ctx(scan, pdef, reg,
                                 {"scan_meta": scan.meta,
                                  "sid_mm": sid_mm})
        geometry = rec["geometry"]

    results = pipeline.compute_all(ctx, geometry)
    new_status = pipeline.overall_status(results)

    summary = {
        "id": aid, "status": "would_change" if dry_run else "updated",
        "mode": mode,
        "site": rec.get("site", ""), "phantom": rec.get("phantom", ""),
        "old_overall": old_status, "new_overall": new_status,
        "old_algo": rec.get("algo_version"), "new_algo": ALGO_VERSION,
        "old_pdef": rec.get("pdef_version"), "new_pdef": pdef.version,
        "changes": _diff(old_results, results),
    }
    if dry_run:
        return summary

    try:
        store.save_reanalysis(
            aid, rec, results, new_status, algo_version=ALGO_VERSION,
            pdef_version=pdef.version, include_validated=include_validated, user=user,
            registration=pipeline.registration_to_dict(reg) if mode == "full" else None,
            geometry=geometry if mode == "full" else None)
    except (AnalysisConflict, KeyError) as exc:
        return {"id": aid, "status": "conflict", "message": str(exc)}
    store.audit(aid, "E", "re-analysed",
                {"mode": mode, "algo": f"{summary['old_algo']} -> {ALGO_VERSION}",
                 "pdef": f"{summary['old_pdef']} -> {pdef.version}",
                 "overall": f"{old_status} -> {new_status}"})
    audit("reanalyze", user=user, analysis=aid, outcome=new_status,
          mode=mode, old_overall=old_status,
          old_algo=summary["old_algo"], new_algo=ALGO_VERSION,
          old_pdef=summary["old_pdef"], new_pdef=pdef.version)
    log.info("re-analysed %s mode=%s %s -> %s", aid, mode, old_status,
             new_status)
    return summary


def _diff(old: dict, new: dict, limit: int = 6) -> list[str]:
    """Human-readable summary of the metrics that moved most."""
    from .store import flatten_results
    try:
        o = {(r["test"], r["object"], r["metric"]): r["value"]
             for r in flatten_results(old)}
        n = {(r["test"], r["object"], r["metric"]): r["value"]
             for r in flatten_results(new)}
    except Exception:
        return []
    rows = []
    for k, nv in n.items():
        ov = o.get(k)
        if not isinstance(ov, (int, float)) or not isinstance(nv, (int, float)):
            continue
        if ov == 0:
            continue
        pct = 100.0 * (nv - ov) / abs(ov)
        if abs(pct) >= 0.5:
            rows.append((abs(pct), f"{k[0]}·{k[1]}·{k[2]} {ov:.4g}->{nv:.4g} "
                                   f"({pct:+.1f}%)"))
    rows.sort(key=lambda t: -t[0])
    return [r[1] for r in rows[:limit]]


def reanalyze_all(store, pdef: PhantomDef, mode: str = "results",
                  dry_run: bool = False, only_outdated: bool = True,
                  include_validated: bool = False,
                  user: str = "cli") -> list[dict]:
    out = []
    for item in store.list_all():
        rec = store.get(item["id"])
        if not rec or not rec.get("results"):
            continue
        if only_outdated and not is_outdated(rec, pdef)[0]:
            continue
        if rec.get("validation_status") and not include_validated:
            out.append({"id": rec["id"], "status": "skipped_validated",
                        "site": rec.get("site", ""),
                        "phantom": rec.get("phantom", ""),
                        "message": f"signed off by "
                                   f"{rec.get('validated_by') or 'someone'}; "
                                   f"use --include-validated to recompute"})
            continue
        out.append(reanalyze_one(store, pdef, rec["id"], mode=mode,
                                 dry_run=dry_run, user=user,
                                 include_validated=include_validated))
    return out
