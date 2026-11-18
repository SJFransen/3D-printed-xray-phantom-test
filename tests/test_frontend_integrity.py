"""Structural checks on the browser code.

There is no build step and no JavaScript runtime in the deployment, so nothing
catches an unbalanced brace or a control that the markup does not contain until
an operator opens the page and the whole wizard is blank. These tests are the
substitute: they parse the sources far enough to prove the two files agree with
each other.
"""

from __future__ import annotations

import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "phantom_qa", "webapp", "static")


def _read(name: str) -> str:
    with open(os.path.join(STATIC, name), encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def app_js() -> str:
    return _read("app.js")


@pytest.fixture(scope="module")
def index_html() -> str:
    return _read("index.html")


#: A '/' begins a regular expression rather than a division when the previous
#: significant character cannot end an expression.
_REGEX_PRECEDERS = set("(,=:[!&|?{};+-*%~^<>") | {""}
_REGEX_KEYWORDS = ("return", "typeof", "instanceof", "in", "of", "new",
                   "delete", "void", "case", "do", "else", "yield", "await")


def _regex_allowed(out: list[str]) -> bool:
    tail = "".join(out).rstrip()
    if not tail:
        return True
    if tail[-1] in _REGEX_PRECEDERS:
        return True
    word = re.search(r"[A-Za-z_$][A-Za-z0-9_$]*$", tail)
    return bool(word and word.group(0) in _REGEX_KEYWORDS)


def strip_js(s: str) -> str:
    """Blank out comments, string literals and regular expressions.

    Crude but sufficient: the point is to leave a token stream whose brackets
    can be balanced, and `${...}` inside a template literal really is code.
    Regular expressions have to be recognised too — `/[&<>"]/g` contains an
    unpaired bracket and a quote that would otherwise swallow the rest of the
    file."""
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "/" and i + 1 < n and s[i + 1] == "/":
            j = s.find("\n", i)
            i = n if j < 0 else j
        elif c == "/" and i + 1 < n and s[i + 1] == "*":
            j = s.find("*/", i + 2)
            end = n if j < 0 else j + 2
            # keep the newlines so reported line numbers stay usable
            out.append("\n" * s.count("\n", i, end))
            i = end
        elif c == "/" and _regex_allowed(out):
            i += 1
            in_class = False
            while i < n:
                if s[i] == "\\":
                    i += 2
                    continue
                if s[i] == "[":
                    in_class = True
                elif s[i] == "]":
                    in_class = False
                elif s[i] == "/" and not in_class:
                    i += 1
                    break
                elif s[i] == "\n":       # unterminated: not a regex after all
                    break
                i += 1
            while i < n and s[i].isalpha():   # flags
                i += 1
            out.append("0")
        elif c in "\"'`":
            quote = c
            i += 1
            while i < n:
                if s[i] == "\\":
                    i += 2
                    continue
                if quote == "`" and s[i] == "$" and i + 1 < n and s[i + 1] == "{":
                    depth = 1
                    i += 2
                    while i < n and depth:
                        if s[i] == "{":
                            depth += 1
                        elif s[i] == "}":
                            depth -= 1
                            if depth == 0:
                                i += 1
                                break
                        out.append(s[i])
                        i += 1
                    continue
                if s[i] == quote:
                    i += 1
                    break
                if s[i] == "\n":
                    out.append("\n")
                i += 1
            out.append('""')
        else:
            out.append(c)
            i += 1
    return "".join(out)


def test_app_js_brackets_are_balanced(app_js):
    code = strip_js(app_js)
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[tuple[str, int]] = []
    line = 1
    for ch in code:
        if ch == "\n":
            line += 1
        elif ch in "([{":
            stack.append((ch, line))
        elif ch in ")]}":
            assert stack, f"stray {ch!r} at line ~{line} of app.js"
            opener, opened = stack.pop()
            assert opener == pairs[ch], (
                f"{ch!r} at line ~{line} closes {opener!r} opened at line "
                f"~{opened} in app.js")
    assert not stack, f"unclosed {stack[:3]} in app.js"


def _defined_ids(app_js: str, index_html: str) -> set[str]:
    ids = set(re.findall(r'id="([^"]+)"', index_html))
    # elements app.js creates itself
    ids |= set(re.findall(r'\{\s*id:\s*"([^"]+)"', app_js))
    ids |= set(re.findall(r'\.id\s*=\s*"([^"]+)"', app_js))
    ids |= set(re.findall(r'id="([^"]+)"', app_js))
    return ids


def test_every_referenced_element_exists(app_js, index_html):
    """A `$("#thing")` with no matching element is a silent null dereference.

    Every id app.js looks up must either be in index.html or be created by
    app.js itself."""
    used = set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"\)', app_js))
    missing = sorted(used - _defined_ids(app_js, index_html))
    assert not missing, f"app.js looks up ids that nothing creates: {missing}"


def test_no_browser_prompt_dialogs_for_data_entry(app_js):
    """Values the operator types go into fields on the page.

    window.prompt() cannot show the record being acted on, cannot validate
    before sending, and cannot display the server's refusal — which is how a
    failed deletion came to look like a successful one."""
    code = strip_js(app_js)
    assert "prompt(" not in code, (
        "app.js still calls prompt(); collect the value in a panel instead")


def test_both_dates_are_shown_in_history(app_js, index_html):
    """Acquisition and upload dates are separate columns, not one fallback."""
    assert ">uploaded</th>" in index_html, "History has no upload-date column"
    assert ">acquired</th>" in index_html, "History has no acquisition column"
    assert "acquired_flag" in app_js, (
        "History does not mark scans whose acquisition date is unusable")
    assert "a.created_at" in app_js, (
        "the history table never reads the upload date")


def test_the_history_row_has_one_cell_per_column(app_js, index_html):
    """Adding the upload column meant adding a <td>. A row one cell short
    shifts every column after it, which reads as corrupted data rather than as
    a layout bug."""
    header = re.search(r'<table id="history-table"><thead>(.*?)</thead>',
                       index_html, re.S)
    assert header, "the history table header moved"
    n_th = len(re.findall(r"<th[ >]", header.group(1)))

    row = re.search(r'const tr = el\("tr", \{\}, `(.*?)`\);', app_js, re.S)
    assert row, "the history row template moved"
    n_td = len(re.findall(r"<td[ >]", row.group(1)))

    assert n_td == n_th, (
        f"the history table declares {n_th} columns but each row renders "
        f"{n_td} cells")


def test_an_uncommitted_angle_preview_cannot_survive_the_step(app_js):
    """Typing an angle draws it immediately; leaving Stage C without applying
    it must not leave the page showing geometry the server does not have."""
    assert "previewPending" in app_js
    assert "discardPreview" in app_js


def test_undo_redo_and_reset_controls_exist(app_js):
    for token in ("geometry/${which}", "geometry/reset", "stepHistory",
                  "resetGeometry", "btn-undo", "btn-redo", "btn-reset-auto",
                  "btn-reset-profile"):
        assert token in app_js, f"Stage C is missing the {token} control"


def test_block_angle_is_a_field_not_a_dialog(app_js, index_html):
    assert 'id="lc-angle"' in app_js, (
        "the low-contrast block angle is not an input field in the panel")
    assert "commitBlockAngle" in app_js and "previewBlockAngle" in app_js


def test_a_signed_off_analysis_is_shown_as_locked(app_js):
    """The server refuses every edit on a validated analysis. The step must say
    so up front rather than letting the operator drag and then fail."""
    assert "signedOff" in app_js and "lockedBar" in app_js
    assert 'S.stage === "C" && !signedOff()' in app_js, (
        "dragging is still armed on a signed-off analysis")


def test_the_detail_tables_are_collapsed_by_default(app_js):
    """Steps B, D and E used to open with several dense tables. An operator who
    is not a physicist should see a verdict; the tables stay one click away."""
    assert "function advanced(" in app_js
    # every collapsible defaults to closed unless explicitly told otherwise
    assert 'return `<details class="advanced"${open ? " open" : ""}>' in app_js
    for token in ("Detection detail",          # step B
                  "Measured dimensions",       # step D
                  "Detail per pattern"):       # step E
        assert token in app_js, f"the {token} section is not collapsible"


def test_step_e_leads_with_a_summary(app_js):
    assert "summaryRows" in app_js
    assert "<th>test</th><th>result</th><th>measured</th>" in app_js, (
        "step E has no per-test summary table")
    # Anything that is not a pass opens its own detail — including "n/a",
    # which means the test was never measured, not that it was fine.
    assert 'cd.status !== "pass")).join("")' in app_js


def test_the_block_can_be_turned_end_for_end(app_js):
    """The block outline is symmetrical, so detection can find it 180 degrees
    out and every circle still lands on a real disc. One button, because a
    typed angle is the wrong tool for a binary mistake."""
    assert "flipBlock" in app_js and "lc-flip" in app_js
    assert "normaliseAngle" in app_js, (
        "a flipped angle must be brought back into -180..180 or the field and "
        "the nudge buttons stop being usable")


def test_the_reference_scan_can_be_removed(app_js):
    assert "toggleBaseline" in app_js and "baselineBlock" in app_js
    assert "Remove as reference" in app_js
    assert "cb-baseline" not in app_js, (
        "the old set-only baseline checkbox is still there")


def test_the_trend_section_is_collapsed_and_gated(app_js, index_html):
    """The trend chart draws nothing until the operator has said which scans
    belong together — a line across different phantoms would show assembly
    differences as if they were drift. And the whole section is folded away
    by default, because most History visits are not about trending."""
    m = re.search(r'<details class="advanced" id="trend-section"([^>]*)>',
                  index_html)
    assert m, "the trend section is not a collapsible <details>"
    assert "open" not in m.group(1), "the trend section must start collapsed"
    assert "trendSelectionMissing" in app_js
    assert "!H.filter.phantom && !selectedIds().length" in app_js, (
        "the chart no longer requires a phantom filter or ticked rows")


def test_the_trend_chart_renders_crisply_and_in_theme(app_js, index_html):
    """The old chart was a fixed 1000-px white bitmap, CSS-scaled to fit (so
    blurry), with 9-px labels rotated off the bottom edge of the canvas."""
    assert 'width="1000"' not in index_html, (
        "the trend canvas is still a fixed-size bitmap")
    assert "devicePixelRatio" in app_js, "no HiDPI scaling in the chart"
    assert "cssVar" in app_js and 'cssVar("--accent")' in app_js, (
        "the chart does not draw in the app's own palette")
    # rotated x labels were what ran off the canvas edge
    trend = app_js[app_js.index("function drawTrend"):
                   app_js.index("function wireTrendHover")]
    assert "rotate(" not in trend, "x labels are rotated again"
    assert "9px" not in trend, "the 9px label font is back"


def test_trend_x_labels_cannot_overlap_at_200_records(app_js):
    """The rule the chart uses, checked arithmetically at the sizes the field
    actually produces: labels are thinned so that drawn neighbours are at
    least TREND_MIN_XLABEL_PX apart, whatever the point count."""
    m = re.search(r"TREND_MIN_XLABEL_PX = (\d+)", app_js)
    assert m, "the label-spacing constant is gone"
    min_px = int(m.group(1))
    assert min_px >= 60, "labels narrower than a date cannot stay readable"
    # the exact thinning formula the chart applies
    assert "Math.floor(plotW / TREND_MIN_XLABEL_PX)" in app_js
    assert "Math.ceil(pts.length / maxTicks)" in app_js

    pad = re.search(r"TREND_PAD = \{ l: (\d+), r: (\d+)", app_js)
    pad_l, pad_r = int(pad.group(1)), int(pad.group(2))
    for css_w in (600, 900, 1200, 1600):          # laptop to wide desktop
        plot_w = css_w - pad_l - pad_r
        for n in (100, 150, 200):
            max_ticks = max(2, plot_w // min_px)
            every = max(1, -(-n // max_ticks))    # ceil
            spacing = every * plot_w / (n - 1)
            assert spacing >= min_px * 0.9, (
                f"{n} points at {css_w}px: labels {spacing:.0f}px apart "
                f"(minimum {min_px}px) — they would overlap")


def test_the_trend_canvas_cannot_outgrow_its_container(app_js):
    """The bitmap is sized from the canvas's OWN content box, and the layout
    width stays CSS's business. Measuring the PARENT's clientWidth included
    the section's padding, so the chart was pinned ~20px wider than its
    container and visually overflowed on the right."""
    sizing = app_js[app_js.index("function sizeTrendCanvas"):
                    app_js.index("function drawTrendMessage")]
    assert 'cv.style.width = "100%";' in sizing, (
        "the canvas pins an inline pixel width again — that is what overflowed")
    assert "cv.clientWidth" in sizing, (
        "the bitmap is no longer sized from the canvas's own content box")


def test_the_rightmost_date_label_stays_inside_the_canvas(app_js):
    """The last point sits at the plot's right edge; a label centred on it
    hangs half outside, so the newest date — the one an operator most wants —
    was always cut. Labels are clamped by their measured width."""
    trend = app_js[app_js.index("function drawTrend"):
                   app_js.index("function wireTrendHover")]
    assert "ctx.measureText(text).width / 2" in trend
    assert "Math.min(Math.max(x, half + 2), w - half - 2)" in trend, (
        "x labels are no longer clamped inside the canvas")


def test_the_trend_has_a_hover_readout(app_js):
    """With 100+ points on screen, reading values off the line is guesswork;
    hovering a point shows its exact value, date and scan."""
    assert "wireTrendHover" in app_js
    assert 'addEventListener("mousemove"' in app_js
    assert 'addEventListener("mouseleave"' in app_js


def test_the_collapsed_trend_costs_no_requests(app_js):
    """Collapsed means dormant: the section only fetches when opened, and a
    resize only redraws while it is open."""
    assert "if (sect && !sect.open) return;" in app_js
    assert 'addEventListener("toggle"' in app_js


def test_delete_panel_markup_is_present(index_html):
    for el_id in ("del-backdrop", "del-card", "del-reason", "del-pw",
                  "del-error", "del-confirm", "del-cancel", "del-layout-warn"):
        assert f'id="{el_id}"' in index_html, f"delete panel lacks #{el_id}"
    assert "confirm_id" not in index_html
