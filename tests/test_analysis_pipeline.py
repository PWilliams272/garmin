from __future__ import annotations

import pandas as pd
import pytest

from garmin.analysis.analysis_pipeline import (
    CARDIO_METRICS,
    LOAD_TYPE_ASSISTED,
    LOAD_TYPE_BODYWEIGHT,
    LOAD_TYPE_BW_PLUS,
    LOAD_TYPE_EXTERNAL,
    LOAD_TYPE_MIXED,
    REVIEW_STATUS_ACCEPTED,
    REVIEW_STATUS_GARMIN_CONFIRMED,
    REVIEW_STATUS_PENDING,
    REVIEW_STATUS_REJECTED,
    STRENGTH_MIN_SESSIONS,
    _apply_exercise_review_corrections,
    _assign_set_numbers,
    _session_metrics,
    analyze_cardio_activities,
    analyze_lifting,
    detect_session_stride,
    fit_strength_curves_for_all,
    infer_exercise_corrections,
    load_type_for,
    reject_watch_left_running,
    resolve_variant,
    session_fatigue_curve,
    variant_load_type,
)
from garmin.io.curated_store import CuratedDataStore
from garmin.io.file_manager import FileManager
from garmin.prototypes.activity_explorer import blended_1rm


def _cycling_summary(n: int = 10) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=n, freq="3D")
    return pd.DataFrame({
        "activity_id": [str(i) for i in range(n)],
        "date": dates,
        "distance_mi": [10.0 + i * 0.2 for i in range(n)],
        "duration_min": [45.0] * n,
        "pace_min_per_mile": [6.0 - i * 0.02 for i in range(n)],
        "avg_hr": [140] * n,
    })


def test_analyze_cardio_activities_writes_points_and_trend_for_non_running_sports(tmp_path) -> None:
    store = CuratedDataStore(file_manager=FileManager(environment="local", local_dir=str(tmp_path)))
    store.merge_activity_summary("cycling", _cycling_summary())

    analyze_cardio_activities(store)

    for metric in CARDIO_METRICS:
        points = store.load_analyzed_points("cycling", metric)
        trend = store.load_analyzed_trend("cycling", metric, kind="gp")
        assert not points.empty
        assert not trend.empty


def test_analyze_cardio_activities_skips_running_and_strength(tmp_path) -> None:
    store = CuratedDataStore(file_manager=FileManager(environment="local", local_dir=str(tmp_path)))
    running = _cycling_summary().assign(cadence_spm=170)
    store.merge_activity_summary("running", running)
    store.merge_activity_summary("strength", pd.DataFrame([
        {"activity_id": "1", "date": "2024-01-01", "duration_min": 45.0},
    ]))

    analyze_cardio_activities(store)

    # analyze_running/analyze_lifting own these datasets -- this function
    # should leave them untouched (no distance_mi points written under
    # "running" by the generic cardio path).
    assert store.load_analyzed_points("running", "distance_mi").empty


def test_analyze_cardio_activities_handles_no_data_for_any_sport(tmp_path) -> None:
    store = CuratedDataStore(file_manager=FileManager(environment="local", local_dir=str(tmp_path)))

    analyze_cardio_activities(store)  # should not raise


def _seed_strength_sessions(store: CuratedDataStore, exercise: str, n_sessions: int) -> None:
    dates = pd.date_range("2024-01-01", periods=n_sessions, freq="2D")
    summary_rows = []
    for i, date in enumerate(dates):
        activity_id = str(i)
        summary_rows.append({"activity_id": activity_id, "date": date.date().isoformat(), "duration_min": 45.0})
        # Two sets per session: a heavier low-rep set and a lighter higher-rep
        # set, so top-1RM picks the max across sets, and volume sums both.
        store.write_activity_detail(
            "strength", activity_id,
            pd.DataFrame([
                {"exercise": exercise, "reps": 5, "weight_lb": 135.0 + i, "activity_id": activity_id},
                {"exercise": exercise, "reps": 10, "weight_lb": 95.0, "activity_id": activity_id},
            ]),
        )
    store.merge_activity_summary("strength", pd.DataFrame(summary_rows))


