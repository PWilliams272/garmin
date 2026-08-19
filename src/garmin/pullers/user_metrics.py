"""Garmin endpoints that were never being called at all.

The 2026-08-18 field audit covered what each *already-called* endpoint returns
versus what we keep. This module covers the other half of that question: whole
endpoints Garmin serves that nothing here had ever requested.

Probed live on 2026-08-19. Each pull below returned real data for this
account; endpoints that errored or came back empty (endurance score, hill
score, gear, courses, goals) are deliberately absent rather than left in as
dead code.

Three shapes again, as with :mod:`garmin.pullers.training`:

- **Range** -- race predictions accept a date range and return one row per
  day, so the whole history is a couple of requests.
- **Per-day** -- fitness age, daily summary, intensity minutes and hydration
  are one request per day, so a full backfill is ~1000 requests and every
  puller here is resumable.
- **Snapshot** -- personal records and devices are undated; they describe
  current state and are rewritten whole.
"""

from __future__ import annotations

import pandas as pd
from tqdm.auto import tqdm

#: Scalar fields worth keeping from the 94-field daily summary. The endpoint
#: also returns nested event lists (bodyBatteryActivityEventList and friends)
#: which are per-event, not per-day, and so do not belong in a daily frame.
#:
#: Several of these duplicate existing curated datasets -- steps, resting
#: heart rate, stress durations, body battery, respiration and SpO2 all have
#: their own datasets already. They are kept anyway, because this endpoint is
#: Garmin's own end-of-day reconciliation and can differ from the per-metric
#: endpoints. Treat the dedicated dataset as canonical and this as a
#: cross-check; see GARMIN_FIELD_AUDIT.md.
DAILY_SUMMARY_FIELDS = {
    "calendarDate": "date",
    # Energy
    "totalKilocalories": "total_kcal",
    "activeKilocalories": "active_kcal",
    "bmrKilocalories": "bmr_kcal",
    "wellnessKilocalories": "wellness_kcal",
    "restingCaloriesFromActivity": "resting_kcal_from_activity",
    "netCalorieGoal": "net_calorie_goal",
    "remainingKilocalories": "remaining_kcal",
    # Movement
    "totalSteps": "total_steps",
    "dailyStepGoal": "step_goal",
    "totalDistanceMeters": "total_distance_m",
    "wellnessDistanceMeters": "wellness_distance_m",
    "floorsAscended": "floors_ascended",
    "floorsDescended": "floors_descended",
    "floorsAscendedInMeters": "floors_ascended_m",
    "floorsDescendedInMeters": "floors_descended_m",
    "userFloorsAscendedGoal": "floors_goal",
    # Intensity minutes -- the WHO-style weekly activity target
    "moderateIntensityMinutes": "moderate_intensity_min",
    "vigorousIntensityMinutes": "vigorous_intensity_min",
    "intensityMinutesGoal": "intensity_minutes_goal",
    # Time budget
    "activeSeconds": "active_s",
    "highlyActiveSeconds": "highly_active_s",
    "sedentarySeconds": "sedentary_s",
    "sleepingSeconds": "sleeping_s",
    "measurableAsleepDuration": "measurable_asleep_s",
    "measurableAwakeDuration": "measurable_awake_s",
    # Heart rate
    "restingHeartRate": "resting_hr",
    "lastSevenDaysAvgRestingHeartRate": "resting_hr_7d_avg",
    "maxHeartRate": "max_hr",
    "minHeartRate": "min_hr",
    "maxAvgHeartRate": "max_avg_hr",
    "minAvgHeartRate": "min_avg_hr",
    "abnormalHeartRateAlertsCount": "abnormal_hr_alerts",
    # Stress
    "averageStressLevel": "avg_stress",
    "maxStressLevel": "max_stress",
    "stressQualifier": "stress_qualifier",
    "stressDuration": "stress_duration_s",
    "restStressDuration": "rest_stress_s",
    "lowStressDuration": "low_stress_s",
    "mediumStressDuration": "medium_stress_s",
    "highStressDuration": "high_stress_s",
    "activityStressDuration": "activity_stress_s",
    "uncategorizedStressDuration": "uncategorized_stress_s",
    "totalStressDuration": "total_stress_s",
    # Body battery
    "bodyBatteryChargedValue": "bb_charged",
    "bodyBatteryDrainedValue": "bb_drained",
    "bodyBatteryHighestValue": "bb_highest",
    "bodyBatteryLowestValue": "bb_lowest",
    "bodyBatteryMostRecentValue": "bb_most_recent",
    "bodyBatteryDuringSleep": "bb_during_sleep",
    "bodyBatteryAtWakeTime": "bb_at_wake",
    # Respiration / SpO2
    "avgWakingRespirationValue": "avg_waking_respiration",
    "highestRespirationValue": "highest_respiration",
    "lowestRespirationValue": "lowest_respiration",
    "latestRespirationValue": "latest_respiration",
    "averageSpo2": "avg_spo2",
    "lowestSpo2": "lowest_spo2",
    "latestSpo2": "latest_spo2",
    # Environment -- a second, independent daily altitude signal alongside
    # training_status.current_altitude_m.
    "averageMonitoringEnvironmentAltitude": "avg_environment_altitude_m",
    # Provenance
    "source": "source",
    "durationInMilliseconds": "duration_ms",
}

