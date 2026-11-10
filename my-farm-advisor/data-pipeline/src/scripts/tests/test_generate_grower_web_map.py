#!/usr/bin/env python3
"""Offline tests for the grower web-map generator.

Run from the skill checkout:

    python -m unittest my-farm-advisor/data-pipeline/src/scripts/tests/test_generate_grower_web_map.py

The tests build a synthetic runtime tree in a temporary directory, so they need
no network access, no runtime venv, and no real grower data. Geometry is read
with the standard library only; shapely is used only for very large polygons.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TEST_PATH = Path(__file__).resolve()
SCRIPTS_DIR = TEST_PATH.parents[1]
GENERATOR = SCRIPTS_DIR / "reporting" / "generate_grower_web_map.py"
DATA_SECTION = re.compile(
    r'<script id="map-data" type="application/json">(.*?)</script>', re.S
)
INLINE_SCRIPT = re.compile(r"<script>\s*(\(function \(\).*?)</script>", re.S)

SQUARE_SMALL = [
    [-93.21, 43.11],
    [-93.209, 43.11],
    [-93.209, 43.1108],
    [-93.21, 43.1108],
    [-93.21, 43.11],
]
SQUARE_LARGE = [
    [-93.22, 43.12],
    [-93.21, 43.12],
    [-93.21, 43.125],
    [-93.22, 43.125],
    [-93.22, 43.12],
]


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _boundary_feature(field_id: str, acres: float, geometry: list) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [geometry]},
        "properties": {
            "field_id": field_id,
            "source": "OpenStreetMap/Overpass",
            "crop_name": "farmland",
            "state_fips": "19",
            "county_fips": "19109",
            "county_name": "Kossuth",
            "area_acres": acres,
        },
    }


def build_runtime(root: Path, *, farm_slug: str = "acme-farm") -> Path:
    pipeline = root / "data-pipeline"
    grower = pipeline / "growers" / "acme-grower"
    _write_json(
        grower / "grower.json",
        {"grower_slug": "acme-grower", "display_name": "Acme Grower"},
    )
    farm = grower / "farms" / farm_slug
    _write_json(
        farm / "farm.json",
        {"farm_slug": farm_slug, "display_name": "Acme Farm", "state": "IA"},
    )
    _write_json(
        farm / "boundary" / "field_boundaries.geojson",
        {
            "type": "FeatureCollection",
            "features": [
                _boundary_feature("osm-1", 120.0, SQUARE_LARGE),
                _boundary_feature("osm-2", 40.5, SQUARE_SMALL),
            ],
        },
    )
    for field_id in ("osm-1", "osm-2"):
        _write_json(
            farm / "fields" / field_id / "field.json",
            {"field_id": field_id.upper(), "display_name": f"{field_id} name"},
        )
    tables = farm / "derived" / "tables"
    tables.mkdir(parents=True, exist_ok=True)
    (tables / "acme_2023_cdl.csv").write_text(
        "field_id,year,crop_code,crop_name,pixel_count,pct,crop_source\n"
        "osm-1,2023,1,Corn,900,90.0,cdl\n"
        "osm-1,2023,5,Soybeans,100,10.0,cdl\n"
        "osm-2,2023,5,Soybeans,80,100.0,cdl\n",
        encoding="utf-8",
    )
    reports = farm / "derived" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "acme_farm_report.html").write_text("<html>report</html>", encoding="utf-8")
    return pipeline


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GENERATOR), *args],
        capture_output=True,
        text=True,
        check=False,
    )


class GrowerWebMapTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.pipeline = build_runtime(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_builds_map_and_manifest(self) -> None:
        result = run_cli("--grower", "acme-grower", "--root", str(self.root))
        self.assertEqual(result.returncode, 0, result.stderr)

        html_path = (
            self.pipeline
            / "growers"
            / "acme-grower"
            / "derived"
            / "reports"
            / "acme-grower_fields_map.html"
        )
        manifest_path = (
            self.pipeline / "growers" / "acme-grower" / "manifests" / "grower_web_map.json"
        )
        self.assertTrue(html_path.is_file())
        self.assertTrue(manifest_path.is_file())

        html = html_path.read_text(encoding="utf-8")
        match = DATA_SECTION.search(html)
        self.assertIsNotNone(match, "inline map data section is missing")
        data = json.loads(match.group(1).replace("<\\/", "</"))

        features = data["geojson"]["features"]
        self.assertEqual(len(features), 2)
        self.assertEqual(
            {feature["properties"]["field_id"] for feature in features}, {"osm-1", "osm-2"}
        )
        props = features[0]["properties"]
        for key in (
            "grower_name",
            "farm_name",
            "field_name",
            "field_id",
            "area_acres",
            "last_crop_name",
            "last_crop_year",
        ):
            self.assertIn(key, props)
        self.assertEqual(props["grower_name"], "Acme Grower")
        self.assertEqual(props["farm_name"], "Acme Farm")
        self.assertEqual(props["last_crop_year"], 2023)

        # Basemap, library, and interactive controls.
        self.assertIn("World_Imagery/MapServer/tile/{z}/{y}/{x}", html)
        self.assertIn("leaflet@1.9.4", html)
        self.assertIn("zoomControl", html)
        self.assertIn("fitBounds", html)
        self.assertIn("osm-1 name", html)

        # Data-quality guardrails: no weather aggregates in the map.
        self.assertNotIn("annual_precip", html)
        self.assertNotIn("PRECTOTCORR", html)

        # Size budget: a grower map must stay small.
        self.assertLess(html_path.stat().st_size, 200_000)

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["grower_slug"], "acme-grower")
        self.assertEqual(manifest["farms"][0]["field_count"], 2)
        self.assertEqual(manifest["fields"][0]["last_crop_year"], 2023)
        self.assertEqual(manifest["output"]["bytes"], html_path.stat().st_size)
        self.assertRegex(manifest["output"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(manifest["farms"][0]["boundary_sha256"], r"^[0-9a-f]{64}$")

    def test_inline_javascript_parses(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed; skipping JavaScript syntax check")

        result = run_cli("--grower", "acme-grower", "--root", str(self.root))
        self.assertEqual(result.returncode, 0, result.stderr)
        html = (
            self.pipeline
            / "growers"
            / "acme-grower"
            / "derived"
            / "reports"
            / "acme-grower_fields_map.html"
        ).read_text(encoding="utf-8")
        match = INLINE_SCRIPT.search(html)
        self.assertIsNotNone(match, "inline map script is missing")

        script_path = self.root / "map.js"
        script_path.write_text(match.group(1), encoding="utf-8")
        check = subprocess.run(
            [node, "--check", str(script_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(check.returncode, 0, check.stderr)

    def test_weighted_crop_is_dominant(self) -> None:
        result = run_cli("--grower", "acme-grower", "--root", str(self.root))
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads(
            (
                self.pipeline
                / "growers"
                / "acme-grower"
                / "manifests"
                / "grower_web_map.json"
            ).read_text(encoding="utf-8")
        )
        crops = {item["field_id"]: item["last_crop_name"] for item in manifest["fields"]}
        self.assertEqual(crops["osm-1"], "Corn")
        self.assertEqual(crops["osm-2"], "Soybeans")

    def test_crop_fallback_uses_latest_available_year(self) -> None:
        tables = (
            self.pipeline
            / "growers"
            / "acme-grower"
            / "farms"
            / "acme-farm"
            / "derived"
            / "tables"
        )
        (tables / "acme_2022_cdl.csv").write_text(
            "field_id,year,crop_code,crop_name,pixel_count,pct,crop_source\n"
            "osm-1,2022,5,Soybeans,100,100.0,cdl\n",
            encoding="utf-8",
        )
        (tables / "acme_2023_cdl.csv").unlink()
        result = run_cli("--grower", "acme-grower", "--root", str(self.root))
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads(
            (
                self.pipeline
                / "growers"
                / "acme-grower"
                / "manifests"
                / "grower_web_map.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["farms"][0]["cdl_year"], 2022)
        self.assertEqual(manifest["fields"][0]["last_crop_year"], 2022)

    def test_all_growers_skips_farms_without_boundaries(self) -> None:
        grower = self.pipeline / "growers" / "empty-grower"
        _write_json(grower / "grower.json", {"display_name": "Empty Grower"})
        (grower / "farms" / "no-boundary").mkdir(parents=True)

        result = run_cli("--all-growers", "--root", str(self.root))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            (
                self.pipeline
                / "growers"
                / "acme-grower"
                / "derived"
                / "reports"
                / "acme-grower_fields_map.html"
            ).is_file()
        )
        self.assertFalse(
            (grower / "derived" / "reports" / "empty-grower_fields_map.html").exists()
        )
        self.assertIn("empty-grower", result.stderr)

    def test_farm_filter_limits_fields(self) -> None:
        farm = self.pipeline / "growers" / "acme-grower" / "farms" / "second-farm"
        _write_json(farm / "farm.json", {"display_name": "Second Farm", "state": "IA"})
        _write_json(
            farm / "boundary" / "field_boundaries.geojson",
            {
                "type": "FeatureCollection",
                "features": [_boundary_feature("osm-3", 10.0, SQUARE_SMALL)],
            },
        )
        result = run_cli(
            "--grower",
            "acme-grower",
            "--root",
            str(self.root),
            "--farm",
            "second-farm",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        html = (
            self.pipeline
            / "growers"
            / "acme-grower"
            / "derived"
            / "reports"
            / "acme-grower_fields_map.html"
        ).read_text(encoding="utf-8")
        self.assertIn("osm-3", html)
        self.assertNotIn("osm-1", html)

    def test_unsafe_grower_slug_is_rejected(self) -> None:
        result = run_cli("--grower", "../escape", "--root", str(self.root))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("grower_slug", result.stderr)

    def test_missing_grower_fails_cleanly(self) -> None:
        result = run_cli("--grower", "does-not-exist", "--root", str(self.root))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("grower not found", result.stderr)

    def test_out_override_cannot_combine_with_all_growers(self) -> None:
        result = run_cli(
            "--all-growers", "--root", str(self.root), "--out", str(self.root / "x.html")
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--out", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
