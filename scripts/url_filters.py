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

_GENERIC_SECTION_PATHS = {"/press", "/press/", "/news-list", "/news-list/"}
_GENERIC_QUERY_KEYS = {
    "page",
    "pagen_1",
    "per-page",
    "tag",
    "category",
    "year",
    "month",
    "sort",
    "filter",
}

_HOST_ALLOW_LIST = {
    "notim.ru": (re.compile(r"^/news/[^/?#]+/?$"),),
    "www.notim.ru": (re.compile(r"^/news/[^/?#]+/?$"),),
    "eec.eaeunion.org": (re.compile(r"^/news/[^/?#]+/?$"),),
    "www.eec.eaeunion.org": (re.compile(r"^/news/[^/?#]+/?$"),),
    "erzrf.ru": (re.compile(r"^/news/[^/?#]+/?$"),),
    "www.erzrf.ru": (re.compile(r"^/news/[^/?#]+/?$"),),
}

_HOST_LISTING_HUBS = {
    "eec.eaeunion.org": (re.compile(r"^/news/(speech|events|video-gallery|photo-gallery|broadcasts)/?$"),),
    "www.eec.eaeunion.org": (re.compile(r"^/news/(speech|events|video-gallery|photo-gallery|broadcasts)/?$"),),
    "ria-stk.ru": (re.compile(r"^/news/vse-novosti\.php$"),),
    "www.ria-stk.ru": (re.compile(r"^/news/vse-novosti\.php$"),),
}


def _path_segments(path: str) -> list[str]:
    if not path:
        return []
    return [segment for segment in path.split("/") if segment]


def _normalize_host(host: str) -> str:
    host = host.lower()
    if host.startswith("www."):
        return host[4:]
    return host


def is_listing_url(url: str | None) -> bool:
    """Return True if the URL points to a listing/service page."""

    if not url:
        return False

    parsed = urlparse(url)
    host = parsed.netloc.lower()
    host_no_www = _normalize_host(host)
    path = parsed.path or ""
    normalized_path = path.rstrip("/") or "/"
    lowered_path = path.lower()
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)

    hub_patterns = _HOST_LISTING_HUBS.get(host) or _HOST_LISTING_HUBS.get(host_no_www)
    hub_match = False
    if hub_patterns:
        for pattern in hub_patterns:
            if pattern.match(lowered_path):
                hub_match = True
                break

    # Host allow-lists: treat matched URLs as articles regardless of other heuristics.
    allow_patterns = _HOST_ALLOW_LIST.get(host) or _HOST_ALLOW_LIST.get(host_no_www)
    if allow_patterns:
        for pattern in allow_patterns:
            if pattern.match(path):
                if hub_match:
                    break
                return False
    if host_no_www == "ria-stk.ru" and re.match(r"^/news/index\.php$", path, re.IGNORECASE):
        if any(k.lower() == "element_id" and v for k, v in query_pairs):
            return False

    # Explicit listing hubs that should always be filtered.
    if normalized_path in {"/news"}:
        return True
    if hub_match:
        return True

    # Generic listing signals.
    if "/page/" in lowered_path:
        return True
    if normalized_path in _GENERIC_SECTION_PATHS:
        return True

    query_keys_lower = {k.lower() for k, _ in query_pairs}
    if query_keys_lower & _GENERIC_QUERY_KEYS:
        return True
    if any(k.upper().startswith("PAGEN_") for k, _ in query_pairs):
        return True

    segments = [segment.lower() for segment in _path_segments(path)]
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

    if not query_pairs:
        return False

    for key, value in query_pairs:
        if not value or not value.isdigit():
            continue
        if key.lower() == "page":
            return True
        if re.fullmatch(r"PAGEN_\d+", key, re.IGNORECASE):
            return True
        if key.upper() == "VOTE_ID":
            return True

    return False
