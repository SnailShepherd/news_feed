from __future__ import annotations
import re
from ..fetch import Fetcher
from ._common import soup_html, make_item, normalize_url, log
from ..dateparse_ru import parse_ru_date

SOURCE = "Правительство РФ"
BASE = "http://government.ru/news/"

ARTICLE_RE = re.compile(r"^http://government\.ru/news/\d+/?$")

def harvest(fetch: Fetcher, limit: int = 30):
    resp, err = fetch.get(BASE)
    if err:
        log(f"ERROR !! Failed: Правительство РФ ({err})")
        return []
    soup = soup_html(resp)
    items = []
    for a in soup.select("a[href*='/news/']"):
        href = normalize_url(a.get("href"), BASE)
        if not ARTICLE_RE.match(href):  # отсекаем списки ?page=
            continue
        title = a.get_text(strip=True)
        if not title: continue
        # карточка
        resp2, err2 = fetch.get(href)
        dt = None
        if not err2:
            s2 = soup_html(resp2)
            t = s2.select_one("time[datetime], time, .pubtime, .date")
            if t:
                dt = parse_ru_date(t.get("datetime") or t.get_text(strip=True))
        items.append(make_item(SOURCE, href, title, dt))
        if len(items) >= limit: break
    return items
