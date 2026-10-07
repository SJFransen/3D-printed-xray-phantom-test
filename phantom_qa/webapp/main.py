"""FastAPI backend for the phantom QA wizard.

Run with:  python run_app.py   (or: uvicorn phantom_qa.webapp.main:app)
"""

from __future__ import annotations

import html as _html
import io
import logging
import math
import os
import time

html_escape = _html.escape

import numpy as np
from fastapi import (FastAPI, File, Form, HTTPException, Request, Response,
                     UploadFile)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, conlist, field_validator

from .. import ALGO_VERSION
from .. import ingest, layout_profile, pipeline
from ..analysis import linepairs
from ..analysis.common import roi_center_from_px
from ..comparison_report import build_comparison_report
from ..config import get_config
from ..logging_setup import audit, get_logger, setup_logging
from ..phantom_def import load_default
from ..registration import Registration, Transform
from ..report import build_report
from ..security import (CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE, SharedThrottle,
                        csrf_ok, issue_session, new_csrf_token, read_session,
                        verify_password)
from ..store import (VALIDATION_LABELS, VALIDATION_STATES, Store,
                     acquisition_flag, csv_export, flatten_results,
                     wide_csv_export)

#: A deletion reason short enough to be meaningless is the same as none at all,
#: and the audit log is the only record of why data was destroyed.
MIN_DELETE_REASON_CHARS = 5
MAX_DELETE_REASON_CHARS = 500

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
cfg = get_config()

log_dir = cfg.log_dir if os.path.isabs(cfg.log_dir) \
    else os.path.join(ROOT, cfg.log_dir)
setup_logging(log_dir, level=cfg.log_level, max_mb=cfg.log_max_mb,
              backups=cfg.log_backups, audit_backups=cfg.log_audit_backups,
              console=cfg.log_console)
log = get_logger("web")
log.info("starting: %s", cfg.summary())
if not cfg.deletion_enabled:
    log.warning("deletion is DISABLED (no PHANTOMQA_ADMIN_PASSWORD_HASH set)")

app = FastAPI(title="MSF Phantom QA", docs_url=None, redoc_url=None,
              openapi_url=None, root_path=cfg.root_path)
store = Store(ROOT)
pdef = load_default()

# Shared across gunicorn workers — an in-process counter would give an attacker
# max_attempts x worker_count guesses.
throttle = SharedThrottle(store.db_path, "login", cfg.max_login_attempts,
                          cfg.lockout_minutes)
admin_throttle = SharedThrottle(store.db_path, "admin",
                                max(cfg.max_login_attempts // 2, 3),
                                cfg.lockout_minutes)

# Per-process caches: under gunicorn each worker has its own copy, so nothing
# here may be treated as authoritative. _regs in particular is keyed by the
# stored registration itself, not by the analysis id — otherwise a worker that
# served an earlier request keeps handing out a transform that a /register on
# another worker has since replaced, and every pixel coordinate derived from it
# would be wrong.
_scans: dict[str, ingest.ScanData] = {}       # id -> ScanData cache
_regs: dict[tuple, Registration] = {}         # (id, reg fingerprint) -> Registration
_img_cache: dict[tuple, bytes] = {}


def _forget(aid: str):
    """Drop every cached artefact of one analysis in THIS worker."""
    _scans.pop(aid, None)
    for key in [k for k in _regs if k[0] == aid]:
        _regs.pop(key, None)
    for key in [k for k in _img_cache if k[0] == aid]:
        _img_cache.pop(key, None)


# ------------------------------------------------------------- security layer

# Everything else requires a session. Kept as an explicit allow-list so a new
# route is private by default — adding an endpoint can never accidentally
# publish it.
_PUBLIC_PATHS = frozenset({"/login", "/api/login", "/api/auth", "/style.css",
                           "/login.js", "/favicon.ico"})


def _app_path(request: Request) -> str:
    """Path relative to the mount point, normalised.

    Behind nginx at /x-ray/ the incoming path may or may not carry the prefix
    depending on how proxy_pass is written, so strip it if present. Duplicate
    slashes are collapsed and a trailing slash removed so that '/api/login/'
    or '//api/login' cannot dodge the allow-list comparison."""
    p = request.url.path
    rp = (request.scope.get("root_path") or "")
    if rp and p.startswith(rp):
        p = p[len(rp):] or "/"
    while "//" in p:
        p = p.replace("//", "/")
    if len(p) > 1 and p.endswith("/"):
        p = p.rstrip("/") or "/"
    return p


def _client_key(request: Request) -> str:
    """The client address used for throttling and the audit log.

    Deliberately does NOT read X-Forwarded-For itself. The ASGI server already
    does that — uvicorn's ProxyHeadersMiddleware (the equivalent of WSGI's
    ProxyFix) rewrites the client address from the header, but ONLY when the
    immediate peer is listed in `forwarded_allow_ips`. Parsing the header here
    as well would throw that trust boundary away: anything able to reach
    gunicorn directly on the loopback — another app on the same VM, a local
    user, an SSRF in a colocated service — could then forge an address per
    request and get unlimited password guesses.

    So: trust the server's answer, and make sure the server is configured with
    the right `forwarded_allow_ips` (see gunicorn.conf.py)."""
    return request.client.host if request.client else "unknown"


def _current_user(request: Request) -> str:
    if not cfg.auth_enabled:
        return "anonymous"
    s = read_session(cfg.secret_key, request.cookies.get(SESSION_COOKIE))
    return (s or {}).get("u", "-")


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    # 1. Host allow-list (defends against Host-header poisoning)
    if cfg.allowed_hosts:
        host = (request.headers.get("host") or "").split(":")[0].lower()
        if host not in cfg.allowed_hosts:
            return JSONResponse({"detail": "Host not allowed"}, status_code=400)

    # 2. Body-size cap (uploads are large but not unbounded)
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > cfg.max_upload_mb * 1024 * 1024:
        return JSONResponse(
            {"detail": f"Upload exceeds {cfg.max_upload_mb} MB"}, status_code=413)

    path = _app_path(request)
    if cfg.auth_enabled and path not in _PUBLIC_PATHS:
        session = read_session(cfg.secret_key,
                               request.cookies.get(SESSION_COOKIE))
        if session is None:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Authentication required"},
                                    status_code=401)
            return HTMLResponse(_login_page(), status_code=401)
        # 3. CSRF: state-changing requests must echo the cookie in a header
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            if not csrf_ok(request.cookies.get(CSRF_COOKIE),
                           request.headers.get(CSRF_HEADER)):
                return JSONResponse({"detail": "CSRF token missing or invalid"},
                                    status_code=403)

    t0 = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("unhandled error %s %s client=%s user=%s",
                      request.method, path, _client_key(request),
                      _current_user(request))
        raise
    dt_ms = (time.perf_counter() - t0) * 1000.0
    if not path.startswith(("/style.css", "/app.js", "/login.js", "/favicon")):
        lvl = logging.WARNING if response.status_code >= 400 else logging.INFO
        log.log(lvl, "%s %s -> %s in %.0f ms client=%s user=%s",
                request.method, path, response.status_code, dt_ms,
                _client_key(request), _current_user(request))

    # 4. Response hardening headers
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = \
        "geolocation=(), microphone=(), camera=()"
    # Reports embed their charts as data: URIs; nothing is loaded cross-origin.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'")
    if cfg.https_only:
        response.headers["Strict-Transport-Security"] = \
            "max-age=31536000; includeSubDomains"
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    """Return a clean 422 instead of echoing the rejected input back.

    FastAPI's default handler puts the offending value in the response. Two
    problems with that: a body containing NaN or Infinity (legal to Python's
    JSON parser, not to its encoder) makes serialising the error itself fail,
    turning a 422 into a 500; and reflecting arbitrary client input into a
    response is a habit worth not having. Only the field location and the
    message go back."""
    detail = [{"loc": [str(p) for p in e.get("loc", [])],
               "msg": str(e.get("msg", "invalid value")),
               "type": str(e.get("type", ""))}
              for e in exc.errors()]
    log.info("422 %s %s: %s", request.method, _app_path(request), detail)
    return JSONResponse(status_code=422, content={"detail": detail})


def _set_auth_cookies(response, username: str, csrf: str) -> None:
    token = issue_session(cfg.secret_key, username, cfg.session_hours)
    common = dict(secure=cfg.https_only, samesite="strict",
                  max_age=cfg.session_hours * 3600, path=cfg.cookie_path)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, **common)
    # readable by JS on purpose: the frontend echoes it in the CSRF header
    response.set_cookie(CSRF_COOKIE, csrf, httponly=False, **common)


def _base_href() -> str:
    """URL prefix the browser must use. Everything the frontend requests is
    relative to this, so the app works at / and at /x-ray/ unchanged."""
    return (cfg.root_path + "/") if cfg.root_path else "/"


