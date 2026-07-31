"""Bayesian structural time series (local linear trend) trend fitting.

An alternative to garmin.analysis.trend_gp's Gaussian process approach, for
comparison. Instead of a nonparametric smooth function with a hand-set
length-scale, this explicitly models named, separate state components --
level (today's true value) and trend (its current rate of change) -- updated
recursively via a Kalman filter:

    level_t = level_{t-1} + trend_{t-1} (+ eta_t)     eta_t   ~ N(0, sigma2_level)
    trend_t = trend_{t-1} + zeta_t                     zeta_t  ~ N(0, sigma2_trend)
    y_t     = level_t + epsilon_t                       epsilon_t ~ N(0, sigma2_irregular)

level='local linear trend' includes the eta_t term (level itself picks up
independent innovations each step, which can produce sharp corners -- the
level jumping on its own, not just accumulating trend). level='smooth
trend' drops eta_t entirely: the level only ever changes by accumulating
trend/velocity, giving a continuous, physically-motivated path (matches the
argument that e.g. weight is a slowly-responding physical quantity, not
something that re-randomizes every day beyond measurement noise).

Variances are estimated by maximum likelihood (statsmodels' UnobservedComponents)
rather than a hand-picked length-scale or noise fraction -- "empirical Bayes";
putting priors on the variances and sampling instead (full Bayesian) is a
natural next step, not implemented here.

Optional AR(1) noise (autoregressive_order=1): a single day's noisy reading
(one bad night's sleep, one late meal) is one thing; several days in a row
sharing the same hidden cause (a week of travel, consistently high sodium)
is not independent noise, and plain iid observation noise (sigma2_irregular)
has no way to represent that. An AR(1) component lets today's "extra"
deviation partly persist into tomorrow's rather than resetting.

That component is NOT freely estimable, though: tested empirically, letting
statsmodels fit the AR coefficient by MLE finds ar.L1 ~ 0.994 -- so close to
a random walk that it directly competes with (and steals from) the trend
component. A grid search over fixed AR coefficients confirmed this isn't a
local-optimum fluke: log-likelihood improves *monotonically* as the
coefficient increases, all the way to the degenerate solution, with no
data-preferred interior optimum. That's a genuine non-identifiability
between "trend" and "AR(1)" under plain likelihood, not a bug -- the fix is
a prior that favors short decay (e.g. Beta/truncated-normal concentrated
around 0.1-0.4), which isn't implemented yet (that's the real Bayesian
version). fixed_ar_coefficient below is an explicit stand-in: pick a small,
short-decay value by hand rather than let MLE run to the degenerate corner.
Revisit once priors + sampling are actually in place.
"""

from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
from statsmodels.tsa.statespace.structural import UnobservedComponents

Z_68 = 1.0
Z_95 = 1.959963984540054

MIN_VALID_POINTS = 5

# Not data-derived -- see module docstring. A short-decay stand-in for a
# proper prior on the AR(1) coefficient, used only when autoregressive_order > 0.
DEFAULT_FIXED_AR_COEFFICIENT = 0.3


def fit_structural_trend(
    dates: pd.Series,
    values: pd.Series,
    weights: pd.Series | None = None,
    *,
    level: str = "local linear trend",
    autoregressive_order: int = 0,
    fixed_ar_coefficient: float | None = DEFAULT_FIXED_AR_COEFFICIENT,
    min_value: float | None = 0.0,
    max_value: float | None = None,
) -> tuple[pd.DataFrame, float]:
    """Fit a structural trend model and return its smoothed level as the trend.

    Hard-tier points (weight == 0, or missing values) are treated as missing
    observations -- the Kalman filter propagates state through them without
    updating on them, same effect as dropping them for the GP. Soft-tier
    downweighting isn't natively supported by this model the way it is for
    the GP's per-point alpha; soft points are included at full weight (a
    real difference between the two models worth knowing about when
    comparing them).

    If autoregressive_order > 0, an AR(p) noise component is added; its
    lag-1 coefficient is fixed at fixed_ar_coefficient (see module
    docstring for why it's fixed rather than fit) unless
    fixed_ar_coefficient is None, in which case it's left free (will likely
    hit the degenerate near-unit-root solution -- mainly useful for
    reproducing/inspecting that failure mode).

    Returns (trend_df, day_to_day_std):
    - trend_df: date, mean (the smoothed level), lower_68/upper_68/
      lower_95/upper_95 -- a predictive interval from the smoothed state's
      own variance (epistemic, level + AR state if present) plus the
      estimated observation-noise variance (sigma2_irregular).
    - day_to_day_std: combines sigma2_irregular with the AR component's own
      stationary variance (sigma2_ar / (1 - ar_coefficient^2)) when present,
      since a fixed short-decay AR coefficient tends to absorb most of the
      "noise" job from sigma2_irregular -- using sigma2_irregular alone
      would understate day-to-day variability in that case.
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

    use_ar = autoregressive_order > 0
    model = UnobservedComponents(series, level=level, autoregressive=autoregressive_order if use_ar else None)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        if use_ar and fixed_ar_coefficient is not None:
            result = model.fit_constrained({"ar.L1": fixed_ar_coefficient})
        else:
            result = model.fit(disp=False)

    params = dict(zip(model.param_names, result.params))
    irregular_variance = float(params.get("sigma2.irregular", 0.0))

    # The displayed trend is the level ALONE, never level+AR: the AR term
    # exists to model transient, partially-persistent bias as part of the
    # *noise* budget, not to be folded into "the trend" itself. Doing that
    # (an earlier version of this function did) makes the AR state hug
    # individual points and the trend line stops meaning anything -- verified
    # empirically: level+AR tracked raw weekly averages almost exactly and
    # its predictive bands covered 100% of points, i.e. it had stopped
    # smoothing at all.
    level_mean = result.states.smoothed["level"]
    level_variance = result.states.smoothed_cov.xs("level", level=0)["level"].to_numpy()

    day_to_day_variance = irregular_variance
    if use_ar:
        ar_coef = float(params.get("ar.L1", 0.0))
        ar_innovation_variance = float(params.get("sigma2.ar", 0.0))
        if abs(ar_coef) < 1.0:
            day_to_day_variance += ar_innovation_variance / (1.0 - ar_coef**2)
    day_to_day_std = float(np.sqrt(max(day_to_day_variance, 0.0)))

    predictive_std = np.sqrt(np.maximum(level_variance, 0.0) + day_to_day_variance)
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
