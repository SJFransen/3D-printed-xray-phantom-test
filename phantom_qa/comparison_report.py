"""Visual comparison report across several analyses.

Works for two different questions with the same layout:

  * one phantom over time      -> the entities happen to be dates
  * many phantoms side by side -> the entities are different phantoms/sites

Because "many phantoms" has no meaningful first entry, every deviation is
measured against the **median of the selection**, not against the first scan.
The output is plots rather than tables — the numeric table is kept as a
collapsed appendix. Every visual identifies entries by Site / Phantom ID.
"""

from __future__ import annotations

import base64
import contextvars
import re
import secrets
from concurrent.futures import ThreadPoolExecutor

from . import thumbnails
from .report_controls import controls, print_css
import html
import io

import numpy as np

from .store import flatten_results

_STATUS_COLOR = {"pass": "#2e9e44", "warn": "#d9a021", "fail": "#cf3f3f",
                 "n/a": "#9aa4b2", "error": "#cf3f3f", "": "#9aa4b2"}

_VALIDATION_COLOR = {"validated": "#2e9e44",
                     "conditionally_validated": "#d9a021",
                     "not_validated": "#cf3f3f",
                     "": "#9aa4b2"}
_VALIDATION_SHORT = {"validated": "validated",
                     "conditionally_validated": "conditional",
                     "not_validated": "NOT validated",
                     "": "pending"}

_SECTION_TITLES = {
    "geometry": "Geometry & dimensions",
    "alignment": "X-ray field alignment",
    "linepairs": "Line patterns (spatial resolution)",
    "lowcontrast": "Low contrast",
    "uniformity": "Uniformity",
    "wedge": "Attenuation wedge (dynamic range)",
}
_SECTION_ORDER = ["linepairs", "lowcontrast", "uniformity", "wedge",
                  "geometry", "alignment"]

# Metrics worth a dedicated per-object comparison panel.
_PANELS = {
    "linepairs": [("sd", "SD inside the group ROI", ""),
                  ("pitch_dev", "Pitch deviation vs nominal", "%")],
    "lowcontrast": [("cnr", "CNR per circle", "")],
    "uniformity": [("snr", "SNR per square", ""),
                   ("dsnr", "ΔSNR vs the scan's own mean", "%")],
    "wedge": [("mean", "Mean value per step", "")],
    "geometry": [("pitch", "Ruler pitch per side", "mm"),
                 ("linearity_rms", "Ruler mark linearity RMS", "mm")],
}


def _b64(fig) -> str:
    import matplotlib.pyplot as plt
    buf = io.BytesIO()
    # Draw once without costly deflate; bounded workers compress while the
    # calling thread draws the next chart. Pixel content stays identical.
    fig.savefig(buf, format="png", dpi=115, bbox_inches="tight",
                pil_kwargs={"compress_level": 0})
    plt.close(fig)
    batch = _CHART_BATCH.get()
    return batch.submit(buf.getvalue()) if batch else _encode_chart(buf.getvalue())


def _img(b64, cls="") -> str:
    return f'<img class="{cls}" src="data:image/png;base64,{b64}">' if b64 else ""


def _mpl():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


# ------------------------------------------------------------------- entities

def _entity_labels(records) -> list[str]:
    """Short unique label per analysis, always carrying Site / Phantom.

    Duplicates (the same phantom measured repeatedly) are disambiguated with
    the acquisition date, and only then with a sequence number."""
    base = []
    for r in records:
        site = (r.get("site") or "").strip()
        ph = (r.get("phantom") or "").strip()
        if site and ph:
            base.append(f"{site} / {ph}")
        elif ph:
            base.append(ph)
        elif site:
            base.append(site)
        else:
            base.append(f"(unlabelled {r['id'][:6]})")
    counts = {}
    for b in base:
        counts[b] = counts.get(b, 0) + 1
    out, used = [], {}
    for b, r in zip(base, records):
        if counts[b] == 1:
            out.append(b)
            continue
        date = (r.get("acquired_at") or r.get("created_at") or "")[:10]
        cand = f"{b}\n{date}"
        used[cand] = used.get(cand, 0) + 1
        if used[cand] > 1:
            cand = f"{b}\n{date} #{used[cand]}"
        out.append(cand)
    return out


