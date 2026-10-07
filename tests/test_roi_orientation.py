"""Setting an orientation by hand, and the edits that sit next to it.

The tester asked for the low-contrast angle to be a field in the panel rather
than a dialog box. Moving it there exposed three defects that had been hidden
by the prompt: rotating a group square swung its measuring line 45 degrees off
the pattern, rotating anything without an area returned a 500 *after* committing
the change, and the manual field-edge placement was the one Stage C edit that
could still silently discard a concurrent one.
"""

from __future__ import annotations

import hashlib
import io
import threading

import numpy as np
import pytest
from fastapi.testclient import TestClient

from phantom_qa import pipeline
from phantom_qa.analysis.common import (Ctx, rect_roi, roi_angle_deg, segment)
from phantom_qa.phantom_def import load_default
from phantom_qa.registration import Transform
from phantom_qa.store import Store
from test_authorization import _build_app, _login
from test_store_labels import fake_scan


def _png(seed: int = 0) -> bytes:
    from PIL import Image
    arr = np.full((64, 64), 100 + (seed % 50), dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return buf.getvalue()


def make_ctx(px_per_mm=1.0, n=64):
    rng = np.random.default_rng(5)
    img = 2000 + rng.normal(0, 20, (n, n))
    T = Transform(A=np.array([[px_per_mm, 0.0], [0.0, -px_per_mm]]),
                  t=np.array([n / 2.0, n / 2.0]))
    return Ctx(pixels=img, T=T, pdef=load_default())


def geometry_with_a_group(ctx) -> dict:
    """A line-pair group and its profile line, 45 degrees apart as designed."""
    return {"linepairs": {
        "strip_angle_deg": 44.1, "roi_size_mm": 12.6,
        "roi_angle_offset_deg": 45.0,
        "groups": [{
            "id": "G2.0", "freq_lp_mm": 2.0, "detected": True,
            "roi": rect_roi(ctx, (5.0, 5.0), (12.6, 12.6), 89.1,
                            "linepairs/G2.0"),
            "profile_seg": segment(ctx, (2.0, 2.0), (8.0, 8.0),
                                   "linepairs/G2.0/profile"),
        }]}}


@pytest.fixture()
def mod(tmp_path, monkeypatch):
    return _build_app(tmp_path, monkeypatch)


@pytest.fixture()
def client(mod):
    c = TestClient(mod.app)
    c.headers.update({"X-CSRF-Token": _login(c)})
    return c


@pytest.fixture()
def aid(mod):
    """An analysis whose stored file really decodes, so _ctx() can run."""
    payload = _png()
    ctx = make_ctx()
    a = mod.store.new_analysis(
        fake_scan(sha=hashlib.sha256(payload).hexdigest(), name="scan.png"),
        payload, "sig", "1.0.0", "1.0", labels={"phantom": "MSF-01"})
    mod.store.update(a, reg={"transform": ctx.T.to_dict(),
                             "corners_px": [[0, 0], [1, 0], [1, 1], [0, 1]]})
    mod.store.set_geometry_baseline(a, pipeline.to_jsonable(
        geometry_with_a_group(ctx)))
    return a


# ------------------------------------------- rotating something with no area

def test_rotating_a_profile_line_does_not_fail_after_committing(client, aid):
    """It used to raise from stats_for_roi *after* store had committed, so the
    client was told the change failed while the database said it had happened."""
    r = client.post(f"/api/analyses/{aid}/roi_rotate",
                    json={"roi_id": "linepairs/G2.0/profile", "angle_deg": 30.0})
    assert r.status_code == 200, r.text
    assert r.json()["stats"] is None          # a line has no pixels to average
    assert roi_angle_deg(r.json()["roi"]) == pytest.approx(30.0, abs=1e-6)


def test_reading_statistics_for_a_line_is_not_an_error(client, aid):
    r = client.get(f"/api/analyses/{aid}/roi_stats"
                   f"?roi_id=linepairs/G2.0/profile")
    assert r.status_code == 200, r.text
    assert r.json()["stats"] is None


def test_a_circle_still_reports_statistics(client, mod, aid):
    r = client.get(f"/api/analyses/{aid}/roi_stats?roi_id=linepairs/G2.0")
    assert r.status_code == 200
    assert r.json()["stats"]["n"] > 0


# --------------------------------- the square and its measuring line stay apart

def test_rotating_a_group_square_turns_its_line_by_the_same_amount(client, aid,
                                                                   mod):
    """The square sits 45 degrees off its profile line by design. Setting both
    to the same absolute angle swung the measuring line off the pattern — and
    marked it hand-set, which suppresses the automatic re-measurement for
    good, so the damage outlived the edit."""
    before = mod.store.get(aid)["geometry"]["linepairs"]["groups"][0]
    roi_before = roi_angle_deg(before["roi"])
    seg_before = roi_angle_deg(before["profile_seg"])
    offset = (seg_before - roi_before) % 180.0

    r = client.post(f"/api/analyses/{aid}/roi_rotate",
                    json={"roi_id": "linepairs/G2.0",
                          "angle_deg": roi_before + 10.0})
    assert r.status_code == 200, r.text

    after = mod.store.get(aid)["geometry"]["linepairs"]["groups"][0]
    roi_after = roi_angle_deg(after["roi"])
    seg_after = roi_angle_deg(after["profile_seg"])
    assert roi_after == pytest.approx(roi_before + 10.0, abs=1e-6)
    assert (seg_after - roi_after) % 180.0 == pytest.approx(offset, abs=1e-6), (
        "the measuring line no longer sits at its designed angle to the square")


def test_an_ROI_without_an_orientation_is_refused_cleanly(client, mod, aid):
    ctx = make_ctx()
    from phantom_qa.analysis.common import circle_roi
    mod.store.mutate_geometry(
        aid,
        lambda g: g.__setitem__("lowcontrast", {"circles": [
            {"id": "L1", "roi": pipeline.to_jsonable(
                circle_roi(ctx, (0.0, 0.0), 7.0, "lowcontrast/L1"))}]}),
        action="test")
    r = client.post(f"/api/analyses/{aid}/roi_rotate",
                    json={"roi_id": "lowcontrast/L1", "angle_deg": 10.0})
    assert r.status_code == 400
    assert "orientation" in r.json()["detail"]


# ------------------------------------------------- the field edge is serialised

@pytest.mark.parametrize("side", ["top", "right", "bottom", "left"])
def test_a_manual_field_edge_is_recorded(client, mod, aid, side):
    mod.store.mutate_geometry(
        aid, lambda g: g.__setitem__(
            "geometry", {"field_edges": {}, "rulers": {}}), action="test")
    r = client.post(f"/api/analyses/{aid}/field_edge",
                    json={"side": side, "point_px": [11.0, 2.0]})
    assert r.status_code == 200, r.text
    assert r.json()["manual"] is True
    assert "history" in r.json(), "the edit is not undoable"
    assert r.json()["edge_pt_px"] == [11.0, 2.0]
    stored = mod.store.get(aid)["geometry"]["geometry"]["field_edges"][side]
    assert stored["manual"] is True
    assert stored["edge_pt_px"] == [11.0, 2.0]


def test_a_field_edge_on_a_scan_without_geometry_is_a_400_not_a_500(client, mod,
                                                                    aid):
    """propose_all stores {"_error": ...} for a test that raised, so the
    field-edge dict is not guaranteed to be there."""
    mod.store.mutate_geometry(
        aid, lambda g: g.__setitem__("geometry", {"_error": "ValueError: no"}),
        action="test")
    r = client.post(f"/api/analyses/{aid}/field_edge",
                    json={"side": "top", "point_px": [32.0, 2.0]})
    assert r.status_code == 400
    assert isinstance(r.json()["detail"], str)


def test_a_field_edge_does_not_discard_a_concurrent_roi_move(mod, aid):
    """It was the last Stage C edit doing an unserialised read-modify-write."""
    store = mod.store
    store.mutate_geometry(
        aid, lambda g: g.__setitem__(
            "geometry", {"field_edges": {}, "rulers": {}}), action="test")

    gate = threading.Barrier(6)
    errors: list[Exception] = []

    def edit(i):
        try:
            gate.wait(timeout=30)
            if i % 2:
                store.mutate_geometry(
                    aid,
                    lambda g: g["geometry"]["field_edges"].__setitem__(
                        f"side{i}", {"offset_from_edge_mm": float(i)}),
                    action="field_edge")
            else:
                store.mutate_geometry(
                    aid,
                    lambda g: g["linepairs"]["groups"][0]["roi"].__setitem__(
                        f"probe{i}", i),
                    action="roi")
        except Exception as exc:            # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=edit, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
        assert not t.is_alive(), "the field-edge write deadlocked"
    assert not errors, errors

    geom = store.get(aid)["geometry"]
    for i in range(6):
        if i % 2:
            assert f"side{i}" in geom["geometry"]["field_edges"], f"lost side{i}"
        else:
            assert f"probe{i}" in geom["linepairs"]["groups"][0]["roi"], \
                f"lost probe{i}"


# -------------------------------------------------- the block angle round trip

def test_the_block_angle_endpoint_takes_a_plain_number(client, mod, aid):
    """What the panel's field posts. The endpoint already accepted this shape;
    the test pins it, because the field is now the only way to set the angle."""
    ctx = make_ctx()
    from phantom_qa.analysis import lowcontrast
    mod.store.mutate_geometry(
        aid, lambda g: g.__setitem__("lowcontrast", pipeline.to_jsonable({
            "angle_deg": 0.0, "grid_shift_mm": [0.0, 0.0],
            "block": rect_roi(ctx, (0.0, 0.0), tuple(ctx.pdef.lowcontrast["size_mm"]),
                              0.0, "lowcontrast/block"),
            "circles": lowcontrast.circles_for_block(ctx, (0.0, 0.0), 0.0),
        })), action="test")
    r = client.post(f"/api/analyses/{aid}/lowcontrast_block",
                    json={"angle_deg": -45.5})
    assert r.status_code == 200, r.text
    assert r.json()["angle_deg"] == pytest.approx(-45.5)
    assert r.json()["lowcontrast"]["angle_deg"] == pytest.approx(-45.5)
    assert "history" in r.json(), "setting the angle is not undoable"


def test_turning_the_block_180_degrees_swaps_the_disc_each_circle_sits_on():
    """What the Turn 180° button is for.

    The block outline is symmetrical, so detection determines its angle only
    modulo 180 and can find it end for end. Every ROI then still lands on a
    real disc — L1 on L5's, L2 on L6's — so nothing looks wrong on the image
    and no geometric check fails. The only symptom is the contrast series
    coming out backwards, which is why one button is the right correction."""
    from phantom_qa.analysis import lowcontrast
    ctx = make_ctx(px_per_mm=2.0, n=600)
    upright = lowcontrast.circles_for_block(ctx, (0.0, 0.0), -45.1)
    turned = lowcontrast.circles_for_block(ctx, (0.0, 0.0), -45.1 + 180)

    at = {c["id"]: np.asarray(c["roi"]["center_mm"]) for c in upright}
    pairs = {}
    for c in turned:
        here = np.asarray(c["roi"]["center_mm"])
        nearest = min(at, key=lambda k: float(np.linalg.norm(at[k] - here)))
        pairs[c["id"]] = (nearest,
                          float(np.linalg.norm(at[nearest] - here)))

    for cid, (nearest, dist) in pairs.items():
        assert cid != nearest, f"{cid} did not move at all"
        assert dist < 1.5, (
            f"after the flip {cid} sits {dist:.1f} mm from any disc — the two "
            f"orientations should map onto the same eight discs")
    # the pairing is the one the phantom's own grid implies
    assert pairs["L1"][0] == "L5" and pairs["L5"][0] == "L1"
    assert pairs["L4"][0] == "L8" and pairs["L8"][0] == "L4"


def test_turning_twice_returns_to_the_original(client, mod, aid):
    from phantom_qa.analysis import lowcontrast
    ctx = make_ctx()
    mod.store.mutate_geometry(
        aid, lambda g: g.__setitem__("lowcontrast", pipeline.to_jsonable({
            "angle_deg": -45.1, "grid_shift_mm": [0.0, 0.0],
            "block": rect_roi(ctx, (0.0, 0.0),
                              tuple(ctx.pdef.lowcontrast["size_mm"]), -45.1,
                              "lowcontrast/block"),
            "circles": lowcontrast.circles_for_block(ctx, (0.0, 0.0), -45.1),
        })), action="test")

    first = client.post(f"/api/analyses/{aid}/lowcontrast_block",
                        json={"angle_deg": -45.1 + 180})
    assert first.status_code == 200, first.text
    back = client.post(f"/api/analyses/{aid}/lowcontrast_block",
                       json={"angle_deg": -45.1})
    assert back.status_code == 200, back.text

    restored = {c["id"]: c["roi"]["center_mm"]
                for c in back.json()["lowcontrast"]["circles"]}
    original = {c["id"]: c["roi"]["center_mm"]
                for c in lowcontrast.circles_for_block(ctx, (0.0, 0.0), -45.1)}
    for cid, centre in original.items():
        assert restored[cid] == pytest.approx(centre, abs=1e-6), (
            f"{cid} did not come back to where it started")


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_a_non_finite_angle_is_rejected_on_the_wire(client, aid, bad):
    import json
    r = client.post(f"/api/analyses/{aid}/lowcontrast_block",
                    content=json.dumps({"angle_deg": bad}),
                    headers={"Content-Type": "application/json"})
    assert r.status_code in (400, 422)
