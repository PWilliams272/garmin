"""Tests for mapping Garmin's own effort metrics onto summary rows.

Garmin computes an EPOC-based training load and per-zone time on the watch.
These arrive in the activity-list response the summary pullers already call,
so the risk here is not fetching -- it is a silently wrong mapping. A zone time
landing in the wrong column, or a missing field becoming 0 instead of None,
would produce a load metric that looks entirely reasonable and is wrong.
"""

from __future__ import annotations

import pandas as pd

from garmin.pullers.activities import (
    HR_ZONE_COUNT,
    ActivityPuller,
    _training_load_fields,
)

#: Shape of a real activity-list entry, trimmed to what these tests touch.
_ACTIVITY = {
    "activityId": 123,
    "activityName": "Afternoon Lift",
    "startTimeLocal": "2026-07-25 11:14:07",
    "duration": 3966.0,
    "averageHR": 107.0,
    "maxHR": 154.0,
    "calories": 300,
    "activityTrainingLoad": 38.364410400390625,
    "aerobicTrainingEffect": 1.6,
    "anaerobicTrainingEffect": 2.0,
    "trainingEffectLabel": "ANAEROBIC_CAPACITY",
    "moderateIntensityMinutes": 38,
    "vigorousIntensityMinutes": 14,
    "hrTimeInZone_1": 1203.016,
    "hrTimeInZone_2": 1122.269,
    "hrTimeInZone_3": 256.383,
    "hrTimeInZone_4": 0.0,
    "hrTimeInZone_5": 0.0,
}


def test_training_load_is_carried_through():
    fields = _training_load_fields(_ACTIVITY)
    assert fields["training_load"] == 38.364410400390625
    assert fields["aerobic_training_effect"] == 1.6
    assert fields["anaerobic_training_effect"] == 2.0
    assert fields["training_effect_label"] == "ANAEROBIC_CAPACITY"


def test_every_zone_is_mapped_in_order():
    """Off-by-one here would silently reassign effort between zones, which is
    exactly the input a zone-summed (Edwards) load depends on."""
    fields = _training_load_fields(_ACTIVITY)
    assert [fields[f"hr_zone_{z}_s"] for z in range(1, HR_ZONE_COUNT + 1)] == [
        1203.016, 1122.269, 256.383, 0.0, 0.0
    ]


def test_a_zero_zone_stays_zero_and_a_missing_one_stays_none():
    """Zone 4/5 at 0.0 means measured-and-empty; an absent key means unknown.
    Collapsing the two would invent an easy session out of a missing one."""
    partial = {k: v for k, v in _ACTIVITY.items() if k != "hrTimeInZone_5"}
    fields = _training_load_fields(partial)
    assert fields["hr_zone_4_s"] == 0.0
    assert fields["hr_zone_5_s"] is None


def test_missing_effort_block_yields_none_not_zero():
    """Pre-2022 and manually-entered activities carry none of this. A zero
    training load would read as 'trained, no effort' rather than 'unknown'."""
    fields = _training_load_fields({"activityId": 1})
    assert set(fields) == {
        "training_load", "aerobic_training_effect", "anaerobic_training_effect",
        "training_effect_label", "moderate_intensity_min", "vigorous_intensity_min",
        *(f"hr_zone_{z}_s" for z in range(1, HR_ZONE_COUNT + 1)),
    }
    assert all(v is None for v in fields.values())


class _StubSession:
    def __init__(self, activities):
        self._activities = activities
        self._served = False

    def get(self, url):
        if self._served:
            return []
        self._served = True
        return self._activities


def _puller(type_key):
    activity = dict(_ACTIVITY, activityType={"typeKey": type_key})
    return ActivityPuller(_StubSession([activity]))


def test_strength_summary_carries_the_effort_block_and_max_hr():
    """Strength previously had no max_hr at all, unlike every other sport."""
    frame = _puller("strength_training").pull_strength_summary("2026-01-01", "2026-12-31")
    row = frame.iloc[0]
    assert row["training_load"] == 38.364410400390625
    assert row["hr_zone_2_s"] == 1122.269
    assert row["max_hr"] == 154.0
    # The pre-existing columns must survive the addition.
    assert row["avg_hr"] == 107.0
    assert row["duration_min"] == 66.1


def test_cardio_summary_carries_the_effort_block():
    frame = _puller("cycling").pull_cardio_summary("cycling", "2026-01-01", "2026-12-31")
    row = frame.iloc[0]
    assert row["training_load"] == 38.364410400390625
    assert row["vigorous_intensity_min"] == 14


def test_running_summary_carries_the_effort_block():
    frame = _puller("running").pull_running_summary("2026-01-01", "2026-12-31")
    row = frame.iloc[0]
    assert row["training_load"] == 38.364410400390625
    assert row["moderate_intensity_min"] == 38


def test_zone_seconds_are_consistent_with_duration():
    """Sanity bound on the mapping: zone times are seconds, and their sum
    should not exceed the session. Catches a units mix-up (minutes vs seconds)
    that would otherwise look plausible."""
    frame = _puller("running").pull_running_summary("2026-01-01", "2026-12-31")
    row = frame.iloc[0]
    zone_total = sum(row[f"hr_zone_{z}_s"] for z in range(1, HR_ZONE_COUNT + 1))
    assert zone_total <= _ACTIVITY["duration"] + 1
    assert zone_total == pd.Series([1203.016, 1122.269, 256.383, 0.0, 0.0]).sum()
