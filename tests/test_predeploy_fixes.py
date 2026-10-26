"""Regression tests for the pre-deployment review round.

Each test names the defect it pins. They are grouped here rather than spread
across the thematic files because they share one harness: a small editable
analysis whose stored file decodes and whose registration is a plain
transform, so every endpoint can run for real.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile

import numpy as np
import pytest
from fastapi.testclient import TestClient

from conftest import SAMPLES, needs_samples
from phantom_qa import pipeline
from phantom_qa.analysis.common import Ctx, rect_roi
from phantom_qa.phantom_def import load_default
from phantom_qa.registration import Transform
from phantom_qa.store import Store
from test_authorization import ADMIN_PW, _build_app, _login
from test_store_labels import fake_scan, results_for


def _png(seed: int = 0) -> bytes:
    from PIL import Image
    rng = np.random.default_rng(seed)
    arr = rng.integers(0, 255, (24, 24), dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return buf.getvalue()


def make_ctx(n=64):
    rng = np.random.default_rng(5)
    return Ctx(pixels=2000 + rng.normal(0, 20, (n, n)),
               T=Transform(A=np.array([[1.0, 0.0], [0.0, -1.0]]),
                           t=np.array([n / 2.0, n / 2.0])),
               pdef=load_default())


@pytest.fixture()
def mod(tmp_path, monkeypatch):
    return _build_app(tmp_path, monkeypatch)


@pytest.fixture()
def client(mod):
    c = TestClient(mod.app, raise_server_exceptions=False)
    c.headers.update({"X-CSRF-Token": _login(c)})
    return c


@pytest.fixture()
def aid(mod):
    payload = _png(7)
    ctx = make_ctx()
    a = mod.store.new_analysis(
        fake_scan(sha=hashlib.sha256(payload).hexdigest(), name="scan.png"),
        payload, "sig", "1.0.0", "1.0",
        labels={"site": "Goma", "phantom": "MSF-01"})
    mod.store.update(a, reg={"transform": ctx.T.to_dict(),
                             "corners_px": [[0, 0], [1, 0], [1, 1], [0, 1]]})
    mod.store.set_geometry_baseline(a, pipeline.to_jsonable(
        {"uniformity": {"roi_size_mm": 30.0, "squares": [
            {"id": "C", "detected": True,
             "roi": rect_roi(ctx, (0.0, 0.0), (10.0, 10.0), 0.0,
                             "uniformity/C")}]}}))
    return a


# ------------------------------------------------------------- the SID bug

def test_compute_analyses_with_the_sid_it_was_given(client, mod, aid,
                                                    monkeypatch):
    """compute() wrote the submitted SID to the database and then built the
    analysis context from the record it had fetched BEFORE the write, so the
    %-of-SID field verdict was computed with the previous SID while every
    display showed the new one."""
    seen = {}

    def fake_compute_all(ctx, geometry):
        seen["sid"] = (ctx.params or {}).get("sid_mm")
        return results_for()

    monkeypatch.setattr(mod.pipeline, "compute_all", fake_compute_all)
    r = client.post(f"/api/analyses/{aid}/compute", json={"sid_mm": 1234.0})
    assert r.status_code == 200, r.text
    assert seen["sid"] == 1234.0, (
        f"compute ran with SID {seen['sid']}, not the submitted 1234")
    assert mod.store.get(aid)["sid_mm"] == 1234.0


# ------------------------------------------- the block through generic ROI

@pytest.mark.parametrize("path,body", [
    ("roi", {"roi_id": "lowcontrast/block", "center_px": [10.0, 10.0]}),
    ("roi_rotate", {"roi_id": "lowcontrast/block", "angle_deg": 10.0}),
])
def test_the_block_cannot_be_moved_through_the_generic_roi_paths(client, aid,
                                                                 path, body):
    """The eight circles are companions by GRID, not by id prefix, so the
    generic endpoints would turn the outline and leave the circles behind —
    and desync the two stored copies of the block angle."""
    r = client.post(f"/api/analyses/{aid}/{path}", json=body)
    assert r.status_code == 400, r.text
    assert "lowcontrast_block" in r.json()["detail"]


# ------------------------------------------------- corrupted registration

def test_a_corrupted_registration_is_a_conflict_not_a_crash(client, mod, aid):
    mod.store.update(aid, reg={"transform": {"bogus": True},
                               "corners_px": "not-a-matrix"})
    rec = client.get(f"/api/analyses/{aid}")
    assert rec.status_code == 200
    assert rec.json()["registration"] is None

    r = client.post(f"/api/analyses/{aid}/roi",
                    json={"roi_id": "uniformity/C", "center_px": [5.0, 5.0]})
    assert r.status_code == 409, r.text
    assert "Re-register" in r.json()["detail"]


def test_corrupted_geometry_reads_as_no_geometry_not_a_500(client, mod, aid):
    import sqlite3
    con = sqlite3.connect(mod.store.db_path)
    con.execute("UPDATE analyses SET geometry_json='[1,2,{broken' WHERE id=?",
                (aid,))
    con.commit()
    con.close()
    r = client.post(f"/api/analyses/{aid}/roi",
                    json={"roi_id": "uniformity/C", "center_px": [5.0, 5.0]})
    assert r.status_code == 400, r.text
    assert "no geometry" in r.json()["detail"]


# ----------------------------------------------------- delete-race answers

def test_a_record_deleted_mid_request_is_a_404_everywhere(client, mod, aid,
                                                          monkeypatch):
    """The store raises KeyError when the row vanished between the endpoint's
    existence check and its write; that is the loser's 404, not a 500."""
    monkeypatch.setattr(mod.store, "set_labels",
                        lambda *a, **k: (_ for _ in ()).throw(KeyError(aid)))
    r = client.post(f"/api/analyses/{aid}/labels", json={"site": "X"})
    assert r.status_code == 404

    monkeypatch.setattr(mod.store, "set_baseline",
                        lambda *a, **k: (_ for _ in ()).throw(KeyError(aid)))
    mod.store.update(aid, results=results_for(), status="pass")
    r = client.post(f"/api/analyses/{aid}/baseline", json={"baseline": True})
    assert r.status_code == 404


