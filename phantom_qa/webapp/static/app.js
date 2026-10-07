/* MSF Phantom QA — wizard frontend (no build step, no external deps). */
"use strict";

const COLORS = { geometry: "#00c8ff", linepairs: "#ffd400", lowcontrast: "#ff7bda",
                 uniformity: "#7bff9f", wedge: "#ff9d5c", reg: "#ff4040" };
const TESTS = ["geometry", "linepairs", "lowcontrast", "uniformity", "wedge"];
const STAGES = ["U", "A", "B", "C", "D", "E", "F"];

const S = {
  aid: null, record: null, reg: null, geometry: null, results: null,
  baseline: null, stage: "U", sid: 1000,
  imgEl: null, imgScale: 1, nativeCols: 1, nativeRows: 1,
  view: { k: 1, tx: 0, ty: 0 },
  visible: { geometry: true, linepairs: true, lowcontrast: true,
             uniformity: true, wedge: true },
  labels: true, selectedRoi: null, mode: "normal", manualCorners: [],
  fieldEdgeSide: null, dragFieldEdge: null, dragRoi: null, dimPreview: null, lcCorners: [],
  pendingFile: null,
  /* measuring-point undo state, mirrored from the server after every edit */
  history: { seq: 0, undo_depth: 0, redo_depth: 0 },
  layoutSource: "", phantomProfile: null,
  lcAngleCommitted: 0, rotTargetId: null, previewPending: false,
};

/* Everything the viewer knows about ONE analysis. Cleared as a unit whenever
   an analysis is closed or deleted, so no fragment of the previous scan — a
   selected ROI id, a half-finished corner click, a dimension preview — can be
   drawn over the next one. */
function clearAnalysisState() {
  S.aid = null; S.record = null; S.reg = null; S.geometry = null;
  S.results = null; S.baseline = null; S.imgEl = null;
  S.selectedRoi = null; S.mode = "normal"; S.manualCorners = [];
  S.fieldEdgeSide = null; S.dragFieldEdge = null; S.dragRoi = null; S.dimPreview = null;
  S.lcCorners = []; S.pendingFile = null;
  S.history = { seq: 0, undo_depth: 0, redo_depth: 0 };
  S.layoutSource = ""; S.phantomProfile = null;
  S.lcAngleCommitted = 0; S.rotTargetId = null; S.previewPending = false;
  const d = document.querySelector("#roi-details");
  if (d) d.remove();
}

const $ = (sel) => document.querySelector(sel);
const el = (tag, attrs = {}, html = "") => {
  const e = document.createElement(tag);
  Object.entries(attrs).forEach(([k, v]) => e.setAttribute(k, v));
  e.innerHTML = html;
  return e;
};
const fmt = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v))
  ? "—" : (typeof v === "number" ? v.toFixed(d) : String(v));
const chip = (s) => `<span class="chip ${(s || "na").replace("/", "")}">${s || "n/a"}</span>`;

function csrfToken() {
  const m = document.cookie.match(/(?:^|;\s*)phantomqa_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

/* Every state-changing request carries the CSRF token from the cookie; the
   server rejects the request if the two do not match.

   A 401 normally means the session expired, so we sign in again. The
   administrator-password endpoints (delete, validation, forgetting a stored
   layout) also answer 401 for a WRONG PASSWORD — bouncing to the login page
   there would log the operator out mid-action and never show them why. Those
   callers pass adminAuth so the error comes back for the panel to display. */
async function api(path, opts = {}) {
  const { adminAuth = false, ...rest } = opts;
  const o = { credentials: "same-origin", ...rest };
  const method = (o.method || "GET").toUpperCase();
  if (["POST", "PUT", "PATCH", "DELETE"].includes(method)) {
    o.headers = { ...(o.headers || {}), "X-CSRF-Token": csrfToken() };
  }
  const r = await fetch(path, o);
  let msg = r.statusText, body = null;
  if (!r.ok) {
    try { body = await r.json(); msg = body.detail || msg; } catch (e) { /* noop */ }
  }
  if (r.status === 401) {
    // The admin-password endpoints answer 401 for two different things. Only a
    // genuine session expiry may bounce to the login page; a refused password
    // has to reach the panel that asked for it. The middleware's expiry
    // message is the one thing that distinguishes them.
    const expired = msg === "Authentication required";
    if (!adminAuth || expired) {
      window.location = "login";
      throw new Error("Session expired — signing in again");
    }
  }
  if (!r.ok) {
    const err = new Error(typeof msg === "string" ? msg : r.statusText);
    err.status = r.status;
    if (body && body.duplicate_of) err.duplicateOf = body.duplicate_of;
    throw err;
  }
  return r.json();
}
const postJSON = (path, body, opts = {}) => api(path, {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body), ...opts });

function status(msg, isErr = false) {
  const n = $("#app-status");
  n.textContent = msg;
  n.style.color = isErr ? "var(--fail)" : "var(--muted)";
  if (msg) setTimeout(() => { if (n.textContent === msg) n.textContent = ""; }, 6000);
}

/* ================= coordinate transforms ================= */

function pxToMm(px) {
  if (!S.reg || !S.reg.transform) return null;
  const A = S.reg.transform.A, t = S.reg.transform.t;
  const det = A[0][0] * A[1][1] - A[0][1] * A[1][0];
  const dx = px[0] - t[0], dy = px[1] - t[1];
  return [ (A[1][1] * dx - A[0][1] * dy) / det,
           (-A[1][0] * dx + A[0][0] * dy) / det ];
}

/* screen <-> native image px */
const nat2scr = (p) => [ p[0] * S.imgScale * S.view.k + S.view.tx,
                         p[1] * S.imgScale * S.view.k + S.view.ty ];
const scr2nat = (p) => [ (p[0] - S.view.tx) / (S.imgScale * S.view.k),
                         (p[1] - S.view.ty) / (S.imgScale * S.view.k) ];

/* ================= viewer ================= */

const canvas = $("#viewer");
const ctx2d = canvas.getContext("2d");

function resizeCanvas() {
  const wrap = $("#viewer-wrap");
  canvas.width = wrap.clientWidth;
  canvas.height = wrap.clientHeight;
  draw();
}
window.addEventListener("resize", resizeCanvas);

function zoomFit() {
  if (!S.imgEl) return;
  const k = Math.min(canvas.width / S.imgEl.width,
                     canvas.height / S.imgEl.height) * 0.98;
  S.view = { k, tx: (canvas.width - S.imgEl.width * k) / 2,
             ty: (canvas.height - S.imgEl.height * k) / 2 };
  draw();
}

function loadImage(params = "") {
  if (!S.aid) return;
  const img = new Image();
  img.onload = () => {
    const first = !S.imgEl;
    S.imgEl = img;
    S.imgScale = img.width / S.nativeCols;
    if (first) zoomFit(); else draw();
  };
  img.src = `api/analyses/${S.aid}/image.png${params}`;
}

/* Window/level is expressed RELATIVE to the image's own value range, because
   detectors differ in bit depth: a fixed 0..4095 slider whites out a 14-bit
   image completely. 0-100 on the sliders maps into [lo, hi] measured from the
   data, and width can exceed the range so the image can be flattened. */
function wlAbsolute() {
  const r = (S.reg && S.reg.display_range) || null;
  if (!r) return null;
  const span = Math.max(r.hi - r.lo, 1e-6);
  const cPct = +$("#wl-center").value / 100;       // 0..1 across the range
  const wPct = +$("#wl-width").value / 100;        // 0..1.5 of the range
  const wc = r.lo + cPct * span;
  const ww = Math.max(wPct * span, span * 0.01);
  return { wc, ww };
}

function updateWLReadout() {
  const a = wlAbsolute();
  $("#wl-readout").textContent = a
    ? `W ${Math.round(a.ww)}  C ${Math.round(a.wc)}`
    : "";
}

let wlTimer = null;
function onWL() {
  updateWLReadout();
  clearTimeout(wlTimer);
  wlTimer = setTimeout(() => {
    const a = wlAbsolute();
    if (a) loadImage(`?wc=${a.wc.toFixed(1)}&ww=${a.ww.toFixed(1)}`);
  }, 200);
}
$("#wl-center").addEventListener("input", onWL);
$("#wl-width").addEventListener("input", onWL);
$("#wl-reset").addEventListener("click", () => {
  $("#wl-center").value = 50;
  $("#wl-width").value = 100;
  updateWLReadout();
  loadImage();                       // server default: 1-99 percentile stretch
});
$("#zoom-fit").addEventListener("click", zoomFit);

function drawRoi(roi, color, opts = {}) {
  if (!roi || !roi.type) return;
  ctx2d.strokeStyle = color;
  ctx2d.lineWidth = opts.selected ? 2.5 : 1.2;
  ctx2d.setLineDash(opts.dash || []);
  if (roi.type === "rect" && roi.corners_px) {
    ctx2d.beginPath();
    roi.corners_px.forEach((p, i) => {
      const s = nat2scr(p);
      i === 0 ? ctx2d.moveTo(s[0], s[1]) : ctx2d.lineTo(s[0], s[1]);
    });
    ctx2d.closePath();
    ctx2d.stroke();
  } else if (roi.type === "circle") {
    const c = nat2scr(roi.center_px);
    ctx2d.beginPath();
    ctx2d.arc(c[0], c[1], roi.radius_px * S.imgScale * S.view.k, 0, Math.PI * 2);
    ctx2d.stroke();
  } else if (roi.type === "annulus") {
    const c = nat2scr(roi.center_px);
    const k = S.imgScale * S.view.k;
    ctx2d.setLineDash([2, 3]);
    [roi.inner_radius_px, roi.outer_radius_px].forEach(r => {
      ctx2d.beginPath();
      ctx2d.arc(c[0], c[1], r * k, 0, Math.PI * 2);
      ctx2d.stroke();
    });
    ctx2d.setLineDash([]);
  } else if (roi.type === "segment") {
    const a = nat2scr(roi.p0_px), b = nat2scr(roi.p1_px);
    ctx2d.beginPath(); ctx2d.moveTo(a[0], a[1]); ctx2d.lineTo(b[0], b[1]);
    ctx2d.stroke();
  }
  ctx2d.setLineDash([]);
  if (opts.handle && roi.center_px) {
    const c = nat2scr(roi.center_px);
    ctx2d.fillStyle = roi.manually_adjusted ? "#ff9d00" : color;
    ctx2d.beginPath(); ctx2d.arc(c[0], c[1], 4, 0, Math.PI * 2); ctx2d.fill();
  }
  if (opts.label && roi.center_px) {
    const c = nat2scr(roi.center_px);
    ctx2d.font = "12px Segoe UI";
    ctx2d.fillStyle = color;
    ctx2d.fillText(opts.label, c[0] + 8, c[1] - 8);
  }
}

/* collect drawable+draggable ROIs from the current geometry */
function activeRois() {
  const out = [];
  if (!S.geometry) return out;
  const g = S.geometry;
  if (g.uniformity && !g.uniformity._error && S.visible.uniformity)
    g.uniformity.squares.forEach(sq => out.push(
      { roi: sq.roi, test: "uniformity", label: sq.id, drag: true }));
  if (g.wedge && !g.wedge._error && S.visible.wedge) {
    g.wedge.steps.forEach(st => out.push(
      { roi: st.roi, test: "wedge", label: `S${st.step}`, drag: true }));
    out.push({ roi: g.wedge.axis, test: "wedge" });
  }
  if (g.linepairs && !g.linepairs._error && S.visible.linepairs)
    g.linepairs.groups.forEach(gr => {
      out.push({ roi: gr.roi, test: "linepairs", label: gr.id, drag: true });
      out.push({ roi: gr.profile_seg, test: "linepairs" });
    });
  if (g.lowcontrast && !g.lowcontrast._error && S.visible.lowcontrast) {
    out.push({ roi: g.lowcontrast.block, test: "lowcontrast",
               label: "block", drag: true, block: true });
    g.lowcontrast.circles.forEach(c => {
      out.push({ roi: c.full_circle, test: "lowcontrast", dash: [4, 3] });
      out.push({ roi: c.bg_roi, test: "lowcontrast" });
      out.push({ roi: c.roi, test: "lowcontrast", label: c.id, drag: true });
    });
  }
  if (g.geometry && !g.geometry._error && S.visible.geometry) {
    Object.values(g.geometry.rulers || {}).forEach(r => {
      if (r.probe) out.push({ roi: r.probe, test: "geometry" });
    });
    Object.values(g.geometry.field_edges || {}).forEach(f => {
      if (f.probe) out.push({ roi: f.probe, test: "geometry", dash: [6, 4] });
    });
  }
  return out;
}

function draw() {
  ctx2d.clearRect(0, 0, canvas.width, canvas.height);
  if (!S.imgEl) return;
  ctx2d.imageSmoothingEnabled = S.view.k * S.imgScale < 1.5;
  ctx2d.save();
  ctx2d.translate(S.view.tx, S.view.ty);
  ctx2d.scale(S.view.k, S.view.k);
  ctx2d.drawImage(S.imgEl, 0, 0);
  ctx2d.restore();

  /* registration corners */
  if (S.reg && S.reg.summary && S.reg.summary.corners_px) {
    ctx2d.strokeStyle = COLORS.reg; ctx2d.lineWidth = 1.4;
    ctx2d.setLineDash([8, 5]);
    ctx2d.beginPath();
    S.reg.summary.corners_px.concat([S.reg.summary.corners_px[0]])
      .forEach((p, i) => {
        const s = nat2scr(p);
        i === 0 ? ctx2d.moveTo(s[0], s[1]) : ctx2d.lineTo(s[0], s[1]);
      });
    ctx2d.stroke();
    ctx2d.setLineDash([]);
  }

  const showHandles = S.stage === "C";
  const showLabels = S.labels && (S.stage === "B" || S.stage === "C");
  activeRois().forEach(item => {
    drawRoi(item.roi, COLORS[item.test], {
      label: showLabels ? item.label : null,
      handle: showHandles && item.drag,
      dash: item.dash,
      selected: S.selectedRoi && item.roi.id === S.selectedRoi,
    });
  });

  /* field edge markers */
  if (S.geometry && S.geometry.geometry && !S.geometry.geometry._error) {
    Object.entries(S.geometry.geometry.field_edges || {}).forEach(([side, f]) => {
      if (f.edge_pt_px) {
        const c = nat2scr(f.edge_pt_px);
        ctx2d.strokeStyle = COLORS.geometry;
        ctx2d.lineWidth = 2;
        ctx2d.beginPath();
        ctx2d.moveTo(c[0] - 10, c[1]); ctx2d.lineTo(c[0] + 10, c[1]);
        ctx2d.moveTo(c[0], c[1] - 10); ctx2d.lineTo(c[0], c[1] + 10);
        ctx2d.stroke();
        if (f.manual && showHandles && !signedOff()) {
          ctx2d.beginPath();
          ctx2d.arc(c[0], c[1], 6, 0, Math.PI * 2);
          ctx2d.stroke();
        }
        ctx2d.font = "11px Segoe UI"; ctx2d.fillStyle = COLORS.geometry;
        ctx2d.fillText(`field ${side}${f.manual ? " (manual)" : ""}`,
                       c[0] + 12, c[1] + 4);
      }
    });
  }

  /* low-contrast block corner clicks */
  (S.lcCorners || []).forEach((p, i) => {
    const c = nat2scr(p);
    ctx2d.fillStyle = COLORS.lowcontrast;
    ctx2d.beginPath(); ctx2d.arc(c[0], c[1], 5, 0, Math.PI * 2); ctx2d.fill();
    ctx2d.font = "12px Segoe UI";
    ctx2d.fillText(String(i + 1), c[0] + 8, c[1]);
  });

  /* manual corner clicks */
  S.manualCorners.forEach((p, i) => {
    const s = nat2scr(p);
    ctx2d.fillStyle = "#ff4040";
    ctx2d.beginPath(); ctx2d.arc(s[0], s[1], 5, 0, Math.PI * 2); ctx2d.fill();
    ctx2d.fillText(String(i + 1), s[0] + 8, s[1]);
  });
}

/* ---- mouse interaction ---- */
let panning = null;
canvas.addEventListener("mousedown", (ev) => {
  const pos = [ev.offsetX, ev.offsetY];
  if (S.mode === "corners" || S.mode === "fieldedge" || S.mode === "lccorners") return;
  if (S.stage === "C" && !signedOff()) {
    const field = hitFieldEdge(pos);
    if (field) {
      S.dragFieldEdge = { ...field, start: pos,
        original: field.edge.edge_pt_px.slice(), moved: false };
      canvas.style.cursor = "grabbing";
      return;
    }
    const hit = hitRoi(pos);
    if (hit && hit.drag) {
      // Remember where the press started: a click that never moves must stay a
      // click. It used to POST the ROI's unchanged centre, which stamped it
      // "manually adjusted", wrote an audit line, and — on the low-contrast
      // block — discarded the automatic grid refinement.
      S.dragRoi = hit;
      S.dragStart = pos;
      S.dragMoved = false;
      S.selectedRoi = hit.roi.id;
      draw();
      return;
    }
  }
  panning = { start: pos, tx: S.view.tx, ty: S.view.ty };
  canvas.style.cursor = "grabbing";
});
canvas.addEventListener("mousemove", (ev) => {
  const pos = [ev.offsetX, ev.offsetY];
  const nat = scr2nat(pos);
  const mm = pxToMm(nat);
  $("#cursor-mm").textContent = mm
    ? `x ${mm[0].toFixed(1)} mm  y ${mm[1].toFixed(1)} mm` : "";
  if (S.dragFieldEdge) {
    const drag = S.dragFieldEdge;
    if (Math.hypot(pos[0] - drag.start[0], pos[1] - drag.start[1]) > 3)
      drag.moved = true;
    if (drag.moved) {
      const start = scr2nat(drag.start);
      drag.edge.edge_pt_px = [drag.original[0] + nat[0] - start[0],
                             drag.original[1] + nat[1] - start[1]];
      draw();
    }
    return;
  }
  if (S.dragRoi) {
    if (S.dragStart && Math.hypot(pos[0] - S.dragStart[0],
                                  pos[1] - S.dragStart[1]) > 3) {
      S.dragMoved = true;
    }
    if (!S.dragMoved) return;
    const natP = scr2nat(pos);
    moveRoiLocal(S.dragRoi.roi, natP);
    draw();
    return;
  }
  if (panning) {
    S.view.tx = panning.tx + (pos[0] - panning.start[0]);
    S.view.ty = panning.ty + (pos[1] - panning.start[1]);
    draw();
  }
});
canvas.addEventListener("mouseup", async (ev) => {
  canvas.style.cursor = "grab";
  const pos = [ev.offsetX, ev.offsetY];
  if (S.dragFieldEdge) {
    const drag = S.dragFieldEdge;
    S.dragFieldEdge = null;
    if (drag.moved) {
      const saved = await submitFieldEdge(drag.edge.edge_pt_px, drag.side);
      if (!saved) { drag.edge.edge_pt_px = drag.original; draw(); }
    }
    return;
  }
  if (S.mode === "corners") {
    S.manualCorners.push(scr2nat(pos));
    draw();
    if (S.manualCorners.length === 4) await submitManualCorners();
    return;
  }
  if (S.mode === "lccorners") {
    S.lcCorners.push(scr2nat(pos));
    draw();
    if (S.lcCorners.length === 4) {
      S.mode = "normal";
      const corners = S.lcCorners.slice();
      S.lcCorners = [];
      await placeBlock({ corners_px: corners });
    }
    return;
  }
  if (S.mode === "fieldedge" && S.fieldEdgeSide) {
    await submitFieldEdge(scr2nat(pos));
    return;
  }
  if (S.dragRoi) {
    const hit = S.dragRoi, roi = hit.roi, moved = S.dragMoved;
    S.dragRoi = null; S.dragStart = null; S.dragMoved = false;
    if (!moved) {
      // A press that did not move is an inspect, not an edit.
      try {
        const r = await api(`api/analyses/${S.aid}/roi_stats?roi_id=`
                            + encodeURIComponent(roi.id));
        showRoiDetails(r.roi, r.stats);
      } catch (e) { /* noop */ }
      draw();
      return;
    }
    if (hit.block) { await placeBlock({ center_px: roi.center_px }); return; }
    try {
      const r = await postJSON(`api/analyses/${S.aid}/roi`,
        { roi_id: roi.id, center_px: roi.center_px });
      applyChanged(r);
      showRoiDetails(r.roi, r.stats);
      status(`${roi.id} moved — measurement updated`);
      draw();
    } catch (e) {
      status("ROI update failed: " + e.message, true);
      openAnalysis(S.aid);            // resync rather than show a stale ROI
    }
    return;
  }
  if (panning) {
    const movedFar = Math.hypot(pos[0] - panning.start[0],
                                pos[1] - panning.start[1]) > 4;
    panning = null;
    if (!movedFar && S.stage === "C") {
      const hit = hitRoi(pos);
      if (hit) {
        S.selectedRoi = hit.roi.id;
        try {
          const r = await api(`api/analyses/${S.aid}/roi_stats?roi_id=` +
                              encodeURIComponent(hit.roi.id));
          showRoiDetails(r.roi, r.stats);
        } catch (e) { /* noop */ }
        draw();
      }
    }
  }
});
canvas.addEventListener("mouseleave", () => {
  if (S.dragFieldEdge) {
    S.dragFieldEdge.edge.edge_pt_px = S.dragFieldEdge.original;
    S.dragFieldEdge = null;
    canvas.style.cursor = "grab";
    draw();
  }
});
canvas.addEventListener("wheel", (ev) => {
  ev.preventDefault();
  const f = ev.deltaY < 0 ? 1.15 : 1 / 1.15;
  const pos = [ev.offsetX, ev.offsetY];
  S.view.tx = pos[0] - (pos[0] - S.view.tx) * f;
  S.view.ty = pos[1] - (pos[1] - S.view.ty) * f;
  S.view.k *= f;
  draw();
}, { passive: false });

function hitFieldEdge(screenPos) {
  let best = null, bestD = 14;
  Object.entries(S.geometry?.geometry?.field_edges || {}).forEach(([side, edge]) => {
    if (!edge.manual || !edge.edge_pt_px) return;
    const c = nat2scr(edge.edge_pt_px);
    const d = Math.hypot(c[0] - screenPos[0], c[1] - screenPos[1]);
    if (d < bestD) { bestD = d; best = { side, edge }; }
  });
  return best;
}

function hitRoi(screenPos) {
  const items = activeRois().filter(i => i.drag && i.roi.center_px);
  let best = null, bestD = 14;
  items.forEach(i => {
    const c = nat2scr(i.roi.center_px);
    const d = Math.hypot(c[0] - screenPos[0], c[1] - screenPos[1]);
    if (d < bestD) { bestD = d; best = i; }
  });
  return best;
}

function moveRoiLocal(roi, natCenter) {
  const dx = natCenter[0] - roi.center_px[0];
  const dy = natCenter[1] - roi.center_px[1];
  roi.center_px = natCenter;
  if (roi.corners_px)
    roi.corners_px = roi.corners_px.map(p => [p[0] + dx, p[1] + dy]);
}

function replaceRoi(roiId, fresh) {
  const walk = (node) => {
    if (Array.isArray(node)) { node.forEach(walk); return; }
    if (node && typeof node === "object") {
      if (node.id === roiId && node.type) {
        Object.keys(node).forEach(k => { delete node[k]; });
        Object.assign(node, fresh);
      } else {
        Object.values(node).forEach(walk);
      }
    }
  };
  walk(S.geometry);
}

/* The server returns every ROI a move or rotation touched — the dragged one
   plus its companions (background ring, object outline, profile line). All of
   them must be redrawn, or the display shows a measurement in one place while
   the number comes from another. */
function applyChanged(response) {
  const list = response.changed || (response.roi ? [response.roi] : []);
  list.forEach(r => { if (r && r.id) replaceRoi(r.id, r); });
  noteHistory(response);
}

/* Every geometry-changing response carries how far undo and redo can now go,
   so the buttons never need a second round trip to know their state. */
function noteHistory(response) {
  if (response && response.history) {
    S.history = response.history;
    refreshHistoryButtons();
  }
}

function refreshHistoryButtons() {
  const u = $("#btn-undo"), r = $("#btn-redo");
  if (u) {
    u.disabled = !S.history.undo_depth;
    u.textContent = `↶ Undo${S.history.undo_depth ? ` (${S.history.undo_depth})` : ""}`;
  }
  if (r) {
    r.disabled = !S.history.redo_depth;
    r.textContent = `↷ Redo${S.history.redo_depth ? ` (${S.history.redo_depth})` : ""}`;
  }
}

function showRoiDetails(roi, stats) {
  let d = $("#roi-details");
  if (!d) {
    d = el("div", { id: "roi-details", class: "roi-details" });
    $("#wizard-pane").appendChild(d);
  }
  S.selectedRoi = roi.id;
  const mm = roi.center_mm || [];
  const rotatable = roi.type === "rect";
  const ang = rotatable ? (roi.angle_deg || 0) : null;
  const badges =
    (roi.manually_adjusted ? ' <span class="chip warn">manually adjusted</span>' : "")
    + (roi.from_profile ? ' <span class="roi-badge">from stored layout</span>' : "");
  // Segments (profile lines, the wedge axis) have no area, so no statistics.
  const statLine = stats
    ? `μ=${fmt(stats.mean, 1)} σ=${fmt(stats.std, 1)} n=${stats.n}` : "";
  d.innerHTML = `<b>${roi.id}</b>${badges}<br>
    centre (${fmt(mm[0])}, ${fmt(mm[1])}) mm &nbsp; ${statLine}
    ${rotatable ? `
    <div class="rot-row">
      <label>angle
        <input type="range" id="roi-angle" min="-180" max="180" step="0.5"
               value="${ang.toFixed(1)}">
      </label>
      <input type="number" id="roi-angle-num" step="0.5" min="-180" max="180"
             value="${ang.toFixed(1)}" title="degrees in the phantom frame">
      <button class="secondary-sm" id="roi-angle-minus">−1°</button>
      <button class="secondary-sm" id="roi-angle-plus">+1°</button>
    </div>
    <span class="hint">Drag the dot to move · type an exact angle or drag the
    slider · [ and ] nudge by 1°</span>`
    : ""}`;
  if (!rotatable) return;
  // The keyboard shortcut acts on the ROI whose panel is actually on screen,
  // not on whatever was last clicked: selecting the low-contrast block on
  // mousedown used to leave a stale panel and send the nudge to the wrong ROI.
  S.rotTargetId = roi.id;
  const slider = $("#roi-angle");
  const num = $("#roi-angle-num");
  const sync = (v, from) => {
    if (from !== "slider") slider.value = v;
    if (from !== "num") num.value = (+v).toFixed(1);
  };
  slider.addEventListener("input", () => {
    sync(slider.value, "slider");
    previewRotation(roi.id, +slider.value);
  });
  slider.addEventListener("change", () => commitRotation(roi.id, +slider.value));
  num.addEventListener("input", () => {
    const v = parseFloat(num.value);
    if (Number.isFinite(v)) { sync(v, "num"); previewRotation(roi.id, v); }
  });
  // Enter commits, not blur: `change` on a number input also fires when the
  // field loses focus, so tabbing away would silently POST a rotation.
  num.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    const v = parseFloat(num.value);
    if (Number.isFinite(v)) commitRotation(roi.id, v);
    else status("Enter a number of degrees.", true);
  });
  $("#roi-angle-minus").addEventListener("click", () => nudgeRotation(-1));
  $("#roi-angle-plus").addEventListener("click", () => nudgeRotation(+1));
}

