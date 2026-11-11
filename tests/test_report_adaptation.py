"""Report-only changes must not write measurements or change operator flows."""
import base64
import copy
import io
import json
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest
from PIL import Image

from phantom_qa import comparison_report as comparison, thumbnails
from phantom_qa.analysis.common import Ctx
from phantom_qa.registration import Transform
from phantom_qa.report_controls import SCRIPT_HASH, print_css
from phantom_qa.store import Store
from test_auto_analyse import algorithms, mod, passing, png, client


def context(pdef):
    pixels = np.arange(640 * 640, dtype=float).reshape(640, 640) % 1024
    return Ctx(pixels, Transform(np.array([[2., 0.], [0., -2.]]), np.array([320., 320.])), pdef)


def test_report_regions_do_not_modify_geometry_or_pixels(pdef):
    ctx = context(pdef)
    geometry = {"lowcontrast": {"block": {"center_mm": [0, 0], "size_mm": [80, 70], "angle_deg": 12}}}
    before = copy.deepcopy(geometry)
    pixels = ctx.pixels.copy()
    regions = thumbnails.render_all(ctx, geometry)
    assert set(regions) == set(thumbnails.REGIONS)
    assert all(thumbnails.drawable(r) for r in regions.values())
    assert geometry == before
    np.testing.assert_array_equal(ctx.pixels, pixels)
    for r in regions.values():
        encoded = thumbnails.encode_on(r, r["lo"], r["hi"])
        assert encoded["mime"] == "image/png"
        with Image.open(io.BytesIO(base64.b64decode(encoded["b64"]))) as image:
            assert image.size == (encoded["w"], encoded["h"])


def test_disk_cache_reuses_values_and_recovers_from_damage(tmp_path, pdef):
    ctx = context(pdef)
    rec = {"id": "a", "sha256": "hash", "reg": {"transform": ctx.T.to_dict()}, "geometry": {}}
    calls = []
    def make():
        calls.append(1)
        return ctx
    directory = str(tmp_path / "pictures")
    kwargs = dict(pdef_version=pdef.version, algo_version="test")
    first = thumbnails.pictures_for(rec, directory, make, **kwargs)
    second = thumbnails.pictures_for(rec, directory, make, **kwargs)
    assert len(calls) == 1
    np.testing.assert_array_equal(first["phantom"]["values"], second["phantom"]["values"])
    next((tmp_path / "pictures").glob("*.npz")).write_bytes(b"damaged")
    thumbnails.pictures_for(rec, directory, make, **kwargs)
    assert len(calls) == 2
    rec["reg"]["transform"] = {"different": True}
    thumbnails.pictures_for(rec, directory, make, **kwargs)
    assert len(calls) == 3


def test_cache_key_changes_without_sequence_change():
    rec = {"sha256": "a", "geometry_seq": 0, "geometry": {"x": 1}}
    kw = dict(pdef_version="1", algo_version="1")
    old = thumbnails.cache_key(rec, **kw)
    rec["geometry"]["x"] = 2
    assert thumbnails.cache_key(rec, **kw) != old


def test_pictures_include_every_scan_and_do_not_mix_protocol_windows(monkeypatch):
    records = [{"id": str(i), "signature": "A" if i < 13 else "B", "phantom": "<P>"} for i in range(14)]
    regions = {r["id"]: {name: {"values": np.zeros((2, 2)), "lo": i, "hi": i + 2, "window": "raw"}
                         for name in thumbnails.REGIONS} for i, r in enumerate(records)}
    calls = []
    original = thumbnails.encode_on
    def encode(pic, lo, hi):
        calls.append((lo, hi))
        return original(pic, lo, hi)
    monkeypatch.setattr(thumbnails, "encode_on", encode)
    page = comparison._picture_section(records, ["<label>"] * 14, regions)
    assert page.count('class="report-picture"') == 70
    assert calls[:14] == [(0, 2)] * 13 + [(13, 15)]
    assert "&lt;label&gt;" in page and "&lt;P&gt;" in page
    assert '<label>' not in page


def test_chart_encoding_workers_match_sequential_and_restore_context():
    raw = io.BytesIO()
    Image.fromarray(np.arange(100, dtype=np.uint8).reshape(10, 10)).save(raw, format="PNG")
    expected = comparison._encode_chart(raw.getvalue())
    with ThreadPoolExecutor(max_workers=2) as pool:
        batch = comparison._ChartBatch(pool)
        marker = batch.submit(raw.getvalue())
        batch.raise_first_failure()
        assert batch.fill(marker) == expected
    assert comparison._CHART_BATCH.get() is None


