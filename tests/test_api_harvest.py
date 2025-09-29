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
    }
    monkeypatch.setattr(aggregate, "STATE", state)
    aggregate.SOURCE_SUMMARY.clear()
    monkeypatch.setattr(aggregate, "_last_req_at", defaultdict(lambda: 0.0))
    yield
    aggregate.SOURCE_SUMMARY.clear()


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
