"""Tests for the S3 -> local mirror used by the dev server.

The comparison logic is where this goes wrong quietly. If freshness is
misjudged in one direction the sync re-downloads everything on every start
(slow but visible); in the other it never re-downloads and the dev server
serves stale data that looks exactly like fresh data. The second is the one
worth pinning.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from garmin.io.local_sync import SYNC_PREFIXES, _local_is_current


def _write(path, content=b"x" * 100):
    path.write_bytes(content)
    return path


def test_a_missing_file_is_not_current(tmp_path):
    missing = tmp_path / "nope.parquet"
    assert _local_is_current(str(missing), 100, datetime.now(timezone.utc)) is False


def test_a_matching_size_and_fresh_mtime_is_current(tmp_path):
    f = _write(tmp_path / "a.parquet")
    remote_time = datetime.now(timezone.utc) - timedelta(hours=1)
    assert _local_is_current(str(f), 100, remote_time) is True


def test_a_size_mismatch_is_not_current(tmp_path):
    """Same name, different bytes -- the object changed."""
    f = _write(tmp_path / "a.parquet")
    remote_time = datetime.now(timezone.utc) - timedelta(hours=1)
    assert _local_is_current(str(f), 999, remote_time) is False


def test_an_older_local_file_is_not_current(tmp_path):
    """The case that matters: S3 has a newer object of coincidentally equal
    size. Missing this serves stale data indistinguishable from fresh."""
    f = _write(tmp_path / "a.parquet")
    old = (datetime.now(timezone.utc) - timedelta(days=2)).timestamp()
    os.utime(f, (old, old))
    remote_time = datetime.now(timezone.utc)
    assert _local_is_current(str(f), 100, remote_time) is False


def test_naive_remote_timestamps_are_treated_as_utc(tmp_path):
    """boto3 returns tz-aware datetimes, but a naive one must not be compared
    against a UTC epoch as if it were local time -- that shifts freshness by
    the machine's offset and silently skips real updates."""
    f = _write(tmp_path / "a.parquet")
    old = (datetime.now(timezone.utc) - timedelta(days=2)).timestamp()
    os.utime(f, (old, old))
    naive_now = datetime.utcnow()
    assert _local_is_current(str(f), 100, naive_now) is False


def test_sync_covers_the_prefixes_the_viewer_reads():
    """Raw pulls are deliberately excluded -- large, and nothing reads them."""
    assert "curated/" in SYNC_PREFIXES
    assert "viewer_cache/" in SYNC_PREFIXES
    assert not any(p.startswith("raw") for p in SYNC_PREFIXES)
