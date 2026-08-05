"""How much of tomorrow's wellness is actually predictable, and from what.

This is the reality check that has to come before any causal modelling. If
training history carries no out-of-sample signal about tomorrow's HRV beyond
what today's HRV already tells you, then a more elaborate causal model is
fitting noise with more machinery, and the honest thing is to say so.

Evaluated against persistence, not against zero
-----------------------------------------------
The baseline is "tomorrow looks like today". This matters enormously and is
the easiest thing to get wrong: HRV has lag-1 autocorrelation around 0.56 on
this account, so a model that only learns "tomorrow resembles today" will
post an impressive-looking R^2 while containing no information about
training at all. Skill is therefore reported *relative to persistence*, and a
model that cannot beat it is reported as not beating it.

Forward-chaining, never random splits
-------------------------------------
Days are autocorrelated, so a random train/test split puts a Tuesday in
training and the Wednesday either side of it in test. The model then predicts
values it has effectively already seen and scores far better than it could in
practice. Every split here trains only on the past and tests on the future.

Leakage
-------
Features for predicting day t+1 come from day t and earlier. The panel's
training columns describe the day they happened on, so nothing about t+1 is
available at prediction time. Outcome columns are lagged explicitly.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# Outcomes worth attempting. Weight is excluded: it is measured irregularly
# and its day-to-day movement is mostly hydration, so predicting it well would
# say nothing about training.
DEFAULT_TARGETS = ("hrv", "resting_hr", "sleep_score")

# Panel columns offered as features. Deliberately excludes the target's own
# same-day value only where that would be circular -- for the rest, "today's
# value" is exactly the signal persistence uses and the model should have it
# too, or the comparison is unfair to the model.
FEATURE_COLUMNS = (
    "hrv", "resting_hr", "sleep_score", "sleep_total_s", "sleep_deep_s",
    "sleep_rem_s", "stress", "body_battery_high", "body_battery_low",
    "respiration_waking", "steps",
    "duration_min", "hr_load", "sessions",
    "duration_running", "duration_strength", "duration_cycling",
    "duration_min_acute_7d", "duration_min_chronic_28d", "duration_min_acwr",
    "hr_load_acute_7d", "hr_load_chronic_28d", "hr_load_acwr",
    "days_since_running", "days_since_strength",
    "day_of_week", "season_sin", "season_cos",
)

# Lags of the target itself to include, so the model starts from at least as
# much information as the persistence baseline has.
TARGET_LAGS = (1, 2, 3, 7)

# Lags of *training load* to include. Without these the ablation cannot see a
# delayed effect at all, and would report "training adds nothing" even when a
# large one exists -- the positive control in tests/test_predictive.py caught
# exactly that. This account's own feasibility check put the peak partial
# correlation with HRV at two days out, so the window has to cover it
# comfortably rather than stopping at same-day load.
LOAD_LAG_COLUMNS = ("duration_min", "hr_load")
LOAD_LAGS = (1, 2, 3, 4, 5)

# Number of forward-chaining folds. Each trains on everything before its test
# window and tests on the window itself.
N_SPLITS = 5

# Minimum rows before attempting anything.
MIN_ROWS = 200


def _prepare(panel: pd.DataFrame, target: str) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    """Build the feature matrix, the outcome, and the persistence prediction.

    Row `t` holds features observed up to and including day `t`, and predicts
    the outcome on day `t + 1`.
    """
    frame = panel.sort_values("date").reset_index(drop=True).copy()

    for lag in TARGET_LAGS:
        frame[f"{target}_lag{lag}"] = frame[target].shift(lag)
    # A short rolling baseline is what a person would eyeball, so the model
    # should have it rather than being made to rediscover it.
    frame[f"{target}_roll7"] = frame[target].rolling(7, min_periods=3).mean()

    for column in LOAD_LAG_COLUMNS:
        if column not in frame.columns:
            continue
        for lag in LOAD_LAGS:
            frame[f"{column}_lag{lag}"] = frame[column].shift(lag)

    frame["_outcome"] = frame[target].shift(-1)
    # Persistence: tomorrow equals today.
    frame["_persistence"] = frame[target]

    feature_names = [c for c in FEATURE_COLUMNS if c in frame.columns]
    feature_names += [f"{target}_lag{lag}" for lag in TARGET_LAGS]
    feature_names += [f"{target}_roll7"]
    feature_names += [
        f"{column}_lag{lag}"
        for column in LOAD_LAG_COLUMNS if column in panel.columns
        for lag in LOAD_LAGS
    ]

    usable = frame.dropna(subset=["_outcome", "_persistence"])
    features = usable[feature_names]
    # Median-fill the remaining gaps: a missing body-battery reading should
    # cost that row's contribution, not delete the row entirely.
    features = features.fillna(features.median(numeric_only=True)).fillna(0.0)
    return features, usable["_outcome"], usable["_persistence"]


def _forward_chaining_splits(n_rows: int, n_splits: int = N_SPLITS):
    """Expanding-window splits: train on the past, test on the next block."""
    fold = n_rows // (n_splits + 1)
    for i in range(1, n_splits + 1):
        train_end = fold * i
        test_end = min(fold * (i + 1), n_rows)
        if test_end - train_end < 5:
            continue
        yield np.arange(0, train_end), np.arange(train_end, test_end)


def evaluate_target(panel: pd.DataFrame, target: str) -> dict | None:
    """Out-of-sample skill for one outcome, against a persistence baseline.

    Returns MAE for persistence, ridge and gradient boosting, plus the skill
    of each model relative to persistence (positive means better). None when
    there isn't enough data to say anything.
    """
    from sklearn.ensemble import HistGradientBoostingRegressor
    from sklearn.linear_model import RidgeCV
    from sklearn.preprocessing import StandardScaler

    if target not in panel.columns:
        return None
    features, outcome, persistence = _prepare(panel, target)
    if len(features) < MIN_ROWS:
        return None

    errors: dict[str, list[float]] = {"persistence": [], "ridge": [], "boosting": []}
    n_test = 0
    for train_idx, test_idx in _forward_chaining_splits(len(features)):
        x_train = features.iloc[train_idx]
        x_test = features.iloc[test_idx]
        y_train = outcome.iloc[train_idx]
        y_test = outcome.iloc[test_idx]
        if y_train.nunique() < 5:
            continue
        n_test += len(test_idx)

        errors["persistence"].append(
            float(np.mean(np.abs(y_test - persistence.iloc[test_idx])))
        )

        scaler = StandardScaler().fit(x_train)
        ridge = RidgeCV(alphas=np.logspace(-2, 4, 25)).fit(scaler.transform(x_train), y_train)
        errors["ridge"].append(
            float(np.mean(np.abs(y_test - ridge.predict(scaler.transform(x_test)))))
        )

        # Early stopping rather than a fixed 200 iterations: this is refit
        # once per fold per target, and at 200 it dominated the whole offline
        # report build (122s of a 123s run) while never once beating ridge.
        boosting = HistGradientBoostingRegressor(
            max_iter=200, learning_rate=0.05, max_depth=3, random_state=0,
            early_stopping=True, n_iter_no_change=10, validation_fraction=0.15,
        ).fit(x_train, y_train)
        errors["boosting"].append(
            float(np.mean(np.abs(y_test - boosting.predict(x_test))))
        )

    if not errors["persistence"]:
        return None

    mae = {name: float(np.mean(values)) for name, values in errors.items()}
    baseline = mae["persistence"]
    return {
        "target": target,
        "n_train_rows": int(len(features)),
        "n_test_rows": int(n_test),
        "n_folds": len(errors["persistence"]),
        "mae": {k: round(v, 4) for k, v in mae.items()},
        # Positive means the model beat "tomorrow looks like today".
        "skill_vs_persistence": {
            name: round(float(1.0 - value / baseline), 4)
            for name, value in mae.items() if name != "persistence"
        },
        "sd_of_outcome": round(float(outcome.std()), 4),
    }


# Feature groups, so the contribution of training can be isolated from the
# contribution of the outcome's own recent history.
TRAINING_PREFIXES = ("duration", "hr_load", "sessions", "days_since")


def _feature_groups(feature_names: list[str], target: str) -> dict[str, list[str]]:
    training = [c for c in feature_names if c.startswith(TRAINING_PREFIXES)]
    own_history = [c for c in feature_names if c.startswith(f"{target}_") or c == target]
    return {
        # Only the outcome's own past. Beating persistence here means the
        # signal is smoothable, not that anything external predicts it.
        "own_history": own_history,
        # Own past plus training. Any gain over own_history is the part that
        # training actually contributes.
        "own_plus_training": own_history + training,
        "all_features": feature_names,
    }


def ablate_target(panel: pd.DataFrame, target: str) -> dict | None:
    """Separate "this outcome is smoothable" from "training predicts it".

    Beating persistence is a low bar that autocorrelation alone can clear, so
    on its own it says nothing about training. This refits using only the
    outcome's own history, then adds the training columns, and reports the
    difference. If adding training buys nothing, that is the finding.
    """
    from sklearn.linear_model import RidgeCV
    from sklearn.preprocessing import StandardScaler

    if target not in panel.columns:
        return None
    features, outcome, persistence = _prepare(panel, target)
    if len(features) < MIN_ROWS:
        return None

    groups = _feature_groups(list(features.columns), target)
    scores: dict[str, list[float]] = {name: [] for name in groups}
    baseline: list[float] = []

    for train_idx, test_idx in _forward_chaining_splits(len(features)):
        y_train, y_test = outcome.iloc[train_idx], outcome.iloc[test_idx]
        if y_train.nunique() < 5:
            continue
        baseline.append(float(np.mean(np.abs(y_test - persistence.iloc[test_idx]))))
        for name, columns in groups.items():
            if not columns:
                continue
            x_train = features.iloc[train_idx][columns]
            x_test = features.iloc[test_idx][columns]
            scaler = StandardScaler().fit(x_train)
            with np.errstate(all="ignore"):
                model = RidgeCV(alphas=np.logspace(-2, 4, 25)).fit(
                    scaler.transform(x_train), y_train
                )
                predicted = model.predict(scaler.transform(x_test))
            scores[name].append(float(np.mean(np.abs(y_test - predicted))))

    if not baseline:
        return None

    persistence_mae = float(np.mean(baseline))
    mae = {name: float(np.mean(values)) for name, values in scores.items() if values}
    own = mae.get("own_history")
    with_training = mae.get("own_plus_training")
    return {
        "target": target,
        "persistence_mae": round(persistence_mae, 4),
        "mae": {k: round(v, 4) for k, v in mae.items()},
        "skill_vs_persistence": {
            k: round(1.0 - v / persistence_mae, 4) for k, v in mae.items()
        },
        # The number that answers "does training tell me anything extra".
        "training_gain": (
            None if own is None or with_training is None
            else round(float(1.0 - with_training / own), 4)
        ),
    }


def format_ablation(rows: list[dict]) -> str:
    """Report the training contribution, stating plainly when it is nil."""
    if not rows:
        return "Not enough data for an ablation."
    lines = [
        f"{'outcome':14s} {'persist':>8s} {'own hist':>9s} {'+training':>10s} "
        f"{'gain from':>10s}",
        f"{'':14s} {'MAE':>8s} {'MAE':>9s} {'MAE':>10s} {'training':>10s}",
    ]
    for row in rows:
        mae = row["mae"]
        gain = row["training_gain"]
        verdict = (
            "training adds real signal" if gain is not None and gain > 0.02
            else "training adds nothing measurable" if gain is not None and gain > -0.02
            else "training features hurt (overfitting)"
        )
        lines.append(
            f"{row['target']:14s} {row['persistence_mae']:8.3f} "
            f"{mae.get('own_history', float('nan')):9.3f} "
            f"{mae.get('own_plus_training', float('nan')):10.3f} "
            f"{gain:+10.1%}  {verdict}"
        )
    return "\n".join(lines)


def evaluate_all(panel: pd.DataFrame, targets: tuple[str, ...] = DEFAULT_TARGETS) -> list[dict]:
    """Run every target and return the results, worst-to-best skill last."""
    results = []
    for target in targets:
        result = evaluate_target(panel, target)
        if result is not None:
            results.append(result)
    return results


def format_report(results: list[dict]) -> str:
    """Human-readable summary, stating plainly where there is no skill."""
    if not results:
        return "Not enough data to evaluate predictability."
    lines = [
        f"{'outcome':14s} {'persist':>9s} {'ridge':>9s} {'boost':>9s} "
        f"{'ridge':>8s} {'boost':>8s}  verdict",
        f"{'':14s} {'MAE':>9s} {'MAE':>9s} {'MAE':>9s} {'skill':>8s} {'skill':>8s}",
    ]
    for row in results:
        mae, skill = row["mae"], row["skill_vs_persistence"]
        best = max(skill.values())
        verdict = (
            "beats persistence" if best > 0.02
            else "no better than persistence" if best > -0.02
            else "worse than persistence"
        )
        lines.append(
            f"{row['target']:14s} {mae['persistence']:9.3f} {mae['ridge']:9.3f} "
            f"{mae['boosting']:9.3f} {skill['ridge']:+8.1%} {skill['boosting']:+8.1%}  {verdict}"
        )
    return "\n".join(lines)


def analyze_predictive_skill(curated_store) -> None:
    """Evaluate next-day predictability and persist the result.

    Refitting this per page load is not an option -- the fold-by-fold refits
    take over a minute -- so it is computed once here and the web tier reads
    the artifact, the same split the strength curves use.
    """
    from garmin.analysis.daily_panel import build_daily_panel

    panel = build_daily_panel(curated_store)
    if panel.empty:
        print("No daily panel to evaluate predictability from.")
        return
    # Restrict to the period where wellness was actually being recorded;
    # earlier years have activity but no HRV or sleep to predict.
    measured = panel[panel[list(DEFAULT_TARGETS)].notna().any(axis=1)]

    results = evaluate_all(measured)
    ablations = [row for target in DEFAULT_TARGETS
                 if (row := ablate_target(measured, target)) is not None]
    if not results:
        print("Not enough overlapping data to evaluate predictability.")
        return

    curated_store.write_predictive_skill("panel", {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "n_days": int(len(measured)),
        "date_min": str(measured["date"].min().date()),
        "date_max": str(measured["date"].max().date()),
        "results": results,
        "ablations": ablations,
    })
    print(f"Evaluated predictability on {len(measured)} days:")
    print(format_report(results))
    print(format_ablation(ablations))
