## garmin/pullers/activities.py

import io
import zipfile

import fitparse
import pandas as pd
from tqdm.auto import tqdm

METERS_PER_MILE = 1609.344
METERS_PER_FOOT = 0.3048

#: Number of Garmin heart-rate zones. Zone boundaries themselves come from
#: /biometric-service/heartRateZones, not from the activity response.
HR_ZONE_COUNT = 5


def _training_load_fields(activity: dict) -> dict:
    """Garmin's own effort metrics for one activity.

    Garmin computes an EPOC-based training load and per-zone time on the watch
    and returns them in the activity list response. Carrying them through means
    a hand-rolled TRIMP can be benchmarked against a validated one rather than
    trusted on faith, and ``hr_zone_*_s`` makes a zone-summed (Edwards) load
    computable without reconstructing zone boundaries.

    Every field is optional: older activities, manually-entered ones, and
    sessions recorded before the current watch simply omit them, so each comes
    back None rather than raising.

    Args:
        activity: One entry from the activity-list response.

    Returns:
        Column name to value, ready to merge into a summary row.
    """
    fields = {
        "training_load": activity.get("activityTrainingLoad"),
        "aerobic_training_effect": activity.get("aerobicTrainingEffect"),
        "anaerobic_training_effect": activity.get("anaerobicTrainingEffect"),
        "training_effect_label": activity.get("trainingEffectLabel"),
        "moderate_intensity_min": activity.get("moderateIntensityMinutes"),
        "vigorous_intensity_min": activity.get("vigorousIntensityMinutes"),
    }
    # Seconds spent in each HR zone, zone 1 (easiest) through 5.
    for zone in range(1, HR_ZONE_COUNT + 1):
        fields[f"hr_zone_{zone}_s"] = activity.get(f"hrTimeInZone_{zone}")
    return fields
GRAMS_PER_LB = 453.592
SEMICIRCLE_TO_DEGREES = 180 / (2 ** 31)

# Sports whose FIT `cadence` field is per-leg (confirmed for running by
# comparing a FIT record against the JSON /details endpoint's
# directDoubleCadence for the same timestamp: FIT reported exactly half).
# Cycling's `cadence` is already true RPM, not doubled.
FIT_CADENCE_DOUBLED_SPORTS = {"running"}


def _session_context_fields(activity: dict) -> dict:
    """Session context Garmin returns but that we historically discarded.

    Three of these answer questions this repo has previously had to *infer*:

    - ``is_manual`` flags a hand-logged session outright. Until now those were
      identified indirectly, by their heart rate being null.
    - ``moving_duration_s`` / ``elapsed_duration_s`` separate active from
      wall-clock time. ``duration`` alone cannot distinguish a paused session
      from a continuous one.
    - ``device_id`` / ``manufacturer`` date the hardware. The 2022-12-04
      per-second-HR boundary is currently inferred from where HR data starts.

    Args:
        activity: One entry from the activity-list response.

    Returns:
        Flat dict of extra columns, all ``None`` when absent.
    """
    fields = {
        "moving_duration_s": activity.get("movingDuration"),
        "elapsed_duration_s": activity.get("elapsedDuration"),
        # Garmin sends both spellings; they have always agreed in this account,
        # but prefer the explicit one and fall back rather than assume.
        "is_manual": activity.get("isManualActivity", activity.get("manualActivity")),
        "body_battery_change": activity.get("differenceBodyBattery"),
        "water_estimated_ml": activity.get("waterEstimated"),
        "bmr_calories": activity.get("bmrCalories"),
        "steps": activity.get("steps"),
        "lap_count": activity.get("lapCount"),
        "device_id": activity.get("deviceId"),
        "manufacturer": activity.get("manufacturer"),
        "start_time_gmt": activity.get("startTimeGMT"),
        "end_time_gmt": activity.get("endTimeGMT"),
        "begin_timestamp_ms": activity.get("beginTimestamp"),
        "time_zone_id": activity.get("timeZoneId"),
        "aerobic_te_message": activity.get("aerobicTrainingEffectMessage"),
        "anaerobic_te_message": activity.get("anaerobicTrainingEffectMessage"),
        "is_personal_record": activity.get("isPR", activity.get("pr")),
        "has_splits": activity.get("hasSplits"),
        "has_polyline": activity.get("hasPolyline"),
    }
    fields.update(_split_summary_fields(activity))
    return fields