function findRoi(roiId) {
  let found = null;
  const walk = (n) => {
    if (found) return;
    if (Array.isArray(n)) { n.forEach(walk); return; }
    if (n && typeof n === "object") {
      if (n.id === roiId && n.type) { found = n; return; }
      Object.values(n).forEach(walk);
    }
  };
  walk(S.geometry);
  return found;
}

/* A phantom-frame angle expressed in image space — the same mapping the
   server uses (analysis/common.img_angle_deg). The old code assumed the
   phantom's +y always pointed up on screen, which is false for a mirrored
   registration: the local preview then turned the opposite way and only
   snapped back after the round trip. */
function imgAngleDeg(mmDeg) {
  if (!S.reg || !S.reg.transform) return mmDeg;
  const A = S.reg.transform.A, r = mmDeg * Math.PI / 180;
  const c = Math.cos(r), s = Math.sin(r);
  return Math.atan2(A[1][0] * c + A[1][1] * s,
                    A[0][0] * c + A[0][1] * s) * 180 / Math.PI;
}

const rotatePointAbout = (p, c, a) => {
  const dx = p[0] - c[0], dy = p[1] - c[1];
  return [c[0] + dx * Math.cos(a) - dy * Math.sin(a),
          c[1] + dx * Math.sin(a) + dy * Math.cos(a)];
};

/* Rotate locally for instant feedback; the server has the final word. */
function previewRotation(roiId, angleDeg) {
  if (roiId === "lowcontrast/block") { previewBlockAngle(angleDeg); return; }
  const roi = findRoi(roiId);
  if (!roi || roi.type !== "rect" || !roi.corners_px) return;
  const a = (imgAngleDeg(angleDeg) - imgAngleDeg(roi.angle_deg || 0))
            * Math.PI / 180;
  const c = roi.center_px;
  roi.corners_px = roi.corners_px.map(p => rotatePointAbout(p, c, a));
  roi.angle_deg = angleDeg;
  roi.angle_img_deg = imgAngleDeg(angleDeg);
  S.previewPending = true;
  draw();
}

async function commitRotation(roiId, angleDeg) {
  // The block is a rigid group of 25 ROIs; rotating it through the generic ROI
  // endpoint would turn its outline and leave the eight circles behind.
  if (roiId === "lowcontrast/block") { await commitBlockAngle(angleDeg); return; }
  try {
    const r = await postJSON(`api/analyses/${S.aid}/roi_rotate`,
                             { roi_id: roiId, angle_deg: angleDeg });
    applyChanged(r);
    S.previewPending = false;
    showRoiDetails(r.roi, r.stats);
    status(`${roiId} rotated to ${angleDeg.toFixed(1)}° — measurement updated`);
    draw();
  } catch (e) {
    status("Rotation failed: " + e.message, true);
    openAnalysis(S.aid);
  }
}

function nudgeRotation(delta) {
  const slider = $("#roi-angle");
  const target = S.rotTargetId;
  if (!slider || !target) return;
  const v = +(+slider.value + delta).toFixed(1);
  slider.value = v;
  const num = $("#roi-angle-num");
  if (num) num.value = v.toFixed(1);
  previewRotation(target, v);
  commitRotation(target, v);
}

/* [ and ] nudge whichever angle control is in play: the block's field when it
   has focus, otherwise the selected ROI's panel, otherwise the block. */
document.addEventListener("keydown", (e) => {
  if (S.stage !== "C") return;
  if (e.key !== "[" && e.key !== "]") return;
  const t = e.target;
  if (t && /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) && t.id !== "lc-angle")
    return;
  const d = e.key === "[" ? -1 : +1;
  e.preventDefault();
  if (t && t.id === "lc-angle") { nudgeBlockAngle(d); return; }
  if (S.rotTargetId && $("#roi-angle")) { nudgeRotation(d); return; }
  nudgeBlockAngle(d);
});

/* ---- low-contrast block angle ----
   Its own path, because the block is a rigid group: the outline and all eight
   circle triples turn together about the block centre. */
function previewBlockAngle(angleDeg) {
  const lc = S.geometry && S.geometry.lowcontrast;
  if (!lc || lc._error || !lc.block || !lc.block.corners_px) return;
  const c = lc.block.center_px;
  const a = (imgAngleDeg(angleDeg) - imgAngleDeg(lc.block.angle_deg || 0))
            * Math.PI / 180;
  lc.block.corners_px = lc.block.corners_px.map(p => rotatePointAbout(p, c, a));
  lc.block.angle_deg = angleDeg;
  lc.block.angle_img_deg = imgAngleDeg(angleDeg);
  (lc.circles || []).forEach(ci => ["roi", "bg_roi", "full_circle"].forEach(k => {
    if (ci[k] && ci[k].center_px)
      ci[k].center_px = rotatePointAbout(ci[k].center_px, c, a);
  }));
  lc.angle_deg = angleDeg;
  S.previewPending = true;
  draw();
}

async function commitBlockAngle(v) {
  if (!Number.isFinite(v)) { status("Enter a number of degrees.", true); return; }
  await placeBlock({ angle_deg: v });
}

/* Turn the block end for end.

   The block outline is symmetrical about its centre, so the automatic angle is
   only ever determined modulo 180 — detection genuinely cannot tell which end
   is which. When it guesses wrong the eight circles are still on eight real
   discs, so nothing looks broken and no check fails: L1 simply sits on L8's
   disc and the contrast series is reported backwards. That makes it the one
   correction worth a single button rather than a typed angle. */
async function flipBlock() {
  const lc = S.geometry && S.geometry.lowcontrast;
  if (!lc || lc._error || !lc.block) return;
  const flipped = normaliseAngle((Number(lc.angle_deg) || 0) + 180);
  const f = $("#lc-angle");
  if (f) f.value = flipped.toFixed(1);
  previewBlockAngle(flipped);
  await commitBlockAngle(flipped);
}

/* Keep a phantom-frame angle in -180..180 so the field and the ±1° buttons
   stay usable after a flip. */
function normaliseAngle(deg) {
  let a = ((deg + 180) % 360 + 360) % 360 - 180;
  if (Object.is(a, -180)) a = 180;
  return a;
}

