import os
from sqlalchemy import create_engine, Column, Integer, Float, String, Date, Boolean, DateTime
from sqlalchemy.orm import declarative_base

Base = declarative_base()

# Set schema for PostgreSQL, but not for SQLite
if os.environ.get('DATABASE_BACKEND', '').lower() == 'postgresql':
    Base.metadata.schema = 'garmin'

# --- Models ---
class HealthStats(Base):
    __tablename__ = 'health_stats'
    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, unique=True, nullable=False)
    weight = Column(Float)
    bmi = Column(Float)
    body_fat = Column(Float)
    body_water = Column(Float)
    bone_mass = Column(Float)
    muscle_mass = Column(Float)
    fat_mass = Column(Float)
    date_pulled = Column(Date)

class HeartRate(Base):
    __tablename__ = 'heart_rate'
    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, unique=True, nullable=False)
    resting_hr = Column(Float)
    wellness_max_avg_hr = Column(Float)
    wellness_min_avg_hr = Column(Float)
    date_pulled = Column(Date)

class Sleep(Base):
    __tablename__ = 'sleep'
    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, unique=True, nullable=False)
    rem_time = Column(Float)
    resting_hr = Column(Float)
    local_sleep_start_time = Column(Float)
    local_sleep_time_end = Column(Float)
    gmt_sleep_start_time = Column(Float)
    gmt_sleep_end_time = Column(Float)
    total_sleep_time = Column(Float)
    deep_time = Column(Float)
    awake_time = Column(Float)
    light_time = Column(Float)
    sleep_score_quality = Column(String)
    respiration = Column(Float)
    spo2 = Column(Float)
    hrv_status = Column(String)
    sleep_need = Column(Float)
    body_battery_change = Column(Float)
    skin_temp_f = Column(Float)
    skin_temp_c = Column(Float)
    hrv_7d_average = Column(Float)
    sleep_score = Column(Float)
    date_pulled = Column(Date)

class Steps(Base):
    __tablename__ = 'steps'
    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, unique=True, nullable=False)
    step_goal = Column(Float)
    total_steps = Column(Float)
    total_distance = Column(Float)
    date_pulled = Column(Date)

class Stress(Base):
    __tablename__ = 'stress'
    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, unique=True, nullable=False)
    high_stress_duration = Column(Float)
    low_stress_duration = Column(Float)
    overall_stress_level = Column(Float)
    rest_stress_duration = Column(Float)
    date_pulled = Column(Date)

class BodyBattery(Base):
    __tablename__ = 'body_battery'
    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, unique=True, nullable=False)
    low_body_battery = Column(Float)
    high_body_battery = Column(Float)
    date_pulled = Column(Date)

class StepsDetailed(Base):
    __tablename__ = 'steps_detailed'
    id = Column(Integer, primary_key=True, autoincrement=True)
    query_date = Column(Date, unique=False, nullable=False)
    date_time_utc = Column(DateTime, unique=True, nullable=False)
    start_gmt = Column(Date)
    end_gmt = Column(Date)
    steps = Column(Float)
    pushes = Column(Float)
    primary_activity_level = Column(String)
    activity_level_constant = Column(String)
    date_pulled = Column(Date)
    pull_status = Column(String)

class HeartRateDetailed(Base):
    __tablename__ = 'heart_rate_detailed'
    id = Column(Integer, primary_key=True, autoincrement=True)
    query_date = Column(Date, unique=False, nullable=False)
    date_time_utc = Column(DateTime, unique=True, nullable=False)
    timestamp = Column(Float)
    hr = Column(Float)
    date_pulled = Column(Date)
    pull_status = Column(String)

class SpO2Detailed(Base):
    __tablename__ = 'spo2_detailed'
    id = Column(Integer, primary_key=True, autoincrement=True)
    query_date = Column(Date, unique=False, nullable=False)
    date_time_utc = Column(DateTime, unique=True, nullable=False)
    timestamp = Column(Float)
    spo2_level = Column(Float)
    monitoring_environment_level = Column(Float)
    date_pulled = Column(Date)
    pull_status = Column(String)

