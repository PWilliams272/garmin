from __future__ import annotations

import pandas as pd

from garmin.analysis.trend_gp import fit_gp_trend


def test_fit_gp_trend_handles_constant_series_with_same_day_points() -> None:
    # Reproduces the real crash hit analyzing bouldering.distance_mi: an
    # activity type whose metric is always ~0 (indoor climbing has no GPS
    # distance), with more than one session on the same calendar date --
    # base_noise_variance and value_variance both collapse to exactly 0,
    # which previously made the kernel matrix exactly singular and raised
    # numpy.linalg.LinAlgError instead of returning a (flat) trend.
    dates = pd.to_datetime([
        "2026-01-01", "2026-01-01", "2026-01-03", "2026-01-05", "2026-01-05", "2026-01-08",
    ])
    values = pd.Series([0.0, 0.0, 0.0, 0.0, 0.0, 0.0])

    trend = fit_gp_trend(dates, values)

    assert not trend.empty
    assert (trend["mean"].abs() < 1e-6).all()


def test_fit_gp_trend_still_fits_a_real_trend() -> None:
    dates = pd.date_range("2026-01-01", periods=30, freq="D")
    values = pd.Series(range(30), dtype=float)

    trend = fit_gp_trend(dates, values, length_scale_days=7.0)

    assert not trend.empty
    assert trend["mean"].iloc[-1] > trend["mean"].iloc[0]
