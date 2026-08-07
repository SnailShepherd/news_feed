"""Offline extraction contracts captured on 2026-08-06.

The small fixtures intentionally retain only markup which exercises crawler behavior;
``expected.json`` beside each capture records its provenance date and assertions.
"""

import json
from collections import defaultdict
from pathlib import Path
import sys
from urllib.parse import urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import aggregate

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
SOURCES = [source for source in json.loads((ROOT / "sources.json").read_text()) if source.get("enabled")]


def fixture_dir(source):
    return FIXTURES / (urlparse(source["base_url"]).hostname or "").removeprefix("www.")


@pytest.fixture(autouse=True)
def isolated_crawler_state(monkeypatch):
    monkeypatch.setattr(aggregate, "STATE", aggregate.ensure_state_keys({}))
    monkeypatch.setattr(aggregate, "_last_req_at", defaultdict(lambda: 0.0))
    aggregate.SOURCE_SUMMARY.clear()
    aggregate.SOURCE_MIN_WORDS.clear()
    for source in SOURCES:
        aggregate.SOURCE_MIN_WORDS[source["name"]] = int(
            source.get("min_words", aggregate.DEFAULT_MIN_WORDS)
        )
    yield
    aggregate.SOURCE_SUMMARY.clear()
    aggregate.SOURCE_MIN_WORDS.clear()


@pytest.mark.parametrize("source", SOURCES, ids=lambda source: source["name"])
def test_source_index_and_article_contract(source, monkeypatch):
    """Use production harvesting with captured index and article responses only."""
    directory = fixture_dir(source)
    expected = json.loads((directory / "expected.json").read_text())
    index = (directory / "index.html").read_text()
    article = (directory / "article.html").read_text()

    assert expected["capture_date"] == "2026-08-06"

    def fixture_fetch(url, src=None):
        if url == source["start_url"]:
            return index
        if url.rstrip("/") == expected["article_url"].rstrip("/"):
            return article
        raise AssertionError(f"offline contract attempted an unexpected request: {url}")

    monkeypatch.setattr(aggregate, "fetch_page", fixture_fetch)
    monkeypatch.setattr(aggregate, "fetch_amp_if_available", lambda *args, **kwargs: (None, None))

    items = aggregate.harvest_source(source, force=True)
    matching = [item for item in items if item["url"] == expected["article_url"]]
    assert matching, "index extraction did not find the expected article URL"
    item = matching[0]
    assert item["title"] == expected["title"]
    assert item.get("canonical_url", item["url"].rstrip("/")) == expected["canonical_url"]
    assert item["published_at"] == expected["published_at"]
    assert aggregate._word_count(item["content_text"]) > int(
        source.get("min_words", aggregate.DEFAULT_MIN_WORDS)
    )


def test_xml_fallback_index_contract(monkeypatch):
    source = next(source for source in SOURCES if source["name"] == "Российская газета: Экономика")
    directory = fixture_dir(source)
    expected = json.loads((directory / "expected.json").read_text())
    xml_index = (directory / "index.xml").read_text()
    article = (directory / "article.html").read_text()
    monkeypatch.setattr(
        aggregate,
        "fetch_page",
        lambda url, src=None: xml_index if url == source["start_url"] else article,
    )
    monkeypatch.setattr(aggregate, "fetch_amp_if_available", lambda *args, **kwargs: (None, None))
    assert expected["article_url"] in [item["url"] for item in aggregate.harvest_source(source, force=True)]


def test_fallback_skips_anchor_page_without_accepted_articles(monkeypatch, tmp_path):
    primary = "https://example.test/challenge"
    fallback = "https://example.test/news"
    article_url = "https://example.test/articles/valid-story"
    source = {
        "name": "Fallback policy fixture",
        "start_url": primary,
        "fallback_start_urls": [fallback],
        "base_url": "https://example.test/",
        "include_patterns": ["/articles/"],
        "include_regex": r"/articles/[a-z-]+$",
        "exclude_regex": r"/articles/(?:archive|tags)/",
        "restrict_domain": True,
        "link_min_text_len": 8,
        "min_words": 1,
    }
    pages = {
        primary: '<nav><a href="/login">Log in here</a><a href="/help">Help center</a></nav>',
        fallback: (
            '<a href="/news">News index</a>'
            '<a href="https://elsewhere.test/articles/wrong-domain">Wrong domain</a>'
            '<a href="/articles/archive/old-story">Archived story</a>'
            f'<a href="{article_url}">A valid article</a>'
        ),
        article_url: "<html><head><title>Valid story</title></head>"
        "<body><article><p>This is a sufficiently complete article body for the fixture.</p>"
        "<p>It verifies fallback selection after filtering links.</p>"
        "<p>The final paragraph makes extraction deterministic.</p></article></body></html>",
    }
    calls = []

    def fixture_fetch(url, src=None):
        calls.append(url)
        return pages[url]

    monkeypatch.setattr(aggregate, "fetch_page", fixture_fetch)
    monkeypatch.setattr(aggregate, "fetch_amp_if_available", lambda *args, **kwargs: (None, None))
    monkeypatch.setattr(aggregate, "SOURCE_HEALTH_JSON", tmp_path / "source-health.json")
    monkeypatch.setattr(aggregate, "save_state", lambda: None)
    monkeypatch.setattr(aggregate, "prune_page_cache", lambda: (0, 0))
    aggregate.SOURCE_MIN_WORDS[source["name"]] = 1

    items = aggregate.harvest_source(source, force=True)

    assert [item["url"] for item in items] == [article_url]
    assert calls[:2] == [primary, fallback]
    assert article_url in calls
    aggregate.write_source_health_report([source])
    health = json.loads((tmp_path / "source-health.json").read_text())["sources"][0]
    assert health["index_attempts"] == [
        {"url": primary, "raw_link_candidates": 2, "accepted_links": 0},
        {"url": fallback, "raw_link_candidates": 4, "accepted_links": 1},
    ]


def test_faufcc_api_response_contract(monkeypatch):
    source = next(source for source in SOURCES if source["name"] == "ФАУ ФЦС")
    directory = fixture_dir(source)
    expected = json.loads((directory / "expected.json").read_text())
    payload = json.loads((directory / "api.json").read_text())

    class FixtureResponse:
        status_code = 200
        headers = {}
        text = json.dumps(payload, ensure_ascii=False)

        def json(self):
            return payload

        def raise_for_status(self):
            return None

    monkeypatch.setattr(aggregate.SESSION, "get", lambda *args, **kwargs: FixtureResponse())
    monkeypatch.setattr(aggregate, "fetch_amp_if_available", lambda *args, **kwargs: (None, None))
    items = aggregate.harvest_json_source(source, force=True)
    item = next(item for item in items if item["url"] == expected["article_url"])
    assert item["title"] == expected["title"]
    assert item["published_at"] == expected["published_at"]
    assert aggregate._word_count(item["content_text"]) > source["min_words"]
