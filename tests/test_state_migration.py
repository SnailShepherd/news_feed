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
        "index_hash",
        "seen_urls",
        "first_seen",
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


def test_save_state_never_serializes_session_cookies(tmp_path, monkeypatch):
    durable_path = tmp_path / "state.json"
    session_path = tmp_path / "session-state.json"
    monkeypatch.setattr(aggregate, "STATE_FILE", durable_path)
    monkeypatch.setattr(aggregate, "SESSION_STATE_FILE", session_path)
    monkeypatch.setattr(
        aggregate,
        "STATE",
        {
            "index_hash": {"url": "digest"},
            "host_state": {"leak.example": {"cookies": [{"value": "durable-secret"}]}},
        },
    )
    monkeypatch.setattr(
        aggregate,
        "SESSION_STATE",
        {"host_state": {"example.com": {"cookies": [{"name": "sid", "value": "secret"}]}}},
    )

    aggregate.save_state()

    durable = durable_path.read_text(encoding="utf-8")
    assert "cookie" not in durable.lower()
    assert "secret" not in durable
    assert json.loads(session_path.read_text(encoding="utf-8"))["host_state"]
