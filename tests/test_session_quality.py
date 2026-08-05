"""Tests for submaximal-session rejection.

The property that matters most is the one that isn't obvious: rejection must
be one-sided *and* judged locally, or it quietly deletes every real setback
and manufactures a trend that only ever rises.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from garmin.analysis.session_quality import (
    MAX_SHORTFALL_LOG,
    capacity_sessions,
    flag_submaximal_sessions,
)


def _sessions(values, *, n_sets=5, family="bench", start="2024-01-01", step=5):
    start = pd.Timestamp(start)
    counts = n_sets if isinstance(n_sets, list) else [n_sets] * len(values)
    return pd.DataFrame({
        "family": family,
        "date": [start + pd.Timedelta(days=step * i) for i in range(len(values))],
        "est_1rm": values,
        "n_sets": counts,
    })


def test_steady_training_is_all_kept():
    rng = np.random.default_rng(0)
    frame = _sessions(list(200 + rng.normal(0, 3, 40)))
    assert not flag_submaximal_sessions(frame)["submaximal"].any()


def test_isolated_easy_day_is_flagged():
    values = [200.0] * 40
    values[20] = 120.0  # 40% down: nothing like a normal session
    flagged = flag_submaximal_sessions(_sessions(values))
    assert flagged["submaximal"].sum() == 1
    assert flagged.loc[flagged["submaximal"], "est_1rm"].iloc[0] == 120.0


def test_abandoned_single_set_session_is_flagged():
    """The reported case: one warmup set, then a change of plan. Its value can
    look reasonable, so the set count is what gives it away."""
    counts = [5] * 40
    counts[20] = 1
    flagged = flag_submaximal_sessions(_sessions([200.0] * 40, n_sets=counts))
    assert flagged["submaximal"].sum() == 1
    assert flagged.loc[flagged["submaximal"], "n_sets"].iloc[0] == 1


def test_exceptional_day_is_never_rejected():
    """One-sided by design: you cannot lift more than you are capable of, so a
    high session is information, not an outlier. A symmetric rule would throw
    away the most informative sessions there are."""
    values = [200.0] * 40
    values[20] = 260.0
    assert not flag_submaximal_sessions(_sessions(values))["submaximal"].any()


def test_a_genuine_sustained_decline_is_preserved():
    """The property that makes this safe. Detraining, injury or time off
    should move the trend down -- if the filter treated a real decline as a
    run of outliers it would erase every setback and only ever show progress."""
    values = [200.0] * 25 + [140.0] * 25
    flagged = flag_submaximal_sessions(_sessions(values))
    declined = flagged[flagged["est_1rm"] == 140.0]
    # A handful right at the transition may trip the local median; the body of
    # the decline must survive.
    assert declined["submaximal"].mean() < 0.25
    assert not declined["submaximal"].iloc[-5:].any()


def test_gradual_decline_is_fully_preserved():
    values = list(np.linspace(220, 150, 50))
    assert not flag_submaximal_sessions(_sessions(values))["submaximal"].any()


def test_families_are_judged_independently():
    """A curl at 30 lb is not a submaximal bench press."""
    frame = pd.concat([
        _sessions([200.0] * 20, family="bench"),
        _sessions([30.0] * 20, family="curl"),
    ], ignore_index=True)
    assert not flag_submaximal_sessions(frame)["submaximal"].any()


def test_shortfall_sign_convention():
    values = [200.0] * 40
    values[20] = 100.0
    flagged = flag_submaximal_sessions(_sessions(values))
    low = flagged.loc[flagged["est_1rm"] == 100.0, "shortfall"].iloc[0]
    assert low > 0  # positive shortfall means "below the local level"
    assert low > MAX_SHORTFALL_LOG


def test_capacity_sessions_drops_the_flagged_ones():
    values = [200.0] * 40
    values[10] = 100.0
    kept = capacity_sessions(_sessions(values))
    assert len(kept) == 39
    assert 100.0 not in set(kept["est_1rm"])


def test_capacity_sessions_keeps_everything_when_data_is_thin():
    """A sparsely-trained exercise is better served by noisy data than none."""
    frame = _sessions([200.0, 100.0, 200.0])
    assert len(capacity_sessions(frame)) == 3


def test_empty_input_returns_the_expected_columns():
    empty = pd.DataFrame(columns=["family", "date", "est_1rm", "n_sets"])
    flagged = flag_submaximal_sessions(empty)
    assert {"local_level", "shortfall", "submaximal"} <= set(flagged.columns)
    assert flagged.empty


def test_no_columns_are_left_null():
    """A NaN here reaches the page payload, where it is not valid JSON."""
    flagged = flag_submaximal_sessions(_sessions([200.0] * 30))
    assert not flagged["shortfall"].isna().any()
    assert not flagged["submaximal"].isna().any()
