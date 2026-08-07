import json
import os
from collections import defaultdict

from scripts import aggregate


def _item(source: str, sequence: int) -> dict:
    return {
        "id": f"{source}-{sequence}",
        "source": source,
        "published_at": f"2026-08-{sequence:02d}T00:00:00+00:00",
    }


def test_fair_retention_reserves_items_for_low_volume_sources(monkeypatch):
    monkeypatch.setattr(aggregate, "FEED_MAX_ITEMS", 6)
    monkeypatch.setattr(aggregate, "FEED_MIN_ITEMS_PER_SOURCE", 2)
    items = [_item("busy", day) for day in range(9, 0, -1)] + [
        _item("quiet", 2),
        _item("quiet", 1),
    ]

    retained = aggregate.retain_bounded_items(items)

    assert len(retained) == 6
    assert sum(item["source"] == "quiet" for item in retained) == 2
    assert retained == sorted(retained, key=lambda item: item["published_at"], reverse=True)


def test_page_cache_pruning_removes_old_then_excess_files(tmp_path, monkeypatch):
    monkeypatch.setattr(aggregate, "PAGES_DIR", tmp_path)
    monkeypatch.setattr(aggregate, "CACHE_MAX_AGE_DAYS", 2)
    monkeypatch.setattr(aggregate, "CACHE_MAX_BYTES", 8)
    old = tmp_path / "old"
    middle = tmp_path / "middle"
    newest = tmp_path / "newest"
    for path in (old, middle, newest):
        path.write_bytes(b"123456")
    os.utime(old, (100, 100))
    os.utime(middle, (250000, 250000))
    os.utime(newest, (300000, 300000))

    removed_files, removed_bytes = aggregate.prune_page_cache(now=300000)

    assert (removed_files, removed_bytes) == (2, 12)
    assert not old.exists()
    assert not middle.exists()
    assert newest.exists()


def test_state_pruning_bounds_url_lifecycle_to_current_candidates(monkeypatch):
    monkeypatch.setattr(
        aggregate,
        "STATE",
        aggregate.ensure_state_keys({
            "seen_urls": {"active": ["https://example.test/seen"]},
            "candidate_urls": {
                "active": ["https://example.test/current"],
                "removed": ["https://old.test/current"],
            },
            "url_states": {
                "active": {
                    "https://example.test/current": {"status": "retryable_failure"},
                    "https://example.test/seen": {"status": "accepted"},
                    "https://example.test/stale": {"status": "permanently_rejected"},
                },
                "removed": {"https://old.test/current": {"status": "accepted"}},
            },
        }),
    )

    aggregate.prune_state([], [{"name": "active", "start_url": "https://example.test"}])

    assert aggregate.STATE["candidate_urls"] == {
        "active": ["https://example.test/current"]
    }
    assert set(aggregate.STATE["url_states"]["active"]) == {
        "https://example.test/current",
        "https://example.test/seen",
    }
    assert "removed" not in aggregate.STATE["url_states"]


def test_health_report_preserves_skipped_streak_and_counts_article_outage(tmp_path, monkeypatch):
    health_path = tmp_path / "health.json"
    state_path = tmp_path / "state.json"
    pages = tmp_path / "pages"
    pages.mkdir()
    summaries = defaultdict(dict)
    summaries["selected"] = {
        "index_fetch_status": "fetched",
        "attempted_articles": 2,
        "total": 0,
        "last_error": "timeout",
    }
    summaries["skipped"] = {"index_fetch_status": "skipped_selection"}
    summaries["new skipped"] = {"index_fetch_status": "skipped_selection"}
    monkeypatch.setattr(aggregate, "SOURCE_HEALTH_JSON", health_path)
    monkeypatch.setattr(aggregate, "STATE_FILE", state_path)
    monkeypatch.setattr(aggregate, "PAGES_DIR", pages)
    monkeypatch.setattr(aggregate, "SOURCE_SUMMARY", summaries)
    monkeypatch.setattr(
        aggregate,
        "STATE",
        {"source_health_streaks": {"selected": 1, "skipped": 7}},
    )

    aggregate.write_source_health_report([
        {"name": "selected", "enabled": True},
        {"name": "skipped", "enabled": True},
        {"name": "new skipped", "enabled": True},
    ])

    rows = {row["source"]: row for row in json.loads(health_path.read_text())["sources"]}
    assert rows["selected"]["consecutive_failures"] == 2
    assert rows["skipped"]["consecutive_failures"] == 7
    assert rows["new skipped"]["consecutive_failures"] == 0