def _login_page() -> str:
    base = html_escape(_base_href())
    return f"""<!doctype html><html><head><meta charset="utf-8">
<base href="{base}">
<title>MSF Phantom QA — sign in</title><link rel="stylesheet" href="style.css">
</head><body class="login-body">
<form id="login-form" class="login-card">
  <h1>MSF Phantom QA</h1>
  <label>User <input name="username" autocomplete="username" required></label>
  <label>Password <input name="password" type="password"
         autocomplete="current-password" required></label>
  <button class="primary" type="submit">Sign in</button>
  <p id="login-error" class="login-error"></p>
</form>
<script src="login.js"></script></body></html>"""


class LoginBody(BaseModel):
    username: str
    password: str


@app.get("/api/auth")
def auth_state(request: Request):
    if not cfg.auth_enabled:
        return {"auth_enabled": False, "authenticated": True,
                "csrf": request.cookies.get(CSRF_COOKIE)}
    s = read_session(cfg.secret_key, request.cookies.get(SESSION_COOKIE))
    return {"auth_enabled": True, "authenticated": s is not None,
            "user": (s or {}).get("u"),
            "csrf": request.cookies.get(CSRF_COOKIE)}


@app.post("/api/login")
def login(body: LoginBody, request: Request):
    csrf = new_csrf_token()
    if not cfg.auth_enabled:
        resp = JSONResponse({"ok": True, "auth_enabled": False, "csrf": csrf})
        _set_auth_cookies(resp, "anonymous", csrf)
        return resp
    key = _client_key(request)
    wait = throttle.locked_for(key)
    if wait > 0:
        raise HTTPException(429, f"Too many failed attempts. "
                                 f"Try again in {wait // 60 + 1} min.")
    ok_user = secrets_equal(body.username, cfg.username)
    if cfg.password_hash:
        ok_pass = verify_password(body.password, cfg.password_hash)
    else:
        ok_pass = secrets_equal(body.password, cfg.password_plain)
    # both checks always run, then combine — no early return that would leak
    # which of the two was wrong via response timing
    if not (ok_user and ok_pass):
        throttle.record_failure(key)
        audit("login", user=body.username, client=key, outcome="denied")
        log.warning("failed sign-in for %r from %s", body.username, key)
        raise HTTPException(401, "Invalid credentials")
    throttle.reset(key)
    audit("login", user=body.username, client=key, outcome="ok")
    resp = JSONResponse({"ok": True, "csrf": csrf})
    _set_auth_cookies(resp, body.username, csrf)
    return resp


@app.post("/api/logout")
def logout(request: Request):
    audit("logout", user=_current_user(request), client=_client_key(request))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path=cfg.cookie_path)
    resp.delete_cookie(CSRF_COOKIE, path=cfg.cookie_path)
    return resp


def secrets_equal(a: str, b: str) -> bool:
    import hmac
    return hmac.compare_digest((a or "").encode(), (b or "").encode())


def _scan(aid: str) -> ingest.ScanData:
    if aid in _scans:
        # The cache is per gunicorn worker. A delete served by ANOTHER worker
        # cannot reach this dict, so an existence check is what keeps a deleted
        # scan's pixels from being served forever. One indexed SELECT — cheap
        # next to decoding an image.
        if store.exists(aid):
            return _scans[aid]
        _forget(aid)
    rec = store.get(aid)
    if rec is None:
        raise HTTPException(404, "analysis not found")
    path = store.upload_path(aid)
    if not os.path.exists(path):
        log.error("stored source file missing for analysis=%s", aid)
        raise HTTPException(
            410, "The stored source file for this analysis is missing, so the "
                 "image can no longer be loaded. Run 'verify' for details.")
    with open(path, "rb") as f:
        data = f.read()
    try:
        scans = ingest.load_any_bytes(data, rec["source_name"])
    except Exception as e:
        # A stored file that no longer decodes is a data problem, not a bug —
        # answer with a clear status instead of an unhandled 500.
        log.error("stored file for analysis=%s could not be decoded: %s", aid, e)
        raise HTTPException(
            422, "The stored source file could not be decoded as an image. "
                 "It may be corrupted — run 'verify' to check its SHA-256.")
    match = next((s for s in scans if s.sha256 == rec["sha256"]), scans[0])
    _scans[aid] = match
    return match


def _reg(aid: str, rec: dict | None = None) -> Registration:
    rec = rec if rec is not None else store.get(aid)
    if rec is None or not rec.get("reg"):
        raise HTTPException(400, "not registered yet")
    r = rec["reg"]
    # Fingerprint the stored transform, so a re-registration invalidates the
    # cache in every worker instead of only the one that served it.
    key = (aid, json_dumps_stable(r.get("transform")))
    if key in _regs:
        return _regs[key]
    try:
        reg = Registration(
            transform=Transform.from_dict(r["transform"]),
            corners_px=np.asarray(r["corners_px"], float),
            coarse_angle_deg=r.get("coarse_angle_deg", float("nan")),
            score=r.get("score", {}),
            candidate_scores=r.get("candidate_scores", []),
            landmarks=r.get("landmarks", {}),
            residual_rms_mm=r.get("residual_rms_mm", float("nan")),
        )
    except (KeyError, TypeError, ValueError) as e:
        # A corrupted stored registration must not 500 every endpoint that
        # builds a context from it — say what is wrong and how to recover.
        log.error("stored registration for analysis=%s is corrupted: %s",
                  aid, e)
        raise HTTPException(
            409, "The stored registration for this analysis is corrupted. "
                 "Re-register it in Stage A (manual corners work too).")
    if len(_regs) > 64:
        _regs.clear()
    _regs[key] = reg
    return reg


def json_dumps_stable(obj) -> str:
    import json as _json
    return _json.dumps(obj, sort_keys=True)


def _ctx(aid: str, rec: dict | None = None):
    scan = _scan(aid)
    rec = rec if rec is not None else store.get(aid)
    return pipeline.build_ctx(scan, pdef, _reg(aid, rec),
                              {"scan_meta": scan.meta,
                               "sid_mm": rec.get("sid_mm") or 1000.0})


def _require_unsigned(rec: dict, what: str):
    """Refuse to change measurements an administrator has signed off.

    Changing the geometry drops the stored results, because results computed
    from geometry that no longer exists are worse than none. On a validated
    analysis that combination is worse still: the ruling, the approver's name
    and the date all survive while the numbers they refer to are gone, and the
    record then vanishes from every trend and export, which filter on completed
    results. The reanalyze CLI already skips signed-off analyses; the web path
    now does the same. Withdrawing the ruling is one click, and it is recorded."""
    if (rec.get("validation_status") or "").strip():
        raise HTTPException(
            409,
            f"This analysis has been signed off by "
            f"{rec.get('validated_by') or 'an administrator'}"
            + (f" on {rec['validated_at'][:16]}" if rec.get("validated_at") else "")
            + f", so {what} would change measurements somebody has taken "
              f"responsibility for. Withdraw the validation first if the "
              f"analysis really needs to be reworked.")


def _roi_stats_or_none(ctx, node):
    """Statistics for an ROI, or None for shapes that have none.

    Segments (the line-pair profile lines, the wedge axis) carry no area, and
    stats_for_roi raises on them. Without this guard, rotating a profile line
    committed the edit and THEN returned a 500, leaving the client convinced
    the change had failed while the database said otherwise."""
    from ..analysis.common import stats_for_roi
    if (node or {}).get("type") not in ("rect", "circle", "annulus"):
        return None
    return stats_for_roi(ctx, node)


def _display_range(aid: str) -> dict:
    """Value range the viewer should map to black..white.

    Detectors differ in bit depth — 12-bit on one unit, 14-bit on another — so
    a fixed 0..4095 slider range whites out a 14-bit image entirely. The
    window/level controls work relative to this measured range instead."""
    img = _scan(aid).pixels
    lo, hi = (float(x) for x in np.percentile(img, [0.5, 99.5]))
    if hi - lo < 1e-6:
        lo, hi = float(img.min()), float(img.max()) or 1.0
    return {"lo": lo, "hi": hi,
            "min": float(img.min()), "max": float(img.max())}


def _reg_payload(aid: str, reg: Registration) -> dict:
    scan = _scan(aid)
    return pipeline.to_jsonable({
        "summary": reg.summary(),
        "transform": reg.transform.to_dict(),
        "display_range": _display_range(aid),
        "landmarks": reg.landmarks,
        "candidates": reg.candidate_scores[:4],
        "image": {"rows": scan.shape[0], "cols": scan.shape[1]},
        "reduced_precision": scan.reduced_precision,
        "meta": scan.meta,
    })


def _do_register(aid: str, corners_hint=None):
    scan = _scan(aid)
    reg = pipeline.run_stage_a(scan, pdef, corners_hint=corners_hint)
    store.update(aid, reg=pipeline.to_jsonable({
        "transform": reg.transform.to_dict(),
        "corners_px": reg.corners_px,
        "coarse_angle_deg": reg.coarse_angle_deg,
        "score": reg.score,
        "candidate_scores": reg.candidate_scores,
        "landmarks": reg.landmarks,
        "residual_rms_mm": reg.residual_rms_mm,
    }))
    return reg


# ------------------------------------------------------------------ endpoints

