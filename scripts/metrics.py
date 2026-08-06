#!/usr/bin/env python3
"""Quick metrics for aggregated feed files."""

from __future__ import annotations

import argparse
import json
import pathlib
from datetime import datetime, timedelta, timezone
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


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def compute_source_metrics(
    items: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    *,
    stale_after: timedelta,
    now: datetime | None = None,
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """Return aggregate and per-source health for enabled configured sources."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    enabled = [source for source in sources if source.get("enabled", True)]
    report = []
    for source in enabled:
        name = str(source.get("name", ""))
        source_items = [item for item in items if item.get("source") == name]
        timestamps = []
        for item in source_items:
            # Publication time is authoritative; fetch time is its fallback.
            timestamp = _parse_timestamp(item.get("published_at")) or _parse_timestamp(
                item.get("fetched_at")
            )
            if timestamp is not None:
                timestamps.append(timestamp)
        newest = max(timestamps, default=None)
        report.append(
            {
                "source": name,
                "item_count": len(source_items),
                "newest_timestamp": newest.isoformat() if newest else None,
                "stale": newest is not None and newest < now - stale_after,
            }
        )
    totals = {
        "enabled_sources": len(report),
        "sources_with_items": sum(row["item_count"] > 0 for row in report),
        "sources_without_items": sum(row["item_count"] == 0 for row in report),
        "stale_sources": sum(row["stale"] for row in report),
    }
    return totals, report


def find_unexpected_empty_sources(
    source_report: list[dict[str, Any]], allow_empty: set[str]
) -> list[str]:
    """List empty enabled sources that are not explicitly exempted."""
    return [
        str(row["source"])
        for row in source_report
        if row["item_count"] == 0 and row["source"] not in allow_empty
    ]


def _load_source_health(path: pathlib.Path) -> list[dict[str, Any]]:
    """Load the crawl report written by ``aggregate.py``."""
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("sources", []) if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("Unexpected source health structure: expected a sources list")
    return [row for row in rows if isinstance(row, dict)]


def classify_source_health(
    rows: list[dict[str, Any]], *, failure_threshold: int = 3
) -> tuple[list[str], list[str]]:
    """Return hard crawl failures and degraded-but-usable source diagnostics.

    Feed retention is deliberately not used here: the globally bounded feed can
    legitimately contain zero records for a low-volume source.  The crawl report
    instead describes what happened during this particular run.
    """
    failures: list[str] = []
    warnings: list[str] = []
    for row in rows:
        name = str(row.get("source") or "<unnamed>")
        status = str(row.get("index_fetch_status") or "not_attempted")
        error = row.get("last_error")
        streak = int(row.get("consecutive_failures") or 0)
        if status in {"failed", "parser_error", "not_attempted"} and streak >= failure_threshold:
            detail = f" ({error})" if error else ""
            failures.append(f"{name}: {status} for {streak} consecutive runs{detail}")
        elif status in {"failed", "parser_error", "not_attempted"}:
            warnings.append(f"{name}: transient {status} (run {streak}/{failure_threshold})")
        elif status == "cached" or row.get("cached_fallback_used"):
            warnings.append(f"{name}: cached fallback used")
        elif status == "fetched" and int(row.get("raw_link_candidates") or 0) == 0:
            warnings.append(f"{name}: fetched index contained no link candidates")
        future_rejections = int(row.get("future_date_rejections") or 0)
        if future_rejections:
            warnings.append(f"{name}: rejected {future_rejections} future publication dates")
    return failures, warnings


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
    parser.add_argument("--sources", default="sources.json", help="Path to source configuration JSON")
    parser.add_argument("--stale-hours", type=float, default=168, help="Age in hours after which a source is stale")
    parser.add_argument(
        "--allow-empty", action="append", default=[], metavar="SOURCE",
        help="Source exempted when --fail-on-empty-source is enabled (repeat or comma-separated)",
    )
    parser.add_argument(
        "--fail-on-empty-source", action="store_true",
        help="Fail when an enabled source has no retained feed items (unsafe for bounded feeds)",
    )
    parser.add_argument(
        "--source-health", metavar="PATH",
        help="Crawl diagnostics JSON produced by aggregate.py",
    )
    parser.add_argument(
        "--strict-source-health", action="store_true",
        help="Fail on repeated crawl failures reported by --source-health",
    )
    parser.add_argument(
        "--failure-threshold", type=int, default=3,
        help="Consecutive failed crawls required for a hard source-health failure (default: 3)",
    )
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

    sources_path = pathlib.Path(args.sources)
    if not sources_path.exists():
        raise SystemExit(f"Sources file not found: {sources_path}")
    sources = json.loads(sources_path.read_text(encoding="utf-8"))
    if not isinstance(sources, list):
        raise SystemExit("Unexpected sources structure: expected a list")
    source_totals, source_report = compute_source_metrics(
        items, sources, stale_after=timedelta(hours=max(0, args.stale_hours))
    )
    for row in source_report:
        newest = row["newest_timestamp"] or "none"
        print(f"source: {row['source']} | items={row['item_count']} | newest={newest} | stale={str(row['stale']).lower()}")
    for key, value in source_totals.items():
        print(f"{key}: {value}")

    exit_code = 0
    allow_empty = {
        name.strip() for value in args.allow_empty for name in value.split(",") if name.strip()
    }
    unexpected_empty = find_unexpected_empty_sources(source_report, allow_empty)
    if unexpected_empty:
        print(f"unexpected_empty_sources: {', '.join(unexpected_empty)}")
        if args.fail_on_empty_source:
            exit_code = 1

    if args.source_health:
        health_path = pathlib.Path(args.source_health)
        if not health_path.exists():
            raise SystemExit(f"Source health file not found: {health_path}")
        health_rows = _load_source_health(health_path)
        failures, warnings = classify_source_health(
            health_rows, failure_threshold=max(1, args.failure_threshold)
        )
        print(f"crawl_sources_reported: {len(health_rows)}")
        print(f"crawl_failures: {len(failures)}")
        print(f"crawl_warnings: {len(warnings)}")
        for warning in warnings:
            print(f"crawl_warning: {warning}")
        for failure in failures:
            print(f"crawl_failure: {failure}")
        if failures and args.strict_source_health:
            exit_code = 1

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
