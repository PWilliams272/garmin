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

import numpy as np
import pandas as pd

from garmin.analysis.quality import classify_metric
from garmin.analysis.trend_gp import fit_gp_multiscale_trend, fit_gp_trend
from garmin.analysis.trend_sts import fit_structural_trend
from garmin.io.curated_store import CuratedDataStore
from garmin.prototypes.activity_explorer import blended_1rm

# (dataset, metric) pairs to analyze. Extend as more metrics need the same
# quality-classification + GP-trend treatment.
RUNNING_METRICS = ["cadence_spm", "pace_min_per_mile", "distance_mi"]

HEALTH_METRICS = {
    "heart_rate": ["resting_hr"],
    "steps": ["total_steps"],
    "health_stats": ["weight", "body_fat", "bone_mass", "muscle_mass"],
}

# The frontend only ever renders the STS trend right now (quick_dashboard.html's
# SHOW_GP_TREND = false) -- the multiscale GP is kept in the codebase for a
# possible future side-by-side comparison, but isn't worth its cost while
# unused: fitting it against ~4000-day-long daily series is what pushed the
# analyzer Lambda's memory past 1024MB and got it OOM-killed. Flip this back
# on (and give the Lambda more memory) if the GP comparison view comes back.
FIT_GP_TREND = False

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

# How the `weight_lb` column should be interpreted for a given exercise.
# Getting this wrong isn't cosmetic: before this table existed, every
# exercise was fed through the same est-1RM path, which produced NaN series
# for exercises that are never loaded and an *inverted* series for assisted
# ones (see LOAD_TYPE_ASSISTED).
LOAD_TYPE_EXTERNAL = "external_load"      # weight_lb is the load being lifted
LOAD_TYPE_BODYWEIGHT = "bodyweight_reps"  # weight_lb is always 0; reps/duration is the signal
LOAD_TYPE_BW_PLUS = "bodyweight_plus"     # weight_lb is *added* to bodyweight
LOAD_TYPE_ASSISTED = "assisted"           # weight_lb is machine *assistance*, not load
LOAD_TYPE_MIXED = "mixed"                 # both bodyweight and machine-loaded sets occur

# Assigned from this account's real data. Note this table is keyed on
# Garmin's *coarse category*, which is a known weakness -- see
# resolve_variant() below, which is the finer unit modelling should use.
#
# A cautionary note on how pull_up was originally classified. The heuristic
# was: a genuine load makes reps fall as weight rises (negative correlation),
# while machine assistance makes them rise. Measured across categories,
# pull_up came back at **+0.39** while every other lift was negative, and it
# was labelled "assisted" on that basis.
#
# That was wrong, and the user corrected it: those are *weighted* pull-ups,
# bodyweight plus added load. The correlation was positive because Garmin's
# `pull_up` category lumps three different movements together -- bodyweight
# pull-ups (weight 0), weighted pull-ups (~25 lb added) and lat pulldowns
# (~47-120 lb of machine stack). Comparing across that mixture measures the
# difference between exercises, not a load-rep relationship within one.
#
# The lesson generalises: the sign test is only meaningful once the unit
# being correlated is a single movement.
EXERCISE_LOAD_TYPES = {
    "bench_press": LOAD_TYPE_EXTERNAL,
    "incline_bench_press": LOAD_TYPE_EXTERNAL,
    "decline_bench_press": LOAD_TYPE_EXTERNAL,
    "squat": LOAD_TYPE_EXTERNAL,
    "front_squat": LOAD_TYPE_EXTERNAL,
    "deadlift": LOAD_TYPE_EXTERNAL,
    "curl": LOAD_TYPE_EXTERNAL,
    "hammer_curl": LOAD_TYPE_EXTERNAL,
    "triceps_extension": LOAD_TYPE_EXTERNAL,
    "row": LOAD_TYPE_EXTERNAL,
    "shoulder_press": LOAD_TYPE_EXTERNAL,
    "lateral_raise": LOAD_TYPE_EXTERNAL,
    "flye": LOAD_TYPE_EXTERNAL,
    "leg_curl": LOAD_TYPE_EXTERNAL,
    "leg_extension": LOAD_TYPE_EXTERNAL,
    "leg_press": LOAD_TYPE_EXTERNAL,
    "lat_pulldown": LOAD_TYPE_EXTERNAL,
    "calf_raise": LOAD_TYPE_EXTERNAL,
    "sit_up": LOAD_TYPE_EXTERNAL,      # 0 of 106 sets are bodyweight in practice
    "chop": LOAD_TYPE_EXTERNAL,
    "hip_raise": LOAD_TYPE_EXTERNAL,
    "shoulder_stability": LOAD_TYPE_EXTERNAL,

    "plank": LOAD_TYPE_BODYWEIGHT,     # 174/174 sets at weight 0
    "leg_raise": LOAD_TYPE_BODYWEIGHT,  # 113/113 sets at weight 0
    "push_up": LOAD_TYPE_BODYWEIGHT,

    # Weighted pull-ups/dips: the number is added load, on top of bodyweight.
    # (This category also contains lat pulldowns in this account's data --
    # only variant-level modelling separates those out properly.)
    "pull_up": LOAD_TYPE_BW_PLUS,
    "chin_up": LOAD_TYPE_BW_PLUS,
    "dip": LOAD_TYPE_BW_PLUS,

    "crunch": LOAD_TYPE_MIXED,         # 123 bodyweight + 448 machine-loaded sets
}

