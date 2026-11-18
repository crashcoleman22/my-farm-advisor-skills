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
  soil: { visible: false, layer: null, data: null },
  raster: {
    info: null,
    pixels: null,
    shape: [0, 0],
    overlay: null,
    canvas: null,
    imageData: null,
    url: null,
    token: 0,
    timer: null,
  },
  scale: { ramp: "rdylgn", stretch: "fixed", min: -0.2, max: 1.0 },
  fieldFeatures: [],
  fieldLayer: null,
  map: null,
  layers: {},
  selected: null,
  selectedStats: null,
  chart: null,
  timer: null,
  polling: null,
  job: null,
};

const el = (id) => document.getElementById(id);
const fmt = (v, n = 2) => (v == null || Number.isNaN(v) ? "—" : Number(v).toFixed(n));

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
/* Geo helpers                                                         */
/* ------------------------------------------------------------------ */
function pointInRing(x, y, ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i, i += 1) {
    const xi = ring[i][0];
    const yi = ring[i][1];
    const xj = ring[j][0];
    const yj = ring[j][1];
    if (((yi > y) !== (yj > y))
        && (x < ((xj - xi) * (y - yi)) / (yj - yi || 1e-12) + xi)) {
      inside = !inside;
    }
  }
  return inside;
}

function pointInPolygon(x, y, rings) {
  if (!rings.length || !pointInRing(x, y, rings[0])) return false;
  for (let i = 1; i < rings.length; i += 1) {
    if (pointInRing(x, y, rings[i])) return false;
  }
  return true;
}

function fieldAt(latlng) {
  const x = latlng.lng;
  const y = latlng.lat;
  for (const feature of state.fieldFeatures) {
    const bb = feature._bbox;
    if (x < bb[0] || x > bb[2] || y < bb[1] || y > bb[3]) continue;
    const geom = feature.geometry;
    if (geom.type === "Polygon") {
      if (pointInPolygon(x, y, geom.coordinates)) return feature.properties.field_id;
    } else if (geom.type === "MultiPolygon") {
      for (const poly of geom.coordinates) {
        if (pointInPolygon(x, y, poly)) return feature.properties.field_id;
      }
    }
  }
  return null;
}

function bboxOf(geom) {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  const walk = (coords) => {
    if (typeof coords[0] === "number") {
      x0 = Math.min(x0, coords[0]); x1 = Math.max(x1, coords[0]);
      y0 = Math.min(y0, coords[1]); y1 = Math.max(y1, coords[1]);
      return;
    }
    coords.forEach(walk);
  };
  walk(geom.coordinates);
  return [x0, y0, x1, y1];
}

/* ------------------------------------------------------------------ */
/* Map                                                                 */
/* ------------------------------------------------------------------ */
function styleField(feature) {
  const crop = feature.properties.ag_class === "crop";
  return {
    color: crop ? "rgba(25,45,25,.55)" : "rgba(70,90,70,.35)",
    weight: crop ? 0.7 : 0.4,
    fillColor: "#ffffff",
    fillOpacity: 0.01,
    opacity: 1,
  };
}

function soilColor(musym) {
  let h = 0;
  for (let i = 0; i < musym.length; i += 1) h = (h * 31 + musym.charCodeAt(i)) >>> 0;
  return SOIL_COLORS[h % SOIL_COLORS.length];
}

