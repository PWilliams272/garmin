"""Pull Garmin's own training metrics: readiness, status, VO2max, HR zones.

These are Garmin's watch-computed views of load and recovery. Keeping them lets
a hand-rolled training-load metric be scored against a validated one.

Readiness and training status are **per-day endpoints** -- Garmin offers no
range form (verified 2026-08-15) -- so a full backfill is one request per day
over ~1000 days. Both are resumable: dates already stored are skipped, so a run
interrupted by a rate limit or timeout can simply be re-run. VO2max and HR zones
are cheap by comparison, one request each.

Backfill everything to S3::

    python -m garmin.scripts.manual_pull_training_metrics \
        --storage-target s3 --start-date 2022-12-04

Just top up the last week::

    python -m garmin.scripts.manual_pull_training_metrics \
        --storage-target s3 --start-date 2026-08-01 --metric readiness
"""

from dotenv import load_dotenv

load_dotenv()

import argparse  # noqa: E402
from datetime import datetime  # noqa: E402

import pandas as pd  # noqa: E402

from garmin.io.curated_store import CuratedDataStore  # noqa: E402
from garmin.io.file_manager import FileManager  # noqa: E402

#: Earliest date worth requesting. The current watch came online 2022-12-04 and
#: none of these metrics exist before it.
DEFAULT_START = "2022-12-04"

_PER_DAY_METRICS = ("readiness", "status")
_ALL_METRICS = ("zones", "vo2max", *_PER_DAY_METRICS)


def _known_dates(store: CuratedDataStore, dataset: str) -> set:
    """Dates already stored, so a resumed run skips them."""
    existing = store.load_daily(dataset)
    if existing.empty or "date" not in existing.columns:
        return set()
    return set(pd.to_datetime(existing["date"]).dt.date)


def pull_training_metrics(
    storage_target: str = "local",
    metrics: tuple[str, ...] = _ALL_METRICS,
    start_date: str = DEFAULT_START,
    end_date: str | None = None,
    refresh: bool = False,
) -> dict:
    """Pull the requested metrics and merge them into curated storage.

    Args:
        storage_target: ``"local"`` or ``"s3"``.
        metrics: Any of ``zones``, ``vo2max``, ``readiness``, ``status``.
        start_date: First date for the dated metrics.
        end_date: Last date, defaulting to today.
        refresh: Re-pull dates already stored. Needed after a mapping change --
            the resume logic skips known dates, so new columns would otherwise
            only ever reach days pulled after the change.

    Returns:
        Metric name to the number of rows now stored.
    """
    from garmin.api import GarminSession
    from garmin.pullers.training import TrainingPuller

    end_date = end_date or datetime.today().date().strftime("%Y-%m-%d")
    store = CuratedDataStore(
        FileManager(environment="aws" if storage_target == "s3" else "local")
    )
    puller = TrainingPuller(GarminSession())
    results: dict[str, int] = {}

    if "zones" in metrics:
        zones = puller.pull_hr_zones()
        store.write_hr_zones(zones)
        results["hr_zones"] = len(zones)
        print(f"hr_zones: {len(zones)} sports.")

    if "vo2max" in metrics:
        frame = puller.pull_vo2max(start_date, end_date)
        if frame.empty:
            print("vo2max: nothing returned (only updates on qualifying activities).")
            results["vo2max"] = len(store.load_daily("vo2max"))
        else:
            merged = store.merge_daily("vo2max", frame)
            results["vo2max"] = len(merged)
            print(f"vo2max: pulled {len(frame)}, stored {len(merged)}.")

    per_day = {
        "readiness": ("training_readiness", puller.pull_training_readiness),
        "status": ("training_status", puller.pull_training_status),
    }
    for metric, (dataset, pull_fn) in per_day.items():
        if metric not in metrics:
            continue
        known = set() if refresh else _known_dates(store, dataset)
        frame = pull_fn(start_date, end_date, known_dates=known)
        if frame.empty:
            print(f"{dataset}: no new days ({len(known)} already stored).")
            results[dataset] = len(known)
            continue
        merged = store.merge_daily(dataset, frame)
        results[dataset] = len(merged)
        print(f"{dataset}: pulled {len(frame)} new days, stored {len(merged)}.")

    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--storage-target", choices=["local", "s3"], default="local")
    parser.add_argument(
        "--metric", action="append", choices=list(_ALL_METRICS), default=None,
        help="Repeatable. Defaults to all four.",
    )
    parser.add_argument("--start-date", default=DEFAULT_START)
    parser.add_argument("--end-date", default=None)
    parser.add_argument(
        "--refresh", action="store_true",
        help="Re-pull dates already stored, to backfill newly mapped columns.",
    )
    return parser


def main(argv: list[str] | None = None):
    args = build_parser().parse_args(argv)
    pull_training_metrics(
        args.storage_target,
        metrics=tuple(args.metric) if args.metric else _ALL_METRICS,
        start_date=args.start_date,
        end_date=args.end_date,
        refresh=args.refresh,
    )


if __name__ == "__main__":
    main()
