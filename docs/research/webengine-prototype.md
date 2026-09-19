# Research: webengine-prototype

## Summary

The rendering core works end to end on this machine. I built and ran a prototype in C:\Users\mengz\epub-reader\_proto that registers an `epub://` URL scheme before QApplication, installs a zip-backed QWebEngineUrlSchemeHandler on an off-the-record QWebEngineProfile, and loads a generated multi-file EPUB-like zip. All relative references resolve through the handler and were proven to resolve, not merely to load: CSS applied (computed color match), PNG/JPEG naturalWidth/Height match the generated pixel sizes, the @font-face TTF (relative to the CSS, not the document) reports status "loaded" with measurably different text metrics, and a relative <a href> navigates to chapter 2 whose own relative CSS also applies. QWebEngineScript injection at DocumentCreation and DocumentReady into ApplicationWorld works and overrides the book's own CSS; world isolation from book JS was verified in both directions. QWebChannel is the right bridge here — qwebchannel.js is retrievable from :/qtwebchannel/qwebchannel.js (16509 bytes) and the full handshake, JS→Python fire-and-forget, JS→Python with return value, and Python→JS signal push all work; polling is only needed as a readiness fallback. The final run is 92 PASS / 0 FAIL / 16 INFO, exit code 0, ~6.8s wall clock, and it is stable across repeated runs. Along the way I found four PySide6 6.11 behaviours that would each have silently broken the real app, the most serious being that runJavaScript cannot return anything but scalars and that setAdditionalResponseHeaders corrupts header values unless each value is wrapped in a list.


## Verified facts

1. PySide6 6.11.1 / Qt 6.11.1 on Python 3.14.6 (C:\Users\mengz\AppData\Local\Programs\Python\Python314\python.exe). Proven by running `import PySide6; qVersion()`.

2. EXACT enum spellings, proven by dir()/.pyi introspection of the installed package (C:\Users\mengz\AppData\Roaming\Python\Python314\site-packages\PySide6\QtWebEngineCore.pyi): the syntax enum is `QWebEngineUrlScheme.Syntax` (NOT `SyntaxType`) with members HostPortAndUserInformation/HostAndPort/Host/Path. `QWebEngineUrlScheme.Flag` members are SecureScheme=0x1, LocalScheme=0x2, LocalAccessAllowed=0x4, NoAccessAllowed=0x8, ServiceWorkersAllowed=0x10, ViewSourceAllowed=0x20, ContentSecurityPolicyIgnored=0x40, CorsEnabled=0x80, FetchApiAllowed=0x100. `SpecialPort.PortUnspecified = -1`. `QWebEngineScript.ScriptWorldId` = MainWorld/ApplicationWorld/UserWorld. `QWebEngineUrlRequestJob.Error` = NoError/UrlNotFound/UrlInvalid/RequestAborted/RequestDenied/RequestFailed.

3. Signatures confirmed from the shipped .pyi: `job.reply(contentType: QByteArray|bytes, device: QIODevice)`; `page.runJavaScript(src, worldId=..., resultCallback=...)`; `page.setWebChannel(channel, worldId=...)`; `QWebEngineProfile(name: str, parent=None)` OR `QWebEngineProfile(parent=None)` (the no-name overload gives an off-the-record profile — `isOffTheRecord()` returned True in the run).

4. The QBuffer lifetime model is correct and survives hostile GC. In requestStarted I create `QBuffer(job)` (parented to the job), call setData() then open(), call job.reply(), then `del buf` and `gc.collect()` on every single request. 12 consecutive reloads + navigations = 102 requests served with zero crashes, zero handler errors, and images/CSS/theme still verified correct on the final load. Proven by the `stress.*` rows in the run.

5. runJavaScript's result callback CANNOT return structured data in PySide6 6.11. Proven by C:\Users\mengz\epub-reader\_proto\t_runjs.py, which tested 16 JS value kinds x 2 worlds x both overloads: only numbers (-> float), strings (-> str) and booleans (-> bool) survive. Arrays, objects, null, undefined, DOM nodes, functions, Promises and thrown exceptions ALL arrive as the empty string ''. The callback always receives exactly one argument. `JSON.stringify(...)` on the JS side + `json.loads()` on the Python side round-trips correctly (verified: {'a':[1,2],'b':'x','c':True}).

