from __future__ import annotations

import garmin.scripts.lambda_update as lambda_update
import garmin.scripts.manual_update as manual_update


def test_manual_update_uses_curated_store(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class StubSession:
        pass

    class StubDataUpdater:
        def __init__(self, session, db_manager=None, curated_store=None, **kwargs):
            captured['session'] = session
            captured['db_manager'] = db_manager
            captured['curated_store'] = curated_store

        def update_all(self) -> None:
            captured['updated'] = True

    monkeypatch.setattr(manual_update, 'GarminSession', StubSession)
    monkeypatch.setattr(manual_update, 'DataUpdater', StubDataUpdater)

    manual_update.main([])

    assert captured['db_manager'] is None
    assert captured['curated_store'] is not None
    assert captured['updated'] is True


def test_manual_update_can_target_s3(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class StubSession:
        pass

    class StubFileManager:
        def __init__(self, environment=None, **kwargs):
            captured['environment'] = environment

    class StubCuratedStore:
        def __init__(self, file_manager):
            captured['file_manager'] = file_manager

    class StubDataUpdater:
        def __init__(self, session, db_manager=None, curated_store=None, **kwargs):
            captured['curated_store'] = curated_store

        def update_all(self) -> None:
            captured['updated'] = True

    monkeypatch.setattr(manual_update, 'GarminSession', StubSession)
    monkeypatch.setattr(manual_update, 'FileManager', StubFileManager)
    monkeypatch.setattr(manual_update, 'CuratedDataStore', StubCuratedStore)
    monkeypatch.setattr(manual_update, 'DataUpdater', StubDataUpdater)

    manual_update.main(['--storage-target', 's3'])

    assert captured['environment'] == 'aws'
    assert captured['updated'] is True


def test_lambda_update_uses_curated_store(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class StubSession:
        pass

    class StubDataUpdater:
        def __init__(self, session, db_manager=None, curated_store=None, **kwargs):
            captured['session'] = session
            captured['db_manager'] = db_manager
            captured['curated_store'] = curated_store

        def update_all(self) -> None:
            captured['updated'] = True

    monkeypatch.setattr(lambda_update, 'GarminSession', StubSession)
    monkeypatch.setattr(lambda_update, 'DataUpdater', StubDataUpdater)

    result = lambda_update.lambda_handler({}, {})

    assert captured['db_manager'] is None
    assert captured['curated_store'] is not None
    assert captured['updated'] is True
    assert result == {'status': 'success'}