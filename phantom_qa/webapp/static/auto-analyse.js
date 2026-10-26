/* Optional Auto Analyze UI. No wizard state, positioning controls or QA rules. */
(() => {
  "use strict";
  const page = document.querySelector("#view-auto-analyse");
  if (!page) return;
  const get = id => document.getElementById(id);
  const form = get("auto-upload-form");
  const viewer = new ViewerTools.ImageViewer({
    strokeWidth: 2.6,
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
    if (!response.ok) {
      const error = new Error(typeof data.detail === "string" ? data.detail : "The analysis could not be completed.");
      error.status = response.status; throw error;
    }
    return data;
  }

  const upload = UploadForm.mount(get("auto-upload-fields"), {
    prefix: "auto", request,
    onUpload: () => { if (!busy) form.requestSubmit(); },
  });
  let refreshVersion = 0, scanGeneration = 0, dirty = false, navigationBusy = false;
  function updateControls() {
    const blocked = busy || navigationBusy;
    get("auto-new-scan").disabled = blocked;
    get("analyze-new-scan").disabled = blocked;
    get("analyze-return-auto").disabled = blocked;
    get("auto-open-analyze").disabled = blocked || !analyses[activeIndex];
    get("auto-scan-select").disabled = blocked;
    get("tab-auto-analyse").disabled = blocked;
    get("tab-history").disabled = blocked;
  }
  function showResults() {
    form.classList.add("hidden");
    get("auto-results-page").classList.remove("hidden");
    showTab("auto-analyse");
  }
  function setRecords(records, index = 0) {
    analyses = records; activeIndex = index;
    const select = get("auto-scan-select");
    select.replaceChildren(...records.map((record, i) => {
      const option = make("option", `${record.source_name} ? ${record.overall === "pass" ? "PASS" : "Attention required"}`);
      option.value = i; return option;
    }));
    select.value = index;
    get("auto-scan-choice").classList.toggle("hidden", records.length < 2);
    renderResult(index); updateControls();
  }
  function resetAutoState() {
    analyses = []; activeIndex = 0; dirty = false;
    viewer.clear(); get("auto-image").removeAttribute("src");
    for (const id of ["auto-overlays", "auto-feature-status", "auto-tasks", "auto-scan-select"])
      get(id).replaceChildren();
    get("auto-identity").textContent = ""; get("auto-progress").textContent = "";
    for (const id of ["auto-result", "auto-results-page", "auto-image-error", "auto-error", "auto-scan-choice", "auto-viewer"])
      get(id).classList.add("hidden");
    upload.reset(); form.classList.remove("hidden");
  }
  function removeSavedRecord(aid) {
    const index = analyses.findIndex(record => record.id === aid);
    if (index < 0) return;
    ++scanGeneration; ++refreshVersion;
    const selected = analyses[activeIndex]?.id;
    const remaining = analyses.filter(record => record.id !== aid);
    if (!remaining.length) resetAutoState();
    else setRecords(remaining, Math.max(0, remaining.findIndex(record => record.id === selected)));
    updateControls();
  }
  async function refreshSavedResult() {
    const index = activeIndex, current = analyses[index], version = ++refreshVersion, generation = scanGeneration;
    if (!current) return;
    try {
      const record = await request(`api/auto-analyse/${encodeURIComponent(current.id)}?duplicate=${current.duplicate}`);
      if (generation !== scanGeneration || version !== refreshVersion || analyses[index]?.id !== current.id) return;
      analyses[index] = record; dirty = false;
      if (index === activeIndex && !get("auto-results-page").classList.contains("hidden")) renderResult(index);
    } catch (error) {
      if (error.status === 404 && generation === scanGeneration && version === refreshVersion) removeSavedRecord(current.id);
      // Other failures keep the preview available; returning from Analyze retries it.
    }
  }
  async function returnToAuto() {
    if (busy || navigationBusy) return;
    navigationBusy = true; updateControls();
    const intent = ++navigationRequest, generation = scanGeneration;
    const aid = S.aid || analyses[activeIndex]?.id, fromAnalyze = !!S.aid;
    ++refreshVersion;
    try {
      const index = analyses.findIndex(record => record.id === aid);
      let refreshed = null;
      await closeAnalysis(async () => {
        if (aid) {
          if (fromAnalyze || dirty) await completePendingAnalysis(aid);
          refreshed = await request(`api/auto-analyse/${encodeURIComponent(aid)}?duplicate=${index < 0 ? false : analyses[index].duplicate}`);
        }
      });
      if (intent !== navigationRequest || generation !== scanGeneration) return;
      if (aid) {
        if (refreshed) {
          if (index < 0) setRecords([refreshed]);
          else { analyses[index] = refreshed; setRecords(analyses, index); }
          dirty = false;
        } else renderResult(index);
        showResults();
      } else showTab("auto-analyse");
      status("");
    } catch (error) {
      if (error.status === 404 && intent === navigationRequest && generation === scanGeneration) {
        await closeAnalysis(); removeSavedRecord(aid);
        showTab("auto-analyse");
        status("This scan is no longer available. Choose another scan or upload a new one.", true);
      } else status("Could not return to Auto Analyze: " + error.message, true);
    } finally { navigationBusy = false; updateControls(); }
  }
  async function openSavedResult(aid) {
    if (busy || navigationBusy || typeof aid !== "string") return;
    navigationBusy = true; updateControls();
    const intent = ++navigationRequest, generation = ++scanGeneration;
    ++refreshVersion;
    try {
      await closeAnalysis();
      if (dirty && analyses.some(record => record.id === aid)) await completePendingAnalysis(aid);
      const record = await request(`api/auto-analyse/${encodeURIComponent(aid)}`);
      if (intent !== navigationRequest || generation !== scanGeneration) return;
      dirty = false; setRecords([record]); showResults(); status("");
    } catch (error) {
      if (error.status === 404 && intent === navigationRequest && generation === scanGeneration) {
        removeSavedRecord(aid); showTab("auto-analyse");
        status("This scan is no longer available. Choose another scan or upload a new one.", true);
      } else status("Could not open the saved scan: " + error.message, true);
    }
    finally {
      navigationBusy = false; updateControls();
      if (!page.classList.contains("hidden") && analyses[activeIndex]?.id === aid) get("auto-open-analyze").focus();
    }
  }
  async function startNewScan() {
    if (busy || navigationBusy) return;
    navigationBusy = true; updateControls();
    const intent = ++navigationRequest;
    ++scanGeneration; ++refreshVersion;
    try {
      await closeAnalysis();
      if (intent !== navigationRequest) return;
      resetAutoState();
      showTab("auto-analyse"); status(""); upload.nodes.drop.focus();
    } catch (error) { status("Could not close the current scan: " + error.message, true); }
    finally { navigationBusy = false; updateControls(); }
  }
  document.addEventListener("phantomqa:new-scan", startNewScan);
  document.addEventListener("phantomqa:analysis-deleted", async event => {
    if (S.aid === event.detail) await closeAnalysis();
    removeSavedRecord(event.detail);
  });
  document.addEventListener("phantomqa:open-auto-analysis", event => openSavedResult(event.detail));
  document.addEventListener("phantomqa:analysis-changed", event => {
    if (!analyses.some(record => record.id === event.detail)) return;
    dirty = true;
    if (!page.classList.contains("hidden") && !busy && !navigationBusy) refreshSavedResult();
  });
  document.addEventListener("phantomqa:tab", event => {
    page.classList.toggle("hidden", event.detail !== "auto-analyse");
    get("tab-auto-analyse").classList.toggle("active", event.detail === "auto-analyse");
    if (event.detail === "auto-analyse") upload.loadLabels();
  });
  get("tab-auto-analyse").addEventListener("click", returnToAuto);
  get("analyze-return-auto").addEventListener("click", returnToAuto);
  get("auto-new-scan").addEventListener("click", startNewScan);
  get("analyze-new-scan").addEventListener("click", startNewScan);

  async function loadTaskPanel(host, aid) {
    try {
      const record = await request(`api/auto-analyse/${encodeURIComponent(aid)}`);
      if (host.isConnected && host.dataset.analysisTasks === aid) renderTasks(record.tasks, host);
    } catch (_) {
      if (host.isConnected) host.textContent = "Guidance could not be loaded. Reopen results to retry.";
    }
  }
  document.addEventListener("phantomqa:render-tasks", event => {
    const {host, aid} = event.detail;
    if (host && typeof aid === "string") loadTaskPanel(host, aid);
  });
  function renderTasks(tasks, target = get("auto-tasks")) {
    target.replaceChildren();
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
      for (const step of task.solutions || []) {
        const dropdown = make("details", "", "solution-step");
        dropdown.open = task.solutions.length === 1;
        dropdown.append(make("summary", step.title));
        for (const content of step.content) {
          const audiences = target.id === "auto-tasks" ? ["all", "operator"]
            : ["all", "operator", "technician", "physicist"];
          if (!audiences.includes(content.audience)) continue;
          for (const block of content.blocks) {
            if (block.type === "text") {
              dropdown.append(make("p", block.text, "solution-text"));
              continue;
            }
            const url = new URL(block.url, document.baseURI);
            if (url.origin !== location.origin || !url.pathname.includes("/api/solutions/")) continue;
            if (block.type === "image") {
              const img = make("img"); img.src = url.href; img.alt = block.description;
              img.loading = "lazy"; dropdown.append(img);
              img.addEventListener("error", () => img.replaceWith(make("p", "Image unavailable. Continue with the written instructions.")));
            } else if (block.type === "video") {
              const video = make("video"); video.src = url.href; video.controls = true;
              video.preload = "none"; video.setAttribute("aria-label", block.description);
              video.append(make("p", "Video playback is unavailable.")); dropdown.append(video);
              const link = make("a", "Open or download video"); link.href = url.href;
              dropdown.append(link);
            }
          }
        }
        card.append(dropdown);
      }
      if (task.type === "feature_solution" && !task.solutions?.length)
        card.append(make("p", "No troubleshooting steps are available yet. Contact your technical support team."));
      for (const reference of task.image_references || []) {
        // Image references are restricted to this application or embedded images.
        const url = new URL(reference.url, document.baseURI);
        if (url.origin !== window.location.origin && url.protocol !== "data:") continue;
        const img = make("img"); img.src = url.href; img.alt = reference.description || "Task illustration";
        card.append(img);
      }
      target.append(card);
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
      const rois = feature.id === "linepairs" && record.geometry
        ? ViewerTools.linepairRois(record.geometry).map(entry => entry.roi) : feature.overlays;
      for (const roi of rois) {
        const draw = (tag, attrs) => add(tag, {"data-roi-id": roi.id || feature.id, ...attrs});
        if (roi.type === "rect") draw("polygon", {points: roi.corners_px.map(p => p.join(",")).join(" ")});
        else if (roi.type === "segment") draw("line", {x1: roi.p0_px[0], y1: roi.p0_px[1], x2: roi.p1_px[0], y2: roi.p1_px[1]});
        else if (roi.type === "point") draw("circle", {cx: roi.center_px[0], cy: roi.center_px[1], r: radius});
        else if (roi.type === "circle") draw("circle", {cx: roi.center_px[0], cy: roi.center_px[1], r: roi.radius_px});
        else if (roi.type === "annulus") {
          [roi.inner_radius_px, roi.outer_radius_px].forEach(r => draw("circle", {cx: roi.center_px[0], cy: roi.center_px[1], r}));
        }
      }
    }
  }

  function renderResult(index) {
    activeIndex = index;
    const record = analyses[index];
    if (!record) return;
    get("auto-scan-select").value = index;
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
      row.append(make("span", feature.state === "pass" ? "PASS" : "Attention", "auto-verdict"));
      row.setAttribute("aria-label", `${feature.label}: ${feature.state === "pass" ? "PASS" : "Attention"}`);
      statuses.append(row);
    }
    renderTasks(record.tasks);
    updateControls();
  }

  get("auto-open-analyze").addEventListener("click", async () => {
    const record = analyses[activeIndex];
    if (!record || busy || navigationBusy) return;
    navigationBusy = true; updateControls();
    try {
      await enterAnalyze(record.id);
    } catch (error) { status("Could not open Analyze: " + error.message, true); }
    finally {
      navigationBusy = false; updateControls();
      if (S.aid === record.id && !get("view-analyze").classList.contains("hidden")) get("analyze-return-auto").focus();
    }
  });
  get("auto-scan-select").addEventListener("change", event => {
    if (!busy && !navigationBusy) renderResult(Number(event.target.value));
  });
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (busy || navigationBusy || !upload.nodes.file.files.length || !form.reportValidity()) return;
    viewer.clear();
    busy = true;
    const generation = ++scanGeneration; ++refreshVersion;
    analyses = []; activeIndex = 0; dirty = false; updateControls();
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
      if (generation !== scanGeneration) return;
      if (!response.analyses.length) throw new Error("No scan images were found in this upload.");
      setRecords(response.analyses);
      get("auto-progress").textContent = `Analysis complete. ${analyses.length} scan${analyses.length === 1 ? "" : "s"} processed.`;
      upload.loadLabels();
    } catch (error) {
      get("auto-results-page").classList.add("hidden");
      form.classList.remove("hidden");
      get("auto-error").textContent = (error.status === 422 ? error.message : "The upload could not be completed.")
        + " Check History before trying again.";
      get("auto-error").classList.remove("hidden");
    } finally {
      busy = false; form.removeAttribute("aria-busy");
      get("auto-processing").classList.add("hidden");
      updateControls();
      upload.setBusy(false);
    }
  });
  updateControls();
})();
