from __future__ import annotations

from datetime import date

import pandas as pd

from garmin.io.curated_store import CuratedDataStore
from garmin.io.file_manager import FileManager
from garmin.io.models import Steps
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