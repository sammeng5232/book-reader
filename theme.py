# -*- coding: utf-8 -*-
"""EPUB Reader — themes.  日 light / 纸 sepia / 夜 dark, plus follow-system.

One set of colour tokens drives BOTH surfaces, so the Qt chrome and the book
page are the same colour:

* the Qt side:   :func:`build_palette` (QPalette) + :func:`build_qss` (stylesheet),
  or both at once with :func:`apply_to_application`;
* the page side: :func:`css_variables` / :func:`css_text` (the ``--er-*`` custom
  properties ``assets/reader.css`` consumes) and :func:`page_colors` (the
  ``settings.colors`` object ``assets/reader.js`` accepts).

Theme choices are the strings in :data:`store.THEME_CHOICES`:
``'light' | 'sepia' | 'dark' | 'system'``.  ``'system'`` follows the Windows
app colour mode (Settings > Personalization > Colors > "Choose your app mode").
Display names are string keys (``theme.light`` ...), never literals.

System dark-mode detection, verified on this machine (Windows 11 26200, Qt
6.11.1): the registry value ``HKCU\\...\\Themes\\Personalize\\AppsUseLightTheme``
and ``QGuiApplication.styleHints().colorScheme()`` both report the real mode.
The registry is used first because ``colorScheme()`` returns the APP's override
as soon as anything calls ``QStyleHints.setColorScheme()`` (verified: override
Light on a dark system -> ``colorScheme()`` said Light).  Live changes come
from three sources, each only triggering a registry re-read: Qt's
``colorSchemeChanged``, a native ``WM_SETTINGCHANGE("ImmersiveColorSet")``
filter (works even when the scheme is overridden), and application
re-activation.

Usage::

    controller = ThemeController(app)            # follows store's choice
    controller.set_choice(store.get("reader.theme"))
    controller.themeChanged.connect(reader_page.apply_theme)
    host.set_theme_css(theme.css_text(controller.theme))
"""

from __future__ import annotations

import ctypes
import sys
from dataclasses import asdict, dataclass
from typing import Any, Final

from PySide6.QtCore import (
    QAbstractNativeEventFilter, QCoreApplication, QEvent, QObject, QPointF, QRectF, Qt, Signal,
)
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPainterPath, QPalette, QPen
from PySide6.QtWidgets import QProxyStyle, QStyle, QStyleFactory, QStyleOption, QWidget

from store import LEGACY_THEME_ALIASES, THEME_CHOICES, normalize_theme_choice

__all__ = [
    # vocabulary
    "THEME_CHOICES",
    "THEME_NAMES",
    "THEME_NAME_KEYS",
    "HIGHLIGHT_COLORS",
    "HIGHLIGHT_NAME_KEYS",
    "CSS_TOKEN_NAMES",
    "CSS_FIND_TOKEN_NAMES",
    # the themes
    "Theme",
    "LIGHT",
    "SEPIA",
    "DARK",
    "THEMES",
    "get_theme",
    "resolve_theme",
    "name_key",
    "display_name",
    # page side
    "css_variables",
    "css_text",
    "page_colors",
    "highlight_colors_payload",
    # chrome side
    "build_palette",
    "build_qss",
    "ChromeStyle",
    "apply_to_application",
    # contrast
    "relative_luminance",
    "contrast_ratio",
    "contrast_report",
    # system dark mode
    "registry_apps_use_light_theme",
    "qt_color_scheme_is_dark",
    "detect_system_dark",
    "system_is_dark",
    "SystemThemeWatcher",
    "ThemeController",
]

#: The three concrete themes, in UI order.
THEME_NAMES: Final[tuple[str, ...]] = ("light", "sepia", "dark")

#: String keys for the settings panel's segmented control (owner I's tables).
THEME_NAME_KEYS: Final[dict[str, str]] = {
    "light": "theme.light",
    "sepia": "theme.sepia",
    "dark": "theme.dark",
    "system": "theme.system",
}

#: Highlight colour ids, in shortcut order (Ctrl+1..4).
HIGHLIGHT_COLORS: Final[tuple[str, ...]] = ("yellow", "green", "blue", "pink")

#: String keys for highlight colour names.
HIGHLIGHT_NAME_KEYS: Final[dict[str, str]] = {c: f"color.{c}" for c in HIGHLIGHT_COLORS}

#: The custom properties reader.css documents as theme tokens (its header contract).
CSS_TOKEN_NAMES: Final[tuple[str, ...]] = (
    "--er-bg", "--er-fg", "--er-secondary", "--er-accent", "--er-border",
    "--er-selection", "--er-hl-yellow", "--er-hl-green", "--er-hl-blue", "--er-hl-pink",
)

#: Search-match colours reader.css reads with fallbacks (``var(--er-find-bg, #FFD54A)``).
CSS_FIND_TOKEN_NAMES: Final[tuple[str, ...]] = (
    "--er-find-bg", "--er-find-fg", "--er-find-active-bg", "--er-find-active-fg",
)


# ==========================================================================
# tokens
# ==========================================================================

