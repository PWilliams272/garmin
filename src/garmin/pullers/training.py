"""Garmin's own training metrics: readiness, status, HR zones.

These are Garmin's watch-computed views of load and recovery, distinct from
anything this repo derives. Keeping them means a hand-rolled training-load
metric can be scored against a validated one instead of trusted on faith.

Three different response shapes live here because Garmin offers three:

- **HR zones** are a single undated call returning one record per sport.
- **Readiness** and **status** are strictly per-day -- there is no range form
  (verified 2026-08-15: ``/daily/{start}/{end}``, ``/range/...`` and a
  ``?startDate=`` parameter were all tried and only the single-date URL works).
  A full backfill is therefore one request per day, so both pullers skip dates
  already held and report progress.
"""

from __future__ import annotations

import pandas as pd
from tqdm.auto import tqdm

__all__ = ["TrainingPuller"]

#: Fields worth keeping from the training-readiness response.
_READINESS_FIELDS = {
    "score": "readiness_score",
    "level": "readiness_level",
    "acuteLoad": "acute_load",
    "acwrFactorPercent": "acwr_factor_pct",
    "recoveryTime": "recovery_time_hr",
    "hrvWeeklyAverage": "hrv_weekly_avg",
    "sleepScore": "readiness_sleep_score",
    "sleepScoreFactorPercent": "sleep_score_factor_pct",
    "sleepHistoryFactorPercent": "sleep_history_factor_pct",
    "stressHistoryFactorPercent": "stress_history_factor_pct",
    "hrvFactorPercent": "hrv_factor_pct",
    "recoveryTimeFactorPercent": "recovery_time_factor_pct",
    "feedbackShort": "readiness_feedback",
}

#: Fields from the monthly load balance inside the training-status response.
_LOAD_BALANCE_FIELDS = {
    "monthlyLoadAerobicLow": "load_aerobic_low",
    "monthlyLoadAerobicHigh": "load_aerobic_high",
    "monthlyLoadAnaerobic": "load_anaerobic",
    "monthlyLoadAerobicLowTargetMin": "load_aerobic_low_target_min",
    "monthlyLoadAerobicLowTargetMax": "load_aerobic_low_target_max",
    "monthlyLoadAerobicHighTargetMin": "load_aerobic_high_target_min",
    "monthlyLoadAerobicHighTargetMax": "load_aerobic_high_target_max",
    "monthlyLoadAnaerobicTargetMin": "load_anaerobic_target_min",
    "monthlyLoadAnaerobicTargetMax": "load_anaerobic_target_max",
    "trainingBalanceFeedbackPhrase": "load_balance_feedback",
}


