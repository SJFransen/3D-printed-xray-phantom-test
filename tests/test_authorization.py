"""Pre-deployment authorization audit.

Two privilege levels are claimed:

  anonymous : nothing except the login page and the sign-in endpoint
  user      : password login — may run analyses, read data, export
  admin     : additionally holds the administrator password — may DELETE and
              VALIDATE

These tests enumerate EVERY route on the app and assert the level each one
actually enforces, so a newly added endpoint cannot silently become public.
"""

from __future__ import annotations

import importlib
import io
import os
import sys

import pytest
from fastapi.testclient import TestClient

USER_PW = "user-password-1234"
ADMIN_PW = "admin-password-5678"


# --------------------------------------------------------------- app fixtures

def _build_app(tmp_path, monkeypatch, **env):
    """Import a fresh app instance with a throwaway database and log dir."""
    from phantom_qa.security import hash_password
    defaults = {
        "PHANTOMQA_ENV": "development",
        "PHANTOMQA_AUTH_ENABLED": "true",
        "PHANTOMQA_USERNAME": "qa",
        "PHANTOMQA_PASSWORD_HASH": hash_password(USER_PW, rounds=1000),
        "PHANTOMQA_ADMIN_PASSWORD_HASH": hash_password(ADMIN_PW, rounds=1000),
        "PHANTOMQA_SECRET_KEY": "k" * 50,
        "PHANTOMQA_HTTPS_ONLY": "false",
        "PHANTOMQA_LOG_CONSOLE": "false",
        "PHANTOMQA_LOG_DIR": str(tmp_path / "logs"),
        "PHANTOMQA_ROOT_PATH": "",
    }
    # clear inherited values FIRST, then apply, so a caller can set them
    for k in ("PHANTOMQA_PASSWORD", "PHANTOMQA_ALLOWED_HOSTS",
              "PHANTOMQA_MAX_UPLOAD_MB"):
        monkeypatch.delenv(k, raising=False)
    defaults.update(env)
    for k, v in defaults.items():
        monkeypatch.setenv(k, v)

    import phantom_qa.config as config
    import phantom_qa.logging_setup as ls
    config._config = None
    ls._configured = False
    import logging
    for name in (ls.APP_LOGGER, ls.AUDIT_LOGGER):
        logging.getLogger(name).handlers.clear()

    import phantom_qa.webapp.main as main
    monkeypatch.setattr(main, "ROOT", str(tmp_path), raising=False)
    main = importlib.reload(main)
    # point the reloaded module's store at the temp dir
    from phantom_qa.store import Store
    main.store = Store(str(tmp_path))
    from phantom_qa.security import SharedThrottle
    main.throttle = SharedThrottle(main.store.db_path, "login", 8, 15)
    main.admin_throttle = SharedThrottle(main.store.db_path, "admin", 4, 15)
    return main


@pytest.fixture()
def app_mod(tmp_path, monkeypatch):
    return _build_app(tmp_path, monkeypatch)


@pytest.fixture()
def client(app_mod):
    return TestClient(app_mod.app)


def _login(client, password=USER_PW, username="qa"):
    r = client.post("/api/login", json={"username": username,
                                        "password": password})
    assert r.status_code == 200, r.text
    return r.json()["csrf"]


@pytest.fixture()
def user_client(app_mod):
    c = TestClient(app_mod.app)
    csrf = _login(c)
    c.headers.update({"X-CSRF-Token": csrf})
    return c


@pytest.fixture()
def analysis(user_client, tmp_path):
    """A stored analysis with results, created without touching the pipeline."""
    from phantom_qa.store import Store
    from test_store_labels import fake_scan, results_for
    import phantom_qa.webapp.main as m
    store = m.store
    aid = store.new_analysis(fake_scan(), b"payload-bytes", "sig", "1.0.0",
                             "1.0", labels={"site": "Goma", "phantom": "P1"})
    store.update(aid, results=results_for(), status="pass", geometry={})
    return aid


# ------------------------------------------------------- the route inventory

