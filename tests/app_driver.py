#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Drive the REAL application (``epub_reader.main``) in this process.

    python tests\\app_driver.py <scenario> <result.json> <out_dir> [app arguments...]

``tests/test_app.py`` starts one child per scenario with a temporary APPDATA and
LOCALAPPDATA, so the user's real ``%APPDATA%\\Book Reader`` is never touched.
The scenario runs from ``main(on_started=...)`` once the event loop is up; it
paces itself with processEvents loops, records checks into *result.json* and
quits through the real quit path (a Ctrl+Q key event) unless it says otherwise.
The user's books are opened read-only in place, never copied or modified.
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import epub_reader  # noqa: E402  (registers epub:// before any QApplication)

from PySide6.QtCore import QEventLoop, QPoint, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (QAbstractButton, QAbstractSpinBox, QApplication, QComboBox,  # noqa: E402
                               QLabel, QLineEdit, QMenu, QPlainTextEdit, QWidget)

import strings  # noqa: E402
import theme as theme_mod  # noqa: E402
from strings import S  # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures")
REAL_BOOKS = [
    r"C:\Users\mengz\Desktop\文件\Econ Books\50人的二十年_樊纲 易纲等.epub",
    r"C:\Users\mengz\Desktop\文件\Econ Books\Econ Books_China\从此岸到彼岸_人民币汇率如何实现清洁浮动_缪延亮.epub",
] + sorted(glob.glob(r"C:\Users\mengz\Desktop\文件\CUHK Notes\CUHK_BFRM\Fin Books\*(李向科) (Z-Library).epub"))
SEARCH_CANDIDATES = ("汇率", "改革", "价格", "市场", "经济", "投资")
K = Qt.Key
CTRL = Qt.KeyboardModifier.ControlModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier
NOMOD = Qt.KeyboardModifier.NoModifier
_CJK = re.compile(r"[\u3400-\u9fff]")

VISIBLE_TEXT_JS = r"""(function(n){
  var W=innerWidth,H=innerHeight,b=document.body;if(!b)return '';
  var tw=document.createTreeWalker(b,NodeFilter.SHOW_TEXT,null),node,out='',r=document.createRange();
  while((node=tw.nextNode())){
    var p=node.parentNode,tag=p?(p.nodeName||'').toLowerCase():'';
    if(tag==='script'||tag==='style'||tag==='noscript')continue;
    var v=node.nodeValue;if(!v||!v.trim())continue;
    r.selectNodeContents(node);var rs=r.getClientRects(),vis=false;
    for(var i=0;i<rs.length;i++){var q=rs[i];if(q.width>0&&q.right>0&&q.left<W&&q.bottom>0&&q.top<H){vis=true;break;}}
    if(!vis)continue;
    if(!out){for(var k=0;k<v.length;k++){r.setStart(node,k);r.setEnd(node,k+1);var c=r.getBoundingClientRect();
      if(c.width>0&&c.right>0&&c.left<W&&c.bottom>0&&c.top<H){out=v.slice(k);break;}}}
    else{out+=v;}
    if(out.length>=n)break;
  }
  return out.replace(/\s+/g,' ').slice(0,n);
})(%d)"""

PAGE_TEXT_JS = r"""(function(){
  var W=innerWidth,H=innerHeight,b=document.body;if(!b)return '';
  var tw=document.createTreeWalker(b,NodeFilter.SHOW_TEXT,null),node,out=[],r=document.createRange();
  while((node=tw.nextNode())){
    var p=node.parentNode,tag=p?(p.nodeName||'').toLowerCase():'';
    if(tag==='script'||tag==='style'||tag==='noscript')continue;
    var v=node.nodeValue;if(!v)continue;
    r.selectNodeContents(node);var rs=r.getClientRects(),vis=false;
    for(var i=0;i<rs.length;i++){var q=rs[i];if(q.width>0&&q.right>0&&q.left<W&&q.bottom>0&&q.top<H){vis=true;break;}}
    if(vis)out.push(v);
  }
  return out.join('').replace(/\s+/g,'');
})()"""

#: [text colour of the first <p>, the background actually painted behind it]
EFFECTIVE_COLORS_JS = r"""(function(){var p=document.querySelector('p')||document.body,e=p,bg='';
  while(e&&e.nodeType===1){var b=getComputedStyle(e).backgroundColor;
    if(b&&b!=='rgba(0, 0, 0, 0)'&&b!=='transparent'){bg=b;break;}e=e.parentElement;}
  return [getComputedStyle(p).color,bg||getComputedStyle(document.documentElement).backgroundColor];})()"""

SELECT_JS = r"""(function(n,fx,fy){
  var xs=[fx,0.5,0.4,0.6,0.3,0.7],ys=[fy,0.3,0.4,0.5,0.25,0.6];
  for(var i=0;i<xs.length;i++){for(var j=0;j<ys.length;j++){
    var r=document.caretRangeFromPoint(innerWidth*xs[i],innerHeight*ys[j]);
    if(!r||r.startContainer.nodeType!==3)continue;var t=r.startContainer,v=t.nodeValue;
    var s=Math.max(0,Math.min(r.startOffset,v.length-n));
    if(v.slice(s,s+n).trim().length<n)continue;
    var g=document.createRange();g.setStart(t,s);g.setEnd(t,s+n);
    var sel=getSelection();sel.removeAllRanges();sel.addRange(g);return sel.toString();}}
  return '';})(%d,%f,%f)"""


# Arms a capture-phase DOM 'copy' listener. run_json evaluates ONE expression, hence the IIFE.
COPY_LISTEN_JS = ("(function(){window.__erCopies=window.__erCopies||[];if(!window.__erCopyL){window.__erCopyL=1;"
                  "document.addEventListener('copy',function(){window.__erCopies.push(String(getSelection()))},true)}"
                  "window.__erCopies.length=0;return true})()")
COPY_READ_JS = "(function(){return window.__erCopies||null})()"


def windows_clipboard_available() -> bool:
    """False when another process holds the Windows clipboard open (then no app can copy)."""
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

class Ctx:
    """What a scenario gets: the window, waits, JS, keys, checks and screenshots."""

    def __init__(self, win: "epub_reader.MainWindow", scenario: str, result_path: str, out: str,
                 argv: list[str]) -> None:
        self.win = win
        self.app = QApplication.instance()
        self.r = win.reader
        self.lib = win.library
        self.store = win.store
        self.scenario = scenario
        self.result_path = result_path
        self.out = out
        self.argv = argv
        self.rows: list[list] = []
        self.data: dict = {}
        self.shots: list[str] = []
        os.makedirs(out, exist_ok=True)

    # -- results ----------------------------------------------------------------
    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append([name, bool(ok), str(detail)[:1500]])
        print(f"  [{'PASS' if ok else 'FAIL'}] {self.scenario}: {name}" + (f" -- {detail}" if detail else ""),
              flush=True)
        return bool(ok)

    def step(self, name: str, fn) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - one broken step must not hide the others
            self.check(f"{name} (raised)", False, "".join(traceback.format_exception(exc))[-1400:])

    def save(self) -> None:
        with open(self.result_path, "w", encoding="utf-8") as fh:
            json.dump({"scenario": self.scenario, "rows": self.rows, "data": self.data, "shots": self.shots},
                      fh, ensure_ascii=False, indent=1)

    # -- waiting --------------------------------------------------------------------
    def pump(self, sec: float) -> None:
        end = time.monotonic() + sec
        while time.monotonic() < end:
            self.app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)

    def wait(self, pred, timeout: float = 8.0, what: str = "") -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
            try:
                if pred():
                    return True
            except Exception:  # noqa: BLE001
                pass
        raise AssertionError(f"timed out after {timeout}s waiting for {what}")

    def ready(self, timeout: float = 15.0, spine: int | None = None) -> None:
        r = self.r
        self.wait(lambda: r.is_ready() and bool(r.page_state) and (spine is None or r.spine_index == spine),
                  timeout, f"chapter {spine} ready")
        self.pump(0.3)

    def js(self, expr: str, timeout: float = 6.0):
        box: dict = {}
        self.r.host.run_json(expr, lambda v: box.setdefault("v", v))
        self.wait(lambda: "v" in box, timeout, "js " + expr[:40])
        return box["v"]

    def call(self, method: str, *args, timeout: float = 6.0):
        box: dict = {}
        self.r.host.call_reader(method, *args, callback=lambda v: box.setdefault("v", v))
        self.wait(lambda: "v" in box, timeout, f"epubReader.{method}")
        return box["v"]

    def visible_text(self, n: int = 40) -> str:
        return str(self.js(VISIBLE_TEXT_JS % n) or "")

    def page_text(self) -> str:
        return str(self.js(PAGE_TEXT_JS) or "")

    def select_text(self, n: int = 8, fx: float = 0.5, fy: float = 0.3) -> str:
        text = str(self.js(SELECT_JS % (n, fx, fy)) or "")
        self.pump(0.25)
        return text

    # -- input ------------------------------------------------------------------------
    def activate(self) -> None:
        self.win.raise_()
        self.win.activateWindow()
        try:
            self.wait(lambda: self.win.isActiveWindow(), 1.5, "active window")
        except AssertionError:
            pass            # a background launch may not get the foreground; keys still route

    def focus_target(self) -> QWidget:
        return QApplication.focusWidget() or self.win.focusWidget() or self.win

    def proxy(self) -> QWidget:
        return self.r.view.focusProxy() or self.r.view

    def key(self, key, mods=NOMOD, to: QWidget | None = None, settle: float = 0.15) -> None:
        QTest.keyClick(to or self.focus_target(), key, mods)
        self.pump(settle)

    def focus_book(self) -> None:
        self.r.focus_book()
        self.pump(0.15)
        self.wait(lambda: self.proxy().hasFocus() or self.win.focusWidget() is self.proxy(), 3, "book focus")

    def quit_via_key(self) -> None:
        self.save()
        self.key(K.Key_Q, CTRL, settle=0.05)

    # -- screenshots ------------------------------------------------------------------
    def shot(self, name: str, widget: QWidget | None = None) -> str:
        path = os.path.join(self.out, name + ".png")
        (widget or self.win).grab().save(path)
        self.shots.append(path)
        print(f"  shot {path}", flush=True)
        return path

    # -- helpers ----------------------------------------------------------------------
    def goto_spine(self, i: int) -> None:
        self.r._goto(i, at="start", focus=True)
        self.ready(spine=i)

    def spine_with_images(self, book, start: int = 0) -> int:
        for i, item in enumerate(book.spine):
            if i < start:
                continue
            try:
                if "<img" in book.read_text(item.zip_name) and book.plain_text(item.zip_name).strip():
                    return i
            except Exception:  # noqa: BLE001
                continue
        for i, item in enumerate(book.spine):
            try:
                if "<img" in book.read_text(item.zip_name):
                    return i
            except Exception:  # noqa: BLE001
                continue
        return -1


