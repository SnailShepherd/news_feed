import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts import aggregate
from scripts.http_client import RequestStrategy


def test_get_host_client_does_not_force_source_strategy_for_foreign_domain(monkeypatch):
    state = {"host_state": {}, "stats": {"metrics": {}}}
    monkeypatch.setattr(aggregate, "STATE", state)
    monkeypatch.setattr(aggregate, "HOST_CLIENTS", {})
    monkeypatch.setattr(
        aggregate,
        "HOST_STRATEGIES",
        {
            "rg.ru": RequestStrategy(name="rg"),
            "ria.ru": RequestStrategy(name="ria"),
        },
    )

    src = {"base_url": "https://rg.ru", "start_url": "https://rg.ru/rubric/tema/ekonomika"}
    client = aggregate.get_host_client("https://ria.ru/20260211/example.html", src=src)

    assert client is not None
    assert client.host == "ria.ru"


def test_get_host_client_uses_source_strategy_for_www_alias(monkeypatch):
    state = {"host_state": {}, "stats": {"metrics": {}}}
    monkeypatch.setattr(aggregate, "STATE", state)
    monkeypatch.setattr(aggregate, "HOST_CLIENTS", {})
    monkeypatch.setattr(
        aggregate,
        "HOST_STRATEGIES",
        {
            "gge.ru": RequestStrategy(name="gge"),
        },
    )

    src = {"base_url": "https://gge.ru", "start_url": "https://gge.ru/press-center/news/"}
    client = aggregate.get_host_client("https://www.gge.ru/press-center/news/", src=src)

    assert client is not None
    assert client.host == "gge.ru"
