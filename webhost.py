"""Book Reader — QtWebEngine host: custom ``epub://`` scheme, zip-backed request
handler, profile/settings, script injection and the JS↔Python bridge.

===============================================================================
READ THIS BEFORE YOU IMPORT ANYTHING ELSE
===============================================================================
``register_epub_scheme()`` MUST run **before** ``QApplication`` is constructed and
before any ``QWebEngineProfile`` exists.  Registering the scheme later silently
does nothing: Chromium then treats ``epub://`` as an unknown, opaque scheme and
every book fails to load with no useful error.

This module therefore calls ``register_epub_scheme()`` **at import time** — that is
the single documented import-time side effect allowed by ``docs/CONTRACT.md`` §0.
It also appends the Chromium flags in :data:`REQUIRED_CHROMIUM_FLAGS` to
``QTWEBENGINE_CHROMIUM_FLAGS`` at import time, for the same reason (the
environment variable is only read when Chromium starts).

The entry point (owner G) must still call it explicitly as the first statement of
``main()`` — it is idempotent — so the ordering is visible at the call site::

    import webhost            # registration already happened here
    webhost.register_epub_scheme()          # explicit, idempotent, documented
    app = QApplication(sys.argv)            # only now

===============================================================================
WHY EACH NON-OBVIOUS THING IS THE WAY IT IS  (all verified, see docs/research/)
===============================================================================
* ``Content-Type`` always carries ``; charset=utf-8`` for markup/CSS/JS/SVG, and
  text resources are transcoded to UTF-8 before they are served.  Without this
  Chromium re-guesses the encoding and the user's Chinese books render as
  mojibake even though Python decoded them correctly.
* The reply ``QBuffer`` is parented to the ``QWebEngineUrlRequestJob``; C++ owns
  it, no Python reference is kept.  Verified over 100+ requests with a forced
  ``gc.collect()`` after every single one.
* ``prefers-color-scheme`` is forced to *light* with
  ``--blink-settings=preferredColorScheme=1``.  MEASURED on this machine:
  with no flag ``matchMedia('(prefers-color-scheme: dark)').matches`` is ``true``,
  so a book's own dark-mode media queries would fight the app's theme.
* Content documents are served as ``text/html``, not ``application/xhtml+xml``
  (the prototype's choice).  Chromium's XML parser is draconian — one unescaped
  ``&`` replaces the whole chapter with an error page — and ``reader.js`` relies on
  the HTML parser for ``[epub\\:type~=noteref]`` selectors
  (docs/research/epub-spec.md §63/§110, reading-ux-engine.md §119/§164).
* ``qwebchannel.js`` + the bridge facade + ``assets/reader.js`` are ONE
  DocumentCreation script in ApplicationWorld (two scripts at one injection
  point would depend on collection ordering).  MEASURED (tests/test_webhost.py):
  for the ``text/html`` documents served here ``document.documentElement`` is
  still ``null`` at DocumentCreation, so the boot's stylesheet injection falls
  back to a MutationObserver and ``reader.js`` resolves the root lazily.
* Links whose scheme Chromium has no handler for (``mailto:``, ``tel:``…) never
  reach ``acceptNavigationRequest``; a capture-phase click listener in the boot
  script reports them, so they are not silently lost.
* Every byte comes out of ``epublib.EpubBook.read()`` / ``.read_text()``, so font
  de-obfuscation applies transparently, and every path is resolved through
  ``EpubBook.resolve()``, so odd/percent-encoded/CJK/mis-cased relative paths work.
* The origin is ``epub://<per-book-host>``, so two different books (or the same
  file after it changed on disk) can never share a Chromium memory-cache entry
  or a localStorage bucket.
* The page is destroyed **before** the profile (the child order is arranged in
  ``BookHost.__init__``); the other way round Qt prints "Release of profile
  requested but WebEnginePage still not deleted. Expect troubles !".

Public surface: see ``docs/api-webhost.md``.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import sys
import time
from typing import TYPE_CHECKING, Any, Callable, Optional
from urllib.parse import quote, unquote

from PySide6.QtCore import (
    QBuffer,
    QByteArray,
    QCoreApplication,
    QFile,
    QIODevice,
    QObject,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QColor
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import (
    QWebEngineLoadingInfo,
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineScript,
    QWebEngineSettings,
    QWebEngineUrlRequestInfo,
    QWebEngineUrlRequestInterceptor,
    QWebEngineUrlRequestJob,
    QWebEngineUrlScheme,
    QWebEngineUrlSchemeHandler,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from PySide6.QtWebEngineWidgets import QWebEngineView

    from epublib import EpubBook

__all__ = [
    "EPUB_SCHEME",
    "EPUB_SCHEME_NAME",
    "DEFAULT_BOOK_HOST",
    "REQUIRED_CHROMIUM_FLAGS",
    "BOOK_CSP",
    "APP_WORLD",
    "MAIN_WORLD",
    "CHANNEL_OBJECT_NAME",
    "BOOT_SCRIPT_NAME",
    "READY_SCRIPT_NAME",
    "THEME_STYLE_ID",
    "READER_STYLE_ID",
    "EXTERNAL_SCHEMES",
    "MIME",
    "register_epub_scheme",
    "configure_chromium_flags",
    "resource_path",
    "asset_path",
    "read_asset",
    "guess_mime",
    "host_id_for",
    "HostBridge",
    "EpubSchemeHandler",
    "BookPage",
    "BookHost",
]


# ===========================================================================
# 0. Scheme + Chromium flags.  Both MUST happen before QApplication exists.
# ===========================================================================

EPUB_SCHEME: bytes = b"epub"
EPUB_SCHEME_NAME: str = "epub"
DEFAULT_BOOK_HOST: str = "book"

#: MEASURED on this machine: without this flag QtWebEngine reports
#: ``prefers-color-scheme: dark`` (matchMedia dark == True), so a book that ships
#: its own ``@media (prefers-color-scheme: dark)`` rules would repaint itself dark
#: while the app is in 日 or 纸.  ``preferredColorScheme=1`` == light (value 2 is
#: not a valid enum member and leaves *neither* query matching).
REQUIRED_CHROMIUM_FLAGS: tuple[str, ...] = ("--blink-settings=preferredColorScheme=1",)

APP_WORLD = QWebEngineScript.ScriptWorldId.ApplicationWorld
MAIN_WORLD = QWebEngineScript.ScriptWorldId.MainWorld

#: Name the bridge is registered under on the QWebChannel (internal; pages use
#: ``window.epubReaderHost``).
CHANNEL_OBJECT_NAME: str = "epubReaderHostChannel"
#: ``QWebEngineScript`` names installed on the profile.
BOOT_SCRIPT_NAME: str = "epub_reader_boot"
READY_SCRIPT_NAME: str = "epub_reader_ready"
#: ``id`` of the ``<style>`` elements the boot script injects.
THEME_STYLE_ID: str = "__er_theme"
READER_STYLE_ID: str = "__er_reader"

#: Sandbox for untrusted book markup.  ``script-src 'none'`` kills every script a
#: book ships; our own QWebEngineScript user scripts are injected by the browser
#: and still run.  ``style-src 'unsafe-inline'`` is MANDATORY — a user script in
#: ApplicationWorld does NOT get Chromium's isolated-world CSP bypass, so without
#: it the injected reader/theme stylesheet is refused and theming silently dies.
BOOK_CSP: bytes = (
    b"default-src 'none'; "
    b"img-src 'self' data: blob:; "
    b"style-src 'self' 'unsafe-inline'; "
    b"font-src 'self' data:; "
    b"media-src 'self' data:; "
    b"connect-src 'self'; "
    b"script-src 'none'; "
    b"object-src 'none'; "
    b"frame-src 'none'; "
    b"base-uri 'none'; "
    b"form-action 'none'"
)

_scheme_registered = False
_flags_configured = False


def _merge_flag(parts: list[str], flag: str) -> None:
    """Add *flag* to *parts*.  ``--blink-settings=`` values are MERGED, because
    Chromium honours only one ``--blink-settings`` switch (the last one wins)."""
    if flag in parts:
        return
    prefix = "--blink-settings="
    if flag.startswith(prefix):
        wanted = [kv for kv in flag[len(prefix):].split(",") if kv]
        for i, existing in enumerate(parts):
            if existing.startswith(prefix):
                have = [kv for kv in existing[len(prefix):].split(",") if kv]
                keys = {kv.split("=", 1)[0] for kv in wanted}
                merged = [kv for kv in have if kv.split("=", 1)[0] not in keys] + wanted
                parts[i] = prefix + ",".join(merged)
                return
    parts.append(flag)


def configure_chromium_flags(extra: tuple[str, ...] = ()) -> str:
    """Append :data:`REQUIRED_CHROMIUM_FLAGS` to ``QTWEBENGINE_CHROMIUM_FLAGS``.

    Idempotent.  Existing flags set by the entry point are preserved; nothing is
    added twice, and an existing ``--blink-settings=`` switch is merged rather
    than shadowed.  Returns the resulting value of the environment variable.

    Must run before ``QApplication`` is constructed — Chromium only reads the
    variable once, at start-up.
    """
    global _flags_configured
    current = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
    parts = current.split()
    for flag in tuple(REQUIRED_CHROMIUM_FLAGS) + tuple(extra):
        _merge_flag(parts, flag)
    value = " ".join(parts)
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = value
    _flags_configured = True
    return value


def register_epub_scheme() -> None:
    """Register the ``epub://`` URL scheme.  **Call before ``QApplication``.**

    Idempotent: safe to call from module import *and* from ``main()``.  Also
    installs the required Chromium flags (see :func:`configure_chromium_flags`).

    Syntax is ``Host``, so URLs look like ``epub://<book-host>/OEBPS/text/ch1.xhtml``
    and the document gets a real, stable origin (``epub://<book-host>``); CORS,
    ``fetch()``, ``localStorage`` and the CSP all behave sanely as a result.
    """
    global _scheme_registered
    if _scheme_registered:
        return
    if QCoreApplication.instance() is not None:
        # Too late: Chromium has (or soon will have) started without the scheme
        # or the flags.  Say so loudly instead of failing mysteriously later.
        print("webhost: register_epub_scheme() called after QApplication was "
              "created; epub:// will not work. Import webhost before creating "
              "the application.", file=sys.stderr)
    configure_chromium_flags()
    existing = QWebEngineUrlScheme.schemeByName(EPUB_SCHEME)
    if bytes(existing.name().data()) == EPUB_SCHEME:
        # Already registered (e.g. the module was reloaded).  Do not register a
        # second time: Qt warns and the second registration is ignored anyway.
        _scheme_registered = True
        return

    scheme = QWebEngineUrlScheme(EPUB_SCHEME)
    scheme.setSyntax(QWebEngineUrlScheme.Syntax.Host)
    try:
        scheme.setDefaultPort(QWebEngineUrlScheme.SpecialPort.PortUnspecified.value)
    except (TypeError, AttributeError):  # pragma: no cover - PySide6 variance
        pass
    flag = QWebEngineUrlScheme.Flag
    scheme.setFlags(
        flag.SecureScheme  # secure context: no mixed-content nags
        | flag.LocalScheme  # file-like: unreachable from the network
        | flag.LocalAccessAllowed
        | flag.CorsEnabled  # required for same-origin fetch()/XHR
        | flag.FetchApiAllowed
        # ViewSourceAllowed deliberately NOT set.
    )
    QWebEngineUrlScheme.registerScheme(scheme)
    _scheme_registered = True


# The one documented import-time side effect (CONTRACT §0).  Owner G still calls
# register_epub_scheme() explicitly as the first statement of main().
register_epub_scheme()


# ===========================================================================
# 1. Resources (source tree AND the PyInstaller onedir bundle)
# ===========================================================================

def resource_path(*parts: str) -> str:
    """Absolute path to a bundled resource, from source *and* when frozen.

    PyInstaller onedir puts ``--add-data`` payloads inside the contents
    directory, i.e. ``dist\\<app>\\_internal\\assets\\`` which is
    ``sys._MEIPASS + '/assets'`` at runtime.  A naive
    ``Path(__file__).parent / 'assets'`` works from source and breaks frozen.

    >>> resource_path("assets", "reader.js").endswith("reader.js")
    True
    """
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, *parts)


def asset_path(name: str) -> str:
    """Absolute path to ``assets/<name>`` (owner C's ``reader.js`` / ``reader.css``)."""
    return resource_path("assets", name)


def read_asset(name: str, default: str = "") -> str:
    """Read ``assets/<name>`` as UTF-8, returning *default* if it is absent.

    Missing assets are never fatal: the host still serves the book, it simply has
    no reading layer.  ``BookHost.missing_assets`` records what was not found.
    """
    path = asset_path(name)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return default


def _qwebchannel_js() -> str:
    """``qwebchannel.js`` out of the Qt resource system (verified path)."""
    handle = QFile(":/qtwebchannel/qwebchannel.js")
    if not handle.open(QIODevice.OpenModeFlag.ReadOnly):  # pragma: no cover
        raise RuntimeError("cannot open :/qtwebchannel/qwebchannel.js")
    try:
        return bytes(handle.readAll().data()).decode("utf-8")
    finally:
        handle.close()


# ===========================================================================
# 2. MIME types
# ===========================================================================

#: Explicit extension → MIME map.  Deliberately not ``mimetypes``: the stdlib map
#: is registry-driven on Windows and gets ``.xhtml``/``.opf``/``.ncx``/``.woff2``
#: wrong or machine-dependent.
MIME: dict[str, bytes] = {
    ".xhtml": b"application/xhtml+xml",
    ".xht": b"application/xhtml+xml",
    ".html": b"text/html",
    ".htm": b"text/html",
    ".xml": b"application/xml",
    ".opf": b"application/oebps-package+xml",
    ".ncx": b"application/x-dtbncx+xml",
    ".css": b"text/css",
    ".js": b"application/javascript",
    ".mjs": b"application/javascript",
    ".json": b"application/json",
    ".txt": b"text/plain",
    ".smil": b"application/smil+xml",
    ".png": b"image/png",
    ".jpg": b"image/jpeg",
    ".jpeg": b"image/jpeg",
    ".jpe": b"image/jpeg",
    ".gif": b"image/gif",
    ".webp": b"image/webp",
    ".bmp": b"image/bmp",
    ".avif": b"image/avif",
    ".svg": b"image/svg+xml",
    ".ico": b"image/x-icon",
    ".ttf": b"font/ttf",
    ".otf": b"font/otf",
    ".ttc": b"font/collection",
    ".woff": b"font/woff",
    ".woff2": b"font/woff2",
    ".eot": b"application/vnd.ms-fontobject",
    ".mp3": b"audio/mpeg",
    ".m4a": b"audio/mp4",
    ".aac": b"audio/aac",
    ".oga": b"audio/ogg",
    ".ogg": b"audio/ogg",
    ".wav": b"audio/wav",
    ".mp4": b"video/mp4",
    ".m4v": b"video/mp4",
    ".webm": b"video/webm",
    ".pls": b"application/pls+xml",
}

#: Types whose bytes are text and therefore MUST be labelled ``charset=utf-8``.
TEXT_MIME: frozenset[bytes] = frozenset(
    {
        b"application/xhtml+xml",
        b"text/html",
        b"text/css",
        b"text/plain",
        b"application/javascript",
        b"application/xml",
        b"application/oebps-package+xml",
        b"application/x-dtbncx+xml",
        b"application/smil+xml",
        b"application/json",
        b"image/svg+xml",
        b"application/pls+xml",
    }
)

#: Markup that Chromium renders as a *document* (gets the CSP and the reading layer).
DOCUMENT_MIME: frozenset[bytes] = frozenset(
    {b"application/xhtml+xml", b"text/html"}
)

_OCTET = b"application/octet-stream"


def guess_mime(zip_name: str) -> bytes:
    """MIME type for a zip entry name.  Uses ``posixpath`` — zip names are posix.

    >>> guess_mime("OEBPS/text/ch1.xhtml")
    b'application/xhtml+xml'
    >>> guess_mime("fonts/x.woff2")
    b'font/woff2'
    """
    return MIME.get(posixpath.splitext(zip_name)[1].lower(), _OCTET)


def host_id_for(book: Any) -> str:
    """Stable, DNS-safe host component for one book: ``b`` + 12 hex chars.

    Giving every book its own origin means two books can never collide in
    Chromium's in-memory cache or share a ``localStorage`` bucket — a real hazard
    because EPUBs overwhelmingly use the same internal paths
    (``OEBPS/text/ch1.xhtml``).  The file's size and mtime are part of the key, so
    a book that was replaced on disk gets a fresh origin too.
    """
    if book is None:
        return DEFAULT_BOOK_HOST
    path = str(getattr(book, "path", "") or "")
    if path:
        raw = path
        try:
            st = os.stat(path)
            raw = f"{path}|{st.st_size}|{st.st_mtime_ns}"
        except OSError:
            pass
    else:
        raw = f"id:{id(book)}"
    digest = hashlib.blake2b(raw.encode("utf-8", "surrogatepass"), digest_size=6)
    return "b" + digest.hexdigest()


# ===========================================================================
# 3. The zip-backed URL scheme handler
# ===========================================================================

class EpubSchemeHandler(QWebEngineUrlSchemeHandler):
    """Serves one open :class:`epublib.EpubBook` over ``epub://<host>/<zip entry>``.

    Lifetime model (this is the part that crashes if you get it wrong):

    * the reply :class:`QBuffer` is created with the request *job* as its parent,
      so C++ owns it and Python garbage collection cannot delete it mid-request;
    * ``setData()`` is called **before** ``open()``;
    * no Python-side reference to the buffer is kept.

    Verified over 100+ sequential requests with a forced ``gc.collect()`` after
    every single one.

    Every byte is read through ``EpubBook.read()``/``read_text()``, so font
    de-obfuscation applies transparently, and every path goes through
    ``EpubBook.resolve()``, so mis-cased, percent-encoded, ``../``-relative and
    CJK entry names all resolve.
    """

    #: What a content document is served as.  ``text/html`` on purpose: Chromium's
    #: XML parser replaces the whole chapter with an error page on a single
    #: unescaped '&', which real books contain.  Set to
    #: ``b"application/xhtml+xml"`` to get strict XML parsing back.
    content_document_mime: bytes = b"text/html"

    #: Response CSP for content documents.  Set to ``b""`` to serve none.
    csp: bytes = BOOK_CSP

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._book: Any = None
        self._host: str = DEFAULT_BOOK_HOST
        self.served: list[str] = []
        self.missed: list[str] = []
        self.errors: list[str] = []
        self.request_count: int = 0
        #: Bounded history so a long session cannot grow without limit.
        self.history_limit: int = 4096

    # -- book wiring --------------------------------------------------------
    def set_book(self, book: Any, host: str) -> None:
        """Swap the served book atomically.  ``book=None`` makes everything 404."""
        self._book = book
        self._host = host or DEFAULT_BOOK_HOST
        self.served.clear()
        self.missed.clear()
        self.errors.clear()
        self.request_count = 0

    @property
    def book(self) -> Any:
        return self._book

    @property
    def host(self) -> str:
        return self._host

    # -- internals ----------------------------------------------------------
    def _record(self, bucket: list[str], value: str) -> None:
        bucket.append(value)
        if len(bucket) > self.history_limit:
            del bucket[: len(bucket) // 2]

    def _resolve(self, encoded_path: str) -> str | None:
        """URL path (still percent-encoded) → exact zip entry name, or ``None``.

        The path must arrive percent-ENCODED: an EPUB may legitimately contain an
        entry called ``text/a#b.xhtml`` and the fragment has to be split off
        before anything is unquoted.  ``EpubBook.resolve()`` does exactly that.
        """
        book = self._book
        if book is None or not encoded_path:
            return None
        try:
            # Leading '/': the URL path is container-ROOT relative.  resolve()
            # with an empty base would otherwise join it onto the OPF directory
            # ("OEBPS/" + "OEBPS/text/ch1.xhtml").
            zip_name, _fragment = book.resolve("/" + encoded_path.lstrip("/"), "")
        except Exception:  # noqa: BLE001 - a bad href must never kill a request
            zip_name = None
        if zip_name:
            try:
                if book.has(zip_name):
                    return str(zip_name)
            except Exception:  # noqa: BLE001
                return str(zip_name)
        # Last-ditch: treat the decoded path as a literal entry name.
        literal = unquote(encoded_path)
        try:
            if book.has(literal):
                return literal
        except Exception:  # noqa: BLE001
            pass
        return None

    def _payload(self, zip_name: str, mime: bytes) -> tuple[bytes, bytes]:
        """Return ``(body, content_type)`` — text is normalised to real UTF-8.

        Serving raw zip bytes with ``charset=utf-8`` is a lie for the (rare, but
        real) EPUB 2 document written in GBK/Big5.  Decoding through
        ``EpubBook.read_text()`` and re-encoding UTF-8 makes the label true, and
        keeps the bytes Chromium sees identical to the string ``epublib`` hands to
        the §2.1 cross-engine invariant.
        """
        book = self._book
        if mime in TEXT_MIME:
            text: str | None = None
            reader = getattr(book, "read_text", None)
            if callable(reader):
                try:
                    text = reader(zip_name)
                except Exception:  # noqa: BLE001 - fall back to raw bytes
                    text = None
            if text is None:
                try:
                    text = book.read(zip_name).decode("utf-8")
                except Exception:  # noqa: BLE001
                    text = None
            if text is not None:
                if text.startswith("\ufeff"):
                    text = text[1:]
                body = text.encode("utf-8")
                return body, mime + b"; charset=utf-8"
            # Undecodable: serve the raw bytes, still labelled utf-8 so Chromium
            # substitutes U+FFFD instead of inventing latin-1 mojibake.
            return book.read(zip_name), mime + b"; charset=utf-8"
        return book.read(zip_name), mime

    # -- Qt entry point -----------------------------------------------------
    def requestStarted(self, job: QWebEngineUrlRequestJob) -> None:  # noqa: N802
        url = job.requestUrl()
        # FullyEncoded: an entry name may contain '#', ' ' or CJK, and only the
        # encoded form survives the round trip through resolve().
        path = url.path(QUrl.ComponentFormattingOption.FullyEncoded)
        if path.startswith("/"):
            path = path[1:]
        self.request_count += 1
        try:
            method = bytes(job.requestMethod().data()).upper()
            if method not in (b"GET", b"HEAD"):
                job.fail(QWebEngineUrlRequestJob.Error.RequestDenied)
                return
            if self._book is None or url.host() != self._host:
                self._record(self.missed, path)
                job.fail(QWebEngineUrlRequestJob.Error.UrlNotFound)
                return

            zip_name = self._resolve(path)
            if zip_name is None:
                self._record(self.missed, path)
                job.fail(QWebEngineUrlRequestJob.Error.UrlNotFound)
                return

            mime = guess_mime(zip_name)
            is_document = mime in DOCUMENT_MIME
            if is_document:
                mime = self.content_document_mime
            data, content_type = self._payload(zip_name, mime)

            buf = QBuffer(job)  # parented to the job: C++ owns it
            buf.setData(QByteArray(data))  # setData BEFORE open()
            buf.open(QIODevice.OpenModeFlag.ReadOnly)

            # setAdditionalResponseHeaders is a QMultiMap in C++: every VALUE must
            # be a LIST of byte strings.  A bare QByteArray is accepted and then
            # silently emitted one value per byte, reversed.
            headers: dict[QByteArray, list[bytes]] = {
                QByteArray(b"X-Epub-Reader-Entry"): [
                    quote(zip_name, safe="/").encode("ascii")
                ]
            }
            if is_document and self.csp:
                headers[QByteArray(b"Content-Security-Policy")] = [self.csp]
            job.setAdditionalResponseHeaders(headers)

            job.reply(QByteArray(content_type), buf)
            self._record(self.served, zip_name)

            del buf  # drop the Python wrapper; C++ still owns the device
        except Exception as exc:  # noqa: BLE001 - never let this escape into Qt
            self._record(self.errors, f"{path}: {exc!r}")
            try:
                job.fail(QWebEngineUrlRequestJob.Error.RequestFailed)
            except Exception:  # noqa: BLE001 - job may already be gone
                pass


class _SchemeInterceptor(QWebEngineUrlRequestInterceptor):
    """Blocks any request that is not part of the book.

    Belt and braces behind the CSP: a book must never be able to phone home, and
    a blocked request is recorded so the app can tell the user why an image is
    missing.
    """

    ALLOWED = frozenset({EPUB_SCHEME_NAME, "about", "data", "blob", "qrc"})

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.blocked: list[str] = []

    def interceptRequest(self, info: QWebEngineUrlRequestInfo) -> None:  # noqa: N802
        try:
            url = info.requestUrl()
            if url.scheme().lower() not in self.ALLOWED:
                info.block(True)
                if len(self.blocked) < 512:
                    self.blocked.append(url.toString())
        except Exception:  # noqa: BLE001 - never raise into Chromium's IO path
            pass


# ===========================================================================
# 4. The bridge (QWebChannel)
# ===========================================================================

class HostBridge(QObject):
    """The object behind ``window.epubReaderHost``.

    JavaScript never touches this class directly.  ``webhost`` injects a small
    facade so that ``assets/reader.js`` can write the plain calls named in
    CONTRACT §4::

        window.epubReaderHost.positionChanged(state);   // object OR JSON text
        window.epubReaderHost.linkClicked(href);

    The facade forwards them to :meth:`notify`, which re-emits them here as real
    Qt signals with the contract's names.  (A ``QObject`` cannot have a signal and
    a slot of the same name, which is why the facade exists.)  ``state``/``info``
    payloads may be passed either as objects (``reader.js``'s default) or as JSON
    text (``init({hostPayload: 'json'})``) and arrive in Python as ``dict``.

    Python → JS goes the other way through :attr:`hostMessage`; JS receives it via
    ``window.epubReaderHost.on(function (msg) { ... })``.
    """

    # ---- CONTRACT §4 signals (JS -> Python) -------------------------------
    ready = Signal()
    positionChanged = Signal(object)  # dict
    linkClicked = Signal(str)
    selectionChanged = Signal(object)  # dict | None
    noteRequested = Signal(str)
    keyUnhandled = Signal(str)

    # ---- extras -----------------------------------------------------------
    #: Fired at DocumentReady for every document, before images/fonts are ready.
    domReady = Signal()
    #: EVERY message the page sends, named ones included: ``(kind, payload)``.
    #: ``ready``'s state object is only available here.
    message = Signal(str, object)
    #: ``console.log``-ish text from the reading layer.
    logMessage = Signal(str)
    #: Python -> JS.  Payload is a JSON string.
    hostMessage = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        #: Every (kind, payload) received, for debugging.  Bounded.
        self.received: list[tuple[str, Any]] = []
        self.history_limit: int = 1024

    # -- JS -> Python -------------------------------------------------------
    @Slot(str, str)
    def notify(self, kind: str, payload_json: str) -> None:
        """Fire-and-forget call from the page.  ``payload_json`` is JSON text."""
        try:
            payload = json.loads(payload_json) if payload_json else None
        except (TypeError, ValueError):
            payload = payload_json
        self.received.append((kind, payload))
        if len(self.received) > self.history_limit:
            del self.received[: len(self.received) // 2]
        try:
            self._dispatch(kind, payload)
        except Exception as exc:  # noqa: BLE001 - Qt swallows slot exceptions
            print(f"webhost: bridge dispatch failed for {kind!r}: {exc!r}",
                  file=sys.stderr)

    @Slot(str, str, result=str)
    def request(self, kind: str, payload_json: str) -> str:
        """Call from the page that wants an answer.  Returns JSON text.

        Install an answerer with :meth:`set_responder`; the default replies
        ``null``.  In JS this is asynchronous::

            epubReaderHost.ask('settings', {}, function (answer) { ... });
        """
        responder = self._responder
        if responder is None:
            return "null"
        try:
            payload = json.loads(payload_json) if payload_json else None
            return json.dumps(responder(kind, payload), ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            print(f"webhost: bridge responder failed for {kind!r}: {exc!r}",
                  file=sys.stderr)
            return "null"

    _responder: Optional[Callable[[str, Any], Any]] = None

    def set_responder(self, fn: Callable[[str, Any], Any] | None) -> None:
        """Answer :meth:`request` calls from the page.  ``fn(kind, payload) -> json-able``."""
        self._responder = fn

    @staticmethod
    def _as_dict(payload: Any) -> dict | None:
        """``state``/``info`` payloads: accept a dict or JSON text of one."""
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                return None
        return payload if isinstance(payload, dict) else None

    def _dispatch(self, kind: str, payload: Any) -> None:
        if kind == "ready":
            self.ready.emit()
        elif kind == "domReady":
            self.domReady.emit()
        elif kind == "positionChanged":
            self.positionChanged.emit(self._as_dict(payload) or {})
        elif kind == "linkClicked":
            self.linkClicked.emit("" if payload is None else str(payload))
        elif kind == "selectionChanged":
            self.selectionChanged.emit(self._as_dict(payload))
        elif kind == "noteRequested":
            self.noteRequested.emit("" if payload is None else str(payload))
        elif kind == "keyUnhandled":
            self.keyUnhandled.emit("" if payload is None else str(payload))
        elif kind == "log":
            self.logMessage.emit("" if payload is None else str(payload))
        self.message.emit(kind, payload)

    # -- Python -> JS -------------------------------------------------------
    def post(self, kind: str, payload: Any = None) -> None:
        """Push a message to the page.  Delivered to every ``epubReaderHost.on()`` listener."""
        self.hostMessage.emit(
            json.dumps({"kind": kind, "payload": payload}, ensure_ascii=False)
        )


# ===========================================================================
# 5. Injected JavaScript
# ===========================================================================

#: DocumentCreation boot script.  ``__EPUB_READER_CONFIG__`` is replaced with JSON.
#:
#: Verified behaviours it depends on: the QWebChannel handshake is ASYNCHRONOUS,
#: so anything produced before it completes is queued and flushed in the channel
#: callback.  At DocumentCreation ``document.readyState`` is ``'loading'``; for a
#: ``text/html`` response ``documentElement`` may not exist yet, so stylesheet
#: injection falls back to a MutationObserver and is repeated at DocumentReady.
_BOOT_JS = r"""
(function () {
  if (window.__epubReaderBoot) { return; }
  var CONFIG = __EPUB_READER_CONFIG__;
  var THEME_STYLE_ID = '__er_theme';
  var READER_STYLE_ID = '__er_reader';
  var B = window.__epubReaderBoot = {
    config: CONFIG, queued: 0, connected: false,
    creation: {
      readyState: document.readyState,
      hasDocumentElement: !!document.documentElement,
      hasHead: !!document.head,
      contentType: document.contentType
    }
  };

  var pending = [];
  var channel = null;
  var listeners = [];

  function deliver(kind, json) {
    if (channel && channel.notify) { channel.notify(kind, json); return true; }
    return false;
  }
  function flush() {
    while (pending.length && channel) {
      var m = pending.shift();
      deliver(m[0], m[1]);
    }
    B.queued = pending.length;
  }
  function send(kind, payload) {
    var json;
    try { json = JSON.stringify(payload === undefined ? null : payload); }
    catch (e) { json = JSON.stringify(String(payload)); }
    if (json === undefined) { json = 'null'; }
    if (!deliver(kind, json)) {
      pending.push([kind, json]);
      if (pending.length > 500) { pending.shift(); }
      B.queued = pending.length;
    }
  }
  // state/info payloads: objects (reader.js default) or JSON text (hostPayload:'json').
  function obj(v) {
    if (typeof v === 'string') {
      try { return JSON.parse(v); } catch (e) { return null; }
    }
    return (v === undefined) ? null : v;
  }
  function str(v) { return (v === undefined || v === null) ? '' : String(v); }

  // ---- window.epubReaderHost : the surface assets/reader.js writes against --
  var H = window.epubReaderHost = {
    ready:            function (state) { send('ready', obj(state)); },
    positionChanged:  function (state) { send('positionChanged', obj(state)); },
    linkClicked:      function (href)  { send('linkClicked', str(href)); },
    selectionChanged: function (info)  { send('selectionChanged', obj(info)); },
    noteRequested:    function (id)    { send('noteRequested', str(id)); },
    keyUnhandled:     function (key)   { send('keyUnhandled', str(key)); },
    domReady:         function ()      { send('domReady', null); },
    log:              function (msg)   { send('log', str(msg)); },
    send:             send,
    on:               function (fn)    { if (typeof fn === 'function') { listeners.push(fn); } },
    off:              function (fn)    {
      var i = listeners.indexOf(fn); if (i >= 0) { listeners.splice(i, 1); }
    },
    isConnected:      function ()      { return !!channel; },
    config:           CONFIG
  };
  H.ask = function (kind, payload, cb) {
    if (!channel || !channel.request) { if (cb) { cb(null); } return false; }
    var json;
    try { json = JSON.stringify(payload === undefined ? null : payload); }
    catch (e) { json = 'null'; }
    channel.request(kind, json, function (answer) {
      if (!cb) { return; }
      var value = null;
      try { value = JSON.parse(answer); } catch (e) { value = answer; }
      cb(value);
    });
    return true;
  };

  // ---- injected stylesheets ---------------------------------------------
  // createElementNS is correct for every content type (HTML and XHTML alike).
  function makeStyle(id, css) {
    var s = document.createElementNS('http://www.w3.org/1999/xhtml', 'style');
    s.setAttribute('type', 'text/css');
    s.id = id;
    s.textContent = css;
    return s;
  }
  var SHEETS = [[THEME_STYLE_ID, 'themeCss'], [READER_STYLE_ID, 'readerCss']];
  function applyStyle(id, css) {
    var node = document.getElementById(id);
    if (node) { node.textContent = css || ''; return true; }
    if (!css) { return true; }
    var parent = document.head || document.documentElement;
    if (!parent) { return false; }
    parent.appendChild(makeStyle(id, css));
    return true;
  }
  H.applyStyle = applyStyle;
  H.applyStyles = function () {
    var ok = true;
    for (var i = 0; i < SHEETS.length; i++) {
      var css = CONFIG[SHEETS[i][1]] || '';
      if (css && !applyStyle(SHEETS[i][0], css)) { ok = false; }
    }
    return ok;
  };
  H.setThemeCss = function (css) { CONFIG.themeCss = css || ''; return applyStyle(THEME_STYLE_ID, CONFIG.themeCss); };
  H.setReaderCss = function (css) { CONFIG.readerCss = css || ''; return applyStyle(READER_STYLE_ID, CONFIG.readerCss); };

  if (!H.applyStyles()) {
    var obs = new MutationObserver(function () {
      if (H.applyStyles()) { obs.disconnect(); }
    });
    obs.observe(document, { childList: true, subtree: true });
    document.addEventListener('DOMContentLoaded', H.applyStyles);
  }

  // ---- links Chromium would drop silently --------------------------------
  // epub:/http(s):/file:... clicks reach BookHost through the navigation
  // policy.  A scheme Chromium has no handler for (mailto:, tel:, ...) never
  // does, so report it here as linkClicked; javascript: URLs are just refused.
  var ROUTED = { 'epub:': 1, 'http:': 1, 'https:': 1, 'file:': 1, 'about:': 1, 'data:': 1, 'blob:': 1 };
  window.addEventListener('click', function (e) {
    if (e.defaultPrevented || e.button !== 0) { return; }
    var t = e.target;
    var a = (t && t.closest) ? t.closest('a[href]') : null;
    if (!a || typeof a.protocol !== 'string') { return; }   // SVG <a>: leave to Chromium
    var proto = a.protocol.toLowerCase();
    if (ROUTED[proto]) { return; }
    e.preventDefault();
    if (proto !== 'javascript:') { send('linkClicked', String(a.href)); }
  }, true);

  // ---- QWebChannel handshake (asynchronous; queue until it lands) --------
  function connect() {
    if (typeof qt === 'undefined' || !qt.webChannelTransport) { return false; }
    if (typeof QWebChannel === 'undefined') { return false; }
    new QWebChannel(qt.webChannelTransport, function (ch) {
      channel = ch.objects.__EPUB_READER_CHANNEL__;
      if (!channel) { return; }
      B.connected = true;
      if (channel.hostMessage && channel.hostMessage.connect) {
        channel.hostMessage.connect(function (text) {
          var msg = text;
          try { msg = JSON.parse(text); } catch (e) { /* keep the raw string */ }
          var snapshot = listeners.slice();
          for (var i = 0; i < snapshot.length; i++) {
            try { snapshot[i](msg); } catch (e) { /* a bad listener is not fatal */ }
          }
        });
      }
      flush();
    });
    return true;
  }
  if (!connect()) {
    var tries = 0;
    var iv = setInterval(function () {
      if (connect() || ++tries > 300) { clearInterval(iv); }
    }, 10);
  }
})();
"""

#: DocumentReady script: the DOM is parsed (readyState 'interactive'), the book's
#: own CSS has been seen.  Images are NOT decoded yet and ``document.fonts.status``
#: is still 'loading' — never measure here.  It is a separate script from the
#: boot, so ``domReady`` reaches Python even if the reading layer failed to load.
_READY_JS = r"""
(function () {
  var H = window.epubReaderHost;
  if (!H) { return; }
  H.applyStyles();
  H.domReady();
})();
"""


def _make_script(
    name: str,
    source: str,
    point: QWebEngineScript.InjectionPoint,
    world: QWebEngineScript.ScriptWorldId = APP_WORLD,
) -> QWebEngineScript:
    script = QWebEngineScript()
    script.setName(name)
    script.setSourceCode(source)
    script.setInjectionPoint(point)
    script.setWorldId(world)
    script.setRunsOnSubFrames(True)
    return script


# ===========================================================================
# 6. The page (navigation policy + console)
# ===========================================================================

class BookPage(QWebEnginePage):
    """``QWebEnginePage`` with Book Reader's navigation policy.

    Overriding a C++ virtual in PySide6 requires *subclassing* — assigning the
    method onto an instance never reaches the vtable (verified).

    Policy (implemented in :meth:`BookHost._accept_navigation`):

    * ``epub://<current book host>/...`` — allowed for programmatic loads; a
      **clicked** link is blocked, reported as
      :attr:`BookHost.internalLinkClicked` and (by default) re-issued through
      :meth:`BookHost.navigate` so the host always knows where the view is.
    * ``epub://`` with any other host — blocked (a stale or foreign book).
    * ``http``/``https``/``mailto``/``tel``/``ftp`` — never navigates the book
      view.  A *clicked* one is reported as :attr:`BookHost.externalLinkRequested`
      so the app can ask the user before opening the system browser.
    * anything else (``file:``, ``data:``, ``javascript:``…) — blocked.
    * ``target=_blank`` / ``window.open`` — no window is ever created; a
      user-initiated request is classified exactly like a click.
    """

    def __init__(self, profile: QWebEngineProfile, host: "BookHost",
                 parent: QObject | None = None) -> None:
        super().__init__(profile, parent)
        self._host = host
        self.console_log: list[str] = []
        self.console_limit: int = 512
        self.newWindowRequested.connect(self._on_new_window_requested)

    def javaScriptConsoleMessage(  # noqa: N802
        self, level: Any, message: str, line: int, source: str
    ) -> None:
        try:
            name = os.path.basename(source) if source else "?"
            self.console_log.append(f"[{int(getattr(level, 'value', level))}] "
                                    f"{name}:{line} {message}")
            if len(self.console_log) > self.console_limit:
                del self.console_log[: len(self.console_log) // 2]
            self._host.consoleMessage.emit(str(message), str(source), int(line))
        except Exception:  # noqa: BLE001
            pass

    def acceptNavigationRequest(  # noqa: N802
        self, url: QUrl, nav_type: Any, is_main_frame: bool
    ) -> bool:
        try:
            return self._host._accept_navigation(url, nav_type, bool(is_main_frame))
        except Exception as exc:  # noqa: BLE001 - never let this escape into Qt
            print(f"webhost: navigation policy failed: {exc!r}", file=sys.stderr)
            return False

    def createWindow(self, _window_type: Any) -> QWebEnginePage | None:  # noqa: N802
        """No pop-ups, ever (see :meth:`_on_new_window_requested`)."""
        return None

    def _on_new_window_requested(self, request: Any) -> None:
        try:
            self._host._on_new_window_requested(
                QUrl(request.requestedUrl()), bool(request.isUserInitiated())
            )
        except Exception as exc:  # noqa: BLE001
            print(f"webhost: new-window policy failed: {exc!r}", file=sys.stderr)

    def certificateError(self, error: Any) -> bool:  # noqa: N802
        """Nothing in a book may reach the network; never accept a bad certificate."""
        try:
            error.rejectCertificate()
        except Exception:  # noqa: BLE001
            pass
        return False


# ===========================================================================
# 7. BookHost
# ===========================================================================

#: Schemes that are a real destination outside the book (reported, never loaded).
EXTERNAL_SCHEMES: frozenset[str] = frozenset({"http", "https", "mailto", "tel", "ftp"})

#: A link reported by ``reader.js`` AND by Chromium's own navigation request (a
#: pointerup handler cannot cancel the click that follows it) is handled once.
_LINK_DEDUPE_SECONDS = 0.5

_JS_IDENTIFIER = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")


class BookHost(QObject):
    """Owns the profile, the scheme handler, the page and the bridge.

    One instance per reading surface.  The profile is the single most expensive
    object in the app (0.6–4.3 s to construct on this machine), so it is built
    once here and reused for every book; :meth:`set_book` only swaps the book the
    handler serves and the origin it serves it on.

    Typical use::

        host = BookHost()
        host.attach(view)                      # QWebEngineView
        host.set_book(book)                    # epublib.EpubBook
        host.navigate(book.spine[0].zip_name)
        host.bridge.positionChanged.connect(on_position)
        ...
        host.close()
    """

    #: The view's document changed (navigation committed, or a same-document
    #: fragment jump): ``(zip_name, fragment, spine_index)``.  ``spine_index`` is
    #: ``-1`` when the document is not in the spine; ``("", "", -1)`` for
    #: ``about:blank``.
    locationChanged = Signal(str, str, int)
    #: A link inside the book was clicked: ``(zip_name, fragment)``.  Already
    #: resolved to a zip entry.  The host follows it itself unless
    #: :attr:`auto_follow_internal_links` is ``False``.
    internalLinkClicked = Signal(str, str)
    #: An ``http(s)``/``mailto``/``tel``/``ftp`` link was clicked.  The book view
    #: never navigates there — ask the user, then open the system browser.
    externalLinkRequested = Signal(str)
    #: A navigation was refused: ``(url, reason)``; reason is one of
    #: ``foreign-origin``, ``external``, ``scheme``, ``missing``, ``no-book``.
    navigationBlocked = Signal(str, str)
    #: A book document finished loading (``True``) or failed (``False``).  Only
    #: loads of THIS book's ``epub://`` URLs are reported: the ``about:blank``
    #: parking load of :meth:`set_book` and stopped/superseded loads are not, so
    #: ``set_book(b); navigate(x)`` yields exactly one ``loadFinished`` for ``x``.
    loadFinished = Signal(bool)
    #: A load of the current book that ended STOPPED (aborted: superseded, or cancelled by
    #: Chromium) carries no ``loadFinished``.  Emitted with the stopped document's zip name so
    #: a caller still waiting for exactly that document can retry instead of waiting forever.
    loadStopped = Signal(str)
    #: ``(message, source, line)`` from the page's JS console.
    consoleMessage = Signal(str, str, int)
    #: :meth:`set_book` finished: the new origin host (``"book"`` for ``None``).
    bookChanged = Signal(str)

    def __init__(self, profile_name: str = "book-reader",
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        register_epub_scheme()  # cheap, idempotent, and a loud safety net

        self.profile_name = profile_name
        self.setObjectName(f"{profile_name}-host")
        self._book: Any = None
        self._book_host: str = DEFAULT_BOOK_HOST
        self._view: Any = None
        self._closed = False
        self._quit_hooked = False
        self._current_zip_name: str = ""
        self._current_fragment: str = ""
        self._current_spine_index: int = -1
        self._recent_links: dict[tuple, float] = {}

        #: Set ``False`` to take over in-book link following yourself.
        self.auto_follow_internal_links: bool = True

        #: Names of ``assets/*`` that could not be read (never fatal).
        self.missing_assets: list[str] = []

        # ---- profile ------------------------------------------------------
        # Off-the-record (no storage name): nothing is persisted to disk, which is
        # what a reader wants — no cookie/cache directories left behind.
        # ``profile_name`` is a label only (object names, user agent).
        #
        # Created WITHOUT a parent and re-parented after the page: QObject deletes
        # children in insertion order, and the page must die before its profile.
        profile = QWebEngineProfile()
        profile.setObjectName(profile_name)
        agent = profile.httpUserAgent()
        if "BookReader/" not in agent:
            profile.setHttpUserAgent(f"{agent} BookReader/1.0")

        self.handler = EpubSchemeHandler(profile)
        profile.installUrlSchemeHandler(EPUB_SCHEME, self.handler)

        self.interceptor = _SchemeInterceptor(profile)
        profile.setUrlRequestInterceptor(self.interceptor)

        self._apply_settings(profile.settings())

        # ---- page ----------------------------------------------------------
        self.page = BookPage(profile, self, self)       # first child of self
        profile.setParent(self)                          # ...the profile after it
        self.profile = profile
        self.page.setBackgroundColor(QColor("#ffffff"))
        self.page.loadingChanged.connect(self._on_loading_changed)
        self.page.urlChanged.connect(self._on_url_changed)

        # ---- injected scripts ---------------------------------------------
        self._theme_css: str = ""
        self._reader_css: str = ""
        self._reader_js: str = ""
        self._config: dict[str, Any] = {
            "themeCss": "",
            "readerCss": "",
            "settings": {},
            "mode": "paginated",
            "locator": None,
        }
        self.reload_assets()  # reads assets/reader.{js,css}, installs the scripts

        # ---- bridge ---------------------------------------------------------
        self.bridge = HostBridge(self)
        self.channel = QWebChannel(self)
        self.channel.registerObject(CHANNEL_OBJECT_NAME, self.bridge)
        self.page.setWebChannel(self.channel, APP_WORLD)
        self.bridge.linkClicked.connect(self._on_js_link_clicked)

        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.close)
            self._quit_hooked = True

    # -- settings -----------------------------------------------------------
    @staticmethod
    def _apply_settings(settings: QWebEngineSettings) -> None:
        attr = QWebEngineSettings.WebAttribute
        settings.setAttribute(attr.JavascriptEnabled, True)
        settings.setAttribute(attr.LocalContentCanAccessFileUrls, False)
        settings.setAttribute(attr.LocalContentCanAccessRemoteUrls, False)
        settings.setAttribute(attr.LocalStorageEnabled, True)
        settings.setAttribute(attr.PdfViewerEnabled, False)
        settings.setAttribute(attr.PluginsEnabled, False)
        settings.setAttribute(attr.ShowScrollBars, False)
        settings.setAttribute(attr.FocusOnNavigationEnabled, True)
        # Failures surface as loadFinished(False) instead of a Chromium error page
        # rendered *inside* the book.
        settings.setAttribute(attr.ErrorPageEnabled, False)
        settings.setAttribute(attr.PlaybackRequiresUserGesture, True)
        settings.setAttribute(attr.JavascriptCanOpenWindows, False)
        settings.setAttribute(attr.JavascriptCanAccessClipboard, False)
        settings.setAttribute(attr.LinksIncludedInFocusChain, True)
        settings.setAttribute(attr.ScrollAnimatorEnabled, False)
        settings.setAttribute(attr.WebGLEnabled, False)
        settings.setAttribute(attr.PrintElementBackgrounds, True)
        settings.setAttribute(attr.HyperlinkAuditingEnabled, False)
        settings.setAttribute(attr.DnsPrefetchEnabled, False)
        settings.setAttribute(attr.ScreenCaptureEnabled, False)
        settings.setAttribute(attr.FullScreenSupportEnabled, False)
        settings.setAttribute(attr.NavigateOnDropEnabled, False)
        # ForceDarkMode would fight our own theming; the reader owns the colours.
        settings.setAttribute(attr.ForceDarkMode, False)
        settings.setUnknownUrlSchemePolicy(
            QWebEngineSettings.UnknownUrlSchemePolicy.DisallowUnknownUrlSchemes
        )
        # Belt and braces behind the Content-Type charset: if a response ever
        # arrives without one, guess UTF-8, never the locale code page.
        settings.setDefaultTextEncoding("UTF-8")

    # -- scripts ------------------------------------------------------------
    def _boot_source(self) -> str:
        config = json.dumps(self._config, ensure_ascii=False)
        # U+2028/U+2029 are legal in JSON but were line terminators in old JS.
        config = config.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
        boot = (_BOOT_JS.replace("__EPUB_READER_CONFIG__", config)
                .replace("__EPUB_READER_CHANNEL__", CHANNEL_OBJECT_NAME))
        # qwebchannel.js + boot + reader.js are ONE script on purpose: two scripts
        # at the same injection point would rely on collection ordering.
        parts = [_qwebchannel_js(), boot]
        if self._reader_js:
            parts.append(self._reader_js)
        return "\n;\n".join(parts)

    def _install_scripts(self) -> None:
        scripts = self.profile.scripts()
        scripts.clear()
        point = QWebEngineScript.InjectionPoint
        scripts.insert(_make_script(BOOT_SCRIPT_NAME, self._boot_source(),
                                    point.DocumentCreation))
        scripts.insert(_make_script(READY_SCRIPT_NAME, _READY_JS,
                                    point.DocumentReady))

    def set_theme_css(self, css: str) -> None:
        """Push owner B's theme CSS into the page, live and for future loads."""
        self._theme_css = css or ""
        self._config["themeCss"] = self._theme_css
        self._install_scripts()
        self.run_js(
            "window.epubReaderHost && window.epubReaderHost.setThemeCss("
            + json.dumps(self._theme_css, ensure_ascii=False)
            + ")"
        )

    def set_boot_config(self, **values: Any) -> None:
        """Merge values into ``window.epubReaderHost.config`` for subsequent documents.

        Used for ``settings``, ``mode`` and an initial ``locator`` so the reading
        layer can lay out correctly on first paint instead of flashing.
        """
        self._config.update(values)
        if "readerCss" in values:
            self._reader_css = values["readerCss"] or ""
        if "themeCss" in values:
            self._theme_css = values["themeCss"] or ""
        self._install_scripts()

    def reload_assets(self) -> None:
        """Re-read ``assets/reader.js`` and ``assets/reader.css`` (through
        :func:`resource_path`) and reinstall the injected scripts."""
        self.missing_assets = []
        self._reader_css = read_asset("reader.css")
        if not self._reader_css:
            self.missing_assets.append("reader.css")
        self._reader_js = read_asset("reader.js")
        if not self._reader_js:
            self.missing_assets.append("reader.js")
        self._config["readerCss"] = self._reader_css
        self._install_scripts()

    # -- book ---------------------------------------------------------------
    @property
    def book(self) -> Any:
        return self._book

    @property
    def book_host(self) -> str:
        """The host component of the current book's origin (``epub://<this>``)."""
        return self._book_host

    def set_book(self, book: "EpubBook | None") -> None:
        """Serve *book* from now on, or nothing when ``None``.

        The profile, handler, page and bridge are all reused; only the served book
        and the origin change.  Loading stops, the handler drops every reference
        to the previous book and its request history, Chromium's HTTP cache is
        cleared and the view is parked on ``about:blank``.  The host never closes
        a book — the caller owns :class:`epublib.EpubBook` objects.
        """
        if self._closed:
            raise RuntimeError("BookHost is closed")
        self.page.triggerAction(QWebEnginePage.WebAction.Stop)
        self._book = book
        self._book_host = host_id_for(book)
        self.handler.set_book(book, self._book_host)
        self._recent_links.clear()
        self._set_location("", "")
        try:
            self.profile.clearHttpCache()
        except Exception:  # noqa: BLE001
            pass
        self.page.setUrl(QUrl("about:blank"))
        self.bookChanged.emit(self._book_host)

    # -- URLs ---------------------------------------------------------------
    def url_for(self, zip_name: str, fragment: str = "") -> QUrl:
        """``epub://<book host>/<percent-encoded zip entry>[#fragment]``.

        ``zip_name`` is an exact zip entry name (posix, already decoded).  It is
        percent-encoded here, so entries containing ``#``, spaces or CJK survive
        the round trip through :meth:`EpubSchemeHandler._resolve`.
        """
        path = quote(zip_name or "", safe="/")
        text = f"{EPUB_SCHEME_NAME}://{self._book_host}/{path}"
        if fragment:
            text += "#" + quote(fragment, safe="")
        return QUrl(text)

    def zip_name_for(self, url: QUrl) -> tuple[str, str]:
        """Inverse of :meth:`url_for`: ``QUrl`` → ``(zip_name, fragment)``.

        Returns ``("", "")`` for anything that is not this book's origin.
        """
        if url.scheme() != EPUB_SCHEME_NAME or url.host() != self._book_host:
            return "", ""
        encoded = url.path(QUrl.ComponentFormattingOption.FullyEncoded)
        if encoded.startswith("/"):
            encoded = encoded[1:]
        fragment = url.fragment(QUrl.ComponentFormattingOption.FullyDecoded) or ""
        zip_name = self.handler._resolve(encoded)
        if zip_name is None:
            zip_name = unquote(encoded)
        return zip_name, fragment

    # -- navigation ---------------------------------------------------------
    @property
    def current_zip_name(self) -> str:
        return self._current_zip_name

    @property
    def current_fragment(self) -> str:
        return self._current_fragment

    @property
    def current_spine_index(self) -> int:
        return self._current_spine_index

    def navigate(self, zip_name: str, fragment: str = "") -> None:
        """Load a document of the current book into the view.

        A fragment inside the document that is already showing is handed to
        ``window.epubReader.gotoFragment()`` when the reading layer exposes it (a
        plain fragment navigation would scroll a paginated column layout out of
        alignment); otherwise it is an ordinary same-document navigation.
        """
        if self._closed or self._book is None:
            return
        url = self.url_for(zip_name, fragment)
        if fragment and zip_name and zip_name == self._current_zip_name:
            def done(handled: Any) -> None:
                if self._closed:
                    return
                if handled is True:
                    self._set_location(zip_name, fragment)
                else:
                    self.page.setUrl(url)

            self.run_js(
                "(window.epubReader && typeof window.epubReader.gotoFragment"
                " === 'function') ? (window.epubReader.gotoFragment("
                + json.dumps(fragment, ensure_ascii=False) + "), true) : false",
                done,
            )
            return
        self.page.setUrl(url)

    def navigate_spine(self, index: int, fragment: str = "") -> bool:
        """Load spine item *index*.  Returns ``False`` if the index is out of range."""
        book = self._book
        if book is None:
            return False
        spine = getattr(book, "spine", None) or []
        if not (0 <= index < len(spine)):
            return False
        self.navigate(spine[index].zip_name, fragment)
        return True

    def _spine_index_of(self, zip_name: str) -> int:
        book = self._book
        if book is None or not zip_name:
            return -1
        finder = getattr(book, "spine_index", None)
        if callable(finder):
            try:
                found = finder(zip_name)
                if found is not None:
                    return int(found)
            except Exception:  # noqa: BLE001
                pass
        for i, item in enumerate(getattr(book, "spine", None) or []):
            if getattr(item, "zip_name", None) == zip_name:
                return i
        return -1

    def _set_location(self, zip_name: str, fragment: str) -> None:
        index = self._spine_index_of(zip_name)
        if (zip_name, fragment, index) == (
            self._current_zip_name, self._current_fragment, self._current_spine_index
        ):
            return
        self._current_zip_name = zip_name
        self._current_fragment = fragment
        self._current_spine_index = index
        self.locationChanged.emit(zip_name, fragment, index)

    def _on_url_changed(self, url: QUrl) -> None:
        if self._closed:
            return
        if url.scheme() == EPUB_SCHEME_NAME and url.host() == self._book_host:
            self._set_location(*self.zip_name_for(url))
        elif url.isEmpty() or url.scheme() == "about":
            self._set_location("", "")

    # -- navigation policy --------------------------------------------------
    _LINK_TYPES = frozenset({"NavigationTypeLinkClicked", "NavigationTypeFormSubmitted"})

    def _first_report(self, key: tuple) -> bool:
        """True the first time *key* is seen inside the de-duplication window."""
        now = time.monotonic()
        for old in [k for k, t in self._recent_links.items()
                    if now - t > _LINK_DEDUPE_SECONDS]:
            del self._recent_links[old]
        if key in self._recent_links:
            return False
        self._recent_links[key] = now
        return True

    @staticmethod
    def _external_key(url: QUrl) -> tuple:
        # PySide6 6.11 only accepts ComponentFormattingOption here, so the
        # StripTrailingSlash normalisation ("https://x" == "https://x/") is manual.
        text = bytes(url.toEncoded().data()).decode("ascii", "replace")
        return ("external", text[:-1] if text.endswith("/") else text)

    def _internal_click(self, zip_name: str, fragment: str) -> None:
        """One internal link activation, from whichever source saw it first."""
        if not self._first_report(("internal", zip_name, fragment)):
            return
        self.internalLinkClicked.emit(zip_name, fragment)
        if self.auto_follow_internal_links:
            # Never re-enter page.setUrl() from inside the policy callback.
            QTimer.singleShot(0, lambda: self.navigate(zip_name, fragment))

    def _external_click(self, url: QUrl) -> None:
        text = url.toString()
        if url.scheme().lower() not in EXTERNAL_SCHEMES:
            self.navigationBlocked.emit(text, "scheme")
            return
        if self._first_report(self._external_key(url)):
            self.externalLinkRequested.emit(text)
        self.navigationBlocked.emit(text, "external")

    def _accept_navigation(self, url: QUrl, nav_type: Any, is_main_frame: bool) -> bool:
        if self._closed:
            return False
        text = url.toString()
        scheme = url.scheme().lower()

        if not scheme or scheme == "about":
            return True

        type_name = getattr(nav_type, "name", str(nav_type))
        clicked = type_name in self._LINK_TYPES

        if scheme == EPUB_SCHEME_NAME:
            if url.host() != self._book_host or self._book is None:
                self.navigationBlocked.emit(text, "foreign-origin")
                return False
            if clicked and is_main_frame:
                encoded = url.path(QUrl.ComponentFormattingOption.FullyEncoded).lstrip("/")
                if self.handler._resolve(encoded) is None:
                    self.navigationBlocked.emit(text, "missing")
                else:
                    self._internal_click(*self.zip_name_for(url))
                return False
            return True

        # Everything else: the book view must never navigate off the book.
        if clicked:
            self._external_click(url)
        else:
            self.navigationBlocked.emit(
                text, "external" if scheme in EXTERNAL_SCHEMES else "scheme")
        return False

    def _on_new_window_requested(self, url: QUrl, user_initiated: bool) -> None:
        if self._closed:
            return
        if url.scheme().lower() == EPUB_SCHEME_NAME:
            if url.host() == self._book_host and self._book is not None and user_initiated:
                self._internal_click(*self.zip_name_for(url))
            else:
                self.navigationBlocked.emit(url.toString(), "foreign-origin")
            return
        if user_initiated:
            self._external_click(url)
        else:
            self.navigationBlocked.emit(url.toString(), "scheme")

    def _on_js_link_clicked(self, href: str) -> None:
        """``epubReaderHost.linkClicked(href)`` from the reading layer.

        ``href`` is raw, exactly as written in the book, and relative to the
        current document.  It is classified the same way a real click is, and a
        click that Chromium ALSO reports as a navigation is handled only once.
        """
        href = (href or "").strip()
        if not href or self._closed:
            return
        candidate = QUrl(href)
        scheme = candidate.scheme().lower()
        # len(scheme) > 1: "C:/x" parses with scheme "c"; that is a (bad) path.
        if scheme and scheme != EPUB_SCHEME_NAME and len(scheme) > 1:
            self._external_click(candidate)
            return
        book = self._book
        if book is None:
            self.navigationBlocked.emit(href, "no-book")
            return
        if scheme == EPUB_SCHEME_NAME or href.startswith("//"):
            absolute = self.page.url().resolved(candidate)
            if absolute.scheme() != EPUB_SCHEME_NAME or absolute.host() != self._book_host:
                self.navigationBlocked.emit(absolute.toString(), "foreign-origin")
                return
            zip_name, fragment = self.zip_name_for(absolute)
        else:
            try:
                zip_name, fragment = book.resolve(href, self._current_zip_name)
            except Exception:  # noqa: BLE001
                zip_name, fragment = "", ""
            fragment = fragment or ""
        try:
            present = bool(zip_name) and bool(book.has(zip_name))
        except Exception:  # noqa: BLE001
            present = bool(zip_name)
        if not present:
            self.navigationBlocked.emit(href, "missing")
            return
        self._internal_click(zip_name, fragment)

    # -- view ---------------------------------------------------------------
    def attach(self, view: "QWebEngineView") -> None:
        """Put this host's page into *view*.  The view does not own the page."""
        if self._closed:
            raise RuntimeError("BookHost is closed")
        self._view = view
        view.setPage(self.page)

    def detach(self) -> None:
        """Forget the view.  The page and profile stay alive and reusable."""
        self._view = None

    @property
    def view(self) -> Any:
        return self._view

    def set_background_color(self, color: str | QColor) -> None:
        """The colour Chromium paints where the document is transparent.

        This is what kills the white flash between chapters under 夜.  There is no
        ``QWebEngineView.setBackgroundColor`` in Qt 6.11 — the page is the only lever.
        """
        self.page.setBackgroundColor(QColor(color))

    # -- JS -----------------------------------------------------------------
    def run_js(
        self,
        script: str,
        callback: Callable[[Any], None] | None = None,
        *,
        world: QWebEngineScript.ScriptWorldId | None = None,
    ) -> None:
        """Run *script* in the reading layer's world (ApplicationWorld).

        ``runJavaScript`` can only carry **numbers, strings and booleans** back to
        Python; arrays, objects, ``null``, ``undefined``, DOM nodes, functions,
        Promises and thrown errors all arrive as ``''`` and are indistinguishable.
        Use :meth:`run_json` for anything structured.  An exception raised by
        *callback* is printed to stderr (Qt would otherwise swallow it).
        """
        if self._closed:
            return
        target = APP_WORLD if world is None else world
        if callback is None:
            self.page.runJavaScript(script, target)
            return

        def wrapped(value: Any) -> None:
            try:
                callback(value)
            except Exception as exc:  # noqa: BLE001 - Qt swallows callback errors
                print(f"webhost: run_js callback failed: {exc!r}", file=sys.stderr)

        self.page.runJavaScript(script, target, wrapped)

    def run_json(
        self,
        expression: str,
        callback: Callable[[Any], None] | None = None,
        *,
        world: QWebEngineScript.ScriptWorldId | None = None,
    ) -> None:
        """Evaluate *expression* and hand the decoded JSON value to *callback*.

        The only reliable way to read structured data out of the page.  A value
        that cannot be decoded (or ``undefined``) arrives as ``None``.
        """
        if callback is None:
            self.run_js(f"JSON.stringify({expression})", None, world=world)
            return

        def wrapped(value: Any) -> None:
            try:
                decoded = json.loads(value) if isinstance(value, str) and value else None
            except ValueError as exc:
                print(f"webhost: run_json could not decode {expression[:60]!r}: {exc!r}",
                      file=sys.stderr)
                decoded = None
            callback(decoded)

        self.run_js(f"JSON.stringify({expression})", wrapped, world=world)

    def call_reader(
        self,
        method: str,
        *args: Any,
        callback: Callable[[Any], None] | None = None,
    ) -> None:
        """Call ``window.epubReader.<method>(...)`` (CONTRACT §4), JSON result back.

        Arguments are JSON-encoded.  *callback* receives ``None`` when the reading
        layer or the method is missing.
        """
        if not _JS_IDENTIFIER.match(method or ""):
            raise ValueError(f"not a JavaScript identifier: {method!r}")
        packed = ", ".join(json.dumps(a, ensure_ascii=False) for a in args)
        expr = (
            f"(window.epubReader && typeof window.epubReader.{method} === 'function')"
            f" ? window.epubReader.{method}({packed}) : null"
        )
        self.run_json(expr, callback)

    def post_to_page(self, kind: str, payload: Any = None) -> None:
        """Push a message to every ``window.epubReaderHost.on()`` listener in the page."""
        self.bridge.post(kind, payload)

    # -- signals ------------------------------------------------------------
    def _on_loading_changed(self, info: QWebEngineLoadingInfo) -> None:
        if self._closed:
            return
        status = info.status()
        done = QWebEngineLoadingInfo.LoadStatus
        if status == done.LoadStoppedStatus:
            url = info.url()
            if url.scheme() == EPUB_SCHEME_NAME and url.host() == self._book_host:
                self.loadStopped.emit(self.zip_name_for(url)[0])
            return
        if status not in (done.LoadSucceededStatus, done.LoadFailedStatus):
            return
        url = info.url()
        if url.scheme() != EPUB_SCHEME_NAME or url.host() != self._book_host:
            return
        self.loadFinished.emit(status == done.LoadSucceededStatus)

    # -- teardown -----------------------------------------------------------
    def close(self) -> None:
        """Release the page, the handler and the profile.  Idempotent.

        Also runs automatically on ``QCoreApplication.aboutToQuit``.  Order
        matters: the page must die before the profile it was created from, so the
        page's ``deleteLater()`` is posted first.  The host never closes the book.
        """
        if self._closed:
            return
        self._closed = True
        if self._quit_hooked:
            self._quit_hooked = False
            app = QCoreApplication.instance()
            try:
                if app is not None:
                    app.aboutToQuit.disconnect(self.close)
            except (RuntimeError, TypeError):
                pass
        self._book = None
        self._recent_links.clear()
        try:
            self.bridge.set_responder(None)
        except Exception:  # noqa: BLE001
            pass
        for signal, slot in (
            (self.page.loadingChanged, self._on_loading_changed),
            (self.page.urlChanged, self._on_url_changed),
            (self.bridge.linkClicked, self._on_js_link_clicked),
        ):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        try:
            self.page.triggerAction(QWebEnginePage.WebAction.Stop)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.profile.scripts().clear()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.handler.set_book(None, DEFAULT_BOOK_HOST)
            self.profile.removeUrlSchemeHandler(self.handler)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.profile.setUrlRequestInterceptor(None)
        except Exception:  # noqa: BLE001
            pass
        self._view = None
        self.page.deleteLater()
        self.profile.deleteLater()

    @property
    def closed(self) -> bool:
        return self._closed