@app.post("/api/analyses")
async def upload(request: Request, file: UploadFile = File(...),
                 site: str = Form(""), phantom: str = Form(""),
                 operator: str = Form(""), notes: str = Form(""),
                 allow_duplicate: bool = Form(False)):
    user, client = _current_user(request), _client_key(request)
    data = await file.read()
    # The middleware caps the DECLARED size, but a chunked upload carries no
    # Content-Length and a hostile client can lie in the header. Measuring the
    # bytes actually received closes both holes; starlette has already spooled
    # them to disk by now, so this costs nothing extra in memory.
    if len(data) > cfg.max_upload_mb * 1024 * 1024:
        audit("upload", user=user, client=client, outcome="rejected",
              filename=file.filename, error="body larger than declared cap")
        raise HTTPException(413, f"Upload exceeds {cfg.max_upload_mb} MB")
    try:
        scans = ingest.load_any_bytes(data, file.filename or "upload")
    except Exception as e:
        log.warning("upload rejected (%s) name=%r user=%s",
                    e, file.filename, user)
        audit("upload", user=user, client=client, outcome="rejected",
              filename=file.filename, error=str(e))
        raise HTTPException(400, f"Could not read file: {e}")
    labels = {"site": site, "phantom": phantom,
              "operator": operator, "notes": notes}

    # The same file analysed twice produces two records that look identical in
    # History and double-count in any trend. The SHA-256 is already computed,
    # so say so instead of silently creating the duplicate.
    if not allow_duplicate:
        dupes = []
        for scan in scans:
            dupes.extend(store.find_by_sha256(scan.sha256))
        if dupes:
            audit("upload", user=user, client=client, outcome="duplicate",
                  filename=file.filename, existing=[d["id"] for d in dupes])
            log.info("upload rejected as duplicate of %s",
                     [d["id"] for d in dupes])
            return JSONResponse(status_code=409, content=pipeline.to_jsonable({
                "detail": "This file has already been analysed.",
                "duplicate_of": dupes,
            }))

    # The stored layout is looked up once and only advertised here. Applying it
    # means proposing every pattern first, which is the expensive step and is
    # thrown away the moment the operator corrects the registration by hand —
    # so that happens in /propose, at Stage A confirm, not per uploaded image.
    stored_layout = _profile_summary(
        store.get_phantom_profile(labels["phantom"]))

    created = []
    for scan in scans:
        # Store the bytes the recorded hash actually describes. For a zip
        # (CD export) that is the extracted member, NOT the container:
        # storing the container made every integrity check fail, because the
        # recorded sha256 is the member's.
        aid = store.new_analysis(scan, scan.source_bytes or data,
                                 ingest.protocol_signature(scan.meta),
                                 ALGO_VERSION, pdef.version, labels=labels)
        _scans[aid] = scan
        store.audit(aid, "A", "uploaded",
                    {"source": scan.source_name, "kind": scan.kind,
                     **{k: v for k, v in labels.items() if v}})
        audit("upload", user=user, client=client, analysis=aid,
              filename=scan.source_name, kind=scan.kind,
              sha256=scan.sha256, bytes=len(data),
              **{k: v for k, v in labels.items() if v})
        log.info("uploaded analysis=%s source=%r site=%r phantom=%r sha=%s",
                 aid, scan.source_name, labels["site"], labels["phantom"],
                 scan.sha256[:16])
        try:
            reg = _do_register(aid)
            created.append({"id": aid, "source_name": scan.source_name,
                            "registered": True,
                            "phantom_profile": stored_layout,
                            "registration": _reg_payload(aid, reg)})
        except Exception as e:
            created.append({"id": aid, "source_name": scan.source_name,
                            "registered": False, "error": str(e),
                            "phantom_profile": stored_layout})
    return {"analyses": created, "phantom_profile": stored_layout}


_VALIDATION_FILTERS = ("", "pending") + VALIDATION_STATES


def _check_filters(validation: str, order: str = "acquired"):
    """Reject filter values nothing recognises.

    Both parameters used to fall back silently — order=newest listed by
    acquisition date, validation=Validated exported every analysis — which on
    a scripted export reads as a correct answer to the wrong question."""
    if validation not in _VALIDATION_FILTERS:
        raise HTTPException(
            400, f"unknown validation filter {validation!r} — one of "
                 f"{', '.join(v or 'pending' for v in _VALIDATION_FILTERS)}")
    if order not in ("acquired", "uploaded"):
        raise HTTPException(400, "order must be 'acquired' or 'uploaded'")


@app.get("/api/analyses")
def list_analyses(site: str = "", phantom: str = "", signature: str = "",
                  validation: str = "", completed_only: bool = False,
                  order: str = "acquired"):
    _check_filters(validation, order)
    return {"analyses": store.list_all(site=site or None,
                                       phantom=phantom or None,
                                       signature=signature or None,
                                       validation=validation or None,
                                       completed_only=completed_only,
                                       order_by=order),
            "order": "uploaded" if order == "uploaded" else "acquired"}


@app.get("/api/labels")
def labels():
    return store.labels()


@app.get("/api/phantom_profiles")
def phantom_profiles():
    """Every stored measuring-point layout, with how many analyses still use it.

    A layout is dropped automatically when the last analysis carrying its
    phantom label goes, so a row with n_analyses = 0 should not normally
    appear; when it does, something deleted analyses outside the store."""
    return {"profiles": store.list_phantom_profiles()}


class ProfileDeleteBody(BaseModel):
    phantom: str = ""
    admin_password: str = ""
    reason: str = ""


@app.post("/api/phantom_profiles/forget")
def forget_phantom_profile(body: ProfileDeleteBody, request: Request):
    """Discard a phantom's stored layout without touching its analyses.

    Admin-gated for the same reason deletion is: the layout is shared by every
    future scan of that phantom, so dropping it is a decision about other
    people's work, not just the caller's."""
    user, client = _current_user(request), _client_key(request)
    label = store.profile_key(body.phantom)
    if not label:
        raise HTTPException(400, "name the phantom whose layout to forget")
    if not cfg.deletion_enabled:
        raise HTTPException(
            403, "Removing a stored layout requires an administrator password. "
                 "Set PHANTOMQA_ADMIN_PASSWORD_HASH in .env "
                 "(python -m phantom_qa.manage set-admin-password).")
    wait = admin_throttle.locked_for(client)
    if wait > 0:
        raise HTTPException(429, f"Too many failed admin attempts. Try again in "
                                 f"{wait // 60 + 1} min.")
    if not verify_password(body.admin_password, cfg.admin_password_hash):
        admin_throttle.record_failure(client)
        audit("phantom_profile", user=user, client=client, phantom=label,
              outcome="denied", refusal="bad admin password")
        raise HTTPException(401, "Incorrect administrator password.")
    admin_throttle.reset(client)
    gone = store.delete_phantom_profile(label)
    audit("phantom_profile", user=user, client=client, phantom=label,
          outcome="forgotten" if gone else "absent", reason=body.reason)
    log.warning("stored layout for phantom=%r forgotten by user=%s (%s)",
                label, user, "removed" if gone else "there was none")
    return {"ok": True, "deleted": gone, "phantom": label}


class LabelBody(BaseModel):
    site: str | None = None
    phantom: str | None = None
    operator: str | None = None
    notes: str | None = None


@app.post("/api/analyses/{aid}/labels")
def set_labels(aid: str, body: LabelBody, request: Request):
    rec = store.get(aid)
    if rec is None:
        raise HTTPException(404, "not found")
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    before = {k: rec.get(k) for k in fields}
    try:
        out = store.set_labels(aid, fields)
    except KeyError:
        raise HTTPException(404, "not found")   # deleted while we were editing
    store.audit(aid, "F", "labels edited",
                {**fields, "layout_deleted": out.get("profile_deleted", False),
                 "baseline_demoted": out.get("baseline_demoted", False)})
    audit("labels", user=_current_user(request), client=_client_key(request),
          analysis=aid, before=before, after=fields,
          layout_deleted=out.get("profile_deleted", False))
    return {"ok": True, **fields,
            # A stored layout belongs to the phantom label. Renaming the last
            # analysis off a label leaves nothing for that layout to describe,
            # so it goes — and the operator is told, because it is not obvious.
            "layout_deleted": out.get("profile_deleted", False),
            "baseline_demoted": out.get("baseline_demoted", False),
            "phantom_before": out.get("phantom_before", "")}