function styleSoil(feature) {
  return {
    color: "#3f3324",
    weight: 0.6,
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
  return `<div class="soilpop"><b>${p.musym}</b> — ${p.muname}`
    + `<table>${rows.map(([k, v]) => `<tr><th>${k}</th><td>${v}</td></tr>`).join("")}</table></div>`;
}

function buildMap(fields) {
  const map = L.map("map", { preferCanvas: true, zoomControl: true })
    .setView([39.49, -86.1], 11);
  state.map = map;

  map.createPane("ndviPane").style.zIndex = 350;
  map.createPane("fieldPane").style.zIndex = 400;
  map.createPane("soilPane").style.zIndex = 410;

  L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    { maxZoom: 19, attribution: "Imagery &copy; Esri, Maxar, Earthstar Geographics" }
  ).addTo(map);

  state.fieldFeatures = fields.features;
  fields.features.forEach((f) => { f._bbox = bboxOf(f.geometry); });

  state.fieldLayer = L.geoJSON(fields, {
    pane: "fieldPane",
    style: styleField,
    onEachFeature(feature, lyr) {
      state.layers[feature.properties.field_id] = lyr;
      lyr.bindTooltip(() => {
        const p = feature.properties;
        const crop = p.crop_name ? ` · ${p.crop_name}` : "";
        return `<b>${p.acres.toFixed(1)} ac</b>${crop}`;
      }, { sticky: true, className: "leaflet-tooltip" });
      lyr.on("mouseover", () => lyr.setStyle({ weight: 2.4, color: "#123d14" }));
      lyr.on("mouseout", () => lyr.setStyle(styleField(feature)));
      lyr.on("click", () => selectField(feature.properties.field_id));
    },
  }).addTo(map);

  map.on("click", (event) => {
    const fieldId = fieldAt(event.latlng);
    if (fieldId) selectField(fieldId);
  });

  map.fitBounds(state.fieldLayer.getBounds(), { padding: [12, 12] });
}

/* ------------------------------------------------------------------ */
/* Raster                                                              */
/* ------------------------------------------------------------------ */
function encodingDomain() {
  const enc = state.raster.info && state.raster.info.encoding;
  return [enc ? enc.min : -0.2, enc ? enc.max : 1.0];
}

function currentDomain() {
  const [encMin, encMax] = encodingDomain();
  const info = state.raster.info;
  const mode = state.scale.stretch;

  if (mode === "auto" && info && info.stats && info.stats.pixels) {
    const lo = info.stats.p02;
    const hi = info.stats.p98;
    if (hi - lo > 0.02) return [lo, hi];
  }
  if (mode === "field" && state.selectedStats && state.selectedStats.status === "ready") {
    const lo = state.selectedStats.p02;
    const hi = state.selectedStats.p98;
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
  const info = state.raster.info;
  el("legendnote").textContent = info && info.status === "ready"
    ? `NDVI · ${info.year} P${String(info.period).padStart(2, "0")} · ${info.n_dates} clear dates`
    : "NDVI · no composite loaded";
}

function renderStatus() {
  const info = state.raster.info;
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
  const s = info.stats;
  el("statusline").innerHTML =
    `<b>10 m NDVI</b> · ${s.pixels.toLocaleString()} px · ${info.n_dates} dates · `
    + `mean <b>${fmt(s.mean, 3)}</b> · ${fmt(s.min, 2)}–${fmt(s.max, 2)}`;
  el("compute").classList.add("d-none");
}

async function loadRaster() {
  const { year, period } = state;
  const token = (state.raster.token += 1);
  el("statusline").textContent = "Loading 10 m NDVI…";
  el("compute").classList.add("d-none");

  let info;
  try {
    info = await fetch(`/api/ndvi-raster?year=${year}&period=${period}`)
      .then((r) => r.json());
  } catch (err) {
    el("statusline").textContent = `Raster request failed: ${err.message}`;
    return;
  }
  if (token !== state.raster.token) return;

  state.raster.info = info;
  clearOverlay();
  renderStatus();
  updateLegend();
  updateFieldStretchOption();

  if (info.status !== "ready") {
    state.selectedStats = null;
    updateFieldStretchOption();
    if (state.selected) renderPixelStats({ status: "not_computed" });
    return;
  }

  const img = new Image();
  img.crossOrigin = "anonymous";
  const loaded = new Promise((resolve, reject) => {
    img.onload = resolve;
    img.onerror = () => reject(new Error("could not load raster image"));
  });
  img.src = `${info.png_url}&v=${Date.now()}`;
  try {
    await loaded;
  } catch (err) {
    el("statusline").textContent = err.message;
    return;
  }
  if (token !== state.raster.token) return;

  const canvas = document.createElement("canvas");
  canvas.width = img.width;
  canvas.height = img.height;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  ctx.drawImage(img, 0, 0);
  const source = ctx.getImageData(0, 0, canvas.width, canvas.height);
  state.raster.canvas = canvas;
  state.raster.pixels = source.data;
  state.raster.imageData = ctx.createImageData(canvas.width, canvas.height);
  state.raster.shape = [canvas.height, canvas.width];
  state.raster.paintId = (state.raster.paintId || 0) + 1;
  paintRaster();

  if (state.selected) refreshSelectedStats();
}

function paintRaster() {
  const pixels = state.raster.pixels;
  if (!pixels) return;
  const paintId = (state.raster.paintId = (state.raster.paintId || 0) + 1);
  const [encMin, encMax] = encodingDomain();
  const [lo, hi] = currentDomain();
  const span = hi - lo || 1;
  const lut = buildLut(RAMPS[state.scale.ramp] || RAMPS.rdylgn);
  const out = state.raster.imageData.data;
  const alpha = state.soil.visible ? 205 : 235;

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
    out[i + 3] = alpha;
  }
  state.raster.canvas.getContext("2d").putImageData(state.raster.imageData, 0, 0);
  scheduleOverlayUpdate(paintId);
}

