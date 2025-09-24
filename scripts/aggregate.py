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

REQUEST_TIMEOUT = 30
USER_AGENT = DEFAULT_USER_AGENT
MAX_LINKS_PER_SOURCE = 100
FEED_MAX_ITEMS = int(os.environ.get("FEED_MAX_ITEMS", "2000"))
ARGS = None  # будет заполнено в main()

LISTING_PATH_RE = re.compile(r"/(?:news/?$|tag/|category/|archive/|search/|page/)", re.I)
LISTING_QUERY_RE = re.compile(r"(?:^|&)(?:page=\d+|PAGEN_\d+=\d+|VOTE_ID=\d+)", re.I)


def is_listing_url(u: str) -> bool:
    p = urlparse(u)
    if LISTING_PATH_RE.search(p.path or ""):
        return True
    if p.query and LISTING_QUERY_RE.search(p.query):
        return True
    return False


def log_listing_skip(url: str):
    if ARGS and getattr(ARGS, "debug", False):
        logging.info("Skip listing URL: %s", url)

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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

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

HOST_STRATEGIES: dict[str, RequestStrategy] = {}
HOST_CLIENTS: dict[str, HostClient] = {}

def save_state():
    STATE_FILE.write_text(json.dumps(STATE, ensure_ascii=False, indent=2), encoding="utf-8")

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
            timeout_value = None if client.strategy.timeout else (REQUEST_TIMEOUT, REQUEST_TIMEOUT)
            resp = client.get(
                url,
                headers=hdrs,
                allow_redirects=True,
                timeout=timeout_value,
            )
        else:
            resp = SESSION.get(url, headers=hdrs, timeout=REQUEST_TIMEOUT, allow_redirects=True)
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
        resp = SESSION.get(url, headers=hdrs, timeout=REQUEST_TIMEOUT, allow_redirects=True)
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
        content = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT).text
    page_path.write_text(content, encoding="utf-8")
    return content

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


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    return soup.get_text("\n", strip=True)


def build_item(url: str, source_name: str, html: str, content_selectors=None):
    soup = BeautifulSoup(html, "html.parser")
    title = extract_title(soup) or url
    cands = extract_date_candidates(soup)
    dt = try_parse_any_date(cands)

    # Fallback: URL like /2024/09/21/
    if dt is None:
        m = re.search(r"/(20\d{2})/([01]\d)/([0-3]\d)/", url)
        if m:
            y, mo, d = map(int, m.groups())
            try:
                dt = finalize_datetime(datetime(y, mo, d))
            except ValueError:
                dt = None

    item_id = hashlib.sha256(url.encode("utf-8")).hexdigest()
    content_text = extract_content_text(soup, selectors=content_selectors)

    item = {
        "id": item_id,
        "url": url,
        "title": title,
        "date_published": dt.isoformat() if dt else None,
        "content_text": content_text,
        "tags": [],
        "source": source_name,
    }

    if not item["date_published"]:
        first_seen_map = STATE.setdefault("first_seen", {})
        cached = first_seen_map.get(item_id)
        if cached:
            item["date_published"] = cached
        else:
            seen_dt = make_aware_msk(datetime.now(MSK)).replace(second=0, microsecond=0)
            iso = seen_dt.isoformat()
            first_seen_map[item_id] = iso
            item["date_published"] = iso

    return item

def harvest_json_source(src: dict, force: bool = False):
    endpoint = src.get("api_endpoint")
    if not endpoint:
        logging.warning("  missing api_endpoint for %s", src.get("name"))
        return []

    logging.info("Harvest API: %s — %s", src.get("name"), endpoint)

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
            log_listing_skip(url)
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
    for url, entry in new_entries:
        try:
            html = fetch_page(url)
            item = build_item(url, src["name"], html, content_selectors=src.get("content_selectors"))
            body_html = entry.get("content") or entry.get("text") or entry.get("body")
            if body_html:
                text = html_to_text(body_html)
                if text:
                    item["content_text"] = text
            title = entry.get("name") or entry.get("title")
            if title:
                item["title"] = title.strip()
            date_val = entry.get("publishedAt") or entry.get("publishDate") or entry.get("publish_date")
            if date_val:
                try:
                    dt = finalize_datetime(dparser.isoparse(date_val))
                    if dt:
                        item["date_published"] = dt.isoformat()
                except Exception:
                    pass
            elif entry.get("publishDateRus"):
                dt = try_parse_any_date([entry["publishDateRus"]])
                if dt:
                    item["date_published"] = dt.isoformat()
            items.append(item)
        except Exception as e:
            logging.warning("  skip %s: %s", url, e)

    keep = 500
    tail = [u for u in already_seen_list if u in entry_urls]
    seen_map[src["name"]] = ([url for url, _ in new_entries] + tail)[:keep]

    return items


