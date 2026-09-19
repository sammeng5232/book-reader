# -*- coding: utf-8 -*-
"""EPUB Reader — the one door to every user-visible string.

No other module may contain literal user-facing text.  Tables live in the
:mod:`i18n` package (one module per language); this module is the API::

    from strings import S, plural, set_language, language_changed

    S("tb.toc")                           # '目录' / 'Contents'
    S("menu.about")                       # '关于 EPUB Reader'   ({app} is automatic)
    plural("search.count", 2371)          # '共 2371 处' / '2371 matches'
    S("status.left.chapter", time=duration(12))   # '本章剩余 12 分钟'

    set_language("auto")                  # or 'zh-Hans' | 'zh-Hant' | 'en' | 'ja'
    language_changed.subscribe(widget.retranslate_ui)

Languages are switched LIVE: :func:`set_language` notifies every subscriber of
:data:`language_changed`, and each widget re-reads its strings.  The registry
is plain Python — this module never imports Qt.

Carve-outs that stay the same in every language:
    * :data:`APP_DISPLAY_NAME` — the product name, never translated.
    * :data:`LANGUAGE_NAMES` — endonyms, so a reader can always find their own
      language in the selector whatever the current UI language is.
    * :data:`KEYS` — shortcut labels (``Ctrl+F``, ``F11``, arrow glyphs).

Checker: ``python strings.py --check`` (see :func:`check`).
"""

from __future__ import annotations

import datetime as _dt
import inspect
import locale
import logging
import os
import re
import string
import sys
import weakref
from typing import Any, Callable, Final

import i18n as _i18n

__all__ = [
    "APP_DISPLAY_NAME",
    "LANGUAGES",
    "LANGUAGE_NAMES",
    "QT_LOCALE_NAMES",
    "STUB_MARKERS",
    "TABLES",
    "KEYS",
    "CHEATSHEET_LAYOUT",
    "THEME_KEYS",
    "FONT_KEYS",
    "LanguageChangedRegistry",
    "language_changed",
    "S",
    "translate",
    "has",
    "plural",
    "plural_category",
    "duration",
    "format_date",
    "tip",
    "keys_label",
    "cheatsheet",
    "theme_label",
    "font_label",
    "current_language",
    "language_preference",
    "set_language",
    "resolve_auto",
    "system_locale_name",
    "language_choices",
    "set_strict",
    "is_strict",
    "check",
    "stub_counts",
    "main",
]

_log = logging.getLogger("epub_reader.strings")

# ==========================================================================
# identity and languages
# ==========================================================================

#: The product name shown in window titles, About and the cheat sheet.
#: Filesystem/registry identities live in store.py (APP_DIR_NAME, PROG_ID, PIPE_NAME).
APP_DISPLAY_NAME: Final[str] = "EPUB Reader"

#: Shipped UI languages, in selector order.
LANGUAGES: Final[tuple[str, ...]] = ("zh-Hans", "zh-Hant", "en", "ja")

#: Endonyms.  NEVER translated: each language is named in itself.
LANGUAGE_NAMES: Final[dict[str, str]] = {
    "zh-Hans": "简体中文",
    "zh-Hant": "繁體中文",
    "en": "English",
    "ja": "日本語",
}

#: Language code -> Qt locale name, for ``QLocale(name)`` (number/date widgets)
#: and for loading Qt's own translations (``qtbase_<name>.qm``; PySide6 ships
#: qtbase_zh_CN, qtbase_zh_TW and qtbase_ja).  Pure data; no Qt import here.
QT_LOCALE_NAMES: Final[dict[str, str]] = {
    "zh-Hans": "zh_CN",
    "zh-Hant": "zh_TW",
    "en": "en_US",
    "ja": "ja_JP",
}

#: Prefix carried by every untranslated stub value.  The checker counts them.
STUB_MARKERS: Final[dict[str, str]] = {
    "zh-Hans": "[zh-Hans] ",
    "zh-Hant": "[zh-Hant] ",
    "en": "[en] ",
    "ja": "[ja] ",
}

#: Language code -> table.  Treat as read-only.
TABLES: Final[dict[str, dict[str, str]]] = _i18n.TABLES

# ==========================================================================
# shortcut labels — identical in every language
# ==========================================================================