function nudgeBlockAngle(delta) {
  const f = $("#lc-angle");
  if (!f || f.disabled) return;
  const v = +((parseFloat(f.value) || 0) + delta).toFixed(1);
  f.value = v.toFixed(1);
  previewBlockAngle(v);
  commitBlockAngle(v);
}

/* ================= overlay toggles ================= */

function renderToggles() {
  const c = $("#overlay-toggles");
  c.innerHTML = "";
  TESTS.forEach(t => {
    const lab = el("label", {},
      `<span class="swatch" style="background:${COLORS[t]}"></span> ${t}`);
    const cb = el("input", { type: "checkbox" });
    cb.checked = S.visible[t];
    cb.addEventListener("change", () => { S.visible[t] = cb.checked; draw(); });
    lab.prepend(cb);
    c.appendChild(lab);
  });
  const lab = el("label", {}, "labels");
  const cb = el("input", { type: "checkbox" });
  cb.checked = S.labels;
  cb.addEventListener("change", () => { S.labels = cb.checked; draw(); });
  lab.prepend(cb);
  c.appendChild(lab);
}

/* ================= identity (site / phantom) ================= */

/* Shown above every stage once an analysis is open, so the labels are always
   visible and always editable — forgetting them at upload is recoverable. */
function renderIdentityBar() {
  const bar = $("#identity-bar");
  if (!S.aid || !S.record) { bar.innerHTML = ""; bar.classList.add("hidden"); return; }
  bar.classList.remove("hidden");
  const r = S.record;
  const missing = !r.site && !r.phantom;
  const val = (v) => v ? html_escape(v) : '<span class="hint">—</span>';
  bar.innerHTML = `
    <div class="ident-row">
      <div>
        <span class="ident-k">Site</span> ${val(r.site)}
        <span class="ident-sep">·</span>
        <span class="ident-k">Phantom</span> ${val(r.phantom)}
        ${r.operator ? `<span class="ident-sep">·</span>
           <span class="ident-k">Op</span> ${html_escape(r.operator)}` : ""}
      </div>
      <button id="btn-edit-ident" class="secondary-sm">Edit</button>
    </div>
    <div class="ident-row" style="padding-top:0">
      <div>
        <span class="ident-k">Validation</span> ${valChip(r.validation_status)}
        ${r.validated_by
          ? `<span class="hint"> by ${html_escape(r.validated_by)}`
            + `${r.validated_at ? " · " + html_escape(r.validated_at.slice(0, 16)) : ""}</span>`
          : ""}
      </div>
      <button id="btn-validate" class="secondary-sm">Set…</button>
    </div>
    ${r.validation_comment
      ? `<div class="ident-comment">“${html_escape(r.validation_comment)}”</div>`
      : ""}
    ${missing ? '<div class="ident-warn">⚠ No site or phantom — this analysis '
      + 'will not appear in any grouped trend. Add them now.</div>' : ""}`;
  $("#btn-validate").addEventListener("click", () =>
    setValidation(r, (v) => { Object.assign(S.record, v); renderIdentityBar(); }));
  $("#btn-edit-ident").addEventListener("click", async () => {
    const vals = await editLabelsDialog(r, `Identification — ${r.id}`);
    if (!vals) return;
    try {
      const out = await postJSON(`api/analyses/${S.aid}/labels`, vals);
      Object.assign(S.record, vals);
      // A stored layout belongs to the phantom label. Renaming the last
      // analysis off a label leaves that layout describing nothing, so it is
      // discarded — say so, because it is not visible anywhere else.
      if (out.layout_deleted) {
        S.phantomProfile = null;
        status(`Identification updated. The stored measuring-point layout for `
               + `phantom ${out.phantom_before} was discarded — no analyses `
               + `carry that name any more.`);
      } else {
        status("Identification updated.");
      }
      await refreshProfileForRecord();
      renderIdentityBar();
      renderStage();
    } catch (e) { status("Could not save: " + e.message, true); }
  });
}

/* After a phantom rename the layout on offer changes, so re-read it rather
   than leaving Stage C advertising the previous phantom's. */
async function refreshProfileForRecord() {
  if (!S.aid) return;
  try {
    const rec = await api(`api/analyses/${S.aid}`);
    S.phantomProfile = rec.phantom_profile || null;
    S.record = { ...S.record, ...rec };
  } catch (e) { /* leave what we have */ }
}

