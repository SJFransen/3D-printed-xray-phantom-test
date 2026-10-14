# Algorithms and validation

How each reported number is computed. The algorithm version is stamped into
every stored result as `phantom_qa.ALGO_VERSION`.

**Conventions.** Image coordinates are (x, y) = (column, row) in pixels; phantom
coordinates are millimetres with the origin at the phantom-face centre, +x right
and +y up in the guide-figure orientation. After normalisation, a higher pixel
value means more attenuation, so the phantom is bright and direct exposure dark.

---

## Template positioning — `analysis/positioning.py` (algorithm 1.1)

After phantom registration, the complete feature layout is projected from the
definition into image coordinates. Each feature receives a starting pose and a
bounded search mask. The local models preserve dimensions and spacing; only
translation and rotation are fitted. Radiation-field edges remain independent
of the phantom layout, and ruler pitch/dimensions retain their existing
measurement path and scale.

A coarse grid searches each permitted pose range. Local iterations then use
translation steps of 2, 1, 0.5 and 0.25 mm and rotation steps of 2, 1, 0.5 and
0.25 degrees, with at most 12 iterations per level. Each level stops when no
neighbour improves the score. Outline matching uses progressively less
smoothing (1, 0.7, 0.45 and 0.25 mm) on a rectified local image. These are search
increments, not a claim of positioning accuracy.

Scores balance noise-normalised responses across structural outline sections.
No QA ROI SD, CNR, dimension deviation or pitch deviation is maximised. The
line-pair strip and low-contrast grid are aligned as groups before their smaller
local corrections. Weak, clipped and search-boundary outline fits retain the
template and report `detected: false`; diagnostics record scores, candidate
poses, iteration counts and the fallback reason. Scores without image evidence
are stored as JSON null rather than non-finite numbers.

Search radii and angle limits live in the feature sections of the phantom JSON:
uniformity 8 mm / 8 degrees, low contrast 15 mm / 25 degrees, line-pair strip
28 mm / 15 degrees, and wedge 12 mm / 8 degrees. The wedge's approximate
`frame_width_mm` (20 mm) describes structural support, not a reported width.
Confirmed layouts and manual edits continue to override proposals; computation
does not rerun this pose optimiser.

Synthetic ground-truth checks cover displaced/rotated features, a mirrored
registered frame, weak/absent circles, missing line groups, bounded/clipped
searches, manual edits, saved layouts and preservation of a real pitch error.
Run them with `python -m unittest discover -s tests -p test_positioning.py -v`
or the normal pytest suite.

## 1. Ingest and normalisation — `ingest.py`

- DICOM pixel data is decoded losslessly (pylibjpeg for JPEG-Lossless), the
  Modality LUT is applied (`RescaleSlope`, `RescaleIntercept`), and MONOCHROME1
  is inverted so polarity is uniform. The full header and the SHA-256 of the
  source file are stored.
- Zip archives are walked for DICOM images; viewer executables, DICOMDIR and
  HTML are skipped.
- Plain images are converted to greyscale float and flagged
  `reduced_precision`.
- The **protocol signature** is detector model | kV | pixel spacing | processing
  family (the first tokens of `AcquisitionDeviceProcessingDescription`). It
  groups comparable analyses for baselines and trends.

## 2. Phantom detection and registration — `registration.py`

1. **Coarse detection.** The image is downsampled 8×, Otsu-thresholded, and the
   largest bright connected component taken as the phantom face. Burned-in text
   and foreign objects form smaller components and are ignored.
2. **Angle.** A rotating-calipers search (0.5° coarse, 0.02° fine) finds the
   rotation that minimises the component's bounding-box area.
3. **Sub-pixel edges.** For each side, 25 profiles are sampled perpendicular to
   the coarse edge. The edge point is the extremum of the smoothed profile
   derivative with parabolic refinement. A total-least-squares line is fitted per
   side with two rounds of MAD outlier rejection, which removes edge tabs, screws
   and touching objects. Corners are the line intersections.
