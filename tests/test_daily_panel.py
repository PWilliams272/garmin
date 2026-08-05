"""Tests for the daily wellness+training panel.

The distinction that carries the most risk here is null-versus-zero: an
untrained day genuinely has zero load, an unmeasured day has unknown HRV, and
conflating them would corrupt every model built on this without ever looking
wrong.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from garmin.analysis.daily_panel import (
    ACUTE_DAYS,
    CHRONIC_DAYS,
    build_daily_panel,
)


class _FakeStore:
    """Minimal stand-in exposing only what build_daily_panel reads."""

    def __init__(self, daily=None, activities=None):
        self._daily = daily or {}
        self._activities = activities or {}

    def load_daily(self, dataset):
        return self._daily.get(dataset, pd.DataFrame())

    def load_activity_summary(self, dataset):
        return self._activities.get(dataset, pd.DataFrame())


def _dates(n, start="2024-01-01"):
    return pd.date_range(start, periods=n, freq="D")


def _store(*, n=40, hrv_gap=None, runs=None):
    dates = _dates(n)
    hrv = pd.DataFrame({"date": dates, "last_night_avg": np.linspace(60, 70, n)})
    if hrv_gap is not None:
        hrv = hrv.drop(index=hrv_gap).reset_index(drop=True)
    daily = {
        "hrv": hrv,
        "heart_rate": pd.DataFrame({"date": dates, "resting_hr": [55.0] * n}),
    }
    activities = {}
    if runs is not None:
        activities["running"] = pd.DataFrame(runs)
    return _FakeStore(daily=daily, activities=activities)


def test_panel_spans_every_calendar_day():
    panel = build_daily_panel(_store(n=40))
    assert len(panel) == 40
    assert panel["date"].diff().dropna().eq(pd.Timedelta(days=1)).all()


def test_untrained_days_get_zero_load_not_null():
    panel = build_daily_panel(_store(n=20, runs=[
        {"date": pd.Timestamp("2024-01-05"), "duration_min": 30.0, "avg_hr": 150.0},
    ]))
    assert panel["duration_min"].notna().all()
    assert panel.loc[panel["date"] == pd.Timestamp("2024-01-06"), "duration_min"].iloc[0] == 0.0


def test_unmeasured_days_keep_null_outcomes():
    """The counterpart, and the more dangerous direction: filling an unmeasured
    HRV with zero (or anything else) invents data that every downstream model
    would believe."""
    panel = build_daily_panel(_store(n=20, hrv_gap=[5, 6, 7]))
    assert panel["hrv"].isna().sum() == 3


def test_hr_load_is_zero_without_recorded_heart_rate():
    """A manually-logged session with no HR must not contribute a fabricated
    load, but its duration still counts."""
    panel = build_daily_panel(_store(n=10, runs=[
        {"date": pd.Timestamp("2024-01-03"), "duration_min": 45.0, "avg_hr": np.nan},
    ]))
    row = panel[panel["date"] == pd.Timestamp("2024-01-03")].iloc[0]
    assert row["duration_min"] == 45.0
    assert row["hr_load"] == 0.0


def test_hr_load_counts_beats_above_rest():
    panel = build_daily_panel(_store(n=10, runs=[
        {"date": pd.Timestamp("2024-01-03"), "duration_min": 60.0, "avg_hr": 155.0},
    ]))
    row = panel[panel["date"] == pd.Timestamp("2024-01-03")].iloc[0]
    assert row["hr_load"] == pytest.approx(60.0 * (155.0 - 55.0))


def test_easy_session_carries_less_hr_load_than_a_hard_one():
    """The reason two load measures are kept: equal duration, different load."""
    panel = build_daily_panel(_store(n=10, runs=[
        {"date": pd.Timestamp("2024-01-03"), "duration_min": 60.0, "avg_hr": 120.0},
        {"date": pd.Timestamp("2024-01-05"), "duration_min": 60.0, "avg_hr": 170.0},
    ]))
    easy = panel.loc[panel["date"] == pd.Timestamp("2024-01-03")].iloc[0]
    hard = panel.loc[panel["date"] == pd.Timestamp("2024-01-05")].iloc[0]
    assert easy["duration_min"] == hard["duration_min"]
    # (170-55)/(120-55) = 1.77x, not 2x: the measure is beats above rest, so
    # the ratio is set by the margin over resting HR, not by raw HR.
    assert hard["hr_load"] == pytest.approx(easy["hr_load"] * (170 - 55) / (120 - 55))


def test_acute_load_sums_the_trailing_window():
    runs = [{"date": d, "duration_min": 10.0, "avg_hr": 150.0} for d in _dates(30)]
    panel = build_daily_panel(_store(n=30, runs=runs))
    # Every day trained 10 minutes, so a 7-day window holds 70.
    assert panel[f"duration_min_acute_{ACUTE_DAYS}d"].iloc[-1] == pytest.approx(70.0)


def test_acwr_is_about_one_under_steady_training():
    runs = [{"date": d, "duration_min": 10.0, "avg_hr": 150.0} for d in _dates(60)]
    panel = build_daily_panel(_store(n=60, runs=runs))
    assert panel["duration_min_acwr"].iloc[-1] == pytest.approx(1.0, abs=0.05)


def test_acwr_rises_when_training_spikes():
    # The spike has to land inside the trailing acute window: placed earlier
    # it has already decayed out by the last row, which is the ratio working
    # as intended rather than failing.
    steady = [{"date": d, "duration_min": 10.0, "avg_hr": 150.0} for d in _dates(55)]
    spike = [{"date": d, "duration_min": 60.0, "avg_hr": 150.0}
             for d in _dates(5, start="2024-02-25")]
    panel = build_daily_panel(_store(n=60, runs=steady + spike))
    assert panel["duration_min_acwr"].iloc[-1] > 1.5


def test_days_since_last_session_counts_up_through_a_layoff():
    """Layoffs are the closest thing to a natural experiment here, so they
    need to be visible rather than reconstructed from a run of zeros."""
    runs = [{"date": pd.Timestamp("2024-01-02"), "duration_min": 30.0, "avg_hr": 150.0}]
    panel = build_daily_panel(_store(n=10, runs=runs))
    since = panel.set_index("date")["days_since_running"]
    assert np.isnan(since.loc[pd.Timestamp("2024-01-01")])  # never trained yet
    assert since.loc[pd.Timestamp("2024-01-02")] == 0
    assert since.loc[pd.Timestamp("2024-01-05")] == 3


def test_seasonal_terms_wrap_around_the_year():
    """A day-of-year number would put 31 December and 1 January maximally far
    apart; a sin/cos pair keeps them adjacent."""
    store = _FakeStore(daily={"hrv": pd.DataFrame({
        "date": [pd.Timestamp("2024-12-30"), pd.Timestamp("2025-01-02")],
        "last_night_avg": [60.0, 61.0],
    })})
    panel = build_daily_panel(store)
    first, last = panel.iloc[0], panel.iloc[-1]
    distance = np.hypot(first["season_sin"] - last["season_sin"],
                        first["season_cos"] - last["season_cos"])
    assert distance < 0.15


def test_empty_store_returns_an_empty_frame():
    assert build_daily_panel(_FakeStore()).empty


def test_chronic_window_is_longer_than_acute():
    assert CHRONIC_DAYS > ACUTE_DAYS
