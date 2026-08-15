"""Tests for Garmin's own training-metric pullers.

Two things here fail silently rather than loudly. Readiness returns a row even
for days it never scored, which would land as an all-null record indistinguishable
from a real one. And VO2max keeps two independent series -- running/general and
cycling -- which are separate estimates from different activity types; collapsing
them would blend two different measurements into a plausible-looking average.
"""

from __future__ import annotations

import pandas as pd

from garmin.pullers.training import TrainingPuller

_ZONES = [
    {
        "sport": "DEFAULT", "trainingMethod": "HR_MAX", "maxHeartRateUsed": 197,
        "lactateThresholdHeartRateUsed": 171, "restingHeartRateUsed": None,
        "zone1Floor": 100, "zone2Floor": 116, "zone3Floor": 136,
        "zone4Floor": 160, "zone5Floor": 177,
    },
    {
        "sport": "CYCLING", "trainingMethod": "HR_MAX", "maxHeartRateUsed": 197,
        "lactateThresholdHeartRateUsed": 166, "restingHeartRateUsed": None,
        "zone1Floor": 99, "zone2Floor": 118, "zone3Floor": 138,
        "zone4Floor": 158, "zone5Floor": 177,
    },
]

_READINESS = [{
    "calendarDate": "2026-08-13", "score": 68, "level": "MODERATE",
    "acuteLoad": 120, "acwrFactorPercent": 100, "recoveryTime": 12,
    "hrvWeeklyAverage": 52, "sleepScore": 77, "sleepScoreFactorPercent": 65,
    "sleepHistoryFactorPercent": 67, "stressHistoryFactorPercent": 61,
    "hrvFactorPercent": 10, "recoveryTimeFactorPercent": 99,
    "feedbackShort": "LISTEN_TO_YOUR_BODY",
}]


