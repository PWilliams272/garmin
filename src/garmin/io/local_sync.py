"""Mirror the S3 curated data down to the local data directory.

Local development has a genuine tension: S3 is the source of truth, but every
read against it is a network round trip, and the viewer does a lot of reads.
Reading local files is far faster and works offline; reading S3 is always
current. Syncing once at startup and then serving local gets both.

This is deliberately *not* used in the deployed viewer. That host has no local
data directory and no business writing one -- it reads S3 directly.

This mirrors *down* only: files that exist locally but not in S3 are left
alone, never deleted. Local-only artifacts (a `*_local.json` viewer cache, an
experiment) are the developer's, not this function's to remove.

The comparison is size-plus-mtime, the same approach ``aws s3 sync`` uses.
After downloading, the local file's mtime is set to the object's
``LastModified`` so the next run compares like with like; without that, every
local file would look newer than its source and nothing would ever re-sync.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC

import boto3

from garmin._paths import get_data_dir

#: Prefixes worth mirroring: the curated layer the pages read, and the
#: precomputed page payloads. Raw pulls are deliberately excluded -- they are
#: large, and nothing in the viewer reads them.
SYNC_PREFIXES = ("curated/", "viewer_cache/")

#: Concurrent downloads. These are many small objects where latency, not
#: bandwidth, dominates, so parallelism helps far more than it does for one
#: large transfer.
_MAX_WORKERS = 16


def _local_is_current(path: str, size: int, last_modified) -> bool:
    """Whether the local file already matches the S3 object.

    Args:
        path: Local file path.
        size: Object size in bytes.
        last_modified: Object's ``LastModified`` datetime.

    Returns:
        True when the local file exists with the same size and an mtime no
        older than the object's.
    """
    try:
        stat = os.stat(path)
    except FileNotFoundError:
        return False
    if stat.st_size != size:
        return False
    return stat.st_mtime >= last_modified.replace(tzinfo=UTC).timestamp()


def sync_from_s3(
    bucket: str | None = None,
    local_dir: str | None = None,
    prefixes: tuple[str, ...] = SYNC_PREFIXES,
    show_progress: bool = True,
) -> dict:
    """Download S3 objects that are missing or stale locally.

    Args:
        bucket: Source bucket, defaulting to ``GARMIN_S3_BUCKET``.
        local_dir: Destination directory, defaulting to the package data dir.
        prefixes: S3 key prefixes to mirror.
        show_progress: Print a one-line summary per prefix.

    Returns:
        ``{"checked", "downloaded", "bytes", "skipped"}`` counts.

    Raises:
        ValueError: If no bucket is configured.
    """
    bucket = bucket or os.environ.get("GARMIN_S3_BUCKET")
    if not bucket:
        raise ValueError("No S3 bucket configured (set GARMIN_S3_BUCKET).")
    local_dir = local_dir or str(get_data_dir())

    client = boto3.client("s3")
    totals = {"checked": 0, "downloaded": 0, "bytes": 0, "skipped": 0}

    for prefix in prefixes:
        stale: list[tuple[str, str, int, object]] = []
        current_here = 0
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith("/"):
                    continue
                totals["checked"] += 1
                destination = os.path.join(local_dir, key)
                if _local_is_current(destination, obj["Size"], obj["LastModified"]):
                    totals["skipped"] += 1
                    current_here += 1
                    continue
                stale.append((key, destination, obj["Size"], obj["LastModified"]))

        def fetch(item):
            key, destination, size, last_modified = item
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            client.download_file(bucket, key, destination)
            # Stamp the source's mtime so the next run's comparison is
            # meaningful rather than always seeing a brand-new local file.
            stamp = last_modified.replace(tzinfo=UTC).timestamp()
            os.utime(destination, (stamp, stamp))
            return size

        if stale:
            with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
                for size in pool.map(fetch, stale):
                    totals["downloaded"] += 1
                    totals["bytes"] += size

        if show_progress:
            # Per-prefix, not the running total -- reporting the cumulative
            # count here made a 7-object prefix claim thousands of files.
            print(f"  {prefix:16s} {len(stale)} updated, "
                  f"{current_here} already current")

    return totals
