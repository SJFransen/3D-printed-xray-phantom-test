# Design decisions

The reasoning behind the main architectural choices, for anyone modifying the
software.

---

## Fully automatic ingestion; no manual export

The DICOM files contain everything needed: lossless pixel data, pixel spacing,
kV, mAs, detector identification and the processing description. The application
reads them directly, so no number is ever transcribed from a viewer.

Manual transcription is the largest source of error and inconsistency in phantom
QA, and consistent ROI placement is something software does better than a
person.

## The program proposes, the user confirms

Nothing is measured from geometry the user has not approved. The wizard gates
registration, pattern identification, measuring points and dimensions before any
result is computed.

Full automation without verification would be faster but unverifiable; manual
measurement would be verifiable but inconsistent. The staged workflow keeps the
consistency of automation while leaving a person responsible for what is
measured.

Every displayed number is backed by an ROI drawn on the actual image, and any
ROI can be dragged, with both the automatic and the adjusted position retained.

## Constancy testing against a protocol signature

Pixel values in processed radiographs are not proportional to dose, so absolute
values cannot be compared across acquisition conditions. Each analysis therefore
records a **protocol signature** — detector model, kV, pixel spacing and
processing family — and comparisons are grouped by it.

Scans taken with different parameters, machines or processing are never silently
mixed.

## Scale anchored to the printed tape pitch

Three scale sources are cross-checked in every scan: the DICOM pixel spacings,
the measured 5 mm tape pitch, and the phantom's own dimensions.

The absolute scale is anchored to the **tape pitch**, not the metadata, because
the phantom face sits at a magnification of about 1.042 relative to the detector
plane. Trusting `ImagerPixelSpacing` alone would embed that error in every
dimension.

## No material assumptions

The phantom is experimental and not made of the materials a commercial phantom
would use, so the guide's nominal values — copper-equivalent thicknesses for the
wedge, percentage contrasts for the circles — are neither used nor displayed.

- Wedge steps are **S1…S7 by position**, and the fit is mean value against step
  index.
- Low-contrast circles are **L1…L8 in design order** of increasing measured
  contrast.
- Line-pair frequencies in the definition were measured from the reference
  scans, not taken on faith.

Two nominal values remain, both stated explicitly in the interface: the printed
5 mm tape pitch, used for scale, and the assumed 300 mm phantom side, for which
no drawing was available.

## Geometry measured per scan, not read from the definition

Line-pair block positions are detected in every scan; the stored coordinates are
search seeds only.

The reference set contains two physically different phantom units whose internal
features sit up to 12 mm apart. Stored coordinates cannot track that, and a
future phantom would differ again. Measuring per scan means the ROIs land
correctly regardless.

## The ROI is the handle; everything else follows it

A measurement region is rarely a single shape. A low-contrast circle has a
background ring and an object outline; a line-pair square has a profile line
that samples across the printed lines. Only one of them is what the user grabs.

When an ROI is moved or rotated, every shape attached to it is updated with it,
found by id prefix rather than a hard-coded list so a new companion cannot be
forgotten. The server returns all of them and the interface redraws all of them.

The rule this enforces: **what is drawn is what is measured.** A companion left
behind would put the displayed region in one place and the number it produced in
another, which is worse than an obviously wrong result because it looks right.

For the same reason the line-pair profile is derived from its ROI rather than
stored independently: the square is the handle, so the profile is rebuilt at the
new centre and its direction re-measured against the pattern actually there. A
direction the user sets by hand is marked as such and no longer overridden.

## A rigid group moves as one

The eight low-contrast circles are not independent measurements that happen to
sit near each other — they are a fixed grid printed inside one block. When the
block is found in the wrong place, all eight are wrong by the same amount, and
correcting them one at a time is eight chances to introduce a different error.

So the block itself is a handle: drag it, set its angle, or click its four
corners, and the circles are re-derived from the block frame. The four-corner
path mirrors manual phantom corners in registration, because it solves the same
problem — position and rotation are both wrong and clicking the corners states
both at once.

Placing the block discards the automatic grid-shift refinement. That refinement
was fitted to the previous placement; carrying it over would drag the circles
back off the objects the user just aimed at.

## Edits are serialised, not last-write-wins

Geometry is one JSON blob per analysis. Read-modify-write on a shared blob loses
whichever edit was read first, and the symptom is confusing rather than obvious:
an ROI springs back to where it was, and moving a *different* ROI appears to fix
it — because that request re-read fresher state.

