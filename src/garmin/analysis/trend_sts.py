"""Bayesian structural time series (local linear trend) trend fitting.

An alternative to garmin.analysis.trend_gp's Gaussian process approach, for
comparison. Instead of a nonparametric smooth function with a hand-set
length-scale, this explicitly models named, separate state components --
level (today's true value) and trend (its current rate of change) -- updated
recursively via a Kalman filter:

    level_t = level_{t-1} + trend_{t-1} + eta_t      eta_t   ~ N(0, sigma2_level)
    trend_t = trend_{t-1} + zeta_t                    zeta_t  ~ N(0, sigma2_trend)
    y_t     = level_t + epsilon_t                      epsilon_t ~ N(0, sigma2_irregular)

All three variances are estimated by maximum likelihood (statsmodels'
UnobservedComponents implementation) rather than a hand-picked length-scale
or noise fraction -- this is the "empirical Bayes" version; putting priors on
the three variances and sampling instead (full Bayesian) is a natural next
step, not implemented here.

sigma2_irregular is exactly "day-to-day variability" as a named, directly
estimated model parameter, not something derived from residuals after the
fact the way it has to be for the GP.
"""

from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
from statsmodels.tsa.statespace.structural import UnobservedComponents

Z_68 = 1.0
Z_95 = 1.959963984540054

MIN_VALID_POINTS = 5


def fit_structural_trend(
    dates: pd.Series,
    values: pd.Series,
    weights: pd.Series | None = None,
    *,
    min_value: float | None = 0.0,
    max_value: float | None = None,
) -> tuple[pd.DataFrame, float]:
    """Fit a local-linear-trend structural model and return its smoothed level as the trend.

    Hard-tier points (weight == 0, or missing values) are treated as missing
    observations -- the Kalman filter propagates state through them without
    updating on them, same effect as dropping them for the GP. Soft-tier
    downweighting isn't natively supported by this model the way it is for
    the GP's per-point alpha; soft points are included at full weight (a
    real difference between the two models worth knowing about when
    comparing them).

    Returns (trend_df, day_to_day_std):
    - trend_df: date, mean (the smoothed level), lower_68/upper_68/
      lower_95/upper_95 -- a predictive interval from the smoothed level's
      own variance (epistemic) plus the estimated observation-noise
      variance (sigma2_irregular), same convention as trend_gp's bands.
    - day_to_day_std: sqrt(sigma2_irregular) -- the model's own directly
      estimated observation-noise parameter.
    """
    df = pd.DataFrame({"date": pd.to_datetime(dates), "value": values})
    df["weight"] = 1.0 if weights is None else pd.Series(weights).to_numpy()
    df = df.dropna(subset=["date"])
    df.loc[df["weight"] <= 0, "value"] = np.nan
    df = df.drop_duplicates(subset=["date"]).sort_values("date")

    empty_cols = ["date", "mean", "lower_68", "upper_68", "lower_95", "upper_95"]
    if df["value"].notna().sum() < MIN_VALID_POINTS:
        return pd.DataFrame(columns=empty_cols), float("nan")

    full_index = pd.date_range(df["date"].min(), df["date"].max(), freq="D")
    series = df.set_index("date")["value"].reindex(full_index)

    model = UnobservedComponents(series, level="local linear trend")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = model.fit(disp=False)

    params = dict(zip(model.param_names, result.params))
    irregular_variance = float(params.get("sigma2.irregular", 0.0))
    day_to_day_std = float(np.sqrt(max(irregular_variance, 0.0)))

    level_mean = result.states.smoothed["level"]
    level_variance = result.states.smoothed_cov.xs("level", level=0)["level"]

    predictive_std = np.sqrt(np.maximum(level_variance.to_numpy(), 0.0) + irregular_variance)
    mean = level_mean.to_numpy()

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

    trend_df = pd.DataFrame({"date": full_index, **bands})
    return trend_df, day_to_day_std
