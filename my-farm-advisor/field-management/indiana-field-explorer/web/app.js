"use strict";

const YEAR_COLORS = {
  2020: "#8d99a6",
  2021: "#1f77b4",
  2022: "#2ca02c",
  2023: "#ff7f0e",
  2024: "#9467bd",
  2025: "#d62728",
  2026: "#17becf",
};

const RAMPS = {
  rdylgn: ["#d73027", "#fc8d59", "#fee08b", "#d9ef8b", "#66bd63", "#1a9850"],
  viridis: ["#440154", "#3b528b", "#21918c", "#5ec962", "#fde725"],
  greens: ["#f7fcf5", "#c7e9c0", "#74c476", "#31a354", "#006d2c"],
  brbg: ["#8c510a", "#d8b365", "#f6e8c3", "#c7eae5", "#5ab4ac", "#01665e"],
};

const SOIL_COLORS = [
  "#8dd3c7", "#ffffb3", "#bebada", "#fb8072", "#80b1d3", "#fdb462",
  "#b3de69", "#fccde5", "#d9d9d9", "#bc80bd", "#ccebc5", "#ffed6f",
];

const state = {
  meta: null,
  year: 2025,
  period: 10,
  scale: { ramp: "rdylgn", stretch: "fixed", min: -0.2, max: 1.0 },
  map: null,
  fieldLayer: null,
  layers: {},
  featureById: new Map(),
  fieldFeatures: [],
  selected: null,
  selectionToken: 0,
  selectionAbort: null,
  chart: null,
  timer: null,
  polling: null,
  job: null,
  rasterMeta: null,
  rasterToken: 0,
  ndvi: {
    visible: true,
    info: null,
    canvas: null,
    pixels: null,
    imageData: null,
    overlay: null,
    url: null,
    paintId: 0,
    timer: null,
    token: 0,
  },
  soil: { visible: false, data: null, layer: null, token: 0 },
};

const el = (id) => document.getElementById(id);
const fmt = (v, n = 2) => (v == null || Number.isNaN(v) ? "—" : Number(v).toFixed(n));
const esc = (value) => String(value == null ? "" : value)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;");

/* ------------------------------------------------------------------ */
/* Color ramps                                                         */
/* ------------------------------------------------------------------ */
function hexToRgb(hex) {
  return [parseInt(hex.slice(1, 3), 16), parseInt(hex.slice(3, 5), 16),
          parseInt(hex.slice(5, 7), 16)];
}

function buildLut(stops) {
  const rgb = stops.map(hexToRgb);
  const lut = new Uint8ClampedArray(256 * 3);
  for (let i = 0; i < 256; i += 1) {
    const t = (i / 255) * (rgb.length - 1);
    const i0 = Math.floor(t);
    const i1 = Math.min(i0 + 1, rgb.length - 1);
    const f = t - i0;
    for (let c = 0; c < 3; c += 1) {
      lut[i * 3 + c] = Math.round(rgb[i0][c] * (1 - f) + rgb[i1][c] * f);
    }
  }
  return lut;
}

function rampCss(stops) {
  return `linear-gradient(90deg, ${stops
    .map((c, i) => `${c} ${((i / (stops.length - 1)) * 100).toFixed(1)}%`)
    .join(", ")})`;
}

/* ------------------------------------------------------------------ */
/* Map                                                                 */
/* ------------------------------------------------------------------ */
function styleField(feature) {
  const crop = feature.properties.ag_class === "crop";
  return {
    color: crop ? "rgba(25,45,25,.55)" : "rgba(70,90,70,.35)",
    weight: crop ? 0.8 : 0.5,
    fillColor: "#ffffff",
    fillOpacity: 0.01,
    opacity: 1,
  };
}

function selectedStyle() {
  return {
    color: "#ffd400",
    weight: 3.6,
    opacity: 1,
    fillColor: "#ffd400",
    fillOpacity: 0.08,
  };
}

function restyleFeature(feature) {
  if (!feature) return;
  const id = feature.properties.field_id;
  const lyr = state.layers[id];
  if (!lyr) return;
  lyr.setStyle(state.selected === id ? selectedStyle() : styleField(feature));
}

function soilColor(musym) {
  let h = 0;
  for (let i = 0; i < musym.length; i += 1) h = (h * 31 + musym.charCodeAt(i)) >>> 0;
  return SOIL_COLORS[h % SOIL_COLORS.length];
}

function styleSoil(feature) {
  return {
    color: "#3f3324",
    weight: 0.7,
    fillColor: soilColor(feature.properties.musym || ""),
    fillOpacity: 0.45,
  };
}