#: Shortcut id -> the key combinations shown for it, in display order.
#: Tooltips show the first one; the cheat sheet shows all of them.
KEYS: Final[dict[str, tuple[str, ...]]] = {
    # reading
    "next_page":        ("Space", "PageDown", "→", "↓"),
    "prev_page":        ("Shift+Space", "PageUp", "←", "↑"),
    "page_jk":          ("j", "k"),
    "next_chapter":     ("Ctrl+→", "Ctrl+PageDown"),
    "prev_chapter":     ("Ctrl+←", "Ctrl+PageUp"),
    "chapter_edge":     ("Home", "End"),
    "book_edge":        ("Ctrl+Home", "Ctrl+End"),
    "jump_history":     ("Alt+←", "Alt+→"),
    "goto":             ("Ctrl+G",),
    "find_next":        ("F3", "n"),
    "find_prev":        ("Shift+F3", "N"),
    # panels
    "toc":              ("Ctrl+T",),
    "bookmarks":        ("Ctrl+B",),
    "notes":            ("Ctrl+E",),
    "search":           ("Ctrl+F", "/"),
    "typography":       ("Ctrl+,",),
    "escape":           ("Esc",),
    "cheatsheet":       ("F1", "Ctrl+/"),
    # actions
    "bookmark_toggle":  ("Ctrl+D",),
    "copy":             ("Ctrl+C",),
    "copy_cite":        ("Ctrl+Shift+C",),
    "highlight":        ("Ctrl+1", "Ctrl+2", "Ctrl+3", "Ctrl+4"),
    "highlight_delete": ("Delete",),
    "toggle_mode":      ("Ctrl+M",),
    "font_step":        ("Ctrl+=", "Ctrl+-"),
    "font_reset":       ("Ctrl+0",),
    "toggle_daynight":  ("Ctrl+Shift+D",),
    # window & app
    "fullscreen":       ("F11",),
    "zen":              ("Ctrl+Shift+F",),
    "open":             ("Ctrl+O",),
    "library":          ("Ctrl+Shift+L",),
    "close_book":       ("Ctrl+W",),
    "quit":             ("Ctrl+Q",),
    # library screen
    "lib_move":         ("↑", "↓", "←", "→"),
    "lib_open":         ("Enter",),
    "lib_focus_search": ("Ctrl+F", "/"),
    "lib_clear":        ("Esc",),
    "lib_remove":       ("Delete",),
    "lib_open_book":    ("Ctrl+O",),
    "lib_rescan":       ("F5",),
    # toolbar tooltips only
    "back":             ("Alt+←",),
    "forward":          ("Alt+→",),
}

#: The F1 cheat sheet: ``((group title key, ((KEYS id, description key), ...)), ...)``.
CHEATSHEET_LAYOUT: Final[tuple[tuple[str, tuple[tuple[str, str], ...]], ...]] = (
    ("keys.group.reading", (
        ("next_page",        "keys.next_page"),
        ("prev_page",        "keys.prev_page"),
        ("page_jk",          "keys.page_jk"),
        ("next_chapter",     "keys.next_chapter"),
        ("prev_chapter",     "keys.prev_chapter"),
        ("chapter_edge",     "keys.chapter_edge"),
        ("book_edge",        "keys.book_edge"),
        ("jump_history",     "keys.jump_history"),
        ("goto",             "keys.goto"),
        ("find_next",        "keys.find_next"),
        ("find_prev",        "keys.find_prev"),
    )),
    ("keys.group.panels", (
        ("toc",              "keys.toc"),
        ("bookmarks",        "keys.bookmarks"),
        ("notes",            "keys.notes"),
        ("search",           "keys.search"),
        ("typography",       "keys.typography"),
        ("escape",           "keys.escape"),
        ("cheatsheet",       "keys.cheatsheet"),
    )),
    ("keys.group.actions", (
        ("bookmark_toggle",  "keys.bookmark_toggle"),
        ("copy",             "keys.copy"),
        ("copy_cite",        "keys.copy_cite"),
        ("highlight",        "keys.highlight"),
        ("highlight_delete", "keys.highlight_delete"),
        ("toggle_mode",      "keys.toggle_mode"),
        ("font_step",        "keys.font_step"),
        ("font_reset",       "keys.font_reset"),
        ("toggle_daynight",  "keys.toggle_daynight"),
    )),
    ("keys.group.window", (
        ("fullscreen",       "keys.fullscreen"),
        ("zen",              "keys.zen"),
        ("open",             "keys.open"),
        ("library",          "keys.library"),
        ("close_book",       "keys.close_book"),
        ("quit",             "keys.quit"),
    )),
    ("keys.group.library", (
        ("lib_move",         "keys.lib_move"),
        ("lib_open",         "keys.lib_open"),
        ("lib_focus_search", "keys.lib_focus_search"),
        ("lib_clear",        "keys.lib_clear"),
        ("lib_remove",       "keys.lib_remove"),
        ("lib_open_book",    "keys.lib_open_book"),
        ("lib_rescan",       "keys.lib_rescan"),
    )),
)

#: Internal theme name -> string key.  Both naming schemes are accepted
#: (light/sepia/dark as in the UI contract, day/paper/night as in the settings
#: schema from the research spec), so the theme owner can use either.
THEME_KEYS: Final[dict[str, str]] = {
    "light": "theme.light",
    "day": "theme.light",
    "sepia": "theme.sepia",
    "paper": "theme.sepia",
    "dark": "theme.dark",
    "night": "theme.dark",
    "system": "theme.system",
    "auto": "theme.system",
}

