"""Helpers for resolving stable repository-relative paths."""

from __future__ import annotations

import os
from pathlib import Path


def get_repo_root() -> Path:
    """Return the repository root by walking upward to `pyproject.toml`."""

    repo_override = os.environ.get("GARMIN_REPO_ROOT")
    if repo_override:
        return Path(repo_override).expanduser().resolve()

    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "pyproject.toml").exists():
            return parent

    lambda_task_root = os.environ.get("LAMBDA_TASK_ROOT")
    if lambda_task_root:
        return Path(lambda_task_root).expanduser().resolve()

    # Packaged installs do not carry the repo marker files. Fall back to the
    # parent directory that contains the installed garmin package.
    if len(current.parents) >= 2:
        return current.parents[1]

    raise RuntimeError("Unable to locate repository root from package paths.")


def get_data_dir() -> Path:
    """Return the repository-level data directory."""

    data_override = os.environ.get("GARMIN_DATA_DIR")
    if data_override:
        return Path(data_override).expanduser().resolve()

    return get_repo_root() / "data"