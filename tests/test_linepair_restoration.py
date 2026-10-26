"""Withdrawn contour profiles must not undo the restored line-pattern detector."""
import copy

from phantom_qa import layout_profile
from phantom_qa.analysis import linepairs
from test_positioning import synthetic_ctx


def test_withdrawn_contour_profile_is_skipped_but_legacy_profile_still_applies():
    ctx, _ = synthetic_ctx()
    geometry = {"linepairs": linepairs.propose(ctx)}
    before = copy.deepcopy(geometry)
    profile = layout_profile.extract_layout(geometry)
    roi_id = geometry["linepairs"]["groups"][0]["roi"]["id"]
    profile["rois"][roi_id]["center_mm"] = [999, 999]
    profile["scalars"]["linepairs"]["rigid_layout"] = True
    result, report = layout_profile.apply_layout(ctx, geometry, profile)
    assert result == before and roi_id in report["skipped"]
    profile["scalars"]["linepairs"].pop("rigid_layout")
    result, report = layout_profile.apply_layout(ctx, geometry, profile)
    assert result["linepairs"]["groups"][0]["roi"]["center_mm"] == [999, 999]
    assert roi_id in report["applied"]


def test_withdrawn_geometry_cannot_be_resaved_as_an_independent_box_profile():
    ctx, _ = synthetic_ctx()
    geometry = {"linepairs": linepairs.propose(ctx)}
    geometry["linepairs"]["rigid_layout"] = True
    profile = layout_profile.extract_layout(geometry)
    assert not profile["rois"]
    assert "linepairs" not in profile["scalars"]
