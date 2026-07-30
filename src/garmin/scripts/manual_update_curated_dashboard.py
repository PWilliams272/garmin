from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

import argparse

from garmin.dashboard_curated import build_curated_dashboard_artifacts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build curated Bokeh dashboard artifacts.")
    parser.add_argument(
        "--source",
        choices=["local", "s3"],
        default="local",
        help="Where curated daily parquet inputs are read from.",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    output_dir = build_curated_dashboard_artifacts(source=args.source)
    print(f"Wrote curated dashboard artifacts to {output_dir}")


if __name__ == "__main__":
    main()