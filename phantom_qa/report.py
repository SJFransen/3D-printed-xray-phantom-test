"""Self-contained printable HTML report for a single analysis.

Organised as one section per phantom pattern (geometry, line patterns, low
contrast, uniformity, wedge), each with its own table, chart and pass/fail
state. Charts are rendered server-side with matplotlib and inlined as base64
PNGs, so the report file has no external dependencies (offline/archive safe).
"""

from __future__ import annotations

import base64
import html
import io

import numpy as np

from .store import flatten_results

_STATUS_COLOR = {"pass": "#2e9e44", "warn": "#d9a021", "fail": "#cf3f3f",
                 "n/a": "#7a7a7a", "error": "#cf3f3f"}


def _fig_to_b64(fig) -> str:
    import matplotlib.pyplot as plt
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def _chip(s: str) -> str:
    color = _STATUS_COLOR.get(s, "#7a7a7a")
    return (f'<span class="chip" style="background:{color}">'
            f'{html.escape(str(s or "n/a"))}</span>')


def _num(v, d=3):
    if v is None:
        return "—"
    if isinstance(v, float):
        if not np.isfinite(v):
            return "—"
        return f"{v:,.{d}f}".rstrip("0").rstrip(".") if abs(v) < 1e6 else f"{v:.3g}"
    return html.escape(str(v))


def _delta_cell(value, base):
    if base in (None, 0) or not isinstance(value, (int, float)):
        return "<td class='num'>—</td><td class='num'>—</td>"
    d = 100.0 * (value - base) / abs(base)
    warn = " style='color:#cf3f3f;font-weight:600'" if abs(d) > 20 else ""
    return f"<td class='num'>{_num(base)}</td><td class='num'{warn}>{d:+.1f}%</td>"


def _img(b64: str) -> str:
    return f'<img src="data:image/png;base64,{b64}">'


# --------------------------------------------------------------------- charts

def _chart_wedge(res):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    w = res.get("wedge") or {}
    if not w.get("rows"):
        return None
    steps = [r["step"] for r in w["rows"]]
    means = [r["mean"] for r in w["rows"]]
    fit = w.get("fit") or {}
    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(9, 3.2),
                                  gridspec_kw={"width_ratios": [3, 2]})
    ax.plot(steps, means, "ks-", ms=6, lw=1, label="measured mean")
    if fit:
        xs = np.array([min(steps), max(steps)])
        ax.plot(xs, fit["slope"] * xs + fit["intercept"], "r--",
                label=f"linear fit  R² = {fit['r2']:.4f}")
    ax.set_xlabel("wedge step (S1 = top)")
    ax.set_ylabel("mean pixel value")
    ax.legend(fontsize=8)
    ax.grid(alpha=0.3)
    resid = (fit or {}).get("residuals_pct_of_span")
    if resid:
        ax2.bar(steps, resid, color="#6b8fc4")
        ax2.axhline(0, color="k", lw=0.6)
        ax2.set_xlabel("step")
        ax2.set_ylabel("residual [% of span]")
        ax2.set_title("deviation from linear", fontsize=9)
        ax2.grid(alpha=0.3, axis="y")
    fig.tight_layout()
    return _fig_to_b64(fig)


