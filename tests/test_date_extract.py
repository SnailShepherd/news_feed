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
