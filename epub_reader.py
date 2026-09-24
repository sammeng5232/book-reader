# -*- coding: utf-8 -*-
"""Book Reader — application entry point (owner G, CONTRACT §8).

Run it from source with ``run.ps1`` (or the 3.14 interpreter directly)::

    python epub_reader.py [book.epub]
    python epub_reader.py --help

What this module does, in start-up order:

1. parses the command line (``argv[1]`` is a book, ``--help`` prints usage);
2. migrates a development build's state directory (``store.migrate_legacy_dirs``)
   and opens the rotating log at ``store.log_file()``;
3. installs the exception hooks: an unhandled exception is logged and shown as
   a non-fatal in-window card instead of killing the app;
4. registers the ``epub://`` scheme (``webhost``, before ``QApplication``),
   sets the HiDPI rounding policy and creates the application;
5. hands the book to an already running instance over ``QLocalServer``
    (``store.PIPE_NAME``, message ``OPEN <path>``) and exits, or becomes the
    running instance itself;
6. builds :class:`MainWindow` — browser-style tabs over ``LibraryPage`` and
    any number of ``ReaderPage`` s — restores the window geometry and reopens
    the last session (every tab that was open), the one book given on the
    command line, or the shelf;
7. binds the whole keyboard map of product spec §3b in ONE place
   (:class:`KeyRouter`) and, in debug mode, refuses to start if any key
   sequence is bound twice;
8. on quit, saves the window state, closes the book and flushes the Store.

All user-facing text comes from ``strings.S()``; the language is
``ui.language`` (``auto`` follows Windows) and switches live.
"""

from __future__ import annotations

import ctypes
import json
import logging
import logging.handlers
import os
import sys
import threading
import time
import traceback
import uuid
import weakref
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

import webhost  # registers epub:// at import time; must precede QApplication

from PySide6.QtCore import (
    QCoreApplication,
    QEvent,
    QLibraryInfo,
    QLocale,
    QMimeData,
    QObject,
    QPoint,
    QRect,
    QSize,
    Qt,
    QTimer,
    QTranslator,
    QUrl,
    Signal,
    qInstallMessageHandler,
    QtMsgType,
    qVersion,
)
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QCursor,
    QDesktopServices,
    QDrag,
    QFont,
    QGuiApplication,
    QIcon,
    QKeyEvent,
    QMouseEvent,
)
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QTabBar,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

import bookformats
import store as store_mod
import strings
import theme as theme_mod
from library_page import LibraryPage
from reader_page import CheatSheet, ReaderPage
from store import APP_DIR_NAME, PIPE_NAME, Store
from strings import KEYS, S

__all__ = [
    "APP_VERSION",
    "MIN_SIZE",
    "DEFAULT_SIZE",
    "Binding",
    "KeyRouter",
    "build_bindings",
    "audit_bindings",
    "parse_combo",
    "SingleInstance",
    "CrashCard",
    "AboutDialog",
    "MainWindow",
    "parse_args",
    "help_text",
    "setup_logging",
    "install_exception_hooks",
    "main",
]

APP_VERSION: str = str(store_mod.DEFAULT_SETTINGS.get("version") or "1.0.0")
MIN_SIZE: tuple[int, int] = (720, 520)          # product spec §3
DEFAULT_SIZE: tuple[int, int] = (1280, 900)
APP_USER_MODEL_ID = "BookReader.App.1"
LOG_MAX_BYTES = 1 << 20
LOG_BACKUPS = 3
FORWARD_TIMEOUT_MS = 2500
BACK_TO_LIBRARY_NOTE_MS = 2000                  # spec §3b conflict audit: Ctrl+W flash

log = logging.getLogger("epub_reader")


def _iv(x: Any) -> int:
    """An int from a PySide6 enum/flag or an int."""
    return int(getattr(x, "value", x))


def _debug_default() -> bool:
    """Debug mode: running from source (``__debug__``), or EPUB_READER_DEBUG=1."""
    if os.environ.get("EPUB_READER_DEBUG", "").strip() in ("1", "true", "yes"):
        return True
    return bool(__debug__) and not getattr(sys, "frozen", False)


# ==========================================================================
# logging and exception hooks
# ==========================================================================

_LOG_HANDLER: logging.Handler | None = None


def setup_logging(root: str | None = None, *, debug: bool = False) -> str:
    """Open ``<state root>\\logs\\book-reader.log`` (1 MB x 3) and return its path.

    Idempotent: a second call does not add a second handler.  Never raises; if
    the log directory cannot be created the app still runs, logging to stderr.
    """
    global _LOG_HANDLER
    path = store_mod.log_file(root)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG if debug else logging.INFO)
    if _LOG_HANDLER is not None:
        return path
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8", delay=True)
    except OSError:
        handler = logging.StreamHandler(sys.stderr) if sys.stderr else logging.NullHandler()
    handler.setFormatter(fmt)
    root_logger.addHandler(handler)
    _LOG_HANDLER = handler
    if debug and sys.stderr is not None and isinstance(handler, logging.handlers.RotatingFileHandler):
        echo = logging.StreamHandler(sys.stderr)
        echo.setLevel(logging.WARNING)
        echo.setFormatter(fmt)
        root_logger.addHandler(echo)
    return path


_QT_LEVELS = {
    QtMsgType.QtDebugMsg: logging.DEBUG,
    QtMsgType.QtInfoMsg: logging.INFO,
    QtMsgType.QtWarningMsg: logging.WARNING,
    QtMsgType.QtCriticalMsg: logging.ERROR,
    QtMsgType.QtFatalMsg: logging.CRITICAL,
}


def _qt_message_handler(mode: Any, _context: Any, message: str) -> None:
    logging.getLogger("qt").log(_QT_LEVELS.get(mode, logging.WARNING), "%s", message)


class _ErrorRelay(QObject):
    """Carries an error report from any thread to the GUI thread."""

    report = Signal(str)


_RELAY: _ErrorRelay | None = None
_IN_HOOK = threading.local()


def _report(text: str) -> None:
    """Show *text* on the current window's error card (GUI thread only)."""
    win = MainWindow.current()
    if win is None:
        return
    try:
        win.show_internal_error(text)
    except Exception:  # noqa: BLE001 - the error card must never crash the app itself
        log.exception("could not show the error card")


def _excepthook(etype: type[BaseException], value: BaseException, tb: Any) -> None:
    if issubclass(etype, KeyboardInterrupt):
        sys.__excepthook__(etype, value, tb)
        return
    text = "".join(traceback.format_exception(etype, value, tb))
    try:
        log.error("unhandled exception\n%s", text)
    except Exception:  # noqa: BLE001
        pass
    if sys.stderr is not None:
        try:
            sys.stderr.write(text)
        except Exception:  # noqa: BLE001
            pass
    if getattr(_IN_HOOK, "busy", False):
        return
    _IN_HOOK.busy = True
    try:
        if _RELAY is not None and QCoreApplication.instance() is not None:
            _RELAY.report.emit(text)      # queued when raised off the GUI thread
    finally:
        _IN_HOOK.busy = False


def _thread_excepthook(args: Any) -> None:
    if args.exc_type is SystemExit:
        return
    _excepthook(args.exc_type, args.exc_value, args.exc_traceback)


def install_exception_hooks() -> None:
    """Route unhandled exceptions (GUI thread, worker threads, Qt messages) to the log.

    On the GUI thread the current :class:`MainWindow` shows a non-fatal error
    card with the log location; the app keeps running.
    """
    global _RELAY
    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    if _RELAY is None and QCoreApplication.instance() is not None:
        _RELAY = _ErrorRelay()
        _RELAY.report.connect(_report, Qt.ConnectionType.QueuedConnection)
    qInstallMessageHandler(_qt_message_handler)


# ==========================================================================
# the keyboard map (product spec §3b), bound in one place
# ==========================================================================

_K = Qt.Key
_NAMED_KEYS: dict[str, int] = {
    "→": _iv(_K.Key_Right), "←": _iv(_K.Key_Left), "↑": _iv(_K.Key_Up), "↓": _iv(_K.Key_Down),
    "Esc": _iv(_K.Key_Escape), "Enter": _iv(_K.Key_Return), "Space": _iv(_K.Key_Space),
    "PageDown": _iv(_K.Key_PageDown), "PageUp": _iv(_K.Key_PageUp),
    "Home": _iv(_K.Key_Home), "End": _iv(_K.Key_End), "Delete": _iv(_K.Key_Delete),
    "/": _iv(_K.Key_Slash), ",": _iv(_K.Key_Comma), "=": _iv(_K.Key_Equal),
    "-": _iv(_K.Key_Minus), "Plus": _iv(_K.Key_Plus), "Tab": _iv(_K.Key_Tab),
}
for _n in range(1, 13):
    _NAMED_KEYS[f"F{_n}"] = _iv(_K.Key_F1) + _n - 1
for _n in range(10):
    _NAMED_KEYS[str(_n)] = _iv(_K.Key_0) + _n

CTRL = _iv(Qt.KeyboardModifier.ControlModifier)
SHIFT = _iv(Qt.KeyboardModifier.ShiftModifier)
ALT = _iv(Qt.KeyboardModifier.AltModifier)
META = _iv(Qt.KeyboardModifier.MetaModifier)
_MOD_MASK = CTRL | SHIFT | ALT | META
_MOD_NAMES = {"Ctrl": CTRL, "Shift": SHIFT, "Alt": ALT, "Meta": META}
_LETTER_A = _iv(_K.Key_A)
_KEY_F1, _KEY_F35 = _iv(_K.Key_F1), _iv(_K.Key_F35)
_KEY_ESC, _KEY_DEL, _KEY_SLASH = _iv(_K.Key_Escape), _iv(_K.Key_Delete), _iv(_K.Key_Slash)
_MODIFIER_KEYS = {_iv(_K.Key_Shift), _iv(_K.Key_Control), _iv(_K.Key_Alt), _iv(_K.Key_Meta),
                  _iv(_K.Key_AltGr), _iv(_K.Key_CapsLock), _iv(_K.Key_NumLock)}


def parse_combo(label: str) -> tuple[int, int]:
    """``'Ctrl+Shift+C'`` / ``'→'`` / ``'N'`` (capital = Shift) -> ``(key, modifiers)`` ints."""
    parts = label.split("+")
    if label.endswith("++"):
        parts = label[:-2].split("+") + ["Plus"]
    *mods_s, name = parts
    mods = 0
    for m in mods_s:
        if m not in _MOD_NAMES:
            raise ValueError(f"unknown modifier {m!r} in {label!r}")
        mods |= _MOD_NAMES[m]
    if len(name) == 1 and name.isascii() and name.isalpha():
        # a bare capital is Shift+letter ("N"); after a modifier the case is only
        # the display convention ("Ctrl+D" is not Ctrl+Shift+D)
        if name.isupper() and not mods_s:
            mods |= SHIFT
        return _LETTER_A + ord(name.upper()) - ord("A"), mods
    if name in _NAMED_KEYS:
        return _NAMED_KEYS[name], mods
    raise ValueError(f"unknown key {name!r} in {label!r}")


def _event_chord(ev: QKeyEvent) -> tuple[int, int]:
    """The (key, modifiers) of a key event, keypad and group-switch bits dropped."""
    key = int(ev.key())
    if key == _iv(_K.Key_Enter):
        key = _iv(_K.Key_Return)
    return key, _iv(ev.modifiers()) & _MOD_MASK


def _page_desc(key: int, mods: int) -> str:
    """What reader.js's ``keyDesc()`` reports for the same key (``'Shift+N'``, ``'F3'``)."""
    names = {v: k for k, v in _NAMED_KEYS.items() if len(k) > 1 or k in "/,=-"}
    names[_KEY_ESC] = "Escape"
    if _LETTER_A <= key <= _LETTER_A + 25:
        name = chr(ord("A") + key - _LETTER_A)
    else:
        name = names.get(key, "")
    if not name:
        return ""
    parts = [n for n, bit in (("Ctrl", CTRL), ("Alt", ALT), ("Shift", SHIFT)) if mods & bit]
    return "+".join(parts + [name])