# Load type keyed on substrings of Garmin's specific movement name
# (`exercise_name`), checked in order. First match wins, so put the more
# specific patterns first.
VARIANT_LOAD_TYPE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("BODY_WEIGHT", LOAD_TYPE_BODYWEIGHT),
    ("WEIGHTED_PULL_UP", LOAD_TYPE_BW_PLUS),
    ("WEIGHTED_DIP", LOAD_TYPE_BW_PLUS),
    ("PULLDOWN", LOAD_TYPE_EXTERNAL),   # a machine stack, not bodyweight
    ("PLANK", LOAD_TYPE_BODYWEIGHT),
    ("PUSH_UP", LOAD_TYPE_BODYWEIGHT),
    ("SIT_UP", LOAD_TYPE_BODYWEIGHT),
)

UNLABELLED_SUFFIX = "__unlabelled"


def resolve_variant(exercise: str, exercise_name: object) -> str:
    """The unit strength should actually be modelled on.

    Garmin reports a coarse `exercise` category and, when a set has been
    reviewed in the app, a specific `exercise_name`. The categories lump
    genuinely different movements: in this account `triceps_extension` spans
    a 55x weight range (cable kickback at 15 lb through dumbbell overhead
    extension at 55 lb to bodyweight dips), `curl` mixes 25 lb dumbbell curls
    with 60 lb barbell curls, and `pull_up` mixes weighted pull-ups with lat
    pulldowns.

    Modelling a category as one exercise therefore asks a smooth latent
    strength level to jump between load regimes that differ several-fold,
    which is very likely what has been making the posterior multi-modal.

    Sets without a specific name (about 81% here, since the name only appears
    once a set has been reviewed in Garmin Connect) get their own
    "<category>__unlabelled" unit rather than being merged into a named one.
    That keeps them separate without inventing a label for them -- for bench
    press the unlabelled sets sit at 115-155 lb with warmups down to 45 lb (an
    empty barbell), clearly distinct from the 55 lb labelled dumbbell work.
    """
    if exercise_name is None or (isinstance(exercise_name, float) and pd.isna(exercise_name)):
        return f"{exercise}{UNLABELLED_SUFFIX}"
    name = str(exercise_name).strip()
    if not name or name.lower() == "none":
        return f"{exercise}{UNLABELLED_SUFFIX}"
    return name


def variant_slug(variant: str) -> str:
    """Filesystem-safe key for a variant, used in analyzed output filenames."""
    return "".join(c if c.isalnum() else "_" for c in variant.lower()).strip("_")


def variant_load_type(variant: str, exercise: str) -> str:
    """Load type for a specific movement, falling back to its category."""
    upper = variant.upper()
    for pattern, load_type in VARIANT_LOAD_TYPE_PATTERNS:
        if pattern in upper:
            return load_type
    return load_type_for(exercise)


def load_type_for(exercise: str) -> str:
    """Load-type for an exercise (see EXERCISE_LOAD_TYPES).

    Unknown exercises default to external load -- that's the common case for
    anything new Garmin starts reporting, and it degrades to the pre-existing
    behaviour rather than silently dropping the exercise from analysis.
    """
    return EXERCISE_LOAD_TYPES.get(exercise, LOAD_TYPE_EXTERNAL)


def _assign_set_numbers(detail: pd.DataFrame) -> pd.DataFrame:
    """Adds a 1-indexed `set_number` -- each set's position within its own
    activity -- to a strength detail frame.

    This is the join key the exercise_review overlay is stored against, and
    it deliberately is *not* Garmin's own set_index/messageIndex field:
    confirmed live 2026-08-04 that 8155 of 9204 sets in this account carry
    set_index=None, so keying on it directly silently drops almost
    everything. Sorting by an all-null key per activity is a stable no-op
    (rows keep their already-chronological order), so this degrades
    gracefully for activities with no usable set_index at all.

    Shared by infer_exercise_corrections (which writes the overlay) and
    _apply_exercise_review_corrections (which reads it back) so the two
    can't drift apart on how a set is identified.
    """
    detail = detail.copy()
    if "set_index" not in detail.columns:
        detail["set_index"] = None
    detail = detail.sort_values(["activity_id", "set_index"], na_position="last").reset_index(drop=True)
    detail["set_number"] = detail.groupby("activity_id").cumcount() + 1
    return detail


# Review statuses whose exercise label is trusted enough to feed the trend
# models. "pending" is deliberately excluded -- an unreviewed guess should
# never silently change what gets analyzed -- and so is "rejected", where the
# user explicitly said to keep Garmin's original label.
_REVIEW_TRUSTED_STATUSES = ("accepted", "garmin_confirmed")


def _apply_exercise_review_corrections(detail: pd.DataFrame, review: pd.DataFrame) -> pd.DataFrame:
    """Overwrites `exercise` with the user-confirmed label from the
    exercise_review overlay, for sets that have one.

    Without this the review queue is decorative: analyze_lifting would keep
    grouping on Garmin's raw guess, so known-bad labels (e.g. this account's
    `curl` series topping out at 164.8 lb against a 37.5 lb median -- clearly
    mislabeled bench/press sets) keep inflating each exercise's maxima no
    matter how much reviewing has been done.

    Requires `set_number` on `detail` (see _assign_set_numbers) and must be
    called before any row-dropping that would shift positions.
    """
    if review.empty or "set_number" not in detail.columns:
        return detail

    trusted = review[review["review_status"].isin(_REVIEW_TRUSTED_STATUSES)]
    trusted = trusted.dropna(subset=["our_guess_exercise"])
    if trusted.empty:
        return detail

    corrections = trusted[["activity_id", "set_number", "our_guess_exercise"]].copy()
    corrections["activity_id"] = corrections["activity_id"].astype(str)
    corrections["set_number"] = corrections["set_number"].astype(int)

    detail = detail.copy()
    original_ids = detail["activity_id"]
    detail["activity_id"] = detail["activity_id"].astype(str)
    detail = detail.merge(corrections, on=["activity_id", "set_number"], how="left")
    detail["activity_id"] = original_ids.values

    corrected = detail["our_guess_exercise"].notna()
    changed = int((corrected & (detail["our_guess_exercise"] != detail["exercise"])).sum())
    detail.loc[corrected, "exercise"] = detail.loc[corrected, "our_guess_exercise"]
    detail = detail.drop(columns=["our_guess_exercise"])

    print(f"Applied {int(corrected.sum())} reviewed exercise labels ({changed} relabeled).")
    return detail