@app.get("/api/analyses/{aid}")
def get_analysis(aid: str):
    rec = store.get(aid)
    if rec is None:
        raise HTTPException(404, "not found")
    payload = {k: rec[k] for k in ("id", "created_at", "source_name", "sha256",
                                   "kind", "reduced_precision", "signature",
                                   "stage", "status", "sid_mm", "is_baseline",
                                   "algo_version", "pdef_version",
                                   "site", "phantom", "operator", "notes",
                                   "acquired_at", "validation_status",
                                   "validated_by", "validation_comment",
                                   "validated_at")}
    payload["meta"] = rec.get("meta")
    payload["geometry"] = rec.get("geometry")
    payload["results"] = rec.get("results")
    payload["audit"] = rec.get("audit")
    payload["acquired_flag"] = acquisition_flag(rec)
    payload["layout_source"] = rec.get("layout_source") or ""
    payload["history"] = store.geometry_state(aid)
    prof = store.get_phantom_profile(rec.get("phantom") or "")
    payload["phantom_profile"] = _profile_summary(prof)
    # What a delete would take with it. Shown in the confirmation panel, so an
    # operator can see that removing this row also forgets the phantom's
    # measuring-point layout before they agree to it.
    n_same = store.count_for_phantom(rec.get("phantom") or "")
    payload["delete_impact"] = {
        "phantom": store.profile_key(rec.get("phantom") or ""),
        "analyses_for_phantom": n_same,
        "is_last_for_phantom": bool(n_same == 1),
        "layout_would_be_deleted": bool(prof and n_same <= 1),
    }
    try:
        payload["registration"] = _reg_payload(aid, _reg(aid, rec))
    except HTTPException:
        payload["registration"] = None
    except Exception as e:                     # corrupted reg or missing file
        log.error("registration payload failed for analysis=%s: %s", aid, e)
        payload["registration"] = None
    return pipeline.to_jsonable(payload)


@app.get("/api/analyses/{aid}/image.png")
def image_png(aid: str, wc: float | None = None, ww: float | None = None,
              scale: int = 1600):
    # Query values are viewer state, not trusted input: a zero or negative
    # scale crashes PIL's thumbnail, a huge one asks for a gigapixel resample,
    # and NaN passes FastAPI's float parsing and poisons the window arithmetic.
    scale = min(max(scale, 64), 4096)
    if wc is not None and not math.isfinite(wc):
        wc = None
    if ww is not None and not math.isfinite(ww):
        ww = None
    key = (aid, wc, ww, scale)
    if key not in _img_cache:
        from PIL import Image
        img = _scan(aid).pixels
        if wc is None or ww is None:
            lo, hi = np.percentile(img, [1, 99])
        else:
            lo, hi = wc - ww / 2, wc + ww / 2
        a = np.clip((img - lo) / max(hi - lo, 1e-9), 0, 1)
        pil = Image.fromarray((a * 255).astype(np.uint8))
        if max(pil.size) > scale:
            pil.thumbnail((scale, scale), Image.LANCZOS)
        buf = io.BytesIO()
        pil.save(buf, format="png")
        # FIFO eviction: clearing the whole cache meant one operator paging
        # through History threw away every other operator's rendered view.
        while len(_img_cache) > 24:
            _img_cache.pop(next(iter(_img_cache)), None)
        _img_cache[key] = buf.getvalue()
    return Response(_img_cache[key], media_type="image/png")


class CornersBody(BaseModel):
    corners_px: list[list[float]] | None = None


@app.post("/api/analyses/{aid}/register")
def re_register(aid: str, body: CornersBody):
    rec = store.get(aid)
    if rec is None:
        raise HTTPException(404, "not found")
    _require_unsigned(rec, "re-registering the phantom")
    hint = np.asarray(body.corners_px, float) if body.corners_px else None
    try:
        reg = _do_register(aid, corners_hint=hint)
    except Exception as e:
        raise HTTPException(400, f"registration failed: {e}")
    store.audit(aid, "A", "re-registered",
                {"manual_corners": body.corners_px is not None})
    # Geometry proposals are expressed in pixels derived from the transform, so
    # a new transform invalidates them — and every undo state built on top.
    store.update(aid, geometry=None, results=None, stage="A", geometry_seq=0)
    store.clear_geometry_history(aid)
    return _reg_payload(aid, reg)


def _profile_summary(prof: dict | None) -> dict | None:
    if not prof:
        return None
    return {"phantom": prof["phantom_key"], "updated_at": prof["updated_at"],
            "updated_by": prof.get("updated_by") or "",
            "pdef_version": prof.get("pdef_version") or "",
            "source_analysis_id": prof.get("source_analysis_id") or "",
            "n_rois": len((prof.get("layout") or {}).get("rois") or {})}


class ProposeBody(BaseModel):
    #: None means "do what this analysis did last time"; the Stage C reset
    #: buttons send an explicit true / false.
    use_profile: bool | None = None


@app.post("/api/analyses/{aid}/propose")
def propose(aid: str, body: ProposeBody | None = None, request: Request = None):
    """Re-detect every measuring point, then optionally replay the phantom's
    stored layout on top.

    The automatic proposal always runs first, even when a layout is going to be
    applied: its per-pattern detection flags are the evidence Stage B shows, and
    they are only meaningful if they came from this scan. The raw proposal is
    also pinned as undo state 0, which is what "reset to auto-detected" returns
    to — exactly, rather than by re-detecting and hoping for the same answer."""
    body = body or ProposeBody()
    rec = store.get(aid)
    if rec is None:
        raise HTTPException(404, "not found")
    _require_unsigned(rec, "re-detecting the patterns")
    ctx = _ctx(aid, rec)
    geom = pipeline.propose_all(ctx)
    user = _current_user(request) if request is not None else ""

    # seq 0 is the untouched automatic proposal, always.
    try:
        store.set_geometry_baseline(aid, geom, action="propose", user=user)
    except KeyError:
        raise HTTPException(404, "not found")   # deleted while proposing
    store.audit(aid, "B", "proposals generated")

    want = (body.use_profile if body.use_profile is not None
            else (rec.get("layout_source") or "") != "auto")
    prof = store.get_phantom_profile(rec.get("phantom") or "") if want else None
    applied, check, report = False, None, None
    if prof:
        check = layout_profile.layout_agrees(prof["layout"], geom)
        if check["ok"]:
            geom, report = layout_profile.apply_layout(ctx, geom, prof["layout"])
            store.replace_geometry(aid, geom, action="apply_profile", user=user,
                                   detail={"phantom": prof["phantom_key"]})
            applied = True
            store.audit(aid, "B", "stored phantom layout applied",
                        {"phantom": prof["phantom_key"], **report})
        else:
            log.warning("stored layout for phantom=%r refused on analysis=%s: %s",
                        prof["phantom_key"], aid, check["reason"])
            store.audit(aid, "B", "stored phantom layout refused",
                        {"phantom": prof["phantom_key"], **check})

    store.update(aid, stage="B", layout_source=("profile" if applied else "auto"))
    return pipeline.to_jsonable({
        "geometry": geom,
        "layout_source": "profile" if applied else "auto",
        "profile": _profile_summary(prof),
        "profile_applied": applied,
        "profile_report": report,
        "profile_check": check,
        "history": store.geometry_state(aid),
    })


def _finite_point(v):
    """A pixel coordinate pair that numpy can actually use.

    `list[float]` alone accepts [], [1.0] and [1, 2, 3]: the first two reach
    numpy and raise, giving a 500 for what is plainly a bad request, and a
    one-element list silently indexes as a coordinate of (1, 1)."""
    if v is None:
        return v
    if len(v) != 2:
        raise ValueError("expected exactly two coordinates, x and y")
    if not all(math.isfinite(c) for c in v):
        raise ValueError("coordinates must be finite")
    return v


class RoiMove(BaseModel):
    roi_id: str
    center_px: conlist(float, min_length=2, max_length=2)

    @field_validator("center_px")
    @classmethod
    def _finite(cls, v):
        return _finite_point(v)


# The lookup semantics are shared with the layout-profile writer/reader, so
# the two cannot drift apart about what counts as an ROI.
_ROI_TYPES = layout_profile.ROI_TYPES
_walk_find = layout_profile.walk_find
_walk_children = layout_profile.walk_children


def _seg_angle(seg: dict):
    from ..analysis.common import roi_angle_deg
    return roi_angle_deg(seg)


