from scripts import aggregate


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