def _order(records):
    """Group the same phantom together; within a phantom, chronological.
    That is time order for a single-phantom series and a sensible grouping
    for a multi-phantom comparison."""
    return sorted([r for r in records if r.get("results")],
                  key=lambda r: ((r.get("site") or "~").lower(),
                                 (r.get("phantom") or "~").lower(),
                                 r.get("acquired_at") or r.get("created_at")))


def _collect(records):
    ordered = _order(records)
    keys, seen, maps = [], set(), []
    for rec in ordered:
        v, u, s = {}, {}, {}
        for row in flatten_results(rec["results"]):
            k = (row["test"], row["object"], row["metric"])
            if k not in seen:
                seen.add(k)
                keys.append(k)
            v[k] = row["value"]
            u[k] = row["unit"]
            s[k] = row["status"]
        maps.append({"v": v, "u": u, "s": s})
    return ordered, keys, maps


def _site_colors(ordered):
    """One colour per site so multi-site selections are readable at a glance."""
    plt = _mpl()
    sites = sorted({(r.get("site") or "(none)") for r in ordered})
    cmap = plt.get_cmap("tab10" if len(sites) <= 10 else "tab20")
    return {s: cmap(i % cmap.N) for i, s in enumerate(sites)}, sites


# ---------------------------------------------------------------------- plots

def _fig_width(n, per=0.62, base=4.2, lo=7.0, hi=18.0):
    return float(np.clip(base + per * n, lo, hi))


def _status_grid(ordered, labels):
    plt = _mpl()
    tests = [("geometry", "dimension_status", "Geometry"),
             ("geometry", "field_status", "Field alignment"),
             ("linepairs", "status", "Line patterns"),
             ("lowcontrast", "status", "Low contrast"),
             ("uniformity", "status", "Uniformity"),
             ("wedge", "status", "Wedge")]
    n = len(ordered)
    nrows = len(tests) + 1                     # + the validation row
    fig, ax = plt.subplots(figsize=(_fig_width(n), 0.42 * nrows + 2.2))
    for row, (test, key, label) in enumerate(tests):
        for col, rec in enumerate(ordered):
            st = ((rec.get("results") or {}).get(test) or {}).get(key, "n/a")
            ax.add_patch(plt.Rectangle((col - 0.46, row - 0.42), 0.92, 0.84,
                                       color=_STATUS_COLOR.get(st, "#9aa4b2")))
    # the administrator's ruling, separated by a gap so it reads as a verdict
    vrow = len(tests) + 0.35
    for col, rec in enumerate(ordered):
        v = rec.get("validation_status") or ""
        ax.add_patch(plt.Rectangle((col - 0.46, vrow - 0.42), 0.92, 0.84,
                                   color=_VALIDATION_COLOR.get(v, "#9aa4b2")))
    ax.set_xlim(-0.6, n - 0.4)
    ax.set_ylim(-0.6, vrow + 0.5)
    ax.set_yticks(list(range(len(tests))) + [vrow])
    ax.set_yticklabels([t[2] for t in tests] + ["VALIDATION"], fontsize=8)
    for lbl in ax.get_yticklabels()[-1:]:
        lbl.set_fontweight("bold")
    ax.set_xticks(range(n))
    ax.set_xticklabels(labels, rotation=40, ha="right", fontsize=7)
    ax.invert_yaxis()
    for s in ax.spines.values():
        s.set_visible(False)
    ax.tick_params(length=0)
    handles = [plt.Rectangle((0, 0), 1, 1, color=_STATUS_COLOR[k])
               for k in ("pass", "warn", "fail", "n/a")]
    ax.legend(handles,
              ["pass / validated", "warn / conditional", "fail / not validated",
               "n/a / pending"],
              ncol=4, fontsize=7, loc="upper center",
              bbox_to_anchor=(0.5, -0.42), frameon=False)
    fig.tight_layout()
    return _b64(fig)


