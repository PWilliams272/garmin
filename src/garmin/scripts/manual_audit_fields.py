"""Report which Garmin response fields we persist and which we discard.

Every puller maps a hand-picked subset of each endpoint's response. That subset
was chosen incrementally and has drifted behind what Garmin actually returns --
this repo has more than once *inferred* something (hand-logged sessions, the
device boundary) that Garmin was reporting outright in a field nobody mapped.

This probes each endpoint live, flattens the first record, and checks whether
each field name appears anywhere in the package source. Two caveats on reading
the output:

- Keys built with f-strings (``hrTimeInZone_{n}``, ``zone{n}Floor``) look
  discarded because the literal never appears in source. They are not.
- Identifiers and privacy/visibility flags are filtered out by default; they
  are noise, not data.

Run::

    python -m garmin.scripts.manual_audit_fields --out audit.json
"""

from dotenv import load_dotenv

load_dotenv()

import argparse  # noqa: E402
import json  # noqa: E402
import pathlib  # noqa: E402
import re  # noqa: E402
from datetime import date, timedelta  # noqa: E402

#: Field-name fragments that are identifiers or UI/privacy flags, not data.
NOISE = re.compile(
    r"(uuid|Uuid|UUID|Visibility|privacy|Privacy|ownerId|ownerDisplay|ownerProfile"
    r"|ownerFullName|userProfile|imageUrl|ImageUrl|Pk$|userRoles|userPro)"
)

#: Keys assembled with f-strings, so the literal never appears in source.
FSTRING_BUILT = re.compile(r"^(hrTimeInZone_\d|zone\dFloor)$")


def flatten(obj, prefix: str = "") -> dict:
    """Flatten one record to ``{dotted.path: sample value}``.

    Lists collapse to their first element -- enough to enumerate the schema.
    """
    out: dict = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            out.update(flatten(value, f"{prefix}{key}."))
    elif isinstance(obj, list):
        if obj:
            out.update(flatten(obj[0], prefix))
    else:
        out[prefix.rstrip(".")] = obj
    return out


def probes(end: str, start: str) -> dict:
    """Endpoint name to URL, covering every endpoint the pullers use."""
    return {
        "weight": f"/weight-service/weight/range/{start}/{end}?includeAll=true",
        "steps": f"usersummary-service/stats/daily/{start}/{end}?statsType=STEPS",
        "sleep": f"/sleep-service/stats/sleep/daily/{start}/{end}",
        "heart_rate": f"/usersummary-service/stats/heartRate/daily/{start}/{end}",
        "stress": f"/usersummary-service/stats/stress/daily/{start}/{end}",
        "body_battery": f"/usersummary-service/stats/bodybattery/daily/{start}/{end}",
        "hrv": f"/hrv-service/hrv/daily/{start}/{end}",
        "vo2max": f"/metrics-service/metrics/maxmet/daily/{start}/{end}",
        "respiration": f"/usersummary-service/stats/respiration/daily/{start}/{end}",
        "training_readiness": f"/metrics-service/metrics/trainingreadiness/{end}",
        "training_status": f"/metrics-service/metrics/trainingstatus/aggregated/{end}",
        "hr_zones": "/biometric-service/heartRateZones",
        "daily_hr_detail": f"wellness-service/wellness/dailyHeartRate?date={end}",
        "daily_respiration_detail": f"wellness-service/wellness/daily/respiration/{end}",
        "spo2": f"wellness-service/wellness/daily/spo2acclimation/{end}",
        "daily_summary_chart": f"wellness-service/wellness/dailySummaryChart/?date={end}",
    }


def source_text() -> str:
    """All package source, for checking whether a field name is referenced."""
    root = pathlib.Path(__file__).resolve().parents[1]
    return "\n".join(p.read_text() for p in root.rglob("*.py"))


def is_used(field: str, source: str) -> bool:
    """Whether this response field appears to be mapped anywhere."""
    leaf = field.split(".")[-1]
    if FSTRING_BUILT.match(leaf):
        return True
    if len(leaf) < 3:
        return True
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", leaf).lower()
    return any(f'"{n}"' in source or f"'{n}'" in source for n in (leaf, snake))


def audit(days_back: int = 15, include_noise: bool = False) -> dict:
    """Probe every endpoint and classify each returned field.

    Returns:
        Endpoint name to ``{kept, dropped, values}``.
    """
    from garmin.api import GarminSession

    session = GarminSession()
    source = source_text()
    end = date.today().isoformat()
    start = (date.today() - timedelta(days=days_back)).isoformat()

    report: dict = {}
    targets = dict(probes(end, start))
    for name, url in targets.items():
        try:
            response = session.get(url)
        except Exception as exc:  # noqa: BLE001 - report, don't abort the audit
            report[name] = {"error": str(exc)[:200]}
            continue
        record = response[0] if isinstance(response, list) and response else response
        if not record:
            report[name] = {"error": "empty response"}
            continue
        report[name] = _classify(flatten(record), source, include_noise)

    # Activities need an id, so they are probed separately.
    activities = session.get("/activitylist-service/activities/search/activities?limit=1&start=0")
    if activities:
        activity = activities[0]
        report["activity_summary"] = _classify(flatten(activity), source, include_noise)
        activity_id = activity.get("activityId")
        for label, url in (
            ("activity_detail", f"/activity-service/activity/{activity_id}"),
            ("exercise_sets", f"/activity-service/activity/{activity_id}/exerciseSets"),
        ):
            try:
                response = session.get(url)
            except Exception as exc:  # noqa: BLE001
                report[label] = {"error": str(exc)[:200]}
                continue
            record = response[0] if isinstance(response, list) and response else response
            if record:
                report[label] = _classify(flatten(record), source, include_noise)
    return report


def _classify(fields: dict, source: str, include_noise: bool) -> dict:
    kept, dropped = [], []
    for name in fields:
        if not include_noise and NOISE.search(name):
            continue
        (kept if is_used(name, source) else dropped).append(name)
    return {
        "n_fields": len(kept) + len(dropped),
        "kept": sorted(kept),
        "dropped": sorted(dropped),
        "values": {k: str(v)[:60] for k, v in fields.items()},
    }


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", default=None, help="Write the full JSON report here.")
    parser.add_argument("--days-back", type=int, default=15)
    parser.add_argument("--include-noise", action="store_true",
                        help="Also list identifier and privacy fields.")
    args = parser.parse_args(argv)

    report = audit(args.days_back, args.include_noise)
    print(f"{'source':<26s} {'fields':>7s} {'kept':>5s} {'dropped':>8s}")
    for name, info in report.items():
        if "error" in info:
            print(f"{name:<26s}   {info['error'][:44]}")
            continue
        print(f"{name:<26s} {info['n_fields']:>7d} {len(info['kept']):>5d} "
              f"{len(info['dropped']):>8d}")
    for name, info in report.items():
        if info.get("dropped"):
            print(f"\n--- {name}: dropped ---")
            for field in info["dropped"]:
                print(f"    {field:<52s} = {info['values'].get(field)}")
    if args.out:
        with open(args.out, "w") as handle:
            json.dump(report, handle, indent=1, default=str)
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
