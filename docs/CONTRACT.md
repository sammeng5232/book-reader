# Book Reader — module contract (authoritative)

> **Read `docs/DECISIONS.md` too.** The user named the app **Book Reader** and asked for a four-language UI
> (简体中文 / 繁體中文 / English / 日本語). The working name `Verso` from the research phase is RETIRED —
> it must not appear in any user-visible string, filesystem path, registry key, pipe name or exe name.

Every implementer MUST follow this file. Where this file and a research doc disagree, THIS FILE WINS.
Research lives in `docs/research/*.md` (six verified reports) and reference implementations in
`research/`, `_proto/`, `_proto2/`. Those are *sources to lift from*, not files to ship.

## 0. Ground rules

- Python 3.14.6 at `C:\Users\mengz\AppData\Local\Programs\Python\Python314\python.exe` (NOT `python` on PATH).
- Allowed imports: Python stdlib, PySide6, Pillow, lxml. **No pip installs. No new dependencies. Ever.**
- Identity (see DECISIONS.md §1): display name `Book Reader`; exe `Book Reader.exe`; state `%APPDATA%\Book Reader\`;
  cache `%LOCALAPPDATA%\Book Reader\cache\`; ProgId `BookReader.Book.1`; pipe `book-reader-single-instance`.
  Define these ONCE as constants in `store.py` (`APP_DIR_NAME`, `PROG_ID`, `PIPE_NAME`) and import them everywhere.
- Internal code names: JS namespace `window.epubReader`, bridge `window.epubReaderHost`, CSS layer/class/data-attr
  prefix `er-`. Any leftover `verso` identifier is a bug.
- UI copy comes from `strings.py` only. No literal user-facing text anywhere else in the codebase.
- Files are flat at the project root (the test suite does `from epublib import EpubBook`).
- Every module: type hints on public functions, a module docstring, `__all__`, and no import-time side effects
  except `webhost.register_epub_scheme()` which is documented to be import-time.
- Windows path rule: **zip entry names are posix paths.** Use `posixpath`, never `os.path`, on anything from a zip.

## 1. File ownership (do not write outside your own files)

| Owner | Files |
|---|---|
| A | `epublib.py` |
| B | `store.py`, `theme.py` |
| I | `strings.py`, `i18n/__init__.py`, `i18n/zh_Hans.py`, `i18n/zh_Hant.py`, `i18n/en.py`, `i18n/ja.py` |
| C | `assets/reader.js`, `assets/reader.css` |
| D | `webhost.py` |
| E | `reader_page.py` |
| F | `library_page.py` |
| G | `epub_reader.py`, `run.ps1` |

**Partial work exists.** A previous build attempt was cut off by a usage limit. `epublib.py` (156/156 tests pass),
`store.py`, `strings.py`, `webhost.py`, `assets/reader.js` and `assets/reader.css` are on disk, import cleanly and
are not truncated — but most were NOT fully verified. If you own one: read it, verify it, finish it. Do not rewrite
working code from scratch.

**New UI strings protocol.** Owners E/F/G must use existing string keys. If a key is genuinely missing, do NOT edit
`i18n/`; append it with all four translations to `docs/new-keys-<owner>.md` (your own file). The assembler merges them.

`tests/`, `docs/`, `build_exe.ps1`, `tools/` already exist — do not rewrite them; you may ADD a file under
`docs/api-<module>.md` (yours only) and, if you own tests for your module, `tests/test_<module>.py`.

## 2. `epublib.py` (owner A) — pinned by `tests/test_epub_parsing.py`

```python
class EpubError(Exception):
    kind: str   # 'not_epub' | 'corrupt' | 'drm' | 'no_container' | 'bad_opf' | 'too_large'
    detail: str # technical text for the 技术细节 disclosure; never shown by default
    drm_scheme: str | None  # 'adept' | 'lcp' | 'unknown' when kind == 'drm'

@dataclass(frozen=True)
class SpineItem:  id: str; href: str; media_type: str; linear: bool; zip_name: str
@dataclass(frozen=True)
class TocEntry:   title: str; href: str; fragment: str; zip_name: str; children: list["TocEntry"]