Every geometry edit therefore runs inside a single `BEGIN IMMEDIATE`
transaction (`Store.mutate_geometry`), which takes the SQLite write lock before
reading. Overlapping edits queue instead of overlapping. The cost is a held
write lock for the duration of one edit, which is microseconds of JSON work; the
benefit is that the geometry the user sees is the geometry that was saved.

The undo snapshot is written inside that same transaction, not after it. A
snapshot taken in a second transaction would reintroduce exactly the lost-update
class this exists to prevent, one level up.

## Corrections are undoable, and the history is on the server

An operator adjusting eight ROIs by hand will misplace one. Before, recovering
meant re-uploading the scan — a disproportionate price for a slip, and one that
also cost the analysis id, the audit trail and any labels already entered.

Stage C therefore has Undo, Redo and two Resets, backed by a snapshot per edit
in `geometry_history`. Three decisions worth stating:

**The stack lives on the server, not in the page.** The browser resyncs from the
server whenever a request fails or the analysis is reopened — precisely the
moments an operator most wants to undo something. A stack in the page would be
empty then.

**Snapshots are whole, not deltas.** One low-contrast block placement rewrites
25 ROIs and a re-propose rewrites everything, so a delta scheme would have to
encode whole subtrees anyway. ~120 kB of geometry compresses to ~50 kB, and the
depth cap bounds the cost. Snapshot 0 — the untouched automatic proposal — is
pinned and never trimmed, because it is what "reset to auto-detected" returns
to. Returning to a stored snapshot rather than re-running detection also means
the reset is exact rather than merely likely to agree.

**A reset is an edit, not a rewind.** It appends a new state, so pressing it by
mistake is itself undoable. A rewind would silently destroy the work it replaced.

## A phantom's measuring points are remembered, per phantom

Two phantoms built to the same drawing are not the same object. Measured on the
reference scans, repeat scans of one build reproduce the internal feature
positions to 0.2–1 mm, while two builds differ by 3–4 mm — a tenfold margin, and
the DICOM header is identical between them. The difference is assembly, and it
does not drift, so re-correcting it on every scan is work the software should be
doing.

So the measuring points confirmed in Stage C are stored against the phantom
label and replayed on the next scan of that phantom. Four decisions carry the
safety:

**Millimetres in the phantom's own frame, never pixels.** Registration resolves
that frame from the phantom's asymmetric content, so a scan taken with the
phantom turned 90° or flipped lands on the same millimetre coordinates —
measured agreement is ≤ 0.1 mm on the reference scans. Pixel coordinates are
re-derived per scan. This is the whole reason a stored layout survives the
phantom being placed differently every time.

**Applied on top of a fresh detection, never instead of one.** The per-pattern
`detected` flags an operator reads in Stage B are only evidence if they came
from this scan. Building the geometry from the layout alone would bypass exactly
the detection whose failures reveal a mis-registered scan.

**Refused when it disagrees with that detection.** The gate compares the layout
against the scan's own proposal on the orientation-bearing patterns only — line
pairs, the low-contrast block, the wedge ends. Uniformity and the corner
dimensions are excluded because they are symmetric under all eight candidate
orientations and would vote "fine" for a layout landing on the wrong side of the
phantom. So would the registration residual, since the four rulers are
identical. A real assembly difference measures 3–4 mm; a wrong orientation
measures a hundred.

**It dies with the label.** Deleting or renaming the last analysis carrying a
phantom name deletes that name's layout, inside the same transaction that counts
the remaining analyses. Without this, a layout would outlive every scan that
justified it and be silently inherited by an unrelated phantom that happened to
be given the same name — which is indistinguishable, from the operator's chair,
from marks coming back from a deleted scan.

## Two dates, kept apart

The acquisition time comes from the scanner's clock; the upload time comes from
this server's. Neither carries a timezone, and a detector's clock can be unset,
reset during service, or wrong. Storing one date and falling back to the other
made those cases indistinguishable after the fact, which on a large dataset
loses the real order irrecoverably.

Both are now stored, exported and displayed separately, and an acquisition time
that is absent or impossible is labelled rather than replaced. Ordering still
falls back to the upload time so nothing drops out of a listing — but the
fallback is visible, and either date can drive the table and the trend axis.

## The same file is not analysed twice by accident

Uploads are fingerprinted with SHA-256, and a file already present is refused
with the existing record rather than silently duplicated: a duplicate shows
twice in History and counts twice in every trend, quietly distorting the
statistics it feeds.

