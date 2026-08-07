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
    index = (directory / expected.get("discovery_fixture", "index.html")).read_text()
    fallback = (
        (directory / expected["fallback_fixture"]).read_text()
        if expected.get("fallback_fixture")
        else None
    )
    article = (directory / "article.html").read_text()

    assert expected["capture_date"] == "2026-08-06"

    def fixture_fetch(url, src=None):
        if url == source["start_url"]:
            return index
        if fallback is not None and url in source.get("fallback_start_urls", []):
            return fallback
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

    if source["name"] in {"Гостинформ", "Интерфакс-Недвижимость", "Металлоснабжение и сбыт", "ЕРЗ.РФ", "РИА СТК"}:
        assert expected["capture_kind"] == "sanitized first-party discovery response"


def test_xml_fallback_index_contract(monkeypatch):
    source = next(source for source in SOURCES if source["name"] == "Российская газета: Экономика")
    directory = fixture_dir(source)
    expected = json.loads((directory / "expected.json").read_text())
    xml_index = (directory / "index.xml").read_text()
    article = (directory / "article.html").read_text()
    unrelated = "https://rg.ru/2026/08/06/unrelated-sports-fixture.html"

    def fixture_fetch(url, src=None):
        if url == source["start_url"]:
            return xml_index
        if url == expected["article_url"]:
            return article
        raise AssertionError(f"unrelated sitemap record was fetched: {url}")

    monkeypatch.setattr(aggregate, "fetch_page", fixture_fetch)
    monkeypatch.setattr(aggregate, "fetch_amp_if_available", lambda *args, **kwargs: (None, None))
    items = aggregate.harvest_source(source, force=True)
    assert [item["url"] for item in items] == [expected["article_url"]]
    assert aggregate.STATE["candidate_urls"][source["name"]] == [expected["article_url"]]
    assert unrelated not in aggregate.STATE["candidate_urls"][source["name"]]


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


def test_parser_exception_is_reported_as_crawler_parser_error(monkeypatch, tmp_path):
    source = {
        "name": "Broken parser fixture",
        "start_url": "https://example.test/news",
        "base_url": "https://example.test/",
    }
    monkeypatch.setattr(aggregate, "fetch_page", lambda *args, **kwargs: "<html>valid</html>")
    monkeypatch.setattr(aggregate, "_parse_index_soup", lambda html: (_ for _ in ()).throw(ValueError("parser exploded")))
    monkeypatch.setattr(aggregate, "SOURCE_HEALTH_JSON", tmp_path / "health.json")

    assert aggregate.harvest_source(source, force=True) == []
    aggregate.write_source_health_report([source])
    row = json.loads((tmp_path / "health.json").read_text())["sources"][0]
    assert row["index_fetch_status"] == "parser_error"
    assert row["failure_class"] == "crawler_parser_error"
    assert row["index_attempts"][0]["error"] == "parser exploded"


def test_challenge_response_does_not_overwrite_valid_cached_index(monkeypatch, tmp_path):
    primary = "https://example.test/news"
    fallback = "https://example.test/fallback"
    article_url = "https://example.test/articles/cached-story"
    source = {
        "name": "Cached index fixture",
        "start_url": primary,
        "fallback_start_urls": [fallback],
        "base_url": "https://example.test/",
        "include_patterns": ["/articles/"],
        "restrict_domain": True,
    }
    cached_index = f'<a href="{article_url}">Cached article</a>'
    challenge = '<nav><a href="/login">Log in</a></nav>'
    fallback_index = '<a href="/articles/fallback-story">Fallback article</a>'
    monkeypatch.setattr(aggregate, "PAGES_DIR", tmp_path)
    cache_path = tmp_path / aggregate.cache_key_for(primary)
    cache_path.write_text(cached_index, encoding="utf-8")

    def fixture_fetch(url, src=None):
        if url == primary:
            # Match fetch_page's behavior: a successful response is cached
            # before harvest_source has had a chance to validate its links.
            cache_path.write_text(challenge, encoding="utf-8")
            return challenge
        if url == article_url:
            return "article body"
        if url == fallback:
            return fallback_index
        raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.setattr(aggregate, "fetch_page", fixture_fetch)
    monkeypatch.setattr(
        aggregate,
        "build_item",
        lambda url, *args, **kwargs: {
            "id": "cached-story",
            "source": source["name"],
            "title": "Cached story",
            "url": url,
            "content_text": "cached article body",
            "first_seen": "2026-08-07T00:00:00+00:00",
            "bucketed_at": "2026-08-07T00:00:00+00:00",
            "fetched_at": "2026-08-07T00:00:00+00:00",
        },
    )
    aggregate.SOURCE_MIN_WORDS[source["name"]] = 1

    items = aggregate.harvest_source(source, force=True)

    assert [item["url"] for item in items] == [article_url]
    assert cache_path.read_text(encoding="utf-8") == cached_index
    assert aggregate.SOURCE_SUMMARY[source["name"]]["index_attempts"] == [{
        "url": primary,
        "raw_link_candidates": 1,
        "accepted_links": 1,
        "cached": True,
    }]
    assert aggregate.SOURCE_SUMMARY[source["name"]]["cached_fallback_used"] is True
@pytest.mark.parametrize(
    "source",
    [source for source in SOURCES if source["name"] in {
        "Гостинформ", "Интерфакс-Недвижимость", "Металлоснабжение и сбыт", "ЕРЗ.РФ", "РИА СТК"
    }],
    ids=lambda source: source["name"],
)
def test_configured_fallback_discovery_contract(source, monkeypatch):
    """Each configured alternate index remains a usable production discovery path."""
    directory = fixture_dir(source)
    expected = json.loads((directory / "expected.json").read_text())
    index = (directory / expected["fallback_fixture"]).read_text()
    article = (directory / "article.html").read_text()
    fallback_url = source["fallback_start_urls"][0]
    fallback_source = dict(source, start_url=fallback_url, fallback_start_urls=[])

    def fixture_fetch(url, src=None):
        if url == fallback_url:
            return index
        if url.rstrip("/") == expected["article_url"].rstrip("/"):
            return article
        raise AssertionError(f"offline fallback attempted an unexpected request: {url}")

    monkeypatch.setattr(aggregate, "fetch_page", fixture_fetch)
    monkeypatch.setattr(aggregate, "fetch_amp_if_available", lambda *args, **kwargs: (None, None))
    items = aggregate.harvest_source(fallback_source, force=True)
    assert expected["article_url"] in [item["url"] for item in items]


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
