"""CLI: ``python -m indiana_field_explorer serve|warmup|compute|raster|soils``."""

from __future__ import annotations

import argparse
import sys

from . import server
from .store import Store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="indiana_field_explorer")
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="run the web app")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8420)

    p_warm = sub.add_parser("warmup", help="pre-compute 2020 biweekly periods")
    p_warm.add_argument("--years", default="2020")
    p_warm.add_argument("--periods", default="1-15")

    p_one = sub.add_parser("compute", help="compute one 2020 period")
    p_one.add_argument("period", type=int)

    p_raster = sub.add_parser("raster", help="build per-pixel NDVI composites")
    p_raster.add_argument("--year", type=int, required=True)
    p_raster.add_argument("--periods", default="1-15")

    sub.add_parser("soils", help="fetch and cache SSURGO soil polygons")

    args = parser.parse_args(argv)

    if args.command == "serve":
        server.serve(args.host, args.port)
        return 0

    store = Store()
    if args.command == "compute":
        periods = [args.period]
    elif args.command == "raster":
        start, _, end = args.periods.partition("-")
        periods = list(range(int(start), int(end or start) + 1))
        for period in periods:
            print(f"building {args.year} P{period:02d} raster ...", flush=True)
            store._build_raster(args.year, period)
            print(f"  cached {args.year}/P{period:02d}", flush=True)
        return 0
    elif args.command == "soils":
        print("fetching SSURGO polygons ...", flush=True)
        print(store.build_soils(), flush=True)
        return 0
    else:
        start, _, end = args.periods.partition("-")
        periods = list(range(int(start), int(end or start) + 1))

    for period in periods:
        print(f"computing 2020 period {period:02d} ...", flush=True)
        store._compute_2020(period)
        print(f"  cached P{period:02d}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
