"""PDF import, lazy rendering and reader integration, on isolated state."""
from __future__ import annotations
import hashlib
import io
import os
from pathlib import Path
import runpy
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox")
ROOT = Path(os.environ.get("BOOK_READER_TEST_ROOT", Path(__file__).resolve().parents[1]))
IMPLEMENTATION = Path(os.environ.get("BOOK_READER_PDF_IMPL", ROOT))
sys.path[:0] = [str(IMPLEMENTATION), str(ROOT)]
import bookformats
import pdfbook
from epublib import EpubError
import pymupdf
from PIL import Image, ImageDraw


def sample_pdf(path: Path, *, scanned=False, count=3, password=False):
    document = pymupdf.open()
    for i in range(count):
        page = document.new_page(width=400, height=600)
        if scanned:
            im = Image.new("RGB", (400, 600), "white")
            ImageDraw.Draw(im).rectangle((30, 50, 200, 150), fill=(15, 90 + i * 10, 210))
            buffer = io.BytesIO(); im.save(buffer, "PNG")
            page.insert_image(page.rect, stream=buffer.getvalue())
        else:
            page.insert_text((30, 60), f"PDF searchable alpha page {i + 1}", fontsize=16)
            page.draw_rect(pymupdf.Rect(30, 100, 150, 200), color=(1, 0, 0), fill=(1, 0, 0))
            if i == 1:
                page.set_rotation(90)
    document.set_metadata({"title": "PDF sample_title", "author": "PDF Test Author"})
    document.set_toc([[1, "Opening", 1], [1, "Second chapter", min(2, count)]])
    if count > 1:
        document[0].insert_link({"kind": pymupdf.LINK_GOTO, "from": pymupdf.Rect(25, 40, 250, 75),
                                 "page": 1, "to": pymupdf.Point(0, 0)})
    options = {"encryption": pymupdf.PDF_ENCRYPT_AES_256, "user_pw": "secret", "owner_pw": "owner"} if password else {}
    document.save(path, **options)
    document.close()
    return path


class PdfAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="reader-pdf-test-")
        self.folder = Path(self.tmp.name)
        self.source = sample_pdf(self.folder / "中文 sample_作者.pdf")
        self.before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.cache = str(self.folder / "cache")

    def tearDown(self):
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.before)
        self.tmp.cleanup()

    def test_content_detection_and_uppercase_extension(self):
        self.assertEqual(bookformats.sniff(self.source), "pdf")
        self.assertTrue(bookformats.is_book_file("SOME.PDF"))
        renamed = self.folder / "file_without_extension"
        renamed.write_bytes(self.source.read_bytes())
        with bookformats.open_book(renamed, cache_root=self.cache) as book:
            self.assertEqual(book.source_format, "pdf")

    def test_metadata_outline_text_links_and_lazy_page_cache(self):
        with bookformats.open_book(self.source, cache_root=self.cache) as book:
            self.assertEqual(book.path, str(self.source))
            self.assertTrue(book.is_fixed_layout)
            self.assertEqual(len(book.spine), 3)
            self.assertEqual(book.metadata["title"], "PDF sample_title")
            self.assertEqual(book.metadata["authors"], ["PDF Test Author"])
            self.assertEqual([t.title for t in book.toc], ["Opening", "Second chapter"])
            self.assertEqual(len(book.search("searchable alpha")), 3)
            self.assertEqual(len(book._pages), 0)
            html = book.read_text(book.spine[0].zip_name)
            self.assertIn('href="p000002.xhtml"', html)
            self.assertIn('transform:matrix(0.000000,1.000000,-1.000000,0.000000',
                          book.read_text(book.spine[1].zip_name))

    def test_page_pixels_are_rendered_on_demand_with_rotation(self):
        with bookformats.open_book(self.source, cache_root=self.cache) as book:
            image = Image.open(io.BytesIO(book.cover_bytes())).convert("RGB")
            self.assertEqual(image.size, (2000, 3000))
            self.assertEqual(image.getpixel((300, 700)), (255, 0, 0))
            landscape = Image.open(io.BytesIO(book.read("OEBPS/pages/p000002.png")))
            self.assertGreater(landscape.width, landscape.height)
            self.assertEqual(len(book._pages), 2)

    def test_cached_structure_reused_and_render_cache_is_bounded(self):
        large = sample_pdf(self.folder / "many.pdf", count=11)
        with bookformats.open_book(large, cache_root=self.cache) as book:
            for i in range(11):
                book.read(f"OEBPS/pages/p{i + 1:06}.png")
            self.assertLessEqual(len(book._pages), pdfbook.PAGE_CACHE_COUNT)
            self.assertLessEqual(book._page_bytes, pdfbook.PAGE_CACHE_BYTES)
        with patch.object(pdfbook, "_build_epub", side_effect=AssertionError("reconverted")):
            with bookformats.open_book(large, cache_root=self.cache) as book:
                self.assertEqual(len(book.spine), 11)

    def test_parallel_workers_receive_complete_page_images(self):
        with bookformats.open_book(self.source, cache_root=self.cache) as book:
            names = [f"OEBPS/pages/p{i % 3 + 1:06}.png" for i in range(9)]
            with ThreadPoolExecutor(max_workers=3) as pool:
                data = list(pool.map(book.read, names))
            self.assertTrue(all(b.startswith(b"\x89PNG") for b in data))
            self.assertEqual(data[0], data[3])

    def test_password_and_corrupt_files_have_distinct_errors(self):
        encrypted = sample_pdf(self.folder / "password.pdf", password=True)
        with self.assertRaises(EpubError) as caught:
            bookformats.open_book(encrypted, cache_root=self.cache)
        self.assertEqual(caught.exception.kind, "password")
        corrupt = self.folder / "corrupt.pdf"; corrupt.write_bytes(b"%PDF-1.7\ninvalid data")
        with self.assertRaises(EpubError) as caught:
            bookformats.open_book(corrupt, cache_root=self.cache)
        self.assertEqual(caught.exception.kind, "corrupt")


