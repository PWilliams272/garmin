"""Score Garmin's per-activity ``training_load`` against from-scratch HR formulas.

Garmin's "Exercise Load" is EPOC-derived and its exact formula is undocumented.
Anything downstream that treats it as a training-dose covariate is trusting a
black box, so this script rebuilds three published alternatives from the raw
per-sample heart-rate curves and asks how well each tracks Garmin's number:

1. **HR-above-resting integral** -- :math:`\\int \\max(HR - HR_{rest}, 0)\\,dt`.
   The naive dose: no intensity weighting at all.
2. **Banister TRIMP** -- duration weighted by an *exponential* function of
   heart-rate-reserve fraction, so hard minutes count disproportionately.
3. **Edwards zone-weighted TRIMP** -- time in each of five HR zones, weighted
   1..5 and summed.

Two design choices matter for fairness and are easy to get wrong:

- **Sample intervals are not 1 Hz.** Garmin's "smart recording" emits irregular
  samples (2-4 s gaps are common, and vary by sport). Every formula here
  integrates over the *actual* elapsed time per sample. Assuming 1 Hz would
  inflate the integral-style metrics for densely-sampled sports and deflate
  them for sparsely-sampled ones -- which would show up as a spurious
  *per-sport* difference, exactly the quantity this script exists to measure.
- **Edwards uses Garmin's own configured zone floors**, not textbook
  percentages of max HR, and Garmin stores a *separate* set for cycling. Using
  generic %HRmax boundaries would handicap Edwards for reasons that have
  nothing to do with the formula.

Correlations are reported three ways because they answer different questions.
Pearson on raw values is what a linear covariate would exploit; Pearson on
``log1p`` reflects that an EPOC-based quantity is roughly exponential in
intensity; Spearman is scale-free and answers "does it rank sessions the same
way", which is the honest headline when the functional form is unknown.

Pooled correlations across sports are reported but should not be the headline:
they are inflated by between-sport differences in typical duration and
intensity. The within-sport figure (sport means removed) is the number that
says whether a formula tracks Garmin *for comparable sessions*.

Run::

    python -m garmin.scripts.manual_validate_training_load --out report.json
"""

from dotenv import load_dotenv

load_dotenv()

import argparse  # noqa: E402
import json  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from garmin.io.curated_store import CuratedDataStore  # noqa: E402
from garmin.io.file_manager import FileManager  # noqa: E402

#: Sports carrying a Garmin ``training_load`` and a per-sample HR timeseries.
#: Maps the summary dataset to its detail (timeseries) dataset.
SPORT_DETAIL = {
    "running": "running_timeseries",
    "cycling": "cycling_timeseries",
    "indoor_cycling": "indoor_cycling_timeseries",
    "strength": "strength_timeseries",
    "bouldering": "bouldering_timeseries",
    "lap_swimming": "lap_swimming_timeseries",
    "hiking": "hiking_timeseries",
}

#: Sports Garmin configures a distinct HR-zone set for.
_CYCLING_SPORTS = {"cycling", "indoor_cycling"}

#: A gap longer than this is treated as a pause rather than elapsed training
#: time: HR during it is unobserved, so charging the full gap to the nearest
#: sample would invent load that never happened.
MAX_SAMPLE_GAP_S = 60.0

#: Edwards' zone weights, lowest zone first.
EDWARDS_WEIGHTS = (1, 2, 3, 4, 5)

#: Banister's male coefficients for the exponential HRR weighting.
BANISTER_A, BANISTER_B = 0.64, 1.92


def _as_float(value) -> float:
    """Float or NaN, preserving a genuine zero.

    ``float(x or nan)`` silently converts a measured 0.0 into missing, which
    for the training-effect scores would delete every session Garmin scored as
    having no anaerobic component at all.
    """
    return np.nan if value is None or pd.isna(value) else float(value)


def sample_durations(timestamps: pd.Series) -> np.ndarray:
    """Seconds of elapsed time each HR sample stands for.

    Args:
        timestamps: Sample times, ascending.

    Returns:
        Per-sample durations in seconds. Gaps beyond :data:`MAX_SAMPLE_GAP_S`
        are replaced by the median interval, so an auto-pause does not get
        counted as training time.
    """
    seconds = pd.to_datetime(timestamps).astype("int64").to_numpy() / 1e9
    if seconds.size < 2:
        return np.zeros(seconds.size)
    gaps = np.diff(seconds)
    # The typical interval must be estimated from *plausible* gaps only. Taking
    # the median over all gaps lets a dropout-heavy activity inflate its own
    # replacement value, which would quietly inflate its load -- the opposite of
    # what a dropout should do.
    plausible = gaps[(gaps > 0) & (gaps <= MAX_SAMPLE_GAP_S)]
    typical = float(np.median(plausible)) if plausible.size else 1.0
    gaps = np.where((gaps <= 0) | (gaps > MAX_SAMPLE_GAP_S), typical, gaps)
    # The final sample stands for one typical interval.
    return np.append(gaps, typical)


