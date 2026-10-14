# Maintenance

Backups, integrity checks, re-analysis and logs.

---

## What is stored, and where

| Path | Contents | Version controlled | Back up |
|---|---|---|---|
| `data/phantom_definitions/*.json` | Calibrated phantom geometry — source, not runtime data | **yes** | with the code |
| `data/phantom_qa.sqlite3` | All analyses: labels, geometry, results, validation, audit trail; the per-phantom measuring-point layouts; the Stage C undo history | no | **yes** |
| `data/phantom_qa.sqlite3-wal`, `-shm` | SQLite runtime companions | no | no |
| `data/uploads/*.bin` | The original scan of every analysis | no | **yes** |
| `logs/*` | Application, error and audit logs | no | per retention policy |

`data/` and `logs/` are excluded by `.gitignore`, so `git pull` and branch
changes cannot touch them.

### The `-wal` and `-shm` files

The database runs in WAL mode so gunicorn's workers can read while another
writes. SQLite creates these two companions whenever a connection is open and
removes them when the last one closes, so they appear and disappear during use.

They need no management:

- Do not commit them.
- Do not delete them while the application is running. With it stopped they are
  gone anyway, and removing any leftovers loses nothing.
- Do not copy the `.sqlite3` file alone while the application is live — recent
  commits may still be in the `-wal`, making the copy stale. Use the backup
  command below.

---

## Backups

With the application running:

```bash
cd ~/apps/phantomqa
venv/bin/python -m phantom_qa.manage backup data/backup/phantomqa-$(date +%F).sqlite3
tar czf ~/backups/phantomqa-$(date +%F).tar.gz \
        data/backup/phantomqa-$(date +%F).sqlite3 data/uploads .env
```

Or with it stopped:

```bash
sudo systemctl stop phantomqa
tar czf ~/backups/phantomqa-$(date +%F).tar.gz data/ logs/ .env
sudo systemctl start phantomqa
```

The database alone is not sufficient: `data/uploads/` holds the scans that the
SHA-256 values refer to. Restore both, or integrity checks will report missing
files.

`manage checkpoint` folds the write-ahead log back into the main file if another
tool needs the `.sqlite3` to be complete on its own.

---

## Source file integrity

Every analysis records a SHA-256 hash of the file it was computed from and keeps
a copy of that file under `data/uploads/<analysis-id>.bin`.

### What it detects

| Situation | Without the hash | With it |
|---|---|---|
| Silent corruption — bad disk, bad copy, truncated transfer | Nothing; the report still opens | **Mismatch** |
| A backup restored from the wrong date | Nothing; the file looks plausible | **Mismatch** |
| The wrong scan attached to a phantom or date | Nothing | **Mismatch** |
| The stored file deleted or moved | Nothing | **File missing** |

This is a detection mechanism, not access control. Anyone able to rewrite the
stored file could also rewrite the database row holding the expected hash. Its
value is against accident and drift. For tamper-evidence against a motivated
insider, record the hash somewhere the application cannot write — a signed
e-mail, a ticket, or a printed report in a QA binder. The value printed in every
report serves that purpose.

### Where to find it

- In the **report**, under *Source file integrity*, which re-checks the stored
  file as the report is generated.
- In the application: **Verify source file** in step F, or the **verify** link
  on any History row.
- In the JSON export, as `sha256`.

### Checking it yourself

The stored value is a plain SHA-256 of the file's bytes, so any standard tool
reproduces it:

```powershell
Get-FileHash -Algorithm SHA256 "path\to\scan.dcm"     # Windows PowerShell
```

```bash
sha256sum path/to/scan.dcm                            # Linux / macOS
sha256sum data/uploads/<analysis-id>.bin              # the server's copy
```

### Checking from the command line

```bash
python -m phantom_qa.manage verify              # every stored analysis
python -m phantom_qa.manage verify <analysis-id>
```

The command exits non-zero if anything fails, so it can be scheduled:

```
OK       c81a123830db  10000003
MISMATCH d4df2f8f0132  10000005
         stored   09a846d530ee78f3...
         computed 1b77c0e4a9f1220c...
2/3 verified.
```

Checks made through the web interface are written to `logs/audit.log` as
`event=verify` lines. The CLI prints its results to the terminal instead —
capture its output if the run needs to be kept.

### Responding to a mismatch

1. **Do not delete anything.** The mismatch is evidence.
2. Hash your own copy of the original scan. If it matches the recorded value,
   the server's copy is the damaged one — restore it from backup and re-verify.
