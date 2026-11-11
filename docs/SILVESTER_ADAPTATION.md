THIS (AI generated plan) PLAN SHOULD ONLY BE APPLIED WITH EXPLICIT APPROVAL

# Silvester adaptation on Stefan's implementation

Target: `merge_dev_stefan_silvester`, based on Stefan's `02e7996`.
Source of ideas and selected helpers: Silvester's `69db7d6`.

## Implemented within the authorized scope

New individual and comparison reports have Export to PDF controls using the
browser's print dialog. Reports print on A4 portrait / landscape respectively,
with identifying headers and page numbers in browsers supporting page margin
boxes. Existing archived revision reports are not rewritten.

Comparison reports add five pictures per registered scan: whole phantom,
line-pair strip, wedge, low contrast and uniformity. Every selected scan appears;
picture tables are grouped into three scan columns to fit landscape printing.
Native disclosure controls enlarge pictures without a server connection.
Windows are shared with the first drawable scan in the same acquisition
protocol; low contrast uses its own window and is labelled **unprocessed**.
Stefan has no processed block-view function, so Silvester's new processing and
close-up controls were not imported. All report pictures use lossless PNG.
These small resampled pictures support visual comparison, not fine-detail QA.

Picture sampling and disposable caches adapt `thumbnails.py` from Silvester.
No registration, ROI proposal or measurement runs when a report is requested.
Keys cover source hash, registration, complete geometry, sequence, algorithm,
definition and picture versions. Arrays are read without pickle. Atomic cache
writes, corrupt-file recovery and deletion cleanup keep caches disposable.
`data/thumbs/` need not be backed up; the next report regenerates it.

Comparison decoding does not add scans to the normal decoded-image cache.
Chart drawing stays sequential under the existing report lock; two report-local
workers compress chart PNGs while the calling thread draws the next chart.
Workers and their context are released even when generation fails.
Text responses are gzip-compressed; existing compressed picture/video/archive
paths bypass gzip. API cache/security policies remain unchanged; only report
routes admit the exact PDF-control script hash, without permitting arbitrary
inline scripts.

The local Windows launcher requests the process-specific execution-throttling
opt-out adapted from Silvester's `run_app._run_at_full_speed`. Declining or
unsupported APIs do not stop startup; production configuration is unchanged.

Profile saves also retain internal `source_json` evidence: source analysis ID
and a fingerprint of the saved source geometry. This adds no profile selection
rule and does not change the profile API. Existing rows keep unknown evidence.

No analysis modules, positioning code, application static files or Auto Analyze
controller were changed. Database query optimizations tied to Silvester-only
status notes were not applicable to Stefan's existing listing.

## Concrete changes awaiting user approval

These are deliberately **not implemented**: the user's preservation rule
requires approval before changes to positioning, analysis, results presentation,
application UI or Auto Analyze/Analyze workflow. Each item can be approved alone.

1. **Profile-save policy and control.** Adapt Silvester's
   `webapp/main.py:_layout_save_default`, `confirm_stage` and
   `store.py:save_phantom_profile`. Keep `save_profile: bool | None` on the
   existing confirmation route. False writes nothing; true explicitly replaces;
   omitted saves only if no profile exists or this analysis supplied it. Enforce
   the omitted-value rule inside the transaction. Add a Stage C checkbox using
   that default, with the existing profile's source/date shown. This changes
   application UI and the profile used to position future scans.
2. **Profile recovery on source deletion.** Adapt `Store._unwind_profile`:
   retain one previous profile and its source evidence on replacement; if the
   current profile's source is deleted, restore that predecessor only if its
   source still exists, otherwise remove the current profile. Do not unwind
   when another analysis is deleted. This changes future ROI placement.
3. **Production image-usability gate.** Adapt `quality.assess` as an independent
   exposure/registration assessment; validate thresholds against Stefan's
   reference scans before enabling any gate. Preserve raw measurements and
   allow detailed correction. Failed usability blocks shared-profile/reference
   saves, with an explicit reason. Administrator override requires the existing
   administrator credential and a recorded reason. Unknown historical evidence
   is displayed as unassessed. This affects UI and allowed actions. If reference
   scans cannot establish safe limits, leave the gate disabled and report why.
4. **Missing-measurement semantics.** Adapt `pipeline.overall_status` and
   `verdict_notes` together with Stefan's shared assessment: absent field edges
   can be not applicable, missing required measurements remain attention, and
   the result explains skipped tests. Change both Auto/Analyze consistently;
   leave historical saved results untouched. This changes verdicts, presentation
   and guidance, so needs approval even if measurement formulas stay unchanged.
5. **Review provenance and reference eligibility.** Preserve Stefan's existing
   workflow metadata; opening Analyze alone never declares review. Track explicit
   Stage C confirmation of the current geometry and subsequent computation as
   reviewed, invalidate that evidence after geometry edits, and expose the state
   consistently in results, History and reports. Require reviewed suitability for
   selecting a new baseline. This changes presentation and reference workflow.

No insert-orientation detection, ROI re-positioning, upload compression, Field
analysis mode or interactive image-rendering replacement is proposed.

## Validation

Focused regressions are in `tests/test_report_adaptation.py`; existing Auto
Analyze, revisions, profile, security and navigation suites remain authoritative.
Performance measurements use identical synthetic records against `02e7996`;
they are not clinical/reference-scan performance claims. Cache cold/warm timing
and rendered report artifacts are kept outside the repository under
`../.azg/report-preview/` during verification.

Validation completed during implementation:

- Full Python suite: **907 passed, 65 skipped**, with 36 subtests passed.
- Final focused report/cache/compression checks: **12 passed**, including exact
  chart-pixel parity with Stefan's rendering and unchanged saved records.
- All five existing JavaScript suites passed using Windows Node 24. The WSL
  Node version is too old for their `node:assert/strict` imports.
- Chrome checks at 1280px and 390px: no broken images or page overflow;
  enlargement and PDF controls worked. PDF page raster inspection caught and
  corrected excessive chart height and CSS Unicode escaping. Comparison output
  is A4 landscape; individual output is A4 portrait.
- Python Playwright is unavailable in this environment; existing browser tests
  were skipped by their normal availability checks. Report interactions were
  checked directly through Chrome's debugging protocol instead.
- Synthetic chart-only comparison timings (single runs under concurrent test
  load): 2 scans **17.37 → 16.54 s**, 12 scans **38.93 → 38.61 s**. These small
  differences are not evidence of a robust end-to-end speedup. Picture-value
  caching reduced **68.2 → 5.5 ms** from cold generation to warm retrieval.
- No commits, branch merges or remote pushes were performed.