#: Font family (as QFontDatabase reports it) -> display-name key.  Families not
#: listed here are shown as-is by :func:`font_label`.
FONT_KEYS: Final[dict[str, str]] = {
    "Microsoft YaHei": "font.microsoft_yahei",
    "SimSun": "font.simsun",
    "NSimSun": "font.nsimsun",
    "SimHei": "font.simhei",
    "KaiTi": "font.kaiti",
    "FangSong": "font.fangsong",
    "STKaiti": "font.stkaiti",
    "STFangsong": "font.stfangsong",
    "DengXian": "font.dengxian",
    "Microsoft JhengHei": "font.microsoft_jhenghei",
}

# ==========================================================================
# module state
# ==========================================================================

#: None until first needed; then always one of LANGUAGES.
_current: str | None = None
#: What the user chose: 'auto' or one of LANGUAGES.
_preference: str = "auto"
#: Missing keys / placeholders raise when True.  Loud from source, quiet in a
#: frozen (PyInstaller) build or under ``python -O``.
_strict: bool = __debug__ and not getattr(sys, "frozen", False)


def set_strict(strict: bool) -> None:
    """Choose what a missing key or placeholder does: raise (True) or degrade (False)."""
    global _strict
    _strict = bool(strict)


def is_strict() -> bool:
    """True when missing keys raise instead of degrading."""
    return _strict


# ==========================================================================
# language_changed — a pure-Python subscriber registry
# ==========================================================================

