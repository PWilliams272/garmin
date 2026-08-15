"""Re-pull activity *summaries* over full history to fill columns added later.

`_update_activity_curated` resumes from the last stored activity date, so a
column added to a summary puller only ever reaches activities recorded after
that change. Everything older keeps whatever schema it was written with, and
nothing in the daily loop will ever revisit it.

That is how `start_time` ended up null for 1061 of 1062 running activities and
489 of 490 strength ones: it was added to `pull_running_summary` and
`pull_strength_summary` on 2026-07-31, by which point both datasets already
held years of history. The sports that came through `pull_cardio_summary` --
introduced the same day, so pulled fresh -- are 100% populated.

`merge_activity_summary` de-duplicates on `activity_id` keeping the last row,
so re-pulling a date range simply overwrites those rows with the current
schema. Nothing is lost.

Report what is missing, without touching Garmin::

    python -m garmin.scripts.manual_backfill_activity_summaries \
        --storage-target s3 --report-only

Backfill one dataset (needs Garmin credentials)::

    python -m garmin.scripts.manual_backfill_activity_summaries \
        --storage-target s3 --dataset running --apply
"""

from dotenv import load_dotenv

load_dotenv()

import argparse  # noqa: E402
from datetime import datetime  # noqa: E402

import pandas as pd  # noqa: E402

from garmin.io.curated_store import CuratedDataStore  # noqa: E402
from garmin.io.file_manager import FileManager  # noqa: E402

#: Columns whose absence means the row predates a puller change worth
#: backfilling. `training_load` stands in for the whole Garmin effort block
#: (training effect, zone times, intensity minutes) since they arrive together.
_SCHEMA_COLUMNS = ("start_time", "training_load")


def report_gaps(store: CuratedDataStore, datasets: list[str]) -> pd.DataFrame:
    """Report, per dataset, how many summary rows are missing schema columns.

    Args:
        store: Curated store to read summaries from.
        datasets: Dataset names to inspect.

    Returns:
        One row per dataset/column with the null count and the date range that
        would need re-pulling.
    """
    rows = []
    for dataset in datasets:
        frame = store.load_activity_summary(dataset)
        if frame.empty:
            continue
        dates = pd.to_datetime(frame["date"])
        for column in _SCHEMA_COLUMNS:
            if column not in frame.columns:
                missing = len(frame)
                gap_dates = dates
            else:
                mask = frame[column].isna()
                missing = int(mask.sum())
                gap_dates = dates[mask]
            rows.append(
                {
                    "dataset": dataset,
                    "column": column,
                    "missing": missing,
                    "rows": len(frame),
                    "pct": round(100 * missing / len(frame), 1),
                    "first": gap_dates.min() if missing else pd.NaT,
                    "last": gap_dates.max() if missing else pd.NaT,
                }
            )
    return pd.DataFrame(rows)


def backfill_summaries(
    storage_target: str = "local",
    dataset: str | None = None,
    apply: bool = False,
    start_date: str = "2015-01-01",
) -> pd.DataFrame:
    """Re-pull and merge activity summaries across the full date range.

    Args:
        storage_target: ``"local"`` or ``"s3"``.
        dataset: Restrict to one dataset; all registered ones if None.
        apply: When False, report and contact nothing.
        start_date: Earliest date to re-pull.

    Returns:
        The gap report, as produced by :func:`report_gaps`.

    Raises:
        RuntimeError: If ``dataset`` is not a registered activity type.
    """
    store = CuratedDataStore(
        FileManager(environment="aws" if storage_target == "s3" else "local")
    )

    # The registry needs a live puller to build, so the report path uses the
    # dependency-free dataset list instead -- reporting must not require
    # Garmin credentials.
    from garmin.datasets import ACTIVITY_DATASETS

    if dataset is not None and dataset not in ACTIVITY_DATASETS:
        raise RuntimeError(
            f"unknown dataset {dataset!r}; known: {sorted(ACTIVITY_DATASETS)}"
        )
    targets = [dataset] if dataset else sorted(ACTIVITY_DATASETS)

    report = report_gaps(store, targets)
    if not apply:
        print("REPORT ONLY -- nothing pulled, nothing written.")
        print(report.to_string(index=False))
        return report

    from garmin.api import GarminSession
    from garmin.updaters import DataUpdater

    session = GarminSession()
    updater = DataUpdater(session, curated_store=store)
    entries = {e["dataset"]: e for e in updater._activity_type_registry()}

    today = datetime.today().date().strftime("%Y-%m-%d")
    for name in targets:
        summary_fn = entries[name]["summary_fn"]
        # Deliberately bypasses _update_activity_curated, which would override
        # start_date with the last stored date -- the exact behaviour that
        # leaves history stale.
        pulled = summary_fn(start_date, today)
        if pulled.empty:
            print(f"{name}: nothing returned.")
            continue
        merged = store.merge_activity_summary(name, pulled)
        print(f"{name}: re-pulled {len(pulled)} rows, summary now {len(merged)}.")

    after = report_gaps(store, targets)
    print("\nAfter backfill:")
    print(after.to_string(index=False))
    return after


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--storage-target", choices=["local", "s3"], default="local")
    parser.add_argument("--dataset", default=None, help="Only this dataset.")
    parser.add_argument("--start-date", default="2015-01-01")
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Default behaviour, accepted explicitly for readable command lines.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually re-pull from Garmin and write. Needs credentials; the "
        "report path does not.",
    )
    return parser


def main(argv: list[str] | None = None):
    args = build_parser().parse_args(argv)
    backfill_summaries(
        args.storage_target,
        dataset=args.dataset,
        apply=args.apply,
        start_date=args.start_date,
    )


if __name__ == "__main__":
    main()
