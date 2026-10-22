"""Behavioral regressions from the functional audit; all data is synthetic."""
import asyncio
import copy
import hashlib
import io
import json
import runpy
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from phantom_qa import ingest, pipeline, reanalyze
from phantom_qa.analysis.assessment import evaluate_results
from phantom_qa.analysis.common import Ctx, rect_roi
from phantom_qa.features import rect_mask
from phantom_qa.registration import Registration, Transform
from phantom_qa.report import build_report
from phantom_qa.store import AnalysisConflict, Store
from test_auto_analyse import passing
from test_predeploy_fixes import aid, client, make_ctx, mod
from test_store_labels import results_for


def registration(ctx):
    return Registration(transform=ctx.T, corners_px=np.array([[0, 0], [20, 0], [20, 20], [0, 20]]),
                        coarse_angle_deg=0., score={})


def test_registration_succeeds_and_invalidates_old_geometry(client, mod, aid, monkeypatch):
    mod.store.update(aid, results=results_for(), status="pass")
    monkeypatch.setattr(pipeline, "run_stage_a", lambda *a, **kw: registration(make_ctx()))
    response = client.post(f"/api/analyses/{aid}/register", json={"corners_px": [[0, 0], [20, 0], [20, 20], [0, 20]]})
    assert response.status_code == 200, response.text
    record = mod.store.get(aid)
    assert record["geometry"] is None and record["results"] is None
    assert mod.store.geometry_state(aid)["undo_depth"] == 0
    assert record["audit"][-1]["action"] == "re-registered"


def test_signoff_during_registration_leaves_the_record_intact(client, mod, aid, monkeypatch):
    before = mod.store.get(aid)
    def detect(*args, **kwargs):
        mod.store.set_validation(aid, "validated", "Concurrent approver")
        return registration(make_ctx())
    monkeypatch.setattr(pipeline, "run_stage_a", detect)
    response = client.post(f"/api/analyses/{aid}/register", json={})
    assert response.status_code == 409
    assert mod.store.get(aid)["geometry"] == before["geometry"]
    assert mod.store.get(aid)["reg"] == before["reg"]


def test_signoff_between_context_and_roi_write_is_protected(client, mod, aid, monkeypatch):
    mod.store.update(aid, results=results_for(), status="pass")
    before = mod.store.get(aid)
    original = mod._ctx
    def context(*args, **kwargs):
        result = original(*args, **kwargs)
        mod.store.set_validation(aid, "validated", "Concurrent approver")
        return result
    monkeypatch.setattr(mod, "_ctx", context)
    response = client.post(f"/api/analyses/{aid}/roi", json={"roi_id": "uniformity/C", "center_px": [30, 30]})
    assert response.status_code == 409
    after = mod.store.get(aid)
    assert after["geometry"] == before["geometry"] and after["results"] == before["results"]
    assert after["validation_status"] == "validated"


def test_registration_changed_before_roi_commit_is_a_conflict(client, mod, aid, monkeypatch):
    original = mod._ctx
    fresh = make_ctx()
    fresh.T.t = np.array([40., 40.])
    replacement = {"uniformity": {"squares": [{"id": "C", "detected": True,
                   "roi": rect_roi(fresh, [0, 0], [10, 10], roi_id="uniformity/C")}]}}
    def context(*args, **kwargs):
        result = original(*args, **kwargs)
        mod.store.save_registration(aid, mod.store.get(aid), pipeline.registration_to_dict(registration(fresh)))
        mod.store.set_geometry_baseline(aid, replacement)
        return result
    monkeypatch.setattr(mod, "_ctx", context)
    response = client.post(f"/api/analyses/{aid}/roi", json={"roi_id": "uniformity/C", "center_px": [30, 30]})
    assert response.status_code == 409
    assert mod.store.get(aid)["geometry"] == replacement


@pytest.mark.parametrize("operation", ["baseline", "replace", "undo", "redo"])
def test_store_geometry_operations_check_signoff(mod, aid, operation):
    geometry = mod.store.get(aid)["geometry"]
    mod.store.set_validation(aid, "validated", "Approver")
    with pytest.raises(AnalysisConflict):
        if operation == "baseline":
            mod.store.set_geometry_baseline(aid, geometry)
        elif operation == "replace":
            mod.store.replace_geometry(aid, geometry, action="reset")
        elif operation == "undo":
            mod.store.undo_geometry(aid)
        else:
            mod.store.redo_geometry(aid)


