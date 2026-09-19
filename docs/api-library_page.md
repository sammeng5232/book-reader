# `library_page.py` — API (owner F)

The shelf: CONTRACT §7 and product-spec §3 "LIBRARY / START SCREEN". Books are referenced in place.
The page never deletes, moves, renames, copies or modifies a book file, and no menu or key offers to.
It persists only through `store.Store`, and all text comes from `strings.S()`. The page and every
dialog implement `retranslate_ui()`, and the page subscribes to `strings.language_changed`, so a UI
language change applies immediately.

Tests: `tests/test_library_page.py` has 29 tests. Each uses a temporary Store root and shows no window.
The real-book test is skipped when the books are absent.

```
python -m unittest tests.test_library_page -v
```

## Wiring it in (owner G)

```python
from library_page import LibraryPage

library = LibraryPage(store, theme=ctl.theme)          # reads the shelf, starts cover workers
stack.addWidget(library)
ctl.themeChanged.connect(library.apply_theme)          # the painted shelf follows the theme
library.openBook.connect(reader.open_book)             # path of the book to open
library.removeBook.connect(lambda bid: None)           # already removed from the Store; see below
library.statusMessage.connect(lambda text: None)       # optional: mirror notices elsewhere
# Ctrl+O is yours. Bind it centrally and call library.open_book_dialog() while the shelf is current.
```

* `aboutToQuit` is connected to `shutdown()` automatically.
* The page re-reads the Store whenever it is shown again, for example after the reader closed a book.
  Call `refresh()` for any other change.
* Keys. The page handles its own keys and claims them at `ShortcutOverride`, so an application-wide
  `QShortcut` cannot take them while the shelf is visible: arrows, Home/End, PageUp/PageDown, Enter,
  Delete, Escape, F5, `/` and Ctrl+F. It does not claim Ctrl+O, but it handles Ctrl+O when the key
  reaches it.

## `class LibraryPage(QWidget)`

`LibraryPage(store: Store, parent: QWidget | None = None, *, theme: Theme | None = None)`. Without
`theme`, the page resolves `reader.theme` from the Store.

### Signals

| Signal | When |
|---|---|
| `openBook(str path)` | double-click, Enter, the menu's Open, or a single file added by dialog or drop (after it is on the shelf). A single file that could not be shelved (corrupt, not an EPUB, unreadable) is still handed over, so the reader can show the matching error card with its technical details. Missing files emit it too: the reader looks for moved files. |
| `removeBook(str book_id)` | emitted **after** the page confirmed and called `Store.library_remove`. The `.epub` and `books/<id>.json` are kept. |
| `statusMessage(str text)` | every notice the page shows (added / removed / copied / scan results). |

```python
library.openBook.connect(lambda path: reader.open_book(path))
```

### Content and state

| Member | Notes |
|---|---|
| `refresh()` | re-read `store.library()`, re-sort, re-filter, queue missing-file checks and covers |
| `entries() -> list[dict]` | shallow copies of the shelf entries as last read |
| `selected_book_id() -> str` / `select_book(book_id) -> bool` | selection (prefers the card in 全部书籍) |
| `open_book(book_id)` / `open_selected()` | emit `openBook` for an entry, or for the selection (else the first card) |
| `cover_state(book_id) -> str` | `pending`, `image`, `none` (generated cover), `missing`, `error`, or `''` |
| `is_busy() -> bool` | an add, scan or rescan job is running |
| `wait_for_workers(timeout_ms=30000) -> bool` | block until the pool is idle (tests only) |
| `shutdown(timeout_ms=3000)` | cancel queued jobs and wait for the running ones (on quit) |
| `notice_text() -> str` | the notice currently shown, or `''` |
| `theme()` / `apply_theme(theme)` | the current `theme.Theme` / repaint the shelf, generated covers, overlay and icons |
| `retranslate_ui()` | re-render every string (subscribed to `language_changed`) |
| `compact_level() -> int` | the top bar's current level: 0 all labels, 1 icon-only view toggle, 2 icon-only Open/Add Folder |
| `reveal_handler: Callable[[str], Any]` | replaceable. The default shows the file selected in Explorer. |

```python
library.select_book(bid); library.open_selected()
```

