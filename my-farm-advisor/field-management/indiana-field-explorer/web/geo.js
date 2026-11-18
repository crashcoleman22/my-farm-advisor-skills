/* Pure geometry helpers shared by the map and the Node selection test. */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FieldGeo = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";

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

  function bboxOf(geom) {
    let x0 = Infinity;
    let y0 = Infinity;
    let x1 = -Infinity;
    let y1 = -Infinity;
    const walk = (coords) => {
      if (typeof coords[0] === "number") {
        x0 = Math.min(x0, coords[0]);
        x1 = Math.max(x1, coords[0]);
        y0 = Math.min(y0, coords[1]);
        y1 = Math.max(y1, coords[1]);
        return;
      }
      coords.forEach(walk);
    };
    walk(geom.coordinates);
    return [x0, y0, x1, y1];
  }

  function contains(feature, x, y) {
    const geom = feature.geometry;
    if (geom.type === "Polygon") return pointInPolygon(x, y, geom.coordinates);
    if (geom.type === "MultiPolygon") {
      for (const poly of geom.coordinates) {
        if (pointInPolygon(x, y, poly)) return true;
      }
    }
    return false;
  }

  /**
   * Return the smallest containing field feature, or null.
   *
   * Smallest-area wins so a sliver overlap can never steal a click from the
   * field a user is actually pointing at.
   */
  function fieldAt(features, lng, lat) {
    let best = null;
    let bestAcres = Infinity;
    for (const feature of features) {
      if (!feature._bbox) feature._bbox = bboxOf(feature.geometry);
      const bb = feature._bbox;
      if (lng < bb[0] || lng > bb[2] || lat < bb[1] || lat > bb[3]) continue;
      if (!contains(feature, lng, lat)) continue;
      const acres = Number(feature.properties && feature.properties.acres);
      const key = Number.isFinite(acres) ? acres : Infinity;
      if (best === null || key < bestAcres) {
        best = feature;
        bestAcres = key;
      }
    }
    return best;
  }

  return { pointInRing, pointInPolygon, bboxOf, contains, fieldAt };
});
