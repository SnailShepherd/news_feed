#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, json, logging, pathlib, sys, hashlib, argparse, random
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

import requests
import time
from collections import defaultdict
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
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
OUT_JSON = DOCS_DIR / "unified.json"

CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 10.0
REQUEST_TIMEOUT = (CONNECT_TIMEOUT, READ_TIMEOUT)
USER_AGENT = DEFAULT_USER_AGENT
MAX_LINKS_PER_SOURCE = 100
FEED_MAX_ITEMS = int(os.environ.get("FEED_MAX_ITEMS", "2000"))
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
SOURCE_SUMMARY: dict[str, dict[str, int]] = defaultdict(
    lambda: {
        "total": 0,
        "empty": 0,
        "short": 0,
        "listing": 0,
        "api": 0,
        "amp": 0,
    }
)
SOURCE_MIN_WORDS: dict[str, int] = {}

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
    total=5, connect=3, read=3, backoff_factor=1.5,
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

if STATE_FILE.exists():
    STATE = json.loads(STATE_FILE.read_text(encoding="utf-8"))
else:
    STATE = {"headers": {}, "stats": {}, "index_hash": {}, "seen_urls": {}}

STATE.setdefault("first_seen", {})
STATE.setdefault("host_state", {})
STATE.setdefault("aliases", {})
STATE.setdefault("content_hashes", {})

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
        ".page-content",
        ".layout-two-columns .main-column",
        "article",
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
}

def save_state():
    STATE_FILE.write_text(json.dumps(STATE, ensure_ascii=False, indent=2), encoding="utf-8")


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


def get_host_client(url: str, src: dict | None = None) -> HostClient | None:
    host = urlparse(url).netloc
    src_host = _get_host_for_source(src)
    if src_host:
        host = src_host
    strategy = HOST_STRATEGIES.get(host)
    if not strategy:
        return None
    client = HOST_CLIENTS.get(host)
    if client is None:
        client = HostClient(host, strategy, STATE)
        HOST_CLIENTS[host] = client
    return client


def http_get(url: str, allow_conditional: bool = True, src: dict | None = None):
    hdrs = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
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


AMP_APPEND_WHITELIST = {"rg.ru", "ria.ru", "realty.ria.ru"}


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
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status in {500, 502, 503, 504} and page_path.exists():
            logging.warning(
                "HTTP %s for %s — using cached copy", status, url
            )
            return page_path.read_text(encoding="utf-8")
        raise
    except SourceTemporarilyUnavailable:
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


def fetch_amp_if_available(url: str, soup: BeautifulSoup, src: dict | None = None) -> str | None:
    amp_href = None
    for link in soup.find_all("link"):
        rel = link.get("rel")
        if not rel:
            continue
        if isinstance(rel, (list, tuple)):
            rels = [str(r).lower() for r in rel]
        else:
            rels = [part.lower() for part in str(rel).split() if part]
        if "amphtml" in rels:
            href = link.get("href")
            if href:
                amp_href = urljoin(url, href)
                break
    if amp_href is None:
        host = urlparse(url).netloc
        if _amp_append_allowed(host):
            base = url.rstrip("/")
            if base and not base.endswith("/amp"):
                amp_href = f"{base}/amp"
    if not amp_href or amp_href == url:
        return None
    try:
        return fetch_page(amp_href, src=src)
    except Exception as exc:
        logging.debug("AMP fetch failed for %s: %s", amp_href, exc)
        return None

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
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    return "\n".join(lines)


def _word_count(text: str) -> int:
    if not text:
        return 0
    return len(re.findall(r"\w+", text, flags=re.UNICODE))


def _finalize_item_schema(item: dict[str, object]) -> dict[str, object]:
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