@app.post("/api/analyses/{aid}/roi")
def move_roi(aid: str, body: RoiMove, request: Request):
    """Move one ROI and everything attached to it.

    The whole edit happens inside one write transaction. The geometry is a
    single JSON blob, so a plain read-modify-write would let two overlapping
    edits discard one another — the user moves an ROI, it springs back, and
    moving a different one appears to 'fix' it."""
    rec = store.get(aid)
    if rec is None:
        raise HTTPException(404, "analysis not found")
    _require_unsigned(rec, "moving a measuring area")
    if body.roi_id == "lowcontrast/block":
        raise HTTPException(
            400, "The low-contrast block is a rigid group — use the "
                 "lowcontrast_block endpoint so the eight circles move with it.")
    ctx = _ctx(aid, rec)
    from ..analysis.common import roi_center_mm, roi_translate_mm

    def edit(geom):
        if not geom:
            raise HTTPException(400, "no geometry yet")
        node = _walk_find(geom, body.roi_id)
        if node is None:
            raise HTTPException(404, f"ROI {body.roi_id} not found")

        old_center = roi_center_mm(node)
        updated = roi_center_from_px(ctx, node, body.center_px)
        node.clear()
        node.update(pipeline.to_jsonable(updated))
        node["manually_adjusted"] = True
        node["auto_center_mm"] = node.get("auto_center_mm", old_center)
        new_center = roi_center_mm(node)
        dx = new_center[0] - old_center[0]
        dy = new_center[1] - old_center[1]

        # Companions are found by id prefix, so a new one cannot be forgotten.
        changed = [node]
        for comp in _walk_children(geom, body.roi_id):
            if comp.get("type") == "segment" and body.roi_id.startswith("linepairs/"):
                fresh = linepairs.profile_for_center(
                    ctx, new_center,
                    (geom.get("linepairs") or {}).get("roi_size_mm", 12.6),
                    comp.get("id", ""), fallback_dir_deg=_seg_angle(comp))
            else:
                fresh = roi_translate_mm(ctx, comp, dx, dy)
            comp.clear()
            comp.update(pipeline.to_jsonable(fresh))
            changed.append(comp)
        return old_center, new_center, node, changed

    try:
        (old_center, new_center, node, changed), hist = store.mutate_geometry(
            aid, edit, action="roi", user=_current_user(request),
            detail={"roi": body.roi_id})
    except KeyError:
        raise HTTPException(404, "analysis not found")

    store.audit(aid, "C", "roi moved",
                {"roi": body.roi_id, "from_mm": old_center, "to_mm": new_center})
    return pipeline.to_jsonable({"roi": node,
                                 "stats": _roi_stats_or_none(ctx, node),
                                 "changed": changed, "history": hist})


class RoiRotate(BaseModel):
    roi_id: str
    angle_deg: float

    @field_validator("angle_deg")
    @classmethod
    def _finite(cls, v):
        # NaN survives JSON, reaches the rotation matrix and poisons every
        # coordinate it touches — the ROI simply disappears from the overlay.
        if not math.isfinite(v):
            raise ValueError("the angle must be a finite number of degrees")
        return v


@app.post("/api/analyses/{aid}/roi_rotate")
def rotate_roi(aid: str, body: RoiRotate, request: Request):
    """Set an ROI's phantom-frame angle.

    Needed when automatic placement gets the orientation wrong on a phantom
    that differs from the definition."""
    rec = store.get(aid)
    if rec is None:
        raise HTTPException(404, "analysis not found")
    _require_unsigned(rec, "rotating a measuring area")
    if body.roi_id == "lowcontrast/block":
        raise HTTPException(
            400, "The low-contrast block is a rigid group — use the "
                 "lowcontrast_block endpoint so the eight circles turn with it.")
    ctx = _ctx(aid, rec)
    from ..analysis.common import roi_angle_deg, roi_rotate

    def edit(geom):
        if not geom:
            raise HTTPException(400, "no geometry yet")
        node = _walk_find(geom, body.roi_id)
        if node is None:
            raise HTTPException(404, f"ROI {body.roi_id} not found")
        old_angle = roi_angle_deg(node)
        if old_angle is None:
            raise HTTPException(400, "this ROI has no orientation to set")

        rotated = roi_rotate(ctx, node, body.angle_deg)
        node.clear()
        node.update(pipeline.to_jsonable(rotated))
        node["manually_adjusted"] = True
        if node.get("auto_angle_deg") is None:
            node["auto_angle_deg"] = old_angle

        delta = float(body.angle_deg) - float(old_angle)
        changed = [node]
        # The companion turns BY the same amount, not TO the same angle. The
        # line-pair square deliberately sits 45 deg off its profile line, so
        # setting both to one absolute angle would swing the measuring line off
        # the pattern — and mark it hand-set, which suppresses the automatic
        # re-measurement for good.
        for comp in _walk_children(geom, body.roi_id):
            if comp.get("type") == "segment":
                comp_angle = roi_angle_deg(comp)
                if comp_angle is None:
                    continue
                fresh = roi_rotate(ctx, comp, comp_angle + delta)
                comp.clear()
                comp.update(pipeline.to_jsonable(fresh))
                comp["manually_adjusted"] = True
                changed.append(comp)
        return old_angle, node, changed

    try:
        (old_angle, node, changed), hist = store.mutate_geometry(
            aid, edit, action="roi_rotate", user=_current_user(request),
            detail={"roi": body.roi_id, "to_deg": body.angle_deg})
    except KeyError:
        raise HTTPException(404, "analysis not found")

    store.audit(aid, "C", "roi rotated",
                {"roi": body.roi_id, "from_deg": old_angle,
                 "to_deg": body.angle_deg})
    return pipeline.to_jsonable({"roi": node,
                                 "stats": _roi_stats_or_none(ctx, node),
                                 "changed": changed, "history": hist})


class BlockPlace(BaseModel):
    """Reposition the low-contrast block as a whole.

    Either give centre+angle (drag / rotate) or four clicked corners. The
    shapes are pinned here so a malformed body is a 400 from the model rather
    than a 500 from numpy further down."""
    center_px: conlist(float, min_length=2, max_length=2) | None = None
    angle_deg: float | None = None
    corners_px: conlist(
        conlist(float, min_length=2, max_length=2),
        min_length=4, max_length=4) | None = None

    @field_validator("center_px", "corners_px")
    @classmethod
    def _finite(cls, v):
        if v is None:
            return v
        flat = v if isinstance(v[0], float) else [c for p in v for c in p]
        if not all(math.isfinite(c) for c in flat):
            raise ValueError("coordinates must be finite")
        return v

    @field_validator("angle_deg")
    @classmethod
    def _finite_angle(cls, v):
        if v is not None and not math.isfinite(v):
            raise ValueError("angle must be finite")
        return v


@app.post("/api/analyses/{aid}/lowcontrast_block")
def place_lowcontrast_block(aid: str, body: BlockPlace, request: Request):
    """Move or rotate the whole low-contrast block; the eight circles follow.

    The circles sit on a rigid grid inside the block, so correcting the block
    once is far better than dragging eight circles individually."""
    rec = store.get(aid)
    if rec is None:
        raise HTTPException(404, "analysis not found")
    _require_unsigned(rec, "moving the low-contrast block")
    ctx = _ctx(aid, rec)
    from ..analysis import lowcontrast
    from ..analysis.common import rect_roi

    def edit(geom):
        if not geom or not geom.get("lowcontrast"):
            raise HTTPException(400, "no low-contrast geometry yet")
        lcg = geom["lowcontrast"]
        block = lcg.get("block") or {}

        if body.corners_px:
            if len(body.corners_px) != 4:
                raise HTTPException(400, "exactly four corners are required")
            centre, angle = lowcontrast.block_from_corners(ctx, body.corners_px)
        else:
            centre = (list(ctx.T.px_to_mm(body.center_px))
                      if body.center_px else list(block.get("center_mm", [0, 0])))
            angle = (float(body.angle_deg) if body.angle_deg is not None
                     else float(block.get("angle_deg", 0.0)))

        size = block.get("size_mm") or ctx.pdef.lowcontrast["size_mm"]
        lcg["block"] = pipeline.to_jsonable(
            rect_roi(ctx, centre, size, angle, roi_id="lowcontrast/block"))
        lcg["block"]["manually_adjusted"] = True
        lcg["angle_deg"] = angle
        # the grid shift was a refinement of the OLD placement; drop it
        lcg["grid_shift_mm"] = [0.0, 0.0]
        lcg["circles"] = pipeline.to_jsonable(
            lowcontrast.circles_for_block(ctx, centre, angle))
        for c in lcg["circles"]:
            for k in ("roi", "bg_roi", "full_circle"):
                c[k]["manually_adjusted"] = True
        return centre, angle, lcg

    try:
        (centre, angle, lcg), hist = store.mutate_geometry(
            aid, edit, action="lowcontrast_block",
            user=_current_user(request), detail={"angle_deg": body.angle_deg})
    except KeyError:
        raise HTTPException(404, "analysis not found")

    store.audit(aid, "C", "low-contrast block placed",
                {"center_mm": centre, "angle_deg": angle,
                 "by": "corners" if body.corners_px else "drag"})
    return pipeline.to_jsonable({"lowcontrast": lcg,
                                 "center_mm": centre, "angle_deg": angle,
                                 "history": hist})


@app.get("/api/analyses/{aid}/roi_stats")
def roi_stats(aid: str, roi_id: str):
    rec = store.get(aid)
    if not rec or not rec.get("geometry"):
        raise HTTPException(400, "no geometry yet")
    node = _walk_find(rec["geometry"], roi_id)
    if node is None:
        raise HTTPException(404, f"ROI {roi_id} not found")
    ctx = _ctx(aid, rec)
    return pipeline.to_jsonable({"roi": node,
                                 "stats": _roi_stats_or_none(ctx, node)})


class PreviewBody(BaseModel):
    tests: list[str] = ["geometry"]
    sid_mm: float = 1000.0