class TrainingPuller:
    """Pulls Garmin's readiness, training-status and HR-zone metrics."""

    def __init__(self, session) -> None:
        self.session = session

    def pull_hr_zones(self) -> pd.DataFrame:
        """Configured heart-rate zones and max HR, per sport.

        Returns:
            One row per sport with ``max_hr``, ``lactate_threshold_hr`` and the
            five zone floors. Empty if Garmin returns nothing.

        Note:
            ``max_hr`` here is Garmin's *observed* figure, which for this
            account is meaningfully higher than an age-predicted one -- prefer
            it over ``220 - age`` when scaling HR reserve.
        """
        response = self.session.get("/biometric-service/heartRateZones") or []
        rows = []
        for zone_set in response:
            row = {
                "sport": zone_set.get("sport"),
                "training_method": zone_set.get("trainingMethod"),
                "max_hr": zone_set.get("maxHeartRateUsed"),
                "lactate_threshold_hr": zone_set.get("lactateThresholdHeartRateUsed"),
                "resting_hr_used": zone_set.get("restingHeartRateUsed"),
            }
            for zone in range(1, 6):
                row[f"zone{zone}_floor"] = zone_set.get(f"zone{zone}Floor")
            rows.append(row)
        return pd.DataFrame(rows)

    def _pull_per_day(
        self, name: str, url_template: str, extract, start_date: str, end_date: str,
        known_dates: set | None = None,
    ) -> pd.DataFrame:
        """Walk a date range one request per day, skipping dates already held.

        Args:
            name: Label for the progress bar.
            url_template: URL with a ``{date}`` placeholder.
            extract: Callable turning one response into a dict, or None to skip.
            start_date: First date, ``YYYY-MM-DD``.
            end_date: Last date, inclusive.
            known_dates: ``datetime.date`` values to skip, making a partial run
                resumable rather than restarting the whole range.

        Returns:
            One row per day that returned data, with a ``date`` column.
        """
        known_dates = known_dates or set()
        dates = pd.date_range(start_date, end_date, freq="D")
        pending = [d for d in dates if d.date() not in known_dates]
        rows = []
        for day in tqdm(pending, desc=name, unit="day"):
            stamp = day.strftime("%Y-%m-%d")
            try:
                response = self.session.get(url_template.format(date=stamp))
            except Exception:
                # A single bad day should not abandon a thousand-day backfill;
                # the run is resumable, so re-running picks the gap back up.
                continue
            record = extract(response) if response else None
            if not record:
                continue
            record["date"] = day.date()
            rows.append(record)
        return pd.DataFrame(rows)

    @staticmethod
    def _extract_readiness(response) -> dict | None:
        if not isinstance(response, list) or not response:
            return None
        entry = response[0]
        record = {out: entry.get(src) for src, out in _READINESS_FIELDS.items()}
        # Garmin returns a row for days it has not scored; those carry no
        # readiness at all and would otherwise land as an all-null row.
        return record if record.get("readiness_score") is not None else None

    @staticmethod
    def _extract_status(response) -> dict | None:
        if not isinstance(response, dict):
            return None
        record: dict = {}

        balance = (response.get("mostRecentTrainingLoadBalance") or {})
        by_device = balance.get("metricsTrainingLoadBalanceDTOMap") or {}
        # Keyed by device id; take the primary training device, else any.
        chosen = next(
            (v for v in by_device.values() if v.get("primaryTrainingDevice")),
            next(iter(by_device.values()), None),
        )
        if chosen:
            record.update({out: chosen.get(src) for src, out in _LOAD_BALANCE_FIELDS.items()})

        status = (response.get("mostRecentTrainingStatus") or {})
        status_map = status.get("latestTrainingStatusData") or {}
        latest = next(iter(status_map.values()), None)
        if latest:
            record["training_status"] = latest.get("trainingStatus")
            record["training_status_feedback"] = latest.get("trainingStatusFeedbackPhrase")
            record["fitness_trend"] = latest.get("fitnessTrend")
            record["weekly_training_load"] = latest.get("weeklyTrainingLoad")
            record["training_paused"] = latest.get("trainingPaused")
            load = latest.get("acuteTrainingLoadDTO") or {}
            record["load_acute"] = load.get("dailyTrainingLoadAcute")
            record["load_chronic"] = load.get("dailyTrainingLoadChronic")
            record["acwr_percent"] = load.get("acwrPercent")
            record["acwr_status"] = load.get("acwrStatus")
            record["load_chronic_target_min"] = load.get("minTrainingLoadChronic")
            record["load_chronic_target_max"] = load.get("maxTrainingLoadChronic")

        return record or None

    def pull_training_readiness(
        self, start_date: str, end_date: str, known_dates: set | None = None
    ) -> pd.DataFrame:
        """Daily training readiness, including Garmin's own acute load and ACWR.

        Args:
            start_date: First date, ``YYYY-MM-DD``.
            end_date: Last date, inclusive.
            known_dates: Dates already stored, skipped so a run is resumable.

        Returns:
            One row per scored day.
        """
        return self._pull_per_day(
            "training_readiness",
            "/metrics-service/metrics/trainingreadiness/{date}",
            self._extract_readiness,
            start_date, end_date, known_dates,
        )

    def pull_vo2max(self, start_date: str, end_date: str) -> pd.DataFrame:
        """VO2max, as a date range rather than one request per day.

        Garmin keeps two independent series: ``generic`` (running and general
        fitness) and ``cycling``. They are separate estimates from different
        activity types, so they are kept as separate columns rather than
        collapsed -- averaging them would blend two different measurements.

        Args:
            start_date: First date, ``YYYY-MM-DD``.
            end_date: Last date, inclusive.

        Returns:
            One row per date with ``vo2max_generic`` and/or ``vo2max_cycling``.
            Days without a new estimate are absent -- VO2max only updates after
            a qualifying activity, so a short window can legitimately return
            nothing.

        Note:
            This supersedes the ``vo2max`` entry in ``HealthPuller``, which
            reads only the ``generic`` series and would silently drop cycling.
        """
        response = self.session.get(
            f"/metrics-service/metrics/maxmet/daily/{start_date}/{end_date}"
        ) or []
        by_date: dict = {}
        for record in response:
            for series in ("generic", "cycling"):
                entry = record.get(series)
                if not entry or entry.get("vo2MaxPreciseValue") is None:
                    continue
                day = pd.to_datetime(entry["calendarDate"]).date()
                row = by_date.setdefault(day, {"date": day})
                row[f"vo2max_{series}"] = entry.get("vo2MaxPreciseValue")
                row[f"vo2max_{series}_rounded"] = entry.get("vo2MaxValue")
                if entry.get("fitnessAge") is not None:
                    row["fitness_age"] = entry.get("fitnessAge")
        return pd.DataFrame(sorted(by_date.values(), key=lambda r: r["date"]))

    def pull_training_status(
        self, start_date: str, end_date: str, known_dates: set | None = None
    ) -> pd.DataFrame:
        """Daily training status and monthly aerobic/anaerobic load balance.

        Args:
            start_date: First date, ``YYYY-MM-DD``.
            end_date: Last date, inclusive.
            known_dates: Dates already stored, skipped so a run is resumable.

        Returns:
            One row per day that returned a status.
        """
        return self._pull_per_day(
            "training_status",
            "/metrics-service/metrics/trainingstatus/aggregated/{date}",
            self._extract_status,
            start_date, end_date, known_dates,
        )
