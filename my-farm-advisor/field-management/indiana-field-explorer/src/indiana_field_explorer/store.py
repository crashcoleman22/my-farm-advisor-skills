"""Data access, biweekly aggregation, and on-demand 2020 processing."""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

from . import raster, soils_layer
from .config import YEARS, periods_for, settings

SOIL_FIELDS = (
    "dominant_mukey", "dominant_musym", "dominant_muname", "dominant_comppct",
    "n_map_units", "top_om_r", "top_cec7_r", "top_awc_r", "top_ph1to1h2o_r",
    "top_claytotal_r", "top_sandtotal_r", "top_silttotal_r", "top_kwfact",
    "aws025wta", "aws050wta", "aws0100wta", "slopegradwta", "wtdepannmin",
)

MIN_COVERAGE = 50.0  # same quality gate as the discovery pipeline peaks/trends


class Store:
    def __init__(self) -> None:
        self.cfg = settings()
        self.farm = self.cfg.farm_dir
        if not self.farm.exists():
            raise FileNotFoundError(f"farm output not found: {self.farm}")

        self.fields = gpd.read_parquet(self.farm / "boundary/field_boundaries.parquet")
        self.fields["field_id"] = self.fields["field_id"].astype(str)
        self._by_id = self.fields.set_index("field_id", drop=False)

        self.soil = pd.read_csv(self.farm / "manifests/field_soil_summary.csv",
                                dtype={"field_id": str})
        self.soil = self.soil.set_index("field_id", drop=False)
        self.units = pd.read_csv(self.farm / "manifests/field_soil_map_units.csv",
                                 dtype={"field_id": str})
        self.crop = pd.read_csv(self.farm / "manifests/field_crop_history.csv",
                                dtype={"field_id": str})

        self.daily = pd.read_parquet(self.farm / "derived/ndvi/field_ndvi_daily.parquet")
        self.daily["field_id"] = self.daily["field_id"].astype(str)
        self.daily["date"] = pd.to_datetime(self.daily["date"])
        self.daily["period"] = (
            (self.daily["date"]
             - pd.to_datetime(self.daily["year"].astype(str) + "-04-01")).dt.days // 14 + 1
        ).clip(upper=15)

        self._period_cache: dict[int, pd.DataFrame] = {}
        self._cache_2020: pd.DataFrame | None = None
        self._stamp_2020: float = 0.0
        self._lock = threading.Lock()
        self._soil_lock = threading.Lock()
        self._field_raster_lock = threading.Lock()
        self._field_soil_lock = threading.Lock()
        self._soil_frame_lock = threading.Lock()
        self._soil_gdf: gpd.GeoDataFrame | None = None
        self._job: dict | None = None

    # ------------------------------------------------------------------
    # Static payloads
    # ------------------------------------------------------------------
    def county_meta(self) -> dict:
        stats = json.loads((self.farm / "manifests/county_field_stats.json").read_text())
        return {
            "county": "Johnson County, Indiana",
            "fips": "18081",
            "fields": int(len(self.fields)),
            "crop_fields": int((self.fields["ag_class"] == "crop").sum()),
            "acres": round(float(self.fields["area_acres_clipped"].sum()), 1),
            "years": list(YEARS),
            "season": "April 1 - October 31",
            "periods": periods_for(2026),
            "ndvi_source": "Sentinel-2 L2A, 10 m, SCL-masked",
            "county_stats": stats,
        }

    def fields_geojson(self) -> dict:
        """Simplified field polygons for the map (cached next to runtime state)."""
        cache = self.cfg.cache_dir / "fields_simplified.geojson"
        src_mtime = (self.farm / "boundary/field_boundaries.parquet").stat().st_mtime
        if cache.exists() and cache.stat().st_mtime >= src_mtime:
            return json.loads(cache.read_text())

        geo = self.fields[["field_id", "ag_class", "crop_name", "area_acres_clipped",
                           "admin_county_mismatch", "geometry"]].to_crs(32616)
        geo["geometry"] = geo.geometry.simplify(2.0, preserve_topology=True)
        geo = geo.to_crs(4326)

        features = []
        for row in geo.itertuples(index=False):
            features.append({
                "type": "Feature",
                "id": row.field_id,
                "properties": {
                    "field_id": row.field_id,
                    "ag_class": row.ag_class,
                    "crop_name": "" if pd.isna(row.crop_name) else str(row.crop_name),
                    "acres": round(float(row.area_acres_clipped), 2),
                    "admin_mismatch": bool(row.admin_county_mismatch),
                },
                "geometry": row.geometry.__geo_interface__,
            })
        payload = {"type": "FeatureCollection", "features": features}
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".tmp.geojson")
        tmp.write_text(json.dumps(payload, separators=(",", ":"), allow_nan=False))
        tmp.replace(cache)
        return payload

    # ------------------------------------------------------------------
    # Biweekly NDVI
    # ------------------------------------------------------------------
    def _aggregate_daily(self, year: int) -> pd.DataFrame:
        if year in self._period_cache:
            return self._period_cache[year]

        df = self.daily[(self.daily["year"] == year)
                        & (self.daily["coverage_pct"] >= MIN_COVERAGE)
                        & (self.daily["period"].between(1, 15))].copy()
        if df.empty:
            out = pd.DataFrame(columns=["field_id", "period", "ndvi", "n_dates",
                                        "coverage_mean", "first_date", "last_date"])
        else:
            df["w"] = df["valid_px"].astype("float64")
            df["wn"] = df["ndvi_mean"].astype("float64") * df["w"]
            grouped = df.groupby(["field_id", "period"], sort=False).agg(
                wn=("wn", "sum"), w=("w", "sum"), n_dates=("ndvi_mean", "size"),
                coverage_mean=("coverage_pct", "mean"),
                first_date=("date", "min"), last_date=("date", "max"),
            )
            grouped["ndvi"] = (grouped["wn"] / grouped["w"]).round(4)
            out = grouped[["ndvi", "n_dates", "coverage_mean",
                           "first_date", "last_date"]].reset_index()
            out["first_date"] = out["first_date"].dt.strftime("%Y-%m-%d")
            out["last_date"] = out["last_date"].dt.strftime("%Y-%m-%d")
            out["coverage_mean"] = out["coverage_mean"].round(1)

        self._period_cache[year] = out
        return out

    def _cached_2020(self, period: int) -> pd.DataFrame | None:
        path = self.cfg.periods_cache / "2020" / f"P{period:02d}.parquet"
        if not path.exists():
            return None
        frame = pd.read_parquet(path)
        frame["period"] = period
        frame["field_id"] = frame["field_id"].astype(str)
        return frame

    def period_values(self, year: int, period: int) -> dict:
        """Map field_id -> NDVI for one biweekly period, or a not-computed status."""
        if year == 2020:
            df = self._cached_2020(period)
            if df is None:
                return {"year": year, "period": period, "status": "not_computed",
                        "values": {}, "n_fields": 0}
            df = df.dropna(subset=["ndvi"])
            values = {str(r.field_id): float(r.ndvi) for r in df.itertuples()}
            return {"year": year, "period": period, "status": "ready",
                    "values": values, "n_fields": len(values)}

        df = self._aggregate_daily(year)
        subset = df[df["period"] == period].dropna(subset=["ndvi"])
        values = {str(r.field_id): float(r.ndvi) for r in subset.itertuples()}
        return {"year": year, "period": period, "status": "ready",
                "values": values, "n_fields": len(values)}

    def _all_2020(self) -> pd.DataFrame:
        files = sorted(self.cfg.periods_cache.glob("2020/P*.parquet"))
        if not files:
            return pd.DataFrame(columns=["field_id", "period", "ndvi", "coverage_mean"])
        stamp = max(f.stat().st_mtime for f in files)
        if self._cache_2020 is not None and self._stamp_2020 == stamp:
            return self._cache_2020
        frame = pd.concat(
            [pd.read_parquet(f).assign(period=int(f.stem[1:])) for f in files],
            ignore_index=True,
        )
        if not frame.empty:
            frame["field_id"] = frame["field_id"].astype(str)
        self._cache_2020 = frame
        self._stamp_2020 = stamp
        return frame

    def field_series(self, field_id: str) -> dict:
        out: dict[int, list[float | None]] = {}
        coverage: dict[int, list[float | None]] = {}
        for year in YEARS:
            if year == 2020:
                frame = self._all_2020()
                row = frame[frame["field_id"] == field_id] if not frame.empty else frame
                series = {int(p): float(v) for p, v in zip(row.get("period", []), row.get("ndvi", []))
                          if pd.notna(v)} if not frame.empty else {}
                cov = {int(p): float(v) for p, v in
                       zip(row.get("period", []), row.get("coverage_mean", []))
                       if pd.notna(v)} if not frame.empty else {}
            else:
                df = self._aggregate_daily(year)
                row = df[df["field_id"] == field_id]
                series = {int(p): float(v) for p, v in zip(row["period"], row["ndvi"])
                          if pd.notna(v)}
                cov = {int(p): float(v) for p, v in zip(row["period"], row["coverage_mean"])
                       if pd.notna(v)}
            out[year] = [round(series[p], 4) if p in series else None for p in range(1, 16)]
            coverage[year] = [round(cov[p], 1) if p in cov else None for p in range(1, 16)]
        return {"values": out, "coverage": coverage}

    def field_detail(self, field_id: str) -> dict | None:
        if field_id not in self._by_id.index:
            return None
        f = self._by_id.loc[field_id]
        if isinstance(f, pd.DataFrame):
            f = f.iloc[0]

        soil = self.soil.loc[field_id]
        if isinstance(soil, pd.DataFrame):
            soil = soil.iloc[0]

        def clean(value):
            if value is None:
                return None
            try:
                if pd.isna(value):
                    return None
            except (TypeError, ValueError):
                pass
            if isinstance(value, (np.integer, np.floating)):
                return float(value)
            return value

        props = {}
        for key in SOIL_FIELDS:
            value = clean(soil.get(key))
            if isinstance(value, float):
                value = round(value, 3)
            props[key] = value

        units = self.units[self.units["field_id"] == field_id]
        unit_rows = [{
            "mukey": str(r.mukey), "musym": r.musym, "muname": r.muname,
            "comppct": None if pd.isna(r.comp_pct_total) else round(float(r.comp_pct_total), 1),
        } for r in units.itertuples()]

        crop = self.crop[self.crop["field_id"] == field_id].sort_values("year")
        crop_rows = [{
            "year": int(r.year), "crop_name": r.crop_name,
            "crop_pct": None if pd.isna(r.crop_pct) else round(float(r.crop_pct), 1),
            "ag_class": r.ag_class,
        } for r in crop.itertuples()]

        return {
            "field_id": field_id,
            "acres": round(float(f["area_acres_clipped"]), 2),
            "acres_original": round(float(f["area_acres_original"]), 2),
            "ag_class": f["ag_class"],
            "crop_name": "" if pd.isna(f["crop_name"]) else str(f["crop_name"]),
            "csb_admin_county": f["csb_admin_county"],
            "admin_mismatch": bool(f["admin_county_mismatch"]),
            "edge_clipped": bool(f["edge_flag"]),
            "soils": {"properties": props, "n_units": int(soil["n_map_units"]),
                      "dominant": soil["dominant_muname"], "units": unit_rows},
            "crop_history": crop_rows,
            "ndvi": self.field_series(field_id),
        }

    # ------------------------------------------------------------------
    # On-demand 2020 processing
    # ------------------------------------------------------------------
    def job(self) -> dict | None:
        with self._lock:
            return dict(self._job) if self._job else None

    def start_2020(self, period: int) -> dict:
        with self._lock:
            if self._job and self._job["state"] == "running":
                return {"ok": False, "error": "a job is already running",
                        "job": dict(self._job)}
            self._job = {"state": "running", "period": period, "done": 0,
                         "total": 0, "message": "starting", "error": None}
        threading.Thread(target=self._run_2020, args=(period,), daemon=True).start()
        return {"ok": True, "job": self.job()}

    def _set_job(self, **kwargs) -> None:
        with self._lock:
            if self._job is None:
                self._job = {}
            self._job.update(kwargs)

    def _run_2020(self, period: int) -> None:
        try:
            self._compute_2020(period)
            self._set_job(state="done", message=f"period {period} cached")
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            self._set_job(state="error", error=str(exc), message="failed")

    def _compute_2020(self, period: int) -> None:
        cache_path = self.cfg.periods_cache / "2020" / f"P{period:02d}.parquet"
        if cache_path.exists():
            return
        cache_path.parent.mkdir(parents=True, exist_ok=True)

        sys.path.insert(0, str(self.cfg.discovery_src))
        from indiana_field_discovery import ndvi as discovery_ndvi

        windows = periods_for(2020)
        window = windows[period - 1]

        crop = self.fields[self.fields["ag_class"] == "crop"].copy()
        bounds_utm = tuple(float(v) for v in crop.to_crs(32616).total_bounds)
        bounds_wgs = tuple(float(v) for v in crop.to_crs(4326).total_bounds)

        self._set_job(message="searching STAC", done=0, total=1)
        items = discovery_ndvi.search_items(bounds_wgs, window["start"], window["end"])
        items = discovery_ndvi.dedupe_by_date(items)
        if not items:
            self._write(pd.DataFrame(columns=["field_id", "ndvi", "n_dates", "coverage_mean",
                                              "first_date", "last_date"]), cache_path)
            return

        self._set_job(message="building field index", total=len(items) + 1)
        raster_path = self.cfg.field_index_dir / "field_index.tif"
        if not raster_path.exists() or not (raster_path.parent / "field_index.csv").exists():
            discovery_ndvi.build_field_raster(None, crop, items[0], bounds_utm, raster_path)

        frames = []
        for index, item in enumerate(items, start=1):
            self._set_job(message=f"reading {item['properties']['datetime'][:10]}",
                          done=index, total=len(items) + 1)
            frame = discovery_ndvi.process_date(item, raster_path, bounds_utm)
            if not frame.empty:
                frame = frame[frame["coverage_pct"] >= MIN_COVERAGE]
                if not frame.empty:
                    frames.append(frame)

        self._set_job(message="aggregating", done=len(items), total=len(items) + 1)
        if not frames:
            self._write(pd.DataFrame(columns=["field_id", "ndvi", "n_dates", "coverage_mean",
                                              "first_date", "last_date"]), cache_path)
            return

        daily = pd.concat(frames, ignore_index=True)
        daily["w"] = daily["valid_px"].astype("float64")
        daily["wn"] = daily["ndvi_mean"].astype("float64") * daily["w"]
        grouped = daily.groupby("field_id", sort=False).agg(
            wn=("wn", "sum"), w=("w", "sum"), n_dates=("ndvi_mean", "size"),
            coverage_mean=("coverage_pct", "mean"),
            first_date=("date", "min"), last_date=("date", "max"),
        )
        grouped["ndvi"] = (grouped["wn"] / grouped["w"]).round(4)
        out = grouped[["ndvi", "n_dates", "coverage_mean",
                       "first_date", "last_date"]].reset_index()
        out["coverage_mean"] = out["coverage_mean"].round(1)
        self._write(out, cache_path)

    @staticmethod
    def _write(frame: pd.DataFrame, path: Path) -> None:
        tmp = path.with_name(f"{path.stem}.{os.getpid()}.{threading.get_ident()}.tmp.parquet")
        frame.to_parquet(tmp, index=False)
        tmp.replace(path)

    # ------------------------------------------------------------------
    # Per-pixel NDVI raster composites
    # ------------------------------------------------------------------
    def raster_tif(self, year: int, period: int) -> Path:
        return self.cfg.raster_cache / str(year) / f"P{period:02d}.tif"

    def raster_png(self, year: int, period: int) -> Path:
        return self.cfg.raster_cache / str(year) / f"P{period:02d}.png"

    def raster_json(self, year: int, period: int) -> Path:
        return self.cfg.raster_cache / str(year) / f"P{period:02d}.json"

    def raster_info(self, year: int, period: int) -> dict:
        meta_path = self.raster_json(year, period)
        if not meta_path.exists() or not self.raster_tif(year, period).exists():
            return {"year": year, "period": period, "status": "not_computed"}
        payload = raster.load_json(meta_path)
        payload["status"] = "ready" if payload.get("stats", {}).get("pixels") else "empty"
        payload["png_url"] = f"/api/ndvi-raster.png?year={year}&period={period}"
        return payload

    def cached_rasters(self) -> list[dict]:
        out = []
        for meta_path in sorted(self.cfg.raster_cache.glob("*/P*.json")):
            try:
                payload = raster.load_json(meta_path)
            except (OSError, ValueError):
                continue
            out.append({"year": payload.get("year"), "period": payload.get("period"),
                        "label": payload.get("label")})
        return out

    def start_raster(self, year: int, period: int) -> dict:
        with self._lock:
            if self._job and self._job["state"] == "running":
                return {"ok": False, "error": "a job is already running",
                        "job": dict(self._job)}
            if self.raster_json(year, period).exists():
                self._job = {"state": "done", "kind": "raster", "year": year,
                             "period": period, "done": 1, "total": 1,
                             "message": "already cached", "error": None}
                return {"ok": True, "job": dict(self._job)}
            self._job = {"state": "running", "kind": "raster", "year": year,
                         "period": period, "done": 0, "total": 1,
                         "message": "starting", "error": None}
        threading.Thread(target=self._run_raster, args=(year, period), daemon=True).start()
        return {"ok": True, "job": self.job()}

    def _run_raster(self, year: int, period: int) -> None:
        try:
            self._build_raster(year, period)
            self._set_job(state="done", message=f"{year} P{period:02d} cached")
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            self._set_job(state="error", error=str(exc), message="failed")

    def _build_raster(self, year: int, period: int) -> None:
        tif_path = self.raster_tif(year, period)
        if tif_path.exists() and self.raster_json(year, period).exists():
            return

        sys.path.insert(0, str(self.cfg.discovery_src))
        from indiana_field_discovery import ndvi as discovery_ndvi

        grid_path = self.cfg.field_index_dir / "field_index.tif"
        if not grid_path.exists():
            raise FileNotFoundError(
                f"missing {grid_path}; run one 2020 aggregate period first")
        grid = raster.grid_from(grid_path)

        window = periods_for(year)[period - 1]
        crop = self.fields[self.fields["ag_class"] == "crop"]
        wgs_bounds = tuple(float(v) for v in crop.to_crs(4326).total_bounds)

        self._set_job(message="searching STAC", done=0, total=1)
        items = discovery_ndvi.search_items(wgs_bounds, window["start"], window["end"])
        items = discovery_ndvi.dedupe_by_date(items)
        if not items:
            raise RuntimeError(f"no Sentinel-2 items for {window['start']}..{window['end']}")

        self._set_job(total=len(items))

        def progress(message: str, done: int, total: int) -> None:
            self._set_job(message=message, done=done, total=max(total, 1))

        ndvi, days = raster.build_composite(items, grid, discovery_ndvi.SCL_MASK,
                                            progress=progress)
        stats = raster.raster_stats(ndvi)

        self._set_job(message="writing raster", done=len(items), total=len(items))
        raster.write_geotiff(tif_path, ndvi, grid)
        raster.write_gray_alpha_png(self.raster_png(year, period), ndvi)

        from rasterio.warp import transform_bounds
        left, bottom, right, top = transform_bounds(
            grid["crs"], "EPSG:4326", *grid["bounds"])
        meta = {
            "year": year,
            "period": period,
            "start": window["start"],
            "end": window["end"],
            "label": window["label"],
            "width": grid["width"],
            "height": grid["height"],
            "resolution_m": 10,
            "crs": grid["crs"].to_string(),
            "bounds": [[bottom, left], [top, right]],
            "bounds_utm": list(grid["bounds"]),
            "dates": days,
            "n_dates": len(days),
            "method": "mean of clear Sentinel-2 observations per 10 m pixel",
            "encoding": {"min": raster.NDVI_MIN, "max": raster.NDVI_MAX,
                         "scheme": "gray8 alpha8"},
            "stats": stats,
        }
        meta_path = self.raster_json(year, period)
        tmp = meta_path.with_name(f"{meta_path.stem}.tmp.json")
        tmp.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        tmp.replace(meta_path)

    def field_raster_stats(self, field_id: str, year: int, period: int) -> dict | None:
        if field_id not in self._by_id.index:
            return None
        tif_path = self.raster_tif(year, period)
        if not tif_path.exists():
            return {"status": "not_computed", "field_id": field_id,
                    "year": year, "period": period}
        row = self._by_id.loc[field_id]
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        geometry = self.fields[self.fields["field_id"] == field_id].to_crs(32616) \
            .geometry.iloc[0]
        stats = raster.field_stats(tif_path, geometry)
        if stats is None:
            return {"status": "no_pixels", "field_id": field_id,
                    "year": year, "period": period}
        return {"status": "ready", "field_id": field_id, "year": year,
                "period": period, **stats}

    # ------------------------------------------------------------------
    # Per-field NDVI crops (Web Mercator, native ground resolution)
    # ------------------------------------------------------------------
    def field_ndvi_paths(self, field_id: str, year: int, period: int) -> tuple[Path, Path]:
        base = self.cfg.field_raster_cache / str(year) / f"P{period:02d}"
        return base / f"{field_id}.png", base / f"{field_id}.json"

    def _field_ndvi_cached(self, png: Path, meta_path: Path, tif: Path) -> bool:
        if not meta_path.exists() or meta_path.stat().st_mtime < tif.stat().st_mtime:
            return False
        try:
            meta = raster.load_json(meta_path)
        except (OSError, ValueError):
            return False
        return meta.get("status") != "ready" or png.exists()

    def _build_field_ndvi(self, field_id: str, year: int, period: int) -> dict:
        tif_path = self.raster_tif(year, period)
        png_path, meta_path = self.field_ndvi_paths(field_id, year, period)
        meta_path.parent.mkdir(parents=True, exist_ok=True)

        selected = self.fields[self.fields["field_id"] == field_id]
        geometry_wgs = selected.to_crs(4326).geometry.iloc[0]
        geometry_utm = selected.to_crs(32616).geometry.iloc[0]
        geometry_merc = selected.to_crs(3857).geometry.iloc[0]
        latitude = float(geometry_wgs.centroid.y)

        crop = raster.field_crop_3857(tif_path, geometry_merc, latitude)
        stats = raster.field_stats(tif_path, geometry_utm)

        if crop is None or stats is None:
            meta = {
                "status": "no_pixels",
                "field_id": field_id,
                "year": year,
                "period": period,
                "reason": "no clear 10 m pixels inside this field for this period",
            }
        else:
            raster.write_gray_alpha_png(png_path, crop["ndvi"])
            from rasterio.warp import transform_bounds

            left, bottom, right, top = transform_bounds(
                "EPSG:3857", "EPSG:4326", *crop["bounds"])
            dates: list[str] = []
            county_meta = self.raster_json(year, period)
            if county_meta.exists():
                try:
                    dates = list(raster.load_json(county_meta).get("dates") or [])
                except (OSError, ValueError):
                    dates = []
            meta = {
                "status": "ready",
                "field_id": field_id,
                "year": year,
                "period": period,
                "width": crop["width"],
                "height": crop["height"],
                "resolution_m": round(crop["resolution_m"], 2),
                "ground_resolution_m": 10,
                "crs": "EPSG:3857",
                "bounds": [[bottom, left], [top, right]],
                "bounds_3857": list(crop["bounds"]),
                "dates": dates,
                "n_dates": len(dates),
                "method": ("cached 10 m UTM composite warped to Web Mercator with "
                           "nearest resampling, masked to the field"),
                "encoding": {"min": raster.NDVI_MIN, "max": raster.NDVI_MAX,
                             "scheme": "gray8 alpha8"},
                "stats": stats,
                "crop_stats": raster.raster_stats(crop["ndvi"]),
            }

        tmp = meta_path.with_name(f"{meta_path.stem}.tmp.json")
        tmp.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        tmp.replace(meta_path)
        return meta

    def field_ndvi_info(self, field_id: str, year: int, period: int) -> dict | None:
        if field_id not in self._by_id.index:
            return None
        tif_path = self.raster_tif(year, period)
        if not tif_path.exists():
            return {"status": "not_computed", "field_id": field_id,
                    "year": year, "period": period}

        png_path, meta_path = self.field_ndvi_paths(field_id, year, period)
        if not self._field_ndvi_cached(png_path, meta_path, tif_path):
            with self._field_raster_lock:
                if not self._field_ndvi_cached(png_path, meta_path, tif_path):
                    self._build_field_ndvi(field_id, year, period)

        try:
            payload = raster.load_json(meta_path)
        except (OSError, ValueError):
            return {"status": "error", "field_id": field_id,
                    "year": year, "period": period,
                    "error": "could not read cached field crop"}
        payload["png_url"] = (f"/api/field-ndvi.png?field_id={field_id}"
                              f"&year={year}&period={period}")
        return payload

    def field_ndvi_png(self, field_id: str, year: int, period: int) -> Path | None:
        info = self.field_ndvi_info(field_id, year, period)
        if info is None or info.get("status") != "ready":
            return None
        png_path, _ = self.field_ndvi_paths(field_id, year, period)
        return png_path if png_path.exists() else None

    # ------------------------------------------------------------------
    # SSURGO soil polygons
    # ------------------------------------------------------------------
    def _soil_frame(self) -> gpd.GeoDataFrame:
        """Load the cached county SSURGO layer once, with a spatial index."""
        with self._soil_frame_lock:
            if self._soil_gdf is None:
                if not self.cfg.soil_cache.exists():
                    with self._soil_lock:
                        if not self.cfg.soil_cache.exists():
                            soils_layer.build(self.cfg, self.fields)
                frame = gpd.read_file(self.cfg.soil_cache)
                if frame.crs is None:
                    frame = frame.set_crs(4326)
                frame = frame.to_crs(4326)
                frame["geometry"] = frame.geometry.make_valid()
                self._soil_gdf = frame
            return self._soil_gdf

    def field_soils_geojson(self, field_id: str) -> dict | None:
        """SSURGO polygons clipped to one field, cached per field."""
        if field_id not in self._by_id.index:
            return None

        out = self.cfg.field_soil_cache / f"{field_id}.geojson"
        soil_mtime = (self.cfg.soil_cache.stat().st_mtime
                      if self.cfg.soil_cache.exists() else 0.0)

        def fresh() -> bool:
            return out.exists() and out.stat().st_mtime >= soil_mtime

        if fresh():
            return json.loads(out.read_text(encoding="utf-8"))

        with self._field_soil_lock:
            if fresh():
                return json.loads(out.read_text(encoding="utf-8"))
            payload = self._build_field_soils(field_id)
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_name(f"{out.stem}.tmp.geojson")
            tmp.write_text(json.dumps(payload, separators=(",", ":")),
                           encoding="utf-8")
            tmp.replace(out)
            return payload

    def _build_field_soils(self, field_id: str) -> dict:
        frame = self._soil_frame()
        geometry = self.fields[self.fields["field_id"] == field_id] \
            .to_crs(4326).geometry.iloc[0]

        indices = frame.sindex.query(geometry, predicate="intersects")
        candidates = frame.iloc[sorted(set(int(i) for i in indices))]
        if candidates.empty:
            clipped = candidates
        else:
            clipped = gpd.clip(candidates, gpd.GeoSeries([geometry], crs=4326))
        clipped = clipped[clipped.geometry.notna() & ~clipped.geometry.is_empty].copy()
        if not clipped.empty:
            clipped["geometry"] = clipped.geometry.make_valid()

        records = []
        for row in clipped.itertuples(index=False):
            def text(value):
                return None if pd.isna(value) else value

            def num(value):
                return None if pd.isna(value) else round(float(value), 2)

            records.append({
                "type": "Feature",
                "properties": {
                    "field_id": field_id,
                    "mukey": str(row.mukey),
                    "musym": row.musym,
                    "muname": row.muname,
                    "drainage": text(row.drainage),
                    "hydgrp": text(row.hydgrp),
                    "slope_pct": num(row.slope_pct),
                    "awc_050": num(row.awc_050),
                    "wt_depth_cm": num(row.wt_depth_cm),
                    "flood_freq": text(row.flood_freq),
                },
                "geometry": row.geometry.__geo_interface__,
            })

        coverage = None
        if not clipped.empty:
            field_area = gpd.GeoSeries([geometry], crs=4326).to_crs(32616).area.iloc[0]
            soil_area = clipped.to_crs(32616).union_all().area
            if field_area:
                coverage = round(100.0 * soil_area / field_area, 2)

        symbols = sorted({r["properties"]["musym"] for r in records
                          if r["properties"]["musym"]})
        return {
            "type": "FeatureCollection",
            "field_id": field_id,
            "n_features": len(records),
            "map_units": symbols,
            "coverage_pct": coverage,
            "features": records,
        }

    def soils_geojson(self) -> dict:
        if self.cfg.soil_cache.exists():
            return json.loads(self.cfg.soil_cache.read_text(encoding="utf-8"))
        with self._soil_lock:
            if self.cfg.soil_cache.exists():
                return json.loads(self.cfg.soil_cache.read_text(encoding="utf-8"))
            soils_layer.build(self.cfg, self.fields)
            return json.loads(self.cfg.soil_cache.read_text(encoding="utf-8"))

    def build_soils(self) -> dict:
        return soils_layer.build(self.cfg, self.fields)
