from .base import Source, NewsItem, make_item_id
from ..util.rss import parse_rss_bytes
import re

ALLOWED = re.compile(r"https?://www\.metalinfo\.ru/ru/news/\d+$")

class MetalInfo(Source):
    NAME = "Металлоснабжение и сбыт"
    RSS = "https://www.metalinfo.ru/ru/news/list.rss"

    def fetch(self):
        resp = self.http.get(self.RSS)
        feed = parse_rss_bytes(resp.content)
        out = []
        for e in feed.entries[:60]:
            url = e.get("link")
            title = e.get("title")
            if not url or not title:
                continue
            if not ALLOWED.match(url):
                # игнорируем теги/комментарии/категории
                continue
            dt = None
            if e.get("published_parsed"):
                import datetime as dtmod
                dt = dtmod.datetime(*e.published_parsed[:6]).astimezone()
                dt_iso = dt.isoformat()
            else:
                dt_iso = None
            out.append(NewsItem(
                id=make_item_id(url, title), url=url, title=title,
                date_published=dt_iso, content_text=None, tags=[], source=self.NAME
            ))
        return out
