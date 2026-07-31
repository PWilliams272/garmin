"""Per-point data-quality classification for noisy Garmin metrics.

Classifies each point in a metric series into one of three tiers:

- ``hard``: physiologically/mechanically implausible (e.g. a cadence reading
  near 0 while "running"). Excluded from trend fitting entirely and hidden
  from the default plotted range, but flagged so the UI can render an edge
  indicator instead of silently dropping the point.
- ``soft``: plausible but in a gray zone, or locally discontinuous versus
  the point's own neighborhood. Kept in the data and the plot, but assigned
  a reduced confidence weight so it contributes less to any weighted trend
  fit (moving average or GP).
- ``normal``: full weight.

The intent is that "weight" becomes the per-point uncertainty input to GP
trend fitting later, rather than outlier handling being a separate,
disconnected concern from the trend itself.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

HARD_WEIGHT = 0.0
SOFT_WEIGHT = 0.35
NORMAL_WEIGHT = 1.0

# Scales median absolute deviation (MAD) to be a consistent estimator of
# standard deviation under normality, so the local-deviation threshold below
# reads in familiar "how many std devs away" units.
MAD_TO_STD = 1.4826


def _local_deviation(values: pd.Series, window: int, min_points: int) -> pd.Series:
    """Deviation of each point from a local rolling median, in robust MAD units."""
    rolling_median = values.rolling(window=window, center=True, min_periods=min_points).median()
    abs_dev = (values - rolling_median).abs()
    rolling_mad = abs_dev.rolling(window=window, center=True, min_periods=min_points).median()
    robust_std = (rolling_mad * MAD_TO_STD).replace(0, np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        return abs_dev / robust_std


def classify_outliers(
    df: pd.DataFrame,
    value_col: str,
    date_col: str = "date",
    *,
    hard_bounds: tuple[float | None, float | None] = (None, None),
    soft_bounds: tuple[float | None, float | None] = (None, None),
    local_window: int = 15,
    local_min_points: int = 5,
    local_mad_threshold: float = 6.0,
) -> pd.DataFrame:
    """Classify each row of ``df[value_col]`` into a data-quality tier.

    ``hard_bounds`` / ``soft_bounds`` are absolute domain bounds ``(low, high)``;
    either side may be ``None`` to leave that side unbounded. A point outside
    ``hard_bounds`` is always ``hard``. A point outside ``soft_bounds`` (but
    inside ``hard_bounds``) is ``soft``.

    Points that pass both absolute checks are additionally compared to a
    local rolling median (in robust MAD units) — a point far from its own
    recent neighborhood is flagged ``soft`` even if it's within the
    domain-plausible range, since genuinely real extremes should still
    roughly track the local trend rather than jump discontinuously.

    Returns a copy of ``df`` sorted by ``date_col`` with two new columns:
    ``quality_tier`` and ``quality_weight``.
    """
    out = df.sort_values(date_col).reset_index(drop=True).copy()
    values = out[value_col]

    hard_low, hard_high = hard_bounds
    soft_low, soft_high = soft_bounds

    tier = pd.Series("normal", index=out.index, dtype="object")

    if soft_low is not None:
        tier[values < soft_low] = "soft"
    if soft_high is not None:
        tier[values > soft_high] = "soft"

    if local_window > 0:
        deviation = _local_deviation(values, local_window, local_min_points)
        locally_deviant = deviation > local_mad_threshold
        tier[locally_deviant.fillna(False) & (tier == "normal")] = "soft"

    if hard_low is not None:
        tier[values < hard_low] = "hard"
    if hard_high is not None:
        tier[values > hard_high] = "hard"

    tier[values.isna()] = "hard"

    weight_map = {"normal": NORMAL_WEIGHT, "soft": SOFT_WEIGHT, "hard": HARD_WEIGHT}
    out["quality_tier"] = tier
    out["quality_weight"] = tier.map(weight_map)
    return out


# Starting defaults for metrics with a known-noisy quality problem. These are
# a first pass, not tuned analysis — expect to adjust the thresholds once
# real distributions are reviewed per metric.
METRIC_QUALITY_BOUNDS: dict[str, dict] = {
    "cadence_spm": dict(hard_bounds=(80, None), soft_bounds=(130, None)),
    "pace_min_per_mile": dict(hard_bounds=(3.5, 30.0), soft_bounds=(4.5, 20.0)),
}


def classify_metric(df: pd.DataFrame, value_col: str, date_col: str = "date") -> pd.DataFrame:
    """classify_outliers() using the METRIC_QUALITY_BOUNDS default for value_col, if any."""
    bounds = METRIC_QUALITY_BOUNDS.get(value_col, {})
    return classify_outliers(df, value_col, date_col=date_col, **bounds)
