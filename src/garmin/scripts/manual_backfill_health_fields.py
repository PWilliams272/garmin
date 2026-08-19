"""Re-pull daily health datasets over full history to fill newly mapped columns.

The nightly updater resumes from the last stored date, so a field added to a
puller's ``mapping`` only ever reaches days pulled *after* the change. Every
earlier row keeps the schema it was written with, and nothing in the daily loop
revisits it. That is the same failure that left ``start_time`` null on 1061 of
1062 running activities.

``merge_daily`` de-duplicates on ``date`` keeping the last row, so re-pulling a
range simply rewrites those days with the current schema. Nothing is lost, but
take a snapshot first if the columns being replaced matter.

Report which datasets are missing which columns::

    python -m garmin.scripts.manual_backfill_health_fields --storage-target s3 --report-only

Backfill one dataset::

    python -m garmin.scripts.manual_backfill_health_fields \
        --storage-target s3 --dataset sleep --apply
"""

from dotenv import load_dotenv

load_dotenv()

import argparse  # noqa: E402
from datetime import datetime  # noqa: E402

import pandas as pd  # noqa: E402

from garmin.io.curated_store import CuratedDataStore  # noqa: E402
from garmin.io.file_manager import FileManager  # noqa: E402

#: Curated dataset name -> (puller config key, earliest date worth requesting).
#: The start dates differ because the metrics themselves do: body composition
#: goes back to the first scale, the wellness metrics to the current watch.
DATASETS = {
    "health_stats": ("weight", "2015-01-01"),
    "sleep": ("sleep", "2015-01-01"),
    "stress": ("stress", "2022-12-01"),
    "steps": ("steps", "2015-01-01"),
    "heart_rate": ("heart_rate", "2022-12-01"),
    "body_battery": ("body_battery", "2022-12-01"),
    "hrv": ("hrv", "2022-12-01"),
    "respiration": ("respiration", "2022-12-01"),
}

#: Columns added on 2026-08-18, used to detect rows predating the change.
NEW_COLUMNS = {
    "health_stats": ("source_type", "timestamp_gmt", "weight_delta"),
    "sleep": ("avg_sleep_heart_rate", "avg_overnight_hrv"),
    "stress": ("medium_stress_duration",),
}


def report_gaps(store: CuratedDataStore) -> pd.DataFrame:
    """Per dataset and new column, how many stored rows are missing it."""
    rows = []
    for dataset, columns in NEW_COLUMNS.items():
        frame = store.load_daily(dataset)
        for column in columns:
            missing = len(frame) if column not in frame.columns else int(frame[column].isna().sum())
            rows.append({
                "dataset": dataset, "column": column,
                "missing": missing, "rows": len(frame),
                "pct": round(100 * missing / len(frame), 1) if len(frame) else None,
            })
    return pd.DataFrame(rows)


def normalize(frame: pd.DataFrame, today) -> pd.DataFrame:
    """Give the frame a plain ``date`` column, matching the updater's shape."""
    out = frame.copy()
    if isinstance(out.index, pd.DatetimeIndex) and "date" not in out.columns:
        out = out.reset_index()
        index_column = out.columns[0]
        out["date"] = pd.to_datetime(out[index_column]).dt.date
        if index_column != "date":
            out = out.drop(columns=[index_column])
    else:
        out["date"] = pd.to_datetime(out["date"]).dt.date
    out["date_pulled"] = today
    return out


def backfill(dataset: str, storage_target: str = "local",
             start_date: str | None = None) -> int:
    """Re-pull one dataset over full history and merge it into curated storage.

    Args:
        dataset: Curated dataset name, a key of :data:`DATASETS`.
        storage_target: ``"local"`` or ``"s3"``.
        start_date: Override the default earliest date.

    Returns:
        Number of rows stored after the merge.
    """
    from garmin.api import GarminSession
    from garmin.pullers.health import HealthPuller

    config_key, default_start = DATASETS[dataset]
    store = CuratedDataStore(
        FileManager(environment="aws" if storage_target == "s3" else "local")
    )
    puller = HealthPuller(GarminSession())
    today = datetime.today().date()
    frame = puller.pull_data(
        config_key,
        start_date=start_date or default_start,
        end_date=today.strftime("%Y-%m-%d"),
    )
    if frame.empty:
        print(f"{dataset}: Garmin returned nothing.")
        return len(store.load_daily(dataset))
    merged = store.merge_daily(dataset, normalize(frame, today))
    print(f"{dataset}: pulled {len(frame)}, stored {len(merged)}, "
          f"{len(merged.columns)} columns.")
    return len(merged)


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--storage-target", choices=["local", "s3"], default="local")
    parser.add_argument("--dataset", choices=sorted(DATASETS), default=None,
                        help="Default: every dataset with newly mapped columns.")
    parser.add_argument("--start-date", default=None)
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--apply", action="store_true",
                        help="Required to write. Without it this only reports.")
    args = parser.parse_args(argv)

    store = CuratedDataStore(
        FileManager(environment="aws" if args.storage_target == "s3" else "local")
    )
    print(report_gaps(store).to_string(index=False))
    if args.report_only or not args.apply:
        print("\nNothing written. Pass --apply to backfill.")
        return
    targets = [args.dataset] if args.dataset else sorted(NEW_COLUMNS)
    for dataset in targets:
        backfill(dataset, args.storage_target, args.start_date)


if __name__ == "__main__":
    main()
