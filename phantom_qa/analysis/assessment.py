"""Shared result completeness and reliability checks for every analysis flow.

Measurements and acceptance thresholds remain in the individual test modules.
"""
from __future__ import annotations

import copy
import math

TESTS = ("geometry", "linepairs", "lowcontrast", "uniformity", "wedge")
LABELS = {"geometry": "Geometry", "linepairs": "Line pairs", "lowcontrast": "Low contrast",
          "uniformity": "Uniformity", "wedge": "Wedge"}
SIDES = ("top", "right", "bottom", "left")


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _point(value):
    return isinstance(value, (list, tuple)) and len(value) == 2 and all(_finite(v) for v in value)


def _roi_valid(roi):
    if not isinstance(roi, dict):
        return False
    kind = roi.get("type")
    if kind == "rect":
        points = roi.get("corners_px")
        return isinstance(points, list) and len(points) == 4 and all(_point(p) for p in points)
    if kind == "segment":
        return _point(roi.get("p0_px")) and _point(roi.get("p1_px"))
    if kind in ("circle", "annulus"):
        radii = ["radius_px"] if kind == "circle" else ["inner_radius_px", "outer_radius_px"]
        return _point(roi.get("center_px")) and all(_finite(roi.get(k)) and roi[k] > 0 for k in radii)
    return kind == "point" and _point(roi.get("center_px"))


def collect_overlays(geometry):
    """Keep all usable proposed ROIs, including fallback and incomplete regions."""
    overlays = []
    def walk(node):
        if isinstance(node, dict):
            if _roi_valid(node):
                overlays.append(node)
            else:
                for value in node.values():
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(geometry)
    corners = geometry.get("corners") if isinstance(geometry, dict) else None
    if isinstance(corners, dict):
        roi = {"type": "rect", "id": "geometry/corners",
               "corners_px": [corners[k].get("px") if isinstance(corners.get(k), dict) else None
                              for k in ("TL", "TR", "BR", "BL")]}
        if _roi_valid(roi):
            overlays.append(roi)
    fields = geometry.get("field_edges") if isinstance(geometry, dict) else None
    if isinstance(fields, dict):
        for side, field in fields.items():
            roi = {"type": "point", "id": f"geometry/field/{side}",
                   "center_px": field.get("edge_pt_px") if isinstance(field, dict) else None}
            if _roi_valid(roi):
                overlays.append(roi)
    return overlays


def _manually_adjusted(node):
    if isinstance(node, dict):
        return node.get("manually_adjusted") is True or any(_manually_adjusted(v) for v in node.values())
    return isinstance(node, list) and any(_manually_adjusted(v) for v in node)


def assess_features(geometry, results, pdef, tests=TESTS):
    """Keep malformed/incomplete output local to the affected feature."""
    features = []
    for name in tests:
        node = results.get(name)
        if isinstance(node, dict) and "raw_invalid_result" in node:
            results = {**results, name: copy.deepcopy(node["raw_invalid_result"])}
        elif isinstance(node, dict) and "measurement_status" in node:
            original = {k: v for k, v in node.items() if k not in ("_assessment", "analysis_issues", "measurement_status")}
            if node["measurement_status"] is None:
                original.pop("status", None)
            else:
                original["status"] = node["measurement_status"]
            results = {**results, name: original}
        try:
            features.extend(_assess_features(geometry, results, pdef, (name,)))
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            features.append({"id": name, "label": LABELS[name], "state": "attention",
                             "technical_attention": True, "quality_attention": False,
                             "issues": [{"kind": "invalid_result", "path": name,
                                         "detail": f"Incomplete analysis output: {type(exc).__name__}: {exc}"}],
                             "overlays": [], "position_missing": True, "malformed_output": True})
        features[-1]["overlays"] = collect_overlays(geometry.get(name) or {})
    return features


