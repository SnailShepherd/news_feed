from collections import defaultdict
import json
from pathlib import Path
import sys

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import aggregate

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "metalinfo.ru"
SOURCE = next(s for s in json.loads((ROOT / "sources.json").read_text()) if s["name"] == "Металлоснабжение и сбыт")
FEED_URL = "https://www.metalinfo.ru/ru/news/list.rss"
ARTICLE_URL = "https://www.metalinfo.ru/ru/news/123456"


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    monkeypatch.setattr(aggregate, "STATE", aggregate.ensure_state_keys({}))
    monkeypatch.setattr(aggregate, "SESSION_STATE", aggregate.ensure_session_state_keys({}))
    monkeypatch.setattr(aggregate, "_last_req_at", defaultdict(lambda: 0.0))
    monkeypatch.setattr(aggregate, "PAGES_DIR", tmp_path)
    aggregate.SOURCE_SUMMARY.clear()
    aggregate.SOURCE_MIN_WORDS.clear()
    aggregate.SOURCE_MIN_WORDS[SOURCE["name"]] = aggregate.DEFAULT_MIN_WORDS
    yield
    aggregate.SOURCE_SUMMARY.clear()
    aggregate.SOURCE_MIN_WORDS.clear()


def test_rss_discovery_fields_dates_and_numeric_url_normalization():
    entries = aggregate.parse_rss_atom_index((FIXTURES / "index.rss").read_text(), SOURCE, index_url=FEED_URL)

    assert [entry["url"] for entry in entries] == [ARTICLE_URL]
    assert entries[0]["title"] == "Контрактная новость — Металлоснабжение и сбыт"
    assert entries[0]["published"] == "Wed, 05 Aug 2026 10:30:00 +0300"
    assert entries[0]["content_field"] == "encoded"
    assert "лента129" in entries[0]["content"]


def test_numeric_normalization_does_not_rewrite_an_external_feed_link():
    feed = """<rss><channel><item>
      <title>External article</title>
      <link>https://external.example/ru/news/999999.html</link>
      <pubDate>Wed, 05 Aug 2026 10:30:00 +0300</pubDate>
      <description>Untrusted content.</description>
    </item></channel></rss>"""

    assert aggregate.parse_rss_atom_index(feed, SOURCE, index_url=FEED_URL) == []


def test_adequate_feed_content_fallback_reports_article_degradation(monkeypatch):
    feed = (FIXTURES / "index.rss").read_text()
    monkeypatch.setattr(aggregate, "fetch_page", lambda url, src=None: feed if url == FEED_URL else (_ for _ in ()).throw(requests.ConnectionError("blocked")))

    items = aggregate.harvest_source(SOURCE, force=True)

    assert [item["url"] for item in items] == [ARTICLE_URL]
    assert items[0]["published_at"].startswith("2026-08-05T10:30:00")
    summary = aggregate.SOURCE_SUMMARY[SOURCE["name"]]
    assert summary["article_fetch_degradations"] == 1
    assert summary["feed_content_fallbacks"] == 1
    assert aggregate.SESSION_STATE["stats"]["errors"][0]["failure_kind"] == "article_fetch_degradation"


@pytest.mark.parametrize("missing", ["content", "title", "published"])
def test_inadequate_feed_and_unavailable_html_has_clear_diagnostic(monkeypatch, missing):
    feed = (FIXTURES / "index.rss").read_text()
    if missing == "content":
        feed = feed.replace(" ".join(f"лента{i}" for i in range(130)), "слишком кратко")
    elif missing == "title":
        feed = feed.replace("<title>Контрактная новость — Металлоснабжение и сбыт</title>", "<title></title>")
    else:
        feed = feed.replace("<pubDate>Wed, 05 Aug 2026 10:30:00 +0300</pubDate>", "<pubDate></pubDate>")
    monkeypatch.setattr(aggregate, "fetch_page", lambda url, src=None: feed if url == FEED_URL else (_ for _ in ()).throw(requests.ConnectionError("blocked")))

    assert aggregate.harvest_source(SOURCE, force=True) == []
    summary = aggregate.SOURCE_SUMMARY[SOURCE["name"]]
    assert summary["article_fetch_degradations"] == 1
    assert summary["feed_content_fallbacks"] == 0
    assert summary["last_error"] == "article fetch unavailable: blocked"
    assert aggregate.STATE["url_states"][SOURCE["name"]][ARTICLE_URL]["status"] == "retryable_failure"
