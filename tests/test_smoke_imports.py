from garmin._paths import get_data_dir, get_repo_root
from garmin.api import GarminSession
from garmin.app.app import create_app
from garmin.io.file_manager import FileManager


def test_repo_paths_resolve_from_package() -> None:
    repo_root = get_repo_root()
    assert (repo_root / "pyproject.toml").exists()
    assert get_data_dir() == repo_root / "data"


def test_flask_app_imports() -> None:
    app = create_app()
    assert app is not None


def test_local_defaults_point_at_repo_data_dir() -> None:
    session = GarminSession()
    file_manager = FileManager(environment="local")

    assert session.data_dir == str(get_data_dir())
    assert file_manager.local_dir == str(get_data_dir())