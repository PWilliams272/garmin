"""Tests for the from-scratch training-load reconstructions.

The formulas themselves are simple; what is easy to get wrong is the time
handling. Garmin's "smart recording" emits irregular sample intervals, so any
metric that assumes 1 Hz silently rescales itself per sport -- which would show
up as a spurious per-sport difference in exactly the comparison these functions
exist to support.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from garmin.scripts.manual_validate_training_load import (
    MAX_SAMPLE_GAP_S,
    banister_trimp,
    edwards_trimp,
    hr_integral,
    sample_durations,
)

_FLOORS = (100.0, 116.0, 136.0, 160.0, 177.0)


def _stamps(offsets_s):
    base = pd.Timestamp("2026-01-01 12:00:00")
    return pd.Series([base + pd.Timedelta(seconds=s) for s in offsets_s])


def test_sample_durations_uses_real_intervals_not_a_1hz_assumption():
    """A 4 s-cadence activity must not be counted as 1 s per sample."""
    durations = sample_durations(_stamps([0, 4, 8, 12]))
    assert list(durations[:3]) == [4.0, 4.0, 4.0]
    assert durations.sum() == 16.0


def test_a_long_gap_is_treated_as_a_pause_not_as_training_time():
    """An auto-pause would otherwise charge the whole stopped period as load."""
    durations = sample_durations(_stamps([0, 1, 2, 2 + MAX_SAMPLE_GAP_S * 10, 3600]))
    assert durations.max() <= MAX_SAMPLE_GAP_S
    assert durations.sum() < 20


def test_integral_counts_only_heart_rate_above_resting():
    hr = np.array([40.0, 100.0])          # 10 below resting, then 50 above
    dt = np.array([60.0, 60.0])
    # Only the second sample contributes: 50 bpm for one minute.
    assert hr_integral(hr, dt, resting_hr=50.0) == 50.0


def test_banister_weights_hard_minutes_disproportionately():
    """Doubling intensity must more than double the score -- that is the point
    of the exponential form, and what separates it from the plain integral."""
    dt = np.array([60.0])
    easy = banister_trimp(np.array([100.0]), dt, 50.0, 200.0)   # HRR 1/3
    hard = banister_trimp(np.array([150.0]), dt, 50.0, 200.0)   # HRR 2/3
    assert hard > 2 * easy


def test_edwards_weights_each_zone_by_its_rank():
    """One minute in zone 5 is worth five minutes in zone 1."""
    minute = np.array([60.0])
    zone1 = edwards_trimp(np.array([105.0]), minute, _FLOORS)
    zone5 = edwards_trimp(np.array([180.0]), minute, _FLOORS)
    assert zone1 == 1.0
    assert zone5 == 5.0


def test_edwards_ignores_time_below_the_first_zone_floor():
    below = edwards_trimp(np.array([80.0]), np.array([600.0]), _FLOORS)
    assert below == 0.0


def test_edwards_respects_zone_boundaries_exactly():
    """A sample sitting exactly on a floor belongs to the higher zone."""
    minute = np.array([60.0])
    assert edwards_trimp(np.array([136.0]), minute, _FLOORS) == 3.0
    assert edwards_trimp(np.array([135.0]), minute, _FLOORS) == 2.0


def test_all_three_metrics_scale_with_duration():
    hr = np.full(10, 150.0)
    short = np.full(10, 30.0)
    long = np.full(10, 60.0)
    for metric in (
        lambda dt: hr_integral(hr, dt, 50.0),
        lambda dt: banister_trimp(hr, dt, 50.0, 200.0),
        lambda dt: edwards_trimp(hr, dt, _FLOORS),
    ):
        assert metric(long) == 2 * metric(short)


def test_effect_formula_recovers_known_coefficients():
    """Synthetic data built from the assumed structure must be recovered.

    Guards the claim that Garmin's load is a fixed function of its two
    training-effect scores: if the fitter cannot recover parameters it was
    handed, the fit on real data means nothing.
    """
    from garmin.scripts.manual_validate_training_load import fit_effect_formula

    rng = np.random.default_rng(0)
    aerobic = rng.uniform(0, 5, 400)
    anaerobic = rng.uniform(0, 3, 400)
    coefficient, exponent = 11.5, 0.66
    load = coefficient * (np.expm1(exponent * aerobic) + np.expm1(exponent * anaerobic))
    table = pd.DataFrame({
        "training_load": load, "aerobic_te": aerobic, "anaerobic_te": anaerobic,
        "sport": ["running"] * 400,
    })
    fitted = fit_effect_formula(table)
    assert abs(fitted["exponent"] - exponent) < 0.02
    assert abs(fitted["coefficient"] - coefficient) < 0.3
    assert fitted["r2"] > 0.999


def test_effect_formula_is_not_fooled_by_unrelated_load():
    """A load column with no relation to the effect scores must not fit well."""
    from garmin.scripts.manual_validate_training_load import fit_effect_formula

    rng = np.random.default_rng(1)
    table = pd.DataFrame({
        "training_load": rng.uniform(20, 200, 300),
        "aerobic_te": rng.uniform(0, 5, 300),
        "anaerobic_te": rng.uniform(0, 3, 300),
        "sport": ["running"] * 300,
    })
    assert fit_effect_formula(table)["r2"] < 0.3


def test_zero_anaerobic_effect_is_kept_not_treated_as_missing():
    """Hiking's median anaerobic effect is exactly 0.0 -- a real measurement."""
    from garmin.scripts.manual_validate_training_load import _as_float

    assert _as_float(0.0) == 0.0
    assert np.isnan(_as_float(None))
