from __future__ import annotations

import json
import math
from datetime import date
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from garmin.io.curated_store import CuratedDataStore

MUSCLE_LABELS = {
    "chest": "Chest",
    "back": "Back",
    "shoulders": "Shoulders",
    "biceps": "Biceps",
    "triceps": "Triceps",
    "forearms": "Forearms",
    "core": "Core",
    "glutes": "Glutes",
    "quads": "Quads",
    "hamstrings": "Hamstrings",
    "calves": "Calves",
    "lats": "Lats",
    "traps": "Traps",
    "hip_flexors": "Hip Flexors",
    "obliques": "Obliques",
    "adductors": "Adductors",
}

EXERCISE_MUSCLES: dict[str, dict[str, float]] = {
    "bench_press": {"chest": 1.0, "triceps": 0.7, "shoulders": 0.45},
    "incline_bench_press": {"chest": 0.9, "shoulders": 0.7, "triceps": 0.45},
    "decline_bench_press": {"chest": 1.0, "triceps": 0.65, "shoulders": 0.25},
    "shoulder_press": {"shoulders": 1.0, "triceps": 0.6, "core": 0.2},
    "overhead_press": {"shoulders": 1.0, "triceps": 0.65, "core": 0.25},
    "lateral_raise": {"shoulders": 1.0, "traps": 0.15},
    "row": {"back": 0.85, "lats": 0.8, "biceps": 0.55, "forearms": 0.35},
    "lat_pulldown": {"lats": 1.0, "back": 0.7, "biceps": 0.45, "forearms": 0.25},
    "pull_up": {"lats": 1.0, "back": 0.75, "biceps": 0.55, "core": 0.15},
    "chin_up": {"lats": 0.8, "back": 0.6, "biceps": 0.8, "core": 0.15},
    "deadlift": {"glutes": 0.95, "hamstrings": 0.85, "back": 0.75, "forearms": 0.45, "core": 0.3},
    "romanian_deadlift": {"hamstrings": 1.0, "glutes": 0.9, "back": 0.35, "core": 0.2},
    "squat": {"quads": 0.95, "glutes": 0.85, "core": 0.35, "hamstrings": 0.25, "adductors": 0.3},
    "front_squat": {"quads": 1.0, "core": 0.45, "glutes": 0.55, "adductors": 0.25},
    "lunge": {"quads": 0.8, "glutes": 0.7, "hamstrings": 0.35, "calves": 0.15, "adductors": 0.3},
    "leg_press": {"quads": 1.0, "glutes": 0.55, "hamstrings": 0.2, "adductors": 0.2},
    "leg_curl": {"hamstrings": 1.0, "calves": 0.1},
    "leg_extension": {"quads": 1.0},
    "hip_raise": {"glutes": 1.0, "hamstrings": 0.4, "core": 0.15},
    "calf_raise": {"calves": 1.0},
    "curl": {"biceps": 1.0, "forearms": 0.35},
    "hammer_curl": {"biceps": 0.75, "forearms": 0.8},
    "triceps_extension": {"triceps": 1.0, "shoulders": 0.1},
    "dip": {"triceps": 0.75, "chest": 0.55, "shoulders": 0.35},
    "push_up": {"chest": 0.85, "triceps": 0.55, "shoulders": 0.35, "core": 0.2},
    "flye": {"chest": 1.0, "shoulders": 0.25},
    "crunch": {"core": 1.0, "obliques": 0.25},
    "sit_up": {"core": 1.0, "hip_flexors": 0.25},
    "plank": {"core": 1.0, "shoulders": 0.2, "glutes": 0.15, "obliques": 0.2},
    "leg_raise": {"core": 0.9, "hip_flexors": 0.45},
    "chop": {"core": 1.0, "shoulders": 0.2, "obliques": 0.9},
    "shoulder_stability": {"shoulders": 0.8, "core": 0.2},
}


def format_exercise_label(exercise: str) -> str:
    return exercise.replace("_", " ").title()


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (float, int, np.integer, np.floating)):
        if pd.isna(value):
            return None
        return float(value)
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(parsed) else parsed


def blended_1rm(weight_lb: float, reps: float) -> float:
    """Blend multiple rep-to-max formulas so high-rep sets are less jumpy than raw Epley."""
    weight = _safe_float(weight_lb)
    rep_count = _safe_float(reps)
    if weight is None or rep_count is None or weight <= 0 or rep_count <= 0:
        return float("nan")

    epley = weight * (1.0 + rep_count / 30.0)
    brzycki = weight * 36.0 / max(1.0, 37.0 - rep_count)
    lombardi = weight * math.pow(rep_count, 0.10)

    if rep_count <= 5:
        weights = (0.45, 0.4, 0.15)
    elif rep_count <= 10:
        weights = (0.45, 0.35, 0.2)
    else:
        weights = (0.25, 0.2, 0.55)

    formulas = np.array([epley, brzycki, lombardi], dtype=float)
    return float(np.average(formulas, weights=np.array(weights, dtype=float)))


def session_strength_estimate(exercise_sets: pd.DataFrame) -> dict[str, float]:
    """Collapse a set-level workout slice into one session estimate and load summary."""
    sets = exercise_sets.dropna(subset=["weight_lb", "reps"]).copy()
    if sets.empty:
        return {
            "legacy_epley_1rm": float("nan"),
            "improved_1rm": float("nan"),
            "volume_lb": 0.0,
            "working_sets": 0.0,
            "total_reps": 0.0,
            "max_weight_lb": float("nan"),
        }

    sets["legacy_epley_1rm"] = sets["weight_lb"] * (1.0 + sets["reps"] / 30.0)
    sets["improved_set_1rm"] = [
        blended_1rm(weight_lb, reps) for weight_lb, reps in zip(sets["weight_lb"], sets["reps"], strict=False)
    ]
    sets["volume_lb"] = sets["weight_lb"] * sets["reps"]
    ranked = sets.sort_values(["improved_set_1rm", "weight_lb", "reps"], ascending=False).head(3).reset_index(drop=True)

    top_weight = max(float(ranked["weight_lb"].max()), 1.0)
    weights = []
    for rank, row in ranked.iterrows():
        rep_confidence = 1.0 / (1.0 + abs(float(row["reps"]) - 6.0) / 6.0)
        load_confidence = math.sqrt(max(float(row["weight_lb"]), 1.0) / top_weight)
        weights.append((0.68**rank) * max(0.25, rep_confidence) * max(0.4, load_confidence))

    improved = float(np.average(ranked["improved_set_1rm"], weights=np.array(weights, dtype=float)))

    return {
        "legacy_epley_1rm": float(sets["legacy_epley_1rm"].max()),
        "improved_1rm": improved,
        "volume_lb": float(sets["volume_lb"].sum()),
        "working_sets": float(len(sets)),
        "total_reps": float(sets["reps"].sum()),
        "max_weight_lb": float(sets["weight_lb"].max()),
    }


def _serialize_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, list):
        return [_serialize_value(item) for item in value]
    if isinstance(value, tuple):
        return [_serialize_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _serialize_value(item) for key, item in value.items()}
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and math.isnan(value):
        return None
    if pd.isna(value):
        return None
    return value


def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df.empty:
        return []
    return [
        {column: _serialize_value(value) for column, value in row.items()}
        for row in df.to_dict(orient="records")
    ]


def _rolling_time_mean(group: pd.DataFrame, value_col: str, output_col: str, window: str = "42D") -> pd.DataFrame:
    ordered = group.sort_values("date").copy()
    rolled = (
        ordered.set_index("date")[value_col]
        .rolling(window, min_periods=1)
        .mean()
        .reset_index(drop=True)
    )
    ordered[output_col] = rolled.to_numpy()
    return ordered