def _estimated_1rm(weight_lb: pd.Series, reps: pd.Series) -> pd.Series:
    """Blended multi-formula 1RM estimate (see blended_1rm), vectorized over
    a Series -- shared with the per-set estimate shown on the Activities
    tab's strength detail view and the exercise-explorer prototype, so the
    trend fit here and the "vs. your PR" comparison there are on the same
    scale. Was plain Epley (weight * (1 + reps/30)) until 2026-08-04;
    switching required re-running manual_analyze_metrics.py to refresh the
    curated/analyzed/strength/*_1rm_* output on the new scale."""
    return pd.Series(
        [blended_1rm(w, r) for w, r in zip(weight_lb, reps, strict=False)],
        index=weight_lb.index,
    )


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

    if FIT_GP_TREND:
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


def _bodyweight_by_date(curated_store: CuratedDataStore) -> pd.Series | None:
    """Daily bodyweight (lb), forward/back-filled across gaps.

    Needed to turn an assisted exercise's machine counterweight into an
    actual load (see _session_metrics). Weigh-ins are irregular (1590 of
    3890 days in this account), and bodyweight moves slowly enough that
    carrying the last reading forward is a reasonable interpolation.
    """
    stats = curated_store.load_daily("health_stats")
    if stats.empty or "weight" not in stats.columns:
        return None
    weights = stats[["date", "weight"]].dropna()
    if weights.empty:
        return None
    weights = weights.copy()
    weights["date"] = pd.to_datetime(weights["date"])
    series = weights.set_index("date")["weight"].sort_index()
    series = series[~series.index.duplicated(keep="last")]
    full_range = pd.date_range(series.index.min(), series.index.max(), freq="D")
    return series.reindex(full_range).ffill().bfill()


def _session_metrics(
    ex_df: pd.DataFrame, load_type: str, bodyweight: pd.Series | None
) -> tuple[pd.DataFrame, list[tuple[str, str]]]:
    """Collapse one exercise's sets to one row per session date.

    Returns (session_df, [(column, metric_suffix), ...]). Which metrics are
    meaningful depends on how the exercise is loaded:

    - external load -> top estimated 1RM + summed weight x reps volume.
    - assisted -> same, but on *effective* load (bodyweight - assistance).
      Using the raw column here inverts the trend: more machine help is a
      bigger number but a weaker athlete, which is why pull-ups previously
      charted as steady progress while measuring the opposite.
    - bodyweight -> reps, not load. An estimated 1RM on a column that is
      always 0 is what produced the all-NaN plank/leg_raise series.
    - mixed -> the machine-loaded sets get the external treatment; the
      bodyweight sets in the same exercise are dropped rather than averaged
      in, since they aren't measuring the same thing.
    """
    if load_type == LOAD_TYPE_BODYWEIGHT:
        session_df = (
            ex_df.groupby("date", as_index=False)
            .agg(top_reps=("reps", "max"), total_reps=("reps", "sum"))
            .sort_values("date")
        )
        return session_df, [("top_reps", "top_reps"), ("total_reps", "rep_volume")]

    ex_df = ex_df.copy()
    if load_type == LOAD_TYPE_MIXED:
        ex_df = ex_df[ex_df["weight_lb"] > 0]
    elif load_type in (LOAD_TYPE_ASSISTED, LOAD_TYPE_BW_PLUS):
        if bodyweight is None:
            return pd.DataFrame(), []
        body = bodyweight.reindex(pd.DatetimeIndex(ex_df["date"])).to_numpy()
        if load_type == LOAD_TYPE_BW_PLUS:
            # Weighted pull-ups/dips: you move your own bodyweight plus
            # whatever is hanging off you, so a 0 lb set is still a real set
            # at full bodyweight -- not a missing value.
            ex_df["weight_lb"] = body + ex_df["weight_lb"].to_numpy()
        else:
            ex_df["weight_lb"] = body - ex_df["weight_lb"].to_numpy()
        # Non-positive effective load is nonsense either way -- drop rather
        # than propagate it.
        ex_df = ex_df[ex_df["weight_lb"] > 0]

    if ex_df.empty:
        return pd.DataFrame(), []

    ex_df["est_1rm"] = _estimated_1rm(ex_df["weight_lb"], ex_df["reps"])
    ex_df["set_volume_lb"] = ex_df["weight_lb"] * ex_df["reps"]
    session_df = (
        ex_df.groupby("date", as_index=False)
        .agg(est_1rm=("est_1rm", "max"), volume_lb=("set_volume_lb", "sum"))
        .sort_values("date")
    )
    return session_df, [("est_1rm", "1rm"), ("volume_lb", "volume")]