def _row_delta(vals, unit):
    """Deviation of each value from the row median, in a unit that stays finite.

    A metric that is ALREADY a percentage (pitch deviation, ΔSNR, …) has a
    median close to zero, so a relative deviation would explode — those are
    reported in percentage points instead."""
    med = np.nanmedian(vals)
    if not np.isfinite(med):
        return None, None
    if unit == "%":
        return vals - med, "pp"
    if abs(med) < 1e-12:
        return None, None
    return 100.0 * (vals - med) / abs(med), "%"


def _deviation_heatmap(ordered, labels, keys, maps, test):
    """Metric rows x analysis columns, coloured by deviation from the row median.

    Each row is scaled to its own largest deviation, so heterogeneous metrics
    (mm, counts, percentages) can share one picture; the printed number is the
    real deviation with its unit. Rows that are flat within noise are greyed."""
    plt = _mpl()
    rows = [k for k in keys
            if k[0] == test and k[1] not in ("all", "fit", "scale")]
    data, ann, rowlab = [], [], []
    flat = 0
    for k in rows:
        raw = [m["v"].get(k) for m in maps]
        vals = np.array([v if isinstance(v, (int, float)) else np.nan
                         for v in raw], float)
        if not np.isfinite(vals).any():
            continue
        unit = next((m["u"].get(k) for m in maps if m["u"].get(k)), "")
        delta, dunit = _row_delta(vals, unit)
        if delta is None:
            continue
        scale = float(np.nanmax(np.abs(delta)))
        # Each row is scaled to its own spread, so a metric that is identical
        # everywhere would otherwise be painted in full colour for a rounding
        # difference. Those rows are dropped and counted instead.
        floor = 0.5 if dunit == "pp" else 2.0
        if not np.isfinite(scale) or scale < floor:
            flat += 1
            continue
        data.append(delta / scale)
        ann.append((delta, dunit))
        rowlab.append(f"{k[1]} · {k[2]}")
    if not data:
        return None
    M = np.array(data)
    n = M.shape[1]
    fig, ax = plt.subplots(figsize=(_fig_width(n, per=0.62),
                                    0.32 * len(rowlab) + 2.2))
    im = ax.imshow(M, cmap="RdBu_r", vmin=-1, vmax=1, aspect="auto")
    ax.set_xticks(range(n))
    ax.set_xticklabels(labels, rotation=40, ha="right", fontsize=7)
    ax.set_yticks(range(len(rowlab)))
    ax.set_yticklabels(rowlab, fontsize=7)
    if M.size <= 260:
        for i in range(M.shape[0]):
            delta, dunit = ann[i]
            for j in range(M.shape[1]):
                if np.isfinite(delta[j]):
                    txt = (f"{delta[j]:+.2g}" if abs(delta[j]) < 100
                           else f"{delta[j]:+.0f}")
                    ax.text(j, i, txt, ha="center", va="center", fontsize=5.8,
                            color="white" if abs(M[i, j]) > 0.6 else "#222")
    cb = fig.colorbar(im, ax=ax, fraction=0.022, pad=0.012,
                      ticks=[-1, 0, 1])
    cb.ax.set_yticklabels(["lowest", "median", "highest"], fontsize=7)
    subtitle = ("colour = position within each metric's own spread; "
                "number = actual deviation, % or percentage points")
    if flat:
        subtitle += f"\n{flat} metric(s) omitted — effectively identical here"
    ax.set_title("Deviation from the median of the selection\n" + subtitle,
                 fontsize=9)
    fig.tight_layout()
    return _b64(fig)