def _split_summary_fields(activity: dict) -> dict:
    """Climb/split volume, where Garmin reports it.

    ``splitSummaries`` is a list of per-split-type summaries. For bouldering it
    carries the completed-climb count and the hardest grade, which exist in no
    other field we pull -- a real volume metric for a sport that otherwise has
    only duration and heart rate.
    """
    summaries = activity.get("splitSummaries")
    if not isinstance(summaries, list) or not summaries:
        return {"climbs_completed": None, "max_grade": None, "split_type": None}
    # Prefer the active-climb summary; otherwise the one with the most splits.
    chosen = next(
        (s for s in summaries if s.get("splitType") == "CLIMB_ACTIVE"),
        max(summaries, key=lambda s: s.get("noOfSplits") or 0),
    )
    grade = chosen.get("maxGradeValue") or {}
    return {
        "climbs_completed": chosen.get("numClimbsCompleted") or chosen.get("noOfSplits"),
        "max_grade": grade.get("valueKey") if isinstance(grade, dict) else None,
        "split_type": chosen.get("splitType"),
    }


#: Cycling power-meter dynamics. Present only on rides recorded with a
#: dual-sided power meter, and null everywhere else -- parquet stores the
#: all-null columns cheaply, and having them means a later analysis does not
#: require re-pulling several thousand activities.
_CYCLING_DYNAMICS = {
    "left_right_balance": "directRightBalance",
    "left_torque_effectiveness": "directLeftTorqueEffectiveness",
    "right_torque_effectiveness": "directRightTorqueEffectiveness",
    "left_pedal_smoothness": "directLeftPedalSmoothness",
    "right_pedal_smoothness": "directRightPedalSmoothness",
    "left_platform_center_offset": "directLeftPlatformCenterOffset",
    "right_platform_center_offset": "directRightPlatformCenterOffset",
    "left_power_phase_start": "directLeftPowerPhaseStart",
    "left_power_phase_end": "directLeftPowerPhaseEnd",
    "left_power_phase_peak_start": "directLeftPowerPhasePeakStart",
    "left_power_phase_peak_end": "directLeftPowerPhasePeakEnd",
    "right_power_phase_start": "directRightPowerPhaseStart",
    "right_power_phase_end": "directRightPowerPhaseEnd",
    "right_power_phase_peak_start": "directRightPowerPhasePeakStart",
    "right_power_phase_peak_end": "directRightPowerPhasePeakEnd",
}


def _cycling_dynamics(getter) -> dict:
    """Per-sample power-meter dynamics, via the caller's column accessor."""
    return {out: getter(src) for out, src in _CYCLING_DYNAMICS.items()}


#: FIT record fields carrying the same power-meter dynamics as
#: `_CYCLING_DYNAMICS`, under FIT's own names. Verified 100% exact against the
#: JSON endpoint on cycling activity 23026068497 (2026-08-18), except the two
#: noted below. `left_pco`/`right_pco` are the platform-centre offsets, and the
#: power-phase fields arrive as 2-element [start, end] arrays rather than
#: separate columns.
_FIT_DYNAMICS_DIRECT = {
    "left_torque_effectiveness": "left_torque_effectiveness",
    "right_torque_effectiveness": "right_torque_effectiveness",
    "left_pedal_smoothness": "left_pedal_smoothness",
    "right_pedal_smoothness": "right_pedal_smoothness",
    "left_platform_center_offset": "left_pco",
    "right_platform_center_offset": "right_pco",
}

#: (output column, FIT array field, element index).
_FIT_DYNAMICS_ARRAYS = (
    ("left_power_phase_start", "left_power_phase", 0),
    ("left_power_phase_end", "left_power_phase", 1),
    ("left_power_phase_peak_start", "left_power_phase_peak", 0),
    ("left_power_phase_peak_end", "left_power_phase_peak", 1),
    ("right_power_phase_start", "right_power_phase", 0),
    ("right_power_phase_end", "right_power_phase", 1),
    ("right_power_phase_peak_start", "right_power_phase_peak", 0),
    ("right_power_phase_peak_end", "right_power_phase_peak", 1),
)


def _nan_column(length: int) -> pd.Series:
    """An all-null float column, for fields the FIT file genuinely lacks."""
    return pd.Series([None] * length, dtype="float64")


