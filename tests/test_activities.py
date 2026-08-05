from __future__ import annotations

import io
import zipfile

import fitparse
import pandas as pd
import pytest

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
    assert "detail_fn" in running_entry
    assert running_entry["detail_dataset"] == "running_timeseries"
    cycling_entry = next(e for e in updater._activity_type_registry() if e["dataset"] == "cycling")
    assert "detail_fn" in cycling_entry
    assert cycling_entry["detail_dataset"] == "cycling_timeseries"
    strength_entry = next(e for e in updater._activity_type_registry() if e["dataset"] == "strength")
    assert "detail_fn" in strength_entry
    assert "detail_dataset" not in strength_entry


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


def test_update_activity_curated_writes_detail_under_separate_dataset(tmp_path) -> None:
    store = CuratedDataStore(file_manager=FileManager(environment="local", local_dir=str(tmp_path)))
    updater = DataUpdater(session=object(), db_manager=object(), curated_store=store)

    def summary_fn(start_date, end_date):
        return pd.DataFrame([{"activity_id": "1", "date": "2024-06-01", "duration_min": 45.0}])

    def detail_fn(activity_id):
        return pd.DataFrame([{"timestamp": "2024-06-01T08:00:00", "speed_mph": 6.0}])

    updater._update_activity_curated("running", summary_fn, detail_fn, "running_timeseries")

    assert len(store.load_activity_detail("running_timeseries", "1")) == 1
    assert store.load_activity_detail("running", "1").empty


def test_backfill_activity_details_only_fetches_missing_ids(tmp_path) -> None:
    store = CuratedDataStore(file_manager=FileManager(environment="local", local_dir=str(tmp_path)))
    store.merge_activity_summary(
        "running",
        pd.DataFrame([
            {"activity_id": "1", "date": "2024-06-01", "duration_min": 45.0},
            {"activity_id": "2", "date": "2024-06-02", "duration_min": 30.0},
        ]),
    )
    store.write_activity_detail(
        "running_timeseries", "1", pd.DataFrame([{"timestamp": "2024-06-01T08:00:00", "speed_mph": 6.0}])
    )
    updater = DataUpdater(session=object(), db_manager=object(), curated_store=store)

    detail_calls: list[str] = []

    def detail_fn(activity_id):
        detail_calls.append(activity_id)
        return pd.DataFrame([{"timestamp": "2024-06-02T08:00:00", "speed_mph": 7.0}])

    result = updater.backfill_activity_details("running", detail_fn, "running_timeseries")

    assert detail_calls == ["2"]
    assert result == {"dataset": "running", "total": 2, "already_had_detail": 1, "fetched": 1, "empty": 0}
    assert len(store.load_activity_detail("running_timeseries", "2")) == 1


def test_backfill_activity_details_limit_reports_deferred_ids_as_not_yet_had(tmp_path) -> None:
    store = CuratedDataStore(file_manager=FileManager(environment="local", local_dir=str(tmp_path)))
    store.merge_activity_summary(
        "running",
        pd.DataFrame([
            {"activity_id": str(i), "date": "2024-06-01", "duration_min": 45.0} for i in range(1, 6)
        ]),
    )
    updater = DataUpdater(session=object(), db_manager=object(), curated_store=store)

    def detail_fn(activity_id):
        return pd.DataFrame([{"timestamp": "2024-06-01T08:00:00", "speed_mph": 6.0}])

    result = updater.backfill_activity_details("running", detail_fn, "running_timeseries", limit=2)

    # 2 fetched this run, 0 already had detail before the run -- the other 3
    # are still genuinely missing (deferred to a future run), not "had".
    assert result == {"dataset": "running", "total": 5, "already_had_detail": 0, "fetched": 2, "empty": 0}


