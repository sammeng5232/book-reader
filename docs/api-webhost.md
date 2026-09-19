# `webhost.py` — API reference (owner D)

The QtWebEngine host for Book Reader: the `epub://` URL scheme, the zip-backed
request handler, the profile and its settings, the injected scripts, the
JS↔Python bridge and the navigation policy.

Verification: `tests/test_webhost.py` runs everything below in a live
`QWebEngineView` against `tests/fixtures/*.epub` and one of the user's real Chinese
books. Run it with
`C:\Users\mengz\AppData\Local\Programs\Python\Python314\python.exe tests\test_webhost.py`.
It prints a PASS/FAIL table and exits 0 only when every check passes.
`--quit-path` runs a second short check: a host left open when the app quits.
Under `unittest` discovery, both checks run in child processes. Each opens a
small window for well under 20 s. Set `EPUB_READER_SKIP_GUI_TESTS=1` to skip
them.

---

## 0. Import order

```python
import webhost                    # registers epub:// and sets Chromium flags, at import
webhost.register_epub_scheme()    # idempotent; call it anyway so the order is visible
app = QApplication(sys.argv)      # only now
host = webhost.BookHost()
```

Importing `webhost` after a `QApplication` exists prints a warning to stderr, and
`epub://` will not work. The import-time registration is the one import-time
side effect that CONTRACT §0 allows.

---

## 1. Module functions

### `register_epub_scheme() -> None`
Registers `epub` with `Syntax.Host` and the flags `SecureScheme | LocalScheme |
LocalAccessAllowed | CorsEnabled | FetchApiAllowed` (`ViewSourceAllowed` is
deliberately off). It also calls `configure_chromium_flags()`. It is idempotent
and already runs at import.
```python
import webhost
webhost.register_epub_scheme()   # safe to call again in main()
```

### `configure_chromium_flags(extra: tuple[str, ...] = ()) -> str`
Adds `REQUIRED_CHROMIUM_FLAGS` (plus `extra`) to `QTWEBENGINE_CHROMIUM_FLAGS` and
returns the new value. Existing flags are kept. An existing `--blink-settings=`
is merged rather than shadowed, because Chromium honours only one. The call is
idempotent. It must run before `QApplication`.
```python
webhost.configure_chromium_flags(("--disable-features=Translate",))
```

### `resource_path(*parts: str) -> str`
Returns the absolute path to a bundled resource. It uses `sys._MEIPASS` in the
PyInstaller build and the directory of `webhost.py` when running from source.
```python
icon = webhost.resource_path("assets", "app.ico")
```

### `asset_path(name: str) -> str`
Shorthand for `resource_path("assets", name)`.
```python
webhost.asset_path("reader.js")
```

### `read_asset(name: str, default: str = "") -> str`
Reads `assets/<name>` as UTF-8 and returns `default` if the file is missing. A
missing asset never causes a failure.
```python
css = webhost.read_asset("reader.css")
```

### `guess_mime(zip_name: str) -> bytes`
Maps an extension to a MIME type using the explicit `MIME` table, not `mimetypes`.
```python
webhost.guess_mime("OEBPS/fonts/a.woff2")   # b'font/woff2'
```

### `host_id_for(book) -> str`
Returns the per-book origin host: `b` + 12 hex characters of
blake2b(path | size | mtime_ns). It returns `"book"` for `None`. Each book gets
its own origin, and so does a file that changed on disk, so books never share a
Chromium cache entry or a localStorage bucket.
```python
webhost.host_id_for(book)   # 'bf3d1fa58dbab'
```

---

## 2. Constants