function html_escape(s) {
  return String(s).replace(/[&<>"]/g, ch => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]));
}

/* Modal editor shared by the identity bar and the History table. */
function editLabelsDialog(rec, title = "Edit identification") {
  return new Promise(async (resolve) => {
    $("#modal-title").textContent = title;
    ["site", "phantom", "operator", "notes"].forEach(
      k => { $("#m-" + k).value = rec[k] || ""; });
    try {
      const lab = await api("api/labels");
      const opts = (items) => (items || [])
        .map(s => `<option value="${s.value.replace(/"/g, "&quot;")}">`).join("");
      $("#dl-site-m").innerHTML = opts(lab.site);
      $("#dl-phantom-m").innerHTML = opts(lab.phantom);
    } catch (e) { /* ignore */ }
    const back = $("#modal-backdrop");
    back.classList.remove("hidden");
    $("#m-site").focus();

    const done = (result) => {
      back.classList.add("hidden");
      $("#m-save").onclick = null;
      $("#m-cancel").onclick = null;
      back.onclick = null;
      document.onkeydown = null;
      resolve(result);
    };
    $("#m-save").onclick = () => done({
      site: $("#m-site").value.trim(),
      phantom: $("#m-phantom").value.trim(),
      operator: $("#m-operator").value.trim(),
      notes: $("#m-notes").value.trim(),
    });
    $("#m-cancel").onclick = () => done(null);
    back.onclick = (e) => { if (e.target === back) done(null); };
    document.onkeydown = (e) => { if (e.key === "Escape") done(null); };
  });
}


/* Three-way choice for a re-uploaded file.

   Deliberately not a confirm(): with two buttons, "Cancel" would have to mean
   "analyse it again", so an operator dismissing the dialog would create the
   very duplicate this check exists to prevent. Cancel must mean cancel. */
function duplicateDialog(d) {
  return new Promise((resolve) => {
    // textContent, not innerHTML: site / phantom / notes are operator-supplied
    $("#dup-id").textContent = d.id;
    $("#dup-who").textContent =
      [d.site, d.phantom].filter(Boolean).join(" / ") || "unlabelled";
    $("#dup-when").textContent =
      (d.acquired_at || d.created_at || "").slice(0, 16).replace("T", " ");
    $("#dup-status").textContent = d.status || "-";

    const back = $("#dup-backdrop");
    back.classList.remove("hidden");
    $("#dup-open").focus();

    const done = (result) => {
      back.classList.add("hidden");
      $("#dup-open").onclick = null;
      $("#dup-again").onclick = null;
      $("#dup-cancel").onclick = null;
      back.onclick = null;
      document.onkeydown = null;
      resolve(result);
    };
    $("#dup-open").onclick = () => done("open");
    $("#dup-again").onclick = () => done("again");
    $("#dup-cancel").onclick = () => done("cancel");
    back.onclick = (e) => { if (e.target === back) done("cancel"); };
    document.onkeydown = (e) => { if (e.key === "Escape") done("cancel"); };
  });
}


/* The low-contrast circles are a rigid grid inside the block, so the block is
   the natural handle: place it once and all eight circles follow. */
async function placeBlock(payload) {
  try {
    const r = await postJSON(`api/analyses/${S.aid}/lowcontrast_block`, payload);
    S.geometry.lowcontrast = r.lowcontrast;
    S.previewPending = false;
    noteHistory(r);
    // Every low-contrast ROI object was just replaced, so anything holding a
    // reference to one — the angle field, an open ROI panel — must re-read.
    S.lcAngleCommitted = Number(r.angle_deg);
    const f = $("#lc-angle");
    if (f) f.value = S.lcAngleCommitted.toFixed(1);
    if (S.selectedRoi && String(S.selectedRoi).startsWith("lowcontrast/")) {
      if (!findRoi(S.selectedRoi)) {
        S.selectedRoi = null; S.rotTargetId = null;
        const d = $("#roi-details");
        if (d) d.remove();
      } else if (S.rotTargetId === "lowcontrast/block") {
        // The details panel lives outside #stage-content, so it survives every
        // re-render and would keep the pre-edit angle on its slider. [ and ]
        // read that slider, so the next nudge would quietly undo the angle
        // just applied.
        const sl = $("#roi-angle"), num = $("#roi-angle-num");
        if (sl) sl.value = S.lcAngleCommitted;
        if (num) num.value = S.lcAngleCommitted.toFixed(1);
      }
    }
    status(`Low-contrast block placed at ${r.angle_deg.toFixed(1)}° — `
           + `all 8 circles moved with it`);
    draw();
  } catch (e) {
    status("Could not place block: " + e.message, true);
    // The local preview already moved the block; the server did not accept it,
    // so re-read rather than leave the two disagreeing.
    openAnalysis(S.aid);
  }
}

/* ================= validation (administrator sign-off) ================= */

const VAL_LABEL = {
  "": "pending review",
  validated: "validated",
  conditionally_validated: "conditionally validated",
  not_validated: "not validated",
};
const VAL_CLASS = {
  "": "na", validated: "pass",
  conditionally_validated: "warn", not_validated: "fail",
};

function valChip(st) {
  const s = st || "";
  return `<span class="chip ${VAL_CLASS[s] || "na"}">${VAL_LABEL[s] || s}</span>`;
}

/* Same administrator credential as deletion: it is the other decision an
   ordinary user must not be able to make. The approver's NAME is recorded
   separately, because a shared password cannot say who signed. */
function validationDialog(rec, submit) {
  return new Promise((resolve) => {
    const back = $("#val-backdrop");
    $("#val-target").textContent =
      `${rec.id} · ${[rec.site, rec.phantom].filter(Boolean).join(" / ") || "unlabelled"}`;
    const cur = rec.validation_status || "";
    document.querySelectorAll('input[name="vstatus"]').forEach(
      r => { r.checked = (r.value === cur); });
    if (!document.querySelector('input[name="vstatus"]:checked')) {
      document.querySelector('input[name="vstatus"][value="validated"]').checked = true;
    }
    $("#v-by").value = rec.validated_by || "";
    $("#v-comment").value = rec.validation_comment || "";
    $("#v-pw").value = "";
    $("#val-error").textContent = "";
    back.classList.remove("hidden");
    $("#v-by").focus();

    // The panel owns the submit, for the same reason the delete panel does: a
    // wrong administrator password is a 401, and letting that reach the shared
    // 401 handler threw the operator out to the sign-in page mid-signoff, with
    // the approver name and comment they had typed lost and a throttle strike
    // recorded that they never saw.
    let busy = false;
    const done = (result) => {
      if (busy) return;
      back.classList.add("hidden");
      $("#v-save").onclick = null;
      $("#v-cancel").onclick = null;
      back.onclick = null;
      document.onkeydown = null;
      resolve(result);
    };
    $("#v-save").onclick = async () => {
      const sel = document.querySelector('input[name="vstatus"]:checked');
      const status = sel ? sel.value : "";
      const by = $("#v-by").value.trim();
      if (status && !by) {
        $("#val-error").textContent =
          "The name of the person approving is required.";
        return;
      }
      if (!$("#v-pw").value) {
        $("#val-error").textContent = "The administrator password is required.";
        return;
      }
      busy = true;
      $("#v-save").disabled = true;
      $("#val-error").textContent = "Recording…";
      try {
        const r = await submit({
          status, validated_by: by, comment: $("#v-comment").value.trim(),
          admin_password: $("#v-pw").value });
        busy = false;
        done(r);
      } catch (e) {
        busy = false;
        $("#v-save").disabled = false;
        $("#val-error").textContent = e.message;
        $("#v-pw").select();
      }
    };
    $("#v-cancel").onclick = () => done(null);
    back.onclick = (e) => { if (e.target === back) done(null); };
    document.onkeydown = (e) => { if (e.key === "Escape") done(null); };
  });
}

async function setValidation(rec, onDone) {
  let policy = { enabled: true };
  try { policy = await api("api/validation_policy"); } catch (e) { /* noop */ }
  if (!policy.enabled) {
    alert("Validation requires an administrator password.\n\nAn administrator "
      + "must set PHANTOMQA_ADMIN_PASSWORD_HASH in .env\n"
      + "(python -m phantom_qa.manage set-admin-password).");
    return;
  }
  $("#v-save").disabled = false;
  const r = await validationDialog(rec, (vals) =>
    postJSON(`api/analyses/${rec.id}/validation`, vals, { adminAuth: true }));
  if (!r) return;
  status(`Recorded: ${VAL_LABEL[r.validation_status] || "pending review"}`
         + (r.validated_by ? ` (${r.validated_by})` : ""));
  if (onDone) onDone(r);
}

/* ================= integrity & deletion ================= */

async function verifyAnalysis(aid) {
  status("Re-hashing the stored source file…");
  try {
    const r = await api(`api/analyses/${aid}/verify`);
    const msg = {
      ok: `✔ Verified — the stored file still matches the SHA-256 recorded at `
        + `analysis time.\n\nSHA-256:\n${r.stored_sha256}\n\n`
        + `File: ${r.source_name} (${(r.size_bytes / 1048576).toFixed(1)} MB)`,
      mismatch: `✘ MISMATCH — the stored file no longer matches the hash `
        + `recorded at analysis time. Do not rely on these results.\n\n`
        + `recorded: ${r.stored_sha256}\ncomputed: ${r.computed_sha256}`,
      missing_file: `⚠ The stored source file is missing, so the results can no `
        + `longer be traced back to their input.\n\nrecorded: ${r.stored_sha256}`,
    }[r.status] || r.message;
    alert(msg);
    status(r.status === "ok" ? "Integrity verified." : "Integrity check FAILED.",
           r.status !== "ok");
  } catch (e) { status("Verification failed: " + e.message, true); }
}

/* Deleting destroys the stored source file, the edit history and — when this
   was the last analysis of its phantom — that phantom's stored measuring-point
   layout. It needs the ADMIN password and a written reason.

   A panel with real fields, not a chain of prompt() boxes. The old flow asked
   for the id, then the password, then the reason, and every refusal came back
   as a status line that cleared itself after six seconds. An operator who
   mistyped the password saw the row still there, assumed the browser had not
   caught up, and on re-uploading the same file was offered "open the existing
   analysis" — which brought back the very marks they thought they had
   deleted. Here the target stays on screen while they type and the refusal is
   shown in place until they deal with it. */
function deleteDialog(rec, impact, submit) {
  return new Promise((resolve) => {
    const back = $("#del-backdrop");
    const text = (id, v) => { $(id).textContent = v || "—"; };
    // textContent throughout: site, phantom and the file name are operator-
    // supplied and must never be parsed as markup.
    text("#del-id", rec.id);
    text("#del-who", [rec.site, rec.phantom].filter(Boolean).join(" / ")
                     || "unlabelled");
    text("#del-acquired", (rec.acquired_at || "").slice(0, 16)
                          || "not recorded by the scanner");
    text("#del-uploaded", (rec.created_at || "").slice(0, 16));
    text("#del-source", rec.source_name);
    text("#del-status", rec.status || "-");

    const warn = $("#del-layout-warn");
    if (impact && impact.layout_would_be_deleted) {
      warn.textContent =
        `⚠ This is the last analysis of phantom “${impact.phantom}”. Its stored `
        + `measuring-point layout will be deleted too, so the next scan of that `
        + `phantom starts from automatic detection again.`;
      warn.classList.remove("hidden");
    } else {
      warn.textContent = "";
      warn.classList.add("hidden");
    }

    $("#del-reason").value = "";
    $("#del-pw").value = "";
    $("#del-error").textContent = "";
    $("#del-confirm").disabled = false;
    back.classList.remove("hidden");
    $("#del-reason").focus();

    // While the request is in flight the panel refuses to close. Otherwise
    // Escape would report "nothing was deleted" to an operator whose deletion
    // was, at that moment, succeeding.
    let busy = false;
    const done = (result) => {
      if (busy) return;
      back.classList.add("hidden");
      $("#del-confirm").onclick = null;
      $("#del-cancel").onclick = null;
      back.onclick = null;
      document.onkeydown = null;
      resolve(result);
    };
    const minChars = (impact && impact.min_reason_chars) || 5;
    $("#del-confirm").onclick = async () => {
      const reason = $("#del-reason").value.trim();
      if (reason.length < minChars) {
        $("#del-error").textContent =
          `Give a reason of at least ${minChars} characters — it is the only `
          + "record of why this data was destroyed.";
        return;
      }
      if (!$("#del-pw").value) {
        $("#del-error").textContent = "The administrator password is required.";
        return;
      }
      // The panel stays open until the server actually accepts. A refusal —
      // wrong password, throttled, reason too short — is shown here rather
      // than as a status line that fades, so the deletion can never appear to
      // have happened when it did not.
      busy = true;
      $("#del-confirm").disabled = true;
      $("#del-error").textContent = "Deleting…";
      try {
        const r = await submit({ admin_password: $("#del-pw").value, reason });
        busy = false;
        done(r);
      } catch (e) {
        busy = false;
        $("#del-error").textContent = e.message;
        $("#del-confirm").disabled = false;
        $("#del-pw").select();
      }
    };
    $("#del-cancel").onclick = () => done(null);
    back.onclick = (e) => { if (e.target === back) done(null); };
    document.onkeydown = (e) => { if (e.key === "Escape") done(null); };
  });
}

async function deleteAnalysis(aid) {
  let policy = { enabled: true, min_reason_chars: 5 };
  try { policy = await api("api/deletion_policy"); } catch (e) { /* noop */ }
  if (!policy.enabled) {
    alert("Deletion is disabled on this installation.\n\nAn administrator must "
      + "set PHANTOMQA_ADMIN_PASSWORD_HASH in .env\n"
      + "(python -m phantom_qa.manage set-admin-password).");
    return;
  }
  let rec = (H.rows || []).find(x => x.id === aid) || { id: aid };
  let impact = { min_reason_chars: policy.min_reason_chars || 5 };
  try {
    const full = await api(`api/analyses/${aid}`);
    rec = { ...rec, ...full };
    impact = { ...(full.delete_impact || {}), ...impact };
  } catch (e) { /* fall back to what History already knows */ }

  const r = await deleteDialog(rec, impact, (vals) =>
    postJSON(`api/analyses/${aid}/delete`, vals, { adminAuth: true }));
  if (!r) { status("Nothing was deleted."); return; }
  status(`Analysis ${aid} deleted.`
         + (r.layout_deleted
            ? ` The stored measuring-point layout for phantom ${r.phantom} `
              + `was removed with it.`
            : ""));
  if (S.aid === aid) {
    clearAnalysisState();
    setStage("U");
    draw();
  }
  loadHistory();
}

/* ================= wizard stages ================= */

function setStage(st) {
  // An angle typed but not applied has already been drawn locally. Leaving the
  // step with that preview standing would show geometry the server does not
  // have — and step C, re-entered, would read the previewed angle back out of
  // S.geometry as if it had been committed. Resync instead of guessing.
  const leavingWithPreview = S.stage === "C" && st !== "C" && S.previewPending;
  S.stage = st;
  document.querySelectorAll("#stage-nav li").forEach(li => {
    const s = li.dataset.stage;
    li.classList.toggle("active", s === st);
    li.classList.toggle("done",
      STAGES.indexOf(s) < STAGES.indexOf(st) && s !== "U");
  });
  const rd = $("#roi-details");
  if (rd && st !== "C") { rd.remove(); S.rotTargetId = null; }
  renderIdentityBar();
  renderStage();
  draw();
  if (leavingWithPreview) discardPreview();
}

/* Drop an uncommitted local preview by re-reading the stored geometry. Cheap,
   because it only runs when a preview is actually outstanding. */
async function discardPreview() {
  S.previewPending = false;
  if (!S.aid) return;
  try {
    const rec = await api(`api/analyses/${S.aid}`);
    S.geometry = rec.geometry;
    S.history = rec.history || S.history;
    renderStage();
    draw();
  } catch (e) { /* leave the local copy; the next edit will resync */ }
}

function renderStage() {
  const c = $("#stage-content");
  ({ U: stageU, A: stageA, B: stageB, C: stageC, D: stageD, E: stageE,
     F: stageF }[S.stage])(c);
}

/* ---- Stage U: upload (choose file, fill identity, then confirm) ---- */
async function stageU(c) {
  c.innerHTML = `<h2>New analysis</h2>
    <p class="hint">DICOM file (preferred), zipped DICOM CD export, or a plain
    image (reduced precision). Nothing is uploaded until you press
    <b>Upload &amp; analyse</b>, so you can set the file and the labels in any
    order.</p>

    <h3>1 · Choose the scan file</h3>
    <div class="drop-zone" id="drop">Drop file here or click to choose</div>
    <input type="file" id="file-input" class="hidden">
    <div id="file-chosen" class="chosen hidden"></div>

    <h3>2 · Identify this scan</h3>
    <p class="hint">Site and phantom are how analyses are grouped for trending.
    Use the same spelling every time — previous values appear as suggestions.
    You can still change these later from the identity bar above.</p>
    <div class="form-grid">
      <label>Site <input id="up-site" list="dl-site" placeholder="e.g. Goma Hospital"></label>
      <label>Phantom <input id="up-phantom" list="dl-phantom" placeholder="e.g. MSF-01"></label>
      <label>Operator <input id="up-operator" placeholder="optional"></label>
      <label>Notes <input id="up-notes" placeholder="optional"></label>
    </div>
    <datalist id="dl-site"></datalist>
    <datalist id="dl-phantom"></datalist>
    <p id="up-warn" class="hint"></p>

    <h3>3 · Confirm</h3>
    <button class="primary" id="btn-upload" disabled>Upload &amp; analyse</button>
    <button class="secondary" id="btn-clear-file">Clear file</button>`;

  try {
    const lab = await api("api/labels");
    const opts = (items) => (items || [])
      .map(s => `<option value="${s.value.replace(/"/g, "&quot;")}">`).join("");
    $("#dl-site").innerHTML = opts(lab.site);
    $("#dl-phantom").innerHTML = opts(lab.phantom);
  } catch (e) { /* first run: no labels yet */ }

  // remember the last used labels so a batch of scans is not retyped
  ["site", "phantom", "operator"].forEach(k => {
    const v = sessionStorage.getItem("lbl_" + k);
    if (v) $("#up-" + k).value = v;
  });

  const drop = $("#drop"), inp = $("#file-input");
  const refresh = () => {
    const f = S.pendingFile;
    const box = $("#file-chosen");
    box.classList.toggle("hidden", !f);
    if (f) {
      box.innerHTML = `<b>${f.name}</b> · ${(f.size / 1048576).toFixed(1)} MB`;
    }
    $("#btn-upload").disabled = !f;
    const noLabel = !$("#up-site").value.trim() && !$("#up-phantom").value.trim();
    $("#up-warn").innerHTML = noLabel
      ? '<span style="color:var(--warn)">⚠ Without a site or phantom this '
        + 'analysis will not appear in any grouped trend.</span>'
      : "";
  };

  drop.addEventListener("click", () => inp.click());
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("armed"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("armed"));
  drop.addEventListener("drop", (e) => {
    e.preventDefault(); drop.classList.remove("armed");
    if (e.dataTransfer.files.length) { S.pendingFile = e.dataTransfer.files[0]; refresh(); }
  });
  inp.addEventListener("change", () => {
    if (inp.files.length) { S.pendingFile = inp.files[0]; refresh(); }
  });
  ["up-site", "up-phantom"].forEach(id =>
    $("#" + id).addEventListener("input", refresh));
  $("#btn-clear-file").addEventListener("click", () => {
    S.pendingFile = null; inp.value = ""; refresh();
  });
  $("#btn-upload").addEventListener("click", () => {
    if (S.pendingFile) uploadFile(S.pendingFile);
  });
  refresh();
}

async function uploadFile(file, opts = {}) {
  const labels = {
    site: ($("#up-site") || {}).value.trim() || "",
    phantom: ($("#up-phantom") || {}).value.trim() || "",
    operator: ($("#up-operator") || {}).value.trim() || "",
    notes: ($("#up-notes") || {}).value.trim() || "",
  };
  ["site", "phantom", "operator"].forEach(
    k => sessionStorage.setItem("lbl_" + k, labels[k]));
  $("#btn-upload").disabled = true;
  status("Uploading and registering…");
  const fd = new FormData();
  fd.append("file", file);
  Object.entries(labels).forEach(([k, v]) => fd.append(k, v));
  if (opts.allowDuplicate) fd.append("allow_duplicate", "true");
  try {
    const r = await api("api/analyses", { method: "POST", body: fd });
    const ok = r.analyses.filter(a => a.registered);
    if (!r.analyses.length) throw new Error("no images found");
    const notes = [];
    if (r.analyses.length > 1)
      notes.push(`${r.analyses.length} images found — opening the first; others `
                 + `are in History.`);
    if (r.phantom_profile)
      notes.push(`A stored measuring-point layout for phantom `
                 + `${r.phantom_profile.phantom} will be applied when you `
                 + `confirm the registration.`);
    S.pendingFile = null;
    const first = ok[0] || r.analyses[0];
    await openAnalysis(first.id);
    if (notes.length) status(notes.join(" "));
  } catch (e) {
    if (e.duplicateOf && e.duplicateOf.length) {
      const choice = await duplicateDialog(e.duplicateOf[0]);
      if (choice === "open") { await openAnalysis(e.duplicateOf[0].id); return; }
      if (choice === "again") {
        await uploadFile(file, { allowDuplicate: true });
        return;
      }
      status("Upload cancelled — the file was already analysed.");
      if ($("#btn-upload")) $("#btn-upload").disabled = false;
      return;
    }
    status("Upload failed: " + e.message, true);
    if ($("#btn-upload")) $("#btn-upload").disabled = false;
  }
}

async function openAnalysis(aid) {
  // Full reset first: nothing of the previously open analysis may survive into
  // this one, not even a selected ROI id or a half-finished corner click.
  clearAnalysisState();
  S.aid = aid;
  const rec = await api(`api/analyses/${aid}`);
  S.record = rec;
  S.reg = rec.registration;
  S.geometry = rec.geometry;
  S.results = rec.results;
  S.history = rec.history || { seq: 0, undo_depth: 0, redo_depth: 0 };
  S.layoutSource = rec.layout_source || "";
  S.phantomProfile = rec.phantom_profile || null;
  S.sid = rec.sid_mm || 1000;
  S.nativeCols = S.reg ? S.reg.image.cols : (rec.meta.Columns || 3000);
  S.nativeRows = S.reg ? S.reg.image.rows : (rec.meta.Rows || 3000);
  loadImage();
  showTab("analyze");
  setStage(rec.geometry ? (rec.results ? "F" : rec.stage || "B") : "A");
}

/* ---- Stage A ---- */
function stageA(c) {
  if (!S.reg) { c.innerHTML = "<p>Registration unavailable.</p>"; return; }
  const s = S.reg.summary;
  const lmRows = Object.entries(S.reg.landmarks || {}).map(([side, l]) =>
    `<tr><td>${side}</td><td class="num">${l.err_mm !== null && l.err_mm !== undefined
       ? fmt(l.err_mm, 3) + " mm" : "not found"}</td></tr>`).join("");
  const rp = S.record.reduced_precision
    ? `<p class="hint" style="color:var(--warn)">⚠ reduced-precision input
       (plain image, no metadata)</p>` : "";
  c.innerHTML = `<h2>Stage A — Registration check</h2>${rp}
    <div class="card"><div class="kv">
      <div>rotation</div><div>${fmt(s.rotation_deg, 2)}°</div>
      <div>mirrored</div><div>${s.mirrored}</div>
      <div>scale</div><div>${fmt(s.mm_per_px, 5)} mm/px</div>
      <div>landmark RMS</div><div>${fmt(s.residual_rms_mm, 3)} mm</div>
    </div></div>
    <h3>Ruler central-line verification</h3>
    <table><tr><th>side</th><th>prediction error</th></tr>${lmRows}</table>
    <p class="hint">Check on the image: the red dashed outline must follow the
    phantom edge; rotation/mirroring must be plausible. If detection failed, use
    manual corners.</p>
    <button class="primary" id="btn-confirm-a">Confirm registration ✓</button>
    <button class="secondary" id="btn-manual-corners">Manual corners…</button>`;
  $("#btn-confirm-a").addEventListener("click", async () => {
    status("Detecting patterns…");
    try {
      await postJSON(`api/analyses/${S.aid}/confirm`, { stage: "A" });
      const r = await postJSON(`api/analyses/${S.aid}/propose`, {});
      applyProposal(r);
      setStage("B");
    } catch (e) { status(e.message, true); }
  });
  $("#btn-manual-corners").addEventListener("click", () => {
    S.mode = "corners"; S.manualCorners = [];
    status("Click the 4 phantom corners in order around the square.");
    draw();
  });
}

/* Adopt a fresh proposal, and say plainly where the marks came from.

   The operator must be able to tell a stored layout from a detection at the
   moment the marks appear on screen — that difference is the whole point of
   storing a layout, and the absence of it is how "the marks came back from a
   deleted scan" felt like a bug rather than a feature. */
function applyProposal(r) {
  S.geometry = r.geometry;
  S.layoutSource = r.layout_source || "auto";
  if (r.profile) S.phantomProfile = r.profile;
  noteHistory(r);
  if (r.profile_applied && r.profile) {
    status(`Measuring points loaded from the stored layout for phantom `
           + `${r.profile.phantom} (saved `
           + `${(r.profile.updated_at || "").slice(0, 16)}). Stage C can reset `
           + `them to automatic detection.`);
  } else if (r.profile && r.profile_check && !r.profile_check.ok) {
    status(`The stored layout for phantom ${r.profile.phantom} was NOT applied: `
           + r.profile_check.reason, true);
  } else {
    status("");
  }
}

async function submitManualCorners() {
  S.mode = "normal";
  status("Re-registering with manual corners…");
  try {
    S.reg = await postJSON(`api/analyses/${S.aid}/register`,
      { corners_px: S.manualCorners });
    S.manualCorners = [];
    S.geometry = null;
    setStage("A");
    status("Re-registered.");
  } catch (e) {
    S.manualCorners = [];
    status("Manual registration failed: " + e.message, true);
  }
}

/* ---- Stage B ---- */
/* Detail a confident user wants and a hurried one should not have to wade
   through. A native <details>: it needs no script, survives a re-render, is
   keyboard-accessible, and prints expanded — so the printable report is still
   complete even when the screen is not. */
function advanced(summary, html, open = false) {
  return `<details class="advanced"${open ? " open" : ""}>`
       + `<summary>${summary}</summary>${html}</details>`;
}

function stageB(c) {
  if (!S.geometry) { c.innerHTML = "<p>No proposals yet.</p>"; return; }
  const g = S.geometry;
  const rows = [];
  const missing = [];
  const add = (test, name, ok, extra = "") => {
    if (!ok) missing.push(name);
    rows.push(
      `<tr><td><span class="swatch" style="background:${COLORS[test]}"></span>
       ${name}</td><td>${ok ? "detected" : "NOT refined (nominal used)"}</td>
       <td>${extra}</td></tr>`);
  };
  if (g.linepairs && !g.linepairs._error)
    g.linepairs.groups.forEach(gr =>
      add("linepairs", `line group ${gr.id} (${gr.freq_lp_mm} lp/mm)`, gr.detected));
  if (g.lowcontrast && !g.lowcontrast._error)
    add("lowcontrast", "low-contrast block + 8 circles", g.lowcontrast.detected,
        `angle ${fmt(g.lowcontrast.angle_deg, 1)}°`);
  if (g.wedge && !g.wedge._error)
    add("wedge", "wedge, 7 steps (S1 top … S7 bottom)", g.wedge.detected,
        `x = ${fmt(g.wedge.center_x_mm, 1)} mm`);
  if (g.uniformity && !g.uniformity._error)
    g.uniformity.squares.forEach(sq =>
      add("uniformity", `uniformity square ${sq.id}`, sq.detected,
          sq.outline ? `${fmt(sq.outline.measured_w_mm, 1)}×${fmt(sq.outline.measured_h_mm, 1)} mm` : ""));
  if (g.geometry && !g.geometry._error) {
    Object.entries(g.geometry.rulers).forEach(([side, r]) =>
      add("geometry", `ruler ${side}`, r.detected,
          r.detected ? `${r.line_offsets_mm.length} lines` : ""));
    Object.entries(g.geometry.field_edges).forEach(([side, f]) =>
      add("geometry", `field edge ${side}`, f.detected,
          f.detected ? `${fmt(f.offset_from_edge_mm, 1)} mm outside`
                     : `<span class="hint">${f.reason || "no edge"} — place manually in Stage C if visible</span>`));
  }
  TESTS.forEach(t => {
    if (g[t] && g[t]._error) rows.push(
      `<tr><td>${t}</td><td colspan="2" style="color:var(--fail)">${g[t]._error}</td></tr>`);
  });
  const errored = TESTS.filter(t => g[t] && g[t]._error);
  const total = rows.length;
  const found = total - missing.length;
  let verdict;
  if (errored.length) {
    verdict = `<div class="reasons-why"><b>${errored.join(", ")} could not be
      analysed on this scan.</b> The remaining patterns are still usable.</div>`;
  } else if (!missing.length) {
    verdict = `<div class="reasons-pass"><b>Every pattern was found</b>
      (${total} of ${total}).</div>`;
  } else {
    verdict = `<div class="reasons-why"><b>${found} of ${total} patterns were
      found.</b> These were not, so their nominal positions are being used:
      <ul>${missing.map(m => `<li>${m}</li>`).join("")}</ul></div>`;
  }

  c.innerHTML = `<h2>Stage B — Pattern identification</h2>
    <p class="hint">Look at the image, not at this panel: every pattern found is
    outlined and labelled there. Check that each outline is on the right object
    with the right label — zoom in. A pattern found end-for-end, or a group
    labelled as its neighbour, has to be caught here.</p>
    ${verdict}
    ${advanced("Detection detail — every pattern, with what was measured",
      `<table><tr><th>pattern</th><th>detection</th><th></th></tr>
       ${rows.join("")}</table>`)}
    <button class="primary" id="btn-confirm-b">Verify measuring points →</button>
    <button class="secondary" id="btn-back-a">Back to registration</button>
    <p class="hint">Nothing needs fixing here. Patterns that were not found, or
    were labelled wrongly, are corrected in the next step by dragging and
    rotating their measuring areas.</p>`;
  $("#btn-confirm-b").addEventListener("click", async () => {
    await postJSON(`api/analyses/${S.aid}/confirm`, { stage: "B" });
    setStage("C");
  });
  $("#btn-back-a").addEventListener("click", () => setStage("A"));
}

/* ---- Stage C ---- */

/* Where the measuring points currently come from, and how to change that.

   Two resets rather than one, because "start again" is ambiguous once a
   phantom has a stored layout: the operator has to be able to say whether they
   mean this scan's own detection or the layout confirmed for this phantom. */
/* An analysis somebody has signed off is read-only. Say so before the operator
   drags something and gets a refusal, rather than after. */
function signedOff() {
  return !!(S.record && (S.record.validation_status || "").trim());
}

function lockedBar() {
  const r = S.record || {};
  return `<div class="ident-warn" style="border-radius:6px;margin:8px 0">
      🔒 <b>Signed off${r.validated_by
        ? ` by ${html_escape(r.validated_by)}` : ""}</b>${r.validated_at
        ? ` on ${html_escape(r.validated_at.slice(0, 16))}` : ""} — the
      measuring points, the registration and the results are locked, because
      someone has taken responsibility for these numbers. To rework this
      analysis, withdraw the validation from the identity bar above first.
    </div>`;
}

function layoutBar() {
  if (signedOff()) return lockedBar();
  const prof = S.phantomProfile;
  const fromProfile = S.layoutSource === "profile";
  const phantom = (S.record && S.record.phantom) || "";
  const head = fromProfile
    ? `<b>Measuring points: stored layout for phantom ${html_escape(prof
        ? prof.phantom : phantom)}</b>`
    : `<b>Measuring points: automatic detection on this scan</b>`;
  const meta = fromProfile && prof
    ? `<br>saved ${html_escape((prof.updated_at || "").slice(0, 16))}`
      + (prof.updated_by ? ` by ${html_escape(prof.updated_by)}` : "")
      + ` · ${prof.n_rois} measuring area(s)`
    : (prof
       ? `<br>A stored layout for phantom ${html_escape(prof.phantom)} is `
         + `available (saved ${html_escape((prof.updated_at || "").slice(0, 16))}).`
       : (phantom
          ? `<br>No layout stored for phantom ${html_escape(phantom)} yet — `
            + `confirming this stage will store one.`
          : `<br><span style="color:var(--warn)">No phantom is named, so these `
            + `corrections cannot be reused on the next scan. Add a phantom in `
            + `the identity bar above.</span>`));
  return `<div class="layout-bar ${fromProfile ? "profile" : ""}">
      ${head}${meta}
      <div class="btn-row">
        <button class="secondary-sm" id="btn-undo">↶ Undo</button>
        <button class="secondary-sm" id="btn-redo">↷ Redo</button>
        <button class="secondary-sm" id="btn-reset-auto">Reset to auto-detected</button>
        ${prof ? `<button class="secondary-sm" id="btn-reset-profile">Reset to
          stored layout</button>` : ""}
      </div>
    </div>`;
}

function stageC(c) {
  const fieldBtns = ["top", "right", "bottom", "left"].map(s =>
    `<button class="secondary btn-field" data-side="${s}">${s}</button>`).join(" ");
  const locked = signedOff();
  const lc = (S.geometry && S.geometry.lowcontrast) || null;
  const lcOk = !locked && !!lc && !lc._error && !!lc.block;
  const lcAng = lcOk ? Number(lc.angle_deg || 0) : 0;
  S.lcAngleCommitted = lcAng;
  c.innerHTML = `<h2>Stage C — Measuring points</h2>
    ${layoutBar()}
    <p class="hint">Click an ROI center dot to inspect μ/σ; drag it to adjust.
    Adjusted ROIs turn orange and are recorded in the audit trail. Low-contrast:
    solid = object ROI, dotted = background ROI, dashed = full circle outline.
    Every change can be undone — you never have to re-upload the scan to
    recover from a slip.</p>
    <h3>Low-contrast block</h3>
    <p class="hint">The eight circles sit on a fixed grid inside the block, so
    correcting the block once moves them all. Drag the block outline like any
    ROI, type its angle below, or click its four corners.</p>
    <div class="btn-row">
      <button class="secondary-sm" id="lc-flip" ${lcOk ? "" : "disabled"}
              title="Turn the block end for end — L1 and L8 swap places">
        ⟲ Turn 180°</button>
    </div>
    <p class="hint">Use <b>Turn 180°</b> first if the circles are numbered the
    wrong way round: the block outline is symmetrical, so detection can find it
    end for end, and L1 then sits on L5's disc, L2 on L6's and so on. Every ROI
    still lands on a real disc, so nothing looks wrong on the image — the sign
    is step E reporting that |CNR| is not in design order. Everything below is
    fine adjustment on top of this.</p>
    <div class="rot-row" id="lc-angle-row">
      <label for="lc-angle">fine angle °
        <input type="number" id="lc-angle" step="0.5" min="-180" max="180"
               value="${lcAng.toFixed(1)}" ${lcOk ? "" : "disabled"}></label>
      <button class="secondary-sm" id="lc-angle-minus" ${lcOk ? "" : "disabled"}>−1°</button>
      <button class="secondary-sm" id="lc-angle-plus" ${lcOk ? "" : "disabled"}>+1°</button>
      <button class="secondary-sm" id="lc-angle-apply" ${lcOk ? "" : "disabled"}>Apply</button>
    </div>
    <p class="hint">Degrees in the phantom frame. Typing previews on the image;
    Enter or Apply commits and re-lays all eight circles. [ and ] nudge by 1°.
    The first commit after detection also drops the automatic sub-millimetre
    grid refinement, so the circles can settle up to about 2 mm from the
    preview; after that, preview and result agree exactly.
    ${lcOk ? "" : "<b>The low-contrast block was not proposed on this scan, so "
              + "there is no angle to set.</b>"}</p>
    <button class="secondary" id="btn-block-corners" ${lcOk ? "" : "disabled"}>Click
      4 block corners…</button>

    <h3>Manual field-edge placement</h3>
    <p class="hint">If a field edge was not auto-detected (or looks wrong),
    choose a side and click the visible radiation-field edge on the image.
    Field edges describe the collimation of this exposure, so they are never
    stored as part of the phantom's layout.</p>
    <div>${fieldBtns}</div>
    <button class="primary" id="btn-confirm-c">Measuring points confirmed ✓</button>
    <button class="secondary" id="btn-back-b">Back to patterns</button>`;

  if (!locked) {
    refreshHistoryButtons();
    $("#btn-undo").addEventListener("click", () => stepHistory("undo"));
    $("#btn-redo").addEventListener("click", () => stepHistory("redo"));
    $("#btn-reset-auto").addEventListener("click", () => resetGeometry("auto"));
    const rp = $("#btn-reset-profile");
    if (rp) rp.addEventListener("click", () => resetGeometry("profile"));
  }
  document.querySelectorAll(".btn-field").forEach(
    b => { b.disabled = locked; });

  if (lcOk) {
    const f = $("#lc-angle");
    f.addEventListener("input", () => {
      const v = parseFloat(f.value);
      if (Number.isFinite(v)) previewBlockAngle(v);
    });
    f.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault(); commitBlockAngle(parseFloat(f.value));
      } else if (e.key === "Escape") {
        e.preventDefault();
        previewBlockAngle(S.lcAngleCommitted);
        f.value = S.lcAngleCommitted.toFixed(1);
      }
    });
    $("#lc-angle-apply").addEventListener("click",
      () => commitBlockAngle(parseFloat(f.value)));
    $("#lc-angle-minus").addEventListener("click", () => nudgeBlockAngle(-1));
    $("#lc-angle-plus").addEventListener("click", () => nudgeBlockAngle(+1));
    $("#lc-flip").addEventListener("click", () => flipBlock());
    $("#btn-block-corners").addEventListener("click", () => {
      S.mode = "lccorners"; S.lcCorners = [];
      status("Click the 4 corners of the low-contrast block, in any order.");
      draw();
    });
  }

  document.querySelectorAll(".btn-field").forEach(b =>
    b.addEventListener("click", () => {
      S.mode = "fieldedge"; S.fieldEdgeSide = b.dataset.side;
      status(`Click the radiation-field edge on the ${b.dataset.side} side.`);
    }));
  $("#btn-confirm-c").addEventListener("click", async () => {
    try {
      const r = await postJSON(`api/analyses/${S.aid}/confirm`, { stage: "C" });
      if (r.profile_saved && r.profile) {
        S.phantomProfile = { phantom: r.profile.phantom,
                             updated_at: r.profile.updated_at,
                             updated_by: r.profile.updated_by,
                             n_rois: r.profile.n_rois };
        let msg = `Measuring points stored for phantom ${r.profile.phantom} — `
                + `the next scan of it starts here.`;
        if ((r.profile.near_miss || []).length)
          msg += ` Note: a separate layout also exists for `
               + `“${r.profile.near_miss.join("”, “")}”, which differs only in `
               + `spelling.`;
        status(msg);
      } else if (r.profile_error) {
        status(r.profile_error, true);
      }
      setStage("D");
    } catch (e) { status(e.message, true); }
  });
  $("#btn-back-b").addEventListener("click", () => setStage("B"));
}

