## garmin/pullers/health.py

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from tqdm.auto import tqdm

class HealthPuller:
    def __init__(self, session):
        self.session = session
        self._pull_configs = {
            'weight': {
                'url_template': "/weight-service/weight/range/{start_date}/{end_date}?includeAll=true",
                'mapping': {
                    'weight': 'weight',
                    'bmi': 'bmi',
                    'bodyFat': 'body_fat',
                    'bodyWater': 'body_water',
                    'boneMass': 'bone_mass',
                    'muscleMass': 'muscle_mass',
                },
                'response_path': ["dailyWeightSummaries"],
                'values_field': "allWeightMetrics",
                'date_field': "summaryDate",
                'chunk_days': 1000,
                'post_processing': self._post_process_weight,
            },
            'steps': {
                'url_template': "usersummary-service/stats/daily/{start_date}/{end_date}?statsType=STEPS",
                'mapping': {
                    'stepGoal': 'step_goal',
                    'totalSteps': 'total_steps',
                    'totalDistance': 'total_distance',
                },
                'response_path': ["values"],
                'date_field': "calendarDate",
                'values_field': "values",
                'chunk_days': 28,
            },
            'sleep': {
                'url_template': "/sleep-service/stats/sleep/daily/{start_date}/{end_date}",
                'mapping': {
                    'remTime': 'rem_time',
                    'restingHeartRate': 'resting_hr',
                    'localSleepStartTimeInMillis': 'local_sleep_start_time',
                    'localSleepEndTimeInMillis': 'local_sleep_time_end',
                    'gmtSleepStartTimeInMillis': 'gmt_sleep_start_time',
                    'gmtSleepEndTimeInMillis': 'gmt_sleep_end_time',
                    'totalSleepTimeInSeconds': 'total_sleep_time',
                    'deepTime': 'deep_time',
                    'awakeTime': 'awake_time',
                    'lightTime': 'light_time',
                    'sleepScoreQuality': 'sleep_score_quality',
                    'respiration': 'respiration',
                    'spO2': 'spo2',
                    'hrvStatus': 'hrv_status',
                    'sleepNeed': 'sleep_need',
                    'bodyBatteryChange': 'body_battery_change',
                    'skinTempF': 'skin_temp_f',
                    'skinTempC': 'skin_temp_c',
                    'hrv7dAverage': 'hrv_7d_average',
                    'sleepScore': 'sleep_score',
                },
                'response_path': ["individualStats"],
                'date_field': "calendarDate",
                'values_field': "values",
                'chunk_days': 28,
            },
            'heart_rate': {
                'url_template': "/usersummary-service/stats/heartRate/daily/{start_date}/{end_date}",
                'mapping': {
                    "restingHR": "resting_hr",
                    "wellnessMaxAvgHR": "wellness_max_avg_hr",
                    "wellnessMinAvgHR": "wellness_min_avg_hr",
                },
                'response_path': None,
                'date_field': "calendarDate",
                'values_field': "values",
                'chunk_days': 28,
            },
            'stress': {
                'url_template': "/usersummary-service/stats/stress/daily/{start_date}/{end_date}",
                'mapping': {
                    "highStressDuration": "high_stress_duration",
                    "lowStressDuration": "low_stress_duration",
                    "overallStressLevel": "overall_stress_level",
                    "restStressDuration": "rest_stress_duration",
                },
                'response_path': None,
                'date_field': "calendarDate",
                'values_field': "values",
                'chunk_days': 28,
            },
            'body_battery': {
                'url_template': "/usersummary-service/stats/bodybattery/daily/{start_date}/{end_date}",
                'mapping': {
                    "lowBodyBattery": "low_body_battery",
                    "highBodyBattery": "high_body_battery",
                },
                'response_path': None,
                'date_field': "calendarDate",
                'values_field': "values",
                'chunk_days': 28,
            },
        }

    def _pull(self,
              url_template: str,
              mapping: dict[str,str],
              start_date: str,
              end_date: str,
              response_path: list[str] = None,
              date_field: str = 'calendarDate',
              values_field: str = 'values',
              chunk_days: int = 28,
              show_progress: bool = True) -> pd.DataFrame:
        
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.strptime(end_date,   "%Y-%m-%d").date()
        if start > end:
            raise ValueError("start_date must be <= end_date")

        total_days = (end - start).days + 1
        n_chunks   = (total_days + chunk_days - 1) // chunk_days

        chunks = []
        chunk_start = start
        with tqdm(total=n_chunks, desc="Pulling time series", disable=not show_progress) as pbar:
            while chunk_start <= end:
                chunk_end = min(chunk_start + timedelta(days=chunk_days-1), end)
                url = url_template.format(start_date=chunk_start, end_date=chunk_end)
                res = self.session.get(url)

                # Pull out the data in case it's nested
                if response_path:
                    for key in response_path:
                        if res is None:
                            break
                        res = res.get(key, [])
                if res is None:
                    chunk_start = chunk_end + timedelta(days=1)
                    pbar.update(1)
                    continue

                rows = []
                for entry in res:
                    values = entry.get(values_field, entry)
                    if isinstance(values, dict):
                        values = [values]
                    for measurement in values:
                        row = {"date": entry[date_field]}
                        for key, val in mapping.items():
                            row[val] = measurement.get(key, np.nan)
                        rows.append(row)

                df_chunk = pd.DataFrame(rows, columns=["date"] + list(mapping.values()))
                chunks.append(df_chunk)
                chunk_start = chunk_end + timedelta(days=1)

                pbar.update(1)

        if not chunks:
            return pd.DataFrame(columns=["date"] + list(mapping.values())).set_index("date")

        df = pd.concat(chunks, ignore_index=True)
        df["date"] = pd.to_datetime(df["date"])
        return df.set_index("date").sort_index()

    def pull_data(self, data_type: str, start_date=None, end_date=None, show_progress=True) -> pd.DataFrame:
        if data_type not in self._pull_configs:
            raise ValueError(f"Unsupported data type: {data_type}")
        
        config = self._pull_configs[data_type]
        df = self._pull(
            url_template=config['url_template'],
            mapping=config['mapping'],
            start_date=start_date or "2017-01-01",
            end_date=end_date or datetime.today().strftime("%Y-%m-%d"),
            response_path=config.get('response_path'),
            date_field=config.get('date_field', 'calendarDate'),
            values_field=config.get('values_field', 'values'),
            chunk_days=config.get('chunk_days', 28),
            show_progress=show_progress
        )
        
        # Apply any post-processing if defined
        if 'post_processing' in config and callable(config['post_processing']):
            df = config['post_processing'](df)
        
        return df

    def _post_process_weight(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.groupby(df.index).mean().asfreq("D")
        for col in ['weight', 'muscle_mass', 'bone_mass']:
            df[col] = df[col] * 0.00220462
        df['fat_mass'] = df['body_fat'] * df['weight'] / 100.
        return df