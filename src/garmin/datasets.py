"""Dataset name constants, deliberately free of any dependency.

These live here rather than in `updaters.py` because the analysis and web
layers need the *names* without needing the machinery that populates them.
Importing them from `updaters` pulls in the Garmin API pullers and, through
them, `fitparse` -- which took the analyzer Lambda down for two weeks in
2026-08 with `No module named 'fitparse'`, in a job that never opens a FIT
file. Keep this module import-free so that can't recur.
"""

from __future__ import annotations

__all__ = ["ACTIVITY_DATASETS"]

#: Activity types pulled from Garmin and curated into per-sport datasets.
ACTIVITY_DATASETS = [
    "running", "strength", "cycling", "indoor_cycling", "hiking",
    "lap_swimming", "open_water_swimming", "hiit", "bouldering",
    "rock_climbing", "tennis", "pickleball",
]
