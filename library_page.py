"""library_page.py — the EPUB Reader shelf (owner F).

Implements CONTRACT.md §7 and product-spec §3 "LIBRARY / START SCREEN":

* a top bar (Open Book…, Add Folder…, search, sort, cover/list toggle);
* a 「继续阅读」 row of up to six books with ``0 < progress < 1`` (omitted when empty);
* the 「全部书籍」 flow grid: 160x240 covers, two-line elided titles, secondary-colour
  authors, and a 3px progress bar flush to the cover bottom only when progress > 0;
* generated fallback covers (flat, hue = hash(book_id) mod 360 at low saturation);
* missing files at 55% opacity with a 「文件缺失」 badge;
* the right-click menu (open / show in folder / copy path / book details / ─ /
  remove from library).  There is **no** delete-file item anywhere: the page
  never deletes, moves, copies or modifies a book file;
* drag-and-drop of files and folders anywhere on the page;
* the three-line empty state;
* a dismissible found-books suggestion card that never adds anything silently.

Cover thumbnails are made with Pillow (LANCZOS to 360px wide, long edge at most
720px, JPEG q86) into ``Store.cover_path()`` by a ``QThreadPool`` worker, so
the UI never blocks and the grid fills in as covers arrive.  SVG covers are
rasterised with QtSvg first.

All text comes from :mod:`strings`; every widget implements ``retranslate_ui()``
and the page subscribes to ``strings.language_changed`` so the UI language
switches live.

The page persists through :class:`store.Store` only:

=================================  ==========================================
``window.library_view``            ``'grid'`` or ``'list'``
``window.library_sort``            one of :data:`SORT_KEYS`
``behavior.watch_folders``         folders added with Add Folder… (F5 rescans)
``behavior.confirm_remove_from_shelf``  the removal confirmation
``behavior.found_books_dismissed`` folders whose suggestion card got "No Thanks"
=================================  ==========================================
"""

from __future__ import annotations

import copy
import functools
import html
import io
import logging
import os
import re
import subprocess
import sys
import threading
import unicodedata
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable

from PySide6.QtCore import (
    QBuffer, QByteArray, QCollator, QCoreApplication, QEvent, QIODevice, QLocale,
    QObject, QPoint, QPointF, QRect, QRectF, QSize, Qt, QThreadPool, QTimer, QUrl,
    Signal,
)
from PySide6.QtGui import (
    QAction, QColor, QDesktopServices, QFont, QFontDatabase, QFontMetricsF,
    QGuiApplication, QIcon, QImage, QKeyEvent, QPainter, QPainterPath, QPen,
    QPixmap, QTextLayout, QTextOption,
)
from PySide6.QtWidgets import (
    QAbstractScrollArea, QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog,
    QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMenu, QPlainTextEdit, QPushButton,
    QSizePolicy, QStackedLayout, QToolButton, QToolTip, QVBoxLayout, QWidget,
)

import strings
import theme as theme_mod
from epublib import EpubBook, EpubError
from store import Store, now_iso
from strings import S, duration, format_date, language_changed, plural, tip

__all__ = [
    "LibraryPage", "ShelfView", "BookInfoDialog", "ConfirmRemoveDialog",
    "COVER_SIZE", "THUMB_WIDTH", "THUMB_MAX_EDGE", "THUMB_JPEG_QUALITY",
    "SORT_KEYS", "SORT_LABEL_KEYS", "VIEW_MODES", "CONTINUE_LIMIT", "MISSING_OPACITY", "PROGRESS_BAR_PX",
    "SETTING_VIEW", "SETTING_SORT", "SETTING_FOLDERS", "SETTING_CONFIRM_REMOVE",
    "SETTING_FOUND_DISMISSED",
    "cover_hue", "generated_cover_colors", "paint_generated_cover",
    "make_thumbnail", "fit_cover_image", "read_library_entry",
    "display_title", "display_authors", "book_progress",
    "sort_entries", "filter_entries", "continue_entries",
    "is_epub_path", "collect_epubs", "wrap_text",
]

_log = logging.getLogger("epub_reader.library")

# ==========================================================================
# constants
# ==========================================================================

#: Logical size of a cover box on the shelf (product spec §3).
COVER_SIZE: tuple[int, int] = (160, 240)
#: Cached thumbnails are LANCZOS-resized to this width ...
THUMB_WIDTH: int = 360
#: ... with the long edge capped here (product spec §4 cache note).
THUMB_MAX_EDGE: int = 720
THUMB_JPEG_QUALITY: int = 86

SORT_KEYS: tuple[str, ...] = ("recent", "added", "title", "author", "progress")
SORT_LABEL_KEYS: dict[str, str] = {k: f"lib.sort.{k}" for k in SORT_KEYS}
VIEW_MODES: tuple[str, ...] = ("grid", "list")
CONTINUE_LIMIT: int = 6
MISSING_OPACITY: float = 0.55
PROGRESS_BAR_PX: int = 3

SETTING_VIEW = "window.library_view"
SETTING_SORT = "window.library_sort"
SETTING_FOLDERS = "behavior.watch_folders"
SETTING_CONFIRM_REMOVE = "behavior.confirm_remove_from_shelf"
SETTING_FOUND_DISMISSED = "behavior.found_books_dismissed"

# grid geometry (logical px)
_MARGIN_X = 32
_TOP = 20
_HEADER_H = 44
_SECTION_GAP = 26
_GAP_X_MIN = 28
_GAP_X_MAX = 48
_GAP_Y = 30
_BOTTOM = 48
_SCROLLBAR_RESERVE = 10
# list geometry
_ROW_H = 64
_LIST_HEADER_H = 32
_MINI_W, _MINI_H = 32, 48

# crop covers whose aspect ratio is this close to the box; letterbox the rest
_CROP_TOLERANCE = 0.10

_SKIP_DIRS = {"$recycle.bin", "system volume information", "__macosx"}
_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")


# ==========================================================================
# small pure helpers
# ==========================================================================

def _clean(text: Any) -> str:
    """Collapse whitespace; ``None`` becomes ``''``."""
    if text is None:
        return ""
    return _WS_RE.sub(" ", str(text)).strip()


def _fold(text: str) -> str:
    """Search folding: NFKC (full-width Latin = half-width) and casefold."""
    return unicodedata.normalize("NFKC", text).casefold()


def _ts(value: Any) -> float:
    """ISO timestamp (as store.py writes it) or epoch number -> epoch seconds; 0 if absent."""
    if not value:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (ValueError, OverflowError, OSError):
        return 0.0


def _norm_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(path)) if path else ""


def is_epub_path(path: str) -> bool:
    """True for a name ending in ``.epub`` (any case)."""
    return str(path).lower().endswith(".epub")


def display_title(entry: dict) -> str:
    """The title to show for a library entry.

    The metadata title, else the file name without ``.epub``, else ``''`` (the
    caller renders ``S("lib.title.unknown")`` for that).
    """
    title = _clean(entry.get("title"))
    if title:
        return title
    path = entry.get("path") or ""
    return _clean(os.path.splitext(os.path.basename(path))[0])


def _author_list(entry: dict) -> list[str]:
    authors = entry.get("authors") or []
    if isinstance(authors, str):
        authors = [authors]
    return [a for a in (_clean(x) for x in authors) if a]


def display_authors(entry: dict) -> str:
    """Authors joined with ``S("common.sep")``; ``''`` when unknown."""
    return S("common.sep").join(_author_list(entry))


