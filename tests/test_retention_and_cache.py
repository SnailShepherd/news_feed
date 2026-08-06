import os

from scripts import aggregate


def _item(source: str, sequence: int) -> dict:
    return {
        "id": f"{source}-{sequence}",
        "source": source,
        "published_at": f"2026-08-{sequence:02d}T00:00:00+00:00",
    }


def test_fair_retention_reserves_items_for_low_volume_sources(monkeypatch):
    monkeypatch.setattr(aggregate, "FEED_MAX_ITEMS", 6)
    monkeypatch.setattr(aggregate, "FEED_MIN_ITEMS_PER_SOURCE", 2)
    items = [_item("busy", day) for day in range(9, 0, -1)] + [
        _item("quiet", 2),
        _item("quiet", 1),
    ]

    retained = aggregate.retain_bounded_items(items)

    assert len(retained) == 6
    assert sum(item["source"] == "quiet" for item in retained) == 2
    assert retained == sorted(retained, key=lambda item: item["published_at"], reverse=True)


def test_page_cache_pruning_removes_old_then_excess_files(tmp_path, monkeypatch):
    monkeypatch.setattr(aggregate, "PAGES_DIR", tmp_path)
    monkeypatch.setattr(aggregate, "CACHE_MAX_AGE_DAYS", 2)
    monkeypatch.setattr(aggregate, "CACHE_MAX_BYTES", 8)
    old = tmp_path / "old"
    middle = tmp_path / "middle"
    newest = tmp_path / "newest"
    for path in (old, middle, newest):
        path.write_bytes(b"123456")
    os.utime(old, (100, 100))
    os.utime(middle, (250000, 250000))
    os.utime(newest, (300000, 300000))

    removed_files, removed_bytes = aggregate.prune_page_cache(now=300000)

    assert (removed_files, removed_bytes) == (2, 12)
    assert not old.exists()
    assert not middle.exists()
    assert newest.exists()
