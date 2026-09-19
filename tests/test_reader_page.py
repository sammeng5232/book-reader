#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verification harness for ``reader_page.py`` (owner E).

Every GUI phase runs in its own process, drives a real ``ReaderPage`` with
``QTimer`` (no human input), asserts, screenshots with ``QWidget.grab()`` and
quits within ~20 s.  Every ``Store`` lives under a temporary root.  The user's
real books are opened read-only and are never copied or modified; the file
moving tests use copies of ``tests/fixtures`` only.

Run everything (prints a PASS/FAIL table, exit 0 only when all pass)::

    C:\\Users\\mengz\\AppData\\Local\\Programs\\Python\\Python314\\python.exe tests\\test_reader_page.py --all

One phase, screenshots into a folder of your choice::

    python tests\\test_reader_page.py --phase real --out C:\\temp\\shots

Phases: unit, fixture, errors, toc, real, i18n.  Under ``unittest`` each phase
runs in a child process; set ``EPUB_READER_SKIP_GUI_TESTS=1`` to skip the GUI
phases.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FIXTURES = os.path.join(HERE, "fixtures")
DEFAULT_OUT = os.path.join(tempfile.gettempdir(), "epub-reader-reader-page")
REAL_BOOKS = [
    r"C:\Users\mengz\Desktop\文件\Econ Books\Econ Books_China\从此岸到彼岸_人民币汇率如何实现清洁浮动_缪延亮.epub",
    r"C:\Users\mengz\Desktop\文件\Econ Books\50人的二十年_樊纲 易纲等.epub",
] + glob.glob(r"C:\Users\mengz\Desktop\文件\CUHK Notes\CUHK_BFRM\Fin Books\*(李向科) (Z-Library).epub")
PHASES = ("unit", "fixture", "errors", "toc", "real", "i18n")
GUI_PHASES = PHASES[1:]
PHASE_TIMEOUT_S = 45


# ==========================================================================
# result recording
# ==========================================================================

class Results:
    def __init__(self, phase: str) -> None:
        self.phase = phase
        self.rows: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((name, bool(ok), detail))
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {self.phase}: {name}" + (f" -- {detail}" if detail else ""), flush=True)
        return bool(ok)

    @property
    def failed(self) -> list[str]:
        return [n for n, ok, _ in self.rows if not ok]


# ==========================================================================
# phase: unit (no window)
# ==========================================================================

