"""Optional real-browser acceptance checks (pip install playwright uvicorn).

Uses local Chrome or Playwright Chromium; no browser download at test time.
"""
from __future__ import annotations

import json
import io
import os
import socket
import threading
import time
from urllib.parse import parse_qs, urlparse

import pytest

from test_auto_analyse import algorithms, mod, passing, png  # shared fixtures
from test_analysis_revisions import alignment_case
from test_authorization import USER_PW


@pytest.fixture
def legacy_upload_cache(mod):
    """Prime a real browser cache with controllers from before shared uploads."""
    from fastapi.responses import HTMLResponse, Response

    state = {"priming": False, "legacy_requests": 0}

    @mod.app.middleware("http")
    async def previous_release(request, call_next):
        if request.url.path == "/prime-upload-cache":
            return HTMLResponse('<input id="auto-file" type="file">'
                                '<script src="app.js"></script>'
                                '<script src="auto-analyse.js"></script>')
        if state["priming"] and request.url.path in ("/app.js", "/auto-analyse.js"):
            state["legacy_requests"] += 1
            # Earlier controllers bound directly to fields in the old markup.
            return Response('document.getElementById("auto-file").addEventListener("change", () => {});',
                            media_type="application/javascript",
                            headers={"Cache-Control": "private, max-age=3600"})
        return await call_next(request)

    return state


@pytest.fixture
def browser_page(mod, algorithms, legacy_upload_cache):
    sync_api = pytest.importorskip("playwright.sync_api")
    uvicorn = pytest.importorskip("uvicorn")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(mod.app, log_level="critical"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started, "Test server did not start"
    try:
        with sync_api.sync_playwright() as playwright:
            chrome = os.path.join(os.environ.get("PROGRAMFILES", "C:/Program Files"),
                                  "Google/Chrome/Application/chrome.exe")
            try:
                browser = playwright.chromium.launch(headless=True,
                    **({"executable_path": chrome} if os.path.exists(chrome) else {}))
            except sync_api.Error as exc:
                pytest.skip(f"A local Chrome/Chromium browser is unavailable: {exc}")
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 1000})
                response = page.request.post(f"http://127.0.0.1:{port}/api/login",
                                             data={"username": "qa", "password": USER_PW})
                assert response.ok
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{port}/")
                yield page, errors
            finally:
                browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


def send_scan(page, seed=0):
    page.locator("#auto-file").set_input_files({"name": "scan.png", "mimeType": "image/png", "buffer": png(seed)})
    page.locator("#auto-result").wait_for(state="visible")
    page.locator("#auto-viewer").wait_for(state="visible")


def test_browser_upload_forms_survive_cached_controllers_from_previous_release(browser_page, legacy_upload_cache):
    page, errors = browser_page
    origin = page.url.rstrip("/")
    session = page.context.new_cdp_session(page)
    session.send("Network.clearBrowserCache")
    session.detach()
    legacy_upload_cache["priming"] = True
    page.goto(origin + "/prime-upload-cache")
    assert legacy_upload_cache["legacy_requests"] == 2
    legacy_upload_cache["priming"] = False
    page.goto(origin + "/")
    page.locator("#auto-drop").wait_for(state="visible", timeout=3000)
    assert page.locator("#auto-site").is_hidden()
    assert page.locator("#auto-drop").inner_text() == "Drop DICOM file here"
    with page.expect_file_chooser() as chosen:
        page.locator("#auto-drop").click()
    chosen.value.set_files({"name": "scan.png", "mimeType": "image/png", "buffer": png()})
    page.locator("#auto-viewer").wait_for(state="visible")
    page.locator("#tab-analyze").click()
    page.locator("#drop").wait_for(state="visible")
    with page.expect_file_chooser() as chosen:
        page.locator("#drop").click()
    chosen.value.set_files({"name": "scan.png", "mimeType": "image/png", "buffer": png(1)})
    assert page.locator("#btn-upload").is_enabled()
    page.locator("#btn-upload").click()
    page.locator('#stage-nav li.active[data-stage="A"]').wait_for(state="visible")
    assert not errors


