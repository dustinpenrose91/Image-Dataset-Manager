"""Tests for imgdb_thumbs_qt.py — PixmapLRU (pure Python, no Qt required)."""
from __future__ import annotations

import unittest

from imgdb_thumbs_qt import PixmapLRU


class PixmapLRUTests(unittest.TestCase):

    def test_miss_returns_none(self):
        lru = PixmapLRU(capacity=4)
        self.assertIsNone(lru.get("/some/path.webp"))

    def test_put_and_get(self):
        lru = PixmapLRU(capacity=4)
        lru.put("/a.webp", "pixmap-a")
        self.assertEqual(lru.get("/a.webp"), "pixmap-a")

    def test_len(self):
        lru = PixmapLRU(capacity=4)
        self.assertEqual(len(lru), 0)
        lru.put("/a.webp", "x")
        lru.put("/b.webp", "y")
        self.assertEqual(len(lru), 2)

    def test_put_updates_existing(self):
        lru = PixmapLRU(capacity=4)
        lru.put("/a.webp", "first")
        lru.put("/a.webp", "second")
        self.assertEqual(lru.get("/a.webp"), "second")
        self.assertEqual(len(lru), 1)

    def test_evicts_lru_when_over_capacity(self):
        lru = PixmapLRU(capacity=3)
        lru.put("/a.webp", "a")
        lru.put("/b.webp", "b")
        lru.put("/c.webp", "c")
        lru.put("/d.webp", "d")  # should evict /a.webp (LRU)
        self.assertIsNone(lru.get("/a.webp"))
        self.assertEqual(lru.get("/d.webp"), "d")
        self.assertEqual(len(lru), 3)

    def test_get_promotes_to_mru(self):
        lru = PixmapLRU(capacity=3)
        lru.put("/a.webp", "a")
        lru.put("/b.webp", "b")
        lru.put("/c.webp", "c")
        # Access /a.webp so it becomes MRU; /b.webp becomes LRU.
        lru.get("/a.webp")
        lru.put("/d.webp", "d")  # should evict /b.webp, not /a.webp
        self.assertIsNone(lru.get("/b.webp"))
        self.assertIsNotNone(lru.get("/a.webp"))

    def test_clear_empties_cache(self):
        lru = PixmapLRU(capacity=4)
        lru.put("/a.webp", "a")
        lru.put("/b.webp", "b")
        lru.clear()
        self.assertEqual(len(lru), 0)
        self.assertIsNone(lru.get("/a.webp"))

    def test_capacity_of_one(self):
        lru = PixmapLRU(capacity=1)
        lru.put("/a.webp", "a")
        lru.put("/b.webp", "b")
        self.assertIsNone(lru.get("/a.webp"))
        self.assertEqual(lru.get("/b.webp"), "b")

    def test_invalid_capacity_raises(self):
        with self.assertRaises(ValueError):
            PixmapLRU(capacity=0)
        with self.assertRaises(ValueError):
            PixmapLRU(capacity=-1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