def analyze_lifting(curated_store: CuratedDataStore) -> None:
    """Per-exercise strength and volume trends from strength set detail.

    Unlike analyze_running/analyze_health_metric (one row per date already),
    strength detail is one row per *set* -- collapse to one row per session
    date per exercise before quality classification and trend fitting, the
    same "one row per date" shape the rest of this pipeline (and
    quality.classify_metric) expects.

    Which metrics get written depends on the exercise's load type (see
    EXERCISE_LOAD_TYPES / _session_metrics): loaded lifts get `_1rm` and
    `_volume`, bodyweight ones get `_top_reps` and `_rep_volume`.
    """
    from garmin.analysis.exercise_taxonomy import family_for

    detail = _reviewed_strength_detail(curated_store)
    if detail.empty:
        print("No curated strength data to analyze.")
        return

    detail = detail.dropna(subset=["reps", "weight_lb"])
    bodyweight = _bodyweight_by_date(curated_store)

    # Group by *variant*, not Garmin's category. A single "bench_press"
    # series that mixes 55 lb dumbbell work with 135 lb barbell work is two
    # exercises sharing an axis, and its trend is dominated by which
    # implement was in use rather than by getting stronger.
    session_counts = detail.groupby("variant")["activity_id"].nunique()
    variants = sorted(session_counts[session_counts >= STRENGTH_MIN_SESSIONS].index)

    written = []
    for variant in variants:
        variant_df = detail[detail["variant"] == variant]
        exercise = variant_df["exercise"].iloc[0]
        load_type = variant_load_type(variant, exercise)
        session_df, metric_specs = _session_metrics(variant_df, load_type, bodyweight)
        if session_df.empty:
            print(f"Skipped strength.{variant}: no usable sets for load type '{load_type}'.")
            continue

        slug = variant_slug(variant)
        for metric, suffix in metric_specs:
            dataset_metric = f"{slug}_{suffix}"
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

        written.append({
            "variant": variant,
            "slug": slug,
            "family": family_for(variant, exercise),
            "exercise": exercise,
            "load_type": load_type,
            "sessions": int(len(session_df)),
            "metrics": [suffix for _metric, suffix in metric_specs],
            "median_weight": float(variant_df["weight_lb"].median()),
        })
        print(f"Analyzed strength.{variant} ({load_type}): {len(session_df)} sessions.")

    # An index of what was written, so the web tier can group variants into
    # families without re-deriving the taxonomy or guessing at file names.
    if written:
        curated_store.write_strength_variant_index("strength", pd.DataFrame(written))

    _analyze_family_combined(curated_store, detail)


def _analyze_family_combined(curated_store: CuratedDataStore, detail: pd.DataFrame) -> None:
    """Per-*family* strength series, combining variants onto one scale.

    The per-variant series answer "how is my dumbbell bench going". This
    answers "how is my bench going" -- which needs the variants converted onto
    a common scale first, because switching implement otherwise reads as a
    step change in strength.

    Only conversions that pass their checks are used. A variant whose factor
    failed is left out of the combined series rather than folded in on a
    number we don't believe; its own per-variant series is unaffected.
    """
    from garmin.analysis.variant_conversion import fit_variant_conversions, session_bests

    sessions = session_bests(detail)
    if sessions.empty:
        return

    conversions = fit_variant_conversions(sessions)
    if conversions.empty:
        return
    curated_store.write_variant_conversions("strength", conversions)

    trusted = conversions[conversions["identified"]]
    factors = dict(zip(trusted["variant"], trusted["factor"], strict=True))

    combined = sessions[sessions["variant"].isin(factors)].copy()
    if combined.empty:
        return
    combined["est_1rm"] = combined["est_1rm"] * combined["variant"].map(factors)

    for family, group in combined.groupby("family"):
        # One session can hit the same family through two variants; keep the
        # best converted estimate for the day.
        family_df = (
            group.groupby("date", as_index=False)["est_1rm"].max().sort_values("date")
        )
        if len(family_df) < STRENGTH_MIN_SESSIONS:
            continue

        dataset_metric = f"family_{variant_slug(family)}_1rm"
        points = classify_metric(family_df[["date", "est_1rm"]], "est_1rm")
        curated_store.write_analyzed_points("strength", dataset_metric, points)

        fittable = points[points["quality_weight"] > 0]
        trend, day_to_day_std = fit_structural_trend(
            fittable["date"], fittable["est_1rm"], fittable["quality_weight"],
            level="smooth trend",
        )
        if not trend.empty:
            trend["day_to_day_std"] = day_to_day_std
        curated_store.write_analyzed_trend("strength", dataset_metric, trend, kind="sts")

        used = sorted(group["variant"].unique())
        print(f"Analyzed strength family {family}: {len(family_df)} sessions "
              f"from {len(used)} variant(s) {used}.")

    dropped = conversions[~conversions["identified"]]
    for _, row in dropped.iterrows():
        print(f"Excluded {row['variant']} from family {row['family']}: "
              f"conversion not identified "
              f"(agrees={row.get('agrees')}, homogeneous={row.get('homogeneous')}).")


def _reviewed_strength_detail(curated_store: CuratedDataStore) -> pd.DataFrame:
    """Strength set detail with dates joined and reviewed exercise labels
    applied -- the common front half of analyze_lifting and
    fit_strength_curves_for_all."""
    summary = curated_store.load_activity_summary("strength")
    detail = curated_store.load_all_activity_details("strength")
    if summary.empty or detail.empty:
        return pd.DataFrame()

    detail = detail.merge(summary[["activity_id", "date"]], on="activity_id", how="left")
    detail = detail.dropna(subset=["date"])
    detail = _assign_set_numbers(detail)
    detail = _apply_exercise_review_corrections(detail, curated_store.load_exercise_review("strength"))
    detail["date"] = pd.to_datetime(detail["date"])
    detail["weight_lb"] = pd.to_numeric(detail["weight_lb"], errors="coerce")

    # Resolve the specific movement. Garmin's category is too coarse to model
    # on -- a "bench_press" series that mixes 55 lb dumbbell work with 135 lb
    # barbell work is two different exercises sharing an axis.
    from garmin.analysis.exercise_taxonomy import infer_variants

    detail = infer_variants(detail)

    # Flag sets whose recorded duration is the watch being left running rather
    # than time under load. Only the timing is affected, so these rows still
    # count toward strength and volume -- they just can't be used for tempo.
    detail = reject_watch_left_running(detail)
    return detail


