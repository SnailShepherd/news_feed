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
        "https://example.com/news/?view_result=Y",
        "https://ardexpert.ru/article/article-archive/",
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
        "https://eec.eaeunion.org/news/novoe-napravlenie-sotrudnichestva/",
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
    assert is_listing_url("https://notim.ru/news-partners/")

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


def test_minfin_press_center_listing():
    assert is_listing_url("https://minfin.gov.ru/ru/press-center/")
    assert is_listing_url(
        "https://minfin.gov.ru/ru/press-center", start_url="https://minfin.gov.ru/ru/press-center/"
    )
    assert not is_listing_url("https://minfin.gov.ru/ru/press-center/?id_4=40170-test-article")
    assert not is_listing_url("https://minfin.gov.ru/ru/press-center/news/test-article/")
    assert not is_listing_url("https://www.minfin.gov.ru/ru/press-center/press-relizy/2024/10/01/test/")
    assert is_listing_url("https://minfin.gov.ru/ru/press-center/contacts/")
    assert is_listing_url("https://minfin.gov.ru/ru/press-center/tags/finance/")


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://minfin.gov.ru/ru/press-center/news/test-article/", False),
        ("https://minfin.gov.ru/ru/press-center/press-relizy/test-release/", False),
        ("https://minfin.gov.ru/ru/press-center/news/test-article/?ysclid=123", True),
        ("https://minfin.gov.ru/ru/press-center/press-relizy/test-release/?fbclid=abc", True),
        ("https://minfin.gov.ru/ru/press-center/news/?search=1", True),
        ("https://minfin.gov.ru/ru/press-center/?id_4=40170-test-article", False),
        ("https://minfin.gov.ru/ru/press-center/press-relizy/tags/investments/", True),
    ],
)
def test_minfin_allow_deny_matrix(url, expected):
    assert is_listing_url(url) is expected


def test_government_numeric_news_allowed():
    assert not is_listing_url("http://government.ru/news/57782/")
