"""Regression tests for the issues found in the second tester round.

1. Stage B offers "Verify measuring points" rather than "All patterns correct".
2. Every result carries ``reasons`` explaining the pass / warn / fail.
3. The low-contrast block can be dragged, rotated, or set from four corners.
4. Concurrent ROI edits no longer lose one another (the "moving a point does
   not register, moving another one fixes it" report).
5. Uploading the same file twice does not create two History rows.
"""

import io
import os
import threading

import pytest

from conftest import SAMPLES, needs_samples
from test_authorization import ADMIN_PW, _build_app, _login


# --------------------------------------------------------------- 4. atomicity

def test_concurrent_roi_edits_all_survive(tmp_path):
    """Eight ROI moves issued at the same instant must all be kept.

    Before the fix each request did read-modify-write on the whole geometry
    blob, so whichever wrote last erased the others' changes. The symptom the
    tester saw was a point that "did not register" until some other point was
    moved, which rewrote the blob from fresher state.
    """
    from phantom_qa.store import Store
    from test_store_labels import fake_scan

    store = Store(str(tmp_path))
    aid = store.new_analysis(fake_scan(), b"x", "sig", "1.0.0", "1.0", labels={})
    keys = [f"roi{i}" for i in range(8)]
    store.update(aid, geometry={k: {"id": k, "center_mm": [0.0, 0.0]}
                                for k in keys})

    gate = threading.Barrier(len(keys))
    errors: list[Exception] = []

    def move(key, value):
        try:
            gate.wait(timeout=30)          # release all threads together
            store.mutate_json(
                aid, "geometry",
                lambda g: g[key].__setitem__("center_mm", [value, value]))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=move, args=(k, i + 1))
               for i, k in enumerate(keys)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
        assert not t.is_alive(), "mutate_json deadlocked"

    assert not errors, errors
    geom = store.get(aid)["geometry"]
    lost = [k for i, k in enumerate(keys)
            if geom[k]["center_mm"] != [i + 1, i + 1]]
    assert not lost, f"updates lost for {lost}"


# ----------------------------------------------------------- 5. duplicate rows

@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    mod = _build_app(tmp_path, monkeypatch)
    c = TestClient(mod.app)
    c.headers.update({"X-CSRF-Token": _login(c)})
    return c


@needs_samples
def test_same_file_twice_is_one_history_row(client):
    data = open(SAMPLES[0], "rb").read()
    first = client.post("/api/analyses",
                        files={"file": ("scan.dcm", io.BytesIO(data))},
                        data={"site": "Goma", "phantom": "P1"})
    assert first.status_code == 200, first.text
    aid = first.json()["analyses"][0]["id"]

    again = client.post("/api/analyses",
                        files={"file": ("scan.dcm", io.BytesIO(data))}, data={"site": "Test hospital"})
    assert again.status_code == 409
    assert [d["id"] for d in again.json()["duplicate_of"]] == [aid]
    assert len(client.get("/api/analyses").json()["analyses"]) == 1

    # ...unless the user deliberately confirms it
    forced = client.post("/api/analyses",
                         files={"file": ("scan.dcm", io.BytesIO(data))},
                         data={"site": "Test hospital", "allow_duplicate": "true"})
    assert forced.status_code == 200
    assert len(client.get("/api/analyses").json()["analyses"]) == 2


@needs_samples
def test_duplicate_check_ignores_deleted_records(client):
    data = open(SAMPLES[0], "rb").read()
    aid = client.post("/api/analyses",
                      files={"file": ("s.dcm", io.BytesIO(data))}
                      , data={"site": "Test hospital"}).json()["analyses"][0]["id"]
    deleted = client.post(f"/api/analyses/{aid}/delete",
                          json={"admin_password": ADMIN_PW,
                                "reason": "re-upload regression check"})
    assert deleted.status_code == 200, deleted.text
    again = client.post("/api/analyses",
                        files={"file": ("s.dcm", io.BytesIO(data))}, data={"site": "Test hospital"})
    assert again.status_code == 200, "re-upload after deletion must be allowed"


