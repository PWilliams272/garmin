from __future__ import annotations

import garmin.app.routes as routes_module
import garmin.scripts.manual_build_viewer_cache as build_viewer_cache_module
from garmin.scripts.manual_build_viewer_cache import build_viewer_cache


def test_build_viewer_cache_writes_only_non_none_payloads(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(routes_module.fm_local, 'local_dir', str(tmp_path))
    monkeypatch.setattr(build_viewer_cache_module, 'FileManager', lambda environment: routes_module.fm_local)

    monkeypatch.setattr(routes_module, '_health_analyzed_payload', lambda source='local': {'analyzed': {'weight': []}})
    monkeypatch.setattr(routes_module, '_running_real_payload', lambda source='local': None)
    monkeypatch.setattr(routes_module, '_lifting_real_payload', lambda source='local': None)
    monkeypatch.setattr(routes_module, '_activities_real_payload', lambda source='local': {'kpis': {'total_activities': 3}})
    monkeypatch.setattr(routes_module, '_data_status_payload', lambda source='local': {'datasets': []})

    build_viewer_cache('local')

    store = routes_module.curated_local
    assert store.load_viewer_cache('quick_dashboard_local') == {'analyzed': {'weight': []}}
    assert store.load_viewer_cache('activities_overview_local') == {'kpis': {'total_activities': 3}}
    assert store.load_viewer_cache('data_status_local') == {'datasets': []}
    # Payloads that returned None (no data yet) are not cached.
    assert store.load_viewer_cache('fitness_running_local') is None
    assert store.load_viewer_cache('fitness_lifting_local') is None
