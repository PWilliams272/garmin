"""Tests for multi-detail-dataset wiring on an activity type.

Strength is the only sport with two detail views -- per-set reps/weight and a
per-second HR trace. The risk in adding the second is silently losing the
first: both the daily update and the backfill have to iterate every spec, and
a regression there would look like "HR arrived" while the set data quietly
stopped being written.
"""

from __future__ import annotations

import pandas as pd

from garmin.updaters import _detail_specs


def _fn(name):
    def puller(activity_id):
        return pd.DataFrame({"activity_id": [activity_id], "source": [name]})

    puller.__name__ = name
    return puller


def test_single_detail_is_unchanged():
    specs = _detail_specs("running", _fn("ts"), "running_timeseries", None)
    assert len(specs) == 1
    assert specs[0][1] == "running_timeseries"


def test_detail_dataset_defaults_to_the_summary_name():
    """get_strength_workout writes to curated/activities/detail/strength/."""
    specs = _detail_specs("strength", _fn("sets"), None, None)
    assert specs[0][1] == "strength"


def test_no_detail_fn_yields_no_specs():
    assert _detail_specs("tennis", None, None, None) == []


def test_extras_are_appended_after_the_primary():
    """Order matters: the primary per-set pull must not be displaced by the
    HR trace, or strength loses its reps/weight data."""
    specs = _detail_specs(
        "strength",
        _fn("sets"),
        None,
        [{"detail_fn": _fn("hr"), "detail_dataset": "strength_timeseries"}],
    )
    assert [d for _, d in specs] == ["strength", "strength_timeseries"]
    assert specs[0][0].__name__ == "sets"
    assert specs[1][0].__name__ == "hr"


def test_extras_work_without_a_primary():
    specs = _detail_specs(
        "x", None, None, [{"detail_fn": _fn("hr"), "detail_dataset": "x_timeseries"}]
    )
    assert [d for _, d in specs] == ["x_timeseries"]


def test_strength_registry_entry_carries_both_details():
    """The wiring itself, read off the real registry rather than a fixture."""
    from garmin.updaters import DataUpdater

    class _StubPuller:
        def __getattr__(self, name):
            return lambda *a, **k: pd.DataFrame()

    updater = DataUpdater.__new__(DataUpdater)
    updater.activity_puller = _StubPuller()

    entries = {e["dataset"]: e for e in updater._activity_type_registry()}
    specs = _detail_specs(
        "strength",
        entries["strength"].get("detail_fn"),
        entries["strength"].get("detail_dataset"),
        entries["strength"].get("extra_details"),
    )
    assert [d for _, d in specs] == ["strength", "strength_timeseries"]


def test_every_other_sport_still_has_exactly_one_detail():
    """Guards against pasting extra_details onto the generic cardio branch."""
    from garmin.updaters import DataUpdater

    class _StubPuller:
        def __getattr__(self, name):
            return lambda *a, **k: pd.DataFrame()

    updater = DataUpdater.__new__(DataUpdater)
    updater.activity_puller = _StubPuller()

    for entry in updater._activity_type_registry():
        specs = _detail_specs(
            entry["dataset"], entry.get("detail_fn"),
            entry.get("detail_dataset"), entry.get("extra_details"),
        )
        expected = 2 if entry["dataset"] == "strength" else 1
        assert len(specs) == expected, f"{entry['dataset']} has {len(specs)} details"
