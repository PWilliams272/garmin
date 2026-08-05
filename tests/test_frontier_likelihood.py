"""Fast tests for the stochastic-frontier effort model's building blocks.

The sampling itself is exercised in tests/test_strength_curve.py (marked
slow). Everything here is deterministic and runs in the default suite,
because these are exactly the pieces whose breakage is silent: a log-CDF that
returns -inf, or a covariate matrix that mislabels which set was the warmup,
produces a fit that still looks plausible.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from garmin.analysis.strength_curve import (
    FATIGUE_ONSET_POSITION,
    LIKELIHOOD_ASYMMETRIC,
    LIKELIHOOD_FRONTIER,
    _effort_covariates,
    _log_normal_cdf,
    _prepare_exercise_sets,
    fit_strength_curves,
)


def _compiled_log_phi(mode: str = "JAX"):
    import pytensor
    import pytensor.tensor as pt

    z = pt.vector("z")
    return pytensor.function([z], _log_normal_cdf(pt, z), mode=mode)


def test_log_normal_cdf_matches_scipy():
    scipy_stats = pytest.importorskip("scipy.stats")
    f = _compiled_log_phi()
    z = np.array([-100.0, -40.0, -20.0, -10.0, -8.0, -6.001, -5.999, -3.0, -1.0, 0.0, 1.0, 5.0])
    assert np.allclose(f(z), scipy_stats.norm.logcdf(z), atol=1e-3)


def test_log_normal_cdf_is_finite_deep_in_the_tail():
    """The whole reason for the tail branch: erfc underflows to zero out here
    and a plain log would hand the sampler -inf."""
    f = _compiled_log_phi()
    values = f(np.array([-50.0, -200.0, -1000.0]))
    assert np.all(np.isfinite(values))
    assert np.all(values < 0)


def test_log_normal_cdf_is_continuous_across_the_branch_switch():
    f = _compiled_log_phi()
    below, above = f(np.array([-6.0001]))[0], f(np.array([-5.9999]))[0]
    assert abs(below - above) < 1e-3


def test_log_normal_cdf_gradient_is_finite_everywhere():
    """pt.switch evaluates both branches, so an inf in the discarded side
    still poisons the gradient even though the value looks correct."""
    import pytensor
    import pytensor.tensor as pt

    z = pt.vector("z")
    grad = pytensor.function([z], pt.grad(_log_normal_cdf(pt, z).sum(), z), mode="JAX")
    assert np.all(np.isfinite(grad(np.array([-100.0, -6.0, -1.0, 0.0, 3.0]))))


def test_log_normal_cdf_compiles_under_jax():
    """PyMC's own Normal logcdf uses erfcx, which has no JAX kernel -- so this
    is the specific thing that has to keep working for numpyro sampling."""
    assert np.all(np.isfinite(_compiled_log_phi("JAX")(np.array([-8.0, 0.0, 2.0]))))


def test_effort_covariates_flag_the_opening_set():
    x = _effort_covariates(np.array([1.0, 2.0, 3.0]))
    assert x[0, 0] == 1.0
    assert x[1, 0] == 0.0 and x[2, 0] == 0.0


def test_effort_covariates_ramp_only_after_fatigue_onset():
    positions = np.arange(1.0, FATIGUE_ONSET_POSITION + 4)
    fatigue = _effort_covariates(positions)[:, 1]
    assert np.all(fatigue[: FATIGUE_ONSET_POSITION] == 0.0)
    assert fatigue[-1] > fatigue[-2] > 0.0


def test_set_position_is_per_exercise_not_per_activity():
    """Garmin numbers sets across the whole activity, so in an alternating
    session the first curl set carries a high set_number. Using that directly
    would label a warmup as a late-session set and invert the effort model."""
    frame = pd.DataFrame([
        {"activity_id": "a", "set_number": 1, "date": pd.Timestamp("2024-01-01"),
         "reps": 8, "weight_lb": 100.0},
        {"activity_id": "a", "set_number": 2, "date": pd.Timestamp("2024-01-01"),
         "reps": 8, "weight_lb": 110.0},
        {"activity_id": "a", "set_number": 3, "date": pd.Timestamp("2024-01-01"),
         "reps": 8, "weight_lb": 120.0},
    ])
    prepared = _prepare_exercise_sets(frame)
    assert prepared["set_position"].tolist() == [1, 2, 3]


def test_set_position_restarts_each_session():
    frame = pd.DataFrame([
        {"activity_id": "a", "set_number": 1, "date": pd.Timestamp("2024-01-01"),
         "reps": 8, "weight_lb": 100.0},
        {"activity_id": "a", "set_number": 2, "date": pd.Timestamp("2024-01-01"),
         "reps": 8, "weight_lb": 100.0},
        {"activity_id": "b", "set_number": 1, "date": pd.Timestamp("2024-01-08"),
         "reps": 8, "weight_lb": 100.0},
    ])
    prepared = _prepare_exercise_sets(frame)
    assert prepared["set_position"].tolist() == [1, 2, 1]


def test_prepare_exercise_sets_works_without_activity_id():
    """Synthetic frames and older callers pass only date/reps/weight."""
    frame = pd.DataFrame([
        {"date": pd.Timestamp("2024-01-01"), "reps": 8, "weight_lb": 100.0},
        {"date": pd.Timestamp("2024-01-01"), "reps": 8, "weight_lb": 110.0},
    ])
    prepared = _prepare_exercise_sets(frame)
    assert prepared["set_position"].tolist() == [1, 2]


def test_unknown_likelihood_is_rejected():
    with pytest.raises(ValueError, match="unknown likelihood"):
        fit_strength_curves({}, likelihood="not_a_likelihood")


def test_both_likelihood_names_are_accepted():
    """An empty input returns early, so this reaches validation without
    paying for a fit."""
    for name in (LIKELIHOOD_FRONTIER, LIKELIHOOD_ASYMMETRIC):
        curves, betas, diagnostics = fit_strength_curves({}, likelihood=name)
        assert curves == {}
        assert not diagnostics["converged"]
