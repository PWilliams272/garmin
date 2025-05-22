# garmin/data_puller/puller.py

import os
import pandas as pd
from datetime import datetime, timedelta, time as dt_time
from getpass import getpass
import garth
from garth.exc import GarthException
import numpy as np
import time
from tqdm.auto import tqdm


from garmin.db.db_manager import (
            DatabaseManager, Activity, StrengthActivity, CyclingActivity,
            RunningActivity, IndoorCyclingActivity, HikingActivity, LapSwimmingActivity,
            VolleyballActivity, RockClimbingActivity, HIITActivity, PickleballActivity,
            TennisActivity, IndoorRunningActivity, OpenWaterSwimmingActivity,
            BoulderingActivity  # if defined
        )

class GarminDataPuller:
    """
    Handles pulling data from Garmin Connect using the garth package.
    """
    def __init__(self, data_dir=None, garth_home=None):
        # Set default data directory relative to this file if not provided.
        if data_dir is None:
            data_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../data'))
        self.data_dir = data_dir
        # Set garth_home relative to data_dir if not provided.
        if garth_home is None:
            self.garth_home = os.path.join(self.data_dir, 'sessions', 'garth')
        else:
            self.garth_home = garth_home

        self.username = os.environ.get('GARMIN_USERNAME')
        self.password = os.environ.get('GARMIN_PASSWORD')
        self.garth = None

    def connect(self):
        """
        Connects to Garmin Connect via the garth package. Uses a stored session if available.
        """
        try:
            garth.resume(self.garth_home)
            _ = garth.client.username  # Test if already logged in.
        except (FileNotFoundError, GarthException):
            if not self.username or not self.password:
                print("Environment variables GARMIN_USERNAME and/or GARMIN_PASSWORD not set.")
                self.username = input("Email: ")
                self.password = getpass("Password: ")
            garth.client.login(self.username, self.password)
            garth.save(self.garth_home)
        self.garth = garth
    
    def _pull_time_series(
        self,
        url_template: str,
        mapping: dict[str,str],
        start_date: str,
        end_date: str,
        response_path: list[str] = None,
        date_field: str = "calendarDate",
        values_field: str = "values",
        chunk_days: int = 28,
    ) -> pd.DataFrame:
        if self.garth is None:
            self.connect()

        start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
        end_dt = datetime.strptime(end_date,   "%Y-%m-%d").date()
        if start_dt > end_dt:
            raise ValueError("start_date must be ≤ end_date")

        chunks = []
        chunk_start = start_dt
        while chunk_start <= end_dt:
            chunk_end = min(chunk_start + timedelta(days=chunk_days-1), end_dt)
            url = url_template.format(start_date=chunk_start, end_date=chunk_end)
            res = self.garth.client.connectapi(url)
            
            # Pull out the data in case it's nested
            if response_path:
                for key in response_path:
                    res = res.get(key, [])

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

        if not chunks:
            return pd.DataFrame(columns=["date"] + list(mapping.values())).set_index("date")

        df = pd.concat(chunks, ignore_index=True)
        df["date"] = pd.to_datetime(df["date"])
        return df.set_index("date").sort_index()
    
    def _pull_wellness_timeseries(self,
        url_template: str,  
        mapping: dict[str,str],
        date: str,
        descriptors_key: str,
        values_key: str,
        descriptors_key_map: dict = {'index': 'index', 'key': 'key'},
    ) -> pd.DataFrame:
        
        if self.garth is None:
            self.connect()
        url = url_template.format(date=date)
        res = self.garth.client.connectapi(url)
        
        descriptors = res.get(descriptors_key) or []
        if not descriptors:
            if res.get('startTimestampGMT'):
                # Data are missing, but can get garmin to load
                post_url = f'wellness-service/wellness/epoch/request/{date}'
                self.garth.client.connectapi(post_url, method='POST')
                #time.sleep(1)

                # Re-pull
                res = self.garth.client.connectapi(url)
                descriptors = res.get(descriptors_key) or []
                if not descriptors:
                    # Still nothing, return empty DataFrame
                    return pd.DataFrame(columns=mapping.items())
            else:
                # No data available for the given date
                return pd.DataFrame(columns=mapping.items())
        
        cols = [None] * len(res[descriptors_key])
        for desc in res[descriptors_key]:
            cols[desc[descriptors_key_map['index']]] = desc[descriptors_key_map['key']]
        df = pd.DataFrame(res[values_key], columns=cols)
        df = df.rename(columns=mapping)
        if 'timestamp' in df.columns:
            df['date_time_utc'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
        return df

    def pull_heart_rate_single_day(self, date):
        mapping = {
            'timestamp': 'timestamp',
            'heartrate': 'hr',
        }
        return self._pull_wellness_timeseries(
            url_template='wellness-service/wellness/dailyHeartRate?date={date}',
            mapping=mapping,
            date=date,
            descriptors_key='heartRateValueDescriptors',
            values_key='heartRateValues'
        )

    def pull_heart_rate_detailed(self, start_date, end_date):
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end   = datetime.strptime(end_date,   "%Y-%m-%d").date()
        if start > end:
            raise ValueError("start_date must be ≤ end_date")
        
        total_days = (end - start).days + 1
        date_list = [start + timedelta(days=i) for i in range(total_days)]

        df_list = []
        for date in tqdm(date_list):
            date_str = date.strftime("%Y-%m-%d")
            df = self.pull_heart_rate_single_day(date_str)
            df_list.append(df)
        df = pd.concat(df_list, ignore_index=True)
        return df


    def pull_respiration_single_day(self, date):
        mapping = {
            'timestamp': 'timestamp',
            'respiration': 'respiration',
        }
        return self._pull_wellness_timeseries(
            url_template='wellness-service/wellness/daily/respiration/{date}',
            mapping=mapping,
            date=date,
            descriptors_key='respirationValueDescriptorsDTOList',
            values_key='respirationValuesArray'
        )

    def pull_spo2_single_day(self, date):
        mapping = {
            'timestamp': 'timestamp',
            'spo2Level': 'spo2_level',
            'monitoringEnvironmentLevel': 'monitoring_environment_level',
        }
        return self._pull_wellness_timeseries(
            url_template='wellness-service/wellness/daily/spo2acclimation/{date}',
            mapping=mapping,
            date=date,
            descriptors_key='spO2HourlyAveragesDescriptorList',
            values_key='spO2HourlyAverages',
            descriptors_key_map = {'index': 'spo2ValueDescIndex', 'key': 'spo2ValueDescKey'},
        )

    def quick_pull(self, url, method='GET'):
        res = self.garth.client.connectapi(url, method=method)
        return res

    def pull_steps_single_day(self, date):
        if self.garth is None:
            self.connect()
        url = f'wellness-service/wellness/dailySummaryChart/?date={date}'
        res = self.garth.client.connectapi(url)
        
        mapping = {
            'startGMT': 'start_gmt',
            'endGMT': 'end_gmt',
            'steps': 'steps',
            'pushes': 'pushes',
            'primaryActivityLevel': 'primary_activity_level',
            'activityLevelConstant': 'activity_level_constant',
        }
        df = pd.DataFrame(res).rename(columns=mapping)
        df['date_time_utc'] = pd.to_datetime(df['start_gmt'], utc=True)
        return df

    def pull_steps_data(self, start_date=None, end_date=None):
        mapping = {
            'stepGoal': 'step_goal',
            'totalSteps': 'total_steps',
            'totalDistance': 'total_distance',
        }
        return self._pull_time_series(
            url_template="usersummary-service/stats/daily/{start_date}/{end_date}?statsType=STEPS",
            mapping=mapping,
            start_date=start_date or "2017-01-01",
            end_date=end_date or datetime.today().strftime("%Y-%m-%d"),
            response_path=["values"],
            date_field="calendarDate",
            values_field="values",
            chunk_days=28
        )

    def pull_weight_data(self, start_date=None, end_date=None):
        mapping = {
            'weight': 'weight',
            'bmi': 'bmi',
            'bodyFat': 'body_fat',
            'bodyWater': 'body_water',
            'boneMass': 'bone_mass',
            'muscleMass': 'muscle_mass',
        }
        df = self._pull_time_series(
            url_template="/weight-service/weight/range/{start_date}/{end_date}?includeAll=true",
            mapping=mapping,
            start_date=start_date or "2017-01-01",
            end_date=end_date or datetime.today().strftime("%Y-%m-%d"),
            response_path=["dailyWeightSummaries"],
            values_field="allWeightMetrics",
            date_field="summaryDate",
            chunk_days=1000
        )
        df = df.groupby(df.index).mean().asfreq("D")

        # Convert from g to lbs
        for col in ['weight', 'muscle_mass', 'bone_mass']:
            df[col] = df[col] * 0.00220462
        df['fat_mass'] = df['body_fat'] * df['weight'] / 100.
        return df

    def pull_sleep_data(self, start_date=None, end_date=None):
        mapping = {
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
        }
        return self._pull_time_series(
            url_template="/sleep-service/stats/sleep/daily/{start_date}/{end_date}",
            mapping=mapping,
            start_date=start_date or "2017-01-01",
            end_date=end_date or datetime.today().strftime("%Y-%m-%d"),
            response_path=["individualStats"],
            date_field="calendarDate",
            values_field="values",
            chunk_days=28
        )

    def pull_heart_rate_data(self, start_date=None, end_date=None):
        mapping = {
            "restingHR": "resting_hr",
            "wellnessMaxAvgHR": "wellness_max_avg_hr",
            "wellnessMinAvgHR": "wellness_min_avg_hr",
        }
        return self._pull_time_series(
            url_template="/usersummary-service/stats/heartRate/daily/{start_date}/{end_date}",
            mapping=mapping,
            start_date=start_date or "2017-01-01",
            end_date=end_date or datetime.today().strftime("%Y-%m-%d"),
            response_path=None,
            date_field="calendarDate",
            values_field="values",
            chunk_days=28
        )
    
    def pull_stress_data(self, start_date=None, end_date=None):
        mapping = {
            "highStressDuration": "high_stress_duration",
            "lowStressDuration": "low_stress_duration",
            "overallStressLevel": "overall_stress_level",
            "restStressDuration": "rest_stress_duration",
        }
        return self._pull_time_series(
            url_template="/usersummary-service/stats/stress/daily/{start_date}/{end_date}",
            mapping=mapping,
            start_date=start_date or "2017-01-01",
            end_date=end_date or datetime.today().strftime("%Y-%m-%d"),
            response_path=None,
            date_field="calendarDate",
            values_field="values",
            chunk_days=28
        )

    def pull_body_battery_data(self, start_date=None, end_date=None):
        mapping = {
            "lowBodyBattery": "low_body_battery",
            "highBodyBattery": "high_body_battery",
        }
        return self._pull_time_series(   
            url_template="/usersummary-service/stats/bodybattery/daily/{start_date}/{end_date}",
            mapping=mapping,
            start_date=start_date or "2017-01-01",
            end_date=end_date or datetime.today().strftime("%Y-%m-%d"),
            response_path=None,
            date_field="calendarDate",
            values_field="values",
            chunk_days=28
        )

    def get_strength_workout(self, activity_id):
        """
        Retrieves the strength workout (exercise sets) data for a given activity ID.
        Returns a DataFrame with details or None on failure.
        """
        url = f"/activity-service/activity/{activity_id}/exerciseSets"
        res = garth.client.connectapi(url)

        try:
            rename_dict = {
                'activityId': 'activityId',
                'date': 'date',
                'time': 'time',
                'exerciseType': 'exerciseType',
                'exerciseName': 'exerciseName',
                'exerciseProbability': 'exerciseProbability',
                'setType': 'setType',
                'repetitionCount': 'reps',
                'weight': 'weight',
                'duration': 'duration'
            }
            if res['exerciseSets'] == [] or res['exerciseSets'] is None:
                return pd.DataFrame()
            df = pd.DataFrame(res['exerciseSets'])
            df['exercises'] = df['exercises'].apply(
                lambda x: x[0] if isinstance(x, list) and len(x) > 0 else {}
            )
            df['exerciseType'] = df['exercises'].apply(lambda x: x.get('category', None))
            df['exerciseName'] = df['exercises'].apply(lambda x: x.get('name', None))
            df['exerciseProbability'] = df['exercises'].apply(lambda x: x.get('probability', None))
            df['weight'] = round(2. * df['weight'] / 453.592, 0) / 2.
            df['dt'] = pd.to_datetime(df['startTime'])
            df['date'] = df['dt'].dt.strftime('%Y-%m-%d')
            df['time'] = df['dt'].dt.strftime('%H:%M:%S')
            df['activityId'] = activity_id
            df = df[list(rename_dict)].rename(columns=rename_dict).reset_index(drop=True)
            return df 
        except Exception as e:
            print(f"Error processing activity {activity_id}: {e}")
            return None

    def _update_time_series(
        self,
        model_class,
        pull_fn: callable,
        default_start: str = "2017-01-01",
    ):
        """
        Generic updater for any daily time series.
        
        model_class:      SQLAlchemy model (must have a `date` column)
        pull_fn:          function(start_date: str, end_date: str) -> DataFrame indexed by date
        default_start:    if no existing rows, start from here
        """
        from garmin.db.db_manager import DatabaseManager

        db_manager = DatabaseManager()
        existing_records = db_manager.get_records(model_class)
        if existing_records:
            last = max(r.date for r in existing_records)
            start = (last + timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            start = default_start
        end = datetime.today().strftime("%Y-%m-%d")

        if start > end:
            print(f"No new {model_class.__tablename__} data to update.")
            return

        new_data = pull_fn(start_date=start, end_date=end)
        if new_data.empty:
            print(f"No new {model_class.__tablename__} data pulled.")
            return

        new_records = []
        for dt, row in new_data.iterrows():
            data = {"date": dt.date()}
            for col in new_data.columns:
                data[col] = row.get(col)
            new_records.append(model_class(**data))

        if new_records:
            db_manager.add_records(new_records)
            print(f"Added {len(new_records)} new {model_class.__tablename__} records.")
        else:
            print(f"No new records to add for {model_class.__tablename__}.")

    def _update_time_series_detailed(
        self,
        model_class,
        pull_fn: callable,
        default_start: str = "2017-01-01",
        date_field: str = "date_time_utc",
    ):
        """
        Generic updater for any detailed, per‐timestamp time series.
        model_class:  SQLAlchemy model (must have a datetime‐typed column named `date_field`)
        pull_fn:      function(date_str: str) -> DataFrame with a datetime column `date_field`
        default_start: if no existing rows, start from here
        """
        from garmin.db.db_manager import DatabaseManager

        db_manager = DatabaseManager()
        existing_records = db_manager.get_records(model_class)

        if existing_records:
            last = max(getattr(r, date_field) for r in existing_records)
            start = (last + timedelta(days=1)).date()
        else:
            start = datetime.strptime(default_start, "%Y-%m-%d").date()

        end = datetime.today().date()
        if start > end:
            print(f"No new {model_class.__tablename__} detail data to update.")
            return

        total_days = (end - start).days + 1
        date_list = [start + timedelta(days=i) for i in range(total_days)]

        session = db_manager.Session()
        new_records = []
        
        for current in tqdm(date_list, desc=f"Updating {model_class.__tablename__}"):
            day_str = current.strftime("%Y-%m-%d")
            df = pull_fn(day_str)
            if not df.empty:
                # delete any old rows for this calendar‐date
                start_dt = datetime.combine(current, dt_time.min)
                end_dt   = datetime.combine(current, dt_time.max)
                session.query(model_class) \
                    .filter(getattr(model_class, date_field) >= start_dt) \
                    .filter(getattr(model_class, date_field) <= end_dt) \
                    .delete(synchronize_session=False)
                session.commit()

                # convert each row
                for _, row in df.iterrows():
                    kwargs = {}
                    for col in df.columns:
                        kwargs[col] = row[col]
                    new_records.append(model_class(**kwargs))

        session.close()
        if new_records:
            db_manager.add_records(new_records)
            print(f"Added {len(new_records)} new {model_class.__tablename__} records.")
        else:
            print(f"No new records to add for {model_class.__tablename__}.")

    def update_heart_rate_detailed_data(self):
        from garmin.db.db_manager import HeartRateDetailed
        self._update_time_series_detailed(
            HeartRateDetailed,
            self.pull_heart_rate_single_day,
            date_field="date_time_utc",
        )

    def update_respiration_detailed_data(self):
        from garmin.db.db_manager import RespirationDetailed
        self._update_time_series_detailed(
            RespirationDetailed,
            self.pull_respiration_single_day,
            date_field="date_time_utc",
        )
    
    def update_spo2_detailed_data(self):
        from garmin.db.db_manager import SpO2Detailed
        self._update_time_series_detailed(
            SpO2Detailed,
            self.pull_spo2_single_day,
            date_field="date_time_utc",
        )
    def update_steps_detailed_data(self):
        from garmin.db.db_manager import StepsDetailed
        self._update_time_series_detailed(
            StepsDetailed,
            self.pull_steps_single_day,
            date_field="date_time_utc",
        )

    def update_all_health_data(self):
        """
        Pulls all health data from Garmin Connect and updates the database.
        """
        self.update_weight_data()
        self.update_heart_rate_data()
        self.update_sleep_data()
        self.update_steps_data()
        self.update_stress_data()
        self.update_body_battery_data()
        self.update_heart_rate_detailed_data()
        self.update_respiration_detailed_data()
        self.update_spo2_detailed_data()
        self.update_steps_detailed_data()

    def update_weight_data(self):
        from garmin.db.db_manager import HealthStat
        self._update_time_series(HealthStat, self.pull_weight_data)
    
    def update_heart_rate_data(self):
        from garmin.db.db_manager import HeartRate
        self._update_time_series(HeartRate, self.pull_heart_rate_data)

    def update_sleep_data(self):
        from garmin.db.db_manager import Sleep
        self._update_time_series(Sleep, self.pull_sleep_data)
    
    def update_steps_data(self):
        from garmin.db.db_manager import Steps
        self._update_time_series(Steps, self.pull_steps_data)

    def update_stress_data(self):
        from garmin.db.db_manager import Stress
        self._update_time_series(Stress, self.pull_stress_data)

    def update_body_battery_data(self):
        from garmin.db.db_manager import BodyBattery
        self._update_time_series(BodyBattery, self.pull_body_battery_data)

    def update_activities(self, full=False):
        """
        Pulls the latest activities from Garmin Connect and updates the database.
        
        Creates a master Activity record for each new activity with the common fields:
            - activity_id, activity_name, date, time_of_day, duration, activity_type, calories, avg_heart_rate, details_pulled
          
        Depending on the activity type (the 'typeKey'), creates a specialized record in the corresponding table.
        
        For example:
          - For 'cycling': creates a CyclingActivity record (fields: distance, elevation_gain, cadence, etc.)
          - For 'strength_training': creates a StrengthActivity record (fields: total_sets, total_reps)
          - For 'running': creates a RunningActivity record
          - For 'indoor_cycling': creates an IndoorCyclingActivity record
          - etc.
        """
        from garmin.db.db_manager import (
            DatabaseManager, Activity, StrengthActivity, CyclingActivity,
            RunningActivity, IndoorCyclingActivity, HikingActivity, LapSwimmingActivity,
            VolleyballActivity, RockClimbingActivity, HIITActivity, PickleballActivity,
            TennisActivity, IndoorRunningActivity, OpenWaterSwimmingActivity,
            BoulderingActivity  # if defined
        )

        db_manager = DatabaseManager()
        existing_records = db_manager.get_records(Activity)
        existing_ids = {str(rec.activityId) for rec in existing_records}
        existing_dates = {rec.date for rec in existing_records}

        if existing_dates:
            days_since_last_pull = (datetime.today().date() - max(existing_dates)).days
            limit = 5 * days_since_last_pull
        else:
            limit=5000
        
        url = f"/activitylist-service/activities/search/activities?limit={limit}"
        res = garth.client.connectapi(url)
        if not res:
            print("No activities found.")
            return
        
        new_master_records = []
        specialized_records = []  # List of tuples: (model_class, record)
        
        for activity in res:
            activity_id = str(activity.get('activityId'))
            if activity_id in existing_ids:
                continue

            # Parse start time into date and time components.
            start_time_str = activity.get('startTimeLocal')
            try:
                dt = datetime.strptime(start_time_str, '%Y-%m-%d %H:%M:%S')
                activity_date = dt.date()
                activity_time = dt.strftime('%H:%M:%S')
            except Exception as e:
                print(f"Error parsing start time for activity {activity_id}: {e}")
                continue

            # Extract common fields.
            
            master_record = Activity(
                activityId=activity_id,
                activityName=activity.get('activityName', ''),
                activityType=activity.get('activityType', {}).get('typeKey', 'unknown'),
                date=activity_date,
                startTime=activity_time,
                distance=activity.get('distance', 0),
                duration=activity.get('duration', 0),
                elapsedDuration=activity.get('elapsedDuration', 0),
                movingDuration=activity.get('movingDuration', 0),
                averageSpeed=activity.get('averageSpeed', 0),
                calories=activity.get('calories', 0),
                bmrCalories=activity.get('bmrCalories', 0),
                averageHR=activity.get('averageHR', 0),
                maxHR=activity.get('maxHR', 0),
                steps=activity.get('steps', 0),
                waterEstimated=activity.get('waterEstimated', 0),
                aerobicTrainingEffect=activity.get('aerobicTrainingEffect', 0),
                anaerobicTrainingEffect=activity.get('anaerobicTrainingEffect', 0),
                activityTrainingLoad=activity.get('activityTrainingLoad', 0),
                moderateIntensityMinutes=activity.get('moderateIntensityMinutes', 0),
                vigorousIntensityMinutes=activity.get('vigorousIntensityMinutes', 0),
                differenceBodyBattery=activity.get('differenceBodyBattery', 0),
                hrTimeInZone_1=activity.get('hrTimeInZone_1', 0),
                hrTimeInZone_2=activity.get('hrTimeInZone_2', 0),
                hrTimeInZone_3=activity.get('hrTimeInZone_3', 0),
                hrTimeInZone_4=activity.get('hrTimeInZone_4', 0),
                hrTimeInZone_5=activity.get('hrTimeInZone_5', 0),
                detailsPulled=False
            )
            new_master_records.append(master_record)

            continue

            # Depending on activity type, create a specialized record.
            if act_type == 'cycling':
                distance = activity.get('distance', 0)
                elevation_gain = activity.get('elevationGain', 0)
                cadence = activity.get('averageBikingCadenceInRevPerMinute', None)
                spec_record = CyclingActivity(
                    activity_id=act_id,
                    date=act_date,
                    time_of_day=act_time,
                    distance=distance,
                    duration=duration,
                    elevation_gain=elevation_gain,
                    cadence=cadence
                )
                specialized_records.append((CyclingActivity, spec_record))
            elif act_type == 'strength_training':
                total_sets = activity.get('totalSets', 0)
                total_reps = activity.get('totalReps', 0)
                spec_record = StrengthActivity(
                    activity_id=act_id,
                    date=act_date,
                    time_of_day=act_time,
                    duration=duration,
                    total_sets=total_sets,
                    total_reps=total_reps
                )
                specialized_records.append((StrengthActivity, spec_record))
            
            # For unrecognized types, you could skip or create a generic specialized record.
        
        # Insert the new master records.
        if new_master_records:
            db_manager.add_records(new_master_records)
            print(f"Added {len(new_master_records)} new master activity records.")
        else:
            print("No new activities to update.")
        # Insert specialized records.
        for model, rec in specialized_records:
            db_manager.add_record(rec)
            print(f"Added new record to {model.__tablename__} for activity {rec.activityId}.")

    def update_strength_workout(self, activity_id):
        """
        Retrieves the strength workout data for the given activity_id and updates the
        strength_workouts table in the database by overwriting any existing data with the same activity_id.
        
        It first calls get_strength_workout(activity_id) to obtain a DataFrame, then
        deletes all existing records in the StrengthActivity table for that activity,
        and finally inserts the new rows.
        """
        import pandas as pd
        from garmin.db.db_manager import DatabaseManager, StrengthActivity

        db_manager = DatabaseManager()

        # Get the new strength workout data as a DataFrame.
        df = self.get_strength_workout(activity_id)
        if df.empty:
            print(f"No strength workout data found for activity {activity_id}.")
            db_manager.update_record(Activity, "activityId", activity_id, {"detailsPulled": True})
            return
        if df is None:
            print(f"Error pulling workout data for activity {activity_id}.")
            return

        
        # Open a session and delete existing records for this activity.
        session = db_manager.Session()
        try:
            session.query(StrengthActivity).filter(StrengthActivity.activityId == str(activity_id)).delete()
            session.commit()
        except Exception as e:
            session.rollback()
            print(f"Error deleting existing strength workout records for activity {activity_id}: {e}")
        finally:
            session.close()

        # Convert DataFrame rows into StrengthActivity objects.
        new_records = []
        # It is assumed that your DataFrame has been renamed so its columns match those you want.
        # For example, after renaming, df should contain:
        # 'activityId', 'date', 'time', 'exerciseType', 'exerciseName', 'exerciseProbability', 'setType', 'reps', 'weight', 'duration'
        for idx, row in df.iterrows():
            new_record = StrengthActivity(
                activityId=str(row.get('activityId', activity_id)),
                date=pd.to_datetime(row['date']).date(),
                time=row['time'],
                exerciseType=row['exerciseType'],
                exerciseName=row['exerciseName'],
                exerciseProbability=row['exerciseProbability'],
                setType=row['setType'],
                reps=row['reps'],
                weight=row['weight'],
                duration=row['duration'],
            )
            new_records.append(new_record)

        if new_records:
            db_manager.add_records(new_records)
            print(f"Updated strength workout records for activity {activity_id} (added {len(new_records)} rows).")
            # Now update the master Activity record to mark detailsPulled as True.
            db_manager.update_record(Activity, "activityId", activity_id, {"detailsPulled": True})
        else:
            print("No new records were created.")

    def update_all_strength_workouts(self):
        """
        Checks the master activities table for all activities with activityType == 'strength_training'
        that have detailsPulled == False. For each such activity, it calls update_strength_workout(activity_id)
        to pull and insert the workout details.
        """
        from garmin.db.db_manager import DatabaseManager, Activity

        db_manager = DatabaseManager()
        master_activities = db_manager.get_records(Activity)
        # Filter only strength training activities that have not been pulled.
        strength_master = [
            rec for rec in master_activities 
            if rec.activityType == 'strength_training' and not rec.detailsPulled
        ]

        if len(strength_master) == 0:
            print("No new strength data to update.")
            return

        print(f"Updating strength workout for {len(strength_master)} activities...")
        for act in strength_master:
            self.update_strength_workout(act.activityId)