# ==========================================================================
# UI text snapshot (every visible label, tooltip, placeholder, menu item)
# ==========================================================================

def _menu_texts(menu: QMenu, prefix: str, snap: dict) -> None:
    # NOTE: never call QAction.menu() here: in PySide6 6.11 it leaves the C++
    # submenu to be destroyed afterwards (verified: both submenus died).
    for i, act in enumerate(menu.actions()):
        if act.isSeparator():
            continue
        snap[f"{prefix}/{i}"] = act.text()
    for j, sub in enumerate(menu.findChildren(QMenu, options=Qt.FindChildOption.FindDirectChildrenOnly)):
        snap[f"{prefix}/sub{j}:title"] = sub.title()
        _menu_texts(sub, f"{prefix}/sub{j}", snap)


def snapshot_window(win) -> dict[str, str]:
    """UI copy in the whole window (both pages, menus, overlays) + the title."""
    from PySide6.QtGui import QTextDocumentFragment

    snap: dict[str, str] = {"window:title": win.windowTitle()}
    pages = (win.library, win.reader)
    for w in win.findChildren(QWidget):
        if w.property("erBookData"):
            continue
        # only what is on screen, or on screen as soon as its page is shown again
        page = next((p for p in pages if p is w or p.isAncestorOf(w)), None)
        if not (w.isVisibleTo(page) if page is not None and page is not w else w.isVisibleTo(win)):
            continue
        key = f"{type(w).__name__}:{w.objectName()}:{id(w)}"
        if isinstance(w, QLabel):
            text = w.text()
            if w.textFormat() != Qt.TextFormat.PlainText and "<" in text:
                text = QTextDocumentFragment.fromHtml(text).toPlainText()
            if w.objectName() != "crash-log-path":
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
    for name, act in win.reader.toolbar.actions.items():
        snap[f"reader-menu:{name}"] = act.text()
    _menu_texts(win.library.more_menu, "library-menu", snap)
    snap["status:left"] = win.reader.statusbar.left.text() if hasattr(win.reader.statusbar, "left") else ""
    snap["status:center"] = win.reader.statusbar.center.text()
    snap["status:right"] = win.reader.statusbar.right.text()
    snap["library:notice"] = win.library.notice_text()
    # the painted shelf: section headers ("继续阅读", "全部书籍 · 5 本书") are not widgets
    for i, h in enumerate(win.library.view.headers()):
        for j, part in enumerate(x for x in h if isinstance(x, str)):
            snap[f"shelf-header{i}.{j}"] = part
    return {k: v for k, v in snap.items() if v}


# ==========================================================================
# scenarios
# ==========================================================================

def sc_launch(c: Ctx) -> None:
    """a. launch from source, render, quit cleanly (Ctrl+Q)."""
    win, r = c.win, c.r
    c.activate()
    c.check("window is shown", win.isVisible())
    c.check("title on the shelf is 'Book Reader'", win.windowTitle() == "Book Reader", win.windowTitle())
    c.check("shelf is the current page", win.stack.currentWidget() is win.library)
    c.check("key map audit is clean", not win.keys.problems, "; ".join(win.keys.problems))
    c.check("minimum size 720x520", (win.minimumWidth(), win.minimumHeight()) == (720, 520))
    c.data["bindings"] = len(win.keys.bindings)
    c.check("empty shelf shows the empty state", c.lib._stack.currentWidget() is c.lib.empty)
    c.shot("app_empty_shelf")
    path = os.path.join(FIXTURES, "epub3_nav.epub")
    win.open_path(path)
    c.ready()
    title = r.book.metadata.get("title")
    c.check("reader is current after open", win.stack.currentWidget() is r)
    c.check("title while reading is '<book> — Book Reader'", win.windowTitle() == f"{title} — Book Reader",
            win.windowTitle())
    c.check("page rendered (pages >= 1)", int(r.page_state.get("pages") or 0) >= 1, str(r.page_state))
    c.shot("app_launch_reading")
    c.quit_via_key()


