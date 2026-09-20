# -*- coding: utf-8 -*-
"""Book Reader — the reading surface (owner E, CONTRACT §6).

``ReaderPage`` is one ``QWidget`` that holds everything a reader sees while a
book is open (product spec §3):

* the 44 px **toolbar**, auto-hidden after ``behavior.auto_hide_chrome_ms`` of
  mouse stillness and revealed again within 60 px of the top edge.  It floats
  over the reading column, so hiding it never re-flows the book;
* ONE **left dock** with a segmented control over four panes: contents,
  bookmarks, highlights (批注) and search;
* the 320 px **settings panel** on the right.  Every control applies live;
  there is no OK / Cancel / Apply;
* the 26 px **status bar**: chapter · percent · time left (right-click the
  right cell to cycle it);
* the in-pane **error cards** (never a modal ``QMessageBox``) and the in-flow
  placeholder for one chapter that cannot be displayed;
* the **F1 cheat sheet** overlay built from ``strings.CHEATSHEET_LAYOUT``.

The page persists nothing itself: every read and write goes through
``store.Store``.  It binds no application shortcut.  Instead, each keyboard
action has one public slot, and :data:`ACTION_SLOTS` maps every
``strings.KEYS`` id to those slots so ``epub_reader.py`` can bind them all in
one place.  All text comes from ``strings.S()``, and ``retranslate_ui()``
refreshes every visible string when the UI language changes.

Import order: this module imports :mod:`webhost`, which registers the
``epub://`` scheme at import time.  Import it BEFORE ``QApplication`` exists.
"""

from __future__ import annotations

import datetime as _dt
import html
import math
import os
import re
import threading
import logging
import time
from typing import Any, Callable, Iterable, Sequence

import webhost  # registers epub:// at import (must precede QApplication)

from PySide6.QtCore import (
    QEvent,
    QModelIndex,
    QObject,
    QPoint,
    QPointF,
    QProcess,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QCursor,
    QDesktopServices,
    QFont,
    QFontDatabase,
    QFontMetrics,
    QFontMetricsF,
    QGuiApplication,
    QIcon,
    QKeyEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QStandardItem,
    QStandardItemModel,
    QTextCharFormat,
    QTextLayout,
    QTextOption,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView

import store as store_mod
import strings
import theme as theme_mod
import bookformats
from epublib import EpubBook, EpubError, SearchHit
from store import Store, now_iso
from strings import KEYS, S, duration, format_date, plural, tip

__all__ = [
    "ReaderPage",
    "ACTION_SLOTS",
    "PAGE_KEY_ACTIONS",
    "PANES",
    "ReadingTracker",
    "SegmentedControl",
    "TOOLBAR_HEIGHT",
    "STATUS_HEIGHT",
    "SETTINGS_WIDTH",
    "DOCK_DEFAULT_WIDTH",
    "DOCK_MIN_WIDTH",
    "DOCK_MAX_WIDTH",
    "SEARCH_RENDER_CAP",
    "TYPOGRAPHY_KEYS",
    "available_fonts",
]

# ==========================================================================
# constants (product spec §3 unless noted)
# ==========================================================================

TOOLBAR_HEIGHT = 44
STATUS_HEIGHT = 26
SETTINGS_WIDTH = 320
DOCK_DEFAULT_WIDTH = 280
DOCK_MIN_WIDTH = 200
DOCK_MAX_WIDTH = 480
REVEAL_ZONE_PX = 60
TOC_SCROLL_GRACE_S = 5.0
SEARCH_RENDER_CAP = 500
SEARCH_LIMIT = 100_000
log = logging.getLogger("reader_page")

UNDO_MS = 3000
NOTE_MS = 3000
LOADING_DELAY_MS = 300          # never show "opening…" for anything faster (spec §5)
READY_WATCHDOG_MS = 8000        # a chapter not initialised by then lost a load signal: retry once
POSITION_CAPTURE_MS = 350       # capture the pinned locator this long after a move
APPLY_DEBOUNCE_MS = 40
OVERRIDE_DEBOUNCE_MS = 400
HISTORY_LIMIT = 50

# reading speed (spec "Reading-speed learning"): EWMA, clamped, seeded
SPEED_MIN = 120.0
SPEED_MAX = 1200.0
SPEED_SEED = 300.0
SPEED_ALPHA = 0.25
SESSION_MIN_S = 90.0
IDLE_TIMEOUT_S = 120.0
MAX_STEP_UNITS = 6000           # one observed move larger than this is a jump, not reading

PANES: tuple[str, ...] = ("toc", "bookmarks", "notes", "search")

#: The ``reader.*`` keys that "this book only" may override per book.
TYPOGRAPHY_KEYS: tuple[str, ...] = (
    "layout", "font_cjk", "font_latin", "use_book_fonts", "font_size_px",
    "font_weight", "line_height", "para_spacing_em", "text_indent_ch",
    "text_align", "page_margin_px", "max_measure_ch",
)

FONT_SIZE_MIN, FONT_SIZE_MAX = 14, 32
LINE_HEIGHT_MIN, LINE_HEIGHT_MAX = 1.2, 2.4
PARA_SPACING_MIN, PARA_SPACING_MAX = 0.0, 1.5
MARGIN_MIN, MARGIN_MAX = 24, 160
MEASURE_MIN, MEASURE_MAX = 28, 60

#: Preferred families, intersected with QFontDatabase at runtime (spec pitfall
#: 10: never offer a font that is not installed).
PREFERRED_CJK_FONTS: tuple[str, ...] = (
    "Microsoft YaHei", "DengXian", "SimSun", "NSimSun", "SimHei", "KaiTi",
    "FangSong", "STKaiti", "STFangsong", "STSong", "STZhongsong",
    "Noto Serif SC", "Noto Sans SC", "Microsoft JhengHei", "YouYuan",
)
PREFERRED_LATIN_FONTS: tuple[str, ...] = (
    "Georgia", "Cambria", "Constantia", "Palatino Linotype", "Sitka Text",
    "Book Antiqua", "Times New Roman", "Segoe UI", "Calibri", "Garamond",
)

#: ``strings.KEYS`` id -> ReaderPage slot name(s).  With one name every combo of
#: that id binds to it; with several, combo *i* binds to name *i*.  Library-screen
#: ids are not listed (owner F).  The in-page keys (Space, arrows, PageUp/Down,
#: Home/End, Enter) are already handled by reader.js while the book has focus;
#: bind them application-wide only under the spec's gating rule.
ACTION_SLOTS: dict[str, tuple[str, ...]] = {
    "next_page": ("next_page",),
    "prev_page": ("prev_page",),
    "page_jk": ("next_page", "prev_page"),
    "next_chapter": ("next_chapter",),
    "prev_chapter": ("prev_chapter",),
    "chapter_edge": ("chapter_start", "chapter_end"),
    "book_edge": ("book_start", "book_end"),
    "jump_history": ("jump_back", "jump_forward"),
    "goto": ("goto",),
    "find_next": ("find_next",),
    "find_prev": ("find_prev",),
    "toc": ("toggle_toc",),
    "bookmarks": ("toggle_bookmarks",),
    "notes": ("toggle_notes",),
    "search": ("focus_search",),
    "typography": ("toggle_settings",),
    "escape": ("escape",),
    "cheatsheet": ("toggle_cheatsheet",),
    "bookmark_toggle": ("toggle_bookmark",),
    "copy": ("copy_selection",),
    "copy_cite": ("copy_with_citation",),
    "highlight": ("highlight_yellow", "highlight_green", "highlight_blue", "highlight_pink"),
    "highlight_delete": ("delete_active_highlight",),
    "toggle_mode": ("toggle_layout_mode",),
    "font_step": ("font_larger", "font_smaller"),
    "font_reset": ("font_reset",),
    "toggle_daynight": ("toggle_day_night",),
    "fullscreen": ("toggle_fullscreen",),
    "zen": ("toggle_zen",),
    "open": ("request_open",),
    "library": ("back_to_library",),
    "close_book": ("close_and_return",),
    "quit": ("request_quit",),
    "back": ("jump_back",),
    "forward": ("jump_forward",),
}

#: Key descriptions reader.js forwards through ``keyUnhandled`` (only while the
#: book view has focus and no input is focused — exactly the spec's gating rule
#: for single-letter keys) -> slot.  See ReaderPage.dispatch_page_key().
PAGE_KEY_ACTIONS: dict[str, str] = {
    "J": "next_page",
    "K": "prev_page",
    "N": "find_next",
    "Shift+N": "find_prev",
    "/": "focus_search",
    "Shift+/": "focus_search",
    "F3": "find_next",
    "Shift+F3": "find_prev",
    "F1": "toggle_cheatsheet",
    "Ctrl+/": "toggle_cheatsheet",
    "Escape": "escape",
    "Delete": "delete_active_highlight",
    "F11": "toggle_fullscreen",
}
#: The subset ReaderPage handles by itself when ``handle_letter_keys`` is on.
_LETTER_KEYS = frozenset({"J", "K", "N", "Shift+N", "/", "Shift+/"})

_WS_RE = re.compile(r"\s+")
_ROLE_DATA = int(Qt.ItemDataRole.UserRole) + 1


# ==========================================================================
# small helpers
# ==========================================================================

def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def _collapse(text: str) -> str:
    """Whitespace runs -> one space, trimmed (for previews and list rows)."""
    return _WS_RE.sub(" ", text or "").strip()


def _mix(a: str, b: str, t: float) -> QColor:
    """Blend two ``#rrggbb`` colours: ``t=0`` gives *a*, ``t=1`` gives *b*."""
    ca, cb = QColor(a), QColor(b)
    return QColor(round(ca.red() + (cb.red() - ca.red()) * t),
                  round(ca.green() + (cb.green() - ca.green()) * t),
                  round(ca.blue() + (cb.blue() - ca.blue()) * t))


def _u16(text: str) -> int:
    """Length of *text* in UTF-16 code units (Qt string indices)."""
    return len(text.encode("utf-16-le")) // 2


def _set_role(widget: QWidget, role: str) -> None:
    """Set the theme.py ``erRole`` hook and re-polish so the QSS rule applies."""
    if widget.property("erRole") == role:
        return
    widget.setProperty("erRole", role)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def release_focus(container: QWidget) -> None:
    """Drop keyboard focus held inside *container* before it is hidden.

    Hiding a focused widget makes Qt focus the next widget in the tab chain
    with ``TabFocusReason``.  When that is the book view, Chromium treats it
    as a Tab into the page: it focuses the first link, draws a focus ring and
    scrolls to it, so the reading position moves (seen on a real book when a
    card was dismissed).  Callers then focus the book explicitly.
    """
    fw = QApplication.focusWidget()
    if fw is not None and (fw is container or container.isAncestorOf(fw)):
        fw.clearFocus()


def hide_quietly(widget: QWidget) -> None:
    """``release_focus`` + ``hide``."""
    if widget.isVisible():
        release_focus(widget)
    widget.hide()


def _book_data(widget: QWidget) -> QWidget:
    """Mark a widget whose text is book data (titles), not UI copy."""
    widget.setProperty("erBookData", True)
    return widget


def _percent_int(fraction: float) -> int:
    return int(math.floor(_clamp(float(fraction or 0.0), 0.0, 1.0) * 100 + 1e-9))


_FONT_CACHE: list[str] | None = None


def available_fonts() -> tuple[list[str], list[str]]:
    """``(cjk, latin)`` font families that are actually installed.

    The preferred lists intersected with ``QFontDatabase.families()``.  Needs a
    QApplication with the real platform plugin (offscreen reports none).
    """
    global _FONT_CACHE
    if _FONT_CACHE is None:
        try:
            _FONT_CACHE = list(QFontDatabase.families())
        except Exception:  # noqa: BLE001 - no GUI yet
            _FONT_CACHE = []
    installed = {f.casefold(): f for f in _FONT_CACHE}
    cjk = [installed[f.casefold()] for f in PREFERRED_CJK_FONTS if f.casefold() in installed]
    latin = [installed[f.casefold()] for f in PREFERRED_LATIN_FONTS if f.casefold() in installed]
    return cjk, latin


# ==========================================================================
# icons: drawn with QPainter so no image assets and no text glyphs are needed
# ==========================================================================

def _pen(p: QPainter, color: QColor, width: float = 1.7) -> None:
    pen = QPen(color, width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)


def make_icon(name: str, color: str, size: int = 24, fill: str | None = None) -> QIcon:
    """A line icon drawn at 2x for crisp rendering on any scale factor."""
    dpr = 2.0
    pix = QPixmap(int(size * dpr), int(size * dpr))
    pix.setDevicePixelRatio(dpr)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    c = QColor(color)
    k = size / 24.0
    p.scale(k, k)
    _pen(p, c)
    if name == "toc":
        for y in (7.0, 12.0, 17.0):
            p.drawLine(QPointF(9, y), QPointF(19, y))
            p.setBrush(c)
            p.drawEllipse(QPointF(5.5, y), 0.9, 0.9)
            p.setBrush(Qt.BrushStyle.NoBrush)
    elif name == "back":
        path = QPainterPath(QPointF(14.5, 6))
        path.lineTo(8.5, 12)
        path.lineTo(14.5, 18)
        p.drawPath(path)
    elif name == "forward":
        path = QPainterPath(QPointF(9.5, 6))
        path.lineTo(15.5, 12)
        path.lineTo(9.5, 18)
        p.drawPath(path)
    elif name == "typography":
        big = QPainterPath(QPointF(3.5, 19))
        big.lineTo(9.5, 5)
        big.lineTo(15.5, 19)
        p.drawPath(big)
        p.drawLine(QPointF(5.8, 14.2), QPointF(13.2, 14.2))
        small = QPainterPath(QPointF(14.5, 19))
        small.lineTo(17.8, 11)
        small.lineTo(21.1, 19)
        p.drawPath(small)
        p.drawLine(QPointF(15.7, 16.2), QPointF(19.9, 16.2))
    elif name == "search":
        p.drawEllipse(QPointF(10.5, 10.5), 5.6, 5.6)
        p.drawLine(QPointF(14.6, 14.6), QPointF(19.5, 19.5))
    elif name in ("bookmark", "bookmark_on"):
        path = QPainterPath(QPointF(7, 4.5))
        path.lineTo(17, 4.5)
        path.lineTo(17, 19.5)
        path.lineTo(12, 15.5)
        path.lineTo(7, 19.5)
        path.closeSubpath()
        if name == "bookmark_on":
            p.setBrush(QColor(fill or color))
        p.drawPath(path)
    elif name == "fullscreen":
        for (x, y, dx, dy) in ((5, 9, 0, -4), (19, 9, 0, -4), (5, 15, 0, 4), (19, 15, 0, 4)):
            path = QPainterPath(QPointF(x, y))
            path.lineTo(x, y + dy)
            path.lineTo(x + (4 if x < 12 else -4), y + dy)
            p.drawPath(path)
    elif name == "fullscreen_exit":
        for (x, y, sx, sy) in ((9, 5, -1, 1), (15, 5, 1, 1), (9, 19, -1, -1), (15, 19, 1, -1)):
            path = QPainterPath(QPointF(x, y))
            path.lineTo(x, y + 4 * sy)
            path.lineTo(x + 4 * sx, y + 4 * sy)
            p.drawPath(path)
    elif name == "more":
        p.setBrush(c)
        p.setPen(Qt.PenStyle.NoPen)
        for x in (6.0, 12.0, 18.0):
            p.drawEllipse(QPointF(x, 12), 1.7, 1.7)
    elif name == "close":
        p.drawLine(QPointF(7, 7), QPointF(17, 17))
        p.drawLine(QPointF(17, 7), QPointF(7, 17))
    elif name == "up":
        path = QPainterPath(QPointF(6.5, 14.5))
        path.lineTo(12, 9)
        path.lineTo(17.5, 14.5)
        p.drawPath(path)
    elif name == "down":
        path = QPainterPath(QPointF(6.5, 9.5))
        path.lineTo(12, 15)
        path.lineTo(17.5, 9.5)
        p.drawPath(path)
    elif name == "chevron_right":
        path = QPainterPath(QPointF(10, 7))
        path.lineTo(15, 12)
        path.lineTo(10, 17)
        p.drawPath(path)
    elif name == "chevron_down":
        path = QPainterPath(QPointF(7, 10))
        path.lineTo(12, 15)
        path.lineTo(17, 10)
        p.drawPath(path)
    elif name == "note":
        p.drawRoundedRect(QRectF(5, 5, 14, 14), 2, 2)
        p.drawLine(QPointF(8.5, 10), QPointF(15.5, 10))
        p.drawLine(QPointF(8.5, 14), QPointF(13, 14))
    p.end()
    return QIcon(pix)


def swatch_icon(fill: str, ring: str, size: int = 18, checked: bool = False) -> QIcon:
    """A round highlight-colour chip (fill + ink ring), optionally with a check mark."""
    dpr = 2.0
    pix = QPixmap(int(size * dpr), int(size * dpr))
    pix.setDevicePixelRatio(dpr)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setPen(QPen(QColor(ring), 2.0))
    p.setBrush(QColor(fill))
    r = size / 2.0 - 1.5
    p.drawEllipse(QPointF(size / 2.0, size / 2.0), r, r)
    if checked:
        _pen(p, QColor(ring), 2.0)
        path = QPainterPath(QPointF(size * 0.30, size * 0.52))
        path.lineTo(size * 0.45, size * 0.66)
        path.lineTo(size * 0.72, size * 0.36)
        p.drawPath(path)
    p.end()
    return QIcon(pix)


# ==========================================================================
# wrapped text with a line cap (list rows)
# ==========================================================================

def _text_layout(text: str, font: QFont, width: float, max_lines: int,
                 formats: list | None = None) -> tuple[QTextLayout, list]:
    lay = QTextLayout(text, font)
    opt = QTextOption()
    opt.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    lay.setTextOption(opt)
    if formats:
        lay.setFormats(formats)
    lay.beginLayout()
    lines = []
    y = 0.0
    while True:
        line = lay.createLine()
        if not line.isValid():
            break
        line.setLineWidth(max(1.0, width))
        line.setPosition(QPointF(0.0, y))
        y += line.height()
        lines.append(line)
        if max_lines and len(lines) > max_lines:
            break
    lay.endLayout()
    return lay, lines


def text_block_height(text: str, font: QFont, width: float, max_lines: int) -> float:
    """Height of *text* wrapped to *width*, capped at *max_lines* lines."""
    if not text:
        return 0.0
    _lay, lines = _text_layout(text, font, width, max_lines)
    shown = lines[:max_lines] if max_lines else lines
    return sum(line.height() for line in shown)


def draw_text_block(p: QPainter, x: float, y: float, text: str, font: QFont, width: float,
                    max_lines: int, color: QColor,
                    marks: Sequence[tuple[int, int]] = (),
                    mark_bg: QColor | None = None, mark_fg: QColor | None = None) -> float:
    """Draw wrapped text; the last allowed line is elided.  Returns the height used.

    *marks* are ``(start, length)`` in Python indices, painted with the find colours.
    """
    if not text:
        return 0.0
    formats = []
    for start, length in marks:
        if length <= 0:
            continue
        fr = QTextLayout.FormatRange()
        fr.start = _u16(text[:start])
        fr.length = _u16(text[start:start + length])
        fmt = QTextCharFormat()
        if mark_bg is not None:
            fmt.setBackground(mark_bg)
        if mark_fg is not None:
            fmt.setForeground(mark_fg)
        fr.format = fmt
        formats.append(fr)
    lay, lines = _text_layout(text, font, width, max_lines, formats)
    p.save()
    p.setPen(color)
    p.setFont(font)
    truncated = bool(max_lines) and len(lines) > max_lines
    if not truncated:
        lay.draw(p, QPointF(x, y))
        p.restore()
        return sum(line.height() for line in lines)
    shown = lines[:max_lines]
    head = sum(line.height() for line in shown[:-1])
    if head > 0:
        p.save()
        p.setClipRect(QRectF(x - 1, y, width + 2, head))
        lay.draw(p, QPointF(x, y))
        p.restore()
    last = shown[-1]
    # QTextLine.textStart() is a UTF-16 index; map it back to a Python index.
    u16_start = last.textStart()
    py_start = len(text.encode("utf-16-le")[: u16_start * 2].decode("utf-16-le", "ignore"))
    rest = text[py_start:].replace(" ", " ").replace("\n", " ")
    elided = QFontMetricsF(font).elidedText(rest, Qt.TextElideMode.ElideRight, width)
    p.drawText(QPointF(x, y + last.y() + last.ascent()), elided)
    p.restore()
    return head + last.height()


# ==========================================================================
# reading speed (spec: EWMA over sessions >= 90 s with no idle gap > 120 s)
# ==========================================================================

class ReadingTracker:
    """Learns reading speed in units per minute (one CJK char or one Latin word).

    Call :meth:`observe` with the book-level unit position after every move;
    ``sequential`` is False for jumps (TOC, search, bookmarks), which re-base the
    count without crediting any reading.  A *segment* ends at an idle gap longer
    than ``idle_s``; a segment of at least ``min_session_s`` that advanced gives
    one sample, clamped to 120–1200, folded in with EWMA (alpha 0.25).
    """

    def __init__(self, speed: float = SPEED_SEED, *, idle_s: float = IDLE_TIMEOUT_S,
                 min_session_s: float = SESSION_MIN_S) -> None:
        self.speed = _clamp(float(speed or SPEED_SEED), SPEED_MIN, SPEED_MAX)
        self.idle_s = float(idle_s)
        self.min_session_s = float(min_session_s)
        self.samples: list[float] = []
        self.total_seconds = 0.0
        self.total_units = 0.0
        self._last_t: float | None = None
        self._last_units: float | None = None
        self._seg_seconds = 0.0
        self._seg_units = 0.0

    def observe(self, unit_pos: float, now: float, *, sequential: bool = True) -> None:
        """Record a position; time since the previous one counts unless idle."""
        if self._last_t is not None:
            gap = now - self._last_t
            if gap > self.idle_s:
                self._close_segment()
            elif gap > 0:
                self._seg_seconds += gap
                self.total_seconds += gap
        if sequential and self._last_units is not None:
            delta = unit_pos - self._last_units
            if 0 < delta <= MAX_STEP_UNITS:
                self._seg_units += delta
                self.total_units += delta
        self._last_t = now
        self._last_units = unit_pos

    def finish(self, now: float) -> None:
        """End the session (book closed): count the tail and close the segment."""
        if self._last_t is not None:
            gap = now - self._last_t
            if 0 < gap <= self.idle_s:
                self._seg_seconds += gap
                self.total_seconds += gap
        self._close_segment()
        self._last_t = None
        self._last_units = None

    def _close_segment(self) -> None:
        if self._seg_seconds >= self.min_session_s and self._seg_units > 0:
            sample = _clamp(self._seg_units / (self._seg_seconds / 60.0), SPEED_MIN, SPEED_MAX)
            self.samples.append(sample)
            self.speed = _clamp((1 - SPEED_ALPHA) * self.speed + SPEED_ALPHA * sample,
                                SPEED_MIN, SPEED_MAX)
        self._seg_seconds = 0.0
        self._seg_units = 0.0

    def minutes_for(self, units: float) -> float:
        """Minutes to read *units* at the learned speed."""
        return max(0.0, float(units)) / max(self.speed, SPEED_MIN)


# ==========================================================================
# segmented control (erRole "segment" buttons, labels elided to fit)
# ==========================================================================

LabelSpec = str | Callable[[], str]


class SegmentedControl(QWidget):
    """A row of exclusive checkable buttons.  ``changed(key)`` on user choice.

    Labels are string keys (or callables) resolved at :meth:`retranslate_ui`
    time, and elided when a language's label is wider than its segment; the
    full label is then the tooltip.
    """

    changed = Signal(str)

    def __init__(self, items: Sequence[tuple[str, LabelSpec]], parent: QWidget | None = None,
                 *, tooltips: dict[str, LabelSpec] | None = None) -> None:
        super().__init__(parent)
        self._items = list(items)
        self._tooltips = dict(tooltips or {})
        self._buttons: dict[str, QToolButton] = {}
        self._full: dict[str, str] = {}
        self._current = ""
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        for key, _label in self._items:
            b = QToolButton(self)
            b.setProperty("erRole", "segment")
            b.setCheckable(True)
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            b.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            b.setMinimumWidth(24)
            b.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            b.setObjectName(f"segment-{key}")
            b.clicked.connect(lambda _checked=False, k=key: self._on_click(k))
            self._group.addButton(b)
            lay.addWidget(b, 1)
            self._buttons[key] = b
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        # theme.py pads segments 12 px each side; labels such as 跟随系统 or
        # Japanese katakana need that room for text in a 320 px panel.
        self.setStyleSheet('QToolButton[erRole="segment"] { padding-left: 4px; padding-right: 4px; }')
        self.retranslate_ui()

    @staticmethod
    def _resolve(spec: LabelSpec) -> str:
        return spec() if callable(spec) else S(spec)

    def buttons(self) -> dict[str, QToolButton]:
        return dict(self._buttons)

    def current(self) -> str:
        return self._current

    def set_current(self, key: str, *, emit: bool = False) -> None:
        b = self._buttons.get(key)
        if b is None:
            return
        self._current = key
        b.setChecked(True)
        if emit:
            self.changed.emit(key)

    def _on_click(self, key: str) -> None:
        if key == self._current:
            return
        self._current = key
        self.changed.emit(key)

    def retranslate_ui(self) -> None:
        for key, label in self._items:
            self._full[key] = self._resolve(label)
        self._fit()

    def resizeEvent(self, event: Any) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._fit()

    def sizeHint(self) -> QSize:  # noqa: N802
        fm = self.fontMetrics()
        widest = max((fm.horizontalAdvance(t) for t in self._full.values()), default=40)
        h = max(28, fm.height() + 12)
        return QSize((widest + 28) * max(1, len(self._items)), h)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(40 * max(1, len(self._items)), max(28, self.fontMetrics().height() + 12))

    def _fit(self) -> None:
        """Share the row in proportion to the labels when they all fit, else
        equally with elision (the full label is then the tooltip).

        Stretch factors, never fixed widths: a fixed width would pin the row's
        minimum at whatever width it first saw and stop it shrinking later.
        """
        n = max(1, len(self._items))
        h = max(28, self.fontMetrics().height() + 12)
        overhead = 10                      # 4 px padding each side + 1 px borders
        total = max(n * 20, self.width())
        natural = {k: b.fontMetrics().horizontalAdvance(self._full.get(k, "")) + overhead
                   for k, b in self._buttons.items()}
        need = sum(natural.values())
        lay = self.layout()
        for i, (key, b) in enumerate(self._buttons.items()):
            if need <= total:
                stretch = max(1, natural[key])
                width = total * natural[key] / need
            else:
                stretch = 1
                width = total / n
            lay.setStretch(i, stretch)
            b.setFixedHeight(h)
            full = self._full.get(key, "")
            shown = b.fontMetrics().elidedText(full, Qt.TextElideMode.ElideRight,
                                               max(8, int(width) - overhead))
            b.setText(shown)
            extra_tip = self._tooltips.get(key)
            if extra_tip is not None:
                b.setToolTip(self._resolve(extra_tip))
            else:
                b.setToolTip(full if shown != full else "")
            b.setAccessibleName(full)


# ==========================================================================
# list rows painted by one delegate (bookmarks, highlights, search results)
# ==========================================================================

class RowDelegate(QStyledItemDelegate):
    """Paints rows described by a spec dict built at paint time.

    The spec is rebuilt on every paint from the row's *data*, so a language
    change is a repaint, never a stale cached string.  Keys::

        kind      'row' (default) | 'group' | 'info'
        title     small secondary line above the text (book data or UI copy)
        meta      small secondary text right-aligned on the title line
        text      main text, wrapped to text_lines (default 3)
        marks     [(start, length)] ranges of `text` painted with the find colours
        note      secondary text under the main text, note_lines (default 2)
        badge     small pill under the text
        bar       '#rrggbb' 3 px left bar
        muted     greyed (lost anchors)
        flash     draw the accent flash background
    """

    PAD_X = 12
    PAD_Y = 7
    BAR_GAP = 8

    def __init__(self, view: QAbstractItemView, build: Callable[[QModelIndex], dict],
                 theme_fn: Callable[[], theme_mod.Theme]) -> None:
        super().__init__(view)
        self._view = view
        self._build = build
        self._theme_fn = theme_fn

    # -- fonts ---------------------------------------------------------------
    def _fonts(self) -> tuple[QFont, QFont, QFont]:
        base = QFont(self._view.font())
        small = QFont(base)
        size = base.pointSizeF()
        if size > 0:
            small.setPointSizeF(max(7.0, size * 0.9))
        bold = QFont(base)
        bold.setBold(True)
        return base, small, bold

    def _inner_width(self, spec: dict, width: int) -> float:
        left = self.PAD_X + (3 + self.BAR_GAP if spec.get("bar") else 0)
        return max(40.0, width - left - self.PAD_X)

    def row_height(self, spec: dict, width: int) -> int:
        base, small, bold = self._fonts()
        kind = spec.get("kind", "row")
        if kind == "group":
            return int(math.ceil(QFontMetricsF(bold).height() + 14))
        inner = self._inner_width(spec, width)
        if kind == "info":
            return int(math.ceil(text_block_height(spec.get("text", ""), small, inner, 4)
                                 + 2 * self.PAD_Y))
        h = float(self.PAD_Y)
        if spec.get("title") or spec.get("meta"):
            h += QFontMetricsF(small).height() + 2
        if spec.get("text"):
            h += text_block_height(spec["text"], base, inner, int(spec.get("text_lines", 3)))
        if spec.get("note"):
            h += 3 + text_block_height(spec["note"], small, inner, int(spec.get("note_lines", 2)))
        if spec.get("badge"):
            h += 4 + QFontMetricsF(small).height() + 2
        h += self.PAD_Y
        return int(math.ceil(h))

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # noqa: N802
        width = max(80, self._view.viewport().width())
        return QSize(width, self.row_height(self._build(index), width))

    def paint(self, p: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        spec = self._build(index)
        t = self._theme_fn()
        base, small, bold = self._fonts()
        r = option.rect
        state = option.state
        kind = spec.get("kind", "row")
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        selected = bool(state & QStyle.StateFlag.State_Selected) and kind == "row"
        hover = bool(state & QStyle.StateFlag.State_MouseOver) and kind == "row" \
            and not spec.get("muted")
        if spec.get("flash"):
            p.fillRect(r, QColor(t.find_bg))
        elif selected:
            p.fillRect(r, QColor(t.selection))
        elif hover:
            p.fillRect(r, QColor(t.chrome_hover))
        fg = QColor(t.fg if (selected or spec.get("flash")) else t.chrome_fg)
        sec = QColor(t.chrome_secondary)
        if spec.get("muted"):
            fg = QColor(t.chrome_secondary)
            sec = QColor(_mix(t.chrome_secondary, t.chrome_bg, 0.35))
        if kind == "group":
            p.setFont(bold)
            p.setPen(QColor(t.chrome_fg))
            fm = QFontMetricsF(bold)
            text = fm.elidedText(spec.get("title", ""), Qt.TextElideMode.ElideRight,
                                 r.width() - 2 * self.PAD_X)
            p.drawText(QPointF(r.x() + self.PAD_X, r.y() + 10 + fm.ascent()), text)
            p.restore()
            return
        x = float(r.x() + self.PAD_X)
        if spec.get("bar"):
            bar = QColor(spec["bar"])
            if spec.get("muted"):
                bar = QColor(_mix(spec["bar"], t.chrome_bg, 0.55))
            p.fillRect(QRectF(x, r.y() + 6, 3, r.height() - 12), bar)
            x += 3 + self.BAR_GAP
        inner = self._inner_width(spec, r.width())
        y = float(r.y() + self.PAD_Y)
        if kind == "info":
            draw_text_block(p, x, y, spec.get("text", ""), small, inner, 4, sec)
            p.restore()
            return
        if spec.get("title") or spec.get("meta"):
            fm = QFontMetricsF(small)
            meta = spec.get("meta") or ""
            meta_w = fm.horizontalAdvance(meta) if meta else 0.0
            title_w = max(20.0, inner - meta_w - (10 if meta else 0))
            p.setFont(small)
            p.setPen(sec)
            title = fm.elidedText(spec.get("title") or "", Qt.TextElideMode.ElideRight, title_w)
            p.drawText(QPointF(x, y + fm.ascent()), title)
            if meta:
                p.drawText(QPointF(x + inner - meta_w, y + fm.ascent()), meta)
            y += fm.height() + 2
        if spec.get("text"):
            y += draw_text_block(p, x, y, spec["text"], base, inner, int(spec.get("text_lines", 3)),
                                 fg, spec.get("marks") or (),
                                 QColor(t.find_bg), QColor(t.find_fg))
        if spec.get("note"):
            y += 3
            y += draw_text_block(p, x, y, spec["note"], small, inner,
                                 int(spec.get("note_lines", 2)), sec)
        if spec.get("badge"):
            y += 4
            fm = QFontMetricsF(small)
            label = spec["badge"]
            w = fm.horizontalAdvance(label) + 12
            rect = QRectF(x, y, w, fm.height() + 2)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(t.chrome_pressed))
            p.drawRoundedRect(rect, 4, 4)
            p.setPen(QColor(t.chrome_fg))
            p.setFont(small)
            p.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), label)
        p.restore()