def hr_integral(hr: np.ndarray, dt_s: np.ndarray, resting_hr: float) -> float:
    """Integral of heart rate above resting, in bpm-minutes."""
    return float(np.sum(np.clip(hr - resting_hr, 0, None) * dt_s) / 60.0)


def banister_trimp(
    hr: np.ndarray, dt_s: np.ndarray, resting_hr: float, max_hr: float
) -> float:
    """Banister's TRIMP: minutes weighted by ``exp`` of HR-reserve fraction."""
    reserve = max(max_hr - resting_hr, 1.0)
    frac = np.clip((hr - resting_hr) / reserve, 0, None)
    return float(np.sum((dt_s / 60.0) * frac * BANISTER_A * np.exp(BANISTER_B * frac)))


def edwards_trimp(hr: np.ndarray, dt_s: np.ndarray, floors: tuple) -> float:
    """Edwards' zone-weighted TRIMP using explicit zone floors.

    Args:
        hr: Heart-rate samples.
        dt_s: Elapsed seconds per sample.
        floors: Five ascending zone floors in bpm. Time below the first floor
            contributes nothing.

    Returns:
        Weighted minutes.
    """
    minutes = dt_s / 60.0
    # searchsorted gives 0 below zone 1, then 1..5 for the five zones.
    zone = np.searchsorted(np.asarray(floors, dtype=float), hr, side="right")
    total = 0.0
    for index, weight in enumerate(EDWARDS_WEIGHTS, start=1):
        total += weight * float(np.sum(minutes[zone == index]))
    return total


def metrics_for_activity(detail: pd.DataFrame, resting_hr: float, max_hr: float,
                         floors: tuple) -> dict | None:
    """All three reconstructions for one activity, plus HR-quality diagnostics.

    Returns:
        Metric dict, or ``None`` when the activity carries no usable HR.
    """
    if detail.empty or "heart_rate_bpm" not in detail or "timestamp" not in detail:
        return None
    frame = detail[["timestamp", "heart_rate_bpm"]].dropna().sort_values("timestamp")
    if len(frame) < 10:
        return None
    hr = frame["heart_rate_bpm"].to_numpy(dtype=float)
    dt_s = sample_durations(frame["timestamp"])
    span_s = float(
        pd.to_datetime(detail["timestamp"]).max().timestamp()
        - pd.to_datetime(detail["timestamp"]).min().timestamp()
    )
    return {
        "integral": hr_integral(hr, dt_s, resting_hr),
        "banister": banister_trimp(hr, dt_s, resting_hr, max_hr),
        "edwards": edwards_trimp(hr, dt_s, floors),
        "hr_minutes": float(dt_s.sum() / 60.0),
        "hr_mean": float(hr.mean()),
        "hr_max": float(hr.max()),
        "n_samples": int(len(frame)),
        "median_gap_s": float(np.median(np.diff(
            pd.to_datetime(frame["timestamp"]).astype("int64").to_numpy() / 1e9
        ))) if len(frame) > 1 else np.nan,
        # Fraction of the wall-clock activity actually covered by HR samples.
        # Low values mean dropped samples or a strap losing contact.
        "hr_coverage": float(dt_s.sum() / span_s) if span_s > 0 else np.nan,
    }