| Name | Value / meaning |
|---|---|
| `EPUB_SCHEME` / `EPUB_SCHEME_NAME` | `b"epub"` / `"epub"` |
| `DEFAULT_BOOK_HOST` | `"book"` — the host while no book is set |
| `REQUIRED_CHROMIUM_FLAGS` | `("--blink-settings=preferredColorScheme=1",)`: forces `prefers-color-scheme: light` (measured: without it, dark matches on this machine) |
| `BOOK_CSP` | CSP sent with every content document: `script-src 'none'` (book JS never runs), `style-src 'self' 'unsafe-inline'` (required for our injected styles) |
| `APP_WORLD` / `MAIN_WORLD` | `QWebEngineScript.ScriptWorldId` values. All our scripts and `run_js` use `APP_WORLD` |
| `CHANNEL_OBJECT_NAME` | `"epubReaderHostChannel"` — internal QWebChannel name. Pages use `window.epubReaderHost` |
| `BOOT_SCRIPT_NAME` / `READY_SCRIPT_NAME` | `"epub_reader_boot"` / `"epub_reader_ready"` (the profile's two scripts) |
| `THEME_STYLE_ID` / `READER_STYLE_ID` | `"__er_theme"` / `"__er_reader"` — ids of the injected `<style>` elements |
| `EXTERNAL_SCHEMES` | `{"http","https","mailto","tel","ftp"}`. A click on one of these is reported to the app and never loaded |
| `MIME` | extension → MIME table used by `guess_mime` |

Example:
```python
if QUrl(href).scheme() in webhost.EXTERNAL_SCHEMES: ...
```

---

## 3. `class BookHost(QObject)`

`BookHost(profile_name: str = "book-reader", parent: QObject | None = None)`

Each reading surface gets one host. The host owns an **off-the-record**
`QWebEngineProfile`, the `EpubSchemeHandler`, a request interceptor, the
`BookPage`, the `HostBridge` and the `QWebChannel`. Building the profile takes
0.9–2 s on this machine, so build the host once and reuse it for every book.
`profile_name` is only a label (object name and user agent).

```python
host = webhost.BookHost()
view = QWebEngineView()
host.attach(view)
host.bridge.positionChanged.connect(on_position)
host.externalLinkRequested.connect(ask_then_open_in_browser)
host.set_book(EpubBook.open(path))
host.loadFinished.connect(lambda ok: host.call_reader("init", {"settings": s, "locator": loc, "mode": "paginated"}))
host.navigate(book.spine[0].zip_name)
```

### Signals
| Signal | Meaning |
|---|---|
| `loadFinished(bool)` | A document from **this book** finished loading (`True`) or failed (`False`). The `about:blank` parking load and stopped or superseded loads are not reported, so `set_book(b); navigate(x)` gives exactly one signal, for `x`. |
| `locationChanged(str zip_name, str fragment, int spine_index)` | The view's document changed: a committed navigation, a same-document jump, or `("", "", -1)` for `about:blank`. |
| `internalLinkClicked(str zip_name, str fragment)` | The user activated a link inside the book. It is already resolved to a zip entry. The host follows it unless `auto_follow_internal_links` is `False`. |
| `externalLinkRequested(str url)` | The user clicked an http(s), mailto, tel or ftp link, or a `target=_blank` link. The view never goes there. Ask the user, then open the system browser. A link that both `reader.js` and Chromium report within 0.5 s is reported only once. |
| `navigationBlocked(str url, str reason)` | A navigation was refused. `reason` is one of `foreign-origin`, `external`, `scheme`, `missing` or `no-book`. |
| `consoleMessage(str message, str source, int line)` | A message from the page's JS console. |
| `bookChanged(str host)` | `set_book()` finished. The argument is the new origin host. |

```python
host.navigationBlocked.connect(lambda url, why: log.info("blocked %s (%s)", url, why))
```

### Attributes
`profile`, `page` (`BookPage`), `handler` (`EpubSchemeHandler`), `interceptor`,
`bridge` (`HostBridge`), `channel`, `profile_name`,
`auto_follow_internal_links: bool = True`, and `missing_assets: list[str]`.
```python
host.auto_follow_internal_links = False   # ReaderPage takes over link following
```

### `set_book(book: EpubBook | None) -> None`
Serves `book` from now on, or nothing when `book` is `None`. This call stops
loading, gives the handler the new book on a new origin, clears the handler's
request history and Chromium's HTTP cache, parks the view on `about:blank`, and
emits `bookChanged`. The profile, page and bridge are reused. **The host never
closes a book;** the caller owns it. Navigations to the previous book's origin
are then blocked (`foreign-origin`).
```python
host.set_book(new_book); old_book.close()
```

### `book` / `book_host` (properties)
The book being served, and its origin host.
```python
assert host.book_host == webhost.host_id_for(host.book)
```

### `url_for(zip_name: str, fragment: str = "") -> QUrl`
Returns `epub://<book_host>/<percent-encoded entry>#<fragment>`. Entries that
contain `#`, spaces or CJK characters round-trip safely.
```python
host.url_for("文本/第一章.xhtml", "sec2")
```

### `zip_name_for(url: QUrl) -> tuple[str, str]`
The inverse of `url_for`. It resolves through `EpubBook.resolve()`, so a
mis-cased URL still maps to the real entry. For another origin it returns
`("", "")`.
```python
host.zip_name_for(host.page.url())   # ('Text/MiXeD.xhtml', '')
```

### `navigate(zip_name: str, fragment: str = "") -> None`
Loads a document of the current book. A fragment inside the document that is
already showing goes to `window.epubReader.gotoFragment()` when that function
exists. Otherwise it is a normal same-document navigation.
```python
host.navigate("OEBPS/text/ch3.xhtml", "note-12")
```

### `navigate_spine(index: int, fragment: str = "") -> bool`
Loads spine item `index` and returns `False` if the index is out of range.
```python
host.navigate_spine(host.current_spine_index + 1)
```

### `current_zip_name` / `current_fragment` / `current_spine_index` (properties)
Where the view is now. The values are updated from `urlChanged`.
```python
title = book.doc_title(host.current_zip_name)
```

### `attach(view: QWebEngineView) -> None` / `detach() -> None` / `view`
Puts the host's page into `view`. The view does not own the page.
```python
host.attach(self.web_view)
```

### `set_background_color(color: str | QColor) -> None`
Sets the colour Chromium paints where the document is transparent. Use it to
avoid a white flash between chapters in the dark theme.
```python
host.set_background_color(theme.tokens["bg"])
```

### `run_js(script, callback=None, *, world=None) -> None`
Runs `script` in `APP_WORLD`. The callback receives **only numbers, strings or
booleans**. Arrays, objects, `null` and `undefined` all arrive as `''`. An
exception inside the callback is printed, not swallowed.
```python
host.run_js("document.title", lambda t: print(t))
```

### `run_json(expression, callback=None, *, world=None) -> None`
Evaluates `JSON.stringify(expression)` and passes the decoded value to the
callback, or `None` if the value can't be decoded. This is the only reliable way
to get structured data out of the page.
```python
host.run_json("({w: innerWidth, h: innerHeight})", lambda d: print(d["w"]))
```

### `call_reader(method: str, *args, callback=None) -> None`
Calls `window.epubReader.<method>(*args)` (CONTRACT §4) with JSON-encoded
arguments and returns the result as JSON. The callback receives `None` if the
engine or the method is missing. `method` must be a JavaScript identifier.
```python
host.call_reader("init", {"settings": s, "locator": None, "mode": "paginated"}, callback=on_state)
host.call_reader("capture", callback=save_locator)
```

### `post_to_page(kind: str, payload=None) -> None`
Sends a Python → JS message to every `window.epubReaderHost.on(fn)` listener as
`{kind, payload}`.
```python
host.post_to_page("themeChanged", {"name": "night"})
```

### `set_theme_css(css: str) -> None`
Replaces `<style id="__er_theme">` live, and for every later document.
```python
host.set_theme_css(":root{--er-bg:#111}")
```

### `set_boot_config(**values) -> None`
Merges values into `window.epubReaderHost.config` for documents loaded after the
call. `readerCss` and `themeCss` also replace the injected stylesheets.
```python
host.set_boot_config(settings=reader_settings, mode="scroll")
```

### `reload_assets() -> None`
Reads `assets/reader.js` and `assets/reader.css` again through `resource_path()`
and reinstalls the scripts.
```python
host.reload_assets()   # after editing reader.js during development
```

### `close() -> None` / `closed`
Releases the host. It is idempotent and also runs on
`QCoreApplication.aboutToQuit`. The page is deleted before the profile, which is
proven by the absence of Qt's "Release of profile requested but WebEnginePage
still not deleted" warning. A negative control shows the old parenting triggers
that warning. After `close()`, `set_book` and `attach` raise `RuntimeError`, and
`run_js` and `navigate` do nothing.
```python
host.close()
```

---

## 4. `class HostBridge(QObject)` — `host.bridge`

The Python object behind `window.epubReaderHost` (see §7).

| Signal | From JS |
|---|---|
| `ready()` | `epubReaderHost.ready(state)`. The state object is on `message`. |
| `positionChanged(dict)` | `epubReaderHost.positionChanged(state)`. Accepts an object or JSON text. |
| `linkClicked(str)` | `epubReaderHost.linkClicked(href)`. `BookHost` classifies it. |
| `selectionChanged(dict \| None)` | `epubReaderHost.selectionChanged(info \| null)` |
| `noteRequested(str)` | `epubReaderHost.noteRequested(id)` |
| `keyUnhandled(str)` | `epubReaderHost.keyUnhandled(keyDescription)` |
| `domReady()` | Sent by the DocumentReady script for every document |
| `message(str kind, object payload)` | Every message, named ones included |
| `logMessage(str)` | `epubReaderHost.log(text)` |
| `hostMessage(str)` | Python → JS transport (JSON text). Use `post()`. |

```python
host.bridge.keyUnhandled.connect(shortcuts.dispatch_from_page)
```

### `set_responder(fn: Callable[[str, Any], Any] | None) -> None`
Answers `epubReaderHost.ask(kind, payload, cb)`. The return value must be
JSON-serialisable. Without a responder the answer is `null`.
```python
host.bridge.set_responder(lambda kind, p: store.reader_settings(book_id) if kind == "settings" else None)
```

### `post(kind: str, payload=None) -> None`
The same as `BookHost.post_to_page`.
```python
host.bridge.post("ping")
```

### `notify(kind, payload_json)` / `request(kind, payload_json) -> str` (slots)
The transport used by the facade. Don't call these from Python.
`received: list[(kind, payload)]` is a bounded debug history.
```python
print(host.bridge.received[-5:])
```

---

## 5. `class EpubSchemeHandler(QWebEngineUrlSchemeHandler)` — `host.handler`

Serves `epub://<host>/<entry>`. Every path goes through `EpubBook.resolve("/" +
path)`, and every byte comes from `EpubBook.read()` (binary) or `read_text()`
(text, re-encoded to UTF-8 with the BOM removed). Every text type is served with
`; charset=utf-8`. Content documents are served as `text/html` with `BOOK_CSP`.
The reply `QBuffer` is parented to the job.

| Member | Meaning |
|---|---|
| `set_book(book, host)` | Swaps the book and host, and clears the history |
| `book`, `host` | What the handler serves now |
| `served`, `missed`, `errors` | Bounded histories (4096 entries) |
| `request_count` | Requests seen since the last `set_book` |
| `content_document_mime` | `b"text/html"` (class attribute) |
| `csp` | `BOOK_CSP` (class attribute). `b""` disables the CSP |

```python
assert host.handler.errors == [] and "OEBPS/styles/main.css" in host.handler.served
```

## 6. `class BookPage(QWebEnginePage)` — `host.page`

This subclass exists because PySide6 only overrides C++ virtuals in a subclass.
It forwards `acceptNavigationRequest` and `newWindowRequested` to the host's
policy. `createWindow` always returns `None`, `certificateError` always rejects,
and console lines are kept in `console_log` (bounded).
```python
print(host.page.console_log[-3:])
```

---

## 7. Injected JavaScript (ApplicationWorld, every frame, every document)

**DocumentCreation** (`epub_reader_boot`) is one script:
`qwebchannel.js` + the boot facade + `assets/reader.js`. **DocumentReady**
(`epub_reader_ready`) re-applies the styles and sends `domReady`.

MEASURED: at DocumentCreation, a `text/html` document still has
`document.documentElement === null`. The boot script therefore attaches its
styles through a MutationObserver, and `reader.js` resolves the root lazily
(owner C). Messages sent before the asynchronous QWebChannel handshake are
queued (up to 500) and flushed in order.

### `window.epubReaderHost`
```js
epubReaderHost.ready(state)              // object or JSON text
epubReaderHost.positionChanged(state)    // object or JSON text
epubReaderHost.linkClicked(href)         // raw href from the book
epubReaderHost.selectionChanged(info)    // object | JSON text | null
epubReaderHost.noteRequested(id)
epubReaderHost.keyUnhandled(keyDescription)
epubReaderHost.log(text)
epubReaderHost.send(kind, payload)       // arbitrary message -> bridge.message
epubReaderHost.ask(kind, payload, cb)    // -> bridge responder, cb(answer)
epubReaderHost.on(fn) / off(fn)          // Python post_to_page() listeners: fn({kind, payload})
epubReaderHost.isConnected()
epubReaderHost.setThemeCss(css) / setReaderCss(css) / applyStyles()
epubReaderHost.config                    // set_boot_config() values
```
Example:
```js
epubReaderHost.ask('settings', null, function (s) { epubReader.applySettings(s); });
```

A capture-phase `click` listener reports links whose scheme Chromium has no
handler for (`mailto:`, `tel:`…) as `linkClicked`, because those never reach
`acceptNavigationRequest`. `javascript:` links are refused.

`window.__epubReaderBoot` holds diagnostics: `connected`, `queued`, and
`creation` (the document state measured at DocumentCreation).

---

## 8. Settings applied to the profile

JavaScript is on. File and remote URL access from local content is **off**. The
error page is off, so failures show up as `loadFinished(False)`. Scroll bars,
window opening, clipboard, WebGL, DNS prefetch, hyperlink auditing, fullscreen,
screen capture, navigate-on-drop and ForceDarkMode are off. Unknown URL schemes
are disallowed. The default text encoding is UTF-8. A request interceptor blocks
every sub-resource that is not `epub:`, `about:`, `data:`, `blob:` or `qrc:`.
```python
host.profile.settings().testAttribute(QWebEngineSettings.WebAttribute.ErrorPageEnabled)  # False
```

## 9. Notes for other owners

* **E (reader_page):** wait for `host.loadFinished(True)` before
  `call_reader("init", …)`. `loadFinished` is already filtered to this book's
  documents. Connect `externalLinkRequested` to a confirmation dialog followed
  by `QDesktopServices.openUrl`. For paginated fragment jumps, set
  `auto_follow_internal_links = False` and drive `navigate()` and
  `gotoFragment` yourself, or keep the default. The default already calls
  `gotoFragment` for same-document jumps.
* **E and G:** `reader.css` is injected by webhost. Don't pass `cssText` to
  `init()` as well.
* **C (reader.js):** payloads may be objects or JSON text. A link reported from
  `pointerup` and by Chromium's own click navigation is de-duplicated here
  (0.5 s window).