def _panel(ordered, labels, maps, test, metric, title, unit):
    """One subplot per object; each analysis is a labelled point.

    A dot plot rather than a line: with ten different phantoms there is no
    meaningful line to draw between them, and points stay readable."""
    plt = _mpl()
    objects = sorted({k[1] for m in maps for k in m["v"]
                      if k[0] == test and k[2] == metric
                      and k[1] not in ("all", "fit", "scale")})
    if not objects:
        return None
    colors, sites = _site_colors(ordered)
    n = len(ordered)
    ncol = min(len(objects), 4)
    nrow = int(np.ceil(len(objects) / ncol))
    fig, axes = plt.subplots(nrow, ncol, squeeze=False,
                             figsize=(max(3.4 * ncol, 8), 2.7 * nrow))
    x = np.arange(n)
    any_data = False
    for idx, obj in enumerate(objects):
        ax = axes[idx // ncol][idx % ncol]
        ys = np.array([m["v"].get((test, obj, metric), np.nan)
                       if isinstance(m["v"].get((test, obj, metric)),
                                     (int, float)) else np.nan
                       for m in maps], float)
        if np.all(~np.isfinite(ys)):
            ax.axis("off")
            continue
        any_data = True
        med = np.nanmedian(ys)
        ax.axhline(med, color="#888", lw=0.9, ls="--", zorder=1)
        for i, rec in enumerate(ordered):
            ax.scatter(x[i], ys[i], s=46, zorder=3,
                       color=colors[rec.get("site") or "(none)"],
                       edgecolor="#333", linewidth=0.4)
        ax.set_title(f"{obj}", fontsize=9)
        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=40, ha="right", fontsize=6)
        ax.tick_params(axis="y", labelsize=7)
        ax.grid(alpha=0.25, axis="y")
        if idx % ncol == 0:
            ax.set_ylabel(unit or "value", fontsize=8)
    for idx in range(len(objects), nrow * ncol):
        axes[idx // ncol][idx % ncol].axis("off")
    if not any_data:
        plt.close(fig)
        return None
    if len(sites) > 1:
        handles = [plt.Line2D([], [], marker="o", ls="", color=colors[s],
                              markeredgecolor="#333", label=s) for s in sites]
        fig.legend(handles=handles, loc="upper right", fontsize=7, ncol=len(sites),
                   frameon=False, bbox_to_anchor=(1.0, 1.02))
    fig.suptitle(title + ("  ·  dashed line = median of the selection"),
                 fontsize=10, y=1.0)
    fig.tight_layout()
    return _b64(fig)


def _wedge_curves(ordered, labels, maps):
    """Wedge response of every analysis on one axis — directly comparable."""
    plt = _mpl()
    steps = sorted({int(k[1][1:]) for m in maps for k in m["v"]
                    if k[0] == "wedge" and k[2] == "mean"
                    and k[1].startswith("S") and k[1][1:].isdigit()})
    if not steps:
        return None
    colors, sites = _site_colors(ordered)
    fig, ax = plt.subplots(figsize=(9, 4.0))
    cmap = plt.get_cmap("viridis")
    for i, (rec, m) in enumerate(zip(ordered, maps)):
        ys = [m["v"].get(("wedge", f"S{s}", "mean")) for s in steps]
        if all(y is None for y in ys):
            continue
        ax.plot(steps, [np.nan if y is None else y for y in ys], "o-", ms=4,
                lw=1.3, color=cmap(i / max(len(ordered) - 1, 1)),
                label=labels[i].replace("\n", " "))
    ax.set_xlabel("wedge step (S1 = top)")
    ax.set_ylabel("mean pixel value")
    ax.set_title("Wedge response curve per analysis", fontsize=10)
    ax.grid(alpha=0.3)
    ax.legend(fontsize=6.5, ncol=2 if len(ordered) > 6 else 1,
              loc="best", frameon=False)
    fig.tight_layout()
    return _b64(fig)


def _lowcontrast_curves(ordered, labels, maps):
    plt = _mpl()
    levels = sorted({k[1] for m in maps for k in m["v"]
                     if k[0] == "lowcontrast" and k[2] == "cnr"})
    if not levels:
        return None
    fig, ax = plt.subplots(figsize=(9, 4.0))
    cmap = plt.get_cmap("viridis")
    for i, m in enumerate(maps):
        ys = [m["v"].get(("lowcontrast", l, "cnr")) for l in levels]
        ys = [abs(y) if isinstance(y, (int, float)) else np.nan for y in ys]
        if all(not np.isfinite(y) for y in ys):
            continue
        ax.plot(range(len(levels)), ys, "o-", ms=4, lw=1.3,
                color=cmap(i / max(len(ordered) - 1, 1)),
                label=labels[i].replace("\n", " "))
    ax.set_xticks(range(len(levels)))
    ax.set_xticklabels(levels)
    ax.set_xlabel("circle (design order, L1 = weakest)")
    ax.set_ylabel("|CNR|")
    ax.set_title("Low-contrast detectability per analysis", fontsize=10)
    ax.grid(alpha=0.3)
    ax.legend(fontsize=6.5, ncol=2 if len(ordered) > 6 else 1, loc="best",
              frameon=False)
    fig.tight_layout()
    return _b64(fig)


def _variability_chart(keys, maps, top=14):
    """Which metrics differ most across the selection.

    Split by unit: magnitudes are ranked by relative range (% of median),
    metrics that are already percentages by their absolute range in percentage
    points. Mixing the two on one axis would put a ΔSNR of a few percent next
    to a 13 000 % artefact of dividing by a near-zero median."""
    plt = _mpl()
    rel, absolute = [], []
    for k in keys:
        if k[1] in ("all", "fit", "scale"):
            continue
        vals = np.array([m["v"].get(k, np.nan) if isinstance(
            m["v"].get(k), (int, float)) else np.nan for m in maps], float)
        good = vals[np.isfinite(vals)]
        if good.size < 2:
            continue
        unit = next((m["u"].get(k) for m in maps if m["u"].get(k)), "")
        rng = float(good.max() - good.min())
        name = f"{k[0]} · {k[1]} · {k[2]}"
        if unit == "%":
            absolute.append((name, rng))
        else:
            med = float(np.median(good))
            if abs(med) < 1e-12:
                continue
            rel.append((name, 100.0 * rng / abs(med)))
    if not rel and not absolute:
        return None, []
    rel.sort(key=lambda t: -t[1])
    absolute.sort(key=lambda t: -t[1])
    rel, absolute = rel[:top], absolute[:top]

    panels = [(rel, "relative range (% of that metric's median)", (10.0, 20.0)),
              (absolute, "range in percentage points", (2.0, 5.0))]
    panels = [p for p in panels if p[0]]
    heights = [0.30 * len(p[0]) + 1.1 for p in panels]
    fig, axes = plt.subplots(len(panels), 1, squeeze=False,
                             figsize=(9.5, sum(heights)),
                             gridspec_kw={"height_ratios": heights})
    for ax, (rows, xlabel, (amber, red)) in zip(axes[:, 0], panels):
        y = np.arange(len(rows))
        ax.barh(y, [r[1] for r in rows],
                color=["#cf3f3f" if r[1] > red else "#d9a021" if r[1] > amber
                       else "#4a7dbd" for r in rows])
        for yi, r in zip(y, rows):
            ax.text(r[1], yi, f"  {r[1]:.1f}", va="center", fontsize=6.5,
                    color="#444")
        ax.set_yticks(y)
        ax.set_yticklabels([r[0] for r in rows], fontsize=7)
        ax.invert_yaxis()
        ax.set_xlabel(xlabel, fontsize=8)
        ax.grid(alpha=0.3, axis="x")
        ax.margins(x=0.12)
    axes[0, 0].set_title("Metrics that differ most across this selection",
                         fontsize=10)
    fig.tight_layout()
    return _b64(fig), rel + absolute


# --------------------------------------------------------------------- report

_CHART_BATCH = contextvars.ContextVar("comparison_chart_batch", default=None)

def _encode_chart(png):
    from PIL import Image
    buf = io.BytesIO()
    with Image.open(io.BytesIO(png)) as image:
        image.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()

class _ChartBatch:
    """The charts of one report, encoded while the next is drawn.

    Each chart takes a placeholder in the page, numbered in the order the
    charts were drawn, and the placeholders are replaced by the finished
    pictures once the page is composed. The placeholder carries a random
    string drawn afresh for every report, so nothing written on the page — a
    site or phantom label, anything an operator typed — can pass for one: it
    would have to guess a string that did not exist when it was written."""

    def __init__(self, pool: ThreadPoolExecutor):
        self._pool = pool
        self._jobs: list = []
        self._nonce = secrets.token_hex(8)
        self._placeholder = re.compile(rf"@@chart-{self._nonce}-(\d+)@@")

    def submit(self, png: bytes) -> str:
        self._jobs.append(self._pool.submit(_encode_chart, png))
        return f"@@chart-{self._nonce}-{len(self._jobs) - 1}@@"

    def raise_first_failure(self) -> None:
        """Wait for every chart, and fail as encoding them in turn would have.

        In turn, the first chart that failed would have stopped the report
        right there, so its error — not a later one, and not whatever the
        rest of the page ran into after it — is the one to raise."""
        for job in self._jobs:
            failure = job.exception()
            if failure is not None:
                raise failure

    def fill(self, page: str) -> str:
        return self._placeholder.sub(
            lambda m: self._jobs[int(m.group(1))].result(), page)

def _picture_section(ordered, labels, pictures):
    titles = {"phantom": "Whole phantom", "linepairs": "Line-pair strip",
              "wedge": "Step wedge", "lowcontrast": "Low contrast (unprocessed; own window)",
              "uniformity": "Uniformity squares"}
    # First available scan within each acquisition protocol supplies the shared
    # window. Never compare brightness across different protocols.
    references = {}
    for rec in ordered:
        for region in thumbnails.REGIONS:
            pic = pictures.get(rec["id"], {}).get(region, {})
            if thumbnails.drawable(pic):
                references.setdefault((rec.get("signature") or "", region), pic)
    rows = []
    for region in thumbnails.REGIONS:
        cells = []
        for rec in ordered:
            pic = pictures.get(rec["id"], {}).get(region, {})
            if not thumbnails.drawable(pic):
                body = html.escape(str(pic.get("missing", "Picture unavailable")))
            else:
                ref = references[(rec.get("signature") or "", region)]
                window = pic if pic.get("window") == "self" else ref
                try:
                    encoded = thumbnails.encode_on(pic, window["lo"], window["hi"])
                except Exception:
                    cells.append('<td class="report-picture">Picture unavailable</td>')
                    continue
                uri = "data:" + encoded["mime"] + ";base64," + encoded["b64"]
                alt = html.escape(titles[region] + " — " + str(rec.get("phantom") or rec["id"]), quote=True)
                body = (f'<img src="{uri}" alt="{alt}"><details><summary>Enlarge picture</summary>'
                        f'<img class="enlarged" src="{uri}" alt="{alt}"></details>')
            cells.append('<td class="report-picture">' + body + '</td>')
        rows.append((titles[region], cells))
    # Three scan columns fit an A4 landscape page. All selected scans remain
    # present, in the same order, without shrinking them into unreadable cells.
    tables = []
    for start in range(0, len(ordered), 3):
        head = ''.join('<th scope="col">'+html.escape(label.replace("\n", " "))+'</th>'
                       for label in labels[start:start + 3])
        body = ''.join('<tr><th scope="row">' + title + '</th>' +
                       ''.join(cells[start:start + 3]) + '</tr>' for title, cells in rows)
        tables.append('<div class="scroll"><table class="pictures"><thead><tr><th>Region</th>' +
                      head + '</tr></thead><tbody>' + body + '</tbody></table></div>')
    return ('<section class="card"><h2>Scan pictures</h2><p>Pictures are aligned in the phantom frame. '
            'Each row shares the first available scan’s window within its protocol, except low contrast. '
            'Small pictures are for comparison; inspect fine detail in Analyze.</p>'
            + ''.join(tables) + '</section>')

def build_comparison_report(records, title_suffix="", filters=None, pictures=None):
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="report-encode") as pool:
        batch = _ChartBatch(pool)
        token = _CHART_BATCH.set(batch)
        try:
            try:
                page = _compose_comparison_report(records, title_suffix, filters, pictures)
            except BaseException:
                batch.raise_first_failure()
                raise
            batch.raise_first_failure()
            return batch.fill(page)
        finally:
            _CHART_BATCH.reset(token)


