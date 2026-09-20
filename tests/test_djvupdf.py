# -*- coding: utf-8 -*-
"""DjVu -> PDF export (djvupdf.py), checked by reading the PDFs back with QtPdf.

Uses the two DjVu samples in tests/samples (skipped when absent) and, for the
outline, rotation, non-Latin text and page-failure paths, a stand-in decoder that
serves synthetic pages.

    python -m unittest tests.test_djvupdf
"""

from __future__ import annotations

import io
import os
import re
import shutil
import sys
import tempfile
import unicodedata
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import webhost  # noqa: E402,F401  (registers epub:// before any Qt application exists)
import djvu  # noqa: E402
import djvupdf  # noqa: E402
from epublib import EpubError  # noqa: E402

SAMPLES = os.path.join(HERE, "samples")
JSTOR = os.path.join(SAMPLES, "ia_jstor_20637537.djvu")
INDIAN = os.path.join(SAMPLES, "ia_indiansummer.djvu")

_app = None


def setUpModule() -> None:
    global _app
    from PySide6.QtGui import QGuiApplication
    _app = QGuiApplication.instance() or QGuiApplication([])


def _open_pdf(path: str):
    from PySide6.QtPdf import QPdfDocument
    doc = QPdfDocument()
    doc.load(path)
    return doc


def _render(doc, page: int, width: int):
    """The page rendered by QtPdf, as a Pillow RGB image."""
    from PIL import Image
    from PySide6.QtCore import QBuffer, QIODevice, QSize
    size = doc.pagePointSize(page)
    img = doc.render(page, QSize(width, max(1, round(width * size.height() / size.width()))))
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    im = Image.open(io.BytesIO(bytes(buf.data()))).convert("RGBA")
    page = Image.new("RGBA", im.size, (255, 255, 255, 255))      # QtPdf leaves paper transparent
    return Image.alpha_composite(page, im).convert("RGB")


def _mean_diff(a, b, width: int = 300) -> float:
    """Mean absolute grey difference of two images, both scaled to ``width`` (box filter)."""
    from PIL import Image, ImageChops, ImageStat
    size = (width, round(width * a.size[1] / a.size[0]))
    ga = a.convert("L").resize(size, Image.Resampling.BOX)
    gb = b.convert("L").resize(size, Image.Resampling.BOX)
    return ImageStat.Stat(ImageChops.difference(ga, gb)).mean[0]


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).replace("￾", "").replace("­", "")
    return re.sub(r"[^\w]+", " ", s).lower()


