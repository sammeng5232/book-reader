# `theme.py` — API (owner B)

Three themes (日 `light`, 纸 `sepia`, 夜 `dark`) plus follow-system. One set of tokens drives the Qt
chrome **and** the book page, so they are the same colour. It depends on PySide6 and `store` (for
`THEME_CHOICES`). Importing it has no side effects. Tests: `tests/test_theme.py` (15 tests, no visible
window).

## Quick start (owner G / E / F)

```python
import theme
from store import Store

store = Store()
ctl = theme.ThemeController(app, store.get("reader.theme"))   # applies palette + QSS + style now
ctl.themeChanged.connect(lambda t: (reader_page.apply_theme(t), host.set_theme_css(theme.css_text(t))))
# settings panel:  ctl.set_choice("sepia"); store.set("reader.theme", "sepia")
# page settings:   js_settings = {**store.reader_settings(bid), **theme.page_colors(ctl.theme)}
# no white flash:  view.page().setBackgroundColor(ctl.theme.qcolor("bg"))
```

## Vocabulary

| Symbol | Value |
|---|---|
| `THEME_CHOICES` | `("light", "sepia", "dark", "system")` (from `store`) |
| `THEME_NAMES` | `("light", "sepia", "dark")` |
| `THEME_NAME_KEYS` | `{"light": "theme.light", "sepia": "theme.sepia", "dark": "theme.dark", "system": "theme.system"}` |
| `HIGHLIGHT_COLORS` | `("yellow", "green", "blue", "pink")` (Ctrl+1..4 order) |
| `HIGHLIGHT_NAME_KEYS` | `{"yellow": "color.yellow", ...}` |
| `CSS_TOKEN_NAMES` | the 10 `--er-*` tokens reader.css documents |
| `CSS_FIND_TOKEN_NAMES` | `--er-find-bg`, `--er-find-fg`, `--er-find-active-bg`, `--er-find-active-fg` |

Display names always come from string keys:

```python
for choice in theme.THEME_CHOICES:
    button = QToolButton(text=theme.display_name(choice), checkable=True)   # S("theme.sepia") -> 纸
```

`name_key(choice) -> str` returns the key (legacy names accepted). `display_name(choice) -> str` returns
`strings.S(name_key(choice))`, with a lazy import.

## `@dataclass(frozen=True) class Theme`

Every colour is `#RRGGBB`.

| Group | Fields |
|---|---|
| identity | `name` (`light`/`sepia`/`dark`), `scheme` (`light`/`dark`) |
| page | `bg`, `fg`, `secondary`, `accent`, `border`, `selection` |
| chrome | `chrome_bg`, `chrome_fg`, `chrome_secondary`, `chrome_border`, `chrome_hover`, `chrome_pressed`, `input_bg`, `on_accent` |
| highlights | `hl_yellow`, `hl_green`, `hl_blue`, `hl_pink` (fills); `ink_yellow`, `ink_green`, `ink_blue`, `ink_pink` (strong: underline style, the 3px bar in 批注, swatches) |
| search | `find_bg`, `find_fg`, `find_active_bg`, `find_active_fg` |

Members: `is_dark`, `name_key`, `tokens() -> dict`, `highlight(color) -> str`, `ink(color) -> str`,
`qcolor(token) -> QColor`.

```python
bar.setStyleSheet(f"background:{t.ink(h['color'])}")      # 批注 row colour bar
```

Module constants `LIGHT`, `SEPIA`, `DARK`, `THEMES = {"light": LIGHT, ...}`.

| Token | light | sepia | dark |
|---|---|---|---|
| bg | `#FFFFFF` | `#F6F0E4` | `#16181C` |
| fg | `#1A1A1A` | `#33302B` | `#C9CCD1` |
| secondary | `#636363` | `#665E54` | `#8A9099` |
| accent | `#1155CC` | `#85561F` | `#7FB4F5` |
| border | `#E0E0E0` | `#DFD5C0` | `#2A2E36` |
| selection | `#B4D5FE` | `#E3D3AE` | `#2E4763` |
| chrome_bg / chrome_fg | `#F3F3F3` / `#1A1A1A` | `#EFE8DA` / `#33302B` | `#101216` / `#C9CCD1` |
| hl yellow/green/blue/pink | `#FFF08A #BDEBBD #B9DCF7 #F8C6D8` | `#EFDF9A #C7DEBB #C2D6E2 #E8C3C9` | `#5C4E14 #2A4A2E #233F55 #522838` |

