"""The confirmed measuring-point layout of one physical phantom.

Why this exists
---------------
Two phantoms built to the same drawing are not the same object. On the MSF
reference scans the printed internal features sit up to ~12 mm apart between
two builds, while repeat scans of ONE build reproduce to a few tenths of a
millimetre. That difference is assembly, not drift: it is a property of the
phantom and it does not change over time. So once an operator has corrected
the measuring points for a phantom, the correction is worth keeping and
replaying on the next scan of that same phantom instead of being re-done by
hand every time.

What is stored, and why it survives a rotated phantom
-----------------------------------------------------
Only phantom-frame millimetres. Registration resolves the phantom's own frame
from the eight possible corner assignments (four rotations x mirror) using the
phantom's asymmetric content, so a scan taken with the phantom turned 90 deg on
the table lands on the same mm coordinates — measured agreement is <= 0.1 mm
for the uniformity, wedge and line-pair objects. Every ``*_px`` field, by
contrast, is a product of THIS scan's transform, so none of them are stored;
they are re-derived with :func:`refresh_px_geometry` when a layout is replayed.

What is deliberately NOT stored
-------------------------------
* field edges — collimation is a property of the exposure, not the phantom, and
  the side names are phantom-frame while the collimator is not;
* ruler probes, corner marks and every ``detected`` / ``measured`` flag — those
  are the per-scan evidence the operator reads in Stage B;
* anything that failed to propose on the source scan.

The 180-degree trap
-------------------
A layout is applied ON TOP of a fresh automatic proposal, never instead of one.
That matters: applying it blind would bypass exactly the detection whose
failures reveal a mis-registered scan. Uniformity and the corner dimensions
cannot detect an orientation error at all (five symmetric squares, four
symmetric sides), and the low-contrast block angle is only defined modulo 180,
which maps circle L1 to within 1 mm of L5. :func:`layout_agrees` compares the
stored layout against the same scan's own proposal on the objects that ARE
orientation-sensitive, and refuses the replay when they disagree.
"""

from __future__ import annotations

import math

from .analysis.common import refresh_px_geometry

LAYOUT_VERSION = 1

ROI_TYPES = ("rect", "circle", "annulus", "segment")

#: The mm-space fields that define each ROI type. Everything else in an ROI
#: dict is derived from these plus the scan's transform.
_MM_FIELDS = {
    "rect": ("center_mm", "size_mm", "angle_deg"),
    "circle": ("center_mm", "dia_mm"),
    "annulus": ("center_mm", "inner_dia_mm", "outer_dia_mm"),
    "segment": ("p0_mm", "p1_mm"),
}

#: Per-test scalars that describe the phantom rather than the exposure.
_SCALARS = {
    "lowcontrast": ("angle_deg", "grid_shift_mm", "bg_ring_mm"),
    "linepairs": ("strip_angle_deg", "roi_size_mm", "roi_angle_offset_deg"),
    "uniformity": ("roi_size_mm",),
    "wedge": ("center_x_mm", "y_top_mm", "y_bottom_mm", "boundaries_y_mm",
              "roi_size_mm", "angle_deg"),
}

#: Objects whose position changes under a wrong 90/180-degree registration.
#: Uniformity and the corner dimensions are excluded on purpose — they are
#: symmetric under every candidate assignment and would vote "fine" for a
#: layout landing on the wrong side of the phantom.
ORIENTATION_PROBES = ("linepairs/G2.0", "linepairs/G1.1", "lowcontrast/block",
                      "wedge/S1", "wedge/S7")

#: Millimetres. Repeat scans of one phantom agree to ~1 mm; two builds of the
#: phantom differ by 3-4 mm; a 180-degree-wrong frame is 100 mm out. Anything
#: past this is not an assembly difference.
ORIENTATION_TOLERANCE_MM = 8.0


# ------------------------------------------------------------- tree walking

def walk_find(node, roi_id, types=ROI_TYPES):
    """The ROI with this id, anywhere in the geometry tree."""
    if isinstance(node, dict):
        if node.get("id") == roi_id and node.get("type") in types:
            return node
        for v in node.values():
            r = walk_find(v, roi_id, types)
            if r is not None:
                return r
    elif isinstance(node, list):
        for v in node:
            r = walk_find(v, roi_id, types)
            if r is not None:
                return r
    return None


def walk_children(node, roi_id, out=None):
    """Every ROI whose id is '<roi_id>/…' — the companions of one handle."""
    if out is None:
        out = []
    prefix = roi_id + "/"
    if isinstance(node, dict):
        if (isinstance(node.get("id"), str) and node["id"].startswith(prefix)
                and node.get("type") in ROI_TYPES):
            out.append(node)
        else:
            for v in node.values():
                walk_children(v, roi_id, out)
    elif isinstance(node, list):
        for v in node:
            walk_children(v, roi_id, out)
    return out


def walk_rois(node, out=None):
    """Every ROI in the tree, as (id, node) pairs."""
    if out is None:
        out = []
    if isinstance(node, dict):
        if node.get("type") in ROI_TYPES and isinstance(node.get("id"), str) \
                and node["id"]:
            out.append((node["id"], node))
        else:
            for v in node.values():
                walk_rois(v, out)
    elif isinstance(node, list):
        for v in node:
            walk_rois(v, out)
    return out


# ------------------------------------------------------------- extraction

