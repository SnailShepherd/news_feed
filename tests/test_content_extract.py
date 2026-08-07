from datetime import timedelta

from bs4 import BeautifulSoup

from scripts.aggregate import (
    _effective_source_min_words,
    clean_content_text,
    extract_content_text,
    extract_content_with_fallback,
    extract_published_datetime,
)


def test_not_equals_title():
    title = "Пример заголовка"
    html = (
        "<article><h1>Пример заголовка</h1><div itemprop='articleBody'>"
        "<p>Тело новости ...</p><p>Еще немного текста.</p></div></article>"
    )
    text = extract_content_with_fallback(html, ["[itemprop='articleBody']"], title)
    assert text.strip()
    assert text.strip() != title
    assert len(text) >= 20


def test_source_without_explicit_min_words_uses_host_override():
    short_widget = " ".join(f"карточка{i}" for i in range(35))
    html = f"""
      <html><head><title>Рынок недвижимости</title></head><body>
        <div class="widget"><p><a href="#one">{short_widget}</a></p>
          <p><a href="#two">{short_widget}</a></p></div>
      </body></html>
    """
    min_words = _effective_source_min_words(
        {"name": "РИА Недвижимость"},
        "https://realty.ria.ru/20260807/example.html",
    )
    text = extract_content_text(
        BeautifulSoup(html, "html.parser"), selectors=[".widget"], min_words=min_words
    )
    assert min_words == 120
    assert text is None


def test_standalone_dated_headlines_are_penalized_before_normalization():
    headlines = "".join(
        f"<p><span>0{i}.08.2026</span><span>Рекомендация номер {i} с подробным заголовком</span></p>"
        for i in range(1, 7)
    )
    body = "".join(
        f"<p>Основной абзац {i} содержит факты публикации и подробное объяснение события.</p>"
        for i in range(1, 4)
    )
    html = f"<main><div class='recommendations'>{headlines}</div><div class='body'>{body}</div></main>"
    text = extract_content_with_fallback(
        html, [".recommendations", ".body"], title=None, min_words=20
    )
    assert "Основной абзац" in text
    assert "Рекомендация номер" not in text


def test_clean_content_text_removes_noise_and_formats():
    raw = (
        "Заголовок\n"
        '"Цитата"\n'
        "Читайте нас в соцсетях\n"
        "Основной текст... еще текст.\n"
        "Поделиться материалом\n"
        "Слово - слово и ещё — слово.\n"
        "© Авторские права"
    )
    cleaned = clean_content_text(raw, title="Заголовок")
    assert "Читайте нас" not in cleaned
    assert "Поделиться" not in cleaned
    assert "©" not in cleaned
    assert "«Цитата»" in cleaned
    assert "…" in cleaned
    assert "слово — слово" in cleaned.lower()


def test_clean_content_text_strips_social_boilerplate():
    raw = (
        "Главная / Экономика / Новость\n"
        "Автор: Редакция\n"
        "Читайте нас в Telegram: https://t.me/pnp\n"
        "Основной текст новости.\n"
        "Подписывайтесь на наши соцсети\n"
        "Комментарии: 0\n"
    )
    cleaned = clean_content_text(raw, title="Неважно")
    assert "Автор:" not in cleaned
    assert "Telegram" not in cleaned
    assert "Главная" not in cleaned
    assert "Подписывайтесь" not in cleaned
    assert "Комментарии" not in cleaned
    assert "Основной текст" in cleaned


def test_extract_published_datetime_priority_meta():
    html = (
        "<html><head>"
        "<meta property='article:published_time' content='2024-10-02T08:00:00+03:00'>"
        "<meta itemprop='datePublished' content='2024-09-01T00:00:00+03:00'>"
        "</head><body></body></html>"
    )
    soup = BeautifulSoup(html, "html.parser")
    dt = extract_published_datetime(soup, url="https://example.com/test")
    assert dt is not None
    assert dt.isoformat() == "2024-10-02T08:00:00+03:00"


def test_extract_published_datetime_json_ld_fallback():
    html = (
        "<html><head></head><body>"
        "<script type='application/ld+json'>"
        '{"@type":"NewsArticle","datePublished":"2024-03-10T12:00:00"}'
        "</script>"
        "<span class='news-date'>10 марта 2024</span>"
        "</body></html>"
    )
    soup = BeautifulSoup(html, "html.parser")
    dt = extract_published_datetime(soup, url="https://example.com/jsonld")
    assert dt is not None
    assert dt.utcoffset() == timedelta(hours=3)
    assert dt.isoformat() == "2024-03-10T12:00:00+03:00"
