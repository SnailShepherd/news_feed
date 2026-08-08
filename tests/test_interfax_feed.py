from collections import defaultdict
from pathlib import Path
import sys

import requests
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import aggregate

FIXTURES = Path(__file__).parent / "fixtures" / "interfax-russia.ru"
FEED_URL = "https://www.interfax-russia.ru/rss/realty/public.rss"
HTML_URL = "https://www.interfax-russia.ru/realty/news?per-page=32"
ARTICLE_URL = "https://www.interfax-russia.ru/realty/news/v-moskve-vveli-novyy-zhiloy-kompleks"
SOURCE = {
    "name": "Интерфакс-Недвижимость",
    "base_url": "https://www.interfax-russia.ru",
    "start_url": FEED_URL,
    "fallback_start_urls": [HTML_URL],
    "include_patterns": ["/realty/news/"],
    "restrict_domain": True,
    "index_format": "rss_atom",
    "feed_content_fallback": True,
    "link_min_text_len": 8,
    "accept_empty_anchor": True,
    "min_words": 100,
}


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch):
    monkeypatch.setattr(aggregate, "STATE", aggregate.ensure_state_keys({}))
    monkeypatch.setattr(aggregate, "SESSION_STATE", aggregate.ensure_session_state_keys({}))
    monkeypatch.setattr(aggregate, "_last_req_at", defaultdict(lambda: 0.0))
    aggregate.SOURCE_SUMMARY.clear()
    aggregate.SOURCE_MIN_WORDS.clear()
    aggregate.SOURCE_MIN_WORDS[SOURCE["name"]] = 100
    yield
    aggregate.SOURCE_SUMMARY.clear()
    aggregate.SOURCE_MIN_WORDS.clear()


def test_official_feed_fields_and_html_candidates_are_deduplicated():
    feed = (FIXTURES / "official-feed.xml").read_text()
    saved_html = (FIXTURES / "saved-news.html").read_text()
    entries = aggregate.parse_rss_atom_index(feed, SOURCE, index_url=FEED_URL)
    html_links, raw, accepted = aggregate.extract_index_links(saved_html, SOURCE, index_url=HTML_URL)

    assert entries
    assert entries[0]["url"] == ARTICLE_URL
    assert entries[0]["title"] == "В Москве ввели новый жилой комплекс"
    assert entries[0]["published"] == "Fri, 07 Aug 2026 12:30:00 +0300"
    assert "содержание134" in entries[0]["content"]
    assert raw > 0 and accepted > 0
    assert all("/realty/news/" in url for url in html_links)
    assert len(set([entry["url"] for entry in entries] + html_links)) == 2


def test_feed_content_is_safe_fallback_when_article_fetch_fails(monkeypatch):
    feed = (FIXTURES / "official-feed.xml").read_text()

    def fetch(url, src=None):
        if url == FEED_URL:
            return feed
        raise requests.ConnectionError("article unavailable")

    monkeypatch.setattr(aggregate, "fetch_page", fetch)
    monkeypatch.setattr(aggregate, "fetch_amp_if_available", lambda *args, **kwargs: (None, None))
    items = aggregate.harvest_source(SOURCE, force=True)

    assert len(items) == 1
    assert items[0]["url"] == ARTICLE_URL
    assert items[0]["published_at"].startswith("2026-08-07T12:30:00")
    assert "содержание134" in items[0]["content_text"]


def test_feed_fetch_failure_diagnostic_and_saved_html_fallback(monkeypatch):
    saved_html = (FIXTURES / "saved-news.html").read_text()

    def fetch(url, src=None):
        if url == FEED_URL:
            raise requests.ConnectionError("feed unavailable")
        if url == HTML_URL:
            return saved_html
        raise requests.ConnectionError("article unavailable")

    monkeypatch.setattr(aggregate, "fetch_page", fetch)
    aggregate.harvest_source(SOURCE, force=True)
    attempts = aggregate.SOURCE_SUMMARY[SOURCE["name"]]["index_attempts"]
    assert attempts[0]["failure_kind"] == "feed_fetch_failure"
    assert attempts[1]["accepted_links"] == 2
    assert ARTICLE_URL in aggregate.STATE["candidate_urls"][SOURCE["name"]]


def test_empty_feed_parse_has_distinct_diagnostic(monkeypatch):
    monkeypatch.setattr(aggregate, "fetch_page", lambda url, src=None: "<rss><channel/></rss>" if url == FEED_URL else (FIXTURES / "saved-news.html").read_text())
    aggregate.harvest_source(SOURCE, force=True)
    attempt = aggregate.SOURCE_SUMMARY[SOURCE["name"]]["index_attempts"][0]
    assert attempt["failure_kind"] == "feed_empty_parse"