def phase_unit(out: str) -> Results:
    r = Results("unit")
    import reader_page as rp
    import strings

    # ---- reading speed: EWMA, clamp, idle gaps, jumps --------------------------
    t = rp.ReadingTracker(300)
    for sec, units in ((0, 0), (30, 150), (60, 300), (100, 600)):
        t.observe(units, sec)
    t.finish(100)
    r.check("tracker: 600 units in 100 s -> sample 360, EWMA 315",
            abs(t.speed - 315.0) < 1e-6 and len(t.samples) == 1, f"speed={t.speed:.2f}")
    t = rp.ReadingTracker(300)
    t.observe(0, 0)
    t.observe(100, 60)
    t.finish(60)
    r.check("tracker: a 60 s session is ignored", t.speed == 300 and not t.samples)
    t = rp.ReadingTracker(300)
    t.observe(0, 0)
    t.observe(50, 60)
    t.observe(100, 400)          # 340 s gap: idle, the segment ends, the time is not counted
    t.observe(200, 460)
    t.finish(460)
    r.check("tracker: idle gap > 120 s is not reading time", t.total_seconds == 120 and not t.samples,
            f"seconds={t.total_seconds}")
    t = rp.ReadingTracker(300)
    t.observe(0, 0)
    t.observe(5000, 30, sequential=False)   # a jump
    t.observe(5100, 100)
    t.finish(100)
    r.check("tracker: jumps are not credited as reading", t.total_units == 100, f"units={t.total_units}")
    t = rp.ReadingTracker(300)
    t.observe(0, 0)
    t.observe(5900, 95)
    t.finish(95)
    r.check("tracker: fast sample clamped to 1200", abs(t.samples[0] - 1200) < 1e-9 and t.speed <= 1200)
    r.check("tracker: seeded at 300, clamped seed", rp.ReadingTracker(None).speed == 300
            and rp.ReadingTracker(5).speed == 120 and rp.ReadingTracker(99999).speed == 1200)

    # ---- the keyboard slot map --------------------------------------------------
    problems = []
    for kid, names in rp.ACTION_SLOTS.items():
        if kid not in strings.KEYS:
            problems.append(f"{kid} not in strings.KEYS")
            continue
        if len(names) > 1 and len(names) != len(strings.KEYS[kid]):
            problems.append(f"{kid}: {len(names)} slots for {len(strings.KEYS[kid])} combos")
        for n in names:
            if not callable(getattr(rp.ReaderPage, n, None)):
                problems.append(f"{kid}: ReaderPage.{n} missing")
    reader_ids = [kid for g, rows in strings.CHEATSHEET_LAYOUT if g != "keys.group.library" for kid, _ in rows]
    missing = [kid for kid in reader_ids if kid not in rp.ACTION_SLOTS]
    r.check("ACTION_SLOTS covers every reader shortcut id with a real slot",
            not problems and not missing, "; ".join(problems + missing))
    bad_page_keys = [k for k, n in rp.PAGE_KEY_ACTIONS.items() if not callable(getattr(rp.ReaderPage, n, None))]
    r.check("PAGE_KEY_ACTIONS slots exist", not bad_page_keys, str(bad_page_keys))

    # ---- string keys used by the module exist in all four tables -----------------
    src = open(os.path.join(ROOT, "reader_page.py"), encoding="utf-8").read()
    used = set(re.findall(r'\bS\(\s*"([a-z0-9_.]+)"', src))
    used |= set(re.findall(r'\btip\(\s*"([a-z0-9_.]+)"', src))
    used |= {k + ".one" for k in re.findall(r'\bplural\(\s*"([a-z0-9_.]+)"', src)}
    used |= {k + ".other" for k in re.findall(r'\bplural\(\s*"([a-z0-9_.]+)"', src)}
    absent = sorted(k for k in used for lang in strings.LANGUAGES if k not in strings.TABLES[lang])
    r.check(f"all {len(used)} literal string keys exist in all four tables", not absent, str(absent[:10]))
    r.check("the retired working name does not appear", "verso" not in src.lower())

    # ---- moved-file search (fixture copies only) ---------------------------------
    tmp = tempfile.mkdtemp(prefix="er-moved-")
    try:
        a, b = os.path.join(tmp, "a"), os.path.join(tmp, "b")
        os.makedirs(a)
        os.makedirs(b)
        src_book = os.path.join(FIXTURES, "epub3_nav.epub")
        old = os.path.join(a, "book.epub")
        shutil.copy2(src_book, old)
        import store as store_mod
        bid = store_mod.book_id(old)
        size = os.path.getsize(old)
        shutil.copy2(os.path.join(FIXTURES, "epub2_ncx.epub"), os.path.join(b, "decoy.epub"))
        new = os.path.join(b, "renamed.epub")
        shutil.move(old, new)
        found = rp.find_moved_file(old, size, bid, [a, b])
        r.check("find_moved_file finds the same bytes under a new name", found and os.path.samefile(found, new),
                str(found))
        r.check("find_moved_file ignores other books", rp.find_moved_file(old, size, "0" * 32, [a, b]) is None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return r


# ==========================================================================
# GUI harness
# ==========================================================================

class Harness:
    """A small window with one ReaderPage over a temporary Store."""

    def __init__(self, phase: str, out: str, *, lang: str = "zh-Hans", size: tuple[int, int] = (1100, 720),
                 theme_choice: str = "light", settings: dict | None = None) -> None:
        import reader_page as rp  # noqa: F401  (imports webhost before QApplication)
        from PySide6.QtWidgets import QApplication, QMainWindow
        import strings
        import theme
        from store import Store

        self.rp = rp
        self.results = Results(phase)
        self.out = out
        os.makedirs(out, exist_ok=True)
        self.app = QApplication.instance() or QApplication(sys.argv)
        self.tmp = tempfile.mkdtemp(prefix=f"er-{phase}-")
        self.store = Store(root=self.tmp)
        for k, v in (settings or {}).items():
            self.store.set(k, v)
        self.store.set("ui.language", lang)
        strings.set_language(lang)
        self.ctl = theme.ThemeController(self.app, theme_choice)
        self.win = QMainWindow()
        self.win.resize(*size)
        self.win.move(80, 60)
        self.page = None
        self.titles: list[str] = []
        self.new_page()
        self.win.show()
        self.pump(0.2)

    # -- lifecycle ------------------------------------------------------------
    def new_page(self):
        page = self.rp.ReaderPage(self.store, theme_controller=self.ctl)
        page.titleChanged.connect(self.win.setWindowTitle)
        page.titleChanged.connect(self.titles.append)
        self.win.setCentralWidget(page)
        self.page = page
        return page

    def destroy_page(self) -> None:
        page = self.page
        page.shutdown()
        self.win.takeCentralWidget()
        page.setParent(None)
        page.deleteLater()
        self.page = None
        self.pump(0.3)

    def finish(self) -> None:
        try:
            if self.page is not None:
                self.page.shutdown()
            self.store.close()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    # -- waiting --------------------------------------------------------------
    def pump(self, sec: float) -> None:
        from PySide6.QtCore import QEventLoop

        end = time.monotonic() + sec
        while time.monotonic() < end:
            self.app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)

    def wait(self, pred, timeout: float = 8.0, what: str = "") -> bool:
        from PySide6.QtCore import QEventLoop

        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
            try:
                if pred():
                    return True
            except Exception:  # noqa: BLE001
                pass
        raise AssertionError(f"timed out after {timeout}s waiting for {what}")

    def ready(self, spine: int | None = None, timeout: float = 10.0) -> None:
        p = self.page
        self.wait(lambda: p.is_ready() and (spine is None or p.spine_index == spine) and p.page_state,
                  timeout, f"chapter {spine} ready")
        self.pump(0.25)

    def call(self, method: str, *args, timeout: float = 5.0):
        box: dict = {}
        self.page.host.call_reader(method, *args, callback=lambda v: box.setdefault("v", v))
        self.wait(lambda: "v" in box, timeout, f"epubReader.{method}")
        return box["v"]

    def js(self, expr: str, timeout: float = 5.0):
        box: dict = {}
        self.page.host.run_json(expr, lambda v: box.setdefault("v", v))
        self.wait(lambda: "v" in box, timeout, "js")
        return box["v"]

    def shot(self, name: str, widget=None) -> str:
        path = os.path.join(self.out, name + ".png")
        (widget or self.win).grab().save(path)
        print(f"  shot {path}", flush=True)
        return path

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        return self.results.check(name, ok, detail)

    def select_visible_text(self, n: int = 10) -> str:
        """Select *n* characters of text near the middle of the current screen."""
        return self.js(
            "(function(){var xs=[0.5,0.4,0.6,0.3,0.7],ys=[0.3,0.4,0.5,0.25,0.6];"
            "for(var i=0;i<xs.length;i++){for(var j=0;j<ys.length;j++){"
            "var r=document.caretRangeFromPoint(innerWidth*xs[i],innerHeight*ys[j]);"
            "if(!r||r.startContainer.nodeType!==3)continue;var t=r.startContainer,v=t.nodeValue;"
            "var s=Math.max(0,Math.min(r.startOffset,v.length-" + str(n) + "));"
            "if(v.slice(s,s+" + str(n) + ").trim().length<" + str(n) + ")continue;"
            "var g=document.createRange();g.setStart(t,s);g.setEnd(t,s+" + str(n) + ");"
            "var sel=getSelection();sel.removeAllRanges();sel.addRange(g);return sel.toString();}}"
            "return ''})()")


def run_gui_phase(phase: str, out: str) -> Results:
    from PySide6.QtCore import QTimer

    holder: dict = {}

    def body() -> None:
        try:
            holder["r"] = GUI[phase](out)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            r = holder.get("r") or Results(phase)
            r.check("phase ran to completion", False, traceback.format_exc().strip().splitlines()[-1])
            holder["r"] = r
        finally:
            from PySide6.QtWidgets import QApplication

            QApplication.instance().quit()

    import reader_page  # noqa: F401  (webhost must be imported before QApplication)
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    QTimer.singleShot(0, body)
    QTimer.singleShot(40000, lambda: (print("WATCHDOG: phase took too long", flush=True), app.exit(3)))
    app.exec()
    return holder.get("r") or Results(phase)


# ==========================================================================
# phase: fixture  (epub2_ncx.epub end to end)
# ==========================================================================

