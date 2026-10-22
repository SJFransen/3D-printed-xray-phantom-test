"""Headless command-line analysis (fully automatic, no verification gates).

Intended for validation and batch work — the gated web wizard is the primary
workflow. Results are still marked with the algorithm version and the automatic
geometry is saved alongside so they remain traceable.

Usage:
  python -m phantom_qa.cli <dicom-or-zip-or-image> [--out DIR] [--sid 1000]
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys

from . import ingest, pipeline
from .phantom_def import load_default


def main(argv=None):
    ap = argparse.ArgumentParser(description="MSF phantom QA — headless analysis")
    ap.add_argument("path", help="DICOM file, zipped CD export, or image")
    ap.add_argument("--out", default="qa_output", help="output directory")
    ap.add_argument("--sid", type=float, default=1000.0, help="SID in mm")
    args = ap.parse_args(argv)
    if not math.isfinite(args.sid) or args.sid <= 0:
        ap.error("--sid must be a finite positive distance in mm")

    pdef = load_default()
    scans = ingest.load_path(args.path)
    os.makedirs(args.out, exist_ok=True)

    for i, scan in enumerate(scans):
        tag = f"scan{i}" if len(scans) > 1 else "scan"
        print(f"--- {scan.source_name} ({scan.kind}"
              f"{', REDUCED PRECISION' if scan.reduced_precision else ''})")
        reg = pipeline.run_stage_a(scan, pdef)
        print(f"registration: rotation {reg.transform.rotation_deg:.2f} deg, "
              f"mirrored {reg.transform.mirrored}, "
              f"{reg.transform.mm_per_px:.5f} mm/px, "
              f"landmark RMS {reg.residual_rms_mm:.3f} mm")
        ctx = pipeline.build_ctx(scan, pdef, reg,
                                 {"scan_meta": scan.meta, "sid_mm": args.sid})
        geom = pipeline.propose_all(ctx)
        results = pipeline.compute_all(ctx, geom)
        overall = pipeline.overall_status(results)
        print(f"overall status: {overall}")

        base = os.path.join(args.out, tag)
        with open(base + "_results.json", "w", encoding="utf-8") as f:
            json.dump({"meta": scan.meta, "sha256": scan.sha256,
                       "geometry": geom, "results": results,
                       "overall": overall}, f, indent=1)
        with open(base + "_overlay.png", "wb") as f:
            f.write(pipeline.render_overlay(scan, ctx, geom))
        print(f"written: {base}_results.json, {base}_overlay.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
