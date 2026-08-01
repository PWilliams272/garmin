from flask import Blueprint, jsonify, render_template, request, url_for, redirect
from garmin.io.curated_store import CuratedDataStore
from garmin.io.file_manager import FileManager
from garmin.dashboard_curated import (
    build_curated_dashboard_artifacts,
    cache_curated_dashboard_artifacts_locally,
    curated_dashboard_relative_dir,
)
from garmin.data_processor.processor import GarminDataProcessor
from garmin.analysis.quality import classify_metric
from garmin.analysis.trend_gp import fit_gp_trend
from garmin.analysis.analysis_pipeline import STRENGTH_EXERCISE_CANDIDATES
from garmin.updaters import ACTIVITY_DATASETS
import numpy as np
import pandas as pd
import os
import random as _random

# Which curated store ('local' or 's3') API routes read from when the
# request doesn't specify ?source= explicitly. The standalone deployed
# viewer (no local curated/ directory on that host) sets
# GARMIN_VIEWER_SOURCE=s3 via its systemd unit; local dev keeps the
# 'local' default.
DEFAULT_SOURCE = os.environ.get('GARMIN_VIEWER_SOURCE', 'local')
if DEFAULT_SOURCE not in {'local', 's3'}:
    DEFAULT_SOURCE = 'local'

RUNNING_ANALYZED_METRICS = ['cadence_spm', 'pace_min_per_mile', 'distance_mi']

MOCK_MA_BANDWIDTH_DAYS = 21

bp = Blueprint(
    'garmin',
    __name__,
    template_folder="templates",
    static_folder="static"
)

fm_local = FileManager(environment='local')
fm_s3 = FileManager(environment='aws')
curated_local = CuratedDataStore(file_manager=fm_local)
curated_s3 = CuratedDataStore(file_manager=fm_s3)

DASHBOARD_FILES = [
    ("health_stats_timeseries_script.html", "bokeh_script_weight_timeseries"),
    ("health_stats_timeseries_div.html", "bokeh_div_weight_timeseries"),
    ("heart_rate_timeseries_script.html", "bokeh_script_hr_timeseries"),
    ("heart_rate_timeseries_div.html", "bokeh_div_hr_timeseries"),
    ("sleep_timeseries_script.html", "bokeh_script_sleep_timeseries"),
    ("sleep_timeseries_div.html", "bokeh_div_sleep_timeseries"),
    ("steps_timeseries_script.html", "bokeh_script_steps_timeseries"),
    ("steps_timeseries_div.html", "bokeh_div_steps_timeseries"),
]

CURATED_DASHBOARD_FILES = DASHBOARD_FILES


# dataset -> metrics, matching garmin.analysis.analysis_pipeline.HEALTH_METRICS.
# The web app only ever reads the precomputed curated/analyzed/ layer here
# (written by garmin.scripts.manual_analyze_metrics) -- no GP fitting happens
# on request.
HEALTH_ANALYZED_METRICS = {
    'heart_rate': ['resting_hr'],
    'steps': ['total_steps'],
    'health_stats': ['weight', 'body_fat', 'bone_mass', 'muscle_mass'],
}


def _health_analyzed_payload(source: str = 'local') -> dict | None:
    store = curated_s3 if source == 's3' else curated_local

    analyzed = {}
    any_analyzed = False
    for dataset, metrics in HEALTH_ANALYZED_METRICS.items():
        for metric in metrics:
            points = store.load_analyzed_points(dataset, metric)
            trend_gp = store.load_analyzed_trend(dataset, metric, kind='gp_multiscale')
            trend_sts = store.load_analyzed_trend(dataset, metric, kind='sts')
            if not points.empty:
                any_analyzed = True
            analyzed[metric] = {
                'points': _timeseries_records(points) if not points.empty else [],
                'trend_gp': _timeseries_records(trend_gp) if not trend_gp.empty else [],
                'trend_sts': _timeseries_records(trend_sts) if not trend_sts.empty else [],
            }

    if not any_analyzed:
        return None

    return {'analyzed': analyzed}


def _cached_or_live(cache_name: str, source: str, build_fn):
    """Try the precomputed viewer-cache blob first (built offline by
    garmin.scripts.manual_build_viewer_cache -- one JSON write per page per
    source), falling back to assembling the payload live from curated/
    analyzed/ parquet if no cache exists yet.

    This matters most for source='s3': each of the payload builders below
    does a dozen-plus individual file_manager.read_df calls, and against S3
    every one of those is its own network round trip (see file_manager.py --
    no batching, no connection reuse across calls). Reading one cached JSON
    blob instead turns that into a single request.
    """
    store = curated_s3 if source == 's3' else curated_local
    cached = store.load_viewer_cache(cache_name)
    if cached is not None:
        return cached
    return build_fn()


# Every intraday dataset that tracks per-day pull status via
# curated/metadata/detailed_status/<dataset>.parquet (fetched/no_data/denied),
# written by DataUpdater._update_detailed_time_series_curated.
DATA_STATUS_DETAILED_DATASETS = [
    'heart_rate_detailed', 'spo2_detailed', 'steps_detailed', 'respiration_detailed',
]