function soilPopup(p) {
  const rows = [
    ["Map unit", `${p.musym} · ${p.mukey}`],
    ["Name", p.muname],
    ["Drainage", p.drainage || "—"],
    ["Hydrologic group", p.hydgrp || "—"],
    ["Slope", p.slope_pct == null ? "—" : `${p.slope_pct} %`],
    ["AWC 0–50 cm", p.awc_050 == null ? "—" : p.awc_050],
    ["Water table", p.wt_depth_cm == null ? "—" : `${p.wt_depth_cm} cm`],
    ["Flood frequency", p.flood_freq || "—"],
  ];
  return `<div class="soilpop"><b>${esc(p.musym)}</b> — ${esc(p.muname)}`
    + `<table>${rows.map(([k, v]) => `<tr><th>${esc(k)}</th><td>${esc(v)}</td></tr>`).join("")}</table></div>`;
}

function buildMap(fields) {
  const map = L.map("map", { preferCanvas: true, zoomControl: true })
    .setView([39.49, -86.1], 11);
  state.map = map;

  map.createPane("ndviPane").style.zIndex = 350;
  map.createPane("soilPane").style.zIndex = 360;
  map.createPane("fieldPane").style.zIndex = 400;

  L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    { maxZoom: 19, attribution: "Imagery &copy; Esri, Maxar, Earthstar Geographics" }
  ).addTo(map);

  state.fieldFeatures = fields.features;
  fields.features.forEach((f) => {
    f._bbox = FieldGeo.bboxOf(f.geometry);
    state.featureById.set(f.properties.field_id, f);
  });

  // Fields are the only interactive vector layer. NDVI and soil layers are
  // non-interactive so field clicks always reach this canvas and the map.
  state.fieldLayer = L.geoJSON(fields, {
    pane: "fieldPane",
    style: styleField,
    onEachFeature(feature, lyr) {
      const id = feature.properties.field_id;
      state.layers[id] = lyr;
      lyr.bindTooltip(() => {
        const p = feature.properties;
        const crop = p.crop_name ? ` · ${esc(p.crop_name)}` : "";
        return `<b>${Number(p.acres).toFixed(1)} ac</b>${crop}`
          + `<br><span class="toolid">${esc(id)}</span>`;
      }, { sticky: true, className: "leaflet-tooltip" });
      lyr.on("mouseover", () => {
        if (state.selected !== id) {
          lyr.setStyle({ weight: 2.4, color: "#123d14" });
        }
      });
      lyr.on("mouseout", () => restyleFeature(feature));
    },
  }).addTo(map);

  map.on("click", onMapClick);

  map.fitBounds(state.fieldLayer.getBounds(), { padding: [12, 12] });
}

function onMapClick(event) {
  const hit = FieldGeo.fieldAt(state.fieldFeatures, event.latlng.lng, event.latlng.lat);
  if (!hit) {
    clearSelection();
    return;
  }
  const id = hit.properties.field_id;
  if (state.selected && id === state.selected) {
    // A click inside the selected field inspects soil attributes; it must not
    // change which field is selected.
    if (state.soil.visible) {
      const soil = soilAt(event.latlng);
      if (soil) showSoilPopup(soil, event.latlng);
    }
    return;
  }
  selectField(id);
}

function soilAt(latlng) {
  if (!state.soil.data) return null;
  for (const feature of state.soil.data.features) {
    if (!feature._bbox) feature._bbox = FieldGeo.bboxOf(feature.geometry);
    const bb = feature._bbox;
    if (latlng.lng < bb[0] || latlng.lng > bb[2]
        || latlng.lat < bb[1] || latlng.lat > bb[3]) continue;
    if (FieldGeo.contains(feature, latlng.lng, latlng.lat)) return feature;
  }
  return null;
}

function showSoilPopup(feature, latlng) {
  L.popup({ maxWidth: 340 })
    .setLatLng(latlng)
    .setContent(soilPopup(feature.properties))
    .openOn(state.map);
}

/* ------------------------------------------------------------------ */
/* Selection                                                           */
/* ------------------------------------------------------------------ */
function setTogglesEnabled(enabled) {
  el("ndvitoggle").disabled = !enabled;
  el("soiltoggle").disabled = !enabled;
}

function setNdviStatus(text, kind) {
  const node = el("ndvi-status");
  node.textContent = text;
  node.classList.toggle("error", kind === "error");
  node.classList.toggle("loading", kind === "loading");
}

function setSoilStatus(text, kind) {
  const node = el("soil-status");
  node.textContent = text;
  node.classList.toggle("error", kind === "error");
  node.classList.toggle("loading", kind === "loading");
}

