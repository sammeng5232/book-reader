#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for ``library_page.py`` (owner F).  stdlib ``unittest`` only.

Run::

    C:\\Users\\mengz\\AppData\\Local\\Programs\\Python\\Python314\\python.exe ^
        -m unittest tests.test_library_page -v

Every Store here uses a temporary root.  No window reaches the screen: the page and
its dialogs carry ``WA_DontShowOnScreen``.  The user's three real books are only read
(hashed, opened, thumbnailed into the temp cache); their size and mtime are checked
to be unchanged afterwards.  Real-book tests skip when the files are absent.
"""

from __future__ import annotations

import datetime as _dt
import glob
import io
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
import uuid
import zipfile
from xml.sax.saxutils import escape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from PySide6.QtCore import QEvent, QMimeData, QPoint, QPointF, Qt, QTimer, QUrl  # noqa: E402
from PySide6.QtGui import (QColor, QDragEnterEvent, QDropEvent, QFont, QGuiApplication,  # noqa: E402
                           QImage, QKeyEvent)
from PySide6.QtWidgets import QApplication  # noqa: E402

import library_page as lp  # noqa: E402
import strings  # noqa: E402
import theme as T  # noqa: E402
from store import Store  # noqa: E402

def _windows_clipboard_available() -> bool:
    """False while another process holds the Windows clipboard open (no app can copy)."""
    if sys.platform != "win32":
        return True
    import ctypes
    user32 = ctypes.windll.user32
    for _ in range(10):
        if user32.OpenClipboard(None):
            user32.CloseClipboard()
            return True
        time.sleep(0.05)
    return False


FIXTURES = os.path.join(ROOT, "tests", "fixtures")
CORRUPT = {"empty.epub", "not_a_zip.epub", "truncated.epub", "not_an_epub.epub", "no_container.epub"}
REAL_BOOKS = [p for p in [
    r"C:\Users\mengz\Desktop\文件\Econ Books\50人的二十年_樊纲 易纲等.epub",
    r"C:\Users\mengz\Desktop\文件\Econ Books\Econ Books_China\从此岸到彼岸_人民币汇率如何实现清洁浮动_缪延亮.epub",
] + glob.glob(r"C:\Users\mengz\Desktop\文件\CUHK Notes\CUHK_BFRM\Fin Books\*(李向科) (Z-Library).epub")
    if os.path.isfile(p)]


def app() -> QApplication:
    inst = QApplication.instance()
    return inst if inst is not None else QApplication([])


def pump(ms: int = 30) -> None:
    end = time.time() + ms / 1000
    while time.time() < end:
        QApplication.processEvents()
        time.sleep(0.003)


def settle(page: lp.LibraryPage, timeout: float = 90.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        page.wait_for_workers(200)
        pump(20)
        if not page.is_busy() and page._pool.activeThreadCount() == 0:
            pump(20)
            return
    raise TimeoutError("library workers did not finish")


# --------------------------------------------------------------------------
# synthetic books
# --------------------------------------------------------------------------

def _image(kind: str) -> tuple[bytes, str, str]:
    from PIL import Image, ImageDraw
    buf = io.BytesIO()
    if kind == "rgba":
        im = Image.new("RGBA", (600, 900), (30, 80, 150, 255))
        ImageDraw.Draw(im).ellipse((150, 200, 450, 500), fill=(0, 0, 0, 0))
        im.save(buf, "PNG")
        return buf.getvalue(), "cover.png", "image/png"
    if kind == "cmyk":
        Image.new("CMYK", (500, 750), (0, 140, 220, 20)).save(buf, "JPEG")
        return buf.getvalue(), "cover.jpg", "image/jpeg"
    if kind == "wide":
        Image.new("RGB", (1200, 560), (200, 60, 40)).save(buf, "JPEG")
        return buf.getvalue(), "cover.jpg", "image/jpeg"
    if kind == "tall":
        Image.new("RGB", (300, 3000), (20, 120, 60)).save(buf, "JPEG")
        return buf.getvalue(), "cover.jpg", "image/jpeg"
    if kind == "svg":
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="600" height="800">'
               '<rect width="600" height="800" fill="#1d3557"/><circle cx="300" cy="300" r="150" fill="#e63946"/></svg>')
        return svg.encode("utf-8"), "cover.svg", "image/svg+xml"
    raise ValueError(kind)


def make_epub(path: str, title: str, authors: list[str], lang: str = "en", cover: str | None = None) -> str:
    items = ['<item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>']
    meta_cover, extra = "", {}
    if cover:
        data, name, mt = _image(cover)
        extra[f"OEBPS/{name}"] = data
        items.append(f'<item id="cov" href="{name}" media-type="{mt}" properties="cover-image"/>')
        meta_cover = '<meta name="cover" content="cov"/>'
    creators = "".join(f"<dc:creator>{escape(a)}</dc:creator>" for a in authors)
    opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
 <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
  <dc:identifier id="uid">urn:uuid:{uuid.uuid4()}</dc:identifier><dc:title>{escape(title)}</dc:title>{creators}
  <dc:language>{lang}</dc:language><dc:description>&lt;p&gt;Test &amp;amp; book&lt;/p&gt;</dc:description>{meta_cover}
 </metadata>
 <manifest>{''.join(items)}</manifest><spine><itemref idref="c1"/></spine>
</package>"""
    chap = (f'<?xml version="1.0" encoding="UTF-8"?><html xmlns="http://www.w3.org/1999/xhtml"><head>'
            f'<title>{escape(title)}</title></head><body><p>{escape(title) * 20}</p></body></html>')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml",
                    '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:'
                    'xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type='
                    '"application/oebps-package+xml"/></rootfiles></container>')
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/c1.xhtml", chap)
        for name, data in extra.items():
            zf.writestr(name, data)
    return path