async function stepHistory(which) {
  try {
    const r = await postJSON(`api/analyses/${S.aid}/geometry/${which}`, {});
    S.geometry = r.geometry;
    if (r.layout_source) S.layoutSource = r.layout_source;
    noteHistory(r);
    // An undo can revert any part of the tree, so the whole blob is replaced
    // rather than patched ROI by ROI.
    S.selectedRoi = null; S.rotTargetId = null;
    const d = $("#roi-details");
    if (d) d.remove();
    renderStage();
    draw();
    status(which === "undo" ? "Last measuring-point change undone."
                            : "Change redone.");
  } catch (e) {
    if (e.status === 409) { status(e.message); refreshHistoryButtons(); return; }
    status(`${which} failed: ` + e.message, true);
    openAnalysis(S.aid);
  }
}

async function resetGeometry(to) {
  const label = to === "auto" ? "the automatic detection for this scan"
                             : "the stored layout for this phantom";
  if (!confirm(`Reset every measuring point to ${label}?\n\n`
               + `Your manual corrections on this scan are replaced. `
               + `The reset itself can be undone.`)) return;
  status("Resetting measuring points…");
  try {
    const r = await postJSON(`api/analyses/${S.aid}/geometry/reset`, { to });
    S.geometry = r.geometry;
    S.layoutSource = r.layout_source || S.layoutSource;
    if (r.profile) S.phantomProfile = r.profile;
    noteHistory(r);
    S.selectedRoi = null; S.rotTargetId = null;
    const d = $("#roi-details");
    if (d) d.remove();
    renderStage();
    draw();
    status(to === "auto"
      ? "Measuring points reset to the automatic detection. Undo restores your edits."
      : "Measuring points reset to this phantom's stored layout.");
  } catch (e) { status("Reset refused: " + e.message, true); }
}

async function submitFieldEdge(natPoint, side = S.fieldEdgeSide) {
  S.mode = "normal";
  try {
    const f = await postJSON(`api/analyses/${S.aid}/field_edge`,
      { side, point_px: natPoint });
    S.geometry.geometry.field_edges[side] = f;
    noteHistory(f);
    status(`Field edge ${side} set (${fmt(f.offset_from_edge_mm, 1)} mm outside phantom edge).`);
    draw();
    return true;
  } catch (e) { status("Failed: " + e.message, true); return false; }
  finally { S.fieldEdgeSide = null; }
}

/* ---- Stage D ---- */
async function stageD(c) {
  c.innerHTML = `<h2>Stage D — Dimension verification</h2>
    <p>SID <input type="number" id="sid-input" value="${S.sid}" step="10"> mm</p>
    <p class="hint">Computing dimensions…</p>`;
  let gr;
  try {
    const r = await postJSON(`api/analyses/${S.aid}/compute_preview`,
      { tests: ["geometry"], sid_mm: S.sid });
    gr = r.geometry;
    S.dimPreview = gr;
  } catch (e) {
    c.innerHTML += `<p style="color:var(--fail)">${e.message}</p>`;
    return;
  }
  const d = gr.dimensions || {};
  const dimRow = (name, v, nom) => {
    const dev = nom ? (100 * (v - nom) / nom) : null;
    return `<tr><td>${name}</td><td class="num">${fmt(v, 2)}</td>
      <td class="num">${nom ? fmt(nom, 1) : "—"}</td>
      <td class="num" ${dev !== null && Math.abs(dev) > 1 ? 'style="color:var(--fail)"' : ""}>
      ${dev !== null ? dev.toFixed(2) + " %" : "—"}</td></tr>`;
  };
  const nomS = d.nominal_side_mm;
  const dimsHtml = `
    ${dimRow("side top [mm]", d.side_top_mm, nomS)}
    ${dimRow("side right [mm]", d.side_right_mm, nomS)}
    ${dimRow("side bottom [mm]", d.side_bottom_mm, nomS)}
    ${dimRow("side left [mm]", d.side_left_mm, nomS)}
    ${dimRow("diagonal TL–BR [mm]", d.diag_tlbr_mm, nomS * Math.SQRT2)}
    ${dimRow("diagonal TR–BL [mm]", d.diag_trbl_mm, nomS * Math.SQRT2)}`;
  const seps = gr.central_line_separations || {};
  const rulRows = Object.entries(gr.rulers || {}).map(([side, r]) => r.detected ?
    `<tr><td>${side}</td><td class="num">${fmt(r.pitch_mm, 4)}</td>
     <td class="num">${fmt(r.pitch_dev_pct, 2)} %</td>
     <td class="num">${fmt(r.linearity_rms_mm, 3)}</td>
     <td class="num">${fmt(r.central_line_from_edge_mm, 2)}</td></tr>` :
    `<tr><td>${side}</td><td colspan="4">not detected</td></tr>`).join("");
  const sc = gr.scale || {};
  const spac = sc.dicom_spacings_mm_per_px || {};
  const fieldRows = Object.entries(gr.field_alignment || {}).map(([side, f]) =>
    f.detected ?
    `<tr><td>${side}</td><td class="num">${fmt(f.deviation_from_central_line_mm, 1)}</td>
     <td class="num">${fmt(f.pct_of_sid, 2)} %</td><td>${chip(f.status)}</td></tr>` :
    `<tr><td>${side}</td><td colspan="3" class="hint">not measured — ${f.reason || "no edge found"}</td></tr>`).join("");
  const worstDim = d.dev_from_nominal_pct;
  c.innerHTML = `<h2>Stage D — Dimension verification</h2>
    <p class="hint">This step confirms the millimetre scale before anything is
    measured with it. If both lines below are green you can go straight on.</p>
    <div class="card"><div class="kv">
      <div>Phantom size</div>
      <div>${chip(gr.dimension_status)} mean side ${fmt(d.mean_side_mm, 1)} mm
        ${worstDim !== undefined && worstDim !== null
          ? `(${worstDim >= 0 ? "+" : ""}${fmt(worstDim, 2)} % from the assumed
             ${nomS} mm)` : ""}</div>
      <div>X-ray field</div>
      <div>${chip(gr.field_status)} ${fieldSummary(gr)}</div>
    </div></div>
    ${reasonsBlock(gr.dimension_reasons, gr.dimension_status)}
    ${reasonsBlock(gr.field_reasons, gr.field_status)}
    <p>SID <input type="number" id="sid-input" value="${S.sid}" step="10"> mm
       <button class="secondary" id="btn-recompute-d">recompute</button></p>
    <p class="hint">Source-to-image distance, used to express the field
    deviation as a percentage. Change it only if this exposure used a different
    one.</p>

    ${advanced("Measured dimensions — corners, rulers, scale and field",
      `<h3>Corner-mark dimensions ${chip(gr.dimension_status)}</h3>
       <table><tr><th>dimension</th><th>measured</th><th>nominal*</th><th>Δ</th></tr>
       ${dimsHtml}</table>
       <p class="hint">*nominal side ${nomS} mm is assumed design intent
       (no drawing available); calibrated reference is
       ${fmt(d.calibrated_side_mm, 1)} mm.</p>
       <h3>Side-mark rulers (0.5 cm pitch)</h3>
       <table><tr><th>side</th><th>pitch [mm]</th><th>Δpitch</th>
       <th>linearity RMS [mm]</th><th>central line from edge [mm]</th></tr>
       ${rulRows}</table>
       <h3>Central-line separations</h3>
       <table>
         <tr><td>vertical</td><td class="num">${fmt(seps.vertical_mm, 2)} mm</td>
             <td>nominal ${fmt(seps.nominal_mm, 1)} mm</td></tr>
         <tr><td>horizontal</td><td class="num">${fmt(seps.horizontal_mm, 2)} mm</td>
             <td></td></tr></table>
       <h3>Scale cross-check</h3>
       <table>
         <tr><td>tape-pitch measured</td><td class="num">${fmt(sc.pitch_measured_mm, 4)} mm (nominal 5.000)</td></tr>
         <tr><td>absolute scale (pitch-anchored)</td><td class="num">${fmt(sc.absolute_mm_per_px, 5)} mm/px</td></tr>
         <tr><td>DICOM ImagerPixelSpacing</td><td class="num">${fmt(spac.ImagerPixelSpacing, 5)} mm/px</td></tr>
         <tr><td>DICOM PixelSpacing</td><td class="num">${fmt(spac.PixelSpacing, 5)} mm/px</td></tr>
         <tr><td>implied magnification vs detector plane</td>
             <td class="num">${fmt(sc.implied_magnification_vs_detector_plane, 4)}</td></tr>
       </table>
       <h3>X-ray field vs central lines ${chip(gr.field_status)}</h3>
       <table><tr><th>side</th><th>deviation [mm]</th><th>% of SID</th><th></th></tr>
       ${fieldRows}</table>`)}

    <button class="primary" id="btn-confirm-d">Dimensions verified ✓ — run analysis</button>
    <button class="secondary" id="btn-back-c">Back to measuring points</button>`;
  $("#btn-recompute-d").addEventListener("click", () => {
    S.sid = +$("#sid-input").value || 1000;
    stageD(c);
  });
  $("#btn-confirm-d").addEventListener("click", async () => {
    S.sid = +$("#sid-input").value || 1000;
    await postJSON(`api/analyses/${S.aid}/confirm`, { stage: "D" });
    setStage("E");
  });
  $("#btn-back-c").addEventListener("click", () => setStage("C"));
}