def fit_effect_formula(table: pd.DataFrame, exponent: float | None = None) -> dict:
    """Recover the structural relation between ``training_load`` and Garmin's
    aerobic/anaerobic training-effect scores.

    Firstbeat's training-effect scores are a saturating 0-5 scale over each
    EPOC component, and Garmin's load is EPOC-derived, which motivates::

        training_load = C * [ (e^(k*aerobic_TE) - 1) + (e^(k*anaerobic_TE) - 1) ]

    Both components share one coefficient, because fitting them separately
    returns near-equal values (~13 and ~12) across sports.

    **This is not independent validation.** Both the training-effect scores and
    ``training_load`` are outputs of the same proprietary Firstbeat model, so a
    tight fit shows those outputs are mutually consistent -- it does not confirm
    either against physiology. The genuinely independent check is the
    HR-reconstruction comparison elsewhere in this module.

    Args:
        table: Per-activity table with ``training_load``, ``aerobic_te`` and
            ``anaerobic_te``.
        exponent: Fix ``k`` instead of fitting it. ``ln(2)`` makes each
            training-effect point exactly double that component's contribution.

    Returns:
        Fitted ``coefficient``, ``exponent``, pooled and per-sport R-squared.
    """
    frame = table[table["training_load"] > 0].dropna(
        subset=["aerobic_te", "anaerobic_te"])
    if frame.empty:
        return {}
    aerobic = frame["aerobic_te"].to_numpy(dtype=float)
    anaerobic = frame["anaerobic_te"].to_numpy(dtype=float)
    target = frame["training_load"].to_numpy(dtype=float)

    def basis_for(k):
        return np.expm1(k * aerobic) + np.expm1(k * anaerobic)

    def r_squared(actual, predicted):
        return 1.0 - float(np.sum((actual - predicted) ** 2)) / float(
            np.sum((actual - actual.mean()) ** 2))

    candidates = [exponent] if exponent else np.arange(0.30, 2.001, 0.005)
    best = max(
        ((k, float(basis_for(k) @ target / (basis_for(k) @ basis_for(k)))) for k in candidates),
        key=lambda pair: r_squared(target, pair[1] * basis_for(pair[0])),
    )
    k, coefficient = best
    predicted = coefficient * basis_for(k)
    report = {
        "exponent": round(float(k), 4),
        "coefficient": round(coefficient, 3),
        "doubling_factor": round(float(np.exp(k)), 3),
        "r2": round(r_squared(target, predicted), 4),
        "median_abs_pct_error": round(
            float(np.median(np.abs(predicted - target) / target) * 100), 1),
        "per_sport": {},
    }
    for sport, group in frame.assign(predicted=predicted).groupby("sport"):
        if len(group) < 10:
            continue
        actual = group["training_load"].to_numpy(dtype=float)
        fitted = group["predicted"].to_numpy(dtype=float)
        report["per_sport"][sport] = {
            "n": int(len(group)),
            "r2": round(r_squared(actual, fitted), 3),
            "median_abs_pct_error": round(
                float(np.median(np.abs(fitted - actual) / actual) * 100), 1),
        }
    return report


def _ols_cv_r2(X: np.ndarray, y: np.ndarray, folds: int = 5, seed: int = 0) -> tuple:
    """In-sample and k-fold cross-validated R^2 for an OLS fit with intercept.

    The cross-validated figure is the one to quote. Several sports here have
    n in the teens against three predictors, where in-sample R^2 rises simply
    because the model can bend to the points it was fitted on.

    Returns:
        ``(r2_in_sample, r2_cross_validated)``.
    """
    design = np.column_stack([np.ones(len(X)), X])
    coef, *_ = np.linalg.lstsq(design, y, rcond=None)
    resid = y - design @ coef
    total = float(np.sum((y - y.mean()) ** 2))
    r2_in = 1.0 - float(np.sum(resid ** 2)) / total if total > 0 else np.nan

    rng = np.random.default_rng(seed)
    order = rng.permutation(len(y))
    if len(y) < folds * 3:
        return round(r2_in, 3), None
    predictions = np.empty(len(y))
    for fold in np.array_split(order, folds):
        train = np.setdiff1d(order, fold)
        fit, *_ = np.linalg.lstsq(design[train], y[train], rcond=None)
        predictions[fold] = design[fold] @ fit
    r2_cv = 1.0 - float(np.sum((y - predictions) ** 2)) / total
    return round(r2_in, 3), round(r2_cv, 3)


#: Candidate models. Each maps a label to the predictor columns it uses.
#: ``recon`` is the HR-only reconstruction; ``te`` is Garmin's own two
#: training-effect scores, which use information beyond the HR curve.
NESTED_MODELS = {
    "recon_only": ["log_edwards"],
    "te_only": ["aerobic_te", "anaerobic_te"],
    "recon_plus_te": ["log_edwards", "aerobic_te", "anaerobic_te"],
}


