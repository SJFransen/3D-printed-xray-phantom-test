"""Text/media ordering, upload identity and durable feature-specific tasks."""
import copy
import io
from pathlib import Path

import pytest
from pydicom.dataset import Dataset, FileMetaDataset

from phantom_qa import ingest, solutions
from test_auto_analyse import client, mod, algorithms, passing, png
from test_auto_analyse import upload


def dicom_bytes(institution="Example clinic", source="SOURCE_AE"):
    import numpy as np
    import pydicom
    from pydicom.dataset import FileDataset
    from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid
    meta = FileMetaDataset()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.SourceApplicationEntityTitle = source
    ds = FileDataset("", {}, file_meta=meta, preamble=b"\0" * 128)
    ds.SOPClassUID = meta.MediaStorageSOPClassUID
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.InstitutionName = institution
    ds.Rows = ds.Columns = 64
    ds.SamplesPerPixel = 1
    ds.BitsAllocated = ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelData = np.arange(4096, dtype="<u2").tobytes()
    stream = io.BytesIO()
    pydicom.dcmwrite(stream, ds, write_like_original=False)
    return stream.getvalue()


def test_institution_priority_and_file_metadata():
    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.SourceApplicationEntityTitle = "SOURCE_AE"
    ds.InstitutionName = "  Hospital  "
    meta = ingest._extract_dicom_meta(ds)
    assert ingest.institution(meta) == "Hospital"
    ds.InstitutionName = "  "
    assert ingest.institution(ingest._extract_dicom_meta(ds)) == "SOURCE_AE"
    assert ingest.institution({}) == ""


@pytest.mark.parametrize("route", ["/api/auto-analyse", "/api/analyses"])
def test_missing_institution_requires_entry_before_persistence(client, mod, route):
    response = client.post(route, files={"file": ("scan.png", png())})
    assert response.status_code == 422
    assert "institution" in response.json()["detail"]
    assert mod.store.list_all() == []


def test_preview_does_not_create_a_record(client, mod):
    response = client.post("/api/uploads/preview", files={"file": ("scan.png", png())})
    assert response.status_code == 200
    assert response.json()["scans"][0]["institution"] == ""
    assert not mod.store.list_all()


@pytest.mark.parametrize("institution,expected", [("Example clinic", "Example clinic"), ("", "SOURCE_AE")])
def test_real_dicom_preview_and_saved_identity_match(client, mod, algorithms, institution, expected):
    files = {"file": ("scan.dcm", dicom_bytes(institution))}
    preview = client.post("/api/uploads/preview", files=files)
    assert preview.status_code == 200
    assert preview.json()["scans"][0]["institution"] == expected
    assert not mod.store.list_all()
    result = client.post("/api/auto-analyse", files=files)
    assert result.status_code == 200, result.text
    record = result.json()["analyses"][0]
    assert record["site"] == expected
    assert mod.store.get(record["id"])["site"] == expected


def test_archive_uses_each_institution_and_validates_before_writing(client, mod, algorithms, monkeypatch):
    scans = [ingest.load_image_bytes(png(seed), f"scan{seed}.png") for seed in (1, 2)]
    scans[0].meta["InstitutionName"] = "Hospital A"
    scans[1].meta["SourceApplicationEntityTitle"] = "Hospital B"
    monkeypatch.setattr(ingest, "load_any_bytes", lambda *args: scans)
    response = client.post("/api/auto-analyse", files={"file": ("scans.zip", b"zip")})
    assert response.status_code == 200, response.text
    assert {record["site"] for record in response.json()["analyses"]} == {"Hospital A", "Hospital B"}
    before = len(mod.store.list_all())
    scans.append(ingest.load_image_bytes(png(3), "missing.png"))
    response = client.post("/api/auto-analyse", files={"file": ("scans.zip", b"zip")})
    assert response.status_code == 422
    assert len(mod.store.list_all()) == before


def test_steps_sort_numerically_and_keep_text_media_order(tmp_path, monkeypatch):
    monkeypatch.setattr(solutions, "ROOT", tmp_path)
    step = tmp_path / "line-pair" / "step 2"
    step.mkdir(parents=True)
    (step / "explanation.txt").write_text("Before [image 01] between [video] after", encoding="utf-8")
    (step / "01.png").write_bytes(b"image")
    (step / "demo.mp4").write_bytes(b"movie")
    (step / "technician.txt").write_text("Technical details", encoding="utf-8")
    ten = step.parent / "step 10"
    ten.mkdir()
    (ten / "explanation.txt").write_text("Later", encoding="utf-8")
    steps = solutions.load_steps("linepairs")
    assert [item["number"] for item in steps] == [2, 10]
    blocks = steps[0]["content"][0]["blocks"]
    assert [block["type"] for block in blocks] == ["text", "image", "text", "video", "text"]
    assert blocks[0]["text"] == "Before " and blocks[-1]["text"] == " after"
    assert steps[0]["content"][1]["audience"] == "technician"
    assert solutions.load_steps("unknown") == []


def test_missing_media_and_path_escape_are_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(solutions, "ROOT", tmp_path)
    step = tmp_path / "line-pair" / "step 1"
    step.mkdir(parents=True)
    (step / "explanation.txt").write_text("[image missing.png] [video ../../outside.mp4]", encoding="utf-8")
    blocks = solutions.load_steps("linepairs")[0]["content"][0]["blocks"]
    assert all(block["type"] == "text" for block in blocks)
    assert solutions.media_path("../outside.png") is None
    assert solutions.media_path("line-pair/step 1/explanation.txt") is None


def test_solution_media_is_authenticated_and_can_be_read(client, tmp_path, monkeypatch):
    monkeypatch.setattr(solutions, "ROOT", tmp_path)
    step = tmp_path / "line-pair" / "step 1"
    step.mkdir(parents=True)
    (step / "image.png").write_bytes(b"image")
    response = client.get("/api/solutions/line-pair/step%201/image.png")
    assert response.status_code == 200 and response.content == b"image"
    assert client.get("/api/solutions/line-pair/step%201/explanation.txt").status_code == 404
    client.cookies.clear()
    assert client.get("/api/solutions/line-pair/step%201/image.png").status_code == 401


def test_linepair_manual_api_adjusts_individual_boxes(client, mod, algorithms, monkeypatch):
    import numpy as np
    from test_positioning import synthetic_ctx
    from phantom_qa.analysis import linepairs
    ctx, _ = synthetic_ctx()
    record = upload(client)
    geometry = copy.deepcopy(mod.store.get(record["id"])["geometry"])
    geometry["linepairs"] = linepairs.propose(ctx)
    mod.store.set_geometry_baseline(record["id"], geometry)
    monkeypatch.setattr(mod, "_ctx", lambda *args: ctx)
    lp = geometry["linepairs"]
    before = np.array([group["roi"]["center_mm"] for group in lp["groups"]])
    roi = lp["groups"][1]["roi"]
    response = client.post(f"/api/analyses/{record['id']}/roi",
                           json={"roi_id": roi["id"], "center_px": ctx.T.mm_to_px(np.array(roi["center_mm"]) + [2, 3]).tolist()})
    assert response.status_code == 200, response.text
    saved = mod.store.get(record["id"])["geometry"]["linepairs"]
    expected = before.copy()
    expected[1] += [2, 3]
    np.testing.assert_allclose([group["roi"]["center_mm"] for group in saved["groups"]], expected)
    profile = saved["groups"][1]["profile_seg"]
    np.testing.assert_allclose((np.array(profile["p0_mm"]) + profile["p1_mm"]) / 2, expected[1])
    assert mod.store.get(record["id"])["results"] is None
