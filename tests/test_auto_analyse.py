"""Automatic workflow: trustworthy verdicts, duplicate safety and operator tasks."""
from __future__ import annotations

import copy
import io
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from phantom_qa import ALGO_VERSION, auto_analyse as auto, ingest, pipeline
from phantom_qa.registration import Registration, Transform
from phantom_qa.store import Store
from test_authorization import _build_app, _login


@pytest.fixture
def passing(pdef):
    """Complete algorithm-shaped results; no acceptance thresholds in fixture."""
    rect = {"type": "rect", "corners_px": [[1, 1], [10, 1], [10, 10], [1, 10]]}
    segment = {"type": "segment", "p0_px": [2, 2], "p1_px": [9, 9]}
    circle = {"type": "circle", "center_px": [5, 5], "radius_px": 2}
    position = {"detected": True, "positioning": {"accepted": True}}
    geometry = {
        "geometry": {"corners": {k: {"px": v} for k, v in zip(("TL", "TR", "BR", "BL"), rect["corners_px"])},
                     "rulers": {s: {"detected": True, "probe": segment} for s in auto.SIDES},
                     "field_edges": {s: {"detected": True, "edge_pt_px": [5, 5]} for s in auto.SIDES}},
        "linepairs": {"groups": [{**position, "id": g["id"], "roi": rect, "profile_seg": segment} for g in pdef.linepairs["groups"]]},
        "lowcontrast": {**position, "block": rect, "grid_positioning": {"accepted": True},
                        "circles": [{"id": c["id"], "roi": circle} for c in pdef.lowcontrast["circles"]]},
        "uniformity": {"squares": [{**position, "id": s["id"], "roi": rect} for s in pdef.uniformity["squares"]]},
        "wedge": {**position, "axis": segment, "steps": [{"step": s["step"], "fits": True, "roi": rect} for s in pdef.wedge["steps"]]},
    }
    results = {
        "geometry": {"dimension_status": "pass", "field_status": "pass", "scale": {"reliable": True},
                     "dimensions": {"mean_side_mm": 300., "dev_from_nominal_pct": 0.},
                     "rulers": {s: {"detected": True, "reliable": True, "pitch_mm": 5., "pitch_dev_pct": 0.,
                                    "central_line_from_edge_mm": 20., "linearity_rms_mm": 0.01} for s in auto.SIDES},
                     "field_alignment": {s: {"detected": True, "status": "pass", "pct_of_sid": 0.,
                                             "deviation_from_central_line_mm": 0.} for s in auto.SIDES}},
        "linepairs": {"status": "pass", "rows": [{"id": g["id"], "freq_lp_mm": g.get("freq_lp_mm", 1.), "mean": 100., "std": 5., "n": 100,
                      "status": "pass", "linearity": {"measured_pitch_mm": 1., "pitch_dev_pct": 0., "residual_rms_mm": 0.01}}
                      for g in pdef.linepairs["groups"]]},
        "lowcontrast": {"status": "pass", "ordering_ok": True, "rows": [{"id": c["id"], "level": c.get("level", 1), "obj_mean": 100., "obj_std": 5.,
                        "obj_n": 100, "bg_mean": 80., "bg_std": 5., "bg_n": 100, "cnr": 2., "abs_cnr": 2.}
                        for c in pdef.lowcontrast["circles"]]},
        "uniformity": {"status": "pass", "snr_avg": 20., "mean_avg": 100., "max_abs_dsnr_pct": 0.,
                       "rows": [{"id": s["id"], "status": "pass", "mean": 100., "std": 5., "n": 100,
                                 "snr": 20., "dsnr_pct": 0., "dmean_pct": 0.} for s in pdef.uniformity["squares"]]},
        "wedge": {"status": "pass", "fit": {"r2": 1., "slope": 10., "intercept": 0.}, "span": 100., "dynamic_range_ratio": 2.,
                  "monotonic": True, "saturated": False,
                  "rows": [{"step": s["step"], "mean": 100., "std": 5., "n": 100, "saturated": False} for s in pdef.wedge["steps"]]},
        "_meta": {"algo_version": ALGO_VERSION},
    }
    return copy.deepcopy((geometry, results))