def phase_fixture(out: str) -> Results:
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    import theme
    from strings import S

    h = Harness("fixture", out)
    r = h.results
    p = h.page
    try:
        fx = os.path.join(FIXTURES, "epub2_ncx.epub")
        p.open_book(fx)
        h.ready(0)
        bid = p.book_id
        r.check("opened: title, library entry and bookOpened", bool(bid) and h.store.library_get(bid) is not None
                and h.titles and "EPUB 2 With NCX" in h.titles[-1], h.titles[-1] if h.titles else "")
        r.check("status bar shows percent and time left",
                p.statusbar.center.text().endswith("%") and bool(p.statusbar.right.text()),
                f"{p.statusbar.center.text()!r} {p.statusbar.right.text()!r}")
        r.check("TOC marks the first chapter current", p.toc_current == 0, str(p.toc_current))
        h.shot("f01_open")

        # real key presses into the book view (reader.js handles them in the page)
        proxy = p.view.focusProxy() or p.view
        p.focus_book()
        h.pump(0.1)
        QTest.keyClick(proxy, Qt.Key.Key_Right)
        h.ready(1)
        r.check("Right arrow in the view turns into the next chapter", p.spine_index == 1)
        QTest.keyClick(proxy, Qt.Key.Key_K)          # forwarded as keyUnhandled('K') -> prev_page
        h.ready(0)
        r.check("'k' (forwarded by the page) goes back", p.spine_index == 0)
        r.check("focus stays in the book across chapter loads", p._view_has_focus() or not h.win.isActiveWindow())
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QWheelEvent
        fs0 = h.store.get("reader.font_size_px")
        ev = QWheelEvent(QPointF(100, 100), QPointF(p.view.mapToGlobal(QPoint(100, 100))), QPoint(0, 0),
                         QPoint(0, 120), Qt.MouseButton.NoButton, Qt.KeyboardModifier.ControlModifier,
                         Qt.ScrollPhase.NoScrollPhase, False)
        h.app.sendEvent(proxy, ev)
        h.pump(0.2)
        r.check("Ctrl+wheel in the view steps the font size", h.store.get("reader.font_size_px") == fs0 + 1,
                f"{fs0} -> {h.store.get('reader.font_size_px')}")
        p.font_reset()
        h.pump(0.3)

        # page turn past the end of a one-page chapter loads the next chapter
        p.next_page()
        h.ready(1)
        r.check("turning past the last page opens the next chapter", p.spine_index == 1)
        h.wait(lambda: p.toc_current == 1, 3, "toc current follows")
        r.check("TOC current follows the chapter (Chapter One)", p.dock.toc.title_of(p.toc_current) == "Chapter One",
                p.dock.toc.title_of(p.toc_current))
        p.prev_page()
        h.ready(0)
        r.check("turning before page 1 goes back a chapter", p.spine_index == 0)

        # TOC: a real mouse click on "Chapter Two"
        tree = p.dock.toc.tree
        entries = p.dock.toc.entries
        target = next(i for i, e in enumerate(entries) if e["title"] == "Chapter Two")
        idx = entries[target]["item"].index()
        tree.scrollTo(idx)
        h.pump(0.1)
        QTest.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         tree.visualRect(idx).center())
        h.ready(entries[target]["spine"])
        r.check("TOC single click jumps", p.spine_index == entries[target]["spine"])
        r.check("TOC single click keeps the tree focused", tree.hasFocus() or not h.win.isActiveWindow(),
                "window not active" if not h.win.isActiveWindow() else "")
        h.wait(lambda: p.toc_current == target, 3, "toc current")
        r.check("clicked entry is current (bold + bar)", tree.current_row is not None and tree.current_row == idx
                and entries[target]["item"].font().bold())
        h.shot("f02_toc_click")

        # history: back returns to where the TOC jump started
        p.jump_back()
        h.ready(0)
        r.check("Alt+Left (jump_back) returns before the TOC jump", p.spine_index == 0)
        p.jump_forward()
        h.ready(entries[target]["spine"])
        r.check("Alt+Right (jump_forward) goes forward again", p.spine_index == entries[target]["spine"])

        # nested entry with a fragment
        sub = next(i for i, e in enumerate(entries) if e["title"] == "Section 1.1")
        p._on_toc_jump(sub, False)
        h.ready(entries[sub]["spine"])
        h.wait(lambda: p.toc_current == sub, 3, "section current")
        r.check("fragment TOC entry becomes current", p.toc_current == sub, p.dock.toc.title_of(p.toc_current))

        # bookmarks: add, remove with undo
        p.toggle_bookmark()
        h.wait(lambda: len(h.store.book_state(bid)["bookmarks"]) == 1, 3, "bookmark stored")
        h.pump(0.2)
        bm = h.store.book_state(bid)["bookmarks"][0]
        r.check("Ctrl+D stores a bookmark with locator, chapter and preview",
                bm.get("locator", {}).get("snippet") is not None and bm.get("chapter_title") == "Section 1.1"
                and bool(bm.get("text")), json.dumps({k: bm.get(k) for k in ("chapter_title", "text")},
                                                     ensure_ascii=False)[:120])
        r.check("toolbar shows the page as bookmarked", p.toolbar._bookmarked)
        r.check("bookmark pane lists it", p.dock.bookmarks.list.count() == 1)
        p.toggle_bookmarks()
        h.pump(0.3)
        h.shot("f03_bookmarks")
        p.toggle_bookmark()          # same screen: removes, with an undo link
        h.pump(0.2)
        r.check("Ctrl+D on a bookmarked screen removes it", not h.store.book_state(bid)["bookmarks"])
        r.check("status bar offers undo", "<a href" in p.statusbar.left.text()
                and S("common.undo") in p.statusbar.left.text())
        p.statusbar.trigger_undo()
        h.pump(0.2)
        r.check("undo restores the bookmark", len(h.store.book_state(bid)["bookmarks"]) == 1
                and p.dock.bookmarks.list.count() == 1)

        # highlight: select text in the page, Ctrl+2
        sel = h.select_visible_text(10)
        h.pump(0.2)
        p.highlight_green()
        h.wait(lambda: len(h.store.book_state(bid)["highlights"]) == 1, 3, "highlight stored")
        hl = h.store.book_state(bid)["highlights"][0]
        r.check("Ctrl+2 stores a green highlight of the selection", hl["color"] == "green"
                and hl["text"] == sel and hl["start"]["gpos"] < hl["end"]["gpos"], f"{sel!r} vs {hl['text']!r}")
        h.pump(0.3)
        painted = h.js("CSS.highlights.has('er-hl-green') ? CSS.highlights.get('er-hl-green').size : 0")
        r.check("the highlight is painted in the page", painted == 1, str(painted))
        p.toggle_notes()
        h.pump(0.3)
        r.check("highlights pane shows a chapter group and the row", p.dock.notes.list.count() == 2
                and p.dock.notes.visible_ids() == [hl["id"]])
        p.dock.notes.set_filter(["yellow"])
        r.check("colour filter chips filter", p.dock.notes.visible_ids() == [])
        p.dock.notes.set_filter([])
        p._save_note(hl["id"], "a note", "green")
        h.pump(0.2)
        r.check("note saved", h.store.book_state(bid)["highlights"][0]["note"] == "a note")
        p.dock.notes.set_filter(noted=True)
        r.check("'with notes' chip keeps noted rows", p.dock.notes.visible_ids() == [hl["id"]])
        p.dock.notes.set_filter()
        h.shot("f04_highlight")

        # theme: chrome AND page
        p.set_theme_choice("dark")
        h.pump(0.6)
        bg = h.js("getComputedStyle(document.documentElement).backgroundColor")
        r.check("dark theme reaches the page", bg == "rgb(22, 24, 28)", str(bg))
        r.check("dark theme reaches the chrome", h.app.palette().window().color().name().upper()
                == theme.DARK.chrome_bg.upper(), h.app.palette().window().color().name())
        r.check("theme choice saved", h.store.get("reader.theme") == "dark")
        h.shot("f05_dark")
        p.set_theme_choice("light")
        h.pump(0.3)

        # font size keeps the reading position
        g0 = p.page_state.get("gpos")
        for _ in range(3):
            p.font_larger()
        h.pump(0.8)
        fs = h.js("getComputedStyle(document.documentElement).getPropertyValue('--er-fs').trim()")
        r.check("Ctrl+= x3 -> 24 px in store and page", h.store.get("reader.font_size_px") == 24 and fs == "24px",
                f"store={h.store.get('reader.font_size_px')} page={fs}")
        r.check("font change keeps the same position (gpos)", p.page_state.get("gpos") == g0,
                f"{g0} -> {p.page_state.get('gpos')}")
        h.shot("f06_font24")

        # this-book-only override
        p.set_this_book_only(True)
        p.set_typography("line_height", 2.2)
        p._flush_overrides()
        r.check("'this book only' writes overrides, not global", h.store.book_state(bid)["overrides"]
                == {"line_height": 2.2} and h.store.get("reader.line_height") == 1.9,
                str(h.store.book_state(bid)["overrides"]))
        p.set_this_book_only(False)
        r.check("unchecking deletes the overrides", h.store.book_state(bid)["overrides"] == {})

        # scroll mode and back
        p.toggle_layout_mode()
        h.wait(lambda: p.page_state.get("mode") == "scroll", 3, "scroll mode")
        r.check("Ctrl+M switches to scrolling", h.store.get("reader.layout") == "scroll")
        p.toggle_layout_mode()
        h.wait(lambda: p.page_state.get("mode") == "paginated", 3, "paged mode")

        # go to 50 %
        p.goto_percent(50)
        h.wait(lambda: p.is_ready() and abs(p.page_state.get("percent", 0) - 0.5) < 0.12, 5, "50%")
        r.check("go to 50 % lands near the middle", abs(p.page_state.get("percent", 0) - 0.5) < 0.12,
                str(p.page_state.get("percent")))

        # status bar right cell cycles
        p.cycle_status_cell()
        r.check("right-click cycle: time left in book", p.statusbar.mode == "book"
                and h.store.get("window.status_right") == "book")
        p.statusbar.set_mode("chapter", emit=True)

        # chrome auto-hide and reveal near the top edge (no real cursor movement)
        h.store.set("behavior.auto_hide_chrome_ms", 400)
        p._last_cursor = QPoint(-5, -5)
        p.statusbar.clear_message()          # a pending message keeps the status bar up (by design)
        p.reveal_chrome()
        h.pump(0.1)
        r.check("chrome visible after reveal", p.toolbar.isVisible() and p.statusbar.isVisible())
        h.wait(lambda: not p.toolbar.isVisible(), 3, "auto-hide")
        r.check("toolbar and status bar hide after the idle delay", not p.toolbar.isVisible()
                and not p.statusbar.isVisible())
        p._handle_pointer(p.column.mapToGlobal(QPoint(200, 300)))
        r.check("mouse in the middle does not reveal", not p.toolbar.isVisible())
        p._handle_pointer(p.column.mapToGlobal(QPoint(200, 30)))
        r.check("mouse within 60 px of the top reveals", p.toolbar.isVisible())
        h.shot("f07_chrome")
        h.store.set("behavior.auto_hide_chrome_ms", 3000)
        p.reveal_chrome()

        # overlays and Escape priority
        p.toggle_cheatsheet()
        h.pump(0.2)
        r.check("F1 shows the cheat sheet", p.cheatsheet.isVisible())
        h.shot("f08_cheatsheet")
        p.escape()
        r.check("Esc closes the cheat sheet", not p.cheatsheet.isVisible())
        p.set_settings_visible(True)
        p.set_dock_visible(True)
        h.pump(0.2)
        p.escape()
        r.check("Esc closes the settings panel first", not p.settings_panel.isVisible() and p.dock.isVisible())
        p.escape()
        r.check("then the dock", not p.dock.isVisible())

        # external links are only opened after asking
        p.host.externalLinkRequested.emit("https://example.com/x")
        h.pump(0.2)
        r.check("external link asks first (card, url on its own line)", p.link_confirm.isVisible()
                and p.link_confirm.url_label.text() == "https://example.com/x")
        h.shot("f09_link")
        p.link_confirm.cancel.click()
        r.check("cancel closes it", not p.link_confirm.isVisible())

        # book details and go-to popovers
        p.show_book_info()
        h.pump(0.2)
        r.check("book details card", p.book_info.isVisible() and p.book_info.form.rowCount() >= 6)
        h.shot("f10_bookinfo")
        p.escape()
        p.goto()
        h.pump(0.1)
        r.check("Ctrl+G opens the go-to card", p.goto_popover.isVisible())
        p.escape()

        # export
        md_path = os.path.join(h.tmp, "export.md")
        written = p.export_highlights(md_path)
        text = open(md_path, encoding="utf-8").read() if written else ""
        r.check("export to Markdown", written == md_path and sel in text and "a note" in text
                and text.startswith("# EPUB 2 With NCX"), text[:80].replace("\n", " | "))

        # position is saved on close
        gpos, spine = p.page_state.get("gpos"), p.spine_index
        p.close_book()
        pos = h.store.book_state(bid)["position"]
        r.check("closing saves the position immediately", pos and pos["spine_index"] == spine
                and pos["locator"]["gpos"] == gpos, f"{pos and pos['spine_index']}/{pos and pos['locator']['gpos']}"
                f" vs {spine}/{gpos}")
        r.check("closing emits the library window title", h.titles[-1] == S("title.library"))
    finally:
        h.finish()
    return r