def book_progress(entry: dict) -> float:
    """Reading progress clamped to 0..1 (bad values count as 0)."""
    try:
        value = float(entry.get("progress") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if value != value:  # NaN
        return 0.0
    return min(1.0, max(0.0, value))


def _is_finished(entry: dict) -> bool:
    return bool(entry.get("finished_at")) or book_progress(entry) >= 1.0


def _percent(progress: float) -> int:
    """34.1% -> 34, but never shows 0 for a started book or 100 for an unfinished one."""
    p = round(progress * 100)
    if progress > 0:
        p = max(1, p)
    if progress < 1:
        p = min(99, p)
    return int(p)


def filter_entries(entries: Iterable[dict], query: str) -> list[dict]:
    """Entries whose title, authors or file name contain every whitespace-separated term.

    Case-insensitive and width-insensitive (NFKC), so ``ＥＰＵＢ`` finds ``EPUB``.
    """
    terms = _fold(query or "").split()
    out = []
    for e in entries:
        if not terms:
            out.append(e)
            continue
        hay = _fold(" ".join([
            display_title(e), _clean(e.get("subtitle")), " ".join(_author_list(e)),
            # the file name without ".epub" (with it, every book would match "epub")
            os.path.splitext(os.path.basename(e.get("path") or ""))[0],
        ]))
        if all(t in hay for t in terms):
            out.append(e)
    return out


def continue_entries(entries: Iterable[dict], limit: int = CONTINUE_LIMIT) -> list[dict]:
    """Up to *limit* books with ``0 < progress < 1``, most recently opened first."""
    started = [e for e in entries if 0.0 < book_progress(e) < 1.0 and not e.get("finished_at")]
    started.sort(key=lambda e: (-_ts(e.get("opened_at")), -_ts(e.get("added_at"))))
    return started[:limit]


#: QCollator locale per UI language.  English uses zh_CN too: it sorts Latin
#: A-Z exactly like en_US, and orders Chinese titles by pinyin instead of by
#: code point (verified on this machine: en_US put 中 before 从 before 阿).
_COLLATION_LOCALES = {"zh-Hans": "zh_CN", "zh-Hant": "zh_TW", "ja": "ja_JP", "en": "zh_CN"}


def _collator(lang: str | None) -> QCollator:
    loc = _COLLATION_LOCALES.get(lang or "", "zh_CN")
    coll = QCollator(QLocale(loc))
    coll.setNumericMode(True)       # "Book 9" before "Book 10"
    coll.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
    return coll


def _sort_text(text: str) -> str:
    """Drop leading punctuation/symbols so 《红楼梦》 sorts under 红 and "Emma" under E."""
    i = 0
    while i < len(text) and unicodedata.category(text[i])[0] in "PSZC":
        i += 1
    return text[i:]


def sort_entries(entries: Iterable[dict], key: str, lang: str | None = None) -> list[dict]:
    """Sort library entries for display.

    ``recent``   last opened first; never-opened books follow, newest added first.
    ``added``    newest added first.
    ``title``    collated for the UI language: pinyin for zh-Hans/en, stroke order for
                 zh-Hant, Japanese order for ja.  Latin A-Z sorts before CJK; digits use
                 numeric order; leading brackets and quotes are ignored; untitled last.
    ``author``   first author, same collation; unknown authors last; then title.
    ``progress`` furthest along first; ties by most recently opened.
    """
    items = list(entries)
    lang = lang or strings.current_language()
    cmp = functools.cmp_to_key(_collator(lang).compare)

    def title_k(e: dict) -> Any:
        t = _sort_text(display_title(e))
        return (not t, cmp(t))

    def recent_k(e: dict) -> Any:
        opened = _ts(e.get("opened_at"))
        return (opened == 0.0, -opened, -_ts(e.get("added_at")))

    if key == "title":
        items.sort(key=lambda e: cmp(_sort_text(" ".join(_author_list(e)))))
        items.sort(key=title_k)
    elif key == "author":
        items.sort(key=title_k)
        items.sort(key=lambda e: (not _author_list(e),
                                  cmp(_sort_text(" ".join(_author_list(e))))))
    elif key == "added":
        items.sort(key=title_k)
        items.sort(key=lambda e: -_ts(e.get("added_at")))
    elif key == "progress":
        items.sort(key=recent_k)
        items.sort(key=lambda e: -book_progress(e))
    else:  # recent (default)
        items.sort(key=recent_k)
    return items


def collect_epubs(paths: Iterable[str], *, cancel: threading.Event | None = None
                  ) -> tuple[list[str], list[str]]:
    """Expand files and folders (recursively) into ``.epub`` files.

    Returns ``(epubs, unsupported)``: absolute, de-duplicated (case-insensitively
    on Windows) epub paths in a stable order, plus the non-epub *files* that were
    passed in directly.  Hidden folders and the recycle bin are skipped.  Nothing
    is opened or modified.
    """
    seen: set[str] = set()
    epubs: list[str] = []
    unsupported: list[str] = []

    def add(p: str) -> None:
        n = _norm_path(p)
        if n not in seen:
            seen.add(n)
            epubs.append(os.path.abspath(p))

    for raw in paths:
        if cancel is not None and cancel.is_set():
            break
        if not raw:
            continue
        path = os.path.abspath(raw)
        if os.path.isdir(path):
            for root, dirs, files in os.walk(path, onerror=lambda _e: None):
                if cancel is not None and cancel.is_set():
                    break
                dirs[:] = sorted(d for d in dirs
                                 if not d.startswith(".") and d.lower() not in _SKIP_DIRS)
                for name in sorted(files):
                    if is_epub_path(name):
                        full = os.path.join(root, name)
                        if os.path.isfile(full):
                            add(full)
        elif os.path.isfile(path) and is_epub_path(path):
            add(path)
        else:
            unsupported.append(path)
    return epubs, unsupported


def _short_folder(folder: str) -> str:
    """A folder for display in one line: relative to the home folder, else the last parts."""
    folder = os.path.normpath(folder)
    home = os.path.normpath(os.path.expanduser("~"))
    shown = folder
    try:
        if os.path.normcase(folder).startswith(os.path.normcase(home) + os.sep):
            shown = folder[len(home) + 1:]
    except ValueError:
        pass
    parts = shown.split(os.sep)
    if len(shown) > 42 and len(parts) > 2:
        shown = "…" + os.sep + os.sep.join(parts[-2:])
    return shown


def _strip_html(text: str) -> str:
    return _clean(html.unescape(_TAG_RE.sub(" ", text or "")))


# ==========================================================================
# covers: hue, generated covers, thumbnails
# ==========================================================================

def cover_hue(book_id: str) -> int:
    """Hue in 0..359 for a generated cover: ``hash(book_id) mod 360``.

    The book id is already a blake2b hash, so its first 32 bits are used directly;
    a non-hex id falls back to CRC-32.  Python's ``hash()`` is NOT used — it is
    salted per process and the colour would change on every launch.
    """
    try:
        n = int((book_id or "")[:8], 16)
    except ValueError:
        n = zlib.crc32((book_id or "").encode("utf-8"))
    return n % 360


def generated_cover_colors(book_id: str, theme: Any = None) -> dict[str, QColor]:
    """Colours of a generated cover: ``bg``, ``rule``, ``title``, ``author``.

    Flat background at a fixed low saturation; lightness depends on the theme so
    a shelf of generated covers never glares in the dark theme.
    """
    h = cover_hue(book_id) / 360.0
    name = getattr(theme, "name", "light")
    if name == "dark":
        c = {"bg": QColor.fromHslF(h, 0.20, 0.25), "rule": QColor.fromHslF(h, 0.22, 0.46),
             "title": QColor.fromHslF(h, 0.30, 0.90), "author": QColor.fromHslF(h, 0.16, 0.74)}
    elif name == "sepia":
        c = {"bg": QColor.fromHslF(h, 0.22, 0.79), "rule": QColor.fromHslF(h, 0.22, 0.58),
             "title": QColor.fromHslF(h, 0.40, 0.18), "author": QColor.fromHslF(h, 0.22, 0.32)}
    else:
        c = {"bg": QColor.fromHslF(h, 0.28, 0.83), "rule": QColor.fromHslF(h, 0.26, 0.60),
             "title": QColor.fromHslF(h, 0.45, 0.17), "author": QColor.fromHslF(h, 0.28, 0.31)}
    # Equal HSL lightness is not equal luminance (yellows are brighter): walk the
    # text colours away from the background until they reach 7:1 and 4.5:1
    # (verified: a fixed author colour measured 4.10:1 on a yellow sepia cover).
    # (targets carry a small margin: theme.contrast_ratio measures the 8-bit hex)
    c["title"] = _with_contrast(c["title"], c["bg"], 7.15)
    c["author"] = _with_contrast(c["author"], c["bg"], 4.65)
    return c


def _luminance(c: QColor) -> float:
    def ch(v: float) -> float:
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(c.redF()) + 0.7152 * ch(c.greenF()) + 0.0722 * ch(c.blueF())


def _contrast(a: QColor, b: QColor) -> float:
    la, lb = _luminance(a), _luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _with_contrast(fg: QColor, bg: QColor, target: float) -> QColor:
    """*fg* with its HSL lightness moved away from *bg* until the WCAG ratio >= *target*."""
    darker = _luminance(bg) > 0.18
    h, s, l = fg.hslHueF(), fg.hslSaturationF(), fg.lightnessF()
    out = QColor(fg)
    for _ in range(60):
        if _contrast(out, bg) >= target:
            break
        l = max(0.0, l - 0.01) if darker else min(1.0, l + 0.01)
        out = QColor.fromHslF(max(0.0, h), s, l)
    return out


_FONT_FAMILIES_CACHE: set[str] | None = None


def _installed_families() -> set[str]:
    global _FONT_FAMILIES_CACHE
    if _FONT_FAMILIES_CACHE is None:
        try:
            _FONT_FAMILIES_CACHE = set(QFontDatabase.families())
        except Exception:  # no QGuiApplication yet
            return set()
    return _FONT_FAMILIES_CACHE


def _cover_font_family(book_language: str = "") -> str:
    """A CJK-capable family for a generated cover, picked by the BOOK's language."""
    lang = (book_language or "").lower().replace("_", "-")
    if lang.startswith("ja"):
        prefs = ["Yu Gothic UI", "Yu Gothic", "Meiryo", "Microsoft YaHei"]
    elif lang in ("zh-tw", "zh-hk", "zh-mo") or lang.startswith("zh-hant"):
        prefs = ["Microsoft JhengHei", "Microsoft YaHei"]
    else:
        prefs = ["Microsoft YaHei", "DengXian", "SimHei"]
    fams = _installed_families()
    for fam in prefs:
        if fam in fams:
            return fam
    app = QGuiApplication.instance()
    return app.font().family() if app is not None else prefs[-1]


def _utf16_slices(text: str, spans: list[tuple[int, int]]) -> list[str]:
    """Cut *text* at QTextLayout's UTF-16 offsets (astral characters count 2)."""
    raw = text.encode("utf-16-le")
    return [raw[s * 2:(s + n) * 2].decode("utf-16-le", "replace") for s, n in spans]


def wrap_text(text: str, font: QFont, width: float, max_lines: int) -> tuple[list[str], bool]:
    """Wrap *text* into at most *max_lines* lines of *width* px; elide the last one.

    Uses QTextLayout, so CJK breaks between characters with kinsoku rules and Latin
    breaks at word boundaries (anywhere, if one word is too long).  Returns
    ``(lines, elided)``.
    """
    text = _clean(text)
    if not text or max_lines <= 0 or width <= 1:
        return ([], bool(text))
    layout = QTextLayout(text, font)
    opt = QTextOption()
    opt.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    layout.setTextOption(opt)
    spans: list[tuple[int, int]] = []
    layout.beginLayout()
    while True:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(width)
        spans.append((line.textStart(), line.textLength()))
    layout.endLayout()
    spans = [s for s, piece in zip(spans, _utf16_slices(text, spans)) if piece.strip()]
    pieces = [p.strip() for p in _utf16_slices(text, spans)]
    if len(pieces) <= max_lines:
        return (pieces, False)
    # the last visible line carries the whole remainder, elided at the right edge
    rest_start = spans[max_lines - 1][0]
    rest = text.encode("utf-16-le")[rest_start * 2:].decode("utf-16-le", "replace").strip()
    last = QFontMetricsF(font).elidedText(rest, Qt.TextElideMode.ElideRight, width)
    return (pieces[:max_lines - 1] + [last], True)


def _line_step(fm: QFontMetricsF) -> float:
    # YaHei reports lineSpacing (18) < height (19) at 14px; stepping by the larger
    # value keeps descenders of one line clear of the next line's ascenders.
    return max(fm.height(), fm.lineSpacing())


@functools.lru_cache(maxsize=2048)
def _cover_title_layout(title: str, family: str, w: int, inner_w: int, area_h: int
                        ) -> tuple[int, tuple[str, ...]]:
    """Font size and lines for a generated cover title (cached: paint runs often).

    Shrinks the font until the title fits without elision (up to 6 lines), then
    balances the lines: the narrowest width that keeps the same line count is used,
    so 吾輩は猫である wraps as 吾輩は猫 / である, never with one orphan character.
    """
    tf = QFont(family)
    lines: list[str] = []
    px = 11
    elided = False
    max_lines = 1
    for frac in (0.13, 0.12, 0.11, 0.10, 0.092, 0.085):
        px = max(11, round(w * frac))
        tf.setPixelSize(px)
        tf.setWeight(QFont.Weight.DemiBold)
        step = _line_step(QFontMetricsF(tf))
        max_lines = max(1, min(6, int(area_h // step)))
        lines, elided = wrap_text(title, tf, inner_w, max_lines)
        if not elided:
            break
    if len(lines) > 1 and not elided:
        n = len(lines)
        lo, hi = inner_w * 0.4, float(inner_w)
        for _ in range(9):
            mid = (lo + hi) / 2
            trial, trial_elided = wrap_text(title, tf, mid, max_lines)
            if not trial_elided and len(trial) <= n:
                hi = mid
            else:
                lo = mid
        balanced, bal_elided = wrap_text(title, tf, hi + 0.5, max_lines)
        if not bal_elided and len(balanced) == n:
            lines = balanced
    return px, tuple(lines)


def paint_generated_cover(painter: QPainter, rect: QRectF, title: str, author: str,
                          book_id: str, theme: Any = None, *, language: str = "") -> None:
    """Paint a generated cover into *rect*.

    Flat background (hue from :func:`cover_hue`), the title centred in a CJK font
    chosen by the book's *language* (sized down until it fits, up to 6 lines), a
    short rule under it, and the author small at the bottom.  Rects narrower than
    80px get a compact variant with only the first character of the title.
    """
    colors = generated_cover_colors(book_id, theme)
    family = _cover_font_family(language)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    painter.fillRect(rect, colors["bg"])
    title = _clean(title)
    if rect.width() < 80:
        glyph = title[:1] or "·"
        f = QFont(family)
        f.setPixelSize(max(9, round(rect.height() * 0.40)))
        f.setWeight(QFont.Weight.DemiBold)
        painter.setFont(f)
        painter.setPen(colors["title"])
        painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), glyph)
        painter.restore()
        return

    w = rect.width()
    pad = round(w * 0.11)
    inner_w = w - 2 * pad
    # author, bottom
    af = QFont(family)
    af.setPixelSize(max(10, round(w * 0.07)))
    afm = QFontMetricsF(af)
    author_h = _line_step(afm)
    author_top = rect.bottom() - pad * 0.8 - author_h
    if author:
        painter.setFont(af)
        painter.setPen(colors["author"])
        painter.drawText(QRectF(rect.left() + pad, author_top, inner_w, author_h),
                         int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                         afm.elidedText(author, Qt.TextElideMode.ElideRight, inner_w))
    # title, centred in the area above the author
    area_top = rect.top() + pad * 1.2
    area_bottom = author_top - pad * 0.9
    area_h = max(10.0, area_bottom - area_top)
    px, lines = _cover_title_layout(title, family, round(w), round(inner_w), round(area_h))
    tf = QFont(family)
    tf.setPixelSize(px)
    tf.setWeight(QFont.Weight.DemiBold)
    step = _line_step(QFontMetricsF(tf))
    if lines:
        block_h = step * len(lines)
        rule_gap = step * 0.55
        y = area_top + (area_h - block_h - rule_gap) / 2
        painter.setFont(tf)
        painter.setPen(colors["title"])
        for ln in lines:
            painter.drawText(QRectF(rect.left() + pad, y, inner_w, step),
                             int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter), ln)
            y += step
        rule_w = min(28.0, inner_w * 0.25)
        pen = QPen(colors["rule"], 1.5)
        painter.setPen(pen)
        ry = y + rule_gap * 0.6
        painter.drawLine(QPointF(rect.center().x() - rule_w / 2, ry),
                         QPointF(rect.center().x() + rule_w / 2, ry))
    painter.restore()


def _looks_like_svg(data: bytes, media_type: str) -> bool:
    if "svg" in (media_type or "").lower():
        return True
    head = data[:4096].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return head.startswith(b"<") and b"<svg" in head


def _svg_to_pil(data: bytes) -> Any:
    """Rasterise SVG bytes with QtSvg (white background) and hand back a PIL image."""
    from PIL import Image
    from PySide6.QtSvg import QSvgRenderer

    if data[:2] == b"\x1f\x8b":  # svgz
        import gzip
        data = gzip.decompress(data)
    renderer = QSvgRenderer(QByteArray(data))
    if not renderer.isValid():
        raise ValueError("SVG cover could not be parsed")
    size = renderer.defaultSize()
    sw, sh = size.width(), size.height()
    if sw <= 0 or sh <= 0:
        box = renderer.viewBoxF()
        sw, sh = box.width(), box.height()
    if sw <= 0 or sh <= 0:
        sw, sh = 600, 800
    w = THUMB_WIDTH * 2
    h = max(1, round(w * sh / sw))
    if h > THUMB_MAX_EDGE * 2:
        h = THUMB_MAX_EDGE * 2
        w = max(1, round(h * sw / sh))
    img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(QColor("#FFFFFF"))
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    renderer.render(p, QRectF(0, 0, w, h))
    p.end()
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    pil = Image.open(io.BytesIO(bytes(buf.data())))
    pil.load()
    return pil.convert("RGB")


def _pil_to_rgb(img: Any) -> Any:
    from PIL import Image

    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.getchannel("A"))
        return bg
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def make_thumbnail(data: bytes, dest: str, *, media_type: str = "") -> tuple[int, int]:
    """Decode cover bytes and write the cached thumbnail to *dest*.

    Raster formats go through Pillow (EXIF orientation applied, transparency
    flattened onto white, CMYK/palette converted); SVG is rasterised with QtSvg.
    The image is LANCZOS-resized to :data:`THUMB_WIDTH` px wide with the long
    edge capped at :data:`THUMB_MAX_EDGE`, and saved as JPEG q86 through a temp
    file and ``os.replace`` (a reader never sees half a file).  Returns
    ``(width, height)``; raises on undecodable data.
    """
    from PIL import Image, ImageOps

    if not data:
        raise ValueError("empty cover")
    if _looks_like_svg(data, media_type):
        img = _svg_to_pil(data)
    else:
        img = Image.open(io.BytesIO(data))
        try:
            img.draft("RGB", (THUMB_WIDTH * 2, THUMB_WIDTH * 2))  # fast JPEG downscale on load
        except Exception:
            pass
        img.load()
        try:
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass
        img = _pil_to_rgb(img)
    sw, sh = img.size
    if sw < 1 or sh < 1:
        raise ValueError("cover has no pixels")
    tw = THUMB_WIDTH
    th = max(1, round(sh * tw / sw))
    if th > THUMB_MAX_EDGE:
        th = THUMB_MAX_EDGE
        tw = max(1, round(sw * th / sh))
    img = img.resize((tw, th), Image.Resampling.LANCZOS)
    directory = os.path.dirname(dest)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = f"{dest}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        img.save(tmp, "JPEG", quality=THUMB_JPEG_QUALITY, optimize=True)
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return (tw, th)