3. If neither matches, the original is gone. Treat the results as unverifiable:
   keep the report for reference, note the mismatch, and re-acquire if the
   measurement matters.
4. Check `logs/audit.log` for `event=verify`, `event=upload` and `event=delete`
   lines to establish when the file was last known good and who touched the
   record.

A mismatch does not occur through normal use.

---

## Code updates and stored results

Updating the code does not affect stored analyses, and no scan ever needs
re-uploading.

- `data/` is excluded from version control, so `git pull` cannot alter it.
- The database schema migrates in place on startup: columns are added, existing
  rows are untouched.
- The application deletes an analysis only through the administrator-gated
  delete action.

What can become **stale** is the numbers. Stored results are those produced by
the algorithm and phantom definition current on the day they were computed. This
is deliberate: a QA record should not change silently, particularly one that has
been signed off.

Each analysis records `algo_version` and `pdef_version`. To list those that
predate the current version:

```bash
python -m phantom_qa.manage outdated
```

### Recomputing

Because the original file is kept, results can be recomputed from disk:

```bash
python -m phantom_qa.manage reanalyze --dry-run   # preview; writes nothing
python -m phantom_qa.manage reanalyze             # apply
```

| Mode | Command | Behaviour | Use after |
|---|---|---|---|
| Results (default) | `reanalyze` | Recomputes from the geometry already confirmed. Manual ROI adjustments are preserved | an **algorithm** change |
| Full | `reanalyze --full` | Re-detects the phantom and every ROI, then computes. Manual adjustments are discarded | a **phantom definition** change |

Options:

| Flag | Effect |
|---|---|
| `--dry-run` | Show what would change, including the metrics that move most; write nothing |
| `--include-validated` | Also recompute analyses that have been signed off. Skipped by default, because recomputing changes numbers someone has put their name to |
| `--all` | Include analyses already on the current version |
| `<id> …` | Only the given analyses |

Site, phantom, operator, notes, validation state and approver, comment, baseline
flag, the audit trail and the stored source file are always preserved. The
re-analysis is added to the audit trail and to `logs/audit.log`.

Re-analysis refuses to run when the stored file no longer matches its recorded
SHA-256, since new numbers must not be computed from a file that is not the one
the original results came from. A missing or undecodable file is reported rather
than raised, so one bad record cannot abort a batch.

### Is re-analysis necessary after every update?

No.

| Change | Action |
|---|---|
| Web interface, reports, exports, security | None. Reports are rendered from stored results when opened, so presentation improvements appear immediately |
| A measurement algorithm | Old and new results are not strictly comparable. Recompute the series you intend to trend together, or accept a mixed history knowing `algo_version` records which is which |
| The phantom definition | Recompute with `--full` to apply the improved detection to older scans |

For trending, compare like with like: a series recomputed on one version is
comparable throughout, whereas one spanning an algorithm change is not.

---

## A phantom that differs from the definition

Automatic placement is seeded from `data/phantom_definitions/msf_v1.json`.
Runtime measurement absorbs a few millimetres of variation between phantom
units, but not a different layout. On a substantially different build the
template searches may no longer cover the objects. Reversed line-pair
frequency order is supported, but different group counts or geometry require
a definition for that build.

Symptoms in Stage B:

| Symptom | Meaning |
|---|---|
| Groups detected, but offsets from nominal of tens of millimetres | The strip runs in the opposite frequency order, or the groups sit elsewhere |
| A group's measured frequency far from its nominal | That block is not the group it has been labelled as |
| Fewer or more blocks than the definition expects | This build has a different number of groups |
| Uniformity squares "NOT refined (nominal used)" | The squares were not found; the nominal positions are being used |
| Step D names rulers "excluded from the scale" | Those ruler probes are not on the printed marks; the mm/px scale rests on the remaining sides |
| Step D says the scale rests on fewer than two rulers | The scale could not be cross-checked — dimensions are indicative only |

The dimension and per-test reasons in steps D and E name the specific
measurement that failed and the limit it was compared against; read those first,
since they distinguish a mis-placed ROI from a genuine detector problem.

### Working with it now

Correct the placement by hand in Stage C: drag each ROI onto the right object
and rotate it to match. Every measurement is taken from where you put the ROI,
and the profile line follows the square. Undo, Redo and the two Reset buttons
mean a slip costs a click rather than a re-upload.