def _fit_cycling_dynamics(raw: pd.DataFrame, getter) -> dict:
    """Power-meter dynamics from FIT records, matching `_CYCLING_DYNAMICS`'s
    output columns so the FIT and JSON paths stay schema-identical.

    Two fields need decoding rather than a straight rename:

    - `left_right_balance` carries a flag in its high bit, so the raw value
      lands in 136-228 rather than 0-100. Masking with 0x7F reproduces the
      JSON `directRightBalance` value exactly (100.0% of 195 samples);
      unmasked it agrees on 0.0%, i.e. it is silently wrong, not merely
      offset.
    - the power-phase fields are 2-element [start, end] arrays.
    """
    out = {name: getter(src) for name, src in _FIT_DYNAMICS_DIRECT.items()}

    balance = getter("left_right_balance")
    # Nullable-safe: mask only where a value is present, so a missing sample
    # stays missing instead of becoming a real-looking 0% balance.
    masked = balance.fillna(0).astype("int64") & 0x7F
    out["left_right_balance"] = balance.where(balance.isna(), masked)

    for name, src, index in _FIT_DYNAMICS_ARRAYS:
        if src in raw.columns:
            out[name] = pd.to_numeric(
                raw[src].apply(
                    lambda v, i=index: v[i]
                    if isinstance(v, (list, tuple)) and len(v) > i else None
                ),
                errors="coerce",
            )
        else:
            out[name] = _nan_column(len(raw))
    # Emit in _CYCLING_DYNAMICS order so the FIT and JSON frames are column-for-
    # column identical, not merely the same set -- callers concat these.
    return {name: out[name] for name in _CYCLING_DYNAMICS}


