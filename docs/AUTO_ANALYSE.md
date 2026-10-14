# Auto Analyze

Auto Analyze is the first tab. Its upload screen follows Analyze: choose or drop
a scan, enter optional site/phantom/operator/notes, then press **Upload & analyze**.
The page switches to processing and then a simplified results screen with a
read-only phantom viewer and To Do panel. **New scan** returns to upload.

The viewer fits the full scan into the available screen height. Its **W** (window
width), **C** (window center), **auto** and **fit** controls use Analyze's image
rendering and shared view calculations. Scroll or use **+ / −** to zoom; drag the
image to pan. Image and ROI outlines move together. These controls change only
the preview, never the measured pixels, ROI positions or stored results. Automatic
window reset preserves zoom/pan; fit restores the full scan. Fitted scans also
refit when the viewport changes size.

Green borders mean PASS; orange borders mean attention. Feature labels have
colored borders without explanations, measurement summaries or technical detail.
Existing proposed ROIs remain visible in orange when detection or measurement
is incomplete, including nominal fallback proposals. Invalid coordinates cannot
be plotted, and no new ROI is invented when registration/proposal fails entirely.

To Do contains only the next scan deadline and, when needed, one short instruction:

- New orange image: “Repeat the phantom scan once and upload the new scan.”
- Identical previously recorded orange image: “Contact the MSF technician.”

There are no feature-specific task summaries, troubleshooting steps or technical
tasks. Upload-date warnings remain explicit. The next deadline is the stored
server upload date plus one calendar month, clamped at month end; DICOM acquisition
dates are not used. Different file hashes are evaluated independently.

**Open in Analyze** opens the same History record. For an unsigned registered
record it runs the existing Analyze results stage, where detailed errors can be
reviewed. Signed-off records retain the existing protected workflow. No review
or validation is assigned automatically.

## Shared results and the line-pair correction

The line-pair detector stores its acceptance flag at `positioning.strip.accepted`.
The original Auto implementation incorrectly required `positioning.accepted`,
marking successful line-pair measurements as rejected because that field is absent.
The corrected lookup respects the nested strip pose and local centroid metadata.

Result completeness and reliability checks now live in the independent shared
module `phantom_qa/analysis/assessment.py`. Both Analyze and Auto Analyze obtain
their final verdicts from `pipeline.compute_all()`, which calls this module.
Analyze's dimension preview uses the same checks. Auto Analyze consumes the
pipeline's assessment directly without applying a second acceptance decision.
The same image, geometry, SID and algorithm version therefore produce the same
verdicts and errors. Manual position/SID changes can intentionally change results.

No numerical QA threshold or image-processing algorithm was changed. Existing
subtest verdicts, required feature identities, finite required measurements and
detector/reliability flags are reused. Manually corrected ROIs in Analyze are
recognized. Existing `n/a` and computation errors retain their status and diagnostic.
An incomplete apparent pass becomes an analysis error in both flows. Raw quality
verdicts remain in `measurement_status`; shared `_assessment`, `analysis_issues`
and `reasons` retain technical diagnostics for Analyze, reports and export.

The result version is **1.1.4** so older saved assessments can be distinguished.
Existing records are not silently rewritten. Recompute an unsigned record through
Analyze or use the existing maintenance reanalysis command when an update is
needed. Administrator sign-off remains protected.

## Integration points

| File | Responsibility |
| --- | --- |
| `phantom_qa/auto_analyse.py` | Optional automatic orchestration and task generation |
| `phantom_qa/webapp/auto_analyse.py` | Optional upload route and persistence coordination |
| `phantom_qa/analysis/assessment.py` | Shared result checks and proposed overlay extraction |
| `phantom_qa/pipeline.py` | Runs the shared checks after existing measurements |
| `phantom_qa/__init__.py` | Result version 1.1.4 |
| `phantom_qa/webapp/main.py` | Router mount, shared upload helpers and preview integration |
| `phantom_qa/store.py` | Atomic, opt-in `new_analysis_once()` duplicate safeguard |
| `phantom_qa/webapp/static/index.html` | Auto Analyze tab and separate upload/results markup |
| `phantom_qa/webapp/static/auto-analyse.js` | Optional upload, read-only overlays, concise tasks and navigation |
| `phantom_qa/webapp/static/auto-analyse.css` | Scoped styling |
| `phantom_qa/webapp/static/viewer-tools.js` | Shared fit, zoom, pan and W/C mapping; read-only image-view controller |
| `phantom_qa/webapp/static/app.js` | Shared view calculations, generic tab/open-analysis events and error detail display |
| `tests/test_auto_analyse.py` | Workflow, duplicate, task, ROI and cross-flow parity tests |
| `tests/test_auto_analyse_browser.py` | Real-browser upload/results, view controls, errors, orange ROIs and Analyze handoff |
| `tests/test_viewer_tools.js` | Preview request races, scan switching, window reset and failed-preview retry |
| `tests/test_authorization.py` | Authenticated-user classification for the optional route and assets |
| `README.md` | Guide link |

