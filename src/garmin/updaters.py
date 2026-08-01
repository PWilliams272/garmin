## garmin/updaters.py

import pandas as pd
import time
from datetime import datetime, timedelta
from tqdm import tqdm
from typing import Callable
from garmin.io.db_manager import DatabaseManager
from garmin.io.curated_store import CuratedDataStore
from garmin.pullers.health import HealthPuller
from garmin.pullers.health_detailed import HealthDetailedPuller
from garmin.pullers.activities import ActivityPuller
from sqlalchemy.dialects.postgresql import insert
from garmin.io.models import (
    HealthStats, Steps, Sleep, Stress, BodyBattery, HeartRate, HRV, Respiration,
    HeartRateDetailed, SpO2Detailed, StepsDetailed, RespirationDetailed
)


# Every curated/activities/summary/<dataset>.parquet dataset name the
# activity pipeline knows about -- kept as a plain module-level list (rather
# than only living inside DataUpdater._activity_type_registry) so the web
# app can enumerate real activity datasets without needing a Garmin session.
ACTIVITY_DATASETS = [
    "running", "strength", "cycling", "indoor_cycling", "hiking",
    "lap_swimming", "open_water_swimming", "hiit", "bouldering",
    "rock_climbing", "tennis", "pickleball",
]


def convert_nulls(df):
    # Convert all NaT in datetime columns to None
    for col in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            df[col] = df[col].apply(lambda x: x if pd.notnull(x) else None)
    # Convert all remaining NaN to None for non-datetime columns
    df = df.where(pd.notnull(df), None)
    return df


