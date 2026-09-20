# -*- coding: utf-8 -*-
"""Book Reader — persistence.  Human-readable JSON, atomic writes, one writer thread.

Layout (all of it computed from ``os.environ``, never from ``QStandardPaths``,
because ``QStandardPaths.AppConfigLocation`` returns AppData\\**Local** on
Windows and would put the user's library in the wrong place)::

    %APPDATA%\\Book Reader\\settings.json          settings, window state, typography
    %APPDATA%\\Book Reader\\library.json           the shelf, one entry per known book
    %APPDATA%\\Book Reader\\books\\<id>.json       position, bookmarks, highlights, overrides
    %APPDATA%\\Book Reader\\books\\<id>.json.bak   the previous good generation
    %APPDATA%\\Book Reader\\logs\\book-reader.log  the log (owner G writes it)
    %LOCALAPPDATA%\\Book Reader\\cache\\covers\\    regenerable cover thumbnails

Every write is: serialize under the data lock -> ``copy2`` the current file to
``.bak`` -> ``mkstemp`` in the same directory -> write -> ``flush`` ->
``os.fsync`` -> ``os.replace``.  That is atomic on NTFS and leaves no temporary
files behind (verified, including under ``TerminateProcess`` mid-write).

Every read is: primary -> ``.bak`` -> quarantine the wreckage as
``<name>.json.corrupt-<epoch>`` and start from defaults.  Nothing the user
might want back is ever deleted.

Write policy, from ``docs/research/product-spec.md`` §4.  The ``.bak`` holds the
*previous* generation, so a crash costs exactly one generation; that is why the
debounces differ:

===================  ===========================================================
settings.json        debounced 500 ms, plus on quit
books/<id>.json      position debounced 2000 ms; forced on chapter change, book
                     close, window deactivate and quit.  **Bookmarks, highlights
                     and notes are written immediately and never debounced.**
library.json         250 ms after an add/remove (coalesces a folder import),
                     60 s for progress churn, plus on book close and on quit
===================  ===========================================================

All writes go through one serialized writer thread so the UI never blocks on a
40-60 ms fsync.  The debounce is a *bounded-latency* debounce: repeated changes
to the same file replace the payload but keep the earliest deadline, so a
continuously scrolling reader still gets a write every 2 s instead of never.

Usage::

    store = Store()
    store.settings.reader.font_size_px = 23     # debounced 500 ms
    store.set("window.dock_width", 320)         # same thing, dotted
    store.add_highlight(book_id, hl)            # on disk before this returns
    store.flush()                               # on quit
"""

from __future__ import annotations

import atexit
import contextlib
import copy
import datetime
import hashlib
import json
import os
import secrets
import shutil
import tempfile
import threading
import time
import weakref
from typing import Any, Callable, Final

__all__ = [
    # identity (DECISIONS.md §1) — defined ONCE here, imported everywhere
    "APP_DIR_NAME",
    "PROG_ID",
    "PIPE_NAME",
    "LOG_FILE_NAME",
    "LEGACY_DIR_NAME",
    # value vocabularies
    "UI_LANGUAGES",
    "THEME_CHOICES",
    "LEGACY_THEME_ALIASES",
    "normalize_language",
    "normalize_theme_choice",
    # locations
    "app_dir",
    "cache_dir",
    "cover_dir",
    "log_dir",
    "log_file",
    "books_dir",
    "migrate_legacy_dirs",
    # primitives
    "SCHEMA",
    "now_iso",
    "new_id",
    "book_id",
    "write_json",
    "read_json",
    "MIGRATIONS",
    # defaults
    "DEFAULT_SETTINGS",
    "default_settings",
    "default_library",
    "default_book_state",
    # policy
    "SETTINGS_DEBOUNCE_MS",
    "POSITION_DEBOUNCE_MS",
    "LIBRARY_DEBOUNCE_MS",
    "LIBRARY_IDLE_MS",
    # the store
    "Store",
    "Settings",
]

#: On-disk format version.  Every file carries it as a top-level integer.
SCHEMA: Final[int] = 1

# --------------------------------------------------------------------------
# identity — DECISIONS.md §1.  Import these; never retype them.
# --------------------------------------------------------------------------

#: Directory name under %APPDATA% and %LOCALAPPDATA%; also the Qt
#: organization/application name.
APP_DIR_NAME: Final[str] = "Book Reader"
#: Registry ProgId for the .epub association (no spaces allowed).
PROG_ID: Final[str] = "BookReader.Book.1"
#: QLocalServer name for single-instance hand-off (\\.\pipe\book-reader-single-instance).
PIPE_NAME: Final[str] = "book-reader-single-instance"
#: Log file name inside :func:`log_dir`.
LOG_FILE_NAME: Final[str] = "book-reader.log"
#: Retired names, newest first ("EPUB Reader" until v1.1, "Verso" in development).
#: Used for exactly one thing: finding an older state directory to migrate in
#: :func:`migrate_legacy_dirs`.
LEGACY_DIR_NAMES: Final[tuple[str, ...]] = ("EPUB Reader", "Verso")
LEGACY_DIR_NAME: Final[str] = LEGACY_DIR_NAMES[-1]
#: Log file names those versions wrote (renamed to LOG_FILE_NAME on migration).
LEGACY_LOG_NAMES: Final[tuple[str, ...]] = ("epub-reader.log", "verso.log")

# --------------------------------------------------------------------------
# value vocabularies
# --------------------------------------------------------------------------

#: Accepted values of the ``ui.language`` setting.  ``auto`` is resolved by
#: ``strings.set_language`` from ``QLocale.system()``.
UI_LANGUAGES: Final[tuple[str, ...]] = ("auto", "zh-Hans", "zh-Hant", "en", "ja")

#: Accepted values of the ``reader.theme`` setting.  theme.py imports these.
THEME_CHOICES: Final[tuple[str, ...]] = ("light", "sepia", "dark", "system")

#: Research-phase names that may still be found in a development settings file.
LEGACY_THEME_ALIASES: Final[dict[str, str]] = {
    "day": "light",
    "paper": "sepia",
    "night": "dark",
    "auto": "system",
    "follow": "system",
}

SETTINGS_DEBOUNCE_MS: Final[int] = 500
POSITION_DEBOUNCE_MS: Final[int] = 2000
LIBRARY_DEBOUNCE_MS: Final[int] = 250
LIBRARY_IDLE_MS: Final[int] = 60_000