# Register epub:// before creating the Qt application.
import epub_reader as er
import library_page
import strings
import theme
from store import Store
from PySide6.QtCore import QCoreApplication, QEvent, QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
if IMPLEMENTATION != ROOT:
    for code, file in (("en", "en"), ("zh-Hans", "zh_Hans"), ("zh-Hant", "zh_Hant"), ("ja", "ja")):
        strings.TABLES[code].update(runpy.run_path(str(IMPLEMENTATION / "i18n" / (file + ".py")))["TABLE"])


class PdfReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        cls.theme = theme.ThemeController(cls.app, "light", cls.app)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="reader-pdf-ui-")
        self.folder = Path(self.tmp.name)
        self.path = sample_pdf(self.folder / "demo_unchanged.pdf")
        self.store = Store(str(self.folder / "state"), start_writer=False)
        self.store.set("ui.language", "en")
        self.windows = []
        er.MainWindow._quitting = False

    def tearDown(self):
        er.MainWindow._quitting = True
        for win in self.windows:
            win.close(); win.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents(); self.store.close(); self.tmp.cleanup()
        er.MainWindow._quitting = False

    def window(self, path=None):
        win = er.MainWindow(path=path, store=self.store, theme_controller=self.theme)
        self.windows.append(win); win.show(); self.app.processEvents()
        return win

    def wait(self, predicate, timeout=15):
        end = time.monotonic() + timeout
        while not predicate() and time.monotonic() < end:
            self.app.processEvents(); QTest.qWait(15)
        self.assertTrue(predicate(), "PDF reader did not become ready")

    def ready(self, reader, page):
        self.wait(lambda: reader.is_ready() and reader.spine_index == page and bool(reader.page_state))

    def test_pdf_reads_inside_application_with_page_navigation_and_search(self):
        win = self.window(str(self.path)); reader = win.active_reader; self.ready(reader, 0)
        self.assertEqual(reader.source_format(), "pdf")
        self.assertTrue(reader.page_state["fixedLayout"])
        self.assertEqual(reader.statusbar.center.text(), "Page 1 of 3")
        self.assertEqual(len(reader.book.search("searchable")), 3)
        reader.focus_book(); QTest.keyClick(reader.view.focusProxy() or reader.view, Qt.Key.Key_PageDown)
        self.ready(reader, 1)
        self.assertEqual(reader.statusbar.center.text(), "Page 2 of 3")
        reader.toolbar.retranslate_ui()
        self.assertFalse(reader.toolbar.actions["convert"].isVisible())
        self.assertEqual(self.store.library_get(reader.book_id)["format"], "PDF")

    def test_scanned_pdf_progress_and_restart_do_not_need_text(self):
        scanned = sample_pdf(self.folder / "scanned.pdf", scanned=True)
        win = self.window(str(scanned)); reader = win.active_reader; self.ready(reader, 0)
        self.assertEqual(reader._total_chars, 0)
        reader.goto_percent(60); self.ready(reader, 1)
        self.assertAlmostEqual(reader._book_fraction(), 2 / 3)
        reader.toggle_bookmark()
        self.wait(lambda: bool(self.store.book_state(reader.book_id)["bookmarks"]))
        bid = reader.book_id
        win.close()
        other = self.window(str(scanned)); self.ready(other.active_reader, 1)
        self.assertTrue(self.store.book_state(bid)["bookmarks"])
        self.assertEqual(other.active_reader.statusbar.center.text(), "Page 2 of 3")

    def test_drop_opens_pdf_in_new_tab_and_moving_window_preserves_page(self):
        src = self.window(); src.open_path(str(ROOT / "tests/fixtures/epub2_ncx.epub"))
        self.ready(src.active_reader, 0)
        mime = QMimeData(); mime.setUrls([QUrl.fromLocalFile(str(self.path))])
        event = QDropEvent(QPointF(40, 200), Qt.DropAction.CopyAction, mime,
                           Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        src.dropEvent(event)
        self.assertTrue(event.isAccepted()); self.assertEqual(len(src.tabs), 2)
        reader = src.active_reader; self.ready(reader, 0)
        reader.next_chapter(); self.ready(reader, 1)
        dst = self.window(); src._move_tab_to_window(src.tabbar.currentIndex(), dst)
        self.assertIs(dst.active_reader, reader); self.assertEqual(reader.spine_index, 1)
        self.assertIs(reader.parentWidget(), dst.stack)

    def test_open_dialog_and_library_accept_pdf_without_conversion_action(self):
        win = self.window()
        with patch.object(library_page.QFileDialog, "getOpenFileNames", return_value=([str(self.path)], "PDF (*.pdf)")) as dialog:
            win.open_dialog()
        self.wait(lambda: win.active_reader is not None and win.active_reader.book is not None)
        self.ready(win.active_reader, 0)
        self.assertIn("*.pdf", dialog.call_args.args[-1])
        entry = self.store.library_get(win.active_reader.book_id)
        self.assertEqual(entry["format"], "PDF")
        self.assertEqual(entry["spine_count"], 3)
        menu = win.library.build_context_menu(win.active_reader.book_id)
        conversion = next(a for a in menu.actions() if a.objectName() == "ctx.convert")
        self.assertFalse(conversion.isVisible()); menu.deleteLater()

    def test_pdf_password_error_stays_inside_the_app_and_survives_library_import(self):
        encrypted = sample_pdf(self.folder / "protected.pdf", password=True)
        win = self.window(str(encrypted))
        self.assertIsNone(win.active_reader.book)
        self.assertEqual(win.active_reader.error_spec()["kind"], "password")
        self.assertIn("password", win.active_reader.error_card.body.text().lower())
        entry = library_page.read_library_entry(str(encrypted), self.store.book_id_for(str(encrypted)),
                                                cache_root=self.store.cache_root)
        self.assertTrue(entry["password_protected"])
        self.assertEqual(entry["format"], "PDF")

    def test_pdf_image_and_transparent_search_text_reach_chromium(self):
        win = self.window(str(self.path)); reader = win.active_reader; self.ready(reader, 0)
        result = {}
        reader.host.run_json('({images:Array.from(document.images).map(i=>[i.complete,i.naturalWidth,i.naturalHeight]),'
                             'text:document.body.innerText,wordColor:getComputedStyle(document.querySelector(".pdf-word")).color})',
                             lambda value: result.update(value or {}))
        self.wait(lambda: bool(result))
        self.assertEqual(result["images"], [[True, 2000, 3000]])
        self.assertIn("PDF searchable alpha page 1", " ".join(result["text"].split()))
        self.assertIn(result["wordColor"], ("rgba(0, 0, 0, 0)", "transparent"))
        reader._on_internal_link("OEBPS/pages/p000002.xhtml", "")
        self.ready(reader, 1)
        self.assertEqual(reader.statusbar.center.text(), "Page 2 of 3")


if __name__ == "__main__":
    unittest.main(verbosity=2)
