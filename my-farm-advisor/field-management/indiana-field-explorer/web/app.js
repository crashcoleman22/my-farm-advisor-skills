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

const NDVI_STOPS = [
  [0.10, "#d73027"],
  [0.25, "#fc8d59"],
  [0.42, "#fee08b"],
  [0.58, "#d9ef8b"],
  [0.72, "#66bd63"],
  [0.85, "#1a9850"],
];

const state = {
  meta: null,
  year: 2025,
  period: 1,
  values: {},
  status: "ready",
  layers: {},
  selected: null,
  chart: null,
  timer: null,
  polling: null,
  job: null,
};

const el = (id) => document.getElementById(id);

function colorFor(v) {
  if (v == null) return null;
  if (v <= NDVI_STOPS[0][0]) return NDVI_STOPS[0][1];
  for (let i = 1; i < NDVI_STOPS.length; i += 1) {
    const [x1, c1] = NDVI_STOPS[i];
    if (v <= x1) {
      const [x0, c0] = NDVI_STOPS[i - 1];
      return c0;
    }
  }
  return NDVI_STOPS[NDVI_STOPS.length - 1][1];
}

function styleFor(feature) {
  const props = feature.properties;
  const ndvi = state.values[props.field_id];
  if (props.ag_class !== "crop") {
    return { color: "rgba(70,90,70,.35)", weight: 0.4, fillColor: "#c9d3c6", fillOpacity: 0.35 };
  }
  if (ndvi == null) {
    return { color: "rgba(70,90,70,.35)", weight: 0.4, fillColor: "#9aa39a", fillOpacity: 0.45 };
  }
  return { color: "rgba(35,55,35,.45)", weight: 0.5, fillColor: colorFor(ndvi), fillOpacity: 0.78 };
}

function buildMap(fields) {
  const map = L.map("map", { preferCanvas: true, zoomControl: true }).setView([39.49, -86.1], 11);
  L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    { maxZoom: 19, attribution: "Imagery &copy; Esri, Maxar, Earthstar Geographics" }
  ).addTo(map);

  const layer = L.geoJSON(fields, {
    style: styleFor,
    onEachFeature(feature, lyr) {
      state.layers[feature.properties.field_id] = lyr;
      lyr.bindTooltip(() => {
        const v = state.values[feature.properties.field_id];
        const acres = feature.properties.acres.toFixed(1);
        const ndvi = v == null ? "no NDVI" : `NDVI ${v.toFixed(3)}`;
        return `<b>${acres} ac</b> · ${ndvi}`;
      }, { sticky: true, className: "leaflet-tooltip" });
      lyr.on("mouseover", () => lyr.setStyle({ weight: 2, color: "#123d14" }));
      lyr.on("mouseout", () => lyr.setStyle(styleFor(feature)));
      lyr.on("click", () => selectField(feature.properties.field_id));
    },
  }).addTo(map);

  map.fitBounds(layer.getBounds(), { padding: [12, 12] });
  return map;
}

function updateStyles() {
  Object.values(state.layers).forEach((lyr) => lyr.setStyle(styleFor(lyr.feature)));
}

function periodMeta(period) {
  return state.meta.periods[period - 1];
}

function setStatus() {
  const total = state.meta.fields;
  const ids = Object.keys(state.values);
  const count = ids.length;
  const mean = count
    ? ids.reduce((a, k) => a + state.values[k], 0) / count
    : null;

  if (state.status === "not_computed") {
    el("statusline").innerHTML =
      `<b>${state.year}</b> period <b>${state.period}</b> has not been computed yet. ` +
      `Nothing is shown until it is.`;
    el("compute").classList.remove("d-none");
  } else {
    el("statusline").innerHTML = count
      ? `<b>${count.toLocaleString()}</b> fields with NDVI · mean <b>${mean.toFixed(3)}</b> · ` +
        `${total.toLocaleString()} boundaries`
      : `No fields have NDVI for this period.`;
    el("compute").classList.add("d-none");
  }
  el("period-label").textContent =
    `P${String(state.period).padStart(2, "0")} · ${periodMeta(state.period).label}`;
}

async function loadPeriod() {
  const { year, period } = state;
  el("statusline").textContent = "Loading NDVI…";
  const res = await fetch(`/api/ndvi?year=${year}&period=${period}`);
  const data = await res.json();
  state.values = data.values || {};
  state.status = data.status;
  updateStyles();
  setStatus();
  if (state.selected) refreshChartHighlight();
}

async function selectField(fieldId) {
  state.selected = fieldId;
  const res = await fetch(`/api/field/${encodeURIComponent(fieldId)}`);
  if (!res.ok) return;
  const d = await res.json();
  renderDetail(d);

  const lyr = state.layers[fieldId];
  if (lyr) lyr.bringToFront();
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
  const fmt = (v, suffix = "") => (v == null ? "—" : `${v}${suffix}`);
  el("d-soilprops").innerHTML = [
    ["Organic matter", fmt(p.top_om_r, " %")],
    ["CEC", fmt(p.top_cec7_r)],
    ["AWC", fmt(p.top_awc_r)],
    ["pH", fmt(p.top_ph1to1h2o_r)],
    ["Clay", fmt(p.top_claytotal_r, " %")],
    ["Sand", fmt(p.top_sandtotal_r, " %")],
  ].map(([k, v]) => `<div>${k} <b>${v}</b></div>`).join("");

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
    ? "Biweekly field-average NDVI. Empty periods had no usable imagery."
    : "No NDVI is available for this field (outside the crop layer or below one pixel).";
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
  if (job.state === "done") await loadPeriod();
  else if (job.state === "error") {
    el("statusline").textContent = `Computation failed: ${job.error}`;
    el("compute").classList.remove("d-none");
  }
}

async function startCompute() {
  const res = await fetch(`/api/compute-2020/${state.period}`, { method: "POST" });
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
    loadPeriod();
    if (state.chart) refreshChartHighlight();
  });

  const slider = el("period");
  slider.max = state.meta.periods.length;
  slider.addEventListener("input", () => {
    state.period = Number(slider.value);
    loadPeriod();
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
      loadPeriod();
    }, 2200);
  });

  el("compute").addEventListener("click", startCompute);
  el("close-detail").addEventListener("click", () => {
    el("detail").classList.add("d-none");
    state.selected = null;
  });
}

async function init() {
  const meta = await fetch("/api/meta").then((r) => r.json());
  state.meta = meta;
  el("stat-fields").textContent = meta.fields.toLocaleString();
  el("stat-acres").textContent = meta.acres.toLocaleString();
  el("stat-crop").textContent = meta.crop_fields.toLocaleString();

  const fields = await fetch("/api/fields.geojson").then((r) => r.json());
  buildMap(fields);
  bindControls();
  await loadPeriod();

  const job = await fetch("/api/job").then((r) => r.json());
  if (job.state === "running") pollJob();
}

init().catch((err) => {
  el("statusline").textContent = `Failed to load: ${err.message}`;
  console.error(err);
});