_HASH_CHUNK: Final[int] = 1 << 20
_MISSING: Final[object] = object()
_WRITE_RETRY_MS: Final[int] = 250
_WRITE_MAX_ATTEMPTS: Final[int] = 20
_STALE_TMP_SECONDS: Final[int] = 600

_LANGUAGE_ALIASES: Final[dict[str, str]] = {
    "": "auto",
    "auto": "auto",
    "system": "auto",
    "zh": "zh-Hans",
    "zh-hans": "zh-Hans",
    "zh-cn": "zh-Hans",
    "zh-sg": "zh-Hans",
    "zh-my": "zh-Hans",
    "zh-hans-cn": "zh-Hans",
    "zh-hant": "zh-Hant",
    "zh-tw": "zh-Hant",
    "zh-hk": "zh-Hant",
    "zh-mo": "zh-Hant",
    "zh-hant-tw": "zh-Hant",
    "zh-hant-hk": "zh-Hant",
    "en": "en",
    "ja": "ja",
    "jp": "ja",
}


def normalize_language(value: object) -> str:
    """Map any stored ``ui.language`` value onto :data:`UI_LANGUAGES`.

    Development builds stored ``'zh'`` / ``'en'``; that becomes ``'zh-Hans'`` /
    ``'en'``.  Locale-style spellings (``zh_TW``, ``ja-JP``) are accepted.
    Anything unrecognised becomes ``'auto'`` rather than a wrong language.
    """
    if not isinstance(value, str):
        return "auto"
    raw = value.strip()
    if raw in UI_LANGUAGES:
        return raw
    key = raw.lower().replace("_", "-")
    if key in _LANGUAGE_ALIASES:
        return _LANGUAGE_ALIASES[key]
    if key.startswith("zh-hant") or key[:5] in ("zh-tw", "zh-hk", "zh-mo"):
        return "zh-Hant"
    if key.startswith("zh"):
        return "zh-Hans"
    if key.startswith("ja"):
        return "ja"
    if key.startswith("en"):
        return "en"
    return "auto"


def normalize_theme_choice(value: object) -> str:
    """Map any stored ``reader.theme`` value onto :data:`THEME_CHOICES`.

    ``day``/``paper``/``night`` (research names) become ``light``/``sepia``/
    ``dark``; anything unrecognised becomes ``system``.
    """
    if not isinstance(value, str):
        return "system"
    key = value.strip().lower()
    if key in THEME_CHOICES:
        return key
    return LEGACY_THEME_ALIASES.get(key, "system")


# ==========================================================================
# locations
# ==========================================================================

def app_dir() -> str:
    """``%APPDATA%\\Book Reader`` — the state of record, roams with the profile.

    Do NOT use ``QStandardPaths.AppConfigLocation``: on Windows it returns
    ``AppData\\Local`` (verified), which would strand the user's library.
    """
    base = os.environ.get("APPDATA")
    if not base:  # non-Windows / stripped environment: stay predictable
        base = os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, APP_DIR_NAME)


def cache_dir() -> str:
    """``%LOCALAPPDATA%\\Book Reader\\cache`` — regenerable, must not roam."""
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, APP_DIR_NAME, "cache")


def cover_dir(cache_root: str | None = None) -> str:
    """Where cover thumbnails live: ``<cache>\\covers``."""
    return os.path.join(cache_root or cache_dir(), "covers")


def books_dir(root: str | None = None) -> str:
    """Where per-book state lives: ``<root>\\books``."""
    return os.path.join(root or app_dir(), "books")


def log_dir(root: str | None = None) -> str:
    """Where the log lives: ``<root>\\logs``."""
    return os.path.join(root or app_dir(), "logs")


def log_file(root: str | None = None) -> str:
    """``<root>\\logs\\book-reader.log``."""
    return os.path.join(log_dir(root), LOG_FILE_NAME)


def migrate_legacy_dirs(
    appdata: str | None = None, localappdata: str | None = None
) -> list[str]:
    """One-time move of an older version's state into the Book Reader root.

    If ``<appdata>\\Book Reader`` does NOT exist, the newest existing directory
    named in :data:`LEGACY_DIR_NAMES` (``EPUB Reader``, then ``Verso``) is moved
    across (and the same name under ``<localappdata>`` with it).  Once
    the new root exists this function returns immediately without looking at
    the legacy location again.  Idempotent and safe to call on every launch;
    :class:`Store` calls it itself when constructed with the default root.

    If the rename fails because something inside is locked, the tree is copied
    instead and the original is left in place (nothing is ever deleted).

    Returns human-readable notes for the log; an empty list means nothing was
    done.  Never raises.
    """
    notes: list[str] = []
    appdata = appdata if appdata is not None else os.environ.get("APPDATA")
    localappdata = (localappdata if localappdata is not None
                    else os.environ.get("LOCALAPPDATA"))
    if not appdata:
        return notes
    new_root = os.path.join(appdata, APP_DIR_NAME)
    if os.path.lexists(new_root):
        return notes
    legacy = next((n for n in LEGACY_DIR_NAMES if os.path.isdir(os.path.join(appdata, n))), None)
    if legacy is None:
        return notes
    old_root = os.path.join(appdata, legacy)

    # 1. the Local cache first: it is regenerable, so a failure here is harmless
    #    and the state-root rename below stays the single commit point.
    if localappdata:
        old_local = os.path.join(localappdata, legacy)
        new_local = os.path.join(localappdata, APP_DIR_NAME)
        if os.path.isdir(old_local):
            try:
                if not os.path.lexists(new_local):
                    os.replace(old_local, new_local)
                    notes.append(f"moved {old_local} -> {new_local}")
                else:
                    old_cache = os.path.join(old_local, "cache")
                    new_cache = os.path.join(new_local, "cache")
                    if os.path.isdir(old_cache) and not os.path.lexists(new_cache):
                        os.replace(old_cache, new_cache)
                        notes.append(f"moved {old_cache} -> {new_cache}")
            except OSError as exc:
                notes.append(f"cache left in place ({type(exc).__name__}: {exc})")

    # 2. the state of record
    try:
        os.replace(old_root, new_root)
        notes.append(f"moved {old_root} -> {new_root}")
    except OSError as exc:
        staging = new_root + ".migrating"
        try:
            shutil.rmtree(staging, ignore_errors=True)
            shutil.copytree(old_root, staging)
            os.replace(staging, new_root)
            notes.append(
                f"copied {old_root} -> {new_root}; original left in place "
                f"because the move failed ({type(exc).__name__}: {exc})"
            )
        except OSError as exc2:
            shutil.rmtree(staging, ignore_errors=True)
            notes.append(f"migration failed, legacy state untouched "
                         f"({type(exc2).__name__}: {exc2})")
            return notes

    # 3. legacy log names (epub-reader.log, verso.log) -> book-reader.log[.N]
    logs = os.path.join(new_root, "logs")
    with contextlib.suppress(OSError):
        for name in os.listdir(logs):
            for legacy_log in LEGACY_LOG_NAMES:
                if name.lower().startswith(legacy_log):
                    target = os.path.join(logs, LOG_FILE_NAME + name[len(legacy_log):])
                    if not os.path.lexists(target):
                        with contextlib.suppress(OSError):
                            os.replace(os.path.join(logs, name), target)
                    break
    return notes