def test_backfill_activity_details_is_resumable_noop_when_all_present(tmp_path) -> None:
    store = CuratedDataStore(file_manager=FileManager(environment="local", local_dir=str(tmp_path)))
    store.merge_activity_summary(
        "running", pd.DataFrame([{"activity_id": "1", "date": "2024-06-01", "duration_min": 45.0}])
    )
    store.write_activity_detail(
        "running_timeseries", "1", pd.DataFrame([{"timestamp": "2024-06-01T08:00:00", "speed_mph": 6.0}])
    )
    updater = DataUpdater(session=object(), db_manager=object(), curated_store=store)

    def detail_fn(activity_id):
        raise AssertionError("should not be called -- detail already present")

    result = updater.backfill_activity_details("running", detail_fn, "running_timeseries")

    assert result == {"dataset": "running", "total": 1, "already_had_detail": 1, "fetched": 0, "empty": 0}


def test_backfill_activity_details_force_refetches_even_with_existing_file(tmp_path) -> None:
    store = CuratedDataStore(file_manager=FileManager(environment="local", local_dir=str(tmp_path)))
    store.merge_activity_summary(
        "running", pd.DataFrame([{"activity_id": "1", "date": "2024-06-01", "duration_min": 45.0}])
    )
    store.write_activity_detail(
        "running_timeseries", "1", pd.DataFrame([{"timestamp": "2024-06-01T08:00:00", "speed_mph": 6.0}])
    )
    updater = DataUpdater(session=object(), db_manager=object(), curated_store=store)

    detail_calls: list[str] = []

    def detail_fn(activity_id):
        detail_calls.append(activity_id)
        return pd.DataFrame([{"timestamp": "2024-06-01T08:00:00", "speed_mph": 7.5}])

    result = updater.backfill_activity_details("running", detail_fn, "running_timeseries", force=True)

    assert detail_calls == ["1"]
    assert result == {"dataset": "running", "total": 1, "already_had_detail": 0, "fetched": 1, "empty": 0}
    refreshed = store.load_activity_detail("running_timeseries", "1")
    assert refreshed.iloc[0]["speed_mph"] == 7.5


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


class StubDownloadSession:
    """Returns fixed bytes for any session.download(...) call."""

    def __init__(self, content: bytes) -> None:
        self.content = content

    def download(self, url: str) -> bytes:
        return self.content


def _zip_with_fit(fit_bytes: bytes, filename: str = "123_ACTIVITY.fit") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(filename, fit_bytes)
    return buf.getvalue()


def test_download_activity_fit_extracts_fit_from_zip() -> None:
    zip_bytes = _zip_with_fit(b"fake-fit-content")
    puller = ActivityPuller(StubDownloadSession(zip_bytes))

    result = puller.download_activity_fit("123")

    assert result == b"fake-fit-content"


def test_download_activity_fit_returns_none_for_zip_without_fit_file() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", b"no fit here")
    puller = ActivityPuller(StubDownloadSession(buf.getvalue()))

    assert puller.download_activity_fit("123") is None


def test_download_activity_fit_returns_none_for_bad_zip() -> None:
    puller = ActivityPuller(StubDownloadSession(b"not a zip at all"))

    assert puller.download_activity_fit("123") is None


def test_download_activity_fit_returns_none_for_empty_response() -> None:
    puller = ActivityPuller(StubDownloadSession(b""))

    assert puller.download_activity_fit("123") is None


class _FakeFitField:
    def __init__(self, name, value):
        self.name = name
        self.value = value


class _FakeFitRecord:
    def __init__(self, fields: dict):
        self._fields = fields

    def __iter__(self):
        for k, v in self._fields.items():
            yield _FakeFitField(k, v)

    def get_value(self, name):
        return self._fields.get(name)


class _FakeFitFile:
    """Mimics fitparse.FitFile's public interface (get_messages) without
    needing a real binary FIT fixture -- lets us test our own field
    extraction/conversion logic using real values captured from a live
    pull (see get_activity_fit_timeseries's docstring) without checking a
    ~100KB+ binary file into the repo.
    """

    def __init__(self, sport: str, records: list[dict], sets: list[dict] | None = None) -> None:
        self._sport = sport
        self._records = [_FakeFitRecord(r) for r in records]
        self._sets = [_FakeFitRecord(s) for s in (sets or [])]

    def get_messages(self, kind: str):
        if kind == "session":
            return [_FakeFitRecord({"sport": self._sport})]
        if kind == "record":
            return self._records
        if kind == "set":
            return self._sets
        return []


