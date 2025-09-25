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
        "https://example.com/path/?page=two",
        "https://example.com/path/?ref=page",
    ],
)
def test_is_listing_url_negative(url):
    assert not is_listing_url(url)
