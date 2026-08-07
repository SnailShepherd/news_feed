from scripts.aggregate import _normalize_canonical_url


def test_canonical_drops_tracking_params():
    base = "https://example.com/news/story"
    url_with_trackers = f"{base}?utm_source=newsletter&yclid=12345&gclid=abc123"
    alt_url = f"{base}?fbclid=zzz&utm_referrer=https%3A%2F%2Fsocial.example"

    assert _normalize_canonical_url(url_with_trackers) == base
    assert _normalize_canonical_url(alt_url) == base

from scripts import aggregate


def test_root_canonical_is_rejected_for_valid_article(monkeypatch):
    source = {
        "name": "Canonical fixture",
        "base_url": "https://example.com",
        "start_url": "https://example.com/news",
        "include_patterns": ["/news/"],
        "include_regex": r"/news/[^/]+$",
        "restrict_domain": True,
    }
    monkeypatch.setattr(aggregate, "STATE", aggregate.ensure_state_keys({}))
    aggregate.SOURCE_SUMMARY.clear()
    monkeypatch.setattr(
        aggregate, "fetch_amp_if_available", lambda *args, **kwargs: (None, None)
    )
    article_url = "https://example.com/news/a-real-story"
    html = """<html><head><title>A real story</title><link rel="canonical" href="/"></head>
    <body><article><p>A meaningful article body with enough distinct words for identity.</p></article></body></html>"""

    item = aggregate.build_item(article_url, source["name"], html, src=source)

    assert item["url"] == article_url
    assert "canonical_url" not in item
    assert item["id"] == aggregate.hashlib.sha256(article_url.encode()).hexdigest()
    assert aggregate.SOURCE_SUMMARY[source["name"]][
        "canonical_rejections_by_reason"
    ] == {"site_root": 1}
