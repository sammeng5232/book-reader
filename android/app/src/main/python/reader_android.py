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
import hashlib
import os
import threading

_book = None
_book_path = ""
_lock = threading.RLock()


def formula_scales(book, path: str) -> dict[str, float]:
    """Read-only bitmap series calibration, shared with the PDF glyph analyser.

    Parse one spine document at a time, retain at most 96 successful samples per
    series, and cache only the resulting em/pixel ratios. The original EPUB and
    its text offsets are never rewritten. Explicit CSS remains the JS owner's
    first choice; these ratios are for unsized formula bitmaps only.
    """
    import store
    from latexexport import _FORMULA_HINT, _BLOCKS, _glyph_heights, _image_series, _local, parse_document

    fingerprint = f"reader-formulas-v1|{os.path.abspath(path)}|{os.stat(path).st_size}|{os.stat(path).st_mtime_ns}"
    cache = os.path.join(store.cache_dir(), "reader-formulas", hashlib.sha256(fingerprint.encode()).hexdigest() + ".json")
    try:
        with open(cache, encoding="utf8") as f:
            return json.load(f)
    except (OSError, ValueError):
        pass
    samples, hints, checked = {}, set(), set()
    for item in book.spine:
        doc = item.zip_name
        if not doc or not doc.lower().endswith((".html", ".htm", ".xhtml", ".xml")):
            continue
        try:
            root = parse_document(book.read_text(doc))
        except Exception:
            continue
        for el in root.iter():
            if _local(el) != "img":
                continue
            src = el.get("src") or ""
            hint = bool(_FORMULA_HINT.search(src + " " + (el.get("class") or "")))
            parent = el.getparent()
            while parent is not None and _local(parent) not in _BLOCKS:
                parent = parent.getparent()
            if not hint and (parent is None or not "".join(parent.itertext()).strip()):
                continue
            try:
                image, _ = book.resolve(src, doc)
                series = _image_series(image)
                group = samples.setdefault(series, {})
                if image in checked or len(group) >= 96:
                    continue
                checked.add(image)
                glyphs = _glyph_heights(book.read(image))
            except Exception:
                continue
            if glyphs:
                group[image] = sorted(glyphs)[::max(1, len(glyphs) // 12)]
                if hint:
                    hints.add(series)
    scales = {}
    for series, group in samples.items():
        heights = sorted(h for sample in group.values() for h in sample)
        if len(group) >= (8 if series in hints else 24) and len(heights) >= 40:
            cap = heights[int(.8 * (len(heights) - 1))]
            if 8 <= cap <= 64:
                scales[series] = .70 / cap
    try:
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        with open(cache + ".part", "w", encoding="utf8") as f:
            json.dump(scales, f)
        os.replace(cache + ".part", cache)
    except OSError:
        pass
    return scales


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
            "formulaScales": formula_scales(book, path),
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
