"""Deliver matching markup/controllers across an application update."""
from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path

from test_authorization import app_mod, user_client


def test_upload_page_delivers_current_version_of_every_asset(user_client, app_mod):
    page = user_client.get("/")
    assert page.status_code == 200
    assert page.headers["Cache-Control"] == "no-store"
    assets = re.findall(r'(?:src|href)="([^"?]+\.(?:js|css))\?v=([a-f0-9]+)"', page.text)
    assert {name for name, _ in assets} == {
        "style.css", "auto-analyse.css", "viewer-tools.js", "upload-form.js",
        "app.js", "auto-analyse.js",
    }
    for name, version in assets:
        response = user_client.get(f"/{name}?v={version}")
        assert response.status_code == 200
        assert hashlib.sha256(response.content).hexdigest().startswith(version)
        assert response.headers["Cache-Control"] == "private, no-cache"
        cached = user_client.get(f"/{name}?v={version}",
                                 headers={"If-None-Match": response.headers["etag"]})
        assert cached.status_code == 304
        assert cached.headers["Cache-Control"] == "private, no-cache"


def test_asset_edit_invalidates_url_without_server_restart(user_client, app_mod, tmp_path, monkeypatch):
    static = tmp_path / "static"
    shutil.copytree(app_mod._STATIC_DIR, static)
    monkeypatch.setattr(app_mod, "_STATIC_DIR", str(static))
    before = user_client.get("/").text
    script = Path(static, "upload-form.js")
    script.write_bytes(script.read_bytes() + b"\n// Updated upload form\n")
    after = user_client.get("/").text
    pattern = r'upload-form\.js\?v=([a-f0-9]+)'
    assert re.search(pattern, before).group(1) != re.search(pattern, after).group(1)


def test_unversioned_assets_also_revalidate(user_client):
    response = user_client.get("/app.js")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-cache"
