# -*- coding: utf-8 -*-
"""EPUB Reader — application entry point (owner G, CONTRACT §8).

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
6. builds :class:`MainWindow` — a ``QStackedWidget`` over ``LibraryPage`` and
   ``ReaderPage`` — restores the window geometry and opens the book, the last
   book, or the shelf;
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
import weakref
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

import webhost  # registers epub:// at import time; must precede QApplication

from PySide6.QtCore import (
    QCoreApplication,
    QEvent,
    QLibraryInfo,
    QLocale,
    QObject,
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
    QDesktopServices,
    QFont,
    QGuiApplication,
    QIcon,
    QKeyEvent,
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
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

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
APP_USER_MODEL_ID = "EPUBReader.App.1"
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
    """Open ``<state root>\\logs\\epub-reader.log`` (1 MB x 3) and return its path.

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
    "-": _iv(_K.Key_Minus), "Plus": _iv(_K.Key_Plus),
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
    "open", "library", "close_book", "quit", "fullscreen", "cheatsheet", "toggle_daynight"})
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
        if b.owner == "shell" and b.handler is None:
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
        self.bindings: list[Binding] = list(bindings)
        self.problems = audit_bindings(self.bindings)
        if self.problems:
            msg = "keyboard map conflicts:\n  " + "\n  ".join(self.problems)
            if debug:
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
        #: The last binding fired (tests and the log).
        self.last_fired: str = ""

    # -- queries --------------------------------------------------------------
    def binding_for(self, scope: str, label: str) -> Binding | None:
        key, mods = parse_combo(label)
        return self._qt.get((scope, key, mods)) or self._qt.get(("global", key, mods))

    def _in_view(self, w: QWidget | None) -> bool:
        view = self._win.reader.view
        return w is not None and (w is view or view.isAncestorOf(w) or w is view.focusProxy())

    def _interactive(self, w: QWidget | None) -> bool:
        """A focused control that may use a plain key itself (list, slider, field, button)."""
        if w is None or self._in_view(w):
            return False
        win = self._win
        return w not in (win, win.stack, win.reader, win.reader.column, win.library)

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
        if b.scope == "reader" and b.needs_book and reader.book is None:
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
        if b.needs_book and self._win.reader.book is None:
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
    """关于 EPUB Reader: version, what it was built with, and where the data lives."""

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
# the main window
# ==========================================================================