# ==========================================================================
# small helpers
# ==========================================================================

def now_iso() -> str:
    """Local time with offset, to the second: ``2026-09-16T14:02:11+08:00``."""
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    """A time-sortable id, stdlib only: ``h_01a0a8cd0efd41c2``."""
    return f"{prefix}_{int(time.time() * 1000):011x}{secrets.token_hex(3)}"


def book_id(path: str | os.PathLike[str]) -> str:
    """blake2b-128 hex (32 chars) of the WHOLE file.

    Measured 32.8 ms for 6.36 MB and 73.1 ms for 13.08 MB on this machine, so
    it is cheap enough to compute on every open.  Not the OPF ``dc:identifier``
    (the user's book declares an ISBN, which collides across printings) and not
    the path (which moves).
    """
    h = hashlib.blake2b(digest_size=16)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_HASH_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _unique_sidecar(path: str, kind: str) -> str:
    """``<path>.<kind>-<epoch>`` that does not exist yet."""
    base = f"{path}.{kind}-{int(time.time())}"
    cand, n = base, 1
    while os.path.lexists(cand):
        cand = f"{base}-{n}"
        n += 1
    return cand


# ==========================================================================
# atomic JSON
# ==========================================================================

def _serialize(obj: dict[str, Any], lock: Any) -> str:
    """Stamp and serialize *obj* while holding *lock*.

    On Python 3.14 ``json.dumps(indent=1)`` runs the C encoder, which holds the
    GIL for the whole call, so a caller mutating a live dict without the lock
    cannot interleave (verified: 0 errors under a hostile mutator thread).  If
    an encoder that yields is ever in play, ``RuntimeError: dictionary changed
    size during iteration`` is retried rather than losing the write.
    """
    last: RuntimeError | None = None
    for _ in range(8):
        try:
            with (lock if lock is not None else contextlib.nullcontext()):
                obj["schema"] = SCHEMA
                obj["updated"] = now_iso()
                return json.dumps(obj, ensure_ascii=False, indent=1)
        except RuntimeError as exc:
            last = exc
            time.sleep(0.002)
    assert last is not None
    raise last


def write_json(path: str, obj: dict[str, Any], *, lock: Any = None) -> None:
    """Write *obj* to *path* atomically, keeping the old file as ``.bak``.

    ``lock`` (any context manager) is held only while the object is serialized,
    never during the fsync, so the UI thread is never blocked on the disk.
    """
    text = _serialize(obj, lock)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    if os.path.exists(path):
        try:
            shutil.copy2(path, path + ".bak")
        except OSError:
            pass
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)  # atomic on NTFS, verified
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _migrate_0_to_1(obj: dict[str, Any]) -> dict[str, Any]:
    """Schema 0 = a file with no ``schema`` key (hand-written or pre-versioning).

    Its shape is already the v1 shape; missing keys are filled in by the loader
    (``_merge_defaults`` for settings, ``book_state`` for books), and legacy
    values are normalized by ``_normalize_settings``.
    """
    return dict(obj)


#: ``{from_version: callable(obj) -> obj}``.  A migration must return the object
#: at version ``from_version + 1``; the loader stamps ``schema`` itself.
MIGRATIONS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {
    0: _migrate_0_to_1,
}


def read_json(
    path: str, default_factory: Callable[[], dict[str, Any]]
) -> tuple[dict[str, Any], str]:
    """Read *path* resiliently.  Returns ``(obj, source_tag)``.

    ``source_tag`` is one of ``'primary'``, ``'backup'``, ``'new'``,
    ``'reset:CORRUPT_QUARANTINED'``, ``'<tag>:MIGRATED(a->b)'``,
    ``'<tag>:READ_ONLY_FUTURE_SCHEMA(v)'`` or ``'<tag>:UNMIGRATABLE(v)'``.
    Nothing is ever deleted: an unreadable primary with no usable backup is
    renamed to ``<name>.json.corrupt-<epoch>``.
    """
    for cand, tag in ((path, "primary"), (path + ".bak", "backup")):
        if not os.path.exists(cand):
            continue
        try:
            with open(cand, encoding="utf-8") as f:
                obj = json.load(f)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        v = obj.get("schema", 0)
        if not isinstance(v, int) or isinstance(v, bool):
            continue
        if v > SCHEMA:
            return obj, f"{tag}:READ_ONLY_FUTURE_SCHEMA({v})"
        start = v
        while v < SCHEMA:
            fn = MIGRATIONS.get(v)
            if fn is None:
                return default_factory(), f"{tag}:UNMIGRATABLE({v})"
            obj = fn(obj)
            v += 1
            obj["schema"] = v
        if start != v:
            return obj, f"{tag}:MIGRATED({start}->{v})"
        return obj, tag
    if os.path.exists(path):
        with contextlib.suppress(OSError):
            os.replace(path, _unique_sidecar(path, "corrupt"))
        return default_factory(), "reset:CORRUPT_QUARANTINED"
    return default_factory(), "new"


# ==========================================================================
# defaults — the literal schemas from docs/research/product-spec.md §4
# ==========================================================================

