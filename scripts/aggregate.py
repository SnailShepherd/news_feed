#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, json, logging, pathlib, sys, hashlib, argparse, random, html, shutil
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

try:
    from scripts.url_filters import is_listing_url
except ModuleNotFoundError:  # pragma: no cover - fallback when run as a script
    from url_filters import is_listing_url  # type: ignore

try:
    from scripts.http_client import (
        HostClient,
        RequestStrategy,
        SourceTemporarilyUnavailable,
        build_strategy_registry,
        DEFAULT_USER_AGENT,
    )
except ModuleNotFoundError:  # pragma: no cover - fallback when run as a script
    from http_client import (  # type: ignore
        HostClient,
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
        "attempted_articles": 0,
        "cached_fallback_used": False,
        "future_date_rejections": 0,
        "last_error": None,
    }
)
SOURCE_MIN_WORDS: dict[str, int] = {}
DEFAULT_MIN_WORDS = 100
HOST_MIN_WORD_OVERRIDES = {
    "realty.ria.ru": 120,
    "realty.interfax.ru": 120,
    "stroygaz.ru": 120,
    "rg.ru": 150,
    "faufcc.ru": 70,
}
PUBLICATION_CLOCK_SKEW = timedelta(
    hours=float(os.environ.get("PUBLICATION_CLOCK_SKEW_HOURS", "24"))
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
OPTIONAL_ITEM_FIELDS = ("published_at", "canonical_url")

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
        "stats": {},
        "index_hash": {},
        "seen_urls": {},
        "first_seen": {},
        "host_state": {},
        "aliases": {},
        "content_hashes": {},
        "canonical_item_ids": {},
    }
    for key, default in required_defaults.items():
        val = state.get(key)
        if not isinstance(val, dict):
            state[key] = dict(default)
    return state


if STATE_FILE.exists():
    STATE = json.loads(STATE_FILE.read_text(encoding="utf-8"))
else:
    STATE = {
        "headers": {},
        "stats": {},
        "index_hash": {},
        "seen_urls": {},
    }

STATE = ensure_state_keys(STATE)

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
_MIN_PARAGRAPH_CLUSTER = 3

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
        client = HostClient(strategy_host, strategy, STATE)
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


def validate_publication_datetime(
    dt: datetime | None,
    *,
    raw_value: object = None,
    url: str | None = None,
    source: str | None = None,
    signal: str = "unknown",
    now: datetime | None = None,
    allowance: timedelta = PUBLICATION_CLOCK_SKEW,
) -> datetime | None:
    """Return a normalized, plausible publication time, or reject it.

    ``now`` and ``allowance`` are injectable so callers and tests do not need
    to depend on wall-clock time. All extraction paths should pass through
    this single boundary before a value is stored as ``published_at``.
    """
    dt = finalize_datetime(dt)
    if dt is None:
        return None
    reference = finalize_datetime(now or datetime.now(MSK))
    if dt > reference + allowance:
        rejection_count = 1
        if source:
            rejection_count = int(
                SOURCE_SUMMARY[source].get("future_date_rejections", 0) or 0
            ) + 1
            SOURCE_SUMMARY[source]["future_date_rejections"] = rejection_count
        if rejection_count <= 3:
            logging.warning(
                "Reject future publication time value=%r url=%s source=%s signal=%s",
                raw_value if raw_value is not None else dt.isoformat(),
                url or "",
                source or "",
                signal,
            )
        elif rejection_count == 4:
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
        published_dt = validate_publication_datetime(
            published_dt,
            raw_value=published_val,
            url=str(item.get("url") or ""),
            source=str(item.get("source") or ""),
            signal="stored:published_at",
        )
        if published_dt:
            item["published_at"] = published_dt.isoformat()
        else:
            item.pop("published_at", None)


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