class _StubSession:
    """Serves canned responses keyed by a substring of the URL."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        for fragment, payload in self.routes.items():
            if fragment in url:
                return payload(url) if callable(payload) else payload
        return None


def test_hr_zones_are_flattened_per_sport():
    puller = TrainingPuller(_StubSession({"heartRateZones": _ZONES}))
    frame = puller.pull_hr_zones()
    assert list(frame["sport"]) == ["DEFAULT", "CYCLING"]
    default = frame[frame["sport"] == "DEFAULT"].iloc[0]
    assert default["max_hr"] == 197
    assert default["lactate_threshold_hr"] == 171
    assert [default[f"zone{z}_floor"] for z in range(1, 6)] == [100, 116, 136, 160, 177]


def test_readiness_maps_garmins_own_load_and_acwr():
    """The point of pulling this: Garmin computes acute load and ACWR itself."""
    puller = TrainingPuller(_StubSession({"trainingreadiness": _READINESS}))
    frame = puller.pull_training_readiness("2026-08-13", "2026-08-13")
    row = frame.iloc[0]
    assert row["readiness_score"] == 68
    assert row["acute_load"] == 120
    assert row["acwr_factor_pct"] == 100
    assert row["hrv_weekly_avg"] == 52
    assert row["date"] == pd.Timestamp("2026-08-13").date()


def test_unscored_days_are_dropped_rather_than_stored_as_null_rows():
    """Garmin returns a row for days it never scored. Storing it would create a
    day that looks measured and is not."""
    unscored = [dict(_READINESS[0], score=None)]
    puller = TrainingPuller(_StubSession({"trainingreadiness": unscored}))
    assert puller.pull_training_readiness("2026-08-13", "2026-08-13").empty


def test_known_dates_are_skipped_so_a_backfill_resumes():
    """~1000 single-day requests, so an interrupted run must not restart."""
    session = _StubSession({"trainingreadiness": _READINESS})
    puller = TrainingPuller(session)
    known = {pd.Timestamp("2026-08-11").date(), pd.Timestamp("2026-08-12").date()}
    puller.pull_training_readiness("2026-08-11", "2026-08-13", known_dates=known)
    assert len(session.calls) == 1
    assert "2026-08-13" in session.calls[0]


def test_a_failing_day_does_not_abandon_the_run():
    """One bad day in a thousand-day backfill must not lose the other 999."""
    def flaky(url):
        if "2026-08-12" in url:
            raise RuntimeError("garmin blew up")
        return _READINESS

    session = _StubSession({"trainingreadiness": flaky})
    frame = TrainingPuller(session).pull_training_readiness("2026-08-11", "2026-08-13")
    assert len(frame) == 2


def test_vo2max_keeps_running_and_cycling_apart():
    """Two independent estimates from different activity types. Averaging them
    would report a number that was never measured."""
    response = [
        {"generic": {"calendarDate": "2023-01-08", "vo2MaxPreciseValue": 58.8,
                     "vo2MaxValue": 59.0, "fitnessAge": None}},
        {"cycling": {"calendarDate": "2023-01-05", "vo2MaxPreciseValue": 55.8,
                     "vo2MaxValue": 56.0, "fitnessAge": None}},
    ]
    puller = TrainingPuller(_StubSession({"maxmet": response}))
    frame = puller.pull_vo2max("2023-01-01", "2023-01-31").set_index("date")
    assert frame.loc[pd.Timestamp("2023-01-08").date(), "vo2max_generic"] == 58.8
    assert frame.loc[pd.Timestamp("2023-01-05").date(), "vo2max_cycling"] == 55.8
    # Each date carries only the series actually measured that day.
    assert "vo2max_cycling" not in frame.columns[frame.loc[
        pd.Timestamp("2023-01-08").date()].notna()]


def test_vo2max_merges_both_series_on_a_shared_date():
    response = [{
        "generic": {"calendarDate": "2023-02-01", "vo2MaxPreciseValue": 58.0,
                    "vo2MaxValue": 58.0, "fitnessAge": None},
        "cycling": {"calendarDate": "2023-02-01", "vo2MaxPreciseValue": 55.0,
                    "vo2MaxValue": 55.0, "fitnessAge": None},
    }]
    frame = TrainingPuller(_StubSession({"maxmet": response})).pull_vo2max(
        "2023-02-01", "2023-02-01")
    assert len(frame) == 1
    assert frame.iloc[0]["vo2max_generic"] == 58.0
    assert frame.iloc[0]["vo2max_cycling"] == 55.0


def test_vo2max_ignores_placeholder_entries():
    """Records exist for days with no new estimate; they carry a null value."""
    response = [{"generic": {"calendarDate": "2023-03-01", "vo2MaxPreciseValue": None}}]
    assert TrainingPuller(_StubSession({"maxmet": response})).pull_vo2max(
        "2023-03-01", "2023-03-01").empty


def test_training_status_reads_the_primary_device():
    """The response is keyed by device id and can hold more than one."""
    response = {
        "mostRecentTrainingLoadBalance": {
            "metricsTrainingLoadBalanceDTOMap": {
                "111": {"monthlyLoadAnaerobic": 1.0, "primaryTrainingDevice": False},
                "222": {"monthlyLoadAnaerobic": 140.2, "primaryTrainingDevice": True},
            }
        },
        "mostRecentTrainingStatus": {
            "latestTrainingStatusData": {
                "222": {
                    "trainingStatus": 1, "trainingStatusFeedbackPhrase": "DETRAINING",
                    "fitnessTrend": 0, "trainingPaused": False, "weeklyTrainingLoad": None,
                    "acuteTrainingLoadDTO": {
                        "dailyTrainingLoadAcute": 0, "dailyTrainingLoadChronic": 219,
                        "acwrPercent": 0, "acwrStatus": "LOW",
                        "minTrainingLoadChronic": 175.2, "maxTrainingLoadChronic": 328.5,
                    },
                }
            }
        },
    }
    puller = TrainingPuller(_StubSession({"trainingstatus": response}))
    row = puller.pull_training_status("2026-08-13", "2026-08-13").iloc[0]
    assert row["load_anaerobic"] == 140.2
    assert row["load_chronic"] == 219
    assert row["acwr_status"] == "LOW"
    assert row["training_status_feedback"] == "DETRAINING"
