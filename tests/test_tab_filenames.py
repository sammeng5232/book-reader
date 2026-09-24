"""Tab captions identify source filenames; window titles can use book metadata."""
import hashlib
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox")
ROOT = Path(os.environ.get("BOOK_READER_TEST_ROOT", Path(__file__).resolve().parents[1]))
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
import epub_reader as er
import test_tabs_regressions as fixtures
from test_pdfbook import sample_pdf
from strings import S
from PySide6.QtWidgets import QTabBar


class TabFilenameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.TabsRegressionTests.setUpClass()
        cls.app = fixtures.TabsRegressionTests.app
        cls.controller = fixtures.TabsRegressionTests.controller

    setUp = fixtures.TabsRegressionTests.setUp
    tearDown = fixtures.TabsRegressionTests.tearDown
    window = fixtures.TabsRegressionTests.window
    wait = fixtures.TabsRegressionTests.wait

    def assert_file_tab(self, win, filename, index=None):
        if index is None:
            index = win.tabbar.currentIndex()
        self.assertEqual(win.tabbar.tabText(index), filename)
        self.assertEqual(win.tabbar.tabToolTip(index), filename)

    def copy_epub(self, name):
        target = Path(self.directory.name) / name
        shutil.copyfile(ROOT / "tests/fixtures/epub3_nav.epub", target)
        return target

    def test_epub_filename_extension_unicode_and_multiple_dots_survive_title_signals(self):
        name = "数学_作者.v2.全文_日本語.EPUB"
        source = self.copy_epub(name)
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        win = self.window(); win.open_path(str(source))
        self.assert_file_tab(win, name)
        reader = win.active_reader
        self.assertNotEqual(reader.book.metadata["title"], source.stem)
        self.assertIn(reader.book.metadata["title"], win.windowTitle())
        reader.titleChanged.emit("Entirely different metadata title")
        self.assert_file_tab(win, name)
        self.assertEqual(win.windowTitle(), "Entirely different metadata title")
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)

    def test_pdf_and_djvu_tab_use_original_extension_not_metadata_or_cache_path(self):
        pdf = sample_pdf(Path(self.directory.name) / "文档_作者.rev.4.PDF")
        djvu = Path(self.directory.name) / "扫描_作者.full.2.DJVU"
        shutil.copyfile(ROOT / "tests/samples/ia_jstor_20637537.djvu", djvu)
        for source in (pdf, djvu):
            with self.subTest(format=source.suffix):
                before = hashlib.sha256(source.read_bytes()).hexdigest()
                win = self.window(); win.open_path(str(source))
                self.assertIsNotNone(win.active_reader.book)
                win.active_reader.book.metadata["title"] = "Metadata deliberately differs from filename"
                win.active_reader._emit_title()
                self.assert_file_tab(win, source.name)
                self.assertIn("Metadata deliberately differs", win.windowTitle())
                self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)

    def test_full_filename_tooltip_stays_complete_when_tab_is_visually_elided(self):
        name = "Unicode_文件_" + "long_metadata_is_not_filename_" * 5 + "作者.final.v3.epub"
        source = self.copy_epub(name)
        win = self.window(); win.resize(720, 520); win.open_path(str(source))
        self.assert_file_tab(win, name)
        self.assertEqual(win.tabbar.elideMode(), er.Qt.TextElideMode.ElideRight)
        self.assertLess(win.tabbar.tabRect(0).width(), win.tabbar.fontMetrics().horizontalAdvance(name))

    def test_move_and_lazy_restore_keep_filename_with_distinct_window_title(self):
        source = self.copy_epub("会话_源文件.backup.5.epub")
        src = self.window(); src.open_path(str(source))
        moved = src._active_tab
        dst = self.window(); src._move_tab_to_window(0, dst)
        self.assert_file_tab(dst, source.name)
        dst.new_library_tab()
        session = dst._session_slice()
        restored = self.window(); restored.restore_session(session)
        index = next(i for i,t in enumerate(restored.tabs) if not t.is_library)
        self.assertIsNone(restored.tabs[index].reader)
        self.assert_file_tab(restored, source.name, index)
        restored._activate_tab(index)
        self.assert_file_tab(restored, source.name)
        self.assertIn(restored.active_reader.book.metadata["title"], restored.windowTitle())
        self.assertEqual(restored.tabs[index].path, str(source))
        self.assertEqual(restored.tabbar.tabText(next(i for i,t in enumerate(restored.tabs) if t.is_library)),
                         S("title.library"))

    def test_relocating_same_book_updates_caption_tooltip_and_duplicate_detection(self):
        source = self.copy_epub("旧位置_作者.part.1.epub")
        target = Path(self.directory.name) / "重新定位_新文件名.part.2.EPUB"
        win = self.window(); win.open_path(str(source))
        reader, bid = win.active_reader, win.active_reader.book_id
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        reader.close_book()
        source.rename(target)
        reader._show_error({"kind":"missing", "path":str(source), "book_id":bid, "in_library":True})
        reader.relocate_to(str(target))
        self.assertIsNotNone(reader.book)
        self.assertEqual(reader.book_id, bid)
        self.assert_file_tab(win, target.name)
        self.assertEqual(win._active_tab.path, str(target))
        self.assertEqual(self.store.library_get(bid)["path"], str(target))
        win.open_path(str(target), new_tab=True)
        self.assertEqual(len(win.tabs), 1)
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
