# MSF Phantom QA

Analysis software for X-ray scans of the MSF 3D-printed QA phantom.

Upload a DICOM scan, confirm what the program detected in a step-by-step
wizard, and get reproducible QA metrics with full traceability — plus reports,
exports and trend comparison across phantoms and sites.

It implements the tests of the *MSF X-ray QA phantom acquisition and analysis
guide*:

| Test | Metric |
|---|---|
| Spatial resolution | Standard deviation per line-pair group, plus measured line pitch and per-line residuals |
| Low contrast | CNR for each of 8 circles |
| Uniformity | SNR per square and ΔSNR against the scan's own mean |
| Dynamic range | Mean per wedge step, monotonicity and dynamic-range ratio |
| X-ray / light field alignment | Field-edge deviation from each side's central line, in mm and % of SID |
| Dimensions | Corner-mark distances and side-ruler pitch |

**No material assumptions are made.** Wedge steps and low-contrast circles are
identified by position and design order only; every reported value is a direct
measurement.

Every test reports **why** it passed, warned or failed — the measured value, the
limit it was compared against, and where to correct it — so a result can be
acted on rather than merely recorded.

## Documentation

| Document | Contents |
|---|---|
| [docs/USER_GUIDE.md](docs/USER_GUIDE.md) | Running an analysis, validation, comparing scans, exports |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Installing on a server: nginx, gunicorn, security |
| [docs/MAINTENANCE.md](docs/MAINTENANCE.md) | Backups, integrity checks, re-analysis, logs |
| [docs/ALGORITHMS.md](docs/ALGORITHMS.md) | How each measurement is computed, and its validation |
| [docs/DESIGN.md](docs/DESIGN.md) | The main design decisions and why they were taken |
| [docs/AUTO_ANALYSE.md](docs/AUTO_ANALYSE.md) | Automatic operator workflow, integration points, limitations and safe removal |
| [docs/USERS.md](docs/USERS.md) | A description on how different users should be able to interact with the site |

## Requirements

Python 3.11 or later. Install dependencies:

```bash
python -m pip install -r requirements.txt
```

## Running locally

```bash
python run_app.py                 # then open http://127.0.0.1:8777
```

Everything runs on your machine; no internet connection is used.

```bash
python -m pytest tests -q         # test suite

# headless batch analysis, without the verification steps
python -m phantom_qa.cli "path/to/DICOMFILE" --out qa_output --sid 1000
```

## Running on a server

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md). In outline:

```bash
cp .env.example .env
python -m phantom_qa.manage gen-secret          # PHANTOMQA_SECRET_KEY
python -m phantom_qa.manage set-password        # user login
python -m phantom_qa.manage set-admin-password  # delete + validate
python -m phantom_qa.manage check               # must exit 0
gunicorn -c gunicorn.conf.py phantom_qa.webapp.main:app
```

## Access levels

| Level | Credential | Permitted actions |
|---|---|---|
| **User** | login password | Upload, run the wizard, edit labels, read reports, verify integrity, export |
| **Admin** | additionally the administrator password | Delete an analysis, validate one, discard a phantom's stored measuring-point layout |

The administrator password is entered per action rather than at sign-in, so an
administrator works as an ordinary user and supplies it only when deleting or
signing off.

## What to upload

| Input | Support |
|---|---|
| DICOM file (PACS or CD export, JPEG-Lossless included) | Preferred — full precision, metadata, protocol signature |
| Zipped DICOM CD export | Every DICOM image inside is found automatically |
| JPG or PNG | Accepted but marked *reduced precision*: 8-bit lossy, no metadata, scale from phantom geometry only. Excluded from baselines |

You never transcribe numbers from a DICOM viewer — the application reads the
pixel data and metadata directly.

## Project layout

