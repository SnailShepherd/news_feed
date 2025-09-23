import pathlib
import sys
import unittest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


from scripts.aggregate import merge_items, STATE


class MergeItemsTests(unittest.TestCase):
    def setUp(self):
        # Preserve the original first_seen map so tests can safely modify it.
        self._orig_first_seen = dict(STATE.get("first_seen", {}))

    def tearDown(self):
        STATE.setdefault("first_seen", {}).clear()
        STATE["first_seen"].update(self._orig_first_seen)

    def test_keeps_existing_content_text_when_new_is_missing(self):
        existing = [
            {
                "url": "https://example.com/news/1",
                "title": "Old title",
                "date_published": None,
                "content_text": "Existing full text",
            }
        ]
        new = [
            {
                "url": "https://example.com/news/1",
                "title": "Updated title",
                "date_published": "2024-09-23T10:00:00+03:00",
                "content_text": None,
            }
        ]

        merged = merge_items(existing, new)

        self.assertEqual(len(merged), 1)
        item = merged[0]
        self.assertEqual(item["title"], "Updated title")
        self.assertEqual(item["date_published"], "2024-09-23T10:00:00+03:00")
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
                "date_published": "2024-09-20T09:00:00+03:00",
            }
        ]
        new = [
            {
                "id": item_id,
                "url": "https://example.com/news/1",
                "title": "Second crawl",
                "date_published": fallback_iso,
            }
        ]

        merged = merge_items(existing, new)

        self.assertEqual(len(merged), 1)
        item = merged[0]
        self.assertEqual(item["date_published"], "2024-09-20T09:00:00+03:00")
        self.assertEqual(item["title"], "Second crawl")


if __name__ == "__main__":
    unittest.main()
