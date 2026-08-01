from dotenv import load_dotenv
load_dotenv()

import argparse

from garmin.updaters import DataUpdater
from garmin.api import GarminSession
from garmin.io.curated_store import CuratedDataStore
from garmin.io.file_manager import FileManager


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill curated/activities/detail/<sport>_timeseries/ (FIT-based "
        "per-point data) for activities that already have a summary row but no detail "
        "file yet -- the daily updater only pulls details for newly-seen activities. "
        "Resumable: safe to re-run, only fetches ids still missing a detail file."
    )
    parser.add_argument(
        "--storage-target",
        choices=["local", "s3"],
        default="local",
        help="Where curated activity summaries are read from and detail outputs are written.",
    )
    parser.add_argument(
        "--limit-per-dataset",
        type=int,
        default=None,
        help="Cap the number of activities fetched per dataset this run (useful for "
        "spot-checking or working around a session timeout/rate limit in chunks).",
    )
    return parser


def main(argv: list[str] | None = None):
    args = build_parser().parse_args(argv)
    session = GarminSession()
    file_manager = FileManager(environment="aws" if args.storage_target == "s3" else "local")
    curated_store = CuratedDataStore(file_manager=file_manager)
    updater = DataUpdater(session=session, curated_store=curated_store)
    updater.backfill_all_activity_details(limit_per_dataset=args.limit_per_dataset)


if __name__ == "__main__":
    main()
