#!/usr/bin/env python3
"""Offline tests for the reusable field-level EDA metric helpers.

Run from the skill checkout with the runtime venv (geopandas is required):

    /path/to/runtime/.venv/bin/python -m unittest \
        my-farm-advisor/data-pipeline/src/scripts/tests/test_eda_metrics.py

The tests use synthetic geometries and weather frames only. They need no
network access, no runtime data root, and no downloaded grower data.
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

TEST_PATH = Path(__file__).resolve()
SCRIPTS_DIR = TEST_PATH.parents[1]
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point, box

from eda_metrics import (
    CORN,
    OTHER,
    SOYBEANS,
    boundary_metrics,
    classify_shape,
    crop_group,
    crop_history_metrics,
    crop_transition_counts,
    crop_transitions,
    cumulative_gdd_curve,
    daily_gdd,
    rotation_summary,
    shannon_diversity,
    weather_season_metrics,
)


def _square_geometry(side_m: float, x: float = 0.0, y: float = 0.0):
    return box(x, y, x + side_m, y + side_m)


def _fields_frame(geometries: list, field_ids: list[str]) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {
            "field_id": field_ids,
            "grower": ["test-grower"] * len(geometries),
            "farm": ["test-farm"] * len(geometries),
            "state": ["NE"] * len(geometries),
            "county_name": ["Test County"] * len(geometries),
        },
        geometry=geometries,
        crs="EPSG:5070",
    )


class ShapeClassificationTests(unittest.TestCase):
    def test_circle_is_pivot_like(self) -> None:
        circle = Point(0, 0).buffer(100.0, quad_segs=32)
        metrics = boundary_metrics(_fields_frame([circle], ["pivot"]))
        self.assertEqual(metrics.loc[0, "shape_class"], "pivot-like")
        self.assertAlmostEqual(metrics.loc[0, "rect_fill"], math.pi / 4.0, places=1)

    def test_square_is_rectangular(self) -> None:
        metrics = boundary_metrics(_fields_frame([_square_geometry(100.0)], ["square"]))
        self.assertEqual(metrics.loc[0, "shape_class"], "rectangular")
        self.assertAlmostEqual(metrics.loc[0, "compactness_pp"], math.pi / 4.0, places=3)
        self.assertAlmostEqual(metrics.loc[0, "rect_fill"], 1.0, places=3)

    def test_classify_shape_thresholds(self) -> None:
        self.assertEqual(classify_shape(0.99, 0.79), "pivot-like")
        self.assertEqual(classify_shape(0.80, 0.99), "rectangular")
        self.assertEqual(classify_shape(0.40, 0.50), "irregular")
        self.assertEqual(classify_shape(float("nan"), 1.0), "unknown")

    def test_area_and_perimeter_are_equal_area_crs(self) -> None:
        metrics = boundary_metrics(
            _fields_frame(
                [_square_geometry(100.0, 0.0, 0.0), _square_geometry(200.0, 1000.0, 0.0)],
                ["small", "large"],
            )
        )
        by_id = metrics.set_index("field_id")
        self.assertAlmostEqual(by_id.loc["large", "area_acres"] / by_id.loc["small", "area_acres"], 4.0, places=3)
        self.assertAlmostEqual(by_id.loc["small", "perimeter_m"], 400.0, places=3)
        self.assertGreater(by_id.loc["large", "area_acres"], by_id.loc["small", "area_acres"])


class GddTests(unittest.TestCase):
    def test_daily_gdd_base_and_cap(self) -> None:
        tmax = pd.Series([30.0, 40.0, 5.0])
        tmin = pd.Series([10.0, 20.0, 0.0])
        values = daily_gdd(tmax, tmin)
        self.assertAlmostEqual(values.iloc[0], 10.0)
        self.assertAlmostEqual(values.iloc[1], 15.0)  # tmax capped at 30 C, tmin 20 C
        self.assertAlmostEqual(values.iloc[2], 0.0)  # clipped at zero

    def test_cumulative_gdd_is_monotonic(self) -> None:
        dates = pd.date_range("2022-04-01", "2022-10-31", freq="D")
        daily = pd.DataFrame(
            {
                "field_id": "f1",
                "date": dates,
                "T2M_MAX": 28.0,
                "T2M_MIN": 14.0,
            }
        )
        curve = cumulative_gdd_curve(daily)
        values = curve.sort_values("doy")["cumulative_gdd"].to_numpy()
        self.assertTrue(np.all(np.diff(values) >= 0.0))
        self.assertGreater(values[-1], 0.0)


def _synthetic_weather() -> pd.DataFrame:
    dates = pd.date_range("2022-01-01", "2022-12-31", freq="D")
    tmin = np.full(len(dates), 5.0)
    tmin[99] = -1.0  # last spring frost, doy 100
    tmin[299] = -1.0  # first fall frost, doy 300
    precip = np.full(len(dates), 2.0)
    june_start = int(np.where((dates.month == 6) & (dates.day == 1))[0][0])
    precip[june_start : june_start + 6] = 0.0  # six-day dry spell
    return pd.DataFrame(
        {
            "field_id": "f1",
            "date": dates,
            "T2M": (tmin + 20.0),
            "T2M_MAX": np.full(len(dates), 28.0),
            "T2M_MIN": tmin,
            "PRECTOTCORR": precip,
            "ALLSKY_SFC_SW_DWN": np.full(len(dates), 18.0),
            "RH2M": np.full(len(dates), 65.0),
        }
    )


class WeatherMetricTests(unittest.TestCase):
    def test_season_summary_and_frost_window(self) -> None:
        summary = weather_season_metrics(_synthetic_weather())
        row = summary.iloc[0]
        self.assertEqual(int(row["year"]), 2022)
        self.assertAlmostEqual(row["last_spring_frost_doy"], 100.0)
        self.assertAlmostEqual(row["first_fall_frost_doy"], 300.0)
        self.assertAlmostEqual(row["frost_free_days"], 200.0)
        self.assertEqual(int(row["max_dry_spell_days"]), 6)
        self.assertGreater(row["gs_precip_mm"], 0.0)
        self.assertGreater(row["gs_gdd"], 0.0)


class CropHistoryTests(unittest.TestCase):
    def test_crop_grouping(self) -> None:
        self.assertEqual(crop_group("Corn"), CORN)
        self.assertEqual(crop_group("Soybeans"), SOYBEANS)
        self.assertEqual(crop_group("Alfalfa"), OTHER)
        self.assertEqual(crop_group("Grass/Pasture"), OTHER)

    def test_rotation_sequence_and_transitions(self) -> None:
        years = [2021, 2022, 2023, 2024, 2025]
        crops = [CORN, SOYBEANS, CORN, SOYBEANS, CORN]
        history = pd.DataFrame(
            {
                "field_id": ["f1"] * 5,
                "year": years,
                "corn_pct": [90.0 if crop == CORN else 5.0 for crop in crops],
                "soy_pct": [5.0 if crop == CORN else 90.0 for crop in crops],
                "other_pct": [5.0] * 5,
                "dominant_crop": crops,
                "dominant_pct": [90.0] * 5,
                "mixed_pixels": [False] * 5,
            }
        )
        rotations = rotation_summary(history)
        row = rotations.iloc[0]
        self.assertEqual(row["rotation_sequence"], "CSCSC")
        self.assertEqual(int(row["crop_changes"]), 4)
        self.assertEqual(int(row["corn_years"]), 3)
        self.assertEqual(int(row["soy_years"]), 2)
        self.assertEqual(int(row["n_distinct_crops"]), 2)
        self.assertFalse(bool(row["continuous_corn"]))
        self.assertGreater(row["rotation_diversity"], 0.5)
        self.assertLess(row["rotation_diversity"], math.log(3))

        transitions = crop_transitions(history)
        self.assertEqual(len(transitions), 4)
        self.assertEqual(set(transitions["from_crop"]), {CORN, SOYBEANS})
        counts = crop_transition_counts(transitions)
        self.assertEqual(int(counts["count"].sum()), 4)
        self.assertTrue(np.allclose(counts.groupby("from_crop")["probability"].sum(), 1.0))

    def test_shannon_diversity(self) -> None:
        self.assertAlmostEqual(shannon_diversity([1.0, 1.0, 1.0]), math.log(3))
        self.assertTrue(math.isnan(shannon_diversity([0.0, 0.0])))

    def test_history_metrics_pivot(self) -> None:
        composition = pd.DataFrame(
            {
                "field_id": ["f1", "f1"],
                "year": [2021, 2022],
                "crop_name": ["Corn", "Soybeans"],
                "pct": [100.0, 100.0],
            }
        )
        history = crop_history_metrics(composition)
        by_year = history.set_index("year")
        self.assertEqual(by_year.loc[2021, "dominant_crop"], CORN)
        self.assertEqual(by_year.loc[2022, "dominant_crop"], SOYBEANS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