@app.post("/api/analyses/{aid}/compute_preview")
def compute_preview(aid: str, body: PreviewBody):
    """Run a subset of tests on the current geometry WITHOUT storing results.
    Used by wizard Stage D (dimension verification)."""
    rec = store.get(aid)
    if not rec or not rec.get("geometry"):
        raise HTTPException(400, "no geometry yet")
    store.update(aid, sid_mm=body.sid_mm)
    ctx = _ctx(aid)
    out = {}
    for name in body.tests:
        if name not in pipeline.TESTS:
            continue
        geom = rec["geometry"].get(name)
        if not geom or geom.get("_error"):
            out[name] = {"status": "n/a"}
            continue
        try:
            out[name] = pipeline._MODULES[name].compute(ctx, geom)
        except Exception as e:
            out[name] = {"status": "error", "error": str(e)}
    return pipeline.to_jsonable(out)


class FieldEdgeSet(BaseModel):
    side: str
    point_px: conlist(float, min_length=2, max_length=2)

    @field_validator("point_px")
    @classmethod
    def _finite(cls, v):
        return _finite_point(v)


@app.post("/api/analyses/{aid}/field_edge")
def set_field_edge(aid: str, body: FieldEdgeSet, request: Request):
    """Place a radiation-field edge by hand.

    Serialised like every other Stage C edit: this used to read the whole
    geometry blob, change it in memory and write it back, so an ROI drag
    committed in between was silently thrown away."""
    side_geom = {"top": ((0, 1), (0, -1)), "right": ((1, 0), (-1, 0)),
                 "bottom": ((0, -1), (0, 1)), "left": ((-1, 0), (1, 0))}
    if body.side not in side_geom:
        raise HTTPException(400, "bad side")
    rec = store.get(aid)
    if rec is None:
        raise HTTPException(404, "analysis not found")
    _require_unsigned(rec, "moving a field edge")
    ctx = _ctx(aid, rec)
    (ex, ey), (nx, ny) = side_geom[body.side]
    S2 = pdef.side_mm / 2.0
    edge_pt = np.array([ex * S2, ey * S2], float)
    outward = -np.array([nx, ny], float)
    p_mm = np.asarray(ctx.T.px_to_mm(body.point_px), float)
    offset = float(np.dot(p_mm - edge_pt, outward))
    entry = pipeline.to_jsonable({
        "side": body.side, "detected": True, "manual": True,
        "offset_from_edge_mm": offset,
        # Keep the marker where the operator clicked. Only the measurement
        # projects onto the side normal; projecting the marker discards its
        # position along the edge and makes it jump to the side's centre.
        "edge_pt_px": list(body.point_px),
    })

    def edit(geom):
        if not geom:
            raise HTTPException(400, "no geometry yet")
        gg = geom.get("geometry")
        # propose_all stores {"_error": ...} for a test that raised, so the
        # field_edges dict is not guaranteed to exist.
        if not isinstance(gg, dict) or not isinstance(gg.get("field_edges"), dict):
            raise HTTPException(
                400, "this scan has no field-edge geometry to correct — the "
                     "geometry test did not propose successfully")
        gg["field_edges"][body.side] = entry

    try:
        _, hist = store.mutate_geometry(
            aid, edit, action="field_edge", user=_current_user(request),
            detail={"side": body.side, "offset_mm": offset})
    except KeyError:
        raise HTTPException(404, "analysis not found")
    store.audit(aid, "C", "field edge set manually",
                {"side": body.side, "offset_mm": offset})
    return {**entry, "history": hist}


# ------------------------------------------- measuring-point undo / redo / reset

def _layout_source_of(geometry) -> str:
    """What a geometry state's measuring points are actually based on.

    Undo and redo can step across the point where a stored layout was applied,
    so the column cannot simply be left as it was — it would then say
    "profile" over a state that is pure detection, or the reverse, and the
    Stage C banner and the next propose would both act on the lie."""
    for _, node in layout_profile.walk_rois(geometry or {}):
        if node.get("from_profile"):
            return "profile"
    return "auto"


@app.post("/api/analyses/{aid}/geometry/undo")
def geometry_undo(aid: str, request: Request):
    """Step the measuring points back one edit.

    Server-side rather than in the browser, because the browser resyncs from
    the server whenever a request fails or the analysis is reopened — a stack
    kept in the page would vanish exactly when it was most needed."""
    rec = store.get(aid)
    if rec is None:
        raise HTTPException(404, "not found")
    _require_unsigned(rec, "undoing a measuring-point change")
    try:
        out = store.undo_geometry(aid)
    except LookupError as e:
        raise HTTPException(409, str(e))
    store.audit(aid, "C", "undo", {"to_seq": out["seq"]})
    source = _layout_source_of(out["geometry"])
    store.update(aid, layout_source=source)
    return pipeline.to_jsonable({"geometry": out.pop("geometry"),
                                 "history": out, "layout_source": source})


@app.post("/api/analyses/{aid}/geometry/redo")
def geometry_redo(aid: str, request: Request):
    rec = store.get(aid)
    if rec is None:
        raise HTTPException(404, "not found")
    _require_unsigned(rec, "redoing a measuring-point change")
    try:
        out = store.redo_geometry(aid)
    except LookupError as e:
        raise HTTPException(409, str(e))
    store.audit(aid, "C", "redo", {"to_seq": out["seq"]})
    source = _layout_source_of(out["geometry"])
    store.update(aid, layout_source=source)
    return pipeline.to_jsonable({"geometry": out.pop("geometry"),
                                 "history": out, "layout_source": source})


class GeometryReset(BaseModel):
    #: "auto"    — back to this scan's untouched automatic proposal
    #: "profile" — back to the stored layout of this phantom
    to: str = "auto"


@app.post("/api/analyses/{aid}/geometry/reset")
def geometry_reset(aid: str, body: GeometryReset, request: Request):
    """Discard the manual corrections and start from a known layout again.

    A reset is recorded as an ordinary edit rather than as a rewind, so it is
    itself undoable: pressing it by mistake never destroys an afternoon's work.
    "auto" restores the exact snapshot taken when the patterns were proposed —
    not a re-detection, which could legitimately land somewhere else."""
    rec = store.get(aid)
    if rec is None:
        raise HTTPException(404, "not found")
    _require_unsigned(rec, "resetting the measuring points")
    user = _current_user(request)

    if body.to == "auto":
        base = store.geometry_at(aid, 0)
        if base is None:
            raise HTTPException(
                409, "there is no automatic proposal to go back to — confirm "
                     "the registration in Stage A to generate one")
        try:
            hist = store.replace_geometry(aid, base, action="reset_auto",
                                          user=user)
        except KeyError:
            raise HTTPException(404, "not found")
        store.update(aid, layout_source="auto")
        store.audit(aid, "C", "measuring points reset to auto-detected")
        return pipeline.to_jsonable({"geometry": base, "history": hist,
                                     "layout_source": "auto"})

    if body.to != "profile":
        raise HTTPException(400, "reset target must be 'auto' or 'profile'")

    prof = store.get_phantom_profile(rec.get("phantom") or "")
    if not prof:
        raise HTTPException(
            404, "no measuring-point layout is stored for this phantom yet")
    base = store.geometry_at(aid, 0)
    if base is None:
        raise HTTPException(409, "there is no automatic proposal to build on")
    check = layout_profile.layout_agrees(prof["layout"], base)
    if not check["ok"]:
        raise HTTPException(409, check["reason"])
    ctx = _ctx(aid, rec)
    geom, report = layout_profile.apply_layout(ctx, base, prof["layout"])
    hist = store.replace_geometry(aid, geom, action="reset_profile", user=user,
                                  detail={"phantom": prof["phantom_key"]})
    store.update(aid, layout_source="profile")
    store.audit(aid, "C", "measuring points reset to the stored phantom layout",
                {"phantom": prof["phantom_key"], **report})
    return pipeline.to_jsonable({
        "geometry": geom, "history": hist, "layout_source": "profile",
        "profile": _profile_summary(prof), "profile_report": report})


class StageConfirm(BaseModel):
    stage: str
    note: str | None = None
    #: Stage C only. None means "store the layout if the phantom is named";
    #: false is the escape hatch for a one-off correction that should not
    #: become the default for every future scan of that phantom.
    save_profile: bool | None = None


