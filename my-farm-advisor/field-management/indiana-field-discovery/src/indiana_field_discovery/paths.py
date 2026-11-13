"""Runtime path helpers for county field discovery.

All generated data lives outside the repository, under ``INDIANA_RUNTIME_ROOT``::

    <root>/data-pipeline/growers/<grower>/farms/<farm>/{boundary,manifests,derived}
    <root>/state/        resumability checkpoints
    <root>/tmp/          scratch, wiped between stages
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class PathError(ValueError):
    """Raised when the runtime root is missing or unusable."""


def data_root(env: dict[str, str] | None = None) -> Path:
    values = os.environ if env is None else env
    raw = values.get("INDIANA_RUNTIME_ROOT")
    if not raw:
        raise PathError(
            "INDIANA_RUNTIME_ROOT is required (absolute path, e.g. "
            "/home/coder/workspace/indiana-runtime)"
        )
    root = Path(raw).expanduser()
    if not root.is_absolute():
        raise PathError(f"INDIANA_RUNTIME_ROOT must be absolute, got {raw!r}")
    return root.resolve(strict=False)


@dataclass(frozen=True)
class FarmPaths:
    root: Path
    grower: str
    farm: str

    @property
    def data_dir(self) -> Path:
        return self.root / "data-pipeline"

    @property
    def farm_dir(self) -> Path:
        return self.data_dir / "growers" / self.grower / "farms" / self.farm

    @property
    def boundary_dir(self) -> Path:
        return self.farm_dir / "boundary"

    @property
    def manifests_dir(self) -> Path:
        return self.farm_dir / "manifests"

    @property
    def derived_dir(self) -> Path:
        return self.farm_dir / "derived"

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def tmp_dir(self) -> Path:
        return self.root / "tmp"

    def ensure(self) -> "FarmPaths":
        for path in (
            self.boundary_dir,
            self.manifests_dir,
            self.derived_dir,
            self.state_dir,
            self.tmp_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        return self


def farm_paths(grower: str, farm: str, env: dict[str, str] | None = None) -> FarmPaths:
    return FarmPaths(root=data_root(env), grower=grower, farm=farm)
