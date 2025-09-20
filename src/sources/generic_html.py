from .base import Source, NewsItem, make_item_id

class GenericList(Source):
    def __init__(self, http, html, dates, name, url):
        super().__init__(http, html, dates)
        self.NAME = name
        self.URL = url

    def fetch(self):
        r = self.http.get(self.URL)
        s = self.html.soup_html(r.text)
        out = []
        for a in s.select("a"):
            href = a.get("href")
            title = (a.get("title") or a.text or '').strip()
            if not href or not title:
                continue
            if href.startswith("/"):
                from urllib.parse import urljoin
                href = urljoin(self.URL, href)
            if any(x in href for x in ("?page=", "/tag/", "/List?page=")):
                continue
            if len(title) < 6:
                continue
            out.append(NewsItem(
                id=make_item_id(href, title), url=href, title=title,
                date_published=None, content_text=None, tags=[], source=self.NAME
            ))
        # dedupe
        uniq=[]; seen=set()
        for it in out:
            if it.url in seen: continue
            seen.add(it.url); uniq.append(it)
        return uniq[:50]