class DataUpdater:
    def __init__(
        self,
        session,
        db_manager=None,
        curated_store=None,
        health_puller=None,
        health_detailed_puller=None,
        activity_puller=None,
    ):
        self.curated_store = curated_store
        if self.curated_store is not None:
            self.db = db_manager
        else:
            self.db = db_manager or DatabaseManager()
        self.health_puller = health_puller or HealthPuller(session)
        self.health_detailed_puller = health_detailed_puller or HealthDetailedPuller(session)
        self.activity_puller = activity_puller or ActivityPuller(session)
        
        self.pull_fn_map = {
            HealthStats: lambda **kwargs: self.health_puller.pull_data('weight', **kwargs),
            Steps: lambda **kwargs: self.health_puller.pull_data('steps', **kwargs),
            Sleep: lambda **kwargs: self.health_puller.pull_data('sleep', **kwargs),
            Stress: lambda **kwargs: self.health_puller.pull_data('stress', **kwargs),
            BodyBattery: lambda **kwargs: self.health_puller.pull_data('body_battery', **kwargs),
            HeartRate: lambda **kwargs: self.health_puller.pull_data('heart_rate', **kwargs),
            HRV: lambda **kwargs: self.health_puller.pull_data('hrv', **kwargs),
            Respiration: lambda **kwargs: self.health_puller.pull_data('respiration', **kwargs),
            HeartRateDetailed: lambda **kwargs: self.health_detailed_puller.pull_data('heart_rate', **kwargs),
            RespirationDetailed: lambda **kwargs: self.health_detailed_puller.pull_data('respiration', **kwargs),
            SpO2Detailed: lambda **kwargs: self.health_detailed_puller.pull_data('spo2', **kwargs),
            StepsDetailed: lambda **kwargs: self.health_detailed_puller.pull_data('steps', **kwargs),
        }
        self.updater_map = {
            HealthStats: self._update_daily_time_series,
            Steps: self._update_daily_time_series,
            Sleep: self._update_daily_time_series,
            Stress: self._update_daily_time_series,
            BodyBattery: self._update_daily_time_series,
            HeartRate: self._update_daily_time_series,
            HRV: self._update_daily_time_series,
            Respiration: self._update_daily_time_series,
            HeartRateDetailed: self._update_detailed_time_series,
            RespirationDetailed: self._update_detailed_time_series,
            SpO2Detailed: self._update_detailed_time_series,
            StepsDetailed: self._update_detailed_time_series,
        }


    def _update_daily_time_series(
        self,
        model_class,
        start_date: str = "2015-01-01",
        batch_size: int = 100,
    ):
        if self.curated_store is not None:
            return self._update_daily_time_series_curated(model_class, start_date=start_date)

        pull_fn = self.pull_fn_map.get(model_class)
        if pull_fn is None:
            raise ValueError(f"No puller found for {model_class.__name__}")
        existing_records = self.db.get_records(model_class)
        if existing_records:
            last_date = max(r.date for r in existing_records)
            start_date = last_date.strftime("%Y-%m-%d")

        today = datetime.today().date()
        df = pull_fn(start_date=start_date, end_date=today.strftime("%Y-%m-%d"))
        if df.empty:
            print(f"No {model_class.__tablename__} data returned from Garmin.")
            return

        df["date"] = df.index.date if isinstance(df.index, pd.DatetimeIndex) else df["date"]
        df["date_pulled"] = today
        print('Data pulled, upserting')

        session = self.db.Session()
        try:
            batch = []
            for i, (_, row) in enumerate(tqdm(df.iterrows(), total=len(df)), 1):
                data = row.to_dict()
                stmt = insert(model_class).values(**data)
                update_cols = {c: stmt.excluded[c] for c in data if c != "id"}
                stmt = stmt.on_conflict_do_update(index_elements=["date"], set_=update_cols)
                batch.append(stmt)

                if i % batch_size == 0:
                    for b in batch:
                        session.execute(b)
                    session.commit()
                    batch = []

            # Final batch
            for b in batch:
                session.execute(b)
            session.commit()

        except Exception as e:
            session.rollback()
            print("Error upserting records:", e)
        finally:
            session.close()

        print(f"Upserted {len(df)} rows into {model_class.__tablename__}.")

    def _update_daily_time_series_curated(
        self,
        model_class,
        start_date: str = "2015-01-01",
    ):
        pull_fn = self.pull_fn_map.get(model_class)
        if pull_fn is None:
            raise ValueError(f"No puller found for {model_class.__name__}")

        dataset_name = model_class.__tablename__
        existing_df = self.curated_store.load_daily(dataset_name)
        if not existing_df.empty and "date" in existing_df.columns:
            last_date = pd.to_datetime(existing_df["date"]).dt.date.max()
            start_date = last_date.strftime("%Y-%m-%d")

        today = datetime.today().date()
        df = pull_fn(start_date=start_date, end_date=today.strftime("%Y-%m-%d"))
        if df.empty:
            print(f"No {dataset_name} data returned from Garmin.")
            return

        normalized = df.copy()
        if isinstance(normalized.index, pd.DatetimeIndex):
            if "date" not in normalized.columns:
                normalized = normalized.reset_index()
                index_column = normalized.columns[0]
                normalized["date"] = pd.to_datetime(normalized[index_column]).dt.date
                if index_column != "date":
                    normalized = normalized.drop(columns=[index_column])
            else:
                normalized["date"] = pd.to_datetime(normalized["date"]).dt.date
        else:
            normalized["date"] = pd.to_datetime(normalized["date"]).dt.date
        normalized["date_pulled"] = today

        merged = self.curated_store.merge_daily(dataset_name, normalized)
        print(f"Saved {len(merged)} curated rows for {dataset_name}.")

    def _update_detailed_time_series(
        self,
        model_class,
        start_date: str = "2015-01-01",
        batch_size: int = 100,
    ):
        if self.curated_store is not None:
            return self._update_detailed_time_series_curated(model_class, start_date=start_date)

        pull_fn = self.pull_fn_map.get(model_class)
        if pull_fn is None:
            raise ValueError(f"No puller found for {model_class.__name__}")

        today = datetime.today().date()
        session = self.db.Session()

        # Find which query dates to pull
        existing = session.query(model_class.query_date, model_class.pull_status).all()
        existing_status = {r.query_date: r.pull_status for r in existing}

        date_list = pd.date_range(start=start_date, end=today).date
        to_pull = [
            d.strftime('%Y-%m-%d') for d in date_list
            if existing_status.get(d) not in {"fetched", "no_data"}
        ]
        if not to_pull:
            print(f"No dates to pull for {model_class.__tablename__}.")
            return
        
        df = pull_fn(dates=to_pull)
        pulled_on = datetime.today().date()

        # Map pull status to each record
        status_map = getattr(self.health_detailed_puller, "_last_pull_status", {})

        df["date_pulled"] = pulled_on
        df["pull_status"] = df["query_date"].apply(
            lambda d: "fetched" if d in status_map['fetched'] else
                    "denied"  if d in status_map['denied']  else
                    "no_data" if d in status_map['no_data'] else "unknown"
        )
        # Add missing `no data` dates with minimal records
        for d in set(status_map['no_data']) | set(status_map['denied']):
            if d in df["query_date"]:
                continue
            if d in status_map['no_data']:
                status = 'no_data'
            elif d in status_map['denied']:
                status = 'denied'
            df = pd.concat([
                df,
                pd.DataFrame([{
                    "query_date": d,
                    "date_time_utc": datetime.combine(pd.to_datetime(d), datetime.max.time()),
                    "date_pulled": pulled_on,
                    "pull_status": status
                }])
            ], ignore_index=True)
        
        dt_cols = ['query_date', 'date_pulled', 'start_gmt', 'end_gmt']
        for col in dt_cols:
            if col in df.columns:
                df[col] = pd.to_datetime(df[col]).dt.date
        df["date_time_utc"] = pd.to_datetime(df["date_time_utc"], utc=True)
        df = convert_nulls(df)

        print(f"Upserting {len(df)} rows to {model_class.__tablename__}")
        try:
            batch = []
            for i, (_, row) in enumerate(tqdm(df.iterrows(), total=len(df)), 1):
                data = row.to_dict()
                stmt = insert(model_class).values(**data)
                update_cols = {c: stmt.excluded[c] for c in data if c != "id"}
                stmt = stmt.on_conflict_do_update(
                    index_elements=["date_time_utc"],
                    set_=update_cols
                )
                batch.append(stmt)

                if i % batch_size == 0:
                    for s in batch:
                        session.execute(s)
                    session.commit()
                    batch = []

            for s in batch:
                session.execute(s)
            session.commit()
        except Exception as e:
            session.rollback()
            print(f"Error during upsert of {model_class.__tablename__}:", e)
        finally:
            session.close()

    def _update_detailed_time_series_curated(
        self,
        model_class,
        start_date: str = "2015-01-01",
    ):
        pull_fn = self.pull_fn_map.get(model_class)
        if pull_fn is None:
            raise ValueError(f"No puller found for {model_class.__name__}")

        dataset_name = model_class.__tablename__
        today = datetime.today().date()

        existing_status_df = self.curated_store.load_detailed_status(dataset_name)
        existing_status: dict = {}
        if not existing_status_df.empty and {"query_date", "pull_status"}.issubset(existing_status_df.columns):
            normalized_status_df = existing_status_df.copy()
            normalized_status_df["query_date"] = pd.to_datetime(normalized_status_df["query_date"]).dt.date
            if "date_pulled" in normalized_status_df.columns:
                normalized_status_df["date_pulled"] = pd.to_datetime(
                    normalized_status_df["date_pulled"],
                    errors="coerce",
                ).dt.date
            else:
                normalized_status_df["date_pulled"] = pd.NaT

            existing_status = {
                row.query_date: (row.pull_status, row.date_pulled)
                for row in normalized_status_df.itertuples(index=False)
            }

        date_list = list(reversed(pd.date_range(start=start_date, end=today).date))

        def should_pull(query_date) -> bool:
            status_info = existing_status.get(query_date)
            if status_info is None:
                return True

            pull_status, date_pulled = status_info
            if date_pulled == query_date:
                return True

            return pull_status not in {"fetched", "no_data"}

        to_pull = [
            d.strftime('%Y-%m-%d') for d in date_list
            if should_pull(d)
        ]
        if not to_pull:
            print(f"No dates to pull for {dataset_name}.")
            return

        print(
            f"Starting curated detailed update for {dataset_name}: "
            f"{len(to_pull)} dates queued."
        )
        pull_started_at = time.perf_counter()
        df = pull_fn(dates=to_pull)
        pull_elapsed = time.perf_counter() - pull_started_at
        status_map = getattr(self.health_detailed_puller, "_last_pull_status", {})
        fetched_dates = status_map.get("fetched", [])
        no_data_dates = status_map.get("no_data", [])
        denied_dates = status_map.get("denied", [])

        print(
            f"Detailed pull for {dataset_name} finished in {pull_elapsed:.2f}s: "
            f"{len(fetched_dates)} fetched, {len(no_data_dates)} no_data, "
            f"{len(denied_dates)} denied."
        )

        if not df.empty:
            detailed_df = df.copy()
            detailed_df["query_date"] = pd.to_datetime(detailed_df["query_date"]).dt.date
            if "date_time_utc" in detailed_df.columns:
                detailed_df["date_time_utc"] = pd.to_datetime(detailed_df["date_time_utc"], utc=True)
            detailed_df["date_pulled"] = today
            detailed_df["pull_status"] = detailed_df["query_date"].apply(
                lambda d: "fetched" if d.strftime("%Y-%m-%d") in fetched_dates else "unknown"
            )
            detailed_df = convert_nulls(detailed_df)

            write_started_at = time.perf_counter()
            write_groups = 0
            for query_date, day_df in detailed_df.groupby("query_date", sort=True):
                self.curated_store.write_detailed_day(
                    dataset_name,
                    query_date.isoformat(),
                    day_df.reset_index(drop=True),
                )
                write_groups += 1

            write_elapsed = time.perf_counter() - write_started_at
            print(
                f"Wrote {write_groups} detailed day files for {dataset_name} "
                f"in {write_elapsed:.2f}s."
            )

        status_rows = [
            {
                "query_date": pulled_date,
                "date_pulled": today,
                "pull_status": "fetched",
            }
            for pulled_date in fetched_dates
        ]
        status_rows.extend(
            {
                "query_date": pulled_date,
                "date_pulled": today,
                "pull_status": "no_data",
            }
            for pulled_date in no_data_dates
        )
        status_rows.extend(
            {
                "query_date": pulled_date,
                "date_pulled": today,
                "pull_status": "denied",
            }
            for pulled_date in denied_dates
        )

        if status_rows:
            status_started_at = time.perf_counter()
            self.curated_store.merge_detailed_status(dataset_name, pd.DataFrame(status_rows))
            status_elapsed = time.perf_counter() - status_started_at
            print(
                f"Merged {len(status_rows)} detailed status rows for {dataset_name} "
                f"in {status_elapsed:.2f}s."
            )

        print(
            "Saved curated detailed data for "
            f"{dataset_name}: {len(fetched_dates)} fetched, "
            f"{len(no_data_dates)} no_data, {len(denied_dates)} denied."
        )

    def _activity_type_registry(self) -> list[dict]:
        """Dataset name -> (Garmin typeKey, summary puller, optional per-activity detail puller).

        Registry-driven so adding a sport is one entry here rather than a new
        `_update_<sport>_curated` method. `summary_fn(start_date, end_date)` must
        return a DataFrame with an `activity_id` column (matches ActivityPuller's
        existing pull_running_summary/pull_strength_summary/pull_cardio_summary
        shape). `detail_fn(activity_id)`, if given, is called once per activity
        in the new/updated summary and written via write_activity_detail.

        typeKeys beyond "running"/"strength_training" haven't been confirmed
        against a live Garmin response yet -- verify/adjust these against a
        real pull_activity_list() result if a sport's data doesn't come back
        as expected.
        """
        cardio = self.activity_puller.pull_cardio_summary
        entries = [
            {
                "dataset": "running", "activity_type": "running",
                "summary_fn": self.activity_puller.pull_running_summary,
                "detail_fn": self.activity_puller.get_activity_detail_timeseries,
                "detail_dataset": "running_timeseries",
            },
            {
                "dataset": "strength", "activity_type": "strength_training",
                "summary_fn": self.activity_puller.pull_strength_summary,
                "detail_fn": self.activity_puller.get_strength_workout,
            },
        ]
        bespoke_datasets = {e["dataset"] for e in entries}
        for dataset in ACTIVITY_DATASETS:
            if dataset in bespoke_datasets:
                continue
            entries.append({
                "dataset": dataset, "activity_type": dataset,
                "summary_fn": lambda s, e, t=dataset: cardio(t, s, e),
                # Every non-strength sport goes through the same FIT-first/
                # JSON-fallback per-point puller as running (see
                # get_activity_detail_timeseries) -- it degrades gracefully
                # (empty df, skipped below) for sports with no GPS/FIT data.
                "detail_fn": self.activity_puller.get_activity_detail_timeseries,
                "detail_dataset": f"{dataset}_timeseries",
            })
        return entries

    def _update_activity_curated(
        self, dataset: str, summary_fn, detail_fn=None, detail_dataset: str | None = None,
        start_date: str = "2015-01-01",
    ) -> None:
        existing = self.curated_store.load_activity_summary(dataset)
        if not existing.empty and "date" in existing.columns:
            last_date = pd.to_datetime(existing["date"]).dt.date.max()
            start_date = last_date.strftime("%Y-%m-%d")

        today = datetime.today().date()
        summary_df = summary_fn(start_date, today.strftime("%Y-%m-%d"))
        if summary_df.empty:
            print(f"No new {dataset} activities.")
            return

        merged = self.curated_store.merge_activity_summary(dataset, summary_df)

        if detail_fn is not None:
            detail_dataset = detail_dataset or dataset
            for activity_id in summary_df["activity_id"]:
                detail_df = detail_fn(activity_id)
                if detail_df.empty:
                    continue
                detail_df["activity_id"] = activity_id
                self.curated_store.write_activity_detail(detail_dataset, activity_id, detail_df)

        print(f"Saved {len(merged)} curated {dataset} activities ({len(summary_df)} new/updated).")

    def _update_all_activities_curated(self) -> None:
        for entry in self._activity_type_registry():
            self._update_activity_curated(
                entry["dataset"], entry["summary_fn"], entry.get("detail_fn"),
                entry.get("detail_dataset"),
            )

    def backfill_activity_details(
        self, dataset: str, detail_fn, detail_dataset: str | None = None, limit: int | None = None,
    ) -> dict:
        """Fill in curated/activities/detail/<detail_dataset>/ for activities
        that already have a summary row but no detail file yet -- covers
        history pulled before detail_fn was wired into the daily registry
        (_update_activity_curated only pulls details for *new* activities
        each run). Resumable: safe to re-run, only touches ids missing a
        detail file, so a partial run (rate limit, timeout, Ctrl-C) can just
        be re-invoked. Returns counts for the caller to report/log.
        """
        detail_dataset = detail_dataset or dataset
        summary = self.curated_store.load_activity_summary(dataset)
        if summary.empty:
            return {"dataset": dataset, "total": 0, "already_had_detail": 0, "fetched": 0, "empty": 0}

        prefix = f"curated/activities/detail/{detail_dataset}/"
        existing_files = self.curated_store.file_manager.list_files(prefix)
        existing_ids = {
            f.rsplit("activity_id=", 1)[-1].removesuffix(".parquet")
            for f in existing_files if f.endswith(".parquet")
        }

        all_ids = summary["activity_id"].astype(str).tolist()
        missing_ids = [aid for aid in all_ids if aid not in existing_ids]
        already_had_detail = len(all_ids) - len(missing_ids)
        if limit is not None:
            missing_ids = missing_ids[:limit]

        fetched, empty = 0, 0
        for activity_id in missing_ids:
            detail_df = detail_fn(activity_id)
            if detail_df.empty:
                empty += 1
                continue
            detail_df["activity_id"] = activity_id
            self.curated_store.write_activity_detail(detail_dataset, activity_id, detail_df)
            fetched += 1

        return {
            "dataset": dataset, "total": len(all_ids),
            "already_had_detail": already_had_detail,
            "fetched": fetched, "empty": empty,
        }

    def backfill_all_activity_details(self, limit_per_dataset: int | None = None) -> list[dict]:
        results = []
        for entry in self._activity_type_registry():
            detail_fn = entry.get("detail_fn")
            if detail_fn is None:
                continue
            result = self.backfill_activity_details(
                entry["dataset"], detail_fn, entry.get("detail_dataset"), limit_per_dataset,
            )
            results.append(result)
            print(
                f"[{result['dataset']}] {result['fetched']} fetched, "
                f"{result['already_had_detail']} already had detail, "
                f"{result['empty']} empty of {result['total']} total."
            )
        return results

    def _resolve_model_class(self, class_or_name: str | type) -> type:
        if isinstance(class_or_name, str):
            # Avoid circular imports
            from garmin.io import models  
            model_class = getattr(models, class_or_name, None)
            if model_class is None:
                raise ValueError(f"No model class named '{class_or_name}' found in models.")
            return model_class
        return class_or_name

    def update(self,
               model_class,
               start_date: str = "2015-01-01",
               batch_size: int = 100):
        model_class = self._resolve_model_class(model_class)
        self.updater_map[model_class](
            model_class,
            start_date=start_date,
            batch_size=batch_size
        )

    def update_all(self):
        model_class_list = [
            "HealthStats", "Steps", "Sleep", "Stress", "BodyBattery", "HeartRate",
            "HRV", "Respiration",
            "HeartRateDetailed", "SpO2Detailed",
            "StepsDetailed", "RespirationDetailed"
        ]
        for model_class in model_class_list:
            self.update(model_class)

        if self.curated_store is not None:
            self._update_all_activities_curated()