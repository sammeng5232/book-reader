# -*- coding: utf-8 -*-
"""Serving an open book to the Android WebView reader.

The desktop app serves a book into QtWebEngine through ``webhost.py``; here the
WebView asks Kotlin for bytes and Kotlin asks this module.  The book object is the
same ``bookformats.open_book`` handle the converter uses, so EPUB, MOBI, AZW and
AZW3 all work; DjVu has no HTML spine and is converted to PDF instead.

One book is open at a time (the reader is single-window).  Everything returns JSON
text so the Kotlin side never sees a Python object.
"""

from __future__ import annotations

import json
import threading

_book = None
_book_path = ""
_lock = threading.RLock()


def _norm(settings: dict) -> dict:
    return {k: v for k, v in (settings or {}).items() if v is not None}


def open_book(path: str) -> str:
    """Open *path* and answer what the reader needs to start: metadata, spine,
    TOC, sizes.  Closes any previously open book."""
    global _book, _book_path
    import bookformats
    import store

    with _lock:
        close_book()
        book = bookformats.open_book(path, cache_root=store.cache_dir())
        _book = book
        _book_path = path

        spine = [
            {"i": i, "zip": it.zip_name, "linear": bool(it.linear), "units": book.units(it.zip_name)}
            for i, it in enumerate(book.spine)
        ]
        running = 0
        for s in spine:
            s["offset"] = running
            running += s["units"]
        total = max(1, running)

        def toc_out(entries):
            out = []
            for e in entries:
                try:
                    zip_name, fragment = book.resolve(e.href, "")
                except Exception:                       # noqa: BLE001
                    zip_name, fragment = "", ""
                idx = book.spine_index(zip_name) if zip_name else None
                out.append({"title": e.title, "spine": idx, "fragment": fragment or "",
                            "children": toc_out(e.children)})
            return out

        md = book.metadata or {}
        state = {}
        try:
            state = store.Store().book_state(store.Store().book_id_for(path)) or {}
        except Exception:                               # noqa: BLE001 - a fresh book is fine
            state = {}
        return json.dumps({
            "ok": True,
            "title": md.get("title") or "",
            "authors": md.get("authors") or [],
            "language": md.get("language") or "",
            "format": getattr(book, "source_format", "epub"),
            "spine": spine,
            "totalUnits": total,
            "toc": toc_out(book.toc),
            "tocSynthetic": bool(getattr(book, "toc_is_synthetic", False)),
            "fixedLayout": bool(book.is_fixed_layout),
            "pageDirection": getattr(book, "page_direction", "ltr"),
            "position": (state.get("position") or None),
        }, ensure_ascii=False)


def _need():
    if _book is None:
        raise RuntimeError("no book is open")
    return _book


def read(entry: str) -> bytes:
    """Raw bytes of one zip entry (fonts, images).  Text goes through read_text."""
    with _lock:
        return _need().read(entry)


def read_text(entry: str) -> str:
    """One text entry as a real str (BOM-aware, never guessed), for the WebView."""
    with _lock:
        return _need().read_text(entry)


def resolve(href: str, base: str) -> str:
    """``{"zip": ..., "fragment": ...}`` for a link, as the book sees it."""
    with _lock:
        zip_name, fragment = _need().resolve(href, base or "")
        return json.dumps({"zip": zip_name or "", "fragment": fragment or ""})


def has(entry: str) -> bool:
    with _lock:
        try:
            return bool(_need().has(entry))
        except Exception:                               # noqa: BLE001
            return False


def is_pre_paginated(zip_name: str) -> bool:
    with _lock:
        try:
            return bool(_need().is_pre_paginated(zip_name))
        except Exception:                               # noqa: BLE001
            return False


def close_book() -> None:
    global _book, _book_path
    with _lock:
        book, _book = _book, None
        _book_path = ""
    if book is not None:
        try:
            book.close()
        except Exception:                               # noqa: BLE001
            pass


def save_position(locator_json: str, percent: float) -> None:
    """Persist the reading position exactly as the desktop store keeps it."""
    import store
    with _lock:
        path = _book_path
    if not path:
        return
    try:
        st = store.Store()
        bid = st.book_id_for(path)
        state = st.book_state(bid) or {}
        state["position"] = {"locator": json.loads(locator_json), "percent": percent}
        st.save_book_state(bid, state, immediate=True)
    except Exception:                                   # noqa: BLE001 - never crash the page
        pass