4. **Orientation.** Eight candidate corner assignments (four rotations ×
   mirror) are fitted as affine transforms and scored on image content: wedge
   means must decrease from step 1 to 7, the line-pair strip must show high
   local standard deviation, and the low-contrast block must be bright and
   smooth. All candidate scores are stored.
5. **Verification landmarks.** The central long ruler line of each side is
   located along the side normal near its predicted position. The RMS prediction
   error in millimetres is reported as `residual_rms_mm`. These landmarks verify
   the fit; they do not drive it.
6. **Manual fallback.** Four user-clicked corners replace steps 1–3. Orientation
   scoring still resolves the assignment, so click order does not matter.

The transform is a full six-degree-of-freedom affine (mm → px). `mirrored` is
derived from the determinant sign, accounting for the y-down image frame;
rotation from the image of the +x axis.

## 3. Scale chain — `analysis/geometry.py`

`side_mm = 299.5` in the phantom definition is chosen so the measured tape pitch
reads 5.000 mm: the transform scale is anchored to the printed pitch, not to
DICOM metadata. Per scan:

- `pitch_measured_mm` — mean regression slope of the six ruler-line positions,
  over the sides that pass the quality gate below.
- `k = 5.0 / pitch_measured` — a residual per-scan correction. All absolute
  dimensions are reported as (transform mm × k).
- A cross-check table compares the pitch-anchored `absolute_mm_per_px` with the
  DICOM `ImagerPixelSpacing` (detector plane) and `PixelSpacing`.

**Ruler quality gate.** Because `k` multiplies every reported length, one ruler
that was not actually found corrupts the whole report. A side is admitted to the
mean only if its mark positions are evenly spaced — linearity residual RMS
≤ `ruler_linearity_rms_max_mm` (0.5 mm; the reference scans sit below 0.25 mm).
Rejected sides are listed in `scale.rulers_rejected` and named in the dimension
reasons. Two admitted sides are required for `scale.reliable`; below that the
scale cannot be cross-checked and the results say so rather than presenting a
number that looks measured. If every side is rejected the mean falls back to all
detected sides, flagged unreliable — a bad scale that announces itself is more
useful than no result.

> Measured on a scan from a different X-ray unit, three of four rulers were
> mis-detected (residual RMS 1.2–2.0 mm against 0.02 mm for the good side) and
> outvoted the one good ruler, reporting the 300 mm phantom as 232.7 mm. With
> the gate the same scan reports 293.1 mm and flags the scale as resting on a
> single ruler.

On the reference scans the phantom face sits at magnification ≈ 1.042 relative
to the detector plane (0.1419 against 0.148 mm/px), because the phantom face
stands off the imaging plane. This is why the tape pitch, not the metadata, sets
the scale.

## 4. Rulers, dimensions and field alignment — `analysis/geometry.py`

**Ruler probe.** A profile is taken along the side's inward normal through the
ruler centre, averaged over ±3.5 mm along the edge to dilute the pointer tick.
The six line positions are the six strongest peaks, located sub-pixel by
intensity centroid. Offsets are referenced to the locally detected face edge —
the strongest rising step within −2…+6 mm — which decouples them from
corner-fit residuals.

**Pitch and linearity.** Linear regression of position against index per ruler
gives the pitch (nominal 5.0 mm), the per-line residuals, and their RMS
(`linearity_rms_mm`). The RMS is also the admission test for the scale chain
above; `reliable` on each ruler row records the outcome.

**Dimensions.** Corner-to-corner distances in calibrated millimetres for four
sides and both diagonals, against the assumed nominal side of 300.0 mm; plus
central-line separations (nominal 260.0 mm = side − 2 × 20 mm).

**Field alignment.** A probe runs outward from each side, from 8 mm past the
face edge to 12 mm short of the image border, averaged over ±10 mm along the
edge. A collimation border is a genuine step — direct exposure on one side, a
blocked beam on the other — whereas scatter gradients and the phantom's carrier
frame are not. The detector fits both a two-plateau change-point model and a
single linear ramp, and accepts the step only if **all** of:

