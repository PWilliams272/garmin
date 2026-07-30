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


def test_quick_dashboard_data_returns_combined_rows(monkeypatch) -> None:
    app = create_app()

    monkeypatch.setattr(
        routes_module,
        '_load_dashboard_timeseries',
        lambda source='local': pd.DataFrame(
            [
                {
                    'date': pd.Timestamp('2024-01-01'),
                    'resting_hr': 50.0,
                    'total_steps': 9000.0,
                    'weight': 180.5,
                },
                {
                    'date': pd.Timestamp('2024-01-02'),
                    'resting_hr': 49.0,
                    'total_steps': None,
                    'weight': 180.0,
                },
            ]
        ),
    )

    with app.test_client() as client:
        response = client.get('/api/quick_dashboard_data?source=local')

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['source'] == 'local'
    assert payload['rows'][0]['date'] == '2024-01-01'
    assert payload['rows'][0]['resting_hr'] == 50.0
    assert payload['rows'][0]['total_steps'] == 9000.0
    assert payload['rows'][1]['total_steps'] is None


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