# Every daily (one-row-per-date) dataset. These don't track denial the way
# detailed datasets do -- a date either has a row (fetched) or doesn't
# (untouched); there's no way yet to distinguish "Garmin had nothing for
# that date" from "we haven't tried."
DATA_STATUS_DAILY_DATASETS = [
    'health_stats', 'steps', 'sleep', 'stress', 'body_battery', 'heart_rate', 'hrv', 'respiration',
]

DATA_STATUS_CODES = {'untouched': 0, 'no_data': 1, 'denied': 2, 'fetched': 3}

# Fixed rather than derived from the data -- Garmin device history for this
# account starts around here, and a fixed axis start keeps the calendar's
# column count (and therefore its rendered width) stable run to run instead
# of creeping earlier every time an older dataset happens to be involved.
DATA_STATUS_START_DATE = pd.Timestamp('2015-12-01')


def _data_status_payload(source: str = 'local') -> dict:
    """Per-week pull status (fetched/no_data/denied/untouched) for every dataset
    that tracks it, for the calendar-style status monitor. Activity datasets
    (running/strength/...) are excluded -- ActivityPuller doesn't yet detect
    or record 429/denial the way HealthDetailedPuller does, so there's no
    per-day status to show for them.

    Aggregated to one column per week (majority status that week), not one
    per day: at ~10 years of history that's ~4000 days, which renders as
    sub-pixel columns in any reasonably-sized chart and visually corrupts
    into muddy/near-black bands. ~550 weekly columns fits a normal screen
    width cleanly instead of requiring horizontal scroll.
    """
    store = curated_s3 if source == 's3' else curated_local
    today = pd.Timestamp.today().normalize()

    per_dataset: dict[str, dict[str, pd.Timestamp | pd.Series]] = {}

    for dataset in DATA_STATUS_DETAILED_DATASETS:
        status_df = store.load_detailed_status(dataset)
        if status_df.empty:
            continue
        status_df = status_df.copy()
        status_df['query_date'] = pd.to_datetime(status_df['query_date'])
        status_by_date = status_df.set_index('query_date')['pull_status']
        per_dataset[dataset] = {'start': status_by_date.index.min(), 'status_by_date': status_by_date}

    for dataset in DATA_STATUS_DAILY_DATASETS:
        daily_df = store.load_daily(dataset)
        if daily_df.empty:
            continue
        dates = pd.to_datetime(daily_df['date'])
        status_by_date = pd.Series('fetched', index=dates)
        per_dataset[dataset] = {'start': dates.min(), 'status_by_date': status_by_date}

    if not per_dataset:
        return {'datasets': [], 'dates': [], 'status_codes': DATA_STATUS_CODES}

    all_dates = pd.date_range(DATA_STATUS_START_DATE, today, freq='D')
    week_starts = all_dates.to_period('W-MON').start_time
    unique_weeks = sorted(week_starts.unique())

    datasets_payload = []
    for name in DATA_STATUS_DETAILED_DATASETS + DATA_STATUS_DAILY_DATASETS:
        info = per_dataset.get(name)
        if info is None:
            continue
        status_by_date = info['status_by_date']
        daily_codes = [
            DATA_STATUS_CODES.get(status_by_date.get(d), DATA_STATUS_CODES['untouched']) if d >= info['start']
            else DATA_STATUS_CODES['untouched']
            for d in all_dates
        ]
        # Majority vote per week -- ties resolve to whichever code
        # value_counts() lists first, not a meaningful preference.
        weekly = (
            pd.Series(daily_codes, index=week_starts)
            .groupby(level=0)
            .agg(lambda s: s.value_counts().idxmax())
            .reindex(unique_weeks, fill_value=DATA_STATUS_CODES['untouched'])
        )
        datasets_payload.append({
            'name': name,
            'kind': 'detailed' if name in DATA_STATUS_DETAILED_DATASETS else 'daily',
            'statuses': weekly.tolist(),
        })

    return {
        'datasets': datasets_payload,
        'dates': [w.date().isoformat() for w in unique_weeks],
        'status_codes': DATA_STATUS_CODES,
    }


def _timeseries_records(df: pd.DataFrame) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for row in df.to_dict(orient='records'):
        cleaned: dict[str, object] = {}
        for key, value in row.items():
            if key == 'date' and pd.notnull(value):
                cleaned[key] = pd.Timestamp(value).date().isoformat()
            elif key in ('timestamp', 'start_time') and pd.notnull(value):
                cleaned[key] = pd.Timestamp(value).isoformat()
            elif pd.isna(value):
                cleaned[key] = None
            elif hasattr(value, 'item'):
                cleaned[key] = value.item()
            else:
                cleaned[key] = value
        records.append(cleaned)
    return records