### Top bar, search, sort, view

| Member | Notes |
|---|---|
| `open_book_dialog()` | Ctrl+O / 打开书籍…. A file dialog (`dlg.open.*`). One chosen file is added and then opened. |
| `add_folder_dialog()` / `add_folder(folder)` | 添加文件夹…. The folder's EPUBs are added recursively, and the folder is remembered for F5. |
| `add_paths(paths, *, open_single=True) -> int` | Files and/or folders, processed in a worker. Returns a job token (`0` when nothing was started). |
| `focus_search()` | Ctrl+F or `/` |
| `set_search(text)` / `clear_search_or_selection()` | Escape clears the search first, then the selection |
| `set_sort(key)` / `sort_key()` | one of `SORT_KEYS`, saved to `window.library_sort` |
| `set_view_mode(mode)` / `view_mode()` | `'grid'` or `'list'`, saved to `window.library_view` |
| `rescan() -> int` | F5. New EPUBs in the watched folders are added. Missing flags are re-checked. |
| `watched_folders() -> list[str]` | `behavior.watch_folders` |

```python
library.add_paths([r"C:\Books\新书.epub"])          # added, then openBook(path)
library.add_paths([r"C:\Books"], open_single=False)  # recursive; C:\Books is watched for F5
```

What an add does, in a `QThreadPool` worker:

* Each file is hashed with `Store.book_id_for`. A file whose path, size and mtime are already on the
  shelf is not hashed again.
* Each new book is opened read-only with `EpubBook` for its metadata, and its cover thumbnail is
  written on the way.
* A DRM book is still added: its title is the file name and `drm` is set, so opening it shows the
  reader's DRM card.
* Corrupt or non-EPUB files are counted in 「N 个文件无法添加」.
* A book already on the shelf is 「这本书已在书架上」.
* A shelved book that went missing and turns up at a new path (same hash) is repaired: `path` is
  updated, the old path is appended to `path_history`, and 「文件已移动，位置已更新」 is shown.
* A "working" notice appears only if the job takes longer than 300 ms.

### Found-books suggestion card

| Member | Notes |
|---|---|
| `offer_found_books(folder, paths) -> bool` | Shows 「在 X 中发现 N 本书」 [添加到书架] [不用了]. Returns False when nothing new is left to offer, or the folder is watched or was dismissed. |
| `suggestion() -> (folder, paths) \| None` | the card's current content |

```python
library.offer_found_books(r"C:\Users\me\Desktop\文件", candidates)   # e.g. a first-run offer from G
```

The page never walks the disk on its own. The only listing it makes is a non-recursive
`os.scandir` of the folder that holds a single file the user has just opened or dropped. Nothing is
added until 添加到书架 is pressed. 不用了 stores the folder in `behavior.found_books_dismissed`, and
that folder is never offered again.

### Context menu and actions

| Member | Notes |
|---|---|
| `build_context_menu(book_id) -> QMenu` | 打开 · 在文件夹中显示 · 复制文件路径 · 书籍信息 · ─ · 从书架移除. There is no delete-file item. Key hints (Enter, Delete) are display text only, so they cannot fire inside the menu. |
| `show_context_menu(book_id, global_pos)` | `exec()`s that menu |
| `reveal_in_folder(book_id)` | Explorer `/select`. For a missing file it opens the old folder if that still exists (otherwise the menu item is disabled). |
| `copy_path(book_id) -> str` | clipboard, then 「已复制文件路径」 |
| `show_book_info(book_id) -> BookInfoDialog` | non-modal. The description and subjects are read in a worker. |
| `remove_book(book_id, *, confirm=True) -> bool` / `remove_selected()` | Delete key. Confirmation honours `behavior.confirm_remove_from_shelf` and its 不再询问 box. The notice offers 撤销, which re-upserts the saved entry. |

```python
menu = library.build_context_menu(bid); menu.exec(QCursor.pos())
```

## `class ShelfView(QAbstractScrollArea)`

The painted shelf. In grid mode it shows 继续阅读 (up to 6 books with 0 < progress < 1, most
recently opened first, omitted when empty or while searching) and then 全部书籍 with its count. Both
use the same centred column grid:

