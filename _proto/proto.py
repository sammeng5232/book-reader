"""EPUB rendering-core prototype -- QtWebEngine + custom URL scheme served from a zip.

Runs non-interactively: opens a small window, drives every check from QTimer /
signal callbacks, prints a machine-checkable PASS/FAIL table, exits non-zero on
any FAIL.

    "C:\\Users\\mengz\\AppData\\Local\\Programs\\Python\\Python314\\python.exe" proto.py
"""

from __future__ import annotations

import gc
import io
import json
import os
import sys
import time
import zipfile

T0 = time.perf_counter()

# --------------------------------------------------------------------------
# 0. Environment / Chromium flags -- MUST be set before QApplication exists.
# --------------------------------------------------------------------------
# --expose-gc is used purely as a *verifiable* probe that QTWEBENGINE_CHROMIUM_FLAGS
# really reaches the renderer's V8. Real app would not ship this.
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--js-flags=--expose-gc --disable-features=Translate",
)

from PySide6.QtCore import (  # noqa: E402
    QBuffer,
    QByteArray,
    QIODevice,
    QObject,
    QTimer,
    QUrl,
    Signal,
    Slot,
    qVersion,
)
from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWebChannel import QWebChannel  # noqa: E402
from PySide6.QtWebEngineCore import (  # noqa: E402
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineScript,
    QWebEngineSettings,
    QWebEngineUrlRequestJob,
    QWebEngineUrlScheme,
    QWebEngineUrlSchemeHandler,
)
from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import make_book  # noqa: E402

SCHEME = b"epub"
HOST = "book"
APP_WORLD = QWebEngineScript.ScriptWorldId.ApplicationWorld  # == 1
MAIN_WORLD = QWebEngineScript.ScriptWorldId.MainWorld        # == 0


# --------------------------------------------------------------------------
# 1. Scheme registration -- before QApplication.
# --------------------------------------------------------------------------
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


REGISTERED_SCHEME = register_scheme()


# --------------------------------------------------------------------------
# 2. The zip-backed URL scheme handler.
# --------------------------------------------------------------------------
MIME = {
    ".xhtml": b"application/xhtml+xml",
    ".html": b"text/html",
    ".htm": b"text/html",
    ".xml": b"application/xml",
    ".opf": b"application/oebps-package+xml",
    ".ncx": b"application/x-dtbncx+xml",
    ".css": b"text/css",
    ".js": b"application/javascript",
    ".json": b"application/json",
    ".png": b"image/png",
    ".jpg": b"image/jpeg",
    ".jpeg": b"image/jpeg",
    ".gif": b"image/gif",
    ".webp": b"image/webp",
    ".svg": b"image/svg+xml",
    ".ttf": b"font/ttf",
    ".otf": b"font/otf",
    ".woff": b"font/woff",
    ".woff2": b"font/woff2",
    ".mp3": b"audio/mpeg",
    ".m4a": b"audio/mp4",
    ".mp4": b"video/mp4",
    ".txt": b"text/plain",
}


# Sandbox for untrusted book markup.
#   script-src 'none'  kills all book JavaScript. QWebEngineScript user scripts
#                      are injected by the browser and still run -- verified.
#   style-src  'unsafe-inline'  is MANDATORY: a QWebEngineScript running in
#                      ApplicationWorld does NOT get Chromium's isolated-world
#                      CSP bypass, so without it our own injected <style> (i.e.
#                      the whole theming/pagination layer) is refused. Verified.
BOOK_CSP = (
    b"default-src 'none'; "
    b"img-src 'self' data:; "
    b"style-src 'self' 'unsafe-inline'; "
    b"font-src 'self' data:; "
    b"media-src 'self'; "
    b"connect-src 'self'; "
    b"script-src 'none'; "
    b"object-src 'none'; "
    b"frame-src 'none'; "
    b"base-uri 'none'; "
    b"form-action 'none'"
)

# ch1 gets the CSP, ch2 gets none -> an A/B proof that the header really applies.
CSP_PATHS = {"OEBPS/text/ch1.xhtml"}


def guess_mime(path: str) -> bytes:
    return MIME.get(os.path.splitext(path)[1].lower(), b"application/octet-stream")


class ZipSchemeHandler(QWebEngineUrlSchemeHandler):
    """Serves entries of an in-memory zip over epub://<host>/<path-in-zip>.

    Lifetime model (the part that crashes if you get it wrong):
      * The QBuffer is created with the QWebEngineUrlRequestJob as its PARENT.
        C++ then owns it, so the Python wrapper going out of scope / being
        garbage-collected does not delete the device.
      * Qt destroys the job when the request completes, taking the buffer with it.
      * We keep NO Python-side reference at all -- see the gc.collect() below,
        which is deliberately hostile and proves the model.
    """

    def __init__(self, data: bytes, parent: QObject | None = None):
        super().__init__(parent)
        self._raw = data
        self._zf = zipfile.ZipFile(io.BytesIO(data))
        self._names = set(self._zf.namelist())
        self.served: list[str] = []
        self.missed: list[str] = []
        self.errors: list[str] = []

    def requestStarted(self, job: QWebEngineUrlRequestJob) -> None:  # noqa: N802
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
            buf.setData(QByteArray(data))          # setData before open()
            buf.open(QIODevice.OpenModeFlag.ReadOnly)

            # !! setAdditionalResponseHeaders takes a QMultiMap in C++, so every
            # VALUE must be a LIST of byte strings. Passing a bare QByteArray is
            # accepted but silently emits one header value per BYTE, in reverse
            # order ("abc" -> "c, b, a"). Passing bytes/str raises TypeError.
            headers = {QByteArray(b"X-Epub-Entry"): [path.encode("utf-8")]}
            if path in CSP_PATHS:
                headers[QByteArray(b"Content-Security-Policy")] = [BOOK_CSP]
            job.setAdditionalResponseHeaders(headers)
            job.reply(guess_mime(path), buf)
            self.served.append(path)

            del buf                                # drop the Python wrapper...
            gc.collect()                           # ...and force a collection NOW.
        except Exception as exc:                   # never let an exception escape into Qt
            self.errors.append(f"{path}: {exc!r}")
            job.fail(QWebEngineUrlRequestJob.Error.RequestFailed)