- the probe is at least 25 mm long, with at least 8 mm of plateau on both sides
  of the split;
- `SSE_step ≤ 0.35 × SSE_linear`, so a ramp does not explain it as well;
- at least 70 % of the plateau difference occurs within a few millimetres of the
  split;
- the step height exceeds max(8 × local noise, 4 % of the image dynamic range).

Otherwise the side is reported not detected, with a reason, and manual placement
is requested. Deviation is the distance from the field edge to that side's
central long line, in millimetres and as a percentage of SID (tolerance ±2 %).

The probe deliberately stops short of the image border, which is itself a large
sharp intensity change that would otherwise be reported as a collimation edge.

> On all six reference scans every side is correctly reported as not measurable:
> the phantom nearly fills the detector, leaving only 30–61 mm of image outside
> its edge, and the radiation field extends past the image. To use this test,
> collimate visibly inside the detector.

**Accuracy note.** Mark-to-edge distances carry a processing-dependent
uncertainty of roughly ±0.5 mm, because the edge feature itself shifts with edge
enhancement. Pitch and mark-to-mark metrics do not.

## 5. Line patterns — `analysis/linepairs.py`

Block positions are **measured in every scan**; the phantom definition supplies
nominal seeds only.

**Periodicity map.** A 10 mm FFT window at 2 mm stride measures the
peak-to-median spectral ratio in the 1.0–2.4 lp/mm band. The registered template
defines the corridor. Ratios above 12 provide structural support; directions
inconsistent with the strip's permitted orientation are suppressed. The row
axis runs along decreasing phantom y, so its Fourier direction is sign-flipped
when expressed in phantom coordinates.

**Strip alignment.** The complete set of expected block centres is translated
and rotated together over this map using the shared coarse-to-fine optimiser.
At least three supported centres are required for the default five-group strip.
This avoids merging adjacent groups into one connected component through their
printed frame. Each aligned template centre then seeds a distinct 5 mm local
refinement region.

**Local centring.** A native-image 2-D FFT measures each block's frequency and
modulation direction. Directional demodulation at that measured carrier
frequency produces a narrow-band energy envelope, suppressing the broadband
frame. Its centre of mass is refined with progressively smaller bounded moves
(2 → 1 → 0.5 → 0.25 mm), stopping below 0.05 mm movement, after 16 updates, or
before leaving the 5 mm local mask. The strip axis is finally fitted through
the measured block centres, independently of the printed-line direction.

**Identity assignment.** Ordered subsets are matched by measured frequency,
including missing groups and reversed frequency order. A match more than
0.35 lp/mm from its nominal identity is rejected. Nominal frequency identifies
the block; it does not fix the measured line pitch.

**SD metric.** Standard deviation inside the 12.6 × 12.6 mm ROI — the guide's
constancy metric. The ROI is placed at `roi_angle_offset_deg` (45°) to the strip
axis, so its corners meet the nub markers on the two frame lines and the strip's
centre line, as the guide specifies. The ROI orientation and the profile
direction are independent: the square follows the guide's placement, the profile
follows the printed lines.

**Manual correction.** The ROI is the handle. When it is moved, the profile is
rebuilt at the new centre and its direction re-measured there, so the reported
numbers always come from where the square is. A profile whose direction the user
set by hand is marked and no longer overridden by the FFT.

**Linearity.** The pattern's frequency and direction come from a 2-D FFT of a
10 mm patch, whose peak must exceed 8× the band median. The profile is sampled
along that direction in ~0.07 mm steps, averaged ±4 mm along the lines,
detrended, and line peaks detected. The run of peaks is selected against the
**measured median gap** (±18 %) rather than the nominal: block-edge artefacts
produce short spurious gaps that a nominal-only window admits, dragging the
fitted pitch down by several percent. A uniform grid `pos_i = p0 + i·pitch` is
then least-squares fitted with one MAD-based outlier pass.

