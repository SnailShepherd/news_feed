import pathlib
import sys
import time
import types

import pytest
import requests
from requests.cookies import RequestsCookieJar

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts.http_client import (
    DEFAULT_USER_AGENT,
    HostClient,
    CrawlerParserError,
    RequestStrategy,
    SourceTemporarilyUnavailable,
    WarmupConfig,
    build_strategy_registry,
)


def test_selenium_non_string_page_source_is_crawler_parser_error(monkeypatch):
    client = HostClient(
        "example.com", RequestStrategy(selenium_fallback=True), {}
    )

    class Driver:
        page_source = {"unexpected": "payload"}

        def set_page_load_timeout(self, seconds):
            self.page_load_timeout = seconds

        def get(self, url):
            pass

        def get_cookies(self):
            return []

        def quit(self):
            pass

    selenium = types.ModuleType("selenium")
    selenium.webdriver = types.SimpleNamespace(Chrome=lambda options: Driver())
    monkeypatch.setitem(sys.modules, "selenium", selenium)
    monkeypatch.setattr("scripts.http_client.importlib.util.find_spec", lambda name: object())
    monkeypatch.setattr(client, "_build_chrome_options", lambda: object())
    monkeypatch.setattr(client, "_apply_selenium_rendering", lambda driver, url: driver.get(url))

    with pytest.raises(CrawlerParserError) as exc_info:
        client.fetch_html_with_selenium("https://example.com/index")

    message = str(exc_info.value)
    assert "selenium.page_source" in message
    assert "https://example.com/index" in message
    assert "dict" in message


def test_selenium_host_budget_preserves_unrelated_host_opportunity(monkeypatch):
    import scripts.http_client as http_client

    monkeypatch.setattr(http_client, "SELENIUM_RUN_BUDGET_SECONDS", 90.0)
    monkeypatch.setattr(http_client, "SELENIUM_HOST_BUDGET_SECONDS", 15.0)
    monkeypatch.setattr(http_client, "_SELENIUM_SECONDS_USED", 0.0)
    monkeypatch.setattr(http_client, "_SELENIUM_SECONDS_BY_HOST", {})
    monkeypatch.setattr(http_client.time, "monotonic", lambda: 16.0)

    early = HostClient("slow.example", RequestStrategy(selenium_fallback=True), {})
    later = HostClient("later.example", RequestStrategy(selenium_fallback=True), {})
    early._record_selenium_runtime(0.0)

    assert not early._selenium_budget_available()
    assert later._selenium_budget_available()

    class Driver:
        def set_page_load_timeout(self, seconds):
            self.timeout = seconds

    driver = Driver()
    assert later._configure_selenium_deadline(driver)
    assert driver.timeout == 15.0


def test_selenium_startup_overrun_skips_remaining_attempt(monkeypatch, caplog):
    import scripts.http_client as http_client

    monkeypatch.setattr(http_client, "SELENIUM_RUN_BUDGET_SECONDS", 90.0)
    monkeypatch.setattr(http_client, "SELENIUM_HOST_BUDGET_SECONDS", 15.0)
    monkeypatch.setattr(http_client, "_SELENIUM_SECONDS_USED", 0.0)
    monkeypatch.setattr(http_client, "_SELENIUM_SECONDS_BY_HOST", {})
    clock = {"now": 0.0}
    monkeypatch.setattr(http_client.time, "monotonic", lambda: clock["now"])
    client = HostClient("slow.example", RequestStrategy(selenium_fallback=True), {})

    class Driver:
        def set_page_load_timeout(self, seconds):
            raise AssertionError("an exhausted attempt must not receive a navigation timeout")

        def quit(self):
            pass

    def start_slow_chrome(options):
        clock["now"] = 16.0
        return Driver()

    selenium = types.ModuleType("selenium")
    selenium.webdriver = types.SimpleNamespace(Chrome=start_slow_chrome)
    monkeypatch.setitem(sys.modules, "selenium", selenium)
    monkeypatch.setattr("scripts.http_client.importlib.util.find_spec", lambda name: object())
    monkeypatch.setattr(client, "_build_chrome_options", lambda: object())
    monkeypatch.setattr(
        client,
        "_apply_selenium_rendering",
        lambda driver, url: pytest.fail("navigation must be skipped after startup overrun"),
    )

    with caplog.at_level("WARNING"):
        assert client.fetch_html_with_selenium("https://slow.example/") is None

    assert "Selenium startup budget overrun for slow.example" in caplog.text
    assert "skip navigation" in caplog.text
    assert http_client._SELENIUM_SECONDS_USED == 16.0
    assert http_client._SELENIUM_SECONDS_BY_HOST == {"slow.example": 16.0}


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
            requests.exceptions.ReadTimeout("read timeout"),
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


