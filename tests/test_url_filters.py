import pytest

from scripts.url_filters import is_listing_url


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/news/",
        "https://example.com/tag/economy/",
        "https://example.com/section/archive/2024/",
        "https://example.com/path/?page=2",
        "https://example.com/list/?PAGEN_1=3",
        "https://example.com/poll/?VOTE_ID=12",
        "https://stroygaz.ru/news/regulation/",
        "https://stroygaz.ru/news/official/",
        "https://eec.eaeunion.org/news/speech/",
        "https://example.com/path/?page=two",
    ],
)
def test_is_listing_url_positive(url):
    assert is_listing_url(url)


@pytest.mark.parametrize(
    "url",
    [
        None,
        "https://example.com/article/",
        "https://example.com/breaking-news/",
        "https://example.com/path/?homepage=1",
        "https://example.com/path/?ref=page",
    ],
)
def test_is_listing_url_negative(url):
    assert not is_listing_url(url)


def test_host_specific_rules():
    # NOTIM
    assert not is_listing_url(
        "https://notim.ru/news/iskusstvennyy-intellekt-protiv-konservatizma-kak-ii-menyaet-stroitelnuyu-otrasl/"
    )
    assert is_listing_url("https://notim.ru/news/?PAGEN_1=3")

    # EEC
    assert not is_listing_url(
        "https://eec.eaeunion.org/news/bakytzhan-sagintaev-provel-rabochuyu-vstrechu-s-alekseem-overchukom/"
    )
    for hub in ("speech", "events", "video-gallery", "photo-gallery", "broadcasts"):
        assert is_listing_url(f"https://eec.eaeunion.org/news/{hub}/")
    assert is_listing_url("https://eec.eaeunion.org/news/?page=2")

    # ERZ.RF
    assert not is_listing_url(
        "https://erzrf.ru/news/za-god-predlozheniye-apartamentov-v-moskve-sokratilos-na-tret"
    )
    assert is_listing_url("https://erzrf.ru/news/?tag=%D0%9C%D0%BE%D1%81%D0%BA%D0%B2%D0%B0")

    # RIA-STK
    assert is_listing_url("https://ria-stk.ru/news/vse-novosti.php")
    assert is_listing_url("https://ria-stk.ru/news/vse-novosti.php?PAGEN_1=2")
    assert not is_listing_url("https://ria-stk.ru/news/index.php?ELEMENT_ID=244992&all_news=Y")
