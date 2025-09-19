
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, re, json, time, hashlib, logging, pathlib, sys
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dparser
import pytz

# ---- Settings ----
ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCS_DIR = ROOT / "docs"
CACHE_DIR = ROOT / ".cache"
PAGES_DIR = CACHE_DIR / "pages"
STATE_PATH = CACHE_DIR / "state.json"
SOURCES_PATH = ROOT / "sources.json"
OUT_JSON = DOCS_DIR / "unified.json"
USER_AGENT = "normacs-feed-bot/1.0 (+https://www.normacs.info)"
REQUEST_TIMEOUT = 30
SLEEP_BETWEEN_REQUESTS = 2.0  # seconds

MOSCOW = pytz.timezone("Europe/Moscow")

RU_MONTHS = {
    "января":1,"февраля":2,"марта":3,"апреля":4,"мая":5,"июня":6,
    "июля":7,"августа":8,"сентября":9,"октября":10,"ноября":11,"декабря":12
}

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

for d in [DOCS_DIR, CACHE_DIR, PAGES_DIR]:
    d.mkdir(parents=True, exist_ok=True)

def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logging.warning("Failed to read %s, using default", path)
    return default

STATE = load_json(STATE_PATH, {"headers": {}, "stats": {}})

def save_state():
    STATE_PATH.write_text(json.dumps(STATE, ensure_ascii=False, indent=2), encoding="utf-8")

def msk_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = MOSCOW.localize(dt)
    return dt.astimezone(MOSCOW).isoformat()

def parse_date_ru(text: str):
    t = re.sub(r"\s+", " ", text.strip().lower())
    # dd.mm.yyyy [hh:mm]
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})(?:\s+(\d{1,2}):(\d{2}))?", t)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh = int(m.group(4)) if m.group(4) else 0
        mm = int(m.group(5)) if m.group(5) else 0
        return MOSCOW.localize(datetime(y, mo, d, hh, mm))
    # '18 сентября 2025 [hh:mm]'
    m = re.search(r"(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\s+(\d{4})(?:\s+(\d{1,2}):(\d{2}))?", t)
    if m:
        d = int(m.group(1)); mo = RU_MONTHS[m.group(2)]; y = int(m.group(3))
        hh = int(m.group(4)) if m.group(4) else 0
        mm = int(m.group(5)) if m.group(5) else 0
        return MOSCOW.localize(datetime(y, mo, d, hh, mm))
    # fallback: ISO-looking
    try:
        dt = dparser.parse(text, dayfirst=True, fuzzy=True)
        if dt:
            return MOSCOW.localize(dt) if dt.tzinfo is None else dt.astimezone(MOSCOW)
    except Exception:
        pass
    return None

def nearest_date_for_anchor(a):
    # Walk up and around the anchor to find a nearby date string
    for parent in [a, a.parent, a.parent.parent if a.parent else None]:
        if not parent: 
            continue
        txt = parent.get_text(" ", strip=True)
        dt = parse_date_ru(txt)
        if dt: return dt
        sibs = list(parent.previous_siblings)[-3:] + list(parent.next_siblings)[:3]
        for sib in sibs:
            if hasattr(sib, "get_text"):
                dt = parse_date_ru(sib.get_text(" ", strip=True))
                if dt: return dt
            elif isinstance(sib, str):
                dt = parse_date_ru(sib)
                if dt: return dt
    return None

def http_get(url: str):
    # Conditional GET
    hdrs = {"User-Agent": USER_AGENT}
    hinfo = STATE["headers"].get(url, {})
    if "ETag" in hinfo:
        hdrs["If-None-Match"] = hinfo["ETag"]
    if "Last-Modified" in hinfo:
        hdrs["If-Modified-Since"] = hinfo["Last-Modified"]

    resp = requests.get(url, headers=hdrs, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    if resp.status_code == 304:
        logging.info("304 Not Modified: %s", url)
        return None, hinfo  # indicate to reuse cached file
    resp.raise_for_status()
    # Save headers for next time
    new_hinfo = {}
    if et := resp.headers.get("ETag"):
        new_hinfo["ETag"] = et
    if lm := resp.headers.get("Last-Modified"):
        new_hinfo["Last-Modified"] = lm
    STATE["headers"][url] = new_hinfo
    return resp.text, new_hinfo

def cache_key_for(url: str) -> str:
    p = urlparse(url)
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", (p.path or "/")).strip("-")
    if not slug: slug = "index"
    return f"{p.netloc}{('-' + slug) if slug else ''}.html"

def fetch_page(url: str) -> str:
    page_path = PAGES_DIR / cache_key_for(url)
    content, _ = http_get(url)
    if content is None:
        # use cache
        if page_path.exists():
            return page_path.read_text(encoding="utf-8", errors="ignore")
        else:
            # no cache; force fetch
            content, _ = http_get(url)
    page_path.write_text(content, encoding="utf-8")
    # politeness: sleep
    time.sleep(SLEEP_BETWEEN_REQUESTS)
    return content

def extract_items_from_listing(listing_html: str, base_url: str, include_patterns, link_min_text_len=20):
    soup = BeautifulSoup(listing_html, "lxml")
    items = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(" ", strip=True)
        if not text or len(text) < link_min_text_len:
            continue
        if include_patterns and not any(pat in href for pat in include_patterns):
            continue
        abs_url = urljoin(base_url, href)
        key = (text, abs_url)
        if key in seen:
            continue
        seen.add(key)
        dt = nearest_date_for_anchor(a)
        items.append({
            "title": text,
            "url": abs_url,
            "published_msk": msk_iso(dt) if dt else None,
            "source": base_url
        })
    return items

def make_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()

def build_feed(all_items):
    # Deduplicate by URL, keep first (usually newest in listing)
    seen = set()
    uniq = []
    for it in all_items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        uniq.append(it)
    feed = {
        "version": "https://jsonfeed.org/version/1.1",
        "title": "Unified news feed (normacs sources)",
        "home_page_url": "https://www.normacs.info",
        "feed_url": "https://example.com/unified.json",
        "items": []
    }
    for it in uniq:
        feed["items"].append({
            "id": make_id(it["url"]),
            "url": it["url"],
            "title": it["title"],
            "date_published": it["published_msk"],
            "content_text": "",
            "tags": [],
            "source": it["source"]
        })
    return feed

def main():
    sources = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
    all_items = []
    for s in sources:
        name = s.get("name")
        base = s["base_url"]
        url = s["start_url"]
        pats = s.get("include_patterns", [])
        minlen = s.get("link_min_text_len", 20)
        logging.info("Fetching [%s] %s", name, url)
        try:
            html = fetch_page(url)
            items = extract_items_from_listing(html, base, pats, minlen)
            logging.info("  -> %d items", len(items))
            all_items.extend(items)
        except Exception as e:
            logging.error("  !! Failed: %s", e)
            STATE["stats"].setdefault("errors", []).append({"source": name, "url": url, "error": str(e)})
    feed = build_feed(all_items)
    OUT_JSON.write_text(json.dumps(feed, ensure_ascii=False, indent=2), encoding="utf-8")
    STATE["stats"]["last_run"] = datetime.utcnow().isoformat() + "Z"
    STATE["stats"]["items"] = len(feed["items"])
    save_state()
    logging.info("Saved feed to %s (%d items)", OUT_JSON, len(feed["items"]))

if __name__ == "__main__":
    sys.exit(main())
