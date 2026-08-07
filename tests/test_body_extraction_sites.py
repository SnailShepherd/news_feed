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

PNP_URL = "https://www.pnp.ru/economics/pensionerov-v-rossii-za-god-stalo-menshe-na-409-tysyach-chelovek.html"


@pytest.mark.parametrize("url, fixture_name", FIXTURES.items())
def test_body_extraction_clean_text(url, fixture_name):
    fixture_path = pathlib.Path(__file__).resolve().parent / "fixtures" / fixture_name
    html = fixture_path.read_text(encoding="utf-8")
    text, _soup, _title, source_label = extract_article_content(
        url, html, selectors=None, title=None, src=None
    )
    assert text, f"Expected extracted text for {url}"
    assert _word_count(text) >= 25
    assert not _contains_deny_phrase(text)
    assert source_label in {"primary_selectors", "jsonld", "fallback_selectors"}


def test_pnp_production_body_beats_sidebar_recommendations():
    fixture_path = pathlib.Path(__file__).resolve().parent / "fixtures/pnp.ru/article.html"
    html = fixture_path.read_text(encoding="utf-8")
    src = {"min_words": 35, "content_selectors": [".js-mediator-article"]}
    text, _soup, _title, source_label = extract_article_content(
        PNP_URL, html, selectors=src["content_selectors"], title=None, src=src
    )
    assert source_label in {"primary_selectors", "jsonld", "fallback_selectors"}
    assert _word_count(text) >= src["min_words"]
    assert "Количество же неработающих пенсионеров" in text
    assert "Средний размер назначенной пенсии" in text
    assert "Минтруд предложил расширить категории лиц" not in text
    assert "Интересное за неделю" not in text
    assert "Главное сегодня" not in text
