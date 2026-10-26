"""Deletion: what it takes, what it destroys, and what must not come back.

The third tester round reported: upload a DICOM, adjust some marks, delete it,
upload the same file again — and the previous marks were still there. Whatever
produced it, the fix has three parts, and each is tested here:

1. a deletion that fails must be impossible to mistake for one that succeeded;
2. deleting an analysis must leave nothing of it behind — row, file, edit
   history, cached image, and the phantom's stored layout when it was the last
   analysis of that phantom;
3. a re-upload of the same bytes must start from a clean slate.
"""

from __future__ import annotations

import hashlib
import io
import os

import pytest
from fastapi.testclient import TestClient

from test_authorization import ADMIN_PW, USER_PW, _build_app, _login
from test_store_labels import fake_scan, results_for

GOOD_REASON = "test exposure, wrong phantom fitted"


def _tiny_png(seed: int = 0) -> bytes:
    import numpy as np
    from PIL import Image
    arr = np.full((24, 24), 100 + (seed % 50), dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture()
def mod(tmp_path, monkeypatch):
    return _build_app(tmp_path, monkeypatch)


@pytest.fixture()
def client(mod):
    c = TestClient(mod.app)
    c.headers.update({"X-CSRF-Token": _login(c)})
    return c


def _make(store, *, phantom="MSF-01", payload=None, geometry=None):
    payload = payload if payload is not None else _tiny_png()
    aid = store.new_analysis(
        fake_scan(sha=hashlib.sha256(payload).hexdigest()), payload,
        "sig", "1.0.0", "1.0",
        labels={"site": "Goma", "phantom": phantom})
    store.update(aid, results=results_for(), status="pass",
                 geometry=geometry if geometry is not None else {"geometry": {}})
    return aid


# --------------------------------------------------- the confirmation rules

def test_the_analysis_id_is_no_longer_typed_back(client, mod):
    """A copy-pasted id proved nothing; a written reason says why."""
    aid = _make(mod.store)
    r = client.post(f"/api/analyses/{aid}/delete",
                    json={"admin_password": ADMIN_PW, "reason": GOOD_REASON})
    assert r.status_code == 200, r.text
    assert mod.store.get(aid) is None


def test_the_policy_endpoint_describes_the_current_rules(client):
    p = client.get("/api/deletion_policy").json()
    assert p["requires_admin_password"] is True
    assert p["requires_reason"] is True
    assert p["min_reason_chars"] >= 1
    assert "requires_id_confirmation" not in p


@pytest.mark.parametrize("reason", ["", "  ", "no", "abcd"])
def test_a_deletion_without_a_real_reason_is_refused(client, mod, reason):
    aid = _make(mod.store)
    r = client.post(f"/api/analyses/{aid}/delete",
                    json={"admin_password": ADMIN_PW, "reason": reason})
    assert r.status_code == 400
    # A plain string, not pydantic's list of dicts: the panel shows it verbatim.
    assert isinstance(r.json()["detail"], str)
    assert mod.store.get(aid) is not None


def test_a_wrong_password_answers_401_whatever_the_reason_says(client, mod):
    """Order matters: the reason check must not leak whether the password was
    right, and a bad password must still be a 401 so the UI can say so."""
    aid = _make(mod.store)
    for reason in ("", GOOD_REASON):
        r = client.post(f"/api/analyses/{aid}/delete",
                        json={"admin_password": "not-the-admin-password",
                              "reason": reason})
        assert r.status_code == 401, r.text
    assert mod.store.get(aid) is not None


def test_the_everyday_password_is_not_the_admin_password(client, mod):
    aid = _make(mod.store)
    r = client.post(f"/api/analyses/{aid}/delete",
                    json={"admin_password": USER_PW, "reason": GOOD_REASON})
    assert r.status_code == 401
    assert mod.store.get(aid) is not None


def test_an_over_long_reason_is_truncated_not_rejected(client, mod, tmp_path):
    aid = _make(mod.store)
    r = client.post(f"/api/analyses/{aid}/delete",
                    json={"admin_password": ADMIN_PW, "reason": "x" * 5000})
    assert r.status_code == 200
    audit_log = os.path.join(str(tmp_path), "logs", "audit.log")
    if os.path.exists(audit_log):
        with open(audit_log, encoding="utf-8") as f:
            body = f.read()
        assert "x" * 5000 not in body, "the reason was written to the log uncapped"


def test_the_reason_reaches_the_audit_log(client, mod, tmp_path):
    aid = _make(mod.store)
    client.post(f"/api/analyses/{aid}/delete",
                json={"admin_password": ADMIN_PW, "reason": GOOD_REASON})
    audit_log = os.path.join(str(tmp_path), "logs", "audit.log")
    assert os.path.exists(audit_log)
    with open(audit_log, encoding="utf-8") as f:
        body = f.read()
    assert "event=delete" in body
    assert GOOD_REASON in body, "the operator's reason was not recorded"
    assert ADMIN_PW not in body, "the administrator password leaked into the log"


# ------------------------------------------------------- what deletion takes

def test_deletion_removes_row_file_and_edit_history(mod):
    store = mod.store
    aid = _make(store)
    store.set_geometry_baseline(aid, {"geometry": {}})
    store.mutate_geometry(aid, lambda g: g.__setitem__("marker", 1),
                          action="roi")
    assert store.geometry_state(aid)["undo_depth"] == 1
    path = store.upload_path(aid)
    assert os.path.exists(path)

    store.delete(aid)

    assert store.get(aid) is None
    assert not os.path.exists(path)
    import sqlite3
    con = sqlite3.connect(store.db_path)
    n = con.execute("SELECT COUNT(*) FROM geometry_history WHERE analysis_id=?",
                    (aid,)).fetchone()[0]
    con.close()
    assert n == 0, "the undo history outlived the analysis it belonged to"


def test_deleting_the_last_analysis_forgets_the_phantom_layout(mod):
    store = mod.store
    aid = _make(store, phantom="MSF-01")
    store.save_phantom_profile("MSF-01", {"rois": {}}, source_analysis_id=aid)
    assert store.get_phantom_profile("MSF-01") is not None

    out = store.delete(aid)

    assert out["profile_deleted"] is True
    assert store.get_phantom_profile("MSF-01") is None, (
        "the stored layout outlived every analysis of its phantom — the next "
        "scan of an unrelated phantom with the same label would inherit it")


def test_a_layout_survives_while_another_analysis_still_uses_it(mod):
    store = mod.store
    keep = _make(store, phantom="MSF-01", payload=_tiny_png(1))
    drop = _make(store, phantom="MSF-01", payload=_tiny_png(2))
    store.save_phantom_profile("MSF-01", {"rois": {}}, source_analysis_id=keep)

    out = store.delete(drop)

    assert out["profile_deleted"] is False
    assert store.get_phantom_profile("MSF-01") is not None
    store.delete(keep)
    assert store.get_phantom_profile("MSF-01") is None


def test_deleting_an_unlabelled_analysis_touches_no_layout(mod):
    """'' is a legal phantom value and must never be a layout key."""
    store = mod.store
    aid = _make(store, phantom="")
    store.save_phantom_profile("MSF-01", {"rois": {}})
    out = store.delete(aid)
    assert out["profile_deleted"] is False
    assert store.get_phantom_profile("MSF-01") is not None
    with pytest.raises(ValueError):
        store.save_phantom_profile("   ", {"rois": {}})


def test_the_endpoint_reports_the_layout_it_destroyed(client, mod):
    aid = _make(mod.store, phantom="MSF-07")
    mod.store.save_phantom_profile("MSF-07", {"rois": {}}, source_analysis_id=aid)
    r = client.post(f"/api/analyses/{aid}/delete",
                    json={"admin_password": ADMIN_PW, "reason": GOOD_REASON})
    assert r.status_code == 200
    assert r.json()["layout_deleted"] is True
    assert r.json()["phantom"] == "MSF-07"


def test_the_record_says_in_advance_what_a_delete_would_take(client, mod):
    aid = _make(mod.store, phantom="MSF-09")
    mod.store.save_phantom_profile("MSF-09", {"rois": {}}, source_analysis_id=aid)
    impact = client.get(f"/api/analyses/{aid}").json()["delete_impact"]
    assert impact["is_last_for_phantom"] is True
    assert impact["layout_would_be_deleted"] is True
    assert impact["phantom"] == "MSF-09"


def test_deletion_evicts_the_rendered_image_cache(mod):
    """A deleted scan's pixels must not stay in the worker's memory."""
    store = mod.store
    aid = _make(store)
    mod._img_cache[(aid, None, None, 1600)] = b"pixels"
    mod._scans[aid] = object()
    mod._regs[(aid, "{}")] = object()
    mod._forget(aid)
    assert not [k for k in mod._img_cache if k[0] == aid]
    assert aid not in mod._scans
    assert not [k for k in mod._regs if k[0] == aid]


# ------------------------------------------- the reported "marks came back"

def test_reuploading_a_deleted_file_starts_from_nothing(client, mod):
    """The regression the third tester round reported.

    Upload, adjust the marks, delete, upload the same bytes again: the new
    analysis must be a different record with no geometry at all."""
    payload = _tiny_png(7)
    store = mod.store

    first = store.new_analysis(
        fake_scan(sha=hashlib.sha256(payload).hexdigest()), payload,
        "sig", "1.0.0", "1.0", labels={"site": "Goma", "phantom": "MSF-01"})
    # the operator adjusts a mark
    store.set_geometry_baseline(first, {"uniformity": {"squares": [
        {"id": "C", "roi": {"id": "uniformity/C", "type": "rect",
                            "center_mm": [0.0, 0.0], "size_mm": [30.0, 30.0],
                            "angle_deg": 0.0}}]}})
    store.mutate_geometry(
        first,
        lambda g: g["uniformity"]["squares"][0]["roi"].update(
            {"center_mm": [12.0, -8.0], "manually_adjusted": True}),
        action="roi")
    assert store.get(first)["geometry"]["uniformity"]["squares"][0]["roi"][
        "manually_adjusted"] is True

    r = client.post(f"/api/analyses/{first}/delete",
                    json={"admin_password": ADMIN_PW, "reason": GOOD_REASON})
    assert r.status_code == 200, r.text

    again = client.post("/api/analyses",
                        files={"file": ("scan.png", io.BytesIO(payload))},
                        data={"site": "Goma", "phantom": "MSF-01"})
    assert again.status_code == 200, again.text
    new_id = again.json()["analyses"][0]["id"]
    assert new_id != first

    rec = client.get(f"/api/analyses/{new_id}").json()
    assert rec["geometry"] is None, (
        "the re-uploaded scan inherited geometry from the deleted analysis")
    assert rec["history"]["undo_depth"] == 0
    assert rec["history"]["redo_depth"] == 0
    # and the phantom's stored layout went with the deleted analysis, so
    # nothing can be replayed onto it either
    assert rec["phantom_profile"] is None


def test_a_refused_delete_leaves_the_record_findable(client, mod):
    """The failure mode behind the report: a delete that silently did not
    happen, followed by a re-upload that offered to reopen the old record."""
    payload = _tiny_png(11)
    store = mod.store
    aid = store.new_analysis(
        fake_scan(sha=hashlib.sha256(payload).hexdigest()), payload,
        "sig", "1.0.0", "1.0", labels={})

    refused = client.post(f"/api/analyses/{aid}/delete",
                          json={"admin_password": "wrong", "reason": GOOD_REASON})
    assert refused.status_code == 401

    again = client.post("/api/analyses",
                        files={"file": ("scan.png", io.BytesIO(payload))}, data={"site": "Test hospital"})
    assert again.status_code == 409, (
        "the duplicate check must still see the record that was NOT deleted")
    assert [d["id"] for d in again.json()["duplicate_of"]] == [aid]
