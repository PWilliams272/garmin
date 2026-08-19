"""Backfill the Garmin endpoints that were never being called.

Found in the 2026-08-19 endpoint sweep: race predictions, fitness age, the
94-field daily user summary, hydration/sweat loss, weekly intensity minutes,
personal records and registered devices. None of these had ever been pulled.

Race predictions come from a range endpoint (capped at 365 days, so longer
spans are chunked); the rest are one request per day and resumable -- dates
already stored are skipped unless ``--refresh``.

    python -m garmin.scripts.manual_pull_user_metrics \
        --storage-target s3 --start-date 2022-12-04
"""

from dotenv import load_dotenv

load_dotenv()

import argparse  # noqa: E402
from datetime import datetime  # noqa: E402

import pandas as pd  # noqa: E402

from garmin.io.curated_store import CuratedDataStore  # noqa: E402
from garmin.io.file_manager import FileManager  # noqa: E402
from garmin.updaters import USER_METRIC_PULLERS, USER_METRIC_SNAPSHOTS  # noqa: E402

#: These metrics only exist on the current watch.
DEFAULT_START = "2022-12-04"


def _known_dates(store: CuratedDataStore, dataset: str) -> set:
    existing = store.load_daily(dataset)
    if existing.empty or "date" not in existing.columns:
        return set()
    return set(pd.to_datetime(existing["date"]).dt.date)


def pull_user_metrics(storage_target: str = "local",
                      datasets: tuple[str, ...] | None = None,
                      start_date: str = DEFAULT_START,
                      end_date: str | None = None,
                      refresh: bool = False) -> dict:
    """Pull each metric and merge it into curated storage.

    Args:
        storage_target: ``"local"`` or ``"s3"``.
        datasets: Subset to pull, defaulting to everything.
        start_date: First date for the dated metrics.
        end_date: Last date, defaulting to today.
        refresh: Re-pull dates already stored, to fill newly mapped columns.

    Returns:
        Dataset name to the row count now stored.
    """
    from garmin.api import GarminSession
    from garmin.pullers.user_metrics import UserMetricsPuller

    end_date = end_date or datetime.today().date().strftime("%Y-%m-%d")
    store = CuratedDataStore(
        FileManager(environment="aws" if storage_target == "s3" else "local")
    )
    puller = UserMetricsPuller(GarminSession())
    wanted = set(datasets) if datasets else None
    results: dict[str, int] = {}

    def selected(name: str) -> bool:
        return wanted is None or name in wanted

    if selected("race_predictions"):
        frame = puller.pull_race_predictions(start_date, end_date)
        merged = store.merge_daily("race_predictions", frame) if not frame.empty else frame
        results["race_predictions"] = len(merged)
        print(f"race_predictions: pulled {len(frame)}, stored {len(merged)}.")

    for dataset, method in USER_METRIC_PULLERS.items():
        if not selected(dataset):
            continue
        known = set() if refresh else _known_dates(store, dataset)
        frame = getattr(puller, method)(start_date, end_date, known_dates=known)
        if frame.empty:
            print(f"{dataset}: no new days ({len(known)} already stored).")
            results[dataset] = len(known)
            continue
        merged = store.merge_daily(dataset, frame)
        results[dataset] = len(merged)
        print(f"{dataset}: pulled {len(frame)} days, stored {len(merged)}, "
              f"{len(merged.columns)} columns.")

    for name, method in USER_METRIC_SNAPSHOTS.items():
        if not selected(name):
            continue
        frame = getattr(puller, method)()
        if frame.empty:
            print(f"{name}: nothing returned.")
            continue
        store.write_metadata(name, frame)
        results[name] = len(frame)
        print(f"{name}: {len(frame)} rows.")

    return results


def main(argv: list[str] | None = None):
    all_names = ["race_predictions", *USER_METRIC_PULLERS, *USER_METRIC_SNAPSHOTS]
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--storage-target", choices=["local", "s3"], default="local")
    parser.add_argument("--dataset", action="append", choices=all_names, default=None,
                        help="Repeatable. Defaults to all.")
    parser.add_argument("--start-date", default=DEFAULT_START)
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args(argv)
    pull_user_metrics(
        args.storage_target,
        datasets=tuple(args.dataset) if args.dataset else None,
        start_date=args.start_date, end_date=args.end_date, refresh=args.refresh,
    )


if __name__ == "__main__":
    main()
