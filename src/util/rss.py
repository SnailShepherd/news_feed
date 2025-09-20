import feedparser, chardet
def parse_rss_bytes(content: bytes):
    enc = chardet.detect(content).get("encoding") or "utf-8"
    text = content.decode(enc, errors="replace")
    return feedparser.parse(text)
