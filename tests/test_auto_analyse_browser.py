"""Optional real-browser acceptance checks (pip install playwright uvicorn).

Uses local Chrome or Playwright Chromium; no browser download at test time.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time

import pytest

from test_auto_analyse import algorithms, mod, passing, png  # shared fixtures
from test_authorization import USER_PW


@pytest.fixture
def browser_page(mod, algorithms):
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
    page.locator("#auto-submit").click()
    page.locator("#auto-result").wait_for(state="visible")
    page.locator("#auto-viewer").wait_for(state="visible")


def test_browser_upload_pass_viewer_tasks_and_navigation(browser_page):
    page, errors = browser_page
    assert page.locator("header nav button").first.inner_text() == "Auto Analyze"
    assert page.locator("#view-auto-analyse").is_visible()
    assert not page.locator("#view-analyze").is_visible()
    assert page.locator("#auto-drop").is_visible()
    assert page.locator("#auto-upload-form .auto-labels input:visible").count() == 4
    assert page.locator("#auto-submit").is_disabled()
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


def test_browser_incomplete_measurement_keeps_orange_rois_and_opens_same_error_in_analyze(browser_page, passing):
    page, errors = browser_page
    passing[1]["uniformity"]["rows"][0]["snr"] = None
    send_scan(page)
    assert page.locator("#auto-feature-status li.auto-attention").inner_text() == "Uniformity"
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
    assert page.locator("#auto-feature-status li.auto-attention").inner_text() == "Uniformity"
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
    page.locator("#auto-submit").click()
    page.locator("#auto-error").wait_for(state="visible")
    assert "The upload could not be completed" in page.locator("#auto-error").inner_text()
    assert "Test processing error" not in page.locator("#auto-error").inner_text()
    assert page.locator("#auto-submit").is_enabled()
    assert not page.locator("#auto-result").is_visible()
    assert not errors