#: KEYS ids that bind in both the reader and the library (the app shell's own keys).
GLOBAL_ACTIONS: frozenset[str] = frozenset({
    "open", "library", "close_book", "quit", "fullscreen", "cheatsheet", "toggle_daynight",
    "new_tab", "next_tab", "prev_tab", "new_window"})
#: KEYS ids that only label toolbar tooltips (their keys are ``jump_history``'s).
TOOLTIP_ONLY: frozenset[str] = frozenset({"back", "forward"})
#: Library-screen ids that are NOT the library's: Ctrl+O is the shell's ``open``.
SHELL_OWNED_LIBRARY_IDS: dict[str, str] = {"lib_open_book": "open"}
#: Reader actions that work without an open book.
NO_BOOK_ACTIONS: frozenset[str] = frozenset({
    "escape", "cheatsheet", "fullscreen", "open", "library", "close_book", "quit",
    "toggle_daynight"})
#: Extra key chords for an existing action (keypad and shifted plus for Ctrl+=).
ALIASES: tuple[tuple[str, str, int], ...] = (
    ("font_step", "Ctrl+Plus", 0),
    ("font_step", "Ctrl+Shift+Plus", 0),
)


@dataclass(frozen=True)
class Binding:
    """One key chord and what it does.

    ``kind`` decides the gating rule (see :class:`KeyRouter`):
    ``page`` (Space/arrows/PageUp/PageDown/Home/End: reader.js turns the page
    itself while the book has focus), ``letter`` (j/k/n/N and /: only through
    the book, i.e. only while the web view has focus and no input is focused),
    ``fkey``, ``escape``, ``delete`` and ``chord`` (Ctrl/Alt combinations).
    ``owner`` is ``shell`` for keys bound here and ``library`` for the keys
    LibraryPage claims itself at ShortcutOverride (listed so the audit sees them).
    """

    kid: str
    index: int
    label: str
    key: int
    mods: int
    scope: str          # 'global' | 'reader' | 'library'
    kind: str
    owner: str
    handler: Callable[[], Any] | None
    needs_book: bool
    page_desc: str

    @property
    def chord(self) -> tuple[int, int]:
        return self.key, self.mods


def _kind_of(key: int, mods: int) -> str:
    if mods & (CTRL | ALT | META):
        return "chord"
    if _KEY_F1 <= key <= _KEY_F35:
        return "fkey"
    if key == _KEY_ESC:
        return "escape"
    if key == _KEY_DEL:
        return "delete"
    if (_LETTER_A <= key <= _LETTER_A + 25) or key == _KEY_SLASH:
        return "letter"
    return "page"


def build_bindings(reader_actions: dict[str, list[Callable[[], Any]]],
                   shell_actions: dict[str, Callable[[], Any]]) -> list[Binding]:
    """The complete map: every ``strings.KEYS`` id, one Binding per combo.

    *reader_actions* is ``ReaderPage.action_map()``; *shell_actions* overrides
    the handler of the app-level ids (``open``, ``quit``, ``cheatsheet``...).
    """
    out: list[Binding] = []

    def add(kid: str, index: int, label: str, scope: str, owner: str,
            handler: Callable[[], Any] | None) -> None:
        key, mods = parse_combo(label)
        kind = _kind_of(key, mods)
        desc = _page_desc(key, mods) if kind in ("letter", "fkey", "escape", "delete") else ""
        out.append(Binding(kid, index, label, key, mods, scope, kind, owner, handler,
                           kid not in NO_BOOK_ACTIONS, desc))

    for kid, combos in KEYS.items():
        if kid in TOOLTIP_ONLY or kid in SHELL_OWNED_LIBRARY_IDS:
            continue
        if kid.startswith("lib_"):
            for i, label in enumerate(combos):
                add(kid, i, label, "library", "library", None)
            continue
        slots = reader_actions.get(kid) or []
        for i, label in enumerate(combos):
            if kid in shell_actions:
                handler: Callable[[], Any] | None = shell_actions[kid]
            elif slots:
                handler = slots[i] if len(slots) > 1 else slots[0]
            else:
                handler = None
            add(kid, i, label, "global" if kid in GLOBAL_ACTIONS else "reader", "shell", handler)
    for kid, label, index in ALIASES:
        base = next(b for b in out if b.kid == kid and b.index == index)
        add(kid, index, label, base.scope, base.owner, base.handler)
    return out


def audit_bindings(bindings: Sequence[Binding]) -> list[str]:
    """Every problem with the map: a chord bound twice where both can fire, an id
    with no handler, or a ``strings.KEYS`` id that nothing binds."""
    problems: list[str] = []
    for scope in ("reader", "library"):
        seen: dict[tuple[int, int], Binding] = {}
        for b in bindings:
            if b.scope not in ("global", scope):
                continue
            other = seen.get(b.chord)
            if other is not None:
                problems.append(f"{b.label!r} is bound twice on the {scope} screen: "
                                f"{other.kid} and {b.kid}")
            else:
                seen[b.chord] = b
    descs: dict[str, Binding] = {}
    for b in bindings:
        if b.page_desc and b.scope in ("global", "reader"):
            other = descs.get(b.page_desc)
            if other is not None and other.handler is not b.handler:
                problems.append(f"page key {b.page_desc!r} maps to {other.kid} and {b.kid}")
            descs.setdefault(b.page_desc, b)
    for b in bindings:
        if b.owner == "shell" and b.handler is None and b.scope == "global":
            # reader-scoped ids legitimately have no handler until a book tab
            # exists (the app starts on the shelf); the shell's own keys must not.
            problems.append(f"{b.kid} ({b.label}) has no handler")
    covered = {b.kid for b in bindings} | set(TOOLTIP_ONLY) | set(SHELL_OWNED_LIBRARY_IDS)
    for kid in KEYS:
        if kid not in covered:
            problems.append(f"{kid} is not bound")
    return problems


class KeyRouter(QObject):
    """The one place every shortcut is dispatched from.

    Installed as an application event filter.  It looks at each ``KeyPress``
    once, on its way to the focus widget, and applies the spec's gating rule:

    * nothing is handled while a popup menu or a modal dialog is up, or when
      the key is for another top-level window;
    * the reader's keys work only while the reader is the current page, the
      shell's keys (Ctrl+O, Ctrl+W, Ctrl+Q, Ctrl+Shift+L, F11, F1, Ctrl+/,
      Ctrl+Shift+D) everywhere; the library's own keys are left to LibraryPage;
    * j/k/n/N and / are never taken here.  reader.js forwards them through
      ``keyUnhandled`` only while the book view has focus and no input in the
      page is focused; they arrive in :meth:`dispatch_page_key`;
    * Space/arrows/PageUp/PageDown/Home/End go to reader.js while the book has
      focus (instant turns, scroll-mode behaviour) and are handled here only
      when nothing that uses them (a list, a slider, a text field) has focus;
    * Escape goes to the page first while the book has focus (it closes an
      image zoom or a note bubble), which forwards it back; the ladder itself
      is ``ReaderPage.escape()``;
    * a Ctrl/Alt chord that the focused text field wants (Ctrl+C, Ctrl+V,
      Ctrl+←...) stays with the field.  Ctrl+C is never intercepted while the
      book has focus, so Chromium copies the selection natively.
    """

    def __init__(self, window: "MainWindow", bindings: Sequence[Binding], *, debug: bool = False) -> None:
        super().__init__(window)
        self._win = window
        self.debug = debug
        self.set_bindings(bindings)
        #: The last binding fired (tests and the log).
        self.last_fired: str = ""

    def set_bindings(self, bindings: Sequence[Binding]) -> None:
        """Install a (possibly rebuilt) map — the active tab's reader changed."""
        self.bindings: list[Binding] = list(bindings)
        self.problems = audit_bindings(self.bindings)
        if self.problems:
            msg = "keyboard map conflicts:\n  " + "\n  ".join(self.problems)
            if self.debug:
                raise AssertionError(msg)
            log.error("%s", msg)
        self._qt: dict[tuple[str, int, int], Binding] = {}
        self._page: dict[str, Binding] = {}
        for b in self.bindings:
            if b.owner != "shell":
                continue
            if b.kind != "letter":
                self._qt.setdefault((b.scope, b.key, b.mods), b)
            if b.page_desc and b.scope in ("global", "reader"):
                self._page.setdefault(b.page_desc, b)
        if "/" in self._page:
            self._page.setdefault("Shift+/", self._page["/"])   # layouts where / needs Shift

    # -- queries --------------------------------------------------------------
    def binding_for(self, scope: str, label: str) -> Binding | None:
        key, mods = parse_combo(label)
        return self._qt.get((scope, key, mods)) or self._qt.get(("global", key, mods))

    def _in_view(self, w: QWidget | None) -> bool:
        reader = self._win.reader
        if reader is None:
            return False
        view = reader.view
        return w is not None and (w is view or view.isAncestorOf(w) or w is view.focusProxy())

    def _interactive(self, w: QWidget | None) -> bool:
        """A focused control that may use a plain key itself (list, slider, field, button)."""
        if w is None or self._in_view(w):
            return False
        win = self._win
        exempt = [win, win.stack, win.library]
        reader = win.reader
        if reader is not None:
            exempt += [reader, reader.column]
        return w not in exempt

    @staticmethod
    def _text_like(w: QWidget | None) -> bool:
        if isinstance(w, (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox)):
            return True
        return isinstance(w, QComboBox) and w.isEditable()

    @staticmethod
    def _field_wants(w: QWidget, ev: QKeyEvent) -> bool:
        """Ask the focused field, the way Qt does before any shortcut fires."""
        probe = QKeyEvent(QEvent.Type.ShortcutOverride, ev.key(), ev.modifiers(), ev.text(),
                          ev.isAutoRepeat(), ev.count())
        probe.ignore()
        QCoreApplication.sendEvent(w, probe)
        return probe.isAccepted()

    def _scope(self) -> str:
        return "reader" if self._win.stack.currentWidget() is self._win.reader else "library"

    # -- the filter -----------------------------------------------------------
    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        if event.type() != QEvent.Type.KeyPress:
            return False
        win = self._win
        fw = QApplication.focusWidget()
        if fw is None and not win.isActiveWindow():
            fw = win.focusWidget()             # synthetic keys to an inactive window (tests)
        if obj is not (fw if fw is not None else win):
            return False                       # only the first delivery, to the focus widget
        if QApplication.activePopupWidget() is not None or QApplication.activeModalWidget() is not None:
            return False
        if (fw if fw is not None else win).window() is not win:
            return False
        key, mods = _event_chord(event)
        if key in _MODIFIER_KEYS or key == 0:
            return False
        scope = self._scope()
        b = self._qt.get((scope, key, mods)) or self._qt.get(("global", key, mods))
        if b is None or not self._allowed(b, fw, event):
            return False
        event.accept()
        self._fire(b)
        return True

    def _allowed(self, b: Binding, fw: QWidget | None, ev: QKeyEvent) -> bool:
        reader = self._win.reader
        if b.scope == "reader" and (reader is None or (b.needs_book and reader.book is None)):
            return False
        in_view = self._in_view(fw)
        page_live = in_view and reader.is_ready()
        if b.kind == "page":
            return reader.error_spec() is None and not page_live and not self._interactive(fw)
        if b.kind == "escape":
            return not page_live
        if b.kind == "delete":
            return not page_live and not self._interactive(fw)
        if b.kind == "chord":
            if b.kid == "copy" and in_view:
                return False                   # Chromium copies the selection natively
            if self._text_like(fw) and self._field_wants(fw, ev):
                return False
            return True
        return b.kind == "fkey"

    def _fire(self, b: Binding) -> None:
        self.last_fired = f"{b.kid}:{b.label}"
        if b.handler is not None:
            b.handler()

    def dispatch_page_key(self, desc: str) -> bool:
        """A key reader.js did not handle (the book view had focus, no input in the page)."""
        if self._scope() != "reader":
            return False
        b = self._page.get(desc)
        if b is None:
            return False
        reader = self._win.reader
        if b.needs_book and (reader is None or reader.book is None):
            return False
        self._fire(b)
        return True

    def table(self) -> list[tuple[str, str, str, str]]:
        """``(scope, label, kid, owner)`` rows, for the log and the tests."""
        return [(b.scope, b.label, b.kid, b.owner) for b in self.bindings]