def _assess_features(geometry, results, pdef, tests):
    """Conservative presentation of existing verdicts, with completeness guards.

    No numerical QA limits are evaluated here. Required rows come from the
    phantom definition; positioning/reliability flags come from the algorithms.
    Position metadata is checked using its detector-specific structure.
    """
    features = []
    for name in tests:
        geom = geometry.get(name) or {}
        result = results.get(name) or {}
        issues, overlays = [], []

        def issue(kind, path, detail, *, status=None):
            item = {"kind": kind, "path": path, "detail": str(detail)}
            if status is not None:
                item["status"] = status
            if item not in issues:
                issues.append(item)

        def numeric(node, keys, path):
            for key in keys:
                # Explicit NULL keeps existing metric export/report readers
                # compatible with an incomplete row without inventing values.
                node.setdefault(key, None)
                if not _finite(node.get(key)):
                    issue("invalid_result", f"{path}.{key}", "Required measurement is missing or invalid")
                elif key in ("n", "obj_n", "bg_n") and node[key] <= 0:
                    issue("invalid_result", f"{path}.{key}", "Measuring region contains no usable pixels")

        def position(node, path):
            if _manually_adjusted(node):
                return True
            if not isinstance(node, dict) or node.get("detected") is not True:
                issue("detection_error", path, (node or {}).get("reason", "Automatic position was not detected"))
                return False
            positioning = node.get("positioning")
            # Line-pair groups carry a strip pose and a local centroid, rather
            # than an `accepted` flag directly on `positioning`.
            pose = (positioning.get("strip") or positioning) if positioning else {}
            if pose.get("accepted") is False:
                issue("detection_error", path, positioning.get("reason", "Automatic position was not accepted"))
                return False
            return True

        def overlay(roi, path):
            if _roi_valid(roi):
                overlays.append(roi)
            else:
                issue("detection_error", path, "Detected position is missing or invalid")

        def rows(key, definitions, id_key="id"):
            expected = [d[id_key] for d in definitions]
            actual = result.get("rows") or []
            placed = geom.get(key) or []
            if any(not isinstance(r, dict) or id_key not in r for r in actual):
                raise ValueError("Measurement row has no valid feature identity")
            if (len(actual) != len(expected) or {r.get(id_key) for r in actual} != set(expected)
                    or len(placed) != len(expected) or {r.get(id_key) for r in placed} != set(expected)):
                issue("missing_result", key, "Required feature positions or measurement rows are missing")
            return actual, placed

        def statuses(node, path="results"):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key.startswith("_") or key in ("measurement_status", "analysis_issues"):
                        continue
                    if key == "status" or key.endswith("_status"):
                        if value != "pass":
                            kind = "quality_attention" if value in ("fail", "warn") else "invalid_result"
                            issue(kind, f"{path}.{key}", f"Existing algorithm verdict: {value}", status=value)
                    elif isinstance(value, (dict, list)):
                        statuses(value, f"{path}.{key}")
            elif isinstance(node, list):
                for i, value in enumerate(node):
                    statuses(value, f"{path}[{i}]")

        if geom.get("_error"):
            issue("detection_error", "geometry", geom["_error"])
        if result.get("error"):
            issue("computation_error", "results", result["error"])
        if not geom or not result:
            issue("missing_result", name, "Automatic analysis is missing")
        statuses(result)
        if name == "geometry":
            for key in ("dimension_status", "field_status"):
                if key not in result:
                    issue("missing_result", key, "Required subtest verdict is missing")
            numeric(result.get("dimensions") or {}, ["mean_side_mm", "dev_from_nominal_pct"], "dimensions")
            if (result.get("scale") or {}).get("reliable") is not True:
                issue("invalid_result", "scale", "The measurement scale is unreliable")
            corners = geom.get("corners") or {}
            points = [(corners.get(k) or {}).get("px") for k in ("TL", "TR", "BR", "BL")]
            overlay({"type": "rect", "corners_px": points, "id": "geometry/corners"}, "corners")
            for side in SIDES:
                ruler = (geom.get("rulers") or {}).get(side) or {}
                if position(ruler, f"rulers.{side}"):
                    overlay(ruler.get("probe"), f"rulers.{side}.probe")
                measured = (result.get("rulers") or {}).get(side) or {}
                if measured.get("detected") is not True or measured.get("reliable") is not True:
                    issue("invalid_result", f"rulers.{side}", "Ruler could not be measured reliably")
                numeric(measured, ["pitch_mm", "pitch_dev_pct", "central_line_from_edge_mm",
                                   "linearity_rms_mm"], f"rulers.{side}")
                edge = (geom.get("field_edges") or {}).get(side) or {}
                if position(edge, f"field_edges.{side}"):
                    overlay({"type": "point", "center_px": edge.get("edge_pt_px"),
                             "id": f"geometry/field/{side}"}, f"field_edges.{side}")
                alignment = (result.get("field_alignment") or {}).get(side) or {}
                if alignment.get("detected") is not True or "status" not in alignment:
                    issue("missing_result", f"field_alignment.{side}", "Field alignment could not be measured")
                numeric(alignment, ["pct_of_sid", "deviation_from_central_line_mm"], f"field_alignment.{side}")
        else:
            if "status" not in result:
                issue("missing_result", "status", "Required group verdict is missing")
            if name == "linepairs":
                actual, placed = rows("groups", pdef.linepairs["groups"])
                for row in actual:
                    numeric(row, ["mean", "std", "n"], row.get("id", "row"))
                    numeric(row.get("linearity") or {}, ["measured_pitch_mm", "pitch_dev_pct", "residual_rms_mm"], "linearity")
                    if "status" not in row:
                        issue("missing_result", "rows.status", "Subtest verdict is missing")
                for node in placed:
                    if position(node, node["id"]):
                        overlay(node.get("roi"), node["id"])
                        overlay(node.get("profile_seg"), node["id"])
            elif name == "uniformity":
                actual, placed = rows("squares", pdef.uniformity["squares"])
                numeric(result, ["snr_avg", "mean_avg", "max_abs_dsnr_pct"], name)
                for row in actual:
                    numeric(row, ["mean", "std", "n", "snr", "dsnr_pct", "dmean_pct"], row.get("id", "row"))
                    if "status" not in row:
                        issue("missing_result", "rows.status", "Subtest verdict is missing")
                for node in placed:
                    if position(node, node["id"]):
                        overlay(node.get("roi"), node["id"])
            elif name == "lowcontrast":
                actual, placed = rows("circles", pdef.lowcontrast["circles"])
                located = position(geom, name)
                if located:
                    overlay(geom.get("block"), "block")
                grid = geom.get("grid_positioning") or {}
                if grid.get("accepted") is not True and not _manually_adjusted(geom.get("block")):
                    issue("detection_error", "grid_positioning", grid.get("reason", "Circle grid could not be verified"))
                elif located:
                    for node in placed:
                        overlay(node.get("roi"), node["id"])
                for row in actual:
                    numeric(row, ["obj_mean", "obj_std", "obj_n", "bg_mean", "bg_std", "bg_n", "cnr", "abs_cnr"], row.get("id", "row"))
                if result.get("ordering_ok") is not True:
                    issue("quality_attention" if result.get("ordering_ok") is False else "missing_result",
                          "ordering_ok", "Existing contrast ordering check did not pass")
            elif name == "wedge":
                actual, placed = rows("steps", pdef.wedge["steps"], "step")
                if position(geom, name):
                    overlay(geom.get("axis"), "axis")
                    for node in placed:
                        if node.get("fits") is True or _manually_adjusted(node.get("roi")):
                            overlay(node.get("roi"), f"step.{node['step']}")
                        else:
                            issue("detection_error", f"step.{node['step']}", "Measuring region does not fit inside its detected step")
                for row in actual:
                    numeric(row, ["mean", "std", "n"], f"step.{row.get('step')}")
                    if row.get("saturated") is not False:
                        issue("quality_attention" if row.get("saturated") is True else "missing_result",
                              f"step.{row.get('step')}.saturated", "Existing saturation check did not pass")
                numeric(result.get("fit") or {}, ["r2", "slope"], "fit")
                numeric(result, ["span", "dynamic_range_ratio"], name)
                for key, expected in (("monotonic", True), ("saturated", False)):
                    if result.get(key) is not expected:
                        issue("quality_attention" if key in result else "missing_result", key,
                              "Existing wedge check did not pass")
        technical = any(i["kind"] != "quality_attention" for i in issues)
        features.append({"id": name, "label": LABELS[name],
                         "state": "attention" if issues else "pass",
                         "technical_attention": technical,
                         "quality_attention": any(i["kind"] == "quality_attention" for i in issues),
                         "issues": issues, "overlays": overlays,
                         "position_missing": any(i["kind"] == "detection_error" for i in issues)})
    return features


