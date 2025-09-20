import json, logging
from pathlib import Path
import yaml

from .util.http import HttpClient
from .util import html as html_util
from .util import dates as dates_util

from .sources.base import NewsItem
from .sources.metalinfo import MetalInfo
from .sources.pnp import PNP
from .sources.ria_realty import RiaRealty
from .sources.stroygaz import StroyGaz
from .sources.government import Government
from .sources.generic_html import GenericList
from .sources.interfax_realty import InterfaxRealty
from .sources.erzrf import ERZRF

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("build")

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
CONFIG = ROOT / "sources.yaml"

def to_jsonfeed(items):
    return {
        "version": "https://jsonfeed.org/version/1.1",
        "title": "Unified feed",
        "home_page_url": "",
        "feed_url": "",
        "items": [{
            "id": it.id,
            "url": it.url,
            "title": it.title,
            "date_published": it.date_published,
            "content_text": it.content_text,
            "tags": it.tags,
            "source": it.source,
        } for it in items]
    }

def load_sources(http):
    with open(CONFIG, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    sources = []
    for c in cfg:
        state = c.get("state","active")
        if state in ("disabled","manual"):
            log.warning("Skip %s — state=%s", c["name"], state)
            continue
        if c["id"]=="metalinfo":
            sources.append(MetalInfo(http, html_util, dates_util))
        elif c["id"]=="pnp":
            sources.append(PNP(http, html_util, dates_util))
        elif c["id"]=="ria_realty":
            sources.append(RiaRealty(http, html_util, dates_util))
        elif c["id"]=="stroygaz":
            sources.append(StroyGaz(http, html_util, dates_util))
        elif c["id"]=="government":
            sources.append(Government(http, html_util, dates_util))
        elif c["id"]=="interfax_realty":
            sources.append(InterfaxRealty(http, html_util, dates_util))
        elif c["id"]=="erzrf":
            sources.append(ERZRF(http, html_util, dates_util))
        else:
            sources.append(GenericList(http, html_util, dates_util, name=c["name"], url=c["url"]))
    return sources

def main():
    http = HttpClient(min_delay=1.0, per_domain_delay={
        "www.metalinfo.ru": 5.0,
        "metalinfo.ru": 5.0,
    })
    all_items = []
    for src in load_sources(http):
        try:
            log.info("Harvest: %s", src.NAME)
            items = src.fetch()
            log.info("  -> %d items", len(items))
            all_items.extend(items)
        except Exception:
            log.exception("  !! Failed: %s", src.NAME)

    # dedupe by normalized URL
    seen = set(); uniq = []
    for it in all_items:
        key = (it.url or "").split("?")[0].rstrip("/")
        if key in seen: 
            continue
        seen.add(key); uniq.append(it)

    feed = to_jsonfeed(uniq)
    DOCS.mkdir(parents=True, exist_ok=True)
    out = DOCS / "unified.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False, indent=2)
    log.info("Saved feed to %s (%d items)", out, len(uniq))

if __name__ == "__main__":
    main()