def nested_model_report(table: pd.DataFrame) -> dict:
    """Does adding Garmin's training-effect scores close the reconstruction gap?

    Models ``log(training_load)``, because Garmin's load is roughly exponential
    in intensity while the training-effect scores are already log-like (a
    saturating 0-5 scale), so this is the scale on which the three are
    structurally comparable.

    Returns:
        Per-sport and pooled R^2 for each model in :data:`NESTED_MODELS`, plus
        the residual diagnostics after the fullest model.
    """
    frame = table.copy()
    frame = frame[(frame["training_load"] > 0) & (frame["edwards"] > 0)]
    frame["log_load"] = np.log(frame["training_load"])
    frame["log_edwards"] = np.log(frame["edwards"])
    needed = ["log_load", "log_edwards", "aerobic_te", "anaerobic_te",
              "duration_min", "hr_coverage"]
    frame = frame.dropna(subset=needed[:4])

    report: dict = {"per_sport": {}, "pooled_within_sport": {}}
    for sport, group in frame.groupby("sport"):
        if len(group) < 10:
            continue
        entry: dict = {"n": int(len(group))}
        y = group["log_load"].to_numpy()
        for label, columns in NESTED_MODELS.items():
            r2_in, r2_cv = _ols_cv_r2(group[columns].to_numpy(dtype=float), y)
            entry[label] = {"r2": r2_in, "r2_cv": r2_cv}
        # What survives the fullest model?
        full = group[NESTED_MODELS["recon_plus_te"]].to_numpy(dtype=float)
        design = np.column_stack([np.ones(len(full)), full])
        coef, *_ = np.linalg.lstsq(design, y, rcond=None)
        resid = y - design @ coef
        entry["resid_sd_log"] = round(float(resid.std()), 3)
        for name, column in (("duration", "duration_min"), ("hr_coverage", "hr_coverage")):
            values = group[column].to_numpy(dtype=float)
            mask = np.isfinite(values)
            entry[f"resid_vs_{name}"] = (
                round(float(np.corrcoef(values[mask], resid[mask])[0, 1]), 3)
                if mask.sum() > 10 and np.std(values[mask]) > 0 else None
            )
        report["per_sport"][sport] = entry

    # Pooled, with sport absorbed as fixed effects so the comparison is
    # within-sport rather than driven by between-sport differences.
    dummies = pd.get_dummies(frame["sport"], drop_first=True).to_numpy(dtype=float)
    y = frame["log_load"].to_numpy()
    for label, columns in NESTED_MODELS.items():
        X = np.column_stack([frame[columns].to_numpy(dtype=float), dummies])
        r2_in, r2_cv = _ols_cv_r2(X, y)
        report["pooled_within_sport"][label] = {"r2": r2_in, "r2_cv": r2_cv}
    report["pooled_within_sport"]["n"] = int(len(frame))
    return report


def build_table(store: CuratedDataStore, workers: int = 16) -> pd.DataFrame:
    """Assemble one row per activity with Garmin's load and all three rebuilds."""
    zones = store.load_hr_zones()
    by_sport = {row["sport"]: row for _, row in zones.iterrows()}
    resting = store.load_daily("heart_rate")
    resting["date"] = pd.to_datetime(resting["date"]).dt.date
    resting_by_date = dict(zip(resting["date"], resting["resting_hr"], strict=True))
    median_resting = float(resting["resting_hr"].median())

    rows = []
    for sport, detail_dataset in SPORT_DETAIL.items():
        summary = store.load_activity_summary(sport)
        if summary.empty or "training_load" not in summary.columns:
            continue
        scored = summary[summary["training_load"].notna()].copy()
        if scored.empty:
            continue
        zone_row = by_sport.get("CYCLING" if sport in _CYCLING_SPORTS else "DEFAULT")
        floors = tuple(float(zone_row[f"zone{z}_floor"]) for z in range(1, 6))
        max_hr = float(zone_row["max_hr"])
        scored["date"] = pd.to_datetime(scored["date"]).dt.date

        # Loop variables are bound as defaults: the closure is handed to a
        # thread pool, and late binding would silently score every sport with
        # the last sport's zones.
        def one(record, sport=sport, detail_dataset=detail_dataset,
                max_hr=max_hr, floors=floors):
            activity_id = str(record["activity_id"])
            try:
                detail = store.load_activity_detail(detail_dataset, activity_id)
            except Exception:
                return None
            rest = float(resting_by_date.get(record["date"], median_resting))
            computed = metrics_for_activity(detail, rest, max_hr, floors)
            if computed is None:
                return None
            computed.update(
                sport=sport,
                # `or np.nan` would be wrong here: an anaerobic effect of 0.0 is
                # a real measurement ("this session had no anaerobic component"),
                # and hiking's median is exactly 0.0.
                aerobic_te=_as_float(record.get("aerobic_training_effect")),
                anaerobic_te=_as_float(record.get("anaerobic_training_effect")),
                activity_id=activity_id,
                date=record["date"],
                training_load=float(record["training_load"]),
                duration_min=_as_float(record.get("duration_min")),
                avg_hr=_as_float(record.get("avg_hr")),
                resting_hr=rest,
            )
            return computed

        records = scored.to_dict("records")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(one, records))
        got = [r for r in results if r]
        print(f"{sport:16s} scored={len(records):4d} usable_hr={len(got):4d}")
        rows.extend(got)
    return pd.DataFrame(rows)