def _build_strength_payload(store: CuratedDataStore) -> dict[str, Any]:
    summary = store.load_activity_summary("strength")
    detail = store.load_all_activity_details("strength")
    if summary.empty or detail.empty:
        return {"available": False, "sessions": [], "exercise_order": [], "exercise_summaries": []}

    summary = summary.copy()
    summary["date"] = pd.to_datetime(summary["date"])

    detail = detail.copy()
    detail["exercise"] = detail["exercise"].astype(str).str.strip().str.lower().str.replace(" ", "_", regex=False)
    detail = detail.merge(summary[["activity_id", "date"]], on="activity_id", how="left")
    detail = detail.dropna(subset=["date", "reps", "weight_lb", "exercise"])
    if detail.empty:
        return {"available": False, "sessions": [], "exercise_order": [], "exercise_summaries": []}

    session_rows: list[dict[str, Any]] = []
    for (activity_id, session_date, exercise), sets in detail.groupby(["activity_id", "date", "exercise"]):
        estimate = session_strength_estimate(sets)
        muscles = EXERCISE_MUSCLES.get(str(exercise), {})
        session_rows.append({
            "activity_id": str(activity_id),
            "date": pd.Timestamp(session_date),
            "exercise": str(exercise),
            "exercise_label": format_exercise_label(str(exercise)),
            "legacy_epley_1rm": estimate["legacy_epley_1rm"],
            "improved_1rm": estimate["improved_1rm"],
            "volume_lb": estimate["volume_lb"],
            "working_sets": estimate["working_sets"],
            "total_reps": estimate["total_reps"],
            "max_weight_lb": estimate["max_weight_lb"],
            "muscles": sorted(muscles.keys()),
        })

    sessions = pd.DataFrame(session_rows).sort_values(["date", "exercise"]).reset_index(drop=True)
    sessions["estimator_delta_lb"] = sessions["improved_1rm"] - sessions["legacy_epley_1rm"]

    trended_frames = []
    for _, group in sessions.groupby("exercise", sort=False):
        trended_frames.append(_rolling_time_mean(group, "improved_1rm", "trend_42d"))
    sessions = pd.concat(trended_frames, ignore_index=True).sort_values(["date", "exercise"]).reset_index(drop=True)

    summary_rows = []
    for exercise, group in sessions.groupby("exercise", sort=False):
        latest = group.sort_values("date").iloc[-1]
        summary_rows.append({
            "exercise": exercise,
            "exercise_label": format_exercise_label(str(exercise)),
            "sessions": int(len(group)),
            "last_date": latest["date"],
            "latest_improved_1rm": latest["improved_1rm"],
            "latest_legacy_1rm": latest["legacy_epley_1rm"],
            "peak_improved_1rm": float(group["improved_1rm"].max()),
            "total_volume_lb": float(group["volume_lb"].sum()),
            "median_volume_lb": float(group["volume_lb"].median()),
            "mean_estimator_delta_lb": float(group["estimator_delta_lb"].mean()),
            "muscles": sorted(EXERCISE_MUSCLES.get(str(exercise), {}).keys()),
        })

    exercise_summaries = (
        pd.DataFrame(summary_rows)
        .sort_values(["sessions", "last_date", "peak_improved_1rm"], ascending=[False, False, False])
        .reset_index(drop=True)
    )

    return {
        "available": True,
        "sessions": _records(sessions),
        "exercise_summaries": _records(exercise_summaries),
        "exercise_order": exercise_summaries["exercise"].tolist(),
        "exercise_muscles": EXERCISE_MUSCLES,
        "muscle_labels": {key: label for key, label in MUSCLE_LABELS.items() if key in {m for muscles in EXERCISE_MUSCLES.values() for m in muscles}},
        "notes": [
            "Improved estimator blends Epley, Brzycki, and Lombardi per set, then averages the best three sets instead of trusting one peak set.",
            "Current app data already has set-level strength detail, so this sketch can stay read-only and still give better per-session comparisons.",
        ],
    }


def _build_climbing_payload(store: CuratedDataStore) -> dict[str, Any]:
    session_rows: list[dict[str, Any]] = []
    for dataset in ["bouldering", "rock_climbing"]:
        summary = store.load_activity_summary(dataset)
        if summary.empty:
            continue
        frame = summary.copy()
        frame["date"] = pd.to_datetime(frame["date"])
        frame["type"] = dataset
        frame["type_label"] = format_exercise_label(dataset)
        for column in ["duration_min", "calories", "avg_hr", "max_hr", "name"]:
            if column not in frame.columns:
                frame[column] = None
        session_rows.extend(frame[["activity_id", "date", "type", "type_label", "name", "duration_min", "calories", "avg_hr", "max_hr"]].to_dict(orient="records"))

    if not session_rows:
        return {"available": False, "sessions": []}

    sessions = pd.DataFrame(session_rows).sort_values("date").reset_index(drop=True)
    sessions["year_month"] = sessions["date"].dt.strftime("%Y-%m")
    sessions["weekday"] = sessions["date"].dt.day_name().str[:3]
    sessions["hours"] = sessions["duration_min"].fillna(0) / 60.0

    return {
        "available": True,
        "sessions": _records(sessions),
        "notes": [
            "Current Garmin climbing data in this repo is session-level only: date, duration, calories, and heart-rate summary where available.",
            "There is no route-grade, send/fall, wall angle, or hold-type detail in curated storage yet, so this sketch focuses on frequency, duration, and effort patterns.",
        ],
    }


def build_activity_explorer_payload(store: CuratedDataStore) -> dict[str, Any]:
    """Build read-only payloads for a standalone lifting and climbing explorer."""
    return {
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "strength": _build_strength_payload(store),
        "climbing": _build_climbing_payload(store),
    }


