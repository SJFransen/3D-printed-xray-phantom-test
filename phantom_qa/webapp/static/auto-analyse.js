/* Optional Auto Analyze UI. No wizard state, positioning controls or QA rules. */
(() => {
  "use strict";
  const page = document.querySelector("#view-auto-analyse");
  if (!page) return;
  const get = id => document.getElementById(id);
  const form = get("auto-upload-form");
  const viewer = new ViewerTools.ImageViewer({
    viewport: get("auto-viewer"), layer: get("auto-viewer-layer"), image: get("auto-image"),
    controls: {
      width: get("auto-wl-width"), center: get("auto-wl-center"), readout: get("auto-wl-readout"),
      auto: get("auto-wl-reset"), fit: get("auto-zoom-fit"),
      zoomIn: get("auto-zoom-in"), zoomOut: get("auto-zoom-out"),
    },
    onLoad: () => {
      get("auto-image-error").classList.add("hidden");
      get("auto-viewer").classList.remove("hidden");
    },
    onError: () => get("auto-image-error").classList.remove("hidden"),
  });
  let analyses = [], activeIndex = 0, busy = false;
  const make = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text) node.textContent = text;
    if (className) node.className = className;
    return node;
  };

  async function request(path, options = {}) {
    const match = document.cookie.match(/(?:^|;\s*)phantomqa_csrf=([^;]+)/);
    if (options.method === "POST") {
      options.headers = {"X-CSRF-Token": match ? decodeURIComponent(match[1]) : ""};
    }
    const response = await fetch(path, {credentials: "same-origin", ...options});
    if (response.status === 401) {
      window.location.href = "login";
      throw new Error("Your session expired. Sign in again.");
    }
    let data;
    try { data = await response.json(); }
    catch (_) { throw new Error("The server did not return a result. Check History before uploading again."); }
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "The analysis could not be completed.");
    return data;
  }

  const upload = UploadForm.mount(get("auto-upload-fields"), {
    prefix: "auto", request,
    onUpload: () => { if (!busy) form.requestSubmit(); },
  });
  let refreshVersion = 0, dirty = false;
  async function refreshSavedResult() {
    const index = activeIndex, current = analyses[index], version = ++refreshVersion;
    if (!current) return;
    try {
      const record = await request(`api/auto-analyse/${encodeURIComponent(current.id)}?duplicate=${current.duplicate}`);
      if (version !== refreshVersion || analyses[index]?.id !== current.id) return;
      analyses[index] = record; dirty = false;
      if (index === activeIndex && !get("auto-results-page").classList.contains("hidden")) renderResult(index);
    } catch (_) { /* Keep the saved preview available; opening Analyze can retry the record. */ }
  }
  document.addEventListener("phantomqa:analysis-changed", event => {
    if (!analyses.some(record => record.id === event.detail)) return;
    dirty = true;
    if (!page.classList.contains("hidden")) refreshSavedResult();
  });
  document.addEventListener("phantomqa:tab", event => {
    page.classList.toggle("hidden", event.detail !== "auto-analyse");
    get("tab-auto-analyse").classList.toggle("active", event.detail === "auto-analyse");
    if (event.detail === "auto-analyse") {
      upload.loadLabels();
      if (dirty) refreshSavedResult();
    }
  });
  get("tab-auto-analyse").addEventListener("click", () => {
    get("view-analyze").classList.add("hidden");
    get("view-history").classList.add("hidden");
    get("tab-analyze").classList.remove("active");
    get("tab-history").classList.remove("active");
    document.dispatchEvent(new CustomEvent("phantomqa:tab", {detail: "auto-analyse"}));
  });

  function renderTasks(tasks) {
    get("auto-tasks").replaceChildren();
    for (const task of [...tasks].sort((a, b) => (b.priority === "high") - (a.priority === "high"))) {
      const card = make("article", "", "auto-task");
      card.dataset.taskId = task.id; card.dataset.taskType = task.type;
      card.dataset.status = task.status; card.dataset.priority = task.priority;
      if (task.related_feature) card.dataset.feature = task.related_feature;
      card.append(make("span", task.description || task.title));
      if (task.steps && task.steps.length) {
        const steps = make("ol");
        task.steps.forEach(step => steps.append(make("li", step))); card.append(steps);
      }
      for (const reference of task.image_references || []) {
        // Image references are restricted to this application or embedded images.
        const url = new URL(reference.url, document.baseURI);
        if (url.origin !== window.location.origin && url.protocol !== "data:") continue;
        const img = make("img"); img.src = url.href; img.alt = reference.description || "Task illustration";
        card.append(img);
      }
      get("auto-tasks").append(card);
    }
  }

  function renderOverlays(record) {
    const svg = get("auto-overlays");
    svg.replaceChildren(); svg.setAttribute("viewBox", `0 0 ${record.image.cols} ${record.image.rows}`);
    const radius = Math.max(record.image.cols, record.image.rows) / 125;
    for (const feature of record.features) {
      const color = feature.state === "pass" ? "#65df8b" : "#ffb45c";
      const add = (tag, attrs) => {
        const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
        Object.entries({fill: "none", stroke: color, "stroke-width": 1.4, ...attrs})
          .forEach(([key, value]) => node.setAttribute(key, value));
        const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
        title.textContent = `${feature.label}: ${feature.state === "pass" ? "PASS" : "Attention required"}`;
        node.append(title); svg.append(node);
      };
      for (const roi of feature.overlays) {
        if (roi.type === "rect") add("polygon", {points: roi.corners_px.map(p => p.join(",")).join(" ")});
        else if (roi.type === "segment") add("line", {x1: roi.p0_px[0], y1: roi.p0_px[1], x2: roi.p1_px[0], y2: roi.p1_px[1]});
        else if (roi.type === "point") add("circle", {cx: roi.center_px[0], cy: roi.center_px[1], r: radius});
        else if (roi.type === "circle") add("circle", {cx: roi.center_px[0], cy: roi.center_px[1], r: roi.radius_px});
        else if (roi.type === "annulus") {
          [roi.inner_radius_px, roi.outer_radius_px].forEach(r => add("circle", {cx: roi.center_px[0], cy: roi.center_px[1], r}));
        }
      }
    }
  }

  function renderResult(index) {
    activeIndex = index;
    const record = analyses[index];
    get("auto-identity").textContent = [record.site, record.phantom, record.operator].filter(Boolean).join(" · ");
    get("auto-result").classList.remove("hidden");
    get("auto-image-error").classList.add("hidden");
    // Hide the old image and overlays until the new image has loaded.
    get("auto-viewer").classList.add("hidden");
    renderOverlays(record);
    viewer.setScan(record);
    const statuses = get("auto-feature-status"); statuses.replaceChildren();
    for (const feature of record.features) {
      const row = make("li", feature.label,
                       feature.state === "pass" ? "auto-pass" : "auto-attention");
      row.setAttribute("aria-label", `${feature.label}: ${feature.state === "pass" ? "PASS" : "Attention"}`);
      statuses.append(row);
    }
    renderTasks(record.tasks);
  }

  get("auto-new-scan").addEventListener("click", () => {
    if (busy) return;
    viewer.clear();
    get("auto-results-page").classList.add("hidden");
    form.classList.remove("hidden");
    refreshVersion++; upload.reset();
    get("auto-error").classList.add("hidden");
  });
  get("auto-open-analyze").addEventListener("click", () => {
    const record = analyses[activeIndex];
    if (record && !busy) document.dispatchEvent(new CustomEvent("phantomqa:open-analysis", {detail: record.id}));
  });
  get("auto-scan-select").addEventListener("change", event => renderResult(Number(event.target.value)));
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (busy || !upload.nodes.file.files.length || !form.reportValidity()) return;
    viewer.clear();
    busy = true;
    const data = upload.data();
    form.classList.add("hidden");
    get("auto-results-page").classList.remove("hidden");
    get("auto-processing").classList.remove("hidden");
    get("auto-new-scan").disabled = true;
    get("auto-open-analyze").disabled = true;
    get("auto-result").classList.add("hidden");
    get("auto-tasks").replaceChildren();
    get("auto-error").classList.add("hidden");
    form.setAttribute("aria-busy", "true");
    upload.setBusy(true);
    get("auto-progress").textContent = "Analyzing…";
    try {
      const response = await request("api/auto-analyse", {method: "POST", body: data});
      analyses = response.analyses;
      if (!analyses.length) throw new Error("No scan images were found in this upload.");
      const select = get("auto-scan-select");
      select.replaceChildren(...analyses.map((record, index) => {
        const option = make("option", `${record.source_name} — ${record.overall === "pass" ? "PASS" : "Attention required"}`);
        option.value = index; return option;
      }));
      get("auto-scan-choice").classList.toggle("hidden", analyses.length < 2);
      renderResult(0);
      get("auto-progress").textContent = `Analysis complete. ${analyses.length} scan${analyses.length === 1 ? "" : "s"} processed.`;
      upload.loadLabels();
    } catch (error) {
      get("auto-results-page").classList.add("hidden");
      form.classList.remove("hidden");
      get("auto-error").textContent = "The upload could not be completed. Check History before trying again.";
      get("auto-error").classList.remove("hidden");
    } finally {
      busy = false; form.removeAttribute("aria-busy");
      get("auto-processing").classList.add("hidden");
      get("auto-new-scan").disabled = false;
      get("auto-open-analyze").disabled = false;
      upload.setBusy(false);
    }
  });
})();