def sc_fixtures(c: Ctx) -> None:
    """b. every fixture: error cards, fonts, fixed layout, broken XML, odd paths, big book."""
    win, r = c.win, c.r
    c.activate()
    expect_err = {"drm_fake.epub": "drm", "truncated.epub": "corrupt", "not_a_zip.epub": "corrupt",
                  "empty.epub": "corrupt", "no_container.epub": "structure", "not_an_epub.epub": "structure"}
    for path in sorted(glob.glob(os.path.join(FIXTURES, "*.epub"))):
        name = os.path.basename(path)

        def one(path=path, name=name) -> None:
            t0 = time.perf_counter()
            win.open_path(path)
            if name in expect_err:
                c.pump(0.3)                 # let the splitter re-lay out before a screenshot
                spec = r.error_spec() or {}
                c.check(f"{name}: {expect_err[name]} card", spec.get("kind") == expect_err[name]
                        and r.error_card.isVisible() and r.book is None,
                        f"kind={spec.get('kind')} title={r.error_card.title.text()!r}")
                if name == "drm_fake.epub":
                    c.check("drm card names the scheme and says it does not decrypt",
                            "ADEPT" in r.error_card.body.text() or "Adobe" in r.error_card.body.text(),
                            r.error_card.body.text())
                    c.shot("app_error_card_drm")
                if name == "no_container.epub":
                    c.shot("app_error_card_structure")
                if name == "truncated.epub":
                    c.shot("app_error_card_corrupt")
                c.check(f"{name}: window title names the file",
                        win.windowTitle().endswith("— Book Reader"), win.windowTitle())
                return
            c.ready(timeout=20)
            ms = (time.perf_counter() - t0) * 1000
            st = r.page_state
            c.check(f"{name}: opens and renders", r.book is not None and int(st.get("pages") or 0) >= 1,
                    f"{ms:.0f} ms, state={ {k: st.get(k) for k in ('page', 'pages', 'mode', 'fixedLayout')} }")
            if name == "obfuscated_fonts.epub":
                fonts = c.js("(function(){var o={};document.fonts.forEach(function(f){o[f.family.replace(/\"/g,'')]"
                             "=f.status;});return o;})()")
                c.pump(0.6)
                fonts = c.js("(function(){var o={};document.fonts.forEach(function(f){o[f.family.replace(/\"/g,'')]"
                             "=f.status;});return o;})()")
                used = c.js("document.fonts.check('28px ObfTest')")
                c.check("obfuscated_fonts: de-obfuscated font loads (ObfTest loaded)",
                        isinstance(fonts, dict) and fonts.get("ObfTest") == "loaded", f"{fonts} check={used}")
                c.shot("app_fixture_obfuscated_fonts")
            if name == "fixed_layout.epub":
                box = c.js("(function(){var e=document.body.getBoundingClientRect(),"
                           "d=document.documentElement.getBoundingClientRect();return {bw:e.width,bh:e.height,"
                           "bl:e.left,bt:e.top,dw:d.width,dh:d.height,W:innerWidth,H:innerHeight};})()")
                fits = isinstance(box, dict) and box["bw"] <= box["W"] + 2 and box["bh"] <= box["H"] + 2 \
                    and box["bl"] >= -2 and box["bt"] >= -2
                c.check("fixed_layout: page is scaled to fit the view", bool(st.get("fixedLayout")) and fits,
                        f"{box} fixedLayout={st.get('fixedLayout')}")
                for tname in ("light", "sepia", "dark"):
                    r.set_theme_choice(tname)
                    c.pump(0.5)
                    cols = c.js(EFFECTIVE_COLORS_JS)
                    ratio = _contrast(*cols) if isinstance(cols, list) else 0
                    c.check(f"fixed_layout: page text is readable under {tname} (>= 4.5:1)",
                            ratio >= 4.5, f"{cols} ratio={ratio:.2f}")
                c.shot("app_fixture_fixed_layout_dark")
                r.set_theme_choice("system")
                c.pump(0.3)
                c.shot("app_fixture_fixed_layout")
            if name == "broken_xml.epub":
                flat = c.call("flatText") or ""
                c.check("broken_xml: chapter text renders", len(flat.strip()) > 20, repr(flat[:60]))
            if name == "odd_paths.epub":
                bad, total = [], 0
                for i in range(len(r.book.spine)):
                    c.goto_spine(i)
                    imgs = c.js("Array.from(document.images).map(function(i){return [i.getAttribute('src'),"
                                "i.complete,i.naturalWidth];})") or []
                    total += len(imgs)
                    bad += [x for x in imgs if not (x[1] and x[2] > 0)]
                c.check("odd_paths: every image resolves", total > 0 and not bad, f"total={total} bad={bad}")
            if name == "images_fonts.epub":
                c.goto_spine(0)
                imgs = c.js("Array.from(document.images).map(function(i){return i.naturalWidth;})") or []
                c.check("images_fonts: images decode", imgs and all(w > 0 for w in imgs), str(imgs))
            if name == "big_book.epub":
                times = []
                for _ in range(8):
                    t1 = time.perf_counter()
                    before = r.spine_index
                    r.next_chapter()
                    c.wait(lambda b=before: r.spine_index != b and r.is_ready(), 5, "next chapter")
                    times.append((time.perf_counter() - t1) * 1000)
                t1 = time.perf_counter()
                r.goto_percent(75.0)
                c.wait(lambda: r.is_ready() and abs(float(r.page_state.get("percent") or 0) - 0.75) < 0.05,
                       6, "75%")
                jump = (time.perf_counter() - t1) * 1000
                c.pump(0.3)
                c.check("big_book: chapter turns and a 75% jump are quick (< 1.5 s each)",
                        max(times) < 1500 and jump < 1500,
                        f"chapter ms={[round(t) for t in times]} jump75 ms={jump:.0f} "
                        f"toc entries={len(r.dock.toc.entries)} toc_current={r.toc_current}")
                c.check("big_book: TOC tracks the chapter", r.toc_current >= 0 and
                        r.dock.toc.entries[r.toc_current]["spine"] == r.spine_index,
                        f"toc_current={r.toc_current} spine={r.spine_index}")
        c.step(name, one)
    c.quit_via_key()


def _search_query(book) -> str:
    for q in SEARCH_CANDIDATES:
        if len(book.search(q, limit=5)) >= 2:
            return q
    return ""


def _flat_toc(entries, depth=0):
    for e in entries:
        yield e, depth
        yield from _flat_toc(e.children, depth + 1)


def sc_real(c: Ctx) -> None:
    """c. the user's three real books, read-only."""
    win, r = c.win, c.r
    c.activate()
    books = [b for b in REAL_BOOKS if os.path.exists(b)]
    c.check("the three real books are present", len(books) == 3, str(books))
    for n, path in enumerate(books):
        tag = os.path.basename(path)[:12]

        def one(path=path, tag=tag, n=n) -> None:
            st0 = os.stat(path)
            t0 = time.perf_counter()
            win.open_path(path)
            c.ready(timeout=25)
            book = r.book
            c.check(f"{tag}: opens", book is not None, f"{(time.perf_counter() - t0) * 1000:.0f} ms")
            c.check(f"{tag}: a reflowable book is never announced as fixed layout",
                    book.is_fixed_layout or ("fxl" not in r._once
                                             and S("status.fixed_layout") not in r.statusbar.left.text()),
                    r.statusbar.left.text())
            # CJK, not mojibake: the title, the page text and the JS/Python flat text agree
            title = book.metadata.get("title") or ""
            c.check(f"{tag}: title is CJK text", bool(_CJK.search(title)) and "\ufffd" not in title, title)
            spine_i = next((i for i, s in enumerate(book.spine) if len(book.plain_text(s.zip_name)) > 400), 0)
            c.goto_spine(spine_i)
            flat = c.call("flatText") or ""
            py = book.plain_text(book.spine[spine_i].zip_name)
            c.check(f"{tag}: page text is real CJK (not mojibake) and matches epublib exactly",
                    flat == py and len(_CJK.findall(flat)) > 100 and "\ufffd" not in flat
                    and not re.search("Ã.|â€|锟斤拷", flat), f"js={len(flat)} py={len(py)} sample={flat[:30]!r}")
            vis = c.visible_text(30)
            c.check(f"{tag}: visible text on screen is CJK", len(_CJK.findall(vis)) >= 10, vis)
            c.shot(f"app_real_{n}_text")
            # TOC in document order
            flat_toc = [e for e, _d in _flat_toc(book.toc)]
            pane = [e["title"] for e in r.dock.toc.entries]
            order = [book.spine_index(e.zip_name) for e in flat_toc if book.spine_index(e.zip_name) is not None]
            mono = all(a <= b for a, b in zip(order, order[1:]))
            c.check(f"{tag}: TOC pane shows the book's TOC in document order",
                    pane == [e.title for e in flat_toc] and mono and len(pane) > 5,
                    f"{len(pane)} entries, first={pane[:4]}, spine order monotonic={mono}")
            # page turns
            g = []
            for _ in range(3):
                before = (r.spine_index, r.page_state.get("page"), r.page_state.get("gpos"))
                r.next_page()
                c.wait(lambda b=before: (r.spine_index, r.page_state.get("page"), r.page_state.get("gpos")) != b,
                       4, "page turn")
                g.append((r.spine_index, r.page_state.get("page"), r.page_state.get("gpos")))
            c.check(f"{tag}: page turns move forward", len(set(g)) == 3, str(g))
            # 2-character search: finds hits and jumps to the right place
            q = _search_query(book)
            c.data[f"query_{n}"] = q
            r.focus_search()
            r.dock.search.edit.setText(q)
            c.key(K.Key_Return, to=r.dock.search.edit, settle=0.1)
            c.wait(lambda: r.search_hits and r.search_index >= 0 and r.is_ready(), 12, "search jump")
            c.wait(lambda: r.page_state.get("matchActive", -1) >= 0, 5, "active match painted")
            c.pump(0.4)
            hit = r.search_hits[r.search_index]
            text = c.page_text()
            c.check(f"{tag}: search {q!r} finds hits and the page shows the active hit",
                    len(r.search_hits) >= 2 and r.spine_index == hit.spine_index and q in text,
                    f"hits={len(r.search_hits)} index={r.search_index} spine={r.spine_index}/{hit.spine_index} "
                    f"context={hit.before[-8:]}[{hit.match}]{hit.after[:8]}")
            c.check(f"{tag}: the hit's own context is on the page",
                    (hit.before[-4:] + hit.match + hit.after[:4]).replace(" ", "").replace("\n", "") in text,
                    hit.before[-4:] + hit.match + hit.after[:4])
            if n == 1:
                c.shot("app_search_results")
            # images visible
            si = c.spine_with_images(book, 1)
            if si >= 0:
                c.goto_spine(si)
                imgs = c.js("Array.from(document.images).map(function(i){var b=i.getBoundingClientRect();"
                            "return [i.naturalWidth,i.naturalHeight,Math.round(b.left),Math.round(b.width)];})") or []
                page_of = c.js("(function(){var W=innerWidth,se=document.scrollingElement;var i=document.images[0];"
                               "if(!i)return -1;var b=i.getBoundingClientRect();"
                               "return Math.floor((b.left+Math.abs(se.scrollLeft)+1)/W);})()")
                if isinstance(page_of, int) and page_of >= 0:
                    c.call("gotoPage", page_of)
                    c.pump(0.5)
                on_screen = c.js("Array.from(document.images).some(function(i){var b=i.getBoundingClientRect();"
                                 "return i.naturalWidth>0&&b.width>20&&b.right>0&&b.left<innerWidth;})")
                c.check(f"{tag}: images decode and one is on screen",
                        imgs and all(x[0] > 0 for x in imgs) and bool(on_screen),
                        f"spine {si}: {imgs[:4]}")
                c.shot(f"app_real_{n}_image")
            else:
                c.check(f"{tag}: has an image document", False, "no <img> found")
            st1 = os.stat(path)
            c.check(f"{tag}: the book file was not modified", (st0.st_size, st0.st_mtime_ns)
                    == (st1.st_size, st1.st_mtime_ns))
        c.step(tag, one)
    c.quit_via_key()