DEFAULT_SETTINGS: Final[dict[str, Any]] = {
    "schema": SCHEMA,
    "app": APP_DIR_NAME,
    "version": "1.2.0",
    "updated": "",
    "ui": {
        # UI_LANGUAGES: 'auto' | 'zh-Hans' | 'zh-Hant' | 'en' | 'ja'  (DECISIONS.md §2)
        "language": "auto",
    },
    "window": {
        "geometry": {"x": -1, "y": -1, "w": 1280, "h": 900},
        "maximized": False,
        "fullscreen": False,
        "zen": False,
        "screen": "",
        "dock_visible": True,
        "dock_width": 280,
        "dock_tab": "toc",
        "settings_panel_visible": False,
        "library_view": "grid",
        "library_sort": "recent",
        "last_route": {"kind": "library", "book_id": None},
    },
    "reader": {
        # THEME_CHOICES: 'light' | 'sepia' | 'dark' | 'system'.  Colours live in theme.py.
        "theme": "system",
        "layout": "paged",
        "font_cjk": "Microsoft YaHei",
        "font_latin": "Georgia",
        "use_book_fonts": False,
        "font_size_px": 21,
        "font_weight": 400,
        "line_height": 1.9,
        "para_spacing_em": 0.6,
        "text_indent_ch": 0,
        "text_align": "left",
        "page_margin_px": 64,
        "max_measure_ch": 40,
        "image_click_zoom": True,
        "invert_images_in_dark": False,
    },
    "behavior": {
        "restore_last_book_on_launch": True,
        "confirm_remove_from_shelf": True,
        "auto_hide_chrome_ms": 3000,
        "reading_speed_units_per_min": 300,
        "reading_speed_learned": True,
        "idle_timeout_s": 120,
        "single_instance": True,
        "watch_folders": [],
    },
    "convert": {
        # 转换为 LaTeX 和 PDF: the choices last made in the convert dialog
        "last_dir": "",
        "paper": "a5",
        "font_size": 11,
        "cover": True,
        "contents": True,
        "compile_pdf": True,
    },
    "shortcut_overrides": {},
}


def default_settings() -> dict[str, Any]:
    """A fresh, independent copy of the shipped defaults."""
    return copy.deepcopy(DEFAULT_SETTINGS)


def default_library() -> dict[str, Any]:
    """A fresh, empty ``library.json`` document."""
    return {"schema": SCHEMA, "updated": "", "books": []}


def default_book_state(bid: str, title: str = "") -> dict[str, Any]:
    """A fresh ``books/<id>.json`` document."""
    return {
        "schema": SCHEMA,
        "book_id": bid,
        "title": title,
        "updated": "",
        "position": None,
        "history": [],
        "bookmarks": [],
        "highlights": [],
        "overrides": {},
        "stats": {
            "seconds_read": 0,
            "sessions": 0,
            "units_read": 0,
            "last_session_at": None,
            "speed_samples": [],
        },
    }


def _merge_defaults(target: dict[str, Any], defaults: dict[str, Any]) -> None:
    """Add any key a newer build introduced, without touching the user's values."""
    for key, value in defaults.items():
        if key not in target:
            target[key] = copy.deepcopy(value)
        elif isinstance(value, dict) and isinstance(target[key], dict):
            _merge_defaults(target[key], value)


def _normalize_settings(obj: dict[str, Any]) -> list[str]:
    """Bring legacy values in a settings document up to date.  Returns changes."""
    changes: list[str] = []
    if obj.get("app") != APP_DIR_NAME:
        changes.append(f"app: {obj.get('app')!r} -> {APP_DIR_NAME!r}")
        obj["app"] = APP_DIR_NAME
    ui = obj.get("ui")
    if isinstance(ui, dict):
        old = ui.get("language", "auto")
        new = normalize_language(old)
        if old != new:
            changes.append(f"ui.language: {old!r} -> {new!r}")
            ui["language"] = new
    reader = obj.get("reader")
    if isinstance(reader, dict) and "theme" in reader:
        old = reader.get("theme")
        new = normalize_theme_choice(old)
        if old != new:
            changes.append(f"reader.theme: {old!r} -> {new!r}")
            reader["theme"] = new
    # Research builds copied colour presets into settings.json.  No build ever
    # read them back and there is no UI to edit them; theme.py is the only
    # source of colours, so a stale copy here would only mislead.
    for dead in ("themes", "highlight_colors"):
        if dead in obj:
            del obj[dead]
            changes.append(f"removed stale '{dead}' block (colours live in theme.py)")
    return changes


# ==========================================================================
# the single serialized writer thread
# ==========================================================================

class _Pending:
    __slots__ = ("obj", "due", "hits", "attempts")

    def __init__(self, obj: dict[str, Any], due: float, attempts: int = 0) -> None:
        self.obj = obj
        self.due = due
        self.hits = 1
        self.attempts = attempts