def test_browser_upload_pass_viewer_tasks_and_navigation(browser_page):
    page, errors = browser_page
    assert page.locator("header nav button").first.inner_text() == "Auto Analyze"
    assert page.locator("#view-auto-analyse").is_visible()
    assert not page.locator("#view-analyze").is_visible()
    assert page.locator("#auto-drop").is_visible()
    assert page.locator("#auto-upload-form .auto-labels input:visible").count() == 0
    assert page.locator("#auto-upload-form button:visible").count() == 0
    send_scan(page)
    assert not page.locator("#auto-upload-form").is_visible()
    assert page.locator("#auto-results-page").is_visible()
    assert page.locator("#auto-technical").count() == 0
    assert page.locator("#auto-feature-status small").count() == 0
    assert page.locator("#auto-feature-status li.auto-pass").count() == 5
    assert page.locator('#auto-tasks [data-task-type="next_scan"]').count() == 1
    assert page.locator('#auto-tasks [data-task-type="contact_technician"]').count() == 0
    assert page.locator("#auto-overlays polygon").count() > 0
    assert page.locator("#auto-overlays [stroke]").evaluate_all("nodes => nodes.every(n => n.getAttribute('stroke') === '#65df8b')")
    assert page.locator("#auto-overlays").evaluate("node => getComputedStyle(node).pointerEvents") == "none"
    page.locator("#tab-analyze").click()
    assert page.locator("#view-analyze").is_visible() and not page.locator("#view-auto-analyse").is_visible()
    page.locator("#tab-history").click()
    page.locator("#history-table tbody tr").first.wait_for(state="visible")
    assert not page.locator("#view-auto-analyse").is_visible()
    page.locator("#tab-auto-analyse").click()
    assert page.locator("#view-auto-analyse").is_visible() and not page.locator("#view-history").is_visible()
    # Responsive two-column layout collapses into one column on a small screen.
    page.set_viewport_size({"width": 650, "height": 900})
    assert page.locator(".auto-results-grid").evaluate("node => getComputedStyle(node).gridTemplateColumns.split(' ').length") == 1
    page.locator("#auto-new-scan").click()
    assert page.locator("#auto-upload-form").is_visible()
    assert not page.locator("#auto-results-page").is_visible()
    assert not errors


def test_browser_linepair_geometry_does_not_shift_on_analyze_handoff(browser_page, mod, passing, tmp_path):
    import numpy as np
    from PIL import Image
    from phantom_qa.analysis.common import Ctx, rect_roi, segment
    from phantom_qa.registration import Transform
    page, errors = browser_page
    # Odd detector dimensions force the PNG preview to round its smaller axis.
    pixels = np.tile(np.linspace(20, 220, 3001, dtype=np.uint8), (2223, 1))
    encoded = io.BytesIO()
    Image.fromarray(pixels).save(encoded, format="PNG")
    ctx = Ctx(pixels, Transform(np.array([[5., 0.], [0., -5.]]), np.array([1500., 1111.])), mod.pdef)
    for index, group in enumerate(passing[0]["linepairs"]["groups"]):
        center = [index * 15. - 35., 0.]
        group["roi"] = rect_roi(ctx, center, [12.6, 12.6], 37., f"linepairs/{group['id']}")
        group["profile_seg"] = segment(ctx, [center[0] - 8., -8.], [center[0] + 8., 8.],
                                       f"linepairs/{group['id']}/profile")
    with page.expect_response("**/api/auto-analyse") as uploaded:
        page.locator("#auto-file").set_input_files({"name": "odd-detector.png", "mimeType": "image/png", "buffer": encoded.getvalue()})
    page.locator("#auto-viewer").wait_for(state="visible")
    record = uploaded.value.json()["analyses"][0]
    geometry = record["geometry"]
    before = page.locator("#auto-overlays").inner_html()
    for group in geometry["linepairs"]["groups"]:
        points = page.locator(f'#auto-overlays polygon[data-roi-id="{group["roi"]["id"]}"]').get_attribute("points")
        assert [[float(n) for n in pair.split(",")] for pair in points.split()] == group["roi"]["corners_px"]
    results_box = page.locator("#auto-feature-status").bounding_box()
    image_box = page.locator("#auto-viewer").bounding_box()
    controls_box = page.locator(".auto-viewer-toolbar").bounding_box()
    assert results_box["y"] + results_box["height"] <= image_box["y"]
    assert controls_box["y"] >= image_box["y"] + image_box["height"]
    stroke = page.locator('#auto-overlays polygon[data-roi-id^="linepairs/"]').first.evaluate(
        "node => parseFloat(getComputedStyle(node).strokeWidth) * new DOMMatrix(getComputedStyle(node.parentNode.parentNode).transform).a")
    assert stroke == pytest.approx(2.6, rel=1e-5)
    writes = []
    page.on("request", lambda request: writes.append(request.url) if request.method != "GET" else None)
    page.locator("#auto-open-analyze").click()
    page.locator("#btn-back-d").wait_for(state="visible")
    assert page.evaluate("S.geometry") == geometry
    assert page.evaluate("ViewerTools.linepairRois(S.geometry).filter(e => e.drag).map(e => e.roi)") == [
        group["roi"] for group in geometry["linepairs"]["groups"]]
    assert page.evaluate("S.imgScale") == 1
    page.locator("#tab-auto-analyse").click()
    page.locator("#auto-viewer").wait_for(state="visible")
    assert page.locator("#auto-overlays").inner_html() == before
    assert mod.store.get(record["id"])["geometry"] == geometry
    assert not writes and not errors
    page.screenshot(path=str(tmp_path / "auto-results-desktop.png"), full_page=True)
    page.set_viewport_size({"width": 650, "height": 900})
    page.screenshot(path=str(tmp_path / "auto-results-mobile.png"), full_page=True)
    page.set_viewport_size({"width": 360, "height": 800})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / "auto-results-small-mobile.png"), full_page=True)