def test_host_client_retries_literal_script_cookie_challenge(monkeypatch):
    state = {}
    strategy = RequestStrategy(max_attempts=2, backoff_factor=0)
    client = HostClient("example.com", strategy, state)
    challenge = (
        "<script>document.cookie='realauth=token' + '; path=/; SameSite=Lax';"
        "location.reload();</script>"
    )
    client._session = DummySession(
        [DummyResponse(text=challenge), DummyResponse(text="<a href='/news/1'>News</a>")]
    )
    monkeypatch.setattr(client, "_perform_dns_lookup", lambda url: 1.0)

    response = client.get("https://example.com/news", headers={})

    assert response.text == "<a href='/news/1'>News</a>"
    assert client._session.cookies.get("realauth") == "token"
    attempts = state["stats"]["metrics"]["example.com"]["attempt_log"]
    assert attempts[0]["script_cookie_challenge"] is True


def test_host_client_does_not_evaluate_nonliteral_script_cookie():
    state = {}
    client = HostClient("example.com", RequestStrategy(max_attempts=2), state)
    response = DummyResponse(
        text="<script>document.cookie=makeCookie();location.reload();</script>"
    )

    assert client._apply_script_cookie_challenge(response, "https://example.com") is False
    assert not client._session.cookies


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


def test_read_timeout_activates_host_cooldown(monkeypatch):
    state = {}
    strategy = RequestStrategy(max_attempts=1, backoff_factor=0)
    client = HostClient("slow.example", strategy, state)
    client._session = DummySession([requests.exceptions.ReadTimeout("read timed out")])
    monkeypatch.setattr(client, "_perform_dns_lookup", lambda url: 1.0)

    with pytest.raises(SourceTemporarilyUnavailable):
        client.get("https://slow.example/first", headers={})

    assert client.state_root["failures"]["network_cooldown_until"] > time.time()
    with pytest.raises(SourceTemporarilyUnavailable, match="cooldown active"):
        client.get("https://slow.example/second", headers={})


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


def test_warmup_accepts_401_with_qrator_cookies(monkeypatch):
    state = {}
    warmup = WarmupConfig(url="https://example.com/warm", delay_range=(0.0, 0.0))
    strategy = RequestStrategy(warmup=warmup, selenium_fallback=True)
    client = HostClient("example.com", strategy, state)
    response_cookies = _cookie_jar_with("qrator_jsr")
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
    assert "qrator" in "".join(warmup_stats["cookies"])


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

    def fake_selenium(url, force=False):
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

    def fake_selenium(url, force=False):
        return False

    monkeypatch.setattr(client, "_selenium_warmup", fake_selenium)
    monkeypatch.setattr(time, "sleep", lambda *_args, **_kwargs: None)

    with pytest.raises(SourceTemporarilyUnavailable):
        client.get("https://example.com/data", headers={})

    warmup_stats = state["stats"]["metrics"]["example.com"]["warmup"]
    assert warmup_stats["result"] == "selenium_failed"
    assert client.state_root.get("warmup_done") is not True