function scheduleOverlayUpdate(paintId) {
  const token = state.raster.token;
  clearTimeout(state.raster.timer);
  state.raster.timer = setTimeout(() => {
    const canvas = state.raster.canvas;
    if (!canvas) return;
    canvas.toBlob((blob) => {
      if (!blob || token !== state.raster.token
          || paintId !== state.raster.paintId) return;
      const url = URL.createObjectURL(blob);
      const info = state.raster.info;
      if (state.raster.overlay) {
        state.raster.overlay.setUrl(url);
      } else {
        state.raster.overlay = L.imageOverlay(url, info.bounds, {
          pane: "ndviPane", opacity: 0.92, interactive: false,
          className: "ndvi-img",
        }).addTo(state.map);
      }
      if (state.raster.url) URL.revokeObjectURL(state.raster.url);
      state.raster.url = url;
    }, "image/png");
  }, 120);
}

function clearOverlay() {
  if (state.raster.overlay) {
    state.map.removeLayer(state.raster.overlay);
    state.raster.overlay = null;
  }
  if (state.raster.url) {
    URL.revokeObjectURL(state.raster.url);
    state.raster.url = null;
  }
  state.raster.pixels = null;
  state.raster.imageData = null;
  state.raster.canvas = null;
}

/* ------------------------------------------------------------------ */
/* Soils                                                               */
/* ------------------------------------------------------------------ */
async function toggleSoils(visible) {
  state.soil.visible = visible;
  if (!visible) {
    if (state.soil.layer) state.map.removeLayer(state.soil.layer);
    if (state.raster.pixels) paintRaster();
    return;
  }
  if (!state.soil.data) {
    el("statusline").textContent = "Loading SSURGO soil map units…";
    try {
      state.soil.data = await fetch("/api/soils.geojson").then((r) => r.json());
    } catch (err) {
      el("statusline").textContent = `Soil layer failed: ${err.message}`;
      el("soiltoggle").checked = false;
      state.soil.visible = false;
      return;
    }
    state.soil.layer = L.geoJSON(state.soil.data, {
      pane: "soilPane",
      renderer: L.canvas({ pane: "soilPane" }),
      style: styleSoil,
      onEachFeature(feature, lyr) {
        const p = feature.properties;
        lyr.bindTooltip(`${p.musym} · ${p.muname}`,
                        { sticky: true, className: "leaflet-tooltip" });
        lyr.bindPopup(soilPopup(p), { maxWidth: 320 });
        lyr.on("click", (event) => {
          const fieldId = fieldAt(event.latlng);
          if (fieldId) selectField(fieldId);
        });
        lyr.on("mouseover", () => lyr.setStyle({ weight: 2, color: "#1f160c" }));
        lyr.on("mouseout", () => lyr.setStyle(styleSoil(feature)));
      },
    });
    el("statusline").textContent =
      `${state.soil.data.features.length.toLocaleString()} SSURGO map-unit polygons`;
    setTimeout(renderStatus, 1800);
  }
  state.soil.layer.addTo(state.map);
  if (state.raster.pixels) paintRaster();
}

