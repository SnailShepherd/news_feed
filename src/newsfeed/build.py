from __future__ import annotations
import sys, json, pathlib
from datetime import datetime
from dateutil import tz
from .fetch import Fetcher
from .sources import _common
from .sources import (
    notim, minfin, eec, gostinfo, erzrf, interfax_realty, metalinfo, pnp, rg,
    government, stroygaz, ria_realty, faufcc, minstroy, ancb, gge, ria_stk
)

SOURCES = [
    notim,
    minfin,
    eec,
    gostinfo,
    erzrf,
    interfax_realty,
    metalinfo,
    pnp,
    rg,
    government,
    stroygaz,
    ria_realty,
    faufcc,
    minstroy,
    ancb,
    gge,
    ria_stk,
]

def build():
    fetch = Fetcher()
    all_items = []
    print("==== Harvest start ====")
    for mod in SOURCES:
        name = getattr(mod, "SOURCE", mod.__name__)
        url = getattr(mod, "BASE", getattr(mod, "LIST_URL", ""))
        print(f"INFO Harvest: {name} — {url}")
        try:
            items = mod.harvest(fetch)
            print(f"INFO   -> {len(items)} items")
            all_items.extend(items)
        except Exception as e:
            print(f"ERROR   !! Failed: {name} ({e})")
    fetch.close()

    # dedupe by url
    seen = set(); deduped = []
    for it in all_items:
        u = it.get("url")
        if u in seen: 
            continue
        seen.add(u)
        deduped.append(it)

    # sort by date desc; None-дату в конец
    def sort_key(it):
        return (it["date_published"] is not None, it["date_published"] or "")
    deduped.sort(key=sort_key, reverse=True)

    feed = {
        "version": "https://jsonfeed.org/version/1.1",
        "title": "Unified News Feed (normacs)",
        "home_page_url": "https://example.github.io/news_feed/",
        "feed_url": "https://example.github.io/news_feed/unified.json",
        "items": deduped,
    }

    out = pathlib.Path(__file__).resolve().parents[2] / "docs" / "unified.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False, indent=2)
    print(f"INFO Saved feed to {out} ({len(deduped)} items)")

def main():
    build()

if __name__ == "__main__":
    main()
