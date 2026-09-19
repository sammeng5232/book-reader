#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Live verification of ``webhost.py`` (owner D) in a real QWebEngineView.

Run directly (prints a PASS/FAIL table, exit code 0 = all PASS)::

    C:\\Users\\mengz\\AppData\\Local\\Programs\\Python\\Python314\\python.exe ^
        C:\\Users\\mengz\\epub-reader\\tests\\test_webhost.py

Under ``unittest`` discovery the live run happens in a CHILD process (QtWebEngine
must be initialised before any QApplication, and a GUI must not leak into the
rest of the suite); the test asserts the child exited 0.

What is proven, by running code, against tests\\fixtures and one of the user's
real Chinese books (opened read-only, never modified):

* scheme registered before QApplication; Chromium flags; resource_path() from
  source and from ``sys._MEIPASS``; ``charset=utf-8`` on every text type;
* documents: ``document.characterSet == 'UTF-8'``, real CJK text, every
  ``img.naturalWidth > 0``, embedded fonts ``loaded`` per ``document.fonts``
  (IDPF- and Adobe-obfuscated ones included, with a negative control that
  serves the RAW bytes and must fail), zip stylesheets applied (computed style);
* every served entry went through ``EpubBook.resolve()`` and ``read()/read_text()``;
* the bridge in both directions (signals, ask/answer, Python push) with CJK;
* external links (real mouse clicks, ``target=_blank``, mailto, file:) are
  intercepted, never navigated, reported once;
* 120 sequential ``fetch()`` requests + reloads with ``gc.collect()`` between;
* ``set_book()`` A↔B cycles: foreign origins blocked, old book collectable,
  object counts flat; ``close()`` tears down page-before-profile cleanly.