class EpubBook:
    @classmethod
    def open(cls, path: str | os.PathLike) -> "EpubBook": ...
    def close(self) -> None: ...
    def __enter__/__exit__                       # context manager

    path: str
    opf_dir: str                                  # posix, '' when the OPF is at the zip root
    metadata: dict                                # keys: title, authors(list[str]), language, identifier,
                                                  #       publisher, date, description, subjects(list[str])
    spine: list[SpineItem]                        # linear=False items INCLUDED, in document order
    toc: list[TocEntry]                           # nested; from nav.xhtml, else NCX, else spine fallback
    toc_is_synthetic: bool                        # True when built from spine (UI shows a note)
    cover: str | None                             # zip entry name
    is_fixed_layout: bool
    page_direction: str                           # 'ltr' | 'rtl'
    warnings: list[str]                           # non-fatal problems, for logs only

    def read(self, zip_name: str) -> bytes        # font de-obfuscation applied transparently
    def read_text(self, zip_name: str) -> str     # BOM-aware, UTF-8 first. NEVER lets lxml guess.
    def resolve(self, href: str, base: str = "") -> tuple[str, str]   # -> (zip_name, fragment)
    def has(self, zip_name: str) -> bool
    def plain_text(self, zip_name: str) -> str    # SEE §2.1 — cross-engine invariant
    def doc_title(self, zip_name: str) -> str
    def spine_index(self, zip_name: str) -> int | None
    def units(self, zip_name: str) -> int         # CJK chars + Latin words
    def total_units(self) -> int
    def cover_bytes(self) -> bytes | None
    def search(self, query: str, limit: int = 2000) -> list[SearchHit]
```

```python
@dataclass(frozen=True)
class SearchHit:
    spine_index: int; zip_name: str; gpos: int; length: int
    before: str; match: str; after: str        # ~40 chars of context each side, for the result list
    chapter_title: str
```

### 2.1 THE CROSS-ENGINE INVARIANT (highest-risk rule in the project)

`epublib.plain_text(zip_name)` MUST return **character-for-character the same string** that
`assets/reader.js`'s flattened-text walker produces for the same document in Chromium. A `gpos`
(global character offset) computed in Python is handed to JavaScript to scroll to, and vice versa.
Both sides therefore MUST:

1. Walk text nodes in document order.
2. Reject text inside `<script>`, `<style>`, `<noscript>`, and anything `display:none` is **NOT** excluded
   (Python cannot see computed style — so JS must not exclude it either).
3. Concatenate raw `textContent` with **no** separators, no whitespace collapsing, no trimming.
4. Resolve character entities (`&amp;` → `&`, `&#8212;` → `—`, `&nbsp;` → U+00A0) identically.
5. Count in Python `str` code points (JS uses UTF-16; owners A and C must jointly verify that books with
   astral-plane characters, e.g. rare CJK ext-B, still agree — if they cannot, document the limit).

Owners A and C must each write a check for this, and the verification phase tests it in a live
QWebEngineView against all fixtures **and** the user's three real books. If it disagrees, positions,
search jumps and highlights are all silently wrong.