6. `QWebEngineUrlRequestJob.setAdditionalResponseHeaders()` mangles values unless each value is a LIST. Proven by direct A/B: `{QByteArray(b'X-T'): QByteArray(b'OEBPS/styles/main.css')}` is accepted but emits the header as 's, s, c, ., n, i, a, m, /, s, e, l, y, t, s, /, s, p, b, e, o' (one value per byte, reversed — it is a QMultiMap in C++). Passing `bytes` or `str` values raises TypeError. `{QByteArray(b'X-T'): [b'hello-world']}` works correctly. Qt also lower-cases header VALUES (sent 'OEBPS/styles/main.css', fetch read back 'oebps/styles/main.css').

7. A page Content-Security-Policy DOES block styles injected by a QWebEngineScript, even in ApplicationWorld — Qt user scripts do NOT get Chromium's isolated-world CSP bypass. Proven: with `default-src 'self'` the console logged "Refused to apply inline style ... userscript:proto_boot" and the theme check failed (got rgb(9,9,9) = book value). Adding `style-src 'self' 'unsafe-inline'` fixed it. The user script itself still EXECUTES under `script-src 'none'` — only its DOM operations are CSP-checked.

8. A CSP served via the scheme handler genuinely sandboxes book JavaScript. A/B proven in one run: ch1 (served with the CSP) had its inline <script> refused (`typeof window.__inlineRan === 'undefined'` in MainWorld), ch2 (served without) ran it (`'boolean'`), while ch1's images, CSS, font, fetch() and XHR all still worked.

9. `QWebEnginePage.setBackgroundColor()` works and is the only lever — `QWebEngineView` has no setBackgroundColor in Qt 6.11 (verified hasattr == False). Proven by pixel: page background set to #123456, document background transparent (rgba(0,0,0,0) on html and body), grabbed pixel == #123456. With `Qt.transparent` the widget palette shows through instead (measured #1e1e1e under this machine's dark theme).

