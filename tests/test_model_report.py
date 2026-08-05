from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from garmin.analysis.model_report import (
    MIN_SETS_FOR_RELIABLE_CORR,
    _corr_ci,
    _load_type_evidence,
    _warmup_bias_evidence,
    build_model_report,
)
from garmin.io.curated_store import CuratedDataStore
from garmin.io.file_manager import FileManager


def test_corr_ci_widens_as_sample_shrinks() -> None:
    """The whole point of reporting an interval: chop (n=17) and pull_up
    (n=161) had similar-looking correlations but wildly different evidence."""
    lo_small, hi_small = _corr_ci(0.5, 17)
    lo_large, hi_large = _corr_ci(0.5, 161)

    assert (hi_small - lo_small) > 2 * (hi_large - lo_large)
    assert lo_large > 0  # well-sampled positive correlation clears zero


def test_corr_ci_handles_degenerate_input() -> None:
    assert all(np.isnan(v) for v in _corr_ci(1.0, 50))
    assert all(np.isnan(v) for v in _corr_ci(0.5, 3))


def _sets(exercise: str, n: int, *, weight_grows_with_reps: bool, zero_weight: bool = False) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    reps = rng.integers(5, 11, size=n).astype(float)
    if zero_weight:
        weight = np.zeros(n)
    elif weight_grows_with_reps:
        weight = 20 + reps * 5 + rng.normal(0, 1, n)   # assistance pattern
    else:
        weight = 250 - reps * 12 + rng.normal(0, 3, n)  # real-load pattern
    return pd.DataFrame({
        "exercise": exercise,
        "date": pd.date_range("2024-01-01", periods=n, freq="D"),
        "reps": reps,
        "weight_lb": weight,
    })


def test_load_type_evidence_flags_small_samples_as_unreliable() -> None:
    detail = pd.concat([
        _sets("bench_press", 200, weight_grows_with_reps=False),
        _sets("chop", MIN_SETS_FOR_RELIABLE_CORR - 5, weight_grows_with_reps=True),
    ], ignore_index=True)

    rows = {r["exercise"]: r for r in _load_type_evidence(detail)}

    assert rows["bench_press"]["reliable"] is True
    assert rows["bench_press"]["corr_weight_reps"] < 0
    assert rows["chop"]["reliable"] is False


def test_load_type_evidence_separates_assistance_from_real_load_by_sign() -> None:
    detail = pd.concat([
        _sets("bench_press", 200, weight_grows_with_reps=False),
        _sets("pull_up", 200, weight_grows_with_reps=True),
    ], ignore_index=True)

    rows = {r["exercise"]: r for r in _load_type_evidence(detail)}

    assert rows["bench_press"]["corr_weight_reps"] < 0
    assert rows["pull_up"]["corr_weight_reps"] > 0
    assert rows["pull_up"]["corr_lo"] > 0  # interval clears zero


def test_load_type_evidence_counts_zero_weight_sets() -> None:
    detail = _sets("plank", 60, weight_grows_with_reps=False, zero_weight=True)

    rows = {r["exercise"]: r for r in _load_type_evidence(detail)}

    assert rows["plank"]["n_zero_weight"] == 60
    assert rows["plank"]["n_loaded_sets"] == 0
    assert rows["plank"]["corr_weight_reps"] is None


def test_warmup_bias_evidence_shows_ols_steeper_than_the_envelope() -> None:
    """Warmup sets sit far below capacity, so a least-squares fit through the
    middle of the cloud reports a steeper rep-decay than the upper-envelope
    fit does. Reproduces the real finding (OLS 0.61 vs envelope 0.32)."""
    rng = np.random.default_rng(1)
    rows = []
    for _ in range(400):
        reps = float(rng.integers(5, 11))
        capacity = np.exp(np.log(200.0) - 0.13 * np.log(reps))
        # Two thirds of sets are deliberately submaximal.
        fraction = rng.choice([0.45, 0.6, 0.95, 1.0], p=[0.3, 0.3, 0.2, 0.2])
        rows.append({"exercise": "bench_press", "date": pd.Timestamp("2024-01-01"),
                     "reps": reps, "weight_lb": capacity * fraction})
    bias = _warmup_bias_evidence(pd.DataFrame(rows))

    assert bias is not None
    assert bias["envelope"] is not None
    assert bias["ols"]["beta"] > bias["envelope"]["beta"]


def test_warmup_bias_evidence_returns_none_without_enough_sets() -> None:
    assert _warmup_bias_evidence(_sets("bench_press", 10, weight_grows_with_reps=False)) is None


def test_build_model_report_is_json_safe_on_an_empty_store(tmp_path) -> None:
    """The page must render on a store with nothing in it rather than 500."""
    import json

    store = CuratedDataStore(file_manager=FileManager(environment="local", local_dir=str(tmp_path)))

    report = build_model_report(store)

    assert set(report) >= {"load_types", "warmup_bias", "review_queue", "coverage", "strength_curves"}
    json.dumps(report)  # must not raise on numpy scalars / NaN-typed columns
