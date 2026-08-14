"""Guards the analyzer Lambda's import surface.

In 2026-08 the analyzer Lambda failed every night for two weeks with
`No module named 'fitparse'`. Nothing in the analyzer parses FIT files -- the
dependency arrived through a module-level `from garmin.updaters import
ACTIVITY_DATASETS`, a twelve-string constant, which transitively imported the
Garmin activity puller.

The failure was invisible from inside the repo: the local venv has fitparse, so
every test and every manual run passed. Only the container lacked it. Each
import here therefore runs in a **subprocess** with the package hidden -- doing
it in-process would mean tearing modules out of `sys.modules`, which scipy's
C extensions do not survive, and would test the teardown rather than the
import graph.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

#: Packages the analyzer image deliberately does not ship. Importing the
#: analyzer entrypoint must not need any of them.
_ABSENT_IN_CONTAINER = ("fitparse",)

#: Entrypoints that run inside the analyzer container.
_ANALYZER_MODULES = (
    "garmin.scripts.lambda_analyze",
    "garmin.scripts.manual_build_viewer_cache",
    "garmin.analysis.model_report",
    "garmin.analysis.daily_panel",
    "garmin.analysis.analysis_pipeline",
)

_SCRIPT = textwrap.dedent(
    """
    import sys

    BLOCKED = {blocked!r}

    class Blocker:
        def find_module(self, name, path=None):
            return self if name.split(".")[0] in BLOCKED else None

        def load_module(self, name):
            raise ImportError("No module named %r" % name)

    sys.meta_path.insert(0, Blocker())
    import {module}
    for name in BLOCKED:
        assert name not in sys.modules, name + " was imported after all"
    print("ok")
    """
)


def _import_without(module: str, blocked: tuple[str, ...]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _SCRIPT.format(module=module, blocked=blocked)],
        capture_output=True,
        text=True,
        timeout=180,
    )


@pytest.mark.parametrize("module", _ANALYZER_MODULES)
def test_analyzer_modules_import_without_puller_only_dependencies(module):
    """The exact failure that took the Lambda down for two weeks."""
    result = _import_without(module, _ABSENT_IN_CONTAINER)
    assert result.returncode == 0, (
        f"{module} needs a package the analyzer image does not ship.\n"
        f"This is the 2026-08 outage shape -- check what pulled it in.\n"
        f"{result.stderr}"
    )


def test_activity_datasets_is_importable_with_nothing_installed():
    """garmin.datasets exists precisely to be safe to import from anywhere.
    If it grows a dependency, the decoupling is undone."""
    result = _import_without("garmin.datasets", ("fitparse", "pandas", "numpy", "boto3"))
    assert result.returncode == 0, result.stderr


def test_updaters_still_re_exports_activity_datasets():
    """Existing callers import it from garmin.updaters; that must keep working."""
    from garmin.datasets import ACTIVITY_DATASETS as canonical
    from garmin.updaters import ACTIVITY_DATASETS as re_exported

    assert re_exported is canonical
    assert "running" in canonical
