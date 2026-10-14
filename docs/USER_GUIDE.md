# User guide

## Signing in

Open the application URL and sign in with the shared user account. The session
lasts 12 hours by default.

Deleting and validating additionally require the **administrator password**,
which is entered at the moment of the action rather than at sign-in.

---

## Running an analysis

The wizard has seven steps. Nothing is measured from geometry you have not
confirmed, and you can move back at any point.

Throughout, the image viewer supports mouse-wheel zoom and drag to pan. Overlay
layers can be toggled individually below the image.

The **W** (width) and **C** (centre) sliders are **relative to the image's own
value range**, shown as a percentage with the resulting absolute values beside
them. Detectors differ in bit depth — 12-bit on one unit, 14-bit on another — so
a fixed absolute scale would white out an image from the wrong device entirely.
**auto** returns to the automatic 1–99 percentile window. Opening another scan
resets both controls to that scan's automatic window.

### Upload — choose the file and identify the scan

Choose the scan file and fill in the identification, in either order. Nothing is
uploaded until you press **Upload & analyse**.

| Field | Purpose |
|---|---|
| **Site** | Groups analyses for trending. Required for grouped comparison |
| **Phantom** | Identifies the physical phantom. Required for grouped comparison |
| Operator | Optional |
| Notes | Optional |

Previously used values appear as suggestions, and the last values are remembered
for the rest of the session so a batch of scans need not be retyped. A warning
appears if both Site and Phantom are empty, because such an analysis will not
appear in any grouped trend.

Labels can be changed later at any time — see *Correcting the identification*.

**A file that has already been analysed.** Every upload is fingerprinted with
its SHA-256. If the same file is uploaded twice the program stops and shows the
existing record instead of quietly creating a second one, because a duplicate
would appear twice in History and count twice in every trend. Three choices are
offered:

| Choice | Effect |
|---|---|
| **Open the existing analysis** | Go to the record that already holds this file |
| **Analyse again anyway** | Deliberately create a second record — for a re-analysis after a code or definition change |
| **Cancel** | Do nothing |

Re-uploading a file whose record was deleted is allowed and creates a new one —
with no geometry, no edit history and no stored layout carried over from the
deleted record.

### A — Registration

The program locates the phantom, resolves its orientation (any rotation,
mirrored or not) and reports the fitted outline, rotation, scale and the
per-side landmark residuals.

Check that the red dashed outline follows the phantom edge. If detection failed,
choose **Manual corners** and click the four phantom corners yourself; the order
does not matter.

### B — Patterns

The step opens with one line: how many patterns were found, and which were not.
That is normally all you need — the check that matters is on the **image**, not
in this panel. **Detection detail** opens the full table for anyone who wants
the measured values.

Every detected pattern is outlined and labelled: the five line-pair groups with
their frequencies, the low-contrast block, the wedge steps, the uniformity
squares, the rulers and the field edges.

The four field-edge markers are initialised by walking outward along the blue
ruler lines until zero-valued pixels begin. A marker can sit inside the phantom
when the radiation field is smaller than its face; the detection table reports
the distance as **inside** or **outside**.

Confirm each label sits on the correct object. Zoom in — a mislabelled group or
a 180° mix-up must be caught here.

Two ways on from here:

- **Verify measuring points →** continues to step C, where every ROI can be
  moved and rotated by hand. This is the way on whether or not the automatic
  labels are right: a wrong pattern is corrected in step C, not by confirming
  it here.
- **Back to registration** returns to step A, for when the phantom outline
  itself was found wrongly and everything downstream inherited the error.

### C — Measuring points

The actual measurement ROIs are shown. Click an ROI's centre dot to read its
mean, standard deviation and pixel count.

**Moving.** Drag the centre dot. Everything attached to that ROI moves with it —
the low-contrast background ring and object outline, and the line-pattern
profile line — so what you see is always what is measured. Adjusted ROIs turn
orange, and both the automatic and the manual position are kept in the audit
trail.

**Rotating.** Select an ROI and use the angle slider in the details panel at the
bottom, type an exact angle in the box beside it, or use the ±1° buttons and the
`[` and `]` keys. This matters when a phantom differs from the definition and
the automatic orientation is wrong.

