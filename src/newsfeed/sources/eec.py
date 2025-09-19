from __future__ import annotations
from ..fetch import Fetcher
from ._common import soup_html, soup_xml, make_item, normalize_url, log
from ..dateparse_ru import parse_ru_date

SOURCE = "ЕЭК ЕАЭС"
BASE = "https://eec.eaeunion.org/news/"

def harvest(fetch: Fetcher, limit: int = 30):
    # Пытаемся RSS сначала (как правило доступен без блокировок)
    rss_url = BASE.rstrip("/") + "/?format=feed&type=rss"
    resp, err = fetch.get(rss_url)
    items = []
    if not err and "xml" in (resp.headers.get("content-type","")):
        soup = soup_xml(resp)
        for it in soup.select("item"):
            title = it.select_one("title")
            link = it.select_one("link")
            pub = it.select_one("pubDate")
            if not title or not link: continue
            dt = parse_ru_date(pub.get_text(strip=True) if pub else None)
            items.append(make_item(SOURCE, link.get_text(strip=True), title.get_text(strip=True), dt))
            if len(items) >= limit: break
        if items: 
            return items

    # Фоллбэк по HTML
    resp, err = fetch.get(BASE)
    if err:
        log(f"ERROR !! Failed: ЕЭК ЕАЭС ({err})")
        return []
    soup = soup_html(resp)
    for a in soup.select("a.news-list__item, a.news-card, .news__list a"):
        href = a.get("href")
        title = a.get_text(strip=True)
        if not href or not title: continue
        full = normalize_url(href, BASE)
        dt = None
        t = a.select_one("time, time[datetime], .date")
        if t:
            dt = parse_ru_date(t.get("datetime") or t.get_text(strip=True))
        items.append(make_item(SOURCE, full, title, dt))
        if len(items) >= limit: break
    return items
