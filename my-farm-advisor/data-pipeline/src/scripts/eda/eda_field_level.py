#!/usr/bin/env python3
# ruff: noqa: E402
# pyright: reportMissingImports=false, reportAttributeAccessIssue=false, reportCallIssue=false
"""Field-level exploratory data analysis across growers, farms and years.

Produces static PNG/SVG figures and CSV tables for three required categories:

* field boundaries (area, shape, perimeter-area scaling)
* historical weather (growing-season water supply, GDD, spatial coherence)
* USDA CDL crop history (rotation sequences, composition, transitions)

plus one static geospatial map built from the real field polygons.

The analysis is read-only: it never downloads data and never writes inside a
grower tree. Outputs go to ``shared/eda/<run-slug>`` by default.

Run from the installed runtime source copy:

    python scripts/eda/eda_field_level.py \
        --grower illinois-grower --grower iowa-grower --grower nebraska-grower \
        --start-year 2021 --end-year 2025 --command all
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

STATE_ORDER_DEFAULT = ("IL", "IA", "NE")
STATE_COLORS = {"IL": "#3b6ea5", "IA": "#d17a3f", "NE": "#4f9d69"}
FALLBACK_COLORS = ("#6b5b95", "#b56576", "#2a9d8f", "#e9c46a", "#457b9d")
CROP_COLORS = {"Corn": "#e0a526", "Soybeans": "#4f8f45", "Other": "#9aa0a6"}
CROP_LETTER = {"Corn": "C", "Soybeans": "S", "Other": "O"}


def _preparse_root(argv: list[str]) -> str | None:
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--root", default=None)
    known, _ = pre.parse_known_args(argv)
    if known.root:
        os.environ["DATA_PIPELINE_DATA_ROOT"] = str(Path(known.root).expanduser().resolve())
    return known.root


_preparse_root(sys.argv[1:])

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
from matplotlib import ticker
from matplotlib.patches import Patch

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR / "lib"))

from eda_metrics import (  # noqa: E402
    GDD_BASE_C,
    SEASON_END_MONTH,
    SEASON_START_MONTH,
    boundary_metrics,
    crop_history_metrics,
    crop_transition_counts,
    crop_transitions,
    cumulative_gdd_curve,
    rotation_summary,
    spearman_with_permutation,
    weather_season_metrics,
)
from paths import (  # noqa: E402
    DATA_ROOT,
    farm_boundary_path,
    farm_cdl_preferred_full_composition_path,
    farm_cdl_year_table_path,
    field_weather_path,
    shared_eda_run_dir,
)

DEFAULT_START_YEAR = 2021
DEFAULT_END_YEAR = 2025


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=None, help="Runtime data root (sets DATA_PIPELINE_DATA_ROOT)")
    parser.add_argument(
        "--grower",
        action="append",
        default=None,
        help="Grower slug, optionally slug:farm. Repeatable. Defaults to all growers.",
    )
    parser.add_argument("--start-year", type=int, default=DEFAULT_START_YEAR)
    parser.add_argument("--end-year", type=int, default=DEFAULT_END_YEAR)
    parser.add_argument(
        "--cdl-years",
        default=None,
        help="Comma-separated CDL years. Defaults to --start-year..--end-year.",
    )
    parser.add_argument(
        "--command",
        choices=["all", "audit", "boundaries", "weather", "cdl", "map"],
        default="all",
    )
    parser.add_argument("--out", default=None, help="Output directory. Defaults to shared/eda/field-level.")
    parser.add_argument("--formats", default="png,svg", help="Comma-separated figure formats.")
    parser.add_argument("--dpi", type=int, default=300)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--no-basemap", action="store_true", help="Skip contextily tiles on the map.")
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Discovery and loading
# ---------------------------------------------------------------------------


def _log(args: argparse.Namespace, message: str) -> None:
    if not args.quiet:
        print(message, flush=True)


def _resolve_grower_specs(args: argparse.Namespace) -> list[tuple[str, list[str]]]:
    growers_root = DATA_ROOT / "growers"
    if not growers_root.exists():
        raise SystemExit(f"growers directory not found: {growers_root}")

    if not args.grower:
        specs = []
        for grower_dir in sorted(path for path in growers_root.iterdir() if path.is_dir()):
            farms = sorted(
                path.name
                for path in (grower_dir / "farms").glob("*")
                if (path / "boundary" / "field_boundaries.geojson").exists()
            )
            if farms:
                specs.append((grower_dir.name, farms))
        return specs

    specs = []
    for spec in args.grower:
        if ":" in spec:
            grower_slug, farm_slug = spec.split(":", 1)
            specs.append((grower_slug.strip(), [farm_slug.strip()]))
        else:
            grower_slug = spec.strip()
            farms = sorted(
                path.name
                for path in (growers_root / grower_slug / "farms").glob("*")
                if (path / "boundary" / "field_boundaries.geojson").exists()
            )
            if not farms:
                raise SystemExit(f"no farm boundaries found for grower {grower_slug}")
            specs.append((grower_slug, farms))
    return specs


def _farm_metadata(grower_slug: str, farm_slug: str) -> dict:
    path = DATA_ROOT / "growers" / grower_slug / "farms" / farm_slug / "farm.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def load_fields(specs: list[tuple[str, list[str]]]) -> gpd.GeoDataFrame:
    frames: list[gpd.GeoDataFrame] = []
    for grower_slug, farm_slugs in specs:
        for farm_slug in farm_slugs:
            boundary_path = farm_boundary_path(grower_slug, farm_slug)
            if not boundary_path.exists():
                continue
            frame = gpd.read_file(boundary_path)
            if frame.empty:
                continue
            metadata = _farm_metadata(grower_slug, farm_slug)
            frame["grower"] = grower_slug
            frame["farm"] = farm_slug
            frame["state"] = (
                frame.get("state", pd.Series([""] * len(frame)))
                .fillna("")
                .astype(str)
                .replace("", metadata.get("state", ""))
            )
            if "county_name" not in frame.columns:
                frame["county_name"] = ""
            frames.append(frame)
    if not frames:
        raise SystemExit("no field boundaries found for the requested growers")
    combined = pd.concat(frames, ignore_index=True)
    combined = gpd.GeoDataFrame(combined, geometry="geometry", crs=frames[0].crs)
    if combined.crs is None:
        combined = combined.set_crs("EPSG:4326")
    return combined.to_crs("EPSG:4326")


def load_weather(fields: gpd.GeoDataFrame, start_year: int, end_year: int) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for row in fields[["grower", "farm", "field_id"]].drop_duplicates().itertuples(index=False):
        path = field_weather_path(row.grower, row.farm, row.field_id)
        if not path.exists():
            continue
        frame = pd.read_csv(path)
        frame["field_id"] = row.field_id
        frames.append(frame)
    if not frames:
        raise SystemExit("no per-field weather CSVs found; run download_weather.py first")
    combined = pd.concat(frames, ignore_index=True)
    combined["date"] = pd.to_datetime(combined["date"])
    combined = combined[combined["date"].dt.year.between(start_year, end_year)].copy()
    label_map = fields.set_index("field_id")[["grower", "farm", "state"]]
    combined = combined.merge(label_map, left_on="field_id", right_index=True, how="left")
    return combined.sort_values(["grower", "farm", "field_id", "date"]).reset_index(drop=True)


def load_cdl(fields: gpd.GeoDataFrame, years: list[int]) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for row in fields[["grower", "farm"]].drop_duplicates().itertuples(index=False):
        preferred = farm_cdl_preferred_full_composition_path(row.grower, row.farm)
        if preferred.exists():
            frame = pd.read_csv(preferred)
        else:
            yearly = [
                pd.read_csv(path)
                for path in (
                    farm_cdl_year_table_path(row.grower, row.farm, year) for year in years
                )
                if path.exists()
            ]
            if not yearly:
                continue
            frame = pd.concat(yearly, ignore_index=True)
        frame = frame[frame["year"].isin(years)].copy()
        frame["grower"] = row.grower
        frame["farm"] = row.farm
        frames.append(frame)
    if not frames:
        raise SystemExit("no farm CDL composition tables found; run download_cdl.py first")
    combined = pd.concat(frames, ignore_index=True)
    label_map = fields.set_index("field_id")[["state", "area_acres"]]
    combined = combined.merge(label_map, left_on="field_id", right_index=True, how="left")
    return combined.sort_values(["state", "field_id", "year"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Shared output helpers
# ---------------------------------------------------------------------------


class Outputs:
    def __init__(self, root: Path, formats: list[str], dpi: int) -> None:
        self.root = root
        self.tables = root / "tables"
        self.figures = root / "figures"
        self.audit = root / "audit"
        self.report = root / "report"
        for directory in (self.tables, self.figures, self.audit, self.report):
            directory.mkdir(parents=True, exist_ok=True)
        self.formats = formats
        self.dpi = dpi
        self.generated: list[str] = []

    def table(self, name: str, frame: pd.DataFrame) -> Path:
        path = self.tables / name
        frame.to_csv(path, index=False)
        self.generated.append(str(path.relative_to(self.root)))
        return path

    def figure(self, figure, name: str) -> list[Path]:
        paths = []
        for fmt in self.formats:
            path = self.figures / f"{name}.{fmt}"
            figure.savefig(path, dpi=self.dpi, bbox_inches="tight", facecolor="white")
            paths.append(path)
            self.generated.append(str(path.relative_to(self.root)))
        plt.close(figure)
        return paths


def _style() -> None:
    sns.set_theme(style="whitegrid", context="paper")
    matplotlib.rcParams.update(
        {
            "figure.dpi": 110,
            "savefig.dpi": 300,
            "font.size": 9,
            "axes.titlesize": 10,
            "axes.titleweight": "bold",
            "axes.labelsize": 9,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "legend.frameon": False,
            "figure.facecolor": "white",
        }
    )


def _state_order(states: list[str]) -> list[str]:
    ordered = [state for state in STATE_ORDER_DEFAULT if state in states]
    ordered.extend(sorted(state for state in states if state and state not in ordered))
    unordered = [state for state in states if not state and state not in ordered]
    return ordered + unordered


def _state_color(state: str, index: int) -> str:
    return STATE_COLORS.get(state, FALLBACK_COLORS[index % len(FALLBACK_COLORS)])


def _state_palette(states: list[str]) -> dict[str, str]:
    return {state: _state_color(state, index) for index, state in enumerate(states)}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def run_audit(
    args: argparse.Namespace,
    outputs: Outputs,
    fields: gpd.GeoDataFrame,
    weather: pd.DataFrame,
    cdl: pd.DataFrame,
) -> None:
    rows: list[dict[str, object]] = []

    for (grower, farm), group in fields.groupby(["grower", "farm"], dropna=False):
        projected = group.to_crs("EPSG:5070")
        areas = projected.geometry.area / 4046.8564224
        overlaps = 0
        geometries = list(projected.geometry)
        for left in range(len(geometries)):
            for right in range(left + 1, len(geometries)):
                intersection = geometries[left].intersection(geometries[right]).area
                smaller = min(geometries[left].area, geometries[right].area)
                if smaller and intersection / smaller > 0.05:
                    overlaps += 1
        rows.append(
            {
                "category": "boundaries",
                "grower": grower,
                "farm": farm,
                "state": group["state"].mode().iloc[0] if not group["state"].mode().empty else "",
                "records": len(group),
                "detail": (
                    f"acres={areas.sum():.1f}; median={areas.median():.1f}; "
                    f"min={areas.min():.1f}; max={areas.max():.1f}; "
                    f"invalid={int((~group.geometry.is_valid).sum())}; "
                    f"overlap_pairs={overlaps}"
                ),
            }
        )

    for (grower, farm), group in weather.groupby(["grower", "farm"], dropna=False):
        expected_days = (args.end_year - args.start_year + 1) * 365 + sum(
            1
            for year in range(args.start_year, args.end_year + 1)
            if (year % 4 == 0 and year % 100 != 0) or year % 400 == 0
        )
        per_field = group.groupby("field_id")["date"].nunique()
        grid_cells = group[["field_id", "lat", "lon"]].drop_duplicates()
        rows.append(
            {
                "category": "weather",
                "grower": grower,
                "farm": farm,
                "state": group["state"].mode().iloc[0] if not group["state"].mode().empty else "",
                "records": len(group),
                "detail": (
                    f"fields={group['field_id'].nunique()}; "
                    f"days_per_field={per_field.min()}..{per_field.max()} of {expected_days}; "
                    f"missing_T2M={int(group['T2M'].isna().sum())}; "
                    f"missing_precip={int(group['PRECTOTCORR'].isna().sum())}; "
                    f"distinct_power_centroids={len(grid_cells)}; "
                    f"range={group['date'].min().date()}..{group['date'].max().date()}"
                ),
            }
        )

    if not cdl.empty:
        for (grower, farm), group in cdl.groupby(["grower", "farm"], dropna=False):
            years = sorted(group["year"].unique())
            per_year = group.groupby(["field_id", "year"])["pct"].sum()
            rows.append(
                {
                    "category": "cdl",
                    "grower": grower,
                    "farm": farm,
                    "state": group["state"].mode().iloc[0] if not group["state"].mode().empty else "",
                    "records": len(group),
                    "detail": (
                        f"fields={group['field_id'].nunique()}; years={','.join(map(str, years))}; "
                        f"pct_sum_min={per_year.min():.1f}; pct_sum_max={per_year.max():.1f}; "
                        f"missing_crop_name={int(group['crop_name'].isna().sum())}"
                    ),
                }
            )

    audit_frame = pd.DataFrame(rows)
    outputs.table("data_audit.csv", audit_frame)

    lines = ["# Data audit", ""]
    lines.append(f"Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    lines.append("")
    lines.append("| Category | Grower | Farm | Records | Detail |")
    lines.append("|---|---|---|---:|---|")
    for row in audit_frame.itertuples(index=False):
        lines.append(
            f"| {row.category} | {row.grower} | {row.farm} | {row.records} | {row.detail} |"
        )
    lines.append("")
    (outputs.audit / "data_audit.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    outputs.generated.append("audit/data_audit.md")
    _log(args, f"audit: wrote {len(audit_frame)} rows")


# ---------------------------------------------------------------------------
# Boundaries
# ---------------------------------------------------------------------------


def run_boundaries(
    args: argparse.Namespace,
    outputs: Outputs,
    fields: gpd.GeoDataFrame,
    states: list[str],
) -> pd.DataFrame:
    metrics = boundary_metrics(fields)
    outputs.table("field_boundary_metrics.csv", metrics)

    stats_rows: list[dict[str, object]] = []
    for state, group in metrics.groupby("state", dropna=False):
        stats_rows.append(
            {
                "state": state,
                "n_fields": len(group),
                "area_acres_median": group["area_acres"].median(),
                "area_acres_iqr": group["area_acres"].quantile(0.75)
                - group["area_acres"].quantile(0.25),
                "area_acres_min": group["area_acres"].min(),
                "area_acres_max": group["area_acres"].max(),
                "compactness_median": group["compactness_pp"].median(),
                "rect_fill_median": group["rect_fill"].median(),
                "aspect_median": group["aspect_ratio"].median(),
                "pivot_like_share": float((group["shape_class"] == "pivot-like").mean()),
                "rectangular_share": float((group["shape_class"] == "rectangular").mean()),
                "irregular_share": float((group["shape_class"] == "irregular").mean()),
            }
        )
    stats_frame = pd.DataFrame(stats_rows)
    outputs.table("boundary_stats_by_state.csv", stats_frame)

    palette = _state_palette(states)
    ordered_states = [state for state in states if state in set(metrics["state"])]

    # A1 - field area distribution
    figure, axis = plt.subplots(figsize=(7.2, 4.2))
    sns.boxplot(
        data=metrics,
        x="area_acres",
        y="state",
        order=ordered_states,
        hue="state",
        palette=palette,
        legend=False,
        width=0.45,
        fliersize=0,
        ax=axis,
    )
    sns.stripplot(
        data=metrics,
        x="area_acres",
        y="state",
        order=ordered_states,
        hue="state",
        palette=palette,
        legend=False,
        size=4.5,
        alpha=0.75,
        jitter=0.16,
        ax=axis,
    )
    axis.set_xscale("log")
    area_values = metrics["area_acres"].dropna()
    if not area_values.empty:
        tick_candidates = [10, 20, 50, 100, 200, 500, 1000, 2000]
        visible_ticks = [
            tick
            for tick in tick_candidates
            if area_values.min() * 0.8 <= tick <= area_values.max() * 1.3
        ]
        axis.set_xticks(visible_ticks)
        axis.xaxis.set_major_formatter(ticker.ScalarFormatter())
        axis.xaxis.set_minor_formatter(ticker.NullFormatter())
    axis.set_xlabel("Field area (acres, log scale)")
    axis.set_ylabel("")
    axis.set_title(
        "A1  Field size distribution by grower state\n"
        f"n={len(metrics)} fields; current OSM boundary snapshot from the runtime root"
    )
    for state in ordered_states:
        subset = metrics.loc[metrics["state"] == state, "area_acres"]
        axis.text(
            subset.max() * 1.08,
            ordered_states.index(state),
            f"median {subset.median():.0f} ac",
            va="center",
            fontsize=8,
            color="#333333",
        )
    figure.tight_layout()
    outputs.figure(figure, "fig01_boundary_area_by_state")

    # A2 - shape: compactness ECDF + shape class composition
    figure, (left, right) = plt.subplots(1, 2, figsize=(9.2, 4.1), gridspec_kw={"width_ratios": [1.1, 1]})
    for state in ordered_states:
        values = np.sort(metrics.loc[metrics["state"] == state, "compactness_pp"].dropna().to_numpy())
        if values.size:
            left.step(
                values,
                np.arange(1, values.size + 1) / values.size,
                where="post",
                label=f"{state} (n={values.size})",
                color=palette[state],
                lw=1.6,
            )
    left.axvline(math.pi / 4.0, color="#b23b3b", ls="--", lw=1.2)
    left.text(
        math.pi / 4.0 + 0.01,
        0.08,
        "square compactness\n\u03c0/4 \u2248 0.785",
        color="#b23b3b",
        fontsize=7.5,
        va="bottom",
    )
    left.set_xlabel("Polsby-Popper compactness (1 = circle)")
    left.set_ylabel("Cumulative share of fields")
    left.set_title("A2a  Shape compactness")
    left.legend(fontsize=7.5, loc="upper left", framealpha=0.9)
    left.set_ylim(0, 1.02)

    shape_order = ["pivot-like", "rectangular", "irregular"]
    shape_colors = {"pivot-like": "#b23b3b", "rectangular": "#3b6ea5", "irregular": "#8d8d8d"}
    shares = (
        metrics.groupby(["state", "shape_class"]).size().unstack(fill_value=0).reindex(
            index=ordered_states, columns=shape_order, fill_value=0
        )
    )
    shares_pct = shares.div(shares.sum(axis=1), axis=0) * 100.0
    bottom = np.zeros(len(shares_pct))
    for shape in shape_order:
        values = shares_pct[shape].to_numpy()
        right.bar(
            shares_pct.index,
            values,
            bottom=bottom,
            color=shape_colors[shape],
            label=shape,
            width=0.6,
        )
        for index, value in enumerate(values):
            if value >= 6:
                right.text(
                    index,
                    bottom[index] + value / 2,
                    f"{value:.0f}%",
                    ha="center",
                    va="center",
                    fontsize=7.5,
                    color="white" if shape != "irregular" else "#222222",
                )
        bottom += values
    right.set_ylabel("Share of fields (%)")
    right.set_title("A2b  Shape class composition")
    right.legend(fontsize=7.5, loc="upper center", ncol=3, bbox_to_anchor=(0.5, -0.12))
    figure.suptitle("A2  Field shape signature", fontsize=10, fontweight="bold", y=1.02)
    figure.tight_layout()
    outputs.figure(figure, "fig02_boundary_shape_by_state")

    # A3 - perimeter-area scaling correlation
    figure, axis = plt.subplots(figsize=(6.8, 4.6))
    correlation_rows: list[dict[str, object]] = []
    for state in ordered_states:
        subset = metrics[metrics["state"] == state].dropna(subset=["area_acres", "perimeter_m"])
        if subset.empty:
            continue
        axis.scatter(
            subset["area_acres"],
            subset["perimeter_m"],
            s=34,
            alpha=0.8,
            color=palette[state],
            edgecolor="white",
            linewidth=0.5,
            label=state,
        )
        slope, intercept = np.polyfit(
            np.log10(subset["area_acres"]), np.log10(subset["perimeter_m"]), 1
        )
        x_grid = np.logspace(
            np.log10(subset["area_acres"].min()), np.log10(subset["area_acres"].max()), 60
        )
        axis.plot(x_grid, 10 ** (intercept + slope * np.log10(x_grid)), color=palette[state], lw=1.4)
        result = spearman_with_permutation(
            subset["area_acres"], subset["perimeter_m"], permutations=5000, seed=args.seed
        )
        correlation_rows.append(
            {
                "state": state,
                "n": int(result["n"]),
                "spearman_rho": result["rho"],
                "p_permutation": result["p_permutation"],
                "ci_low": result["ci_low"],
                "ci_high": result["ci_high"],
                "loglog_slope": float(slope),
                "expected_self_similar_slope": 0.5,
            }
        )
        axis.annotate(
            f"{state}: slope={slope:.2f}, \u03c1={result['rho']:.2f}",
            xy=(0.03, 0.92 - 0.08 * ordered_states.index(state)),
            xycoords="axes fraction",
            fontsize=8,
            color=palette[state],
        )
    correlations = pd.DataFrame(correlation_rows)
    outputs.table("boundary_correlations.csv", correlations)
    axis.set_xscale("log")
    axis.set_yscale("log")
    area_values = metrics["area_acres"].dropna()
    if not area_values.empty:
        tick_candidates = [10, 20, 50, 100, 200, 500, 1000, 2000]
        visible_ticks = [
            tick
            for tick in tick_candidates
            if area_values.min() * 0.8 <= tick <= area_values.max() * 1.3
        ]
        axis.set_xticks(visible_ticks)
        axis.xaxis.set_major_formatter(ticker.ScalarFormatter())
        axis.xaxis.set_minor_formatter(ticker.NullFormatter())
    axis.set_xlabel("Field area (acres, log scale)")
    axis.set_ylabel("Field perimeter (m, log scale)")
    axis.set_title(
        "A3  Perimeter-area scaling by state\n"
        "slope 0.5 = self-similar shapes; steeper = elongation or irregular edges"
    )
    axis.legend(fontsize=8, loc="lower right")
    figure.tight_layout()
    outputs.figure(figure, "fig03_boundary_perimeter_area")
    _log(args, f"boundaries: {len(metrics)} fields, {len(stats_frame)} states")
    return metrics


# ---------------------------------------------------------------------------
# Weather
# ---------------------------------------------------------------------------


def run_weather(
    args: argparse.Namespace,
    outputs: Outputs,
    fields: gpd.GeoDataFrame,
    weather: pd.DataFrame,
    states: list[str],
) -> None:
    seasonal = weather_season_metrics(weather)
    labels = fields[["field_id", "state", "grower", "farm"]].drop_duplicates("field_id")
    seasonal = seasonal.merge(labels, on="field_id", how="left")
    outputs.table("weather_seasonal_metrics.csv", seasonal)

    stats_rows: list[dict[str, object]] = []
    for (state, year), group in seasonal.groupby(["state", "year"], dropna=False):
        stats_rows.append(
            {
                "state": state,
                "year": year,
                "n_fields": len(group),
                "precip_mm_median": group["gs_precip_mm"].median(),
                "precip_mm_min": group["gs_precip_mm"].min(),
                "precip_mm_max": group["gs_precip_mm"].max(),
                "gdd_median": group["gs_gdd"].median(),
                "heat_days_35c_median": group["heat_days_35c"].median(),
                "frost_free_days_median": group["frost_free_days"].median(),
                "dry_spell_days_median": group["max_dry_spell_days"].median(),
            }
        )
    stats_frame = pd.DataFrame(stats_rows)
    outputs.table("weather_stats_by_state.csv", stats_frame)

    palette = _state_palette(states)
    ordered_states = [state for state in states if state in set(seasonal["state"])]

    # B1 - growing-season precipitation trajectories
    figure, axes = plt.subplots(1, len(ordered_states), figsize=(3.2 * len(ordered_states), 3.9), sharey=True)
    axes = np.atleast_1d(axes)
    for axis, state in zip(axes, ordered_states):
        subset = seasonal[seasonal["state"] == state]
        for field_id, group in subset.groupby("field_id"):
            axis.plot(group["year"], group["gs_precip_mm"], color=palette[state], alpha=0.28, lw=1.0)
        median = subset.groupby("year")["gs_precip_mm"].median()
        axis.plot(median.index, median.values, color="#111111", lw=2.2, marker="o", ms=3.5)
        axis.set_title(
            f"{state}  median {subset['gs_precip_mm'].median():.0f} mm"
        )
        axis.set_xlabel("Year")
        axis.set_xticks(sorted(subset["year"].unique()))
        axis.tick_params(axis="x", rotation=45)
    axes[0].set_ylabel("Growing-season precipitation (mm)")
    figure.suptitle(
        "B1  Growing-season water supply (Apr-Oct)\n"
        "thin lines = individual fields; black = state median",
        fontsize=10,
        fontweight="bold",
        y=1.04,
    )
    figure.tight_layout()
    outputs.figure(figure, "fig04_weather_gs_precip")

    # B2 - cumulative GDD curves
    curves = cumulative_gdd_curve(weather)
    curves = curves.merge(labels, on="field_id", how="left")
    figure, axes = plt.subplots(1, len(ordered_states), figsize=(3.2 * len(ordered_states), 3.9), sharey=True)
    axes = np.atleast_1d(axes)
    year_colors = sns.color_palette("viridis", n_colors=len(range(args.start_year, args.end_year + 1)))
    for axis, state in zip(axes, ordered_states):
        subset = curves[curves["state"] == state]
        for color, year in zip(year_colors, range(args.start_year, args.end_year + 1)):
            year_data = subset[subset["year"] == year]
            if year_data.empty:
                continue
            grouped = year_data.groupby("doy")["cumulative_gdd"]
            median = grouped.median()
            low = grouped.quantile(0.25)
            high = grouped.quantile(0.75)
            axis.plot(median.index, median.values, color=color, lw=1.4, label=str(year))
            axis.fill_between(median.index, low.values, high.values, color=color, alpha=0.14, lw=0)
        season_totals = subset.groupby(["field_id", "year"])["cumulative_gdd"].max()
        axis.set_title(f"{state}  median season total {season_totals.median():.0f} GDD")
        axis.set_xlabel("Day of year")
    axes[0].set_ylabel(f"Cumulative GDD (\u00b0C, base {GDD_BASE_C:.0f})")
    axes[-1].legend(fontsize=7, loc="lower right", title="Year")
    figure.suptitle(
        "B2  Heat accumulation through the season\n"
        "line = state median across fields; band = interquartile range",
        fontsize=10,
        fontweight="bold",
        y=1.04,
    )
    figure.tight_layout()
    outputs.figure(figure, "fig05_weather_gdd")

    # B3 - distance decay of daily temperature correlation
    temperature = weather.pivot_table(index="date", columns="field_id", values="T2M")
    correlation_matrix = temperature.corr(method="spearman")
    unique_fields = fields.drop_duplicates("field_id").set_index("field_id")
    centroids = unique_fields.geometry.to_crs("EPSG:5070").centroid
    field_ids = list(correlation_matrix.columns)
    centroids = centroids.reindex(field_ids)
    coordinates = np.column_stack([centroids.x.to_numpy(), centroids.y.to_numpy()])
    id_to_index = {field_id: index for index, field_id in enumerate(field_ids)}
    id_to_state = fields.set_index("field_id")["state"].to_dict()
    id_to_farm = fields.set_index("field_id")["farm"].to_dict()

    pair_rows: list[dict[str, object]] = []
    for left in range(len(field_ids)):
        for right in range(left + 1, len(field_ids)):
            left_id = field_ids[left]
            right_id = field_ids[right]
            distance_km = float(
                np.hypot(
                    coordinates[id_to_index[left_id], 0] - coordinates[id_to_index[right_id], 0],
                    coordinates[id_to_index[left_id], 1] - coordinates[id_to_index[right_id], 1],
                )
                / 1000.0
            )
            pair_rows.append(
                {
                    "field_a": left_id,
                    "field_b": right_id,
                    "state_a": id_to_state.get(left_id),
                    "state_b": id_to_state.get(right_id),
                    "same_farm": id_to_farm.get(left_id) == id_to_farm.get(right_id)
                    and id_to_state.get(left_id) == id_to_state.get(right_id),
                    "distance_km": distance_km,
                    "spearman_rho": correlation_matrix.loc[left_id, right_id],
                }
            )
    pairs = pd.DataFrame(pair_rows)
    outputs.table("weather_pairwise_correlations.csv", pairs)

    figure, axis = plt.subplots(figsize=(7.0, 4.4))
    cross = pairs[~pairs["same_farm"]]
    within = pairs[pairs["same_farm"]]
    if not cross.empty:
        axis.scatter(
            cross["distance_km"],
            cross["spearman_rho"],
            s=16,
            alpha=0.35,
            color="#7f7f7f",
            label="different farms",
        )
    if not within.empty:
        axis.scatter(
            within["distance_km"],
            within["spearman_rho"],
            s=30,
            alpha=0.85,
            color="#b23b3b",
            edgecolor="white",
            linewidth=0.5,
            label="same farm",
        )
    if len(pairs) >= 10:
        bins = pd.cut(pairs["distance_km"], bins=min(10, max(4, len(pairs) // 30)))
        binned = pairs.groupby(bins, observed=True)["spearman_rho"].median()
        centers = [
            float(np.mean([interval.left, interval.right]))
            for interval in binned.index
            if isinstance(interval, pd.Interval)
        ]
        axis.plot(centers, binned.values, color="#111111", lw=2.0, marker="o", ms=4, label="binned median")
    within_median = within["spearman_rho"].median() if not within.empty else float("nan")
    cross_median = cross["spearman_rho"].median() if not cross.empty else float("nan")
    within_text = f"{within_median:.3f}" if np.isfinite(within_median) else "n/a"
    cross_text = f"{cross_median:.3f}" if np.isfinite(cross_median) else "n/a"
    axis.set_xlabel("Distance between field centroids (km)")
    axis.set_ylabel("Spearman correlation of daily mean temperature")
    if not pairs.empty:
        lowest = float(pairs["spearman_rho"].min())
        axis.set_ylim(max(-1.0, min(0.98, lowest - 0.01)), 1.003)
    axis.set_title(
        "B3  Spatial coherence of weather\n"
        f"same-farm median \u03c1={within_text}; other pairs median \u03c1={cross_text}"
    )
    if axis.get_legend_handles_labels()[0]:
        axis.legend(fontsize=8, loc="lower left")
    figure.tight_layout()
    outputs.figure(figure, "fig06_weather_distance_decay")
    _log(args, f"weather: {len(seasonal)} field-years, {len(pairs)} field pairs")


# ---------------------------------------------------------------------------
# CDL
# ---------------------------------------------------------------------------


def run_cdl(
    args: argparse.Namespace,
    outputs: Outputs,
    fields: gpd.GeoDataFrame,
    cdl: pd.DataFrame,
    states: list[str],
) -> None:
    history = crop_history_metrics(cdl)
    labels = fields[["field_id", "state", "grower", "farm", "area_acres"]].drop_duplicates("field_id")
    history = history.merge(labels, on="field_id", how="left")
    outputs.table("cdl_crop_history.csv", history)

    rotations = rotation_summary(history)
    rotations = rotations.merge(labels[["field_id", "state", "grower", "farm"]], on="field_id", how="left")
    outputs.table("cdl_rotation_summary.csv", rotations)

    stats_rows: list[dict[str, object]] = []
    for state, group in rotations.groupby("state", dropna=False):
        stats_rows.append(
            {
                "state": state,
                "n_fields": len(group),
                "continuous_corn_share": float(group["continuous_corn"].mean()),
                "mean_crop_changes": group["crop_changes"].mean(),
                "mean_rotation_diversity": group["rotation_diversity"].mean(),
                "mean_n_distinct_crops": group["n_distinct_crops"].mean(),
                "mean_corn_share_years": (group["corn_years"] / group["n_years"]).mean(),
                "mean_soy_share_years": (group["soy_years"] / group["n_years"]).mean(),
                "mean_other_share_years": (group["other_years"] / group["n_years"]).mean(),
            }
        )
    stats_frame = pd.DataFrame(stats_rows)
    outputs.table("cdl_stats_by_state.csv", stats_frame)

    palette = _state_palette(states)
    ordered_states = [state for state in states if state in set(history["state"])]
    years = sorted(history["year"].unique())

    # C1 - rotation heatmap
    field_order = (
        history[["state", "field_id"]].drop_duplicates().sort_values(["state", "field_id"])["field_id"].tolist()
    )
    crop_index = {"Corn": 0, "Soybeans": 1, "Other": 2}
    matrix = np.full((len(field_order), len(years)), np.nan)
    for row_index, field_id in enumerate(field_order):
        for column_index, year in enumerate(years):
            match = history[(history["field_id"] == field_id) & (history["year"] == year)]
            if not match.empty:
                matrix[row_index, column_index] = crop_index.get(match.iloc[0]["dominant_crop"], 2)
    figure, axis = plt.subplots(figsize=(5.6, max(4.8, 0.28 * len(field_order) + 1.5)))
    cmap = matplotlib.colors.ListedColormap([CROP_COLORS["Corn"], CROP_COLORS["Soybeans"], CROP_COLORS["Other"]])
    axis.imshow(matrix, aspect="auto", cmap=cmap, vmin=0, vmax=2)
    axis.set_xticks(range(len(years)))
    axis.set_xticklabels(years)
    axis.set_yticks(range(len(field_order)))
    state_by_field = history.drop_duplicates("field_id").set_index("field_id")["state"]
    axis.set_yticklabels(
        [
            f"{state_by_field.get(field_id, '')} {str(field_id).split('-')[-1][-6:]}"
            for field_id in field_order
        ],
        fontsize=6.5 if len(field_order) > 15 else 7.5,
    )
    text_colors = ("#3b2a00", "#ffffff", "#1b1b1b")
    text_size = 6.0 if len(field_order) > 15 else 7.0
    for row_index, field_id in enumerate(field_order):
        for column_index, year in enumerate(years):
            value = matrix[row_index, column_index]
            if np.isfinite(value):
                axis.text(
                    column_index,
                    row_index,
                    ["C", "S", "O"][int(value)],
                    ha="center",
                    va="center",
                    fontsize=text_size,
                    color=text_colors[int(value)],
                )
    state_boundaries: list[tuple[str, int]] = []
    current_state = None
    for index, field_id in enumerate(field_order):
        state = history.loc[history["field_id"] == field_id, "state"].iloc[0]
        if state != current_state:
            state_boundaries.append((state, index))
            current_state = state
    for state, index in state_boundaries[1:]:
        axis.axhline(index - 0.5, color="#111111", lw=1.2)
    axis.set_xlabel("CDL year")
    axis.set_title(
        "C1  Dominant crop by field-year\nC = corn, S = soybeans, O = other",
        pad=30,
    )
    handles = [Patch(facecolor=color, label=label) for label, color in CROP_COLORS.items()]
    axis.legend(
        handles=handles,
        fontsize=7.5,
        loc="lower center",
        ncol=3,
        bbox_to_anchor=(0.5, 1.0),
        frameon=False,
    )
    figure.tight_layout()
    outputs.figure(figure, "fig07_cdl_rotation_heatmap")

    # C2 - area-weighted composition + rotation diversity
    weighted = history.dropna(subset=["area_acres"]).copy()
    weighted["corn_area"] = weighted["corn_pct"] / 100.0 * weighted["area_acres"]
    weighted["soy_area"] = weighted["soy_pct"] / 100.0 * weighted["area_acres"]
    weighted["other_area"] = weighted["other_pct"] / 100.0 * weighted["area_acres"]
    composition = (
        weighted.groupby(["state", "year"])[["corn_area", "soy_area", "other_area"]].sum().reset_index()
    )
    row_totals = composition[["corn_area", "soy_area", "other_area"]].sum(axis=1).replace(0.0, np.nan)
    for column in ("corn_area", "soy_area", "other_area"):
        composition[column.replace("_area", "_share")] = (
            composition[column] / row_totals * 100.0
        )
    outputs.table("cdl_composition_by_state_year.csv", composition)

    figure, (left, right) = plt.subplots(1, 2, figsize=(9.4, 4.3), gridspec_kw={"width_ratios": [1.35, 1]})
    bar_width = 0.14
    for state_index, state in enumerate(ordered_states):
        subset = composition[composition["state"] == state].sort_values("year")
        if subset.empty:
            continue
        offsets = (np.arange(len(subset)) - (len(subset) - 1) / 2) * (bar_width * 1.15)
        x_positions = state_index + offsets
        bottom = np.zeros(len(subset))
        for column, label in (
            ("corn_share", "Corn"),
            ("soy_share", "Soybeans"),
            ("other_share", "Other"),
        ):
            values = subset[column].fillna(0.0).to_numpy()
            left.bar(
                x_positions,
                values,
                bottom=bottom,
                width=bar_width,
                color=CROP_COLORS[label],
                alpha=0.92,
                edgecolor="white",
                linewidth=0.3,
            )
            bottom += values
        for x_position, year in zip(x_positions, subset["year"]):
            left.text(
                x_position,
                102.0,
                str(int(year))[-2:],
                ha="center",
                va="bottom",
                fontsize=6,
                rotation=90,
                color="#444444",
            )
    left.set_xticks(range(len(ordered_states)))
    left.set_xticklabels(ordered_states)
    left.set_xlim(-0.6, max(len(ordered_states) - 0.4, 0.6))
    left.set_ylabel("Area-weighted crop share (%)")
    left.set_ylim(0, 114)
    left.set_title("C2a  Crop mix by year (labels = 2-digit year)")
    left.legend(
        handles=[Patch(facecolor=CROP_COLORS[label], label=label) for label in ("Corn", "Soybeans", "Other")],
        fontsize=7.5,
        loc="upper center",
        ncol=3,
        bbox_to_anchor=(0.5, -0.20),
    )

    sns.boxplot(
        data=rotations,
        x="state",
        y="rotation_diversity",
        order=ordered_states,
        hue="state",
        palette=palette,
        legend=False,
        width=0.45,
        fliersize=0,
        ax=right,
    )
    sns.stripplot(
        data=rotations,
        x="state",
        y="rotation_diversity",
        order=ordered_states,
        hue="state",
        palette=palette,
        legend=False,
        size=4.5,
        alpha=0.8,
        jitter=0.15,
        ax=right,
    )
    right.set_xlabel("")
    right.set_ylabel("Crop-mix Shannon diversity")
    right.set_title("C2b  Rotation diversity")
    figure.suptitle(f"C2  Crop composition and diversity, {min(years)}-{max(years)}", fontsize=10, fontweight="bold", y=1.02)
    figure.tight_layout()
    outputs.figure(figure, "fig08_cdl_composition_diversity")

    # C3 - transition matrices
    transitions = crop_transitions(history)
    transitions = transitions.merge(
        history[["field_id", "state"]].drop_duplicates("field_id"), on="field_id", how="left"
    )
    transition_counts = crop_transition_counts(transitions, group_columns=("state",))
    outputs.table("cdl_transitions.csv", transition_counts)
    crop_classes = ["Corn", "Soybeans", "Other"]
    figure, axes = plt.subplots(1, len(ordered_states), figsize=(3.5 * len(ordered_states), 3.6))
    axes = np.atleast_1d(axes)
    for axis, state in zip(axes, ordered_states):
        subset = transition_counts[transition_counts["state"] == state]
        matrix = np.zeros((3, 3))
        for _, row in subset.iterrows():
            if row["from_crop"] in crop_classes and row["to_crop"] in crop_classes:
                matrix[
                    crop_classes.index(row["from_crop"]), crop_classes.index(row["to_crop"])
                ] += row["count"]
        row_sums = matrix.sum(axis=1, keepdims=True)
        probabilities = np.divide(matrix, row_sums, out=np.zeros_like(matrix), where=row_sums > 0)
        axis.imshow(probabilities, cmap="Blues", vmin=0, vmax=1)
        for row_index in range(3):
            for column_index in range(3):
                axis.text(
                    column_index,
                    row_index,
                    f"{probabilities[row_index, column_index]:.2f}\n(n={int(matrix[row_index, column_index])})",
                    ha="center",
                    va="center",
                    fontsize=7.5,
                    color="white" if probabilities[row_index, column_index] > 0.55 else "#222222",
                )
        axis.set_xticks(range(3))
        axis.set_xticklabels(["Corn", "Soy", "Other"], fontsize=8)
        axis.set_yticks(range(3))
        axis.set_yticklabels(["Corn", "Soy", "Other"], fontsize=8)
        axis.set_xlabel("Next year")
        axis.set_ylabel("Current year")
        axis.set_title(state)
    figure.suptitle(
        "C3  Year-to-year crop transitions (row-normalised)\n"
        "diagonal = same crop repeated; off-diagonal = rotation",
        fontsize=10,
        fontweight="bold",
        y=1.05,
    )
    figure.tight_layout()
    outputs.figure(figure, "fig09_cdl_transitions")
    _log(args, f"cdl: {len(history)} field-years, {len(rotations)} rotations")


# ---------------------------------------------------------------------------
# Static map
# ---------------------------------------------------------------------------


def run_map(
    args: argparse.Namespace,
    outputs: Outputs,
    fields: gpd.GeoDataFrame,
    cdl: pd.DataFrame,
    states: list[str],
) -> None:
    history = crop_history_metrics(cdl)
    latest_year = int(history["year"].max())
    latest = history[history["year"] == latest_year][["field_id", "dominant_crop", "dominant_pct"]]
    mapped = fields.merge(latest, on="field_id", how="left")
    mapped["dominant_crop"] = mapped["dominant_crop"].fillna("Other")

    ordered_states = [state for state in states if state in set(mapped["state"])]
    projected_frames: dict[str, gpd.GeoDataFrame] = {}
    ratios: list[float] = []
    for state in ordered_states:
        subset = mapped[mapped["state"] == state].to_crs("EPSG:3857")
        projected_frames[state] = subset
        minx, miny, maxx, maxy = subset.total_bounds
        width = max(float(maxx - minx), 1.0)
        height = max(float(maxy - miny), 1.0)
        ratios.append(float(np.clip(width / height, 0.55, 2.0)))
    panel_height = 4.8
    panel_widths = [panel_height * ratio for ratio in ratios]
    figure, axes = plt.subplots(
        1,
        len(ordered_states),
        figsize=(max(sum(panel_widths), 4.4), panel_height + 1.0),
        gridspec_kw={"width_ratios": ratios},
    )
    axes = np.atleast_1d(axes)
    try:
        import contextily as ctx
    except ImportError:
        ctx = None
    basemap_used = False
    for axis, state in zip(axes, ordered_states):
        subset = projected_frames[state]
        subset.plot(
            ax=axis,
            color=[CROP_COLORS.get(crop, CROP_COLORS["Other"]) for crop in subset["dominant_crop"]],
            edgecolor="#1b1b1b",
            linewidth=0.8,
            alpha=0.92,
        )
        for _, row in subset.iterrows():
            point = row.geometry.representative_point()
            axis.annotate(
                str(row["field_id"]).split("-")[-1][-4:],
                (point.x, point.y),
                ha="center",
                va="center",
                fontsize=5.5,
                color="#111111",
                bbox=dict(boxstyle="round,pad=0.12", fc="white", ec="none", alpha=0.65),
            )
        if ctx is not None and not args.no_basemap:
            try:
                ctx.add_basemap(
                    axis,
                    source=ctx.providers.Esri.WorldImagery,
                    zoom="auto",
                    attribution=False,
                )
                basemap_used = True
            except Exception as exc:  # network or tile failures must not fail the run
                print(f"  warning: basemap unavailable for {state}: {exc}")
        minx, miny, maxx, maxy = subset.total_bounds
        pad_x = (maxx - minx) * 0.08 or 1000
        pad_y = (maxy - miny) * 0.08 or 1000
        axis.set_xlim(minx - pad_x, maxx + pad_x)
        axis.set_ylim(miny - pad_y, maxy + pad_y)
        axis.set_title(f"{state}  ({subset['farm'].iloc[0]})")
        axis.set_xticks([])
        axis.set_yticks([])
        axis.set_xlabel("")
        axis.set_ylabel("")
    handles = [Patch(facecolor=color, edgecolor="#1b1b1b", label=label) for label, color in CROP_COLORS.items()]
    figure.legend(handles=handles, loc="lower center", ncol=3, fontsize=8, bbox_to_anchor=(0.5, -0.01))
    if basemap_used:
        figure.text(
            0.995,
            0.005,
            "Basemap: Esri World Imagery",
            ha="right",
            va="bottom",
            fontsize=5.5,
            color="#555555",
        )
    figure.suptitle(
        f"Field boundaries and {latest_year} CDL dominant crop\n"
        "labels are the last four digits of each field ID",
        fontsize=11,
        fontweight="bold",
        y=1.0,
    )
    figure.tight_layout(rect=(0, 0.03, 1, 0.97))
    outputs.figure(figure, "fig10_map_fields_by_crop")
    _log(args, f"map: {len(mapped)} fields across {len(ordered_states)} states")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _write_manifest(
    outputs: Outputs,
    args: argparse.Namespace,
    fields: gpd.GeoDataFrame,
    weather: pd.DataFrame,
    cdl: pd.DataFrame,
    figures: list[str],
) -> None:
    inputs: dict[str, str] = {}
    for row in fields[["grower", "farm"]].drop_duplicates().itertuples(index=False):
        path = farm_boundary_path(row.grower, row.farm)
        if path.exists():
            inputs[str(path.relative_to(DATA_ROOT))] = _sha256(path)

    farms: list[dict[str, object]] = []
    for (grower, farm), group in fields.groupby(["grower", "farm"], dropna=False):
        state_values = group["state"].dropna().astype(str)
        county_values = group["county_name"].dropna().astype(str) if "county_name" in group else pd.Series(dtype=str)
        farms.append(
            {
                "grower": str(grower),
                "farm": str(farm),
                "state": state_values.mode().iloc[0] if not state_values.empty else "",
                "county": county_values.mode().iloc[0] if not county_values.empty else "",
                "fields": int(group["field_id"].nunique()),
                "acres": float(group.to_crs("EPSG:5070").geometry.area.sum() / 4046.8564224),
            }
        )

    manifest = {
        "analysis": "field-level-eda",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data_root": str(DATA_ROOT),
        "start_year": args.start_year,
        "end_year": args.end_year,
        "cdl_years": [int(year) for year in sorted(cdl["year"].unique().tolist())] if not cdl.empty else [],
        "growers": sorted(str(grower) for grower in fields["grower"].unique().tolist()),
        "states": sorted(str(state) for state in fields["state"].unique().tolist()),
        "farms": sorted(farms, key=lambda item: (str(item["state"]), str(item["farm"]))),
        "field_count": int(fields["field_id"].nunique()),
        "weather_rows": int(len(weather)),
        "cdl_rows": int(len(cdl)),
        "gdd_base_c": GDD_BASE_C,
        "season_months": [SEASON_START_MONTH, SEASON_END_MONTH],
        "seed": args.seed,
        "boundary_inputs_sha256": inputs,
        "outputs": sorted(outputs.generated),
        "figures": sorted(figures),
    }
    path = outputs.root / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    _style()
    cdl_years = (
        [int(part) for part in args.cdl_years.split(",") if part.strip()]
        if args.cdl_years
        else list(range(args.start_year, args.end_year + 1))
    )
    out_root = (
        Path(args.out).expanduser().resolve()
        if args.out
        else shared_eda_run_dir("field-level")
    )
    formats = [part.strip() for part in args.formats.split(",") if part.strip()]
    outputs = Outputs(out_root, formats, args.dpi)

    specs = _resolve_grower_specs(args)
    fields = load_fields(specs)
    states = _state_order(sorted(fields["state"].fillna("").unique().tolist()))

    _log(args, f"Loaded {len(fields)} fields from {len(specs)} grower(s): {', '.join(s[0] for s in specs)}")
    _log(args, f"Output: {out_root}")

    weather = load_weather(fields, args.start_year, args.end_year)
    cdl = load_cdl(fields, cdl_years)
    _log(args, f"Weather rows: {len(weather)}; CDL rows: {len(cdl)}")

    figures: list[str] = []
    expected_figures = {
        "boundaries": ["fig01_boundary_area_by_state", "fig02_boundary_shape_by_state", "fig03_boundary_perimeter_area"],
        "weather": ["fig04_weather_gs_precip", "fig05_weather_gdd", "fig06_weather_distance_decay"],
        "cdl": ["fig07_cdl_rotation_heatmap", "fig08_cdl_composition_diversity", "fig09_cdl_transitions"],
        "map": ["fig10_map_fields_by_crop"],
    }

    if args.command in ("all", "audit"):
        run_audit(args, outputs, fields, weather, cdl)
    if args.command in ("all", "boundaries"):
        run_boundaries(args, outputs, fields, states)
        figures.extend(expected_figures["boundaries"])
    if args.command in ("all", "weather"):
        run_weather(args, outputs, fields, weather, states)
        figures.extend(expected_figures["weather"])
    if args.command in ("all", "cdl"):
        run_cdl(args, outputs, fields, cdl, states)
        figures.extend(expected_figures["cdl"])
    if args.command in ("all", "map"):
        run_map(args, outputs, fields, cdl, states)
        figures.extend(expected_figures["map"])

    _write_manifest(outputs, args, fields, weather, cdl, figures)
    _log(args, "done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
