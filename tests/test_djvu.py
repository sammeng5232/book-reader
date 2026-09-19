# -*- coding: utf-8 -*-
"""DjVu support: the C# decoder (djvutool) and djvu.py / bookformats.py.

Samples: two public-domain scans from the Internet Archive in tests/samples (a
2-page JSTOR notice with an OCR layer and colour logo; a 295-page 600 dpi Google
scan with OCR), and, when present on this machine, the user's own 642-page book
(shared JB2 dictionaries, colour palettes).  Tests skip when a sample is absent.

    python -m unittest tests.test_djvu
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import bookformats  # noqa: E402
import djvu  # noqa: E402
from epublib import EpubError  # noqa: E402

SAMPLES = os.path.join(HERE, "samples")
JSTOR = os.path.join(SAMPLES, "ia_jstor_20637537.djvu")
INDIAN = os.path.join(SAMPLES, "ia_indiansummer.djvu")
PHYSICS = (r"C:\Users\mengz\Desktop\文件\CUHK Notes\CUHK_PHYS\Physics Books\Computational Physics"
           r"\PHYS4061\4061reference\0521782856.Djvu")


def _tool(*args: str) -> bytes:
    return subprocess.run([djvu.tool_path(), *args], capture_output=True, check=True,
                          creationflags=djvu._NO_WINDOW).stdout


def _image(data: bytes):
    from PIL import Image
    return Image.open(io.BytesIO(data))


class ToolTests(unittest.TestCase):
    def test_tool_builds_and_runs(self) -> None:
        self.assertTrue(os.path.isfile(djvu.tool_path()))

    def test_iw44_coefficient_order(self) -> None:
        """Coefficient n's bits alternate column/row bits from the most significant down."""
        order = [int(x) for x in _tool("zigzag").decode().strip().split(",")]
        expect = []
        for n in range(1024):
            col = sum(((n >> (2 * b)) & 1) << (4 - b) for b in range(5))
            row = sum(((n >> (2 * b + 1)) & 1) << (4 - b) for b in range(5))
            expect.append(row * 32 + col)
        self.assertEqual(order, expect)
        self.assertEqual(sorted(order), list(range(1024)))     # a permutation of the tile

    def test_zp_table_is_the_verified_one(self) -> None:
        with open(os.path.join(ROOT, "djvutool", "ZPTable.cs"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("0x8000, 0x0000,  84, 145,   // 0", src)
        self.assertIn("0x481a, 0x0000, 230, 246,   // 250", src)
        self.assertIn("0x0b5d, 0x0000,  99, 127,   // 97", src)   # the spec PDF's text layer reads "0x0BSD"


@unittest.skipUnless(os.path.isfile(JSTOR), "JSTOR sample absent")
class JstorSampleTests(unittest.TestCase):
    def test_info_and_text_layer(self) -> None:
        info = json.loads(_tool("info", JSTOR))
        self.assertEqual(len(info["pages"]), 2)
        self.assertEqual((info["pages"][0]["w"], info["pages"][0]["h"], info["pages"][0]["dpi"]), (2550, 3301, 300))
        text = json.loads(_tool("text", JSTOR, "0"))
        words = [w[4] for line in text["lines"] for w in line[4]]
        self.assertIn("Journal", words)
        self.assertIn("Early Journal Content on JSTOR, Free to Anyone in the World",
                      " ".join(words))
        for line in text["lines"]:
            for x0, y0, x1, y1, _t in line[4]:
                self.assertTrue(0 <= x0 < x1 <= 2550 and 0 <= y0 < y1 <= 3301)

    def test_colour_page_renders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "p0.jpg")
            self.assertEqual(_tool("render", JSTOR, "0", "1275", out, "jpg").decode().strip(), "jpeg")
            with open(out, "rb") as fh:
                im = _image(fh.read()).convert("RGB")
            self.assertEqual(im.size, (1275, 1651))
            # the JSTOR logo (top left) comes from the IW44 background layer and is
            # red-brown: strongly red pixels there prove the chroma planes decode
            corner = list(im.crop((0, 0, 400, 400)).getdata())
            red = sum(1 for r, g, b in corner if r - b > 60 and r > 90)
            self.assertGreater(red, 0.02 * len(corner), f"{red} red pixels of {len(corner)}")
            # body text (JB2 mask) is dark on a white page
            gray = im.convert("L")
            hist = gray.histogram()
            self.assertGreater(sum(hist[240:]), 0.8 * sum(hist))       # mostly paper
            self.assertGreater(sum(hist[:60]), 0.005 * sum(hist))      # but real ink

    def test_book_opens_as_fixed_layout(self) -> None:
        cache = tempfile.mkdtemp(prefix="djvu-t-")
        try:
            book = bookformats.open_book(JSTOR, cache_root=cache)
            self.assertEqual(book.source_format, "djvu")
            self.assertTrue(book.is_fixed_layout)
            self.assertEqual(len(book.spine), 2)
            self.assertEqual(book.warnings, [])
            self.assertTrue(book.search("JSTOR"))
            cover = book.cover_bytes()
            self.assertGreater(_image(cover).size[0], 1000)
            book.close()
        finally:
            shutil.rmtree(cache, ignore_errors=True)


@unittest.skipUnless(os.path.isfile(INDIAN), "Indian Summer sample absent")
class IndianSummerTests(unittest.TestCase):
    def test_ocr_search_and_page_text(self) -> None:
        cache = tempfile.mkdtemp(prefix="djvu-t-")
        try:
            book = bookformats.open_book(INDIAN, cache_root=cache)
            self.assertEqual(len(book.spine), 295)
            self.assertGreater(len(book.search("Colville")), 300)
            page = book.plain_text(book.spine[15].zip_name)
            self.assertIn("INDIAN SUMMER", page)
            self.assertIn("Colville was finding a sort of vindictive", " ".join(page.split()))
            book.close()
        finally:
            shutil.rmtree(cache, ignore_errors=True)

    def test_600dpi_grey_page_renders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "p.png")
            self.assertEqual(_tool("render", INDIAN, "15", "1092", out, "png").decode().strip(), "png")
            with open(out, "rb") as fh:
                im = _image(fh.read())
            self.assertEqual(im.size, (1092, 1563))