def _corr_block(frame: pd.DataFrame, predictor: str, with_log: bool = True) -> dict:
    """Pearson (raw and log1p) and Spearman against Garmin's load.

    Args:
        frame: Table holding ``training_load`` and the predictor.
        predictor: Column to score.
        with_log: Compute the log1p correlation. Off for mean-centred data,
            where roughly half the values are negative and the log is undefined.
    """
    target, values = frame["training_load"], frame[predictor]
    keep = target.notna() & values.notna() & np.isfinite(values)
    # Reset both indices: these came from a filtered frame, and pandas aligns
    # Series on index, silently returning NaN for a mismatched pair.
    target = target[keep].reset_index(drop=True)
    values = values[keep].reset_index(drop=True)
    if len(target) < 5 or values.nunique() < 3:
        return {"n": int(len(target))}
    block = {
        "n": int(len(target)),
        "pearson": round(float(np.corrcoef(values, target)[0, 1]), 3),
        "spearman": round(float(values.corr(target, method="spearman")), 3),
    }
    if with_log:
        shift = min(0.0, float(values.min()))
        block["pearson_log"] = round(float(np.corrcoef(
            np.log1p(values - shift), np.log1p(target))[0, 1]), 3)
    return block


def summarise(table: pd.DataFrame, predictors=("integral", "banister", "edwards",
                                               "duration_min")) -> dict:
    """Pooled, within-sport and per-sport correlations for each predictor."""
    report: dict = {"n_activities": int(len(table)), "pooled": {},
                    "within_sport": {}, "per_sport": {}}
    for predictor in predictors:
        report["pooled"][predictor] = _corr_block(table, predictor)

    # Within-sport: remove each sport's mean from both sides, so the figure
    # reflects agreement among comparable sessions rather than between sports.
    centred = table.copy()
    for column in (*predictors, "training_load"):
        centred[column] = centred[column] - centred.groupby("sport")[column].transform("mean")
    for predictor in predictors:
        report["within_sport"][predictor] = _corr_block(centred, predictor, with_log=False)

    for sport, group in table.groupby("sport"):
        block = {p: _corr_block(group, p) for p in predictors}
        block["median_load"] = round(float(group["training_load"].median()), 1)
        block["median_duration_min"] = round(float(group["duration_min"].median()), 1)
        block["median_hr_coverage"] = round(float(group["hr_coverage"].median()), 3)
        block["median_gap_s"] = round(float(group["median_gap_s"].median()), 1)
        report["per_sport"][sport] = block
    return report


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--storage-target", choices=["local", "s3"], default="s3")
    parser.add_argument("--out", default=None, help="Write the JSON report here.")
    parser.add_argument("--dump-table", default=None,
                        help="Write the per-activity table as parquet.")
    args = parser.parse_args(argv)

    store = CuratedDataStore(
        FileManager(environment="aws" if args.storage_target == "s3" else "local")
    )
    table = build_table(store)
    if table.empty:
        print("No activities with both training_load and HR timeseries.")
        return
    report = summarise(table)
    report["nested_models"] = nested_model_report(table)
    report["effect_formula"] = fit_effect_formula(table)
    report["effect_formula_log2"] = fit_effect_formula(table, exponent=float(np.log(2)))
    print(json.dumps(report, indent=2, default=str))
    if args.out:
        with open(args.out, "w") as handle:
            json.dump(report, handle, indent=2, default=str)
    if args.dump_table:
        table["date"] = pd.to_datetime(table["date"])
        table.to_parquet(args.dump_table, index=False)


if __name__ == "__main__":
    main()
