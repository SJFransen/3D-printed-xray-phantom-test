# Auto Analyze

Auto Analyze is the first tab. Its upload screen contains one control:
**Drop DICOM file here**. Drop a scan or activate the area with the mouse,
Enter or Space to choose a file; analysis starts immediately. The shared upload
controller uses the remembered site, phantom and operator from the current
browser session. Identification can be edited in Analyze; no new identity is
inferred from the file name.
The page switches to processing and then a simplified results screen with a
read-only phantom viewer and To Do panel. **New scan** returns to upload.

The app page serves scripts and styles with content fingerprints in their URLs.
HTML is not cached; scripts/styles revalidate with the server. This prevents
cached upload controllers from an older release leaving the shared form blank
after an update. Fingerprints update on page load without a frontend build.

The viewer fits the full scan into the available screen height. Its **W** (window
width), **C** (window center), **auto** and **fit** controls use Analyze's image
rendering and shared view calculations. Scroll or use **+ / −** to zoom; drag the
image to pan. Image and ROI outlines move together. These controls change only
the preview, never the measured pixels, ROI positions or stored results. Automatic
window reset preserves zoom/pan; fit restores the full scan. Fitted scans also
refit when the viewport changes size.

Feature results sit above the image, with PASS or Attention beside each label.
Window/centre controls sit below it. Green ROI outlines mean PASS; orange means
attention. Outlines stay 2.6 screen pixels thick when fitting or zooming.
Existing proposed ROIs remain visible in orange when detection or measurement
is incomplete, including nominal fallback proposals. Invalid coordinates cannot
be plotted, and no new ROI is invented when registration/proposal fails entirely.

To Do contains only the next scan deadline and, when needed, one short instruction:

- New orange image: “Repeat the phantom scan once and upload the new scan.”
- Identical previously recorded orange image: “Contact the MSF technician about
  X-ray field alignment.” The affected tests are named together in one sentence.

There are no feature-specific task summaries, troubleshooting steps or technical
tasks. Upload-date warnings remain explicit. The next deadline is the stored
server upload date plus one calendar month, clamped at month end; DICOM acquisition
dates are not used. Different file hashes are evaluated independently.

**Open in Analyze** opens the same saved History record and displays its existing
results without recomputing on entry. Site, phantom, operator and notes come from
that record. Going back through the wizard permits corrections; the results stage
then recomputes from the saved positions. Returning to Auto Analyze refreshes the
saved result. Signed-off records retain the protected workflow. No review or
validation is assigned automatically.

Both viewers use original detector-pixel coordinates, including when an odd-size
image is rounded while generating its smaller PNG preview. They share the same
line-pair ROI selection function and the exact saved geometry. Opening either
view changes no geometry, measurements, profile defaults or revisions.

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

The result version is **1.1.5** so older saved assessments can be distinguished.
Rectangle sampling now uses half-open boundaries with a floating-point tolerance;
an integral-size ROI no longer loses a row of pixels due to scale roundoff.
Existing records are not silently rewritten. Recompute an unsigned record through
Analyze or use the existing maintenance reanalysis command when an update is
needed. Administrator sign-off remains protected.

## Integration points