The refusal offers three choices — open the existing record, deliberately
analyse again, or cancel — as three buttons rather than a two-way confirm. With
two buttons, "cancel" would have to mean "create the duplicate", so dismissing
the dialog would cause the exact outcome the check exists to prevent. Deliberate
re-analysis remains available because it is legitimate after an algorithm or
definition change.

## Manual correction is a first-class path

Automatic placement is seeded from the stored phantom definition, so a
different phantom build will place some patterns wrongly. That is expected and
cannot be fully engineered away — the software cannot know a layout it has never
seen.

What it can do is make correction reliable: every ROI can be moved and rotated,
the measurement follows, and the change is recorded in the audit trail with both
the automatic and the manual position. Detection quality determines how much
work the user does, not whether the result can be trusted.

Corrections are also **undoable** and **remembered per phantom**, so the cost of
a mis-detection is paid once rather than on every scan.

Values the operator types belong in fields on the page, not in browser prompts.
A prompt cannot show the record being acted on, cannot validate before sending,
and cannot display the server's refusal — a mistyped angle became `NaN` and
silently nudged the geometry, and a refused deletion looked like a successful
one.

## Fixed ROI sizes where results are compared

Wedge step ROIs are a fixed size for every step rather than fitted to each
step's height. The printed steps are not equal heights, so per-step sizing would
average a different area in each one and make the same step incomparable between
phantoms. Each step reports its measured height and whether the fixed ROI fits.

## Display scaling relative to the image

Window width and centre are expressed as a fraction of each image's own measured
value range, not as absolute pixel values. Detectors differ in bit depth — 12
bits on one unit, 14 on another — so a control calibrated to one blanks out an
image from the other entirely.

The renderer and the sliders share the same 1–99 percentile automatic range.
Opening a scan resets the controls to that window, and late image responses
cannot overwrite a newer W/C setting or the next scan. Windowing also works
before successful registration and preserves fractional source values.

## Detectors report failure rather than guessing

Where a feature cannot be found reliably, the software says so and requests
manual placement, instead of returning a number that looks authoritative.

Field-edge detection starts along each blue ruler line and walks outward to the
first zero-valued boundary, including a boundary inside the phantom. Without a
zero boundary, it requires a genuine intensity step that a linear ramp cannot
explain as well, with sufficient plateau on both sides and away from the image
border. An image border alone does not establish the radiation-field edge.

## Pass criteria matched to the phantom, not to convention

The wedge response is reproducibly S-shaped because the printed steps are not
equal increments of attenuation. Judging the detector by the linearity of that
curve would fail a sound detector for a property of the phantom.

Pass and fail are therefore decided by **monotonicity and saturation**, with R²
retained as a shape descriptor carrying a soft warning threshold.

## A reference per phantom, and it can be given up

A constancy test needs something to be constant against. That reference used to
be scoped to the protocol signature alone, which quietly assumed one phantom per
machine. It is not true here: the reference set contains two builds whose
internal features sit millimetres apart, both valid, and a site can run both on
one unit. Marking the second one's reference silently demoted the first one's,
so one of the two phantoms could never be trended at all.

The scope is now the phantom AND the protocol. The protocol half stays because
pixel values in processed radiographs are not proportional to dose, so comparing
across kV, detector or processing is meaningless whatever the phantom was.

A reference is also removable. It was not, and a reference is a judgement — a
scan chosen before anyone noticed it was poor should not be permanent just
because the flag was one-way. Removing leaves the phantom with none, which is a
legitimate state and the one every phantom starts in. Setting and clearing are
both audited, and both name the phantom, because the operator needs to see that
the decision was scoped to one phantom rather than to everything on the machine.

Finalising an analysis no longer touches the flag. It is pressed more than once,
and a second press must not undo a decision made deliberately somewhere else.

## Detail is one click away, not on the page

The wizard is used by people who are not medical physicists. A step that opens
with four dense tables asks them to decide which numbers matter, which is the
one thing they cannot do — and the risk is not confusion but false confidence:
a table nobody reads is indistinguishable from a table that says everything is
fine.

Steps B, D and E therefore lead with a verdict in words — what was found, what
needs attention, and which test — and fold the measurements behind a native
`<details>`. Native because it needs no script, survives the re-render each
stage does, is keyboard-accessible, and prints expanded, so the printable report
stays complete even when the screen is not. In step E anything that did not pass
opens itself, so the detail a reader needs is never the detail they have to go
looking for.

Nothing is removed. The same numbers are one click away for whoever wants them,
which is what makes the summary safe to trust.

## Results are stored, not recomputed