def test_analyze_lifting_writes_1rm_and_volume_for_exercises_over_threshold(tmp_path) -> None:
    store = CuratedDataStore(file_manager=FileManager(environment="local", local_dir=str(tmp_path)))
    _seed_strength_sessions(store, "bench_press", STRENGTH_MIN_SESSIONS + 2)

    analyze_lifting(store)

    points = store.load_analyzed_points("strength", "bench_press__unassigned_1rm")
    trend = store.load_analyzed_trend("strength", "bench_press__unassigned_1rm", kind="sts")
    volume_points = store.load_analyzed_points("strength", "bench_press__unassigned_volume")
    assert not points.empty
    assert not trend.empty
    assert not volume_points.empty

    # Session 0: sets are (5 reps, 135 lb) and (10 reps, 95 lb). The blended
    # estimate (see blended_1rm) for the 5-rep/135lb set beats the 10-rep/
    # 95lb one -- top set (heavier/lower reps) should win. Volume is the
    # sum of both sets: 5*135 + 10*95 = 1625.
    first_session = points.sort_values("date").iloc[0]
    assert first_session["est_1rm"] == pytest.approx(blended_1rm(135.0, 5), abs=1e-9)
    first_volume = volume_points.sort_values("date").iloc[0]
    assert first_volume["volume_lb"] == 5 * 135.0 + 10 * 95.0


def test_analyze_lifting_skips_exercises_under_session_threshold(tmp_path) -> None:
    store = CuratedDataStore(file_manager=FileManager(environment="local", local_dir=str(tmp_path)))
    _seed_strength_sessions(store, "curl", STRENGTH_MIN_SESSIONS - 5)

    analyze_lifting(store)

    assert store.load_analyzed_points("strength", "curl__unassigned_1rm").empty


def test_analyze_lifting_handles_no_strength_data(tmp_path) -> None:
    store = CuratedDataStore(file_manager=FileManager(environment="local", local_dir=str(tmp_path)))

    analyze_lifting(store)  # should not raise


def test_infer_exercise_corrections_passes_through_manually_reviewed_sets(tmp_path) -> None:
    store = CuratedDataStore(file_manager=FileManager(environment="local", local_dir=str(tmp_path)))
    store.merge_activity_summary("strength", pd.DataFrame([
        {"activity_id": "1", "date": "2024-06-01", "duration_min": 30.0},
    ]))
    store.write_activity_detail("strength", "1", pd.DataFrame([
        {"exercise": "bench_press", "weight_lb": 185.0, "reps": 5,
         "manually_reviewed": True, "candidate_1_probability": 100.0, "activity_id": "1"},
    ]))

    infer_exercise_corrections(store)

    review = store.load_exercise_review("strength")
    row = review.iloc[0]
    assert row["review_status"] == REVIEW_STATUS_GARMIN_CONFIRMED
    assert row["our_guess_exercise"] == "bench_press"
    assert row["confidence"] == 1.0