/* ------------------------------------------------------------------ */
/* Field detail                                                        */
/* ------------------------------------------------------------------ */
async function selectField(fieldId) {
  state.selected = fieldId;
  const res = await fetch(`/api/field/${encodeURIComponent(fieldId)}`);
  if (!res.ok) return;
  const d = await res.json();
  renderDetail(d);
  const lyr = state.layers[fieldId];
  if (lyr && lyr.bringToFront) lyr.bringToFront();
  refreshSelectedStats();
}

function renderDetail(d) {
  el("detail").classList.remove("d-none");
  el("d-id").textContent = d.field_id;
  el("d-acres").textContent = `${d.acres.toFixed(2)} ac`;
  el("d-class").textContent = d.ag_class === "crop" ? "crop field" : d.ag_class.replace(/_/g, " ");
  el("d-crop").textContent = d.crop_name || "no CDL class";
  el("d-crop").className = "chip neutral";

  const p = d.soils.properties;
  el("d-soilname").textContent =
    `${d.soils.dominant} (${d.soils.n_units} map unit${d.soils.n_units === 1 ? "" : "s"})`;
  const v = (x, suffix = "") => (x == null ? "—" : `${x}${suffix}`);
  el("d-soilprops").innerHTML = [
    ["Organic matter", v(p.top_om_r, " %")],
    ["CEC", v(p.top_cec7_r)],
    ["AWC", v(p.top_awc_r)],
    ["pH", v(p.top_ph1to1h2o_r)],
    ["Clay", v(p.top_claytotal_r, " %")],
    ["Sand", v(p.top_sandtotal_r, " %")],
  ].map(([k, val]) => `<div>${k} <b>${val}</b></div>`).join("");

  el("d-units").innerHTML = d.soils.units
    .map((u) => `<span title="${u.muname}">${u.musym}${u.comppct == null ? "" : ` ${u.comppct}%`}</span>`)
    .join("");

  const rows = d.crop_history.length
    ? d.crop_history.map((c) =>
        `<tr><td>${c.year}</td><td>${c.crop_name}</td><td class="text-end">${
          c.crop_pct == null ? "—" : c.crop_pct.toFixed(1)}</td></tr>`).join("")
    : `<tr><td colspan="3" class="text-muted">No CDL history for this field.</td></tr>`;
  el("d-croprows").innerHTML = rows;

  drawChart(d);
}

async function refreshSelectedStats() {
  if (!state.selected) return;
  const { year, period } = state;
  const res = await fetch(
    `/api/field-raster-stats?year=${year}&period=${period}&field_id=${encodeURIComponent(state.selected)}`
  );
  const s = await res.json();
  if (state.selected == null) return;
  state.selectedStats = s;
  renderPixelStats(s);
  updateFieldStretchOption();
  if (state.scale.stretch === "field") {
    updateLegend();
    paintRaster();
  }
}

