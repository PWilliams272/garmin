from dotenv import load_dotenv
load_dotenv()

import argparse

from garmin.analysis.analysis_pipeline import analyze_all
from garmin.io.curated_store import CuratedDataStore
from garmin.io.file_manager import FileManager


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run quality classification + GP trend fitting on curated activity data, "
        "writing results to the curated/analyzed/ layer so the web app never computes on request."
    )
    parser.add_argument(
        "--storage-target",
        choices=["local", "s3"],
        default="local",
        help="Where curated activity inputs are read from and analyzed outputs are written.",
    )
    return parser


def main(argv: list[str] | None = None):
    args = build_parser().parse_args(argv)
    fm = FileManager(environment="aws" if args.storage_target == "s3" else "local")
    curated_store = CuratedDataStore(file_manager=fm)
    analyze_all(curated_store)


if __name__ == "__main__":
    main()