class RespirationDetailed(Base):
    __tablename__ = 'respiration_detailed'
    id = Column(Integer, primary_key=True, autoincrement=True)
    query_date = Column(Date, unique=False, nullable=False)
    date_time_utc = Column(DateTime, unique=True, nullable=False)
    timestamp = Column(Float)
    respiration = Column(Float)
    date_pulled = Column(Date)
    pull_status = Column(String)

# ------------------------------
# Master table for common fields
# ------------------------------
class Activity(Base):
    __tablename__ = 'activities'
    id = Column(Integer, primary_key=True, autoincrement=True)
    activityId = Column(String, unique=True, nullable=False)
    activityName = Column(String)
    activityType = Column(String)
    date = Column(Date)
    startTime = Column(String)
    distance = Column(Float)
    duration = Column(Float)
    elapsedDuration = Column(Float)
    movingDuration = Column(Float)
    averageSpeed = Column(Float)
    calories = Column(Float)
    bmrCalories = Column(Float)
    averageHR = Column(Float)
    maxHR = Column(Float)
    steps = Column(Float)
    waterEstimated = Column(Float)
    aerobicTrainingEffect = Column(Float)
    anaerobicTrainingEffect = Column(Float)
    activityTrainingLoad = Column(Float)
    moderateIntensityMinutes = Column(Float)
    vigorousIntensityMinutes = Column(Float)
    differenceBodyBattery = Column(Float)
    hrTimeInZone_1 = Column(Float)
    hrTimeInZone_2 = Column(Float)
    hrTimeInZone_3 = Column(Float)
    hrTimeInZone_4 = Column(Float)
    hrTimeInZone_5 = Column(Float)
    detailsPulled = Column(Boolean, default=False)

# ------------------------------
# Specialized activity tables
# ------------------------------

class StrengthActivity(Base):
    __tablename__ = 'strength'
    id = Column(Integer, primary_key=True, autoincrement=True)
    activityId = Column(String, nullable=False)
    date = Column(Date)
    time = Column(String)
    exerciseType = Column(String)
    exerciseName = Column(String)
    exerciseProbability = Column(String)
    setType = Column(String)
    reps = Column(Integer)
    weight = Column(Integer)
    duration = Column(Float)


class BoulderingActivity(Base):
    __tablename__ = 'bouldering'
    id = Column(Integer, primary_key=True, autoincrement=True)


class CyclingActivity(Base):
    __tablename__ = 'cycling'
    id = Column(Integer, primary_key=True, autoincrement=True)


class RunningActivity(Base):
    __tablename__ = 'running'
    id = Column(Integer, primary_key=True, autoincrement=True)


class IndoorCyclingActivity(Base):
    __tablename__ = 'indoor_cycling'
    id = Column(Integer, primary_key=True, autoincrement=True)


class HikingActivity(Base):
    __tablename__ = 'hiking'
    id = Column(Integer, primary_key=True, autoincrement=True)


class LapSwimmingActivity(Base):
    __tablename__ = 'lap_swimming'
    id = Column(Integer, primary_key=True, autoincrement=True)


class VolleyballActivity(Base):
    __tablename__ = 'volleyball'
    id = Column(Integer, primary_key=True, autoincrement=True)


class RockClimbingActivity(Base):
    __tablename__ = 'rock_climbing'
    id = Column(Integer, primary_key=True, autoincrement=True)


class HIITActivity(Base):
    __tablename__ = 'hiit'
    id = Column(Integer, primary_key=True, autoincrement=True)


class PickleballActivity(Base):
    __tablename__ = 'pickleball'
    id = Column(Integer, primary_key=True, autoincrement=True)


class TennisActivity(Base):
    __tablename__ = 'tennis'
    id = Column(Integer, primary_key=True, autoincrement=True)


class IndoorRunningActivity(Base):
    __tablename__ = 'indoor_running'
    id = Column(Integer, primary_key=True, autoincrement=True)


class OpenWaterSwimmingActivity(Base):
    __tablename__ = 'open_water_swimming'
    id = Column(Integer, primary_key=True, autoincrement=True)
