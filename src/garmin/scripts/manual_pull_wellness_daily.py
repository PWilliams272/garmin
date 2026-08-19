"""Backfill the daily scalar summaries from the wellness detail endpoints.

``HealthDetailedPuller`` already calls these endpoints every night, but only
reads their intraday arrays. The scalar fields in the same responses -- sleep
and waking respiration, sleep SpO2, daily min/max HR -- have never been stored.
The sleep-window aggregates in particular are what a sleep model wants, and are
less noisy than re-deriving them from the arrays.

Strictly per-day endpoints, so a full backfill is one request per day per
metric. Resumable: dates already stored are skipped unless ``--refresh``.

    python -m garmin.scripts.manual_pull_wellness_daily \
        --storage-target s3 --start-date 2022-12-04
"""

from dotenv import load_dotenv

load_dotenv()

import argparse  # noqa: E402
from datetime import datetime  # noqa: E402

import pandas as pd  # noqa: E402

from garmin.io.curated_store import CuratedDataStore  # noqa: E402
from garmin.io.file_manager import FileManager  # noqa: E402

#: Metric name -> curated dataset it is stored as.
DATASETS = {
    "heart_rate": "wellness_heart_rate",
    "respiration": "wellness_respiration",
    "spo2": "wellness_spo2",
}

#: These metrics only exist on the current watch.
DEFAULT_START = "2022-12-04"


def _known_dates(store: CuratedDataStore, dataset: str) -> set:
    existing = store.load_daily(dataset)
    if existing.empty or "date" not in existing.columns:
        return set()
    return set(pd.to_datetime(existing["date"]).dt.date)


def pull_wellness_daily(storage_target: str = "local",
                        metrics: tuple[str, ...] = tuple(DATASETS),
                        start_date: str = DEFAULT_START,
                        end_date: str | None = None,
                        refresh: bool = False) -> dict:
    """Pull each metric's daily summaries and merge into curated storage.

    Args:
        storage_target: ``"local"`` or ``"s3"``.
        metrics: Any of ``heart_rate``, ``respiration``, ``spo2``.
        start_date: First date.
        end_date: Last date, defaulting to today.
        refresh: Re-pull dates already stored, to fill newly mapped columns.

    Returns:
        Dataset name to row count after the merge.
    """
    from garmin.api import GarminSession
    from garmin.pullers.health_detailed import HealthDetailedPuller

    end_date = end_date or datetime.today().date().strftime("%Y-%m-%d")
    store = CuratedDataStore(
        FileManager(environment="aws" if storage_target == "s3" else "local")
    )
    puller = HealthDetailedPuller(GarminSession())
    results: dict[str, int] = {}
    for metric in metrics:
        dataset = DATASETS[metric]
        known = set() if refresh else _known_dates(store, dataset)
        frame = puller.pull_daily_summaries(metric, start_date, end_date, known_dates=known)
        if frame.empty:
            print(f"{dataset}: no new days ({len(known)} already stored).")
            results[dataset] = len(known)
            continue
        merged = store.merge_daily(dataset, frame)
        results[dataset] = len(merged)
        print(f"{dataset}: pulled {len(frame)} days, stored {len(merged)}, "
              f"{len(merged.columns)} columns.")
    return results


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--storage-target", choices=["local", "s3"], default="local")
    parser.add_argument("--metric", action="append", choices=sorted(DATASETS), default=None)
    parser.add_argument("--start-date", default=DEFAULT_START)
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args(argv)
    pull_wellness_daily(
        args.storage_target,
        metrics=tuple(args.metric) if args.metric else tuple(DATASETS),
        start_date=args.start_date, end_date=args.end_date, refresh=args.refresh,
    )


if __name__ == "__main__":
    main()