def test_infer_exercise_corrections_prefers_own_guess_over_coincidental_neighbor_weight(tmp_path) -> None:
    """Regression test: a set whose own Garmin guess is a real exercise at
    decent confidence used to get flipped by a single neighboring set that
    only coincidentally shared a similar weight -- self-normalizing the
    neighbor vote against its own max always mapped that neighbor to full
    strength, letting it outvote strong direct evidence about the set
    itself. Both the own-guess signal and the fixed-reference
    normalization fix this."""
    store = CuratedDataStore(file_manager=FileManager(environment="local", local_dir=str(tmp_path)))
    store.merge_activity_summary("strength", pd.DataFrame([
        {"activity_id": "1", "date": "2024-06-01", "duration_min": 30.0},
    ]))
    store.write_activity_detail("strength", "1", pd.DataFrame([
        {"exercise": "bench_press", "weight_lb": 185.0, "reps": 4, "manually_reviewed": True,
         "candidate_1_probability": 100.0, "activity_id": "1"},
        # Ambiguous set: Garmin itself guessed triceps_extension at decent
        # confidence, but its weight (42.4) happens to be within 20% of the
        # unrelated sit_up set that follows.
        {"exercise": "triceps_extension", "weight_lb": 42.4, "reps": 10, "manually_reviewed": False,
         "candidate_1_probability": 69.5, "activity_id": "1"},
        {"exercise": "sit_up", "weight_lb": 50.0, "reps": 9, "manually_reviewed": False,
         "candidate_1_probability": 89.5, "activity_id": "1"},
    ]))

    infer_exercise_corrections(store)

    review = store.load_exercise_review("strength").sort_values("set_number")
    ambiguous_row = review.iloc[1]
    assert ambiguous_row["our_guess_exercise"] == "triceps_extension"
    assert ambiguous_row["review_status"] == REVIEW_STATUS_PENDING


def test_infer_exercise_corrections_preserves_existing_user_decision(tmp_path) -> None:
    store = CuratedDataStore(file_manager=FileManager(environment="local", local_dir=str(tmp_path)))
    store.merge_activity_summary("strength", pd.DataFrame([
        {"activity_id": "1", "date": "2024-06-01", "duration_min": 30.0},
    ]))
    store.write_activity_detail("strength", "1", pd.DataFrame([
        {"exercise": "unknown", "weight_lb": 60.0, "reps": 8, "manually_reviewed": False,
         "candidate_1_probability": 0.0, "activity_id": "1"},
    ]))
    store.write_exercise_review("strength", pd.DataFrame([
        {"activity_id": "1", "set_number": 1, "our_guess_exercise": "curl", "confidence": 0.4,
         "review_status": REVIEW_STATUS_ACCEPTED, "reason": "User accepted a prior suggestion."},
    ]))

    infer_exercise_corrections(store)

    review = store.load_exercise_review("strength")
    row = review.iloc[0]
    assert row["review_status"] == REVIEW_STATUS_ACCEPTED
    assert row["our_guess_exercise"] == "curl"


def test_infer_exercise_corrections_handles_no_strength_data(tmp_path) -> None:
    store = CuratedDataStore(file_manager=FileManager(environment="local", local_dir=str(tmp_path)))

    infer_exercise_corrections(store)  # should not raise

    assert store.load_exercise_review("strength").empty


def _seed_strength_for_review(store: CuratedDataStore) -> None:
    """One session, three sets, all recorded by Garmin as `curl` -- but the
    third is a mislabeled 200 lb set, the exact contamination pattern seen in
    this account's real data (curl max 164.8 lb vs a 37.5 lb median)."""
    store.merge_activity_summary("strength", pd.DataFrame([
        {"activity_id": "1", "date": "2024-06-01", "duration_min": 45.0},
    ]))
    store.write_activity_detail("strength", "1", pd.DataFrame([
        {"exercise": "curl", "weight_lb": 40.0, "reps": 10, "activity_id": "1"},
        {"exercise": "curl", "weight_lb": 40.0, "reps": 9, "activity_id": "1"},
        {"exercise": "curl", "weight_lb": 200.0, "reps": 5, "activity_id": "1"},
    ]))


def test_apply_exercise_review_corrections_relabels_accepted_sets(tmp_path) -> None:
    store = CuratedDataStore(file_manager=FileManager(environment="local", local_dir=str(tmp_path)))
    _seed_strength_for_review(store)
    detail = store.load_all_activity_details("strength")
    detail = _assign_set_numbers(detail)
    review = pd.DataFrame([
        {"activity_id": "1", "set_number": 3, "our_guess_exercise": "bench_press",
         "review_status": REVIEW_STATUS_ACCEPTED},
    ])

    corrected = _apply_exercise_review_corrections(detail, review)

    by_set = corrected.set_index("set_number")["exercise"]
    assert by_set[1] == "curl"
    assert by_set[2] == "curl"
    assert by_set[3] == "bench_press"


