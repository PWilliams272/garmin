## garmin/pullers/activities.py

import pandas as pd
from tqdm.auto import tqdm

METERS_PER_MILE = 1609.344
METERS_PER_FOOT = 0.3048
GRAMS_PER_LB = 453.592


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
            })
        return pd.DataFrame(rows)

    def get_strength_workout(self, activity_id: str) -> pd.DataFrame:
        """Per-set detail for one strength_training activity: exercise, reps, weight."""
        url = f"/activity-service/activity/{activity_id}/exerciseSets"
        res = self.session.get(url)
        if not res:
            return pd.DataFrame()

        rows = []
        for s in res.get("exerciseSets") or []:
            if s.get("setType") != "ACTIVE":
                continue
            exercises = s.get("exercises") or []
            category = None
            if exercises:
                category = max(exercises, key=lambda e: e.get("probability", 0) or 0).get("category")
            weight_grams = s.get("weight")
            rows.append({
                "exercise": (category or "unknown").lower(),
                "reps": s.get("repetitionCount"),
                "weight_lb": round(weight_grams / GRAMS_PER_LB, 1) if weight_grams is not None else None,
                "duration_s": s.get("duration"),
                "set_start_time": s.get("startTime"),
                "set_index": s.get("messageIndex"),
            })
        return pd.DataFrame(rows)

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
        - cadence_spm uses `directDoubleCadence`, not `directRunCadence` --
          the latter is per-leg (observed ~half of directDoubleCadence,
          e.g. 80 vs. 161), while directDoubleCadence lines up with the
          full steps/min convention pull_running_summary's
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
            return raw[col] if col in raw.columns else pd.Series([None] * len(raw))

        out = pd.DataFrame({
            "timestamp": pd.to_datetime(_get("directTimestamp"), unit="ms", errors="coerce"),
            "lat": _get("directLatitude"),
            "lon": _get("directLongitude"),
            "elevation_ft": (_get("directElevation") / METERS_PER_FOOT).round(1),
            "distance_mi": (_get("sumDistance") / METERS_PER_MILE).round(3),
            "speed_mph": (_get("directSpeed") * 2.236936).round(2),
            "cadence_spm": _get("directDoubleCadence"),
            "heart_rate_bpm": _get("directHeartRate"),
            "power_w": _get("directPower"),
            "grade_adjusted_speed": _get("directGradeAdjustedSpeed"),
            "ground_contact_time_ms": _get("directGroundContactTime"),
            "vertical_oscillation": _get("directVerticalOscillation"),
            "vertical_ratio": _get("directVerticalRatio"),
            "stride_length": _get("directStrideLength"),
        })
        return out

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