# level: "public" | "user" | "admin"
ROUTES = [
    # (method, path template, level)
    ("GET",  "/api/auth",                          "public"),
    ("POST", "/api/login",                         "public"),
    ("GET",  "/login",                             "public"),
    ("GET",  "/style.css",                         "public"),
    ("GET",  "/login.js",                          "public"),

    ("POST", "/api/logout",                        "user"),
    ("GET",  "/",                                  "user"),
    ("GET",  "/app.js",                            "user"),
    ("GET",  "/viewer-tools.js",                   "user"),
    ("GET",  "/upload-form.js",                    "user"),
    ("GET",  "/api/analyses",                      "user"),
    ("POST", "/api/analyses",                      "user"),
    ("POST", "/api/uploads/preview", "user"),
    ("GET", "/api/solutions/{asset_path:path}", "user"),
    ("POST", "/api/auto-analyse",                  "user"),
    ("GET",  "/api/auto-analyse/{aid}",            "user"),
    ("GET",  "/auto-analyse.js",                   "user"),
    ("GET",  "/auto-analyse.css",                  "user"),
    ("GET",  "/api/labels",                        "user"),
    ("GET",  "/api/phantom_profiles",              "user"),
    ("GET",  "/api/signatures",                    "user"),
    ("GET",  "/api/trends",                        "user"),
    ("GET",  "/api/deletion_policy",               "user"),
    ("GET",  "/api/validation_policy",             "user"),
    ("GET",  "/api/export.csv",                    "user"),
    ("GET",  "/api/comparison_report.html",        "user"),
    ("GET",  "/api/analyses/{aid}",                "user"),
    ("GET",  "/api/analyses/{aid}/image.png",      "user"),
    ("GET",  "/api/analyses/{aid}/roi_stats",      "user"),
    ("GET",  "/api/analyses/{aid}/verify",         "user"),
    ("GET",  "/api/analyses/{aid}/export.json",    "user"),
    ("GET",  "/api/analyses/{aid}/export.csv",     "user"),
    ("GET",  "/api/analyses/{aid}/report.html",    "user"),
    ("GET",  "/api/analyses/{aid}/revisions",       "user"),
    ("GET",  "/api/analyses/{aid}/revisions/{revision}", "user"),
    ("POST", "/api/analyses/{aid}/labels",         "user"),
    ("POST", "/api/analyses/{aid}/register",       "user"),
    ("POST", "/api/analyses/{aid}/propose",        "user"),
    ("POST", "/api/analyses/{aid}/roi",            "user"),
    ("POST", "/api/analyses/{aid}/roi_rotate",     "user"),
    ("POST", "/api/analyses/{aid}/lowcontrast_block", "user"),
    ("POST", "/api/analyses/{aid}/compute_preview", "user"),
    ("POST", "/api/analyses/{aid}/field_edge",     "user"),
    ("POST", "/api/analyses/{aid}/confirm",        "user"),
    ("POST", "/api/analyses/{aid}/geometry/undo",  "user"),
    ("POST", "/api/analyses/{aid}/geometry/redo",  "user"),
    ("POST", "/api/analyses/{aid}/geometry/reset", "user"),
    ("POST", "/api/analyses/{aid}/compute",        "user"),
    ("POST", "/api/analyses/{aid}/finalize",       "user"),
    ("POST", "/api/analyses/{aid}/baseline",       "user"),
    ("GET",  "/api/baselines",                     "user"),

    ("POST", "/api/analyses/{aid}/delete",         "admin"),
    ("POST", "/api/analyses/{aid}/validation",     "admin"),
    ("POST", "/api/phantom_profiles/forget",       "admin"),
]


def test_route_inventory_is_complete(app_mod):
    """Every mounted route must appear in ROUTES.

    This is the guard that matters: adding an endpoint without classifying it
    fails here, so nothing can be shipped unclassified."""
    listed = {(m, p) for m, p, _ in ROUTES}
    actual = set()
    for r in app_mod.app.routes:
        path = getattr(r, "path", None)
        methods = getattr(r, "methods", None) or set()
        if not path or path.startswith("/openapi"):
            continue
        for meth in methods:
            if meth in ("HEAD", "OPTIONS"):
                continue
            actual.add((meth, path))
    # static mount serves /style.css, /login.js, /app.js etc. under one route
    actual = {(m, p) for m, p in actual if p != "/{path:path}"}
    missing = actual - listed
    assert not missing, (
        f"routes not classified in ROUTES: {sorted(missing)} — "
        f"classify them as public/user/admin before deploying")


