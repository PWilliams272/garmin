## garmin/updaters.py

import pandas as pd
from datetime import datetime, timedelta
from tqdm import tqdm
from typing import Callable
from garmin.db.db_manager import DatabaseManager
from garmin.pullers.health import HealthPuller
from garmin.pullers.health_detailed import HealthDetailedPuller
from sqlalchemy.dialects.postgresql import insert
from garmin.db.models import (
    HealthStats, Steps, Sleep, Stress, BodyBattery, HeartRate,
    HeartRateDetailed, SpO2Detailed, StepsDetailed, RespirationDetailed
)

class DataUpdater:
    def __init__(
        self,
        session,
        db_manager=None,
        health_puller=None,
        health_detailed_puller=None,
        activity_puller=None,
    ):
        self.db = db_manager or DatabaseManager()
        self.health_puller = health_puller or HealthPuller(session)
        self.health_detailed_puller = health_detailed_puller or HealthDetailedPuller(session)
        #self.activity_puller = activity_puller or ActivityPuller(session)
        
        self.pull_fn_map = {
            HealthStats: lambda **kwargs: self.health_puller.pull_data('weight', **kwargs),
            Steps: lambda **kwargs: self.health_puller.pull_data('steps', **kwargs),
            Sleep: lambda **kwargs: self.health_puller.pull_data('sleep', **kwargs),
            Stress: lambda **kwargs: self.health_puller.pull_data('stress', **kwargs),
            BodyBattery: lambda **kwargs: self.health_puller.pull_data('body_battery', **kwargs),
            HeartRate: lambda **kwargs: self.health_puller.pull_data('heart_rate', **kwargs),
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

    def _update_detailed_time_series(
        self,
        model_class,
        start_date: str = "2015-01-01",
        batch_size: int = 100,
    ):
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

    def _resolve_model_class(self, class_or_name: str | type) -> type:
        if isinstance(class_or_name, str):
            # Avoid circular imports
            from garmin.db import models  
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
            "HeartRateDetailed", "SpO2Detailed",
            "StepsDetailed", "RespirationDetailed"
        ]
        for model_class in model_class_list:
            self.update(model_class)