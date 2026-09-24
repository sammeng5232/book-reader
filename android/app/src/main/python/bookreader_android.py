# -*- coding: utf-8 -*-
"""What the Android app calls into: the desktop converter, wired to the phone.

The heavy lifting is the same code the Windows app runs -- ``epublib``, ``mobi``,
``bookformats``, ``djvupdf`` and ``latexexport`` are copied in unchanged.  This
module only supplies the three things that differ on a phone:

* where state and caches live (``store`` reads them from the environment),
* the typesetter: instead of looking for an installed XeLaTeX, hand the .tex to
  the engine the app carries (``TexEngine`` in Kotlin, through JNI),
* fonts: the phone has no Windows font library, so documents are written with the
  fonts TeX itself ships (``ExportOptions.fonts = "texlive"``).

Kotlin calls :func:`setup` once, then :func:`convert_book` or :func:`convert_djvu`.
Both return a JSON string so the Kotlin side does not need to know Python types.
"""

from __future__ import annotations

import json
import os
import threading
import traceback

_READY = False
_TEX = None          # the Kotlin TexEngine object
_BUNDLE = ""
_TEXCACHE = ""


def setup(files_dir: str, cache_dir: str, bundle_dir: str, tex_cache: str) -> str:
    """Point the shared code at this app's folders.  Safe to call more than once."""
    global _READY, _TEX, _BUNDLE, _TEXCACHE
    os.environ["APPDATA"] = files_dir            # store.app_dir()   -> <files>/Book Reader
    os.environ["LOCALAPPDATA"] = cache_dir       # store.cache_dir() -> <cache>/Book Reader/cache
    os.environ.setdefault("HOME", files_dir)
    os.makedirs(files_dir, exist_ok=True)
    os.makedirs(cache_dir, exist_ok=True)
    _BUNDLE, _TEXCACHE = bundle_dir, tex_cache
    if _TEX is None:
        from java import jclass                  # provided by Chaquopy
        _TEX = jclass("com.bookreader.TexEngine").INSTANCE
    import djvu_bridge                           # DjVu is decoded in Kotlin here
    djvu_bridge.install()
    _READY = True
    import store
    return json.dumps({"state": store.app_dir(), "cache": store.cache_dir(),
                       "engine": _TEX.version()})


class _EngineTypesetter:
    """Stands in for XeLaTeX: the engine inside the app, driven over JNI."""

    name = "Tectonic (bundled)"

    def __call__(self, tex_path, pdf_path, progress=None, cancelled=None):
        import latexexport
        if progress is not None:
            progress("typeset1", 0, 0)
        if cancelled is not None and cancelled():
            raise latexexport.ExportCancelled()

        # The JNI call blocks until the PDF is done.  To keep a live progress
        # readout, typeset on a worker thread and poll the growing PDF here,
        # reporting the page count as it appears.
        holder = {}

        def work():
            try:
                holder["error"] = str(_TEX.typeset(tex_path, pdf_path, _BUNDLE, _TEXCACHE) or "")
            except Exception as exc:
                # Exceptions on the JNI worker do not propagate through join().
                # Preserve the actual failure instead of reporting "no PDF" (or
                # accepting a stale PDF from an earlier run).
                holder["error"] = f"{type(exc).__name__}: {exc}"

        t = threading.Thread(target=work, daemon=True)
        t.start()
        seen = 0
        while t.is_alive():
            t.join(timeout=0.5)
            if progress is not None:
                n = _pdf_pages(pdf_path)
                if n > seen:
                    seen = n
                    progress("typeset1", n, 0)      # total unknown until the end
            if cancelled is not None and cancelled():
                # Tectonic cannot be interrupted mid-run; report and let it finish,
                # then discard the half-written PDF below.
                pass
        t.join()

        error = holder.get("error", "")
        ok = (not error) and os.path.isfile(pdf_path)
        pages = _pdf_pages(pdf_path) if ok else 0
        if progress is not None:
            progress("typeset1", pages, pages)
        return latexexport.CompileResult(
            ok=ok, pdf_path=pdf_path if ok else None, pages=pages, passes=1,
            engine=self.name, problems=[] if ok else [error or "the engine wrote no PDF"])


