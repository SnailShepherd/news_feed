from __future__ import annotations
import re
from ..fetch import Fetcher
from ._common import soup_html, make_item, normalize_url, log
from ..dateparse_ru import parse_ru_date

SOURCE = "Стройгаз.ру"
BASE = "https://stroygaz.ru/news/"
# Статья: /news/<section>/<slug>/
ARTICLE_RE = re.compile(r"^https?://stroygaz\.ru/news/[^/]+/[^/]+/?$")

def harvest(fetch: Fetcher, limit: int = 40):
    resp, err = fetch.get(BASE)
    if err:
        log(f"ERROR !! Failed: Стройгаз.ру ({err})")
        return []
    soup = soup_html(resp)
    items = []
    seen = set()
    for a in soup.select("a[href^='/news/'], a[href*='stroygaz.ru/news/']"):
        href = normalize_url(a.get("href"), BASE)
        if not ARTICLE_RE.match(href):  # отсекаем рубрики вида /news/<section>/
            continue
        if href in seen: 
            continue
        seen.add(href)
        title = a.get_text(strip=True) or None
        # карточка ради даты + точного заголовка
        resp2, err2 = fetch.get(href)
        dt = None
        if not err2:
            s2 = soup_html(resp2)
            t = s2.select_one("time[datetime], time, .date, .article-date")
            if t:
                dt = parse_ru_date(t.get("datetime") or t.get_text(strip=True))
            h1 = s2.select_one("h1")
            if h1:
                title = h1.get_text(strip=True)
        items.append(make_item(SOURCE, href, title or href, dt))
        if len(items) >= limit:
            break
    return items