# --------------------------------------------------- JSON null vs SQL NULL

def test_clearing_results_actually_clears_the_completed_flag(mod, aid):
    """update(results=None) stored the TEXT 'null', which passes every
    IS NOT NULL filter — a re-registered analysis still counted as completed
    in trends and exports while holding no numbers at all."""
    mod.store.update(aid, results=results_for(), status="pass")
    assert [r["id"] for r in mod.store.list_all(completed_only=True)] == [aid]
    mod.store.update(aid, results=None)
    assert mod.store.list_all(completed_only=True) == []


# ----------------------------------------------------------- filter typos

@pytest.mark.parametrize("query", ["order=newest", "validation=Validated",
                                   "validation=nonsense"])
def test_an_unknown_filter_value_is_a_400_not_a_wrong_answer(client, query):
    r = client.get(f"/api/analyses?{query}")
    assert r.status_code == 400, f"?{query} -> {r.status_code}"


def test_known_filter_values_still_answer(client):
    for q in ("", "order=uploaded", "validation=pending",
              "validation=validated"):
        assert client.get(f"/api/analyses?{q}").status_code == 200


@pytest.mark.parametrize("path", ["/api/trends", "/api/export.csv",
                                  "/api/comparison_report.html"])
def test_the_exports_reject_filter_typos_too(client, path):
    r = client.get(f"{path}?validation=Validated")
    assert r.status_code == 400, f"{path}: {r.status_code}"


# ----------------------------------------- layout_source follows undo/redo

def test_undo_across_the_profile_boundary_corrects_the_provenance(client, mod,
                                                                  aid):
    """Undo can step from a profile-applied state back to pure detection; the
    layout_source column (and the Stage C banner it drives) must follow."""
    auto = mod.store.get(aid)["geometry"]
    applied = json.loads(json.dumps(auto))
    applied["uniformity"]["squares"][0]["roi"]["from_profile"] = True
    mod.store.replace_geometry(aid, applied, action="apply_profile")
    mod.store.update(aid, layout_source="profile")

    r = client.post(f"/api/analyses/{aid}/geometry/undo", json={})
    assert r.status_code == 200, r.text
    assert r.json()["layout_source"] == "auto"
    assert mod.store.get(aid)["layout_source"] == "auto"

    r = client.post(f"/api/analyses/{aid}/geometry/redo", json={})
    assert r.json()["layout_source"] == "profile"
    assert mod.store.get(aid)["layout_source"] == "profile"


# ------------------------------------------------- reanalyze by explicit id

