"""Spike: pull one real activity's full per-point time series (GPS + speed/
cadence/HR/power/...) for prototyping a detail view (map, pace/HR charts).

Not part of the scheduled pipeline -- ActivityPuller.get_activity_timeseries()
is verified against a live response (see its docstring) but not yet wired
into update_all() or any curated storage convention; this script's only job
is to get one concrete sample on disk. Requires a live Garmin login (local
dev only), same as manual_update.py.
"""
from dotenv import load_dotenv
load_dotenv()

import argparse

from garmin.api import GarminSession
from garmin.io.file_manager import FileManager
from garmin.pullers.activities import ActivityPuller


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--activity-type", default="running",
        help="Garmin typeKey to search for the most recent activity of, if --activity-id isn't given.",
    )
    parser.add_argument("--activity-id", default=None, help="Pull a specific activity_id instead of searching.")
    parser.add_argument("--lookback-days", type=int, default=90, help="How far back to search for a recent activity.")
    parser.add_argument("--storage-target", choices=["local", "s3"], default="local")
    return parser


def main(argv: list[str] | None = None) -> None:
    import datetime

    args = build_parser().parse_args(argv)
    session = GarminSession()
    puller = ActivityPuller(session)

    activity_id = args.activity_id
    if activity_id is None:
        today = datetime.date.today()
        start = (today - datetime.timedelta(days=args.lookback_days)).isoformat()
        activities = puller.pull_activity_list(start, today.isoformat(), activity_types={args.activity_type})
        if not activities:
            print(f"No {args.activity_type} activities found in the last {args.lookback_days} days.")
            return
        activity_id = str(max(activities, key=lambda a: a["startTimeLocal"])["activityId"])
        print(f"Using most recent {args.activity_type} activity: {activity_id}")

    detail_df = puller.get_activity_timeseries(activity_id)
    if detail_df.empty:
        print(f"No detail time series returned for activity {activity_id} -- check "
              "ActivityPuller.get_activity_timeseries's docstring, the endpoint/response shape may have changed.")
        return

    print(f"Pulled {len(detail_df)} points.")
    print(detail_df.head())

    fm = FileManager(environment="aws" if args.storage_target == "s3" else "local")
    path = f"curated/activities/detail/{args.activity_type}/timeseries/activity_id={activity_id}.parquet"
    fm.write_df(detail_df, path, format="parquet")
    print(f"Wrote {path} ({args.storage_target}).")


if __name__ == "__main__":
    main()
