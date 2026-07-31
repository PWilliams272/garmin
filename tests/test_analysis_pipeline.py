from __future__ import annotations

import pandas as pd

from garmin.analysis.analysis_pipeline import (
    CARDIO_METRICS,
    STRENGTH_MIN_SESSIONS,
    analyze_cardio_activities,
    analyze_lifting,
)
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


def _seed_strength_sessions(store: CuratedDataStore, exercise: str, n_sessions: int) -> None:
    dates = pd.date_range("2024-01-01", periods=n_sessions, freq="2D")
    summary_rows = []
    for i, date in enumerate(dates):
        activity_id = str(i)
        summary_rows.append({"activity_id": activity_id, "date": date.date().isoformat(), "duration_min": 45.0})
        # Two sets per session: a heavier low-rep set and a lighter higher-rep
        # set, so top-1RM picks the max across sets, and volume sums both.
        store.write_activity_detail(
            "strength", activity_id,
            pd.DataFrame([
                {"exercise": exercise, "reps": 5, "weight_lb": 135.0 + i, "activity_id": activity_id},
                {"exercise": exercise, "reps": 10, "weight_lb": 95.0, "activity_id": activity_id},
            ]),
        )
    store.merge_activity_summary("strength", pd.DataFrame(summary_rows))


def test_analyze_lifting_writes_1rm_and_volume_for_exercises_over_threshold(tmp_path) -> None:
    store = CuratedDataStore(file_manager=FileManager(environment="local", local_dir=str(tmp_path)))
    _seed_strength_sessions(store, "bench_press", STRENGTH_MIN_SESSIONS + 2)

    analyze_lifting(store)

    points = store.load_analyzed_points("strength", "bench_press_1rm")
    trend = store.load_analyzed_trend("strength", "bench_press_1rm", kind="sts")
    volume_points = store.load_analyzed_points("strength", "bench_press_volume")
    assert not points.empty
    assert not trend.empty
    assert not volume_points.empty

    # Session 0: sets are (5 reps, 135 lb) and (10 reps, 95 lb). Epley 1RM for
    # each: 135*(1+5/30)=157.5, 95*(1+10/30)=126.67 -- top set (heavier/lower
    # reps) should win. Volume is the sum of both sets: 5*135 + 10*95 = 1625.
    first_session = points.sort_values("date").iloc[0]
    assert first_session["est_1rm"] == round(135.0 * (1 + 5 / 30), 10)
    first_volume = volume_points.sort_values("date").iloc[0]
    assert first_volume["volume_lb"] == 5 * 135.0 + 10 * 95.0


def test_analyze_lifting_skips_exercises_under_session_threshold(tmp_path) -> None:
    store = CuratedDataStore(file_manager=FileManager(environment="local", local_dir=str(tmp_path)))
    _seed_strength_sessions(store, "curl", STRENGTH_MIN_SESSIONS - 5)

    analyze_lifting(store)

    assert store.load_analyzed_points("strength", "curl_1rm").empty


def test_analyze_lifting_handles_no_strength_data(tmp_path) -> None:
    store = CuratedDataStore(file_manager=FileManager(environment="local", local_dir=str(tmp_path)))

    analyze_lifting(store)  # should not raise