| File | Responsibility |
| --- | --- |
| `phantom_qa/auto_analyse.py` | Optional saved-result presentation and task generation |
| `phantom_qa/webapp/auto_analyse.py` | Optional upload route and persistence coordination |
| `phantom_qa/analysis/assessment.py` | Shared result checks and proposed overlay extraction |
| `phantom_qa/pipeline.py` | Runs the shared checks after existing measurements |
| `phantom_qa/__init__.py` | Result version 1.1.5 |
| `phantom_qa/webapp/main.py` | Shared registration/proposal/compute/default orchestration, revision/report APIs and persistence |
| `phantom_qa/store.py` | Atomic duplicate safeguard, computation commit and durable revision storage |
| `phantom_qa/report.py` | Shared report renderer; incomplete sections preserve the rest of the report |
| `phantom_qa/webapp/static/index.html` | Auto Analyze tab and separate upload/results markup |
| `phantom_qa/webapp/static/auto-analyse.js` | Optional upload, read-only overlays, concise tasks and navigation |
| `phantom_qa/webapp/static/auto-analyse.css` | Scoped styling |
| `phantom_qa/webapp/static/viewer-tools.js` | Shared fit, zoom, pan and W/C mapping; read-only image-view controller |
| `phantom_qa/webapp/static/upload-form.js` | Shared file selection, labels, suggestions, normalization, loading and submission |
| `phantom_qa/webapp/static/app.js` | Serial position saves, result invalidation, Analyze handoff and saved-revision links |
| `tests/test_auto_analyse.py` | Workflow, duplicate, task, ROI and cross-flow parity tests |
| `tests/test_auto_analyse_browser.py` | Real-browser upload/results, view controls, errors, orange ROIs and Analyze handoff |
| `tests/test_viewer_tools.js` | Preview request races, scan switching, window reset and failed-preview retry |
| `tests/test_authorization.py` | Authenticated-user classification for the optional route and assets |
| `tests/test_analysis_revisions.py` | Real alignment, saved-layout parity, concurrent edits and archived reports |
| `tests/test_revision_store.py` | Independent shared-storage migration, retention, sign-off and deletion checks |
| `tests/test_analysis_write_queue.js` | Pending field-edge saves must complete before preview/compute |
| `README.md` | Guide link |

The existing `auto-analyse` filenames, DOM IDs, route and workflow metadata value
are retained for compatibility. The displayed name is **Auto Analyze**.

The optional route calls the shared `_run_default_analysis()` helper, which uses
Analyze's `_do_register()`, `propose()` and `_compute_analysis()` functions. These
use the existing pipeline and acceptance criteria. Auto Analyze now follows the
normal saved-phantom-layout default, including the existing agreement gate; the
original detection remains geometry state zero. Automatic execution never saves
a phantom profile on an operator's behalf. The SID default is 1000 mm.

Both pages use `scan-upload-template` and `UploadForm.mount()` (compact automatic
submission in Auto Analyze), along with the
same backend upload validation and source storage. Blank labels on unsigned
duplicates can be filled from explicitly supplied upload labels, with an audit
event. Existing nonempty labels and signed records are preserved. The current
record's identification appears in Auto results and Analyze's identity bar.

Field-edge saves, confirmations and computations are serialized per analysis in
the browser. Geometry edits invalidate stored and displayed results/previews.
Preview requests use the submitted SID without changing the record. A computation
cannot commit if its geometry, registration, SID or sign-off changed while it
ran; it returns 409 for a fresh retry instead of saving stale measurements.
Alignment measures displacement perpendicular to the phantom edge; moving a
point only along that edge does not change its alignment offset.
Authentication, CSRF checks, upload limits and administrator permissions remain.

Automatic results carry `_meta.workflow = "auto_analyse"` and
`_meta.manually_reviewed = false`. The current assessment is always unreviewed;
`history_validation_status` separately describes any ruling on a reused record.
Automatic finalization does not confirm wizard stages, sign off, save a phantom
profile or choose a baseline. The existing database gains one shared revision
table automatically; there is no separate Auto Analyze database.

## Audit and corrected reports

`analysis_revisions` retains compressed full record snapshots independently of
the bounded undo stack. Audit events include the actor. The source file, hash,
upload date, registration, geometry, parameters, version metadata and result
diagnostics remain traceable after editing, re-proposing or trimming undo states.
Computations archive their inputs/results and commit with a geometry/ruling check.
Finalization archives a self-contained HTML report against its saved revision.
An incomplete report section is explicit and does not prevent other sections
from being saved. A report-generation failure is returned rather than silently
claiming the report was archived.

After corrections, Analyze recomputes the same record and Finalize saves the
updated report. The original automatic report remains unchanged. **Save & export →
Saved revisions** links to saved reports and full revision JSON. History, exports
and trends use the latest current results and count the scan once.

Authenticated read endpoints:

- `GET /api/analyses/{id}/revisions`: revision metadata.
- `GET /api/analyses/{id}/revisions/{revision}`: saved record and report.
- `GET /api/analyses/{id}/report.html?revision={revision}`: a revision's report.
- `GET /api/auto-analyse/{id}`: presentation of the current saved result.

Existing databases acquire the new table on startup. Older records are not
recomputed or assigned fictional prior revisions. Their current state is retained
before subsequent edits; changes made before this update cannot be reconstructed.
Back up the SQLite database with the existing backup command before upgrading.
Revision storage grows with edits and archived reports; deleting a scan through
the authorized deletion workflow also deletes its revisions.

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

An identical re-upload reuses the existing record ID, upload date, measurements,
manual corrections, baseline and sign-off. It does not run a new unsaved automatic
assessment. A genuinely new image is processed independently. An identical image
is never treated as proof of a repeat exposure. Re-encoding the same exposure can
change its file hash; no repeat relationships are inferred from matching labels.

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
   includes from `static/index.html`. Keep `scan-upload-template`. Restore
   Analyze's active tab and visible page.
3. Delete `phantom_qa/auto_analyse.py`, `webapp/auto_analyse.py`, both optional
   static assets, the two Auto test files and `tests/test_analysis_revisions.py`,
   which exercises the Auto-to-Analyze integration. Remove the four feature entries
   from the authorization inventory and this guide's README link.
4. Keep `analysis/assessment.py`, its pipeline/preview integration and shared
   error rendering, plus `static/viewer-tools.js`, `static/upload-form.js` and their
   script includes. Keep shared revision/report storage and APIs, serial edits,
   and `tests/test_revision_store.py`: Analyze uses them independently. Shared
   upload helpers and generic navigation events may also remain.
5. Run the regression suite. No migration or deletion is required; saved automatic
   analyses remain ordinary History records with images, geometry and audit trails.

## Validation

Run `python -m pytest -q`, plus:

```text
node tests/test_window_level.js
node tests/test_fieldedge_drag.js
node tests/test_fieldedge_patterns.js
node tests/test_viewer_tools.js
node tests/test_analysis_write_queue.js
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

Validation on 21 October 2026 for shared uploads and audited corrections:

- Full suite: **830 passed, 35 skipped, 1 pre-existing failure**; 36 subtests passed.
- The remaining failure is the same synthetic wedge pixel-count mismatch above
  (4830 versus 4900). Wedge measurement code and QA thresholds remain unchanged.
- New coverage includes real field-edge measurements on all four sides under
  every rotation/mirror, pending-save navigation, shared layout defaults, site
  transfer, database reopening, retained reports, revision migration/deletion,
  incomplete reports and rejection of stale computation commits.
- Final browser/frontend run: **26 passed**, including all five Chrome workflows.
  All five JavaScript suites and syntax checks passed. Desktop and mobile renders
  were inspected; ROI outlines retain a readable screen thickness while zooming.

Upload cache regression fix on 21 October 2026:

- **313 relevant checks passed**: 304 authorization, frontend, automatic workflow
  and correction checks, plus three asset-delivery checks and six Chrome workflows.
- The new browser test reproduces blank forms with cached older controllers,
  then verifies both file choosers and an Analyze upload after the fix.
- The restarted local app was checked in Chrome on desktop and mobile. Both
  upload forms render without browser errors; no saved scan was changed.

Version 1.1.5 audit fixes and compact Auto Analyze validation:

- Full regression run: **882 passed, 36 skipped**, with 36 passing subtests.
  The previously failing wedge pixel-count test now passes. The skips require
  absent reference fixtures, except the Git-tracking check in the isolated copy.
- Final affected checks: **131 passed**, including three additional tests for
  invalid SID values in legacy records, concurrency and all browser journeys.
  All five JavaScript suites passed.
- A copied real DICOM produced identical complete geometry and all five result
  groups through fresh Auto Analyze and Analyze runs. A copied saved DICOM also
  retained all five line-pair ROIs through the viewer round trip.
- Desktop, 650-pixel and 360-pixel layouts were inspected. Feature results are
  above the image, window controls below it, and no extra upload options appear.
  Validation used disposable databases; existing saved analyses were unchanged.
