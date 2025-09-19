from __future__ import annotations
from bs4 import BeautifulSoup
from typing import List, Dict, Optional, Tuple
from datetime import datetime
from ..fetch import Fetcher
from ..dateparse_ru import parse_ru_date, to_msk
import hashlib, re, sys

def soup_html(resp) -> BeautifulSoup:
    return BeautifulSoup(resp.text, "lxml")

def soup_xml(resp) -> BeautifulSoup:
    return BeautifulSoup(resp.text, "lxml-xml")

def make_item(source: str, url: str, title: str, dt: Optional[datetime], tags=None, content=None):
    h = hashlib.sha256((url or title).encode("utf-8")).hexdigest()
    return {
        "id": h,
        "url": url,
        "title": title,
        "date_published": (dt.isoformat() if dt else None),
        "content_text": content,
        "tags": tags or [],
        "source": source,
    }

def normalize_url(url: str, base: str) -> str:
    if url.startswith("http"): return url
    if url.startswith("//"): return "https:" + url
    if url.startswith("/"):
        # extract scheme+host from base
        m = re.match(r"^(https?://[^/]+)", base)
        if m: return m.group(1) + url
    return base.rstrip("/") + "/" + url.lstrip("/")

def log(msg: str):
    print(msg, file=sys.stderr)

# Примечания по сложным сайтам:
# - minstroyrf.gov.ru и gge.ru часто блокируют небраузерные User-Agent'ы → в Fetcher заложены хэдэры и ретраи.
# - metalinfo.ru ограничивает частоту → не запрашиваем десятки теговых страниц; только список + ограниченное число карточек.
# - erzrf.ru, notim.ru, gostinfo.ru, minfin.gov.ru, eec.eaeunion.org — корректные селекторы для дат/заголовков.
# - government.ru — отбрасываем страницы вида /news?page=... и берём только /news/<id>/
# - stroygaz.ru — игнорируем рубрики вида /news/<section>/, берём /news/<section>/<slug>/
