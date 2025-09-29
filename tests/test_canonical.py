from scripts.aggregate import _normalize_canonical_url


def test_canonical_drops_tracking_params():
    base = "https://example.com/news/story"
    url_with_trackers = f"{base}?utm_source=newsletter&yclid=12345&gclid=abc123"
    alt_url = f"{base}?fbclid=zzz&utm_referrer=https%3A%2F%2Fsocial.example"

    assert _normalize_canonical_url(url_with_trackers) == base
    assert _normalize_canonical_url(alt_url) == base