def test_apply_exercise_review_corrections_ignores_pending_and_rejected(tmp_path) -> None:
    """A guess the user hasn't accepted must never silently change what gets
    analyzed -- pending is unreviewed, and rejected means "keep Garmin's own
    label", so neither may be applied."""
    store = CuratedDataStore(file_manager=FileManager(environment="local", local_dir=str(tmp_path)))
    _seed_strength_for_review(store)
    detail = store.load_all_activity_details("strength")
    detail = _assign_set_numbers(detail)
    review = pd.DataFrame([
        {"activity_id": "1", "set_number": 1, "our_guess_exercise": "hammer_curl",
         "review_status": REVIEW_STATUS_PENDING},
        {"activity_id": "1", "set_number": 3, "our_guess_exercise": "bench_press",
         "review_status": REVIEW_STATUS_REJECTED},
    ])

    corrected = _apply_exercise_review_corrections(detail, review)

    assert (corrected["exercise"] == "curl").all()


def test_apply_exercise_review_corrections_is_a_noop_without_review_data(tmp_path) -> None:
    store = CuratedDataStore(file_manager=FileManager(environment="local", local_dir=str(tmp_path)))
    _seed_strength_for_review(store)
    detail = _assign_set_numbers(store.load_all_activity_details("strength"))

    corrected = _apply_exercise_review_corrections(detail, pd.DataFrame())

    assert corrected["exercise"].tolist() == detail["exercise"].tolist()


def test_assign_set_numbers_positions_within_activity_when_set_index_is_null() -> None:
    """Real data has set_index=None for most sets (8155 of 9204 in this
    account), so numbering has to fall back to row order within the activity
    rather than dropping those rows."""
    detail = pd.DataFrame([
        {"activity_id": "a", "set_index": None, "exercise": "curl"},
        {"activity_id": "a", "set_index": None, "exercise": "curl"},
        {"activity_id": "b", "set_index": None, "exercise": "squat"},
    ])

    numbered = _assign_set_numbers(detail)

    assert numbered[numbered["activity_id"] == "a"]["set_number"].tolist() == [1, 2]
    assert numbered[numbered["activity_id"] == "b"]["set_number"].tolist() == [1]


def test_analyze_lifting_excludes_a_reviewed_away_set_from_the_trend(tmp_path) -> None:
    """End-to-end: relabeling the bogus 200 lb set as bench_press must pull it
    out of curl's analyzed 1RM series entirely."""
    store = CuratedDataStore(file_manager=FileManager(environment="local", local_dir=str(tmp_path)))
    dates = pd.date_range("2024-01-01", periods=STRENGTH_MIN_SESSIONS + 2, freq="2D")
    summary_rows = []
    for i, date in enumerate(dates):
        activity_id = str(i)
        summary_rows.append({"activity_id": activity_id, "date": date.date().isoformat(), "duration_min": 45.0})
        store.write_activity_detail("strength", activity_id, pd.DataFrame([
            {"exercise": "curl", "weight_lb": 40.0, "reps": 10, "activity_id": activity_id},
            {"exercise": "curl", "weight_lb": 200.0, "reps": 5, "activity_id": activity_id},
        ]))
    store.merge_activity_summary("strength", pd.DataFrame(summary_rows))
    store.write_exercise_review("strength", pd.DataFrame([
        {"activity_id": str(i), "set_number": 2, "our_guess_exercise": "bench_press",
         "review_status": REVIEW_STATUS_ACCEPTED}
        for i in range(len(dates))
    ]))

    analyze_lifting(store)

    curl_points = store.load_analyzed_points("strength", "curl__unassigned_1rm")
    assert not curl_points.empty
    # Every 200 lb set was relabeled, so curl's estimate must come from the
    # 40 lb x 10 sets alone -- well under even a generous 1RM estimate of them.
    assert curl_points["est_1rm"].max() < 100


