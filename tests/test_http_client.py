import pathlib
import sys
import time

import pytest
import requests
from requests.cookies import RequestsCookieJar

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts.http_client import (
    DEFAULT_USER_AGENT,
    HostClient,
    RequestStrategy,
    SourceTemporarilyUnavailable,
    WarmupConfig,
    build_strategy_registry,
)


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


def _cookie_jar_with(name: str) -> RequestsCookieJar:
    jar = RequestsCookieJar()
    jar.set(name, "value", domain="example.com", path="/")
    return jar


class RecordingSession(DummySession):
    def __init__(self, results):
        super().__init__(results)
        self.calls = []

    def get(self, *args, **kwargs):
        self.calls.append(kwargs)
        return super().get(*args, **kwargs)


def test_warmup_accepts_401_with_ddos_cookies(monkeypatch):
    state = {}
    warmup = WarmupConfig(url="https://example.com/warm", delay_range=(0.0, 0.0))
    strategy = RequestStrategy(warmup=warmup, selenium_fallback=True)
    client = HostClient("example.com", strategy, state)
    response_cookies = _cookie_jar_with("__ddgid")
    client._session = DummySession(
        [
            DummyResponse(status_code=401, cookies=response_cookies),
            DummyResponse(status_code=200),
        ]
    )
    monkeypatch.setattr(time, "sleep", lambda *_args, **_kwargs: None)

    response = client.get("https://example.com/data", headers={})

    assert response.status_code == 200
    assert client.state_root["warmup_done"] is True
    assert client.state_root["cookies"]
    warmup_stats = state["stats"]["metrics"]["example.com"]["warmup"]
    assert warmup_stats["result"] == "http_4xx_with_cookies"
    assert "__ddg" in "".join(warmup_stats["cookies"])


def test_warmup_uses_default_headers(monkeypatch):
    state = {}
    warmup = WarmupConfig(url="https://example.com/warm", delay_range=(0.0, 0.0))
    strategy = RequestStrategy(warmup=warmup)
    client = HostClient("example.com", strategy, state)
    session = RecordingSession([DummyResponse(status_code=200), DummyResponse(status_code=200)])
    client._session = session
    monkeypatch.setattr(time, "sleep", lambda *_args, **_kwargs: None)

    response = client.get("https://example.com/data", headers={})

    assert response.status_code == 200
    assert session.calls
    headers = session.calls[0].get("headers")
    assert headers is not None
    assert headers.get("User-Agent") == DEFAULT_USER_AGENT
    assert headers.get("Accept")
    assert headers.get("Accept-Language")


def test_warmup_401_without_cookies_uses_selenium(monkeypatch):
    state = {}
    warmup = WarmupConfig(url="https://example.com/warm", delay_range=(0.0, 0.0))
    strategy = RequestStrategy(warmup=warmup, selenium_fallback=True)
    client = HostClient("example.com", strategy, state)
    client._session = DummySession(
        [
            DummyResponse(status_code=401),
            DummyResponse(status_code=200),
        ]
    )
    selenium_called = {"count": 0}

    def fake_selenium(url):
        selenium_called["count"] += 1
        client._session.cookies.set("__ddgid", "value", domain="example.com", path="/")
        client._store_cookies()
        return True

    monkeypatch.setattr(client, "_selenium_warmup", fake_selenium)
    monkeypatch.setattr(time, "sleep", lambda *_args, **_kwargs: None)

    response = client.get("https://example.com/data", headers={})

    assert response.status_code == 200
    assert selenium_called["count"] == 1
    assert client.state_root["warmup_done"] is True
    warmup_stats = state["stats"]["metrics"]["example.com"]["warmup"]
    assert warmup_stats["result"] == "selenium_success"


def test_warmup_401_without_cookies_and_failed_selenium(monkeypatch):
    state = {}
    warmup = WarmupConfig(url="https://example.com/warm", delay_range=(0.0, 0.0))
    strategy = RequestStrategy(warmup=warmup, selenium_fallback=True)
    client = HostClient("example.com", strategy, state)
    client._session = DummySession(
        [
            DummyResponse(status_code=401),
        ]
    )

    def fake_selenium(url):
        return False

    monkeypatch.setattr(client, "_selenium_warmup", fake_selenium)
    monkeypatch.setattr(time, "sleep", lambda *_args, **_kwargs: None)

    with pytest.raises(SourceTemporarilyUnavailable):
        client.get("https://example.com/data", headers={})

    warmup_stats = state["stats"]["metrics"]["example.com"]["warmup"]
    assert warmup_stats["result"] == "selenium_failed"
    assert client.state_root.get("warmup_done") is not True
