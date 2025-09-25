import json

from scripts.metrics import _load_items, check_anti_genie, compute_metrics


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
