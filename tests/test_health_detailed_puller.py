import pandas as pd

from garmin.pullers.health_detailed import (
    CacheWarmDenied,
    CacheWarmRequested,
    HealthDetailedPuller,
)


def test_pull_for_range_stops_after_first_denial() -> None:
    puller = HealthDetailedPuller(session=object())
    calls: list[str] = []

    def pull_one_day(date: str) -> pd.DataFrame:
        calls.append(date)
        if date == "2024-01-01":
            return pd.DataFrame(
                [{"query_date": date, "date_time_utc": "2024-01-01T00:00:00Z", "hr": 50}]
            )
        if date == "2024-01-02":
            raise CacheWarmRequested(date)
        if date == "2024-01-03":
            raise CacheWarmDenied(date)
        raise AssertionError(f"Unexpected extra pull attempt for {date}")

    df = puller._pull_for_range(
        pull_one_day,
        dates=["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"],
    )

    assert calls == ["2024-01-01", "2024-01-02", "2024-01-03"]
    assert list(df["query_date"]) == ["2024-01-01"]
    assert puller._last_pull_status == {
        "fetched": ["2024-01-01"],
        "no_data": [],
        "denied": ["2024-01-03"],
    }