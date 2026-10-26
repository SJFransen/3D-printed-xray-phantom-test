"""Frontend-only navigation, closed-scan isolation and existing API journeys."""
from pathlib import Path

import pytest

from test_auto_analyse_browser import browser_page, legacy_upload_cache, send_scan, choose_scan
from test_auto_analyse import algorithms, mod, passing, png


def assert_closed(page):
    page.locator("#auto-upload-form").wait_for(state="visible")
    assert page.locator("#view-analyze").is_hidden()
    assert page.locator("#auto-results-page").is_hidden()
    assert page.locator("#auto-overlays > *, #auto-tasks > *, #auto-scan-select > *").count() == 0
    assert not page.locator("#auto-image").get_attribute("src")
    assert page.evaluate("() => S.aid === null && S.results === null && S.geometry === null && S.imgEl === null && S.record === null")
    assert page.locator("#stage-content").inner_text() == ""


def test_auto_only_entry_and_return_to_same_saved_record(browser_page, mod):
    page, errors = browser_page
    assert page.locator("#tab-analyze, #upload-workflow, #stage-nav [data-stage='U']").count() == 0
    assert page.locator("#view-analyze").is_hidden()
    send_scan(page)
    aid = mod.store.list_all()[0]["id"]
    position = page.locator("#auto-open-analyze").bounding_box()
    writes = []
    page.on("request", lambda request: writes.append(request.url) if request.method == "POST" else None)
    page.locator("#auto-open-analyze").click()
    back = page.locator("#analyze-return-auto")
    back.wait_for(state="visible")
    assert page.evaluate("S.aid") == aid
    box = back.bounding_box()
    assert box["y"] == pytest.approx(position["y"], abs=2)
    assert box["x"] + box["width"] == pytest.approx(position["x"] + position["width"], abs=2)
    preview = Path(__file__).resolve().parents[1] / ".venv" / "qa-preview"
    preview.mkdir(exist_ok=True)
    page.screenshot(path=str(preview / "frontend-analyze-navigation.png"), full_page=True)
    page.set_viewport_size({"width": 360, "height": 900})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert page.locator("#view-analyze").evaluate("node => node.scrollWidth <= node.clientWidth")
    assert back.is_visible()
    page.screenshot(path=str(preview / "frontend-analyze-navigation-mobile.png"), full_page=True)
    back.click()
    page.locator("#auto-viewer").wait_for(state="visible")
    assert aid in page.locator("#auto-image").get_attribute("src")
    assert page.locator("#view-analyze").is_hidden()
    assert page.evaluate("S.aid") is None
    assert not writes, "Opening and returning must not upload, compute or finalize"
    assert len(mod.store.list_all()) == 1
    assert not errors


@pytest.mark.parametrize("origin", ["auto", "analyze"])
def test_new_scan_closes_both_interfaces_and_next_scan_is_independent(browser_page, mod, origin):
    page, errors = browser_page
    send_scan(page, 1)
    old = mod.store.list_all()[0]["id"]
    page.locator("#auto-open-analyze").click()
    page.locator("#btn-confirm-e").wait_for(state="visible")
    if origin == "auto":
        page.locator("#analyze-return-auto").click()
        page.locator("#auto-result").wait_for(state="visible")
    page.locator("#analyze-new-scan" if origin == "analyze" else "#auto-new-scan").click()
    assert_closed(page)
    assert page.locator("#auto-open-analyze").is_disabled()
    send_scan(page, 2)
    new = next(record["id"] for record in mod.store.list_all() if record["id"] != old)
    assert new in page.locator("#auto-image").get_attribute("src")
    page.locator("#auto-open-analyze").click()
    page.wait_for_function("aid => S.aid === aid && S.record?.id === aid", arg=new)
    assert len(mod.store.list_all()) == 2, "Closing a scan must preserve its saved record"
    assert not errors


def test_new_scan_settles_pending_edit_before_closing(browser_page, mod):
    page, errors = browser_page
    send_scan(page)
    aid = mod.store.list_all()[0]["id"]
    page.locator("#auto-open-analyze").click()
    page.locator("#btn-confirm-e").wait_for(state="visible")
    held = []
    page.route("**/labels", lambda route: held.append(route) if route.request.method == "POST" else route.continue_())
    page.evaluate("aid => { window.pendingEdit = postJSON(`api/analyses/${aid}/labels`, {notes: 'Saved before closing'}); }", aid)
    page.wait_for_function("() => analysisWrites.has(S.aid)")
    page.locator("#analyze-new-scan").click()
    assert page.locator("#auto-upload-form").is_hidden()
    assert held
    held[0].continue_()
    assert_closed(page)
    assert mod.store.get(aid)["notes"] == "Saved before closing"
    assert not errors