def _chart_lowcontrast(res, baseline_rows=None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    lc = res.get("lowcontrast") or {}
    if not lc.get("rows"):
        return None
    labels = [r["id"] for r in lc["rows"]]
    cnrs = [r["abs_cnr"] for r in lc["rows"]]
    fig, ax = plt.subplots(figsize=(6, 3.0))
    x = np.arange(len(labels))
    ax.bar(x - (0.2 if baseline_rows else 0), cnrs,
           width=0.4 if baseline_rows else 0.65, color="#4a7dbd", label="this scan")
    if baseline_rows:
        bmap = {r["object"]: r["value"] for r in baseline_rows
                if r["test"] == "lowcontrast" and r["metric"] == "cnr"}
        bl = [abs(bmap.get(l, np.nan)) for l in labels]
        ax.bar(x + 0.2, bl, width=0.4, color="#b9c6d8", label="baseline")
        ax.legend(fontsize=8)
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_xlabel("circle (design order, L1 = weakest)")
    ax.set_ylabel("|CNR|")
    ax.grid(alpha=0.3, axis="y")
    fig.tight_layout()
    return _fig_to_b64(fig)


def _chart_uniformity(res):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    u = res.get("uniformity") or {}
    if not u.get("rows"):
        return None
    labels = [r["id"] for r in u["rows"]]
    d = [r["dsnr_pct"] for r in u["rows"]]
    tol = u.get("tolerance_pct", 20)
    fig, ax = plt.subplots(figsize=(6, 2.8))
    ax.bar(labels, d, color=["#cf3f3f" if abs(v) > tol else "#4a7dbd" for v in d])
    ax.axhline(tol, color="#d9a021", ls="--", lw=1)
    ax.axhline(-tol, color="#d9a021", ls="--", lw=1)
    ax.axhline(0, color="k", lw=0.6)
    ax.set_ylabel("ΔSNR [%]")
    ax.set_xlabel(f"uniformity square (tolerance ±{tol:g}%)")
    ax.grid(alpha=0.3, axis="y")
    fig.tight_layout()
    return _fig_to_b64(fig)


def _chart_linepairs(res):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    lp = res.get("linepairs") or {}
    rows = [r for r in lp.get("rows", [])
            if (r.get("linearity") or {}).get("profile")]
    if not rows:
        return None
    fig, axes = plt.subplots(len(rows), 2, figsize=(10, 1.85 * len(rows)),
                             gridspec_kw={"width_ratios": [2, 1]}, squeeze=False)
    for i, r in enumerate(rows):
        lin = r["linearity"]
        prof = lin["profile"]
        ax = axes[i][0]
        ax.plot(prof["pos_mm"], prof["value"], lw=0.7, color="#333")
        for p in lin.get("grid_peaks_mm", []):
            ax.axvline(p, color="#d95050", lw=0.5, alpha=0.55)
        ax.set_ylabel(f"{r['id']}\n({r['freq_lp_mm']} lp/mm)", fontsize=8)
        ax.tick_params(labelsize=7)
        if i == 0:
            ax.set_title("intensity profile across the group "
                         "(red = fitted line grid)", fontsize=9)
        if i == len(rows) - 1:
            ax.set_xlabel("position [mm]", fontsize=8)
        ax2 = axes[i][1]
        resid = lin.get("grid_residuals_mm")
        if resid:
            ax2.stem(np.arange(len(resid)), np.array(resid) * 1000, basefmt=" ")
            ax2.axhline(0, color="k", lw=0.5)
            pd = lin.get("pitch_dev_pct")
            ax2.set_title(f"pitch {lin.get('measured_pitch_mm'):.4f} mm "
                          f"({pd:+.2f}% vs nominal)", fontsize=8)
        ax2.set_ylabel("resid [µm]", fontsize=7)
        ax2.tick_params(labelsize=7)
        if i == len(rows) - 1:
            ax2.set_xlabel("line index", fontsize=8)
    fig.tight_layout()
    return _fig_to_b64(fig)


# -------------------------------------------------------------------- sections

def _section(title, status, body, note=""):
    return f"""
<section class="card">
  <h2>{html.escape(title)} {_chip(status)}</h2>
  {f'<p class="note">{note}</p>' if note else ''}
  {body}
</section>"""


def _geometry_section(res, bmap):
    g = res.get("geometry") or {}
    if not g:
        return ""
    d = g.get("dimensions") or {}
    seps = g.get("central_line_separations") or {}
    sc = g.get("scale") or {}
    spac = sc.get("dicom_spacings_mm_per_px") or {}

    def row(label, val, base_key=None, unit="mm", dec=2):
        b = bmap.get(base_key) if base_key else None
        return (f"<tr><td>{label}</td><td class='num'>{_num(val, dec)}</td>"
                f"<td>{unit}</td>{_delta_cell(val, b)}</tr>")

    dims = "".join([
        row("side — top", d.get("side_top_mm"), ("geometry", "corners", "side_top")),
        row("side — right", d.get("side_right_mm"), ("geometry", "corners", "side_right")),
        row("side — bottom", d.get("side_bottom_mm"), ("geometry", "corners", "side_bottom")),
        row("side — left", d.get("side_left_mm"), ("geometry", "corners", "side_left")),
        row("diagonal TL–BR", d.get("diag_tlbr_mm"), ("geometry", "corners", "diag_tlbr")),
        row("diagonal TR–BL", d.get("diag_trbl_mm"), ("geometry", "corners", "diag_trbl")),
        row("<b>mean side</b>", d.get("mean_side_mm"), ("geometry", "corners", "mean_side")),
        row("deviation from nominal", d.get("dev_from_nominal_pct"),
            ("geometry", "corners", "dev_from_nominal"), "%"),
    ])
    rulers = "".join(
        f"<tr><td>{side}</td><td class='num'>{_num(r.get('pitch_mm'), 4)}</td>"
        f"<td class='num'>{_num(r.get('pitch_dev_pct'), 2)}%</td>"
        f"<td class='num'>{_num(r.get('linearity_rms_mm'), 3)}</td>"
        f"<td class='num'>{_num(r.get('central_line_from_edge_mm'), 2)}</td></tr>"
        if r.get("detected") else
        f"<tr><td>{side}</td><td colspan='4' class='muted'>not detected</td></tr>"
        for side, r in (g.get("rulers") or {}).items())
    fields = "".join(
        (f"<tr><td>{side}</td><td class='num'>"
         f"{_num(f.get('deviation_from_central_line_mm'), 1)}</td>"
         f"<td class='num'>{_num(f.get('pct_of_sid'), 2)}%</td>"
         f"<td>{_chip(f.get('status'))}</td></tr>") if f.get("detected") else
        (f"<tr><td>{side}</td><td colspan='3' class='muted'>not measurable — "
         f"{html.escape(str(f.get('reason', '')))}</td></tr>")
        for side, f in (g.get("field_alignment") or {}).items())

    body = f"""
<h3>Corner-mark dimensions</h3>
<table><tr><th>dimension</th><th>value</th><th>unit</th><th>baseline</th><th>Δ</th></tr>
{dims}</table>
<h3>Side-mark rulers (5 mm pitch)</h3>
<table><tr><th>side</th><th>pitch [mm]</th><th>Δ pitch</th>
<th>mark linearity RMS [mm]</th><th>central line from edge [mm]</th></tr>{rulers}</table>
<h3>Central-line separations &amp; scale cross-check</h3>
<table>
<tr><td>vertical separation</td><td class='num'>{_num(seps.get('vertical_mm'))}</td><td>mm</td></tr>
<tr><td>horizontal separation</td><td class='num'>{_num(seps.get('horizontal_mm'))}</td><td>mm</td></tr>
<tr><td>nominal separation</td><td class='num'>{_num(seps.get('nominal_mm'), 1)}</td><td>mm</td></tr>
<tr><td>tape pitch measured</td><td class='num'>{_num(sc.get('pitch_measured_mm'), 4)}</td><td>mm (nominal 5.0000)</td></tr>
<tr><td>absolute scale (pitch-anchored)</td><td class='num'>{_num(sc.get('absolute_mm_per_px'), 5)}</td><td>mm/px</td></tr>
<tr><td>DICOM ImagerPixelSpacing</td><td class='num'>{_num(spac.get('ImagerPixelSpacing'), 5)}</td><td>mm/px</td></tr>
<tr><td>DICOM PixelSpacing</td><td class='num'>{_num(spac.get('PixelSpacing'), 5)}</td><td>mm/px</td></tr>
<tr><td>implied magnification vs detector plane</td>
    <td class='num'>{_num(sc.get('implied_magnification_vs_detector_plane'), 4)}</td><td>×</td></tr>
</table>
<h3>X-ray field vs central long lines {_chip(g.get('field_status'))}</h3>
<table><tr><th>side</th><th>deviation [mm]</th><th>% of SID</th><th></th></tr>{fields}</table>"""
    return _section("Geometry & dimensions", g.get("dimension_status"), body)


def _not_analysed(title: str, res: dict, key: str) -> str:
    """A section for a test that produced no rows at all.

    Returning "" instead — which is what this replaces — silently dropped the
    test from the report, so a scan where a pattern was never measured read
    exactly like one where it passed. On a document somebody signs, an absent
    answer has to look absent."""
    t = res.get(key) or {}
    why = t.get("error") or "no result was produced for this test"
    return _section(
        title, t.get("status") or "n/a",
        f'<p class="warnbox">This test could not be analysed on this scan: '
        f'{html.escape(str(why))}</p>')


def _linepairs_section(res, bmap):
    lp = res.get("linepairs") or {}
    if not lp.get("rows"):
        return _not_analysed("Line patterns", res, "linepairs")
    rows = ""
    for r in lp["rows"]:
        lin = r.get("linearity") or {}
        b_sd = bmap.get(("linepairs", r["id"], "sd"))
        rows += (
            f"<tr><td>{r['id']}</td><td class='num'>{_num(r['freq_lp_mm'], 2)}</td>"
            f"<td class='num'>{_num(r['std'], 1)}</td>"
            f"<td class='num'>{_num(lin.get('measured_pitch_mm'), 4)}</td>"
            f"<td class='num'>{_num(lin.get('pitch_dev_pct'), 2)}%</td>"
            f"<td class='num'>{_num((lin.get('residual_rms_mm') or 0) * 1000, 1)}</td>"
            f"<td class='num'>{_num(lin.get('used_lines'))}</td>"
            f"{_delta_cell(r['std'], b_sd)}<td>{_chip(r.get('status'))}</td></tr>")
    chart = _chart_linepairs(res)
    body = f"""
<table><tr><th>group</th><th>nominal [lp/mm]</th><th>SD (guide metric)</th>
<th>measured pitch [mm]</th><th>Δ pitch</th><th>grid residual RMS [µm]</th>
<th>lines used</th><th>baseline SD</th><th>Δ</th><th></th></tr>{rows}</table>
{_img(chart) if chart else ''}"""
    note = ("Block positions and orientation are measured in this scan, not taken "
            "from stored coordinates. SD is the guide's constancy metric; the "
            "pitch/residual columns are the line-pattern linearity.")
    return _section("Line patterns (spatial resolution)", lp.get("status"), body, note)


def _lowcontrast_section(res, bmap, baseline_rows):
    lc = res.get("lowcontrast") or {}
    if not lc.get("rows"):
        return _not_analysed("Low contrast", res, "lowcontrast")
    rows = ""
    for r in lc["rows"]:
        b = bmap.get(("lowcontrast", r["id"], "cnr"))
        rows += (
            f"<tr><td>{r['id']}</td><td class='num'>{r['level']}</td>"
            f"<td class='num'>{_num(r['cnr'], 3)}</td>"
            f"<td class='num'>{_num(r['obj_mean'], 1)}</td>"
            f"<td class='num'>{_num(r['obj_std'], 1)}</td>"
            f"<td class='num'>{_num(r['bg_mean'], 1)}</td>"
            f"<td class='num'>{_num(r['bg_std'], 1)}</td>"
            f"{_delta_cell(r['cnr'], b)}</tr>")
    chart = _chart_lowcontrast(res, baseline_rows)
    body = f"""
<table><tr><th>circle</th><th>level</th><th>CNR</th><th>μ object</th><th>σ object</th>
<th>μ background</th><th>σ background</th><th>baseline CNR</th><th>Δ</th></tr>{rows}</table>
{_img(chart) if chart else ''}"""
    note = ("CNR = (μ<sub>obj</sub> − μ<sub>bg</sub>) / √(σ<sub>obj</sub>² + σ<sub>bg</sub>²). "
            "Background is the ring immediately around each circle, so every "
            "value is local to its own object. Circles carry positional design "
            "order L1…L8 — no nominal contrast percentages are assumed.")
    return _section("Low contrast", lc.get("status"), body, note)


def _uniformity_section(res, bmap):
    u = res.get("uniformity") or {}
    if not u.get("rows"):
        return _not_analysed("Uniformity", res, "uniformity")
    rows = ""
    for r in u["rows"]:
        b = bmap.get(("uniformity", r["id"], "snr"))
        rows += (f"<tr><td>{r['id']}</td><td class='num'>{_num(r['mean'], 1)}</td>"
                 f"<td class='num'>{_num(r['std'], 2)}</td>"
                 f"<td class='num'>{_num(r['snr'], 1)}</td>"
                 f"<td class='num'>{_num(r['dsnr_pct'], 2)}%</td>"
                 f"<td class='num'>{_num(r['dmean_pct'], 2)}%</td>"
                 f"{_delta_cell(r['snr'], b)}<td>{_chip(r.get('status'))}</td></tr>")
    chart = _chart_uniformity(res)
    body = f"""
<table><tr><th>square</th><th>μ</th><th>σ</th><th>SNR</th><th>ΔSNR</th>
<th>Δμ</th><th>baseline SNR</th><th>Δ</th><th></th></tr>{rows}</table>
<p class="note">Average SNR {_num(u.get('snr_avg'), 1)} · worst |ΔSNR|
{_num(u.get('max_abs_dsnr_pct'), 2)}% · tolerance ±{_num(u.get('tolerance_pct'), 0)}%</p>
{_img(chart) if chart else ''}"""
    return _section("Uniformity", u.get("status"), body)


def _wedge_section(res, bmap):
    w = res.get("wedge") or {}
    if not w.get("rows"):
        return _not_analysed("Wedge", res, "wedge")
    resid = (w.get("fit") or {}).get("residuals_pct_of_span") or []
    rows = ""
    for i, r in enumerate(w["rows"]):
        b = bmap.get(("wedge", f"S{r['step']}", "mean"))
        saturation_badge = (
            '<span class="chip" style="background:#cf3f3f">saturated</span>'
            if r.get("saturated") else ""
        )
        rows += (f"<tr><td>S{r['step']}</td><td class='num'>{_num(r['mean'], 1)}</td>"
                 f"<td class='num'>{_num(r['std'], 1)}</td>"
                 f"<td class='num'>{_num(resid[i] if i < len(resid) else None, 1)}%</td>"
                 f"{_delta_cell(r['mean'], b)}"
                 f"<td>{saturation_badge}</td></tr>")
    chart = _chart_wedge(res)
    reasons = w.get("reasons") or []
    body = f"""
<table>
<tr><td>monotonic response</td><td class='num'>{w.get('monotonic')}</td></tr>
<tr><td>dynamic-range ratio (S1 / S7)</td><td class='num'>{_num(w.get('dynamic_range_ratio'), 1)}×</td></tr>
<tr><td>linear-fit R² (shape descriptor)</td><td class='num'>{_num((w.get('fit') or {}).get('r2'), 4)}
 (warn below {_num(w.get('r2_min'), 2)})</td></tr>
</table>
<table><tr><th>step</th><th>mean</th><th>σ</th><th>residual vs linear</th>
<th>baseline mean</th><th>Δ</th><th></th></tr>{rows}</table>
{_img(chart) if chart else ''}
{'<p class="note">' + html.escape('; '.join(reasons)) + '</p>' if reasons else ''}"""
    note = ("Steps are labelled by position (S1 = top). The printed steps are not "
            "equal attenuation increments, so the response is inherently non-linear; "
            "pass/fail is decided by monotonicity and saturation, with R² kept as a "
            "shape descriptor only.")
    return _section("Attenuation wedge (dynamic range)", w.get("status"), body, note)


# ---------------------------------------------------------------------- report

_VALIDATION_COLOR = {"validated": "#2e9e44",
                     "conditionally_validated": "#d9a021",
                     "not_validated": "#cf3f3f",
                     "": "#7a7a7a"}
_VALIDATION_TEXT = {"validated": "VALIDATED",
                    "conditionally_validated": "CONDITIONALLY VALIDATED",
                    "not_validated": "NOT VALIDATED",
                    "": "PENDING REVIEW"}


def _validation_block(record: dict) -> str:
    """The administrator's ruling — the first thing a reader needs to know."""
    st = record.get("validation_status") or ""
    color = _VALIDATION_COLOR.get(st, "#7a7a7a")
    who = record.get("validated_by") or ""
    when = (record.get("validated_at") or "")[:16]
    comment = record.get("validation_comment") or ""
    if st:
        meta = (f"<div class='vmeta'>Approved by <b>{html.escape(who)}</b>"
                f" on {html.escape(when)}</div>")
    else:
        meta = ("<div class='vmeta'>No administrator has ruled on this analysis "
                "yet. The measurements below stand on their own; they have not "
                "been signed off.</div>")
    body = (f"<div class='vcomment'><b>Comment:</b> {html.escape(comment)}</div>"
            if comment else "")
    return f"""
<section class="card vcard" style="border-left:6px solid {color}">
  <h2>Validation</h2>
  <div class="vstate" style="color:{color}">{_VALIDATION_TEXT.get(st, st)}</div>
  {meta}
  {body}
</section>"""


def _identity_block(record: dict) -> str:
    from .store import acquisition_flag
    flag = acquisition_flag(record)
    acquired = (record.get("acquired_at") or "")[:16]
    if flag == "missing":
        acquired = "not recorded by the scanner"
    elif flag == "implausible":
        acquired += " (implausible — check the scanner clock)"
    rows = [("Site", record.get("site")),
            ("Phantom", record.get("phantom")),
            ("Operator", record.get("operator")),
            # Both dates: a scanner clock that was never set, or was reset,
            # makes the acquisition date unusable for tracing a scan, and the
            # upload date is then the only reliable ordering.
            ("Acquired", acquired),
            ("Uploaded", (record.get("created_at") or "")[:16]),
            ("Notes", record.get("notes"))]
    cells = "".join(
        f'<div class="idcell"><span class="idk">{html.escape(k)}</span>'
        f'<span class="idv">{html.escape(str(v)) if v else "—"}</span></div>'
        for k, v in rows)
    warn = ""
    if not record.get("site") and not record.get("phantom"):
        warn = ('<p class="warnbox">⚠ No site or phantom recorded — this '
                'analysis will not appear in grouped trends.</p>')
    return f'<section class="card"><h2>Identification</h2>' \
           f'<div class="idgrid">{cells}</div>{warn}</section>'


def _integrity_block(record: dict, integrity: dict | None) -> str:
    """SHA-256 of the analysed file, and what to do with it."""
    sha = record.get("sha256", "")
    if integrity:
        st = integrity.get("status")
        if st == "ok":
            verdict = (f'<span class="chip" style="background:{_STATUS_COLOR["pass"]}">'
                       f'verified</span> the stored source file still hashes to '
                       f'this value')
        elif st == "missing_file":
            verdict = (f'<span class="chip" style="background:{_STATUS_COLOR["warn"]}">'
                       f'file missing</span> the stored source file is no longer '
                       f'on the server; the hash below cannot be re-checked')
        else:
            verdict = (f'<span class="chip" style="background:{_STATUS_COLOR["fail"]}">'
                       f'MISMATCH</span> the stored file no longer matches — do '
                       f'not rely on these results')
        computed = integrity.get("computed_sha256")
        extra = (f"<tr><td>recomputed now</td><td><code>{html.escape(computed)}</code>"
                 f"</td></tr>" if computed and computed != sha else "")
    else:
        verdict = "not checked when this report was generated"
        extra = ""
    return f"""
<section class="card"><h2>Source file integrity</h2>
<table>
<tr><td>source file</td><td>{html.escape(record.get('source_name', ''))}</td></tr>
<tr><td>SHA-256 recorded at analysis</td><td><code>{html.escape(sha)}</code></td></tr>
{extra}
<tr><td>status</td><td>{verdict}</td></tr>
</table>
<p class="note"><b>What this is for.</b> The SHA-256 is a fingerprint of the exact
file that produced the numbers in this report. It ties the results to their
input: if the file is later corrupted, restored from the wrong backup, or
swapped, the fingerprint changes and the link is broken.</p>
<p class="note"><b>How to check it yourself.</b> Hash your copy of the original
file and compare the value with the one above — they must match character for
character:</p>
<pre class="cmd">Windows PowerShell:  Get-FileHash -Algorithm SHA256 "&lt;file&gt;"
Linux / macOS:       sha256sum "&lt;file&gt;"</pre>
<p class="note">Inside the app, <i>Verify source file</i> on the analysis (or
<code>python -m phantom_qa.manage verify --all</code>) re-hashes the copy the
server kept and reports any mismatch. Every check is written to the audit log.</p>
</section>"""


def build_report(record: dict, overlay_png: bytes | None = None,
                 baseline: dict | None = None,
                 integrity: dict | None = None) -> str:
    results = record.get("results") or {}
    meta = record.get("meta") or {}
    reg = (results.get("_meta") or {}).get("registration") or {}

    baseline_rows = None
    bmap = {}
    if baseline and baseline.get("results"):
        baseline_rows = flatten_results(baseline["results"])
        for row in baseline_rows:
            bmap[(row["test"], row["object"], row["metric"])] = row["value"]

    statuses = [
        ("Geometry / dimensions", (results.get("geometry") or {}).get("dimension_status", "n/a")),
        ("Field alignment", (results.get("geometry") or {}).get("field_status", "n/a")),
        ("Line patterns", (results.get("linepairs") or {}).get("status", "n/a")),
        ("Low contrast", (results.get("lowcontrast") or {}).get("status", "n/a")),
        ("Uniformity", (results.get("uniformity") or {}).get("status", "n/a")),
        ("Wedge", (results.get("wedge") or {}).get("status", "n/a")),
    ]
    summary = "".join(
        f'<div class="sumcell"><span>{html.escape(k)}</span>{_chip(v)}</div>'
        for k, v in statuses)

    sections = (
        _geometry_section(results, bmap)
        + _linepairs_section(results, bmap)
        + _lowcontrast_section(results, bmap, baseline_rows)
        + _uniformity_section(results, bmap)
        + _wedge_section(results, bmap)
    )

    overlay_html = ""
    if overlay_png:
        b64 = base64.b64encode(overlay_png).decode()
        overlay_html = _section(
            "Confirmed geometry overlay", "",
            f'<img src="data:image/png;base64,{b64}">')

    audit_html = "".join(
        f"<tr><td>{html.escape(a['ts'])}</td><td>{html.escape(a['stage'])}</td>"
        f"<td>{html.escape(a['action'])}</td>"
        f"<td>{html.escape(str(a.get('detail') or ''))}</td></tr>"
        for a in (record.get("audit") or []))

    meta_rows = "".join(
        f"<tr><td>{html.escape(str(k))}</td><td>{html.escape(str(meta[k]))}</td></tr>"
        for k in ("Manufacturer", "ManufacturerModelName", "StationName",
                  "StudyDate", "KVP", "ExposureInuAs", "ExposureTime",
                  "ImagerPixelSpacing", "PixelSpacing",
                  "AcquisitionDeviceProcessingDescription", "TransferSyntax")
        if k in meta)

    reduced = ('<p class="warnbox">⚠ REDUCED-PRECISION MODE: plain-image input '
               '(no DICOM metadata, lossy 8-bit data). Excluded from baselines '
               'by default.</p>') if record.get("reduced_precision") else ""

    rms = reg.get("residual_rms_mm")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Phantom QA report {html.escape(record['id'])}</title>
<style>
 body {{ font-family: "Segoe UI", system-ui, Arial, sans-serif; margin: 0;
        background:#f4f6f8; color:#1d2530; }}
 .wrap {{ max-width: 1080px; margin: 0 auto; padding: 24px; }}
 h1 {{ font-size: 22px; margin: 0 0 4px; }}
 h2 {{ font-size: 16px; margin: 0 0 10px; display:flex; gap:10px;
       align-items:center; }}
 h3 {{ font-size: 13px; margin: 16px 0 4px; color:#41506a;
       text-transform: uppercase; letter-spacing: .04em; }}
 .card {{ background:#fff; border:1px solid #dde3ea; border-radius:10px;
          padding:16px 18px; margin: 16px 0; }}
 table {{ border-collapse: collapse; margin: 6px 0 10px; width: 100%; }}
 td, th {{ border: 1px solid #dfe4ea; padding: 4px 9px; font-size: 12px;
           text-align: left; }}
 th {{ background: #eef2f6; font-weight: 600; }}
 td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
 td.muted, .muted {{ color:#8994a4; }}
 .chip {{ color:#fff; padding:1px 9px; border-radius:9px; font-size:11px;
          font-weight:600; }}
 .note {{ font-size:11.5px; color:#5b6b80; margin:4px 0 8px; }}
 .warnbox {{ background:#fff5d6; border:1px solid #d9a021; padding:8px 12px;
             border-radius:6px; }}
 .summary {{ display:flex; flex-wrap:wrap; gap:8px; }}
 .sumcell {{ background:#fff; border:1px solid #dde3ea; border-radius:8px;
             padding:8px 12px; display:flex; gap:10px; align-items:center;
             font-size:12.5px; }}
 img {{ max-width:100%; height:auto; display:block; margin:8px 0; }}
 code {{ font-size:10.5px; word-break:break-all; }}
 pre.cmd {{ background:#f2f5f8; border:1px solid #dfe4ea; border-radius:6px;
            padding:8px 10px; font-size:11px; overflow-x:auto; }}
 .idgrid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
            gap:8px; }}
 .idcell {{ background:#f2f5f8; border-radius:8px; padding:8px 10px; }}
 .idk {{ display:block; font-size:10px; color:#5b6b80; text-transform:uppercase;
         letter-spacing:.05em; }}
 .idv {{ font-size:13.5px; font-weight:600; }}
 .vstate {{ font-size:20px; font-weight:700; letter-spacing:.03em; }}
 .vmeta {{ font-size:12.5px; margin-top:4px; }}
 .vcomment {{ font-size:12.5px; margin-top:8px; background:#f2f5f8;
              border-radius:6px; padding:8px 10px; }}
 @media print {{ body {{ background:#fff; }} .card {{ break-inside: avoid; }} }}
</style></head><body><div class="wrap">
<h1>MSF Phantom QA report</h1>
<p class="muted" style="font-size:12px">
<b>Analysis</b> {html.escape(record['id'])} ·
<b>Uploaded</b> {html.escape(record['created_at'])} ·
<b>Source</b> {html.escape(record['source_name'])} ·
<b>SID</b> {record.get('sid_mm') or 1000.0} mm ·
<b>Algorithm</b> v{html.escape(record.get('algo_version') or '')}<br>
<b>Protocol signature</b> {html.escape(record.get('signature') or '')}</p>
{reduced}
{_validation_block(record)}
{_identity_block(record)}
<div class="summary">{summary}</div>
<section class="card"><h2>Registration</h2>
<table>
<tr><td>rotation</td><td class='num'>{_num(reg.get('rotation_deg'), 2)}°</td></tr>
<tr><td>mirrored</td><td class='num'>{reg.get('mirrored')}</td></tr>
<tr><td>scale</td><td class='num'>{_num(reg.get('mm_per_px'), 5)} mm/px</td></tr>
<tr><td>landmark residual RMS</td><td class='num'>{_num(rms, 3)} mm</td></tr>
</table></section>
{sections}
{overlay_html}
<section class="card"><h2>Acquisition metadata</h2>
<table>{meta_rows}</table></section>
{_integrity_block(record, integrity)}
<section class="card"><h2>Audit trail</h2>
<table><tr><th>time</th><th>stage</th><th>action</th><th>detail</th></tr>
{audit_html}</table></section>
</div></body></html>"""
