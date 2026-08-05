"""Tests for the predictive baseline.

Most of these guard the *evaluation*, not the models. A leaky split or a
mis-specified baseline produces flattering numbers that look entirely
plausible, which is the failure mode that matters here.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from garmin.analysis.predictive import (
    _feature_groups,
    _forward_chaining_splits,
    _prepare,
    ablate_target,
    evaluate_target,
    format_ablation,
    format_report,
)


def _panel(n=600, seed=0, training_effect=0.0):
    """Synthetic panel. `training_effect` injects a genuine two-day-lagged
    influence of load on the outcome, so the ablation can be checked against a
    known answer in both directions."""
    rng = np.random.default_rng(seed)
    load = rng.gamma(2.0, 20.0, n)
    hrv = np.zeros(n)
    hrv[0] = 60.0
    for i in range(1, n):
        lagged = load[i - 2] if i >= 2 else 0.0
        hrv[i] = (
            0.6 * hrv[i - 1] + 0.4 * 60.0
            + training_effect * (lagged - load.mean()) / load.std()
            + rng.normal(0, 3)
        )
    return pd.DataFrame({
        "date": pd.date_range("2023-01-01", periods=n, freq="D"),
        "hrv": hrv,
        "duration_min": load,
        "hr_load": load * 50,
        "sessions": (load > load.mean()).astype(int),
        "days_since_running": rng.integers(0, 5, n).astype(float),
        "day_of_week": np.arange(n) % 7,
    })


def test_forward_chaining_never_trains_on_the_future():
    """The single most important property: a random split would put a Tuesday
    in training and the Wednesday either side in test, and the model would
    score far better than it could in practice."""
    for train_idx, test_idx in _forward_chaining_splits(600):
        assert train_idx.max() < test_idx.min()


def test_forward_chaining_windows_expand():
    sizes = [len(train) for train, _ in _forward_chaining_splits(600)]
    assert sizes == sorted(sizes)
    assert len(sizes) >= 3


def test_prepare_predicts_the_following_day():
    frame = _panel(n=300)
    features, outcome, persistence = _prepare(frame, "hrv")
    # Persistence for row t is day t's value; the outcome is day t+1's.
    assert persistence.iloc[0] == frame["hrv"].iloc[0]
    assert outcome.iloc[0] == frame["hrv"].iloc[1]


def test_prepare_leaves_no_nulls_in_the_feature_matrix():
    features, _, _ = _prepare(_panel(n=300), "hrv")
    assert not features.isna().any().any()


def test_persistence_baseline_is_beaten_on_a_smoothable_series():
    """A mean-reverting series is genuinely smoothable, so the model should
    beat 'tomorrow looks like today'. If it can't, the evaluation is broken."""
    result = evaluate_target(_panel(n=600), "hrv")
    assert result is not None
    assert result["skill_vs_persistence"]["ridge"] > 0


def test_no_training_gain_when_training_does_not_affect_the_outcome():
    """The negative control. Load is generated independently of the outcome
    here, so any apparent gain would be overfitting rather than signal."""
    result = ablate_target(_panel(n=600, training_effect=0.0), "hrv")
    assert result is not None
    assert result["training_gain"] < 0.02


def test_training_gain_is_detected_when_the_effect_is_real():
    """The positive control: with a strong injected lag-2 effect the ablation
    must find it, or a null result elsewhere would be meaningless."""
    result = ablate_target(_panel(n=600, training_effect=6.0, seed=3), "hrv")
    assert result is not None
    assert result["training_gain"] > 0.02


def test_feature_groups_separate_training_from_own_history():
    features, _, _ = _prepare(_panel(n=300), "hrv")
    groups = _feature_groups(list(features.columns), "hrv")
    assert "duration_min" in groups["own_plus_training"]
    assert "duration_min" not in groups["own_history"]
    assert any(c.startswith("hrv_lag") for c in groups["own_history"])


def test_short_series_returns_none_rather_than_a_fragile_number():
    assert evaluate_target(_panel(n=50), "hrv") is None
    assert ablate_target(_panel(n=50), "hrv") is None


def test_missing_target_returns_none():
    assert evaluate_target(_panel(n=300), "not_a_column") is None


def test_reports_render_without_data():
    assert "Not enough data" in format_report([])
    assert "Not enough data" in format_ablation([])


def test_report_names_the_verdict():
    result = evaluate_target(_panel(n=600), "hrv")
    assert "persistence" in format_report([result])