def _mock_running_runs() -> pd.DataFrame:
    """Synthetic per-run records: date, distance, pace, cadence, with a slow improvement trend."""
    rng = np.random.default_rng(42)
    weeks = 52
    today = pd.Timestamp.today().normalize()
    start = today - pd.Timedelta(weeks=weeks)

    rows = []
    for week_idx in range(weeks + 1):
        week_start = start + pd.Timedelta(weeks=week_idx)
        progress = week_idx / weeks
        for _ in range(int(rng.integers(2, 5))):
            run_date = week_start + pd.Timedelta(days=int(rng.integers(0, 7)))
            if run_date > today:
                continue
            pace = max(6.5, 9.4 - 1.0 * progress + rng.normal(0, 0.35))
            distance = max(1.5, rng.normal(4.5 + 2.5 * progress, 1.4))
            cadence = 167 + 6 * progress + rng.normal(0, 3)
            rows.append({
                'date': run_date,
                'distance_mi': round(float(distance), 2),
                'pace_min_per_mile': round(float(pace), 2),
                'cadence_spm': round(float(cadence), 1),
            })

    df = pd.DataFrame(rows).sort_values('date').reset_index(drop=True)
    processor = GarminDataProcessor()
    ma_columns = ['distance_mi', 'pace_min_per_mile', 'cadence_spm']
    ma_df = processor.calculate_moving_averages(df, ma_columns, kernels=['gaussian'], bandwidths=[MOCK_MA_BANDWIDTH_DAYS])
    for col in ma_columns:
        df[f'{col}_ma'] = ma_df[f'{col}_gaussian_{MOCK_MA_BANDWIDTH_DAYS}']
    return df


def _running_payload() -> dict:
    df = _mock_running_runs()
    weekly = (
        df.set_index('date')
        .resample('W-MON')
        .agg(run_count=('distance_mi', 'count'), total_distance_mi=('distance_mi', 'sum'))
        .reset_index()
    )

    # Mock data is synthetic/preview-only, so computing its analyzed view live
    # (unlike real data, which is always precomputed) is fine -- it lets the
    # UI preview the quality-tier/GP rendering before real analyzed data exists.
    analyzed = {}
    for metric in RUNNING_ANALYZED_METRICS:
        points = classify_metric(df[['date', metric]], metric)
        fittable = points[points['quality_weight'] > 0]
        trend = fit_gp_trend(fittable['date'], fittable[metric], fittable['quality_weight'])
        analyzed[metric] = {
            'points': _timeseries_records(points),
            'trend': _timeseries_records(trend) if not trend.empty else [],
        }

    return {
        'runs': _timeseries_records(df),
        'weekly': _timeseries_records(weekly),
        'analyzed': analyzed,
    }


def _mock_lifting_sessions() -> pd.DataFrame:
    """Synthetic strength sessions: per-exercise top-set weight with a slow progression trend."""
    rng = np.random.default_rng(7)
    exercises = ['bench_press', 'squat', 'curl']
    base_weights = {'bench_press': 135.0, 'squat': 185.0, 'curl': 30.0}
    weeks = 52
    today = pd.Timestamp.today().normalize()
    start = today - pd.Timedelta(weeks=weeks)

    rows = []
    for week_idx in range(weeks + 1):
        week_start = start + pd.Timedelta(weeks=week_idx)
        progress = week_idx / weeks
        for exercise in exercises:
            for _ in range(int(rng.integers(1, 3))):
                session_date = week_start + pd.Timedelta(days=int(rng.integers(0, 7)))
                if session_date > today:
                    continue
                base = base_weights[exercise]
                weight = base * (1 + 0.28 * progress) + rng.normal(0, base * 0.03)
                rows.append({
                    'date': session_date,
                    'exercise': exercise,
                    'top_weight_lb': round(float(weight), 1),
                })
    return pd.DataFrame(rows).sort_values('date').reset_index(drop=True)


def _lifting_payload() -> dict:
    df = _mock_lifting_sessions()
    processor = GarminDataProcessor()
    exercises = {}
    for exercise, group in df.groupby('exercise'):
        group = group.sort_values('date').reset_index(drop=True)
        ma_df = processor.calculate_moving_averages(
            group, ['top_weight_lb'], kernels=['gaussian'], bandwidths=[MOCK_MA_BANDWIDTH_DAYS]
        )
        group['top_weight_lb_ma'] = ma_df[f'top_weight_lb_gaussian_{MOCK_MA_BANDWIDTH_DAYS}']
        exercises[exercise] = _timeseries_records(group[['date', 'top_weight_lb', 'top_weight_lb_ma']])

    weekly_frequency = (
        df.set_index('date')
        .groupby([pd.Grouper(freq='W-MON'), 'exercise'])
        .size()
        .reset_index(name='sessions')
    )
    return {
        'exercises': exercises,
        'weekly_frequency': _timeseries_records(weekly_frequency),
    }




def _running_real_payload(source: str = 'local') -> dict | None:
    store = curated_s3 if source == 's3' else curated_local
    df = store.load_activity_summary('running')
    if df.empty:
        return None

    df = df.copy()
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date').reset_index(drop=True)

    # Quality classification + GP trend fitting are precomputed by
    # garmin.scripts.manual_analyze_metrics into curated/analyzed/ — this
    # route only ever reads that output, it never fits a GP on request.
    analyzed = {}
    any_analyzed = False
    for metric in RUNNING_ANALYZED_METRICS:
        points = store.load_analyzed_points('running', metric)
        trend = store.load_analyzed_trend('running', metric)
        if not points.empty:
            any_analyzed = True
        analyzed[metric] = {
            'points': _timeseries_records(points) if not points.empty else [],
            'trend': _timeseries_records(trend) if not trend.empty else [],
        }

    if not any_analyzed:
        return None

    weekly = (
        df.set_index('date')
        .resample('W-MON')
        .agg(run_count=('distance_mi', 'count'), total_distance_mi=('distance_mi', 'sum'))
        .reset_index()
    )
    return {
        'runs': _timeseries_records(df),
        'weekly': _timeseries_records(weekly),
        'analyzed': analyzed,
    }


