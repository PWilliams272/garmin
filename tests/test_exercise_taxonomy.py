from __future__ import annotations

import pandas as pd

from garmin.analysis.exercise_taxonomy import (
    SOURCE_INFERRED,
    SOURCE_LABELLED,
    SOURCE_UNKNOWN,
    family_for,
    infer_variants,
)


def test_family_for_groups_implements_of_the_same_movement() -> None:
    for variant in ("DUMBBELL_BICEPS_CURL", "BARBELL_BICEPS_CURL", "STANDING_EZ_BAR_BICEPS_CURL"):
        assert family_for(variant, "curl") == "biceps_curl"
    # Hammer curl is deliberately its own family -- different enough to track
    # separately even though Garmin files it under `curl`.
    assert family_for("DUMBBELL_HAMMER_CURL", "curl") == "hammer_curl"


def test_family_for_rescues_variants_filed_under_the_wrong_category() -> None:
    """Garmin's categories are unreliable: lat pulldowns arrive under
    `pull_up`, face pulls under `row`, leg extensions under `crunch`."""
    assert family_for("KNEELING_LAT_PULLDOWN", "pull_up") == "lat_pulldown"
    assert family_for("FACE_PULL", "row") == "face_pull"
    assert family_for("WEIGHTED_LEG_EXTENSIONS", "crunch") == "leg_extension"


def test_family_for_falls_back_to_the_category_default() -> None:
    assert family_for(None, "bench_press") == "flat_bench_press"
    assert family_for(None, "some_new_thing") == "some_new_thing"


def _sets(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def test_infer_variants_does_not_assign_an_implausible_only_option() -> None:
    """Regression: with one labelled variant in a family, "only option" was
    treated as "right option" and 1620 barbell bench sets at 135 lb were
    assigned to the 55 lb DUMBBELL_BENCH_PRESS profile. The true variant may
    simply never have been labelled, so plausibility is still required."""
    rows = [
        {"activity_id": f"lab{i}", "exercise": "bench_press",
         "exercise_name": "DUMBBELL_BENCH_PRESS", "weight_lb": 55.0}
        for i in range(10)
    ] + [
        {"activity_id": f"unl{i}", "exercise": "bench_press",
         "exercise_name": None, "weight_lb": 135.0}
        for i in range(10)
    ]

    out = infer_variants(_sets(rows))

    heavy = out[out["weight_lb"] == 135.0]
    assert (heavy["variant_source"] == SOURCE_UNKNOWN).all()
    assert (heavy["variant"] == "bench_press__unassigned").all()


def test_infer_variants_propagates_a_label_within_the_same_session() -> None:
    """One labelled set in a session identifies the rest of that session's
    work on the same family -- the strongest evidence available."""
    rows = [
        {"activity_id": "A", "exercise": "curl", "exercise_name": "BARBELL_BICEPS_CURL", "weight_lb": 60.0},
        {"activity_id": "A", "exercise": "curl", "exercise_name": None, "weight_lb": 45.0},
        {"activity_id": "A", "exercise": "curl", "exercise_name": None, "weight_lb": 60.0},
    ] + [
        {"activity_id": f"B{i}", "exercise": "curl",
         "exercise_name": "DUMBBELL_BICEPS_CURL", "weight_lb": 25.0}
        for i in range(10)
    ]

    out = infer_variants(_sets(rows))

    session_a = out[out["activity_id"] == "A"]
    assert set(session_a["variant"]) == {"BARBELL_BICEPS_CURL"}
    # The 45 lb set is a warmup for barbell work, not dumbbell work, even
    # though 45 sits between the two profiles.
    warmup = session_a[session_a["weight_lb"] == 45.0].iloc[0]
    assert warmup["variant_source"] == SOURCE_INFERRED


def test_infer_variants_judges_a_session_by_its_heaviest_set() -> None:
    """Warmups follow the working sets, so an empty-barbell 45 lb set in a
    135 lb session is barbell work -- judging sets individually got this
    wrong for 133 real bench sets."""
    rows = [
        {"activity_id": f"db{i}", "exercise": "curl",
         "exercise_name": "DUMBBELL_BICEPS_CURL", "weight_lb": 25.0} for i in range(10)
    ] + [
        {"activity_id": f"bb{i}", "exercise": "curl",
         "exercise_name": "BARBELL_BICEPS_CURL", "weight_lb": 60.0} for i in range(10)
    ] + [
        # Unlabelled session: light warmup plus a clearly-barbell top set.
        {"activity_id": "X", "exercise": "curl", "exercise_name": None, "weight_lb": 25.0},
        {"activity_id": "X", "exercise": "curl", "exercise_name": None, "weight_lb": 60.0},
    ]

    out = infer_variants(_sets(rows))

    session_x = out[out["activity_id"] == "X"]
    assert set(session_x["variant"]) == {"BARBELL_BICEPS_CURL"}
    assert set(session_x["variant_source"]) == {SOURCE_INFERRED}


def test_infer_variants_leaves_ambiguous_weights_unassigned() -> None:
    """A weight sitting midway between two variants is not evidence for
    either, so it stays unknown rather than being forced."""
    rows = [
        {"activity_id": f"db{i}", "exercise": "curl",
         "exercise_name": "DUMBBELL_BICEPS_CURL", "weight_lb": 25.0 + (i % 3)} for i in range(10)
    ] + [
        {"activity_id": f"bb{i}", "exercise": "curl",
         "exercise_name": "BARBELL_BICEPS_CURL", "weight_lb": 60.0 + (i % 3)} for i in range(10)
    ] + [
        {"activity_id": "MID", "exercise": "curl", "exercise_name": None, "weight_lb": 42.0},
    ]

    out = infer_variants(_sets(rows))

    mid = out[out["activity_id"] == "MID"].iloc[0]
    assert mid["variant_source"] == SOURCE_UNKNOWN


def test_infer_variants_marks_provenance_for_every_row() -> None:
    rows = [
        {"activity_id": "A", "exercise": "curl", "exercise_name": "DUMBBELL_BICEPS_CURL", "weight_lb": 25.0},
        {"activity_id": "B", "exercise": "curl", "exercise_name": None, "weight_lb": 25.0},
    ]

    out = infer_variants(_sets(rows))

    assert out.loc[out["activity_id"] == "A", "variant_source"].iloc[0] == SOURCE_LABELLED
    assert set(out["variant_source"]) <= {SOURCE_LABELLED, SOURCE_INFERRED, SOURCE_UNKNOWN}
    assert out["variant"].notna().all()
    assert out["family"].notna().all()