def sc_features(c: Ctx) -> None:
    """d. ReaderPage behaviour: TOC, search walking, bookmarks, highlights, export,
    settings live-apply + this-book override, image zoom, F1, time left, Escape ladder,
    key gating."""
    win, r, store = c.win, c.r, c.store
    c.activate()
    path = REAL_BOOKS[1] if os.path.exists(REAL_BOOKS[1]) else os.path.join(FIXTURES, "epub2_ncx.epub")
    win.open_path(path)
    c.ready(timeout=25)
    book, bid = r.book, r.book_id

    def toc() -> None:
        if not r.dock.isVisible() or r.dock.current_pane() != "toc":
            c.focus_book()
            c.key(K.Key_T, CTRL)
        c.check("Ctrl+T opens the TOC pane", r.dock.isVisible() and r.dock.current_pane() == "toc")
        entries = r.dock.toc.entries
        spines = [e["spine"] for e in entries]
        # an entry that is alone in its file, so "current" can only be that entry
        k = next(i for i, e in enumerate(entries) if e["spine"] >= 12 and spines.count(e["spine"]) == 1)
        tree = r.dock.toc.tree
        idx = entries[k]["item"].index()
        tree.scrollTo(idx)
        c.pump(0.2)
        rect = tree.visualRect(idx)
        QTest.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton, NOMOD, rect.center())
        c.ready(spine=entries[k]["spine"])
        c.wait(lambda: r.toc_current == k, 3, "toc current after click")
        c.check("TOC click jumps to the entry and marks it current",
                r.spine_index == entries[k]["spine"] and r.toc_current == k,
                f"{entries[k]['title']!r} spine={r.spine_index} toc_current={r.toc_current}")
        c.check("single click keeps focus in the tree", tree.hasFocus() or win.focusWidget() is tree)
        c.shot("app_toc_open")
        # current-chapter tracking: move to the next chapter, the marker follows
        nxt = next(i for i in range(k + 1, len(entries)) if entries[i]["spine"] > entries[k]["spine"])
        c.focus_book()
        c.key(K.Key_Right, CTRL)                  # Ctrl+→ next chapter
        c.ready(spine=r.spine_index)
        c.wait(lambda: r.toc_current >= nxt - 0 and entries[r.toc_current]["spine"] == r.spine_index, 3,
               "toc follows")
        c.check("Ctrl+→ next chapter; the TOC marker follows the chapter",
                entries[r.toc_current]["spine"] == r.spine_index,
                f"spine={r.spine_index} toc_current={r.toc_current} ({entries[r.toc_current]['title']!r})")

    def search_walk() -> None:
        q = _search_query(book)
        c.key(K.Key_F, CTRL)
        c.check("Ctrl+F focuses the search field", r.dock.search.edit.hasFocus()
                or win.focusWidget() is r.dock.search.edit)
        r.dock.search.edit.insert(q)           # QTest cannot type CJK (qasciikey asserts)
        c.key(K.Key_Return, to=r.dock.search.edit)
        c.wait(lambda: r.search_index >= 0 and r.is_ready(), 12, "first hit")
        n = len(r.search_hits)
        i0 = r.search_index                     # the first hit at or after the reading position
        c.pump(0.3)
        c.focus_book()
        c.key(K.Key_N, to=c.proxy())             # n: through reader.js -> keyUnhandled('N')
        c.wait(lambda: r.search_index == (i0 + 1) % n, 5, "n -> next hit")
        c.check("n walks to the next result (view focused)", r.search_index == (i0 + 1) % n,
                f"{i0} -> {r.search_index} of {n}; {win.keys.last_fired}")
        c.key(K.Key_F3)
        c.wait(lambda: r.search_index == (i0 + 2) % n, 5, "F3 -> next hit")
        c.check("F3 walks to the next result", r.search_index == (i0 + 2) % n, win.keys.last_fired)
        c.focus_book()
        c.key(K.Key_N, SHIFT, to=c.proxy())
        c.wait(lambda: r.search_index == (i0 + 1) % n, 5, "N -> previous hit")
        c.key(K.Key_F3, SHIFT)
        c.wait(lambda: r.search_index == i0, 5, "Shift+F3 -> previous hit")
        c.check("N and Shift+F3 walk back", r.search_index == i0)
        c.ready()
        c.wait(lambda: r.page_state.get("matchActive", -1) >= 0, 5, "active match")
        text = c.page_text()
        c.check("the current result is on the page", q in text and r.spine_index
                == r.search_hits[i0].spine_index, f"q={q} hit {i0}")
        c.data["search_hits"] = len(r.search_hits)

    def bookmarks() -> None:
        c.focus_book()
        n0 = len(store.book_state(bid)["bookmarks"])
        c.key(K.Key_D, CTRL)
        c.wait(lambda: len(store.book_state(bid)["bookmarks"]) == n0 + 1, 3, "bookmark added")
        c.check("Ctrl+D adds a bookmark (stored immediately)", len(store.book_state(bid)["bookmarks"]) == n0 + 1)
        c.check("toolbar shows the page as bookmarked", r.toolbar._bookmarked)
        c.key(K.Key_B, CTRL)
        c.check("Ctrl+B shows the bookmarks pane", r.dock.isVisible() and r.dock.current_pane() == "bookmarks")
        c.pump(0.3)
        c.shot("app_bookmarks_pane")
        c.focus_book()
        c.key(K.Key_D, CTRL)
        c.wait(lambda: len(store.book_state(bid)["bookmarks"]) == n0, 3, "bookmark removed")
        c.check("Ctrl+D again removes it, with an undo link", r.statusbar.has_message()
                and S("common.undo") in r.statusbar.center.text() + r.statusbar.left.text()
                + r.statusbar.right.text() + " ".join(w.text() for w in r.statusbar.findChildren(QLabel)),
                " | ".join(w.text() for w in r.statusbar.findChildren(QLabel)))
        c.check("undo restores the bookmark", r.statusbar.trigger_undo()
                and len(store.book_state(bid)["bookmarks"]) == n0 + 1)

    def highlights() -> None:
        c.focus_book()
        r.next_page()
        c.pump(0.4)
        colors = []
        spots = [(0.3, 0.2), (0.5, 0.4), (0.4, 0.6), (0.6, 0.75)]
        for i, (mod_key, color) in enumerate(((K.Key_1, "yellow"), (K.Key_2, "green"),
                                              (K.Key_3, "blue"), (K.Key_4, "pink"))):
            ids_before = {h["id"] for h in store.book_state(bid)["highlights"]}
            sel = c.select_text(6, *spots[i])
            c.focus_book()
            c.key(mod_key, CTRL)
            c.wait(lambda ib=ids_before: len(store.book_state(bid)["highlights"]) == len(ib) + 1, 3, f"{color} hl")
            hl = next(h for h in store.book_state(bid)["highlights"] if h["id"] not in ids_before)
            colors.append(hl.get("color"))
            c.check(f"Ctrl+{i + 1} highlights the selection in {color}", hl.get("color") == color
                    and _norm(hl.get("text")) == _norm(sel), f"sel={sel!r} stored={hl.get('text')!r}")
        c.pump(0.4)
        marks = c.js("(function(){var o={};CSS.highlights.forEach(function(v,k){o[k]=v.size;});return o;})()")
        painted = [col for col in ("yellow", "green", "blue", "pink")
                   if isinstance(marks, dict) and (marks.get(f"er-hl-{col}") or marks.get(f"er-hu-{col}"))]
        c.check("the four highlights are painted on the page, one per colour",
                colors == ["yellow", "green", "blue", "pink"] and len(painted) == 4, f"registries={marks}")
        r.edit_highlight_note(hl["id"])
        c.pump(0.3)
        ed = r.note_editor
        c.check("the note editor opens", ed.isVisible())
        ed.edit.setPlainText("批注测试：清洁浮动的前提")
        c.shot("app_note_editor")
        c.key(K.Key_Return, CTRL, to=ed.edit)
        c.wait(lambda: any(h.get("note") for h in store.book_state(bid)["highlights"]), 3, "note saved")
        c.check("the note is saved with the highlight", any(h.get("note") == "批注测试：清洁浮动的前提"
                                                           for h in store.book_state(bid)["highlights"]))
        c.key(K.Key_E, CTRL)
        c.pump(0.3)
        c.check("Ctrl+E shows the highlights pane", r.dock.current_pane() == "notes" and r.dock.isVisible())
        c.shot("app_highlights_pane")

    def export() -> None:
        md = r.export_markdown()
        tmp = os.path.join(tempfile.gettempdir(), "book-reader-export-test.md")
        out = r.export_highlights(tmp)
        body = open(tmp, encoding="utf-8").read() if out and os.path.exists(tmp) else ""
        c.check("Markdown export has the title, all four quotes and the note",
                book.metadata.get("title", "") in md and md.count("> ") >= 4 and "批注测试" in md
                and body == md, md[:300])
        c.data["markdown_head"] = md[:600]
        try:
            os.remove(tmp)
        except OSError:
            pass

    def settings_live() -> None:
        c.focus_book()
        c.key(K.Key_Comma, CTRL)
        sp = r.settings_panel
        c.check("Ctrl+, opens the settings panel", sp.isVisible())
        c.pump(0.3)
        fs0 = c.js("parseFloat(getComputedStyle(document.body).fontSize)")
        glob0 = store.get("reader.font_size_px")
        # font size +3 through the panel's own control
        size_ctl = sp.font_size
        size_ctl.spin.setValue(int(glob0) + 3)          # what typing in the spin box does
        c.wait(lambda: store.get("reader.font_size_px") == glob0 + 3, 3, "font size stored")
        c.pump(0.4)
        fs1 = c.js("parseFloat(getComputedStyle(document.body).fontSize)")
        c.check("font size applies live from the panel", fs1 and fs0 and fs1 > fs0, f"{fs0} -> {fs1}")
        lh0 = c.js("getComputedStyle(document.body).lineHeight")
        sp.line_height.spin.setValue(2.2)
        c.pump(0.5)
        lh1 = c.js("getComputedStyle(document.body).lineHeight")
        c.check("line height applies live", store.get("reader.line_height") == 2.2 and lh0 != lh1, f"{lh0} -> {lh1}")
        # theme sepia via the segmented control
        sp_theme = sp.theme
        btn = sp_theme.buttons()["sepia"]
        btn.click()
        c.pump(0.6)
        bg = c.js("getComputedStyle(document.documentElement).backgroundColor")
        c.check("theme applies live to page and chrome", store.get("reader.theme") == "sepia"
                and r.theme.name == "sepia" and bg == "rgb(246, 240, 228)", f"bg={bg}")
        c.shot("app_settings_panel")
        # "this book only": the change goes to the book's overrides, global stays
        sp.this_book.click()
        c.pump(0.2)
        c.check("this-book toggle is on", r.this_book_only)
        size_ctl.spin.setValue(int(glob0) + 6)
        c.pump(0.8)
        ov = store.book_state(bid).get("overrides") or {}
        c.check("with 仅用于本书, the font size goes to the book's overrides only",
                ov.get("font_size_px") == glob0 + 6 and store.get("reader.font_size_px") == glob0 + 3, str(ov))
        fs2 = c.js("parseFloat(getComputedStyle(document.body).fontSize)")
        sp.this_book.click()
        c.pump(0.8)
        fs3 = c.js("parseFloat(getComputedStyle(document.body).fontSize)")
        c.check("unchecking deletes the override and the book snaps back to global",
                not (store.book_state(bid).get("overrides") or {}) and fs3 == fs1 and fs2 > fs1,
                f"{fs1} / {fs2} / {fs3}")
        # restore for later steps
        size_ctl.spin.setValue(int(glob0))
        sp.line_height.spin.setValue(1.9)
        sp_theme.buttons()["light"].click()
        c.pump(0.5)
        c.key(K.Key_Escape)
        c.check("Esc closes the settings panel", not sp.isVisible())

    def zoom() -> None:
        si = c.spine_with_images(book, 1)
        c.goto_spine(si)
        page_of = c.js("(function(){var W=innerWidth,se=document.scrollingElement;var i=document.images[0];"
                       "var b=i.getBoundingClientRect();return Math.floor((b.left+Math.abs(se.scrollLeft)+1)/W);})()")
        c.call("gotoPage", page_of)
        c.pump(0.5)
        pt = c.js("(function(){for(var k=0;k<document.images.length;k++){var b=document.images[k]"
                  ".getBoundingClientRect();if(b.width>20&&b.left>=0&&b.right<=innerWidth)"
                  "return [Math.round(b.left+b.width/2),Math.round(b.top+b.height/2)];}return null;})()")
        c.check("an image is on the page", isinstance(pt, list), str(pt))
        target = c.proxy()
        QTest.mouseClick(target, Qt.MouseButton.LeftButton, NOMOD, QPoint(int(pt[0]), int(pt[1])))
        c.wait(lambda: bool(c.js("!!document.getElementById('er-zoom')")), 3, "zoom overlay")
        c.pump(0.3)
        cover = c.js("(function(){var z=document.getElementById('er-zoom').getBoundingClientRect(),"
                     "i=document.querySelector('#er-zoom img').getBoundingClientRect();return [z.left,z.top,"
                     "z.width,z.height,innerWidth,innerHeight,Math.round(i.width),Math.round(i.height)];})()")
        c.check("clicking an image zooms it to a full-window overlay",
                isinstance(cover, list) and cover[0] <= 0 and cover[1] <= 0 and cover[2] >= cover[4] - 1
                and cover[3] >= cover[5] - 1, f"overlay/viewport/img={cover}")
        c.shot("app_image_zoom")
        dock_before = r.dock.isVisible()
        c.focus_book()
        c.key(K.Key_Escape, to=c.proxy(), settle=0.4)
        c.check("Esc closes the zoom first (panels untouched)",
                not c.js("!!document.getElementById('er-zoom')") and r.dock.isVisible() == dock_before)

    def cheatsheet_and_status() -> None:
        c.focus_book()
        c.key(K.Key_F1)
        c.check("F1 opens the cheat sheet", r.cheatsheet.isVisible())
        c.pump(0.3)
        c.shot("app_cheatsheet")
        c.key(K.Key_Escape)
        c.check("Esc closes the cheat sheet", not r.cheatsheet.isVisible())
        right = r.statusbar.right.text()
        prefix = S("status.left.chapter", time="").strip()
        c.check("status bar shows the chapter time left", bool(re.search(r"\d", right)) and prefix in right,
                f"left={r.statusbar.left.text()!r} center={r.statusbar.center.text()!r} right={right!r}")
        r.cycle_status_cell()
        c.pump(0.1)
        right2 = r.statusbar.right.text()
        r.cycle_status_cell()
        r.cycle_status_cell()
        c.check("the right cell cycles (chapter / book / time read)", right2 != right, f"{right!r} -> {right2!r}")

    def escape_ladder() -> None:
        c.focus_book()
        if not r.dock.isVisible():
            c.key(K.Key_T, CTRL)
        c.focus_book()
        c.key(K.Key_Comma, CTRL)
        c.check("setup: dock and settings open", r.dock.isVisible() and r.settings_panel.isVisible())
        c.focus_book()
        c.key(K.Key_Escape, to=c.proxy(), settle=0.35)
        c.check("Esc 1: closes the settings panel first", not r.settings_panel.isVisible() and r.dock.isVisible())
        c.focus_book()
        c.key(K.Key_Escape, to=c.proxy(), settle=0.35)
        c.check("Esc 2: then the dock", not r.dock.isVisible())
        c.focus_book()
        c.key(K.Key_F, CTRL | SHIFT, settle=0.6)
        c.check("Ctrl+Shift+F enters focus mode", r.is_zen())
        c.focus_book()
        c.key(K.Key_Escape, to=c.proxy(), settle=0.6)
        c.check("Esc 3: leaves focus mode", not r.is_zen() and not win.isFullScreen())
        c.focus_book()
        c.key(K.Key_F11, settle=0.6)
        c.check("F11 enters full screen", win.isFullScreen())
        c.focus_book()
        c.key(K.Key_Escape, to=c.proxy(), settle=0.6)
        c.wait(lambda: not win.isFullScreen(), 3, "leave full screen")
        c.check("Esc 4: leaves full screen", not win.isFullScreen())
        c.focus_book()
        sel = c.select_text(6)
        c.wait(lambda: r._last_selection, 3, "selection reported")
        c.focus_book()
        c.key(K.Key_Escape, to=c.proxy(), settle=0.4)
        cleared = c.js("getSelection().toString()")
        c.check("Esc 5: clears the selection", sel and not cleared, f"{sel!r} -> {cleared!r}")
        c.focus_book()
        c.key(K.Key_Escape, to=c.proxy(), settle=0.3)
        c.key(K.Key_Escape, to=c.proxy(), settle=0.3)
        c.check("Esc never closes the book or quits", r.book is not None and win.isVisible()
                and win.stack.currentWidget() is r)

    def gating() -> None:
        c.focus_book()
        c.key(K.Key_F, CTRL)
        edit = r.dock.search.edit
        edit.clear()
        g0 = (r.spine_index, r.page_state.get("page"))
        c.key(K.Key_J, to=edit)
        c.key(K.Key_K, to=edit)
        c.pump(0.3)
        c.check("j/k typed into the search field stay text (no page turn)",
                edit.text() == "jk" and (r.spine_index, r.page_state.get("page")) == g0, edit.text())
        c.key(K.Key_Left, to=edit)
        c.check("← in the search field moves the cursor, not the page",
                (r.spine_index, r.page_state.get("page")) == g0 and edit.cursorPosition() == 1)
        edit.clear()
        c.focus_book()
        before = (r.spine_index, r.page_state.get("page"), r.page_state.get("gpos"))
        c.key(K.Key_J, to=c.proxy(), settle=0.4)
        c.wait(lambda: (r.spine_index, r.page_state.get("page"), r.page_state.get("gpos")) != before, 3, "j")
        after_j = (r.spine_index, r.page_state.get("page"), r.page_state.get("gpos"))
        c.key(K.Key_K, to=c.proxy(), settle=0.4)
        c.wait(lambda: (r.spine_index, r.page_state.get("page")) == before[:2], 3, "k")
        c.check("j / k turn pages while the book has focus", after_j != before, f"{before} -> {after_j}")
        # Ctrl+C with a selection in the book is not intercepted: Chromium copies natively.
        # Two separate facts: (1) the app lets the key through and Chromium runs its Copy
        # command on the selection (observed via the DOM 'copy' event, independent of the OS),
        # and (2) the text reaches the Windows clipboard -- which another process can hold
        # locked machine-wide, in which case every app's copy fails and (2) is not the app's fault.
        sel = c.select_text(6)
        c.focus_book()
        c.js(COPY_LISTEN_JS)
        win.keys.last_fired = ""
        QGuiApplication.clipboard().clear()
        c.key(K.Key_C, CTRL, to=c.proxy(), settle=0.5)
        events = c.js(COPY_READ_JS)
        c.check("Ctrl+C with a selection is not intercepted; Chromium copies the selection",
                win.keys.last_fired == "" and isinstance(events, list) and sel in events,
                f"fired={win.keys.last_fired!r} copy-events={events!r} sel={sel!r}")
        if windows_clipboard_available():
            c.check("the copied selection reaches the clipboard",
                    _norm(QGuiApplication.clipboard().text()) == _norm(sel),
                    f"clip={QGuiApplication.clipboard().text()!r} sel={sel!r}")
        else:
            print("  [SKIP] clipboard contents: the Windows clipboard is locked by another process "
                  "(copy/paste fails in every app until it is released)", flush=True)
        c.call("clearSelection")
        # Space turns the page even when nothing has focus
        r.dock.search.edit.clearFocus()
        win.setFocus()
        c.pump(0.1)
        before = (r.spine_index, r.page_state.get("page"))
        c.key(K.Key_Space, to=win)
        c.wait(lambda: (r.spine_index, r.page_state.get("page")) != before, 3, "space")
        c.check("Space turns the page when no control has focus", True, win.keys.last_fired)

    for name, fn in (("toc", toc), ("search", search_walk), ("bookmarks", bookmarks), ("highlights", highlights),
                     ("export", export), ("settings", settings_live), ("zoom", zoom),
                     ("cheatsheet", cheatsheet_and_status), ("escape", escape_ladder), ("gating", gating)):
        c.step(name, fn)

    def ctrl_w() -> None:
        c.focus_book()
        c.key(K.Key_W, CTRL, settle=0.4)
        c.check("Ctrl+W closes the book and shows the shelf", win.stack.currentWidget() is win.library
                and r.book is None and win.windowTitle() == "Book Reader")
        c.check("the shelf flashes 已回到书架 (in the UI language)", c.lib.notice_text()
                == S("status.back_to_library"), c.lib.notice_text())
        c.key(K.Key_F1)
        c.check("F1 on the shelf shows the cheat sheet", win.library_cheatsheet.isVisible())
        c.key(K.Key_Escape)
        c.check("Esc closes it", not win.library_cheatsheet.isVisible())
        c.save()
        c.key(K.Key_W, CTRL, settle=0.05)          # on the shelf: closes the window -> quits
    c.step("ctrl_w", ctrl_w)