# --------------------------------------------------------------------------
# 3. JS <-> Python bridge object (QWebChannel).
# --------------------------------------------------------------------------
class LoggingPage(QWebEnginePage):
    """Subclassing is the only way to override a C++ virtual in PySide6 --
    assigning the method on an instance does NOT reach the vtable."""

    def __init__(self, profile, parent=None):
        super().__init__(profile, parent)
        self.console_log: list[str] = []

    def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
        self.console_log.append(f"[{level}] {os.path.basename(source)}:{line} {message}")


class Bridge(QObject):
    # Python -> JS push
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


# --------------------------------------------------------------------------
# 4. Injected scripts.
# --------------------------------------------------------------------------
def qwebchannel_js() -> str:
    """qwebchannel.js ships inside the Qt resource system. Verified path."""
    from PySide6.QtCore import QFile

    f = QFile(":/qtwebchannel/qwebchannel.js")
    if not f.open(QIODevice.OpenModeFlag.ReadOnly):
        raise RuntimeError("cannot open :/qtwebchannel/qwebchannel.js")
    try:
        return bytes(f.readAll().data()).decode("utf-8")
    finally:
        f.close()


THEME_CSS = """
#theme-probe { color: rgb(7, 7, 7) !important; }
body { --proto-injected: yes; }
"""

BOOT_JS = r"""
(function () {
  if (window.__proto) { return; }
  var P = window.__proto = {
    creationRan: true,
    creationReadyState: document.readyState,
    creationHadDocEl: !!document.documentElement,
    creationHadHead: !!document.head,
    world: 'application',
    styleInjectedAt: null,
    bridgeReady: false,
    lastPush: null,
    echoResult: null,
    probe: null
  };

  var CSS = __THEME_CSS__;

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
"""

PROBE_JS = r"""
(function () {
  var P = window.__proto;
  if (!P) { return; }
  P.readyRan = true;
  P.readyReadyState = document.readyState;
  P.injectTheme();
  P.probeDone = false;

  function cs(sel, prop) {
    var el = document.querySelector(sel);
    if (!el) { return null; }
    return getComputedStyle(el).getPropertyValue(prop).trim();
  }
  function w(el) {
    return el ? Math.round(el.getBoundingClientRect().width * 100) / 100 : -1;
  }
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
  function faceList() {
    var a = [];
    document.fonts.forEach(function (f) { a.push(f.family + ':' + f.status); });
    return a;
  }

  var out = {
    href: location.href,
    origin: location.origin,
    title: document.title,
    contentType: document.contentType,
    docEl: document.documentElement.namespaceURI,
    // markers left by the DocumentCreation script
    creationRan: P.creationRan === true,
    creationReadyState: P.creationReadyState,
    creationHadDocEl: P.creationHadDocEl,
    creationHadHead: P.creationHadHead,
    readyReadyState: P.readyReadyState,
    styleInjectedAt: P.styleInjectedAt,
    // measured at DocumentReady, before load
    fontsStatusAtReady: document.fonts.status,
    fontCheckAtReady: document.fonts.check('32px "ProtoFont"'),
    fontFacesAtReady: faceList(),
    devicePixelRatio: window.devicePixelRatio
  };

  afterLoad()
    .then(imagesReady)
    .then(function () { return document.fonts.ready; })
    .then(afterPaint)
    .then(function () {
      out.readyStateAtMeasure = document.readyState;
      out.cssApplied = cs('#css-probe', 'color');
      out.bodyBg = cs('body', 'background-color');
      out.themeApplied = cs('#theme-probe', 'color');
      out.themeNodePresent = !!document.getElementById('__proto_theme');

      var png = document.getElementById('png');
      var jpg = document.getElementById('jpg');
      out.pngW = png ? png.naturalWidth : -1;
      out.pngH = png ? png.naturalHeight : -1;
      out.pngComplete = png ? png.complete : false;
      out.jpgW = jpg ? jpg.naturalWidth : -1;
      out.jpgH = jpg ? jpg.naturalHeight : -1;
      out.jpgComplete = jpg ? jpg.complete : false;

      // ---- createElement vs createElementNS inside an XHTML document ----
      var host = document.body || document.documentElement;
      var mk = function (ns, id, color) {
        var probe = document.createElementNS('http://www.w3.org/1999/xhtml', 'span');
        probe.id = id; probe.textContent = '.';
        host.appendChild(probe);
        var st = ns
          ? document.createElementNS('http://www.w3.org/1999/xhtml', 'style')
          : document.createElement('style');
        st.textContent = '#' + id + '{color:' + color + '}';
        host.appendChild(st);
        return getComputedStyle(probe).color.trim();
      };
      out.plainCreateElementColor = mk(false, 'plain-probe', 'rgb(11, 11, 11)');
      out.nsCreateElementColor = mk(true, 'ns-probe', 'rgb(12, 12, 12)');

      // ---- fonts ------------------------------------------------------
      out.fontSpanW = w(document.getElementById('fspan'));
      out.fallbackSpanW = w(document.getElementById('nspan'));
      out.fontFaces = faceList();
      out.fontCheck = document.fonts.check('32px "ProtoFont"');
      out.fontsStatus = document.fonts.status;

      // ---- timings ----------------------------------------------------
      out.paints = (performance.getEntriesByType('paint') || []).map(function (e) {
        return [e.name, Math.round(e.startTime)];
      });
      var nav = performance.getEntriesByType('navigation')[0];
      if (nav) {
        out.responseEndMs = Math.round(nav.responseEnd);
        out.domContentLoadedMs = Math.round(nav.domContentLoadedEventEnd);
        out.loadEventMs = Math.round(nav.loadEventEnd);
      }
      out.resourceCount = performance.getEntriesByType('resource').length;

      return fetch('../styles/main.css');
    })
    .then(function (r) {
      out.fetchStatus = r.status;
      out.fetchOk = r.ok;
      out.fetchType = r.type;
      out.fetchCustomHeader = r.headers.get('x-epub-entry');
      return r.text();
    })
    .then(function (t) {
      out.fetchLen = t.length;
      out.fetchHasFontFace = t.indexOf('ProtoFont') !== -1;
    })
    .catch(function (e) { out.fetchError = String(e && e.stack || e); })
    .then(function () {
      return new Promise(function (res) {
        try {
          var x = new XMLHttpRequest();
          x.open('GET', '../styles/ch2.css');
          x.onload = function () {
            out.xhrStatus = x.status; out.xhrLen = x.responseText.length; res();
          };
          x.onerror = function () { out.xhrError = 'onerror'; res(); };
          x.send();
        } catch (e) { out.xhrError = String(e); res(); }
      });
    })
    .then(function () {
      out.bridgeReady = P.bridgeReady === true;
      out.echoResult = P.echoResult;
      P.probe = out;
      P.probeDone = true;
      P.send(JSON.stringify(out));
    })
    .catch(function (e) {
      P.probe = { fatal: String(e && e.stack || e) };
      P.probeDone = true;
    });
})();
"""


