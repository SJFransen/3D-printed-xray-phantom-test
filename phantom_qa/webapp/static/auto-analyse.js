/* Optional Auto Analyze UI. No wizard state, positioning controls or QA rules. */
(() => {
  "use strict";
  const page = document.querySelector("#view-auto-analyse");
  if (!page) return;
  const get = id => document.getElementById(id);
  const form = get("auto-upload-form");
  let analyses = [], activeIndex = 0, busy = false, labelsLoaded = false;
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

  async function loadLabels() {
    if (labelsLoaded) return;
    try {
      const labels = await request("api/labels");
      for (const [key, id] of [["site", "auto-sites"], ["phantom", "auto-phantoms"]]) {
        const list = get(id);
        list.replaceChildren(...(labels[key] || []).map(item => {
          const option = make("option"); option.value = item.value; return option;
        }));
      }
      labelsLoaded = true;
    } catch (_) { /* Identification remains optional when suggestions fail. */ }
  }

  // A DOM event is the only connection to the existing navigation.
  // Removing this script restores Analyze/History without changing app.js.
  document.addEventListener("phantomqa:tab", event => {
    page.classList.toggle("hidden", event.detail !== "auto-analyse");
    get("tab-auto-analyse").classList.toggle("active", event.detail === "auto-analyse");
    if (event.detail === "auto-analyse") loadLabels();
  });
  get("tab-auto-analyse").addEventListener("click", () => {
    get("view-analyze").classList.add("hidden");
    get("view-history").classList.add("hidden");
    get("tab-analyze").classList.remove("active");
    get("tab-history").classList.remove("active");
    document.dispatchEvent(new CustomEvent("phantomqa:tab", {detail: "auto-analyse"}));
  });
  loadLabels();
  for (const key of ["site", "phantom", "operator"]) {
    form.elements[key].value = sessionStorage.getItem("lbl_" + key) || "";
  }
  const refreshFile = () => {
    const file = get("auto-file").files[0];
    get("auto-submit").disabled = !file || busy;
    get("auto-file-chosen").classList.toggle("hidden", !file);
    get("auto-file-chosen").textContent = file ? `${file.name} · ${(file.size / 1048576).toFixed(1)} MB` : "";
  };
  get("auto-file").addEventListener("change", refreshFile);
  get("auto-clear-file").addEventListener("click", () => {
    get("auto-file").value = ""; refreshFile();
  });
  const drop = get("auto-drop");
  drop.addEventListener("click", () => { if (!busy) get("auto-file").click(); });
  drop.addEventListener("keydown", event => {
    if (event.key === "Enter" || event.key === " ") { event.preventDefault(); drop.click(); }
  });
  drop.addEventListener("dragover", event => { event.preventDefault(); drop.classList.add("armed"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("armed"));
  drop.addEventListener("drop", event => {
    event.preventDefault(); drop.classList.remove("armed");
    if (busy || !event.dataTransfer.files.length) return;
    const files = new DataTransfer(); files.items.add(event.dataTransfer.files[0]);
    get("auto-file").files = files.files; refreshFile();
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
    const width = Math.max(record.image.cols, record.image.rows) / 500;
    for (const feature of record.features) {
      const color = feature.state === "pass" ? "#65df8b" : "#ffb45c";
      const add = (tag, attrs) => {
        const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
        Object.entries({fill: "none", stroke: color, "stroke-width": width, ...attrs})
          .forEach(([key, value]) => node.setAttribute(key, value));
        const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
        title.textContent = `${feature.label}: ${feature.state === "pass" ? "PASS" : "Attention required"}`;
        node.append(title); svg.append(node);
      };
      for (const roi of feature.overlays) {
        if (roi.type === "rect") add("polygon", {points: roi.corners_px.map(p => p.join(",")).join(" ")});
        else if (roi.type === "segment") add("line", {x1: roi.p0_px[0], y1: roi.p0_px[1], x2: roi.p1_px[0], y2: roi.p1_px[1]});
        else if (roi.type === "point") add("circle", {cx: roi.center_px[0], cy: roi.center_px[1], r: width * 4});
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
    get("auto-result").classList.remove("hidden");
    const img = get("auto-image");
    const viewer = get("auto-viewer");
    get("auto-image-error").classList.add("hidden");
    // Hide the old image and overlays until the new image has loaded.
    viewer.classList.add("hidden");
    img.onload = () => { renderOverlays(record); viewer.classList.remove("hidden"); };
    img.onerror = () => {
      get("auto-image-error").classList.remove("hidden");
    };
    img.src = `api/analyses/${encodeURIComponent(record.id)}/image.png?scale=1600`;
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
    get("auto-results-page").classList.add("hidden");
    form.classList.remove("hidden");
    get("auto-file").value = ""; refreshFile();
    get("auto-error").classList.add("hidden");
  });
  get("auto-open-analyze").addEventListener("click", () => {
    const record = analyses[activeIndex];
    if (record && !busy) document.dispatchEvent(new CustomEvent("phantomqa:open-analysis", {detail: record.id}));
  });
  get("auto-scan-select").addEventListener("change", event => renderResult(Number(event.target.value)));
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (busy || !form.reportValidity()) return;
    busy = true;
    const data = new FormData(form);
    for (const key of ["site", "phantom", "operator"]) sessionStorage.setItem("lbl_" + key, data.get(key) || "");
    form.classList.add("hidden");
    get("auto-results-page").classList.remove("hidden");
    get("auto-processing").classList.remove("hidden");
    get("auto-new-scan").disabled = true;
    get("auto-open-analyze").disabled = true;
    get("auto-result").classList.add("hidden");
    get("auto-tasks").replaceChildren();
    get("auto-error").classList.add("hidden");
    form.setAttribute("aria-busy", "true");
    form.querySelectorAll("input, button").forEach(input => { input.disabled = true; });
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
      labelsLoaded = false;
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
      form.querySelectorAll("input, button").forEach(input => { input.disabled = false; });
      refreshFile();
    }
  });
})();
