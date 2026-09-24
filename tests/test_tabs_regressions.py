"""Real Qt widgets and reader regression checks for browser-style tabs.

Run in a separate process; state lives in temporary folders and the user
profile/instance pipe are never opened. Qt offscreen avoids touching the desktop.
    python -B tests/test_tabs_regressions.py
"""
from __future__ import annotations

import os
import json
from pathlib import Path
import sys
import site
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox")
ROOT = Path(os.environ.get("BOOK_READER_TEST_ROOT", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT))
import epub_reader as er
from PySide6.QtCore import QCoreApplication, QEvent, QMimeData, QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu
from store import Store
import theme


class TabDrop(QDropEvent):
    """Qt drop event with an in-process source, normally provided by QDrag."""
    def __init__(self, source, token, pos):
        self.mime = QMimeData()
        self.mime.setData(er.TabBar.MIME_TYPE, token.encode("ascii"))
        super().__init__(QPointF(pos), Qt.DropAction.MoveAction, self.mime,
                         Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        self._source = source

    def source(self):
        return self._source


class TabsRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        cls.controller = theme.ThemeController(cls.app, "light", cls.app)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="bookreader-tabs-")
        self.store = Store(self.directory.name, start_writer=False)
        self.store.set("ui.language", "en")
        self.windows = []
        er.MainWindow._quitting = False

    def tearDown(self):
        er.MainWindow._quitting = True
        for win in list(er.WINDOWS.live()):
            win.close()
        for win in self.windows:
            win.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.store.close()
        self.directory.cleanup()
        er.MainWindow._quitting = False

    def window(self):
        win = er.MainWindow(store=self.store, theme_controller=self.controller)
        self.windows.append(win)
        win._session_ready = True
        win.show()
        self.app.processEvents()
        return win

    def wait(self, condition, timeout=12):
        end = time.monotonic() + timeout
        while not condition() and time.monotonic() < end:
            self.app.processEvents()
            QTest.qWait(15)
        self.assertTrue(condition(), "Qt condition did not complete")

    def assert_consistent(self, win):
        self.assertEqual(win.tabbar.count(), len(win.tabs))
        self.assertEqual([win.tabbar.tabData(i) for i in range(win.tabbar.count())],
                         [t.token for t in win.tabs])
        if win.tabs:
            self.assertIs(win.tabs[win.tabbar.currentIndex()], win._active_tab)
            current = win.library if win._active_tab.is_library else win._active_tab.reader
            self.assertIs(win.stack.currentWidget(), current)

    def test_plus_and_ctrl_t_create_distinct_shelves_and_ctrl_w_only_closes_one(self):
        win = self.window()
        first = win.tabs[0]
        QTest.mouseClick(win.new_tab_btn, Qt.MouseButton.LeftButton)
        QTest.keyClick(win.library, Qt.Key.Key_T, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(len(win.tabs), 3)
        self.assertEqual(len({t.token for t in win.tabs}), 3)
        self.assertIs(win.tabs[0], first)
        QTest.keyClick(win.library, Qt.Key.Key_W, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(len(win.tabs), 2)
        self.assertFalse(win._shut)
        self.assert_consistent(win)

    def test_middle_click_identical_shelf_removes_the_clicked_instance(self):
        win = self.window()
        win.new_library_tab()
        win.new_library_tab()
        first, second, third = win.tabs
        QTest.mouseClick(win.tabbar, Qt.MouseButton.MiddleButton,
                         pos=win.tabbar.tabRect(1).center())
        self.assertEqual(win.tabs, [first, third])
        self.assertNotIn(second, win.tabs)
        self.assert_consistent(win)

    def test_native_horizontal_drag_reorders_without_starting_external_drag(self):
        win = self.window()
        win.new_library_tab()
        win.new_library_tab()
        first = win.tabs[0]
        bar = win.tabbar
        start = bar.tabRect(0).center()
        end = bar.tabRect(2).center()
        with patch.object(bar, "_begin_drag") as external:
            QTest.mousePress(bar, Qt.MouseButton.LeftButton, pos=start)
            QTest.mouseMove(bar, start + QPoint(QApplication.startDragDistance() + 4, 0), 30)
            QTest.mouseMove(bar, end, 80)
            QTest.mouseRelease(bar, Qt.MouseButton.LeftButton, pos=end)
            external.assert_not_called()
        self.assertEqual(win.tabs[-1], first)
        self.assert_consistent(win)

    def test_leaving_strip_starts_drag_for_the_same_tab_after_native_reorder(self):
        win = self.window()
        win.new_library_tab()
        win.new_library_tab()
        dragged = win.tabs[0]
        bar = win.tabbar
        with patch.object(bar, "_begin_drag") as external:
            QTest.mousePress(bar, Qt.MouseButton.LeftButton, pos=bar.tabRect(0).center())
            QTest.mouseMove(bar, bar.tabRect(2).center(), 60)
            index = win._index_of(dragged)
            QTest.mouseMove(bar, QPoint(100, bar.height() + 35), 60)
            external.assert_called_once_with(index)
            QTest.mouseRelease(bar, Qt.MouseButton.LeftButton, pos=QPoint(100, bar.height() + 35))
        self.assert_consistent(win)

    def test_drag_back_into_own_strip_reorders_at_drop_gap(self):
        win = self.window()
        win.new_library_tab()
        win.new_library_tab()
        first, second, third = win.tabs
        event = TabDrop(win.tabbar, third.token, win.tabbar.tabRect(0).topLeft() + QPoint(2, 2))
        win.tabbar.dropEvent(event)
        self.assertTrue(event.isAccepted())
        self.assertEqual(win.tabs, [third, first, second])
        self.assert_consistent(win)

    def test_cross_window_drop_uses_identity_after_reorder_and_insertion_gap(self):
        src, dst = self.window(), self.window()
        src.new_library_tab()
        src.new_library_tab()
        dst.new_library_tab()
        moved = src.tabs[0]
        event = TabDrop(src.tabbar, moved.token, dst.tabbar.tabRect(0).topLeft() + QPoint(2, 2))
        src.tabbar.moveTab(0, 2)  # nested event loop changed indices after drag began
        dst.tabbar.dropEvent(event)
        self.assertTrue(event.isAccepted())
        self.assertIs(dst.tabs[0], moved)
        self.assertNotIn(moved, src.tabs)
        self.assert_consistent(src)
        self.assert_consistent(dst)

    def test_stale_or_foreign_drag_is_rejected(self):
        src, dst = self.window(), self.window()
        src.new_library_tab()
        stale = src.tabs[0].token
        src.close_tab(0)
        for source, token in ((src.tabbar, stale), (None, src.tabs[0].token)):
            event = TabDrop(source, token, QPoint(4, 4))
            dst.tabbar.dropEvent(event)
            self.assertFalse(event.isAccepted())
        self.assertEqual(len(dst.tabs), 1)

    def test_tearoff_last_tab_moves_it_closes_source_and_has_no_extra_blank_tab(self):
        src = self.window()
        tab = src.tabs[0]
        src._detach_tab_to_new_window(0, QPoint(500, 300))
        dst = er.WINDOWS.live()[0]
        self.windows.append(dst)
        self.assertTrue(src._shut)
        self.assertIsNot(dst, src)
        self.assertEqual(dst.tabs, [tab])
        self.assert_consistent(dst)

    def test_context_menu_moves_the_selected_tab_to_a_new_window(self):
        src = self.window()
        src.new_library_tab()
        moved = src.tabs[0]
        def choose():
            menu = self.app.activePopupWidget()
            self.assertIsNotNone(menu)
            menu.actions()[-1].trigger()
            menu.close()
        QTimer.singleShot(30, choose)
        src.tabbar._menu(src.tabbar.tabRect(0).center())
        dst = next(w for w in er.WINDOWS.live() if w is not src)
        self.windows.append(dst)
        self.assertEqual(dst.tabs, [moved])
        self.assertNotIn(moved, src.tabs)
        self.assert_consistent(src)
        self.assert_consistent(dst)

    def test_context_menu_moves_to_an_existing_window(self):
        src, dst = self.window(), self.window()
        moved = src.tabs[0]
        def choose():
            menu = self.app.activePopupWidget()
            destinations = menu.findChild(QMenu, "tabbar-window-menu")
            self.assertEqual(len(destinations.actions()), 1)
            destinations.actions()[0].trigger()
            menu.close()
        QTimer.singleShot(30, choose)
        src.tabbar._menu(src.tabbar.tabRect(0).center())
        self.assertTrue(src._shut)
        self.assertIs(dst.tabs[-1], moved)
        self.assert_consistent(dst)

    def test_windows_release_before_drag_start_never_enters_native_drag(self):
        src, dst = self.window(), self.window()
        moved = src.tabs[0]
        point = dst.tabbar.mapToGlobal(QPoint(2, 2))
        with patch.object(er.sys, "platform", "win32"), \
             patch.object(src.tabbar, "_left_button_down", return_value=False), \
             patch.object(er.QCursor, "pos", return_value=point), \
             patch.object(QApplication, "widgetAt", return_value=dst.tabbar), \
             patch.object(er, "QDrag") as native:
            src.tabbar._begin_drag(0)
            native.assert_not_called()
        self.assertTrue(src._shut)
        self.assertIs(dst.tabs[0], moved)
        self.assert_consistent(dst)

    def test_windows_release_before_drag_start_tears_off_over_desktop(self):
        src = self.window()
        moved = src.tabs[0]
        with patch.object(er.sys, "platform", "win32"), \
             patch.object(src.tabbar, "_left_button_down", return_value=False), \
             patch.object(QApplication, "widgetAt", return_value=None), \
             patch.object(er, "QDrag") as native:
            src.tabbar._begin_drag(0)
            native.assert_not_called()
        dst = er.WINDOWS.live()[0]
        self.windows.append(dst)
        self.assertTrue(src._shut)
        self.assertEqual(dst.tabs, [moved])
        self.assert_consistent(dst)

    def test_escape_does_not_tearoff_but_desktop_release_does(self):
        win = self.window()
        token = win.tabs[0].token
        point = win.tabbar.mapToGlobal(QPoint(80, 300))
        with patch.object(win.tabbar, "detachTabRequested") as signal:
            win.tabbar.eventFilter(self.app, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                                                     Qt.KeyboardModifier.NoModifier))
            win.tabbar._finish_drag(token, Qt.DropAction.IgnoreAction, point)
            signal.emit.assert_not_called()
            win.tabbar._drag_cancelled = False
            win.tabbar._finish_drag(token, Qt.DropAction.IgnoreAction, point)
            signal.emit.assert_called_once_with(0, point)

    def test_session_restores_every_shelf_and_active_instance(self):
        win = self.window()
        win.new_library_tab()
        win.new_library_tab()
        win._activate_tab(1)
        session = win._session_slice()
        dst = self.window()
        dst.restore_session(session)
        self.assertEqual(len(dst.tabs), 3)
        self.assertTrue(all(t.is_library for t in dst.tabs))
        self.assertEqual(dst.tabbar.currentIndex(), 1)
        self.assert_consistent(dst)

    def test_real_startup_restores_book_first_window_and_other_window(self):
        src, dst = self.window(), self.window()
        src.open_path(str(ROOT / "tests" / "fixtures" / "epub2_ncx.epub"))
        dst.open_path(str(ROOT / "tests" / "fixtures" / "epub3_nav.epub"))
        dst.new_library_tab()
        dst.new_library_tab()
        dst._save_session()
        expected = [[t.kind for t in src.tabs], [t.kind for t in dst.tabs]]
        self.store.flush()
        env = dict(os.environ, APPDATA=str(Path(self.directory.name) / "Roaming"),
                   LOCALAPPDATA=str(Path(self.directory.name) / "Local"),
                   PYTHONUSERBASE=os.environ.get("PYTHONUSERBASE") or site.getuserbase(),
                   BOOK_READER_TEST_ROOT=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, "-B", __file__, "--restore-child", self.directory.name],
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                env=env, timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        line = next(line for line in result.stdout.splitlines() if line.startswith("RESTORED="))
        self.assertEqual(json.loads(line.split("=", 1)[1]), expected)

    def test_shared_store_and_instance_outlive_the_first_window(self):
        src, dst = self.window(), self.window()
        src._own_store = True
        inst = er.SingleInstance("bookreader-tabs-test-" + uuid.uuid4().hex)
        self.assertTrue(inst.listen())
        src.attach_instance(inst)
        src.close()
        self.assertFalse(self.store._closed)
        self.assertTrue(dst._own_store)
        self.assertIs(dst.instance, inst)
        self.assertTrue(inst.server.isListening())
        inst.messageReceived.emit("ACTIVATE")
        self.assertEqual(len(dst.tabs), 2)
        dst.close()
        self.assertTrue(self.store._closed)
        self.assertIsNone(inst.server)

    def test_live_reader_move_preserves_bookmarks_and_rebinds_after_source_destruction(self):
        src, dst = self.window(), self.window()
        src.open_path(str(ROOT / "tests" / "fixtures" / "big_book.epub"))
        reader = src.active_reader
        self.wait(lambda: reader.is_ready() and bool(reader.page_state))
        reader.next_page()
        self.wait(lambda: reader.page_state.get("page", 0) > 0)
        reader.toggle_bookmark()
        self.wait(lambda: bool(self.store.book_state(reader.book_id)["bookmarks"]))
        bookmarks = self.store.book_state(reader.book_id)["bookmarks"]
        book = reader.book
        host = reader.host
        tab = src._active_tab
        serial = reader._load_serial
        before_page = reader.page_state.get("page")
        dst.tabbar.dropEvent(TabDrop(src.tabbar, tab.token, QPoint(2, 2)))
        self.assertTrue(src._shut)
        self.assertIs(dst.active_reader, reader)
        self.assertIs(reader.parentWidget(), dst.stack)
        self.assertIs(reader.book, book)
        self.assertIs(reader.host, host)
        self.assertEqual(reader._load_serial, serial)
        self.assertEqual(reader.page_state.get("page"), before_page)
        self.assertEqual(self.store.book_state(reader.book_id)["bookmarks"], bookmarks)
        src.deleteLater()
        self.windows.remove(src)
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        callback_errors = []
        with patch.object(sys, "excepthook", lambda *exc: callback_errors.append(exc)):
            self.controller.set_choice("sepia")
            self.app.processEvents()
            self.controller.set_choice("light")
        self.assertFalse(callback_errors, callback_errors)
        reader.titleChanged.emit("Moved book title")
        self.assertEqual(dst.tabbar.tabText(dst.tabbar.currentIndex()), "big_book.epub")
        self.assertEqual(dst.windowTitle(), "Moved book title")
        reader.pageKeyUnhandled.emit("J")
        self.wait(lambda: reader.page_state.get("page", 0) > before_page)
        reader.back_to_library()
        self.assertNotIn(tab, dst.tabs)
        self.assertTrue(dst._active_tab.is_library)
        self.assertFalse(dst._shut)
        self.assert_consistent(dst)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--restore-child":
        state_root = sys.argv[2]
        def started(win):
            print("RESTORED=" + json.dumps([[t.kind for t in w.tabs] for w in er.WINDOWS.live()]),
                  flush=True)
            QTimer.singleShot(0, win.request_quit)
        with patch.object(er, "Store", lambda: Store(state_root, start_writer=False)), \
             patch.object(er, "_quick_setting", return_value=False), \
             patch.object(er.store_mod, "migrate_legacy_dirs", return_value=[]):
            sys.exit(er.main([str(ROOT / "epub_reader.py")], on_started=started))
    unittest.main(verbosity=2)
