from .base import Source, NewsItem, make_item_id

class RiaRealty(Source):
    NAME = "РИА Недвижимость: лента"
    URL = "https://realty.ria.ru/lenta/"

    def fetch(self):
        r = self.http.get(self.URL)
        s = self.html.soup_html(r.text)
        out = []
        for a in s.select("a.list-item__title"):
            href = a.get("href")
            title = (a.text or "").strip()
            if not href or not title:
                continue
            if href.startswith("/"):
                href = "https://realty.ria.ru" + href
            out.append(NewsItem(
                id=make_item_id(href, title), url=href, title=title,
                date_published=None, content_text=None, tags=[], source=self.NAME
            ))
        return out[:40]
