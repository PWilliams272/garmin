from __future__ import annotations

import pandas as pd

from garmin.io.curated_store import CuratedDataStore
from garmin.io.file_manager import FileManager
from garmin.pullers.activities import ActivityPuller
from garmin.updaters import DataUpdater


class StubSession:
    """Returns a fixed page of activity-list results for any search call."""

    def __init__(self, activities: list[dict]) -> None:
        self.activities = activities
        self.calls: list[str] = []

    def get(self, url: str):
        self.calls.append(url)
        if "search/activities" in url:
            return self.activities
        return None


def _activity(activity_id: int, type_key: str, **overrides) -> dict:
    base = {
        "activityId": activity_id,
        "activityName": f"{type_key} activity",
        "activityType": {"typeKey": type_key},
        "startTimeLocal": "2024-06-01 08:00:00",
        "distance": 8046.72,  # 5 miles
        "duration": 1800.0,  # 30 min
        "averageSpeed": 4.4704,  # ~6:00/mi pace
        "averageHR": 140,
        "maxHR": 165,
        "elevationGain": 30.48,  # 100 ft
        "calories": 400,
    }
    base.update(overrides)
    return base


def test_pull_cardio_summary_extracts_common_fields_for_any_type() -> None:
    activities = [_activity(1, "cycling"), _activity(2, "running")]
    puller = ActivityPuller(StubSession(activities))

    df = puller.pull_cardio_summary("cycling", "2024-01-01", "2024-12-31")

    assert list(df["activity_id"]) == ["1"]
    row = df.iloc[0]
    assert row["distance_mi"] == 5.0
    assert row["duration_min"] == 30.0
    assert row["avg_hr"] == 140
    assert row["elevation_gain_ft"] == 100.0
    assert "cadence_spm" not in df.columns


def test_pull_cardio_summary_handles_missing_fields_gracefully() -> None:
    activities = [_activity(1, "hiit", distance=None, averageSpeed=None, elevationGain=None)]
    puller = ActivityPuller(StubSession(activities))

    df = puller.pull_cardio_summary("hiit", "2024-01-01", "2024-12-31")

    row = df.iloc[0]
    assert row["distance_mi"] is None
    assert row["pace_min_per_mile"] is None
    assert row["elevation_gain_ft"] is None
    assert row["duration_min"] == 30.0


def test_pull_running_summary_still_includes_cadence() -> None:
    activities = [_activity(1, "running", averageRunningCadenceInStepsPerMinute=172)]
    puller = ActivityPuller(StubSession(activities))

    df = puller.pull_running_summary("2024-01-01", "2024-12-31")

    assert df.iloc[0]["cadence_spm"] == 172


def test_activity_type_registry_has_expected_datasets() -> None:
    updater = DataUpdater(session=object(), db_manager=object(), curated_store=object())
    datasets = {entry["dataset"] for entry in updater._activity_type_registry()}

    assert {"running", "strength", "cycling", "hiking", "lap_swimming"}.issubset(datasets)
    running_entry = next(e for e in updater._activity_type_registry() if e["dataset"] == "running")
    assert "detail_fn" not in running_entry
    strength_entry = next(e for e in updater._activity_type_registry() if e["dataset"] == "strength")
    assert "detail_fn" in strength_entry


def test_update_activity_curated_writes_summary_and_detail(tmp_path) -> None:
    store = CuratedDataStore(file_manager=FileManager(environment="local", local_dir=str(tmp_path)))
    updater = DataUpdater(session=object(), db_manager=object(), curated_store=store)

    summary_calls: list[tuple[str, str]] = []
    detail_calls: list[str] = []

    def summary_fn(start_date, end_date):
        summary_calls.append((start_date, end_date))
        return pd.DataFrame([{"activity_id": "1", "date": "2024-06-01", "duration_min": 45.0}])

    def detail_fn(activity_id):
        detail_calls.append(activity_id)
        return pd.DataFrame([{"exercise": "bench_press", "reps": 8, "weight_lb": 135.0}])

    updater._update_activity_curated("cycling", summary_fn, detail_fn)

    assert summary_calls == [("2015-01-01", pd.Timestamp.today().strftime("%Y-%m-%d"))]
    assert detail_calls == ["1"]
    assert len(store.load_activity_summary("cycling")) == 1
    assert len(store.load_activity_detail("cycling", "1")) == 1


def test_update_activity_curated_resumes_from_last_date(tmp_path) -> None:
    store = CuratedDataStore(file_manager=FileManager(environment="local", local_dir=str(tmp_path)))
    store.merge_activity_summary(
        "cycling", pd.DataFrame([{"activity_id": "1", "date": "2024-06-01", "duration_min": 45.0}])
    )
    updater = DataUpdater(session=object(), db_manager=object(), curated_store=store)

    seen_start_dates: list[str] = []

    def summary_fn(start_date, end_date):
        seen_start_dates.append(start_date)
        return pd.DataFrame()

    updater._update_activity_curated("cycling", summary_fn)

    assert seen_start_dates == ["2024-06-01"]
