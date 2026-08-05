"""Tests for the empirical variant conversion fit."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from garmin.analysis.variant_conversion import (
    _agrees,
    _level_basis,
    _paired_ratio,
    fit_variant_conversions,
    session_bests,
)


def _synthetic_family(
    *,
    true_factor: float = 2.5,
    n_days: int = 900,
    drift: float = 0.3,
    noise: float = 0.02,
    seed: int = 0,
    variant_b_start: int = 0,
    variant_b_end: int | None = None,
) -> pd.DataFrame:
    """Two variants measuring one strength trajectory through instruments that
    differ by a known constant factor."""
    rng = np.random.default_rng(seed)
    start = pd.Timestamp("2022-01-01")
    end = n_days if variant_b_end is None else variant_b_end

    rows = []
    for day in range(0, n_days, 5):
        # Reference: strength rises smoothly over the whole window.
        level = 100.0 * np.exp(drift * day / n_days)
        rows.append({
            "family": "fam", "variant": "A", "date": start + pd.Timedelta(days=day),
            "est_1rm": level * np.exp(rng.normal(0, noise)),
        })
        if variant_b_start <= day <= end:
            rows.append({
                "family": "fam", "variant": "B", "date": start + pd.Timedelta(days=day),
                "est_1rm": level / true_factor * np.exp(rng.normal(0, noise)),
            })
    return pd.DataFrame(rows)


def test_recovers_known_factor():
    """The whole point: a variant reading 2.5x light should come back as 2.5x."""
    result = fit_variant_conversions(_synthetic_family(true_factor=2.5))
    factor = result.loc[result["variant"] == "B", "factor"].iloc[0]
    assert factor == pytest.approx(2.5, rel=0.05)
    assert bool(result.loc[result["variant"] == "B", "identified"].iloc[0])


def test_reference_gets_unit_factor():
    result = fit_variant_conversions(_synthetic_family())
    reference = result.loc[result["variant"] == result["reference"]]
    assert len(reference) == 1
    assert reference["factor"].iloc[0] == 1.0


def test_most_trained_variant_is_the_reference():
    """The anchor should be the best-sampled variant, not an arbitrary one."""
    frame = _synthetic_family(variant_b_start=600)
    assert (frame["variant"] == "A").sum() > (frame["variant"] == "B").sum()
    result = fit_variant_conversions(frame)
    assert set(result["reference"]) == {"A"}


def test_underlying_trend_is_not_absorbed_into_the_offset():
    """A strong trend must not bias the factor -- that separation is the
    reason for fitting a level at all rather than taking a raw ratio."""
    flat = fit_variant_conversions(_synthetic_family(drift=0.0, seed=1))
    steep = fit_variant_conversions(_synthetic_family(drift=0.8, seed=1))
    flat_factor = flat.loc[flat["variant"] == "B", "factor"].iloc[0]
    steep_factor = steep.loc[steep["variant"] == "B", "factor"].iloc[0]
    assert flat_factor == pytest.approx(steep_factor, rel=0.05)


def test_disjoint_variants_are_not_identified():
    """Variants trained in non-overlapping periods carry no evidence linking
    them, and must be reported as such rather than given a number."""
    start = pd.Timestamp("2022-01-01")
    rows = []
    for day in range(0, 300, 5):  # A trains early and then stops.
        rows.append({"family": "fam", "variant": "A",
                     "date": start + pd.Timedelta(days=day), "est_1rm": 100.0})
    for day in range(900, 1200, 5):  # B starts long after A ended.
        rows.append({"family": "fam", "variant": "B",
                     "date": start + pd.Timedelta(days=day), "est_1rm": 40.0})
    result = fit_variant_conversions(pd.DataFrame(rows))
    row = result.loc[result["variant"] == "B"].iloc[0]
    assert row["overlap_days"] == 0
    assert row["n_pairs"] == 0
    assert not row["identified"]


def test_sparse_variants_are_excluded():
    frame = _synthetic_family()
    frame = pd.concat([frame, pd.DataFrame([{
        "family": "fam", "variant": "C",
        "date": pd.Timestamp("2022-06-01"), "est_1rm": 50.0,
    }])], ignore_index=True)
    result = fit_variant_conversions(frame)
    assert "C" not in set(result["variant"])


def test_single_variant_family_is_trivially_identified():
    frame = _synthetic_family()
    frame = frame[frame["variant"] == "A"]
    result = fit_variant_conversions(frame)
    assert len(result) == 1
    assert result["factor"].iloc[0] == 1.0
    assert bool(result["identified"].iloc[0])


def test_paired_estimate_agrees_with_the_fit():
    """The cross-check must corroborate the fit on clean synthetic data --
    otherwise it could never be trusted to flag a bad one."""
    frame = _synthetic_family(true_factor=3.0)
    result = fit_variant_conversions(frame)
    row = result.loc[result["variant"] == "B"].iloc[0]
    assert row["paired_ratio"] == pytest.approx(3.0, rel=0.08)
    assert row["n_pairs"] > 0
    assert bool(row["agrees"])


def test_heterogeneous_bucket_is_flagged():
    """A 'variant' that is really a mixture of two different implements has no
    single valid factor; the two estimators should diverge and flag it."""
    rng = np.random.default_rng(7)
    start = pd.Timestamp("2022-01-01")
    rows = []
    for day in range(0, 900, 5):
        rows.append({"family": "fam", "variant": "A",
                     "date": start + pd.Timedelta(days=day), "est_1rm": 100.0})
        # Alternates between a heavy and a light implement under one label.
        scale = 1.0 if (day // 5) % 2 == 0 else 4.0
        rows.append({"family": "fam", "variant": "MIX",
                     "date": start + pd.Timedelta(days=day),
                     "est_1rm": 100.0 / scale * np.exp(rng.normal(0, 0.02))})
    result = fit_variant_conversions(pd.DataFrame(rows))
    row = result.loc[result["variant"] == "MIX"].iloc[0]
    # Both central estimates land near the midpoint of the mixture, so they
    # agree with each other while both being wrong -- this is precisely the
    # failure the spread check exists to catch, and why agreement alone is
    # not enough to trust a factor.
    assert row["agrees"]
    assert not row["homogeneous"]
    assert not row["identified"]


def test_agreement_tolerance_bounds():
    assert _agrees(1.0, 1.0)
    assert _agrees(1.2, 1.0)
    assert not _agrees(2.0, 1.0)
    assert not _agrees(1.0, float("nan"))
    assert not _agrees(1.0, 0.0)


def test_paired_ratio_needs_nearby_sessions():
    a = pd.DataFrame([{"date": pd.Timestamp("2022-01-01"), "est_1rm": 100.0}])
    far = pd.DataFrame([{"date": pd.Timestamp("2024-01-01"), "est_1rm": 50.0}])
    ratio, n = _paired_ratio(a, far)
    assert n == 0
    assert not np.isfinite(ratio)


def test_level_basis_is_a_partition_of_unity():
    """Hat functions must sum to 1 at every point, or the level would apply an
    unintended time-varying gain that the offsets would then absorb."""
    days = np.linspace(0, 400, 101)
    basis = _level_basis(days)
    assert np.allclose(basis.sum(axis=1), 1.0)
    assert (basis >= 0).all()


def test_session_bests_takes_the_best_set_per_day():
    detail = pd.DataFrame([
        {"family": "fam", "variant": "A", "date": pd.Timestamp("2022-01-01"),
         "reps": 8, "weight_lb": 100.0},
        {"family": "fam", "variant": "A", "date": pd.Timestamp("2022-01-01"),
         "reps": 8, "weight_lb": 135.0},
    ])
    result = session_bests(detail)
    assert len(result) == 1
    # The heavier set at equal reps must win.
    assert result["est_1rm"].iloc[0] > 135.0


def test_every_row_has_the_same_columns():
    """A branch that omits a column gets NaN filled in for it, and NaN is not
    valid JSON -- which breaks the whole payload for any page reading it, not
    just the part that uses this table."""
    multi = _synthetic_family()
    single = multi[multi["variant"] == "A"].assign(family="other")
    result = fit_variant_conversions(pd.concat([multi, single], ignore_index=True))
    assert result["family"].nunique() == 2
    assert not result.isna().any().any()


def test_result_is_strictly_json_serializable():
    multi = _synthetic_family()
    single = multi[multi["variant"] == "A"].assign(family="other")
    result = fit_variant_conversions(pd.concat([multi, single], ignore_index=True))
    # allow_nan=False is the check that matters: json.dumps happily emits bare
    # NaN by default, which every browser's JSON.parse then rejects.
    json.dumps(result.to_dict(orient="records"), allow_nan=False, default=str)


def test_session_bests_drops_bodyweight_sets():
    detail = pd.DataFrame([
        {"family": "fam", "variant": "A", "date": pd.Timestamp("2022-01-01"),
         "reps": 10, "weight_lb": 0.0},
    ])
    assert session_bests(detail).empty