def try_parse_any_date(candidates):
    default_base = make_aware_msk(datetime.now(MSK).replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0))
    for raw in candidates:
        s = raw.strip()
        # Try ISO-like first
        try:
            dt = finalize_datetime(dparser.isoparse(s))
            if dt: return dt
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
            if dt: return dt
        except Exception:
            pass
        # Try Russian words
        dt = parse_ru_date_words(s)
        if dt:
            dt = finalize_datetime(dt)
            if dt: return dt
        # Relative dates
        low = s.lower()
        if "сегодня" in low or "today" in low:
            m = re.search(r"(\d{1,2}):(\d{2})", low)
            hh, mm = (int(m.group(1)), int(m.group(2))) if m else (12, 0)
            dt = make_aware_msk(datetime.now(MSK)).replace(hour=hh, minute=mm, second=0, microsecond=0)
            return dt
        if "вчера" in low or "yesterday" in low:
            m = re.search(r"(\d{1,2}):(\d{2})", low)
            hh, mm = (int(m.group(1)), int(m.group(2))) if m else (12, 0)
            dt = make_aware_msk(datetime.now(MSK) - timedelta(days=1)).replace(hour=hh, minute=mm, second=0, microsecond=0)
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
    normalized = parsed._replace(path=path, query="", fragment="")
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
    text = extract_content_text(primary_soup, selectors=combined_selectors)
    if text:
        text = _drop_leading_title(text, title)
        text = _strip_deny_phrases(text)
        text = _normalize_whitespace(text)
    else:
        fallback_text = extract_content_with_fallback(soup, combined_selectors, title)
        if fallback_text:
            fallback_text = _drop_leading_title(fallback_text, title)
            text = _strip_deny_phrases(fallback_text)
            text = _normalize_whitespace(text)
        else:
            text = None

    return text, soup, title


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
    if pre_extracted_content is not None:
        soup = BeautifulSoup(html or "", "html.parser")
        title = extract_title(soup) or url
        content_text = _strip_deny_phrases(_normalize_whitespace(pre_extracted_content))
        content_text = _normalize_whitespace(content_text)
    else:
        content_text, soup, title = extract_article_content(
            url,
            html,
            selectors=selectors,
            title=None,
        )

    if not content_text and html.strip():
        amp_html = fetch_amp_if_available(url, soup, src=src)
        if amp_html:
            amp_text, _, _ = extract_article_content(
                url,
                amp_html,
                selectors=selectors,
                title=title,
            )
            if amp_text:
                content_text = amp_text
                amp_used = True

    cands = extract_date_candidates(soup)
    dt = try_parse_any_date(cands)

    if dt is None:
        m = re.search(r"/(20\d{2})/([01]\d)/([0-3]\d)/", url)
        if m:
            y, mo, d = map(int, m.groups())
            try:
                dt = finalize_datetime(datetime(y, mo, d))
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

    id_source = canonical_key or url
    item_id = hashlib.sha256(id_source.encode("utf-8")).hexdigest()

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
    if dt:
        item["published_at"] = dt.isoformat()
    if canonical_url and canonical_url != url:
        item["canonical_url"] = canonical_url
    if amp_used:
        item["_amp_used"] = True

    return item

API_CONTENT_KEYS = [
    "content",
    "text",
    "body",
    "content_html",
    "text_html",
    "fullText",
    "full_text",
    "contentHtml",
    "html",
    "description",
]


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

    text = resp.text
    idx_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    ih = STATE.setdefault("index_hash", {})
    if not force and ih.get(endpoint) == idx_digest:
        logging.info("Index unchanged (API): %s — %s", src.get("name"), endpoint)
        return []
    ih[endpoint] = idx_digest

    try:
        payload = resp.json()
    except ValueError as exc:
        logging.error("  invalid JSON for %s: %s", src.get("name"), exc)
        return []

    data = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(data, list):
        logging.warning("  unexpected API payload for %s", src.get("name"))
        return []

    base_url = src.get("base_url") or endpoint
    max_links = int(src.get("max_links", MAX_LINKS_PER_SOURCE))
    min_words = int(src.get("min_words", 0) or 0)
    seen_map = STATE.setdefault("seen_urls", {})
    already_seen_list = list(seen_map.get(src["name"], []))
    already_seen = set(already_seen_list)

    entries = []
    seen_links = set()
    for entry in data:
        link = entry.get("link") or entry.get("url") or entry.get("slug")
        if not link:
            continue
        if isinstance(link, str) and not link.startswith("http"):
            url = urljoin(base_url, link)
        else:
            url = link
        if is_listing_url(url):
            SOURCE_SUMMARY[src_name]["listing"] += 1
            if ARGS and getattr(ARGS, "debug", False):
                logging.debug("Filtered listing URL: %s", url)
            continue
        if url in seen_links:
            continue
        seen_links.add(url)
        entries.append((url, entry))
        if len(entries) >= max_links:
            break

    entry_urls = [url for url, _ in entries]

    if force:
        new_entries = entries
    else:
        new_entries = [it for it in entries if it[0] not in already_seen]
        if not new_entries:
            logging.info("  no new links for %s", src["name"])
            return []

    items = []
    processed_links = []
    for idx, (url, entry) in enumerate(new_entries):
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
            containers = [entry]
            attributes = entry.get("attributes")
            if isinstance(attributes, dict):
                containers.append(attributes)
            for container in containers:
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
                        text_val = _normalize_whitespace(val)
                    if text_val:
                        api_text = text_val
                        break
                if api_text:
                    break

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
                SOURCE_SUMMARY[src_name]["api"] += 1
            else:
                html = fetch_page(url, src=src)
                item = build_item(
                    url,
                    src_name,
                    html,
                    content_selectors=src.get("content_selectors"),
                    src=src,
                )
            title = entry.get("name") or entry.get("title")
            if title:
                item["title"] = title.strip()
            date_val = entry.get("publishedAt") or entry.get("publishDate") or entry.get("publish_date")
            if date_val:
                try:
                    dt = finalize_datetime(dparser.isoparse(date_val))
                    if dt:
                        item["published_at"] = dt.isoformat()
                except Exception:
                    pass
            elif entry.get("publishDateRus"):
                dt = try_parse_any_date([entry["publishDateRus"]])
                if dt:
                    item["published_at"] = dt.isoformat()
            content_text = item.get("content_text") or ""
            if not content_text.strip():
                SOURCE_SUMMARY[src_name]["empty"] += 1
            if min_words and _word_count(content_text) < min_words:
                SOURCE_SUMMARY[src_name]["short"] += 1
                processed_links.append(url)
                continue
            SOURCE_SUMMARY[src_name]["total"] += 1
            items.append(_finalize_item_schema(item))
            processed_links.append(url)
        except Exception as e:
            logging.warning("  skip %s: %s", url, e)

    keep = 500
    tail = [u for u in already_seen_list if u in entry_urls]
    seen_map[src["name"]] = (processed_links + tail)[:keep]

    return items


