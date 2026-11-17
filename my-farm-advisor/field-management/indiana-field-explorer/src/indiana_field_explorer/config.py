"""Configuration and path resolution for the Indiana Field Explorer."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

YEARS = (2020, 2021, 2022, 2023, 2024, 2025, 2026)
SEASON_START = date(2000, 4, 1)  # month/day only


def _period_starts(year: int) -> list[date]:
    starts: list[date] = []
    cursor = date(year, 4, 1)
    end_of_season = date(year, 10, 31)
    while cursor <= end_of_season:
        starts.append(cursor)
        cursor = cursor + timedelta(days=14)
    return starts


def periods_for(year: int) -> list[dict]:
    """Fifteen biweekly windows from Apr 1 to Oct 31 (last window is extended)."""
    end_of_season = date(year, 10, 31)
    starts = [date(year, 4, 1) + timedelta(days=14 * i) for i in range(15)]
    out: list[dict] = []
    for index, start in enumerate(starts, start=1):
        end = end_of_season if index == 15 else start + timedelta(days=13)
        out.append({
            "period": index,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "label": f"{start.strftime('%b %d')} - {end.strftime('%b %d')}",
        })
    return out


def period_index(year: int, iso_date: str) -> int | None:
    """Return the 1-based biweekly period for an ISO date, or None off-season."""
    d = date.fromisoformat(iso_date)
    if not (date(year, 4, 1) <= d <= date(year, 10, 31)):
        return None
    return min((d - date(year, 4, 1)).days // 14 + 1, 15)


@dataclass(frozen=True)
class Settings:
    repo_root: Path
    runtime_root: Path
    grower: str = "in-johnson"
    farm: str = "countywide"

    @property
    def farm_dir(self) -> Path:
        return (self.runtime_root / "data-pipeline" / "growers"
                / self.grower / "farms" / self.farm)

    @property
    def cache_dir(self) -> Path:
        return self.runtime_root / "state" / "explorer"

    @property
    def periods_cache(self) -> Path:
        return self.cache_dir / "ndvi_biweekly"

    @property
    def field_index_dir(self) -> Path:
        return self.cache_dir / "field_index"

    @property
    def web_dir(self) -> Path:
        return self.repo_root / "my-farm-advisor" / "field-management" \
            / "indiana-field-explorer" / "web"

    @property
    def discovery_src(self) -> Path:
        return self.repo_root / "my-farm-advisor" / "field-management" \
            / "indiana-field-discovery" / "src"


def settings() -> Settings:
    repo_root = Path(__file__).resolve().parents[5]
    runtime = Path(os.environ.get(
        "INDIANA_RUNTIME_ROOT", "/home/coder/workspace/indiana-runtime"
    )).expanduser()
    return Settings(repo_root=repo_root, runtime_root=runtime)
