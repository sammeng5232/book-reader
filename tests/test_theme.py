#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for ``theme.py`` (owner B).  stdlib ``unittest`` only.

Run::

    C:\\Users\\mengz\\AppData\\Local\\Programs\\Python\\Python314\\python.exe ^
        -m unittest tests.test_theme -v

Shows no window: palettes and stylesheets are checked by ``QWidget.grab()`` on
hidden widgets, and the native ``WM_SETTINGCHANGE`` filter is driven by sending
the message to a hidden widget's own HWND (the real system setting is never
touched).  Set ``ER_THEME_SWATCH_DIR`` to also save one swatch PNG per theme.
"""

from __future__ import annotations

import ctypes
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from PySide6.QtCore import QCoreApplication, QEvent, Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication, QPalette  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QCheckBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QPushButton, QSlider, QStyle, QStyleOptionButton, QToolButton, QVBoxLayout, QWidget,
)

import store  # noqa: E402
import theme as T  # noqa: E402

HEX = re.compile(r"^#[0-9A-F]{6}$")


def app() -> QApplication:
    inst = QApplication.instance()
    return inst if inst is not None else QApplication([])


# ==========================================================================
# pure tokens
# ==========================================================================

class TokenTests(unittest.TestCase):
    def test_three_themes_complete_and_hex(self) -> None:
        self.assertEqual(tuple(T.THEMES), ("light", "sepia", "dark"))
        for t in T.THEMES.values():
            toks = t.tokens()
            for key, value in toks.items():
                if key in ("name", "scheme"):
                    continue
                self.assertRegex(value, HEX, f"{t.name}.{key}")
            for c in T.HIGHLIGHT_COLORS:
                self.assertRegex(t.highlight(c), HEX)
                self.assertRegex(t.ink(c), HEX)
        self.assertFalse(T.LIGHT.is_dark)
        self.assertFalse(T.SEPIA.is_dark)
        self.assertTrue(T.DARK.is_dark)

    def test_choices_and_keys(self) -> None:
        self.assertEqual(T.THEME_CHOICES, store.THEME_CHOICES)
        self.assertEqual(T.THEME_CHOICES, ("light", "sepia", "dark", "system"))
        self.assertEqual(T.name_key("light"), "theme.light")
        self.assertEqual(T.name_key("sepia"), "theme.sepia")
        self.assertEqual(T.name_key("dark"), "theme.dark")
        self.assertEqual(T.name_key("system"), "theme.system")
        self.assertEqual(T.name_key("night"), "theme.dark")          # legacy
        self.assertEqual(T.SEPIA.name_key, "theme.sepia")
        self.assertEqual(T.HIGHLIGHT_NAME_KEYS["pink"], "color.pink")

    def test_string_keys_exist_in_every_language(self) -> None:
        try:
            from i18n import TABLES
        except Exception as exc:  # owner I's package not importable yet
            self.skipTest(f"i18n not importable: {exc}")
        keys = list(T.THEME_NAME_KEYS.values()) + list(T.HIGHLIGHT_NAME_KEYS.values())
        for lang, table in TABLES.items():
            for key in keys:
                self.assertIn(key, table, f"{lang} lacks {key}")

    def test_resolve(self) -> None:
        self.assertIs(T.resolve_theme("light"), T.LIGHT)
        self.assertIs(T.resolve_theme("paper"), T.SEPIA)
        self.assertIs(T.resolve_theme("dark"), T.DARK)
        self.assertIs(T.resolve_theme("system", system_dark=True), T.DARK)
        self.assertIs(T.resolve_theme("system", system_dark=False), T.LIGHT)
        self.assertIs(T.resolve_theme("garbage", system_dark=True), T.DARK)
        self.assertIs(T.get_theme("night"), T.DARK)
        with self.assertRaises(KeyError):
            T.get_theme("system")

    def test_css_variables_match_reader_css_contract(self) -> None:
        for t in T.THEMES.values():
            v = T.css_variables(t)
            self.assertEqual(tuple(v), T.CSS_TOKEN_NAMES + T.CSS_FIND_TOKEN_NAMES)
            self.assertEqual(v["--er-find-active-bg"], t.find_active_bg)
            self.assertEqual(v["--er-bg"], t.bg)
            self.assertEqual(v["--er-selection"], t.selection)
            self.assertEqual(v["--er-hl-pink"], t.hl_pink)
            css = T.css_text(t)
            self.assertTrue(css.startswith(":root{") and css.endswith("}"))
            self.assertIn(f"--er-fg:{t.fg}", css)
            self.assertNotIn("!important", css)
            self.assertNotIn("@layer", css)                           # must be unlayered
        css_path = os.path.join(ROOT, "assets", "reader.css")
        if not os.path.exists(css_path):
            self.skipTest("reader.css not present")
        with open(css_path, encoding="utf-8") as f:
            reader_css = f.read()
        if "--er-bg" not in reader_css:
            self.skipTest("reader.css has not adopted the --er-* tokens yet")
        # every documented token has a default in reader.css ...
        for name in T.CSS_TOKEN_NAMES:
            self.assertRegex(reader_css, re.escape(name) + r"\s*:", f"reader.css never declares {name}")
        # ... and every colour reader.css actually reads through var() is one we export
        consumed = set(re.findall(r"var\((--er-[a-z-]+)", reader_css))
        colour_vars = {n for n in consumed
                       if n.startswith(("--er-bg", "--er-fg", "--er-secondary", "--er-accent",
                                        "--er-border", "--er-selection", "--er-hl-", "--er-find-"))}
        self.assertLessEqual(colour_vars, set(T.css_variables(T.LIGHT)),
                             "reader.css reads a colour variable theme.py does not export")

    def test_page_colors_payload(self) -> None:
        p = T.page_colors(T.DARK)
        self.assertEqual(p["theme"], "dark")
        self.assertEqual(p["colors"]["bg"], T.DARK.bg)
        self.assertEqual(p["colors"]["sel"], T.DARK.selection)
        self.assertEqual(p["colors"]["scheme"], "dark")
        hc = p["highlight_colors"]
        self.assertEqual(hc["yellow"]["dark"], T.DARK.hl_yellow)
        self.assertEqual(hc["yellow"]["night"], T.DARK.hl_yellow)
        self.assertEqual(hc["blue"]["paper"], T.SEPIA.hl_blue)
        self.assertEqual(hc["pink"]["day"], T.LIGHT.hl_pink)

    def test_wcag_contrast(self) -> None:
        self.assertAlmostEqual(T.contrast_ratio("#000000", "#FFFFFF"), 21.0, places=2)
        self.assertAlmostEqual(T.contrast_ratio("#777777", "#777777"), 1.0, places=6)
        rep = T.contrast_report()
        self.assertGreaterEqual(rep["light"]["body text on page"], 7.0)
        self.assertGreaterEqual(rep["dark"]["body text on page"], 7.0)
        self.assertGreaterEqual(rep["sepia"]["body text on page"], 7.0)
        for name, pairs in rep.items():
            self.assertGreaterEqual(pairs["chrome text on chrome"], 7.0, name)
            for label, ratio in pairs.items():
                self.assertGreaterEqual(ratio, 4.5, f"{name}: {label} = {ratio}")


# ==========================================================================
# Qt: palette, stylesheet, pixels
# ==========================================================================

def _swatch(t: T.Theme) -> QWidget:
    root = QWidget()
    root.resize(560, 300)
    outer = QVBoxLayout(root)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(0)

    bar = QWidget(objectName="bar")
    bar.setProperty("erRole", "toolbar")
    bar_l = QHBoxLayout(bar)
    for i, choice in enumerate(T.THEME_CHOICES):
        b = QToolButton(text=choice, checkable=True)
        b.setProperty("erRole", "segment")
        b.setChecked(choice == t.name)
        bar_l.addWidget(b)
    bar_l.addStretch(1)
    for c in T.HIGHLIGHT_COLORS:
        chip = QWidget(objectName=f"chip_{c}")
        chip.setProperty("erRole", f"swatch-{c}")
        chip.setFixedSize(18, 18)
        bar_l.addWidget(chip)
    outer.addWidget(bar)

    body = QHBoxLayout()
    body.setSpacing(0)
    panel = QFrame(objectName="panel")
    panel.setProperty("erRole", "panel")
    panel.setFixedWidth(200)
    pl = QVBoxLayout(panel)
    edit = QLineEdit(placeholderText="在本书中搜索")
    pl.addWidget(edit)
    lst = QListWidget(objectName="list")
    for s in ("第一章 汇率的锚", "第二章 清洁浮动", "第三章 三元悖论"):
        lst.addItem(s)
    lst.setCurrentRow(1)
    pl.addWidget(lst)
    pl.addWidget(QCheckBox("仅用于本书", checked=True))
    sl = QSlider(Qt.Orientation.Horizontal)
    sl.setRange(14, 32)
    sl.setValue(21)
    pl.addWidget(sl)
    body.addWidget(panel)

    page = QWidget(objectName="page")
    page.setProperty("erRole", "page")
    pg = QVBoxLayout(page)
    text = QLabel(objectName="text")
    text.setWordWrap(True)
    text.setTextFormat(Qt.TextFormat.RichText)
    marks = " ".join(
        f'<span style="background:{t.highlight(c)}">高亮{c}</span>' for c in T.HIGHLIGHT_COLORS)
    text.setText(
        f'<p style="color:{t.fg}">以外汇储备为代表的官方资本改变了全球资本流动和金融市场格局。'
        f'The quick brown fox.</p>'
        f'<p style="color:{t.secondary}">次要文字 secondary · 34% · 本章剩余 12 分钟</p>'
        f'<p><a style="color:{t.accent}" href="#">链接 link</a> {marks} '
        f'<span style="background:{t.find_bg};color:{t.find_fg}">匹配</span> '
        f'<span style="background:{t.find_active_bg};color:{t.find_active_fg}">当前</span></p>')
    pg.addWidget(text)
    card = QFrame(objectName="card")
    card.setProperty("erRole", "card")
    cl = QVBoxLayout(card)
    title = QLabel("无法打开这本书")
    title.setProperty("erRole", "title")
    msg = QLabel("文件可能已损坏，或者不是 EPUB 格式。")
    msg.setProperty("erRole", "body")
    cl.addWidget(title)
    cl.addWidget(msg)
    row = QHBoxLayout()
    primary = QPushButton("在文件夹中显示")
    primary.setProperty("erRole", "primary")
    row.addWidget(primary)
    row.addWidget(QPushButton("关闭"))
    row.addStretch(1)
    cl.addLayout(row)
    pg.addWidget(card)
    pg.addStretch(1)
    body.addWidget(page, 1)
    outer.addLayout(body, 1)

    status = QWidget(objectName="status")
    status.setProperty("erRole", "statusbar")
    status.setFixedHeight(26)
    sl2 = QHBoxLayout(status)
    sl2.setContentsMargins(8, 0, 8, 0)
    sl2.addWidget(QLabel("第三章 汇率的锚"))
    sl2.addStretch(1)
    sl2.addWidget(QLabel("34%"))
    outer.addWidget(status)
    return root


def _check_option(box: QCheckBox) -> QStyleOptionButton:
    opt = QStyleOptionButton()
    opt.initFrom(box)
    return opt


def _pixel_at(widget: QWidget, child: QWidget, x: int, y: int) -> str:
    img = widget.grab().toImage()
    pos = child.mapTo(widget, child.rect().topLeft())
    return img.pixelColor(pos.x() + x, pos.y() + y).name().upper()


class QtThemeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = app()
        cls._saved_style = cls.app.style().name()
        cls._saved_palette = QPalette(cls.app.palette())
        cls._saved_qss = cls.app.styleSheet()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.app.setStyleSheet(cls._saved_qss)
        cls.app.setPalette(cls._saved_palette)
        cls.app.setStyle(cls._saved_style or "Fusion")
        cls.app.setProperty("erChromeStyle", None)
        hints = QGuiApplication.styleHints()
        if hasattr(hints, "unsetColorScheme"):
            hints.unsetColorScheme()

    def test_palette_roles(self) -> None:
        for t in T.THEMES.values():
            pal = T.build_palette(t)
            R = QPalette.ColorRole
            self.assertEqual(pal.color(R.Window).name().upper(), t.chrome_bg)
            self.assertEqual(pal.color(R.WindowText).name().upper(), t.chrome_fg)
            self.assertEqual(pal.color(R.Base).name().upper(), t.input_bg)
            self.assertEqual(pal.color(R.Text).name().upper(), t.fg)
            self.assertEqual(pal.color(R.Highlight).name().upper(), t.selection)
            self.assertEqual(pal.color(R.Link).name().upper(), t.accent)
            self.assertEqual(pal.color(R.PlaceholderText).name().upper(), t.chrome_secondary)

    def test_qss_parses_and_paints_exact_token_colours(self) -> None:
        out_dir = os.environ.get("ER_THEME_SWATCH_DIR")
        for t in T.THEMES.values():
            qss = T.build_qss(t)
            self.assertNotIn("{t.", qss)                                  # no unformatted field
            T.apply_to_application(self.app, t)
            self.assertTrue(self.app.property("erChromeStyle"))
            self.assertEqual(self.app.styleSheet(), qss)
            w = _swatch(t)
            w.ensurePolished()
            w.layout().activate()
            page = w.findChild(QWidget, "page")
            panel = w.findChild(QFrame, "panel")
            status = w.findChild(QWidget, "status")
            card = w.findChild(QFrame, "card")
            # the reading surface is EXACTLY the page token, the chrome EXACTLY the chrome token
            self.assertEqual(_pixel_at(w, page, page.width() - 3, page.height() - 3), t.bg, t.name)
            self.assertEqual(_pixel_at(w, panel, panel.width() // 2, panel.height() - 4), t.chrome_bg, t.name)
            self.assertEqual(_pixel_at(w, status, status.width() // 2, status.height() // 2), t.chrome_bg)
            self.assertEqual(_pixel_at(w, card, card.width() - 12, card.height() // 2), t.input_bg)
            chip = w.findChild(QWidget, "chip_green")
            self.assertEqual(_pixel_at(w, chip, 9, 9), t.hl_green)
            self.assertEqual(_pixel_at(w, chip, 0, 9), t.ink_green)
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)
                self.assertTrue(w.grab().save(os.path.join(out_dir, f"swatch_{t.name}.png")))
            w.deleteLater()

    def test_check_indicators_visible_in_every_theme(self) -> None:
        for t in T.THEMES.values():
            qss = T.build_qss(t)
            # rules verified to break native sub-controls must not come back
            self.assertNotRegex(qss, r"(?m)^QCheckBox|^QRadioButton|QComboBox::drop-down|QAbstractSpinBox \{")
            T.apply_to_application(self.app, t)
            host = QFrame()
            host.setProperty("erRole", "panel")
            lay = QVBoxLayout(host)
            on = QCheckBox("仅用于本书")
            on.setChecked(True)
            off = QCheckBox("仅用于本书")
            lay.addWidget(on)
            lay.addWidget(off)
            host.resize(200, 80)
            host.ensurePolished()
            host.layout().activate()
            img = host.grab().toImage()
            opt_rect = self.app.style().subElementRect(
                QStyle.SubElement.SE_CheckBoxIndicator, _check_option(on), on)
            for box, checked in ((on, True), (off, False)):
                r = opt_rect.translated(box.pos())
                center = img.pixelColor(r.center()).name().upper()
                self.assertEqual(center, t.accent if checked else t.input_bg, f"{t.name} checked={checked}")
                # the unchecked outline must stand out from the chrome (WCAG 1.4.11: 3:1)
                if not checked:
                    y = r.center().y()
                    best = max(T.contrast_ratio(img.pixelColor(x, y).name(), t.chrome_bg)
                               for x in range(r.left(), r.left() + 3))
                    self.assertGreaterEqual(best, 3.0, f"{t.name}: unchecked box outline is invisible")
            host.deleteLater()

    def test_item_views_filled_before_show_do_not_overlap(self) -> None:
        # Reproduces with the swatch layout (verified: with `QListView::item{padding:4px 6px}`
        # added to the QSS this asserts rows at tops 0/15/30 painted 23px tall).
        for t in T.THEMES.values():
            T.apply_to_application(self.app, t)
            w = _swatch(t)
            w.ensurePolished()
            w.layout().activate()
            w.grab()
            lst = w.findChild(QListWidget, "list")
            rects = [lst.visualItemRect(lst.item(k)) for k in range(lst.count())]
            for a, b in zip(rects, rects[1:]):
                self.assertLess(a.bottom(), b.top(), f"{t.name}: rows overlap {rects}")
            w.deleteLater()

    def test_detection_reports_every_method(self) -> None:
        d = T.detect_system_dark()
        self.assertIn(d["source"], ("registry", "qt", "palette", "default"))
        self.assertIsInstance(d["dark"], bool)
        if sys.platform == "win32":
            self.assertIsNotNone(d["registry"], "AppsUseLightTheme should exist on Windows 10/11")
            self.assertEqual(d["source"], "registry")
            hints = QGuiApplication.styleHints()
            hints.unsetColorScheme()
            QCoreApplication.processEvents()
            if d["qt"] is not None:
                self.assertEqual(T.qt_color_scheme_is_dark(), d["registry"],
                                 "Qt and the registry disagree about the system mode")
            # the override pitfall: forcing the opposite scheme fools Qt but not us
            forced = Qt.ColorScheme.Light if d["registry"] else Qt.ColorScheme.Dark
            hints.setColorScheme(forced)
            QCoreApplication.processEvents()
            self.assertEqual(T.system_is_dark(), d["registry"])
            hints.unsetColorScheme()
            QCoreApplication.processEvents()


class WatcherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = app()

    def setUp(self) -> None:
        self.value = [False]
        self.watcher = T.SystemThemeWatcher(detector=lambda: self.value[0])
        self.seen: list[bool] = []
        self.watcher.darkChanged.connect(self.seen.append)

    def tearDown(self) -> None:
        self.watcher.stop()
        self.watcher.deleteLater()
        hints = QGuiApplication.styleHints()
        if hasattr(hints, "unsetColorScheme"):
            hints.unsetColorScheme()

    @unittest.skipUnless(sys.platform == "win32", "WM_SETTINGCHANGE is Windows-only")
    def test_native_setting_change_triggers_recheck(self) -> None:
        from ctypes import wintypes

        host = QWidget()
        hwnd = int(host.winId())                                        # hidden, never shown
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.SendMessageW.restype = ctypes.c_ssize_t
        other = ctypes.create_unicode_buffer("intl")
        immersive = ctypes.create_unicode_buffer("ImmersiveColorSet")

        user32.SendMessageW(hwnd, 0x001A, 0, ctypes.addressof(other))
        self.assertEqual(self.watcher.triggers["native"], 0)            # unrelated setting ignored
        user32.SendMessageW(hwnd, 0x001A, 0, ctypes.addressof(immersive))
        self.assertEqual(self.watcher.triggers["native"], 1)
        self.assertEqual(self.seen, [])                                 # nothing changed
        self.value[0] = True
        user32.SendMessageW(hwnd, 0x001A, 0, ctypes.addressof(immersive))
        self.assertEqual(self.seen, [True])
        self.assertTrue(self.watcher.is_dark)
        host.deleteLater()

    def test_qt_signal_and_activation_trigger_recheck_without_false_changes(self) -> None:
        hints = QGuiApplication.styleHints()
        hints.setColorScheme(Qt.ColorScheme.Dark)
        QCoreApplication.processEvents()
        hints.setColorScheme(Qt.ColorScheme.Light)
        QCoreApplication.processEvents()
        self.assertGreaterEqual(self.watcher.triggers["qt"], 1)
        self.assertEqual(self.seen, [])            # an app override is not a system change
        self.value[0] = True
        QCoreApplication.sendEvent(self.app, QEvent(QEvent.Type.ApplicationActivate))
        self.assertEqual(self.watcher.triggers["activate"], 1)
        self.assertEqual(self.seen, [True])
        self.value[0] = False
        self.assertFalse(self.watcher.recheck())
        self.assertEqual(self.seen, [True, False])

    def test_controller_follows_system_only_when_choice_is_system(self) -> None:
        ctl = T.ThemeController(None, "system", watcher=self.watcher)
        emitted: list[T.Theme] = []
        ctl.themeChanged.connect(emitted.append)
        self.assertIs(ctl.theme, T.LIGHT)
        self.value[0] = True
        self.watcher.recheck()
        self.assertIs(ctl.theme, T.DARK)
        self.assertEqual(emitted, [T.DARK])
        self.assertIs(ctl.set_choice("paper"), T.SEPIA)                  # legacy name accepted
        self.assertEqual(ctl.choice, "sepia")
        self.value[0] = False
        self.watcher.recheck()
        self.assertIs(ctl.theme, T.SEPIA)                               # not following now
        self.assertEqual(emitted, [T.DARK, T.SEPIA])
        ctl.set_choice("sepia")
        self.assertEqual(len(emitted), 2)                               # no-op, no signal
        ctl.set_choice("system")
        self.assertIs(ctl.theme, T.LIGHT)
        ctl.deleteLater()


if __name__ == "__main__":
    unittest.main(verbosity=2)
