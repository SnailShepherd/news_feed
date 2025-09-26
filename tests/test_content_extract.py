from scripts.aggregate import extract_content_with_fallback


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