The palettes start from the product-spec presets. They were changed only where a measured ratio fell short:
- sepia `secondary` measured 4.17:1 and is now 5.62.
- dark highlight fills were darkened: the yellow fill measured 4.22 and is now 5.10.
- dark `find_active_bg`: white text on it measured 4.18 and is now 5.33.
- light `secondary`, sepia `accent` and dark `secondary` were tightened for margin.

## Resolving a choice

| Function | Notes |
|---|---|
| `resolve_theme(choice, *, system_dark=None) -> Theme` | `'system'` (and anything unknown) gives DARK if Windows is dark, else LIGHT (never sepia). Legacy `day/paper/night` accepted |
| `get_theme(name) -> Theme` | concrete names only; `'system'` raises `KeyError` |

```python
t = theme.resolve_theme(store.get("reader.theme"))
```

## Page side (reader.css / reader.js)

| Function | Returns |
|---|---|
| `css_variables(theme) -> dict` | `CSS_TOKEN_NAMES` + `CSS_FIND_TOKEN_NAMES` mapped to hex values, in that order |
| `css_text(theme) -> str` | unlayered `:root{--er-bg:#F6F0E4;...}` with no `!important`, for `webhost.BookHost.set_theme_css()` |
| `page_colors(theme) -> dict` | `{"theme": name, "colors": {bg, fg, secondary, accent, link, border, sel, selection, scheme, hl_*, find_*}, "highlight_colors": ...}`, merged into the `applySettings` object |
| `highlight_colors_payload() -> dict` | `{colour: {light, sepia, dark, day, paper, night: hex}}`, keyed by both naming conventions |

```python
host.set_theme_css(theme.css_text(t))
host.run_js(f"epubReader.applySettings({json.dumps({**store.reader_settings(bid), **theme.page_colors(t)})})")
```

Verified in a live QWebEngineView: with `css_text()` injected, Chromium painted the page background,
a `var(--er-hl-yellow)` block and a `var(--er-selection)` block **byte-identical** to the token hex values
in all three themes. With reader.css loaded, `getComputedStyle(html).backgroundColor` was exactly
`rgb(22, 24, 28)` = `#16181C` for dark.

## Chrome side (Qt)

| Function | Notes |
|---|---|
| `build_palette(theme) -> QPalette` | all roles incl. Disabled group, `PlaceholderText`, `Accent`, bevel roles |
| `build_qss(theme) -> str` | application stylesheet (see role hooks) |
| `class ChromeStyle(QProxyStyle)` | Fusion. Draws check box / radio / item-view check indicators from tokens (`theme` attribute) |
| `apply_to_application(app, theme, *, set_style=True, set_color_scheme=True)` | installs `ChromeStyle` once (then retargets it), sets `QStyleHints.setColorScheme` to match, palette, QSS |

```python
theme.apply_to_application(app, theme.DARK)
```

Role hooks. Set them with `widget.setProperty("erRole", "<role>")` before the widget is shown.

**Python subclasses of `QWidget` also need `setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)`**,
or the role background is not painted. Verified: a `class Page(QWidget)` with `erRole="page"` showed the
parent's `#101216` instead of `#16181C` until the attribute was set. `QFrame` and plain `QWidget`
instances do not need it.

| Role | Use |
|---|---|
| `page` | reading-surface container, painted exactly `bg` |
| `panel` | settings panel / dock body, `chrome_bg` with a left hairline |
| `toolbar`, `statusbar` | the 44 px bar and the 26 px bar |
| `card` | in-pane error card (`input_bg`, 10 px radius) |
| `title` | card title, 20 px semibold |
| `body` | card body, 14 px, secondary colour |
| `secondary` | muted label |
| `link` | accent label |
| `badge` | small pill, e.g. 文件缺失 |
| `segment` | checkable `QToolButton` in a segmented control (checked = `selection` + accent border) |
| `primary` | accent `QPushButton` (`on_accent` label) |
| `swatch-yellow` / `-green` / `-blue` / `-pink` | 18 px highlight colour chip (fill + ink ring) |

Verified by pixel sampling of `grab()` (`tests/test_theme.py`):
- `page` = `bg`, `panel`/`statusbar` = `chrome_bg`, `card` = `input_bg`.
- swatch centre = `hl_*`, ring = `ink_*`.
- checked box centre = `accent`, unchecked box centre = `input_bg`.
- unchecked outline is at least 3:1 against the chrome.

