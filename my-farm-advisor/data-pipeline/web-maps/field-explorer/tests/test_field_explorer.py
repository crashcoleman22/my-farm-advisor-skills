#!/usr/bin/env python3
"""Offline tests for the field explorer generator.

Run with the runtime virtualenv, because the generator and these tests use
shapely, pyproj, numpy, rasterio and Pillow:

    /path/to/runtime/data-pipeline/.venv/bin/python -m unittest \
      my-farm-advisor/data-pipeline/web-maps/field-explorer/tests/test_field_explorer.py

No network access is used. USDA SDA and Sentinel-2 downloads are replaced by
synthetic files in a temporary directory.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import math
import re
import tempfile
import unittest
from pathlib import Path

import numpy as np
from shapely.geometry import shape

GENERATOR = Path(__file__).resolve().parents[1] / "generate.py"
SPEC = importlib.util.spec_from_file_location("field_explorer_generate", GENERATOR)
fe = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(fe)

SQUARE = [
    [-93.2100, 43.1100],
    [-93.2000, 43.1100],
    [-93.2000, 43.1200],
    [-93.2100, 43.1200],
    [-93.2100, 43.1100],
]
SMALL_SQUARE = [
    [-93.2100, 43.1100],
    [-93.2060, 43.1100],
    [-93.2060, 43.1130],
    [-93.2100, 43.1130],
    [-93.2100, 43.1100],
]


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")


def polygon_feature(geometry: list, **properties: object) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [geometry]},
        "properties": properties,
    }


def field_record(field_id: str, state: str, acres: float = 100.0, **extra: object) -> dict:
    record = {
        "field_id": field_id,
        "field_name": field_id,
        "field_dir": Path("/nonexistent") / field_id,
        "farm_dir": Path("/nonexistent"),
        "grower_slug": state.lower() + "-grower",
        "grower_name": state + " Grower",
        "farm_slug": state.lower() + "-farm",
        "farm_name": state + " Farm",
        "state": state,
        "county": "Test",
        "acres": acres,
        "source": "synthetic",
        "geometry": {"type": "Polygon", "coordinates": [SQUARE]},
        "bbox": [-93.21, 43.11, -93.20, 43.12],
    }
    record.update(extra)
    return record


class SelectFieldsTests(unittest.TestCase):
    def test_limit_per_state_is_deterministic_and_balanced(self) -> None:
        fields = [
            field_record("b", "IL"),
            field_record("a", "IL"),
            field_record("d", "IA"),
            field_record("c", "IA"),
            field_record("e", "NE"),
        ]
        selected = fe.select_fields(fields, 1)
        self.assertEqual([item["field_id"] for item in selected], ["c", "a", "e"])
        self.assertEqual(len(selected), 3)

    def test_zero_or_none_keeps_everything(self) -> None:
        fields = [field_record("a", "IL"), field_record("b", "IA")]
        self.assertEqual(fe.select_fields(fields, 0), fields)
        self.assertEqual(fe.select_fields(fields, None), fields)

    def test_limit_larger_than_group_keeps_group(self) -> None:
        fields = [field_record("a", "IL"), field_record("b", "IL")]
        self.assertEqual(len(fe.select_fields(fields, 5)), 2)


class NdviSceneStatsTests(unittest.TestCase):
    def test_all_missing_is_not_usable(self) -> None:
        stats = fe.ndvi_scene_stats(np.full((4, 4), np.nan))
        self.assertFalse(stats["usable"])
        self.assertTrue(stats["low_coverage"])
        self.assertEqual(stats["valid_pixels"], 0)
        self.assertIsNone(stats["mean"])

    def test_fraction_and_percentiles_use_valid_pixels(self) -> None:
        values = np.array([[np.nan, 0.2], [0.6, 0.4]], dtype="float32")
        stats = fe.ndvi_scene_stats(values)
        self.assertEqual(stats["total_pixels"], 4)
        self.assertEqual(stats["valid_pixels"], 3)
        self.assertAlmostEqual(stats["valid_fraction"], 0.75)
        self.assertAlmostEqual(stats["mean"], 0.4, places=3)
        self.assertFalse(stats["usable"] is False)

    def test_field_mask_replaces_bounding_box_denominator(self) -> None:
        values = np.full((4, 4), 0.5, dtype="float32")
        mask = np.zeros((4, 4), dtype=bool)
        mask[0, :] = True  # a field that fills one quarter of its rectangle
        stats = fe.ndvi_scene_stats(values, field_mask=mask)
        self.assertEqual(stats["total_pixels"], 4)
        self.assertEqual(stats["valid_pixels"], 4)
        self.assertEqual(stats["valid_fraction"], 1.0)
        self.assertFalse(stats["low_coverage"])

    def test_pixels_outside_field_are_ignored(self) -> None:
        values = np.array([[0.2, 0.9], [0.9, 0.9]], dtype="float32")
        mask = np.array([[True, False], [False, False]])
        stats = fe.ndvi_scene_stats(values, field_mask=mask)
        self.assertEqual(stats["valid_pixels"], 1)
        self.assertEqual(stats["mean"], 0.2)

    def test_minimum_valid_fraction_excludes_nearly_empty_scene(self) -> None:
        values = np.full((10, 10), np.nan, dtype="float32")
        values[0, 0] = 0.5  # 1% of the field
        stats = fe.ndvi_scene_stats(values, min_valid_fraction=0.05)
        self.assertEqual(stats["valid_fraction"], 0.01)
        self.assertFalse(stats["usable"])
        self.assertTrue(stats["low_coverage"])

    def test_partial_coverage_is_flagged_but_kept(self) -> None:
        values = np.full((10, 10), np.nan, dtype="float32")
        values.flat[:40] = 0.4  # 40% valid
        stats = fe.ndvi_scene_stats(values)
        self.assertTrue(stats["usable"])
        self.assertFalse(stats["low_coverage"])


class SoilUnitTests(unittest.TestCase):
    def test_soil_intersections_percent_and_attributes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            field_dir = Path(tmp) / "field"
            geojson = {
                "type": "FeatureCollection",
                "features": [
                    polygon_feature(SQUARE, mukey="111"),
                    polygon_feature(SMALL_SQUARE, mukey="222"),
                ],
            }
            write_json(field_dir / "soil" / "ssurgo_soil_types.geojson", geojson)

            big_acres = fe.area_acres(shape({"type": "Polygon", "coordinates": [SQUARE]}))
            field = field_record("f1", "IA", acres=big_acres / 0.5)
            field["field_dir"] = field_dir
            attributes = {
                "111": {
                    "musym": "X1",
                    "muname": "Alpha silt loam",
                    "slope_weighted_pct": 1.5,
                    "drainage": "Well drained",
                    "hydrologic_group": "B",
                    "aws_050cm": 8.2,
                    "nccpi": 0.77,
                    "nccpi_class": "Moderately high inherent productivity",
                }
            }

            rows, features = fe.load_soil_units(field, attributes)

        self.assertEqual(len(rows), 2)
        self.assertGreaterEqual(rows[0]["acres"], rows[1]["acres"])
        self.assertEqual(rows[0]["mukey"], "111")
        self.assertAlmostEqual(rows[0]["pct"], 50.0, delta=0.2)
        self.assertEqual(rows[0]["musym"], "X1")
        self.assertEqual(rows[0]["drainage"], "Well drained")
        self.assertEqual(rows[0]["nccpi"], 0.77)
        self.assertTrue(rows[0]["attributes_available"])
        self.assertFalse(rows[1]["attributes_available"])
        self.assertEqual(len(features), 2)
        self.assertEqual(features[0]["properties"]["field_id"], "f1")
        self.assertIn("mukey", features[0]["properties"])

    def test_disjoint_polygons_of_one_map_unit_are_merged_for_the_inspector(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            field_dir = Path(tmp) / "field"
            write_json(
                field_dir / "soil" / "ssurgo_soil_types.geojson",
                {
                    "type": "FeatureCollection",
                    "features": [
                        polygon_feature(SQUARE, mukey="111"),
                        polygon_feature(SMALL_SQUARE, mukey="111"),
                    ],
                },
            )
            big = fe.area_acres(shape({"type": "Polygon", "coordinates": [SQUARE]}))
            small = fe.area_acres(shape({"type": "Polygon", "coordinates": [SMALL_SQUARE]}))
            field = field_record("f1", "IA", acres=(big + small) * 2)
            field["field_dir"] = field_dir
            rows, features = fe.load_soil_units(field, {})

        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["acres"], round(big + small, 1), places=1)
        self.assertAlmostEqual(rows[0]["pct"], 50.0, delta=0.2)
        self.assertEqual(len(features), 2)
        self.assertEqual({feature["properties"]["mukey"] for feature in features}, {"111"})

    def test_missing_soil_file_returns_empty(self) -> None:
        field = field_record("f1", "IL")
        with tempfile.TemporaryDirectory() as tmp:
            field["field_dir"] = Path(tmp)
            rows, features = fe.load_soil_units(field, {})
        self.assertEqual(rows, [])
        self.assertEqual(features, [])


class CdlTests(unittest.TestCase):
    def _write_table(self, farm_dir: Path, year: int, rows: list[dict[str, object]]) -> None:
        table = farm_dir / "derived" / "tables" / f"synthetic_{year}_cdl.csv"
        table.parent.mkdir(parents=True, exist_ok=True)
        with table.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=["field_id", "crop_name", "pct", "pixel_count"]
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

    def test_dominant_crop_top_list_and_mixed_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            farm_dir = Path(tmp)
            self._write_table(
                farm_dir,
                2025,
                [
                    {"field_id": "a", "crop_name": "Corn", "pct": 72.5, "pixel_count": 725},
                    {"field_id": "a", "crop_name": "Soybeans", "pct": 20.0, "pixel_count": 200},
                    {"field_id": "a", "crop_name": "Forest", "pct": 7.5, "pixel_count": 75},
                    {"field_id": "b", "crop_name": "Corn", "pct": 45.0, "pixel_count": 450},
                    {"field_id": "b", "crop_name": "Soybeans", "pct": 55.0, "pixel_count": 550},
                ],
            )
            fields = [
                field_record("a", "IL", farm_dir=farm_dir),
                field_record("b", "IL", farm_dir=farm_dir),
            ]
            result = fe.load_cdl_by_field(fields, [2025, 2024])

        a = result["a"][2025]
        self.assertEqual(a["crop"], "Corn")
        self.assertEqual(a["pct"], 72.5)
        self.assertEqual(a["classified_pct"], 100.0)
        self.assertFalse(a["mixed"])
        self.assertEqual(a["top"][0], {"crop": "Corn", "pct": 72.5})
        self.assertEqual(len(a["top"]), 3)

        b = result["b"][2025]
        self.assertEqual(b["crop"], "Soybeans")
        self.assertTrue(b["mixed"])
        self.assertNotIn(2024, result["b"])

    def test_missing_table_is_absent_not_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fields = [field_record("a", "IL", farm_dir=Path(tmp))]
            result = fe.load_cdl_by_field(fields, [2025])
        self.assertEqual(result, {})


class WeatherTests(unittest.TestCase):
    def test_row_parsing_and_missing_values(self) -> None:
        row = {
            "year": "2023",
            "gs_precip_mm": "480.5",
            "gs_gdd": "1980",
            "heat_days_35c": "12",
            "frost_free_days": "nan",
            "max_dry_spell_days": "18",
            "gs_mean_temp_c": "19.25",
        }
        parsed = fe._weather_row_from_csv(row)
        self.assertEqual(parsed["year"], 2023)
        self.assertEqual(parsed["precip_mm"], 480.5)
        self.assertIsNone(parsed["frost_free_days"])
        self.assertEqual(parsed["mean_temp_c"], 19.25)

    def test_shared_weather_cells_are_detected(self) -> None:
        def vector(precip: float) -> dict:
            # load_weather keys the inner dictionaries by integer year.
            return {
                year: {
                    "year": year,
                    "precip_mm": precip,
                    "gdd": 2000,
                    "frost_free_days": 180,
                }
                for year in range(2021, 2026)
            }

        fields = [
            field_record("a", "IL"),
            field_record("b", "IL"),
            field_record("c", "NE"),
        ]
        weather = {"a": vector(500), "b": vector(500), "c": vector(300)}
        fe.annotate_shared_weather(fields, weather, list(range(2021, 2026)))
        self.assertEqual(fields[0]["weather_shared_with"], 2)
        self.assertEqual(fields[1]["weather_shared_with"], 2)
        self.assertEqual(fields[2]["weather_shared_with"], 1)


class SsurgoAttributeCacheTests(unittest.TestCase):
    def test_cache_hit_avoids_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "soil_attributes.json"
            write_json(
                cache,
                {"attributes": {"123": {"muname": "Cached soil", "nccpi": 0.5}}},
            )
            attributes, status = fe.fetch_ssurgo_attributes(
                ["123"], cache, offline=False
            )
        self.assertEqual(status, "cache")
        self.assertEqual(attributes["123"]["muname"], "Cached soil")

    def test_offline_without_cache_degrades_gracefully(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "soil_attributes.json"
            attributes, status = fe.fetch_ssurgo_attributes(
                ["123"], cache, offline=True
            )
        self.assertEqual(attributes, {})
        self.assertEqual(status, "offline-no-cache")

    def test_no_mukeys_short_circuits(self) -> None:
        attributes, status = fe.fetch_ssurgo_attributes([], Path("/tmp/none.json"))
        self.assertEqual(attributes, {})
        self.assertEqual(status, "no-mukeys")


class PayloadAndHtmlTests(unittest.TestCase):
    def _payload(self) -> dict:
        field = field_record("f1", "IA", acres=80.0)
        field["weather_shared_with"] = 2
        field["cdl"] = {"2025": {"crop": "Corn", "pct": 80.0, "top": [], "mixed": False}}
        field["weather"] = {"2025": {"year": 2025, "precip_mm": 500.0, "gdd": 2000}}
        field["soil"] = [
            {
                "mukey": "123",
                "musym": "X1",
                "muname": "Alpha silt loam",
                "acres": 40.0,
                "pct": 50.0,
                "slope_pct": 1.0,
                "drainage": "Well drained",
                "nccpi": 0.7,
                "attributes_available": True,
            }
        ]
        field["ndvi"] = {
            "available": True,
            "years": {
                "2025": [
                    {
                        "date": "2025-07-01",
                        "mean": 0.6,
                        "p10": 0.5,
                        "p50": 0.6,
                        "p90": 0.7,
                        "valid_fraction": 0.9,
                        "low_coverage": False,
                        "png": "ndvi/f1/2025-07-01.png",
                        "bounds": [[43.11, -93.21], [43.12, -93.20]],
                    }
                ]
            },
            "excluded": 1,
        }
        return fe.build_payload(
            title="Test explorer",
            fields=[field],
            years=[2021, 2022, 2023, 2024, 2025],
            weather_source="eda_table",
            soil_attribute_status="cache",
            ndvi_meta={"scene_count": 1, "excluded_scenes": 1, "years": [2021, 2025]},
            notes=["A note."],
        )

    def test_state_summary_uses_requested_order(self) -> None:
        fields = [
            field_record("ne", "NE", acres=10.0),
            field_record("ia", "IA", acres=20.0),
            field_record("il", "IL", acres=30.0),
        ]
        payload = fe.build_payload(
            title="Order",
            fields=fields,
            years=[2025],
            weather_source="eda_table",
            soil_attribute_status="cache",
            ndvi_meta={"scene_count": 0},
            notes=[],
        )
        self.assertEqual(
            [state["code"] for state in payload["meta"]["states"]],
            ["IL", "IA", "NE"],
        )

    def test_payload_shape(self) -> None:
        payload = self._payload()
        self.assertEqual(payload["meta"]["field_count"], 1)
        self.assertEqual(payload["meta"]["states"][0]["code"], "IA")
        self.assertEqual(len(payload["geojson"]["features"]), 1)
        self.assertEqual(payload["fields"][0]["soil"][0]["mukey"], "123")
        self.assertEqual(payload["fields"][0]["ndvi"]["years"]["2025"][0]["date"], "2025-07-01")

    def test_html_embeds_data_but_not_rasters(self) -> None:
        payload = self._payload()
        html = fe.render_html(payload, fe.WEB_DIR / "assets")
        self.assertNotIn("{{", html)
        self.assertIn('id="map-data"', html)
        self.assertIn("leaflet", html.lower())
        self.assertNotIn("data:image", html)
        self.assertIn("Raster vs. average", html)
        match = re.search(
            r'<script id="map-data" type="application/json">(.*?)</script>', html, re.S
        )
        self.assertIsNotNone(match)
        inline = json.loads(match.group(1))
        self.assertEqual(inline["fields"][0]["field_id"], "f1")

    def test_missing_sections_render_placeholders(self) -> None:
        field = field_record("f2", "NE", acres=12.0)
        field["cdl"] = {}
        field["weather"] = {}
        field["soil"] = []
        field["ndvi"] = {"available": False, "years": {}, "reason": "no scenes"}
        payload = fe.build_payload(
            title="Missing data",
            fields=[field],
            years=[2025],
            weather_source="eda_table",
            soil_attribute_status="unavailable",
            ndvi_meta={"scene_count": 0, "excluded_scenes": 0},
            notes=[],
        )
        html = fe.render_html(payload, fe.WEB_DIR / "assets")
        self.assertIn("No CDL table for this year", html)
        self.assertIn("No usable NDVI scenes", html)
        self.assertIn("No SSURGO map-unit polygons", html)


class NdviAssetTests(unittest.TestCase):
    def _build_field(self, root: Path) -> tuple[dict, Path, Path]:
        import rasterio
        from rasterio.transform import from_bounds

        field_dir = root / "growers" / "g" / "farms" / "farm" / "fields" / "f1"
        sentinel = field_dir / "satellite" / "sentinel"
        scene_dir = sentinel / "2025" / "sentinel_20250701"
        scene_dir.mkdir(parents=True, exist_ok=True)

        width = height = 40
        west, south, east, north = -93.22, 43.10, -93.21, 43.11
        transform = from_bounds(west, south, east, north, width, height)
        values = np.full((height, width), 0.4, dtype="float32")
        values[0:5, 0:5] = np.nan
        tif = scene_dir / "sentinel_20250701_ndvi.tif"
        with rasterio.open(
            tif,
            "w",
            driver="GTiff",
            height=height,
            width=width,
            count=1,
            dtype="float32",
            crs="EPSG:4326",
            transform=transform,
            nodata=np.nan,
        ) as dataset:
            dataset.write(values, 1)

        # Triangle covering roughly half of the bounding box.
        triangle = [
            [west, south],
            [east, south],
            [west, north],
            [west, south],
        ]
        write_json(
            sentinel / "manifest.json",
            {
                "years": [
                    {
                        "year": 2025,
                        "scenes": [
                            {
                                "scene_id": "S2_TEST",
                                "scene_date": "2025-07-01",
                                "cloud_cover": 2.5,
                                "ndvi_tif": str(tif),
                            },
                            {
                                "scene_id": "S2_EMPTY",
                                "scene_date": "2025-07-15",
                                "cloud_cover": 99.0,
                                "ndvi_tif": str(scene_dir / "missing.tif"),
                            },
                        ],
                    }
                ]
            },
        )
        field = {
            "field_id": "f1",
            "field_dir": field_dir,
            "geometry": {"type": "Polygon", "coordinates": [triangle]},
            "acres": 50.0,
        }
        return field, root, field_dir

    def test_scene_png_stats_and_exclusion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data-pipeline"
            root.mkdir()
            field, pipeline_root, field_dir = self._build_field(root)
            out = Path(tmp) / "out"
            result = fe.build_ndvi_assets(
                field,
                out,
                pipeline_root,
                start_year=2021,
                end_year=2025,
            )

            self.assertTrue(result["available"])
            self.assertEqual(result["excluded"], 1)
            scenes = result["years"]["2025"]
            self.assertEqual(len(scenes), 1)
            scene = scenes[0]
            self.assertEqual(scene["date"], "2025-07-01")
            self.assertAlmostEqual(scene["mean"], 0.4, places=2)
            # The triangle fills about half the raster bbox but nearly every
            # pixel inside it is valid, so coverage must be high.
            self.assertGreater(scene["valid_fraction"], 0.85)
            self.assertFalse(scene["low_coverage"])
            png = out / scene["png"]
            self.assertTrue(png.is_file())

            from PIL import Image

            with Image.open(png) as image:
                alpha = np.array(image)[..., 3]
            covered = float((alpha > 0).mean())
            self.assertGreater(covered, 0.25)
            self.assertLess(covered, 0.9)

            # Bounds are [[south, west], [north, east]].
            (south, west), (north, east) = scene["bounds"]
            self.assertLess(south, north)
            self.assertLess(west, east)
            self.assertAlmostEqual(south, 43.10, places=3)
            self.assertAlmostEqual(west, -93.22, places=3)

    def test_missing_manifest_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            field_dir = Path(tmp) / "field"
            field_dir.mkdir()
            field = {
                "field_id": "f1",
                "field_dir": field_dir,
                "geometry": {"type": "Polygon", "coordinates": [SQUARE]},
            }
            result = fe.build_ndvi_assets(
                field,
                Path(tmp) / "out",
                Path(tmp),
                start_year=2021,
                end_year=2025,
            )
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "no Sentinel-2 manifest")


class RasterPaletteTests(unittest.TestCase):
    def test_discrete_palette_and_transparency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.png"
            values = np.array([[-0.5, 0.05, 0.45, 0.85, np.nan]], dtype="float32")
            fe._write_ndvi_png(values, path)
            from PIL import Image

            with Image.open(path) as image:
                array = np.array(image)
        self.assertEqual(array.shape, (1, 5, 4))
        self.assertEqual(tuple(array[0, 0, :3]), (140, 81, 10))
        self.assertEqual(tuple(array[0, 1, :3]), (191, 129, 45))
        self.assertEqual(tuple(array[0, 2, :3]), (128, 205, 193))
        self.assertEqual(tuple(array[0, 3, :3]), (0, 68, 27))
        self.assertEqual(array[0, 4, 3], 0)
        self.assertEqual(array[0, 0, 3], 235)


class GeometryTests(unittest.TestCase):
    def test_bbox_and_area(self) -> None:
        geometry = {"type": "Polygon", "coordinates": [SQUARE]}
        bbox = fe.geometry_bbox(geometry)
        self.assertAlmostEqual(bbox[0], -93.21)
        self.assertAlmostEqual(bbox[2], -93.20)
        acres = fe.area_acres(shape({"type": "Polygon", "coordinates": [SQUARE]}))
        # 0.01 deg x 0.01 deg near 43 N is roughly 220 acres.
        self.assertTrue(200 < acres < 250, acres)
        self.assertFalse(math.isnan(acres))


if __name__ == "__main__":
    unittest.main()
