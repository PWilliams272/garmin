"""One-off repair for placeholder zeros in curated health_stats.

`HealthPuller._post_process_weight` now nulls the body-composition zeros that
Garmin returns when a scale reports only a weight (see tests/test_health_puller.py),
but that fix only applies to newly-pulled days. The nightly updater resumes from
the last stored date and never revisits history, so rows written before the fix
keep their zeros indefinitely. This script rewrites them in place.

Zero is physiologically impossible for every affected field, so replacing it with
null cannot destroy a real measurement -- the repair is lossless.

Dry run (default) against S3::

    python -m garmin.scripts.manual_repair_health_stats_zeros --storage-target s3

Then, once the reported counts look right::

    python -m garmin.scripts.manual_repair_health_stats_zeros \
        --storage-target s3 --apply
"""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from garmin.io.curated_store import CuratedDataStore
from garmin.io.file_manager import FileManager
from garmin.pullers.health import HealthPuller

#: Derived from body_fat, so it inherits the placeholder and needs the same repair.
_DERIVED_FIELDS = ("fat_mass",)

_REPAIR_FIELDS = HealthPuller._COMPOSITION_FIELDS + _DERIVED_FIELDS


def repair_health_stats(storage_target: str = "local", apply: bool = False) -> pd.DataFrame:
    """Replace placeholder zeros in curated ``health_stats`` with null.

    Args:
        storage_target: ``"local"`` or ``"s3"``.
        apply: When False (the default) report what would change and write
            nothing.

    Returns:
        A frame with one row per repaired field and the number of zeros found.

    Raises:
        RuntimeError: If the curated ``health_stats`` dataset is empty.
    """
    store = CuratedDataStore(
        FileManager(environment="aws" if storage_target == "s3" else "local")
    )
    frame = store.load_daily("health_stats")
    if frame.empty:
        raise RuntimeError("curated health_stats is empty -- nothing to repair")

    frame = frame.copy()
    dates = pd.to_datetime(frame["date"])
    report = []
    for field in _REPAIR_FIELDS:
        if field not in frame.columns:
            continue
        mask = frame[field] == 0
        affected = dates[mask]
        report.append(
            {
                "field": field,
                "zeros": int(mask.sum()),
                "first": affected.min() if mask.any() else pd.NaT,
                "last": affected.max() if mask.any() else pd.NaT,
            }
        )
        if mask.any():
            frame.loc[mask, field] = np.nan

    summary = pd.DataFrame(report)
    total = int(summary["zeros"].sum()) if not summary.empty else 0

    if not apply:
        print(f"DRY RUN -- would null {total} placeholder zero(s). Re-run with --apply.")
        print(summary.to_string(index=False))
        return summary

    if total == 0:
        print("Nothing to repair.")
        return summary

    store.file_manager.write_df(
        store._prepare_for_parquet(frame),
        store.daily_dataset_path("health_stats"),
        format="parquet",
    )
    print(f"Repaired {total} placeholder zero(s) in {storage_target} health_stats.")
    print(summary.to_string(index=False))
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--storage-target", choices=["local", "s3"], default="local")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write. Without it the script only reports.",
    )
    return parser


def main(argv: list[str] | None = None):
    args = build_parser().parse_args(argv)
    repair_health_stats(args.storage_target, apply=args.apply)


if __name__ == "__main__":
    main()