def _lifting_real_payload(source: str = 'local') -> dict | None:
    """Per-exercise estimated-1RM points + STS trend, precomputed offline by
    garmin.analysis.analysis_pipeline.analyze_lifting -- this route only reads
    curated/analyzed/strength/<exercise>_1rm_* output, same "no live model
    fitting" rule as the Health tab. weekly_frequency is still computed live
    since it's a plain groupby, not a model fit.
    """
    store = curated_s3 if source == 's3' else curated_local
    summary = store.load_activity_summary('strength')
    if summary.empty:
        return None

    summary = summary.copy()
    summary['date'] = pd.to_datetime(summary['date'])

    detail = store.load_all_activity_details('strength')
    if detail.empty:
        return None

    detail = detail.merge(summary[['activity_id', 'date']], on='activity_id', how='left')
    detail = detail.dropna(subset=['date'])

    exercises = {}
    session_counts = detail.groupby('exercise')['activity_id'].nunique()
    for exercise in STRENGTH_EXERCISE_CANDIDATES:
        points = store.load_analyzed_points('strength', f'{exercise}_1rm')
        if points.empty:
            continue
        trend = store.load_analyzed_trend('strength', f'{exercise}_1rm', kind='sts')
        volume_trend = store.load_analyzed_trend('strength', f'{exercise}_volume', kind='sts')
        exercises[exercise] = {
            'points': _timeseries_records(points),
            'trend': _timeseries_records(trend) if not trend.empty else [],
            'volume_trend': _timeseries_records(volume_trend) if not volume_trend.empty else [],
            'sessions': int(session_counts.get(exercise, 0)),
        }

    if not exercises:
        return None

    exercise_order = sorted(exercises, key=lambda ex: exercises[ex]['sessions'], reverse=True)

    weekly_frequency = (
        detail.drop_duplicates(subset=['activity_id', 'exercise'])
        .set_index('date')
        .groupby([pd.Grouper(freq='W-MON'), 'exercise'])
        .size()
        .reset_index(name='sessions')
    )
    return {
        'exercises': exercises,
        'exercise_order': exercise_order,
        'weekly_frequency': _timeseries_records(weekly_frequency),
    }


def _mock_activities() -> pd.DataFrame:
    """Synthetic multi-sport activity log used for the Activities overview and recent-activities list."""
    rng = np.random.default_rng(99)
    weeks = 26
    today = pd.Timestamp.today().normalize()
    start = today - pd.Timedelta(weeks=weeks)

    activity_types = ['running', 'cycling', 'climbing', 'lifting', 'swimming']
    has_distance = {'running', 'cycling', 'swimming'}
    avg_duration_min = {'running': 45, 'cycling': 75, 'climbing': 90, 'lifting': 55, 'swimming': 40}
    avg_distance_mi = {'running': 4.5, 'cycling': 15.0, 'swimming': 1.2}

    rows = []
    for week_idx in range(weeks):
        week_start = start + pd.Timedelta(weeks=week_idx)
        for activity_type in activity_types:
            count = int(rng.integers(1, 4)) if activity_type == 'lifting' else int(rng.integers(0, 3))
            for _ in range(count):
                activity_date = week_start + pd.Timedelta(days=int(rng.integers(0, 7)))
                if activity_date > today:
                    continue
                duration = max(15.0, rng.normal(avg_duration_min[activity_type], avg_duration_min[activity_type] * 0.25))
                distance = None
                if activity_type in has_distance:
                    distance = max(0.5, rng.normal(avg_distance_mi[activity_type], avg_distance_mi[activity_type] * 0.3))
                rows.append({
                    'date': activity_date,
                    'type': activity_type,
                    'duration_min': round(float(duration), 1),
                    'distance_mi': round(float(distance), 2) if distance is not None else None,
                })
    return pd.DataFrame(rows).sort_values('date').reset_index(drop=True)


def _mock_activities_list_payload() -> dict:
    """Same synthetic log as _mock_activities(), reshaped to match
    _activities_list_payload's fuller per-activity record (adds activity_id,
    name, avg_hr, a synthetic start_time) for the Activities tab's list.
    """
    rng = np.random.default_rng(101)
    df = _mock_activities().reset_index(drop=True)
    avg_hr_by_type = {'running': 148, 'cycling': 132, 'climbing': 118, 'lifting': 110, 'swimming': 138}
    df['activity_id'] = [f'mock-{i}' for i in df.index]
    df['name'] = df['type'].str.title() + ' Activity'
    df['avg_hr'] = df['type'].map(lambda t: round(float(rng.normal(avg_hr_by_type.get(t, 130), 8))))
    df['start_time'] = df['date'] + pd.to_timedelta(rng.integers(6, 20, size=len(df)), unit='h')
    df = df.sort_values('date', ascending=False).reset_index(drop=True)
    return {
        'activity_types': sorted(df['type'].unique().tolist()),
        'activities': _timeseries_records(df),
    }


