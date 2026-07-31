"""Batch analysis pipeline: curated data -> analyzed layer.

Runs quality classification (garmin.analysis.quality) once per metric,
across both per-activity data (running) and daily health metrics (heart
rate, steps, weight, ...), and writes the results -- plus one or more fitted
trends -- to CuratedDataStore's "analyzed" layer, so the web app never has
to compute any of it on request; it only reads precomputed output.

Two trend models are available (garmin.analysis.trend_gp / trend_sts) and,
for health metrics, both are computed side by side for comparison:
- gp_multiscale: two-length-scale Gaussian process (slow trend + fast
  short-term component, see trend_gp.fit_gp_multiscale_trend).
- sts: Bayesian structural time series / local-linear-trend Kalman filter
  (see trend_sts.fit_structural_trend).
Running metrics still use the original single-length-scale GP
(fit_gp_trend) -- activities are a separate problem (mixed-effort spread,
not continuous physiological noise) being deliberately deferred; the STS
model also isn't yet safe for multiple-same-day activities (see
trend_sts's docstring on same-day deduplication).

Layers, per the repo's raw -> curated -> analyzed -> viewer split:
- curated/activities/summary/<dataset>.parquet  or  curated/daily/<dataset>.parquet  (source)
- curated/analyzed/<dataset>/<metric>_points.parquet        (per-point quality tier/weight)
- curated/analyzed/<dataset>/<metric>_trend.parquet          (running: single-scale GP)
- curated/analyzed/<dataset>/<metric>_trend_gp_multiscale.parquet  (health: multiscale GP)
- curated/analyzed/<dataset>/<metric>_trend_sts.parquet            (health: structural time series)
"""

from __future__ import annotations

import pandas as pd

from garmin.analysis.quality import classify_metric
from garmin.analysis.trend_gp import fit_gp_multiscale_trend, fit_gp_trend
from garmin.analysis.trend_sts import fit_structural_trend
from garmin.io.curated_store import CuratedDataStore

# (dataset, metric) pairs to analyze. Extend as more metrics need the same
# quality-classification + GP-trend treatment.
RUNNING_METRICS = ["cadence_spm", "pace_min_per_mile", "distance_mi"]

HEALTH_METRICS = {
    "heart_rate": ["resting_hr"],
    "steps": ["total_steps"],
    "health_stats": ["weight", "body_fat", "bone_mass", "muscle_mass"],
}


def analyze_metric(
    curated_store: CuratedDataStore,
    dataset: str,
    metric: str,
    source_df: pd.DataFrame,
    length_scale_days: float = 21.0,
) -> None:
    """Classify one metric's points and fit its (single-scale) GP trend -- used for running."""
    if source_df.empty or metric not in source_df.columns:
        return

    points = classify_metric(source_df[["date", metric]], metric)
    curated_store.write_analyzed_points(dataset, metric, points)

    fittable = points[points["quality_weight"] > 0]
    trend = fit_gp_trend(
        fittable["date"],
        fittable[metric],
        fittable["quality_weight"],
        length_scale_days=length_scale_days,
    )
    curated_store.write_analyzed_trend(dataset, metric, trend, kind="gp")


def analyze_health_metric(curated_store: CuratedDataStore, dataset: str, metric: str, source_df: pd.DataFrame) -> None:
    """Classify one health metric's points and fit both comparison trends (multiscale GP + STS)."""
    if source_df.empty or metric not in source_df.columns:
        return

    points = classify_metric(source_df[["date", metric]], metric)
    curated_store.write_analyzed_points(dataset, metric, points)

    fittable = points[points["quality_weight"] > 0]

    gp_trend, gp_day_to_day_std = fit_gp_multiscale_trend(fittable["date"], fittable[metric], fittable["quality_weight"])
    if not gp_trend.empty:
        gp_trend["day_to_day_std"] = gp_day_to_day_std
    curated_store.write_analyzed_trend(dataset, metric, gp_trend, kind="gp_multiscale")

    sts_trend, sts_day_to_day_std = fit_structural_trend(
        fittable["date"], fittable[metric], fittable["quality_weight"],
        level="smooth trend", autoregressive_order=1,
    )
    if not sts_trend.empty:
        sts_trend["day_to_day_std"] = sts_day_to_day_std
    curated_store.write_analyzed_trend(dataset, metric, sts_trend, kind="sts")


def analyze_running(curated_store: CuratedDataStore) -> None:
    running = curated_store.load_activity_summary("running")
    if running.empty:
        print("No curated running data to analyze.")
        return

    running = running.copy()
    running["date"] = pd.to_datetime(running["date"])

    for metric in RUNNING_METRICS:
        analyze_metric(curated_store, "running", metric, running)
        print(f"Analyzed running.{metric}: quality points + GP trend written.")


def analyze_health(curated_store: CuratedDataStore) -> None:
    for dataset, metrics in HEALTH_METRICS.items():
        df = curated_store.load_daily(dataset)
        if df.empty:
            print(f"No curated {dataset} data to analyze.")
            continue

        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])

        for metric in metrics:
            analyze_health_metric(curated_store, dataset, metric, df)
            print(f"Analyzed {dataset}.{metric}: quality points + GP-multiscale + STS trends written.")


def analyze_all(curated_store: CuratedDataStore) -> None:
    analyze_running(curated_store)
    analyze_health(curated_store)