@pytest.mark.parametrize("mode", ["results", "full"])
@pytest.mark.parametrize("change", ["geometry", "signoff"])
def test_cli_reanalysis_does_not_overwrite_new_inputs(mod, aid, monkeypatch, mode, change):
    mod.store.update(aid, results=results_for(), status="pass")
    old = mod.store.get(aid)
    monkeypatch.setattr(pipeline, "run_stage_a", lambda *a, **kw: registration(make_ctx()))
    monkeypatch.setattr(pipeline, "propose_all", lambda *a: copy.deepcopy(old["geometry"]))
    def compute(*args):
        if change == "geometry":
            mod.store.mutate_geometry(aid, lambda value: value.update(concurrent=True), action="edit")
        else:
            mod.store.set_validation(aid, "validated", "Approver")
        return results_for(sd=999.)
    monkeypatch.setattr(pipeline, "compute_all", compute)
    result = reanalyze.reanalyze_one(mod.store, mod.pdef, aid, mode=mode)
    assert result["status"] == "conflict"
    current = mod.store.get(aid)
    if change == "geometry":
        assert current["geometry"]["concurrent"] and current["results"] is None
    else:
        assert current["results"] == old["results"]


@pytest.mark.parametrize("endpoint", ["compute", "compute_preview"])
@pytest.mark.parametrize("sid", [0, -1, float("inf"), float("-inf"), float("nan")])
def test_invalid_sid_is_rejected_without_changing_storage(client, mod, aid, endpoint, sid):
    before = mod.store.get(aid)
    response = client.post(f"/api/analyses/{aid}/{endpoint}", content=json.dumps({"sid_mm": sid}),
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 422, response.text
    assert mod.store.get(aid) == before


def test_cached_image_is_not_served_after_another_worker_deletes(client, mod, aid):
    assert client.get(f"/api/analyses/{aid}/image.png").status_code == 200
    mod.store.delete(aid)  # No local _forget(): simulates a second worker.
    assert client.get(f"/api/analyses/{aid}/image.png").status_code == 404
    assert not any(key[0] == aid for key in mod._img_cache)


@pytest.mark.parametrize("sid", [0, -1, float("inf")])
def test_reanalysis_rejects_legacy_invalid_sid(mod, aid, sid):
    mod.store.update(aid, sid_mm=sid)
    assert reanalyze.reanalyze_one(mod.store, mod.pdef, aid)["status"] == "invalid_parameters"


def test_decoded_cache_obeys_byte_budget_and_releases_sources(mod, monkeypatch):
    monkeypatch.setattr(mod, "SCAN_CACHE_BYTES", 1024)
    mod._scans.clear()
    for index in range(4):
        scan = SimpleNamespace(pixels=np.zeros((8, 8)), source_bytes=b"original")
        mod._cache_scan(str(index), scan)
        assert scan.source_bytes is None
    assert list(mod._scans) == ["2", "3"]
    mod._cache_scan("oversize", SimpleNamespace(pixels=np.zeros((32, 32)), source_bytes=b"source"))
    assert "oversize" not in mod._scans


def dicom_bytes(**metadata):
    from pydicom.dataset import FileDataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, generate_uid
    meta = FileMetaDataset()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.MediaStorageSOPClassUID = generate_uid()
    meta.MediaStorageSOPInstanceUID = generate_uid()
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.Rows = ds.Columns = 64
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.PixelData = np.arange(4096, dtype=np.uint16).tobytes()
    for key, value in metadata.items():
        setattr(ds, key, value)
    output = io.BytesIO()
    ds.save_as(output, write_like_original=False)
    return output.getvalue()


@pytest.mark.parametrize("tag", ["AcquisitionDate", "SeriesDate", "ContentDate", "InstanceCreationDate"])
def test_ingestion_preserves_fallback_dates(mod, tag):
    data = dicom_bytes(**{tag: "20260727", "AcquisitionTime": "103000"})
    scan = ingest.load_dicom_bytes(data, "scan.dcm")
    aid = mod.store.new_analysis(scan, data, "sig", "1", "1")
    assert mod.store.get(aid)["acquired_at"] == "2026-07-27 10:30:00"


def test_archive_with_duplicate_members_creates_one_analysis(client, mod, monkeypatch):
    data = dicom_bytes()
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("first.dcm", data)
        stream.writestr("second.dcm", data)
    monkeypatch.setattr(pipeline, "run_stage_a", lambda *a, **kw: registration(make_ctx()))
    response = client.post("/api/analyses", files={"file": ("scans.zip", archive.getvalue())})
    assert response.status_code == 200, response.text
    assert len(response.json()["analyses"]) == 1
    assert len(mod.store.find_by_sha256(hashlib.sha256(data).hexdigest())) == 1


def test_simultaneous_identical_uploads_need_duplicate_approval(client, mod, monkeypatch):
    data = dicom_bytes()
    monkeypatch.setattr(pipeline, "run_stage_a", lambda *a, **kw: registration(make_ctx()))
    gate = threading.Barrier(4)
    def upload(_):
        gate.wait(timeout=5)
        return client.post("/api/analyses", files={"file": ("scan.dcm", data)}).status_code
    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(upload, range(4)))
    assert sorted(statuses) == [200, 409, 409, 409]
    assert len(mod.store.find_by_sha256(hashlib.sha256(data).hexdigest())) == 1