function renderPixelStats(s) {
  const box = el("d-pixstats");
  if (!s || s.status === "not_computed") {
    box.innerHTML = `<span class="muted">No 10 m composite for ${state.year} P${String(state.period).padStart(2, "0")}.</span>`;
    return;
  }
  if (s.status === "no_pixels") {
    box.innerHTML = `<span class="muted">No clear pixels inside this field for this period.</span>`;
    return;
  }
  box.innerHTML = `
    <div class="pixgrid">
      <div>pixels <b>${s.pixels.toLocaleString()}</b></div>
      <div>min <b>${fmt(s.min, 3)}</b></div>
      <div>max <b>${fmt(s.max, 3)}</b></div>
      <div>mean <b>${fmt(s.mean, 3)}</b></div>
      <div>std <b>${fmt(s.std, 3)}</b></div>
      <div>p02–p98 <b>${fmt(s.p02, 2)}–${fmt(s.p98, 2)}</b></div>
    </div>`;
}

function updateFieldStretchOption() {
  const opt = el("stretch").querySelector('option[value="field"]');
  const ready = state.selectedStats && state.selectedStats.status === "ready";
  opt.disabled = !ready;
  opt.textContent = ready ? "Selected field 2–98 %" : "Selected field (pick a field)";
}

/* ------------------------------------------------------------------ */
/* Chart (field-average NDVI)                                          */
/* ------------------------------------------------------------------ */
function drawChart(d) {
  const labels = state.meta.periods.map((p) => p.label.replace(" - ", "–"));
  const years = [2020, 2021, 2022, 2023, 2024, 2025, 2026];
  const datasets = years.map((y) => ({
    label: String(y),
    data: d.ndvi.values[y],
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

  const any = years.some((y) => (d.ndvi.values[y] || []).some((v) => v != null));
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
/* Jobs                                                                */
/* ------------------------------------------------------------------ */
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
  if (job.state === "done" && job.kind === "raster") await loadRaster();
  else if (job.state === "error") {
    el("statusline").textContent = `Computation failed: ${job.error}`;
    el("compute").classList.remove("d-none");
  }
}

async function startCompute() {
  const res = await fetch(`/api/compute-raster/${state.year}/${state.period}`, { method: "POST" });
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
/* Controls                                                            */
/* ------------------------------------------------------------------ */
function setPeriodLabel() {
  const p = state.meta.periods[state.period - 1];
  el("period-label").textContent = `P${String(state.period).padStart(2, "0")} · ${p.label}`;
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
    loadRaster();
    refreshChartHighlight();
  });

  const slider = el("period");
  slider.max = state.meta.periods.length;
  slider.value = state.period;
  slider.addEventListener("input", () => {
    state.period = Number(slider.value);
    setPeriodLabel();
    loadRaster();
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
      loadRaster();
    }, 2600);
  });

  el("ramp").addEventListener("change", (event) => {
    state.scale.ramp = event.target.value;
    updateLegend();
    paintRaster();
  });

  el("stretch").addEventListener("change", (event) => {
    state.scale.stretch = event.target.value;
    el("manualwrap").classList.toggle("d-none", state.scale.stretch !== "manual");
    updateLegend();
    paintRaster();
  });

  ["minval", "maxval"].forEach((id) => {
    el(id).addEventListener("change", () => {
      state.scale.stretch = "manual";
      el("stretch").value = "manual";
      el("manualwrap").classList.remove("d-none");
      updateLegend();
      paintRaster();
    });
  });

  el("soiltoggle").addEventListener("change", (event) => toggleSoils(event.target.checked));
  el("compute").addEventListener("click", startCompute);
  el("close-detail").addEventListener("click", () => {
    el("detail").classList.add("d-none");
    state.selected = null;
    state.selectedStats = null;
    updateFieldStretchOption();
    if (state.scale.stretch === "field") {
      state.scale.stretch = "fixed";
      el("stretch").value = "fixed";
      updateLegend();
      paintRaster();
    }
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
  await loadRaster();

  const job = await fetch("/api/job").then((r) => r.json());
  if (job.state === "running") pollJob();
}

init().catch((err) => {
  el("statusline").textContent = `Failed to load: ${err.message}`;
  console.error(err);
});