def _activities_real_payload(source: str = 'local') -> dict | None:
    """Real multi-sport activity overview, built from every registered activity dataset's summary.

    Shaped identically to _mock_activities()'s output (date, type, duration_min,
    distance_mi) so _activities_overview_from_df needs no branching on source.
    """
    store = curated_s3 if source == 's3' else curated_local
    frames = []
    for dataset in ACTIVITY_DATASETS:
        summary = store.load_activity_summary(dataset)
        if summary.empty:
            continue
        frame = summary[['date']].copy()
        frame['type'] = dataset
        frame['activity_id'] = summary.get('activity_id')
        frame['duration_min'] = summary.get('duration_min')
        frame['distance_mi'] = summary.get('distance_mi') if 'distance_mi' in summary.columns else None
        frames.append(frame)

    if not frames:
        return None

    df = pd.concat(frames, ignore_index=True)
    df['date'] = pd.to_datetime(df['date'])
    return _activities_overview_from_df(df)


ACTIVITIES_LIST_COLUMNS = ['activity_id', 'date', 'start_time', 'type', 'name', 'duration_min', 'distance_mi', 'avg_hr']


def _activities_list_payload(source: str = 'local') -> dict | None:
    """Every pulled activity across every sport, for the Activities tab's
    browsable list -- unlike _activities_real_payload's `recent_activities`
    (capped at 15, no avg_hr), this is the full history with everything the
    list/filter UI needs. `start_time` is only present for activities
    pulled after ActivityPuller started capturing it (2026-07-31) -- older
    rows fall back to date-only, no re-pull is triggered here to backfill it.
    """
    store = curated_s3 if source == 's3' else curated_local
    frames = []
    for dataset in ACTIVITY_DATASETS:
        summary = store.load_activity_summary(dataset)
        if summary.empty:
            continue
        frame = summary.copy()
        frame['type'] = dataset
        for col in ACTIVITIES_LIST_COLUMNS:
            if col not in frame.columns:
                frame[col] = None
        frames.append(frame[ACTIVITIES_LIST_COLUMNS])

    if not frames:
        return None

    df = pd.concat(frames, ignore_index=True)
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date', ascending=False).reset_index(drop=True)

    return {
        'activity_types': sorted(df['type'].unique().tolist()),
        'activities': _timeseries_records(df),
    }


def _activities_overview_payload() -> dict:
    return _activities_overview_from_df(_mock_activities())


def _activities_overview_from_df(df: pd.DataFrame) -> dict:
    weekly_by_type = (
        df.set_index('date')
        .groupby([pd.Grouper(freq='W-MON'), 'type'])
        .size()
        .reset_index(name='count')
    )
    weekly_totals = (
        df.set_index('date')
        .resample('W-MON')
        .agg(total_count=('type', 'size'), total_distance_mi=('distance_mi', 'sum'))
        .reset_index()
    )
    weekly_totals['total_hours'] = (
        df.set_index('date').resample('W-MON')['duration_min'].sum() / 60
    ).round(1).values

    recent = df.sort_values('date', ascending=False).head(15)

    return {
        'activity_types': sorted(df['type'].unique().tolist()),
        'weekly_by_type': _timeseries_records(weekly_by_type),
        'weekly_totals': _timeseries_records(weekly_totals),
        'recent_activities': _timeseries_records(recent),
        'kpis': {
            'total_activities': int(len(df)),
            'total_distance_mi': round(float(df['distance_mi'].sum(skipna=True)), 1),
            'total_hours': round(float(df['duration_min'].sum() / 60), 1),
        },
    }


HR_ZONE_LABELS = ['Z1 Recovery', 'Z2 Endurance', 'Z3 Tempo', 'Z4 Threshold', 'Z5 VO2max']
HR_ZONE_BOUNDS = [0, 114, 133, 152, 171, 999]

# Per-point metrics worth screening for sensor/GPS glitches within a single
# activity (a momentary HR dropout, a GPS-jump speed spike, ...). Each
# column is classified independently -- one metric glitching at a given
# instant doesn't invalidate the others at that same timestamp.
ACTIVITY_DETAIL_OUTLIER_METRICS = ['speed_mph', 'heart_rate_bpm', 'cadence', 'power_w']


def _reject_activity_detail_outliers(detail: pd.DataFrame) -> pd.DataFrame:
    """Null out hard-tier (very likely sensor/GPS glitch) values in place,
    reusing the same classify_metric() used for health metrics -- its
    local-neighborhood check (rolling-median deviation) is exactly what
    catches a momentary spike in a densely-sampled series like this, even
    though the "local window" here is ~15 seconds instead of ~15 days.
    Soft-tier points are left alone (kept, not visually distinguished --
    this view is dense scatter, not a trend fit, so there's no weighting
    to apply them to).
    """
    detail = detail.copy()
    for metric in ACTIVITY_DETAIL_OUTLIER_METRICS:
        if metric not in detail.columns or detail[metric].notna().sum() < 5:
            continue
        classified = classify_metric(detail[['timestamp', metric]], metric, date_col='timestamp')
        detail.loc[classified['quality_tier'].to_numpy() == 'hard', metric] = None
    return _reject_speed_transition_points(detail)


# mph per second. Samples are ~1Hz, and a real steady run/ride's speed
# doesn't swing this fast second to second -- anything faster is almost
# certainly still accelerating/decelerating out of or into a stop, not a
# stable pace. This isn't a glitch (classify_metric's job above): it's
# real, physically accurate data, just not representative of a "pace"
# worth plotting, and since pace = 60/speed, a few genuine low-but-
# transient speed samples still produce wild pace values that visibly
# "race down" toward the real pace over several points if left in.
SPEED_TRANSITION_THRESHOLD_MPH_PER_S = 1.0