# ------------------------------------------------- anonymous access is denied

@pytest.mark.parametrize("method,path,level",
                         [r for r in ROUTES if r[2] != "public"])
def test_anonymous_is_rejected(client, analysis, method, path, level):
    url = path.replace("{asset_path:path}", "line-pair/step%201/image.png").format(aid=analysis, revision=1)
    r = client.request(method, url, json={}, headers={"X-CSRF-Token": "x"})
    assert r.status_code in (401, 403), (
        f"{method} {url} returned {r.status_code} to an ANONYMOUS caller")
    if url.startswith("/api/"):
        assert r.headers.get("content-type", "").startswith("application/json")
        assert r.json()["detail"] in ("Authentication required",
                                      "CSRF token missing or invalid")


@pytest.mark.parametrize("method,path,level",
                         [r for r in ROUTES if r[2] != "public"])
def test_anonymous_never_leaks_data(client, analysis, method, path, level):
    """A denied response must not contain any stored content."""
    url = path.replace("{asset_path:path}", "line-pair/step%201/image.png").format(aid=analysis, revision=1)
    r = client.request(method, url, json={}, headers={"X-CSRF-Token": "x"})
    body = r.text.lower()
    for secret in ("goma", "sha256", "phantom_qa.sqlite3", "traceback"):
        assert secret not in body, f"{method} {url} leaked {secret!r}"


def test_public_endpoints_work_without_a_session(client):
    assert client.get("/api/auth").status_code == 200
    assert client.get("/login").status_code == 200
    assert client.get("/style.css").status_code == 200
    assert client.get("/login.js").status_code == 200


def test_auth_endpoint_reveals_nothing_when_anonymous(client):
    body = client.get("/api/auth").json()
    assert body["authenticated"] is False
    assert body.get("user") in (None, "")
    assert "password" not in str(body).lower()


# ------------------------------------------------------ user level is allowed

@pytest.mark.parametrize("method,path", [(m, p) for m, p, lv in ROUTES
                                         if lv == "user"])
def test_user_is_not_blocked_by_auth(app_mod, analysis, method, path):
    """A signed-in user must get past the auth layer on every user route.

    The endpoint may still answer 4xx/5xx for a bogus body or an undecodable
    fixture file — what must never happen is 401/403."""
    c = TestClient(app_mod.app, raise_server_exceptions=False)
    csrf = _login(c)
    c.headers.update({"X-CSRF-Token": csrf})
    url = path.replace("{asset_path:path}", "line-pair/step%201/image.png").format(aid=analysis, revision=1)
    r = c.request(method, url, json={})
    assert r.status_code not in (401, 403), (
        f"{method} {url} refused an authenticated user ({r.status_code})")


# ------------------------------------------------------- admin level enforced

@pytest.mark.parametrize("path,body", [
    ("/api/analyses/{aid}/delete", {"reason": "regression test"}),
    ("/api/analyses/{aid}/validation",
     {"status": "validated", "validated_by": "Someone"}),
])
def test_admin_routes_reject_a_plain_user(user_client, analysis, path, body):
    url = path.replace("{asset_path:path}", "line-pair/step%201/image.png").format(aid=analysis, revision=1)
    payload = {k: v.format(aid=analysis) if isinstance(v, str) else v
               for k, v in body.items()}
    for pw in ("", "wrong", USER_PW):
        r = user_client.post(url, json={**payload, "admin_password": pw})
        assert r.status_code == 401, (
            f"{url} accepted admin_password={pw!r} -> {r.status_code}")


def test_admin_password_is_not_the_user_password(user_client, analysis):
    """The everyday login must never work as the administrator credential."""
    r = user_client.post(f"/api/analyses/{analysis}/validation",
                         json={"status": "validated", "validated_by": "X",
                               "admin_password": USER_PW})
    assert r.status_code == 401


