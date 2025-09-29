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
    text, _soup, _title, source_label = extract_article_content(
        url, html, selectors=None, title=None, src=None
    )
    assert text, f"Expected extracted text for {url}"
    assert _word_count(text) >= 25
    assert not _contains_deny_phrase(text)
    assert source_label in {"primary_selectors", "jsonld", "fallback_selectors"}


def test_pnp_boilerplate_removed_and_body_nonempty():
    url = "https://www.pnp.ru/economics/test-article.html"
    html = """
    <html>
      <body>
        <article class="article__content">
          <p>Экономика развивается ускоренными темпами.</p>
          <p>Автор: Редакция</p>
          <p>Читайте нас в Telegram t.me/pnpdaily</p>
          <p>Следите за обновлениями: vk.com/pnp</p>
        </article>
      </body>
    </html>
    """
    text, _soup, _title, source_label = extract_article_content(url, html, selectors=None, title=None, src=None)
    assert source_label in {"primary_selectors", "jsonld", "fallback_selectors"}
    assert _word_count(text) >= 4
    assert "Автор" not in text
    assert "Читайте нас" not in text
    assert "vk.com" not in text