# ==========================================================================
# phase: errors (in-pane cards, moved files, one failing chapter)
# ==========================================================================

def _zip_with_damaged_entry(src: str, dst: str, name: str) -> None:
    """Copy *src* with *name* deflated and a few of its compressed bytes flipped.

    The entry stays in the manifest and spine, but reading it fails the CRC
    check, so the scheme handler cannot serve it: one chapter that cannot be
    displayed, exactly the spec's case (e).
    """
    import struct

    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            ct = zipfile.ZIP_STORED if info.filename == "mimetype" else zipfile.ZIP_DEFLATED
            zout.writestr(info, data, compress_type=ct)
    with zipfile.ZipFile(dst) as z:
        info = z.getinfo(name)
    with open(dst, "r+b") as f:
        f.seek(info.header_offset)
        header = f.read(30)
        n_len, x_len = struct.unpack("<HH", header[26:30])
        start = info.header_offset + 30 + n_len + x_len
        f.seek(start + 4)
        chunk = bytearray(f.read(16))
        f.seek(start + 4)
        f.write(bytes(b ^ 0x5A for b in chunk))


def phase_errors(out: str) -> Results:
    from strings import S

    h = Harness("errors", out, size=(960, 640))
    r = h.results
    p = h.page
    try:
        cases = [("drm_fake.epub", "drm"), ("not_a_zip.epub", "corrupt"), ("no_container.epub", "structure"),
                 ("truncated.epub", "corrupt")]
        for name, kind in cases:
            p.open_book(os.path.join(FIXTURES, name))
            h.pump(0.2)
            spec = p.error_spec() or {}
            card = p.error_card
            ok = spec.get("kind") == kind and p.column.error_page.isVisible() and card.path.text().endswith(name)
            r.check(f"{name}: in-pane '{kind}' card with the path on its own line", ok, str(spec.get("kind")))
            if kind == "drm":
                r.check("DRM card names the scheme and never blames", "Adobe ADEPT" in card.body.text()
                        and card.title.text() == S("err.drm.title") and not card.buttons["remove"].isVisible()
                        and card.buttons["reveal"].isVisible() and card.buttons["close"].isVisible())
            if kind == "corrupt" and name == "not_a_zip.epub":
                card.details_btn.setChecked(True)
                h.pump(0.1)
                r.check("technical details disclosure opens", card.details.isVisible()
                        and bool(card.details.toPlainText()))
            h.shot(f"e_{kind}_{os.path.splitext(name)[0]}")
        r.check("no modal message box was used", not [w for w in h.app.topLevelWidgets()
                                                         if w.inherits("QMessageBox") and w.isVisible()])

        # missing and never shelved: the card straight away
        ghost = os.path.join(h.tmp, "nowhere", "ghost.epub")
        p.open_book(ghost)
        h.pump(0.2)
        r.check("missing file (not in library): missing card", (p.error_spec() or {}).get("kind") == "missing"
                and p.error_card.buttons["relocate"].isVisible())
        h.shot("e_missing")

        # moved file: found again by size + hash, path repaired, status note
        a, b, c = (os.path.join(h.tmp, x) for x in "abc")
        for d in (a, b, c):
            os.makedirs(d)
        book_a = os.path.join(a, "book.epub")
        shutil.copy2(os.path.join(FIXTURES, "epub3_nav.epub"), book_a)
        other_b = os.path.join(b, "other.epub")
        shutil.copy2(os.path.join(FIXTURES, "epub2_ncx.epub"), other_b)
        p.open_book(other_b)
        h.ready()
        p.open_book(book_a)
        h.ready()
        bid_a = p.book_id
        p.toggle_bookmark()
        h.wait(lambda: len(h.store.book_state(bid_a)["bookmarks"]) == 1, 3, "bookmark")
        p.close_book()
        moved = os.path.join(b, "moved here.epub")
        shutil.move(book_a, moved)
        p.open_book(book_a)
        h.wait(lambda: p.book is not None and p.is_ready(), 10, "moved book reopened")
        entry = h.store.library_get(bid_a) or {}
        r.check("moved file found by size + hash in a known folder", os.path.normcase(entry.get("path", ""))
                == os.path.normcase(moved) and book_a in entry.get("path_history", []), entry.get("path", ""))
        r.check("the move is reported quietly in the status bar", S("status.moved") in p.statusbar.left.text())
        h.shot("e_moved_repaired")
        p.close_book()

        # moved to an unknown folder: missing card, then relocate to a DIFFERENT file -> inline confirm
        far = os.path.join(c, "far.epub")
        shutil.move(moved, far)
        p.open_book(moved)
        h.wait(lambda: (p.error_spec() or {}).get("kind") == "missing", 10, "search gives up")
        r.check("not found anywhere known: missing card with the title", "EPUB 3" in p.error_card.body.text(),
                p.error_card.body.text())
        different = os.path.join(c, "different.epub")
        shutil.copy2(os.path.join(FIXTURES, "images_fonts.epub"), different)
        p.relocate_to(different)
        h.pump(0.2)
        r.check("relocating to a different file asks inline", p.error_card.confirm.isVisible()
                and p.error_card.confirm_label.text() == S("err.relocate.mismatch"))
        h.shot("e_relocate_mismatch")
        p._on_error_action("link_yes")
        h.ready()
        r.check("'link anyway' keeps the old id, so the bookmarks follow", p.book_id == bid_a
                and len(h.store.book_state(bid_a)["bookmarks"]) == 1, p.book_id)
        p.close_book()

        # one chapter that cannot be displayed; the rest of the book keeps working
        broken = os.path.join(h.tmp, "broken_chapter.epub")
        _zip_with_damaged_entry(os.path.join(FIXTURES, "epub2_ncx.epub"), broken, "OEBPS/ch1s1.xhtml")
        p.open_book(broken)
        h.ready()
        bad = next((i for i, s in enumerate(p.book.spine) if s.zip_name.endswith("/ch1s1.xhtml")), None)
        if bad is None:
            r.check("broken-chapter fixture", False, str([s.zip_name for s in p.book.spine]))
        else:
            p._goto(bad)
            h.wait(lambda: p.section_card.isVisible(), 8, "section card")
            r.check("a failing chapter shows the in-flow card, not an error page",
                    p.section_card.isVisible() and p.error_spec() is None
                    and p.section_card.title.text() == S("err.section"))
            h.shot("e_section_failed")
            p.section_card.next.click()
            h.ready(bad + 1)
            r.check("navigation keeps working past it", p.spine_index == bad + 1 and not p.section_card.isVisible())
        p.close_book()
    finally:
        h.finish()
    return r