def extract_content_with_fallback(doc, selectors, title: str | None):
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

    candidates: list[tuple[int, str]] = []
    candidate_nodes = []

    for sel in ordered_selectors:
        try:
            nodes = soup.select(sel)
        except Exception:
            continue
        for node in nodes:
            if node in candidate_nodes:
                continue
            text = _normalize_whitespace(node.get_text("\n", strip=True))
            if not text:
                continue
            cleaned_text = _strip_deny_phrases(text)
            if not cleaned_text:
                continue
            candidates.append((len(cleaned_text), cleaned_text))
            candidate_nodes.append(node)

    article_node = soup.find("article")
    if article_node and article_node not in candidate_nodes:
        candidate_nodes.append(article_node)
    if soup.body and soup.body not in candidate_nodes:
        candidate_nodes.append(soup.body)

    best_text = ""
    if candidates:
        best_text = max(candidates, key=lambda item: item[0])[1]

    best_density_text = ""
    best_density_score = 0
    for node in candidate_nodes:
        parts = []
        for sub in node.find_all(["p", "li", "h2", "h3"]):
            fragment = _normalize_whitespace(sub.get_text(" ", strip=True))
            if fragment:
                if _contains_deny_phrase(fragment):
                    continue
                parts.append(fragment)
        if not parts:
            continue
        joined = "\n\n".join(parts)
        score = len("".join(parts))
        if score > best_density_score:
            best_density_score = score
            best_density_text = joined

    if best_density_text and len(best_density_text) > len(best_text):
        best_text = best_density_text

    readability_text = ""
    readability_score = 0
    search_nodes = list(candidate_nodes)
    for extra in soup.find_all(["article", "section", "main", "div", "body"]):
        if extra not in search_nodes:
            search_nodes.append(extra)
    for node in search_nodes:
        paragraphs = []
        for p in node.find_all("p"):
            fragment = _normalize_whitespace(p.get_text(" ", strip=True))
            if not fragment:
                continue
            if _contains_deny_phrase(fragment):
                continue
            paragraphs.append(fragment)
        if len(paragraphs) < _MIN_PARAGRAPH_CLUSTER:
            continue
        joined = "\n\n".join(paragraphs)
        score = len(joined)
        if score > readability_score:
            readability_score = score
            readability_text = joined

    if not best_text and readability_text:
        best_text = readability_text

    final_text = _drop_leading_title(best_text, title)
    final_text = _strip_deny_phrases(final_text)
    final_text = _normalize_whitespace(final_text)

    if not final_text:
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

def extract_content_text(soup: BeautifulSoup, selectors=None):
    if isinstance(selectors, str):
        selectors = [selectors]
    else:
        selectors = list(selectors or [])
    tried = []

    def element_text(elem):
        if elem is None:
            return ""
        for junk in elem.find_all(["script", "style", "noscript", "form", "iframe"]):
            junk.decompose()
        text = elem.get_text("\n", strip=True)
        return _normalize_whitespace(text)

    for sel in selectors + DEFAULT_CONTENT_SELECTORS:
        if sel in tried:
            continue
        tried.append(sel)
        for node in soup.select(sel):
            text = element_text(node)
            if len(text) >= 120:
                return text
            # Короткие карточки тоже могут встречаться
            if len(text) >= 40:
                return text

    # Fallback: собрать параграфы из <article> или <body>
    container = soup.find("article") or soup.body
    if container:
        paragraphs = []
        for p in container.find_all(["p", "li"]):
            txt = _normalize_whitespace(p.get_text(" ", strip=True))
            if len(txt) >= 20:
                if _contains_deny_phrase(txt):
                    continue
                paragraphs.append(txt)
        if paragraphs:
            return "\n\n".join(paragraphs)

    return None


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

