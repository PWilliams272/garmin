from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from garmin.analysis.strength_curve import (
    FATIGUE_ONSET_POSITION,
    LIKELIHOOD_FRONTIER,
    KNOT_SPACING_DAYS,
    RHAT_THRESHOLD,
    _build_design,
    _knot_interpolation,
    _prepare_exercise_sets,
    fit_strength_curves,
)

pytestmark = pytest.mark.slow


def _synthetic_sets(
    true_beta: float = 0.13,
    n_sessions: int = 40,
    start_1rm: float = 200.0,
    growth: float = 0.004,
    set_fractions: tuple[float, ...] = (0.55, 0.70, 0.95, 0.97, 1.0, 0.98),
    seed: int = 0,
) -> pd.DataFrame:
    """Sets from a known load-rep curve with a known strength trajectory.

    `set_fractions` mimics a real session: the first two are warmups well
    below capacity, the rest are working sets near it.
    """
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2024-01-01", periods=n_sessions, freq="4D")
    rows = []
    for i, date in enumerate(dates):
        alpha = np.log(start_1rm * np.exp(growth * i))
        for fraction in set_fractions:
            reps = int(rng.integers(5, 11))
            rows.append({
                "date": date,
                "reps": reps,
                "weight_lb": np.exp(alpha - true_beta * np.log(reps)) * fraction,
            })
    return pd.DataFrame(rows)


def test_prepare_exercise_sets_drops_zero_and_missing_rows() -> None:
    raw = pd.DataFrame({
        "date": pd.to_datetime(["2024-01-01"] * 4),
        "reps": [8.0, 0.0, 8.0, np.nan],
        "weight_lb": [100.0, 100.0, 0.0, 100.0],
    })

    prepared = _prepare_exercise_sets(raw)

    # Only the first row is usable -- a log-log curve is undefined at zero.
    assert len(prepared) == 1
    assert prepared.iloc[0]["weight_lb"] == 100.0


def test_build_design_skips_exercises_under_the_session_threshold() -> None:
    sparse = _synthetic_sets(n_sessions=3)
    dense = _synthetic_sets(n_sessions=20)

    design, exercises = _build_design({"sparse": sparse, "dense": dense}, min_sessions=8)

    assert exercises == ["dense"]
    # 20 sessions at 4-day spacing spans 76 days, so the monthly knot grid is
    # far smaller than one node per session -- that reduction is the whole
    # point (per-session nodes gave ~1500 params and r-hat 2.9).
    span_days = 19 * 4
    assert design["n_alpha"] == int(np.ceil(span_days / KNOT_SPACING_DAYS)) + 1
    assert design["n_alpha"] < 20


def test_build_design_places_sessions_on_the_knot_grid_by_elapsed_time() -> None:
    """Two sessions a day apart must land on essentially the same knot, while
    one four months later sits several knots along -- the grid tracks calendar
    time, not session count, so a layoff isn't collapsed to one step."""
    sets = pd.DataFrame({
        "date": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-05-01"]),
        "reps": [8.0, 8.0, 8.0],
        "weight_lb": [100.0, 100.0, 100.0],
    })

    design, _ = _build_design({"x": sets}, min_sessions=2)

    lower, _upper, weight = design["session_interp"][0]
    assert lower[0] == 0 and weight[0] == pytest.approx(0.0)
    # 1 day in is a small fraction of a 30-day knot span.
    assert lower[1] == 0 and weight[1] == pytest.approx(1 / KNOT_SPACING_DAYS, abs=1e-6)
    # 121 days in -> knot 4 of a 0..4 grid.
    assert lower[2] == 4


def test_knot_interpolation_weights_sum_to_one_across_the_pair() -> None:
    dates = pd.DatetimeIndex(["2024-01-01", "2024-01-16", "2024-01-31"])
    lower, upper, weight = _knot_interpolation(dates, pd.Timestamp("2024-01-01"), 3)

    assert np.all(weight >= 0) and np.all(weight <= 1)
    assert np.all(upper >= lower)
    # Mid-month sits halfway between knot 0 and knot 1.
    assert weight[1] == pytest.approx(0.5, abs=0.02)


def test_fit_strength_curves_recovers_a_known_rep_decay_exponent() -> None:
    true_beta = 0.13
    curves, betas, _ = fit_strength_curves(
        {"bench_press": _synthetic_sets(true_beta=true_beta)},
        draws=800, tune=800, chains=2,
    )

    row = betas.iloc[0]
    assert row["beta_lower_95"] < true_beta < row["beta_upper_95"]
    assert row["beta_mean"] == pytest.approx(true_beta, abs=0.04)

    curve = curves["bench_press"]
    # Trajectory: 200 lb growing at 0.004/session over 40 sessions -> ~234.
    assert curve["e1rm_mean"].iloc[0] == pytest.approx(200.0, rel=0.10)
    assert curve["e1rm_mean"].iloc[-1] == pytest.approx(233.8, rel=0.10)
    assert curve["e1rm_mean"].iloc[-1] > curve["e1rm_mean"].iloc[0]


def test_fit_strength_curves_widens_the_interval_for_extrapolated_1rm() -> None:
    """The whole point of headlining e8RM: 8 reps is inside the trained range
    (interpolation) while 1 rep is past the edge of the data, so its interval
    must be visibly wider rather than falsely precise."""
    curves, _, _ = fit_strength_curves(
        {"bench_press": _synthetic_sets()}, draws=800, tune=800, chains=2,
    )
    curve = curves["bench_press"]

    width_1rm = (curve["e1rm_upper_95"] - curve["e1rm_lower_95"]).mean()
    width_8rm = (curve["e8rm_upper_95"] - curve["e8rm_lower_95"]).mean()
    assert width_1rm > 2 * width_8rm


