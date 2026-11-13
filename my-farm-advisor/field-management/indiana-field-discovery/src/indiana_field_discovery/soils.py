"""Phase 2: SSURGO soil map units and properties for every field via NRCS SDA.

Uses the public Soil Data Access tabular REST API (no key). Per-field map unit
membership comes from ``SDA_Get_Mukey_from_intersection_with_WktWgs84`` so a
field's centroid is never used as a stand-in for its soils.

Limitation recorded in the outputs: the tabular API returns *membership* but not
map unit polygon area, so field-level property averages are component-weighted
within map units and equally weighted across map units. Map unit ids, symbols
and names are retained per field so a future area-weighted pass can be run
without re-discovering fields.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests

from .paths import farm_paths

SDA_URL = "https://sdmdataaccess.sc.egov.usda.gov/Tabular/post.rest"
BATCH_FIELDS = 20
MUKEY_CHUNK = 150

MAP_UNIT_COLUMNS = ["field_id", "mukey", "musym", "muname"]
ATTRIBUTE_COLUMNS = [
    "mukey", "cokey", "musym", "muname", "drclassdcd", "hydgrpdcd", "niccdcd", "iccdcd",
    "slopegradwta", "flodfreqdcd", "wtdepannmin", "aws025wta", "aws050wta",
    "aws0100wta", "compname", "comppct_r", "drainagecl", "hzdept_r", "hzdepb_r",
    "om_r", "cec7_r", "awc_r", "ph1to1h2o_r", "claytotal_r", "sandtotal_r",
    "silttotal_r", "dbthirdbar_r", "kwfact",
]


def _post(sql: str, timeout: int = 180) -> list[list]:
    response = requests.post(SDA_URL, data={"query": sql, "format": "JSON"}, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if "Table" not in payload:
        message = payload.get("ServiceException") or payload
        raise RuntimeError(f"SDA error: {str(message)[:400]}")
    return payload["Table"]


def _map_unit_query(field_wkts: list[tuple[str, str]]) -> str:
    parts = []
    for field_id, wkt in field_wkts:
        parts.append(
            f"SELECT '{field_id}' AS field_id, mu.mukey, mu.musym, mu.muname "
            f"FROM mapunit mu WHERE mu.mukey IN (SELECT * FROM "
            f"SDA_Get_Mukey_from_intersection_with_WktWgs84('{wkt}'))"
        )
    return " UNION ALL ".join(parts)


def _attribute_query(mukeys: list[str]) -> str:
    quoted = ",".join(f"'{m}'" for m in mukeys)
    return (
        "SELECT mu.mukey, mu.musym, mu.muname, a.drclassdcd, a.hydgrpdcd, a.niccdcd, "
        "a.iccdcd, a.slopegradwta, a.flodfreqdcd, a.wtdepannmin, a.aws025wta, "
        "a.aws050wta, a.aws0100wta, c.compname, c.comppct_r, c.cokey, c.drainagecl, "
        "ch.hzdept_r, ch.hzdepb_r, ch.om_r, ch.cec7_r, ch.awc_r, ch.ph1to1h2o_r, "
        "ch.claytotal_r, ch.sandtotal_r, ch.silttotal_r, ch.dbthirdbar_r, ch.kwfact "
        "FROM mapunit mu "
        "LEFT JOIN muaggatt a ON a.mukey = mu.mukey "
        "LEFT JOIN component c ON c.mukey = mu.mukey "
        "LEFT JOIN chorizon ch ON ch.cokey = c.cokey "
        f"WHERE mu.mukey IN ({quoted}) "
        "ORDER BY mu.mukey, c.comppct_r DESC, ch.hzdept_r"
    )


def _cached_batch(path: Path, sql: str, label: str) -> list[list]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    last: Exception | None = None
    for attempt in range(4):
        try:
            rows = _post(sql)
            path.write_text(json.dumps(rows), encoding="utf-8")
            return rows
        except Exception as exc:  # noqa: BLE001 - transient SDA failures
            last = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{label} failed after retries: {last}")


def build_soils(args: argparse.Namespace) -> int:
    t0 = time.time()
    paths = farm_paths(args.grower, args.farm).ensure()
    boundary = paths.boundary_dir / "field_boundaries.parquet"
    if not boundary.exists():
        raise SystemExit(f"missing {boundary}; run county-fields first")

    fields = gpd.read_parquet(boundary)
    if args.crop_only:
        fields = fields[fields["ag_class"] == "crop"].copy()
    fields = fields[~fields.geometry.is_empty].copy()
    fields["wkt"] = fields.to_crs(4326).geometry.apply(lambda g: g.wkt)
    print(f"[soil] {len(fields)} fields")

    patch_dir = paths.state_dir / "soil" / "map_units"
    patch_dir.mkdir(parents=True, exist_ok=True)
    pairs = [(fid, wkt) for fid, wkt in zip(fields["field_id"], fields["wkt"])]
    batches = [pairs[i:i + BATCH_FIELDS] for i in range(0, len(pairs), BATCH_FIELDS)]

    map_unit_rows: list[list] = []
    for index, batch in enumerate(batches):
        path = patch_dir / f"map_units_{index:04d}.json"
        rows = _cached_batch(path, _map_unit_query(batch), f"map units batch {index}")
        map_unit_rows.extend(rows)
        if index % 20 == 0:
            print(f"[soil] map units {index + 1}/{len(batches)} batches")

    mu_df = pd.DataFrame(map_unit_rows, columns=MAP_UNIT_COLUMNS)
    mu_df = mu_df.drop_duplicates(subset=["field_id", "mukey"])
    print(f"[soil] {len(mu_df)} field/map-unit pairs, {mu_df['mukey'].nunique()} unique map units")

    mukeys = sorted(mu_df["mukey"].astype(str).unique())
    attr_dir = paths.state_dir / "soil" / "attributes"
    attr_dir.mkdir(parents=True, exist_ok=True)
    attr_rows: list[list] = []
    chunks = [mukeys[i:i + MUKEY_CHUNK] for i in range(0, len(mukeys), MUKEY_CHUNK)]
    for index, chunk in enumerate(chunks):
        path = attr_dir / f"attributes_{index:04d}.json"
        attr_rows.extend(_cached_batch(path, _attribute_query(chunk), f"attributes {index}"))
        print(f"[soil] attributes {index + 1}/{len(chunks)} chunks")

    attr = pd.DataFrame(attr_rows, columns=ATTRIBUTE_COLUMNS)
    id_columns = {"mukey", "cokey", "musym", "muname", "compname", "drclassdcd",
                  "hydgrpdcd", "flodfreqdcd", "drainagecl"}
    for column in ATTRIBUTE_COLUMNS:
        if column not in id_columns:
            attr[column] = pd.to_numeric(attr[column], errors="coerce")

    # Topsoil (0-30 cm) thickness-weighted component mean.
    # Each horizon contributes only the part of its thickness that falls inside 0-30 cm;
    # components are then weighted by their component percentage within the map unit.
    numeric = ["om_r", "cec7_r", "awc_r", "ph1to1h2o_r", "claytotal_r",
               "sandtotal_r", "silttotal_r", "dbthirdbar_r", "kwfact"]
    attr["comppct_r"] = pd.to_numeric(attr["comppct_r"], errors="coerce").fillna(0)
    top_depth = attr["hzdept_r"].clip(lower=0)
    bottom = attr["hzdepb_r"].fillna(np.where(top_depth < 30, 30.0, top_depth))
    thickness = (np.minimum(bottom, 30.0) - top_depth).clip(lower=0).fillna(0)
    attr["_thick30"] = thickness
    has_horizon = attr["cokey"].notna() & (thickness > 0)

    comp = attr[has_horizon].copy()
    comp_den = comp["_thick30"]
    agg_spec: dict = {"comppct": ("comppct_r", "max"), "_t": ("_thick30", "sum")}
    for column in numeric:
        comp[f"_num_{column}"] = comp[column] * comp_den
        comp[f"_den_{column}"] = np.where(comp[column].notna(), comp_den, 0.0)
        agg_spec[f"_num_{column}"] = (f"_num_{column}", "sum")
        agg_spec[f"_den_{column}"] = (f"_den_{column}", "sum")
    comp = comp.groupby(["mukey", "cokey"], as_index=False).agg(**agg_spec)
    for column in numeric:
        den = comp[f"_den_{column}"].replace(0, np.nan)
        comp[f"v_{column}"] = comp[f"_num_{column}"] / den

    weight = comp["comppct"].fillna(0)
    comp["_w"] = weight
    for column in numeric:
        comp[f"_wn_{column}"] = comp[f"v_{column}"] * weight
    mu_props = comp.groupby("mukey").agg(
        **{f"top_{c}": (f"_wn_{c}", "sum") for c in numeric},
        comp_pct_total=("comppct", "sum"),
        _wtotal=("_w", "sum"),
    ).reset_index()
    for column in numeric:
        mu_props[f"top_{column}"] = mu_props[f"top_{column}"] / mu_props["_wtotal"].replace(0, np.nan)
    properties = mu_props.drop(columns=["_wtotal"])

    mu_meta = attr.drop_duplicates("mukey")[[
        "mukey", "drclassdcd", "hydgrpdcd", "niccdcd",
        "iccdcd", "slopegradwta", "flodfreqdcd", "wtdepannmin",
        "aws025wta", "aws050wta", "aws0100wta",
    ]]
    mu_full = mu_df.merge(mu_meta, on="mukey", how="left").merge(properties, on="mukey", how="left")
    mu_full.to_csv(paths.manifests_dir / "field_soil_map_units.csv", index=False)

    # Dominant map unit per field: component representing the largest share.
    comp_max = (
        attr.dropna(subset=["comppct_r"])
        .assign(comppct_r=lambda d: pd.to_numeric(d["comppct_r"], errors="coerce"))
        .groupby(["mukey"])["comppct_r"].max().reset_index()
    )
    comp_max = comp_max.merge(
        attr.drop_duplicates("mukey")[["mukey", "musym", "muname"]],
        on="mukey", how="left",
    )
    rank = (
        mu_df.merge(comp_max[["mukey", "comppct_r"]], on="mukey", how="left")
        .sort_values(["field_id", "comppct_r"], ascending=[True, False])
        .groupby("field_id").head(1)
        .rename(columns={"mukey": "dominant_mukey", "musym": "dominant_musym",
                         "muname": "dominant_muname", "comppct_r": "dominant_comppct"})
    )

    per_field = (
        mu_full.groupby("field_id").agg(
            n_map_units=("mukey", "nunique"),
            mukey_list=("mukey", lambda s: ";".join(sorted(set(map(str, s))))),
            musym_list=("musym", lambda s: ";".join(sorted(set(map(str, s))))),
        ).reset_index()
    )
    # Equal map-unit weighting for field-level property means (documented limitation).
    grouped = mu_full.groupby("field_id")
    for column in ["top_om_r", "top_cec7_r", "top_awc_r", "top_ph1to1h2o_r",
                   "top_claytotal_r", "top_sandtotal_r", "top_silttotal_r",
                   "top_dbthirdbar_r", "top_kwfact"]:
        per_field[column] = grouped[column].mean().reindex(per_field["field_id"]).round(4).values
    for column in ["aws025wta", "aws050wta", "aws0100wta", "slopegradwta", "wtdepannmin"]:
        per_field[column] = grouped[column].mean().reindex(per_field["field_id"]).round(4).values

    summary = rank.merge(per_field, on="field_id", how="right")
    summary["weighting_method"] = (
        "thickness-weighted 0-30 cm within component; component pct across components; "
        "equal weight across map units"
    )
    summary["area_weighted"] = False
    summary["limitation"] = (
        "SDA tabular API returns map unit membership without polygon area; "
        "map unit fractions are not area-weighted."
    )
    summary.to_csv(paths.manifests_dir / "field_soil_summary.csv", index=False)

    horizons = attr.copy()
    soil_dir = paths.derived_dir / "soil"
    soil_dir.mkdir(parents=True, exist_ok=True)
    horizons.to_parquet(soil_dir / "ssurgo_horizons.parquet", index=False)

    provenance = {
        "source": "USDA NRCS Soil Data Access (SDA) tabular REST",
        "endpoint": SDA_URL,
        "accessed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "survey_area": args.survey_area,
        "fields": int(len(fields)),
        "fields_with_soil": int(summary["dominant_mukey"].notna().sum()),
        "unique_map_units": int(mu_df["mukey"].nunique()),
        "field_map_unit_pairs": int(len(mu_df)),
        "query_batches": len(batches),
        "weighting": (
            "0-30 cm horizon overlap within component; component percentage across "
            "components; equal across map units"
        ),
        "area_weighted": False,
        "notes": [
            "Map unit membership comes from polygon intersection, not a centroid.",
            "Every map unit touching a field is retained in field_soil_map_units.csv.",
            "Map unit ids are retained so a future area-weighted pass can use mupolygon.",
            "Map unit fractions are not area-weighted: the tabular API has no polygon area.",
            "Horizons are clipped to 0-30 cm and weighted by their overlap thickness.",
        ],
    }
    (paths.manifests_dir / "field_soil_provenance.json").write_text(
        json.dumps(provenance, indent=2, default=str) + "\n", encoding="utf-8"
    )

    print(f"[soil] {len(summary)} fields, {provenance['fields_with_soil']} with soil; "
          f"{mu_df['mukey'].nunique()} map units in {time.time() - t0:.1f}s")
    return 0


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser("soils", help="Attach SSURGO map units and properties per field")
    parser.add_argument("--state-fips", default="18")
    parser.add_argument("--county-fips", default="081")
    parser.add_argument("--grower", default="in-johnson")
    parser.add_argument("--farm", default="countywide")
    parser.add_argument("--survey-area", default="IN081")
    parser.add_argument("--crop-only", action="store_true")
    parser.set_defaults(func=build_soils)