Reported: measured pitch and lp/mm, deviation against nominal, per-line
residuals in micrometres and their RMS, and the raw profile for plotting.

> With algorithm 1.0, across all six reference scans the pitch deviations are stable to about 0.1 %:
> +0.8 %, −2.0 %, +1.4 %, −2.4 %, +1.9 % for 2.0/1.6/1.4/1.2/1.1 lp/mm. This is
> reproducible print geometry, not measurement noise.

## 6. Low contrast — `analysis/lowcontrast.py`

- The projected block outline is fitted for centre and angle using the shared
  structural matcher, preserving the fixed block dimensions.
- Circle rims are then fitted as one rigid grid, with at most 2.5 mm translation
  and 4 degrees rotation relative to the block pose. At least three circles must
  provide coherent rim evidence; invisible circles inherit the common pose.
  The grid correction uses edge alignment rather than maximising circle CNR.
- Per circle: an object ROI of Ø 7 mm and a concentric background annulus
  (Ø 12 → 16 mm), keeping every background tied to its own object.

`CNR = (μ_obj − μ_bg) / √(σ_obj² + σ_bg²)` — the guide's formula, signed;
circles are darker, so values are negative.

**Geometry** measured on the reference scans: circles at u = ±10, ±30 mm and
v = +9.4 / −10.2 mm in block coordinates; block 84.5 × 44.5 mm at −45.1°.

**Labelling.** Circles are L1…L8 in design order, a boustrophedon starting
top-right, confirmed by measurement: contrast rises monotonically along that
path, from about 0.33 % to 1.37 % at 77 kV. No nominal contrast values are
claimed. A sanity check warns if |CNR| does not broadly increase with level.

## 7. Uniformity — `analysis/uniformity.py`

Each square's projected outline is refined for translation and rotation. Three
line-centre probes per side then verify the outline and measure its dimensions;
opposing edges determine the final centre. The 30 × 30 mm ROI follows the fitted
angle. Neither outline dimensions nor the QA ROI size are optimisation variables.

`SNR = μ / σ` and `ΔSNRᵢ = (SNRᵢ − SNR_avg) / SNR_avg`; the test passes if
|ΔSNRᵢ| ≤ 20 %.

## 8. Wedge — `analysis/wedge.py`

- The wedge frame and expected step boundaries are first aligned as one rigid
  template. Dark frame minima at three heights refine its transverse centre;
  gradient peaks within ±3 mm locate individual boundaries along its rotated
  axis. Boundaries must have local gradient evidence and remain ordered.
  Unequal step heights are measured, not replaced with an equal-spacing grid.
  Failed verification retains the nominal boundaries and reports no detection.
- Step ROIs are a **fixed** `roi_w_mm` × `roi_h_mm` (10 × 10 mm) centred in
  every step. The printed steps are not equal heights, so sizing each ROI to its
  own step would average a different area per step and make the same step
  incomparable between phantoms. Each step reports its measured height and
  whether the fixed ROI fits inside it.
- Reported: mean and standard deviation per step S1…S7 (positional labels only),
  monotonicity, dynamic-range ratio (S1/S7), the axis profile, and a
  least-squares fit of mean against step index with R² and per-step residuals as
  a percentage of span.

**Pass criteria.**

| Outcome | Condition |
|---|---|
| **Fail** | Response is not monotonic, or any step is saturated |
| **Warn** | R² below `wedge_r2_min` (default 0.85) |
| **Pass** | Otherwise |

R² is not the pass criterion because the response is reproducibly S-shaped:
across all six reference scans R² is 0.918–0.953 with a near-identical residual
pattern (−13, −1, +9, +10, +7, +2, −15 % of span) and step-to-step ratios from
1.03 to 2.0. The printed steps are not equal increments of attenuation. A
log-linear fit is worse (R² ≈ 0.77–0.81), so this is not a missing exponential.
Requiring R² > 0.95 on a linear fit would fail a sound detector because of the
phantom's own geometry. The per-step means are what get trended against the
baseline.

