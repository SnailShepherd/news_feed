from collections import defaultdict
from pathlib import Path
import sys
import time

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


def test_atom_prefers_alternate_link_and_published_date():
    atom = f'''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
      <title>Atom article</title>
      <link rel="self" href="{FEED_URL}"/>
      <updated>2026-08-08T14:00:00+03:00</updated>
      <link rel="alternate" href="{ARTICLE_URL}"/>
      <published>2026-08-07T12:30:00+03:00</published>
      <content>Complete article content.</content>
    </entry></feed>'''

    entries = aggregate.parse_rss_atom_index(atom, SOURCE, index_url=FEED_URL)

    assert entries[0]["url"] == ARTICLE_URL
    assert entries[0]["published"] == "2026-08-07T12:30:00+03:00"


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


def test_cached_feed_uses_feed_parser_and_content_fallback(monkeypatch, tmp_path):
    feed = (FIXTURES / "official-feed.xml").read_text()
    monkeypatch.setattr(aggregate, "PAGES_DIR", tmp_path)
    (tmp_path / aggregate.cache_key_for(FEED_URL)).write_text(feed)
    aggregate.SESSION_STATE["stats"] = {"cooldowns": {FEED_URL: time.time() + 3600}}

    def fail_if_network_used(*args, **kwargs):
        raise AssertionError("network used during cooldown")

    monkeypatch.setattr(aggregate, "fetch_page", fail_if_network_used)

    items = aggregate.harvest_source(SOURCE, force=True)

    assert [item["url"] for item in items] == [ARTICLE_URL]
    assert "содержание134" in items[0]["content_text"]
    assert aggregate.SOURCE_SUMMARY[SOURCE["name"]]["index_fetch_status"] == "cached"


def test_invalid_fresh_feed_restores_prior_cached_feed(monkeypatch, tmp_path):
    feed = (FIXTURES / "official-feed.xml").read_text()
    cache_path = tmp_path / aggregate.cache_key_for(FEED_URL)
    cache_path.write_text(feed)
    monkeypatch.setattr(aggregate, "PAGES_DIR", tmp_path)

    def fetch(url, src=None):
        if url == FEED_URL:
            cache_path.write_text("<rss><channel/></rss>")
            return "<rss><channel/></rss>"
        raise requests.ConnectionError("article unavailable")

    monkeypatch.setattr(aggregate, "fetch_page", fetch)
    items = aggregate.harvest_source(SOURCE, force=True)

    assert [item["url"] for item in items] == [ARTICLE_URL]
    assert cache_path.read_text() == feed
    attempt = aggregate.SOURCE_SUMMARY[SOURCE["name"]]["index_attempts"][0]
    assert attempt["cached"] is True
    assert attempt["failure_kind"] == "feed_empty_parse"
