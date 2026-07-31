from flask import Blueprint, jsonify, render_template, request, url_for, redirect
from garmin.io.curated_store import CuratedDataStore
from garmin.io.file_manager import FileManager
from garmin.dashboard_curated import (
    build_curated_dashboard_artifacts,
    cache_curated_dashboard_artifacts_locally,
    curated_dashboard_relative_dir,
)
from garmin.data_processor.processor import GarminDataProcessor
import pandas as pd
import os

QUICK_DASHBOARD_MA_BANDWIDTH_DAYS = 14

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
    return render_template('quick_dashboard.html', source=source)


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