#: Hydration fields. sweat_loss_ml is the interesting one -- Garmin's own
#: estimate of fluid lost, which tracks heat stress and activity volume.
HYDRATION_FIELDS = {
    "valueInML": "intake_ml",
    "goalInML": "goal_ml",
    "baseGoalInML": "base_goal_ml",
    "sweatLossInML": "sweat_loss_ml",
    "activityIntakeInML": "activity_intake_ml",
}

#: Weekly rolling intensity minutes. Distinct from the daily-summary fields of
#: similar name: these are running weekly totals against the weekly goal.
INTENSITY_FIELDS = {
    "weeklyModerate": "weekly_moderate_min",
    "weeklyVigorous": "weekly_vigorous_min",
    "weeklyTotal": "weekly_total_min",
    "weekGoal": "week_goal_min",
}


#: Garmin caps the race-prediction range endpoint at 365 days (verified
#: 2026-08-19: 365 days returns 366 rows, 366 days returns HTTP 400).
RACE_PREDICTION_MAX_DAYS = 365


def _year_chunks(start_date: str, end_date: str) -> list[tuple[str, str]]:
    """Split a date range into windows the range endpoints will accept.

    Args:
        start_date: First date, ``YYYY-MM-DD``.
        end_date: Last date, inclusive.

    Returns:
        ``(start, end)`` string pairs, each at most
        :data:`RACE_PREDICTION_MAX_DAYS` long, covering the range in order.
    """
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    chunks = []
    while start <= end:
        stop = min(start + pd.Timedelta(days=RACE_PREDICTION_MAX_DAYS - 1), end)
        chunks.append((start.strftime("%Y-%m-%d"), stop.strftime("%Y-%m-%d")))
        start = stop + pd.Timedelta(days=1)
    return chunks


def _value(record: dict, key: str):
    """Field value, preserving a genuine zero.

    ``record.get(key) or None`` would turn a real 0 -- zero floors climbed,
    zero vigorous minutes -- into a missing value, which is a different claim
    entirely.
    """
    value = record.get(key)
    return None if value is None else value


