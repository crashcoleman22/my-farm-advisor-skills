(function () {
  "use strict";

  var DATA = JSON.parse(document.getElementById("map-data").textContent);
  var META = DATA.meta || {};
  var FIELDS = (DATA.fields || []).slice();
  var FIELD_BY_ID = {};
  var GEOM_BY_ID = {};
  FIELDS.forEach(function (field) { FIELD_BY_ID[field.field_id] = field; });
  ((DATA.geojson || {}).features || []).forEach(function (feature) {
    GEOM_BY_ID[feature.properties.field_id] = feature.geometry;
  });

  var YEARS = (META.years || []).slice();
  var LAST_YEAR = YEARS.length ? YEARS[YEARS.length - 1] : 2025;
  var STATE_NAMES = {};
  var STATE_RANK = {};
  (META.states || []).forEach(function (s, index) {
    STATE_NAMES[s.code] = s.name;
    STATE_RANK[s.code] = index;
  });
  var GROWER_COLORS = META.grower_colors || {};
  var CROP_COLORS = META.crop_colors || {};
  var CROP_ORDER = META.crop_order || ["Corn", "Soybeans", "Other", "Not classified"];

  var SOIL_PALETTE = [
    "#8c6d46", "#a68a64", "#6b8f71", "#9c6b4f", "#7d8ca3",
    "#b09a6a", "#5e7d5a", "#8f7a8c", "#a3785a", "#6f8f8a"
  ];
  var SOIL_TINT = {};
  (function () {
    var keys = {};
    FIELDS.forEach(function (field) {
      (field.soil || []).forEach(function (unit) {
        if (unit.mukey) { keys[unit.mukey] = true; }
      });
    });
    Object.keys(keys).sort().forEach(function (key, index) {
      SOIL_TINT[key] = SOIL_PALETTE[index % SOIL_PALETTE.length];
    });
  })();

  var NDVI_BINS = [
    { min: -1.0, max: 0.0, color: "#8c510a", label: "<0" },
    { min: 0.0, max: 0.1, color: "#bf812d", label: "0" },
    { min: 0.1, max: 0.2, color: "#dfc27d", label: "" },
    { min: 0.2, max: 0.3, color: "#f6e8c3", label: "0.2" },
    { min: 0.3, max: 0.4, color: "#c7eae5", label: "" },
    { min: 0.4, max: 0.5, color: "#80cdc1", label: "0.4" },
    { min: 0.5, max: 0.6, color: "#35978f", label: "" },
    { min: 0.6, max: 0.7, color: "#01665e", label: "0.6" },
    { min: 0.7, max: 0.8, color: "#1b7837", label: "" },
    { min: 0.8, max: 1.01, color: "#00441b", label: "0.8+" }
  ];

  var STATE = {
    boundaries: true,
    cdl: true,
    soil: false,
    ndvi: false,
    cdlYear: LAST_YEAR,
    selected: null,
    stateFilter: "ALL",
    query: "",
    ndviYear: null,
    ndviDate: null
  };

  var soilData = null;
  var soilLayer = null;
  var soilRequest = null;
  var soilHighlight = null;
  var ndviImage = null;
  var fieldLayers = {};
  var labelsOn = false;

  var hasLeaflet = typeof window.L !== "undefined";

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  function num(value, digits) {
    if (value == null || value === "" || isNaN(Number(value))) { return null; }
    return Number(value).toLocaleString(undefined, {
      minimumFractionDigits: digits == null ? 0 : digits,
      maximumFractionDigits: digits == null ? 0 : digits
    });
  }

  function cropClass(name) {
    if (name === "Corn" || name === "Soybeans") { return name; }
    if (!name) { return "Not classified"; }
    return "Other";
  }

  function fieldCdl(field, year) {
    return (field.cdl || {})[String(year)] || null;
  }

  function fieldColor(field) {
    var cdl = STATE.cdl ? fieldCdl(field, STATE.cdlYear) : null;
    if (cdl) { return CROP_COLORS[cropClass(cdl.crop)] || "#b9c0cb"; }
    return GROWER_COLORS[field.state] || "#2f6fb0";
  }

  function soilColor(mukey) {
    return SOIL_TINT[mukey] || "#9a8b72";
  }

  function setText(id, text) {
    var el = document.getElementById(id);
    if (el) { el.textContent = text; }
  }

  // ------------------------------------------------------------------ map

  var map = null;
  var fieldLayer = null;
  var selectionLayer = null;

  function initMap() {
    if (!hasLeaflet) {
      var wrap = document.getElementById("mapwrap");
      var fatal = document.createElement("div");
      fatal.className = "fatal";
      fatal.innerHTML =
        "<div><h2>Map library could not be loaded</h2>" +
        "<p>The page could not load Leaflet from unpkg.com. Allow unpkg.com and " +
        "server.arcgisonline.com, then reload. The field list and inspector still " +
        "work without the basemap.</p></div>";
      wrap.appendChild(fatal);
      return;
    }

    map = L.map("map", { zoomControl: true, worldCopyJump: false });
    map.attributionControl.setPrefix("");

    map.createPane("ndviPane");
    map.getPane("ndviPane").style.zIndex = 415;
    map.createPane("soilPane");
    map.getPane("soilPane").style.zIndex = 410;
    map.createPane("fieldPane");
    map.getPane("fieldPane").style.zIndex = 420;
    map.createPane("selectionPane");
    map.getPane("selectionPane").style.zIndex = 430;

    L.tileLayer(
      "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
      {
        maxZoom: 19,
        attribution:
          "Imagery &copy; Esri, Maxar, Earthstar Geographics, and the GIS User Community"
      }
    ).addTo(map);

    var labels = L.tileLayer(
      "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
      { maxZoom: 19, attribution: "Labels &copy; Esri" }
    );

    L.control.scale({ imperial: true, metric: true, position: "bottomright" }).addTo(map);

    var fieldRenderer = L.svg({ pane: "fieldPane" });
    fieldLayer = L.geoJSON(DATA.geojson, {
      pane: "fieldPane",
      renderer: fieldRenderer,
      style: function (feature) { return fieldStyle(feature.properties.field_id); },
      onEachFeature: function (feature, layer) {
        var id = feature.properties.field_id;
        var field = FIELD_BY_ID[id];
        if (!field) { return; }
        fieldLayers[id] = layer;
        layer.bindTooltip(
          field.field_name + " \u00b7 " + num(field.acres) + " ac",
          { sticky: true, direction: "top" }
        );
        layer.on("click", function () { selectField(id, false); });
        layer.on("mouseover", function () {
          if (STATE.boundaries) {
            layer.setStyle({ fillOpacity: id === STATE.selected ? 0.6 : 0.48 });
          }
        });
        layer.on("mouseout", function () { layer.setStyle(fieldStyle(id)); });
      }
    }).addTo(map);

    var selectionRenderer = L.svg({ pane: "selectionPane" });
    selectionLayer = L.geoJSON(null, {
      pane: "selectionPane",
      renderer: selectionRenderer,
      interactive: false,
      style: { color: "#111827", weight: 2.6, opacity: 0.9, fill: false, dashArray: "5 4" }
    }).addTo(map);

    var bounds = fieldLayer.getBounds();
    if (bounds.isValid()) { map.fitBounds(bounds, { padding: [28, 28] }); }

    document.getElementById("labels-toggle").addEventListener("click", function () {
      labelsOn = !labelsOn;
      if (labelsOn) { labels.addTo(map); } else { map.removeLayer(labels); }
      this.textContent = labelsOn ? "Labels off" : "Labels on";
    });
  }

  function fieldStyle(fieldId) {
    var field = FIELD_BY_ID[fieldId];
    if (!field) { return {}; }
    var stroke = GROWER_COLORS[field.state] || "#2f6fb0";
    var selected = fieldId === STATE.selected;
    var fill = fieldColor(field);
    // Soil fills and the NDVI raster are thematic layers of their own. A strong
    // CDL wash would hide them, so keep only a light tint while either is on.
    var thematic = STATE.soil || STATE.ndvi;
    var fillOpacity = 0;
    if (STATE.boundaries) {
      if (thematic) {
        fillOpacity = selected ? 0.12 : 0.05;
      } else {
        fillOpacity = selected ? 0.58 : (STATE.cdl ? 0.34 : 0.22);
      }
    }
    return {
      color: stroke,
      weight: selected ? 3.6 : 2,
      opacity: STATE.boundaries ? (selected ? 1 : 0.95) : 0,
      fillColor: fill,
      fillOpacity: fillOpacity,
      lineJoin: "round"
    };
  }

  function applyFieldStyles() {
    Object.keys(fieldLayers).forEach(function (fieldId) {
      fieldLayers[fieldId].setStyle(fieldStyle(fieldId));
    });
    if (STATE.selected && fieldLayers[STATE.selected] && STATE.boundaries) {
      fieldLayers[STATE.selected].bringToFront();
    }
  }

  function updateSelection() {
    if (!selectionLayer) { return; }
    selectionLayer.clearLayers();
    var field = FIELD_BY_ID[STATE.selected];
    var geometry = field ? (field.geometry || GEOM_BY_ID[field.field_id]) : null;
    if (!field || !geometry || !STATE.boundaries) { return; }
    selectionLayer.addData({ type: "Feature", geometry: geometry, properties: {} });
  }

  function setStatus(text, warn) {
    var el = document.getElementById("map-status");
    if (!text) {
      el.classList.add("hidden");
      return;
    }
    el.textContent = text;
    el.classList.toggle("warn", !!warn);
    el.classList.remove("hidden");
  }

  function hideStatus() {
    document.getElementById("map-status").classList.add("hidden");
  }

  // --------------------------------------------------------------- soils

  function ensureSoils() {
    if (soilLayer) { return Promise.resolve(soilLayer); }
    if (soilRequest) { return soilRequest; }
    soilRequest = fetch("soil_map_units.geojson", { cache: "no-cache" })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("HTTP " + response.status);
        }
        return response.json();
      })
      .then(function (geojson) {
        soilData = geojson;
        var renderer = L.svg({ pane: "soilPane" });
        soilLayer = L.geoJSON(geojson, {
          pane: "soilPane",
          renderer: renderer,
          interactive: false,
          style: soilStyle
        });
        applySoilStyles();
        if (STATE.soil) { soilLayer.addTo(map); }
        return soilLayer;
      })
      .catch(function (err) {
        soilRequest = null;
        STATE.soil = false;
        document.getElementById("toggle-soil").checked = false;
        setStatus(
          "SSURGO overlay could not be loaded (" + err.message +
          "). Serve this page over HTTP from its output directory; the inspector " +
          "still lists soil units.",
          true
        );
        return null;
      });
    return soilRequest;
  }

  function soilStyle(feature) {
    var props = feature.properties || {};
    var selected = props.field_id === STATE.selected;
    var dim = STATE.selected && !selected;
    return {
      color: selected ? "#3c2f22" : "#5c4a37",
      weight: selected ? 1.3 : 0.7,
      opacity: selected ? 0.85 : 0.55,
      fillColor: soilColor(props.mukey),
      fillOpacity: dim ? 0.16 : (selected ? 0.62 : 0.4)
    };
  }

  function applySoilStyles() {
    if (soilLayer) { soilLayer.setStyle(soilStyle); }
  }

  function focusSoilUnit(fieldId, mukey) {
    if (!soilData || !map) { return; }
    var matches = (soilData.features || []).filter(function (feature) {
      var props = feature.properties || {};
      return props.field_id === fieldId && String(props.mukey) === String(mukey);
    });
    if (!matches.length) { return; }
    if (soilHighlight) { map.removeLayer(soilHighlight); }
    soilHighlight = L.geoJSON(
      { type: "FeatureCollection", features: matches },
      {
        pane: "selectionPane",
        interactive: false,
        style: { color: "#111827", weight: 2.4, opacity: 1, fill: false, dashArray: "3 3" }
      }
    ).addTo(map);
    map.fitBounds(soilHighlight.getBounds(), { padding: [70, 70], maxZoom: 18 });
    var unit = null;
    var field = FIELD_BY_ID[fieldId];
    if (field) {
      (field.soil || []).forEach(function (row) {
        if (String(row.mukey) === String(mukey)) { unit = row; }
      });
    }
    setStatus(
      "SSURGO unit " + (unit && unit.musym ? unit.musym + " · " : "") +
      (unit && unit.muname ? unit.muname : "mukey " + mukey) +
      (unit ? " · " + num(unit.acres, 1) + " ac (" + num(unit.pct, 1) + "% of field)" : "")
    );
  }

  // ---------------------------------------------------------------- ndvi

  function ndviYears(field) {
    return Object.keys((field.ndvi && field.ndvi.years) || {})
      .map(Number)
      .filter(function (year) { return !isNaN(year); })
      .sort(function (a, b) { return a - b; });
  }

  function peakScene(scenes) {
    var best = null;
    scenes.forEach(function (scene) {
      if (scene.mean == null) { return; }
      if (!best || scene.mean > best.mean) { best = scene; }
    });
    return best || scenes[0] || null;
  }

  function ndviView(field) {
    var years = ndviYears(field);
    if (!years.length) {
      return { years: [], year: null, scenes: [], scene: null };
    }
    var year = years.indexOf(STATE.ndviYear) >= 0 ? STATE.ndviYear : years[years.length - 1];
    var scenes = (field.ndvi.years[String(year)] || []).slice();
    var scene = null;
    scenes.forEach(function (item) {
      if (item.date === STATE.ndviDate) { scene = item; }
    });
    if (!scene) { scene = peakScene(scenes); }
    STATE.ndviYear = year;
    STATE.ndviDate = scene ? scene.date : null;
    return { years: years, year: year, scenes: scenes, scene: scene };
  }

  function cloudPct(scene) {
    if (scene.cloud_cover == null) { return null; }
    var value = Number(scene.cloud_cover);
    if (isNaN(value)) { return null; }
    // STAC eo:cloud_cover is a percentage (0-100), not a 0-1 fraction.
    return (Math.round(value * 10) / 10) + "%";
  }

  function updateNdviOverlay() {
    if (!map) { return; }
    if (ndviImage) {
      map.removeLayer(ndviImage);
      ndviImage = null;
    }
    var field = FIELD_BY_ID[STATE.selected];
    if (!STATE.ndvi) {
      hideStatus();
      showNdviLegend(false);
      return;
    }
    showNdviLegend(true);
    if (!field) {
      setStatus("Select a field to view its observed NDVI pixels.");
      return;
    }
    if (!field.ndvi || !field.ndvi.available) {
      setStatus(
        "No usable cloud-free Sentinel-2 scenes for this field" +
        (field.ndvi && field.ndvi.reason ? " (" + field.ndvi.reason + ")." : "."),
        true
      );
      return;
    }
    var view = ndviView(field);
    if (!view.scene) {
      setStatus("No NDVI scene is available for " + view.year + ".", true);
      return;
    }
    var opacity = Number(document.getElementById("ndvi-opacity").value) / 100;
    ndviImage = L.imageOverlay(view.scene.png, view.scene.bounds, {
      pane: "ndviPane",
      opacity: opacity,
      interactive: false,
      className: "ndvi-image"
    }).addTo(map);
    var valid = Math.round(Number(view.scene.valid_fraction) * 100);
    setStatus(
      "Observed NDVI pixels \u00b7 " + view.scene.date + " \u00b7 10 m \u00b7 " +
      valid + "% of field pixels valid" +
      (view.scene.low_coverage ? " (partial cloud cover)" : ""),
      !!view.scene.low_coverage
    );
  }

  var ndviLegendBuilt = false;

  function showNdviLegend(show) {
    var el = document.getElementById("ndvi-legend");
    if (!show) {
      el.classList.add("hidden");
      return;
    }
    if (!ndviLegendBuilt) {
      var blocks = NDVI_BINS.map(function (bin) {
        return '<span style="background:' + bin.color + '" title="NDVI ' + esc(bin.label || "") + '"></span>';
      }).join("");
      el.innerHTML =
        '<div class="legend-title">Observed NDVI pixels</div>' +
        '<div class="scale">' + blocks + "</div>" +
        '<div class="scale-labels"><span>0</span><span>0.4</span><span>0.8+</span></div>' +
        '<div class="legend-note">Cloud-masked Sentinel-2, 10 m. The chart is a ' +
        "field <b>average</b> of these pixels, not an image.</div>";
      ndviLegendBuilt = true;
    }
    el.classList.remove("hidden");
  }

  // --------------------------------------------------------------- header

  function renderHeader() {
    var totalAcres = FIELDS.reduce(function (sum, field) { return sum + Number(field.acres || 0); }, 0);
    var states = META.states || [];
    var sceneCount = (META.ndvi && META.ndvi.scene_count) || 0;
    document.getElementById("summary").innerHTML =
      '<div class="stat"><b>' + FIELDS.length + "</b><span>Fields</span></div>" +
      '<div class="stat"><b>' + num(totalAcres) + "</b><span>Acres</span></div>" +
      '<div class="stat"><b>' + states.length + "</b><span>States</span></div>" +
      '<div class="stat"><b>' + sceneCount + "</b><span>NDVI scenes</span></div>";

    var stateNames = states.map(function (s) { return s.name; }).join(", ");
    document.getElementById("subtitle").textContent =
      FIELDS.length + " fields \u00b7 " + stateNames + " \u00b7 CDL " +
      YEARS[0] + "\u2013" + LAST_YEAR + " \u00b7 SSURGO soils \u00b7 Sentinel-2 NDVI " +
      YEARS[0] + "\u2013" + LAST_YEAR;
  }

  // ------------------------------------------------------------ field list

  function chipsFor(fields) {
    var counts = {};
    fields.forEach(function (field) { counts[field.state] = (counts[field.state] || 0) + 1; });
    var html = '<button type="button" data-state="ALL" class="' +
      (STATE.stateFilter === "ALL" ? "active" : "") + '">All ' + fields.length + "</button>";
    (META.states || []).forEach(function (state) {
      if (!counts[state.code]) { return; }
      html += '<button type="button" data-state="' + esc(state.code) + '" class="' +
        (STATE.stateFilter === state.code ? "active" : "") + '">' +
        esc(state.code) + " " + counts[state.code] + "</button>";
    });
    return html;
  }

  function renderChips() {
    var el = document.getElementById("state-chips");
    el.innerHTML = chipsFor(FIELDS);
    Array.prototype.forEach.call(el.querySelectorAll("button"), function (button) {
      button.addEventListener("click", function () {
        STATE.stateFilter = button.getAttribute("data-state");
        renderFieldList();
      });
    });
  }

  function visibleFields() {
    var query = STATE.query.trim().toLowerCase();
    return FIELDS.filter(function (field) {
      if (STATE.stateFilter !== "ALL" && field.state !== STATE.stateFilter) { return false; }
      if (!query) { return true; }
      var haystack = [
        field.field_id,
        field.field_name,
        field.farm_name,
        field.grower_name,
        field.county,
        field.state,
        STATE_NAMES[field.state] || ""
      ].join(" ").toLowerCase();
      return haystack.indexOf(query) >= 0;
    }).sort(function (a, b) {
      if (a.state !== b.state) {
        return (STATE_RANK[a.state] == null ? 99 : STATE_RANK[a.state]) -
          (STATE_RANK[b.state] == null ? 99 : STATE_RANK[b.state]);
      }
      if (a.farm_slug !== b.farm_slug) { return a.farm_slug < b.farm_slug ? -1 : 1; }
      return b.acres - a.acres;
    });
  }

  function renderFieldList() {
    renderChips();
    var list = visibleFields();
    var container = document.getElementById("fields");
    container.innerHTML = "";
    setText("field-total", list.length + " shown");

    if (!list.length) {
      container.innerHTML = '<p class="list-empty">No fields match this filter.</p>';
      return;
    }

    var currentGroup = null;
    list.forEach(function (field) {
      var groupKey = field.state + "|" + field.farm_slug;
      if (groupKey !== currentGroup) {
        currentGroup = groupKey;
        var count = list.filter(function (item) {
          return item.state === field.state && item.farm_slug === field.farm_slug;
        }).length;
        var group = document.createElement("div");
        group.className = "farm-group";
        group.innerHTML =
          "<span>" + esc(STATE_NAMES[field.state] || field.state) + " \u00b7 " +
          esc(field.farm_name) + "</span><span>" + count + "</span>";
        container.appendChild(group);
      }

      var cdl = fieldCdl(field, STATE.cdlYear);
      var cropLabel = cdl ? cdl.crop : "no CDL";
      var row = document.createElement("button");
      row.type = "button";
      row.className = "field-row" + (field.field_id === STATE.selected ? " active" : "");
      row.setAttribute("data-field-id", field.field_id);
      row.innerHTML =
        '<span class="swatch" style="background:' + esc(fieldColor(field)) + '"></span>' +
        '<span class="field-text"><span class="field-name">' + esc(field.field_name) +
        '</span><span class="field-meta">' + num(field.acres) + " ac \u00b7 " +
        esc(cropLabel) + " " + STATE.cdlYear + " \u00b7 " +
        esc(field.county || field.state) + ", " + esc(field.state) + "</span></span>";
      row.addEventListener("click", function () { selectField(field.field_id, true); });
      container.appendChild(row);
    });

    highlightRows();
  }

  function highlightRows() {
    var rows = document.querySelectorAll(".field-row");
    for (var i = 0; i < rows.length; i += 1) {
      rows[i].classList.toggle("active", rows[i].getAttribute("data-field-id") === STATE.selected);
    }
  }

  // ---------------------------------------------------------------- legend

  function renderLegend() {
    var el = document.getElementById("legend");
    var parts = [];
    if (STATE.cdl) {
      CROP_ORDER.forEach(function (crop) {
        parts.push('<span class="chip"><span class="swatch" style="background:' +
          esc(CROP_COLORS[crop] || "#b9c0cb") + '"></span>' + esc(crop) + "</span>");
      });
      parts.push('<span class="chip">CDL ' + STATE.cdlYear + " fill</span>");
    } else {
      parts.push('<span class="chip">Fill: grower/state outline colour</span>');
    }
    if (STATE.soil) {
      parts.push('<span class="chip">SSURGO units shaded per map unit</span>');
    }
    el.innerHTML = parts.join("");
  }

  // ------------------------------------------------------------- inspector

  function selectField(fieldId, zoom) {
    if (!FIELD_BY_ID[fieldId]) { return; }
    STATE.selected = fieldId;
    STATE.ndviDate = null;
    if (soilHighlight && map) { map.removeLayer(soilHighlight); soilHighlight = null; }
    if (!STATE.boundaries) {
      STATE.boundaries = true;
      document.getElementById("toggle-boundaries").checked = true;
    }
    applyFieldStyles();
    applySoilStyles();
    updateSelection();
    highlightRows();
    renderInspector();
    if (STATE.ndvi) { updateNdviOverlay(); }
    var panel = document.getElementById("inspector");
    panel.classList.add("open");
    var sidebar = document.getElementById("sidebar");
    sidebar.classList.remove("open");
    document.getElementById("sidebar-toggle").setAttribute("aria-expanded", "false");
    if (zoom && fieldLayers[fieldId] && map) {
      map.fitBounds(fieldLayers[fieldId].getBounds(), { padding: [46, 46], maxZoom: 17 });
    }
    var row = document.querySelector('.field-row[data-field-id="' + fieldId + '"]');
    if (row) { row.scrollIntoView({ block: "nearest" }); }
  }

  function clearSelection() {
    STATE.selected = null;
    STATE.ndviDate = null;
    applyFieldStyles();
    applySoilStyles();
    updateSelection();
    highlightRows();
    renderInspector();
    updateNdviOverlay();
    document.getElementById("inspector").classList.remove("open");
    if (soilHighlight && map) { map.removeLayer(soilHighlight); soilHighlight = null; }
  }

  function statBox(value, label) {
    return '<div class="stat"><b>' + value + "</b><span>" + label + "</span></div>";
  }

  function cdlSection(field) {
    var rows = YEARS.map(function (year) {
      var cdl = fieldCdl(field, year);
      if (!cdl) {
        return '<div class="data-row"><span class="label">' + year +
          '</span><span class="value missing">No CDL table for this year</span></div>';
      }
      var others = (cdl.top || []).slice(1).map(function (item) {
        return "<li>" + esc(item.crop) + " " + num(item.pct, 1) + "%</li>";
      }).join("");
      return '<div class="data-row"><span class="label">' + year +
        '</span><span class="value"><span class="crop-chip"><span class="swatch" style="background:' +
        esc(CROP_COLORS[cropClass(cdl.crop)] || "#b9c0cb") + '"></span>' + esc(cdl.crop) +
        "</span>" + (cdl.mixed ? '<span class="tag">mixed</span>' : "") +
        (others ? '<ul class="sub-list">' + others + "</ul>" : "") +
        '</span><span class="num">' + num(cdl.pct, 1) + "%</span></div>";
    }).join("");
    return (
      '<section class="insp-section"><h3>CDL crop history <span class="hint">dominant crop, pixel share</span></h3>' +
      rows +
      '<p class="note">Pixel percentages from USDA CDL, not planted acres. ' +
      "&ldquo;Mixed&rdquo; marks a field where the top crop is below 60% of classified pixels.</p></section>"
    );
  }

  function weatherSection(field) {
    var rows = YEARS.map(function (year) {
      var row = (field.weather || {})[String(year)];
      if (!row) {
        return '<tr><td>' + year + '</td><td class="na" colspan="6">not available</td></tr>';
      }
      function cell(value, digits) {
        return value == null
          ? '<td class="na">\u2014</td>'
          : "<td>" + num(value, digits == null ? 0 : digits) + "</td>";
      }
      return "<tr><td>" + year + "</td>" + cell(row.precip_mm, 0) + cell(row.gdd, 0) +
        cell(row.heat_days) + cell(row.frost_free_days, 0) + cell(row.dry_spell_days) +
        cell(row.mean_temp_c, 1) + "</tr>";
    }).join("");

    var shared = Number(field.weather_shared_with || 1);
    var note =
      '<p class="note">NASA POWER growing-season summaries for ' + YEARS[0] + "\u2013" +
      LAST_YEAR + ". " +
      (shared > 1
        ? "This field shares one gridded cell with " + (shared - 1) +
          " other field" + (shared - 1 === 1 ? "" : "s") + " in this map, so values are identical by design."
        : "No other field in this map shares this gridded cell.") +
      "</p>";
    var sourceNote =
      META.weather_source === "daily_csv"
        ? '<p class="note">Source: per-field daily weather CSVs, summarized by the pipeline.</p>'
        : '<p class="note">Source: shared EDA field-level seasonal metrics table.</p>';

    return (
      '<section class="insp-section"><h3>Weather <span class="hint">growing season</span></h3>' +
      '<table class="weather-table"><thead><tr><th>Year</th><th>Precip mm</th><th>GDD</th>' +
      "<th>Heat &ge;35&deg;C</th><th>Frost-free</th><th>Dry spell</th><th>Mean &deg;C</th></tr></thead>" +
      "<tbody>" + rows + "</tbody></table>" + note + sourceNote + "</section>"
    );
  }

  function soilSection(field) {
    var units = field.soil || [];
    if (!units.length) {
      return (
        '<section class="insp-section"><h3>SSURGO soils</h3>' +
        '<p class="missing">No SSURGO map-unit polygons were found for this field.</p>' +
        '<p class="note">Map units come from USDA NRCS Soil Data Access and are clipped to the field boundary.</p></section>'
      );
    }
    var rows = units.map(function (unit) {
      var meta = [];
      if (unit.drainage) { meta.push(esc(unit.drainage)); }
      if (unit.slope_pct != null) { meta.push("slope " + num(unit.slope_pct, 1) + "%"); }
      if (unit.hydrologic_group) { meta.push("hydrologic group " + esc(unit.hydrologic_group)); }
      if (unit.aws_050cm != null) { meta.push("AWS " + num(unit.aws_050cm, 1) + " in/50 cm"); }
      var nccpi = unit.nccpi == null
        ? (unit.attributes_available ? "NCCPI not rated" : "USDA attributes unavailable")
        : "NCCPI " + num(unit.nccpi, 2) +
          (unit.nccpi_class ? " (" + esc(unit.nccpi_class) + ")" : "");
      var name = unit.muname || ("mukey " + unit.mukey);
      var symbol = unit.musym || unit.mukey;
      return (
        '<button type="button" class="soil-row" data-mukey="' + esc(unit.mukey) + '" title="Zoom to this map unit">' +
        '<span class="soil-top"><span class="swatch" style="background:' + esc(soilColor(unit.mukey)) + '"></span>' +
        '<span class="sym">' + esc(symbol) + '</span><span class="name">' + esc(name) + "</span>" +
        '<span class="pct">' + num(unit.pct, 1) + "%</span></span>" +
        '<span class="soil-meta">' + num(unit.acres, 1) + " ac \u00b7 " + meta.join(" \u00b7 ") + "</span>" +
        '<span class="soil-meta">' + nccpi + "</span>" +
        '<span class="soil-bar"><span style="width:' + Math.max(1, Math.min(100, Number(unit.pct) || 0)) + '%"></span></span>' +
        "</button>"
      );
    }).join("");

    return (
      '<section class="insp-section"><h3>SSURGO soils <span class="hint">' + units.length +
      " map units</span></h3>" + rows +
      '<p class="note">USDA NRCS SSURGO map units clipped to this boundary; each row zooms to the unit. ' +
      "NCCPI is a component-percentage-weighted average of the USDA rating (Ver 3.0) and is omitted where the component is not rated.</p></section>"
    );
  }

  function dayOfYear(dateText) {
    var parts = String(dateText).split("-").map(Number);
    if (parts.length !== 3 || parts.some(isNaN)) { return null; }
    var stamp = Date.UTC(parts[0], parts[1] - 1, parts[2]);
    return Math.floor((stamp - Date.UTC(parts[0], 0, 1)) / 86400000) + 1;
  }

  function chartSvg(field, year) {
    var width = 368;
    var height = 168;
    var padLeft = 30;
    var padRight = 8;
    var padTop = 10;
    var padBottom = 22;
    var plotW = width - padLeft - padRight;
    var plotH = height - padTop - padBottom;
    var dayStart = 60;
    var dayEnd = 334;

    function xFor(dateText) {
      var day = dayOfYear(dateText);
      if (day == null) { return null; }
      var ratio = (day - dayStart) / (dayEnd - dayStart);
      return padLeft + Math.max(0, Math.min(1, ratio)) * plotW;
    }

    function yFor(value) {
      var ratio = Math.max(0, Math.min(1, Number(value)));
      return padTop + (1 - ratio) * plotH;
    }

    var parts = [];
    parts.push('<svg viewBox="0 0 ' + width + " " + height +
      '" role="img" aria-label="Field-average NDVI by Sentinel-2 scene date">');

    [0, 0.25, 0.5, 0.75, 1].forEach(function (value) {
      var y = yFor(value);
      parts.push('<line class="grid" x1="' + padLeft + '" y1="' + y.toFixed(1) +
        '" x2="' + (width - padRight) + '" y2="' + y.toFixed(1) + '"></line>');
    });
    [["0", 0], ["0.5", 0.5], ["1.0", 1]].forEach(function (tick) {
      parts.push('<text class="axis" x="' + (padLeft - 5) + '" y="' +
        (yFor(tick[1]) + 2.5).toFixed(1) + '" text-anchor="end">' + tick[0] + "</text>");
    });
    [["Mar", 60], ["May", 121], ["Jul", 182], ["Sep", 244], ["Nov", 305]].forEach(function (tick) {
      var ratio = (tick[1] - dayStart) / (dayEnd - dayStart);
      var x = padLeft + ratio * plotW;
      parts.push('<text class="axis" x="' + x.toFixed(1) + '" y="' + (height - 6) +
        '" text-anchor="middle">' + tick[0] + "</text>");
    });

    var years = Object.keys((field.ndvi && field.ndvi.years) || {}).map(Number).sort();
    years.forEach(function (otherYear) {
      var scenes = (field.ndvi.years[String(otherYear)] || []).filter(function (scene) {
        return scene.mean != null && xFor(scene.date) != null;
      });
      if (!scenes.length) { return; }
      if (otherYear === year) { return; }
      var points = scenes.map(function (scene) {
        return xFor(scene.date).toFixed(1) + "," + yFor(scene.mean).toFixed(1);
      }).join(" ");
      parts.push('<polyline class="year-line" points="' + points + '"></polyline>');
      scenes.forEach(function (scene) {
        parts.push('<circle class="year-dot" cx="' + xFor(scene.date).toFixed(1) +
          '" cy="' + yFor(scene.mean).toFixed(1) + '" r="1.6"></circle>');
      });
    });

    var selected = ((field.ndvi.years || {})[String(year)] || []).filter(function (scene) {
      return scene.mean != null && xFor(scene.date) != null;
    });

    if (selected.length > 1) {
      var band = [];
      selected.forEach(function (scene) {
        band.push(xFor(scene.date).toFixed(1) + "," + yFor(scene.p90).toFixed(1));
      });
      selected.slice().reverse().forEach(function (scene) {
        band.push(xFor(scene.date).toFixed(1) + "," + yFor(scene.p10).toFixed(1));
      });
      parts.push('<polygon class="sel-band" points="' + band.join(" ") + '"></polygon>');
    }

    if (selected.length) {
      var linePoints = selected.map(function (scene) {
        return xFor(scene.date).toFixed(1) + "," + yFor(scene.mean).toFixed(1);
      }).join(" ");
      parts.push('<polyline class="sel-line" points="' + linePoints + '"></polyline>');
      selected.forEach(function (scene) {
        var tooltip = scene.date + " \u00b7 mean " + num(scene.mean, 2) +
          " \u00b7 p10\u2013p90 " + num(scene.p10, 2) + "\u2013" + num(scene.p90, 2) +
          " \u00b7 " + Math.round(Number(scene.valid_fraction) * 100) + "% valid";
        parts.push('<circle class="sel-dot" cx="' + xFor(scene.date).toFixed(1) +
          '" cy="' + yFor(scene.mean).toFixed(1) + '" r="2.8"><title>' +
          esc(tooltip) + "</title></circle>");
      });
    }

    parts.push("</svg>");
    return parts.join("");
  }

  function ndviSection(field) {
    var head =
      '<section class="insp-section"><h3>Sentinel-2 NDVI <span class="hint">10 m, SCL cloud mask</span></h3>';

    if (!field.ndvi || !field.ndvi.available) {
      var reason = field.ndvi && field.ndvi.reason ? field.ndvi.reason : "no usable scenes";
      return head + '<p class="missing">No usable NDVI scenes for this field (' +
        esc(reason) + ").</p>" +
        '<p class="note">Scenes where clouds, shadows, snow, or missing pixels leave ' +
        "less than 5% of the field valid are excluded rather than shown as bare soil.</p></section>";
    }

    var view = ndviView(field);
    var scene = view.scene;
    if (!scene) {
      return head + '<p class="missing">No scene available for the selected year.</p></section>';
    }

    var yearOptions = view.years.map(function (year) {
      return '<option value="' + year + '"' + (year === view.year ? " selected" : "") + ">" + year + "</option>";
    }).join("");

    var sceneOptions = view.scenes.map(function (item) {
      var label = item.date + " \u00b7 mean " + num(item.mean, 2) +
        " \u00b7 " + Math.round(Number(item.valid_fraction) * 100) + "% valid";
      return '<option value="' + esc(item.date) + '"' +
        (item.date === scene.date ? " selected" : "") + ">" + esc(label) + "</option>";
    }).join("");

    var stats =
      '<div class="ndvi-stats">' +
      statBox(num(scene.mean, 2), "field mean") +
      statBox(num(scene.p10, 2) + "\u2013" + num(scene.p90, 2), "p10\u2013p90") +
      statBox(Math.round(Number(scene.valid_fraction) * 100) + "%", "pixels valid") +
      statBox(cloudPct(scene) || "\u2014", "scene cloud") +
      "</div>";

    var warn = scene.low_coverage
      ? '<p class="note warn">Low coverage: only ' +
        Math.round(Number(scene.valid_fraction) * 100) +
        "% of the field has valid cloud-free pixels in this scene. The remaining " +
        "pixels are transparent on the map and excluded from the average.</p>"
      : "";

    var legend = view.years.map(function (year) {
      return '<span class="key' + (year === view.year ? " sel" : "") + '"><i></i>' + year + "</span>";
    }).join("");

    var onMap = STATE.ndvi ? "NDVI layer is on." : "NDVI layer is off.";
    var coverage = "Scenes in this field: " + view.years.map(function (year) {
      return year + " (" + (field.ndvi.years[String(year)] || []).length + ")";
    }).join(", ") + ".";

    return head +
      '<div class="ndvi-controls">' +
      '<label>Year<select id="ndvi-year">' + yearOptions + "</select></label>" +
      '<label>Scene<select id="ndvi-date">' + sceneOptions + "</select></label>" +
      "</div>" + stats + warn +
      '<div class="ndvi-chart">' + chartSvg(field, view.year) + "</div>" +
      '<div class="chart-legend">' + legend + "</div>" +
      '<p class="chart-caption">Field <b>average</b> NDVI per scene: line is the mean of ' +
      "valid pixels, band is p10\u2013p90. This is a summary, not an image.</p>" +
      '<div class="callout"><b>Raster vs. average.</b> The map overlay (toggle "Sentinel-2 NDVI") ' +
      "shows observed 10 m pixels for the selected scene. The chart above is a field-average " +
      "summary of those same pixels and is never drawn as imagery.</div>" +
      '<div class="ndvi-actions">' +
      '<button type="button" id="show-ndvi-on-map">' +
      (STATE.ndvi ? "Refresh overlay" : "Show on map") + "</button>" +
      '<span class="on-map">' + onMap + " Scene " + esc(scene.date) + " \u00b7 10 m.</span>" +
      "</div>" +
      '<p class="note">' + esc(coverage) + " " +
      (field.ndvi.excluded
        ? field.ndvi.excluded + " scene(s) excluded for cloud cover or missing pixels. "
        : "") +
      "Cloud-mask classes: " + ((META.ndvi && META.ndvi.cloud_classes) || []).join(", ") +
      ". Scene cloud is the provider's tile-level STAC value; the valid-pixel share above " +
      "is measured inside this field.</p>" +
      "</section>";
  }

  function monthCoverage(field) {
    var names = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    return Object.keys((field.ndvi && field.ndvi.years) || {}).sort().map(function (year) {
      var scenes = field.ndvi.years[year] || [];
      var months = {};
      scenes.forEach(function (scene) {
        var month = Number(String(scene.date).slice(5, 7));
        if (month) { months[month] = true; }
      });
      var missing = [];
      for (var month = 3; month <= 11; month += 1) {
        if (!months[month]) { missing.push(names[month]); }
      }
      return year + ": " + scenes.length + " usable scene" + (scenes.length === 1 ? "" : "s") +
        (missing.length
          ? ", no usable scene in " + missing.join(", ")
          : ", every month Mar\u2013Nov covered");
    });
  }

  function aboutSection(field) {
    var notes = (META.notes || []).map(function (note) {
      return "<li>" + esc(note) + "</li>";
    }).join("");

    var dates = [];
    Object.keys((field.ndvi && field.ndvi.years) || {}).forEach(function (year) {
      (field.ndvi.years[year] || []).forEach(function (scene) { dates.push(scene.date); });
    });
    dates.sort();
    var range = dates.length ? dates[0] + " to " + dates[dates.length - 1] : "none";

    var soilStatus = META.soil_attribute_status || "unknown";
    var soilLabel = soilStatus === "usda-sda"
      ? "USDA Soil Data Access"
      : (soilStatus === "cache" ? "USDA Soil Data Access (cached)" : soilStatus);

    var coverage = monthCoverage(field).map(function (line) {
      return "<li>" + esc(line) + "</li>";
    }).join("");

    return (
      '<details class="about"><summary>Data sources &amp; limitations</summary><ul>' +
      notes +
      "<li>NDVI acquisition dates in this field: " + esc(range) +
      ". Excluded scenes: " + (field.ndvi ? field.ndvi.excluded || 0 : 0) + ".</li>" +
      coverage +
      "<li>Soil attributes: " + esc(soilLabel) + ".</li>" +
      "<li>Weather source: " +
      esc(META.weather_source === "daily_csv" ? "per-field daily CSVs" : "shared EDA seasonal table") +
      ".</li>" +
      "</ul>" +
      '<p class="stamp">Generated ' + esc(META.generated_at || "") +
      ". Basemap and labels: Esri, online tiles. Field boundaries: pipeline polygons.</p>" +
      "</details>"
    );
  }

  function inspectorHtml(field) {
    var totalSoil = (field.soil || []).length;
    var ndviSceneCount = 0;
    Object.keys((field.ndvi && field.ndvi.years) || {}).forEach(function (year) {
      ndviSceneCount += (field.ndvi.years[year] || []).length;
    });

    var html =
      '<div class="insp-head"><h2>' + esc(field.field_name) + "</h2>" +
      '<p class="insp-sub">' + esc(field.grower_name) + " \u00b7 " + esc(field.farm_name) + "</p>" +
      '<button type="button" id="inspector-close" aria-label="Close details">&times;</button></div>' +
      '<div class="stat-row">' +
      statBox(num(field.acres), "acres") +
      statBox(esc(field.state), esc(field.state_name || "")) +
      statBox(totalSoil, "soil units") +
      "</div>" +
      '<p class="meta-line">' +
      "<b>Field ID</b> " + esc(field.field_id) +
      " &nbsp;\u00b7&nbsp; <b>County</b> " + esc(field.county || "unknown") +
      " &nbsp;\u00b7&nbsp; <b>NDVI scenes</b> " + ndviSceneCount +
      " &nbsp;\u00b7&nbsp; <b>Boundary</b> " + esc(field.source || "unknown") +
      "</p>";

    html += cdlSection(field);
    html += weatherSection(field);
    html += soilSection(field);
    html += ndviSection(field);
    html += aboutSection(field);
    return html;
  }

  function renderInspector() {
    var empty = document.getElementById("inspector-empty");
    var body = document.getElementById("inspector-body");
    var field = FIELD_BY_ID[STATE.selected];
    if (!field) {
      empty.classList.remove("hidden");
      body.classList.add("hidden");
      body.innerHTML = "";
      return;
    }
    empty.classList.add("hidden");
    body.classList.remove("hidden");
    var panel = document.getElementById("inspector");
    var scroll = panel.scrollTop;
    body.innerHTML = inspectorHtml(field);
    panel.scrollTop = scroll;
    bindInspector(field);
  }

  function bindInspector(field) {
    var close = document.getElementById("inspector-close");
    if (close) { close.addEventListener("click", clearSelection); }

    var yearSelect = document.getElementById("ndvi-year");
    if (yearSelect) {
      yearSelect.addEventListener("change", function () {
        STATE.ndviYear = Number(yearSelect.value);
        STATE.ndviDate = null;
        renderInspector();
        if (STATE.ndvi) { updateNdviOverlay(); }
      });
    }

    var dateSelect = document.getElementById("ndvi-date");
    if (dateSelect) {
      dateSelect.addEventListener("change", function () {
        STATE.ndviDate = dateSelect.value;
        renderInspector();
        if (STATE.ndvi) { updateNdviOverlay(); }
      });
    }

    var showButton = document.getElementById("show-ndvi-on-map");
    if (showButton) {
      showButton.addEventListener("click", function () {
        STATE.ndvi = true;
        document.getElementById("toggle-ndvi").checked = true;
        applyFieldStyles();
        updateNdviOverlay();
        renderInspector();
      });
    }

    Array.prototype.forEach.call(
      document.querySelectorAll(".soil-row"),
      function (row) {
        row.addEventListener("click", function () {
          focusSoilUnit(field.field_id, row.getAttribute("data-mukey"));
        });
      }
    );
  }

  // -------------------------------------------------------------- controls

  function bindControls() {
    var cdlSelect = document.getElementById("cdl-year");
    cdlSelect.innerHTML = YEARS.slice().reverse().map(function (year) {
      return '<option value="' + year + '"' + (year === STATE.cdlYear ? " selected" : "") + ">" + year + "</option>";
    }).join("");
    cdlSelect.addEventListener("change", function () {
      STATE.cdlYear = Number(cdlSelect.value);
      applyFieldStyles();
      renderLegend();
      renderFieldList();
    });

    document.getElementById("toggle-boundaries").addEventListener("change", function (event) {
      STATE.boundaries = event.target.checked;
      applyFieldStyles();
      updateSelection();
      renderLegend();
    });

    document.getElementById("toggle-cdl").addEventListener("change", function (event) {
      STATE.cdl = event.target.checked;
      applyFieldStyles();
      renderLegend();
      renderFieldList();
    });

    document.getElementById("toggle-soil").addEventListener("change", function (event) {
      STATE.soil = event.target.checked;
      applyFieldStyles();
      renderLegend();
      if (STATE.soil) {
        ensureSoils().then(function (layer) {
          if (!layer) { return; }
          applySoilStyles();
          layer.addTo(map);
          setStatus((soilData.features || []).length + " SSURGO map units from USDA NRCS; click a soil row to zoom.");
        });
      } else if (soilLayer && map) {
        map.removeLayer(soilLayer);
        if (soilHighlight) { map.removeLayer(soilHighlight); soilHighlight = null; }
        if (STATE.ndvi) { updateNdviOverlay(); } else { hideStatus(); }
      }
    });

    document.getElementById("toggle-ndvi").addEventListener("change", function (event) {
      STATE.ndvi = event.target.checked;
      applyFieldStyles();
      updateNdviOverlay();
      if (STATE.selected) { renderInspector(); }
    });

    document.getElementById("ndvi-opacity").addEventListener("input", function () {
      if (ndviImage) { ndviImage.setOpacity(Number(this.value) / 100); }
    });

    document.getElementById("search").addEventListener("input", function () {
      STATE.query = this.value;
      renderFieldList();
    });

    document.getElementById("reset-view").addEventListener("click", function () {
      clearSelection();
      if (map && fieldLayer) {
        map.fitBounds(fieldLayer.getBounds(), { padding: [28, 28] });
      }
      hideStatus();
    });

    document.getElementById("sidebar-toggle").addEventListener("click", function () {
      var sidebar = document.getElementById("sidebar");
      var open = sidebar.classList.toggle("open");
      this.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }

  // ----------------------------------------------------------------- boot

  function boot() {
    renderHeader();
    renderLegend();
    renderFieldList();
    bindControls();
    initMap();

    var loading = document.getElementById("loading");
    loading.classList.add("done");
    setTimeout(function () { loading.classList.add("hidden"); }, 250);
  }

  boot();
})();
