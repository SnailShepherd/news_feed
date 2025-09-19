# -*- coding: utf-8 -*-
import os, re, json, time, hashlib
from pathlib import Path
from typing import Iterable
from datetime import datetime, timezone, timedelta
import requests
from bs4 import BeautifulSoup
import feedparser

from date_utils import MSK, parse_ru_date
from sources import SOURCES, Source

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36 (+github-actions news_feed)"
ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
CACHE_ITEMS = ROOT / "cache" / "items"
CACHE_ITEMS.mkdir(parents=True, exist_ok=True)

def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

def log(level: str, msg: str):
    now = datetime.now(tz=MSK).strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
    print(f"{now} {level} {msg}", flush=True)

def http_get(url: str, *, allow_304=False, timeout=30) -> requests.Response | None:
    try:
        headers = {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru,en;q=0.7",
            "Cache-Control": "no-cache",
        }
        resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        if allow_304 and resp.status_code == 304:
            return resp
        resp.raise_for_status()
        return resp
    except requests.RequestException as e:
        log("ERROR", f"!! http_get failed: {url} ({e})")
        return None

def keep_previous_if_needed(src: Source, items: list[dict]) -> list[dict]:
    if items:
        (CACHE_ITEMS / f"{src.slug}.json").write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        return items

    cache_file = CACHE_ITEMS / f"{src.slug}.json"
    if cache_file.exists():
        log("INFO", f"Using cached items for {src.name} ({src.slug})")
        return json.loads(cache_file.read_text(encoding="utf-8"))
    return []

def normalize_published(dt_val, fallback_now=True):
    if isinstance(dt_val, datetime):
        dt = dt_val
    elif isinstance(dt_val, str):
        dt = parse_ru_date(dt_val) or None
    else:
        dt = None

    if dt is None and fallback_now:
        dt = datetime.now(tz=MSK)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MSK)
    return dt.astimezone(MSK).isoformat()

def harvest_rss(src: Source) -> list[dict]:
    log("INFO", f"Harvest (RSS): {src.name} — {src.url}")
    try:
        parsed = feedparser.parse(src.url)
    except Exception as e:
        log("ERROR", f"!! RSS parse failed: {src.name} ({e})")
        return keep_previous_if_needed(src, [])

    items = []
    for e in parsed.entries[:100]:
        link = e.get("link")
        if src.allowed_url_regex and (not src.allowed_url_regex.search(link or "")):
            continue

        title = e.get("title")
        pub = None
        if "published_parsed" in e and e.published_parsed:
            try:
                pub = datetime(*e.published_parsed[:6], tzinfo=MSK)
            except Exception:
                pub = None
        pub = normalize_published(pub)

        item = {
            "id": hashlib.sha256((link or title or "").encode("utf-8")).hexdigest(),
            "url": link,
            "title": title,
            "date_published": pub,
            "content_text": None,
            "tags": [],
            "source": src.name,
        }
        items.append(item)

    return keep_previous_if_needed(src, items)

def extract_links(soup: BeautifulSoup) -> list[str]:
    hrefs = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            pass
        hrefs.append(href)
    return list(dict.fromkeys(hrefs))

def pick_site_links(src: Source, soup: BeautifulSoup) -> list[str]:
    domain_rules = {
        "notim": r"^https?://notim\.ru/[^#?]+$",
        "minstroyrf": r"^https?://minstroyrf\.gov\.ru/press/\d+",
        "ardexpert": r"^https?://ardexpert\.ru/\d+",
        "gostinfo": r"^https?://www\.gostinfo\.ru/News/[^/]+/\d+",
        "faufcc": r"^https?://faufcc\.ru/press-tsentr/novosti/[^/]+/",
        "ancb": r"^https?://ancb\.ru/publication/\d+",
        "eec": r"^https?://eec\.eaeunion\.org/news/[^/]+/",
        "minfin": r"^https?://minfin\.gov\.ru/ru/press-center/\?id_\d*=",
        "interfax_realty": r"^https?://www\.interfax-russia\.ru/realty/novosti/[^#?]+$",
        "pnp": r"^https?://www\.pnp\.ru/economics/[^/]+\.html$",
        "erzrf": r"^https?://erzrf\.ru/news/\d+",
        "government": r"^http://government\.ru/news/\d+/",
        "stroygaz": r"^https?://stroygaz\.ru/news/[^/]+/",
        "ria_realty": r"^https?://realty\.ria\.ru/\d{4}/\d{2}/\d{2}/\d+\.html$",
        "rg": r"^https?://rg\.ru/\d{4}/\d{2}/\d{2}/[^/]+\.html$",
    }
    pat = re.compile(domain_rules.get(src.slug, r"^https?://"))
    links = [h for h in extract_links(soup) if pat.search(h or "")]
    seen = set()
    keep = []
    for l in links:
        if l not in seen:
            keep.append(l)
            seen.add(l)
    return keep[:60]