function selectField(fieldId) {
  const feature = state.featureById.get(fieldId);
  if (!feature) return;

  const previous = state.selected;
  state.selected = fieldId;
  const token = (state.selectionToken += 1);
  if (state.selectionAbort) state.selectionAbort.abort();
  const controller = new AbortController();
  state.selectionAbort = controller;

  if (previous && previous !== fieldId) {
    restyleFeature(state.featureById.get(previous));
  }
  const lyr = state.layers[fieldId];
  if (lyr) {
    lyr.setStyle(selectedStyle());
    if (lyr.bringToFront) lyr.bringToFront();
    state.map.fitBounds(lyr.getBounds(), { padding: [28, 28], maxZoom: 16 });
  }
  state.map.closePopup();

  setTogglesEnabled(true);
  showDetailLoading(feature);
  el("selecthint").textContent =
    `Selected ${fieldId} · click another field to switch · Esc or ✕ clears.`;

  const tasks = [loadFieldDetail(fieldId, token, controller.signal)];

  if (state.ndvi.visible) {
    setNdviStatus("Loading 10 m NDVI…", "loading");
    tasks.push(loadFieldNdvi(fieldId, token, controller.signal));
  } else {
    clearNdviOverlay();
    state.ndvi.info = null;
    setNdviStatus("NDVI hidden.");
    setPixelStatsMessage("NDVI layer is hidden.");
    updateFieldStretchOption();
    updateLegend();
  }

  if (state.soil.visible) {
    setSoilStatus("Loading SSURGO polygons…", "loading");
    tasks.push(loadFieldSoils(fieldId, token, controller.signal));
  } else {
    clearSoilLayer();
    state.soil.data = null;
    setSoilStatus("SSURGO hidden.");
  }

  Promise.allSettled(tasks);
}

function clearSelection() {
  const previous = state.selected;
  if (!previous) return;

  state.selectionToken += 1;
  if (state.selectionAbort) {
    state.selectionAbort.abort();
    state.selectionAbort = null;
  }
  state.selected = null;
  restyleFeature(state.featureById.get(previous));

  clearNdviOverlay();
  state.ndvi.info = null;
  clearSoilLayer();
  state.soil.data = null;

  el("detail").classList.add("d-none");
  el("d-error").classList.add("d-none");
  if (state.chart) {
    state.chart.destroy();
    state.chart = null;
  }

  setTogglesEnabled(false);
  setNdviStatus(state.ndvi.visible
    ? "Select a field to view 10 m NDVI." : "NDVI hidden.");
  setSoilStatus(state.soil.visible
    ? "Select a field to view SSURGO soils." : "SSURGO hidden.");
  el("selecthint").textContent = "Click a field to zoom and load its NDVI and soils.";
  state.map.closePopup();

  if (state.scale.stretch === "field") {
    state.scale.stretch = "fixed";
    el("stretch").value = "fixed";
  }
  updateFieldStretchOption();
  updateLegend();
}

/* ------------------------------------------------------------------ */
/* NDVI colorisation                                                   */
/* ------------------------------------------------------------------ */
function encodingDomain() {
  const enc = state.ndvi.info && state.ndvi.info.encoding;
  return [enc ? enc.min : -0.2, enc ? enc.max : 1.0];
}

function currentDomain() {
  const [encMin, encMax] = encodingDomain();
  const mode = state.scale.stretch;
  const stats = state.ndvi.info && state.ndvi.info.stats;

  if (mode === "field" && state.ndvi.info && state.ndvi.info.status === "ready"
      && stats && stats.pixels) {
    const lo = stats.p02;
    const hi = stats.p98;
    if (hi - lo > 0.02) return [lo, hi];
  }
  if (mode === "manual") {
    const lo = Number(el("minval").value);
    const hi = Number(el("maxval").value);
    if (Number.isFinite(lo) && Number.isFinite(hi) && hi - lo > 0.02) return [lo, hi];
  }
  return [encMin, encMax];
}

function updateLegend() {
  const stops = RAMPS[state.scale.ramp] || RAMPS.rdylgn;
  el("legendbar").style.background = rampCss(stops);
  const [lo, hi] = currentDomain();
  const mid = (lo + hi) / 2;
  el("legendticks").innerHTML =
    `<span>${fmt(lo, 2)}</span><span>${fmt(mid, 2)}</span><span>${fmt(hi, 2)}</span>`;

  const info = state.ndvi.info;
  if (info && info.status === "ready") {
    el("legendnote").textContent =
      `NDVI · ${info.year} P${String(info.period).padStart(2, "0")}`
      + ` · selected field · ${info.n_dates || 0} clear dates`;
  } else if (state.rasterMeta && state.rasterMeta.status === "ready") {
    el("legendnote").textContent =
      `NDVI · ${state.year} P${String(state.period).padStart(2, "0")}`
      + " · select a field to display";
  } else {
    el("legendnote").textContent = "NDVI · no composite loaded";
  }
}