@app.post("/api/analyses/{aid}/confirm")
def confirm_stage(aid: str, body: StageConfirm, request: Request):
    rec = store.get(aid)
    if rec is None:
        raise HTTPException(404, "not found")
    order = ["A", "B", "C", "D", "E", "F"]
    if body.stage not in order:
        raise HTTPException(400, "bad stage")
    nxt = order[min(order.index(body.stage) + 1, len(order) - 1)]
    store.update(aid, stage=nxt)
    store.audit(aid, body.stage, "confirmed", {"note": body.note})

    out = {"stage": nxt, "profile_saved": False, "profile": None,
           "profile_error": None}
    # Confirming Stage C is the moment the operator says "these measuring
    # points are right for this phantom" — so that is when the layout becomes
    # the phantom's stored default. Saving on every drag instead would let a
    # half-finished correction become the default for everyone.
    if body.stage != "C" or body.save_profile is False:
        return out
    label = store.profile_key(rec.get("phantom") or "")
    if not label:
        out["profile_error"] = ("no phantom is named on this analysis, so the "
                                "measuring points could not be stored for "
                                "future scans of it")
        return out
    if not rec.get("geometry"):
        out["profile_error"] = "there is no confirmed geometry to store"
        return out
    try:
        reg = _reg(aid, rec).summary()
    except HTTPException:
        reg = {}
    layout = layout_profile.extract_layout(
        rec["geometry"], pdef_name=pdef.name, pdef_version=pdef.version,
        algo_version=ALGO_VERSION, registration=pipeline.to_jsonable(reg))
    saved = store.save_phantom_profile(
        label, layout, pdef_version=pdef.version, algo_version=ALGO_VERSION,
        source_analysis_id=aid, updated_by=_current_user(request))
    store.audit(aid, "C", "phantom layout stored",
                {"phantom": label, "rois": saved["n_rois"]})
    audit("phantom_profile", user=_current_user(request),
          client=_client_key(request), analysis=aid, phantom=label,
          outcome="saved", rois=saved["n_rois"])
    log.info("stored measuring-point layout for phantom=%r from analysis=%s "
             "(%d ROIs)", label, aid, saved["n_rois"])
    out["profile_saved"] = True
    out["profile"] = saved
    return out


class ComputeBody(BaseModel):
    sid_mm: float = 1000.0


@app.post("/api/analyses/{aid}/compute")
def compute(aid: str, body: ComputeBody, request: Request):
    rec = store.get(aid)
    if not rec or not rec.get("geometry"):
        raise HTTPException(400, "no confirmed geometry")
    _require_unsigned(rec, "recomputing the results")
    store.update(aid, sid_mm=body.sid_mm)
    # Keep the in-hand record in step with what was just written: building the
    # context from the stale copy analysed with the PREVIOUS SID, so the
    # field-alignment %-of-SID verdict disagreed with the SID shown everywhere.
    rec["sid_mm"] = body.sid_mm
    ctx = _ctx(aid, rec)
    results = pipeline.compute_all(ctx, rec["geometry"])
    status = pipeline.overall_status(results)
    store.update(aid, results=results, status=status, stage="F")
    store.audit(aid, "E", "computed", {"overall": status,
                                       "sid_mm": body.sid_mm})
    audit("compute", user=_current_user(request), client=_client_key(request),
          analysis=aid, outcome=status, sid_mm=body.sid_mm)
    log.info("computed analysis=%s overall=%s", aid, status)
    baseline = store.baseline_for(rec["signature"], rec.get("phantom", ""),
                                  exclude_id=aid)
    return pipeline.to_jsonable({
        "results": results, "overall": status,
        "baseline": ({"id": baseline["id"],
                      "phantom": baseline.get("phantom", ""),
                      "acquired_at": baseline.get("acquired_at", ""),
                      "created_at": baseline.get("created_at", ""),
                      "rows": flatten_results(baseline["results"])}
                     if baseline and baseline.get("results") else None),
    })


class FinalizeBody(BaseModel):
    #: None leaves the baseline flag alone. Finalising is not the only way to
    #: set it any more, so re-finalising must not silently clear it.
    baseline: bool | None = None


def _set_baseline(aid: str, rec: dict, value: bool, request: Request) -> dict:
    if value and rec.get("reduced_precision"):
        raise HTTPException(
            400, "A reduced-precision analysis (a plain image, 8-bit and "
                 "without acquisition metadata) cannot be a reference.")
    if value and not rec.get("results"):
        raise HTTPException(
            400, "This analysis has no results yet, so there is nothing for "
                 "later scans to be compared against.")
    try:
        out = store.set_baseline(aid, value)
    except KeyError:
        raise HTTPException(404, "not found")   # deleted while we were editing
    store.audit(aid, "F", "baseline set" if value else "baseline cleared",
                {"phantom": out["phantom"], "replaced": out["replaced"]})
    audit("baseline", user=_current_user(request), client=_client_key(request),
          analysis=aid, outcome="set" if value else "cleared",
          phantom=out["phantom"], signature=out["signature"],
          replaced=out["replaced"])
    log.info("baseline %s for phantom=%r protocol=%r: analysis=%s%s",
             "set" if value else "cleared", out["phantom"], out["signature"],
             aid, f" (replacing {out['replaced']})" if out["replaced"] else "")
    return out


class BaselineBody(BaseModel):
    baseline: bool = True


@app.post("/api/analyses/{aid}/baseline")
def set_baseline(aid: str, body: BaselineBody, request: Request):
    """Make this analysis its phantom's reference, or stop it being one.

    Scoped to the phantom AND the protocol: two phantoms can differ by design
    and both be valid, so each needs its own reference, while comparing across
    protocols is meaningless whatever the phantom."""
    rec = store.get(aid)
    if rec is None:
        raise HTTPException(404, "not found")
    out = _set_baseline(aid, rec, bool(body.baseline), request)
    return {"ok": True, **out}


@app.get("/api/baselines")
def list_baselines():
    """Every current reference — one per phantom per protocol."""
    return {"baselines": store.baselines()}


@app.post("/api/analyses/{aid}/finalize")
def finalize(aid: str, body: FinalizeBody, request: Request):
    rec = store.get(aid)
    if rec is None:
        raise HTTPException(404, "not found")
    store.update(aid, stage="F", status=rec.get("status") or "complete")
    changed = None
    if body.baseline is not None:
        changed = _set_baseline(aid, rec, body.baseline, request)
    store.audit(aid, "F", "finalized", {"baseline": body.baseline})
    audit("finalize", user=_current_user(request), client=_client_key(request),
          analysis=aid, baseline=body.baseline,
          site=rec.get("site"), phantom=rec.get("phantom"))
    return {"ok": True, "baseline": changed}


class DeleteBody(BaseModel):
    admin_password: str = ""
    reason: str = ""


@app.post("/api/analyses/{aid}/delete")
def delete_analysis(aid: str, body: DeleteBody, request: Request):
    """Delete an analysis, its stored source file and its edit history.

    Deliberately hard to do by accident on a shared installation:
      * a separate ADMIN password is required — not the everyday login;
      * a written reason of at least a few characters must be given, and is
        recorded in the audit log next to who did it and from where;
      * every attempt, successful or not, goes to the audit log.
    With no admin password configured the endpoint refuses outright.

    Typing the analysis id back used to be required as well. It was dropped
    because it protected nothing an operator could not satisfy by copy-paste,
    while the failure it produced was a silent 400 the browser showed for six
    seconds — after which a re-upload of the same file offered to reopen the
    record the operator believed they had deleted."""
    user = _current_user(request)
    client = _client_key(request)
    rec = store.get(aid)
    if rec is None:
        raise HTTPException(404, "not found")

    if not cfg.deletion_enabled:
        audit("delete", user=user, client=client, analysis=aid,
              outcome="refused", refusal="deletion disabled")
        raise HTTPException(
            403, "Deletion is disabled on this installation. An administrator "
                 "must set PHANTOMQA_ADMIN_PASSWORD_HASH in .env "
                 "(python -m phantom_qa.manage set-admin-password).")

    wait = admin_throttle.locked_for(client)
    if wait > 0:
        audit("delete", user=user, client=client, analysis=aid,
              outcome="throttled")
        raise HTTPException(429, f"Too many failed admin attempts. Try again in "
                                 f"{wait // 60 + 1} min.")

    if not verify_password(body.admin_password, cfg.admin_password_hash):
        admin_throttle.record_failure(client)
        audit("delete", user=user, client=client, analysis=aid,
              outcome="denied", refusal="bad admin password")
        log.warning("delete denied (bad admin password) analysis=%s client=%s "
                    "user=%s", aid, client, user)
        raise HTTPException(401, "Incorrect administrator password.")

    # After the password, so a missing reason cannot be used to probe whether a
    # password was right, and so a wrong password still answers 401.
    reason = (body.reason or "").strip()
    if len(reason) < MIN_DELETE_REASON_CHARS:
        audit("delete", user=user, client=client, analysis=aid,
              outcome="refused", refusal="no reason given")
        raise HTTPException(
            400, f"Give a reason of at least {MIN_DELETE_REASON_CHARS} "
                 f"characters. It is recorded in the audit log and is the only "
                 f"record of why this data was destroyed.")
    reason = reason[:MAX_DELETE_REASON_CHARS]

    admin_throttle.reset(client)
    out = store.delete(aid)
    audit("delete", user=user, client=client, analysis=aid, outcome="ok",
          site=rec.get("site"), phantom=rec.get("phantom"),
          source=rec.get("source_name"), sha256=rec.get("sha256"),
          created_at=rec.get("created_at"), acquired_at=rec.get("acquired_at"),
          layout_deleted=out["profile_deleted"], reason=reason)
    log.warning("DELETED analysis=%s site=%r phantom=%r by user=%s client=%s "
                "layout_deleted=%s", aid, rec.get("site"), rec.get("phantom"),
                user, client, out["profile_deleted"])
    _forget(aid)
    return {"ok": True, "phantom": out["phantom"],
            "layout_deleted": out["profile_deleted"]}


