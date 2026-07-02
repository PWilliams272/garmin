from __future__ import annotations

from datetime import date

import pandas as pd

import garmin.scripts.backfill_detailed_from_rds as backfill_script


def test_backfill_detailed_from_rds_writes_days_and_status(monkeypatch) -> None:
    captured: dict[str, object] = {"written_days": [], "status_frames": []}

    class StubDatabaseManager:
        def __init__(self, db_uri=None, **kwargs):
            captured["db_uri"] = db_uri

        def get_df(self, table_name: str) -> pd.DataFrame:
            if table_name != "heart_rate_detailed":
                return pd.DataFrame()
            return pd.DataFrame(
                [
                    {
                        "id": 1,
                        "query_date": date(2024, 1, 2),
                        "date_time_utc": "2024-01-02T00:00:00Z",
                        "timestamp": 1704153600000,
                        "hr": 54,
                        "date_pulled": date(2024, 1, 3),
                    },
                    {
                        "id": 2,
                        "query_date": date(2024, 1, 2),
                        "date_time_utc": "2024-01-02T00:02:00Z",
                        "timestamp": 1704153720000,
                        "hr": 55,
                        "date_pulled": date(2024, 1, 3),
                    },
                    {
                        "id": 3,
                        "query_date": date(2024, 1, 3),
                        "date_time_utc": "2024-01-03T00:00:00Z",
                        "timestamp": 1704240000000,
                        "hr": 56,
                        "date_pulled": date(2024, 1, 4),
                    },
                ]
            )

    class StubFileManager:
        def __init__(self, environment=None, **kwargs):
            captured["environment"] = environment

    class StubCuratedStore:
        def __init__(self, file_manager):
            captured["file_manager"] = file_manager

        def write_detailed_day(self, dataset: str, query_date: str, df: pd.DataFrame) -> None:
            captured["written_days"].append((dataset, query_date, len(df), list(df["pull_status"].unique())))

        def merge_detailed_status(self, dataset: str, df: pd.DataFrame) -> None:
            captured["status_frames"].append((dataset, df.copy()))

    monkeypatch.setattr(backfill_script, "DatabaseManager", StubDatabaseManager)
    monkeypatch.setattr(backfill_script, "FileManager", StubFileManager)
    monkeypatch.setattr(backfill_script, "CuratedDataStore", StubCuratedStore)
    monkeypatch.setattr(backfill_script, "assert_tunnel_ready", lambda host, port: None)

    backfill_script.main([
        "--storage-target",
        "s3",
        "--db-uri",
        "postgresql://user:pass@db.example.com:5432/postgres",
        "--datasets",
        "heart_rate_detailed",
    ])

    assert captured["db_uri"] == "postgresql://user:pass@127.0.0.1:5433/postgres"
    assert captured["environment"] == "aws"
    assert captured["written_days"] == [
        ("heart_rate_detailed", "2024-01-02", 2, ["fetched"]),
        ("heart_rate_detailed", "2024-01-03", 1, ["fetched"]),
    ]

    dataset_name, status_df = captured["status_frames"][0]
    assert dataset_name == "heart_rate_detailed"
    assert list(status_df["query_date"].astype(str)) == ["2024-01-02", "2024-01-03"]
    assert list(status_df["pull_status"]) == ["fetched", "fetched"]
