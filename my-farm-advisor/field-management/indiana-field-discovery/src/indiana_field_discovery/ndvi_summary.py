"""Phase 5: aggregate daily field NDVI into monthly, annual, trend and 6-year tables.

Reads only the compact daily statistics written by :mod:`ndvi`. No imagery is
touched. Coverage rules are explicit so a "peak" is never taken from a mostly
cloud-masked observation.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

from . import sources
from .paths import farm_paths

PEAK_MIN_COVERAGE = 50.0   # percent of field pixels required for a peak candidate
TREND_MIN_COVERAGE = 50.0  # percent of field pixels required for trend fitting
SEASON_EARLY = (4, 5)      # Apr-May
SEASON_MID = (6, 7)        # Jun-Jul
SEASON_LATE = (8, 9, 10)   # Aug-Oct


def _weighted_mean(values: pd.Series, weights: pd.Series) -> float:
    mask = values.notna() & weights.notna() & (weights > 0)
    if not mask.any():
        return float("nan")
    v = values[mask].to_numpy(dtype=float)
    w = weights[mask].to_numpy(dtype=float)
    return float(np.average(v, weights=w))


def _slope(x: np.ndarray, y: np.ndarray) -> float:
    if x.size < 2:
        return float("nan")
    x = x.astype(float)
    y = y.astype(float)
    xm = x.mean()
    denom = ((x - xm) ** 2).sum()
    if denom == 0:
        return float("nan")
    return float(((x - xm) * (y - y.mean())).sum() / denom)


def _monthly(daily: pd.DataFrame) -> pd.DataFrame:
    daily = daily.copy()
    daily["month"] = pd.to_datetime(daily["date"]).dt.month
    daily["wv"] = daily["ndvi_mean"] * daily["valid_px"]
    grouped = daily.groupby(["field_id", "year", "month"], sort=True).agg(
        n_dates=("date", "nunique"),
        wv=("wv", "sum"),
        w=("valid_px", "sum"),
        ndvi_median_of_dates=("ndvi_median", "median"),
        ndvi_min=("ndvi_median", "min"),
        ndvi_max=("ndvi_median", "max"),
        coverage_pct_mean=("coverage_pct", "mean"),
        valid_px_total=("valid_px", "sum"),
    ).reset_index()
    grouped["ndvi_mean"] = (grouped["wv"] / grouped["w"].replace(0, np.nan)).round(5)
    return pd.DataFrame({
        "field_id": grouped["field_id"],
        "year": grouped["year"].astype(int),
        "month": grouped["month"].astype(int),
        "n_dates": grouped["n_dates"].astype(int),
        "ndvi_mean": grouped["ndvi_mean"],
        "ndvi_median_of_dates": grouped["ndvi_median_of_dates"].round(5),
        "ndvi_min": grouped["ndvi_min"].round(5),
        "ndvi_max": grouped["ndvi_max"].round(5),
        "coverage_pct_mean": grouped["coverage_pct_mean"].round(2),
        "valid_px_total": grouped["valid_px_total"].astype(int),
    })


def _annual(daily: pd.DataFrame) -> pd.DataFrame:
    usable = daily[daily["coverage_pct"] >= TREND_MIN_COVERAGE]
    peak_rows = []
    for (field_id, year), chunk in usable.groupby(["field_id", "year"], sort=True):
        best = chunk.loc[chunk["ndvi_median"].idxmax()]
        early = chunk[chunk["date"].str[5:7].astype(int).isin(SEASON_EARLY)]
        mid = chunk[chunk["date"].str[5:7].astype(int).isin(SEASON_MID)]
        late = chunk[chunk["date"].str[5:7].astype(int).isin(SEASON_LATE)]
        doy = pd.to_datetime(chunk["date"]).dt.dayofyear.to_numpy()
        peak_rows.append({
            "field_id": field_id,
            "year": int(year),
            "n_dates": int(len(chunk)),
            "first_date": chunk["date"].min(),
            "last_date": chunk["date"].max(),
            "season_mean_ndvi": round(_weighted_mean(chunk["ndvi_mean"], chunk["valid_px"]), 5),
            "peak_ndvi": round(float(best["ndvi_median"]), 5),
            "peak_date": best["date"],
            "peak_coverage_pct": round(float(best["coverage_pct"]), 2),
            "early_mean_ndvi": round(_weighted_mean(early["ndvi_mean"], early["valid_px"]), 5),
            "mid_mean_ndvi": round(_weighted_mean(mid["ndvi_mean"], mid["valid_px"]), 5),
            "late_mean_ndvi": round(_weighted_mean(late["ndvi_mean"], late["valid_px"]), 5),
            "season_slope_per_day": round(_slope(doy, chunk["ndvi_median"].to_numpy()), 6),
            "coverage_pct_mean": round(float(chunk["coverage_pct"].mean()), 2),
        })
    return pd.DataFrame(peak_rows)


def _trends(annual: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for field_id, chunk in annual.groupby("field_id", sort=True):
        chunk = chunk.sort_values("year")
        years = chunk["year"].to_numpy(dtype=float)
        rows.append({
            "field_id": field_id,
            "n_years": int(len(chunk)),
            "years": ";".join(str(int(y)) for y in chunk["year"]),
            "peak_mean": round(float(chunk["peak_ndvi"].mean()), 5),
            "peak_std": round(float(chunk["peak_ndvi"].std(ddof=0)), 5),
            "peak_slope_per_year": round(_slope(years, chunk["peak_ndvi"].to_numpy()), 6),
            "season_mean_mean": round(float(chunk["season_mean_ndvi"].mean()), 5),
            "season_mean_slope_per_year": round(
                _slope(years, chunk["season_mean_ndvi"].to_numpy()), 6),
            "first_year": int(chunk["year"].min()),
            "last_year": int(chunk["year"].max()),
        })
    return pd.DataFrame(rows)


def _year_summary(daily: pd.DataFrame, annual: pd.DataFrame, requested_years: list[int]) -> pd.DataFrame:
    rows = []
    for year in requested_years:
        d = daily[daily["year"] == year]
        a = annual[annual["year"] == year]
        rows.append({
            "year": year,
            "tile_dates_with_any_field_data": int(d["date"].nunique()),
            "fields_with_any_observation": int(d["field_id"].nunique()),
            "field_date_rows": int(len(d)),
            "median_field_coverage_pct": round(float(d["coverage_pct"].median()), 2) if len(d) else None,
            "fields_with_peak": int(len(a)),
            "peak_ndvi_median": round(float(a["peak_ndvi"].median()), 4) if len(a) else None,
            "peak_ndvi_p10": round(float(a["peak_ndvi"].quantile(0.10)), 4) if len(a) else None,
            "peak_ndvi_p90": round(float(a["peak_ndvi"].quantile(0.90)), 4) if len(a) else None,
            "season_mean_ndvi_median": round(float(a["season_mean_ndvi"].median()), 4) if len(a) else None,
        })
    return pd.DataFrame(rows)


def _render_map(trends: pd.DataFrame, fields: gpd.GeoDataFrame, out: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    gdf = fields[["field_id", "geometry"]].merge(trends, on="field_id", how="left").to_crs(4326)
    fig, ax = plt.subplots(figsize=(11, 9), dpi=150)
    missing = gdf[gdf["peak_mean"].isna()]
    if len(missing):
        missing.plot(ax=ax, color="#d9d9d9", edgecolor="none")
    gdf[gdf["peak_mean"].notna()].plot(
        ax=ax, column="peak_mean", cmap="YlGn", legend=True, vmin=0.5, vmax=0.95,
        edgecolor="none", legend_kwds={"label": "mean annual peak NDVI (2021-2026)", "shrink": 0.7},
    )
    ax.set_title("Johnson County, IN — mean annual peak field NDVI (2021–2026)")
    ax.set_xlabel("longitude")
    ax.set_ylabel("latitude")
    ax.set_aspect("equal")
    fig.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)


def summarise(args: argparse.Namespace) -> int:
    t0 = time.time()
    paths = farm_paths(args.grower, args.farm).ensure()
    daily_path = paths.derived_dir / "ndvi" / "field_ndvi_daily.parquet"
    if not daily_path.exists():
        raise SystemExit(f"missing {daily_path}; run the ndvi stage first")

    daily = pd.read_parquet(daily_path)
    daily["field_id"] = daily["field_id"].astype(str)
    print(f"[summary] {len(daily):,} field-date rows across {daily['date'].nunique()} dates")

    monthly = _monthly(daily)
    annual = _annual(daily)
    trends = _trends(annual)
    years = list(range(args.start_year, args.end_year + 1))
    year_summary = _year_summary(daily, annual, years)

    out_dir = paths.derived_dir / "ndvi"
    monthly.to_parquet(out_dir / "field_ndvi_monthly.parquet", index=False)
    monthly.to_csv(out_dir / "field_ndvi_monthly.csv", index=False)
    annual.to_parquet(out_dir / "field_ndvi_annual.parquet", index=False)
    annual.to_csv(out_dir / "field_ndvi_annual.csv", index=False)
    trends.to_parquet(out_dir / "field_ndvi_trends.parquet", index=False)
    trends.to_csv(out_dir / "field_ndvi_trends.csv", index=False)
    year_summary.to_csv(out_dir / "county_ndvi_year_summary.csv", index=False)

    fields = gpd.read_parquet(paths.boundary_dir / "field_boundaries_ag.parquet")
    fields["field_id"] = fields["field_id"].astype(str)
    _render_map(trends, fields, paths.derived_dir / "reports" / "johnson_in_ndvi_peak_map.png")

    coverage = daily.groupby("year").agg(
        dates=("date", "nunique"),
        fields=("field_id", "nunique"),
        rows=("field_id", "size"),
        median_coverage=("coverage_pct", "median"),
    ).reset_index()
    coverage["median_coverage"] = coverage["median_coverage"].round(2)
    coverage.to_csv(out_dir / "county_ndvi_coverage.csv", index=False)

    report = {
        "generated_at": sources.utc_now(),
        "daily_rows": int(len(daily)),
        "dates": int(daily["date"].nunique()),
        "fields_with_ndvi": int(daily["field_id"].nunique()),
        "coverage_by_year": coverage.to_dict(orient="records"),
        "peak_rule": f"max daily ndvi_median among dates with coverage_pct >= {PEAK_MIN_COVERAGE}",
        "trend_rule": f"OLS slope over years and day-of-year; coverage_pct >= {TREND_MIN_COVERAGE}",
        "percentile_note": "p10/p90 from a 400-bin NDVI histogram; accuracy about one bin (0.005)",
    }
    (out_dir / "ndvi_summary.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(year_summary.to_string(index=False))
    print(f"[summary] {len(monthly)} field-months, {len(annual)} field-years, "
          f"{len(trends)} field trends in {time.time() - t0:.1f}s")
    return 0


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser("ndvi-summary", help="Aggregate daily NDVI to monthly/annual/trends")
    parser.add_argument("--grower", default="in-johnson")
    parser.add_argument("--farm", default="countywide")
    parser.add_argument("--start-year", type=int, default=2021)
    parser.add_argument("--end-year", type=int, default=2026)
    parser.set_defaults(func=summarise)