class UserMetricsPuller:
    """Pulls the Garmin endpoints listed in this module's docstring."""

    def __init__(self, session):
        self.session = session
        self._display_name: str | None = None

    @property
    def display_name(self) -> str:
        """Garmin's ``displayName``, required by several endpoint paths.

        Fetched once and cached: it never changes within a run, and these
        endpoints are called once per day over ~1000 days.
        """
        if self._display_name is None:
            profile = self.session.get("/userprofile-service/socialProfile") or {}
            self._display_name = profile.get("displayName") or ""
        return self._display_name

    def _pull_per_day(self, name: str, url_template: str, extract, start_date: str,
                      end_date: str, known_dates: set | None = None,
                      show_progress: bool = True) -> pd.DataFrame:
        """Walk a date range one request per day, skipping dates already held.

        Args:
            name: Progress-bar label.
            url_template: URL with a ``{date}`` placeholder.
            extract: Turns one response into a dict, or None to skip the day.
            start_date: First date, ``YYYY-MM-DD``.
            end_date: Last date, inclusive.
            known_dates: ``datetime.date`` values to skip, so a partial run
                resumes rather than restarting.
            show_progress: Show a progress bar. Off for the nightly run.

        Returns:
            One row per day that returned data, with a ``date`` column.
        """
        known_dates = known_dates or set()
        pending = [d for d in pd.date_range(start_date, end_date, freq="D")
                   if d.date() not in known_dates]
        rows = []
        for day in tqdm(pending, desc=name, unit="day", disable=not show_progress):
            stamp = day.strftime("%Y-%m-%d")
            try:
                response = self.session.get(url_template.format(date=stamp))
            except Exception:
                # One bad day must not abandon a thousand-day backfill; the
                # run is resumable, so re-running picks the gap back up.
                continue
            record = extract(response) if response else None
            if record:
                record["date"] = day.date()
                rows.append(record)
        return pd.DataFrame(rows)

    # -- range ------------------------------------------------------------

    def pull_race_predictions(self, start_date: str, end_date: str) -> pd.DataFrame:
        """Garmin's predicted race times, one row per day.

        A range endpoint, so the whole history costs a couple of requests
        rather than one per day. These are a useful longitudinal fitness proxy
        precisely because they are computed even on days with no race and no
        hard run.

        The range is capped by Garmin at **365 days** -- verified 2026-08-19,
        where 365 returned 366 rows and 366 returned HTTP 400 -- so a longer
        span is split into yearly windows here rather than failing.

        Args:
            start_date: First date, ``YYYY-MM-DD``.
            end_date: Last date, inclusive.

        Returns:
            One row per day that carried an estimate, times in seconds.
        """
        rows = []
        for chunk_start, chunk_end in _year_chunks(start_date, end_date):
            url = (f"/metrics-service/metrics/racepredictions/daily/{self.display_name}"
                   f"?fromCalendarDate={chunk_start}&toCalendarDate={chunk_end}")
            try:
                response = self.session.get(url)
            except Exception:
                # One bad window must not lose the rest of the history.
                continue
            for record in response or []:
                date = record.get("calendarDate")
                if not date:
                    continue
                rows.append({
                    "date": pd.Timestamp(date).date(),
                    "race_time_5k_s": _value(record, "time5K"),
                    "race_time_10k_s": _value(record, "time10K"),
                    "race_time_half_marathon_s": _value(record, "timeHalfMarathon"),
                    "race_time_marathon_s": _value(record, "timeMarathon"),
                })
        frame = pd.DataFrame(rows)
        if frame.empty:
            return frame
        # Garmin returns a row for every day in range, carrying nulls before
        # it had an estimate. Storing those would create days that look
        # measured and are not.
        value_cols = [c for c in frame.columns if c != "date"]
        return frame.dropna(subset=value_cols, how="all").reset_index(drop=True)

    # -- per day ----------------------------------------------------------

    def _extract_fitness_age(self, response: dict) -> dict | None:
        if not isinstance(response, dict) or response.get("fitnessAge") is None:
            return None
        record = {
            "fitness_age": _value(response, "fitnessAge"),
            "chronological_age": _value(response, "chronologicalAge"),
            "achievable_fitness_age": _value(response, "achievableFitnessAge"),
            "previous_fitness_age": _value(response, "previousFitnessAge"),
            "last_updated": _value(response, "lastUpdated"),
        }
        # Components carry the *why*: which input is holding the score back,
        # and what value would improve it.
        for name, component in (response.get("components") or {}).items():
            if not isinstance(component, dict):
                continue
            record[f"{name}_value"] = _value(component, "value")
            record[f"{name}_target"] = _value(component, "targetValue")
            record[f"{name}_potential_age"] = _value(component, "potentialAge")
        return record

    def pull_fitness_age(self, start_date: str, end_date: str,
                         known_dates: set | None = None,
                         show_progress: bool = True) -> pd.DataFrame:
        """Garmin's fitness age, with the components driving it."""
        return self._pull_per_day(
            "fitness_age", "/fitnessage-service/fitnessage/{date}",
            self._extract_fitness_age, start_date, end_date, known_dates,
            show_progress=show_progress,
        )

    def _extract_daily_summary(self, response: dict) -> dict | None:
        if not isinstance(response, dict) or not response.get("calendarDate"):
            return None
        record = {out: _value(response, src)
                  for src, out in DAILY_SUMMARY_FIELDS.items() if out != "date"}
        return record if any(v is not None for v in record.values()) else None

    def pull_daily_summary(self, start_date: str, end_date: str,
                           known_dates: set | None = None,
                           show_progress: bool = True) -> pd.DataFrame:
        """Garmin's own end-of-day reconciliation across every daily metric.

        The single richest endpoint found in the 2026-08-19 sweep: 94 fields,
        83 of them populated on a sampled day. Much of it overlaps existing
        datasets -- see DAILY_SUMMARY_FIELDS on why it is kept anyway.
        """
        url = ("/usersummary-service/usersummary/daily/" + self.display_name
               + "?calendarDate={date}")
        return self._pull_per_day(
            "daily_summary", url, self._extract_daily_summary,
            start_date, end_date, known_dates, show_progress=show_progress,
        )

    def _extract_hydration(self, response: dict) -> dict | None:
        if not isinstance(response, dict) or not response.get("calendarDate"):
            return None
        record = {out: _value(response, src) for src, out in HYDRATION_FIELDS.items()}
        return record if any(v is not None for v in record.values()) else None

    def pull_hydration(self, start_date: str, end_date: str,
                       known_dates: set | None = None,
                       show_progress: bool = True) -> pd.DataFrame:
        """Fluid intake and Garmin's sweat-loss estimate."""
        return self._pull_per_day(
            "hydration", "/usersummary-service/usersummary/hydration/allData/{date}",
            self._extract_hydration, start_date, end_date, known_dates,
            show_progress=show_progress,
        )

    def _extract_intensity(self, response: dict) -> dict | None:
        if not isinstance(response, dict) or not response.get("calendarDate"):
            return None
        record = {out: _value(response, src) for src, out in INTENSITY_FIELDS.items()}
        return record if any(v is not None for v in record.values()) else None

    def pull_intensity_minutes(self, start_date: str, end_date: str,
                               known_dates: set | None = None,
                               show_progress: bool = True) -> pd.DataFrame:
        """Rolling weekly intensity minutes against the weekly goal."""
        return self._pull_per_day(
            "intensity_minutes", "/wellness-service/wellness/daily/im/{date}",
            self._extract_intensity, start_date, end_date, known_dates,
            show_progress=show_progress,
        )

    # -- snapshots --------------------------------------------------------

    def pull_personal_records(self) -> pd.DataFrame:
        """Current personal records, one row per record type.

        Undated and rewritten whole: this is current state, not a time series.
        """
        response = self.session.get(
            f"/personalrecord-service/personalrecord/prs/{self.display_name}")
        if not response:
            return pd.DataFrame()
        rows = []
        for record in response:
            rows.append({
                "record_id": _value(record, "id"),
                "type_id": _value(record, "typeId"),
                "type_label": _value(record, "prTypeLabelKey"),
                "activity_type": _value(record, "activityType"),
                "activity_id": _value(record, "activityId"),
                "activity_name": _value(record, "activityName"),
                "value": _value(record, "value"),
                "achieved_at_local": _value(record, "prStartTimeLocalFormatted"),
                "status": _value(record, "status"),
            })
        return pd.DataFrame(rows)

    def pull_devices(self) -> pd.DataFrame:
        """Registered devices.

        Useful as provenance: metadataDTO.sensors was null across 2016-2026,
        so this is the available answer to "which watch recorded this era".
        """
        response = self.session.get("/device-service/deviceregistration/devices")
        if not response:
            return pd.DataFrame()
        rows = []
        for device in response:
            rows.append({
                "device_id": _value(device, "deviceId"),
                "display_name": _value(device, "displayName"),
                "product_display_name": _value(device, "productDisplayName"),
                "part_number": _value(device, "partNumber"),
                "serial_number": _value(device, "serialNumber"),
                "device_type_pk": _value(device, "deviceTypePk"),
            })
        return pd.DataFrame(rows)