class MainWindow(QMainWindow):
    """The one window: ``QStackedWidget`` over the shelf and the reader.

    ``MainWindow(path=None, *, store=None, theme_controller=None, debug=None)``.
    Without *store* it opens the default Store (``%APPDATA%\\EPUB Reader``) and
    closes it when the window closes.  *path* opens that book right away.
    """

    _current: "weakref.ReferenceType[MainWindow] | None" = None

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
        self._reader_title = ""
        self._fs_outside_zen = False
        self._last_maximized = False
        self._about: AboutDialog | None = None
        self.instance: SingleInstance | None = None

        strings.set_language(self.store.get("ui.language", "auto"))
        self._qt_tr = QtTranslations(app)
        self.theme_controller = theme_controller or theme_mod.ThemeController(
            app, self.store.get("reader.theme", "system"), self)

        self.setObjectName("er-main-window")
        self.setWindowIcon(QIcon(webhost.asset_path("app.ico")))
        self.setMinimumSize(*MIN_SIZE)
        self.resize(*DEFAULT_SIZE)
        self.setAcceptDrops(True)

        self.stack = QStackedWidget(self)
        self.stack.setObjectName("er-stack")
        self.library = LibraryPage(self.store, self.stack, theme=self.theme_controller.theme)
        self.reader = ReaderPage(self.store, self.stack, theme_controller=self.theme_controller)
        self.stack.addWidget(self.library)
        self.stack.addWidget(self.reader)
        self.setCentralWidget(self.stack)
        # a file dropped on the book goes to the window (and opens), never into Chromium
        self.reader.view.setAcceptDrops(False)
        self.library_cheatsheet = CheatSheet(self.library, lambda: self.theme_controller.theme)
        self.library_cheatsheet.closed.connect(lambda: self.library.setFocus())
        self.library.installEventFilter(self)
        self.crash_card = CrashCard(self, lambda: store_mod.log_file(self.store.root))

        # ---- wiring ----------------------------------------------------------
        r, lib = self.reader, self.library
        r.titleChanged.connect(self._on_reader_title)
        r.backToLibrary.connect(self.show_library)
        r.bookOpened.connect(self._on_book_opened)
        r.bookRemoved.connect(lambda _bid: self.library.refresh())
        r.openBookRequested.connect(self.open_dialog)
        r.aboutRequested.connect(self.show_about)
        r.quitRequested.connect(self.request_quit)
        r.associateRequested.connect(self.associate_file_type)
        r.zenChanged.connect(self._on_zen)
        lib.openBook.connect(self.open_path)
        self.theme_controller.themeChanged.connect(lib.apply_theme)
        self.theme_controller.themeChanged.connect(lambda _t: self.library_cheatsheet.update())
        self._build_library_menu()
        strings.language_changed.subscribe(self.retranslate_ui)

        # ---- the keyboard map --------------------------------------------------
        r.handle_letter_keys = False          # j/k/n/N and / come to the router
        shell = {
            "open": self.open_dialog,
            "library": self.back_to_library,
            "close_book": self.close_book_or_window,
            "quit": self.request_quit,
            "fullscreen": self.reader.toggle_fullscreen,
            "cheatsheet": self.toggle_cheatsheet,
            "toggle_daynight": self.reader.toggle_day_night,
        }
        self.keys = KeyRouter(self, build_bindings(r.action_map(), shell), debug=self._debug)
        app.installEventFilter(self.keys)
        r.pageKeyUnhandled.connect(self.keys.dispatch_page_key)

        # ---- state saving --------------------------------------------------------
        self._geom_timer = QTimer(self)
        self._geom_timer.setSingleShot(True)
        self._geom_timer.setInterval(600)
        self._geom_timer.timeout.connect(self.save_window_state)
        app.aboutToQuit.connect(self.shutdown)

        MainWindow._current = weakref.ref(self)
        self.stack.setCurrentWidget(self.library)
        self._update_title()
        if path is not None:
            self.open_path(os.fspath(path))

    # ------------------------------------------------------------------
    @classmethod
    def current(cls) -> "MainWindow | None":
        """The live main window, or None."""
        ref = cls._current
        win = ref() if ref is not None else None
        try:
            if win is not None and not win._shut:
                win.objectName()           # raises if the C++ side is gone
                return win
        except RuntimeError:
            pass
        return None

    def is_reading(self) -> bool:
        return self.stack.currentWidget() is self.reader

    # ---- pages ---------------------------------------------------------------
    def open_path(self, path: str) -> None:
        """Open a book (shelf double-click, Ctrl+O, argv, a second launch, a drop)."""
        path = os.path.abspath(path)
        r = self.reader
        cur = r.book
        if cur is not None and os.path.normcase(os.path.abspath(cur.path)) == os.path.normcase(path):
            self._show_reader()
            r.focus_book()
            return
        log.info("open %s", path)
        self.library_cheatsheet.dismiss()
        r.open_book(path)
        self._show_reader()
        if r.book is None:
            self.store.set("window.last_route", {"kind": "library", "book_id": None})

    def _show_reader(self) -> None:
        if self.stack.currentWidget() is not self.reader:
            self.stack.setCurrentWidget(self.reader)
        self._update_title()
        if self.reader.book is not None:
            self.reader.focus_book()

    def show_library(self) -> None:
        """The shelf (the reader has already closed its book)."""
        self.stack.setCurrentWidget(self.library)
        self.store.set("window.last_route", {"kind": "library", "book_id": None})
        self._update_title()
        self.library.setFocus(Qt.FocusReason.OtherFocusReason)

    def back_to_library(self) -> None:
        """Ctrl+Shift+L: close the book (saved) and show the shelf."""
        if self.is_reading():
            self.reader.back_to_library()
        else:
            self.show_library()

    def close_book_or_window(self) -> None:
        """Ctrl+W: close the book and return to the shelf; on the shelf, close the window."""
        if self.is_reading():
            self.reader.close_and_return()
            self.library.notify(lambda: S("status.back_to_library"), timeout_ms=BACK_TO_LIBRARY_NOTE_MS)
        else:
            self.close()

    def open_dialog(self) -> None:
        """Ctrl+O: the file dialog; the chosen book is shelved, then opened."""
        self.library.open_book_dialog()

    def request_quit(self) -> None:
        """Ctrl+Q / 退出."""
        self.close()
        QCoreApplication.quit()

    def toggle_cheatsheet(self) -> None:
        """F1 / Ctrl+/ on either page."""
        if self.is_reading():
            self.reader.toggle_cheatsheet()
        elif self.library_cheatsheet.isVisible():
            self.library_cheatsheet.dismiss()
        else:
            self.library_cheatsheet.open()

    def restore_last_route(self) -> None:
        """Reopen the last book if the setting allows it and the file is there."""
        route = self.store.get("window.last_route") or {}
        if bool(self.store.get("behavior.restore_last_book_on_launch", True)) \
                and isinstance(route, dict) and route.get("kind") == "book" and route.get("book_id"):
            entry = self.store.library_get(str(route["book_id"])) or {}
            path = str(entry.get("path") or "")
            if path and os.path.isfile(path):
                self.open_path(path)
                return
        self.show_library()

    def _on_book_opened(self, bid: str) -> None:
        self.store.set("window.last_route", {"kind": "book", "book_id": bid})

    def _on_reader_title(self, title: str) -> None:
        self._reader_title = title
        self._update_title()

    def _update_title(self) -> None:
        r = self.reader
        if self.is_reading() and (r.book is not None or r.error_spec() is not None) and self._reader_title:
            self.setWindowTitle(self._reader_title)
        else:
            self.setWindowTitle(S("title.library"))

    def retranslate_ui(self) -> None:
        self._retranslate_library_menu()
        self.crash_card.retranslate_ui()
        self.library_cheatsheet.retranslate_ui()
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
            act = add(f"theme-{name}", lambda n=name: self.reader.set_theme_choice(n), self._theme_menu)
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
            self.reader.show_message(lambda: S("set.assoc.failed"), ms=6000)
            return
        from PySide6.QtCore import QProcess

        proc = QProcess(self)

        def done(code: int, _status: Any) -> None:
            ok = code == 0
            log.info("file association script exited %s", code)
            self.reader.show_message(lambda: S("set.assoc.done" if ok else "set.assoc.failed"), ms=6000)
            proc.deleteLater()

        proc.finished.connect(done)
        proc.start("powershell.exe", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script,
                                      "-ExePath", sys.executable])

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
        if cmd == "OPEN" and arg.strip():
            self.open_path(arg.strip())
        self.bring_to_front()

    def bring_to_front(self) -> None:
        if self.isMinimized():
            self.setWindowState((self.windowState() & ~Qt.WindowState.WindowMinimized)
                                | Qt.WindowState.WindowActive)
        self.show()
        self.raise_()
        self.activateWindow()

    # ---- window state -------------------------------------------------------------
    def restore_window_state(self) -> None:
        """Geometry, maximized and full screen from ``window.*``; then show."""
        g = self.store.get("window.geometry") or {}
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
            name = str(self.store.get("window.screen") or "")
            for s in screens:
                if s.name() == name:
                    screen = s
            avail = screen.availableGeometry() if screen is not None else QRect(0, 0, *DEFAULT_SIZE)
            w, h = min(w, avail.width()), min(h, avail.height())
            self.setGeometry(avail.x() + (avail.width() - w) // 2, avail.y() + (avail.height() - h) // 2, w, h)
        self._last_maximized = bool(self.store.get("window.maximized", False))
        if self._last_maximized:
            self.showMaximized()
        else:
            self.show()
        if bool(self.store.get("window.fullscreen", False)):
            self.reader.toggle_fullscreen()

    def save_window_state(self) -> None:
        fs = self._fs_outside_zen if self.reader.is_zen() else self.isFullScreen()
        maximized = self._last_maximized if self.isFullScreen() else self.isMaximized()
        g = self.normalGeometry() if (self.isMaximized() or self.isFullScreen()) else self.geometry()
        if g.width() <= 0 or g.height() <= 0:
            g = self.geometry()
        screen = self.screen()
        self.store.update({
            "window.geometry": {"x": g.x(), "y": g.y(), "w": g.width(), "h": g.height()},
            "window.maximized": bool(maximized),
            "window.fullscreen": bool(fs),
            "window.zen": False,
            "window.screen": screen.name() if screen is not None else "",
        })

    def _on_zen(self, _on: bool) -> None:
        self._geom_timer.start()

    # ---- events ---------------------------------------------------------------------
    def changeEvent(self, event: QEvent) -> None:  # noqa: N802
        if event.type() == QEvent.Type.WindowStateChange:
            if not self.isFullScreen() and not self.isMinimized():
                self._last_maximized = self.isMaximized()
            if not self.reader.is_zen():
                # focus mode enters full screen itself; remember the state outside it
                self._fs_outside_zen = self.isFullScreen()
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._geom_timer.start()

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
                if u.isLocalFile() and u.toLocalFile().lower().endswith(".epub")]

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
        self.open_path(paths[0])

    def closeEvent(self, event: Any) -> None:  # noqa: N802
        self.shutdown()
        super().closeEvent(event)

    def shutdown(self) -> None:
        """Save the window, close the book, stop workers, flush the Store.  Idempotent."""
        if self._shut:
            return
        try:
            self.save_window_state()
        except Exception:  # noqa: BLE001
            log.exception("could not save the window state")
        self._shut = True
        self._geom_timer.stop()
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self.keys)
        try:
            self.reader.shutdown()
        except Exception:  # noqa: BLE001
            log.exception("reader shutdown failed")
        try:
            self.library.shutdown()
        except Exception:  # noqa: BLE001
            log.exception("library shutdown failed")
        if self.instance is not None:
            self.instance.close()
        ok = self.store.flush()
        log.info("shutdown: store flushed=%s", ok)
        if self._own_store:
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
        win = MainWindow(store=store, debug=debug)
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
        win.restore_last_route()
    if on_started is not None:
        QTimer.singleShot(0, lambda: on_started(win))
    rc = app.exec()
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
