"""Tests for the nightly refresh of Garmin's training metrics and the wellness
daily summaries.

Both groups were backfilled by hand and had no nightly refresh, so they froze
at the backfill date -- including `current_altitude_m`, the only altitude
signal in the store. Wiring them into `update_all` introduces two risks these
tests pin down:

1. **Unbounded work.** Readiness and status are one-request-per-day endpoints
   with no range form. Deriving the window from the last stored date means an
   empty or stale dataset asks Garmin for ~1000 days inside a nightly Lambda.
   The window must be a small fixed lookback regardless of what is stored.
2. **A new failure mode for the whole run.** These datasets are supplementary;
   a Garmin outage in one must not cost the run the health and activity data
   already pulled.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from garmin.updaters import (
    NIGHTLY_LOOKBACK_DAYS,
    WELLNESS_DAILY_DATASETS,
    DataUpdater,
)


class _StubStore:
    """Curated store recording what was merged."""

    def __init__(self, existing: dict | None = None):
        self.existing = existing or {}
        self.merged: dict[str, pd.DataFrame] = {}
        self.hr_zones = None

    def load_daily(self, dataset):
        return self.existing.get(dataset, pd.DataFrame())

    def merge_daily(self, dataset, df):
        self.merged[dataset] = df
        return df

    def write_hr_zones(self, df):
        self.hr_zones = df


class _StubTrainingPuller:
    def __init__(self, frame=None, fail=False):
        self.frame = frame if frame is not None else pd.DataFrame()
        self.fail = fail
        self.calls = []

    def _record(self, name, start, end, known_dates, show_progress=True):
        if self.fail:
            raise RuntimeError("garmin timed out")
        self.calls.append({"name": name, "start": start, "end": end,
                           "known": known_dates or set(), "progress": show_progress})
        return self.frame

    def pull_training_readiness(self, start, end, known_dates=None, show_progress=True):
        return self._record("readiness", start, end, known_dates, show_progress)

    def pull_training_status(self, start, end, known_dates=None, show_progress=True):
        return self._record("status", start, end, known_dates, show_progress)

    def pull_vo2max(self, start, end):
        return self._record("vo2max", start, end, None)

    def pull_hr_zones(self):
        return pd.DataFrame([{"sport": "DEFAULT", "max_hr": 197}])


class _StubDetailedPuller:
    def __init__(self, frame=None, fail=False):
        self.frame = frame if frame is not None else pd.DataFrame()
        self.fail = fail
        self.calls = []

    def pull_daily_summaries(self, metric, start, end, known_dates=None, show_progress=True):
        if self.fail:
            raise RuntimeError("garmin timed out")
        self.calls.append({"metric": metric, "start": start, "end": end,
                           "known": known_dates or set(), "progress": show_progress})
        return self.frame


def _updater(store, training=None, detailed=None):
    return DataUpdater(
        session=object(),
        curated_store=store,
        health_puller=object(),
        health_detailed_puller=detailed or _StubDetailedPuller(),
        activity_puller=object(),
        training_puller=training or _StubTrainingPuller(),
    )


def _day_frame(date_str):
    return pd.DataFrame([{"date": pd.Timestamp(date_str).date(), "score": 70}])


def test_window_is_a_fixed_lookback_not_since_the_last_stored_date():
    """The safety property: a dataset stale by years must still ask for only
    the lookback window, or the nightly Lambda tries a ~1000-request backfill."""
    stale = pd.DataFrame({"date": pd.to_datetime(["2023-01-01"]).date})
    store = _StubStore({"training_readiness": stale})
    start, end, _ = _updater(store)._nightly_window("training_readiness")

    span = (datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")).days
    assert span == NIGHTLY_LOOKBACK_DAYS
    assert start > "2023-01-01"


def test_window_is_the_same_bounded_span_when_nothing_is_stored_at_all():
    start, end, known = _updater(_StubStore())._nightly_window("training_readiness")
    span = (datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")).days
    assert span == NIGHTLY_LOOKBACK_DAYS
    assert known == set()


def test_days_already_stored_are_passed_through_so_they_are_not_refetched():
    yesterday = (datetime.today().date() - timedelta(days=1))
    store = _StubStore({
        "training_readiness": pd.DataFrame({"date": [yesterday]}),
    })
    training = _StubTrainingPuller(_day_frame("2026-08-18"))
    _updater(store, training=training)._update_training_metrics_curated()

    readiness = next(c for c in training.calls if c["name"] == "readiness")
    assert yesterday in readiness["known"]


def test_each_training_dataset_is_pulled_and_merged():
    training = _StubTrainingPuller(_day_frame("2026-08-18"))
    store = _StubStore()
    _updater(store, training=training)._update_training_metrics_curated()

    assert {"training_readiness", "training_status", "vo2max"} <= set(store.merged)
    assert store.hr_zones is not None


def test_an_empty_pull_is_not_merged():
    """Merging an empty frame would rewrite the dataset for no reason."""
    store = _StubStore()
    _updater(store, training=_StubTrainingPuller(pd.DataFrame()))._update_training_metrics_curated()
    assert store.merged == {}
    # HR zones are undated and always rewritten, so they still land.
    assert store.hr_zones is not None


def test_all_three_wellness_datasets_are_refreshed():
    detailed = _StubDetailedPuller(_day_frame("2026-08-18"))
    store = _StubStore()
    _updater(store, detailed=detailed)._update_wellness_daily_curated()

    assert {c["metric"] for c in detailed.calls} == set(WELLNESS_DAILY_DATASETS)
    assert set(store.merged) == set(WELLNESS_DAILY_DATASETS.values())


def test_progress_bars_are_off_in_the_nightly_run():
    """tqdm writing to Lambda logs produces thousands of useless lines."""
    detailed = _StubDetailedPuller(_day_frame("2026-08-18"))
    training = _StubTrainingPuller(_day_frame("2026-08-18"))
    updater = _updater(_StubStore(), training=training, detailed=detailed)
    updater._update_wellness_daily_curated()
    updater._update_training_metrics_curated()

    assert all(c["progress"] is False for c in detailed.calls)
    per_day = [c for c in training.calls if c["name"] in {"readiness", "status"}]
    assert per_day and all(c["progress"] is False for c in per_day)


def test_a_failing_training_pull_does_not_stop_the_wellness_pull():
    """The isolation property: one supplementary dataset failing must not cost
    the others, nor the run."""
    store = _StubStore()
    detailed = _StubDetailedPuller(_day_frame("2026-08-18"))
    updater = _updater(store, training=_StubTrainingPuller(fail=True), detailed=detailed)

    failures = updater._update_supplementary_curated()

    assert len(failures) == 1 and "training metrics" in failures[0]
    assert set(store.merged) == set(WELLNESS_DAILY_DATASETS.values())


def test_both_failing_is_reported_and_still_does_not_raise():
    updater = _updater(
        _StubStore(),
        training=_StubTrainingPuller(fail=True),
        detailed=_StubDetailedPuller(fail=True),
    )
    assert len(updater._update_supplementary_curated()) == 2


def test_a_clean_run_reports_no_failures():
    updater = _updater(
        _StubStore(),
        training=_StubTrainingPuller(_day_frame("2026-08-18")),
        detailed=_StubDetailedPuller(_day_frame("2026-08-18")),
    )
    assert updater._update_supplementary_curated() == []


def test_update_all_actually_invokes_the_supplementary_step():
    """The wiring itself. Without this, every test above passes while the
    nightly run still never calls any of it -- which is exactly the state
    these datasets were in."""
    store = _StubStore()
    updater = _updater(store)
    called = []
    updater.update = lambda model_class: called.append(("health", model_class))
    updater._update_all_activities_curated = lambda: called.append(("activities", None))
    updater._update_supplementary_curated = lambda: called.append(("supplementary", None))

    updater.update_all()

    steps = [name for name, _ in called]
    assert "supplementary" in steps
    # Ordering matters: the supplementary datasets are the least important, so
    # they run last and cannot delay or break the core pull.
    assert steps.index("supplementary") > steps.index("activities")


def test_supplementary_step_is_skipped_without_a_curated_store():
    """The legacy DB path has none of these datasets."""
    updater = DataUpdater(
        session=object(), db_manager=object(), curated_store=None,
        health_puller=object(), health_detailed_puller=_StubDetailedPuller(),
        activity_puller=object(), training_puller=_StubTrainingPuller(),
    )
    called = []
    updater.update = lambda model_class: None
    updater._update_supplementary_curated = lambda: called.append("ran")

    updater.update_all()

    assert called == []
