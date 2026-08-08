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
    assert result is None


def test_try_parse_any_date_relative_yesterday(fixed_now):
    result = aggregate.try_parse_any_date(["вчера 07:30"])
    assert result is not None
    assert result.isoformat() == "2024-10-04T07:30:00+03:00"


def test_extract_published_datetime_time_tag():
    html = "<html><body><article><time datetime='2024-10-03T09:15:00+03:00'>03.10.2024</time></article></body></html>"
    soup = BeautifulSoup(html, "html.parser")
    dt = aggregate.extract_published_datetime(soup, url="https://example.com/test")
    assert dt is not None
    assert dt.isoformat() == "2024-10-03T09:15:00+03:00"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2024-10-05T09:00:00+03:00", "2024-10-05T09:00:00+03:00"),
        ("2024-10-06T10:00:00+03:00", None),
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


def test_article_date_wins_over_current_sidebar_date(fixed_now):
    soup = BeautifulSoup("""
      <article><time datetime="2020-02-03T09:00:00+03:00">3 февраля 2020</time></article>
      <aside><div class="date">5 октября 2024</div></aside>
    """, "html.parser")
    dt = aggregate.extract_published_datetime(soup, "https://notim.ru/news/story")
    assert dt.isoformat() == "2020-02-03T09:00:00+03:00"


def test_page_chrome_date_is_not_a_publication_date(fixed_now):
    soup = BeautifulSoup("""
      <main><h1>Старая новость</h1></main>
      <aside><time datetime="2024-10-05T09:59:00+03:00">Сегодня</time></aside>
    """, "html.parser")
    assert aggregate.extract_published_datetime(
        soup, "https://gge.ru/press/news/old-story"
    ) is None


def test_stroygaz_printed_article_date_wins_over_foreign_metadata():
    html = """
      <meta property="article:published_time" content="2026-08-08T11:52:00+03:00">
      <article><header><time>6 августа 2026 16:27</time></header>
      <div itemprop="articleBody">
        6 августа 2026 16:27 Shutterstock/FOTODOM В I полугодии 2026 года
        самыми быстрорастущими стали гостиницы без звезд. Подробный текст
        публикации с достаточным количеством слов для извлечения материала.
        {body}
      </div></article>
    """.format(body=" ".join(["содержание"] * 130))
    item = aggregate.build_item(
        "https://stroygaz.ru/news/commercial/oteli-bez-zvezd/",
        "Стройгаз.ру",
        html,
        content_selectors=["[itemprop='articleBody']"],
        src={"min_words": 0, "publication_date_selectors": ["article header time"]},
    )
    assert item["published_at"] == "2026-08-06T16:27:00+03:00"


def test_notim_recovers_old_date_without_swapping_day_and_month():
    soup = BeautifulSoup(
        '<div class="news-detail__date">9 декабря 2025</div>', "html.parser"
    )
    dt = aggregate.extract_published_datetime(
        soup, url="https://notim.ru/news/dlya-mostovikov/", source="НОТИМ",
        selectors=[".news-detail__date"],
    )
    assert dt.isoformat() == "2025-12-09T00:00:00+03:00"


def test_opening_event_date_cannot_override_article_metadata():
    soup = BeautifulSoup("""
      <meta property="article:published_time" content="2026-08-08T11:52:00+03:00">
      <article><p>5 августа 2026 состоялось заседание рабочей группы.</p></article>
    """, "html.parser")
    dt = aggregate.extract_published_datetime(soup, source="Стройгаз.ру")
    assert dt.isoformat() == "2026-08-08T11:52:00+03:00"


def test_numeric_date_with_explicit_offset_keeps_its_instant():
    dt = aggregate._parse_datetime_signal("09.12.2025 10:30 +0500", "api:date")
    assert dt.isoformat() == "2025-12-09T08:30:00+03:00"


def test_rejected_article_metadata_falls_back_to_canonical_url(fixed_now):
    soup = BeautifulSoup("""
      <head><meta property="article:published_time" content="2024-10-06T09:00:00+03:00">
      <link rel="canonical" href="https://example.com/news/2020/02/03/story"></head>
      <article><p>Story</p></article>
    """, "html.parser")
    dt = aggregate.extract_published_datetime(soup, "https://example.com/story")
    assert dt.isoformat().startswith("2020-02-03T")