def test_machine_identity_separates_new_signatures_without_rewriting_history(mod, aid):
    common = {"ManufacturerModelName": "Same model", "KVP": 70, "PixelSpacing": [".1", ".1"]}
    old = mod.store.get(aid)["signature"]
    first = ingest.protocol_signature({**common, "DeviceSerialNumber": "machine-A"})
    second = ingest.protocol_signature({**common, "DeviceSerialNumber": "machine-B"})
    assert first != second and first != ingest.protocol_signature(common)
    assert Store(mod.store.root).get(aid)["signature"] == old


def test_geometry_errors_are_visible_in_report(passing, pdef):
    geometry, results = passing
    results["geometry"]["scale"]["reliable"] = False
    checked, _ = evaluate_results(geometry, results, pdef)
    report = build_report({"id": "scan", "created_at": "2026-10-21", "source_name": "synthetic.dcm", "results": checked})
    summary = report.split('<div class="summary">')[1].split('</div>')[0]
    assert ">error</span>" in summary
    assert "The measurement scale is unreliable" in report


def test_direct_launcher_uses_explicit_proxy_allow_list(mod, monkeypatch):
    import uvicorn
    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: seen.update(kw))
    monkeypatch.setenv("PHANTOMQA_FORWARDED_ALLOW_IPS", "127.0.0.1,10.0.0.2")
    monkeypatch.setattr(mod.cfg, "behind_proxy", True)
    monkeypatch.setattr("sys.argv", ["run_app.py"])
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "run_app.py"), run_name="__main__")
    assert seen["forwarded_allow_ips"] == "127.0.0.1,10.0.0.2"


def test_upload_decode_keeps_the_event_loop_responsive(mod, monkeypatch):
    from starlette.datastructures import UploadFile
    from starlette.requests import Request
    started, release = threading.Event(), threading.Event()
    def decode(*args):
        started.set()
        assert release.wait(timeout=2), "decoder blocked the event loop"
        return []
    monkeypatch.setattr(ingest, "load_any_bytes", decode)
    monkeypatch.setattr(mod, "_current_user", lambda request: "test")
    async def run():
        request = Request({"type": "http", "headers": [], "client": ("test", 0)})
        task = asyncio.create_task(mod._read_upload(request, UploadFile(filename="scan", file=io.BytesIO(b"data"))))
        assert await asyncio.to_thread(started.wait, 1)
        release.set()
        await task
    asyncio.run(run())


@pytest.mark.parametrize("angle", [0, 90, 180])
@pytest.mark.parametrize("offset", [0, .25, .5, .999999])
def test_integral_rectangle_sampling_is_stable(angle, offset):
    assert rect_mask((140, 140), (70 + offset, 70 + offset), (69.99999999999999, 70), angle).sum() == 4900