# ==========================================================================
# phase: toc (150-chapter book; auto-scroll and the 5 s user-scroll grace)
# ==========================================================================

def phase_toc(out: str) -> Results:
    h = Harness("toc", out, size=(1000, 640))
    r = h.results
    p = h.page
    try:
        p.open_book(os.path.join(FIXTURES, "big_book.epub"))
        h.ready()
        tree = p.dock.toc.tree

        def row_visible(i: int) -> bool:
            rect = tree.visualRect(p.dock.toc.entries[i]["item"].index())
            return rect.isValid() and tree.viewport().rect().intersects(rect)

        tree.last_user_scroll = 0.0
        p._goto(120)
        h.ready(120)
        h.wait(lambda: p.dock.toc.entries[p.toc_current]["spine"] == 120, 3, "toc current 120")
        h.pump(0.2)
        r.check("TOC auto-scrolls the current chapter into view", row_visible(p.toc_current))
        h.shot("t01_autoscroll")
        from PySide6.QtWidgets import QAbstractSlider

        tree.verticalScrollBar().triggerAction(QAbstractSlider.SliderAction.SliderToMinimum)
        h.pump(0.1)
        p._goto(90)
        h.ready(90)
        h.wait(lambda: p.dock.toc.entries[p.toc_current]["spine"] == 90, 3, "toc current 90")
        h.pump(0.2)
        r.check("within 5 s of a user scroll the tree is left alone", not row_visible(p.toc_current))
        tree.last_user_scroll = time.monotonic() - 6
        p._goto(60)
        h.ready(60)
        h.wait(lambda: p.dock.toc.entries[p.toc_current]["spine"] == 60, 3, "toc current 60")
        h.pump(0.2)
        r.check("after 5 s it follows again", row_visible(p.toc_current))
        cur = p.toc_current
        tree.doubleClicked.emit(p.dock.toc.entries[cur + 3]["item"].index())
        h.ready(p.dock.toc.entries[cur + 3]["spine"])
        r.check("double-click jumps and hands focus to the book",
                p.spine_index == p.dock.toc.entries[cur + 3]["spine"]
                and (p.view.hasFocus() or p.view.focusProxy() is None or p.view.focusProxy().hasFocus()
                     or not h.win.isActiveWindow()))
        p.close_book()
        p.open_book(os.path.join(FIXTURES, "no_toc.epub"))
        h.ready()
        r.check("synthetic TOC shows its note", p.dock.toc.note.isVisible() and bool(p.dock.toc.note.text()))
        h.shot("t02_synthetic")
    finally:
        h.finish()
    return r


