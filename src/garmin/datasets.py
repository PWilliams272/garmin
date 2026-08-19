"""Dataset name constants, deliberately free of any dependency.

These live here rather than in `updaters.py` because the analysis and web
layers need the *names* without needing the machinery that populates them.
Importing them from `updaters` pulls in the Garmin API pullers and, through
them, `fitparse` -- which took the analyzer Lambda down for two weeks in
2026-08 with `No module named 'fitparse'`, in a job that never opens a FIT
file. Keep this module import-free so that can't recur.
"""

from __future__ import annotations

__all__ = ["ACTIVITY_DATASETS", "ACTIVITY_TYPE_KEYS", "activity_type_key"]

#: Activity types pulled from Garmin and curated into per-sport datasets.
#:
#: Verified 2026-08-19 against a full ``pull_activity_list("2010-01-01", ...)``
#: -- 3838 activities -- so this list is the set Garmin actually reports, not
#: a guess. Before that check, 280 activities across 15 types (267 hours) were
#: being pulled from the activity list and then silently discarded because no
#: dataset claimed them.
ACTIVITY_DATASETS = [
    # Verified present in the account's history.
    "running", "strength", "cycling", "indoor_cycling", "hiking",
    "lap_swimming", "open_water_swimming", "hiit", "bouldering",
    "rock_climbing", "tennis", "pickleball",
    # Added 2026-08-19 -- previously discarded.
    "treadmill_running", "indoor_running", "trail_running",
    "indoor_climbing", "swimming", "walking", "volleyball", "paddling",
    "softball", "skiing", "fitness_equipment", "other",
    "multi_sport", "transition",
]

#: Dataset name -> Garmin ``activityType.typeKey``, for the datasets whose
#: name differs from the key Garmin uses.
#:
#: This mapping exists because the registry used to derive the typeKey from
#: the dataset name. That silently broke ``tennis``: Garmin calls it
#: ``tennis_v2``, so the dataset was registered, queried, and came back empty
#: for years -- indistinguishable from "never played tennis". Garmin has
#: versioned several keys with a ``_v2`` suffix, so this is a category of
#: mismatch rather than a one-off.
ACTIVITY_TYPE_KEYS = {
    "strength": "strength_training",
    "tennis": "tennis_v2",
    "paddling": "paddling_v2",
    "transition": "transition_v2",
    "skiing": "resort_skiing_snowboarding_ws",
}


def activity_type_key(dataset: str) -> str:
    """Garmin's ``typeKey`` for a curated dataset name.

    Args:
        dataset: Curated dataset name, e.g. ``"strength"``.

    Returns:
        The ``activityType.typeKey`` Garmin uses, which is the dataset name
        itself unless :data:`ACTIVITY_TYPE_KEYS` overrides it.
    """
    return ACTIVITY_TYPE_KEYS.get(dataset, dataset)