def _pdf_pages(path: str) -> int:
    """Page count straight from the PDF (no PDF library on the phone)."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return 0
    import re
    counts = [int(m.group(1)) for m in re.finditer(rb"/Count\s+(\d+)", data)]
    return max(counts) if counts else data.count(b"/Type /Page") or 0


def _progress_adapter(listener):
    """Wrap a Java listener (``onProgress(stage, done, total)``) as a Python callback."""
    if listener is None:
        return None

    def report(stage: str, done: int, total: int) -> None:
        try:
            listener.onProgress(str(stage), int(done), int(total))
        except Exception:                       # noqa: BLE001 - progress must never fail a run
            pass
    return report


def _cancel_adapter(flag):
    if flag is None:
        return None
    return lambda: bool(flag.isCancelled())


def convert_book(path: str, destination: str, *, paper: str = "a5", font_size: int = 11,
                 cover: bool = True, contents: bool = True, make_pdf: bool = True,
                 listener=None, cancel=None) -> str:
    """EPUB / MOBI / AZW3 -> LaTeX (+ PDF).  Returns a JSON report."""
    import bookformats
    import latexexport
    try:
        options = latexexport.ExportOptions(paper=paper, font_size=int(font_size), cover=bool(cover),
                                            contents=bool(contents), compile_pdf=bool(make_pdf),
                                            fonts="texlive")
        book = bookformats.open_book(path, cache_root=_conversion_cache())
        try:
            res = latexexport.export_book(
                book, destination, options,
                progress=_progress_adapter(listener), cancelled=_cancel_adapter(cancel),
                typesetter=_EngineTypesetter() if make_pdf else None)
        finally:
            book.close()
        return json.dumps({
            "ok": True, "kind": "latex", "folder": res.folder, "tex": res.tex_path,
            "pdf": res.pdf_path, "pages": res.pages, "chapters": res.chapters,
            "images": res.images, "problems": res.problems[:20], "warnings": res.warnings[:20],
        }, ensure_ascii=False)
    except Exception as exc:                    # noqa: BLE001 - reported to the user
        return _failure(exc)


def convert_djvu(path: str, pdf_path: str, *, listener=None, cancel=None) -> str:
    """DjVu -> PDF (no LaTeX involved).  Returns a JSON report."""
    try:
        import djvu_bridge
        djvu_bridge.install()           # the decoder is Kotlin here, not djvutool.exe
        import djvupdf
        stats = djvupdf.convert_djvu_to_pdf(
            path, pdf_path,
            progress=(lambda d, t: listener.onProgress("pages", int(d), int(t))) if listener else None,
            cancelled=_cancel_adapter(cancel))
        return json.dumps({"ok": True, "kind": "djvu", "pdf": pdf_path,
                           "pages": stats.get("pages", 0),
                           "failed_pages": stats.get("failed_pages", [])}, ensure_ascii=False)
    except Exception as exc:                    # noqa: BLE001
        return _failure(exc)


def book_info(path: str) -> str:
    """Title, authors, format and chapter count, for the library screen."""
    try:
        import bookformats
        book = bookformats.open_book(path, cache_root=_conversion_cache())
        try:
            md = book.metadata or {}
            return json.dumps({
                "ok": True, "title": md.get("title") or "", "authors": md.get("authors") or [],
                "language": md.get("language") or "", "format": getattr(book, "source_format", "epub"),
                "chapters": len(book.toc), "spine": len(book.spine),
            }, ensure_ascii=False)
        finally:
            book.close()
    except Exception as exc:                    # noqa: BLE001
        return _failure(exc)


def _conversion_cache() -> str:
    import store
    return store.cache_dir()


def _failure(exc: BaseException) -> str:
    kind = getattr(exc, "kind", "") or type(exc).__name__
    return json.dumps({"ok": False, "error": f"{exc}", "kind": kind,
                       "detail": getattr(exc, "detail", "") or "",
                       "trace": traceback.format_exc()[-2000:]}, ensure_ascii=False)