def _reject_speed_transition_points(detail: pd.DataFrame) -> pd.DataFrame:
    """Nulls speed_mph at points where speed is changing faster than
    SPEED_TRANSITION_THRESHOLD_MPH_PER_S on either side (the jump into the
    point or the jump out of it) -- flags both endpoints of a rapid ramp,
    not just the single most extreme sample in it.
    """
    if "speed_mph" not in detail.columns or detail["speed_mph"].notna().sum() < 3:
        return detail

    detail = detail.copy()
    dt_seconds = detail["timestamp"].diff().dt.total_seconds().replace(0, np.nan)
    accel_in = detail["speed_mph"].diff().abs() / dt_seconds
    accel_out = accel_in.shift(-1)
    transitioning = (accel_in > SPEED_TRANSITION_THRESHOLD_MPH_PER_S) | (accel_out > SPEED_TRANSITION_THRESHOLD_MPH_PER_S)
    detail.loc[transitioning.fillna(False), "speed_mph"] = None
    return detail


# Consecutive near-zero-speed or missing-HR samples needed before a stretch
# counts as "probably paused the watch" rather than just noise/a red light.
MIN_PAUSE_SAMPLES = 4


def _detect_pause_windows(detail: pd.DataFrame) -> list[dict]:
    """Contiguous stretches of near-zero speed or missing heart rate,
    returned as timestamp windows -- the working theory (yours) is these
    are where the watch was paused, not sensor noise. Rather than trying
    to clean these points away, the frontend shades them for context so
    the dip/gap in the data reads as "watch was paused here", not "the
    data's wrong here". Computed from the raw (pre-outlier-rejection)
    columns, since rejection already nulls some of the same points and
    would otherwise make them indistinguishable from genuine gaps.
    """
    if detail.empty or 'timestamp' not in detail.columns:
        return []

    low_speed = detail['speed_mph'].fillna(0) < 1.0 if 'speed_mph' in detail.columns else pd.Series(False, index=detail.index)
    missing_hr = detail['heart_rate_bpm'].isna() if 'heart_rate_bpm' in detail.columns else pd.Series(False, index=detail.index)
    paused = (low_speed | missing_hr).to_numpy()

    windows = []
    start_idx = None
    for i, is_paused in enumerate(paused):
        if is_paused and start_idx is None:
            start_idx = i
        elif not is_paused and start_idx is not None:
            if i - start_idx >= MIN_PAUSE_SAMPLES:
                windows.append((start_idx, i - 1))
            start_idx = None
    if start_idx is not None and len(paused) - start_idx >= MIN_PAUSE_SAMPLES:
        windows.append((start_idx, len(paused) - 1))

    timestamps = detail['timestamp']
    return [
        {'start': timestamps.iloc[s].isoformat(), 'end': timestamps.iloc[e].isoformat()}
        for s, e in windows
    ]


def _activity_detail_real_payload(sport: str = 'running', activity_id: str | None = None, source: str = 'local') -> dict | None:
    """Real per-point activity detail (map + pace/HR/cadence/power charts),
    read from curated/activities/detail/<sport>_timeseries/ -- written by
    manual_fetch_activity_detail.py, an exploratory spike script, not yet
    part of the scheduled pipeline. Expect this to usually be empty/cover
    only whichever single activity that script has been pointed at.

    If activity_id isn't given, picks the most recent activity (by the
    sport's summary date) that actually has a timeseries file, since not
    every activity has one yet.
    """
    store = curated_s3 if source == 's3' else curated_local
    dataset = f'{sport}_timeseries'

    if activity_id is None:
        prefix = f'curated/activities/detail/{dataset}/'
        files = [f for f in store.file_manager.list_files(prefix) if f.endswith('.parquet')]
        if not files:
            return None
        ids = [f.rsplit('activity_id=', 1)[-1].removesuffix('.parquet') for f in files]
        summary_all = store.load_activity_summary(sport)
        if not summary_all.empty:
            candidates = summary_all[summary_all['activity_id'].astype(str).isin(ids)]
            if not candidates.empty:
                activity_id = str(candidates.sort_values('date').iloc[-1]['activity_id'])
        if activity_id is None:
            activity_id = ids[0]

    detail = store.load_activity_detail(dataset, str(activity_id))
    if detail.empty:
        return None

    detail = detail.dropna(subset=['timestamp']).sort_values('timestamp').reset_index(drop=True)
    pause_windows = _detect_pause_windows(detail)
    detail = _reject_activity_detail_outliers(detail)

    summary = store.load_activity_summary(sport)
    activity_meta = {'type': sport, 'name': None, 'date': None, 'duration_min': None, 'distance_mi': None}
    meta_row = summary[summary['activity_id'].astype(str) == str(activity_id)] if not summary.empty else summary
    if not meta_row.empty:
        r = meta_row.iloc[0]
        activity_meta.update({
            'name': r.get('name'),
            'date': pd.Timestamp(r['date']).date().isoformat() if pd.notnull(r.get('date')) else None,
            'duration_min': float(r['duration_min']) if pd.notnull(r.get('duration_min')) else None,
            'distance_mi': float(r['distance_mi']) if pd.notnull(r.get('distance_mi')) else None,
        })

    # Per-sample elapsed seconds -- Garmin's sampling interval isn't fixed --
    # clipped so a pause/gap in recording doesn't dump several minutes into
    # whichever HR zone the heart rate happened to be in right before/after it.
    delta_s = detail['timestamp'].diff().dt.total_seconds().fillna(0).clip(upper=30)
    hr = detail['heart_rate_bpm']
    zone_minutes = [
        round(float(delta_s[(hr >= HR_ZONE_BOUNDS[i]) & (hr < HR_ZONE_BOUNDS[i + 1])].sum() / 60), 1)
        for i in range(len(HR_ZONE_LABELS))
    ]

    return {
        'activity': activity_meta,
        'activity_id': str(activity_id),
        'points': _timeseries_records(detail),
        'zones': {'labels': HR_ZONE_LABELS, 'minutes': zone_minutes},
        'pause_windows': pause_windows,
    }


