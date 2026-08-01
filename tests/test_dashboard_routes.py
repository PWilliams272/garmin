from __future__ import annotations

from pathlib import Path

import pandas as pd

import garmin.app.routes as routes_module
from garmin.app.app import create_app


def test_quick_dashboard_page_renders() -> None:
    app = create_app()

    with app.test_client() as client:
        response = client.get('/quick_dashboard')

    assert response.status_code == 200
    assert b'chart-host' in response.data


def test_quick_dashboard_data_returns_analyzed_payload(tmp_path, monkeypatch) -> None:
    app = create_app()
    # No viewer-cache file in an empty tmp_dir -- _cached_or_live falls
    # through to the monkeypatched builder below rather than reading
    # whatever real cache this machine happens to have on disk.
    monkeypatch.setattr(routes_module.fm_local, 'local_dir', str(tmp_path))

    fake_payload = {
        'analyzed': {
            'resting_hr': {
                'points': [
                    {'date': '2024-01-01', 'resting_hr': 50.0, 'quality_tier': 'normal', 'quality_weight': 1.0},
                    {'date': '2024-01-02', 'resting_hr': 49.0, 'quality_tier': 'normal', 'quality_weight': 1.0},
                ],
                'trend': [{'date': '2024-01-01', 'mean': 49.5, 'lower_68': 48.0, 'upper_68': 51.0, 'lower_95': 46.0, 'upper_95': 53.0}],
            },
            'total_steps': {'points': [], 'trend': []},
            'weight': {'points': [], 'trend': []},
            'body_fat': {'points': [], 'trend': []},
            'bone_mass': {'points': [], 'trend': []},
            'muscle_mass': {'points': [], 'trend': []},
        },
    }
    monkeypatch.setattr(routes_module, '_health_analyzed_payload', lambda source='local': fake_payload)

    with app.test_client() as client:
        response = client.get('/api/quick_dashboard_data?source=local')

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['source'] == 'local'
    assert payload['analyzed']['resting_hr']['points'][0]['date'] == '2024-01-01'
    assert payload['analyzed']['resting_hr']['points'][0]['resting_hr'] == 50.0
    assert payload['analyzed']['resting_hr']['trend'][0]['mean'] == 49.5


def test_curated_metrics_dashboard_page_renders_from_cached_artifacts(tmp_path, monkeypatch) -> None:
    app = create_app()
    monkeypatch.setattr(routes_module.fm_local, 'local_dir', str(tmp_path))

    artifact_dir = tmp_path / 'dashboards' / 'curated_metric_timeseries' / 'local'
    artifact_dir.mkdir(parents=True)
    for filename, _ in routes_module.CURATED_DASHBOARD_FILES:
        (artifact_dir / filename).write_text('<div>cached artifact</div>', encoding='utf-8')

    with app.test_client() as client:
        response = client.get('/curated_metrics_dashboard?source=local')

    assert response.status_code == 200
    assert b'Curated Metrics Dashboard' in response.data
    assert b'cached artifact' in response.data


def test_curated_metrics_dashboard_refresh_builds_artifacts(tmp_path, monkeypatch) -> None:
    app = create_app()
    monkeypatch.setattr(routes_module.fm_local, 'local_dir', str(tmp_path))

    def write_artifacts(source: str) -> Path:
        artifact_dir = tmp_path / 'dashboards' / 'curated_metric_timeseries' / source
        artifact_dir.mkdir(parents=True, exist_ok=True)
        for filename, _ in routes_module.CURATED_DASHBOARD_FILES:
            (artifact_dir / filename).write_text(f'<div>{source} artifact</div>', encoding='utf-8')
        return artifact_dir

    def fake_build(source: str = 'local') -> str:
        write_artifacts(source)
        return str(tmp_path / 'dashboards' / 'curated_metric_timeseries' / source)

    def fake_cache(source: str = 's3') -> Path:
        return write_artifacts(source)

    monkeypatch.setattr(routes_module, 'build_curated_dashboard_artifacts', fake_build)
    monkeypatch.setattr(routes_module, 'cache_curated_dashboard_artifacts_locally', fake_cache)

    with app.test_client() as client:
        response = client.get('/curated_metrics_dashboard?source=s3&refresh=1')

    assert response.status_code == 200
    assert b's3 artifact' in response.data


