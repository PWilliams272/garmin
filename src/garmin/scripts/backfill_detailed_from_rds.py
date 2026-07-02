from __future__ import annotations

import argparse
import os
import socket

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy.engine import make_url

from garmin.io.curated_store import CuratedDataStore
from garmin.io.db_manager import DatabaseManager
from garmin.io.file_manager import FileManager
from garmin.io.models import (
    HeartRateDetailed,
    RespirationDetailed,
    SpO2Detailed,
    StepsDetailed,
)


load_dotenv()


DETAILED_MODELS = {
    "heart_rate_detailed": HeartRateDetailed,
    "respiration_detailed": RespirationDetailed,
    "spo2_detailed": SpO2Detailed,
    "steps_detailed": StepsDetailed,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill detailed Garmin RDS tables into curated parquet storage."
    )
    parser.add_argument(
        "--storage-target",
        choices=["local", "s3"],
        default="local",
        help="Where curated detailed outputs should be written.",
    )
    parser.add_argument(
        "--db-uri",
        default=None,
        help="Optional SQLAlchemy connection string for the legacy RDS database.",
    )
    parser.add_argument(
        "--use-rds-tunnel",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Route RDS access through the local SSH tunnel on 127.0.0.1:5433.",
    )
    parser.add_argument(
        "--rds-tunnel-host",
        default=os.environ.get("RDS_TUNNEL_HOST", "127.0.0.1"),
        help="Local host for the SSH-forwarded RDS tunnel.",
    )
    parser.add_argument(
        "--rds-tunnel-port",
        type=int,
        default=int(os.environ.get("RDS_TUNNEL_PORT", "5433")),
        help="Local port for the SSH-forwarded RDS tunnel.",
    )
    parser.add_argument(
        "--datasets",
        nargs="*",
        choices=sorted(DETAILED_MODELS),
        default=sorted(DETAILED_MODELS),
        help="Subset of detailed datasets to backfill.",
    )
    return parser


def assert_tunnel_ready(host: str, port: int) -> None:
    try:
        with socket.create_connection((host, port), timeout=2):
            return
    except OSError as exc:
        raise RuntimeError(
            f"RDS tunnel is not reachable on {host}:{port}. Start `rds-tunnel` in another terminal first."
        ) from exc


def resolve_db_uri(
    explicit_db_uri: str | None,
    *,
    use_rds_tunnel: bool,
    rds_tunnel_host: str,
    rds_tunnel_port: int,
) -> str:
    db_uri = explicit_db_uri or os.environ.get("GARMIN_RDS_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not db_uri:
        raise ValueError(
            "Set GARMIN_RDS_DATABASE_URL or DATABASE_URL, or pass --db-uri, before running the backfill."
        )

    if use_rds_tunnel:
        assert_tunnel_ready(rds_tunnel_host, rds_tunnel_port)
        url_obj = make_url(db_uri).set(host=rds_tunnel_host, port=rds_tunnel_port)
        return url_obj.render_as_string(hide_password=False)

    return db_uri


def backfill_dataset(dataset_name: str, db: DatabaseManager, curated_store: CuratedDataStore) -> None:
    df = db.get_df(dataset_name)
    if df.empty:
        print(f"No legacy rows found for {dataset_name}; skipping.")
        return

    backfill_df = df.copy()
    backfill_df["query_date"] = pd.to_datetime(backfill_df["query_date"]).dt.date
    if "date_time_utc" in backfill_df.columns:
        backfill_df["date_time_utc"] = pd.to_datetime(backfill_df["date_time_utc"], utc=True)
    if "date_pulled" in backfill_df.columns:
        backfill_df["date_pulled"] = pd.to_datetime(backfill_df["date_pulled"], errors="coerce").dt.date
    else:
        backfill_df["date_pulled"] = pd.NaT
    backfill_df["pull_status"] = "fetched"

    for query_date, day_df in backfill_df.groupby("query_date", sort=True):
        curated_store.write_detailed_day(
            dataset_name,
            query_date.isoformat(),
            day_df.reset_index(drop=True),
        )

    status_rows = (
        backfill_df.groupby("query_date", as_index=False)
        .agg(date_pulled=("date_pulled", "max"))
        .assign(pull_status="fetched")
    )
    curated_store.merge_detailed_status(dataset_name, status_rows)

    print(
        f"Backfilled {dataset_name}: {len(backfill_df)} rows across {status_rows.shape[0]} query dates."
    )


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    db = DatabaseManager(
        db_uri=resolve_db_uri(
            args.db_uri,
            use_rds_tunnel=args.use_rds_tunnel,
            rds_tunnel_host=args.rds_tunnel_host,
            rds_tunnel_port=args.rds_tunnel_port,
        )
    )
    file_manager = FileManager(environment="aws" if args.storage_target == "s3" else "local")
    curated_store = CuratedDataStore(file_manager=file_manager)

    for dataset_name in args.datasets:
        backfill_dataset(dataset_name, db, curated_store)


if __name__ == "__main__":
    main()