function updateFieldStretchOption() {
  const opt = el("stretch").querySelector('option[value="field"]');
  const ready = Boolean(state.ndvi.info && state.ndvi.info.status === "ready"
    && state.ndvi.info.stats && state.ndvi.info.stats.pixels);
  opt.disabled = !ready;
  opt.textContent = ready ? "Selected field 2–98 %" : "Selected field (pick a field)";
  if (!ready && state.scale.stretch === "field") {
    state.scale.stretch = "fixed";
    el("stretch").value = "fixed";
  }
}

function renderPixelStats(payload) {
  const box = el("d-pixstats");
  if (!payload || payload.status === "not_computed") {
    box.innerHTML = `<span class="muted">No 10 m composite for ${state.year} P${String(state.period).padStart(2, "0")}.</span>`;
    return;
  }
  if (payload.status === "no_pixels") {
    box.innerHTML = `<span class="muted">No clear pixels inside this field for this period.</span>`;
    return;
  }
  if (payload.status === "error") {
    box.innerHTML = `<span class="muted">NDVI unavailable: ${esc(payload.error || "unknown error")}</span>`;
    return;
  }
  const s = payload.stats;
  if (!s || !s.pixels) {
    box.innerHTML = `<span class="muted">No 10 m NDVI for this field and period.</span>`;
    return;
  }
  box.innerHTML = `
    <div class="pixgrid">
      <div>pixels <b>${Number(s.pixels).toLocaleString()}</b></div>
      <div>min <b>${fmt(s.min, 3)}</b></div>
      <div>max <b>${fmt(s.max, 3)}</b></div>
      <div>mean <b>${fmt(s.mean, 3)}</b></div>
      <div>std <b>${fmt(s.std, 3)}</b></div>
      <div>p02–p98 <b>${fmt(s.p02, 2)}–${fmt(s.p98, 2)}</b></div>
    </div>`;
}

function setPixelStatsMessage(message) {
  el("d-pixstats").innerHTML = `<span class="muted">${esc(message)}</span>`;
}

function paintNdvi() {
  if (!state.ndvi.pixels || !state.ndvi.canvas || !state.ndvi.info
      || state.ndvi.info.status !== "ready") return;

  const paintId = (state.ndvi.paintId += 1);
  const [encMin, encMax] = encodingDomain();
  const [lo, hi] = currentDomain();
  const span = hi - lo || 1;
  const lut = buildLut(RAMPS[state.scale.ramp] || RAMPS.rdylgn);
  const pixels = state.ndvi.pixels;
  const out = state.ndvi.imageData.data;

  for (let i = 0; i < pixels.length; i += 4) {
    if (pixels[i + 3] === 0) {
      out[i + 3] = 0;
      continue;
    }
    const ndvi = encMin + (pixels[i] / 255) * (encMax - encMin);
    let t = (ndvi - lo) / span;
    t = t < 0 ? 0 : t > 1 ? 1 : t;
    const k = (t * 255) | 0;
    out[i] = lut[k * 3];
    out[i + 1] = lut[k * 3 + 1];
    out[i + 2] = lut[k * 3 + 2];
    out[i + 3] = 235;
  }
  state.ndvi.canvas.getContext("2d")
    .putImageData(state.ndvi.imageData, 0, 0);
  scheduleOverlayUpdate(paintId);
}

function scheduleOverlayUpdate(paintId) {
  const token = state.selectionToken;
  const ndviToken = state.ndvi.token;
  clearTimeout(state.ndvi.timer);
  state.ndvi.timer = setTimeout(() => {
    const canvas = state.ndvi.canvas;
    const info = state.ndvi.info;
    if (!canvas || !info || info.status !== "ready") return;
    canvas.toBlob((blob) => {
      if (!blob || token !== state.selectionToken
          || ndviToken !== state.ndvi.token
          || paintId !== state.ndvi.paintId || !state.ndvi.info) return;
      const url = URL.createObjectURL(blob);
      if (state.ndvi.overlay) {
        state.ndvi.overlay.setUrl(url);
      } else {
        state.ndvi.overlay = L.imageOverlay(url, info.bounds, {
          pane: "ndviPane",
          opacity: 0.95,
          interactive: false,
          className: "ndvi-img",
        }).addTo(state.map);
      }
      if (state.ndvi.url) URL.revokeObjectURL(state.ndvi.url);
      state.ndvi.url = url;
    }, "image/png");
  }, 100);
}

function clearNdviOverlay() {
  state.ndvi.token += 1;
  clearTimeout(state.ndvi.timer);
  if (state.ndvi.overlay) {
    state.map.removeLayer(state.ndvi.overlay);
    state.ndvi.overlay = null;
  }
  if (state.ndvi.url) {
    URL.revokeObjectURL(state.ndvi.url);
    state.ndvi.url = null;
  }
  state.ndvi.pixels = null;
  state.ndvi.imageData = null;
  state.ndvi.canvas = null;
}