def test_late_saved_result_response_cannot_reopen_closed_scan(browser_page, mod):
    page, errors = browser_page
    send_scan(page)
    aid = mod.store.list_all()[0]["id"]
    held = []
    page.route(f"**/api/auto-analyse/{aid}?*", lambda route: held.append(route))
    page.evaluate("aid => document.dispatchEvent(new CustomEvent('phantomqa:analysis-changed', {detail: aid}))", aid)
    page.wait_for_timeout(50)
    assert held
    page.locator("#auto-new-scan").click()
    assert_closed(page)
    with page.expect_response(lambda response: f"/api/auto-analyse/{aid}?" in response.url):
        held[0].continue_()
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    assert_closed(page)
    assert not errors


@pytest.mark.parametrize("return_control", ["#analyze-return-auto", "#tab-auto-analyse", "history"])
def test_return_from_measuring_points_completes_pending_analysis(browser_page, mod, return_control):
    page, errors = browser_page
    send_scan(page)
    aid = mod.store.list_all()[0]["id"]
    page.locator("#auto-open-analyze").click()
    page.locator("#btn-confirm-e").wait_for(state="visible")
    page.locator("#btn-back-d").click()
    page.locator("#btn-back-c").click()
    page.locator("#btn-confirm-c").wait_for(state="visible")
    page.evaluate("aid => postJSON(`api/analyses/${aid}/field_edge`, {side: 'top', point_px: [33, 6]})", aid)
    assert mod.store.get(aid)["results"] is None
    writes = []
    page.on("request", lambda request: writes.append(request.url) if request.method == "POST" else None)
    if return_control == "history":
        page.locator("#tab-history").click()
        page.locator(f'#history-table a.open[data-id="{aid}"]').click()
    else:
        page.locator(return_control).click()
    page.locator("#auto-viewer").wait_for(state="visible")
    assert mod.store.get(aid)["results"] is not None
    assert page.locator("#auto-feature-status li.auto-attention").count() == 0
    assert page.locator('#auto-tasks [data-task-type="repeat_scan"]').count() == 0
    assert len(writes) == 1 and writes[0].endswith("/compute")
    page.locator("#auto-open-analyze").click()
    page.locator("#btn-confirm-e").wait_for(state="visible")
    assert not errors


def test_return_waits_for_roi_save_and_compute_failure_can_be_retried(browser_page, mod):
    page, errors = browser_page
    send_scan(page)
    aid = mod.store.list_all()[0]["id"]
    page.locator("#auto-open-analyze").click()
    page.locator("#btn-confirm-e").wait_for(state="visible")
    held = []
    page.route("**/field_edge", lambda route: held.append(route))
    page.evaluate("aid => { window.pendingEdit = postJSON(`api/analyses/${aid}/field_edge`, {side: 'top', point_px: [33, 6]}); }", aid)
    page.wait_for_function("() => analysisWrites.has(S.aid)")
    compute = f"**/api/analyses/{aid}/compute"
    page.route(compute, lambda route: route.fulfill(status=500, content_type="application/json", body='{"detail":"Compute unavailable"}'))
    page.locator("#analyze-return-auto").click()
    assert page.locator("#view-analyze").is_visible()
    assert page.locator("#analyze-return-auto").is_disabled()
    assert held
    held[0].continue_()
    page.wait_for_function("() => document.getElementById('app-status').textContent.includes('Compute unavailable')")
    assert page.evaluate("S.aid") == aid
    assert mod.store.get(aid)["results"] is None
    assert not page.locator("#view-analyze").evaluate("node => node.inert")
    page.unroute(compute)
    page.locator("#analyze-return-auto").click()
    page.locator("#auto-viewer").wait_for(state="visible")
    assert mod.store.get(aid)["results"] is not None
    assert not errors


