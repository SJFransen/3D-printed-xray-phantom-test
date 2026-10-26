"""Shared defaults, real field-alignment recomputation and durable report revisions."""
from __future__ import annotations

import copy

import numpy as np
import pytest

from phantom_qa import pipeline, layout_profile
from phantom_qa.analysis.geometry import compute as compute_geometry
from phantom_qa.registration import Registration, Transform
from phantom_qa.store import Store
from test_auto_analyse import algorithms, client, mod, passing, png, upload


@pytest.fixture
def alignment_case(algorithms, passing, pdef, monkeypatch):
    geometry = passing[0]["geometry"]
    half = pdef.side_mm / 2
    for key, mm in zip(("TL", "TR", "BR", "BL"),
                       ((-half, half), (half, half), (half, -half), (-half, -half))):
        geometry["corners"][key]["mm"] = list(mm)
    for side, ruler in geometry["rulers"].items():
        definition = pdef.rulers[side]
        ruler["line_offsets_mm"] = [definition["first_line_from_edge_mm"] + n * definition["pitch_mm"] for n in range(13)]
        geometry["field_edges"][side]["offset_from_edge_mm"] = -ruler["line_offsets_mm"][definition["central_long_index"]]
    original_register = pipeline.run_stage_a
    transform = Transform(np.array([[.16, 0.], [0., -.16]]), np.array([32., 32.]))
    def register(scan, definition, **kwargs):
        registration = original_register(scan, definition)
        registration.transform = transform
        return registration
    monkeypatch.setattr(pipeline, "run_stage_a", register)
    monkeypatch.setattr(pipeline._MODULES["geometry"], "compute", compute_geometry)
    return transform


def test_corrected_alignment_and_reports_survive_reopening(client, mod, alignment_case, pdef):
    original = upload(client, site="Goma", phantom="P1", operator="Operator", notes="Initial scan")
    aid = original["id"]
    assert original["results"]["geometry"]["field_status"] == "pass"
    first = next(r for r in mod.store.revisions(aid) if r["has_report"])
    original_report = mod.store.revision(aid, first["revision"])["report_html"]
    point = alignment_case.mm_to_px([0., pdef.side_mm / 2 + 40]).tolist()
    edited = client.post(f"/api/analyses/{aid}/field_edge", json={"side": "top", "point_px": point})
    assert edited.status_code == 200, edited.text
    assert mod.store.get(aid)["results"] is None
    preview = client.post(f"/api/analyses/{aid}/compute_preview", json={"tests": ["geometry"], "sid_mm": 1000}).json()
    updated = client.post(f"/api/analyses/{aid}/compute", json={"sid_mm": 1000})
    assert updated.status_code == 200, updated.text
    field = updated.json()["results"]["geometry"]["field_alignment"]["top"]
    assert field["status"] == "fail"
    assert field["offset_from_phantom_edge_mm"] == pytest.approx(40)
    assert field == preview["geometry"]["field_alignment"]["top"]
    finalized = client.post(f"/api/analyses/{aid}/finalize", json={}).json()
    assert not finalized["report_error"]
    reopened = Store(mod.store.root)
    final = reopened.revision(aid, finalized["revision"])
    assert final["record"]["results"]["geometry"]["field_alignment"]["top"] == field
    assert final["report_html"] != original_report
    assert reopened.revision(aid, first["revision"])["report_html"] == original_report
    assert final["record"]["site"] == "Goma" and final["user"] == "qa"
    assert {event["stage"] for event in final["record"]["audit"]} >= {"A", "B", "C", "D", "E", "F"}
    assert reopened.verify_integrity(aid)["status"] == "ok"
    assert len(reopened.list_all()) == 1
    assert client.get(f"/api/analyses/{aid}/report.html?revision={first['revision']}").text == original_report
    saved = client.get(f"/api/auto-analyse/{aid}").json()
    assert saved["results"] == final["record"]["results"]
    assert saved["site"] == "Goma" and saved["features"][0]["state"] == "attention"


def test_compute_cannot_commit_over_a_newer_position(client, mod, alignment_case, monkeypatch):
    scan = upload(client)
    real_compute = pipeline.compute_all
    def racing_compute(ctx, geometry):
        results = real_compute(ctx, geometry)
        mod.store.mutate_geometry(scan["id"], lambda value: value["geometry"]["field_edges"]["top"].update(
            offset_from_edge_mm=50.), action="concurrent field correction", user="colleague")
        return results
    monkeypatch.setattr(pipeline, "compute_all", racing_compute)
    response = client.post(f"/api/analyses/{scan['id']}/compute", json={"sid_mm": 1000})
    assert response.status_code == 409
    assert mod.store.get(scan["id"])["results"] is None
    assert any(row["has_report"] for row in mod.store.revisions(scan["id"]))


