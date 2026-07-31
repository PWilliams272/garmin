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
import numpy as np
import pandas as pd
import os
import random as _random

RUNNING_ANALYZED_METRICS = ['cadence_spm', 'pace_min_per_mile', 'distance_mi']

QUICK_DASHBOARD_MA_BANDWIDTH_DAYS = 14
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


def _load_dashboard_timeseries(source: str = 'local') -> pd.DataFrame:
    store = curated_s3 if source == 's3' else curated_local

    heart_rate = store.load_daily('heart_rate')
    steps = store.load_daily('steps')
    health_stats = store.load_daily('health_stats')

    frames = []
    if not heart_rate.empty:
        hr_frame = heart_rate[['date', 'resting_hr']].copy()
        hr_frame['date'] = pd.to_datetime(hr_frame['date'])
        frames.append(hr_frame)
    if not steps.empty:
        steps_frame = steps[['date', 'total_steps']].copy()
        steps_frame['date'] = pd.to_datetime(steps_frame['date'])
        frames.append(steps_frame)
    if not health_stats.empty:
        weight_frame = health_stats[['date', 'weight', 'body_fat', 'bone_mass', 'muscle_mass']].copy()
        weight_frame['date'] = pd.to_datetime(weight_frame['date'])
        frames.append(weight_frame)

    if not frames:
        return pd.DataFrame(columns=['date', 'resting_hr', 'total_steps', 'weight', 'body_fat', 'bone_mass', 'muscle_mass'])

    combined = frames[0]
    for frame in frames[1:]:
        combined = combined.merge(frame, on='date', how='outer')

    combined = combined.sort_values('date').reset_index(drop=True)
    combined = _add_gaussian_moving_averages(
        combined, ['resting_hr', 'total_steps', 'weight', 'body_fat', 'bone_mass', 'muscle_mass']
    )
    return combined


def _add_gaussian_moving_averages(
    df: pd.DataFrame,
    columns: list[str],
    bandwidth: int = QUICK_DASHBOARD_MA_BANDWIDTH_DAYS,
) -> pd.DataFrame:
    present_columns = [col for col in columns if col in df.columns]
    if df.empty or not present_columns:
        for col in columns:
            df[f'{col}_ma'] = pd.Series(dtype='float64')
        return df

    processor = GarminDataProcessor()
    ma_df = processor.calculate_moving_averages(df, present_columns, kernels=['gaussian'], bandwidths=[bandwidth])
    for col in present_columns:
        df[f'{col}_ma'] = ma_df[f'{col}_gaussian_{bandwidth}']
    return df


def _timeseries_records(df: pd.DataFrame) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for row in df.to_dict(orient='records'):
        cleaned: dict[str, object] = {}
        for key, value in row.items():
            if key == 'date' and pd.notnull(value):
                cleaned[key] = pd.Timestamp(value).date().isoformat()
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


REAL_LIFTING_EXERCISES = ['bench_press', 'squat', 'curl']


