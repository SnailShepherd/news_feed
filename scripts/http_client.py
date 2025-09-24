"""HTTP client helpers with per-host strategies and stateful sessions."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import random
import shutil
import socket
import time
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from typing import Any, Dict, Iterable, List, MutableMapping, Optional, Tuple

import requests
from requests import Response, Session
from requests.adapters import HTTPAdapter


try:
    import cloudscraper  # type: ignore
except Exception:  # pragma: no cover - optional dependency during import stage
    cloudscraper = None


LOGGER = logging.getLogger(__name__)

_DDOS_GUARD_COOKIE_PREFIXES = (
    "__ddg",
    "ddg1",
    "ddg2",
    "ddg3",
    "ddg4",
    "ddg5",
    "cf_clearance",
    "cf_bm",
    "cf_chl_",
)


class SourceTemporarilyUnavailable(RuntimeError):
    """Raised when a host cannot be fetched even after fallbacks."""


class SeleniumUnavailable(RuntimeError):
    """Raised when Selenium fallback cannot be executed."""


@dataclass
class WarmupConfig:
    """Configuration for a warm-up request before regular traffic."""

    url: Optional[str] = None
    delay_range: Tuple[float, float] = (5.0, 10.0)
    timeout: Optional[float] = None


@dataclass
class RequestStrategy:
    """Per-host request strategy parsed from the configuration."""

    connect_timeout: Optional[float] = None
    read_timeout: Optional[float] = None
    max_attempts: int = 3
    backoff_factor: float = 1.5
    proxies: List[Dict[str, str]] = field(default_factory=list)
    retry_statuses: List[int] = field(default_factory=list)
    extra_headers: Dict[str, str] = field(default_factory=dict)
    warmup: Optional[WarmupConfig] = None
    use_cloudscraper: bool = False
    selenium_fallback: bool = False
    selenium_wait: float = 5.0
    selenium_extra_options: List[str] = field(default_factory=list)
    capture_cookies: bool = True
    name: Optional[str] = None
    warmup_first: bool = False
    record_path_on_success: bool = False

    @property
    def timeout(self) -> Optional[Tuple[float, float]]:
        if self.connect_timeout is None and self.read_timeout is None:
            return None
        return (
            self.connect_timeout or self.read_timeout or 30.0,
            self.read_timeout or self.connect_timeout or 30.0,
        )


def parse_proxy_list(raw: Iterable[Any]) -> List[Dict[str, str]]:
    proxies: List[Dict[str, str]] = []
    for item in raw:
        if not item:
            continue
        if isinstance(item, str):
            proxies.append({"http": item, "https": item})
        elif isinstance(item, dict):
            mapping: Dict[str, str] = {}
            for key, value in item.items():
                if value:
                    mapping[str(key)] = str(value)
            if mapping:
                proxies.append(mapping)
    return proxies


def parse_warmup(raw: MutableMapping[str, Any]) -> WarmupConfig:
    cfg = WarmupConfig()
    if not raw:
        return cfg
    cfg.url = raw.get("url")
    delay = raw.get("delay") or raw.get("delay_range")
    if isinstance(delay, (list, tuple)) and len(delay) >= 2:
        cfg.delay_range = (float(delay[0]), float(delay[1]))
    elif isinstance(delay, (int, float)):
        cfg.delay_range = (float(delay), float(delay))
    if raw.get("timeout") is not None:
        cfg.timeout = float(raw["timeout"])
    return cfg


def build_strategy_from_config(name: str, config: Dict[str, Any]) -> RequestStrategy:
    strategy = RequestStrategy(name=name)
    if not config:
        return strategy

    strategy.connect_timeout = (
        float(config.get("connect_timeout")) if config.get("connect_timeout") is not None else None
    )
    strategy.read_timeout = (
        float(config.get("read_timeout")) if config.get("read_timeout") is not None else None
    )
    if config.get("max_attempts"):
        strategy.max_attempts = int(config["max_attempts"])
    if config.get("backoff_factor") is not None:
        strategy.backoff_factor = float(config["backoff_factor"])
    proxies = config.get("proxies") or []
    if proxies:
        strategy.proxies = parse_proxy_list(proxies)
    retry_statuses = config.get("retry_statuses") or []
    if isinstance(retry_statuses, int):
        retry_statuses = [retry_statuses]
    strategy.retry_statuses = [int(s) for s in retry_statuses]
    headers = config.get("extra_headers") or {}
    if isinstance(headers, dict):
        strategy.extra_headers = {str(k): str(v) for k, v in headers.items() if v}
    warmup_cfg = config.get("warmup")
    if isinstance(warmup_cfg, dict):
        strategy.warmup = parse_warmup(warmup_cfg)
    strategy.use_cloudscraper = bool(config.get("use_cloudscraper"))
    strategy.selenium_fallback = bool(config.get("selenium_fallback"))
    if config.get("selenium_wait") is not None:
        strategy.selenium_wait = float(config["selenium_wait"])
    extra_options = config.get("selenium_extra_options") or []
    if extra_options:
        strategy.selenium_extra_options = [str(x) for x in extra_options]
    if config.get("capture_cookies") is not None:
        strategy.capture_cookies = bool(config["capture_cookies"])
    strategy.warmup_first = bool(config.get("warmup_first"))
    strategy.record_path_on_success = bool(config.get("record_path_on_success"))
    return strategy


def build_strategy_registry(sources: Iterable[Dict[str, Any]]) -> Dict[str, RequestStrategy]:
    mapping: Dict[str, RequestStrategy] = {}
    for src in sources:
        cfg = src.get("request_strategy")
        if not cfg:
            continue
        base_url = src.get("base_url") or src.get("start_url")
        if not base_url:
            continue
        host = requests.utils.urlparse(base_url).netloc
        mapping[host] = build_strategy_from_config(src.get("name") or host, cfg)
    return mapping


class HostClient:
    """Stateful HTTP client that honours the configured strategy."""

    def __init__(self, host: str, strategy: RequestStrategy, state: Dict[str, Any]):
        self.host = host
        self.strategy = strategy
        self.state_root = state.setdefault("host_state", {}).setdefault(
            host,
            {"cookies": [], "failures": {}, "warmup_done": False, "last_success_path": None},
        )
        self.stats_root = state.setdefault("stats", {}).setdefault("metrics", {}).setdefault(host, {})
        self._session: Session = self._create_session()
        self._load_cached_cookies()

    # Public API ---------------------------------------------------------
    def get(self, url: str, headers: Dict[str, str], allow_redirects: bool = True, timeout: Optional[Tuple[float, float]] = None,
            proxies: Optional[Dict[str, str]] = None) -> Response:
        self._ensure_warmup(url)
        timeout_value = timeout or self.strategy.timeout
        metrics = {
            "dns_ms": None,
            "connect_ms": None,
            "response_ms": None,
            "attempts": 0,
            "status": None,
            "error": None,
        }

        attempt = 0
        errors: List[str] = []
        proxies_cycle = self._iter_proxies()
        response: Optional[Response] = None
        while attempt < max(1, self.strategy.max_attempts):
            attempt += 1
            metrics["attempts"] = attempt
            proxy_cfg = proxies or next(proxies_cycle)
            request_headers = dict(headers)
            request_headers.update(self.strategy.extra_headers)
            dns_started = time.monotonic()
            dns_ms = self._perform_dns_lookup(url)
            request_started = time.monotonic()
            try:
                response = self._session.get(
                    url,
                    headers=request_headers,
                    allow_redirects=allow_redirects,
                    timeout=timeout_value,
                    proxies=proxy_cfg,
                )
                metrics["response_ms"] = (time.monotonic() - request_started) * 1000
                metrics["dns_ms"] = dns_ms
                if dns_ms is not None and metrics["response_ms"] is not None:
                    metrics["connect_ms"] = max(0.0, metrics["response_ms"] - dns_ms)
                metrics["status"] = response.status_code
                if response.status_code in self.strategy.retry_statuses and attempt < self.strategy.max_attempts:
                    self._handle_retry_status(url, response.status_code)
                    self._backoff(attempt)
                    continue
                response.raise_for_status()
                self._record_success(url, response)
                self._persist_metrics(metrics)
                return response
            except SourceTemporarilyUnavailable:
                metrics["error"] = "temporarily_unavailable"
                break
            except requests.exceptions.RequestException as exc:  # covers HTTPError after raise_for_status
                err_text = str(exc)
                errors.append(err_text)
                metrics["error"] = err_text
                should_switch = self._is_network_unreachable(exc)
                if should_switch:
                    proxy_cfg = None  # ensure next iteration selects new proxy
                if attempt >= self.strategy.max_attempts:
                    break
                self._record_failure(err_text)
                self._backoff(attempt)
                continue
            except OSError as exc:
                err_text = f"{exc.__class__.__name__}: {exc}"
                errors.append(err_text)
                metrics["error"] = err_text
                if attempt >= self.strategy.max_attempts:
                    break
                self._record_failure(err_text)
                self._backoff(attempt)
                continue

        self._persist_metrics(metrics)
        error_summary = ", ".join(errors) or metrics.get("error") or "unknown error"
        raise SourceTemporarilyUnavailable(f"{self.host}: {error_summary}")

    # Internal helpers ---------------------------------------------------
    def _create_session(self) -> Session:
        if self.strategy.use_cloudscraper and cloudscraper is not None:
            session = cloudscraper.create_scraper()
        else:
            session = requests.Session()
        adapter = HTTPAdapter(max_retries=0)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    def _load_cached_cookies(self) -> None:
        cookies = self.state_root.get("cookies") or []
        if not cookies:
            return
        for cookie in cookies:
            try:
                self._session.cookies.set(**cookie)
            except TypeError:
                continue

    def _store_cookies(self, response: Optional[Response] = None) -> None:
        if not self.strategy.capture_cookies:
            return
        jar = response.cookies if response is not None and response.cookies else self._session.cookies
        cookies: List[Dict[str, Any]] = []
        for cookie in jar:
            cookies.append(
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain,
                    "path": cookie.path,
                    "secure": cookie.secure,
                    "expires": cookie.expires,
                }
            )
        if not cookies:
            return
        self.state_root["cookies"] = cookies

    def _collect_cookie_names(self, response: Optional[Response] = None) -> List[str]:
        names = set()
        names.update(cookie.name.lower() for cookie in self._session.cookies)
        if response is not None:
            names.update(cookie.name.lower() for cookie in response.cookies)
            header_values: List[str] = []
            header = response.headers.get("Set-Cookie") if hasattr(response, "headers") else None
            if header:
                header_values.append(str(header))
            raw_headers = getattr(getattr(response, "raw", None), "headers", None)
            if raw_headers is not None:
                for getter_name in ("get_all", "getall", "getlist"):
                    getter = getattr(raw_headers, getter_name, None)
                    if getter:
                        try:
                            values = getter("Set-Cookie")  # type: ignore[arg-type]
                        except Exception:  # pragma: no cover - defensive for incompatible interfaces
                            values = None
                        if not values:
                            continue
                        if isinstance(values, (list, tuple)):
                            header_values.extend(str(value) for value in values if value)
                        else:
                            header_values.append(str(values))
            for value in header_values:
                try:
                    parsed = SimpleCookie()
                    parsed.load(value)
                except Exception:  # pragma: no cover - defensive for malformed cookies
                    continue
                for morsel in parsed.values():
                    name = morsel.key.lower()
                    if name:
                        names.add(name)
        return sorted(names)

    def _has_protection_cookies(self, response: Optional[Response] = None) -> bool:
        for name in self._collect_cookie_names(response):
            for prefix in _DDOS_GUARD_COOKIE_PREFIXES:
                if name.startswith(prefix):
                    return True
            if "ddos" in name and "guard" in name:
                return True
        return False

    def _ensure_warmup(self, url: str) -> None:
        if not self.strategy.warmup or self.state_root.get("warmup_done"):
            return
        warmup_metrics = self.stats_root.setdefault("warmup", {})
        if self.strategy.capture_cookies and self.state_root.get("cookies"):
            if self._has_protection_cookies():
                LOGGER.info("Warm-up for %s skipped: cached protection cookies present", self.host)
                warmup_metrics.update({
                    "mode": "cached",
                    "result": "cached_cookies",
                    "cookies": self._collect_cookie_names(),
                })
                warmup_metrics.pop("error", None)
                self.state_root["warmup_done"] = True
                return
        warmup_url = self.strategy.warmup.url or url
        timeout = self.strategy.warmup.timeout or self.strategy.timeout
        try:
            LOGGER.info("Warm-up %s using %s", self.host, warmup_url)
            response = self._session.get(warmup_url, timeout=timeout, allow_redirects=True)
            LOGGER.debug("Warm-up response headers for %s: %s", self.host, response.headers)
            LOGGER.debug("Warm-up cookies for %s: %s", self.host, response.cookies.get_dict())
            protection_cookies = self._has_protection_cookies(response)
            if response.status_code >= 400 and not protection_cookies:
                warmup_metrics.update(
                    {
                        "mode": "http",
                        "status_code": response.status_code,
                        "result": "http_error",
                        "cookies": self._collect_cookie_names(response),
                    }
                )
                # Even if we think there are no protection cookies, persist what we saw to aid diagnostics.
                self._store_cookies(response)
                raise requests.HTTPError(f"warm-up failed with {response.status_code}")
            if protection_cookies and response.status_code >= 400:
                LOGGER.info(
                    "Warm-up for %s solved protection with status %s", self.host, response.status_code
                )
                warmup_metrics.update(
                    {
                        "mode": "http",
                        "status_code": response.status_code,
                        "result": "http_4xx_with_cookies",
                        "cookies": self._collect_cookie_names(response),
                    }
                )
                warmup_metrics.pop("error", None)
            else:
                warmup_metrics.update(
                    {
                        "mode": "http",
                        "status_code": response.status_code,
                        "result": "http_success",
                        "cookies": self._collect_cookie_names(response),
                    }
                )
                warmup_metrics.pop("error", None)
            self._store_cookies(response)
            delay_min, delay_max = self.strategy.warmup.delay_range
            time.sleep(random.uniform(delay_min, delay_max))
            self.state_root["warmup_done"] = True
        except Exception as exc:
            LOGGER.warning("Warm-up for %s failed: %s", self.host, exc)
            warmup_metrics.setdefault("mode", "http")
            warmup_metrics.setdefault("cookies", self._collect_cookie_names())
            warmup_metrics["error"] = str(exc)
            warmup_metrics["result"] = "http_error"
            if self.strategy.selenium_fallback:
                if self._selenium_warmup(warmup_url):
                    warmup_metrics.update(
                        {
                            "mode": "selenium",
                            "result": "selenium_success",
                            "cookies": self._collect_cookie_names(),
                        }
                    )
                    warmup_metrics.pop("error", None)
                    self.state_root["warmup_done"] = True
                    return
                warmup_metrics.update({"mode": "selenium", "result": "selenium_failed"})
            raise SourceTemporarilyUnavailable(f"warm-up failed for {self.host}: {exc}")

    def _selenium_warmup(self, url: str) -> bool:
        if not self.strategy.selenium_fallback:
            return False
        if self.strategy.capture_cookies and self._has_protection_cookies():
            LOGGER.info("Skipping Selenium warm-up for %s: protection cookies already loaded", self.host)
            return True
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options as ChromeOptions
        except Exception as exc:  # pragma: no cover - optional dependency missing
            LOGGER.warning("Selenium not available for %s: %s", self.host, exc)
            return False

        options = ChromeOptions()
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        binary_location = os.environ.get("CHROME_BINARY")
        if not binary_location:
            for candidate in ("chromium-browser", "chromium", "google-chrome", "google-chrome-stable"):
                binary_location = shutil.which(candidate)
                if binary_location:
                    break
        if binary_location:
            options.binary_location = binary_location
            LOGGER.debug("Using Chrome binary for %s: %s", self.host, binary_location)
        for extra in self.strategy.selenium_extra_options:
            options.add_argument(extra)

        driver = None
        try:
            driver = webdriver.Chrome(options=options)
            driver.get(url)
            time.sleep(self.strategy.selenium_wait)
            cookies = driver.get_cookies()
            if not cookies:
                return False
            self._session.cookies.clear()
            for cookie in cookies:
                params = {k: cookie.get(k) for k in ["name", "value", "domain", "path", "secure", "expiry"]}
                if params.get("expiry") is not None:
                    params["expires"] = params.pop("expiry")
                params = {k: v for k, v in params.items() if v is not None}
                self._session.cookies.set(**params)
            self._store_cookies()
            LOGGER.info("Selenium warm-up success for %s", self.host)
            return True
        except Exception as exc:
            LOGGER.warning("Selenium warm-up failed for %s: %s", self.host, exc)
            return False
        finally:
            with contextlib.suppress(Exception):
                if driver is not None:
                    driver.quit()

    def _iter_proxies(self) -> Iterable[Optional[Dict[str, str]]]:
        if not self.strategy.proxies:
            while True:
                yield None
        while True:
            for proxy in self.strategy.proxies:
                yield proxy

    def _backoff(self, attempt: int) -> None:
        if self.strategy.backoff_factor <= 0:
            return
        delay = self.strategy.backoff_factor * (2 ** max(0, attempt - 1))
        time.sleep(delay)

    def _record_success(self, url: str, response: Response) -> None:
        self.state_root.setdefault("failures", {})["last_error"] = None
        self.state_root.setdefault("failures", {})["consecutive"] = 0
        if self.strategy.record_path_on_success:
            path = requests.utils.urlparse(url).path
            self.state_root["last_success_path"] = path
        self._store_cookies(response)

    def _record_failure(self, reason: str) -> None:
        failures = self.state_root.setdefault("failures", {})
        failures["consecutive"] = failures.get("consecutive", 0) + 1
        failures["last_error"] = reason
        failures["last_ts"] = time.time()

    def _persist_metrics(self, metrics: Dict[str, Any]) -> None:
        metrics_copy = json.loads(json.dumps(metrics))  # ensure JSON serialisable (floats -> floats)
        self.stats_root.update(metrics_copy)

    @staticmethod
    def _is_network_unreachable(exc: Exception) -> bool:
        message = str(exc)
        return "Network is unreachable" in message or "ENETUNREACH" in message

    def _perform_dns_lookup(self, url: str) -> Optional[float]:
        host = requests.utils.urlparse(url).hostname
        if not host:
            return None
        start = time.monotonic()
        try:
            socket.getaddrinfo(host, None)
        except socket.gaierror as exc:
            LOGGER.debug("DNS lookup failed for %s: %s", host, exc)
            return None
        return (time.monotonic() - start) * 1000

    def _handle_retry_status(self, url: str, status_code: int) -> None:
        LOGGER.warning("Status %s for %s -> retry via strategy", status_code, url)
        if status_code == 403 and self.strategy.selenium_fallback:
            if self._selenium_warmup(url):
                return
        self._reset_session()

    def _reset_session(self) -> None:
        self._session.close()
        self._session = self._create_session()
        self._load_cached_cookies()


__all__ = [
    "HostClient",
    "RequestStrategy",
    "SourceTemporarilyUnavailable",
    "SeleniumUnavailable",
    "WarmupConfig",
    "build_strategy_registry",
]