@pytest.mark.parametrize("timestamp,expected", [
    ("2026-10-15 12:30:00", date(2026, 11, 15)),
    ("2026-01-31 23:00:00", date(2026, 2, 28)),
    ("2028-01-31T23:00:00Z", date(2028, 2, 29)),
    ("2026-03-31T23:00:00+02:00", date(2026, 4, 30)),
    ("2026-12-31", date(2027, 1, 31)),
    (None, None), ("", None), ("bad-date", None), ("2026-02-30", None),
])
def test_calendar_deadline(timestamp, expected):
    assert auto.next_scan_date(timestamp) == expected


def test_all_pass_has_only_next_scan(passing, pdef):
    features = auto.assess_features(*passing, pdef)
    assert all(f["state"] == "pass" and f["overlays"] for f in features)
    tasks = auto.generate_tasks("a", "2026-10-15 12:00:00", features, False)
    assert [t["type"] for t in tasks] == ["next_scan"]
    assert tasks[0]["description"] == "Repeat the phantom scan before 15 November 2026."
    assert tasks[0]["due_date"] == "2026-11-15"
    assert all(k in tasks[0] for k in ("id", "type", "title", "description", "priority", "status", "related_feature", "steps", "image_references"))


@pytest.mark.parametrize("group", pipeline.TESTS)
def test_missing_group_is_attention(group, passing, pdef):
    geometry, results = passing
    results.pop(group)
    feature = next(f for f in auto.assess_features(geometry, results, pdef) if f["id"] == group)
    assert feature["state"] == "attention" and feature["technical_attention"]


@pytest.mark.parametrize("group,key", [("linepairs", "groups"), ("uniformity", "squares"), ("wedge", "steps"), ("lowcontrast", "circles")])
def test_missing_subtest_cannot_pass(group, key, passing, pdef):
    geometry, results = passing
    results[group]["rows"].pop()
    feature = next(f for f in auto.assess_features(geometry, results, pdef) if f["id"] == group)
    assert feature["state"] == "attention" and feature["technical_attention"]


@pytest.mark.parametrize("group", ["linepairs", "uniformity"])
def test_failing_subtest_prevents_green_group(group, passing, pdef):
    geometry, results = passing
    results[group]["rows"][0]["status"] = "fail"  # top-level pass is deliberately inconsistent
    feature = next(f for f in auto.assess_features(geometry, results, pdef) if f["id"] == group)
    assert feature["state"] == "attention" and feature["quality_attention"]
    assert not feature["technical_attention"]


def test_geometry_subtest_and_missing_side_prevent_green(passing, pdef):
    geometry, results = passing
    results["geometry"]["dimension_status"] = "warn"
    results["geometry"]["field_alignment"].pop("left")
    feature = auto.assess_features(geometry, results, pdef)[0]
    assert feature["state"] == "attention" and feature["quality_attention"] and feature["technical_attention"]


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), "unknown"])
def test_invalid_measurement_cannot_pass(value, passing, pdef):
    geometry, results = passing
    results["uniformity"]["rows"][0]["snr"] = value
    feature = auto.assess_features(geometry, results, pdef)[3]
    assert feature["state"] == "attention" and feature["technical_attention"]


@pytest.mark.parametrize("malformed", [None, "invalid", [1, 2], {"status": "pass", "rows": [None]}])
def test_malformed_group_output_is_local_technical_attention(malformed, passing, pdef):
    geometry, results = passing
    results["uniformity"] = malformed
    features = auto.assess_features(geometry, results, pdef)
    assert features[3]["technical_attention"] and features[3]["state"] == "attention"
    assert all(f["state"] == "pass" for f in features if f["id"] != "uniformity")


