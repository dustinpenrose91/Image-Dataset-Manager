"""Tests for compute_perceptual_hash() in imgdb.py."""
from __future__ import annotations

import os
import sys
import shutil
import tempfile
import unittest

sys.path.insert(0, "/tmp/stubs")
sys.path.insert(0, "/home/claude/out")

from PIL import Image

import imgdb


def _gradient_image(size: int = 64) -> Image.Image:
    """Return a deterministic gradient PIL Image."""
    img = Image.new("RGB", (size, size))
    img.putdata([(i * 3 % 256, i * 7 % 256, i * 11 % 256) for i in range(size * size)])
    return img


class ComputePerceptualHashTests(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def _save(self, img: Image.Image, name: str, **kwargs) -> str:
        path = os.path.join(self.tmp, name)
        img.save(path, **kwargs)
        return path

    # ── format invariance ─────────────────────────────────────────────────────

    def test_png_vs_bmp_same_hash(self) -> None:
        """Same pixels, PNG vs BMP → identical perceptual hash."""
        img = _gradient_image()
        h_png = imgdb.compute_perceptual_hash(self._save(img, "a.png"))
        h_bmp = imgdb.compute_perceptual_hash(self._save(img, "a.bmp"))
        self.assertIsNotNone(h_png)
        self.assertEqual(h_png, h_bmp)

    def test_png_vs_tiff_same_hash(self) -> None:
        """Same pixels, PNG vs TIFF → identical perceptual hash."""
        img = _gradient_image()
        h_png = imgdb.compute_perceptual_hash(self._save(img, "a.png"))
        h_tiff = imgdb.compute_perceptual_hash(self._save(img, "a.tiff"))
        self.assertIsNotNone(h_png)
        self.assertEqual(h_png, h_tiff)

    def test_png_vs_webp_lossless_same_hash(self) -> None:
        """Same pixels, PNG vs lossless WEBP → identical perceptual hash."""
        img = _gradient_image()
        h_png = imgdb.compute_perceptual_hash(self._save(img, "a.png"))
        h_webp = imgdb.compute_perceptual_hash(self._save(img, "a.webp", lossless=True))
        self.assertIsNotNone(h_png)
        self.assertEqual(h_png, h_webp)

    # ── metadata invariance ───────────────────────────────────────────────────

    def test_png_different_dpi_same_hash(self) -> None:
        """Same pixels, different DPI metadata → identical perceptual hash."""
        img = Image.new("RGB", (64, 64), color=(100, 150, 200))
        h1 = imgdb.compute_perceptual_hash(self._save(img, "low.png", dpi=(72, 72)))
        h2 = imgdb.compute_perceptual_hash(self._save(img, "hi.png",  dpi=(300, 300)))
        self.assertIsNotNone(h1)
        self.assertEqual(h1, h2)

    # ── JPEG: lossy compression ───────────────────────────────────────────────

    def test_jpeg_vs_png_hamming_distance_small(self) -> None:
        """
        High-quality JPEG of the same image should produce a pHash within
        Hamming distance 5 of the PNG version. JPEG is lossy so exact
        equality is not guaranteed — but the perceptual difference is tiny.
        """
        img = _gradient_image()
        h_png = imgdb.compute_perceptual_hash(self._save(img, "a.png"))
        h_jpg = imgdb.compute_perceptual_hash(self._save(img, "a.jpg", quality=95))
        self.assertIsNotNone(h_png)
        self.assertIsNotNone(h_jpg)
        dist = bin(int(h_png, 16) ^ int(h_jpg, 16)).count("1")
        self.assertLessEqual(
            dist, 5,
            f"PNG vs high-quality JPEG Hamming distance {dist} is unexpectedly large",
        )

    # ── correctness ──────────────────────────────────────────────────────────

    def test_different_images_differ(self) -> None:
        """Two clearly different images → different perceptual hashes."""
        h_black = imgdb.compute_perceptual_hash(
            self._save(Image.new("RGB", (64, 64), (0, 0, 0)), "black.png"))
        h_white = imgdb.compute_perceptual_hash(
            self._save(Image.new("RGB", (64, 64), (255, 255, 255)), "white.png"))
        self.assertIsNotNone(h_black)
        self.assertIsNotNone(h_white)
        self.assertNotEqual(h_black, h_white)

    def test_hash_is_hex_string(self) -> None:
        """Return value is a lowercase hex string of the expected length."""
        img = _gradient_image()
        h = imgdb.compute_perceptual_hash(self._save(img, "a.png"))
        self.assertIsNotNone(h)
        self.assertIsInstance(h, str)
        # Default phash size=8 → 64 bits → 16 hex chars.
        self.assertEqual(len(h), 16)
        int(h, 16)  # must be valid hex

    # ── error handling ────────────────────────────────────────────────────────

    def test_non_image_file_returns_none(self) -> None:
        path = os.path.join(self.tmp, "text.txt")
        with open(path, "w") as f:
            f.write("not an image")
        self.assertIsNone(imgdb.compute_perceptual_hash(path))

    def test_missing_file_returns_none(self) -> None:
        self.assertIsNone(
            imgdb.compute_perceptual_hash(os.path.join(self.tmp, "ghost.png")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