class ActivityPuller:
    def __init__(self, session):
        self.session = session

    def pull_activity_list(
        self,
        start_date: str,
        end_date: str,
        activity_types: set[str] | None = None,
        page_size: int = 100,
    ) -> list[dict]:
        """Pull raw activity summary dicts within [start_date, end_date], optionally filtered by typeKey."""
        results = []
        start = 0
        while True:
            page = self.session.get(
                "/activitylist-service/activities/search/activities"
                f"?start={start}&limit={page_size}&startDate={start_date}&endDate={end_date}"
            ) or []
            if not page:
                break
            results.extend(page)
            if len(page) < page_size:
                break
            start += page_size

        if activity_types is not None:
            results = [
                a for a in results
                if a.get("activityType", {}).get("typeKey") in activity_types
            ]
        return results

    def pull_cardio_summary(self, activity_type: str, start_date: str, end_date: str) -> pd.DataFrame:
        """One row per activity of `activity_type`: distance, pace, HR, elevation, calories.

        Generic across any Garmin cardio/distance-based typeKey (cycling, hiking,
        lap_swimming, open_water_swimming, indoor_running, ...) -- fields not present
        for a given type (e.g. distance on an indoor session) come back as None
        rather than raising, same as the existing running/strength summaries.
        """
        activities = self.pull_activity_list(start_date, end_date, activity_types={activity_type})
        rows = []
        for a in activities:
            distance_m = a.get("distance")
            duration_s = a.get("duration")
            avg_speed = a.get("averageSpeed")
            elevation_gain_m = a.get("elevationGain")
            rows.append({
                "activity_id": str(a["activityId"]),
                "date": pd.to_datetime(a["startTimeLocal"]).date(),
                "start_time": a["startTimeLocal"],
                "name": a.get("activityName"),
                "distance_mi": round(distance_m / METERS_PER_MILE, 2) if distance_m is not None else None,
                "duration_min": round(duration_s / 60, 1) if duration_s is not None else None,
                "pace_min_per_mile": round((METERS_PER_MILE / avg_speed) / 60, 2) if avg_speed else None,
                "avg_hr": a.get("averageHR"),
                "max_hr": a.get("maxHR"),
                "elevation_gain_ft": round(elevation_gain_m / METERS_PER_FOOT, 1) if elevation_gain_m is not None else None,
                "calories": a.get("calories"),
                **_training_load_fields(a),
                **_session_context_fields(a),
            })
        return pd.DataFrame(rows)

    def pull_running_summary(self, start_date: str, end_date: str) -> pd.DataFrame:
        """One row per running activity: distance, pace, cadence, HR, elevation.

        Kept as its own method (rather than pull_cardio_summary("running", ...))
        so it stays a single pull_activity_list call -- Garmin rate-limits these
        pulls, so this avoids doubling the list request just to add cadence.
        """
        activities = self.pull_activity_list(start_date, end_date, activity_types={"running"})
        rows = []
        for a in activities:
            distance_m = a.get("distance")
            duration_s = a.get("duration")
            avg_speed = a.get("averageSpeed")
            elevation_gain_m = a.get("elevationGain")
            rows.append({
                "activity_id": str(a["activityId"]),
                "date": pd.to_datetime(a["startTimeLocal"]).date(),
                "start_time": a["startTimeLocal"],
                "name": a.get("activityName"),
                "distance_mi": round(distance_m / METERS_PER_MILE, 2) if distance_m is not None else None,
                "duration_min": round(duration_s / 60, 1) if duration_s is not None else None,
                "pace_min_per_mile": round((METERS_PER_MILE / avg_speed) / 60, 2) if avg_speed else None,
                "cadence_spm": a.get("averageRunningCadenceInStepsPerMinute"),
                "avg_hr": a.get("averageHR"),
                "max_hr": a.get("maxHR"),
                "elevation_gain_ft": round(elevation_gain_m / METERS_PER_FOOT, 1) if elevation_gain_m is not None else None,
                "calories": a.get("calories"),
                **_training_load_fields(a),
                **_session_context_fields(a),
            })
        return pd.DataFrame(rows)

    def pull_strength_summary(self, start_date: str, end_date: str) -> pd.DataFrame:
        """One row per strength_training activity (session-level)."""
        activities = self.pull_activity_list(start_date, end_date, activity_types={"strength_training"})
        rows = []
        for a in activities:
            duration_s = a.get("duration")
            rows.append({
                "activity_id": str(a["activityId"]),
                "date": pd.to_datetime(a["startTimeLocal"]).date(),
                "start_time": a["startTimeLocal"],
                "name": a.get("activityName"),
                "duration_min": round(duration_s / 60, 1) if duration_s is not None else None,
                "calories": a.get("calories"),
                "avg_hr": a.get("averageHR"),
                # Strength summaries lacked max_hr entirely, unlike every other
                # sport -- added here so the schema is consistent.
                "max_hr": a.get("maxHR"),
                **_training_load_fields(a),
                **_session_context_fields(a),
            })
        return pd.DataFrame(rows)

    def get_strength_workout(self, activity_id: str) -> pd.DataFrame:
        """Per-set detail for one strength_training activity: exercise
        (+ up to 3 candidate guesses with confidence), reps, weight, rest
        before the set, and avg/max heart rate during the set.

        Rest and HR come from the FIT file, not the JSON exerciseSets
        endpoint (which has no HR field at all -- confirmed live 2026-08-03,
        the raw response only ever has exercises/duration/repetitionCount/
        weight/setType/startTime/messageIndex). The FIT file has genuine
        native `set` messages alternating active/rest (set_type field,
        confirmed live) with their own precise start_time+duration, so rest
        before a set is the preceding rest set's real duration -- not a
        derived gap -- when the FIT file parses; falls back to no rest/HR
        data (still exercise/reps/weight from JSON) if it doesn't.
        HR is windowed from the FIT record stream (confirmed present and
        fully populated for strength activities, same as running/cycling)
        over each active set's own FIT-native start/duration.

        The exercise classifier's probabilities are raw ML output, but
        manual corrections in the Garmin app *do* leave a real signature --
        confirmed live across 15 real recent sessions (2026-08-04): a set
        the user edited comes back with exactly one exercise candidate at
        probability 100, vs. an unedited/merely-"confirmed" set which keeps
        its original multi-candidate spread (a 3-candidate set with one
        100/two 0 entries is the classifier's own padding pattern, not an
        edit -- distinguished by candidate *count*, not just the top
        probability). Exposed as `manually_reviewed`. The single-candidate
        case also often carries a specific `name` (e.g.
        CABLE_OVERHEAD_TRICEPS_EXTENSION vs. the coarser TRICEPS_EXTENSION
        category) -- captured as `exercise_name` when present.
        """
        url = f"/activity-service/activity/{activity_id}/exerciseSets"
        res = self.session.get(url)
        if not res:
            return pd.DataFrame()

        rows = []
        for s in res.get("exerciseSets") or []:
            if s.get("setType") != "ACTIVE":
                continue
            exercises = sorted(s.get("exercises") or [], key=lambda e: e.get("probability", 0) or 0, reverse=True)
            top = exercises[0] if exercises else {}
            weight_grams = s.get("weight")
            row = {
                "exercise": (top.get("category") or "unknown").lower(),
                "exercise_name": top.get("name"),
                "manually_reviewed": len(exercises) == 1 and (exercises[0].get("probability") or 0) >= 99.99,
                "reps": s.get("repetitionCount"),
                "weight_lb": round(weight_grams / GRAMS_PER_LB, 1) if weight_grams is not None else None,
                "duration_s": s.get("duration"),
                "set_start_time": s.get("startTime"),
                "set_index": s.get("messageIndex"),
            }
            for i in range(3):
                candidate = exercises[i] if i < len(exercises) else {}
                row[f"candidate_{i + 1}_exercise"] = (candidate.get("category") or None) and candidate["category"].lower()
                row[f"candidate_{i + 1}_probability"] = candidate.get("probability")
            rows.append(row)
        df = pd.DataFrame(rows)
        if df.empty:
            return df

        fit_bytes = self.download_activity_fit(activity_id)
        if not fit_bytes:
            return df

        try:
            fitfile = fitparse.FitFile(io.BytesIO(fit_bytes))
            all_fit_sets = sorted(fitfile.get_messages("set"), key=lambda msg: msg.get_value("start_time") or 0)
            records = list(fitfile.get_messages("record"))
        except fitparse.FitParseError:
            return df

        # Align by position (Nth active JSON set <-> Nth active FIT set
        # message), not by messageIndex -- confirmed live 2026-08-04 that
        # some activities' JSON exerciseSets response has messageIndex=None
        # for every set (still None across all 20 sets on a real session),
        # which silently broke lookup-by-index and left rest/HR empty for
        # those activities even though the FIT file itself has everything.
        # Both sources reflect the same chronological set sequence, so
        # positional zip is exact as long as the counts agree.
        fit_active_sets = [m for m in all_fit_sets if m.get_value("set_type") == "active"]
        fit_rest_sets = [m for m in all_fit_sets if m.get_value("set_type") == "rest"]

        record_times = pd.to_datetime(pd.Series([r.get_value("timestamp") for r in records]))
        record_hr = pd.to_numeric(pd.Series([r.get_value("heart_rate") for r in records]), errors="coerce")

        rest_before_s, hr_avg, hr_max, hr_series_t, hr_series_bpm = [], [], [], [], []
        for position in range(len(df)):
            fit_set = fit_active_sets[position] if position < len(fit_active_sets) else None
            # Rest before this set is the rest interval immediately prior
            # to it in FIT's own set sequence -- the Nth active set is
            # preceded by the (N-1)th rest interval (no rest before the
            # very first set).
            rest_set = fit_rest_sets[position - 1] if 0 < position <= len(fit_rest_sets) else None

            rest_before_s.append(rest_set.get_value("duration") if rest_set is not None else None)

            if fit_set is not None and fit_set.get_value("start_time") and fit_set.get_value("duration"):
                window_start = pd.Timestamp(fit_set.get_value("start_time"))
                window_end = window_start + pd.Timedelta(seconds=float(fit_set.get_value("duration")))
                in_window_mask = (record_times >= window_start) & (record_times <= window_end)
                window_times = record_times[in_window_mask]
                window_hr = record_hr[in_window_mask]
                valid = window_hr.notna()
                window_times, window_hr = window_times[valid], window_hr[valid]
            else:
                window_times, window_hr = pd.Series(dtype="datetime64[ns]"), pd.Series(dtype="float64")

            hr_avg.append(round(float(window_hr.mean()), 1) if not window_hr.empty else None)
            hr_max.append(float(window_hr.max()) if not window_hr.empty else None)
            # Small per-set series (a typical set is well under a minute at
            # ~1Hz) for a lightweight in-page sparkline -- offsets in
            # seconds from the set's own start, not wall-clock timestamps.
            if not window_times.empty:
                offsets = (window_times - window_times.iloc[0]).dt.total_seconds().round(1).tolist()
                hr_series_t.append(offsets)
                hr_series_bpm.append(window_hr.tolist())
            else:
                hr_series_t.append([])
                hr_series_bpm.append([])

        df["rest_before_s"] = rest_before_s
        df["hr_avg"] = hr_avg
        df["hr_max"] = hr_max
        df["hr_series_t"] = hr_series_t
        df["hr_series_bpm"] = hr_series_bpm
        return df

    def get_activity_gps(self, activity_id: str) -> pd.DataFrame:
        """Lat/lng (+ optional elevation/timestamp) track for one GPS-based activity.

        `geoPolylineDTO.polyline` on `/activity-service/activity/{id}/details`
        -- confirmed against a live pull (2026-07-31, activity 23528115932):
        real fields are {lat, lon, altitude, time, timerStart, timerStop,
        distanceFromPreviousPoint, distanceInMeters, speed, cumulativeAscent,
        cumulativeDescent, extendedCoordinate, valid}; altitude/distance
        fields are frequently None even on real points, unlike get_activity_
        timeseries's directElevation. get_activity_timeseries (below) covers
        lat/lon plus everything else from the same response and is the
        better default; this is kept as the lighter-weight option when only
        the track itself is needed.
        """
        url = f"/activity-service/activity/{activity_id}/details"
        res = self.session.get(url)
        if not res:
            return pd.DataFrame()

        polyline = (res.get("geoPolylineDTO") or {}).get("polyline") or []
        rows = [
            {
                "lat": p.get("lat"),
                "lon": p.get("lon"),
                "elevation_ft": round(p["altitude"] / METERS_PER_FOOT, 1) if p.get("altitude") is not None else None,
                "time": p.get("time"),
            }
            for p in polyline
        ]
        return pd.DataFrame(rows)

    def get_activity_timeseries(self, activity_id: str) -> pd.DataFrame:
        """Full per-point time series for one activity: timestamp, lat/lon,
        elevation, speed, pace, cadence, heart rate, power, plus a few
        running-dynamics fields, one row per sampled point (~250 points for
        a ~40min run in the confirming pull below -- Garmin's own sampling
        interval, not fixed).

        `/activity-service/activity/{id}/details` (same endpoint as
        get_activity_gps) also returns `metricDescriptors` (an index ->
        field-name map) and `activityDetailMetrics` (one {"metrics": [...]}
        array per point, positioned per that map) -- this is the actual
        source of Garmin Connect's activity detail charts, richer than the
        polyline. Confirmed against a live pull (2026-07-31, running
        activity 23528115932, 254 points): the fields below are real,
        verified `metricDescriptors` keys and observed value ranges, not
        guessed.

        Unit notes (verified where noted, else Garmin's raw units passed
        through unconverted since they weren't independently confirmed):
        - `cadence` uses `directDoubleCadence` (running, steps/min) or
          `directBikeCadence` (cycling, RPM) -- confirmed these are two
          entirely different metricDescriptors sets per activity type, not
          just missing values (a cycling pull returned directBikeCadence,
          directLeftPowerPhaseStart, directPedalSmoothness, and other
          power-meter fields with zero running-dynamics fields present at
          all; a running pull is the reverse). Only one of the two cadence
          fields is ever populated for a given activity, so this column
          combines them rather than picking one and leaving cycling blank.
          Not unit-normalized (steps/min vs. RPM) since the two sports
          aren't meant to be compared on this axis -- treat the unit as
          sport-dependent, same as pace vs. speed.
        - directDoubleCadence itself (running only) is used over
          directRunCadence, which is per-leg (observed ~half of
          directDoubleCadence, e.g. 80 vs. 161) and doesn't match the full
          steps/min convention pull_running_summary's
          averageRunningCadenceInStepsPerMinute already uses.
        - speed_mph converted from directSpeed (confirmed m/s: e.g. 3.55
          m/s observed mid-run maps to a plausible ~7:30/mi pace).
        - elevation_ft converted from directElevation (confirmed meters).
        - distance_mi converted from sumDistance (confirmed meters,
          cumulative from activity start).
        - heart_rate_bpm, power_w, ground_contact_time_ms,
          vertical_oscillation, vertical_ratio, stride_length,
          grade_adjusted_speed passed through as Garmin reports them --
          plausible-looking values observed, but units not independently
          re-derived from a second source.
        """
        url = f"/activity-service/activity/{activity_id}/details"
        res = self.session.get(url)
        if not res:
            return pd.DataFrame()

        index_to_key = {d["metricsIndex"]: d["key"] for d in res.get("metricDescriptors") or []}
        raw_rows = []
        for point in res.get("activityDetailMetrics") or []:
            values = point.get("metrics") or []
            raw_rows.append({index_to_key.get(i, f"metric_{i}"): v for i, v in enumerate(values)})
        if not raw_rows:
            return pd.DataFrame()
        raw = pd.DataFrame(raw_rows)

        def _get(col):
            if col in raw.columns:
                return pd.to_numeric(raw[col], errors="coerce")
            return pd.Series([None] * len(raw), dtype="float64")

        out = pd.DataFrame({
            "timestamp": pd.to_datetime(_get("directTimestamp"), unit="ms", errors="coerce"),
            "lat": _get("directLatitude"),
            "lon": _get("directLongitude"),
            "elevation_ft": (_get("directElevation") / METERS_PER_FOOT).round(1),
            "distance_mi": (_get("sumDistance") / METERS_PER_MILE).round(3),
            "speed_mph": (_get("directSpeed") * 2.236936).round(2),
            "cadence": _get("directDoubleCadence").fillna(_get("directBikeCadence")),
            "heart_rate_bpm": _get("directHeartRate"),
            "power_w": _get("directPower"),
            "grade_adjusted_speed": _get("directGradeAdjustedSpeed"),
            "ground_contact_time_ms": _get("directGroundContactTime"),
            "vertical_oscillation": _get("directVerticalOscillation"),
            "vertical_ratio": _get("directVerticalRatio"),
            "stride_length": _get("directStrideLength"),
            # Cumulative clocks. Garmin reports moving and elapsed time per
            # sample, which is finer than the activity-level `movingDuration`
            # and exists for sports where that summary field is 0 (bouldering).
            "moving_duration_s": _get("sumMovingDuration"),
            "elapsed_duration_s": _get("sumElapsedDuration"),
            "duration_s": _get("sumDuration"),
            # Intraday body battery: the within-session drain, rather than only
            # the net change reported on the activity summary.
            "body_battery": _get("directBodyBattery"),
            # Garmin's own stamina model, cycling and running only.
            "available_stamina": _get("directAvailableStamina"),
            "potential_stamina": _get("directPotentialStamina"),
            "performance_condition": _get("directPerformanceCondition"),
            "vertical_speed": _get("directVerticalSpeed"),
            "run_cadence": _get("directRunCadence"),
            "fractional_cadence": _get("directFractionalCadence"),
            "accumulated_power_w": _get("sumAccumulatedPower"),
            "calories_burn_rate": _get("directCaloriesBurnRate"),
            **_cycling_dynamics(_get),
        })
        return out

    def download_activity_fit(self, activity_id: str) -> bytes | None:
        """Raw FIT file bytes for one activity, unzipped from Garmin's
        activity-file download endpoint. Confirmed live (2026-07-31):
        reachable through the same session/auth as every other endpoint
        here (no separate domain), returns a ZIP containing one .fit file.
        """
        content = self.session.download(f"/download-service/files/activity/{activity_id}")
        if not content:
            return None
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                fit_names = [n for n in zf.namelist() if n.lower().endswith(".fit")]
                if not fit_names:
                    return None
                return zf.read(fit_names[0])
        except zipfile.BadZipFile:
            return None

    def get_activity_fit_timeseries(self, activity_id: str) -> pd.DataFrame:
        """Full per-point time series parsed from the real FIT file --
        confirmed live (2026-07-31) to be genuinely higher resolution than
        get_activity_timeseries's JSON /details endpoint: 1Hz (1560 points
        for a 1550s ride, 1408 for a 1403s run) vs. that endpoint's ~254
        points regardless of activity length (a decimated set meant for
        chart rendering, not raw data).

        Also confirmed the two sources are semantically equivalent, not
        just different resolutions of different things: cross-referencing
        FIT records against JSON points at matching timestamps decoded
        several of FIT's undocumented `unknown_NNN` fields as exact or
        near-exact matches for JSON's directBodyBattery (-> unknown_143),
        directAvailableStamina/directPotentialStamina (-> unknown_138/137),
        and directGradeAdjustedSpeed (-> unknown_140, scaled x1000).

        Re-verified 2026-08-18 on running 23528115932 and cycling
        23026068497, which corrected two things this docstring previously
        got wrong. The stamina pair is **swapped** from what was recorded
        here: available_stamina is unknown_138 and potential_stamina is
        unknown_137, each 100.0% exact, where the reverse assignment agrees
        on only 58-72% -- close enough to look right in a spot check, which
        is presumably how it was mis-recorded. Garmin's own invariant
        (potential >= available) holds for that assignment and fails for
        the other. And directPerformanceCondition, described below as
        unconfirmable, is unknown_90: 100.0% exact on both activities.

        Output columns match get_activity_timeseries's exactly (same
        names/units) so callers can treat the two interchangeably --
        see get_activity_detail_timeseries, which prefers this and falls
        back to the JSON endpoint only if the FIT download/parse fails.
        """
        fit_bytes = self.download_activity_fit(activity_id)
        if not fit_bytes:
            return pd.DataFrame()

        try:
            fitfile = fitparse.FitFile(io.BytesIO(fit_bytes))
            sport = None
            for msg in fitfile.get_messages("session"):
                sport = msg.get_value("sport")
                break
            records = list(fitfile.get_messages("record"))
        except fitparse.FitParseError:
            return pd.DataFrame()

        if not records:
            return pd.DataFrame()

        raw = pd.DataFrame([
            {field.name: field.value for field in record if field.value is not None}
            for record in records
        ])

        def _get(col):
            if col in raw.columns:
                return pd.to_numeric(raw[col], errors="coerce")
            return pd.Series([None] * len(raw), dtype="float64")

        cadence = _get("cadence") + _get("fractional_cadence").fillna(0)
        if sport in FIT_CADENCE_DOUBLED_SPORTS:
            cadence = cadence * 2

        timestamps = pd.to_datetime(raw["timestamp"], errors="coerce") if "timestamp" in raw.columns else pd.Series([pd.NaT] * len(raw))

        out = pd.DataFrame({
            "timestamp": timestamps,
            "lat": _get("position_lat") * SEMICIRCLE_TO_DEGREES,
            "lon": _get("position_long") * SEMICIRCLE_TO_DEGREES,
            "elevation_ft": (_get("enhanced_altitude") / METERS_PER_FOOT).round(1),
            "distance_mi": (_get("distance") / METERS_PER_MILE).round(3),
            "speed_mph": (_get("enhanced_speed") * 2.236936).round(2),
            "cadence": cadence,
            "heart_rate_bpm": _get("heart_rate"),
            "power_w": _get("power"),
            "grade_adjusted_speed": _get("unknown_140") / 1000.0,
            "ground_contact_time_ms": _get("stance_time"),
            "vertical_oscillation": _get("vertical_oscillation"),
            "vertical_ratio": _get("vertical_ratio"),
            "stride_length": _get("step_length"),
            # Wall-clock elapsed. FIT has no counterpart to the JSON path's
            # sumMovingDuration/sumDuration (the moving and timer clocks, which
            # pause), so those stay null here rather than being faked from
            # wall-clock time -- they would be wrong for any paused activity.
            "moving_duration_s": _nan_column(len(raw)),
            "elapsed_duration_s": (timestamps - timestamps.min()).dt.total_seconds(),
            "duration_s": _nan_column(len(raw)),
            "body_battery": _get("unknown_143"),
            "available_stamina": _get("unknown_138"),
            "potential_stamina": _get("unknown_137"),
            "performance_condition": _get("unknown_90"),
            # No FIT counterpart to directVerticalSpeed/directCaloriesBurnRate.
            # directCaloriesBurnRate was all-null in the JSON path too.
            "vertical_speed": _nan_column(len(raw)),
            "run_cadence": _get("cadence") if sport in FIT_CADENCE_DOUBLED_SPORTS
                           else _nan_column(len(raw)),
            "fractional_cadence": _get("fractional_cadence"),
            "accumulated_power_w": _get("accumulated_power"),
            "calories_burn_rate": _nan_column(len(raw)),
            **_fit_cycling_dynamics(raw, _get),
        })
        return out

    def get_activity_detail_timeseries(self, activity_id: str) -> pd.DataFrame:
        """Preferred per-point detail source for an activity: the real FIT
        file (1Hz, richer -- see get_activity_fit_timeseries), falling back
        to the decimated JSON /details endpoint (get_activity_timeseries)
        only if the FIT download/parse fails. Same output columns either
        way, so callers don't need to care which source actually served
        a given activity.
        """
        fit_df = self.get_activity_fit_timeseries(activity_id)
        if not fit_df.empty:
            return fit_df
        return self.get_activity_timeseries(activity_id)

    def pull_strength_sets(self, start_date: str, end_date: str, show_progress: bool = True) -> pd.DataFrame:
        """Per-set detail rows across all strength_training activities in range, tagged with activity_id/date."""
        activities = self.pull_activity_list(start_date, end_date, activity_types={"strength_training"})
        frames = []
        for a in tqdm(activities, desc="Pulling strength sets", disable=not show_progress):
            sets_df = self.get_strength_workout(str(a["activityId"]))
            if sets_df.empty:
                continue
            sets_df["activity_id"] = str(a["activityId"])
            sets_df["date"] = pd.to_datetime(a["startTimeLocal"]).date()
            frames.append(sets_df)
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)
