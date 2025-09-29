import hashlib
import pathlib
import sys
import unittest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


from scripts.aggregate import (
    SOURCE_MIN_WORDS,
    STATE,
    _filter_by_min_words,
    merge_items,
)


class MergeItemsTests(unittest.TestCase):
    def setUp(self):
        # Preserve the original first_seen map so tests can safely modify it.
        self._orig_first_seen = dict(STATE.get("first_seen", {}))
        self._orig_min_words = dict(SOURCE_MIN_WORDS)

    def tearDown(self):
        STATE.setdefault("first_seen", {}).clear()
        STATE["first_seen"].update(self._orig_first_seen)
        SOURCE_MIN_WORDS.clear()
        SOURCE_MIN_WORDS.update(self._orig_min_words)

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


if __name__ == "__main__":
    unittest.main()