def harvest_source(src: dict, force: bool = False):
    stats = STATE.setdefault("stats", {})
    cooldowns = stats.setdefault("cooldowns", {})
    errors = stats.setdefault("errors", [])

    start_url = src["start_url"]
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
        if src.get("restrict_domain"):
            h = urlparse(href).netloc.replace("www.", "")
            if h != base_host:
                continue
        if is_listing_url(href):
            log_listing_skip(href)
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
    for url in new_links:
        try:
            if use_only_cache:
                page_path = PAGES_DIR / cache_key_for(url)
                if page_path.exists():
                    html = page_path.read_text(encoding="utf-8")
                else:
                    raise FileNotFoundError("cached copy missing during cooldown")
            else:
                html = fetch_page(url, src=src)
            item = build_item(url, src["name"], html, content_selectors=src.get("content_selectors"))
            if item:
                items.append(item)
        except SourceTemporarilyUnavailable as exc:
            page_path = PAGES_DIR / cache_key_for(url)
            if page_path.exists():
                logging.warning(
                    "  using cached copy for %s due to temporary issue: %s", url, exc
                )
                html = page_path.read_text(encoding="utf-8")
                item = build_item(
                    url,
                    src["name"],
                    html,
                    content_selectors=src.get("content_selectors"),
                )
                if item:
                    items.append(item)
            else:
                logging.warning("  skip %s: %s", url, exc)
        except Exception as e:
            logging.warning("  skip %s: %s", url, e)

    # обновим «виденные» ссылки — держим скользящее окно последних 500
    keep = 500
    # сначала — новые (в порядке обхода), затем часть старых, которые ещё встречаются в uniq
    tail = [u for u in already_seen_list if u in uniq]
    # при rebuild тоже обновляем, чтобы после форс-прогона обычные запуски работали эффективно
    seen_map[src["name"]] = (new_links + tail)[:keep]

    return items

def build_feed(all_items):
    # Deduplicate by URL (keep newest date if available)
    by_url = {}
    for it in all_items:
        u = it["url"]
        if u in by_url:
            a = by_url[u]
            if (not a.get("date_published")) and it.get("date_published"):
                by_url[u] = it
        else:
            by_url[u] = it
    items = []
    for it in by_url.values():
        title = (it.get("title") or "").strip()
        url = it.get("url") or ""
        if ('SKIP_KEYWORDS' in globals() and SKIP_KEYWORDS and (SKIP_KEYWORDS.search(title) or SKIP_KEYWORDS.search(url))):
            continue
        if not it.get("date_published"):
            continue
        items.append(it)

    # Sort by date desc (новые сверху), nulls-last (но мы null уже отфильтровали)
    def sort_key(x):
        dp = x.get("date_published")
        return (0, dp) if dp else (1, "")
    items.sort(key=sort_key, reverse=True)

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
    """Склеить items, убрать дубликаты по URL, предпочитая новые записи и записи с заполненной датой."""

    def has_rich_value(value):
        if value is None:
            return False
        if isinstance(value, str):
            return value.strip() != ""
        return True

    rich_fields = {"content_text", "content_html", "summary"}

    by_url = {}
    for it in existing:
        u = it.get("url")
        if not u:
            continue
        by_url[u] = it
    for it in new:
        u = it.get("url")
        if not u:
            continue
        old = by_url.get(u)
        if not old:
            by_url[u] = it
            continue

        merged = dict(old)
        for key, value in it.items():
            if key == "date_published":
                old_date = old.get("date_published")
                new_date = value
                if old_date and new_date:
                    item_id = it.get("id") or old.get("id")
                    if item_id:
                        first_seen_map = STATE.get("first_seen", {})
                        fallback_date = first_seen_map.get(item_id)
                        if fallback_date and fallback_date == new_date:
                            # The new value comes from the first-seen fallback; keep the
                            # previously stored publication date instead of overwriting it
                            # with the crawl timestamp.
                            continue
            if key in rich_fields and not has_rich_value(value):
                continue
            merged[key] = value

        by_url[u] = merged

    merged = list(by_url.values())

    # Сортировка по дате у нас окончательно произойдёт в build_feed,
    # но слегка подсортируем тут, чтобы ограничение по размеру не «съело» самые новые.
    merged.sort(key=lambda x: x.get("date_published") or "", reverse=True)

    # Обрезка по размеру
    if FEED_MAX_ITEMS and len(merged) > FEED_MAX_ITEMS:
        merged = merged[:FEED_MAX_ITEMS]

    return merged

def main():
    global ARGS
    parser = argparse.ArgumentParser(description="Aggregate news feed")
    parser.add_argument("--rebuild", action="store_true", help="Force rebuild: ignore index unchanged and seen-URL filters; always rewrite unified.json")
    parser.add_argument("--dry-run", action="store_true", help="Run without writing unified.json/state")
    parser.add_argument("--debug", action="store_true", help="Enable verbose debug output")
    ARGS = parser.parse_args()

    sources = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
    HOST_STRATEGIES.update(build_strategy_registry(sources))
    all_items = []
    for src in sources:
        if not src.get('enabled', True):
            logging.info("Skip disabled source: %s — %s", src.get('name'), src.get('start_url'))
            continue
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

if __name__ == "__main__":
    sys.exit(main())