def make_script(name: str, source: str, point, world=APP_WORLD) -> QWebEngineScript:
    s = QWebEngineScript()
    s.setName(name)
    s.setSourceCode(source)
    s.setInjectionPoint(point)
    s.setWorldId(world)
    s.setRunsOnSubFrames(True)
    return s


# --------------------------------------------------------------------------
# 5. Checks table
# --------------------------------------------------------------------------
class Results:
    def __init__(self):
        self.rows: list[tuple[str, str, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((name, "PASS" if ok else "FAIL", detail))
        return ok

    def eq(self, name: str, got, want) -> bool:
        return self.check(name, got == want, f"got={got!r} want={want!r}")

    def info(self, name: str, detail: str) -> None:
        self.rows.append((name, "INFO", detail))

    @property
    def failures(self) -> int:
        return sum(1 for _, s, _ in self.rows if s == "FAIL")

    def dump(self) -> None:
        w = max((len(n) for n, _, _ in self.rows), default=10)
        print()
        print("=" * (w + 60))
        print(f"{'CHECK'.ljust(w)}  STATUS  DETAIL")
        print("-" * (w + 60))
        for n, s, d in self.rows:
            print(f"{n.ljust(w)}  {s:<6}  {d}")
        print("=" * (w + 60))
        n_pass = sum(1 for _, s, _ in self.rows if s == "PASS")
        n_info = sum(1 for _, s, _ in self.rows if s == "INFO")
        print(f"PASS={n_pass}  FAIL={self.failures}  INFO={n_info}")


# --------------------------------------------------------------------------
# 6. The prototype driver
# --------------------------------------------------------------------------
STRESS_LOADS = 12


class Proto(QObject):
    def __init__(self, app: QApplication, book: bytes):
        super().__init__()
        self.app = app
        self.R = Results()
        self.t_start = T0

        # ---- profile ------------------------------------------------------
        t = time.perf_counter()
        # Off-the-record profile: nothing persisted to disk, which is what a
        # reader wants (no cookies/cache dirs left behind).
        self.profile = QWebEngineProfile(self)           # no storageName => OTR
        self.t_profile = (time.perf_counter() - t) * 1000

        self.handler = ZipSchemeHandler(book, self)
        self.profile.installUrlSchemeHandler(SCHEME, self.handler)

        # ---- settings -----------------------------------------------------
        st = self.profile.settings()
        A = QWebEngineSettings.WebAttribute
        st.setAttribute(A.JavascriptEnabled, True)
        st.setAttribute(A.LocalContentCanAccessFileUrls, False)
        st.setAttribute(A.LocalContentCanAccessRemoteUrls, False)
        st.setAttribute(A.LocalStorageEnabled, True)
        st.setAttribute(A.PdfViewerEnabled, False)
        st.setAttribute(A.PluginsEnabled, False)
        st.setAttribute(A.ShowScrollBars, False)
        st.setAttribute(A.FocusOnNavigationEnabled, True)
        st.setAttribute(A.ErrorPageEnabled, False)
        st.setAttribute(A.PlaybackRequiresUserGesture, True)
        st.setAttribute(A.JavascriptCanOpenWindows, False)
        st.setAttribute(A.LinksIncludedInFocusChain, True)
        st.setAttribute(A.ScrollAnimatorEnabled, False)
        st.setAttribute(A.WebGLEnabled, False)
        st.setAttribute(A.PrintElementBackgrounds, True)
        self.settings = st

        # ---- injected scripts --------------------------------------------
        boot = qwebchannel_js() + "\n" + BOOT_JS.replace(
            "__THEME_CSS__", json.dumps(THEME_CSS)
        )
        self.profile.scripts().insert(
            make_script("proto_boot", boot,
                        QWebEngineScript.InjectionPoint.DocumentCreation))
        self.profile.scripts().insert(
            make_script("proto_probe", PROBE_JS,
                        QWebEngineScript.InjectionPoint.DocumentReady))

        # ---- page / view --------------------------------------------------
        self.page = LoggingPage(self.profile, self)
        # The colour Chromium paints where the document itself is transparent.
        # Setting it kills the white flash between navigations under a dark theme.
        # Deliberately a nonsense colour so the pixel check proves causality.
        self.BG = "#123456"
        self.page.setBackgroundColor(QColor(self.BG))

        self.view = QWebEngineView()
        self.view.setPage(self.page)
        self.view.setGeometry(60, 60, 900, 700)
        self.view.setWindowTitle("epub rendering-core prototype")

        # ---- web channel ---------------------------------------------------
        self.bridge = Bridge(self)
        self.channel = QWebChannel(self)
        self.channel.registerObject("bridge", self.bridge)
        self.page.setWebChannel(self.channel, APP_WORLD)

        self._steps: list = []
        self._done = False
        self._stress_n = 0
        self.t_view_ready = (time.perf_counter() - T0) * 1000

    # ------------------------------------------------------------------
    def _load(self, url: str, cb):
        started = time.perf_counter()

        def handler(ok: bool):
            try:
                self.page.loadFinished.disconnect(handler)
            except (RuntimeError, TypeError):
                pass
            cb(ok, (time.perf_counter() - started) * 1000)

        self.page.loadFinished.connect(handler)
        self.page.load(QUrl(url))

    def _js(self, src: str, cb, world=APP_WORLD):
        """Raw runJavaScript. NOTE: only JS numbers/strings/booleans survive the
        trip; everything else arrives as ''. Prefer _js_json()."""
        self.page.runJavaScript(src, world, cb)

    def _js_json(self, expr: str, cb, world=APP_WORLD):
        """The only reliable way to get structured data out of the page:
        stringify on the JS side, json.loads on the Python side."""

        def wrapped(val):
            try:
                cb(json.loads(val) if val else None)
            except Exception as exc:  # surface, never swallow, callback errors
                print(f"!! _js_json parse/callback error for {expr[:60]!r}: {exc!r}",
                      file=sys.stderr)
                raise

        self.page.runJavaScript(f"JSON.stringify({expr})", world, wrapped)

    def _await_probe(self, cb, timeout_ms=8000):
        deadline = time.perf_counter() + timeout_ms / 1000.0

        def poll():
            def got(val):
                if val:
                    cb(json.loads(val))
                elif time.perf_counter() > deadline:
                    cb(None)
                else:
                    QTimer.singleShot(25, poll)

            self._js(
                "(window.__proto && window.__proto.probeDone) "
                "? JSON.stringify(window.__proto.probe) : ''",
                got,
            )

        poll()

    def _advance(self, *_):
        if self._done:
            return
        if not self._steps:
            self.finish()
            return
        QTimer.singleShot(0, self._steps.pop(0))

    # ==================================================================
    def start(self):
        self._steps = [
            self.step_static_checks,
            self.step_load_ch1,
            self.step_timing,
            self.step_bridge_checks,
            self.step_world_isolation,
            self.step_to_html,
            self.step_navigate_ch2,
            self.step_background_pixel,
            self.step_stress,
            self.step_resource_timing_causality,
            self.step_negative_404,
        ]
        self._advance()

    # ---- static / non-DOM checks -------------------------------------
    def step_static_checks(self):
        R = self.R
        s = QWebEngineUrlScheme.schemeByName(SCHEME)
        R.eq("scheme.registered.name", bytes(s.name().data()), b"epub")
        R.eq("scheme.syntax", s.syntax(), QWebEngineUrlScheme.Syntax.Host)
        F = QWebEngineUrlScheme.Flag
        fl = s.flags()
        for label, flag, want in [
            ("SecureScheme", F.SecureScheme, True),
            ("LocalScheme", F.LocalScheme, True),
            ("LocalAccessAllowed", F.LocalAccessAllowed, True),
            ("CorsEnabled", F.CorsEnabled, True),
            ("FetchApiAllowed", F.FetchApiAllowed, True),
            ("ViewSourceAllowed", F.ViewSourceAllowed, False),
            ("NoAccessAllowed", F.NoAccessAllowed, False),
        ]:
            R.eq(f"scheme.flag.{label}", bool(fl & flag), want)

        R.check(
            "handler.installed",
            self.profile.urlSchemeHandler(SCHEME) is self.handler,
            f"{self.profile.urlSchemeHandler(SCHEME)!r}",
        )
        R.check("profile.offTheRecord", self.profile.isOffTheRecord(), "")

        A = QWebEngineSettings.WebAttribute
        for label, attr, want in [
            ("JavascriptEnabled", A.JavascriptEnabled, True),
            ("LocalContentCanAccessFileUrls", A.LocalContentCanAccessFileUrls, False),
            ("LocalContentCanAccessRemoteUrls", A.LocalContentCanAccessRemoteUrls, False),
            ("LocalStorageEnabled", A.LocalStorageEnabled, True),
            ("PdfViewerEnabled", A.PdfViewerEnabled, False),
            ("ShowScrollBars", A.ShowScrollBars, False),
            ("FocusOnNavigationEnabled", A.FocusOnNavigationEnabled, True),
            ("ErrorPageEnabled", A.ErrorPageEnabled, False),
        ]:
            R.eq(f"settings.{label}", self.settings.testAttribute(attr), want)

        R.eq("page.backgroundColor", self.page.backgroundColor().name(), self.BG)

        wc = qwebchannel_js()
        R.check("qwebchannel.js.fromQrc", len(wc) > 10000,
                f"len={len(wc)} path=:/qtwebchannel/qwebchannel.js")
        R.eq("profile.scripts.count", self.profile.scripts().count(), 2)

        self.view.show()
        self.view.raise_()
        self.view.activateWindow()   # occluded windows do not get painted by Chromium
        self._advance()

    # ---- load chapter 1 ------------------------------------------------
    def step_load_ch1(self):
        def done(ok, ms):
            self.R.check("load.ch1.loadFinished", ok, f"{ms:.0f} ms")
            self.t_ch1_load = ms
            if not ok:
                self._advance()
                return
            self._await_probe(self.on_ch1_probe)

        self._load(f"epub://{HOST}/OEBPS/text/ch1.xhtml", done)

    def on_ch1_probe(self, p):
        R = self.R
        if p is None:
            R.check("probe.ch1.returned", False, "timed out")
            self._advance()
            return
        self.ch1_probe = p
        R.check("probe.ch1.returned", True, "")
        R.eq("doc.title", p.get("title"), "Proto Chapter One")
        R.eq("doc.href", p.get("href"), f"epub://{HOST}/OEBPS/text/ch1.xhtml")
        R.eq("doc.origin", p.get("origin"), f"epub://{HOST}")
        R.eq("doc.contentType", p.get("contentType"), "application/xhtml+xml")
        R.eq("doc.namespace", p.get("docEl"), "http://www.w3.org/1999/xhtml")

        # relative CSS (../styles/main.css) actually fetched + applied
        R.eq("css.relative.applied", p.get("cssApplied"), "rgb(1, 2, 3)")
        R.eq("css.body.background", p.get("bodyBg"), "rgb(17, 34, 51)")

        # relative images (../images/*)
        R.eq("img.png.naturalWidth", p.get("pngW"), make_book.PNG_W)
        R.eq("img.png.naturalHeight", p.get("pngH"), make_book.PNG_H)
        R.eq("img.jpeg.naturalWidth", p.get("jpgW"), make_book.JPG_W)
        R.eq("img.jpeg.naturalHeight", p.get("jpgH"), make_book.JPG_H)

        # font: url(../fonts/proto.ttf) resolved RELATIVE TO THE CSS FILE
        faces = p.get("fontFaces") or []
        R.check("font.face.loaded", "ProtoFont:loaded" in faces, f"faces={faces}")
        R.check("font.document.fonts.check", bool(p.get("fontCheck")),
                f"check={p.get('fontCheck')} status={p.get('fontsStatus')}")
        fw, nw = p.get("fontSpanW", -1), p.get("fallbackSpanW", -1)
        R.check("font.metrics.differ", fw > 0 and nw > 0 and abs(fw - nw) > 1.0,
                f"proto={fw} fallback={nw}")

        # script injection
        R.check("script.DocumentCreation.ran", p.get("creationRan") is True,
                f"readyState at injection={p.get('creationReadyState')} "
                f"docEl={p.get('creationHadDocEl')} head={p.get('creationHadHead')}")
        R.check("script.DocumentReady.ran", p.get("readyReadyState") is not None,
                f"readyState={p.get('readyReadyState')}")
        R.check("script.injectedStyle.present", bool(p.get("themeNodePresent")),
                f"injectedAt readyState={p.get('styleInjectedAt')}")
        R.eq("script.injectedStyle.overrides.bookCss", p.get("themeApplied"),
             "rgb(7, 7, 7)")

        # XHTML DOM gotcha
        R.eq("xhtml.createElementNS.works", p.get("nsCreateElementColor"),
             "rgb(12, 12, 12)")
        R.eq("xhtml.plainCreateElement.alsoWorks",
             p.get("plainCreateElementColor"), "rgb(11, 11, 11)")

        # fetch / XHR over the custom scheme
        R.eq("fetch.status", p.get("fetchStatus"), 200)
        R.check("fetch.body.ok", bool(p.get("fetchHasFontFace")),
                f"len={p.get('fetchLen')} type={p.get('fetchType')} "
                f"err={p.get('fetchError')}")
        R.eq("xhr.status", p.get("xhrStatus"), 200)
        # NOTE: Qt lower-cases additional response header VALUES as well as names.
        R.eq("response.additionalHeaderVisible",
             (p.get("fetchCustomHeader") or "").lower(),
             "oebps/styles/main.css")

        # handler actually served everything, nothing 404'd
        for need in make_book.CH1_REQUIRED:
            R.check(f"handler.served[{need}]", need in self.handler.served, "")
        R.eq("handler.misses", self.handler.missed, [])
        R.eq("handler.errors", self.handler.errors, [])

        R.info("paint.entries(measuredInsideInjectedProbe)",
               f"{p.get('paints')} resources={p.get('resourceCount')} "
               f"readyState={p.get('readyStateAtMeasure')}")
        R.info("timing.navigation(in-page)",
               f"responseEnd={p.get('responseEndMs')}ms "
               f"DCL={p.get('domContentLoadedMs')}ms load={p.get('loadEventMs')}ms "
               f"resources={p.get('resourceCount')}")
        R.info("devicePixelRatio", str(p.get("devicePixelRatio")))
        R.info("fonts.atDocumentReady",
               f"status={p.get('fontsStatusAtReady')} check={p.get('fontCheckAtReady')} "
               f"faces={p.get('fontFacesAtReady')}")
        self._advance()

    # ---- paint / resource timing, measured from Python -------------------
    def step_timing(self):
        R = self.R

        def got(d):
            d = d or {}
            R.check("paint.entriesRecorded", bool(d.get("paint")),
                    f"{d.get('paint')}")
            # NOTE: on the very FIRST navigation of a fresh renderer the resource
            # timeline comes back empty; it populates from the 2nd navigation on.
            # Verified independently -- not caused by our response headers.
            R.info("resourceTiming.onFirstNavigation",
                   f"count={d.get('resourceCount')} names={d.get('resource')} "
                   f"(empty on a cold renderer -- expected)")
            R.info("timing.firstContentfulPaint(in-page ms from nav start)",
                   str(dict(d.get("paint") or [])))
            self._advance()

        # give the compositor one more beat than the probe did
        QTimer.singleShot(
            250,
            lambda: self._js_json(
                "({paint: performance.getEntriesByType('paint')"
                "          .map(e => [e.name, Math.round(e.startTime)]),"
                "  resourceCount: performance.getEntriesByType('resource').length,"
                "  resource: performance.getEntriesByType('resource')"
                "          .map(e => e.name.split('/').pop())})",
                got,
            ),
        )

    # ---- bridge --------------------------------------------------------
    def step_bridge_checks(self):
        R = self.R
        p = getattr(self, "ch1_probe", {}) or {}
        R.check("webchannel.handshake", bool(p.get("bridgeReady")), "")
        R.eq("webchannel.js->py->js.echo", p.get("echoResult"), "py:hello-from-js")
        R.check("webchannel.py.received.echo", self.bridge.echo_calls >= 1,
                f"calls={self.bridge.echo_calls}")
        R.check("webchannel.js->py.report", len(self.bridge.reports) >= 1,
                f"reports={len(self.bridge.reports)} "
                f"bytes={len(self.bridge.reports[0]) if self.bridge.reports else 0}")
        if self.bridge.reports:
            try:
                same = json.loads(self.bridge.reports[0]).get("title")
            except Exception as e:
                same = f"<{e}>"
            R.eq("webchannel.report.payload.intact", same, "Proto Chapter One")

        # Python -> JS push via signal, then read it back.
        self.bridge.pushed.emit("tick-42")

        def got(val):
            R.eq("webchannel.py->js.signal", val, "tick-42")

            # Python -> JS with a result callback. Scalars survive as-is...
            def got_scalar(v2):
                R.eq("runJavaScript.scalarResult", v2, "scalar-ok")

                # ...but arrays/objects/null/undefined ALL arrive as '' (Qt 6.11).
                def got_raw_array(v3):
                    R.eq("runJavaScript.arrayResultIsEmptyString", v3, "")

                    def got_json(v4):
                        R.eq("runJavaScript.jsonRoundTrip", v4,
                             {"a": [1, 2], "b": "x", "c": True})
                        self._advance()

                    self._js_json("({a:[1,2], b:'x', c:true})", got_json)

                self._js("[1, 'two', true]", got_raw_array)

            self._js("'scalar-ok'", got_scalar)

        QTimer.singleShot(60, lambda: self._js("window.__proto.lastPush", got))

    # ---- world isolation ------------------------------------------------
    def step_world_isolation(self):
        R = self.R

        def a(v):
            R.eq("world.app.sees.__proto", v, "object")

            def b(v2):
                R.eq("world.main.blind.to.__proto", v2, "undefined")

                def c(_):
                    def d(v4):
                        R.eq("world.app.blind.to.mainSentinel", v4, "undefined")

                        def e(v5):
                            R.info("chromium.flags.--js-flags=--expose-gc",
                                   f"typeof gc in MainWorld = {v5!r}")

                            def f(v6):
                                # ch1 is served with a strict CSP -> the book's
                                # own inline <script> must NOT have run...
                                R.eq("csp.blocksBookInlineScript(ch1)",
                                     v6, "undefined")
                                # ...while our ApplicationWorld injection did
                                # (isolated worlds bypass the page CSP).
                                R.check("csp.injectedStyleSurvives(needs style-src unsafe-inline)",
                                        self.ch1_probe.get("themeApplied")
                                        == "rgb(7, 7, 7)",
                                        f"themeApplied={self.ch1_probe.get('themeApplied')!r}")
                                self._advance()

                            self._js("typeof window.__inlineRan", f, MAIN_WORLD)

                        self._js("typeof gc", e, MAIN_WORLD)

                    self._js("typeof window.__mainSentinel", d, APP_WORLD)

                self._js("window.__mainSentinel = 1; 'ok'", c, MAIN_WORLD)

            self._js("typeof window.__proto", b, MAIN_WORLD)

        self._js("typeof window.__proto", a, APP_WORLD)

    # ---- toHtml ---------------------------------------------------------
    def step_to_html(self):
        def got(html):
            self.R.check("page.toHtml.hasBookMarkup",
                         "Chapter One" in html and "__proto_theme" in html,
                         f"len={len(html)}")
            self._advance()

        self.page.toHtml(got)

    # ---- relative link navigation ---------------------------------------
    def step_navigate_ch2(self):
        def done(ok, ms):
            self.R.check("nav.ch2.loadFinished", ok, f"{ms:.0f} ms")
            if not ok:
                self._advance()
                return

            def got(d):
                d = d or {}
                self.R.eq("nav.ch2.href", d.get("href"),
                          f"epub://{HOST}/OEBPS/text/ch2.xhtml")
                self.R.eq("nav.ch2.title", d.get("title"), "Proto Chapter Two")
                self.R.eq("nav.ch2.ownRelativeCss", d.get("probeColor"),
                          "rgb(4, 5, 6)")
                self.R.check("nav.ch2.scriptsReinjected",
                             d.get("creationRan") is True and d.get("themeNode") is True,
                             "")
                self.R.check("handler.served[OEBPS/styles/ch2.css]",
                             "OEBPS/styles/ch2.css" in self.handler.served, "")

                # ch2 is served WITHOUT a CSP -> its inline script must run.
                # Together with the ch1 result this proves the CSP header, and
                # therefore setAdditionalResponseHeaders, really takes effect.
                def inline(v):
                    self.R.eq("csp.absent.bookInlineScriptRuns(ch2)", v, "boolean")
                    self._advance()

                self._js("typeof window.__inlineRan", inline, MAIN_WORLD)

            self._js_json(
                "({href: location.href, title: document.title,"
                " probeColor: getComputedStyle(document.querySelector('#ch2-probe')).color.trim(),"
                " creationRan: !!(window.__proto && window.__proto.creationRan),"
                " themeNode: !!document.getElementById('__proto_theme')})",
                got,
            )

        # navigate by clicking the RELATIVE <a href="ch2.xhtml"> in the book
        started = time.perf_counter()

        def on_load(ok):
            try:
                self.page.loadFinished.disconnect(on_load)
            except (RuntimeError, TypeError):
                pass
            done(ok, (time.perf_counter() - started) * 1000)

        self.page.loadFinished.connect(on_load)
        self._js("document.getElementById('next').click(); 'clicked'", lambda _: None)

    # ---- background colour (white-flash) --------------------------------
    def step_background_pixel(self):
        """ch2's document sets no background, so what the user actually sees is
        whatever QWebEnginePage.setBackgroundColor() painted underneath. Grab a
        real pixel off the screen to prove it."""
        R = self.R

        def measure(v):
            R.info("bg.documentBackground(ch2)",
                   f"html={v.get('html')!r} body={v.get('body')!r}")
            # let the compositor actually present the new frame before grabbing
            QTimer.singleShot(400, grab)

        def grab():
            try:
                # QWebEngineView.grab() DOES capture composited web content in
                # Qt 6.11 and is reliable. QScreen.grabWindow(view.winId()) is
                # NOT -- on Windows it returns stale frames.
                pm = self.view.grab()
                if pm.isNull():
                    R.check("bg.pageBackgroundColorIsWhatPaints", False,
                            "view.grab() returned a null pixmap")
                else:
                    img = pm.toImage()
                    c = QColor(img.pixel(img.width() // 2, img.height() - 40))
                    want = self.page.backgroundColor().name()
                    R.check("bg.pageBackgroundColorIsWhatPaints",
                            c.name().lower() == want.lower(),
                            f"pixel={c.name()} page.backgroundColor={want} "
                            f"grab={img.width()}x{img.height()}")
                    stale = self.app.primaryScreen().grabWindow(int(self.view.winId()))
                    R.info("bg.QScreen.grabWindow(winId).comparison",
                           "null" if stale.isNull() else
                           QColor(stale.toImage().pixel(
                               stale.width() // 2, stale.height() - 40)).name())
            except Exception as exc:
                R.check("bg.pageBackgroundColorIsWhatPaints", False, f"exception: {exc!r}")
            self._advance()

        self._js_json(
            "({html: getComputedStyle(document.documentElement).backgroundColor,"
            "  body: getComputedStyle(document.body).backgroundColor})",
            measure,
        )

    # ---- lifetime stress -------------------------------------------------
    def step_stress(self):
        self._stress_before = len(self.handler.served)
        self._stress_n = 0
        self._stress_t = time.perf_counter()
        self._stress_next()

    def _stress_next(self):
        if self._stress_n >= STRESS_LOADS:
            R = self.R
            served = len(self.handler.served) - self._stress_before
            R.check("stress.survived",
                    served >= STRESS_LOADS * len(make_book.CH1_REQUIRED),
                    f"{STRESS_LOADS} loads, {served} sub-resource requests served, "
                    f"{(time.perf_counter() - self._stress_t) * 1000:.0f} ms total")
            R.eq("stress.handler.errors", self.handler.errors, [])
            R.eq("stress.handler.misses", self.handler.missed, [])

            def got(d):
                d = d or {}
                R.eq("stress.final.pngWidth", d.get("pngW"), make_book.PNG_W)
                R.eq("stress.final.jpgWidth", d.get("jpgW"), make_book.JPG_W)
                R.eq("stress.final.cssColor", d.get("css"), "rgb(1, 2, 3)")
                R.eq("stress.final.themeColor", d.get("theme"), "rgb(7, 7, 7)")
                R.check("paint.entriesOnWarmRenderer", bool(d.get("paint")),
                        f"{d.get('paint')}")
                self._resource_count_secure = d.get("resourceCount")
                self._advance()

            # the last stress load also waits for its probe, so images are decoded
            def after_probe(_p):
                self._js_json(
                    "({pngW: document.getElementById('png').naturalWidth,"
                    " jpgW: document.getElementById('jpg').naturalWidth,"
                    " css: getComputedStyle(document.querySelector('#css-probe')).color.trim(),"
                    " theme: getComputedStyle(document.querySelector('#theme-probe')).color.trim(),"
                    " resourceCount: performance.getEntriesByType('resource').length,"
                    " resNames: performance.getEntriesByType('resource')"
                    "            .map(e => e.name.split('/').pop()),"
                    " paint: performance.getEntriesByType('paint')"
                    "            .map(e => [e.name, Math.round(e.startTime)])})",
                    got,
                )

            self._await_probe(after_probe, timeout_ms=5000)
            return

        self._stress_n += 1
        gc.collect()

        def done(ok, _ms):
            if not ok:
                self.R.check(f"stress.load[{self._stress_n}]", False, "load failed")
            self._stress_next()

        self._load(f"epub://{HOST}/OEBPS/text/ch1.xhtml", done)

    # ---- why Resource Timing is empty ------------------------------------
    def step_resource_timing_causality(self):
        """On a LocalScheme document, explicitly setting
        LocalContentCanAccessFileUrls=False suppresses the Resource Timing
        buffer. Prove causality by flipping it and reloading.
        (The reader wants False anyway: a book must not read file:// URLs.
        Losing Resource Timing costs us nothing -- Navigation and Paint Timing
        both still work.)"""
        R = self.R
        A = QWebEngineSettings.WebAttribute
        EXPR = ("({n: performance.getEntriesByType('resource').length,"
                "  names: performance.getEntriesByType('resource')"
                "          .map(e => e.name.split('/').pop())})")

        R.eq("resourceTiming.withFileUrlsDenied(secure default)",
             self._resource_count_secure, 0)

        def phase_allow():
            self.settings.setAttribute(A.LocalContentCanAccessFileUrls, True)
            self._load(f"epub://{HOST}/OEBPS/text/ch1.xhtml",
                       lambda ok, ms: self._load(
                           f"epub://{HOST}/OEBPS/text/ch1.xhtml",
                           lambda ok2, ms2: QTimer.singleShot(
                               300, lambda: self._js_json(EXPR, measured))))

        def measured(d):
            d = d or {}
            R.check("resourceTiming.withFileUrlsAllowed",
                    (d.get("n") or 0) >= 3,
                    f"count={d.get('n')} names={d.get('names')} "
                    f"-- proves the ONLY cause is LocalContentCanAccessFileUrls")
            # restore the secure value
            self.settings.setAttribute(A.LocalContentCanAccessFileUrls, False)
            R.eq("settings.restored.LocalContentCanAccessFileUrls",
                 self.settings.testAttribute(A.LocalContentCanAccessFileUrls), False)
            self._advance()

        phase_allow()

    # ---- 404 path --------------------------------------------------------
    def step_negative_404(self):
        def done(ok, _ms):
            self.R.check("missing.resource.loadFails", not ok,
                         f"loadFinished(ok={ok}) with ErrorPageEnabled=False")
            self.R.check("missing.resource.recorded",
                         "OEBPS/text/nope.xhtml" in self.handler.missed,
                         f"missed={self.handler.missed}")
            self._advance()

        self._load(f"epub://{HOST}/OEBPS/text/nope.xhtml", done)

    # ------------------------------------------------------------------
    def finish(self):
        if self._done:
            return
        self._done = True
        R = self.R
        R.info("env.QTWEBENGINE_CHROMIUM_FLAGS", os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", ""))
        R.info("timing.importToViewReady", f"{self.t_view_ready:.0f} ms")
        R.info("timing.profileConstruct", f"{self.t_profile:.1f} ms")
        R.info("timing.firstLoadFinished(cold)", f"{getattr(self, 't_ch1_load', -1):.0f} ms")
        R.info("timing.totalProcess", f"{(time.perf_counter() - T0) * 1000:.0f} ms")
        R.info("handler.totalRequestsServed", str(len(self.handler.served)))
        if self.page.console_log:
            R.info("js.console", " | ".join(self.page.console_log[:6]))
        R.dump()
        code = 1 if R.failures else 0
        self.view.close()
        QTimer.singleShot(0, lambda: self.app.exit(code))


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    book_path = os.path.join(here, "proto_book.epub")
    make_book.build(book_path)
    with open(book_path, "rb") as fh:
        book = fh.read()

    app = QApplication(sys.argv)
    print(f"Qt {qVersion()}  Python {sys.version.split()[0]}  "
          f"QApplication up at {(time.perf_counter() - T0) * 1000:.0f} ms")

    proto = Proto(app, book)

    # Hard watchdog: never leave a process behind.
    def watchdog():
        if not proto._done:
            proto.R.check("watchdog", False, "timed out before all steps finished")
            proto.finish()

    QTimer.singleShot(15000, watchdog)
    QTimer.singleShot(0, proto.start)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