class _SampleCase(unittest.TestCase):
    SOURCE = ""
    tmp = ""
    pdf = ""
    stats: dict = {}
    info: dict = {}
    progress: list = []

    @classmethod
    def setUpClass(cls) -> None:
        if not os.path.isfile(cls.SOURCE):
            raise unittest.SkipTest("sample absent")
        cls.tmp = tempfile.mkdtemp(prefix="djvupdf-t-")
        cls.pdf = os.path.join(cls.tmp, "out.pdf")
        cls.progress = []
        cls.stats = djvupdf.convert_djvu_to_pdf(cls.SOURCE, cls.pdf,
                                                progress=lambda d, t: cls.progress.append((d, t)))
        tool = djvu.DjvuTool(cls.SOURCE)
        try:
            cls.info = tool.info()
            cls.texts = tool.all_text()
        finally:
            tool.close()

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.tmp:
            shutil.rmtree(cls.tmp, ignore_errors=True)

    def check_structure(self) -> None:
        pages = self.info["pages"]
        doc = _open_pdf(self.pdf)
        from PySide6.QtPdf import QPdfDocument
        self.assertEqual(doc.status(), QPdfDocument.Status.Ready)
        self.assertEqual(doc.pageCount(), len(pages))
        self.assertEqual(self.stats["pages"], len(pages))
        self.assertEqual(self.stats["failed_pages"], [])
        self.assertEqual(self.stats["bytes"], os.path.getsize(self.pdf))
        self.assertEqual(self.progress[-1], (len(pages), len(pages)))
        self.assertEqual(len(self.progress), len(pages))
        for i in range(0, len(pages), max(1, len(pages) // 20)):
            p = pages[i]
            size = doc.pagePointSize(i)
            w, h = p["w"] / p["dpi"] * 72, p["h"] / p["dpi"] * 72
            if p["rot"] in (90, 270):
                w, h = h, w
            self.assertAlmostEqual(size.width(), w, delta=0.5)
            self.assertAlmostEqual(size.height(), h, delta=0.5)
        with open(self.pdf, "rb") as fh:
            head = fh.read(8)
            fh.seek(-6, os.SEEK_END)
            tail = fh.read()
        self.assertEqual(head, b"%PDF-1.5")
        self.assertIn(b"%%EOF", tail)
        doc.close()

    def check_text(self, page: int) -> None:
        words = [str(w[4]) for line in self.texts[page]["lines"] for w in line[4]]
        words = [n for n in (_norm(w).strip() for w in words) if len(n) >= 3 and " " not in n]
        self.assertGreater(len(words), 10)
        doc = _open_pdf(self.pdf)
        text = " " + _norm(doc.getAllText(page).text()) + " "
        doc.close()
        sample = words[:: max(1, len(words) // 60)]
        found = [w for w in sample if w in text]
        self.assertGreaterEqual(len(found), 0.95 * len(sample), f"missing: {set(sample) - set(found)}")
        # whole lines survive too (words are separated by spaces, lines by line breaks)
        longest = max(self.texts[page]["lines"], key=lambda ln: len(ln[4]))
        phrase = " ".join(_norm(" ".join(str(w[4]) for w in longest[4])).split())
        self.assertIn(phrase, " ".join(text.split()))

    def check_looks_like_djvu(self, page: int, limit: float) -> None:
        from PIL import Image
        doc = _open_pdf(self.pdf)
        mine = _render(doc, page, 1200)
        doc.close()
        tool = djvu.DjvuTool(self.SOURCE)
        try:
            ref = Image.open(io.BytesIO(tool.render(page, 1200, "png"))).convert("RGB")
        finally:
            tool.close()
        ref = ref.resize(mine.size, Image.Resampling.BOX)
        diff = _mean_diff(mine, ref)
        self.assertLess(diff, limit, f"page {page}: mean grey difference {diff:.2f}")
        return mine


@unittest.skipUnless(os.path.isfile(JSTOR), "JSTOR sample absent")
class JstorPdfTests(_SampleCase):
    SOURCE = JSTOR

    def test_structure(self) -> None:
        self.check_structure()
        self.assertEqual(self.stats["text_pages"], 2)

    def test_text_layer(self) -> None:
        self.check_text(0)
        self.check_text(1)
        doc = _open_pdf(self.pdf)
        self.assertIn("Early Journal Content on JSTOR, Free to Anyone in the World",
                      " ".join(doc.getAllText(0).text().split()))
        doc.close()

    def test_pages_look_like_the_djvu(self) -> None:
        mine = self.check_looks_like_djvu(0, 6.0)
        # the red-brown JSTOR logo (IW44 background) is in colour
        corner = mine.crop((0, 0, mine.size[0] // 3, mine.size[1] // 4))
        red = sum(1 for r, g, b in corner.get_flattened_data() if r - b > 60 and r > 90)
        self.assertGreater(red, 0.01 * corner.size[0] * corner.size[1])
        self.check_looks_like_djvu(1, 8.0)

    def test_compact_mixed_raster(self) -> None:
        with open(self.pdf, "rb") as fh:
            data = fh.read()
        self.assertIn(b"/CCITTFaxDecode", data)          # full-resolution Group 4 text masks
        self.assertIn(b"/ImageMask true", data)
        self.assertIn(b"/DCTDecode", data)               # the colour background as JPEG
        self.assertIn(b"/Title <FEFF", data)
        self.assertIn(b"/Producer (Book Reader)", data)
        self.assertNotIn(b"/Outlines", data)             # the sample has no outline
        self.assertLess(len(data), 600_000)


@unittest.skipUnless(os.path.isfile(INDIAN), "Indian Summer sample absent")
class IndianSummerPdfTests(_SampleCase):
    SOURCE = INDIAN

    def test_structure(self) -> None:
        self.check_structure()
        self.assertGreater(self.stats["text_pages"], 280)

    def test_text_layer(self) -> None:
        self.check_text(15)
        doc = _open_pdf(self.pdf)
        page = " ".join(doc.getAllText(15).text().split())
        doc.close()
        self.assertIn("INDIAN SUMMER", page)
        self.assertIn("Colville was finding a sort of vindictive", page)

    def test_pages_look_like_the_djvu(self) -> None:
        for page in (0, 15, 150):
            self.check_looks_like_djvu(page, 6.0)

    def test_size(self) -> None:
        # 600 dpi bi-level text as Group 4: within a few times the (JB2) DjVu
        self.assertLess(self.stats["bytes"], 4 * os.path.getsize(INDIAN))


@unittest.skipUnless(os.path.isfile(JSTOR), "JSTOR sample absent")
class ExportControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="djvupdf-t-")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cancel_leaves_nothing(self) -> None:
        dst = os.path.join(self.tmp, "out.pdf")
        calls = []

        def cancelled() -> bool:
            calls.append(1)
            return len(calls) > 1                       # after the first page

        with self.assertRaises(djvupdf.ExportCancelled):
            djvupdf.convert_djvu_to_pdf(JSTOR, dst, cancelled=cancelled)
        self.assertEqual(os.listdir(self.tmp), [])

    def test_cancel_keeps_an_existing_file(self) -> None:
        dst = os.path.join(self.tmp, "out.pdf")
        with open(dst, "wb") as fh:
            fh.write(b"old")
        with self.assertRaises(djvupdf.ExportCancelled):
            djvupdf.convert_djvu_to_pdf(JSTOR, dst, cancelled=lambda: True)
        self.assertEqual(os.listdir(self.tmp), ["out.pdf"])
        with open(dst, "rb") as fh:
            self.assertEqual(fh.read(), b"old")

    def test_truncated_file_is_corrupt(self) -> None:
        for cut in (400, 30000):
            src = os.path.join(self.tmp, f"cut{cut}.djvu")
            with open(JSTOR, "rb") as a, open(src, "wb") as b:
                b.write(a.read()[:cut])
            dst = os.path.join(self.tmp, f"cut{cut}.pdf")
            with self.assertRaises(EpubError) as cm:
                djvupdf.convert_djvu_to_pdf(src, dst)
            self.assertEqual(cm.exception.kind, "corrupt")
            self.assertFalse(os.path.exists(dst))
        self.assertEqual(sorted(os.listdir(self.tmp)), ["cut30000.djvu", "cut400.djvu"])

    def test_not_a_djvu(self) -> None:
        src = os.path.join(self.tmp, "x.djvu")
        with open(src, "wb") as fh:
            fh.write(b"not a djvu file at all" * 10)
        with self.assertRaises(EpubError) as cm:
            djvupdf.convert_djvu_to_pdf(src, os.path.join(self.tmp, "x.pdf"))
        self.assertEqual(cm.exception.kind, "corrupt")

    def test_title_option(self) -> None:
        dst = os.path.join(self.tmp, "t.pdf")
        djvupdf.convert_djvu_to_pdf(JSTOR, dst, title="Ünïcode 標題")
        with open(dst, "rb") as fh:
            data = fh.read()
        self.assertIn(b"/Title <FEFF" + "Ünïcode 標題".encode("utf-16-be").hex().upper().encode(), data)


# --------------------------------------------------------------------------
# synthetic pages: outline, rotation, non-Latin text, a page that fails
# --------------------------------------------------------------------------

def _box_layers(w: int, h: int, box: tuple[int, int, int, int], rgb=(0, 0, 0)):
    """A page whose mask is one filled rectangle (x0, y0, x1, y1, top-down)."""
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0, y1 - y0
    row = bytearray(b"\xff" * ((bw + 7) // 8))
    for x in range(bw):
        row[x >> 3] &= ~(0x80 >> (x & 7)) & 0xFF
    lay = djvupdf._Layers()
    lay.w, lay.h, lay.dpi, lay.has_mask = w, h, 200, True
    lay.planes = [djvupdf._Plane(rgb, False, x0, y0, bw, bh, bytes(row) * bh)]
    return lay


class _FakeTool:
    """Serves three synthetic 200-dpi pages; the third one fails to decode."""

    PAGES = [{"w": 1700, "h": 2200, "dpi": 200, "rot": 0},
             {"w": 1700, "h": 2200, "dpi": 200, "rot": 90},
             {"w": 1000, "h": 1000, "dpi": 200, "rot": 0}]
    TEXT = {"lines": [[200, 200, 1400, 260, [[200, 200, 600, 260, "Hello"], [700, 200, 900, 260, "世界"],
                                            [1000, 205, 1400, 260, "\U0001d400\U0001d401x"]]],
                      [200, 400, 1000, 460, [[200, 400, 1000, 460, "Ωmega"]]]]}

    def __init__(self, _path: str) -> None:
        pass

    def info(self) -> dict:
        return {"pages": self.PAGES, "truncated": False,
                "outline": [{"t": "Chapter 1", "p": 0, "c": [{"t": "Section 1.1 ünï", "p": 1, "c": []}]},
                            {"t": "", "p": -1, "c": [{"t": "第二章", "p": 2, "c": []}]},
                            {"t": "nowhere", "p": 99, "c": []}]}

    def text(self, page: int) -> dict:
        return self.TEXT if page < 2 else {}

    def layers(self, page: int, _max: int):
        if page == 2:
            raise EpubError("a DjVu page could not be decoded", kind="corrupt", detail="test")
        return _box_layers(1700, 2200, (850, 1100, 1700, 2200), (0, 0, 0) if page == 0 else (200, 0, 0))

    def render(self, page: int, width: int, fmt: str) -> bytes:
        raise AssertionError("not used")

    def close(self) -> None:
        pass


class SyntheticPdfTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.mkdtemp(prefix="djvupdf-t-")
        cls.pdf = os.path.join(cls.tmp, "synthetic.pdf")
        with mock.patch.object(djvupdf, "_Tool", _FakeTool):
            cls.stats = djvupdf.convert_djvu_to_pdf(os.path.join(cls.tmp, "My_Book  Title.djvu"), cls.pdf)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_stats(self) -> None:
        self.assertEqual(self.stats["pages"], 3)
        self.assertEqual(self.stats["failed_pages"], [2])
        self.assertEqual(self.stats["text_pages"], 2)

    def test_pages_and_rotation(self) -> None:
        doc = _open_pdf(self.pdf)
        self.assertEqual(doc.pageCount(), 3)
        s0, s1, s2 = (doc.pagePointSize(i) for i in range(3))
        self.assertAlmostEqual(s0.width(), 612, delta=0.5)
        self.assertAlmostEqual(s0.height(), 792, delta=0.5)
        self.assertAlmostEqual(s1.width(), 792, delta=0.5)        # /Rotate 90
        self.assertAlmostEqual(s2.width(), 360, delta=0.5)
        # page 0: black ink exactly in the bottom-right quarter (mask polarity and placement)
        img = _render(doc, 0, 340).convert("L")
        w, h = img.size
        self.assertLess(img.getpixel((w * 3 // 4, h * 3 // 4)), 40)
        for x, y in ((w // 4, h // 4), (w * 3 // 4, h // 4), (w // 4, h * 3 // 4)):
            self.assertGreater(img.getpixel((x, y)), 215)
        # page 1, turned clockwise: the (red) bottom-right quarter lands bottom-left
        img = _render(doc, 1, 440)
        w, h = img.size
        r, g, b = img.getpixel((w // 4, h * 3 // 4))
        self.assertTrue(r > 150 and g < 60 and b < 60, (r, g, b))
        self.assertGreater(min(img.getpixel((w * 3 // 4, h * 3 // 4))), 215)
        # the failed page is blank
        img = _render(doc, 2, 200).convert("L")
        self.assertGreater(img.getextrema()[0], 215)
        doc.close()

    def test_text_in_any_script(self) -> None:
        doc = _open_pdf(self.pdf)
        text = doc.getAllText(0).text()
        for word in ("Hello", "世界", "\U0001d400\U0001d401x", "Ωmega"):
            self.assertIn(word, text)
        self.assertIn("Hello 世界", text)
        self.assertEqual(doc.getAllText(2).text().strip(), "")
        # the invisible words sit where the scan has them: find "Hello" by its box
        from PySide6.QtCore import QPointF
        sel = doc.getSelection(0, QPointF(200 * 72 / 200 + 2, 230 * 72 / 200), QPointF(600 * 72 / 200 - 2, 230 * 72 / 200))
        self.assertEqual(sel.text().strip(), "Hello")
        doc.close()

    def test_outline(self) -> None:
        from PySide6.QtPdf import QPdfBookmarkModel
        doc = _open_pdf(self.pdf)
        model = QPdfBookmarkModel()
        model.setDocument(doc)
        top = [model.index(r, 0) for r in range(model.rowCount())]
        self.assertEqual([i.data() for i in top], ["Chapter 1", "第二章"])
        kid = model.index(0, 0, top[0])
        self.assertEqual(kid.data(), "Section 1.1 ünï")
        page_role = QPdfBookmarkModel.Role.Page
        self.assertEqual(kid.data(page_role), 1)
        self.assertEqual(top[1].data(page_role), 2)
        with open(self.pdf, "rb") as fh:
            data = fh.read()
        self.assertIn(b"/PageMode /UseOutlines", data)
        self.assertIn(b"/Title <FEFF" + "My Book Title".encode("utf-16-be").hex().upper().encode(), data)
        doc.close()


class FontTests(unittest.TestCase):
    def test_glyphless_font_loads(self) -> None:
        from PySide6.QtGui import QFontDatabase
        ttf = djvupdf._glyphless_ttf()
        self.assertLess(len(ttf), 2000)
        fid = QFontDatabase.addApplicationFontFromData(ttf)
        self.assertGreaterEqual(fid, 0)
        self.assertIn("GlyphLessFont", QFontDatabase.applicationFontFamilies(fid))
        QFontDatabase.removeApplicationFont(fid)

    def test_to_unicode_covers_the_bmp_and_astral_characters(self) -> None:
        enc = djvupdf._TextEncoder()
        hexs, n = enc.encode("a中\U0001f600\U0001f600")
        self.assertEqual((hexs, n), ("0061" "4E2D" "D800" "D800", 4))
        cmap = djvupdf._to_unicode(enc.astral).decode()
        self.assertIn("<4E00> <4EFF> <4E00>", cmap)
        self.assertIn("<D800> <D83DDE00>", cmap)
        self.assertNotIn("<D800> <D8FF>", cmap)


if __name__ == "__main__":
    unittest.main()
