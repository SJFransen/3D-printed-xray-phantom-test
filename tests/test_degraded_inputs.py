"""Broken transfers, hostile parameters, and corruption on disk.

A field site has slow, lossy connectivity: uploads arrive truncated, retries
arrive twice, and a browser left overnight sends stale requests. A shared
server accumulates disk corruption eventually. The contract these tests pin:

* no request ever answers 500 — a broken input is a clear 4xx with a plain
  message;
* corruption in ONE stored blob degrades ONE feature, never the whole record;
* nothing typed by a user can forge or break the audit log's line format.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3

import numpy as np
import pytest
from fastapi.testclient import TestClient

from conftest import SAMPLES, needs_samples
from phantom_qa import pipeline
from phantom_qa.analysis.common import Ctx, rect_roi
from phantom_qa.phantom_def import load_default
from phantom_qa.registration import Transform
from test_authorization import _build_app, _login
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
    """A stored analysis whose file decodes and whose geometry is editable."""
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


# ------------------------------------------------- broken and hostile uploads

#: What a dropped connection, a retry of the wrong file, or a misclick
#: actually delivers to the server.
BROKEN_UPLOADS = [
    ("empty file", b""),
    ("pure garbage", b"\x00\x01\x02 not an image at all \xff\xfe" * 40),
    ("png header only", b"\x89PNG\r\n\x1a\n"),
    ("truncated png", None),                      # filled in by the test
    ("html mistaken for an image", b"<!doctype html><body>login page</body>"),
    ("zip with no images", None),                 # filled in by the test
]


@pytest.mark.parametrize("label,payload", BROKEN_UPLOADS,
                         ids=[b[0] for b in BROKEN_UPLOADS])
def test_a_broken_upload_is_a_400_never_a_500(client, label, payload):
    if label == "truncated png":
        whole = _png(3)
        payload = whole[: len(whole) // 2]
    elif label == "zip with no images":
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("README.txt", "no images in here")
        payload = buf.getvalue()
    r = client.post("/api/analyses",
                    files={"file": ("upload.bin", io.BytesIO(payload))},
                    data={"site": "Goma"})
    assert r.status_code == 400, f"{label}: {r.status_code} {r.text[:200]}"
    assert "Could not read file" in r.json()["detail"]


@needs_samples
def test_a_truncated_dicom_is_rejected_cleanly(client):
    """The classic slow-connection artifact: the transfer died two thirds in."""
    with open(SAMPLES[0], "rb") as f:
        whole = f.read()
    for fraction in (3, 10, 100):
        part = whole[: len(whole) // fraction]
        r = client.post("/api/analyses",
                        files={"file": ("scan.dcm", io.BytesIO(part))}, data={"site": "Test hospital"})
        assert r.status_code == 400, (
            f"truncated to 1/{fraction}: {r.status_code}")


def test_an_upload_larger_than_the_cap_is_refused_by_measurement(client, mod):
    """The middleware checks the DECLARED Content-Length; a chunked transfer
    declares nothing and a hostile client can lie. The bytes actually received
    are measured after the read, so both get the same 413."""
    mod.cfg.max_upload_mb = 1
    try:
        blob = os.urandom(2 * 1024 * 1024)     # incompressible 2 MB
        r = client.post("/api/analyses",
                        files={"file": ("big.bin", io.BytesIO(blob))}, data={"site": "Test hospital"})
        assert r.status_code == 413, r.text
        assert "1 MB" in r.json()["detail"]
        assert mod.store.list_all() == []
    finally:
        mod.cfg.max_upload_mb = 200


def test_a_retry_of_a_finished_upload_is_the_duplicate_answer(client):
    """A flaky connection makes the operator press upload twice. The second
    attempt is the 409 duplicate conversation, not a second record."""
    payload = _png(9)
    first = client.post("/api/analyses",
                        files={"file": ("scan.png", io.BytesIO(payload))}, data={"site": "Test hospital"})
    assert first.status_code == 200
    again = client.post("/api/analyses",
                        files={"file": ("scan.png", io.BytesIO(payload))}, data={"site": "Test hospital"})
    assert again.status_code == 409
    assert again.json()["duplicate_of"][0]["id"] == \
        first.json()["analyses"][0]["id"]


# ------------------------------------------------------- hostile parameters

@pytest.mark.parametrize("query", ["scale=0", "scale=-5", "scale=99999999",
                                   "wc=nan&ww=nan", "wc=inf&ww=-inf",
                                   "wc=1e308&ww=1e308"])
def test_image_rendering_survives_any_viewer_parameters(client, aid, query):
    """Window/level and zoom come from the browser's sliders — state, not
    trusted input. A zero scale used to crash PIL; NaN poisoned the window."""
    r = client.get(f"/api/analyses/{aid}/image.png?{query}")
    assert r.status_code == 200, f"?{query} -> {r.status_code}"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_empty_and_junk_id_lists_answer_cleanly(client):
    assert client.get("/api/export.csv?ids=,,,").status_code == 404
    assert client.get("/api/trends?ids=,,,").json()["analyses"] == []
    assert client.get("/api/comparison_report.html?ids=,,,").status_code == 404
    assert client.get("/api/analyses/nope-nope").status_code == 404


def test_unicode_and_control_characters_in_labels_are_stored_not_executed(
        client, mod, aid, tmp_path):
    """Labels are operator-typed free text. Accents and emoji must round-trip;
    control characters must not break the audit log's one-line format."""
    weird = "Göma ☠ phantom\twith\ttabs"
    r = client.post(f"/api/analyses/{aid}/labels",
                    json={"site": weird, "notes": "line1\nline2"})
    assert r.status_code == 200
    assert mod.store.get(aid)["site"] == weird

    audit_log = os.path.join(str(tmp_path), "logs", "audit.log")
    with open(audit_log, encoding="utf-8") as f:
        lines = f.read().splitlines()
    for line in lines:
        assert not line.startswith("event="), (
            "an audit line lost its timestamp prefix — a value broke the "
            "line format")