# Real values from a live pull (2026-07-31, running activity 23528115932,
# the record matched against the JSON /details point used elsewhere in this
# file) -- FIT's cadence (80 + 0.5 fractional) is per-leg, confirmed by
# comparing against that JSON point's directDoubleCadence (161): FIT
# reports exactly half.
REAL_FIT_RUNNING_RECORD = {
    "timestamp": pd.Timestamp("2026-07-08 19:59:49"),
    "position_lat": 406112566, "position_long": -1413094774,
    "enhanced_altitude": 63.2, "distance": 1973.59, "enhanced_speed": 3.546,
    "cadence": 80, "fractional_cadence": 0.5,
    "heart_rate": 130, "power": 488,
    "unknown_140": 3683,
    "stance_time": 272.0, "vertical_oscillation": 101.6, "vertical_ratio": 7.64, "step_length": 1329.0,
}

# Real values from a live pull (2026-07-31, cycling activity 23026068497) --
# cycling's cadence field is already true RPM, not per-leg, so it should
# NOT be doubled the way running's is.
REAL_FIT_CYCLING_RECORD = {
    "timestamp": pd.Timestamp("2026-05-27 00:55:35"),
    "position_lat": 406028864, "position_long": -1412243258,
    "enhanced_altitude": 26.0, "distance": 218.7, "enhanced_speed": 2.351,
    "cadence": 67, "fractional_cadence": 0.0,
    "heart_rate": 100, "power": 0,
}


def test_get_activity_fit_timeseries_doubles_cadence_for_running(monkeypatch) -> None:
    puller = ActivityPuller(object())
    monkeypatch.setattr(puller, "download_activity_fit", lambda activity_id: b"fake-fit-bytes")
    monkeypatch.setattr(
        fitparse, "FitFile",
        lambda source: _FakeFitFile("running", [REAL_FIT_RUNNING_RECORD]),
    )

    df = puller.get_activity_fit_timeseries("23528115932")

    row = df.iloc[0]
    assert row["cadence"] == 161.0  # (80 + 0.5) * 2, matches JSON's directDoubleCadence
    assert row["heart_rate_bpm"] == 130.0
    assert row["power_w"] == 488.0
    assert row["grade_adjusted_speed"] == 3.683  # unknown_140 / 1000
    assert row["lat"] == pytest.approx(34.03996204957366, abs=1e-6)
    assert row["lon"] == pytest.approx(-118.4442356787622, abs=1e-6)
    assert row["elevation_ft"] == round(63.2 / METERS_PER_FOOT, 1)
    assert row["stride_length"] == 1329.0


def test_get_activity_fit_timeseries_does_not_double_cadence_for_cycling(monkeypatch) -> None:
    puller = ActivityPuller(object())
    monkeypatch.setattr(puller, "download_activity_fit", lambda activity_id: b"fake-fit-bytes")
    monkeypatch.setattr(
        fitparse, "FitFile",
        lambda source: _FakeFitFile("cycling", [REAL_FIT_CYCLING_RECORD]),
    )

    df = puller.get_activity_fit_timeseries("23026068497")

    row = df.iloc[0]
    assert row["cadence"] == 67.0  # not doubled
    assert row["power_w"] == 0.0
    assert pd.isna(row["stride_length"])  # running-dynamics fields absent for cycling


def test_get_activity_fit_timeseries_returns_empty_when_no_fit_file(monkeypatch) -> None:
    puller = ActivityPuller(object())
    monkeypatch.setattr(puller, "download_activity_fit", lambda activity_id: None)

    assert puller.get_activity_fit_timeseries("123").empty


class StubExerciseSetsSession:
    """Returns a fixed /exerciseSets response for any activity-detail call."""

    def __init__(self, exercise_sets_response: dict) -> None:
        self.response = exercise_sets_response

    def get(self, url: str):
        if "exerciseSets" in url:
            return self.response
        return None


