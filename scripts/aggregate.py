#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, json, logging, pathlib, sys, hashlib, argparse, random, html, shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode

import requests
import time
from collections import defaultdict
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup, FeatureNotFound
from dateutil import parser as dparser
import pytz
import xml.etree.ElementTree as ET

try:
    from scripts.url_filters import is_listing_url
except ModuleNotFoundError:  # pragma: no cover - fallback when run as a script
    from url_filters import is_listing_url  # type: ignore

try:
    from scripts.http_client import (
        HostClient,
        CrawlerParserError,
        RequestStrategy,
        SourceTemporarilyUnavailable,
        build_strategy_registry,
        DEFAULT_USER_AGENT,
    )
except ModuleNotFoundError:  # pragma: no cover - fallback when run as a script
    from http_client import (  # type: ignore
        HostClient,
        CrawlerParserError,
        RequestStrategy,
        SourceTemporarilyUnavailable,
        build_strategy_registry,
        DEFAULT_USER_AGENT,
    )

# ---- Settings ----
ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCS_DIR = ROOT / "docs"
CACHE_DIR = ROOT / ".cache"
PAGES_DIR = CACHE_DIR / "pages"
STATE_FILE = CACHE_DIR / "state.json"
SESSION_STATE_FILE = CACHE_DIR / "session-state.json"
SOURCE_HEALTH_STATE_FILE = CACHE_DIR / "source-health-state.json"
OUT_JSON = DOCS_DIR / "unified.json"
EXISTING_FEED_JSON = OUT_JSON
SOURCE_HEALTH_JSON = DOCS_DIR / "source-health.json"

CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 10.0
REQUEST_TIMEOUT = (CONNECT_TIMEOUT, READ_TIMEOUT)
USER_AGENT = DEFAULT_USER_AGENT
MAX_LINKS_PER_SOURCE = 100
FEED_MAX_ITEMS = int(os.environ.get("FEED_MAX_ITEMS", "800"))
FEED_MIN_ITEMS_PER_SOURCE = int(os.environ.get("FEED_MIN_ITEMS_PER_SOURCE", "5"))
CACHE_MAX_BYTES = int(os.environ.get("NEWSFEED_CACHE_MAX_BYTES", str(512 * 1024 * 1024)))
CACHE_MAX_AGE_DAYS = int(os.environ.get("NEWSFEED_CACHE_MAX_AGE_DAYS", "14"))
ARGS = None  # будет заполнено в main()
SMOKE_DEFAULT_SOURCES = {
    "НОТИМ",
    "АРД: статьи",
    "ЕЭК ЕАЭС",
    "Минфин России",
    "Российская газета: Экономика",
}
START_TIME = time.monotonic()
RUNTIME_EXCEEDED = False
_RUNTIME_LOGGED = False
SOURCE_SUMMARY: dict[str, dict[str, object]] = defaultdict(
    lambda: {
        "total": 0,
        "empty": 0,
        "short": 0,
        "listing": 0,
        "api": 0,
        "amp": 0,
        "min_words": DEFAULT_MIN_WORDS,
        "index_fetch_status": "not_attempted",
        "raw_link_candidates": 0,
        "accepted_links": 0,
        "index_attempts": [],
        "attempted_articles": 0,
        "cached_fallback_used": False,
        "future_date_rejections": 0,
        "publication_rejections_by_signal": {},
        "publication_rejection_samples": [],
        "maximum_future_offset_seconds": 0,
        "canonical_rejections_by_reason": {},
        "canonical_rejection_samples": [],
        "structured_endpoint_fetch_failures": 0,
        "structured_schema_mismatches": 0,
        "structured_zero_records": 0,
        "structured_article_extraction_failures": 0,
        "article_fetch_degradations": 0,
        "feed_content_fallbacks": 0,
        "last_error": None,
    }
)
SOURCE_MIN_WORDS: dict[str, int] = {}
SOURCE_RETENTION_WEIGHTS: dict[str, float] = {}
SOURCE_CONFIGS_BY_NAME: dict[str, dict] = {}
DEFAULT_MIN_WORDS = 100
HOST_MIN_WORD_OVERRIDES = {
    "realty.ria.ru": 120,
    "realty.interfax.ru": 120,
    "stroygaz.ru": 120,
    "rg.ru": 150,
    "faufcc.ru": 70,
}


def _effective_source_min_words(src: dict, url: str | None = None) -> int:
    """Resolve the same extraction threshold used by harvest validation."""
    source_defined = int(src.get("min_words", 0) or 0)
    if source_defined > 0:
        return source_defined
    host = urlparse(url).netloc if url else _get_host_for_source(src)
    normalized_host = host.lower().lstrip("www.") if host else ""
    return HOST_MIN_WORD_OVERRIDES.get(normalized_host, DEFAULT_MIN_WORDS)
PUBLICATION_CLOCK_SKEW = timedelta(
    minutes=float(os.environ.get("PUBLICATION_CLOCK_SKEW_MINUTES", "15"))
)
PUBLICATION_REJECTION_SAMPLE_LIMIT = int(
    os.environ.get("PUBLICATION_REJECTION_SAMPLE_LIMIT", "5")
)

ESSENTIAL_ITEM_FIELDS = (
    "id",
    "source",
    "title",
    "url",
    "content_text",
    "first_seen",
    "bucketed_at",
    "fetched_at",
)
OPTIONAL_ITEM_FIELDS = (
    "published_at", "canonical_url", "source_record_id", "tags", "summary",
)

# Перехваты ошибок/429 и паузы между запросами к одному хосту
SESSION = requests.Session()
_retry = Retry(
    # Keep the generic path resilient without multiplying a 10 second timeout
    # into ~47 seconds for every cached article on an unavailable host.
    total=1, connect=1, read=1, backoff_factor=0.5,
    status_forcelist=[429,500,502,503,504],
    allowed_methods=["GET","HEAD"]
)
_adapter = HTTPAdapter(max_retries=_retry)
SESSION.mount("http://", _adapter)
SESSION.mount("https://", _adapter)
HOST_DELAY_DEFAULT = 1.5
HOST_DELAY_OVERRIDES = {"www.metalinfo.ru": 6.0, "metalinfo.ru": 6.0, "www.pnp.ru": 6.0, "pnp.ru": 6.0}
_last_req_at = defaultdict(lambda: 0.0)

MSK = pytz.timezone("Europe/Moscow")