```
phantom_qa/                 application package
  ingest.py                 DICOM / zip / image loading, normalisation, metadata
  features.py               sub-pixel measurement primitives
  registration.py           phantom detection, orientation, affine transform
  phantom_def.py            phantom definition loader
  layout_profile.py         per-phantom measuring-point layouts (store and replay)
  analysis/                 one module per test
  pipeline.py               stage orchestration and overlay rendering
  store.py                  database, labels, filtering, CSV export, undo history
  report.py                 single-analysis HTML report
  comparison_report.py      multi-analysis comparison report
  reanalyze.py              recompute stored analyses from kept source files
  config.py                 .env loading and production safety checks
  security.py               password hashing, sessions, CSRF, throttling
  logging_setup.py          rotating application, error and audit logs
  manage.py                 administration CLI
  cli.py                    headless batch analysis
  webapp/                   FastAPI backend and browser frontend
data/phantom_definitions/   calibrated phantom geometry (version controlled)
data/                       database and uploaded scans (created at runtime)
logs/                       application, error and audit logs (created at runtime)
gunicorn.conf.py            production server configuration
tests/                      test suite
```

## Phantom definition

`data/phantom_definitions/msf_v1.json` holds each object's position in the
phantom's own millimetre coordinate system, calibrated from six reference scans
covering three orientations. Its `provenance` section records how each value was
obtained.

The layout drives positioning as follows:

- **Template first, then local refinement.** Registration projects the complete
  feature layout into the scan. Bounded iterations align feature outlines and
  groups from coarse to fine, preserving feature sizes and the measured QA
  quantities. Search settings are stored with each feature in the definition.
  See [docs/ALGORITHMS.md](docs/ALGORITHMS.md) for scores and fallback behaviour.

- **Line-pair blocks are measured in every scan.** The stored positions are
  search seeds only, so small differences between phantom units are absorbed
  automatically.
- **The reference set contains two different phantom units.** Their internal
  features sit up to 12 mm apart and their strip angles differ by about 2°. The
  stored values are the mean of the two, so they are not design intent.
- **A substantially different build needs its own definition.** Runtime
  measurement handles a few millimetres of variation, not a different layout.
  Reversed line-pair frequency order is resolved from measurements; a different
  number of groups or substantially different geometry needs a matching definition. See
  [docs/MAINTENANCE.md](docs/MAINTENANCE.md#a-phantom-that-differs-from-the-definition).
- **Hand corrections are remembered per phantom.** Confirming step C stores the
  measuring points against the analysis's Phantom name and replays them on the
  next scan of that phantom, in the phantom's own frame so a different placement
  on the detector does not matter. It complements a calibrated definition rather
  than replacing one: it freezes ROI positions, it does not change the nominal
  geometry results are measured against.

`nominal_side_mm` (300.0) is an assumed design dimension; no drawing was
available. Measured side lengths are 299.3–300.4 mm.

## Known limitations

- The viewer displays a downscaled rendering; all measurements are made on the
  full-resolution data.
- **Automatic placement is seeded from the stored phantom definition.** On a
  different phantom build some patterns will be placed wrongly. Every ROI can be
  moved and rotated by hand — and the low-contrast block as a whole, by dragging
  it, setting its angle, or clicking its four corners — with the measurement
  following it. For a build you use regularly, calibrate its own definition.
- **The millimetre scale comes from the printed 5 mm ruler marks.** A ruler
  whose marks are not evenly spaced is excluded from the scale and named in the
  results; with fewer than two usable rulers the scale cannot be cross-checked
  and the dimensions are reported as indicative.
- Field-edge detection requires a visible collimation edge. Where the phantom
  nearly fills the detector, the test reports "not measurable" rather than
  guessing.
- The wedge response is not linear by design of the phantom, so R² is a shape
  descriptor rather than a pass criterion. See
  [docs/ALGORITHMS.md](docs/ALGORITHMS.md).
- One shared user login and one administrator password; there are no per-user
  accounts. The approver's name on a validation is typed, not authenticated.
- `data/uploads/` holds the original scans. These are phantom images, but the
  DICOM headers still carry institution and device fields — treat the directory
  as sensitive.