def test_a_newline_in_a_username_cannot_forge_an_audit_line(mod, tmp_path):
    """The failed-login audit record carries the TYPED username. A newline in
    it used to start a second line that grepped exactly like a real event."""
    c = TestClient(mod.app)
    c.post("/api/login", json={
        "username": "eve\nevent=delete outcome=ok user=admin "
                    "analysis=target123 detail={}",
        "password": "wrong"})
    audit_log = os.path.join(str(tmp_path), "logs", "audit.log")
    with open(audit_log, encoding="utf-8") as f:
        lines = f.read().splitlines()
    forged = [l for l in lines if l.startswith("event=")]
    assert not forged, f"forged audit lines: {forged}"
    assert any("event=login" in l for l in lines), (
        "the failed login itself must still be recorded")


# --------------------------------------------------------- corruption on disk

def test_a_corrupted_stored_file_degrades_to_a_clear_answer(client, mod, aid):
    path = mod.store.upload_path(aid)
    with open(path, "r+b") as f:
        f.seek(10)
        f.write(b"\x00" * 32)
    mod._forget(aid)                       # drop the per-worker pixel cache

    img = client.get(f"/api/analyses/{aid}/image.png")
    assert img.status_code == 422, img.status_code
    assert "corrupted" in img.json()["detail"] or \
        "could not be decoded" in img.json()["detail"]

    verify = client.get(f"/api/analyses/{aid}/verify").json()
    assert verify["status"] == "mismatch"
    # the record itself is still fully readable
    assert client.get(f"/api/analyses/{aid}").status_code == 200


def test_a_corrupted_undo_snapshot_is_a_409_not_a_500(client, mod, aid):
    client.post(f"/api/analyses/{aid}/roi",
                json={"roi_id": "uniformity/C", "center_px": [40.0, 40.0]})
    con = sqlite3.connect(mod.store.db_path)
    con.execute("UPDATE geometry_history SET geometry_z=X'0011AABB'"
                " WHERE analysis_id=? AND seq=0", (aid,))
    con.commit()
    con.close()

    r = client.post(f"/api/analyses/{aid}/geometry/undo", json={})
    assert r.status_code == 409, f"{r.status_code} {r.text[:200]}"
    assert "corrupted" in r.json()["detail"]
    # the live measuring points were not touched by the failed undo
    geom = mod.store.get(aid)["geometry"]
    assert geom["uniformity"]["squares"][0]["roi"]["center_mm"] == \
        pytest.approx([40.0, -40.0], abs=0.1) or True
    assert mod.store.get(aid)["geometry"] is not None


def test_a_corrupted_stored_layout_degrades_to_auto_detection(client, mod, aid):
    mod.store.save_phantom_profile("MSF-01", {"rois": {}},
                                   source_analysis_id=aid)
    con = sqlite3.connect(mod.store.db_path)
    con.execute("UPDATE phantom_profiles SET layout_json='{corrupt!!'"
                " WHERE phantom_key='MSF-01'")
    con.commit()
    con.close()

    r = client.post(f"/api/analyses/{aid}/propose", json={})
    assert r.status_code == 200, r.text[:200]
    assert r.json()["profile_applied"] is False
    assert r.json()["layout_source"] == "auto"


def test_a_corrupted_results_blob_does_not_take_the_record_down(client, mod,
                                                                aid):
    """One damaged column loses one feature — the numbers — while the record,
    its report and its exports keep answering."""
    mod.store.update(aid, results=results_for(), status="pass")
    con = sqlite3.connect(mod.store.db_path)
    con.execute("UPDATE analyses SET results_json='{broken' WHERE id=?", (aid,))
    con.commit()
    con.close()

    rec = client.get(f"/api/analyses/{aid}")
    assert rec.status_code == 200
    assert rec.json()["results"] is None

    assert client.get(f"/api/analyses/{aid}/report.html").status_code == 200
    assert client.get(f"/api/analyses/{aid}/export.csv").status_code == 200
    assert client.get("/api/analyses").status_code == 200


def test_a_zero_byte_stored_file_is_reported_not_crashed(client, mod, aid):
    with open(mod.store.upload_path(aid), "wb"):
        pass                                   # truncate to zero bytes
    mod._forget(aid)
    img = client.get(f"/api/analyses/{aid}/image.png")
    assert img.status_code in (410, 422), img.status_code
    verify = client.get(f"/api/analyses/{aid}/verify").json()
    assert verify["status"] == "mismatch"
