"""Optional automatic workflow and operator tasks; no image-processing rules.

All measurements and QA thresholds belong to pipeline/analysis. This adapter
checks that the automatic positions and required results actually exist before
presenting their verdicts. Technical attention is distinct from a QA failure.
"""
from __future__ import annotations

import calendar
import copy
from dataclasses import asdict, dataclass, field
from datetime import datetime

from . import pipeline
from . import solutions

from .analysis.assessment import LABELS, SIDES, assess_features, collect_overlays


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
    solutions: list[dict] = field(default_factory=list)


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
            names = []
            for feature in affected:
                if feature["id"] == "geometry":
                    paths = " ".join(issue["path"] for issue in feature.get("issues", []))
                    if "field" in paths:
                        names.append("X-ray field alignment")
                    if any(part in paths for part in ("dimension", "ruler", "scale", "corner")):
                        names.append("Dimensions")
                    if not names:
                        names.append("Geometry")
                else:
                    names.append(LABELS[feature["id"]])
            tests = ", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else names[0]
            tasks.append(Task(f"{aid}:contact_technician", "contact_technician",
                              "Contact the MSF technician",
                              f"Contact the MSF technician about {tests}.", priority="high"))
        else:
            tasks.append(Task(f"{aid}:repeat_scan", "repeat_scan", "Repeat the phantom scan once",
                              "Repeat the phantom scan once and upload the new scan.", priority="high"))
    for feature in affected:
        for key in solutions.feature_keys(feature):
            steps = solutions.load_steps(key)
            tasks.append(Task(f"{aid}:solution:{key}", "feature_solution",
                              "Check " + solutions.FEATURES[key].replace("-", " "),
                              "Check " + solutions.FEATURES[key].replace("-", " ") + ".",
                              priority="high", related_feature=feature["id"], solutions=steps))
    return [asdict(t) for t in tasks]


def result_payload(record, pdef, duplicate, *, saved):
    """Render the persisted record, including manual corrections, without recomputing."""
    geometry, results = record.get("geometry") or {}, record.get("results") or {}
    if all(isinstance(results.get(name), dict) and "_assessment" in results[name] for name in pipeline.TESTS):
        features = [{**results[name]["_assessment"], "overlays": collect_overlays(geometry.get(name) or {})}
                    for name in pipeline.TESTS]
    else:
        # Legacy results remain stored as originally computed; no fresh measurement is invented.
        features = assess_features(geometry, copy.deepcopy(results), pdef)
    all_pass = all(f["state"] == "pass" for f in features)
    return {"id": record["id"], "source_name": record["source_name"],
            "created_at": record.get("created_at"), "duplicate": duplicate,
            "saved": saved, "history_record_unchanged": duplicate,
            "overall": "pass" if all_pass else "attention", "status": record.get("status"),
            "message": "All features passed. No further action required." if all_pass
                       else "One or more features need attention.",
            "features": features, "results": results, "geometry": geometry,
            **{key: record.get(key, "") for key in ("site", "phantom", "operator", "notes")},
            "tasks": generate_tasks(record["id"], record.get("created_at"), features, duplicate),
            "validation_status": "", "manually_reviewed": False,
            "history_validation_status": record.get("validation_status") or ""}
