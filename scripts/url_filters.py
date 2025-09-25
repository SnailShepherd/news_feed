"""Utilities for URL filtering rules shared across scripts."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlparse

_LISTING_SEGMENTS = {"tag", "category", "archive", "search", "page"}
_SECTION_PREFIXES = {
    "news",
    "novosti",
    "lenta",
    "tema",
    "topics",
    "press",
    "press-center",
    "press-tsentr",
}


def _path_segments(path: str) -> list[str]:
    if not path:
        return []
    return [segment for segment in path.split("/") if segment]


def is_listing_url(url: str | None) -> bool:
    """Return True if the URL points to a listing/service page."""

    if not url:
        return False

    parsed = urlparse(url)
    segments = [segment.lower() for segment in _path_segments(parsed.path)]
    if segments:
        last_segment = segments[-1]
        if last_segment == "news":
            return True
        if (
            len(segments) == 2
            and segments[0] in _SECTION_PREFIXES
            and not segments[1].isdigit()
        ):
            return True
    if any(segment in _LISTING_SEGMENTS for segment in segments):
        return True

    query = parsed.query
    if not query:
        return False

    for key, value in parse_qsl(query, keep_blank_values=True):
        if not value or not value.isdigit():
            continue
        if key.lower() == "page":
            return True
        if re.fullmatch(r"PAGEN_\d+", key, re.IGNORECASE):
            return True
        if key.upper() == "VOTE_ID":
            return True

    return False