def test_retry_status_401_triggers_selenium(monkeypatch):
    state = {}
    strategy = RequestStrategy(max_attempts=2, backoff_factor=0, retry_statuses=[401], selenium_fallback=True)
    client = HostClient("example.com", strategy, state)
    client._session = DummySession(
        [
            DummyResponse(status_code=401),
            DummyResponse(status_code=200),
        ]
    )
    selenium_called = {"count": 0}

    def fake_selenium(url, force=False):
        selenium_called["count"] += 1
        return True

    monkeypatch.setattr(client, "_selenium_warmup", fake_selenium)
    monkeypatch.setattr(time, "sleep", lambda *_args, **_kwargs: None)

    response = client.get("https://example.com/data", headers={})

    assert response.status_code == 200
    assert selenium_called["count"] == 1


def test_network_cooldown_short_circuits_retries(monkeypatch):
    state = {}
    strategy = RequestStrategy(max_attempts=6, backoff_factor=0)
    client = HostClient("example.com", strategy, state)

    class CountingSession(DummySession):
        def __init__(self):
            super().__init__([requests.exceptions.ConnectTimeout("connect timeout")])
            self.calls = 0

        def get(self, *args, **kwargs):
            self.calls += 1
            return super().get(*args, **kwargs)

    session = CountingSession()
    client._session = session
    monkeypatch.setattr(time, "sleep", lambda *_args, **_kwargs: None)

    with pytest.raises(SourceTemporarilyUnavailable):
        client.get("https://example.com/data", headers={})

    assert session.calls == 1

    with pytest.raises(SourceTemporarilyUnavailable) as exc_info:
        client.get("https://example.com/data", headers={})

    assert "network cooldown active" in str(exc_info.value)


def test_connect_timeout_with_proxy_pool_does_not_trigger_immediate_cooldown(monkeypatch):
    state = {}
    strategy = RequestStrategy(
        max_attempts=2,
        backoff_factor=0,
        proxies=[{"http": "http://proxy-1", "https": "http://proxy-1"}],
    )
    client = HostClient("example.com", strategy, state)
    client._session = DummySession(
        [
            requests.exceptions.ConnectTimeout("connect timeout"),
            DummyResponse(status_code=200),
        ]
    )
    monkeypatch.setattr(time, "sleep", lambda *_args, **_kwargs: None)

    response = client.get("https://example.com/data", headers={})

    assert response.status_code == 200
    failures = state["host_state"]["example.com"].get("failures", {})
    assert not failures.get("network_cooldown_until")


def test_connect_timeout_without_proxy_pool_triggers_cooldown(monkeypatch):
    state = {}
    strategy = RequestStrategy(max_attempts=3, backoff_factor=0)
    client = HostClient("example.com", strategy, state)
    client._session = DummySession([requests.exceptions.ConnectTimeout("connect timeout")])
    monkeypatch.setattr(time, "sleep", lambda *_args, **_kwargs: None)

    with pytest.raises(SourceTemporarilyUnavailable):
        client.get("https://example.com/data", headers={})

    failures = state["host_state"]["example.com"].get("failures", {})
    assert failures.get("network_cooldown_until")


def test_repeated_401_for_same_url_aborts_strategy_retries(monkeypatch):
    state = {}
    strategy = RequestStrategy(max_attempts=4, backoff_factor=0, retry_statuses=[401], selenium_fallback=True)
    client = HostClient("example.com", strategy, state)
    client._session = DummySession(
        [
            DummyResponse(status_code=401),
            DummyResponse(status_code=401),
        ]
    )
    selenium_called = {"count": 0}

    def fake_selenium(url, force=False):
        selenium_called["count"] += 1
        return True

    monkeypatch.setattr(client, "_selenium_warmup", fake_selenium)
    monkeypatch.setattr(time, "sleep", lambda *_args, **_kwargs: None)

    with pytest.raises(SourceTemporarilyUnavailable):
        client.get("https://example.com/data", headers={})

    assert selenium_called["count"] == 1
    metrics = state["stats"]["metrics"]["example.com"]
    assert metrics["attempts"] == 2
