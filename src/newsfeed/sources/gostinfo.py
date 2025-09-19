from __future__ import annotations
from ..fetch import Fetcher
from ._common import soup_html, make_item, normalize_url, log
from ..dateparse_ru import parse_ru_date

SOURCE = "Гостинформ"
BASE = "https://www.gostinfo.ru/News/List"

def harvest(fetch: Fetcher, limit: int = 30):
    resp, err = fetch.get(BASE)
    if err:
        log(f"ERROR !! Failed: Гостинформ ({err})")
        return []
    soup = soup_html(resp)
    items = []
    for row in soup.select("a[href*='/News/Details/']"):
        href = row.get("href")
        full = normalize_url(href, BASE)
        # Заголовок и дата — из карточки
        resp2, err2 = fetch.get(full)
        if err2: 
            continue
        s2 = soup_html(resp2)
        title = s2.select_one("h1, .detail-title, .content h1")
        t = s2.select_one("time[datetime], .date, .news-date, .detail-date")
        title = (title.get_text(strip=True) if title else s2.title.get_text(strip=True))
        dt = parse_ru_date(t.get("datetime") or t.get_text(strip=True) if t else None)
        items.append(make_item(SOURCE, full, title, dt))
        if len(items) >= limit: break
    return items
