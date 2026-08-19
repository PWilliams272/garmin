from __future__ import annotations

import pytest

import garmin.app.routes as routes_module
import garmin.scripts.manual_build_viewer_cache as build_viewer_cache_module
from garmin.io.curated_store import CuratedDataStore
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


def test_build_viewer_cache_continues_after_one_job_fails(tmp_path, monkeypatch) -> None:
    """Regression test: a transient error reading one dataset (seen in
    practice -- a dropped S3 connection partway through activity_explorer's
    490+ individual strength-detail reads) used to kill the whole script,
    leaving every other page's cache stale even though their builders never
    touched the failing dataset."""
    monkeypatch.setattr(routes_module.fm_local, 'local_dir', str(tmp_path))
    monkeypatch.setattr(build_viewer_cache_module, 'FileManager', lambda environment: routes_module.fm_local)

    def boom(source='local'):
        raise RuntimeError('transient S3 blip')

    monkeypatch.setattr(routes_module, '_health_analyzed_payload', lambda source='local': {'ok': True})
    monkeypatch.setattr(routes_module, '_running_real_payload', boom)
    monkeypatch.setattr(routes_module, '_lifting_real_payload', lambda source='local': None)
    monkeypatch.setattr(routes_module, '_activities_real_payload', lambda source='local': {'also_ok': True})
    monkeypatch.setattr(routes_module, '_activities_list_payload', lambda source='local': None)
    monkeypatch.setattr(routes_module, '_data_status_payload', lambda source='local': None)

    with pytest.raises(RuntimeError, match='fitness_running_local'):
        build_viewer_cache('local')

    store = routes_module.curated_local
    # Jobs before *and after* the failing one still ran and cached normally.
    assert store.load_viewer_cache('quick_dashboard_local') == {'ok': True}
    assert store.load_viewer_cache('activities_overview_local') == {'also_ok': True}
    assert store.load_viewer_cache('fitness_running_local') is None


def test_build_viewer_cache_continues_after_a_write_fails(tmp_path, monkeypatch) -> None:
    """First fix attempt only wrapped the build step, not the write --
    a transient error while writing the cache blob itself (also seen live,
    a dropped connection mid-PUT to viewer_cache/fitness_running_s3.json)
    still killed the script. Both steps need covering."""
    monkeypatch.setattr(routes_module.fm_local, 'local_dir', str(tmp_path))
    monkeypatch.setattr(build_viewer_cache_module, 'FileManager', lambda environment: routes_module.fm_local)

    # build_viewer_cache() constructs its own CuratedDataStore instance
    # (not routes_module.curated_local), so the patch has to be on the
    # class -- patching the instance method on curated_local wouldn't
    # affect the object the function under test actually uses.
    original_write = CuratedDataStore.write_viewer_cache

    def flaky_write(self, cache_name, payload):
        if cache_name == 'fitness_running_local':
            raise RuntimeError('transient S3 write blip')
        return original_write(self, cache_name, payload)

    monkeypatch.setattr(CuratedDataStore, 'write_viewer_cache', flaky_write)
    store = routes_module.curated_local
    monkeypatch.setattr(routes_module, '_health_analyzed_payload', lambda source='local': {'ok': True})
    monkeypatch.setattr(routes_module, '_running_real_payload', lambda source='local': {'never': 'cached'})
    monkeypatch.setattr(routes_module, '_lifting_real_payload', lambda source='local': None)
    monkeypatch.setattr(routes_module, '_activities_real_payload', lambda source='local': {'also_ok': True})
    monkeypatch.setattr(routes_module, '_activities_list_payload', lambda source='local': None)
    monkeypatch.setattr(routes_module, '_data_status_payload', lambda source='local': None)

    with pytest.raises(RuntimeError, match='fitness_running_local'):
        build_viewer_cache('local')

    assert store.load_viewer_cache('quick_dashboard_local') == {'ok': True}
    assert store.load_viewer_cache('activities_overview_local') == {'also_ok': True}
    assert store.load_viewer_cache('fitness_running_local') is None
