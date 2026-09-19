# -*- coding: utf-8 -*-
"""Open any supported book file as an :class:`epublib.EpubBook`.

The reader, the library and the web host all speak EPUB.  Other formats are
turned into an equivalent EPUB once and cached, so every feature (positions,
search, highlights, themes, covers) works for them unchanged:

* EPUB                     opened directly
* MOBI / PRC / AZW / AZW3  converted by :mod:`mobi` (DRM is refused, never removed)
* DjVu                     wrapped by :mod:`djvu` as a fixed-layout book whose page
                           images are rendered on demand

Files are recognised by their content, not their extension.  A converted book's
``path`` is the user's original file (so its identity, origin and "show in
folder" all refer to that file); ``source_format`` says what it really is.
Failures raise :class:`epublib.EpubError` with the usual kinds, plus
``"unsupported"`` for recognised-but-unsupported formats (KFX, Topaz).

Nothing is ever written next to the user's file.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import threading

from epublib import EpubBook, EpubError

__all__ = ["BOOK_EXTENSIONS", "sniff", "open_book", "converted_dir", "is_book_file"]

#: File extensions offered in open dialogs and picked up by folder scans.
BOOK_EXTENSIONS = (".epub", ".mobi", ".azw3", ".azw", ".prc", ".djvu", ".djv")

_convert_lock = threading.Lock()


def is_book_file(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in BOOK_EXTENSIONS


def sniff(path: "str | os.PathLike[str]") -> str | None:
    """``"epub"``, ``"mobi"``, ``"djvu"``, ``"kfx"``, ``"topaz"`` or ``None``."""
    with open(path, "rb") as fh:
        head = fh.read(96)
    if head[:4] == b"PK\x03\x04":
        return "epub"
    if head[:8] == b"AT&TFORM":
        return "djvu"
    import mobi
    return mobi.detect(head)


def converted_dir(cache_root: str | None = None) -> str:
    """``<cache>\\converted``: regenerable EPUB versions of non-EPUB books."""
    import store
    return os.path.join(cache_root or store.cache_dir(), "converted")


def _content_key(path: str) -> str:
    h = hashlib.blake2b(digest_size=16)
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _write_atomic(target: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(target), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".conv-", suffix=".tmp", dir=os.path.dirname(target))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _converted_epub(path: str, fmt: str, cache_root: str | None, key: str | None) -> str:
    """Path of the cached EPUB for *path*, converting on a miss."""
    import mobi
    version = mobi.CONVERTER_VERSION
    key = key or _content_key(path)
    target = os.path.join(converted_dir(cache_root), f"{key}-{fmt}-v{version}.epub")
    if os.path.isfile(target) and os.path.getsize(target) > 0:
        return target
    with _convert_lock:                       # one conversion at a time: they are CPU-bound
        if os.path.isfile(target) and os.path.getsize(target) > 0:
            return target
        try:
            data = mobi.convert(path)
        except mobi.MobiError as exc:
            kind = {"drm": "drm", "unsupported": "unsupported"}.get(exc.kind, "corrupt")
            raise EpubError(str(exc), kind=kind, detail=exc.detail, drm_scheme=exc.drm_scheme) from exc
        _write_atomic(target, data)
    return target


def open_book(path: "str | os.PathLike[str]", *, cache_root: str | None = None,
              content_key: str | None = None) -> EpubBook:
    """Open *path* whatever its format.

    ``cache_root`` is the Store's cache directory (tests pass a temporary one);
    ``content_key`` may pass an already computed blake2b-128 of the file to avoid
    hashing it twice.  ``FileNotFoundError`` propagates unchanged, as with
    :meth:`EpubBook.open`.
    """
    spath = os.fspath(path)
    fmt = sniff(spath)
    if fmt in (None, "epub"):
        book = EpubBook.open(spath)          # raises the right EpubError for junk
        book.source_format = "epub"
        return book
    if fmt in ("kfx", "topaz"):
        raise EpubError(f"{fmt.upper()} Kindle books are not supported", kind="unsupported",
                        detail=f"{fmt} container")
    if fmt == "mobi":
        sub = "azw3" if _is_kf8(spath) else "mobi"
        cached = _converted_epub(spath, "mobi", cache_root, content_key)
        book = EpubBook.open(cached)
        book.path = os.path.abspath(spath)
        book.source_format = sub
        return book
    if fmt == "djvu":
        import djvu
        return djvu.open_book(spath, cache_root=cache_root, content_key=content_key)
    raise EpubError("unrecognised file", kind="corrupt", detail=fmt or "")


def _is_kf8(path: str) -> bool:
    """True when record 0 carries a version-8 (KF8/AZW3) MOBI header."""
    import struct
    with open(path, "rb") as fh:
        head = fh.read(86)
        if len(head) < 86:
            return False
        fh.seek(struct.unpack_from(">I", head, 78)[0])   # record 0 follows the record table
        r0 = fh.read(40)
    return len(r0) >= 40 and r0[16:20] == b"MOBI" and struct.unpack_from(">I", r0, 36)[0] >= 8
