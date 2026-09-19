# `strings.py` + `i18n/` — API (owner I)

Every user-visible string in Book Reader comes from here. The UI language can be 简体中文, 繁體中文, English or
日本語, and it switches **live**. `strings.py` is the API and imports no Qt. The tables live in `i18n/`, one module
per language.

```
strings.py          API, shortcut labels, cheat-sheet layout, checker (python strings.py --check)
i18n/__init__.py    TABLES = {"zh-Hans": ..., "zh-Hant": ..., "en": ..., "ja": ...}, MODULE_FILES
i18n/zh_Hans.py     TABLE  (product-spec §5b copy)
i18n/en.py          TABLE  (reference for the stubs)
i18n/zh_Hant.py     TABLE  (STUB: "[zh-Hant] " + English, until the translator phase)
i18n/ja.py          TABLE  (STUB: "[ja] " + English, until the translator phase)
docs/i18n-glossary.md   canonical terms in all four languages; translators follow it
tests/test_strings.py   48 unittest cases (python -m unittest tests.test_strings)
```

## 1. How UI code uses it (E, F, G read this first)

```python
from strings import S, plural, tip, duration, format_date, language_changed

class ReaderToolbar(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.toc_btn = QToolButton(self)
        ...
        language_changed.subscribe(self.retranslate_ui)   # held weakly; auto-dropped when the widget dies
        self.retranslate_ui()

    def retranslate_ui(self) -> None:                     # zero-arg or (lang) both work
        self.toc_btn.setToolTip(tip("tb.toc", "toc"))     # '目录 (Ctrl+T)'
        self.title_label.setText(S("tb.title", title=book_title, chapter=chapter_title))
```

Rules:

1. **Never** put a user-facing literal in code. If a key is missing, append it with all four translations to
   `docs/new-keys-<owner>.md` (CONTRACT §1). Do not edit `i18n/`.
2. Every widget that shows text implements `retranslate_ui()`, subscribes it once, and calls it once at the end of
   construction. Text that is built dynamically must be re-rendered there too: status cells, list rows, menus,
   tooltips, placeholders and window titles.
3. Keep **data**, not rendered text, in models. For example, a bookmark row stores the percent and a date, and
   formats `S("bookmarks.meta", percent=..., date=format_date(...))` at paint or retranslate time.
4. Counts go through `plural(key, n)`, never `S(key + ".other")`. Reading times go through `duration(minutes)`.
   Dates go through `format_date(value)`.
5. `{app}` is filled automatically. Do not pass the product name.

### Startup and the settings row (owner G wires this, E owns the panel row)

```python
import strings
from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator

app = QApplication(sys.argv)
strings.set_language(store.get("ui.language", "auto"))   # before any widget is built

qt_translator = QTranslator()                            # Qt's own dialogs and context menus
def _install_qt_translation(lang: str) -> None:
    app.removeTranslator(qt_translator)
    name = strings.QT_LOCALE_NAMES[lang]                 # zh_CN / zh_TW / en_US / ja_JP
    QLocale.setDefault(QLocale(name))
    if qt_translator.load(QLocale(name), "qtbase", "_",
                          QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
        app.installTranslator(qt_translator)
strings.language_changed.subscribe(_install_qt_translation)
_install_qt_translation(strings.current_language())

# settings panel: language selector
combo.clear()
for value, label in strings.language_choices():          # [('auto', '跟随系统（简体中文）'), ('zh-Hans', '简体中文'), ...]
    combo.addItem(label, value)
combo.setCurrentIndex(combo.findData(strings.language_preference()))
combo.activated.connect(lambda i: (store.set("ui.language", combo.itemData(i)),
                                   strings.set_language(combo.itemData(i))))
# row label S("set.language"), hint S("set.language.hint"); rebuild the combo in retranslate_ui
# (the 'auto' row's label is translated), preserving the selection.
```

Verified on this machine: `qtbase_zh_CN.qm`, `qtbase_zh_TW.qm` and `qtbase_ja.qm` all load from PySide6's
translations directory. English needs no translator.

### Strings the web page needs (owners C and E)

`reader.js` cannot import Python. Pass these rendered strings in `epubReader.init(config)` and again after a
language change: `err.section` (the in-flow placeholder for a spine document that fails) and `reader.zoom.hint`
(the image zoom overlay).

## 2. Public symbols

Each entry has one example. Output shown for `zh-Hans` unless stated.

### Constants

**`APP_DISPLAY_NAME: str`** is `"Book Reader"`, never translated. Filesystem and registry identities live in `store.py`.
```python
QApplication.setApplicationDisplayName(strings.APP_DISPLAY_NAME)
```

**`LANGUAGES: tuple[str, ...]`** is `("zh-Hans", "zh-Hant", "en", "ja")`, in selector order.
```python
assert store.get("ui.language") in ("auto", *strings.LANGUAGES)
```

**`LANGUAGE_NAMES: dict[str, str]`** holds endonyms that are never translated.
```python
strings.LANGUAGE_NAMES["ja"]            # '日本語' in every UI language
```

**`QT_LOCALE_NAMES: dict[str, str]`** maps a language code to a Qt locale name, for `QLocale` / `qtbase_*.qm`.
```python
QLocale.setDefault(QLocale(strings.QT_LOCALE_NAMES[strings.current_language()]))
```

**`STUB_MARKERS: dict[str, str]`** gives the prefix on untranslated stub values (`"[ja] "`, ...).
```python
strings.TABLES["ja"]["tb.toc"].startswith(strings.STUB_MARKERS["ja"])   # True until translated
```

**`TABLES: dict[str, dict[str, str]]`** is the four tables (read-only by convention). UI code uses `S()` instead.
```python
len(strings.TABLES["en"])               # 435
```

**`KEYS: dict[str, tuple[str, ...]]`** maps a shortcut id to its key-combo labels (ASCII and arrow glyphs, the same in
every language). It only displays shortcuts; owner G still binds the real `QKeySequence`s.
```python
strings.KEYS["find_next"]               # ('F3', 'n')
```

**`CHEATSHEET_LAYOUT`** is the F1 overlay structure: `((group key, ((KEYS id, description key), ...)), ...)`.
```python
for group_key, rows in strings.CHEATSHEET_LAYOUT: ...
```

**`THEME_KEYS: dict[str, str]`** maps a theme name to its string key. It accepts both `light/sepia/dark/system` and
`day/paper/night` (plus `auto`).
```python
S(strings.THEME_KEYS["paper"])          # '纸'
```

**`FONT_KEYS: dict[str, str]`** maps a font family to its display-name key. See `font_label`.
```python
"SimSun" in strings.FONT_KEYS           # True
```

### Lookup

