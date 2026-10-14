"""Optional automatic workflow and operator tasks; no image-processing rules.

All measurements and QA thresholds belong to pipeline/analysis. This adapter
checks that the automatic positions and required results actually exist before
presenting their verdicts. Technical attention is distinct from a QA failure.
"""
from __future__ import annotations

import calendar
from dataclasses import asdict, dataclass, field
from datetime import datetime

from . import ALGO_VERSION, pipeline
from .ingest import protocol_signature

from .analysis.assessment import (LABELS, SIDES, assess_features, collect_overlays,
                                 evaluate_results)


@dataclass
class Task:
    id: str
    type: str
    title: str
    description: str
    priority: str = "normal"
    status: str = "open"
    related_feature: str | None = None
    due_date: str | None = None
    steps: list[str] = field(default_factory=list)
    image_references: list[dict] = field(default_factory=list)


def next_scan_date(uploaded_at):
    """One calendar month from the server timestamp, clamped at month end."""
    try:
        uploaded = datetime.fromisoformat(str(uploaded_at).replace("Z", "+00:00")).date()
        year = uploaded.year + (uploaded.month == 12)
        month = uploaded.month % 12 + 1
        return uploaded.replace(year=year, month=month,
                                day=min(uploaded.day, calendar.monthrange(year, month)[1]))
    except (TypeError, ValueError, OverflowError):
        return None


def generate_tasks(aid, uploaded_at, features, duplicate):
    tasks = []
    due = next_scan_date(uploaded_at)
    if due:
        # Explicit English names: server locale must not change operator text.
        months = ("January", "February", "March", "April", "May", "June", "July",
                  "August", "September", "October", "November", "December")
        tasks.append(Task(f"{aid}:next_scan", "next_scan", "Next phantom scan",
                          f"Repeat the phantom scan before {due.day} {months[due.month-1]} {due.year}.",
                          due_date=due.isoformat()))
    else:
        tasks.append(Task(f"{aid}:missing_upload_date", "analysis_attention",
                          "Upload date missing",
                          "Next scan date unavailable.",
                          priority="high"))
    affected = [f for f in features if f["state"] != "pass"]
    if affected:
        if duplicate:
            tasks.append(Task(f"{aid}:contact_technician", "contact_technician",
                              "Contact the MSF technician",
                              "Contact the MSF technician.", priority="high"))
        else:
            tasks.append(Task(f"{aid}:repeat_scan", "repeat_scan", "Repeat the phantom scan once",
                              "Repeat the phantom scan once and upload the new scan.", priority="high"))
    return [asdict(t) for t in tasks]


def run_automatic(scan, pdef):
    """Run the existing pipeline with untouched automatic proposals only."""
    reg, geometry, results = None, {}, {}
    try:
        reg = pipeline.run_stage_a(scan, pdef)
        ctx = pipeline.build_ctx(scan, pdef, reg, {"scan_meta": scan.meta, "sid_mm": 1000.0})
        geometry = pipeline.propose_all(ctx)
        results = pipeline.compute_all(ctx, geometry)
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        geometry = geometry if isinstance(geometry, dict) else {}
        results = results if isinstance(results, dict) else {}
        for name in pipeline.TESTS:
            geometry.setdefault(name, {"_error": detail})
            results.setdefault(name, {"status": "error", "error": detail})
    geometry = pipeline.to_jsonable(geometry)
    results = pipeline.to_jsonable(results)
    if all(isinstance(results.get(name), dict) and "_assessment" in results[name]
           for name in pipeline.TESTS):
        # Consume the pipeline's verdict once, exactly as Analyze receives it.
        features = [{**results[name]["_assessment"],
                     "overlays": collect_overlays(geometry.get(name) or {})}
                    for name in pipeline.TESTS]
    else:
        results, features = evaluate_results(geometry, results, pdef)
    results.setdefault("_meta", {}).update({"algo_version": ALGO_VERSION,
        "signature": protocol_signature(scan.meta), "workflow": "auto_analyse", "manually_reviewed": False})
    return {"reg": pipeline.registration_to_dict(reg) if reg else None,
            "geometry": geometry, "results": results, "features": features,
            "status": pipeline.overall_status(results)}


def result_payload(record, automatic, duplicate, *, saved):
    features = automatic["features"]
    all_pass = all(f["state"] == "pass" for f in features)
    return {"id": record["id"], "source_name": record["source_name"],
            "created_at": record.get("created_at"), "duplicate": duplicate,
            "saved": saved, "history_record_unchanged": duplicate,
            "overall": "pass" if all_pass else "attention", "status": automatic["status"],
            "message": "All features passed. No further action required." if all_pass
                       else "One or more features need attention.",
            "features": features, "results": automatic["results"],
            "tasks": generate_tasks(record["id"], record.get("created_at"), features, duplicate),
            "validation_status": "", "manually_reviewed": False,
            "history_validation_status": record.get("validation_status") or ""}
