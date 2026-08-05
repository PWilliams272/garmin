"""Movement taxonomy: Garmin category -> family -> variant.

Three levels, because the data needs all three and they do different jobs:

- **category** -- what Garmin reports in `exercise`. Too coarse to model on.
  Its `row` bucket contains both 15 lb face pulls and 130 lb barbell rows;
  its `crunch` bucket contains leg extensions; `pull_up` contains lat
  pulldowns.
- **family** -- the movement pattern progress is actually tracked against
  ("biceps curl", "overhead triceps extension"). This is the unit to compare
  and plot *together*.
- **variant** -- the specific implement or grip (dumbbell vs EZ-bar vs
  barbell curl). Same family, but a different absolute load, so each needs
  its own strength level and its own colour on a chart.

The family map below is a first pass built from this account's real logged
names. It encodes training intent, not just data, so it is expected to need
correcting by the person who did the training.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# variant name (Garmin's `exercise_name`) -> family.
# Grouped by family for readability; inverted into a lookup at import.
FAMILY_VARIANTS: dict[str, tuple[str, ...]] = {
    # ---- chest ----
    "flat_bench_press": ("DUMBBELL_BENCH_PRESS", "BARBELL_BENCH_PRESS", "BENCH_PRESS"),
    "incline_bench_press": (
        "INCLINE_BARBELL_BENCH_PRESS",
        "NEUTRAL_GRIP_DUMBBELL_INCLINE_BENCH_PRESS",
        "INCLINE_DUMBBELL_BENCH_PRESS",
    ),
    "chest_flye": ("DUMBBELL_FLYE", "CABLE_CROSSOVER"),

    # ---- back ----
    "lat_pulldown": (
        "LAT_PULLDOWN", "KNEELING_LAT_PULLDOWN",
        "WIDE_GRIP_LAT_PULLDOWN", "CLOSE_GRIP_LAT_PULLDOWN",
    ),
    "pull_up": ("WEIGHTED_PULL_UP", "PULL_UP", "CHIN_UP"),
    "row": ("CABLE_ROW_STANDING", "BARBELL_ROW", "DUMBBELL_ROW"),
    "face_pull": ("FACE_PULL",),

    # ---- shoulders ----
    "overhead_press": ("OVERHEAD_BARBELL_PRESS", "OVERHEAD_DUMBBELL_PRESS"),
    "lateral_raise": ("ONE_ARM_CABLE_LATERAL_RAISE", "DUMBBELL_LATERAL_RAISE"),
    "front_raise": ("FRONT_RAISE", "CABLE_FRONT_RAISE"),
    "rear_delt_flye": (
        "KNEELING_REAR_FLYE", "INCLINE_REVERSE_FLYE",
        "SINGLE_ARM_STANDING_CABLE_REVERSE_FLYE",
    ),
    "external_rotation": ("CABLE_EXTERNAL_ROTATION",),

    # ---- arms ----
    "biceps_curl": (
        "DUMBBELL_BICEPS_CURL", "BARBELL_BICEPS_CURL",
        "STANDING_EZ_BAR_BICEPS_CURL", "DEAD_HANG_BICEPS_CURL",
    ),
    "hammer_curl": ("DUMBBELL_HAMMER_CURL",),
    "wrist_curl": ("DUMBBELL_WRIST_CURL", "BARBELL_REVERSE_WRIST_CURL"),
    "overhead_triceps_extension": (
        "CABLE_OVERHEAD_TRICEPS_EXTENSION",
        "OVERHEAD_DUMBBELL_TRICEPS_EXTENSION",
        "SEATED_DUMBBELL_OVERHEAD_TRICEPS_EXTENSION",
        "SINGLE_ARM_DUMBBELL_OVERHEAD_TRICEPS_EXTENSION",
    ),
    "triceps_kickback": ("CABLE_KICKBACK",),
    "dip": ("BODY_WEIGHT_DIP", "WEIGHTED_DIP"),

    # ---- legs ----
    "squat": ("WEIGHTED_SQUAT", "BARBELL_SQUAT", "FRONT_SQUAT"),
    "deadlift": ("BARBELL_DEADLIFT", "ROMANIAN_DEADLIFT"),
    "leg_extension": ("WEIGHTED_LEG_EXTENSIONS", "LEG_EXTENSION"),
    "leg_curl": ("LEG_CURL", "SEATED_LEG_CURL"),
    "calf_raise": ("CALF_RAISE", "STANDING_CALF_RAISE"),

    # ---- core ----
    "cable_crunch": ("CABLE_CRUNCH",),
    "sit_up": ("SIT_UP", "WEIGHTED_SIT_UP"),
    "hanging_leg_raise": ("HANGING_LEG_RAISE",),
    "plank": (
        "PLANK", "SIDE_PLANK", "SIDE_PLANK_LIFT",
        "SIDE_PLANK_WITH_REACH_UNDER", "PLANK_WITH_OBLIQUE_CRUNCH",
    ),
    "chop": ("CHOP", "CABLE_CHOP"),
    "hip_raise": ("HIP_RAISE", "GLUTE_BRIDGE"),
}

VARIANT_TO_FAMILY: dict[str, str] = {
    variant: family
    for family, variants in FAMILY_VARIANTS.items()
    for variant in variants
}

# When a Garmin category has no labelled variant to go on, fall back to this
# family for its unlabelled sets. Only categories where the fallback is
# unambiguous are listed; anything else keeps its own category-level family.
CATEGORY_DEFAULT_FAMILY: dict[str, str] = {
    "bench_press": "flat_bench_press",
    "curl": "biceps_curl",
    "squat": "squat",
    "deadlift": "deadlift",
    "shoulder_press": "overhead_press",
    "lateral_raise": "lateral_raise",
    "crunch": "cable_crunch",
    "sit_up": "sit_up",
    "leg_raise": "hanging_leg_raise",
    "plank": "plank",
    "triceps_extension": "overhead_triceps_extension",
    "row": "row",
    "flye": "chest_flye",
    "pull_up": "pull_up",
    "shoulder_stability": "external_rotation",
}

# How a variant label was arrived at.
SOURCE_LABELLED = "labelled"   # Garmin's own exercise_name, user-reviewed
SOURCE_INFERRED = "inferred"   # assigned here from weight, see infer_variants
SOURCE_UNKNOWN = "unknown"     # no label and no confident inference


def family_for(variant: str | None, exercise: str) -> str:
    """Family for a variant, falling back to the category's default."""
    if variant and variant in VARIANT_TO_FAMILY:
        return VARIANT_TO_FAMILY[variant]
    return CATEGORY_DEFAULT_FAMILY.get(exercise, exercise)


