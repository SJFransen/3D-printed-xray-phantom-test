/* Shared upload UI: both workflows submit the same file and identification fields. */
(() => {
  "use strict";
  function mount(host, {prefix = "", request, onUpload, onFile = () => {}}) {
    host.replaceChildren(document.getElementById("scan-upload-template").content.cloneNode(true));
    const ids = {drop: "drop", file: "file-input", chosen: "file-chosen", upload: "btn-upload",
      clear: "btn-clear-file", warning: "up-warn", site: "up-site", phantom: "up-phantom",
      operator: "up-operator", notes: "up-notes", sites: "dl-site", phantoms: "dl-phantom"};
    const autoIds = {drop: "drop", file: "file", chosen: "file-chosen", upload: "submit",
      clear: "clear-file", warning: "label-warning", sites: "sites", phantoms: "phantoms"};
    const nodes = {};
    for (const [key, id] of Object.entries(ids)) {
      nodes[key] = host.querySelector("#" + id);
      if (prefix) nodes[key].id = prefix + "-" + (autoIds[key] || key);
    }
    nodes.site.setAttribute("list", nodes.sites.id);
    nodes.phantom.setAttribute("list", nodes.phantoms.id);
    for (const key of ["site", "phantom", "operator"]) {
      nodes[key].value = sessionStorage.getItem("lbl_" + key) || "";
    }
    let file = null, busy = false, previewing = false, version = 0;
    nodes.site.required = true;
    const labels = () => Object.fromEntries(["site", "phantom", "operator", "notes"]
      .map(key => [key, nodes[key].value.trim()]));
    function refresh() {
      nodes.chosen.classList.toggle("hidden", !file);
      nodes.chosen.textContent = file ? `${file.name} · ${(file.size / 1048576).toFixed(1)} MB` : "";
      nodes.upload.disabled = !file || busy || previewing || !labels().site;
    }
    async function choose(next) {
      file = next; onFile(file); refresh();
      const current = ++version;
      nodes.warning.textContent = "";
      if (!file) { previewing = false; refresh(); return; }
      previewing = true; nodes.site.value = "";
      nodes.warning.textContent = "Reading institution from the scan…"; refresh();
      const data = new FormData(); data.append("file", file);
      try {
        const result = await request("api/uploads/preview", {method: "POST", body: data});
        if (current !== version) return;
        const names = [...new Set(result.scans.map(scan => scan.institution))];
        nodes.site.value = names.length === 1 ? names[0] : "";
        nodes.warning.textContent = names.length > 1
          ? "This archive contains different or missing institutions. Enter the site to use for all scans."
          : nodes.site.value ? "Institution read from DICOM. Check it before uploading."
          : "No institution in the DICOM headers. Enter the site / institution to continue.";
      } catch (error) {
        if (current !== version) return;
        nodes.warning.textContent = error.message + " Enter the institution before retrying.";
      } finally {
        if (current === version) { previewing = false; refresh(); }
      }
    }
    nodes.file.addEventListener("change", () => choose(nodes.file.files[0] || null));
    nodes.drop.addEventListener("click", () => { if (!busy) nodes.file.click(); });
    nodes.drop.addEventListener("keydown", event => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); nodes.drop.click(); }
    });
    nodes.drop.addEventListener("dragover", event => { event.preventDefault(); nodes.drop.classList.add("armed"); });
    nodes.drop.addEventListener("dragleave", () => nodes.drop.classList.remove("armed"));
    nodes.drop.addEventListener("drop", event => {
      event.preventDefault(); nodes.drop.classList.remove("armed");
      if (busy || !event.dataTransfer.files.length) return;
      const files = new DataTransfer(); files.items.add(event.dataTransfer.files[0]);
      nodes.file.files = files.files; choose(files.files[0]);
    });
    nodes.clear.addEventListener("click", () => { nodes.file.value = ""; choose(null); });
    for (const key of ["site", "phantom"]) nodes[key].addEventListener("input", refresh);
    nodes.upload.addEventListener("click", () => {
      if (file && !busy && !previewing && nodes.site.reportValidity()) onUpload(file);
    });
    async function loadLabels() {
      try {
        const data = await request("api/labels");
        for (const [key, list] of [["site", nodes.sites], ["phantom", nodes.phantoms]]) {
          list.replaceChildren(...(data[key] || []).map(item => {
            const option = document.createElement("option"); option.value = item.value; return option;
          }));
        }
      } catch (_) { /* Identification remains available when suggestions cannot be loaded. */ }
    }
    loadLabels(); refresh();
    return {nodes, labels, refresh, loadLabels,
      data() {
        const data = new FormData(); data.append("file", file);
        for (const [key, value] of Object.entries(labels())) data.append(key, value);
        for (const key of ["site", "phantom", "operator"]) sessionStorage.setItem("lbl_" + key, labels()[key]);
        return data;
      },
      clear() { nodes.file.value = ""; choose(null); },
      reset() {
        for (const key of ["site", "phantom", "operator"]) nodes[key].value = sessionStorage.getItem("lbl_" + key) || "";
        nodes.notes.value = ""; nodes.file.value = ""; choose(null);
      },
      setBusy(value) {
        busy = value;
        nodes.drop.setAttribute("aria-disabled", String(busy));
        nodes.drop.tabIndex = busy ? -1 : 0;
        host.querySelectorAll("input,button").forEach(node => { node.disabled = busy; });
        refresh();
      },
    };
  }
  globalThis.UploadForm = {mount};
})();