An analysis keeps the numbers produced by the algorithm and definition current
when it was computed, together with both version stamps.

A QA record that changed silently under a software update would be worthless,
especially one that has been signed off. Because the source file is also kept,
results can be recomputed deliberately with `manage reanalyze` when an
improvement warrants it.

## Median of the selection as the comparison reference

The comparison report measures every deviation against the **median of the
selection**, not against the first entry.

When comparing several phantoms there is no meaningful "first", so
"change since the first scan" would be an arbitrary framing. For a single
phantom over time the median remains a sensible reference, so one layout serves
both cases.

Metrics that are themselves percentages, such as pitch deviation and ΔSNR, are
compared in percentage points, because their median sits near zero and a ratio
would be meaningless.

## Two privilege levels, with the administrator password per action

A user can run analyses and read everything. Deleting and validating require a
separate administrator password, entered at the moment of the action rather than
at sign-in.

This lets an administrator work as an ordinary user and supply the credential
only when making a decision that an ordinary user must not make. Deletion
additionally requires a written reason, which is what the audit log keeps: the
password establishes the right to destroy data, the reason records why it was
destroyed.

The reason replaced a requirement to type the analysis id back. That guard was
theatre — a copy-paste satisfied it — and it had a cost: its refusal appeared
only as a status message that cleared itself, so a deletion that had not
happened could be taken for one that had. The confirmation panel now shows the
record being destroyed and keeps every refusal on screen until it is answered,
which is the property the id-typing was reaching for.

The approver's **name** is recorded separately from the password: a shared
credential establishes the right to sign off, not who did.

A ruling also freezes the analysis. Every path that would change the geometry,
the registration or the results is refused while a ruling stands. This became
necessary once a geometry edit correctly dropped results computed from geometry
that no longer exists: on a validated analysis that left the ruling, the name
and the date intact with the numbers gone, and the record then vanished from
every trend and export, all of which select on completed results. The reanalyze
CLI already skipped signed-off analyses; the web path now matches it. Withdrawing
is one click and is itself audited, so this costs nothing but deliberateness.

## Trust the ASGI server for the client address

The application does not parse `X-Forwarded-For`. uvicorn already does, and only
when the immediate peer is listed in `forwarded_allow_ips`.

Parsing the header in the application as well would discard that trust boundary:
on a shared server, any process able to reach the loopback port could forge an
address per request and evade the login throttle.

For the same reason the sub-path comes from configuration rather than from
`X-Forwarded-Prefix` — a client-controlled header should not determine where the
application believes it is mounted.

## Shared throttle state

Gunicorn runs several worker processes, so the login and administrator throttles
are stored in SQLite rather than process memory. An in-memory counter would give
an attacker `max_attempts × workers` guesses.

## Self-contained, offline-capable output

Reports embed their charts and overlays as data URIs, and the frontend has no
build step and no external dependencies. The application runs without internet
access, and an exported report remains readable years later with no server
involved.

---

## Technology

Python 3.11+, FastAPI and uvicorn, numpy, scipy and scikit-image, pydicom with
pylibjpeg, matplotlib for report charts, and SQLite for storage. The frontend is
plain JavaScript with a `<canvas>` viewer.

SQLite in WAL mode is appropriate at QA-team concurrency. Should that change,
`store.py` is the only module that would need replacing.

---

## Open points

- **Phantom drawing.** No CAD or drawing was available, so `nominal_side_mm`
  (300.0) is assumed design intent. Everything else in the definition is
  measured. A drawing would allow dimensional deviations to be judged against
  design intent rather than against a self-derived reference.
- **Tolerances.** The guide's values are implemented (±2 % of SID, |ΔSNR| < 20 %).
  Constancy bands default to ±20 % and should be revisited once several months
  of data exist.
- **Per-user accounts.** There is one shared login and one administrator
  password. The approver's name is typed rather than authenticated. If
  attribution must be provable, per-user accounts are a genuine feature to add.
- **One phantom definition at a time.** The application loads `msf_v1.json`.
  Supporting several phantom builds in one installation means selecting the
  definition per phantom — recording it on the analysis, and grouping trends by
  it as protocol signature already does.
- **Identity assignment when a build differs.** Line-pair groups are matched to
  the definition by order along the strip, falling back to nearest measured
  frequency. On a build whose strip runs in the opposite frequency order the
  fallback still identifies the groups, but a tolerant frequency match can
  mislabel one. Declining to label a block whose frequency or spacing is
  inconsistent with its neighbours would be truer to the "report failure rather
  than guess" rule applied elsewhere.
