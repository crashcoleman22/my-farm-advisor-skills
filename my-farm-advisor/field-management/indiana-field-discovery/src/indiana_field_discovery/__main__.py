"""CLI dispatcher: ``python -m indiana_field_discovery <stage>``."""

from __future__ import annotations

import argparse
import sys

from . import county_fields, crop_history, ndvi, ndvi_summary, soils


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="indiana_field_discovery",
        description="Reusable US county agricultural field discovery (USDA CSB, SSURGO, CDL, NDVI).",
    )
    subparsers = parser.add_subparsers(dest="stage", required=True)
    county_fields.add_parser(subparsers)
    soils.add_parser(subparsers)
    crop_history.add_parser(subparsers)
    ndvi.add_parser(subparsers)
    ndvi_summary.add_parser(subparsers)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
