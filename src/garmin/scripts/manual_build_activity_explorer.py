from __future__ import annotations

import argparse

from garmin.io.curated_store import CuratedDataStore
from garmin.io.file_manager import FileManager
from garmin.prototypes.activity_explorer import write_activity_explorer_html


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a standalone interactive HTML sketch for lifting and climbing exploration."
    )
    parser.add_argument(
        "--storage-target",
        choices=["local", "s3"],
        default="local",
        help="Where curated activity data is read from.",
    )
    parser.add_argument(
        "--output",
        default="figures/activity_explorer_sketch.html",
        help="Where to write the generated HTML file.",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    file_manager = FileManager(environment="aws" if args.storage_target == "s3" else "local")
    store = CuratedDataStore(file_manager=file_manager)
    output_path = write_activity_explorer_html(args.output, store)
    print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()