def _make_row_list(parent: QWidget) -> QListWidget:
    lst = QListWidget(parent)
    lst.setUniformItemSizes(False)
    lst.setWordWrap(True)
    lst.setMouseTracking(True)
    lst.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    lst.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    lst.setResizeMode(QListView.ResizeMode.Adjust)
    lst.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    lst.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    lst.setFrameShape(QFrame.Shape.NoFrame)
    lst.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    return lst


# ==========================================================================
# toolbar (44 px, floats over the reading column)
# ==========================================================================

class ChromeToolbar(QFrame):
    """``[toc][back][forward] — title · chapter — [Aa][search][bookmark][full][more]``.

    Icon-only buttons (drawn icons) with ``label (shortcut)`` tooltips; the
    "more" menu replaces the menu bar and shows each item's shortcut.
    """

    def __init__(self, page: "ReaderPage", parent: QWidget) -> None:
        super().__init__(parent)
        self._page = page
        self.setObjectName("er-toolbar")
        self.setProperty("erRole", "toolbar")
        self.setFixedHeight(TOOLBAR_HEIGHT)
        self._title = ""
        self._chapter = ""
        self._bookmarked = False
        self._fullscreen = False
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 3, 8, 3)
        lay.setSpacing(2)

        def button(name: str) -> QToolButton:
            b = QToolButton(self)
            b.setObjectName(f"tb-{name}")
            b.setAutoRaise(True)
            b.setIconSize(QSize(24, 24))
            b.setFixedSize(36, 36)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            return b

        self.btn_toc = button("toc")
        self.btn_back = button("back")
        self.btn_forward = button("forward")
        self.btn_typography = button("typography")
        self.btn_search = button("search")
        self.btn_bookmark = button("bookmark")
        self.btn_fullscreen = button("fullscreen")
        self.btn_more = button("more")
        self.btn_more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)

        for b in (self.btn_toc, self.btn_back, self.btn_forward):
            lay.addWidget(b)
        # balance the two button groups so the title sits in the true centre
        self._balance = QWidget(self)
        self._balance.setFixedWidth(2 * 36 + 2 * 2)
        self._balance.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        lay.addWidget(self._balance)
        self.title_label = _book_data(QLabel(self))
        self.title_label.setObjectName("tb-title")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.title_label.setTextFormat(Qt.TextFormat.PlainText)
        lay.addWidget(self.title_label, 1)
        for b in (self.btn_typography, self.btn_search, self.btn_bookmark,
                  self.btn_fullscreen, self.btn_more):
            lay.addWidget(b)

        self.menu = QMenu(self)
        self.menu.setObjectName("tb-more-menu")
        self.actions: dict[str, QAction] = {}
        for name in ("open", "library", "-", "bookinfo", "export", "convert", "-",
                     "shortcuts", "settings", "about", "-", "quit"):
            if name == "-":
                self.menu.addSeparator()
                continue
            act = QAction(self.menu)
            act.setObjectName(f"menu-{name}")
            self.menu.addAction(act)
            self.actions[name] = act
        self.btn_more.setMenu(self.menu)

        self.btn_toc.clicked.connect(page.toggle_toc)
        self.btn_back.clicked.connect(page.jump_back)
        self.btn_forward.clicked.connect(page.jump_forward)
        self.btn_typography.clicked.connect(page.toggle_settings)
        self.btn_search.clicked.connect(page.focus_search)
        self.btn_bookmark.clicked.connect(page.toggle_bookmark)
        self.btn_fullscreen.clicked.connect(page.toggle_fullscreen)
        self.actions["open"].triggered.connect(page.request_open)
        self.actions["library"].triggered.connect(page.back_to_library)
        self.actions["bookinfo"].triggered.connect(page.show_book_info)
        self.actions["export"].triggered.connect(lambda: page.export_highlights())
        self.actions["convert"].triggered.connect(lambda: page.convert_book())
        self.menu.aboutToShow.connect(self.retranslate_ui)      # the convert label follows the format
        self.actions["shortcuts"].triggered.connect(page.toggle_cheatsheet)
        self.actions["settings"].triggered.connect(page.toggle_settings)
        self.actions["about"].triggered.connect(page.request_about)
        self.actions["quit"].triggered.connect(page.request_quit)
        self.retranslate_ui()

    # -- state ----------------------------------------------------------------
    def set_title(self, title: str, chapter: str) -> None:
        self._title, self._chapter = title or "", chapter or ""
        self._fit_title()

    def set_bookmarked(self, on: bool) -> None:
        if on != self._bookmarked:
            self._bookmarked = on
            self.apply_theme(self._page.theme)
            self.retranslate_ui()

    def set_fullscreen(self, on: bool) -> None:
        if on != self._fullscreen:
            self._fullscreen = on
            self.apply_theme(self._page.theme)
            self.retranslate_ui()

    def set_history(self, can_back: bool, can_forward: bool) -> None:
        self.btn_back.setEnabled(can_back)
        self.btn_forward.setEnabled(can_forward)

    def set_book_actions_enabled(self, on: bool) -> None:
        for b in (self.btn_toc, self.btn_typography, self.btn_search, self.btn_bookmark):
            b.setEnabled(on)
        for name in ("bookinfo", "export"):
            self.actions[name].setEnabled(on)

    def _fit_title(self) -> None:
        if self._title and self._chapter and self._chapter != self._title:
            full = S("tb.title", title=self._title, chapter=self._chapter)
        else:
            full = self._title or self._chapter
        width = max(10, self.title_label.width() - 8)
        self.title_label.setText(self.title_label.fontMetrics().elidedText(
            full, Qt.TextElideMode.ElideMiddle, width))
        self.title_label.setToolTip(full if self.title_label.text() != full else "")

    def resizeEvent(self, event: Any) -> None:  # noqa: N802
        super().resizeEvent(event)
        # centre the title only when there is room; a narrow column needs the width
        self._balance.setVisible(self.width() >= 720)
        self._fit_title()

    # -- language / theme -----------------------------------------------------
    def retranslate_ui(self) -> None:
        pairs = (
            (self.btn_toc, "tb.toc", "toc"),
            (self.btn_back, "tb.back", "back"),
            (self.btn_forward, "tb.forward", "forward"),
            (self.btn_typography, "tb.typography", "typography"),
            (self.btn_search, "tb.search", "search"),
            (self.btn_bookmark, "tb.bookmark.remove" if self._bookmarked else "tb.bookmark.add",
             "bookmark_toggle"),
            (self.btn_fullscreen, "tb.fullscreen.exit" if self._fullscreen else "tb.fullscreen",
             "fullscreen"),
            (self.btn_more, "tb.more", None),
        )
        for b, key, kid in pairs:
            b.setToolTip(tip(key, kid))
            b.setAccessibleName(S(key))
        rows = {
            "open": ("menu.open", "open"),
            "library": ("menu.library", "library"),
            "bookinfo": ("menu.bookinfo", None),
            "export": ("menu.export", None),
            "convert": ("menu.convert_pdf" if self._page.source_format() == "djvu" else "menu.convert", None),
            "shortcuts": ("menu.shortcuts", "cheatsheet"),
            "settings": ("menu.settings", "typography"),
            "about": ("menu.about", None),
            "quit": ("menu.quit", "quit"),
        }
        for name, (key, kid) in rows.items():
            text = S(key)
            if kid and KEYS.get(kid):
                text += "\t" + KEYS[kid][0]
            self.actions[name].setText(text)
        self.actions["convert"].setEnabled(bool(self._page.source_format()))
        self._fit_title()

    def apply_theme(self, t: theme_mod.Theme) -> None:
        fg = t.chrome_fg
        self.btn_toc.setIcon(make_icon("toc", fg))
        self.btn_back.setIcon(make_icon("back", fg))
        self.btn_forward.setIcon(make_icon("forward", fg))
        self.btn_typography.setIcon(make_icon("typography", fg))
        self.btn_search.setIcon(make_icon("search", fg))
        if self._bookmarked:
            self.btn_bookmark.setIcon(make_icon("bookmark_on", t.accent, fill=t.accent))
        else:
            self.btn_bookmark.setIcon(make_icon("bookmark", fg))
        self.btn_fullscreen.setIcon(make_icon("fullscreen_exit" if self._fullscreen else "fullscreen", fg))
        self.btn_more.setIcon(make_icon("more", fg))


# ==========================================================================
# status bar (26 px): chapter · percent · time left
# ==========================================================================

class ChromeStatusBar(QFrame):
    """Three cells.  Right-click the right cell to cycle what it shows.

    Transient messages (with an optional undo link) replace the left cell for
    a few seconds.  Messages are stored as render callables so a language
    change re-renders them.
    """

    MODES: tuple[str, ...] = ("chapter", "book", "read")
    rightModeChanged = Signal(str)

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("er-statusbar")
        self.setProperty("erRole", "statusbar")
        self.setFixedHeight(STATUS_HEIGHT)
        grid = QGridLayout(self)
        grid.setContentsMargins(14, 0, 14, 0)
        grid.setHorizontalSpacing(12)
        self.left = QLabel(self)
        self.left.setObjectName("status-left")
        self.left.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.left.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse)
        self.left.linkActivated.connect(self._on_link)
        self.center = QLabel(self)
        self.center.setObjectName("status-center")
        self.center.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.right = QLabel(self)
        self.right.setObjectName("status-right")
        self.right.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.right.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.right.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.right.customContextMenuRequested.connect(self._right_menu)
        grid.addWidget(self.left, 0, 0)
        grid.addWidget(self.center, 0, 1)
        grid.addWidget(self.right, 0, 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(2, 1)
        self.mode = "chapter"
        self._chapter = ""
        self._center_fn: Callable[[], str] | None = None
        self._right_fn: Callable[[], str] | None = None
        self._message_fn: Callable[[], str] | None = None
        self._undo_cb: Callable[[], None] | None = None
        self._msg_timer = QTimer(self)
        self._msg_timer.setSingleShot(True)
        self._msg_timer.timeout.connect(self.clear_message)
        self.retranslate_ui()

    def has_message(self) -> bool:
        return self._message_fn is not None

    def set_cells(self, chapter: str, center: Callable[[], str] | None,
                  right: Callable[[], str] | None) -> None:
        self._chapter = chapter or ""
        self._center_fn = center
        self._right_fn = right
        self._render()

    def show_message(self, render: Callable[[], str], *, undo: Callable[[], None] | None = None,
                     ms: int = NOTE_MS) -> None:
        self._message_fn = render
        self._undo_cb = undo
        self._msg_timer.start(max(500, ms))
        self._render()

    def clear_message(self) -> None:
        self._message_fn = None
        self._undo_cb = None
        self._msg_timer.stop()
        self._render()

    def trigger_undo(self) -> bool:
        """Run the pending undo, as if its link were clicked.  False when none."""
        if self._undo_cb is None:
            return False
        self._on_link("undo")
        return True

    def _on_link(self, _href: str) -> None:
        cb = self._undo_cb
        self._undo_cb = None
        if cb is not None:
            cb()

    def _render(self) -> None:
        if self._message_fn is not None:
            msg = html.escape(self._message_fn())
            self.left.setProperty("erBookData", False)
            self.left.setTextFormat(Qt.TextFormat.RichText)
            if self._undo_cb is not None:
                self.left.setText(f'{msg}&nbsp;&nbsp;<a href="undo">{html.escape(S("common.undo"))}</a>')
            else:
                self.left.setText(msg)
        else:
            self.left.setProperty("erBookData", True)
            self.left.setTextFormat(Qt.TextFormat.PlainText)
            width = max(10, self.left.width())
            self.left.setText(self.left.fontMetrics().elidedText(
                self._chapter, Qt.TextElideMode.ElideRight, width))
        self.center.setText(self._center_fn() if self._center_fn else "")
        right = self._right_fn() if self._right_fn else ""
        self.right.setText(self.right.fontMetrics().elidedText(
            right, Qt.TextElideMode.ElideLeft, max(10, self.right.width())))

    def resizeEvent(self, event: Any) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._render()

    def _right_menu(self, pos: QPoint) -> None:
        menu = QMenu(self)
        for mode in self.MODES:
            act = menu.addAction(S(f"status.cell.{mode}"))
            act.setCheckable(True)
            act.setChecked(mode == self.mode)
            act.triggered.connect(lambda _c=False, m=mode: self.set_mode(m, emit=True))
        menu.exec(self.right.mapToGlobal(pos))

    def cycle_mode(self) -> None:
        i = self.MODES.index(self.mode) if self.mode in self.MODES else 0
        self.set_mode(self.MODES[(i + 1) % len(self.MODES)], emit=True)

    def set_mode(self, mode: str, *, emit: bool = False) -> None:
        if mode not in self.MODES:
            mode = "chapter"
        self.mode = mode
        if emit:
            self.rightModeChanged.emit(mode)
        self._render()

    def retranslate_ui(self) -> None:
        self.right.setToolTip(S("status.cell.tip"))
        self._render()


# ==========================================================================
# dock pane: contents
# ==========================================================================

class _PaddedDelegate(QStyledItemDelegate):
    """Adds vertical breathing room to plain rows (QSS item padding is unsafe, see theme.py)."""

    def __init__(self, parent: QWidget, extra: int = 8) -> None:
        super().__init__(parent)
        self._extra = extra

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # noqa: N802
        size = super().sizeHint(option, index)
        return QSize(size.width(), size.height() + self._extra)


class TocTree(QTreeView):
    """The contents tree: current row bold with a 3 px accent bar at the left edge."""

    escapePressed = Signal()
    enterPressed = Signal(QModelIndex)

    def __init__(self, parent: QWidget, theme_fn: Callable[[], theme_mod.Theme]) -> None:
        super().__init__(parent)
        self._theme_fn = theme_fn
        self.current_row: QModelIndex | None = None
        self.last_user_scroll = 0.0
        self.setObjectName("toc-tree")
        self.setHeaderHidden(True)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setExpandsOnDoubleClick(False)
        self.setUniformRowHeights(True)
        self.setIndentation(16)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAnimated(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.setMouseTracking(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setItemDelegate(_PaddedDelegate(self, 9))
        self.verticalScrollBar().actionTriggered.connect(self._user_scrolled)

    def _user_scrolled(self, *_: Any) -> None:
        self.last_user_scroll = time.monotonic()

    def wheelEvent(self, event: Any) -> None:  # noqa: N802
        self.last_user_scroll = time.monotonic()
        super().wheelEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            idx = self.currentIndex()
            if idx.isValid():
                self.enterPressed.emit(idx)
                event.accept()
                return
        if key == Qt.Key.Key_Escape:
            self.escapePressed.emit()
            event.accept()
            return
        if key in (Qt.Key.Key_PageUp, Qt.Key.Key_PageDown, Qt.Key.Key_Up, Qt.Key.Key_Down):
            self.last_user_scroll = time.monotonic()
        super().keyPressEvent(event)

    def drawRow(self, painter: QPainter, option: QStyleOptionViewItem,  # noqa: N802
                index: QModelIndex) -> None:
        super().drawRow(painter, option, index)
        cur = self.current_row
        if cur is not None and cur.isValid() and index == cur:
            painter.fillRect(0, option.rect.y() + 3, 3, option.rect.height() - 6,
                             QColor(self._theme_fn().accent))


class TocPane(QWidget):
    """Contents.  Single click jumps and keeps focus; double-click returns focus to the book."""

    jumpRequested = Signal(int, bool)   # flat entry index, focus the book afterwards
    escapeRequested = Signal()

    def __init__(self, parent: QWidget, theme_fn: Callable[[], theme_mod.Theme]) -> None:
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 4, 0, 0)
        v.setSpacing(4)
        self.note = QLabel(self)
        self.note.setObjectName("toc-note")
        self.note.setProperty("erRole", "secondary")
        self.note.setWordWrap(True)
        self.note.setContentsMargins(12, 2, 12, 4)
        self.note.hide()
        v.addWidget(self.note)
        self.tree = TocTree(self, theme_fn)
        self.model = QStandardItemModel(self)
        self.tree.setModel(self.model)
        v.addWidget(self.tree, 1)
        self.empty = QLabel(self)
        self.empty.setObjectName("toc-empty")
        self.empty.setProperty("erRole", "secondary")
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setContentsMargins(16, 24, 16, 24)
        self.empty.hide()
        v.addWidget(self.empty)
        v.addStretch(0)
        #: flat entries in TOC order: {item, title, zip, fragment, spine, depth}
        self.entries: list[dict] = []
        self.current = -1
        self.synthetic = False
        self.tree.clicked.connect(lambda i: self._emit(i, False))
        self.tree.doubleClicked.connect(lambda i: self._emit(i, True))
        self.tree.enterPressed.connect(lambda i: self._emit(i, False))
        self.tree.escapePressed.connect(self.escapeRequested)
        self.retranslate_ui()

    def _emit(self, index: QModelIndex, focus_book: bool) -> None:
        data = index.data(Qt.ItemDataRole.UserRole)
        if isinstance(data, int):
            self.jumpRequested.emit(data, focus_book)

    def set_book(self, book: EpubBook | None) -> None:
        self.model.clear()
        self.entries = []
        self.current = -1
        self.tree.current_row = None
        self.synthetic = bool(book is not None and book.toc_is_synthetic)
        if book is not None:
            def walk(entries: Iterable[Any], parent: QStandardItem, depth: int) -> None:
                for e in entries:
                    title = (e.title or "").strip()
                    item = QStandardItem(title or S("toc.untitled"))
                    item.setEditable(False)
                    idx = len(self.entries)
                    item.setData(idx, Qt.ItemDataRole.UserRole)
                    if title:
                        item.setToolTip(title)
                    spine = book.spine_index(e.zip_name) if e.zip_name else None
                    self.entries.append({"item": item, "title": title, "zip": e.zip_name,
                                         "fragment": e.fragment or "", "spine": spine,
                                         "depth": depth})
                    parent.appendRow(item)
                    walk(e.children, item, depth + 1)
            walk(book.toc, self.model.invisibleRootItem(), 0)
            self.tree.expandToDepth(0)
        self.note.setVisible(self.synthetic)
        self.empty.setVisible(book is not None and not self.entries)
        self.tree.setVisible(bool(self.entries))

    def set_current(self, i: int | None) -> None:
        i = -1 if i is None else i
        if i == self.current:
            return
        if 0 <= self.current < len(self.entries):
            self.entries[self.current]["item"].setFont(QFont(self.tree.font()))
        self.current = i
        if 0 <= i < len(self.entries):
            item = self.entries[i]["item"]
            f = QFont(self.tree.font())
            f.setBold(True)
            item.setFont(f)
            index = item.index()
            self.tree.current_row = index
            parent = index.parent()
            while parent.isValid():
                self.tree.expand(parent)
                parent = parent.parent()
            if time.monotonic() - self.tree.last_user_scroll > TOC_SCROLL_GRACE_S:
                self.tree.scrollTo(index, QAbstractItemView.ScrollHint.EnsureVisible)
        else:
            self.tree.current_row = None
        self.tree.viewport().update()

    def title_of(self, i: int) -> str:
        if 0 <= i < len(self.entries):
            return self.entries[i]["title"] or S("toc.untitled")
        return ""

    def retranslate_ui(self) -> None:
        self.note.setText(S("toc.synthetic"))
        self.empty.setText(S("toc.empty"))
        for e in self.entries:
            if not e["title"]:
                e["item"].setText(S("toc.untitled"))


# ==========================================================================
# dock pane: bookmarks
# ==========================================================================

class BookmarksPane(QWidget):
    """Newest first: chapter, 3-line preview, percent and date.

    Enter / double-click jumps; Delete removes (the page offers a 3 s undo).
    """

    jumpRequested = Signal(str, bool)
    removeRequested = Signal(str)

    def __init__(self, parent: QWidget, theme_fn: Callable[[], theme_mod.Theme]) -> None:
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 4, 0, 0)
        v.setSpacing(4)
        self.count = QLabel(self)
        self.count.setObjectName("bookmarks-count")
        self.count.setProperty("erRole", "secondary")
        self.count.setContentsMargins(12, 2, 12, 2)
        v.addWidget(self.count)
        self.list = _make_row_list(self)
        self.list.setObjectName("bookmarks-list")
        self.delegate = RowDelegate(self.list, self._spec, theme_fn)
        self.list.setItemDelegate(self.delegate)
        v.addWidget(self.list, 1)
        self.empty = QLabel(self)
        self.empty.setObjectName("bookmarks-empty")
        self.empty.setProperty("erRole", "secondary")
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setContentsMargins(16, 24, 16, 24)
        v.addWidget(self.empty)
        v.addStretch(0)
        self.list.itemDoubleClicked.connect(lambda it: self._jump(it, True))
        self.list.customContextMenuRequested.connect(self._menu)
        self.list.installEventFilter(self)
        self._marks: list[dict] = []
        self.retranslate_ui()

    def _spec(self, index: QModelIndex) -> dict:
        bm = index.data(_ROLE_DATA) or {}
        pct = _percent_int(bm.get("book_progress") or 0.0)
        return {
            "title": bm.get("chapter_title") or S("toc.untitled"),
            "meta": S("bookmarks.meta", percent=pct, date=format_date(bm.get("created_at"))),
            "text": _collapse(bm.get("text") or (bm.get("locator") or {}).get("snippet") or ""),
            "text_lines": 3,
        }

    def set_bookmarks(self, marks: Sequence[dict]) -> None:
        self._marks = sorted(marks, key=lambda b: str(b.get("created_at") or ""), reverse=True)
        current = self.selected_id()
        self.list.clear()
        for bm in self._marks:
            it = QListWidgetItem()
            it.setData(_ROLE_DATA, bm)
            it.setData(Qt.ItemDataRole.UserRole, bm.get("id"))
            self.list.addItem(it)
            if bm.get("id") == current:
                self.list.setCurrentItem(it)
        self._refresh_counts()

    def selected_id(self) -> str | None:
        it = self.list.currentItem()
        return it.data(Qt.ItemDataRole.UserRole) if it is not None else None

    def _jump(self, it: QListWidgetItem | None, focus_book: bool) -> None:
        if it is not None:
            self.jumpRequested.emit(str(it.data(Qt.ItemDataRole.UserRole)), focus_book)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        if obj is self.list and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._jump(self.list.currentItem(), False)
                return True
            if key == Qt.Key.Key_Delete:
                bid = self.selected_id()
                if bid:
                    self.removeRequested.emit(str(bid))
                return True
        return super().eventFilter(obj, event)

    def _menu(self, pos: QPoint) -> None:
        it = self.list.itemAt(pos)
        if it is None:
            return
        self.list.setCurrentItem(it)
        menu = QMenu(self)
        menu.addAction(S("bookmarks.go"), lambda: self._jump(it, True))
        menu.addAction(S("bookmarks.remove"),
                       lambda: self.removeRequested.emit(str(it.data(Qt.ItemDataRole.UserRole))))
        menu.exec(self.list.viewport().mapToGlobal(pos))

    def _refresh_counts(self) -> None:
        n = len(self._marks)
        self.count.setText(plural("bookmarks.count", n) if n else "")
        self.count.setVisible(n > 0)
        self.empty.setVisible(n == 0)
        self.list.setVisible(n > 0)

    def retranslate_ui(self) -> None:
        self.empty.setText(S("bookmarks.empty"))
        self._refresh_counts()
        self.list.doItemsLayout()
        self.list.viewport().update()


# ==========================================================================
# dock pane: highlights (批注)
# ==========================================================================

class NotesPane(QWidget):
    """Grouped by chapter, coloured bar per highlight, quote + note underneath.

    Filter chips: All, the four colours, With notes.  Rows whose anchor is lost
    are greyed with a badge and cannot jump, but they stay and are exported.
    """

    jumpRequested = Signal(str, bool)
    removeRequested = Signal(str)
    editRequested = Signal(str)
    colorRequested = Signal(str, str)
    copyRequested = Signal(str)
    exportRequested = Signal()

    def __init__(self, parent: QWidget, theme_fn: Callable[[], theme_mod.Theme]) -> None:
        super().__init__(parent)
        self._theme_fn = theme_fn
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 4, 0, 8)
        v.setSpacing(4)
        chips = QHBoxLayout()
        chips.setContentsMargins(10, 0, 10, 0)
        chips.setSpacing(4)
        self.chip_all = QToolButton(self)
        self.chip_all.setObjectName("notes-chip-all")
        self.chip_all.setCheckable(True)
        self.chip_all.setChecked(True)
        chips.addWidget(self.chip_all)
        self.color_chips: dict[str, QToolButton] = {}
        for color in theme_mod.HIGHLIGHT_COLORS:
            b = QToolButton(self)
            b.setObjectName(f"notes-chip-{color}")
            b.setCheckable(True)
            b.setIconSize(QSize(18, 18))
            b.setFixedSize(28, 28)
            b.toggled.connect(self._filters_changed)
            chips.addWidget(b)
            self.color_chips[color] = b
        self.chip_noted = QToolButton(self)
        self.chip_noted.setObjectName("notes-chip-noted")
        self.chip_noted.setCheckable(True)
        self.chip_noted.toggled.connect(self._filters_changed)
        chips.addWidget(self.chip_noted)
        chips.addStretch(1)
        self.chip_all.clicked.connect(self._clear_filters)
        v.addLayout(chips)
        self.count = QLabel(self)
        self.count.setObjectName("notes-count")
        self.count.setProperty("erRole", "secondary")
        self.count.setContentsMargins(12, 0, 12, 0)
        v.addWidget(self.count)
        self.list = _make_row_list(self)
        self.list.setObjectName("notes-list")
        self.delegate = RowDelegate(self.list, self._spec, theme_fn)
        self.list.setItemDelegate(self.delegate)
        v.addWidget(self.list, 1)
        self.empty = QLabel(self)
        self.empty.setObjectName("notes-empty")
        self.empty.setProperty("erRole", "secondary")
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setContentsMargins(16, 24, 16, 24)
        v.addWidget(self.empty)
        v.addStretch(0)
        self.export_btn = QPushButton(self)
        self.export_btn.setObjectName("notes-export")
        self.export_btn.clicked.connect(self.exportRequested)
        row = QHBoxLayout()
        row.setContentsMargins(10, 0, 10, 0)
        row.addWidget(self.export_btn, 1)
        v.addLayout(row)
        self.list.itemClicked.connect(lambda it: self._jump(it, False))
        self.list.itemDoubleClicked.connect(lambda it: self._jump(it, True))
        self.list.customContextMenuRequested.connect(self._menu)
        self.list.installEventFilter(self)
        self._highlights: list[dict] = []
        self._lost: set[str] = set()
        self._group_title: Callable[[dict], str] = lambda h: h.get("chapter_title") or ""
        self._order: Callable[[dict], tuple] = lambda h: (0, 0)
        self.apply_theme(theme_fn())
        self.retranslate_ui()

    # -- data ---------------------------------------------------------------
    def set_highlights(self, highlights: Sequence[dict], lost: set[str],
                       group_title: Callable[[dict], str], order: Callable[[dict], tuple]) -> None:
        self._highlights = list(highlights)
        self._lost = set(lost)
        self._group_title = group_title
        self._order = order
        self._rebuild()

    def _filter(self) -> tuple[set[str], bool]:
        colors = {c for c, b in self.color_chips.items() if b.isChecked()}
        return colors, self.chip_noted.isChecked()

    def _filters_changed(self, *_: Any) -> None:
        colors, noted = self._filter()
        self.chip_all.setChecked(not colors and not noted)
        self._rebuild()

    def _clear_filters(self) -> None:
        for b in (*self.color_chips.values(), self.chip_noted):
            b.blockSignals(True)
            b.setChecked(False)
            b.blockSignals(False)
        self.chip_all.setChecked(True)
        self._rebuild()

    def set_filter(self, colors: Iterable[str] = (), noted: bool = False) -> None:
        """Programmatic filter (tests, and "show only this colour" affordances)."""
        wanted = set(colors)
        for c, b in self.color_chips.items():
            b.blockSignals(True)
            b.setChecked(c in wanted)
            b.blockSignals(False)
        self.chip_noted.blockSignals(True)
        self.chip_noted.setChecked(noted)
        self.chip_noted.blockSignals(False)
        self._filters_changed()

    def visible_ids(self) -> list[str]:
        out = []
        for i in range(self.list.count()):
            d = self.list.item(i).data(_ROLE_DATA) or {}
            if d.get("kind") == "row":
                out.append(str((d.get("hl") or {}).get("id")))
        return out

    def _rebuild(self) -> None:
        colors, noted = self._filter()
        rows = sorted(self._highlights, key=self._order)
        shown = [h for h in rows
                 if (not colors or (h.get("color") or "yellow") in colors)
                 and (not noted or (h.get("note") or "").strip())]
        current = self.selected_id()
        self.list.clear()
        last_group: object = object()
        for h in shown:
            group = self._order(h)[0]
            if group != last_group:
                last_group = group
                gi = QListWidgetItem()
                gi.setData(_ROLE_DATA, {"kind": "group", "hl": h})
                gi.setFlags(Qt.ItemFlag.NoItemFlags)
                self.list.addItem(gi)
            it = QListWidgetItem()
            lost = h.get("id") in self._lost
            it.setData(_ROLE_DATA, {"kind": "row", "hl": h, "lost": lost})
            it.setData(Qt.ItemDataRole.UserRole, h.get("id"))
            if lost:
                it.setFlags(Qt.ItemFlag.NoItemFlags)
                it.setToolTip(S("notes.lost.tip"))
            self.list.addItem(it)
            if h.get("id") == current:
                self.list.setCurrentItem(it)
        total = len(self._highlights)
        self.count.setText(plural("notes.count", len(shown)) if shown else "")
        self.count.setVisible(bool(shown))
        self.list.setVisible(bool(shown))
        self.empty.setText(S("notes.empty") if total == 0 else S("notes.filtered_empty"))
        self.empty.setVisible(not shown)
        self.export_btn.setEnabled(total > 0)

    def _spec(self, index: QModelIndex) -> dict:
        d = index.data(_ROLE_DATA) or {}
        h = d.get("hl") or {}
        if d.get("kind") == "group":
            return {"kind": "group", "title": self._group_title(h) or S("toc.untitled")}
        t = self._theme_fn()
        note = (h.get("note") or "").strip().replace("\n", " ")
        return {
            "bar": t.ink(h.get("color") or "yellow"),
            "text": _collapse(h.get("text") or ""),
            "text_lines": 4,
            "note": note,
            "note_lines": 3,
            "badge": S("notes.lost") if d.get("lost") else "",
            "muted": bool(d.get("lost")),
        }

    def selected_id(self) -> str | None:
        it = self.list.currentItem()
        return it.data(Qt.ItemDataRole.UserRole) if it is not None else None

    def _jump(self, it: QListWidgetItem | None, focus_book: bool) -> None:
        if it is None:
            return
        d = it.data(_ROLE_DATA) or {}
        if d.get("kind") != "row" or d.get("lost"):
            return
        self.jumpRequested.emit(str(it.data(Qt.ItemDataRole.UserRole)), focus_book)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        if obj is self.list and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._jump(self.list.currentItem(), False)
                return True
            if key == Qt.Key.Key_Delete:
                hid = self.selected_id()
                if hid:
                    self.removeRequested.emit(str(hid))
                return True
        return super().eventFilter(obj, event)

    def _menu(self, pos: QPoint) -> None:
        it = self.list.itemAt(pos)
        if it is None:
            return
        d = it.data(_ROLE_DATA) or {}
        if d.get("kind") != "row":
            return
        hid = str(it.data(Qt.ItemDataRole.UserRole))
        h = d.get("hl") or {}
        menu = QMenu(self)
        if not d.get("lost"):
            menu.addAction(S("notes.go"), lambda: self._jump(it, True))
        menu.addAction(S("notes.copy"), lambda: self.copyRequested.emit(hid))
        menu.addAction(S("notes.edit_note") if (h.get("note") or "").strip() else S("notes.add_note"),
                       lambda: self.editRequested.emit(hid))
        sub = menu.addMenu(S("notes.color"))
        t = self._theme_fn()
        for color in theme_mod.HIGHLIGHT_COLORS:
            act = sub.addAction(swatch_icon(t.highlight(color), t.ink(color), 16),
                                S(theme_mod.HIGHLIGHT_NAME_KEYS[color]))
            act.triggered.connect(lambda _c=False, col=color: self.colorRequested.emit(hid, col))
        menu.addSeparator()
        menu.addAction(S("notes.remove"), lambda: self.removeRequested.emit(hid))
        menu.exec(self.list.viewport().mapToGlobal(pos))

    def apply_theme(self, t: theme_mod.Theme) -> None:
        for color, b in self.color_chips.items():
            b.setIcon(swatch_icon(t.highlight(color), t.ink(color), 18))
        self.list.viewport().update()

    def retranslate_ui(self) -> None:
        self.chip_all.setText(S("notes.filter.all"))
        self.chip_noted.setText(S("notes.filter.noted"))
        for color, b in self.color_chips.items():
            b.setToolTip(S(theme_mod.HIGHLIGHT_NAME_KEYS[color]))
            b.setAccessibleName(S(theme_mod.HIGHLIGHT_NAME_KEYS[color]))
        self.export_btn.setText(S("notes.export"))
        self._rebuild()


