from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import re

def discover_feed(html, base_url):
    soup = BeautifulSoup(html, "lxml")
    # ищем RSS/Atom
    for link in soup.select('link[rel="alternate"]'):
        t = (link.get("type") or "").lower()
        if "rss" in t or "atom" in t or "xml" in t:
            href = link.get("href")
            if href:
                return urljoin(base_url, href)
    # эвристика
    for path in ("/rss", "/feed", "/rss.xml", "/news/rss", "/news/feed"):
        if base_url.endswith("/"):
            return base_url.rstrip("/") + path
        else:
            return base_url + path
    return None

def extract_links(html, base_url, deny_paths=None, max_items=30):
    soup = BeautifulSoup(html, "lxml")
    deny_paths = deny_paths or []
    items = []
    # эвристический селектор для списков новостей
    for a in soup.select("a[href]"):
        href = a.get("href")
        if not href:
            continue
        url = urljoin(base_url, href)
        p = urlparse(url).path.lower()
        if any(dp for dp in deny_paths if p.startswith(dp)):
            continue
        title = a.get_text(strip=True)
        if not title or len(title) < 5:
            continue
        items.append({"title": title, "url": url})
        if len(items) >= max_items:
            break
    return items
