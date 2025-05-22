## garmin/pullers/health_detailed.py

import pandas as pd
import numpy as np
import time
from datetime import datetime
from tqdm.auto import tqdm
from typing import Callable

class NoDataAvailable(Exception):
    """No historical data available for the given date."""

class CacheWarmDenied(Exception):
    """Garmin refused to warm the cache (rate limit hit)."""

class CacheWarmRequested(Exception):
    """We POSTed a cache‐warm and need to retry once data is ready."""

class HealthDetailedPuller:
    def __init__(self, session):
        self.session = session
        self._cache_warm_denied = False
        self._last_pull_status: dict[str, list[str]] = {}
        self._pull_configs = {
            'heart_rate': {
                'url_template': 'wellness-service/wellness/dailyHeartRate?date={date}',
                'mapping': {
                    'timestamp': 'timestamp',
                    'heartrate': 'hr',
                },
                'availability_key': 'restingHeartRate',
                'descriptors_key': 'heartRateValueDescriptors',
                'values_key': 'heartRateValues',
            },
            'respiration': {
                'url_template': 'wellness-service/wellness/daily/respiration/{date}',
                'mapping': {
                    'timestamp': 'timestamp',
                    'respiration': 'respiration',
                },
                'availability_key': 'lowestRespirationValue',
                'descriptors_key': 'respirationValueDescriptorsDTOList',
                'values_key': 'respirationValuesArray',
            },
            'spo2': {
                'url_template': 'wellness-service/wellness/daily/spo2acclimation/{date}',
                'mapping': {
                    'timestamp': 'timestamp',
                    'spo2Level': 'spo2_level',
                    'monitoringEnvironmentLevel': 'monitoring_environment_level',
                },
                'availability_key': 'averageSpO2',
                'descriptors_key': 'spO2HourlyAveragesDescriptorList',
                'values_key': 'spO2HourlyAverages',
                'descriptors_key_map': {'index': 'spo2ValueDescIndex', 'key': 'spo2ValueDescKey'},
            },
            'steps': {
                'url_template': "wellness-service/wellness/dailySummaryChart/?date={date}",
                'mapping': {
                    'startGMT': 'start_gmt',
                    'endGMT': 'end_gmt',
                    'steps': 'steps',
                    'pushes': 'pushes',
                    'primaryActivityLevel': 'primary_activity_level',
                    'activityLevelConstant': 'activity_level_constant',
                },
                'availability_key': 'steps',
                'descriptors_key': None,
                'values_key': None
            },
        }

    def _pull(self,
              url_template: str,
              mapping: dict[str,str],
              date: str,
              availability_key: str,
              descriptors_key: str = None,
              values_key: str = None,
              descriptors_key_map: dict[str, str] = {'index': 'index', 'key': 'key'}
        ) -> pd.DataFrame:
        """
        Single-day grab.  If `descriptors_key` is given we use the descriptor→values logic
        and support cache‐warm.  Otherwise we treat `res` as a flat JSON for chart data.
        In either case, `availability_key` tells us which field to look at to decide
        “really no data” vs “might need cache‐warm.”
        """
        url = url_template.format(date=date)
        res = self.session.get(url)

        # Descriptor-based endpoints (e.g., heart rate, respiration, spo2)
        if descriptors_key:
            descriptors = res.get(descriptors_key) or []
            # No descriptors field, need to determine why
            if not descriptors:
                if res.get(availability_key) is None:
                    raise NoDataAvailable(date)
                
                # If availability_key exists, then data are missing,
                # but we can likely get garmin to warm the cache. This
                # is the "Reload Chart" button in the web UI
                if self._cache_warm_denied:
                    # Already tried to warm the cache, but Garmin refused
                    raise CacheWarmDenied(date)

                post_url = f'wellness-service/wellness/epoch/request/{date}'
                post_res = self.session.post(post_url)
                if post_res.get("status") == "DENIED":
                    self._cache_warm_denied = True
                    raise CacheWarmDenied(date)
                else:
                    # Cache warm request was accepted, but need to wait a
                    # few seconds before it'll be available
                    raise CacheWarmRequested(date)
            
            cols = [None] * len(res[descriptors_key])
            for desc in res[descriptors_key]:
                cols[desc[descriptors_key_map['index']]] = desc[descriptors_key_map['key']]
            df = pd.DataFrame(res[values_key], columns=cols)
            df = df.rename(columns=mapping)
            df['query_date'] = date
            if 'timestamp' in df.columns:
                df['date_time_utc'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
            return df

        # Table-based endpoints (e.g., steps)
        if res == []:
            raise NoDataAvailable(date)
        df = pd.DataFrame(res).rename(columns=mapping)
        # If all values are zero, might need to warm cache
        if (df[availability_key] == 0).all():
            if self._cache_warm_denied:
                raise CacheWarmDenied(date)
            post_res = self.session.post(
                f"wellness-service/wellness/epoch/request/{date}"
            )
            if post_res.get("status") == "DENIED":
                self._cache_warm_denied = True
                raise CacheWarmDenied(date)
            else:
                raise CacheWarmRequested(date)
        df['query_date'] = date
        # Otherwise return the data
        if 'start_gmt' in df.columns:
            df['date_time_utc'] = pd.to_datetime(df['start_gmt'], utc=True)
        return df

    def _pull_for_range(self,
                        pull_one_day: Callable[[str],pd.DataFrame],
                        start_date: str = None,
                        end_date: str = None,
                        dates: list[str] = None) -> pd.DataFrame:
        """Loop over single‐day pulls, batch cache warms, then retry once."""
        if dates is not None:
            date_list = sorted(set(dates))  # Ensure no duplicates and sorted
        elif start_date and end_date:
            start = datetime.strptime(start_date, "%Y-%m-%d").date()
            end = datetime.strptime(end_date, "%Y-%m-%d").date()
            if start > end:
                raise ValueError("start_date must be <= end_date")
            date_list = [d.strftime("%Y-%m-%d") for d in pd.date_range(start, end)]
        else:
            raise ValueError("Must provide either dates or both start_date and end_date.")
        
        fetched = []
        no_data = []
        to_retry = []
        denied = []
        df_list = []

        # First pass: try to pull all the data, keeping track of cache warms
        # and denied requests
        for date in tqdm(date_list, desc="Pulling data"):
            try:
                df_day = pull_one_day(date)
                if not df_day.empty:
                    df_list.append(df_day)
                    fetched.append(date)
                else:
                    no_data.append(date)
            except CacheWarmRequested:
                to_retry.append(date)
            except CacheWarmDenied:
                denied.append(date)
            except NoDataAvailable:
                no_data.append(date)
                continue
        
        # Try to get the data after the cache warm requests
        if to_retry:
            time.sleep(1)
            for date in tqdm(to_retry, desc="Retrying cache warm requests"):
                try:
                    df_day = pull_one_day(date)
                    if not df_day.empty:
                        df_list.append(df_day)
                        fetched.append(date)
                    else:
                        no_data.append(date)
                except CacheWarmDenied:
                    denied.append(date)
                except NoDataAvailable:
                    no_data.append(date)

        if denied:
            print(f"Cache warm denied for {len(denied)} dates: {', '.join(denied)}")
        if no_data:
            print(f"No data available for {len(no_data)} dates: {', '.join(no_data)}")
        
        self._last_pull_status = {
            'fetched': fetched,
            'no_data': no_data,
            'denied': denied,
        }
        if df_list:
            return pd.concat(df_list, ignore_index=True)
        else:
            return pd.DataFrame()

    def _pull_single_day(self,
                         date: str,
                         config: dict) -> pd.DataFrame:
        """
        Generic single-day pull function.
        """
        return self._pull(
            url_template=config['url_template'],
            mapping=config['mapping'],
            date=date,
            availability_key=config['availability_key'],
            descriptors_key=config.get('descriptors_key'),
            values_key=config.get('values_key'),
            descriptors_key_map=config.get('descriptors_key_map', {'index': 'index', 'key': 'key'}),
        )

    def _generic_range_pull(self,
                            name: str,
                            start_date=None,
                            end_date=None,
                            dates=None) -> pd.DataFrame:
        if start_date is None:
            start_date = datetime.today().strftime('%Y-%m-%d')
        if end_date is None:
            end_date = start_date
        config = self._pull_configs[name]
        expected_cols = list(config['mapping'].values()) + ['query_date', 'date_time_utc']
        df = self._pull_for_range(
            lambda date: self._pull_single_day(date, config),
            start_date=start_date,
            end_date=end_date,
            dates=dates
        )
        if df.empty:
            df = pd.DataFrame(columns=expected_cols)
        return df

    def pull_data(self,
                  data_type: str,
                  start_date: str = None,
                  end_date: str = None,
                  dates: list[str] = None) -> pd.DataFrame:
        """
        Consolidated method to pull any data type based on `_pull_configs`.
        """
        if data_type not in self._pull_configs:
            raise ValueError(f"Unsupported data type: {data_type}")
        return self._generic_range_pull(data_type, start_date, end_date, dates)