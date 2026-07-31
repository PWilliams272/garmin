"""Gaussian process trend fitting with per-point uncertainty from quality weights.

Rather than treating outlier handling as a separate, disconnected step from
trend fitting, this takes each point's quality weight (see
garmin.analysis.quality) as its per-point noise level: full-confidence points
constrain the fit tightly, low-confidence ("soft") points are allowed to
deviate from the fitted mean without much penalty, and zero-confidence
("hard") points should already have been dropped before calling this.
"""

from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import RBF, ConstantKernel, WhiteKernel

# Noise floor applied even to fully-trusted (weight == 1) points, so the GP
# doesn't try to interpolate exactly through every observation.
BASE_NOISE_VARIANCE = 1e-2


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

    Weights (0..1) are converted to per-point noise variance: a weight of 1
    gets only the base noise floor; lower weights get proportionally more
    noise, so they pull less on the fitted mean and widen the local CI rather
    than being a hard include/exclude decision. Points with weight == 0, or
    missing values, are dropped before fitting.

    A plain GP assumes Gaussian residuals, so its CI band can extend past a
    metric's physical bounds (e.g. negative distance). min_value/max_value
    clip the returned mean/CI to a valid range; min_value defaults to 0 since
    every metric this is used for so far is a non-negative physical quantity.

    Returns a DataFrame with columns date, mean, lower_95, upper_95, sampled
    at n_predict_points evenly spaced points across the observed date range
    (or at predict_dates if given).
    """
    df = pd.DataFrame({"date": pd.to_datetime(dates), "value": values})
    df["weight"] = 1.0 if weights is None else pd.Series(weights).to_numpy()
    df = df.dropna(subset=["value"])
    df = df[df["weight"] > 0]

    if len(df) < 2:
        return pd.DataFrame(columns=["date", "mean", "lower_95", "upper_95"])

    df = df.sort_values("date").reset_index(drop=True)
    origin = df["date"].min()
    x = (df["date"] - origin).dt.total_seconds().to_numpy() / 86400.0
    y = df["value"].to_numpy()

    weight_arr = df["weight"].to_numpy()
    value_variance = float(np.var(y)) if len(y) > 1 else 1.0
    alpha = BASE_NOISE_VARIANCE + (1.0 - weight_arr) * value_variance * 2.0

    kernel = (
        ConstantKernel(1.0, (1e-2, 1e3))
        * RBF(length_scale=length_scale_days, length_scale_bounds=(3.0, 365.0))
        + WhiteKernel(noise_level=BASE_NOISE_VARIANCE, noise_level_bounds=(1e-5, 10.0))
    )
    gp = GaussianProcessRegressor(kernel=kernel, alpha=alpha, normalize_y=True, n_restarts_optimizer=2)
    with warnings.catch_warnings():
        # Multi-restart L-BFGS hyperparameter search transiently evaluates
        # numerically unstable kernel parameter combinations before settling
        # on the best one; those intermediate warnings are expected noise.
        warnings.simplefilter("ignore", RuntimeWarning)
        gp.fit(x.reshape(-1, 1), y)

    if predict_dates is None:
        predict_dates = pd.date_range(df["date"].min(), df["date"].max(), periods=n_predict_points)
    else:
        predict_dates = pd.to_datetime(predict_dates)

    x_pred = (predict_dates - origin).total_seconds().to_numpy() / 86400.0
    mean, std = gp.predict(x_pred.reshape(-1, 1), return_std=True)
    lower_95 = mean - 1.96 * std
    upper_95 = mean + 1.96 * std

    if min_value is not None:
        mean = np.maximum(mean, min_value)
        lower_95 = np.maximum(lower_95, min_value)
        upper_95 = np.maximum(upper_95, min_value)
    if max_value is not None:
        mean = np.minimum(mean, max_value)
        lower_95 = np.minimum(lower_95, max_value)
        upper_95 = np.minimum(upper_95, max_value)

    return pd.DataFrame({
        "date": predict_dates,
        "mean": mean,
        "lower_95": lower_95,
        "upper_95": upper_95,
    })