def test_chart_pixels_match_incumbent_rendering():
    import matplotlib.pyplot as plt
    def figure():
        fig, ax = plt.subplots(figsize=(3, 2))
        ax.plot([0, 1, 2], [2, 0, 3], label="Measurements")
        ax.legend()
        return fig
    fig = figure()
    incumbent = io.BytesIO()
    fig.savefig(incumbent, format="png", dpi=115, bbox_inches="tight")
    plt.close(fig)
    adapted = base64.b64decode(comparison._b64(figure()))
    with Image.open(incumbent) as before, Image.open(io.BytesIO(adapted)) as after:
        np.testing.assert_array_equal(np.asarray(before), np.asarray(after))


@pytest.mark.anyio
async def test_compression_skips_images_but_compresses_text():
    from phantom_qa.webapp.compression import TextGZipMiddleware
    from starlette.responses import Response
    async def receive():
        return {"type": "http.request", "body": b""}
    for path, mime, compressed in (("/api/a/image.png", "image/png", False),
                                   ("/api/export.csv", "text/csv", True)):
        output = []
        async def send(message):
            output.append(message)
        app = TextGZipMiddleware(Response(b"a" * 2000, media_type=mime), minimum_size=900)
        await app({"type": "http", "path": path, "method": "GET",
                   "headers": [(b"accept-encoding", b"gzip")]}, receive, send)
        headers = dict(output[0]["headers"])
        assert (headers.get(b"content-encoding") == b"gzip") is compressed


def test_windows_opt_out_fails_harmlessly_on_unsupported_systems():
    import runpy
    from pathlib import Path
    from types import SimpleNamespace
    namespace = runpy.run_path(str(Path(__file__).parents[1] / "run_app.py"))
    function = namespace["_run_at_full_speed"]
    function.__globals__["os"] = SimpleNamespace(name="posix")
    assert function() is False
    # Linux has no ctypes.WinDLL: the unsupported Windows API is caught.
    import ctypes
    if not hasattr(ctypes, "WinDLL"):
        function.__globals__["os"] = SimpleNamespace(name="nt")
        assert function() is False


def test_chart_failure_propagates_first_in_drawing_order(monkeypatch):
    def fail(png):
        raise ValueError(png.decode())
    monkeypatch.setattr(comparison, "_encode_chart", fail)
    with ThreadPoolExecutor(max_workers=2) as pool:
        batch = comparison._ChartBatch(pool)
        batch.submit(b"first")
        batch.submit(b"second")
        with pytest.raises(ValueError, match="first"):
            batch.raise_first_failure()


def test_print_css_escapes_untrusted_margin_text():
    css = print_css('bad"; } </style><script>alert(1)</script>\n')
    assert '</style>' not in css
    assert 'A4 portrait' in css and 'counter(page)' in css
    assert 'A4 landscape' in print_css('comparison', landscape=True)
    assert r'\2192 ' in print_css('a → b')
    assert r'\u2192' not in print_css('a → b')


def test_report_controls_and_cache_do_not_change_saved_record(mod, client, algorithms):
    response = client.post('/api/auto-analyse', files={"file": ("scan.png", png(), "image/png")},
                               data={"site": "Test", "phantom": "P1", "operator": "A"})
    assert response.status_code == 200
    aid = response.json()["analyses"][0]["id"]
    before = mod.store.get(aid)
    report = client.get(f'/api/analyses/{aid}/report.html')
    assert report.status_code == 200
    assert 'Export to PDF' in report.text
    assert 'sha256-' + SCRIPT_HASH in report.headers['content-security-policy']
    page = client.get('/api/comparison_report.html', params={"ids": aid})
    assert page.status_code == 200
    assert 'Scan pictures' in page.text and 'Export to PDF' in page.text
    assert 'sha256-' + SCRIPT_HASH in page.headers['content-security-policy']
    assert 'sha256-' not in client.get('/').headers['content-security-policy']
    after = mod.store.get(aid)
    assert after == before
    assert page.headers.get('content-encoding') == 'gzip'


def test_profile_evidence_is_internal_and_cache_deletion_is_safe(tmp_path, pdef):
    store = Store(str(tmp_path))
    for hostile in ('../outside', '/tmp/other', '.', 'a/b', 'a\\b'):
        assert store.thumbs_dir(hostile) is None
    store.save_phantom_profile('P', {"rois": {}}, source_analysis_id="unknown")
    assert 'source_json' not in store.get_phantom_profile('P')
    with store._conn() as conn:
        source = json.loads(conn.execute('SELECT source_json FROM phantom_profiles').fetchone()[0])
    assert source['geometry_fingerprint'] is None
    directory = tmp_path / 'data' / 'thumbs' / 'safe'
    directory.mkdir(parents=True)
    (directory / 'derived.npz').write_bytes(b'cache')
    store.delete('safe')
    assert not directory.exists()