@dataclass(frozen=True)
class Theme:
    """One concrete theme.  Every colour is ``#RRGGBB``.

    Page tokens (the book surface, mirrored into reader.css):
      ``bg`` ``fg`` ``secondary`` ``accent`` ``border`` ``selection``
    Chrome tokens (toolbar, dock, panels, library):
      ``chrome_bg`` ``chrome_fg`` ``chrome_secondary`` ``chrome_border``
      ``chrome_hover`` ``chrome_pressed`` ``input_bg`` ``on_accent``
    Highlights, x4: ``hl_<colour>`` is the fill painted behind text,
    ``ink_<colour>`` is the strong version for underline style, the 3px bar in
    the 批注 panel and colour swatches.
    Search: ``find_bg`` ``find_fg`` ``find_active_bg`` ``find_active_fg``.
    """

    name: str
    scheme: str                 # 'light' | 'dark' — for color-scheme and Qt's colour scheme

    bg: str
    fg: str
    secondary: str
    accent: str
    border: str
    selection: str

    chrome_bg: str
    chrome_fg: str
    chrome_secondary: str
    chrome_border: str
    chrome_hover: str
    chrome_pressed: str
    input_bg: str
    on_accent: str

    hl_yellow: str
    hl_green: str
    hl_blue: str
    hl_pink: str
    ink_yellow: str
    ink_green: str
    ink_blue: str
    ink_pink: str

    find_bg: str
    find_fg: str
    find_active_bg: str
    find_active_fg: str

    @property
    def is_dark(self) -> bool:
        """True for the dark theme."""
        return self.scheme == "dark"

    @property
    def name_key(self) -> str:
        """String key for this theme's display name, e.g. ``'theme.sepia'``."""
        return THEME_NAME_KEYS[self.name]

    def tokens(self) -> dict[str, str]:
        """Every token as a plain dict (name and scheme included)."""
        return asdict(self)

    def highlight(self, color: str) -> str:
        """Fill colour for highlight id *color*; unknown ids fall back to yellow."""
        return getattr(self, f"hl_{color}", self.hl_yellow)

    def ink(self, color: str) -> str:
        """Strong colour for highlight id *color*; unknown ids fall back to yellow."""
        return getattr(self, f"ink_{color}", self.ink_yellow)

    def qcolor(self, token: str) -> QColor:
        """``QColor`` for one token name, e.g. ``theme.qcolor('bg')``."""
        return QColor(getattr(self, token))


# Values start from the product-spec presets (settings.json "themes" block) and
# were adjusted only where a measured WCAG ratio fell short: every secondary
# text colour now clears 4.5:1 on its own background (sepia's #7A7268 measured
# 4.17:1), every body text clears 7:1, and text on every highlight fill clears
# 4.5:1.  tests/test_theme.py asserts all of these.

LIGHT: Final[Theme] = Theme(
    name="light", scheme="light",
    bg="#FFFFFF", fg="#1A1A1A", secondary="#636363", accent="#1155CC",
    border="#E0E0E0", selection="#B4D5FE",
    chrome_bg="#F3F3F3", chrome_fg="#1A1A1A", chrome_secondary="#5E5E5E",
    chrome_border="#DCDCDC", chrome_hover="#E7E7E7", chrome_pressed="#DADADA",
    input_bg="#FFFFFF", on_accent="#FFFFFF",
    hl_yellow="#FFF08A", hl_green="#BDEBBD", hl_blue="#B9DCF7", hl_pink="#F8C6D8",
    ink_yellow="#C99A00", ink_green="#3E9A48", ink_blue="#2F78B8", ink_pink="#C2527C",
    find_bg="#FFD54A", find_fg="#111111", find_active_bg="#FF7A1A", find_active_fg="#111111",
)

SEPIA: Final[Theme] = Theme(
    name="sepia", scheme="light",
    bg="#F6F0E4", fg="#33302B", secondary="#665E54", accent="#85561F",
    border="#DFD5C0", selection="#E3D3AE",
    chrome_bg="#EFE8DA", chrome_fg="#33302B", chrome_secondary="#625A50",
    chrome_border="#DCD1BB", chrome_hover="#E6DDCB", chrome_pressed="#DDD2BC",
    input_bg="#FAF6EE", on_accent="#FFFFFF",
    hl_yellow="#EFDF9A", hl_green="#C7DEBB", hl_blue="#C2D6E2", hl_pink="#E8C3C9",
    ink_yellow="#A88410", ink_green="#5A8A4A", ink_blue="#4A7A9C", ink_pink="#A95D73",
    find_bg="#F2C94C", find_fg="#1E1B16", find_active_bg="#E07A2E", find_active_fg="#1E1B16",
)

DARK: Final[Theme] = Theme(
    name="dark", scheme="dark",
    bg="#16181C", fg="#C9CCD1", secondary="#8A9099", accent="#7FB4F5",
    border="#2A2E36", selection="#2E4763",
    chrome_bg="#101216", chrome_fg="#C9CCD1", chrome_secondary="#8A9099",
    chrome_border="#262A31", chrome_hover="#1D2026", chrome_pressed="#262A31",
    input_bg="#1B1E23", on_accent="#0E1014",
    hl_yellow="#5C4E14", hl_green="#2A4A2E", hl_blue="#233F55", hl_pink="#522838",
    ink_yellow="#D4AE3A", ink_green="#62B06B", ink_blue="#5E9FDB", ink_pink="#D6789A",
    find_bg="#7A6210", find_fg="#F2E9C8", find_active_bg="#A8540A", find_active_fg="#FFFFFF",
)