def test_admin_can_validate(user_client, analysis):
    r = user_client.post(f"/api/analyses/{analysis}/validation",
                         json={"status": "validated", "validated_by": "Dr A",
                               "comment": "ok", "admin_password": ADMIN_PW})
    assert r.status_code == 200, r.text
    rec = user_client.get(f"/api/analyses/{analysis}").json()
    assert rec["validation_status"] == "validated"
    assert rec["validated_by"] == "Dr A"


def test_admin_can_delete(user_client, analysis):
    r = user_client.post(f"/api/analyses/{analysis}/delete",
                         json={"admin_password": ADMIN_PW,
                               "reason": "duplicate upload, wrong phantom"})
    assert r.status_code == 200, r.text
    assert user_client.get(f"/api/analyses/{analysis}").status_code == 404


@pytest.mark.parametrize("reason", ["", "   ", "why", "abcd"])
def test_delete_requires_a_written_reason(user_client, analysis, reason):
    """The audit log is the only record of why data was destroyed.

    Refused with 400 rather than 422: the frontend shows `detail` verbatim, and
    a pydantic validation error would put a list of dicts there."""
    r = user_client.post(f"/api/analyses/{analysis}/delete",
                         json={"admin_password": ADMIN_PW, "reason": reason})
    assert r.status_code == 400, r.text
    assert isinstance(r.json()["detail"], str)
    assert user_client.get(f"/api/analyses/{analysis}").status_code == 200


def test_delete_checks_the_password_before_the_reason(user_client, analysis):
    """A missing reason must not reveal that the password was right."""
    r = user_client.post(f"/api/analyses/{analysis}/delete",
                         json={"admin_password": "wrong", "reason": ""})
    assert r.status_code == 401


def test_validation_requires_an_approver_name(user_client, analysis):
    r = user_client.post(f"/api/analyses/{analysis}/validation",
                         json={"status": "validated", "validated_by": "  ",
                               "admin_password": ADMIN_PW})
    assert r.status_code == 400


def test_admin_actions_disabled_without_admin_password(tmp_path, monkeypatch):
    m = _build_app(tmp_path, monkeypatch, PHANTOMQA_ADMIN_PASSWORD_HASH="")
    c = TestClient(m.app)
    csrf = _login(c)
    c.headers.update({"X-CSRF-Token": csrf})
    from test_store_labels import fake_scan, results_for
    aid = m.store.new_analysis(fake_scan(), b"x", "sig", "1", "1", labels={})
    m.store.update(aid, results=results_for())
    for url, body in ((f"/api/analyses/{aid}/delete",
                       {"admin_password": "x", "reason": "no longer needed"}),
                      (f"/api/analyses/{aid}/validation",
                       {"status": "validated", "validated_by": "X",
                        "admin_password": "x"})):
        r = c.post(url, json=body)
        assert r.status_code == 403, f"{url} -> {r.status_code}"
    assert m.store.get(aid) is not None


# ------------------------------------------------------------------ CSRF

@pytest.mark.parametrize("method,path", [(m, p) for m, p, lv in ROUTES
                                         if lv in ("user", "admin")
                                         and m in ("POST", "DELETE")])
def test_state_changing_requests_need_csrf(app_mod, analysis, method, path):
    c = TestClient(app_mod.app)
    _login(c)                                   # session cookie, no CSRF header
    url = path.replace("{asset_path:path}", "line-pair/step%201/image.png").format(aid=analysis, revision=1)
    r = c.request(method, url, json={})
    assert r.status_code == 403, (
        f"{method} {url} accepted a request with no CSRF header")
    assert r.json()["detail"] == "CSRF token missing or invalid"


def test_csrf_token_must_match_the_cookie(app_mod, analysis):
    c = TestClient(app_mod.app)
    _login(c)
    r = c.post(f"/api/analyses/{analysis}/labels",
               json={"site": "X"}, headers={"X-CSRF-Token": "not-the-token"})
    assert r.status_code == 403


def test_get_requests_do_not_need_csrf(user_client, analysis):
    c = TestClient(user_client.app)
    _login(c)
    assert c.get("/api/analyses").status_code == 200


# ------------------------------------------------------ session integrity