def _mock_activity_detail() -> dict:
    """A single synthetic cycling activity, for the 'precision dive' drill-in view.

    Shaped identically to _activity_detail_real_payload's `points` records
    (lat/lon are just always null here) so the frontend has one renderer
    for both.
    """
    rng = np.random.default_rng(123)
    duration_s = 60 * 62
    t = np.arange(0, duration_s, 5)
    n = len(t)

    speed = np.clip(16 + 6 * np.sin(t / 900) + rng.normal(0, 1.2, n), 3, 34)
    power = np.clip(150 + 60 * np.sin(t / 700 + 0.5) + rng.normal(0, 15, n), 0, 420)
    cadence = np.clip(82 + 8 * np.sin(t / 800) + rng.normal(0, 4, n), 0, 110)
    heart_rate = np.clip(130 + 25 * np.sin(t / 850 + 1.0) + rng.normal(0, 4, n), 95, 178)
    distance_mi = np.cumsum(speed * 5 / 3600)

    zone_minutes = [
        round(float(np.sum((heart_rate >= HR_ZONE_BOUNDS[i]) & (heart_rate < HR_ZONE_BOUNDS[i + 1])) * 5 / 60), 1)
        for i in range(len(HR_ZONE_LABELS))
    ]

    base_time = pd.Timestamp.today().normalize() + pd.Timedelta(hours=7)
    points = pd.DataFrame({
        'timestamp': [base_time + pd.Timedelta(seconds=int(s)) for s in t],
        'lat': None,
        'lon': None,
        'elevation_ft': None,
        'distance_mi': np.round(distance_mi, 3),
        'speed_mph': np.round(speed, 1),
        'cadence': np.round(cadence, 0),
        'heart_rate_bpm': np.round(heart_rate, 0),
        'power_w': np.round(power, 0),
    })

    return {
        'activity': {
            'type': 'cycling',
            'name': 'Example Ride (sample data)',
            'date': pd.Timestamp.today().date().isoformat(),
            'duration_min': round(duration_s / 60, 1),
            'distance_mi': round(float(distance_mi[-1]), 1),
        },
        'points': _timeseries_records(points),
        'zones': {'labels': HR_ZONE_LABELS, 'minutes': zone_minutes},
        'pause_windows': [],
    }


RECOMMENDATION_POOL = [
    {'type': 'Easy Run', 'detail': 'Zone 2, 35-40 min. Recovery emphasis after a higher-load stretch.'},
    {'type': 'Interval Ride', 'detail': '5x4min near threshold, 3min easy spin between reps.'},
    {'type': 'Rest Day', 'detail': 'Recent training load looks elevated relative to your usual pattern.'},
    {'type': 'Long Run', 'detail': '75-90 min conversational pace to build aerobic volume.'},
    {'type': 'Strength Session', 'detail': 'Lower-body focus — squat and hinge patterns due for progression.'},
]


@bp.route('/')
def index():
    # Redirect /garmin to /garmin/metrics_dashboard
    return redirect(url_for('garmin.metrics_dashboard'))

@bp.route('/metrics_dashboard')
def metrics_dashboard():
    refresh = request.args.get('refresh', '0') == '1'
    dashboard_dir = os.path.join(fm_local.local_dir, "dashboards/metric_timeseries")
    os.makedirs(dashboard_dir, exist_ok=True)
    context = {}
    for fname, context_key in DASHBOARD_FILES:
        local_path = os.path.join(dashboard_dir, fname)
        if refresh or not os.path.exists(local_path):
            # Fetch from S3 and save locally
            text = fm_s3.read_text(f"dashboards/metric_timeseries/{fname}")
            with open(local_path, 'w', encoding='utf-8') as f:
                f.write(text)
        else:
            with open(local_path, 'r', encoding='utf-8') as f:
                text = f.read()
        context[context_key] = text
    return render_template(
        'metrics_dashboard.html',
        **context
    )


@bp.route('/curated_metrics_dashboard')
def curated_metrics_dashboard():
    refresh = request.args.get('refresh', '0') == '1'
    source = request.args.get('source', DEFAULT_SOURCE)
    if source not in {'local', 's3'}:
        source = DEFAULT_SOURCE

    dashboard_dir = os.path.join(fm_local.local_dir, curated_dashboard_relative_dir(source))
    os.makedirs(dashboard_dir, exist_ok=True)

    if refresh:
        build_curated_dashboard_artifacts(source=source)
        if source == 's3':
            cache_curated_dashboard_artifacts_locally(source=source)

    context = {'source': source}
    for fname, context_key in CURATED_DASHBOARD_FILES:
        local_path = os.path.join(dashboard_dir, fname)
        if not os.path.exists(local_path):
            if source == 's3':
                cache_curated_dashboard_artifacts_locally(source=source)
            else:
                build_curated_dashboard_artifacts(source=source)
        with open(local_path, 'r', encoding='utf-8') as f:
            context[context_key] = f.read()

    return render_template('curated_metrics_dashboard.html', **context)