def test_get_strength_workout_aligns_by_position_not_message_index(monkeypatch) -> None:
    """Regression test: a real activity was found (2026-08-04) where every
    set's messageIndex in the JSON response was None, which broke the
    original messageIndex-keyed lookup against the FIT file's own `set`
    messages and silently dropped rest/HR for the whole activity. Position-
    based alignment (Nth active JSON set <-> Nth active FIT set) has to
    work even when messageIndex is missing/None throughout."""
    exercise_sets_response = {
        "exerciseSets": [
            {
                "setType": "ACTIVE", "messageIndex": None, "duration": 40.0,
                "repetitionCount": 8, "weight": 61250.0, "startTime": "2024-06-01T18:00:00.0",
                "exercises": [{"category": "BENCH_PRESS", "name": None, "probability": 100.0}],
            },
            {
                "setType": "ACTIVE", "messageIndex": None, "duration": 35.0,
                "repetitionCount": 6, "weight": 70312.0, "startTime": "2024-06-01T18:02:00.0",
                "exercises": [
                    {"category": "BENCH_PRESS", "name": None, "probability": 55.0},
                    {"category": "SHOULDER_PRESS", "name": None, "probability": 55.0},
                ],
            },
        ]
    }
    fit_sets = [
        {"set_type": "active", "start_time": pd.Timestamp("2024-06-01 18:00:00"), "duration": 40.0},
        {"set_type": "rest", "start_time": pd.Timestamp("2024-06-01 18:00:40"), "duration": 80.0},
        {"set_type": "active", "start_time": pd.Timestamp("2024-06-01 18:02:00"), "duration": 35.0},
    ]
    fit_records = [
        {"timestamp": pd.Timestamp("2024-06-01 18:00:00"), "heart_rate": 90},
        {"timestamp": pd.Timestamp("2024-06-01 18:00:20"), "heart_rate": 100},
        {"timestamp": pd.Timestamp("2024-06-01 18:00:40"), "heart_rate": 110},
        {"timestamp": pd.Timestamp("2024-06-01 18:02:00"), "heart_rate": 95},
        {"timestamp": pd.Timestamp("2024-06-01 18:02:35"), "heart_rate": 105},
    ]

    puller = ActivityPuller(StubExerciseSetsSession(exercise_sets_response))
    monkeypatch.setattr(puller, "download_activity_fit", lambda activity_id: b"fake-fit-bytes")
    monkeypatch.setattr(
        fitparse, "FitFile",
        lambda source: _FakeFitFile("strength_training", fit_records, sets=fit_sets),
    )

    df = puller.get_strength_workout("999")

    assert len(df) == 2
    # First set has no preceding rest; second's rest comes from the FIT
    # rest set's real duration, found by position despite messageIndex=None.
    assert pd.isna(df.iloc[0]["rest_before_s"])
    assert df.iloc[1]["rest_before_s"] == 80.0
    # HR windowed from each set's own FIT-native start/duration.
    assert df.iloc[0]["hr_avg"] == pytest.approx((90 + 100 + 110) / 3, abs=0.1)
    assert df.iloc[1]["hr_avg"] == pytest.approx((95 + 105) / 2, abs=0.1)
    # Single-candidate-at-100 -> manually reviewed; multi-candidate -> not.
    assert bool(df.iloc[0]["manually_reviewed"]) is True
    assert bool(df.iloc[1]["manually_reviewed"]) is False


def test_get_activity_detail_timeseries_prefers_fit_over_json(monkeypatch) -> None:
    puller = ActivityPuller(object())
    fit_df = pd.DataFrame([{"lat": 1.0}])
    json_df = pd.DataFrame([{"lat": 2.0}])
    monkeypatch.setattr(puller, "get_activity_fit_timeseries", lambda activity_id: fit_df)
    monkeypatch.setattr(puller, "get_activity_timeseries", lambda activity_id: json_df)

    result = puller.get_activity_detail_timeseries("123")

    assert result is fit_df


def test_get_activity_detail_timeseries_falls_back_to_json_when_fit_empty(monkeypatch) -> None:
    puller = ActivityPuller(object())
    json_df = pd.DataFrame([{"lat": 2.0}])
    monkeypatch.setattr(puller, "get_activity_fit_timeseries", lambda activity_id: pd.DataFrame())
    monkeypatch.setattr(puller, "get_activity_timeseries", lambda activity_id: json_df)

    result = puller.get_activity_detail_timeseries("123")

    assert result is json_df
