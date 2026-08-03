from __future__ import annotations

from datetime import date

import pandas as pd

from garmin.io.curated_store import CuratedDataStore
from garmin.io.file_manager import FileManager
from garmin.prototypes.activity_explorer import build_activity_explorer_payload
from garmin.prototypes.activity_explorer import blended_1rm
from garmin.prototypes.activity_explorer import build_activity_explorer_html
from garmin.prototypes.activity_explorer import session_strength_estimate


def test_blended_1rm_downweights_high_rep_epley_spike() -> None:
    legacy_epley = 100.0 * (1 + 15 / 30)
    improved = blended_1rm(100.0, 15.0)

    assert improved < legacy_epley
    assert improved > 110.0


def test_session_strength_estimate_uses_multiple_top_sets() -> None:
    sets = pd.DataFrame([
        {"exercise": "bench_press", "reps": 5, "weight_lb": 185.0},
        {"exercise": "bench_press", "reps": 8, "weight_lb": 165.0},
        {"exercise": "bench_press", "reps": 10, "weight_lb": 155.0},
    ])

    result = session_strength_estimate(sets)

    assert result["improved_1rm"] < result["legacy_epley_1rm"]
    assert round(result["volume_lb"], 1) == 3795.0
    assert result["working_sets"] == 3.0


def test_activity_explorer_payload_includes_strength_and_climbing(tmp_path) -> None:
    store = CuratedDataStore(file_manager=FileManager(environment="local", local_dir=str(tmp_path)))

    store.merge_activity_summary("strength", pd.DataFrame([
        {"activity_id": "s1", "date": date(2024, 1, 3), "duration_min": 55.0, "name": "Strength Training"},
    ]))
    store.write_activity_detail("strength", "s1", pd.DataFrame([
        {"activity_id": "s1", "exercise": "bench_press", "reps": 5, "weight_lb": 185.0},
        {"activity_id": "s1", "exercise": "bench_press", "reps": 8, "weight_lb": 165.0},
    ]))
    store.merge_activity_summary("bouldering", pd.DataFrame([
        {"activity_id": "c1", "date": date(2024, 1, 4), "duration_min": 90.0, "calories": 700.0, "name": "Bouldering"},
    ]))

    payload = build_activity_explorer_payload(store)

    assert payload["strength"]["available"] is True
    assert payload["climbing"]["available"] is True
    assert payload["strength"]["exercise_order"] == ["bench_press"]
    assert payload["strength"]["sessions"][0]["exercise_label"] == "Bench Press"
    assert "chest" in payload["strength"]["exercise_muscles"]["bench_press"]


def test_activity_explorer_html_renders_sections(tmp_path) -> None:
    store = CuratedDataStore(file_manager=FileManager(environment="local", local_dir=str(tmp_path)))
    store.merge_activity_summary("strength", pd.DataFrame([
        {"activity_id": "s1", "date": date(2024, 1, 3), "duration_min": 55.0, "name": "Strength Training"},
    ]))
    store.write_activity_detail("strength", "s1", pd.DataFrame([
        {"activity_id": "s1", "exercise": "bench_press", "reps": 5, "weight_lb": 185.0},
    ]))

    html = build_activity_explorer_html(store)

    assert "Garmin Activity Explorer Sketch" in html
    assert "Targeted Muscle Sketch" in html
    assert "Climbing Explorer" in html