def evaluate_results(geometry, results, pdef, tests=TESTS):
    """Shared, idempotent result checks for Analyze, Auto Analyze and reanalysis.

    Preserve numerical measurements and existing n/a/error results. An
    incomplete apparent pass is marked as an analysis error in every workflow.
    """
    results = copy.deepcopy(results)
    features = assess_features(geometry, results, pdef, tests)
    for feature in features:
        name = feature["id"]
        node = results.setdefault(name, {})
        if not isinstance(node, dict) or feature.get("malformed_output"):
            raw = node.get("raw_invalid_result", node) if isinstance(node, dict) else node
            node = results[name] = {"status": "error", "raw_invalid_result": raw}
        node["_assessment"] = {k: v for k, v in feature.items() if k != "overlays"}
        technical = [i for i in feature["issues"] if i["kind"] != "quality_attention"]
        if technical:
            node["analysis_issues"] = technical
            reasons = node.setdefault("reasons", [])
            for item in technical:
                detail = f"{item['path']}: {item['detail']}"
                if detail not in reasons:
                    reasons.append(detail)
            if node.get("status") not in ("error", "n/a"):
                node.setdefault("measurement_status", node.get("status"))
                node["status"] = "error"
        elif feature["quality_attention"] and node.get("status") == "pass":
            node.setdefault("measurement_status", "pass")
            hard_failure = any(i.get("status") == "fail"
                               or i["path"].endswith(("saturated", "monotonic")) for i in feature["issues"])
            node["status"] = "fail" if hard_failure else "warn"
    return results, features