def test_browser_manual_registration_is_available_after_detection_failure(browser_page, mod, monkeypatch):
    from phantom_qa.registration import Registration, Transform
    import numpy as np
    page, errors = browser_page
    def detect(scan, definition, corners_hint=None):
        if corners_hint is None:
            raise ValueError("Synthetic detection failure")
        return Registration(Transform(np.array([[.16, 0.], [0., -.16]]), np.array([32., 32.])),
                            np.asarray(corners_hint), 0., {})
    monkeypatch.setattr(mod.pipeline, "run_stage_a", detect)
    send_scan(page)
    page.locator("#auto-open-analyze").click()
    page.locator("#btn-manual-corners").wait_for(state="visible")
    page.locator("#btn-manual-corners").click()
    assert page.evaluate("S.mode") == "corners"
    page.wait_for_function("S.imgEl !== null")
    canvas = page.locator("#viewer").bounding_box()
    for point in ([8, 8], [56, 8], [56, 56], [8, 56]):
        position = page.evaluate("point => nat2scr(point)", point)
        page.mouse.click(canvas["x"] + position[0], canvas["y"] + position[1])
    page.locator("#btn-confirm-a").wait_for(state="visible")
    assert not errors


def test_browser_incomplete_measurement_keeps_orange_rois_and_opens_same_error_in_analyze(browser_page, passing):
    page, errors = browser_page
    passing[1]["uniformity"]["rows"][0]["snr"] = None
    send_scan(page)
    assert page.locator("#auto-feature-status li.auto-attention").inner_text().startswith("Uniformity")
    assert page.locator("#auto-overlays [stroke='#ffb45c']").count() == 5
    assert page.locator("#auto-feature-status small, #auto-technical").count() == 0
    page.locator("#auto-open-analyze").click()
    page.locator("#view-analyze").wait_for(state="visible")
    page.locator("#stage-content").filter(has_text="Required measurement is missing or invalid").wait_for(state="visible")
    assert not page.locator("#view-auto-analyse").is_visible()
    assert not errors


def test_browser_orange_duplicate_and_failed_request(browser_page, passing):
    page, errors = browser_page
    passing[1]["uniformity"]["rows"][0]["status"] = "fail"
    send_scan(page)
    assert page.locator("#auto-feature-status li.auto-attention").count() == 1
    assert page.locator('#auto-tasks [data-task-type="repeat_scan"]').count() == 1
    assert page.locator("#auto-overlays [stroke='#ffb45c']").count() > 0
    assert page.locator("#auto-feature-status li.auto-attention").inner_text().startswith("Uniformity")
    assert "Uniformity" not in page.locator("#auto-tasks").inner_text()
    page.locator("#auto-new-scan").click()
    send_scan(page)  # exactly the same bytes
    assert page.locator('#auto-tasks [data-task-type="contact_technician"]').count() == 1
    assert page.locator('#auto-tasks [data-task-type="repeat_scan"]').count() == 0
    assert page.locator("#auto-save-note").count() == 0
    page.route("**/api/auto-analyse", lambda route: route.fulfill(status=500,
               content_type="application/json", body=json.dumps({"detail": "Test processing error"})))
    page.locator("#auto-new-scan").click()
    page.locator("#auto-file").set_input_files({"name": "scan.png", "mimeType": "image/png", "buffer": png()})
    page.locator("#auto-error").wait_for(state="visible")
    assert "The upload could not be completed" in page.locator("#auto-error").inner_text()
    assert "Test processing error" not in page.locator("#auto-error").inner_text()
    assert page.locator("#auto-drop").get_attribute("aria-disabled") == "false"
    assert not page.locator("#auto-result").is_visible()
    assert not errors


