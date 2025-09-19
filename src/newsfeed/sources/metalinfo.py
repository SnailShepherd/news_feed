from __future__ import annotations
import re
from ..fetch import Fetcher
from ._common import soup_html, make_item, normalize_url, log
from ..dateparse_ru import parse_ru_date

SOURCE = "Металлоснабжение и сбыт"
LIST_URL = "https://www.metalinfo.ru/ru/news"

ARTICLE_RE = re.compile(r"^https?://www\.metalinfo\.ru/ru/news/\d+$")

def harvest(fetch: Fetcher, limit: int = 60):
    resp, err = fetch.get(LIST_URL)
    if err:
        log(f"ERROR !! Failed: Металлоснабжение и сбыт ({err})")
        return []
    soup = soup_html(resp)
    items = []
    seen = set()
    for a in soup.select("a[href*='/ru/news/']"):
        href = normalize_url(a.get("href"), LIST_URL)
        if not ARTICLE_RE.match(href):
            # отсекаем рубрики, теги и спец-страницы
            continue
        if href in seen: 
            continue
        seen.add(href)
        # карточка ради корректной даты
        resp2, err2 = fetch.get(href)
        dt = None; title = a.get_text(strip=True) or None
        if not err2:
            s2 = soup_html(resp2)
            t = s2.select_one("time[datetime], time, .news-date, .date")
            if t:
                dt = parse_ru_date(t.get("datetime") or t.get_text(strip=True))
            h1 = s2.select_one("h1")
            if h1: title = h1.get_text(strip=True)
        items.append(make_item(SOURCE, href, title or href, dt))
        if len(items) >= limit:
            break
    return items
