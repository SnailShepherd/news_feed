from .base import Source, NewsItem, make_item_id

class ERZRF(Source):
    NAME = "ЕРЗ.РФ"
    URL = "https://erzrf.ru/news/"

    def fetch(self):
        r = self.http.get(self.URL)
        s = self.html.soup_html(r.text)
        out = []
        for a in s.select("a[href*='/news/']"):
            href = a.get("href")
            title = (a.get("title") or a.text or '').strip()
            if not href or not title:
                continue
            if href.startswith("/"):
                href = "https://erzrf.ru" + href
            if "tag=" in href:
                continue
            out.append(NewsItem(
                id=make_item_id(href, title), url=href, title=title,
                date_published=None, content_text=None, tags=[], source=self.NAME
            ))
        uniq=[]; seen=set()
        for it in out:
            if it.url in seen: continue
            seen.add(it.url); uniq.append(it)
        return uniq[:60]
