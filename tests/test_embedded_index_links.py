import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts.aggregate import _count_index_candidates


def test_count_index_candidates_plain_html():
    html = '<div><a href="/press-center/news/x/">x</a></div>'
    assert _count_index_candidates(html, parse_embedded_links=True) >= 1


def test_count_index_candidates_escaped_blob():
    payload = r'''\n<div class=\"press-item-inline__block\">\n<a class=\"press-item-inline\" href=\"/press-center/news/arkhitekturnoe-nasledie/\">\n</a>\n</div>'''
    assert _count_index_candidates(payload, parse_embedded_links=True) >= 1
