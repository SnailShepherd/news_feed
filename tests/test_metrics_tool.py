import json
from datetime import datetime, timedelta, timezone

from scripts.metrics import (
    _load_items,
    check_anti_genie,
    compute_metrics,
    compute_source_metrics,
    find_unexpected_empty_sources,
)


def test_compute_metrics_counts(tmp_path):
    payload = {
        "items": [
            {"url": "https://example.com/news/", "content_text": ""},
            {"url": "https://example.com/story", "content_text": "Full text"},
        ]
    }
    path = tmp_path / "feed.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    items = _load_items(path)
    metrics = compute_metrics(items)

    assert metrics["total"] == 2
    assert metrics["empty_content_text"] == 1
    assert metrics["listing_urls_count"] == 1


def test_check_anti_genie_detects_drop():
    baseline = {"total": 10, "empty_content_text": 4, "listing_urls_count": 2}
    current = {"total": 7, "empty_content_text": 3, "listing_urls_count": 0}

    ok, message = check_anti_genie(baseline, current)

    assert not ok
    assert "dropped below" in (message or "")


def test_check_anti_genie_allows_listing_reduction():
    baseline = {"total": 10, "empty_content_text": 4, "listing_urls_count": 2}
    current = {"total": 8, "empty_content_text": 3, "listing_urls_count": 0}

    ok, message = check_anti_genie(baseline, current)

    assert ok
    assert message is None


NOW = datetime(2026, 8, 6, tzinfo=timezone.utc)
SOURCES = [
    {"name": "healthy", "enabled": True},
    {"name": "missing", "enabled": True},
    {"name": "disabled", "enabled": False},
]


def test_missing_enabled_source_fails_health_check():
    _, report = compute_source_metrics([], SOURCES, stale_after=timedelta(days=7), now=NOW)
    assert find_unexpected_empty_sources(report, set()) == ["healthy", "missing"]


def test_stale_source_is_reported():
    items = [{"source": "healthy", "published_at": "2026-07-01T00:00:00Z"}]
    totals, report = compute_source_metrics(items, SOURCES, stale_after=timedelta(days=7), now=NOW)
    assert totals["stale_sources"] == 1
    assert report[0]["stale"] is True


def test_healthy_source_uses_fetch_timestamp_when_publication_is_invalid():
    items = [{"source": "healthy", "published_at": "not-a-date", "fetched_at": "2026-08-05T00:00:00Z"}]
    totals, report = compute_source_metrics(items, SOURCES, stale_after=timedelta(days=7), now=NOW)
    assert totals == {"enabled_sources": 2, "sources_with_items": 1, "sources_without_items": 1, "stale_sources": 0}
    assert report[0]["newest_timestamp"] == "2026-08-05T00:00:00+00:00"


def test_explicitly_exempted_empty_source_passes():
    _, report = compute_source_metrics([], [{"name": "expected empty"}], stale_after=timedelta(days=7), now=NOW)
    assert find_unexpected_empty_sources(report, {"expected empty"}) == []
