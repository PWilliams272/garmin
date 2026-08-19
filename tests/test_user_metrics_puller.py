"""Tests for the endpoints found in the 2026-08-19 sweep.

Two failure modes here are silent rather than loud, so they get pinned:

1. **Range caps.** Garmin caps the race-prediction endpoint at 365 days and
   returns HTTP 400 beyond it. Asking for the full history in one call fails
   entirely, which is easy to mistake for "no data".
2. **Null-versus-zero.** These endpoints report genuine zeros -- zero floors
   climbed, zero vigorous minutes, zero hydration logged. A `value or None`
   idiom would turn each into a missing day, which is a different claim.
"""

from __future__ import annotations

import pandas as pd

from garmin.pullers.user_metrics import (
    RACE_PREDICTION_MAX_DAYS,
    UserMetricsPuller,
    _year_chunks,
)


class _StubSession:
    """Serves canned responses keyed by a URL fragment, recording calls."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        for fragment, payload in self.routes.items():
            if fragment in url:
                return payload(url) if callable(payload) else payload
        return None


_PROFILE = {"displayName": "TestUser"}


def _puller(routes):
    routes = {"socialProfile": _PROFILE, **routes}
    return UserMetricsPuller(_StubSession(routes))


# -- range chunking -------------------------------------------------------

def test_a_range_within_the_cap_is_one_chunk():
    assert _year_chunks("2026-01-01", "2026-03-01") == [("2026-01-01", "2026-03-01")]


def test_a_multi_year_range_is_split_at_the_cap():
    chunks = _year_chunks("2022-01-01", "2026-08-19")
    assert len(chunks) > 1
    for start, end in chunks:
        span = (pd.Timestamp(end) - pd.Timestamp(start)).days + 1
        assert span <= RACE_PREDICTION_MAX_DAYS


def test_chunks_are_contiguous_and_cover_the_whole_range():
    """A gap here would silently lose a year of history."""
    chunks = _year_chunks("2022-01-01", "2026-08-19")
    assert chunks[0][0] == "2022-01-01"
    assert chunks[-1][1] == "2026-08-19"
    for (_, end), (next_start, _) in zip(chunks, chunks[1:], strict=False):
        assert pd.Timestamp(next_start) == pd.Timestamp(end) + pd.Timedelta(days=1)


def test_a_single_day_range_is_one_chunk():
    assert _year_chunks("2026-08-19", "2026-08-19") == [("2026-08-19", "2026-08-19")]


# -- race predictions -----------------------------------------------------

_PREDICTION = {"calendarDate": "2026-08-18", "time5K": 1297, "time10K": 2803,
               "timeHalfMarathon": 6327, "timeMarathon": 14034}


def test_race_predictions_are_mapped_to_seconds_columns():
    frame = _puller({"racepredictions": [_PREDICTION]}).pull_race_predictions(
        "2026-08-01", "2026-08-19")
    row = frame.iloc[0]
    assert row["race_time_5k_s"] == 1297
    assert row["race_time_marathon_s"] == 14034
    assert row["date"] == pd.Timestamp("2026-08-18").date()


def test_days_with_no_estimate_are_dropped_not_stored_as_null_rows():
    """Garmin returns a row for every day in range, carrying nulls before it
    had an estimate. Keeping them would create days that look measured."""
    empty_day = {"calendarDate": "2026-08-01", "time5K": None, "time10K": None,
                 "timeHalfMarathon": None, "timeMarathon": None}
    # A single chunk, so the stub's fixed response is served exactly once.
    frame = _puller({"racepredictions": [empty_day, _PREDICTION]}).pull_race_predictions(
        "2026-08-01", "2026-08-19")
    assert len(frame) == 1
    assert frame.iloc[0]["date"] == pd.Timestamp("2026-08-18").date()


def test_a_failing_chunk_does_not_lose_the_other_chunks():
    def flaky(url):
        if "fromCalendarDate=2022-01-01" in url:
            raise RuntimeError("garmin blew up")
        return [_PREDICTION]

    frame = _puller({"racepredictions": flaky}).pull_race_predictions(
        "2022-01-01", "2026-08-19")
    assert not frame.empty


# -- per-day pullers ------------------------------------------------------

def test_fitness_age_flattens_its_components():
    response = {
        "fitnessAge": 29.8, "chronologicalAge": 33, "achievableFitnessAge": 27.0,
        "previousFitnessAge": 29.8, "lastUpdated": "2026-08-16T00:00:00.0",
        "components": {"bodyFat": {"value": 20.9, "targetValue": 15.7, "potentialAge": 29.7}},
    }
    frame = _puller({"fitnessage": response}).pull_fitness_age(
        "2026-08-16", "2026-08-16", show_progress=False)
    row = frame.iloc[0]
    assert row["fitness_age"] == 29.8
    assert row["bodyFat_value"] == 20.9
    assert row["bodyFat_target"] == 15.7


def test_a_day_with_no_fitness_age_is_skipped():
    frame = _puller({"fitnessage": {"fitnessAge": None}}).pull_fitness_age(
        "2026-08-16", "2026-08-16", show_progress=False)
    assert frame.empty


def test_a_genuine_zero_is_kept_rather_than_read_as_missing():
    """Zero floors climbed and zero vigorous minutes are real measurements."""
    response = {"calendarDate": "2026-08-16", "floorsAscended": 0.0,
                "vigorousIntensityMinutes": 0, "totalSteps": 3933}
    frame = _puller({"usersummary/daily": response}).pull_daily_summary(
        "2026-08-16", "2026-08-16", show_progress=False)
    row = frame.iloc[0]
    assert row["floors_ascended"] == 0.0
    assert row["vigorous_intensity_min"] == 0
    assert pd.notna(row["floors_ascended"])


def test_hydration_keeps_a_zero_intake_day():
    response = {"calendarDate": "2026-08-16", "valueInML": 0.0, "sweatLossInML": 1484.0}
    frame = _puller({"hydration": response}).pull_hydration(
        "2026-08-16", "2026-08-16", show_progress=False)
    assert frame.iloc[0]["intake_ml"] == 0.0
    assert frame.iloc[0]["sweat_loss_ml"] == 1484.0


def test_known_dates_are_skipped_so_a_backfill_resumes():
    """~1000 single-day requests per metric, so an interrupted run must not
    restart from the beginning."""
    puller = _puller({"hydration": {"calendarDate": "x", "valueInML": 1.0}})
    known = {pd.Timestamp("2026-08-14").date(), pd.Timestamp("2026-08-15").date()}
    puller.pull_hydration("2026-08-14", "2026-08-16", known_dates=known,
                          show_progress=False)
    hydration_calls = [c for c in puller.session.calls if "hydration" in c]
    assert len(hydration_calls) == 1
    assert "2026-08-16" in hydration_calls[0]


def test_a_failing_day_does_not_abandon_the_run():
    def flaky(url):
        if "2026-08-15" in url:
            raise RuntimeError("garmin blew up")
        return {"calendarDate": "x", "valueInML": 1.0}

    frame = _puller({"hydration": flaky}).pull_hydration(
        "2026-08-14", "2026-08-16", show_progress=False)
    assert len(frame) == 2


# -- snapshots ------------------------------------------------------------

def test_personal_records_are_mapped():
    record = {"id": 1, "typeId": 3, "activityType": "running", "value": 1007.1,
              "activityId": 42, "prTypeLabelKey": "1km", "status": "ACTIVE"}
    frame = _puller({"personalrecord": [record]}).pull_personal_records()
    assert frame.iloc[0]["type_id"] == 3
    assert frame.iloc[0]["value"] == 1007.1


def test_devices_are_mapped():
    frame = _puller({"deviceregistration": [
        {"displayName": "fenix 7", "partNumber": "006-B3906-00"}]}).pull_devices()
    assert frame.iloc[0]["display_name"] == "fenix 7"


def test_the_display_name_is_fetched_once_not_per_day():
    """It is needed by several per-day URLs; re-fetching would double the
    request count across a ~1000-day backfill."""
    puller = _puller({"usersummary/daily": {"calendarDate": "x", "totalSteps": 1}})
    puller.pull_daily_summary("2026-08-01", "2026-08-10", show_progress=False)
    assert sum("socialProfile" in c for c in puller.session.calls) == 1