#: Concrete themes by name.
THEMES: Final[dict[str, Theme]] = {t.name: t for t in (LIGHT, SEPIA, DARK)}


def get_theme(name: str) -> Theme:
    """A concrete theme by name (``light``/``sepia``/``dark``, legacy aliases accepted).

    ``'system'`` and unknown names raise ``KeyError``; use :func:`resolve_theme`
    for a user's choice.
    """
    key = LEGACY_THEME_ALIASES.get(name, name) if isinstance(name, str) else name
    if key not in THEMES:
        raise KeyError(f"not a concrete theme: {name!r}")
    return THEMES[key]


def resolve_theme(choice: str | None, *, system_dark: bool | None = None) -> Theme:
    """Turn a settings value into a concrete :class:`Theme`.

    ``'system'`` (and anything unrecognised) follows Windows: dark when the app
    mode is dark, otherwise light — never sepia.  Pass *system_dark* to avoid
    re-detecting (e.g. from :class:`SystemThemeWatcher`).
    """
    normalized = normalize_theme_choice(choice)
    if normalized == "system":
        dark = system_is_dark() if system_dark is None else system_dark
        return DARK if dark else LIGHT
    return THEMES[normalized]


def name_key(choice: str) -> str:
    """String key for a theme choice's display name (``'system'`` included)."""
    return THEME_NAME_KEYS[normalize_theme_choice(choice)]


def display_name(choice: str) -> str:
    """Localized display name for a theme choice, via ``strings.S``."""
    from strings import S  # lazy: keep theme importable without the string tables

    return S(name_key(choice))


# ==========================================================================
# page side — reader.css / reader.js
# ==========================================================================

def css_variables(theme: Theme) -> dict[str, str]:
    """The ``--er-*`` custom properties for the page.

    :data:`CSS_TOKEN_NAMES` (the ten tokens reader.css documents) followed by
    :data:`CSS_FIND_TOKEN_NAMES` (search-match colours reader.css reads with
    fallbacks), in that order.
    """
    return {
        "--er-bg": theme.bg,
        "--er-fg": theme.fg,
        "--er-secondary": theme.secondary,
        "--er-accent": theme.accent,
        "--er-border": theme.border,
        "--er-selection": theme.selection,
        "--er-hl-yellow": theme.hl_yellow,
        "--er-hl-green": theme.hl_green,
        "--er-hl-blue": theme.hl_blue,
        "--er-hl-pink": theme.hl_pink,
        "--er-find-bg": theme.find_bg,
        "--er-find-fg": theme.find_fg,
        "--er-find-active-bg": theme.find_active_bg,
        "--er-find-active-fg": theme.find_active_fg,
    }


def css_text(theme: Theme) -> str:
    """An unlayered ``:root{...}`` stylesheet carrying :func:`css_variables`.

    This is what ``webhost.BookHost.set_theme_css()`` expects.
    """
    body = ";".join(f"{k}:{v}" for k, v in css_variables(theme).items())
    return f":root{{{body}}}"


def highlight_colors_payload() -> dict[str, dict[str, str]]:
    """Highlight fills for every theme, keyed ``{colour: {theme: '#hex'}}``.

    Keyed by both the concrete names (light/sepia/dark) and the research names
    (day/paper/night) so any reader.js lookup convention finds its value.
    """
    reverse = {v: k for k, v in LEGACY_THEME_ALIASES.items() if v in THEMES}
    out: dict[str, dict[str, str]] = {}
    for color in HIGHLIGHT_COLORS:
        entry: dict[str, str] = {}
        for t in THEMES.values():
            entry[t.name] = t.highlight(color)
            if t.name in reverse:
                entry[reverse[t.name]] = t.highlight(color)
        out[color] = entry
    return out


def page_colors(theme: Theme) -> dict[str, Any]:
    """The colour part of the settings object handed to ``epubReader.applySettings``.

    ``{'theme': name, 'colors': {...}, 'highlight_colors': {...}}`` — merge it
    into ``store.reader_settings(book_id)`` before sending.  ``colors`` carries
    both ``sel`` (reader.js's key) and ``selection`` (reader.css's token name).
    """
    return {
        "theme": theme.name,
        "colors": {
            "bg": theme.bg,
            "fg": theme.fg,
            "secondary": theme.secondary,
            "accent": theme.accent,
            "link": theme.accent,
            "border": theme.border,
            "sel": theme.selection,
            "selection": theme.selection,
            "scheme": theme.scheme,
            "hl_yellow": theme.hl_yellow,
            "hl_green": theme.hl_green,
            "hl_blue": theme.hl_blue,
            "hl_pink": theme.hl_pink,
            "find_bg": theme.find_bg,
            "find_fg": theme.find_fg,
            "find_active_bg": theme.find_active_bg,
            "find_active_fg": theme.find_active_fg,
        },
        "highlight_colors": highlight_colors_payload(),
    }