* 160x240 covers, gaps of 28 to 48 px, rows 30 px apart.
* Titles take at most 2 lines and are elided with QTextLayout, which breaks lines correctly for CJK.
* Authors take 1 line in the secondary colour.
* A 3 px progress bar sits flush inside the cover bottom, drawn only when progress > 0.
* A missing file is drawn at 55% opacity with a full-opacity 「文件缺失」 badge.
* The selection is a 2 px accent ring.

List mode has 64 px rows with a 32x48 cover and the columns 书名/作者 · 进度 · 最近阅读 · 添加时间
(添加时间 is hidden below 780 px). It has no 继续阅读 section.

| Member | Notes |
|---|---|
| `activated(str)`, `contextRequested(str, QPoint)`, `removeRequested(str)`, `selectionChanged(str)` | signals (book id) |
| `set_books(continue_books, books, *, query_active)` / `set_mode(mode)` / `mode()` | fed by the page |
| `set_theme(theme)` / `retranslate_ui()` | |
| `covers: dict[str, QPixmap]` | cover pixmaps by book id (the page fills it) |
| `slots()`, `headers()`, `grid_metrics() -> (cols, gap, x0)`, `content_height()` | layout, read-only |
| `select_book(bid)`, `select_index(i)`, `selected_index()`, `selected_book_id()`, `clear_selection()`, `ensure_visible(i)` | selection |
| `index_at(viewport_pos) -> int`, `slot_viewport_rect(i) -> QRect`, `invalidate_book(bid)` | hit testing, repaint |

```python
cols, gap, x0 = library.view.grid_metrics()      # 6 columns at 1280 px, 3 at 720 px
```

Keys: the arrows move between cards (Up and Down go to the nearest card in the row above or below,
across sections). Home, End, PageUp and PageDown also move. Enter emits `activated`, Delete emits
`removeRequested`, and the Menu key opens the context menu. The layout always reserves the
scrollbar's width, so the column count never flips when the scrollbar appears.

## Dialogs

`ConfirmRemoveDialog(title, parent)`: 「从书架移除《X》？文件本身不会被删除。」, with a `dont_ask`
check box, `cancel_btn` and `remove_btn` (primary).

```python
dlg = ConfirmRemoveDialog("红楼梦", page); ok = dlg.exec() == QDialog.DialogCode.Accepted
```

`BookInfoDialog(entry, parent, *, seconds_read=0)`: a non-modal dialog, 560 px wide, whose height
fits its content (the path wraps and is never clipped). It shows the title and authors, then
publisher, date, language (native name), identifier, EPUB version, layout, sections, length, file
size (`QLocale.formattedDataSize`), dates, progress, reading time, subjects and path. The description
sits in a bounded scroll box. Members: `rows()`, `description()`, `set_details(info)`, the
`copyPathRequested(str)` signal, and `retranslate_ui()`.

```python
dlg = library.show_book_info(bid); dict(dlg.rows())["文件位置"]
```

## Covers

| Function | Notes |
|---|---|
| `cover_hue(book_id) -> int` | `int(book_id[:8], 16) % 360` (CRC-32 for a non-hex id). Never the salted builtin `hash()`. |
| `generated_cover_colors(book_id, theme) -> {bg, rule, title, author}` | Flat background at a fixed low saturation per theme. Text lightness is walked until it reaches at least 7:1 (title) and 4.5:1 (author) at every hue. Verified minima over all 360 hues: light 8.09/4.67, sepia 7.16/4.62, dark 7.33/5.05. |
| `paint_generated_cover(painter, rect, title, author, book_id, theme, *, language="")` | The title is centred in a CJK font chosen by the **book's** language (YaHei, JhengHei or Yu Gothic). It shrinks to fit up to 6 lines and is balanced, so no single character is orphaned. A short rule follows, then the author small at the bottom. Rects under 80 px wide show just the first character. |
| `make_thumbnail(data, dest, *, media_type="") -> (w, h)` | Pillow: EXIF orientation applied, alpha flattened on white, CMYK/P converted to RGB. SVG and svgz go through QtSvg first. LANCZOS to 360 px wide with the long edge ≤ 720, JPEG q86, written to a temp file then `os.replace`. |
| `fit_cover_image(img, box_w, box_h, dpr=1.0) -> QImage` | Centre-crop when the aspect is within 10% of 2:3, otherwise letterbox on the cover's own edge colour. Safe in a worker. |
| `read_library_entry(path, book_id, *, cover_file=None) -> dict` | the shelf entry for one file (see the fields below) |