@unittest.skipUnless(os.path.isfile(PHYSICS), "the user's DjVu book is not on this machine")
class SharedDictionaryBookTests(unittest.TestCase):
    """642 pages, JB2 shared dictionaries (INCL + Djbz), FGbz palettes, IW44 backgrounds."""

    def test_pages_render(self) -> None:
        info = json.loads(_tool("info", PHYSICS))
        self.assertEqual(len(info["pages"]), 642)
        with tempfile.TemporaryDirectory() as tmp:
            for page in (0, 30, 641):
                out = os.path.join(tmp, f"p{page}.jpg")
                _tool("render", PHYSICS, str(page), "1240", out, "jpg")
                with open(out, "rb") as fh:
                    gray = _image(fh.read()).convert("L")
                hist = gray.histogram()
                self.assertGreater(sum(hist[:80]), 0, f"page {page} has no ink")


class BrokenFileTests(unittest.TestCase):
    def test_truncated_djvu(self) -> None:
        if not os.path.isfile(JSTOR):
            self.skipTest("JSTOR sample absent")
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "cut.djvu")
            with open(JSTOR, "rb") as src, open(p, "wb") as dst:
                dst.write(src.read()[:400])
            with self.assertRaises(EpubError) as cm:
                bookformats.open_book(p, cache_root=tmp)
            self.assertEqual(cm.exception.kind, "corrupt")

    def test_empty_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "empty.djvu")
            open(p, "wb").close()
            with self.assertRaises(EpubError) as cm:
                bookformats.open_book(p, cache_root=tmp)
            self.assertEqual(cm.exception.kind, "corrupt")


if __name__ == "__main__":
    unittest.main()
