from dotenv import load_dotenv
load_dotenv()

import argparse

from garmin.updaters import DataUpdater
from garmin.api import GarminSession
from garmin.io.curated_store import CuratedDataStore
from garmin.io.file_manager import FileManager


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Garmin updater.")
    parser.add_argument(
        "--storage-target",
        choices=["local", "s3"],
        default="local",
        help="Where curated updater outputs should be written.",
    )
    return parser


def main(argv: list[str] | None = None):
    args = build_parser().parse_args(argv)
    session = GarminSession()
    file_manager = FileManager(environment="aws" if args.storage_target == "s3" else "local")
    curated_store = CuratedDataStore(file_manager=file_manager)
    updater = DataUpdater(session=session, curated_store=curated_store)
    updater.update_all()

if __name__ == "__main__":
    main()