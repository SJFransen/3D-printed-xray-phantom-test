"""Image-based regressions for near-edge collimation and scale reliability."""
import copy
import io

import numpy as np
import pytest

from phantom_qa.analysis import geometry
from phantom_qa.analysis.common import Ctx
from phantom_qa import ingest, pipeline
from phantom_qa.registration import Registration
from test_fieldedge_zero import zero_field
from test_analysis_revisions import alignment_case
from test_auto_analyse import algorithms, passing


@pytest.mark.parametrize("angle,mirrored,ppm", [(0, False, 1), (27, False, 2), (-38, True, 3)])
@pytest.mark.parametrize("background", [0., 3900.])
def test_ideal_322_boundary_detected_from_pixels(angle, mirrored, ppm, background):
    ctx = zero_field(offset=11.25, angle=angle, mirrored=mirrored, margin=55)
    # Resample geometry and image together to exercise detector resolution.
    from scipy.ndimage import zoom
    factor = ppm / 2
    ctx.pixels = zoom(ctx.pixels, factor, order=0)
    ctx.T.A *= factor
    ctx.T.t *= factor
    ctx.pixels[ctx.pixels == 0] = background
    for side in geometry.SIDES:
        edge = geometry._field_edge(ctx, side)
        assert edge["detected"], edge
        assert edge["offset_from_edge_mm"] == pytest.approx(11.25, abs=1.5)
        assert edge["agreeing_profiles"] >= 3
        assert edge["edge_spread_mm"] <= 2


def test_nonzero_boundary_with_narrow_visible_margin():
    ctx = zero_field(offset=11.25, margin=18)
    ctx.pixels[ctx.pixels == 0] = 3900
    for side in geometry.SIDES:
        edge = geometry._field_edge(ctx, side)
        assert edge["detected"], edge
        assert edge["offset_from_edge_mm"] == pytest.approx(11.25, abs=1)


def test_local_zero_artifact_cannot_replace_supported_boundary():
    ctx = zero_field(offset=15)
    point = np.rint(ctx.T.mm_to_px([0, ctx.pdef.side_mm / 2 - 10])).astype(int)
    ctx.pixels[point[1]-6:point[1]+6, point[0]] = 0
    edge = geometry._field_edge(ctx, "top")
    assert edge["detected"] and edge["offset_from_edge_mm"] == pytest.approx(15, abs=1)
    assert edge["agreeing_profiles"] == 4


def test_padding_is_not_a_field_boundary():
    ctx = zero_field(offset=15)
    ctx.params = {"scan_meta": {"_padding_range_normalized": [0., 0.]}}
    for side in geometry.SIDES:
        assert not geometry._field_edge(ctx, side)["detected"]


def test_zero_exposed_background_does_not_measure_the_phantom_face():
    ctx = zero_field(offset=0)
    for side in geometry.SIDES:
        assert not geometry._field_edge(ctx, side)["detected"]


def test_noisy_nonzero_boundary_and_slanted_edge():
    ctx = zero_field(offset=11.25, margin=30)
    yy, xx = np.indices(ctx.pixels.shape)
    mm = ctx.T.px_to_mm(np.c_[xx.ravel(), yy.ravel()]).reshape(*ctx.pixels.shape, 2)
    ctx.pixels[ctx.pixels == 0] = 250
    ctx.pixels[mm[..., 1] > ctx.pdef.side_mm / 2 + 11.25 + .35 * mm[..., 0]] = 3900
    ctx.pixels += np.random.default_rng(42).normal(0, 15, ctx.pixels.shape)
    edge = geometry._field_edge(ctx, "top")
    assert edge["detected"], edge
    assert edge["offset_from_edge_mm"] == pytest.approx(11.25, abs=1)
    assert abs(edge["edge_slope"]) == pytest.approx(.35, abs=.15)


def test_ruler_marks_are_measured_despite_brighter_extra_peak():
    ctx = zero_field()
    yy, xx = np.indices(ctx.pixels.shape)
    mm = ctx.T.px_to_mm(np.c_[xx.ravel(), yy.ravel()]).reshape(*ctx.pixels.shape, 2)
    depth = ctx.pdef.side_mm / 2 - mm[..., 1]
    for mark in [10, 15, 20, 25, 30, 35]:
        ctx.pixels += 1000 * np.exp(-.5 * ((depth - mark) / .4) ** 2)
    ctx.pixels += 2000 * np.exp(-.5 * ((depth - 22.5) / .4) ** 2)
    proposal = geometry._ruler_probe(ctx, "top")
    assert proposal["detected"], proposal
    assert proposal["line_offsets_mm"] == pytest.approx([10, 15, 20, 25, 30, 35], abs=.6)


