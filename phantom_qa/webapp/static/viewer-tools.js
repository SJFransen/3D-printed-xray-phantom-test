/* Shared image-view tools. They change display state only, never measuring ROIs. */
(() => {
  "use strict";
  const fit = (width, height, imageWidth, imageHeight) => {
    const k = Math.min(width / imageWidth, height / imageHeight) * 0.98;
    return {k, tx: (width - imageWidth * k) / 2, ty: (height - imageHeight * k) / 2};
  };
  const zoomAt = (view, point, factor) => ({
    k: view.k * factor,
    tx: point[0] - (point[0] - view.tx) * factor,
    ty: point[1] - (point[1] - view.ty) * factor,
  });
  const pan = (view, dx, dy) => ({...view, tx: view.tx + dx, ty: view.ty + dy});
  const windowAbsolute = (range, width, center) => {
    if (!range) return null;
    const span = Math.max(range.hi - range.lo, 1e-6);
    return {wc: range.lo + center / 100 * span, ww: Math.max(width / 100 * span, span * 0.01)};
  };
  const windowLabel = value => {
    const format = number => Number(number.toPrecision(5)).toString();
    return value ? `W ${format(value.ww)}  C ${format(value.wc)}` : "";
  };

  class ImageViewer {
    constructor({viewport, layer, image, controls, onLoad, onError}) {
      Object.assign(this, {viewport, layer, image, controls, onLoad, onError});
      this.view = {k: 1, tx: 0, ty: 0};
      this.version = 0; this.timer = null; this.loaded = false;
      this.fitted = true; this.size = [0, 0]; this.drag = null;
      for (const input of [controls.width, controls.center]) {
        input.addEventListener("input", () => {
          this.updateReadout();
          this.invalidate();
          this.timer = setTimeout(() => { this.timer = null; this.load(true); }, 200);
        });
      }
      controls.auto.addEventListener("click", () => { this.resetWindow(); this.load(); });
      controls.fit.addEventListener("click", () => this.fit());
      controls.zoomIn.addEventListener("click", () => this.zoom(1.15));
      controls.zoomOut.addEventListener("click", () => this.zoom(1 / 1.15));
      viewport.addEventListener("wheel", event => {
        if (!this.loaded) return;
        event.preventDefault();
        const box = viewport.getBoundingClientRect();
        this.zoom(event.deltaY < 0 ? 1.15 : 1 / 1.15,
                  [event.clientX - box.left, event.clientY - box.top]);
      }, {passive: false});
      viewport.addEventListener("pointerdown", event => {
        if (!this.loaded || event.button !== 0 || this.drag) return;
        event.preventDefault();
        this.drag = {id: event.pointerId, x: event.clientX, y: event.clientY, view: {...this.view}};
        viewport.setPointerCapture(event.pointerId);
        viewport.classList.add("dragging");
      });
      viewport.addEventListener("pointermove", event => {
        if (!this.drag || event.pointerId !== this.drag.id) return;
        this.fitted = false;
        this.view = pan(this.drag.view, event.clientX - this.drag.x, event.clientY - this.drag.y);
        this.apply();
      });
      const endDrag = event => {
        if (!this.drag || event.pointerId !== this.drag.id) return;
        this.drag = null; viewport.classList.remove("dragging");
        if (viewport.hasPointerCapture(event.pointerId)) viewport.releasePointerCapture(event.pointerId);
      };
      viewport.addEventListener("pointerup", endDrag);
      viewport.addEventListener("pointercancel", endDrag);
      viewport.addEventListener("lostpointercapture", endDrag);
      this.observer = new ResizeObserver(() => this.resize());
      this.observer.observe(viewport);
    }

    invalidate() {
      this.version++;
      clearTimeout(this.timer); this.timer = null;
    }

    clear() {
      this.invalidate(); this.record = null; this.loaded = false; this.drag = null;
      this.viewport.classList.remove("dragging");
    }

    setScan(record) {
      this.clear();
      this.record = record; this.loaded = false; this.fitted = true;
      this.layer.style.width = `${record.image.cols}px`;
      this.layer.style.height = `${record.image.rows}px`;
      this.resetWindow(); this.load();
    }

    resetWindow() {
      this.invalidate();
      this.controls.width.value = 100; this.controls.center.value = 50;
      const disabled = !this.record?.display_range;
      this.controls.width.disabled = disabled; this.controls.center.disabled = disabled;
      this.updateReadout();
    }

    updateReadout() {
      this.controls.readout.textContent = windowLabel(this.absoluteWindow());
    }

    absoluteWindow() {
      return windowAbsolute(this.record?.display_range, +this.controls.width.value, +this.controls.center.value);
    }

    load(manualWindow = false) {
      if (!this.record) return;
      this.invalidate();
      const version = this.version, record = this.record, preview = new Image();
      const url = new URL(`api/analyses/${encodeURIComponent(record.id)}/image.png`, document.baseURI);
      url.searchParams.set("scale", "1600");
      const window = manualWindow ? this.absoluteWindow() : null;
      if (window) {
        url.searchParams.set("wc", window.wc); url.searchParams.set("ww", window.ww);
      }
      preview.onload = () => {
        if (version !== this.version || record !== this.record) return;
        const first = !this.loaded;
        this.image.src = preview.src; this.loaded = true;
        this.onLoad?.();
        if (first) this.fit(); else this.apply();
      };
      preview.onerror = () => { if (version === this.version) this.onError?.(); };
      preview.src = url.href;
    }

    apply() {
      const {k, tx, ty} = this.view;
      this.layer.style.transform = `translate(${tx}px, ${ty}px) scale(${k})`;
    }

    fit() {
      if (!this.loaded || !this.viewport.clientWidth || !this.viewport.clientHeight) return;
      this.view = fit(this.viewport.clientWidth, this.viewport.clientHeight, this.record.image.cols, this.record.image.rows);
      this.size = [this.viewport.clientWidth, this.viewport.clientHeight];
      this.fitted = true; this.apply();
    }

    zoom(factor, point = [this.viewport.clientWidth / 2, this.viewport.clientHeight / 2]) {
      if (!this.loaded) return;
      this.view = zoomAt(this.view, point, factor); this.fitted = false; this.apply();
    }

    resize() {
      const size = [this.viewport.clientWidth, this.viewport.clientHeight];
      if (!this.loaded || !size[0] || !size[1]) return;
      if (this.fitted) { this.fit(); return; }
      this.view = pan(this.view, (size[0] - this.size[0]) / 2, (size[1] - this.size[1]) / 2);
      this.size = size; this.apply();
    }
  }

  globalThis.ViewerTools = {fit, zoomAt, pan, windowAbsolute, windowLabel, ImageViewer};
})();
