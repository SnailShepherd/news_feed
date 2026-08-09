#!/usr/bin/env python3
"""Quick metrics for aggregated feed files."""

from __future__ import annotations

import argparse
import json
import os
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
        expected_update_hours = source.get("expected_update_hours")
        freshness_window = stale_after
        if expected_update_hours is not None:
            freshness_window = timedelta(hours=max(0, float(expected_update_hours)))
        retained_freshness_status = (
            "no_data"
            if newest is None
            else "stale" if newest < now - freshness_window else "fresh"
        )
        report.append(
            {
                "source": name,
                "retained_item_count": len(source_items),
                "newest_retained_timestamp": newest.isoformat() if newest else None,
                "retained_content_freshness_status": retained_freshness_status,
                "expected_min_candidates": source.get("expected_min_candidates"),
                "expected_update_hours": expected_update_hours,
                # Bounded feeds are allowed to omit a source unless its
                # configuration explicitly opts in to an emptiness check.
                # Preserve an omitted value so the global CLI check can tell it
                # apart from an explicit per-source exemption.
                "allow_empty": source.get("allow_empty"),
            }
        )
    totals = {
        "enabled_sources": len(report),
        "sources_with_items": sum(row["retained_item_count"] > 0 for row in report),
        "sources_without_items": sum(row["retained_item_count"] == 0 for row in report),
        "stale_sources": sum(
            row["retained_content_freshness_status"] == "stale" for row in report
        ),
        "sources_without_freshness_data": sum(
            row["retained_content_freshness_status"] == "no_data" for row in report
        ),
    }
    return totals, report


def find_unexpected_empty_sources(
    source_report: list[dict[str, Any]],
    allow_empty: set[str],
    *,
    fail_on_all: bool = False,
) -> list[str]:
    """List empty sources selected by per-source or global enforcement."""
    return [
        str(row["source"])
        for row in source_report
        if row["retained_item_count"] == 0
        and row["source"] not in allow_empty
        and (
            row.get("allow_empty") is False
            or (fail_on_all and row.get("allow_empty") is not True)
        )
    ]