class _Writer(threading.Thread):
    """One thread, one queue keyed by path.  Coalescing is the whole point."""

    def __init__(self, data_lock: threading.RLock) -> None:
        super().__init__(name="book-reader-store-writer", daemon=True)
        self._lock = data_lock
        self._cv = threading.Condition()
        self._pending: dict[str, _Pending] = {}
        self._inflight: set[str] = set()
        self._stop = False
        # counters, for verification and for diagnostics
        self.requests = 0
        self.writes = 0
        self.write_log: list[str] = []          # path of every completed write
        self.errors: list[tuple[str, str]] = []

    # -- public ------------------------------------------------------------
    def schedule(self, path: str, obj: dict[str, Any], delay_ms: int) -> None:
        due = time.monotonic() + max(0, delay_ms) / 1000.0
        with self._cv:
            if self._stop:
                return
            self.requests += 1
            p = self._pending.get(path)
            if p is None:
                self._pending[path] = _Pending(obj, due)
            else:
                # Bounded-latency debounce: newest payload, EARLIEST deadline.
                # A plain trailing debounce would never write while the user
                # keeps scrolling.
                p.obj = obj
                p.due = min(p.due, due)
                p.hits += 1
                p.attempts = 0
            self._cv.notify_all()

    def flush(self, paths: list[str] | None = None, timeout: float = 15.0) -> bool:
        """Make queued writes hit the disk now.  Blocking.

        ``paths=None`` flushes everything.  Returns False on timeout or when a
        write attempted during the flush failed.
        """
        if not self.is_alive():
            return self._drain_sync(paths)
        deadline = time.monotonic() + timeout
        with self._cv:
            errors_before = len(self.errors)

            def outstanding() -> bool:
                keys = self._pending.keys() if paths is None else paths
                return any(k in self._pending or k in self._inflight for k in keys)

            for key, p in self._pending.items():
                if paths is None or key in paths:
                    p.due = 0.0
            self._cv.notify_all()
            while outstanding():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                # a retry scheduled during the flush must not wait its backoff
                for key, p in self._pending.items():
                    if paths is None or key in paths:
                        p.due = min(p.due, time.monotonic())
                self._cv.wait(min(remaining, 0.05))
            return len(self.errors) == errors_before

    def _drain_sync(self, paths: list[str] | None) -> bool:
        """Writer not running (start_writer=False or already stopped): write inline."""
        with self._cv:
            keys = [k for k in self._pending if paths is None or k in paths]
            batch = [(k, self._pending.pop(k)) for k in keys]
        ok = True
        for path, p in batch:
            try:
                write_json(path, p.obj, lock=self._lock)
                with self._cv:
                    self.writes += 1
                    self.write_log.append(path)
            except Exception as exc:
                ok = False
                with self._cv:
                    self.errors.append((path, f"{type(exc).__name__}: {exc}"))
        return ok

    def stop(self, timeout: float = 15.0) -> None:
        self.flush(None, timeout)
        with self._cv:
            self._stop = True
            self._cv.notify_all()
        if self.is_alive():
            self.join(timeout)

    def pending_paths(self) -> list[str]:
        with self._cv:
            return sorted(self._pending)

    # -- thread body -------------------------------------------------------
    def run(self) -> None:  # pragma: no cover - exercised, not introspected
        while True:
            with self._cv:
                while True:
                    if self._stop and not self._pending:
                        return
                    now = time.monotonic()
                    ready = [k for k, p in self._pending.items() if p.due <= now]
                    if ready:
                        break
                    if self._pending:
                        wait = min(p.due for p in self._pending.values()) - now
                        self._cv.wait(max(0.001, wait))
                    else:
                        self._cv.wait()
                batch = [(k, self._pending.pop(k)) for k in ready]
                self._inflight.update(k for k, _ in batch)

            for path, p in batch:
                try:
                    write_json(path, p.obj, lock=self._lock)
                    with self._cv:
                        self.writes += 1
                        self.write_log.append(path)
                except Exception as exc:  # never kill the writer thread
                    with self._cv:
                        msg = f"{type(exc).__name__}: {exc}"
                        if path in self._pending:
                            pass  # a newer payload is already queued; it supersedes
                        elif p.attempts + 1 < _WRITE_MAX_ATTEMPTS:
                            # transient on Windows: AV scanner / sync client holding
                            # the file.  Retry with the same payload.
                            self._pending[path] = _Pending(
                                p.obj, time.monotonic() + _WRITE_RETRY_MS / 1000.0,
                                p.attempts + 1)
                        else:
                            self.errors.append((path, msg))

            with self._cv:
                self._inflight.difference_update(k for k, _ in batch)
                self._cv.notify_all()


# ==========================================================================
# attribute-style settings
# ==========================================================================

class Settings:
    """``store.settings.reader.font_size_px = 23`` — a live view on the dict.

    Reads come straight from the in-memory settings; writes go through
    :meth:`Store.set`, which schedules the 500 ms debounced save.
    """

    __slots__ = ("_store", "_prefix")

    def __init__(self, store: "Store", prefix: str = "") -> None:
        object.__setattr__(self, "_store", store)
        object.__setattr__(self, "_prefix", prefix)

    def _full(self, name: str) -> str:
        return f"{self._prefix}{name}"

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        value = self._store.get(self._full(name), _MISSING)
        if value is _MISSING:
            raise AttributeError(
                f"settings has no key {self._full(name)!r}"
            )
        if isinstance(value, dict):
            return Settings(self._store, self._full(name) + ".")
        return value

    def __setattr__(self, name: str, value: Any) -> None:
        self._store.set(self._full(name), value)

    def __getitem__(self, dotted: str) -> Any:
        value = self._store.get(self._full(dotted), _MISSING)
        if value is _MISSING:
            raise KeyError(self._full(dotted))
        if isinstance(value, dict):
            return Settings(self._store, self._full(dotted) + ".")
        return value

    def __setitem__(self, dotted: str, value: Any) -> None:
        self._store.set(self._full(dotted), value)

    def __contains__(self, dotted: str) -> bool:
        return self._store.get(self._full(dotted), _MISSING) is not _MISSING

    def get(self, dotted: str, default: Any = None) -> Any:
        """Dotted lookup relative to this subtree; never raises."""
        return self._store.get(self._full(dotted), default)

    def keys(self) -> list[str]:
        """Sorted child keys of this subtree."""
        node = self._store.get(self._prefix.rstrip("."), {}) if self._prefix else \
            self._store.settings_dict()
        return sorted(node) if isinstance(node, dict) else []

    def to_dict(self) -> dict[str, Any]:
        """A deep copy of this subtree, safe to hand anywhere."""
        with self._store.lock:
            node = self._store.get(self._prefix.rstrip("."), {}) if self._prefix else \
                self._store.settings_dict()
            return copy.deepcopy(node) if isinstance(node, dict) else {}

    def __repr__(self) -> str:
        return f"<Settings {self._prefix or '<root>'} {self.keys()}>"


# ==========================================================================
# the store
# ==========================================================================

