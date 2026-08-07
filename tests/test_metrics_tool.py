import json
import sys
from datetime import datetime, timedelta, timezone

from scripts.metrics import (
    _load_items,
    check_anti_genie,
    compute_metrics,
    compute_source_metrics,
    classify_source_health,
    find_unexpected_empty_sources,
)
from scripts import metrics


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


def test_source_health_distinguishes_failures_from_degraded_sources():
    failures, warnings = classify_source_health([
        {"source": "broken", "index_fetch_status": "failed", "consecutive_failures": 3, "last_error": "timeout"},
        {"source": "cached", "index_fetch_status": "cached", "cached_fallback_used": True},
        {"source": "changed markup", "index_fetch_status": "fetched", "raw_link_candidates": 0},
        {"source": "good", "index_fetch_status": "unchanged"},
    ])

    assert failures == ["broken: failed for 3 consecutive runs (timeout)"]
    assert warnings == [
        "cached: cached fallback used",
        "changed markup: fetched index contained no link candidates",
    ]


def test_source_health_warns_before_failure_threshold_and_counts_bad_dates():
    failures, warnings = classify_source_health([
        {
            "source": "flaky",
            "index_fetch_status": "failed",
            "consecutive_failures": 2,
            "future_date_rejections": 4,
        }
    ])

    assert failures == []
    assert warnings == [
        "flaky: transient failed (run 2/3)",
        "flaky: rejected 4 future publication dates",
    ]


def test_article_crawl_outage_uses_failure_streak():
    failures, warnings = classify_source_health([
        {
            "source": "articles broken",
            "index_fetch_status": "fetched",
            "attempted_articles": 5,
            "accepted_articles": 0,
            "last_error": "article timeout",
            "consecutive_failures": 3,
        },
        {
            "source": "content rejected",
            "index_fetch_status": "fetched",
            "attempted_articles": 4,
            "accepted_articles": 0,
            "consecutive_failures": 0,
        },
    ])

    assert failures == [
        "articles broken: article crawl failed for 3 consecutive runs (article timeout)"
    ]
    assert warnings == ["content rejected: attempted 4 articles but accepted none"]


def test_sources_skipped_by_selection_are_ignored():
    failures, warnings = classify_source_health([
        {
            "source": "not selected",
            "index_fetch_status": "skipped_selection",
            "consecutive_failures": 99,
        }
    ])

    assert failures == []
    assert warnings == []


def test_hard_source_failure_does_not_publish_candidate(tmp_path, monkeypatch):
    published = tmp_path / "published.json"
    published_health = tmp_path / "published-health.json"
    candidate = tmp_path / "candidate.json"
    candidate_health = tmp_path / "candidate-health.json"
    published_state = tmp_path / "published-state.json"
    candidate_state = tmp_path / "candidate-state.json"
    sources = tmp_path / "sources.json"
    published.write_text('{"items": [{"id": "last-good"}]}', encoding="utf-8")
    published_health.write_text('{"sources": [{"source": "good"}]}', encoding="utf-8")
    candidate.write_text('{"items": [{"id": "invalid-new"}]}', encoding="utf-8")
    candidate_health.write_text(json.dumps({"sources": [{
        "source": "upstream", "index_fetch_status": "failed",
        "consecutive_failures": 3, "last_error": "outage"
    }]}), encoding="utf-8")
    published_state.write_text('{"seen_urls": {"upstream": ["old"]}}', encoding="utf-8")
    candidate_state.write_text('{"seen_urls": {"upstream": ["rejected-new"]}}', encoding="utf-8")
    sources.write_text('[{"name": "upstream"}]', encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["metrics.py", str(candidate), "--sources", str(sources),
        "--source-health", str(candidate_health), "--strict-source-health",
        "--promote-feed", str(published), "--promote-source-health", str(published_health),
        "--candidate-state", str(candidate_state), "--promote-state", str(published_state)])

    assert metrics.main() == 1
    assert json.loads(published.read_text(encoding="utf-8"))["items"][0]["id"] == "last-good"
    assert json.loads(published_health.read_text(encoding="utf-8"))["sources"][0]["source"] == "good"
    assert json.loads(published_state.read_text(encoding="utf-8"))["seen_urls"]["upstream"] == ["old"]
    assert candidate.exists()
    assert candidate_health.exists()
    assert candidate_state.exists()