def _rgb_hex(css: str) -> str:
    nums = [int(float(x)) for x in re.findall(r"[\d.]+", css)[:3]]
    return "#%02X%02X%02X" % tuple(nums) if len(nums) == 3 else "#000000"


def _contrast(fg: str, bg: str) -> float:
    return theme_mod.contrast_ratio(_rgb_hex(fg), _rgb_hex(bg))


def _norm(s) -> str:
    return re.sub(r"\s+", "", str(s or ""))


def sc_restart_a(c: Ctx) -> None:
    """e (1/2). Page ~30 pages into a real book, record the position, quit with Ctrl+Q."""
    r = c.r
    c.activate()
    c.ready(timeout=25)
    book = r.book
    # start from a chapter with real text, then turn 30 pages with the keyboard
    start = next(i for i, s in enumerate(book.spine) if len(book.plain_text(s.zip_name)) > 3000)
    c.goto_spine(start)
    c.focus_book()
    turns = 0
    for _ in range(30):
        before = (r.spine_index, r.page_state.get("page"), r.page_state.get("gpos"))
        c.key(K.Key_PageDown, to=c.proxy(), settle=0.05)
        c.wait(lambda b=before: r.is_ready() and (r.spine_index, r.page_state.get("page"),
                                                  r.page_state.get("gpos")) != b, 6, "turn")
        turns += 1
    c.pump(1.0)
    st = dict(r.page_state)
    gpos = int(st.get("gpos") or 0)
    text = book.plain_text(book.spine[r.spine_index].zip_name)
    vis = c.visible_text(40)
    c.data.update({"spine": r.spine_index, "gpos": gpos, "page": st.get("page"), "pages": st.get("pages"),
                   "snippet": text[gpos:gpos + 40], "visible": vis, "book_id": r.book_id, "turns": turns,
                   "font_size": c.store.get("reader.font_size_px")})
    c.check("turned 30 pages with PageDown", turns == 30, f"spine {start} -> {r.spine_index}, page {st.get('page')}")
    c.check("the visible text starts at the locator's sentence",
            _norm(vis)[:10] == _norm(text[gpos:gpos + 40])[:10], f"vis={vis!r} gpos text={text[gpos:gpos+40]!r}")
    c.shot("app_restart_before")
    c.quit_via_key()