def test_successful_compute_is_not_repeated_after_failed_result_fetch(browser_page, mod):
    page, errors = browser_page
    send_scan(page)
    aid = mod.store.list_all()[0]["id"]
    page.locator("#auto-open-analyze").click()
    page.locator("#btn-confirm-e").wait_for(state="visible")
    page.evaluate("aid => postJSON(`api/analyses/${aid}/field_edge`, {side: 'top', point_px: [33, 6]})", aid)
    computes = []
    page.on("request", lambda request: computes.append(request.url) if request.url.endswith("/compute") else None)
    pattern = f"**/api/auto-analyse/{aid}?*"
    page.route(pattern, lambda route: route.fulfill(status=500, content_type="application/json", body='{"detail":"Result unavailable"}'))
    page.locator("#analyze-return-auto").click()
    page.wait_for_function("() => document.getElementById('app-status').textContent.includes('Result unavailable')")
    assert page.locator("#view-analyze").is_visible()
    assert mod.store.get(aid)["results"] is not None
    page.unroute(pattern)
    page.locator("#analyze-return-auto").click()
    page.locator("#auto-viewer").wait_for(state="visible")
    assert len(computes) == 1
    assert not errors


def test_history_deletion_clears_selected_auto_scan(browser_page, mod):
    from test_authorization import ADMIN_PW
    page, errors = browser_page
    send_scan(page)
    aid = mod.store.list_all()[0]["id"]
    page.locator("#tab-history").click()
    page.locator(f'#history-table a.del[data-id="{aid}"]').click()
    page.locator("#del-reason").fill("Disposable test scan")
    page.locator("#del-pw").fill(ADMIN_PW)
    page.locator("#del-confirm").click()
    page.locator(f'#history-table a.del[data-id="{aid}"]').wait_for(state="detached")
    assert page.locator("#view-history").is_visible()
    page.locator("#tab-auto-analyse").click()
    assert_closed(page)
    send_scan(page, 2)
    assert len(mod.store.list_all()) == 1
    assert not errors


def test_return_saves_changed_sid_without_confirming_dimensions(browser_page, mod):
    page, errors = browser_page
    send_scan(page)
    aid = mod.store.list_all()[0]["id"]
    page.locator("#auto-open-analyze").click()
    page.locator("#btn-confirm-e").wait_for(state="visible")
    held = []
    page.route("**/compute_preview", lambda route: held.append(route))
    page.locator("#btn-back-d").click()
    page.locator("#sid-input").fill("1500")
    page.locator("#analyze-return-auto").click()
    assert held
    held[0].continue_()
    page.locator("#auto-viewer").wait_for(state="visible")
    assert mod.store.get(aid)["sid_mm"] == 1500
    assert mod.store.get(aid)["results"] is not None
    assert not errors


def test_return_to_auto_preserves_signed_off_results(browser_page, mod):
    page, errors = browser_page
    send_scan(page)
    aid = mod.store.list_all()[0]["id"]
    mod.store.update(aid, validation_status="validated", validated_by="Reviewer")
    before = mod.store.get(aid)
    writes = []
    page.on("request", lambda request: writes.append(request.url) if request.method == "POST" else None)
    page.locator("#auto-open-analyze").click()
    page.locator('#stage-nav li.active[data-stage="F"]').wait_for(state="visible")
    page.locator("#analyze-return-auto").click()
    page.locator("#auto-viewer").wait_for(state="visible")
    after = mod.store.get(aid)
    assert after["results"] == before["results"]
    assert after["validation_status"] == "validated"
    assert after["validated_by"] == "Reviewer"
    assert not writes
    assert not errors