def test_browser_viewer_fits_window_levels_zooms_and_pans_without_editing(browser_page):
    page, errors = browser_page
    page.set_viewport_size({"width": 1366, "height": 768})
    with page.expect_response("**/api/auto-analyse") as response:
        send_scan(page)
    record = response.value.json()["analyses"][0]
    viewer = page.locator("#auto-viewer")
    image = page.locator("#auto-image")
    layer = page.locator("#auto-viewer-layer")
    matrix = lambda: layer.evaluate("node => { const m = new DOMMatrix(getComputedStyle(node).transform); return [m.a, m.e, m.f]; }")

    def check_fit():
        box, scan = viewer.bounding_box(), image.bounding_box()
        assert 0 < box["height"] and box["y"] + box["height"] <= page.viewport_size["height"]
        assert box["x"] <= scan["x"] and box["y"] <= scan["y"]
        assert scan["x"] + scan["width"] <= box["x"] + box["width"] + 1
        assert scan["y"] + scan["height"] <= box["y"] + box["height"] + 1
        assert page.locator("#auto-feature-status").bounding_box()["y"] < page.viewport_size["height"]

    check_fit()
    initial, overlays = matrix(), page.locator("#auto-overlays").inner_html()
    stroke = lambda: page.locator("#auto-overlays [stroke]").first.evaluate("node => parseFloat(getComputedStyle(node).strokeWidth)") * matrix()[0]
    assert stroke() == pytest.approx(2.6, rel=1e-5)
    automatic_url = image.get_attribute("src")
    writes = []
    page.on("request", lambda request: writes.append(request.url) if request.method != "GET" else None)
    page.locator("#auto-zoom-in").click()
    assert matrix()[0] == pytest.approx(initial[0] * 1.15, rel=1e-5)
    assert stroke() == pytest.approx(2.6, rel=1e-5)
    page.locator("#auto-zoom-out").click()
    assert matrix() == pytest.approx(initial, rel=1e-5)
    box = viewer.bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.wheel(0, -100)
    page.wait_for_function("node => new DOMMatrix(getComputedStyle(node).transform).a > " + str(initial[0]), arg=layer.element_handle())
    zoomed = matrix()
    page.mouse.down()
    page.mouse.move(x + 45, y + 30, steps=3)
    page.mouse.up()
    moved = matrix()
    assert moved == pytest.approx([zoomed[0], zoomed[1] + 45, zoomed[2] + 30], rel=1e-5)
    assert viewer.evaluate("node => !node.classList.contains('dragging')")

    with page.expect_response(lambda response: "/image.png?" in response.url and "wc=" in response.url) as windowed:
        page.locator("#auto-wl-width").evaluate("node => { node.value = 25; node.dispatchEvent(new Event('input')); }")
        page.locator("#auto-wl-center").evaluate("node => { node.value = 75; node.dispatchEvent(new Event('input')); }")
    params = parse_qs(urlparse(windowed.value.url).query)
    lo, hi = record["display_range"]["lo"], record["display_range"]["hi"]
    assert float(params["wc"][0]) == pytest.approx(lo + 0.75 * (hi - lo))
    assert float(params["ww"][0]) == pytest.approx(0.25 * (hi - lo))
    page.wait_for_function("url => document.getElementById('auto-image').src === url", arg=windowed.value.url)
    assert matrix() == pytest.approx(moved)
    # The automatic preview may already be in the browser's image cache.
    page.locator("#auto-wl-reset").click()
    page.wait_for_function("url => document.getElementById('auto-image').src === url", arg=automatic_url)
    assert page.locator("#auto-wl-width").input_value() == "100"
    assert page.locator("#auto-wl-center").input_value() == "50"
    assert matrix() == pytest.approx(moved)
    page.locator("#auto-zoom-fit").click()
    assert matrix() == pytest.approx(initial)
    assert page.locator("#auto-overlays").inner_html() == overlays
    assert not writes, "Display controls must never save or edit measuring regions"
    assert page.locator("#auto-overlays").evaluate("node => node.parentNode === document.getElementById('auto-image').parentNode")
    page.set_viewport_size({"width": 1100, "height": 700})
    page.wait_for_function("() => document.getElementById('auto-viewer-layer').getBoundingClientRect().bottom <= document.getElementById('auto-viewer').getBoundingClientRect().bottom")
    check_fit()
    # Hidden-tab resizes must not lose the fitted image when returning.
    page.locator("#tab-analyze").click()
    page.set_viewport_size({"width": 1280, "height": 800})
    page.locator("#tab-auto-analyse").click()
    page.wait_for_function("() => document.getElementById('auto-viewer-layer').getBoundingClientRect().bottom <= document.getElementById('auto-viewer').getBoundingClientRect().bottom")
    check_fit()
    assert not errors


