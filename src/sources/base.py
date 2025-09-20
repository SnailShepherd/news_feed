from dataclasses import dataclass
from typing import Optional, List
import hashlib

@dataclass
class NewsItem:
    id: str
    url: str
    title: str
    date_published: Optional[str]
    content_text: Optional[str]
    tags: list
    source: str

def make_item_id(url: str, title: str) -> str:
    key = (url or "") + "|" + (title or "")
    return hashlib.sha256(key.encode("utf-8")).hexdigest()

class Source:
    NAME = "Base"
    def __init__(self, http, html, dates):
        self.http = http
        self.html = html
        self.dates = dates