# ==========================================================================
# chrome side — QPalette + QSS
# ==========================================================================

def _mix(a: str, b: str, t: float) -> QColor:
    ca, cb = QColor(a), QColor(b)
    return QColor(
        round(ca.red() + (cb.red() - ca.red()) * t),
        round(ca.green() + (cb.green() - ca.green()) * t),
        round(ca.blue() + (cb.blue() - ca.blue()) * t),
    )


def build_palette(theme: Theme) -> QPalette:
    """A complete QPalette for the chrome.  Designed for the Fusion style."""
    pal = QPalette()
    R = QPalette.ColorRole
    G = QPalette.ColorGroup

    def put(role: QPalette.ColorRole, value: str | QColor, disabled: str | QColor | None = None) -> None:
        color = QColor(value)
        pal.setColor(G.Active, role, color)
        pal.setColor(G.Inactive, role, color)
        pal.setColor(G.Disabled, role, QColor(disabled) if disabled is not None else color)

    muted = _mix(theme.chrome_fg, theme.chrome_bg, 0.55)
    put(R.Window, theme.chrome_bg)
    put(R.WindowText, theme.chrome_fg, muted)
    put(R.Base, theme.input_bg)
    put(R.AlternateBase, theme.chrome_hover)
    put(R.Text, theme.fg, muted)
    put(R.Button, theme.chrome_bg)
    put(R.ButtonText, theme.chrome_fg, muted)
    put(R.BrightText, theme.on_accent)
    put(R.ToolTipBase, theme.input_bg)
    put(R.ToolTipText, theme.chrome_fg)
    put(R.PlaceholderText, theme.chrome_secondary)
    put(R.Highlight, theme.selection, theme.chrome_pressed)
    put(R.HighlightedText, theme.fg, muted)
    put(R.Link, theme.accent)
    put(R.LinkVisited, theme.accent)
    put(R.Accent, theme.accent)
    # bevel roles Fusion uses for frames, sliders and check boxes
    if theme.is_dark:
        put(R.Light, _mix(theme.chrome_pressed, "#FFFFFF", 0.10))
        put(R.Midlight, theme.chrome_pressed)
        put(R.Mid, theme.chrome_border)
        put(R.Dark, _mix(theme.chrome_bg, "#000000", 0.35))
        put(R.Shadow, "#000000")
    else:
        put(R.Light, "#FFFFFF")
        put(R.Midlight, theme.chrome_hover)
        put(R.Mid, theme.chrome_border)
        put(R.Dark, _mix(theme.chrome_border, "#000000", 0.25))
        put(R.Shadow, _mix(theme.chrome_border, "#000000", 0.55))
    return pal


#: Python references to installed styles (Qt owns them; this keeps the wrapper alive).
_CHROME_STYLES: dict[int, "ChromeStyle"] = {}


def _rgba(color: str, alpha: float) -> str:
    c = QColor(color)
    return f"rgba({c.red()},{c.green()},{c.blue()},{alpha:.2f})"