def test_load_type_for_defaults_unknown_exercises_to_external_load() -> None:
    assert load_type_for("bench_press") == LOAD_TYPE_EXTERNAL
    assert load_type_for("plank") == LOAD_TYPE_BODYWEIGHT
    # Weighted pull-ups, not assisted -- corrected after the original
    # classification misread Garmin's lumped category (see resolve_variant).
    assert load_type_for("pull_up") == LOAD_TYPE_BW_PLUS
    assert load_type_for("crunch") == LOAD_TYPE_MIXED
    # Anything Garmin starts reporting that isn't in the table should degrade
    # to the pre-existing behaviour, not vanish from analysis.
    assert load_type_for("some_new_machine") == LOAD_TYPE_EXTERNAL


def test_session_metrics_uses_reps_not_load_for_bodyweight_exercises() -> None:
    """plank/leg_raise are 100% weight=0 in real data, so running them through
    the est-1RM path produced an all-NaN series."""
    ex_df = pd.DataFrame({
        "date": pd.to_datetime(["2024-01-01", "2024-01-01", "2024-01-08"]),
        "reps": [20.0, 25.0, 30.0],
        "weight_lb": [0.0, 0.0, 0.0],
    })

    session_df, specs = _session_metrics(ex_df, LOAD_TYPE_BODYWEIGHT, None)

    assert [s[1] for s in specs] == ["top_reps", "rep_volume"]
    first = session_df.iloc[0]
    assert first["top_reps"] == 25.0
    assert first["total_reps"] == 45.0
    assert not session_df[["top_reps", "total_reps"]].isna().any().any()


def test_session_metrics_inverts_assistance_into_effective_load() -> None:
    """Assisted-machine 'weight' is help, not load: measured corr(weight, reps)
    for pull_up is +0.39 while every real load is negative. Subtracting it from
    bodyweight is what makes less assistance read as stronger."""
    bodyweight = pd.Series(
        [200.0] * 3, index=pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-08"])
    )
    ex_df = pd.DataFrame({
        "date": pd.to_datetime(["2024-01-01", "2024-01-08"]),
        "reps": [5.0, 5.0],
        "weight_lb": [80.0, 20.0],  # got stronger: needs far less help
    })

    session_df, specs = _session_metrics(ex_df, LOAD_TYPE_ASSISTED, bodyweight)

    assert [s[1] for s in specs] == ["1rm", "volume"]
    values = session_df.sort_values("date")["est_1rm"].tolist()
    # 200-80=120 lb effective, then 200-20=180 lb -- must increase.
    assert values[1] > values[0]


def test_session_metrics_drops_bodyweight_sets_from_a_mixed_exercise() -> None:
    """crunch mixes 123 bodyweight sets with 448 machine-loaded ones; averaging
    a 0 lb set into the loaded series would drag the estimate down."""
    ex_df = pd.DataFrame({
        "date": pd.to_datetime(["2024-01-01", "2024-01-01"]),
        "reps": [20.0, 10.0],
        "weight_lb": [0.0, 100.0],
    })

    session_df, _ = _session_metrics(ex_df, LOAD_TYPE_MIXED, None)

    assert len(session_df) == 1
    # Volume comes from the 100 lb x 10 set alone, not the bodyweight one.
    assert session_df.iloc[0]["volume_lb"] == 1000.0


def test_session_metrics_returns_empty_for_assisted_without_bodyweight() -> None:
    ex_df = pd.DataFrame({
        "date": pd.to_datetime(["2024-01-01"]), "reps": [5.0], "weight_lb": [50.0],
    })

    session_df, specs = _session_metrics(ex_df, LOAD_TYPE_ASSISTED, None)

    assert session_df.empty
    assert specs == []


def _seed_curve_fit_input(store: CuratedDataStore) -> None:
    store.merge_activity_summary("strength", pd.DataFrame([
        {"activity_id": "1", "date": "2024-06-01", "duration_min": 45.0},
    ]))
    store.write_activity_detail("strength", "1", pd.DataFrame([
        {"exercise": "bench_press", "weight_lb": 150.0, "reps": 8, "activity_id": "1"},
    ]))