@pytest.mark.parametrize("turn,mirrored", [(turn, mirrored) for turn in range(4) for mirrored in (False, True)])
def test_field_corrections_follow_every_side_and_orientation(client, alignment_case, pdef, turn, mirrored):
    angle = turn * np.pi / 2
    rotation = np.array([[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]])
    alignment_case.A[:] = .16 * rotation @ np.diag([-1. if mirrored else 1., -1.])
    scan = upload(client)
    for side, normal in {"top": [0, 1], "right": [1, 0], "bottom": [0, -1], "left": [-1, 0]}.items():
        point = alignment_case.mm_to_px(np.asarray(normal) * (pdef.side_mm / 2 + 30)).tolist()
        response = client.post(f"/api/analyses/{scan['id']}/field_edge", json={"side": side, "point_px": point})
        assert response.status_code == 200, response.text
        preview = client.post(f"/api/analyses/{scan['id']}/compute_preview", json={"tests": ["geometry"], "sid_mm": 1000})
        field = preview.json()["geometry"]["field_alignment"][side]
        assert field["offset_from_phantom_edge_mm"] == pytest.approx(30)
        assert field["status"] == "fail"


def test_duplicate_can_supply_a_missing_site_without_reanalysing(client, mod, algorithms):
    first = upload(client)
    mod.store.update(first["id"], site="")  # a legacy record from before required identity
    second = upload(client, site=" Goma ")
    assert first["id"] == second["id"] and second["site"] == "Goma"
    assert algorithms == ["register", "propose", "compute"]
    assert client.get(f"/api/analyses/{first['id']}").json()["site"] == "Goma"
    assert any(event["action"] == "missing identification supplied on re-upload" for event in mod.store.get(first["id"])["audit"])
    assert upload(client, site="Different site")["site"] == "Goma"


def test_default_saved_layout_matches_analyze(client, mod, algorithms, passing, pdef):
    # A real orientation anchor permits the existing layout-agreement gate.
    block = passing[0]["lowcontrast"]["block"] = copy.deepcopy(passing[0]["lowcontrast"]["block"])
    block.update(id="lowcontrast/block", center_mm=[0., 0.], size_mm=[30., 30.], angle_deg=0.)
    square = passing[0]["uniformity"]["squares"][0]
    square["roi"] = copy.deepcopy(square["roi"])
    square["roi"].update(id=f"uniformity/{square['id']}", center_mm=[0., 0.], size_mm=[30., 30.], angle_deg=0.)
    layout = layout_profile.extract_layout(passing[0])
    layout["rois"][square["roi"]["id"]]["center_mm"] = [3., 4.]
    mod.store.save_phantom_profile("P1", layout, pdef_version=pdef.version, algo_version="test", source_analysis_id="source", updated_by="Technician")
    automatic = upload(client, png(1), phantom="P1")
    manual = client.post("/api/analyses", files={"file": ("new.png", png(2))}, data={"site": "Test hospital", "phantom": "P1"}).json()["analyses"][0]
    proposed = client.post(f"/api/analyses/{manual['id']}/propose", json={})
    assert proposed.status_code == 200, proposed.text
    assert proposed.json()["profile_applied"]
    auto_record = mod.store.get(automatic["id"])
    assert auto_record["layout_source"] == "profile"
    assert auto_record["geometry"] == proposed.json()["geometry"]
    assert auto_record["geometry"]["uniformity"]["squares"][0]["roi"]["center_mm"] == [3., 4.]
    assert mod.store.get_phantom_profile("P1")["updated_by"] == "Technician"


def test_incomplete_results_still_have_a_saved_report(client, mod, algorithms, passing):
    passing[1]["wedge"]["fit"]["r2"] = None
    result = upload(client)
    assert result["features"][4]["technical_attention"]
    reports = [mod.store.revision(result["id"], row["revision"])
               for row in mod.store.revisions(result["id"]) if row["has_report"]]
    assert reports and "missing or invalid" in reports[0]["report_html"]
    assert reports[0]["record"]["results"]["wedge"]["fit"]["r2"] is None


def test_technician_instruction_names_alignment_and_multiple_tests(passing, pdef):
    from phantom_qa import auto_analyse
    passing[1]["geometry"]["field_status"] = "fail"
    passing[1]["geometry"]["field_alignment"]["top"]["status"] = "fail"
    passing[1]["uniformity"]["status"] = "fail"
    features = auto_analyse.assess_features(*passing, pdef)
    tasks = auto_analyse.generate_tasks("scan", "2026-10-21", features, True)
    contact = next(task for task in tasks if task["type"] == "contact_technician")
    assert contact["description"] == "Contact the MSF technician about X-ray field alignment and Uniformity."