```python
w, h = make_thumbnail(book.cover_bytes(), store.cover_path(bid), media_type=book.media_type(book.cover))
```

Entry fields written on add:

* `id`, `path`, `size`, `mtime_ns`, `verified_at`, `missing`, `drm`
* `title`, `authors`, `publisher`, `pubdate`, `language`, `identifier`
* `epub_version`, `layout` (`reflowable`/`fixed`), `toc_source` (`toc`/`spine`), `spine_count`, `units_total`
* `cover` (`covers/<id>.jpg`, or `""` meaning the book was checked and has no cover), `cover_w`, `cover_h`

The Store adds `added_at`, `progress`, `path_history` and the other defaults.

## Sorting, filtering, files

| Function | Notes |
|---|---|
| `sort_entries(entries, key, lang=None)` | `recent`: last opened first, then never-opened by newest added. `added`: newest first. `progress`: furthest first. `title`/`author`: QCollator for the UI language, so pinyin for zh-Hans **and en** (en_US orders CJK by code point), stroke order for zh-Hant, and Japanese order for ja. Latin A-Z comes before CJK, digits sort numerically (Book 9 < Book 10), leading 《》“” are ignored, and untitled or unknown sorts last. |
| `filter_entries(entries, query)` | every whitespace-separated term must occur in the title, subtitle, authors or file-name stem. NFKC and casefold, so `ＥＰＵＢ` matches `EPUB`. |
| `continue_entries(entries, limit=6)` | 0 < progress < 1, not `finished_at`, most recently opened first |
| `collect_epubs(paths, *, cancel=None) -> (epubs, unsupported)` | Recursive and de-duplicated. Hidden folders and the recycle bin are skipped. Nothing is opened. |
| `display_title(entry)`, `display_authors(entry)`, `book_progress(entry)`, `is_epub_path(path)` | helpers |
| `wrap_text(text, font, width, max_lines) -> (lines, elided)` | QTextLayout wrapping. The last line carries the elided remainder. |

```python
books = sort_entries(filter_entries(store.library(), "樊纲"), "title", "zh-Hans")
```

## Constants

`COVER_SIZE = (160, 240)`, `THUMB_WIDTH = 360`, `THUMB_MAX_EDGE = 720`, `THUMB_JPEG_QUALITY = 86`,
`SORT_KEYS = ("recent", "added", "title", "author", "progress")`, `SORT_LABEL_KEYS` (the key →
`lib.sort.*` string key), `VIEW_MODES = ("grid", "list")`, `CONTINUE_LIMIT = 6`,
`MISSING_OPACITY = 0.55`, `PROGRESS_BAR_PX = 3`.

Settings keys: `SETTING_VIEW = "window.library_view"`, `SETTING_SORT = "window.library_sort"`,
`SETTING_FOLDERS = "behavior.watch_folders"`,
`SETTING_CONFIRM_REMOVE = "behavior.confirm_remove_from_shelf"`, and
`SETTING_FOUND_DISMISSED = "behavior.found_books_dismissed"` (new: a list of folder paths; `Store.set`
creates it).

```python
if store.get(SETTING_CONFIRM_REMOVE, True): ...
```

## Measured on this machine

| Case | Result |
|---|---|
| Seeding 11 valid fixtures, 5 corrupt ones, the 3 real books and 11 synthetic books through `add_paths` | 2.0 s in a worker, including hashing, metadata, `total_units` and 10 thumbnails. 「已添加 25 本书 · 5 个文件无法添加」 |
| 800 shelf entries | refresh 7 ms, title sort 16 ms, search 5 ms, full repaint 23 ms (79 ms the first time a screen of generated covers is laid out, then cached) |
| Real-book thumbnails | 50人的二十年 720x1280 → 360x640 (letterboxed on the shelf). 从此岸到彼岸 575x800 → 360x501 (EXIF orientation applied). 李向科 570x800 → 360x505. |
