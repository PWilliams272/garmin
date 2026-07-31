from __future__ import annotations

import pandas as pd

from garmin.analysis.analysis_pipeline import CARDIO_METRICS, analyze_cardio_activities
from garmin.io.curated_store import CuratedDataStore
from garmin.io.file_manager import FileManager


def _cycling_summary(n: int = 10) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=n, freq="3D")
    return pd.DataFrame({
        "activity_id": [str(i) for i in range(n)],
        "date": dates,
        "distance_mi": [10.0 + i * 0.2 for i in range(n)],
        "duration_min": [45.0] * n,
        "pace_min_per_mile": [6.0 - i * 0.02 for i in range(n)],
        "avg_hr": [140] * n,
    })


def test_analyze_cardio_activities_writes_points_and_trend_for_non_running_sports(tmp_path) -> None:
    store = CuratedDataStore(file_manager=FileManager(environment="local", local_dir=str(tmp_path)))
    store.merge_activity_summary("cycling", _cycling_summary())

    analyze_cardio_activities(store)

    for metric in CARDIO_METRICS:
        points = store.load_analyzed_points("cycling", metric)
        trend = store.load_analyzed_trend("cycling", metric, kind="gp")
        assert not points.empty
        assert not trend.empty


def test_analyze_cardio_activities_skips_running_and_strength(tmp_path) -> None:
    store = CuratedDataStore(file_manager=FileManager(environment="local", local_dir=str(tmp_path)))
    running = _cycling_summary().assign(cadence_spm=170)
    store.merge_activity_summary("running", running)
    store.merge_activity_summary("strength", pd.DataFrame([
        {"activity_id": "1", "date": "2024-01-01", "duration_min": 45.0},
    ]))

    analyze_cardio_activities(store)

    # analyze_running/analyze_lifting own these datasets -- this function
    # should leave them untouched (no distance_mi points written under
    # "running" by the generic cardio path).
    assert store.load_analyzed_points("running", "distance_mi").empty


def test_analyze_cardio_activities_handles_no_data_for_any_sport(tmp_path) -> None:
    store = CuratedDataStore(file_manager=FileManager(environment="local", local_dir=str(tmp_path)))

    analyze_cardio_activities(store)  # should not raise
