"""Builds the evidence behind the modeling work into one JSON payload.

Everything the Modeling tab shows is computed here, offline, and written to
the viewer cache -- the web tier only ever reads the result, same rule as
every other page (see CLAUDE.md: routes never fit a model on request).

Each section corresponds to a decision made while building the strength and
wellness models, and carries the actual measurement that drove it rather
than a restatement of the conclusion.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from garmin.analysis.analysis_pipeline import (
    EXERCISE_LOAD_TYPES,
    LOAD_TYPE_EXTERNAL,
    STRENGTH_EXERCISE_CANDIDATES,
    _reviewed_strength_detail,
    load_type_for,
)
from garmin.io.curated_store import CuratedDataStore
from garmin.updaters import ACTIVITY_DATASETS

# Quantile the envelope regression targets -- matches strength_curve's
# default so the illustration reflects the real model's behaviour.
ENVELOPE_QUANTILE = 0.9

# Lags (days) to test training load against next-day wellness.
LOAD_LAGS = (0, 1, 2, 3)


# Below this many loaded sets, a correlation is too noisy to classify an
# exercise on. Real example: `chop` (17 sets) reads +0.51 and `hip_raise`
# (11 sets) +0.18, which look like assistance next to pull-up's +0.39 but
# are entirely consistent with zero once the interval is drawn.
MIN_SETS_FOR_RELIABLE_CORR = 30


def _corr_ci(r: float, n: int) -> tuple[float, float]:
    """95% CI for a correlation via the Fisher z transform."""
    if n < 4 or not np.isfinite(r) or abs(r) >= 1:
        return (float("nan"), float("nan"))
    z = np.arctanh(r)
    se = 1.0 / np.sqrt(n - 3)
    return (float(np.tanh(z - 1.96 * se)), float(np.tanh(z + 1.96 * se)))


def _load_type_evidence(detail: pd.DataFrame) -> list[dict]:
    """corr(weight, reps) per exercise -- the test that settles whether a
    'weight' column is real load or machine assistance.

    A genuine load makes reps fall as weight rises (negative). Assistance
    makes reps rise (positive). This is what identified pull-ups as assisted
    and therefore inverted in the old estimate.

    Each correlation carries a Fisher-z 95% interval and a `reliable` flag,
    because several rarely-trained exercises have single- or double-digit
    set counts where the point estimate alone is meaningless.
    """
    rows = []
    for exercise in STRENGTH_EXERCISE_CANDIDATES:
        ex_df = detail[detail["exercise"] == exercise].copy()
        ex_df["w"] = pd.to_numeric(ex_df["weight_lb"], errors="coerce")
        ex_df["r"] = pd.to_numeric(ex_df["reps"], errors="coerce")
        ex_df = ex_df.dropna(subset=["w", "r"])
        if ex_df.empty:
            continue
        loaded = ex_df[ex_df["w"] > 0]
        n_loaded = int(len(loaded))
        corr = float(loaded["w"].corr(loaded["r"])) if n_loaded > 10 else None
        if corr is not None and not np.isfinite(corr):
            corr = None
        lo, hi = _corr_ci(corr, n_loaded) if corr is not None else (float("nan"), float("nan"))
        rows.append({
            "exercise": exercise,
            "load_type": load_type_for(exercise),
            "n_sets": int(len(ex_df)),
            "n_loaded_sets": n_loaded,
            "n_zero_weight": int((ex_df["w"] == 0).sum()),
            "corr_weight_reps": None if corr is None else round(corr, 3),
            "corr_lo": None if not np.isfinite(lo) else round(lo, 3),
            "corr_hi": None if not np.isfinite(hi) else round(hi, 3),
            "reliable": corr is not None and n_loaded >= MIN_SETS_FOR_RELIABLE_CORR,
            "median_weight": None if loaded.empty else round(float(loaded["w"].median()), 1),
            # Drives the "which exercises can identify their own beta" chart.
            "rep_sd": None if len(ex_df) < 2 else round(float(ex_df["r"].std()), 2),
        })
    return rows


def _warmup_bias_evidence(detail: pd.DataFrame, exercise: str = "bench_press") -> dict | None:
    """The set cloud for one exercise plus two competing fits of
    log(weight) ~ -beta * log(reps).

    Shows why the model uses an asymmetric (upper-envelope) likelihood:
    ordinary least squares runs through the middle of a cloud that is mostly
    warmup and back-off work, so it reports a far steeper rep decay than is
    physically plausible.
    """
    ex_df = detail[detail["exercise"] == exercise].copy()
    ex_df["w"] = pd.to_numeric(ex_df["weight_lb"], errors="coerce")
    ex_df["r"] = pd.to_numeric(ex_df["reps"], errors="coerce")
    ex_df = ex_df.dropna(subset=["w", "r"])
    ex_df = ex_df[(ex_df["w"] > 0) & (ex_df["r"] > 0)]
    if len(ex_df) < 50:
        return None

    log_r = np.log(ex_df["r"].to_numpy(dtype=float))
    log_w = np.log(ex_df["w"].to_numpy(dtype=float))
    design = np.column_stack([np.ones(len(log_r)), log_r])

    ols = np.linalg.lstsq(design, log_w, rcond=None)[0]

    # Bayesian quantile regression's frequentist twin -- same upper-envelope
    # idea as the AsymmetricLaplace likelihood, without needing to sample.
    try:
        from statsmodels.regression.quantile_regression import QuantReg
        envelope = QuantReg(log_w, design).fit(q=ENVELOPE_QUANTILE).params
    except Exception:
        envelope = None

    def curve(params):
        reps = np.arange(1, 13)
        return {
            "reps": reps.tolist(),
            "weight": np.exp(params[0] + params[1] * np.log(reps)).round(1).tolist(),
            "beta": round(float(-params[1]), 3),
        }

    return {
        "exercise": exercise,
        "n_sets": int(len(ex_df)),
        "sets": {
            "reps": ex_df["r"].round(0).tolist(),
            "weight": ex_df["w"].round(1).tolist(),
        },
        "session_series": [
            {"date": d.date().isoformat(), "top_weight": round(float(w), 1)}
            for d, w in ex_df.groupby("date")["w"].max().items()
        ],
        "ols": curve(ols),
        "envelope": None if envelope is None else curve(envelope),
    }


# Quantiles to fit the envelope at, so the explainer can show how the choice
# of quantile moves the fitted curve through the real set cloud.
ENVELOPE_SWEEP = (0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.99)


def _envelope_sweep(detail: pd.DataFrame, exercise: str = "bench_press") -> dict | None:
    """Refit the load-rep line at a range of quantiles.

    Drives the interactive quantile slider: q=0.5 is ordinary median
    regression running through the middle of the warmup-heavy cloud, and
    raising q walks the line up toward the capacity envelope.
    """
    ex_df = detail[detail["exercise"] == exercise].copy()
    ex_df["w"] = pd.to_numeric(ex_df["weight_lb"], errors="coerce")
    ex_df["r"] = pd.to_numeric(ex_df["reps"], errors="coerce")
    ex_df = ex_df.dropna(subset=["w", "r"])
    ex_df = ex_df[(ex_df["w"] > 0) & (ex_df["r"] > 0)]
    if len(ex_df) < 50:
        return None

    log_r = np.log(ex_df["r"].to_numpy(dtype=float))
    log_w = np.log(ex_df["w"].to_numpy(dtype=float))
    design = np.column_stack([np.ones(len(log_r)), log_r])

    try:
        from statsmodels.regression.quantile_regression import QuantReg
    except Exception:
        return None

    reps = np.arange(1, 13)
    fits = []
    for q in ENVELOPE_SWEEP:
        try:
            params = QuantReg(log_w, design).fit(q=q).params
        except Exception:
            continue
        fits.append({
            "q": q,
            "beta": round(float(-params[1]), 3),
            "one_rm": round(float(np.exp(params[0])), 1),
            "weight": np.exp(params[0] + params[1] * np.log(reps)).round(1).tolist(),
        })
    if not fits:
        return None
    return {"exercise": exercise, "reps": reps.tolist(), "fits": fits}


def _tempo_quality(detail: pd.DataFrame) -> dict | None:
    """Seconds-per-rep distribution, and how much of its tail is the watch
    being left running rather than a genuinely slow set."""
    if detail.empty or "sec_per_rep" not in detail.columns:
        return None
    spr = pd.to_numeric(detail["sec_per_rep"], errors="coerce")
    usable = detail["duration_suspect"] == False  # noqa: E712
    kept = spr[usable].dropna()
    flagged = spr[detail["duration_suspect"] == True].dropna()  # noqa: E712
    if kept.empty:
        return None
    # Histogram of the plausible range; the tail is reported separately
    # because a handful of 100+ s/rep sets would flatten the whole chart.
    edges = list(np.arange(0, 21, 1.0))
    counts, _ = np.histogram(kept.clip(upper=20.5), bins=edges + [1e9])
    return {
        "bins": [float(e) for e in edges],
        "counts": [int(c) for c in counts[:len(edges)]],
        "n_total": int(spr.notna().sum()),
        "n_flagged": int(len(flagged)),
        "quantiles": {str(q): round(float(kept.quantile(q)), 1) for q in (0.5, 0.9, 0.99)},
        "max_flagged": None if flagged.empty else round(float(flagged.max()), 1),
    }


def _session_structure(detail: pd.DataFrame) -> dict | None:
    """How sessions are organised: straight sets, or alternating supersets.

    Matters because the neighbour-consensus rule that infers an ambiguous
    set's exercise assumes the set next door is the same movement. In a
    superset (bench, curl, bench, curl) it is not.
    """
    from garmin.analysis.analysis_pipeline import detect_session_stride

    if detail.empty or "set_number" not in detail.columns:
        return None
    ordered = detail.sort_values(["activity_id", "set_number"])
    counts = {"straight": 0, "superset": 0, "too_short": 0}
    by_year: dict[int, dict[str, int]] = {}
    for _activity_id, group in ordered.groupby("activity_id", sort=False):
        exercises = group["exercise"].tolist()
        year = int(pd.Timestamp(group["date"].iloc[0]).year)
        bucket = by_year.setdefault(year, {"straight": 0, "superset": 0})
        if len(exercises) < 6:
            counts["too_short"] += 1
            continue
        key = "superset" if detect_session_stride(exercises) == 2 else "straight"
        counts[key] += 1
        bucket[key] += 1

    return {
        "counts": counts,
        "by_year": [
            {"year": y, "straight": v["straight"], "superset": v["superset"]}
            for y, v in sorted(by_year.items())
        ],
    }


def _review_queue_status(store: CuratedDataStore) -> dict:
    review = store.load_exercise_review("strength")
    if review.empty:
        return {"counts": {}, "total": 0}
    counts = review["review_status"].value_counts().to_dict()
    return {
        "counts": {str(k): int(v) for k, v in counts.items()},
        "total": int(len(review)),
    }


def _daily_training_load(store: CuratedDataStore) -> pd.DataFrame:
    """One row per day: summed duration and an HR-weighted load proxy."""
    frames = []
    for dataset in ACTIVITY_DATASETS:
        summary = store.load_activity_summary(dataset)
        if summary.empty or "date" not in summary.columns:
            continue
        frame = summary[["date"]].copy()
        frame["duration_min"] = pd.to_numeric(summary.get("duration_min"), errors="coerce")
        frame["avg_hr"] = pd.to_numeric(summary.get("avg_hr"), errors="coerce")
        frames.append(frame)
    if not frames:
        return pd.DataFrame(columns=["date", "load"])

    activities = pd.concat(frames, ignore_index=True)
    activities["date"] = pd.to_datetime(activities["date"])
    # Duration x relative intensity. avg_hr is missing on many older/manually
    # logged sessions, so fall back to a neutral value rather than dropping
    # the session's duration entirely.
    activities["load"] = activities["duration_min"] * activities["avg_hr"].fillna(120) / 100
    return activities.groupby("date", as_index=False).agg(load=("load", "sum"))


def _wellness_lag_evidence(store: CuratedDataStore) -> dict | None:
    """Partial correlation of each training-load lag with next-day HRV,
    controlling for HRV's own lag-1 and 7-day baseline.

    HRV is strongly autocorrelated, so a raw correlation mostly measures
    "yesterday's HRV predicts today's". Residualising against that is what
    isolates any training signal. The reverse direction is reported too: if
    today's HRV predicted tomorrow's training, the forward estimate would be
    confounded by self-selection.
    """
    hrv = store.load_daily("hrv")
    if hrv.empty or "last_night_avg" not in hrv.columns:
        return None
    hrv = hrv[["date", "last_night_avg"]].dropna().copy()
    hrv["date"] = pd.to_datetime(hrv["date"])
    if len(hrv) < 100:
        return None

    daily = _daily_training_load(store)
    if daily.empty:
        return None

    index = pd.date_range(hrv["date"].min(), hrv["date"].max(), freq="D")
    df = pd.DataFrame({"date": index}).merge(hrv, on="date", how="left").merge(daily, on="date", how="left")
    df["load"] = df["load"].fillna(0.0).astype(float)
    df["hrv"] = pd.to_numeric(df["last_night_avg"], errors="coerce")
    for lag in LOAD_LAGS:
        df[f"load_lag{lag}"] = df["load"].shift(lag)
    df["load_7d"] = df["load"].rolling(7).mean()
    df["hrv_lag1"] = df["hrv"].shift(1)
    df["hrv_7d"] = df["hrv"].shift(1).rolling(7).mean()
    df["load_next"] = df["load"].shift(-1)

    needed = ["hrv", "hrv_lag1", "hrv_7d", "load_7d", "load_next"] + [f"load_lag{lag}" for lag in LOAD_LAGS]
    data = df.dropna(subset=needed)
    data = data[np.isfinite(data[needed]).all(axis=1)]
    if len(data) < 100:
        return None

    controls = np.column_stack([np.ones(len(data)), data["hrv_lag1"], data["hrv_7d"]])

    def residual(values: np.ndarray) -> np.ndarray:
        # Apple's Accelerate BLAS emits spurious divide/overflow warnings on
        # these well-conditioned solves; outputs verified finite and correct.
        with np.errstate(all="ignore"):
            return values - controls @ np.linalg.lstsq(controls, values, rcond=None)[0]

    hrv_resid = residual(data["hrv"].to_numpy(dtype=float))
    n = len(data)

    def partial(column: str) -> dict:
        r = float(np.corrcoef(hrv_resid, residual(data[column].to_numpy(dtype=float)))[0, 1])
        t = r * np.sqrt((n - 4) / max(1e-12, 1 - r ** 2))
        return {"r": round(r, 3), "t": round(float(t), 1)}

    lags = [{"label": f"Load, same day" if lag == 0 else f"Load, {lag}d earlier", **partial(f"load_lag{lag}")}
            for lag in LOAD_LAGS]
    lags.append({"label": "Load, 7d mean", **partial("load_7d")})

    # Reverse check, controlling for baseline HRV and yesterday's load.
    reverse_controls = np.column_stack([
        np.ones(len(data)), data["hrv_7d"], data["load_lag1"],
    ])

    def reverse_residual(values: np.ndarray) -> np.ndarray:
        with np.errstate(all="ignore"):
            return values - reverse_controls @ np.linalg.lstsq(reverse_controls, values, rcond=None)[0]

    rev_r = float(np.corrcoef(
        reverse_residual(data["hrv"].to_numpy(dtype=float)),
        reverse_residual(data["load_next"].to_numpy(dtype=float)),
    )[0, 1])
    rev_t = rev_r * np.sqrt((n - 4) / max(1e-12, 1 - rev_r ** 2))

    return {
        "n": int(n),
        "lags": lags,
        "reverse": {"r": round(rev_r, 3), "t": round(float(rev_t), 1)},
    }


def _data_coverage(store: CuratedDataStore) -> list[dict]:
    """Span and row count for each daily wellness signal and activity sport --
    what any model here actually has to work with."""
    rows = []
    for dataset in ["hrv", "sleep", "heart_rate", "body_battery", "stress", "respiration", "health_stats", "steps"]:
        df = store.load_daily(dataset)
        if df.empty or "date" not in df.columns:
            continue
        dates = pd.to_datetime(df["date"]).dropna()
        if dates.empty:
            continue
        rows.append({
            "name": dataset, "kind": "daily", "rows": int(len(df)),
            "start": dates.min().date().isoformat(), "end": dates.max().date().isoformat(),
        })
    for dataset in ACTIVITY_DATASETS:
        summary = store.load_activity_summary(dataset)
        if summary.empty or "date" not in summary.columns:
            continue
        dates = pd.to_datetime(summary["date"]).dropna()
        if dates.empty:
            continue
        rows.append({
            "name": dataset, "kind": "activity", "rows": int(len(summary)),
            "start": dates.min().date().isoformat(), "end": dates.max().date().isoformat(),
        })
    return rows


def _strength_curve_status(store: CuratedDataStore) -> dict:
    """Whether the sampled load-rep curves are present, and what they say."""
    fitted = []
    for exercise in STRENGTH_EXERCISE_CANDIDATES:
        if load_type_for(exercise) != LOAD_TYPE_EXTERNAL:
            continue
        curve = store.load_strength_curve("strength", exercise)
        if curve.empty:
            continue
        last = curve.sort_values("date").iloc[-1]
        entry = {"exercise": exercise, "date": pd.Timestamp(last["date"]).date().isoformat()}
        for target in (1, 5, 8):
            mean_col = f"e{target}rm_mean"
            if mean_col in curve.columns:
                entry[f"e{target}rm"] = round(float(last[mean_col]), 1)
                lo, hi = f"e{target}rm_lower_95", f"e{target}rm_upper_95"
                if lo in curve.columns and hi in curve.columns:
                    entry[f"e{target}rm_width"] = round(float(last[hi] - last[lo]), 1)
        fitted.append(entry)
    return {"fitted": fitted, "n_fitted": len(fitted)}


def _rep_distribution(detail: pd.DataFrame) -> dict:
    """Why a 1RM is an extrapolation here: where the reps actually sit."""
    reps = pd.to_numeric(detail["reps"], errors="coerce").dropna()
    reps = reps[(reps > 0) & (reps <= 30)]
    if reps.empty:
        return {}
    counts = reps.value_counts().sort_index()
    return {
        "reps": [int(r) for r in counts.index],
        "counts": [int(c) for c in counts.values],
        "p10": float(reps.quantile(0.10)),
        "p50": float(reps.quantile(0.50)),
        "p90": float(reps.quantile(0.90)),
    }


# Record of the convergence experiments actually run against this dataset,
# newest last. Kept here rather than in prose so the explainer page can plot
# it and so a later attempt can see what has already been ruled out.
CONVERGENCE_HISTORY = [
    {"label": "Per-session nodes, unbounded β", "rhat": 2.88, "scope": "11 exercises", "note": "~1400 latent parameters"},
    {"label": "+ monthly knot grid", "rhat": 2.19, "scope": "11 exercises", "note": "1400 → 480 nodes"},
    {"label": "+ β bounded physiologically", "rhat": 1.85, "scope": "11 exercises", "note": "β ∈ [0.03, 0.35]"},
    {"label": "+ β free only where reps vary", "rhat": 1.54, "scope": "11 exercises", "note": "needs rep sd ≥ 1.5"},
    {"label": "β fixed outright", "rhat": 2.10, "scope": "6 exercises", "note": "worse — rules β out as the cause"},
    {"label": "+ centered walk, tune 2500", "rhat": 1.01, "scope": "2 exercises", "note": "converged, but only at this scale"},
    {"label": "same settings, full dataset", "rhat": 1.22, "scope": "11 exercises", "note": "best full-scale run so far"},
    {"label": "bench press fitted alone", "rhat": 1.83, "scope": "1 exercise", "note": "worse than joint — pooling helps"},
]

# Where each piece of the approach comes from. The distinction matters: most
# of this is standard method applied to personal data, and only the small
# "bespoke" set was assembled here.
PROVENANCE = [
    {
        "name": "Epley / Brzycki / Lombardi 1RM formulas",
        "kind": "literature",
        "detail": "Epley (1985), Brzycki (1993), Lombardi (1989). Standard published load-rep "
                  "equations. Used as the baseline this model is measured against, and still "
                  "used directly by the simpler top-set estimate in analyze_lifting.",
    },
    {
        "name": "Power-law load-rep form  w = 1RM · r^(−β)",
        "kind": "literature-extended",
        "detail": "This is exactly Lombardi's power law, which fixes the exponent at 0.10. The "
                  "only change here is that β is estimated from data (and partially pooled "
                  "across exercises) rather than fixed at a published constant.",
    },
    {
        "name": "Bayesian quantile regression via Asymmetric Laplace",
        "kind": "literature",
        "detail": "Yu & Moyeed (2001). Standard device for fitting a conditional quantile in a "
                  "Bayesian model. Applied here to track the capacity envelope rather than the "
                  "middle of a warmup-heavy set cloud.",
    },
    {
        "name": "Gaussian random-walk latent level",
        "kind": "literature",
        "detail": "Standard local-level structural time series (Harvey 1989; Durbin & Koopman). "
                  "Same family as trend_sts.py already uses for health metrics.",
    },
    {
        "name": "Partial pooling / hierarchical priors",
        "kind": "literature",
        "detail": "Standard multilevel modelling (Gelman & Hill). Used so exercises with little "
                  "rep variation borrow the population rep-decay exponent.",
    },
    {
        "name": "r-hat convergence diagnostic",
        "kind": "literature",
        "detail": "Gelman & Rubin (1992), rank-normalised version Vehtari et al. (2021). The "
                  "1.01 threshold used here is theirs.",
    },
    {
        "name": "Fisher z interval for correlations",
        "kind": "literature",
        "detail": "Fisher (1915). Used to put honest intervals on the load-type correlations.",
    },
    {
        "name": "Acute:chronic workload ratio",
        "kind": "literature-contested",
        "detail": "Hulin et al. / Gabbett popularised it, but it has been substantially "
                  "criticised on methodological grounds (Impellizzeri et al., 2020) — spurious "
                  "correlation from shared terms, and sensitivity to window choice. Planned as "
                  "one input among several, not as a headline metric.",
    },
    {
        "name": "Fitness-fatigue impulse response (functional form)",
        "kind": "literature",
        "detail": "Banister, Calvert, Savage & Bach (1975); Morton, Fitz-Clarke & Banister "
                  "(1990). The idea that each training bout adds an impulse which decays "
                  "exponentially. Only the shape is borrowed here.",
    },
    {
        "name": "The within-session fatigue timescale (tau = 6 min)",
        "kind": "unsupported",
        "detail": "Not from any literature. Banister's fitted constants are about 42 days "
                  "(fitness) and 7 days (fatigue) -- weeks, not minutes, so they do not "
                  "transfer. The nearest measured within-session quantity is phosphocreatine "
                  "resynthesis, fast-component half-time about 30s (Harris et al. 1976), but a "
                  "tau that short would leave fatigue almost fully cleared by this account's "
                  "72-second median rest, showing no accumulation at all across a session. "
                  "360s was chosen to sit between those extremes and reproduce the qualitative "
                  "shape of a workout. It is a modelling choice, produces an index with no "
                  "physiological units, and should not be compared between people. Fitting it "
                  "against late-session load decline or next-day HRV would make it real.",
    },
    {
        "name": "corr(weight, reps) sign test for load type",
        "kind": "bespoke",
        "detail": "A pragmatic data-quality heuristic put together here, not a published method: "
                  "a real load makes reps fall as weight rises, machine assistance makes them "
                  "rise. Cheap, and it caught that pull-ups were being trended backwards.",
    },
    {
        "name": "Exercise-label review overlay + inference",
        "kind": "bespoke",
        "detail": "Repo-specific plumbing for correcting Garmin's own exercise classifier, with "
                  "user decisions stored separately so re-pulling never clobbers them. The "
                  "inference itself is weighted heuristics, not a trained model.",
    },
    {
        "name": "Variant conversion factors (dumbbell ↔ barbell)",
        "kind": "bespoke",
        "detail": "Published dumbbell:barbell ratios vary far too widely to borrow, so the "
                  "factors here are fitted from this account's own overlapping training "
                  "periods -- a shared time-varying level plus a per-variant offset. The "
                  "structure is ordinary fixed-effects regression; applying it to convert "
                  "lifting implements is a choice made here. Each factor is cross-checked "
                  "against an independent estimate from sessions within two weeks of each "
                  "other, and dropped when the two disagree.",
    },
    {
        "name": "This particular model assembly",
        "kind": "bespoke",
        "detail": "Combining a time-varying level, an estimated rep-decay exponent and an "
                  "envelope likelihood in one hierarchical fit is an assembly of standard parts "
                  "for this dataset — not a new method, and not validated beyond this account.",
    },
]


def _variant_conversion_evidence(detail: pd.DataFrame) -> dict:
    """Fitted conversion factors, plus the check that they actually help.

    The factors are only worth anything if converting genuinely removes the
    implement-driven steps from a family's series. That is measurable: compare
    how far the series scatters around its own 60-day rolling median before and
    after conversion. Where the factors are near 1 there is nothing to fix and
    the improvement should be ~0 -- which is as much a check on the method as
    the large improvements are.
    """
    from garmin.analysis.variant_conversion import fit_variant_conversions, session_bests

    sessions = session_bests(detail)
    if sessions.empty:
        return {"factors": [], "scatter": []}

    conversions = fit_variant_conversions(sessions)
    if conversions.empty:
        return {"factors": [], "scatter": []}

    trusted = conversions[conversions["identified"]]
    factors = dict(zip(trusted["variant"], trusted["factor"], strict=True))

    def scatter(frame: pd.DataFrame) -> float:
        frame = frame.sort_values("date").set_index("date")
        rolling = frame["est_1rm"].rolling("60D").median()
        with np.errstate(all="ignore"):
            return float(np.std(np.log(frame["est_1rm"] / rolling).dropna()))

    rows = []
    usable = sessions[sessions["variant"].isin(factors)]
    for family, group in usable.groupby("family"):
        if group["variant"].nunique() < 2:
            continue
        converted = group.copy()
        converted["est_1rm"] = converted["est_1rm"] * converted["variant"].map(factors)
        raw_sd, conv_sd = scatter(group), scatter(converted)
        if not (np.isfinite(raw_sd) and np.isfinite(conv_sd)) or raw_sd <= 0:
            continue
        rows.append({
            "family": family,
            "variants": int(group["variant"].nunique()),
            "raw_scatter": round(raw_sd, 4),
            "converted_scatter": round(conv_sd, 4),
            "improvement_pct": round(100.0 * (1.0 - conv_sd / raw_sd), 1),
        })

    return {
        # NaN is not valid JSON and takes down the whole page payload, and a
        # frame like this legitimately carries missing values (a variant with
        # no concurrent sessions has no cross-check ratio). Convert once here,
        # at the boundary, rather than forcing the frame to hold None.
        "factors": conversions.astype(object).where(conversions.notna(), None)
                   .to_dict(orient="records"),
        "scatter": sorted(rows, key=lambda r: -r["improvement_pct"]),
    }


def build_model_report(store: CuratedDataStore) -> dict:
    """Assemble every section of the Modeling tab payload."""
    detail = _reviewed_strength_detail(store)
    if detail.empty:
        detail = pd.DataFrame(columns=["exercise", "reps", "weight_lb"])

    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "load_types": _load_type_evidence(detail),
        "load_type_table": {k: v for k, v in sorted(EXERCISE_LOAD_TYPES.items())},
        "warmup_bias": _warmup_bias_evidence(detail),
        "envelope_sweep": _envelope_sweep(detail),
        "session_structure": _session_structure(detail),
        "tempo_quality": _tempo_quality(detail),
        "convergence_history": CONVERGENCE_HISTORY,
        "provenance": PROVENANCE,
        "rep_distribution": _rep_distribution(detail),
        "review_queue": _review_queue_status(store),
        "wellness_lags": _wellness_lag_evidence(store),
        "coverage": _data_coverage(store),
        "strength_curves": _strength_curve_status(store),
        "variant_conversion": _variant_conversion_evidence(detail),
    }
