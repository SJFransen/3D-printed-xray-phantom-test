"""Inward ruler contrast, uncertainty bounds and the reported real regression."""
from pathlib import Path

import numpy as np
import pytest

from phantom_qa import ingest, pipeline
from phantom_qa.analysis import geometry
from phantom_qa.analysis.common import Ctx
from phantom_qa.analysis.field_intrusion import measure
from phantom_qa.analysis.positioning import rotation
from phantom_qa.registration import Transform


def ruler_image(pdef, intrusion=0., angle=0., mirrored=False, amplitude=600.):
    n, ppm = 700, 2.
    T = Transform(rotation(angle) @ np.diag([ppm, ppm if mirrored else -ppm]), np.array([n/2,n/2]))
    yy, xx = np.indices((n,n))
    mm = T.px_to_mm(np.c_[xx.ravel(), yy.ravel()]).reshape(n,n,2)
    half = pdef.side_mm / 2
    board = np.max(np.abs(mm), axis=2) < half
    pixels = np.where(board, 1500., 200.)
    exposed = mm[...,0] >= -half + intrusion
    pixels[~exposed] = 3000.
    for side, definition in pdef.rulers.items():
        inward = np.asarray(definition["normal_inward_mm"])
        tangent = np.array([-inward[1], inward[0]])
        depth = half + mm @ inward
        along = mm @ tangent
        for mark in range(6):
            center = 10 + 5 * mark
            signal = np.exp(-.5 * ((depth-center)/.3)**2) * (np.abs(along) < (6 if mark%2 == 0 else 3))
            pixels += amplitude * signal * np.where(exposed, 1., .01)
    pixels += np.random.default_rng(42).normal(0, 2, pixels.shape)
    return Ctx(pixels, T, pdef)


@pytest.mark.parametrize("angle,mirrored", [(0,False),(27,False),(-33,True)])
def test_clipped_outer_marks_recover_visibility_inwards(pdef, angle, mirrored):
    ctx = ruler_image(pdef, intrusion=32, angle=angle, mirrored=mirrored)
    edge = measure(ctx, "left", {})
    assert edge["detected"] and edge["measurement_kind"] == "interval"
    assert edge["intrusion_bounds_mm"] == [29.,36.]
    assert [m["visible"] for m in edge["ruler_visibility"]] == [False]*5 + [True]
    proposal = geometry.propose(ctx)
    result = geometry.compute(ctx, proposal)
    assert result["field_status"] == "fail"
    assert result["scale"]["reliable"]
    assert not proposal["rulers"]["left"]["detected"]


def test_visible_rulers_give_upper_bound_not_exact_zero(pdef):
    ctx = ruler_image(pdef)
    edge = measure(ctx, "top", {})
    assert edge["detected"] and edge["intrusion_bounds_mm"] == [0.,11.]
    assert edge["measurement_kind"] == "upper_bound"
    result = geometry.compute(ctx, geometry.propose(ctx))
    assert result["field_reference"] == "phantom_edge_inward"
    assert "field_reference_width_mm" not in result
    assert result["field_status"] == "pass"
    assert result["field_alignment"]["top"]["offset_from_phantom_edge_mm"] is None


def test_equal_weak_contrast_is_not_coverage(pdef):
    ctx = ruler_image(pdef, amplitude=3.)
    assert not measure(ctx,"top",{})["detected"]


def test_interval_crossing_tolerance_warns_and_missing_side_cannot_pass(pdef):
    ctx = ruler_image(pdef, intrusion=17)
    proposal = geometry.propose(ctx)
    result = geometry.compute(ctx, proposal)
    assert result["field_status"] == "warn"
    assert result["field_alignment"]["left"]["intrusion_lower_mm"] < 20
    assert result["field_alignment"]["left"]["intrusion_upper_mm"] > 20
    proposal["field_edges"]["left"] = {"method":"inward_ruler_contrast", "detected":False,"reason":"missing ruler"}
    result = geometry.compute(ctx, proposal)
    assert result["field_status"] == "n/a"
    proposal["field_edges"]["left"] = {"method":"inward_ruler_contrast", "detected":True,
                                         "intrusion_bounds_mm":[20.,None]}
    result = geometry.compute(ctx, proposal)
    assert result["field_status"] == "n/a"
    assert "Invalid" in result["field_alignment"]["left"]["reason"]


def test_malformed_or_incomplete_bounds_cannot_get_overview_pass():
    from phantom_qa.analysis.assessment import geometry_checks
    result = {"field_reference":"phantom_edge_inward", "field_status":"pass",
              "field_alignment": {s: {"detected":True,"status":"pass", "pct_of_sid":1.,
                                       "intrusion_lower_mm":0., "intrusion_upper_mm":11.} for s in geometry.SIDES}}
    assert geometry_checks(result)[1]["status"] == "pass"
    result["field_alignment"].pop("left")
    assert geometry_checks(result)[1]["status"] == "error"
    result["field_alignment"]["left"] = {"detected":True,"status":"pass","pct_of_sid":1.,
                                          "intrusion_lower_mm":None, "intrusion_upper_mm":11.}
    assert geometry_checks(result)[1]["status"] == "error"


SCAN = Path(__file__).resolve().parents[3] / "images" / "Test Bucky 1 LZR QC Leeds" / "DICOM" / "000036"


@pytest.mark.skipif(not SCAN.is_file(), reason="User-supplied DICOM 000036 is not available")
def test_real_000036_detects_lower_ruler_clipping(pdef):
    scan = ingest.load_dicom_bytes(SCAN.read_bytes(), "000036")
    reg = pipeline.run_stage_a(scan, pdef)
    ctx = pipeline.build_ctx(scan, pdef, reg)
    proposal = geometry.propose(ctx)
    result = geometry.compute(ctx, proposal)
    # Registration rotates this image: canonical left is the displayed lower side.
    row = result["field_alignment"]["left"]
    assert result["field_status"] == "fail" and result["scale"]["reliable"]
    assert row["intrusion_lower_mm"] > row["tolerance_mm"]
    assert row["intrusion_upper_mm"] < 40
    assert row["ruler_visibility"][-1]["visible"]
    assert not any(mark["visible"] for mark in row["ruler_visibility"][:-1])
