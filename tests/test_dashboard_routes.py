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


def test_quick_dashboard_data_returns_analyzed_payload(monkeypatch) -> None:
    app = create_app()

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


def test_activities_overview_data_uses_real_payload_when_available(monkeypatch) -> None:
    app = create_app()
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


def test_activities_overview_data_falls_back_to_mock_when_no_real_data(monkeypatch) -> None:
    app = create_app()
    monkeypatch.setattr(routes_module, '_activities_real_payload', lambda source='local': None)

    with app.test_client() as client:
        response = client.get('/api/activities_overview_data?source=local')

    payload = response.get_json()
    assert payload['mock'] is True
    assert 'kpis' in payload


def test_activities_real_payload_returns_none_when_no_datasets_have_data(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(routes_module.fm_local, 'local_dir', str(tmp_path))

    assert routes_module._activities_real_payload(source='local') is None


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