**Do it once per phantom, not once per scan.** Confirming Stage C stores the
corrected positions against the analysis's **Phantom** name, and the next scan
of that phantom starts from them. This is what makes a hand-placed series
comparable with itself: the placement is no longer re-done by hand, it is
replayed. Positions are stored in the phantom's own frame, so a scan with the
phantom turned differently on the detector replays correctly; and a layout that
disagrees with the new scan's own detection about where the patterns are — the
signature of a mis-registered scan — is refused rather than applied.

A stored layout is bound to the phantom name and dies with it: deleting or
renaming the last analysis carrying that name deletes the layout. An
administrator can also discard one deliberately (`POST
/api/phantom_profiles/forget`, admin password required — the request body
names the layout under the key `phantom`, and the listing below carries the
same value as both `phantom` and `phantom_key`); `GET
/api/phantom_profiles` lists them with how many analyses still use each.

A stored layout is not a substitute for a calibrated definition. It freezes ROI
positions; it does not change the nominal geometry the software measures
against. For a phantom you will use regularly, do both.

### Calibrating a definition for that build

For a phantom you will use regularly, give it its own definition rather than
correcting every scan:

1. Acquire several scans of that phantom, in at least two or three orientations,
   on the device you will use.
2. Run each one and note the measured geometry reported in Stage B and D — strip
   angle, block centres and frequencies, block count, low-contrast block centre
   and angle, wedge extent, uniformity square centres. Correct the placement by
   hand first (step C) so the values you record come from ROIs that are on the
   right objects; for the low-contrast grid, place the block by its four corners
   rather than nudging eight circles.
3. Copy `msf_v1.json` to a new file, e.g. `msf_v2.json`, bump its `version`, and
   replace the nominal values with the means of your measurements. Record how
   each value was obtained in its `provenance` section, as `msf_v1.json` does.
4. Check the group list matches that build: the number of line-pair groups, and
   their frequencies **in the order they appear along the strip**.
   Also check `tolerances.ruler_linearity_rms_max_mm` (default 0.5 mm) still
   suits the build — it is the limit above which a ruler is judged mis-detected
   and dropped from the scale.
5. Re-run the scans and confirm Stage B labels every pattern correctly.

Loading a definition per phantom is not yet exposed in the interface; the
application reads `msf_v1.json`. Until it is, point the loader at the new file
for that phantom's scans, or keep the definition that matches the phantom you
scan most often.

After changing or replacing a definition, bring stored analyses up to date with
`python -m phantom_qa.manage reanalyze --full` (see *Recomputing* above).

## Logs

Three files under `PHANTOMQA_LOG_DIR` (default `logs/`), all size-rotated:

| File | Contents | Default retention |
|---|---|---|
| `phantomqa.log` | Every request — method, path, status, duration, client, user — and application activity | 10 × 10 MB |
| `errors.log` | Warnings and errors only, with tracebacks | 10 × 10 MB |
| `audit.log` | Sign-ins, uploads, computes, label edits, validation rulings, integrity checks, deletions | 30 × 10 MB |

`audit.log` is written at INFO regardless of `PHANTOMQA_LOG_LEVEL` and kept
longer. Lines carry fixed fields and a JSON tail:

```
2026-08-01 15:19:16Z event=delete outcome=ok user=qa client=10.0.0.4 \
  analysis=499b02d1e219 detail={"site":"Goma Hospital","phantom":"MSF-01",…}
```

```bash
grep 'event=delete'          logs/audit.log   # deletion attempts
grep 'event=validation'      logs/audit.log   # sign-offs and reversals
grep 'outcome=denied'        logs/audit.log   # failed sign-ins and admin attempts
grep 'event=verify.*FAILED'  logs/audit.log   # integrity problems
```

Passwords, tokens and cookies are redacted before anything is written.

**Retention.** Logs contain site names, operator names and client addresses.
Treat them like `data/`: restrict permissions, include them in backups, and set
a retention period. Rotation caps disk use at roughly 100 MB for the application
logs and 300 MB for the audit log, but does not expire by date — use logrotate
if time-based deletion is required.

---

## Administration commands

```
python -m phantom_qa.manage gen-secret            # generate PHANTOMQA_SECRET_KEY
python -m phantom_qa.manage set-password          # hash the user login password
python -m phantom_qa.manage set-admin-password    # hash the administrator password
python -m phantom_qa.manage check                 # validate configuration
python -m phantom_qa.manage verify [<id> | --all] # check source file integrity
python -m phantom_qa.manage backup [<dest>]       # consistent database copy
python -m phantom_qa.manage checkpoint            # fold the WAL into the database
python -m phantom_qa.manage outdated              # analyses predating this version
python -m phantom_qa.manage reanalyze [options]   # recompute stored analyses
```