function loadImage(src) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.crossOrigin = "anonymous";
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error("could not load field NDVI image"));
    img.src = src;
  });
}

function isAbort(err) {
  return err && (err.name === "AbortError" || err.code === 20);
}

async function loadFieldNdvi(fieldId, token, signal) {
  const { year, period } = state;
  clearNdviOverlay();
  const ndviToken = state.ndvi.token;
  const stale = () => token !== state.selectionToken || ndviToken !== state.ndvi.token;

  state.ndvi.info = null;
  updateFieldStretchOption();
  setNdviStatus("Loading 10 m NDVI…", "loading");

  let info;
  try {
    const res = await fetch(
      `/api/field-ndvi?field_id=${encodeURIComponent(fieldId)}&year=${year}&period=${period}`,
      { signal });
    if (!res.ok) throw new Error(`request failed (${res.status})`);
    info = await res.json();
  } catch (err) {
    if (isAbort(err) || stale()) return;
    setNdviStatus(`NDVI failed: ${err.message}`, "error");
    return;
  }
  if (stale()) return;

  state.ndvi.info = info;
  renderPixelStats(info);
  updateFieldStretchOption();
  updateLegend();

  if (info.status !== "ready") {
    setNdviStatus(info.status === "not_computed"
      ? "No 10 m composite for this period."
      : "No clear NDVI pixels in this field.");
    return;
  }

  let img;
  try {
    img = await loadImage(`${info.png_url}&v=${Date.now()}`);
  } catch (err) {
    if (stale()) return;
    setNdviStatus(`NDVI failed: ${err.message}`, "error");
    return;
  }
  if (stale()) return;

  const canvas = document.createElement("canvas");
  canvas.width = img.width;
  canvas.height = img.height;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  ctx.drawImage(img, 0, 0);
  state.ndvi.canvas = canvas;
  state.ndvi.pixels = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
  state.ndvi.imageData = ctx.createImageData(canvas.width, canvas.height);
  paintNdvi();

  const s = info.stats || {};
  setNdviStatus(
    `${Number(s.pixels || 0).toLocaleString()} px · mean ${fmt(s.mean, 3)}`
    + ` · ${fmt(s.min, 2)}–${fmt(s.max, 2)}`);
}

/* ------------------------------------------------------------------ */
/* Soils                                                               */
/* ------------------------------------------------------------------ */
function clearSoilLayer() {
  state.soil.token += 1;
  if (state.soil.layer) {
    state.map.removeLayer(state.soil.layer);
    state.soil.layer = null;
  }
}

async function loadFieldSoils(fieldId, token, signal) {
  clearSoilLayer();
  const soilToken = state.soil.token;
  const stale = () => token !== state.selectionToken || soilToken !== state.soil.token;

  state.soil.data = null;
  setSoilStatus("Loading SSURGO polygons…", "loading");

  let data;
  try {
    const res = await fetch(
      `/api/soils.geojson?field_id=${encodeURIComponent(fieldId)}`, { signal });
    if (!res.ok) throw new Error(`request failed (${res.status})`);
    data = await res.json();
  } catch (err) {
    if (isAbort(err) || stale()) return;
    setSoilStatus(`SSURGO failed: ${err.message}`, "error");
    return;
  }
  if (stale()) return;

  const features = data.features || [];
  if (!features.length) {
    setSoilStatus("No SSURGO polygons for this field.");
    return;
  }

  features.forEach((f) => {
    if (!f._bbox) f._bbox = FieldGeo.bboxOf(f.geometry);
  });
  state.soil.data = data;
  state.soil.layer = L.geoJSON(data, {
    pane: "soilPane",
    interactive: false,
    style: styleSoil,
  }).addTo(state.map);

  const units = data.map_units || [];
  const coverage = data.coverage_pct == null
    ? "" : ` · ${Number(data.coverage_pct).toFixed(1)}% covered`;
  setSoilStatus(
    `${features.length} polygons · ${units.length} map units${coverage}`
    + " · click a polygon for attributes");
}