def build_activity_explorer_html(store: CuratedDataStore) -> str:
    """Render a standalone interactive HTML explorer for lifting and climbing."""
    payload = build_activity_explorer_payload(store)
    payload_json = json.dumps(payload)

    return f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>Garmin Activity Explorer Sketch</title>
  <script src=\"https://cdn.plot.ly/plotly-2.35.2.min.js\"></script>
  <style>
    :root {{
      --paper: #f4efe6;
      --ink: #1d2935;
      --muted: #6b7280;
      --card: rgba(255, 255, 255, 0.72);
      --line: rgba(29, 41, 53, 0.12);
      --accent: #ba4a30;
      --accent-2: #2d7d7c;
      --accent-3: #d39b2d;
      --accent-4: #6e4c9a;
      --chip: rgba(29, 41, 53, 0.08);
      --chip-active: #1d2935;
      --shadow: 0 18px 55px rgba(61, 49, 28, 0.12);
      --muscle-base: #d8d3ca;
      --muscle-stroke: rgba(29, 41, 53, 0.14);
    }}

    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      font-family: Georgia, \"Iowan Old Style\", \"Palatino Linotype\", serif;
      background:
        radial-gradient(circle at top left, rgba(186, 74, 48, 0.16), transparent 28%),
        radial-gradient(circle at top right, rgba(45, 125, 124, 0.16), transparent 25%),
        linear-gradient(180deg, #f7f2ea 0%, #efe6d8 100%);
      min-height: 100vh;
    }}

    .page {{
      max-width: 1480px;
      margin: 0 auto;
      padding: 32px 20px 56px;
    }}

    .hero {{
      display: grid;
      grid-template-columns: minmax(0, 1.4fr) minmax(260px, 0.9fr);
      gap: 24px;
      align-items: end;
      margin-bottom: 24px;
    }}

    .hero h1 {{
      margin: 0 0 10px;
      font-size: clamp(2.5rem, 5vw, 4.4rem);
      line-height: 0.95;
      letter-spacing: -0.03em;
    }}

    .hero p, .muted {{ color: var(--muted); }}

    .hero-card, .panel, .kpi, .note {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 24px;
      backdrop-filter: blur(12px);
      box-shadow: var(--shadow);
    }}

    .hero-card {{ padding: 22px; }}
    .hero-card strong {{ display: block; font-size: 0.8rem; letter-spacing: 0.14em; text-transform: uppercase; color: var(--accent); }}
    .hero-card .value {{ font-size: 2rem; margin-top: 10px; }}

    .tabs {{ display: flex; gap: 12px; margin: 10px 0 20px; flex-wrap: wrap; }}
    .tab-button {{
      border: 0;
      border-radius: 999px;
      padding: 12px 18px;
      background: rgba(255,255,255,0.6);
      color: var(--ink);
      font: inherit;
      cursor: pointer;
      box-shadow: inset 0 0 0 1px var(--line);
      transition: transform 120ms ease, background 120ms ease, color 120ms ease;
    }}
    .tab-button.active {{ background: var(--ink); color: #fff; transform: translateY(-1px); }}

    .tab-panel {{ display: none; gap: 20px; }}
    .tab-panel.active {{ display: grid; }}
    .grid-strength {{ grid-template-columns: minmax(0, 1.4fr) minmax(320px, 0.9fr); }}
    .grid-climbing {{ grid-template-columns: minmax(0, 1fr); }}

    .panel {{ padding: 18px; }}
    .panel h2, .panel h3 {{ margin: 0 0 10px; }}
    .panel-header {{ display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; flex-wrap: wrap; margin-bottom: 14px; }}

    .controls {{ display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }}
    select, .ghost-button {{
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.78);
      border-radius: 999px;
      padding: 10px 14px;
      font: inherit;
      color: var(--ink);
    }}
    .ghost-button {{ cursor: pointer; }}

    .chip-row {{ display: flex; flex-wrap: wrap; gap: 8px; }}
    .chip {{
      border: 0;
      border-radius: 999px;
      padding: 9px 13px;
      cursor: pointer;
      background: var(--chip);
      color: var(--ink);
      font: inherit;
      transition: background 120ms ease, color 120ms ease, transform 120ms ease;
    }}
    .chip.active {{ background: var(--chip-active); color: #fff; transform: translateY(-1px); }}

    .chart {{ min-height: 320px; }}
    .chart.tall {{ min-height: 380px; }}

    .two-up {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 20px; }}
    .kpi-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }}
    .kpi {{ padding: 16px; }}
    .kpi .label {{ font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.12em; color: var(--muted); }}
    .kpi .value {{ font-size: 1.75rem; margin-top: 8px; }}
    .kpi .detail {{ color: var(--muted); margin-top: 4px; }}

    .table-wrap {{ overflow: auto; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.95rem; }}
    th, td {{ padding: 10px 8px; text-align: left; border-bottom: 1px solid var(--line); white-space: nowrap; }}
    th {{ font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.12em; color: var(--muted); }}

    .note {{ padding: 16px 18px; }}
    .note p {{ margin: 6px 0; }}

    .muscle-wrap {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; }}
    .figure-card {{ border: 1px solid var(--line); border-radius: 22px; background: rgba(255,255,255,0.55); padding: 12px; }}
    svg {{ width: 100%; height: auto; display: block; }}
    .silhouette {{ fill: rgba(29, 41, 53, 0.06); stroke: rgba(29, 41, 53, 0.18); stroke-width: 2; }}
    .muscle-region {{ fill: var(--muscle-base); stroke: var(--muscle-stroke); stroke-width: 1.5; cursor: pointer; transition: opacity 100ms ease, transform 100ms ease; }}
    .muscle-region:hover {{ opacity: 0.9; }}
    .muscle-region.active {{ stroke: var(--ink); stroke-width: 3; }}

    .legend {{ display: flex; gap: 12px; flex-wrap: wrap; margin-top: 10px; color: var(--muted); font-size: 0.92rem; }}
    .swatch {{ width: 14px; height: 14px; border-radius: 4px; display: inline-block; margin-right: 6px; vertical-align: -2px; }}

    @media (max-width: 1080px) {{
      .hero, .grid-strength {{ grid-template-columns: 1fr; }}
      .two-up, .kpi-grid, .muscle-wrap {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <div class=\"page\">
    <a href=\"/\" style=\"display: inline-block; margin-bottom: 10px; color: var(--muted); font-size: 0.85rem; text-decoration: none;\">&larr; Back to Garmin</a>
    <section class=\"hero\">
      <div>
        <p class=\"muted\">Standalone exploratory sketch built on top of curated Garmin data</p>
        <h1>Lift sharper. Climb clearer.</h1>
        <p class=\"muted\">This prototype stays outside the main app: it reads your current curated activity data, compares a multi-set lifting estimator against the legacy top-set Epley view, and gives climbing its own exploratory surface even with only session-level Garmin summaries.</p>
      </div>
      <div class=\"hero-card\">
        <strong>Generated</strong>
        <div class=\"value\" id=\"generated-at\"></div>
        <div class=\"muted\">Interactive HTML sketch with Plotly charts, clickable exercise chips, and a muscle-region overlay.</div>
      </div>
    </section>

    <div class=\"tabs\">
      <button class=\"tab-button active\" data-tab=\"strength\">Lifting Explorer</button>
      <button class=\"tab-button\" data-tab=\"climbing\">Climbing Explorer</button>
    </div>

    <section id=\"strength\" class=\"tab-panel grid-strength active\">
      <div class=\"panel\">
        <div class=\"panel-header\">
          <div>
            <h2>Strength Progression</h2>
            <p class=\"muted\">Click exercises or muscles to focus the charts. The improved estimate uses the best three sets in a session instead of a single peak set.</p>
          </div>
          <div class=\"controls\">
            <select id=\"strength-range\">
              <option value=\"90\">Last 90 days</option>
              <option value=\"180\">Last 180 days</option>
              <option value=\"365\" selected>Last 365 days</option>
              <option value=\"all\">All time</option>
            </select>
            <select id=\"strength-metric\">
              <option value=\"improved\" selected>Improved estimate</option>
              <option value=\"legacy\">Legacy Epley</option>
            </select>
            <button id=\"strength-reset\" class=\"ghost-button\">Reset filters</button>
          </div>
        </div>
        <div class=\"chip-row\" id=\"exercise-chips\"></div>
        <div id=\"strength-trend\" class=\"chart tall\"></div>
        <div class=\"two-up\">
          <div id=\"strength-volume\" class=\"chart\"></div>
          <div id=\"strength-delta\" class=\"chart\"></div>
        </div>
      </div>

      <div class=\"panel\">
        <div class=\"panel-header\">
          <div>
            <h2>Targeted Muscle Sketch</h2>
            <p class=\"muted\">Click a muscle region to filter to the exercises mapped to it. Intensity reflects recent training load from the filtered sessions.</p>
          </div>
        </div>
        <div class=\"muscle-wrap\">
          <div class=\"figure-card\">{_front_body_svg()}</div>
          <div class=\"figure-card\">{_back_body_svg()}</div>
        </div>
        <div class=\"legend\">
          <span><span class=\"swatch\" style=\"background: rgba(186, 74, 48, 0.25);\"></span>lower load</span>
          <span><span class=\"swatch\" style=\"background: rgba(186, 74, 48, 0.85);\"></span>higher load</span>
        </div>
        <div class=\"kpi-grid\" id=\"strength-kpis\"></div>
        <div class=\"note\" id=\"strength-notes\"></div>
        <div class=\"table-wrap\">
          <table>
            <thead>
              <tr>
                <th>Exercise</th>
                <th>Sessions</th>
                <th>Latest</th>
                <th>Peak</th>
                <th>Median Volume</th>
                <th>Bias vs Legacy</th>
              </tr>
            </thead>
            <tbody id=\"strength-table\"></tbody>
          </table>
        </div>
      </div>
    </section>

    <section id=\"climbing\" class=\"tab-panel grid-climbing\">
      <div class=\"panel\">
        <div class=\"panel-header\">
          <div>
            <h2>Climbing Rhythm</h2>
            <p class=\"muted\">This sketch uses the climbing fields Garmin currently exposes here: session date, duration, calories, and occasional heart-rate summary.</p>
          </div>
          <div class=\"controls\">
            <select id=\"climbing-range\">
              <option value=\"90\">Last 90 days</option>
              <option value=\"180\">Last 180 days</option>
              <option value=\"365\" selected>Last 365 days</option>
              <option value=\"all\">All time</option>
            </select>
            <select id=\"climbing-type\">
              <option value=\"all\" selected>All climbing</option>
              <option value=\"bouldering\">Bouldering</option>
              <option value=\"rock_climbing\">Rock climbing</option>
            </select>
          </div>
        </div>
        <div class=\"kpi-grid\" id=\"climbing-kpis\"></div>
        <div class=\"two-up\">
          <div id=\"climbing-weekly\" class=\"chart tall\"></div>
          <div id=\"climbing-scatter\" class=\"chart tall\"></div>
        </div>
        <div id=\"climbing-heatmap\" class=\"chart\"></div>
        <div class=\"note\" id=\"climbing-notes\"></div>
      </div>
    </section>
  </div>

  <script id=\"payload\" type=\"application/json\">{payload_json}</script>
  <script>
    const payload = JSON.parse(document.getElementById('payload').textContent);
    const strengthState = {{
      range: '365',
      metric: 'improved',
      activeTab: 'strength',
      selectedExercises: new Set((payload.strength.exercise_order || []).slice(0, 6)),
      selectedMuscles: new Set(),
    }};
    const climbingState = {{ range: '365', type: 'all' }};

    const colors = {{
      improved: '#ba4a30',
      improvedTrend: '#7d2f1d',
      legacy: '#2d7d7c',
      legacyTrend: '#1b5c5a',
      volume: '#d39b2d',
      delta: '#6e4c9a',
      bouldering: '#ba4a30',
      rock_climbing: '#2d7d7c',
    }};

    document.getElementById('generated-at').textContent = new Date(payload.generated_at).toLocaleString();

    function labelize(value) {{
      return value.split('_').map((part) => part.charAt(0).toUpperCase() + part.slice(1)).join(' ');
    }}

    function parseDate(value) {{
      return new Date(value);
    }}

    function rangeCutoff(days, rows) {{
      if (days === 'all' || !rows.length) return null;
      const last = rows.reduce((best, row) => Math.max(best, parseDate(row.date).getTime()), 0);
      return last - (Number(days) * 24 * 60 * 60 * 1000);
    }}

    function withinRange(row, cutoff) {{
      if (cutoff === null) return true;
      return parseDate(row.date).getTime() >= cutoff;
    }}

    function currentStrengthSessions() {{
      const allRows = payload.strength.sessions || [];
      const cutoff = rangeCutoff(strengthState.range, allRows);
      let rows = allRows.filter((row) => withinRange(row, cutoff));
      if (strengthState.selectedMuscles.size) {{
        rows = rows.filter((row) => row.muscles.some((muscle) => strengthState.selectedMuscles.has(muscle)));
      }}
      if (strengthState.selectedExercises.size) {{
        rows = rows.filter((row) => strengthState.selectedExercises.has(row.exercise));
      }}
      return rows;
    }}

    function currentClimbingSessions() {{
      const allRows = payload.climbing.sessions || [];
      const cutoff = rangeCutoff(climbingState.range, allRows);
      return allRows.filter((row) => withinRange(row, cutoff) && (climbingState.type === 'all' || row.type === climbingState.type));
    }}

    function uniqueOrdered(values) {{
      return [...new Set(values)];
    }}

    function formatNumber(value, digits = 0) {{
      if (value === null || value === undefined || Number.isNaN(value)) return '—';
      return Number(value).toLocaleString(undefined, {{ maximumFractionDigits: digits, minimumFractionDigits: digits }});
    }}

    function renderExerciseChips() {{
      const container = document.getElementById('exercise-chips');
      container.innerHTML = '';
      (payload.strength.exercise_order || []).forEach((exercise) => {{
        const button = document.createElement('button');
        button.className = 'chip' + (strengthState.selectedExercises.has(exercise) ? ' active' : '');
        button.textContent = labelize(exercise);
        button.onclick = () => {{
          if (strengthState.selectedExercises.has(exercise)) {{
            strengthState.selectedExercises.delete(exercise);
          }} else {{
            strengthState.selectedExercises.add(exercise);
          }}
          renderStrength();
        }};
        container.appendChild(button);
      }});
    }}

    function rollingWeekly(rows, valueAccessor) {{
      const byWeek = new Map();
      rows.forEach((row) => {{
        const day = parseDate(row.date);
        const monday = new Date(day);
        const diff = (day.getDay() + 6) % 7;
        monday.setDate(day.getDate() - diff);
        const key = monday.toISOString().slice(0, 10);
        const current = byWeek.get(key) || 0;
        byWeek.set(key, current + valueAccessor(row));
      }});
      return [...byWeek.entries()].sort((a, b) => a[0].localeCompare(b[0]));
    }}

    function renderStrengthCharts(rows) {{
      const metricKey = strengthState.metric === 'legacy' ? 'legacy_epley_1rm' : 'improved_1rm';
      const trendKey = strengthState.metric === 'legacy' ? 'legacy_epley_1rm' : 'trend_42d';
      const exerciseOrder = uniqueOrdered(rows.map((row) => row.exercise));
      const trendTraces = [];

      exerciseOrder.forEach((exercise, index) => {{
        const exerciseRows = rows.filter((row) => row.exercise === exercise).sort((a, b) => parseDate(a.date) - parseDate(b.date));
        const tone = strengthState.metric === 'legacy' ? colors.legacy : colors.improved;
        trendTraces.push({{
          x: exerciseRows.map((row) => row.date),
          y: exerciseRows.map((row) => row[metricKey]),
          type: 'scatter',
          mode: 'markers',
          name: labelize(exercise) + ' sessions',
          marker: {{ color: tone, size: 7, opacity: 0.38 + (index % 3) * 0.12 }},
          legendgroup: exercise,
          showlegend: false,
          hovertemplate: '%{{x|%b %d, %Y}}<br>%{{y:.1f}} lb<extra>' + labelize(exercise) + '</extra>',
        }});
        trendTraces.push({{
          x: exerciseRows.map((row) => row.date),
          y: exerciseRows.map((row) => row[trendKey]),
          type: 'scatter',
          mode: 'lines',
          name: labelize(exercise),
          line: {{ color: Plotly.d3.interpolateRgb(tone, '#1d2935')(0.25), width: 3 }},
          legendgroup: exercise,
        }});
      }});

      Plotly.newPlot('strength-trend', trendTraces, {{
        paper_bgcolor: 'rgba(0,0,0,0)',
        plot_bgcolor: 'rgba(0,0,0,0)',
        title: strengthState.metric === 'legacy' ? 'Legacy top-set Epley view' : 'Improved multi-set session estimate',
        margin: {{ l: 56, r: 18, t: 54, b: 52 }},
        xaxis: {{ gridcolor: 'rgba(29,41,53,0.08)' }},
        yaxis: {{ title: 'Estimated 1RM (lb)', gridcolor: 'rgba(29,41,53,0.08)' }},
        legend: {{ orientation: 'h', y: -0.22 }},
      }}, {{ responsive: true, displaylogo: false }});

      const weeklyVolume = rollingWeekly(rows, (row) => (row.volume_lb || 0) / 1000.0);
      Plotly.newPlot('strength-volume', [{{
        x: weeklyVolume.map(([week]) => week),
        y: weeklyVolume.map(([, load]) => load),
        type: 'bar',
        marker: {{ color: colors.volume }},
        hovertemplate: '%{{x}}<br>%{{y:.1f}}k lb<extra>Weekly load</extra>',
      }}], {{
        paper_bgcolor: 'rgba(0,0,0,0)',
        plot_bgcolor: 'rgba(0,0,0,0)',
        title: 'Weekly load',
        margin: {{ l: 56, r: 14, t: 50, b: 46 }},
        yaxis: {{ title: 'Thousand lb', gridcolor: 'rgba(29,41,53,0.08)' }},
      }}, {{ responsive: true, displaylogo: false }});

      const latestByExercise = exerciseOrder.map((exercise) => rows.filter((row) => row.exercise === exercise).sort((a, b) => parseDate(a.date) - parseDate(b.date)).at(-1)).filter(Boolean);
      Plotly.newPlot('strength-delta', [
        {{
          x: latestByExercise.map((row) => row.exercise_label),
          y: latestByExercise.map((row) => row.legacy_epley_1rm),
          type: 'bar',
          name: 'Legacy',
          marker: {{ color: colors.legacy }},
        }},
        {{
          x: latestByExercise.map((row) => row.exercise_label),
          y: latestByExercise.map((row) => row.improved_1rm),
          type: 'bar',
          name: 'Improved',
          marker: {{ color: colors.improved }},
        }},
      ], {{
        barmode: 'group',
        paper_bgcolor: 'rgba(0,0,0,0)',
        plot_bgcolor: 'rgba(0,0,0,0)',
        title: 'Latest estimate comparison',
        margin: {{ l: 56, r: 14, t: 50, b: 90 }},
        yaxis: {{ title: 'Estimated 1RM (lb)', gridcolor: 'rgba(29,41,53,0.08)' }},
      }}, {{ responsive: true, displaylogo: false }});
    }}

    function renderStrengthKPIs(rows) {{
      const latestDate = rows.length ? rows.reduce((best, row) => Math.max(best, parseDate(row.date).getTime()), 0) : null;
      const totalVolume = rows.reduce((sum, row) => sum + (row.volume_lb || 0), 0);
      const avgBias = rows.length ? rows.reduce((sum, row) => sum + (row.estimator_delta_lb || 0), 0) / rows.length : 0;
      const kpis = [
        {{ label: 'Sessions', value: formatNumber(rows.length), detail: 'Filtered strength sessions' }},
        {{ label: 'Load', value: formatNumber(totalVolume / 1000, 1) + 'k lb', detail: 'Total filtered volume' }},
        {{ label: 'Estimator shift', value: (avgBias >= 0 ? '+' : '') + formatNumber(avgBias, 1) + ' lb', detail: 'Improved minus legacy' }},
        {{ label: 'Latest session', value: latestDate ? new Date(latestDate).toLocaleDateString() : '—', detail: 'Most recent filtered lift' }},
      ];
      const container = document.getElementById('strength-kpis');
      container.innerHTML = kpis.map((kpi) => `<div class=\"kpi\"><div class=\"label\">${{kpi.label}}</div><div class=\"value\">${{kpi.value}}</div><div class=\"detail\">${{kpi.detail}}</div></div>`).join('');
    }}

    function renderStrengthTable(rows) {{
      const summaries = (payload.strength.exercise_summaries || []).filter((row) => {{
        if (strengthState.selectedExercises.size && !strengthState.selectedExercises.has(row.exercise)) return false;
        if (strengthState.selectedMuscles.size && !(row.muscles || []).some((muscle) => strengthState.selectedMuscles.has(muscle))) return false;
        return true;
      }});
      const table = document.getElementById('strength-table');
      table.innerHTML = summaries.map((row) => `
        <tr>
          <td>${{row.exercise_label}}</td>
          <td>${{formatNumber(row.sessions)}}</td>
          <td>${{formatNumber(row.latest_improved_1rm, 1)}} lb</td>
          <td>${{formatNumber(row.peak_improved_1rm, 1)}} lb</td>
          <td>${{formatNumber(row.median_volume_lb, 0)}} lb</td>
          <td>${{(row.mean_estimator_delta_lb >= 0 ? '+' : '') + formatNumber(row.mean_estimator_delta_lb, 1)}} lb</td>
        </tr>
      `).join('');
    }}

    function renderStrengthNotes() {{
      const notes = payload.strength.notes || [];
      document.getElementById('strength-notes').innerHTML = notes.map((note) => `<p>${{note}}</p>`).join('');
    }}

    function muscleLoad(rows) {{
      const load = {{}};
      rows.forEach((row) => {{
        const mapping = payload.strength.exercise_muscles[row.exercise] || {{}};
        Object.entries(mapping).forEach(([muscle, weight]) => {{
          load[muscle] = (load[muscle] || 0) + (row.volume_lb || 0) * weight;
        }});
      }});
      return load;
    }}

    function exercisesForMuscle(muscle) {{
      return uniqueOrdered((payload.strength.exercise_order || []).filter((exercise) => Object.prototype.hasOwnProperty.call(payload.strength.exercise_muscles[exercise] || {{}}, muscle)));
    }}

    function colorForMuscle(muscle, loadMap) {{
      const maxLoad = Math.max(1, ...Object.values(loadMap));
      const value = loadMap[muscle] || 0;
      const alpha = 0.18 + 0.72 * Math.sqrt(value / maxLoad);
      return `rgba(186, 74, 48, ${{alpha.toFixed(3)}})`;
    }}

    function renderMuscleMap(rows) {{
      const loadMap = muscleLoad(rows);
      document.querySelectorAll('.muscle-region').forEach((region) => {{
        const muscle = region.dataset.muscle;
        region.style.fill = colorForMuscle(muscle, loadMap);
        region.classList.toggle('active', strengthState.selectedMuscles.has(muscle));
        region.onclick = () => {{
          if (strengthState.selectedMuscles.has(muscle)) {{
            strengthState.selectedMuscles.delete(muscle);
          }} else {{
            strengthState.selectedMuscles.add(muscle);
            exercisesForMuscle(muscle).forEach((exercise) => strengthState.selectedExercises.add(exercise));
          }}
          renderStrength();
        }};
      }});
    }}

    function renderStrength() {{
      renderExerciseChips();
      const rows = currentStrengthSessions();
      renderStrengthCharts(rows);
      renderStrengthKPIs(rows);
      renderStrengthTable(rows);
      renderStrengthNotes();
      renderMuscleMap(rows);
    }}

    function weekdayOrder() {{
      return ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
    }}

    function climbingKPIs(rows) {{
      const totalHours = rows.reduce((sum, row) => sum + (row.hours || 0), 0);
      const avgDuration = rows.length ? rows.reduce((sum, row) => sum + (row.duration_min || 0), 0) / rows.length : 0;
      const recent = rows.length ? rows[rows.length - 1] : null;
      const activeMonths = uniqueOrdered(rows.map((row) => row.year_month)).length;
      return [
        {{ label: 'Sessions', value: formatNumber(rows.length), detail: 'Filtered climbing sessions' }},
        {{ label: 'Hours', value: formatNumber(totalHours, 1), detail: 'Total duration' }},
        {{ label: 'Average session', value: formatNumber(avgDuration, 0) + ' min', detail: 'Mean session duration' }},
        {{ label: 'Active months', value: formatNumber(activeMonths), detail: recent ? 'Latest: ' + new Date(recent.date).toLocaleDateString() : 'No sessions' }},
      ];
    }}

    function renderClimbing() {{
      const rows = currentClimbingSessions().sort((a, b) => parseDate(a.date) - parseDate(b.date));
      const kpis = climbingKPIs(rows);
      document.getElementById('climbing-kpis').innerHTML = kpis.map((kpi) => `<div class=\"kpi\"><div class=\"label\">${{kpi.label}}</div><div class=\"value\">${{kpi.value}}</div><div class=\"detail\">${{kpi.detail}}</div></div>`).join('');

      const weeklySessions = rollingWeekly(rows, () => 1);
      const weeklyHours = rollingWeekly(rows, (row) => row.hours || 0);
      Plotly.newPlot('climbing-weekly', [
        {{
          x: weeklySessions.map(([week]) => week),
          y: weeklySessions.map(([, count]) => count),
          type: 'bar',
          name: 'Sessions',
          marker: {{ color: colors.bouldering }},
          yaxis: 'y',
        }},
        {{
          x: weeklyHours.map(([week]) => week),
          y: weeklyHours.map(([, hours]) => hours),
          type: 'scatter',
          mode: 'lines+markers',
          name: 'Hours',
          line: {{ color: colors.rock_climbing, width: 3 }},
          yaxis: 'y2',
        }}
      ], {{
        paper_bgcolor: 'rgba(0,0,0,0)',
        plot_bgcolor: 'rgba(0,0,0,0)',
        title: 'Weekly session rhythm',
        margin: {{ l: 56, r: 56, t: 50, b: 46 }},
        yaxis: {{ title: 'Sessions', gridcolor: 'rgba(29,41,53,0.08)' }},
        yaxis2: {{ title: 'Hours', overlaying: 'y', side: 'right' }},
      }}, {{ responsive: true, displaylogo: false }});

      Plotly.newPlot('climbing-scatter', ['bouldering', 'rock_climbing'].map((kind) => {{
        const filtered = rows.filter((row) => row.type === kind);
        return {{
          x: filtered.map((row) => row.duration_min),
          y: filtered.map((row) => row.calories),
          mode: 'markers',
          type: 'scatter',
          name: labelize(kind),
          marker: {{ color: colors[kind], size: 11, opacity: 0.7 }},
          text: filtered.map((row) => new Date(row.date).toLocaleDateString()),
          hovertemplate: '%{{text}}<br>%{{x:.0f}} min<br>%{{y:.0f}} cal<extra>' + labelize(kind) + '</extra>',
        }};
      }}), {{
        paper_bgcolor: 'rgba(0,0,0,0)',
        plot_bgcolor: 'rgba(0,0,0,0)',
        title: 'Session duration vs. calories',
        margin: {{ l: 56, r: 16, t: 50, b: 50 }},
        xaxis: {{ title: 'Duration (min)', gridcolor: 'rgba(29,41,53,0.08)' }},
        yaxis: {{ title: 'Calories', gridcolor: 'rgba(29,41,53,0.08)' }},
      }}, {{ responsive: true, displaylogo: false }});

      const months = uniqueOrdered(rows.map((row) => row.year_month)).sort();
      const weekdays = weekdayOrder();
      const matrix = weekdays.map(() => months.map(() => 0));
      rows.forEach((row) => {{
        const x = months.indexOf(row.year_month);
        const y = weekdays.indexOf(row.weekday);
        if (x >= 0 && y >= 0) matrix[y][x] += 1;
      }});
      Plotly.newPlot('climbing-heatmap', [{{
        z: matrix,
        x: months,
        y: weekdays,
        type: 'heatmap',
        colorscale: [
          [0, '#f6ecde'],
          [0.35, '#e8ba67'],
          [0.7, '#cf7442'],
          [1, '#7d2f1d'],
        ],
        hovertemplate: '%{{y}} / %{{x}}<br>%{{z}} sessions<extra></extra>',
      }}], {{
        paper_bgcolor: 'rgba(0,0,0,0)',
        plot_bgcolor: 'rgba(0,0,0,0)',
        title: 'Month x weekday frequency',
        margin: {{ l: 56, r: 16, t: 46, b: 64 }},
      }}, {{ responsive: true, displaylogo: false }});

      document.getElementById('climbing-notes').innerHTML = (payload.climbing.notes || []).map((note) => `<p>${{note}}</p>`).join('');
    }}

    function setActiveTab(nextTab) {{
      document.querySelectorAll('.tab-button').forEach((button) => button.classList.toggle('active', button.dataset.tab === nextTab));
      document.querySelectorAll('.tab-panel').forEach((panel) => panel.classList.toggle('active', panel.id === nextTab));
    }}

    document.querySelectorAll('.tab-button').forEach((button) => {{
      button.onclick = () => setActiveTab(button.dataset.tab);
    }});

    document.getElementById('strength-range').onchange = (event) => {{
      strengthState.range = event.target.value;
      renderStrength();
    }};
    document.getElementById('strength-metric').onchange = (event) => {{
      strengthState.metric = event.target.value;
      renderStrength();
    }};
    document.getElementById('strength-reset').onclick = () => {{
      strengthState.selectedExercises = new Set((payload.strength.exercise_order || []).slice(0, 6));
      strengthState.selectedMuscles.clear();
      strengthState.range = '365';
      strengthState.metric = 'improved';
      document.getElementById('strength-range').value = '365';
      document.getElementById('strength-metric').value = 'improved';
      renderStrength();
    }};
    document.getElementById('climbing-range').onchange = (event) => {{
      climbingState.range = event.target.value;
      renderClimbing();
    }};
    document.getElementById('climbing-type').onchange = (event) => {{
      climbingState.type = event.target.value;
      renderClimbing();
    }};

    renderStrength();
    renderClimbing();
  </script>
</body>
</html>
"""


def _front_body_svg() -> str:
    """Traced from a real front/back muscle-map reference image (cv2 color
    segmentation + contour extraction -- see PR description/commit message
    for the source and method), not hand-drawn primitives. Each blob's
    anatomical label was assigned by inspecting its position against the
    reference image, then verified by rendering the result."""
    return """
<svg viewBox="0 0 362 484" aria-label="Front muscle sketch">
  <path d="M 198.2,16.8 L 186.8,21.5 L 181.2,27.8 L 179.5,33.0 L 179.5,52.8 L 182.0,60.5 L 186.5,66.2 L 185.8,78.8 L 178.8,84.2 L 158.2,93.8 L 148.0,105.2 L 143.8,115.8 L 140.8,134.5 L 135.8,147.8 L 120.2,174.2 L 109.0,202.8 L 103.0,211.2 L 91.8,219.2 L 86.8,228.5 L 87.0,233.0 L 84.5,238.0 L 88.0,242.8 L 96.5,246.2 L 104.8,247.5 L 107.5,245.5 L 115.0,231.2 L 118.8,217.2 L 135.5,194.8 L 147.5,168.2 L 162.2,145.2 L 165.0,144.8 L 170.5,176.0 L 160.5,204.5 L 155.8,233.0 L 154.5,271.5 L 156.8,312.2 L 155.8,339.8 L 151.5,357.2 L 152.8,390.8 L 158.5,432.5 L 155.5,446.2 L 151.2,451.0 L 145.0,453.5 L 142.5,458.5 L 147.0,462.0 L 157.5,462.2 L 170.8,460.0 L 174.2,455.8 L 175.5,437.8 L 173.2,411.0 L 179.0,375.5 L 180.2,341.2 L 195.2,273.5 L 198.5,243.8 L 201.8,242.2 L 206.8,279.0 L 221.5,347.2 L 221.8,376.8 L 227.5,408.8 L 227.5,426.2 L 225.5,437.2 L 226.8,455.8 L 231.0,460.2 L 252.8,462.2 L 256.8,460.8 L 258.8,457.5 L 255.8,453.2 L 249.8,451.0 L 245.2,445.8 L 242.8,433.0 L 248.0,392.8 L 249.8,363.8 L 249.0,353.0 L 245.2,340.2 L 244.2,314.5 L 246.2,277.2 L 245.2,234.2 L 241.0,206.5 L 230.2,175.2 L 236.2,144.8 L 238.5,145.0 L 255.0,170.8 L 265.5,195.0 L 282.8,218.2 L 286.0,231.5 L 293.5,245.5 L 298.5,247.5 L 308.0,245.0 L 313.5,242.2 L 316.2,237.0 L 314.0,233.2 L 314.2,228.5 L 308.5,218.2 L 296.0,209.2 L 290.0,199.2 L 281.2,176.0 L 267.5,152.5 L 261.5,138.8 L 258.0,118.8 L 252.0,103.8 L 241.8,93.0 L 224.0,85.2 L 215.5,79.2 L 214.2,66.5 L 220.2,56.0 L 221.0,32.2 L 214.8,22.0 L 205.5,17.5 Z" class="silhouette"></path>
  <path d="M 179.5,93.0 L 172.5,91.2 L 166.0,91.8 L 157.0,96.8 L 152.5,101.8 L 148.2,109.8 L 144.8,124.2 L 161.5,105.5 Z" class="muscle-region" data-muscle="shoulders"></path>
  <path d="M 220.8,93.2 L 237.0,103.5 L 255.8,123.8 L 255.0,116.8 L 252.0,108.5 L 244.2,97.0 L 233.5,91.5 L 228.5,91.2 Z" class="muscle-region" data-muscle="shoulders"></path>
  <path d="M 184.2,98.5 L 178.5,100.5 L 166.0,108.2 L 162.5,113.8 L 162.2,119.5 L 166.5,128.8 L 171.5,132.8 L 178.5,135.0 L 187.5,135.5 L 195.2,131.5 L 197.8,127.8 L 198.0,103.5 L 195.2,99.2 Z" class="muscle-region" data-muscle="chest"></path>
  <path d="M 204.5,99.8 L 202.8,104.2 L 202.8,125.5 L 204.0,129.8 L 213.0,135.5 L 225.0,134.5 L 232.2,130.8 L 236.2,125.8 L 238.5,119.5 L 238.5,114.5 L 236.2,109.8 L 231.2,105.5 L 218.2,99.0 Z" class="muscle-region" data-muscle="chest"></path>
  <path d="M 159.5,120.8 L 149.5,129.0 L 142.2,139.5 L 138.5,154.5 L 138.5,159.8 L 140.5,163.8 L 143.8,163.5 L 150.2,157.8 L 159.5,143.2 L 160.5,135.5 Z" class="muscle-region" data-muscle="biceps"></path>
  <path d="M 241.2,120.8 L 240.2,134.2 L 242.0,144.5 L 251.2,158.5 L 256.8,163.2 L 260.2,163.8 L 262.2,159.8 L 262.2,154.2 L 258.5,139.2 L 251.5,129.2 Z" class="muscle-region" data-muscle="biceps"></path>
  <path d="M 191.2,138.8 L 185.8,140.8 L 182.0,147.0 L 183.2,163.8 L 182.5,186.2 L 186.5,202.0 L 191.8,210.2 L 198.0,214.8 L 198.5,142.0 L 196.5,139.2 Z" class="muscle-region" data-muscle="core"></path>
  <path d="M 209.2,138.8 L 204.5,139.2 L 202.2,142.0 L 203.0,215.0 L 209.2,210.0 L 213.5,203.5 L 218.2,187.2 L 218.5,145.8 L 215.8,141.2 Z" class="muscle-region" data-muscle="core"></path>
  <path d="M 176.8,143.8 L 174.5,146.0 L 172.2,152.2 L 173.0,175.2 L 170.0,186.0 L 172.8,190.5 L 178.8,195.5 L 177.5,189.2 L 178.5,162.2 Z" class="muscle-region" data-muscle="obliques"></path>
  <path d="M 224.0,143.8 L 224.0,154.0 L 222.2,163.2 L 223.5,180.0 L 222.2,195.5 L 226.5,192.2 L 230.8,186.2 L 227.8,175.8 L 228.5,152.2 L 226.5,146.2 Z" class="muscle-region" data-muscle="obliques"></path>
  <path d="M 165.5,235.2 L 162.2,242.8 L 160.2,255.2 L 160.2,285.0 L 163.2,297.8 L 166.5,304.5 L 170.8,309.0 L 174.5,309.5 L 178.8,307.2 L 184.5,299.8 L 188.5,272.0 L 187.2,264.0 L 183.2,254.5 L 169.5,237.8 Z" class="muscle-region" data-muscle="quads"></path>
  <path d="M 235.5,235.2 L 231.2,237.8 L 217.2,255.0 L 212.5,270.0 L 212.2,276.2 L 216.2,299.5 L 222.0,307.2 L 226.5,309.5 L 231.0,308.2 L 234.8,303.8 L 240.5,285.5 L 240.5,254.0 L 237.5,238.5 Z" class="muscle-region" data-muscle="quads"></path>
  <path d="M 138.8,167.0 L 130.5,168.5 L 124.5,174.8 L 116.2,194.5 L 114.2,201.8 L 114.8,208.0 L 118.2,207.5 L 124.8,201.5 L 138.8,182.2 L 141.2,173.0 L 141.0,169.5 Z" class="muscle-region" data-muscle="forearms"></path>
  <path d="M 262.0,167.0 L 260.2,168.8 L 260.0,174.5 L 262.5,182.8 L 276.2,201.5 L 282.2,207.2 L 286.0,208.0 L 286.8,204.8 L 285.2,197.0 L 275.5,173.2 L 269.8,168.2 Z" class="muscle-region" data-muscle="forearms"></path>
  <path d="M 166.0,196.0 L 166.5,204.5 L 170.8,209.2 L 178.8,213.5 L 188.8,215.2 L 181.2,202.8 L 174.8,200.0 L 168.0,195.0 Z" class="muscle-region" data-muscle="hip_flexors"></path>
  <path d="M 234.5,196.2 L 233.0,195.0 L 219.2,203.0 L 212.2,215.5 L 222.8,213.2 L 230.5,209.0 L 235.2,202.2 Z" class="muscle-region" data-muscle="hip_flexors"></path>
  <path d="M 163.5,207.2 L 161.8,208.2 L 159.8,217.0 L 157.5,246.5 L 159.2,244.2 L 167.5,217.5 L 167.8,211.8 Z" class="muscle-region" data-muscle="hip_flexors"></path>
  <path d="M 237.2,207.5 L 233.0,212.0 L 233.5,217.2 L 241.5,241.5 L 242.2,247.5 L 243.8,249.0 L 241.8,220.2 L 239.0,209.0 Z" class="muscle-region" data-muscle="hip_flexors"></path>
  <path d="M 228.8,214.2 L 213.8,226.2 L 206.0,236.0 L 204.0,242.0 L 206.0,263.0 L 208.2,268.8 L 213.8,254.0 L 226.2,238.8 L 229.5,227.2 L 230.0,216.0 Z" class="muscle-region" data-muscle="adductors"></path>
  <path d="M 171.5,214.5 L 171.2,225.0 L 174.5,238.8 L 187.2,254.2 L 193.2,271.8 L 196.0,253.8 L 195.2,236.5 L 189.8,228.8 L 178.8,218.5 Z" class="muscle-region" data-muscle="adductors"></path>
</svg>
"""


def _back_body_svg() -> str:
    """See _front_body_svg -- same traced-from-reference approach.

    The forearm regions were re-traced from assets/muscle_map/body_reference.png
    (cv2 colour segmentation on the highlighted region, upscale + blur +
    rethreshold + approxPolyDP, then mapped from image to viewBox coordinates
    via the silhouette bounding boxes). An earlier version of these two paths
    was hand-drawn from silhouette coordinates because the reference image had
    been lost, and it showed -- crude polygons spanning the arm's whole
    diagonal sweep rather than a muscle band."""
    return """
<svg viewBox="0 0 345 484" aria-label="Back muscle sketch">
  <path d="M 157.2,16.8 L 144.5,22.2 L 138.5,32.0 L 137.8,49.2 L 139.5,58.2 L 145.5,66.5 L 144.2,79.2 L 137.0,84.5 L 118.2,92.8 L 109.0,101.5 L 102.8,114.8 L 99.5,134.8 L 92.8,151.2 L 81.0,170.0 L 66.2,205.2 L 61.2,211.8 L 51.5,218.0 L 45.2,229.0 L 45.8,233.0 L 43.5,238.8 L 45.8,242.0 L 54.2,246.0 L 63.5,247.5 L 66.2,245.5 L 74.0,230.5 L 77.0,218.0 L 94.8,193.8 L 106.0,168.5 L 121.0,145.2 L 123.5,144.8 L 129.2,175.8 L 117.8,210.0 L 114.8,230.2 L 113.0,261.5 L 115.5,313.2 L 114.5,339.8 L 111.5,348.2 L 110.0,360.8 L 111.8,394.0 L 117.0,432.0 L 114.5,445.5 L 109.8,451.2 L 103.2,453.8 L 100.8,458.0 L 105.8,462.0 L 115.8,462.2 L 130.0,459.8 L 133.0,455.5 L 134.2,437.5 L 132.0,413.0 L 137.2,379.0 L 138.2,346.0 L 153.0,278.5 L 157.2,243.2 L 160.0,242.2 L 166.5,284.8 L 179.5,341.5 L 180.5,377.2 L 186.5,411.5 L 184.0,437.8 L 185.5,456.0 L 189.8,460.2 L 211.2,462.2 L 215.8,460.5 L 217.2,457.0 L 204.0,446.0 L 201.2,433.0 L 207.0,389.8 L 208.2,358.8 L 204.0,340.0 L 203.0,303.8 L 205.2,256.0 L 203.0,226.0 L 200.0,207.8 L 189.0,175.8 L 194.8,144.5 L 197.2,145.0 L 213.8,171.0 L 223.8,194.2 L 241.0,217.5 L 244.8,231.5 L 252.0,245.2 L 255.0,247.5 L 266.0,245.2 L 272.2,242.2 L 275.0,237.2 L 272.8,233.2 L 273.0,228.5 L 267.8,219.0 L 255.0,209.5 L 249.2,200.2 L 239.0,173.8 L 221.0,141.0 L 216.0,116.2 L 209.2,101.5 L 199.5,92.5 L 181.8,84.8 L 173.8,78.8 L 173.2,66.0 L 177.8,59.8 L 180.0,48.0 L 179.0,30.0 L 171.2,20.5 Z" class="silhouette"></path>
  <path d="M 156.5,76.2 L 121.5,94.5 L 123.8,96.5 L 140.8,103.0 L 147.2,130.2 L 155.0,146.8 L 156.5,122.2 Z" class="muscle-region" data-muscle="traps"></path>
  <path d="M 161.8,76.5 L 161.5,113.2 L 163.0,146.8 L 170.5,131.8 L 177.5,103.2 L 196.8,94.5 Z" class="muscle-region" data-muscle="traps"></path>
  <path d="M 136.0,104.8 L 116.5,97.5 L 112.2,101.5 L 108.0,108.5 L 105.0,116.0 L 103.5,125.5 L 122.2,117.5 L 131.8,110.5 Z" class="muscle-region" data-muscle="shoulders"></path>
  <path d="M 182.0,105.0 L 187.5,111.5 L 196.0,117.5 L 214.2,124.8 L 210.8,109.8 L 206.5,102.2 L 201.8,97.5 Z" class="muscle-region" data-muscle="shoulders"></path>
  <path d="M 138.2,111.8 L 125.2,121.5 L 125.2,140.5 L 127.5,160.2 L 130.0,169.8 L 134.5,177.2 L 140.2,171.2 L 146.8,156.2 L 152.2,149.2 L 143.8,130.5 Z" class="muscle-region" data-muscle="lats"></path>
  <path d="M 180.5,111.5 L 174.2,131.2 L 166.2,149.8 L 171.5,156.2 L 177.5,170.5 L 183.8,177.2 L 188.0,170.2 L 190.5,162.2 L 193.2,136.2 L 193.2,122.0 Z" class="muscle-region" data-muscle="lats"></path>
  <path d="M 138.2,193.2 L 134.5,194.0 L 131.5,197.8 L 126.2,211.8 L 125.5,228.8 L 127.2,234.8 L 129.5,236.5 L 138.8,236.5 L 150.0,234.2 L 154.2,230.8 L 156.5,224.8 L 156.2,217.8 L 153.5,210.0 L 143.5,197.0 Z" class="muscle-region" data-muscle="glutes"></path>
  <path d="M 180.0,193.2 L 174.2,197.5 L 164.5,210.5 L 162.2,216.2 L 162.0,226.0 L 165.0,232.2 L 169.5,234.5 L 179.2,236.5 L 188.5,236.5 L 191.0,234.5 L 192.5,230.0 L 191.5,210.2 L 186.2,197.0 L 183.2,193.8 Z" class="muscle-region" data-muscle="glutes"></path>
  <path d="M 120.2,124.2 L 108.0,129.0 L 102.5,135.8 L 100.0,141.5 L 98.2,151.0 L 99.8,151.5 L 107.8,144.8 L 108.0,157.8 L 113.8,152.5 L 119.5,142.5 L 121.2,136.0 Z" class="muscle-region" data-muscle="triceps"></path>
  <path d="M 198.0,124.2 L 197.2,136.8 L 198.5,142.8 L 204.5,152.5 L 210.5,158.0 L 210.5,144.2 L 218.2,151.2 L 219.8,151.0 L 217.5,139.5 L 213.8,132.8 L 209.5,128.5 Z" class="muscle-region" data-muscle="triceps"></path>
  <path d="M 135.0,240.2 L 130.5,240.8 L 125.8,245.2 L 118.2,258.0 L 120.5,305.5 L 123.0,319.2 L 126.8,324.2 L 131.8,323.5 L 135.2,320.5 L 139.5,302.2 L 139.2,263.8 L 137.2,245.5 Z" class="muscle-region" data-muscle="hamstrings"></path>
  <path d="M 183.0,240.2 L 180.5,248.2 L 178.5,271.5 L 178.8,301.2 L 182.8,320.0 L 185.5,323.0 L 191.2,324.2 L 195.0,319.5 L 197.2,308.2 L 199.8,257.5 L 192.0,244.8 L 187.8,240.8 Z" class="muscle-region" data-muscle="hamstrings"></path>
  <path d="M 157.5,64.2 L 147.8,65.2 L 147.5,77.2 L 155.2,73.2 Z" class="muscle-region" data-muscle="back"></path>
  <path d="M 160.5,64.2 L 162.5,72.8 L 171.0,77.2 L 170.8,65.8 L 167.8,64.2 Z" class="muscle-region" data-muscle="back"></path>
  <path d="M 154.8,154.2 L 150.8,158.2 L 145.2,171.2 L 137.2,183.0 L 151.0,195.0 L 155.2,197.0 L 156.0,155.0 Z" class="muscle-region" data-muscle="obliques"></path>
  <path d="M 162.8,154.2 L 162.2,196.2 L 164.0,196.8 L 178.5,186.0 L 181.2,182.2 L 173.2,171.8 L 167.5,158.2 Z" class="muscle-region" data-muscle="obliques"></path>
  <path d="M 187.5,180.0 L 181.8,189.2 L 189.2,195.0 L 195.0,207.5 L 197.0,220.5 L 197.0,232.0 L 192.8,240.8 L 202.2,253.0 L 202.5,244.8 L 198.5,211.2 L 191.5,187.8 Z" class="muscle-region" data-muscle="glutes"></path>
  <path d="M 130.8,180.5 L 127.2,186.5 L 120.0,210.2 L 117.0,230.8 L 115.8,254.0 L 125.5,240.8 L 122.0,235.0 L 121.2,230.2 L 121.8,214.8 L 123.8,206.2 L 129.8,194.0 L 136.8,189.0 Z" class="muscle-region" data-muscle="glutes"></path>
  <path d="M 154.2,236.8 L 140.5,239.8 L 144.0,277.5 L 143.5,305.5 L 147.8,292.5 L 152.8,266.5 L 155.5,239.8 Z" class="muscle-region" data-muscle="hamstrings"></path>
  <path d="M 164.2,236.8 L 177.9,239.8 L 174.4,277.5 L 174.9,305.5 L 170.6,292.5 L 165.6,266.5 L 162.9,239.8 Z" class="muscle-region" data-muscle="hamstrings"></path>
  <path d="M 98.3,166.6 L 95.5,166.3 L 91.2,167.5 L 84.4,173.1 L 76.1,190.6 L 73.1,203.2 L 73.1,207.0 L 74.6,209.0 L 78.2,207.5 L 83.3,203.1 L 94.5,189.4 L 98.9,182.4 L 101.1,175.4 L 100.8,169.8 Z" class="muscle-region" data-muscle="forearms"></path>
  <path d="M 221.0,166.6 L 218.5,169.5 L 218.5,175.6 L 220.1,182.1 L 235.7,202.8 L 242.3,208.4 L 245.0,208.7 L 246.2,206.4 L 245.2,196.8 L 237.0,176.2 L 234.4,172.5 L 228.7,167.8 L 224.1,166.3 Z" class="muscle-region" data-muscle="forearms"></path>
  <path d="M 118.0,344.0 L 113.0,358.0 L 112.0,378.0 L 116.0,398.0 L 122.0,414.0 L 129.0,406.0 L 134.0,384.0 L 136.0,360.0 L 132.0,346.0 Z" class="muscle-region" data-muscle="calves"></path>
  <path d="M 200.4,344.0 L 205.4,358.0 L 206.4,378.0 L 202.4,398.0 L 196.4,414.0 L 189.4,406.0 L 184.4,384.0 L 182.4,360.0 L 186.4,346.0 Z" class="muscle-region" data-muscle="calves"></path>
</svg>
"""


def write_activity_explorer_html(output_path: str | Path, store: CuratedDataStore) -> Path:
    """Build and write the standalone explorer HTML to disk."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_activity_explorer_html(store), encoding="utf-8")
    return path