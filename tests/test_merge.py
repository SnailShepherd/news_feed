import unittest


from scripts.aggregate import merge_items


class MergeItemsTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