/* The field-alignment headline: the worst side, or why there is none. */
function fieldSummary(gr) {
  const sides = Object.entries(gr.field_alignment || {});
  const measured = sides.filter(([, f]) => f.detected);
  if (!measured.length)
    return "not measured — no field edge was visible on any side";
  let worst = measured[0];
  measured.forEach((s) => {
    if (Math.abs(s[1].pct_of_sid) > Math.abs(worst[1].pct_of_sid)) worst = s;
  });
  return `worst side ${worst[0]}, `
       + `${fmt(worst[1].deviation_from_central_line_mm, 1)} mm `
       + `(${fmt(worst[1].pct_of_sid, 2)} % of SID)`
       + (measured.length < sides.length
          ? ` · ${sides.length - measured.length} side(s) not measurable` : "");
}

/* Why a test passed, warned or failed. The status chip alone is not enough to
   troubleshoot with — especially on a phantom the definition does not match. */
function reasonsBlock(reasons, status) {
  const list = (reasons || []).filter(Boolean);
  if (!list.length) return "";
  const cls = status === "pass" ? "reasons-pass" : "reasons-why";
  return `<div class="${cls}"><b>Why ${status || ""}:</b><ul>`
    + list.map(r => `<li>${html_escape(r)}</li>`).join("")
    + `</ul></div>`;
}

/* ---- Stage E ---- */
async function stageE(c) {
  c.innerHTML = `<h2>Stage E — Analysis</h2><p class="hint">Computing…</p>`;
  let r;
  try {
    r = await postJSON(`api/analyses/${S.aid}/compute`, { sid_mm: S.sid });
  } catch (e) {
    c.innerHTML = `<h2>Stage E — Analysis</h2>
      <p style="color:var(--fail)">${e.message}</p>`;
    return;
  }
  S.results = r.results;
  S.baseline = r.baseline;
  const res = r.results;
  // Each entry carries what the summary row needs plus the detail behind it,
  // so the two can never disagree about a test's status.
  const cards = [];
  const card = (key, title, status, headline, html) =>
    cards.push({ key, title, status, headline, html });

  /* line pairs */
  const lp = res.linepairs || {};
  if (lp.rows) {
    const rows = lp.rows.map(row => {
      const lin = row.linearity || {};
      return `<tr><td>${row.id}</td><td class="num">${fmt(row.std, 1)}</td>
        <td class="num">${lin.measured_pitch_mm ? lin.measured_pitch_mm.toFixed(4) : "—"}</td>
        <td class="num">${lin.pitch_dev_pct !== undefined && lin.pitch_dev_pct !== null ? lin.pitch_dev_pct.toFixed(2) + " %" : "—"}</td>
        <td class="num">${lin.residual_rms_mm ? (lin.residual_rms_mm * 1000).toFixed(1) + " µm" : "—"}</td>
        <td>${chip(row.status)}</td></tr>
        ${row.status !== "pass" && row.reason
          ? `<tr class="reason-row"><td colspan="6">${html_escape(row.reason)}</td></tr>`
          : ""}`;
    }).join("");
    const worstLp = lp.rows.filter(x => x.status !== "pass").map(x => x.id);
    card("linepairs", "Line patterns (resolution)", lp.status,
      worstLp.length ? `${worstLp.length} of ${lp.rows.length} group(s) outside `
                       + `tolerance: ${worstLp.join(", ")}`
                     : `all ${lp.rows.length} groups within tolerance`,
      `${reasonsBlock(lp.reasons, lp.status)}
       <table><tr><th>group</th><th>SD</th><th>pitch [mm]</th><th>Δpitch</th>
       <th>grid RMS</th><th></th></tr>${rows}</table>
       <div id="lp-charts"></div>`);
  }

  /* wedge */
  const w = res.wedge || {};
  if (w.rows) {
    const rows = w.rows.map(row =>
      `<tr><td>S${row.step}</td><td class="num">${fmt(row.mean, 1)}</td>
       <td class="num">${fmt(row.std, 1)}</td>
       <td>${row.saturated ? '<span class="chip fail">saturated</span>' : ""}</td></tr>`).join("");
    const sat = w.rows.filter(x => x.saturated).length;
    card("wedge", "Wedge (dynamic range)", w.status,
      `${w.monotonic ? "steps in order" : "STEPS NOT IN ORDER"}, `
      + `range ${fmt(w.dynamic_range_ratio, 1)}×`
      + (sat ? ` · ${sat} step(s) saturated` : ""),
      `${reasonsBlock(w.reasons, w.status)}
       <div class="kv"><div>R² (fit vs step index)</div><div>${fmt(w.fit.r2, 4)}
       (min ${w.r2_min})</div><div>slope</div><div>${fmt(w.fit.slope, 1)} /step</div>
       <div>monotonic</div><div>${w.monotonic}</div></div>
       <canvas class="mini-chart" id="chart-wedge" width="420" height="220"></canvas>
       <table><tr><th>step</th><th>mean</th><th>σ</th><th></th></tr>${rows}</table>`);
  }

  /* low contrast */
  const lc = res.lowcontrast || {};
  if (lc.rows) {
    const rows = lc.rows.map(row =>
      `<tr><td>${row.id}</td><td class="num">${row.cnr.toFixed(3)}</td>
       <td class="num">${fmt(row.obj_mean, 1)}</td>
       <td class="num">${fmt(row.bg_mean, 1)}</td></tr>`).join("");
    const visible = lc.rows.filter(x => Math.abs(x.cnr) >= 0.2).length;
    const orderWarn = lc.ordering_ok ? "" :
      '<p class="hint" style="color:var(--warn)">|CNR| is not in design order. '
      + 'The usual cause is the block having been found end for end — go back to '
      + 'step C and press <b>Turn 180°</b>.</p>';
    card("lowcontrast", "Low contrast (visible discs)", lc.status,
      `${visible} of ${lc.rows.length} discs above CNR 0.2`
      + (lc.ordering_ok ? "" : " · NOT in design order"),
      `${reasonsBlock(lc.reasons, lc.status)}${orderWarn}
       <canvas class="mini-chart" id="chart-lc" width="420" height="200"></canvas>
       <table><tr><th>circle</th><th>CNR</th><th>μ obj</th><th>μ bg</th></tr>
       ${rows}</table>`);
  }

  /* uniformity */
  const u = res.uniformity || {};
  if (u.rows) {
    const rows = u.rows.map(row =>
      `<tr><td>${row.id}</td><td class="num">${fmt(row.mean, 1)}</td>
       <td class="num">${fmt(row.std, 2)}</td><td class="num">${fmt(row.snr, 1)}</td>
       <td class="num">${fmt(row.dsnr_pct, 2)} %</td><td>${chip(row.status)}</td></tr>`).join("");
    card("uniformity", "Uniformity (SNR across the field)", u.status,
      `worst corner ${fmt(u.max_abs_dsnr_pct, 1)} % from the average `
      + `(tolerance ${u.tolerance_pct} %)`,
      `${reasonsBlock(u.reasons, u.status)}
       <table><tr><th>square</th><th>μ</th><th>σ</th><th>SNR</th><th>ΔSNR</th>
       <th></th></tr>${rows}</table>
       <p class="hint">tolerance |ΔSNR| ≤ ${u.tolerance_pct}%</p>`);
  }

  /* geometry + field alignment had no card at all, so a "fail" overall could
     come from a test the user could not see */
  const gm = res.geometry || {};
  if (gm.dimension_status || gm.field_status) {
    const d = gm.dimensions || {};
    card("geometry", "Geometry &amp; dimensions", gm.dimension_status,
      `mean side ${fmt(d.mean_side_mm, 1)} mm `
      + `(${fmt(d.dev_from_nominal_pct, 2)} % from nominal)`,
      `${reasonsBlock(gm.dimension_reasons, gm.dimension_status)}
       <div class="kv">
         <div>mean side</div><div>${fmt(d.mean_side_mm)} mm</div>
         <div>deviation</div><div>${fmt(d.dev_from_nominal_pct)} %</div>
       </div>`);
    card("alignment", "X-ray field alignment", gm.field_status,
      fieldSummary(gm),
      reasonsBlock(gm.field_reasons, gm.field_status)
      || '<p class="hint">No further detail was recorded for this test.</p>');
  }

  /* A test whose geometry carried an _error, or whose compute() raised, comes
     back as {status:"n/a"|"error", error:"…"} with no rows — so none of the
     blocks above pushed a card for it. Without this it vanishes from the
     summary entirely and the verdict below claims every test passed, on a scan
     where a test was never measured at all. */
  const TEST_TITLES = {
    geometry: "Geometry &amp; dimensions",
    linepairs: "Line patterns (resolution)",
    lowcontrast: "Low contrast (visible discs)",
    uniformity: "Uniformity (SNR across the field)",
    wedge: "Wedge (dynamic range)",
  };
  TESTS.forEach(t => {
    if (cards.some(cd => cd.key === t)) return;
    const rr = res[t] || {};
    const why = rr.error || "no result was produced for this test";
    card(t, TEST_TITLES[t] || t, rr.status || "n/a",
      `not analysed — ${html_escape(why)}`,
      `<p style="color:var(--fail)">This test could not be analysed on this
         scan: ${html_escape(why)}</p>
       <p class="hint">Go back to step C and place its measuring areas by hand,
         or repeat the exposure.</p>`);
  });

  const phantomName = (S.record && S.record.phantom) || "";
  const base = r.baseline
    ? `<p class="hint">Compared against the reference for
       ${phantomName ? `phantom <b>${html_escape(phantomName)}</b>` : "this phantom"}
       on this protocol: ${r.baseline.id}
       ${r.baseline.acquired_at
         ? `(${html_escape(r.baseline.acquired_at.slice(0, 16))})` : ""}.
       Differences are shown in the printable report.</p>`
    : `<p class="hint">No reference is stored for
       ${phantomName ? `phantom <b>${html_escape(phantomName)}</b>` : "this phantom"}
       on this protocol yet, so there is nothing to compare against. Mark this
       analysis as the reference in step F if it should become one.</p>`;

  // The summary answers "is anything wrong, and where"; the detail behind each
  // row answers "why". Most operators only ever need the first.
  const summaryRows = cards.map(cd =>
    `<tr><td>${cd.title}</td><td>${chip(cd.status)}</td>
     <td class="hint">${cd.headline}</td></tr>`).join("");
  const attention = cards.filter(cd => cd.status !== "pass");
  const verdict = attention.length
    ? `<div class="reasons-why"><b>Needs attention:</b>
       ${attention.map(cd => cd.title).join(", ")}. The matching section below
       is already open, with the measured values and the reason.</div>`
    : `<div class="reasons-pass"><b>Every test passed.</b> The detail below is
       there if you want it.</div>`;

  const details = cards.map(cd => advanced(
    `${cd.title} ${chip(cd.status)}`, `<div class="card">${cd.html}</div>`,
    cd.status !== "pass")).join("");

  c.innerHTML = `<h2>Stage E — Analysis results</h2>
    <div class="card"><h3>Overall ${chip(r.overall)}</h3>
      <table><tr><th>test</th><th>result</th><th>measured</th></tr>
      ${summaryRows}</table></div>
    ${verdict}
    ${base}
    <h3>Detail per pattern</h3>
    <p class="hint">Anything that did not pass is already open.</p>
    ${details}
    <button class="primary" id="btn-confirm-e">Accept results → save</button>
    <button class="secondary" id="btn-back-d">Back to dimensions</button>`;

  drawWedgeChart(res);
  drawLcChart(res);
  drawLpCharts(res);

  $("#btn-confirm-e").addEventListener("click", async () => {
    await postJSON(`api/analyses/${S.aid}/confirm`, { stage: "E" });
    setStage("F");
  });
  $("#btn-back-d").addEventListener("click", () => setStage("D"));
}

/* The reference this phantom's future scans are compared against.

   Scoped to the phantom AND the protocol: two phantoms can differ by design and
   both be valid, so each needs its own. It is also removable — a reference
   chosen from a scan that later turns out to be poor has to be retractable. */
function baselineBlock(r) {
  const phantom = (r.phantom || "").trim();
  if (r.is_baseline) {
    return `<h3>Reference scan</h3>
      <p><span class="chip pass">★ this is the reference</span> for
      ${phantom ? `phantom <b>${html_escape(phantom)}</b>` : "unlabelled scans"}
      on this protocol.</p>
      <p class="hint">Every later scan of this phantom on this protocol is
      compared against it. Remove it if this scan turned out not to be a good
      reference — the phantom then simply has none until another is chosen.</p>
      <button class="secondary" id="btn-baseline">Remove as reference</button>`;
  }
  if (r.reduced_precision) {
    return `<h3>Reference scan</h3>
      <p class="hint">A reduced-precision analysis cannot be a reference: it is
      8-bit, lossy and carries no acquisition metadata.</p>`;
  }
  return `<h3>Reference scan</h3>
    <p class="hint">Marking this as the reference makes every later scan of
    ${phantom ? `phantom <b>${html_escape(phantom)}</b>` : "this phantom"} on
    this protocol compare against it. Each phantom has its own reference, so
    doing this does not affect any other phantom.${phantom ? ""
      : " <b>Name the phantom first</b>, or the reference will belong to every "
        + "unlabelled scan on this protocol."}</p>
    <button class="secondary" id="btn-baseline">Mark as the reference for
      this phantom</button>`;
}

async function toggleBaseline(value) {
  try {
    const r = await postJSON(`api/analyses/${S.aid}/baseline`,
                             { baseline: value });
    S.record.is_baseline = r.is_baseline ? 1 : 0;
    status(r.is_baseline
      ? `This is now the reference for `
        + `${r.phantom || "unlabelled scans"} on this protocol.`
        + (r.replaced.length
           ? ` It replaced ${r.replaced.join(", ")}.` : "")
      : `Reference removed. ${r.phantom || "This phantom"} has no reference `
        + `until another scan is marked.`);
    renderStage();
  } catch (e) { status("Could not change the reference: " + e.message, true); }
}