def harvest_source(src: dict, force: bool = False):
    stats = STATE.setdefault("stats", {})
    cooldowns = stats.setdefault("cooldowns", {})
    errors = stats.setdefault("errors", [])

    src_name = src.get("name", "")
    start_url = src["start_url"]
    min_words = int(src.get("min_words", 0) or 0)
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
        try:
            index_html = fetch_page(start_url, src=src)
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
        return []
    ih[src["start_url"]] = idx_digest

    # XML/HTML автодетект
    soup = BeautifulSoup(index_html, "xml" if index_html.lstrip().startswith("<?xml") else "html.parser")

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

    base_host = urlparse(src["base_url"]).netloc.replace("www.", "")
    for a in soup.find_all("a"):
        href = a.get("href")
        if not href:
            continue
        href = urljoin(src["base_url"], href)
        if is_listing_url(href):
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
        text_ok = (a.get_text(strip=True) or "")
        # Allow empty anchors when source explicitly permits it
        min_len = int(src.get("link_min_text_len", 0))
        if len(text_ok) < min_len:
            if src.get("accept_empty_anchor"):
                # fallback to attributes
                txt2 = a.get("title") or a.get("aria-label") or ""
                if len(txt2) < min_len:
                    pass  # still accept link
            else:
                continue
        links.append(href)

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

    items = []
    processed_links = []

    def handle_item(item: dict | None, url: str) -> None:
        if not item:
            return
        content_text = item.get("content_text") or ""
        if not content_text.strip():
            SOURCE_SUMMARY[src_name]["empty"] += 1
        if min_words and _word_count(content_text) < min_words:
            SOURCE_SUMMARY[src_name]["short"] += 1
            processed_links.append(url)
            return
        SOURCE_SUMMARY[src_name]["total"] += 1
        if item.pop("_amp_used", False):
            SOURCE_SUMMARY[src_name]["amp"] += 1
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
        except Exception as e:
            logging.warning("  skip %s: %s", url, e)

    # обновим «виденные» ссылки — держим скользящее окно последних 500
    keep = 500
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
            "%s | total=%d empty=%d short=%d listing=%d api=%d amp=%d",
            name,
            summary.get("total", 0),
            summary.get("empty", 0),
            summary.get("short", 0),
            summary.get("listing", 0),
            summary.get("api", 0),
            summary.get("amp", 0),
        )


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

    items = list(by_id.values())
    items.sort(
        key=lambda x: x.get("published_at") or x.get("first_seen") or "",
        reverse=True,
    )

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
    if not OUT_JSON.exists():
        return []
    try:
        data = json.loads(OUT_JSON.read_text(encoding="utf-8"))
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
    merged_items.sort(key=lambda x: x.get("published_at") or x.get("first_seen") or "", reverse=True)

    if FEED_MAX_ITEMS and len(merged_items) > FEED_MAX_ITEMS:
        merged_items = merged_items[:FEED_MAX_ITEMS]

    return merged_items

def main():
    global ARGS, CONNECT_TIMEOUT, READ_TIMEOUT, REQUEST_TIMEOUT, START_TIME, RUNTIME_EXCEEDED, _RUNTIME_LOGGED
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
    ARGS = parser.parse_args()

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
        SOURCE_MIN_WORDS[src_name] = int(src.get("min_words", 0) or 0)
        if runtime_expired():
            logging.info("Stop processing further sources due to max-runtime limit")
            break
        if not src.get('enabled', True):
            logging.info("Skip disabled source: %s — %s", src.get('name'), src.get('start_url'))
            continue
        if selected_sources and src.get("name") not in selected_sources:
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
            save_state()
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
            save_state()
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
        save_state()
    logging.info("Saved feed to %s (%d items)", OUT_JSON, len(feed["items"]))

    if RUNTIME_EXCEEDED and os.environ.get("CI"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