def _weight_profiles(labelled: pd.DataFrame) -> dict[str, tuple[float, float]]:
    """Median and spread of weight for each labelled variant.

    Spread uses the MAD (scaled to be comparable to a standard deviation) so
    one mis-logged set doesn't widen a variant's profile.
    """
    profiles: dict[str, tuple[float, float]] = {}
    for variant, group in labelled.groupby("variant"):
        weights = group["weight_lb"].dropna()
        weights = weights[weights > 0]
        if len(weights) < 5:
            continue
        median = float(weights.median())
        mad = float((weights - median).abs().median()) * 1.4826
        scale = mad if mad > 1.0 else max(float(weights.std() or 0.0), 1.0)
        profiles[variant] = (median, scale)
    return profiles


def infer_variants(
    detail: pd.DataFrame,
    *,
    max_z: float = 2.0,
    min_separation: float = 1.5,
) -> pd.DataFrame:
    """Guess a variant for unlabelled sets from how much they lift.

    Within a family, variants are usually well separated by load -- in this
    account a dumbbell curl sits near 25 lb against 60 lb for barbell and
    EZ-bar, and a dumbbell bench near 55 lb against roughly 135 lb for
    barbell. So an unlabelled set can often be attributed by asking which
    labelled variant's weight profile it looks like.

    This is explicitly a guess. Every assigned row is marked
    `variant_source = "inferred"`, and a set is only assigned when it is both
    close to the best-matching variant (within `max_z` robust deviations) and
    clearly closer to it than to the runner-up (by `min_separation`).
    Anything ambiguous is left as `unknown` rather than forced.

    Expects `exercise`, `exercise_name` and `weight_lb` columns; returns the
    frame with `variant`, `family` and `variant_source` added.
    """
    out = detail.copy()
    name = out["exercise_name"] if "exercise_name" in out.columns else pd.Series(None, index=out.index)
    # object dtype explicitly: an all-missing name column comes back float64,
    # and assigning strings into it later raises a pandas dtype warning.
    out["variant"] = name.where(name.notna() & (name.astype(str).str.strip() != ""), None).astype(object)
    out["variant_source"] = np.where(out["variant"].notna(), SOURCE_LABELLED, SOURCE_UNKNOWN)
    out["family"] = [family_for(v, e) for v, e in zip(out["variant"], out["exercise"], strict=False)]

    # Pass 1: propagate labels within a session. Confirmed by the user that
    # implements aren't mixed within a session for the same movement ("I
    # don't think I ever mixed dumbbell with barbell variants of the same
    # exercise within the same session"), so one labelled set identifies the
    # whole session's work on that family. This runs for every family,
    # independent of whether enough labelled sets exist to build weight
    # profiles -- a single labelled set is enough to name its own session.
    for (_activity_id, _family), session in out.groupby(["activity_id", "family"], sort=False):
        unlabelled_idx = session.index[session["variant_source"] == SOURCE_UNKNOWN]
        if len(unlabelled_idx) == 0:
            continue
        session_labels = session.loc[
            session["variant_source"] == SOURCE_LABELLED, "variant"
        ].dropna().unique()
        if len(session_labels) == 1:
            out.loc[unlabelled_idx, "variant"] = session_labels[0]
            out.loc[unlabelled_idx, "variant_source"] = SOURCE_INFERRED

    # Pass 2: for sessions with nothing labelled at all, match the session's
    # heaviest set against the labelled variants' weight profiles.
    for family, group in out.groupby("family"):
        labelled = group[group["variant_source"] == SOURCE_LABELLED]
        profiles = _weight_profiles(labelled)
        if len(profiles) < 2:
            # With one labelled variant there's nothing to choose *between*,
            # but "only option" is not the same as "right option": the true
            # variant may simply never have been labelled. Bench press is
            # exactly that case here -- the only labelled variant is the 55 lb
            # dumbbell press, while 1620 unlabelled sets sit at 135 lb
            # (barbell, warming up from a 45 lb empty bar). Assigning them to
            # the dumbbell profile on "only option" grounds mislabelled all of
            # them, so plausibility is still required.
            if len(profiles) == 1:
                only = next(iter(profiles))
                median, scale = profiles[only]
                # Judge by session, on the session's heaviest set -- the same
                # rule the multi-profile branch below uses, and for the same
                # reason. Testing sets individually here matched *warmups*:
                # bench press has only one labelled variant (the 55 lb
                # dumbbell press), so every 45 lb empty-bar warmup opening a
                # 135-160 lb barbell session landed within tolerance of the
                # dumbbell profile and was labelled dumbbell work, while the
                # heavy sets around it stayed unknown. That invented 101
                # dumbbell bench "sessions" across 2024-2026, none of which
                # happened.
                for _activity_id, session in group.groupby("activity_id", sort=False):
                    unlabelled_idx = session.index[session["variant_source"] == SOURCE_UNKNOWN]
                    if len(unlabelled_idx) == 0:
                        continue
                    if (session["variant_source"] == SOURCE_LABELLED).any():
                        continue
                    weights = session.loc[unlabelled_idx, "weight_lb"].dropna()
                    weights = weights[weights > 0]
                    if weights.empty:
                        continue
                    if abs(float(weights.max()) - median) / scale <= max_z:
                        out.loc[unlabelled_idx, "variant"] = only
                        out.loc[unlabelled_idx, "variant_source"] = SOURCE_INFERRED
            continue

        # Attribute per session, not per set. Within one workout an exercise
        # is essentially always the same implement, and warmups follow the
        # working sets -- so a 45 lb set means "empty barbell warmup" if the
        # session's top set was 135 lb barbell, not "dumbbell press". Judging
        # sets individually mislabelled exactly that: 133 bench sets at a
        # median of 45 lb were being read as dumbbell work.
        for activity_id, session in group.groupby("activity_id", sort=False):
            unlabelled_idx = session.index[session["variant_source"] == SOURCE_UNKNOWN]
            if len(unlabelled_idx) == 0:
                continue

            # Anything labelled was already handled in pass 1; a session with
            # two different labelled variants is genuinely mixed (rare) and is
            # left alone rather than guessed at.
            if (session["variant_source"] == SOURCE_LABELLED).any():
                continue

            # Judge the session by its heaviest set, which is the one that
            # actually characterises the implement.
            weights = session.loc[unlabelled_idx, "weight_lb"].dropna()
            weights = weights[weights > 0]
            if weights.empty:
                continue
            top = float(weights.max())
            scored = sorted(
                ((abs(top - med) / scale, variant) for variant, (med, scale) in profiles.items()),
            )
            best_z, best_variant = scored[0]
            runner_up_z = scored[1][0] if len(scored) > 1 else float("inf")
            if best_z <= max_z and (runner_up_z - best_z) >= min_separation:
                out.loc[unlabelled_idx, "variant"] = best_variant
                out.loc[unlabelled_idx, "variant_source"] = SOURCE_INFERRED

    # Anything still unassigned keeps a stable per-category placeholder so it
    # can be charted and reviewed rather than silently dropped.
    still_unknown = out["variant"].isna()
    out.loc[still_unknown, "variant"] = out.loc[still_unknown, "exercise"] + "__unassigned"
    return out
