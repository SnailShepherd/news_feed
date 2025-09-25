#!/usr/bin/env python3
"""Quick metrics for aggregated feed files."""

from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any, Tuple

try:
    from scripts.url_filters import is_listing_url
except ModuleNotFoundError:  # pragma: no cover - fallback when run as a script
    from url_filters import is_listing_url  # type: ignore


def _load_items(path: pathlib.Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        items = data.get("items", [])
    else:
        items = data
    if not isinstance(items, list):
        raise ValueError("Unexpected feed structure: expected a list of items")
    return [it for it in items if isinstance(it, dict)]


def compute_metrics(items: list[dict[str, Any]]) -> dict[str, int]:
    total = len(items)
    empty_content = 0
    listing_urls = 0
    for item in items:
        content = item.get("content_text")
        if not content or (isinstance(content, str) and not content.strip()):
            empty_content += 1
        if is_listing_url(item.get("url")):
            listing_urls += 1
    return {
        "total": total,
        "empty_content_text": empty_content,
        "listing_urls_count": listing_urls,
    }


def check_anti_genie(baseline: dict[str, int], current: dict[str, int]) -> Tuple[bool, str | None]:
    """Ensure totals do not shrink except for removed listings."""

    allowed_min_total = baseline["total"] - baseline["listing_urls_count"]
    if current["total"] < allowed_min_total:
        message = (
            "total items dropped below baseline minus listings "
            f"({current['total']} < {allowed_min_total})"
        )
        return False, message
    return True, None


def main() -> int:
    parser = argparse.ArgumentParser(description="Compute quick feed metrics")
    parser.add_argument("path", nargs="?", default="docs/unified.json", help="Path to unified feed JSON")
    parser.add_argument(
        "--baseline",
        help="Optional baseline feed JSON to enforce anti-genie rule (total can't drop except filtered listings)",
    )
    args = parser.parse_args()

    path = pathlib.Path(args.path)
    if not path.exists():
        raise SystemExit(f"File not found: {path}")

    items = _load_items(path)
    metrics = compute_metrics(items)
    for key, value in metrics.items():
        print(f"{key}: {value}")

    exit_code = 0

    if args.baseline:
        baseline_path = pathlib.Path(args.baseline)
        if not baseline_path.exists():
            raise SystemExit(f"Baseline file not found: {baseline_path}")
        baseline_items = _load_items(baseline_path)
        baseline_metrics = compute_metrics(baseline_items)
        for key, value in baseline_metrics.items():
            print(f"baseline_{key}: {value}")
        allowed_min = baseline_metrics["total"] - baseline_metrics["listing_urls_count"]
        print(f"allowed_min_total_without_listings: {allowed_min}")
        ok, message = check_anti_genie(baseline_metrics, metrics)
        if not ok and message:
            print(f"anti_genie_violation: {message}")
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