def extract_layout(geometry: dict, *, pdef_name: str = "",
                   pdef_version: str = "", algo_version: str = "",
                   registration: dict | None = None) -> dict:
    """Build the storable layout from a geometry blob the operator confirmed.

    Every ROI is captured, not only the hand-moved ones. The automatic
    proposal already varies by up to a millimetre between scans of one phantom
    (coarse-to-fine placement ends at a 0.25 mm search increment and the
    wedge boundary measurements also vary with image noise), so freezing the confirmed positions makes
    successive scans of a phantom directly comparable rather than merely
    re-detected. ``manually_adjusted`` is kept per ROI because it changes what
    ``compute`` does: a line-pair profile segment without it has its direction
    re-measured by FFT, which would throw away the operator's correction."""
    rois: dict[str, dict] = {}
    scalars: dict[str, dict] = {}
    for test, node in (geometry or {}).items():
        if not isinstance(node, dict) or node.get("_error"):
            continue
        if test == "geometry":
            # Rulers, corner marks and field edges describe the exposure and
            # the collimator, not the phantom's assembly.
            continue
        for roi_id, roi in walk_rois(node):
            entry = {"type": roi["type"]}
            for f in _MM_FIELDS.get(roi["type"], ()):
                if f in roi:
                    entry[f] = roi[f]
            if roi.get("manually_adjusted"):
                entry["manually_adjusted"] = True
            rois[roi_id] = entry
        keep = {k: node[k] for k in _SCALARS.get(test, ()) if k in node}
        if keep:
            scalars[test] = keep
    return {
        "version": LAYOUT_VERSION,
        "pdef_name": pdef_name,
        "pdef_version": pdef_version,
        "algo_version": algo_version,
        "rois": rois,
        "scalars": scalars,
        # For diagnosing a suspect replay: what the frame looked like when the
        # layout was confirmed. Note residual_rms_mm is NOT an orientation
        # check — the four rulers are identical, so it reads the same for all
        # eight candidate assignments.
        "registration": registration or {},
    }


# ------------------------------------------------------------- application

def _distance_mm(a: dict, b: dict) -> float | None:
    """Separation between the centres of two ROI descriptions, in mm."""
    def centre(d):
        if d.get("type") == "segment" and "p0_mm" in d and "p1_mm" in d:
            return [(d["p0_mm"][0] + d["p1_mm"][0]) / 2.0,
                    (d["p0_mm"][1] + d["p1_mm"][1]) / 2.0]
        return d.get("center_mm")
    ca, cb = centre(a), centre(b)
    if not ca or not cb:
        return None
    return math.hypot(ca[0] - cb[0], ca[1] - cb[1])


def layout_agrees(layout: dict, geometry: dict) -> dict:
    """Does this stored layout describe the same phantom, the same way up?

    Compares only the orientation-sensitive objects against the SAME scan's
    fresh proposal. A genuine assembly difference between two builds of the
    phantom measures 3-4 mm; a mis-registered scan measures a hundred."""
    offsets = []
    for roi_id in ORIENTATION_PROBES:
        stored = (layout.get("rois") or {}).get(roi_id)
        fresh = walk_find(geometry, roi_id)
        if not stored or not fresh:
            continue
        d = _distance_mm(stored, fresh)
        if d is not None:
            offsets.append((roi_id, d))
    if not offsets:
        # Nothing comparable — the proposal failed on every orientation-bearing
        # object. Applying a layout on top of that is a guess, so refuse.
        return {"ok": False, "checked": [], "median_mm": None,
                "reason": "none of the orientation-bearing patterns "
                          "(line pairs, low-contrast block, wedge) could be "
                          "detected on this scan, so the stored layout cannot "
                          "be checked against it"}
    ds = sorted(d for _, d in offsets)
    median = ds[len(ds) // 2] if len(ds) % 2 else (ds[len(ds) // 2 - 1]
                                                  + ds[len(ds) // 2]) / 2.0
    ok = median <= ORIENTATION_TOLERANCE_MM
    return {
        "ok": ok,
        "checked": [{"id": i, "offset_mm": round(d, 2)} for i, d in offsets],
        "median_mm": round(median, 2),
        "reason": "" if ok else (
            f"the stored layout sits {median:.0f} mm from where this scan's own "
            f"detection puts the same patterns (tolerance "
            f"{ORIENTATION_TOLERANCE_MM:g} mm). That is far more than any "
            f"phantom-to-phantom difference, so the scan is most likely "
            f"registered at a different orientation. Check Stage A, or use the "
            f"automatic layout for this scan."),
    }


def apply_layout(ctx, geometry: dict, layout: dict) -> tuple[dict, dict]:
    """Overlay a stored layout on a FRESH automatic proposal.

    The proposal must have run first: its per-pattern ``detected`` flags are the
    evidence the operator reads in Stage B, and they stay meaningful only if
    they came from this scan. This function then moves the ROIs to the stored
    millimetre positions and re-derives every pixel field from THIS scan's
    transform, which is the whole rotation-invariance mechanism."""
    applied, skipped = [], []
    for roi_id, entry in (layout.get("rois") or {}).items():
        node = walk_find(geometry, roi_id)
        if node is None or node.get("type") != entry.get("type"):
            # The phantom definition may have changed the object list since.
            skipped.append(roi_id)
            continue
        for f in _MM_FIELDS.get(entry["type"], ()):
            if f in entry:
                node[f] = entry[f]
        node.update(refresh_px_geometry(ctx, node))
        node["from_profile"] = True
        if entry.get("manually_adjusted"):
            # Without this the line-pair profile direction is re-measured by
            # FFT at compute time and the stored orientation is discarded.
            node["manually_adjusted"] = True
        applied.append(roi_id)

    for test, keep in (layout.get("scalars") or {}).items():
        node = (geometry or {}).get(test)
        if not isinstance(node, dict) or node.get("_error"):
            continue
        for k, v in keep.items():
            node[k] = v
        node["from_profile"] = True

    report = {
        "applied": applied,
        "skipped": skipped,
        "n_applied": len(applied),
        "n_skipped": len(skipped),
    }
    return geometry, report
