"""Helpers for resolving stable repository-relative paths."""

from __future__ import annotations

from pathlib import Path


def get_repo_root() -> Path:
    """Return the repository root by walking upward to `pyproject.toml`."""

    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    raise RuntimeError("Unable to locate repository root from package paths.")


def get_data_dir() -> Path:
    """Return the repository-level data directory."""

    return get_repo_root() / "data"