def try_parse_any_date(candidates, *, url=None, source=None, signal="heuristic", now=None):
    default_base = make_aware_msk(datetime.now(MSK).replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0))
    for raw in candidates:
        s = raw.strip()
        low = s.lower()
        if "сегодня" in low or "today" in low:
            m = re.search(r"(\d{1,2}):(\d{2})", low)
            hh, mm = (int(m.group(1)), int(m.group(2))) if m else (12, 0)
            dt = make_aware_msk(datetime.now(MSK)).replace(hour=hh, minute=mm, second=0, microsecond=0)
            return validate_publication_datetime(dt, raw_value=s, url=url, source=source, signal=signal, now=now)
        if "вчера" in low or "yesterday" in low:
            m = re.search(r"(\d{1,2}):(\d{2})", low)
            hh, mm = (int(m.group(1)), int(m.group(2))) if m else (12, 0)
            dt = make_aware_msk(datetime.now(MSK) - timedelta(days=1)).replace(hour=hh, minute=mm, second=0, microsecond=0)
            return validate_publication_datetime(dt, raw_value=s, url=url, source=source, signal=signal, now=now)
        # Try ISO-like first
        try:
            dt = finalize_datetime(dparser.isoparse(s))
            if dt:
                return validate_publication_datetime(dt, raw_value=s, url=url, source=source, signal=signal, now=now)
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
                return validate_publication_datetime(dt, raw_value=s, url=url, source=source, signal=signal, now=now)
        except Exception:
            pass
        # Try Russian words
        dt = parse_ru_date_words(s)
        if dt:
            dt = finalize_datetime(dt)
            if dt:
                return validate_publication_datetime(dt, raw_value=s, url=url, source=source, signal=signal, now=now)
    return None


def _parse_datetime_signal(value: str | None, signal: str, *, url=None, source=None, now=None) -> datetime | None:
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
            dt, raw_value=value, url=url, source=source, signal=signal, now=now
        )
    return None


def extract_published_datetime(soup: BeautifulSoup, url: str | None = None, source: str | None = None) -> datetime | None:
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
        return _parse_datetime_signal(candidate, signal, url=url, source=source)

    # Primary meta tags.
    for tag in soup.find_all("meta", attrs={"property": "article:published_time"}):
        dt = attempt(tag.get("content") or tag.get("value"), "meta[property=article:published_time]")
        if dt:
            return dt

    for tag in soup.find_all("meta", attrs={"name": "pubdate"}):
        dt = attempt(tag.get("content") or tag.get("value"), "meta[name=pubdate]")
        if dt:
            return dt

    for node in soup.find_all(attrs={"itemprop": "datePublished"}):
        dt = attempt(
            node.get("content")
            or node.get("datetime")
            or node.get_text(" ", strip=True),
            "[itemprop=datePublished]",
        )
        if dt:
            return dt

    # <time> elements
    for t in soup.find_all("time"):
        for candidate in (t.get("datetime"), t.get("content"), t.get_text(" ", strip=True)):
            dt = attempt(candidate, "<time>")
            if dt:
                return dt

    # JSON-LD datePublished/dateCreated.
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
            for key in ("datePublished", "dateCreated", "dateModified", "uploadDate"):
                raw_val = node.get(key)
                if isinstance(raw_val, str):
                    dt = attempt(raw_val, f"json_ld:{key}")
                    if dt:
                        return dt

    # Fallback heuristics.
    for candidate in extract_date_candidates(soup):
        dt = attempt(candidate, "heuristic")
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


def extract_article_content(
    url: str,
    html: str,
    selectors: list[str] | str | None = None,
    title: str | None = None,
    src: dict | None = None,
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
    raw_text = extract_content_text(primary_soup, selectors=combined_selectors)
    content_source = "primary_selectors" if raw_text else ""
    content_text = clean_content_text(raw_text, title=title)

    if _is_short_content(content_text):
        json_ld_body = extract_json_ld_article_body(soup)
        json_ld_clean = clean_content_text(json_ld_body, title=title)
        if json_ld_clean and _word_count(json_ld_clean) > _word_count(content_text or ""):
            content_text = json_ld_clean
            content_source = "jsonld"

    if not content_text:
        fallback_text = extract_content_with_fallback(soup, combined_selectors, title)
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
    amp_used = False
    selectors = content_selectors
    content_text: str | None
    title: str
    content_source = "primary_selectors"
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
            )
            if amp_text and not _is_short_content(amp_text):
                content_text = amp_text
                content_source = amp_label or amp_source or "amp"
                amp_used = True

    dt = extract_published_datetime(soup, url, source_name)

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
                )
            except ValueError:
                dt = None

    canonical_url = extract_canonical_url(soup, url)
    url_key = _normalize_canonical_url(url) or url
    alias_map = STATE.setdefault("aliases", {})
    content_hashes = STATE.setdefault("content_hashes", {})
    canonical_key = canonical_url or alias_map.get(url_key) or url_key

    fingerprint = _content_fingerprint(content_text)
    if fingerprint:
        existing = content_hashes.get(fingerprint)
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
    item_id = canonical_ids.get(id_source)
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