# ==========================================================================
# dock pane: search
# ==========================================================================

class SearchPane(QWidget):
    """Query field, ``共 N 处``, result rows ``chapter · …before[match]after…``.

    At most :data:`SEARCH_RENDER_CAP` rows are rendered (with a note); Enter,
    ``n`` and F3 walk every hit.
    """

    searchRequested = Signal(str)
    stepRequested = Signal(int)
    resultActivated = Signal(int, bool)
    escapeRequested = Signal()

    def __init__(self, parent: QWidget, theme_fn: Callable[[], theme_mod.Theme]) -> None:
        super().__init__(parent)
        self._theme_fn = theme_fn
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 6, 10, 6)
        v.setSpacing(6)
        row = QHBoxLayout()
        row.setSpacing(4)
        self.edit = QLineEdit(self)
        self.edit.setObjectName("search-edit")
        self.edit.setClearButtonEnabled(True)
        row.addWidget(self.edit, 1)
        self.btn_prev = QToolButton(self)
        self.btn_prev.setObjectName("search-prev")
        self.btn_prev.setIconSize(QSize(18, 18))
        self.btn_next = QToolButton(self)
        self.btn_next.setObjectName("search-next")
        self.btn_next.setIconSize(QSize(18, 18))
        row.addWidget(self.btn_prev)
        row.addWidget(self.btn_next)
        v.addLayout(row)
        self.status = QLabel(self)
        self.status.setObjectName("search-status")
        self.status.setProperty("erRole", "secondary")
        self.status.setWordWrap(True)
        v.addWidget(self.status)
        self.list = _make_row_list(self)
        self.list.setObjectName("search-list")
        self.delegate = RowDelegate(self.list, self._spec, theme_fn)
        self.list.setItemDelegate(self.delegate)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        v.addWidget(self.list, 1)
        self.capped = QLabel(self)
        self.capped.setObjectName("search-capped")
        self.capped.setProperty("erRole", "secondary")
        self.capped.setWordWrap(True)
        self.capped.hide()
        v.addWidget(self.capped)
        v.addStretch(0)
        self.hits: list[SearchHit] = []
        self.query = ""
        self.active = -1
        self._flash_row = -1
        self._status: tuple[str, dict] = ("", {})
        self._chapter_fn: Callable[[SearchHit], str] = lambda h: h.chapter_title
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(400)
        self._debounce.timeout.connect(self._fire_search)
        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(self._end_flash)
        self.edit.textEdited.connect(lambda _t: self._debounce.start())
        self.edit.returnPressed.connect(self._on_return)
        self.edit.installEventFilter(self)
        self.list.installEventFilter(self)
        self.btn_prev.clicked.connect(lambda: self.stepRequested.emit(-1))
        self.btn_next.clicked.connect(lambda: self.stepRequested.emit(1))
        self.list.itemClicked.connect(lambda it: self._activate(it, False))
        self.list.itemDoubleClicked.connect(lambda it: self._activate(it, True))
        self.list.hide()
        self.apply_theme(theme_fn())
        self.retranslate_ui()

    def _fire_search(self) -> None:
        text = self.edit.text().strip()
        if text != self.query:
            self.searchRequested.emit(text)

    def _on_return(self) -> None:
        self._debounce.stop()
        text = self.edit.text().strip()
        if text != self.query:
            self.searchRequested.emit(text)
            if text:
                self.stepRequested.emit(1)
        elif text:
            self.stepRequested.emit(1)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.KeyPress:
            key = event.key()
            mods = event.modifiers()
            if obj is self.edit:
                if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) \
                        and mods & Qt.KeyboardModifier.ShiftModifier:
                    self.stepRequested.emit(-1)
                    return True
                if key == Qt.Key.Key_Escape:
                    self.escapeRequested.emit()
                    return True
                if key == Qt.Key.Key_Down and self.list.count():
                    self.list.setFocus()
                    if self.list.currentRow() < 0:
                        self.list.setCurrentRow(0)
                    return True
            elif obj is self.list:
                if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    self._activate(self.list.currentItem(), False)
                    return True
                if key == Qt.Key.Key_Escape:
                    self.escapeRequested.emit()
                    return True
        return super().eventFilter(obj, event)

    def _activate(self, it: QListWidgetItem | None, focus_book: bool) -> None:
        if it is not None:
            self.resultActivated.emit(int(it.data(Qt.ItemDataRole.UserRole)), focus_book)

    # -- results ------------------------------------------------------------
    def set_running(self) -> None:
        self._status = ("running", {})
        self._render_status()

    def set_results(self, query: str, hits: Sequence[SearchHit],
                    chapter_fn: Callable[[SearchHit], str]) -> None:
        self.query = query
        self.hits = list(hits)
        self.active = -1
        self._chapter_fn = chapter_fn
        if self.edit.text().strip() != query:
            self.edit.setText(query)
        self.list.clear()
        for i, _hit in enumerate(self.hits[:SEARCH_RENDER_CAP]):
            it = QListWidgetItem()
            it.setData(Qt.ItemDataRole.UserRole, i)
            self.list.addItem(it)
        if not query:
            self._status = ("", {})
        elif not self.hits:
            self._status = ("none", {"q": query})
        else:
            self._status = ("count", {"n": len(self.hits)})
        self.list.setVisible(bool(self.hits))
        self.capped.setVisible(len(self.hits) > SEARCH_RENDER_CAP)
        self.retranslate_ui()

    def set_active(self, i: int) -> None:
        self.active = i
        if 0 <= i < len(self.hits):
            self._status = ("position", {"i": i + 1, "n": len(self.hits)})
            if i < self.list.count():
                it = self.list.item(i)
                self.list.setCurrentItem(it)
                self.list.scrollToItem(it, QAbstractItemView.ScrollHint.EnsureVisible)
                self._flash_row = i
                self._flash_timer.start(650)
                self.list.viewport().update()
        self._render_status()

    def _end_flash(self) -> None:
        self._flash_row = -1
        self.list.viewport().update()

    def _spec(self, index: QModelIndex) -> dict:
        i = int(index.data(Qt.ItemDataRole.UserRole) or 0)
        if not (0 <= i < len(self.hits)):
            return {"text": ""}
        hit = self.hits[i]
        sentinel = ""
        framed = S("search.context", before=hit.before, match=sentinel, after=hit.after)
        at = framed.find(sentinel)
        text = framed.replace(sentinel, hit.match)
        return {
            "title": self._chapter_fn(hit),
            "text": text,
            "text_lines": 3,
            "marks": [(at, len(hit.match))] if at >= 0 else [],
            "flash": i == self._flash_row,
        }

    def _render_status(self) -> None:
        kind, args = self._status
        if kind == "running":
            text = S("search.running")
        elif kind == "none":
            text = S("search.none", q=args.get("q", ""))
        elif kind == "count":
            text = plural("search.count", args["n"])
        elif kind == "position":
            text = S("search.position", i=args["i"], n=args["n"])
        else:
            text = ""
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def apply_theme(self, t: theme_mod.Theme) -> None:
        self.btn_prev.setIcon(make_icon("up", t.chrome_fg, 18))
        self.btn_next.setIcon(make_icon("down", t.chrome_fg, 18))
        self.list.viewport().update()

    def retranslate_ui(self) -> None:
        self.edit.setPlaceholderText(S("search.placeholder"))
        self.edit.setAccessibleName(S("search.placeholder"))
        self.btn_prev.setToolTip(tip("search.prev", "find_prev"))
        self.btn_next.setToolTip(tip("search.next", "find_next"))
        self.capped.setText(S("search.capped", n=SEARCH_RENDER_CAP))
        self._render_status()
        self.list.doItemsLayout()
        self.list.viewport().update()


# ==========================================================================
# the left dock: one segmented control over four panes
# ==========================================================================

class LeftDock(QFrame):
    """ONE dock, four panes (目录 | 书签 | 批注 | 搜索), never four docks."""

    paneChanged = Signal(str)

    def __init__(self, parent: QWidget, theme_fn: Callable[[], theme_mod.Theme]) -> None:
        super().__init__(parent)
        self.setObjectName("er-dock")
        self.setProperty("erRole", "panel")
        self.setMinimumWidth(DOCK_MIN_WIDTH)
        self.setMaximumWidth(DOCK_MAX_WIDTH)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 8, 0, 0)
        v.setSpacing(6)
        self.segment = SegmentedControl(
            [(k, f"dock.{k}") for k in PANES], self,
            tooltips={
                "toc": lambda: tip("dock.toc", "toc"),
                "bookmarks": lambda: tip("dock.bookmarks", "bookmarks"),
                "notes": lambda: tip("dock.notes", "notes"),
                "search": lambda: tip("dock.search", "search"),
            })
        self.segment.setObjectName("dock-segment")
        seg_row = QHBoxLayout()
        seg_row.setContentsMargins(8, 0, 8, 0)
        seg_row.addWidget(self.segment)
        v.addLayout(seg_row)
        self.stack = QStackedWidget(self)
        self.toc = TocPane(self.stack, theme_fn)
        self.bookmarks = BookmarksPane(self.stack, theme_fn)
        self.notes = NotesPane(self.stack, theme_fn)
        self.search = SearchPane(self.stack, theme_fn)
        self.panes: dict[str, QWidget] = {"toc": self.toc, "bookmarks": self.bookmarks,
                                          "notes": self.notes, "search": self.search}
        for key in PANES:
            self.stack.addWidget(self.panes[key])
        v.addWidget(self.stack, 1)
        self.segment.changed.connect(self._on_segment)
        self.set_pane("toc")

    def _on_segment(self, key: str) -> None:
        self.set_pane(key)
        self.paneChanged.emit(key)

    def current_pane(self) -> str:
        return self.segment.current() or "toc"

    def set_pane(self, key: str) -> None:
        if key not in self.panes:
            key = "toc"
        self.segment.set_current(key)
        self.stack.setCurrentWidget(self.panes[key])

    def apply_theme(self, t: theme_mod.Theme) -> None:
        self.notes.apply_theme(t)
        self.search.apply_theme(t)
        self.bookmarks.list.viewport().update()
        self.toc.tree.viewport().update()

    def retranslate_ui(self) -> None:
        self.segment.retranslate_ui()
        for pane in self.panes.values():
            pane.retranslate_ui()


# ==========================================================================
# settings panel (320 px, live apply, no OK/Cancel/Apply)
# ==========================================================================