# ==========================================================================
# single instance (QLocalServer on store.PIPE_NAME)
# ==========================================================================

def _allow_foreground() -> None:
    """Let the running instance take the foreground when we hand it a file."""
    if sys.platform == "win32":
        try:
            ctypes.windll.user32.AllowSetForegroundWindow(-1)   # ASFW_ANY
        except Exception:  # noqa: BLE001
            pass


class SingleInstance(QObject):
    """Hand-off between launches.

    The first process :meth:`listen` s on ``\\\\.\\pipe\\<name>``.  A later launch
    calls :meth:`forward` with ``OPEN <path>`` (or ``ACTIVATE``), waits for the
    ``OK`` reply and exits.  ``messageReceived(str)`` fires in the first process.
    """

    messageReceived = Signal(str)

    def __init__(self, name: str = PIPE_NAME, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.name = name
        self.server: QLocalServer | None = None
        self._buffers: dict[int, bytes] = {}
        self.received: list[str] = []
        # True after forward() reached a server, even if it never acknowledged:
        # someone already owns the pipe, so this process must not listen() too.
        self.server_found = False

    def forward(self, message: str, timeout_ms: int = FORWARD_TIMEOUT_MS) -> bool:
        """Send *message* to a running instance.  True only when it acknowledged."""
        sock = QLocalSocket()
        sock.connectToServer(self.name)
        if not sock.waitForConnected(min(timeout_ms, 800)):
            return False
        self.server_found = True
        _allow_foreground()
        sock.write((message + "\n").encode("utf-8"))
        sock.flush()
        if not sock.waitForBytesWritten(timeout_ms):
            sock.abort()
            return False
        reply = b""
        deadline = time.monotonic() + timeout_ms / 1000.0
        while b"\n" not in reply:
            left = int((deadline - time.monotonic()) * 1000)
            if left <= 0 or not sock.waitForReadyRead(left):
                break
            reply += bytes(sock.readAll().data())
        sock.disconnectFromServer()
        return reply.strip().startswith(b"OK")

    def listen(self) -> bool:
        """Become the running instance.  False if the pipe could not be created."""
        server = QLocalServer(self)
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        ok = server.listen(self.name)
        if not ok:
            QLocalServer.removeServer(self.name)
            ok = server.listen(self.name)
        if not ok:
            log.warning("single instance: cannot listen on %s: %s", self.name, server.errorString())
            server.deleteLater()
            return False
        server.newConnection.connect(self._on_connection)
        self.server = server
        return True

    def close(self) -> None:
        if self.server is not None:
            self.server.close()
            self.server = None

    def _on_connection(self) -> None:
        server = self.server
        while server is not None and server.hasPendingConnections():
            sock = server.nextPendingConnection()
            self._buffers[id(sock)] = b""
            sock.readyRead.connect(lambda s=sock: self._read(s))
            sock.disconnected.connect(lambda s=sock: self._drop(s))

    def _drop(self, sock: QLocalSocket) -> None:
        self._buffers.pop(id(sock), None)
        sock.deleteLater()

    def _read(self, sock: QLocalSocket) -> None:
        buf = self._buffers.get(id(sock), b"") + bytes(sock.readAll().data())
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            msg = line.decode("utf-8", "replace").strip()
            if not msg:
                continue
            sock.write(b"OK\n")                # acknowledge before the (slower) open
            sock.flush()
            self.received.append(msg)
            self.messageReceived.emit(msg)
        self._buffers[id(sock)] = buf


def _quick_setting(dotted: str, default: Any) -> Any:
    """Read one value from settings.json without creating a Store (no writes)."""
    try:
        with open(os.path.join(store_mod.app_dir(), "settings.json"), encoding="utf-8") as fh:
            obj: Any = json.load(fh)
        for part in dotted.split("."):
            obj = obj[part]
        return obj
    except Exception:  # noqa: BLE001 - absent, damaged or partial: the default
        return default


# ==========================================================================
# Qt's own strings (context menus, standard buttons) follow the UI language
# ==========================================================================

class QtTranslations:
    """Keeps ``qtbase_<locale>.qm`` and ``QLocale.setDefault`` in step with the UI language."""

    def __init__(self, app: QCoreApplication) -> None:
        self._app = app
        self._tr: QTranslator | None = None
        strings.language_changed.subscribe(self.apply)
        self.apply(strings.current_language())

    def apply(self, lang: str) -> None:
        name = strings.QT_LOCALE_NAMES.get(lang, "en_US")
        QLocale.setDefault(QLocale(name))
        if self._tr is not None:
            self._app.removeTranslator(self._tr)
            self._tr = None
        if lang == "en":
            return
        tr = QTranslator(self._app)
        folder = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
        if tr.load(f"qtbase_{name}", folder):
            self._app.installTranslator(tr)
            self._tr = tr


# ==========================================================================
# the non-fatal error card and the About dialog
# ==========================================================================

def _open_folder(path: str) -> None:
    QDesktopServices.openUrl(QUrl.fromLocalFile(path))


def _role(w: QWidget, role: str) -> QWidget:
    w.setProperty("erRole", role)
    return w


class CrashCard(QFrame):
    """In-window card for an unexpected internal error (never a modal box)."""

    def __init__(self, parent: QWidget, log_path: Callable[[], str]) -> None:
        super().__init__(parent)
        self.setObjectName("er-crash-card")
        self.setProperty("erRole", "card")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._log_path = log_path
        self.count = 0
        self.last_text = ""
        v = QVBoxLayout(self)
        v.setContentsMargins(24, 20, 24, 18)
        v.setSpacing(10)
        self.title = _role(QLabel(self), "title")
        self.title.setWordWrap(True)
        self.body = _role(QLabel(self), "body")
        self.body.setWordWrap(True)
        self.path = QLabel(self)
        self.path.setObjectName("crash-log-path")
        self.path.setWordWrap(True)
        self.path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.details_btn = QToolButton(self)
        self.details_btn.setObjectName("crash-details-toggle")
        self.details_btn.setCheckable(True)
        self.details_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.details_btn.setArrowType(Qt.ArrowType.RightArrow)
        self.details_btn.toggled.connect(self._toggle)
        self.details = QPlainTextEdit(self)
        self.details.setObjectName("crash-details")
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(180)
        mono = QFont("Consolas")
        mono.setPixelSize(12)
        self.details.setFont(mono)
        self.details.hide()
        row = QHBoxLayout()
        row.addStretch(1)
        self.open_log = QPushButton(self)
        self.open_log.setObjectName("crash-open-log")
        self.open_log.clicked.connect(lambda: _open_folder(os.path.dirname(self._log_path())))
        self.close_btn = _role(QPushButton(self), "primary")
        self.close_btn.setObjectName("crash-close")
        self.close_btn.clicked.connect(self.hide)
        row.addWidget(self.open_log)
        row.addWidget(self.close_btn)
        for w in (self.title, self.body, self.path, self.details_btn, self.details):
            v.addWidget(w)
        v.addLayout(row)
        self.hide()
        self.retranslate_ui()

    def show_error(self, text: str) -> None:
        self.count += 1
        self.last_text = text
        self.details.setPlainText(text)
        self.retranslate_ui()
        self.show()
        self.raise_()
        self.place()

    def _toggle(self, on: bool) -> None:
        self.details.setVisible(on)
        self.details_btn.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)
        self.place()

    def place(self) -> None:
        parent = self.parentWidget()
        if parent is None or not self.isVisible():
            return
        w = int(min(580, max(320, parent.width() - 48)))
        lay = self.layout()
        h = lay.totalHeightForWidth(w) if lay.hasHeightForWidth() else self.sizeHint().height()
        h = int(min(max(h, 120), max(120, parent.height() - 48)))
        self.setGeometry((parent.width() - w) // 2, max(24, (parent.height() - h) // 3), w, h)

    def retranslate_ui(self) -> None:
        self.title.setText(S("err.crash.title"))
        self.body.setText(S("err.crash.body"))
        self.path.setText(self._log_path())
        self.details_btn.setText(S("err.details"))
        self.open_log.setText(S("err.crash.open_log"))
        self.close_btn.setText(S("common.close"))
        self.place()


class AboutDialog(QDialog):
    """关于 Book Reader: version, what it was built with, and where the data lives."""

    def __init__(self, store: Store, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("er-about")
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self._store = store
        v = QVBoxLayout(self)
        v.setContentsMargins(28, 24, 28, 20)
        v.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(16)
        icon = QLabel(self)
        icon.setPixmap(QIcon(webhost.asset_path("app.ico")).pixmap(QSize(56, 56)))
        head.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        names = QVBoxLayout()
        names.setSpacing(2)
        self.name = QLabel(strings.APP_DISPLAY_NAME, self)
        f = QFont(self.name.font())
        f.setPixelSize(22)
        f.setBold(True)
        self.name.setFont(f)
        self.tagline = _role(QLabel(self), "secondary")
        self.version = QLabel(self)
        self.built = _role(QLabel(self), "secondary")
        for w in (self.name, self.tagline, self.version, self.built):
            names.addWidget(w)
        head.addLayout(names, 1)
        v.addLayout(head)
        v.addSpacing(10)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(4)
        self.rows: list[tuple[QLabel, QLabel, QPushButton, str, str]] = []
        for r, (key, path) in enumerate((("about.storage", store.root),
                                          ("about.cache", store.cache_root),
                                          ("about.logs", store.log_dir))):
            label = _role(QLabel(self), "secondary")
            value = QLabel(path, self)
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            btn = QPushButton(self)
            btn.clicked.connect(lambda _=False, p=path: _open_folder(p))
            grid.addWidget(label, 2 * r, 0, 1, 2)
            grid.addWidget(value, 2 * r + 1, 0)
            grid.addWidget(btn, 2 * r + 1, 1, Qt.AlignmentFlag.AlignTop)
            self.rows.append((label, value, btn, key, path))
        grid.setColumnStretch(0, 1)
        v.addLayout(grid)
        v.addSpacing(8)
        row = QHBoxLayout()
        row.addStretch(1)
        self.close_btn = _role(QPushButton(self), "primary")
        self.close_btn.setDefault(True)
        self.close_btn.clicked.connect(self.close)
        row.addWidget(self.close_btn)
        v.addLayout(row)
        self.setMinimumWidth(520)
        strings.language_changed.subscribe(self.retranslate_ui)
        self.retranslate_ui()

    def retranslate_ui(self) -> None:
        self.setWindowTitle(S("about.title"))
        self.tagline.setText(S("about.tagline"))
        self.version.setText(S("about.version", version=APP_VERSION))
        py = ".".join(str(x) for x in sys.version_info[:3])
        self.built.setText(S("about.built", py=py, qt=qVersion()))
        for label, _value, btn, key, _path in self.rows:
            label.setText(S(key))
            btn.setText(S("about.open_folder"))
        self.close_btn.setText(S("common.close"))


# ==========================================================================
# the window set (multiple OS windows, each with its own tabs)
# ==========================================================================

class _WindowSet:
    """Every live main window, in slot order.

    A window's *slot* is stable while it lives and keys its per-window
    settings; slot 0 keeps the historical ``window.*`` keys, so a single-window
    setup reads and writes exactly what it always did.
    """

    def __init__(self) -> None:
        self.all: list["MainWindow"] = []

    def add(self, win: "MainWindow") -> int:
        used = {w._slot for w in self.all}
        slot = next(i for i in range(len(self.all) + 1) if i not in used)
        self.all.append(win)
        return slot

    def remove(self, win: "MainWindow") -> None:
        try:
            self.all.remove(win)
        except ValueError:
            pass

    def live(self) -> list["MainWindow"]:
        return [w for w in self.all if not w._shut]

    def focused(self, default: "MainWindow | None" = None) -> "MainWindow | None":
        app = QApplication.instance()
        w = app.activeWindow() if app is not None else None
        if isinstance(w, MainWindow):
            return w
        if default is not None and not default._shut:
            return default
        live = self.live()
        return live[-1] if live else None


WINDOWS = _WindowSet()


# ==========================================================================
# the tab strip
# ==========================================================================

@dataclass(eq=False)
class _Tab:
    """One tab: the shelf, or one open book (its reader is built lazily).

    A book tab with ``reader is None`` is a *placeholder* restored from the
    last session: the shelf entry supplies path and title, and the reading
    surface itself is only created the first time the tab is activated (so
    restarting with ten books open does not spawn ten Chromium renderers).
    """

    kind: str                                  # "library" | "book"
    bid: str = ""                              # book id (content hash)
    path: str = ""                             # the book file
    reader: "ReaderPage | None" = None
    title: str = ""                            # metadata title used by the OS window
    token: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def is_library(self) -> bool:
        return self.kind == "library"


class TabBar(QTabBar):
    """Browser-style tabs: closable, movable, middle-click closes, context menu.

    Dragging a tab to another window's tab strip moves it there (like a browser);
    dropping on empty space in the same window just reorders.
    """

    closeOthersRequested = Signal(int)
    moveTabToWindowRequested = Signal(int, object, int)  # index, window, insertion slot
    detachTabRequested = Signal(int, object)             # index, optional screen point
    MIME_TYPE = "application/x-bookreader-tab"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("er-tabbar")
        self.setMovable(True)
        self.setTabsClosable(True)
        self.setExpanding(False)
        self.setUsesScrollButtons(True)
        self.setElideMode(Qt.TextElideMode.ElideRight)
        self.setSelectionBehaviorOnRemove(QTabBar.SelectionBehavior.SelectLeftTab)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.setAcceptDrops(True)
        self._drag_start: QPoint | None = None
        self._drag_token: str | None = None
        self._drag_cancelled = False

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.MiddleButton:
            i = self.tabAt(event.position().toPoint())
            if i >= 0:
                self.tabCloseRequested.emit(i)
                return
        if event.button() == Qt.MouseButton.LeftButton:
            i = self.tabAt(event.position().toPoint())
            if i >= 0:
                self._drag_start = event.position().toPoint()
                self._drag_token = self.tabData(i)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.buttons() & Qt.MouseButton.LeftButton \
                and self._drag_start is not None and self._drag_token is not None \
                and (event.position().toPoint() - self._drag_start).manhattanLength() \
                >= QApplication.startDragDistance() \
                and not self.rect().adjusted(0, -8, 0, 8).contains(event.position().toPoint()):
            # Let QTabBar do its normal live reordering while the pointer stays
            # in the strip. Only take over when the tab leaves it. Native moves
            # can change the index, so resolve the original tab by identity.
            i = self._index_for_token(self._drag_token)
            self._drag_start = None
            self._drag_token = None
            release = QMouseEvent(QEvent.Type.MouseButtonRelease, event.position(),
                                  event.globalPosition(), Qt.MouseButton.LeftButton,
                                  Qt.MouseButton.NoButton, event.modifiers())
            super().mouseReleaseEvent(release)
            if i >= 0:
                self._begin_drag(i)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.MiddleButton:
            # We closed on press. Qt 6 also closes middle-clicked tabs on
            # release, which would otherwise close the neighbour a second time.
            event.accept()
            return
        self._drag_start = None
        self._drag_token = None
        super().mouseReleaseEvent(event)

    def _index_for_token(self, token: str | None) -> int:
        if not token:
            return -1
        return next((i for i in range(self.count()) if self.tabData(i) == token), -1)

    def _begin_drag(self, index: int) -> None:
        """Move the live tab, retaining its identity throughout the nested drag loop."""
        owner = self.window()  # retain a source emptied/closed inside QDrag.exec()
        token = self.tabData(index)
        if not token:
            return
        # Windows can deliver a queued mouse-move after the real button-up
        # (fast dragging or input automation). Starting OLE drag then leaves a
        # floating tab waiting for a release which has already happened.
        if sys.platform == "win32" and not self._left_button_down():
            self._drop_at_position(token, QCursor.pos())
            return
        mime = QMimeData()
        mime.setData(self.MIME_TYPE, token.encode("ascii"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.setPixmap(self.grab(self.tabRect(index)))
        drag.setHotSpot(QPoint(8, 8))
        app = QApplication.instance()
        self._drag_cancelled = False
        if app is not None:
            app.installEventFilter(self)
        try:
            action = drag.exec(Qt.DropAction.MoveAction)
        finally:
            if app is not None:
                app.removeEventFilter(self)
            drag.deleteLater()
        self._finish_drag(token, action, QCursor.pos())
        del owner

    @staticmethod
    def _left_button_down() -> bool:
        if sys.platform == "win32":
            return bool(ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000)
        return bool(QApplication.mouseButtons() & Qt.MouseButton.LeftButton)

    def _drop_at_position(self, token: str, position: QPoint) -> None:
        """Complete an already released drag without entering the native loop."""
        i = self._index_for_token(token)
        if i < 0:
            return
        target = QApplication.widgetAt(position)
        while target is not None and not isinstance(target, TabBar):
            target = target.parentWidget()
        if isinstance(target, TabBar) and isinstance(target.window(), MainWindow):
            at = target._insertion_slot(target.mapFromGlobal(position))
            if target is self:
                self.moveTab(i, at - (i < at))
            else:
                self.moveTabToWindowRequested.emit(i, target.window(), at)
        else:
            self.detachTabRequested.emit(i, position)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
            self._drag_cancelled = True
        return super().eventFilter(obj, event)

    def _finish_drag(self, token: str, action: Qt.DropAction, position: QPoint) -> None:
        # IgnoreAction also means Escape/cancel. Only a released mouse outside
        # our strip is a tear-off; never turn a cancelled drag into a new window.
        if action != Qt.DropAction.IgnoreAction or self._drag_cancelled \
                or self._left_button_down():
            return
        i = self._index_for_token(token)
        if i >= 0 and not self.rect().contains(self.mapFromGlobal(position)):
            self.detachTabRequested.emit(i, position)

    def _drag_source(self, event: Any) -> tuple["TabBar", int] | None:
        if not event.mimeData().hasFormat(self.MIME_TYPE):
            return None
        source = event.source()
        if not isinstance(source, TabBar):
            return None
        try:
            token = bytes(event.mimeData().data(self.MIME_TYPE)).decode("ascii")
        except UnicodeDecodeError:
            return None
        i = source._index_for_token(token)
        if i < 0 or not isinstance(source.window(), MainWindow) \
                or not isinstance(self.window(), MainWindow) \
                or source.window()._shut or self.window()._shut:
            return None
        return source, i

    def _insertion_slot(self, pos: QPoint) -> int:
        """The gap before/after a tab, including blank space after the last tab."""
        rtl = self.layoutDirection() == Qt.LayoutDirection.RightToLeft
        for i in range(self.count()):
            centre = self.tabRect(i).center().x()
            if (rtl and pos.x() > centre) or (not rtl and pos.x() < centre):
                return i
        return self.count()

    def dragEnterEvent(self, event: Any) -> None:  # noqa: N802
        if self._drag_source(event) is not None:
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event: Any) -> None:  # noqa: N802
        self.dragEnterEvent(event)

    def dropEvent(self, event: Any) -> None:  # noqa: N802
        source = self._drag_source(event)
        if source is None:
            event.ignore()
            return
        src_bar, src_index = source
        at = self._insertion_slot(event.position().toPoint())
        if src_bar is self:
            # A drag which left the strip can re-enter it. Qt's native movable
            # logic does not handle QDrag drops; reorder explicitly in this case.
            self.moveTab(src_index, at - (src_index < at))
        else:
            src_bar.moveTabToWindowRequested.emit(src_index, self.window(), at)
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()

    def _menu(self, pos: QPoint) -> None:
        i = self.tabAt(pos)
        if i < 0:
            return
        token = self.tabData(i)
        menu = QMenu(self)
        menu.setObjectName("tabbar-menu")
        menu.addAction(S("tabs.close"),
                       lambda: self.tabCloseRequested.emit(self._index_for_token(token)))
        menu.addSeparator()
        menu.addAction(S("tabs.close_others"),
                       lambda: self.closeOthersRequested.emit(self._index_for_token(token)))
        menu.addSeparator()
        others = [w for w in WINDOWS.live() if w is not self.window()]
        if others:
            windows_menu = menu.addMenu(S("tabs.move_to_window"))
            windows_menu.setObjectName("tabbar-window-menu")
            for win in others:
                windows_menu.addAction(f"{win._slot + 1} · {win.windowTitle()}",
                                       lambda _checked=False, w=win: self._move_to_menu_window(token, w))
        menu.addAction(S("tabs.move_to_new_window"),
                       lambda: self.detachTabRequested.emit(self._index_for_token(token), None))
        menu.exec(self.mapToGlobal(pos))
        menu.deleteLater()

    def _move_to_menu_window(self, token: str, window: "MainWindow") -> None:
        if not window._shut:
            self.moveTabToWindowRequested.emit(self._index_for_token(token), window,
                                               window.tabbar.count())


# ==========================================================================
# the main window
# ==========================================================================

class MainWindow(QMainWindow):
    """The one window: browser-style tabs over the shelf and the readers.

    ``MainWindow(path=None, *, store=None, theme_controller=None, debug=None)``.
    Without *store* it opens the default Store (``%APPDATA%\\Book Reader``) and
    closes it when the window closes.  *path* opens that book right away.
    """

    _current: "weakref.ReferenceType[MainWindow] | None" = None
    #: True while every window is closing together (Ctrl+Q): the session must
    #: keep ALL windows, so the per-close rewrite is suspended.
    _quitting = False

    def __init__(self, path: str | os.PathLike | None = None, *, store: Store | None = None,
                 theme_controller: theme_mod.ThemeController | None = None,
                 debug: bool | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        app = QApplication.instance()
        assert app is not None, "create the QApplication first"
        self._debug = _debug_default() if debug is None else bool(debug)
        self._own_store = store is None
        self.store: Store = store if store is not None else Store()
        self._shut = False
        self._slot = WINDOWS.add(self)
        self._fs_outside_zen = False
        self._last_maximized = False
        self._about: AboutDialog | None = None
        self.instance: SingleInstance | None = None

        strings.set_language(self.store.get("ui.language", "auto"))
        self._qt_tr = QtTranslations(app)
        self.theme_controller = theme_controller or theme_mod.ThemeController(
            app, self.store.get("reader.theme", "system"), app)

        self.setObjectName("er-main-window")
        self.setWindowIcon(QIcon(webhost.asset_path("app.ico")))
        self.setMinimumSize(*MIN_SIZE)
        self.resize(*DEFAULT_SIZE)
        self.setAcceptDrops(True)

        self.stack = QStackedWidget(self)
        self.stack.setObjectName("er-stack")
        self.library = LibraryPage(self.store, self.stack, theme=self.theme_controller.theme)
        self.stack.addWidget(self.library)

        # ---- the tab strip (browser-style) ---------------------------------------
        self.tabbar = TabBar(self)
        self.new_tab_btn = QToolButton(self)
        self.new_tab_btn.setObjectName("er-newtab")
        self.new_tab_btn.setText("+")
        self.new_tab_btn.setToolTip(S("tabs.new"))
        self.new_tab_btn.clicked.connect(lambda: self.new_library_tab())
        self.tabrow = QWidget(self)
        self.tabrow.setObjectName("er-tabrow")
        self.tabrow.setProperty("erRole", "tabrow")
        row = QHBoxLayout(self.tabrow)
        row.setContentsMargins(6, 0, 6, 0)
        row.setSpacing(0)
        row.addWidget(self.tabbar, 1)
        row.addWidget(self.new_tab_btn, 0)
        central = QWidget(self)
        box = QVBoxLayout(central)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)
        box.addWidget(self.tabrow)
        box.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self.library_cheatsheet = CheatSheet(self.library, lambda: self.theme_controller.theme)
        self.library_cheatsheet.closed.connect(lambda: self.library.setFocus())
        self.library.installEventFilter(self)
        self.crash_card = CrashCard(self, lambda: store_mod.log_file(self.store.root))

        # ---- wiring ----------------------------------------------------------
        lib = self.library
        lib.openBook.connect(self.open_path)
        self.theme_controller.themeChanged.connect(lib.apply_theme)
        self.theme_controller.themeChanged.connect(self._refresh_library_cheatsheet)
        self._build_library_menu()
        strings.language_changed.subscribe(self.retranslate_ui)

        # ---- the keyboard map --------------------------------------------------
        self._shell = {
            "open": self.open_dialog,
            "library": self.back_to_library,
            "close_book": self.close_book_or_window,
            "quit": self.request_quit,
            "fullscreen": self.toggle_fullscreen,
            "cheatsheet": self.toggle_cheatsheet,
            "toggle_daynight": self.toggle_day_night,
            "new_tab": self.new_library_tab,
            "next_tab": self.next_tab,
            "prev_tab": self.prev_tab,
            "new_window": self.new_window,
        }
        self.keys = KeyRouter(self, build_bindings({}, self._shell), debug=self._debug)
        app.installEventFilter(self.keys)

        # ---- tabs ----------------------------------------------------------------
        self.tabs: list[_Tab] = []
        self._active_tab: _Tab | None = None
        self._pool: list["ReaderPage"] = []         # idle readers, reusable
        # False until open_path()/restore_session() runs: the start-up tab
        # activation must not overwrite the saved session before it is restored.
        self._session_ready = False
        self.tabbar.currentChanged.connect(self._on_tabbar_current)
        self.tabbar.tabCloseRequested.connect(self.close_tab)
        self.tabbar.closeOthersRequested.connect(self._close_other_tabs)
        self.tabbar.tabMoved.connect(self._on_tab_moved)
        self.tabbar.moveTabToWindowRequested.connect(self._move_tab_to_window)
        self.tabbar.detachTabRequested.connect(self._detach_tab_to_new_window)
        # One reading surface exists from the start (as it always did), so the
        # window is instantly ready for a book and `win.reader` is never None.
        self._pool.append(self._make_reader())
        self._add_tab(_Tab(kind="library"), at=0)
        self._activate_tab(0)

        # ---- state saving --------------------------------------------------------
        self._geom_timer = QTimer(self)
        self._geom_timer.setSingleShot(True)
        self._geom_timer.setInterval(600)
        self._geom_timer.timeout.connect(self.save_window_state)
        app.aboutToQuit.connect(self.shutdown)

        MainWindow._current = weakref.ref(self)
        self._update_title()
        if path is not None:
            self.open_path(os.fspath(path))

    # ------------------------------------------------------------------
    @classmethod
    def current(cls) -> "MainWindow | None":
        """The window to act on: the focused one, else the last one standing."""
        focused = WINDOWS.focused()
        if focused is not None:
            return focused
        ref = cls._current
        win = ref() if ref is not None else None
        try:
            if win is not None and not win._shut:
                win.objectName()           # raises if the C++ side is gone
                return win
        except RuntimeError:
            pass
        return None

    # ---- tabs -------------------------------------------------------------
    @property
    def reader(self) -> "ReaderPage | None":
        """The reader to act on: the active tab's, else any live or idle one.

        Code that runs while the shelf is showing (the theme menu, the window
        title) still finds a reader this way; it is ``None`` only before the
        window is fully built.
        """
        tab = self._active_tab
        if tab is not None and tab.reader is not None:
            return tab.reader
        for t in self.tabs:
            if t.reader is not None:
                return t.reader
        return self._pool[-1] if self._pool else None

    @property
    def active_reader(self) -> "ReaderPage | None":
        """The reader of the tab that is showing, if that tab is a book."""
        tab = self._active_tab
        return tab.reader if tab is not None and not tab.is_library else None

    def is_reading(self) -> bool:
        tab = self._active_tab
        return tab is not None and not tab.is_library

    def _current_tab(self) -> _Tab | None:
        return self._active_tab

    def _index_of(self, tab: _Tab) -> int:
        try:
            return self.tabs.index(tab)
        except ValueError:
            return -1

    def _tab_of_reader(self, reader: "ReaderPage") -> _Tab | None:
        for t in self.tabs:
            if t.reader is reader:
                return t
        return None

    def _tab_label(self, tab: _Tab) -> str:
        if tab.is_library:
            return S("title.library")
        # Tabs identify the original user file, including extension. Metadata
        # remains the window title and must never replace the filename here.
        return os.path.basename(tab.path) if tab.path else "…"

    def _add_tab(self, tab: _Tab, at: int | None = None) -> int:
        """Append (or insert) a tab record and its strip entry; returns the index."""
        if at is None:
            at = len(self.tabs)
        at = max(0, min(at, len(self.tabs)))
        self.tabs.insert(at, tab)
        self.tabbar.blockSignals(True)
        self.tabbar.insertTab(at, self._tab_label(tab))
        self.tabbar.setTabData(at, tab.token)
        self.tabbar.blockSignals(False)
        self.tabbar.setTabToolTip(at, self._tab_label(tab))
        self._style_tab_buttons(at)
        return at

    def _style_tab_buttons(self, i: int) -> None:
        """Our own translation on the strip's close button (Qt's is stale-language)."""
        btn = self.tabbar.tabButton(i, QTabBar.ButtonPosition.RightSide)
        if btn is not None:
            btn.setToolTip(S("tabs.close"))
            btn.setAccessibleName(S("tabs.close"))

    def _remove_tab(self, tab: _Tab) -> None:
        i = self._index_of(tab)
        if i < 0:
            return
        self.tabs.pop(i)
        self.tabbar.blockSignals(True)
        self.tabbar.removeTab(i)
        self.tabbar.blockSignals(False)

    def _on_tabbar_current(self, i: int) -> None:
        if 0 <= i < len(self.tabs):
            self._activate_tab(i)

    def _on_tab_moved(self, a: int, b: int) -> None:
        try:
            tab = self.tabs.pop(a)
            self.tabs.insert(b, tab)
        except IndexError:
            return
        self._save_session()

    def _activate_tab(self, i: int) -> None:
        """Show tab *i*, building its reader first when it is still a placeholder."""
        if not (0 <= i < len(self.tabs)):
            return
        tab = self.tabs[i]
        if tab is self._active_tab and (tab.is_library or tab.reader is not None):
            self._focus_active()
            return
        if not tab.is_library and tab.reader is None:
            self._materialize(tab)
        self._active_tab = tab
        if self.tabbar.currentIndex() != i:
            self.tabbar.blockSignals(True)
            self.tabbar.setCurrentIndex(i)
            self.tabbar.blockSignals(False)
        self.stack.setCurrentWidget(self.library if tab.is_library else tab.reader)
        self._rebuild_keys()
        self._update_title()
        self._update_tabrow_visibility()
        self._focus_active()
        self._save_session()

    def _focus_active(self) -> None:
        tab = self._active_tab
        if tab is None:
            return
        if tab.is_library:
            self.library.setFocus(Qt.FocusReason.OtherFocusReason)
        elif tab.reader is not None and tab.reader.book is not None:
            tab.reader.focus_book()

    def _materialize(self, tab: _Tab) -> None:
        """Build (or reuse) the ReaderPage of a book tab and open its book."""
        r = self._pool.pop() if self._pool else self._make_reader()
        tab.reader = r
        self.stack.addWidget(r)
        r.open_book(tab.path)
        if not tab.title:
            tab.title = os.path.splitext(os.path.basename(tab.path))[0]
            i = self._index_of(tab)
            if i >= 0:
                self.tabbar.setTabText(i, self._tab_label(tab))

    def _make_reader(self) -> "ReaderPage":
        r = ReaderPage(self.store, self.stack, theme_controller=self.theme_controller)
        self.stack.addWidget(r)
        # a file dropped on the book goes to the window (and opens), never into Chromium
        r.view.setAcceptDrops(False)
        r.handle_letter_keys = False          # j/k/n/N and / come to the router
        self._bind_reader(r)
        return r

    def _bind_reader(self, r: "ReaderPage") -> None:
        """Rebind only shell signals when a live reader changes windows.

        Reparenting the widget alone leaves Python closures and bound methods
        pointing at the source window. Keep the precise connections so reader
        internals (bookmarks, positions, host and workers) remain untouched.
        """
        for signal, callback in getattr(r, "_window_connections", []):
            signal.disconnect(callback)
        connections = [
            (r.titleChanged, lambda title: self._on_reader_title(r, title)),
            (r.backToLibrary, lambda: self._reader_wants_library(r)),
            (r.bookOpened, lambda bid: self._on_book_opened(r, bid)),
            (r.bookRemoved, lambda _bid: self.library.refresh()),
            (r.openBookRequested, self.open_dialog),
            (r.aboutRequested, self.show_about),
            (r.quitRequested, self.request_quit),
            (r.associateRequested, self.associate_file_type),
            (r.zenChanged, self._on_zen),
            (r.pageKeyUnhandled, self.keys.dispatch_page_key),
        ]
        for signal, callback in connections:
            signal.connect(callback)
        r._window_connections = connections

    def _rebuild_keys(self) -> None:
        r = self.active_reader
        self.keys.set_bindings(build_bindings(r.action_map() if r is not None else {}, self._shell))

    def _refresh_library_cheatsheet(self, _theme: theme_mod.Theme) -> None:
        self.library_cheatsheet.update()

    def new_library_tab(self) -> None:
        """Ctrl+T / the "+" button: a fresh tab showing the shelf."""
        self._session_ready = True
        self._activate_tab(self._add_tab(_Tab(kind="library"),
                                        at=self.tabbar.currentIndex() + 1))

    def next_tab(self) -> None:
        """Ctrl+Tab."""
        if len(self.tabs) > 1:
            self._activate_tab((self.tabbar.currentIndex() + 1) % len(self.tabs))

    def prev_tab(self) -> None:
        """Ctrl+Shift+Tab."""
        if len(self.tabs) > 1:
            self._activate_tab((self.tabbar.currentIndex() - 1) % len(self.tabs))

    def new_window(self) -> "MainWindow":
        """Ctrl+N: another window with its own tabs, the shelf first (like a browser)."""
        w = MainWindow(store=self.store, theme_controller=self.theme_controller, debug=self._debug)
        w._session_ready = True          # a fresh window has nothing to restore
        g = self.normalGeometry() if (self.isMaximized() or self.isFullScreen()) else self.geometry()
        w.resize(*DEFAULT_SIZE)
        w.setGeometry(min(g.x() + 44, 4000), min(g.y() + 44, 4000), *DEFAULT_SIZE)
        w.show()
        w.raise_()
        w.activateWindow()
        self._save_session()
        return w

    def _detach_tab_to_new_window(self, i: int, position: QPoint | None = None) -> None:
        """Move tab *i* into a brand-new window (browser-style tear-off)."""
        if not (0 <= i < len(self.tabs)):
            return
        # Show the new window before closing an emptied source, so moving its
        # last tab never quits the application. Remove the constructor's shelf.
        w = MainWindow(store=self.store, theme_controller=self.theme_controller, debug=self._debug)
        w._session_ready = True
        g = self.normalGeometry() if (self.isMaximized() or self.isFullScreen()) else self.geometry()
        w.setGeometry(g.translated(44, 44))
        if position is not None:
            point = position - QPoint(36, 12)
            screen = QGuiApplication.screenAt(position) or self.screen()
            if screen is not None:
                available = screen.availableGeometry()
                point.setX(max(available.left(), min(point.x(), available.right() - w.width() + 1)))
                point.setY(max(available.top(), min(point.y(), available.bottom() - w.height() + 1)))
            w.move(point)
        w._remove_tab(w.tabs[0])
        w._active_tab = None
        w.show()
        self._move_tab_to_window(i, w, 0)

    def _move_tab_to_window(self, i: int, dst: "MainWindow", at: int | None = None) -> None:
        """Move tab *i* from this window into *dst* (drag-and-drop between windows).

        The reader surface travels with the tab: no re-render, no lost scroll
        position. Closing an emptied source must not close the moved reader,
        shared store, or the single-instance server used by the other windows.
        """
        if not (0 <= i < len(self.tabs)) or dst is self or self._shut or dst._shut:
            return
        tab = self.tabs[i]
        was_active = tab is self._active_tab
        # remove from this window without disposing the reader
        self._remove_tab(tab)
        if self._active_tab is tab:
            self._active_tab = None
        # hand the reader over: remove from this stack, add to the destination's
        r = tab.reader
        if r is not None:
            self.stack.removeWidget(r)
            dst.stack.addWidget(r)
            dst._bind_reader(r)
        # Honor the actual drop gap, or append after the active tab for callers
        # which do not specify a position (e.g. a future window-selection menu).
        at = dst._add_tab(tab, at=dst.tabbar.currentIndex() + 1 if at is None else at)
        if self.tabs and was_active:
            self._activate_tab(min(i, len(self.tabs) - 1))
        else:
            self._rebuild_keys()
        # activate the moved tab in the destination
        dst._activate_tab(at)
        dst.bring_to_front()
        if not self.tabs:
            self.close()
        dst._save_session()
        self._save_session()

    def close_tab(self, i: int) -> None:
        """The strip's close button / middle click: drop tab *i*.

        Closing the last tab closes the window, like a browser.
        """
        if not (0 <= i < len(self.tabs)):
            return
        if len(self.tabs) == 1:
            self.close()
            return
        tab = self.tabs[i]
        was_active = tab is self._active_tab
        neighbour = None
        if was_active:
            j = i + 1 if i + 1 < len(self.tabs) else i - 1
            neighbour = self.tabs[j]
        self._dispose_tab(tab)
        if was_active and neighbour is not None:
            self._activate_tab(self._index_of(neighbour))
        self._save_session()

    def _close_other_tabs(self, keep: int) -> None:
        """The tab context menu: keep tab *keep*, drop the rest."""
        if not (0 <= keep < len(self.tabs)):
            return
        keep_tab = self.tabs[keep]
        for tab in list(self.tabs):
            if tab is not keep_tab:
                self._dispose_tab(tab)
        self._activate_tab(self._index_of(keep_tab))
        self._save_session()

    def _dispose_tab(self, tab: _Tab) -> None:
        """Close the tab's book (saving it), park its reader, remove the tab.

        One reader is always kept parked for the next book, like the single
        reading surface the app has always had; extras are shut down for real.
        """
        r = tab.reader
        if r is not None:
            tab.reader = None
            if r.book is not None:
                try:
                    r.close_book()
                except Exception:  # noqa: BLE001
                    log.exception("closing the book failed")
            self.stack.removeWidget(r)
            if not self._pool:
                self._pool.append(r)
            else:
                try:
                    r.shutdown()
                except Exception:  # noqa: BLE001 - closing one tab must never kill the app
                    log.exception("reader shutdown failed")
                r.deleteLater()
        self._remove_tab(tab)
        if self._active_tab is tab:
            self._active_tab = None
        self._rebuild_keys()

    def _reader_wants_library(self, r: "ReaderPage") -> None:
        """A reader closed its book and asked for the shelf: drop its tab and
        land on a neighbour — the shelf if there is none (the old behaviour)."""
        tab = self._tab_of_reader(r)
        if tab is None:
            return
        if len(self.tabs) == 1:
            # the only tab: swap it for the shelf instead of closing the window
            self._dispose_tab(tab)
            self._add_tab(_Tab(kind="library"), at=0)
            self._activate_tab(0)
            return
        was_active = tab is self._active_tab
        i = self._index_of(tab)
        neighbour = None
        if was_active:
            j = i + 1 if i + 1 < len(self.tabs) else i - 1
            neighbour = self.tabs[j]
        self._dispose_tab(tab)
        if was_active and neighbour is not None:
            self._activate_tab(self._index_of(neighbour))
        if not any(t.is_library for t in self.tabs) and not any(t.reader is not None for t in self.tabs):
            self._add_tab(_Tab(kind="library"))
        self._save_session()

    def _session_slice(self) -> dict:
        """This window's part of the session: its tabs, in order, and the active one."""
        active = self._active_tab
        return {
            "tabs": [t.bid for t in self.tabs if not t.is_library and t.bid],
            "library": any(t.is_library for t in self.tabs),
            "active": ("library" if (active is not None and active.is_library)
                       else (active.bid if active is not None else "")),
            "items": [{"kind": "library"} if t.is_library else {"kind": "book", "bid": t.bid}
                      for t in self.tabs],
            "active_index": self._index_of(active) if active is not None else 0,
        }

    @staticmethod
    def _session_slices(session: dict) -> list[dict]:
        """The stored session as one slice per window (old single-window format
        is one slice)."""
        if isinstance(session, dict) and isinstance(session.get("windows"), list):
            out = [s for s in session["windows"] if isinstance(s, dict)]
            if out:
                return out
        return [session] if session else []

    def _save_session(self) -> None:
        """Remember every window's open tabs (ids in order, which one is active)."""
        if self._shut or not self._session_ready:
            return
        wins = sorted(WINDOWS.live(), key=lambda w: w._slot)
        slices = [w._session_slice() for w in wins if w._session_ready]
        if not slices:
            slices = [self._session_slice()]
        # one window keeps the historical flat shape; several get "windows"
        if len(slices) == 1:
            self.store.set("window.session", slices[0])
        else:
            self.store.set("window.session", {"windows": slices})

    # ---- pages ---------------------------------------------------------------
    def open_path(self, path: str, *, new_tab: bool = False) -> None:
        """Open a book.

        Like a browser: the current tab navigates — a shelf tab becomes the
        book's tab, a book tab swaps its book (the reading surface is reused).
        ``new_tab=True`` (a second launch, a drop on a reading window) opens a
        separate tab instead.          A book already open in some tab just focuses it.
        """
        self._session_ready = True
        path = os.path.abspath(path)
        for t in self.tabs:
            if not t.is_library and t.path and os.path.normcase(t.path) == os.path.normcase(path):
                self._activate_tab(self._index_of(t))
                return
        log.info("open %s", path)
        self.library_cheatsheet.dismiss()
        cur = self._active_tab
        if not new_tab and cur is not None and not cur.is_library and cur.reader is not None:
            # navigate this tab: swap the book in the reader it already has
            cur.path, cur.bid = path, ""
            cur.title = os.path.splitext(os.path.basename(path))[0]
            i = self._index_of(cur)
            if i >= 0:
                self.tabbar.setTabText(i, self._tab_label(cur))
                self.tabbar.setTabToolTip(i, self._tab_label(cur))
            cur.reader.open_book(path)
            self._update_title()
            if cur.reader.book is None:
                self.store.set("window.last_route", {"kind": "library", "book_id": None})
            self._focus_active()
            return
        tab = _Tab(kind="book", path=path, title=os.path.splitext(os.path.basename(path))[0])
        if cur is not None and cur.is_library and not new_tab:
            i = self._index_of(cur)              # the shelf tab becomes the book's tab
            self._dispose_tab(cur)
            i = self._add_tab(tab, at=min(i, len(self.tabs)))
        else:
            i = self._add_tab(tab, at=(self.tabbar.currentIndex() + 1) if cur is not None
                             else len(self.tabs))
        self._activate_tab(i)
        r = tab.reader
        if r is None or r.book is None:
            self.store.set("window.last_route", {"kind": "library", "book_id": None})

    def show_library(self) -> None:
        """Ctrl+T / Ctrl+Shift+L: focus the shelf tab, creating one if needed."""
        for i, t in enumerate(self.tabs):
            if t.is_library:
                self._activate_tab(i)
                return
        at = self.tabbar.currentIndex() + 1
        self._activate_tab(self._add_tab(_Tab(kind="library"), at=at))

    def back_to_library(self) -> None:
        """Ctrl+Shift+L: close the book's tab (saved) and show the shelf."""
        if self.is_reading():
            r = self.active_reader
            if r is not None:
                r.back_to_library()
                return
        self.show_library()

    def close_book_or_window(self) -> None:
        """Ctrl+W closes the active tab; only the last tab closes the window."""
        self.close_tab(self.tabbar.currentIndex())

    def open_dialog(self) -> None:
        """Ctrl+O: the file dialog; the chosen book is shelved, then opened."""
        self.library.open_book_dialog()

    def request_quit(self) -> None:
        """Ctrl+Q / 退出: every window closes, then the app quits."""
        MainWindow._quitting = True
        for w in list(WINDOWS.all):
            w.close()
        QCoreApplication.quit()

    def toggle_cheatsheet(self) -> None:
        """F1 / Ctrl+/ on either page."""
        if self.is_reading():
            r = self.active_reader
            if r is not None:
                r.toggle_cheatsheet()
        elif self.library_cheatsheet.isVisible():
            self.library_cheatsheet.dismiss()
        else:
            self.library_cheatsheet.open()

    def toggle_fullscreen(self) -> None:
        """F11: full screen (through the active reader when there is one)."""
        r = self.active_reader
        if r is not None:
            r.toggle_fullscreen()
        elif self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def toggle_day_night(self) -> None:
        """Ctrl+Shift+D: light ⇄ dark."""
        r = self.active_reader
        if r is not None:
            r.toggle_day_night()
        else:
            self.set_theme_choice("light" if self.theme_controller.theme.is_dark else "dark")

    def set_theme_choice(self, choice: str) -> None:
        """The shelf's theme menu: light / sepia / dark / system."""
        r = self.reader
        if r is not None:
            r.set_theme_choice(choice)
        else:
            choice = store_mod.normalize_theme_choice(choice)
            self.store.set("reader.theme", choice)
            self.theme_controller.set_choice(choice)

    def restore_session(self, slice_dict: dict | None = None) -> None:
        """Reopen the last session's tabs (the active one eagerly, the rest lazy).

        *slice_dict* restores this window's own part of a multi-window session
        (windows after the first pass theirs in); without it the stored session
        is read, and the very first launch after the tab update has no session
        saved yet: fall back to the old single-window ``window.last_route``.
        """
        self._session_ready = True
        restore = bool(self.store.get("behavior.restore_last_book_on_launch", True))
        session = slice_dict if slice_dict is not None else self.store.get("window.session")
        session = session if isinstance(session, dict) else None
        if session is None:
            route = self.store.get("window.last_route") or {}
            if restore and isinstance(route, dict) and route.get("kind") == "book" and route.get("book_id"):
                entry = self.store.library_get(str(route["book_id"])) or {}
                path = str(entry.get("path") or "")
                if path and os.path.isfile(path):
                    self.open_path(path)
                    return
            self._activate_tab(0)
            return
        if not restore:
            self._activate_tab(0)
            return
        # Version 2 preserves each shelf tab, its place among books, and the
        # selected instance. The historical fields still load older sessions.
        items = session.get("items")
        if isinstance(items, list):
            for t in list(self.tabs):
                self._dispose_tab(t)
            active_index = session.get("active_index", 0)
            start = 0
            for original_index, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                if item.get("kind") == "library":
                    t = _Tab(kind="library")
                else:
                    bid = str(item.get("bid") or "")
                    entry = self.store.library_get(bid) or {}
                    path = str(entry.get("path") or "")
                    if not path or not os.path.isfile(path):
                        continue
                    t = _Tab(kind="book", bid=bid, path=path,
                             title=str(entry.get("title") or ""))
                at = self._add_tab(t)
                if original_index == active_index:
                    start = at
            if not self.tabs:
                self._add_tab(_Tab(kind="library"))
            self._activate_tab(start)
            return
        bids = [str(b) for b in (session.get("tabs") or []) if str(b)]
        tabs: list[_Tab] = []
        for bid in bids:
            entry = self.store.library_get(bid) or {}
            path = str(entry.get("path") or "")
            if path and os.path.isfile(path):
                tabs.append(_Tab(kind="book", bid=bid, path=path,
                                 title=str(entry.get("title") or "")))
        want_library = bool(session.get("library")) or not tabs
        active_bid = str(session.get("active") or "")
        cur = self._current_tab()
        if cur is not None and cur.is_library and tabs:
            self._dispose_tab(cur)              # the placeholder shelf tab from __init__
        start = 0
        for t in tabs:
            self._add_tab(t)
        if want_library and not any(t.is_library for t in self.tabs):
            self._add_tab(_Tab(kind="library"))
        for i, t in enumerate(self.tabs):
            if not t.is_library and t.bid == active_bid:
                start = i
                break
        else:
            if want_library:
                start = next((i for i, t in enumerate(self.tabs) if t.is_library), 0)
        self._activate_tab(start)

    def _on_book_opened(self, r: "ReaderPage", bid: str) -> None:
        tab = self._tab_of_reader(r)
        if tab is not None:
            tab.bid = bid
            # ReaderPage can reopen a relocated file directly, bypassing
            # MainWindow.open_path. Follow its authoritative source path so the
            # tab, duplicate detection and restored session all name that file.
            if r.book is not None:
                tab.path = os.path.abspath(os.fspath(r.book.path))
            i = self._index_of(tab)
            if i >= 0:
                self.tabbar.setTabText(i, self._tab_label(tab))
                self.tabbar.setTabToolTip(i, self._tab_label(tab))
        self.store.set("window.last_route", {"kind": "book", "book_id": bid})
        self._save_session()
        # Heal a shelf title that predates a converter fix (DjVu titles used to
        # lose the underscores of the file name they came from).
        book = r.book
        if book is not None and getattr(book, "source_format", "") == "djvu":
            md_title = str((book.metadata or {}).get("title") or "").strip()
            entry = self.store.library_get(bid) or {}
            if md_title and entry.get("title") and str(entry.get("title")).strip() != md_title:
                self.store.library_update(bid, title=md_title)
                if tab is not None and tab.title == str(entry.get("title")):
                    tab.title = md_title
                    i = self._index_of(tab)
                    if i >= 0:
                        self.tabbar.setTabText(i, self._tab_label(tab))

    def _on_reader_title(self, r: "ReaderPage", title: str) -> None:
        tab = self._tab_of_reader(r)
        if tab is not None:
            tab.title = title
            i = self._index_of(tab)
            if i >= 0:
                self.tabbar.setTabText(i, self._tab_label(tab))
                self.tabbar.setTabToolTip(i, self._tab_label(tab))
        if tab is self._active_tab:
            self._update_title()

    def _update_title(self) -> None:
        tab = self._active_tab
        if tab is not None and not tab.is_library:
            r = tab.reader
            if (r is not None and (r.book is not None or r.error_spec() is not None)
                    and tab.title):
                self.setWindowTitle(tab.title)
                return
            if tab.title:
                self.setWindowTitle(tab.title)
                return
        self.setWindowTitle(S("title.library"))

    def retranslate_ui(self) -> None:
        self._retranslate_library_menu()
        self.crash_card.retranslate_ui()
        self.library_cheatsheet.retranslate_ui()
        self.new_tab_btn.setToolTip(S("tabs.new"))
        for i, t in enumerate(self.tabs):
            self.tabbar.setTabText(i, self._tab_label(t))
            self.tabbar.setTabToolTip(i, self._tab_label(t))
            self._style_tab_buttons(i)
        self._update_title()

    # ---- the shelf's "…" menu --------------------------------------------------
    def _build_library_menu(self) -> None:
        """Build the shelf's "…" menu ONCE.  Language changes retranslate it in place:
        a language is picked from this very menu, so rebuilding it then would delete
        the menu inside its own event handler."""
        menu = self.library.more_menu
        acts: dict[str, QAction] = {}

        def add(name: str, slot: Callable[[], Any], target: Any = menu) -> QAction:
            act = target.addAction("")
            act.setObjectName(f"library-menu-{name}")
            act.triggered.connect(lambda _=False: slot())
            acts[name] = act
            return act

        add("open", self.open_dialog)
        add("add_folder", self.library.add_folder_dialog)
        add("new_window", self.new_window)
        menu.addSeparator()
        self._lang_menu = menu.addMenu("")
        self._lang_menu.setObjectName("LibraryLanguageMenu")
        group = QActionGroup(self._lang_menu)
        self._lang_actions: dict[str, QAction] = {}
        for value, _label in strings.language_choices():
            act = add(f"lang-{value}", lambda v=value: self.set_language(v), self._lang_menu)
            act.setCheckable(True)
            act.setData(value)
            group.addAction(act)
            self._lang_actions[value] = act
        self._theme_menu = menu.addMenu("")
        self._theme_menu.setObjectName("LibraryThemeMenu")
        tgroup = QActionGroup(self._theme_menu)
        self._theme_actions: dict[str, QAction] = {}
        for name in store_mod.THEME_CHOICES:
            act = add(f"theme-{name}", lambda n=name: self.set_theme_choice(n), self._theme_menu)
            act.setCheckable(True)
            tgroup.addAction(act)
            self._theme_actions[name] = act
        menu.addSeparator()
        add("shortcuts", self.toggle_cheatsheet)
        add("about", self.show_about)
        menu.addSeparator()
        add("quit", self.request_quit)
        self._library_actions = acts
        menu.aboutToShow.connect(self._retranslate_library_menu)
        self._retranslate_library_menu()

    def _retranslate_library_menu(self) -> None:
        a = self._library_actions
        a["open"].setText(S("menu.open") + "	" + KEYS["open"][0])
        a["add_folder"].setText(S("lib.add_folder"))
        a["new_window"].setText(S("menu.new_window") + "	" + KEYS["new_window"][0])
        a["shortcuts"].setText(S("menu.shortcuts") + "	" + KEYS["cheatsheet"][0])
        a["about"].setText(S("menu.about"))
        a["quit"].setText(S("menu.quit") + "	" + KEYS["quit"][0])
        self._lang_menu.setTitle(S("set.language"))
        self._theme_menu.setTitle(S("set.section.theme"))
        pref = strings.language_preference()
        for value, label in strings.language_choices():
            act = self._lang_actions[value]
            act.setText(label)
            act.setChecked(value == pref)
        choice = self.theme_controller.choice
        for name, act in self._theme_actions.items():
            act.setText(theme_mod.display_name(name))
            act.setChecked(name == choice)
        self.library.more_btn.setToolTip(S("tb.more"))

    def set_language(self, value: str) -> None:
        """The UI language (``auto`` or a code), live, remembered in ``ui.language``."""
        self.store.set("ui.language", value)
        strings.set_language(value)

    # ---- dialogs and requests ---------------------------------------------------
    def show_about(self) -> None:
        if self._about is None:
            self._about = AboutDialog(self.store, self)
        self._about.show()
        self._about.raise_()
        self._about.activateWindow()

    def associate_file_type(self) -> None:
        """设为默认 EPUB 阅读器: registers the packaged exe for .epub (current user only)."""
        script = webhost.resource_path("tools", "install-file-association.ps1")
        if not getattr(sys, "frozen", False) or not os.path.isfile(script):
            log.info("file association needs the packaged exe; not registering python.exe")
            self._notify_reader(lambda: S("set.assoc.failed"))
            return
        from PySide6.QtCore import QProcess

        proc = QProcess(self)

        def done(code: int, _status: Any) -> None:
            ok = code == 0
            log.info("file association script exited %s", code)
            self._notify_reader(lambda: S("set.assoc.done" if ok else "set.assoc.failed"))
            proc.deleteLater()

        proc.finished.connect(done)
        proc.start("powershell.exe", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script,
                                      "-ExePath", sys.executable])

    def _notify_reader(self, text: Callable[[], str]) -> None:
        r = self.reader
        if r is not None:
            r.show_message(text, ms=6000)

    def show_internal_error(self, text: str) -> None:
        """The non-fatal error card (called by the exception hook)."""
        self.crash_card.show_error(text)

    # ---- single instance --------------------------------------------------------
    def attach_instance(self, inst: SingleInstance) -> None:
        self.instance = inst
        inst.messageReceived.connect(self.handle_instance_message)

    def handle_instance_message(self, msg: str) -> None:
        """``OPEN <path>`` from a second launch (or ``ACTIVATE``)."""
        log.info("second launch: %s", msg)
        cmd, _, arg = msg.partition(" ")
        target = WINDOWS.focused(default=self) or self
        if cmd == "OPEN" and arg.strip():
            target.open_path(arg.strip(), new_tab=True)
        else:
            # a bare second launch: a fresh tab on the shelf, like a browser
            target.new_library_tab()
        target.bring_to_front()

    def bring_to_front(self) -> None:
        if self.isMinimized():
            self.setWindowState((self.windowState() & ~Qt.WindowState.WindowMinimized)
                                | Qt.WindowState.WindowActive)
        self.show()
        self.raise_()
        self.activateWindow()

    # ---- window state -------------------------------------------------------------
    def _wkey(self, base: str) -> str:
        """This window's variant of a ``window.*`` setting: slot 0 (the only
        window, and the historical one) keeps the plain key, later windows get
        ``<base>.w<n>`` so they do not fight over one geometry."""
        return base if self._slot == 0 else f"{base}.w{self._slot}"

    def restore_window_state(self) -> None:
        """Geometry, maximized and full screen from ``window.*``; then show."""
        g = self.store.get(self._wkey("window.geometry")) or {}
        try:
            x, y = int(g.get("x", -1)), int(g.get("y", -1))
            w = max(MIN_SIZE[0], int(g.get("w", DEFAULT_SIZE[0])))
            h = max(MIN_SIZE[1], int(g.get("h", DEFAULT_SIZE[1])))
        except (TypeError, ValueError, AttributeError):
            x, y, (w, h) = -1, -1, DEFAULT_SIZE
        screens = QGuiApplication.screens()
        target = QRect(x, y, w, h)
        visible = any(s.availableGeometry().intersected(target).width() >= 200
                      and s.availableGeometry().intersected(target).height() >= 120 for s in screens)
        if x >= -10000 and y >= -10000 and (x, y) != (-1, -1) and visible:
            self.setGeometry(target)
        else:
            screen = QGuiApplication.primaryScreen()
            name = str(self.store.get(self._wkey("window.screen")) or "")
            for s in screens:
                if s.name() == name:
                    screen = s
            avail = screen.availableGeometry() if screen is not None else QRect(0, 0, *DEFAULT_SIZE)
            w, h = min(w, avail.width()), min(h, avail.height())
            self.setGeometry(avail.x() + (avail.width() - w) // 2, avail.y() + (avail.height() - h) // 2, w, h)
        self._last_maximized = bool(self.store.get(self._wkey("window.maximized"), False))
        if self._last_maximized:
            self.showMaximized()
        else:
            self.show()
        if bool(self.store.get(self._wkey("window.fullscreen"), False)):
            r = self.active_reader
            if r is not None:
                r.toggle_fullscreen()
            else:
                self.showFullScreen()

    def save_window_state(self) -> None:
        r = self.reader
        zen = bool(r is not None and r.is_zen())
        fs = self._fs_outside_zen if zen else self.isFullScreen()
        maximized = self._last_maximized if self.isFullScreen() else self.isMaximized()
        g = self.normalGeometry() if (self.isMaximized() or self.isFullScreen()) else self.geometry()
        if g.width() <= 0 or g.height() <= 0:
            g = self.geometry()
        screen = self.screen()
        self.store.update({
            self._wkey("window.geometry"): {"x": g.x(), "y": g.y(), "w": g.width(), "h": g.height()},
            self._wkey("window.maximized"): bool(maximized),
            self._wkey("window.fullscreen"): bool(fs),
            self._wkey("window.zen"): False,
            self._wkey("window.screen"): screen.name() if screen is not None else "",
        })

    def _on_zen(self, _on: bool) -> None:
        self._geom_timer.start()
        self._update_tabrow_visibility()

    def _update_tabrow_visibility(self) -> None:
        """The tab strip hides with the rest of the chrome (focus mode, full screen)."""
        r = self.active_reader
        hide = self.isFullScreen() or (r is not None and r.is_zen())
        self.tabrow.setVisible(not hide)

    # ---- events ---------------------------------------------------------------------
    def changeEvent(self, event: QEvent) -> None:  # noqa: N802
        if event.type() == QEvent.Type.WindowStateChange:
            if not self.isFullScreen() and not self.isMinimized():
                self._last_maximized = self.isMaximized()
            r = self.reader
            if r is None or not r.is_zen():
                # focus mode enters full screen itself; remember the state outside it
                self._fs_outside_zen = self.isFullScreen()
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._geom_timer.start()
            self._update_tabrow_visibility()

    def moveEvent(self, event: Any) -> None:  # noqa: N802
        super().moveEvent(event)
        if self.isVisible():
            self._geom_timer.start()

    def resizeEvent(self, event: Any) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.crash_card.place()
        if self.isVisible():
            self._geom_timer.start()

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        if obj is self.library and event.type() == QEvent.Type.Resize \
                and self.library_cheatsheet.isVisible():
            self.library_cheatsheet.setGeometry(self.library.rect())
        return False

    @staticmethod
    def _dropped_epubs(event: Any) -> list[str]:
        mime = event.mimeData()
        if mime is None or not mime.hasUrls():
            return []
        return [u.toLocalFile() for u in mime.urls()
                if u.isLocalFile() and bookformats.is_book_file(u.toLocalFile())]

    def dragEnterEvent(self, event: Any) -> None:  # noqa: N802
        if self.is_reading() and self._dropped_epubs(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: Any) -> None:  # noqa: N802
        self.dragEnterEvent(event)

    def dropEvent(self, event: Any) -> None:  # noqa: N802
        paths = self._dropped_epubs(event)
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        if len(paths) > 1:
            self.library.add_paths(paths[1:], open_single=False)
        # dropped on a book: a new tab, like a browser; on the shelf: navigate it
        self.open_path(paths[0], new_tab=self.is_reading())

    def closeEvent(self, event: Any) -> None:  # noqa: N802
        self.shutdown()
        if not MainWindow._quitting:
            # One window closed by the user: its tabs do not come back, so the
            # session is rewritten from the windows that remain.  The last
            # window leaves the session untouched — its tabs are restored on
            # the next launch, exactly as the single-window app always did.
            try:
                live = WINDOWS.live()
                if live and live[0]._session_ready:
                    live[0]._save_session()
            except Exception:  # noqa: BLE001
                log.exception("could not rewrite the session")
        super().closeEvent(event)

    def shutdown(self) -> None:
        """Save the window, close every book, stop workers, flush the Store.  Idempotent."""
        if self._shut:
            return
        try:
            self.save_window_state()
        except Exception:  # noqa: BLE001
            log.exception("could not save the window state")
        self._shut = True
        WINDOWS.remove(self)
        self._geom_timer.stop()
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self.keys)
            app.aboutToQuit.disconnect(self.shutdown)
        strings.language_changed.unsubscribe(self.retranslate_ui)
        self.theme_controller.themeChanged.disconnect(self._refresh_library_cheatsheet)
        readers = [t.reader for t in self.tabs if t.reader is not None] + list(self._pool)
        seen: set[int] = set()
        for r in readers:
            if id(r) in seen:
                continue
            seen.add(id(r))
            try:
                r.shutdown()
            except Exception:  # noqa: BLE001
                log.exception("reader shutdown failed")
        self._pool.clear()
        try:
            self.library.shutdown()
        except Exception:  # noqa: BLE001
            log.exception("library shutdown failed")
        remaining = [w for w in WINDOWS.live() if w.store is self.store]
        if self.instance is not None:
            self.instance.messageReceived.disconnect(self.handle_instance_message)
            if remaining:
                remaining[0].attach_instance(self.instance)
            else:
                self.instance.close()
            self.instance = None
        ok = self.store.flush()
        log.info("shutdown: store flushed=%s", ok)
        if self._own_store:
            if remaining:
                remaining[0]._own_store = True
                self._own_store = False
            else:
                self.store.close()


# ==========================================================================
# command line
# ==========================================================================

@dataclass
class Options:
    path: str | None = None
    help: bool = False
    debug: bool = False
    extra: tuple[str, ...] = ()


def parse_args(args: Iterable[str]) -> Options:
    """``[--help] [--debug] [book.epub]``.  Unknown extra arguments are kept for the log."""
    opts = Options()
    extra: list[str] = []
    for a in args:
        if a in ("-h", "--help", "/?", "-?"):
            opts.help = True
        elif a == "--debug":
            opts.debug = True
        elif a.startswith("-") and not os.path.exists(a):
            extra.append(a)
        elif opts.path is None:
            opts.path = a
        else:
            extra.append(a)
    opts.extra = tuple(extra)
    return opts


def help_text(exe: str) -> str:
    return "\n".join((
        S("cli.usage", exe=exe),
        "",
        S("cli.description"),
        "",
        f"  -h, --help    {S('cli.help')}",
        "",
    ))


def _exe_name() -> str:
    if getattr(sys, "frozen", False):
        return os.path.basename(sys.executable)
    return os.path.basename(sys.argv[0] or "epub_reader.py")


def _set_app_user_model_id() -> None:
    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
        except Exception:  # noqa: BLE001
            pass


def main(argv: Sequence[str] | None = None, *,
         on_started: Callable[[MainWindow], Any] | None = None) -> int:
    """Run the app.  Returns the process exit code.

    *on_started* (tests only) is called with the window once the event loop runs.
    """
    argv = list(sys.argv if argv is None else argv)
    opts = parse_args(argv[1:])
    debug = opts.debug or _debug_default()

    if opts.help:
        strings.set_language(_quick_setting("ui.language", "auto"))
        text = help_text(_exe_name())
        if sys.stdout is not None:
            try:
                sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                pass
            sys.stdout.write(text)
            sys.stdout.flush()
        else:                                   # windowed build: no console to print to
            from PySide6.QtWidgets import QMessageBox

            _app = QApplication.instance() or QApplication(argv[:1])
            QMessageBox.information(None, strings.APP_DISPLAY_NAME, text)
        return 0

    notes = store_mod.migrate_legacy_dirs()
    log_path = setup_logging(debug=debug)
    log.info("start %s %s (Python %s, PySide6 %s) argv=%r", strings.APP_DISPLAY_NAME, APP_VERSION,
             sys.version.split()[0], __import__("PySide6").__version__, argv[1:])
    for n in notes:
        log.info("migration: %s", n)
    if opts.extra:
        log.info("ignored arguments: %r", opts.extra)

    webhost.register_epub_scheme()
    if QApplication.instance() is None:
        # HiDPI: Qt 6 scales by default; never round the factor (125%/150% stay exact)
        QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
        QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    _set_app_user_model_id()
    app = QApplication.instance() or QApplication(argv[:1])
    app.setOrganizationName(APP_DIR_NAME)
    app.setApplicationName(APP_DIR_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setWindowIcon(QIcon(webhost.asset_path("app.ico")))
    install_exception_hooks()

    path = os.path.abspath(opts.path) if opts.path else None
    inst: SingleInstance | None = None
    if bool(_quick_setting("behavior.single_instance", True)):
        inst = SingleInstance(PIPE_NAME)
        if inst.forward(f"OPEN {path}" if path else "ACTIVATE"):
            log.info("handed over to the running instance: %s", path or "(activate)")
            return 0
        if inst.server_found:
            # A running instance owns the pipe but did not answer (busy or hung).
            # Open a standalone window, but never start a second server on the same
            # pipe name: Windows would allow it, and later launches would then reach
            # either process unpredictably.
            log.warning("single instance: the running instance did not answer; "
                        "opening a separate window without claiming the pipe")
            inst = None
        elif not inst.listen():
            inst = None

    try:
        store = Store()
        for n in store.load_notes:
            log.warning("store: %s", n)
        # one theme controller for every window: a theme picked in any window
        # applies to all of them (it targets the application anyway)
        theme_controller = theme_mod.ThemeController(app, store.get("reader.theme", "system"))
        win = MainWindow(store=store, theme_controller=theme_controller, debug=debug)
    except Exception:
        log.exception("start-up failed")
        from PySide6.QtWidgets import QMessageBox

        QMessageBox.critical(None, strings.APP_DISPLAY_NAME,
                             S("err.crash.title") + "\n\n" + S("err.crash.body") + "\n" + log_path)
        return 1
    if debug:
        log.debug("key map: %s", win.keys.table())
    if inst is not None:
        win.attach_instance(inst)
    win.restore_window_state()
    if path:
        win.open_path(path)
    else:
        # a multi-window session: every window after the first is restored here
        # Snapshot before activating a tab: activation saves the currently live
        # windows and would otherwise erase windows not yet constructed.
        slices = MainWindow._session_slices(store.get("window.session") or {})
        win.restore_session(slices[0] if slices else None)
        for sl in slices[1:]:
            extra_win: MainWindow | None = None
            try:
                extra_win = MainWindow(store=store, theme_controller=theme_controller, debug=debug)
                extra_win.restore_window_state()
                extra_win.restore_session(sl)
            except Exception:  # noqa: BLE001
                log.exception("could not restore a session window")
                if extra_win is not None:
                    try:
                        extra_win.close()
                    except Exception:  # noqa: BLE001
                        pass
    if on_started is not None:
        QTimer.singleShot(0, lambda: on_started(win))
    rc = app.exec()
    for w in list(WINDOWS.all):
        w.shutdown()
    win.shutdown()
    if inst is not None:
        inst.close()
    log.info("exit %s", rc)
    for h in logging.getLogger().handlers:
        try:
            h.flush()
        except Exception:  # noqa: BLE001
            pass
    return int(rc)


if __name__ == "__main__":
    sys.exit(main())