def test_browser_metadata_field_correction_and_saved_revisions(browser_page, mod, alignment_case, pdef, tmp_path):
    page, errors = browser_page
    page.evaluate("""() => {
        sessionStorage.setItem('lbl_site', ' Goma ');
        sessionStorage.setItem('lbl_phantom', 'P1');
        sessionStorage.setItem('lbl_operator', 'Operator');
    }""")
    with page.expect_response("**/api/auto-analyse") as uploaded:
        send_scan(page)
    aid = uploaded.value.json()["analyses"][0]["id"]
    first = next(row for row in mod.store.revisions(aid) if row["has_report"])
    page.locator("#auto-open-analyze").click()
    page.locator("#btn-back-d").wait_for(state="visible")
    assert "Goma" in page.locator("#identity-bar").inner_text()
    assert "P1" in page.locator("#identity-bar").inner_text()
    page.locator("#btn-back-d").click()
    page.locator("#btn-back-c").click()
    page.locator("#btn-back-b").click()
    page.locator("#btn-back-a").click()
    page.locator("#btn-confirm-a").click()
    page.locator("#btn-confirm-b").click()
    held = []
    page.route("**/field_edge", lambda route: held.append(route))
    page.locator('.btn-field[data-side="top"]').click()
    point = alignment_case.mm_to_px([0., pdef.side_mm / 2 + 40]).tolist()
    position = page.evaluate("point => nat2scr(point)", point)
    canvas = page.locator("#viewer").bounding_box()
    page.mouse.click(canvas["x"] + position[0], canvas["y"] + position[1])
    page.wait_for_function("() => analysisWrites.has(S.aid)")
    page.locator("#btn-confirm-c").click()  # advance while the position request is still pending
    assert "Stage C" in page.locator("#stage-content h2").inner_text()
    assert held
    held[0].continue_()
    page.locator("#btn-confirm-d").wait_for(state="visible")
    page.locator("#btn-confirm-d").click()
    page.locator("#btn-confirm-e").wait_for(state="visible")
    stored = mod.store.get(aid)
    clicked_offset = stored["geometry"]["geometry"]["field_edges"]["top"]["offset_from_edge_mm"]
    assert clicked_offset == pytest.approx(40, abs=.5)  # screen clicks have finite pixel precision
    assert stored["results"]["geometry"]["field_alignment"]["top"]["offset_from_phantom_edge_mm"] == pytest.approx(clicked_offset)
    page.locator("#btn-confirm-e").click()
    with page.expect_response("**/finalize") as finalized:
        page.locator("#btn-finalize").click()
    assert not finalized.value.json()["report_error"]
    current = mod.store.revision(aid, finalized.value.json()["revision"])
    assert current["record"]["site"] == "Goma"
    assert current["report_html"] and mod.store.revision(aid, first["revision"])["report_html"]
    page.locator("#saved-revisions summary").click()
    page.locator('#saved-revision-list a:text("Report")').first.wait_for(state="visible")
    assert page.locator('#saved-revision-list a:text("Report")').count() == 2
    page.locator("#tab-auto-analyse").click()
    page.locator("#auto-feature-status li.auto-attention").wait_for(state="visible")
    assert "Goma" in page.locator("#auto-identity").inner_text()
    page.screenshot(path=str(tmp_path / "auto-analyze-corrected-desktop.png"), full_page=True)
    page.set_viewport_size({"width": 650, "height": 900})
    page.screenshot(path=str(tmp_path / "auto-analyze-corrected-mobile.png"), full_page=True)
    assert len(mod.store.list_all()) == 1
    assert not errors