LOG_PATH = pathlib.Path("/tmp/rebuild.log")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
try:
    file_handler = logging.FileHandler(LOG_PATH, mode="a", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(file_handler)
except OSError:
    logging.warning("Unable to attach log file handler at %s", LOG_PATH)

# ---- State ----
CACHE_DIR.mkdir(exist_ok=True)
PAGES_DIR.mkdir(exist_ok=True)
DOCS_DIR.mkdir(exist_ok=True)

def ensure_state_keys(state: dict) -> dict:
    required_defaults = {
        "headers": {},
        "index_hash": {},
        "seen_urls": {},
        "candidate_urls": {},
        "url_states": {},
        "first_seen": {},
        "aliases": {},
        "content_hashes": {},
        "canonical_item_ids": {},
    }
    for key, default in required_defaults.items():
        val = state.get(key)
        if not isinstance(val, dict):
            state[key] = dict(default)
    return state


def ensure_session_state_keys(state: dict) -> dict:
    """Normalize state that is meaningful only to a particular runner."""
    for key in ("host_state", "stats"):
        if not isinstance(state.get(key), dict):
            state[key] = {}
    return state


ARTICLE_MAX_ATTEMPTS = max(1, int(os.environ.get("ARTICLE_MAX_ATTEMPTS", "2")))
ARTICLE_RETRY_DELAY = max(0.0, float(os.environ.get("ARTICLE_RETRY_DELAY", "1")))
MAX_EXTRACTION_FAILURES = max(1, int(os.environ.get("MAX_EXTRACTION_FAILURES", "3")))
EXTRACTION_RULES_VERSION = "2026-08-07-v1"
_RETAINED_URL_CACHE: tuple[tuple[str, int, int] | None, dict[str, set[str]]] | None = None


def _retained_urls_by_source() -> dict[str, set[str]]:
    """Load URLs that are known to have passed extraction from the retained feed."""
    global _RETAINED_URL_CACHE
    try:
        stat = OUT_JSON.stat()
        signature = (str(OUT_JSON.resolve()), stat.st_mtime_ns, stat.st_size)
    except OSError:
        signature = None
    if _RETAINED_URL_CACHE is not None and _RETAINED_URL_CACHE[0] == signature:
        return _RETAINED_URL_CACHE[1]

    retained: dict[str, set[str]] = defaultdict(set)
    if signature is not None:
        try:
            payload = json.loads(OUT_JSON.read_text(encoding="utf-8"))
            items = payload.get("items", []) if isinstance(payload, dict) else []
            for item in items:
                if not isinstance(item, dict):
                    continue
                source = item.get("source")
                url = item.get("url")
                if isinstance(source, str) and isinstance(url, str):
                    retained[source].add(url)
        except (OSError, ValueError) as exc:
            logging.warning("Unable to read retained feed URLs for state migration: %s", exc)
    result = dict(retained)
    _RETAINED_URL_CACHE = (signature, result)
    return result


def _source_url_states(source_name: str) -> dict:
    """Return URL states, treating only retained legacy articles as accepted."""
    states = STATE.setdefault("url_states", {}).setdefault(source_name, {})
    retained_urls = _retained_urls_by_source().get(source_name, set())
    for url in STATE.setdefault("seen_urls", {}).get(source_name, []):
        states.setdefault(
            url,
            {"status": "accepted"} if url in retained_urls else {
                "status": "retryable_failure",
                "error": "legacy seen URL not found in retained feed",
            },
        )
    return states


def _record_url_state(source_name: str, url: str, status: str, error: str | None = None) -> None:
    record = {"status": status, "updated_at": datetime.now(timezone.utc).isoformat()}
    if error:
        record["error"] = error
    STATE.setdefault("url_states", {}).setdefault(source_name, {})[url] = record


def _extraction_fingerprint(src: dict, min_words: int) -> str:
    """Identify extraction rules so terminal content failures can be reconsidered."""
    configuration = {
        "version": EXTRACTION_RULES_VERSION,
        "min_words": min_words,
        "content_selectors": src.get("content_selectors"),
        "api_content_field": src.get("api_content_field"),
        "host_content_selectors": HOST_CONTENT_SELECTORS,
    }
    encoded = json.dumps(configuration, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _record_extraction_failure(
    source_name: str,
    url: str,
    *,
    kind: str,
    error: str,
    fingerprint: str,
) -> None:
    states = STATE.setdefault("url_states", {}).setdefault(source_name, {})
    previous = states.get(url, {})
    same_failure = (
        previous.get("fingerprint") == fingerprint
        and previous.get("failure_kind") == kind
    )
    failures = int(previous.get("failure_count", 0)) + 1 if same_failure else 1
    status = "permanently_rejected" if failures >= MAX_EXTRACTION_FAILURES else "retryable_failure"
    states[url] = {
        "status": status,
        "failure_kind": kind,
        "failure_count": failures,
        "fingerprint": fingerprint,
        "error": error,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _should_attempt_url(record: dict | None, fingerprint: str) -> bool:
    if not record:
        return True
    status = record.get("status")
    if status == "accepted":
        return False
    if status == "permanently_rejected" and record.get("fingerprint") == fingerprint:
        return False
    return True


def _with_article_retries(src: dict, url: str, operation):
    """Retry transient article fetch/parser errors without retrying rejected content."""
    attempts = max(1, int(src.get("article_max_attempts", ARTICLE_MAX_ATTEMPTS)))
    delay = max(0.0, float(src.get("article_retry_delay", ARTICLE_RETRY_DELAY)))
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as exc:
            if attempt >= attempts:
                raise
            logging.warning(
                "  transient article failure %d/%d for %s: %s",
                attempt,
                attempts,
                url,
                exc,
            )
            if delay:
                time.sleep(delay)


def _build_item_with_retries(url: str, crawl_src: dict, *args, **kwargs):
    return _with_article_retries(
        crawl_src,
        url,
        lambda: build_item(url, *args, **kwargs),
    )


if STATE_FILE.exists():
    STATE = json.loads(STATE_FILE.read_text(encoding="utf-8"))
else:
    STATE = {
        "headers": {},
        "index_hash": {},
        "seen_urls": {},
    }

STATE = ensure_state_keys(STATE)

# Runner-specific throttling and diagnostics are cached separately from the
# durable crawler state committed by the workflow.
_legacy_host_state = STATE.pop("host_state", {})
_legacy_stats = STATE.get("stats", {})
_legacy_session_stats = {
    key: _legacy_stats.pop(key)
    for key in ("cooldowns", "errors", "metrics")
    if key in _legacy_stats
}
if not _legacy_stats:
    STATE.pop("stats", None)

if SESSION_STATE_FILE.exists():
    SESSION_STATE = json.loads(SESSION_STATE_FILE.read_text(encoding="utf-8"))
else:
    SESSION_STATE = {}
SESSION_STATE = ensure_session_state_keys(SESSION_STATE)
if _legacy_host_state and not SESSION_STATE["host_state"]:
    SESSION_STATE["host_state"] = _legacy_host_state
for key, value in _legacy_session_stats.items():
    SESSION_STATE["stats"].setdefault(key, value)

if SOURCE_HEALTH_STATE_FILE.exists():
    SOURCE_HEALTH_STATE = json.loads(SOURCE_HEALTH_STATE_FILE.read_text(encoding="utf-8"))
else:
    # Migrate streaks out of the crawler cache without resetting escalation.
    SOURCE_HEALTH_STATE = STATE.pop("source_health_streaks", {})
if not isinstance(SOURCE_HEALTH_STATE, dict):
    SOURCE_HEALTH_STATE = {}

HOST_STRATEGIES: dict[str, RequestStrategy] = {}
HOST_CLIENTS: dict[str, HostClient] = {}

DENY_PHRASES = [
    "Актуально",
    "Опрос",
    "Подписка",
    "Архив",
    "Версия для печати",
    "Государственные программы",
    "Creative Commons",
]
_DENY_PATTERNS = [
    re.compile(rf"\b{re.escape(phrase.lower())}\b") for phrase in DENY_PHRASES
]
HOST_CONTENT_SELECTORS: dict[str, list[str]] = {
    "stroygaz.ru": [
        ".news-detail__content",
        ".news-detail__text",
        ".news-detail",
        "article .content",
        "article .article__content",
        "article",
    ],
    "government.ru": [
        ".news__article-body",
        ".article__content",
        ".reader_article_body",
        "[itemprop='articleBody']",
    ],
    "minfin.gov.ru": [
        ".press-reliz-detail__content",
        ".news-detail__content",
        ".article__content",
        ".content",
        "article",
    ],
    "faufcc.ru": [
        "[data-element='content']",
        ".press-center__detail",
        ".news-detail",
        ".article__body",
        "article",
    ],
    "pnp.ru": [
        ".article__content",
        ".article-body",
        ".news__article-body",
        ".article__text",
        "article",
    ],
}

def save_state():
    STATE.pop("source_health_streaks", None)
    STATE_FILE.write_text(json.dumps(STATE, ensure_ascii=False, indent=2), encoding="utf-8")
    SESSION_STATE_FILE.write_text(
        json.dumps(SESSION_STATE, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def save_source_health_state():
    SOURCE_HEALTH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SOURCE_HEALTH_STATE_FILE.write_text(
        json.dumps(SOURCE_HEALTH_STATE, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def prune_page_cache(*, now: float | None = None) -> tuple[int, int]:
    """Bound the persistent HTML cache by age and total bytes, oldest first."""
    now = now if now is not None else time.time()
    cutoff = now - max(0, CACHE_MAX_AGE_DAYS) * 86400
    files = [path for path in PAGES_DIR.iterdir() if path.is_file()]
    removed_files = 0
    removed_bytes = 0
    retained: list[tuple[float, int, pathlib.Path]] = []
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue
        if CACHE_MAX_AGE_DAYS >= 0 and stat.st_mtime < cutoff:
            try:
                path.unlink()
                removed_files += 1
                removed_bytes += stat.st_size
            except OSError:
                pass
        else:
            retained.append((stat.st_mtime, stat.st_size, path))
    total = sum(size for _, size, _ in retained)
    for _, size, path in sorted(retained):
        if CACHE_MAX_BYTES <= 0 or total <= CACHE_MAX_BYTES:
            break
        try:
            path.unlink()
            removed_files += 1
            removed_bytes += size
            total -= size
        except OSError:
            pass
    if removed_files:
        logging.info("Pruned page cache: files=%d bytes=%d retained_bytes=%d", removed_files, removed_bytes, total)
    return removed_files, removed_bytes


def prune_state(feed_items: list[dict], sources: list[dict]) -> None:
    """Discard crawl metadata which can no longer affect the bounded feed.

    The feed contains at most ``FEED_MAX_ITEMS`` records, but the historical
    lookup maps used to grow forever (past 80k entries / 60 MB).  Retaining the
    live feed, configured indexes and the bounded seen-URL windows preserves
    stable IDs and conditional requests while keeping checkout/push inexpensive.
    """
    active_ids = {item.get("id") for item in feed_items if item.get("id")}
    active_urls = {
        value
        for item in feed_items
        for value in (item.get("url"), item.get("canonical_url"))
        if value
    }
    for source in sources:
        active_urls.update(
            value
            for value in (source.get("start_url"), source.get("base_url"))
            if value
        )
        active_urls.update(source.get("index_fallback_urls") or [])
    for urls in STATE.get("seen_urls", {}).values():
        if isinstance(urls, list):
            active_urls.update(urls)

    active_source_names = {source.get("name") for source in sources if source.get("name")}
    candidate_urls = {
        name: list(dict.fromkeys(urls))
        for name, urls in STATE.get("candidate_urls", {}).items()
        if name in active_source_names and isinstance(urls, list)
    }
    STATE["candidate_urls"] = candidate_urls
    lifecycle_urls = {
        name: set(candidate_urls.get(name, [])) | set(STATE.get("seen_urls", {}).get(name, []))
        for name in active_source_names
    }
    STATE["url_states"] = {
        name: {url: record for url, record in records.items() if url in lifecycle_urls.get(name, set())}
        for name, records in STATE.get("url_states", {}).items()
        if name in active_source_names and isinstance(records, dict)
    }

    STATE["headers"] = {
        key: value for key, value in STATE.get("headers", {}).items() if key in active_urls
    }
    STATE["first_seen"] = {
        key: value for key, value in STATE.get("first_seen", {}).items() if key in active_ids
    }
    STATE["aliases"] = {
        key: value
        for key, value in STATE.get("aliases", {}).items()
        if key in active_urls or value in active_urls
    }
    STATE["content_hashes"] = {
        key: value
        for key, value in STATE.get("content_hashes", {}).items()
        if value in active_urls
    }
    STATE["canonical_item_ids"] = {
        key: value
        for key, value in STATE.get("canonical_item_ids", {}).items()
        if key in active_urls or value in active_ids
    }


def runtime_expired() -> bool:
    """Check whether the max runtime threshold has been reached."""

    global RUNTIME_EXCEEDED, _RUNTIME_LOGGED
    if not ARGS or not getattr(ARGS, "max_runtime", None):
        return False
    if RUNTIME_EXCEEDED:
        return True
    elapsed = time.monotonic() - START_TIME
    if elapsed >= ARGS.max_runtime:
        RUNTIME_EXCEEDED = True
        if not _RUNTIME_LOGGED:
            logging.warning(
                "Max runtime of %ss reached — stopping after current source",
                ARGS.max_runtime,
            )
            _RUNTIME_LOGGED = True
        return True
    return False

# ---- HTTP ----
def _get_host_for_source(src: dict | None) -> str | None:
    if not src:
        return None
    base = src.get("base_url") or src.get("start_url")
    if not base:
        return None
    return urlparse(base).netloc


def _normalize_host(host: str | None) -> str:
    if not host:
        return ""
    host = host.lower().strip()
    if host.startswith("www."):
        host = host[4:]
    return host


def get_host_client(url: str, src: dict | None = None) -> HostClient | None:
    request_host = urlparse(url).netloc
    src_host = _get_host_for_source(src)

    candidate_hosts: list[str] = []

    for raw in [request_host, _normalize_host(request_host), src_host, _normalize_host(src_host)]:
        if not raw:
            continue
        if raw not in candidate_hosts:
            candidate_hosts.append(raw)

    # For cross-domain article links found inside a source index (e.g. rg.ru -> ria.ru),
    # do not force the source strategy to unrelated hosts.
    req_norm = _normalize_host(request_host)
    src_norm = _normalize_host(src_host)
    if req_norm and src_norm and req_norm != src_norm:
        candidate_hosts = [h for h in candidate_hosts if _normalize_host(h) == req_norm]

    strategy_host = next((h for h in candidate_hosts if h in HOST_STRATEGIES), None)
    if not strategy_host:
        return None
    strategy = HOST_STRATEGIES.get(strategy_host)
    if not strategy:
        return None
    client = HOST_CLIENTS.get(strategy_host)
    if client is None:
        client = HostClient(strategy_host, strategy, SESSION_STATE)
        HOST_CLIENTS[strategy_host] = client
    return client


def http_get(url: str, allow_conditional: bool = True, src: dict | None = None):
    hdrs = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
    }
    hinfo = STATE["headers"].get(url, {})
    if allow_conditional:
        if "ETag" in hinfo:
            hdrs["If-None-Match"] = hinfo["ETag"]
        if "Last-Modified" in hinfo:
            hdrs["If-Modified-Since"] = hinfo["Last-Modified"]

    # Пауза по хосту
    host = urlparse(url).netloc
    delay = HOST_DELAY_OVERRIDES.get(host, HOST_DELAY_DEFAULT)
    now = time.time()
    sleep_for = _last_req_at[host] + delay - now
    if sleep_for > 0:
        time.sleep(sleep_for)
    if host in HOST_DELAY_OVERRIDES:
        time.sleep(random.uniform(0, 2))
    client = get_host_client(url, src)
    try:
        if client:
            timeout_value = None if client.strategy.timeout else REQUEST_TIMEOUT
            resp = client.get(
                url,
                headers=hdrs,
                allow_redirects=True,
                timeout=timeout_value,
            )
        else:
            resp = SESSION.get(
                url,
                headers=hdrs,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
            )
    except SourceTemporarilyUnavailable:
        raise
    except requests.exceptions.RequestException as exc:
        raise
    _last_req_at[host] = time.time()
    if not client and resp.status_code == 429:
        ra = resp.headers.get("Retry-After")
        try:
            wait = int(ra) if ra else 5
        except ValueError:
            wait = 5
        logging.warning("429 Too Many Requests: %s -> sleep %ss", url, wait)
        time.sleep(wait)
        resp = SESSION.get(
            url,
            headers=hdrs,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        _last_req_at[host] = time.time()
    if resp.status_code == 304:
        logging.info("304 Not Modified: %s", url)
        return None, hinfo
    resp.raise_for_status()
    new_hinfo = {}
    et = resp.headers.get("ETag")
    lm = resp.headers.get("Last-Modified")
    if et:
        new_hinfo["ETag"] = et
    if lm:
        new_hinfo["Last-Modified"] = lm
    STATE["headers"][url] = new_hinfo
    return resp.text, new_hinfo

def cache_key_for(url: str) -> str:
    p = urlparse(url)
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", (p.path or "/")).strip("-")
    query = (p.query or "").strip()
    if query:
        q_hash = hashlib.sha1(query.encode("utf-8")).hexdigest()[:10]
        slug = f"{slug}-{q_hash}" if slug else q_hash
    if not slug:
        slug = "index"
    if len(slug) > 150:
        slug = slug[:150]
    return f"{p.netloc}-{slug}.html"

def cache_key_with_suffix(base_key: str, suffix: str) -> str:
    if base_key.endswith(".html"):
        return f"{base_key[:-5]}{suffix}.html"
    return f"{base_key}{suffix}"


AMP_APPEND_WHITELIST = {"rg.ru", "ria.ru", "realty.ria.ru", "interfax-russia.ru"}
AMP_QUERY_WHITELIST = {"ria.ru", "realty.ria.ru", "realty.interfax.ru", "interfax-russia.ru"}

SHORT_CONTENT_WORDS = 60


def _is_short_content(text: str | None) -> bool:
    return _word_count(text or "") < SHORT_CONTENT_WORDS


def _amp_append_allowed(host: str) -> bool:
    if not host:
        return False
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return host in AMP_APPEND_WHITELIST


def fetch_page(url: str, src: dict | None = None) -> str:
    page_path = PAGES_DIR / cache_key_for(url)
    use_conditional = not (ARGS and getattr(ARGS, 'rebuild', False))
    try:
        content, _ = http_get(url, allow_conditional=use_conditional, src=src)
    except (requests.RequestException, SourceTemporarilyUnavailable) as exc:
        status = None
        if isinstance(exc, requests.HTTPError) and exc.response is not None:
            status = exc.response.status_code
        client = get_host_client(url, src)
        if client and client.strategy.selenium_fallback:
            selenium_html = client.fetch_html_with_selenium(url)
            if selenium_html is not None and not isinstance(selenium_html, str):
                raise CrawlerParserError("fetch_page.selenium_fallback", url, selenium_html)
            if selenium_html:
                content = selenium_html
            else:
                content = None
        else:
            content = None
        if content is None:
            if page_path.exists():
                logging.warning(
                    "Fetch failed for %s%s — using cached copy",
                    url,
                    f" (HTTP {status})" if status else "",
                )
                return page_path.read_text(encoding="utf-8")
            raise
    if content is None and page_path.exists():
        # Not modified -> reuse cached
        return page_path.read_text(encoding="utf-8")
    if content is None:
        # No cached file (first run) but server returned 304 (edge case) -> force GET
        content = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        ).text
    if not isinstance(content, str) or not content.strip():
        raise CrawlerParserError("fetch_page.response", url, content)
    page_path.write_text(content, encoding="utf-8")
    return content


def fetch_amp_if_available(
    url: str, soup: BeautifulSoup, src: dict | None = None
) -> tuple[str | None, str | None]:
    candidates: list[tuple[str, str]] = []

    def add_candidate(raw: str | None, label: str) -> None:
        if not raw:
            return
        absolute = urljoin(url, raw)
        if absolute and absolute != url:
            candidates.append((absolute, label))

    for link in soup.find_all("link"):
        rel = link.get("rel")
        if not rel:
            continue
        if isinstance(rel, (list, tuple)):
            rels = [str(r).lower() for r in rel]
        else:
            rels = [part.lower() for part in str(rel).split() if part]
        if "amphtml" in rels:
            add_candidate(link.get("href"), "amp")
            break
    host = urlparse(url).netloc
    if not candidates and _amp_append_allowed(host):
        base = url.rstrip("/")
        if base and not base.endswith("/amp"):
            candidates.append((f"{base}/amp", "amp"))
    normalized_host = host.lower().lstrip("www.")
    if normalized_host in AMP_QUERY_WHITELIST and "?amp" not in url:
        sep = "&" if "?" in url else "?"
        candidates.append((f"{url}{sep}amp", "amp"))
    if normalized_host == "realty.interfax.ru" and "mobile=1" not in url:
        sep = "&" if "?" in url else "?"
        candidates.append((f"{url}{sep}mobile=1", "mobile"))

    for candidate, label in candidates:
        try:
            html = fetch_page(candidate, src=src)
            return html, label
        except Exception as exc:
            logging.debug("AMP/mobile fetch failed for %s: %s", candidate, exc)
    return None, None


def _parse_index_soup(index_html: str) -> BeautifulSoup:
    """Parse HTML or XML indexes without making the optional lxml parser mandatory."""
    parser = "xml" if index_html.lstrip().startswith("<?xml") else "html.parser"
    try:
        return BeautifulSoup(index_html, parser)
    except FeatureNotFound:
        return BeautifulSoup(index_html, "html.parser")


def _count_index_candidates(index_html: str, parse_embedded_links: bool = False) -> int:
    soup = _parse_index_soup(index_html)
    count = 0
    count += len(soup.find_all("a"))
    for tag_name in ("loc", "link"):
        for tag in soup.find_all(tag_name):
            text = tag.get_text(strip=True)
            if text.startswith("http"):
                count += 1
    if parse_embedded_links:
        for expanded_html in _embedded_text_variants(index_html):
            count += len(re.findall(r'https?://[^\s"\'<>]+', expanded_html))
            count += len(re.findall(r'"(/[^"<>\s]{6,260})"', expanded_html))
    return count


def _embedded_text_variants(index_html: str) -> list[str]:
    """Build variants for pages where links are present only inside escaped blobs."""

    variants = [index_html]
    normalized = index_html.replace("\\/", "/")
    if normalized != index_html:
        variants.append(normalized)

    unescaped_quotes = normalized.replace('\\"', '"').replace("\\'", "'")
    if unescaped_quotes not in variants:
        variants.append(unescaped_quotes)

    html_unescaped = html.unescape(unescaped_quotes)
    if html_unescaped not in variants:
        variants.append(html_unescaped)

    return variants

# ---- Date parsing helpers ----
RU_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
    "янв":1, "фев":2, "мар":3, "апр":4, "май":5, "июн":6, "июл":7, "авг":8, "сен":9, "сент":9, "окт":10, "ноя":11, "дек":12
}

def clamp_year(dt: datetime):
    if dt.year < 2000 or dt.year > 2035:
        return None
    return dt

def make_aware_msk(dt: datetime):
    if dt.tzinfo is None:
        return MSK.localize(dt)
    return dt.astimezone(MSK)

def parse_ru_date_words(s: str):
    # Examples: "19 сентября 2024, 12:34", "19 сент 2024", "19.09.2024 12:34"
    s = re.sub(r"\s+", " ", s.strip())
    # dd.mm.yyyy HH:MM
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})(?:[ T](\d{1,2}):(\d{2}))?", s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh, mm = (int(m.group(4) or 0), int(m.group(5) or 0))
        try:
            return clamp_year(datetime(y, mo, d, hh, mm))
        except ValueError:
            return None
    # "19 сентября 2024", optionally time
    m = re.search(r"(\d{1,2})\s+([А-Яа-яёЁ]+)\s+(\d{4})(?:[ ,](\d{1,2}):(\d{2}))?", s)
    if m:
        d = int(m.group(1))
        month_name = m.group(2).lower()
        y = int(m.group(3))
        mo = RU_MONTHS.get(month_name)
        if mo:
            hh, mm = (int(m.group(4) or 0), int(m.group(5) or 0))
            try:
                return clamp_year(datetime(y, mo, d, hh, mm))
            except ValueError:
                return None
    # dd.mm.yy
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{2})(?:[ T](\d{1,2}):(\d{2}))?", s)
    if m:
        d, mo, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y = 2000 + yy
        hh, mm = (int(m.group(4) or 0), int(m.group(5) or 0))
        try:
            return clamp_year(datetime(y, mo, d, hh, mm))
        except ValueError:
            return None
    return None

def finalize_datetime(dt: datetime):
    if dt is None:
        return None
    dt = make_aware_msk(dt)
    dt = dt.replace(microsecond=0)
    return clamp_year(dt)


@dataclass
class PublicationDiagnostics:
    """Explicit sink for diagnostics produced while parsing this crawl only."""

    source: str

    def reject(self, *, signal: str, raw_value: object, url: str, offset: timedelta) -> None:
        summary = SOURCE_SUMMARY[self.source]
        counts = summary.setdefault("publication_rejections_by_signal", {})
        counts[signal] = int(counts.get(signal, 0) or 0) + 1
        summary["future_date_rejections"] = sum(int(value) for value in counts.values())
        offset_seconds = max(0, int(offset.total_seconds()))
        summary["maximum_future_offset_seconds"] = max(
            int(summary.get("maximum_future_offset_seconds", 0) or 0), offset_seconds
        )
        samples = summary.setdefault("publication_rejection_samples", [])
        if len(samples) < max(0, PUBLICATION_REJECTION_SAMPLE_LIMIT):
            samples.append({
                "signal": signal,
                "raw_value": str(raw_value),
                "url": url,
                "future_offset_seconds": offset_seconds,
                "classification": (
                    "clock_skew" if offset <= PUBLICATION_CLOCK_SKEW
                    else "clearly_invalid_future_date"
                ),
            })


def validate_publication_datetime(
    dt: datetime | None,
    *,
    raw_value: object = None,
    url: str | None = None,
    source: str | None = None,
    signal: str = "unknown",
    now: datetime | None = None,
    allowance: timedelta = PUBLICATION_CLOCK_SKEW,
    diagnostics: PublicationDiagnostics | None = None,
) -> datetime | None:
    """Return a normalized, plausible publication time, or reject it.

    ``now`` and ``allowance`` are injectable so callers and tests do not need
    to depend on wall-clock time. All extraction paths should pass through
    this single boundary before a value is stored as ``published_at``. The
    deliberately small default is also used for timezone-aware values:
    timezone offsets are normalized, not excused with a day-scale allowance.
    """
    dt = finalize_datetime(dt)
    if dt is None:
        return None
    reference = finalize_datetime(now or datetime.now(MSK))
    if dt > reference + allowance:
        rejection_count = 1
        if diagnostics is not None:
            diagnostics.reject(
                signal=signal,
                raw_value=raw_value if raw_value is not None else dt.isoformat(),
                url=url or "",
                offset=dt - reference,
            )
            rejection_count = int(SOURCE_SUMMARY[diagnostics.source]["future_date_rejections"])
        if diagnostics is not None and rejection_count <= 3:
            logging.warning(
                "Reject future publication time value=%r url=%s source=%s signal=%s",
                raw_value if raw_value is not None else dt.isoformat(),
                url or "",
                source or "",
                signal,
            )
        elif diagnostics is not None and rejection_count == 4:
            logging.warning("Suppress further future-date warnings for source=%s", source or "")
        return None
    return dt

DEFAULT_CONTENT_SELECTORS = [
    "article",
    "main article",
    "article .article__content",
    ".article__body",
    ".article__content",
    ".article-body",
    ".article-body__content",
    ".article_text",
    ".article-text",
    ".article-content",
    ".article__text",
    ".content",
    ".content__inner",
    ".content__text",
    ".content-text",
    ".content-text__body",
    ".contentBody",
    ".entry-content",
    ".news-body",
    ".news-content",
    ".news-detail",
    ".news-detail__content",
    ".news-detail__text",
    ".news-detail__wrapper",
    ".news-item__text",
    ".news-text",
    ".post-content",
    ".presscenter__content",
    "#news-detail",
]

def _normalize_whitespace(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    paragraphs = []
    for block in re.split(r"\n{2,}", text):
        block = block.strip()
        if not block:
            continue
        block = re.sub(r"\s*\n\s*", " ", block)
        paragraphs.append(block)
    return "\n\n".join(paragraphs).strip()


_CONTENT_LINE_PATTERNS = [
    re.compile(r"^поделиться", re.IGNORECASE),
    re.compile(r"поделиться$", re.IGNORECASE),
    re.compile(r"^подпис", re.IGNORECASE),
    re.compile(r"^чита(?:йте|ть) нас", re.IGNORECASE),
    re.compile(r"^подписывайтесь", re.IGNORECASE),
    re.compile(r"^рассылк", re.IGNORECASE),
    re.compile(r"^автор(?:[:\s]|$)", re.IGNORECASE),
    re.compile(r"^комментар", re.IGNORECASE),
    re.compile(r"^©", re.IGNORECASE),
    re.compile(r"^\s*email\b", re.IGNORECASE),
    re.compile(r"^\s*телефон\b", re.IGNORECASE),
]

_CONTENT_LINE_CONTAINS = [
    "поделиться",
    "подписывайтесь",
    "подписаться",
    "следите за нами",
    "читайте нас",
    "rss",
    "социальных сетях",
    "подписка",
    "telegram",
    "t.me/",
    "vk.com",
    "ok.ru",
    "автор:",
    "share",
    "bookmark",
]

_BREADCRUMB_PATTERN = re.compile(r"^(главная|home)\b.*\/", re.IGNORECASE)


def _looks_like_breadcrumb(line: str) -> bool:
    if not line:
        return False
    if _BREADCRUMB_PATTERN.match(line.strip()):
        return True
    if line.count("/") >= 2 and len(line) <= 160:
        parts = [p.strip() for p in line.split("/") if p.strip()]
        if parts and parts[0].lower() in {"главная", "новости", "press-center", "пресс-центр"}:
            return True
    return False


def _smart_quotes(text: str) -> str:
    if "\"" not in text:
        return text

    def repl(match: re.Match[str]) -> str:
        inner = match.group(1)
        if "«" in inner or "»" in inner:
            return match.group(0)
        return f"«{inner}»"

    return re.sub(r"\"([^\"]+)\"", repl, text)


def clean_content_text(text: str | None, title: str | None = None) -> str:
    if not text:
        return ""

    raw_text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    if title:
        raw_text = _drop_leading_title(raw_text, title)

    cleaned_lines = []
    for raw_line in raw_text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        lowered = line.lower()
        if title and lowered == title.lower():
            continue
        if _contains_deny_phrase(line):
            parts = [
                chunk.strip()
                for chunk in re.split(r"(?<=[.!?])\s+", line)
                if chunk.strip()
            ]
            parts = [chunk for chunk in parts if not _contains_deny_phrase(chunk)]
            line = " ".join(parts).strip()
            if not line:
                continue
        if any(pattern.search(line) for pattern in _CONTENT_LINE_PATTERNS):
            continue
        if any(token in lowered for token in _CONTENT_LINE_CONTAINS):
            for token in _CONTENT_LINE_CONTAINS:
                idx = lowered.find(token)
                if idx != -1:
                    line = line[:idx].strip()
                    lowered = line.lower()
            if not line:
                continue
        if _looks_like_breadcrumb(line):
            continue
        cleaned_lines.append(line)

    if not cleaned_lines:
        return ""

    cleaned = "\n\n".join(cleaned_lines)
    cleaned = _normalize_whitespace(cleaned)
    cleaned = re.sub(r"\.\.\.+", "…", cleaned)
    cleaned = re.sub(r"(?<=\w)\s+--\s+(?=\w)", " — ", cleaned)
    cleaned = re.sub(r"(?<=\w)\s+-\s+(?=\w)", " — ", cleaned)
    cleaned = _smart_quotes(cleaned)
    cleaned = re.sub(r"\s*©[^\n]+", "", cleaned)
    cleaned = _strip_deny_phrases(cleaned)
    cleaned = _normalize_whitespace(cleaned)
    return cleaned


def _word_count(text: str) -> int:
    if not text:
        return 0
    return len(re.findall(r"\w+", text, flags=re.UNICODE))


def _coerce_msk_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        return make_aware_msk(value)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            try:
                dt = dparser.isoparse(value)
            except Exception:
                return None
        return make_aware_msk(dt)
    return None


def ensure_item_metadata(item: dict[str, object]) -> None:
    now_msk = make_aware_msk(datetime.now(MSK)).replace(microsecond=0)

    first_seen_val = item.get("first_seen")
    first_seen_dt = _coerce_msk_datetime(first_seen_val)
    if not first_seen_dt:
        first_seen_dt = now_msk
    item["first_seen"] = first_seen_dt.isoformat()

    bucket_val = item.get("bucketed_at")
    bucket_dt = _coerce_msk_datetime(bucket_val)
    if not bucket_dt:
        bucket_dt = first_seen_dt.replace(minute=0, second=0, microsecond=0)
    else:
        bucket_dt = bucket_dt.replace(minute=0, second=0, microsecond=0)
    item["bucketed_at"] = bucket_dt.isoformat()

    fetched_val = item.get("fetched_at")
    fetched_dt = _coerce_msk_datetime(fetched_val)
    if not fetched_dt:
        fetched_dt = now_msk
    item["fetched_at"] = fetched_dt.isoformat()

    published_val = item.get("published_at")
    if published_val:
        published_dt = _coerce_msk_datetime(published_val)
        trustworthy = bool(item.get("_published_at_trustworthy"))
        published_dt = validate_publication_datetime(
            published_dt,
            raw_value=published_val,
            url=str(item.get("url") or ""),
            source=str(item.get("source") or ""),
            signal="stored:published_at",
            now=fetched_dt,
        )
        # Old feeds commonly copied crawl time into published_at.  Without
        # fresh extraction provenance it is not a publication signal.
        if published_dt and not trustworthy and published_dt >= fetched_dt:
            published_dt = None
        if published_dt:
            item["published_at"] = published_dt.isoformat()
        else:
            item.pop("published_at", None)


def sort_timestamp(item: dict[str, object]) -> str:
    """Return a stable chronology key which never rewards a later refetch."""
    published = _coerce_msk_datetime(item.get("published_at"))
    fetched = _coerce_msk_datetime(item.get("fetched_at"))
    if published and fetched:
        published = validate_publication_datetime(published, now=fetched)
    if published:
        return published.isoformat()
    first_seen = _coerce_msk_datetime(item.get("first_seen"))
    return first_seen.isoformat() if first_seen else ""


def _filter_by_min_words(items: list[dict]) -> list[dict]:
    filtered: list[dict] = []
    for item in items:
        source = item.get("source") or ""
        threshold = SOURCE_MIN_WORDS.get(source, DEFAULT_MIN_WORDS)
        text = item.get("content_text") or ""
        if _word_count(text) < threshold:
            logging.debug(
                "Drop item %s due to min_words threshold %s (%s words)",
                item.get("id"),
                threshold,
                _word_count(text),
            )
            continue
        filtered.append(item)
    return filtered


def _finalize_item_schema(item: dict[str, object]) -> dict[str, object]:
    ensure_item_metadata(item)
    cleaned: dict[str, object] = {}
    for key in ESSENTIAL_ITEM_FIELDS:
        if key in item:
            cleaned[key] = item[key]
    for key in OPTIONAL_ITEM_FIELDS:
        value = item.get(key)
        if value not in (None, ""):
            cleaned[key] = value
    return cleaned


def _clone_soup(doc: BeautifulSoup | str | None) -> BeautifulSoup:
    if isinstance(doc, BeautifulSoup):
        return BeautifulSoup(str(doc), "html.parser")
    return BeautifulSoup(doc or "", "html.parser")


def _clean_for_content(soup: BeautifulSoup) -> None:
    for junk in soup.find_all(["script", "style", "nav", "footer", "aside", "form", "noscript", "iframe"]):
        junk.decompose()


def _strip_deny_phrases(text: str) -> str:
    if not text:
        return ""
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            if current:
                blocks.append(current)
                current = []
            continue
        if _contains_deny_phrase(stripped):
            continue
        current.append(stripped)
    if current:
        blocks.append(current)
    if not blocks:
        return ""
    paragraphs = [" ".join(chunk) for chunk in blocks if chunk]
    return "\n\n".join(paragraphs).strip()


def _contains_deny_phrase(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(pattern.search(lowered) for pattern in _DENY_PATTERNS)


def _selectors_for_url(url: str, selectors: list[str] | None) -> list[str]:
    combined: list[str] = []
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    for sel in HOST_CONTENT_SELECTORS.get(host, []):
        if sel and sel not in combined:
            combined.append(sel)
    for sel in selectors or []:
        if sel and sel not in combined:
            combined.append(sel)
    return combined


def _drop_leading_title(text: str, title: str | None) -> str:
    if not text:
        return ""
    if not title:
        return text.strip()
    title_norm = re.sub(r"\s+", " ", title).strip()
    if not title_norm:
        return text.strip()
    trimmed = text.lstrip()
    pattern = re.compile(rf"^{re.escape(title_norm)}[\s\-–—:]*", re.IGNORECASE)
    new_text = pattern.sub("", trimmed, count=1)
    if new_text != trimmed:
        return new_text.strip()
    lines = trimmed.splitlines()
    if lines:
        first = re.sub(r"\s+", " ", lines[0]).strip().lower()
        if first == title_norm.lower():
            return "\n".join(lines[1:]).strip()
    return trimmed.strip()


_DATED_HEADLINE_RE = re.compile(
    r"(?:^|\n)\s*(?:сегодня|вчера|\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?)\s*(?:\n|$)",
    re.IGNORECASE,
)


def _score_content_candidate(
    node, text: str, title: str | None, structural_text: str | None = None
) -> float:
    """Rank a possible body without letting a long link list win by size alone."""
    compact = re.sub(r"\s+", " ", text).strip()
    if not compact:
        return float("-inf")
    paragraphs = [
        _normalize_whitespace(p.get_text(" ", strip=True))
        for p in node.find_all("p")
        if _normalize_whitespace(p.get_text(" ", strip=True))
    ]
    links = " ".join(a.get_text(" ", strip=True) for a in node.find_all("a"))
    link_ratio = min(1.0, len(links) / max(1, len(compact)))
    list_items = len(node.find_all("li"))
    # ``structural_text`` retains separators between child elements. Cleaned
    # text intentionally does not, so it cannot reveal standalone timestamps.
    dated_headlines = len(_DATED_HEADLINE_RE.findall(structural_text or text))
    markup_size = max(1, len(str(node)))
    density = len(compact) / markup_size
    title_bonus = 0.0
    if title:
        normalized_title = re.sub(r"\s+", " ", title).strip().lower()
        heading = node.find(["h1", "h2"])
        if heading and normalized_title in re.sub(
            r"\s+", " ", heading.get_text(" ", strip=True)
        ).lower():
            title_bonus = 240.0
        elif node.find_previous("h1") is not None:
            title_bonus = 80.0
    paragraph_chars = sum(len(p) for p in paragraphs)
    return (
        len(compact) * 0.12
        + paragraph_chars * 0.35
        + min(len(paragraphs), 12) * 90
        + density * 300
        + title_bonus
        - link_ratio * 1400
        - list_items * 45
        - dated_headlines * 180
    )


def _rank_content_candidates(soup, selectors, title=None, min_words=0):
    candidates = []
    seen = set()
    for selector_index, sel in enumerate(selectors):
        try:
            nodes = soup.select(sel)
        except Exception:
            continue
        for node in nodes:
            identity = id(node)
            if identity in seen:
                continue
            seen.add(identity)
            structural_text = node.get_text("\n", strip=True)
            raw = _normalize_whitespace(structural_text)
            cleaned = clean_content_text(raw, title=title)
            cleaned = _strip_deny_phrases(cleaned or "")
            cleaned = _normalize_whitespace(cleaned)
            # Validation is deliberately after cleaning: navigation labels and a
            # duplicated page title are not usable article content.
            if not cleaned or (min_words and _word_count(cleaned) < min_words):
                continue
            score = _score_content_candidate(
                node, cleaned, title, structural_text=structural_text
            ) - selector_index * 0.01
            candidates.append((score, cleaned, node))
    return sorted(candidates, key=lambda candidate: candidate[0], reverse=True)


def extract_content_with_fallback(doc, selectors, title: str | None, min_words: int = 0):
    if isinstance(selectors, str):
        selectors = [selectors]
    ordered_selectors = []
    for sel in selectors or []:
        if sel and sel not in ordered_selectors:
            ordered_selectors.append(sel)
    for sel in DEFAULT_CONTENT_SELECTORS:
        if sel not in ordered_selectors:
            ordered_selectors.append(sel)

    soup = _clone_soup(doc)
    _clean_for_content(soup)

    search_selectors = ordered_selectors + ["article", "main", "section", "body"]
    ranked = _rank_content_candidates(soup, search_selectors, title, min_words)
    best_text = ranked[0][1] if ranked else ""

    final_text = _drop_leading_title(best_text, title)
    final_text = _strip_deny_phrases(final_text)
    final_text = _normalize_whitespace(final_text)

    if not final_text or (min_words and _word_count(final_text) < min_words):
        return None

    return final_text


def html_fragment_to_text(fragment: str) -> str:
    if not fragment:
        return ""
    soup = BeautifulSoup(fragment, "html.parser")
    for junk in soup.find_all(["script", "style", "noscript", "form", "iframe"]):
        junk.decompose()
    text = soup.get_text("\n", strip=True)
    return _normalize_whitespace(text)

def extract_content_text(soup: BeautifulSoup, selectors=None, title=None, min_words: int = 0):
    if isinstance(selectors, str):
        selectors = [selectors]
    else:
        selectors = list(selectors or [])
    _clean_for_content(soup)
    ordered = list(dict.fromkeys(selectors + DEFAULT_CONTENT_SELECTORS + ["article", "body"]))
    ranked = _rank_content_candidates(soup, ordered, title, min_words)
    return ranked[0][1] if ranked else None


JSON_LD_ARTICLE_TYPES = {
    "newsarticle",
    "article",
    "blogposting",
    "report",
}


def _json_ld_types(node: dict) -> set[str]:
    types: set[str] = set()
    node_type = node.get("@type") or node.get("type")
    if isinstance(node_type, str):
        types.add(node_type.lower())
    elif isinstance(node_type, (list, tuple, set)):
        for entry in node_type:
            if isinstance(entry, str):
                types.add(entry.lower())
    return types


def _iter_json_ld_nodes(payload) -> list[dict]:
    stack = [payload]
    results: list[dict] = []
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            results.append(current)
            for value in current.values():
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(current, list):
            for item in current:
                if isinstance(item, (dict, list)):
                    stack.append(item)
    return results


def extract_json_ld_article_body(soup: BeautifulSoup) -> str | None:
    candidates: list[tuple[int, int, str]] = []
    priority_map = {"articlebody": 0, "article_body": 0, "text": 1, "description": 2}
    for script in soup.find_all("script"):
        script_type = script.get("type") or ""
        if "ld+json" not in script_type.lower():
            continue
        raw = script.string or script.get_text()
        if not raw:
            continue
        raw = raw.strip()
        if not raw:
            continue
        data = None
        for candidate in (raw, raw.rstrip(";")):
            try:
                data = json.loads(candidate)
                break
            except Exception:
                continue
        if data is None:
            continue
        for node in _iter_json_ld_nodes(data):
            if not isinstance(node, dict):
                continue
            types = _json_ld_types(node)
            if types and not any(t in JSON_LD_ARTICLE_TYPES for t in types):
                continue
            for key in ("articleBody", "articlebody", "text", "description"):
                value = node.get(key)
                if isinstance(value, str) and value.strip():
                    text = html_fragment_to_text(value)
                    cleaned = clean_content_text(text)
                    if not cleaned:
                        continue
                    words = _word_count(cleaned)
                    if words < 40:
                        continue
                    priority = priority_map.get(key.lower(), 3)
                    candidates.append((priority, words, cleaned))
    if not candidates:
        return None
    best = max(candidates, key=lambda entry: (entry[1], -entry[0]))
    return best[2]

META_DATE_KEYS = [
    ("meta", "property", "article:published_time"),
    ("meta", "property", "article:modified_time"),
    ("meta", "property", "og:published_time"),
    ("meta", "property", "og:updated_time"),
    ("meta", "property", "article:published"),
    ("meta", "name", "pubdate"),
    ("meta", "name", "date"),
    ("meta", "name", "publish-date"),
    ("meta", "name", "publication_date"),
    ("meta", "name", "dc.date"),
    ("meta", "name", "dcterms.date"),
    ("meta", "itemprop", "datePublished"),
    ("meta", "itemprop", "dateCreated"),
]

def extract_date_candidates(soup: BeautifulSoup):
    out = []
    # <time datetime="...">
    for t in soup.find_all("time"):
        dt = t.get("datetime") or t.get("content") or ""
        if dt:
            out.append(dt)
        txt = t.get_text(strip=True)
        if txt:
            out.append(txt)
    # meta
    for tag, attr, key in META_DATE_KEYS:
        for m in soup.find_all(tag, attrs={attr: key}):
            val = m.get("content") or m.get("datetime") or ""
            if val:
                out.append(val)
    # json-ld
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.get_text(strip=True))
        except Exception:
            continue
        def walk(obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k in ("datePublished", "dateModified", "uploadDate"):
                        if isinstance(v, str):
                            out.append(v)
                    walk(v)
            elif isinstance(obj, list):
                for it in obj:
                    walk(it)
        walk(data)
    # Common date containers
    for sel in [
        "span.date", ".news-date", ".news__date", ".article-date", ".post-date",
        ".entry-date", ".published", ".article__date", ".article-info__date",
        ".date-publication", ".date-time", ".meta__date", ".time__value",
        ".date", ".time", "time[itemprop='datePublished']", ".news-detail__date",
        ".presscenter_event_date", ".blog-post__date", ".news-item__date",
        ".article__meta-date", ".card__date"
    ]:
        for el in soup.select(sel):
            txt = el.get_text(" ", strip=True)
            if txt:
                out.append(txt)
    # De-duplicate preserving order
    seen = set()
    uniq = []
    for s in out:
        if s in seen:
            continue
        seen.add(s)
        uniq.append(s)
    return uniq[:20]

def try_parse_any_date(
    candidates, *, url=None, source=None, signal="heuristic", now=None,
    diagnostics=None,
):
    default_base = make_aware_msk(datetime.now(MSK).replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0))
    for raw in candidates:
        s = raw.strip()
        low = s.lower()
        if "сегодня" in low or "today" in low:
            m = re.search(r"(\d{1,2}):(\d{2})", low)
            hh, mm = (int(m.group(1)), int(m.group(2))) if m else (12, 0)
            dt = make_aware_msk(datetime.now(MSK)).replace(hour=hh, minute=mm, second=0, microsecond=0)
            return validate_publication_datetime(dt, raw_value=s, url=url, source=source, signal=signal, now=now, diagnostics=diagnostics)
        if "вчера" in low or "yesterday" in low:
            m = re.search(r"(\d{1,2}):(\d{2})", low)
            hh, mm = (int(m.group(1)), int(m.group(2))) if m else (12, 0)
            dt = make_aware_msk(datetime.now(MSK) - timedelta(days=1)).replace(hour=hh, minute=mm, second=0, microsecond=0)
            return validate_publication_datetime(dt, raw_value=s, url=url, source=source, signal=signal, now=now, diagnostics=diagnostics)
        # Try ISO-like first
        try:
            dt = finalize_datetime(dparser.isoparse(s))
            if dt:
                return validate_publication_datetime(dt, raw_value=s, url=url, source=source, signal=signal, now=now, diagnostics=diagnostics)
        except Exception:
            pass
        # Try generic parser in day-first mode
        try:
            dt = finalize_datetime(dparser.parse(
                s,
                dayfirst=True,
                fuzzy=True,
                default=default_base,
            ))
            if dt:
                return validate_publication_datetime(dt, raw_value=s, url=url, source=source, signal=signal, now=now, diagnostics=diagnostics)
        except Exception:
            pass
        # Try Russian words
        dt = parse_ru_date_words(s)
        if dt:
            dt = finalize_datetime(dt)
            if dt:
                return validate_publication_datetime(dt, raw_value=s, url=url, source=source, signal=signal, now=now, diagnostics=diagnostics)
    return None


def _parse_datetime_signal(
    value: str | None, signal: str, *, url=None, source=None, now=None,
    diagnostics=None,
) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    dt = None
    try:
        dt = finalize_datetime(dparser.isoparse(value))
    except Exception:
        dt = None
    if dt is None:
        try:
            dt = finalize_datetime(
                dparser.parse(
                    value,
                    dayfirst=True,
                    fuzzy=True,
                )
            )
        except Exception:
            dt = None
    if dt is None:
        words_dt = parse_ru_date_words(value)
        if words_dt:
            dt = finalize_datetime(words_dt)
    if dt:
        logging.debug("Published time signal (%s): %s -> %s", signal, value, dt.isoformat())
        return validate_publication_datetime(
            dt, raw_value=value, url=url, source=source, signal=signal, now=now,
            diagnostics=diagnostics,
        )
    return None


def extract_published_datetime(
    soup: BeautifulSoup,
    url: str | None = None,
    source: str | None = None,
    diagnostics: PublicationDiagnostics | None = None,
) -> datetime | None:
    seen_candidates: set[tuple[str, str]] = set()

    def attempt(value: str | None, signal: str) -> datetime | None:
        if not value:
            return None
        candidate = value.strip()
        if not candidate:
            return None
        key = (signal, candidate)
        if key in seen_candidates:
            return None
        seen_candidates.add(key)
        return _parse_datetime_signal(candidate, signal, url=url, source=source, diagnostics=diagnostics)

    # Explicit publication metadata is trustworthy.  Modified/upload dates
    # are intentionally not fallbacks: they describe page activity, not the
    # article's original chronology.
    for tag in soup.find_all("meta", attrs={"property": "article:published_time"}):
        dt = attempt(tag.get("content") or tag.get("value"), "meta[property=article:published_time]")
        if dt:
            return dt

    for tag in soup.find_all("meta", attrs={"name": "pubdate"}):
        dt = attempt(tag.get("content") or tag.get("value"), "meta[name=pubdate]")
        if dt:
            return dt

    article_roots = list(soup.select(
        "article, [itemscope][itemtype*='Article'], [itemtype*='NewsArticle']"
    ))
    for root in article_roots:
        for node in root.find_all(attrs={"itemprop": "datePublished"}):
            dt = attempt(
                node.get("content")
                or node.get("datetime")
                or node.get_text(" ", strip=True),
                "[itemprop=datePublished]",
            )
            if dt:
                return dt

    # Only article-scoped time elements are independent evidence.  Global
    # time/date widgets are commonly current-dated sidebar or header chrome.
    for t in (tag for root in article_roots for tag in root.find_all("time")):
        for candidate in (t.get("datetime"), t.get("content"), t.get_text(" ", strip=True)):
            dt = attempt(candidate, "<time>")
            if dt:
                return dt

    # JSON-LD must identify an Article/NewsArticle object and datePublished.
    for script in soup.find_all("script"):
        script_type = script.get("type") or ""
        if "ld+json" not in script_type.lower():
            continue
        raw = script.string or script.get_text()
        if not raw:
            continue
        raw = raw.strip()
        if not raw:
            continue
        data = None
        for candidate in (raw, raw.rstrip(";")):
            try:
                data = json.loads(candidate)
                break
            except Exception:
                continue
        if data is None:
            continue
        for node in _iter_json_ld_nodes(data):
            if not isinstance(node, dict):
                continue
            types = _json_ld_types(node)
            if not types or not any(t in JSON_LD_ARTICLE_TYPES for t in types):
                continue
            raw_val = node.get("datePublished")
            if isinstance(raw_val, str):
                dt = attempt(raw_val, "json_ld:datePublished")
                if dt:
                    return dt

    # A date embedded in the canonical article URL is stable and independent
    # of page chrome.  Try it even after a suspicious metadata value failed.
    canonical = extract_canonical_url(soup, url or "") or url or ""
    match = re.search(r"/(20\d{2})/([01]\d)/([0-3]\d)(?:/|$)", canonical)
    if match:
        try:
            dt = validate_publication_datetime(
                datetime(*map(int, match.groups())), raw_value=match.group(0),
                url=url, source=source, signal="canonical_url:path",
                diagnostics=diagnostics,
            )
        except ValueError:
            dt = None
        if dt:
            return dt

    return None

# ---- Parsing ----
def extract_title(soup: BeautifulSoup):
    for sel in ["meta[property='og:title']", "meta[name='og:title']", "meta[name='title']"]:
        tag = soup.select_one(sel)
        if tag and tag.get("content"):
            return tag["content"].strip()
    h1 = soup.find(["h1", "h2"])
    if h1:
        return h1.get_text(" ", strip=True)
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    return None


CANONICAL_QUERY_DROP_PREFIXES = ("utm_",)
CANONICAL_QUERY_DROP_KEYS = {"ysclid", "yclid", "fbclid", "gclid", "per-page", "page", "utm_referrer"}


def _normalize_canonical_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    if not parsed.scheme or parsed.scheme not in {"http", "https"}:
        return None
    path = parsed.path or "/"
    path = re.sub(r"/+", "/", path)
    if path != "/":
        path = path.rstrip("/")
        if not path:
            path = "/"
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    filtered: list[tuple[str, str]] = []
    for key, value in query_pairs:
        lowered = key.lower()
        if any(lowered.startswith(prefix) for prefix in CANONICAL_QUERY_DROP_PREFIXES):
            continue
        if lowered in CANONICAL_QUERY_DROP_KEYS:
            continue
        filtered.append((key, value))
    filtered.sort()
    query = urlencode(filtered, doseq=True)
    normalized = parsed._replace(path=path, query=query, fragment="")
    return normalized.geturl()


def _content_fingerprint(text: str | None) -> str | None:
    if not text:
        return None
    normalized = re.sub(r"\s+", " ", text).strip().lower()
    if not normalized:
        return None
    if len(normalized) > 4000:
        normalized = normalized[:4000]
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def extract_canonical_url(soup: BeautifulSoup, base_url: str) -> str | None:
    if not soup:
        return None
    for link in soup.find_all("link"):
        rel = link.get("rel")
        if not rel:
            continue
        if isinstance(rel, (list, tuple)):
            rels = [str(r).lower() for r in rel]
        else:
            rels = [part.lower() for part in str(rel).split() if part]
        if "canonical" in rels:
            href = link.get("href")
            if href:
                return _normalize_canonical_url(urljoin(base_url, href.strip()))
    for sel in ["meta[property='og:url']", "meta[name='og:url']"]:
        tag = soup.select_one(sel)
        if tag:
            href = tag.get("content")
            if href:
                return _normalize_canonical_url(urljoin(base_url, href.strip()))
    return None


def _article_url_rejection_reason(url: str, src: dict | None) -> str | None:
    """Return why *url* cannot identify an article for the configured source."""
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return "invalid_url"
    if parsed.path.rstrip("/") == "":
        return "site_root"
    if not src:
        return None
    base_host = urlparse(src.get("base_url", "")).netloc.removeprefix("www.")
    if base_host and parsed.netloc.removeprefix("www.") != base_host:
        return "outside_source_domain"
    if is_listing_url(url, start_url=src.get("start_url")):
        return "listing_page"
    # Canonical normalization intentionally removes a trailing path slash. Some
    # discovery rules describe the source's raw links instead (including the
    # slash immediately before a query string), so match both equivalent forms.
    rule_urls = {url}
    if parsed.path != "/" and not parsed.path.endswith("/"):
        rule_urls.add(parsed._replace(path=f"{parsed.path}/").geturl())
    patterns = src.get("include_patterns") or []
    if isinstance(patterns, str):
        patterns = [patterns]
    if patterns and not any(
        pattern in candidate for pattern in patterns for candidate in rule_urls
    ):
        return "does_not_match_include_patterns"
    include_regex = src.get("include_regex")
    regexes = [include_regex] if isinstance(include_regex, str) else (include_regex or [])
    if regexes and not any(
        re.search(pattern, candidate)
        for pattern in regexes
        for candidate in rule_urls
    ):
        return "does_not_match_include_regex"
    exclude_regex = src.get("exclude_regex")
    regexes = [exclude_regex] if isinstance(exclude_regex, str) else (exclude_regex or [])
    if any(
        re.search(pattern, candidate)
        for pattern in regexes
        for candidate in rule_urls
    ):
        return "matches_exclude_regex"
    return None


def _validated_canonical_url(
    canonical_url: str | None, fetched_url: str, src: dict | None, source_name: str
) -> str | None:
    """Reject malformed metadata when the fetched URL is demonstrably an article."""
    if not canonical_url or not src or _article_url_rejection_reason(fetched_url, src):
        return canonical_url
    reason = _article_url_rejection_reason(canonical_url, src)
    if not reason:
        return canonical_url
    summary = SOURCE_SUMMARY[source_name]
    counts = summary.setdefault("canonical_rejections_by_reason", {})
    counts[reason] = int(counts.get(reason, 0)) + 1
    samples = summary.setdefault("canonical_rejection_samples", [])
    if len(samples) < PUBLICATION_REJECTION_SAMPLE_LIMIT:
        samples.append({
            "fetched_url": fetched_url,
            "canonical_url": canonical_url,
            "reason": reason,
        })
    logging.warning("Rejected canonical for %s (%s): %s", fetched_url, reason, canonical_url)
    return None


def extract_article_content(
    url: str,
    html: str,
    selectors: list[str] | str | None = None,
    title: str | None = None,
    src: dict | None = None,
    min_words: int | None = None,
):
    soup = BeautifulSoup(html or "", "html.parser")
    if title is None:
        title = extract_title(soup) or url
    if isinstance(selectors, str):
        selector_list = [selectors]
    else:
        selector_list = list(selectors or [])
    combined_selectors = _selectors_for_url(url, selector_list)

    primary_soup = _clone_soup(soup)
    if min_words is None:
        min_words = _effective_source_min_words(src, url) if src else 0
    raw_text = extract_content_text(
        primary_soup, selectors=combined_selectors, title=title, min_words=min_words
    )
    content_source = "primary_selectors" if raw_text else ""
    content_text = clean_content_text(raw_text, title=title)

    if _is_short_content(content_text):
        json_ld_body = extract_json_ld_article_body(soup)
        json_ld_clean = clean_content_text(json_ld_body, title=title)
        if json_ld_clean and _word_count(json_ld_clean) > _word_count(content_text or ""):
            content_text = json_ld_clean
            content_source = "jsonld"

    if not content_text or (min_words and _word_count(content_text) < min_words):
        fallback_text = extract_content_with_fallback(
            soup, combined_selectors, title, min_words=min_words
        )
        fallback_clean = clean_content_text(fallback_text, title=title)
        if fallback_clean:
            host = urlparse(url).netloc.lower()
            if host.endswith("government.ru") and _word_count(fallback_clean) < 100:
                fallback_clean = ""
            if fallback_clean:
                content_text = fallback_clean
                content_source = "fallback_selectors"

    if not content_source:
        content_source = "primary_selectors"

    return content_text, soup, title, content_source


def build_item(
    url: str,
    source_name: str,
    html: str,
    content_selectors=None,
    src: dict | None = None,
    pre_extracted_content: str | None = None,
):
    publication_diagnostics = PublicationDiagnostics(source_name)
    amp_used = False
    selectors = content_selectors
    content_text: str | None
    title: str
    content_source = "primary_selectors"
    resolved_min_words = SOURCE_MIN_WORDS.get(
        source_name, _effective_source_min_words(src, url) if src else DEFAULT_MIN_WORDS
    )
    if pre_extracted_content is not None:
        soup = BeautifulSoup(html or "", "html.parser")
        title = extract_title(soup) or url
        content_text = clean_content_text(pre_extracted_content, title=title)
        content_source = "api"
    else:
        content_text, soup, title, content_source = extract_article_content(
            url,
            html,
            selectors=selectors,
            title=None,
            src=src,
            min_words=resolved_min_words,
        )

    if (not content_text or _is_short_content(content_text)) and html.strip():
        amp_html, amp_label = fetch_amp_if_available(url, soup, src=src)
        if amp_html:
            amp_text, _, _, amp_source = extract_article_content(
                url,
                amp_html,
                selectors=selectors,
                title=title,
                src=src,
                min_words=resolved_min_words,
            )
            if amp_text and not _is_short_content(amp_text):
                content_text = amp_text
                content_source = amp_label or amp_source or "amp"
                amp_used = True

    dt = extract_published_datetime(
        soup, url, source_name, diagnostics=publication_diagnostics
    )

    if dt is None:
        m = re.search(r"/(20\d{2})/([01]\d)/([0-3]\d)/", url)
        if m:
            y, mo, d = map(int, m.groups())
            try:
                dt = validate_publication_datetime(
                    datetime(y, mo, d),
                    raw_value=m.group(0),
                    url=url,
                    source=source_name,
                    signal="url:path",
                    diagnostics=publication_diagnostics,
                    allowance=PUBLICATION_CLOCK_SKEW,
                )
            except ValueError:
                dt = None

    reported_canonical_url = extract_canonical_url(soup, url)
    canonical_url = _validated_canonical_url(
        reported_canonical_url, url, src, source_name
    )
    # A rejected canonical may already have poisoned persistent identity state
    # during an earlier crawl.  Rebuilds must recover from that state rather
    # than continuing to alias distinct articles to the malformed canonical.
    canonical_was_rejected = bool(reported_canonical_url and not canonical_url)
    url_key = _normalize_canonical_url(url) or url
    alias_map = STATE.setdefault("aliases", {})
    content_hashes = STATE.setdefault("content_hashes", {})
    canonical_key = canonical_url or (
        url_key if canonical_was_rejected else alias_map.get(url_key) or url_key
    )

    fingerprint = _content_fingerprint(content_text)
    if fingerprint:
        existing = None if canonical_was_rejected else content_hashes.get(fingerprint)
        if existing:
            canonical_key = existing
        else:
            content_hashes[fingerprint] = canonical_key

    alias_map[url_key] = canonical_key
    alias_map[canonical_key] = canonical_key
    if fingerprint:
        content_hashes[fingerprint] = canonical_key

    canonical_ids = STATE.setdefault("canonical_item_ids", {})
    id_source = canonical_key or url
    item_id = None if canonical_was_rejected else canonical_ids.get(id_source)
    if not item_id:
        item_id = hashlib.sha256(id_source.encode("utf-8")).hexdigest()
        canonical_ids[id_source] = item_id
    canonical_ids[url] = item_id
    canonical_ids[url_key] = item_id
    if canonical_key and canonical_key != url_key:
        canonical_ids[canonical_key] = item_id

    now_msk = make_aware_msk(datetime.now(MSK)).replace(second=0, microsecond=0)
    first_seen_map = STATE.setdefault("first_seen", {})
    cached_first_seen = first_seen_map.get(item_id)
    if cached_first_seen:
        try:
            first_seen_dt = datetime.fromisoformat(cached_first_seen)
        except ValueError:
            first_seen_dt = now_msk
            first_seen_map[item_id] = first_seen_dt.isoformat()
    else:
        first_seen_dt = now_msk
        first_seen_map[item_id] = first_seen_dt.isoformat()

    bucketed_at = first_seen_dt.replace(minute=0, second=0, microsecond=0)
    fetched_at = now_msk

    item: dict[str, object] = {
        "id": item_id,
        "source": source_name,
        "title": title,
        "url": url,
        "content_text": content_text,
        "first_seen": first_seen_dt.isoformat(),
        "bucketed_at": bucketed_at.isoformat(),
        "fetched_at": fetched_at.isoformat(),
    }
    item["_content_source"] = content_source
    if dt:
        item["published_at"] = dt.isoformat()
        item["_published_at_trustworthy"] = True
    if canonical_url and canonical_url != url:
        item["canonical_url"] = canonical_url
    if amp_used:
        item["_amp_used"] = True

    return item

API_URL_KEYS = [
    "url",
    "link",
    "slug",
    "path",
    "permalink",
    "full_url",
    "fullUrl",
]
API_TITLE_KEYS = ["title", "name", "headline", "caption", "subject", "heading"]
API_DATE_KEYS = [
    "published_at",
    "publishedAt",
    "publishDate",
    "publish_date",
    "date",
    "createdAt",
    "created_at",
    "updatedAt",
]
API_DATE_HUMAN_KEYS = ["publishDateRus", "date_rus", "dateHuman"]
API_CONTENT_KEYS = [
    "content",
    "text",
    "body",
    "articleBody",
    "article_body",
    "articleText",
    "article_text",
    "content_html",
    "text_html",
    "fullText",
    "full_text",
    "contentHtml",
    "html",
    "description",
    "content_text",
    "contentText",
    "bodyText",
    "body_text",
]
API_FALLBACK_CONTENT_KEYS = ["excerpt", "summary", "lead", "teaser"]


def _first_non_empty(containers: list[dict], keys: list[str]) -> str | None:
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in keys:
            val = container.get(key)
            if isinstance(val, str):
                stripped = val.strip()
                if stripped:
                    return stripped
    return None


def _object_path(value, path: str):
    """Return a value from a JSON object using a dotted path (lists are supported)."""
    current = value
    for part in (path or "").split("."):
        if not part:
            continue
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else None
        else:
            return None
    return current


def _api_items(payload, src: dict) -> list:
    """Extract records from both flat and wrapped first-party API responses."""
    paths = src.get("api_items_path", ["data", "items", "results"])
    if isinstance(paths, str):
        paths = [paths]
    if isinstance(payload, list):
        return payload
    for path in paths:
        candidate = _object_path(payload, path)
        if isinstance(candidate, list):
            return candidate
    return []


def _erz_structured_records(payload, src: dict) -> tuple[list[dict], bool]:
    """Map ERZ's ``news/list/short`` response into the generic API contract.

    ERZ cards contain image URLs and other first-party links, so URLs from the
    response are deliberately ignored.  The public article route is derived
    only from the stable ``latinTitle`` slug and checked against source policy.
    """
    if not isinstance(payload, list):
        return [], False
    records: list[dict] = []
    schema_ok = True
    for raw in payload:
        if not isinstance(raw, dict):
            schema_ok = False
            continue
        record_id, slug, title = raw.get("id"), raw.get("latinTitle"), raw.get("title")
        if record_id in (None, "") or not all(isinstance(v, str) and v.strip() for v in (slug, title)):
            schema_ok = False
            continue
        slug = slug.strip().strip("/")
        if "/" in slug or not re.fullmatch(r"[a-z0-9-]+", slug):
            schema_ok = False
            continue
        article_url = urljoin(src.get("base_url", "https://erzrf.ru"), f"/news/{slug}")
        if _article_url_rejection_reason(article_url, src):
            schema_ok = False
            continue
        tags = raw.get("tags")
        raw_date = raw.get("date")
        date_value = _erz_publication_date(raw_date) if isinstance(raw_date, str) else None
        records.append({
            "id": str(record_id),
            "url": article_url,
            "title": title.strip(),
            "publishedAt": date_value,
            # Keep the card annotation as metadata, not an API content key:
            # list/short is discovery-only and must never replace the article.
            "structured_summary": raw.get("annotation")
            if isinstance(raw.get("annotation"), str) else None,
            "tags": [tag.strip() for tag in tags if isinstance(tag, str) and tag.strip()]
            if isinstance(tags, list) else [],
        })
    return records, schema_ok


def _erz_publication_date(value: str) -> str | None:
    """Convert the Russian date label returned by ERZ into an ISO timestamp."""
    months = {
        "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
        "мая": 5, "июня": 6, "июля": 7, "августа": 8,
        "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
    }
    match = re.fullmatch(
        r"\s*(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?\s+(\d{1,2}):(\d{2})\s*",
        value.lower(),
    )
    if not match or match.group(2) not in months:
        return None
    day, month_name, year, hour, minute = match.groups()
    inferred_year = datetime.now(MSK).year
    try:
        return MSK.localize(datetime(
            int(year or inferred_year), months[month_name], int(day), int(hour), int(minute)
        )).isoformat()
    except ValueError:
        return None


def _api_endpoints(src: dict) -> list[str]:
    """Expand an endpoint template into a bounded set of discovery pages."""
    configured = src.get("api_endpoints")
    if configured:
        return [str(endpoint) for endpoint in configured]
    endpoint = src.get("api_endpoint")
    pages = int(src.get("api_endpoint_pages", 1))
    if pages <= 1:
        return [endpoint] if endpoint else []
    start = int(src.get("api_page_start", 1))
    page_param = str(src.get("api_page_param", "page"))
    endpoints = []
    for page in range(start, start + pages):
        if "{page}" in endpoint:
            endpoints.append(endpoint.format(page=page))
        else:
            separator = "&" if "?" in endpoint else "?"
            endpoints.append(f"{endpoint}{separator}{page_param}={page}")
    return endpoints


def harvest_json_source(src: dict, force: bool = False):
    endpoints = _api_endpoints(src)
    if not endpoints:
        logging.warning("  missing api_endpoint for %s", src.get("name"))
        return []

    src_name = src.get("name", "")
    endpoint = endpoints[0]
    logging.info("Harvest API: %s — %s", src_name, ", ".join(endpoints))

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Language": "ru,en;q=0.9",
    }
    payloads = []
    response_texts = []
    try:
        for endpoint in endpoints:
            host = urlparse(endpoint).netloc
            delay = HOST_DELAY_OVERRIDES.get(host, HOST_DELAY_DEFAULT)
            sleep_for = _last_req_at[host] + delay - time.time()
            if sleep_for > 0:
                time.sleep(sleep_for)
            resp = SESSION.get(endpoint, headers=headers, timeout=REQUEST_TIMEOUT)
            _last_req_at[host] = time.time()
            if resp.status_code == 429:
                ra = resp.headers.get("Retry-After")
                try:
                    wait = int(ra) if ra else 5
                except ValueError:
                    wait = 5
                logging.warning(
                    "429 Too Many Requests (API): %s -> sleep %ss", endpoint, wait
                )
                time.sleep(wait)
                resp = SESSION.get(endpoint, headers=headers, timeout=REQUEST_TIMEOUT)
                _last_req_at[host] = time.time()
            resp.raise_for_status()
            response_texts.append(resp.text)
            payloads.append(resp.json())
    except ValueError as exc:
        logging.error("  invalid JSON for %s: %s", src.get("name"), exc)
        SOURCE_SUMMARY[src_name]["index_fetch_status"] = "parser_error"
        SOURCE_SUMMARY[src_name]["last_error"] = str(exc)
        if src.get("structured_adapter") == "erz":
            SOURCE_SUMMARY[src_name]["structured_endpoint_fetch_failures"] += 1
        if src.get("html_fallback_on_empty_api"):
            return harvest_source(src, force=ARGS.rebuild if ARGS else False)
        return []
    except requests.RequestException as exc:
        logging.warning("API fetch failed for %s: %s", src_name, exc)
        SOURCE_SUMMARY[src_name]["index_fetch_status"] = "failed"
        SOURCE_SUMMARY[src_name]["last_error"] = str(exc)
        if src.get("structured_adapter") == "erz":
            SOURCE_SUMMARY[src_name]["structured_endpoint_fetch_failures"] += 1
        if src.get("html_fallback_on_empty_api"):
            logging.info("API failed for %s — falling back to HTML index", src_name)
            return harvest_source(src, force=ARGS.rebuild if ARGS else False)
        raise

    text = "\n".join(response_texts)
    SOURCE_SUMMARY[src_name]["index_fetch_status"] = "fetched"
    idx_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    ih = STATE.setdefault("index_hash", {})
    digest_key = "|".join(endpoints)
    if not force and ih.get(digest_key) == idx_digest:
        logging.info("Index unchanged (API): %s — %s", src.get("name"), endpoint)
        SOURCE_SUMMARY[src_name]["index_fetch_status"] = "unchanged"
    ih[endpoint] = idx_digest

    data = []
    for payload in payloads:
        if src.get("structured_adapter") == "erz":
            records, schema_ok = _erz_structured_records(payload, src)
            data.extend(records)
            if not schema_ok:
                SOURCE_SUMMARY[src_name]["structured_schema_mismatches"] += 1
        else:
            data.extend(_api_items(payload, src))
    invalid_payload = payloads and not data and any(
        not isinstance(payload, (dict, list)) for payload in payloads
    )
    if not isinstance(data, list) or invalid_payload:
        logging.warning("  unexpected API payload for %s", src.get("name"))
        if src.get("html_fallback_on_empty_api"):
            logging.info("API unexpected payload for %s — falling back to HTML index", src_name)
            return harvest_source(src, force=ARGS.rebuild if ARGS else False)
        return []

    base_url = src.get("base_url") or endpoint
    max_links = int(src.get("max_links", MAX_LINKS_PER_SOURCE))
    min_words = SOURCE_MIN_WORDS.get(src_name, _effective_source_min_words(src))
    extraction_fingerprint = _extraction_fingerprint(src, min_words)
    SOURCE_SUMMARY[src_name]["min_words"] = min_words
    summary_total_before = SOURCE_SUMMARY[src_name]["total"]

    if not data:
        if src.get("structured_adapter") == "erz":
            SOURCE_SUMMARY[src_name]["structured_zero_records"] += 1
        if src.get("html_fallback_on_empty_api"):
            logging.info("API empty/too short for %s — falling back to HTML index", src_name)
            return harvest_source(src, force=ARGS.rebuild if ARGS else False)
        return []
    seen_map = STATE.setdefault("seen_urls", {})
    already_seen_list = list(seen_map.get(src["name"], []))
    already_seen = set(already_seen_list)

    entries = []
    seen_links = set()
    for entry in data:
        containers = [entry]
        attributes = entry.get("attributes") if isinstance(entry, dict) else None
        if isinstance(attributes, dict):
            containers.append(attributes)
        raw_link = _first_non_empty(containers, API_URL_KEYS)
        if not raw_link:
            continue
        if not raw_link.startswith("http"):
            url = urljoin(base_url, raw_link)
        else:
            url = raw_link
        if is_listing_url(url, start_url=src.get("start_url")):
            SOURCE_SUMMARY[src_name]["listing"] += 1
            if ARGS and getattr(ARGS, "debug", False):
                logging.debug("Filtered listing URL: %s", url)
            continue
        if url in seen_links:
            continue
        seen_links.add(url)
        entries.append((url, entry, containers))
        if len(entries) >= max_links:
            break

    SOURCE_SUMMARY[src_name]["raw_link_candidates"] = len(data)
    SOURCE_SUMMARY[src_name]["accepted_links"] = len(entries)

    entry_urls = [url for url, _, _ in entries]
    STATE.setdefault("candidate_urls", {})[src_name] = entry_urls
    url_states = _source_url_states(src_name)

    if force:
        new_entries = entries
    else:
        new_entries = [
            it for it in entries
            if _should_attempt_url(url_states.get(it[0]), extraction_fingerprint)
        ]
        if not new_entries:
            logging.info("  no new links for %s", src["name"])
            return []
    SOURCE_SUMMARY[src_name]["attempted_articles"] += len(new_entries)

    items = []
    processed_links = []
    for idx, (url, entry, containers) in enumerate(new_entries):
        if runtime_expired():
            logging.info(
                "  stop fetching more API items for %s due to max-runtime",
                src.get("name"),
            )
            break
        if (
            ARGS
            and getattr(ARGS, "smoke", False)
            and ARGS.limit_per_source is not None
            and idx >= ARGS.limit_per_source
        ):
            if getattr(ARGS, "debug", False):
                logging.debug(
                    "Skip deep fetch for %s (limit-per-source)",
                    url,
                )
            break
        try:
            api_text = None
            fallback_text = None
            for container in containers:
                if not isinstance(container, dict):
                    continue
                for key in API_CONTENT_KEYS:
                    raw_val = container.get(key)
                    if not isinstance(raw_val, str):
                        continue
                    val = raw_val.strip()
                    if not val:
                        continue
                    if "<" in val and ">" in val and re.search(r"<[a-zA-Z][^>]*>", val):
                        text_val = html_fragment_to_text(val)
                    else:
                        text_val = val
                    if text_val:
                        api_text = text_val
                        break
                if api_text:
                    break
                for key in API_FALLBACK_CONTENT_KEYS:
                    raw_val = container.get(key)
                    if not isinstance(raw_val, str):
                        continue
                    stripped = raw_val.strip()
                    if stripped:
                        if "<" in stripped and ">" in stripped and re.search(r"<[a-zA-Z][^>]*>", stripped):
                            fallback_text = html_fragment_to_text(stripped)
                        else:
                            fallback_text = stripped
                if api_text:
                    break

            if not api_text and fallback_text:
                api_text = fallback_text

            used_api_payload = False
            if api_text:
                html = ""
                item = _build_item_with_retries(
                    url,
                    src,
                    src_name,
                    html,
                    content_selectors=src.get("content_selectors"),
                    src=src,
                    pre_extracted_content=api_text,
                )
                content_text = item.get("content_text") or ""
                word_count = _word_count(content_text)
                if (not content_text.strip()) or (min_words and word_count < min_words):
                    if ARGS and getattr(ARGS, "debug", False):
                        logging.debug(
                            "API fallback to HTML for %s (words=%d, min=%d)",
                            url,
                            word_count,
                            min_words,
                        )
                    html = _with_article_retries(src, url, lambda: fetch_page(url, src=src))
                    item = _build_item_with_retries(
                        url,
                        src,
                        src_name,
                        html,
                        content_selectors=src.get("content_selectors"),
                        src=src,
                    )
                    content_text = item.get("content_text") or ""
                    word_count = _word_count(content_text)
                else:
                    used_api_payload = True
            else:
                html = _with_article_retries(src, url, lambda: fetch_page(url, src=src))
                item = _build_item_with_retries(
                    url,
                    src,
                    src_name,
                    html,
                    content_selectors=src.get("content_selectors"),
                    src=src,
                )
                content_text = item.get("content_text") or ""
                word_count = _word_count(content_text)
            if used_api_payload:
                SOURCE_SUMMARY[src_name]["api"] += 1
            title = _first_non_empty(containers, API_TITLE_KEYS)
            if title:
                item["title"] = title.strip()
            date_val = _first_non_empty(containers, API_DATE_KEYS)
            parsed_dt = None
            if date_val:
                parsed_dt = _parse_datetime_signal(
                    date_val, "api:date", url=url, source=src_name,
                    diagnostics=PublicationDiagnostics(src_name),
                )
            if not parsed_dt:
                human_date = _first_non_empty(containers, API_DATE_HUMAN_KEYS)
                if human_date:
                    parsed_dt = try_parse_any_date(
                        [human_date], url=url, source=src_name, signal="api:human_date",
                        diagnostics=PublicationDiagnostics(src_name),
                    )
            if parsed_dt:
                item["published_at"] = parsed_dt.isoformat()
            if src.get("structured_adapter") == "erz":
                item["source_record_id"] = str(entry["id"])
                item["tags"] = list(entry.get("tags") or [])
                if entry.get("structured_summary"):
                    item["summary"] = entry["structured_summary"]
            content_text = item.get("content_text") or ""
            word_count = _word_count(content_text)
            content_source_label = item.pop("_content_source", None)
            drop_source = content_source_label or ("api" if used_api_payload else "html")
            if ARGS and getattr(ARGS, "debug", False):
                logging.debug(
                    "Content source for %s: %s (%d words)",
                    url,
                    drop_source,
                    word_count,
                )
            amp_flag = item.pop("_amp_used", False)
            if not content_text.strip():
                logging.debug("drop empty: %s source=%s", url, drop_source)
                SOURCE_SUMMARY[src_name]["empty"] += 1
                if amp_flag:
                    SOURCE_SUMMARY[src_name]["amp"] += 1
                _record_extraction_failure(
                    src_name, url, kind="empty", error="empty extraction",
                    fingerprint=extraction_fingerprint,
                )
                if src.get("structured_adapter") == "erz":
                    SOURCE_SUMMARY[src_name]["structured_article_extraction_failures"] += 1
                continue
            if amp_flag:
                SOURCE_SUMMARY[src_name]["amp"] += 1
            if min_words and word_count < min_words:
                logging.debug(
                    "drop short: %s words=%d min=%d source=%s",
                    url,
                    word_count,
                    min_words,
                    drop_source,
                )
                SOURCE_SUMMARY[src_name]["short"] += 1
                _record_extraction_failure(
                    src_name, url, kind="short",
                    error=f"short extraction: {word_count} words",
                    fingerprint=extraction_fingerprint,
                )
                if src.get("structured_adapter") == "erz":
                    SOURCE_SUMMARY[src_name]["structured_article_extraction_failures"] += 1
                continue
            SOURCE_SUMMARY[src_name]["total"] += 1
            items.append(_finalize_item_schema(item))
            processed_links.append(url)
            _record_url_state(src_name, url, "accepted")
        except Exception as e:
            logging.warning("  skip %s: %s", url, e)
            SOURCE_SUMMARY[src_name]["last_error"] = str(e)
            if src.get("structured_adapter") == "erz":
                SOURCE_SUMMARY[src_name]["structured_article_extraction_failures"] += 1
            _record_url_state(src_name, url, "retryable_failure", str(e))

    attempted_links = bool(new_entries)

    if (
        src.get("html_fallback_on_empty_api")
        and attempted_links
        and SOURCE_SUMMARY[src_name]["total"] == summary_total_before
    ):
        logging.info("API empty/too short for %s — falling back to HTML index", src_name)
        return harvest_source(src, force=ARGS.rebuild if ARGS else False)

    keep = 800
    tail = [u for u in already_seen_list if u in entry_urls]
    seen_map[src["name"]] = (processed_links + tail)[:keep]

    return items


def extract_index_links(
    index_html: str, src: dict, *, index_url: str | None = None
) -> tuple[list[str], int, int]:
    """Normalize and filter article links from one index candidate.

    The returned counts describe candidates before and after source policy is
    applied. Accepted links retain document order and duplicates; callers may
    deduplicate them after recording diagnostics.
    """
    if not isinstance(index_html, str) or not index_html.strip():
        raise CrawlerParserError("extract_index_links", index_url or src["start_url"], index_html)
    soup = _parse_index_soup(index_html)
    include_patterns = src.get("include_patterns")
    if include_patterns:
        if isinstance(include_patterns, (str, bytes)):
            include_patterns = [include_patterns]
        else:
            include_patterns = [pattern for pattern in include_patterns if pattern]
    else:
        include_patterns = []

    def compile_patterns(setting: str) -> list[re.Pattern]:
        configured = src.get(setting)
        if not configured:
            return []
        patterns = [configured] if isinstance(configured, (str, bytes)) else configured
        compiled = []
        for pattern in patterns:
            if not pattern:
                continue
            try:
                compiled.append(re.compile(pattern))
            except re.error as exc:
                logging.warning("Invalid %s %r for %s: %s", setting, pattern, src.get("name"), exc)
        return compiled

    include_res = compile_patterns("include_regex")
    exclude_res = compile_patterns("exclude_regex")
    # Keep discovery metadata attached to its URL.  In particular, sitemap
    # category data must not be discarded and then reconstructed from article
    # text after an expensive fetch.
    raw_candidates: list[tuple[str, str, bool, str, str]] = []
    for anchor in soup.find_all("a"):
        href = anchor.get("href")
        if href:
            raw_candidates.append((href, anchor.get_text(strip=True) or "", True, "", ""))
    for tag_name in ("loc", "link"):
        for tag in soup.find_all(tag_name):
            value = tag.get_text(strip=True)
            if value.startswith("http"):
                record = tag.find_parent("url") or tag.parent
                metadata = " ".join(
                    node.get_text(" ", strip=True)
                    for node in record.find_all(True)
                    if node.name.split(":")[-1].lower()
                    in {"category", "keywords", "subject", "section", "rubric"}
                )
                lastmod = record.find(
                    lambda node: node.name
                    and node.name.split(":")[-1].lower() == "lastmod"
                )
                raw_candidates.append((
                    value, value, False, metadata,
                    lastmod.get_text(strip=True) if lastmod else "",
                ))
    if src.get("sort_sitemap_by_lastmod"):
        # ISO sitemap timestamps sort chronologically. Undated records remain
        # usable, but cannot displace explicitly recent records from the cap.
        raw_candidates.sort(key=lambda candidate: candidate[4], reverse=True)
    discovery_categories = src.get("discovery_categories") or []
    if isinstance(discovery_categories, str):
        discovery_categories = [discovery_categories]
    category_terms = [str(term).casefold() for term in discovery_categories if term]
    metadata_available = bool(category_terms and any(candidate[3] for candidate in raw_candidates))
    if src.get("parse_embedded_links") and not metadata_available:
        include_snippets = [str(pattern) for pattern in include_patterns]
        for expanded_html in _embedded_text_variants(index_html):
            raw_candidates.extend(
                (match, "", False, "", "")
                for match in re.findall(r'https?://[^\s"\'<>]+', expanded_html)
            )
            for relative in re.findall(r'"(/[^"<>\s]{6,260})"', expanded_html):
                if not include_snippets or any(part in relative for part in include_snippets):
                    raw_candidates.append((relative, "", False, "", ""))

    base_url = src["base_url"]
    base_host = urlparse(base_url).netloc.removeprefix("www.")
    current_index = index_url or src["start_url"]
    accepted = []
    for raw_href, link_text, is_anchor, metadata, _lastmod in raw_candidates:
        href = urljoin(base_url, raw_href)
        if href.rstrip("/") == current_index.rstrip("/") or is_listing_url(href, start_url=current_index):
            continue
        if src.get("restrict_domain"):
            host = urlparse(href).netloc.removeprefix("www.")
            if host != base_host:
                continue
        if include_patterns and not any(pattern in href for pattern in include_patterns):
            continue
        if include_res and not any(pattern.search(href) for pattern in include_res):
            continue
        if exclude_res and any(pattern.search(href) for pattern in exclude_res):
            continue
        if metadata_available and not any(term in metadata.casefold() for term in category_terms):
            continue
        if is_anchor:
            min_len = int(src.get("link_min_text_len", 0))
            if len(link_text) < min_len and not src.get("accept_empty_anchor"):
                continue
        accepted.append(href)
    return accepted, len(raw_candidates), len(accepted)


def parse_rss_atom_index(index_xml: str, src: dict, *, index_url: str | None = None) -> list[dict]:
    """Parse RSS/Atom records without treating XML elements as HTML links."""
    if not isinstance(index_xml, str) or not index_xml.strip():
        raise CrawlerParserError("parse_rss_atom_index", index_url or src["start_url"], index_xml)
    try:
        root = ET.fromstring(index_xml)
    except ET.ParseError as exc:
        raise CrawlerParserError("parse_rss_atom_index.xml", index_url or src["start_url"], index_xml) from exc

    def local_name(tag: str) -> str:
        return tag.rsplit("}", 1)[-1].split(":")[-1].lower()

    records = [node for node in root.iter() if local_name(node.tag) in {"item", "entry"}]
    entries: list[dict] = []

    def child(record, *names: str):
        """Return the first child using caller-supplied semantic priority."""
        for name in names:
            node = next((item for item in record if local_name(item.tag) == name), None)
            if node is not None:
                return node
        return None

    def value(node) -> str:
        return "" if node is None else "".join(node.itertext()).strip()

    for record in records:
        link_tags = [node for node in record if local_name(node.tag) == "link"]
        # Atom may serialize its feed/API self-link before the article link.
        # Prefer an explicit alternate link, then a link with no relation (the
        # RSS/common Atom article convention), and only then another link.
        link_tag = next(
            (node for node in link_tags if node.get("rel", "").lower() == "alternate"),
            None,
        )
        if link_tag is None:
            link_tag = next((node for node in link_tags if not node.get("rel")), None)
        if link_tag is None:
            link_tag = next(iter(link_tags), None)
        raw_url = ""
        if link_tag is not None:
            raw_url = (link_tag.get("href") or value(link_tag)).strip()
        url = urljoin(src["base_url"], raw_url)
        if src.get("normalize_numeric_article_urls"):
            parsed_url = urlparse(url)
            numeric_match = re.fullmatch(r"(/ru/news/\d+)(?:/|\.html)?", parsed_url.path)
            source_host = urlparse(src["base_url"]).netloc.lower().removeprefix("www.")
            link_host = parsed_url.netloc.lower().removeprefix("www.")
            if numeric_match and link_host == source_host:
                # Feed links have historically alternated between the bare and
                # www host and between three numeric-article suffix forms.
                # Emit the one stable identity used by state and retained items.
                base = urlparse(src["base_url"])
                url = parsed_url._replace(
                    scheme=base.scheme or parsed_url.scheme,
                    netloc=base.netloc,
                    path=numeric_match.group(1),
                    params="",
                    query="",
                    fragment="",
                ).geturl()
        # Feed metadata is only trusted for a URL that passes the exact same
        # source-domain and article-path policy as a fetched canonical URL.
        if _article_url_rejection_reason(url, src):
            continue
        title_tag = child(record, "title")
        # Publication time must win over a later modification timestamp even
        # when Atom happens to serialize <updated> first.
        date_tag = child(record, "pubdate", "published", "date", "updated")
        content_tag = child(record, "encoded", "content")
        description_tag = child(record, "description", "summary")
        content_html = value(content_tag)
        description_html = value(description_tag)
        entries.append({
            "url": url,
            "title": value(title_tag),
            "published": value(date_tag),
            "content": content_html or description_html,
            "content_field": (
                local_name(content_tag.tag) if content_tag is not None else
                (local_name(description_tag.tag) if description_tag is not None else "")
            ),
        })
    return entries


def _is_feed_index(src: dict, index_url: str) -> bool:
    return (
        src.get("index_format") == "rss_atom"
        and urlparse(index_url).path.lower().endswith((".rss", ".xml", ".atom"))
    )


def _extract_index_candidate(index_text: str, src: dict, index_url: str):
    """Parse a live or cached index with the parser configured for its format."""
    if not _is_feed_index(src, index_url):
        links, raw_count, accepted_count = extract_index_links(
            index_text, src, index_url=index_url
        )
        return links, raw_count, accepted_count, {}

    parsed_entries = parse_rss_atom_index(index_text, src, index_url=index_url)
    if not parsed_entries:
        raise CrawlerParserError("parse_rss_atom_index.empty", index_url, index_text)
    entries_by_url: dict[str, dict] = {}
    for entry in parsed_entries:
        entries_by_url.setdefault(entry["url"], entry)
    return (
        list(entries_by_url),
        len(parsed_entries),
        len(entries_by_url),
        entries_by_url,
    )


def _feed_item_fallback(entry: dict, url: str, src: dict, source_name: str) -> dict | None:
    """Build an item from first-party feed content after an article fetch failure."""
    if not src.get("feed_content_fallback") or _article_url_rejection_reason(url, src):
        return None
    required_metadata = src.get("feed_required_metadata") or []
    if isinstance(required_metadata, str):
        required_metadata = [required_metadata]
    if any(not str(entry.get(field) or "").strip() for field in required_metadata):
        return None
    # Reject an unparseable publication value when the source requires one;
    # build_item would otherwise quietly substitute crawl time.
    if "published" in required_metadata:
        try:
            dparser.parse(str(entry["published"]))
        except (ValueError, TypeError, OverflowError):
            return None
    content = entry.get("content") or ""
    plain = clean_content_text(BeautifulSoup(content, "html.parser").get_text(" ", strip=True))
    if _word_count(plain) < _effective_source_min_words(src, url):
        return None
    title = html.escape(entry.get("title") or url)
    published = html.escape(entry.get("published") or "")
    synthetic = (
        f"<html><head><title>{title}</title>"
        f'<meta property="article:published_time" content="{published}">'
        f'<link rel="canonical" href="{html.escape(url)}"></head><body></body></html>'
    )
    item = build_item(url, source_name, synthetic, src=src, pre_extracted_content=plain)
    item["_content_source"] = "official_feed"
    return item


def harvest_source(src: dict, force: bool = False):
    stats = SESSION_STATE.setdefault("stats", {})
    cooldowns = stats.setdefault("cooldowns", {})
    errors = stats.setdefault("errors", [])

    src_name = src.get("name", "")
    start_url = src["start_url"]
    fallback_start_urls = src.get("fallback_start_urls") or []
    if isinstance(fallback_start_urls, (str, bytes)):
        fallback_start_urls = [fallback_start_urls]
    start_candidates = [start_url] + [u for u in fallback_start_urls if u and u != start_url]
    min_words = SOURCE_MIN_WORDS.get(src_name, DEFAULT_MIN_WORDS)
    extraction_fingerprint = _extraction_fingerprint(src, min_words)
    SOURCE_SUMMARY[src_name]["min_words"] = min_words
    cache_path = PAGES_DIR / cache_key_for(start_url)
    cooldown_until = cooldowns.get(start_url)
    now = time.time()
    use_only_cache = False
    index_html = None
    links = None
    feed_entries: dict[str, dict] = {}
    if cooldown_until and cooldown_until > now:
        until_dt = datetime.fromtimestamp(cooldown_until, timezone.utc)
        if cache_path.exists():
            logging.warning(
                "Skip due to active cooldown until %s (using cached index): %s — %s",
                until_dt.isoformat(),
                src.get("name"),
                start_url,
            )
            errors.append(
                {
                    "source": src.get("name"),
                    "url": start_url,
                    "error": f"cooldown active until {until_dt.isoformat()} -> used cache",
                }
            )
            index_html = cache_path.read_text(encoding="utf-8")
            use_only_cache = True
            SOURCE_SUMMARY[src_name]["index_fetch_status"] = "cached"
            SOURCE_SUMMARY[src_name]["cached_fallback_used"] = True
        else:
            logging.warning(
                "Skip due to active cooldown until %s: %s — %s",
                until_dt.isoformat(),
                src.get("name"),
                start_url,
            )
            errors.append(
                {
                    "source": src.get("name"),
                    "url": start_url,
                    "error": f"cooldown active until {until_dt.isoformat()} (no cache)",
                }
            )
            return []

    logging.info("Harvest: %s — %s", src["name"], start_url)
    if index_html is None:
        last_exc = None
        total_raw_candidates = 0
        total_accepted_links = 0
        for candidate_idx, candidate_url in enumerate(start_candidates, start=1):
            logging.info(
                "Index candidate %d/%d for %s: %s",
                candidate_idx,
                len(start_candidates),
                src.get("name"),
                candidate_url,
            )
            try:
                candidate_cache_path = PAGES_DIR / cache_key_for(candidate_url)
                # fetch_page writes successful responses to this path. Snapshot a
                # known-good index first so an HTTP-200 challenge page cannot
                # destroy the only usable cached copy before validation.
                prior_candidate_html = (
                    candidate_cache_path.read_text(encoding="utf-8")
                    if candidate_cache_path.exists()
                    else None
                )
                candidate_html = fetch_page(candidate_url, src=src)
                fresh_parse_error = None
                candidate_used_cache = False
                try:
                    (
                        candidate_links,
                        candidate_raw_links,
                        candidate_accepted_links,
                        candidate_feed_entries,
                    ) = _extract_index_candidate(candidate_html, src, candidate_url)
                except CrawlerParserError as exc:
                    fresh_parse_error = exc
                    if prior_candidate_html is None:
                        raise
                    # fetch_page caches a successful response before validating
                    # it. Restore and parse the snapshot with the feed parser so
                    # malformed/empty fresh XML cannot destroy a known-good feed.
                    (
                        candidate_links,
                        candidate_raw_links,
                        candidate_accepted_links,
                        candidate_feed_entries,
                    ) = _extract_index_candidate(prior_candidate_html, src, candidate_url)
                    candidate_html = prior_candidate_html
                    candidate_used_cache = True
                    candidate_cache_path.write_text(prior_candidate_html, encoding="utf-8")
                    logging.warning(
                        "Fresh index parsing failed for %s: %s -> using cached index",
                        candidate_url,
                        exc,
                    )
                feed_entries = candidate_feed_entries
                SOURCE_SUMMARY[src_name]["index_fetch_status"] = "fetched"
                if candidate_accepted_links == 0 and prior_candidate_html is not None:
                    (
                        cached_links,
                        cached_raw_links,
                        cached_accepted_links,
                        cached_feed_entries,
                    ) = _extract_index_candidate(prior_candidate_html, src, candidate_url)
                    if cached_accepted_links > 0:
                        logging.warning(
                            "Index candidate has no accepted links for %s: %s -> using cached index with %d accepted links",
                            src.get("name"),
                            candidate_url,
                            cached_accepted_links,
                        )
                        candidate_html = prior_candidate_html
                        candidate_links = cached_links
                        candidate_raw_links = cached_raw_links
                        candidate_accepted_links = cached_accepted_links
                        feed_entries = cached_feed_entries
                        candidate_used_cache = True
                        candidate_cache_path.write_text(prior_candidate_html, encoding="utf-8")
                total_raw_candidates += candidate_raw_links
                total_accepted_links += candidate_accepted_links
                candidate_attempt = {
                    "url": candidate_url,
                    "raw_link_candidates": candidate_raw_links,
                    "accepted_links": candidate_accepted_links,
                }
                if candidate_used_cache:
                    candidate_attempt["cached"] = True
                    SOURCE_SUMMARY[src_name]["cached_fallback_used"] = True
                if fresh_parse_error is not None:
                    candidate_attempt["error"] = str(fresh_parse_error)
                    candidate_attempt["failure_kind"] = (
                        "feed_empty_parse"
                        if fresh_parse_error.stage == "parse_rss_atom_index.empty"
                        else "parser_error"
                    )
                SOURCE_SUMMARY[src_name]["index_attempts"].append(candidate_attempt)
                if candidate_accepted_links == 0:
                    logging.warning(
                        "Index candidate produced 0 accepted article links for %s: %s",
                        src.get("name"),
                        candidate_url,
                    )
                    # Retain the last valid index so an all-filtered crawl is
                    # diagnosed as discovery failure rather than hashed as None.
                    index_html = candidate_html
                    links = candidate_links
                    continue
                index_html = candidate_html
                links = candidate_links
                SOURCE_SUMMARY[src_name]["raw_link_candidates"] = total_raw_candidates
                SOURCE_SUMMARY[src_name]["accepted_links"] = total_accepted_links
                SOURCE_SUMMARY[src_name]["index_fetch_status"] = "fetched"
                if candidate_url != src["start_url"]:
                    logging.info("Index fetched via fallback URL for %s: %s", src.get("name"), candidate_url)
                start_url = candidate_url
                cache_path = PAGES_DIR / cache_key_for(start_url)
                break
            except (requests.RequestException, SourceTemporarilyUnavailable) as exc:
                last_exc = exc
                SOURCE_SUMMARY[src_name]["index_attempts"].append({
                    "url": candidate_url,
                    "raw_link_candidates": 0,
                    "accepted_links": 0,
                    "error": str(exc),
                    "failure_kind": "feed_fetch_failure" if _is_feed_index(src, candidate_url) else "fetch_failure",
                })
                logging.warning("Index candidate failed for %s: %s (%s)", src.get("name"), candidate_url, exc)
            except Exception as exc:
                # Parser/library failures are crawler defects, not upstream
                # transport failures. Preserve whatever earlier attempts saw.
                last_exc = exc
                SOURCE_SUMMARY[src_name]["index_attempts"].append({
                    "url": candidate_url,
                    "raw_link_candidates": 0,
                    "accepted_links": 0,
                    "error": str(exc),
                    "failure_kind": "feed_empty_parse" if isinstance(exc, CrawlerParserError) and "empty" in str(exc) else "parser_error",
                })
                SOURCE_SUMMARY[src_name]["index_fetch_status"] = "parser_error"
                SOURCE_SUMMARY[src_name]["last_error"] = str(exc)
                logging.exception("Index crawler/parser failed for %s: %s", src.get("name"), candidate_url)

        SOURCE_SUMMARY[src_name]["raw_link_candidates"] = total_raw_candidates
        SOURCE_SUMMARY[src_name]["accepted_links"] = total_accepted_links

        all_candidates_failed = SOURCE_SUMMARY[src_name]["index_attempts"] and all(
            "error" in attempt for attempt in SOURCE_SUMMARY[src_name]["index_attempts"]
        )
        if (
            index_html is None
            and last_exc is not None
            and all_candidates_failed
            and SOURCE_SUMMARY[src_name]["index_fetch_status"] != "parser_error"
        ):
            SOURCE_SUMMARY[src_name]["index_fetch_status"] = "failed"
            SOURCE_SUMMARY[src_name]["last_error"] = str(last_exc)
            try:
                raise last_exc
            except requests.HTTPError as exc:
                resp = exc.response
                status = resp.status_code if resp is not None else None
                if status in {500, 502, 503, 504}:
                    cooldowns[start_url] = time.time() + 6 * 3600
                    if cache_path.exists():
                        logging.warning(
                            "Server error %s, using cached index + cooldown 6h: %s — %s",
                            status,
                            src.get("name"),
                            start_url,
                        )
                        errors.append(
                            {
                                "source": src.get("name"),
                                "url": start_url,
                                "error": f"HTTP {status} -> used cache + cooldown 6h",
                            }
                        )
                        index_html = cache_path.read_text(encoding="utf-8")
                        use_only_cache = True
                        SOURCE_SUMMARY[src_name]["index_fetch_status"] = "cached"
                        SOURCE_SUMMARY[src_name]["cached_fallback_used"] = True
                    else:
                        logging.warning(
                            "Server error %s, cooldown 6h: %s — %s",
                            status,
                            src.get("name"),
                            start_url,
                        )
                        errors.append(
                            {
                                "source": src.get("name"),
                                "url": start_url,
                                "error": f"HTTP {status} -> cooldown 6h",
                            }
                        )
                        return []
                else:
                    raise
            except requests.exceptions.RetryError as exc:
                cooldowns[start_url] = time.time() + 6 * 3600
                if cache_path.exists():
                    logging.warning(
                        "Server error (retry exhausted), using cached index + cooldown 6h: %s — %s",
                        src.get("name"),
                        start_url,
                    )
                    errors.append(
                        {
                            "source": src.get("name"),
                            "url": start_url,
                            "error": f"retry exhausted -> used cache + cooldown 6h: {exc}",
                        }
                    )
                    index_html = cache_path.read_text(encoding="utf-8")
                    use_only_cache = True
                    SOURCE_SUMMARY[src_name]["index_fetch_status"] = "cached"
                    SOURCE_SUMMARY[src_name]["cached_fallback_used"] = True
                else:
                    logging.warning(
                        "Server error (retry exhausted), cooldown 6h: %s — %s",
                        src.get("name"),
                        start_url,
                    )
                    errors.append(
                        {
                            "source": src.get("name"),
                            "url": start_url,
                            "error": f"retry exhausted -> cooldown 6h: {exc}",
                        }
                    )
                    return []

            except SourceTemporarilyUnavailable as exc:
                failures = SESSION_STATE.setdefault("stats", {}).setdefault("errors", [])
                logging.warning(
                    "Temporary unavailability for %s: %s", src.get("name"), exc
                )
                if cache_path.exists():
                    logging.warning(
                        "Using cached index due to host issue: %s — %s",
                        src.get("name"),
                        start_url,
                    )
                    failures.append(
                        {
                            "source": src.get("name"),
                            "url": start_url,
                            "error": f"temporary unavailable -> used cache: {exc}",
                            "status": "cached",
                        }
                    )
                    index_html = cache_path.read_text(encoding="utf-8")
                    use_only_cache = True
                    SOURCE_SUMMARY[src_name]["index_fetch_status"] = "cached"
                    SOURCE_SUMMARY[src_name]["cached_fallback_used"] = True
                else:
                    failures.append(
                        {
                            "source": src.get("name"),
                            "url": start_url,
                            "error": f"temporary unavailable: {exc}",
                            "status": "skipped",
                        }
                    )
                    return []

        if index_html is None and SOURCE_SUMMARY[src_name]["index_fetch_status"] == "parser_error":
            return []

    # An unchanged index must still be parsed so retryable article URLs are revisited.
    if not isinstance(index_html, str) or not index_html.strip():
        exc = CrawlerParserError("harvest_source.index_hash", start_url, index_html)
        SOURCE_SUMMARY[src_name]["index_fetch_status"] = "parser_error"
        SOURCE_SUMMARY[src_name]["last_error"] = str(exc)
        return []
    idx_digest = hashlib.sha256(index_html.encode("utf-8")).hexdigest()
    ih = STATE.setdefault("index_hash", {})
    if not force and ih.get(src["start_url"]) == idx_digest:
        logging.info("Index unchanged: %s — %s", src["name"], src["start_url"])
        SOURCE_SUMMARY[src_name]["index_fetch_status"] = "unchanged"
    ih[src["start_url"]] = idx_digest

    # Cached/cooldown indexes have not gone through candidate selection above.
    if links is None:
        links, raw_count, accepted_count, cached_feed_entries = _extract_index_candidate(
            index_html, src, start_url
        )
        feed_entries = cached_feed_entries
        SOURCE_SUMMARY[src_name]["raw_link_candidates"] = raw_count
        SOURCE_SUMMARY[src_name]["accepted_links"] = accepted_count
        SOURCE_SUMMARY[src_name]["index_attempts"].append({
            "url": start_url,
            "raw_link_candidates": raw_count,
            "accepted_links": accepted_count,
            "cached": use_only_cache,
        })
    logging.info(
        "Link extraction stats for %s: raw=%d accepted=%d",
        src_name,
        SOURCE_SUMMARY[src_name]["raw_link_candidates"],
        SOURCE_SUMMARY[src_name]["accepted_links"],
    )

    # Dedup and limit
    uniq = []
    seen = set()
    for u in links:
        if u in seen:
            continue
        seen.add(u)
        uniq.append(u)

    # лимит по источнику (берём из sources.json или общий DEFAULT)
    uniq = uniq[: int(src.get("max_links", MAX_LINKS_PER_SOURCE)) ]
    STATE.setdefault("candidate_urls", {})[src_name] = uniq

    # Обрабатываем только новые относительно последнего прогона
    seen_map = STATE.setdefault("seen_urls", {})
    already_seen_list = list(seen_map.get(src["name"], []))
    already_seen = set(already_seen_list)
    url_states = _source_url_states(src_name)

    if force:
        new_links = uniq  # при rebuild обрабатываем все доступные uniq-ссылки
    else:
        new_links = [
            u for u in uniq
            if _should_attempt_url(url_states.get(u), extraction_fingerprint)
        ]
        if not new_links:
            logging.info("  no new links for %s", src["name"])
            return []
    SOURCE_SUMMARY[src_name]["attempted_articles"] += len(new_links)

    items = []
    processed_links = []

    def handle_article_fetch_failure(url: str, exc: Exception) -> None:
        """Expose transport loss even when an adequate feed record saves the item."""
        SOURCE_SUMMARY[src_name]["article_fetch_degradations"] += 1
        message = f"article fetch unavailable: {exc}"
        SOURCE_SUMMARY[src_name]["last_error"] = message
        errors.append({
            "source": src_name,
            "url": url,
            "error": message,
            "failure_kind": "article_fetch_degradation",
        })

    def handle_item(item: dict | None, url: str) -> None:
        if not item:
            return
        content_text = item.get("content_text") or ""
        word_count = _word_count(content_text)
        content_source_label = item.pop("_content_source", None)
        drop_source = content_source_label or "html"
        if ARGS and getattr(ARGS, "debug", False):
            logging.debug(
                "Content source for %s: %s (%d words)",
                url,
                drop_source,
                word_count,
            )
        amp_flag = item.pop("_amp_used", False)
        if not content_text.strip():
            logging.debug("drop empty: %s source=%s", url, drop_source)
            SOURCE_SUMMARY[src_name]["empty"] += 1
            if amp_flag:
                SOURCE_SUMMARY[src_name]["amp"] += 1
            _record_extraction_failure(
                src_name, url, kind="empty", error="empty extraction",
                fingerprint=extraction_fingerprint,
            )
            return
        if amp_flag:
            SOURCE_SUMMARY[src_name]["amp"] += 1
        if min_words and word_count < min_words:
            logging.debug(
                "drop short: %s words=%d min=%d source=%s",
                url,
                word_count,
                min_words,
                drop_source,
            )
            SOURCE_SUMMARY[src_name]["short"] += 1
            _record_extraction_failure(
                src_name, url, kind="short",
                error=f"short extraction: {word_count} words",
                fingerprint=extraction_fingerprint,
            )
            return
        SOURCE_SUMMARY[src_name]["total"] += 1
        items.append(_finalize_item_schema(item))
        processed_links.append(url)
        _record_url_state(src_name, url, "accepted")

    for idx, url in enumerate(new_links):
        if runtime_expired():
            logging.info(
                "  stop fetching more items for %s due to max-runtime",
                src.get("name"),
            )
            break
        if (
            ARGS
            and getattr(ARGS, "smoke", False)
            and ARGS.limit_per_source is not None
            and idx >= ARGS.limit_per_source
        ):
            if getattr(ARGS, "debug", False):
                logging.debug(
                    "Skip deep fetch for %s (limit-per-source)",
                    url,
                )
            break
        try:
            if use_only_cache:
                page_path = PAGES_DIR / cache_key_for(url)
                if page_path.exists():
                    html = page_path.read_text(encoding="utf-8")
                else:
                    raise FileNotFoundError("cached copy missing during cooldown")
            else:
                html = _with_article_retries(src, url, lambda: fetch_page(url, src=src))
            item = _build_item_with_retries(
                url,
                src,
                src_name,
                html,
                content_selectors=src.get("content_selectors"),
                src=src,
            )
            handle_item(item, url)
        except SourceTemporarilyUnavailable as exc:
            handle_article_fetch_failure(url, exc)
            page_path = PAGES_DIR / cache_key_for(url)
            if page_path.exists():
                SOURCE_SUMMARY[src_name]["cached_fallback_used"] = True
                logging.warning(
                    "  using cached copy for %s due to temporary issue: %s", url, exc
                )
                html = page_path.read_text(encoding="utf-8")
                item = _build_item_with_retries(
                    url,
                    src,
                    src_name,
                    html,
                    content_selectors=src.get("content_selectors"),
                    src=src,
                )
                handle_item(item, url)
            else:
                fallback_item = _feed_item_fallback(feed_entries.get(url, {}), url, src, src_name)
                if fallback_item:
                    SOURCE_SUMMARY[src_name]["feed_content_fallbacks"] += 1
                    handle_item(fallback_item, url)
                else:
                    logging.warning("  skip %s: %s", url, exc)
                    _record_url_state(src_name, url, "retryable_failure", str(exc))
        except Exception as e:
            handle_article_fetch_failure(url, e)
            fallback_item = _feed_item_fallback(feed_entries.get(url, {}), url, src, src_name)
            if fallback_item:
                SOURCE_SUMMARY[src_name]["feed_content_fallbacks"] += 1
                handle_item(fallback_item, url)
            else:
                logging.warning("  skip %s: %s", url, e)
                _record_url_state(src_name, url, "retryable_failure", str(e))

    # обновим «виденные» ссылки — держим скользящее окно последних 800
    keep = 800
    # сначала — новые (в порядке обхода), затем часть старых, которые ещё встречаются в uniq
    tail = [u for u in already_seen_list if u in uniq]
    # при rebuild тоже обновляем, чтобы после форс-прогона обычные запуски работали эффективно
    seen_map[src["name"]] = (processed_links + tail)[:keep]

    return items


def log_source_summary() -> None:
    if not SOURCE_SUMMARY:
        logging.info("Summary: no sources processed")
        return
    for name in sorted(SOURCE_SUMMARY):
        summary = SOURCE_SUMMARY[name]
        logging.info(
            "%s | total=%d empty=%d short=%d listing=%d api=%d amp=%d min_words=%d "
            "structured_fetch_failures=%d structured_schema_mismatches=%d "
            "structured_zero_records=%d structured_article_failures=%d",
            name,
            summary.get("total", 0),
            summary.get("empty", 0),
            summary.get("short", 0),
            summary.get("listing", 0),
            summary.get("api", 0),
            summary.get("amp", 0),
            int(summary.get("min_words", DEFAULT_MIN_WORDS) or 0),
            summary.get("structured_endpoint_fetch_failures", 0),
            summary.get("structured_schema_mismatches", 0),
            summary.get("structured_zero_records", 0),
            summary.get("structured_article_extraction_failures", 0),
        )


def write_source_health_report(sources: list[dict]) -> None:
    """Persist crawl diagnostics independently from the retained feed."""
    rows = []
    # Keep the three stages independent: a 200 response does not mean that the
    # index yielded links, and discovered links do not mean article extraction
    # succeeded.
    legacy_streaks = SOURCE_HEALTH_STATE
    fetch_streaks = STATE.setdefault("source_fetch_failure_streaks", {})
    parser_streaks = STATE.setdefault("source_parser_failure_streaks", {})
    discovery_streaks = STATE.setdefault("source_discovery_failure_streaks", {})
    article_streaks = STATE.setdefault("source_article_failure_streaks", {})
    discovery_state = STATE.setdefault("source_discovery_state", {})
    report_time = datetime.now(timezone.utc).isoformat()
    for source in sources:
        if not source.get("enabled", True):
            continue
        name = source.get("name", "")
        summary = SOURCE_SUMMARY[name]
        status = summary.get("index_fetch_status", "not_attempted")
        attempted = int(summary.get("attempted_articles", 0) or 0)
        accepted = int(summary.get("total", 0) or 0)
        raw_candidates = int(summary.get("raw_link_candidates", 0) or 0)
        accepted_links = int(summary.get("accepted_links", 0) or 0)
        article_fetch_degradations = int(summary.get("article_fetch_degradations", 0) or 0)
        source_discovery = discovery_state.setdefault(name, {})
        fetch_failed = status in {"failed", "not_attempted"}
        parser_failed = status == "parser_error"
        discovery_failed: bool | None = None
        if status == "fetched":
            discovery_failed = raw_candidates == 0 or accepted_links == 0
            source_discovery.update({
                "raw_link_candidates": raw_candidates,
                "accepted_links": accepted_links,
            })
            if not discovery_failed:
                source_discovery["last_successful_discovery_at"] = report_time
        elif status == "unchanged":
            # No parser runs for an unchanged index, so carry forward the last
            # actual discovery result rather than treating HTTP 304/hash reuse
            # as healthy by itself.
            has_prior_discovery = "raw_link_candidates" in source_discovery
            raw_candidates = int(source_discovery.get("raw_link_candidates", raw_candidates) or 0)
            accepted_links = int(source_discovery.get("accepted_links", accepted_links) or 0)
            discovery_failed = (
                raw_candidates == 0 or accepted_links == 0
                if has_prior_discovery
                else None
            )
        article_failed = (accepted == 0 or article_fetch_degradations > 0) if attempted > 0 else None
        if status == "parser_error":
            failure_class = "crawler_parser_error"
        elif fetch_failed:
            failure_class = "source_fetch_failure"
        elif discovery_failed is True and raw_candidates > 0 and accepted_links == 0:
            failure_class = "discovery_filter_failure"
        elif article_fetch_degradations > 0:
            failure_class = "article_fetch_degradation"
        else:
            failure_class = None

        def update_streak(streak_map: dict, failed: bool | None) -> int:
            previous = int(streak_map.get(name, 0) or 0)
            if failed is not None:
                streak_map[name] = previous + 1 if failed else 0
            else:
                streak_map.setdefault(name, previous)
            return streak_map[name]

        if status != "skipped_selection":
            fetch_streak = update_streak(fetch_streaks, fetch_failed)
            parser_streak = update_streak(parser_streaks, parser_failed)
            discovery_streak = update_streak(discovery_streaks, discovery_failed)
            article_streak = update_streak(article_streaks, article_failed)
            previous_legacy = int(legacy_streaks.get(name, 0) or 0)
            any_failure = parser_failed or fetch_failed or discovery_failed is True or article_failed is True
            legacy_streaks[name] = previous_legacy + 1 if any_failure else 0
        else:
            fetch_streak = int(fetch_streaks.get(name, 0) or 0)
            parser_streak = int(parser_streaks.get(name, 0) or 0)
            discovery_streak = int(discovery_streaks.get(name, 0) or 0)
            article_streak = int(article_streaks.get(name, 0) or 0)
            legacy_streaks.setdefault(name, int(legacy_streaks.get(name, 0) or 0))
        rows.append({
            "source": name,
            "index_fetch_status": status,
            "failure_class": failure_class,
            "consecutive_failures": legacy_streaks[name],
            "consecutive_fetch_failures": fetch_streak,
            "consecutive_parser_failures": parser_streak,
            "consecutive_discovery_failures": discovery_streak,
            "consecutive_article_failures": article_streak,
            "raw_link_candidates": raw_candidates,
            "accepted_links": accepted_links,
            "index_attempts": summary.get("index_attempts", []),
            "last_successful_discovery_at": source_discovery.get("last_successful_discovery_at"),
            "attempted_articles": attempted,
            "accepted_articles": accepted,
            "article_fetch_degradations": article_fetch_degradations,
            "feed_content_fallbacks": summary.get("feed_content_fallbacks", 0),
            "empty_rejections": summary.get("empty", 0),
            "short_rejections": summary.get("short", 0),
            "future_date_rejections": summary.get("future_date_rejections", 0),
            "publication_rejections_by_signal": summary.get(
                "publication_rejections_by_signal", {}
            ),
            "publication_rejection_samples": summary.get(
                "publication_rejection_samples", []
            ),
            "canonical_rejections_by_reason": summary.get(
                "canonical_rejections_by_reason", {}
            ),
            "canonical_rejection_samples": summary.get(
                "canonical_rejection_samples", []
            ),
            "maximum_future_offset_seconds": summary.get(
                "maximum_future_offset_seconds", 0
            ),
            "structured_endpoint_fetch_failures": summary.get(
                "structured_endpoint_fetch_failures", 0
            ),
            "structured_schema_mismatches": summary.get(
                "structured_schema_mismatches", 0
            ),
            "structured_zero_records": summary.get("structured_zero_records", 0),
            "structured_article_extraction_failures": summary.get(
                "structured_article_extraction_failures", 0
            ),
            "cached_fallback_used": bool(summary.get("cached_fallback_used", False)),
            "last_error": summary.get("last_error"),
        })
    payload = {"generated_at": report_time, "sources": rows}
    SOURCE_HEALTH_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    # Streaks are updated while producing the report, after the normal state
    # save in the caller, so persist them here as part of the same operation.
    prune_page_cache()
    save_state()
    save_source_health_state()


def retain_bounded_items(items: list[dict]) -> list[dict]:
    """Reserve source minima, then allocate soft weighted shares.

    A source's next item scores ``weight / (already_selected + 1)``.  Thus a
    deep dominant queue progressively yields to specialist queues, but remains
    eligible and can consume every unfilled slot once those queues run dry.
    """
    if not FEED_MAX_ITEMS or len(items) <= FEED_MAX_ITEMS:
        return items
    if FEED_MIN_ITEMS_PER_SOURCE <= 0:
        return items[:FEED_MAX_ITEMS]
    reserved: list[dict] = []
    counts: dict[str, int] = defaultdict(int)
    reserved_ids: set[str] = set()
    for item in items:
        source = str(item.get("source") or "")
        item_id = str(item.get("id") or item.get("url") or "")
        if source and counts[source] < FEED_MIN_ITEMS_PER_SOURCE:
            reserved.append(item)
            reserved_ids.add(item_id)
            counts[source] += 1
    if len(reserved) > FEED_MAX_ITEMS:
        return items[:FEED_MAX_ITEMS]
    selected = list(reserved)
    queues: dict[str, list[tuple[int, dict]]] = defaultdict(list)
    for position, item in enumerate(items):
        item_id = str(item.get("id") or item.get("url") or "")
        if item_id not in reserved_ids:
            queues[str(item.get("source") or "")].append((position, item))

    while len(selected) < FEED_MAX_ITEMS and queues:
        eligible = [source for source, queue in queues.items() if queue]
        if not eligible:
            break
        source = min(
            eligible,
            key=lambda name: (
                -(max(0.01, SOURCE_RETENTION_WEIGHTS.get(name, 1.0)) / (counts[name] + 1)),
                queues[name][0][0],
                name,
            ),
        )
        _, item = queues[source].pop(0)
        selected.append(item)
        counts[source] += 1
        if not queues[source]:
            del queues[source]
    selected.sort(
        key=sort_timestamp,
        reverse=True,
    )
    return selected


def build_feed(all_items):
    by_id: dict[str, dict] = {}
    for it in all_items:
        item_id = it.get("id")
        if not item_id:
            continue
        title = (it.get("title") or "").strip()
        url = it.get("url") or ""
        if ('SKIP_KEYWORDS' in globals() and SKIP_KEYWORDS and (SKIP_KEYWORDS.search(title) or SKIP_KEYWORDS.search(url))):
            continue
        if is_listing_url(url):
            continue
        existing = by_id.get(item_id)
        if not existing:
            by_id[item_id] = _finalize_item_schema(dict(it))
            continue
        old_fetched = existing.get("fetched_at") or ""
        new_fetched = it.get("fetched_at") or ""
        if new_fetched > old_fetched:
            by_id[item_id] = _finalize_item_schema(dict(it))

    items = _filter_by_min_words(list(by_id.values()))
    items.sort(
        key=sort_timestamp,
        reverse=True,
    )

    items = retain_bounded_items(items)

    feed = {
        "version": "https://jsonfeed.org/version/1.1",
        "title": "Unified feed",
        "home_page_url": "",
        "feed_url": "",
        "items": items,
    }
    return feed

# ---- Merge helpers (Variant B) ----
def load_existing_feed_items():
    """Загрузить текущие items из docs/unified.json, если файл существует."""
    if not EXISTING_FEED_JSON.exists():
        return []
    try:
        data = json.loads(EXISTING_FEED_JSON.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "items" in data and isinstance(data["items"], list):
            return data["items"]
        # на всякий случай поддержим старый формат (если кто-то сохранил чистый список)
        if isinstance(data, list):
            return data
    except Exception as e:
        logging.warning("Cannot load existing feed (%s). Will start from fresh items.", e)
    return []

def merge_items(existing, new):
    """Merge items preferring the freshest content per stable item id."""

    by_key: dict[str, dict] = {}
    for it in existing:
        key = it.get("id") or it.get("url")
        if not key:
            continue
        url = it.get("url") or ""
        source_config = SOURCE_CONFIGS_BY_NAME.get(str(it.get("source") or ""))
        invalid_url = source_config and _article_url_rejection_reason(url, source_config)
        canonical_url = it.get("canonical_url")
        invalid_canonical = (
            source_config
            and canonical_url
            and _article_url_rejection_reason(str(canonical_url), source_config)
        )
        if invalid_url or invalid_canonical:
            if ARGS and getattr(ARGS, "debug", False):
                logging.debug(
                    "Drop source-boundary violation from existing feed: %s (%s)",
                    url,
                    invalid_url or invalid_canonical,
                )
            continue
        if is_listing_url(url):
            if ARGS and getattr(ARGS, "debug", False):
                logging.debug("Drop listing URL from existing feed: %s", url)
            continue
        by_key[key] = _finalize_item_schema(dict(it))

    for it in new:
        # Sanitize incoming records before comparing publication times. This
        # prevents a rejected future value from replacing (and then erasing) a
        # plausible date already held by the existing record.
        incoming_publication_is_trustworthy = bool(it.get("_published_at_trustworthy"))
        it = _finalize_item_schema(dict(it))
        key = it.get("id") or it.get("url")
        if not key:
            continue
        url = it.get("url") or ""
        if is_listing_url(url):
            if ARGS and getattr(ARGS, "debug", False):
                logging.debug("Skip listing URL from new items: %s", url)
            continue
        old = by_key.get(key)
        if not old:
            by_key[key] = _finalize_item_schema(dict(it))
            continue

        merged = dict(old)
        for field in [
            "source",
            "title",
            "url",
            "content_text",
            "first_seen",
            "bucketed_at",
            "published_at",
            "fetched_at",
            "canonical_url",
            "source_record_id",
            "tags",
            "summary",
        ]:
            value = it.get(field)
            if field == "content_text":
                old_value = old.get(field) or ""
                new_value = value or ""
                if new_value and (not old_value or len(new_value) >= len(old_value)):
                    merged[field] = new_value
            elif field == "published_at":
                old_value = old.get(field) or old.get("date_published")
                if value and old_value:
                    item_identifier = it.get("id") or old.get("id")
                    if item_identifier:
                        fallback_date = STATE.get("first_seen", {}).get(item_identifier)
                        if fallback_date and fallback_date == value:
                            continue
                if value and (not old_value or str(value) > str(old_value)):
                    merged[field] = value
            elif field == "fetched_at":
                old_value = old.get(field)
                if value and (not old_value or str(value) > str(old_value)):
                    merged[field] = value
            elif field == "first_seen":
                if not merged.get(field) and value:
                    merged[field] = value
            elif field == "bucketed_at":
                if not merged.get(field) and value:
                    merged[field] = value
            elif value not in (None, ""):
                merged[field] = value

        if incoming_publication_is_trustworthy and it.get("published_at") == merged.get("published_at"):
            merged["_published_at_trustworthy"] = True
        by_key[key] = _finalize_item_schema(merged)

    merged_items = list(by_key.values())
    merged_items.sort(
        key=sort_timestamp,
        reverse=True,
    )

    merged_items = retain_bounded_items(merged_items)

    return merged_items

def main():
    global ARGS, CONNECT_TIMEOUT, READ_TIMEOUT, REQUEST_TIMEOUT, START_TIME, RUNTIME_EXCEEDED, _RUNTIME_LOGGED
    global OUT_JSON, EXISTING_FEED_JSON, STATE_FILE, SOURCE_HEALTH_JSON, SOURCE_HEALTH_STATE_FILE, SOURCE_HEALTH_STATE
    parser = argparse.ArgumentParser(description="Aggregate news feed")
    parser.add_argument("--rebuild", action="store_true", help="Force rebuild: ignore index unchanged and seen-URL filters; always rewrite unified.json")
    parser.add_argument("--dry-run", action="store_true", help="Run without writing unified.json/state")
    parser.add_argument("--smoke", "--ci-fast", dest="smoke", action="store_true", help="Fast smoke run with limited sources")
    parser.add_argument("--sources", type=str, help="Comma-separated source names to include")
    parser.add_argument("--limit-per-source", type=int, default=None, help="Limit number of deep-parsed items per source in smoke mode")
    parser.add_argument("--connect-timeout", type=float, default=5.0, help="Connection timeout in seconds")
    parser.add_argument("--read-timeout", type=float, default=10.0, help="Read timeout in seconds")
    parser.add_argument("--max-runtime", type=int, default=None, help="Maximum runtime in seconds before stopping gracefully")
    parser.add_argument("--debug", action="store_true", help="Enable verbose debug logging")
    parser.add_argument("--output", type=pathlib.Path, default=OUT_JSON, help="Candidate feed output path")
    parser.add_argument("--source-health-output", type=pathlib.Path, default=SOURCE_HEALTH_JSON, help="Candidate crawl-health output path")
    parser.add_argument("--existing-feed", type=pathlib.Path, default=EXISTING_FEED_JSON, help="Published feed used as the merge baseline")
    parser.add_argument("--state-output", type=pathlib.Path, default=STATE_FILE, help="Candidate crawler-state output path")
    parser.add_argument("--source-health-state", type=pathlib.Path, default=SOURCE_HEALTH_STATE_FILE, help="Persistent failure-streak state path")
    ARGS = parser.parse_args()

    OUT_JSON = ARGS.output
    SOURCE_HEALTH_JSON = ARGS.source_health_output
    EXISTING_FEED_JSON = ARGS.existing_feed
    STATE_FILE = ARGS.state_output
    SOURCE_HEALTH_STATE_FILE = ARGS.source_health_state
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    SOURCE_HEALTH_JSON.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if SOURCE_HEALTH_STATE_FILE.exists():
        loaded_health_state = json.loads(SOURCE_HEALTH_STATE_FILE.read_text(encoding="utf-8"))
        SOURCE_HEALTH_STATE = loaded_health_state if isinstance(loaded_health_state, dict) else {}
    else:
        SOURCE_HEALTH_STATE = {}

    SOURCE_SUMMARY.clear()
    SOURCE_MIN_WORDS.clear()

    if ARGS.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    CONNECT_TIMEOUT = max(0.1, float(ARGS.connect_timeout))
    READ_TIMEOUT = max(0.1, float(ARGS.read_timeout))
    REQUEST_TIMEOUT = (CONNECT_TIMEOUT, READ_TIMEOUT)

    START_TIME = time.monotonic()
    RUNTIME_EXCEEDED = False
    _RUNTIME_LOGGED = False

    if ARGS.smoke:
        logging.info("===== SMOKE MODE =====")
        if ARGS.limit_per_source is None:
            ARGS.limit_per_source = 3

    sources = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
    SOURCE_CONFIGS_BY_NAME.clear()
    SOURCE_CONFIGS_BY_NAME.update({source["name"]: source for source in sources})
    SOURCE_RETENTION_WEIGHTS.clear()
    for source in sources:
        configured_weight = source.get("retention_weight", 1.0)
        try:
            SOURCE_RETENTION_WEIGHTS[source.get("name", "")] = max(0.01, float(configured_weight))
        except (TypeError, ValueError):
            logging.warning("Invalid retention_weight for %s; using 1.0", source.get("name"))
            SOURCE_RETENTION_WEIGHTS[source.get("name", "")] = 1.0
    HOST_STRATEGIES.update(build_strategy_registry(sources))

    selected_sources = None
    if ARGS.sources:
        selected_sources = {name.strip() for name in ARGS.sources.split(",") if name.strip()}
    elif ARGS.smoke:
        selected_sources = set(SMOKE_DEFAULT_SOURCES)

    if selected_sources:
        logging.info("Selected sources: %s", ", ".join(sorted(selected_sources)))

    all_items = []
    seen_source_names = set()
    for src in sources:
        src_name = src.get("name", "")
        SOURCE_MIN_WORDS[src_name] = _effective_source_min_words(src)
        if runtime_expired():
            logging.info("Stop processing further sources due to max-runtime limit")
            break
        if not src.get('enabled', True):
            logging.info("Skip disabled source: %s — %s", src.get('name'), src.get('start_url'))
            continue
        if selected_sources and src.get("name") not in selected_sources:
            SOURCE_SUMMARY[src_name]["index_fetch_status"] = "skipped_selection"
            continue
        seen_source_names.add(src_name)
        try:
            if src.get("mode") == "api":
                items = harvest_json_source(src, force=ARGS.rebuild)
            else:
                items = harvest_source(src, force=ARGS.rebuild)
            logging.info("  -> %d items", len(items))
            all_items.extend(items)
        except Exception as e:
            logging.error("  !! Failed: %s (%s)", src.get("name"), e)
            SOURCE_SUMMARY[src_name]["index_fetch_status"] = "failed"
            SOURCE_SUMMARY[src_name]["last_error"] = str(e)
            SESSION_STATE.setdefault("stats", {}).setdefault("errors", []).append({"source": src.get("name"), "url": src.get("start_url"), "error": str(e)})

    if selected_sources and ARGS.sources:
        missing = selected_sources - seen_source_names
        if missing:
            logging.warning("Requested sources not found or disabled: %s", ", ".join(sorted(missing)))

    log_source_summary()

    if not all_items and not ARGS.rebuild:
        # Нет новых карточек — ленту не переписываем, чтобы не обнулять историю
        existing_count = 0
        if OUT_JSON.exists():
            try:
                existing_count = len(json.loads(OUT_JSON.read_text(encoding="utf-8")).get("items", []))
            except Exception:
                existing_count = 0
        STATE.setdefault("stats", {})["last_run"] = datetime.now(timezone.utc).isoformat()
        STATE["stats"]["items"] = existing_count
        if not ARGS.dry_run:
            existing_items = load_existing_feed_items()
            if OUT_JSON != EXISTING_FEED_JSON:
                if EXISTING_FEED_JSON.exists():
                    shutil.copyfile(EXISTING_FEED_JSON, OUT_JSON)
                else:
                    OUT_JSON.write_text(
                        json.dumps(build_feed([]), ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
            prune_state(existing_items, sources)
            save_state()
            write_source_health_report(sources)
        logging.info("No new items -> keep existing %s as-is (%d items)", OUT_JSON, existing_count)
        return

    if ARGS.rebuild and not all_items:
        # Форс-режим и ничего не накраулилось (сетевые/источники без изменений):
        # просто нормализуем и пересохраним существующую ленту (пересортировка/обрезка)
        existing_items = load_existing_feed_items()
        feed = build_feed(existing_items)
        if not ARGS.dry_run:
            OUT_JSON.write_text(json.dumps(feed, ensure_ascii=False, indent=2), encoding="utf-8")
        STATE.setdefault("stats", {})["last_run"] = datetime.now(timezone.utc).isoformat()
        STATE["stats"]["items"] = len(feed["items"])
        if not ARGS.dry_run:
            prune_state(feed["items"], sources)
            save_state()
            write_source_health_report(sources)
        logging.info("Rewrote(existing only) %s (%d items)", OUT_JSON, len(feed["items"]))
        return

    # Есть новые карточки — сливаем с существующей лентой
    existing_items = load_existing_feed_items()
    merged_raw = merge_items(existing_items, all_items)
    feed = build_feed(merged_raw)

    if not ARGS.dry_run:
        OUT_JSON.write_text(json.dumps(feed, ensure_ascii=False, indent=2), encoding="utf-8")
    STATE.setdefault("stats", {})["last_run"] = datetime.now(timezone.utc).isoformat()
    STATE["stats"]["items"] = len(feed["items"])
    if not ARGS.dry_run:
        prune_state(feed["items"], sources)
        save_state()
        write_source_health_report(sources)
    logging.info("Saved feed to %s (%d items)", OUT_JSON, len(feed["items"]))

    if RUNTIME_EXCEEDED and os.environ.get("CI"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
