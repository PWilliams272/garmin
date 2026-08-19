"""Tests for the session-context fields captured from the activity list.

These exist because three of them replace something this repo previously had to
*infer*: hand-logged sessions (from null HR), active vs paused time (from
nothing at all), and the device boundary (from where HR data happens to start).
An inference that silently disagrees with Garmin's own flag is worse than no
field, so the mapping is pinned here.
"""

from __future__ import annotations

from garmin.pullers.activities import _session_context_fields, _split_summary_fields

_ACTIVITY = {
    "movingDuration": 1620.0,
    "elapsedDuration": 5400.0,
    "isManualActivity": False,
    "differenceBodyBattery": -13,
    "waterEstimated": 1484.0,
    "bmrCalories": 237.0,
    "steps": 1370,
    "lapCount": 1,
    "deviceId": 3427456736,
    "manufacturer": "GARMIN",
    "startTimeGMT": "2026-08-16 18:53:19",
    "endTimeGMT": "2026-08-16 21:19:03",
    "beginTimestamp": 1786906399000,
    "timeZoneId": 121,
    "aerobicTrainingEffectMessage": "MINOR_AEROBIC_BENEFIT_0",
    "anaerobicTrainingEffectMessage": "MAINTAINING_FAST_FORCE_PRODUCTION_6",
    "isPR": False,
    "hasSplits": True,
    "hasPolyline": False,
}


def test_moving_and_elapsed_are_kept_separately():
    """`duration` alone cannot tell a paused session from a continuous one."""
    fields = _session_context_fields(_ACTIVITY)
    assert fields["moving_duration_s"] == 1620.0
    assert fields["elapsed_duration_s"] == 5400.0


def test_manual_flag_is_captured_from_either_spelling():
    """Garmin sends both `isManualActivity` and `manualActivity`."""
    assert _session_context_fields({"isManualActivity": True})["is_manual"] is True
    assert _session_context_fields({"manualActivity": True})["is_manual"] is True


def test_a_false_manual_flag_survives():
    """`or`-style coercion would turn False into None and lose the distinction
    between 'Garmin says not manual' and 'Garmin said nothing'."""
    assert _session_context_fields({"isManualActivity": False})["is_manual"] is False
    assert _session_context_fields({})["is_manual"] is None


def test_zero_body_battery_change_is_not_treated_as_missing():
    """A session that cost zero body battery is a measurement, not a gap."""
    assert _session_context_fields({"differenceBodyBattery": 0})["body_battery_change"] == 0


def test_every_field_is_none_when_garmin_sends_nothing():
    fields = _session_context_fields({})
    assert set(fields) and all(v is None for v in fields.values())


def test_context_fields_are_mapped_end_to_end():
    fields = _session_context_fields(_ACTIVITY)
    assert fields["body_battery_change"] == -13
    assert fields["bmr_calories"] == 237.0
    assert fields["steps"] == 1370
    assert fields["device_id"] == 3427456736
    assert fields["time_zone_id"] == 121
    assert fields["aerobic_te_message"] == "MINOR_AEROBIC_BENEFIT_0"


def test_climb_summary_prefers_the_active_split():
    """Bouldering reports several split summaries; the climbing one carries the
    grade and completed-climb count, which exist in no other field we pull."""
    activity = {"splitSummaries": [
        {"splitType": "CLIMB_REST", "noOfSplits": 20},
        {"splitType": "CLIMB_ACTIVE", "noOfSplits": 10, "numClimbsCompleted": 10,
         "maxGradeValue": {"valueKey": "V5", "scale": "VERMIN"}},
    ]}
    fields = _split_summary_fields(activity)
    assert fields["split_type"] == "CLIMB_ACTIVE"
    assert fields["climbs_completed"] == 10
    assert fields["max_grade"] == "V5"


def test_climb_summary_falls_back_to_the_largest_split_set():
    activity = {"splitSummaries": [{"splitType": "RWD_RUN", "noOfSplits": 3},
                                   {"splitType": "RWD_WALK", "noOfSplits": 9}]}
    assert _split_summary_fields(activity)["split_type"] == "RWD_WALK"


def test_missing_split_summaries_yields_nulls_not_an_error():
    for activity in ({}, {"splitSummaries": None}, {"splitSummaries": []}):
        fields = _split_summary_fields(activity)
        assert fields == {"climbs_completed": None, "max_grade": None, "split_type": None}