def build_qss(theme: Theme) -> str:
    """The chrome stylesheet.  Apply to the QApplication together with the palette.

    Role hooks for other owners (``widget.setProperty("erRole", ...)``):
    ``page`` (reading surface, page ``bg``), ``panel`` (settings panel / dock
    body), ``toolbar``, ``statusbar``, ``card`` (error card), ``title`` (card
    title, 20px semibold), ``body`` (card body, 14px secondary), ``secondary``
    (muted label), ``badge`` (small pill, e.g. 文件缺失), ``link`` (accent text),
    ``segment`` (checkable QToolButton in a segmented control), ``primary``
    (accent QPushButton), ``swatch-<colour>`` (highlight colour chip).
    A Python subclass of ``QWidget`` also needs ``WA_StyledBackground`` for
    its role background to paint (verified).

    Deliberately NO box rule for ``QComboBox`` / ``QAbstractSpinBox``: styling
    their frame or ``::drop-down`` without arrow images removed the combo arrow
    and broke the spin buttons (verified); Fusion draws them from the palette.

    Deliberately NO rule for ``QCheckBox`` / ``QRadioButton``: any QSS rule on
    them made the stylesheet engine drop the indicator box (verified: an
    unchecked checkbox rendered as bare text).  Their indicators are painted by
    the proxy style :func:`apply_to_application` installs.

    Deliberately NO ``padding`` on ``QTreeView::item`` / ``QListView::item``:
    verified that it changes the row size hint only after polish, so a list
    filled before it is first shown lays rows out 15px apart but paints them
    23px tall (overlapping text).  Row height belongs to the view/delegate.
    """
    t = theme
    thumb = _rgba(t.chrome_secondary, 0.45)
    thumb_hover = _rgba(t.chrome_secondary, 0.75)
    swatches = "\n".join(
        f'QWidget[erRole="swatch-{c}"] {{ background: {t.highlight(c)}; '
        f'border: 2px solid {t.ink(c)}; border-radius: 9px; }}'
        for c in HIGHLIGHT_COLORS
    )
    return f"""
QMainWindow, QDialog, QStackedWidget {{ background: {t.chrome_bg}; color: {t.chrome_fg}; }}
QWidget[erRole="page"] {{ background: {t.bg}; color: {t.fg}; }}
QWidget[erRole="panel"], QFrame[erRole="panel"] {{
    background: {t.chrome_bg}; color: {t.chrome_fg}; border: none; border-left: 1px solid {t.chrome_border};
}}
QToolBar, QWidget[erRole="toolbar"] {{
    background: {t.chrome_bg}; color: {t.chrome_fg}; border: none; border-bottom: 1px solid {t.chrome_border};
    spacing: 4px; padding: 4px 8px;
}}
QStatusBar, QWidget[erRole="statusbar"] {{
    background: {t.chrome_bg}; color: {t.chrome_secondary}; border: none; border-top: 1px solid {t.chrome_border};
}}
QStatusBar QLabel, QWidget[erRole="statusbar"] QLabel {{ color: {t.chrome_secondary}; background: transparent; }}

QLabel {{ color: {t.chrome_fg}; background: transparent; }}
QLabel[erRole="secondary"], QLabel[erRole="body"] {{ color: {t.chrome_secondary}; }}
QLabel[erRole="title"] {{ font-size: 20px; font-weight: 600; color: {t.chrome_fg}; }}
QLabel[erRole="body"] {{ font-size: 14px; }}
QLabel[erRole="link"] {{ color: {t.accent}; }}
QLabel[erRole="badge"] {{
    background: {t.chrome_pressed}; color: {t.chrome_fg}; border-radius: 4px; padding: 1px 6px; font-size: 11px;
}}
QFrame[erRole="card"] {{
    background: {t.input_bg}; color: {t.chrome_fg}; border: 1px solid {t.chrome_border}; border-radius: 10px;
}}

QToolButton {{
    background: transparent; color: {t.chrome_fg}; border: 1px solid transparent; border-radius: 6px; padding: 4px;
}}
QToolButton:hover {{ background: {t.chrome_hover}; }}
QToolButton:pressed {{ background: {t.chrome_pressed}; }}
QToolButton:checked {{ background: {t.chrome_pressed}; border-color: {t.chrome_border}; }}
QToolButton:disabled {{ color: {t.chrome_secondary}; }}
QToolButton::menu-indicator {{ image: none; width: 0; }}
QToolButton[erRole="segment"] {{
    border: 1px solid {t.chrome_border}; border-radius: 0; padding: 4px 12px; background: {t.input_bg};
}}
QToolButton[erRole="segment"]:hover {{ background: {t.chrome_hover}; }}
QToolButton[erRole="segment"]:checked {{ background: {t.selection}; color: {t.fg}; border-color: {t.accent}; }}

QPushButton {{
    background: {t.input_bg}; color: {t.chrome_fg}; border: 1px solid {t.chrome_border}; border-radius: 6px;
    padding: 5px 14px;
}}
QPushButton:hover {{ background: {t.chrome_hover}; }}
QPushButton:pressed {{ background: {t.chrome_pressed}; }}
QPushButton:disabled {{ color: {t.chrome_secondary}; }}
QPushButton:focus {{ border-color: {t.accent}; }}
QPushButton[erRole="primary"], QPushButton:default {{
    background: {t.accent}; color: {t.on_accent}; border-color: {t.accent};
}}

QLineEdit, QPlainTextEdit, QTextEdit {{
    background: {t.input_bg}; color: {t.fg}; border: 1px solid {t.chrome_border}; border-radius: 6px;
    padding: 4px 8px; selection-background-color: {t.selection}; selection-color: {t.fg};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {{ border-color: {t.accent}; }}
QComboBox QAbstractItemView {{
    background: {t.input_bg}; color: {t.fg}; border: 1px solid {t.chrome_border};
    selection-background-color: {t.selection}; selection-color: {t.fg}; outline: 0;
}}

QTreeView, QListView {{
    background: {t.chrome_bg}; color: {t.chrome_fg}; border: none; outline: 0;
    selection-background-color: {t.selection}; selection-color: {t.fg};
}}
QTreeView::item:hover, QListView::item:hover {{ background: {t.chrome_hover}; }}
QTreeView::item:selected, QListView::item:selected {{ background: {t.selection}; color: {t.fg}; }}

QTabBar::tab {{
    background: transparent; color: {t.chrome_secondary}; border: none; border-bottom: 2px solid transparent;
    padding: 6px 12px;
}}
QTabBar::tab:hover {{ color: {t.chrome_fg}; }}
QTabBar::tab:selected {{ color: {t.chrome_fg}; border-bottom-color: {t.accent}; }}

QDockWidget {{ color: {t.chrome_fg}; }}
QDockWidget::title {{ background: {t.chrome_bg}; padding: 6px; }}
QMainWindow::separator, QSplitter::handle {{ background: {t.chrome_border}; width: 1px; height: 1px; }}

QMenu {{
    background: {t.input_bg}; color: {t.chrome_fg}; border: 1px solid {t.chrome_border}; padding: 4px;
}}
QMenu::item {{ padding: 6px 24px 6px 12px; border-radius: 4px; }}
QMenu::item:selected {{ background: {t.chrome_hover}; }}
QMenu::item:disabled {{ color: {t.chrome_secondary}; }}
QMenu::separator {{ height: 1px; background: {t.chrome_border}; margin: 4px 8px; }}
QToolTip {{ background: {t.input_bg}; color: {t.chrome_fg}; border: 1px solid {t.chrome_border}; padding: 4px 6px; }}

QSlider::groove:horizontal {{ height: 4px; background: {t.chrome_border}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {t.accent}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {t.input_bg}; border: 1px solid {thumb_hover}; width: 14px; height: 14px; margin: -6px 0;
    border-radius: 8px;
}}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {thumb}; min-height: 32px; border-radius: 3px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {thumb}; min-width: 32px; border-radius: 3px; margin: 2px; }}
QScrollBar::handle:hover {{ background: {thumb_hover}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: none; background: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

{swatches}
""".strip() + "\n"