**`S(key, /, **fmt) -> str`** returns the string in the current language with placeholders filled. A missing key or
placeholder raises `KeyError` when running from source (strict). In a frozen build it returns the key, or leaves
the placeholder visible.
```python
S("err.missing.body", title="从此岸到彼岸")     # '《从此岸到彼岸》原来在：'
```

**`translate(lang, key, /, **fmt) -> str`** works like `S` in an explicit language.
```python
strings.translate("en", "menu.about")   # 'About Book Reader'
```

**`has(key, /) -> bool`** reports whether a key exists.
```python
if strings.has("tb.fullscreen.exit"): ...
```

**`plural(key, n, /, **fmt) -> str`** picks `key.one` or `key.other` and fills `{n}` (pass `n=` to show a formatted number).
```python
strings.plural("search.count", 2371)                 # '共 2371 处'   en: '2371 matches'
strings.plural("lib.count", 1)                       # en: '1 book'
strings.plural("search.count", 2371, n="2,371")      # en: '2,371 matches'
```

**`plural_category(n, /, lang=None) -> str`** returns `'one'` or `'other'`. English gives `'one'` only for the integer 1;
zh and ja always give `'other'`.
```python
strings.plural_category(1, "en")        # 'one'
```

**`duration(minutes, /, *, long=False) -> str`** builds a reading-time phrase, rounded to whole minutes.
```python
S("status.left.chapter", time=strings.duration(12))       # '本章剩余 12 分钟'
S("status.left.book", time=strings.duration(200))         # '全书剩余 3 小时 20 分钟'   en: '3 h 20 min left in book'
strings.duration(0)                                       # '不到 1 分钟'              en: 'under 1 min'
strings.duration(125, long=True)                          # en: '2 hours 5 minutes'
```

**`format_date(value, /, *, today=None, relative=True) -> str`** formats a `date`, a `datetime` (aware values are
converted to local time), an ISO string as `store.py` writes it, or epoch seconds. It gives 今天 / 昨天, then month
and day for the current year, then the full date. `None` gives `''`.
```python
strings.format_date("2026-03-02T09:00:00+08:00")         # '3月2日'    en: 'Mar 2'
strings.format_date(entry["added_at"], relative=False)   # '2025年12月31日'   en: 'Dec 31, 2025'
```

**`tip(label_key, keys_id=None, /) -> str`** returns a tooltip: the label plus the first shortcut.
```python
btn.setToolTip(strings.tip("tb.bookmark.add", "bookmark_toggle"))   # '添加书签 (Ctrl+D)'
```

**`keys_label(keys_id, /, sep=" / ") -> str`** joins every combo for a shortcut id.
```python
strings.keys_label("next_page")         # 'Space / PageDown / → / ↓'
```

**`cheatsheet() -> list[tuple[str, list[tuple[tuple[str, ...], str]]]]`** returns the F1 overlay, rendered.
```python
for title, rows in strings.cheatsheet():
    for combos, description in rows: ...   # (('F3', 'n'), '下一个搜索结果')
```

**`theme_label(name, /) -> str`** returns a theme's display name. An unknown name raises in strict mode.
```python
strings.theme_label("night")            # '夜'   en: 'Dark'
```

**`font_label(family, /) -> str`** returns a font's display name. Unknown families pass through unchanged.
```python
combo.addItem(strings.font_label("Microsoft YaHei"), "Microsoft YaHei")   # '微软雅黑'
```

### Language selection

**`set_language(lang) -> None`** switches the language and notifies `language_changed` only if the effective
language changed. It accepts `'auto'`, `None`, `''` or a code. A legacy or locale-like value (`'zh'`, `'zh_TW'`,
`'ja-JP'`) is mapped through `resolve_auto`, so an old settings file never breaks startup.
```python
strings.set_language("zh-Hant")
```

**`current_language() -> str`** returns the active code. On first use it resolves `'auto'` lazily, without notifying.
```python
QLocale(strings.QT_LOCALE_NAMES[strings.current_language()])
```