_FAKE_CURVE = pd.DataFrame([{"date": pd.Timestamp("2024-06-01"), "e8rm_mean": 150.0}])
_FAKE_BETAS = pd.DataFrame([{"exercise": "bench_press", "beta_mean": 0.13}])


def test_fit_strength_curves_for_all_does_not_persist_a_non_converged_fit(tmp_path, monkeypatch) -> None:
    """Observed live: max r-hat 2.88 produced a lateral-raise e1RM at 2.4x its
    e8RM. The web tier headlines these numbers, so a fit whose chains didn't
    mix must not reach the analyzed layer -- writing nothing is strictly
    better than writing confident-looking nonsense."""
    store = CuratedDataStore(file_manager=FileManager(environment="local", local_dir=str(tmp_path)))
    _seed_curve_fit_input(store)
    monkeypatch.setattr(
        "garmin.analysis.strength_curve.fit_strength_curves",
        lambda *a, **k: ({"bench_press": _FAKE_CURVE}, _FAKE_BETAS,
                         {"max_rhat": 2.88, "divergences": 0, "converged": False}),
    )

    fit_strength_curves_for_all(store)

    assert store.load_strength_curve("strength", "bench_press").empty


def test_fit_strength_curves_for_all_persists_a_converged_fit(tmp_path, monkeypatch) -> None:
    store = CuratedDataStore(file_manager=FileManager(environment="local", local_dir=str(tmp_path)))
    _seed_curve_fit_input(store)
    monkeypatch.setattr(
        "garmin.analysis.strength_curve.fit_strength_curves",
        lambda *a, **k: ({"bench_press": _FAKE_CURVE}, _FAKE_BETAS,
                         {"max_rhat": 1.002, "divergences": 0, "converged": True}),
    )

    fit_strength_curves_for_all(store)

    written = store.load_strength_curve("strength", "bench_press")
    assert not written.empty
    assert written.iloc[0]["e8rm_mean"] == 150.0


def test_resolve_variant_prefers_the_specific_movement_name() -> None:
    assert resolve_variant("bench_press", "DUMBBELL_BENCH_PRESS") == "DUMBBELL_BENCH_PRESS"
    assert resolve_variant("pull_up", "KNEELING_LAT_PULLDOWN") == "KNEELING_LAT_PULLDOWN"


def test_resolve_variant_keeps_unlabelled_sets_separate() -> None:
    """~81% of sets have no specific name. They must not be merged into a
    named variant -- for bench press the unlabelled sets sit at 115-155 lb
    (barbell, warming up from a 45 lb empty bar) while the labelled dumbbell
    work sits at 55 lb, so merging would fuse two different movements."""
    for missing in (None, float("nan"), "", "none"):
        assert resolve_variant("bench_press", missing) == "bench_press__unlabelled"


def test_variant_load_type_separates_pulldowns_from_weighted_pull_ups() -> None:
    """Garmin's `pull_up` category lumps both. A lat pulldown is a machine
    stack; a weighted pull-up is bodyweight plus added load. Treating them
    alike is what made the category's corr(weight, reps) come out positive
    and get it misread as machine assistance."""
    assert variant_load_type("WEIGHTED_PULL_UP", "pull_up") == LOAD_TYPE_BW_PLUS
    assert variant_load_type("KNEELING_LAT_PULLDOWN", "pull_up") == LOAD_TYPE_EXTERNAL
    assert variant_load_type("WIDE_GRIP_LAT_PULLDOWN", "pull_up") == LOAD_TYPE_EXTERNAL


def test_variant_load_type_falls_back_to_the_category() -> None:
    assert variant_load_type("bench_press__unlabelled", "bench_press") == LOAD_TYPE_EXTERNAL
    assert variant_load_type("plank__unlabelled", "plank") == LOAD_TYPE_BODYWEIGHT