Also verified visually on screen: the title bar follows the theme. It measured `#F3F3F3` for light and
sepia and `#202020` for dark.

Things the QSS deliberately does **not** style, each verified broken when styled without images:
- `QCheckBox`/`QRadioButton`: the indicator box vanished.
- `QComboBox` frame or `::drop-down`: the arrow vanished.
- `QAbstractSpinBox` frame: the buttons broke.
- `QListView::item`/`QTreeView::item` padding: rows of a list filled before first show were laid out
  15 px apart but painted 23 px tall. Row height belongs to the view or delegate.

## Contrast (WCAG 2.x)

`relative_luminance(color) -> float`, `contrast_ratio(a, b) -> float`, and
`contrast_report(theme=None) -> {theme: {pair: ratio}}`.

```python
theme.contrast_report(theme.SEPIA)["sepia"]["body text on page"]   # 11.58
```

| Pair | light | sepia | dark |
|---|---:|---:|---:|
| body text on page | **17.40** | **11.58** | **11.04** |
| secondary on page | 6.01 | 5.62 | 5.53 |
| link on page | 6.57 | 5.53 | 8.25 |
| body text on selection | 11.51 | 8.88 | 5.93 |
| chrome text on chrome | 15.68 | 10.78 | 11.64 |
| chrome secondary on chrome | 5.84 | 5.56 | 5.83 |
| input text on input | 17.40 | 12.19 | 10.38 |
| primary button label | 6.57 | 6.27 | 8.84 |
| text on yellow / green / blue / pink highlight | 14.99 / 13.08 / 12.14 / 11.65 | 9.82 / 9.13 / 8.77 / 8.18 | 5.10 / 6.16 / 6.81 / 7.58 |
| text on find match | 13.37 | 10.82 | 4.82 |
| text on active find match | 7.24 | 5.72 | 5.33 |

Body text clears 7:1 (AAA) in all three themes, sepia included. Every listed pair clears 4.5:1 (AA), and
the test suite enforces both.

## System dark mode

| Function | Notes |
|---|---|
| `registry_apps_use_light_theme() -> bool \| None` | `HKCU\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize\AppsUseLightTheme` |
| `qt_color_scheme_is_dark() -> bool \| None` | `QStyleHints.colorScheme()`; None without an app or when Unknown |
| `detect_system_dark() -> dict` | `{registry, qt, palette, dark, source}` |
| `system_is_dark() -> bool` | registry first, then Qt, then palette lightness |

```python
theme.detect_system_dark()
# on this machine: {'registry': True, 'qt': True, 'palette': True, 'dark': True, 'source': 'registry'}
```

**What actually works here** (Windows 11 26200, PySide6/Qt 6.11.1, system in dark mode):
- **Both** the registry (`AppsUseLightTheme = 0`) and `styleHints().colorScheme()` (`Dark`) report correctly.
- The registry is authoritative. As soon as anything calls `QStyleHints.setColorScheme()`, which
  `apply_to_application` does so the title bar matches, `colorScheme()` returns the **app override**:
  forcing Light on this dark system made it report Light. The override also fires `colorSchemeChanged`.

### `class SystemThemeWatcher(QObject)`

`darkChanged(bool)` is emitted only when the system mode actually flips. It is built as
`SystemThemeWatcher(parent=None, *, native_filter=True, detector=None)` and needs a QApplication.
Three triggers each cause a registry re-read:

1. Qt `colorSchemeChanged`
2. a native `WM_SETTINGCHANGE` filter matching lParam `"ImmersiveColorSet"`. This keeps working while the
   scheme is overridden; the cost measured about 4 µs per native message.
3. `QEvent.ApplicationActivate`

Members: `is_dark`, `recheck(trigger="manual") -> bool`, `stop()`, `triggers` (counts per source).

```python
w = theme.SystemThemeWatcher(app)
w.darkChanged.connect(lambda dark: log.info("system dark=%s", dark))
```

### `class ThemeController(QObject)`

`ThemeController(app=None, choice="system", parent=None, *, watcher=None)` has `themeChanged(Theme)`. It
emits when the effective concrete theme changes: either the choice changed, or the choice is `system`
and Windows flipped. When `app` is given it calls `apply_to_application` on every change. Members:
`choice`, `theme`, `set_choice(choice) -> Theme` (legacy names accepted), `watcher`.

```python
ctl.set_choice("paper")   # -> SEPIA; later system flips are ignored until set_choice("system")
```