def harvest_json_source(src: dict, force: bool = False):
    endpoint = src.get("api_endpoint")
    if not endpoint:
        logging.warning("  missing api_endpoint for %s", src.get("name"))
        return []

    src_name = src.get("name", "")
    logging.info("Harvest API: %s — %s", src_name, endpoint)

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Language": "ru,en;q=0.9",
    }
    host = urlparse(endpoint).netloc
    delay = HOST_DELAY_OVERRIDES.get(host, HOST_DELAY_DEFAULT)
    now = time.time()
    sleep_for = _last_req_at[host] + delay - now
    if sleep_for > 0:
        time.sleep(sleep_for)

    try:
        resp = SESSION.get(endpoint, headers=headers, timeout=REQUEST_TIMEOUT)
        _last_req_at[host] = time.time()
        if resp.status_code == 429:
            ra = resp.headers.get("Retry-After")
            try:
                wait = int(ra) if ra else 5
            except ValueError:
                wait = 5
            logging.warning("429 Too Many Requests (API): %s -> sleep %ss", endpoint, wait)
            time.sleep(wait)
            resp = SESSION.get(endpoint, headers=headers, timeout=REQUEST_TIMEOUT)
            _last_req_at[host] = time.time()
        resp.raise_for_status()
    except requests.RequestException as exc:
        logging.warning("API fetch failed for %s: %s", src_name, exc)
        SOURCE_SUMMARY[src_name]["index_fetch_status"] = "failed"
        SOURCE_SUMMARY[src_name]["last_error"] = str(exc)
        if src.get("html_fallback_on_empty_api"):
            logging.info("API failed for %s — falling back to HTML index", src_name)
            return harvest_source(src, force=ARGS.rebuild if ARGS else False)
        raise

    text = resp.text
    SOURCE_SUMMARY[src_name]["index_fetch_status"] = "fetched"
    idx_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    ih = STATE.setdefault("index_hash", {})
    if not force and ih.get(endpoint) == idx_digest:
        logging.info("Index unchanged (API): %s — %s", src.get("name"), endpoint)
        SOURCE_SUMMARY[src_name]["index_fetch_status"] = "unchanged"
        return []
    ih[endpoint] = idx_digest

    try:
        payload = resp.json()
    except ValueError as exc:
        logging.error("  invalid JSON for %s: %s", src.get("name"), exc)
        SOURCE_SUMMARY[src_name]["index_fetch_status"] = "parser_error"
        SOURCE_SUMMARY[src_name]["last_error"] = str(exc)
        if src.get("html_fallback_on_empty_api"):
            logging.info("API invalid JSON for %s — falling back to HTML index", src_name)
            return harvest_source(src, force=ARGS.rebuild if ARGS else False)
        return []

    data = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(data, list):
        logging.warning("  unexpected API payload for %s", src.get("name"))
        if src.get("html_fallback_on_empty_api"):
            logging.info("API unexpected payload for %s — falling back to HTML index", src_name)
            return harvest_source(src, force=ARGS.rebuild if ARGS else False)
        return []

    base_url = src.get("base_url") or endpoint
    max_links = int(src.get("max_links", MAX_LINKS_PER_SOURCE))
    min_words = SOURCE_MIN_WORDS.get(src_name, DEFAULT_MIN_WORDS)
    SOURCE_SUMMARY[src_name]["min_words"] = min_words
    summary_total_before = SOURCE_SUMMARY[src_name]["total"]

    if not data:
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

    if force:
        new_entries = entries
    else:
        new_entries = [it for it in entries if it[0] not in already_seen]
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
                item = build_item(
                    url,
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
                    html = fetch_page(url, src=src)
                    item = build_item(
                        url,
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
                html = fetch_page(url, src=src)
                item = build_item(
                    url,
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
                    date_val, "api:date", url=url, source=src_name
                )
            if not parsed_dt:
                human_date = _first_non_empty(containers, API_DATE_HUMAN_KEYS)
                if human_date:
                    parsed_dt = try_parse_any_date(
                        [human_date], url=url, source=src_name, signal="api:human_date"
                    )
            if parsed_dt:
                item["published_at"] = parsed_dt.isoformat()
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
                processed_links.append(url)
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
                processed_links.append(url)
                continue
            SOURCE_SUMMARY[src_name]["total"] += 1
            items.append(_finalize_item_schema(item))
            processed_links.append(url)
        except Exception as e:
            logging.warning("  skip %s: %s", url, e)
            SOURCE_SUMMARY[src_name]["last_error"] = str(e)

    attempted_links = bool(processed_links)

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


def harvest_source(src: dict, force: bool = False):
    stats = STATE.setdefault("stats", {})
    cooldowns = stats.setdefault("cooldowns", {})
    errors = stats.setdefault("errors", [])

    src_name = src.get("name", "")
    start_url = src["start_url"]
    fallback_start_urls = src.get("fallback_start_urls") or []
    if isinstance(fallback_start_urls, (str, bytes)):
        fallback_start_urls = [fallback_start_urls]
    start_candidates = [start_url] + [u for u in fallback_start_urls if u and u != start_url]
    min_words = SOURCE_MIN_WORDS.get(src_name, DEFAULT_MIN_WORDS)
    SOURCE_SUMMARY[src_name]["min_words"] = min_words
    cache_path = PAGES_DIR / cache_key_for(start_url)
    cooldown_until = cooldowns.get(start_url)
    now = time.time()
    use_only_cache = False
    index_html = None
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
        for candidate_idx, candidate_url in enumerate(start_candidates, start=1):
            logging.info(
                "Index candidate %d/%d for %s: %s",
                candidate_idx,
                len(start_candidates),
                src.get("name"),
                candidate_url,
            )
            try:
                candidate_html = fetch_page(candidate_url, src=src)
                candidate_cache_path = PAGES_DIR / cache_key_for(candidate_url)
                candidate_raw_links = _count_index_candidates(
                    candidate_html,
                    parse_embedded_links=bool(src.get("parse_embedded_links")),
                )
                if candidate_raw_links == 0 and candidate_cache_path.exists():
                    cached_candidate_html = candidate_cache_path.read_text(encoding="utf-8")
                    cached_raw_links = _count_index_candidates(
                        cached_candidate_html,
                        parse_embedded_links=bool(src.get("parse_embedded_links")),
                    )
                    if cached_raw_links > 0:
                        logging.warning(
                            "Index candidate empty for %s: %s -> using cached index with %d raw links",
                            src.get("name"),
                            candidate_url,
                            cached_raw_links,
                        )
                        candidate_html = cached_candidate_html
                        candidate_raw_links = cached_raw_links
                if candidate_raw_links == 0 and candidate_idx < len(start_candidates):
                    logging.warning(
                        "Index candidate produced 0 raw links for %s: %s -> trying fallback",
                        src.get("name"),
                        candidate_url,
                    )
                    continue
                index_html = candidate_html
                SOURCE_SUMMARY[src_name]["index_fetch_status"] = "fetched"
                if candidate_url != src["start_url"]:
                    logging.info("Index fetched via fallback URL for %s: %s", src.get("name"), candidate_url)
                start_url = candidate_url
                cache_path = PAGES_DIR / cache_key_for(start_url)
                break
            except (requests.RequestException, SourceTemporarilyUnavailable) as exc:
                last_exc = exc
                logging.warning("Index candidate failed for %s: %s (%s)", src.get("name"), candidate_url, exc)

        if index_html is None and last_exc is not None:
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
                failures = STATE.setdefault("stats", {}).setdefault("errors", [])
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

    # Если содержимое ленты не изменилось — пропускаем весь источник
    idx_digest = hashlib.sha256(index_html.encode("utf-8")).hexdigest()
    ih = STATE.setdefault("index_hash", {})
    if not force and ih.get(src["start_url"]) == idx_digest:
        logging.info("Index unchanged: %s — %s", src["name"], src["start_url"])
        SOURCE_SUMMARY[src_name]["index_fetch_status"] = "unchanged"
        return []
    ih[src["start_url"]] = idx_digest

    # XML/HTML автодетект
    soup = _parse_index_soup(index_html)

    # Collect candidate links
    links = []
    include_patterns = src.get("include_patterns")
    if include_patterns:
        if isinstance(include_patterns, (str, bytes)):
            include_patterns = [include_patterns]
        else:
            include_patterns = [p for p in include_patterns if p]
    else:
        include_patterns = []

    include_regex = src.get("include_regex")
    include_res = []
    if include_regex:
        raw_patterns = (
            [include_regex]
            if isinstance(include_regex, (str, bytes))
            else [p for p in include_regex if p]
        )
        for pattern in raw_patterns:
            try:
                include_res.append(re.compile(pattern))
            except re.error as exc:
                logging.warning(
                    "Invalid include_regex %r for %s: %s",
                    pattern,
                    src.get("name"),
                    exc,
                )

    exclude_regex = src.get("exclude_regex")
    exclude_res = []
    if exclude_regex:
        raw_patterns = (
            [exclude_regex]
            if isinstance(exclude_regex, (str, bytes))
            else [p for p in exclude_regex if p]
        )
        for pattern in raw_patterns:
            try:
                exclude_res.append(re.compile(pattern))
            except re.error as exc:
                logging.warning(
                    "Invalid exclude_regex %r for %s: %s",
                    pattern,
                    src.get("name"),
                    exc,
                )

    raw_candidates: list[tuple[str, str, bool]] = []
    for a in soup.find_all("a"):
        href = a.get("href")
        if not href:
            continue
        raw_candidates.append((href, a.get_text(strip=True) or "", True))

    for tag_name in ("loc", "link"):
        for tag in soup.find_all(tag_name):
            text = tag.get_text(strip=True)
            if text.startswith("http"):
                raw_candidates.append((text, text, False))

    # Some protected/dynamic pages expose URLs only inside JSON blobs (e.g. script data).
    if src.get("parse_embedded_links"):
        include_snippets = [str(pattern) for pattern in include_patterns if pattern]
        for expanded_html in _embedded_text_variants(index_html):
            for match in re.findall(r'https?://[^\s"\'<>]+', expanded_html):
                raw_candidates.append((match, "", False))

            for rel in re.findall(r'"(/[^"<>\s]{6,260})"', expanded_html):
                if include_snippets and not any(snippet in rel for snippet in include_snippets):
                    continue
                raw_candidates.append((rel, "", False))

    base_host = urlparse(src["base_url"]).netloc.replace("www.", "")
    for raw_href, link_text, is_anchor in raw_candidates:
        href = urljoin(src["base_url"], raw_href)
        if href.rstrip("/") == start_url.rstrip("/"):
            SOURCE_SUMMARY[src_name]["listing"] += 1
            continue
        if is_listing_url(href, start_url=start_url):
            SOURCE_SUMMARY[src_name]["listing"] += 1
            if ARGS and getattr(ARGS, "debug", False):
                logging.debug("Filtered listing URL: %s", href)
            continue
        if src.get("restrict_domain"):
            h = urlparse(href).netloc.replace("www.", "")
            if h != base_host:
                continue
        if include_patterns and not any(p in href for p in include_patterns):
            continue
        if include_res and not any(r.search(href) for r in include_res):
            continue
        if exclude_res and any(r.search(href) for r in exclude_res):
            continue
        if is_anchor:
            # Allow empty anchors when source explicitly permits it.
            min_len = int(src.get("link_min_text_len", 0))
            if len(link_text) < min_len and not src.get("accept_empty_anchor"):
                continue
        links.append(href)

    logging.info("Link extraction stats for %s: raw=%d accepted=%d", src_name, len(raw_candidates), len(links))
    SOURCE_SUMMARY[src_name]["raw_link_candidates"] = len(raw_candidates)
    SOURCE_SUMMARY[src_name]["accepted_links"] = len(links)

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

    # Обрабатываем только новые относительно последнего прогона
    seen_map = STATE.setdefault("seen_urls", {})
    already_seen_list = list(seen_map.get(src["name"], []))
    already_seen = set(already_seen_list)

    if force:
        new_links = uniq  # при rebuild обрабатываем все доступные uniq-ссылки
    else:
        new_links = [u for u in uniq if u not in already_seen]
        if not new_links:
            logging.info("  no new links for %s", src["name"])
            return []
    SOURCE_SUMMARY[src_name]["attempted_articles"] += len(new_links)

    items = []
    processed_links = []

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
            processed_links.append(url)
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
            processed_links.append(url)
            return
        SOURCE_SUMMARY[src_name]["total"] += 1
        items.append(_finalize_item_schema(item))
        processed_links.append(url)

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
                html = fetch_page(url, src=src)
            item = build_item(
                url,
                src_name,
                html,
                content_selectors=src.get("content_selectors"),
                src=src,
            )
            handle_item(item, url)
        except SourceTemporarilyUnavailable as exc:
            page_path = PAGES_DIR / cache_key_for(url)
            if page_path.exists():
                SOURCE_SUMMARY[src_name]["cached_fallback_used"] = True
                logging.warning(
                    "  using cached copy for %s due to temporary issue: %s", url, exc
                )
                html = page_path.read_text(encoding="utf-8")
                item = build_item(
                    url,
                    src_name,
                    html,
                    content_selectors=src.get("content_selectors"),
                    src=src,
                )
                handle_item(item, url)
            else:
                logging.warning("  skip %s: %s", url, exc)
                SOURCE_SUMMARY[src_name]["last_error"] = str(exc)
        except Exception as e:
            logging.warning("  skip %s: %s", url, e)
            SOURCE_SUMMARY[src_name]["last_error"] = str(e)

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
            "%s | total=%d empty=%d short=%d listing=%d api=%d amp=%d min_words=%d",
            name,
            summary.get("total", 0),
            summary.get("empty", 0),
            summary.get("short", 0),
            summary.get("listing", 0),
            summary.get("api", 0),
            summary.get("amp", 0),
            int(summary.get("min_words", DEFAULT_MIN_WORDS) or 0),
        )


def write_source_health_report(sources: list[dict]) -> None:
    """Persist crawl diagnostics independently from the retained feed."""
    rows = []
    streaks = SOURCE_HEALTH_STATE
    for source in sources:
        if not source.get("enabled", True):
            continue
        name = source.get("name", "")
        summary = SOURCE_SUMMARY[name]
        status = summary.get("index_fetch_status", "not_attempted")
        attempted = int(summary.get("attempted_articles", 0) or 0)
        accepted = int(summary.get("total", 0) or 0)
        article_outage = attempted > 0 and accepted == 0 and bool(summary.get("last_error"))
        failed = status in {"failed", "parser_error", "not_attempted"} or article_outage
        previous_streak = int(streaks.get(name, 0) or 0)
        if status != "skipped_selection":
            streaks[name] = previous_streak + 1 if failed else 0
        else:
            streaks.setdefault(name, previous_streak)
        rows.append({
            "source": name,
            "index_fetch_status": status,
            "consecutive_failures": streaks[name],
            "raw_link_candidates": summary.get("raw_link_candidates", 0),
            "accepted_links": summary.get("accepted_links", 0),
            "attempted_articles": attempted,
            "accepted_articles": accepted,
            "empty_rejections": summary.get("empty", 0),
            "short_rejections": summary.get("short", 0),
            "future_date_rejections": summary.get("future_date_rejections", 0),
            "cached_fallback_used": bool(summary.get("cached_fallback_used", False)),
            "last_error": summary.get("last_error"),
        })
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(), "sources": rows}
    SOURCE_HEALTH_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    # Streaks are updated while producing the report, after the normal state
    # save in the caller, so persist them here as part of the same operation.
    prune_page_cache()
    save_state()
    save_source_health_state()