class LanguageChangedRegistry:
    """Callbacks run after the UI language changes.

    * ``subscribe(fn)`` returns *fn*, so it also works as a decorator.
    * *fn* is called as ``fn(lang)`` if it accepts a positional argument, else
      as ``fn()`` — so a Qt-style ``def retranslate_ui(self)`` works unchanged.
    * Bound methods are held **weakly**: subscribing ``widget.retranslate_ui``
      never keeps a widget alive.  Plain functions and lambdas are held strongly.
    * A subscriber whose Qt object was already deleted (PySide raises
      ``RuntimeError: ... already deleted``) is dropped silently.
    * Any other exception is logged, the remaining subscribers still run, and
      the first error is re-raised afterwards when strict mode is on.
    * UI thread only; no locking.
    """

    __slots__ = ("_subs",)

    def __init__(self) -> None:
        # (dereference-callable returning the subscriber or None, takes_arg)
        self._subs: list[tuple[Callable[[], Callable[..., Any] | None], bool]] = []

    @staticmethod
    def _takes_arg(fn: Callable[..., Any]) -> bool:
        try:
            params = inspect.signature(fn).parameters.values()
        except (TypeError, ValueError):
            return True
        kinds = (inspect.Parameter.POSITIONAL_ONLY,
                 inspect.Parameter.POSITIONAL_OR_KEYWORD,
                 inspect.Parameter.VAR_POSITIONAL)
        return any(p.kind in kinds for p in params)

    def _find(self, fn: Callable[..., Any]) -> int:
        for i, (ref, _takes) in enumerate(self._subs):
            target = ref()
            if target is not None and (target is fn or target == fn):
                return i
        return -1

    def subscribe(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        """Register *fn*.  Subscribing the same callable twice is a no-op."""
        if not callable(fn):
            raise TypeError(f"language_changed.subscribe: {fn!r} is not callable")
        if self._find(fn) >= 0:
            return fn
        if inspect.ismethod(fn):
            ref: Callable[[], Callable[..., Any] | None] = weakref.WeakMethod(fn)
        else:
            ref = (lambda f=fn: f)
        self._subs.append((ref, self._takes_arg(fn)))
        return fn

    def unsubscribe(self, fn: Callable[..., Any]) -> bool:
        """Remove *fn*.  Returns False (and does nothing) if it was not subscribed."""
        i = self._find(fn)
        if i < 0:
            return False
        del self._subs[i]
        return True

    def clear(self) -> None:
        """Drop every subscriber (tests use this)."""
        self._subs.clear()

    def __len__(self) -> int:
        self._subs = [s for s in self._subs if s[0]() is not None]
        return len(self._subs)

    def emit(self, lang: str) -> None:
        """Call every live subscriber with *lang*.  :func:`set_language` does this."""
        errors: list[BaseException] = []
        for entry in list(self._subs):
            ref, takes_arg = entry
            target = ref()
            if target is None:
                self._discard(entry)
                continue
            try:
                if takes_arg:
                    target(lang)
                else:
                    target()
            except RuntimeError as exc:
                if "already deleted" in str(exc):
                    self._discard(entry)
                    continue
                _log.exception("language_changed subscriber %r failed", target)
                errors.append(exc)
            except Exception as exc:  # noqa: BLE001 - one bad widget must not stop the rest
                _log.exception("language_changed subscriber %r failed", target)
                errors.append(exc)
        if errors and _strict:
            raise errors[0]

    def _discard(self, entry: tuple[Callable[[], Any], bool]) -> None:
        try:
            self._subs.remove(entry)
        except ValueError:
            pass


#: Subscribe widgets' ``retranslate_ui`` here.
language_changed: Final[LanguageChangedRegistry] = LanguageChangedRegistry()


# ==========================================================================
# choosing the language
# ==========================================================================

def system_locale_name() -> str:
    """The Windows display language as a locale name, e.g. ``'zh-CN'``.

    Order: Win32 ``GetUserDefaultUILanguage`` (the display language itself) ->
    ``QLocale.system().name()`` if PySide6 is *already* loaded (never imported
    from here) -> :func:`locale.getlocale` -> ``LANG``/``LC_ALL``.  Returns ``''``
    when nothing is known.
    """
    if sys.platform == "win32":
        try:
            import ctypes

            k32 = ctypes.windll.kernel32
            langid = int(k32.GetUserDefaultUILanguage())
            buf = ctypes.create_unicode_buffer(85)
            if langid and k32.LCIDToLocaleName(langid, buf, len(buf), 0):
                if buf.value:
                    return buf.value
        except Exception:  # noqa: BLE001 - fall through to the next source
            pass
    qtcore = sys.modules.get("PySide6.QtCore")
    if qtcore is not None:
        try:
            name = qtcore.QLocale.system().name()
            if name and name != "C":
                return str(name)
        except Exception:  # noqa: BLE001
            pass
    try:
        name = locale.getlocale()[0]
        if name:
            return name
    except (ValueError, TypeError):
        pass
    return os.environ.get("LC_ALL") or os.environ.get("LANG") or ""


_ZH_HANT_REGIONS: Final[frozenset[str]] = frozenset({"tw", "hk", "mo"})


def resolve_auto(locale_name: str, /) -> str:
    """Map a locale name to a shipped language.

    ``zh_CN``/``zh_SG``/``zh-Hans-*``/bare ``zh`` -> ``zh-Hans``;
    ``zh_TW``/``zh_HK``/``zh_MO``/``zh-Hant-*`` -> ``zh-Hant``; ``ja*`` -> ``ja``;
    anything else (including ``''``) -> ``en``.  Accepts ``_`` or ``-``, any
    case, POSIX suffixes (``zh_TW.UTF-8``) and Windows long names
    (``Chinese (Traditional)_Taiwan``).
    """
    raw = (locale_name or "").strip()
    if raw in LANGUAGES:
        return raw
    tag = raw.split(".")[0].split("@")[0].replace("_", "-").lower()
    if not tag:
        return "en"
    parts = [p for p in tag.split("-") if p]
    first = parts[0] if parts else ""
    if first in ("zh", "zho", "chi") or tag.startswith("chinese"):
        if "hant" in parts or "traditional" in tag:
            return "zh-Hant"
        if "hans" in parts or "simplified" in tag:
            return "zh-Hans"
        if any(p in _ZH_HANT_REGIONS for p in parts[1:]):
            return "zh-Hant"
        if any(w in tag for w in ("taiwan", "hong kong", "macao", "macau")):
            return "zh-Hant"
        return "zh-Hans"
    if first in ("ja", "jpn") or tag.startswith("japanese"):
        return "ja"
    return "en"


def current_language() -> str:
    """The active language, always one of :data:`LANGUAGES`.

    Before the first :func:`set_language` call this resolves ``'auto'`` once
    (without notifying subscribers).
    """
    global _current
    if _current is None:
        _current = resolve_auto(system_locale_name())
    return _current


def language_preference() -> str:
    """What was last passed to :func:`set_language`, normalised: ``'auto'`` or a code."""
    return _preference


def set_language(lang: str | None) -> None:
    """Switch the UI language and notify :data:`language_changed` if it changed.

    *lang* is ``'auto'`` or one of :data:`LANGUAGES` (the ``ui.language``
    setting).  ``'auto'``/``None``/``''`` resolve from the Windows display
    language.  A legacy or locale-like value (``'zh'``, ``'zh_TW'``, ``'ja-JP'``)
    is mapped through :func:`resolve_auto` rather than rejected, so an old
    settings file never breaks startup.
    """
    global _current, _preference
    pref = lang.strip() if isinstance(lang, str) else ""
    if pref in LANGUAGES:
        resolved = pref
        _preference = pref
    elif pref.lower() in ("", "auto"):
        resolved = resolve_auto(system_locale_name())
        _preference = "auto"
    else:
        resolved = resolve_auto(pref)
        _preference = resolved
    previous = _current
    _current = resolved
    if previous != resolved:
        language_changed.emit(resolved)


def language_choices() -> list[tuple[str, str]]:
    """Rows for the language selector: ``[(value, label), ...]``.

    First row is ``('auto', 'Follow system (<endonym of what auto resolves to>)')``
    in the current UI language; then each language by its endonym.
    """
    detected = resolve_auto(system_locale_name())
    rows = [("auto", S("set.language.auto_resolved", language=LANGUAGE_NAMES[detected]))]
    rows.extend((code, LANGUAGE_NAMES[code]) for code in LANGUAGES)
    return rows


# ==========================================================================
# lookup
# ==========================================================================

class _KeepMissing(dict):
    """format_map helper for non-strict mode: unknown placeholders stay visible."""

    def __missing__(self, name: str) -> str:
        return "{" + name + "}"


def _lookup(lang: str, key: str) -> str | None:
    text = TABLES[lang].get(key)
    if text is not None:
        return text
    if _strict:
        raise KeyError(f"strings: no such key {key!r} (language {lang!r})")
    for fallback in ("en", "zh-Hans"):
        text = TABLES[fallback].get(key)
        if text is not None:
            return text
    return None


def _render(lang: str, key: str, fmt: dict[str, Any]) -> str:
    text = _lookup(lang, key)
    if text is None:
        return key
    if "{" not in text:
        return text
    values: dict[str, Any] = {"app": APP_DISPLAY_NAME}
    values.update(fmt)
    try:
        return text.format_map(values)
    except (KeyError, IndexError, ValueError, AttributeError) as exc:
        if _strict:
            raise KeyError(
                f"strings: key {key!r} ({lang}) could not be formatted: "
                f"{type(exc).__name__}: {exc}; given {sorted(fmt)}"
            ) from None
        try:
            return text.format_map(_KeepMissing(values))
        except Exception:  # noqa: BLE001
            return text


def S(key: str, /, **fmt: Any) -> str:
    """The string for *key* in the current language, with ``{placeholders}`` filled.

    ``{app}`` is always available (:data:`APP_DISPLAY_NAME`).  A missing key or
    placeholder raises :class:`KeyError` in strict mode (running from source);
    in a frozen build a missing key returns the key itself and a missing
    placeholder is left visible, so the app never crashes over copy.
    """
    return _render(current_language(), key, fmt)


def translate(lang: str, key: str, /, **fmt: Any) -> str:
    """Like :func:`S` but in an explicit language (tests, tools, exports)."""
    if lang not in TABLES:
        raise ValueError(f"strings.translate: unknown language {lang!r}")
    return _render(lang, key, fmt)


def has(key: str, /) -> bool:
    """True when *key* exists (all tables share one key set)."""
    return key in TABLES[current_language()]


def plural_category(n: int | float, /, lang: str | None = None) -> str:
    """``'one'`` or ``'other'`` for *n* under *lang*'s plural rule.

    English: ``'one'`` only for the integer 1.  Chinese and Japanese have no
    grammatical plural: always ``'other'``.
    """
    lang = lang or current_language()
    if lang == "en":
        return "one" if (n == 1 and not isinstance(n, float)) else "other"
    return "other"


def plural(key: str, n: int | float, /, **fmt: Any) -> str:
    """Look up ``key.one`` or ``key.other`` for count *n*; ``{n}`` is filled with *n*.

    Pass ``n=`` in *fmt* to display a pre-formatted number
    (``plural("search.count", 2371, n="2,371")``).
    """
    fmt.setdefault("n", n)
    return S(f"{key}.{plural_category(n)}", **fmt)


def duration(minutes: int | float, /, *, long: bool = False) -> str:
    """A reading-time phrase.

    Short (status bar): ``'12 分钟'``, ``'3 小时 20 分钟'``, ``'2 h'``, ``'under 1 min'``.
    Long (dialogs): ``'1 hour 1 minute'``, ``'2 hours'``, ``'less than a minute'``.
    *minutes* is rounded to the nearest whole minute; negatives and junk read
    as less than a minute.
    """
    try:
        total = int(round(float(minutes)))
    except (TypeError, ValueError, OverflowError):
        total = 0
    if total < 1:
        return S("time.lt_minute.long" if long else "time.lt_minute")
    h, m = divmod(total, 60)
    if not long:
        if h == 0:
            return S("time.m", m=m)
        if m == 0:
            return S("time.h", h=h)
        return S("time.hm", h=h, m=m)
    if h == 0:
        return plural("time.minutes", m)
    hours = plural("time.hours", h)
    if m == 0:
        return hours
    return S("time.hours_minutes", hours=hours, minutes=plural("time.minutes", m))


def _to_local_date(value: Any) -> _dt.date | None:
    if value is None or value == "":
        return None
    if isinstance(value, _dt.datetime):
        return (value.astimezone() if value.tzinfo else value).date()
    if isinstance(value, _dt.date):
        return value
    if isinstance(value, str):
        try:
            return _to_local_date(_dt.datetime.fromisoformat(value.strip()))
        except ValueError:
            return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return _dt.datetime.fromtimestamp(value).date()
        except (OverflowError, OSError, ValueError):
            return None
    return None


def format_date(value: Any, /, *, today: _dt.date | None = None,
                relative: bool = True) -> str:
    """A calendar date for lists and dialogs.

    *value* may be a ``date``, a ``datetime`` (aware ones are converted to local
    time), an ISO-8601 string as stored by ``store.py``, or epoch seconds.
    With *relative* (default): 今天 / 昨天, then month+day within the current
    year, then the full date.  ``relative=False`` always gives the full date.
    ``None``/``''`` -> ``''``; an unparseable value is returned as ``str(value)``.
    """
    d = _to_local_date(value)
    if d is None:
        return "" if value is None or value == "" else str(value)
    month = S(f"month.{d.month}")
    if relative:
        today = today or _dt.date.today()
        if d == today:
            return S("date.today")
        if d == today - _dt.timedelta(days=1):
            return S("date.yesterday")
        if d.year == today.year:
            return S("date.month_day", month=month, d=d.day, y=d.year)
    return S("date.full", month=month, d=d.day, y=d.year)


def keys_label(keys_id: str, /, sep: str = " / ") -> str:
    """All shortcuts for *keys_id* joined for display: ``'F3 / n'``."""
    return sep.join(KEYS[keys_id])


def tip(label_key: str, keys_id: str | None = None, /) -> str:
    """Tooltip: the label plus its first shortcut, e.g. ``'目录 (Ctrl+T)'``.

    With no *keys_id* (or an unknown one) it is just the label.
    """
    label = S(label_key)
    combo = KEYS.get(keys_id or "")
    if not combo:
        return label
    return S("tip.fmt", label=label, keys=combo[0])


def cheatsheet() -> list[tuple[str, list[tuple[tuple[str, ...], str]]]]:
    """The F1 overlay, ready to render: ``[(group title, [(key combos, description)])]``."""
    return [
        (S(group), [(KEYS[kid], S(desc)) for kid, desc in rows])
        for group, rows in CHEATSHEET_LAYOUT
    ]


def theme_label(name: str, /) -> str:
    """Display name of a theme (``'light'``/``'day'`` -> ``'日'``)."""
    key = THEME_KEYS.get(name)
    if key is None:
        if _strict:
            raise KeyError(f"strings.theme_label: unknown theme {name!r}")
        return name
    return S(key)


def font_label(family: str, /) -> str:
    """Display name of a font family (``'Microsoft YaHei'`` -> ``'微软雅黑'``); unknown families pass through."""
    key = FONT_KEYS.get(family)
    return S(key) if key else family


# ==========================================================================
# the checker
# ==========================================================================

_PLACEHOLDER_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_RETIRED_NAME = "ver" + "so"   # spelled apart so this file never contains it
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")
_MOJIBAKE = re.compile("�|Ã.|â€|锟斤拷|ï»¿")
_CJK_ASCII_PUNCT = re.compile("[぀-ヿ㐀-䶿一-鿿][,;:?]")
_CJK_TABLES = ("zh-Hans", "zh-Hant", "ja")

#: Glyphs that exist only in Traditional Chinese; zh-Hans must not use them.
_TRAD_ONLY = set(
    "書設刪們這為時後關開頁錄籤覽顯選擇單鍵複導讀進過應該無與發現夾縮視圖項類標題語認確敗錯誤"
    "損壞護檢測數據級鏈轉動態華體號間邊緣對齊兩從條處個張節餘鐘閱歷記筆黃綠藍線畫幫盤統計權網絡"
    "壓專滾檔螢捲櫃顏資訊預啟儲將請產見頭"
)
#: Glyphs that exist only in Simplified Chinese; zh-Hant must not use them.
_SIMP_ONLY = set(
    "书设删们这为时关开页录签览显选择单键复导读进过应该无与发现夹缩视图项类标题语认确败错误"
    "损坏护检测数据级链转动态华体号间边缘对齐两从条处个张节钟阅历记笔黄绿蓝线画帮盘统计权网络"
    "压专滚档萤柜颜资讯预启储将请产见头"
)
#: Simplified-only glyphs that are also not Japanese; ja must not use them.
#: (与 数 据 条 体 号 黄 画 将 are ordinary Japanese shinjitai and stay allowed.)
_SIMP_NOT_JA = _SIMP_ONLY - set("与数据条体号黄画将")
#: Mainland terms a Taiwan-convention zh-Hant table must not use (term -> preferred).
_ZH_HANT_AVOID: Final[dict[str, str]] = {
    "文件": "檔案", "設置": "設定", "搜索": "搜尋", "字體": "字型", "默認": "預設",
    "信息": "資訊", "屏幕": "螢幕", "全屏": "全螢幕", "視頻": "影片", "鼠標": "滑鼠",
    "軟件": "軟體", "界面": "介面", "菜單": "選單", "導出": "匯出", "導入": "匯入",
    "撤銷": "復原", "書簽": "書籤", "滾動": "捲動", "保存": "儲存", "退出": "結束",
    "網絡": "網路", "程序": "程式", "文本": "文字", "字符": "字元", "高亮": "畫線",
    "打開": "開啟", "激活": "啟用", "登錄": "登入", "默认": "預設",
}
#: Chinese-style terms a Japanese table must not use (term -> preferred).
_JA_AVOID: Final[dict[str, str]] = {
    "書簽": "しおり", "書籤": "しおり", "搜索": "検索", "全屏": "全画面表示", "目錄": "目次",
    "設置": "設定", "退出": "終了", "文件": "ファイル", "打開": "開く", "書架": "本棚",
    "高亮": "ハイライト", "字體": "フォント",
}
_CUTE_PARTICLES = ("哦", "呀", "啦", "喔", "嘛")


def _placeholders(text: str) -> set[str]:
    """Placeholder names in *text*; ValueError for anything but ``{simple_name}``."""
    names: set[str] = set()
    for _literal, field, spec, conversion in string.Formatter().parse(text):
        if field is None:
            continue
        if not _PLACEHOLDER_NAME.match(field) or spec or conversion:
            raise ValueError(f"only {{name}} placeholders are allowed, found {{{field}}}")
        names.add(field)
    return names


def stub_counts(tables: dict[str, dict[str, str]] | None = None) -> dict[str, int]:
    """How many values per language still carry the untranslated stub marker."""
    tables = TABLES if tables is None else tables
    return {
        lang: sum(1 for v in table.values()
                  if isinstance(v, str) and v.startswith(STUB_MARKERS.get(lang, "\0")))
        for lang, table in tables.items()
    }


def _source_files() -> list[str]:
    here = os.path.dirname(os.path.abspath(__file__))
    files = [os.path.abspath(__file__), os.path.join(here, "i18n", "__init__.py")]
    files.extend(_i18n.MODULE_FILES.values())
    for doc in ("api-strings.md", "i18n-glossary.md"):
        path = os.path.join(here, "docs", doc)
        if os.path.exists(path):
            files.append(path)
    return files


def check(tables: dict[str, dict[str, str]] | None = None, *,
          strict_translations: bool = False, scan_files: bool = True) -> list[str]:
    """Return every problem found; an empty list means the tables are shippable.

    Asserts: the four languages are present; identical key sets; identical
    ``{placeholder}`` sets per key, and only ``{simple_name}`` placeholders;
    no empty values; no stray leading/trailing whitespace (``*.sep`` and ``set.suffix.*``
    excepted: they carry their own spacing);
    no retired product name anywhere (tables and this package's source/docs);
    no hard-coded product name in copy (use ``{app}``); no exclamation marks;
    no emoji; no mojibake; ``…`` instead of ``...``; complete ``.one``/``.other``
    pairs; every key the helpers and cheat sheet use exists; Chinese/Japanese
    tables use full-width punctuation next to CJK text, the right script
    (no Traditional glyphs in zh-Hans, no Simplified ones in zh-Hant or ja) and
    the glossary's regional terms.  With *strict_translations*, any remaining
    stub marker is a problem too.
    """
    tables = TABLES if tables is None else tables
    problems: list[str] = []

    if set(tables) != set(LANGUAGES):
        problems.append(f"tables: expected {sorted(LANGUAGES)}, found {sorted(tables)}")

    all_keys: set[str] = set().union(*(set(t) for t in tables.values())) if tables else set()
    for lang, table in tables.items():
        for key in sorted(all_keys - set(table)):
            problems.append(f"{lang}: missing key {key!r}")

    ref_lang = "en" if "en" in tables else next(iter(tables), None)
    for key in sorted(all_keys):
        seen: dict[str, set[str]] = {}
        for lang, table in tables.items():
            value = table.get(key)
            if not isinstance(value, str):
                if key in table:
                    problems.append(f"{lang}: {key!r} is not a string")
                continue
            try:
                seen[lang] = _placeholders(value)
            except ValueError as exc:
                problems.append(f"{lang}: {key!r} {exc}")
        if ref_lang in seen:
            for lang, names in seen.items():
                if names != seen[ref_lang]:
                    problems.append(
                        f"{lang}: {key!r} placeholders {sorted(names)} != "
                        f"{ref_lang} {sorted(seen[ref_lang])}"
                    )

    for lang, table in tables.items():
        marker = STUB_MARKERS.get(lang, "\0")
        for key, value in table.items():
            if _RETIRED_NAME in key.lower():
                problems.append(f"{lang}: key {key!r} contains the retired product name")
            if not isinstance(value, str):
                continue
            where = f"{lang}: {key!r}"
            if not value.strip():
                problems.append(f"{where} is empty")
                continue
            if (value != value.strip() and not key.endswith(".sep")
                    and not key.startswith("set.suffix.") and not value.startswith(marker)):
                problems.append(f"{where} has leading/trailing whitespace")
            if _RETIRED_NAME in value.lower():
                problems.append(f"{where} contains the retired product name")
            if APP_DISPLAY_NAME in value:
                problems.append(f"{where} hard-codes the product name; use {{app}}")
            if any(ch in value for ch in "!！¡"):
                problems.append(f"{where} contains an exclamation mark")
            if _EMOJI.search(value):
                problems.append(f"{where} contains emoji")
            if _MOJIBAKE.search(value):
                problems.append(f"{where} looks like mojibake: {value!r}")
            if "..." in value:
                problems.append(f"{where} uses '...'; use the ellipsis character '…'")
            if strict_translations and value.startswith(marker):
                problems.append(f"{where} is still an untranslated stub")
            if lang in _CJK_TABLES:
                if _CJK_ASCII_PUNCT.search(value):
                    problems.append(f"{where} has half-width punctuation after CJK text")
            if lang in ("zh-Hans", "zh-Hant"):
                if "请稍候" in value or "請稍候" in value:
                    problems.append(f"{where} uses 请稍候 (just do the work instead)")
                for particle in _CUTE_PARTICLES:
                    if particle in value:
                        problems.append(f"{where} uses the particle {particle}")
            if lang == "zh-Hans":
                bad = sorted(set(value) & _TRAD_ONLY)
                if bad:
                    problems.append(f"{where} uses Traditional glyphs {''.join(bad)}")
            elif lang == "zh-Hant":
                bad = sorted(set(value) & _SIMP_ONLY)
                if bad:
                    problems.append(f"{where} uses Simplified glyphs {''.join(bad)}")
                for term, better in _ZH_HANT_AVOID.items():
                    if term in value:
                        problems.append(f"{where} uses mainland term {term}; Taiwan UI uses {better}")
            elif lang == "ja":
                bad = sorted(set(value) & _SIMP_NOT_JA)
                if bad:
                    problems.append(f"{where} uses Simplified Chinese glyphs {''.join(bad)}")
                for term, better in _JA_AVOID.items():
                    if term in value:
                        problems.append(f"{where} uses Chinese-style term {term}; Japanese UI uses {better}")

    for key in sorted(all_keys):
        for have, want in ((".one", ".other"), (".other", ".one")):
            if key.endswith(have) and key[: -len(have)] + want not in all_keys:
                problems.append(f"plural: {key!r} has no {want!r} partner")

    required: set[str] = set()
    for group, rows in CHEATSHEET_LAYOUT:
        required.add(group)
        for kid, desc in rows:
            required.add(desc)
            if kid not in KEYS:
                problems.append(f"cheatsheet: no KEYS entry {kid!r}")
    required.update(THEME_KEYS.values())
    required.update(FONT_KEYS.values())
    required.update(f"month.{m}" for m in range(1, 13))
    required.update({
        "tip.fmt", "set.language.auto_resolved", "date.today", "date.yesterday",
        "date.month_day", "date.full", "time.lt_minute", "time.m", "time.h", "time.hm",
        "time.lt_minute.long", "time.hours_minutes",
        "time.minutes.one", "time.minutes.other", "time.hours.one", "time.hours.other",
    })
    for key in sorted(required - all_keys):
        problems.append(f"helpers: required key {key!r} is missing")

    if scan_files:
        for path in _source_files():
            try:
                with open(path, encoding="utf-8", errors="strict") as fh:
                    text = fh.read()
            except UnicodeDecodeError as exc:
                problems.append(f"file {path}: not valid UTF-8 ({exc})")
                continue
            except OSError as exc:
                problems.append(f"file {path}: unreadable ({exc})")
                continue
            if _RETIRED_NAME in text.lower():
                problems.append(f"file {path}: contains the retired product name")

    return problems


def main(argv: list[str] | None = None) -> int:
    """``python strings.py --check [--strict-translations]`` / ``--show KEY``."""
    import argparse

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(prog="strings.py", description="EPUB Reader UI string tables.")
    parser.add_argument("--check", action="store_true", help="validate all four tables")
    parser.add_argument("--strict-translations", action="store_true",
                        help="with --check: untranslated stub values are errors")
    parser.add_argument("--show", metavar="KEY", help="print KEY in every language")
    args = parser.parse_args(argv)

    if args.show:
        for lang in LANGUAGES:
            print(f"{lang:8} {TABLES[lang].get(args.show, '<missing>')!r}")
        return 0 if all(args.show in TABLES[l] for l in LANGUAGES) else 1
    if not args.check:
        parser.print_help()
        return 0

    problems = check(strict_translations=args.strict_translations)
    print("languages:", ", ".join(f"{l} ({LANGUAGE_NAMES[l]}) {len(TABLES[l])} keys" for l in LANGUAGES))
    stubs = stub_counts()
    pending = {l: n for l, n in stubs.items() if n}
    if pending:
        print("untranslated stub values:", ", ".join(f"{l} {n}" for l, n in pending.items()))
    for problem in problems:
        print("PROBLEM:", problem)
    if problems:
        print(f"FAILED: {len(problems)} problem(s)")
        return 1
    print("OK: key parity, placeholders, tone and naming rules all hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