# ------------------------------------------------- 2 + 3. reasons and block

@pytest.fixture(scope="module")
def analysed(sample_scans, pdef):
    from phantom_qa import pipeline
    scan = sample_scans[0]
    reg = pipeline.run_stage_a(scan, pdef)
    ctx = pipeline.build_ctx(scan, pdef, reg, {"scan_meta": scan.meta})
    geom = pipeline.propose_all(ctx)
    res = pipeline.compute_all(ctx, geom)
    return ctx, geom, res


@needs_samples
@pytest.mark.parametrize("test", ["linepairs", "lowcontrast", "uniformity",
                                  "wedge"])
def test_every_test_explains_itself(analysed, test):
    _, _, res = analysed
    reasons = res[test].get("reasons")
    assert reasons, f"{test} reported {res[test]['status']} with no reason"
    assert all(isinstance(r, str) and len(r) > 15 for r in reasons), reasons


@needs_samples
def test_geometry_and_field_explain_themselves(analysed):
    _, _, res = analysed
    g = res["geometry"]
    assert g.get("dimension_reasons"), "dimensions gave no reason"
    assert g.get("field_reasons"), "field alignment gave no reason"


@needs_samples
def test_linepair_groups_explain_missing_pitch(analysed):
    """Per-group reasons are what let the operator see WHICH group failed."""
    _, _, res = analysed
    for row in res["linepairs"]["rows"]:
        if row.get("status") in ("fail", "warn") or row.get("pitch_mm") is None:
            assert row.get("reason"), f"group {row.get('group')} unexplained"


@needs_samples
def test_block_drag_moves_all_eight_circles(analysed):
    from phantom_qa.analysis import lowcontrast
    ctx, geom, _ = analysed
    lcg = geom["lowcontrast"]
    centre = list(lcg["block"]["center_mm"])
    angle = float(lcg["angle_deg"])

    # Dragging the block re-lays the grid from scratch, so compare against a
    # freshly laid grid, not against the proposed circles (those still carry
    # the auto-refinement grid_shift_mm that a manual drag deliberately drops).
    before = lowcontrast.circles_for_block(ctx, centre, angle)
    moved = lowcontrast.circles_for_block(
        ctx, [centre[0] + 7.0, centre[1] - 4.0], angle)
    assert len(moved) == len(before) == 8
    for a, b in zip(before, moved):
        dx = b["roi"]["center_mm"][0] - a["roi"]["center_mm"][0]
        dy = b["roi"]["center_mm"][1] - a["roi"]["center_mm"][1]
        assert abs(dx - 7.0) < 1e-6 and abs(dy + 4.0) < 1e-6, (dx, dy)

    # The proposed circles are the same grid plus the refinement shift. That
    # shift lives in the block's own (u, v) frame, so rotate it into world mm.
    import math
    du, dv = lcg.get("grid_shift_mm") or [0.0, 0.0]
    rad = math.radians(angle)
    sx = du * math.cos(rad) - dv * math.sin(rad)
    sy = du * math.sin(rad) + dv * math.cos(rad)
    for a, prop in zip(before, lcg["circles"]):
        assert abs(a["roi"]["center_mm"][0] + sx
                   - prop["roi"]["center_mm"][0]) < 1e-6
        assert abs(a["roi"]["center_mm"][1] + sy
                   - prop["roi"]["center_mm"][1]) < 1e-6