def test_ruler_pattern_rejects_spurious_peaks_and_missing_marks():
    marks = [10., 15., 20., 25., 30., 35.]
    assert geometry._ruler_pattern(marks + [6., 12.2, 22.4, 37.]) == pytest.approx(marks)
    assert geometry._ruler_pattern([10., 15., 25., 30., 35.]) is None
    assert geometry._ruler_pattern([9., 13., 21., 29., 33., 39.]) is None


def test_competing_edge_lines_are_not_arbitrarily_accepted(monkeypatch):
    ctx = zero_field()
    offsets = iter([0., 6., 3., 3., 6.])  # Two equally supported crossing lines.
    monkeypatch.setattr(geometry, "_zero_field_edge", lambda *args: {
        "detected": True, "offset_from_edge_mm": next(offsets), "side": "top"})
    edge = geometry._field_edge(ctx, "top")
    assert not edge["detected"]
    assert "disagree" in edge["reason"]


def test_primary_diagnostics_keep_missing_values_in_technical_details():
    from phantom_qa.analysis.assessment import summarize_issues
    issues = [{"path": path, "detail": detail} for path, detail in [
        ("results.field_status", "Existing algorithm verdict: n/a"),
        ("scale", "The measurement scale is unreliable"),
        ("field_edges.top", "Insufficient search margin"),
        ("field_alignment.top", "Field alignment could not be measured"),
        ("field_alignment.top.pct_of_sid", "Required measurement is missing"),
        ("rulers.top", "Ruler could not be measured reliably"),
        ("rulers.top.pitch_mm", "Required measurement is missing")]]
    assert summarize_issues(issues) == ["Measurement scale: The measurement scale is unreliable",
        "Top field edge: Insufficient search margin", "Top ruler: Ruler could not be measured reliably"]
    from phantom_qa.report import _analysis_notes
    html = _analysis_notes({"analysis_issues": issues})
    summary, details = html.split("<details>")
    assert "pct_of_sid" not in summary and "pct_of_sid" in details


def test_ruler_scale_rejects_regular_but_disagreeing_pitch(passing, pdef, alignment_case):
    proposal = copy.deepcopy(passing[0]["geometry"])
    proposal["rulers"]["left"]["line_offsets_mm"] = [10. + n * 6 for n in range(6)]
    result = geometry.compute(Ctx(np.zeros((64, 64)), alignment_case, pdef), proposal)
    assert result["scale"]["reliable"]
    assert result["scale"]["rulers_rejected"] == ["left"]
    assert result["scale"]["pitch_measured_mm"] == pytest.approx(5.)
    proposal["rulers"]["bottom"]["line_offsets_mm"] = [10. + n * 6 for n in range(6)]
    assert not geometry.compute(Ctx(np.zeros((64, 64)), alignment_case, pdef), proposal)["scale"]["reliable"]


@pytest.mark.parametrize("polarity", ["MONOCHROME1", "MONOCHROME2"])
def test_padding_survives_ingest_rescale_and_context(polarity, pdef):
    import pydicom
    from test_solutions_and_identity import dicom_bytes
    ds = pydicom.dcmread(io.BytesIO(dicom_bytes()))
    ds.add_new((0x0028, 0x0120), "US", 0)
    ds.add_new((0x0028, 0x0121), "US", 5)
    ds.RescaleSlope, ds.RescaleIntercept = 2, -100
    ds.PhotometricInterpretation = polarity
    buffer = io.BytesIO()
    pydicom.dcmwrite(buffer, ds, write_like_original=False)
    scan = ingest.load_dicom_bytes(buffer.getvalue(), "padding.dcm")
    bounds = scan.meta["_padding_range_normalized"]
    assert bounds[1] - bounds[0] == 10
    assert scan.meta["PixelPaddingValue"] == 0
    registration = Registration(None, np.zeros((4, 2)), 0., {})
    ctx = pipeline.build_ctx(scan, pdef, registration)
    assert ctx.params["scan_meta"]["_padding_range_normalized"] == bounds
    assert not geometry._usable_pixels(ctx, np.array(bounds)).any()