def _compose_comparison_report(records: list[dict], title_suffix: str = "",
                            filters: dict | None = None, pictures=None) -> str:
    ordered, keys, maps = _collect(records)
    if not ordered:
        return ("<!doctype html><html><body style='font-family:sans-serif;"
                "padding:40px'><h1>No completed analyses</h1><p>The selection "
                "contains no analyses with computed results.</p>"
                "</body></html>")

    labels = _entity_labels(ordered)
    n = len(ordered)
    sites = sorted({(r.get("site") or "(unlabelled)") for r in ordered})
    phantoms = sorted({(r.get("phantom") or "(unlabelled)") for r in ordered})

    # ---- key: label -> full identification -------------------------------
    key_rows = "".join(
        f"<tr><td><b>{html.escape(labels[i].replace(chr(10), ' '))}</b></td>"
        f"<td>{html.escape(r.get('site') or '—')}</td>"
        f"<td>{html.escape(r.get('phantom') or '—')}</td>"
        f"<td>{html.escape((r.get('acquired_at') or '')[:16] or '—')}</td>"
        f"<td>{html.escape((r.get('created_at') or '')[:16])}</td>"
        f"<td><span class='chip' style='background:"
        f"{_STATUS_COLOR.get(r.get('status'), '#9aa4b2')}'>"
        f"{html.escape(str(r.get('status') or 'n/a'))}</span></td>"
        f"<td><span class='chip' style='background:"
        f"{_VALIDATION_COLOR.get(r.get('validation_status') or '', '#9aa4b2')}'>"
        f"{html.escape(_VALIDATION_SHORT.get(r.get('validation_status') or '', ''))}"
        f"</span>"
        f"{('<br><span class=muted>' + html.escape(r.get('validated_by') or '') + '</span>') if r.get('validated_by') else ''}"
        f"</td>"
        f"<td class='muted'>{html.escape(r.get('signature') or '')}</td>"
        f"<td class='muted'>{html.escape(r['id'][:8])}</td></tr>"
        for i, r in enumerate(ordered))

    grid = _status_grid(ordered, labels)
    var_chart, var_rows = _variability_chart(keys, maps)

    # ---- per-pattern sections -------------------------------------------
    sections = ""
    for test in _SECTION_ORDER:
        tkeys = [k for k in keys if k[0] == test]
        if not tkeys:
            continue
        blocks = ""
        if test == "wedge":
            blocks += _img(_wedge_curves(ordered, labels, maps))
        if test == "lowcontrast":
            blocks += _img(_lowcontrast_curves(ordered, labels, maps))
        for metric, ptitle, unit in _PANELS.get(test, []):
            blocks += _img(_panel(ordered, labels, maps, test, metric,
                                  ptitle, unit))
        hm = _deviation_heatmap(ordered, labels, keys, maps, test)
        blocks += _img(hm)
        if not blocks:
            continue

        # numeric detail, collapsed
        head = "".join(f"<th>{html.escape(l.replace(chr(10), ' '))}</th>"
                       for l in labels)
        body = ""
        for k in tkeys:
            unit = next((m["u"].get(k) for m in maps if m["u"].get(k)), "")
            cells = ""
            for m in maps:
                v = m["v"].get(k)
                cells += (f"<td class='num'>"
                          f"{('%.4g' % v) if isinstance(v, (int, float)) and np.isfinite(v) else '—'}"
                          f"</td>")
            body += (f"<tr><td>{html.escape(k[1])}</td><td>{html.escape(k[2])}</td>"
                     f"<td class='muted'>{html.escape(unit)}</td>{cells}</tr>")
        sections += f"""
<section class="card">
  <h2>{html.escape(_SECTION_TITLES[test])}</h2>
  {blocks}
  <details><summary>Numeric detail ({len(tkeys)} metrics)</summary>
    <div class="scroll"><table>
      <tr><th>object</th><th>metric</th><th>unit</th>{head}</tr>{body}
    </table></div>
  </details>
</section>"""

    filt = ""
    if filters:
        parts = [f"{k}: {v}" for k, v in filters.items() if v]
        filt = " · ".join(html.escape(p) for p in parts)

    def _stamp(r):
        return (r.get("acquired_at") or "")[:16] or (r.get("created_at") or "")[:16]

    span = f"{_stamp(ordered[0])} → {_stamp(ordered[-1])}"

    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Phantom QA — comparison</title>