def fit_strength_curves_for_all(curated_store: CuratedDataStore, **fit_kwargs) -> None:
    """Fit the hierarchical load-rep model and write per-exercise eXRM curves.

    Deliberately *not* part of analyze_all: this samples (MCMC), which takes
    minutes and far more memory than the daily garmin-data-analyzer Lambda
    has -- that Lambda was already OOM-killed at 1024MB by the much cheaper
    GP fits. Run it from scripts/manual_fit_strength_curves.py on a schedule
    of its own; the daily job and the web tier just read what it leaves
    behind.

    Only external-load exercises are fit. Bodyweight ones have no load to
    model, and assisted ones would need bodyweight-corrected effective load
    threading through the model (analyze_lifting already does that for its
    simpler estimate) -- deferred rather than approximated.
    """
    from garmin.analysis.strength_curve import fit_strength_curves

    detail = _reviewed_strength_detail(curated_store)
    if detail.empty:
        print("No curated strength data to fit strength curves for.")
        return

    sets_by_exercise = {}
    for exercise in STRENGTH_EXERCISE_CANDIDATES:
        if load_type_for(exercise) != LOAD_TYPE_EXTERNAL:
            continue
        ex_df = detail[detail["exercise"] == exercise]
        if not ex_df.empty:
            sets_by_exercise[exercise] = ex_df

    if not sets_by_exercise:
        print("No external-load exercises with sets to fit.")
        return

    curves, betas, diagnostics = fit_strength_curves(sets_by_exercise, **fit_kwargs)

    if not betas.empty:
        print("\nFitted rep-decay exponents (higher = strength drops off faster with reps):")
        print(betas.round(3).to_string(index=False))

    # Refuse to persist a fit whose chains didn't mix. Writing it anyway is
    # actively worse than writing nothing: the web tier headlines these
    # numbers, and a non-converged posterior still produces tight-looking
    # intervals. Seen live at max r-hat 2.88, which put a lateral-raise e1RM
    # at 2.4x its e8RM.
    if not diagnostics.get("converged"):
        print(
            f"\nNOT WRITING {len(curves)} curves: fit failed convergence "
            f"(max r-hat={diagnostics.get('max_rhat'):.3f}, "
            f"divergences={diagnostics.get('divergences')}). "
            "Re-run with more tuning, or reduce the model's latent resolution."
        )
        return

    for exercise, curve in curves.items():
        curated_store.write_strength_curve("strength", exercise, curve)
    print(f"\nWrote {len(curves)} strength curves.")


REVIEW_STATUS_GARMIN_CONFIRMED = "garmin_confirmed"
REVIEW_STATUS_PENDING = "pending"
REVIEW_STATUS_ACCEPTED = "accepted"
REVIEW_STATUS_REJECTED = "rejected"

# Sets manually reviewed in the Garmin app are trusted outright (the user
# already looked at them); this is only ever consulted for the remaining
# ambiguous/unreviewed sets.
_USER_DECIDED_STATUSES = {REVIEW_STATUS_ACCEPTED, REVIEW_STATUS_REJECTED}


def detect_session_stride(exercises: list) -> int:
    """How many sets apart two sets of the *same* exercise sit in a session.

    Straight-sets sessions do all of one exercise before moving on, so the
    next set of the same exercise is 1 away. Supersets alternate --
    bench, curl, bench, curl -- so it's 2 away, and the immediate neighbour
    is a *different* exercise.

    This matters because the neighbour-consensus rule used to infer an
    ambiguous set's exercise assumes adjacent sets are the same movement.
    Measured on this account, 187 of 468 sessions (40%) are alternating, so
    treating every session as straight-sets weights precisely the wrong
    neighbours most heavily in a large minority of the data.

    Returns 1 (straight sets) or 2 (alternating). Only these two are
    detected: they're what actually shows up here, and inferring longer
    cycles from a handful of sets would be over-reading the data.
    """
    if len(exercises) < 6:
        return 1
    adjacent_same = sum(1 for i in range(len(exercises) - 1) if exercises[i] == exercises[i + 1])
    skip_same = sum(1 for i in range(len(exercises) - 2) if exercises[i] == exercises[i + 2])
    adjacent_rate = adjacent_same / (len(exercises) - 1)
    skip_rate = skip_same / (len(exercises) - 2)
    # Require a clear margin rather than a bare win, so a session that merely
    # happens to repeat isn't reclassified on noise.
    return 2 if skip_rate > adjacent_rate + 0.25 else 1


# Time constant for within-session fatigue decay, in seconds.
#
# *** This number has no research backing. It is a modelling choice. ***
#
# It is emphatically NOT the Banister value. Banister's fitness-fatigue model
# operates on training loads across weeks: the commonly cited fitted constants
# are roughly 42 days for the fitness term and 7 days for the fatigue term
# (Banister et al. 1975; Morton, Fitz-Clarke & Banister 1990), and they vary
# by individual and by study. Those describe multi-week adaptation, not what
# happens between your third and fourth set.
#
# Nor is there an obvious within-session number to borrow. The nearest
# measured quantity is phosphocreatine resynthesis, whose fast component has a
# half-time of roughly 30 seconds (Harris et al. 1976) -- but a tau that short
# would have fatigue almost fully cleared by a 72-second rest (this account's
# median), which would show essentially no accumulation across a session and
# contradicts the obvious fact that late sets are harder. Neuromuscular and
# central fatigue outlast metabolic recovery, and there is no single
# established constant for their combination.
#
# 360s was picked to sit between those extremes: long enough that consecutive
# sets stack, short enough that a five-minute break between exercises mostly
# clears. It reproduces the qualitative shape of a session. It is not a
# measurement, and the index it produces should not be compared across people
# or treated as physiological.
FATIGUE_TAU_S = 360.0


