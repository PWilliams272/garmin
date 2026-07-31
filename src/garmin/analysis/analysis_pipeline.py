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

# An exercise needs at least this many distinct sessions before a trend is
# worth fitting -- avoids a near-empty STS fit on a exercise tried once or
# twice. Chosen by inspecting real set counts (see this repo's strength
# data): with this threshold, common lifts (bench/squat/deadlift/curl/rows/
# pull-ups/...) clear it while one-off exercises don't.
STRENGTH_MIN_SESSIONS = 20

# Superset of Garmin's strength exercise-category taxonomy observed in this
# repo's real curated data. Deliberately broader than what actually clears
# STRENGTH_MIN_SESSIONS today -- analyze_lifting only writes analyzed output
# for exercises that clear the threshold, so this list can include exercises
# not yet trained enough to trend without needing a code change once they
# are. "unknown" (Garmin's own low-confidence category) is excluded.
STRENGTH_EXERCISE_CANDIDATES = [
    "bench_press", "squat", "deadlift", "curl", "triceps_extension", "row",
    "pull_up", "shoulder_press", "lateral_raise", "crunch", "sit_up", "flye",
    "plank", "leg_curl", "leg_raise", "hip_raise", "chop", "push_up",
    "shoulder_stability",
]

# Epley formula: estimates the weight liftable for 1 rep from a set's actual
# reps/weight, so sessions with different rep ranges are comparable on one
# strength axis instead of raw top-set weight alone being noisier signal.
def _estimated_1rm(weight_lb: pd.Series, reps: pd.Series) -> pd.Series:
    return weight_lb * (1 + reps / 30.0)


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
        level="smooth trend",
    )
    if not sts_trend.empty:
        sts_trend["day_to_day_std"] = sts_day_to_day_std
    curated_store.write_analyzed_trend(dataset, metric, sts_trend, kind="sts")


def analyze_lifting(curated_store: CuratedDataStore) -> None:
    """Per-exercise estimated-1RM and session-volume trends from strength set detail.

    Unlike analyze_running/analyze_health_metric (one row per date already),
    strength detail is one row per *set* -- collapse to one row per session
    date per exercise (top estimated-1RM, summed volume) before quality
    classification and trend fitting, same "one row per date" shape the
    rest of this pipeline (and quality.classify_metric) expects.
    """
    summary = curated_store.load_activity_summary("strength")
    if summary.empty:
        print("No curated strength data to analyze.")
        return

    detail = curated_store.load_all_activity_details("strength")
    if detail.empty:
        print("No curated strength set detail to analyze.")
        return

    detail = detail.merge(summary[["activity_id", "date"]], on="activity_id", how="left")
    detail = detail.dropna(subset=["date", "reps", "weight_lb"])
    detail["date"] = pd.to_datetime(detail["date"])
    detail["est_1rm"] = _estimated_1rm(detail["weight_lb"], detail["reps"])
    detail["set_volume_lb"] = detail["weight_lb"] * detail["reps"]

    session_counts = detail.groupby("exercise")["activity_id"].nunique()
    exercises = [
        ex for ex in STRENGTH_EXERCISE_CANDIDATES
        if session_counts.get(ex, 0) >= STRENGTH_MIN_SESSIONS
    ]

    for exercise in exercises:
        ex_df = detail[detail["exercise"] == exercise]
        session_df = (
            ex_df.groupby("date", as_index=False)
            .agg(est_1rm=("est_1rm", "max"), volume_lb=("set_volume_lb", "sum"))
            .sort_values("date")
        )

        for metric, dataset_metric in [("est_1rm", f"{exercise}_1rm"), ("volume_lb", f"{exercise}_volume")]:
            points = classify_metric(session_df[["date", metric]], metric)
            curated_store.write_analyzed_points("strength", dataset_metric, points)

            fittable = points[points["quality_weight"] > 0]
            trend, day_to_day_std = fit_structural_trend(
                fittable["date"], fittable[metric], fittable["quality_weight"],
                level="smooth trend",
            )
            if not trend.empty:
                trend["day_to_day_std"] = day_to_day_std
            curated_store.write_analyzed_trend("strength", dataset_metric, trend, kind="sts")

        print(f"Analyzed strength.{exercise}: {len(session_df)} sessions, quality points + STS trend written.")


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
    analyze_lifting(curated_store)
    analyze_health(curated_store)