def test_zero_pixel_count_is_invalid(passing, pdef):
    geometry, results = passing
    results["uniformity"]["rows"][0]["n"] = 0
    assert auto.assess_features(geometry, results, pdef)[3]["technical_attention"]


def test_incomplete_positions_keep_proposed_rois_in_orange(passing, pdef):
    geometry, results = passing
    geometry["linepairs"]["groups"][0]["detected"] = False
    geometry["wedge"]["detected"] = False
    geometry["lowcontrast"]["detected"] = False
    geometry["uniformity"]["squares"][0]["detected"] = False
    features = auto.assess_features(geometry, results, pdef)
    assert features[1]["state"] == "attention"
    assert len(features[1]["overlays"]) == 2 * len(geometry["linepairs"]["groups"])
    assert features[2]["overlays"] and features[4]["overlays"]
    assert len(features[3]["overlays"]) == len(geometry["uniformity"]["squares"])
    assert all(f["technical_attention"] for f in features[1:])


def test_rejected_circle_grid_keeps_circle_overlays(passing, pdef):
    geometry, results = passing
    geometry["lowcontrast"]["grid_positioning"] = {"accepted": False, "reason": "weak rim evidence"}
    feature = auto.assess_features(geometry, results, pdef)[2]
    assert feature["state"] == "attention" and len(feature["overlays"]) == 1 + len(geometry["lowcontrast"]["circles"])
    assert any(i["detail"] == "weak rim evidence" for i in feature["issues"])


@pytest.mark.parametrize("duplicate,expected,absent", [(False, "repeat_scan", "contact_technician"), (True, "contact_technician", "repeat_scan")])
def test_duplicate_aware_tasks(duplicate, expected, absent, passing, pdef):
    geometry, results = passing
    results["uniformity"]["status"] = "fail"
    features = auto.assess_features(geometry, results, pdef)
    tasks = auto.generate_tasks("a", "2026-10-15", features, duplicate)
    assert expected in [t["type"] for t in tasks] and absent not in [t["type"] for t in tasks]
    description = next(t for t in tasks if t["type"] == expected)["description"]
    assert ("Uniformity" in description) is duplicate
    feature_tasks = [task for task in tasks if task["type"] == "feature_solution"]
    assert len(feature_tasks) == 1 and feature_tasks[0]["related_feature"] == "uniformity"
    assert feature_tasks[0]["solutions"]
    assert len({t["id"] for t in tasks}) == len(tasks)
    assert "analysis_attention" not in [t["type"] for t in tasks]


def test_missing_upload_date_warns_without_inventing_deadline(passing, pdef):
    tasks = auto.generate_tasks("a", None, auto.assess_features(*passing, pdef), False)
    assert len(tasks) == 1 and tasks[0]["type"] == "analysis_attention"
    assert tasks[0]["due_date"] is None


