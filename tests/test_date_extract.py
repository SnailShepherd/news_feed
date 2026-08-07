import pytest
from datetime import datetime

from bs4 import BeautifulSoup

from scripts import aggregate


@pytest.fixture
def fixed_now(monkeypatch):
    real_datetime = datetime

    class FixedDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            base = real_datetime(2024, 10, 5, 10, 0)
            if tz is not None:
                if getattr(base, "tzinfo", None):
                    base_local = base.astimezone(tz)
                    return base_local
                return tz.localize(base)
            return base

    monkeypatch.setattr(aggregate, "datetime", FixedDatetime)
    yield
    monkeypatch.setattr(aggregate, "datetime", real_datetime)


def test_try_parse_any_date_relative_today(fixed_now):
    result = aggregate.try_parse_any_date(["сегодня 12:45"])
    assert result is not None
    assert result.isoformat() == "2024-10-05T12:45:00+03:00"


def test_try_parse_any_date_relative_yesterday(fixed_now):
    result = aggregate.try_parse_any_date(["вчера 07:30"])
    assert result is not None
    assert result.isoformat() == "2024-10-04T07:30:00+03:00"


def test_extract_published_datetime_time_tag():
    html = "<html><body><time datetime='2024-10-03T09:15:00+03:00'>03.10.2024</time></body></html>"
    soup = BeautifulSoup(html, "html.parser")
    dt = aggregate.extract_published_datetime(soup, url="https://example.com/test")
    assert dt is not None
    assert dt.isoformat() == "2024-10-03T09:15:00+03:00"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2024-10-05T09:00:00+03:00", "2024-10-05T09:00:00+03:00"),
        ("2024-10-06T10:00:00+03:00", "2024-10-06T10:00:00+03:00"),
        ("2025-02-05T10:00:00+03:00", None),
    ],
)
def test_publication_date_plausibility(fixed_now, value, expected):
    parsed = aggregate._parse_datetime_signal(
        value,
        "test",
        url="https://example.com/article",
        source="Example",
    )
    assert (parsed.isoformat() if parsed else None) == expected


def test_merge_removes_preexisting_future_publication_date(fixed_now):
    existing = [{
        "id": "article-1",
        "source": "Example",
        "title": "Article",
        "url": "https://example.com/article",
        "content_text": "content",
        "first_seen": "2024-10-05T08:00:00+03:00",
        "bucketed_at": "2024-10-05T08:00:00+03:00",
        "fetched_at": "2024-10-05T09:00:00+03:00",
        "published_at": "2025-02-05T10:00:00+03:00",
    }]

    merged = aggregate.merge_items(existing, [])

    assert "published_at" not in merged[0]
    assert merged[0]["fetched_at"] == "2024-10-05T09:00:00+03:00"


def test_merging_retained_items_does_not_change_crawl_rejection_counters(fixed_now):
    aggregate.SOURCE_SUMMARY.clear()
    retained = [{
        "id": "article-1",
        "source": "Example",
        "title": "Article",
        "url": "https://example.com/article",
        "content_text": "content",
        "first_seen": "2024-10-05T08:00:00+03:00",
        "bucketed_at": "2024-10-05T08:00:00+03:00",
        "fetched_at": "2024-10-05T09:00:00+03:00",
        "published_at": "2025-02-05T10:00:00+03:00",
    }]

    aggregate.merge_items(retained, [])
    aggregate.build_feed(retained)

    assert dict(aggregate.SOURCE_SUMMARY) == {}


def test_explicit_publication_diagnostics_are_bounded_and_structured(
    fixed_now, monkeypatch
):
    aggregate.SOURCE_SUMMARY.clear()
    monkeypatch.setattr(aggregate, "PUBLICATION_REJECTION_SAMPLE_LIMIT", 1)
    diagnostics = aggregate.PublicationDiagnostics("Example")

    for value in ("2024-10-06T13:00:00+03:00", "2025-02-05T10:00:00+03:00"):
        assert aggregate._parse_datetime_signal(
            value,
            "api:date",
            url="https://example.com/article",
            source="Example",
            diagnostics=diagnostics,
        ) is None

    summary = aggregate.SOURCE_SUMMARY["Example"]
    assert summary["publication_rejections_by_signal"] == {"api:date": 2}
    assert len(summary["publication_rejection_samples"]) == 1
    assert (
        summary["publication_rejection_samples"][0]["raw_value"]
        == "2024-10-06T13:00:00+03:00"
    )
    assert summary["maximum_future_offset_seconds"] > 0
