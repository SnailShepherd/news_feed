from .base import Source, NewsItem, make_item_id
from ..util.rss import parse_rss_bytes

class Government(Source):
    NAME = "Правительство РФ"
    URL = "http://government.ru/rss/news/"

    def fetch(self):
        r = self.http.get(self.URL)
        feed = parse_rss_bytes(r.content)
        out = []
        for e in feed.entries[:50]:
            url = e.get("link"); title = e.get("title")
            dt_iso = None
            if e.get("published_parsed"):
                import datetime as dtmod
                dt = dtmod.datetime(*e.published_parsed[:6]).astimezone()
                dt_iso = dt.isoformat()
            if not url or not title:
                continue
            out.append(NewsItem(
                id=make_item_id(url, title), url=url, title=title,
                date_published=dt_iso, content_text=None, tags=[], source=self.NAME
            ))
        return out