# ==========================================================================
# phase: real (the user's Chinese book, read-only)
# ==========================================================================

def phase_real(out: str) -> Results:
    from epublib import EpubBook

    h = Harness("real", out, size=(1180, 760), settings={"reader.theme": "light"})
    r = h.results
    p = h.page
    book_path = next((b for b in REAL_BOOKS if os.path.exists(b)), None)
    if book_path is None:
        r.check("a real book is available (skipped)", True, "none of the user's books found")
        h.finish()
        return r
    try:
        before = (os.path.getsize(book_path), os.stat(book_path).st_mtime_ns)
        p.open_book(book_path)
        h.ready(timeout=15)
        bid = p.book_id
        r.check("real book opens with its Chinese title", "从此岸到彼岸" in (h.titles[-1] if h.titles else ""),
                h.titles[-1] if h.titles else "")
        h.shot("r01_open")

        # Chinese book-wide search
        with EpubBook.open(book_path) as ref:
            expected = len(ref.search("汇率", limit=100000))
        p.focus_search()
        p.dock.search.edit.setText("汇率")
        p.run_search("汇率")
        h.pump(0.3)
        hits = p.search_hits
        r.check("search 汇率 finds every hit (epublib)", len(hits) == expected and expected > 500,
                f"{len(hits)} vs {expected}")
        r.check("result list capped at 500 rows with a note", p.dock.search.list.count() == 500
                and p.dock.search.capped.isVisible())
        r.check("count label", str(expected) in p.dock.search.status.text(), p.dock.search.status.text())
        h.shot("r02_search")
        p.find_next()
        first = p.search_index
        h.ready(hits[first].spine_index)
        h.wait(lambda: p.page_state.get("matches", 0) > 0, 3, "matches painted")
        r.check("Enter/F3 jumps to the first hit and marks it", first >= 0 and p.page_state.get("matches", 0) > 0,
                f"index={first} matches={p.page_state.get('matches')}")
        h.pump(0.5)
        h.shot("r03_search_jump")
        for _ in range(3):
            p.find_next()
            h.pump(0.25)
        r.check("n/F3 walks the results", p.search_index == first + 3, str(p.search_index))
        far = next(i for i, x in enumerate(hits[:500]) if x.spine_index > hits[first].spine_index + 2)
        p.dock.search.resultActivated.emit(far, False)
        h.ready(hits[far].spine_index)
        h.wait(lambda: p.page_state.get("matchActive", -1) >= 0, 3, "active match")
        r.check("clicking a result in another chapter jumps there", p.spine_index == hits[far].spine_index
                and p.search_index == far)
        active_gpos = h.call("state")
        r.check("the active match is on the screen", active_gpos and active_gpos.get("matchActive", -1) >= 0)
        h.shot("r04_search_far")

        # hiding a focused card or the dock must not Tab-focus a link in the book
        entries = p.dock.toc.entries
        cr = next((i for i, e in enumerate(entries) if e["title"] == "版权信息"), None)
        if cr is not None:
            p._on_toc_jump(cr, False)
            h.ready(entries[cr]["spine"])
            page0 = p.page_state.get("page")
            active = "(function(){var a=document.activeElement;return a?a.tagName:''})()"
            p.show_book_info()
            h.pump(0.2)
            p.book_info.close_btn.setFocus()
            h.pump(0.1)
            p.escape()
            h.pump(0.6)
            r.check("dismissing a focused card moves neither the page nor focus onto a link",
                    p.page_state.get("page") == page0 and h.js(active) == "BODY",
                    f"page {page0}->{p.page_state.get('page')} active={h.js(active)}")
            p.set_dock_visible(True)
            p.dock.toc.tree.setFocus()
            h.pump(0.1)
            p.escape()
            h.pump(0.6)
            r.check("Esc in the TOC closes the dock without moving the page",
                    not p.dock.isVisible() and p.page_state.get("page") == page0 and h.js(active) == "BODY",
                    f"page {page0}->{p.page_state.get('page')} active={h.js(active)}")
            p.set_dock_visible(True)
            p.focus_search()

        # TOC click
        t = next(i for i, e in enumerate(entries) if e["spine"] is not None and e["spine"] > p.spine_index + 1)
        p._on_toc_jump(t, False)
        h.ready(entries[t]["spine"])
        h.wait(lambda: p.toc_current == t, 3, "toc current")
        r.check("TOC jump in the real book", p.spine_index == entries[t]["spine"] and p.toc_current == t,
                entries[t]["title"])

        # page turns
        pages = p.page_state.get("pages", 1)
        p.next_page()
        h.wait(lambda: p.page_state.get("page") == 1 or p.spine_index != entries[t]["spine"], 3, "next page")
        r.check("next page", p.page_state.get("page") == 1 or pages == 1)
        p.next_page()
        p.next_page()
        h.pump(0.4)
        p.prev_page()
        h.pump(0.3)

        # bookmark + highlight
        p.toggle_bookmark()
        h.wait(lambda: len(h.store.book_state(bid)["bookmarks"]) == 1, 3, "bookmark")
        sel = h.select_visible_text(12)
        h.pump(0.2)
        p.highlight_yellow()
        h.wait(lambda: len(h.store.book_state(bid)["highlights"]) == 1, 3, "highlight")
        hl = h.store.book_state(bid)["highlights"][0]
        r.check("highlight of Chinese text", hl["text"] == sel and len(sel) == 12, repr(sel))
        p.toggle_notes()
        h.pump(0.4)
        h.shot("r05_highlight")
        p.toggle_bookmarks()
        h.pump(0.3)
        h.shot("r06_bookmarks")

        # sepia + a font change keep the sentence
        p.set_theme_choice("sepia")
        h.pump(0.5)
        g0 = p.page_state.get("gpos")
        p.font_larger()
        p.font_larger()
        h.pump(0.9)
        r.check("font change keeps the sentence (real book)", p.page_state.get("gpos") == g0,
                f"{g0} -> {p.page_state.get('gpos')}")
        h.shot("r07_sepia_font23")

        # restart: destroy the page, build a new one, reopen
        h.pump(0.5)
        saved_spine, saved_gpos = p.spine_index, p.page_state.get("gpos")
        h.destroy_page()
        pos = h.store.book_state(bid)["position"]
        r.check("position saved before the page was destroyed", pos["spine_index"] == saved_spine
                and pos["locator"]["gpos"] == saved_gpos, f"{pos['spine_index']}/{pos['locator']['gpos']}")
        p = h.new_page()
        p.open_book(book_path)
        h.ready(saved_spine, timeout=15)
        h.wait(lambda: p.page_state.get("gpos") == saved_gpos, 4, "restored gpos")
        r.check("rebuilt page restores the same sentence (spine + gpos)", p.spine_index == saved_spine
                and p.page_state.get("gpos") == saved_gpos, f"{p.spine_index}/{p.page_state.get('gpos')} vs "
                f"{saved_spine}/{saved_gpos}")
        r.check("highlight and bookmark came back", p.dock.notes.visible_ids() == [hl["id"]]
                and p.dock.bookmarks.list.count() == 1)
        h.shot("r08_restored")
        p.font_smaller()
        h.pump(0.9)
        r.check("font change after restart keeps the sentence", p.page_state.get("gpos") == saved_gpos)
        after = (os.path.getsize(book_path), os.stat(book_path).st_mtime_ns)
        r.check("the user's book file was not modified", before == after)
        p.close_book()
    finally:
        h.finish()
    return r