# Plausible seconds-per-rep for a *counted* rep. Measured across this
# account: median 5.1, p90 8.1, p95 9.7, p99 16.7 -- then a long thin tail out
# to 203 s/rep, which is the watch being left running into the rest period
# rather than anything that happened under load. Only ~0.9% of sets exceed 20.
MAX_SEC_PER_REP = 20.0
MIN_SEC_PER_REP = 0.8


def reject_watch_left_running(detail: pd.DataFrame) -> pd.DataFrame:
    """Flag sets whose recorded duration can't be the time spent lifting.

    Adds a boolean `duration_suspect` column; nothing is dropped here, so
    callers can decide whether to exclude those rows or just avoid using
    their timing.

    Two things make this less trivial than a threshold on seconds-per-rep:

    1. **Timed holds are not rep-based.** A plank logged as "1 rep, 60s" is a
       perfectly good 60-second hold, not a forgotten watch. Anything whose
       load type is bodyweight is therefore exempt -- see EXERCISE_LOAD_TYPES.
    2. **The failure is one-sided.** Forgetting to stop the watch only ever
       inflates a duration, so the upper bound does the real work; the lower
       bound just catches obviously corrupt rows.

    Only the *timing* is suspect on these rows. The reps and weight are still
    fine, which is why this returns a flag rather than deleting anything --
    the sets remain usable for strength estimates and only drop out of
    tempo-based work.
    """
    out = detail.copy()
    if "duration_s" not in out.columns or "reps" not in out.columns:
        out["duration_suspect"] = False
        return out

    duration = pd.to_numeric(out["duration_s"], errors="coerce")
    reps = pd.to_numeric(out["reps"], errors="coerce")
    sec_per_rep = duration / reps.where(reps > 0)

    if "exercise" in out.columns:
        timed_hold = out["exercise"].map(
            lambda ex: load_type_for(str(ex)) == LOAD_TYPE_BODYWEIGHT
        ).fillna(False)
    else:
        timed_hold = pd.Series(False, index=out.index)

    out["sec_per_rep"] = sec_per_rep
    out["duration_suspect"] = (
        sec_per_rep.notna()
        & ~timed_hold
        & ((sec_per_rep > MAX_SEC_PER_REP) | (sec_per_rep < MIN_SEC_PER_REP))
    )
    return out


def session_fatigue_curve(
    set_times_s, set_work, sample_times_s, tau_s: float = FATIGUE_TAU_S
) -> list[float]:
    """Accumulated within-session fatigue, as an exponentially-decaying sum of
    the work done so far.

    Each completed set adds an impulse proportional to its work, and that
    impulse decays with time constant `tau_s`:

        F(t) = sum_i  work_i * exp(-(t - t_i) / tau)   for all sets i before t

    The *functional form* -- an exponentially-decaying sum of past impulses --
    is borrowed from the fatigue half of Banister's fitness-fatigue model
    (Banister, Calvert, Savage & Bach 1975, "A systems model of training for
    athletic performance"; see also Morton, Fitz-Clarke & Banister 1990).

    **The timescale is not Banister's and is not from any literature.**
    Banister's model runs on weeks, with fitted constants around 42 days
    (fitness) and 7 days (fatigue). Applying that shape to minutes within one
    workout is an analogy, not an established method, and `tau_s` here is a
    modelling choice -- see FATIGUE_TAU_S for why 360s and why nothing better
    was available to copy.

    So: a heuristic, not a fitted result. Nothing has been validated against
    this account's data, and the output is scaled to a 0-100 index precisely
    because its absolute units mean nothing. Useful for reading a session's
    shape; not a physiological measurement, and not comparable between people.

    Making it real would mean fitting `tau` against something observable --
    the decline in achievable load late in a session, or next-day HRV -- and
    testing whether the impulse should scale with intensity rather than raw
    volume. Both are possible with data already on hand.
    """
    times = np.asarray(list(set_times_s), dtype=float)
    work = np.asarray(list(set_work), dtype=float)
    samples = np.asarray(list(sample_times_s), dtype=float)
    if times.size == 0 or samples.size == 0:
        return [0.0] * samples.size

    finite = np.isfinite(times) & np.isfinite(work)
    times, work = times[finite], work[finite]
    if times.size == 0:
        return [0.0] * samples.size

    # (samples, sets): each set contributes only after it has happened.
    delta = samples[:, None] - times[None, :]
    contribution = np.where(delta >= 0, work[None, :] * np.exp(-np.maximum(delta, 0) / tau_s), 0.0)
    fatigue = contribution.sum(axis=1)

    peak = float(fatigue.max())
    if peak <= 0:
        return [0.0] * samples.size
    return [round(float(v) / peak * 100.0, 1) for v in fatigue]


def _personal_weight_ranges(detail: pd.DataFrame) -> dict[str, tuple[float, float]]:
    """Median + MAD-based weight scale per exercise, built only from sets
    the user has actually confirmed (manually_reviewed) -- a personal prior,
    not a generic norm, and deliberately simple (no time-weighting for
    progression) for a first pass; revisit if it proves too coarse across
    someone's multi-year weight progression on one exercise."""
    reviewed = detail[detail["manually_reviewed"] == True]  # noqa: E712
    ranges = {}
    for exercise, group in reviewed.groupby("exercise"):
        weights = group["weight_lb"].dropna()
        if len(weights) < 5:
            continue
        median = float(weights.median())
        mad = float((weights - median).abs().median()) * 1.4826
        ranges[exercise] = (median, mad if mad > 1.0 else max(float(weights.std()), 1.0))
    return ranges


