from __future__ import annotations

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
        for column in ["date", "query_date", "date_pulled", "start_gmt", "end_gmt"]:
            if column in output.columns:
                output[column] = pd.to_datetime(output[column])
        if "date_time_utc" in output.columns:
            output["date_time_utc"] = pd.to_datetime(output["date_time_utc"], utc=True)
        return output