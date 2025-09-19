from __future__ import annotations
from bs4 import BeautifulSoup
from datetime import datetime
from ..fetch import Fetcher
from ._common import soup_html, make_item, normalize_url, log
from ..dateparse_ru import parse_ru_date

SOURCE = "НОТИМ"
BASE = "https://notim.ru/"

def harvest(fetch: Fetcher, limit: int = 25):
    url = BASE
    resp, err = fetch.get(url)
    if err: 
        log(f"ERROR !! Failed: НОТИМ ({err})")
        return []
    log("INFO   -> parsing НОТИМ")
    soup = soup_html(resp)
    items = []
    for a in soup.select("a.news-card__title, a.news-card__link, a.card-title, .news-card a"):
        href = a.get("href")
        title = a.get_text(strip=True)
        if not href or not title:
            continue
        full = normalize_url(href, BASE)
        dt = None
        # попробуем дату рядом
        dt_text = None
        parent = a.find_parent()
        if parent:
            d = parent.select_one("time, .news-card__date, .date, time[datetime]")
            if d:
                dt_text = d.get("datetime") or d.get_text(strip=True)
        dt = parse_ru_date(dt_text) if dt_text else None
        items.append(make_item(SOURCE, full, title, dt))
        if len(items) >= limit:
            break
    return items