@needs_samples
def test_block_rotation_turns_the_grid(analysed):
    from phantom_qa.analysis import lowcontrast
    ctx, geom, _ = analysed
    lcg = geom["lowcontrast"]
    centre = list(lcg["block"]["center_mm"])
    a0 = lowcontrast.circles_for_block(ctx, centre, 0.0)
    a90 = lowcontrast.circles_for_block(ctx, centre, 90.0)
    # a 90 deg rotation about the block centre must not leave the grid fixed
    assert any(abs(p["roi"]["center_mm"][0] - q["roi"]["center_mm"][0]) > 1.0
               for p, q in zip(a0, a90))
    # ...and must preserve every circle's distance from the centre
    for p, q in zip(a0, a90):
        rp = ((p["roi"]["center_mm"][0] - centre[0]) ** 2
              + (p["roi"]["center_mm"][1] - centre[1]) ** 2) ** 0.5
        rq = ((q["roi"]["center_mm"][0] - centre[0]) ** 2
              + (q["roi"]["center_mm"][1] - centre[1]) ** 2) ** 0.5
        assert abs(rp - rq) < 1e-6


@needs_samples
def test_block_from_four_corners(analysed):
    from phantom_qa.analysis import lowcontrast
    ctx, geom, _ = analysed
    lcg = geom["lowcontrast"]
    centre = list(lcg["block"]["center_mm"])
    w, h = ctx.pdef.lowcontrast["size_mm"]
    corners_mm = [(centre[0] - w / 2, centre[1] + h / 2),
                  (centre[0] + w / 2, centre[1] + h / 2),
                  (centre[0] + w / 2, centre[1] - h / 2),
                  (centre[0] - w / 2, centre[1] - h / 2)]
    corners_px = [list(ctx.T.mm_to_px(c)) for c in corners_mm]
    got_centre, got_angle = lowcontrast.block_from_corners(ctx, corners_px)
    assert abs(got_centre[0] - centre[0]) < 0.2
    assert abs(got_centre[1] - centre[1]) < 0.2
    assert abs(((got_angle + 45) % 90) - 45) < 1.0, got_angle


# ------------------------------------------------------------- 1. UI captions

def _app_js():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(here, "phantom_qa", "webapp", "static", "app.js")
    return io.open(path, encoding="utf-8").read()


def test_stage_b_button_leads_to_measuring_points():
    """The old caption was a dead end when the patterns were NOT correct."""
    js = _app_js()
    assert "Verify measuring points" in js
    assert "All patterns correct" not in js


def test_reasons_are_rendered_not_just_computed():
    js = _app_js()
    assert "reasonsBlock" in js, "no renderer for the reasons"
    for field in ("dimension_reasons", "field_reasons"):
        assert field in js, f"{field} computed but never shown"


# ------------------------------------------- audit: scale must be trustworthy

@needs_samples
def test_reference_scans_use_all_four_rulers(analysed):
    """On a good scan nothing is rejected, so the gate costs nothing."""
    _, _, res = analysed
    scale = res["geometry"]["scale"]
    assert scale["reliable"]
    assert scale["rulers_rejected"] == []
    assert len(scale["rulers_used"]) == 4


@needs_samples
def test_unreliable_ruler_is_excluded_from_the_scale(analysed):
    """A ruler whose marks are not evenly spaced must not set the mm/px scale.

    Found during the comprehensive run: on a scan where three of four rulers
    were mis-detected, their pitches outvoted the one good ruler and dragged
    the measured phantom side from ~293 mm to ~233 mm. The scale is applied to
    every reported length, so a bad ruler corrupts the whole report.
    """
    from phantom_qa.analysis import geometry as geo
    ctx, geom, _ = analysed

    ggeom = geom["geometry"]
    good = ggeom["rulers"]["right"]["line_offsets_mm"]
    spoiled = dict(ggeom)
    spoiled["rulers"] = dict(ggeom["rulers"])
    # same marks, but jittered so the spacing is no longer regular
    spoiled["rulers"]["left"] = dict(ggeom["rulers"]["left"])
    spoiled["rulers"]["left"]["line_offsets_mm"] = [
        v + (2.0 if i % 2 else -2.0) for i, v in enumerate(good)]

    res = geo.compute(ctx, spoiled)
    scale = res["scale"]
    assert "left" in scale["rulers_rejected"], scale
    assert "left" not in scale["rulers_used"]
    reasons = res.get("dimension_reasons") or []
    assert any("excluded from the scale" in r for r in reasons), reasons


