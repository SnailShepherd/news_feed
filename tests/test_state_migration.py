import json

from scripts import aggregate


def test_prune_state_keeps_only_feed_and_seen_metadata(monkeypatch):
    monkeypatch.setattr(
        aggregate,
        "STATE",
        {
            "headers": {"https://live": {}, "https://old": {}},
            "first_seen": {"live-id": "now", "old-id": "then"},
            "seen_urls": {"Source": ["https://seen"]},
            "aliases": {"https://live": "https://canonical", "https://old": "https://old"},
            "content_hashes": {"live-hash": "https://canonical", "old-hash": "https://old"},
            "canonical_item_ids": {"https://live": "live-id", "https://old": "old-id"},
        },
    )

    aggregate.prune_state(
        [{"id": "live-id", "url": "https://live", "canonical_url": "https://canonical"}],
        [{"start_url": "https://index", "base_url": "https://base"}],
    )

    assert "https://old" not in aggregate.STATE["headers"]
    assert aggregate.STATE["first_seen"] == {"live-id": "now"}
    assert aggregate.STATE["aliases"] == {"https://live": "https://canonical"}
    assert aggregate.STATE["content_hashes"] == {"live-hash": "https://canonical"}
    assert aggregate.STATE["canonical_item_ids"] == {"https://live": "live-id"}


def test_ensure_state_keys_adds_missing_fields(monkeypatch):
    legacy_state = {
        "headers": {"X-Test": "1"},
        "stats": {},
        "seen_urls": {},
    }
    aggregate.ensure_state_keys(legacy_state)
    for required in [
        "headers",
        "stats",
        "index_hash",
        "seen_urls",
        "candidate_urls",
        "url_states",
        "first_seen",
        "host_state",
        "aliases",
        "content_hashes",
        "canonical_item_ids",
    ]:
        assert required in legacy_state
        assert isinstance(legacy_state[required], dict)

    monkeypatch.setattr(aggregate, "STATE", legacy_state)
    src_name = "Legacy Source"
    monkeypatch.setitem(aggregate.SOURCE_MIN_WORDS, src_name, aggregate.DEFAULT_MIN_WORDS)

    article_body = "<html><body><article><p>{}</p></article></body></html>".format(
        " ".join(["слово" for _ in range(120)])
    )
    url = "https://example.com/articles/legacy"
    tracked_url = f"{url}?yclid=12345"

    item_a = aggregate.build_item(url, src_name, article_body, content_selectors=["article"])
    item_b = aggregate.build_item(tracked_url, src_name, article_body, content_selectors=["article"])

    assert item_a["id"] == item_b["id"]
    canonical_ids = aggregate.STATE.get("canonical_item_ids", {})
    assert canonical_ids.get(url) == item_a["id"]
    assert canonical_ids.get(tracked_url) == item_a["id"]


def test_legacy_seen_urls_are_accepted_only_when_retained(tmp_path, monkeypatch):
    retained_url = "https://example.test/retained"
    rejected_url = "https://example.test/previously-short"
    feed_path = tmp_path / "unified.json"
    feed_path.write_text(
        json.dumps({"items": [{"source": "Legacy", "url": retained_url}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(aggregate, "OUT_JSON", feed_path)
    monkeypatch.setattr(aggregate, "_RETAINED_URL_CACHE", None)
    monkeypatch.setattr(
        aggregate,
        "STATE",
        aggregate.ensure_state_keys({
            "seen_urls": {"Legacy": [retained_url, rejected_url]},
        }),
    )

    states = aggregate._source_url_states("Legacy")

    assert states[retained_url]["status"] == "accepted"
    assert states[rejected_url]["status"] == "retryable_failure"