def merge_current_crawl_metrics(
    source_report: list[dict[str, Any]],
    health_rows: list[dict[str, Any]],
    *,
    stale_after: timedelta = timedelta(days=7),
    now: datetime | None = None,
) -> None:
    """Attach current-run health without deriving it from retained feed items."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    health_by_source = {str(row.get("source", "")): row for row in health_rows}
    for row in source_report:
        health = health_by_source.get(str(row["source"]))
        count_fields = (
            "raw_link_candidates",
            "accepted_links",
            "attempted_articles",
            "accepted_articles",
            "consecutive_fetch_failures",
            "consecutive_parser_failures",
            "consecutive_discovery_failures",
            "consecutive_article_failures",
        )
        for field in count_fields:
            row[field] = int(health.get(field) or 0) if health is not None else None
        row["index_fetch_status"] = health.get("index_fetch_status") if health else None
        row["last_successful_discovery_at"] = (
            health.get("last_successful_discovery_at") if health else None
        )
        row["cached_fallback_used"] = (
            bool(health.get("cached_fallback_used")) if health else None
        )

        status = (
            str(health.get("index_fetch_status") or "not_attempted")
            if health
            else "not_attempted"
        )
        if status in {"not_attempted", "skipped_selection"}:
            crawl_status = "not_attempted"
        elif status in {"failed", "parser_error"}:
            crawl_status = "failed"
        elif (
            status == "cached"
            or bool(health.get("cached_fallback_used"))
            or any(int(health.get(field) or 0) for field in count_fields[4:])
            or (
                int(health.get("attempted_articles") or 0) > 0
                and int(health.get("accepted_articles") or 0) == 0
            )
        ):
            crawl_status = "degraded"
        else:
            crawl_status = "healthy"
        row["current_crawl_status"] = crawl_status

        discovery = _parse_timestamp(row["last_successful_discovery_at"])
        expected_hours = row.get("expected_update_hours")
        discovery_window = (
            timedelta(hours=max(0, float(expected_hours)))
            if expected_hours is not None
            else stale_after
        )
        row["discovery_recency_status"] = (
            "no_data"
            if discovery is None
            else ("stale" if discovery < now - discovery_window else "recent")
        )


def find_source_expectation_failures(source_report: list[dict[str, Any]]) -> list[str]:
    """Return violations of opt-in per-source crawl/freshness expectations."""
    failures: list[str] = []
    for row in source_report:
        name = str(row["source"])
        minimum = row.get("expected_min_candidates")
        discovered = row.get("raw_link_candidates")
        if minimum is not None:
            if discovered is None:
                failures.append(
                    f"{name}: no current-crawl candidate count, expected at least {int(minimum)}"
                )
            elif discovered < int(minimum):
                failures.append(
                    f"{name}: discovered {discovered} candidates, expected at least {int(minimum)}"
                )
        if row.get("expected_update_hours") is not None:
            # Once crawl health is merged, update expectations concern discovery
            # activity. Retained content age remains an independent diagnostic.
            status_key = (
                "discovery_recency_status"
                if "discovery_recency_status" in row
                else "retained_content_freshness_status"
            )
            recency = row[status_key]
            satisfactory = (
                "recent" if status_key == "discovery_recency_status" else "fresh"
            )
            if recency == satisfactory:
                continue
            subject = (
                "discovery recency"
                if status_key == "discovery_recency_status"
                else "retained content freshness"
            )
            failures.append(
                f"{name}: {subject} is {recency}, expected an update within "
                f"{row['expected_update_hours']} hours"
            )
    return failures


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
        legacy_streak = int(row.get("consecutive_failures") or 0)
        fetch_streak = int(row.get("consecutive_fetch_failures", legacy_streak) or 0)
        parser_streak = int(row.get("consecutive_parser_failures", legacy_streak) or 0)
        discovery_streak = int(row.get("consecutive_discovery_failures") or 0)
        article_streak = int(
            row.get("consecutive_article_failures", legacy_streak) or 0
        )
        attempted = int(row.get("attempted_articles") or 0)
        accepted = int(row.get("accepted_articles") or 0)
        raw_candidates = int(row.get("raw_link_candidates") or 0)
        accepted_links = int(row.get("accepted_links") or 0)
        discovery_at = row.get("last_successful_discovery_at") or "unknown"
        article_outage = attempted > 0 and accepted == 0
        failure_kind = "article crawl failed" if article_outage else status
        hard_status = status in {"failed", "parser_error", "not_attempted"}
        hard_streak = parser_streak if status == "parser_error" else fetch_streak
        if status == "skipped_selection":
            continue
        elif hard_status and hard_streak >= failure_threshold:
            detail = f" ({error})" if error else ""
            failures.append(
                f"{name}: {status} for {hard_streak} consecutive runs{detail}"
            )
        elif hard_status:
            warnings.append(
                f"{name}: transient {status} (run {hard_streak}/{failure_threshold})"
            )
        elif discovery_streak:
            detail = (
                f"raw candidates={raw_candidates}, accepted links={accepted_links}, "
                f"last successful discovery={discovery_at}"
            )
            message = f"{name}: discovery failed for {discovery_streak} consecutive runs ({detail})"
            if discovery_streak >= failure_threshold:
                failures.append(message)
            else:
                warnings.append(
                    f"{name}: transient discovery failure (run {discovery_streak}/{failure_threshold}; {detail})"
                )
        elif article_outage and article_streak >= failure_threshold:
            detail = f" ({error})" if error else ""
            failures.append(
                f"{name}: {failure_kind} for {article_streak} consecutive runs{detail}"
            )
        elif article_outage and article_streak:
            warnings.append(
                f"{name}: transient {failure_kind} (run {article_streak}/{failure_threshold})"
            )
        elif status == "cached" or row.get("cached_fallback_used"):
            warnings.append(f"{name}: cached fallback used")
        elif status == "fetched" and attempted == 0 and raw_candidates == 0:
            warnings.append(f"{name}: fetched index contained no link candidates")
        elif attempted > 0 and accepted == 0:
            warnings.append(f"{name}: attempted {attempted} articles but accepted none")
        future_rejections = int(row.get("future_date_rejections") or 0)
        if future_rejections:
            warnings.append(
                f"{name}: rejected {future_rejections} future publication dates"
            )
    return failures, warnings


def check_anti_genie(
    baseline: dict[str, int], current: dict[str, int]
) -> Tuple[bool, str | None]:
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
    parser.add_argument(
        "path", nargs="?", default="docs/unified.json", help="Path to unified feed JSON"
    )
    parser.add_argument(
        "--sources", default="sources.json", help="Path to source configuration JSON"
    )
    parser.add_argument(
        "--stale-hours",
        type=float,
        default=168,
        help="Age in hours after which a source is stale",
    )
    parser.add_argument(
        "--allow-empty",
        action="append",
        default=[],
        metavar="SOURCE",
        help="Source exempted from empty-source enforcement (repeat or comma-separated)",
    )
    parser.add_argument(
        "--fail-on-empty-source",
        action="store_true",
        help="Globally fail when a non-exempt enabled source has no retained feed items",
    )
    parser.add_argument(
        "--source-health",
        metavar="PATH",
        help="Crawl diagnostics JSON produced by aggregate.py",
    )
    parser.add_argument(
        "--strict-source-health",
        action="store_true",
        help="Fail on repeated crawl failures reported by --source-health",
    )
    parser.add_argument(
        "--failure-threshold",
        type=int,
        default=3,
        help="Consecutive failed crawls required for a hard source-health failure (default: 3)",
    )
    parser.add_argument(
        "--baseline",
        help="Optional baseline feed JSON to enforce anti-genie rule (total can't drop except filtered listings)",
    )
    parser.add_argument(
        "--promote-feed",
        metavar="PATH",
        help="Replace this published feed only after every check passes",
    )
    parser.add_argument(
        "--promote-source-health",
        metavar="PATH",
        help="Replace this published health report only after every check passes",
    )
    parser.add_argument(
        "--candidate-state",
        metavar="PATH",
        help="Candidate crawler state to promote after validation",
    )
    parser.add_argument(
        "--promote-state",
        metavar="PATH",
        help="Published crawler state replaced after validation",
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

    health_rows: list[dict[str, Any]] = []
    if args.source_health:
        health_path = pathlib.Path(args.source_health)
        if not health_path.exists():
            raise SystemExit(f"Source health file not found: {health_path}")
        health_rows = _load_source_health(health_path)
        merge_current_crawl_metrics(
            source_report,
            health_rows,
            stale_after=timedelta(hours=max(0, args.stale_hours)),
        )

    for row in source_report:
        newest = row["newest_retained_timestamp"] or "none"
        fields = [
            f"source: {row['source']}",
            f"retained_item_count={row['retained_item_count']}",
            f"newest_retained_timestamp={newest}",
            f"retained_content_freshness_status={row['retained_content_freshness_status']}",
        ]
        if args.source_health:
            fields.extend(
                [
                    f"current_crawl_status={row['current_crawl_status']}",
                    f"index_fetch_status={row['index_fetch_status']}",
                    f"raw_link_candidates={row['raw_link_candidates']}",
                    f"accepted_links={row['accepted_links']}",
                    f"attempted_articles={row['attempted_articles']}",
                    f"accepted_articles={row['accepted_articles']}",
                    f"consecutive_fetch_failures={row['consecutive_fetch_failures']}",
                    f"consecutive_discovery_failures={row['consecutive_discovery_failures']}",
                    f"consecutive_article_failures={row['consecutive_article_failures']}",
                    f"last_successful_discovery_at={row['last_successful_discovery_at'] or 'none'}",
                    f"discovery_recency_status={row['discovery_recency_status']}",
                    f"cached_fallback_used={row['cached_fallback_used']}",
                ]
            )
        print(" | ".join(fields))
    for key, value in source_totals.items():
        print(f"{key}: {value}")

    exit_code = 0
    allow_empty = {
        name.strip()
        for value in args.allow_empty
        for name in value.split(",")
        if name.strip()
    }
    unexpected_empty = find_unexpected_empty_sources(
        source_report, allow_empty, fail_on_all=args.fail_on_empty_source
    )
    if unexpected_empty:
        print(f"unexpected_empty_sources: {', '.join(unexpected_empty)}")
        exit_code = 1

    if args.source_health:
        failures, warnings = classify_source_health(
            health_rows, failure_threshold=max(1, args.failure_threshold)
        )
        expectation_failures = find_source_expectation_failures(source_report)
        failures.extend(expectation_failures)
        print(f"crawl_sources_reported: {len(health_rows)}")
        print(f"crawl_failures: {len(failures)}")
        print(f"crawl_warnings: {len(warnings)}")
        for warning in warnings:
            print(f"crawl_warning: {warning}")
        for failure in failures:
            print(f"crawl_failure: {failure}")
        # Source health is reported regardless of policy. It only affects the
        # process status when a caller explicitly opts into strict diagnostics;
        # feed publication intentionally uses the diagnostic default.
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

    if exit_code == 0 and args.promote_feed:
        if not all(
            (
                args.source_health,
                args.promote_source_health,
                args.candidate_state,
                args.promote_state,
            )
        ):
            parser.error(
                "promotion requires --source-health, --promote-source-health, "
                "--candidate-state, and --promote-state"
            )
        candidate_state = pathlib.Path(args.candidate_state)
        if not candidate_state.exists():
            raise SystemExit(f"Candidate state file not found: {candidate_state}")
        # The crawler state belongs to the candidate feed. Advance it only when
        # that feed passes validation, otherwise rejected URLs must be retried.
        os.replace(candidate_state, args.promote_state)
        # Both candidates have already parsed successfully. Replace health first
        # and the public feed last, so a crash can never expose an unvalidated feed.
        os.replace(args.source_health, args.promote_source_health)
        os.replace(path, args.promote_feed)
        print(f"promoted_feed: {args.promote_feed}")
        print(f"promoted_source_health: {args.promote_source_health}")
        print(f"promoted_state: {args.promote_state}")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