/* ------------------------------------------------------------------ */
/* County raster metadata + compute job                                */
/* ------------------------------------------------------------------ */
async function loadRasterMeta() {
  const token = (state.rasterToken += 1);
  const { year, period } = state;
  el("statusline").textContent = "Loading 10 m composite…";
  el("compute").classList.add("d-none");

  let info;
  try {
    info = await fetch(`/api/ndvi-raster?year=${year}&period=${period}`)
      .then((r) => r.json());
  } catch (err) {
    if (token !== state.rasterToken) return;
    el("statusline").textContent = `Raster request failed: ${err.message}`;
    return;
  }
  if (token !== state.rasterToken) return;

  state.rasterMeta = info;
  renderRasterStatus();
  updateLegend();
  updateFieldStretchOption();

  if (state.selected) {
    if (state.ndvi.visible) {
      loadFieldNdvi(state.selected, state.selectionToken,
        state.selectionAbort ? state.selectionAbort.signal : undefined);
    } else {
      setPixelStatsMessage("NDVI layer is hidden.");
    }
  }
}

function renderRasterStatus() {
  const info = state.rasterMeta;
  if (!info || info.status === "not_computed") {
    el("statusline").innerHTML =
      `<b>${state.year}</b> P${String(state.period).padStart(2, "0")} has no 10 m composite yet.`;
    el("compute").classList.remove("d-none");
    return;
  }
  if (info.status === "empty") {
    el("statusline").innerHTML =
      "No clear Sentinel-2 pixels for this period. Nothing is filled in.";
    el("compute").classList.add("d-none");
    return;
  }
  const s = info.stats || {};
  el("statusline").innerHTML =
    `<b>10 m composite</b> · ${Number(s.pixels || 0).toLocaleString()} px`
    + ` · ${info.n_dates || 0} dates · county mean <b>${fmt(s.mean, 3)}</b>`;
  el("compute").classList.add("d-none");
}

async function pollJob() {
  const res = await fetch("/api/job");
  const job = await res.json();
  state.job = job;
  if (job.state === "running") {
    const pct = job.total ? Math.round((job.done / job.total) * 100) : 5;
    el("jobwrap").classList.remove("d-none");
    el("jobbar").style.width = `${Math.max(pct, 5)}%`;
    el("jobnote").classList.remove("d-none");
    el("jobnote").textContent = job.message || "processing…";
    state.polling = setTimeout(pollJob, 1500);
    return;
  }
  el("jobwrap").classList.add("d-none");
  el("jobnote").classList.add("d-none");
  state.polling = null;
  if (job.state === "done" && job.kind === "raster") {
    await loadRasterMeta();
  } else if (job.state === "error") {
    el("statusline").textContent = `Computation failed: ${job.error}`;
    el("compute").classList.remove("d-none");
  }
}

async function startCompute() {
  const res = await fetch(`/api/compute-raster/${state.year}/${state.period}`,
    { method: "POST" });
  const body = await res.json();
  if (!body.ok) {
    el("jobnote").classList.remove("d-none");
    el("jobnote").textContent = body.error || "could not start";
    return;
  }
  el("compute").classList.add("d-none");
  el("jobwrap").classList.remove("d-none");
  el("jobbar").style.width = "5%";
  el("jobnote").classList.remove("d-none");
  el("jobnote").textContent = "starting…";
  pollJob();
}

/* ------------------------------------------------------------------ */
/* Field detail                                                        */
/* ------------------------------------------------------------------ */
function showDetailLoading(feature) {
  const p = feature.properties;
  el("detail").classList.remove("d-none");
  el("d-error").classList.add("d-none");
  el("d-id").textContent = p.field_id;
  el("d-acres").textContent = `${Number(p.acres).toFixed(2)} ac`;
  el("d-class").textContent = p.ag_class === "crop"
    ? "crop field" : String(p.ag_class || "—").replace(/_/g, " ");
  el("d-crop").textContent = p.crop_name || "no CDL class";
  el("d-crop").className = "chip neutral";
  el("d-pixstats").innerHTML = `<span class="muted">Loading 10 m NDVI…</span>`;
  el("d-soilname").textContent = "Loading soil summary…";
  el("d-soilprops").innerHTML = "";
  el("d-units").innerHTML = "";
  el("d-croprows").innerHTML =
    `<tr><td colspan="3" class="text-muted">Loading crop history…</td></tr>`;
  el("chartnote").textContent = "Loading field-average NDVI…";
  if (state.chart) {
    state.chart.destroy();
    state.chart = null;
  }
}

function showDetailError(message) {
  const node = el("d-error");
  node.textContent = `Field detail unavailable: ${message}`;
  node.classList.remove("d-none");
}

async function loadFieldDetail(fieldId, token, signal) {
  let detail;
  try {
    const res = await fetch(`/api/field/${encodeURIComponent(fieldId)}`, { signal });
    if (!res.ok) throw new Error(`request failed (${res.status})`);
    detail = await res.json();
  } catch (err) {
    if (isAbort(err) || token !== state.selectionToken) return;
    showDetailError(err.message);
    return;
  }
  if (token !== state.selectionToken) return;
  renderDetail(detail);
}

