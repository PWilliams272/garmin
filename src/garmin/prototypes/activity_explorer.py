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
    .body-label {{ font-size: 12px; fill: var(--muted); letter-spacing: 0.08em; text-transform: uppercase; }}

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
<svg viewBox="0 -24 362 508" aria-label="Front muscle sketch">
  <text x="181" y="-8" text-anchor="middle" class="body-label">Front</text>
  <path d="M 197,17 L 187,21 L 182,26 L 180,31 L 180,55 L 183,62 L 187,66 L 186,79 L 176,86 L 161,92 L 157,92 L 157,94 L 150,101 L 145,111 L 141,135 L 134,152 L 121,172 L 116,188 L 108,205 L 101,213 L 91,220 L 87,228 L 85,239 L 89,243 L 104,247 L 107,245 L 115,229 L 118,216 L 123,213 L 123,209 L 135,194 L 145,170 L 160,147 L 165,146 L 171,176 L 161,202 L 156,231 L 155,283 L 157,316 L 156,340 L 152,353 L 152,381 L 158,424 L 158,435 L 155,447 L 150,452 L 145,453 L 142,458 L 146,461 L 162,461 L 173,457 L 175,438 L 173,433 L 172,411 L 178,377 L 179,341 L 184,325 L 185,311 L 193,281 L 197,246 L 202,245 L 207,280 L 214,306 L 217,329 L 221,340 L 222,379 L 228,409 L 228,425 L 226,435 L 227,456 L 229,459 L 255,461 L 258,457 L 255,453 L 250,452 L 246,448 L 242,434 L 247,395 L 249,362 L 247,346 L 244,340 L 243,314 L 245,287 L 244,230 L 240,205 L 230,177 L 230,167 L 235,146 L 239,146 L 255,170 L 265,194 L 281,214 L 286,231 L 293,245 L 298,247 L 314,241 L 315,235 L 314,229 L 308,218 L 295,209 L 286,193 L 280,174 L 260,138 L 257,117 L 253,106 L 243,94 L 224,86 L 214,78 L 213,66 L 217,62 L 220,53 L 220,31 L 218,26 L 207,18 Z" class="silhouette"></path>
  <path d="M 179,93 L 169,91 L 164,92 L 156,97 L 150,105 L 147,112 L 145,124 L 164,102 Z" class="muscle-region" data-muscle="shoulders"></path>
  <path d="M 221,93 L 236,102 L 255,124 L 255,119 L 251,107 L 243,96 L 231,91 Z" class="muscle-region" data-muscle="shoulders"></path>
  <path d="M 195,99 L 181,99 L 168,106 L 164,110 L 162,114 L 162,119 L 164,125 L 169,131 L 175,134 L 188,135 L 196,130 L 197,101 Z" class="muscle-region" data-muscle="chest"></path>
  <path d="M 205,99 L 203,101 L 204,130 L 212,135 L 225,134 L 231,131 L 236,125 L 238,119 L 238,114 L 236,110 L 229,104 L 219,99 Z" class="muscle-region" data-muscle="chest"></path>
  <path d="M 159,120 L 148,130 L 142,139 L 138,156 L 140,163 L 145,162 L 151,156 L 159,143 Z" class="muscle-region" data-muscle="biceps"></path>
  <path d="M 241,120 L 240,135 L 242,145 L 249,156 L 255,162 L 260,163 L 262,155 L 259,141 L 252,130 Z" class="muscle-region" data-muscle="biceps"></path>
  <path d="M 186,140 L 182,145 L 182,184 L 184,196 L 188,205 L 198,215 L 198,141 L 196,139 Z" class="muscle-region" data-muscle="core"></path>
  <path d="M 212,139 L 204,139 L 202,141 L 202,214 L 204,214 L 212,205 L 216,196 L 218,185 L 218,145 Z" class="muscle-region" data-muscle="core"></path>
  <path d="M 177,143 L 174,146 L 172,152 L 173,176 L 170,183 L 170,187 L 178,195 L 178,161 L 176,154 Z" class="muscle-region" data-muscle="obliques"></path>
  <path d="M 223,143 L 223,192 L 221,196 L 230,187 L 230,183 L 227,176 L 228,152 L 226,146 Z" class="muscle-region" data-muscle="obliques"></path>
  <path d="M 165,235 L 162,242 L 160,254 L 160,285 L 166,304 L 171,309 L 174,309 L 178,307 L 184,299 L 188,271 L 184,256 L 171,240 Z" class="muscle-region" data-muscle="quads"></path>
  <path d="M 235,235 L 218,253 L 214,261 L 212,271 L 216,299 L 218,303 L 226,309 L 229,309 L 234,304 L 240,286 L 240,253 L 237,238 Z" class="muscle-region" data-muscle="quads"></path>
  <path d="M 139,167 L 133,167 L 129,169 L 124,175 L 114,201 L 115,208 L 118,207 L 127,198 L 137,184 L 140,176 Z" class="muscle-region" data-muscle="forearms"></path>
  <path d="M 261,167 L 260,177 L 265,187 L 282,207 L 286,207 L 285,197 L 275,173 L 267,167 Z" class="muscle-region" data-muscle="forearms"></path>
  <path d="M 166,196 L 166,204 L 172,210 L 178,213 L 188,215 L 180,202 L 174,200 L 168,195 Z" class="muscle-region" data-muscle="hip_flexors"></path>
  <path d="M 234,196 L 232,195 L 226,200 L 220,202 L 212,215 L 222,213 L 229,209 L 234,204 Z" class="muscle-region" data-muscle="hip_flexors"></path>
  <path d="M 163,207 L 160,214 L 160,224 L 158,229 L 158,246 L 160,236 L 167,218 L 167,211 Z" class="muscle-region" data-muscle="hip_flexors"></path>
  <path d="M 237,207 L 233,211 L 233,216 L 240,235 L 243,249 L 241,219 Z" class="muscle-region" data-muscle="hip_flexors"></path>
  <path d="M 171,214 L 171,225 L 174,238 L 188,255 L 193,271 L 193,261 L 195,257 L 195,237 L 187,226 L 177,217 Z" class="muscle-region" data-muscle="adductors"></path>
  <path d="M 229,214 L 222,218 L 207,233 L 204,240 L 206,263 L 208,269 L 209,262 L 213,253 L 226,238 L 229,227 Z" class="muscle-region" data-muscle="adductors"></path>
