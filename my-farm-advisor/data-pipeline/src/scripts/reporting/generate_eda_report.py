#!/usr/bin/env python3
# ruff: noqa: E402
# pyright: reportMissingImports=false, reportAttributeAccessIssue=false, reportCallIssue=false
"""Build a self-contained HTML report from a field-level EDA output directory.

This script is deliberately separate from the reusable EDA analysis command:
the analysis writes manifest + tables + figures, and this reporting step turns
that bundle into one static, print-friendly HTML page with base64 images. No
dashboard server, no external assets, and no writes outside the EDA output
directory.

Run from the installed runtime source copy:

    python scripts/reporting/generate_eda_report.py \
        --eda-dir /path/to/data-pipeline/shared/eda/field-level
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

STATE_ORDER = ("IL", "IA", "NE")
STATE_NAMES = {"IL": "Illinois", "IA": "Iowa", "NE": "Nebraska"}
FIGURE_CAPTIONS = {
    "fig01_boundary_area_by_state": (
        "A1 - Field size distribution",
        "Box-and-strip plot of field area on a log scale. Each point is one field "
        "outline; the box shows the interquartile range and the line the median.",
    ),
    "fig02_boundary_shape_by_state": (
        "A2 - Field shape signature",
        "Left: empirical cumulative distribution of Polsby-Popper compactness "
        "(1.0 is a circle, 0.785 a square). Right: share of fields classified as "
        "pivot-like, rectangular, or irregular.",
    ),
    "fig03_boundary_perimeter_area": (
        "A3 - Perimeter-area scaling",
        "Log-log perimeter-area scatter with per-state least-squares fits. A "
        "self-similar outline scales with slope 0.5; steeper slopes indicate "
        "elongation or irregular edges. Spearman rho uses a permutation test.",
    ),
    "fig04_weather_gs_precip": (
        "B1 - Growing-season water supply",
        "April-October precipitation for every field-year. Thin lines are "
        "individual fields; the black line is the state median. Lines coincide "
        "exactly where fields share a NASA POWER grid cell.",
    ),
    "fig05_weather_gdd": (
        "B2 - Heat accumulation",
        "Cumulative growing degree days (base 10 C, 30 C cap) by day of year. "
        "Lines are state medians across fields; bands are interquartile ranges.",
    ),
    "fig06_weather_distance_decay": (
        "B3 - Spatial coherence of weather",
        "Spearman correlation of daily mean temperature against distance between "
        "field centroids. Red points are pairs inside the same farm; grey points "
        "cross farm and state boundaries. The black line is the binned median. "
        "Fields inside one farm share a NASA POWER cell, so same-farm pairs sit "
        "at rho close to 1.0; the informative gradient is between states.",
    ),
    "fig07_cdl_rotation_heatmap": (
        "C1 - Dominant crop by field-year",
        "USDA CDL dominant crop for every field-year. C = corn, S = soybeans, "
        "O = other. Horizontal rules separate states and labels show state + field suffix.",
    ),
    "fig08_cdl_composition_diversity": (
        "C2 - Crop composition and rotation diversity",
        "Left: area-weighted crop mix for each state-year (labels are two-digit "
        "years). Right: Shannon diversity of each field's multi-year crop mix.",
    ),
    "fig09_cdl_transitions": (
        "C3 - Year-to-year crop transitions",
        "Row-normalised transition matrices. Diagonal cells repeat the same crop; "
        "off-diagonal cells are rotations. Cell text shows probability and count.",
    ),
    "fig10_map_fields_by_crop": (
        "D - Field boundaries and latest CDL crop",
        "Static map of real field polygons coloured by the latest available CDL "
        "dominant crop. Labels are the last four digits of each field ID.",
    ),
}


def _preparse_root(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--root", default=None)
    known, _ = parser.parse_known_args(argv)
    if known.root:
        os.environ["DATA_PIPELINE_DATA_ROOT"] = str(Path(known.root).expanduser().resolve())


_preparse_root(sys.argv[1:])

import pandas as pd

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR / "lib"))

from paths import shared_eda_run_dir  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=None, help="Runtime data root")
    parser.add_argument(
        "--eda-dir",
        default=None,
        help="EDA output directory containing manifest.json, tables/, figures/. "
        "Defaults to shared/eda/field-level.",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output HTML path. Defaults to <eda-dir>/report/field_level_eda_report.html.",
    )
    parser.add_argument("--title", default="Field-Level Exploratory Data Analysis")
    return parser.parse_args(argv)


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _read_table(eda_dir: Path, name: str) -> pd.DataFrame:
    path = eda_dir / "tables" / name
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def _image_data_uri(path: Path) -> str:
    payload = path.read_bytes()
    mime = "image/png"
    # Figures are written at print resolution. Downscale wide images before
    # embedding so the single HTML page stays a reviewable size while the PNGs
    # on disk keep their full resolution. Raster basemaps do not compress well
    # as PNG, so oversized payloads fall back to a high-quality JPEG.
    try:
        import io

        from PIL import Image

        with Image.open(path) as image:
            if image.width > 1600:
                height = max(1, round(image.height * 1600 / image.width))
                resized = image.resize((1600, height), Image.LANCZOS)
                buffer = io.BytesIO()
                resized.save(buffer, format="PNG", optimize=True)
                payload = buffer.getvalue()
                if len(payload) > 700_000:
                    buffer = io.BytesIO()
                    if resized.mode not in ("RGB", "L"):
                        resized = resized.convert("RGB")
                    resized.save(buffer, format="JPEG", quality=88, optimize=True)
                    payload = buffer.getvalue()
                    mime = "image/jpeg"
    except Exception:
        payload = path.read_bytes()
        mime = "image/png"
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _format_number(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if isinstance(value, (int,)) and not isinstance(value, bool):
        return f"{value:,}"
    if isinstance(value, float):
        if abs(value) >= 1000:
            return f"{value:,.0f}"
        if abs(value) >= 10:
            return f"{value:,.1f}"
        return f"{value:,.2f}"
    return html.escape(str(value))


def _table_html(
    frame: pd.DataFrame,
    *,
    caption: str,
    max_rows: int = 40,
    numeric_columns: set[str] | None = None,
) -> str:
    if frame.empty:
        return f'<p class="muted">Table unavailable: {html.escape(caption)}.</p>'
    subset = frame.head(max_rows).copy()
    numeric_columns = numeric_columns or set()
    head = "".join(f"<th>{html.escape(str(column))}</th>" for column in subset.columns)
    body_rows = []
    for _, row in subset.iterrows():
        cells = []
        for column in subset.columns:
            value = row[column]
            if column in numeric_columns:
                text = _format_number(value)
            else:
                text = html.escape("" if pd.isna(value) else str(value))
            cells.append(f"<td>{text}</td>")
        body_rows.append("<tr>" + "".join(cells) + "</tr>")
    note = ""
    if len(frame) > max_rows:
        note = f'<p class="muted">Showing {max_rows} of {len(frame)} rows. Full table in <code>tables/</code>.</p>'
    return (
        f'<div class="table-wrap"><table><caption>{html.escape(caption)}</caption>'
        f"<thead><tr>{head}</tr></thead><tbody>{''.join(body_rows)}</tbody></table></div>"
        f"{note}"
    )


def _figure_html(eda_dir: Path, stem: str) -> str:
    png_path = eda_dir / "figures" / f"{stem}.png"
    if not png_path.exists():
        return f'<p class="muted">Figure unavailable: {html.escape(stem)}.</p>'
    title, caption = FIGURE_CAPTIONS.get(stem, (stem, ""))
    uri = _image_data_uri(png_path)
    return (
        f'<figure id="{html.escape(stem)}">'
        f'<img src="{uri}" alt="{html.escape(title)}">'
        f"<figcaption><strong>{html.escape(title)}.</strong> {html.escape(caption)}</figcaption>"
        f"</figure>"
    )


def _state_sort(states: list[str]) -> list[str]:
    unique = [state for state in states if state]
    return [state for state in STATE_ORDER if state in unique] + sorted(
        state for state in unique if state not in STATE_ORDER
    )


def _state_label(state: str) -> str:
    return f"{state} ({STATE_NAMES[state]})" if state in STATE_NAMES else state


def _headline_findings(
    manifest: dict,
    boundary_stats: pd.DataFrame,
    weather_stats: pd.DataFrame,
    cdl_stats: pd.DataFrame,
    composition: pd.DataFrame,
    rotations: pd.DataFrame | None = None,
) -> list[str]:
    findings: list[str] = []
    field_count = manifest.get("field_count", "?")
    states = _state_sort(list(manifest.get("states", [])))
    start_year = manifest.get("start_year", "?")
    end_year = manifest.get("end_year", "?")

    if states:
        state_count = len(states)
        grower_count = len(manifest.get("growers", []))
        findings.append(
            f"<strong>Scope.</strong> {field_count} fields across "
            f"{state_count} state{'s' if state_count != 1 else ''} "
            f"({', '.join(states)}) and {grower_count} "
            f"grower{'s' if grower_count != 1 else ''}, "
            f"with weather and CDL history for {start_year}-{end_year}."
        )

    if not boundary_stats.empty:
        parts = []
        for _, row in boundary_stats.iterrows():
            state = str(row.get("state", ""))
            parts.append(
                f"{_state_label(state)} median {row['area_acres_median']:.0f} ac "
                f"(n={int(row['n_fields'])})"
            )
        shape_parts = []
        for _, row in boundary_stats.iterrows():
            state = str(row.get("state", ""))
            shape_parts.append(
                f"{state}: {row['rectangular_share'] * 100:.0f}% rectangular, "
                f"{row['pivot_like_share'] * 100:.0f}% pivot-like, "
                f"{row['irregular_share'] * 100:.0f}% irregular"
            )
        findings.append(
            "<strong>Field geometry.</strong> " + "; ".join(parts) + ". Shape mix: "
            + "; ".join(shape_parts) + "."
        )

    if not weather_stats.empty:
        precip = weather_stats.groupby("state")["precip_mm_median"].median()
        gdd = weather_stats.groupby("state")["gdd_median"].median()
        parts = [
            f"{_state_label(str(state))} {precip[state]:.0f} mm and {gdd[state]:.0f} GDD"
            for state in precip.index
        ]
        findings.append(
            "<strong>Weather.</strong> Median growing-season totals across years: "
            + "; ".join(parts)
            + ". Fields inside a farm share a NASA POWER cell, so B3 puts same-farm "
            "pairs at rho close to 1.0 and the useful signal is the between-state "
            "gradient."
        )

    if not cdl_stats.empty:
        parts = []
        for _, row in cdl_stats.iterrows():
            state = str(row.get("state", ""))
            parts.append(
                f"{state}: {row['mean_corn_share_years'] * 100:.0f}% corn-years, "
                f"{row['mean_soy_share_years'] * 100:.0f}% soybean-years, "
                f"{row['continuous_corn_share'] * 100:.0f}% continuous corn"
            )
        findings.append("<strong>Crop history.</strong> " + "; ".join(parts) + ".")

    if not composition.empty and "corn_share" in composition.columns:
        latest = composition.sort_values("year").groupby("state").tail(1)
        parts = [
            f"{_state_label(str(row['state']))} {row['year']:.0f}: "
            f"{row['corn_share']:.0f}% corn / {row['soy_share']:.0f}% soybean"
            for _, row in latest.sort_values("state").iterrows()
        ]
        findings.append("<strong>Latest crop mix.</strong> " + "; ".join(parts) + ".")

    if rotations is not None and not rotations.empty and "rotation_sequence" in rotations:
        non_crop = []
        for _, row in rotations.iterrows():
            sequence = str(row.get("rotation_sequence", "") or "")
            if sequence and sequence.count("O") * 2 > len(sequence):
                non_crop.append(f"{row.get('state', '')} {row.get('field_id', '')} ({sequence})")
        if non_crop:
            verb = "is" if len(non_crop) == 1 else "are"
            noun = "field" if len(non_crop) == 1 else "fields"
            findings.append(
                "<strong>Boundary vs CDL.</strong> "
                + ", ".join(non_crop)
                + f" {verb} tagged as farmland in OpenStreetMap but CDL reports mostly "
                f"non-crop cover. {'It is' if len(non_crop) == 1 else 'They are'} kept as "
                "Other instead of silently dropped, so the crop statistics show the "
                "disagreement between the boundary source and the satellite classification."
            )

    findings.append(
        "<strong>Interpretation.</strong> These are descriptive comparisons of "
        "real field boundaries and public data. They do not establish causal "
        "effects, and field-level CDL mixes can include non-crop pixels at edges."
    )
    return findings


def build_report(
    eda_dir: Path,
    title: str,
    manifest: dict,
    tables: dict[str, pd.DataFrame],
) -> str:
    boundary_stats = tables.get("boundary_stats_by_state", pd.DataFrame())
    boundary_correlations = tables.get("boundary_correlations", pd.DataFrame())
    weather_stats = tables.get("weather_stats_by_state", pd.DataFrame())
    cdl_stats = tables.get("cdl_stats_by_state", pd.DataFrame())
    composition = tables.get("cdl_composition_by_state_year", pd.DataFrame())
    transitions = tables.get("cdl_transitions", pd.DataFrame())
    rotations = tables.get("cdl_rotation_summary", pd.DataFrame())
    audit = tables.get("data_audit", pd.DataFrame())

    states = _state_sort(list(manifest.get("states", [])))
    findings = _headline_findings(
        manifest, boundary_stats, weather_stats, cdl_stats, composition, rotations
    )
    figures = manifest.get("figures", [])
    figure_order = [
        "fig01_boundary_area_by_state",
        "fig02_boundary_shape_by_state",
        "fig03_boundary_perimeter_area",
        "fig04_weather_gs_precip",
        "fig05_weather_gdd",
        "fig06_weather_distance_decay",
        "fig07_cdl_rotation_heatmap",
        "fig08_cdl_composition_diversity",
        "fig09_cdl_transitions",
        "fig10_map_fields_by_crop",
    ]
    present_figures = [stem for stem in figure_order if stem in figures]

    def section_figures(stems: list[str]) -> str:
        return "\n".join(_figure_html(eda_dir, stem) for stem in stems)

    generated = manifest.get("generated_at", "")
    try:
        generated_display = datetime.fromisoformat(generated).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        generated_display = generated

    scope_rows = [
        ("Analysis", manifest.get("analysis", "field-level-eda")),
        ("Generated", generated_display),
        ("Data root", manifest.get("data_root", "")),
        ("Growers", ", ".join(manifest.get("growers", []))),
        ("States", ", ".join(states)),
        ("Fields", str(manifest.get("field_count", ""))),
        ("Weather years", f"{manifest.get('start_year', '')}-{manifest.get('end_year', '')}"),
        ("CDL years", ", ".join(str(year) for year in manifest.get("cdl_years", []))),
        ("Season", "-".join(str(month) for month in manifest.get("season_months", []))),
        ("GDD base", f"{manifest.get('gdd_base_c', 10)} C, 30 C cap"),
    ]
    scope_html = "".join(
        f"<tr><th>{html.escape(label)}</th><td>{html.escape(str(value))}</td></tr>"
        for label, value in scope_rows
    )
    farm_frame = pd.DataFrame(manifest.get("farms", []))
    ne_farm = next(
        (farm for farm in manifest.get("farms", []) if str(farm.get("state")) == "NE"),
        None,
    )
    ne_note = ""
    if ne_farm:
        ne_note = (
            '<div class="note"><strong>Nebraska site selection.</strong> '
            f"{html.escape(str(ne_farm.get('county', 'Phelps')))} County was selected because it "
            "combines substantial corn and soybean production with extensive center-pivot "
            "irrigation (USDA Census of Agriculture and NRCS irrigation data). The sampled "
            "farm includes pivot-shaped fields alongside rectangular quarters, so the shape "
            "and crop-history comparisons include irrigated and dryland patterns.</div>"
        )

    numeric_boundary = {
        "n_fields", "area_acres_median", "area_acres_iqr", "area_acres_min", "area_acres_max",
        "compactness_median", "rect_fill_median", "aspect_median", "pivot_like_share",
        "rectangular_share", "irregular_share",
    }
    numeric_weather = {
        "n_fields", "precip_mm_median", "precip_mm_min", "precip_mm_max", "gdd_median",
        "heat_days_35c_median", "frost_free_days_median", "dry_spell_days_median",
    }
    numeric_cdl = {
        "n_fields", "continuous_corn_share", "mean_crop_changes", "mean_rotation_diversity",
        "mean_n_distinct_crops", "mean_corn_share_years", "mean_soy_share_years",
        "mean_other_share_years",
    }

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root {{ color-scheme: light; }}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; padding: 2.5rem 1.25rem 4rem;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  color: #1d2721; background: #f6f7f4; line-height: 1.55;
}}
main {{ max-width: 1080px; margin: 0 auto; background: #ffffff; padding: 2.5rem 2.75rem 3rem;
  border: 1px solid #dfe4dc; border-radius: 10px; }}
header {{ border-bottom: 3px solid #2f6b4f; padding-bottom: 1.25rem; margin-bottom: 1.75rem; }}
h1 {{ margin: 0 0 0.35rem; font-size: 1.9rem; line-height: 1.2; color: #173d2b; }}
h2 {{ margin: 2.4rem 0 0.75rem; font-size: 1.35rem; color: #173d2b; border-bottom: 1px solid #dfe4dc;
  padding-bottom: 0.35rem; }}
h3 {{ margin: 1.6rem 0 0.5rem; font-size: 1.05rem; color: #2f6b4f; }}
p, li {{ font-size: 0.95rem; }}
.subtitle {{ color: #52605a; margin: 0; }}
ul.findings {{ padding-left: 1.2rem; }}
ul.findings li {{ margin: 0.45rem 0; }}
figure {{ margin: 1.25rem 0 1.75rem; padding: 0; }}
figure img {{ width: 100%; height: auto; display: block; border: 1px solid #dfe4dc; border-radius: 6px;
  background: #ffffff; }}
figcaption {{ font-size: 0.82rem; color: #52605a; margin-top: 0.55rem; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(310px, 1fr)); gap: 1.5rem; }}
.table-wrap {{ overflow-x: auto; margin: 1rem 0 1.5rem; }}
table {{ border-collapse: collapse; width: 100%; font-size: 0.8rem; }}
caption {{ text-align: left; font-weight: 700; font-size: 0.86rem; padding-bottom: 0.4rem; color: #173d2b; }}
th, td {{ border-bottom: 1px solid #e6eae4; padding: 0.38rem 0.5rem; text-align: left; vertical-align: top; }}
thead th {{ background: #eef4ef; color: #173d2b; font-weight: 600; white-space: nowrap; }}
tbody tr:nth-child(even) {{ background: #fafbf9; }}
.scope th {{ width: 30%; background: #f6f8f5; }}
code {{ background: #eef1ed; padding: 0.08rem 0.28rem; border-radius: 3px; font-size: 0.85em; }}
pre {{ background: #f2f5f1; border: 1px solid #dfe4dc; border-radius: 6px; padding: 0.8rem 1rem;
  overflow-x: auto; font-size: 0.8rem; }}
.note {{ border-left: 4px solid #2f6b4f; background: #f2f8f3; padding: 0.75rem 1rem; margin: 1.25rem 0;
  font-size: 0.9rem; }}
.muted {{ color: #6b7770; font-size: 0.82rem; }}
footer {{ margin-top: 2.5rem; padding-top: 1rem; border-top: 1px solid #dfe4dc; color: #6b7770;
  font-size: 0.78rem; }}
@media print {{
  body {{ background: #ffffff; padding: 0; }}
  main {{ border: none; border-radius: 0; padding: 0; max-width: none; }}
  figure {{ break-inside: avoid; }}
  table {{ font-size: 0.72rem; }}
}}
</style>
</head>
<body>
<main>
<header>
  <h1>{html.escape(title)}</h1>
  <p class="subtitle">Field boundaries, historical weather, and USDA CDL crop history across
  {html.escape(', '.join(states))} &middot; generated {html.escape(generated_display)}</p>
</header>

<h2>Scope and data</h2>
<div class="table-wrap"><table class="scope"><tbody>{scope_html}</tbody></table></div>
{_table_html(farm_frame, caption="Farm coverage", numeric_columns={"fields", "acres"})}
{ne_note}
<div class="note">All figures and tables in this page are generated from real grower field
boundaries in the runtime data root. The analysis is read-only and does not download data.</div>

<h2>Key findings</h2>
<ul class="findings">
{''.join(f'<li>{finding}</li>' for finding in findings)}
</ul>

<h2>Methods</h2>
<ul>
  <li><strong>Boundaries.</strong> Areas, perimeters, and shapes are computed in an equal-area
  projected CRS (EPSG:5070). Polsby-Popper compactness is <code>4&pi;A/P&sup2;</code>;
  rectangle fill is polygon area divided by minimum rotated rectangle area.</li>
  <li><strong>Weather.</strong> Daily NASA POWER series are sampled at each field centroid.
  Growing degree days use base 10 &deg;C and a 30 &deg;C upper cap. The growing season is
  April-October; frost dates use daily minimum temperature &le; 0 &deg;C.</li>
  <li><strong>Crop history.</strong> USDA CDL pixels are summarised per field-year and collapsed
  to a dominant crop. Rotation diversity is the Shannon entropy of a field's mean crop shares;
  transitions count year-to-year dominant-crop changes.</li>
  <li><strong>Statistics.</strong> Correlations are Spearman rho with permutation p-values;
  group medians are descriptive and not adjusted for multiple comparisons.</li>
</ul>

<h2>Comparison scales</h2>
<div class="table-wrap"><table>
<caption>How the static outputs answer each comparison</caption>
<thead><tr><th>Scale</th><th>Question</th><th>Outputs</th></tr></thead>
<tbody>
<tr><td>Within a field, across years</td><td>How does crop rotation and seasonal weather vary on the same field?</td><td>C1 heatmap, C2b diversity, <code>cdl_rotation_summary.csv</code>, <code>weather_seasonal_metrics.csv</code></td></tr>
<tr><td>Across fields in a grower</td><td>How much do field size, shape, and local weather differ inside one farm?</td><td>A1-A3, B3 distance decay, <code>field_boundary_metrics.csv</code>, <code>weather_pairwise_correlations.csv</code></td></tr>
<tr><td>Across states</td><td>How do Illinois, Iowa, and Nebraska compare on geometry, water supply, heat, and crop mix?</td><td>A1-A3, B1-B2, C2a, C3, all <code>*_by_state.csv</code> tables</td></tr>
</tbody></table></div>

<h2>A. Field boundaries</h2>
{section_figures([stem for stem in present_figures if stem in ("fig01_boundary_area_by_state", "fig02_boundary_shape_by_state", "fig03_boundary_perimeter_area")])}
{_table_html(boundary_stats, caption="Boundary statistics by state", numeric_columns=numeric_boundary)}
{_table_html(boundary_correlations, caption="Perimeter-area correlation and scaling by state", numeric_columns={"n", "spearman_rho", "p_permutation", "ci_low", "ci_high", "loglog_slope", "expected_self_similar_slope"})}

<h2>B. Historical weather</h2>
{section_figures([stem for stem in present_figures if stem in ("fig04_weather_gs_precip", "fig05_weather_gdd", "fig06_weather_distance_decay")])}
{_table_html(weather_stats, caption="Growing-season weather by state and year", numeric_columns=numeric_weather)}

<h2>C. USDA CDL crop history</h2>
{section_figures([stem for stem in present_figures if stem in ("fig07_cdl_rotation_heatmap", "fig08_cdl_composition_diversity", "fig09_cdl_transitions")])}
{_table_html(cdl_stats, caption="Rotation statistics by state", numeric_columns=numeric_cdl)}
{_table_html(composition, caption="Area-weighted crop composition by state-year", numeric_columns={"corn_area", "soy_area", "other_area", "corn_share", "soy_share", "other_share"})}
{_table_html(transitions, caption="Year-to-year crop transitions", numeric_columns={"count", "probability"})}

<h2>D. Static geospatial comparison</h2>
{section_figures([stem for stem in present_figures if stem == "fig10_map_fields_by_crop"])}

<h2>Data audit</h2>
{_table_html(audit, caption="Input coverage and completeness checks")}

<h2>Reproducibility</h2>
<p>The analysis is reproducible from the committed scripts and the runtime data root:</p>
<pre>python scripts/eda/eda_field_level.py --command all \\
  --grower illinois-grower --grower iowa-grower --grower nebraska-grower \\
  --start-year {manifest.get('start_year', 2021)} --end-year {manifest.get('end_year', 2025)} \\
  --cdl-years {','.join(str(year) for year in manifest.get('cdl_years', []))} --seed {manifest.get('seed', 42)}

python scripts/reporting/generate_eda_report.py --eda-dir {html.escape(str(eda_dir))}</pre>
<p class="muted">Boundary input SHA-256 values are recorded in <code>manifest.json</code>, along
with every generated table and figure path.</p>
<p class="muted">Run seed: {manifest.get('seed', '')}. Weather rows: {manifest.get('weather_rows', '')};
CDL rows: {manifest.get('cdl_rows', '')}.</p>

<h2>Limitations</h2>
<ul>
  <li>Field boundaries come from OpenStreetMap and are not legal survey boundaries.</li>
  <li>CDL is a 30 m classification; field edges mix crops, roads, and non-crop pixels.</li>
  <li>NASA POWER grid cells are coarser than fields, so nearby fields can share weather values.</li>
  <li>Because every field in a farm falls inside one NASA POWER cell, within-farm temperature
  correlations are effectively 1.0 by construction; B3 therefore measures the gradient between
  states, not within-farm microclimate.</li>
  <li>Ten fields per state support descriptive comparison, not statewide inference.</li>
  <li>This report contains no soil analysis and no interactive dashboard, by design.</li>
</ul>

<footer>
  Generated {html.escape(datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))} from
  <code>{html.escape(str(eda_dir))}</code>.
</footer>
</main>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    eda_dir = (
        Path(args.eda_dir).expanduser().resolve()
        if args.eda_dir
        else shared_eda_run_dir("field-level")
    )
    manifest = _read_json(eda_dir / "manifest.json")
    if not manifest:
        raise SystemExit(f"manifest.json not found in {eda_dir}")
    tables = {
        name: _read_table(eda_dir, f"{name}.csv")
        for name in (
            "boundary_stats_by_state",
            "boundary_correlations",
            "weather_stats_by_state",
            "cdl_stats_by_state",
            "cdl_composition_by_state_year",
            "cdl_transitions",
            "cdl_rotation_summary",
            "data_audit",
        )
    }
    document = build_report(eda_dir, args.title, manifest, tables)
    out_path = (
        Path(args.out).expanduser().resolve()
        if args.out
        else eda_dir / "report" / "field_level_eda_report.html"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(document, encoding="utf-8")
    print(f"Wrote {out_path} ({out_path.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