@bp.route('/quick_dashboard')
def quick_dashboard():
    source = request.args.get('source', DEFAULT_SOURCE)
    if source not in {'local', 's3'}:
        source = DEFAULT_SOURCE
    return render_template('quick_dashboard.html', source=source, active_section='health')


@bp.route('/api/quick_dashboard_data')
def quick_dashboard_data():
    import traceback
    
    source = request.args.get('source', DEFAULT_SOURCE)
    if source not in {'local', 's3'}:
        source = DEFAULT_SOURCE

    try:
        payload = _cached_or_live(f'quick_dashboard_{source}', source, lambda: _health_analyzed_payload(source=source))
        if payload is None:
            return jsonify({
                'source': source,
                'analyzed': {},
                'error': (
                    'No analyzed health data yet. Run '
                    'python -m garmin.scripts.manual_analyze_metrics to populate it.'
                ),
            })
        payload['source'] = source
        return jsonify(payload)
    except Exception as e:
        print(f"Error loading dashboard data: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@bp.route('/fitness')
def fitness():
    return render_template('fitness.html', active_section='fitness')


@bp.route('/api/fitness_data')
def api_fitness_data():
    import traceback

    sport = request.args.get('sport', 'running')
    source = request.args.get('source', DEFAULT_SOURCE)
    if source not in {'local', 's3'}:
        source = DEFAULT_SOURCE

    try:
        is_mock = False
        if sport == 'running':
            payload = _cached_or_live(f'fitness_running_{source}', source, lambda: _running_real_payload(source=source))
            if payload is None:
                payload = _running_payload()
                is_mock = True
        elif sport == 'lifting':
            payload = _cached_or_live(f'fitness_lifting_{source}', source, lambda: _lifting_real_payload(source=source))
            if payload is None:
                payload = _lifting_payload()
                is_mock = True
        else:
            return jsonify({'sport': sport, 'mock': True, 'available': False})
        payload.update({'sport': sport, 'mock': is_mock, 'available': True})
        return jsonify(payload)
    except Exception as e:
        print(f"Error loading fitness data: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@bp.route('/activities')
def activities():
    return render_template('activities.html', active_section='activities')


@bp.route('/api/activities_overview_data')
def api_activities_overview_data():
    import traceback

    source = request.args.get('source', DEFAULT_SOURCE)
    if source not in {'local', 's3'}:
        source = DEFAULT_SOURCE

    try:
        is_mock = False
        payload = _cached_or_live(f'activities_overview_{source}', source, lambda: _activities_real_payload(source=source))
        if payload is None:
            payload = _activities_overview_payload()
            is_mock = True
        payload['mock'] = is_mock
        return jsonify(payload)
    except Exception as e:
        print(f"Error loading activities overview data: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@bp.route('/api/activities_list_data')
def api_activities_list_data():
    import traceback

    source = request.args.get('source', DEFAULT_SOURCE)
    if source not in {'local', 's3'}:
        source = DEFAULT_SOURCE

    try:
        is_mock = False
        payload = _activities_list_payload(source=source)
        if payload is None:
            payload = _mock_activities_list_payload()
            is_mock = True
        payload['mock'] = is_mock
        return jsonify(payload)
    except Exception as e:
        print(f"Error loading activities list data: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@bp.route('/api/activity_detail_data')
def api_activity_detail_data():
    import traceback

    sport = request.args.get('sport', 'running')
    activity_id = request.args.get('activity_id')
    source = request.args.get('source', DEFAULT_SOURCE)
    if source not in {'local', 's3'}:
        source = DEFAULT_SOURCE

    try:
        is_mock = False
        payload = _activity_detail_real_payload(sport=sport, activity_id=activity_id, source=source)
        if payload is None:
            payload = _mock_activity_detail()
            is_mock = True
        payload['mock'] = is_mock
        return jsonify(payload)
    except Exception as e:
        print(f"Error loading activity detail data: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@bp.route('/analytics')
def analytics():
    return render_template('analytics.html', active_section='analytics')


@bp.route('/api/recommend')
def api_recommend():
    suggestion = _random.choice(RECOMMENDATION_POOL)
    return jsonify({
        'mock': True,
        'suggestion': suggestion,
        'note': 'Placeholder logic — real suggestions will draw on training load, recovery, and sleep once that modeling exists.',
    })


@bp.route('/data_status')
def data_status():
    return render_template('data_status.html', active_section='data_status')


@bp.route('/api/data_status_data')
def api_data_status_data():
    import traceback

    source = request.args.get('source', DEFAULT_SOURCE)
    if source not in {'local', 's3'}:
        source = DEFAULT_SOURCE

    try:
        payload = _cached_or_live(f'data_status_{source}', source, lambda: _data_status_payload(source=source))
        payload['source'] = source
        return jsonify(payload)
    except Exception as e:
        print(f"Error loading data status: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500