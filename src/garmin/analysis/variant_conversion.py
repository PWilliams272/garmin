"""Empirical conversion factors between variants of the same movement.

The question this answers is "is my bench progressing?", not "how is my
dumbbell bench doing?". Those are different questions, and until now only the
second was answerable: dumbbell and barbell bench are the same movement
pattern at very different absolute loads, so putting them on one axis makes a
change of implement look like a change of strength.

Combining them needs a conversion, and the conversion is *estimated*, not
assumed. There is no reliable textbook multiplier -- published dumbbell:
barbell ratios vary widely and none of them are about this person -- but the
data can supply one, because the same underlying strength is being measured
through two instruments over an overlapping period.

The model
---------
For each family, per session date `t` and variant `v`, using that session's
best estimated 1RM:

    log(e1RM) = level(t) + offset[v] + noise

`level(t)` is the family's underlying strength, shared by every variant and
piecewise-linear between monthly knots. `offset[v]` is what that variant reads
relative to the reference variant. Both come out of one linear least-squares
fit, so the offsets are exactly the systematic gap that remains after
allowing for strength changing over time.

`exp(offset[v])` is then the conversion factor: multiply that variant's
weights by it to express them on the reference variant's scale.

What this does *not* do
-----------------------
It assumes the ratio is constant over the period fitted. A single number per
variant is the right place to start -- it is identifiable, easy to check, and
if the residuals show structure (say the ratio drifting as loads get heavier)
that is visible evidence for something more elaborate, rather than a guess.
The fit reports its own residual spread so that check is possible.

Identifiability depends on overlap. Two variants trained in disjoint periods
have no direct evidence linking them; they are only connected if some third
variant overlaps both. Families whose variants never overlap are reported as
unidentified rather than given a fabricated number.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# Spacing of the level knots, in days. Monthly matches the strength-curve
# model, and is slow enough that a variant offset can't be absorbed into the
# level by wiggling it.
LEVEL_KNOT_DAYS = 30.0

# A variant needs at least this many sessions before it gets an offset.
MIN_SESSIONS_PER_VARIANT = 8

# Below this many days of overlap with the rest of the family, a variant's
# offset is not considered identified.
MIN_OVERLAP_DAYS = 45


def _level_basis(days: np.ndarray, knot_days: float = LEVEL_KNOT_DAYS) -> np.ndarray:
    """Piecewise-linear (hat function) basis for a smooth level over time."""
    span = float(days.max() - days.min()) if len(days) else 0.0
    n_knots = max(2, int(np.ceil(span / knot_days)) + 1)
    knots = np.linspace(days.min(), days.max(), n_knots)
    basis = np.zeros((len(days), n_knots))
    for k in range(n_knots):
        left = knots[k - 1] if k > 0 else knots[0] - 1
        right = knots[k + 1] if k < n_knots - 1 else knots[-1] + 1
        rising = (days - left) / max(knots[k] - left, 1e-9)
        falling = (right - days) / max(right - knots[k], 1e-9)
        basis[:, k] = np.clip(np.minimum(rising, falling), 0, 1)
    return basis


PAIR_WINDOW_DAYS = 14

# How far the regression factor and the independent paired-session estimate may
# diverge before the factor is treated as unreliable. Expressed as a ratio, so
# 1.25 means "within 25% of each other in either direction".
AGREEMENT_TOLERANCE = 1.25

# Residual spread (in log units) above which a variant looks like a mixture of
# implements rather than one. 0.35 in log space is roughly a 1.4x spread --
# comfortably wider than session-to-session variation in a single lift, but
# well below the gap between two different implements.
MAX_VARIANT_SD = 0.35


# Suffix analysis_pipeline gives a variant when Garmin recorded the exercise
# category but no specific variant -- "bench_press__unassigned" and friends.
CATCHALL_SUFFIX = "__unassigned"


def _is_catchall(variant: str) -> bool:
    """Whether this 'variant' is really the absence of a label."""
    return str(variant).endswith(CATCHALL_SUFFIX)


def _paired_ratio(
    variant_sessions: pd.DataFrame,
    reference_sessions: pd.DataFrame,
    window_days: int = PAIR_WINDOW_DAYS,
) -> tuple[float, int]:
    """Independent estimate of the conversion, for cross-checking the fit.

    Rather than modelling the level over time, this pairs each session of the
    variant with the reference sessions within a couple of weeks either side --
    close enough that underlying strength has barely moved -- and takes the
    median ratio. It uses far less data and says nothing about trend, so it is
    not a replacement for the regression. But it fails in completely different
    ways, which is what makes it worth having: where the two agree, the factor
    is real rather than an artefact of the level basis soaking up the offset.
    """
    ratios = []
    window = pd.Timedelta(days=window_days)
    for _, row in variant_sessions.iterrows():
        near = reference_sessions[(reference_sessions["date"] - row["date"]).abs() <= window]
        if len(near) and row["est_1rm"] > 0:
            ratios.append(near["est_1rm"].median() / row["est_1rm"])
    if not ratios:
        return float("nan"), 0
    return float(np.median(ratios)), len(ratios)


# How far a variant's offset may shift between the first and second half of
# its own history before it is treated as covering more than one exercise.
# 0.15 in log units is ~16%: comfortably more than a lift drifting relative to
# its family, far less than the gap between two different movements.
MAX_ERA_SHIFT = 0.15

# An era needs this many sessions before its mean residual means anything.
MIN_SESSIONS_PER_ERA = 6


def _era_shift(dates: pd.Series, residuals: np.ndarray) -> float:
    """How much a variant's offset moves between the early and late halves of
    its own history.

    Catches what the overall spread check cannot: a label that covered one
    exercise for a year and a different one afterwards. Within each era it
    looks perfectly consistent, so its residual spread stays small and the
    two central estimates still agree -- but the single factor fitted to it
    is wrong in both halves.

    Real case: this account's unlabelled triceps-extension bucket sits at
    ~50 lb before late 2024 and ~42 lb after, matching whatever labelled
    variant was in use at the time. Folding it in on one factor is what made
    that movement's combined series visibly jumpier than every other lift.

    Returns 0.0 when either era is too small to judge, so a thin variant is
    not condemned on noise.
    """
    if len(residuals) < 2 * MIN_SESSIONS_PER_ERA:
        return 0.0
    order = np.argsort(dates.to_numpy())
    ordered = residuals[order]
    midpoint = len(ordered) // 2
    early, late = ordered[:midpoint], ordered[midpoint:]
    if len(early) < MIN_SESSIONS_PER_ERA or len(late) < MIN_SESSIONS_PER_ERA:
        return 0.0
    return float(abs(np.mean(late) - np.mean(early)))


def _agrees(factor: float, paired: float, tolerance: float = AGREEMENT_TOLERANCE) -> bool:
    if not np.isfinite(paired) or paired <= 0 or factor <= 0:
        return False
    ratio = factor / paired
    return bool(1.0 / tolerance <= ratio <= tolerance)


def _overlap_days(dates: pd.Series, others: pd.Series) -> float:
    if dates.empty or others.empty:
        return 0.0
    lo = max(dates.min(), others.min())
    hi = min(dates.max(), others.max())
    return max(0.0, (hi - lo).days)


def fit_variant_conversions(
    sessions: pd.DataFrame,
    *,
    min_sessions: int = MIN_SESSIONS_PER_VARIANT,
    min_overlap_days: int = MIN_OVERLAP_DAYS,
    max_variant_sd: float = MAX_VARIANT_SD,
    max_era_shift: float = MAX_ERA_SHIFT,
) -> pd.DataFrame:
    """Estimate each variant's multiplicative offset within its family.

    `sessions` needs `family`, `variant`, `date` and `est_1rm` columns, one
    row per session per variant (the session's best estimated 1RM).

    Returns one row per variant with `factor` -- multiply that variant's
    weights by it to put them on the reference variant's scale -- plus the
    reference it is relative to, the overlap backing it, and the fit's
    residual spread.
    """
    rows: list[dict] = []
    for family, group in sessions.groupby("family"):
        group = group.dropna(subset=["est_1rm", "date"])
        group = group[group["est_1rm"] > 0]
        counts = group["variant"].value_counts()
        keep = counts[counts >= min_sessions].index
        group = group[group["variant"].isin(keep)]
        if group["variant"].nunique() == 0:
            continue

        # The anchor must be a *labelled* variant, never an unlabelled
        # catch-all, even when the catch-all has more sessions.
        #
        # The reference is pinned to offset 0, so anything wrong with it is
        # absorbed into the family's level and reappears as "strength". This
        # account's unlabelled triceps-extension bucket is exactly that: it
        # tracks ~50 lb work before late 2024 and ~42 lb after, following
        # whichever labelled variant was in use. As the reference it looked
        # perfectly stable (era shift 0.001) and quietly injected a 20%
        # step into the movement's trend. Anchoring on a real label makes the
        # same bucket testable like any other variant.
        variants = sorted(group["variant"].unique())
        ranked = counts[keep].sort_values(ascending=False)
        labelled = [v for v in ranked.index if not _is_catchall(v)]
        reference = labelled[0] if labelled else (ranked.index[0] if len(ranked) else None)

        if len(variants) == 1:
            # Every column the multi-variant branch produces has to appear here
            # too. A missing key becomes NaN once these rows are assembled into
            # a frame, and NaN is not valid JSON -- which silently breaks the
            # whole payload for any page that reads it.
            rows.append({
                "family": family, "variant": variants[0], "reference": variants[0],
                "factor": 1.0, "n_sessions": int(counts[variants[0]]),
                "overlap_days": 0.0, "residual_sd": 0.0,
                "paired_ratio": 1.0, "n_pairs": int(counts[variants[0]]),
                "agrees": True, "variant_sd": 0.0, "homogeneous": True,
                "era_shift": 0.0, "stable": True,
                "identified": True,
            })
            continue

        days = (group["date"] - group["date"].min()).dt.days.to_numpy(dtype=float)
        level = _level_basis(days)
        others = [v for v in variants if v != reference]
        dummies = np.column_stack([(group["variant"] == v).to_numpy(float) for v in others])
        design = np.column_stack([level, dummies])
        target = np.log(group["est_1rm"].to_numpy(dtype=float))

        # Apple's Accelerate BLAS emits spurious divide/overflow warnings on
        # these solves; outputs verified finite.
        with np.errstate(all="ignore"):
            coef, *_ = np.linalg.lstsq(design, target, rcond=None)
            residual_sd = float(np.std(target - design @ coef))

        offsets = dict(zip(others, coef[level.shape[1]:], strict=True))
        with np.errstate(all="ignore"):
            residuals = target - design @ coef
        ref_sessions = group[group["variant"] == reference]
        variant_col = group["variant"].to_numpy()
        for variant in variants:
            v_sessions = group[group["variant"] == variant]
            rest = group.loc[group["variant"] != variant, "date"]
            overlap = _overlap_days(v_sessions["date"], rest)
            offset = 0.0 if variant == reference else float(offsets[variant])
            # exp(-offset) converts *this* variant onto the reference's scale:
            # a variant reading systematically lower needs scaling up to match.
            factor = float(np.exp(-offset))

            if variant == reference:
                paired, n_pairs, agrees = 1.0, len(ref_sessions), True
            else:
                paired, n_pairs = _paired_ratio(v_sessions, ref_sessions)
                agrees = _agrees(factor, paired)

            # A label covering more than one implement can't have a single
            # factor. Its sets then scatter far more widely around the fitted
            # line than a genuine variant's do, which is the signal that no one
            # number will do -- two central estimates can happily agree on the
            # midpoint of a mixture, so spread catches what agreement misses.
            mask = variant_col == variant
            v_resid = residuals[mask]
            v_sd = float(np.std(v_resid)) if len(v_resid) > 1 else 0.0
            homogeneous = v_sd <= max_variant_sd

            # ...and consistent over time, not just on average.
            era_shift = _era_shift(v_sessions["date"], v_resid)
            stable = era_shift <= max_era_shift

            rows.append({
                "family": family,
                "variant": variant,
                "reference": reference,
                "factor": round(factor, 4),
                "n_sessions": int(counts[variant]),
                "overlap_days": round(overlap, 1),
                "residual_sd": round(residual_sd, 4),
                "paired_ratio": None if not np.isfinite(paired) else round(paired, 4),
                "n_pairs": int(n_pairs),
                "agrees": bool(agrees),
                "variant_sd": round(v_sd, 4),
                "homogeneous": bool(homogeneous),
                "era_shift": round(era_shift, 4),
                "stable": bool(stable),
                # A factor is only trusted when it has enough concurrent data,
                # an independent estimate lands in the same place, and the
                # variant behaves like one implement rather than several.
                "identified": bool(
                    variant == reference
                    or (overlap >= min_overlap_days and agrees and homogeneous and stable)
                ),
            })

    result = pd.DataFrame(rows)
    return _rescale_to_display_units(result)


def _rescale_to_display_units(conversions: pd.DataFrame) -> pd.DataFrame:
    """Express each family on the scale of its most-trained usable variant.

    Fitting anchors on a labelled variant for the identifiability reasons
    above, but that is not always the one whose numbers are familiar -- for
    bench press it makes dumbbell work the anchor, so the whole series would
    read at ~70 lb rather than the ~200 lb of the barbell work that dominates
    it. Rescaling afterwards is free: the factors are all relative, so
    dividing through by one of them changes the units without touching any of
    the evidence or which variants passed their checks.
    """
    if conversions.empty:
        return conversions

    out = []
    for _family, group in conversions.groupby("family", sort=False):
        # Every branch must set display_variant. A column left off one branch
        # becomes NaN in the concatenated frame, and NaN is not valid JSON --
        # the same failure mode the single-variant branch already hit once.
        usable = group[group["identified"]]
        if usable.empty:
            group = group.copy()
            group["display_variant"] = group["reference"]
            out.append(group)
            continue
        display = usable.loc[usable["n_sessions"].idxmax()]
        scale = float(display["factor"])
        if scale <= 0:
            group = group.copy()
            group["display_variant"] = group["reference"]
            out.append(group)
            continue
        group = group.copy()
        group["factor"] = (group["factor"] / scale).round(4)
        group["paired_ratio"] = (group["paired_ratio"] / scale).round(4)
        group["display_variant"] = display["variant"]
        out.append(group)
    return pd.concat(out, ignore_index=True)


def session_bests(detail: pd.DataFrame) -> pd.DataFrame:
    """Collapse set-level detail to one best estimated 1RM per session per
    variant -- the input `fit_variant_conversions` expects."""
    from garmin.analysis.session_quality import capacity_sessions
    from garmin.prototypes.activity_explorer import blended_1rm

    empty = pd.DataFrame(columns=["family", "variant", "date", "est_1rm"])
    # An empty store hands over a frame with only a few columns, so check
    # before selecting rather than letting dropna raise on the missing ones.
    required = {"family", "variant", "date", "reps", "weight_lb"}
    if detail.empty or not required.issubset(detail.columns):
        return empty

    df = detail.dropna(subset=["reps", "weight_lb", "date"]).copy()
    df = df[(df["weight_lb"] > 0) & (df["reps"] > 0)]
    if df.empty:
        return empty
    df["est_1rm"] = [
        blended_1rm(w, r) for w, r in zip(df["weight_lb"], df["reps"], strict=False)
    ]
    sessions = (
        df.groupby(["family", "variant", "date"], as_index=False)
        .agg(est_1rm=("est_1rm", "max"), n_sets=("est_1rm", "size"))
        .sort_values(["family", "variant", "date"])
    )
    # Calibrating a conversion on sessions that never approached failure
    # skews it: an abandoned or deliberately easy day looks like that variant
    # reads low, which is exactly what a conversion factor is supposed to
    # measure. Judge submaximality per *variant*, since variants sit at
    # genuinely different loads.
    return capacity_sessions(
        sessions, group_columns=("family", "variant")
    ).drop(columns=["local_level", "shortfall", "submaximal"], errors="ignore")
