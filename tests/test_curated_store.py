from __future__ import annotations

from datetime import datetime
from datetime import date

import pandas as pd

import garmin.updaters as updaters_module
from garmin.io.curated_store import CuratedDataStore
from garmin.io.file_manager import FileManager
from garmin.io.models import HeartRateDetailed, Steps
from garmin.updaters import DataUpdater


def test_curated_store_merges_daily_rows_by_date(tmp_path) -> None:
    store = CuratedDataStore(
        file_manager=FileManager(environment="local", local_dir=str(tmp_path))
    )

    original = pd.DataFrame(
        [
            {"date": date(2024, 1, 1), "total_steps": 1000, "date_pulled": date(2024, 1, 5)},
            {"date": date(2024, 1, 2), "total_steps": 2000, "date_pulled": date(2024, 1, 5)},
        ]
    )
    incoming = pd.DataFrame(
        [
            {"date": date(2024, 1, 2), "total_steps": 2500, "date_pulled": date(2024, 1, 6)},
            {"date": date(2024, 1, 3), "total_steps": 3000, "date_pulled": date(2024, 1, 6)},
        ]
    )

    store.merge_daily("steps", original)
    merged = store.merge_daily("steps", incoming)

    assert list(merged["date"]) == [date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3)]
    assert list(merged["total_steps"]) == [1000, 2500, 3000]


def test_data_updater_can_write_daily_data_to_curated_store(tmp_path) -> None:
    store = CuratedDataStore(
        file_manager=FileManager(environment="local", local_dir=str(tmp_path))
    )
    calls: list[tuple[str, str, str]] = []

    class StubHealthPuller:
        def pull_data(self, data_type: str, start_date: str, end_date: str) -> pd.DataFrame:
            calls.append((data_type, start_date, end_date))
            return pd.DataFrame(
                [
                    {"date": "2024-01-01", "step_goal": 10000, "total_steps": 9000, "total_distance": 5.0},
                    {"date": "2024-01-02", "step_goal": 10000, "total_steps": 9500, "total_distance": 5.5},
                ]
            ).set_index(pd.to_datetime(["2024-01-01", "2024-01-02"]))

    updater = DataUpdater(
        session=object(),
        db_manager=object(),
        curated_store=store,
        health_puller=StubHealthPuller(),
        health_detailed_puller=object(),
    )

    updater.update(Steps, start_date="2024-01-01")

    saved = store.load_daily("steps")
    assert calls and calls[0][0] == "steps"
    assert list(pd.to_datetime(saved["date"]).dt.date) == [date(2024, 1, 1), date(2024, 1, 2)]
    assert list(saved["total_steps"]) == [9000, 9500]


def test_data_updater_can_resume_detailed_data_from_curated_status(tmp_path, monkeypatch) -> None:
    store = CuratedDataStore(
        file_manager=FileManager(environment="local", local_dir=str(tmp_path))
    )

    class FixedDateTime(datetime):
        @classmethod
        def today(cls) -> datetime:
            return cls(2024, 1, 4)

    monkeypatch.setattr(updaters_module, "datetime", FixedDateTime)

    store.merge_detailed_status(
        "heart_rate_detailed",
        pd.DataFrame(
            [
                {"query_date": date(2024, 1, 1), "date_pulled": date(2024, 1, 10), "pull_status": "fetched"},
                {"query_date": date(2024, 1, 2), "date_pulled": date(2024, 1, 10), "pull_status": "no_data"},
                {"query_date": date(2024, 1, 3), "date_pulled": date(2024, 1, 10), "pull_status": "denied"},
            ]
        ),
    )

    calls: list[list[str]] = []

    class StubHealthDetailedPuller:
        def __init__(self) -> None:
            self._last_pull_status: dict[str, list[str]] = {}

        def pull_data(self, data_type: str, dates: list[str]) -> pd.DataFrame:
            assert data_type == "heart_rate"
            calls.append(dates)
            self._last_pull_status = {
                "fetched": ["2024-01-03"],
                "no_data": ["2024-01-04"],
                "denied": [],
            }
            return pd.DataFrame(
                [
                    {
                        "query_date": "2024-01-03",
                        "date_time_utc": "2024-01-03T00:00:00Z",
                        "timestamp": 1704240000000,
                        "hr": 55,
                    }
                ]
            )

    updater = DataUpdater(
        session=object(),
        db_manager=object(),
        curated_store=store,
        health_puller=object(),
        health_detailed_puller=StubHealthDetailedPuller(),
    )

    updater.update(HeartRateDetailed, start_date="2024-01-01")

    assert calls == [["2024-01-03", "2024-01-04"]]

    status_df = store.load_detailed_status("heart_rate_detailed")
    status_df["query_date"] = pd.to_datetime(status_df["query_date"]).dt.date
    status_map = dict(zip(status_df["query_date"], status_df["pull_status"]))
    assert status_map[date(2024, 1, 1)] == "fetched"
    assert status_map[date(2024, 1, 2)] == "no_data"
    assert status_map[date(2024, 1, 3)] == "fetched"
    assert status_map[date(2024, 1, 4)] == "no_data"

    saved = store.load_daily("this-file-should-not-exist")
    assert saved.empty

    detailed_path = tmp_path / store.detailed_dataset_path("heart_rate_detailed", "2024-01-03")
    assert detailed_path.exists()