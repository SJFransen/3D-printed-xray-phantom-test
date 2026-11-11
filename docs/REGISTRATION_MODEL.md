# Model-based registration and individual insert flips (1.3.0)

The bright surrounding detector region in the local regression scan
`Test Bucky 1 LZR QC Leeds/DICOM/000002` used to become the entire phantom
mask after hole filling. Registration mapped a 299.5 mm square onto the
rectangular detector, corrupting downstream ROI positions and scale.

Automatic registration now starts with centred square hypotheses over multiple
sizes and angles. DICOM spacing supplies additional scale hypotheses, never a
mandatory physical scale. Enclosed bright and dark components propose further
locations, including off-centre placements. Border-connected components are not
filled and accepted as the phantom. Joint four-side evidence refines promising
square candidates; full-resolution edge profiles refine their corners.

The eight rotation/reflection assignments are scored using uniformity outlines,
the low-contrast block's bright perimeter (with a weaker edge fallback), wedge
frame and line-pair corridor. Per-feature responses use local bounded offsets
and bounded intensity contrasts. QA CNR/SNR, ruler pitch, wedge monotonicity and
pass/fail outcomes are not inputs to global pose selection. Automatic transforms
use uniform scale, rotation, translation and optional reflection, preventing
rectangular detector bounds from being disguised by arbitrary affine stretch.
Insufficient structural support or an ambiguous orientation causes a controlled
registration failure. Existing manual corners retain affine support and stored
registrations are read unchanged. The transform format and APIs remain compatible.

All initial feature ROIs share that accepted transform. Existing bounded local
group refinements remain; disc grids and their companion ROIs remain rigid.
Field edges remain properties of the exposure, not of the phantom model.

## Individual features turned over

- Whole-phantom mirroring is handled in registration, independently of inserts.
- Low contrast evaluates four local reflection hypotheses about its long/short
  axes. Disc/background contrast samples supply design-order rank evidence;
  at least four visible discs, correlation >= 0.8 and a >= 0.25 lead over the
  next hypothesis are required. The chosen reflection transforms the complete
  disc group, including object/background/outline ROIs, without changing IDs or
  metric formulas. This identifies assembly orientation; it is not a new QA
  acceptance threshold. These conservative evidence limits are tested on
  synthetic insert flips and the local scan, not a complete multi-build dataset.
- Weak/ambiguous insert evidence retains positional labels and reports
  `undetermined` with a warning; absence of evidence never proves a flip.
- The existing low-contrast correction endpoint accepts optional `flip_x` and
  `flip_y`. Analyze adds two small controls to explicitly reverse either insert
  axis. Explicit choices have source `user`; corrections retain them, invalidate
  previous measurements and remain undoable through existing geometry history.
- Wedge placement additionally tests the reversed axis, choosing it only from
  stronger step-boundary evidence. It does not choose the direction that makes
  its monotonicity test pass. Left/right reflection of its symmetric geometry
  has the same measurement positions.
- The existing ordered line-pair matcher already considers reversed group
  order. Uniformity-square reflection is geometrically indistinguishable and
  requires no relabelling. An unobservable face flip is not invented.

Confirmed profiles retain low-contrast orientation alongside the exact saved
ROI positions. Legacy profiles keep their saved identities and do not inherit
a fresh scan's inferred reflection. No historical results or reports are
automatically recomputed; deliberate reanalysis uses algorithm version 1.3.0.

## Verification and limits

`tests/test_registration_model.py` covers bright surrounds/dark interiors,
blank-image rejection, the local scan under quarter-turns and face-down
placements, all four insert reflection states, a structurally reversed wedge
and undoable explicit correction. The DICOM is referenced locally and is not
committed. Existing registration, positioning, profile and Auto Analyze tests
remain regression checks. Keep manual correction available: clipped boundaries,
different builds and weak insert evidence can still require operator review.

On the reported scan, initial investigation found ruler landmark disagreement
around 1.95 mm; the new model reduced it to approximately 0.52 mm, with the
correctly located square and a non-mirrored pose. Line pairs, low contrast and
wedge then passed their existing tests. Geometry and uniformity still reported
attention under existing measurement/assessment criteria; the registration fix
does not turn those independent outcomes into a blanket pass.

Implementation validation: full Python run **921 passed, 65 skipped** (36
subtests passed); focused positioning/profile/Auto Analyze run **138 passed,
2 skipped**; final model, reflection, saved-layout and bright-border run
**56 passed, 2 skipped**. All five existing JavaScript suites passed. The
corrected overlay was inspected on the supplied DICOM. Optional browser tests
retain their environment-dependent skips. A temporary verification image is
stored outside the repository at `../.azg/registration-1.3.0.png`.
