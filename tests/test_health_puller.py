"""Tests for the weight/body-composition post-processing.

The failure this guards against is silent by construction: Garmin reports 0 for
body-composition fields when the scale sent only a weight, and a 0 that survives
into curated parquet reads as a real measurement rather than a missing one.
Nothing downstream can tell the difference after the fact.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from garmin.pullers.health import HealthPuller

#: grams -> pounds, matching the puller's own constant.
_LB_PER_G = 0.00220462


def _puller() -> HealthPuller:
    """A puller with no session -- these tests only touch post-processing."""
    return HealthPuller(session=None)


def _frame(rows: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    return frame.set_index(pd.to_datetime(frame.pop("date")))


def test_composition_zeros_become_null_not_measurements():
    """The actual bug: 43 days in 2019-2020 carried bmi/body_fat/fat_mass = 0."""
    out = _puller()._post_process_weight(_frame([
        {"date": "2020-01-01", "weight": 80000.0, "bmi": 0.0, "body_fat": 0.0,
         "body_water": 0.0, "bone_mass": 0.0, "muscle_mass": 0.0},
    ]))
    for column in ("bmi", "body_fat", "body_water", "bone_mass", "muscle_mass"):
        assert out[column].isna().all(), f"{column} kept a placeholder zero"


def test_weight_survives_when_composition_is_missing():
    """A weight-only scale still gives a real weight -- nulling the composition
    must not discard it."""
    out = _puller()._post_process_weight(_frame([
        {"date": "2020-01-01", "weight": 80000.0, "bmi": 0.0, "body_fat": 0.0,
         "body_water": 0.0, "bone_mass": 0.0, "muscle_mass": 0.0},
    ]))
    assert out["weight"].iloc[0] == 80000.0 * _LB_PER_G


def test_fat_mass_is_null_rather_than_zero_without_body_fat():
    """fat_mass is derived as body_fat * weight, so a zero body_fat used to
    produce a 0.0 fat_mass that looked like a reading."""
    out = _puller()._post_process_weight(_frame([
        {"date": "2020-01-01", "weight": 80000.0, "bmi": 0.0, "body_fat": 0.0,
         "body_water": 0.0, "bone_mass": 0.0, "muscle_mass": 0.0},
    ]))
    assert out["fat_mass"].isna().all()


def test_real_composition_readings_are_preserved():
    """The negative control: the fix must not touch genuine measurements."""
    out = _puller()._post_process_weight(_frame([
        {"date": "2024-05-01", "weight": 80000.0, "bmi": 24.2, "body_fat": 18.5,
         "body_water": 55.0, "bone_mass": 3000.0, "muscle_mass": 60000.0},
    ]))
    assert out["bmi"].iloc[0] == 24.2
    assert out["body_fat"].iloc[0] == 18.5
    assert out["fat_mass"].iloc[0] == np.float64(18.5 * 80000.0 * _LB_PER_G / 100.0)


def test_a_real_reading_wins_over_a_placeholder_on_the_same_day():
    """Two weigh-ins in one day, one from each scale. Averaging 0 with 18.5
    would report 9.25% body fat -- a plausible-looking number that is simply
    wrong. This is why the zeros are nulled before the groupby."""
    out = _puller()._post_process_weight(_frame([
        {"date": "2024-05-01", "weight": 80000.0, "bmi": 0.0, "body_fat": 0.0,
         "body_water": 0.0, "bone_mass": 0.0, "muscle_mass": 0.0},
        {"date": "2024-05-01", "weight": 80000.0, "bmi": 24.2, "body_fat": 18.5,
         "body_water": 55.0, "bone_mass": 3000.0, "muscle_mass": 60000.0},
    ]))
    assert len(out) == 1
    assert out["body_fat"].iloc[0] == 18.5
    assert out["bmi"].iloc[0] == 24.2


def test_gaps_between_weigh_ins_are_filled_with_null_rows():
    """asfreq('D') makes skipped days explicit rather than absent. Peter has a
    194-day weight gap in 2023, and it must read as unmeasured, not as zero."""
    out = _puller()._post_process_weight(_frame([
        {"date": "2024-05-01", "weight": 80000.0, "bmi": 24.2, "body_fat": 18.5,
         "body_water": 55.0, "bone_mass": 3000.0, "muscle_mass": 60000.0},
        {"date": "2024-05-04", "weight": 80500.0, "bmi": 24.4, "body_fat": 18.7,
         "body_water": 55.0, "bone_mass": 3000.0, "muscle_mass": 60000.0},
    ]))
    assert len(out) == 4
    assert out["weight"].isna().sum() == 2