def png(seed=0):
    buf = io.BytesIO()
    Image.fromarray(np.random.default_rng(seed).integers(0, 255, (64, 64), dtype=np.uint8)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def mod(tmp_path, monkeypatch):
    return _build_app(tmp_path, monkeypatch)


@pytest.fixture
def client(mod):
    with TestClient(mod.app) as client:
        client.headers.update({"X-CSRF-Token": _login(client)})
        yield client


@pytest.fixture
def algorithms(monkeypatch, passing):
    geometry, results = passing
    calls = []
    def register(scan, pdef):
        calls.append("register")
        return Registration(Transform(np.array([[1., 0.], [0., -1.]]), np.array([32., 32.])),
                            np.array([[1., 1.], [50., 1.], [50., 50.], [1., 50.]]), 0., {}, [], {}, 0.)
    def propose(ctx):
        calls.append("propose")
        return copy.deepcopy(geometry)
    shared_compute = pipeline.compute_all
    def compute(ctx, positions):
        calls.append("compute")
        return shared_compute(ctx, positions)
    monkeypatch.setattr(pipeline, "run_stage_a", register)
    monkeypatch.setattr(pipeline, "propose_all", propose)
    monkeypatch.setattr(pipeline, "compute_all", compute)
    for name, module in pipeline._MODULES.items():
        monkeypatch.setattr(module, "compute", lambda ctx, positions, name=name: copy.deepcopy(results[name]))
    return calls


def upload(client, data=None, **labels):
    labels.setdefault("site", "Test hospital")
    response = client.post("/api/auto-analyse", files={"file": ("scan.png", data if data is not None else png())}, data=labels)
    assert response.status_code == 200, response.text
    return response.json()["analyses"][0]


def test_upload_runs_pipeline_and_saves_in_history(client, mod, algorithms):
    from phantom_qa.webapp.display import display_range
    result = upload(client, site="Goma", phantom="P1", operator="Operator")
    assert algorithms == ["register", "propose", "compute"]
    assert result["overall"] == "pass" and result["saved"] and not result["duplicate"]
    assert result["display_range"] == display_range(ingest.load_image_bytes(png(), "scan.png").pixels)
    rec = mod.store.get(result["id"])
    assert rec["stage"] == "F" and rec["status"] == "pass" and rec["layout_source"] == "auto"
    assert rec["validation_status"] == "" and not rec["validated_by"]
    assert rec["results"]["_meta"]["workflow"] == "auto_analyse"
    assert rec["results"]["_meta"]["manually_reviewed"] is False
    assert rec["site"] == "Goma" and rec["phantom"] == "P1" and rec["operator"] == "Operator"
    assert mod.store.geometry_state(rec["id"])["has_auto_proposal"]
    assert mod.store.verify_integrity(rec["id"])["status"] == "ok"
    assert any(a["id"] == rec["id"] for a in client.get("/api/analyses").json()["analyses"])
    assert any(a["id"] == rec["id"] for a in client.get("/api/trends").json()["analyses"])
    assert client.get(f"/api/analyses/{rec['id']}/image.png").status_code == 200
    assert not any(e["action"] == "confirmed" for e in rec["audit"])


def test_identical_orange_reupload_preserves_signed_record(client, mod, algorithms, passing):
    passing[1]["uniformity"]["status"] = "fail"
    data = png()
    first = upload(client, data)
    assert "repeat_scan" in [t["type"] for t in first["tasks"]]
    mod.store.update(first["id"], validation_status="validated", validated_by="Reviewer")
    before = mod.store.get(first["id"])
    second = upload(client, data, site="Do not overwrite")
    assert second["id"] == first["id"] and second["duplicate"] and not second["saved"]
    assert second["validation_status"] == "" and second["manually_reviewed"] is False
    assert second["history_validation_status"] == "validated"
    assert "contact_technician" in [t["type"] for t in second["tasks"]]
    assert "repeat_scan" not in [t["type"] for t in second["tasks"]]
    assert len(mod.store.list_all()) == 1
    assert mod.store.get(first["id"]) == before
    assert algorithms == ["register", "propose", "compute"]


def test_new_scan_same_labels_is_independent(client, mod, algorithms, passing):
    passing[1]["uniformity"]["status"] = "fail"
    first = upload(client, png(1), site="Goma", phantom="P1")
    second = upload(client, png(2), site="Goma", phantom="P1")
    assert first["id"] != second["id"] and not second["duplicate"]
    assert "repeat_scan" in [t["type"] for t in second["tasks"]]
    assert "contact_technician" not in [t["type"] for t in second["tasks"]]
    assert len(mod.store.list_all()) == 2


def test_technical_failure_is_saved_with_actual_error(client, mod, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("registration diagnostic")
    monkeypatch.setattr(pipeline, "run_stage_a", fail)
    result = upload(client)
    assert result["overall"] == "attention"
    assert all(f["technical_attention"] and not f["overlays"] for f in result["features"])
    rec = mod.store.get(result["id"])
    assert rec["status"] == "error" and rec["stage"] == "F"
    assert rec["results"]["geometry"]["error"] == "RuntimeError: registration diagnostic"
    assert {t["type"] for t in result["tasks"]} == {"next_scan", "repeat_scan", "feature_solution"}


def test_real_pipeline_rejects_blank_scan_as_attention(client, mod):
    buffer = io.BytesIO()
    Image.fromarray(np.zeros((64, 64), dtype=np.uint8)).save(buffer, format="PNG")
    result = upload(client, buffer.getvalue())
    assert result["overall"] == "attention"
    assert all(f["state"] == "attention" for f in result["features"])
    assert mod.store.get(result["id"])["results"] is not None


def test_incomplete_analysis_never_saved_as_pass(client, mod, algorithms, passing):
    passing[1]["linepairs"].pop("rows")
    result = upload(client)
    feature = next(f for f in result["features"] if f["id"] == "linepairs")
    assert feature["state"] == "attention" and feature["technical_attention"]
    rec = mod.store.get(result["id"])
    assert rec["status"] == "error"
    assert rec["results"]["linepairs"]["measurement_status"] == "pass"


def test_failed_feature_preserves_error_without_hiding_other_passes(client, mod, algorithms, passing):
    passing[1]["wedge"] = {"status": "error", "error": "ValueError: wedge diagnostic"}
    result = upload(client)
    assert [f["id"] for f in result["features"] if f["state"] == "attention"] == ["wedge"]
    assert mod.store.get(result["id"])["results"]["wedge"]["error"] == "ValueError: wedge diagnostic"


def test_malformed_result_is_saved_for_review(client, mod, algorithms, passing):
    passing[1]["uniformity"] = "bad automatic output"
    result = upload(client)
    assert result["overall"] == "attention"
    stored = mod.store.get(result["id"])["results"]["uniformity"]
    assert stored["status"] == "error" and stored["raw_invalid_result"] == "bad automatic output"
    assert client.get("/api/trends").status_code == 200


def test_incomplete_metric_row_stays_compatible_with_history(client, mod, algorithms, passing):
    passing[1]["uniformity"]["rows"][0].pop("snr")
    result = upload(client)
    assert result["overall"] == "attention"
    assert client.get("/api/trends").status_code == 200
    assert client.get(f"/api/analyses/{result['id']}/export.csv").status_code == 200


def test_invalid_upload_leaves_history_empty(client, mod):
    response = client.post("/api/auto-analyse", files={"file": ("bad.dcm", b"not an image")}, data={"site": "Test hospital"})
    assert response.status_code == 400 and not mod.store.list_all()


def test_duplicate_guard_for_analyze_is_unchanged(client, mod, algorithms):
    data = png()
    first = upload(client, data)
    duplicate = client.post("/api/analyses", files={"file": ("scan.png", data)}, data={"site": "Test hospital"})
    assert duplicate.status_code == 409
    assert duplicate.json()["duplicate_of"][0]["id"] == first["id"]
    override = client.post("/api/analyses", files={"file": ("scan.png", data)}, data={"site": "Test hospital", "allow_duplicate": "true"})
    assert override.status_code == 200 and len(mod.store.list_all()) == 2


def test_identical_archive_members_are_saved_once(client, mod, algorithms):
    data = png()
    scan = ingest.load_any_bytes(data, "scan.png")[0]
    # ZIP ingest normally selects DICOM members. Model its decoded member output
    # here to exercise persistence independently of the archive format.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(mod.ingest, "load_any_bytes", lambda *_: [scan, scan])
        response = client.post("/api/auto-analyse", files={"file": ("export.zip", b"container")}, data={"site": "Test hospital"})
    assert response.status_code == 200
    assert len(response.json()["analyses"]) == 1 and len(mod.store.list_all()) == 1
    assert not response.json()["analyses"][0]["duplicate"]
    assert mod.store.verify_integrity(response.json()["analyses"][0]["id"])["status"] == "ok"


def test_atomic_duplicate_lookup_across_store_instances(tmp_path):
    data = png()
    scan = ingest.load_any_bytes(data, "scan.png")[0]
    stores = [Store(str(tmp_path)), Store(str(tmp_path))]
    def create(store):
        return store.new_analysis_once(scan, data, "sig", ALGO_VERSION, "v1")
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(create, stores))
    assert len({aid for aid, _ in outcomes}) == 1
    assert sorted(created for _, created in outcomes) == [False, True]
    assert len(stores[0].list_all()) == 1


def test_deadline_uses_stored_upload_timestamp(client, mod, algorithms):
    first = upload(client)
    mod.store.update(first["id"], created_at="2026-01-31 15:00:00", acquired_at="2000-01-01 01:00:00")
    duplicate = upload(client)
    task = next(t for t in duplicate["tasks"] if t["type"] == "next_scan")
    assert task["due_date"] == "2026-02-28"


def test_auto_page_is_first_and_viewer_has_no_edit_handlers(client):
    page = client.get("/").text
    assert page.index('id="tab-auto-analyse"') < page.index('id="tab-analyze"') < page.index('id="tab-history"')
    source = client.get("/auto-analyse.js").text
    assert "pointerdown" not in source and "mousedown" not in source and "/roi" not in source
    assert "renderTasks" in source and "textContent" in source
    assert ">Auto Analyze</button>" in page
    assert 'id="auto-technical"' not in page and 'id="user-view"' not in page


def test_nested_linepair_acceptance_matches_detector(passing, pdef):
    geometry, results = passing
    for group in geometry["linepairs"]["groups"]:
        group["positioning"] = {"strip": {"accepted": True}, "local": {"method": "directional_pattern_centroid"}}
    feature = auto.assess_features(geometry, results, pdef)[1]
    assert feature["state"] == "pass" and not feature["issues"]
    geometry["linepairs"]["groups"][0]["positioning"]["strip"]["accepted"] = False
    assert auto.assess_features(geometry, results, pdef)[1]["state"] == "attention"


@pytest.mark.parametrize("case", ["pass", "quality_failure", "missing_measurement", "missing_verdict", "malformed_row", "detector_error"])
def test_same_positions_produce_same_verdict_and_errors_in_both_flows(case, client, mod, algorithms, passing):
    geometry, results = passing
    for group in geometry["linepairs"]["groups"]:
        group["positioning"] = {"strip": {"accepted": True}, "local": {"method": "directional_pattern_centroid"}}
    if case == "quality_failure":
        results["linepairs"]["rows"][0]["status"] = "fail"
    elif case == "missing_measurement":
        results["linepairs"]["rows"][0]["linearity"]["pitch_dev_pct"] = None
    elif case == "missing_verdict":
        results["linepairs"]["status"] = None
    elif case == "malformed_row":
        results["linepairs"]["rows"][0] = None
    elif case == "detector_error":
        geometry["linepairs"]["groups"][0]["detected"] = False
    automatic = upload(client)
    manual_flow = client.post(f"/api/analyses/{automatic['id']}/compute", json={"sid_mm": 1000.0})
    assert manual_flow.status_code == 200, manual_flow.text
    for name in pipeline.TESTS:
        assert manual_flow.json()["results"][name] == automatic["results"][name]
    assert manual_flow.json()["overall"] == automatic["status"]
    assert automatic["features"][1]["state"] == ("pass" if case == "pass" else "attention")


def test_manual_position_corrections_are_respected_by_shared_checks(passing, pdef):
    geometry, results = passing
    group = geometry["linepairs"]["groups"][0]
    group["detected"] = False
    group["roi"]["manually_adjusted"] = True
    assert auto.assess_features(geometry, results, pdef)[1]["state"] == "pass"


def test_auto_routes_are_optional(mod):
    from phantom_qa.webapp.auto_analyse import create_router
    isolated = create_router(mod)
    assert {route.path for route in isolated.routes} == {"/api/auto-analyse", "/api/auto-analyse/{aid}"}