/* ---- Stage F ---- */
function stageF(c) {
  const r = S.record || {};
  const identWarn = (!r.site && !r.phantom)
    ? '<p class="hint" style="color:var(--warn)">⚠ This analysis has no site or '
      + 'phantom, so it will not appear in any grouped trend. Use <b>Edit</b> in '
      + 'the identity bar above to add them — you can do this at any time.</p>'
    : "";
  const valBlock = `
    <h3>Validation</h3>
    <p>${valChip(r.validation_status)}${r.validated_by
        ? ` by <b>${html_escape(r.validated_by)}</b>`
          + (r.validated_at ? ` on ${html_escape(r.validated_at.slice(0, 16))}` : "")
        : ""}</p>
    ${r.validation_comment
      ? `<p class="hint">“${html_escape(r.validation_comment)}”</p>` : ""}
    <p class="hint">The administrator decides whether this phantom is accepted.
    The decision, the approver's name and any comment appear at the top of the
    printable report.</p>
    <button class="secondary" id="btn-validate-f">Set validation…</button>`;
  c.innerHTML = `<h2>Stage F — Save &amp; export</h2>
    <p>Analysis <b>${S.aid}</b> stored with full audit trail.</p>
    ${identWarn}
    ${valBlock}
    ${baselineBlock(r)}
    <button class="primary" id="btn-finalize">Finalize</button>
    <h3>Export</h3>
    <p>
      <a href="api/analyses/${S.aid}/report.html" target="_blank">📄 Printable report</a><br>
      <a href="api/analyses/${S.aid}/export.csv" download="phantom_qa_${S.aid}.csv">⬇ CSV (flat metrics)</a><br>
      <a href="api/analyses/${S.aid}/export.json" target="_blank">⬇ JSON (full record)</a>
    </p>
    <h3>Source file integrity</h3>
    <p class="hint">The SHA-256 below fingerprints the exact file these results
    came from. Verifying re-hashes the copy the server kept and reports any
    mismatch — corruption, a wrong restore, or a swapped file.</p>
    <p class="mono" style="font-size:10.5px; word-break:break-all">
      ${(S.record && S.record.sha256) || ""}</p>
    <button class="secondary" id="btn-verify">Verify source file</button>
    <br>
    <button class="secondary" id="btn-new">New analysis</button>`;
  const bl = $("#btn-baseline");
  if (bl) bl.addEventListener("click", () => toggleBaseline(!r.is_baseline));
  $("#btn-finalize").addEventListener("click", async () => {
    try {
      // Finalising no longer touches the baseline: it is its own decision now,
      // and re-finalising must not silently clear a reference.
      await postJSON(`api/analyses/${S.aid}/finalize`, {});
      status("Finalized.");
    } catch (e) { status(e.message, true); }
  });
  $("#btn-validate-f").addEventListener("click", () =>
    setValidation(S.record, (v) => {
      Object.assign(S.record, v);
      renderIdentityBar();
      renderStage();
    }));
  $("#btn-verify").addEventListener("click", () => verifyAnalysis(S.aid));
  $("#btn-new").addEventListener("click", () => {
    clearAnalysisState();
    setStage("U");
    draw();
  });
}

/* ================= mini charts ================= */

function chartAxes(ctx, W, H, pad, xmin, xmax, ymin, ymax) {
  ctx.clearRect(0, 0, W, H);
  ctx.strokeStyle = "#999"; ctx.lineWidth = 1;
  ctx.strokeRect(pad, 8, W - pad - 8, H - pad - 8 - 8 + 8 - 8);
  const sx = (x) => pad + (x - xmin) / (xmax - xmin || 1) * (W - pad - 8);
  const sy = (y) => (H - pad) - (y - ymin) / (ymax - ymin || 1) * (H - pad - 16);
  ctx.fillStyle = "#555"; ctx.font = "10px Segoe UI";
  ctx.fillText(String(Math.round(ymax)), 2, 14);
  ctx.fillText(String(Math.round(ymin)), 2, H - pad);
  return { sx, sy };
}

function drawWedgeChart(res) {
  const cv = $("#chart-wedge");
  if (!cv || !res.wedge || !res.wedge.rows) return;
  const ctx = cv.getContext("2d");
  const rows = res.wedge.rows;
  const xs = rows.map(r => r.step), ys = rows.map(r => r.mean);
  const { sx, sy } = chartAxes(ctx, cv.width, cv.height, 36,
    Math.min(...xs) - 0.5, Math.max(...xs) + 0.5,
    Math.min(...ys) * 0.95, Math.max(...ys) * 1.05);
  const f = res.wedge.fit;
  ctx.strokeStyle = "#d95050"; ctx.setLineDash([5, 4]);
  ctx.beginPath();
  ctx.moveTo(sx(xs[0]), sy(f.slope * xs[0] + f.intercept));
  ctx.lineTo(sx(xs[xs.length - 1]), sy(f.slope * xs[xs.length - 1] + f.intercept));
  ctx.stroke(); ctx.setLineDash([]);
  ctx.fillStyle = "#222";
  rows.forEach(r => {
    ctx.fillRect(sx(r.step) - 3, sy(r.mean) - 3, 6, 6);
  });
  ctx.fillStyle = "#555"; ctx.font = "10px Segoe UI";
  ctx.fillText("step index →", cv.width - 70, cv.height - 4);
}

function drawLcChart(res) {
  const cv = $("#chart-lc");
  if (!cv || !res.lowcontrast || !res.lowcontrast.rows) return;
  const ctx = cv.getContext("2d");
  const rows = res.lowcontrast.rows;
  const ys = rows.map(r => Math.abs(r.cnr));
  const ymax = Math.max(...ys, 0.1) * 1.15;
  const { sx, sy } = chartAxes(ctx, cv.width, cv.height, 36, 0, rows.length,
                               0, ymax);
  const bw = (cv.width - 44) / rows.length * 0.6;
  rows.forEach((r, i) => {
    ctx.fillStyle = "#4a7dbd";
    const x = sx(i + 0.5) - bw / 2, y = sy(Math.abs(r.cnr));
    ctx.fillRect(x, y, bw, sy(0) - y);
    ctx.fillStyle = "#555"; ctx.font = "10px Segoe UI";
    ctx.fillText(r.id, x, cv.height - 22);
  });
  ctx.fillStyle = "#555";
  ctx.fillText("|CNR| by design order", 40, 14);
}

function drawLpCharts(res) {
  const host = $("#lp-charts");
  if (!host || !res.linepairs || !res.linepairs.rows) return;
  res.linepairs.rows.forEach(row => {
    const lin = row.linearity || {};
    if (!lin.profile) return;
    const cv = el("canvas", { class: "mini-chart", width: 420, height: 130 });
    host.appendChild(cv);
    const ctx = cv.getContext("2d");
    const xs = lin.profile.pos_mm, ys = lin.profile.value;
    const { sx, sy } = chartAxes(ctx, cv.width, cv.height, 36,
      xs[0], xs[xs.length - 1], Math.min(...ys), Math.max(...ys));
    /* fitted line positions */
    (lin.grid_peaks_mm || []).forEach(p => {
      ctx.strokeStyle = "rgba(217,80,80,0.55)";
      ctx.beginPath(); ctx.moveTo(sx(p), sy(Math.min(...ys)));
      ctx.lineTo(sx(p), sy(Math.max(...ys))); ctx.stroke();
    });
    ctx.strokeStyle = "#333"; ctx.lineWidth = 0.8;
    ctx.beginPath();
    xs.forEach((x, i) => i === 0 ? ctx.moveTo(sx(x), sy(ys[i]))
                                 : ctx.lineTo(sx(x), sy(ys[i])));
    ctx.stroke();
    ctx.fillStyle = "#555"; ctx.font = "10px Segoe UI";
    ctx.fillText(`${row.id} profile [mm] — red = fitted grid`, 40, 12);
  });
}

/* ================= history & trends ================= */

function showTab(which) {
  $("#tab-analyze").classList.toggle("active", which === "analyze");
  $("#tab-history").classList.toggle("active", which === "history");
  $("#view-analyze").classList.toggle("hidden", which !== "analyze");
  $("#view-history").classList.toggle("hidden", which !== "history");
  if (which === "history") loadHistory();
  if (which === "analyze") resizeCanvas();
}
$("#tab-analyze").addEventListener("click", () => showTab("analyze"));
$("#tab-history").addEventListener("click", () => showTab("history"));

/* current filter + selection state */
const H = { filter: { site: "", phantom: "", signature: "", validation: "" },
            order: "acquired", rows: [] };

/* One date cell. The scanner's clock and this server's clock are independent,
   and neither records a timezone, so an unusable acquisition date is called
   out rather than quietly replaced by the upload time. */
const ACQ_NOTE = {
  missing: "the scanner recorded no acquisition date for this file",
  implausible: "the acquisition date cannot be right — check the scanner clock",
};

function dateCell(a) {
  const flag = a.acquired_flag || "";
  const stamp = (a.acquired_at || "").slice(0, 16);
  if (!flag) return stamp || "<span class='hint'>—</span>";
  return `<span title="${ACQ_NOTE[flag]}" style="color:var(--warn)">`
       + `${stamp || "unknown"} ⚠</span>`;
}

function selectedIds() {
  return [...document.querySelectorAll("#history-table .sel:checked")]
    .map(cb => cb.dataset.id);
}

function filterQuery(extra = {}) {
  const p = new URLSearchParams();
  const ids = selectedIds();
  if (ids.length) p.set("ids", ids.join(","));
  else Object.entries(H.filter).forEach(([k, v]) => { if (v) p.set(k, v); });
  Object.entries(extra).forEach(([k, v]) => p.set(k, v));
  return p.toString();
}

function updateSelectionNote() {
  const n = selectedIds().length;
  $("#selection-note").textContent = n
    ? `${n} row${n > 1 ? "s" : ""} ticked — actions use the ticked rows`
    : "no rows ticked — actions use the filter above";
}

async function loadHistory() {
  const lab = await api("api/labels");
  const fill = (sel, items, cur) => {
    // the values are operator-typed labels; escape the display text the same
    // way the attribute is escaped, or a crafted label injects markup here
    sel.innerHTML = '<option value="">(all)</option>' + items.map(s =>
      `<option value="${s.value.replace(/"/g, "&quot;")}"${s.value === cur ? " selected" : ""}>` +
      `${html_escape(s.value)} (${s.count})</option>`).join("");
  };
  fill($("#f-site"), lab.site || [], H.filter.site);
  fill($("#f-phantom"), lab.phantom || [], H.filter.phantom);
  const sigs = await api("api/signatures");
  fill($("#f-signature"),
       sigs.signatures.map(s => ({ value: s.signature, count: s.count })),
       H.filter.signature);

  const q = new URLSearchParams();
  Object.entries(H.filter).forEach(([k, v]) => { if (v) q.set(k, v); });
  q.set("order", H.order);
  const r = await api("api/analyses?" + q.toString());
  H.rows = r.analyses;
  const flagged = r.analyses.filter(a => a.acquired_flag).length;
  $("#filter-count").textContent =
    `${r.analyses.length} analysis(es) match`
    + (flagged ? ` · ${flagged} with no usable acquisition date` : "");
  $("#f-order").value = H.order;

  const tb = $("#history-table tbody");
  tb.innerHTML = "";
  r.analyses.forEach(a => {
    const tr = el("tr", {}, `
      <td><input type="checkbox" class="sel" data-id="${a.id}"></td>
      <td>${dateCell(a)}</td>
      <td>${(a.created_at || "").slice(0, 16)}</td>
      <td>${a.site ? html_escape(a.site) : "<span class='hint'>—</span>"}</td>
      <td>${a.phantom ? html_escape(a.phantom) : "<span class='hint'>—</span>"}</td>
      <td>${html_escape(a.source_name || "")}${a.reduced_precision ? " ⚠" : ""}</td>
      <td style="font-size:11px">${a.signature ? html_escape(a.signature) : ""}</td>
      <td>${a.stage}</td><td>${chip(a.status)}</td>
      <td>${valChip(a.validation_status)}${a.validated_by
            ? `<br><span class="hint">${html_escape(a.validated_by)}</span>` : ""}</td>
      <td><a href="#" class="base ${a.is_baseline ? "" : "hint"}"
             data-id="${a.id}" data-on="${a.is_baseline ? 1 : 0}"
             title="${a.is_baseline
               ? "The reference for this phantom on this protocol — click to remove"
               : "Make this the reference for this phantom on this protocol"}"
             >${a.is_baseline ? "★" : "☆"}</a></td>
      <td><a href="#" class="open" data-id="${a.id}">open</a> ·
          <a href="api/analyses/${a.id}/report.html" target="_blank">report</a> ·
          <a href="#" class="edit" data-id="${a.id}">label</a> ·
          <a href="#" class="validate" data-id="${a.id}">validate</a> ·
          <a href="#" class="verify" data-id="${a.id}">verify</a> ·
          <a href="#" class="del danger" data-id="${a.id}">delete</a></td>`);
    tb.appendChild(tr);
  });
  tb.querySelectorAll("a.open").forEach(a => a.addEventListener("click", (e) => {
    e.preventDefault();
    openAnalysis(a.dataset.id);
  }));
  tb.querySelectorAll("a.base").forEach(a => a.addEventListener("click", async (e) => {
    e.preventDefault();
    const on = a.dataset.on === "1";
    try {
      const r = await postJSON(`api/analyses/${a.dataset.id}/baseline`,
                               { baseline: !on });
      status(r.is_baseline
        ? `${r.phantom || "Unlabelled scans"} on this protocol now use `
          + `${a.dataset.id} as the reference.`
          + (r.replaced.length ? ` It replaced ${r.replaced.join(", ")}.` : "")
        : `${r.phantom || "This phantom"} has no reference scan now.`);
      if (S.aid === a.dataset.id && S.record) {
        S.record.is_baseline = r.is_baseline ? 1 : 0;
        renderStage();
      }
      loadHistory();
    } catch (err) {
      status("Could not change the reference: " + err.message, true);
    }
  }));
  tb.querySelectorAll("a.del").forEach(a => a.addEventListener("click", async (e) => {
    e.preventDefault();
    await deleteAnalysis(a.dataset.id);
  }));
  tb.querySelectorAll("a.verify").forEach(a => a.addEventListener("click", async (e) => {
    e.preventDefault();
    await verifyAnalysis(a.dataset.id);
  }));
  tb.querySelectorAll("a.validate").forEach(a => a.addEventListener("click", async (e) => {
    e.preventDefault();
    const rec = H.rows.find(x => x.id === a.dataset.id) || { id: a.dataset.id };
    await setValidation(rec, () => {
      if (S.aid === rec.id && S.record) openAnalysis(rec.id);
      loadHistory();
    });
  }));
  tb.querySelectorAll("a.edit").forEach(a => a.addEventListener("click", async (e) => {
    e.preventDefault();
    const rec = H.rows.find(x => x.id === a.dataset.id) || {};
    const vals = await editLabelsDialog(rec, `Identification — ${rec.id}`);
    if (!vals) return;
    try {
      const out = await postJSON(`api/analyses/${a.dataset.id}/labels`, vals);
      if (out.layout_deleted)
        status(`Renamed. The stored measuring-point layout for phantom `
               + `${out.phantom_before} was discarded — no analyses carry that `
               + `name any more.`);
      if (S.aid === a.dataset.id && S.record) {
        Object.assign(S.record, vals);
        await refreshProfileForRecord();
        renderIdentityBar();
        renderStage();
      }
      loadHistory();
    } catch (err) { status("Could not save: " + err.message, true); }
  }));
  tb.querySelectorAll(".sel").forEach(cb =>
    cb.addEventListener("change", () => { updateSelectionNote(); loadTrends(); }));
  $("#sel-all").checked = false;
  updateSelectionNote();
  loadTrends();
}

["site", "phantom", "signature", "validation"].forEach(k => {
  $("#f-" + k).addEventListener("change", (e) => {
    H.filter[k] = e.target.value;
    loadHistory();
  });
});
$("#f-order").addEventListener("change", (e) => {
  H.order = e.target.value === "uploaded" ? "uploaded" : "acquired";
  loadHistory();
});
$("#btn-clear-filter").addEventListener("click", () => {
  H.filter = { site: "", phantom: "", signature: "", validation: "" };
  $("#f-validation").value = "";
  loadHistory();
});
$("#sel-all").addEventListener("change", (e) => {
  document.querySelectorAll("#history-table .sel").forEach(
    cb => { cb.checked = e.target.checked; });
  updateSelectionNote();
  loadTrends();
});

$("#btn-comparison").addEventListener("click", () => {
  const q = filterQuery();
  if (!q) { alert("Pick a site/phantom filter or tick some rows first."); return; }
  window.open("api/comparison_report.html?" + q, "_blank");
});
$("#btn-export-long").addEventListener("click", () => {
  window.location = "api/export.csv?" + filterQuery({ layout: "long" });
});
$("#btn-export-wide").addEventListener("click", () => {
  window.location = "api/export.csv?" + filterQuery({ layout: "wide" });
});

let trendData = null;
let trendLayout = null;          // point positions of the last draw, for hover
let trendWired = false;

/* A trend line through scans of DIFFERENT phantoms is not a trend — two
   builds legitimately differ, so the line would show assembly differences as
   if they were drift. The chart therefore draws nothing until the operator
   has said which scans belong together: the Phantom filter, or ticked rows. */
