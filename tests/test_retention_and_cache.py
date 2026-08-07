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
