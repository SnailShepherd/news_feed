import hashlib
import pathlib
import sys
import unittest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


from scripts.aggregate import (
    SOURCE_MIN_WORDS,
    STATE,
    _filter_by_min_words,
    build_item,
    merge_items,
    sort_timestamp,
)


class MergeItemsTests(unittest.TestCase):
    def setUp(self):
        # Preserve the original first_seen map so tests can safely modify it.
        self._orig_first_seen = dict(STATE.get("first_seen", {}))
        self._orig_min_words = dict(SOURCE_MIN_WORDS)
        self._orig_aliases = dict(STATE.get("aliases", {}))
        self._orig_content_hashes = dict(STATE.get("content_hashes", {}))
        self._orig_canonical_ids = dict(STATE.get("canonical_item_ids", {}))

    def tearDown(self):
        STATE.setdefault("first_seen", {}).clear()
        STATE["first_seen"].update(self._orig_first_seen)
        STATE.setdefault("aliases", {}).clear()
        STATE["aliases"].update(self._orig_aliases)
        STATE.setdefault("content_hashes", {}).clear()
        STATE["content_hashes"].update(self._orig_content_hashes)
        STATE.setdefault("canonical_item_ids", {}).clear()
        STATE["canonical_item_ids"].update(self._orig_canonical_ids)
        SOURCE_MIN_WORDS.clear()
        SOURCE_MIN_WORDS.update(self._orig_min_words)

    def test_merge_updates_structured_record_metadata(self):
        existing = [{
            "id": "stable", "source": "ЕРЗ.РФ", "title": "Title",
            "url": "https://erzrf.ru/news/story", "content_text": "old body",
            "first_seen": "2026-08-01T00:00:00+00:00",
            "bucketed_at": "2026-08-01T00:00:00+00:00",
            "fetched_at": "2026-08-01T00:00:00+00:00",
        }]
        incoming = [{
            **existing[0], "source_record_id": "28549959001",
            "tags": ["Аналитика", "Цены"], "summary": "Updated card annotation",
            "fetched_at": "2026-08-02T00:00:00+00:00",
        }]

        merged = merge_items(existing, incoming)

        self.assertEqual(merged[0]["source_record_id"], "28549959001")
        self.assertEqual(merged[0]["tags"], ["Аналитика", "Цены"])
        self.assertEqual(merged[0]["summary"], "Updated card annotation")

    def test_keeps_existing_content_text_when_new_is_missing(self):
        existing = [
            {
                "url": "https://example.com/news/1",
                "title": "Old title",
                "published_at": None,
                "content_text": "Existing full text",
            }
        ]
        new = [
            {
                "url": "https://example.com/news/1",
                "title": "Updated title",
                "published_at": "2024-09-23T10:00:00+03:00",
                "content_text": None,
            }
        ]

        merged = merge_items(existing, new)

        self.assertEqual(len(merged), 1)
        item = merged[0]
        self.assertEqual(item["title"], "Updated title")
        self.assertEqual(item.get("published_at"), "2024-09-23T10:00:00+03:00")
        self.assertEqual(item["content_text"], "Existing full text")

    def test_retains_original_date_when_new_uses_first_seen_fallback(self):
        item_id = "news-1"
        fallback_iso = "2024-09-25T12:00:00+03:00"
        STATE.setdefault("first_seen", {})[item_id] = fallback_iso

        existing = [
            {
                "id": item_id,
                "url": "https://example.com/news/1",
                "title": "First crawl",
                "published_at": "2024-09-20T09:00:00+03:00",
            }
        ]
        new = [
            {
                "id": item_id,
                "url": "https://example.com/news/1",
                "title": "Second crawl",
                "published_at": fallback_iso,
            }
        ]

        merged = merge_items(existing, new)

        self.assertEqual(len(merged), 1)
        item = merged[0]
        self.assertEqual(item.get("published_at"), "2024-09-20T09:00:00+03:00")
        self.assertEqual(item["title"], "Second crawl")

    def test_alias_preserves_identity_and_original_timestamps(self):
        canonical_url = "https://example.com/articles/canonical-story"
        item_id = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()
        url_a = canonical_url
        url_b = f"{canonical_url}?from=hub"

        existing_first_seen = "2024-09-20T08:15:00+03:00"
        existing_bucketed = "2024-09-20T08:00:00+03:00"
        existing_published = "2024-09-19T18:00:00+03:00"
        STATE.setdefault("first_seen", {})[item_id] = existing_first_seen

        existing = [
            {
                "id": item_id,
                "source": "Example Source",
                "title": "First crawl",
                "url": url_a,
                "content_text": "Short text.",
                "first_seen": existing_first_seen,
                "bucketed_at": existing_bucketed,
                "published_at": existing_published,
                "fetched_at": "2024-09-20T08:20:00+03:00",
                "canonical_url": canonical_url,
            }
        ]

        new = [
            {
                "id": item_id,
                "source": "Example Source",
                "title": "Updated crawl",
                "url": url_b,
                "content_text": "Short text with a few extra words to be longer.",
                "first_seen": "2024-09-20T09:45:00+03:00",
                "bucketed_at": "2024-09-20T09:00:00+03:00",
                "published_at": existing_first_seen,
                "fetched_at": "2024-09-20T09:50:00+03:00",
                "canonical_url": canonical_url,
            }
        ]

        merged = merge_items(existing, new)

        self.assertEqual(len(merged), 1)
        item = merged[0]
        self.assertEqual(item["id"], item_id)
        self.assertEqual(item["first_seen"], existing_first_seen)
        self.assertEqual(item["bucketed_at"], existing_bucketed)
        self.assertEqual(item["published_at"], existing_published)
        self.assertEqual(
            item["content_text"], "Short text with a few extra words to be longer."
        )
        self.assertEqual(item["url"], url_b)

    def test_filter_by_min_words_threshold(self):
        SOURCE_MIN_WORDS["Test Source"] = 120
        base_item = {
            "id": "1",
            "source": "Test Source",
            "title": "Short",
            "url": "https://example.com/short",
            "content_text": " ".join(["слово"] * 60),
            "first_seen": "2024-10-01T09:00:00+03:00",
            "bucketed_at": "2024-10-01T09:00:00+03:00",
            "fetched_at": "2024-10-01T09:05:00+03:00",
        }
        self.assertEqual(_filter_by_min_words([base_item]), [])

        base_item["content_text"] = " ".join(["слово"] * 140)
        filtered = _filter_by_min_words([base_item])
        self.assertEqual(len(filtered), 1)

    def test_build_item_reuses_canonical_id(self):
        STATE.setdefault("aliases", {}).clear()
        STATE.setdefault("content_hashes", {}).clear()
        STATE.setdefault("canonical_item_ids", {}).clear()

        body_text = " ".join(["Текст"] * 50)
        html_template = (
            "<html><head>"
            "<link rel='canonical' href='https://minfin.gov.ru/ru/press-center/news/test-release/'>"
            "<title>Новость</title>"
            f"</head><body><article><p>{body_text}</p></article></body></html>"
        )

        item_a = build_item(
            "https://minfin.gov.ru/ru/press-center/news/test-release/?ysclid=abcd",
            "Минфин России",
            html_template,
            content_selectors=["article"],
        )
        item_b = build_item(
            "https://minfin.gov.ru/ru/press-center/news/test-release/",
            "Минфин России",
            html_template,
            content_selectors=["article"],
        )

        self.assertEqual(item_a["id"], item_b["id"])
        canonical_ids = STATE.get("canonical_item_ids", {})
        self.assertEqual(
            canonical_ids.get("https://minfin.gov.ru/ru/press-center/news/test-release/"),
            item_a["id"],
        )
        self.assertEqual(
            canonical_ids.get(
                "https://minfin.gov.ru/ru/press-center/news/test-release/?ysclid=abcd"
            ),
            item_a["id"],
        )

    def test_refetch_does_not_promote_old_undated_item(self):
        old = {
            "first_seen": "2020-02-03T09:00:00+03:00",
            "fetched_at": "2026-08-07T18:00:00+03:00",
        }
        recent = {
            "first_seen": "2024-10-05T08:00:00+03:00",
            "fetched_at": "2024-10-05T08:05:00+03:00",
        }
        self.assertLess(sort_timestamp(old), sort_timestamp(recent))

    def test_migrates_crawl_time_masquerading_as_publication(self):
        timestamp = "2024-10-05T09:00:00+03:00"
        merged = merge_items([{
            "id": "legacy",
            "source": "Example",
            "title": "Old undated story",
            "url": "https://example.com/old",
            "content_text": "content",
            "first_seen": "2020-02-03T09:00:00+03:00",
            "bucketed_at": "2020-02-03T09:00:00+03:00",
            "fetched_at": timestamp,
            "published_at": timestamp,
        }], [])
        self.assertNotIn("published_at", merged[0])


if __name__ == "__main__":
    unittest.main()
