import json
from collections import defaultdict

import pytest

from scripts import aggregate


class DummyResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.headers = {}
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    state = {
        "headers": {},
        "stats": {},
        "index_hash": {},
        "seen_urls": {},
        "first_seen": {},
        "aliases": {},
        "content_hashes": {},
        "canonical_item_ids": {},
    }
    monkeypatch.setattr(aggregate, "STATE", state)
    aggregate.SOURCE_SUMMARY.clear()
    aggregate.SOURCE_MIN_WORDS.clear()
    monkeypatch.setattr(aggregate, "_last_req_at", defaultdict(lambda: 0.0))
    yield
    aggregate.SOURCE_SUMMARY.clear()
    aggregate.SOURCE_MIN_WORDS.clear()


def test_faufcc_api_mapping(monkeypatch):
    body_text = " ".join(["Абзац"] * 110)
    payload = {
        "data": [
            {
                "id": "123",
                "link": "/press-tsentr/novosti/test-material",
                "name": "API Заголовок",
                "publishedAt": "2024-10-01T12:30:00+03:00",
                "content": f"<p>{body_text}</p><p>Читайте нас в соцсетях</p>",
            }
        ]
    }

    def fake_get(url, headers=None, timeout=None):
        return DummyResponse(payload)

    monkeypatch.setattr(aggregate.SESSION, "get", fake_get)

    src = {
        "name": "ФАУ ФЦС",
        "base_url": "https://faufcc.ru",
        "start_url": "https://faufcc.ru/press-tsentr/novosti",
        "api_endpoint": "https://api.faufcc.ru/api/publications?type=news&page=1&limit=10",
        "content_selectors": [".press-center__detail"],
        "min_words": 90,
    }

    items = aggregate.harvest_json_source(src, force=True)
    assert len(items) == 1
    item = items[0]
    assert item["published_at"] == "2024-10-01T12:30:00+03:00"
    assert item["url"].startswith("https://faufcc.ru/press-tsentr/novosti/")
    assert "Читайте нас" not in item["content_text"]
    assert aggregate._word_count(item["content_text"]) >= 110


def test_faufcc_api_falls_back_to_html(monkeypatch):
    payload = {
        "data": [
            {
                "id": "234",
                "link": "/press-tsentr/novosti/test-short",
                "name": "Короткая карточка",
                "publishedAt": "2024-10-02T09:00:00+03:00",
                "summary": "Короткий анонс",
            }
        ]
    }

    body_words = " ".join(["Полный" for _ in range(80)])
    long_html = (
        "<html><head><title>HTML версия</title></head><body>"
        "<article class='press-center__detail'>"
        f"<p>{body_words} текст публикации.</p>"
        "<p>Поделиться материалом</p>"
        "</article></body></html>"
    )

    call_counter = {"fetch": 0}

    def fake_get(url, headers=None, timeout=None):
        return DummyResponse(payload)

    def fake_fetch_page(url, src=None):
        call_counter["fetch"] += 1
        return long_html

    monkeypatch.setattr(aggregate.SESSION, "get", fake_get)
    monkeypatch.setattr(aggregate, "fetch_page", fake_fetch_page)
    aggregate.SOURCE_MIN_WORDS["ФАУ ФЦС"] = 70

    src = {
        "name": "ФАУ ФЦС",
        "base_url": "https://faufcc.ru",
        "start_url": "https://faufcc.ru/press-tsentr/novosti",
        "api_endpoint": "https://api.faufcc.ru/api/publications?type=news&page=1&limit=5",
        "content_selectors": [".press-center__detail"],
    }

    items = aggregate.harvest_json_source(src, force=True)
    assert call_counter["fetch"] == 1
    assert len(items) == 1
    item = items[0]
    assert aggregate._word_count(item["content_text"]) >= 70
    assert "Поделиться" not in item["content_text"]
    # API payload should not be counted when HTML fallback wins
    assert aggregate.SOURCE_SUMMARY[src["name"]]["api"] == 0