@app.get("/api/deletion_policy")
def deletion_policy():
    return {"enabled": cfg.deletion_enabled,
            "requires_admin_password": True,
            "requires_reason": True,
            "min_reason_chars": MIN_DELETE_REASON_CHARS}


class ValidationBody(BaseModel):
    status: str                      # validated | conditionally_validated |
                                     # not_validated | "" to withdraw
    validated_by: str = ""           # the NAME of the person signing off
    comment: str = ""
    admin_password: str = ""


@app.get("/api/validation_policy")
def validation_policy():
    return {"enabled": cfg.deletion_enabled,     # same admin credential
            "states": list(VALIDATION_STATES),
            "labels": VALIDATION_LABELS}


@app.post("/api/analyses/{aid}/validation")
def set_validation(aid: str, body: ValidationBody, request: Request):
    """Administrator's ruling on an analysis.

    Gated by the same administrator password as deletion, because it is the
    other decision an ordinary user must not be able to make. The approver's
    NAME is recorded separately from the password: a shared credential proves
    the right to sign off, not who did it."""
    user, client = _current_user(request), _client_key(request)
    rec = store.get(aid)
    if rec is None:
        raise HTTPException(404, "not found")

    if not cfg.deletion_enabled:
        audit("validation", user=user, client=client, analysis=aid,
              outcome="refused", refusal="no administrator password configured")
        raise HTTPException(
            403, "Validation requires an administrator password. Set "
                 "PHANTOMQA_ADMIN_PASSWORD_HASH in .env "
                 "(python -m phantom_qa.manage set-admin-password).")

    wait = admin_throttle.locked_for(client)
    if wait > 0:
        audit("validation", user=user, client=client, analysis=aid,
              outcome="throttled")
        raise HTTPException(429, f"Too many failed admin attempts. Try again in "
                                 f"{wait // 60 + 1} min.")

    if not verify_password(body.admin_password, cfg.admin_password_hash):
        admin_throttle.record_failure(client)
        audit("validation", user=user, client=client, analysis=aid,
              outcome="denied", refusal="bad admin password")
        log.warning("validation denied (bad admin password) analysis=%s "
                    "client=%s", aid, client)
        raise HTTPException(401, "Incorrect administrator password.")
    admin_throttle.reset(client)

    try:
        applied = store.set_validation(aid, body.status, body.validated_by,
                                       body.comment)
    except ValueError as e:
        raise HTTPException(400, str(e))

    before = {"validation_status": rec.get("validation_status", ""),
              "validated_by": rec.get("validated_by", "")}
    store.audit(aid, "F", "validation set", applied)
    audit("validation", user=user, client=client, analysis=aid,
          outcome=applied["validation_status"] or "withdrawn",
          approver=applied["validated_by"], comment=applied["validation_comment"],
          before=before, site=rec.get("site"), phantom=rec.get("phantom"))
    log.info("validation analysis=%s -> %r by %r (login %s)", aid,
             applied["validation_status"], applied["validated_by"], user)
    return {"ok": True, **applied}


@app.get("/api/analyses/{aid}/verify")
def verify(aid: str, request: Request):
    """Re-hash the stored source file and compare with the recorded SHA-256."""
    result = store.verify_integrity(aid)
    if result["status"] == "not_found":
        raise HTTPException(404, "not found")
    outcome = "ok" if result["status"] == "ok" else "FAILED"
    audit("verify", user=_current_user(request), client=_client_key(request),
          analysis=aid, outcome=outcome, result=result["status"])
    if result["status"] != "ok":
        log.error("integrity check %s for analysis=%s: %s",
                  result["status"], aid, result["message"])
    return result


@app.get("/api/analyses/{aid}/export.json")
def export_json(aid: str):
    rec = store.get(aid)
    if rec is None:
        raise HTTPException(404, "not found")
    return pipeline.to_jsonable(rec)


@app.get("/api/analyses/{aid}/export.csv", response_class=PlainTextResponse)
def export_csv_one(aid: str):
    rec = store.get(aid)
    if rec is None:
        raise HTTPException(404, "not found")
    return csv_export([rec])


def _selected_records(ids: str = "", site: str = "", phantom: str = "",
                      signature: str = "", validation: str = "") -> list[dict]:
    """Records for an explicit id list, or for a label filter.

    Every filter the History tab offers has to be honoured here too. The
    validation filter was not, so exporting "the analyses nobody has signed
    off" silently produced a file covering all of them — the sort of thing that
    is only noticed once the wrong list has been acted on."""
    if ids:
        wanted = [a.strip() for a in ids.split(",") if a.strip()]
        return store.get_slim(wanted)
    listing = store.list_all(site=site or None, phantom=phantom or None,
                             signature=signature or None,
                             validation=validation or None,
                             completed_only=True)
    # One batched query instead of a full get() per row — the exports and the
    # comparison report read labels and results, never the geometry blob.
    return store.get_slim([item["id"] for item in listing])


@app.get("/api/export.csv", response_class=PlainTextResponse)
def export_csv_many(ids: str = "", site: str = "", phantom: str = "",
                    signature: str = "", validation: str = "",
                    layout: str = "long"):
    _check_filters(validation)
    recs = _selected_records(ids, site, phantom, signature, validation)
    if not recs:
        raise HTTPException(404, "no matching analyses")
    return wide_csv_export(recs) if layout == "wide" else csv_export(recs)


@app.get("/api/comparison_report.html", response_class=HTMLResponse)
def comparison_report(ids: str = "", site: str = "", phantom: str = "",
                      signature: str = "", validation: str = ""):
    _check_filters(validation)
    recs = _selected_records(ids, site, phantom, signature, validation)
    if not recs:
        raise HTTPException(404, "no matching analyses")
    suffix = ""
    if site or phantom:
        suffix = " — " + " / ".join(x for x in (site, phantom) if x)
    return build_comparison_report(
        recs, title_suffix=suffix,
        filters={"site": site, "phantom": phantom, "signature": signature,
                 "validation": validation})


@app.get("/api/analyses/{aid}/report.html", response_class=HTMLResponse)
def report_html(aid: str):
    rec = store.get(aid)
    if rec is None:
        raise HTTPException(404, "not found")
    overlay = None
    if rec.get("geometry"):
        try:
            ctx = _ctx(aid)
            overlay = pipeline.render_overlay(_scan(aid), ctx, rec["geometry"])
        except Exception:
            overlay = None
    baseline = store.baseline_for(rec["signature"], rec.get("phantom", ""),
                                  exclude_id=aid)
    # the report states whether the source file still matches its recorded hash
    integrity = store.verify_integrity(aid)
    if integrity.get("status") != "ok":
        log.error("integrity %s while building report for analysis=%s",
                  integrity.get("status"), aid)
    return build_report(rec, overlay_png=overlay, baseline=baseline,
                        integrity=integrity)


@app.get("/api/trends")
def trends(signature: str = "", site: str = "", phantom: str = "",
           validation: str = "", ids: str = ""):
    """Trend data for a label filter, a signature, or an explicit id list."""
    _check_filters(validation)
    if ids:
        listing = [{"id": a.strip()} for a in ids.split(",") if a.strip()]
    else:
        listing = store.list_all(site=site or None, phantom=phantom or None,
                                 signature=signature or None,
                                 validation=validation or None,
                                 completed_only=True)
    out = []
    for rec in store.get_slim([item["id"] for item in listing]):
        if not rec.get("results"):
            continue
        # Both dates travel separately. Collapsing them here is what made a
        # detector with a reset clock indistinguishable from a correct one.
        out.append({"id": rec["id"], "created_at": rec["created_at"],
                    "acquired_at": rec.get("acquired_at") or "",
                    "acquired_flag": acquisition_flag(rec),
                    "source_name": rec.get("source_name", ""),
                    "site": rec.get("site", ""), "phantom": rec.get("phantom", ""),
                    "signature": rec.get("signature", ""),
                    "status": rec.get("status", ""),
                    "is_baseline": rec["is_baseline"],
                    "rows": flatten_results(rec["results"])})
    out.sort(key=lambda a: (a["acquired_at"] or a["created_at"]))
    return pipeline.to_jsonable({
        "filter": {"signature": signature, "site": site, "phantom": phantom,
                   "validation": validation},
        "analyses": out})


@app.get("/api/signatures")
def signatures():
    sigs = {}
    for item in store.list_all():
        sigs.setdefault(item["signature"], 0)
        sigs[item["signature"]] += 1
    return {"signatures": [{"signature": k, "count": v} for k, v in sigs.items()]}


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return _login_page()


_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


@app.get("/", response_class=HTMLResponse)
def index():
    """index.html with the mount prefix injected, so the single-page app builds
    its URLs correctly whether it is served from / or from /x-ray/."""
    with open(os.path.join(_STATIC_DIR, "index.html"), encoding="utf-8") as f:
        page = f.read()
    return page.replace("<head>", f'<head>\n<base href="{html_escape(_base_href())}">', 1)


app.mount("/", StaticFiles(directory=_STATIC_DIR, html=False), name="static")