</svg>
"""


def _back_body_svg() -> str:
    """See _front_body_svg -- same traced-from-reference approach."""
    return """
<svg viewBox="0 -24 345 508" aria-label="Back muscle sketch">
  <text x="172" y="-8" text-anchor="middle" class="body-label">Back</text>
  <path d="M 155,17 L 143,23 L 138,34 L 139,57 L 145,65 L 145,78 L 138,84 L 119,92 L 108,102 L 102,117 L 100,134 L 95,147 L 77,178 L 75,187 L 66,206 L 49,221 L 45,229 L 44,240 L 53,245 L 61,247 L 65,246 L 73,231 L 78,214 L 96,190 L 103,171 L 120,146 L 124,146 L 129,168 L 129,177 L 120,201 L 115,228 L 113,255 L 116,303 L 115,339 L 111,350 L 110,360 L 112,396 L 117,434 L 115,437 L 115,445 L 109,452 L 104,453 L 101,456 L 101,459 L 104,461 L 120,461 L 130,459 L 132,456 L 133,435 L 131,427 L 131,408 L 137,374 L 138,339 L 142,328 L 144,310 L 151,285 L 156,247 L 161,246 L 167,287 L 180,342 L 181,381 L 187,412 L 186,433 L 184,437 L 186,457 L 191,460 L 213,461 L 216,459 L 216,456 L 203,446 L 203,440 L 200,433 L 207,380 L 207,353 L 203,340 L 202,317 L 204,282 L 203,233 L 199,206 L 188,176 L 194,146 L 199,147 L 212,167 L 222,191 L 242,218 L 245,232 L 252,245 L 257,247 L 268,243 L 274,244 L 274,239 L 276,238 L 272,233 L 272,228 L 268,220 L 252,207 L 244,191 L 239,175 L 222,146 L 217,131 L 216,118 L 211,105 L 200,93 L 179,84 L 172,77 L 172,66 L 176,62 L 179,62 L 177,61 L 179,51 L 179,32 L 176,25 L 166,18 Z" class="silhouette"></path>
  <path d="M 156,76 L 132,90 L 121,94 L 123,96 L 137,100 L 141,103 L 147,130 L 155,147 Z" class="muscle-region" data-muscle="traps"></path>
  <path d="M 161,76 L 163,146 L 171,128 L 177,102 L 194,96 L 196,94 L 182,88 Z" class="muscle-region" data-muscle="traps"></path>
  <path d="M 136,105 L 117,97 L 115,98 L 106,112 L 103,125 L 122,117 Z" class="muscle-region" data-muscle="shoulders"></path>
  <path d="M 181,105 L 187,111 L 199,119 L 209,122 L 213,125 L 214,124 L 211,111 L 206,102 L 201,97 Z" class="muscle-region" data-muscle="shoulders"></path>
  <path d="M 138,111 L 125,121 L 127,159 L 130,170 L 134,177 L 141,169 L 147,154 L 152,149 L 142,128 Z" class="muscle-region" data-muscle="lats"></path>
  <path d="M 180,111 L 174,132 L 166,149 L 171,155 L 177,170 L 183,177 L 187,171 L 190,162 L 192,145 L 192,121 Z" class="muscle-region" data-muscle="lats"></path>
  <path d="M 138,193 L 132,196 L 126,211 L 125,227 L 126,232 L 129,236 L 149,234 L 154,230 L 156,225 L 156,218 L 152,208 L 146,200 Z" class="muscle-region" data-muscle="glutes"></path>
  <path d="M 179,193 L 174,197 L 165,209 L 162,216 L 162,227 L 166,233 L 178,236 L 188,236 L 190,235 L 192,230 L 191,210 L 186,197 L 182,193 Z" class="muscle-region" data-muscle="glutes"></path>
  <path d="M 120,124 L 110,127 L 103,134 L 100,141 L 98,151 L 107,144 L 107,157 L 113,152 L 119,142 Z" class="muscle-region" data-muscle="triceps"></path>
  <path d="M 197,124 L 198,141 L 210,158 L 210,143 L 219,151 L 217,139 L 209,128 Z" class="muscle-region" data-muscle="triceps"></path>
  <path d="M 135,240 L 131,240 L 123,248 L 118,257 L 120,303 L 122,316 L 125,323 L 131,323 L 135,320 L 139,303 L 139,266 L 137,246 Z" class="muscle-region" data-muscle="hamstrings"></path>
  <path d="M 182,240 L 178,272 L 178,295 L 182,319 L 186,323 L 190,324 L 193,322 L 195,318 L 197,307 L 199,256 L 193,246 L 188,241 Z" class="muscle-region" data-muscle="hamstrings"></path>
  <path d="M 154,154 L 151,157 L 146,170 L 140,179 L 137,181 L 137,183 L 155,197 Z" class="muscle-region" data-muscle="back"></path>
  <path d="M 162,154 L 162,196 L 164,196 L 181,182 L 172,171 L 167,158 Z" class="muscle-region" data-muscle="back"></path>
  <path d="M 130,179 L 119,214 L 116,238 L 116,253 L 125,240 L 121,234 L 120,228 L 123,205 L 127,196 L 134,189 L 137,189 L 132,184 Z" class="muscle-region" data-muscle="obliques"></path>
  <path d="M 187,180 L 181,189 L 185,190 L 189,194 L 195,207 L 197,219 L 197,232 L 196,236 L 192,240 L 199,248 L 202,254 L 198,211 L 191,188 L 189,186 L 190,185 L 188,183 L 189,182 Z" class="muscle-region" data-muscle="obliques"></path>
  <path d="M 140,239 L 144,272 L 143,308 L 152,267 L 151,264 L 153,260 L 152,256 L 154,252 L 154,240 L 162,239 L 165,250 L 164,256 L 166,258 L 165,263 L 167,276 L 175,310 L 174,264 L 177,240 L 164,237 L 163,240 L 155,239 L 154,236 L 151,238 Z" class="muscle-region" data-muscle="glutes"></path>
  <path d="M 124,343 L 118,345 L 113,351 L 112,369 L 114,373 L 113,384 L 115,387 L 114,394 L 116,399 L 115,404 L 117,407 L 116,412 L 117,411 L 120,416 L 123,416 L 126,411 L 130,382 L 134,371 L 135,373 L 135,354 L 133,353 L 131,347 Z" class="muscle-region" data-muscle="calves"></path>
  <path d="M 193,343 L 187,346 L 184,352 L 182,364 L 188,385 L 191,410 L 195,416 L 198,416 L 200,410 L 202,409 L 201,401 L 203,400 L 202,392 L 204,391 L 203,379 L 205,379 L 205,354 L 203,351 L 204,349 L 203,350 L 198,344 Z" class="muscle-region" data-muscle="calves"></path>
</svg>
"""


def write_activity_explorer_html(output_path: str | Path, store: CuratedDataStore) -> Path:
    """Build and write the standalone explorer HTML to disk."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_activity_explorer_html(store), encoding="utf-8")
    return path