# ==========================================================================
# pure helpers
# ==========================================================================

class HelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        app()

    def test_cover_hue_is_stable_and_spread(self) -> None:
        bid = "3f8a1c0b9d2e4f6a7b8c9d0e1f2a3b4c"
        self.assertEqual(lp.cover_hue(bid), int(bid[:8], 16) % 360)   # not the salted builtin hash()
        hues = {lp.cover_hue(f"{i:032x}"[::-1]) for i in range(1, 400, 7)}
        self.assertGreater(len(hues), 30)
        self.assertTrue(0 <= lp.cover_hue("not-hex-id") < 360)

    def test_generated_colors_low_saturation_and_readable(self) -> None:
        for t in (T.LIGHT, T.SEPIA, T.DARK):
            for i in range(0, 360):          # every hue a book id can map to
                c = lp.generated_cover_colors(f"{i:08x}" + "0" * 24, t)
                self.assertLessEqual(c["bg"].hslSaturationF(), 0.31)
                self.assertGreaterEqual(T.contrast_ratio(c["title"], c["bg"]), 7.0, (t.name, i))
                self.assertGreaterEqual(T.contrast_ratio(c["author"], c["bg"]), 4.5, (t.name, i))

    def test_title_sort_pinyin_latin_numeric(self) -> None:
        titles = ["中文竖排测试书", "红楼梦", "《围城》", "阿Q正传", "Book 10", "Book 9", "Zebra", "apple", "从此岸到彼岸"]
        entries = [{"id": str(i), "title": t} for i, t in enumerate(titles)]
        got = [e["title"] for e in lp.sort_entries(entries, "title", "zh-Hans")]
        self.assertEqual(got, ["apple", "Book 9", "Book 10", "Zebra", "阿Q正传", "从此岸到彼岸", "红楼梦",
                               "《围城》", "中文竖排测试书"])
        self.assertEqual([e["title"] for e in lp.sort_entries(entries, "title", "en")], got)

    def test_other_sorts(self) -> None:
        es = [{"id": "a", "title": "A", "progress": 0.2, "opened_at": "2026-09-10T10:00:00+08:00",
               "added_at": "2026-01-01T00:00:00+08:00", "authors": ["Zed"]},
              {"id": "b", "title": "B", "progress": 0.9, "opened_at": "2026-09-12T10:00:00+08:00",
               "added_at": "2026-02-01T00:00:00+08:00", "authors": []},
              {"id": "c", "title": "C", "progress": 0.0, "opened_at": None,
               "added_at": "2026-03-01T00:00:00+08:00", "authors": ["Amy"]}]
        ids = lambda k: [e["id"] for e in lp.sort_entries(es, k, "en")]  # noqa: E731
        self.assertEqual(ids("recent"), ["b", "a", "c"])
        self.assertEqual(ids("added"), ["c", "b", "a"])
        self.assertEqual(ids("progress"), ["b", "a", "c"])
        self.assertEqual(ids("author"), ["c", "a", "b"])         # unknown author last

    def test_filter(self) -> None:
        es = [{"id": "1", "title": "EPUB 3 With Nav", "authors": ["Alan"], "path": r"C:\b\x.epub"},
              {"id": "2", "title": "No TOC", "authors": ["Noel Navigation"], "path": r"C:\b\no_toc.epub"},
              {"id": "3", "title": "50人的二十年", "authors": ["樊纲 易纲"], "path": r"C:\b\50.epub"}]
        f = lambda q: [e["id"] for e in lp.filter_entries(es, q)]  # noqa: E731
        self.assertEqual(f("ＥＰＵＢ nav"), ["1"])                # full-width, AND of terms, not ".epub"
        self.assertEqual(f("樊纲"), ["3"])
        self.assertEqual(f("toc"), ["2"])                         # file name stem counts
        self.assertEqual(f("  "), ["1", "2", "3"])

    def test_continue_entries(self) -> None:
        es = [{"id": str(i), "progress": p, "opened_at": f"2026-09-{10 + i:02d}T00:00:00+08:00"}
              for i, p in enumerate([0.0, 0.3, 1.0, 0.5, 0.1, 0.2, 0.7, 0.9, 0.4])]
        es.append({"id": "fin", "progress": 0.5, "finished_at": "2026-09-01T00:00:00+08:00"})
        got = [e["id"] for e in lp.continue_entries(es)]
        self.assertEqual(got, ["8", "7", "6", "5", "4", "3"])

    def test_collect_epubs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            a = make_epub(os.path.join(tmp, "a.epub"), "A", [])
            make_epub(os.path.join(tmp, "sub", "B.EPUB"), "B", [])
            make_epub(os.path.join(tmp, ".hidden", "c.epub"), "C", [])
            txt = os.path.join(tmp, "n.txt")
            open(txt, "w").close()
            files, bad = lp.collect_epubs([tmp, a, txt])
            self.assertEqual(sorted(os.path.basename(p) for p in files), ["B.EPUB", "a.epub"])
            self.assertEqual(bad, [os.path.abspath(txt)])

    def test_wrap_and_balanced_cover_titles(self) -> None:
        f = QFont("Microsoft YaHei")
        f.setPixelSize(14)
        lines, elided = lp.wrap_text("The Remarkably Long Title of a Book That Keeps Going", f, 160, 2)
        self.assertEqual(len(lines), 2)
        self.assertTrue(elided and lines[1].endswith("…"))
        self.assertEqual(lp.wrap_text("短", f, 160, 2), (["短"], False))
        px, cover_lines = lp._cover_title_layout("吾輩は猫である", "Microsoft YaHei", 160, 124, 150)
        self.assertEqual("".join(cover_lines), "吾輩は猫である")
        self.assertTrue(all(len(ln) >= 2 for ln in cover_lines), cover_lines)   # no orphan character

    def test_make_thumbnail_formats(self) -> None:
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            for kind in ("rgba", "cmyk", "wide", "tall", "svg"):
                data, _name, mt = _image(kind)
                dest = os.path.join(tmp, "covers", f"{kind}.jpg")
                w, h = lp.make_thumbnail(data, dest, media_type=mt)
                with Image.open(dest) as im:
                    self.assertEqual(im.format, "JPEG")
                    self.assertEqual(im.mode, "RGB")
                    self.assertEqual(im.size, (w, h))
                    self.assertTrue(w == lp.THUMB_WIDTH or h == lp.THUMB_MAX_EDGE, (kind, w, h))
                    self.assertLessEqual(max(w, h), lp.THUMB_MAX_EDGE)
                    if kind == "rgba":   # transparency flattened onto white
                        self.assertGreater(min(im.getpixel((w // 2, h * 350 // 900))), 240)
            self.assertEqual([n for n in os.listdir(os.path.join(tmp, "covers")) if n.endswith(".tmp")], [])
            with self.assertRaises(Exception):
                lp.make_thumbnail(b"definitely not an image", os.path.join(tmp, "bad.jpg"))

    def test_fit_cover_crop_and_letterbox(self) -> None:
        near = QImage(570, 800, QImage.Format.Format_RGB32)
        near.fill(QColor("#336699"))
        out = lp.fit_cover_image(near, 160, 240, 1.0)
        self.assertEqual((out.width(), out.height()), (160, 240))
        self.assertEqual(out.pixelColor(0, 120).name(), "#336699")          # cropped to fill
        wide = QImage(1200, 560, QImage.Format.Format_RGB32)
        wide.fill(QColor("#c83c28"))
        out = lp.fit_cover_image(wide, 160, 240, 2.0)
        self.assertEqual((out.width(), out.height(), out.devicePixelRatio()), (320, 480, 2.0))
        self.assertEqual(out.pixelColor(160, 5).name(), "#c83c28")           # letterbox uses the edge colour


# ==========================================================================
# the page, end to end
# ==========================================================================

class LibraryPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        app()
        cls.prev_lang = strings.language_preference()
        strings.set_language("zh-Hans")
        T.apply_to_application(app(), T.LIGHT)
        cls.tmp = tempfile.mkdtemp(prefix="er_test_library_")
        cls.store = Store(root=os.path.join(cls.tmp, "state"))
        b = os.path.join(cls.tmp, "books")
        cls.synth = {
            "hlm": make_epub(os.path.join(b, "hlm.epub"), "红楼梦", ["曹雪芹"], "zh-CN"),
            "neko": make_epub(os.path.join(b, "neko.epub"), "吾輩は猫である", ["夏目漱石"], "ja"),
            "rgba": make_epub(os.path.join(b, "rgba.epub"), "Long Enough Title For Two Lines Of Text", ["X"], "en", "rgba"),
            "svg": make_epub(os.path.join(b, "svg.epub"), "SVG Cover", ["Y"], "en", "svg"),
            "b9": make_epub(os.path.join(b, "b9.epub"), "Book 9", ["N"], "en"),
        }
        cls.vanish = make_epub(os.path.join(cls.tmp, "elsewhere", "v.epub"), "消失的书", ["无名氏"], "zh-CN")
        cls.real_stat = {p: (os.stat(p).st_size, os.stat(p).st_mtime_ns) for p in REAL_BOOKS}
        cls.opened: list[str] = []
        cls.removed: list[str] = []
        cls.page = lp.LibraryPage(cls.store, theme=T.LIGHT)
        cls.page.openBook.connect(cls.opened.append)
        cls.page.removeBook.connect(cls.removed.append)
        cls.revealed: list[str] = []
        cls.page.reveal_handler = cls.revealed.append
        cls.page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        cls.page.resize(1280, 900)
        cls.page.show()
        pump(50)
        cls.empty_at_start = cls.page._stack.currentWidget() is cls.page.empty
        fixtures = sorted(glob.glob(os.path.join(FIXTURES, "*.epub")))
        cls.n_valid_fixtures = len([f for f in fixtures if os.path.basename(f) not in CORRUPT])
        cls.page.add_paths(fixtures + REAL_BOOKS + list(cls.synth.values()) + [cls.vanish], open_single=False)
        settle(cls.page, 180)
        cls.seed_notice = cls.page.notice_text()
        cls.seed_count = len(cls.store.library())
        cls.opened_by_seed = list(cls.opened)
        cls.page.refresh()
        settle(cls.page)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.page.shutdown()
        cls.page.deleteLater()
        cls.store.close()
        pump(20)
        strings.set_language(cls.prev_lang)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self) -> None:
        strings.set_language("zh-Hans")
        self.page.resize(1280, 900)
        self.page.set_search("")
        self.page.set_view_mode("grid")
        self.page.set_sort("recent")
        pump(10)

    def entry(self, title: str) -> dict:
        return [e for e in self.store.library() if e.get("title") == title][0]

    # -- adding --------------------------------------------------------------
    def test_seeded_through_the_add_path(self) -> None:
        self.assertTrue(self.empty_at_start)
        expected = self.n_valid_fixtures + len(REAL_BOOKS) + len(self.synth) + 1
        self.assertEqual(self.seed_count, expected)
        self.assertIn("5 个文件无法添加", self.seed_notice)
        drm = [e for e in self.store.library() if e.get("drm")]
        self.assertEqual([(e["title"], e["drm"]) for e in drm], [("drm_fake", "adept")])
        self.assertNotIn(self.page._stack.currentWidget(), (self.page.empty,))
        self.assertEqual(self.opened_by_seed, [])      # a multi-file add never opens a book

    def test_covers(self) -> None:
        for key in ("rgba", "svg"):
            bid = [e for e in self.store.library() if os.path.normcase(e["path"]) == os.path.normcase(self.synth[key])][0]["id"]
            self.assertEqual(self.page.cover_state(bid), "image", key)
            self.assertTrue(os.path.isfile(self.store.cover_path(bid)))
            self.assertTrue(self.store.cover_path(bid).startswith(self.tmp))
        hlm = self.entry("红楼梦")
        self.assertEqual((self.page.cover_state(hlm["id"]), hlm.get("cover")), ("none", ""))

    @unittest.skipUnless(len(REAL_BOOKS) == 3, "the user's three real books are not present")
    def test_real_books_read_only_with_covers(self) -> None:
        for p in REAL_BOOKS:
            e = [x for x in self.store.library() if os.path.normcase(x["path"]) == os.path.normcase(p)][0]
            self.assertRegex(e["title"], r"[\u4e00-\u9fff]")
            self.assertEqual(self.page.cover_state(e["id"]), "image")
            self.assertEqual((os.stat(p).st_size, os.stat(p).st_mtime_ns), self.real_stat[p])

    # -- shelf layout ----------------------------------------------------------
    def test_continue_row_and_grid(self) -> None:
        now = _dt.datetime.now().astimezone()
        plan = [("红楼梦", 0.3), ("吾輩は猫である", 0.6), ("Book 9", 1.0)]
        for i, (t, p) in enumerate(plan):
            self.store.library_update(self.entry(t)["id"], progress=p,
                                      opened_at=(now - _dt.timedelta(hours=i)).isoformat(timespec="seconds"))
        try:
            self.page.refresh()
            cont = [s.book.title for s in self.page.view.slots() if s.section == "continue"]
            self.assertEqual(cont, ["红楼梦", "吾輩は猫である"])             # finished book excluded
            self.assertEqual(self.page.view.grid_metrics()[0], 6)
            self.page.resize(720, 520)
            pump(20)
            self.assertEqual(self.page.view.grid_metrics()[0], 3)
            self.page.set_search("红楼")
            self.assertFalse([s for s in self.page.view.slots() if s.section == "continue"])
        finally:
            for t, _p in plan:
                self.store.library_update(self.entry(t)["id"], progress=0.0, opened_at=None)
            self.page.refresh()

    def test_progress_bar_pixels(self) -> None:
        bid = self.entry("红楼梦")["id"]
        self.store.library_update(bid, progress=0.5, opened_at=_dt.datetime.now().astimezone().isoformat())
        try:
            self.page.refresh()
            self.page.view.verticalScrollBar().setValue(0)
            pump(20)
            s = [x for x in self.page.view.slots() if x.section == "continue"][0]
            im = self.page.grab().toImage()
            off = self.page.view.mapTo(self.page, QPoint(0, 0))
            cov = s.cover.translated(off.x(), off.y())
            y = int(cov.bottom()) - 2
            self.assertEqual(im.pixelColor(int(cov.left()) + 4, y).name(), T.LIGHT.accent.lower())
            self.assertNotEqual(im.pixelColor(int(cov.right()) - 4, y).name(), T.LIGHT.accent.lower())
            self.assertEqual(im.pixelColor(int(cov.left()) + 4, int(cov.bottom()) + 1).name(), T.LIGHT.bg.lower())
        finally:
            self.store.library_update(bid, progress=0.0, opened_at=None)
            self.page.refresh()

    def test_page_fits_minimum_window_in_every_language(self) -> None:
        for lang in strings.LANGUAGES:
            strings.set_language(lang)
            self.page.resize(720, 520)
            pump(20)
            self.assertEqual((self.page.width(), self.page.height()), (720, 520), lang)
            right = max(w.geometry().right() for w in (self.page.open_btn, self.page.folder_btn, self.page.search,
                                                       self.page.sort_combo, self.page._toggle_host,
                                                       self.page.more_btn))
            self.assertLessEqual(right, self.page.topbar.width(), lang)

    def test_list_view_and_persistence(self) -> None:
        self.page.set_view_mode("list")
        slots = self.page.view.slots()
        self.assertEqual(len(slots), len(self.store.library()))
        self.assertTrue(all(s.rect.height() == 64 and s.section == "all" for s in slots))
        self.assertEqual(self.store.get("window.library_view"), "list")
        self.page.set_sort("title")
        self.assertEqual(self.store.get("window.library_sort"), "title")

    # -- keyboard ---------------------------------------------------------------
    def key(self, k: int, mods=Qt.KeyboardModifier.NoModifier, target=None, text: str = "") -> QKeyEvent:
        ev = QKeyEvent(QEvent.Type.KeyPress, k, mods, text)
        QApplication.sendEvent(target or self.page.view, ev)
        pump(5)
        return ev

    def test_keyboard(self) -> None:
        v = self.page.view
        v.clear_selection()
        self.key(Qt.Key.Key_Right)
        self.assertEqual(v.selected_index(), 0)
        self.key(Qt.Key.Key_Right)
        self.key(Qt.Key.Key_Down)
        self.assertEqual(v.slots()[v.selected_index()].rect.left(), v.slots()[1].rect.left())
        self.key(Qt.Key.Key_End)
        self.assertEqual(v.selected_index(), len(v.slots()) - 1)
        self.opened.clear()
        self.key(Qt.Key.Key_Home)
        self.key(Qt.Key.Key_Return)
        self.assertEqual(len(self.opened), 1)
        self.key(Qt.Key.Key_Slash, text="/")
        self.assertIs(self.page.window().focusWidget(), self.page.search)
        so = QKeyEvent(QEvent.Type.ShortcutOverride, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
        so.ignore()
        QApplication.sendEvent(v, so)
        self.assertTrue(so.isAccepted())
        so = QKeyEvent(QEvent.Type.ShortcutOverride, Qt.Key.Key_O, Qt.KeyboardModifier.ControlModifier)
        so.ignore()
        QApplication.sendEvent(v, so)
        self.assertFalse(so.isAccepted())
        self.page.set_search("红楼")
        self.key(Qt.Key.Key_Escape, target=self.page.search)
        self.assertEqual(self.page.search.text(), "")

    # -- menu and actions ---------------------------------------------------------
    def test_context_menu(self) -> None:
        e = self.entry("红楼梦")
        menu = self.page.build_context_menu(e["id"])
        acts = [a for a in menu.actions()]
        labels = [a.text().split("\t")[0] for a in acts if not a.isSeparator()]
        self.assertEqual(labels, ["打开", "在文件夹中显示", "复制文件路径", "书籍信息", "转换为 LaTeX 和 PDF…",
                                  "从书架移除"])
        self.assertTrue(acts[5].isSeparator())
        self.assertFalse(any(re.search(r"删除|delete", t, re.I) for t in labels))
        self.assertTrue(all(a.shortcut().isEmpty() for a in acts))
        by = {a.objectName(): a for a in acts}
        by["ctx.reveal"].trigger()
        self.assertEqual(os.path.normcase(self.revealed[-1]), os.path.normcase(e["path"]))
        # What the page controls: it hands the exact path to the clipboard.  Checked through
        # a recording stand-in, because another process can hold the Windows clipboard locked
        # machine-wide -- then every app's copy fails and the real round-trip proves nothing.
        handed: list[str] = []

        class _Rec:
            def setText(self, text: str, *_a) -> None:  # noqa: N802 - Qt name
                handed.append(text)

        real_clipboard = lp.QGuiApplication.clipboard
        lp.QGuiApplication.clipboard = staticmethod(lambda: _Rec())
        try:
            by["ctx.copypath"].trigger()
        finally:
            lp.QGuiApplication.clipboard = real_clipboard
        self.assertEqual([os.path.normcase(t) for t in handed], [os.path.normcase(e["path"])])
        # The real round-trip, when Windows lets anyone have the clipboard.
        if _windows_clipboard_available():
            cb = QGuiApplication.clipboard()
            saved = cb.text()
            try:
                by["ctx.copypath"].trigger()
                self.assertEqual(os.path.normcase(cb.text()), os.path.normcase(e["path"]))
            finally:
                cb.setText(saved)
        menu.deleteLater()

    def test_book_info_dialog_live_language(self) -> None:
        orig_show = lp.BookInfoDialog.show

        def offscreen(dlg):  # noqa: ANN001
            dlg.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            orig_show(dlg)
        lp.BookInfoDialog.show = offscreen
        try:
            dlg = self.page.show_book_info(self.entry("红楼梦")["id"])
            settle(self.page)
            rows = dict(dlg.rows())
            self.assertIn("文件位置", rows)
            self.assertIn("Test & book", dlg.description())
            strings.set_language("en")
            pump(10)
            self.assertIn("Location", dict(dlg.rows()))
            self.assertGreaterEqual(dlg.height(), dlg.layout().totalHeightForWidth(dlg.width()) - 1)
            dlg.close()
        finally:
            lp.BookInfoDialog.show = orig_show

    def test_remove_confirm_and_undo(self) -> None:
        orig_exec = lp.ConfirmRemoveDialog.exec
        answers = [False, True]

        def auto(dlg):  # noqa: ANN001
            dlg.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            accept = answers.pop(0)
            QTimer.singleShot(20, (dlg.remove_btn if accept else dlg.cancel_btn).click)
            return orig_exec(dlg)
        lp.ConfirmRemoveDialog.exec = auto
        try:
            e = self.entry("Book 9")
            self.assertFalse(self.page.remove_book(e["id"]))
            self.assertIsNotNone(self.store.library_get(e["id"]))
            self.assertTrue(self.page.remove_book(e["id"]))
            self.assertIsNone(self.store.library_get(e["id"]))
            self.assertEqual(self.removed[-1], e["id"])
            self.assertTrue(os.path.isfile(self.synth["b9"]))                   # the file is never touched
            self.assertIn("Book 9", self.page.notice_text())
            self.page.notice.action_btn.click()
            pump(10)
            self.assertIsNotNone(self.store.library_get(e["id"]))
        finally:
            lp.ConfirmRemoveDialog.exec = orig_exec

    # -- drops, suggestions, rescans, missing -------------------------------------
    def drop(self, paths: list[str]) -> None:
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
        enter = QDragEnterEvent(QPoint(50, 50), Qt.DropAction.CopyAction, mime,
                                Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(self.page, enter)
        self.assertTrue(enter.isAccepted())
        QApplication.sendEvent(self.page, QDropEvent(QPointF(50, 50), Qt.DropAction.CopyAction, mime,
                                                     Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
        pump(20)
        settle(self.page)

    def test_drop_file_opens_and_suggests_without_adding(self) -> None:
        zone = os.path.join(self.tmp, "zone")
        f = make_epub(os.path.join(zone, "dropped.epub"), "拖进来的书", ["拖放"], "zh-CN")
        make_epub(os.path.join(zone, "s1.epub"), "Sibling One", ["S"])
        self.opened.clear()
        self.drop([f])
        self.assertEqual([os.path.normcase(p) for p in self.opened], [os.path.normcase(f)])
        sug = self.page.suggestion()
        self.assertIsNotNone(sug)
        self.assertEqual(len(sug[1]), 1)
        self.assertFalse([e for e in self.store.library() if e["title"] == "Sibling One"])
        self.page.card.dismiss_btn.click()
        self.assertIsNone(self.page.suggestion())
        self.assertIn(os.path.normcase(zone),
                      [os.path.normcase(x) for x in self.store.get("behavior.found_books_dismissed")])
        self.assertFalse(self.page.offer_found_books(zone, [os.path.join(zone, "s1.epub")]))

    def test_single_broken_file_goes_to_the_reader(self) -> None:
        bad = os.path.join(FIXTURES, "truncated.epub")
        n = len(self.store.library())
        self.opened.clear()
        self.drop([bad])
        self.assertEqual(len(self.store.library()), n)                 # not shelved
        self.assertEqual([os.path.normcase(p) for p in self.opened], [os.path.normcase(bad)])
        self.assertEqual(self.page.notice_text(), strings.plural("lib.add_failed", 1))

    def test_drop_folder_then_rescan(self) -> None:
        folder = os.path.join(self.tmp, "folder")
        make_epub(os.path.join(folder, "deep", "f1.epub"), "Folder Book", ["F"])
        self.opened.clear()
        self.drop([folder])
        self.assertIn("Folder Book", {e["title"] for e in self.store.library()})
        self.assertEqual(self.opened, [])
        self.assertIn(os.path.normcase(folder), [os.path.normcase(x) for x in self.page.watched_folders()])
        make_epub(os.path.join(folder, "later.epub"), "Later Book", ["L"])
        self.page.rescan()
        settle(self.page)
        self.assertIn("Later Book", {e["title"] for e in self.store.library()})
        self.assertEqual(self.page.notice_text(), strings.plural("lib.rescan.done", 1))

    def test_open_and_add_folder_dialogs(self) -> None:
        one = make_epub(os.path.join(self.tmp, "picked", "one.epub"), "Picked One", ["P"])
        make_epub(os.path.join(self.tmp, "pickdir", "two.epub"), "Picked Two", ["P"])
        calls: list[tuple] = []
        orig_files, orig_dir = lp.QFileDialog.getOpenFileNames, lp.QFileDialog.getExistingDirectory
        lp.QFileDialog.getOpenFileNames = staticmethod(lambda *a: (calls.append(a), ([one], ""))[1])
        lp.QFileDialog.getExistingDirectory = staticmethod(
            lambda *a: (calls.append(a), os.path.join(self.tmp, "pickdir"))[1])
        try:
            self.opened.clear()
            self.key(Qt.Key.Key_O, Qt.KeyboardModifier.ControlModifier, target=self.page)   # Ctrl+O
            settle(self.page)
            self.assertEqual([os.path.normcase(p) for p in self.opened], [os.path.normcase(one)])
            self.assertEqual(calls[0][1], "打开书籍")                                        # dlg.open.title
            self.assertIn("*.epub", calls[0][3])
            self.page.folder_btn.click()
            settle(self.page)
            self.assertIn("Picked Two", {e["title"] for e in self.store.library()})
            self.assertEqual(calls[1][1], "选择文件夹")
        finally:
            lp.QFileDialog.getOpenFileNames, lp.QFileDialog.getExistingDirectory = orig_files, orig_dir

    def test_missing_then_moved(self) -> None:
        e = self.entry("消失的书")
        moved = os.path.join(self.tmp, "moved", "v.epub")
        os.makedirs(os.path.dirname(moved), exist_ok=True)
        shutil.move(self.vanish, moved)
        try:
            self.page.refresh()
            settle(self.page)
            self.assertTrue(self.store.library_get(e["id"])["missing"])
            s = [x for x in self.page.view.slots() if x.book.bid == e["id"]][0]
            self.assertTrue(s.book.missing)
            self.page.add_paths([moved], open_single=False)
            settle(self.page)
            now = self.store.library_get(e["id"])
            self.assertFalse(now["missing"])
            self.assertEqual(os.path.normcase(now["path"]), os.path.normcase(moved))
            self.assertIn(self.vanish, now["path_history"])
        finally:
            shutil.move(moved, self.vanish)

    # -- language ---------------------------------------------------------------
    def test_live_language_switch(self) -> None:
        for lang in strings.LANGUAGES:
            strings.set_language(lang)
            pump(5)
            tr = lambda k: strings.translate(lang, k)  # noqa: E731
            self.assertEqual(self.page.search.placeholderText(), tr("lib.search"))
            self.assertEqual([self.page.sort_combo.itemText(i) for i in range(self.page.sort_combo.count())],
                             [tr(f"lib.sort.{k}") for k in lp.SORT_KEYS])
            self.assertEqual(self.page.empty.title.text(), tr("lib.empty.title"))
            self.assertEqual(self.page.grid_btn.toolTip(), tr("lib.view.grid"))
            self.assertEqual(self.page.view.headers()[-1][2], tr("lib.section.all"))


# ==========================================================================
# source hygiene
# ==========================================================================

class SourceTests(unittest.TestCase):
    SRC = open(os.path.join(ROOT, "library_page.py"), encoding="utf-8").read()

    def test_every_string_key_exists_in_all_languages(self) -> None:
        keys = set(re.findall(r'\bS\(\s*"([a-z0-9_.]+)"', self.SRC))
        keys |= set(re.findall(r"\bS\(\s*'([a-z0-9_.]+)'", self.SRC))
        plurals = set(re.findall(r'\bplural\(\s*"([a-z0-9_.]+)"', self.SRC))
        keys |= {f"{p}.{f}" for p in plurals for f in ("one", "other")}
        keys |= set(lp.SORT_LABEL_KEYS.values())
        self.assertGreater(len(keys), 40)
        for lang in strings.LANGUAGES:
            missing = sorted(k for k in keys if k not in strings.TABLES[lang])
            self.assertEqual(missing, [], lang)

    def test_no_retired_name_and_public_names_exist(self) -> None:
        retired = "".join(chr(c) for c in (118, 101, 114, 115, 111))   # the retired working name
        self.assertNotIn(retired, self.SRC.lower())
        for name in lp.__all__:
            self.assertTrue(hasattr(lp, name), name)


if __name__ == "__main__":
    unittest.main()