The window is small and the whole run is bounded by a 20 s watchdog.
"""

from __future__ import annotations

import ctypes
import gc
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
import unittest
import weakref
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FIXTURES = os.path.join(HERE, "fixtures")
REAL_BOOK = "C:\\Users\\mengz\\Desktop\\\u6587\u4ef6\\Econ Books\\50\u4eba\u7684\u4e8c\u5341\u5e74_\u6a0a\u7eb2 \u6613\u7eb2\u7b49.epub"
WATCHDOG_MS = 20000


# ===========================================================================
# result table
# ===========================================================================

class Table:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((name, "PASS" if ok else "FAIL", detail))
        return bool(ok)

    def eq(self, name: str, got, want) -> bool:
        return self.check(name, got == want, f"got={got!r} want={want!r}")

    def info(self, name: str, detail: str) -> None:
        self.rows.append((name, "INFO", detail))

    @property
    def failures(self) -> int:
        return sum(1 for _, s, _ in self.rows if s == "FAIL")

    def dump(self) -> None:
        width = min(58, max((len(n) for n, _, _ in self.rows), default=10))
        line = "=" * 118
        print()
        print(line)
        print(f"{'CHECK'.ljust(width)}  STATUS  DETAIL")
        print("-" * 118)
        for name, status, detail in self.rows:
            detail = detail.replace("\n", " ")
            if len(detail) > 220:
                detail = detail[:217] + "..."
            print(f"{name.ljust(width)}  {status:<6}  {detail}")
        print(line)
        n_pass = sum(1 for _, s, _ in self.rows if s == "PASS")
        n_info = sum(1 for _, s, _ in self.rows if s == "INFO")
        print(f"PASS={n_pass}  FAIL={self.failures}  INFO={n_info}")


def private_bytes() -> int:
    """Private bytes of this process (Windows), 0 elsewhere."""
    if os.name != "nt":
        return 0

    class PMC(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                    ("PrivateUsage", ctypes.c_size_t)]

    pmc = PMC()
    pmc.cb = ctypes.sizeof(PMC)
    try:
        k32 = ctypes.WinDLL("kernel32")
        psapi = ctypes.WinDLL("psapi")
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
        if psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
            return int(pmc.PrivateUsage)
    except Exception:  # noqa: BLE001
        pass
    return 0


# ===========================================================================
# book proxies used as instruments
# ===========================================================================

class SpyBook:
    """Delegates to a real EpubBook and records resolve/read/read_text calls."""

    def __init__(self, book) -> None:
        self._book = book
        self.resolved: list[tuple[str, str]] = []
        self.read_names: list[str] = []
        self.read_text_names: list[str] = []

    def __getattr__(self, name):
        return getattr(self._book, name)

    def resolve(self, href, base=""):
        out = self._book.resolve(href, base)
        self.resolved.append((href, out[0]))
        return out

    def read(self, zip_name):
        self.read_names.append(zip_name)
        return self._book.read(zip_name)

    def read_text(self, zip_name):
        self.read_text_names.append(zip_name)
        return self._book.read_text(zip_name)


class RawFontBook:
    """Negative control: serves fonts WITHOUT de-obfuscation, on its own origin."""

    def __init__(self, book) -> None:
        self._book = book
        self._zip = zipfile.ZipFile(book.path)
        self.path = book.path + "#raw-font-control"

    def __getattr__(self, name):
        return getattr(self._book, name)

    def read(self, zip_name):
        if zip_name.lower().endswith((".ttf", ".otf", ".woff", ".woff2")):
            return self._zip.read(zip_name)
        return self._book.read(zip_name)

    def close_control(self) -> None:
        self._zip.close()


class StubBook:
    """Just enough of EpubBook for EpubSchemeHandler._payload()."""

    def __init__(self, entries: dict[str, bytes]) -> None:
        self.entries = entries

    def read(self, name):
        return self.entries[name]

    def read_text(self, name):
        return self.entries[name].decode("utf-8")


# ===========================================================================
# JavaScript used by the harness (ApplicationWorld)
# ===========================================================================

PROBE_JS = r"""
const afterLoad = () => new Promise(r => {
  if (document.readyState === 'complete') { r(); }
  else { addEventListener('load', () => r(), { once: true }); }
});
await afterLoad();
await Promise.all(Array.from(document.images).map(i => i.complete ? 0 : new Promise(r => {
  i.addEventListener('load', r, { once: true }); i.addEventListener('error', r, { once: true });
})));
const faces = []; document.fonts.forEach(f => faces.push(f));
const fonts = [];
for (const f of faces) {
  try { await f.load(); } catch (e) { /* status says 'error' */ }
  fonts.push({ family: f.family.replace(/^["']|["']$/g, ''), status: f.status });
}
try { await document.fonts.ready; } catch (e) {}
const S = __STYLES__;
const styles = {};
for (const k in S) {
  const el = document.querySelector(S[k][0]);
  styles[k] = el ? getComputedStyle(el).getPropertyValue(S[k][1]).trim() : null;
}
const B = window.__epubReaderBoot || null;
return {
  href: location.href, origin: location.origin, charset: document.characterSet,
  contentType: document.contentType, title: document.title,
  text: document.body ? document.body.textContent : '',
  images: Array.from(document.images).map(i => ({ src: i.getAttribute('src'), w: i.naturalWidth, h: i.naturalHeight })),
  fonts: fonts, styles: styles,
  light: matchMedia('(prefers-color-scheme: light)').matches,
  dark: matchMedia('(prefers-color-scheme: dark)').matches,
  creation: B && B.creation, connected: !!(B && B.connected), queued: B ? B.queued : -1,
  readerCss: !!document.getElementById('__er_reader'),
  engine: typeof window.epubReader, host: typeof window.epubReaderHost,
  isSecureContext: window.isSecureContext
};
"""

HEADERS_JS = r"""
const out = {};
for (const u of __URLS__) {
  try {
    const r = await fetch(u, { cache: 'no-store' });
    const b = await r.arrayBuffer();
    out[u] = { status: r.status, ct: r.headers.get('content-type'), csp: r.headers.get('content-security-policy'), len: b.byteLength };
  } catch (e) { out[u] = { error: String(e) }; }
}
return out;
"""

SHA_JS = r"""
const r = await fetch(__URL__, { cache: 'no-store' });
const b = await r.arrayBuffer();
const d = await crypto.subtle.digest('SHA-256', b);
return { len: b.byteLength, sha: Array.from(new Uint8Array(d)).map(x => x.toString(16).padStart(2, '0')).join('') };
"""

# CONTRACT section 2.1 walker (INFO only: the invariant belongs to owners A and C).
FLAT_JS = r"""
const out = [];
if (!document.body) { return ''; }
const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
let n;
while ((n = w.nextNode())) {
  let p = n.parentNode, skip = false;
  while (p && p !== document.documentElement) {
    const t = (p.localName || '').toLowerCase();
    if (t === 'script' || t === 'style' || t === 'noscript') { skip = true; break; }
    p = p.parentNode;
  }
  if (!skip) { out.push(n.nodeValue); }
}
return out.join('');
"""

ANCHORS_JS = r"""
const mk = (id, href, target, top) => {
  const a = document.createElement('a');
  a.id = id; a.setAttribute('href', href); if (target) { a.target = target; }
  a.textContent = id;
  a.style.cssText = 'position:fixed;left:12px;top:' + top + 'px;z-index:2147483647;display:block;' +
    'width:300px;height:30px;font:bold 20px sans-serif;background:#ff0;color:#000;margin:0;padding:0';
  document.body.appendChild(a);
  const r = a.getBoundingClientRect();
  return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) };
};
return {
  ext: mk('er-ext', 'https://example.com/path?q=1', '', 10),
  blank: mk('er-blank', 'https://example.org/blank', '_blank', 50),
  dup: mk('er-dup', 'https://example.net/dup', '', 90),
  mail: mk('er-mail', 'mailto:someone@example.com', '', 130),
  file: mk('er-file', 'file:///C:/Windows/win.ini', '', 170),
  href: location.href
};
"""


# ===========================================================================
# checks that need no QApplication (run BEFORE it exists)
# ===========================================================================

def pre_app_checks(T: Table, webhost) -> None:
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtWebEngineCore import QWebEngineUrlScheme

    T.check("pre.noQApplicationYet", QCoreApplication.instance() is None,
            "webhost imported before QApplication")
    scheme = QWebEngineUrlScheme.schemeByName(b"epub")
    T.eq("scheme.registered.atImport", bytes(scheme.name().data()), b"epub")
    T.eq("scheme.syntax", scheme.syntax(), QWebEngineUrlScheme.Syntax.Host)
    F = QWebEngineUrlScheme.Flag
    flags = scheme.flags()
    got = {n: bool(flags & getattr(F, n)) for n in (
        "SecureScheme", "LocalScheme", "LocalAccessAllowed", "CorsEnabled",
        "FetchApiAllowed", "ViewSourceAllowed", "NoAccessAllowed")}
    T.eq("scheme.flags", got, {"SecureScheme": True, "LocalScheme": True,
                               "LocalAccessAllowed": True, "CorsEnabled": True,
                               "FetchApiAllowed": True, "ViewSourceAllowed": False,
                               "NoAccessAllowed": False})
    webhost.register_epub_scheme()
    webhost.register_epub_scheme()
    T.check("scheme.register.idempotent", True, "called twice more, no error")
    env = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
    T.check("flags.preferredColorScheme.light", "preferredColorScheme=1" in env, env)

    parts = ["--foo", "--blink-settings=imagesEnabled=true,preferredColorScheme=0"]
    webhost._merge_flag(parts, "--blink-settings=preferredColorScheme=1")
    T.eq("flags.blinkSettings.merged", parts,
         ["--foo", "--blink-settings=imagesEnabled=true,preferredColorScheme=1"])

    want_src = os.path.join(ROOT, "assets", "reader.js")
    T.eq("resource_path.fromSource", os.path.normcase(webhost.resource_path("assets", "reader.js")),
         os.path.normcase(want_src))
    with tempfile.TemporaryDirectory(prefix="er_meipass_") as tmp:
        os.makedirs(os.path.join(tmp, "assets"))
        with open(os.path.join(tmp, "assets", "reader.js"), "w", encoding="utf-8") as fh:
            fh.write("/* frozen 冻结 */")
        sys._MEIPASS = tmp  # type: ignore[attr-defined]
        try:
            T.eq("resource_path.fromMEIPASS", webhost.resource_path("assets", "reader.js"),
                 os.path.join(tmp, "assets", "reader.js"))
            T.eq("read_asset.fromMEIPASS.utf8", webhost.read_asset("reader.js"), "/* frozen 冻结 */")
        finally:
            del sys._MEIPASS  # type: ignore[attr-defined]
    T.check("resource_path.restored", webhost.resource_path("x") == os.path.join(ROOT, "x"), "")

    mimes = {n: webhost.guess_mime(n) for n in (
        "OEBPS/a.xhtml", "b.HTML", "c.css", "d.js", "e.svg", "f.woff2", "g.ttf",
        "h.png", "i.JPG", "j.ncx", "k.opf", "l.unknown")}
    T.eq("guess_mime.table", mimes, {
        "OEBPS/a.xhtml": b"application/xhtml+xml", "b.HTML": b"text/html", "c.css": b"text/css",
        "d.js": b"application/javascript", "e.svg": b"image/svg+xml", "f.woff2": b"font/woff2",
        "g.ttf": b"font/ttf", "h.png": b"image/png", "i.JPG": b"image/jpeg",
        "j.ncx": b"application/x-dtbncx+xml", "k.opf": b"application/oebps-package+xml",
        "l.unknown": b"application/octet-stream"})

    handler = webhost.EpubSchemeHandler(None)
    handler.set_book(StubBook({
        "a.xhtml": "\ufeff<p>中文</p>".encode("utf-8"), "b.html": b"<p>x</p>",
        "c.css": b"p{}", "d.js": b"var a=1;", "e.svg": b"<svg/>", "f.png": b"\x89PNG",
    }), "book")
    types = {}
    for name in ("a.xhtml", "b.html", "c.css", "d.js", "e.svg", "f.png"):
        mime = webhost.guess_mime(name)
        if mime in webhost.DOCUMENT_MIME:
            mime = handler.content_document_mime
        body, ctype = handler._payload(name, mime)
        types[name] = ctype
        if name == "a.xhtml":
            T.eq("payload.xhtml.bomStripped.utf8", body, "<p>中文</p>".encode("utf-8"))
    T.eq("payload.contentTypes.charset", types, {
        "a.xhtml": b"text/html; charset=utf-8", "b.html": b"text/html; charset=utf-8",
        "c.css": b"text/css; charset=utf-8", "d.js": b"application/javascript; charset=utf-8",
        "e.svg": b"image/svg+xml; charset=utf-8", "f.png": b"image/png"})

    class P:
        path = os.path.join(FIXTURES, "odd_paths.epub")

    class Q:
        path = os.path.join(FIXTURES, "images_fonts.epub")

    T.check("host_id_for.distinctPerBook",
            webhost.host_id_for(P()) != webhost.host_id_for(Q())
            and webhost.host_id_for(P()) == webhost.host_id_for(P())
            and webhost.host_id_for(None) == "book",
            f"{webhost.host_id_for(P())} {webhost.host_id_for(Q())}")


# ===========================================================================
# the live harness
# ===========================================================================

TIMEOUT = object()


class Wait:
    def __init__(self, start, timeout_ms: int, label: str) -> None:
        self.start = start
        self.timeout_ms = timeout_ms
        self.label = label


def run_live(T: Table) -> int:
    import webhost  # noqa: F401  (already imported by main, before QApplication)
    from epublib import EpubBook
    import shiboken6
    from PySide6.QtCore import QPoint, Qt, QTimer, QUrl, qInstallMessageHandler
    from PySide6.QtTest import QTest
    from PySide6.QtWebEngineCore import (QWebEnginePage, QWebEngineProfile,
                                         QWebEngineSettings)
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QApplication

    qt_messages: list[str] = []

    def on_qt_message(_mode, _ctx, message):
        qt_messages.append(str(message))

    qInstallMessageHandler(on_qt_message)

    app = QApplication(sys.argv)
    t0 = time.perf_counter()
    host = webhost.BookHost()
    T.info("timing.BookHost()", f"{(time.perf_counter() - t0) * 1000:.0f} ms (profile construction)")

    view = QWebEngineView()
    view.setWindowTitle("EPUB Reader - webhost test")
    view.resize(760, 520)
    view.move(60, 60)
    host.attach(view)
    view.show()
    view.raise_()

    rec = {"internal": [], "external": [], "blocked": [], "location": [], "load": [],
           "position": [], "selection": [], "keys": [], "notes": [], "ready": 0,
           "domReady": 0, "messages": []}
    host.internalLinkClicked.connect(lambda z, f: rec["internal"].append((z, f)))
    host.externalLinkRequested.connect(lambda u: rec["external"].append(u))
    host.navigationBlocked.connect(lambda u, r: rec["blocked"].append((u, r)))
    host.locationChanged.connect(lambda z, f, i: rec["location"].append((z, f, i)))
    host.loadFinished.connect(lambda ok: rec["load"].append(ok))
    b = host.bridge
    b.positionChanged.connect(lambda s: rec["position"].append(s))
    b.selectionChanged.connect(lambda s: rec["selection"].append(s))
    b.keyUnhandled.connect(lambda k: rec["keys"].append(k))
    b.noteRequested.connect(lambda n: rec["notes"].append(n))
    b.ready.connect(lambda: rec.__setitem__("ready", rec["ready"] + 1))
    b.domReady.connect(lambda: rec.__setitem__("domReady", rec["domReady"] + 1))

    state = {"done": False, "probe_id": 0, "exit": 1}
    opened: list = []

    # ---- wait primitives ---------------------------------------------------
    def w_sleep(ms: int) -> Wait:
        return Wait(lambda resume: QTimer.singleShot(ms, lambda: resume(True)), ms + 2000, "sleep")

    def w_load(action, timeout_ms: int = 8000) -> Wait:
        def start(resume):
            def on(ok):
                try:
                    host.loadFinished.disconnect(on)
                except (RuntimeError, TypeError):
                    pass
                resume(bool(ok))
            host.loadFinished.connect(on)
            action()
        return Wait(start, timeout_ms, "load")

    def w_js(script: str, world=None) -> Wait:
        return Wait(lambda resume: host.run_js(script, resume, world=world), 5000, "js")

    def w_json(expr: str) -> Wait:
        return Wait(lambda resume: host.run_json(expr, resume), 5000, "json")

    def w_until(pred, timeout_ms: int = 4000, every_ms: int = 15) -> Wait:
        def start(resume):
            deadline = time.monotonic() + timeout_ms / 1000

            def poll():
                if pred():
                    resume(True)
                elif time.monotonic() > deadline:
                    resume(False)
                else:
                    QTimer.singleShot(every_ms, poll)
            poll()
        return Wait(start, timeout_ms + 2000, "until")

    def w_async(body: str, timeout_ms: int = 8000) -> Wait:
        state["probe_id"] += 1
        pid = state["probe_id"]
        script = (
            "(function(){ window.__erProbe = window.__erProbe || {}; var ID = " + str(pid) + ";"
            " Promise.resolve().then(async function () {\n" + body + "\n})"
            ".then(function (v) { window.__erProbe[ID] = {ok: true, value: (v === undefined ? null : v)}; },"
            " function (e) { window.__erProbe[ID] = {ok: false, error: String(e && e.stack || e)}; });"
            " return true; })()"
        )

        def start(resume):
            deadline = time.monotonic() + timeout_ms / 1000
            host.run_js(script)

            def poll():
                def got(v):
                    if v:
                        resume(v.get("value") if v.get("ok") else {"__error": v.get("error")})
                    elif time.monotonic() > deadline:
                        resume(None)
                    else:
                        QTimer.singleShot(15, poll)
                host.run_json("(window.__erProbe && window.__erProbe[" + str(pid) + "]) || null", got)
            QTimer.singleShot(0, poll)
        return Wait(start, timeout_ms + 2000, "async")

    def probe(styles: dict | None = None) -> Wait:
        return w_async(PROBE_JS.replace("__STYLES__", json.dumps(styles or {})))

    # ---- driver ------------------------------------------------------------
    def finish():
        if state["done"]:
            return
        state["done"] = True
        T.info("timing.sections(ms)", " ".join(laps["rows"]))
        T.info("qt.messages", " | ".join(qt_messages[:8]) or "(none)")
        try:
            if not host.closed:
                T.info("js.console", " | ".join(host.page.console_log[:6]) or "(none)")
        except Exception:  # noqa: BLE001
            pass
        T.dump()
        state["exit"] = 1 if T.failures else 0
        try:
            view.close()
        except Exception:  # noqa: BLE001
            pass
        QTimer.singleShot(0, lambda: app.exit(state["exit"]))

    def drive(gen):
        def step(value=None):
            if state["done"]:
                return
            try:
                req = gen.send(value)
            except StopIteration:
                finish()
                return
            except Exception:  # noqa: BLE001
                T.check("harness.exception", False, traceback.format_exc()[-900:])
                finish()
                return
            fired = [False]

            def resume(v=None):
                if fired[0]:
                    return
                fired[0] = True
                if v is TIMEOUT:
                    T.info("harness.timeout", req.label)
                    v = None
                QTimer.singleShot(0, lambda: step(v))

            try:
                req.start(resume)
            except Exception:  # noqa: BLE001
                T.check("harness.exception", False, traceback.format_exc()[-900:])
                finish()
                return
            QTimer.singleShot(req.timeout_ms, lambda: resume(TIMEOUT))
        QTimer.singleShot(0, step)

    def watchdog():
        if not state["done"]:
            T.check("watchdog", False, f"not finished within {WATCHDOG_MS} ms")
            finish()

    QTimer.singleShot(WATCHDOG_MS, watchdog)

    # ---- helpers used by the scenario --------------------------------------
    def font_status(p, family):
        for f in (p or {}).get("fonts") or []:
            if f.get("family") == family:
                return f.get("status")
        return None

    def imgs_ok(p):
        images = (p or {}).get("images") or []
        return bool(images) and all(i.get("w", 0) > 0 for i in images), images

    def ascii_detail(s: str, n: int = 60) -> str:
        return s[:n]

    def open_book(path):
        book = EpubBook.open(path)
        opened.append(book)
        return book

    def text_payload(book, name):
        return len(book.read_text(name).lstrip("\ufeff").encode("utf-8"))

    # =======================================================================
    # the scenario
    # =======================================================================
    laps = {"t": time.perf_counter(), "rows": []}

    def lap(label):
        now = time.perf_counter()
        laps["rows"].append(f"{label}={(now - laps['t']) * 1000:.0f}")
        laps["t"] = now

    def scenario():
        R = T
        # ---- static ----------------------------------------------------------
        prof = host.profile
        R.check("profile.offTheRecord", prof.isOffTheRecord(), f"objectName={prof.objectName()!r}")
        R.check("profile.handlerInstalled", prof.urlSchemeHandler(b"epub") is host.handler, "")
        names = [s.name() for s in prof.scripts().toList()]
        R.eq("profile.scripts", names, ["epub_reader_boot", "epub_reader_ready"])
        R.eq("assets.missing", host.missing_assets, [])
        kids = host.children()
        R.check("teardown.order.pageBeforeProfile",
                host.page in kids and prof in kids and kids.index(host.page) < kids.index(prof),
                f"children={[type(k).__name__ for k in kids]}")
        A = QWebEngineSettings.WebAttribute
        st = prof.settings()
        R.eq("settings.core", {
            "JavascriptEnabled": st.testAttribute(A.JavascriptEnabled),
            "LocalContentCanAccessFileUrls": st.testAttribute(A.LocalContentCanAccessFileUrls),
            "LocalContentCanAccessRemoteUrls": st.testAttribute(A.LocalContentCanAccessRemoteUrls),
            "ErrorPageEnabled": st.testAttribute(A.ErrorPageEnabled),
            "JavascriptCanOpenWindows": st.testAttribute(A.JavascriptCanOpenWindows),
            "ForceDarkMode": st.testAttribute(A.ForceDarkMode)}, {
            "JavascriptEnabled": True, "LocalContentCanAccessFileUrls": False,
            "LocalContentCanAccessRemoteUrls": False, "ErrorPageEnabled": False,
            "JavascriptCanOpenWindows": False, "ForceDarkMode": False})

        # ---- 1. images_fonts.epub through a spy ----------------------------------
        spy = SpyBook(open_book(os.path.join(FIXTURES, "images_fonts.epub")))
        host.set_book(spy)
        R.check("set_book.hostIsPerBook", host.book_host.startswith("b") and len(host.book_host) == 13,
                host.book_host)
        ok = yield w_load(lambda: host.navigate("OEBPS/text/chapter1.xhtml"))
        R.check("images_fonts.ch1.loadFinished", ok is True, str(ok))
        p = yield probe({"boxedSize": [".boxed", "font-size"], "boxedFamily": [".boxed", "font-family"],
                         "bg": [".bg", "background-image"]})
        p = p or {}
        R.eq("images_fonts.ch1.characterSet", p.get("charset"), "UTF-8")
        R.eq("images_fonts.ch1.contentType", p.get("contentType"), "text/html")
        R.check("images_fonts.ch1.origin", p.get("origin") == f"epub://{host.book_host}"
                and p.get("isSecureContext") is True, f"{p.get('origin')} secure={p.get('isSecureContext')}")
        good, images = imgs_ok(p)
        R.check("images_fonts.ch1.img.naturalWidth>0", good and len(images) == 3, json.dumps(images))
        R.eq("images_fonts.ch1.font.EpubReaderTest", font_status(p, "EpubReaderTest"), "loaded")
        R.eq("images_fonts.ch1.css.applied(.boxed font-size)", (p.get("styles") or {}).get("boxedSize"), "28px")
        R.check("images_fonts.ch1.css.urlRelativeToCss", "OEBPS/images/check.png" in
                ((p.get("styles") or {}).get("bg") or ""), (p.get("styles") or {}).get("bg") or "")
        R.check("prefers-color-scheme.forcedLight", p.get("light") is True and p.get("dark") is False,
                f"light={p.get('light')} dark={p.get('dark')}")
        creation = p.get("creation") or {}
        R.info("measured.atDocumentCreation(text/html)", json.dumps(creation))
        R.check("boot.readerCssInjected(despite null documentElement)", p.get("readerCss") is True, "")
        R.check("boot.readerJsInjected(window.epubReader)", p.get("engine") == "object"
                and p.get("host") == "object", f"epubReader={p.get('engine')} epubReaderHost={p.get('host')}")
        R.check("bridge.handshake.connected+queueFlushed", p.get("connected") is True and p.get("queued") == 0,
                f"connected={p.get('connected')} queued={p.get('queued')}")
        served = list(host.handler.served)
        R.check("handler.served.fonts+images+css",
                all(n in served for n in ("OEBPS/text/chapter1.xhtml", "OEBPS/styles/main.css",
                                          "OEBPS/images/photo.jpg", "OEBPS/images/check.png",
                                          "OEBPS/images/cover.svg")) and
                any(n.startswith("OEBPS/fonts/TestFont.") for n in served), json.dumps(served))
        resolved_targets = {z for _, z in spy.resolved}
        read_any = set(spy.read_names) | set(spy.read_text_names)
        R.check("served.throughEpubBook.resolve",
                all(n in resolved_targets for n in served), f"resolve calls={len(spy.resolved)}")
        R.check("served.throughEpubBook.read/read_text", all(n in read_any for n in served),
                f"read={len(spy.read_names)} read_text={len(spy.read_text_names)}")
        R.eq("handler.errors", host.handler.errors, [])
        flat = yield w_async(FLAT_JS)
        py_flat = spy.plain_text("OEBPS/text/chapter1.xhtml")
        R.info("invariant2.1.preview(images_fonts ch1)",
               "equal" if flat == py_flat else f"DIFFER js={len(flat or '')} py={len(py_flat)}")

        urls = {n: host.url_for(n).toString() for n in (
            "OEBPS/text/chapter1.xhtml", "OEBPS/styles/main.css", "OEBPS/images/cover.svg",
            "OEBPS/images/check.png", "OEBPS/fonts/TestFont.woff", "OEBPS/content.opf")}
        hdr = yield w_async(HEADERS_JS.replace("__URLS__", json.dumps(list(urls.values()))))
        hdr = hdr or {}
        cts = {n: (hdr.get(u) or {}).get("ct") for n, u in urls.items()}
        R.eq("response.contentType", {k: (v or "").lower().replace(" ", "") for k, v in cts.items()}, {
            "OEBPS/text/chapter1.xhtml": "text/html;charset=utf-8",
            "OEBPS/styles/main.css": "text/css;charset=utf-8",
            "OEBPS/images/cover.svg": "image/svg+xml;charset=utf-8",
            "OEBPS/images/check.png": "image/png",
            "OEBPS/fonts/TestFont.woff": "font/woff",
            "OEBPS/content.opf": "application/oebps-package+xml;charset=utf-8"})
        csps = {n: ((hdr.get(u) or {}).get("csp") or "").lower() for n, u in urls.items()}
        R.check("response.csp.documentsOnly(script-src none)",
                "script-src 'none'" in csps["OEBPS/text/chapter1.xhtml"]
                and "style-src 'self' 'unsafe-inline'" in csps["OEBPS/text/chapter1.xhtml"]
                and not any(v for n, v in csps.items() if n != "OEBPS/text/chapter1.xhtml"),
                csps["OEBPS/text/chapter1.xhtml"][:80])

        lap("images_fonts")
        # ---- 2. internal link click (Chromium navigation path) -------------------
        n_int = len(rec["internal"])
        ok = yield w_load(lambda: host.run_js(
            "document.querySelector('a[href=\"sub/deep.xhtml\"]').click(); true"))
        R.check("link.internal.click.loads", ok is True, str(ok))
        R.eq("link.internal.click.signal", rec["internal"][n_int:n_int + 1], [("OEBPS/text/sub/deep.xhtml", "")])
        R.eq("location.afterClick", (host.current_zip_name, host.current_spine_index),
             ("OEBPS/text/sub/deep.xhtml", 1))
        p = yield probe({"boxedSize": [".boxed", "font-size"]})
        p = p or {}
        good, images = imgs_ok(p)
        R.check("images_fonts.deep.img.twoUp.naturalWidth>0", good and len(images) == 2, json.dumps(images))
        R.eq("images_fonts.deep.font.EpubReaderTest", font_status(p, "EpubReaderTest"), "loaded")

        # ---- 3. linkClicked from the reading layer resolves relative to the doc --
        n_int = len(rec["internal"])
        ok = yield w_load(lambda: host.run_js("epubReaderHost.linkClicked('../chapter1.xhtml'); true"))
        R.check("link.js.relative.loads", ok is True, str(ok))
        R.eq("link.js.relative.resolvedAgainstCurrentDoc", rec["internal"][n_int:n_int + 1],
             [("OEBPS/text/chapter1.xhtml", "")])

        lap("links")
        # ---- 4. bridge, both directions ------------------------------------------
        rec["position"].clear(); rec["selection"].clear(); rec["keys"].clear(); rec["notes"].clear()
        ready0 = rec["ready"]
        host.run_js(
            "epubReaderHost.positionChanged(JSON.stringify({page: 2, pages: 9, gpos: 1234, note: '中文·測試'}));"
            "epubReaderHost.positionChanged({page: 3, pages: 9});"
            "epubReaderHost.selectionChanged('{\"text\":\"汉字\",\"start\":1,\"end\":3}');"
            "epubReaderHost.selectionChanged(null);"
            "epubReaderHost.keyUnhandled('Ctrl+F');"
            "epubReaderHost.noteRequested('hl-7');"
            "epubReaderHost.ready(JSON.stringify({page: 0})); true")
        ok = yield w_until(lambda: len(rec["position"]) >= 2 and len(rec["selection"]) >= 2
                           and rec["keys"] and rec["notes"] and rec["ready"] > ready0)
        R.check("bridge.js->py.signals.arrived", ok is True, "")
        R.eq("bridge.js->py.positionChanged(JSON text + object)", rec["position"][:2],
             [{"page": 2, "pages": 9, "gpos": 1234, "note": "中文·測試"}, {"page": 3, "pages": 9}])
        R.eq("bridge.js->py.selectionChanged", rec["selection"][:2], [{"text": "汉字", "start": 1, "end": 3}, None])
        R.eq("bridge.js->py.keyUnhandled+noteRequested", (rec["keys"][:1], rec["notes"][:1]), (["Ctrl+F"], ["hl-7"]))
        host.bridge.set_responder(lambda kind, payload: {"kind": kind, "echo": payload, "py": "你好"})
        ans = yield w_async("return await new Promise(r => epubReaderHost.ask('ping', {x: '汉字', n: 5}, r));")
        R.eq("bridge.js->py->js.ask/answer", ans, {"kind": "ping", "echo": {"x": "汉字", "n": 5}, "py": "你好"})
        host.bridge.set_responder(None)
        yield w_js("window.__lastHostMsg = null; epubReaderHost.on(function (m) { window.__lastHostMsg = m; }); true")
        host.post_to_page("hello", {"text": "你好, 世界", "n": [1, 2]})
        got = {}

        def poll_msg():
            return bool(got.get("v"))

        def fetch_msg():
            host.run_json("window.__lastHostMsg", lambda v: got.__setitem__("v", v))
            return poll_msg()

        ok = yield w_until(fetch_msg, 3000, 30)
        R.eq("bridge.py->js.post_to_page", got.get("v"), {"kind": "hello", "payload": {"text": "你好, 世界", "n": [1, 2]}})
        v = yield w_json("({a: [1, 2], b: '汉', c: true, d: null})")
        R.eq("run_json.structured", v, {"a": [1, 2], "b": "汉", "c": True, "d": None})
        v = yield w_js("'scalar-' + (6 * 7)")
        R.eq("run_js.scalar", v, "scalar-42")
        R.check("bridge.domReady.perLoad(queued before handshake)", rec["domReady"] >= 3,
                f"domReady={rec['domReady']} epub loads so far=3")

        lap("bridge")
        # ---- 5. external links are intercepted, never navigated -----------------
        pos = yield w_async(ANCHORS_JS)
        pos = pos or {}
        href_before = pos.get("href")
        load_n = len(rec["load"])
        proxy = view.focusProxy() or view

        def click(key):
            pt = pos.get(key) or {"x": 0, "y": 0}
            QTest.mouseClick(proxy, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                             QPoint(int(pt["x"]), int(pt["y"])))

        n_ext = len(rec["external"])
        click("ext")
        yield w_until(lambda: len(rec["external"]) > n_ext, 2500)
        yield w_sleep(150)
        R.eq("link.external.realClick.reported", rec["external"][n_ext:], ["https://example.com/path?q=1"])
        n_ext = len(rec["external"])
        click("blank")
        yield w_until(lambda: len(rec["external"]) > n_ext, 2500)
        R.eq("link.external.targetBlank.reported", rec["external"][n_ext:], ["https://example.org/blank"])
        n_ext = len(rec["external"])
        n_blk = len(rec["blocked"])
        click("dup")
        host.run_js("epubReaderHost.linkClicked('https://example.net/dup'); true")
        yield w_until(lambda: sum(1 for u, _ in rec["blocked"][n_blk:] if "example.net" in u) >= 2, 2500)
        yield w_sleep(150)
        dup_blocked = sum(1 for u, _ in rec["blocked"][n_blk:] if "example.net" in u)
        R.check("link.external.reportedOnce(js+chromium both saw it)",
                rec["external"][n_ext:] == ["https://example.net/dup"] and dup_blocked >= 2,
                f"external={rec['external'][n_ext:]} blocked-sightings={dup_blocked}")
        n_ext = len(rec["external"])
        click("mail")
        yield w_until(lambda: len(rec["external"]) > n_ext, 2500)
        R.eq("link.mailto.reported", rec["external"][n_ext:], ["mailto:someone@example.com"])
        n_ext = len(rec["external"])
        n_blk = len(rec["blocked"])
        click("file")
        yield w_until(lambda: any(r == "scheme" for _, r in rec["blocked"][n_blk:]), 2000)
        yield w_sleep(100)
        R.check("link.file.blockedNotReported", rec["external"][n_ext:] == []
                and any(r == "scheme" for _, r in rec["blocked"][n_blk:]),
                f"blocked={rec['blocked'][n_blk:]}")
        here = yield w_json("location.href")
        R.check("link.external.viewNeverNavigated",
                here == href_before and len(rec["load"]) == load_n
                and host.current_zip_name == "OEBPS/text/chapter1.xhtml",
                f"href={here} loads+={len(rec['load']) - load_n}")
        R.eq("interceptor.blockedSubresources", [u for u in host.interceptor.blocked if "example" in u], [])
        n_ext = len(rec["external"])
        n_blk = len(rec["blocked"])
        host.page.setUrl(QUrl("https://example.com/typed"))
        yield w_until(lambda: len(rec["blocked"]) > n_blk, 2000)
        yield w_sleep(150)
        here = yield w_json("location.href")
        R.check("navigation.external.programmatic.blockedNotReported",
                rec["external"][n_ext:] == [] and ("https://example.com/typed", "external") in rec["blocked"][n_blk:]
                and here == href_before, f"blocked={rec['blocked'][n_blk:]} href={here}")

        # the real reading layer: init it through call_reader(), signals must come back
        rec["position"].clear()
        ready0 = rec["ready"]
        got_init = {}
        host.call_reader("init", {"settings": {}, "locator": None, "mode": "paginated"},
                         callback=lambda v: got_init.__setitem__("v", v))
        ok = yield w_until(lambda: "v" in got_init and rec["ready"] > ready0 and rec["position"], 4000)
        init_v = got_init.get("v") or {}
        R.check("integration.call_reader(init)->ready+positionChanged",
                ok is True and isinstance(init_v, dict) and "pages" in init_v
                and isinstance(rec["position"][-1], dict) and "pages" in rec["position"][-1],
                f"init={json.dumps(init_v)[:120]} position={json.dumps(rec['position'][-1:])[:120]}")
        pos = (yield w_async(ANCHORS_JS)) or {}
        yield w_sleep(100)
        n_ext = len(rec["external"])
        n_blk = len(rec["blocked"])
        click("ext")
        yield w_until(lambda: len(rec["external"]) > n_ext, 2500)
        yield w_sleep(300)
        sightings = sum(1 for u, _ in rec["blocked"][n_blk:] if "example.com/path" in u)
        R.check("integration.readerJsActive.externalClick.reportedOnce",
                rec["external"][n_ext:] == ["https://example.com/path?q=1"],
                f"external={rec['external'][n_ext:]} sightings={sightings}")
        n_ext = len(rec["external"])
        click("mail")
        yield w_until(lambda: len(rec["external"]) > n_ext, 2500)
        yield w_sleep(300)
        R.eq("integration.readerJsActive.mailto.reportedOnce", rec["external"][n_ext:], ["mailto:someone@example.com"])
        here = yield w_json("location.href")
        R.eq("integration.readerJsActive.viewNeverNavigated", here, href_before)

        lap("external")
        # ---- 6. stress: 120 fetches + reloads, gc.collect() between each ---------
        book_if = spy._book
        host.set_book(book_if)  # same file, now without the spy (fresh handler history)
        ok = yield w_load(lambda: host.navigate("OEBPS/text/chapter1.xhtml"))
        names = ["OEBPS/text/chapter1.xhtml", "OEBPS/text/sub/deep.xhtml", "OEBPS/styles/main.css",
                 "OEBPS/images/photo.jpg", "OEBPS/images/check.png", "OEBPS/images/cover.svg",
                 "OEBPS/fonts/TestFont.ttf", "OEBPS/fonts/TestFont.woff", "OEBPS/nav.xhtml",
                 "OEBPS/content.opf"]
        text_ext = (".xhtml", ".css", ".svg", ".opf")
        expect = {n: (text_payload(book_if, n) if n.endswith(text_ext) else len(book_if.read(n)))
                  for n in names}
        results: list = []
        total = 120
        req0 = host.handler.request_count

        def fire(i):
            n = names[i % len(names)]
            host.run_js(
                "fetch(" + json.dumps(host.url_for(n).toString()) + ", {cache: 'no-store'})"
                ".then(r => r.arrayBuffer().then(b => epubReaderHost.send('stress', "
                "{i: " + str(i) + ", s: r.status, n: b.byteLength})))"
                ".catch(e => epubReaderHost.send('stress', {i: " + str(i) + ", err: String(e)}));")

        def on_message(kind, payload):
            if kind != "stress":
                return
            results.append(payload)
            gc.collect()
            if len(results) < total:
                fire(len(results))

        host.bridge.message.connect(on_message)
        t_s = time.perf_counter()
        fire(0)
        ok = yield w_until(lambda: len(results) >= total, 9000, 20)
        host.bridge.message.disconnect(on_message)
        bad = [r for r in results if r.get("s") != 200 or r.get("n") != expect[names[r["i"] % len(names)]]]
        R.check("stress.120fetches.gcBetween.allOk", ok is True and len(results) == total and not bad,
                f"{len(results)} done in {(time.perf_counter() - t_s) * 1000:.0f} ms, bad={bad[:3]}")
        for k in range(2):
            gc.collect()
            ok = yield w_load(lambda: host.page.triggerAction(QWebEnginePage.WebAction.Reload))
            if ok is not True:
                R.check(f"stress.reload[{k}]", False, str(ok))
        served_n = host.handler.request_count - req0
        R.check("stress.requestsServed>=100", served_n >= 100 and host.handler.errors == []
                and host.handler.missed == [], f"requests={served_n} errors={host.handler.errors[:2]} "
                f"missed={host.handler.missed[:3]}")
        p = yield probe({"boxedSize": [".boxed", "font-size"]})
        good, images = imgs_ok(p)
        R.check("stress.afterwards.pageStillCorrect", good and font_status(p, "EpubReaderTest") == "loaded"
                and ((p or {}).get("styles") or {}).get("boxedSize") == "28px", json.dumps(images))

        lap("stress")
        # ---- 7. odd_paths.epub ---------------------------------------------------
        odd = open_book(os.path.join(FIXTURES, "odd_paths.epub"))
        host.set_book(odd)
        for label, zip_name, sentinel, want_imgs in (
            ("space", "text/chapter one.xhtml", "ODD-SPACE-SENTINEL", 1),
            ("cjk", "\u6587\u672c/\u7b2c\u4e00\u7ae0.xhtml", "\u4e2d\u6587\u8def\u5f84\u6807\u8bb0", 0),
            ("hash", "text/a#b.xhtml", "ODD-HASH-SENTINEL", 0),
        ):
            ok = yield w_load(lambda z=zip_name: host.navigate(z))
            p = (yield probe({"border": ["body", "border-top-width"]})) or {}
            imgs = p.get("images") or []
            R.check(f"odd_paths.{label}.served",
                    ok is True and sentinel in (p.get("text") or "") and p.get("charset") == "UTF-8"
                    and (p.get("styles") or {}).get("border") == "3px"
                    and len(imgs) == want_imgs and all(i["w"] > 0 for i in imgs)
                    and host.current_zip_name == zip_name,
                    f"ok={ok} border={(p.get('styles') or {}).get('border')} imgs={imgs} "
                    f"loc={host.current_zip_name!r} href={p.get('href')}")
        ok = yield w_load(lambda: host.page.setUrl(QUrl(f"epub://{host.book_host}/text/mixed.xhtml")))
        p = (yield probe({"border": ["body", "border-top-width"]})) or {}
        R.check("odd_paths.misCasedUrl.served", ok is True and "ODD-MIXED-SENTINEL" in (p.get("text") or "")
                and host.current_zip_name == "Text/MiXeD.xhtml", f"loc={host.current_zip_name!r}")
        ok = yield w_load(lambda: host.navigate("text/chapter one.xhtml"))
        n_int = len(rec["internal"])
        ok = yield w_load(lambda: host.run_js("document.querySelector('a[href^=\"../%E6%96%87\"]').click(); true"))
        R.check("odd_paths.link.cjkFilename+fragment", ok is True and rec["internal"][n_int:n_int + 1]
                == [("\u6587\u672c/\u7b2c\u4e00\u7ae0.xhtml", "sec2")] and host.current_fragment == "sec2",
                f"{rec['internal'][n_int:]} frag={host.current_fragment!r}")
        ok = yield w_load(lambda: host.navigate("text/chapter one.xhtml"))
        n_int = len(rec["internal"])
        ok = yield w_load(lambda: host.run_js("document.querySelector('a[href=\"a%23b.xhtml\"]').click(); true"))
        R.check("odd_paths.link.hashInFilename", ok is True and rec["internal"][n_int:n_int + 1]
                == [("text/a#b.xhtml", "")], f"{rec['internal'][n_int:]}")
        n_blk = len(rec["blocked"])
        n_int = len(rec["internal"])
        yield w_js("var a = document.createElement('a'); a.id = 'er-missing'; a.setAttribute('href', 'nope%20missing.xhtml');"
                   " a.textContent = 'x'; document.body.appendChild(a); a.click(); true")
        yield w_until(lambda: len(rec["blocked"]) > n_blk, 2000)
        R.check("link.internal.missingEntry.blocked",
                rec["internal"][n_int:] == [] and any(r == "missing" for _, r in rec["blocked"][n_blk:]),
                f"blocked={rec['blocked'][n_blk:]}")
        R.eq("odd_paths.handler.missed", host.handler.missed, [])

        lap("odd_paths")
        # ---- 8. cjk_vertical.epub ------------------------------------------------
        cjk = open_book(os.path.join(FIXTURES, "cjk_vertical.epub"))
        cjk_ref = weakref.ref(cjk)
        host.set_book(cjk)
        ok = yield w_load(lambda: host.navigate("OPS/text/ch1.xhtml"))
        p = (yield probe({"family": ["body", "font-family"]})) or {}
        R.check("cjk_vertical.ch1.utf8+cjkText", ok is True and p.get("charset") == "UTF-8"
                and "\u6a2a\u6392\u6b63\u6587" in (p.get("text") or "")
                and "\u300c\u8fd9\u6837\u300d" in (p.get("text") or ""),
                f"charset={p.get('charset')} text={ascii_detail((p.get('text') or '').strip())!r}")
        R.check("cjk_vertical.ch1.css.applied", "Noto Serif CJK SC" in ((p.get("styles") or {}).get("family") or ""),
                (p.get("styles") or {}).get("family") or "")
        flat = yield w_async(FLAT_JS)
        py_flat = cjk.plain_text("OPS/text/ch1.xhtml")
        R.info("invariant2.1.preview(cjk ch1)", "equal" if flat == py_flat
               else f"DIFFER js={len(flat or '')} py={len(py_flat)}")
        ok = yield w_load(lambda: host.navigate("OPS/text/ch2.xhtml"))
        p = (yield probe({"wm": [".vertical", "writing-mode"]})) or {}
        R.check("cjk_vertical.ch2.verticalRl", ok is True and (p.get("styles") or {}).get("wm") == "vertical-rl"
                and "\u7ad6\u6392\u6807\u8bb0" in (p.get("text") or ""), f"wm={(p.get('styles') or {}).get('wm')}")

        lap("cjk")
        # ---- 9. obfuscated_fonts.epub (IDPF) --------------------------------------
        obf = open_book(os.path.join(FIXTURES, "obfuscated_fonts.epub"))
        host.set_book(obf)
        ok = yield w_load(lambda: host.navigate("OEBPS/text/ch1.xhtml"))
        p = (yield probe()) or {}
        R.check("obfuscated_fonts.idpf.fontsLoaded",
                ok is True and font_status(p, "ObfTest") == "loaded" and font_status(p, "PlainTest") == "loaded",
                json.dumps(p.get("fonts")))
        sha = (yield w_async(SHA_JS.replace("__URL__", json.dumps(host.url_for("OEBPS/fonts/Obfuscated.ttf").toString())))) or {}
        with zipfile.ZipFile(obf.path) as zf:
            raw = zf.read("OEBPS/fonts/Obfuscated.ttf")
        R.check("obfuscated_fonts.servedBytes==EpubBook.read()",
                sha.get("sha") == hashlib.sha256(obf.read("OEBPS/fonts/Obfuscated.ttf")).hexdigest()
                and sha.get("sha") != hashlib.sha256(raw).hexdigest(), f"len={sha.get('len')}")
        control = RawFontBook(obf)
        host.set_book(control)
        ok = yield w_load(lambda: host.navigate("OEBPS/text/ch1.xhtml"))
        p = (yield probe()) or {}
        R.check("obfuscated_fonts.negativeControl.rawBytesFail",
                font_status(p, "ObfTest") == "error" and font_status(p, "PlainTest") == "loaded",
                json.dumps(p.get("fonts")))
        control.close_control()

        lap("obfuscated")
        # ---- 10. the user's real Chinese book (Adobe-obfuscated fonts) -----------
        real = None
        if os.path.exists(REAL_BOOK):
            real = open_book(REAL_BOOK)
            host.set_book(real)
            ok = yield w_load(lambda: host.navigate("text/part0004.html"))
            p = (yield probe({"rkFamily": [".rk", "font-family"], "rkAlign": [".rk", "text-align"]})) or {}
            text = p.get("text") or ""
            R.check("realbook.part0004.utf8+cjkText", ok is True and p.get("charset") == "UTF-8"
                    and "\u4e2d\u56fd\u7ecf\u6d4e50\u4eba\u8bba\u575b\u4e0e\u9ad8\u8d28\u91cf\u53d1\u5c55" in text,
                    f"charset={p.get('charset')} text={text.strip()[:30]!r}")
            R.check("realbook.part0004.css.applied(.rk)",
                    "kai" in ((p.get("styles") or {}).get("rkFamily") or "")
                    and (p.get("styles") or {}).get("rkAlign") == "right", json.dumps(p.get("styles"), ensure_ascii=False))
            R.check("realbook.adobeObfuscatedFonts.loaded",
                    font_status(p, "kai") == "loaded" and font_status(p, "fangsong") == "loaded",
                    json.dumps(p.get("fonts")))
            flat = yield w_async(FLAT_JS)
            py_flat = real.plain_text("text/part0004.html")
            R.info("invariant2.1.preview(realbook part0004)", "equal" if flat == py_flat
                   else f"DIFFER js={len(flat or '')} py={len(py_flat)}")
            sha = (yield w_async(SHA_JS.replace("__URL__", json.dumps(host.url_for("fonts/00060.ttf").toString())))) or {}
            with zipfile.ZipFile(real.path) as zf:
                raw = zf.read("fonts/00060.ttf")
            R.check("realbook.servedFontBytes==deobfuscated",
                    sha.get("sha") == hashlib.sha256(real.read("fonts/00060.ttf")).hexdigest()
                    and sha.get("sha") != hashlib.sha256(raw).hexdigest(), f"len={sha.get('len')}")
            ok = yield w_load(lambda: host.navigate("text/part0039.html"))
            p = (yield probe()) or {}
            good, images = imgs_ok(p)
            R.check("realbook.part0039.17images.naturalWidth>0", ok is True and good and len(images) == 17,
                    f"n={len(images)} zero={[i['src'] for i in images if not i['w']][:3]}")
            control = RawFontBook(real)
            host.set_book(control)
            ok = yield w_load(lambda: host.navigate("text/part0004.html"))
            p = (yield probe()) or {}
            R.check("realbook.negativeControl.rawAdobeFontsFail",
                    font_status(p, "kai") == "error", json.dumps(p.get("fonts")))
            control.close_control()
        else:
            R.info("realbook", f"not found: {REAL_BOOK}")

        lap("realbook")
        # ---- 11. set_book A <-> B cycles, leaks, foreign origins ------------------
        book_b = real or odd
        b_doc, b_needle = (("text/part0003.html", "\u767d\u91cd\u6069\u7b80\u5386") if real
                           else ("text/chapter one.xhtml", "ODD-SPACE-SENTINEL"))
        host.set_book(book_if)
        host_a = host.book_host
        host.set_book(None)
        R.check("set_book(None).servesNothing", host.handler.book is None and host.book_host == "book", "")
        yield w_sleep(50)
        # cjk was replaced several set_book() calls ago: once closed and dropped,
        # nothing in the host may keep it alive.
        opened.remove(cjk)
        cjk.close()
        del cjk
        gc.collect()
        R.check("set_book.previousBookCollectable", cjk_ref() is None, "weakref to a replaced, closed book")
        objs = []
        mem = []
        cycle_ok = []
        for k in range(3):
            host.set_book(book_if)
            ok_a = yield w_load(lambda: host.navigate("OEBPS/text/chapter1.xhtml"))
            ta = yield w_json("document.body.textContent.indexOf('IMAGES-CH1-SENTINEL') >= 0")
            host.set_book(book_b)
            ok_b = yield w_load(lambda: host.navigate(b_doc))
            tb = yield w_json("document.body.textContent.indexOf(" + json.dumps(b_needle) + ") >= 0")
            cycle_ok.append(ok_a is True and ta is True and ok_b is True and tb is True)
            gc.collect()
            objs.append(len(gc.get_objects()))
            mem.append(private_bytes())
        R.check("set_book.cycles.A<->B.renderCorrectly", all(cycle_ok), f"{cycle_ok}")
        growth = objs[-1] - objs[1]
        R.check("set_book.cycles.pythonObjectsFlat", growth < 1500,
                f"gc objects per cycle={objs} growth(c2->c3)={growth}")
        R.info("set_book.cycles.privateBytes(MB)", " ".join(f"{m / 1048576:.1f}" for m in mem))
        R.check("set_book.cycles.singleProfileAndPage",
                len(host.findChildren(QWebEngineProfile)) == 1 and len(host.findChildren(QWebEnginePage)) == 1
                and host.profile.scripts().count() == 2 and host.handler.book is book_b
                and len(host.handler.served) < 200 and len(host._recent_links) < 10,
                f"profiles={len(host.findChildren(QWebEngineProfile))} pages={len(host.findChildren(QWebEnginePage))} "
                f"served={len(host.handler.served)}")
        n_blk = len(rec["blocked"])
        load_n = len(rec["load"])
        before = host.current_zip_name
        host.page.setUrl(QUrl(f"epub://{host_a}/OEBPS/text/chapter1.xhtml"))
        yield w_until(lambda: any(r == "foreign-origin" for _, r in rec["blocked"][n_blk:]), 2000)
        yield w_sleep(150)
        R.check("set_book.oldOriginBlocked", any(r == "foreign-origin" and host_a in u for u, r in rec["blocked"][n_blk:])
                and len(rec["load"]) == load_n and host.current_zip_name == before,
                f"blocked={rec['blocked'][n_blk:]}")
        R.eq("handler.errors.final", host.handler.errors, [])

        lap("cycles")
        # ---- 12. live asset reload through resource_path(sys._MEIPASS) -----------
        with tempfile.TemporaryDirectory(prefix="er_meipass_live_") as tmp:
            os.makedirs(os.path.join(tmp, "assets"))
            for name, text in (("reader.js", "window.__frozenMarker = '冻结';"), ("reader.css", "/*frozen*/")):
                with open(os.path.join(tmp, "assets", name), "w", encoding="utf-8") as fh:
                    fh.write(text)
            sys._MEIPASS = tmp  # type: ignore[attr-defined]
            try:
                host.reload_assets()
                boot = host.profile.scripts().find("epub_reader_boot")[0].sourceCode()
                R.check("resource_path.frozen.hostUsesMEIPASSAssets",
                        "__frozenMarker" in boot and host._reader_css == "/*frozen*/" and not host.missing_assets, "")
            finally:
                del sys._MEIPASS  # type: ignore[attr-defined]
                host.reload_assets()
        R.check("resource_path.source.restored", "__frozenMarker" not in
                host.profile.scripts().find("epub_reader_boot")[0].sourceCode() and not host.missing_assets, "")

        lap("assets")
        # ---- 13. teardown ------------------------------------------------------------
        page_ref, profile_ref = host.page, host.profile
        host.close()
        host.close()
        yield w_sleep(300)
        R.check("close.pageAndProfileDeleted", not shiboken6.isValid(page_ref) and not shiboken6.isValid(profile_ref),
                f"page valid={shiboken6.isValid(page_ref)} profile valid={shiboken6.isValid(profile_ref)}")
        view.resize(700, 480)
        yield w_sleep(100)
        R.check("close.viewSurvivesPageDeletion", shiboken6.isValid(view), "")
        R.check("close.noProfileReleaseWarning", not any("Expect troubles" in m or "still not deleted" in m
                                                         for m in qt_messages), " | ".join(qt_messages[:4]))
        lap("teardown")
        for book in list(opened):
            book.close()

    drive(scenario())
    code = app.exec()
    return code


def run_quit_path() -> int:
    """A BookHost left open when the application quits: aboutToQuit must close it,
    page before profile, with no Qt warning and a clean process exit."""
    import webhost  # noqa: F401  (before QApplication)
    from epublib import EpubBook
    import shiboken6
    from PySide6.QtCore import QTimer, qInstallMessageHandler
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QApplication

    T = Table()
    messages: list[str] = []
    qInstallMessageHandler(lambda _m, _c, msg: messages.append(str(msg)))
    app = QApplication(sys.argv)
    host = webhost.BookHost()
    view = QWebEngineView()
    view.resize(420, 300)
    view.move(80, 80)
    host.attach(view)
    view.show()
    book = EpubBook.open(os.path.join(FIXTURES, "images_fonts.epub"))
    host.set_book(book)
    loads: list[bool] = []
    host.loadFinished.connect(loads.append)
    refs = {"page": host.page, "profile": host.profile}
    QTimer.singleShot(0, lambda: host.navigate("OEBPS/text/chapter1.xhtml"))
    QTimer.singleShot(1500, app.quit)           # quit with the host still open
    QTimer.singleShot(15000, lambda: app.exit(3))  # watchdog
    code = app.exec()
    T.check("quit.pageLoadedBeforeQuit", loads == [True], f"loads={loads}")
    T.check("quit.aboutToQuit.closedHost", host.closed, "")
    T.check("quit.pageAndProfileDeleted", not shiboken6.isValid(refs["page"])
            and not shiboken6.isValid(refs["profile"]), "")
    T.check("quit.noQtWarnings", not any("Expect troubles" in m or "still not deleted" in m for m in messages),
            " | ".join(messages[:4]) or "(none)")
    T.eq("quit.exitCode", code, 0)
    book.close()
    T.dump()
    return 1 if T.failures else 0


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    if "--quit-path" in sys.argv:
        return run_quit_path()
    T = Table()
    import webhost  # the import itself registers the scheme — BEFORE QApplication

    pre_app_checks(T, webhost)
    try:
        code = run_live(T)
    except Exception:  # noqa: BLE001
        T.check("harness.crashed", False, traceback.format_exc()[-1500:])
        T.dump()
        return 1
    return code


@unittest.skipIf(os.environ.get("EPUB_READER_SKIP_GUI_TESTS") == "1",
                 "EPUB_READER_SKIP_GUI_TESTS=1: live QtWebEngine windows disabled")
class WebHostLiveTest(unittest.TestCase):
    """Runs the live harness in a child process and requires exit code 0.

    Each test opens a small window for well under 20 s.  Set
    ``EPUB_READER_SKIP_GUI_TESTS=1`` to skip them in headless runs.
    """

    def test_live_harness(self) -> None:
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        proc = subprocess.run([sys.executable, os.path.abspath(__file__)], capture_output=True,
                              timeout=120, env=env, encoding="utf-8", errors="replace")
        self.assertEqual(proc.returncode, 0, proc.stdout[-6000:] + "\n" + proc.stderr[-3000:])

    def test_quit_path(self) -> None:
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        proc = subprocess.run([sys.executable, os.path.abspath(__file__), "--quit-path"],
                              capture_output=True, timeout=60, env=env, encoding="utf-8",
                              errors="replace")
        self.assertEqual(proc.returncode, 0, proc.stdout[-3000:] + "\n" + proc.stderr[-3000:])


if __name__ == "__main__":
    sys.exit(main())