def sc_restart_b(c: Ctx) -> None:
    """e (2/2). Relaunched with no arguments: the last book reopens on the same sentence;
    then a font-size change keeps that sentence on screen."""
    r = c.r
    c.activate()
    prev = json.load(open(c.argv[0], encoding="utf-8"))["data"] if c.argv else {}
    try:
        c.ready(timeout=25)
    except AssertionError:
        pass
    c.check("the last book reopened on launch", r.book is not None and r.book_id == prev.get("book_id"),
            f"{r.book_id} vs {prev.get('book_id')}")
    c.pump(0.8)
    st = dict(r.page_state)
    book = r.book
    gpos = int(st.get("gpos") or -1)
    vis = c.visible_text(40)
    c.data.update({"spine": r.spine_index, "gpos": gpos, "page": st.get("page"), "visible": vis})
    c.check("same chapter and the same gpos as before the restart",
            r.spine_index == prev.get("spine") and gpos == prev.get("gpos"),
            f"now spine={r.spine_index} gpos={gpos} page={st.get('page')}; before spine={prev.get('spine')} "
            f"gpos={prev.get('gpos')} page={prev.get('page')}")
    c.check("the same visible text as before the restart", _norm(vis) == _norm(prev.get("visible")),
            f"now={vis!r} before={prev.get('visible')!r}")
    c.shot("app_restart_after")
    # a font size change must keep the sentence on screen
    snippet = _norm(prev.get("snippet"))[:12]
    c.focus_book()
    for _ in range(2):
        c.key(K.Key_Equal, CTRL, settle=0.1)
    c.wait(lambda: c.store.get("reader.font_size_px") == (prev.get("font_size") or 21) + 2, 3, "font +2")
    c.pump(1.2)
    page = c.page_text()
    st2 = dict(r.page_state)
    c.data.update({"after_font_gpos": st2.get("gpos"), "after_font_page": st2.get("page")})
    c.check("after Ctrl+= twice the same sentence is still on the page",
            snippet and snippet in page and r.spine_index == prev.get("spine"),
            f"font={c.store.get('reader.font_size_px')} page={st2.get('page')}/{st2.get('pages')} "
            f"gpos={st2.get('gpos')} snippet={snippet!r} page_text_head={page[:30]!r}")
    c.shot("app_restart_font_bigger")
    c.key(K.Key_0, CTRL, settle=0.4)                # Ctrl+0 resets the size
    c.check("Ctrl+0 resets the font size", c.store.get("reader.font_size_px") == 21)
    c.quit_via_key()