def test_activities_overview_from_df_aggregates_by_type_and_week() -> None:
    df = pd.DataFrame([
        {'date': pd.Timestamp('2024-01-01'), 'type': 'running', 'duration_min': 30.0, 'distance_mi': 3.0},
        {'date': pd.Timestamp('2024-01-02'), 'type': 'strength', 'duration_min': 45.0, 'distance_mi': None},
        {'date': pd.Timestamp('2024-01-08'), 'type': 'running', 'duration_min': 40.0, 'distance_mi': 4.0},
    ])

    payload = routes_module._activities_overview_from_df(df)

    assert payload['activity_types'] == ['running', 'strength']
    assert payload['kpis']['total_activities'] == 3
    assert payload['kpis']['total_distance_mi'] == 7.0
    assert len(payload['recent_activities']) == 3


def test_activities_overview_data_uses_real_payload_when_available(tmp_path, monkeypatch) -> None:
    app = create_app()
    monkeypatch.setattr(routes_module.fm_local, 'local_dir', str(tmp_path))
    fake_payload = {
        'activity_types': ['running'],
        'weekly_by_type': [],
        'weekly_totals': [],
        'recent_activities': [],
        'kpis': {'total_activities': 5, 'total_distance_mi': 10.0, 'total_hours': 2.0},
    }
    monkeypatch.setattr(routes_module, '_activities_real_payload', lambda source='local': fake_payload)

    with app.test_client() as client:
        response = client.get('/api/activities_overview_data?source=local')

    payload = response.get_json()
    assert payload['mock'] is False
    assert payload['kpis']['total_activities'] == 5


def test_activities_overview_data_falls_back_to_mock_when_no_real_data(tmp_path, monkeypatch) -> None:
    app = create_app()
    monkeypatch.setattr(routes_module.fm_local, 'local_dir', str(tmp_path))
    monkeypatch.setattr(routes_module, '_activities_real_payload', lambda source='local': None)

    with app.test_client() as client:
        response = client.get('/api/activities_overview_data?source=local')

    payload = response.get_json()
    assert payload['mock'] is True
    assert 'kpis' in payload