class Store:
    """Everything Book Reader remembers.  Thread-safe; one writer thread inside.

    ``root`` defaults to ``%APPDATA%\\Book Reader`` (and then a development
    build's legacy directory is migrated first).  ``cache_root`` defaults to
    ``%LOCALAPPDATA%\\Book Reader\\cache`` for the default root, and to
    ``<root>\\cache`` for any explicit root, so a test store never touches the
    real profile.
    """

    def __init__(
        self,
        root: str | None = None,
        *,
        cache_root: str | None = None,
        start_writer: bool = True,
    ) -> None:
        #: Notes from the one-time legacy migration (empty when nothing moved).
        self.migration_notes: list[str] = []
        if root is None:
            self.migration_notes = migrate_legacy_dirs()
            self.root: str = os.path.abspath(app_dir())
            self.cache_root: str = os.path.abspath(cache_root or cache_dir())
        else:
            self.root = os.path.abspath(root)
            self.cache_root = os.path.abspath(cache_root or os.path.join(self.root, "cache"))
        self.books_dir: str = books_dir(self.root)
        self.log_dir: str = log_dir(self.root)
        self.settings_path: str = os.path.join(self.root, "settings.json")
        self.library_path: str = os.path.join(self.root, "library.json")

        os.makedirs(self.books_dir, exist_ok=True)
        os.makedirs(self.log_dir, exist_ok=True)
        _sweep_stale_tmp(self.root)
        _sweep_stale_tmp(self.books_dir)

        self._lock = threading.RLock()
        self._writer = _Writer(self._lock)
        if start_writer:
            self._writer.start()

        #: path -> reason, for files written by a NEWER build.  Writes to those
        #: paths are refused; everything else keeps saving normally.
        self._read_only: dict[str, str] = {}
        #: Human-readable notes about what happened during load, for the log.
        self.load_notes: list[str] = list(self.migration_notes)

        self._settings, tag = self._load(self.settings_path, default_settings,
                                         write_back=False)
        self._note("settings.json", tag)
        _merge_defaults(self._settings, DEFAULT_SETTINGS)
        changes = [] if self.is_read_only(self.settings_path) else \
            _normalize_settings(self._settings)
        self.load_notes.extend(f"settings.json: {c}" for c in changes)
        if (changes or "MIGRATED" in tag) and not self.is_read_only(self.settings_path):
            # One write-back for migration + normalization, so the .bak keeps
            # the untouched original.
            with contextlib.suppress(OSError):
                write_json(self.settings_path, self._settings, lock=self._lock)

        self._library, tag = self._load(self.library_path, default_library)
        self._note("library.json", tag)
        if not isinstance(self._library.get("books"), list):
            self._library["books"] = []

        self._book_cache: dict[str, dict[str, Any]] = {}
        self._hash_cache: dict[tuple[str, int, int], str] = {}
        self._closed = False

        #: Attribute-style access, e.g. ``store.settings.reader.font_size_px``.
        self.settings: Settings = Settings(self)

        ref = weakref.ref(self)
        atexit.register(_atexit_flush, ref)

    # -- load / heal -------------------------------------------------------
    def _load(
        self, path: str, factory: Callable[[], dict[str, Any]], *, write_back: bool = True
    ) -> tuple[dict[str, Any], str]:
        obj, tag = read_json(path, factory)
        if "READ_ONLY_FUTURE_SCHEMA" in tag:
            self._read_only[path] = f"{os.path.basename(path)}: {tag}"
            return obj, tag
        if "UNMIGRATABLE" in tag:
            # We are about to run on defaults and would overwrite the file on
            # the next save.  Keep the user's original under its own name.
            src = path if tag.startswith("primary") else path + ".bak"
            with contextlib.suppress(OSError):
                shutil.copy2(src, _unique_sidecar(path, "unmigratable"))
            return obj, tag
        if tag.startswith("backup") and os.path.exists(path):
            # The primary was unreadable but the backup was good.  Preserve the
            # wreckage under its own name FIRST; write_json would otherwise
            # copy the corrupt primary over the good .bak on the next save.
            with contextlib.suppress(OSError):
                os.replace(path, _unique_sidecar(path, "corrupt"))
            with contextlib.suppress(OSError):
                write_json(path, obj)
            tag += "+healed"
        elif "MIGRATED" in tag and write_back:
            # Write the migrated document back now; write_json copies the
            # pre-migration file to .bak first, so it stays recoverable.
            with contextlib.suppress(OSError):
                write_json(path, obj)
        return obj, tag

    def _note(self, what: str, tag: str) -> None:
        if tag not in ("primary", "new"):
            self.load_notes.append(f"{what}: {tag}")

    # -- read-only (future schema) ------------------------------------------
    @property
    def read_only(self) -> bool:
        """True when ANY loaded file came from a newer build (tell the user to update)."""
        return bool(self._read_only)

    @property
    def read_only_reason(self) -> str:
        """Which files are read-only and why; empty when none."""
        return "; ".join(self._read_only.values())

    def is_read_only(self, path: str) -> bool:
        """True when *path* was written by a newer build and will not be saved."""
        return path in self._read_only

    def is_book_read_only(self, bid: str) -> bool:
        """True when ``books/<bid>.json`` was written by a newer build."""
        return self.book_state_path(bid) in self._read_only

    @property
    def lock(self) -> threading.RLock:
        """Hold this while mutating a dict obtained from the store in place."""
        return self._lock

    def _schedule(self, path: str, obj: dict[str, Any], delay_ms: int) -> bool:
        if path in self._read_only or self._closed:
            return False
        self._writer.schedule(path, obj, delay_ms)
        return True

    # -- dotted get/set ----------------------------------------------------
    def settings_dict(self) -> dict[str, Any]:
        """The raw settings mapping.  Read-only by convention; use set()."""
        return self._settings

    def get(self, dotted: str, default: Any = None) -> Any:
        """``store.get('reader.font_size_px')``.  Never raises."""
        with self._lock:
            node: Any = self._settings
            if not dotted:
                return node
            for part in dotted.split("."):
                if not isinstance(node, dict) or part not in node:
                    return default
                node = node[part]
            return node

    def set(self, dotted: str, value: Any) -> None:
        """Set a dotted settings key and schedule the 500 ms debounced save.

        ``ui.language`` and ``reader.theme`` are normalized on the way in, so a
        caller passing ``'zh'`` or ``'night'`` stores ``'zh-Hans'`` / ``'dark'``.
        """
        if not dotted:
            raise ValueError("store.set() needs a key")
        if dotted == "ui.language":
            value = normalize_language(value)
        elif dotted == "reader.theme":
            value = normalize_theme_choice(value)
        parts = dotted.split(".")
        with self._lock:
            node = self._settings
            for part in parts[:-1]:
                nxt = node.get(part)
                if not isinstance(nxt, dict):
                    nxt = {}
                    node[part] = nxt
                node = nxt
            if parts[-1] in node and node[parts[-1]] == value \
                    and type(node[parts[-1]]) is type(value):
                return  # no change, no write
            node[parts[-1]] = value
        self._schedule_settings()

    def update(self, mapping: dict[str, Any]) -> None:
        """Set several dotted keys; the debounce coalesces them into one write."""
        for k, v in mapping.items():
            self.set(k, v)

    def _schedule_settings(self, delay_ms: int = SETTINGS_DEBOUNCE_MS) -> None:
        self._schedule(self.settings_path, self._settings, delay_ms)

    def reset_settings(self) -> None:
        """Restore shipped defaults, keeping window geometry and UI language."""
        with self._lock:
            keep_window = copy.deepcopy(self._settings.get("window", {}))
            keep_lang = normalize_language(self.get("ui.language", "auto"))
            self._settings.clear()
            self._settings.update(default_settings())
            self._settings["window"] = keep_window
            self._settings["ui"]["language"] = keep_lang
        self._schedule_settings(0)

    # -- library -----------------------------------------------------------
    def library(self) -> list[dict[str, Any]]:
        """Every known book.  The dicts are live; call library_upsert to save."""
        with self._lock:
            return list(self._library.get("books", []))

    def library_get(self, bid: str) -> dict[str, Any] | None:
        """The shelf entry for *bid*, or None."""
        with self._lock:
            for entry in self._library.get("books", []):
                if entry.get("id") == bid:
                    return entry
        return None

    def library_upsert(self, entry: dict[str, Any], *, immediate: bool = False) -> None:
        """Add or merge one shelf entry.  Merges, so a partial dict is fine."""
        bid = entry.get("id")
        if not bid:
            raise ValueError("library entry needs an 'id'")
        with self._lock:
            books = self._library.setdefault("books", [])
            for i, existing in enumerate(books):
                if existing.get("id") == bid:
                    merged = dict(existing)
                    merged.update(entry)
                    books[i] = merged
                    break
            else:
                base = {
                    "id": bid,
                    "hash_algo": "blake2b-128",
                    "path_history": [],
                    "missing": False,
                    "added_at": now_iso(),
                    "opened_at": None,
                    "finished_at": None,
                    "progress": 0.0,
                    "seconds_read": 0,
                    "tags": [],
                }
                base.update(entry)
                books.append(base)
        self._schedule_library(0 if immediate else LIBRARY_DEBOUNCE_MS)
        if immediate:
            self._writer.flush([self.library_path])

    def library_update(self, bid: str, **fields: Any) -> None:
        """Touch a few fields (progress, opened_at, seconds_read) — 60 s debounce."""
        with self._lock:
            entry = None
            for e in self._library.get("books", []):
                if e.get("id") == bid:
                    entry = e
                    break
            if entry is None:
                return
            entry.update(fields)
        self._schedule_library(LIBRARY_IDLE_MS)

    def library_remove(self, bid: str) -> None:
        """Remove a book from the shelf.

        NEVER touches the user's .epub file, and keeps ``books/<id>.json`` so
        re-adding the same file brings its highlights and notes back.
        """
        with self._lock:
            books = self._library.get("books", [])
            self._library["books"] = [b for b in books if b.get("id") != bid]
            self._book_cache.pop(bid, None)
        self._schedule_library(0)

    def _schedule_library(self, delay_ms: int) -> None:
        self._schedule(self.library_path, self._library, delay_ms)

    # -- per-book state ----------------------------------------------------
    def book_state_path(self, bid: str) -> str:
        """``<root>\\books\\<bid>.json``."""
        return os.path.join(self.books_dir, f"{bid}.json")

    def book_state(self, bid: str) -> dict[str, Any]:
        """Position, bookmarks, highlights, overrides and stats for one book.

        Returns the live cached dict.  Mutate it under :attr:`lock` (or use the
        helpers below) and then call :meth:`save_book_state`.
        """
        with self._lock:
            cached = self._book_cache.get(bid)
            if cached is not None:
                return cached
        path = self.book_state_path(bid)
        obj, tag = self._load(path, lambda: default_book_state(bid))
        self._note(f"books/{bid}.json", tag)
        obj.setdefault("book_id", bid)
        for key, empty in (("bookmarks", list), ("highlights", list),
                           ("history", list)):
            if not isinstance(obj.get(key), list):
                obj[key] = empty()
        if not isinstance(obj.get("overrides"), dict):
            obj["overrides"] = {}
        if not isinstance(obj.get("stats"), dict):
            obj["stats"] = default_book_state(bid)["stats"]
        obj.setdefault("position", None)
        with self._lock:
            # another thread may have raced us here; first one wins
            return self._book_cache.setdefault(bid, obj)

    def save_book_state(
        self, bid: str, state: dict[str, Any], *, immediate: bool = False
    ) -> None:
        """Persist per-book state.

        ``immediate=False``: position-style 2000 ms bounded debounce.
        ``immediate=True``: blocks until this book's file is on disk (bookmarks,
        highlights, notes, chapter change, book close).
        """
        with self._lock:
            self._book_cache[bid] = state
        path = self.book_state_path(bid)
        if not self._schedule(path, state, 0 if immediate else POSITION_DEBOUNCE_MS):
            return
        if immediate:
            self._writer.flush([path])

    # ---- convenience wrappers that encode the write policy ----------------
    def save_position(
        self, bid: str, position: dict[str, Any], *, force: bool = False
    ) -> None:
        """Debounced 2000 ms.  Pass ``force=True`` on chapter change, book close,
        window deactivate and quit."""
        state = self.book_state(bid)
        with self._lock:
            state["position"] = position
        self.save_book_state(bid, state, immediate=force)

    def add_bookmark(self, bid: str, bookmark: dict[str, Any]) -> dict[str, Any]:
        """Written to disk before this returns.  Bookmarks are never debounced."""
        bookmark.setdefault("id", new_id("b"))
        bookmark.setdefault("created_at", now_iso())
        state = self.book_state(bid)
        with self._lock:
            state.setdefault("bookmarks", []).insert(0, bookmark)
        self.save_book_state(bid, state, immediate=True)
        return bookmark

    def remove_bookmark(self, bid: str, bookmark_id: str) -> bool:
        """Remove one bookmark (written immediately).  False when not found."""
        state = self.book_state(bid)
        with self._lock:
            marks = state.setdefault("bookmarks", [])
            kept = [b for b in marks if b.get("id") != bookmark_id]
            changed = len(kept) != len(marks)
            state["bookmarks"] = kept
        if changed:
            self.save_book_state(bid, state, immediate=True)
        return changed

    def add_highlight(self, bid: str, highlight: dict[str, Any]) -> dict[str, Any]:
        """Written to disk before this returns.  Highlights are never debounced."""
        highlight.setdefault("id", new_id("h"))
        highlight.setdefault("created_at", now_iso())
        highlight.setdefault("updated_at", highlight["created_at"])
        highlight.setdefault("anchor_state", "exact")
        state = self.book_state(bid)
        with self._lock:
            state.setdefault("highlights", []).append(highlight)
        self.save_book_state(bid, state, immediate=True)
        return highlight

    def update_highlight(self, bid: str, highlight_id: str, **fields: Any) -> bool:
        """Edit a highlight's note/colour/anchor_state (written immediately)."""
        state = self.book_state(bid)
        with self._lock:
            found = False
            for h in state.get("highlights", []):
                if h.get("id") == highlight_id:
                    h.update(fields)
                    h["updated_at"] = now_iso()
                    found = True
                    break
        if found:
            self.save_book_state(bid, state, immediate=True)
        return found

    def remove_highlight(self, bid: str, highlight_id: str) -> bool:
        """Removes a highlight the user asked to remove.

        A highlight whose anchor cannot be resolved is NOT removed by this or
        anything else: callers set ``anchor_state='lost'`` and keep it forever.
        """
        state = self.book_state(bid)
        with self._lock:
            hls = state.setdefault("highlights", [])
            kept = [h for h in hls if h.get("id") != highlight_id]
            changed = len(kept) != len(hls)
            state["highlights"] = kept
        if changed:
            self.save_book_state(bid, state, immediate=True)
        return changed

    # -- merged reader settings -------------------------------------------
    def reader_settings(self, bid: str | None = None) -> dict[str, Any]:
        """Global ``reader`` settings merged with this book's ``overrides`` (a copy)."""
        with self._lock:
            merged = copy.deepcopy(self._settings.get("reader", {}))
        if bid:
            state = self.book_state(bid)
            with self._lock:
                overrides = copy.deepcopy(state.get("overrides") or {})
            merged.update(overrides)
        if "theme" in merged:
            merged["theme"] = normalize_theme_choice(merged["theme"])
        return merged

    def set_override(self, bid: str, key: str, value: Any) -> None:
        """Per-book typography override (the 仅用于本书 checkbox)."""
        state = self.book_state(bid)
        with self._lock:
            state.setdefault("overrides", {})[key] = value
        self.save_book_state(bid, state, immediate=True)

    def clear_overrides(self, bid: str) -> None:
        """Unchecking 仅用于本书: the book snaps back to the global settings."""
        state = self.book_state(bid)
        with self._lock:
            state["overrides"] = {}
        self.save_book_state(bid, state, immediate=True)

    # -- covers & identity -------------------------------------------------
    def cover_path(self, bid: str) -> str:
        """``%LOCALAPPDATA%\\Book Reader\\cache\\covers\\<id>.jpg``.  Directory created."""
        directory = cover_dir(self.cache_root)
        os.makedirs(directory, exist_ok=True)
        return os.path.join(directory, f"{bid}.jpg")

    def book_id_for(self, path: str) -> str:
        """blake2b-128 of the file, cached on ``(path, size, mtime_ns)``.

        800 books x 5 MB rehashed on every library scan would be ~12 s of I/O;
        this makes a rescan free unless a file actually changed.  The cache is
        in memory and is also seeded from library.json entries that carry
        ``path``/``size``/``mtime_ns``, so it survives restarts.
        """
        full = os.path.abspath(path)
        st = os.stat(full)
        key = (os.path.normcase(full), st.st_size, st.st_mtime_ns)
        with self._lock:
            hit = self._hash_cache.get(key)
            if hit:
                return hit
            for entry in self._library.get("books", []):
                if (os.path.normcase(os.path.abspath(entry.get("path") or "")) == key[0]
                        and entry.get("size") == st.st_size
                        and entry.get("mtime_ns") == st.st_mtime_ns
                        and entry.get("id")):
                    self._hash_cache[key] = entry["id"]
                    return entry["id"]
        bid = book_id(full)
        with self._lock:
            self._hash_cache[key] = bid
        return bid

    # -- lifecycle ---------------------------------------------------------
    def flush(self, timeout: float = 15.0) -> bool:
        """Block until every queued write is on disk.  Called on quit.

        Returns True when everything was written, False on timeout or error
        (details in :attr:`stats`).
        """
        return self._writer.flush(None, timeout)

    def close(self) -> None:
        """Flush and stop the writer thread.  Further saves are ignored."""
        if self._closed:
            return
        self._writer.stop()
        self._closed = True

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- introspection, used by the tests and diagnostics -----------------
    @property
    def stats(self) -> dict[str, Any]:
        """Writer counters, pending paths, errors, read-only and load notes."""
        w = self._writer
        return {
            "requests": w.requests,
            "writes": w.writes,
            "pending": w.pending_paths(),
            "errors": list(w.errors),
            "read_only": self.read_only,
            "read_only_reason": self.read_only_reason,
            "load_notes": list(self.load_notes),
        }

    def __repr__(self) -> str:
        return f"<Store {self.root!r} books={len(self._library.get('books', []))}>"


def _sweep_stale_tmp(directory: str) -> None:
    """Remove ``.tmp_*.json`` left by a process killed between mkstemp and replace.

    Only files older than ten minutes: a temp file that young may belong to a
    write in flight in another process.  These files are never user data (the
    primary and .bak are untouched).
    """
    cutoff = time.time() - _STALE_TMP_SECONDS
    with contextlib.suppress(OSError):
        for name in os.listdir(directory):
            if name.startswith(".tmp_") and name.endswith(".json"):
                full = os.path.join(directory, name)
                with contextlib.suppress(OSError):
                    if os.path.getmtime(full) < cutoff:
                        os.unlink(full)


def _atexit_flush(ref: "weakref.ReferenceType[Store]") -> None:
    """Safety net for exits that skip ``Store.close()`` (e.g. ``sys.exit``)."""
    store = ref()
    if store is not None and not store._closed:
        with contextlib.suppress(Exception):
            store.flush(timeout=10.0)
