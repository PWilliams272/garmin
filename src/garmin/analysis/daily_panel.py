"""One row per day: wellness outcomes, training load, and controls.

Everything downstream in the predictive/causal work reads this rather than
re-deriving it -- the point of building it once is that "training load on day
t" means exactly the same thing to every model that uses it.

This is a deterministic aggregation, no fitting, so it is cheap enough to run
in the daily analyzer alongside the other curated outputs.

What counts as "load"
---------------------
There is no single right answer, so two are carried and neither is blended
into the other:

- ``duration_min`` -- time spent. Available for every session including the
  manually-logged ones, and honest about what it is.
- ``hr_load`` -- duration times average heart rate above resting, i.e. roughly
  "heart beats above rest". A volume-times-intensity measure in the spirit of
  Banister's TRIMP (1975), but deliberately simpler: TRIMP proper needs a
  maximum heart rate, and estimating one from age or from an observed maximum
  would add a fabricated number to every row. Only available where the watch
  recorded HR, which excludes a good share of manually-logged sessions.

Letting a model choose between them is better than picking here, because the
two disagree in an informative way: an hour of bouldering and an hour of easy
running are the same duration and very different loads.

Acute, chronic, and their ratio
-------------------------------
7-day and 28-day rolling sums, and the acute:chronic workload ratio, are the
standard framing in the training-load literature. They are computed here for
convenience, but note the ratio is contested -- the injury-risk "sweet spot"
claim around it has been criticised heavily on statistical grounds, and
nothing in this repo treats it as established. It is a feature, not a finding.

Missing days are real
---------------------
The panel spans every calendar day in range, including days with no activity
and days the watch was not worn. A day with no training genuinely has zero
load, so training columns are filled with zero; a day with no HRV reading has
*unknown* HRV, which stays null. Conflating those two would be the single
easiest way to corrupt everything built on top.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from garmin.datasets import ACTIVITY_DATASETS
from garmin.io.curated_store import CuratedDataStore

# Rolling windows for training load, in days.
ACUTE_DAYS = 7
CHRONIC_DAYS = 28

# Sports rolled up into the per-sport load columns. Everything in
# ACTIVITY_DATASETS is included; this names the ones that get their own
# columns rather than only contributing to the total.
TRACKED_SPORTS = ("running", "strength", "cycling", "bouldering", "lap_swimming")

# Fallback resting heart rate when a day has no measurement, used only to
# compute hr_load. The median of this account's own history is a better guess
# than a textbook value.
DEFAULT_RESTING_HR = 55.0


def _daily_wellness(store: CuratedDataStore) -> pd.DataFrame:
    """Outcome columns, one row per day, nulls preserved."""
    frames: list[pd.DataFrame] = []

    def add(dataset: str, columns: dict[str, str]) -> None:
        raw = store.load_daily(dataset)
        if raw.empty:
            return
        available = {src: dst for src, dst in columns.items() if src in raw.columns}
        if not available:
            return
        frame = raw[["date", *available]].rename(columns=available).copy()
        frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
        # A day should appear once; keep the last reading if it doesn't.
        frames.append(frame.drop_duplicates(subset=["date"], keep="last"))

    add("hrv", {"last_night_avg": "hrv", "weekly_avg": "hrv_weekly"})
    add("heart_rate", {"resting_hr": "resting_hr"})
    add("sleep", {
        "sleep_score": "sleep_score",
        "total_sleep_time": "sleep_total_s",
        "deep_time": "sleep_deep_s",
        "rem_time": "sleep_rem_s",
        "awake_time": "sleep_awake_s",
        "skin_temp_f": "skin_temp_f",
    })
    add("body_battery", {"high_body_battery": "body_battery_high",
                         "low_body_battery": "body_battery_low"})
    add("stress", {"overall_stress_level": "stress"})
    add("respiration", {"avg_waking_respiration": "respiration_waking",
                        "avg_sleep_respiration": "respiration_sleep"})
    add("health_stats", {"weight": "weight", "body_fat": "body_fat",
                         "muscle_mass": "muscle_mass"})
    add("steps", {"total_steps": "steps"})

    if not frames:
        return pd.DataFrame(columns=["date"])

    panel = frames[0]
    for frame in frames[1:]:
        panel = panel.merge(frame, on="date", how="outer")
    return panel.sort_values("date").reset_index(drop=True)


def _daily_training(store: CuratedDataStore, resting_by_date: pd.Series) -> pd.DataFrame:
    """Per-sport duration and HR load, one row per day."""
    rows: list[pd.DataFrame] = []
    for sport in ACTIVITY_DATASETS:
        summary = store.load_activity_summary(sport)
        if summary.empty or "date" not in summary.columns:
            continue
        frame = summary.copy()
        frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
        frame["sport"] = sport
        frame["duration_min"] = pd.to_numeric(
            frame.get("duration_min", np.nan), errors="coerce"
        )
        frame["avg_hr"] = pd.to_numeric(frame.get("avg_hr", np.nan), errors="coerce")
        rows.append(frame[["date", "sport", "duration_min", "avg_hr"]])

    if not rows:
        return pd.DataFrame(columns=["date"])

    sessions = pd.concat(rows, ignore_index=True).dropna(subset=["date"])
    sessions = sessions[sessions["duration_min"].fillna(0) > 0]

    # Heart beats above rest. Null where HR wasn't recorded rather than
    # substituting a guess -- a manually-logged session with no HR should not
    # silently contribute a fabricated load.
    resting = sessions["date"].map(resting_by_date).fillna(DEFAULT_RESTING_HR)
    above_rest = (sessions["avg_hr"] - resting).clip(lower=0)
    sessions["hr_load"] = sessions["duration_min"] * above_rest

    # A session with no recorded HR contributes NaN above, and the groupby sum
    # below skips NaN -- so on its own that collapses to 0.0, making a manually
    # logged session indistinguishable from a rest day. Both are real and they
    # mean opposite things, so the unmeasured duration is carried alongside
    # rather than left implicit. Consumers wanting to impute need to know how
    # much training the hr_load figure is NOT accounting for.
    sessions["missing_hr"] = sessions["avg_hr"].isna()
    sessions["duration_missing_hr"] = sessions["duration_min"].where(sessions["missing_hr"], 0.0)

    totals = sessions.groupby("date", as_index=False).agg(
        duration_min=("duration_min", "sum"),
        hr_load=("hr_load", "sum"),
        sessions=("sport", "size"),
        sessions_missing_hr=("missing_hr", "sum"),
        duration_missing_hr=("duration_missing_hr", "sum"),
    )

    per_sport = (
        sessions.groupby(["date", "sport"], as_index=False)["duration_min"].sum()
        .pivot(index="date", columns="sport", values="duration_min")
    )
    per_sport = per_sport[[c for c in TRACKED_SPORTS if c in per_sport.columns]]
    per_sport.columns = [f"duration_{c}" for c in per_sport.columns]
    return totals.merge(per_sport.reset_index(), on="date", how="left")


def build_daily_panel(store: CuratedDataStore) -> pd.DataFrame:
    """Assemble the full daily panel.

    Returns one row per calendar day between the first and last observation,
    with wellness outcomes (nulls where unmeasured), training load (zero where
    untrained), rolling load features, and calendar controls.
    """
    wellness = _daily_wellness(store)
    if wellness.empty:
        return pd.DataFrame()

    resting_by_date = (
        wellness.set_index("date")["resting_hr"]
        if "resting_hr" in wellness.columns
        else pd.Series(dtype=float)
    )
    training = _daily_training(store, resting_by_date)

    start = wellness["date"].min()
    end = wellness["date"].max()
    if not training.empty:
        start = min(start, training["date"].min())
        end = max(end, training["date"].max())

    panel = pd.DataFrame({"date": pd.date_range(start, end, freq="D")})
    panel = panel.merge(wellness, on="date", how="left")
    if not training.empty:
        panel = panel.merge(training, on="date", how="left")

    # A day with no session really did have zero training. A day with no HRV
    # reading has *unknown* HRV and must stay null -- filling it would invent
    # data, and every model downstream would silently believe it.
    load_columns = [c for c in panel.columns
                    if c.startswith("duration")
                    or c in ("hr_load", "sessions", "sessions_missing_hr")]
    panel[load_columns] = panel[load_columns].fillna(0.0)

    indexed = panel.set_index("date")
    # duration_missing_hr is rolled alongside the loads so the acute/chronic
    # figures can be read honestly: hr_load over a window is understated by
    # exactly the training these minutes represent.
    for column in ("duration_min", "hr_load", "duration_missing_hr"):
        if column not in indexed.columns:
            continue
        acute = indexed[column].rolling(f"{ACUTE_DAYS}D", min_periods=1).sum()
        chronic = indexed[column].rolling(f"{CHRONIC_DAYS}D", min_periods=1).sum()
        panel[f"{column}_acute_{ACUTE_DAYS}d"] = acute.to_numpy()
        panel[f"{column}_chronic_{CHRONIC_DAYS}d"] = chronic.to_numpy()
        if column == "duration_missing_hr":
            # This one is a coverage diagnostic, not a training load. An
            # acute:chronic ratio of unmeasured minutes would mean nothing.
            continue
        # Scale the chronic load to the acute window so the ratio is around 1
        # when training is steady, which is the convention these are read in.
        scaled = chronic * (ACUTE_DAYS / CHRONIC_DAYS)
        with np.errstate(all="ignore"):
            ratio = np.where(scaled > 0, acute / scaled, np.nan)
        panel[f"{column}_acwr"] = ratio

    # Days since the last session of each sport. Long layoffs are the closest
    # thing to a natural experiment in this dataset, so they need to be
    # visible rather than inferred later from a run of zeros.
    for sport in TRACKED_SPORTS:
        column = f"duration_{sport}"
        if column not in panel.columns:
            continue
        trained = panel[column] > 0
        since = np.arange(len(panel)) - np.maximum.accumulate(
            np.where(trained, np.arange(len(panel)), -1)
        )
        panel[f"days_since_{sport}"] = np.where(
            np.maximum.accumulate(trained.to_numpy()), since, np.nan
        )

    panel["day_of_week"] = panel["date"].dt.dayofweek
    # Seasonal terms as a sin/cos pair rather than a day number, so that
    # December 31 and January 1 are adjacent rather than maximally distant.
    day_of_year = panel["date"].dt.dayofyear
    panel["season_sin"] = np.sin(2 * np.pi * day_of_year / 365.25)
    panel["season_cos"] = np.cos(2 * np.pi * day_of_year / 365.25)

    return panel.sort_values("date").reset_index(drop=True)


def analyze_daily_panel(curated_store: CuratedDataStore) -> None:
    """Build the daily panel and write it to curated/analyzed/panel/."""
    panel = build_daily_panel(curated_store)
    if panel.empty:
        print("No wellness data to build a daily panel from.")
        return
    curated_store.write_daily_panel("panel", panel)
    complete = panel.dropna(subset=["hrv"])
    print(
        f"Built daily panel: {len(panel)} days "
        f"({panel['date'].min().date()} to {panel['date'].max().date()}), "
        f"{len(complete)} with HRV, {len(panel.columns)} columns."
    )