The existing `auto-analyse` filenames, DOM IDs, route and workflow metadata value
are retained for compatibility. The displayed name is **Auto Analyze**.

The pipeline uses `run_stage_a()`, `build_ctx()`, `propose_all()`, `compute_all()`
and `overall_status()`. Automatic proposals are stored as geometry state zero;
stored phantom profiles and manual corrections are never applied by Auto Analyze.
Shared upload helpers preserve bytes, hashes, image metadata, labels, algorithm
and phantom-definition versions and audit logging. `Store.update()` and the
existing `finalize()` operation persist ordinary History records. Authentication,
CSRF checks, upload limits and administrator permissions are unchanged.

Automatic results carry `_meta.workflow = "auto_analyse"` and
`_meta.manually_reviewed = false`. The current assessment is always unreviewed;
`history_validation_status` separately describes any ruling on a reused record.
Automatic finalization does not confirm wizard stages, sign off, save a phantom
profile or choose a baseline. No separate database or schema migration is needed.

Tasks retain their extensible data model: `id`, `type`, `related_feature`, `title`,
`description`, `priority`, `due_date`, `status`, `steps` and `image_references`.
Generation and rendering are separate; initial status is `open`. Current tasks
do not populate feature-specific steps/images or add a completion workflow.

## Duplicate handling and remaining limits

`new_analysis_once()` checks Analyze's SHA-256 fingerprint under a SQLite write
transaction before inserting, including concurrent uploads and repeated archive
members. Mixed archives reuse existing records and save only new members. Analyze's
409 response and explicit duplicate override retain their behaviour; Auto Analyze
offers no override. ZIP members' bytes are stored instead of container bytes.

An identical re-upload reuses the existing record ID and original upload date.
Its fresh automatic assessment is shown in memory without replacing existing
measurements, manual corrections, labels, baseline or sign-off. History continues
to show that record's saved assessment, which can differ if it was manually edited
or computed with an older version. An identical image is never treated as proof
of a repeat exposure. Re-encoding the same exposure can change its file hash;
no repeat relationships are inferred from matching labels.

Auto Analyze uses Analyze's 1000 mm SID default. For a different acquisition SID,
use Analyze to enter the actual value. Detector reliability and the existing
phantom definition remain the limits of automatic measurements.

Processing is one request with an indeterminate indicator; CPU work runs in a
worker thread. There is no durable job queue or progress stream. A worker crash
can leave a saved draft. Upload errors tell the operator to check History before
retrying. Distinct archive images have a result selector. A missing stored source
can prevent the preview from loading; the status borders and To Do remain available.

## Safe removal

1. Remove the optional `create_router` import and `app.include_router(...)` near
   the end of `webapp/main.py`; remove its `sys` import if unused.
2. Remove the Auto Analyze tab, `view-auto-analyse` markup and optional asset
   includes from `static/index.html`. Restore Analyze's active tab and visible page.
3. Delete `phantom_qa/auto_analyse.py`, `webapp/auto_analyse.py`, both optional
   static assets and the two Auto test files. Remove the three feature entries
   from the authorization inventory and this guide's README link.
4. Keep `analysis/assessment.py`, its pipeline/preview integration and shared
   error rendering, plus `static/viewer-tools.js` and its script include: Analyze
   uses them independently. Shared upload helpers,
   `new_analysis_once()` and generic navigation events may also remain.
5. Run the regression suite. No migration or deletion is required; saved automatic
   analyses remain ordinary History records with images, geometry and audit trails.

## Validation

Run `python -m pytest -q`, plus:

```text
node tests/test_window_level.js
node tests/test_fieldedge_drag.js
node tests/test_fieldedge_patterns.js
node tests/test_viewer_tools.js
```

Optional browser tests require Playwright, Uvicorn and local Chrome or Playwright
Chromium. They skip when unavailable. Reference DICOM tests retain their existing
skip when sample scans are absent.

Validation on 14 October 2026:

- Full suite before the viewer update: **794 passed, 35 skipped, 1 pre-existing
  failure**; 36 subtests passed.
- Viewer update: **288 focused checks passed across regression runs**, including
  all four real Chrome tests; 6 subtests passed. Coverage includes automatic
  workflow/parity, rendering, frontend integrity and authorization. The browser
  checks cover screen fit, W/C, auto reset, zoom, dragging, resizing, overlay
  alignment and absence of measurement writes from display controls.
- All four JavaScript suites, frontend syntax checks and
  `git diff --check` passed.
- The two saved cases exhibiting the original line-pair mismatch both return
  PASS under the corrected check. This verification did not rewrite their records.

The existing `tests/test_manual_adjustment.py::test_wedge_roi_pixel_counts_match`
failure was reproduced against the original commit with the same Python 3.12,
NumPy 2.2.6 and SciPy 1.13.1 environment: 4830 versus 4900 pixels in synthetic wedge
ROIs. No algorithm or threshold was changed to accommodate that unrelated failure.