class ChromeStyle(QProxyStyle):
    """Fusion, with check box / radio indicators painted from theme tokens.

    Fusion outlines indicators with ``window.darker(140)``, which is invisible
    on the dark theme's window colour (verified), and any QSS rule on the
    buttons removes the box entirely.  Border = ``chrome_secondary`` (at least
    4.5:1 against the chrome), checked fill = ``accent``, mark = ``on_accent``.
    """

    def __init__(self, theme: Theme) -> None:
        base = QStyleFactory.create("Fusion")
        super().__init__(base)
        self.theme = theme

    def drawPrimitive(self, element: QStyle.PrimitiveElement, option: QStyleOption,  # noqa: N802
                      painter: QPainter, widget: QWidget | None = None) -> None:
        PE = QStyle.PrimitiveElement
        if element in (PE.PE_IndicatorCheckBox, PE.PE_IndicatorItemViewItemCheck,
                       PE.PE_IndicatorRadioButton):
            self._indicator(element == PE.PE_IndicatorRadioButton, option, painter)
            return
        super().drawPrimitive(element, option, painter, widget)

    def _indicator(self, radio: bool, option: QStyleOption, painter: QPainter) -> None:
        t = self.theme
        S = QStyle.StateFlag
        state = option.state
        enabled = bool(state & S.State_Enabled)
        on = bool(state & S.State_On)
        partial = bool(state & S.State_NoChange)
        hover = bool(state & S.State_MouseOver) and enabled

        # whole-pixel origin + 0.5 so the 1px outline lands on one pixel column
        side = min(option.rect.width(), option.rect.height())
        x0 = option.rect.x() + (option.rect.width() - side) // 2
        y0 = option.rect.y() + (option.rect.height() - side) // 2
        box = QRectF(x0 + 0.5, y0 + 0.5, side - 1, side - 1)

        border = QColor(t.accent if (hover or on or partial) else t.chrome_secondary)
        fill = QColor(t.accent if (on or partial) else t.input_bg)
        mark = QColor(t.on_accent)
        if not enabled:
            border = _mix(t.chrome_secondary, t.chrome_bg, 0.5)
            fill = _mix(t.accent, t.chrome_bg, 0.55) if (on or partial) else QColor(t.chrome_bg)
            mark = _mix(t.on_accent, t.chrome_bg, 0.3)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(border, 1.0))
        painter.setBrush(fill)
        if radio:
            painter.drawEllipse(box)
            if on:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(mark)
                r = box.width() * 0.2
                painter.drawEllipse(box.center(), r, r)
        else:
            radius = max(2.0, box.width() * 0.2)
            painter.drawRoundedRect(box, radius, radius)
            pen = QPen(mark, max(1.5, box.width() * 0.13))
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            x, y, w, h = box.x(), box.y(), box.width(), box.height()
            if partial and not on:
                painter.drawLine(QPointF(x + w * 0.28, y + h * 0.5), QPointF(x + w * 0.72, y + h * 0.5))
            elif on:
                path = QPainterPath(QPointF(x + w * 0.25, y + h * 0.52))
                path.lineTo(QPointF(x + w * 0.43, y + h * 0.70))
                path.lineTo(QPointF(x + w * 0.76, y + h * 0.32))
                painter.drawPath(path)
        painter.restore()


def apply_to_application(
    app: QGuiApplication,
    theme: Theme,
    *,
    set_style: bool = True,
    set_color_scheme: bool = True,
) -> None:
    """Apply palette + stylesheet (+ :class:`ChromeStyle`, + Qt colour scheme) to *app*.

    ``set_style`` installs one :class:`ChromeStyle` (Fusion-based) the first
    time and retargets it on later calls.  ``set_color_scheme`` calls
    ``QStyleHints.setColorScheme`` so native parts that follow Qt's scheme
    (verified: the Windows title bar) match the theme; it does NOT break
    :func:`system_is_dark`, which reads the registry first.
    """
    if set_style and hasattr(app, "setStyle"):
        style = _CHROME_STYLES.get(id(app))
        if style is None or not app.property("erChromeStyle"):
            # app.style() cannot be asked: once a stylesheet is set it returns
            # the stylesheet wrapper, whose name() is ''.  Remember instead.
            style = ChromeStyle(theme)
            app.setStyle(style)
            _CHROME_STYLES[id(app)] = style
            app.setProperty("erChromeStyle", True)
        style.theme = theme
    if set_color_scheme:
        hints = QGuiApplication.styleHints()
        if hasattr(hints, "setColorScheme"):
            hints.setColorScheme(Qt.ColorScheme.Dark if theme.is_dark else Qt.ColorScheme.Light)
    app.setPalette(build_palette(theme))
    if hasattr(app, "setStyleSheet"):
        app.setStyleSheet(build_qss(theme))