def test_api_empty_falls_back_to_html(monkeypatch):
    payload = {"data": []}

    def fake_get(url, headers=None, timeout=None):
        return DummyResponse(payload)

    fallback_calls = {"count": 0}

    fallback_item = {
        "id": "legacy-item",
        "source": "ФАУ ФЦС",
        "title": "HTML карточка",
        "url": "https://faufcc.ru/press-tsentr/novosti/html-version",
        "content_text": " ".join(["Абзац" for _ in range(40)]),
        "first_seen": "2024-10-05T10:00:00+03:00",
        "bucketed_at": "2024-10-05T10:00:00+03:00",
        "fetched_at": "2024-10-05T10:00:00+03:00",
    }

    def fake_harvest_source(src_arg, force=False):
        fallback_calls["count"] += 1
        assert src_arg is src
        assert not force
        return [fallback_item]

    monkeypatch.setattr(aggregate.SESSION, "get", fake_get)
    monkeypatch.setattr(aggregate, "harvest_source", fake_harvest_source)
    aggregate.SOURCE_MIN_WORDS["ФАУ ФЦС"] = 70

    src = {
        "name": "ФАУ ФЦС",
        "base_url": "https://faufcc.ru",
        "start_url": "https://faufcc.ru/press-tsentr/novosti",
        "api_endpoint": "https://api.faufcc.ru/api/publications?type=news&page=1&limit=10",
        "content_selectors": [".press-center__detail"],
        "html_fallback_on_empty_api": True,
    }

    items = aggregate.harvest_json_source(src, force=False)
    assert fallback_calls["count"] == 1
    assert items == [fallback_item]


def test_api_short_payload_triggers_html_fallback(monkeypatch):
    payload = {
        "data": [
            {
                "id": "345",
                "link": "/press-tsentr/novosti/short-payload",
                "name": "Короткая API карточка",
                "publishedAt": "2024-10-03T09:30:00+03:00",
                "summary": "Очень короткое описание",
            }
        ]
    }

    html_short = "<html><body><article class='press-center__detail'><p>Коротко</p></article></body></html>"

    call_counter = {"fetch": 0, "fallback": 0}

    def fake_get(url, headers=None, timeout=None):
        return DummyResponse(payload)

    def fake_fetch_page(url, src=None):
        call_counter["fetch"] += 1
        return html_short

    fallback_item = {
        "id": "fallback-item",
        "source": "ФАУ ФЦС",
        "title": "HTML список",
        "url": "https://faufcc.ru/press-tsentr/novosti/full-html",
        "content_text": " ".join(["Полный", "текст"] * 20),
        "first_seen": "2024-10-05T10:00:00+03:00",
        "bucketed_at": "2024-10-05T10:00:00+03:00",
        "fetched_at": "2024-10-05T10:00:00+03:00",
    }

    def fake_harvest_source(src_arg, force=False):
        call_counter["fallback"] += 1
        assert src_arg is src
        return [fallback_item]

    monkeypatch.setattr(aggregate.SESSION, "get", fake_get)
    monkeypatch.setattr(aggregate, "fetch_page", fake_fetch_page)
    monkeypatch.setattr(aggregate, "harvest_source", fake_harvest_source)
    aggregate.SOURCE_MIN_WORDS["ФАУ ФЦС"] = 120

    src = {
        "name": "ФАУ ФЦС",
        "base_url": "https://faufcc.ru",
        "start_url": "https://faufcc.ru/press-tsentr/novosti",
        "api_endpoint": "https://api.faufcc.ru/api/publications?type=news&page=1&limit=5",
        "content_selectors": [".press-center__detail"],
        "html_fallback_on_empty_api": True,
    }

    items = aggregate.harvest_json_source(src, force=False)
    assert call_counter["fetch"] == 1
    assert call_counter["fallback"] == 1
    assert items == [fallback_item]
