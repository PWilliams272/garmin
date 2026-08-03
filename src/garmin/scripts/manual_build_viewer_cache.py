from dotenv import load_dotenv
load_dotenv()

import argparse

from garmin.io.curated_store import CuratedDataStore
from garmin.io.file_manager import FileManager

# Imported for their side-effect-free payload-building functions, not as a
# Flask app -- garmin.app.routes only touches Flask (request/jsonify) inside
# its route handlers, so importing it here doesn't need an app context.
# Reusing these functions (rather than reimplementing the same assembly
# logic here) keeps the cached payload and the live-fallback payload
# guaranteed identical in shape.
from garmin.app import routes as app_routes
from garmin.prototypes.activity_explorer import build_activity_explorer_html


def build_viewer_cache(storage_target: str) -> None:
    """Assemble every page's web-response JSON once and write it to
    curated/viewer_cache/<name>.json, so a page load becomes a single read
    instead of the dozen-plus individual curated/analyzed file reads each
    payload builder normally does. Most useful against S3, where each of
    those reads is its own network round trip; run this after
    manual_analyze_metrics.py so the analyzed layer it reads is fresh.

    A None payload (dataset not analyzed yet) is not cached -- the route's
    live fallback path already handles that case (falls through to mock),
    and caching a "no data" result would need its own invalidation once
    real data does land.
    """
    fm = FileManager(environment="aws" if storage_target == "s3" else "local")
    store = CuratedDataStore(file_manager=fm)
    source = storage_target

    # (cache_name, builder) -- one entry per page/sport combination the app
    # actually serves. Keep in sync with the _cached_or_live(...) call sites
    # in garmin/app/routes.py.
    jobs = [
        (f'quick_dashboard_{source}', lambda: app_routes._health_analyzed_payload(source=source)),
        (f'fitness_running_{source}', lambda: app_routes._running_real_payload(source=source)),
        (f'fitness_lifting_{source}', lambda: app_routes._lifting_real_payload(source=source)),
        (f'activities_overview_{source}', lambda: app_routes._activities_real_payload(source=source)),
        (f'activities_list_{source}', lambda: app_routes._activities_list_payload(source=source)),
        (f'data_status_{source}', lambda: app_routes._data_status_payload(source=source)),
    ]

    for cache_name, build_fn in jobs:
        payload = build_fn()
        if payload is None:
            print(f"Skipped {cache_name}: no data to cache yet.")
            continue
        store.write_viewer_cache(cache_name, payload)
        print(f"Wrote viewer_cache/{cache_name}.json.")

    # HTML pages (not JSON payloads) get their own cache path -- see
    # CuratedDataStore.write_viewer_cache_html / _cached_html_or_live in
    # garmin/app/routes.py.
    html_jobs = [
        (f'activity_explorer_{source}', lambda: build_activity_explorer_html(store)),
    ]
    for cache_name, build_fn in html_jobs:
        html = build_fn()
        store.write_viewer_cache_html(cache_name, html)
        print(f"Wrote viewer_cache/{cache_name}.html.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Precompute every page's web-response JSON into curated/viewer_cache/, "
        "so the app serves pages from one fast read instead of assembling them live from "
        "curated/analyzed/ on every request. Run after manual_analyze_metrics.py."
    )
    parser.add_argument(
        "--storage-target",
        choices=["local", "s3"],
        default="local",
        help="Where curated/analyzed inputs are read from and viewer_cache outputs are written.",
    )
    return parser


def main(argv: list[str] | None = None):
    args = build_parser().parse_args(argv)
    build_viewer_cache(args.storage_target)


if __name__ == "__main__":
    main()