def _weight_plausibility(weight: float | None, exercise: str, ranges: dict[str, tuple[float, float]]) -> float:
    if exercise not in ranges or weight is None or pd.isna(weight):
        return 0.0
    median, scale = ranges[exercise]
    z = abs(weight - median) / scale
    return max(0.0, 1.0 - z / 4.0)


def infer_exercise_corrections(curated_store: CuratedDataStore) -> None:
    """Best-effort exercise guess for sets Garmin's own classifier left
    ambiguous/unreviewed -- read-only with respect to the raw pulled data
    (never touches curated/activities/detail/strength/ itself), writing to
    a separate exercise_review overlay (activity_id + set_number -> guess/
    confidence/review_status) that a future review-queue UI will let the
    user accept or reject. Recomputing never overwrites an existing
    accepted/rejected decision -- only garmin_confirmed/pending rows are
    refreshed each run.

    Keyed by set_number (1-indexed position within the activity, same
    definition routes.py's _strength_activity_detail_payload uses for
    display) rather than the raw set_index/messageIndex field -- confirmed
    live 2026-08-04 that most sets (8155 of 9204 in this account's history)
    have set_index=None from Garmin's own API, so keying on it directly
    would silently drop almost everything, as an earlier version of this
    function did.

    Two signals, combined: (1) neighboring sets in the same session/activity
    within 20% of this set's weight ("stayed on the same exercise, may have
    paused the rep counter") -- full-strength vote if that neighbor is
    garmin_confirmed, scaled by Garmin's own top-candidate probability
    otherwise, since requiring a *confirmed* neighbor left any session with
    zero reviewed sets falling through to signal (2) alone, which wasn't
    enough on its own (verified live: got every set wrong in exactly that
    case) -- and (2) how typical this set's weight is for a given exercise,
    from that exercise's own personal weight range (see
    _personal_weight_ranges). Both heuristics, not a trained model; treat
    low-confidence output as a starting point for manual review, not a fact.
    """
    summary = curated_store.load_activity_summary("strength")
    detail = curated_store.load_all_activity_details("strength")
    if summary.empty or detail.empty:
        print("No curated strength data to infer exercise corrections for.")
        return

    detail = detail.merge(summary[["activity_id", "date"]], on="activity_id", how="left")
    detail = detail.dropna(subset=["date"])
    detail["date"] = pd.to_datetime(detail["date"])
    if "manually_reviewed" not in detail.columns:
        detail["manually_reviewed"] = False
    detail["manually_reviewed"] = detail["manually_reviewed"].fillna(False)
    if "set_index" not in detail.columns:
        detail["set_index"] = None
    if "candidate_1_probability" not in detail.columns:
        detail["candidate_1_probability"] = None
    detail = _assign_set_numbers(detail)

    ranges = _personal_weight_ranges(detail)

    existing = curated_store.load_exercise_review("strength")
    existing_by_key = {}
    if not existing.empty:
        for _, row in existing.iterrows():
            existing_by_key[(str(row["activity_id"]), int(row["set_number"]))] = row.to_dict()

    rows = []
    for activity_id, group in detail.groupby("activity_id", sort=False):
        group = group.reset_index(drop=True)
        exercises = group["exercise"].tolist()
        weights = group["weight_lb"].tolist()
        reviewed_flags = group["manually_reviewed"].tolist()
        top_probabilities = (
            group["candidate_1_probability"].tolist() if "candidate_1_probability" in group.columns
            else [None] * len(group)
        )

        # Superset sessions alternate between exercises, so "the neighbouring
        # set" means two along, not one.
        stride = detect_session_stride(exercises)
        set_numbers = group["set_number"].tolist()
        for i in range(len(group)):
            set_number = int(set_numbers[i])
            key = (str(activity_id), set_number)

            if reviewed_flags[i]:
                rows.append({
                    "activity_id": activity_id, "set_number": set_number,
                    "original_exercise": exercises[i], "weight_lb": weights[i], "reps": group.iloc[i]["reps"],
                    "our_guess_exercise": exercises[i], "confidence": 1.0,
                    "review_status": REVIEW_STATUS_GARMIN_CONFIRMED,
                    "reason": "Manually reviewed in Garmin Connect",
                })
                continue

            prior = existing_by_key.get(key)
            if prior is not None and prior.get("review_status") in _USER_DECIDED_STATUSES:
                rows.append(prior)
                continue

            # A confirmed neighbor votes at full strength; an unreviewed one
            # still votes, just scaled by Garmin's own top-candidate
            # probability for that set -- even a "54% bench_press" neighbor
            # is real evidence, and requiring a reviewed neighbor left every
            # session with zero manually_reviewed sets (a real one, found
            # while testing: an entire session came back with wrong guesses
            # across the board because this fell through to the much
            # weaker weight-plausibility-only signal for every set).
            # "unknown" is Garmin's own low-confidence filler category, not
            # a real exercise, so it never contributes a vote.
            #
            # Neighbours are stepped by the session's stride (see
            # detect_session_stride): in a superset session the set next door
            # is a *different* exercise, and the same movement is two along.
            # Weighting the immediate neighbour most heavily is right for
            # straight sets and exactly backwards for the 40% of sessions
            # here that alternate.
            neighbor_votes: dict[str, float] = {}
            for step in (-2, -1, 1, 2):
                j = i + step * stride
                if not (0 <= j < len(group)) or exercises[j] == "unknown":
                    continue
                if not (weights[j] and weights[i] and pd.notnull(weights[i]) and abs(weights[i] - weights[j]) / weights[j] < 0.2):
                    continue
                strength = 1.0 if reviewed_flags[j] else ((top_probabilities[j] or 0) / 100.0)
                if strength <= 0:
                    continue
                neighbor_votes[exercises[j]] = neighbor_votes.get(exercises[j], 0.0) + strength * (2.0 if abs(step) == 1 else 1.0)

            plausibility = {ex: _weight_plausibility(weights[i], ex, ranges) for ex in ranges}

            # Garmin's own top guess for *this* set is direct evidence about
            # it (unlike a neighbor's guess, which is only indirect via
            # weight proximity) -- weighting it in, not just using it as a
            # fallback, stops a merely-coincidental same-weight neighbor
            # from outvoting an already-decent Garmin guess (verified live:
            # a triceps_extension set at 69.5% got flipped to "sit_up"
            # purely because a neighboring sit_up set happened to use a
            # similar weight, before this was added).
            own_guess_vote: dict[str, float] = {}
            if exercises[i] != "unknown" and top_probabilities[i]:
                own_guess_vote[exercises[i]] = top_probabilities[i] / 100.0

            # Normalize against a fixed reference (roughly "two confirmed
            # adjacent neighbors agree"), not the winning candidate's own
            # vote -- dividing by max(neighbor_votes) always maps whichever
            # exercise happens to be ahead to 1.0, which let one single,
            # only-coincidentally-similar-weight neighbor look just as
            # strong as a whole session's worth of agreement (verified
            # live: exactly this let one nearby sit_up set at a vaguely
            # similar weight outvote Garmin's own 69.5%-confidence guess).
            NEIGHBOR_FULL_STRENGTH = 3.0
            combined: dict[str, float] = {}
            for ex, vote in neighbor_votes.items():
                combined[ex] = combined.get(ex, 0.0) + 0.45 * min(1.0, vote / NEIGHBOR_FULL_STRENGTH)
            for ex, score in own_guess_vote.items():
                combined[ex] = combined.get(ex, 0.0) + 0.4 * score
            for ex, score in plausibility.items():
                if score > 0:
                    combined[ex] = combined.get(ex, 0.0) + 0.15 * score

            if not combined:
                rows.append({
                    "activity_id": activity_id, "set_number": set_number,
                    "original_exercise": exercises[i], "weight_lb": weights[i], "reps": group.iloc[i]["reps"],
                    "our_guess_exercise": exercises[i], "confidence": 0.0,
                    "review_status": REVIEW_STATUS_PENDING,
                    "reason": "No strong signal -- kept Garmin's own top guess.",
                })
                continue

            guess = max(combined, key=combined.get)
            confidence = round(min(1.0, combined[guess]), 2)
            reason_bits = []
            if guess in own_guess_vote:
                reason_bits.append("Garmin's own top guess for this set")
            if guess in neighbor_votes:
                reason_bits.append("matches neighboring sets in this session")
            if plausibility.get(guess, 0) > 0.5:
                reason_bits.append("typical weight for you on this exercise")
            rows.append({
                "activity_id": activity_id, "set_number": set_number,
                "original_exercise": exercises[i], "weight_lb": weights[i], "reps": group.iloc[i]["reps"],
                "our_guess_exercise": guess, "confidence": confidence,
                "review_status": REVIEW_STATUS_PENDING,
                "reason": "Based on " + " and ".join(reason_bits) if reason_bits else "Weak signal.",
            })

    result = pd.DataFrame(rows)
    curated_store.write_exercise_review("strength", result)
    pending = int((result["review_status"] == REVIEW_STATUS_PENDING).sum())
    print(f"Inferred exercise review for {len(result)} sets ({pending} pending review).")


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