**Undoing.** Every change to a measuring point can be undone — **↶ Undo** and
**↷ Redo** at the top of the step, and the count on the button says how many
steps are available. The history is kept on the server with the analysis, so it
survives closing the analysis and coming back to it. A slip never costs more
than a click, and never costs a re-upload.

**Starting again.** Two resets, because "start again" is ambiguous once a
phantom has a stored layout:

| Button | Goes back to |
|---|---|
| **Reset to auto-detected** | This scan's own automatic detection, exactly as it was proposed — not a re-detection |
| **Reset to stored layout** | The measuring points confirmed for this phantom previously (only shown when there is one) |

A reset is itself an undoable step, so pressing one by mistake does not destroy
an afternoon's work.

For a line-pattern group, rotating the square also sets the profile direction by
hand, overriding the automatic one. Move the square first, then rotate only if
the automatic direction did not follow the pattern.

If a radiation-field edge was not detected automatically, select a side and
click the visible field edge on the image.

> **A phantom that differs from the definition.** Automatic placement is seeded
> from the stored phantom geometry, so on a different build — or a different
> X-ray unit — some patterns will be found in the wrong place. That is expected.
> Correct them here by dragging and rotating; every measurement is taken from
> where you put the ROI. You only have to do it once per phantom — see
> [Measuring points remembered per phantom](#measuring-points-remembered-per-phantom).

#### Measuring points remembered per phantom

Two phantoms built to the same drawing are not the same object: on the reference
scans the printed internal features sit several millimetres apart between two
builds, while repeat scans of one build reproduce to a few tenths of a
millimetre. That difference is assembly. It does not drift over time, so
correcting it on every scan is wasted work.

So when you press **Measuring points confirmed ✓**, the positions are stored
against the **Phantom** name on the analysis. The next scan you upload with that
same name starts from them instead of from automatic detection, and the step
says so at the top:

> *Measuring points: stored layout for phantom MSF-01 — saved 2026-08-14 09:12
> by S. Tkac · 31 measuring area(s)*

Things worth knowing:

* **Name the phantom.** The name is the only thing that identifies it — the
  DICOM header of two different phantoms on the same X-ray unit is identical.
  Use the same spelling every time; `MSF-01` and `msf-01` are two phantoms as
  far as the software is concerned, and it will warn you when you create a
  layout for a name that differs from an existing one only in spelling.
* **The phantom may lie differently on the detector each time.** The positions
  are stored in the phantom's own frame, not in image pixels, so a scan with the
  phantom turned 90° or flipped replays them correctly.
* **A stored layout is never applied blindly.** It is laid on top of a fresh
  detection of this scan, and if the two disagree about where the phantom's
  patterns are — which is what a mis-registered scan looks like — the layout is
  refused and the step tells you why. Check step A in that case.
* **Field edges are never stored.** Collimation belongs to the exposure, not to
  the phantom.
* **It is not permanent.** **Reset to auto-detected** ignores it for this scan;
  confirming step C again replaces it; and it is deleted automatically when the
  last analysis carrying that phantom name is deleted or renamed.

#### Reading the overlays

| Marking | Meaning |
|---|---|
| Red dashed square | Detected phantom outline |
| Yellow squares on the diagonal strip | Line-pair group ROIs, set 45° to the strip so their corners meet the nub markers and the strip centre line |
| Short yellow line through each square | The profile sampled for pitch and linearity; it crosses the printed lines |
| Pink dashed circle | Low-contrast object outline (10 mm) |
| Pink solid circle | Low-contrast measurement ROI (7 mm) |
| Pink dotted ring | Local background ring for that same circle |
| Green squares | Uniformity ROIs |
| Orange rectangles | Wedge step ROIs (S1 top to S7 bottom) |
| Blue lines from each side | Ruler and field-edge probes |

Dragging a low-contrast circle moves its ring and outline with it.

#### Repositioning the whole low-contrast block

The eight circles sit on a fixed grid inside the block, so when the block as a
whole is in the wrong place there is no need to drag eight circles. Move the
block and all eight follow. Three ways, in the block's own panel in step C:

| Action | How |
|---|---|
| **Turn end for end** | **⟲ Turn 180°** — one button, see below |
| **Move** | Drag the block rectangle itself (not a circle) |
| **Fine rotation** | Type the angle in the **fine angle** box in the step panel — the image previews as you type, Enter or **Apply** commits, `[` and `]` nudge by 1° |
| **Redefine completely** | **Click 4 block corners…** then click the block's four corners on the image, in order around the rectangle |

**Turn 180° first, if at all.** The block outline is symmetrical, so detection
can only ever determine its angle to within 180° — it genuinely cannot tell
which end is which. When it guesses wrong, all eight ROIs still land on real
discs: L1 sits on L5's disc, L2 on L6's, L3 on L7's, L4 on L8's. Nothing looks
wrong on the image and no geometric check fails. The one symptom is step E
reporting that **|CNR| is not in design order**.

That makes it a yes-or-no mistake rather than a matter of degrees, which is why
it has its own button. Press it, and the fine angle box is for everything else.
The turn is stored with the phantom's layout like any other correction, so the
next scan of that phantom starts the right way round.

The four-corner method is the same idea as manual phantom corners in step A and
is the one to use when the block is both shifted and rotated: the centre and the
angle are both fitted from the four clicked points.

Any of the three marks the block and all eight circles as manually adjusted, and
discards the automatic grid refinement — that refinement was a correction to the
*old* placement and would otherwise pull the circles back off the objects.

### D — Dimensions

Two lines at the top say whether the phantom measures the size it should and
whether the X-ray field is centred. If both are green, carry on. **Measured
dimensions** opens the corner measurements, the ruler pitches, the scale
cross-check and the per-side field deviations.

Verification of the millimetre calibration before any measurement depends on it:

- corner-mark distances: four sides and both diagonals
- per-ruler mark pitch and the linearity of the 5 mm marks
- central-line separations
- a three-way scale cross-check: measured tape pitch against both DICOM pixel
  spacings, including the implied magnification
- field-edge deviation per side, in mm and as a percentage of SID

Enter the **SID** here (default 1000 mm). Confirming this step locks the
calibration used by every later number.

**Unusable rulers are excluded.** The mm/px scale comes from the measured 5 mm
mark pitch, so a ruler that was not actually found would corrupt every length in
the report. A ruler whose marks are not evenly spaced — residual RMS above
`ruler_linearity_rms_max_mm`, 0.5 mm by default — is dropped from the scale and
named in the reasons. If fewer than two usable rulers remain the scale cannot be
cross-checked, and the step says so: treat the dimensions as indicative and fix
the ruler probes in step C before relying on them.

### E — Analysis

The results open as a **summary table**: one row per test, its verdict, and the
single number that verdict rests on. Below it, a line naming anything that needs
attention.

Each test's full detail — every ROI, every measured value, the charts, and the
reason behind the verdict — is in a collapsible section beneath. **Anything that
did not pass is already open**, so you never have to go looking for the part
that matters. Nothing has been removed; it is one click away.

All tests are computed from the confirmed geometry:

- **Line patterns** — standard deviation per group, plus the intensity profile
  across each group with the fitted line grid, measured pitch against nominal
  frequency, and per-line residuals in micrometres.
- **Wedge** — mean per step S1…S7, monotonicity, dynamic-range ratio, saturation
  flags, and the linear-fit R² as a shape descriptor.
- **Low contrast** — CNR per circle L1…L8 in design order, with an ordering
  sanity check.
- **Uniformity** — SNR per square and ΔSNR against the mean, tolerance ±20 %.
- **Field alignment** — deviation from each side's central long line, tolerance
  ±2 % of SID.

#### Why a test passed, warned or failed

Every test states its reasoning next to its result, so a bad result can be
acted on rather than merely noted. A green box gives the reason the test
passed; an amber box lists what went wrong, in the test's own terms, with the
measured value, the limit it was compared against, and where to go to correct
it. Line patterns additionally give a per-group reason so it is clear *which*
group is at fault.

Most reasons distinguish a genuine detector problem from a placement problem —
for example, a low-contrast ordering failure says the grid may not be on the
printed objects and points back to step C, and a non-monotonic wedge says a
step ROI may be sitting on a boundary. Read the reason before repeating the
exposure.

### F — Save and export

The analysis is stored with its full audit trail. From here you can:

- mark it as the **reference scan** for its phantom (see below)
- open the **printable report**, or download **CSV** or **JSON**
- **verify the source file** against its recorded SHA-256
- set the **validation** state

---

## Correcting the identification

Once an analysis is open, an identity bar above every wizard step shows Site,
Phantom and Operator with an **Edit** button. Labels can be corrected at any
point — during the wizard, after the results are computed, or later from the
**label** link in the History table. Every change is recorded in the audit
trail, and the analysis appears immediately in the matching filters and trends.

---

## Validation

The measurements describe the phantom; a person still decides whether it is
accepted. That decision is recorded on the analysis.

| State | Meaning |
|---|---|
| **Pending review** | Nobody has ruled yet. The state of every new analysis |
| **Validated** | The phantom and this measurement are accepted |
| **Conditionally validated** | Accepted with reservations — state them in the comment |
| **Not validated** | Rejected — state why in the comment |

Recording a decision requires the administrator password, the **name of the
person approving**, and an optional comment. The name is stored separately from
the password: a shared credential establishes the right to sign off, not who
did.

**A signed-off analysis is locked.** Once a ruling is recorded, the measuring
points, the registration and the results cannot be changed — the wizard refuses
with a message naming who signed it. Validation means a person took
responsibility for a specific set of numbers, so those numbers cannot quietly
become different ones. To rework an analysis, withdraw the ruling first; the
withdrawal is itself recorded.

Set it from the identity bar, from step F, or from the **validate** link in the
History table. A ruling can be changed or withdrawn later; every ruling and
reversal is written to the audit log with the previous state.

The decision appears at the top of the printable report, in the comparison
report, as a column and filter in History, and in both CSV exports.

---

## History and trends

The History tab works from either a **filter** or a **manual selection**:

- filter by Site, Phantom, Protocol or Validation state; the counts beside each
  option show how many analyses match
- or tick individual rows — as soon as anything is ticked, the ticked rows take
  precedence over the filter, and a note states which is in effect

### Two dates, kept apart

The table carries both:

| Column | Where it comes from |
|---|---|
| **acquired** | The scanner's own clock, from the DICOM header |
| **uploaded** | This server's clock, when the file arrived |

They are not interchangeable. A detector's clock can be unset, reset during
service, or simply wrong, and nothing in DICOM records a timezone. When the
header carries no usable date the acquired cell reads **unknown ⚠**; when it
carries one that cannot be right — a year before 2000, or a time after the
upload — it is shown with a ⚠ as well, and the count of such rows appears beside
the filter. The acquisition column is never quietly filled in from the upload
clock.

**Order by** switches the table between the two. Acquisition order is the
default and is what you want normally, because re-analysing an old scan should
not disturb the sequence. Upload order is what you want when a unit's clock is
suspect: on a large dataset it is the only ordering that is certainly real.

The trend chart has the same choice in **Date axis**, and marks points with no
trustworthy acquisition date in amber so a run of them cannot be mistaken for a
real chronology.

Three outputs follow the current selection:

| Output | Contents |
|---|---|
| **Comparison report** | A visual comparison of the whole selection — see below |
| **Trend chart** | One metric over one phantom's scans — see below |
| **CSV export** | *Long*: one row per metric per analysis, for pivot tables. *Wide*: one row per metric, one column per analysis, for reading drift directly. Both carry site, phantom, operator, acquisition time, upload time and whether the acquisition time is trustworthy |

### The trend chart

Collapsed by default — open **Trend of a single metric** when you want it.

The chart draws nothing until you have said which scans belong together:
pick a **Phantom** in the filter, or tick rows in the table. That is not a
limitation but the point — two phantoms differ by design, so a line drawn
across them would show their assembly differences as if a detector were
drifting.

Once a phantom is selected: pick the metric, hover any point for its exact
value, date and scan, and switch the **Date axis** to the upload date when the
scanner's clock is suspect. The reference scan is the highlighted point, the
dashed band is ±20 % of it, and amber points carry no trustworthy acquisition
date. With a long series (a hundred scans and more) the date labels thin out
automatically so they stay readable — the hover readout always has the exact
date.

### The comparison report

Built from plots rather than tables, and equally suited to one phantom over time
or to many phantoms side by side. Every visual identifies entries by **Site /
Phantom**, with the date appended only when the same phantom appears more than
once. A key at the top maps each label to its full record.

- **Status grid** — every test against every analysis, including a validation
  row
- **Where the differences are** — a ranked bar chart of the metrics that vary
  most across the selection
- **Per-pattern panels** — one small plot per object (per line-pair group, per
  low-contrast circle, per uniformity square, per wedge step), each analysis a
  labelled point coloured by site, with the selection median as a dashed
  reference. Whole-curve comparisons are included for the wedge response and the
  low-contrast CNR series
- **Deviation heatmap** per pattern — metrics that are effectively identical
  across the selection are omitted and counted rather than coloured
- **Numeric detail** — present but collapsed behind a toggle in each section

Deviations are measured against the **median of the selection**, not against the
first entry: when comparing several phantoms there is no meaningful first. For a
single phantom over time the median remains a sensible reference, so one layout
serves both.

Metrics that are themselves percentages, such as pitch deviation and ΔSNR, are
compared in percentage points rather than as ratios, because their median sits
near zero.

---

## Comparing like with like

Pixel values in processed radiographs are not proportional to dose, so all tests
are **constancy tests**: each scan is compared against a reference.

### The reference scan

A reference belongs to **one phantom on one protocol**.

The phantom half matters because two phantoms built to the same drawing are not
the same object — on the reference set here, two builds have internal features
several millimetres apart, and both are perfectly valid. A site running two
phantoms on one machine needs a reference for each. Marking one phantom's
reference therefore never disturbs another's.

The protocol half stays because comparing across kV, detector or processing is
meaningless whatever the phantom. Each analysis records a **protocol
signature** — detector model, kV, pixel spacing and processing family — and the
comparison report prints it so a mixed series is visible.

Set or remove a reference from step F, or with the ★ in the History table:

| Symbol | Meaning |
|---|---|
| ★ | This is the reference for its phantom on its protocol. Click to remove it |
| ☆ | Click to make this the reference; whatever held the role for that phantom stands down |

A reference can always be removed, leaving that phantom with none until another
is chosen — a reference picked from a scan that later turns out to be poor must
not be permanent. Reduced-precision analyses and analyses without results cannot
be references.

Because each phantom has its own, a trend chart covering several phantoms
contains several references, and the ±20 % band is then not drawn: it would be a
band around an arbitrary one. Filter to a single phantom to see it.

---

## Source file integrity

Every analysis records a SHA-256 fingerprint of the file it was computed from
and keeps a copy of that file. See
[MAINTENANCE.md](MAINTENANCE.md#source-file-integrity) for what it detects and
how to check it.

---

## Deleting an analysis

The **delete** link in the History table opens a panel that shows exactly what
is about to be destroyed — the id, the site and phantom, both dates, the source
file name and the result — and asks for two things:

1. a **reason**, at least five characters, recorded in the audit log next to who
   did it and from where. It is the only record of why the data went;
2. the **administrator password** — not your everyday login.

Deleting removes the record, the stored source file and the measuring-point edit
history. If it was the last analysis carrying its phantom name, that phantom's
stored measuring-point layout goes too, and the panel warns you before you
confirm.

The panel stays open until the server accepts. A wrong password or a missing
reason is shown in the panel itself, so a deletion can never appear to have
happened when it did not.

> Typing the analysis id back is no longer required. It protected nothing that a
> copy-paste did not satisfy, while its refusal was a message that cleared
> itself after a few seconds — which is how a failed deletion could be mistaken
> for a successful one.

---

## Reduced-precision mode

A JPG or PNG upload is accepted but marked *reduced precision*: 8-bit lossy
data, no acquisition metadata, and scale derived from phantom geometry alone.
Such analyses are excluded from baselines by default and are best used for a
quick look rather than for trending.
