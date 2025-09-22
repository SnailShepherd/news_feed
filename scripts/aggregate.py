#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, json, logging, pathlib, sys, hashlib
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse

import requests
import time
from collections import defaultdict
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from dateutil import parser as dparser
import pytz

# ---- Settings ----
ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCS_DIR = ROOT / "docs"
CACHE_DIR = ROOT / ".cache"
PAGES_DIR = CACHE_DIR / "pages"
STATE_FILE = CACHE_DIR / "state.json"
OUT_JSON = DOCS_DIR / "unified.json"

REQUEST_TIMEOUT = 30
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
MAX_LINKS_PER_SOURCE = 100
FEED_MAX_ITEMS = int(os.environ.get("FEED_MAX_ITEMS", "2000"))

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
HOST_DELAY_OVERRIDES = {"www.metalinfo.ru": 6.0, "metalinfo.ru": 6.0}
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

def save_state():
    STATE_FILE.write_text(json.dumps(STATE, ensure_ascii=False, indent=2), encoding="utf-8")

# ---- HTTP ----
def http_get(url: str):
    hdrs = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }
    hinfo = STATE["headers"].get(url, {})
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
    resp = SESSION.get(url, headers=hdrs, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    _last_req_at[host] = time.time()
    # Явный перехват 429 с уважением Retry-After
    if resp.status_code == 429:
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
        return None, hinfo  # indicate to reuse cached file
    resp.raise_for_status()
    # Save headers for next time
    new_hinfo = {}
    et = resp.headers.get("ETag")
    lm = resp.headers.get("Last-Modified")
    if et: new_hinfo["ETag"] = et
    if lm: new_hinfo["Last-Modified"] = lm
    STATE["headers"][url] = new_hinfo
    return resp.text, new_hinfo

def cache_key_for(url: str) -> str:
    p = urlparse(url)
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", (p.path or "/")).strip("-")
    if not slug:
        slug = "index"
    if len(slug) > 150:
        slug = slug[:150]
    return f"{p.netloc}-{slug}.html"

def fetch_page(url: str) -> str:
    page_path = PAGES_DIR / cache_key_for(url)
    content, _ = http_get(url)
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

META_DATE_KEYS = [
    ("meta", "property", "article:published_time"),
    ("meta", "property", "article:modified_time"),
    ("meta", "property", "og:published_time"),
    ("meta", "property", "og:updated_time"),
    ("meta", "name", "pubdate"),
    ("meta", "name", "date"),
    ("meta", "name", "dcterms.date"),
    ("meta", "itemprop", "datePublished"),
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
    for sel in ["span.date", ".news-date", ".article-date", ".date", ".time"]:
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
    for raw in candidates:
        s = raw.strip()
        # Try ISO-like first
        try:
            dt = dparser.isoparse(s)
            dt = make_aware_msk(dt)
            dt = clamp_year(dt)
            if dt: return dt
        except Exception:
            pass
        # Try generic parser in day-first mode
        try:
            dt = dparser.parse(s, dayfirst=True, fuzzy=True, default=make_aware_msk(datetime.now()).replace(month=1, day=1))
            dt = make_aware_msk(dt)
            dt = clamp_year(dt)
            if dt: return dt
        except Exception:
            pass
        # Try Russian words
        dt = parse_ru_date_words(s)
        if dt:
            dt = make_aware_msk(dt)
            dt = clamp_year(dt)
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

def build_item(url: str, source_name: str, html: str):
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
                dt = make_aware_msk(datetime(y, mo, d))
            except ValueError:
                dt = None

    item = {
        "id": hashlib.sha256(url.encode("utf-8")).hexdigest(),
        "url": url,
        "title": title,
        "date_published": dt.isoformat() if dt else None,
        "content_text": None,
        "tags": [],
        "source": source_name,
    }
    return item

def harvest_source(src: dict):
    logging.info("Harvest: %s — %s", src["name"], src["start_url"])
    index_html = fetch_page(src["start_url"])

    # Если содержимое ленты не изменилось — пропускаем весь источник
    idx_digest = hashlib.sha256(index_html.encode("utf-8")).hexdigest()
    ih = STATE.setdefault("index_hash", {})
    if ih.get(src["start_url"]) == idx_digest:
        logging.info("Index unchanged: %s — %s", src["name"], src["start_url"])
        return []
    ih[src["start_url"]] = idx_digest

    # XML/HTML автодетект
    soup = BeautifulSoup(index_html, "xml" if index_html.lstrip().startswith("<?xml") else "html.parser")

    # Collect candidate links
    links = []
    inc_re = re.compile(src.get("include_regex")) if src.get("include_regex") else None
    exc_re = re.compile(src.get("exclude_regex")) if src.get("exclude_regex") else None
    for a in soup.find_all("a"):
        href = a.get("href")
        if not href:
            continue
        href = urljoin(src["base_url"], href)
        if not any(p in href for p in src["include_patterns"]):
            continue
        if inc_re and not inc_re.search(href):
            continue
        if exc_re and exc_re.search(href):
            continue
        text_ok = (a.get_text(strip=True) or "")
        if len(text_ok) < src.get("link_min_text_len", 0):
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

    new_links = [u for u in uniq if u not in already_seen]
    if not new_links:
        logging.info("  no new links for %s", src["name"])
        return []

    items = []
    for url in new_links:
        try:
            html = fetch_page(url)
            item = build_item(url, src["name"], html)
            if item:
                items.append(item)
        except Exception as e:
            logging.warning("  skip %s: %s", url, e)

    # обновим «виденные» ссылки — держим скользящее окно последних 500
    keep = 500
    # сначала — новые (в порядке обхода), затем часть старых, которые ещё встречаются в uniq
    tail = [u for u in already_seen_list if u in uniq]
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
        # Если в новой записи появилась дата — берём новую
        if it.get("date_published") and not old.get("date_published"):
            by_url[u] = it
            continue
        # В остальных случаях просто обновим старую новой версией (на случай правок заголовка и т. п.)
        by_url[u] = it

    merged = list(by_url.values())

    # Сортировка по дате у нас окончательно произойдёт в build_feed,
    # но слегка подсортируем тут, чтобы ограничение по размеру не «съело» самые новые.
    merged.sort(key=lambda x: x.get("date_published") or "", reverse=True)

    # Обрезка по размеру
    if FEED_MAX_ITEMS and len(merged) > FEED_MAX_ITEMS:
        merged = merged[:FEED_MAX_ITEMS]

    return merged

def main():
    sources = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
    all_items = []
    for src in sources:
        if not src.get('enabled', True):
            logging.info("Skip disabled source: %s — %s", src.get('name'), src.get('start_url'))
            continue
        try:
            items = harvest_source(src)
            logging.info("  -> %d items", len(items))
            all_items.extend(items)
        except Exception as e:
            logging.error("  !! Failed: %s (%s)", src.get("name"), e)
            STATE.setdefault("stats", {}).setdefault("errors", []).append({"source": src.get("name"), "url": src.get("start_url"), "error": str(e)})

    if not all_items:
        # Нет новых карточек — ленту не переписываем, чтобы не обнулять историю
        existing_count = 0
        if OUT_JSON.exists():
            try:
                existing_count = len(json.loads(OUT_JSON.read_text(encoding="utf-8")).get("items", []))
            except Exception:
                existing_count = 0
        STATE.setdefault("stats", {})["last_run"] = datetime.utcnow().isoformat() + "Z"
        STATE["stats"]["items"] = existing_count
        save_state()
        logging.info("No new items -> keep existing %s as-is (%d items)", OUT_JSON, existing_count)
        return

    # Есть новые карточки — сливаем с существующей лентой
    existing_items = load_existing_feed_items()
    merged_raw = merge_items(existing_items, all_items)
    feed = build_feed(merged_raw)

    OUT_JSON.write_text(json.dumps(feed, ensure_ascii=False, indent=2), encoding="utf-8")
    STATE.setdefault("stats", {})["last_run"] = datetime.utcnow().isoformat() + "Z"
    STATE["stats"]["items"] = len(feed["items"])
    save_state()
    logging.info("Saved feed to %s (%d items)", OUT_JSON, len(feed["items"]))

if __name__ == "__main__":
    sys.exit(main())
