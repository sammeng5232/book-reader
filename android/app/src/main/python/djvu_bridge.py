# -*- coding: utf-8 -*-
"""The DjVu decoder on the phone, with the shape the desktop code expects.

On Windows :mod:`djvu` drives ``djvutool.exe serve`` -- a C# decoder -- over a
pipe.  Android has no .NET, so the decoder was ported to Kotlin
(``com.bookreader.djvu.DjvuDocument``) and is called straight through Chaquopy.
This module wraps it in the class the shared modules already talk to, so
:mod:`djvu` and :mod:`djvupdf` run unchanged:

* :class:`DjvuTool` answers ``info()``, ``text()``, ``all_text()``, ``layers()``,
  ``render()`` and ``close()`` exactly as ``djvu.DjvuTool`` does;
* it also answers ``request(line, binary=False)`` with the *serve protocol's*
  payloads, because ``djvupdf._Tool`` subclasses ``djvu.DjvuTool`` and speaks
  that protocol (``"layers\\t<page>\\t<max>"``, ``"text\\t<page>"``).

:func:`install` swaps this class into :mod:`djvu` (and into an already-imported
:mod:`djvupdf`), which is the whole of the Android-specific wiring.
"""

from __future__ import annotations

import json
import os
import threading

from epublib import EpubError

__all__ = ["DjvuTool", "install", "document_class"]

_CLASS = "com.bookreader.djvu.DjvuDocument"
_klass = None
_klass_lock = threading.Lock()


def document_class():
    """The Kotlin decoder class, loaded once."""
    global _klass
    if _klass is None:
        with _klass_lock:
            if _klass is None:
                from java import jclass          # provided by Chaquopy
                _klass = jclass(_CLASS)
    return _klass


def _msg(exc: BaseException) -> str:
    """A Java/Kotlin exception as the C# helper reported it: "TypeName: message"."""
    name = type(exc).__name__.rsplit(".", 1)[-1]
    text = str(exc).strip()
    # Chaquopy prints a Java exception as "<class>: <message>" already
    if text.startswith(name + ":"):
        return text
    first = text.splitlines()[0] if text else ""
    return f"{name}: {first}" if first else name


class DjvuTool:
    """One open document, held by the Kotlin decoder.

    The desktop class keeps a helper process; here the "process" is one Kotlin
    object, created on first use and dropped by :meth:`close`.  ``_proc`` stays
    for ``djvupdf._Tool.close``, which reaches for it while it holds the lock.
    """

    _proc = None                 # the desktop class's helper process: never one here

    def __init__(self, path: str) -> None:
        self.path = os.path.abspath(path)
        # re-entrant: a failed start calls close() while request() still holds the lock
        self._lock = threading.RLock()
        self._doc = None

    # ---- the helper ---------------------------------------------------------
    def _start(self) -> None:
        doc = document_class()()
        try:
            doc.open(self.path)
        except Exception as exc:                # noqa: BLE001 - reported like the helper's JSON
            try:
                doc.close()
            except Exception:                   # noqa: BLE001 - already failing
                pass
            raise EpubError("the DjVu file cannot be read", kind="corrupt",
                            detail=_msg(exc)) from None
        self._doc = doc

    def _need(self):
        if self._doc is None:
            self._start()
        return self._doc

    # ---- the serve protocol -------------------------------------------------
    def request(self, line: str, binary: bool = False):
        """One request of the helper's protocol, answered by the Kotlin decoder."""
        with self._lock:
            parts = line.split("\t")
            what = parts[0]
            try:
                doc = self._need()
                if what == "info":
                    return json.loads(doc.infoJson())
                if what == "alltext":
                    return json.loads(doc.allTextJson())
                if what == "text":
                    return json.loads(doc.textJson(int(parts[1])))
                if what == "render":
                    want = parts[3] if len(parts) > 3 else "jpg"
                    data = bytes(doc.render(int(parts[1]), int(parts[2]), want))
                    return (b"J" if want == "jpg" else b"P") + data
                if what == "layers":
                    planes = int(parts[2]) if len(parts) > 2 else 32
                    return bytes(doc.layers(int(parts[1]), planes))
                if what == "open":
                    return json.loads(doc.infoJson())
                if what == "quit":
                    self.close()
                    return b"" if binary else {}
                raise ValueError("unknown request " + what)
            except EpubError:
                raise
            except Exception as exc:            # noqa: BLE001 - the helper answered errors, too
                text = _msg(exc)
                return b"E" + text.encode("utf-8") if binary else {"error": text}

    # ---- what the shared modules call --------------------------------------
    def info(self) -> dict:
        return self.request("info")

    def all_text(self) -> list:
        return self.request("alltext")

    def text(self, page: int) -> dict:
        res = self.request(f"text\t{page}")
        return res if isinstance(res, dict) and "error" not in res else {}

    def layers(self, page: int, max_planes: int = 32) -> bytes:
        """The raw ``layers`` reply (``djvupdf._Tool`` parses it into planes)."""
        return self.request(f"layers\t{page}\t{max_planes}", binary=True)

    def render(self, page: int, width: int, fmt: str) -> bytes:
        data = self.request(f"render\t{page}\t{width}\t{fmt}", binary=True)
        if not data or data[:1] == b"E":
            raise EpubError("a DjVu page could not be rendered", kind="corrupt",
                            detail=data[1:].decode("utf-8", "replace") if data else "empty reply")
        return data[1:]

    def close(self) -> None:
        with self._lock:
            doc, self._doc = self._doc, None
        if doc is None:
            return
        try:
            doc.close()
        except Exception:                       # noqa: BLE001 - closing must never raise
            pass


def install() -> None:
    """Point :mod:`djvu` (and :mod:`djvupdf`) at the Kotlin decoder.  Idempotent."""
    import sys

    import djvu
    if getattr(djvu, "_ANDROID_BRIDGE", False):
        return
    djvu.DjvuTool = DjvuTool
    djvu._ANDROID_BRIDGE = True
    # djvupdf._Tool subclasses the helper: rebase it when it is already imported
    # (install() normally runs first, so this is only a safety net)
    mod = sys.modules.get("djvupdf")
    if mod is not None and DjvuTool not in mod._Tool.__bases__:
        try:
            mod._Tool.__bases__ = (DjvuTool,)
        except TypeError:                       # pragma: no cover - layout mismatch
            del sys.modules["djvupdf"]