10. `QWebEngineView.grab()` DOES capture composited Chromium content in Qt 6.11 and is reliable. `QScreen.grabWindow(view.winId())` is NOT — on Windows it returns stale frames (it returned #ff00ff long after the background had been changed to #008080, and in other runs returned whatever window was occluding). Proven by t_bg2.py comparing view.grab() vs grabWindow(winId) vs a cropped full-desktop grab.

11. `:/qtwebchannel/qwebchannel.js` exists in the Qt resource system in PySide6 6.11 (QFile(...).exists() == True, 16509 chars read). `:/qwebchannel/qwebchannel.js` does NOT exist. QtWebChannel is importable.

12. Injecting scripts into ApplicationWorld gives real two-way isolation from book JS. Proven: `typeof window.__proto` is 'object' in ApplicationWorld and 'undefined' in MainWorld; a sentinel set from MainWorld is 'undefined' in ApplicationWorld. DOM access is shared, so injected styles/DOM edits still apply.

13. At DocumentCreation in an application/xhtml+xml document, document.readyState is 'loading' and BOTH document.documentElement and document.head already exist (measured). At DocumentReady readyState is 'interactive'.

14. In an application/xhtml+xml document Chromium's plain `document.createElement('style')` works (the DOM spec special-cases that content type) — measured rgb(11,11,11) applied. `createElementNS('http://www.w3.org/1999/xhtml', 'style')` also works. My initial assumption that plain createElement would fail was WRONG and was corrected by measurement.

15. Images are NOT decoded at DocumentReady — naturalWidth was 0 for both PNG and JPEG when measured there. You must wait for the window 'load' event plus img.complete/onload before measuring. Likewise document.fonts.status is 'loading' at DocumentReady and only becomes 'loaded' after awaiting document.fonts.ready.

16. Explicitly calling `settings.setAttribute(LocalContentCanAccessFileUrls, False)` on a LocalScheme document silently empties the Resource Timing buffer. Proven twice: a 14-setting one-at-a-time bisect named it as the sole suspect, and the prototype itself flips it at runtime (0 entries with False, 3-4 entries with True, then restores False). Navigation Timing and Paint Timing are unaffected.

17. `QTWEBENGINE_CHROMIUM_FLAGS` set before QApplication really reaches the renderer's V8 — proven with `--js-flags=--expose-gc`, after which `typeof gc === 'function'` in MainWorld.

18. fetch() and XMLHttpRequest both work against the custom scheme with SecureScheme|LocalScheme|LocalAccessAllowed|CorsEnabled|FetchApiAllowed: fetch returned status 200, response.type 'basic' (same-origin), correct body; XHR returned 200. `window.isSecureContext` is true and location.origin is 'epub://book'.

19. With `ErrorPageEnabled=False`, a handler `job.fail(UrlNotFound)` surfaces as `loadFinished(False)` rather than a Chromium error page — verified with a deliberately missing entry.

20. Cold-start timings on this machine (3+ runs): process start -> QApplication ~0.38-0.51s; -> profile+page+view ready ~0.96-3.3s (QWebEngineProfile construction alone is 0.58-4.3s and is by far the most variable part); first epub:// load -> loadFinished 147-522ms; in-page first-contentful-paint 172-564ms from navigation start; 12 reloads ~1.5-3.3s total. devicePixelRatio was 1 on this display.

21. Paint Timing entries are only recorded once the window is actually visible and painting; the prototype calls view.show(); view.raise_(); view.activateWindow() and only then do 'first-paint'/'first-contentful-paint' appear. Resource Timing is also empty on the very first navigation of a fresh renderer even when the setting above is not the cause.

22. Overriding a C++ virtual by assigning a function to an instance attribute does NOT work in PySide6 — `page.javaScriptConsoleMessage = fn` never fires. Subclassing QWebEnginePage and defining the method does (the console messages in the run came from the subclass).


## Pitfalls

1. NEVER rely on runJavaScript returning an array/object/null/undefined — you get '' and cannot distinguish it from an error, an empty string, or a null. Every structured Python<->JS read must be JSON.stringify on the JS side and json.loads on the Python side. A naive `page.runJavaScript('[a,b]', cb)` will look like it works (callback fires) and silently hand you ''. This cost me a hang in the first run because the callback did v[0] on '' and the IndexError was swallowed by Qt.

2. Exceptions raised inside a runJavaScript/toHtml callback are swallowed by Qt — the step machine just stops with no traceback. Wrap every callback body in try/except and print to stderr, or you will debug a hang instead of an error.

3. setAdditionalResponseHeaders: value MUST be a list (`{QByteArray(b'Name'): [b'value']}`). A bare QByteArray value is accepted and silently emits one header value per byte in reverse order. bytes/str values raise TypeError. Values also come back lower-cased.

4. If you ever serve a CSP for the book, you MUST include `style-src 'unsafe-inline'` or your own injected theme/pagination stylesheet is refused — QWebEngineScript in ApplicationWorld does NOT get Chromium's isolated-world CSP bypass. This silently kills theming with only a console message.

5. Do not measure images or fonts at DocumentReady. naturalWidth is 0 and document.fonts.status is 'loading'. Wait for window 'load', then img.complete/onload, then await document.fonts.ready.

6. Do not use QScreen.grabWindow(view.winId()) to screenshot the web view on Windows — it returns stale/occluded content. Use QWebEngineView.grab().

7. Chromium does not paint an occluded or unraised window, so Paint Timing stays empty and rAF-based work can behave differently. Call view.show(); view.raise_(); view.activateWindow() before timing anything.

8. QBuffer: call setData() BEFORE open() (setData on an open buffer is invalid), and parent the buffer to the job (`QBuffer(job)`). Keeping a Python-side reference instead is not enough and keeping none without parenting will crash.

9. Never let an exception escape requestStarted into Qt — catch everything and call job.fail(RequestFailed), otherwise you risk a hard crash or a hung request.

10. Overriding QWebEnginePage virtuals (javaScriptConsoleMessage, acceptNavigationRequest, certificateError...) requires subclassing; assigning to the instance attribute silently does nothing.

11. Register the scheme BEFORE QApplication is constructed and BEFORE any QWebEngineProfile exists; QWebEngineProfile construction itself is expensive (0.6-4.3s here) so do it once and reuse it.

12. Setting LocalContentCanAccessFileUrls=False (which you want, so a book cannot read file://) has the side effect of disabling Resource Timing for the book document. Don't build any instrumentation that depends on performance.getEntriesByType('resource').

13. 'python' on PATH is 3.13 and does NOT have PySide6 6.11 — everything must be run with the explicit Python314 interpreter path.


## Recommendations

1. Use scheme name `epub` with `Syntax.Host` and a fixed host, giving URLs like epub://book/OEBPS/text/ch1.xhtml and a stable origin epub://book. Flags: SecureScheme | LocalScheme | LocalAccessAllowed | CorsEnabled | FetchApiAllowed. Deliberately omit ViewSourceAllowed and NoAccessAllowed. If you ever open two books at once, use the host component as the book id (epub://<bookid>/...) so their origins are distinct.

2. Use QWebChannel for JS->Python, not polling. It is present, qwebchannel.js is retrievable from :/qtwebchannel/qwebchannel.js, and the handshake, fire-and-forget slots, slots-with-return-value and Python->JS signals all work. Use polling ONLY as a readiness fallback: the handshake is asynchronous, so queue any message produced before `bridgeReady` and flush it in the channel callback (the prototype's P.send does exactly this).

3. Inject all reader scripts into ApplicationWorld, not MainWorld, and pass worldId=ApplicationWorld to both page.setWebChannel() and every page.runJavaScript(). This gives real isolation from book JavaScript while keeping full DOM access for theming and pagination.

4. Concatenate qwebchannel.js and your boot script into ONE QWebEngineScript at DocumentCreation rather than inserting two scripts and relying on collection ordering. Add a second script at DocumentReady for anything needing a parsed DOM. Set runsOnSubFrames(True). Install them on the profile (profile.scripts()), not the page, so they apply to every navigation automatically — verified to re-inject on chapter navigation.

5. Make a single `_js_json(expr, callback)` helper the ONLY way the app reads structured data out of the page, wrapping expr in JSON.stringify() and json.loads()-ing the result. Ban bare runJavaScript for anything but scalars.

6. Own the reply device by parenting: `buf = QBuffer(job); buf.setData(QByteArray(data)); buf.open(QIODevice.OpenModeFlag.ReadOnly); job.reply(mime, buf)`. Keep no Python reference. This is proven over 100+ requests with forced gc.collect() on every one.

7. Serve EPUB content documents as application/xhtml+xml (Chromium parses them correctly, document.contentType confirms, and paint/CSS/font behaviour is identical to text/html). Keep an explicit extension->MIME map rather than the mimetypes module, so .xhtml/.opf/.ncx/.woff2 are right.

8. Ship the verified CSP to sandbox untrusted books: `default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; font-src 'self' data:; media-src 'self'; connect-src 'self'; script-src 'none'; object-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'none'` applied to every .xhtml/.html entry. This blocks all book JavaScript while leaving your injected theming, images, fonts and fetch() working — all four confirmed in the same run.

9. Settings to set on the profile: JavascriptEnabled=True, LocalContentCanAccessFileUrls=False, LocalContentCanAccessRemoteUrls=False, PdfViewerEnabled=False, PluginsEnabled=False, ShowScrollBars=False, FocusOnNavigationEnabled=True, ErrorPageEnabled=False, PlaybackRequiresUserGesture=True, JavascriptCanOpenWindows=False, ScrollAnimatorEnabled=False, WebGLEnabled=False, LocalStorageEnabled=True. ErrorPageEnabled=False in particular makes missing-entry failures visible as loadFinished(False) instead of a Chromium error page inside the book.

10. Kill the white flash with page.setBackgroundColor(QColor(theme_bg)) and update it whenever the theme changes. Do NOT use Qt.transparent unless you also control the widget palette, because the Qt window colour shows through instead. Set it once on the page; it survives navigations.

11. Use an off-the-record QWebEngineProfile (QWebEngineProfile(parent) with no storage name) so the reader leaves no cookie/cache directories behind. Construct it once at startup — it is the single most expensive object (0.6-4.3s) and dominates cold start. Consider constructing it while the window/splash is already visible.

12. Set QTWEBENGINE_CHROMIUM_FLAGS before QApplication if you ever need Chromium switches; it is confirmed to reach the renderer. Nothing in the prototype actually requires a flag, so ship with none (or only genuinely needed ones) — the --expose-gc in the prototype is a probe, not a recommendation.

13. Wait for the window 'load' event and document.fonts.ready before running any pagination/measurement pass, and drive the reader's layout from that point, not from loadFinished or DocumentReady.


## Open questions

1. devicePixelRatio was 1 on this display, so high-DPI behaviour is untested. Qt.AA_EnableHighDpiScaling still exists in PySide6 6.11 but I did not verify whether it is a no-op. Someone should run the prototype on a 125%/150% scaled display and check devicePixelRatio, view.grab() dimensions, and whether text sizing in the book needs compensation.

2. I did not test right-to-left books, vertical writing modes, SVG cover images, MathML, or audio/video overlays (EPUB media overlays). The MIME map covers them but nothing was rendered.

3. I did not test a book large enough to matter: the whole zip is 24KB and is held fully in memory. Behaviour with a 100MB+ EPUB (memory, zipfile.read latency on the UI thread) is unknown. The handler currently reads synchronously on the UI thread inside requestStarted — fine at this size, but worth measuring, and job.reply() with a custom QIODevice could stream instead.

4. Chromium's in-memory cache means a resource is not always re-requested on reload (main.css disappeared from Resource Timing on warm loads). If the app ever needs to invalidate content in-place (e.g. user edits a stylesheet override), the caching behaviour of the custom scheme needs its own test.

5. CSP with 'unsafe-inline' for styles means a malicious book's own inline CSS still runs. With connect-src/img-src locked to 'self' the exfiltration surface looks small, but a security-minded review of CSS-based side channels was not done.

6. PyInstaller packaging of QtWebEngine (the QtWebEngineProcess.exe helper, locales, resources) was completely out of scope here and is the next unmitigated risk.


## Verified code snippets


### Scheme registration — must run at import time, before QApplication exists. Verbatim from _proto/proto.py.

```python
from PySide6.QtWebEngineCore import QWebEngineUrlScheme

SCHEME = b"epub"
HOST = "book"


def register_scheme() -> QWebEngineUrlScheme:
    scheme = QWebEngineUrlScheme(SCHEME)
    # Host syntax => epub://book/OEBPS/text/ch1.xhtml, giving a real, stable
    # origin ("epub://book") so CORS / fetch / localStorage behave sanely.
    scheme.setSyntax(QWebEngineUrlScheme.Syntax.Host)
    scheme.setDefaultPort(QWebEngineUrlScheme.SpecialPort.PortUnspecified.value)
    F = QWebEngineUrlScheme.Flag
    scheme.setFlags(
        F.SecureScheme            # treated as a secure context (no mixed-content nags)
        | F.LocalScheme           # file-like: not reachable from the network
        | F.LocalAccessAllowed    # other local schemes (file:) may access it
        | F.CorsEnabled           # required for fetch()/XHR to same-origin epub:// URLs
        | F.FetchApiAllowed       # required for fetch() at all
        # ViewSourceAllowed deliberately NOT set -> view-source:epub://... is blocked
    )
    QWebEngineUrlScheme.registerScheme(scheme)
    return scheme


REGISTERED_SCHEME = register_scheme()   # module level: runs before QApplication
```

### The zip-backed scheme handler, including the QBuffer lifetime model and the CSP/header gotcha. Verbatim from _proto/proto.py (survived 100+ requests with forced gc.collect on every one).

```python
import gc, io, os, zipfile
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject
from PySide6.QtWebEngineCore import QWebEngineUrlRequestJob, QWebEngineUrlSchemeHandler

MIME = {
    ".xhtml": b"application/xhtml+xml", ".html": b"text/html", ".htm": b"text/html",
    ".xml": b"application/xml", ".opf": b"application/oebps-package+xml",
    ".ncx": b"application/x-dtbncx+xml", ".css": b"text/css",
    ".js": b"application/javascript", ".json": b"application/json",
    ".png": b"image/png", ".jpg": b"image/jpeg", ".jpeg": b"image/jpeg",
    ".gif": b"image/gif", ".webp": b"image/webp", ".svg": b"image/svg+xml",
    ".ttf": b"font/ttf", ".otf": b"font/otf", ".woff": b"font/woff",
    ".woff2": b"font/woff2", ".mp3": b"audio/mpeg", ".m4a": b"audio/mp4",
    ".mp4": b"video/mp4", ".txt": b"text/plain",
}

# Verified: script-src 'none' kills book JS while QWebEngineScript user scripts
# still run. style-src 'unsafe-inline' is MANDATORY or our own injected <style>
# is refused (Qt user scripts do NOT get Chromium's isolated-world CSP bypass).
BOOK_CSP = (
    b"default-src 'none'; img-src 'self' data:; "
    b"style-src 'self' 'unsafe-inline'; font-src 'self' data:; "
    b"media-src 'self'; connect-src 'self'; script-src 'none'; "
    b"object-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'none'"
)


def guess_mime(path: str) -> bytes:
    return MIME.get(os.path.splitext(path)[1].lower(), b"application/octet-stream")


class ZipSchemeHandler(QWebEngineUrlSchemeHandler):
    """Serves entries of an in-memory zip over epub://<host>/<path-in-zip>.

    Lifetime model (the part that crashes if you get it wrong):
      * The QBuffer is created with the QWebEngineUrlRequestJob as its PARENT.
        C++ then owns it, so the Python wrapper going out of scope / being
        garbage-collected does not delete the device.
      * Qt destroys the job when the request completes, taking the buffer with it.
      * We keep NO Python-side reference at all.
    """

    def __init__(self, data: bytes, parent: QObject | None = None):
        super().__init__(parent)
        self._zf = zipfile.ZipFile(io.BytesIO(data))
        self._names = set(self._zf.namelist())
        self.served: list[str] = []
        self.missed: list[str] = []
        self.errors: list[str] = []

    def requestStarted(self, job: QWebEngineUrlRequestJob) -> None:
        url = job.requestUrl()
        path = url.path()          # QUrl.path() is FullyDecoded by default in Qt6
        if path.startswith("/"):
            path = path[1:]
        try:
            if url.host() != HOST or path not in self._names:
                self.missed.append(path)
                job.fail(QWebEngineUrlRequestJob.Error.UrlNotFound)
                return

            data = self._zf.read(path)

            buf = QBuffer(job)                     # <- parented to the job: C++ owns it
            buf.setData(QByteArray(data))          # setData BEFORE open()
            buf.open(QIODevice.OpenModeFlag.ReadOnly)

            # !! setAdditionalResponseHeaders takes a QMultiMap in C++, so every
            # VALUE must be a LIST of byte strings. Passing a bare QByteArray is
            # accepted but silently emits one header value per BYTE, in reverse
            # order ("abc" -> "c, b, a"). Passing bytes/str raises TypeError.
            mime = guess_mime(path)
            if mime in (b"application/xhtml+xml", b"text/html"):
                job.setAdditionalResponseHeaders(
                    {QByteArray(b"Content-Security-Policy"): [BOOK_CSP]}
                )
            job.reply(mime, buf)
            self.served.append(path)

            del buf                                # drop the Python wrapper...
            gc.collect()                           # ...and force a collection NOW.
        except Exception as exc:                   # never let an exception escape into Qt
            self.errors.append(f"{path}: {exc!r}")
            job.fail(QWebEngineUrlRequestJob.Error.RequestFailed)
```

### Profile, settings, page, view and web-channel setup. Verbatim from _proto/proto.py (every settings value was read back and asserted).

```python
from PySide6.QtGui import QColor
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import (QWebEnginePage, QWebEngineProfile,
                                     QWebEngineScript, QWebEngineSettings)
from PySide6.QtWebEngineWidgets import QWebEngineView

APP_WORLD = QWebEngineScript.ScriptWorldId.ApplicationWorld   # == 1
MAIN_WORLD = QWebEngineScript.ScriptWorldId.MainWorld         # == 0

# Off-the-record profile: nothing persisted to disk, which is what a reader
# wants (no cookies/cache dirs left behind). Construction costs 0.6-4.3 s --
# do it once, at startup, ideally with a window already on screen.
profile = QWebEngineProfile(parent_qobject)        # no storageName => OTR

handler = ZipSchemeHandler(book_bytes, parent_qobject)
profile.installUrlSchemeHandler(SCHEME, handler)

st = profile.settings()
A = QWebEngineSettings.WebAttribute
st.setAttribute(A.JavascriptEnabled, True)
st.setAttribute(A.LocalContentCanAccessFileUrls, False)   # book must not read file://
st.setAttribute(A.LocalContentCanAccessRemoteUrls, False)
st.setAttribute(A.LocalStorageEnabled, True)
st.setAttribute(A.PdfViewerEnabled, False)
st.setAttribute(A.PluginsEnabled, False)
st.setAttribute(A.ShowScrollBars, False)
st.setAttribute(A.FocusOnNavigationEnabled, True)
st.setAttribute(A.ErrorPageEnabled, False)   # failures -> loadFinished(False)
st.setAttribute(A.PlaybackRequiresUserGesture, True)
st.setAttribute(A.JavascriptCanOpenWindows, False)
st.setAttribute(A.LinksIncludedInFocusChain, True)
st.setAttribute(A.ScrollAnimatorEnabled, False)
st.setAttribute(A.WebGLEnabled, False)
st.setAttribute(A.PrintElementBackgrounds, True)

page = QWebEnginePage(profile, parent_qobject)
# The colour Chromium paints where the document itself is transparent.
# Kills the white flash between navigations under a dark theme.
# QWebEngineView has NO setBackgroundColor in Qt 6.11 -- this is the only lever.
page.setBackgroundColor(QColor("#1e1e1e"))

view = QWebEngineView()
view.setPage(page)
view.show()
view.raise_()
view.activateWindow()    # Chromium does not paint an occluded window

bridge = Bridge(parent_qobject)
channel = QWebChannel(parent_qobject)
channel.registerObject("bridge", bridge)
page.setWebChannel(channel, APP_WORLD)   # same world as the injected scripts
```

### Script injection: the helper, loading qwebchannel.js out of the Qt resource system, and the DocumentCreation boot script that injects a stylesheet and opens the channel. Verbatim from _proto/proto.py.

```python
import json
from PySide6.QtCore import QFile, QIODevice


def make_script(name: str, source: str, point, world=APP_WORLD) -> QWebEngineScript:
    s = QWebEngineScript()
    s.setName(name)
    s.setSourceCode(source)
    s.setInjectionPoint(point)
    s.setWorldId(world)
    s.setRunsOnSubFrames(True)
    return s


def qwebchannel_js() -> str:
    """qwebchannel.js ships inside the Qt resource system. Verified path."""
    f = QFile(":/qtwebchannel/qwebchannel.js")
    if not f.open(QIODevice.OpenModeFlag.ReadOnly):
        raise RuntimeError("cannot open :/qtwebchannel/qwebchannel.js")
    try:
        return bytes(f.readAll().data()).decode("utf-8")
    finally:
        f.close()


# Concatenate qwebchannel.js and the boot script into ONE DocumentCreation
# script rather than relying on collection ordering between two scripts.
boot = qwebchannel_js() + "\n" + BOOT_JS.replace("__THEME_CSS__", json.dumps(THEME_CSS))
profile.scripts().insert(
    make_script("reader_boot", boot, QWebEngineScript.InjectionPoint.DocumentCreation))
profile.scripts().insert(
    make_script("reader_ready", READY_JS, QWebEngineScript.InjectionPoint.DocumentReady))
# Installed on the PROFILE, so they re-inject on every navigation automatically.
```

### The DocumentCreation boot script: theme-stylesheet injection that is safe in XHTML and at any parse stage, plus the QWebChannel handshake with a pending-message queue. Verbatim from _proto/proto.py.

```javascript
(function () {
  if (window.__proto) { return; }
  var P = window.__proto = {
    creationRan: true,
    creationReadyState: document.readyState,   // measured: 'loading'
    styleInjectedAt: null,
    bridgeReady: false,
    lastPush: null
  };

  var CSS = __THEME_CSS__;   // Python substitutes json.dumps(css) here

  // MEASURED: in an application/xhtml+xml document Chromium's createElement()
  // *does* produce HTML-namespace elements (the DOM spec special-cases that
  // content type), so a plain createElement('style') works too. createElementNS
  // is used anyway because it is also correct for other XML content types.
  function makeStyle(css) {
    var s = document.createElementNS('http://www.w3.org/1999/xhtml', 'style');
    s.setAttribute('type', 'text/css');
    s.textContent = css;
    return s;
  }

  function inject() {
    if (document.getElementById('__proto_theme')) { return true; }
    var parent = document.head || document.documentElement;
    if (!parent) { return false; }
    var s = makeStyle(CSS);
    s.id = '__proto_theme';
    parent.appendChild(s);
    P.styleInjectedAt = document.readyState;
    return true;
  }
  P.injectTheme = inject;

  if (!inject()) {
    var obs = new MutationObserver(function () { if (inject()) { obs.disconnect(); } });
    obs.observe(document, { childList: true, subtree: true });
    document.addEventListener('DOMContentLoaded', inject);
  }

  // ---- QWebChannel handshake -------------------------------------------
  function connectChannel() {
    if (typeof qt === 'undefined' || !qt.webChannelTransport) { return false; }
    new QWebChannel(qt.webChannelTransport, function (channel) {
      P.bridge = channel.objects.bridge;
      P.bridgeReady = true;
      P.bridge.pushed.connect(function (msg) { P.lastPush = msg; });
      P.bridge.echo('hello-from-js', function (r) { P.echoResult = r; });
      if (P.pendingReport) { P.bridge.report(P.pendingReport); P.pendingReport = null; }
    });
    return true;
  }
  // The handshake is ASYNC: queue anything produced before it completes.
  P.send = function (payload) {
    if (P.bridgeReady && P.bridge) { P.bridge.report(payload); }
    else { P.pendingReport = payload; }
  };
  if (!connectChannel()) {
    var n = 0;
    var iv = setInterval(function () {
      if (connectChannel() || ++n > 200) { clearInterval(iv); }
    }, 10);
  }
})();
```

### The chosen JS<->Python bridge (QWebChannel) Python side, plus the mandatory JSON helper for reading structured data back out of the page.

```python
from PySide6.QtCore import QObject, Signal, Slot


class Bridge(QObject):
    # Python -> JS push. In JS: bridge.pushed.connect(function (msg) {...})
    pushed = Signal(str)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.reports: list[str] = []
        self.echo_calls = 0

    @Slot(str)
    def report(self, payload: str) -> None:
        """JS -> Python, fire-and-forget (the important direction for a reader:
        'user tapped', 'page N of M', 'selection changed')."""
        self.reports.append(payload)

    @Slot(str, result=str)
    def echo(self, text: str) -> str:
        """JS -> Python with a return value. In JS this is asynchronous:
        bridge.echo('x', function(result) { ... })."""
        self.echo_calls += 1
        return "py:" + text


# ---- reading data back out of the page -------------------------------------
# runJavaScript's callback only ever survives JS numbers, strings and booleans.
# Arrays, objects, null, undefined, DOM nodes, functions, Promises and thrown
# errors ALL arrive as ''. So: stringify on the JS side, json.loads on ours.

def _js(self, src: str, cb, world=APP_WORLD):
    """Raw runJavaScript. Scalars only -- prefer _js_json()."""
    self.page.runJavaScript(src, world, cb)


def _js_json(self, expr: str, cb, world=APP_WORLD):
    """The only reliable way to get structured data out of the page."""

    def wrapped(val):
        try:
            cb(json.loads(val) if val else None)
        except Exception as exc:  # Qt swallows callback errors -- never let it
            print(f"!! _js_json error for {expr[:60]!r}: {exc!r}", file=sys.stderr)
            raise

    self.page.runJavaScript(f"JSON.stringify({expr})", world, wrapped)
```

### Correct way to wait for the page before measuring anything (images are not decoded at DocumentReady, fonts are still 'loading').

```javascript
function afterLoad() {
  return new Promise(function (res) {
    if (document.readyState === 'complete') { res(); }
    else { window.addEventListener('load', function () { res(); }, { once: true }); }
  });
}
function imagesReady() {
  return Promise.all(Array.prototype.map.call(document.images, function (i) {
    if (i.complete) { return Promise.resolve(); }
    return new Promise(function (r) {
      i.addEventListener('load', r, { once: true });
      i.addEventListener('error', r, { once: true });
    });
  }));
}
function afterPaint() {
  return new Promise(function (res) {
    requestAnimationFrame(function () {
      requestAnimationFrame(function () { setTimeout(res, 0); });
    });
  });
}

// Only now are naturalWidth and document.fonts.status trustworthy.
afterLoad()
  .then(imagesReady)
  .then(function () { return document.fonts.ready; })
  .then(afterPaint)
  .then(measureAndPaginate);
```

## Files written

- `C:\Users\mengz\epub-reader\_proto\proto.py`

- `C:\Users\mengz\epub-reader\_proto\make_book.py`

- `C:\Users\mengz\epub-reader\_proto\proto_book.epub`

- `C:\Users\mengz\epub-reader\_proto\run1.txt`

- `C:\Users\mengz\epub-reader\_proto\t_runjs.py`

- `C:\Users\mengz\epub-reader\_proto\t_bg_paint.py`

- `C:\Users\mengz\epub-reader\_proto\t_bg2.py`

- `C:\Users\mengz\epub-reader\_proto\t_restiming.py`