def sc_lang(c: Ctx) -> None:
    """f. live language switching while reading and on the shelf; screenshots per language."""
    from test_reader_page import stale_entries  # the reader owner's verified checker

    win, r = c.win, c.r
    c.activate()
    books = [b for b in REAL_BOOKS if os.path.exists(b)]
    lib = c.lib
    token = lib.add_paths(books + [os.path.join(FIXTURES, "epub2_ncx.epub")], open_single=False)
    c.wait(lambda: not lib.is_busy(), 30, "shelf adds")
    lib.wait_for_workers(20000)
    c.pump(0.5)
    win.open_path(books[1] if len(books) > 1 else os.path.join(FIXTURES, "epub2_ncx.epub"))
    c.ready(timeout=25)
    r.next_chapter()
    c.ready()
    r.next_chapter()
    c.ready()
    r.toggle_bookmark()
    q = _search_query(r.book)
    r.focus_search()
    r.dock.search.edit.setText(q)
    r.run_search(q)
    r.find_next()
    c.ready()
    r.set_settings_visible(True)
    c.pump(0.4)
    order = ["zh-Hans", "zh-Hant", "en", "ja"]
    combo = r.settings_panel.language
    i = combo.findData("zh-Hans")
    combo.setCurrentIndex(i)
    combo.activated.emit(i)
    c.pump(0.4)
    prev_lang, prev_snap = "zh-Hans", snapshot_window(win)
    r.reveal_chrome()
    c.pump(0.1)
    c.shot("app_lang_zh-Hans_reading")
    for lang in order[1:]:
        i = combo.findData(lang)
        combo.setCurrentIndex(i)
        combo.activated.emit(i)                       # what picking it in the panel does
        c.pump(0.5)
        c.check(f"reading: {prev_lang} -> {lang} applied and saved",
                strings.current_language() == lang and c.store.get("ui.language") == lang)
        snap = snapshot_window(win)
        stale = stale_entries(prev_snap, snap, prev_lang, lang)
        c.check(f"reading: {prev_lang} -> {lang}: no label, tooltip, placeholder, menu item or status text "
                f"shows {prev_lang} ({len(snap)} strings)", not stale, "; ".join(stale[:8]))
        c.check(f"reading: {lang}: window title", win.windowTitle().endswith("— Book Reader"))
        r.reveal_chrome()
        c.pump(0.1)
        c.shot(f"app_lang_{lang}_reading")
        prev_lang, prev_snap = lang, snap
    r.set_settings_visible(False)
    # the reading view in every language without the panel (for the eye)
    for lang in order:
        win.set_language(lang)
        r.reveal_chrome()
        c.pump(0.4)
        c.shot(f"app_reading_{lang}")
    win.set_language("ja")
    c.pump(0.3)
    # on the shelf: through the "…" menu's language items
    r.back_to_library()
    c.pump(0.6)
    prev_lang, prev_snap = "ja", snapshot_window(win)
    for lang in ["zh-Hans", "zh-Hant", "en", "ja"]:
        lang_menu = lib.more_menu.findChild(QMenu, "LibraryLanguageMenu")
        act = next(a for a in lang_menu.actions() if a.data() == lang)
        act.trigger()                                  # what picking it in the … menu does
        c.pump(0.5)
        snap = snapshot_window(win)
        stale = stale_entries(prev_snap, snap, prev_lang, lang)
        c.check(f"shelf: {prev_lang} -> {lang} via the … menu: nothing shows {prev_lang} ({len(snap)} strings)",
                strings.current_language() == lang and not stale, "; ".join(stale[:8]))
        c.shot(f"app_shelf_{lang}")
        # negative control: one planted previous-language string must be caught
        lib.search.setPlaceholderText(strings.translate(prev_lang, "lib.search"))
        caught = stale_entries(prev_snap, snapshot_window(win), prev_lang, lang)
        c.check(f"shelf: {lang}: the checker catches a planted {prev_lang} placeholder", len(caught) == 1,
                "; ".join(caught[:3]))
        lib.retranslate_ui()
        prev_lang, prev_snap = lang, snap
    # leave it on Japanese for the restart check
    win.set_language("ja")
    c.pump(0.3)
    lib.more_btn.showMenu() if False else None
    c.data["final_language"] = strings.current_language()
    c.quit_via_key()


def sc_lang_check(c: Ctx) -> None:
    """f (restart). The chosen language survives a restart."""
    c.activate()
    c.pump(0.5)
    c.check("the language persisted across the restart (ja)", strings.current_language() == "ja"
            and c.store.get("ui.language") == "ja", strings.current_language())
    c.check("the shelf is in Japanese", c.lib.open_btn.text() == strings.translate("ja", "lib.open")
            or c.lib.open_btn.text() == "", c.lib.open_btn.text())
    c.shot("app_lang_after_restart")
    c.quit_via_key()


def sc_single_primary(c: Ctx) -> None:
    """g (primary). Wait for a second launch to hand over a book."""
    win, r = c.win, c.r
    marker = c.argv[0]
    expected = c.argv[1]
    c.check("primary is listening on the pipe", win.instance is not None and win.instance.server is not None
            and win.instance.server.isListening())
    open(marker, "w").write("ready")
    try:
        c.wait(lambda: r.book is not None and os.path.normcase(r.book.path) == os.path.normcase(expected), 40,
               "handed-over book")
        c.ready(timeout=20)
    except AssertionError as exc:
        c.check("handed-over book opened", False, str(exc))
    c.check("the second launch's book opened in the first window", r.book is not None
            and os.path.normcase(r.book.path) == os.path.normcase(expected),
            f"received={win.instance.received if win.instance else None}")
    c.check("message was OPEN <path>", bool(win.instance and win.instance.received
                                            and win.instance.received[-1].startswith("OPEN ")))
    c.shot("app_single_instance")
    c.quit_via_key()


def sc_crash(c: Ctx) -> None:
    """An unexpected exception in a slot, raised from the MAIN event loop (as in real use):
    logged, the in-window card is shown, the app keeps running."""
    win = c.win
    c.activate()

    def boom() -> None:
        raise RuntimeError("deliberate test failure (sc_crash)")

    def phase2() -> None:
        c.step("crash card", lambda: _crash_checks(c))
        c.quit_via_key()

    # NOT raised inside c.pump(): PySide6 6.11 propagates a slot's exception into
    # the Python frame running a nested processEvents(), which is a harness artefact
    QTimer.singleShot(200, boom)
    QTimer.singleShot(1500, phase2)


def _crash_checks(c: Ctx) -> None:
    win = c.win
    c.wait(lambda: win.crash_card.isVisible(), 5, "crash card")
    c.check("an unhandled exception shows the in-window error card", win.crash_card.isVisible()
            and "deliberate test failure" in win.crash_card.last_text)
    c.check("the card names the log file", win.crash_card.path.text().endswith("book-reader.log"),
            win.crash_card.path.text())
    win.crash_card.details_btn.setChecked(True)
    c.pump(0.3)
    c.shot("app_crash_card")
    win.crash_card.close_btn.click()
    win.open_path(os.path.join(FIXTURES, "epub2_ncx.epub"))
    c.ready()
    c.check("the app keeps working after the error", c.r.book is not None and not win.crash_card.isVisible())


