import argparse, logging, json, time, yaml, sys
from dataclasses import dataclass, asdict
from typing import List, Dict, Any
from urllib.parse import urlparse
import requests
import feedparser
from bs4 import BeautifulSoup
from dateutil import tz
import dateparser
from .utils.http import build_session, RateLimiter
from .utils.time import now_msk, parse_window_start
from .adapters.generic import discover_feed, extract_links

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("newsfeed")

MSK = tz.gettz("Europe/Moscow")

@dataclass
class Card:
    title: str
    url: str
    published: str | None
    source: str
    labels: List[str]
    summary: str | None

def parse_date(text):
    if not text:
        return None
    dt = dateparser.parse(text, languages=["ru", "en"], settings={"TIMEZONE":"Europe/Moscow","TO_TIMEZONE":"Europe/Moscow"})
    if not dt:
        return None
    return dt.replace(tzinfo=MSK).strftime("%Y-%m-%d %H:%M")

def clamp_window(published_str, window_start):
    if not window_start or not published_str:
        return True
    try:
        from datetime import datetime
        dt = datetime.strptime(published_str, "%Y-%m-%d %H:%M")
        dt = dt.replace(tzinfo=MSK)
        return dt >= window_start
    except Exception:
        return True

def load_sources(path="data/sources.yaml"):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def canonical_url(u):
    if not isinstance(u,str):
        return ""
    u = u.strip()
    if "#" in u: u = u.split("#",1)[0]
    while u.endswith("/"):
        u = u[:-1]
    return u

def build(window_start: str | None, out_path: str):
    window_dt = parse_window_start(window_start) or now_msk()
    log.info("WINDOW_START(MSK): %s", window_dt.strftime("%Y-%m-%d %H:%M"))
    sess = build_session()

    # Кэш на время выполнения (SQLite в файле .cache.sqlite)
    try:
        import requests_cache
        requests_cache.install_cache(".cache", backend="sqlite", expire_after=1800)
    except Exception:
        pass
    limiter = RateLimiter(default_delay=2.0)

    src_cfg = load_sources()
    domain_delays = {}
    for src in src_cfg:
        delay = src.get("rate_limit_sec", 2)
        dom = urlparse(src["url"]).netloc.lower()
        domain_delays[dom] = max(domain_delays.get(dom, 0), delay)
    for d,sec in domain_delays.items():
        limiter.set_delay(d, sec)

    cards: List[Card] = []
    manual_queue: List[Dict[str, Any]] = []
    seen = set()

    for s in src_cfg:
        name = s["name"]
        url = s["url"]
        stype = s.get("type","html")
        follow = bool(s.get("follow_links", False))
        max_items = int(s.get("max_items", 30))
        deny_paths = s.get("deny_paths") or []

        if stype == "manual":
            log.warning("MANUAL: %s — %s", name, url)
            manual_queue.append({"name": name, "url": url, "reason": s.get("notes","manual")})
            continue

        try:
            limiter.wait(url)
            r = sess.get(url, timeout=30)
            status = r.status_code
            if status == 401 or status == 403:
                log.error("ACCESS DENIED %s (%s)", name, status)
                manual_queue.append({"name": name, "url": url, "reason": f"{status} access denied"})
                continue
            if status >= 400:
                log.error("HTTP %s %s", status, url)
                if status == 429:
                    time.sleep(10)
                continue

            text = r.text or ""
            # Пытаемся найти RSS
            feed_url = discover_feed(text, url)
            if stype == "rss" and feed_url:
                items = []
                limiter.wait(feed_url)
                fp = feedparser.parse(feed_url)
                for e in fp.entries[:max_items]:
                    link = e.get("link") or ""
                    title = e.get("title") or ""
                    pub = e.get("published") or e.get("updated") or ""
                    pub = parse_date(pub)
                    if not link or not title:
                        continue
                    if not clamp_window(pub, window_dt):
                        continue
                    cu = canonical_url(link)
                    if cu in seen: continue
                    seen.add(cu)
                    items.append(Card(title=title, url=cu, published=pub, source=name, labels=[], summary=None))
                cards.extend(items)
            else:

                # HTML-режим
                items = extract_links(text, url, deny_paths=deny_paths, max_items=max_items)
                prepared: List[Card] = []

                # Обогащаем датой публикации по лёгким признакам (без deep-crawl)
                def tiny_date_from_html(ht):
                    soup = BeautifulSoup(ht, "lxml")
                    # метатеги (OpenGraph / schema)
                    meta = soup.select_one('meta[property="article:published_time"], meta[name="article:published_time"], meta[itemprop="datePublished"], meta[name="pubdate"], meta[name="PublishDate"], meta[name="date"]')
                    if meta and meta.get("content"):
                        return parse_date(meta.get("content"))
                    # теги time
                    tm = soup.select_one("time[datetime]")
                    if tm and tm.get("datetime"):
                        return parse_date(tm.get("datetime"))
                    # видимые элементы с датой
                    for sel in [".date", ".news-date", ".published", "[class*=date]"]:
                        el = soup.select_one(sel)
                        if el and el.get_text(strip=True):
                            return parse_date(el.get_text(strip=True))
                    return None

                for it in items:
                    cu = canonical_url(it["url"])
                    if cu in seen: continue
                    seen.add(cu)
                    pub = None
                    # Лёгкая попытка получить дату со страницы статьи (ограничение: 8 штук на источник)
                    try:
                        if len(prepared) < 8:
                            limiter.wait(cu)
                            rr = sess.get(cu, timeout=20)
                            if rr.status_code < 400 and ("text/html" in rr.headers.get("Content-Type","")):
                                pub = tiny_date_from_html(rr.text)
                    except Exception:
                        pass

                    prepared.append(Card(
                        title=it["title"],
                        url=cu,
                        published=pub,
                        source=name,
                        labels=[],
                        summary=None
                    ))
                cards.extend(prepared)

        except requests.exceptions.RequestException as e:
            msg = str(e)
            if "429" in msg or "Too Many Requests" in msg:
                log.warning("429 from %s — backing off", url)
                time.sleep(15)
            else:
                log.error("ERROR %s — %s", url, e)
            manual_queue.append({"name": name, "url": url, "reason": "network error"})
            continue

    # write outputs
    out_dir = out_path.rsplit("/",1)[0]
    import os, pathlib, json
    pathlib.Path(out_dir).mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump([asdict(c) for c in cards], f, ensure_ascii=False, indent=2)

    with open("docs/manual_queue.json", "w", encoding="utf-8") as f:
        json.dump(manual_queue, f, ensure_ascii=False, indent=2)

    log.info("Built %s cards; manual: %s", len(cards), len(manual_queue))

def main():
    p = argparse.ArgumentParser()
    p.add_argument("cmd", nargs="?", default="build")
    p.add_argument("--window-start", default=None, help="Напр.: '17.09.2025 00:00 MSK'")
    p.add_argument("--out", default="docs/unified.json")
    args = p.parse_args()
    if args.cmd == "build":
        build(args.window_start, args.out)
    else:
        print("Unknown cmd")

if __name__ == "__main__":
    main()