function renderDetail(d) {
  el("d-error").classList.add("d-none");
  el("d-id").textContent = d.field_id;
  el("d-acres").textContent = `${d.acres.toFixed(2)} ac`;
  el("d-class").textContent = d.ag_class === "crop"
    ? "crop field" : d.ag_class.replace(/_/g, " ");
  el("d-crop").textContent = d.crop_name || "no CDL class";
  el("d-crop").className = "chip neutral";

  const soils = d.soils || {};
  const p = soils.properties || {};
  el("d-soilname").textContent = soils.dominant
    ? `${soils.dominant} (${soils.n_units} map unit${soils.n_units === 1 ? "" : "s"})`
    : "No SSURGO summary for this field.";
  const v = (x, suffix = "") => (x == null ? "—" : `${x}${suffix}`);
  el("d-soilprops").innerHTML = [
    ["Organic matter", v(p.top_om_r, " %")],
    ["CEC", v(p.top_cec7_r)],
    ["AWC", v(p.top_awc_r)],
    ["pH", v(p.top_ph1to1h2o_r)],
    ["Clay", v(p.top_claytotal_r, " %")],
    ["Sand", v(p.top_sandtotal_r, " %")],
  ].map(([k, val]) => `<div>${k} <b>${esc(val)}</b></div>`).join("");

  const units = soils.units || [];
  el("d-units").innerHTML = units.length
    ? units.map((u) => `<span title="${esc(u.muname)}">${esc(u.musym)}${
        u.comppct == null ? "" : ` ${esc(u.comppct)}%`}</span>`).join("")
    : `<span class="muted">No map units listed.</span>`;

  const history = d.crop_history || [];
  el("d-croprows").innerHTML = history.length
    ? history.map((c) =>
        `<tr><td>${c.year}</td><td>${esc(c.crop_name)}</td><td class="text-end">${
          c.crop_pct == null ? "—" : c.crop_pct.toFixed(1)}</td></tr>`).join("")
    : `<tr><td colspan="3" class="text-muted">No CDL history for this field.</td></tr>`;

  drawChart(d);
}

/* ------------------------------------------------------------------ */
/* Chart (field-average NDVI)                                          */
/* ------------------------------------------------------------------ */
function drawChart(d) {
  if (!state.meta) return;
  const labels = state.meta.periods.map((p) => p.label.replace(" - ", "–"));
  const years = [2020, 2021, 2022, 2023, 2024, 2025, 2026];
  const values = (d.ndvi && d.ndvi.values) || {};
  const datasets = years.map((y) => ({
    label: String(y),
    data: values[y] || [],
    borderColor: YEAR_COLORS[y],
    backgroundColor: YEAR_COLORS[y],
    borderWidth: y === state.year ? 3 : 1.2,
    pointRadius: y === state.year ? 2.6 : 0,
    tension: 0.28,
    spanGaps: false,
    pointHoverRadius: 3,
  }));

  const ctx = el("chart");
  if (state.chart) state.chart.destroy();
  state.chart = new Chart(ctx, {
    type: "line",
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { position: "bottom", labels: { boxWidth: 10, boxHeight: 10, font: { size: 10 } } },
        tooltip: {
          callbacks: {
            label: (c) => `${c.dataset.label}: ${c.parsed.y == null ? "no data" : c.parsed.y.toFixed(3)}`,
          },
        },
      },
      scales: {
        y: { min: 0, max: 1, ticks: { stepSize: 0.2, font: { size: 10 } },
             title: { display: true, text: "NDVI", font: { size: 10 } } },
        x: { ticks: { maxRotation: 60, minRotation: 60, font: { size: 8 } }, grid: { display: false } },
      },
    },
  });

  const any = years.some((y) => (values[y] || []).some((v) => v != null));
  el("chartnote").textContent = any
    ? "Field-average biweekly NDVI. Empty periods had no usable imagery."
    : "No field-average NDVI for this field (outside the crop layer or below one pixel).";
}

function refreshChartHighlight() {
  if (!state.chart) return;
  state.chart.data.datasets.forEach((ds) => {
    const y = Number(ds.label);
    ds.borderWidth = y === state.year ? 3 : 1.2;
    ds.pointRadius = y === state.year ? 2.6 : 0;
  });
  state.chart.update();
}

/* ------------------------------------------------------------------ */
/* Controls                                                            */
/* ------------------------------------------------------------------ */
function setPeriodLabel() {
  const p = state.meta.periods[state.period - 1];
  if (!p) return;
  el("period-label").textContent = `P${String(state.period).padStart(2, "0")} · ${p.label}`;
}