def test_activities_real_payload_returns_none_when_no_datasets_have_data(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(routes_module.fm_local, 'local_dir', str(tmp_path))

    assert routes_module._activities_real_payload(source='local') is None


def test_activities_list_payload_includes_avg_hr_and_all_activities(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(routes_module.fm_local, 'local_dir', str(tmp_path))
    store = routes_module.curated_local
    store.merge_activity_summary('running', pd.DataFrame([
        {'activity_id': '1', 'date': '2024-01-01', 'name': 'Run A', 'duration_min': 30.0, 'distance_mi': 3.0, 'avg_hr': 140.0},
        {'activity_id': '2', 'date': '2024-01-08', 'name': 'Run B', 'duration_min': 45.0, 'distance_mi': 5.0, 'avg_hr': 150.0},
    ]))
    store.merge_activity_summary('strength', pd.DataFrame([
        {'activity_id': '3', 'date': '2024-01-05', 'name': 'Lift', 'duration_min': 60.0, 'avg_hr': 110.0},
    ]))

    payload = routes_module._activities_list_payload(source='local')

    assert payload is not None
    assert set(payload['activity_types']) == {'running', 'strength'}
    assert len(payload['activities']) == 3
    # Sorted newest-first by date.
    assert [a['activity_id'] for a in payload['activities']] == ['2', '3', '1']
    run_b = payload['activities'][0]
    assert run_b['avg_hr'] == 150.0
    lift = payload['activities'][1]
    assert lift['distance_mi'] is None


def test_activities_list_payload_returns_none_when_no_data(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(routes_module.fm_local, 'local_dir', str(tmp_path))

    assert routes_module._activities_list_payload(source='local') is None


def test_activities_list_data_route_falls_back_to_mock(monkeypatch) -> None:
    app = create_app()
    monkeypatch.setattr(routes_module, '_activities_list_payload', lambda source='local': None)

    with app.test_client() as client:
        response = client.get('/api/activities_list_data?source=local')

    payload = response.get_json()
    assert payload['mock'] is True
    assert len(payload['activities']) > 0
    assert 'avg_hr' in payload['activities'][0]


def test_lifting_real_payload_reads_precomputed_analyzed_data(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(routes_module.fm_local, 'local_dir', str(tmp_path))
    store = routes_module.curated_local

    store.merge_activity_summary('strength', pd.DataFrame([
        {'activity_id': '1', 'date': '2024-01-01', 'duration_min': 45.0},
    ]))
    store.write_activity_detail('strength', '1', pd.DataFrame([
        {'exercise': 'bench_press', 'reps': 8, 'weight_lb': 135.0, 'activity_id': '1'},
    ]))
    store.write_analyzed_points('strength', 'bench_press_1rm', pd.DataFrame([
        {'date': '2024-01-01', 'est_1rm': 171.0, 'quality_tier': 'normal', 'quality_weight': 1.0},
    ]))
    store.write_analyzed_trend('strength', 'bench_press_1rm', pd.DataFrame([
        {'date': '2024-01-01', 'mean': 170.0, 'lower_68': 160.0, 'upper_68': 180.0, 'lower_95': 150.0, 'upper_95': 190.0, 'day_to_day_std': 5.0},
    ]), kind='sts')

    payload = routes_module._lifting_real_payload(source='local')

    assert payload is not None
    assert payload['exercise_order'] == ['bench_press']
    assert payload['exercises']['bench_press']['sessions'] == 1
    assert payload['exercises']['bench_press']['trend'][0]['mean'] == 170.0


def test_lifting_real_payload_returns_none_without_analyzed_data(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(routes_module.fm_local, 'local_dir', str(tmp_path))
    store = routes_module.curated_local
    store.merge_activity_summary('strength', pd.DataFrame([
        {'activity_id': '1', 'date': '2024-01-01', 'duration_min': 45.0},
    ]))
    store.write_activity_detail('strength', '1', pd.DataFrame([
        {'exercise': 'bench_press', 'reps': 8, 'weight_lb': 135.0, 'activity_id': '1'},
    ]))

    assert routes_module._lifting_real_payload(source='local') is None


def test_data_status_payload_aggregates_weekly_by_majority_status(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(routes_module.fm_local, 'local_dir', str(tmp_path))
    store = routes_module.curated_local

    # 2024-01-09 through 2024-01-15 all fall in the same W-MON-anchored week
    # bucket (labeled by its start, 2024-01-09): 5 fetched, 2 no_data -> majority fetched.
    week_rows = [
        {'query_date': f'2024-01-{day:02d}', 'date_pulled': '2024-01-15', 'pull_status': status}
        for day, status in zip(range(9, 16), ['fetched'] * 5 + ['no_data'] * 2)
    ]
    store.merge_detailed_status('heart_rate_detailed', pd.DataFrame(week_rows))
    # A daily dataset's weekly status is a majority vote over its 7 individual
    # days too -- give it a row for every day in the week, not just one, or
    # the other 6 untouched days would (correctly) outvote it.
    store.merge_daily('steps', pd.DataFrame([
        {'date': f'2024-01-{day:02d}', 'total_steps': 9000} for day in range(9, 16)
    ]))

    payload = routes_module._data_status_payload(source='local')

    assert payload['dates'][0] == '2015-12-01'  # DATA_STATUS_START_DATE's own week label

    by_name = {d['name']: d for d in payload['datasets']}
    idx = {d: i for i, d in enumerate(payload['dates'])}
    codes = payload['status_codes']

    hr = by_name['heart_rate_detailed']
    assert hr['statuses'][idx['2024-01-09']] == codes['fetched']
    # Weeks entirely before the dataset's own first tracked date are untouched.
    assert hr['statuses'][idx['2015-12-01']] == codes['untouched']

    steps = by_name['steps']
    assert steps['statuses'][idx['2024-01-09']] == codes['fetched']
    # A week well after steps' only recorded date, but still in the shared
    # range (extended by heart_rate_detailed's later weeks), reads untouched.
    later_week = payload['dates'][-1]
    if later_week != '2024-01-09':
        assert steps['statuses'][idx[later_week]] == codes['untouched']


def test_cached_or_live_prefers_cache_over_build_fn(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(routes_module.fm_local, 'local_dir', str(tmp_path))
    routes_module.curated_local.write_viewer_cache('quick_dashboard_local', {'from': 'cache'})

    build_fn_calls = []

    def build_fn():
        build_fn_calls.append(1)
        return {'from': 'live'}

    result = routes_module._cached_or_live('quick_dashboard_local', 'local', build_fn)

    assert result == {'from': 'cache'}
    assert build_fn_calls == []


def test_cached_or_live_falls_back_when_no_cache(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(routes_module.fm_local, 'local_dir', str(tmp_path))

    result = routes_module._cached_or_live('quick_dashboard_local', 'local', lambda: {'from': 'live'})

    assert result == {'from': 'live'}


def test_data_status_payload_empty_when_nothing_pulled(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(routes_module.fm_local, 'local_dir', str(tmp_path))

    payload = routes_module._data_status_payload(source='local')

    assert payload['datasets'] == []
    assert payload['dates'] == []


def _seed_running_timeseries(store, activity_id: str, date: str) -> None:
    store.merge_activity_summary('running', pd.DataFrame([
        {'activity_id': activity_id, 'date': date, 'name': 'Test Run', 'duration_min': 20.0, 'distance_mi': 2.5},
    ]))
    store.write_activity_detail('running_timeseries', activity_id, pd.DataFrame([
        {'timestamp': f'{date}T08:00:00', 'lat': 40.0, 'lon': -105.0, 'elevation_ft': 100.0,
         'distance_mi': 0.0, 'speed_mph': 0.0, 'cadence': 90.0, 'heart_rate_bpm': 100.0, 'power_w': 0.0},
        {'timestamp': f'{date}T08:00:10', 'lat': 40.001, 'lon': -105.001, 'elevation_ft': 101.0,
         'distance_mi': 0.02, 'speed_mph': 7.0, 'cadence': 170.0, 'heart_rate_bpm': 140.0, 'power_w': 250.0},
    ]))


def test_activity_detail_real_payload_reads_precomputed_timeseries(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(routes_module.fm_local, 'local_dir', str(tmp_path))
    store = routes_module.curated_local
    _seed_running_timeseries(store, '111', '2024-01-01')

    payload = routes_module._activity_detail_real_payload(sport='running', source='local')

    assert payload is not None
    assert payload['activity_id'] == '111'
    assert payload['activity']['name'] == 'Test Run'
    assert len(payload['points']) == 2
    assert payload['points'][0]['lat'] == 40.0
    assert sum(payload['zones']['minutes']) > 0


def test_activity_detail_real_payload_picks_most_recent_with_timeseries(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(routes_module.fm_local, 'local_dir', str(tmp_path))
    store = routes_module.curated_local
    _seed_running_timeseries(store, '111', '2024-01-01')
    _seed_running_timeseries(store, '222', '2024-06-01')
    # A third activity with a summary row but no timeseries file shouldn't
    # be selectable.
    store.merge_activity_summary('running', pd.DataFrame([
        {'activity_id': '333', 'date': '2024-12-01', 'name': 'No Detail', 'duration_min': 20.0, 'distance_mi': 2.5},
    ]))

    payload = routes_module._activity_detail_real_payload(sport='running', source='local')

    assert payload['activity_id'] == '222'


def test_activity_detail_real_payload_returns_none_without_data(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(routes_module.fm_local, 'local_dir', str(tmp_path))

    assert routes_module._activity_detail_real_payload(sport='running', source='local') is None


def test_activity_detail_data_route_falls_back_to_mock(monkeypatch) -> None:
    app = create_app()
    monkeypatch.setattr(routes_module, '_activity_detail_real_payload', lambda sport='running', activity_id=None, source='local': None)

    with app.test_client() as client:
        response = client.get('/api/activity_detail_data?sport=running')

    payload = response.get_json()
    assert payload['mock'] is True
    assert len(payload['points']) > 0

def test_reject_activity_detail_outliers_nulls_a_glitch_but_keeps_stable_readings() -> None:
    # A heart rate wobbling naturally around 140 (+/- a couple bpm -- MAD-
    # based detection needs some real spread to work with; a perfectly flat
    # baseline makes the median absolute deviation exactly 0, which the
    # classifier treats as "can't judge" rather than "everything's an
    # outlier") with one implausible one-sample spike to 250 in the middle --
    # a classic sensor glitch. Speed stays clean throughout and shouldn't be
    # touched.
    n = 20
    jitter = [0, 1, -1, 2, -2, 1, 0, -1, 1, 0, 0, -1, 1, 0, -2, 2, 1, -1, 0, 1]
    hr = [140.0 + j for j in jitter]
    hr[10] = 250.0
    detail = pd.DataFrame({
        'timestamp': pd.date_range('2024-01-01', periods=n, freq='s'),
        'speed_mph': [7.0] * n,
        'heart_rate_bpm': hr,
        'cadence': [170.0] * n,
        'power_w': [200.0] * n,
    })

    cleaned = routes_module._reject_activity_detail_outliers(detail)

    assert pd.isna(cleaned.loc[10, 'heart_rate_bpm'])
    assert cleaned['heart_rate_bpm'].dropna().between(137.0, 143.0).all()
    assert cleaned['speed_mph'].notna().all()
    assert cleaned['speed_mph'].eq(7.0).all()


def test_reject_activity_detail_outliers_skips_short_series() -> None:
    detail = pd.DataFrame({
        'timestamp': pd.date_range('2024-01-01', periods=3, freq='s'),
        'heart_rate_bpm': [100.0, 999.0, 100.0],
    })

    cleaned = routes_module._reject_activity_detail_outliers(detail)

    # Fewer than 5 valid points -- classify_metric is skipped, nothing nulled.
    assert cleaned['heart_rate_bpm'].notna().all()
