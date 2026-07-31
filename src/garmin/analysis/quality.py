"""Per-point data-quality classification for noisy Garmin metrics.

Classifies each point in a metric series into one of three tiers:

- ``hard``: far enough from the metric's own overall distribution that it's
  very unlikely to be a real reading (e.g. a cadence reading near 0 while
  "running"). Excluded from trend fitting entirely and hidden from the
  default plotted range, but flagged so the UI can render an edge indicator
  instead of silently dropping the point.
- ``soft``: plausible but in a gray zone relative to the overall
  distribution, or locally discontinuous versus the point's own
  neighborhood. Kept in the data and the plot, but assigned a reduced
  confidence weight so it contributes less to any weighted trend fit
  (moving average or GP).
- ``normal``: full weight.

Thresholds are derived entirely from each metric's own data (median +
median absolute deviation, and a local rolling-median comparison) rather
than hardcoded per-metric bounds -- the same generic multipliers apply to
every metric. This makes the classifier self-calibrating: it adapts to how
tightly or loosely a given metric is naturally distributed instead of
requiring someone to hand-pick "cadence < 80 = bad" for each new metric.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

HARD_WEIGHT = 0.0
SOFT_WEIGHT = 0.35
NORMAL_WEIGHT = 1.0

# Scales median absolute deviation (MAD) to be a consistent estimator of
# standard deviation under normality, so deviation thresholds below read in
# familiar "how many std devs away" units regardless of the metric's units.
MAD_TO_STD = 1.4826

# Global (whole-series) robust z-score thresholds, in MAD-scaled units.
#
# These are the one tunable "how extreme is too extreme" constant this
# module needs -- there's no way to have zero such constant and still detect
# anomalies at all. What makes this data-driven rather than hardcoded is
# that the *reference* (median, MAD) is recomputed fresh from each metric's
# own data, so the same multiplier adapts automatically to how tightly or
# loosely a given metric is naturally distributed, instead of a per-metric
# absolute bound like "cadence < 80".
#
# 25.0 was chosen by checking real running data: a handful of genuine sensor
# errors (cadence readings of 0, 42, 64 spm) are too few (3 points) to form
# a separable density cluster -- tested with a Gaussian mixture model, which
# also failed to isolate them -- so a hard cutoff has to be picked directly.
# 25 cleanly separates those from the next-lowest real value (94 spm,
# z~20.7) without misclassifying it.
GLOBAL_HARD_THRESHOLD = 25.0
GLOBAL_SOFT_THRESHOLD = 4.0

# Local (rolling-neighborhood) robust z-score threshold -- catches points
# that are discontinuous from their own recent neighborhood even if they're
# not extreme relative to the metric's overall distribution.
LOCAL_MAD_THRESHOLD = 6.0
LOCAL_WINDOW = 15
LOCAL_MIN_POINTS = 5


def _robust_z(values: pd.Series) -> pd.Series:
    """Per-point deviation from the series' own median, in robust MAD units."""
    median = values.median()
    mad = (values - median).abs().median() * MAD_TO_STD
    if not mad or np.isnan(mad):
        return pd.Series(0.0, index=values.index)
    return (values - median).abs() / mad


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
    global_hard_threshold: float = GLOBAL_HARD_THRESHOLD,
    global_soft_threshold: float = GLOBAL_SOFT_THRESHOLD,
    local_window: int = LOCAL_WINDOW,
    local_min_points: int = LOCAL_MIN_POINTS,
    local_mad_threshold: float = LOCAL_MAD_THRESHOLD,
) -> pd.DataFrame:
    """Classify each row of ``df[value_col]`` into a data-quality tier.

    A point is ``hard`` if it's more than ``global_hard_threshold`` robust
    MAD-units from the series' own median. A point is ``soft`` if it's more
    than ``global_soft_threshold`` MAD-units away (global check), or if it's
    locally discontinuous -- more than ``local_mad_threshold`` MAD-units from
    a rolling local median, even when it's within the globally-plausible
    range (a real extreme should still roughly track its own neighborhood).

    All thresholds are generic multipliers applied uniformly; the actual
    reference values (median, MAD) are computed fresh from ``df[value_col]``
    every call, so nothing here is specific to any one metric.

    Returns a copy of ``df`` sorted by ``date_col`` with two new columns:
    ``quality_tier`` and ``quality_weight``.
    """
    out = df.sort_values(date_col).reset_index(drop=True).copy()
    values = out[value_col]

    global_z = _robust_z(values)
    tier = pd.Series("normal", index=out.index, dtype="object")
    tier[global_z > global_soft_threshold] = "soft"

    if local_window > 0:
        local_z = _local_deviation(values, local_window, local_min_points)
        locally_deviant = (local_z > local_mad_threshold).fillna(False)
        tier[locally_deviant & (tier == "normal")] = "soft"

    tier[global_z > global_hard_threshold] = "hard"
    tier[values.isna()] = "hard"

    weight_map = {"normal": NORMAL_WEIGHT, "soft": SOFT_WEIGHT, "hard": HARD_WEIGHT}
    out["quality_tier"] = tier
    out["quality_weight"] = tier.map(weight_map)
    return out


def classify_metric(df: pd.DataFrame, value_col: str, date_col: str = "date") -> pd.DataFrame:
    """classify_outliers() with default (data-driven) thresholds for value_col."""
    return classify_outliers(df, value_col, date_col=date_col)
