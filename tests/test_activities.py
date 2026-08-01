from __future__ import annotations

import pandas as pd

from garmin.io.curated_store import CuratedDataStore
from garmin.io.file_manager import FileManager
from garmin.pullers.activities import METERS_PER_FOOT, ActivityPuller
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


class StubDetailSession:
    """Returns a fixed /details response for any activity-detail call."""

    def __init__(self, detail_response) -> None:
        self.detail_response = detail_response

    def get(self, url: str):
        if url.endswith("/details"):
            return self.detail_response
        return None


def test_get_activity_gps_extracts_polyline_points() -> None:
    detail_response = {
        "geoPolylineDTO": {
            "polyline": [
                {"lat": 40.0, "lon": -105.0, "altitude": 1600.0, "time": 1000},
                {"lat": 40.001, "lon": -105.001, "altitude": 1605.0, "time": 1005},
            ]
        }
    }
    puller = ActivityPuller(StubDetailSession(detail_response))

    df = puller.get_activity_gps("123")

    assert list(df["lat"]) == [40.0, 40.001]
    assert list(df["lon"]) == [-105.0, -105.001]
    assert df.iloc[0]["elevation_ft"] == round(1600.0 / 0.3048, 1)


def test_get_activity_gps_handles_missing_polyline_gracefully() -> None:
    puller = ActivityPuller(StubDetailSession({}))

    df = puller.get_activity_gps("123")

    assert df.empty


# metricDescriptors order + one real point, taken verbatim from a live pull
# against a real running activity (2026-07-31, activity 23528115932) --
# see get_activity_timeseries's docstring.
REAL_METRIC_DESCRIPTORS = [
    {"metricsIndex": i, "key": key} for i, key in enumerate([
        "directRunCadence", "directFractionalCadence", "directAvailableStamina", "directBodyBattery",
        "sumDistance", "directPower", "directGradeAdjustedSpeed", "directElevation", "directDoubleCadence",
        "directLongitude", "sumDuration", "directHeartRate", "directTimestamp", "directSpeed",
        "sumElapsedDuration", "directLatitude", "sumMovingDuration", "directPotentialStamina",
        "directVerticalSpeed", "directGroundContactTime", "directStrideLength", "directVerticalOscillation",
        "directVerticalRatio", "sumAccumulatedPower", "directPerformanceCondition",
    ])
]
REAL_METRIC_POINT = {
    "metrics": [
        80.0, 0.5, 91.0, 54.0, 1973.5899658203125, 488.0, 3.683000087738037, 63.20000076293945, 161.0,
        -118.4442356787622, 569.0, 130.0, 1783540789000.0, 3.5460000038146973, 606.0, 34.03996204957366,
        568.0, 91.0, 0.20000000298023224, 272.0, 132.9, 10.15999984741211, 7.639999866485596, 250346.0, None,
    ]
}


def test_get_activity_timeseries_parses_real_response_shape() -> None:
    detail_response = {
        "metricDescriptors": REAL_METRIC_DESCRIPTORS,
        "activityDetailMetrics": [REAL_METRIC_POINT],
    }
    puller = ActivityPuller(StubDetailSession(detail_response))

    df = puller.get_activity_timeseries("23528115932")

    assert len(df) == 1
    row = df.iloc[0]
    assert row["lat"] == 34.03996204957366
    assert row["lon"] == -118.4442356787622
    # directDoubleCadence (161), not directRunCadence (80) -- see docstring.
    assert row["cadence"] == 161.0
    assert row["heart_rate_bpm"] == 130.0
    assert row["power_w"] == 488.0
    assert row["elevation_ft"] == round(63.20000076293945 / METERS_PER_FOOT, 1)
    assert row["speed_mph"] == round(3.5460000038146973 * 2.236936, 2)
    assert row["timestamp"] == pd.Timestamp(1783540789000, unit="ms")


def test_get_activity_timeseries_handles_empty_response_gracefully() -> None:
    puller = ActivityPuller(StubDetailSession({}))

    df = puller.get_activity_timeseries("123")

    assert df.empty


# Cycling activities use an entirely different metricDescriptors set --
# directBikeCadence (RPM) and power-meter fields (pedal smoothness, torque
# effectiveness, power phase), not directDoubleCadence or any running-
# dynamics field. Confirmed against a live pull (2026-07-31, cycling
# activity 23026068497).
REAL_CYCLING_METRIC_DESCRIPTORS = [
    {"metricsIndex": i, "key": key} for i, key in enumerate([
        "directSpeed", "sumDuration", "directPotentialStamina", "directLongitude", "directAvailableStamina",
        "directTimestamp", "sumElapsedDuration", "directLatitude", "directHeartRate", "directPower",
        "sumDistance", "sumMovingDuration", "directBodyBattery", "directElevation", "directVerticalSpeed",
        "sumAccumulatedPower", "directFractionalCadence", "directBikeCadence", "directLeftPowerPhaseStart",
    ])
]
REAL_CYCLING_METRIC_POINT = {
    "metrics": [
        6.42, 278.0, 80.0, -118.38618010282516, 80.0, 1779843586000.0, 301.0, 34.030726198107004, 168.0,
        194.0, 1714.469970703125, 274.0, 42.0, 34.0, 0.0, 55950.0, 0.0, 67.0, 0.0,
    ]
}


def test_get_activity_timeseries_falls_back_to_bike_cadence_for_cycling() -> None:
    detail_response = {
        "metricDescriptors": REAL_CYCLING_METRIC_DESCRIPTORS,
        "activityDetailMetrics": [REAL_CYCLING_METRIC_POINT],
    }
    puller = ActivityPuller(StubDetailSession(detail_response))

    df = puller.get_activity_timeseries("23026068497")

    row = df.iloc[0]
    assert row["cadence"] == 67.0  # directBikeCadence, not directDoubleCadence (absent for cycling)
    assert row["power_w"] == 194.0
    assert row["heart_rate_bpm"] == 168.0
    # Running-dynamics fields aren't present in a cycling response at all.
    assert pd.isna(row["stride_length"])
    assert pd.isna(row["ground_contact_time_ms"])


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