def _edge_color(img: QImage, sides: str) -> QColor:
    """Average colour along the left+right (``'lr'``) or top+bottom (``'tb'``) edges."""
    w, h = img.width(), img.height()
    if w < 1 or h < 1:
        return QColor("#808080")
    if sides == "lr":
        strips = [img.copy(0, 0, max(1, w // 40), h), img.copy(w - max(1, w // 40), 0, max(1, w // 40), h)]
    else:
        strips = [img.copy(0, 0, w, max(1, h // 40)), img.copy(0, h - max(1, h // 40), w, max(1, h // 40))]
    r = g = b = 0
    for s in strips:
        c = s.scaled(1, 1, Qt.AspectRatioMode.IgnoreAspectRatio,
                     Qt.TransformationMode.SmoothTransformation).pixelColor(0, 0)
        r += c.red()
        g += c.green()
        b += c.blue()
    n = len(strips)
    return QColor(r // n, g // n, b // n)


def fit_cover_image(img: QImage, box_w: int, box_h: int, dpr: float = 1.0) -> QImage:
    """Fit a cover into a *box_w* x *box_h* logical box at device pixel ratio *dpr*.

    Covers whose aspect ratio is within 10% of the box are centre-cropped to fill
    it; others are letterboxed on a fill sampled from the cover's own edges, so a
    wide or very tall cover is never cut off.  Safe to call from a worker thread.
    """
    tw, th = max(1, round(box_w * dpr)), max(1, round(box_h * dpr))
    out = QImage(tw, th, QImage.Format.Format_RGB32)
    iw, ih = img.width(), img.height()
    if iw < 1 or ih < 1:
        out.fill(QColor("#808080"))
    else:
        box_ratio = box_w / box_h
        ratio = iw / ih
        if abs(ratio - box_ratio) / box_ratio <= _CROP_TOLERANCE:
            scaled = img.scaled(tw, th, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                Qt.TransformationMode.SmoothTransformation)
            out = scaled.copy((scaled.width() - tw) // 2, (scaled.height() - th) // 2, tw, th)
            out = out.convertToFormat(QImage.Format.Format_RGB32)
        else:
            scaled = img.scaled(tw, th, Qt.AspectRatioMode.KeepAspectRatio,
                                Qt.TransformationMode.SmoothTransformation)
            out.fill(_edge_color(scaled, "lr" if ratio < box_ratio else "tb"))
            p = QPainter(out)
            p.drawImage(QPoint((tw - scaled.width()) // 2, (th - scaled.height()) // 2), scaled)
            p.end()
    out.setDevicePixelRatio(dpr)
    return out


# ==========================================================================
# library entries from files (worker side)
# ==========================================================================

def read_library_entry(path: str, book_id: str, *, cover_file: str | None = None) -> dict:
    """Build a ``library.json`` entry for *path* (read-only access to the file).

    Opens the book with :class:`epublib.EpubBook` for its metadata and, when
    *cover_file* is given, writes the cover thumbnail there on the way.  A DRM
    book still gets an entry (title from the file name, ``drm`` set) so it shows
    on the shelf and opening it explains the problem.  Any other ``EpubError``
    or ``OSError`` propagates.
    """
    full = os.path.abspath(path)
    st = os.stat(full)
    stem = _clean(os.path.splitext(os.path.basename(full))[0])
    entry: dict[str, Any] = {
        "id": book_id, "path": full, "size": st.st_size, "mtime_ns": st.st_mtime_ns,
        "verified_at": now_iso(), "missing": False, "drm": None,
    }
    try:
        book = EpubBook.open(full)
    except EpubError as exc:
        if exc.kind == "drm":
            entry.update(title=stem, authors=[], drm=exc.drm_scheme or "unknown", cover="",
                         cover_w=0, cover_h=0)
            return entry
        raise
    with book:
        md = book.metadata or {}
        entry.update(
            title=_clean(md.get("title")) or stem,
            authors=[a for a in (_clean(x) for x in (md.get("authors") or [])) if a],
            publisher=_clean(md.get("publisher")),
            pubdate=_clean(md.get("date")),
            language=_clean(md.get("language")),
            identifier=_clean(md.get("identifier")),
            epub_version=_clean(getattr(book, "version", "")),
            layout="fixed" if book.is_fixed_layout else "reflowable",
            toc_source="spine" if book.toc_is_synthetic else "toc",
            spine_count=len(book.spine),
        )
        try:
            entry["units_total"] = int(book.total_units())
        except Exception:  # a bad spine document never fails the add
            pass
        if cover_file:
            try:
                data = book.cover_bytes()
                if data:
                    mt = book.media_type(book.cover) if book.cover else ""
                    w, h = make_thumbnail(data, cover_file, media_type=mt)
                    entry.update(cover=f"covers/{book_id}.jpg", cover_w=w, cover_h=h)
                else:
                    entry.update(cover="", cover_w=0, cover_h=0)
            except Exception as exc:  # undecodable cover -> generated cover, retried next launch
                _log.info("cover thumbnail failed for %s: %r", full, exc)
    return entry


# ==========================================================================
# worker plumbing
# ==========================================================================

class _Emitter(QObject):
    """Lives in the GUI thread; workers emit on it, so slots run on the GUI thread."""

    cover = Signal(object)      # _CoverResult
    added = Signal(object)      # _AddResult
    checked = Signal(object)    # dict[str, bool]  book_id -> file exists
    details = Signal(object)    # (book_id, dict)


def _emit(signal: Any, payload: Any) -> None:
    try:
        signal.emit(payload)
    except RuntimeError:  # the page (and its connections) is already gone
        pass


@dataclass
class _CoverResult:
    bid: str
    image: QImage | None = None
    status: str = "error"          # cached | made | none | missing | error
    size: tuple[int, int] = (0, 0)


def _cover_job(emitter: _Emitter, cancel: threading.Event, bid: str, epub_path: str,
               cover_file: str, box: tuple[int, int], dpr: float) -> None:
    if cancel.is_set():
        return
    res = _CoverResult(bid)
    try:
        img = QImage()
        if os.path.isfile(cover_file) and os.path.getsize(cover_file) > 0:
            img = QImage(cover_file)
            if not img.isNull():
                res.status = "cached"
        if img.isNull():
            if not epub_path or not os.path.isfile(epub_path):
                res.status = "missing"
            else:
                with EpubBook.open(epub_path) as book:
                    data = book.cover_bytes()
                    mt = book.media_type(book.cover) if book.cover else ""
                if not data:
                    res.status = "none"
                else:
                    res.size = make_thumbnail(data, cover_file, media_type=mt)
                    img = QImage(cover_file)
                    res.status = "made" if not img.isNull() else "error"
        if not img.isNull() and not cancel.is_set():
            res.image = fit_cover_image(img, box[0], box[1], dpr)
    except Exception as exc:
        res.status = "error"
        _log.info("cover for %s failed: %r", bid, exc)
    if not cancel.is_set():
        _emit(emitter.cover, res)


@dataclass
class _Known:
    bid: str
    path: str
    norm: str
    size: Any
    mtime_ns: Any
    missing: bool


@dataclass
class _AddResult:
    token: int
    mode: str                                   # files | rescan | suggestion
    files_found: int = 0
    new_entries: list[dict] = field(default_factory=list)
    repairs: list[dict] = field(default_factory=list)
    already: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    open_bid: str = ""
    open_path: str = ""
    suggestion: tuple[str, list[str]] | None = None
    folders: list[str] = field(default_factory=list)


def _add_job(emitter: _Emitter, cancel: threading.Event, store: Store, token: int, mode: str,
             paths: list[str], known: list[_Known], open_after: bool, suggest: bool,
             skip_known_paths: bool) -> None:
    res = _AddResult(token=token, mode=mode,
                     folders=[os.path.abspath(p) for p in paths if os.path.isdir(p)])
    try:
        files, res.unsupported = collect_epubs(paths, cancel=cancel)
        by_path = {k.norm: k for k in known}
        by_id = {k.bid: k for k in known}
        if skip_known_paths:
            files = [f for f in files if _norm_path(f) not in by_path]
        res.files_found = len(files)
        batch: set[str] = set()
        for path in files:
            if cancel.is_set():
                return
            try:
                st = os.stat(path)
                norm = _norm_path(path)
                k = by_path.get(norm)
                if k is not None and k.size == st.st_size and k.mtime_ns == st.st_mtime_ns:
                    res.already.append(k.bid)
                    if open_after:
                        res.open_bid, res.open_path = k.bid, path
                    continue
                bid = store.book_id_for(path)
                if bid in batch:
                    continue
                batch.add(bid)
                old = by_id.get(bid)
                if old is not None:
                    if old.norm != norm and (old.missing or not os.path.isfile(old.path)):
                        res.repairs.append({"id": bid, "path": os.path.abspath(path),
                                            "old_path": old.path, "size": st.st_size,
                                            "mtime_ns": st.st_mtime_ns, "missing": False})
                    else:
                        res.already.append(bid)
                    if open_after:
                        res.open_bid, res.open_path = bid, os.path.abspath(path)
                    continue
                entry = read_library_entry(path, bid, cover_file=store.cover_path(bid))
                res.new_entries.append(entry)
                if open_after:
                    res.open_bid, res.open_path = bid, entry["path"]
            except (EpubError, OSError, ValueError, KeyError, RuntimeError) as exc:
                res.failed.append((path, f"{type(exc).__name__}: {exc}"))
                if open_after:
                    # a single file the user asked to open: the reader shows the specific
                    # error card (corrupt / not an EPUB / unreadable) with its technical details
                    res.open_bid, res.open_path = "", os.path.abspath(path)
        if suggest and len(files) == 1 and not res.failed:
            folder = os.path.dirname(files[0])
            taken = set(by_path) | {_norm_path(files[0])}
            try:
                siblings = sorted(
                    os.path.join(folder, e.name) for e in os.scandir(folder)
                    if e.is_file() and is_epub_path(e.name)
                    and _norm_path(os.path.join(folder, e.name)) not in taken)
            except OSError:
                siblings = []
            if siblings:
                res.suggestion = (folder, siblings)
    except Exception as exc:  # never lose the result: report what we have
        _log.exception("add job failed")
        res.failed.append(("", repr(exc)))
    if not cancel.is_set():
        _emit(emitter.added, res)


def _missing_job(emitter: _Emitter, cancel: threading.Event, pairs: list[tuple[str, str]]) -> None:
    out: dict[str, bool] = {}
    for bid, path in pairs:
        if cancel.is_set():
            return
        try:
            out[bid] = bool(path) and os.path.isfile(path)
        except OSError:
            out[bid] = False
    _emit(emitter.checked, out)


def _details_job(emitter: _Emitter, cancel: threading.Event, bid: str, path: str) -> None:
    info: dict[str, Any] = {}
    try:
        if path and os.path.isfile(path):
            with EpubBook.open(path) as book:
                md = book.metadata or {}
                info["description"] = _strip_html(md.get("description") or "")
                info["subjects"] = [s for s in (_clean(x) for x in (md.get("subjects") or [])) if s]
    except Exception as exc:
        info["error"] = repr(exc)
    if not cancel.is_set():
        _emit(emitter.details, (bid, info))


# ==========================================================================
# painted icons (theme coloured, language independent)
# ==========================================================================

def _make_icon(kind: str, color: QColor, size: int = 16, dpr: float = 1.0) -> QIcon:
    pm = QPixmap(max(1, round(size * dpr)), max(1, round(size * dpr)))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(color, 1.5)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    if kind == "grid":
        for x, y in ((2.5, 2.5), (9.0, 2.5), (2.5, 9.0), (9.0, 9.0)):
            p.drawRoundedRect(QRectF(x, y, 4.5, 4.5), 1.0, 1.0)
    elif kind == "list":
        for y in (4.0, 8.0, 12.0):
            p.drawLine(QPointF(5.5, y), QPointF(13.5, y))
            p.drawPoint(QPointF(2.5, y))
    elif kind == "search":
        p.drawEllipse(QPointF(7.0, 7.0), 4.3, 4.3)
        p.drawLine(QPointF(10.3, 10.3), QPointF(13.8, 13.8))
    elif kind == "book":
        path = QPainterPath(QPointF(8, 4.2))
        path.cubicTo(6.2, 3.1, 4.2, 2.9, 1.8, 3.4)
        path.lineTo(1.8, 13.0)
        path.cubicTo(4.2, 12.5, 6.2, 12.7, 8, 13.8)
        path.cubicTo(9.8, 12.7, 11.8, 12.5, 14.2, 13.0)
        path.lineTo(14.2, 3.4)
        path.cubicTo(11.8, 2.9, 9.8, 3.1, 8, 4.2)
        p.drawPath(path)
        p.drawLine(QPointF(8, 4.2), QPointF(8, 13.8))
    elif kind == "folder":
        path = QPainterPath(QPointF(1.8, 4.0))
        path.lineTo(6.0, 4.0)
        path.lineTo(7.4, 5.6)
        path.lineTo(14.2, 5.6)
        path.lineTo(14.2, 13.2)
        path.lineTo(1.8, 13.2)
        path.closeSubpath()
        p.drawPath(path)
        p.drawLine(QPointF(8, 7.6), QPointF(8, 11.4))
        p.drawLine(QPointF(6.1, 9.5), QPointF(9.9, 9.5))
    elif kind == "more":
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        s = size / 16.0
        for x in (3.0, 8.0, 13.0):
            p.drawEllipse(QPointF(x * s, 8.0 * s), 1.35 * s, 1.35 * s)
    p.end()
    return QIcon(pm)


# ==========================================================================
# the shelf view (custom painted; grid and list)
# ==========================================================================

@dataclass
class _Book:
    bid: str
    title: str
    authors: str
    progress: float
    finished: bool
    missing: bool
    path: str
    opened_at: Any
    added_at: Any
    language: str
    drm: Any

    @classmethod
    def from_entry(cls, e: dict) -> "_Book":
        return cls(bid=str(e.get("id") or ""), title=display_title(e), authors=display_authors(e),
                   progress=book_progress(e), finished=_is_finished(e),
                   missing=bool(e.get("missing")), path=str(e.get("path") or ""),
                   opened_at=e.get("opened_at"), added_at=e.get("added_at"),
                   language=str(e.get("language") or ""), drm=e.get("drm"))


@dataclass
class _Slot:
    book: _Book
    section: str        # 'continue' | 'all'
    rect: QRectF        # whole card / row, content coordinates
    cover: QRectF       # cover box (grid) or mini cover (list)


class ShelfView(QAbstractScrollArea):
    """The painted shelf: the continue-reading row and the all-books grid, or the list.

    Keyboard: arrows move the selection (geometrically between rows), Home/End,
    PageUp/PageDown, Enter opens, Delete asks to remove, the Menu key opens the
    context menu.  Mouse: click selects, double-click opens, right-click menus.
    """

    activated = Signal(str)                 # book id (double-click / Enter)
    contextRequested = Signal(str, QPoint)  # book id, global position
    removeRequested = Signal(str)           # book id (Delete)
    selectionChanged = Signal(str)          # book id or ''

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ShelfView")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.viewport().setMouseTracking(True)
        self.viewport().setAutoFillBackground(False)
        self.verticalScrollBar().setSingleStep(48)
        self._theme: Any = theme_mod.LIGHT
        self._mode = "grid"
        self._continue: list[_Book] = []
        self._books: list[_Book] = []
        self._query_active = False
        self.covers: dict[str, QPixmap] = {}
        self._minis: dict[str, QPixmap] = {}
        self._slots: list[_Slot] = []
        self._headers: list[tuple[str, QRectF, str, str]] = []
        self._cols: dict[str, float] = {}
        self._content_h = 0.0
        self._sel_key: tuple[str, str] | None = None   # (section, bid)
        self._selected = -1
        self._hover = -1
        self._wrap_cache: dict[tuple, tuple[list[str], bool]] = {}
        self._card_h = 0.0
        self._init_fonts()

    # -- fonts / theme / language -------------------------------------------
    def _init_fonts(self) -> None:
        base = QFont(self.font())
        self.f_title = QFont(base)
        self.f_title.setPixelSize(14)
        self.f_author = QFont(base)
        self.f_author.setPixelSize(13)
        self.f_header = QFont(base)
        self.f_header.setPixelSize(17)
        self.f_header.setWeight(QFont.Weight.DemiBold)
        self.f_count = QFont(base)
        self.f_count.setPixelSize(13)
        self.f_badge = QFont(base)
        self.f_badge.setPixelSize(11)
        self.f_small = QFont(base)
        self.f_small.setPixelSize(12)
        self.f_empty = QFont(base)
        self.f_empty.setPixelSize(15)
        self.fm_title = QFontMetricsF(self.f_title)
        self.fm_author = QFontMetricsF(self.f_author)
        self._card_h = (COVER_SIZE[1] + 10 + 2 * _line_step(self.fm_title) + 3
                        + _line_step(self.fm_author))
        self._wrap_cache.clear()

    def set_theme(self, theme: Any) -> None:
        """Repaint with a :class:`theme.Theme`."""
        self._theme = theme
        self.viewport().update()

    def retranslate_ui(self) -> None:
        """Section headers, counts, badges and fallbacks are re-rendered from strings."""
        self._wrap_cache.clear()
        self._relayout()

    # -- content ------------------------------------------------------------
    def set_mode(self, mode: str) -> None:
        if mode not in VIEW_MODES:
            mode = "grid"
        if mode != self._mode:
            self._mode = mode
            self._relayout()

    def mode(self) -> str:
        return self._mode

    def set_books(self, continue_books: list[_Book], books: list[_Book], *, query_active: bool) -> None:
        self._continue = list(continue_books)
        self._books = list(books)
        self._query_active = query_active
        self._relayout()

    def slots(self) -> list[_Slot]:
        """The laid-out cards/rows in visual order (read-only; for tests and tools)."""
        return list(self._slots)

    def headers(self) -> list[tuple[str, QRectF, str, str]]:
        return list(self._headers)

    def invalidate_book(self, bid: str) -> None:
        """A cover arrived or changed: repaint that book's slots only."""
        self._minis.pop(bid, None)
        oy = self.verticalScrollBar().value()
        for s in self._slots:
            if s.book.bid == bid:
                self.viewport().update(s.rect.adjusted(-6, -6, 6, 6).translated(0, -oy).toAlignedRect())

    # -- selection ----------------------------------------------------------
    def selected_book_id(self) -> str:
        if 0 <= self._selected < len(self._slots):
            return self._slots[self._selected].book.bid
        return ""

    def select_book(self, bid: str, *, ensure_visible: bool = True) -> bool:
        for i, s in enumerate(self._slots):
            if s.book.bid == bid and (s.section == "all" or self._mode == "list"):
                self._set_selected(i, ensure_visible=ensure_visible)
                return True
        for i, s in enumerate(self._slots):
            if s.book.bid == bid:
                self._set_selected(i, ensure_visible=ensure_visible)
                return True
        return False

    def select_index(self, index: int) -> None:
        if self._slots:
            self._set_selected(max(0, min(len(self._slots) - 1, index)))

    def selected_index(self) -> int:
        return self._selected

    def clear_selection(self) -> None:
        self._set_selected(-1)

    def _set_selected(self, index: int, *, ensure_visible: bool = True) -> None:
        old = self._selected
        self._selected = index if 0 <= index < len(self._slots) else -1
        if self._selected >= 0:
            s = self._slots[self._selected]
            self._sel_key = (s.section, s.book.bid)
            if ensure_visible:
                self.ensure_visible(self._selected)
        else:
            self._sel_key = None
        for i in (old, self._selected):
            self._update_slot(i)
        if old != self._selected:
            self.selectionChanged.emit(self.selected_book_id())

    def ensure_visible(self, index: int) -> None:
        if not 0 <= index < len(self._slots):
            return
        slot = self._slots[index]
        r = slot.rect
        sb = self.verticalScrollBar()
        top, bottom = r.top() - 16, r.bottom() + 16
        # first row of a section: bring its header into view too
        for kind, hrect, _t, _s in self._headers:
            if (kind == slot.section or (kind == "columns" and self._mode == "list")) \
                    and 0 <= r.top() - hrect.bottom() < 1:
                top = hrect.top() - (_TOP if hrect.top() <= _TOP else 8)
        vh = self.viewport().height()
        if top < sb.value():
            sb.setValue(int(max(0, top)))
        elif bottom > sb.value() + vh:
            sb.setValue(int(bottom - vh))

    def _update_slot(self, index: int) -> None:
        if 0 <= index < len(self._slots):
            oy = self.verticalScrollBar().value()
            self.viewport().update(self._slots[index].rect.adjusted(-7, -7, 7, 7)
                                   .translated(0, -oy).toAlignedRect())

    # -- layout -------------------------------------------------------------
    def _layout_width(self) -> float:
        # Always reserve the scrollbar's width, so showing or hiding it never
        # changes the column count (no relayout oscillation at the boundary).
        sb = max(_SCROLLBAR_RESERVE, self.verticalScrollBar().sizeHint().width())
        return max(200.0, float(self.width() - 2 * self.frameWidth() - sb))

    def grid_metrics(self) -> tuple[int, float, float]:
        """(columns, gap, left x) for the current width."""
        lw = self._layout_width()
        cw = COVER_SIZE[0]
        avail = lw - 2 * _MARGIN_X
        cols = max(1, int((avail + _GAP_X_MIN) // (cw + _GAP_X_MIN)))
        gap = _GAP_X_MIN if cols == 1 else min(_GAP_X_MAX, (avail - cols * cw) / (cols - 1))
        used = cols * cw + (cols - 1) * gap
        x0 = max(float(_MARGIN_X) / 2, (lw - used) / 2)
        return cols, gap, x0

    def _relayout(self) -> None:
        self._slots = []
        self._headers = []
        if self._mode == "list":
            y = self._layout_list()
        else:
            y = self._layout_grid()
        self._content_h = y
        # restore the selection by (section, bid), else by bid
        self._selected = -1
        if self._sel_key is not None:
            sec, bid = self._sel_key
            for i, s in enumerate(self._slots):
                if s.book.bid == bid and s.section == sec:
                    self._selected = i
                    break
            else:
                for i, s in enumerate(self._slots):
                    if s.book.bid == bid:
                        self._selected = i
                        break
            if self._selected < 0:
                self._sel_key = None
        self._hover = -1
        self._update_scrollbar()
        self.viewport().update()

    def _layout_grid(self) -> float:
        cols, gap, x0 = self.grid_metrics()
        cw, ch = COVER_SIZE
        used = cols * cw + (cols - 1) * gap
        y = float(_TOP)

        def place(books: list[_Book], section: str, y: float) -> float:
            for i, b in enumerate(books):
                c, r = i % cols, i // cols
                x = round(x0 + c * (cw + gap))
                top = round(y + r * (self._card_h + _GAP_Y))
                card = QRectF(x, top, cw, self._card_h)
                self._slots.append(_Slot(b, section, card, QRectF(x, top, cw, ch)))
            rows = (len(books) + cols - 1) // cols
            return y + rows * self._card_h + max(0, rows - 1) * _GAP_Y

        if self._continue and not self._query_active:
            self._headers.append(("continue", QRectF(x0, y, used, _HEADER_H),
                                  S("lib.section.continue"), ""))
            y = place(self._continue, "continue", y + _HEADER_H)
            y += _SECTION_GAP + 10
        if self._books or not self._query_active:
            self._headers.append(("all", QRectF(x0, y, used, _HEADER_H), S("lib.section.all"),
                                  plural("lib.count", len(self._books))))
            y = place(self._books, "all", y + _HEADER_H)
        return y + _BOTTOM

    def _layout_list(self) -> float:
        lw = self._layout_width()
        x0 = float(_MARGIN_X)
        w = lw - 2 * _MARGIN_X
        y = float(_TOP)
        if not self._books:
            return y + _BOTTOM
        progress_w, opened_w = 150.0, 124.0
        added_w = 124.0 if w >= 780 else 0.0
        text_x = x0 + 12 + _MINI_W + 16
        title_w = max(120.0, (x0 + w) - text_x - progress_w - opened_w - added_w - 12)
        self._cols = {"x0": x0, "w": w, "text_x": text_x, "title_w": title_w,
                      "progress_x": text_x + title_w + 12, "progress_w": progress_w,
                      "opened_x": text_x + title_w + 12 + progress_w, "opened_w": opened_w,
                      "added_x": text_x + title_w + 12 + progress_w + opened_w, "added_w": added_w}
        self._headers.append(("columns", QRectF(x0, y, w, _LIST_HEADER_H), "", ""))
        y += _LIST_HEADER_H
        for b in self._books:
            row = QRectF(x0, y, w, _ROW_H)
            mini = QRectF(x0 + 12, y + (_ROW_H - _MINI_H) / 2, _MINI_W, _MINI_H)
            self._slots.append(_Slot(b, "all", row, mini))
            y += _ROW_H
        return y + _BOTTOM

    def _update_scrollbar(self) -> None:
        sb = self.verticalScrollBar()
        vh = self.viewport().height()
        sb.setPageStep(max(1, vh))
        sb.setRange(0, max(0, int(self._content_h - vh)))

    def resizeEvent(self, event: Any) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._relayout()

    def scrollContentsBy(self, dx: int, dy: int) -> None:  # noqa: N802
        self.viewport().update()

    def content_height(self) -> float:
        return self._content_h

    # -- painting -----------------------------------------------------------
    def _wrapped(self, text: str, font: QFont, width: float, lines: int) -> tuple[list[str], bool]:
        key = (text, font.key(), round(width), lines)
        hit = self._wrap_cache.get(key)
        if hit is None:
            hit = wrap_text(text, font, width, lines)
            if len(self._wrap_cache) > 4000:
                self._wrap_cache.clear()
            self._wrap_cache[key] = hit
        return hit

    def paintEvent(self, event: Any) -> None:  # noqa: N802
        t = self._theme
        p = QPainter(self.viewport())
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        p.fillRect(self.viewport().rect(), QColor(t.bg))
        oy = self.verticalScrollBar().value()
        exposed = QRectF(event.rect()).translated(0, oy)
        p.translate(0, -oy)
        for kind, rect, text, sub in self._headers:
            if rect.intersects(exposed):
                self._paint_header(p, kind, rect, text, sub)
        for i, s in enumerate(self._slots):
            if s.rect.adjusted(-8, -8, 8, 8).intersects(exposed):
                if self._mode == "list":
                    self._paint_row(p, s, i == self._selected, i == self._hover)
                else:
                    self._paint_card(p, s, i == self._selected, i == self._hover)
        p.translate(0, oy)
        if not self._slots and self._query_active:
            p.setFont(self.f_empty)
            p.setPen(QColor(t.secondary))
            vr = QRectF(self.viewport().rect())
            area = QRectF(vr.left(), vr.top() + 90, vr.width(), 60)
            p.drawText(area, int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
                       S("lib.nomatch"))
        p.end()

    def _paint_header(self, p: QPainter, kind: str, rect: QRectF, text: str, sub: str) -> None:
        t = self._theme
        if kind == "columns":
            c = self._cols
            p.setFont(self.f_small)
            p.setPen(QColor(t.secondary))
            fm = QFontMetricsF(self.f_small)
            flags = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            cells = [(c["text_x"], c["title_w"], S("lib.col.title")),
                     (c["progress_x"], c["progress_w"] - 12, S("lib.col.progress")),
                     (c["opened_x"], c["opened_w"] - 12, S("lib.col.opened"))]
            if c["added_w"]:
                cells.append((c["added_x"], c["added_w"] - 12, S("lib.col.added")))
            for x, w, label in cells:
                p.drawText(QRectF(x, rect.top(), w, rect.height()), flags,
                           fm.elidedText(label, Qt.TextElideMode.ElideRight, w))
            p.setPen(QPen(QColor(t.border), 1))
            p.drawLine(QPointF(rect.left(), rect.bottom() - 0.5), QPointF(rect.right(), rect.bottom() - 0.5))
            return
        fm = QFontMetricsF(self.f_header)
        p.setFont(self.f_header)
        p.setPen(QColor(t.fg))
        base = rect.top() + (rect.height() - 8) / 2 + (fm.ascent() - fm.descent()) / 2
        title_w = min(fm.horizontalAdvance(text), rect.width())
        p.drawText(QPointF(rect.left(), base), fm.elidedText(text, Qt.TextElideMode.ElideRight, rect.width()))
        if sub:
            p.setFont(self.f_count)
            p.setPen(QColor(t.secondary))
            fmc = QFontMetricsF(self.f_count)
            room = rect.width() - title_w - 12
            if room > 20:
                p.drawText(QPointF(rect.left() + title_w + 12, base),
                           fmc.elidedText(sub, Qt.TextElideMode.ElideRight, room))

    def _cover_pixmap(self, bid: str) -> QPixmap | None:
        return self.covers.get(bid)

    def _paint_cover(self, p: QPainter, b: _Book, rect: QRectF) -> None:
        pm = self._cover_pixmap(b.bid)
        if pm is not None and not pm.isNull():
            if rect.width() < 80:
                mini = self._minis.get(b.bid)
                dpr = self.devicePixelRatioF()
                if mini is None:
                    img = pm.toImage()
                    img.setDevicePixelRatio(1.0)
                    mini = QPixmap.fromImage(img.scaled(round(rect.width() * dpr), round(rect.height() * dpr),
                                                        Qt.AspectRatioMode.IgnoreAspectRatio,
                                                        Qt.TransformationMode.SmoothTransformation))
                    mini.setDevicePixelRatio(dpr)
                    self._minis[b.bid] = mini
                p.drawPixmap(rect.topLeft(), mini)
            else:
                p.drawPixmap(rect, pm, QRectF(pm.rect()))
        else:
            paint_generated_cover(p, rect, b.title or S("lib.title.unknown"), b.authors, b.bid,
                                  self._theme, language=b.language)
        edge = QColor(self._theme.fg)
        edge.setAlphaF(0.14 if not self._theme.is_dark else 0.22)
        p.setPen(QPen(edge, 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(rect.adjusted(0.5, 0.5, -0.5, -0.5))

    def _paint_badge(self, p: QPainter, x: float, y: float, max_w: float, text: str) -> QRectF:
        t = self._theme
        fm = QFontMetricsF(self.f_badge)
        label = fm.elidedText(text, Qt.TextElideMode.ElideRight, max(10.0, max_w - 12))
        w = fm.horizontalAdvance(label) + 12
        h = _line_step(fm) + 4
        r = QRectF(x, y, w, h)
        p.setPen(QPen(QColor(t.chrome_border), 1))
        p.setBrush(QColor(t.chrome_bg))
        p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)
        p.setFont(self.f_badge)
        p.setPen(QColor(t.chrome_fg))
        p.drawText(r, int(Qt.AlignmentFlag.AlignCenter), label)
        return r

    def _paint_progress(self, p: QPainter, rect: QRectF, progress: float) -> None:
        if progress <= 0:
            return
        bar = QRectF(rect.left(), rect.bottom() - PROGRESS_BAR_PX, rect.width(), PROGRESS_BAR_PX)
        track = QColor(0, 0, 0)
        track.setAlphaF(0.32)
        p.fillRect(bar, track)
        p.fillRect(QRectF(bar.left(), bar.top(), round(bar.width() * min(1.0, progress)), bar.height()),
                   QColor(self._theme.accent))

    def _paint_card(self, p: QPainter, s: _Slot, selected: bool, hover: bool) -> None:
        t = self._theme
        b = s.book
        cover = s.cover
        p.setOpacity(MISSING_OPACITY if b.missing else 1.0)
        self._paint_cover(p, b, cover)
        self._paint_progress(p, cover, b.progress)
        # title (2 lines, elided) and author (1 line, secondary)
        y = cover.bottom() + 10
        step_t = _line_step(self.fm_title)
        lines, _ = self._wrapped(b.title or S("lib.title.unknown"), self.f_title, cover.width(), 2)
        p.setFont(self.f_title)
        p.setPen(QColor(t.fg))
        for ln in lines:
            p.drawText(QRectF(cover.left(), y, cover.width(), step_t),
                       int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), ln)
            y += step_t
        y += 3
        p.setFont(self.f_author)
        p.setPen(QColor(t.secondary))
        author = b.authors or S("lib.author.unknown")
        p.drawText(QRectF(cover.left(), y, cover.width(), _line_step(self.fm_author)),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   self.fm_author.elidedText(author, Qt.TextElideMode.ElideRight, cover.width()))
        p.setOpacity(1.0)
        if b.missing:
            self._paint_badge(p, cover.left() + 6, cover.top() + 6, cover.width() - 12,
                              S("lib.badge.missing"))
        if selected or hover:
            ring = cover.adjusted(-4, -4, 4, 4)
            color = QColor(t.accent) if selected else QColor(t.border)
            if hover and not selected:
                color = QColor(t.secondary)
                color.setAlphaF(0.45)
            p.setPen(QPen(color, 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(ring, 4, 4)

    def _paint_row(self, p: QPainter, s: _Slot, selected: bool, hover: bool) -> None:
        t = self._theme
        b = s.book
        c = self._cols
        r = s.rect
        if selected:
            p.fillRect(r, QColor(t.selection))
        elif hover:
            hc = QColor(t.fg)
            hc.setAlphaF(0.05 if not t.is_dark else 0.07)
            p.fillRect(r, hc)
        else:
            p.setPen(QPen(QColor(t.border), 1))
            p.drawLine(QPointF(r.left(), r.bottom() - 0.5), QPointF(r.right(), r.bottom() - 0.5))
        p.setOpacity(MISSING_OPACITY if b.missing else 1.0)
        self._paint_cover(p, b, s.cover)
        # title + author
        title_w = c["title_w"]
        badge_w = 0.0
        if b.missing:
            # the badge takes what the title does not need, but never more than 60%
            fmb = QFontMetricsF(self.f_badge)
            need = fmb.horizontalAdvance(S("lib.badge.missing")) + 12 + 10
            title_need = self.fm_title.horizontalAdvance(b.title or S("lib.title.unknown"))
            badge_w = min(need, max(title_w - title_need, title_w * 0.4), title_w * 0.6)
        step_t = _line_step(self.fm_title)
        step_a = _line_step(self.fm_author)
        top = r.top() + (r.height() - step_t - step_a - 2) / 2
        p.setFont(self.f_title)
        p.setPen(QColor(t.fg))
        p.drawText(QRectF(c["text_x"], top, title_w - badge_w, step_t),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   self.fm_title.elidedText(b.title or S("lib.title.unknown"),
                                            Qt.TextElideMode.ElideRight, title_w - badge_w))
        p.setFont(self.f_author)
        p.setPen(QColor(t.secondary))
        p.drawText(QRectF(c["text_x"], top + step_t + 2, title_w, step_a),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   self.fm_author.elidedText(b.authors or S("lib.author.unknown"),
                                             Qt.TextElideMode.ElideRight, title_w))
        # progress text + small bar
        fms = QFontMetricsF(self.f_small)
        if b.finished:
            ptxt = S("lib.badge.finished")
        elif b.progress > 0:
            ptxt = S("lib.progress", p=_percent(b.progress))
        else:
            ptxt = S("lib.progress.none")
        pw = c["progress_w"] - 16
        p.setFont(self.f_small)
        p.setPen(QColor(t.fg if b.progress > 0 else t.secondary))
        p.drawText(QRectF(c["progress_x"], r.top(), pw, r.height() - 10),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   fms.elidedText(ptxt, Qt.TextElideMode.ElideRight, pw))
        if b.progress > 0:
            bar = QRectF(c["progress_x"], r.center().y() + 10, min(96.0, pw), PROGRESS_BAR_PX)
            track = QColor(t.border)
            p.fillRect(bar, track)
            p.fillRect(QRectF(bar.left(), bar.top(), round(bar.width() * b.progress), bar.height()),
                       QColor(t.accent))
        p.setPen(QColor(t.secondary))
        ow = c["opened_w"] - 12
        opened = format_date(b.opened_at) if b.opened_at else S("lib.never_opened")
        p.drawText(QRectF(c["opened_x"], r.top(), ow, r.height()),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   fms.elidedText(opened, Qt.TextElideMode.ElideRight, ow))
        if c["added_w"]:
            aw = c["added_w"] - 12
            p.drawText(QRectF(c["added_x"], r.top(), aw, r.height()),
                       int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                       fms.elidedText(format_date(b.added_at) if b.added_at else "",
                                      Qt.TextElideMode.ElideRight, aw))
        p.setOpacity(1.0)
        if b.missing:
            fmb = QFontMetricsF(self.f_badge)
            tw = min(self.fm_title.horizontalAdvance(b.title or S("lib.title.unknown")), title_w - badge_w)
            self._paint_badge(p, c["text_x"] + tw + 10, top + (step_t - _line_step(fmb) - 4) / 2,
                              badge_w - 10, S("lib.badge.missing"))
        if selected and self.hasFocus():
            p.setPen(QPen(QColor(t.accent), 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(r.adjusted(0.75, 0.75, -0.75, -0.75))

    # -- hit testing and input ---------------------------------------------
    def index_at(self, pos: QPoint | QPointF) -> int:
        """Slot index under a viewport position, or -1."""
        pt = QPointF(pos) + QPointF(0, self.verticalScrollBar().value())
        for i, s in enumerate(self._slots):
            if s.rect.contains(pt):
                return i
        return -1

    def slot_viewport_rect(self, index: int) -> QRect:
        if not 0 <= index < len(self._slots):
            return QRect()
        return self._slots[index].cover.translated(0, -self.verticalScrollBar().value()).toAlignedRect()

    def mousePressEvent(self, event: Any) -> None:  # noqa: N802
        if event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            idx = self.index_at(event.position())
            self._set_selected(idx, ensure_visible=False)
            self.setFocus(Qt.FocusReason.MouseFocusReason)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: Any) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            idx = self.index_at(event.position())
            if idx >= 0:
                self.activated.emit(self._slots[idx].book.bid)
                return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event: Any) -> None:  # noqa: N802
        idx = self.index_at(event.position())
        if idx != self._hover:
            old, self._hover = self._hover, idx
            self._update_slot(old)
            self._update_slot(idx)
        super().mouseMoveEvent(event)

    def viewportEvent(self, event: Any) -> bool:  # noqa: N802
        et = event.type()
        if et == QEvent.Type.Leave and self._hover >= 0:
            old, self._hover = self._hover, -1
            self._update_slot(old)
        elif et == QEvent.Type.ToolTip:
            idx = self.index_at(event.pos())
            if idx >= 0:
                QToolTip.showText(event.globalPos(), self._tooltip(self._slots[idx].book), self.viewport())
            else:
                QToolTip.hideText()
                event.ignore()
            return True
        return super().viewportEvent(event)

    def _tooltip(self, b: _Book) -> str:
        lines = [b.title or S("lib.title.unknown"), b.authors or S("lib.author.unknown")]
        if b.finished:
            lines.append(S("lib.badge.finished"))
        elif b.progress > 0:
            lines.append(S("lib.progress", p=_percent(b.progress)))
        if b.missing:
            lines.append(S("lib.badge.missing.tip"))
        return "\n".join(lines)

    def contextMenuEvent(self, event: Any) -> None:  # noqa: N802
        if event.reason() == event.Reason.Keyboard:
            idx = self._selected
            if idx < 0:
                return
            pos = self.viewport().mapToGlobal(self.slot_viewport_rect(idx).center())
        else:
            idx = self.index_at(self.viewport().mapFromGlobal(event.globalPos()))
            if idx < 0:
                return
            self._set_selected(idx, ensure_visible=False)
            pos = event.globalPos()
        self.contextRequested.emit(self._slots[idx].book.bid, pos)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        key = event.key()
        mods = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        if mods == Qt.KeyboardModifier.NoModifier:
            if key in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down,
                       Qt.Key.Key_Home, Qt.Key.Key_End, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
                self._move(key)
                event.accept()
                return
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                bid = self.selected_book_id()
                if bid:
                    self.activated.emit(bid)
                    event.accept()
                    return
            if key == Qt.Key.Key_Delete:
                bid = self.selected_book_id()
                if bid:
                    self.removeRequested.emit(bid)
                    event.accept()
                    return
        event.ignore()

    def _move(self, key: int) -> None:
        n = len(self._slots)
        if n == 0:
            return
        cur = self._selected
        if cur < 0:
            self._set_selected(0 if key not in (Qt.Key.Key_End,) else n - 1)
            return
        K = Qt.Key
        if key == K.Key_Home:
            target = 0
        elif key == K.Key_End:
            target = n - 1
        elif key == K.Key_Left:
            target = max(0, cur - 1)
        elif key == K.Key_Right:
            target = min(n - 1, cur + 1)
        elif self._mode == "list":
            page = max(1, int(self.viewport().height() // _ROW_H) - 1)
            step = {K.Key_Up: -1, K.Key_Down: 1, K.Key_PageUp: -page, K.Key_PageDown: page}[key]
            target = max(0, min(n - 1, cur + step))
        else:
            target = self._geometric_target(cur, key)
        self._set_selected(target)

    def _geometric_target(self, cur: int, key: int) -> int:
        """Up/Down/PageUp/PageDown in the grid: nearest card in the row above/below."""
        here = self._slots[cur].rect
        cx = here.center().x()
        K = Qt.Key
        if key in (K.Key_Up, K.Key_Down):
            if key == K.Key_Up:
                cands = [i for i, s in enumerate(self._slots) if s.rect.top() < here.top() - 1]
                if not cands:
                    return cur
                row_top = max(self._slots[i].rect.top() for i in cands)
            else:
                cands = [i for i, s in enumerate(self._slots) if s.rect.top() > here.top() + 1]
                if not cands:
                    return cur
                row_top = min(self._slots[i].rect.top() for i in cands)
            row = [i for i in cands if abs(self._slots[i].rect.top() - row_top) < 1]
            return min(row, key=lambda i: abs(self._slots[i].rect.center().x() - cx))
        target_y = here.center().y() + (-1 if key == K.Key_PageUp else 1) * self.viewport().height()
        return min(range(len(self._slots)),
                   key=lambda i: (abs(self._slots[i].rect.center().y() - target_y),
                                  abs(self._slots[i].rect.center().x() - cx)))


# ==========================================================================
# dialogs
# ==========================================================================

class ConfirmRemoveDialog(QDialog):
    """「从书架移除《X》？文件本身不会被删除。」 with 不再询问 and [取消] [移除]."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ConfirmRemoveDialog")
        self._title = title
        self.setModal(True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.setSpacing(14)
        self.message = QLabel(self)
        self.message.setWordWrap(True)
        self.message.setMinimumWidth(360)
        self.message.setMaximumWidth(440)
        lay.addWidget(self.message)
        self.dont_ask = QCheckBox(self)
        lay.addWidget(self.dont_ask)
        row = QHBoxLayout()
        row.addStretch(1)
        self.cancel_btn = QPushButton(self)
        self.remove_btn = QPushButton(self)
        self.remove_btn.setProperty("erRole", "primary")
        self.remove_btn.setDefault(True)
        self.cancel_btn.clicked.connect(self.reject)
        self.remove_btn.clicked.connect(self.accept)
        row.addWidget(self.cancel_btn)
        row.addWidget(self.remove_btn)
        lay.addLayout(row)
        language_changed.subscribe(self.retranslate_ui)
        self.retranslate_ui()

    def retranslate_ui(self) -> None:
        self.setWindowTitle(S("lib.remove.title"))
        self.message.setText(S("lib.remove.confirm", title=self._title))
        self.dont_ask.setText(S("lib.remove.dont_ask"))
        self.cancel_btn.setText(S("common.cancel"))
        self.remove_btn.setText(S("lib.remove.yes"))


class BookInfoDialog(QDialog):
    """書籍信息: the shelf entry's metadata, file facts and reading facts.

    Non-modal.  The path sits on its own selectable line.  Description and
    subjects are read from the book in a worker when the dialog opens.
    """

    copyPathRequested = Signal(str)

    def __init__(self, entry: dict, parent: QWidget | None = None, *, seconds_read: int = 0) -> None:
        super().__init__(parent)
        self.setObjectName("BookInfoDialog")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._entry = dict(entry)
        self._seconds = max(int(entry.get("seconds_read") or 0), int(seconds_read or 0))
        self._extra: dict[str, Any] = {}
        self.setFixedWidth(560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.setSpacing(6)
        self.title_label = QLabel(self)
        self.title_label.setProperty("erRole", "title")
        self.title_label.setWordWrap(True)
        self.title_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.author_label = QLabel(self)
        self.author_label.setProperty("erRole", "secondary")
        self.author_label.setWordWrap(True)
        lay.addWidget(self.title_label)
        lay.addWidget(self.author_label)
        lay.addSpacing(10)
        self.grid_host = QWidget(self)
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(16)
        self.grid.setVerticalSpacing(7)
        self.grid.setColumnStretch(1, 1)
        lay.addWidget(self.grid_host)
        # the description can be long: a bounded, scrollable box instead of a label
        # (a word-wrapped QLabel in the grid got clipped mid-line at the dialog bottom)
        self.desc_title = QLabel(self)
        self.desc_title.setProperty("erRole", "secondary")
        self.desc = QPlainTextEdit(self)
        self.desc.setObjectName("BookInfoDescription")
        self.desc.setReadOnly(True)
        self.desc.setFixedHeight(128)
        self.desc.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        lay.addSpacing(8)
        lay.addWidget(self.desc_title)
        lay.addWidget(self.desc)
        self.desc_title.hide()
        self.desc.hide()
        lay.addSpacing(12)
        row = QHBoxLayout()
        row.addStretch(1)
        self.copy_btn = QPushButton(self)
        self.close_btn = QPushButton(self)
        self.close_btn.setDefault(True)
        self.copy_btn.clicked.connect(lambda: self.copyPathRequested.emit(self._entry.get("path") or ""))
        self.close_btn.clicked.connect(self.close)
        row.addWidget(self.copy_btn)
        row.addWidget(self.close_btn)
        lay.addLayout(row)
        language_changed.subscribe(self.retranslate_ui)
        self.retranslate_ui()

    def set_details(self, info: dict) -> None:
        """Description/subjects read from the book (called by the page's worker)."""
        self._extra = dict(info or {})
        self.retranslate_ui()

    def rows(self) -> list[tuple[str, str]]:
        """``(label, value)`` pairs as currently shown (for tests)."""
        out = []
        for r in range(self.grid.rowCount()):
            a = self.grid.itemAtPosition(r, 0)
            b = self.grid.itemAtPosition(r, 1)
            if a and b and a.widget() and b.widget():
                out.append((a.widget().text(), b.widget().text()))
        return out

    def _values(self) -> list[tuple[str, str]]:
        e = self._entry
        lang = strings.current_language()
        loc = QLocale(strings.QT_LOCALE_NAMES.get(lang, "en_US"))
        authors = _author_list(e)
        rows: list[tuple[str, str]] = []

        def add(label_key: str, value: Any) -> None:
            text = _clean(value)
            if text:
                rows.append((S(label_key), text))

        if len(authors) > 1:
            rows.append((plural("info.authors", len(authors)), S("common.sep").join(authors)))
        add("info.publisher", e.get("publisher"))
        add("info.pubdate", e.get("pubdate"))
        code = _clean(e.get("language"))
        if code:
            ql = QLocale(code.replace("-", "_"))
            name = ql.nativeLanguageName() if ql.language() != QLocale.Language.C else ""
            add("info.language", name or code)
        add("info.identifier", e.get("identifier"))
        add("info.epub_version", e.get("epub_version"))
        if e.get("layout") in ("fixed", "reflowable"):
            add("info.layout", S("info.layout.fixed" if e["layout"] == "fixed" else "info.layout.reflowable"))
        if e.get("spine_count"):
            add("info.chapters", loc.toString(int(e["spine_count"])))
        if e.get("units_total"):
            add("info.units", S("info.units.value", n=loc.toString(int(e["units_total"]))))
        if e.get("size"):
            add("info.size", loc.formattedDataSize(int(e["size"]), 1,
                                                   QLocale.DataSizeFormat.DataSizeTraditionalFormat))
        add("info.added", format_date(e.get("added_at"), relative=False) if e.get("added_at") else "")
        add("info.opened", format_date(e.get("opened_at"), relative=False)
            if e.get("opened_at") else S("lib.never_opened"))
        prog = book_progress(e)
        add("info.progress", S("lib.badge.finished") if _is_finished(e)
            else (S("lib.progress", p=_percent(prog)) if prog > 0 else S("lib.progress.none")))
        if self._seconds > 0:
            add("info.reading_time", duration(self._seconds / 60.0, long=True))
        subjects = self._extra.get("subjects") or []
        if subjects:
            add("info.subjects", S("common.sep").join(subjects))
        add("info.path", e.get("path"))
        return rows

    def retranslate_ui(self) -> None:
        e = self._entry
        self.setWindowTitle(S("info.title"))
        self.title_label.setText(display_title(e) or S("lib.title.unknown"))
        authors = _author_list(e)
        self.author_label.setText(S("common.sep").join(authors) if authors else S("lib.author.unknown"))
        self.copy_btn.setText(S("info.copy_path"))
        self.close_btn.setText(S("common.close"))
        while self.grid.count():
            item = self.grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for r, (label, value) in enumerate(self._values()):
            lab = QLabel(label, self.grid_host)
            lab.setProperty("erRole", "secondary")
            lab.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
            val = QLabel(value, self.grid_host)
            val.setWordWrap(True)
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            val.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            val.setTextFormat(Qt.TextFormat.PlainText)
            self.grid.addWidget(lab, r, 0)
            self.grid.addWidget(val, r, 1)
        desc = _clean(self._extra.get("description") or "")
        self.desc_title.setText(S("info.description"))
        self.desc_title.setVisible(bool(desc))
        self.desc.setVisible(bool(desc))
        if desc and self.desc.toPlainText() != desc:
            self.desc.setPlainText(desc)
        self._fit_height()

    def _fit_height(self, *, again: bool = True) -> None:
        """Height for the fixed width, so wrapped rows (the path) are never clipped.

        Qt caches height-for-width in the outer layout until a posted LayoutRequest
        arrives, so both layouts are invalidated first and the fit is repeated on the
        next event-loop turn (verified: without this the dialog stayed at 308px for
        602px of content).
        """
        lay = self.layout()
        self.grid.invalidate()
        self.grid_host.updateGeometry()
        lay.invalidate()
        h = lay.totalHeightForWidth(self.width()) if lay.hasHeightForWidth() else -1
        h = max(h, lay.totalSizeHint().height() if h < 0 else 0, lay.totalMinimumSize().height())
        self.setFixedHeight(h)
        if again:
            QTimer.singleShot(0, self, lambda: self._fit_height(again=False))

    def description(self) -> str:
        return self.desc.toPlainText() if not self.desc.isHidden() else ""


# ==========================================================================
# page furniture: notice, drop overlay, suggestion card, empty state
# ==========================================================================

class _Notice(QFrame):
    """Transient message at the bottom of the page, with an optional action link."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("LibraryNotice")
        self.setProperty("erRole", "card")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 8, 10, 8)
        lay.setSpacing(12)
        self.label = QLabel(self)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setWordWrap(True)
        self.action_btn = QToolButton(self)
        self.action_btn.setObjectName("LibraryNoticeAction")
        self.action_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        lay.addWidget(self.label, 1)
        lay.addWidget(self.action_btn)
        self._text_fn: Callable[[], str] | None = None
        self._action_key: str | None = None
        self._action: Callable[[], None] | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.dismiss)
        self.action_btn.clicked.connect(self._run_action)
        self.hide()

    def show_message(self, text_fn: Callable[[], str], *, action_key: str | None = None,
                     action: Callable[[], None] | None = None, timeout_ms: int = 4000) -> None:
        self._text_fn = text_fn
        self._action_key = action_key
        self._action = action
        self.retranslate_ui()
        self.show()
        self.raise_()
        if timeout_ms > 0:
            self._timer.start(timeout_ms)
        else:
            self._timer.stop()

    def text(self) -> str:
        return self.label.text() if self.isVisible() else ""

    def dismiss(self) -> None:
        self._timer.stop()
        self._text_fn = None
        self._action = None
        self.hide()

    def _run_action(self) -> None:
        fn = self._action
        self.dismiss()
        if fn is not None:
            fn()

    def retranslate_ui(self) -> None:
        if self._text_fn is not None:
            self.label.setText(self._text_fn())
        if self._action_key:
            self.action_btn.setText(S(self._action_key))
            self.action_btn.show()
        else:
            self.action_btn.hide()
        self.reposition()

    def reposition(self) -> None:
        """One line when it fits (a wrapped QLabel would size itself far too narrow),
        wrapped at the page width minus 48px otherwise; bottom-centre of the page."""
        parent = self.parentWidget()
        if parent is None:
            return
        lay = self.layout()
        m = lay.contentsMargins()
        max_w = max(200, min(720, parent.width() - 48))
        action_w = (self.action_btn.sizeHint().width() + lay.spacing()) if not self.action_btn.isHidden() else 0
        text_w = int(self.label.fontMetrics().horizontalAdvance(self.label.text())) + 4
        natural = m.left() + m.right() + text_w + action_w
        wrap = natural > max_w
        self.label.setWordWrap(wrap)
        w = min(natural, max_w)
        self.label.setFixedWidth(max(40, w - m.left() - m.right() - action_w))
        h = self.label.heightForWidth(self.label.width()) if wrap else self.label.sizeHint().height()
        h = max(h, self.action_btn.sizeHint().height() if action_w else 0) + m.top() + m.bottom()
        self.setGeometry((parent.width() - w) // 2, parent.height() - h - 20, w, h)


class _DropOverlay(QWidget):
    """「松开以添加到书架」 over the whole page while files are dragged over it."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("LibraryDropOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.theme: Any = theme_mod.LIGHT
        self.hide()

    def paintEvent(self, event: Any) -> None:  # noqa: N802
        t = self.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        bg = QColor(t.bg)
        bg.setAlphaF(0.90)
        p.fillRect(self.rect(), bg)
        pen = QPen(QColor(t.accent), 2)
        pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(16, 16, -16, -16), 12, 12)
        f = QFont(self.font())
        f.setPixelSize(20)
        f.setWeight(QFont.Weight.DemiBold)
        p.setFont(f)
        p.setPen(QColor(t.fg))
        p.drawText(QRectF(self.rect()).adjusted(40, 40, -40, -40),
                   int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap), S("lib.drop.hint"))
        p.end()

    def retranslate_ui(self) -> None:
        self.update()


class _SuggestionCard(QFrame):
    """「在 X 中发现 N 本书」 [添加到书架] [不用了]."""

    addClicked = Signal()
    dismissClicked = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("FoundBooksCard")
        self.setProperty("erRole", "card")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 10, 12, 10)
        lay.setSpacing(10)
        self.label = QLabel(self)
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.add_btn = QPushButton(self)
        self.add_btn.setObjectName("FoundBooksAdd")
        self.add_btn.setProperty("erRole", "primary")
        self.dismiss_btn = QPushButton(self)
        self.dismiss_btn.setObjectName("FoundBooksDismiss")
        lay.addWidget(self.label, 1)
        lay.addWidget(self.add_btn)
        lay.addWidget(self.dismiss_btn)
        self.add_btn.clicked.connect(self.addClicked)
        self.dismiss_btn.clicked.connect(self.dismissClicked)
        self.folder = ""
        self.paths: list[str] = []

    def set_found(self, folder: str, paths: list[str]) -> None:
        self.folder = folder
        self.paths = list(paths)
        self.retranslate_ui()

    def retranslate_ui(self) -> None:
        if self.paths:
            self.label.setText(plural("lib.found", len(self.paths), folder=_short_folder(self.folder)))
            self.label.setToolTip(self.folder)
        self.add_btn.setText(S("lib.found.add"))
        self.dismiss_btn.setText(S("lib.found.dismiss"))


class _EmptyState(QWidget):
    """Centred, three lines, nothing else (product spec §3 EMPTY LIBRARY)."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("LibraryEmptyState")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setProperty("erRole", "page")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 64)
        lay.addStretch(1)
        self.title = QLabel(self)
        self.title.setProperty("erRole", "title")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title.setWordWrap(True)
        self.body = QLabel(self)
        self.body.setProperty("erRole", "body")
        self.body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.body.setWordWrap(True)
        lay.addWidget(self.title)
        lay.addSpacing(8)
        lay.addWidget(self.body)
        lay.addSpacing(18)
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addStretch(1)
        self.open_btn = QPushButton(self)
        self.open_btn.setObjectName("EmptyOpenBook")
        self.open_btn.setProperty("erRole", "primary")
        self.folder_btn = QPushButton(self)
        self.folder_btn.setObjectName("EmptyAddFolder")
        row.addWidget(self.open_btn)
        row.addWidget(self.folder_btn)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(1)

    def retranslate_ui(self) -> None:
        self.title.setText(S("lib.empty.title"))
        self.body.setText(S("lib.empty.body"))
        self.open_btn.setText(S("lib.open"))
        self.folder_btn.setText(S("lib.add_folder"))


# ==========================================================================
# the page
# ==========================================================================

def _reveal_path(path: str) -> bool:
    """Show *path* selected in Explorer (or open its folder when the file is gone)."""
    if path and os.path.isfile(path):
        if sys.platform == "win32":
            subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"')
            return True
        return QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))
    folder = os.path.dirname(path or "")
    if folder and os.path.isdir(folder):
        return QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
    return False


class LibraryPage(QWidget):
    """The shelf (CONTRACT §7).

    Signals out:
      ``openBook(path)``    a book should be opened (double-click, Enter, menu, single-file add)
      ``removeBook(book_id)`` emitted AFTER the page removed the entry via ``Store.library_remove``
      ``statusMessage(text)`` every transient notice the page shows, for an app status bar
    """

    openBook = Signal(str)
    removeBook = Signal(str)
    statusMessage = Signal(str)

    def __init__(self, store: Store, parent: QWidget | None = None, *, theme: Any = None) -> None:
        super().__init__(parent)
        self.setObjectName("LibraryPage")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setProperty("erRole", "page")
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._store = store
        self._theme = theme if theme is not None else theme_mod.resolve_theme(store.get("reader.theme", "system"))
        sort = store.get(SETTING_SORT, "recent")
        self._sort = sort if sort in SORT_KEYS else "recent"
        view = store.get(SETTING_VIEW, "grid")
        self._view_mode = view if view in VIEW_MODES else "grid"
        self._query = ""
        self._entries: list[dict] = []
        self._cover_state: dict[str, str] = {}
        self._cancel = threading.Event()
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(max(2, min(4, (os.cpu_count() or 2) // 2)))
        self._emitter = _Emitter()
        self._emitter.cover.connect(self._on_cover)
        self._emitter.added.connect(self._on_added)
        self._emitter.checked.connect(self._on_checked)
        self._emitter.details.connect(self._on_details)
        self._token = 0
        self._pending_adds: dict[int, dict[str, Any]] = {}
        self._info_dialogs: dict[str, BookInfoDialog] = {}
        self._last_dir = ""
        self._compact_level = -1
        self._compact_needs: list[int] = []
        self._shut = False
        self._shown_once = False
        #: Replaceable for tests: called with a path; must not raise.
        self.reveal_handler: Callable[[str], Any] = _reveal_path
        self._build_ui()
        self.apply_theme(self._theme)
        language_changed.subscribe(self.retranslate_ui)
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)
        self.retranslate_ui()
        self.refresh()

    # -- construction ---------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.topbar = QFrame(self)
        self.topbar.setObjectName("LibraryTopBar")
        self.topbar.setProperty("erRole", "toolbar")
        self.topbar.setFixedHeight(52)
        # An explicit minimum stops the layout from imposing the full-label width as
        # the page minimum (verified: Japanese labels held the page at 738px when the
        # window asked for 720); _update_compact() switches to icons instead.
        self.topbar.setMinimumWidth(1)
        bar = QHBoxLayout(self.topbar)
        bar.setContentsMargins(12, 6, 12, 6)
        bar.setSpacing(8)
        self.open_btn = QPushButton(self.topbar)
        self.open_btn.setObjectName("LibraryOpenBook")
        self.open_btn.setProperty("erRole", "primary")
        self.folder_btn = QPushButton(self.topbar)
        self.folder_btn.setObjectName("LibraryAddFolder")
        for b in (self.open_btn, self.folder_btn):
            b.setIconSize(QSize(16, 16))
            b.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.search = QLineEdit(self.topbar)
        self.search.setObjectName("LibrarySearch")
        self.search.setClearButtonEnabled(True)
        self.search.setAcceptDrops(False)   # a dropped file adds a book, it is not typed
        self.search.setMinimumWidth(150)
        self.search.setMaximumWidth(320)
        self.search.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._search_icon = self.search.addAction(QIcon(), QLineEdit.ActionPosition.LeadingPosition)
        self.sort_combo = QComboBox(self.topbar)
        self.sort_combo.setObjectName("LibrarySort")
        self.sort_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.sort_combo.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.grid_btn = QToolButton(self.topbar)
        self.grid_btn.setObjectName("LibraryViewGrid")
        self.list_btn = QToolButton(self.topbar)
        self.list_btn.setObjectName("LibraryViewList")
        self._view_group = QButtonGroup(self)
        self._view_group.setExclusive(True)
        for b in (self.grid_btn, self.list_btn):
            b.setCheckable(True)
            b.setProperty("erRole", "segment")
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            b.setIconSize(QSize(16, 16))
            b.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            self._view_group.addButton(b)
        toggle = QWidget(self.topbar)
        tl = QHBoxLayout(toggle)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(0)
        tl.addWidget(self.grid_btn)
        tl.addWidget(self.list_btn)
        self._toggle_host = toggle
        # "…": the app shell fills this menu (language, theme, shortcuts, about, exit),
        # so the shelf reaches the same things as the reader's overflow menu
        self.more_btn = QToolButton(self.topbar)
        self.more_btn.setObjectName("LibraryMore")
        self.more_btn.setAutoRaise(True)
        self.more_btn.setIconSize(QSize(20, 20))
        self.more_btn.setFixedSize(34, 34)
        self.more_btn.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.more_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.more_menu = QMenu(self.more_btn)
        self.more_menu.setObjectName("LibraryMoreMenu")
        self.more_btn.setMenu(self.more_menu)
        bar.addWidget(self.open_btn)
        bar.addWidget(self.folder_btn)
        # stretch 0 spacer + stretch 1 search: the search box takes spare width first
        # (up to its 320px cap), the spacer only what is left after that
        bar.addStretch(0)
        bar.addWidget(self.search, 1)
        bar.addWidget(self.sort_combo)
        bar.addWidget(toggle)
        bar.addWidget(self.more_btn)
        root.addWidget(self.topbar)

        self._card_host = QWidget(self)
        self._card_host.setObjectName("FoundBooksHost")
        self._card_host.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._card_host.setProperty("erRole", "page")
        ch = QVBoxLayout(self._card_host)
        ch.setContentsMargins(24, 14, 24, 2)
        self.card = _SuggestionCard(self._card_host)
        ch.addWidget(self.card)
        self._card_host.hide()
        root.addWidget(self._card_host)

        self._body = QWidget(self)
        self._stack = QStackedLayout(self._body)
        self.empty = _EmptyState(self._body)
        self.view = ShelfView(self._body)
        self._stack.addWidget(self.empty)
        self._stack.addWidget(self.view)
        root.addWidget(self._body, 1)

        self.notice = _Notice(self)
        self.overlay = _DropOverlay(self)

        # wiring
        self.open_btn.clicked.connect(self.open_book_dialog)
        self.folder_btn.clicked.connect(self.add_folder_dialog)
        self.empty.open_btn.clicked.connect(self.open_book_dialog)
        self.empty.folder_btn.clicked.connect(self.add_folder_dialog)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(60)
        self._search_timer.timeout.connect(self._apply_query)
        self.search.textChanged.connect(lambda _t: self._search_timer.start())
        self.search.installEventFilter(self)
        self.sort_combo.activated.connect(
            lambda i: self.set_sort(self.sort_combo.itemData(i) or "recent"))
        self.grid_btn.clicked.connect(lambda: self.set_view_mode("grid"))
        self.list_btn.clicked.connect(lambda: self.set_view_mode("list"))
        self.view.activated.connect(self.open_book)
        self.view.contextRequested.connect(self.show_context_menu)
        self.view.removeRequested.connect(lambda bid: self.remove_book(bid))
        self.card.addClicked.connect(self._accept_suggestion)
        self.card.dismissClicked.connect(self._dismiss_suggestion)
        self.view.set_mode(self._view_mode)
        (self.grid_btn if self._view_mode == "grid" else self.list_btn).setChecked(True)
        self.setFocusProxy(self.view)

    # -- theme and language ---------------------------------------------------
    def apply_theme(self, theme: Any) -> None:
        """Adopt a :class:`theme.Theme` (connect ``ThemeController.themeChanged`` here).

        The QSS-styled widgets follow the application stylesheet; this repaints
        the painted shelf, generated covers, overlay and the theme-coloured icons.
        """
        self._theme = theme
        self.view.set_theme(theme)
        self.overlay.theme = theme
        self.overlay.update()
        dpr = self.devicePixelRatioF() or 1.0
        fg = QColor(theme.chrome_fg)
        self.open_btn.setIcon(_make_icon("book", QColor(theme.on_accent), 16, dpr))
        self.folder_btn.setIcon(_make_icon("folder", fg, 16, dpr))
        self.grid_btn.setIcon(_make_icon("grid", fg, 16, dpr))
        self.list_btn.setIcon(_make_icon("list", fg, 16, dpr))
        self.more_btn.setIcon(_make_icon("more", fg, 20, dpr))
        self._search_icon.setIcon(_make_icon("search", QColor(theme.chrome_secondary), 16, dpr))

    def theme(self) -> Any:
        return self._theme

    def retranslate_ui(self) -> None:
        """Re-render every visible string in the current UI language."""
        self.open_btn.setText(S("lib.open"))
        self.open_btn.setToolTip(tip("lib.open", "lib_open_book"))
        self.folder_btn.setText(S("lib.add_folder"))
        self.folder_btn.setToolTip(S("lib.add_folder"))
        self.search.setPlaceholderText(S("lib.search"))
        self.search.setToolTip(tip("lib.search", "lib_focus_search"))
        clear = self.search.findChild(QAction, "_q_qlineeditclearaction")
        if clear is not None:
            clear.setToolTip(S("lib.search.clear"))
        self.sort_combo.blockSignals(True)
        self.sort_combo.clear()
        for key in SORT_KEYS:
            self.sort_combo.addItem(S(SORT_LABEL_KEYS[key]), key)
        self.sort_combo.setCurrentIndex(max(0, self.sort_combo.findData(self._sort)))
        self.sort_combo.blockSignals(False)
        self.sort_combo.setToolTip(S("lib.sort"))
        self.grid_btn.setToolTip(S("lib.view.grid"))
        self.list_btn.setToolTip(S("lib.view.list"))
        self.more_btn.setToolTip(S("tb.more"))
        self.more_btn.setAccessibleName(S("tb.more"))
        self.empty.retranslate_ui()
        self.card.retranslate_ui()
        self.notice.retranslate_ui()
        self.overlay.retranslate_ui()
        self.view.retranslate_ui()
        if self._stack.currentWidget() is self.view and not self.view.slots():
            self.view.viewport().update()
        self._measure_compact()
        # the sort order of titles depends on the UI language's collation
        if self._sort in ("title", "author"):
            self._apply_filters()

    # -- responsive top bar -------------------------------------------------
    def _apply_compact(self, level: int) -> None:
        """0: all labels; 1: view toggle icon-only; 2: Open/Add Folder icon-only too."""
        grid_txt, list_txt = S("lib.view.grid"), S("lib.view.list")
        for b, txt in ((self.grid_btn, grid_txt), (self.list_btn, list_txt)):
            b.setText(txt)
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon if level < 1
                                 else Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.open_btn.setText(S("lib.open") if level < 2 else "")
        self.folder_btn.setText(S("lib.add_folder") if level < 2 else "")
        self._compact_level = level

    def _measure_compact(self) -> None:
        lay = self.topbar.layout()
        m = lay.contentsMargins()
        # compact labels before the search box drops below ~200px (its placeholder
        # would be cut to a few characters otherwise)
        fixed = (m.left() + m.right() + lay.spacing() * 6 + max(200, self.search.minimumWidth())
                 + self.more_btn.width())
        needs = []
        for level in (0, 1, 2):
            self._apply_compact(level)
            needs.append(fixed + self.open_btn.sizeHint().width() + self.folder_btn.sizeHint().width()
                         + self.sort_combo.sizeHint().width() + self._toggle_host.sizeHint().width())
        self._compact_needs = needs
        self._compact_level = -1
        self._update_compact()

    def _update_compact(self) -> None:
        if not self._compact_needs:
            return
        width = self.width() or self.topbar.width()
        level = 2
        for i, need in enumerate(self._compact_needs):
            if need <= width:
                level = i
                break
        if level != self._compact_level:
            self._apply_compact(level)

    def compact_level(self) -> int:
        return self._compact_level

    # -- data -----------------------------------------------------------------
    def refresh(self) -> None:
        """Re-read the shelf from the Store and repaint (call after a book closes)."""
        if self._shut:
            return
        self._entries = [dict(e) for e in self._store.library() if e.get("id")]
        self._apply_filters()
        self._schedule_covers()
        self._schedule_missing_check()

    def entries(self) -> list[dict]:
        return [dict(e) for e in self._entries]

    def _apply_query(self) -> None:
        text = self.search.text()
        if text != self._query:
            self._query = text
            self._apply_filters()

    def _apply_filters(self) -> None:
        books = sort_entries(filter_entries(self._entries, self._query), self._sort)
        cont = continue_entries(self._entries) if (not self._query.strip()
                                                   and self._view_mode == "grid") else []
        self.view.set_books([_Book.from_entry(e) for e in cont], [_Book.from_entry(e) for e in books],
                            query_active=bool(self._query.strip()))
        empty = not self._entries
        self._stack.setCurrentWidget(self.empty if empty else self.view)
        # the empty shelf keeps only the "…" menu in its bar (language, theme, exit)
        for w in (self.open_btn, self.folder_btn, self.search, self.sort_combo, self._toggle_host):
            w.setVisible(not empty)

    def _entry(self, bid: str) -> dict | None:
        for e in self._entries:
            if e.get("id") == bid:
                return e
        return None

    # -- covers ---------------------------------------------------------------
    def _schedule_covers(self) -> None:
        dpr = self.devicePixelRatioF() or 1.0
        order = [s.book.bid for s in self.view.slots()]
        seen: set[str] = set()
        for bid in order + [e["id"] for e in self._entries]:
            if bid in seen or bid in self._cover_state:
                continue
            seen.add(bid)
            e = self._entry(bid)
            if e is None:
                continue
            if e.get("cover") == "" or e.get("drm"):
                self._cover_state[bid] = "none"      # known to have no cover: generated
                continue
            self._cover_state[bid] = "pending"
            path, cover_file = str(e.get("path") or ""), self._store.cover_path(bid)
            self._pool.start(functools.partial(_cover_job, self._emitter, self._cancel, bid, path,
                                               cover_file, COVER_SIZE, dpr))

    def _on_cover(self, res: _CoverResult) -> None:
        if self._shut:
            return
        if res.image is not None and not res.image.isNull():
            self.view.covers[res.bid] = QPixmap.fromImage(res.image)
            self._cover_state[res.bid] = "image"
            if res.status == "made" and self._store.library_get(res.bid) is not None:
                self._store.library_update(res.bid, cover=f"covers/{res.bid}.jpg",
                                           cover_w=res.size[0], cover_h=res.size[1])
        elif res.status == "none":
            self._cover_state[res.bid] = "none"
            if self._store.library_get(res.bid) is not None:
                self._store.library_update(res.bid, cover="", cover_w=0, cover_h=0)
        else:
            # missing file or unreadable cover: generated now, retried on the next launch
            self._cover_state[res.bid] = res.status
        self.view.invalidate_book(res.bid)

    def cover_state(self, bid: str) -> str:
        """``pending`` | ``image`` | ``none`` | ``missing`` | ``error`` | ``''`` (for tests)."""
        return self._cover_state.get(bid, "")

    def wait_for_workers(self, timeout_ms: int = 30000) -> bool:
        """Block until queued cover/add jobs finish (tests and shutdown only)."""
        ok = self._pool.waitForDone(timeout_ms)
        QCoreApplication.processEvents()
        return ok

    def shutdown(self, timeout_ms: int = 3000) -> None:
        """Cancel queued jobs and wait briefly for running ones.  Called on quit."""
        if self._shut:
            return
        self._shut = True
        self._cancel.set()
        self._pool.clear()
        self._pool.waitForDone(timeout_ms)

    # -- missing files --------------------------------------------------------
    def _schedule_missing_check(self) -> None:
        pairs = [(e["id"], str(e.get("path") or "")) for e in self._entries]
        if pairs:
            self._pool.start(functools.partial(_missing_job, self._emitter, self._cancel, pairs))

    def _on_checked(self, result: dict) -> None:
        if self._shut:
            return
        changed = False
        for e in self._entries:
            bid = e["id"]
            if bid not in result:
                continue
            missing = not result[bid]
            if bool(e.get("missing")) != missing:
                e["missing"] = missing
                changed = True
                if self._store.library_get(bid) is not None:
                    self._store.library_update(bid, missing=missing)
        if changed:
            self._apply_filters()

    # -- selection / opening ----------------------------------------------------
    def selected_book_id(self) -> str:
        return self.view.selected_book_id()

    def select_book(self, bid: str) -> bool:
        return self.view.select_book(bid)

    def open_selected(self) -> None:
        bid = self.selected_book_id()
        if not bid and self.view.slots():
            bid = self.view.slots()[0].book.bid
        if bid:
            self.open_book(bid)

    def open_book(self, bid: str) -> None:
        """Emit ``openBook(path)`` for a shelf entry (missing files too: the reader relocates)."""
        e = self._store.library_get(bid) or self._entry(bid)
        if e and e.get("path"):
            self.openBook.emit(str(e["path"]))

    # -- keyboard -------------------------------------------------------------
    @staticmethod
    def _claims(key: int, mods: Any) -> bool:
        """The library keys this page takes before any QShortcut can (ShortcutOverride)."""
        K = Qt.Key
        if mods == Qt.KeyboardModifier.ControlModifier:
            return key == K.Key_F
        if mods == Qt.KeyboardModifier.NoModifier:
            return key in (K.Key_Slash, K.Key_Escape, K.Key_Delete, K.Key_F5,
                           K.Key_Return, K.Key_Enter)
        return False

    def event(self, event: Any) -> bool:  # noqa: N802
        # Claim the library's keys before any application-wide QShortcut sees them,
        # so a reader binding for Ctrl+F or Delete cannot steal them on this page.
        # Ctrl+O is deliberately NOT claimed: the app shell owns it (keyPressEvent
        # still handles it when nothing else does).
        if event.type() == QEvent.Type.ShortcutOverride and self.isVisible():
            mods = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
            if self._claims(event.key(), mods):
                event.accept()
                return True
        return super().event(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        key = event.key()
        mods = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        ctrl = Qt.KeyboardModifier.ControlModifier
        none = Qt.KeyboardModifier.NoModifier
        if (key == Qt.Key.Key_F and mods == ctrl) or (key == Qt.Key.Key_Slash and mods == none):
            self.focus_search()
        elif key == Qt.Key.Key_Escape and mods == none:
            self.clear_search_or_selection()
        elif key == Qt.Key.Key_F5 and mods == none:
            self.rescan()
        elif key == Qt.Key.Key_Delete and mods == none:
            self.remove_selected()
        elif key == Qt.Key.Key_O and mods == ctrl:
            self.open_book_dialog()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and mods == none:
            self.open_selected()
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down,
                     Qt.Key.Key_Home, Qt.Key.Key_End) and mods == none and self.view.slots():
            self.view.setFocus(Qt.FocusReason.OtherFocusReason)
            self.view.keyPressEvent(event)
            return
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        if obj is self.search and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key == Qt.Key.Key_Escape:
                if self.search.text():
                    self.search.clear()
                    self._apply_query()
                else:
                    self.view.setFocus(Qt.FocusReason.ShortcutFocusReason)
                return True
            if key == Qt.Key.Key_Down and self.view.slots():
                self._apply_query()
                self.view.setFocus(Qt.FocusReason.ShortcutFocusReason)
                if self.view.selected_index() < 0:
                    self.view.select_index(0)
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._apply_query()
                self.open_selected()
                return True
        return super().eventFilter(obj, event)

    def focus_search(self) -> None:
        """Ctrl+F or /: focus the search box and select its text."""
        self.search.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search.selectAll()

    def clear_search_or_selection(self) -> None:
        """Escape: clear the search first, then the selection."""
        if self.search.text():
            self.search.clear()
            self._apply_query()
        else:
            self.view.clear_selection()

    def set_search(self, text: str) -> None:
        self.search.setText(text)
        self._apply_query()

    # -- sort / view -------------------------------------------------------------
    def set_sort(self, key: str) -> None:
        """One of :data:`SORT_KEYS`; remembered in ``window.library_sort``."""
        if key not in SORT_KEYS:
            return
        self._sort = key
        idx = self.sort_combo.findData(key)
        if idx >= 0 and self.sort_combo.currentIndex() != idx:
            self.sort_combo.setCurrentIndex(idx)
        self._store.set(SETTING_SORT, key)
        self._apply_filters()

    def sort_key(self) -> str:
        return self._sort

    def set_view_mode(self, mode: str) -> None:
        """``'grid'`` (covers) or ``'list'``; remembered in ``window.library_view``."""
        if mode not in VIEW_MODES:
            return
        self._view_mode = mode
        (self.grid_btn if mode == "grid" else self.list_btn).setChecked(True)
        self._store.set(SETTING_VIEW, mode)
        self.view.set_mode(mode)
        self._apply_filters()

    def view_mode(self) -> str:
        return self._view_mode

    # -- adding books -----------------------------------------------------------
    def open_book_dialog(self) -> None:
        """Ctrl+O / Open Book…: pick files; one file is added and opened."""
        start = self._last_dir or os.path.expanduser("~")
        paths, _flt = QFileDialog.getOpenFileNames(
            self, S("dlg.open.title"), start, f"{S('dlg.open.filter')};;{S('dlg.all_files')}")
        if paths:
            self._last_dir = os.path.dirname(paths[0])
            self.add_paths(paths, open_single=True)

    def add_folder_dialog(self) -> None:
        """Add Folder…: pick a folder; its EPUBs are added and the folder is watched (F5)."""
        start = self._last_dir or os.path.expanduser("~")
        folder = QFileDialog.getExistingDirectory(self, S("dlg.folder.title"), start)
        if folder:
            self._last_dir = folder
            self.add_folder(folder)

    def add_folder(self, folder: str) -> None:
        self.add_paths([folder], open_single=False)

    def add_paths(self, paths: Iterable[str], *, open_single: bool = True) -> int:
        """Add files and/or folders to the shelf in a worker.  Returns a job token.

        A single ``.epub`` file (dialog or drop) is added and then opened
        (``openBook``); folders are walked recursively and remembered in
        ``behavior.watch_folders``.  Files are referenced in place, never copied.
        """
        paths = [os.path.abspath(p) for p in paths if p]
        if not paths:
            return 0
        if all(os.path.isfile(p) and not is_epub_path(p) for p in paths):
            self._notify(lambda: S("status.drop.unsupported"))
            return 0
        single = (len(paths) == 1 and os.path.isfile(paths[0]) and is_epub_path(paths[0]))
        folders = [p for p in paths if os.path.isdir(p)]
        if folders:
            self._remember_folders(folders)
        return self._start_add(paths, mode="files", open_after=single and open_single,
                               suggest=single, skip_known_paths=False)

    def _remember_folders(self, folders: list[str]) -> None:
        current = [str(f) for f in (self._store.get(SETTING_FOLDERS) or [])]
        seen = {_norm_path(f) for f in current}
        new = list(current)
        for f in folders:
            if _norm_path(f) not in seen:
                seen.add(_norm_path(f))
                new.append(os.path.abspath(f))
        if new != current:
            self._store.set(SETTING_FOLDERS, new)

    def watched_folders(self) -> list[str]:
        return [str(f) for f in (self._store.get(SETTING_FOLDERS) or [])]

    def _known(self) -> list[_Known]:
        out = []
        for e in self._store.library():
            if e.get("id"):
                path = str(e.get("path") or "")
                out.append(_Known(e["id"], path, _norm_path(path), e.get("size"), e.get("mtime_ns"),
                                  bool(e.get("missing"))))
        return out

    def _start_add(self, paths: list[str], *, mode: str, open_after: bool, suggest: bool,
                   skip_known_paths: bool) -> int:
        self._token += 1
        token = self._token
        scans = mode == "rescan" or any(os.path.isdir(p) for p in paths)
        self._pending_adds[token] = {"mode": mode, "scans": scans, "n": len(paths)}
        # only say "working" when it is slower than 300 ms (product spec tone rule)
        QTimer.singleShot(300, self, lambda: self._busy_notice(token))
        self._pool.start(functools.partial(
            _add_job, self._emitter, self._cancel, self._store, token, mode, list(paths),
            self._known(), open_after, suggest, skip_known_paths))
        return token

    def _busy_notice(self, token: int) -> None:
        job = self._pending_adds.get(token)
        if job is None or self._shut:
            return
        if job["scans"]:
            self._notify(lambda: S("lib.rescanning"), timeout_ms=0)
        else:
            self._notify(lambda n=job["n"]: plural("lib.adding", n), timeout_ms=0)

    def is_busy(self) -> bool:
        return bool(self._pending_adds)

    def _on_added(self, res: _AddResult) -> None:
        if self._shut:
            return
        self._pending_adds.pop(res.token, None)
        for entry in res.new_entries:
            self._store.library_upsert(entry)
        for rep in res.repairs:
            cur = self._store.library_get(rep["id"]) or {}
            history = list(cur.get("path_history") or [])
            if rep["old_path"] and rep["old_path"] not in history:
                history.append(rep["old_path"])
            self._store.library_upsert({"id": rep["id"], "path": rep["path"], "size": rep["size"],
                                        "mtime_ns": rep["mtime_ns"], "missing": False,
                                        "path_history": history, "verified_at": now_iso()})
        for bid in [e["id"] for e in res.new_entries] + [r["id"] for r in res.repairs]:
            self._cover_state.pop(bid, None)
        self.refresh()

        n_new, n_failed = len(res.new_entries), len(res.failed)
        parts: list[Callable[[], str]] = []
        if res.mode == "rescan":
            parts.append((lambda n=n_new: plural("lib.rescan.done", n)) if n_new
                         else (lambda: S("lib.rescan.none")))
        elif n_new:
            parts.append(lambda n=n_new: plural("lib.added", n))
        if res.repairs:
            parts.append(lambda: S("status.moved"))
        if n_failed:
            parts.append(lambda n=n_failed: plural("lib.add_failed", n))
        if res.mode != "rescan" and not parts:
            if res.files_found == 0:
                parts.append(lambda: S("status.drop.unsupported") if res.unsupported and not res.folders
                             else S("lib.added.none"))
            elif res.already and not res.open_bid:
                parts.append(lambda: S("lib.already"))
        if parts:
            self._notify(lambda ps=tuple(parts): S("common.sep").join(fn() for fn in ps))
        elif self.notice.isVisible() and self.notice._action is None:
            self.notice.dismiss()

        if res.open_path:
            if res.open_bid:
                self.view.select_book(res.open_bid)
            self.openBook.emit(res.open_path)
        elif res.new_entries:
            self.view.select_book(res.new_entries[0]["id"])
        elif res.already and res.mode == "files":
            self.view.select_book(res.already[0])
        if res.suggestion:
            self.offer_found_books(*res.suggestion)

    # -- rescanning (F5) ------------------------------------------------------
    def rescan(self) -> int:
        """F5: look for new EPUBs in the watched folders and re-check missing files."""
        folders = [f for f in self.watched_folders() if f]
        self._schedule_missing_check()
        if not folders:
            self._notify(lambda: S("lib.rescan.nofolders"))
            return 0
        return self._start_add(folders, mode="rescan", open_after=False, suggest=False,
                               skip_known_paths=True)

    # -- found-books suggestion -----------------------------------------------
    def offer_found_books(self, folder: str, paths: Iterable[str]) -> bool:
        """Show 「在 X 中发现 N 本书」 for EPUBs the user has not added.

        Nothing is scanned or added here: *paths* come from the caller (the page
        itself only offers the other EPUBs sitting next to a file the user just
        chose to add).  Folders the user answered "No Thanks" for, folders
        already watched, and books already on the shelf are never offered.
        Returns True when the card is shown.
        """
        folder = os.path.abspath(folder)
        nf = _norm_path(folder)
        dismissed = {_norm_path(str(f)) for f in (self._store.get(SETTING_FOUND_DISMISSED) or [])}
        watched = {_norm_path(f) for f in self.watched_folders()}
        if nf in dismissed or nf in watched:
            return False
        known = {k.norm for k in self._known()}
        fresh = [os.path.abspath(p) for p in paths if p and _norm_path(p) not in known]
        if not fresh:
            return False
        self.card.set_found(folder, fresh)
        self._card_host.show()
        return True

    def suggestion(self) -> tuple[str, list[str]] | None:
        """``(folder, paths)`` while the found-books card is shown, else None."""
        if not self._card_host.isHidden() and self.card.paths:
            return (self.card.folder, list(self.card.paths))
        return None

    def _accept_suggestion(self) -> None:
        paths = [p for p in self.card.paths if os.path.isfile(p)]   # may have moved since
        self._card_host.hide()
        self.card.paths = []
        if paths:
            self.add_paths(paths, open_single=False)

    def _dismiss_suggestion(self) -> None:
        folder = self.card.folder
        self._card_host.hide()
        self.card.paths = []
        if folder:
            current = [str(f) for f in (self._store.get(SETTING_FOUND_DISMISSED) or [])]
            if _norm_path(folder) not in {_norm_path(f) for f in current}:
                self._store.set(SETTING_FOUND_DISMISSED, current + [os.path.abspath(folder)])

    # -- context menu and its actions -----------------------------------------
    def build_context_menu(self, bid: str) -> QMenu:
        """打开 / 在文件夹中显示 / 复制文件路径 / 书籍信息 / ─ / 从书架移除.

        There is deliberately no item that deletes, moves or renames the file.
        """
        e = self._entry(bid) or self._store.library_get(bid) or {}
        path = str(e.get("path") or "")
        menu = QMenu(self)
        menu.setObjectName("LibraryContextMenu")
        # Shortcut hints are display-only ("label\tkeys"): a real QAction shortcut on
        # a menu item would fire while the menu has focus and could hijack Enter.
        a = menu.addAction(f"{S('lib.ctx.open')}\t{strings.KEYS['lib_open'][0]}")
        a.setObjectName("ctx.open")
        a.triggered.connect(lambda: self.open_book(bid))
        a = menu.addAction(S("lib.ctx.reveal"))
        a.setObjectName("ctx.reveal")
        a.setEnabled(bool(path) and (os.path.isfile(path) or os.path.isdir(os.path.dirname(path))))
        a.triggered.connect(lambda: self.reveal_in_folder(bid))
        a = menu.addAction(S("lib.ctx.copypath"))
        a.setObjectName("ctx.copypath")
        a.setEnabled(bool(path))
        a.triggered.connect(lambda: self.copy_path(bid))
        a = menu.addAction(S("lib.ctx.info"))
        a.setObjectName("ctx.info")
        a.triggered.connect(lambda: self.show_book_info(bid))
        menu.addSeparator()
        a = menu.addAction(f"{S('lib.ctx.remove')}\t{strings.KEYS['lib_remove'][0]}")
        a.setObjectName("ctx.remove")
        a.triggered.connect(lambda: self.remove_book(bid))
        return menu

    def show_context_menu(self, bid: str, global_pos: QPoint) -> None:
        menu = self.build_context_menu(bid)
        menu.exec(global_pos)
        menu.deleteLater()

    def reveal_in_folder(self, bid: str) -> None:
        e = self._entry(bid) or self._store.library_get(bid)
        if e and e.get("path"):
            try:
                self.reveal_handler(str(e["path"]))
            except Exception as exc:
                _log.warning("show in folder failed: %r", exc)

    def copy_path(self, bid: str) -> str:
        e = self._entry(bid) or self._store.library_get(bid)
        path = str((e or {}).get("path") or "")
        if path:
            QGuiApplication.clipboard().setText(os.path.normpath(path))
            self._notify(lambda: S("lib.copied_path"))
        return path

    def show_book_info(self, bid: str) -> BookInfoDialog | None:
        e = self._store.library_get(bid) or self._entry(bid)
        if not e:
            return None
        old = self._info_dialogs.get(bid)
        if old is not None:
            try:
                old.raise_()
                old.activateWindow()
                return old
            except RuntimeError:
                self._info_dialogs.pop(bid, None)
        seconds = 0
        try:
            stats = (self._store.book_state(bid) or {}).get("stats") or {}
            seconds = int(stats.get("seconds_read") or 0)
        except Exception:
            pass
        dlg = BookInfoDialog(copy.deepcopy(e), self.window(), seconds_read=seconds)
        dlg.copyPathRequested.connect(lambda _p, b=bid: self.copy_path(b))
        dlg.destroyed.connect(lambda _o=None, b=bid: self._info_dialogs.pop(b, None))
        self._info_dialogs[bid] = dlg
        dlg.show()
        self._pool.start(functools.partial(_details_job, self._emitter, self._cancel, bid,
                                           str(e.get("path") or "")))
        return dlg

    def _on_details(self, payload: tuple[str, dict]) -> None:
        bid, info = payload
        dlg = self._info_dialogs.get(bid)
        if dlg is not None:
            try:
                dlg.set_details(info)
            except RuntimeError:
                self._info_dialogs.pop(bid, None)

    # -- removal ----------------------------------------------------------------
    def remove_selected(self) -> bool:
        bid = self.selected_book_id()
        return self.remove_book(bid) if bid else False

    def remove_book(self, bid: str, *, confirm: bool = True) -> bool:
        """Remove a book from the SHELF (never the file), after confirmation.

        The confirmation honours ``behavior.confirm_remove_from_shelf`` and its
        "Don't ask again" box.  The notice offers Undo, which restores the entry
        (``books/<id>.json`` is kept by the store, so notes come back too).
        Emits ``removeBook(book_id)`` after the store was updated.
        """
        entry = self._store.library_get(bid)
        if entry is None:
            return False
        title = display_title(entry) or S("lib.title.unknown")
        if confirm and self._store.get(SETTING_CONFIRM_REMOVE, True):
            dlg = ConfirmRemoveDialog(title, self)
            accepted = dlg.exec() == QDialog.DialogCode.Accepted
            dont_ask = dlg.dont_ask.isChecked()
            dlg.deleteLater()
            if not accepted:
                return False
            if dont_ask:
                self._store.set(SETTING_CONFIRM_REMOVE, False)
        saved = copy.deepcopy(entry)
        index = self.view.selected_index()
        self._store.library_remove(bid)
        self.refresh()
        if index >= 0 and self.view.slots():
            self.view.select_index(min(index, len(self.view.slots()) - 1))
        self.removeBook.emit(bid)
        self._notify(lambda: S("lib.removed", title=title), action_key="common.undo",
                     action=lambda: self._undo_remove(saved), timeout_ms=6000)
        return True

    def _undo_remove(self, saved: dict) -> None:
        self._store.library_upsert(saved)
        self.refresh()
        self.view.select_book(saved["id"])
        self._notify(lambda: S("common.undone"))

    # -- notices ------------------------------------------------------------------
    def _notify(self, text_fn: Callable[[], str], *, action_key: str | None = None,
                action: Callable[[], None] | None = None, timeout_ms: int = 4000) -> None:
        self.notice.show_message(text_fn, action_key=action_key, action=action, timeout_ms=timeout_ms)
        self.statusMessage.emit(self.notice.label.text())

    def notice_text(self) -> str:
        return self.notice.text()

    def notify(self, text_fn: Callable[[], str], *, timeout_ms: int = 4000) -> None:
        """Show a transient notice (a callable, so it re-renders on a language change)."""
        self._notify(text_fn, timeout_ms=timeout_ms)

    # -- drag and drop ----------------------------------------------------------------
    @staticmethod
    def _local_paths(mime: Any) -> list[str]:
        if mime is None or not mime.hasUrls():
            return []
        return [u.toLocalFile() for u in mime.urls() if u.isLocalFile() and u.toLocalFile()]

    def dragEnterEvent(self, event: Any) -> None:  # noqa: N802
        if self._local_paths(event.mimeData()):
            event.acceptProposedAction()
            self.overlay.setGeometry(self.rect())
            self.overlay.show()
            self.overlay.raise_()
        else:
            event.ignore()

    def dragMoveEvent(self, event: Any) -> None:  # noqa: N802
        if self._local_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event: Any) -> None:  # noqa: N802
        self.overlay.hide()
        super().dragLeaveEvent(event)

    def dropEvent(self, event: Any) -> None:  # noqa: N802
        self.overlay.hide()
        paths = self._local_paths(event.mimeData())
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        # return to the drag source first; the add itself runs in a worker
        QTimer.singleShot(0, self, lambda: self.add_paths(paths, open_single=True))

    # -- geometry --------------------------------------------------------------------
    def resizeEvent(self, event: Any) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.overlay.setGeometry(self.rect())
        if self.notice.isVisible():
            self.notice.reposition()
        self._update_compact()

    def showEvent(self, event: Any) -> None:  # noqa: N802
        super().showEvent(event)
        self._update_compact()
        if not event.spontaneous() and self._shown_once:
            # back from the reader: progress / opened_at changed in the store
            self.refresh()
        self._shown_once = True
