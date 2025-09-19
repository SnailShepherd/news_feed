from __future__ import annotations
from ..fetch import Fetcher
from ._common import soup_html, make_item, normalize_url, log
from ..dateparse_ru import parse_ru_date

SOURCE = "Минфин России"
BASE = "https://minfin.gov.ru/ru/press-center/"

def harvest(fetch: Fetcher, limit: int = 40):
    resp, err = fetch.get(BASE)
    if err:
        log(f"ERROR !! Failed: Минфин России ({err})")
        return []
    soup = soup_html(resp)
    items = []
    for card in soup.select("a.card-news__title, a.card-news__link, .news-list a"):
        href = card.get("href")
        title = card.get_text(strip=True)
        if not href or not title: 
            continue
        full = normalize_url(href, BASE)
        # открыть карточку ради корректной даты
        resp2, err2 = fetch.get(full)
        dt = None
        if not err2:
            s2 = soup_html(resp2)
            dt_text = None
            t = s2.select_one("time[datetime], time, .article-date, .date")
            if t:
                dt_text = t.get("datetime") or t.get_text(strip=True)
            dt = parse_ru_date(dt_text) if dt_text else None
        items.append(make_item(SOURCE, full, title, dt))
        if len(items) >= limit: break
    return items