**Saturation** is tested against the detector's full scale — 2^`BitsStored` − 1,
or the next power of two above the image maximum when the tag is absent — not
against the image's own extremes, since being the brightest object in one image
is not saturation. A near-zero ROI standard deviation is the second signature of
clipping.

## 9. Statuses and constancy

Fixed tolerances follow the guide: ±2 % of SID for field alignment, |ΔSNR| < 20 %
for uniformity. Everything else is a **constancy test** against the baseline of
the same protocol signature, with a default band of ±20 % shown in trends.

Absolute values in processed radiographs are not proportional to dose. Comparing
across protocol signatures is possible but the interface keeps the groups
separate deliberately.

### Reasons

Every test returns a `reasons` list alongside its status, and the interface and
the report render it next to the result. A status on its own tells an operator
that something is wrong but not what to do about it, which is the difference
between a usable QA tool and a number to be ignored.

| Field | Where |
|---|---|
| `reasons` | `linepairs`, `lowcontrast`, `uniformity`, `wedge` |
| `reason` (per group) | each row of `linepairs.rows` |
| `dimension_reasons`, `field_reasons` | `geometry` |

A reason states the measured value, the limit it was compared against, and —
where the two are distinguishable — whether the cause is more likely the
detector or the ROI placement, naming the step where it can be corrected. A
passing test explains why it passed, so a pass that rests on a mis-placed ROI
is still visible. Reasons are generated from the same numbers as the status;
they are not a separate judgement and cannot disagree with it.

---

## Validation

The test suite runs with `python -m pytest tests -q`.

### Synthetic ground truth

| Check | Result |
|---|---|
| Sub-pixel edge location | < 0.3 px |
| Peak centroids | < 0.15 px |
| ROI statistics | exact |
| TLS and affine round-trips | exact |
| Rectangle detection, 0°–12° rotation | < 1 px corner error |
| Grating pitch recovery, including a 6° direction mismatch | < 1 % |
| Line-block detection with impostors present | exactly 5 blocks, centres within 1.5 mm, impostors never selected |
| Strip displaced by 8 mm | tracked to < 2 mm |
| Grating angles at 30/60/120° | reported in phantom coordinates to < 8° |
| Synthetic collimation edges at 25/40/60 mm | found to < 2 mm; gradients and short probes rejected |

### Reference scans

Six scans in three orientations (0°, 90°, 180°):

- Rotation resolved correctly in every case; none mirrored; landmark residual
  RMS 0.13–0.55 mm.
- Tape pitch 5.00 ± 0.02 mm; mean side 299.35–300.44 mm; central-line
  separations 260 ± 1 mm.
- All five line groups detected in all six scans, grid residual RMS ≤ 0.03 mm.
- Wedge monotonic in every scan; uniformity |ΔSNR| < 4 %; low-contrast CNR
  monotone across L1…L8.

### Repeatability

- Line-group pitches agree across all six scans to about 0.1 %.
- Block centres repeat to 0.4–0.9 mm within a phantom variant.
- A 90°-rotated copy of a scan reproduces pitch, dimensions and group
  frequencies within the same tolerances — as do the real acquisitions at 0°,
  90° and 180°.

### Limitations

- Field-edge detection is limited by the acquisition, not the algorithm; see
  section 4.
- Mark-to-edge distances carry ±0.5 mm of processing-dependent uncertainty.
- The reference set contains two physically different phantom units, whose
  internal features differ by up to 12 mm and whose strip angles differ by about
  2°. Runtime measurement absorbs this, but the stored nominal coordinates are a
  mid-point rather than design intent. A third variant differing by more than
  the 28 mm search pad would need the seeds updated.
- The low-contrast circle grid is placed from the definition with a small
  matched-filter shift, not detected per circle: the individual discs are too
  faint to localise reliably in a single scan. Manual adjustment in step C is
  the fallback.