def test_naming_an_id_does_not_bypass_the_signed_off_skip(mod, aid):
    """The module promises signed-off analyses are skipped unless explicitly
    included; the explicit-id path used to recompute them silently."""
    from phantom_qa.reanalyze import reanalyze_one
    pdef = load_default()
    mod.store.update(aid, results=results_for(), status="pass")
    mod.store.set_validation(aid, "validated", "Dr A")

    out = reanalyze_one(mod.store, pdef, aid, mode="results")
    assert out["status"] == "skipped_validated"
    assert "include-validated" in out["message"]
    assert mod.store.get(aid)["results"] is not None


# --------------------------------------------------------- naming parity

def test_profile_listing_carries_both_label_spellings(mod, aid):
    """The forget endpoint's body key is 'phantom'; the listing said only
    'phantom_key', so a script wiring one into the other sent '' and got a
    400 for every row."""
    mod.store.save_phantom_profile("MSF-01", {"rois": {}})
    row = mod.store.list_phantom_profiles()[0]
    assert row["phantom_key"] == "MSF-01"
    assert row["phantom"] == "MSF-01"


def test_wide_csv_spells_the_ruling_like_the_long_csv(mod, aid):
    from phantom_qa.store import wide_csv_export
    mod.store.update(aid, results=results_for(), status="pass")
    mod.store.set_validation(aid, "conditionally_validated", "Dr A")
    text = wide_csv_export([mod.store.get(aid)])
    assert "# validation," in text
    assert "conditionally validated" in text          # the human label
    assert "# validation_status" in text              # the raw state stays


def test_the_report_header_and_identity_block_agree_on_uploaded(mod, aid):
    from phantom_qa.report import build_report
    mod.store.update(aid, results=results_for(), status="pass")
    html = build_report(mod.store.get(aid))
    assert "<b>Uploaded</b>" in html
    assert "<b>Created</b>" not in html


# ------------------------------------------------------------- slim fetch

def test_the_slim_fetch_agrees_with_the_full_record(mod, aid):
    """get_slim() is what trends and every export now read; it must never
    disagree with get() about the fields it carries."""
    mod.store.update(aid, results=results_for(), status="pass")
    full = mod.store.get(aid)
    slim = mod.store.get_slim([aid])[0]
    assert slim["results"] == full["results"]
    for k in ("id", "created_at", "acquired_at", "site", "phantom",
              "signature", "status", "is_baseline", "source_name",
              "validation_status", "sid_mm"):
        assert slim[k] == full[k], k
    # the heavy blobs are deliberately absent, shaped like corruption-degrade
    for k in ("meta", "reg", "geometry", "audit"):
        assert slim[k] is None


def test_the_slim_fetch_preserves_request_order(mod):
    ids = []
    for seed in range(5):
        payload = _png(20 + seed)
        a = mod.store.new_analysis(
            fake_scan(sha=hashlib.sha256(payload).hexdigest()), payload,
            "sig", "1.0.0", "1.0", labels={})
        mod.store.update(a, results=results_for(), status="pass")
        ids.append(a)
    wanted = list(reversed(ids))
    got = [r["id"] for r in mod.store.get_slim(wanted)]
    assert got == wanted


# ---------------------------------------------------------------- zip flow

@needs_samples
def test_a_zip_upload_stores_the_member_the_hash_describes(client, mod):
    """The recorded sha256 is the extracted member's. Storing the CONTAINER
    made every integrity check on a CD-export upload report a mismatch, and
    re-uploading the extracted file alone was never seen as a duplicate."""
    with open(SAMPLES[0], "rb") as f:
        member = f.read()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("EXPORT/IMG0001", member)
    r = client.post("/api/analyses",
                    files={"file": ("cd_export.zip", io.BytesIO(buf.getvalue()))},
                    data={"site": "Goma", "phantom": "MSF-01"})
    assert r.status_code == 200, r.text
    a = r.json()["analyses"][0]["id"]

    verify = client.get(f"/api/analyses/{a}/verify").json()
    assert verify["status"] == "ok", (
        "a freshly uploaded CD export must verify clean — the stored bytes "
        "did not match the recorded hash")

    # the same scan arriving OUTSIDE the container is the same scan
    again = client.post("/api/analyses",
                        files={"file": ("scan.dcm", io.BytesIO(member))}, data={"site": "Test hospital"})
    assert again.status_code == 409
    assert again.json()["duplicate_of"][0]["id"] == a