function trendSelectionMissing() {
  return !H.filter.phantom && !selectedIds().length;
}

//: X labels are only drawn as densely as they stay readable. ~80 px fits a
//: full YYYY-MM-DD at the chart's 11 px face with air on both sides.
const TREND_MIN_XLABEL_PX = 80;
const TREND_H = 340;
const TREND_PAD = { l: 64, r: 18, t: 30, b: 34 };

const cssVar = (name) =>
  getComputedStyle(document.documentElement).getPropertyValue(name).trim();

async function loadTrends() {
  const sect = $("#trend-section");
  if (sect && !sect.open) return;        // collapsed: fetch nothing
  const msel = $("#trend-metric");
  const cv = $("#trend-chart");

  if (trendSelectionMissing()) {
    trendData = null;
    trendLayout = null;
    msel.innerHTML = "";
    sizeTrendCanvas(cv);
    drawTrendMessage(cv,
      "Pick a Phantom in the filter above, or tick rows in the table.",
      "A line through different phantoms would show their assembly "
      + "differences as if they were drift over time.");
    $("#trend-note").textContent = "";
    return;
  }

  const q = filterQuery();
  try {
    trendData = await api("api/trends?" + q);
  } catch (e) { trendData = null; }
  if (!trendData || !trendData.analyses.length) {
    msel.innerHTML = "";
    trendLayout = null;
    sizeTrendCanvas(cv);
    drawTrendMessage(cv, "No completed analyses in this selection.",
      "Only analyses whose results were computed appear in a trend.");
    $("#trend-note").textContent = "";
    return;
  }
  const metrics = new Set();
  trendData.analyses.forEach(a => a.rows.forEach(row =>
    metrics.add(`${row.test} | ${row.object} | ${row.metric}`)));
  const prev = msel.value;
  const opts = [...metrics].sort();
  msel.innerHTML = opts.map(m =>
    `<option${m === prev ? " selected" : ""}>${m}</option>`).join("");
  msel.onchange = drawTrend;
  $("#trend-axis").onchange = drawTrend;
  wireTrendHover(cv);
  drawTrend();
}

/* Crisp on any display: the bitmap is sized to the element times the device
   pixel ratio. The old fixed 1000-px bitmap was CSS-scaled to fit, which
   blurred every label and let the rotated dates run off the bottom edge. */
function sizeTrendCanvas(cv) {
  // The layout width stays CSS's business ("100%"): pinning an inline pixel
  // width measured from the PARENT's clientWidth included the section's
  // padding, so the canvas came out wider than the space it sits in and
  // overflowed the container on the right. Only the BITMAP is sized here,
  // to the content box the canvas actually got (clientWidth excludes the
  // element's own border).
  cv.style.width = "100%";
  cv.style.height = TREND_H + "px";
  const cssW = Math.max(420, cv.clientWidth ||
                        (cv.parentElement.clientWidth - 24));
  const dpr = window.devicePixelRatio || 1;
  cv.width = Math.round(cssW * dpr);
  cv.height = Math.round(TREND_H * dpr);
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w: cssW, h: TREND_H };
}

let trendMessage = null;             // last placeholder, for resize redraws

function drawTrendMessage(cv, line1, line2) {
  trendMessage = [line1, line2];
  const { ctx, w, h } = sizeTrendCanvas(cv);
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = cssVar("--bg");
  ctx.fillRect(0, 0, w, h);
  ctx.fillStyle = cssVar("--muted");
  ctx.textAlign = "center";
  ctx.font = "600 13px 'Segoe UI', system-ui, sans-serif";
  ctx.fillText(line1, w / 2, h / 2 - 10);
  ctx.font = "12px 'Segoe UI', system-ui, sans-serif";
  ctx.fillText(line2, w / 2, h / 2 + 12);
  ctx.textAlign = "left";
}

/* Round axis bounds to 1/2/5 steps so tick values read like numbers a person
   would choose, not like float noise. */
function niceTicks(lo, hi, target = 5) {
  const span = hi - lo || Math.abs(hi) || 1;
  const raw = span / target;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 5, 10].map(m => m * mag).find(st => span / st <= target)
            || 10 * mag;
  const first = Math.ceil(lo / step) * step;
  const out = [];
  for (let v = first; v <= hi + step * 1e-9; v += step) out.push(v);
  return out;
}

const fmtTick = (v) => {
  const a = Math.abs(v);
  if (a >= 10000 || (a > 0 && a < 0.01)) return v.toExponential(1);
  return (+v.toPrecision(4)).toLocaleString("en-US");
};

function drawTrend() {
  if (!trendData) return;
  const key = ($("#trend-metric").value || "").split(" | ");
  const axis = ($("#trend-axis") || {}).value || "acquired";
  // Which clock orders the series. The acquisition axis still falls back to
  // the upload time for a scan whose header carried no date, but marks it so
  // a run of such points cannot be mistaken for a real chronology.
  const stampOf = (a) => (axis === "uploaded"
    ? (a.created_at || "")
    : (a.acquired_at || a.created_at || "")).slice(0, 16);

  const pts = [];
  // Each phantom has its own reference, so a manual selection spanning
  // several can contain several. The +/-20 % band is only meaningful around
  // exactly one.
  const baseVals = [];
  const ordered = trendData.analyses.slice().sort(
    (p, q) => (stampOf(p) < stampOf(q) ? -1 : stampOf(p) > stampOf(q) ? 1 : 0));
  ordered.forEach(a => {
    const row = a.rows.find(r => r.test === key[0] && r.object === key[1]
                                 && r.metric === key[2]);
    if (row && typeof row.value === "number" && isFinite(row.value)) {
      const flagged = axis !== "uploaded" && !!a.acquired_flag;
      pts.push({ stamp: stampOf(a), y: row.value, id: a.id,
                 baseline: !!a.is_baseline, flagged,
                 who: [a.site, a.phantom].filter(Boolean).join(" / ") });
      if (a.is_baseline) baseVals.push(row.value);
    }
  });

  const cv = $("#trend-chart");
  const { ctx, w, h } = sizeTrendCanvas(cv);
  const C = {
    bg: cssVar("--bg"), panel: cssVar("--panel2"), border: cssVar("--border"),
    text: cssVar("--text"), muted: cssVar("--muted"),
    accent: cssVar("--accent"), pass: cssVar("--pass"), warn: cssVar("--warn"),
  };
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = C.bg;
  ctx.fillRect(0, 0, w, h);
  if (!pts.length) {
    drawTrendMessage(cv, "No values for this metric in the selection.", "");
    trendLayout = null;
    return;
  }

  const baseVal = baseVals.length === 1 ? baseVals[0] : null;
  const ys = pts.map(p => p.y);
  let ymin = Math.min(...ys), ymax = Math.max(...ys);
  if (baseVal !== null) {
    ymin = Math.min(ymin, baseVal * 0.78);
    ymax = Math.max(ymax, baseVal * 1.22);
  }
  const spread = (ymax - ymin) || Math.abs(ymax) * 0.2 || 1;
  ymin -= spread * 0.08; ymax += spread * 0.08;

  const plotW = w - TREND_PAD.l - TREND_PAD.r;
  const plotH = h - TREND_PAD.t - TREND_PAD.b;
  const sx = (i) => TREND_PAD.l + (pts.length === 1 ? plotW / 2
    : i / (pts.length - 1) * plotW);
  const sy = (y) => TREND_PAD.t + plotH - (y - ymin) / (ymax - ymin) * plotH;
  const font = (px, weight = "") =>
    `${weight ? weight + " " : ""}${px}px 'Segoe UI', system-ui, sans-serif`;

  // horizontal gridlines on nice values, labels in the left gutter
  ctx.font = font(11);
  ctx.textBaseline = "middle";
  niceTicks(ymin, ymax).forEach(v => {
    const y = sy(v);
    ctx.strokeStyle = C.border;
    ctx.globalAlpha = 0.45;
    ctx.beginPath(); ctx.moveTo(TREND_PAD.l, y);
    ctx.lineTo(w - TREND_PAD.r, y); ctx.stroke();
    ctx.globalAlpha = 1;
    ctx.fillStyle = C.muted;
    ctx.textAlign = "right";
    ctx.fillText(fmtTick(v), TREND_PAD.l - 8, y);
  });

  // the constancy band around the single reference
  if (baseVal !== null) {
    const yTop = sy(1.2 * baseVal), yBot = sy(0.8 * baseVal);
    ctx.fillStyle = C.pass;
    ctx.globalAlpha = 0.08;
    ctx.fillRect(TREND_PAD.l, yTop, plotW, yBot - yTop);
    ctx.globalAlpha = 0.6;
    ctx.strokeStyle = C.pass;
    ctx.setLineDash([5, 4]);
    [yTop, yBot].forEach(y => {
      ctx.beginPath(); ctx.moveTo(TREND_PAD.l, y);
      ctx.lineTo(w - TREND_PAD.r, y); ctx.stroke();
    });
    ctx.setLineDash([]);
    ctx.globalAlpha = 1;
  }

  // soft area fill under the series, then the line itself
  if (pts.length > 1) {
    const grad = ctx.createLinearGradient(0, TREND_PAD.t, 0, h - TREND_PAD.b);
    grad.addColorStop(0, C.accent + "2e");
    grad.addColorStop(1, C.accent + "00");
    ctx.beginPath();
    pts.forEach((p, i) => i ? ctx.lineTo(sx(i), sy(p.y))
                            : ctx.moveTo(sx(0), sy(p.y)));
    ctx.lineTo(sx(pts.length - 1), h - TREND_PAD.b);
    ctx.lineTo(sx(0), h - TREND_PAD.b);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();
    ctx.beginPath();
    pts.forEach((p, i) => i ? ctx.lineTo(sx(i), sy(p.y))
                            : ctx.moveTo(sx(0), sy(p.y)));
    ctx.strokeStyle = C.accent;
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    ctx.stroke();
    ctx.lineWidth = 1;
  }

  // points: smaller and unstroked in a dense series, ringed when sparse
  const dense = pts.length > 120;
  pts.forEach((p, i) => {
    const r = p.baseline ? 5 : (dense ? 2 : 3.5);
    ctx.beginPath();
    ctx.arc(sx(i), sy(p.y), r, 0, Math.PI * 2);
    ctx.fillStyle = p.baseline ? C.pass : (p.flagged ? C.warn : C.accent);
    ctx.fill();
    if (!dense || p.baseline) {
      ctx.strokeStyle = C.bg;
      ctx.lineWidth = 1.5;
      ctx.stroke();
      ctx.lineWidth = 1;
    }
  });

  // x labels: horizontal, thinned so neighbours never collide, always the
  // first; later ones only where a full label fits
  const maxTicks = Math.max(2, Math.floor(plotW / TREND_MIN_XLABEL_PX));
  const every = Math.max(1, Math.ceil(pts.length / maxTicks));
  ctx.fillStyle = C.muted;
  ctx.font = font(11);
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  let lastX = -1e9;
  pts.forEach((p, i) => {
    if (i % every !== 0 && i !== pts.length - 1) return;
    const x = sx(i);
    if (x - lastX < TREND_MIN_XLABEL_PX * 0.9) return;
    lastX = x;
    ctx.strokeStyle = C.border;
    ctx.beginPath(); ctx.moveTo(x, h - TREND_PAD.b);
    ctx.lineTo(x, h - TREND_PAD.b + 4); ctx.stroke();
    // The tick stays on its point, but the TEXT is clamped inside the canvas:
    // the last point sits at the plot's right edge, and a label centred there
    // hangs half outside — the rightmost date was always cut.
    const text = p.stamp.slice(0, 10);
    const half = ctx.measureText(text).width / 2;
    const lx = Math.min(Math.max(x, half + 2), w - half - 2);
    ctx.fillText(text, lx, h - TREND_PAD.b + 8);
  });

  // header: the metric on the left, the series size on the right
  ctx.textAlign = "left";
  ctx.textBaseline = "alphabetic";
  ctx.fillStyle = C.text;
  ctx.font = font(12, "600");
  ctx.fillText($("#trend-metric").value || "", TREND_PAD.l, 18);
  ctx.fillStyle = C.muted;
  ctx.font = font(11);
  const nText = `${pts.length} scan${pts.length === 1 ? "" : "s"}`;
  ctx.textAlign = "right";
  ctx.fillText(nText, w - TREND_PAD.r, 18);
  ctx.textAlign = "left";

  trendMessage = null;
  trendLayout = { pts, w, h, sx: pts.map((_, i) => sx(i)),
                  sy: pts.map(p => sy(p.y)) };

  const note = $("#trend-note");
  if (note) {
    note.textContent = baseVals.length > 1
      ? `${baseVals.length} reference scans in this selection (one per phantom),`
        + " so no tolerance band is drawn — filter to a single phantom to see it."
      : (baseVals.length ? "" : "No reference scan in this selection.");
  }
}

/* Hover: the nearest point gets a crosshair and a card with the exact value —
   with a hundred points on screen, reading numbers off the line is guesswork. */
function wireTrendHover(cv) {
  if (trendWired) return;
  trendWired = true;
  cv.addEventListener("mousemove", (ev) => {
    if (!trendLayout) return;
    drawTrend();                                    // clean frame
    const L = trendLayout;
    const rect = cv.getBoundingClientRect();
    const mx = ev.clientX - rect.left;
    let best = -1, bestD = 24;
    L.sx.forEach((x, i) => {
      const d = Math.abs(x - mx);
      if (d < bestD) { bestD = d; best = i; }
    });
    if (best < 0) return;
    const ctx = cv.getContext("2d");
    const p = L.pts[best], x = L.sx[best], y = L.sy[best];
    const C = { border: cssVar("--border"), panel: cssVar("--panel"),
                text: cssVar("--text"), muted: cssVar("--muted"),
                accent: cssVar("--accent"), pass: cssVar("--pass"),
                warn: cssVar("--warn") };
    ctx.strokeStyle = C.border;
    ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(x, TREND_PAD.t);
    ctx.lineTo(x, L.h - TREND_PAD.b); ctx.stroke();
    ctx.setLineDash([]);
    ctx.beginPath(); ctx.arc(x, y, 6, 0, Math.PI * 2);
    ctx.strokeStyle = p.baseline ? C.pass : C.accent;
    ctx.lineWidth = 2; ctx.stroke(); ctx.lineWidth = 1;

    const lines = [
      p.stamp,
      fmtTick(p.y),
      p.who || "(unlabelled)",
      p.baseline ? "★ reference scan" : "",
      p.flagged ? "⚠ acquisition date unreliable" : "",
    ].filter(Boolean);
    ctx.font = "11px 'Segoe UI', system-ui, sans-serif";
    const bw = Math.max(...lines.map(t => ctx.measureText(t).width)) + 20;
    const bh = lines.length * 16 + 12;
    let bx = x + 12, by = Math.max(TREND_PAD.t, y - bh - 10);
    if (bx + bw > L.w - 4) bx = x - bw - 12;
    ctx.fillStyle = C.panel;
    ctx.strokeStyle = C.border;
    ctx.beginPath();
    if (ctx.roundRect) ctx.roundRect(bx, by, bw, bh, 6);
    else ctx.rect(bx, by, bw, bh);       // pre-2022 browsers: square corners
    ctx.fill(); ctx.stroke();
    lines.forEach((t, i) => {
      ctx.fillStyle = i === 1 ? C.text : C.muted;
      ctx.font = i === 1 ? "600 12px 'Segoe UI', system-ui, sans-serif"
                         : "11px 'Segoe UI', system-ui, sans-serif";
      ctx.fillText(t, bx + 10, by + 18 + i * 16);
    });
  });
  cv.addEventListener("mouseleave", () => { if (trendLayout) drawTrend(); });
}

// Redraw at the new width when the window changes, and populate lazily the
// first time the collapsed section is opened.
window.addEventListener("resize", () => {
  const sect = $("#trend-section");
  if (!sect || !sect.open) return;
  if (trendData) drawTrend();
  else if (trendMessage) drawTrendMessage($("#trend-chart"), ...trendMessage);
});
const _trendSection = $("#trend-section");
if (_trendSection) {
  _trendSection.addEventListener("toggle", () => {
    if (_trendSection.open) loadTrends();
  });
}

/* ================= sign-out ================= */

async function initAuth() {
  try {
    const a = await api("api/auth");
    if (a.auth_enabled) {
      if (!a.authenticated) { window.location = "login"; return; }
      const btn = el("button", { id: "logout-btn", class: "tab" },
                     `Sign out (${a.user || ""})`);
      btn.addEventListener("click", async () => {
        await postJSON("api/logout", {});
        window.location = "login";
      });
      $("header nav").appendChild(btn);
    }
  } catch (e) { /* auth endpoint unavailable — leave UI as is */ }
}

/* ================= init ================= */
renderToggles();
setStage("U");
resizeCanvas();
initAuth();
