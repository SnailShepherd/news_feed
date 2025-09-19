from __future__ import annotations
from ..fetch import Fetcher
from ._common import soup_html, make_item, normalize_url, log
from ..dateparse_ru import parse_ru_date

SOURCE = "Главгосэкспертиза"
BASE = "https://gge.ru/press-center/news/"

def harvest(fetch: Fetcher, limit: int = 25):
    resp, err = fetch.get(BASE)
    if err:
        log(f"ERROR !! Failed: Главгосэкспертиза ({err})")
        return []
    soup = soup_html(resp)
    items = []
    for a in soup.select("a[href*='/press-center/news/']"):
        href = a.get("href"); title = a.get_text(strip=True)
        if not href or not title: continue
        full = normalize_url(href, BASE)
        # карточка ради даты
        resp2, err2 = fetch.get(full)
        dt = None
        if not err2:
            s2 = soup_html(resp2)
            t = s2.select_one("time[datetime], time, .date")
            if t:
                dt = parse_ru_date(t.get("datetime") or t.get_text(strip=True))
        items.append(make_item(SOURCE, full, title, dt))
        if len(items) >= limit: break
    return items