**`language_preference() -> str`** returns what the user chose: `'auto'` or a code (for the selector's current row).
```python
combo.setCurrentIndex(combo.findData(strings.language_preference()))
```

**`language_choices() -> list[tuple[str, str]]`** returns the selector rows. The `auto` row is labeled with what it
resolves to.
```python
strings.language_choices()[0]           # ('auto', '跟随系统（简体中文）')
```

**`resolve_auto(locale_name, /) -> str`** maps a locale name to a language. zh_CN, zh_SG, bare zh and zh-Hans-* map
to zh-Hans. zh_TW, zh_HK, zh_MO and zh-Hant-* map to zh-Hant. ja* maps to ja. Anything else maps to en. It accepts
`_` or `-`, POSIX suffixes and Windows long names.
```python
strings.resolve_auto("zh_HK")           # 'zh-Hant'
```

**`system_locale_name() -> str`** returns the Windows display language. It tries Win32 `GetUserDefaultUILanguage`
first, then `QLocale.system().name()` if PySide6 is already loaded, then `locale.getlocale()`, then `LANG`.
```python
strings.system_locale_name()            # 'zh-CN' on this machine
```

**`language_changed: LanguageChangedRegistry`** / **`class LanguageChangedRegistry`** is a pure-Python subscriber list.

- `subscribe(fn) -> fn` also works as a decorator. Subscribing the same callable twice does nothing.
- `unsubscribe(fn) -> bool` removes a subscriber.
- `emit(lang)` notifies subscribers. `set_language` calls it.
- `clear()` removes every subscriber, and `len()` counts them.

Bound methods are weak references; functions and lambdas are strong. A callback is called as `fn(lang)` when it
takes a positional argument, else as `fn()`. A subscriber whose Qt object was deleted is dropped silently. Any other
exception is logged, the remaining subscribers still run, and in strict mode the first error is re-raised afterwards.
Use it from the UI thread only.
```python
strings.language_changed.subscribe(self.retranslate_ui)
```

### Strictness and checking

**`set_strict(strict) -> None`** / **`is_strict() -> bool`** control whether a missing key raises. The default is
`__debug__ and not sys.frozen`.
```python
strings.set_strict(False)   # emulate the frozen build in a test
```

**`check(tables=None, *, strict_translations=False, scan_files=True) -> list[str]`** returns every problem found; an
empty list means OK. It checks:

- the four languages are present, with identical key sets
- identical `{placeholder}` sets per key, using only `{simple_name}`
- no empty values and no stray whitespace
- the retired working name appears in no table, source file or doc of this package
- no hard-coded `Book Reader` in copy (use `{app}`)
- no exclamation marks, no emoji, no mojibake, and `…` rather than `...`
- plural `.one` / `.other` pairs are complete
- every key the helpers and cheat sheet reference exists
- full-width punctuation next to CJK text
- script rules: no Traditional glyphs in zh-Hans, no Simplified glyphs in zh-Hant or ja
- the glossary's regional-term blocklists for zh-Hant and ja
- no 请稍候 and no cute particles in Chinese

With `strict_translations`, remaining stub markers are problems too.
```python
assert strings.check() == []
```

**`stub_counts(tables=None) -> dict[str, int]`** counts untranslated stub values per language.
```python
strings.stub_counts()                   # {'zh-Hans': 0, 'zh-Hant': 435, 'en': 0, 'ja': 435} today
```

**`main(argv=None) -> int`** is the command line.
```
python strings.py --check                          # exit 0 = OK
python strings.py --check --strict-translations    # also fails on stub values (translator phase gate)
python strings.py --show menu.about                # one key in all four languages
```

### `i18n` package

**`i18n.TABLES`** has the same contents as `strings.TABLES`. **`i18n.MODULE_FILES`** maps a language to its source
path, for the checker. Each **`i18n.<zh_Hans|zh_Hant|en|ja>.TABLE: dict[str, str]`** holds one language.
```python
from i18n import zh_Hans; zh_Hans.TABLE["tb.toc"]   # '目录'
```

## 3. Conventions inside the tables

- Keys are dotted: `<surface>.<thing>[.<variant>]`. Plural keys end in `.one` / `.other`.
- `{app}` is automatic. Other placeholders: `{title}` `{chapter}` `{n}` `{i}` `{p}` `{percent}` `{date}` `{time}`
  `{q}` `{kind}` `{folder}` `{language}` `{theme}` `{label}` `{keys}` `{text}` `{note}` `{author}` `{version}`
  `{py}` `{qt}` `{exe}` `{path}` `{before}` `{match}` `{after}` `{h}` `{m}` `{d}` `{y}` `{month}` `{hours}`
  `{minutes}`.
- Sentences ending in `：` / `:` (`err.missing.body`, `about.storage`, `export.failed`, `err.crash.body`) are
  followed by a path **on its own selectable line**. Never format a path into the sentence.
- DRM card: use `err.drm.body` with `kind=S("err.drm.adept")` or `S("err.drm.lcp")`. For an unknown scheme use
  `err.drm.body.unknown`, which takes no `kind`.
- Structure card: `err.structure.body` covers a missing container.xml (`EpubError.kind == 'no_container'`), and
  `err.structure.body.opf` covers `bad_opf`.
- Spin boxes: `setSuffix(S("set.suffix.px"))`. Suffix values carry their own leading space, so ja can omit it.
  Labels showing a value use `set.value.*` (`S("set.value.px", n=21)` gives `'21 px'`).
- Status-bar right cell: `status.left.chapter` / `status.left.book` / `status.read`, each with `time=duration(m)`.
  Its context-menu items are `status.cell.*`.
- Undo link after a delete: the status message (`status.bookmark.removed`, `status.highlight.removed`,
  `lib.removed`) followed by a link labeled `common.undo`, then `common.undone` when it is used.

## 4. Key catalog

<!-- BEGIN KEY CATALOG (generated from i18n/en.py; do not edit by hand) -->

435 keys in 39 sections. Values shown for zh-Hans and en; zh-Hant and ja have the same keys.

### window

| key | zh-Hans | en | placeholders |
|---|---|---|---|
| `title.library` | {app} | {app} | app |
| `title.book` | {title} — {app} | {title} — {app} | app, title |

### common words and buttons

| key | zh-Hans | en | placeholders |
|---|---|---|---|
| `common.ok` | 确定 | OK |  |
| `common.cancel` | 取消 | Cancel |  |
| `common.close` | 关闭 | Close |  |
| `common.yes` | 是 | Yes |  |
| `common.no` | 否 | No |  |
| `common.open` | 打开 | Open |  |
| `common.copy` | 复制 | Copy |  |
| `common.delete` | 删除 | Delete |  |
| `common.remove` | 移除 | Remove |  |
| `common.save` | 保存 | Save |  |
| `common.undo` | 撤销 | Undo |  |
| `common.undone` | 已撤销 | Undone |  |
| `common.retry` | 重试 | Try Again |  |
| `common.loading` | 正在打开… | Opening… |  |
| `common.sep` |  ·  |  ·  |  |

### UI language selector

| key | zh-Hans | en | placeholders |
|---|---|---|---|
| `set.language` | 界面语言 | Language |  |
| `set.language.hint` | 更改后立即生效 | Changes apply immediately |  |
| `set.language.auto` | 跟随系统 | System default |  |
| `set.language.auto_resolved` | 跟随系统（{language}） | System default ({language}) | language |

### theme names

| key | zh-Hans | en | placeholders |
|---|---|---|---|
| `theme.light` | 日 | Light |  |
| `theme.sepia` | 纸 | Sepia |  |
| `theme.dark` | 夜 | Dark |  |
| `theme.system` | 跟随系统 | System |  |

### tooltips

| key | zh-Hans | en | placeholders |
|---|---|---|---|
| `tip.fmt` | {label} ({keys}) | {label} ({keys}) | keys, label |

### reader toolbar

| key | zh-Hans | en | placeholders |
|---|---|---|---|
| `tb.toc` | 目录 | Contents |  |
| `tb.back` | 后退 | Back |  |
| `tb.forward` | 前进 | Forward |  |
| `tb.typography` | 排版 | Text & Layout |  |
| `tb.search` | 搜索 | Search |  |
| `tb.bookmark.add` | 添加书签 | Add Bookmark |  |
| `tb.bookmark.remove` | 移除书签 | Remove Bookmark |  |
| `tb.fullscreen` | 全屏 | Full Screen |  |
| `tb.fullscreen.exit` | 退出全屏 | Exit Full Screen |  |
| `tb.more` | 更多 | More |  |
| `tb.title` | {title} · {chapter} | {title} · {chapter} | chapter, title |

### overflow menu

| key | zh-Hans | en | placeholders |
|---|---|---|---|
| `menu.open` | 打开书籍… | Open Book… |  |
| `menu.library` | 回到书架 | Back to Library |  |
| `menu.bookinfo` | 书籍信息 | Book Details |  |
| `menu.export` | 导出批注… | Export Highlights… |  |
| `menu.shortcuts` | 快捷键 | Keyboard Shortcuts |  |
| `menu.settings` | 设置 | Settings |  |
| `menu.about` | 关于 {app} | About {app} | app |
| `menu.quit` | 退出 | Exit |  |

### left dock: pane switcher

| key | zh-Hans | en | placeholders |
|---|---|---|---|
| `dock.toc` | 目录 | Contents |  |
| `dock.bookmarks` | 书签 | Bookmarks |  |
| `dock.notes` | 批注 | Highlights |  |
| `dock.search` | 搜索 | Search |  |

### dock: contents pane

| key | zh-Hans | en | placeholders |
|---|---|---|---|
| `toc.synthetic` | 本书未提供目录，以下为章节文件 | This book has no table of contents. Its chapter files are listed instead. |  |
| `toc.untitled` | 无标题章节 | Untitled section |  |
| `toc.empty` | 本书没有可显示的章节 | This book has no sections to show. |  |

### dock: bookmarks pane

| key | zh-Hans | en | placeholders |
|---|---|---|---|
| `bookmarks.empty` | 还没有书签。按 Ctrl+D 添加。 | No bookmarks yet. Press Ctrl+D to add one. |  |
| `bookmarks.meta` | {percent}% · {date} | {percent}% · {date} | date, percent |
| `bookmarks.go` | 跳转到书签 | Go to Bookmark |  |
| `bookmarks.remove` | 删除书签 | Delete Bookmark |  |
| `bookmarks.count.one` | {n} 个书签 | {n} bookmark | n |
| `bookmarks.count.other` | {n} 个书签 | {n} bookmarks | n |

### dock: highlights pane

| key | zh-Hans | en | placeholders |
|---|---|---|---|
| `notes.empty` | 还没有批注。选中文字后按 Ctrl+1 至 Ctrl+4 高亮。 | No highlights yet. Select some text, then press Ctrl+1 to Ctrl+4. |  |
| `notes.filter.all` | 全部 | All |  |
| `notes.filter.noted` | 仅显示有笔记 | With notes |  |
| `notes.filtered_empty` | 没有符合筛选条件的批注 | No highlights match this filter. |  |
| `notes.lost` | 无法定位 | Location lost |  |
| `notes.lost.tip` | 这段文字已无法在书中找到。批注已保留，仍可导出。 | This passage can no longer be found in the book. The highlight is kept and can still be exported. |  |
| `notes.export` | 导出为 Markdown… | Export as Markdown… |  |
| `notes.go` | 跳转到批注 | Go to Highlight |  |
| `notes.copy` | 复制文字 | Copy Text |  |
| `notes.add_note` | 添加笔记… | Add Note… |  |
| `notes.edit_note` | 编辑笔记… | Edit Note… |  |
| `notes.color` | 更改颜色 | Change Color |  |
| `notes.remove` | 删除批注 | Delete Highlight |  |
| `notes.count.one` | {n} 条批注 | {n} highlight | n |
| `notes.count.other` | {n} 条批注 | {n} highlights | n |

### highlight colors

| key | zh-Hans | en | placeholders |
|---|---|---|---|
| `color.yellow` | 黄色 | Yellow |  |
| `color.green` | 绿色 | Green |  |
| `color.blue` | 蓝色 | Blue |  |
| `color.pink` | 粉色 | Pink |  |

### text selection menu (reader)

| key | zh-Hans | en | placeholders |
|---|---|---|---|
| `sel.copy` | 复制 | Copy |  |
| `sel.copy_cite` | 复制并附出处 | Copy with Citation |  |
| `sel.highlight.yellow` | 黄色高亮 | Highlight in Yellow |  |
| `sel.highlight.green` | 绿色高亮 | Highlight in Green |  |
| `sel.highlight.blue` | 蓝色高亮 | Highlight in Blue |  |
| `sel.highlight.pink` | 粉色高亮 | Highlight in Pink |  |
| `sel.note` | 高亮并添加笔记… | Highlight and Add Note… |  |
| `sel.search` | 在本书中搜索「{q}」 | Search This Book for “{q}” | q |

### note editor

| key | zh-Hans | en | placeholders |
|---|---|---|---|
| `note.title` | 笔记 | Note |  |
| `note.placeholder` | 输入笔记 | Type a note |  |
| `note.delete` | 删除笔记 | Delete Note |  |

### dock: search pane

| key | zh-Hans | en | placeholders |
|---|---|---|---|
| `search.placeholder` | 在本书中搜索 | Search this book |  |
| `search.running` | 正在搜索… | Searching… |  |
| `search.count.one` | 共 {n} 处 | {n} match | n |
| `search.count.other` | 共 {n} 处 | {n} matches | n |
| `search.capped` | 仅显示前 {n} 处 | Showing the first {n} | n |
| `search.none` | 没有找到「{q}」 | No results for “{q}” | q |
| `search.position` | 第 {i} 处，共 {n} 处 | {i} of {n} | i, n |
| `search.prev` | 上一个结果 | Previous Result |  |
| `search.next` | 下一个结果 | Next Result |  |
| `search.clear` | 清除搜索 | Clear Search |  |
| `search.wrapped.start` | 已回到第一个结果 | Back to the first result |  |
| `search.wrapped.end` | 已跳到最后一个结果 | Jumped to the last result |  |
| `search.context` | …{before}{match}{after}… | …{before}{match}{after}… | after, before, match |

### settings panel

| key | zh-Hans | en | placeholders |
|---|---|---|---|
| `set.title` | 设置 | Settings |  |
| `set.close` | 关闭设置 | Close Settings |  |
| `set.section.theme` | 主题 | Theme |  |
| `set.section.font` | 字体 | Font |  |
| `set.section.layout` | 排版 | Layout |  |
| `set.section.paging` | 翻页 | Page turning |  |
| `set.section.general` | 通用 | General |  |
| `set.font.cjk` | 中文字体 | Chinese font |  |
| `set.font.latin` | 西文字体 | Latin font |  |
| `set.font.book` | 使用书籍自带字体 | Use the book's own fonts |  |
| `set.font.size` | 字号 | Font size |  |
| `set.font.weight` | 字重 | Weight |  |
| `set.weight.normal` | 常规 | Regular |  |
| `set.weight.light` | 细 | Light |  |
| `set.line_height` | 行距 | Line spacing |  |
| `set.para_spacing` | 段间距 | Paragraph spacing |  |
| `set.indent` | 段首缩进 | First-line indent |  |
| `set.indent.none` | 无 | None |  |
| `set.indent.two` | 两字符 | 2 characters |  |
| `set.align` | 对齐 | Alignment |  |
| `set.align.left` | 左对齐 | Left |  |
| `set.align.justify` | 两端对齐 | Justified |  |
| `set.margin` | 页边距 | Margins |  |
| `set.measure` | 单行字数上限 | Maximum line length |  |
| `set.paging.paged` | 分页 | Paginated |  |
| `set.paging.scroll` | 滚动 | Scrolling |  |
| `set.value.px` | {n} px | {n} px | n |
| `set.value.em` | {n} em | {n} em | n |
| `set.value.chars` | {n} 字 | {n} characters | n |
| `set.suffix.px` |  px |  px |  |
| `set.suffix.em` |  em |  em |  |
| `set.suffix.chars` |  字 |  characters |  |
| `set.thisbook` | 仅用于本书 | This book only |  |
| `set.thisbook.tip` | 勾选后，这里的改动只作用于当前这本书 | When checked, changes here apply only to this book |  |
| `set.reset` | 恢复默认 | Restore Defaults |  |
| `set.reset.done` | 已恢复默认设置 | Defaults restored |  |
| `set.restore_last` | 启动时打开上次读的书 | Reopen the last book at startup |  |
| `set.autohide` | 阅读时自动隐藏工具栏 | Hide the toolbar while reading |  |
| `set.zoom_images` | 点击图片放大 | Click an image to enlarge it |  |
| `set.invert_images` | 在夜主题下反转图片颜色 | Invert image colors in the dark theme |  |
| `set.confirm_remove` | 从书架移除前先确认 | Confirm before removing a book from the library |  |
| `set.speed` | 阅读速度 | Reading speed |  |
| `set.speed.value` | 每分钟约 {n} 字 | About {n} words per minute | n |
| `set.speed.hint` | 根据阅读记录自动调整 | Adjusts automatically as you read |  |
| `set.assoc` | 设为默认 EPUB 阅读器 | Set as Default for EPUB Files |  |
| `set.assoc.hint` | Windows 会要求在“默认应用”中确认一次 | Windows asks you to confirm this once in Default apps |  |
| `set.assoc.done` | 已注册。在 Windows 的“默认应用”中选择 {app} 即可完成。 | Registered. Choose {app} in Windows Default apps to finish. | app |
| `set.assoc.failed` | 无法注册文件类型。仍可在 Windows 设置的“默认应用”中选择 {app}。 | Could not register the file type. You can still choose {app} in Windows Settings under Default apps. | app |

### file association (written to the registry)

| key | zh-Hans | en | placeholders |
|---|---|---|---|
| `assoc.type_name` | EPUB 电子书 | EPUB Book |  |
| `assoc.verb` | 使用 {app} 打开 | Open with {app} | app |
| `assoc.description` | EPUB 电子书阅读器 | An EPUB book reader |  |

### status bar

| key | zh-Hans | en | placeholders |
|---|---|---|---|
| `status.percent` | {p}% | {p}% | p |
| `status.left.chapter` | 本章剩余 {time} | {time} left in chapter | time |
| `status.left.book` | 全书剩余 {time} | {time} left in book | time |
| `status.read` | 已读 {time} | Read for {time} | time |
| `status.cell.chapter` | 本章剩余时间 | Time left in chapter |  |
| `status.cell.book` | 全书剩余时间 | Time left in book |  |
| `status.cell.read` | 已读时长 | Time spent reading |  |
| `status.cell.tip` | 右键单击可切换显示内容 | Right-click to change what this shows |  |
| `status.screen` | 本屏 {i}/{n} | Screen {i} of {n} | i, n |
| `status.moved` | 文件已移动，位置已更新 | The file was moved. Its new location has been saved. |  |
| `status.back_to_library` | 已回到书架 | Returned to the library |  |
| `status.copied` | 已复制 | Copied |  |
| `status.copied_cite` | 已复制，并附上出处 | Copied with citation |  |
| `status.no_selection` | 没有选中的文字 | No text is selected |  |
| `status.bookmark.added` | 已添加书签 | Bookmark added |  |
| `status.bookmark.removed` | 已移除书签 | Bookmark removed |  |
| `status.highlight.added` | 已高亮 | Highlighted |  |
| `status.highlight.removed` | 已删除批注 | Highlight deleted |  |
| `status.note.saved` | 已保存笔记 | Note saved |  |
| `status.book_start` | 已在全书开头 | This is the beginning of the book |  |
| `status.book_end` | 已到全书末尾 | This is the end of the book |  |
| `status.font_size` | 字号 {n} px | Font size {n} px | n |
| `status.mode.paged` | 已切换到分页 | Switched to paginated |  |
| `status.mode.scroll` | 已切换到滚动 | Switched to scrolling |  |
| `status.theme` | 主题：{theme} | Theme: {theme} | theme |
| `status.zen.hint` | 专注模式 · 按 Esc 退出 | Focus mode · Press Esc to exit |  |
| `status.fullscreen.hint` | 按 F11 或 Esc 退出全屏 | Press F11 or Esc to exit full screen |  |
| `status.fixed_layout` | 这本书是固定版式，已按窗口缩放显示 | This book has a fixed layout and is scaled to fit the window |  |
| `status.vertical` | 这本书是竖排版式，已改为滚动阅读 | This book uses vertical text, so it opens in scrolling mode |  |
| `status.link.missing` | 链接指向的内容不在这本书中 | This link points to something that is not in the book |  |
| `status.save_failed` | 暂时无法保存阅读记录，稍后会自动重试 | Could not save reading data. It will be tried again shortly. |  |
| `status.drop.unsupported` | 只能打开 EPUB 文件 | Only EPUB files can be opened |  |

### durations (strings.duration builds these)

| key | zh-Hans | en | placeholders |
|---|---|---|---|
| `time.lt_minute` | 不到 1 分钟 | under 1 min |  |
| `time.m` | {m} 分钟 | {m} min | m |
| `time.h` | {h} 小时 | {h} h | h |
| `time.hm` | {h} 小时 {m} 分钟 | {h} h {m} min | h, m |
| `time.lt_minute.long` | 不到 1 分钟 | less than a minute |  |
| `time.minutes.one` | {n} 分钟 | {n} minute | n |
| `time.minutes.other` | {n} 分钟 | {n} minutes | n |
| `time.hours.one` | {n} 小时 | {n} hour | n |
| `time.hours.other` | {n} 小时 | {n} hours | n |
| `time.hours_minutes` | {hours} {minutes} | {hours} {minutes} | hours, minutes |

### dates (strings.format_date builds these)

| key | zh-Hans | en | placeholders |
|---|---|---|---|
| `date.today` | 今天 | Today |  |
| `date.yesterday` | 昨天 | Yesterday |  |
| `date.month_day` | {month}{d}日 | {month} {d} | d, month |
| `date.full` | {y}年{month}{d}日 | {month} {d}, {y} | d, month, y |
| `month.1` | 1月 | Jan |  |
| `month.2` | 2月 | Feb |  |
| `month.3` | 3月 | Mar |  |
| `month.4` | 4月 | Apr |  |
| `month.5` | 5月 | May |  |
| `month.6` | 6月 | Jun |  |
| `month.7` | 7月 | Jul |  |
| `month.8` | 8月 | Aug |  |
| `month.9` | 9月 | Sep |  |
| `month.10` | 10月 | Oct |  |
| `month.11` | 11月 | Nov |  |
| `month.12` | 12月 | Dec |  |

### go-to dialog (Ctrl+G)

| key | zh-Hans | en | placeholders |
|---|---|---|---|
| `goto.title` | 跳转到 | Go To |  |
| `goto.label` | 位置（全书百分比） | Position (percent of book) |  |
| `goto.hint` | 输入 0 到 100 之间的数字 | Enter a number from 0 to 100 |  |
| `goto.go` | 跳转 | Go |  |

### in-page reader

| key | zh-Hans | en | placeholders |
|---|---|---|---|
| `reader.zoom.hint` | 点击任意位置或按 Esc 关闭 | Click anywhere or press Esc to close |  |

### external links

| key | zh-Hans | en | placeholders |
|---|---|---|---|
| `link.external.title` | 在浏览器中打开这个链接？ | Open this link in your browser? |  |
| `link.external.open` | 打开链接 | Open Link |  |

### library: top bar

| key | zh-Hans | en | placeholders |
|---|---|---|---|
| `lib.open` | 打开书籍… | Open Book… |  |
| `lib.add_folder` | 添加文件夹… | Add Folder… |  |
| `lib.search` | 搜索书名、作者 | Search titles and authors |  |
| `lib.search.clear` | 清空搜索 | Clear Search |  |
| `lib.sort` | 排序方式 | Sort by |  |
| `lib.sort.recent` | 最近阅读 | Recently read |  |
| `lib.sort.added` | 添加时间 | Date added |  |
| `lib.sort.title` | 书名 | Title |  |
| `lib.sort.author` | 作者 | Author |  |
| `lib.sort.progress` | 阅读进度 | Progress |  |
| `lib.view.grid` | 封面 | Covers |  |
| `lib.view.list` | 列表 | List |  |
| `lib.count.one` | {n} 本书 | {n} book | n |
| `lib.count.other` | {n} 本书 | {n} books | n |

### library: sections, cards, list

| key | zh-Hans | en | placeholders |
|---|---|---|---|
| `lib.section.continue` | 继续阅读 | Continue Reading |  |
| `lib.section.all` | 全部书籍 | All Books |  |
| `lib.progress` | 已读 {p}% | {p}% read | p |
| `lib.progress.none` | 未开始 | Not started |  |
| `lib.badge.finished` | 已读完 | Finished |  |
| `lib.badge.missing` | 文件缺失 | File missing |  |
| `lib.badge.missing.tip` | 找不到这个文件。打开时会先自动查找。 | This file can't be found. Opening it searches for it first. |  |
| `lib.author.unknown` | 未知作者 | Unknown author |  |
| `lib.title.unknown` | 无标题 | Untitled |  |
| `lib.col.title` | 书名 | Title |  |
| `lib.col.author` | 作者 | Author |  |
| `lib.col.progress` | 进度 | Progress |  |
| `lib.col.added` | 添加时间 | Added |  |
| `lib.col.opened` | 最近阅读 | Last Read |  |
| `lib.never_opened` | 从未打开 | Never |  |

### library: empty and no-match states

| key | zh-Hans | en | placeholders |
|---|---|---|---|
| `lib.empty.title` | 书架是空的 | The library is empty |  |
| `lib.empty.body` | 把 EPUB 文件拖到这里，或者 | Drag EPUB files here, or |  |
| `lib.nomatch` | 没有匹配的书 | No matching books |  |

### library: found-books suggestion card

| key | zh-Hans | en | placeholders |
|---|---|---|---|
| `lib.found.one` | 在 {folder} 中发现 {n} 本书 | Found {n} book in {folder} | folder, n |
| `lib.found.other` | 在 {folder} 中发现 {n} 本书 | Found {n} books in {folder} | folder, n |
| `lib.found.add` | 添加到书架 | Add to Library |  |
| `lib.found.dismiss` | 不用了 | No Thanks |  |

### library: adding and scanning

| key | zh-Hans | en | placeholders |
|---|---|---|---|
| `lib.drop.hint` | 松开以添加到书架 | Drop to add to the library |  |
| `lib.adding.one` | 正在添加 {n} 个文件… | Adding {n} file… | n |
| `lib.adding.other` | 正在添加 {n} 个文件… | Adding {n} files… | n |
| `lib.added.one` | 已添加 {n} 本书 | Added {n} book | n |
| `lib.added.other` | 已添加 {n} 本书 | Added {n} books | n |
| `lib.added.none` | 没有找到 EPUB 文件 | No EPUB files were found |  |
| `lib.add_failed.one` | {n} 个文件无法添加 | {n} file could not be added | n |
| `lib.add_failed.other` | {n} 个文件无法添加 | {n} files could not be added | n |
| `lib.already` | 这本书已在书架上 | This book is already in the library |  |
| `lib.rescan` | 重新扫描 | Rescan |  |
| `lib.rescanning` | 正在扫描文件夹… | Scanning folders… |  |
| `lib.rescan.done.one` | 扫描完成，新增 {n} 本书 | Scan complete. {n} new book. | n |
| `lib.rescan.done.other` | 扫描完成，新增 {n} 本书 | Scan complete. {n} new books. | n |
| `lib.rescan.none` | 扫描完成，没有新书 | Scan complete. No new books. |  |
| `lib.rescan.nofolders` | 还没有添加文件夹 | No folders have been added yet |  |

### library: right-click menu

| key | zh-Hans | en | placeholders |
|---|---|---|---|
| `lib.ctx.open` | 打开 | Open |  |
| `lib.ctx.reveal` | 在文件夹中显示 | Show in Folder |  |
| `lib.ctx.copypath` | 复制文件路径 | Copy File Path |  |
| `lib.ctx.info` | 书籍信息 | Book Details |  |
| `lib.ctx.relocate` | 重新定位文件… | Locate File… |  |
| `lib.ctx.remove` | 从书架移除 | Remove from Library |  |
| `lib.copied_path` | 已复制文件路径 | File path copied |  |

### library: removal

| key | zh-Hans | en | placeholders |
|---|---|---|---|
| `lib.remove.title` | 从书架移除 | Remove from Library |  |
| `lib.remove.confirm` | 从书架移除《{title}》？文件本身不会被删除。 | Remove “{title}” from the library? The file itself will not be deleted. | title |
| `lib.remove.confirm_many.one` | 从书架移除 {n} 本书？文件本身不会被删除。 | Remove {n} book from the library? The file itself will not be deleted. | n |
| `lib.remove.confirm_many.other` | 从书架移除 {n} 本书？文件本身不会被删除。 | Remove {n} books from the library? The files themselves will not be deleted. | n |
| `lib.remove.yes` | 移除 | Remove |  |
| `lib.remove.dont_ask` | 不再询问 | Don't ask again |  |
| `lib.removed` | 已从书架移除《{title}》 | Removed “{title}” from the library | title |
| `lib.removed_many.one` | 已从书架移除 {n} 本书 | Removed {n} book from the library | n |
| `lib.removed_many.other` | 已从书架移除 {n} 本书 | Removed {n} books from the library | n |

### book details dialog

| key | zh-Hans | en | placeholders |
|---|---|---|---|
| `info.title` | 书籍信息 | Book Details |  |
| `info.book` | 书名 | Title |  |
| `info.subtitle` | 副标题 | Subtitle |  |
| `info.authors.one` | 作者 | Author |  |
| `info.authors.other` | 作者 | Authors |  |
| `info.publisher` | 出版社 | Publisher |  |
| `info.pubdate` | 出版日期 | Published |  |
| `info.language` | 语言 | Language |  |
| `info.identifier` | 标识符 | Identifier |  |
| `info.description` | 简介 | Description |  |
| `info.subjects` | 分类 | Subjects |  |
| `info.epub_version` | EPUB 版本 | EPUB version |  |
| `info.layout` | 版式 | Layout |  |
| `info.layout.reflowable` | 流式 | Reflowable |  |
| `info.layout.fixed` | 固定版式 | Fixed layout |  |
| `info.path` | 文件位置 | Location |  |
| `info.size` | 文件大小 | File size |  |
| `info.chapters` | 章节文件数 | Sections |  |
| `info.units` | 字数 | Length |  |
| `info.units.value` | 约 {n} 字 | About {n} words | n |
| `info.added` | 添加时间 | Added |  |
| `info.opened` | 最近阅读 | Last read |  |
| `info.progress` | 阅读进度 | Progress |  |
| `info.reading_time` | 阅读时长 | Time spent reading |  |
| `info.unknown` | 未知 | Unknown |  |
| `info.copy_path` | 复制路径 | Copy Path |  |

### export highlights

| key | zh-Hans | en | placeholders |
|---|---|---|---|
| `export.dialog_title` | 导出批注 | Export Highlights |  |
| `export.filter` | Markdown 文件 (*.md) | Markdown files (*.md) |  |
| `export.default_name` | {title} - 批注 | {title} - Highlights | title |
| `export.done.one` | 已导出 {n} 条批注 | Exported {n} highlight | n |
| `export.done.other` | 已导出 {n} 条批注 | Exported {n} highlights | n |
| `export.empty` | 这本书还没有批注 | This book has no highlights yet |  |
| `export.failed` | 无法写入这个文件： | Could not write this file: |  |
| `export.md.highlights` | 批注 | Highlights |  |
| `export.md.bookmarks` | 书签 | Bookmarks |  |
| `export.md.author` | 作者：{author} | Author: {author} | author |
| `export.md.exported` | 导出于 {date} | Exported on {date} | date |
| `export.md.note` | 笔记：{note} | Note: {note} | note |
| `export.md.lost` | （无法在书中定位） | (location lost) |  |
| `export.md.position` | 位置 {p}% | At {p}% | p |

### copy with citation (Ctrl+Shift+C)

| key | zh-Hans | en | placeholders |
|---|---|---|---|
| `cite.chapter` | {text}\n——《{title}》{chapter} | “{text}”\n— {title}, {chapter} | chapter, text, title |
| `cite.book` | {text}\n——《{title}》 | “{text}”\n— {title} | text, title |

### file dialogs

| key | zh-Hans | en | placeholders |
|---|---|---|---|
| `dlg.open.title` | 打开书籍 | Open Book |  |
| `dlg.open.filter` | EPUB 电子书 (*.epub) | EPUB books (*.epub) |  |
| `dlg.all_files` | 所有文件 (*) | All files (*) |  |
| `dlg.folder.title` | 选择文件夹 | Choose a Folder |  |
| `dlg.relocate.title` | 重新定位《{title}》 | Locate “{title}” | title |

### error cards

| key | zh-Hans | en | placeholders |
|---|---|---|---|
| `err.corrupt.title` | 无法打开这本书 | Can't open this book |  |
| `err.corrupt.body` | 文件可能已损坏，或者不是 EPUB 格式。 | The file may be damaged, or it may not be an EPUB. |  |
| `err.drm.title` | 这本书有 DRM 保护 | This book is protected by DRM |  |
| `err.drm.body` | 检测到 {kind} 加密。{app} 不解密受保护的文件，请用购买它的官方应用打开。 | It uses {kind} encryption. {app} does not decrypt protected files. Open it in the app from the store where you bought it. | app, kind |
| `err.drm.body.unknown` | 检测到无法识别的加密方式。{app} 不解密受保护的文件，请用购买它的官方应用打开。 | It uses an encryption scheme that could not be identified. {app} does not decrypt protected files. Open it in the app from the store where you bought it. | app |
| `err.drm.adept` | Adobe ADEPT | Adobe ADEPT |  |
| `err.drm.lcp` | Readium LCP | Readium LCP |  |
| `err.missing.title` | 找不到这个文件 | Can't find this file |  |
| `err.missing.body` | 《{title}》原来在： | “{title}” was last at: | title |
| `err.missing.searching` | 正在查找文件… | Looking for the file… |  |
| `err.structure.title` | 这个 EPUB 结构不完整 | This EPUB is incomplete |  |
| `err.structure.body` | 缺少 META-INF/container.xml，无法确定书籍内容。 | META-INF/container.xml is missing, so the book's contents can't be determined. |  |
| `err.structure.body.opf` | 无法读取书籍的内容清单（OPF 文件），无法确定书籍内容。 | The book's package file (OPF) can't be read, so its contents can't be determined. |  |
| `err.toolarge.title` | 这个文件太大 | This file is too large |  |
| `err.toolarge.body` | 解压后的内容超出了安全上限，{app} 不会打开它。 | Its uncompressed contents exceed the safety limit, so {app} will not open it. | app |
| `err.access.title` | 无法读取这个文件 | Can't read this file |  |
| `err.access.body` | 文件可能正被其他程序占用，或者不允许读取。 | Another program may be using it, or reading it may not be permitted. |  |
| `err.unexpected.body` | 打开时发生了意外错误，技术细节中有具体原因。 | Something unexpected happened while opening it. The technical details show the cause. |  |
| `err.section` | 本节内容无法显示 | This section can't be displayed |  |
| `err.details` | 技术细节 | Technical Details |  |
| `err.details.copy` | 复制技术细节 | Copy Details |  |
| `err.btn.reveal` | 在文件夹中显示 | Show in Folder |  |
| `err.btn.relocate` | 重新定位… | Locate… |  |
| `err.btn.remove` | 从书架移除 | Remove from Library |  |
| `err.btn.close` | 关闭 | Close |  |
| `err.relocate.mismatch` | 这似乎是另一个文件，仍要关联吗？ | This looks like a different file. Link it anyway? |  |
| `err.relocate.yes` | 仍要关联 | Link Anyway |  |
| `err.relocate.no` | 取消 | Cancel |  |
| `err.settings.readonly` | 配置文件由更新版本的 {app} 写入，本次运行不会保存改动。 | The settings were saved by a newer version of {app}, so changes made now will not be saved. | app |
| `err.settings.reset` | 配置文件已损坏，已备份并恢复默认设置。 | The settings file was damaged. A copy was kept and the defaults were restored. |  |
| `err.book_state.reset` | 这本书的阅读记录已损坏，已备份并重新开始记录。 | This book's reading data was damaged. A copy was kept and a new record was started. |  |
| `err.crash.title` | {app} 遇到了内部错误 | {app} ran into an internal error | app |
| `err.crash.body` | 日志保存在： | The log is saved at: |  |
| `err.crash.open_log` | 打开日志文件夹 | Open Log Folder |  |

### command line

| key | zh-Hans | en | placeholders |
|---|---|---|---|
| `cli.usage` | 用法：{exe} [EPUB 文件] | Usage: {exe} [EPUB file] | exe |
| `cli.description` | 打开 EPUB 电子书；不带参数时显示书架。 | Opens an EPUB book, or the library when no file is given. |  |
| `cli.help` | 显示此帮助并退出 | Show this help and exit |  |
| `cli.not_found` | 找不到文件：{path} | File not found: {path} | path |

### keyboard shortcuts overlay (F1)

| key | zh-Hans | en | placeholders |
|---|---|---|---|
| `keys.title` | 快捷键 | Keyboard Shortcuts |  |
| `keys.close` | 按 Esc 关闭 | Press Esc to close |  |
| `keys.note.letters` | 单字母快捷键仅在正文获得焦点时有效 | Single-letter shortcuts work only while the book has focus |  |
| `keys.group.reading` | 阅读 | Reading |  |
| `keys.group.panels` | 面板 | Panels |  |
| `keys.group.actions` | 操作 | Actions |  |
| `keys.group.window` | 窗口与应用 | Window and App |  |
| `keys.group.library` | 书架 | Library |  |
| `keys.next_page` | 下一页 | Next page |  |
| `keys.prev_page` | 上一页 | Previous page |  |
| `keys.page_jk` | 下一页 / 上一页 | Next page / previous page |  |
| `keys.next_chapter` | 下一章 | Next chapter |  |
| `keys.prev_chapter` | 上一章 | Previous chapter |  |
| `keys.chapter_edge` | 本章开头 / 本章末尾 | Start / end of chapter |  |
| `keys.book_edge` | 全书开头 / 全书末尾 | Start / end of book |  |
| `keys.jump_history` | 跳转后退 / 跳转前进 | Back / forward through jumps |  |
| `keys.goto` | 跳转到… | Go to… |  |
| `keys.find_next` | 下一个搜索结果 | Next search result |  |
| `keys.find_prev` | 上一个搜索结果 | Previous search result |  |
| `keys.toc` | 目录 | Contents |  |
| `keys.bookmarks` | 书签列表 | Bookmarks |  |
| `keys.notes` | 批注列表 | Highlights |  |
| `keys.search` | 在本书中搜索 | Search this book |  |
| `keys.typography` | 排版设置 | Text and layout settings |  |
| `keys.escape` | 关闭面板、退出专注模式或全屏、取消选择 | Close a panel, leave focus mode or full screen, clear the selection |  |
| `keys.cheatsheet` | 快捷键一览 | Keyboard shortcuts |  |
| `keys.bookmark_toggle` | 添加或移除书签 | Add or remove a bookmark |  |
| `keys.copy` | 复制所选文字 | Copy the selection |  |
| `keys.copy_cite` | 复制所选文字并附出处 | Copy the selection with a citation |  |
| `keys.highlight` | 用黄 / 绿 / 蓝 / 粉高亮所选文字 | Highlight the selection in yellow / green / blue / pink |  |
| `keys.highlight_delete` | 删除选中的批注 | Delete the selected highlight |  |
| `keys.toggle_mode` | 切换分页 / 滚动 | Switch between paginated and scrolling |  |
| `keys.font_step` | 增大 / 减小字号，也可用 Ctrl+滚轮 | Larger / smaller text (Ctrl+mouse wheel also works) |  |
| `keys.font_reset` | 恢复默认字号 | Reset the font size |  |
| `keys.toggle_daynight` | 在日 / 夜主题之间切换 | Switch between light and dark themes |  |
| `keys.fullscreen` | 全屏 | Full screen |  |
| `keys.zen` | 专注模式 | Focus mode |  |
| `keys.open` | 打开书籍… | Open a book… |  |
| `keys.library` | 回到书架 | Back to the library |  |
| `keys.close_book` | 关闭当前书并回到书架；在书架时关闭窗口 | Close the book and return to the library; in the library, close the window |  |
| `keys.quit` | 退出 | Exit |  |
| `keys.lib_move` | 移动选择 | Move the selection |  |
| `keys.lib_open` | 打开所选书籍 | Open the selected book |  |
| `keys.lib_focus_search` | 聚焦搜索框 | Go to the search box |  |
| `keys.lib_clear` | 清空搜索 / 取消选择 | Clear the search / the selection |  |
| `keys.lib_remove` | 从书架移除（不删除文件） | Remove from the library (the file is kept) |  |
| `keys.lib_open_book` | 打开书籍… | Open a book… |  |
| `keys.lib_rescan` | 重新扫描已添加的文件夹 | Rescan added folders |  |

### about

| key | zh-Hans | en | placeholders |
|---|---|---|---|
| `about.title` | 关于 {app} | About {app} | app |
| `about.tagline` | 一个安静的 EPUB 阅读器 | A quiet EPUB reader |  |
| `about.version` | 版本 {version} | Version {version} | version |
| `about.built` | 基于 Python {py} 与 Qt {qt} | Built with Python {py} and Qt {qt} | py, qt |
| `about.storage` | 书架与批注保存在： | Your library and highlights are stored in: |  |
| `about.cache` | 封面缓存保存在： | Cover images are cached in: |  |
| `about.logs` | 日志保存在： | Logs are stored in: |  |
| `about.open_folder` | 打开文件夹 | Open Folder |  |

### font family display names

| key | zh-Hans | en | placeholders |
|---|---|---|---|
| `font.microsoft_yahei` | 微软雅黑 | Microsoft YaHei |  |
| `font.simsun` | 宋体 | SimSun |  |
| `font.nsimsun` | 新宋体 | NSimSun |  |
| `font.simhei` | 黑体 | SimHei |  |
| `font.kaiti` | 楷体 | KaiTi |  |
| `font.fangsong` | 仿宋 | FangSong |  |
| `font.stkaiti` | 华文楷体 | STKaiti |  |
| `font.stfangsong` | 华文仿宋 | STFangsong |  |
| `font.dengxian` | 等线 | DengXian |  |
| `font.microsoft_jhenghei` | 微软正黑体 | Microsoft JhengHei |  |

<!-- END KEY CATALOG -->