def try_extract_date_from_meta(soup: BeautifulSoup) -> str | None:
    meta_names = [
        ("meta", {"property": "article:published_time"}),
        ("meta", {"name": "article:published_time"}),
        ("meta", {"name": "pubdate"}),
        ("meta", {"itemprop": "datePublished"}),
        ("meta", {"name": "date"}),
        ("time", {"datetime": True}),
    ]
    for tag, attrs in meta_names:
        for node in soup.find_all(tag):
            if tag == "time" and node.get("datetime"):
                return node["datetime"]
            if isinstance(attrs, dict) and all((k in node.attrs and (attrs[k] is True or node.get(k)==attrs[k]) for k in attrs)):
                val = node.get("content") or node.get("value")
                if val:
                    return val

    txt = soup.get_text(" ", strip=True)
    m = re.search(r"(\d{1,2}\.\d{1,2}\.\d{4}(?:\s+\d{1,2}:\d{2})?)", txt)
    if m:
        return m.group(1)
    m = re.search(r"(\d{1,2}\s+[А-Яа-яё]+\s+\d{4}(?:\s*,?\s*\d{1,2}:\d{2})?)", txt)
    if m:
        return m.group(1)
    return None

def harvest_html(src: Source) -> list[dict]:
    log("INFO", f"Harvest (HTML): {src.name} — {src.url}")
    resp = http_get(src.url)
    if not resp:
        return keep_previous_if_needed(src, [])

    soup = BeautifulSoup(resp.content, "lxml")
    links = pick_site_links(src, soup)

    items = []
    for link in links:
        pub_iso = None
        title = None
        if src.follow_detail_for_date:
            time.sleep(1.0)
            r2 = http_get(link, timeout=40)
            if not r2:
                continue
            s2 = BeautifulSoup(r2.content, "lxml")
            t = s2.find("h1")
            title = t.get_text(strip=True) if t else (s2.title.get_text(strip=True) if s2.title else link)
            d = try_extract_date_from_meta(s2)
            pub_iso = normalize_published(d, fallback_now=True)
        else:
            a = soup.find("a", href=lambda x: x and x in link)
            if a and a.get_text(strip=True):
                title = a.get_text(strip=True)
            else:
                title = link
            pub_iso = normalize_published(None, fallback_now=True)

        if not title:
            title = link

        if src.slug == "metalinfo":
            if not re.search(r"/ru/news/\d+$", link):
                continue

        item = {
            "id": hashlib.sha256((link or title or "").encode("utf-8")).hexdigest(),
            "url": link,
            "title": title,
            "date_published": pub_iso,
            "content_text": None,
            "tags": [],
            "source": src.name,
        }
        items.append(item)

    return keep_previous_if_needed(src, items)

def harvest_source(src: Source) -> list[dict]:
    if src.mode == "rss":
        return harvest_rss(src)
    if src.mode == "html":
        return harvest_html(src)
    if src.mode == "manual":
        return keep_previous_if_needed(src, [])
    return []

def build():
    all_items = []
    for src in SOURCES:
        items = harvest_source(src)
        log("INFO", f"  -> {len(items)} items ({src.name})")
        all_items.extend(items)

    seen = set()
    uniq = []
    for it in all_items:
        u = it.get("url")
        if u and u not in seen:
            uniq.append(it)
            seen.add(u)

    def key_dt(it):
        try:
            return datetime.fromisoformat(it["date_published"])
        except Exception:
            return datetime.now(tz=MSK)
    uniq.sort(key=key_dt, reverse=True)

    feed = {
        "version": "https://jsonfeed.org/version/1.1",
        "title": "Unified feed",
        "home_page_url": "https://snailshepherd.github.io/news_feed/",
        "feed_url": "https://snailshepherd.github.io/news_feed/unified.json",
        "items": uniq,
    }
    out = DOCS / "unified.json"
    out.write_text(json.dumps(feed, ensure_ascii=False, indent=2), encoding="utf-8")
    log("INFO", f"Saved feed to {out} ({len(uniq)} items)")

if __name__ == "__main__":
    build()