class _SliderSpin(QWidget):
    """A slider with a numeric spin box; one ``valueChanged(float)`` for both."""

    valueChanged = Signal(float)

    def __init__(self, lo: float, hi: float, step: float, decimals: int,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scale = 10 ** decimals
        self._decimals = decimals
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        self.slider = QSlider(Qt.Orientation.Horizontal, self)
        self.slider.setRange(int(round(lo * self._scale)), int(round(hi * self._scale)))
        self.slider.setSingleStep(max(1, int(round(step * self._scale))))
        self.slider.setPageStep(max(1, int(round(step * self._scale)) * 4))
        if decimals:
            self.spin: QSpinBox | QDoubleSpinBox = QDoubleSpinBox(self)
            self.spin.setDecimals(decimals)
            self.spin.setSingleStep(step)
        else:
            self.spin = QSpinBox(self)
            self.spin.setSingleStep(int(step))
        self.spin.setRange(lo, hi) if decimals else self.spin.setRange(int(lo), int(hi))
        self.spin.setMinimumWidth(78)
        self.spin.setKeyboardTracking(False)
        h.addWidget(self.slider, 1)
        h.addWidget(self.spin)
        self.slider.valueChanged.connect(self._from_slider)
        self.spin.valueChanged.connect(self._from_spin)

    def value(self) -> float:
        return float(self.spin.value())

    def set_value(self, v: float) -> None:
        for w in (self.slider, self.spin):
            w.blockSignals(True)
        self.slider.setValue(int(round(float(v) * self._scale)))
        self.spin.setValue(float(v) if self._decimals else int(round(float(v))))
        for w in (self.slider, self.spin):
            w.blockSignals(False)

    def set_suffix(self, text: str) -> None:
        self.spin.setSuffix(text)

    def _from_slider(self, raw: int) -> None:
        v = raw / self._scale
        self.spin.blockSignals(True)
        self.spin.setValue(v if self._decimals else int(v))
        self.spin.blockSignals(False)
        self.valueChanged.emit(round(v, self._decimals) if self._decimals else float(int(v)))

    def _from_spin(self, v: float) -> None:
        self.slider.blockSignals(True)
        self.slider.setValue(int(round(float(v) * self._scale)))
        self.slider.blockSignals(False)
        self.valueChanged.emit(round(float(v), self._decimals) if self._decimals else float(int(v)))


class SettingsPanel(QFrame):
    """Theme, fonts, layout, page turning and general options; every change is live.

    Emits ``changed(key, value)`` with dotted keys (``reader.font_size_px``,
    ``reader.theme``, ``ui.language``, ``behavior.autohide``...) plus the two
    footer actions ``thisbook`` (bool) and ``reset``.
    """

    changed = Signal(str, object)
    closeRequested = Signal()
    associateRequested = Signal()

    def __init__(self, parent: QWidget, theme_fn: Callable[[], theme_mod.Theme]) -> None:
        super().__init__(parent)
        self._theme_fn = theme_fn
        self.setObjectName("er-settings")
        self.setProperty("erRole", "panel")
        self.setFixedWidth(SETTINGS_WIDTH)
        self._labels: list[tuple[QLabel, Callable[[], str]]] = []
        self._loading = False
        self._values: dict[str, Any] = {}
        self._speed = SPEED_SEED

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        head = QHBoxLayout()
        head.setContentsMargins(16, 10, 8, 6)
        self.title = self._label(lambda: S("set.title"), bold=True, size=15)
        self.title.setObjectName("settings-title")
        head.addWidget(self.title, 1)
        self.btn_close = QToolButton(self)
        self.btn_close.setObjectName("settings-close")
        self.btn_close.setAutoRaise(True)
        self.btn_close.setIconSize(QSize(18, 18))
        self.btn_close.clicked.connect(self.closeRequested)
        head.addWidget(self.btn_close)
        outer.addLayout(head)

        self.scroll = QScrollArea(self)
        self.scroll.setObjectName("settings-scroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget(self.scroll)
        body.setObjectName("settings-body")
        self.scroll.setWidget(body)
        self.scroll.viewport().setAutoFillBackground(False)
        body.setAutoFillBackground(False)
        outer.addWidget(self.scroll, 1)
        v = QVBoxLayout(body)
        v.setContentsMargins(16, 4, 16, 16)
        v.setSpacing(6)

        # ---- theme ----
        v.addWidget(self._section("set.section.theme"))
        self.theme = SegmentedControl([(c, theme_mod.THEME_NAME_KEYS[c]) for c in store_mod.THEME_CHOICES],
                                      body)
        self.theme.setObjectName("settings-theme")
        self.theme.changed.connect(lambda k: self._emit("reader.theme", k))
        v.addWidget(self.theme)

        # ---- font ----
        v.addWidget(self._section("set.section.font"))
        v.addWidget(self._label(lambda: S("set.font.cjk"), secondary=True))
        self.font_cjk = QComboBox(body)
        self.font_cjk.setObjectName("settings-font-cjk")
        self.font_cjk.activated.connect(lambda i: self._emit("reader.font_cjk", self.font_cjk.itemData(i)))
        v.addWidget(self.font_cjk)
        v.addWidget(self._label(lambda: S("set.font.latin"), secondary=True))
        self.font_latin = QComboBox(body)
        self.font_latin.setObjectName("settings-font-latin")
        self.font_latin.activated.connect(
            lambda i: self._emit("reader.font_latin", self.font_latin.itemData(i)))
        v.addWidget(self.font_latin)
        self.book_fonts = QCheckBox(body)
        self.book_fonts.setObjectName("settings-book-fonts")
        self.book_fonts.toggled.connect(lambda on: self._emit("reader.use_book_fonts", bool(on)))
        v.addWidget(self.book_fonts)
        self.size_label = self._label(lambda: S("set.font.size"), secondary=True)
        v.addWidget(self.size_label)
        self.font_size = _SliderSpin(FONT_SIZE_MIN, FONT_SIZE_MAX, 1, 0, body)
        self.font_size.setObjectName("settings-font-size")
        self.font_size.valueChanged.connect(lambda x: self._emit("reader.font_size_px", int(x)))
        v.addWidget(self.font_size)
        v.addWidget(self._label(lambda: S("set.font.weight"), secondary=True))
        self.weight = SegmentedControl([("normal", "set.weight.normal"), ("light", "set.weight.light")], body)
        self.weight.setObjectName("settings-weight")
        self.weight.changed.connect(lambda k: self._emit("reader.font_weight", 300 if k == "light" else 400))
        v.addWidget(self.weight)

        # ---- layout ----
        v.addWidget(self._section("set.section.layout"))
        v.addWidget(self._label(lambda: S("set.line_height"), secondary=True))
        self.line_height = _SliderSpin(LINE_HEIGHT_MIN, LINE_HEIGHT_MAX, 0.1, 1, body)
        self.line_height.setObjectName("settings-line-height")
        self.line_height.valueChanged.connect(lambda x: self._emit("reader.line_height", round(x, 1)))
        v.addWidget(self.line_height)
        v.addWidget(self._label(lambda: S("set.para_spacing"), secondary=True))
        self.para = _SliderSpin(PARA_SPACING_MIN, PARA_SPACING_MAX, 0.1, 1, body)
        self.para.setObjectName("settings-para")
        self.para.valueChanged.connect(lambda x: self._emit("reader.para_spacing_em", round(x, 1)))
        v.addWidget(self.para)
        v.addWidget(self._label(lambda: S("set.indent"), secondary=True))
        self.indent = SegmentedControl([("none", "set.indent.none"), ("two", "set.indent.two")], body)
        self.indent.setObjectName("settings-indent")
        self.indent.changed.connect(lambda k: self._emit("reader.text_indent_ch", 2 if k == "two" else 0))
        v.addWidget(self.indent)
        v.addWidget(self._label(lambda: S("set.align"), secondary=True))
        self.align = SegmentedControl([("left", "set.align.left"), ("justify", "set.align.justify")], body)
        self.align.setObjectName("settings-align")
        self.align.changed.connect(lambda k: self._emit("reader.text_align", k))
        v.addWidget(self.align)
        v.addWidget(self._label(lambda: S("set.margin"), secondary=True))
        self.margin = _SliderSpin(MARGIN_MIN, MARGIN_MAX, 4, 0, body)
        self.margin.setObjectName("settings-margin")
        self.margin.valueChanged.connect(lambda x: self._emit("reader.page_margin_px", int(x)))
        v.addWidget(self.margin)
        v.addWidget(self._label(lambda: S("set.measure"), secondary=True))
        self.measure = _SliderSpin(MEASURE_MIN, MEASURE_MAX, 1, 0, body)
        self.measure.setObjectName("settings-measure")
        self.measure.valueChanged.connect(lambda x: self._emit("reader.max_measure_ch", int(x)))
        v.addWidget(self.measure)

        # ---- page turning ----
        v.addWidget(self._section("set.section.paging"))
        self.paging = SegmentedControl([("paged", "set.paging.paged"), ("scroll", "set.paging.scroll")], body)
        self.paging.setObjectName("settings-paging")
        self.paging.changed.connect(lambda k: self._emit("reader.layout", k))
        v.addWidget(self.paging)

        # ---- general ----
        v.addWidget(self._section("set.section.general"))
        v.addWidget(self._label(lambda: S("set.language"), secondary=True))
        self.language = QComboBox(body)
        self.language.setObjectName("settings-language")
        self.language.activated.connect(lambda i: self._emit("ui.language", self.language.itemData(i)))
        v.addWidget(self.language)
        self.language_hint = self._label(lambda: S("set.language.hint"), secondary=True, small=True)
        v.addWidget(self.language_hint)
        self.autohide = QCheckBox(body)
        self.autohide.setObjectName("settings-autohide")
        self.autohide.toggled.connect(lambda on: self._emit("behavior.autohide", bool(on)))
        v.addWidget(self.autohide)
        self.zoom = QCheckBox(body)
        self.zoom.setObjectName("settings-zoom")
        self.zoom.toggled.connect(lambda on: self._emit("reader.image_click_zoom", bool(on)))
        v.addWidget(self.zoom)
        self.invert = QCheckBox(body)
        self.invert.setObjectName("settings-invert")
        self.invert.toggled.connect(lambda on: self._emit("reader.invert_images_in_dark", bool(on)))
        v.addWidget(self.invert)
        self.restore_last = QCheckBox(body)
        self.restore_last.setObjectName("settings-restore-last")
        self.restore_last.toggled.connect(
            lambda on: self._emit("behavior.restore_last_book_on_launch", bool(on)))
        v.addWidget(self.restore_last)
        self.confirm_remove = QCheckBox(body)
        self.confirm_remove.setObjectName("settings-confirm-remove")
        self.confirm_remove.toggled.connect(
            lambda on: self._emit("behavior.confirm_remove_from_shelf", bool(on)))
        v.addWidget(self.confirm_remove)
        v.addSpacing(4)
        v.addWidget(self._label(lambda: S("set.speed"), secondary=True))
        self.speed_value = self._label(lambda: S("set.speed.value", n=int(round(self._speed))))
        self.speed_value.setObjectName("settings-speed")
        v.addWidget(self.speed_value)
        v.addWidget(self._label(lambda: S("set.speed.hint"), secondary=True, small=True))
        v.addSpacing(6)
        self.assoc = QPushButton(body)
        self.assoc.setObjectName("settings-assoc")
        self.assoc.clicked.connect(self.associateRequested)
        v.addWidget(self.assoc)
        v.addWidget(self._label(lambda: S("set.assoc.hint"), secondary=True, small=True))
        v.addStretch(1)

        # ---- footer: this book only · restore defaults ----
        foot = QFrame(self)
        foot.setObjectName("settings-footer")
        fl = QHBoxLayout(foot)
        fl.setContentsMargins(16, 8, 16, 10)
        fl.setSpacing(8)
        self.this_book = QCheckBox(foot)
        self.this_book.setObjectName("settings-this-book")
        self.this_book.toggled.connect(lambda on: self._emit("thisbook", bool(on)))
        fl.addWidget(self.this_book, 1)
        self.reset = QPushButton(foot)
        self.reset.setObjectName("settings-reset")
        self.reset.clicked.connect(lambda: self._emit("reset", True))
        fl.addWidget(self.reset)
        outer.addWidget(foot)
        self.apply_theme(theme_fn())
        self.retranslate_ui()

    # -- construction helpers ------------------------------------------------
    def _label(self, render: Callable[[], str], *, bold: bool = False, secondary: bool = False,
               small: bool = False, size: int = 0) -> QLabel:
        lab = QLabel(self)
        lab.setWordWrap(True)
        if secondary:
            lab.setProperty("erRole", "secondary")
        f = QFont(lab.font())
        if bold:
            f.setBold(True)
        if size:
            f.setPixelSize(size)
        elif small and f.pointSizeF() > 0:
            f.setPointSizeF(max(7.0, f.pointSizeF() * 0.9))
        lab.setFont(f)
        self._labels.append((lab, render))
        return lab

    def _section(self, key: str) -> QLabel:
        lab = self._label(lambda k=key: S(k), bold=True, size=13)
        lab.setContentsMargins(0, 12, 0, 2)
        return lab

    def _emit(self, key: str, value: Any) -> None:
        if not self._loading:
            self.changed.emit(key, value)

    # -- state ----------------------------------------------------------------
    def load(self, values: dict, *, theme_choice: str, this_book: bool, has_book: bool,
             speed: float, autohide: bool, restore_last: bool, confirm_remove: bool) -> None:
        """Show *values* (merged reader settings) without emitting anything."""
        self._loading = True
        try:
            self._values = dict(values)
            self._speed = float(speed or SPEED_SEED)
            self.theme.set_current(store_mod.normalize_theme_choice(theme_choice))
            self._fill_fonts()
            self.book_fonts.setChecked(bool(values.get("use_book_fonts")))
            self.font_size.set_value(_clamp(float(values.get("font_size_px") or 21),
                                            FONT_SIZE_MIN, FONT_SIZE_MAX))
            weight = values.get("font_weight") or 400
            self.weight.set_current("light" if 0 < int(weight) < 400 else "normal")
            self.line_height.set_value(_clamp(float(values.get("line_height") or 1.9),
                                              LINE_HEIGHT_MIN, LINE_HEIGHT_MAX))
            para = values.get("para_spacing_em")
            self.para.set_value(_clamp(float(0.6 if para is None else para),
                                       PARA_SPACING_MIN, PARA_SPACING_MAX))
            indent = values.get("text_indent_ch") or 0
            self.indent.set_current("two" if float(indent) >= 1 else "none")
            self.align.set_current("justify" if values.get("text_align") == "justify" else "left")
            self.margin.set_value(_clamp(float(values.get("page_margin_px") or 64), MARGIN_MIN, MARGIN_MAX))
            measure = values.get("max_measure_ch") or 40
            self.measure.set_value(_clamp(float(measure), MEASURE_MIN, MEASURE_MAX))
            self.paging.set_current("scroll" if values.get("layout") in ("scroll", "scrolled") else "paged")
            self.zoom.setChecked(bool(values.get("image_click_zoom", True)))
            self.invert.setChecked(bool(values.get("invert_images_in_dark", False)))
            self.autohide.setChecked(bool(autohide))
            self.restore_last.setChecked(bool(restore_last))
            self.confirm_remove.setChecked(bool(confirm_remove))
            self.this_book.setChecked(bool(this_book))
            self.this_book.setEnabled(bool(has_book))
            self._fill_language()
        finally:
            self._loading = False
        self._render_labels()

    def set_theme_choice(self, choice: str) -> None:
        self.theme.set_current(store_mod.normalize_theme_choice(choice))

    def set_speed(self, speed: float) -> None:
        self._speed = float(speed or SPEED_SEED)
        self._render_labels()

    def _fill_fonts(self) -> None:
        cjk, latin = available_fonts()
        for combo, families, key, default in (
            (self.font_cjk, cjk, "font_cjk", "Microsoft YaHei"),
            (self.font_latin, latin, "font_latin", "Georgia"),
        ):
            current = self._values.get(key) or default
            names = list(families)
            if current and current not in names:
                names.insert(0, current)
            combo.blockSignals(True)
            combo.clear()
            for fam in names:
                combo.addItem(strings.font_label(fam), fam)
                combo.setItemData(combo.count() - 1, QFont(fam), Qt.ItemDataRole.FontRole)
            i = combo.findData(current)
            combo.setCurrentIndex(max(0, i))
            combo.blockSignals(False)

    def _fill_language(self) -> None:
        self.language.blockSignals(True)
        self.language.clear()
        for value, label in strings.language_choices():
            self.language.addItem(label, value)
        i = self.language.findData(strings.language_preference())
        self.language.setCurrentIndex(max(0, i))
        self.language.blockSignals(False)

    def _render_labels(self) -> None:
        for lab, render in self._labels:
            lab.setText(render())

    # -- language / theme -----------------------------------------------------
    def retranslate_ui(self) -> None:
        was = self._loading
        self._loading = True
        try:
            self._render_labels()
            for seg in (self.theme, self.weight, self.indent, self.align, self.paging):
                seg.retranslate_ui()
            self.btn_close.setToolTip(S("set.close"))
            self.btn_close.setAccessibleName(S("set.close"))
            self.book_fonts.setText(S("set.font.book"))
            self.font_size.set_suffix(S("set.suffix.px"))
            self.para.set_suffix(S("set.suffix.em"))
            self.margin.set_suffix(S("set.suffix.px"))
            self.measure.set_suffix(S("set.suffix.chars"))
            self.autohide.setText(S("set.autohide"))
            self.zoom.setText(S("set.zoom_images"))
            self.invert.setText(S("set.invert_images"))
            self.restore_last.setText(S("set.restore_last"))
            self.confirm_remove.setText(S("set.confirm_remove"))
            self.assoc.setText(S("set.assoc"))
            self.this_book.setText(S("set.thisbook"))
            self.this_book.setToolTip(S("set.thisbook.tip"))
            self.reset.setText(S("set.reset"))
            self._fill_fonts()
            self._fill_language()
        finally:
            self._loading = was

    def apply_theme(self, t: theme_mod.Theme) -> None:
        self.btn_close.setIcon(make_icon("close", t.chrome_fg, 18))


# ==========================================================================
# in-pane error card (never a modal QMessageBox)
# ==========================================================================

class ErrorCard(QFrame):
    """Title 20 px semibold, body 14 px secondary, the path on its own selectable
    line, a collapsed 技术细节 disclosure, and the action buttons."""

    action = Signal(str)

    #: kind -> (title key | None, body key, buttons, shows details)
    KINDS: dict[str, tuple[str | None, str, tuple[str, ...], bool]] = {
        "corrupt": ("err.corrupt.title", "err.corrupt.body", ("reveal", "remove", "close"), True),
        "unexpected": ("err.corrupt.title", "err.unexpected.body", ("reveal", "remove", "close"), True),
        "drm": ("err.drm.title", "err.drm.body", ("reveal", "close"), False),
        "missing": ("err.missing.title", "err.missing.body", ("relocate", "remove", "close"), False),
        "searching": (None, "err.missing.searching", (), False),
        "structure": ("err.structure.title", "err.structure.body", ("reveal", "remove", "close"), True),
        "structure_opf": ("err.structure.title", "err.structure.body.opf",
                          ("reveal", "remove", "close"), True),
        "toolarge": ("err.toolarge.title", "err.toolarge.body", ("reveal", "remove", "close"), True),
        "access": ("err.access.title", "err.access.body", ("reveal", "remove", "close"), True),
        "unsupported": ("err.unsupported.title", "err.unsupported.body",
                        ("reveal", "remove", "close"), True),
    }
    BUTTON_KEYS = {"reveal": "err.btn.reveal", "relocate": "err.btn.relocate",
                   "remove": "err.btn.remove", "close": "err.btn.close"}

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("er-error-card")
        self.setProperty("erRole", "card")
        self.setMaximumWidth(600)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        v = QVBoxLayout(self)
        v.setContentsMargins(28, 24, 28, 22)
        v.setSpacing(10)
        self.title = QLabel(self)
        self.title.setObjectName("error-title")
        self.title.setProperty("erRole", "title")
        self.title.setWordWrap(True)
        v.addWidget(self.title)
        self.body = QLabel(self)
        self.body.setObjectName("error-body")
        self.body.setProperty("erRole", "body")
        self.body.setWordWrap(True)
        v.addWidget(self.body)
        self.path = _book_data(QLabel(self))
        self.path.setObjectName("error-path")
        self.path.setWordWrap(True)
        self.path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.path.setTextFormat(Qt.TextFormat.PlainText)
        v.addWidget(self.path)
        drow = QHBoxLayout()
        drow.setSpacing(6)
        self.details_btn = QToolButton(self)
        self.details_btn.setObjectName("error-details-toggle")
        self.details_btn.setCheckable(True)
        self.details_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.details_btn.setAutoRaise(True)
        self.details_btn.toggled.connect(self._toggle_details)
        drow.addWidget(self.details_btn)
        drow.addStretch(1)
        self.copy_btn = QToolButton(self)
        self.copy_btn.setObjectName("error-details-copy")
        self.copy_btn.setAutoRaise(True)
        self.copy_btn.clicked.connect(self._copy_details)
        self.copy_btn.hide()
        drow.addWidget(self.copy_btn)
        v.addLayout(drow)
        self.details = _book_data(QPlainTextEdit(self))
        self.details.setObjectName("error-details")
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(140)
        self.details.hide()
        v.addWidget(self.details)
        self.confirm = QFrame(self)
        self.confirm.setObjectName("error-confirm")
        cl = QVBoxLayout(self.confirm)
        cl.setContentsMargins(0, 6, 0, 0)
        self.confirm_label = QLabel(self.confirm)
        self.confirm_label.setWordWrap(True)
        cl.addWidget(self.confirm_label)
        self.confirm_path = _book_data(QLabel(self.confirm))
        self.confirm_path.setWordWrap(True)
        self.confirm_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        cl.addWidget(self.confirm_path)
        crow = QHBoxLayout()
        crow.addStretch(1)
        self.confirm_no = QPushButton(self.confirm)
        self.confirm_no.setObjectName("error-link-no")
        self.confirm_no.clicked.connect(lambda: self.action.emit("link_no"))
        self.confirm_yes = QPushButton(self.confirm)
        self.confirm_yes.setObjectName("error-link-yes")
        self.confirm_yes.setProperty("erRole", "primary")
        self.confirm_yes.clicked.connect(lambda: self.action.emit("link_yes"))
        crow.addWidget(self.confirm_no)
        crow.addWidget(self.confirm_yes)
        cl.addLayout(crow)
        self.confirm.hide()
        v.addWidget(self.confirm)
        self.buttons_row = QHBoxLayout()
        self.buttons_row.setSpacing(8)
        self.buttons_row.addStretch(1)
        self.buttons: dict[str, QPushButton] = {}
        for name in ("reveal", "relocate", "remove", "close"):
            b = QPushButton(self)
            b.setObjectName(f"error-btn-{name}")
            b.clicked.connect(lambda _c=False, n=name: self.action.emit(n))
            self.buttons_row.addWidget(b)
            self.buttons[name] = b
        v.addSpacing(6)
        v.addLayout(self.buttons_row)
        self.spec: dict = {}
        self._theme: theme_mod.Theme | None = None

    def show_spec(self, spec: dict) -> None:
        self.spec = dict(spec)
        self.details_btn.setChecked(False)
        self.retranslate_ui()

    def set_confirm(self, new_path: str | None) -> None:
        self.spec["confirm"] = new_path
        self.retranslate_ui()

    def _toggle_details(self, on: bool) -> None:
        self.details.setVisible(on and bool(self.spec.get("details")))
        self.copy_btn.setVisible(on and bool(self.spec.get("details")))
        if self._theme is not None:
            self.details_btn.setIcon(make_icon("chevron_down" if on else "chevron_right",
                                               self._theme.chrome_secondary, 16))

    def _copy_details(self) -> None:
        QGuiApplication.clipboard().setText(self.spec.get("details") or "")

    def retranslate_ui(self) -> None:
        kind = self.spec.get("kind", "corrupt")
        title_key, body_key, buttons, has_details = self.KINDS.get(kind, self.KINDS["corrupt"])
        self.title.setText(S(title_key) if title_key else "")
        self.title.setVisible(bool(title_key))
        if kind == "drm":
            scheme = self.spec.get("drm_scheme")
            if scheme in ("adept", "lcp", "mobipocket"):
                body = S("err.drm.body", kind=S(f"err.drm.{scheme}"))
            else:
                body = S("err.drm.body.unknown")
        elif kind == "missing":
            body = S(body_key, title=self.spec.get("title") or os.path.basename(self.spec.get("path", "")))
        else:
            body = S(body_key)
        self.body.setText(body)
        path = self.spec.get("path") or ""
        self.path.setText(path)
        self.path.setVisible(bool(path) and kind != "searching")
        details = self.spec.get("details") or ""
        self.details.setPlainText(details)
        show_details = has_details and bool(details)
        self.details_btn.setVisible(show_details)
        self.details_btn.setText(S("err.details"))
        self.copy_btn.setText(S("err.details.copy"))
        self._toggle_details(self.details_btn.isChecked() and show_details)
        for name, b in self.buttons.items():
            b.setText(S(self.BUTTON_KEYS[name]))
            visible = name in buttons and (name != "remove" or bool(self.spec.get("in_library")))
            b.setVisible(visible)
        confirm = self.spec.get("confirm")
        self.confirm.setVisible(bool(confirm))
        self.confirm_label.setText(S("err.relocate.mismatch"))
        self.confirm_path.setText(confirm or "")
        self.confirm_yes.setText(S("err.relocate.yes"))
        self.confirm_no.setText(S("err.relocate.no"))

    def apply_theme(self, t: theme_mod.Theme) -> None:
        self._theme = t
        self._toggle_details(self.details_btn.isChecked())


class ErrorPage(QWidget):
    """The reading column's in-pane error state: one card, centred."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("er-error-page")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setProperty("erRole", "page")
        g = QGridLayout(self)
        g.setContentsMargins(24, TOOLBAR_HEIGHT + 24, 24, STATUS_HEIGHT + 24)
        self.card = ErrorCard(self)
        g.addWidget(self.card, 1, 1)
        g.setRowStretch(0, 2)
        g.setRowStretch(2, 3)
        g.setColumnStretch(0, 1)
        g.setColumnStretch(2, 1)

    def resizeEvent(self, event: Any) -> None:  # noqa: N802
        super().resizeEvent(event)
        # a word-wrapped card would otherwise shrink to its narrowest wrap
        self.card.setFixedWidth(int(min(560, max(260, self.width() - 48))))


# ==========================================================================
# popover cards inside the reading column (non-modal)
# ==========================================================================

class Popover(QFrame):
    """Base for the small in-pane cards.  Esc closes."""

    closed = Signal()
    preferred_width = 380

    def __init__(self, parent: QWidget, name: str) -> None:
        super().__init__(parent)
        self.setObjectName(name)
        self.setProperty("erRole", "card")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.hide()
        self.v = QVBoxLayout(self)
        self.v.setContentsMargins(20, 16, 20, 16)
        self.v.setSpacing(8)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.dismiss()
            event.accept()
            return
        super().keyPressEvent(event)

    def dismiss(self) -> None:
        if self.isVisible():
            hide_quietly(self)
            self.closed.emit()

    def present(self) -> None:
        parent = self.parentWidget()
        self.show()
        self.raise_()
        if isinstance(parent, ReadingColumn):
            parent.layout_overlays()

    def retranslate_ui(self) -> None:  # pragma: no cover - overridden
        pass

    def apply_theme(self, t: theme_mod.Theme) -> None:
        pass

    @staticmethod
    def title_label(parent: QWidget) -> QLabel:
        lab = QLabel(parent)
        f = QFont(lab.font())
        f.setBold(True)
        f.setPixelSize(16)
        lab.setFont(f)
        lab.setWordWrap(True)
        return lab


class GotoPopover(Popover):
    """Ctrl+G: jump to a percentage of the whole book."""

    goRequested = Signal(float)

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent, "er-goto")
        self.title = self.title_label(self)
        self.v.addWidget(self.title)
        self.label = QLabel(self)
        self.label.setWordWrap(True)
        self.v.addWidget(self.label)
        self.spin = QDoubleSpinBox(self)
        self.spin.setObjectName("goto-spin")
        self.spin.setRange(0.0, 100.0)
        self.spin.setDecimals(1)
        self.spin.setSingleStep(1.0)
        self.spin.installEventFilter(self)
        self.v.addWidget(self.spin)
        self.hint = QLabel(self)
        self.hint.setProperty("erRole", "secondary")
        self.hint.setWordWrap(True)
        self.v.addWidget(self.hint)
        row = QHBoxLayout()
        row.addStretch(1)
        self.cancel = QPushButton(self)
        self.cancel.clicked.connect(self.dismiss)
        self.go = QPushButton(self)
        self.go.setObjectName("goto-go")
        self.go.setProperty("erRole", "primary")
        self.go.clicked.connect(self._go)
        row.addWidget(self.cancel)
        row.addWidget(self.go)
        self.v.addLayout(row)
        self.retranslate_ui()

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        if obj is self.spin and event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._go()
                return True
            if event.key() == Qt.Key.Key_Escape:
                self.dismiss()
                return True
        return super().eventFilter(obj, event)

    def open(self, percent: float) -> None:
        self.spin.setValue(_clamp(percent, 0.0, 100.0))
        self.present()
        self.spin.setFocus()
        self.spin.selectAll()

    def _go(self) -> None:
        self.spin.interpretText()
        value = float(self.spin.value())
        self.dismiss()
        self.goRequested.emit(value)

    def retranslate_ui(self) -> None:
        self.title.setText(S("goto.title"))
        self.label.setText(S("goto.label"))
        self.hint.setText(S("goto.hint"))
        self.go.setText(S("goto.go"))
        self.cancel.setText(S("common.cancel"))


class NoteEditor(Popover):
    """Edit one highlight: colour, note, delete."""

    saveRequested = Signal(str, str, str)   # id, note, colour
    deleteRequested = Signal(str)

    def __init__(self, parent: QWidget, theme_fn: Callable[[], theme_mod.Theme]) -> None:
        super().__init__(parent, "er-note-editor")
        self._theme_fn = theme_fn
        self.hid = ""
        self.color = "yellow"
        self.title = self.title_label(self)
        self.v.addWidget(self.title)
        self.quote = _book_data(QLabel(self))
        self.quote.setObjectName("note-quote")
        self.quote.setProperty("erRole", "secondary")
        self.quote.setWordWrap(True)
        self.v.addWidget(self.quote)
        crow = QHBoxLayout()
        crow.setSpacing(6)
        self.chips: dict[str, QToolButton] = {}
        group = QButtonGroup(self)
        group.setExclusive(True)
        for color in theme_mod.HIGHLIGHT_COLORS:
            b = QToolButton(self)
            b.setObjectName(f"note-color-{color}")
            b.setCheckable(True)
            b.setAutoRaise(True)
            b.setIconSize(QSize(20, 20))
            b.clicked.connect(lambda _c=False, c=color: self._pick(c))
            group.addButton(b)
            crow.addWidget(b)
            self.chips[color] = b
        crow.addStretch(1)
        self.v.addLayout(crow)
        self.edit = QPlainTextEdit(self)
        self.edit.setObjectName("note-edit")
        self.edit.setMinimumHeight(90)
        self.edit.installEventFilter(self)
        self.v.addWidget(self.edit)
        row = QHBoxLayout()
        self.delete = QPushButton(self)
        self.delete.setObjectName("note-delete")
        self.delete.clicked.connect(lambda: (self.dismiss(), self.deleteRequested.emit(self.hid)))
        row.addWidget(self.delete)
        row.addStretch(1)
        self.cancel = QPushButton(self)
        self.cancel.clicked.connect(self.dismiss)
        self.save = QPushButton(self)
        self.save.setObjectName("note-save")
        self.save.setProperty("erRole", "primary")
        self.save.clicked.connect(self._save)
        row.addWidget(self.cancel)
        row.addWidget(self.save)
        self.v.addLayout(row)
        self.apply_theme(theme_fn())
        self.retranslate_ui()

    preferred_width = 420

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        if obj is self.edit and event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) \
                    and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self._save()
                return True
            if event.key() == Qt.Key.Key_Escape:
                self.dismiss()
                return True
        return super().eventFilter(obj, event)

    def open(self, hl: dict) -> None:
        self.hid = str(hl.get("id") or "")
        self.color = hl.get("color") or "yellow"
        text = _collapse(hl.get("text") or "")
        fm = self.quote.fontMetrics()
        self.quote.setText(fm.elidedText(text, Qt.TextElideMode.ElideRight, 3 * 360))
        self.edit.setPlainText(hl.get("note") or "")
        self._pick(self.color)
        self.present()
        self.edit.setFocus()

    def _pick(self, color: str) -> None:
        self.color = color
        for c, b in self.chips.items():
            b.setChecked(c == color)

    def _save(self) -> None:
        note = self.edit.toPlainText().strip()
        self.dismiss()
        self.saveRequested.emit(self.hid, note, self.color)

    def apply_theme(self, t: theme_mod.Theme) -> None:
        for color, b in self.chips.items():
            b.setIcon(swatch_icon(t.highlight(color), t.ink(color), 20))

    def retranslate_ui(self) -> None:
        self.title.setText(S("note.title"))
        self.edit.setPlaceholderText(S("note.placeholder"))
        self.delete.setText(S("notes.remove"))
        self.cancel.setText(S("common.cancel"))
        self.save.setText(S("common.save"))
        for color, b in self.chips.items():
            b.setToolTip(S(theme_mod.HIGHLIGHT_NAME_KEYS[color]))


class LinkConfirm(Popover):
    """Ask before opening an external link in the system browser."""

    openRequested = Signal(str)

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent, "er-link-confirm")
        self.url = ""
        self.title = self.title_label(self)
        self.v.addWidget(self.title)
        self.url_label = _book_data(QLabel(self))
        self.url_label.setObjectName("link-url")
        self.url_label.setWordWrap(True)
        self.url_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.url_label.setTextFormat(Qt.TextFormat.PlainText)
        self.v.addWidget(self.url_label)
        row = QHBoxLayout()
        row.addStretch(1)
        self.cancel = QPushButton(self)
        self.cancel.setObjectName("link-cancel")
        self.cancel.clicked.connect(self.dismiss)
        self.open_btn = QPushButton(self)
        self.open_btn.setObjectName("link-open")
        self.open_btn.setProperty("erRole", "primary")
        self.open_btn.clicked.connect(self._open)
        row.addWidget(self.cancel)
        row.addWidget(self.open_btn)
        self.v.addLayout(row)
        self.retranslate_ui()

    def ask(self, url: str) -> None:
        self.url = url
        self.url_label.setText(url)
        self.present()
        self.cancel.setFocus()

    def _open(self) -> None:
        url = self.url
        self.dismiss()
        self.openRequested.emit(url)

    def retranslate_ui(self) -> None:
        self.title.setText(S("link.external.title"))
        self.open_btn.setText(S("link.external.open"))
        self.cancel.setText(S("common.cancel"))


class BookInfoCard(Popover):
    """书籍信息: metadata, file, length and reading time."""

    preferred_width = 460

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent, "er-book-info")
        self.title = self.title_label(self)
        self.v.addWidget(self.title)
        self.form_host = QWidget(self)
        self.form = QFormLayout(self.form_host)
        self.form.setContentsMargins(0, 4, 0, 4)
        self.form.setHorizontalSpacing(14)
        self.form.setVerticalSpacing(6)
        self.form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.v.addWidget(self.form_host)
        row = QHBoxLayout()
        row.addStretch(1)
        self.close_btn = QPushButton(self)
        self.close_btn.setObjectName("info-close")
        self.close_btn.clicked.connect(self.dismiss)
        row.addWidget(self.close_btn)
        self.v.addLayout(row)
        self.rows: list[tuple[Callable[[], str], Callable[[], str]]] = []
        self.retranslate_ui()

    def show_rows(self, rows: list[tuple[Callable[[], str], Callable[[], str]]]) -> None:
        self.rows = rows
        self._render()
        self.present()
        self.close_btn.setFocus()

    def _render(self) -> None:
        while self.form.rowCount():
            self.form.removeRow(0)
        for label_fn, value_fn in self.rows:
            value = value_fn()
            if not value:
                continue
            lab = QLabel(label_fn(), self.form_host)
            lab.setProperty("erRole", "secondary")
            val = _book_data(QLabel(value, self.form_host))
            val.setWordWrap(True)
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            val.setTextFormat(Qt.TextFormat.PlainText)
            self.form.addRow(lab, val)

    def retranslate_ui(self) -> None:
        self.title.setText(S("info.title"))
        self.close_btn.setText(S("common.close"))
        if self.rows:
            self._render()


class SectionFailureCard(Popover):
    """Spec (e): one spine document failed.  The rest of the book keeps working."""

    prevRequested = Signal()
    nextRequested = Signal()
    preferred_width = 420

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent, "er-section-failure")
        self.title = self.title_label(self)
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.v.addWidget(self.title)
        row = QHBoxLayout()
        row.addStretch(1)
        self.prev = QPushButton(self)
        self.prev.setObjectName("section-prev")
        self.prev.clicked.connect(self.prevRequested)
        self.next = QPushButton(self)
        self.next.setObjectName("section-next")
        self.next.clicked.connect(self.nextRequested)
        row.addWidget(self.prev)
        row.addWidget(self.next)
        row.addStretch(1)
        self.v.addLayout(row)
        self.retranslate_ui()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        QFrame.keyPressEvent(self, event)   # not dismissable: it IS the chapter

    def retranslate_ui(self) -> None:
        self.title.setText(S("err.section"))
        self.prev.setText(S("keys.prev_chapter"))
        self.next.setText(S("keys.next_chapter"))


# ==========================================================================
# F1 cheat sheet overlay (strings.CHEATSHEET_LAYOUT)
# ==========================================================================

class CheatSheet(QWidget):
    """Full-page overlay listing every shortcut, grouped; Esc, F1 or a click closes it."""

    closed = Signal()
    MAX_WIDTH = 1040

    def __init__(self, parent: QWidget, theme_fn: Callable[[], theme_mod.Theme]) -> None:
        super().__init__(parent)
        self._theme_fn = theme_fn
        self.setObjectName("er-cheatsheet")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.hide()
        self.scroll = QScrollArea(self)
        self.scroll.setObjectName("cheatsheet-scroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea{background:transparent;}")
        self.scroll.viewport().setAutoFillBackground(False)
        self.card = QFrame()
        self.card.setObjectName("cheatsheet-card")
        self.card.setProperty("erRole", "card")
        self.scroll.setWidget(self.card)
        self.rebuild()

    def rebuild(self) -> None:
        old = self.card.layout()
        if old is not None:
            QWidget().setLayout(old)   # hand the old layout to a throwaway widget
        for child in self.card.findChildren(QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly):
            child.hide()
            child.deleteLater()
        v = QVBoxLayout(self.card)
        v.setContentsMargins(28, 22, 28, 20)
        v.setSpacing(14)
        head = QHBoxLayout()
        title = QLabel(S("keys.title"), self.card)
        title.setObjectName("cheatsheet-title")
        f = QFont(title.font())
        f.setBold(True)
        f.setPixelSize(20)
        title.setFont(f)
        head.addWidget(title)
        head.addStretch(1)
        app = QLabel(strings.APP_DISPLAY_NAME, self.card)
        app.setProperty("erRole", "secondary")
        head.addWidget(app)
        v.addLayout(head)
        groups = strings.cheatsheet()
        columns = [[0], [1, 2], [3, 4]]
        grid = QHBoxLayout()
        grid.setSpacing(32)
        bold = QFont(title.font())
        bold.setPixelSize(14)
        for members in columns:
            box = QVBoxLayout()
            box.setSpacing(4)
            for n, gi in enumerate(members):
                if gi >= len(groups):
                    continue
                gtitle, rows = groups[gi]
                gl = QLabel(gtitle, self.card)
                gl.setFont(bold)
                gl.setContentsMargins(0, 12 if n else 0, 0, 2)
                box.addWidget(gl)
                form = QGridLayout()
                form.setHorizontalSpacing(12)
                form.setVerticalSpacing(4)
                for r, (combos, desc) in enumerate(rows):
                    keys = QLabel(S("common.sep").join(combos), self.card)
                    keys.setProperty("erRole", "link")
                    keys.setWordWrap(True)
                    keys.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
                    d = QLabel(desc, self.card)
                    d.setWordWrap(True)
                    d.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
                    form.addWidget(keys, r, 0)
                    form.addWidget(d, r, 1)
                form.setColumnStretch(0, 5)
                form.setColumnStretch(1, 6)
                box.addLayout(form)
            box.addStretch(1)
            grid.addLayout(box, 1)
        v.addLayout(grid)
        foot = QHBoxLayout()
        note = QLabel(S("keys.note.letters"), self.card)
        note.setProperty("erRole", "secondary")
        note.setWordWrap(True)
        foot.addWidget(note, 1)
        close = QLabel(S("keys.close"), self.card)
        close.setProperty("erRole", "secondary")
        foot.addWidget(close)
        v.addLayout(foot)
        if self.isVisible():
            self._place()

    def _place(self) -> None:
        w, h = self.width(), self.height()
        cw = int(min(self.MAX_WIDTH, max(360, w - 48)))
        lay = self.card.layout()
        want = lay.totalHeightForWidth(cw) if lay is not None and lay.hasHeightForWidth()             else self.card.sizeHint().height()
        ch = int(min(max(200, want + 2), max(200, h - 48)))
        self.scroll.setGeometry((w - cw) // 2, max(24, (h - ch) // 2), cw, ch)

    def open(self) -> None:
        parent = self.parentWidget()
        if parent is not None:
            self.setGeometry(parent.rect())
        self.show()
        self.raise_()
        self._place()
        self.setFocus()

    def resizeEvent(self, event: Any) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._place()

    def dismiss(self) -> None:
        if self.isVisible():
            hide_quietly(self)
            self.closed.emit()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_F1) or (
                event.key() == Qt.Key.Key_Slash and event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self.dismiss()
            event.accept()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event: Any) -> None:  # noqa: N802
        if not self.scroll.geometry().contains(event.position().toPoint()):
            self.dismiss()
            return
        super().mousePressEvent(event)

    def paintEvent(self, event: Any) -> None:  # noqa: N802
        p = QPainter(self)
        c = QColor(self._theme_fn().chrome_bg)
        c.setAlpha(232)
        p.fillRect(self.rect(), c)
        p.end()

    def retranslate_ui(self) -> None:
        self.rebuild()


# ==========================================================================
# the reading column: web view + floating chrome + in-pane cards
# ==========================================================================

class ReadingColumn(QWidget):
    """Holds the book view.  The toolbar and status bar float over its top and
    bottom edges (inside the page margins) so hiding them never re-flows the
    book; popovers and the error page are laid out here too."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("er-column")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setProperty("erRole", "page")
        self.setMinimumWidth(240)
        self.view = QWebEngineView(self)
        self.view.setObjectName("er-view")
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.error_page = ErrorPage(self)
        self.error_page.hide()
        self.loading = QLabel(self)
        self.loading.setObjectName("er-loading")
        self.loading.setProperty("erRole", "secondary")
        self.loading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.loading.hide()
        self.toolbar: QWidget | None = None
        self.statusbar: QWidget | None = None
        self.popovers: list[QWidget] = []

    def resizeEvent(self, event: Any) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.layout_overlays()

    def layout_overlays(self) -> None:
        w, h = self.width(), self.height()
        self.view.setGeometry(0, 0, w, h)
        self.error_page.setGeometry(0, 0, w, h)
        self.loading.setGeometry(0, h // 2 - 20, w, 40)
        if self.toolbar is not None:
            self.toolbar.setGeometry(0, 0, w, TOOLBAR_HEIGHT)
            self.toolbar.raise_()
        if self.statusbar is not None:
            self.statusbar.setGeometry(0, h - STATUS_HEIGHT, w, STATUS_HEIGHT)
            self.statusbar.raise_()
        for pop in self.popovers:
            if not pop.isVisible():
                continue
            pw = min(getattr(pop, "preferred_width", 380), max(200, w - 32))
            ph = min(pop.heightForWidth(pw) if pop.hasHeightForWidth() else pop.sizeHint().height(),
                     max(120, h - TOOLBAR_HEIGHT - STATUS_HEIGHT - 32))
            ph = max(ph, pop.minimumSizeHint().height())
            x = (w - pw) // 2
            y = max(TOOLBAR_HEIGHT + 16, (h - ph) // 3)
            pop.setGeometry(x, y, pw, ph)
            pop.raise_()


class NoticeCard(Popover):
    """A sentence followed by a path on its own selectable line, and Close."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent, "er-notice")
        self.text_fn: Callable[[], str] = lambda: ""
        self.body = QLabel(self)
        self.body.setWordWrap(True)
        self.v.addWidget(self.body)
        self.path = _book_data(QLabel(self))
        self.path.setWordWrap(True)
        self.path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.path.setTextFormat(Qt.TextFormat.PlainText)
        self.v.addWidget(self.path)
        row = QHBoxLayout()
        row.addStretch(1)
        self.close_btn = QPushButton(self)
        self.close_btn.clicked.connect(self.dismiss)
        row.addWidget(self.close_btn)
        self.v.addLayout(row)
        self.retranslate_ui()

    def show_notice(self, text_fn: Callable[[], str], path: str) -> None:
        self.text_fn = text_fn
        self.path.setText(path)
        self.retranslate_ui()
        self.present()
        self.close_btn.setFocus()

    def retranslate_ui(self) -> None:
        self.body.setText(self.text_fn())
        self.close_btn.setText(S("common.close"))


class _Relay(QObject):
    """Carries a worker thread's result back to the UI thread (queued signal)."""

    done = Signal(object)


def find_moved_file(old_path: str, size: int, bid: str, folders: Sequence[str]) -> str | None:
    """Spec §3 (c): look for a file with the same size AND content hash.

    Scans each folder (not recursively) for ``*.epub`` files of exactly *size*
    bytes and returns the first whose blake2b-128 equals *bid*.
    """
    seen: set[str] = set()
    old_key = os.path.normcase(os.path.abspath(old_path))
    for folder in folders:
        if not folder:
            continue
        key = os.path.normcase(os.path.abspath(folder))
        if key in seen or not os.path.isdir(folder):
            continue
        seen.add(key)
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.is_file() or not bookformats.is_book_file(entry.name):
                    continue
                if os.path.normcase(os.path.abspath(entry.path)) == old_key:
                    continue
                if entry.stat().st_size != size:
                    continue
                if store_mod.book_id(entry.path) == bid:
                    return entry.path
            except OSError:
                continue
    return None


# JS run in the page (ApplicationWorld) to tell which TOC anchors of the
# current document are on or before the current screen.
_TOC_PROBE_JS = r"""
(function (ids) {
  var W = window.innerWidth || 1, H = window.innerHeight || 1;
  var st = (window.epubReader && typeof window.epubReader.state === 'function')
           ? window.epubReader.state() : null;
  var mode = st ? st.mode : 'paginated', rtl = !!(st && st.rtl);
  var out = {};
  for (var i = 0; i < ids.length; i++) {
    var id = ids[i], el = document.getElementById(id);
    if (!el) { try { el = document.querySelector('[name="' + CSS.escape(id) + '"]'); } catch (e) { el = null; } }
    if (!el) { continue; }
    var r = el.getBoundingClientRect();
    if (!r.width && !r.height) {
      var rg = document.createRange(); rg.selectNodeContents(el);
      var rs = rg.getClientRects(); if (rs.length) { r = rs[0]; }
    }
    out[id] = (mode === 'scroll') ? (r.top < H * 0.4) : (rtl ? (r.right > 0) : (r.left < W));
  }
  return out;
})(%s)
"""


# ==========================================================================
# ReaderPage
# ==========================================================================

class ReaderPage(QWidget):
    """The whole reading surface (CONTRACT §6, product spec §3).

    API in: :meth:`open_book`, :meth:`close_book`, :meth:`apply_theme`,
    :meth:`retranslate_ui`, :meth:`shutdown`, and one public slot per keyboard
    action (see :data:`ACTION_SLOTS`).  Signals out: ``backToLibrary()``,
    ``bookOpened(book_id)``, ``titleChanged(str)`` plus the requests the page
    cannot fulfil itself (open, about, quit, file association).
    """

    backToLibrary = Signal()
    bookOpened = Signal(str)
    titleChanged = Signal(str)
    bookRemoved = Signal(str)
    openBookRequested = Signal()
    aboutRequested = Signal()
    quitRequested = Signal()
    associateRequested = Signal()
    fullscreenChanged = Signal(bool)
    zenChanged = Signal(bool)
    pageKeyUnhandled = Signal(str)
    chapterLoaded = Signal(int)
    progressChanged = Signal(str, float)

    def __init__(self, store: Store, parent: QWidget | None = None, *,
                 host: webhost.BookHost | None = None,
                 theme_controller: theme_mod.ThemeController | None = None) -> None:
        super().__init__(parent)
        self.store = store
        self.setObjectName("er-reader-page")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setProperty("erRole", "page")
        #: Handle j/k/n/N and "/" forwarded by the page (they only arrive while the
        #: book view has focus, i.e. the spec's gating rule).  Owner G may turn this
        #: off and dispatch everything from ``pageKeyUnhandled`` itself.
        self.handle_letter_keys = True

        # ---- theme -------------------------------------------------------------
        self._own_controller = theme_controller is None
        if theme_controller is None:
            theme_controller = theme_mod.ThemeController(
                QApplication.instance(), store.get("reader.theme", "system"), self)
        self.theme_controller = theme_controller
        self.theme: theme_mod.Theme = theme_controller.theme

        # ---- layout ------------------------------------------------------------
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setObjectName("er-splitter")
        self.splitter.setHandleWidth(1)
        self.splitter.setChildrenCollapsible(False)
        self.dock = LeftDock(self.splitter, lambda: self.theme)
        self.column = ReadingColumn(self.splitter)
        self.splitter.addWidget(self.dock)
        self.splitter.addWidget(self.column)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        root.addWidget(self.splitter, 1)
        self.settings_panel = SettingsPanel(self, lambda: self.theme)
        self.settings_panel.hide()
        root.addWidget(self.settings_panel)
        self.toolbar = ChromeToolbar(self, self.column)
        self.statusbar = ChromeStatusBar(self.column)
        self.column.toolbar = self.toolbar
        self.column.statusbar = self.statusbar
        self.section_card = SectionFailureCard(self.column)
        self.goto_popover = GotoPopover(self.column)
        self.note_editor = NoteEditor(self.column, lambda: self.theme)
        self.link_confirm = LinkConfirm(self.column)
        self.book_info = BookInfoCard(self.column)
        self.notice = NoticeCard(self.column)
        self.column.popovers = [self.section_card, self.goto_popover, self.note_editor,
                                self.link_confirm, self.book_info, self.notice]
        self.cheatsheet = CheatSheet(self, lambda: self.theme)
        self.view = self.column.view
        self.error_card = self.column.error_page.card

        # ---- web host ----------------------------------------------------------
        self._own_host = host is None
        self.host = host if host is not None else webhost.BookHost(parent=self)
        self.host.auto_follow_internal_links = False
        self.host.attach(self.view)
        # Chromium would otherwise pull keyboard focus into the book on every
        # chapter load, so a TOC click or Enter in the search field lost focus.
        # Page-level setting (the profile default stays owner D's); focus is
        # handed to the book explicitly where the spec says so.
        self.host.page.settings().setAttribute(
            QWebEngineSettings.WebAttribute.FocusOnNavigationEnabled, False)
        self.host.loadFinished.connect(self._on_load_finished)
        self.host.loadStopped.connect(self._on_load_stopped)
        self.host.locationChanged.connect(self._on_location)
        self.host.internalLinkClicked.connect(self._on_internal_link)
        self.host.externalLinkRequested.connect(self._on_external_link)
        self.host.navigationBlocked.connect(self._on_navigation_blocked)
        bridge = self.host.bridge
        bridge.positionChanged.connect(self._on_position)
        bridge.selectionChanged.connect(self._on_selection)
        bridge.noteRequested.connect(self._on_note_requested)
        bridge.keyUnhandled.connect(self._on_page_key)

        # ---- state -------------------------------------------------------------
        self._book: EpubBook | None = None
        self._bid = ""
        self._path = ""
        self._spine_chars: list[int] = []
        self._spine_offsets: list[int] = []
        self._total_chars = 0
        self._spine_units: list[int] = []
        self._unit_offsets: list[int] = []
        self._total_units = 0
        self._linear: list[int] = []
        self._image_only: set[int] = set()
        self._cur_spine = -1
        self._loading_spine = -1
        self._state: dict = {}
        self._last_loc: dict | None = None
        self._last_loc_spine = -1
        self._pending: dict = {}
        self._load_serial = 0
        self._page_ready = False
        self._history_back: list[dict] = []
        self._history_fwd: list[dict] = []
        self._search_hits: list[SearchHit] = []
        self._search_query = ""
        self._search_index = -1
        self._hl_ranges: dict[str, tuple[int, int] | None] = {}
        self._active_hl = ""
        self._last_selection: dict | None = None
        self._this_book = False
        self._pending_overrides: dict[str, Any] = {}
        self._zen = False
        self._zen_restore: dict[str, Any] = {}
        self._pre_fs_max = False
        self._fullscreen = False
        self._chrome_visible = True
        self._toc_current = -1
        self._error_spec: dict | None = None
        self._relocate_new: str | None = None
        self._tracker = ReadingTracker(store.get("behavior.reading_speed_units_per_min", SPEED_SEED))
        self._base_seconds = 0.0
        self._base_units = 0.0
        self._jump_until = 0.0
        self._once: set[str] = set()
        self._last_cursor = QPoint(-1, -1)
        self._search_serial = 0
        self._relay = _Relay(self)
        self._relay.done.connect(self._on_worker_done)
        self._worker_serial = 0
        self._shut = False

        # ---- timers ------------------------------------------------------------
        def single(ms: int, slot: Callable[[], None]) -> QTimer:
            t = QTimer(self)
            t.setSingleShot(True)
            t.setInterval(ms)
            t.timeout.connect(slot)
            return t

        self._hide_timer = single(3000, self._on_hide_timer)
        self._capture_timer = single(POSITION_CAPTURE_MS, lambda: self._capture_and_save(False))
        self._toc_timer = single(120, self._update_toc_current)
        self._apply_timer = single(APPLY_DEBOUNCE_MS, self._apply_page_settings_now)
        self._override_timer = single(OVERRIDE_DEBOUNCE_MS, self._flush_overrides)
        self._loading_timer = single(LOADING_DELAY_MS, self._show_loading)
        self._ready_watchdog = single(READY_WATCHDOG_MS, self._on_ready_watchdog)
        self._watchdog_serial = -1
        self._watchdog_retried = False
        self._mouse_timer = QTimer(self)
        self._mouse_timer.setInterval(120)
        self._mouse_timer.timeout.connect(self._poll_mouse)
        self._mouse_timer.start()

        # ---- wiring ------------------------------------------------------------
        d = self.dock
        d.paneChanged.connect(self._on_pane_changed)
        d.toc.jumpRequested.connect(self._on_toc_jump)
        d.toc.escapeRequested.connect(self.escape)
        d.bookmarks.jumpRequested.connect(self._jump_to_bookmark)
        d.bookmarks.removeRequested.connect(lambda i: self.remove_bookmark(i))
        d.notes.jumpRequested.connect(self._jump_to_highlight)
        d.notes.removeRequested.connect(lambda i: self.remove_highlight(i))
        d.notes.editRequested.connect(self.edit_highlight_note)
        d.notes.colorRequested.connect(self.set_highlight_color)
        d.notes.copyRequested.connect(self._copy_highlight_text)
        d.notes.exportRequested.connect(lambda: self.export_highlights())
        d.search.searchRequested.connect(self.run_search)
        d.search.stepRequested.connect(self._search_step)
        d.search.resultActivated.connect(lambda i, f: self._goto_search_hit(i, focus_book=f))
        d.search.escapeRequested.connect(self.escape)
        self.splitter.splitterMoved.connect(self._on_splitter_moved)
        sp = self.settings_panel
        sp.changed.connect(self._on_setting)
        sp.closeRequested.connect(lambda: self.set_settings_visible(False))
        sp.associateRequested.connect(self.associateRequested)
        self.statusbar.rightModeChanged.connect(self._on_status_mode)
        self.statusbar.set_mode(str(store.get("window.status_right", "chapter")))
        self.error_card.action.connect(self._on_error_action)
        self.section_card.prevRequested.connect(lambda: self.prev_chapter())
        self.section_card.nextRequested.connect(self.next_chapter)
        self.goto_popover.goRequested.connect(self.goto_percent)
        self.note_editor.saveRequested.connect(self._save_note)
        self.note_editor.deleteRequested.connect(lambda i: self.remove_highlight(i))
        self.link_confirm.openRequested.connect(lambda url: QDesktopServices.openUrl(QUrl(url)))
        for pop in self.column.popovers:
            pop.closed.connect(self._focus_book_soon)
        self.cheatsheet.closed.connect(self._focus_book_soon)

        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
            app.aboutToQuit.connect(self._on_about_to_quit)
        strings.language_changed.subscribe(self.retranslate_ui)
        self.theme_controller.themeChanged.connect(self.apply_theme)

        dock_w = int(store.get("window.dock_width", DOCK_DEFAULT_WIDTH) or DOCK_DEFAULT_WIDTH)
        self.splitter.setSizes([int(_clamp(dock_w, DOCK_MIN_WIDTH, DOCK_MAX_WIDTH)), 900])
        self.dock.set_pane(str(store.get("window.dock_tab", "toc")))
        self.apply_theme(self.theme, force=True)
        self.retranslate_ui()
        self._set_no_book_ui()

    # ======================================================================
    # public properties
    # ======================================================================
    @property
    def book(self) -> EpubBook | None:
        """The open book, or None."""
        return self._book

    @property
    def book_id(self) -> str:
        """blake2b-128 id of the open book ('' when none)."""
        return self._bid

    @property
    def spine_index(self) -> int:
        """Spine index of the document on screen (-1 when none)."""
        return self._cur_spine

    @property
    def page_state(self) -> dict:
        """The latest ``epubReader.state()`` report (page, pages, gpos, percent...)."""
        return dict(self._state)

    @property
    def last_locator(self) -> dict | None:
        """The last captured (pinned) locator of the current document."""
        return dict(self._last_loc) if self._last_loc else None

    @property
    def search_hits(self) -> list[SearchHit]:
        return list(self._search_hits)

    @property
    def search_index(self) -> int:
        return self._search_index

    @property
    def toc_current(self) -> int:
        """Flat index of the TOC entry marked current (-1 when none)."""
        return self._toc_current

    @property
    def this_book_only(self) -> bool:
        return self._this_book

    def is_ready(self) -> bool:
        """True once the current document has been initialised by reader.js."""
        return self._page_ready

    def is_zen(self) -> bool:
        return self._zen

    def chrome_visible(self) -> bool:
        return self._chrome_visible

    def action_map(self) -> dict[str, list[Callable[[], Any]]]:
        """``strings.KEYS`` id -> the bound slots, one per combo (see ACTION_SLOTS)."""
        out: dict[str, list[Callable[[], Any]]] = {}
        for kid, names in ACTION_SLOTS.items():
            combos = KEYS.get(kid, ())
            slots = [getattr(self, names[i] if len(names) > 1 else names[0])
                     for i in range(max(1, len(combos)))
                     if len(names) == 1 or i < len(names)]
            out[kid] = slots
        return out

    # ======================================================================
    # opening and closing
    # ======================================================================
    def open_book(self, path: str | os.PathLike) -> None:
        """Open *path*.  Failures become in-pane cards, never exceptions."""
        path = os.path.abspath(os.fspath(path))
        if self._book is not None:
            self.close_book()
        self._hide_error()
        self.statusbar.clear_message()
        self._path = path
        self._relocate_new = None
        entry = self._library_entry_for_path(path)
        try:
            st = os.stat(path)
        except FileNotFoundError:
            self._missing(path, entry)
            return
        except OSError as exc:
            self._show_error(self._spec("access", path, entry, exc))
            return
        try:
            bid = self.store.book_id_for(path)
        except FileNotFoundError:
            self._missing(path, entry)
            return
        except OSError as exc:
            self._show_error(self._spec("access", path, entry, exc))
            return
        if entry is None:
            entry = self.store.library_get(bid)
        try:
            book = bookformats.open_book(path, cache_root=self.store.cache_root, content_key=bid)
        except FileNotFoundError:
            self._missing(path, entry)
            return
        except EpubError as exc:
            kind = {"drm": "drm", "not_epub": "structure", "no_container": "structure",
                    "bad_opf": "structure_opf", "too_large": "toolarge",
                    "corrupt": "corrupt", "unsupported": "unsupported"}.get(exc.kind, "unexpected")
            spec = self._spec(kind, path, entry, exc)
            spec["drm_scheme"] = exc.drm_scheme
            spec["book_id"] = (entry or {}).get("id") or bid
            self._show_error(spec)
            return
        except OSError as exc:
            self._show_error(self._spec("access", path, entry, exc))
            return
        except Exception as exc:  # noqa: BLE001 - a broken book must never crash the app
            self._show_error(self._spec("unexpected", path, entry, exc))
            return
        self._start_book(book, bid, path, st)

    def _spec(self, kind: str, path: str, entry: dict | None, exc: BaseException | None) -> dict:
        details = ""
        if exc is not None:
            details = f"{type(exc).__name__}: {exc}"
            extra = getattr(exc, "detail", "")
            if extra and extra not in details:
                details += f"\n{extra}"
        return {"kind": kind, "path": path, "details": details,
                "title": (entry or {}).get("title") or "",
                "in_library": entry is not None, "book_id": (entry or {}).get("id") or ""}

    def _start_book(self, book: EpubBook, bid: str, path: str, st: os.stat_result) -> None:
        self._book = book
        self._bid = bid
        self._path = path
        spine = book.spine
        self._spine_chars = [len(book.plain_text(s.zip_name)) for s in spine]
        self._spine_units = [int(book.units(s.zip_name)) for s in spine]
        self._spine_offsets = [0]
        for n in self._spine_chars:
            self._spine_offsets.append(self._spine_offsets[-1] + n)
        self._total_chars = self._spine_offsets[-1]
        self._unit_offsets = [0]
        for n in self._spine_units:
            self._unit_offsets.append(self._unit_offsets[-1] + n)
        self._total_units = self._unit_offsets[-1]
        self._linear = [i for i, s in enumerate(spine) if s.linear] or list(range(len(spine)))
        self._image_only = {i for i, s in enumerate(spine) if not book.plain_text(s.zip_name).strip()}
        self._upsert_library(book, bid, path, st)

        state = self.store.book_state(bid)
        with self.store.lock:
            stats = state.setdefault("stats", {})
            stats["sessions"] = int(stats.get("sessions") or 0) + 1
            stats["last_session_at"] = now_iso()
            if not state.get("title"):
                state["title"] = self._book_title()
        self._base_seconds = float(stats.get("seconds_read") or 0)
        self._base_units = float(stats.get("units_read") or 0)
        self._this_book = bool(state.get("overrides"))
        self._pending_overrides = {}
        self._history_back = [h for h in (state.get("history") or []) if isinstance(h, dict)][-HISTORY_LIMIT:]
        self._history_fwd = []
        self._tracker = ReadingTracker(
            self.store.get("behavior.reading_speed_units_per_min", SPEED_SEED),
            idle_s=float(self.store.get("behavior.idle_timeout_s", IDLE_TIMEOUT_S) or IDLE_TIMEOUT_S))
        self._once = set()
        self._search_hits, self._search_query, self._search_index = [], "", -1
        self._active_hl = ""
        self._toc_current = -1
        self._state = {}
        self._last_loc = None
        self._last_loc_spine = -1

        self.dock.toc.set_book(book)
        self._resolve_all_highlights()
        self._refresh_bookmarks()
        self._refresh_notes()
        self.dock.search.set_results("", [], self._hit_chapter)
        self.toolbar.set_book_actions_enabled(True)
        self.toolbar.set_title(self._book_title(), "")
        self._update_history_buttons()
        if not self._zen:
            self._set_visible(self.dock, bool(self.store.get("window.dock_visible", True)))
            self._set_visible(self.settings_panel,
                              bool(self.store.get("window.settings_panel_visible", False)))
        if self.settings_panel.isVisible():
            self._load_settings_panel()
        self.column.view.show()

        self.host.set_background_color(self.theme.bg)
        self.host.set_book(book)
        pos = state.get("position") or {}
        spine_i = self._spine_for_position(pos)
        loc = pos.get("locator") if isinstance(pos.get("locator"), dict) else None
        if loc is not None and pos.get("spine_index") not in (None, spine_i) \
                and pos.get("spine_href") != spine[spine_i].zip_name:
            loc = None
        self._cur_spine = -1
        self._goto(spine_i, locator=loc, push_history=False, jump=True, focus=True)
        self._loading_timer.start()
        self.reveal_chrome()
        self.bookOpened.emit(bid)
        self._emit_title()
        notes = [n for n in self.store.load_notes if n.startswith(f"books/{bid}.json") and "reset" in n]
        if notes:
            self.show_message(lambda: S("err.book_state.reset"), ms=6000)
        elif self.store.read_only:
            self.show_message(lambda: S("err.settings.readonly"), ms=6000)

    def _upsert_library(self, book: EpubBook, bid: str, path: str, st: os.stat_result) -> None:
        md = book.metadata or {}
        entry = {
            "id": bid, "path": path, "size": st.st_size, "mtime_ns": st.st_mtime_ns,
            "title": md.get("title") or "", "authors": list(md.get("authors") or []),
            "publisher": md.get("publisher") or "", "pubdate": md.get("date") or "",
            "language": md.get("language") or "", "epub_version": getattr(book, "version", ""),
            "layout": "fixed" if book.is_fixed_layout else "reflowable",
            "toc_source": "spine" if book.toc_is_synthetic else "toc",
            "spine_count": len(book.spine), "units_total": self._total_units,
            "missing": False, "opened_at": now_iso(),
        }
        old = self.store.library_get(bid)
        if old and old.get("path") and os.path.normcase(os.path.abspath(old["path"])) \
                != os.path.normcase(path):
            history = list(old.get("path_history") or [])
            if old["path"] not in history:
                history.append(old["path"])
            entry["path_history"] = history
        self.store.library_upsert(entry)

    def _spine_for_position(self, pos: dict) -> int:
        book = self._book
        assert book is not None
        n = len(book.spine)
        i = pos.get("spine_index")
        href = pos.get("spine_href") or ""
        if isinstance(i, int) and 0 <= i < n and (not href or book.spine[i].zip_name == href):
            return i
        if href:
            j = book.spine_index(href)
            if j is not None:
                return int(j)
        if isinstance(i, int) and 0 <= i < n:
            return i
        return self._linear[0] if self._linear else 0

    def close_book(self) -> None:
        """Save everything and close the book.  Emits ``titleChanged`` for the library."""
        self._hide_error()
        if self._book is None:
            self._set_no_book_ui()
            return
        self._flush_overrides()
        self._finish_session()
        loc = self._current_locator()
        if loc is not None and self._cur_spine >= 0:
            self._save_position(loc, force=True)
        else:
            self.store.save_book_state(self._bid, self.store.book_state(self._bid), immediate=True)
        frac = self._book_fraction()
        self.store.library_upsert({"id": self._bid, "progress": round(frac, 4),
                                   "seconds_read": int(self._base_seconds + self._tracker.total_seconds)})
        self._load_serial += 1
        self._ready_watchdog.stop()
        self._page_ready = False
        self._pending = {}
        try:
            self.host.set_book(None)
        except RuntimeError:
            pass
        book, self._book = self._book, None
        try:
            book.close()
        except Exception:  # noqa: BLE001
            pass
        self._bid = ""
        self._cur_spine = -1
        self._state = {}
        self._search_hits, self._search_query, self._search_index = [], "", -1
        self._hl_ranges = {}
        self._set_no_book_ui()
        self.titleChanged.emit(S("title.library"))

    def _set_no_book_ui(self) -> None:
        for pop in self.column.popovers:
            hide_quietly(pop)
        self.toolbar.set_book_actions_enabled(False)
        self.toolbar.set_title("", "")
        self.toolbar.set_bookmarked(False)
        self.statusbar.set_cells("", None, None)
        self.dock.toc.set_book(None)
        self.dock.bookmarks.set_bookmarks([])
        self.dock.notes.set_highlights([], set(), lambda h: "", lambda h: (0, 0))
        self.dock.search.set_results("", [], self._hit_chapter)
        self._update_history_buttons()

    def shutdown(self) -> None:
        """Close the book, stop timers and release the web host (owner G: on quit)."""
        if self._shut:
            return
        self._shut = True
        try:
            self.close_book()
        finally:
            app = QApplication.instance()
            if app is not None:
                app.removeEventFilter(self)
                try:
                    app.aboutToQuit.disconnect(self._on_about_to_quit)
                except (RuntimeError, TypeError):
                    pass
            strings.language_changed.unsubscribe(self.retranslate_ui)
            for t in (self._hide_timer, self._capture_timer, self._toc_timer, self._apply_timer,
                      self._override_timer, self._loading_timer, self._mouse_timer):
                t.stop()
            if self._own_host:
                self.host.close()
            if self._own_controller:
                try:
                    self.theme_controller.watcher.stop()
                except Exception:  # noqa: BLE001
                    pass

    def _on_about_to_quit(self) -> None:
        if self._book is not None:
            self._flush_overrides()
            self._finish_session()
            loc = self._current_locator()
            if loc is not None:
                self._save_position(loc, force=True)

    # ======================================================================
    # navigation core
    # ======================================================================
    def _goto(self, spine_i: int, *, locator: dict | None = None, fragment: str = "",
              at: str | None = None, search_index: int | None = None,
              push_history: bool = True, jump: bool = True, focus: bool = False) -> None:
        book = self._book
        if book is None or not (0 <= spine_i < len(book.spine)):
            return
        if push_history:
            self._push_history()
        if jump:
            self._jump_until = time.monotonic() + 1.5
        if spine_i == self._cur_spine and self._page_ready:
            self._apply_in_page(locator, fragment, at, search_index)
            if focus:
                self.focus_book()
            return
        self._pending = {"locator": locator, "fragment": fragment, "at": at,
                         "search_index": search_index, "focus": focus or self._view_has_focus()}
        if spine_i == self._loading_spine and not self._page_ready and self._cur_spine == spine_i:
            return  # that document is already on its way; the new intent replaces the old
        self._page_ready = False
        self._load_serial += 1
        self._cur_spine = spine_i
        self._loading_spine = spine_i
        self._state = {}
        hide_quietly(self.section_card)
        self.host.navigate(book.spine[spine_i].zip_name)
        self._arm_ready_watchdog(retried=False)

    # A navigation whose loadFinished (or init callback) never arrives would leave the
    # page un-initialised forever: visible but dead to keys, search and position saving.
    # It was seen once in ~6 end-to-end runs and could not be reproduced on demand, so
    # rather than trust every signal path, retry the navigation once and log why.
    def _arm_ready_watchdog(self, *, retried: bool) -> None:
        self._watchdog_serial = self._load_serial
        self._watchdog_retried = retried
        self._ready_watchdog.start()

    def _on_load_stopped(self, zip_name: str) -> None:
        """The document we are waiting for was aborted: retry it now, not in 8 s."""
        book = self._book
        if book is None or self._page_ready or not 0 <= self._loading_spine < len(book.spine):
            return
        if book.spine[self._loading_spine].zip_name != zip_name:
            return      # a superseded navigation; the one we want is still on its way
        if self._watchdog_retried:
            return      # already retried once; the watchdog reports what happens next
        log.warning("chapter %d (%s): load was stopped before it finished; retrying",
                    self._loading_spine, zip_name)
        self._load_serial += 1
        self._cur_spine = self._loading_spine
        self.host.navigate(zip_name)               # _pending (restore target) is kept
        self._arm_ready_watchdog(retried=True)

    def _on_ready_watchdog(self) -> None:
        book = self._book
        if book is None or self._page_ready or self._load_serial != self._watchdog_serial:
            return
        spine_i = self._loading_spine if self._loading_spine >= 0 else self._cur_spine
        if not 0 <= spine_i < len(book.spine):
            return
        try:
            url = self.view.page().url().toString()
        except RuntimeError:
            url = "?"
        log.warning("chapter %d (%s) not ready after %d ms (loading_spine=%d, page url=%s, retried=%s)",
                    spine_i, book.spine[spine_i].zip_name, READY_WATCHDOG_MS, self._loading_spine,
                    url, self._watchdog_retried)
        if self._watchdog_retried:
            return
        self._load_serial += 1
        self._cur_spine = spine_i
        self._loading_spine = spine_i
        self.host.navigate(book.spine[spine_i].zip_name)   # _pending (restore target) is kept
        self._arm_ready_watchdog(retried=True)

    def _apply_in_page(self, locator: dict | None, fragment: str, at: str | None,
                       search_index: int | None) -> None:
        if search_index is not None:
            self._show_search_active(search_index)
        elif locator:
            self.host.call_reader("restore", locator)
        elif fragment:
            self.host.call_reader("gotoFragment", fragment)
        elif at == "end":
            self.host.call_reader("gotoPercent", 1)
        elif at == "start":
            self.host.call_reader("gotoPage", 0)

    def _on_location(self, zip_name: str, _fragment: str, spine_index: int) -> None:
        # Every navigation is ours (links are followed here too), so while one is in
        # flight the target is already known.  A failed load reverts Chromium's URL
        # to the previous document; that report must not become "current".
        if self._book is not None and spine_index >= 0 and not self._page_ready                 and self._loading_spine < 0:
            self._cur_spine = spine_index

    def _on_load_finished(self, ok: bool) -> None:
        self._loading_timer.stop()
        self.column.loading.hide()
        book = self._book
        if book is None:
            return
        if self._loading_spine >= 0:
            self._cur_spine = self._loading_spine
        self._loading_spine = -1
        serial = self._load_serial
        if not ok:
            self._ready_watchdog.stop()
            self._page_ready = False
            self._pending = {}
            self._state = {}
            hide_quietly(self.view)     # Chromium keeps painting the previous chapter
            self.section_card.present()
            self._update_toc_current()
            self._update_status()
            self.reveal_chrome()
            return
        hide_quietly(self.section_card)
        self.view.show()
        zip_name = book.spine[self._cur_spine].zip_name
        pend, self._pending = self._pending, {}
        cfg: dict[str, Any] = {
            "settings": self._page_settings(),
            "mode": "scroll" if self._effective_settings().get("layout") == "scroll" else "paginated",
            "book": {"offset": self._spine_offsets[self._cur_spine], "total": max(1, self._total_chars)},
        }
        try:
            if book.is_pre_paginated(zip_name):
                cfg["fixedLayout"] = True
        except Exception:  # noqa: BLE001
            pass
        loc = pend.get("locator")
        if loc:
            cfg["locator"] = loc
        self.host.call_reader("init", cfg, callback=lambda st, s=serial, p=pend: self._after_init(s, st, p))

    def _after_init(self, serial: int, st: Any, pend: dict) -> None:
        if serial != self._load_serial or self._book is None:
            return
        self._page_ready = True
        self._ready_watchdog.stop()
        if self._watchdog_retried:
            log.info("chapter %d became ready after a retried navigation", self._cur_spine)
            self._watchdog_retried = False
        self._apply_highlights_to_page()
        si = pend.get("search_index")
        if self._search_hits and si is None:
            self._paint_search_matches()
        if si is not None or pend.get("fragment") or pend.get("at") in ("end", "start"):
            self._apply_in_page(None, pend.get("fragment") or "", pend.get("at"), si)
        if isinstance(st, dict) and self._cur_spine in self._image_only and not si                 and pend.get("at") != "end" and int(st.get("pages") or 1) > 1:
            self._skip_blank_lead_page(serial)
        if isinstance(st, dict):
            self._on_position(st)
            if st.get("fixedLayout") and "fxl" not in self._once and self._declared_fixed():
                # a calibre SVG cover in a reflowable book is also scaled to fit, but
                # "this book is fixed layout" is only true when the book says so
                self._once.add("fxl")
                self.show_message(lambda: S("status.fixed_layout"))
            elif st.get("vertical") and "vertical" not in self._once:
                self._once.add("vertical")
                self.show_message(lambda: S("status.vertical"))
        self._capture_and_save(True)
        if pend.get("focus"):
            self.focus_book()
        self.chapterLoaded.emit(self._cur_spine)

    def _declared_fixed(self) -> bool:
        """The book (or the document on screen) is declared pre-paginated in its OPF."""
        book = self._book
        if book is None:
            return False
        if book.is_fixed_layout:
            return True
        try:
            return bool(book.is_pre_paginated(book.spine[self._cur_spine].zip_name))
        except Exception:  # noqa: BLE001
            return False

    def _skip_blank_lead_page(self, serial: int) -> None:
        """Work around a reader.js pagination bug (reported to owner C).

        An image capped at the page height inside a wrapper with a top margin
        overflows into the second column, so an image-only document such as a
        cover opens on an empty first page.  For those documents only, move to
        the first page that actually shows an image.
        """
        probe = ("(function(){var W=innerWidth||1,se=document.scrollingElement||document.documentElement,"
                 "best=-1;for(var i=0;i<document.images.length;i++){var r=document.images[i]"
                 ".getBoundingClientRect();if(!r.width||!r.height)continue;var p=Math.floor((r.left+"
                 "Math.abs(se.scrollLeft)+1)/W);if(best<0||p<best)best=p;}return best;})()")

        def done(page: Any) -> None:
            if serial == self._load_serial and isinstance(page, int) and page > 0                     and int(self._state.get("page") or 0) < page:
                self.host.call_reader("gotoPage", page)

        self.host.run_json(probe, done)

    def _on_position(self, st: Any) -> None:
        if not isinstance(st, dict) or self._book is None or not self._page_ready and not st:
            return
        self._state = st
        self._update_status()
        self._schedule_toc()
        self._capture_timer.start()
        now = time.monotonic()
        self._tracker.observe(self._unit_position(), now, sequential=now > self._jump_until)
        self._update_bookmark_button()
        self.progressChanged.emit(self._bid, self._book_fraction())

    def _unit_position(self) -> float:
        i = self._cur_spine
        if not (0 <= i < len(self._spine_units)):
            return 0.0
        frac = float(self._state.get("chapterPercent") or 0.0)
        return self._unit_offsets[i] + self._spine_units[i] * _clamp(frac, 0.0, 1.0)

    def _book_fraction(self) -> float:
        if self._book is None or not self._state:
            return 0.0
        st = self._state
        i = self._cur_spine
        if self._linear and i == self._linear[-1] and st.get("pages") \
                and int(st.get("page") or 0) >= int(st.get("pages") or 1) - 1 \
                and st.get("mode") == "paginated":
            return 1.0
        return _clamp(float(st.get("percent") or 0.0), 0.0, 1.0)

    # ======================================================================
    # position persistence
    # ======================================================================
    def _capture_and_save(self, force: bool = False) -> None:
        if self._book is None:
            return
        if not self._page_ready:
            if force:
                loc = self._current_locator()
                if loc is not None:
                    self._save_position(loc, force=True)
            return
        serial = self._load_serial
        spine = self._cur_spine

        def done(loc: Any) -> None:
            if serial != self._load_serial or self._book is None or not isinstance(loc, dict):
                return
            if "gpos" not in loc:
                return
            self._last_loc = loc
            self._last_loc_spine = spine
            self._save_position(loc, force)

        self.host.call_reader("capture", callback=done)

    def _current_locator(self) -> dict | None:
        """Best locator available right now, without a JS round trip."""
        book = self._book
        i = self._cur_spine
        if book is None or not (0 <= i < len(book.spine)):
            return None
        g = self._state.get("gpos") if self._state else None
        if self._last_loc and self._last_loc_spine == i and (g is None or self._last_loc.get("gpos") == g):
            return dict(self._last_loc)
        if g is None:
            return dict(self._last_loc) if self._last_loc and self._last_loc_spine == i else None
        text = book.plain_text(book.spine[i].zip_name)
        g = int(g)
        return {"gpos": g, "snippet": text[g:g + 40], "before": text[max(0, g - 20):g],
                "chapterPercent": self._state.get("chapterPercent"),
                "percent": self._state.get("percent")}

    def _save_position(self, loc: dict, force: bool) -> None:
        book = self._book
        if book is None or not self._bid or not (0 <= self._cur_spine < len(book.spine)):
            return
        frac = self._book_fraction()
        pos = {
            "spine_index": self._cur_spine,
            "spine_href": book.spine[self._cur_spine].zip_name,
            "locator": dict(loc),
            "doc_progress": loc.get("chapterPercent"),
            "book_progress": round(frac, 6),
            "chapter_title": self._chapter_title(),
            "at": now_iso(),
        }
        state = self.store.book_state(self._bid)
        with self.store.lock:
            stats = state.setdefault("stats", {})
            stats["seconds_read"] = int(self._base_seconds + self._tracker.total_seconds)
            stats["units_read"] = int(self._base_units + self._tracker.total_units)
            state["history"] = [dict(h) for h in self._history_back[-HISTORY_LIMIT:]]
        self.store.save_position(self._bid, pos, force=force)
        fields: dict[str, Any] = {"progress": round(frac, 4),
                                  "seconds_read": int(self._base_seconds + self._tracker.total_seconds)}
        if frac >= 0.995:
            entry = self.store.library_get(self._bid) or {}
            if not entry.get("finished_at"):
                fields["finished_at"] = now_iso()
        self.store.library_update(self._bid, **fields)

    def _finish_session(self) -> None:
        if self._book is None:
            return
        before = self._tracker.speed
        n_samples = len(self._tracker.samples)
        self._tracker.finish(time.monotonic())
        state = self.store.book_state(self._bid)
        with self.store.lock:
            stats = state.setdefault("stats", {})
            stats["seconds_read"] = int(self._base_seconds + self._tracker.total_seconds)
            stats["units_read"] = int(self._base_units + self._tracker.total_units)
            new = [round(s, 1) for s in self._tracker.samples[n_samples:]]
            if new:
                stats["speed_samples"] = (list(stats.get("speed_samples") or []) + new)[-20:]
        if self._tracker.speed != before and self.store.get("behavior.reading_speed_learned", True):
            self.store.set("behavior.reading_speed_units_per_min", round(self._tracker.speed, 1))
            self.settings_panel.set_speed(self._tracker.speed)

    # ======================================================================
    # status bar, toolbar title, TOC tracking
    # ======================================================================
    def _book_title(self) -> str:
        if self._book is None:
            return ""
        return (self._book.metadata or {}).get("title") or os.path.splitext(os.path.basename(self._path))[0]

    def _chapter_title(self) -> str:
        book = self._book
        i = self._cur_spine
        if book is None or not (0 <= i < len(book.spine)):
            return ""
        entries = self.dock.toc.entries
        if 0 <= self._toc_current < len(entries) and entries[self._toc_current]["spine"] is not None \
                and entries[self._toc_current]["spine"] <= i:
            return self.dock.toc.title_of(self._toc_current)
        try:
            return book.doc_title(book.spine[i].zip_name)
        except Exception:  # noqa: BLE001
            return ""

    def _right_text(self) -> str:
        if self._book is None:
            return ""
        mode = self.statusbar.mode
        if mode == "read":
            minutes = (self._base_seconds + self._tracker.total_seconds) / 60.0
            return S("status.read", time=duration(minutes))
        i = self._cur_spine
        if not (0 <= i < len(self._spine_units)):
            return ""
        frac = _clamp(float(self._state.get("chapterPercent") or 0.0), 0.0, 1.0)
        chapter_left = self._spine_units[i] * (1.0 - frac)
        if mode == "chapter":
            return S("status.left.chapter", time=duration(self._tracker.minutes_for(chapter_left)))
        book_left = chapter_left + (self._total_units - self._unit_offsets[i + 1])
        return S("status.left.book", time=duration(self._tracker.minutes_for(book_left)))

    def _update_status(self) -> None:
        if self._book is None:
            self.statusbar.set_cells("", None, None)
            return
        chapter = self._chapter_title()
        self.toolbar.set_title(self._book_title(), chapter)
        if not self._state:
            self.statusbar.set_cells(chapter, None, None)
            return
        frac = self._book_fraction()
        self.statusbar.set_cells(chapter, lambda: S("status.percent", p=_percent_int(frac)),
                                 self._right_text)

    def _on_status_mode(self, mode: str) -> None:
        self.store.set("window.status_right", mode)
        self._update_status()

    def cycle_status_cell(self) -> None:
        """Same as right-clicking the right status cell and picking the next item."""
        self.statusbar.cycle_mode()

    def _schedule_toc(self) -> None:
        self._toc_timer.start()

    def _update_toc_current(self) -> None:
        if self._book is None:
            return
        cur = self._cur_spine
        frags = sorted({e["fragment"] for e in self.dock.toc.entries
                        if e["spine"] == cur and e["fragment"]})
        if frags and self._page_ready:
            serial = self._load_serial
            self.host.run_json(_TOC_PROBE_JS % _json_dumps(frags),
                               lambda res, s=serial: s == self._load_serial and self._pick_toc(res or {}))
        else:
            self._pick_toc({})

    def _pick_toc(self, flags: dict) -> None:
        cur = self._cur_spine
        best_cur = -1
        best_prev = -1
        best_prev_spine = -1
        for i, e in enumerate(self.dock.toc.entries):
            si = e["spine"]
            if si is None:
                continue
            if si == cur:
                if not e["fragment"] or flags.get(e["fragment"]):
                    best_cur = i
            elif si < cur and si >= best_prev_spine:
                best_prev, best_prev_spine = i, si
        chosen = best_cur if best_cur >= 0 else best_prev
        if chosen == -1:
            for i, e in enumerate(self.dock.toc.entries):
                if e["spine"] == cur:
                    chosen = i
                    break
        self._toc_current = chosen
        self.dock.toc.set_current(chosen)
        self._update_status()

    # ======================================================================
    # reading slots (one per keyboard action)
    # ======================================================================
    def next_page(self) -> None:
        """Space / PageDown / → / ↓ / j."""
        if self._page_ready:
            self.host.call_reader("nextPage")

    def prev_page(self) -> None:
        """Shift+Space / PageUp / ← / ↑ / k."""
        if self._page_ready:
            self.host.call_reader("prevPage")

    def _next_linear(self, d: int) -> int | None:
        cur = self._cur_spine
        seq = self._linear
        if not seq:
            return None
        if d > 0:
            nxt = [i for i in seq if i > cur]
            return nxt[0] if nxt else None
        prv = [i for i in seq if i < cur]
        return prv[-1] if prv else None

    def next_chapter(self) -> None:
        """Ctrl+→ / Ctrl+PageDown, and turning past the last page."""
        if self._book is None:
            return
        i = self._next_linear(1)
        if i is None:
            self.show_message(lambda: S("status.book_end"))
            return
        self._goto(i, at="start", push_history=False, jump=False)

    def prev_chapter(self, at_end: bool = False) -> None:
        """Ctrl+← / Ctrl+PageUp (chapter start); turning before page 1 lands at its end."""
        if self._book is None:
            return
        i = self._next_linear(-1)
        if i is None:
            self.show_message(lambda: S("status.book_start"))
            return
        self._goto(i, at="end" if at_end else "start", push_history=False, jump=not at_end)

    def chapter_start(self) -> None:
        """Home."""
        if self._page_ready:
            self.host.call_reader("gotoPercent", 0)

    def chapter_end(self) -> None:
        """End."""
        if self._page_ready:
            self.host.call_reader("gotoPercent", 1)

    def book_start(self) -> None:
        """Ctrl+Home."""
        if self._book is not None and self._linear:
            self._goto(self._linear[0], at="start")

    def book_end(self) -> None:
        """Ctrl+End."""
        if self._book is not None and self._linear:
            self._goto(self._linear[-1], at="end")

    def goto(self) -> None:
        """Ctrl+G: the go-to popover (whole-book percent)."""
        if self._book is None:
            return
        self.reveal_chrome()
        self.goto_popover.open(self._book_fraction() * 100.0)

    def goto_percent(self, percent: float) -> None:
        """Jump to *percent* (0-100) of the whole book, by character offset."""
        if self._book is None or not self._total_chars:
            return
        target = _clamp(float(percent), 0.0, 100.0) / 100.0 * self._total_chars
        i = 0
        for j in range(len(self._spine_chars)):
            if self._spine_offsets[j] <= target < self._spine_offsets[j + 1]:
                i = j
                break
        else:
            i = max(0, len(self._spine_chars) - 1)
        local = int(max(0, min(self._spine_chars[i] - 1, target - self._spine_offsets[i])))
        self._goto(i, locator={"gpos": local}, focus=True)

    # -- jump history -----------------------------------------------------------
    def _here(self) -> dict | None:
        if self._book is None or self._cur_spine < 0:
            return None
        return {"spine_index": self._cur_spine, "gpos": int(self._state.get("gpos") or 0),
                "at": now_iso()}

    def _push_history(self) -> None:
        here = self._here()
        if here is None or not self._page_ready:
            return
        last = self._history_back[-1] if self._history_back else None
        if last and last.get("spine_index") == here["spine_index"] and last.get("gpos") == here["gpos"]:
            return
        self._history_back.append(here)
        del self._history_back[:-HISTORY_LIMIT]
        self._history_fwd.clear()
        self._update_history_buttons()

    def _update_history_buttons(self) -> None:
        on = self._book is not None
        self.toolbar.set_history(on and bool(self._history_back), on and bool(self._history_fwd))

    def jump_back(self) -> None:
        """Alt+←: back through jumps (not a page turn)."""
        if not self._history_back or self._book is None:
            return
        here = self._here()
        target = self._history_back.pop()
        if here is not None:
            self._history_fwd.append(here)
        self._update_history_buttons()
        self._goto(int(target.get("spine_index") or 0), locator={"gpos": int(target.get("gpos") or 0)},
                   push_history=False)

    def jump_forward(self) -> None:
        """Alt+→: forward through jumps."""
        if not self._history_fwd or self._book is None:
            return
        here = self._here()
        target = self._history_fwd.pop()
        if here is not None:
            self._history_back.append(here)
        self._update_history_buttons()
        self._goto(int(target.get("spine_index") or 0), locator={"gpos": int(target.get("gpos") or 0)},
                   push_history=False)

    # -- links --------------------------------------------------------------------
    def _on_internal_link(self, zip_name: str, fragment: str) -> None:
        book = self._book
        if book is None:
            return
        i = book.spine_index(zip_name)
        if i is None:
            self.show_message(lambda: S("status.link.missing"))
            return
        self._goto(int(i), fragment=fragment or "", at=None if fragment else "start")

    def _on_external_link(self, url: str) -> None:
        self.reveal_chrome()
        self.link_confirm.ask(url)

    def _on_navigation_blocked(self, _url: str, reason: str) -> None:
        if reason == "missing":
            self.show_message(lambda: S("status.link.missing"))

    # ======================================================================
    # page keys, selection, notes from the page
    # ======================================================================
    def _on_page_key(self, desc: str) -> None:
        if desc == "EpubReader.NextChapter":
            self.next_chapter()
            return
        if desc == "EpubReader.PrevChapter":
            self.prev_chapter(at_end=True)
            return
        if desc == "EpubReader.TapCentre":
            self.toggle_chrome()
            return
        if self.handle_letter_keys and desc in _LETTER_KEYS:
            self.dispatch_page_key(desc)
            return
        self.pageKeyUnhandled.emit(desc)

    def dispatch_page_key(self, desc: str) -> bool:
        """Run the slot for a key description the page forwarded (``'J'``, ``'Shift+N'``, ``'F3'``...)."""
        name = PAGE_KEY_ACTIONS.get(desc)
        if not name:
            return False
        getattr(self, name)()
        return True

    def _on_selection(self, info: Any) -> None:
        self._last_selection = info if isinstance(info, dict) and info.get("text") else None

    def _on_note_requested(self, hid: str) -> None:
        self._active_hl = str(hid)
        self.edit_highlight_note(str(hid))

    # ======================================================================
    # panels
    # ======================================================================
    def _toggle_pane(self, key: str) -> None:
        if self.dock.isVisible() and self.dock.current_pane() == key:
            self.set_dock_visible(False)
            self.focus_book()
            return
        self.dock.set_pane(key)
        self.store.set("window.dock_tab", key)
        self.set_dock_visible(True)
        target = {"toc": self.dock.toc.tree, "bookmarks": self.dock.bookmarks.list,
                  "notes": self.dock.notes.list, "search": self.dock.search.edit}[key]
        target.setFocus()

    def toggle_toc(self) -> None:
        """Ctrl+T."""
        self._toggle_pane("toc")

    def toggle_bookmarks(self) -> None:
        """Ctrl+B: the bookmarks pane (Ctrl+D adds one)."""
        self._toggle_pane("bookmarks")

    def toggle_notes(self) -> None:
        """Ctrl+E: the highlights pane."""
        self._toggle_pane("notes")

    def focus_search(self) -> None:
        """Ctrl+F or "/": open the search pane and focus its field."""
        self.dock.set_pane("search")
        self.store.set("window.dock_tab", "search")
        self.set_dock_visible(True)
        self.dock.search.edit.setFocus()
        self.dock.search.edit.selectAll()

    def set_dock_visible(self, on: bool) -> None:
        if on == self.dock.isVisible():
            return
        self._set_visible(self.dock, on)
        if not self._zen:
            self.store.set("window.dock_visible", bool(on))
        if on:
            self.reveal_chrome()
            self._capture_and_save(True)

    def _on_pane_changed(self, key: str) -> None:
        self.store.set("window.dock_tab", key)

    def _on_splitter_moved(self, *_: Any) -> None:
        if self.dock.isVisible():
            self.store.set("window.dock_width", int(self.dock.width()))

    def toggle_settings(self) -> None:
        """Ctrl+,: the typography / settings panel."""
        self.set_settings_visible(not self.settings_panel.isVisible())

    def set_settings_visible(self, on: bool) -> None:
        if on:
            self._load_settings_panel()
        if on == self.settings_panel.isVisible():
            return
        self._set_visible(self.settings_panel, on)
        if not self._zen:
            self.store.set("window.settings_panel_visible", bool(on))
        if on:
            self.reveal_chrome()
            self._capture_and_save(True)
        else:
            self.focus_book()

    def toggle_cheatsheet(self) -> None:
        """F1 / Ctrl+/."""
        if self.cheatsheet.isVisible():
            self.cheatsheet.dismiss()
        else:
            self.cheatsheet.open()

    def escape(self) -> bool:
        """Esc, in the spec's priority order.  Returns True when something closed."""
        if self.cheatsheet.isVisible():
            self.cheatsheet.dismiss()
            return True
        for pop in self.column.popovers:
            if pop.isVisible() and pop is not self.section_card:
                pop.dismiss()
                return True
        if self.settings_panel.isVisible():
            self.set_settings_visible(False)
            return True
        if self.dock.isVisible():
            self.set_dock_visible(False)
            self.focus_book()
            return True
        if self._zen:
            self.set_zen(False)
            return True
        if self.window().isFullScreen():
            self.toggle_fullscreen()
            return True
        if self._last_selection and self._page_ready:
            self.host.call_reader("clearSelection")
            self._last_selection = None
            return True
        return False

    @staticmethod
    def _set_visible(widget: QWidget, on: bool) -> None:
        if on:
            widget.show()
        else:
            hide_quietly(widget)

    def _view_has_focus(self) -> bool:
        fw = QApplication.focusWidget()
        return fw is not None and (fw is self.view or self.view.isAncestorOf(fw))

    def focus_book(self) -> None:
        """Give keyboard focus to the book view."""
        if self._book is not None:
            self.view.setFocus(Qt.FocusReason.OtherFocusReason)

    def _focus_book_soon(self) -> None:
        QTimer.singleShot(0, self.focus_book)

    # ======================================================================
    # chrome auto-hide
    # ======================================================================
    def _auto_hide_ms(self) -> int:
        try:
            return int(self.store.get("behavior.auto_hide_chrome_ms", 3000) or 0)
        except (TypeError, ValueError):
            return 3000

    def _chrome_pinned(self) -> bool:
        if self._book is None or self._error_spec is not None or self._auto_hide_ms() <= 0:
            return True
        if QApplication.activePopupWidget() is not None:
            return True
        if self.toolbar.underMouse() or self.statusbar.underMouse():
            return True
        if self.section_card.isVisible():
            return True
        return any(p.isVisible() for p in self.column.popovers)

    def reveal_chrome(self) -> None:
        """Show the toolbar and status bar (never in focus mode)."""
        if self._zen:
            return
        self.toolbar.show()
        self.statusbar.show()
        self.toolbar.raise_()
        self.statusbar.raise_()
        self._chrome_visible = True
        self._restart_hide()

    def hide_chrome(self) -> None:
        self.toolbar.hide()
        if not self.statusbar.has_message():
            self.statusbar.hide()
        self._chrome_visible = False
        self._hide_timer.stop()

    def toggle_chrome(self) -> None:
        """A tap in the middle of the page."""
        if self._chrome_visible:
            self.hide_chrome()
        else:
            self.reveal_chrome()

    def _restart_hide(self) -> None:
        ms = self._auto_hide_ms()
        if ms > 0 and not self._zen:
            self._hide_timer.start(ms)
        else:
            self._hide_timer.stop()

    def _on_hide_timer(self) -> None:
        if self._chrome_pinned():
            self._restart_hide()
            return
        self.hide_chrome()

    def _poll_mouse(self) -> None:
        if not self.isVisible() or self._zen:
            return
        pos = QCursor.pos()
        if pos == self._last_cursor:
            return
        self._last_cursor = pos
        self._handle_pointer(pos)

    def _handle_pointer(self, pos: QPoint) -> None:
        """Mouse moved to global *pos*: keep the chrome up, or reveal it near the top edge."""
        if self._zen:
            return
        local = self.column.mapFromGlobal(pos)
        inside = self.column.rect().contains(local)
        if self._chrome_visible:
            if inside:
                self._restart_hide()
        elif inside and 0 <= local.y() < REVEAL_ZONE_PX:
            self.reveal_chrome()

    def show_message(self, text: str | Callable[[], str], *, undo: Callable[[], None] | None = None,
                     ms: int = NOTE_MS) -> None:
        """A transient status-bar message (a callable is re-rendered on language change)."""
        render = text if callable(text) else (lambda t=text: t)
        self.statusbar.show_message(render, undo=undo, ms=ms)
        self.statusbar.show()
        self.statusbar.raise_()
        QTimer.singleShot(ms + 50, self._after_message)

    def _after_message(self) -> None:
        if not self.statusbar.has_message() and (not self._chrome_visible or self._zen):
            self.statusbar.hide()

    # ======================================================================
    # zen and full screen
    # ======================================================================
    def toggle_fullscreen(self) -> None:
        """F11."""
        w = self.window()
        if w.isFullScreen():
            if self._pre_fs_max:
                w.showMaximized()
            else:
                w.showNormal()
        else:
            self._pre_fs_max = w.isMaximized()
            w.showFullScreen()
            self.show_message(lambda: S("status.fullscreen.hint"))
        QTimer.singleShot(0, self._sync_fullscreen)

    def _sync_fullscreen(self) -> None:
        fs = bool(self.window().isFullScreen())
        self.toolbar.set_fullscreen(fs)
        if fs != self._fullscreen:
            self._fullscreen = fs
            self.fullscreenChanged.emit(fs)

    def set_zen(self, on: bool, *, fullscreen: bool = True) -> None:
        """专注模式: chrome never appears, panels close, wider margin (+ full screen)."""
        if on == self._zen:
            return
        w = self.window()
        if on:
            self._zen_restore = {"dock": self.dock.isVisible(),
                                 "settings": self.settings_panel.isVisible(),
                                 "entered_fs": False, "max": w.isMaximized()}
            self._zen = True
            hide_quietly(self.dock)
            hide_quietly(self.settings_panel)
            self.hide_chrome()
            if fullscreen and not w.isFullScreen():
                self._zen_restore["entered_fs"] = True
                self._pre_fs_max = w.isMaximized()
                w.showFullScreen()
            self.show_message(lambda: S("status.zen.hint"))
        else:
            self._zen = False
            r = self._zen_restore
            self._set_visible(self.dock, bool(r.get("dock", False)) and self._book is not None)
            self._set_visible(self.settings_panel, bool(r.get("settings", False)))
            if r.get("entered_fs") and w.isFullScreen():
                if r.get("max"):
                    w.showMaximized()
                else:
                    w.showNormal()
            self.reveal_chrome()
        QTimer.singleShot(0, self._sync_fullscreen)
        self._schedule_apply()
        self.zenChanged.emit(self._zen)

    def toggle_zen(self) -> None:
        """Ctrl+Shift+F."""
        self.set_zen(not self._zen)

    # ======================================================================
    # app-level requests (owner G acts on them)
    # ======================================================================
    def request_open(self) -> None:
        """Ctrl+O: ask the app to show its open dialog."""
        self.openBookRequested.emit()

    def request_about(self) -> None:
        self.aboutRequested.emit()

    def request_quit(self) -> None:
        """Ctrl+Q."""
        self.quitRequested.emit()

    def back_to_library(self) -> None:
        """Ctrl+Shift+L: save, close the book and ask for the library."""
        self.close_book()
        self.backToLibrary.emit()

    def close_and_return(self) -> None:
        """Ctrl+W: close the current book and return to the library."""
        self.back_to_library()

    # ======================================================================
    # bookmarks
    # ======================================================================
    def _bookmarks(self) -> list[dict]:
        if not self._bid:
            return []
        return list(self.store.book_state(self._bid).get("bookmarks") or [])

    def _refresh_bookmarks(self) -> None:
        self.dock.bookmarks.set_bookmarks(self._bookmarks())
        self._update_bookmark_button()

    def _bookmark_on_screen(self) -> dict | None:
        st = self._state
        if not st or self._book is None:
            return None
        g = int(st.get("gpos") or 0)
        pages = max(1, int(st.get("pages") or 1))
        chars = int(st.get("chars") or 0)
        per = max(1, chars // pages) if st.get("mode") == "paginated" else 400
        zip_name = self._book.spine[self._cur_spine].zip_name if self._cur_spine >= 0 else ""
        for bm in self._bookmarks():
            if bm.get("spine_index") != self._cur_spine and bm.get("spine_href") != zip_name:
                continue
            bg = int((bm.get("locator") or {}).get("gpos") or 0)
            if bg == g or g <= bg < g + per:
                return bm
        return None

    def _update_bookmark_button(self) -> None:
        self.toolbar.set_bookmarked(self._bookmark_on_screen() is not None)

    def toggle_bookmark(self) -> None:
        """Ctrl+D: add a bookmark here, or remove the one on this screen."""
        if self._book is None or not self._page_ready:
            return
        existing = self._bookmark_on_screen()
        if existing is not None:
            self.remove_bookmark(str(existing.get("id")))
            return
        serial = self._load_serial
        spine = self._cur_spine

        def done(loc: Any) -> None:
            if serial != self._load_serial or self._book is None or not isinstance(loc, dict):
                return
            zip_name = self._book.spine[spine].zip_name
            text = self._book.plain_text(zip_name)
            g = int(loc.get("gpos") or 0)
            bm = {
                "spine_index": spine, "spine_href": zip_name, "locator": dict(loc),
                "chapter_title": self._chapter_title(), "book_progress": round(self._book_fraction(), 6),
                "label": "", "text": _collapse(text[g:g + 240])[:180],
            }
            self.store.add_bookmark(self._bid, bm)
            self._refresh_bookmarks()
            self.show_message(lambda: S("status.bookmark.added"))

        self.host.call_reader("capture", callback=done)

    def remove_bookmark(self, bookmark_id: str) -> None:
        """Remove one bookmark with a 3 s undo in the status bar."""
        if not self._bid:
            return
        victim = next((dict(b) for b in self._bookmarks() if str(b.get("id")) == str(bookmark_id)), None)
        if victim is None:
            return
        self.store.remove_bookmark(self._bid, str(bookmark_id))
        self._refresh_bookmarks()
        bid = self._bid

        def undo() -> None:
            if self._bid != bid:
                return
            self.store.add_bookmark(bid, dict(victim))
            self._refresh_bookmarks()
            self.show_message(lambda: S("common.undone"))

        self.show_message(lambda: S("status.bookmark.removed"), undo=undo, ms=UNDO_MS)

    def _jump_to_bookmark(self, bookmark_id: str, focus_book: bool) -> None:
        bm = next((b for b in self._bookmarks() if str(b.get("id")) == str(bookmark_id)), None)
        if bm is None or self._book is None:
            return
        i = self._spine_for_position(bm)
        self._goto(i, locator=dict(bm.get("locator") or {}), focus=focus_book)

    # ======================================================================
    # highlights
    # ======================================================================
    def _highlights(self) -> list[dict]:
        if not self._bid:
            return []
        return list(self.store.book_state(self._bid).get("highlights") or [])

    def _hl_spine(self, h: dict) -> int:
        return self._spine_for_position(h) if self._book is not None else -1

    def _resolve_hl(self, h: dict) -> tuple[int, int] | None:
        book = self._book
        if book is None:
            return None
        i = self._hl_spine(h)
        if not (0 <= i < len(book.spine)):
            return None
        text = book.plain_text(book.spine[i].zip_name)
        start = h.get("start") or {}
        end = h.get("end") or {}
        s, e = start.get("gpos"), end.get("gpos")
        full = h.get("text") or ""
        if isinstance(s, int) and isinstance(e, int) and 0 <= s < e <= len(text):
            if full and text[s:e] == full:
                return s, e
            snip = start.get("snippet") or start.get("text") or ""
            if not full and (not snip or text[s:s + len(snip)] == snip):
                return s, e
        needle = full or start.get("snippet") or start.get("text") or ""
        if not needle:
            return None
        hits: list[int] = []
        k = text.find(needle)
        while k >= 0 and len(hits) < 2000:
            hits.append(k)
            k = text.find(needle, k + 1)
        if not hits:
            return None
        want = s if isinstance(s, int) else 0
        before = start.get("before") or ""
        with_before = [k for k in hits if before and text[max(0, k - len(before)):k] == before]
        pool = with_before or hits
        best = min(pool, key=lambda k: abs(k - want))
        length = len(full) if full else ((e - s) if isinstance(s, int) and isinstance(e, int) and e > s
                                         else len(needle))
        return best, best + length

    def _resolve_all_highlights(self) -> None:
        self._hl_ranges = {}
        if not self._bid:
            return
        state = self.store.book_state(self._bid)
        changed = False
        for h in self._highlights():
            hid = str(h.get("id"))
            r = self._resolve_hl(h)
            self._hl_ranges[hid] = r
            want = "lost" if r is None else ("exact" if (h.get("start") or {}).get("gpos") == r[0] else "shifted")
            if h.get("anchor_state") != want:
                with self.store.lock:
                    for live in state.get("highlights") or []:
                        if str(live.get("id")) == hid:
                            live["anchor_state"] = want
                changed = True
        if changed:
            self.store.save_book_state(self._bid, state)

    def _refresh_notes(self) -> None:
        book = self._book
        lost = {hid for hid, r in self._hl_ranges.items() if r is None}

        def order(h: dict) -> tuple:
            i = self._hl_spine(h)
            r = self._hl_ranges.get(str(h.get("id")))
            return (i, r[0] if r else int((h.get("start") or {}).get("gpos") or 0))

        def group_title(h: dict) -> str:
            if book is None:
                return h.get("chapter_title") or ""
            i = self._hl_spine(h)
            best = ""
            for e in self.dock.toc.entries:
                if e["spine"] == i and e["title"]:
                    best = e["title"]
                    break
            return best or h.get("chapter_title") or (book.doc_title(book.spine[i].zip_name)
                                                      if 0 <= i < len(book.spine) else "")

        self.dock.notes.set_highlights(self._highlights(), lost, group_title, order)

    def _apply_highlights_to_page(self) -> None:
        if not self._page_ready or self._book is None:
            return
        items = []
        for h in self._highlights():
            if self._hl_spine(h) != self._cur_spine:
                continue
            r = self._hl_ranges.get(str(h.get("id")))
            if not r:
                continue
            items.append({"id": h.get("id"), "start": r[0], "end": r[1],
                          "color": h.get("color") or "yellow", "note": h.get("note") or "",
                          "style": h.get("style") or "fill"})
        self.host.call_reader("applyHighlights", items)

    def highlight_selection(self, color: str = "yellow", *, then_note: bool = False) -> None:
        """Highlight the current selection in *color* (Ctrl+1..4)."""
        if self._book is None or not self._page_ready:
            return
        if color not in theme_mod.HIGHLIGHT_COLORS:
            color = "yellow"
        serial = self._load_serial
        spine = self._cur_spine

        def got(sel: Any) -> None:
            if serial != self._load_serial or self._book is None:
                return
            if not isinstance(sel, dict) or sel.get("start") is None or sel.get("end") is None:
                self.show_message(lambda: S("status.no_selection"))
                return
            a, b = int(sel["start"]), int(sel["end"])
            if b <= a:
                self.show_message(lambda: S("status.no_selection"))
                return
            zip_name = self._book.spine[spine].zip_name
            text = self._book.plain_text(zip_name)
            for h in self._highlights():
                if self._hl_spine(h) == spine and self._hl_ranges.get(str(h.get("id"))) == (a, b):
                    self.set_highlight_color(str(h.get("id")), color)
                    self.host.call_reader("clearSelection")
                    if then_note:
                        self.edit_highlight_note(str(h.get("id")))
                    return
            hl = {
                "spine_index": spine, "spine_href": zip_name,
                "start": {"gpos": a, "snippet": text[a:a + 40], "before": text[max(0, a - 20):a]},
                "end": {"gpos": b, "snippet": text[b:b + 40], "before": text[max(0, b - 20):b]},
                "text": text[a:b], "note": "", "color": color, "style": "fill",
                "chapter_title": self._chapter_title(),
                "book_progress": round((self._spine_offsets[spine] + a) / max(1, self._total_chars), 6),
            }
            hl = self.store.add_highlight(self._bid, hl)
            self._hl_ranges[str(hl["id"])] = (a, b)
            self._active_hl = str(hl["id"])
            self._apply_highlights_to_page()
            self._refresh_notes()
            self.host.call_reader("clearSelection")
            self._last_selection = None
            self.show_message(lambda: S("status.highlight.added"))
            if then_note:
                self.edit_highlight_note(str(hl["id"]))

        self.host.call_reader("selectionInfo", callback=got)

    def highlight_yellow(self) -> None:
        """Ctrl+1."""
        self.highlight_selection("yellow")

    def highlight_green(self) -> None:
        """Ctrl+2."""
        self.highlight_selection("green")

    def highlight_blue(self) -> None:
        """Ctrl+3."""
        self.highlight_selection("blue")

    def highlight_pink(self) -> None:
        """Ctrl+4."""
        self.highlight_selection("pink")

    def highlight_with_note(self) -> None:
        """Highlight the selection and open the note editor."""
        self.highlight_selection("yellow", then_note=True)

    def edit_highlight_note(self, hid: str) -> None:
        h = next((x for x in self._highlights() if str(x.get("id")) == str(hid)), None)
        if h is None:
            return
        self._active_hl = str(hid)
        self.reveal_chrome()
        self.note_editor.open(h)

    def _save_note(self, hid: str, note: str, color: str) -> None:
        if not self._bid:
            return
        self.store.update_highlight(self._bid, hid, note=note, color=color)
        self._apply_highlights_to_page()
        self._refresh_notes()
        self.show_message(lambda: S("status.note.saved"))

    def set_highlight_color(self, hid: str, color: str) -> None:
        if not self._bid or color not in theme_mod.HIGHLIGHT_COLORS:
            return
        self.store.update_highlight(self._bid, hid, color=color)
        self._apply_highlights_to_page()
        self._refresh_notes()

    def remove_highlight(self, hid: str) -> None:
        """Delete one highlight with a 3 s undo in the status bar."""
        if not self._bid:
            return
        victim = next((dict(h) for h in self._highlights() if str(h.get("id")) == str(hid)), None)
        if victim is None:
            return
        self.store.remove_highlight(self._bid, str(hid))
        rng = self._hl_ranges.pop(str(hid), None)
        if self._active_hl == str(hid):
            self._active_hl = ""
        self._apply_highlights_to_page()
        self._refresh_notes()
        bid = self._bid

        def undo() -> None:
            if self._bid != bid:
                return
            self.store.add_highlight(bid, dict(victim))
            self._hl_ranges[str(victim.get("id"))] = rng
            self._apply_highlights_to_page()
            self._refresh_notes()
            self.show_message(lambda: S("common.undone"))

        self.show_message(lambda: S("status.highlight.removed"), undo=undo, ms=UNDO_MS)

    def delete_active_highlight(self) -> None:
        """Delete: removes the highlight last clicked or created (only then)."""
        if self._active_hl:
            self.remove_highlight(self._active_hl)

    def _jump_to_highlight(self, hid: str, focus_book: bool) -> None:
        h = next((x for x in self._highlights() if str(x.get("id")) == str(hid)), None)
        r = self._hl_ranges.get(str(hid))
        if h is None or r is None:
            return
        self._active_hl = str(hid)
        self._goto(self._hl_spine(h), locator={"gpos": r[0]}, focus=focus_book)

    def _copy_highlight_text(self, hid: str) -> None:
        h = next((x for x in self._highlights() if str(x.get("id")) == str(hid)), None)
        if h is not None:
            QGuiApplication.clipboard().setText(h.get("text") or "")
            self.show_message(lambda: S("status.copied"))

    # ======================================================================
    # copy
    # ======================================================================
    def copy_selection(self) -> None:
        """Ctrl+C (Chromium copies natively while the book has focus; this is for menus)."""
        if not self._page_ready:
            return
        self.view.page().triggerAction(QWebEnginePage.WebAction.Copy)
        if self._last_selection:
            self.show_message(lambda: S("status.copied"))

    def copy_with_citation(self) -> None:
        """Ctrl+Shift+C: the selection followed by 《书名》 and the chapter."""
        if not self._page_ready:
            return

        def got(sel: Any) -> None:
            if not isinstance(sel, dict) or not sel.get("text"):
                self.show_message(lambda: S("status.no_selection"))
                return
            text = str(sel["text"]).strip()
            chapter = self._chapter_title()
            if chapter:
                out = S("cite.chapter", text=text, title=self._book_title(), chapter=chapter)
            else:
                out = S("cite.book", text=text, title=self._book_title())
            QGuiApplication.clipboard().setText(out)
            self.show_message(lambda: S("status.copied_cite"))

        self.host.call_reader("selectionInfo", callback=got)

    def _page_context_menu(self, global_pos: QPoint) -> None:
        if not self._page_ready:
            return

        def got(sel: Any) -> None:
            if not isinstance(sel, dict) or not sel.get("text"):
                return
            t = self.theme
            menu = QMenu(self)
            menu.setObjectName("er-selection-menu")
            menu.addAction(S("sel.copy"), self.copy_selection)
            menu.addAction(S("sel.copy_cite"), self.copy_with_citation)
            menu.addSeparator()
            for color in theme_mod.HIGHLIGHT_COLORS:
                act = menu.addAction(swatch_icon(t.highlight(color), t.ink(color), 16),
                                     S(f"sel.highlight.{color}"))
                act.triggered.connect(lambda _c=False, c=color: self.highlight_selection(c))
            menu.addAction(S("sel.note"), self.highlight_with_note)
            menu.addSeparator()
            q = _collapse(str(sel["text"]))
            shown = menu.fontMetrics().elidedText(q, Qt.TextElideMode.ElideRight, 180)
            menu.addAction(S("sel.search", q=shown), lambda: self._search_for(q))
            menu.exec(global_pos)

        self.host.call_reader("selectionInfo", callback=got)

    def _search_for(self, text: str) -> None:
        self.focus_search()
        self.dock.search.edit.setText(text)
        self.run_search(text)

    # ======================================================================
    # search
    # ======================================================================
    def _hit_chapter(self, hit: SearchHit) -> str:
        return hit.chapter_title or ""

    def run_search(self, query: str) -> None:
        """Book-wide search via epublib.search; paints this chapter's matches."""
        q = (query or "").strip()
        self._search_query = q
        self._search_index = -1
        hits: list[SearchHit] = []
        if q and self._book is not None:
            self.dock.search.set_running()
            hits = self._book.search(q, limit=SEARCH_LIMIT)
        self._search_hits = hits
        self.dock.search.set_results(q, hits, self._hit_chapter)
        self._paint_search_matches()

    def _chapter_ranges(self, spine: int) -> tuple[list[dict], list[int]]:
        ranges, idx = [], []
        for i, h in enumerate(self._search_hits):
            if h.spine_index == spine:
                ranges.append({"gpos": h.gpos, "length": h.length})
                idx.append(i)
        return ranges, idx

    def _paint_search_matches(self) -> None:
        if not self._page_ready:
            return
        ranges, idx = self._chapter_ranges(self._cur_spine)
        if not ranges:
            self.host.call_reader("clearMatches")
            return
        active = idx.index(self._search_index) if self._search_index in idx else -1
        self.host.call_reader("showMatches", ranges, -1 if active < 0 else active)

    def _show_search_active(self, i: int) -> None:
        if not (0 <= i < len(self._search_hits)):
            return
        hit = self._search_hits[i]
        ranges, idx = self._chapter_ranges(hit.spine_index)
        local = idx.index(i) if i in idx else -1
        self.dock.search.set_active(i)
        if local < 0 or not self._page_ready:
            return
        serial = self._load_serial
        self._search_serial += 1
        s2 = self._search_serial
        self.host.call_reader("showMatches", ranges, local)

        def flash(active: int) -> None:
            if serial == self._load_serial and s2 == self._search_serial and self._page_ready:
                self.host.call_reader("showMatches", ranges, active)

        QTimer.singleShot(170, lambda: flash(-1))
        QTimer.singleShot(340, lambda: flash(local))

    def _goto_search_hit(self, i: int, *, focus_book: bool = False) -> None:
        if not (0 <= i < len(self._search_hits)) or self._book is None:
            return
        first = self._search_index < 0
        self._search_index = i
        hit = self._search_hits[i]
        self._goto(hit.spine_index, search_index=i, push_history=first, focus=focus_book)

    def _search_step(self, d: int) -> None:
        hits = self._search_hits
        if not hits:
            return
        n = len(hits)
        if self._search_index < 0:
            cur = (self._cur_spine, int(self._state.get("gpos") or 0))
            if d > 0:
                i = next((k for k, h in enumerate(hits) if (h.spine_index, h.gpos) >= cur), 0)
            else:
                i = next((k for k in range(n - 1, -1, -1) if (hits[k].spine_index, hits[k].gpos) < cur), n - 1)
        else:
            i = self._search_index + d
            if i >= n:
                i = 0
                self.show_message(lambda: S("search.wrapped.start"))
            elif i < 0:
                i = n - 1
                self.show_message(lambda: S("search.wrapped.end"))
        self._goto_search_hit(i)

    def find_next(self) -> None:
        """F3 / n."""
        if not self._search_hits:
            text = self.dock.search.edit.text().strip()
            if text:
                self.run_search(text)
            else:
                self.focus_search()
                return
        self._search_step(1)

    def find_prev(self) -> None:
        """Shift+F3 / N."""
        if not self._search_hits:
            text = self.dock.search.edit.text().strip()
            if text:
                self.run_search(text)
            else:
                self.focus_search()
                return
        self._search_step(-1)

    def clear_search(self) -> None:
        self.dock.search.edit.clear()
        self.run_search("")

    def _on_toc_jump(self, flat_index: int, focus_book: bool) -> None:
        entries = self.dock.toc.entries
        if not (0 <= flat_index < len(entries)) or self._book is None:
            return
        target = flat_index
        while target < len(entries) and entries[target]["spine"] is None:
            target += 1                 # a grouping header: go to its first real child
        if target >= len(entries) or entries[target]["depth"] < entries[flat_index]["depth"]:
            return
        e = entries[target]
        self._toc_current = target
        self.dock.toc.set_current(target)
        self._goto(int(e["spine"]), fragment=e["fragment"], at=None if e["fragment"] else "start",
                   focus=focus_book)

    # ======================================================================
    # settings, typography, theme
    # ======================================================================
    def _effective_settings(self) -> dict:
        s = self.store.reader_settings(self._bid or None)
        s.update(self._pending_overrides)
        return s

    def _page_settings(self) -> dict:
        s = self._effective_settings()
        s.update(theme_mod.page_colors(self.theme))
        if self._zen:
            s["page_margin_px"] = int(min(200, max(96, int(s.get("page_margin_px") or 64) * 1.5)))
        return s

    def _schedule_apply(self) -> None:
        self._apply_timer.start()

    def _apply_page_settings_now(self) -> None:
        if self._page_ready:
            self.host.call_reader("applySettings", self._page_settings())

    def _load_settings_panel(self) -> None:
        self.settings_panel.load(
            self._effective_settings(),
            theme_choice=self.theme_controller.choice,
            this_book=self._this_book,
            has_book=self._book is not None,
            speed=float(self.store.get("behavior.reading_speed_units_per_min", SPEED_SEED) or SPEED_SEED),
            autohide=self._auto_hide_ms() > 0,
            restore_last=bool(self.store.get("behavior.restore_last_book_on_launch", True)),
            confirm_remove=bool(self.store.get("behavior.confirm_remove_from_shelf", True)),
        )

    def set_typography(self, key: str, value: Any) -> None:
        """Change one ``reader.*`` typography key, globally or for this book only."""
        if key not in TYPOGRAPHY_KEYS:
            self.store.set(f"reader.{key}", value)
        elif self._this_book and self._bid:
            self._pending_overrides[key] = value
            self._override_timer.start()
        else:
            self.store.set(f"reader.{key}", value)
        self._schedule_apply()
        if self.settings_panel.isVisible():
            self._load_settings_panel()

    def _flush_overrides(self) -> None:
        self._override_timer.stop()
        if not self._pending_overrides or not self._bid:
            self._pending_overrides = {}
            return
        pending, self._pending_overrides = self._pending_overrides, {}
        state = self.store.book_state(self._bid)
        with self.store.lock:
            state.setdefault("overrides", {}).update(pending)
        self.store.save_book_state(self._bid, state, immediate=True)

    def _on_setting(self, key: str, value: Any) -> None:
        if key == "reader.theme":
            self.set_theme_choice(str(value))
        elif key == "ui.language":
            self.store.set("ui.language", value)
            strings.set_language(value)
            self.settings_panel.retranslate_ui()
        elif key == "behavior.autohide":
            self.store.set("behavior.auto_hide_chrome_ms", 3000 if value else 0)
            self._restart_hide()
        elif key.startswith("behavior."):
            self.store.set(key, value)
        elif key in ("reader.image_click_zoom", "reader.invert_images_in_dark"):
            self.store.set(key, bool(value))
            self._schedule_apply()
        elif key.startswith("reader."):
            self.set_typography(key[len("reader."):], value)
        elif key == "thisbook":
            self.set_this_book_only(bool(value))
        elif key == "reset":
            self.reset_typography()

    def set_this_book_only(self, on: bool) -> None:
        """仅用于本书.  Unchecking deletes the book's overrides; it snaps back to global."""
        if on:
            self._this_book = bool(self._bid)
        else:
            self._pending_overrides = {}
            self._override_timer.stop()
            if self._bid:
                self.store.clear_overrides(self._bid)
            self._this_book = False
            self._schedule_apply()
        self._load_settings_panel()

    def reset_typography(self) -> None:
        """恢复默认: the book's overrides (this-book mode) or the global typography."""
        if self._this_book and self._bid:
            self._pending_overrides = {}
            self._override_timer.stop()
            self.store.clear_overrides(self._bid)
        else:
            defaults = store_mod.DEFAULT_SETTINGS["reader"]
            for k in TYPOGRAPHY_KEYS:
                if k in defaults:
                    self.store.set(f"reader.{k}", defaults[k])
        self._schedule_apply()
        self._load_settings_panel()
        self.show_message(lambda: S("set.reset.done"))

    def _font_size(self) -> int:
        try:
            return int(self._effective_settings().get("font_size_px") or 21)
        except (TypeError, ValueError):
            return 21

    def _step_font(self, d: int) -> None:
        cur = self._font_size()
        new = int(_clamp(cur + d, FONT_SIZE_MIN, FONT_SIZE_MAX))
        if new != cur:
            self.set_typography("font_size_px", new)
        self.show_message(lambda: S("status.font_size", n=new))

    def font_larger(self) -> None:
        """Ctrl+= (and Ctrl+wheel up)."""
        self._step_font(1)

    def font_smaller(self) -> None:
        """Ctrl+- (and Ctrl+wheel down)."""
        self._step_font(-1)

    def font_reset(self) -> None:
        """Ctrl+0."""
        default = int(store_mod.DEFAULT_SETTINGS["reader"]["font_size_px"])
        self.set_typography("font_size_px", default)
        self.show_message(lambda: S("status.font_size", n=default))

    def toggle_layout_mode(self) -> None:
        """Ctrl+M: paginated / scrolling."""
        new = "paged" if self._effective_settings().get("layout") == "scroll" else "scroll"
        self.set_typography("layout", new)
        self.show_message(lambda: S("status.mode.scroll" if new == "scroll" else "status.mode.paged"))

    def set_theme_choice(self, choice: str) -> None:
        """light / sepia / dark / system, for chrome and page alike."""
        choice = store_mod.normalize_theme_choice(choice)
        self.store.set("reader.theme", choice)
        t = self.theme_controller.set_choice(choice)
        if t is not self.theme:
            self.apply_theme(t)
        self.settings_panel.set_theme_choice(choice)
        self.show_message(lambda: S("status.theme", theme=strings.theme_label(choice)))

    def toggle_day_night(self) -> None:
        """Ctrl+Shift+D."""
        self.set_theme_choice("light" if self.theme.is_dark else "dark")

    def apply_theme(self, t: theme_mod.Theme | None = None, *, force: bool = False) -> None:
        """Apply a concrete theme to the chrome parts drawn here and to the page."""
        if t is None:
            t = self.theme_controller.theme
        if t is self.theme and not force:
            return
        self.theme = t
        try:
            self.host.set_background_color(t.bg)
            self.host.set_theme_css(theme_mod.css_text(t))
        except RuntimeError:
            pass
        self.toolbar.apply_theme(t)
        self.dock.apply_theme(t)
        self.settings_panel.apply_theme(t)
        self.settings_panel.set_theme_choice(self.theme_controller.choice)
        self.note_editor.apply_theme(t)
        self.error_card.apply_theme(t)
        self.cheatsheet.update()
        self._schedule_apply()
        self.update()

    # ======================================================================
    # language
    # ======================================================================
    def retranslate_ui(self) -> None:
        """Re-read every visible string (subscribed to strings.language_changed)."""
        self.toolbar.retranslate_ui()
        self.statusbar.retranslate_ui()
        self.dock.retranslate_ui()
        self.settings_panel.retranslate_ui()
        for pop in self.column.popovers:
            pop.retranslate_ui()
        self.error_card.retranslate_ui()
        self.cheatsheet.retranslate_ui()
        self.column.loading.setText(S("common.loading"))
        self._update_status()
        self._emit_title()
        self.column.layout_overlays()

    def _emit_title(self) -> None:
        if self._book is not None:
            self.titleChanged.emit(S("title.book", title=self._book_title()))
        elif self._error_spec is not None:
            name = self._error_spec.get("title") or os.path.splitext(
                os.path.basename(self._error_spec.get("path") or ""))[0]
            self.titleChanged.emit(S("title.book", title=name) if name else S("title.library"))
        else:
            self.titleChanged.emit(S("title.library"))

    def _show_loading(self) -> None:
        if self._book is not None and not self._page_ready:
            self.column.loading.setText(S("common.loading"))
            self.column.loading.show()
            self.column.loading.raise_()

    # ======================================================================
    # errors, missing files, relocation
    # ======================================================================
    def _show_error(self, spec: dict) -> None:
        self._error_spec = dict(spec)
        self.error_card.show_spec(self._error_spec)
        page = self.column.error_page
        page.show()
        page.raise_()
        self.toolbar.raise_()
        self.statusbar.raise_()
        self.toolbar.set_book_actions_enabled(False)
        name = spec.get("title") or os.path.splitext(os.path.basename(spec.get("path") or ""))[0]
        self.toolbar.set_title(name, "")
        self.statusbar.set_cells("", None, None)
        hide_quietly(self.dock)
        hide_quietly(self.settings_panel)
        self.reveal_chrome()
        self._emit_title()

    def _hide_error(self) -> None:
        if self._error_spec is not None:
            self._error_spec = None
            hide_quietly(self.column.error_page)

    def error_spec(self) -> dict | None:
        """The error card on screen, or None (tests and owner G)."""
        return dict(self._error_spec) if self._error_spec else None

    def _library_entry_for_path(self, path: str) -> dict | None:
        key = os.path.normcase(os.path.abspath(path))
        for e in self.store.library():
            p = e.get("path")
            if p and os.path.normcase(os.path.abspath(p)) == key:
                return e
        return None

    def _missing(self, path: str, entry: dict | None) -> None:
        title = (entry or {}).get("title") or ""
        base = {"path": path, "title": title, "in_library": entry is not None,
                "book_id": (entry or {}).get("id") or ""}
        if entry is None or not entry.get("size") or not entry.get("id"):
            self._show_error({"kind": "missing", **base})
            return
        self._show_error({"kind": "searching", **base})
        folders = [os.path.dirname(path)]
        folders += [os.path.dirname(p) for p in entry.get("path_history") or [] if p]
        folders += [os.path.dirname(e.get("path") or "") for e in self.store.library() if e.get("path")]
        self._worker_serial += 1
        serial = self._worker_serial
        size, bid = int(entry["size"]), str(entry["id"])
        relay = self._relay

        def work() -> None:
            try:
                found = find_moved_file(path, size, bid, folders)
            except Exception:  # noqa: BLE001
                found = None
            try:
                relay.done.emit(("moved", serial, path, entry, found))
            except RuntimeError:
                pass

        threading.Thread(target=work, name="book-reader-find-moved", daemon=True).start()

    def _on_worker_done(self, payload: Any) -> None:
        kind, serial, path, entry, found = payload
        if kind != "moved" or serial != self._worker_serial or self._error_spec is None \
                or self._error_spec.get("path") != path:
            return
        if found:
            self._repair_path(entry, found)
            self.open_book(found)
            if self._book is not None:
                self.show_message(lambda: S("status.moved"))
            return
        self.store.library_update(str(entry.get("id")), missing=True)
        spec = dict(self._error_spec)
        spec["kind"] = "missing"
        self._show_error(spec)

    def _repair_path(self, entry: dict, new_path: str, *, keep_id: bool = True) -> None:
        old = entry.get("path") or ""
        history = list(entry.get("path_history") or [])
        if old and old not in history:
            history.append(old)
        st = os.stat(new_path)
        self.store.library_upsert({"id": entry["id"], "path": os.path.abspath(new_path),
                                   "path_history": history, "size": st.st_size,
                                   "mtime_ns": st.st_mtime_ns, "missing": False}, immediate=True)

    def _relocate_dialog(self) -> None:
        spec = self._error_spec or {}
        start = os.path.dirname(spec.get("path") or "") or os.path.expanduser("~")
        if not os.path.isdir(start):
            start = os.path.expanduser("~")
        title = spec.get("title") or os.path.basename(spec.get("path") or "")
        path, _ = QFileDialog.getOpenFileName(
            self, S("dlg.relocate.title", title=title), start,
            f"{S('dlg.open.filter')};;{S('dlg.all_files')}")
        if path:
            self.relocate_to(path)

    def relocate_to(self, new_path: str) -> None:
        """重新定位: link the missing book to *new_path* (asks inline if the hash differs)."""
        spec = self._error_spec or {}
        entry = self.store.library_get(spec.get("book_id") or "") if spec.get("book_id") else None
        try:
            new_id = store_mod.book_id(new_path)
        except OSError as exc:
            self._show_error(self._spec("access", new_path, entry, exc))
            return
        if entry is None:
            self.open_book(new_path)
            return
        if new_id == entry.get("id"):
            self._repair_path(entry, new_path)
            self.open_book(new_path)
            return
        self._relocate_new = new_path
        self.error_card.set_confirm(new_path)

    def _link_anyway(self) -> None:
        spec = self._error_spec or {}
        new_path = self._relocate_new
        entry = self.store.library_get(spec.get("book_id") or "") if spec.get("book_id") else None
        if not new_path or entry is None:
            return
        # Keep the old id: store.book_id_for() finds it again through the library
        # entry's (path, size, mtime_ns), so bookmarks and highlights follow the file.
        self._repair_path(entry, new_path)
        self.open_book(new_path)

    def _on_error_action(self, name: str) -> None:
        spec = self._error_spec or {}
        path = spec.get("path") or ""
        if name == "reveal":
            self._reveal_in_folder(path)
        elif name == "remove":
            bid = spec.get("book_id") or ""
            if bid:
                self.store.library_remove(bid)
                self.bookRemoved.emit(bid)
            self._hide_error()
            self.backToLibrary.emit()
        elif name == "close":
            self.close_book()
            self.backToLibrary.emit()
        elif name == "relocate":
            self._relocate_dialog()
        elif name == "link_yes":
            self._link_anyway()
        elif name == "link_no":
            self._relocate_new = None
            self.error_card.set_confirm(None)

    @staticmethod
    def _reveal_in_folder(path: str) -> None:
        if not path:
            return
        native = os.path.normpath(path)
        if os.path.exists(native):
            proc = QProcess()
            proc.setProgram("explorer.exe")
            if hasattr(proc, "setNativeArguments"):
                proc.setNativeArguments(f'/select,"{native}"')
            else:  # pragma: no cover - non-Windows
                proc.setArguments(["/select,", native])
            proc.startDetached()
        elif os.path.isdir(os.path.dirname(native)):
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(native)))

    # ======================================================================
    # book details and export
    # ======================================================================
    def show_book_info(self) -> None:
        """书籍信息 as an in-pane card."""
        book = self._book
        if book is None:
            return
        md = book.metadata or {}
        entry = self.store.library_get(self._bid) or {}
        authors = list(md.get("authors") or [])
        size = int(entry.get("size") or 0)

        def data_size() -> str:
            from PySide6.QtCore import QLocale

            return QLocale(strings.QT_LOCALE_NAMES[strings.current_language()]).formattedDataSize(size)

        rows: list[tuple[Callable[[], str], Callable[[], str]]] = [
            (lambda: S("info.book"), lambda: self._book_title()),
            (lambda: S("info.authors.one" if len(authors) == 1 else "info.authors.other"),
             lambda: S("common.sep").join(authors)),
            (lambda: S("info.publisher"), lambda: md.get("publisher") or ""),
            (lambda: S("info.pubdate"), lambda: md.get("date") or ""),
            (lambda: S("info.language"), lambda: md.get("language") or ""),
            (lambda: S("info.identifier"), lambda: md.get("identifier") or ""),
            (lambda: S("info.epub_version"), lambda: getattr(book, "version", "") or ""),
            (lambda: S("info.layout"),
             lambda: S("info.layout.fixed") if book.is_fixed_layout else S("info.layout.reflowable")),
            (lambda: S("info.chapters"), lambda: str(len(book.spine))),
            (lambda: S("info.units"), lambda: S("info.units.value", n=f"{self._total_units:,}")),
            (lambda: S("info.size"), lambda: data_size() if size else ""),
            (lambda: S("info.path"), lambda: self._path),
            (lambda: S("info.added"), lambda: format_date(entry.get("added_at"), relative=False)),
            (lambda: S("info.progress"), lambda: S("status.percent", p=_percent_int(self._book_fraction()))),
            (lambda: S("info.reading_time"),
             lambda: duration((self._base_seconds + self._tracker.total_seconds) / 60.0, long=True)),
        ]
        self.reveal_chrome()
        self.book_info.show_rows(rows)

    def export_markdown(self) -> str:
        """The highlights (and bookmarks) of the open book as Markdown text."""
        book = self._book
        if book is None:
            return ""
        md = book.metadata or {}
        sep = S("common.sep")
        lines = [f"# {self._book_title()}", ""]
        if md.get("authors"):
            lines += [S("export.md.author", author=sep.join(md["authors"])), ""]
        lines += [S("export.md.exported", date=format_date(_dt.date.today(), relative=False)), ""]
        hls = self._highlights()
        lost = {hid for hid, r in self._hl_ranges.items() if r is None}

        def order(h: dict) -> tuple:
            r = self._hl_ranges.get(str(h.get("id")))
            return (self._hl_spine(h), r[0] if r else int((h.get("start") or {}).get("gpos") or 0))

        if hls:
            lines += [f"## {S('export.md.highlights')}", ""]
            last = None
            for h in sorted(hls, key=order):
                i = self._hl_spine(h)
                if i != last:
                    last = i
                    title = next((e["title"] for e in self.dock.toc.entries if e["spine"] == i and e["title"]),
                                 "") or h.get("chapter_title") or book.doc_title(book.spine[i].zip_name)
                    lines += [f"### {title}", ""]
                quote = [ln.strip() for ln in (h.get("text") or "").splitlines() if ln.strip()]
                lines += [f"> {ln}" for ln in quote] or ["> "]
                lines.append("")
                meta = S("export.md.position", p=_percent_int(h.get("book_progress") or 0.0))
                if str(h.get("id")) in lost:
                    meta += " " + S("export.md.lost")
                lines.append(meta)
                if (h.get("note") or "").strip():
                    lines.append("")
                    lines.append(S("export.md.note", note=h["note"].strip()))
                lines.append("")
        marks = sorted(self._bookmarks(), key=lambda b: (self._spine_for_position(b),
                                                          int((b.get("locator") or {}).get("gpos") or 0)))
        if marks:
            lines += [f"## {S('export.md.bookmarks')}", ""]
            for bm in marks:
                meta = S("bookmarks.meta", percent=_percent_int(bm.get("book_progress") or 0.0),
                         date=format_date(bm.get("created_at"), relative=False))
                lines.append(f"- {bm.get('chapter_title') or S('toc.untitled')}{sep}{meta}")
                if bm.get("text"):
                    lines.append(f"  > {_collapse(bm['text'])}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def source_format(self) -> str:
        """'epub', 'mobi', 'djvu'…: what the open book's file really is ('' with no book)."""
        book = getattr(self, "_book", None)          # the toolbar asks while the page is still being built
        return str(getattr(book, "source_format", "epub") or "epub") if book is not None else ""

    def convert_book(self) -> Any:
        """转换为 LaTeX 和 PDF… (DjVu: 转换为 PDF…) for the open book.  Returns the running job."""
        if self._book is None:
            return None
        import convert_dialog
        return convert_dialog.start_conversion(self, path=self._path, title=self._book_title(),
                                               source_format=self.source_format(), store=self.store,
                                               content_key=self._bid or None)

    def export_highlights(self, path: str | None = None) -> str | None:
        """导出为 Markdown….  With no *path* a save dialog asks.  Returns the path written."""
        if self._book is None:
            return None
        if not self._highlights():
            self.show_message(lambda: S("export.empty"))
            return None
        if path is None:
            from PySide6.QtCore import QStandardPaths

            base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
            name = re.sub(r'[\\/:*?"<>|]+', "_", S("export.default_name", title=self._book_title()))
            path, _ = QFileDialog.getSaveFileName(self, S("export.dialog_title"),
                                                  os.path.join(base, name + ".md"), S("export.filter"))
            if not path:
                return None
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(self.export_markdown())
        except OSError:
            self.notice.show_notice(lambda: S("export.failed"), path)
            return None
        n = len(self._highlights())
        self.show_message(lambda: plural("export.done", n))
        return path

    # ======================================================================
    # events
    # ======================================================================
    def _in_view(self, obj: QObject) -> bool:
        return isinstance(obj, QWidget) and (obj is self.view or self.view.isAncestorOf(obj))

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        et = event.type()
        if et == QEvent.Type.Wheel:
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier and self._in_view(obj):
                dy = event.angleDelta().y()
                if dy > 0:
                    self.font_larger()
                elif dy < 0:
                    self.font_smaller()
                return True
        elif et == QEvent.Type.MouseButtonRelease:
            if event.button() == Qt.MouseButton.RightButton and self._in_view(obj):
                self._page_context_menu(QCursor.pos())
        elif et == QEvent.Type.WindowDeactivate:
            if obj is self.window() and self._book is not None:
                self._capture_and_save(True)
        elif et == QEvent.Type.WindowStateChange:
            if obj is self.window():
                QTimer.singleShot(0, self._sync_fullscreen)
        return False

    def resizeEvent(self, event: Any) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self.cheatsheet.isVisible():
            self.cheatsheet.setGeometry(self.rect())


def _json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
