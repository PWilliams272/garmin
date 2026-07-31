"""Batch analysis pipeline: curated activity data -> analyzed layer.

Runs quality classification (garmin.analysis.quality) and GP trend fitting
(garmin.analysis.trend_gp) once per metric and writes the results to
CuratedDataStore's "analyzed" layer, so the web app never has to compute
either on request -- it only reads precomputed output.

Layers, per the repo's raw -> curated -> analyzed -> viewer split:
- curated/activities/summary/<dataset>.parquet   (raw-ish, one row per activity)
- curated/analyzed/<dataset>/<metric>_points.parquet  (per-point quality tier/weight)
- curated/analyzed/<dataset>/<metric>_trend.parquet   (GP mean + 95% CI curve)
"""

from __future__ import annotations

import pandas as pd

from garmin.analysis.quality import classify_metric
from garmin.analysis.trend_gp import fit_gp_trend
from garmin.io.curated_store import CuratedDataStore

# (dataset, metric) pairs to analyze. Extend as more metrics need the same
# quality-classification + GP-trend treatment.
RUNNING_METRICS = ["cadence_spm", "pace_min_per_mile", "distance_mi"]


def analyze_metric(
    curated_store: CuratedDataStore,
    dataset: str,
    metric: str,
    source_df: pd.DataFrame,
    length_scale_days: float = 21.0,
) -> None:
    """Classify one metric's points and fit its GP trend, writing both to the analyzed layer."""
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
    curated_store.write_analyzed_trend(dataset, metric, trend)


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


def analyze_all(curated_store: CuratedDataStore) -> None:
    analyze_running(curated_store)