def test_forged_session_cookie_is_rejected(app_mod):
    from phantom_qa.security import SESSION_COOKIE, issue_session
    c = TestClient(app_mod.app)
    c.cookies.set(SESSION_COOKIE, issue_session("a-different-secret", "qa", 12))
    assert c.get("/api/analyses").status_code == 401


def test_expired_session_is_rejected(app_mod):
    import time
    from phantom_qa.security import SESSION_COOKIE, issue_session
    c = TestClient(app_mod.app)
    c.cookies.set(SESSION_COOKIE, issue_session("k" * 50, "qa", 0))
    time.sleep(1.1)
    assert c.get("/api/analyses").status_code == 401


def test_logout_clears_the_session(user_client):
    assert user_client.get("/api/analyses").status_code == 200
    user_client.post("/api/logout")
    user_client.cookies.clear()
    assert user_client.get("/api/analyses").status_code == 401


def test_session_cookie_flags(app_mod, tmp_path, monkeypatch):
    m = _build_app(tmp_path, monkeypatch, PHANTOMQA_HTTPS_ONLY="true",
                   PHANTOMQA_ROOT_PATH="/x-ray")
    c = TestClient(m.app)
    r = c.post("/api/login", json={"username": "qa", "password": USER_PW})
    raw = r.headers.get("set-cookie", "")
    assert "httponly" in raw.lower(), "session cookie must be HttpOnly"
    assert "samesite=strict" in raw.lower().replace(" ", "")
    assert "secure" in raw.lower()
    assert "path=/x-ray/" in raw.lower(), \
        "cookie must be scoped to the mount, not the whole domain"


# --------------------------------------------------- path-matching bypasses

@pytest.mark.parametrize("url", [
    "/api/analyses/",          # trailing slash
    "//api/analyses",          # duplicate slash
    "/api//analyses",
    "/API/analyses",           # case
    "/api/analyses/../analyses",
])
def test_path_tricks_do_not_bypass_auth(client, url):
    r = client.get(url)
    assert r.status_code in (401, 403, 404, 307), (
        f"{url} returned {r.status_code} to an anonymous caller")
    assert r.status_code != 200


def test_public_allowlist_is_exact(client):
    """A path that merely starts with a public one must not be public."""
    for url in ("/api/loginX", "/api/login/extra", "/logins", "/login/x"):
        r = client.get(url)
        assert r.status_code != 200, f"{url} was served to an anonymous caller"


# --------------------------------------------------------- response hardening