def test_fit_strength_curves_is_not_dragged_down_by_warmup_sets() -> None:
    """A symmetric fit through the middle of the set cloud is badly biased by
    submaximal work -- pooled OLS on this account's real bench data returns
    beta=0.61 against a realistic ~0.13. The asymmetric likelihood should make
    adding more warmups barely move the estimate."""
    working_only = _synthetic_sets(set_fractions=(0.95, 0.97, 1.0, 0.98))
    with_warmups = _synthetic_sets(set_fractions=(0.4, 0.5, 0.6, 0.95, 0.97, 1.0, 0.98))

    curves_working, _, _ = fit_strength_curves(
        {"x": working_only}, draws=800, tune=800, chains=2)
    curves_warmup, _, _ = fit_strength_curves(
        {"x": with_warmups}, draws=800, tune=800, chains=2)

    clean = curves_working["x"]["e1rm_mean"].mean()
    noisy = curves_warmup["x"]["e1rm_mean"].mean()
    assert noisy == pytest.approx(clean, rel=0.10)


def test_fit_strength_curves_returns_empty_when_nothing_clears_the_threshold() -> None:
    curves, betas, _ = fit_strength_curves({"x": _synthetic_sets(n_sessions=2)})

    assert curves == {}
    assert betas.empty


def test_fit_strength_curves_reports_convergence_diagnostics() -> None:
    """Callers gate persistence on this, so it has to be present and honest."""
    _, _, diagnostics = fit_strength_curves(
        {"bench_press": _synthetic_sets()}, draws=800, tune=800, chains=2,
    )

    assert set(diagnostics) == {"max_rhat", "divergences", "converged"}
    assert diagnostics["converged"] is True
    assert diagnostics["max_rhat"] <= RHAT_THRESHOLD


def _frontier_sets(
    *,
    true_beta: float = 0.13,
    warmup_coef: float = 1.6,
    fatigue_coef: float = 0.25,
    n_sessions: int = 60,
    seed: int = 4,
) -> pd.DataFrame:
    """Sets generated from the frontier process itself: capacity, minus an
    exponential effort shortfall whose mean depends on set position."""
    rng = np.random.default_rng(seed)
    start = pd.Timestamp("2024-01-01")
    rows = []
    for session in range(n_sessions):
        capacity = np.log(200.0) + 0.25 * session / n_sessions
        for position in range(1, 7):
            reps = int(rng.integers(5, 11))
            mean_shortfall = np.exp(
                -3.0
                + warmup_coef * (position == 1)
                + fatigue_coef * max(0, position - FATIGUE_ONSET_POSITION)
            )
            log_w = (
                capacity
                - true_beta * np.log(reps)
                - rng.exponential(mean_shortfall)
                + rng.normal(0, 0.02)
            )
            rows.append({
                "activity_id": f"a{session}",
                "set_number": position,
                "date": start + pd.Timedelta(days=5 * session),
                "reps": reps,
                "weight_lb": float(np.exp(log_w)),
            })
    return pd.DataFrame(rows)


def test_frontier_recovers_beta_and_the_strength_trajectory() -> None:
    """End-to-end check that the generative effort model reads back what
    generated it -- both the rep-decay exponent and the capacity path."""
    true_beta = 0.13
    frame = _frontier_sets(true_beta=true_beta)
    curves, betas, _ = fit_strength_curves(
        {"bench_press": frame, "curl": frame.assign(weight_lb=frame["weight_lb"] * 0.3)},
        likelihood=LIKELIHOOD_FRONTIER, draws=500, tune=1000, chains=2,
    )

    row = betas[betas["exercise"] == "bench_press"].iloc[0]
    assert row["beta_lower_95"] < true_beta < row["beta_upper_95"]

    curve = curves["bench_press"]
    assert curve["e1rm_mean"].iloc[0] == pytest.approx(200.0, rel=0.10)
    # Capacity rises by exp(0.25) over the window.
    assert curve["e1rm_mean"].iloc[-1] == pytest.approx(200.0 * np.exp(0.25), rel=0.10)


def test_frontier_tracks_capacity_not_the_average_set() -> None:
    """The frontier must sit at the top of the cloud. Most sets are below
    capacity by construction, so a fit through the middle would land well
    under the true value -- this is the bias the whole likelihood exists to
    avoid."""
    frame = _frontier_sets()
    curves, _, _ = fit_strength_curves(
        {"bench_press": frame, "curl": frame.assign(weight_lb=frame["weight_lb"] * 0.3)},
        likelihood=LIKELIHOOD_FRONTIER, draws=500, tune=1000, chains=2,
    )
    fitted = curves["bench_press"]["e1rm_mean"].to_numpy()

    # Best observed single set, converted to a 1RM-equivalent at its own reps.
    observed_top = (
        frame.assign(e1rm=frame["weight_lb"] * frame["reps"] ** 0.13)["e1rm"].max()
    )
    mean_set = (frame["weight_lb"] * frame["reps"] ** 0.13).mean()
    assert fitted.max() > mean_set * 1.05
    assert fitted.max() == pytest.approx(observed_top, rel=0.15)