# ------------------------------- audit: the new endpoint rejects malformed input

# Sent as raw bytes, not via json=: NaN and Infinity are not legal JSON, so
# the test client refuses to encode them - but Python's json parser happily
# ACCEPTS those literals off the wire, so a real client can still send them.
@pytest.mark.parametrize("raw", [
    '{"center_px": [1.0]}',                        # too short
    '{"center_px": [1.0, 2.0, 3.0]}',              # too long
    '{"center_px": [NaN, 2.0]}',                   # not finite
    '{"center_px": [Infinity, 2.0]}',              # not finite
    '{"angle_deg": Infinity}',                     # not finite
    '{"angle_deg": -Infinity}',                    # not finite
    '{"corners_px": [[0,0],[1,1],[2,2]]}',         # only three corners
    '{"corners_px": [[0,0],[1,1],[2,2],[3]]}',     # ragged corner
    '{"corners_px": [[0,0],[1,1],[2,2],[3,NaN]]}',  # not finite
])
def test_block_endpoint_rejects_bad_geometry(client, raw):
    """Malformed coordinates must be a 4xx from validation, never a 500."""
    r = client.post("/api/analyses/does-not-matter/lowcontrast_block",
                    content=raw.encode(),
                    headers={"Content-Type": "application/json"})
    assert 400 <= r.status_code < 500, (r.status_code, r.text)


def test_mutate_json_rejects_unknown_column(tmp_path):
    """The column name is interpolated into SQL, so it must be whitelisted."""
    from phantom_qa.store import Store
    from test_store_labels import fake_scan

    store = Store(str(tmp_path))
    aid = store.new_analysis(fake_scan(), b"x", "s", "1.0.0", "1.0", labels={})
    with pytest.raises(ValueError):
        store.mutate_json(aid, "results_json; DROP TABLE analyses --",
                          lambda v: None)
    assert store.get(aid) is not None


@needs_samples
def test_block_placement_over_http_end_to_end(client):
    """The whole bug-3 flow through the API: propose, drag, rotate, corners."""
    data = open(SAMPLES[0], "rb").read()
    aid = client.post("/api/analyses",
                      files={"file": ("s.dcm", io.BytesIO(data))},
                      data={"site": "Goma", "phantom": "P1"}
                      ).json()["analyses"][0]["id"]
    assert client.post(f"/api/analyses/{aid}/propose", json={}).status_code == 200
    lcg = client.get(f"/api/analyses/{aid}").json()["geometry"]["lowcontrast"]
    cx, cy = lcg["block"]["center_px"]
    before = [c["roi"]["center_mm"] for c in lcg["circles"]]

    # drag
    r = client.post(f"/api/analyses/{aid}/lowcontrast_block",
                    json={"center_px": [cx + 120, cy + 80]})
    assert r.status_code == 200, r.text
    after = [c["roi"]["center_mm"] for c in r.json()["lowcontrast"]["circles"]]
    assert len(after) == 8
    assert all(a != b for a, b in zip(before, after)), "circles did not follow"

    # rotate
    r = client.post(f"/api/analyses/{aid}/lowcontrast_block",
                    json={"angle_deg": -30.0})
    assert r.status_code == 200 and abs(r.json()["angle_deg"] + 30.0) < 1e-6

    # four clicked corners
    r = client.post(f"/api/analyses/{aid}/lowcontrast_block",
                    json={"corners_px": [[cx - 300, cy - 150], [cx + 300, cy - 150],
                                         [cx + 300, cy + 150], [cx - 300, cy + 150]]})
    assert r.status_code == 200, r.text
    assert len(r.json()["lowcontrast"]["circles"]) == 8

    # the edit is persisted and marked as a manual adjustment
    saved = client.get(f"/api/analyses/{aid}").json()["geometry"]["lowcontrast"]
    assert saved["block"]["manually_adjusted"] is True
    assert saved["grid_shift_mm"] == [0.0, 0.0]
    assert all(c["roi"]["manually_adjusted"] for c in saved["circles"])