def test_deleted_archive_selection_falls_back_to_remaining_scan(browser_page, mod, monkeypatch):
    from phantom_qa import ingest
    from test_authorization import ADMIN_PW
    page, errors = browser_page
    scans = [ingest.load_image_bytes(png(seed), f"scan-{seed}.png") for seed in (1, 2)]
    monkeypatch.setattr(ingest, "load_any_bytes", lambda *args: scans)
    choose_scan(page, {"name": "scans.zip", "mimeType": "application/zip", "buffer": b"archive"})
    page.locator("#auto-result").wait_for(state="visible")
    page.locator("#auto-scan-select").select_option("1")
    page.locator("#auto-open-analyze").click()
    page.locator("#btn-confirm-e").wait_for(state="visible")
    selected = page.evaluate("S.aid")
    remaining = next(record["id"] for record in mod.store.list_all() if record["id"] != selected)
    page.locator("#tab-history").click()
    page.locator(f'#history-table a.del[data-id="{selected}"]').click()
    page.locator("#del-reason").fill("Disposable archive scan")
    page.locator("#del-pw").fill(ADMIN_PW)
    page.locator("#del-confirm").click()
    page.locator(f'#history-table a.del[data-id="{selected}"]').wait_for(state="detached")
    page.locator("#tab-auto-analyse").click()
    page.locator("#auto-viewer").wait_for(state="visible")
    assert remaining in page.locator("#auto-image").get_attribute("src")
    assert page.locator("#auto-scan-choice").is_hidden()
    assert page.locator("#auto-scan-select option").count() == 1
    page.locator("#auto-open-analyze").click()
    page.wait_for_function("aid => S.aid === aid", arg=remaining)
    assert not errors


@pytest.mark.parametrize("origin", ["auto", "analyze"])
def test_missing_saved_scan_recovers_to_upload(browser_page, mod, origin):
    page, errors = browser_page
    send_scan(page)
    aid = mod.store.list_all()[0]["id"]
    if origin == "analyze":
        page.locator("#auto-open-analyze").click()
        page.locator("#btn-confirm-e").wait_for(state="visible")
    page.route(f"**/api/analyses/{aid}", lambda route: route.fulfill(status=404, content_type="application/json", body='{"detail":"analysis not found"}'))
    page.route(f"**/api/auto-analyse/{aid}?*", lambda route: route.fulfill(status=404, content_type="application/json", body='{"detail":"analysis not found"}'))
    page.locator("#analyze-return-auto" if origin == "analyze" else "#tab-auto-analyse").click()
    assert_closed(page)
    assert page.locator("#auto-open-analyze").is_disabled()
    assert not errors


def test_failed_return_keeps_analyze_available_for_retry(browser_page, mod):
    page, errors = browser_page
    send_scan(page)
    aid = mod.store.list_all()[0]["id"]
    page.locator("#auto-open-analyze").click()
    page.locator("#btn-confirm-e").wait_for(state="visible")
    pattern = f"**/api/auto-analyse/{aid}?*"
    page.route(pattern, lambda route: route.fulfill(status=500, content_type="application/json", body='{"detail":"Temporarily unavailable"}'))
    page.locator("#analyze-return-auto").click()
    page.wait_for_function("() => document.getElementById('app-status').textContent.includes('Could not return')")
    assert page.locator("#view-analyze").is_visible()
    assert page.evaluate("S.aid") == aid
    page.unroute(pattern)
    page.locator("#analyze-return-auto").click()
    page.locator("#auto-viewer").wait_for(state="visible")
    assert aid in page.locator("#auto-image").get_attribute("src")
    assert not errors


def test_archive_selection_and_history_use_auto_before_analyze(browser_page, mod, monkeypatch):
    from phantom_qa import ingest
    page, errors = browser_page
    scans = [ingest.load_image_bytes(png(seed), f"scan-{seed}.png") for seed in (1, 2)]
    monkeypatch.setattr(ingest, "load_any_bytes", lambda *args: scans)
    choose_scan(page, {"name": "scans.zip", "mimeType": "application/zip", "buffer": b"archive"})
    page.locator("#auto-result").wait_for(state="visible")
    page.locator("#auto-scan-select").select_option("1")
    page.locator("#auto-open-analyze").click()
    page.locator("#btn-confirm-e").wait_for(state="visible")
    selected = page.evaluate("S.aid")
    page.locator("#analyze-return-auto").click()
    page.locator("#auto-viewer").wait_for(state="visible")
    assert page.locator("#auto-scan-select").input_value() == "1"
    assert selected in page.locator("#auto-image").get_attribute("src")
    page.locator("#auto-new-scan").click()
    assert_closed(page)
    page.locator("#tab-history").click()
    page.locator(f'#history-table a.open[data-id="{selected}"]').click()
    page.locator("#auto-viewer").wait_for(state="visible")
    assert page.locator("#view-analyze").is_hidden()
    assert selected in page.locator("#auto-image").get_attribute("src")
    assert page.locator("#auto-scan-choice").is_hidden()
    page.locator("#auto-open-analyze").click()
    page.wait_for_function("aid => S.aid === aid", arg=selected)
    assert not errors