# Metrics pull_cardio_summary (garmin.pullers.activities) always produces,
# regardless of sport -- unlike running's cadence_spm, which is running-only
# and stays in RUNNING_METRICS/analyze_running above.
CARDIO_METRICS = ["distance_mi", "pace_min_per_mile"]


def analyze_cardio_activities(curated_store: CuratedDataStore) -> None:
    """Same single-scale-GP treatment as analyze_running, generalized to every
    other cardio activity dataset (cycling, hiking, swimming, ...) in
    updaters.ACTIVITY_DATASETS. Running and strength keep their own richer
    analyze_running/analyze_lifting and are skipped here.
    """
    # Imported lazily (not at module top) to avoid a hard import-time
    # dependency from analysis -> updaters for a single small constant list.
    from garmin.updaters import ACTIVITY_DATASETS

    for dataset in ACTIVITY_DATASETS:
        if dataset in {"running", "strength"}:
            continue

        summary = curated_store.load_activity_summary(dataset)
        if summary.empty:
            print(f"No curated {dataset} data to analyze.")
            continue

        summary = summary.copy()
        summary["date"] = pd.to_datetime(summary["date"])

        for metric in CARDIO_METRICS:
            analyze_metric(curated_store, dataset, metric, summary)
            print(f"Analyzed {dataset}.{metric}: quality points + GP trend written.")


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
            gp_note = "GP-multiscale + " if FIT_GP_TREND else ""
            print(f"Analyzed {dataset}.{metric}: quality points + {gp_note}STS trend written.")


def analyze_all(curated_store: CuratedDataStore) -> None:
    analyze_running(curated_store)
    analyze_cardio_activities(curated_store)
    analyze_lifting(curated_store)
    infer_exercise_corrections(curated_store)
    analyze_health(curated_store)
    # Deterministic aggregation, so it belongs in the daily run: everything in
    # the predictive/causal work reads this rather than re-deriving load.
    from garmin.analysis.daily_panel import analyze_daily_panel
    analyze_daily_panel(curated_store)