def test_session_metrics_adds_bodyweight_for_weighted_pull_ups() -> None:
    """Added load sits on top of bodyweight, so a 0 lb set is a real set at
    full bodyweight rather than a missing value -- and adding weight must
    increase the estimate, not decrease it."""
    bodyweight = pd.Series([200.0] * 2, index=pd.to_datetime(["2024-01-01", "2024-01-08"]))
    ex_df = pd.DataFrame({
        "date": pd.to_datetime(["2024-01-01", "2024-01-08"]),
        "reps": [5.0, 5.0],
        "weight_lb": [0.0, 45.0],   # bodyweight only, then +45 lb
    })

    session_df, specs = _session_metrics(ex_df, LOAD_TYPE_BW_PLUS, bodyweight)

    assert [s[1] for s in specs] == ["1rm", "volume"]
    values = session_df.sort_values("date")["est_1rm"].tolist()
    assert len(values) == 2               # the 0 lb set is kept, not dropped
    assert values[1] > values[0]          # +45 lb is strictly stronger


def test_detect_session_stride_identifies_straight_sets() -> None:
    exercises = ["bench_press"] * 4 + ["curl"] * 4
    assert detect_session_stride(exercises) == 1


def test_detect_session_stride_identifies_supersets() -> None:
    """Real pattern from this account: bench, curl, bench, curl... 187 of 468
    sessions alternate like this, so the immediate neighbour is a different
    exercise and the same movement is two sets away."""
    exercises = ["bench_press", "curl"] * 5
    assert detect_session_stride(exercises) == 2


def test_detect_session_stride_defaults_to_straight_sets_when_unsure() -> None:
    # Too short to read a pattern from, and a session with no repetition at
    # all shouldn't be reclassified on noise.
    assert detect_session_stride(["bench_press", "curl"]) == 1
    assert detect_session_stride(["a", "b", "c", "d", "e", "f", "g"]) == 1


def test_infer_exercise_corrections_uses_superset_neighbours(tmp_path) -> None:
    """In an alternating session the ambiguous set's true match is two sets
    away. Before stride awareness the immediate (wrong-exercise) neighbour
    was weighted twice as heavily as the correct one."""
    store = CuratedDataStore(file_manager=FileManager(environment="local", local_dir=str(tmp_path)))
    store.merge_activity_summary("strength", pd.DataFrame([
        {"activity_id": "1", "date": "2024-06-01", "duration_min": 45.0},
    ]))
    # bench(135) / curl(135-ish) alternating -- weights deliberately close so
    # the weight filter can't do the work and the stride has to.
    rows = []
    for i in range(5):
        rows.append({"exercise": "bench_press", "weight_lb": 135.0, "reps": 8,
                     "manually_reviewed": True, "candidate_1_probability": 100.0, "activity_id": "1"})
        rows.append({"exercise": "curl", "weight_lb": 130.0, "reps": 8,
                     "manually_reviewed": True, "candidate_1_probability": 100.0, "activity_id": "1"})
    # Ambiguous set in a bench slot (even index keeps the alternation).
    rows[6] = {"exercise": "unknown", "weight_lb": 135.0, "reps": 8,
               "manually_reviewed": False, "candidate_1_probability": 0.0, "activity_id": "1"}
    store.write_activity_detail("strength", "1", pd.DataFrame(rows))

    infer_exercise_corrections(store)

    review = store.load_exercise_review("strength").sort_values("set_number")
    ambiguous = review[review["original_exercise"] == "unknown"].iloc[0]
    assert ambiguous["our_guess_exercise"] == "bench_press"