# ==========================================================================
# WCAG contrast
# ==========================================================================

def relative_luminance(color: str | QColor) -> float:
    """WCAG 2.x relative luminance of an sRGB colour (0.0 black .. 1.0 white)."""
    c = QColor(color)

    def channel(v: int) -> float:
        s = v / 255.0
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(c.red()) + 0.7152 * channel(c.green()) + 0.0722 * channel(c.blue())


def contrast_ratio(a: str | QColor, b: str | QColor) -> float:
    """WCAG contrast ratio between two colours, 1.0 .. 21.0."""
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


#: (label, foreground token, background token) pairs that carry text.
_TEXT_PAIRS: Final[tuple[tuple[str, str, str], ...]] = (
    ("body text on page", "fg", "bg"),
    ("secondary on page", "secondary", "bg"),
    ("link on page", "accent", "bg"),
    ("body text on selection", "fg", "selection"),
    ("chrome text on chrome", "chrome_fg", "chrome_bg"),
    ("chrome secondary on chrome", "chrome_secondary", "chrome_bg"),
    ("input text on input", "fg", "input_bg"),
    ("primary button label", "on_accent", "accent"),
    ("text on yellow highlight", "fg", "hl_yellow"),
    ("text on green highlight", "fg", "hl_green"),
    ("text on blue highlight", "fg", "hl_blue"),
    ("text on pink highlight", "fg", "hl_pink"),
    ("text on find match", "find_fg", "find_bg"),
    ("text on active find match", "find_active_fg", "find_active_bg"),
)


def contrast_report(theme: Theme | None = None) -> dict[str, dict[str, float]]:
    """``{theme_name: {pair_label: ratio}}`` for every text/background pair."""
    themes = [theme] if theme is not None else list(THEMES.values())
    return {
        t.name: {label: round(contrast_ratio(getattr(t, fg), getattr(t, bg)), 2)
                 for label, fg, bg in _TEXT_PAIRS}
        for t in themes
    }


# ==========================================================================
# system dark mode
# ==========================================================================

_PERSONALIZE_KEY: Final[str] = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
_WM_SETTINGCHANGE: Final[int] = 0x001A


def registry_apps_use_light_theme() -> bool | None:
    """``AppsUseLightTheme`` from HKCU, or None when absent / not Windows."""
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _PERSONALIZE_KEY) as key:
            value, kind = winreg.QueryValueEx(key, "AppsUseLightTheme")
    except OSError:
        return None
    if kind != winreg.REG_DWORD or not isinstance(value, int):
        return None
    return value != 0


def qt_color_scheme_is_dark() -> bool | None:
    """``QStyleHints.colorScheme()`` as a bool, or None when unknown / no app.

    Caveat (verified): once anything calls ``setColorScheme()``, this reports
    the app's override instead of the system.
    """
    if QGuiApplication.instance() is None:
        return None
    hints = QGuiApplication.styleHints()
    if not hasattr(hints, "colorScheme"):
        return None
    scheme = hints.colorScheme()
    if scheme == Qt.ColorScheme.Dark:
        return True
    if scheme == Qt.ColorScheme.Light:
        return False
    return None


def detect_system_dark() -> dict[str, Any]:
    """Every detection method's answer, plus the one :func:`system_is_dark` uses.

    ``{'registry': bool|None, 'qt': bool|None, 'palette': bool|None,
    'dark': bool, 'source': 'registry'|'qt'|'palette'|'default'}``
    """
    reg_light = registry_apps_use_light_theme()
    registry = None if reg_light is None else (not reg_light)
    qt = qt_color_scheme_is_dark()
    palette = None
    if QGuiApplication.instance() is not None:
        window = QGuiApplication.palette().color(QPalette.ColorRole.Window)
        palette = window.lightnessF() < 0.5
    if registry is not None:
        dark, source = registry, "registry"
    elif qt is not None:
        dark, source = qt, "qt"
    elif palette is not None:
        dark, source = palette, "palette"
    else:
        dark, source = False, "default"
    return {"registry": registry, "qt": qt, "palette": palette, "dark": dark, "source": source}


def system_is_dark() -> bool:
    """True when Windows' app mode is dark.  Registry first, then Qt, then palette."""
    return bool(detect_system_dark()["dark"])