def retain_bounded_items(items: list[dict]) -> list[dict]:
    """Apply fair, deterministic retention to an already newest-first list."""
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
    remainder = [
        item for item in items
        if str(item.get("id") or item.get("url") or "") not in reserved_ids
    ]
    combined = reserved + remainder
    combined.sort(
        key=lambda x: x.get("published_at") or x.get("fetched_at") or x.get("first_seen") or "",
        reverse=True,
    )
    selected_ids = {
        str(item.get("id") or item.get("url") or "") for item in reserved
    }
    # Keep reservations even when they are older than the global cutoff.
    selected = list(reserved)
    selected.extend(item for item in combined if str(item.get("id") or item.get("url") or "") not in selected_ids)
    selected = selected[:FEED_MAX_ITEMS]
    selected.sort(
        key=lambda x: x.get("published_at") or x.get("fetched_at") or x.get("first_seen") or "",
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
        key=lambda x: x.get("published_at") or x.get("fetched_at") or x.get("first_seen") or "",
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
        if is_listing_url(url):
            if ARGS and getattr(ARGS, "debug", False):
                logging.debug("Drop listing URL from existing feed: %s", url)
            continue
        by_key[key] = _finalize_item_schema(dict(it))

    for it in new:
        # Sanitize incoming records before comparing publication times. This
        # prevents a rejected future value from replacing (and then erasing) a
        # plausible date already held by the existing record.
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

        by_key[key] = _finalize_item_schema(merged)

    merged_items = list(by_key.values())
    merged_items.sort(
        key=lambda x: x.get("published_at") or x.get("fetched_at") or x.get("first_seen") or "",
        reverse=True,
    )

    merged_items = retain_bounded_items(merged_items)

    return merged_items

def main():
    global ARGS, CONNECT_TIMEOUT, READ_TIMEOUT, REQUEST_TIMEOUT, START_TIME, RUNTIME_EXCEEDED, _RUNTIME_LOGGED
    global OUT_JSON, EXISTING_FEED_JSON, SOURCE_HEALTH_JSON, SOURCE_HEALTH_STATE_FILE, SOURCE_HEALTH_STATE
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
    parser.add_argument("--source-health-state", type=pathlib.Path, default=SOURCE_HEALTH_STATE_FILE, help="Persistent failure-streak state path")
    ARGS = parser.parse_args()

    OUT_JSON = ARGS.output
    SOURCE_HEALTH_JSON = ARGS.source_health_output
    EXISTING_FEED_JSON = ARGS.existing_feed
    SOURCE_HEALTH_STATE_FILE = ARGS.source_health_state
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    SOURCE_HEALTH_JSON.parent.mkdir(parents=True, exist_ok=True)
    if SOURCE_HEALTH_STATE_FILE.exists():
        loaded_health_state = json.loads(SOURCE_HEALTH_STATE_FILE.read_text(encoding="utf-8"))
        SOURCE_HEALTH_STATE = loaded_health_state if isinstance(loaded_health_state, dict) else {}

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
        configured_min = DEFAULT_MIN_WORDS
        source_defined = int(src.get("min_words", 0) or 0)
        if source_defined > 0:
            configured_min = source_defined
        host = _get_host_for_source(src)
        if host:
            normalized_host = host.lower().lstrip("www.")
            override = HOST_MIN_WORD_OVERRIDES.get(normalized_host)
            if override and source_defined <= 0:
                configured_min = override
        SOURCE_MIN_WORDS[src_name] = configured_min
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
            STATE.setdefault("stats", {}).setdefault("errors", []).append({"source": src.get("name"), "url": src.get("start_url"), "error": str(e)})

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