def _running_real_payload(source: str = 'local') -> dict | None:
    store = curated_s3 if source == 's3' else curated_local
    df = store.load_activity_summary('running')
    if df.empty:
        return None

    df = df.copy()
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date').reset_index(drop=True)

    # Quality classification + GP trend fitting are precomputed by
    # garmin.scripts.manual_analyze_activities into curated/analyzed/ — this
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

    processor = GarminDataProcessor()
    exercises = {}
    for exercise in REAL_LIFTING_EXERCISES:
        ex_df = detail[detail['exercise'] == exercise]
        if ex_df.empty:
            exercises[exercise] = []
            continue

        top_sets = (
            ex_df.groupby(['activity_id', 'date'], as_index=False)['weight_lb']
            .max()
            .rename(columns={'weight_lb': 'top_weight_lb'})
            .dropna(subset=['top_weight_lb'])
            .sort_values('date')
            .reset_index(drop=True)
        )
        if top_sets.empty:
            exercises[exercise] = []
            continue

        ma_df = processor.calculate_moving_averages(
            top_sets, ['top_weight_lb'], kernels=['gaussian'], bandwidths=[MOCK_MA_BANDWIDTH_DAYS]
        )
        top_sets['top_weight_lb_ma'] = ma_df[f'top_weight_lb_gaussian_{MOCK_MA_BANDWIDTH_DAYS}']
        exercises[exercise] = _timeseries_records(top_sets[['date', 'top_weight_lb', 'top_weight_lb_ma']])

    weekly_frequency = (
        detail.drop_duplicates(subset=['activity_id', 'exercise'])
        .set_index('date')
        .groupby([pd.Grouper(freq='W-MON'), 'exercise'])
        .size()
        .reset_index(name='sessions')
    )
    return {
        'exercises': exercises,
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


def _activities_overview_payload() -> dict:
    df = _mock_activities()

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


def _mock_activity_detail() -> dict:
    """A single synthetic cycling activity, for the 'precision dive' drill-in view."""
    rng = np.random.default_rng(123)
    duration_s = 60 * 62
    t = np.arange(0, duration_s, 5)
    n = len(t)

    speed = np.clip(16 + 6 * np.sin(t / 900) + rng.normal(0, 1.2, n), 3, 34)
    power = np.clip(150 + 60 * np.sin(t / 700 + 0.5) + rng.normal(0, 15, n), 0, 420)
    cadence = np.clip(82 + 8 * np.sin(t / 800) + rng.normal(0, 4, n), 0, 110)
    heart_rate = np.clip(130 + 25 * np.sin(t / 850 + 1.0) + rng.normal(0, 4, n), 95, 178)

    zone_labels = ['Z1 Recovery', 'Z2 Endurance', 'Z3 Tempo', 'Z4 Threshold', 'Z5 VO2max']
    zone_bounds = [0, 114, 133, 152, 171, 999]
    zone_minutes = [
        round(float(np.sum((heart_rate >= zone_bounds[i]) & (heart_rate < zone_bounds[i + 1])) * 5 / 60), 1)
        for i in range(len(zone_labels))
    ]

    return {
        'activity': {
            'type': 'cycling',
            'name': 'Example Ride (sample data)',
            'date': pd.Timestamp.today().date().isoformat(),
            'duration_min': round(duration_s / 60, 1),
            'distance_mi': round(float(np.trapz(speed, dx=5) / 3600), 1),
        },
        'time_s': t.tolist(),
        'speed_mph': np.round(speed, 1).tolist(),
        'power_w': np.round(power, 0).tolist(),
        'cadence_rpm': np.round(cadence, 0).tolist(),
        'heart_rate_bpm': np.round(heart_rate, 0).tolist(),
        'zones': {'labels': zone_labels, 'minutes': zone_minutes},
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
    source = request.args.get('source', 'local')
    if source not in {'local', 's3'}:
        source = 'local'

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
    source = request.args.get('source', 'local')
    if source not in {'local', 's3'}:
        source = 'local'
    return render_template('quick_dashboard.html', source=source, active_section='health')


@bp.route('/api/quick_dashboard_data')
def quick_dashboard_data():
    import traceback
    
    source = request.args.get('source', 'local')
    if source not in {'local', 's3'}:
        source = 'local'

    try:
        df = _load_dashboard_timeseries(source=source)
        return jsonify({
            'source': source,
            'rows': _timeseries_records(df),
        })
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
    source = request.args.get('source', 'local')
    if source not in {'local', 's3'}:
        source = 'local'

    try:
        is_mock = False
        if sport == 'running':
            payload = _running_real_payload(source=source)
            if payload is None:
                payload = _running_payload()
                is_mock = True
        elif sport == 'lifting':
            payload = _lifting_real_payload(source=source)
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

    try:
        payload = _activities_overview_payload()
        payload['mock'] = True
        return jsonify(payload)
    except Exception as e:
        print(f"Error loading activities overview data: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@bp.route('/api/activity_detail_data')
def api_activity_detail_data():
    import traceback

    try:
        payload = _mock_activity_detail()
        payload['mock'] = True
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