class _SettingChangeFilter(QAbstractNativeEventFilter):
    """Calls back on ``WM_SETTINGCHANGE`` with lParam ``"ImmersiveColorSet"``.

    Windows broadcasts that message to top-level windows when the app mode
    changes.  Cost measured at ~4 microseconds per native message.
    """

    def __init__(self, callback: Any) -> None:
        super().__init__()
        self._callback = callback
        self.hits = 0

    def nativeEventFilter(self, eventType: Any, message: Any) -> Any:  # noqa: N802
        try:
            if bytes(eventType) != b"windows_generic_MSG":
                return False, 0
            from ctypes import wintypes

            msg = ctypes.cast(int(message), ctypes.POINTER(wintypes.MSG)).contents
            if msg.message == _WM_SETTINGCHANGE and msg.lParam:
                if ctypes.wstring_at(msg.lParam) == "ImmersiveColorSet":
                    self.hits += 1
                    self._callback()
        except Exception:  # a filter must never throw into Qt's event loop
            pass
        return False, 0


class SystemThemeWatcher(QObject):
    """Emits :attr:`darkChanged` when the Windows app mode flips.

    Needs a ``QGuiApplication``.  Each trigger re-reads :func:`system_is_dark`
    and emits only on an actual change, so an app calling ``setColorScheme()``
    (which also fires Qt's signal) does not produce a false change.
    """

    darkChanged = Signal(bool)

    def __init__(self, parent: QObject | None = None, *, native_filter: bool = True,
                 detector: Any = None) -> None:
        super().__init__(parent)
        app = QCoreApplication.instance()
        if app is None:
            raise RuntimeError("SystemThemeWatcher needs a QApplication")
        self._app = app
        self._detect = detector or system_is_dark
        self._dark = bool(self._detect())
        #: Number of re-checks performed, by trigger, for diagnostics.
        self.triggers: dict[str, int] = {"qt": 0, "native": 0, "activate": 0, "manual": 0}
        hints = QGuiApplication.styleHints()
        self._hints = hints if hasattr(hints, "colorSchemeChanged") else None
        if self._hints is not None:
            self._hints.colorSchemeChanged.connect(self._on_qt_scheme)
        self._filter: _SettingChangeFilter | None = None
        if native_filter and sys.platform == "win32":
            self._filter = _SettingChangeFilter(self._on_native)
            app.installNativeEventFilter(self._filter)
        app.installEventFilter(self)

    @property
    def is_dark(self) -> bool:
        """The last observed system mode."""
        return self._dark

    def recheck(self, trigger: str = "manual") -> bool:
        """Re-read the system mode; emit if it changed.  Returns the current value."""
        self.triggers[trigger] = self.triggers.get(trigger, 0) + 1
        dark = bool(self._detect())
        if dark != self._dark:
            self._dark = dark
            self.darkChanged.emit(dark)
        return dark

    def stop(self) -> None:
        """Disconnect every trigger."""
        if self._hints is not None:
            try:
                self._hints.colorSchemeChanged.disconnect(self._on_qt_scheme)
            except (RuntimeError, TypeError):
                pass
            self._hints = None
        if self._filter is not None:
            self._app.removeNativeEventFilter(self._filter)
            self._filter = None
        self._app.removeEventFilter(self)

    # -- triggers ------------------------------------------------------------
    def _on_qt_scheme(self, *_: Any) -> None:
        self.recheck("qt")

    def _on_native(self) -> None:
        self.recheck("native")

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        if obj is self._app and event.type() == QEvent.Type.ApplicationActivate:
            self.recheck("activate")
        return False


class ThemeController(QObject):
    """Holds the user's theme choice and emits the concrete :class:`Theme`.

    ``themeChanged(Theme)`` fires when the choice changes to a different
    concrete theme, or when the choice is ``'system'`` and Windows flips.
    When *app* is given, the palette/QSS are applied to it on every change.
    """

    themeChanged = Signal(object)

    def __init__(self, app: QGuiApplication | None = None, choice: str = "system",
                 parent: QObject | None = None, *, watcher: SystemThemeWatcher | None = None) -> None:
        super().__init__(parent)
        self._app = app
        self._choice = normalize_theme_choice(choice)
        self.watcher = watcher or SystemThemeWatcher(self)
        self.watcher.darkChanged.connect(self._on_system_changed)
        self._theme = resolve_theme(self._choice, system_dark=self.watcher.is_dark)
        if self._app is not None:
            apply_to_application(self._app, self._theme)

    @property
    def choice(self) -> str:
        """``'light' | 'sepia' | 'dark' | 'system'``."""
        return self._choice

    @property
    def theme(self) -> Theme:
        """The concrete theme in effect."""
        return self._theme

    def set_choice(self, choice: str) -> Theme:
        """Change the choice (legacy names accepted); returns the concrete theme."""
        self._choice = normalize_theme_choice(choice)
        self._update()
        return self._theme

    def _on_system_changed(self, _dark: bool) -> None:
        if self._choice == "system":
            self._update()

    def _update(self) -> None:
        theme = resolve_theme(self._choice, system_dark=self.watcher.is_dark)
        if theme is self._theme:
            return
        self._theme = theme
        if self._app is not None:
            apply_to_application(self._app, theme)
        self.themeChanged.emit(theme)
