from flask import Blueprint, Response, jsonify, render_template, request, url_for, redirect
from garmin.io.curated_store import CuratedDataStore
from garmin.io.file_manager import FileManager
from garmin.data_processor.processor import GarminDataProcessor
from garmin.analysis.quality import classify_metric
from garmin.analysis.trend_gp import fit_gp_trend
from garmin.analysis.model_report import build_model_report
from garmin.analysis.analysis_pipeline import (
    LOAD_TYPE_BODYWEIGHT,
    session_fatigue_curve,
    STRENGTH_EXERCISE_CANDIDATES,
    load_type_for,
    variant_slug,
)
from garmin.datasets import ACTIVITY_DATASETS
from garmin.prototypes.activity_explorer import (
    build_activity_explorer_html,
    blended_1rm,
    EXERCISE_MUSCLES,
    format_exercise_label,
    _front_body_svg,
    _back_body_svg,
)
import numpy as np
import pandas as pd
import math
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


def _cached_html_or_live(cache_name: str, source: str, build_fn):
    """Same contract as _cached_or_live, for a precomputed full HTML page
    (see garmin.prototypes.activity_explorer) rather than a JSON payload."""
    store = curated_s3 if source == 's3' else curated_local
    cached = store.load_viewer_cache_html(cache_name)
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

    # Keyed on *variant*, not Garmin's category. A "bench_press" series that
    # mixes 55 lb dumbbell work with 135 lb barbell work isn't one exercise,
    # and plotting both on one axis reads as a strength jump that never
    # happened. analyze_lifting writes an index of what it produced.
    index = store.load_strength_variant_index('strength')
    if index.empty:
        return None

    exercises = {}
    for _, row in index.iterrows():
        slug, variant = str(row['slug']), str(row['variant'])
        load_type = str(row['load_type'])
        if load_type == LOAD_TYPE_BODYWEIGHT:
            metric, value_key, unit = 'top_reps', 'top_reps', 'reps'
            volume_metric = f'{slug}_rep_volume'
        else:
            metric, value_key, unit = '1rm', 'est_1rm', 'lb'
            volume_metric = f'{slug}_volume'

        points = store.load_analyzed_points('strength', f'{slug}_{metric}')
        if points.empty:
            continue
        trend = store.load_analyzed_trend('strength', f'{slug}_{metric}', kind='sts')
        volume_trend = store.load_analyzed_trend('strength', volume_metric, kind='sts')
        curve = store.load_strength_curve('strength', slug)
        exercises[variant] = {
            'points': _timeseries_records(points),
            'trend': _timeseries_records(trend) if not trend.empty else [],
            'volume_trend': _timeseries_records(volume_trend) if not volume_trend.empty else [],
            'sessions': int(row['sessions']),
            'load_type': load_type,
            'value_key': value_key,
            'unit': unit,
            'family': str(row['family']),
            'median_weight': float(row['median_weight']) if pd.notnull(row.get('median_weight')) else None,
            # Posterior eXRM from the load-rep model, when the offline
            # sampling job has run for this variant. Empty is normal.
            'curve': _timeseries_records(curve) if not curve.empty else [],
        }

    if not exercises:
        return None

    exercise_order = sorted(exercises, key=lambda ex: exercises[ex]['sessions'], reverse=True)
    families: dict[str, list[str]] = {}
    for variant in exercise_order:
        families.setdefault(exercises[variant]['family'], []).append(variant)

    # Date -> activity_id, so a point on a progression chart can link through
    # to the session that produced it. Analysed points are keyed by date only
    # (one row per session date), and strength sessions are effectively one
    # per day here, so the date is a sound join key; where a day somehow has
    # two, the first is used rather than guessing.
    session_links = (
        detail.dropna(subset=['date'])
        .assign(_d=lambda f: pd.to_datetime(f['date']).dt.strftime('%Y-%m-%d'))
        .drop_duplicates(subset=['_d'])
        .set_index('_d')['activity_id']
        .astype(str)
        .to_dict()
    )

    # The combined per-family series: every variant converted onto one scale,
    # so "is my bench progressing" has an answer that doesn't jump when the
    # implement changes. analyze_lifting writes these as family_<slug>_1rm.
    conversions = store.load_variant_conversions('strength')
    combined: dict[str, dict] = {}
    for family in families:
        slug = variant_slug(family)
        points = store.load_analyzed_points('strength', f'family_{slug}_1rm')
        if points.empty:
            continue
        trend = store.load_analyzed_trend('strength', f'family_{slug}_1rm', kind='sts')
        rows = conversions[conversions['family'] == family] if not conversions.empty else pd.DataFrame()
        combined[family] = {
            'points': _timeseries_records(points),
            'trend': _timeseries_records(trend) if not trend.empty else [],
            'reference': str(rows['reference'].iloc[0]) if len(rows) else None,
            # The variant whose units the series is expressed in. Not always
            # the fitting anchor: the anchor is chosen for identifiability
            # (always a labelled variant), the display scale for familiarity
            # (the most-trained one). Factors are relative to this.
            'display_variant': (
                str(rows['display_variant'].iloc[0])
                if len(rows) and 'display_variant' in rows.columns
                   and pd.notnull(rows['display_variant'].iloc[0])
                else (str(rows['reference'].iloc[0]) if len(rows) else None)
            ),
            'factors': [
                {
                    'variant': str(r['variant']),
                    'factor': float(r['factor']),
                    'identified': bool(r['identified']),
                    'n_sessions': int(r['n_sessions']),
                    'paired_ratio': None if pd.isnull(r.get('paired_ratio')) else float(r['paired_ratio']),
                    'overlap_days': None if pd.isnull(r.get('overlap_days')) else float(r['overlap_days']),
                }
                for _, r in rows.iterrows()
            ],
        }

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
        # family -> [variant, ...]. Variants of one movement belong on the
        # same chart (they're the same progression) but need distinguishing,
        # since a dumbbell and a barbell version sit at different loads.
        'families': families,
        # family -> combined series + the conversion factors behind it.
        'combined': combined,
        # date -> activity_id, for linking a plotted point to its session.
        'session_links': session_links,
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
    avg_hr_by_type = {'running': 148, 'cycling': 132, 'climbing': 118, 'lifting': 110, 'swimming': 138}

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
                avg_hr = max(80.0, rng.normal(avg_hr_by_type[activity_type], 8))
                rows.append({
                    'date': activity_date,
                    'type': activity_type,
                    'duration_min': round(float(duration), 1),
                    'distance_mi': round(float(distance), 2) if distance is not None else None,
                    'avg_hr': round(float(avg_hr)),
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
        frame['avg_hr'] = summary.get('avg_hr') if 'avg_hr' in summary.columns else None
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

    # Effort/intensity proxy -- weekly mean avg_hr per type. Distance and
    # hours (above) are weak or misleading effort signals for non-GPS,
    # widely-variable-duration sports (e.g. a 500min bouldering session
    # isn't 8x the effort of a 60min one), so this is a separate, sparser
    # series: only weeks where that type has at least one session with a
    # recorded avg_hr produce a point (many older/manually-logged sessions
    # have none at all -- see ACTIVITY_DATASETS coverage).
    weekly_effort = pd.DataFrame(columns=['date', 'type', 'avg_hr'])
    if 'avg_hr' in df.columns:
        weekly_effort = (
            df.dropna(subset=['avg_hr'])
            .set_index('date')
            .groupby([pd.Grouper(freq='W-MON'), 'type'])['avg_hr']
            .mean()
            .round(1)
            .reset_index()
        )

    recent = df.sort_values('date', ascending=False).head(15)

    return {
        'activity_types': sorted(df['type'].unique().tolist()),
        'weekly_by_type': _timeseries_records(weekly_by_type),
        'weekly_totals': _timeseries_records(weekly_totals),
        'weekly_effort_by_type': _timeseries_records(weekly_effort),
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


def _exercise_pr_context(store: CuratedDataStore, exercise: str, session_date, session_best_1rm: float | None) -> dict | None:
    """How today's best set for one exercise compares to your history, read
    from garmin.analysis.analysis_pipeline.analyze_lifting's precomputed
    curated/analyzed/strength/<exercise>_1rm_* output (same "no live model
    fitting" rule as everywhere else -- this only reads already-fit points/
    trend, it doesn't fit anything itself). Returns None if that exercise
    hasn't cleared STRENGTH_MIN_SESSIONS yet (no analyzed output exists).

    prior_best_1rm is the max across sessions strictly *before* this one --
    "did today beat your PR going in," not including today's own result.
    trend_1rm is the smoothed STS trend's value nearest this session's date
    (last point at-or-before it, else the earliest available point if this
    session predates the trend's own range). days_since_last_session is the
    gap to the most recent earlier session of this exercise.
    """
    if session_best_1rm is None:
        return None

    points = store.load_analyzed_points('strength', f'{exercise}_1rm')
    if points.empty:
        return None
    points = points.copy()
    points['date'] = pd.to_datetime(points['date'])
    # analyze_lifting's points are one per calendar date (date-only, time
    # normalized to midnight); session_date carries this session's actual
    # time-of-day (from set_start_time). Comparing those directly made
    # today's own point count as "prior" (its midnight timestamp is earlier
    # than today's actual set-start time) -- normalize both to the
    # calendar date so "prior" genuinely excludes today.
    session_date = pd.Timestamp(session_date).normalize()

    prior = points[points['date'] < session_date]
    prior_best = float(prior['est_1rm'].max()) if not prior.empty else None
    days_since_last = int((session_date - prior['date'].max()).days) if not prior.empty else None

    trend_at_date = None
    trend = store.load_analyzed_trend('strength', f'{exercise}_1rm', kind='sts')
    if not trend.empty:
        trend = trend.copy()
        trend['date'] = pd.to_datetime(trend['date'])
        at_or_before = trend[trend['date'] <= session_date]
        nearest = at_or_before.sort_values('date').iloc[-1] if not at_or_before.empty else trend.sort_values('date').iloc[0]
        trend_at_date = float(nearest['mean'])

    if prior_best is None and trend_at_date is None:
        return None

    return {
        'prior_best_1rm': round(prior_best, 1) if prior_best is not None else None,
        'trend_1rm': round(trend_at_date, 1) if trend_at_date is not None else None,
        'days_since_last_session': days_since_last,
        'pct_of_prior_best': round(session_best_1rm / prior_best * 100) if prior_best else None,
        'pct_of_trend': round(session_best_1rm / trend_at_date * 100) if trend_at_date else None,
    }


def _strength_activity_detail_payload(activity_id: str | None = None, source: str = 'local') -> dict | None:
    """Per-set detail (exercise, reps, weight, rest between sets) plus a
    session muscle-load breakdown for one strength_training activity --
    the strength-tab analog of _activity_detail_real_payload's map/pace/HR
    view, which doesn't apply here (no GPS/FIT sensor stream for lifting).

    Set-level data comes from ActivityPuller.get_strength_workout via the
    scheduled updater (curated/activities/detail/strength/, keyed by
    activity_id directly -- not the "<sport>_timeseries" naming cardio
    sports use, since this was wired in before that convention existed).
    Rest and HR come from the FIT file's native set/record messages
    (rest_before_s/hr_avg/hr_max columns) when the puller found them;
    activities pulled before that enrichment landed (or where the FIT
    parse failed) fall back to a derived rest-from-timestamp-gap estimate
    and no HR. Muscle load is volume (reps * weight, or just reps for a
    bodyweight movement) distributed across each exercise's
    EXERCISE_MUSCLES activation fractions and summed -- the same model
    garmin.prototypes.activity_explorer's multi-session view uses, just
    scoped to one session. 1RM is per-set (see blended_1rm) -- read it
    loosely for anything that isn't a near-max-effort set (a 15-rep
    warmup's "1RM" is a much noisier extrapolation than a 3-rep top set).
    """
    store = curated_s3 if source == 's3' else curated_local

    if activity_id is None:
        prefix = 'curated/activities/detail/strength/'
        files = [f for f in store.file_manager.list_files(prefix) if f.endswith('.parquet')]
        if not files:
            return None
        ids = [f.rsplit('activity_id=', 1)[-1].removesuffix('.parquet') for f in files]
        summary_all = store.load_activity_summary('strength')
        if not summary_all.empty:
            candidates = summary_all[summary_all['activity_id'].astype(str).isin(ids)]
            if not candidates.empty:
                activity_id = str(candidates.sort_values('date').iloc[-1]['activity_id'])
        if activity_id is None:
            activity_id = ids[0]

    detail = store.load_activity_detail('strength', str(activity_id))
    detail = detail.dropna(subset=['reps'])
    if detail.empty:
        return None

    detail = detail.sort_values(['set_index'], na_position='last').reset_index(drop=True)
    start = pd.to_datetime(detail.get('set_start_time'), errors='coerce')
    duration = pd.to_numeric(detail.get('duration_s'), errors='coerce')

    # Prefer the FIT-native rest duration (pulled straight from the file's
    # own alternating active/rest `set` messages) when the puller found it;
    # only derive from the timestamp gap for rows/sessions missing it (data
    # pulled before this enrichment, or a FIT parse that failed).
    fit_rest = pd.to_numeric(detail.get('rest_before_s'), errors='coerce') if 'rest_before_s' in detail.columns else pd.Series([None] * len(detail))
    rest_s = [None] * len(detail)
    for i in range(len(detail)):
        if pd.notnull(fit_rest.iloc[i]):
            rest_s[i] = round(float(fit_rest.iloc[i]))
            continue
        if i == 0 or pd.isna(start.iloc[i]) or pd.isna(start.iloc[i - 1]):
            continue
        prior_duration = duration.iloc[i - 1] if pd.notnull(duration.iloc[i - 1]) else 0.0
        prior_end = start.iloc[i - 1] + pd.Timedelta(seconds=float(prior_duration))
        gap = (start.iloc[i] - prior_end).total_seconds()
        rest_s[i] = round(gap) if gap > 0 else 0

    muscle_load: dict[str, float] = {}
    exercise_labels = []
    one_rm = []
    candidates = []
    muscles_per_set = []
    for _, row in detail.iterrows():
        exercise = str(row.get('exercise') or 'unknown')
        exercise_labels.append(format_exercise_label(exercise))
        reps = row.get('reps') or 0
        weight = row.get('weight_lb')
        volume = float(reps) * float(weight) if pd.notnull(weight) else float(reps)
        muscles_per_set.append(list(EXERCISE_MUSCLES.get(exercise, {}).keys()))
        for muscle, fraction in EXERCISE_MUSCLES.get(exercise, {}).items():
            muscle_load[muscle] = muscle_load.get(muscle, 0.0) + volume * fraction

        estimate = blended_1rm(weight, reps) if pd.notnull(weight) else float('nan')
        one_rm.append(round(estimate, 1) if pd.notnull(estimate) and not math.isnan(estimate) else None)

        row_candidates = []
        for i in range(1, 4):
            cand_exercise = row.get(f'candidate_{i}_exercise')
            cand_prob = row.get(f'candidate_{i}_probability')
            if pd.isna(cand_exercise):
                continue
            row_candidates.append({
                'exercise': format_exercise_label(str(cand_exercise)),
                'probability': round(float(cand_prob), 1) if pd.notnull(cand_prob) else None,
            })
        candidates.append(row_candidates)

    sets_df = detail[['exercise', 'reps', 'weight_lb']].copy()
    sets_df['exercise_label'] = exercise_labels
    sets_df['exercise_name'] = detail.get('exercise_name')
    sets_df['manually_reviewed'] = detail['manually_reviewed'].fillna(False) if 'manually_reviewed' in detail.columns else False
    sets_df['rest_s'] = rest_s
    sets_df['set_number'] = range(1, len(detail) + 1)
    sets_df['one_rm_lb'] = one_rm
    sets_df['hr_avg'] = pd.to_numeric(detail.get('hr_avg'), errors='coerce') if 'hr_avg' in detail.columns else None
    sets_df['hr_max'] = pd.to_numeric(detail.get('hr_max'), errors='coerce') if 'hr_max' in detail.columns else None
    # Needed to lay the session out on one clock (see _add_session_timeline).
    sets_df['duration_s'] = pd.to_numeric(detail.get('duration_s'), errors='coerce') if 'duration_s' in detail.columns else None
    sets_df['set_start_time'] = detail.get('set_start_time') if 'set_start_time' in detail.columns else None

    hr_series = []
    for _, row in detail.iterrows():
        t_series = row.get('hr_series_t')
        bpm_series = row.get('hr_series_bpm')
        if isinstance(t_series, (list, np.ndarray)) and len(t_series):
            hr_series.append({'t': list(t_series), 'bpm': [float(v) for v in bpm_series]})
        else:
            hr_series.append(None)

    session_reviewed = bool(sets_df['manually_reviewed'].any())

    session_date_for_pr = pd.to_datetime(detail['set_start_time'], errors='coerce').min()
    exercise_pr: dict[str, dict] = {}
    if pd.notnull(session_date_for_pr):
        for exercise in sets_df['exercise'].unique():
            exercise_sets = sets_df[sets_df['exercise'] == exercise]
            session_best = exercise_sets['one_rm_lb'].max()
            session_best = float(session_best) if pd.notnull(session_best) else None
            context = _exercise_pr_context(store, str(exercise), session_date_for_pr, session_best)
            if context is not None:
                exercise_pr[str(exercise)] = context

    summary = store.load_activity_summary('strength')
    activity_meta = {'type': 'strength', 'name': None, 'date': None, 'duration_min': None}
    meta_row = summary[summary['activity_id'].astype(str) == str(activity_id)] if not summary.empty else summary
    if not meta_row.empty:
        r = meta_row.iloc[0]
        activity_meta.update({
            'name': r.get('name'),
            'date': pd.Timestamp(r['date']).date().isoformat() if pd.notnull(r.get('date')) else None,
            'duration_min': float(r['duration_min']) if pd.notnull(r.get('duration_min')) else None,
        })

    set_records = _timeseries_records(sets_df)
    for record, row_candidates, muscles, hr_pts in zip(set_records, candidates, muscles_per_set, hr_series, strict=True):
        record['candidates'] = row_candidates
        record['muscles'] = muscles
        record['hr_series'] = hr_pts

    fatigue_curve = _add_session_timeline(set_records, sets_df, exercise_pr)

    return {
        'activity': activity_meta,
        'activity_id': str(activity_id),
        'sets': set_records,
        'muscle_load': muscle_load,
        'session_reviewed': session_reviewed,
        'exercise_pr': exercise_pr,
        'fatigue_curve': fatigue_curve,
    }


def _add_session_timeline(set_records: list[dict], sets_df: pd.DataFrame, exercise_pr: dict) -> dict | None:
    """Place every set on one session clock, for the HR/set-band timeline.

    Each set's `hr_series_t` is measured from that set's own start and only
    spans the set itself -- Garmin gives no heart rate between sets -- so the
    rest gaps genuinely have no data and render as empty space, which is what
    a rest period should look like anyway.

    Adds to each record: `t_start_s` / `t_end_s` (seconds from the session's
    first set), `hr_abs` (the same HR points shifted onto that clock) and
    `pct_of_ref_1rm`, the set's load as a share of the best estimate of that
    exercise's 1RM available -- prior best if there is history, else the
    session's own best.
    """
    if not set_records or 'set_start_time' not in sets_df.columns:
        return None
    starts = pd.to_datetime(sets_df['set_start_time'], errors='coerce')
    if starts.isna().all():
        return None
    origin = starts.min()

    session_best = sets_df.groupby('exercise')['one_rm_lb'].max().to_dict()
    for record, start in zip(set_records, starts, strict=True):
        if pd.isna(start):
            record['t_start_s'] = None
            record['t_end_s'] = None
            record['hr_abs'] = None
            record['pct_of_ref_1rm'] = None
            continue
        offset = float((start - origin).total_seconds())
        duration = record.get('duration_s')
        record['t_start_s'] = round(offset, 1)
        record['t_end_s'] = round(offset + float(duration), 1) if duration else None

        hr = record.get('hr_series')
        record['hr_abs'] = (
            {'t': [round(offset + float(t), 1) for t in hr['t']], 'bpm': hr['bpm']}
            if hr and hr.get('t') else None
        )

        exercise = record.get('exercise')
        context = exercise_pr.get(str(exercise)) or {}
        reference = context.get('prior_best_1rm') or session_best.get(exercise)
        # Intensity as the *estimated 1RM this set demonstrates*, relative to
        # the best 1RM known for that exercise -- not raw weight over 1RM.
        # Raw weight understates a hard high-rep set: 10 reps at 135 lb and
        # 3 reps at 185 lb are similar efforts, but only the rep-adjusted
        # estimate says so.
        set_1rm = record.get('one_rm_lb')
        record['pct_of_ref_1rm'] = (
            round(float(set_1rm) / float(reference) * 100) if set_1rm and reference else None
        )

    # Within-session fatigue (see analysis_pipeline.session_fatigue_curve --
    # a Banister-style heuristic, not a fitted result). Sampled on a regular
    # grid so it draws as a smooth curve rather than only at set times.
    placed = [r for r in set_records if r.get('t_start_s') is not None]
    if placed:
        work = [
            float(r.get('reps') or 0) * float(r.get('weight_lb') or 0) or float(r.get('reps') or 0)
            for r in placed
        ]
        span = max((r.get('t_end_s') or r['t_start_s']) for r in placed)
        grid = [i * 15.0 for i in range(int(span / 15) + 2)]
        curve = session_fatigue_curve([r['t_start_s'] for r in placed], work, grid)
        return {'t': grid, 'value': curve}
    return None


def _mock_strength_activity_detail() -> dict:
    """Synthetic single strength session, shaped like
    _strength_activity_detail_payload's output, for when no real strength
    detail has been pulled for this account/environment yet."""
    rng = np.random.default_rng(11)
    exercises = ['bench_press', 'squat', 'row', 'curl']
    rows = []
    set_number = 1
    for exercise in exercises:
        base_weight = {'bench_press': 135.0, 'squat': 185.0, 'row': 95.0, 'curl': 30.0}[exercise]
        for _ in range(int(rng.integers(3, 5))):
            reps = int(rng.integers(6, 11))
            weight_lb = round(base_weight + float(rng.normal(0, 5)), 1)
            duration_s = int(rng.integers(25, 45))
            hr_base = float(rng.normal(115, 12))
            rows.append({
                'exercise': exercise,
                'exercise_label': format_exercise_label(exercise),
                'exercise_name': None,
                'manually_reviewed': False,
                'reps': reps,
                'weight_lb': weight_lb,
                'rest_s': int(rng.integers(60, 150)) if set_number > 1 else None,
                'set_number': set_number,
                'one_rm_lb': round(blended_1rm(weight_lb, reps), 1),
                'hr_avg': round(hr_base, 1),
                'hr_max': round(hr_base + 12, 1),
                'candidates': [{'exercise': format_exercise_label(exercise), 'probability': 80.0}],
                'muscles': list(EXERCISE_MUSCLES.get(exercise, {}).keys()),
                'hr_series': {
                    't': list(range(0, duration_s, 2)),
                    'bpm': [round(hr_base + i * 0.3, 1) for i in range(len(range(0, duration_s, 2)))],
                },
            })
            set_number += 1

    muscle_load: dict[str, float] = {}
    for row in rows:
        volume = row['reps'] * row['weight_lb']
        for muscle, fraction in EXERCISE_MUSCLES.get(row['exercise'], {}).items():
            muscle_load[muscle] = muscle_load.get(muscle, 0.0) + volume * fraction

    return {
        'activity': {
            'type': 'strength', 'name': 'Example Strength Session (sample data)',
            'date': pd.Timestamp.today().date().isoformat(), 'duration_min': 52.0,
        },
        'sets': rows,
        'muscle_load': muscle_load,
        'session_reviewed': False,
        'exercise_pr': {},
    }


def _load_exercise_review_with_context(store: CuratedDataStore) -> pd.DataFrame:
    """Loads exercise_review.parquet and joins in date/name from the
    strength activity summary, plus display labels for both the original
    and our-guess exercise. Shared by the activities-list and
    activity-detail payload builders below.
    """
    review = store.load_exercise_review('strength')
    if review.empty:
        return review

    summary = store.load_activity_summary('strength')
    if not summary.empty:
        summary = summary[['activity_id', 'date', 'name']].copy()
        summary['activity_id'] = summary['activity_id'].astype(str)
        review = review.merge(summary, on='activity_id', how='left')
    else:
        review['date'] = None
        review['name'] = None

    review['original_exercise_label'] = review['original_exercise'].apply(
        lambda ex: format_exercise_label(str(ex)) if pd.notnull(ex) else None
    )
    review['our_guess_label'] = review['our_guess_exercise'].apply(
        lambda ex: format_exercise_label(str(ex)) if pd.notnull(ex) else None
    )
    return review


def _exercise_review_status_counts(statuses: pd.Series) -> dict[str, int]:
    counts = statuses.value_counts().to_dict()
    return {
        'pending': int(counts.get('pending', 0)),
        'accepted': int(counts.get('accepted', 0)),
        'rejected': int(counts.get('rejected', 0)),
        'garmin_confirmed': int(counts.get('garmin_confirmed', 0)),
    }


def _exercise_review_activities_payload(source: str = 'local') -> dict | None:
    """One row per strength activity with review-status counts across its
    sets, so the review UI can group by activity (pick a session, review
    every set in it) instead of a flat cross-session queue. Same cheap
    read-live rationale as the per-activity payload below -- everything
    needed is already denormalized into exercise_review.parquet.
    """
    store = curated_s3 if source == 's3' else curated_local
    review = _load_exercise_review_with_context(store)
    if review.empty:
        return None

    grouped = review.groupby('activity_id', dropna=False)
    rows = []
    for activity_id, group in grouped:
        status_counts = _exercise_review_status_counts(group['review_status'])
        date = group['date'].iloc[0] if 'date' in group.columns else None
        name = group['name'].iloc[0] if 'name' in group.columns else None
        rows.append({
            'activity_id': activity_id,
            'date': date,
            'name': name,
            'total_sets': int(len(group)),
            **status_counts,
        })
    activities = pd.DataFrame(rows)
    activities = activities.sort_values(['pending', 'date'], ascending=[False, False])

    return {
        'activities': _timeseries_records(activities),
        'counts': _exercise_review_status_counts(review['review_status']),
    }


def _exercise_review_activity_detail_payload(activity_id: str, source: str = 'local') -> dict | None:
    """Every set in one strength activity, in set-number order, for the
    per-activity review screen (edit an individual set's exercise label,
    then submit the whole session's currently-pending sets as approved).
    """
    store = curated_s3 if source == 's3' else curated_local
    review = _load_exercise_review_with_context(store)
    if review.empty:
        return None

    activity_review = review[review['activity_id'].astype(str) == str(activity_id)]
    if activity_review.empty:
        return None
    activity_review = activity_review.sort_values('set_number')

    date_value = activity_review['date'].iloc[0] if 'date' in activity_review.columns else None
    return {
        'activity_id': str(activity_id),
        'date': pd.Timestamp(date_value).date().isoformat() if pd.notnull(date_value) else None,
        'name': activity_review['name'].iloc[0] if 'name' in activity_review.columns else None,
        'sets': _timeseries_records(activity_review),
    }


def _exercise_review_options() -> list[dict[str, str]]:
    """Canonical exercise-name options for the review UI's edit dropdown --
    every exercise EXERCISE_MUSCLES knows how to map to a muscle heatmap,
    sorted by display label.
    """
    options = [{'value': name, 'label': format_exercise_label(name)} for name in EXERCISE_MUSCLES]
    options.append({'value': 'unknown', 'label': format_exercise_label('unknown')})
    return sorted(options, key=lambda o: o['label'])


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
    return redirect(url_for('garmin.quick_dashboard'))


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
    # Static (session-independent) muscle-map markup, rendered once here
    # rather than re-sent on every /api/activity_detail_data response --
    # the JS colors .muscle-region fills per-session from the JSON payload.
    return render_template(
        'activities.html', active_section='activities',
        front_body_svg=_front_body_svg(), back_body_svg=_back_body_svg(),
    )


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
        payload = _cached_or_live(f'activities_list_{source}', source, lambda: _activities_list_payload(source=source))
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
        if sport == 'strength':
            payload = _strength_activity_detail_payload(activity_id=activity_id, source=source)
            if payload is None:
                payload = _mock_strength_activity_detail()
                is_mock = True
        else:
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


@bp.route('/exercise_review')
def exercise_review():
    return render_template('exercise_review.html', active_section='exercise_review')


@bp.route('/modeling')
def modeling():
    return render_template('modeling.html', active_section='modeling')


@bp.route('/api/model_report_data')
def api_model_report_data():
    """Evidence behind the strength/wellness modeling work.

    Read-only like every other page: the report is assembled offline by
    garmin.analysis.model_report.build_model_report and cached, because
    several sections scan every per-activity strength file, which is far too
    expensive to do per request against S3.
    """
    import traceback

    source = request.args.get('source', DEFAULT_SOURCE)
    if source not in {'local', 's3'}:
        source = DEFAULT_SOURCE

    try:
        store = curated_s3 if source == 's3' else curated_local
        # The cache key carries the source suffix, matching what
        # manual_build_viewer_cache writes (model_report_local.json). Without
        # it this looked for model_report.json, missed every time, and rebuilt
        # the whole report live on each request -- 3-5s per page load.
        payload = _cached_or_live(
            f'model_report_{source}', source, lambda: build_model_report(store)
        )
        return jsonify(payload or {})
    except Exception as e:
        print(f"Error loading model report: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@bp.route('/api/exercise_review_activities')
def api_exercise_review_activities():
    import traceback

    source = request.args.get('source', DEFAULT_SOURCE)
    if source not in {'local', 's3'}:
        source = DEFAULT_SOURCE

    try:
        payload = _exercise_review_activities_payload(source=source)
        if payload is None:
            payload = {'activities': [], 'counts': {'pending': 0, 'accepted': 0, 'rejected': 0, 'garmin_confirmed': 0}}
        return jsonify(payload)
    except Exception as e:
        print(f"Error loading exercise review activities: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@bp.route('/api/exercise_review_activity')
def api_exercise_review_activity():
    import traceback

    source = request.args.get('source', DEFAULT_SOURCE)
    if source not in {'local', 's3'}:
        source = DEFAULT_SOURCE
    activity_id = request.args.get('activity_id')
    if not activity_id:
        return jsonify({'error': 'activity_id is required'}), 400

    try:
        payload = _exercise_review_activity_detail_payload(activity_id, source=source)
        if payload is None:
            return jsonify({'error': 'activity not found'}), 404
        return jsonify(payload)
    except Exception as e:
        print(f"Error loading exercise review activity detail: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@bp.route('/api/exercise_review_options')
def api_exercise_review_options():
    return jsonify({'options': _exercise_review_options()})


@bp.route('/api/exercise_review_decision', methods=['POST'])
def api_exercise_review_decision():
    """Write endpoint for a single set's review decision -- used by the
    per-row Reject action. Everywhere else in this app is read-only
    precomputed output, but a review decision has to happen live, in
    response to a click; there's no way to precompute a user's own
    judgment call offline. Rewrites the whole (small, ~9K row)
    exercise_review.parquet -- fine at this account's scale, not meant to
    hold up under concurrent writers.
    """
    import traceback

    data = request.get_json(silent=True) or {}
    source = data.get('source', DEFAULT_SOURCE)
    if source not in {'local', 's3'}:
        source = DEFAULT_SOURCE
    decision = data.get('decision')
    if decision not in {'accept', 'reject'}:
        return jsonify({'error': "decision must be 'accept' or 'reject'"}), 400
    exercise = data.get('exercise')

    try:
        activity_id = str(data.get('activity_id'))
        set_number = int(data.get('set_number'))
    except (TypeError, ValueError):
        return jsonify({'error': 'activity_id and set_number are required'}), 400

    try:
        store = curated_s3 if source == 's3' else curated_local
        review = store.load_exercise_review('strength')
        if review.empty:
            return jsonify({'error': 'no review data found'}), 404

        mask = (review['activity_id'].astype(str) == activity_id) & (review['set_number'] == set_number)
        if not mask.any():
            return jsonify({'error': 'set not found'}), 404

        if decision == 'accept':
            if exercise:
                review.loc[mask, 'our_guess_exercise'] = exercise
            review.loc[mask, 'review_status'] = 'accepted'
        else:
            review.loc[mask, 'review_status'] = 'rejected'
        store.write_exercise_review('strength', review)
        return jsonify({'status': 'ok'})
    except Exception as e:
        print(f"Error saving exercise review decision: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@bp.route('/api/exercise_review_submit_activity', methods=['POST'])
def api_exercise_review_submit_activity():
    """Batch write: approves every listed set in one activity in a single
    parquet rewrite (one row per edited/confirmed set from the per-
    activity review screen's 'Submit as Approved' button), instead of one
    request per set. Only touches sets that are still 'pending' -- a set
    a user already explicitly accepted/rejected via the single-decision
    endpoint is left alone even if it's included in the batch (defensive:
    the frontend shouldn't include already-decided rows, but a stale page
    load could).
    """
    import traceback

    data = request.get_json(silent=True) or {}
    source = data.get('source', DEFAULT_SOURCE)
    if source not in {'local', 's3'}:
        source = DEFAULT_SOURCE
    activity_id = str(data.get('activity_id') or '')
    items = data.get('items')
    if not activity_id or not isinstance(items, list) or not items:
        return jsonify({'error': 'activity_id and a non-empty items list are required'}), 400

    try:
        store = curated_s3 if source == 's3' else curated_local
        review = store.load_exercise_review('strength')
        if review.empty:
            return jsonify({'error': 'no review data found'}), 404

        updated = 0
        for item in items:
            try:
                set_number = int(item.get('set_number'))
            except (TypeError, ValueError, AttributeError):
                continue
            exercise = item.get('exercise') if isinstance(item, dict) else None
            if not exercise:
                continue
            mask = (
                (review['activity_id'].astype(str) == activity_id)
                & (review['set_number'] == set_number)
                & (review['review_status'] == 'pending')
            )
            if not mask.any():
                continue
            review.loc[mask, 'our_guess_exercise'] = exercise
            review.loc[mask, 'review_status'] = 'accepted'
            updated += mask.sum()

        if updated:
            store.write_exercise_review('strength', review)
        return jsonify({'status': 'ok', 'updated': int(updated)})
    except Exception as e:
        print(f"Error submitting exercise review activity: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@bp.route('/muscle_explorer')
def muscle_explorer():
    """Serves the standalone lifting/climbing/muscle-map prototype (see
    garmin.prototypes.activity_explorer) as a real page instead of a
    manually-regenerated local file. Prefers the precomputed viewer-cache
    HTML (built by manual_build_viewer_cache.py) -- live-building this
    against S3 means garmin.io.curated_store.load_all_activity_details
    doing one S3 GET per strength-session detail file (490+ as of
    2026-08), so an uncached hit would be very slow.
    """
    source = request.args.get('source', DEFAULT_SOURCE)
    if source not in {'local', 's3'}:
        source = DEFAULT_SOURCE
    store = curated_s3 if source == 's3' else curated_local
    html = _cached_html_or_live(f'activity_explorer_{source}', source, lambda: build_activity_explorer_html(store))
    return Response(html, mimetype='text/html')


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