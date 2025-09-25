import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts.aggregate import _amp_append_allowed


def test_amp_append_allowed_whitelist_hosts():
    assert _amp_append_allowed("rg.ru")
    assert _amp_append_allowed("www.ria.ru")
    assert _amp_append_allowed("realty.ria.ru")


def test_amp_append_allowed_disallowed_hosts():
    assert not _amp_append_allowed("example.com")
    assert not _amp_append_allowed("news.rg.ru")
    assert not _amp_append_allowed("m.ria.ru")