def test_session_fatigue_curve_builds_then_decays() -> None:
    """Each set adds an impulse that decays with rest, so fatigue peaks at the
    last set and falls away afterwards."""
    times = [0.0, 60.0, 120.0, 180.0]
    work = [1.0, 1.0, 1.0, 1.0]
    samples = [0.0, 90.0, 180.0, 600.0, 1800.0]

    curve = session_fatigue_curve(times, work, samples)

    assert curve[2] == 100.0            # scaled so the peak is 100
    assert curve[0] < curve[1] < curve[2]   # accumulating across sets
    assert curve[3] < curve[2]              # decaying during rest
    assert curve[4] < curve[3]
    assert all(0 <= v <= 100 for v in curve)


def test_session_fatigue_curve_weights_heavier_sets_more() -> None:
    heavy = session_fatigue_curve([0.0], [10.0], [0.0, 60.0])
    light = session_fatigue_curve([0.0], [1.0], [0.0, 60.0])
    # Both are scaled to their own peak, so the *shape* matches; what matters
    # is that a bigger impulse doesn't change the decay profile.
    assert heavy == light


def test_session_fatigue_curve_handles_empty_and_degenerate_input() -> None:
    assert session_fatigue_curve([], [], [0.0, 1.0]) == [0.0, 0.0]
    assert session_fatigue_curve([0.0], [0.0], [0.0, 1.0]) == [0.0, 0.0]
    assert session_fatigue_curve([0.0], [1.0], []) == []


def test_session_fatigue_curve_ignores_sets_that_have_not_happened_yet() -> None:
    """A sample taken before a set must not carry that set's fatigue."""
    curve = session_fatigue_curve([100.0], [1.0], [0.0, 50.0, 100.0])

    assert curve[0] == 0.0
    assert curve[1] == 0.0
    assert curve[2] == 100.0


def test_reject_watch_left_running_flags_implausible_tempo() -> None:
    """Forgetting to stop the watch inflates a set's duration into the rest
    period. Real example from this account: 10 reps recorded over 1014s."""
    detail = pd.DataFrame([
        {"exercise": "bench_press", "reps": 8.0, "duration_s": 40.0},    # ~5 s/rep
        {"exercise": "bench_press", "reps": 10.0, "duration_s": 1014.0},  # ~101 s/rep
        {"exercise": "squat", "reps": 1.0, "duration_s": 202.9},          # ~203 s/rep
    ])

    out = reject_watch_left_running(detail)

    assert out["duration_suspect"].tolist() == [False, True, True]


def test_reject_watch_left_running_exempts_timed_holds() -> None:
    """A plank logged as 1 rep over 60s is a 60-second hold, not a forgotten
    watch. Bodyweight exercises are timed, not rep-paced, so a
    seconds-per-rep threshold does not apply to them."""
    detail = pd.DataFrame([
        {"exercise": "plank", "reps": 1.0, "duration_s": 60.0},
        {"exercise": "plank", "reps": 1.0, "duration_s": 153.0},
        {"exercise": "leg_raise", "reps": 1.0, "duration_s": 90.0},
    ])

    out = reject_watch_left_running(detail)

    assert not out["duration_suspect"].any()


def test_reject_watch_left_running_keeps_the_rows() -> None:
    """Only the timing is untrustworthy -- reps and weight are still good, so
    the set stays usable for strength estimates and only drops out of
    tempo-based work."""
    detail = pd.DataFrame([
        {"exercise": "bench_press", "reps": 10.0, "duration_s": 1014.0, "weight_lb": 135.0},
    ])

    out = reject_watch_left_running(detail)

    assert len(out) == 1
    assert out.iloc[0]["duration_suspect"]
    assert out.iloc[0]["weight_lb"] == 135.0
    assert out.iloc[0]["reps"] == 10.0


def test_reject_watch_left_running_handles_missing_columns_and_zero_reps() -> None:
    assert not reject_watch_left_running(pd.DataFrame({"exercise": ["squat"]}))["duration_suspect"].any()
    zero = reject_watch_left_running(pd.DataFrame([
        {"exercise": "squat", "reps": 0.0, "duration_s": 60.0},
        {"exercise": "squat", "reps": None, "duration_s": 60.0},
    ]))
    assert not zero["duration_suspect"].any()   # undefined, not suspect