<style>
 body {{ font-family:"Segoe UI",system-ui,Arial,sans-serif; margin:0;
        background:#f4f6f8; color:#1d2530; }}
 .wrap {{ max-width:1180px; margin:0 auto; padding:24px; }}
 h1 {{ font-size:22px; margin:0 0 4px; }}
 h2 {{ font-size:16px; margin:0 0 12px; }}
 .card {{ background:#fff; border:1px solid #dde3ea; border-radius:10px;
          padding:16px 18px; margin:16px 0; }}
 table {{ border-collapse:collapse; width:100%; }}
 td, th {{ border:1px solid #dfe4ea; padding:4px 9px; font-size:11.5px;
           text-align:left; white-space:nowrap; }}
 th {{ background:#eef2f6; font-weight:600; }}
 td.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
 .muted {{ color:#8994a4; font-weight:400; font-size:10.5px; }}
 .chip {{ color:#fff; padding:1px 9px; border-radius:9px; font-size:10.5px;
          font-weight:600; }}
 .scroll {{ overflow-x:auto; }}
 img {{ max-width:100%; height:auto; display:block; margin:10px auto; }}
 details {{ margin-top:10px; }}
 summary {{ cursor:pointer; font-size:12px; color:#41506a; }}
 .facts {{ display:flex; gap:10px; flex-wrap:wrap; margin:6px 0 12px; }}
 .fact {{ background:#eef2f6; border-radius:8px; padding:7px 12px;
          font-size:12px; }}
 .fact b {{ font-size:15px; display:block; }}
 @media print {{ body {{ background:#fff; }} .card {{ break-inside:avoid; }} }}
 .pictures td {{ min-width:180px; vertical-align:top; white-space:normal; }}
 .pictures img {{ max-width:260px; max-height:220px; object-fit:contain; }}
 .pictures .enlarged {{ max-width:none; max-height:none; }}
 .report-picture summary {{ color:#41506a; }}
 {print_css("Comparison | " + span, landscape=True)}
</style></head><body><div class="wrap">
{controls()}
<h1>MSF Phantom QA — comparison{html.escape(title_suffix)}</h1>
<p class="muted" style="font-size:12px">{html.escape(span)}
{(' · ' + filt) if filt else ''}</p>

<section class="card">
  <h2>What is being compared</h2>
  <div class="facts">
    <div class="fact"><b>{n}</b>analyses</div>
    <div class="fact"><b>{len(sites)}</b>site(s)</div>
    <div class="fact"><b>{len(phantoms)}</b>phantom(s)</div>
  </div>
  <div class="scroll"><table>
    <tr><th>label used in the plots</th><th>site</th><th>phantom</th>
        <th>acquired</th><th>uploaded</th><th>overall</th><th>validation</th>
        <th>protocol</th><th>id</th></tr>
    {key_rows}
  </table></div>
  <p class="muted">Entries are grouped by site and phantom, and chronologically
  within a phantom. Every deviation on this page is measured against the
  <b>median of this selection</b>, not against the first entry — so the report
  reads the same whether you are following one phantom over time or comparing
  many phantoms against each other.</p>
  {_img(grid)}
</section>

<section class="card">
  <h2>Where the differences are</h2>
  {_img(var_chart)}
  <p class="muted">Metrics that are themselves percentages (pitch deviation,
  ΔSNR, …) are ranked by their range in percentage points; everything else by
  its range relative to its own median. Blue is small, amber is worth a look,
  red stands out.</p>
</section>

{_picture_section(ordered, labels, pictures) if pictures is not None else ""}
{sections}
</div></body></html>"""