### 2.2 Non-negotiables for A
- Decode before parse (never hand raw bytes to lxml — verified mojibake bug on the user's own book).
- `zipfile.ZipFile(path, metadata_encoding=...)` fallback for cp437-mojibake CJK entry names.
- Font obfuscation (IDPF `http://www.idpf.org/2008/embedding` and Adobe `ns.adobe.com/pdf/enc#RC`) is
  **NOT DRM** — de-obfuscate and open the book normally.
- NCX `playOrder` may be hexadecimal; **document order is authoritative**, never sort by playOrder.
- Zip-bomb guard on `infolist()` metadata before reading anything.
- One bad spine document must never fail the whole book (raise nothing; record in `warnings`).

## 3. `store.py` + `theme.py` (owner B), `strings.py` + `i18n/` (owner I)

`store.py`: JSON under `%APPDATA%\Book Reader\` from `os.environ['APPDATA']` (NOT QStandardPaths), atomic
`os.replace` writes, `.bak` generation, corrupt-file quarantine, integer `schema` + migrations, one
serialized writer thread, debounce policy from the product spec (highlights/bookmarks flush IMMEDIATELY).
Lift the verified implementation from `docs/research/product-spec.md`.

```python
class Store:
    def __init__(self, root: str | None = None) -> None
    settings: Settings                  # attribute-style dotted get/set, debounced 500ms
    def get(self, dotted: str, default=None); def set(self, dotted: str, value) -> None
    def library(self) -> list[dict]; def library_upsert(self, entry: dict) -> None
    def library_remove(self, book_id: str) -> None; def library_get(self, book_id: str) -> dict | None
    def book_state(self, book_id: str) -> dict      # position, bookmarks, highlights, overrides, stats
    def save_book_state(self, book_id: str, state: dict, *, immediate: bool = False) -> None
    def reader_settings(self, book_id: str | None) -> dict    # global merged with per-book overrides
    def cover_path(self, book_id: str) -> str       # %LOCALAPPDATA%\Book Reader\cache\covers\<id>.jpg
    def book_id_for(self, path: str) -> str         # blake2b-128, cached on (path,size,mtime_ns)
    def flush(self) -> None                         # blocking; called on quit
```

`strings.py` + `i18n/` (owner I):
```python
LANGUAGES = ("zh-Hans", "zh-Hant", "en", "ja")
LANGUAGE_NAMES = {"zh-Hans": "简体中文", "zh-Hant": "繁體中文", "en": "English", "ja": "日本語"}  # endonyms, never translated
APP_DISPLAY_NAME = "Book Reader"
def S(key: str, **fmt) -> str            # missing key raises under __debug__, returns the key in a frozen build
def current_language() -> str
def set_language(lang: str) -> None      # 'auto' resolves via QLocale.system(); emits language_changed
def resolve_auto(locale_name: str) -> str  # zh_CN/zh_SG->zh-Hans, zh_TW/zh_HK/zh_MO->zh-Hant, ja*->ja, else en
language_changed: a callable registry (subscribe(fn)/unsubscribe(fn)) — pure Python, no Qt import in strings.py
```
One module per language under `i18n/`, each exporting `TABLE: dict[str, str]`, identical key sets, no gaps. A
checker (`python strings.py --check`) asserts parity and that every `{placeholder}` set matches across languages.
Translations are idiomatic per DECISIONS.md §2 (zh-Hant uses Taiwan software terms, ja uses Japanese UI convention).
Default `ui.language` = `'auto'`. Tone rules in every language: no exclamation marks, verb-first, no emoji.

`theme.py`: Qt-side palette + QSS for 日/纸/夜, `resolve_theme(name) -> Theme`, system-dark detection,
and the token values (bg, fg, secondary, accent, border) that `reader.css` must match so the chrome and
the page are the same colour. Export them as a dict so owner C's CSS variables can be fed from here.

## 4. `assets/reader.js` + `reader.css` (owner C)

Lift the verified engine from `docs/research/reading-ux-engine.md` + `_proto2/reader.js`. Public surface,
exposed as `window.epubReader` and callable via `runJavaScript`; every function must be **idempotent** and safe
to call before `DOMContentLoaded` (queue until ready):

```js
epubReader.init(config)                  // {settings, locator|null, mode:'paginated'|'scroll'}
epubReader.applySettings(settings)       // live; must preserve reading position across the change
epubReader.state()                       // {page, pages, gpos, percent, chapterPercent, mode, fixedLayout}
epubReader.nextPage() / prevPage() / gotoPage(n) / gotoPercent(p)
epubReader.capture()                     // -> locator {gpos, snippet, path, page, percent}
epubReader.restore(locator)              // -> true|false
epubReader.gotoFragment(id)              // anchor within the document
epubReader.flatText()                    // -> the flattened string (for the §2.1 invariant check)
epubReader.search(query)                 // -> [{gpos, length}]   (in-document; Python drives book-wide search)
epubReader.showMatches(ranges, active)   // CSS Custom Highlight API; no reflow
epubReader.applyHighlights(list)         // [{id,start,end,color,note}]
epubReader.selectionInfo()               // -> {text, start, end} | null
epubReader.clearSelection()
epubReader.zoomImage(on)                 // click-to-zoom overlay
```
Python→JS only through these. JS→Python only through the bridge object injected by `webhost.py` as
`window.epubReaderHost` (QWebChannel), with these signals: `ready`, `positionChanged(state)`, `linkClicked(href)`,
`selectionChanged(info)`, `noteRequested(id)`, `keyUnhandled(keyDescription)`.

Theming must be a `@layer` below the book's own CSS and must NOT use `!important` — the book's italics,
small-caps, poetry indents and semantic colours have to survive. Fixed-layout docs: scale-to-fit, never
paginate. Page turns are instant — no animation, no option for one.

## 5. `webhost.py` (owner D)

Lift verbatim from `docs/research/webengine-prototype.md` / `_proto/proto.py` (verified against 100+
requests with forced GC). Must provide:

```python
def register_epub_scheme() -> None        # MUST be called at import time, before QApplication exists
class BookHost(QObject):
    def __init__(self, profile_name: str = "book-reader") -> None
    def set_book(self, book: EpubBook | None) -> None
    def url_for(self, zip_name: str, fragment: str = "") -> QUrl
    def attach(self, view: QWebEngineView) -> None
    bridge: HostBridge                     # QWebChannel-exposed object (signals in §4)
    def run_js(self, script: str) -> Awaitable-ish   # returns a QFuture-like or takes a callback
```
Non-negotiables: `Content-Type` MUST carry `charset=utf-8` for XHTML/HTML/CSS/JS (verified mojibake cause);
QBuffer lifetime parented to the job; `prefers-color-scheme` forced to light via Chromium flags so the
book's own dark-mode media queries don't fight our theme; assets resolved through a `resource_path()`
helper that works both from source and from `sys._MEIPASS` in the frozen build.

## 6. `reader_page.py` (owner E)

`class ReaderPage(QWidget)` — the whole reading surface from product spec §3: 44px auto-hiding toolbar,
one left dock with the four panes (目录/书签/批注/搜索), the 320px live-apply settings panel, the 26px
status bar, the in-pane error cards, and the F1 cheat sheet. Owns no persistence logic of its own: it reads
and writes exclusively through `Store`. Signals out: `backToLibrary()`, `bookOpened(book_id)`, `titleChanged(str)`.
API in: `open_book(path) -> None`, `close_book()`, `apply_theme(theme)`, plus one slot per keyboard action
so `epub_reader.py` can bind shortcuts centrally.

## 7. `library_page.py` (owner F)

`class LibraryPage(QWidget)` — shelf from product spec §3: top bar, 继续阅读 row, cover grid with generated
fallback covers, list view toggle, sort/filter, right-click menu (**never** a delete-file item), drag-and-drop,
missing-file badge, empty state. Cover thumbnails via Pillow into the cache path from `Store`, generated in a
`QThreadPool` worker — the UI must never block. Signals out: `openBook(path)`, `removeBook(book_id)`.

## 8. `epub_reader.py` + `run.ps1` (owner G)

Entry point: `register_epub_scheme()` before `QApplication`; HiDPI setup; single-instance `QLocalServer`
(`book-reader-single-instance`) forwarding `OPEN <path>` to the running process; `QStackedWidget` over
`LibraryPage` and `ReaderPage`; the complete conflict-audited keyboard map bound centrally with the gating
rule (single-letter keys only when the view has focus and no text input is focused); window geometry
restore; `--help`/`argv[1]` handling; a crash-safe `excepthook` that logs to `%APPDATA%\Book Reader\logs\`. On first launch, migrate a dev-build `%APPDATA%\Verso\` if present.
`run.ps1` launches from source with the right interpreter.

## 9. Definition of done (every owner)

1. Your module imports cleanly and passes `python -X dev -W error::SyntaxWarning -c "import <mod>"`.
2. You wrote and RAN a check proving your own contract — not "it should work".
3. `docs/api-<module>.md` documents every public symbol you expose, with one usage example each.
4. You did not touch another owner's file.
5. Anything you could not do, or did differently from this contract, is stated plainly in your return value
   under `deviations`. Silent deviation is the only unacceptable outcome.