def test_security_headers_present(user_client):
    r = user_client.get("/api/analyses")
    h = r.headers
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["X-Frame-Options"] == "DENY"
    assert h["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in h["Content-Security-Policy"]
    assert h["Cache-Control"] == "no-store", "API data must not be cached"


def test_hsts_only_when_https(tmp_path, monkeypatch):
    m = _build_app(tmp_path, monkeypatch, PHANTOMQA_HTTPS_ONLY="true")
    c = TestClient(m.app)
    assert "Strict-Transport-Security" in c.get("/login").headers


def test_api_docs_are_disabled(client):
    for url in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(url).status_code != 200, f"{url} is exposed"


def test_unknown_analysis_is_a_clean_404(user_client):
    r = user_client.get("/api/analyses/does-not-exist/report.html")
    assert r.status_code == 404
    body = r.text.lower()
    assert "traceback" not in body and 'file "' not in body


def test_server_errors_do_not_leak_tracebacks(app_mod, analysis):
    """An unhandled exception must return a bare 500, never a stack trace.

    The stored file in this fixture is not a real DICOM, so any endpoint that
    decodes it exercises the failure path."""
    c = TestClient(app_mod.app, raise_server_exceptions=False)
    csrf = _login(c)
    c.headers.update({"X-CSRF-Token": csrf})
    for url in (f"/api/analyses/{analysis}/image.png",
                f"/api/analyses/{analysis}/propose"):
        r = c.request("POST" if "propose" in url else "GET", url, json={})
        body = r.text.lower()
        assert "traceback" not in body, f"{url} leaked a traceback"
        assert 'file "' not in body, f"{url} leaked a source path"
        assert "site-packages" not in body
        assert r.status_code != 200 or "png" in r.headers.get("content-type", "")


def test_undecodable_stored_file_is_reported_cleanly(user_client, analysis):
    """A corrupted stored file is a data problem — it should say so, not 500."""
    r = user_client.get(f"/api/analyses/{analysis}/image.png")
    assert r.status_code == 422
    assert "could not be decoded" in r.json()["detail"]


def test_missing_stored_file_is_reported_cleanly(user_client, analysis):
    import phantom_qa.webapp.main as m
    m._scans.pop(analysis, None)
    os.remove(m.store.upload_path(analysis))
    r = user_client.get(f"/api/analyses/{analysis}/image.png")
    assert r.status_code == 410
    assert "missing" in r.json()["detail"].lower()


def test_upload_size_is_capped(tmp_path, monkeypatch):
    m = _build_app(tmp_path, monkeypatch, PHANTOMQA_MAX_UPLOAD_MB="1")
    c = TestClient(m.app)
    csrf = _login(c)
    r = c.post("/api/analyses",
               files={"file": ("big.dcm", io.BytesIO(b"0" * 2_000_000))},
               headers={"X-CSRF-Token": csrf, "Content-Length": "2000000"}, data={"site": "Test hospital"})
    assert r.status_code == 413


def test_host_header_allowlist(tmp_path, monkeypatch):
    m = _build_app(tmp_path, monkeypatch,
                   PHANTOMQA_ALLOWED_HOSTS="phantomqa.example.org")
    c = TestClient(m.app)
    assert c.get("/login", headers={"Host": "evil.example.com"}).status_code == 400
    assert c.get("/login",
                 headers={"Host": "phantomqa.example.org"}).status_code == 200


# ------------------------------------------------------------- brute force

def test_login_throttle_locks_out(app_mod):
    c = TestClient(app_mod.app)
    codes = [c.post("/api/login",
                    json={"username": "qa", "password": "wrong"}).status_code
             for _ in range(12)]
    assert 429 in codes, "repeated failures must eventually be throttled"


def test_admin_throttle_is_separate_and_stricter(user_client, analysis, app_mod):
    codes = []
    for _ in range(6):
        codes.append(user_client.post(
            f"/api/analyses/{analysis}/validation",
            json={"status": "validated", "validated_by": "X",
                  "admin_password": "wrong"}).status_code)
    assert 429 in codes, "admin guesses must be throttled"


def test_throttle_state_is_shared_not_per_process(tmp_path, monkeypatch):
    """Gunicorn runs several workers; an in-memory counter would multiply the
    allowed guesses by the worker count."""
    from phantom_qa.security import SharedThrottle
    db = str(tmp_path / "t.sqlite3")
    a = SharedThrottle(db, "login", 3, 15)
    b = SharedThrottle(db, "login", 3, 15)      # a second 'worker'
    for _ in range(3):
        a.record_failure("10.0.0.9")
    assert b.locked_for("10.0.0.9") > 0, \
        "a second worker must see failures recorded by the first"
    b.reset("10.0.0.9")
    assert a.locked_for("10.0.0.9") == 0


def test_throttle_scopes_are_independent(tmp_path):
    from phantom_qa.security import SharedThrottle
    db = str(tmp_path / "t2.sqlite3")
    login = SharedThrottle(db, "login", 2, 15)
    admin = SharedThrottle(db, "admin", 2, 15)
    login.record_failure("c")
    login.record_failure("c")
    assert login.locked_for("c") > 0
    assert admin.locked_for("c") == 0


# ------------------------------------------------------ sub-path deployment

def test_sub_path_mount_prefix_stripped_by_proxy(tmp_path, monkeypatch):
    """nginx `proxy_pass http://app/;` (trailing slash) strips the prefix, so
    the app sees /login while root_path says /x-ray."""
    m = _build_app(tmp_path, monkeypatch, PHANTOMQA_ROOT_PATH="/x-ray")
    c = TestClient(m.app, root_path="/x-ray")
    r = c.get("/login")
    assert r.status_code == 200
    assert '<base href="/x-ray/">' in r.text, \
        "the login page must tell the browser where the app is mounted"


def test_sub_path_mount_prefix_forwarded(tmp_path, monkeypatch):
    """nginx `proxy_pass http://app;` (no trailing slash) forwards the whole
    path, so the app sees /x-ray/login. Both styles must work."""
    m = _build_app(tmp_path, monkeypatch, PHANTOMQA_ROOT_PATH="/x-ray")
    c = TestClient(m.app, root_path="/x-ray")
    r = c.get("/x-ray/login")
    assert r.status_code == 200, "prefix-forwarding proxy layout must work"
    assert '<base href="/x-ray/">' in r.text

    # and a signed-in session reaches the app root through the same prefix
    r = c.post("/x-ray/api/login", json={"username": "qa", "password": USER_PW})
    assert r.status_code == 200, r.text
    c.headers.update({"X-CSRF-Token": r.json()["csrf"]})
    r = c.get("/x-ray/")
    assert r.status_code == 200
    assert '<base href="/x-ray/">' in r.text


def test_sub_path_cookie_is_not_sent_to_other_apps(tmp_path, monkeypatch):
    """The session cookie is scoped to /x-ray/, so a different app on the same
    domain never receives it."""
    m = _build_app(tmp_path, monkeypatch, PHANTOMQA_ROOT_PATH="/x-ray")
    c = TestClient(m.app, root_path="/x-ray")
    r = c.post("/x-ray/api/login", json={"username": "qa", "password": USER_PW})
    assert r.status_code == 200
    assert "path=/x-ray/" in r.headers.get("set-cookie", "").lower()
    # a request outside the mount carries no session -> denied
    assert c.get("/api/analyses").status_code == 401


def test_sub_path_auth_still_enforced(tmp_path, monkeypatch):
    m = _build_app(tmp_path, monkeypatch, PHANTOMQA_ROOT_PATH="/x-ray")
    c = TestClient(m.app, root_path="/x-ray")
    assert c.get("/api/analyses").status_code == 401


def test_frontend_uses_relative_urls():
    """With <base href> in place, an absolute '/api/...' would break the app
    when it is mounted under /x-ray/."""
    static = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "phantom_qa", "webapp", "static")
    for name in ("app.js", "login.js"):
        src = open(os.path.join(static, name), encoding="utf-8").read()
        for bad in ('"/api/', "`/api/", '"/login"', 'href="/api/'):
            assert bad not in src, f"{name} contains an absolute URL {bad!r}"


