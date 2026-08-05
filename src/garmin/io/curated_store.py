from __future__ import annotations

import json

import pandas as pd
from botocore.exceptions import ClientError

from garmin.io.file_manager import FileManager


class CuratedDataStore:
    """Read and write curated Garmin datasets as parquet files."""

    def __init__(self, file_manager: FileManager | None = None) -> None:
        self.file_manager = file_manager or FileManager()

    @staticmethod
    def daily_dataset_path(dataset: str) -> str:
        return f"curated/daily/{dataset}.parquet"

    @staticmethod
    def detailed_dataset_path(dataset: str, query_date: str) -> str:
        return f"curated/detailed/{dataset}/query_date={query_date}.parquet"

    @staticmethod
    def detailed_status_path(dataset: str) -> str:
        return f"curated/metadata/detailed_status/{dataset}.parquet"

    def load_daily(self, dataset: str) -> pd.DataFrame:
        return self._read_df_or_empty(self.daily_dataset_path(dataset))

    def merge_daily(self, dataset: str, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return self.load_daily(dataset)

        incoming = df.copy()
        incoming["date"] = pd.to_datetime(incoming["date"]).dt.date

        existing = self.load_daily(dataset)
        if existing.empty:
            merged = incoming
        else:
            existing = existing.copy()
            existing["date"] = pd.to_datetime(existing["date"]).dt.date
            merged = pd.concat([existing, incoming], ignore_index=True)
            merged = merged.drop_duplicates(subset=["date"], keep="last")

        merged = merged.sort_values("date").reset_index(drop=True)
        self.file_manager.write_df(
            self._prepare_for_parquet(merged),
            self.daily_dataset_path(dataset),
            format="parquet",
        )
        return merged

    def load_detailed_status(self, dataset: str) -> pd.DataFrame:
        return self._read_df_or_empty(self.detailed_status_path(dataset))

    def merge_detailed_status(self, dataset: str, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return self.load_detailed_status(dataset)

        incoming = df.copy()
        incoming["query_date"] = pd.to_datetime(incoming["query_date"]).dt.date

        existing = self.load_detailed_status(dataset)
        if existing.empty:
            merged = incoming
        else:
            existing = existing.copy()
            existing["query_date"] = pd.to_datetime(existing["query_date"]).dt.date
            merged = pd.concat([existing, incoming], ignore_index=True)
            merged = merged.drop_duplicates(subset=["query_date"], keep="last")

        merged = merged.sort_values("query_date").reset_index(drop=True)
        self.file_manager.write_df(
            self._prepare_for_parquet(merged),
            self.detailed_status_path(dataset),
            format="parquet",
        )
        return merged

    @staticmethod
    def activity_summary_path(dataset: str) -> str:
        return f"curated/activities/summary/{dataset}.parquet"

    @staticmethod
    def activity_detail_path(dataset: str, activity_id: str) -> str:
        return f"curated/activities/detail/{dataset}/activity_id={activity_id}.parquet"

    def load_activity_summary(self, dataset: str) -> pd.DataFrame:
        return self._read_df_or_empty(self.activity_summary_path(dataset))

    def merge_activity_summary(self, dataset: str, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return self.load_activity_summary(dataset)

        incoming = df.copy()
        incoming["date"] = pd.to_datetime(incoming["date"]).dt.date

        existing = self.load_activity_summary(dataset)
        if existing.empty:
            merged = incoming
        else:
            existing = existing.copy()
            existing["date"] = pd.to_datetime(existing["date"]).dt.date
            merged = pd.concat([existing, incoming], ignore_index=True)
            merged = merged.drop_duplicates(subset=["activity_id"], keep="last")

        merged = merged.sort_values("date").reset_index(drop=True)
        self.file_manager.write_df(
            self._prepare_for_parquet(merged),
            self.activity_summary_path(dataset),
            format="parquet",
        )
        return merged

    def write_activity_detail(self, dataset: str, activity_id: str, df: pd.DataFrame) -> None:
        if df.empty:
            return
        self.file_manager.write_df(
            self._prepare_for_parquet(df),
            self.activity_detail_path(dataset, activity_id),
            format="parquet",
        )

    def load_activity_detail(self, dataset: str, activity_id: str) -> pd.DataFrame:
        return self._read_df_or_empty(self.activity_detail_path(dataset, activity_id))

    def load_all_activity_details(self, dataset: str) -> pd.DataFrame:
        prefix = f"curated/activities/detail/{dataset}/"
        files = [f for f in self.file_manager.list_files(prefix) if f.endswith(".parquet")]
        frames = []
        for f in files:
            try:
                frames.append(self.file_manager.read_df(f, format="parquet"))
            except FileNotFoundError:
                continue
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    @staticmethod
    def analyzed_points_path(dataset: str, metric: str) -> str:
        return f"curated/analyzed/{dataset}/{metric}_points.parquet"

    @staticmethod
    def analyzed_trend_path(dataset: str, metric: str, kind: str = "gp") -> str:
        suffix = "_trend.parquet" if kind == "gp" else f"_trend_{kind}.parquet"
        return f"curated/analyzed/{dataset}/{metric}{suffix}"

    def load_analyzed_points(self, dataset: str, metric: str) -> pd.DataFrame:
        return self._read_df_or_empty(self.analyzed_points_path(dataset, metric))

    def write_analyzed_points(self, dataset: str, metric: str, df: pd.DataFrame) -> None:
        if df.empty:
            return
        self.file_manager.write_df(
            self._prepare_for_parquet(df),
            self.analyzed_points_path(dataset, metric),
            format="parquet",
        )

    def load_analyzed_trend(self, dataset: str, metric: str, kind: str = "gp") -> pd.DataFrame:
        return self._read_df_or_empty(self.analyzed_trend_path(dataset, metric, kind))

    def write_analyzed_trend(self, dataset: str, metric: str, df: pd.DataFrame, kind: str = "gp") -> None:
        if df.empty:
            return
        self.file_manager.write_df(
            self._prepare_for_parquet(df),
            self.analyzed_trend_path(dataset, metric, kind),
            format="parquet",
        )

    @staticmethod
    def exercise_review_path(dataset: str) -> str:
        return f"curated/analyzed/{dataset}/exercise_review.parquet"

    def load_exercise_review(self, dataset: str) -> pd.DataFrame:
        """User-facing exercise-guess review state (activity_id + set_index
        -> our_guess_exercise/confidence/review_status/reason), separate
        from the raw Garmin-pulled detail so re-pulling from Garmin never
        clobbers a user's accept/reject decision -- see
        garmin.analysis.analysis_pipeline.infer_exercise_corrections."""
        return self._read_df_or_empty(self.exercise_review_path(dataset))

    def write_exercise_review(self, dataset: str, df: pd.DataFrame) -> None:
        if df.empty:
            return
        self.file_manager.write_df(
            self._prepare_for_parquet(df),
            self.exercise_review_path(dataset),
            format="parquet",
        )

    @staticmethod
    def strength_variant_index_path(dataset: str) -> str:
        return f"curated/analyzed/{dataset}/variant_index.parquet"

    def load_strength_variant_index(self, dataset: str) -> pd.DataFrame:
        """One row per analysed exercise variant: its slug, family, load type
        and session count. Lets the web tier group variants into families for
        display without re-deriving the taxonomy."""
        return self._read_df_or_empty(self.strength_variant_index_path(dataset))

    def write_strength_variant_index(self, dataset: str, df: pd.DataFrame) -> None:
        if df.empty:
            return
        self.file_manager.write_df(
            self._prepare_for_parquet(df),
            self.strength_variant_index_path(dataset),
            format="parquet",
        )

    @staticmethod
    def variant_conversion_path(dataset: str) -> str:
        return f"curated/analyzed/{dataset}/variant_conversions.parquet"

    def load_variant_conversions(self, dataset: str) -> pd.DataFrame:
        """One row per variant: the empirically fitted multiplier putting it on
        its family reference variant's scale, plus the evidence behind it (see
        garmin.analysis.variant_conversion). Rows with `identified` false have a
        factor that failed its checks and must not be used to combine series."""
        return self._read_df_or_empty(self.variant_conversion_path(dataset))

    def write_variant_conversions(self, dataset: str, df: pd.DataFrame) -> None:
        if df.empty:
            return
        self.file_manager.write_df(
            self._prepare_for_parquet(df),
            self.variant_conversion_path(dataset),
            format="parquet",
        )

    @staticmethod
    def strength_curve_path(dataset: str, exercise: str) -> str:
        return f"curated/analyzed/{dataset}/{exercise}_strength_curve.parquet"

    def load_strength_curve(self, dataset: str, exercise: str) -> pd.DataFrame:
        """Fitted load-rep curve output for one exercise: one row per session
        date with e1RM/e5RM/e8RM posterior means and credible bounds (see
        garmin.analysis.strength_curve). Written by a separate offline
        sampling job, not the daily analyzer -- may lag the _1rm/_volume
        series by a run."""
        return self._read_df_or_empty(self.strength_curve_path(dataset, exercise))

    def write_strength_curve(self, dataset: str, exercise: str, df: pd.DataFrame) -> None:
        if df.empty:
            return
        self.file_manager.write_df(
            self._prepare_for_parquet(df),
            self.strength_curve_path(dataset, exercise),
            format="parquet",
        )

    @staticmethod
    def viewer_cache_path(name: str) -> str:
        return f"viewer_cache/{name}.json"

    def load_viewer_cache(self, name: str) -> dict | None:
        """Read a precomputed web-response JSON blob (written by
        garmin.scripts.manual_build_viewer_cache), or None if it hasn't been
        built yet. A cache miss is not an error -- callers fall back to
        assembling the payload live from curated/analyzed parquet.
        """
        try:
            text = self.file_manager.read_text(self.viewer_cache_path(name))
        except FileNotFoundError:
            return None
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code")
            if error_code in {"404", "NoSuchKey"}:
                return None
            raise
        return json.loads(text)

    def write_viewer_cache(self, name: str, payload: dict) -> None:
        self.file_manager.write_text(json.dumps(payload), self.viewer_cache_path(name))

    @staticmethod
    def viewer_cache_html_path(name: str) -> str:
        return f"viewer_cache/{name}.html"

    def load_viewer_cache_html(self, name: str) -> str | None:
        """Same as load_viewer_cache but for a precomputed full HTML page
        (e.g. the activity-explorer prototype) rather than a JSON payload --
        None on a cache miss, same fallback contract."""
        try:
            return self.file_manager.read_text(self.viewer_cache_html_path(name))
        except FileNotFoundError:
            return None
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code")
            if error_code in {"404", "NoSuchKey"}:
                return None
            raise

    def write_viewer_cache_html(self, name: str, html: str) -> None:
        self.file_manager.write_text(html, self.viewer_cache_html_path(name))

    def write_detailed_day(self, dataset: str, query_date: str, df: pd.DataFrame) -> None:
        if df.empty:
            return

        day_df = df.copy()
        day_df["query_date"] = pd.to_datetime(day_df["query_date"]).dt.date
        self.file_manager.write_df(
            self._prepare_for_parquet(day_df),
            self.detailed_dataset_path(dataset, query_date),
            format="parquet",
        )

    def _read_df_or_empty(self, path: str) -> pd.DataFrame:
        try:
            return self.file_manager.read_df(path, format="parquet")
        except FileNotFoundError:
            return pd.DataFrame()
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code")
            if error_code in {"404", "NoSuchKey"}:
                return pd.DataFrame()
            raise

    @staticmethod
    def _prepare_for_parquet(df: pd.DataFrame) -> pd.DataFrame:
        output = df.copy()
        for column in ["date", "query_date", "date_pulled", "start_gmt", "end_gmt", "timestamp", "start_time"]:
            if column in output.columns:
                output[column] = pd.to_datetime(output[column])
        if "date_time_utc" in output.columns:
            output["date_time_utc"] = pd.to_datetime(output["date_time_utc"], utc=True)
        return output