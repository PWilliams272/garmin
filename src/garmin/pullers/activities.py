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