def test_root_path_normalisation(tmp_path, monkeypatch):
    from phantom_qa.config import Config
    for given, expected in (("/x-ray", "/x-ray"), ("x-ray", "/x-ray"),
                            ("/x-ray/", "/x-ray"), ("", ""), ("/", "")):
        monkeypatch.setenv("PHANTOMQA_ROOT_PATH", given)
        monkeypatch.setenv("PHANTOMQA_AUTH_ENABLED", "false")
        c = Config(env_path=str(tmp_path / "none.env"))
        assert c.root_path == expected, f"{given!r} -> {c.root_path!r}"
        assert c.cookie_path == (expected + "/" if expected else "/")


# -------------------------------------------------- data exposure specifics

def test_report_and_exports_require_auth(client, analysis):
    for url in (f"/api/analyses/{analysis}/report.html",
                f"/api/analyses/{analysis}/export.json",
                f"/api/analyses/{analysis}/export.csv",
                "/api/export.csv",
                "/api/comparison_report.html"):
        r = client.get(url)
        assert r.status_code == 401, f"{url} served anonymously"


def test_image_endpoint_requires_auth(client, analysis):
    r = client.get(f"/api/analyses/{analysis}/image.png")
    assert r.status_code == 401
    assert not r.content.startswith(b"\x89PNG")


def test_export_json_has_no_secrets(user_client, analysis):
    body = user_client.get(f"/api/analyses/{analysis}/export.json").text.lower()
    for secret in ("password", "secret_key", "pbkdf2", "admin_password"):
        assert secret not in body, f"export.json leaked {secret!r}"


def test_login_does_not_reveal_which_field_was_wrong(client):
    a = client.post("/api/login", json={"username": "nope",
                                        "password": USER_PW})
    b = client.post("/api/login", json={"username": "qa",
                                        "password": "nope"})
    assert a.status_code == b.status_code == 401
    assert a.json()["detail"] == b.json()["detail"]