def sc_shots(c: Ctx) -> None:
    """4. The screenshot set: shelf, reading in three themes, TOC, settings (language), search,
    error card, F1."""
    win, r, lib = c.win, c.r, c.lib
    c.activate()
    books = [b for b in REAL_BOOKS if os.path.exists(b)]
    lib.add_paths(books + [os.path.join(FIXTURES, f) for f in ("epub2_ncx.epub", "images_fonts.epub",
                                                             "cjk_vertical.epub", "no_toc.epub",
                                                             "drm_fake.epub")], open_single=False)
    c.wait(lambda: not lib.is_busy(), 30, "adds")
    lib.wait_for_workers(20000)
    c.pump(0.5)
    # give one book some progress so 继续阅读 appears
    win.open_path(books[0])
    c.ready(timeout=25)
    for _ in range(6):
        r.next_chapter()
        c.ready()
    r.back_to_library()
    c.pump(0.8)
    lib.wait_for_workers(10000)
    c.pump(0.4)
    c.shot("shelf")
    c.check("the shelf shows the continue-reading row and every book",
            any("continue" in str(h) or S("lib.section.continue") in str(h) for h in lib.view.headers())
            and len(lib.entries()) >= 8, f"{len(lib.entries())} books; headers={[h[0] for h in lib.view.headers()]}")
    lib.set_view_mode("list")
    c.pump(0.4)
    c.shot("shelf_list")
    lib.set_view_mode("grid")
    win.open_path(books[1])
    c.ready(timeout=25)
    si = next(i for i, s in enumerate(r.book.spine) if len(r.book.plain_text(s.zip_name)) > 5000)
    c.goto_spine(si)
    r.next_page()
    c.pump(0.5)
    r.set_dock_visible(False)
    for name in ("light", "sepia", "dark"):
        r.set_theme_choice(name)
        c.pump(0.8)
        r.hide_chrome()
        c.pump(0.2)
        c.shot(f"reading_{name}_clean")
        r.reveal_chrome()
        c.pump(0.3)
        c.shot(f"reading_{name}")
        bg_chrome = win.grab().toImage().pixelColor(win.width() // 2, 5).name()
        page_bg = c.js("getComputedStyle(document.documentElement).backgroundColor")
        c.data[f"chrome_{name}"] = bg_chrome
        c.data[f"page_{name}"] = page_bg
        t = theme_mod.THEMES[name]
        c.check(f"{name}: the toolbar and the page are painted from the same theme",
                bg_chrome.lower() == t.chrome_bg.lower() and _rgb_hex(page_bg).lower() == t.bg.lower(),
                f"toolbar={bg_chrome} (token {t.chrome_bg}) page={page_bg} (token {t.bg})")
        img = win.grab().toImage()
        sb = img.pixelColor(win.width() // 2, win.height() - 4).name()
        c.check(f"{name}: the status bar too", sb.lower() == t.chrome_bg.lower(), sb)
    r.set_theme_choice("light")
    c.pump(0.5)
    r.toggle_toc()
    c.pump(0.5)
    c.shot("toc_open")
    r.set_settings_visible(True)
    c.pump(0.4)
    sa = r.settings_panel.findChild(QWidget, "settings-scroll") or None
    lang = r.settings_panel.language
    try:
        area = next(w for w in r.settings_panel.findChildren(QWidget) if w.metaObject().className() == "QScrollArea")
        area.ensureWidgetVisible(lang)
    except StopIteration:
        pass
    c.pump(0.3)
    c.shot("settings_language")
    r.set_settings_visible(False)
    q = _search_query(r.book)
    r.focus_search()
    r.dock.search.edit.setText(q)
    c.key(K.Key_Return, to=r.dock.search.edit)
    c.wait(lambda: r.search_index >= 0 and r.is_ready(), 12, "search")
    c.pump(0.6)
    c.shot("search_results")
    r.toggle_cheatsheet()
    c.pump(0.4)
    c.shot("cheatsheet")
    r.toggle_cheatsheet()
    win.open_path(os.path.join(FIXTURES, "drm_fake.epub"))
    c.pump(0.4)
    c.shot("error_card")
    for name in ("sepia", "dark"):
        r.set_theme_choice(name)
        c.pump(0.4)
        c.shot(f"error_card_{name}")
    r.set_theme_choice("dark")
    r.back_to_library()
    c.pump(0.6)
    c.shot("shelf_dark")
    lib.more_menu.aboutToShow.emit()
    r.set_theme_choice("light")
    c.pump(0.3)
    c.quit_via_key()


def sc_probe(c: Ctx) -> None:
    """Ad-hoc investigation: runs the Python file given as the first extra argument with ``c`` in scope."""
    code = open(c.argv[0], encoding="utf-8").read()
    exec(compile(code, c.argv[0], "exec"), {"c": c, "K": K, "CTRL": CTRL, "SHIFT": SHIFT, "NOMOD": NOMOD,
                                            "FIXTURES": FIXTURES, "REAL_BOOKS": REAL_BOOKS, "QTest": QTest,
                                            "Qt": Qt, "S": S, "strings": strings, "os": os, "time": time})
    if not c.argv[1:] or c.argv[1] != "--stay":
        c.quit_via_key()


FORMAT_SAMPLES = os.path.join(HERE, "samples")
PAGE_IMG_JS = ("(function(){var i=document.querySelector('img.pg');"
               "return i?{w:i.naturalWidth,h:i.naturalHeight}:null})()")


def sc_formats(c: Ctx) -> None:
    """Kindle (MOBI, AZW3) and DjVu books open, render, turn pages and search in the real app."""
    r, win = c.r, c.win
    kindle = (("gb11.mobi", "Rabbit", 16), ("gb11_kf8.azw3", "Rabbit", 16),
              ("gb24264.mobi", "寶玉", 100), ("gb24264_kf8.azw3", "寶玉", 120))
    for name, query, min_toc in kindle:
        path = os.path.join(FORMAT_SAMPLES, name)
        if not os.path.isfile(path):
            c.check(f"{name}: sample present", False, "tests/samples is missing it")
            continue
        win.open_path(path)
        c.ready(timeout=40)
        book = r.book
        entries = []

        def walk(es):
            for e in es:
                entries.append(e)
                walk(e.children)
        walk(book.toc)
        c.check(f"{name}: opens as {book.source_format}", bool(r.page_state) and book.source_format in ("mobi", "azw3"),
                win.windowTitle())
        c.check(f"{name}: contents list", len(entries) >= min_toc, f"{len(entries)} entries")
        c.check(f"{name}: search '{query}'", len(book.search(query)) > 5)
        before = r.spine_index
        r.next_chapter()
        c.ready(timeout=20)
        c.check(f"{name}: next chapter", r.spine_index == before + 1, f"{before} -> {r.spine_index}")
    djvu_books = (("ia_indiansummer.djvu", "Colville", 15), ("ia_jstor_20637537.djvu", "JSTOR", 0))
    for name, query, text_page in djvu_books:
        path = os.path.join(FORMAT_SAMPLES, name)
        if not os.path.isfile(path):
            c.check(f"{name}: sample present", False, "tests/samples is missing it")
            continue
        win.open_path(path)
        c.ready(timeout=60)
        book = r.book
        c.check(f"{name}: opens as a fixed-layout DjVu book",
                book.source_format == "djvu" and book.is_fixed_layout and bool(r.page_state))
        c.wait(lambda: (c.js(PAGE_IMG_JS) or {}).get("w", 0) > 0, 20, "page image")
        c.check(f"{name}: the scanned page image decodes", (c.js(PAGE_IMG_JS) or {}).get("w", 0) > 500,
                str(c.js(PAGE_IMG_JS)))
        c.check(f"{name}: search in the OCR text", len(book.search(query)) > 0)
        r._goto(text_page)
        c.ready(timeout=30)
        c.wait(lambda: (c.js(PAGE_IMG_JS) or {}).get("w", 0) > 0, 20, "page image")
        inside = c.js("(function(){var s=document.querySelector('span.t'),i=document.querySelector('img.pg');"
                      "if(!s||!i)return null;var a=s.getBoundingClientRect(),b=i.getBoundingClientRect();"
                      "return a.left>=b.left&&a.right<=b.right&&a.top>=b.top&&a.bottom<=b.bottom})()")
        c.check(f"{name}: OCR words lie on the scanned page", inside is True, str(inside))
        c.shot("app_format_" + name.split(".")[0])
    c.quit_via_key()


SCENARIOS = {
    "probe": sc_probe, "formats": sc_formats,
    "launch": sc_launch, "fixtures": sc_fixtures, "real": sc_real, "features": sc_features,
    "restart_a": sc_restart_a, "restart_b": sc_restart_b, "lang": sc_lang, "lang_check": sc_lang_check,
    "single_primary": sc_single_primary, "crash": sc_crash, "shots": sc_shots,
}


def main() -> int:
    scenario, result_path, out = sys.argv[1], sys.argv[2], sys.argv[3]
    rest = sys.argv[4:]
    extra: list[str] = []
    app_args: list[str] = []
    if "--" in rest:
        cut = rest.index("--")
        extra, app_args = rest[:cut], rest[cut + 1:]
    else:
        app_args = rest
    fn = SCENARIOS[scenario]
    holder: dict = {}

    def started(win) -> None:
        ctx = Ctx(win, scenario, result_path, out, extra)
        holder["ctx"] = ctx
        # watchdog: never leave a window up
        QTimer.singleShot(240_000, lambda: (ctx.check("watchdog: scenario finished in 240 s", False),
                                            ctx.save(), os._exit(9)))
        try:
            fn(ctx)
        except Exception as exc:  # noqa: BLE001
            ctx.check("scenario raised", False, "".join(traceback.format_exception(exc))[-1500:])
            ctx.save()
            QApplication.instance().quit()
        ctx.save()
        # if the scenario's own quit did not end the loop, end it
        QTimer.singleShot(8000, lambda: (ctx.check("app quit through its own path within 8 s", False),
                                         ctx.save(), QApplication.instance().quit()))

    rc = epub_reader.main([os.path.join(ROOT, "epub_reader.py"), *app_args], on_started=started)
    ctx = holder.get("ctx")
    if ctx is not None:
        ctx.data["exit_code"] = rc
        ctx.save()
    else:
        with open(result_path, "w", encoding="utf-8") as fh:
            json.dump({"scenario": scenario, "rows": [["on_started ran", False, f"rc={rc}"]], "data": {}},
                      fh)
    return rc


if __name__ == "__main__":
    sys.exit(main())