# ==========================================================================
# phase: i18n (live language switch; nothing may keep the previous language)
# ==========================================================================

_FRAG_SPLIT = re.compile(r"\{[a-z_]+\}")
_HAS_WORD = re.compile(r"[A-Za-z\u3040-\u30ff\u3400-\u9fff]")


def _fragments(lang: str) -> set[str]:
    import strings

    out = set()
    for value in strings.TABLES[lang].values():
        for piece in _FRAG_SPLIT.split(value):
            for line in piece.split("\n"):
                frag = line.strip()
                if len(frag) >= 2 and _HAS_WORD.search(frag):
                    out.add(frag)
    return out


def snapshot_ui(page) -> dict[str, str]:
    """Every UI-copy string under *page*: labels, buttons, tooltips, placeholders,
    combo items, spin suffixes, menu actions, delegate-painted chrome text."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QTextDocumentFragment
    from PySide6.QtWidgets import (QAbstractButton, QAbstractSpinBox, QComboBox, QLabel, QLineEdit,
                                   QPlainTextEdit, QWidget)

    snap: dict[str, str] = {}
    direct = Qt.FindChildOption.FindDirectChildrenOnly

    def path_of(w) -> str:
        parts = []
        while w is not None and w is not page:
            parent = w.parentWidget()
            siblings = parent.findChildren(type(w), options=direct) if parent is not None else []
            idx = siblings.index(w) if w in siblings else 0
            parts.append(f"{w.objectName() or type(w).__name__}[{idx}]")
            w = parent
        return "/".join(reversed(parts))

    for w in page.findChildren(QWidget):
        if w.property("erBookData"):
            continue
        key = path_of(w)
        if isinstance(w, QLabel):
            text = w.text()
            if w.textFormat() != Qt.TextFormat.PlainText and "<" in text:
                text = QTextDocumentFragment.fromHtml(text).toPlainText()
            snap[key + ":text"] = text
        if isinstance(w, QAbstractButton):
            snap[key + ":text"] = w.text()
            snap[key + ":accessible"] = w.accessibleName()
        if isinstance(w, (QLineEdit, QPlainTextEdit)):
            snap[key + ":placeholder"] = w.placeholderText()
        if isinstance(w, QComboBox):
            for i in range(w.count()):
                snap[f"{key}:item{i}"] = w.itemText(i)
        if isinstance(w, QAbstractSpinBox) and hasattr(w, "suffix"):
            snap[key + ":suffix"] = w.suffix()
        if w.toolTip():
            snap[key + ":tooltip"] = w.toolTip()
    for name, act in page.toolbar.actions.items():
        snap[f"menu:{name}"] = act.text()
    for pane, lst in (("bookmarks", page.dock.bookmarks.list), ("notes", page.dock.notes.list),
                      ("search", page.dock.search.list)):
        for i in range(min(lst.count(), 20)):
            spec = lst.itemDelegate()._build(lst.model().index(i, 0))
            for field in ("meta", "badge"):
                if spec.get(field):
                    snap[f"{pane}:{i}:{field}"] = spec[field]
            if spec.get("kind") != "group" and spec.get("text") and pane == "search":
                # the frame around the context (…before match after…) is UI copy
                snap[f"{pane}:{i}:frame"] = spec["text"][:1] + spec["text"][-1:]
            if lst.item(i).toolTip():
                snap[f"{pane}:{i}:tooltip"] = lst.item(i).toolTip()
    for e in page.dock.toc.entries:
        if not e["title"]:
            snap[f"toc:{id(e['item'])}"] = e["item"].text()
    snap["status:center"] = page.statusbar.center.text()
    snap["status:right"] = page.statusbar.right.text()
    return snap


def _contains(text: str, frag: str) -> bool:
    """Substring test; Latin fragments must stand as whole words ('No' is not in 'Noto')."""
    if frag.isascii():
        return re.search(r"(?<![A-Za-z])" + re.escape(frag) + r"(?![A-Za-z])", text) is not None
    return frag in text


def stale_entries(prev: dict, cur: dict, a: str, b: str) -> list[str]:
    """Entries still showing language *a* after switching to *b*.

    Shortcut labels (strings.KEYS) are identical in every language by design and
    are skipped; everything else that contains copy exclusive to *a* is stale.
    """
    import strings

    combos = {c for v in strings.KEYS.values() for c in v}
    seps = {strings.translate(lang, "common.sep") for lang in strings.LANGUAGES} | {" / "}

    def is_key_label(text: str) -> bool:
        parts = [text]
        for sep in seps:
            parts = [q for part in parts for q in part.split(sep)]
        return all(part.strip() in combos for part in parts)

    fa, fb = _fragments(a), _fragments(b)
    only_a = sorted(fa - fb, key=len, reverse=True)
    fb_sorted = sorted(fb, key=len, reverse=True)
    stale = []
    for key, text in cur.items():
        if not text or is_key_label(text):
            continue
        rest = text
        for frag in fb_sorted:
            if frag in rest:
                rest = rest.replace(frag, "\u0000")
        hit = next((f for f in only_a if _contains(rest, f)), None)
        if hit is not None:
            stale.append(f"{key}={text!r} (contains {hit!r})")
    return stale


def phase_i18n(out: str) -> Results:
    import strings

    h = Harness("i18n", out, size=(1200, 760), settings={"reader.theme": "light"})
    r = h.results
    p = h.page
    book_path = next((b for b in REAL_BOOKS if os.path.exists(b)), os.path.join(FIXTURES, "epub2_ncx.epub"))
    try:
        p.open_book(book_path)
        h.ready(timeout=15)
        bid = p.book_id
        # give every pane content: a bookmark, a highlight, a lost highlight, search results
        p.next_chapter()
        h.ready()
        p.next_chapter()
        h.ready()
        p.toggle_bookmark()
        h.wait(lambda: len(h.store.book_state(bid)["bookmarks"]) == 1, 3, "bookmark")
        h.select_visible_text(8)
        h.pump(0.2)
        p.highlight_pink()
        h.wait(lambda: len(h.store.book_state(bid)["highlights"]) == 1, 3, "highlight")
        h.store.add_highlight(bid, {"spine_index": p.spine_index, "spine_href": p.book.spine[p.spine_index].zip_name,
                                    "start": {"gpos": 1, "snippet": "zzzz-not-in-book"},
                                    "end": {"gpos": 9, "snippet": "zzzz"}, "text": "zzzz-not-in-book",
                                    "note": "", "color": "blue"})
        p._resolve_all_highlights()
        p._refresh_notes()
        query = "经济" if "从此岸" not in book_path else "汇率"
        p.focus_search()
        p.dock.search.edit.setText(query)
        p.run_search(query)
        p.find_next()
        h.ready()
        p.set_settings_visible(True)
        h.pump(0.4)
        order = ["zh-Hans", "zh-Hant", "en", "ja", "zh-Hans"]
        prev_lang, prev_snap = order[0], snapshot_ui(p)
        for lang in order[1:]:
            i = p.settings_panel.language.findData(lang)
            p.settings_panel.language.setCurrentIndex(i)
            p.settings_panel.language.activated.emit(i)       # what a user's pick does
            h.pump(0.35)
            r.check(f"{prev_lang} -> {lang}: strings.current_language()", strings.current_language() == lang
                    and h.store.get("ui.language") == lang)
            snap = snapshot_ui(p)
            stale = stale_entries(prev_snap, snap, prev_lang, lang)
            r.check(f"{prev_lang} -> {lang}: nothing shows the previous language ({len(snap)} strings)",
                    not stale, "; ".join(stale[:6]))
            # negative control: one planted previous-language placeholder must be caught
            p.dock.search.edit.setPlaceholderText(strings.translate(prev_lang, "search.placeholder"))
            caught = stale_entries(prev_snap, snapshot_ui(p), prev_lang, lang)
            r.check(f"{prev_lang} -> {lang}: the checker catches a planted stale string", len(caught) == 1,
                    "; ".join(caught[:3]))
            p.dock.search.retranslate_ui()
            for pane in ("search", "notes", "bookmarks"):
                p.dock.set_pane(pane)
                h.pump(0.15)
                stale = stale_entries(prev_snap, snapshot_ui(p), prev_lang, lang)
                r.check(f"{lang}: {pane} pane is current", not stale, "; ".join(stale[:4]))
            p.dock.set_pane("toc")
            h.pump(0.15)
            h.shot(f"i18n_{lang}_main")
            p.dock.set_pane("notes")
            h.pump(0.15)
            h.shot(f"i18n_{lang}_notes")
            p.dock.set_pane("search")
            p.toggle_cheatsheet()
            h.pump(0.3)
            stale = stale_entries(prev_snap, snapshot_ui(p), prev_lang, lang)
            r.check(f"{lang}: cheat sheet", not stale, "; ".join(stale[:4]))
            h.shot(f"i18n_{lang}_cheatsheet")
            p.toggle_cheatsheet()
            p.show_book_info()
            h.pump(0.2)
            h.shot(f"i18n_{lang}_bookinfo")
            p.escape()
            prev_lang, prev_snap = lang, snapshot_ui(p)

        # an error card switching language while it is on screen
        p.close_book()
        p.open_book(os.path.join(FIXTURES, "drm_fake.epub"))
        h.pump(0.2)
        prev_lang, prev_snap = strings.current_language(), snapshot_ui(p)
        for lang in ["ja", "en", "zh-Hant", "zh-Hans"]:
            p._on_setting("ui.language", lang)
            h.pump(0.3)
            snap = snapshot_ui(p)
            stale = stale_entries(prev_snap, snap, prev_lang, lang)
            r.check(f"error card {prev_lang} -> {lang}", not stale
                    and p.error_card.title.text() == strings.translate(lang, "err.drm.title"),
                    "; ".join(stale[:4]))
            h.shot(f"i18n_{lang}_error_drm")
            prev_lang, prev_snap = lang, snap
    finally:
        h.finish()
    return r


GUI = {"fixture": phase_fixture, "errors": phase_errors, "toc": phase_toc, "real": phase_real,
       "i18n": phase_i18n}


# ==========================================================================
# command line + unittest
# ==========================================================================

def _run_child(phase: str, out: str) -> tuple[int, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run([sys.executable, os.path.abspath(__file__), "--phase", phase, "--out", out],
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=PHASE_TIMEOUT_S, env=env, cwd=ROOT)
        return proc.returncode, proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as exc:
        return 124, (exc.stdout or "") + f"\nTIMEOUT after {PHASE_TIMEOUT_S}s"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--phase", choices=PHASES)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args(argv)
    if args.phase:
        res = phase_unit(args.out) if args.phase == "unit" else run_gui_phase(args.phase, args.out)
        print(f"PHASE {args.phase}: {len(res.rows) - len(res.failed)}/{len(res.rows)} passed", flush=True)
        return 0 if res.rows and not res.failed else 1
    phases = PHASES if args.all or not args.phase else (args.phase,)
    total_fail = 0
    for ph in phases:
        rc, text = _run_child(ph, args.out)
        print(text.rstrip())
        if rc != 0:
            total_fail += 1
            print(f"PHASE {ph} exited {rc}")
    print(f"\nscreenshots: {args.out}")
    print("ALL PHASES PASSED" if not total_fail else f"{total_fail} PHASE(S) FAILED")
    return 0 if not total_fail else 1


class ReaderPageTests(unittest.TestCase):
    """unittest entry: the unit phase in-process, each GUI phase in a child process."""

    def test_unit(self) -> None:
        res = phase_unit(DEFAULT_OUT)
        self.assertFalse(res.failed, res.failed)

    def _gui(self, phase: str) -> None:
        if os.environ.get("EPUB_READER_SKIP_GUI_TESTS"):
            self.skipTest("EPUB_READER_SKIP_GUI_TESTS is set")
        rc, text = _run_child(phase, DEFAULT_OUT)
        self.assertEqual(rc, 0, text[-3000:])

    def test_gui_fixture(self) -> None:
        self._gui("fixture")

    def test_gui_errors(self) -> None:
        self._gui("errors")

    def test_gui_toc(self) -> None:
        self._gui("toc")

    def test_gui_real(self) -> None:
        self._gui("real")

    def test_gui_i18n(self) -> None:
        self._gui("i18n")


if __name__ == "__main__":
    sys.exit(main())
