"""Gaussian process trend fitting with per-point uncertainty from quality weights.

Rather than treating outlier handling as a separate, disconnected step from
trend fitting, this takes each point's quality weight (see
garmin.analysis.quality) as its per-point noise level: full-confidence points
constrain the fit tightly, low-confidence ("soft") points are allowed to
deviate from the fitted mean without much penalty, and zero-confidence
("hard") points should already have been dropped before calling this.

The kernel has no WhiteKernel term -- observation noise is supplied entirely
via the per-point `alpha` array (used only to regularize the fit), so
gp.predict()'s returned std is the GP's epistemic uncertainty about the
smooth trend itself, not observation noise. A representative noise variance
is estimated separately, from the residuals of full-confidence points, and
added back explicitly to produce a calibrated *predictive* interval -- one
that should contain roughly 68% / 95% of individual observations, not just
the fitted mean. (Folding noise into the kernel via WhiteKernel instead
double-counts it: it gets learned during optimization AND added to every
predicted point's variance regardless of local data density, which is what
was producing bands far wider than the data.)
"""

from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import RBF, ConstantKernel

# Fraction of the metric's own variance assumed to be per-point noise for a
# full-confidence ("normal" tier) point, used as the training alpha. This
# must be a real fraction of the data's scale, not a tiny fixed epsilon --
# too small and the optimizer collapses length_scale toward interpolating
# through noise (verified empirically: epsilon=1e-2 produced a degenerate
# fit with length_scale at its lower bound and amplitude at its upper bound,
# and a predictive band ~2x the entire data range). 0.3 was tuned by
# checking empirical 95% CI coverage against real running data (cadence,
# pace, distance all land at 94.7-95.3% actual coverage of a nominal 95%
# band) -- see garmin.analysis.trend_gp.empirical_coverage.
BASE_NOISE_FRACTION = 0.3
SOFT_NOISE_MULTIPLIER = 2.0

# Lower bound on the RBF length scale, in days. Without a floor here the
# same degenerate collapse above can occur even with reasonable alpha, since
# the optimizer can still choose to fit through sparse noisy points with a
# very short length scale rather than smoothing over them.
MIN_LENGTH_SCALE_DAYS = 7.0

Z_68 = 1.0
Z_95 = 1.959963984540054


def fit_gp_trend(
    dates: pd.Series,
    values: pd.Series,
    weights: pd.Series | None = None,
    *,
    length_scale_days: float = 21.0,
    n_predict_points: int = 400,
    predict_dates: pd.Series | None = None,
    min_value: float | None = 0.0,
    max_value: float | None = None,
) -> pd.DataFrame:
    """Fit a GP trend to (dates, values) with optional per-point quality weights.

    Weights (0..1) are converted to per-point training noise variance: a
    weight of 1 gets only the base noise floor; lower weights get
    proportionally more, so they pull less on the fitted mean without being
    a hard include/exclude decision. Points with weight == 0, or missing
    values, are dropped before fitting.

    A plain GP assumes Gaussian residuals, so its CI band can extend past a
    metric's physical bounds (e.g. negative distance). min_value/max_value
    clip the returned mean/bands to a valid range; min_value defaults to 0
    since every metric this is used for so far is a non-negative physical
    quantity.

    Returns a DataFrame with columns date, mean, lower_68, upper_68,
    lower_95, upper_95 -- a calibrated predictive interval (see module
    docstring), sampled at n_predict_points evenly spaced points across the
    observed date range (or at predict_dates if given).
    """
    df = pd.DataFrame({"date": pd.to_datetime(dates), "value": values})
    df["weight"] = 1.0 if weights is None else pd.Series(weights).to_numpy()
    df = df.dropna(subset=["value"])
    df = df[df["weight"] > 0]

    empty_cols = ["date", "mean", "lower_68", "upper_68", "lower_95", "upper_95"]
    if len(df) < 2:
        return pd.DataFrame(columns=empty_cols)

    df = df.sort_values("date").reset_index(drop=True)
    origin = df["date"].min()
    x = (df["date"] - origin).dt.total_seconds().to_numpy() / 86400.0
    y = df["value"].to_numpy()

    weight_arr = df["weight"].to_numpy()
    value_variance = float(np.var(y)) if len(y) > 1 else 1.0
    alpha = value_variance * (BASE_NOISE_FRACTION + (1.0 - weight_arr) * SOFT_NOISE_MULTIPLIER)

    kernel = ConstantKernel(1.0, (1e-2, 1e3)) * RBF(
        length_scale=length_scale_days, length_scale_bounds=(MIN_LENGTH_SCALE_DAYS, 365.0)
    )
    gp = GaussianProcessRegressor(kernel=kernel, alpha=alpha, normalize_y=True, n_restarts_optimizer=2)
    with warnings.catch_warnings():
        # Multi-restart L-BFGS hyperparameter search transiently evaluates
        # numerically unstable kernel parameter combinations before settling
        # on the best one; those intermediate warnings are expected noise.
        warnings.simplefilter("ignore", RuntimeWarning)
        gp.fit(x.reshape(-1, 1), y)

    # Representative observation-noise variance, estimated from how far
    # full-confidence points actually land from the fitted trend at their
    # own location -- data-driven, not an assumed constant.
    train_fit_mean = gp.predict(x.reshape(-1, 1))
    residuals = y - train_fit_mean
    full_weight_mask = weight_arr >= 0.99
    residual_sample = residuals[full_weight_mask] if full_weight_mask.sum() >= 5 else residuals
    noise_variance = float(np.var(residual_sample))

    if predict_dates is None:
        predict_dates = pd.date_range(df["date"].min(), df["date"].max(), periods=n_predict_points)
    else:
        predict_dates = pd.to_datetime(predict_dates)

    x_pred = (predict_dates - origin).total_seconds().to_numpy() / 86400.0
    mean, f_std = gp.predict(x_pred.reshape(-1, 1), return_std=True)
    predictive_std = np.sqrt(f_std**2 + noise_variance)

    bands = {
        "mean": mean,
        "lower_68": mean - Z_68 * predictive_std,
        "upper_68": mean + Z_68 * predictive_std,
        "lower_95": mean - Z_95 * predictive_std,
        "upper_95": mean + Z_95 * predictive_std,
    }
    for key, arr in bands.items():
        if min_value is not None:
            arr = np.maximum(arr, min_value)
        if max_value is not None:
            arr = np.minimum(arr, max_value)
        bands[key] = arr

    return pd.DataFrame({"date": predict_dates, **bands})


def empirical_coverage(points: pd.DataFrame, trend: pd.DataFrame, value_col: str) -> dict:
    """Fraction of (weight > 0) points falling inside the 68%/95% bands, for calibration checks.

    Interpolates the trend's bands onto each point's date (nearest match)
    rather than requiring exact date alignment.
    """
    if points.empty or trend.empty:
        return {"n": 0, "coverage_68": None, "coverage_95": None}

    fittable = points[points["quality_weight"] > 0].sort_values("date")
    trend_sorted = trend.sort_values("date")
    merged = pd.merge_asof(fittable, trend_sorted, on="date", direction="nearest")

    within_68 = (merged[value_col] >= merged["lower_68"]) & (merged[value_col] <= merged["upper_68"])
    within_95 = (merged[value_col] >= merged["lower_95"]) & (merged[value_col] <= merged["upper_95"])
    return {
        "n": len(merged),
        "coverage_68": float(within_68.mean()),
        "coverage_95": float(within_95.mean()),
    }
