import pathlib
import sys
import time

import pytest
import requests
from requests.cookies import RequestsCookieJar

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts.http_client import HostClient, RequestStrategy, SourceTemporarilyUnavailable, build_strategy_registry


class DummyResponse:
    def __init__(self, status_code=200, text="ok", headers=None, cookies=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self.cookies = cookies or RequestsCookieJar()

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"status {self.status_code}")


class DummySession:
    def __init__(self, results):
        self.results = list(results)
        self.cookies = RequestsCookieJar()

    def get(self, *args, **kwargs):
        if not self.results:
            raise AssertionError("unexpected call")
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        pass


def test_build_strategy_registry_parses_config():
    sources = [
        {
            "name": "Example",
            "base_url": "https://example.com",
            "request_strategy": {
                "connect_timeout": 3,
                "read_timeout": 10,
                "max_attempts": 5,
                "retry_statuses": [403],
            },
        }
    ]

    strategies = build_strategy_registry(sources)

    assert "example.com" in strategies
    strat = strategies["example.com"]
    assert strat.connect_timeout == 3
    assert strat.read_timeout == 10
    assert strat.max_attempts == 5
    assert strat.retry_statuses == [403]


def test_host_client_retries_and_records_metrics(monkeypatch):
    state = {}
    strategy = RequestStrategy(max_attempts=2, backoff_factor=0)
    client = HostClient("example.com", strategy, state)
    client._session = DummySession(
        [
            requests.exceptions.ConnectionError("Network is unreachable"),
            DummyResponse(status_code=200, headers={"ETag": "abc"}),
        ]
    )
    monkeypatch.setattr(client, "_perform_dns_lookup", lambda url: 1.0)
    monkeypatch.setattr(time, "sleep", lambda *_args, **_kwargs: None)

    response = client.get("https://example.com/test", headers={})

    assert response.status_code == 200
    metrics = state["stats"]["metrics"]["example.com"]
    assert metrics["attempts"] == 2
    assert metrics["status"] == 200


def test_host_client_raises_after_failures(monkeypatch):
    state = {}
    strategy = RequestStrategy(max_attempts=2, backoff_factor=0)
    client = HostClient("fail.example", strategy, state)
    client._session = DummySession(
        [
            requests.exceptions.ConnectionError("Network is unreachable"),
            requests.exceptions.ConnectionError("Network is unreachable"),
        ]
    )
    monkeypatch.setattr(client, "_perform_dns_lookup", lambda url: 1.0)
    monkeypatch.setattr(time, "sleep", lambda *_args, **_kwargs: None)

    with pytest.raises(SourceTemporarilyUnavailable):
        client.get("https://fail.example/path", headers={})


def test_build_strategy_registry_skips_without_base_url():
    sources = [
        {"name": "Broken", "request_strategy": {"max_attempts": 2}},
        {"name": "Ok", "base_url": "https://ok.example", "request_strategy": {"max_attempts": 2}},
    ]

    strategies = build_strategy_registry(sources)

    assert "ok.example" in strategies
    assert "Broken" not in strategies
