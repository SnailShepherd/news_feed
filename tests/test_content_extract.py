from datetime import timedelta

from bs4 import BeautifulSoup

from scripts.aggregate import (
    clean_content_text,
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
