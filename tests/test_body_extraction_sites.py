import pathlib

import pytest

from scripts.aggregate import (
    DENY_PHRASES,
    _contains_deny_phrase,
    _word_count,
    extract_article_content,
)

FIXTURES = {
    "https://stroygaz.ru/news/infrastructure/test-article/": "stroygaz_article.html",
    "https://government.ru/news/99999/": "government_article.html",
    "https://minfin.gov.ru/ru/press-center/news/test-article/": "minfin_article.html",
    "https://faufcc.ru/press-tsentr/novosti/test-article/": "faufcc_article.html",
}


@pytest.mark.parametrize("url, fixture_name", FIXTURES.items())
def test_body_extraction_clean_text(url, fixture_name):
    fixture_path = pathlib.Path(__file__).resolve().parent / "fixtures" / fixture_name
    html = fixture_path.read_text(encoding="utf-8")
    text, _soup, _title = extract_article_content(url, html, selectors=None, title=None, src=None)
    assert text, f"Expected extracted text for {url}"
    assert _word_count(text) >= 25
    assert not _contains_deny_phrase(text)
