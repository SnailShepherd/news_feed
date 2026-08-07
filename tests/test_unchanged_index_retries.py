import json
from collections import defaultdict

import requests

from scripts import aggregate


class JsonResponse:
    status_code = 200
    headers = {}

    def __init__(self, payload):
        self.payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self.payload

    def raise_for_status(self):
        return None


def _reset(monkeypatch):
    monkeypatch.setattr(
        aggregate,
        "STATE",
        aggregate.ensure_state_keys({"headers": {}, "stats": {}, "index_hash": {}, "seen_urls": {}}),
    )
    monkeypatch.setattr(aggregate, "_last_req_at", defaultdict(lambda: 0.0))
    aggregate.SOURCE_SUMMARY.clear()
    aggregate.SOURCE_MIN_WORDS.clear()


def _article_html():
    return "<html><body><article><h1>Recovered</h1><p>{}</p></article></body></html>".format(
        " ".join(["word"] * 120)
    )


def test_html_failed_article_is_retried_with_unchanged_index(monkeypatch):
    _reset(monkeypatch)
    index_url = "https://example.test/news"
    article_url = "https://example.test/news/recovered"
    index_html = f'<html><body><a href="{article_url}">Article</a></body></html>'
    run = {"number": 1, "article_calls": 0}

    def fetch(url, src=None):
        if url == index_url:
            return index_html
        run["article_calls"] += 1
        if run["number"] == 1:
            raise requests.ConnectionError("temporary article outage")
        return _article_html()

    monkeypatch.setattr(aggregate, "fetch_page", fetch)
    src = {
        "name": "HTML retry",
        "start_url": index_url,
        "base_url": "https://example.test",
        "content_selectors": ["article"],
        "article_max_attempts": 2,
        "article_retry_delay": 0,
    }

    assert aggregate.harvest_source(src) == []
    assert run["article_calls"] == 2
    assert aggregate.STATE["url_states"][src["name"]][article_url]["status"] == "retryable_failure"

    run["number"] = 2
    items = aggregate.harvest_source(src)
    assert len(items) == 1
    assert items[0]["url"] == article_url
    assert aggregate.SOURCE_SUMMARY[src["name"]]["index_fetch_status"] == "unchanged"
    assert aggregate.STATE["url_states"][src["name"]][article_url]["status"] == "accepted"


def test_api_failed_article_is_retried_with_unchanged_index(monkeypatch):
    _reset(monkeypatch)
    endpoint = "https://api.example.test/news"
    article_url = "https://example.test/news/recovered"
    payload = {"data": [{"url": article_url, "title": "Recovered"}]}
    run = {"number": 1, "article_calls": 0}

    monkeypatch.setattr(aggregate.SESSION, "get", lambda *args, **kwargs: JsonResponse(payload))

    def fetch(url, src=None):
        run["article_calls"] += 1
        if run["number"] == 1:
            raise requests.ConnectionError("temporary article outage")
        return _article_html()

    monkeypatch.setattr(aggregate, "fetch_page", fetch)
    src = {
        "name": "API retry",
        "api_endpoint": endpoint,
        "start_url": "https://example.test/news",
        "base_url": "https://example.test",
        "content_selectors": ["article"],
        "article_max_attempts": 2,
        "article_retry_delay": 0,
    }

    assert aggregate.harvest_json_source(src) == []
    assert run["article_calls"] == 2
    assert aggregate.STATE["url_states"][src["name"]][article_url]["status"] == "retryable_failure"

    run["number"] = 2
    items = aggregate.harvest_json_source(src)
    assert len(items) == 1
    assert items[0]["url"] == article_url
    assert aggregate.SOURCE_SUMMARY[src["name"]]["index_fetch_status"] == "unchanged"
    assert aggregate.STATE["url_states"][src["name"]][article_url]["status"] == "accepted"