function toggleNdvi(visible) {
  state.ndvi.visible = visible;
  if (!state.selected) {
    setNdviStatus(visible
      ? "Select a field to view 10 m NDVI." : "NDVI hidden.");
    return;
  }
  if (!visible) {
    clearNdviOverlay();
    state.ndvi.info = null;
    setNdviStatus("NDVI hidden.");
    setPixelStatsMessage("NDVI layer is hidden.");
    updateFieldStretchOption();
    updateLegend();
    return;
  }
  setNdviStatus("Loading 10 m NDVI…", "loading");
  loadFieldNdvi(state.selected, state.selectionToken,
    state.selectionAbort ? state.selectionAbort.signal : undefined);
}

function toggleSoils(visible) {
  state.soil.visible = visible;
  if (!state.selected) {
    setSoilStatus(visible
      ? "Select a field to view SSURGO soils." : "SSURGO hidden.");
    return;
  }
  if (!visible) {
    clearSoilLayer();
    state.soil.data = null;
    setSoilStatus("SSURGO hidden.");
    return;
  }
  setSoilStatus("Loading SSURGO polygons…", "loading");
  loadFieldSoils(state.selected, state.selectionToken,
    state.selectionAbort ? state.selectionAbort.signal : undefined);
}

function bindControls() {
  const yearSelect = el("year");
  state.meta.years.forEach((y) => {
    const opt = document.createElement("option");
    opt.value = y;
    opt.textContent = y;
    yearSelect.appendChild(opt);
  });
  yearSelect.value = state.year;
  yearSelect.addEventListener("change", () => {
    state.year = Number(yearSelect.value);
    setPeriodLabel();
    refreshChartHighlight();
    loadRasterMeta();
  });

  const slider = el("period");
  slider.max = state.meta.periods.length;
  slider.value = state.period;
  slider.addEventListener("input", () => {
    state.period = Number(slider.value);
    setPeriodLabel();
    loadRasterMeta();
  });

  el("play").addEventListener("click", () => {
    if (state.timer) {
      clearInterval(state.timer);
      state.timer = null;
      el("play").innerHTML = '<i class="fa-solid fa-play"></i>';
      return;
    }
    el("play").innerHTML = '<i class="fa-solid fa-pause"></i>';
    state.timer = setInterval(() => {
      state.period = (state.period % state.meta.periods.length) + 1;
      slider.value = state.period;
      setPeriodLabel();
      loadRasterMeta();
    }, 2600);
  });

  el("ramp").addEventListener("change", (event) => {
    state.scale.ramp = event.target.value;
    updateLegend();
    paintNdvi();
  });

  el("stretch").addEventListener("change", (event) => {
    state.scale.stretch = event.target.value;
    el("manualwrap").classList.toggle("d-none", state.scale.stretch !== "manual");
    updateLegend();
    paintNdvi();
  });

  ["minval", "maxval"].forEach((id) => {
    el(id).addEventListener("change", () => {
      state.scale.stretch = "manual";
      el("stretch").value = "manual";
      el("manualwrap").classList.remove("d-none");
      updateLegend();
      paintNdvi();
    });
  });

  el("ndvitoggle").addEventListener("change", (event) => toggleNdvi(event.target.checked));
  el("soiltoggle").addEventListener("change", (event) => toggleSoils(event.target.checked));
  el("compute").addEventListener("click", startCompute);
  el("close-detail").addEventListener("click", clearSelection);

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") clearSelection();
  });
}

/* ------------------------------------------------------------------ */
/* Init                                                                */
/* ------------------------------------------------------------------ */
async function init() {
  const meta = await fetch("/api/meta").then((r) => r.json());
  state.meta = meta;
  el("stat-fields").textContent = meta.fields.toLocaleString();
  el("stat-acres").textContent = meta.acres.toLocaleString();
  el("stat-crop").textContent = meta.crop_fields.toLocaleString();

  const fields = await fetch("/api/fields.geojson").then((r) => r.json());
  buildMap(fields);

  try {
    const cached = await fetch("/api/rasters").then((r) => r.json());
    const list = (cached.rasters || []).filter((r) => r.year && r.period);
    if (list.length) {
      list.sort((a, b) => (a.year - b.year) || (a.period - b.period));
      const last = list[list.length - 1];
      state.year = last.year;
      state.period = last.period;
    }
  } catch (err) {
    console.warn("could not list cached rasters", err);
  }

  bindControls();
  el("year").value = state.year;
  el("period").value = state.period;
  setPeriodLabel();
  updateLegend();
  setTogglesEnabled(false);
  setNdviStatus("Select a field to view 10 m NDVI.");
  setSoilStatus("Select a field to view SSURGO soils.");
  await loadRasterMeta();

  const job = await fetch("/api/job").then((r) => r.json());
  if (job.state === "running") pollJob();
}

init().catch((err) => {
  el("statusline").textContent = `Failed to load: ${err.message}`;
  console.error(err);
});
