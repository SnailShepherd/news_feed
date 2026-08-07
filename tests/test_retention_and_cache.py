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


def test_soft_shares_materially_improve_specialist_representation(monkeypatch):
    monkeypatch.setattr(aggregate, "FEED_MAX_ITEMS", 12)
    monkeypatch.setattr(aggregate, "FEED_MIN_ITEMS_PER_SOURCE", 1)
    monkeypatch.setattr(aggregate, "SOURCE_RETENTION_WEIGHTS", {})
    items = [_item("dominant", day) for day in range(20, 0, -1)]
    items += [_item("planning", day) for day in range(3, 0, -1)]
    items += [_item("standards", day) for day in range(3, 0, -1)]

    retained = aggregate.retain_bounded_items(items)

    assert sum(item["source"] != "dominant" for item in retained) == 6


def test_soft_shares_leave_no_capacity_unused(monkeypatch):
    monkeypatch.setattr(aggregate, "FEED_MAX_ITEMS", 8)
    monkeypatch.setattr(aggregate, "FEED_MIN_ITEMS_PER_SOURCE", 1)
    monkeypatch.setattr(aggregate, "SOURCE_RETENTION_WEIGHTS", {"dominant": 0.25})
    items = [_item("dominant", day) for day in range(10, 0, -1)] + [_item("specialist", 1)]

    retained = aggregate.retain_bounded_items(items)

    assert len(retained) == 8
    assert sum(item["source"] == "dominant" for item in retained) == 7


def test_soft_share_result_is_newest_first_with_stable_source_order(monkeypatch):
    monkeypatch.setattr(aggregate, "FEED_MAX_ITEMS", 7)
    monkeypatch.setattr(aggregate, "FEED_MIN_ITEMS_PER_SOURCE", 1)
    monkeypatch.setattr(aggregate, "SOURCE_RETENTION_WEIGHTS", {})
    items = [_item("dominant", day) for day in range(9, 0, -1)]
    items += [_item("specialist", day) for day in range(3, 0, -1)]

    retained = aggregate.retain_bounded_items(items)

    assert retained == sorted(retained, key=lambda item: item["published_at"], reverse=True)
    for source in {item["source"] for item in retained}:
        source_dates = [item["published_at"] for item in retained if item["source"] == source]
        assert source_dates == sorted(source_dates, reverse=True)


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
    health_state_path = tmp_path / "health-state.json"
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
    monkeypatch.setattr(aggregate, "SOURCE_HEALTH_STATE_FILE", health_state_path)
    monkeypatch.setattr(aggregate, "PAGES_DIR", pages)
    monkeypatch.setattr(aggregate, "SOURCE_SUMMARY", summaries)
    monkeypatch.setattr(aggregate, "STATE", {})
    monkeypatch.setattr(aggregate, "SOURCE_HEALTH_STATE", {"selected": 1, "skipped": 7})

    aggregate.write_source_health_report([
        {"name": "selected", "enabled": True},
        {"name": "skipped", "enabled": True},
        {"name": "new skipped", "enabled": True},
    ])

    rows = {row["source"]: row for row in json.loads(health_path.read_text())["sources"]}
    assert rows["selected"]["consecutive_failures"] == 2
    assert rows["skipped"]["consecutive_failures"] == 7
    assert rows["new skipped"]["consecutive_failures"] == 0


def test_unchanged_index_preserves_zero_accepted_discovery_failure(tmp_path, monkeypatch):
    health_path = tmp_path / "health.json"
    state_path = tmp_path / "state.json"
    pages = tmp_path / "pages"
    pages.mkdir()
    summaries = defaultdict(dict)
    summaries["filtered"] = {"index_fetch_status": "unchanged"}
    monkeypatch.setattr(aggregate, "SOURCE_HEALTH_JSON", health_path)
    monkeypatch.setattr(aggregate, "STATE_FILE", state_path)
    monkeypatch.setattr(aggregate, "PAGES_DIR", pages)
    monkeypatch.setattr(aggregate, "SOURCE_SUMMARY", summaries)
    monkeypatch.setattr(
        aggregate,
        "STATE",
        {
            "source_discovery_failure_streaks": {"filtered": 1},
            "source_discovery_state": {
                "filtered": {
                    "last_successful_discovery_at": "2026-08-06T12:00:00+00:00",
                    "raw_link_candidates": 12,
                    "accepted_links": 0,
                }
            },
        },
    )

    aggregate.write_source_health_report([{"name": "filtered", "enabled": True}])

    row = json.loads(health_path.read_text())["sources"][0]
    assert row["raw_link_candidates"] == 12
    assert row["accepted_links"] == 0
    assert row["consecutive_discovery_failures"] == 2
    assert row["last_successful_discovery_at"] == "2026-08-06T12:00:00+00:00"


def test_filtered_attempts_retain_aggregate_raw_candidates(tmp_path, monkeypatch):
    health_path = tmp_path / "health.json"
    summaries = defaultdict(dict)
    summaries["filtered"] = {
        "index_fetch_status": "fetched",
        "raw_link_candidates": 37,
        "accepted_links": 0,
        "index_attempts": [
            {"url": "https://example.test/a", "raw_link_candidates": 20, "accepted_links": 0},
            {"url": "https://example.test/b", "raw_link_candidates": 17, "accepted_links": 0},
        ],
    }
    monkeypatch.setattr(aggregate, "SOURCE_HEALTH_JSON", health_path)
    monkeypatch.setattr(aggregate, "SOURCE_SUMMARY", summaries)
    monkeypatch.setattr(aggregate, "STATE", {})

    aggregate.write_source_health_report([{"name": "filtered", "enabled": True}])

    row = json.loads(health_path.read_text())["sources"][0]
    assert row["raw_link_candidates"] == 37
    assert row["accepted_links"] == 0
    assert row["failure_class"] == "discovery_filter_failure"
    assert [attempt["raw_link_candidates"] for attempt in row["index_attempts"]] == [20, 17]


def test_failed_discovery_does_not_advance_last_success_timestamp(tmp_path, monkeypatch):
    health_path = tmp_path / "health.json"
    state_path = tmp_path / "state.json"
    pages = tmp_path / "pages"
    pages.mkdir()
    summaries = defaultdict(dict)
    summaries["empty"] = {
        "index_fetch_status": "fetched",
        "raw_link_candidates": 0,
        "accepted_links": 0,
    }
    previous_success = "2026-08-01T09:00:00+00:00"
    state = {
        "source_discovery_state": {
            "empty": {
                "last_successful_discovery_at": previous_success,
                "raw_link_candidates": 5,
                "accepted_links": 4,
            }
        }
    }
    monkeypatch.setattr(aggregate, "SOURCE_HEALTH_JSON", health_path)
    monkeypatch.setattr(aggregate, "STATE_FILE", state_path)
    monkeypatch.setattr(aggregate, "PAGES_DIR", pages)
    monkeypatch.setattr(aggregate, "SOURCE_SUMMARY", summaries)
    monkeypatch.setattr(aggregate, "STATE", state)

    aggregate.write_source_health_report([{"name": "empty", "enabled": True}])

    row = json.loads(health_path.read_text())["sources"][0]
    assert row["consecutive_discovery_failures"] == 1
    assert row["last_successful_discovery_at"] == previous_success
    assert state["source_discovery_state"]["empty"]["raw_link_candidates"] == 0
    assert state["source_discovery_state"]["empty"]["accepted_links"] == 0
