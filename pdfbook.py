# -*- coding: utf-8 -*-
"""Read PDF pages through the app's fixed-layout protocol without changing the PDF.

The cached EPUB contains page geometry, text and outline only. PNGs are rendered
on demand, with a bounded cache. PyMuPDF is imported only for PDF; Android's
native PDF route does not load this desktop dependency. All MuPDF calls share
a lock because it does not support simultaneous calls from different workers.
"""
from __future__ import annotations
from collections import OrderedDict
import io
import math
import os
import re
import threading
import traceback
import zipfile
from xml.sax.saxutils import escape
from epublib import EpubBook, EpubError

PDF_ADAPTER_VERSION = 1
RENDER_WIDTH = 2000
MAX_RENDER_PIXELS = 8_000_000
PAGE_CACHE_BYTES = 32 * 1024 * 1024
PAGE_CACHE_COUNT = 8
_CSS_SCALE = 96 / 72
_IMAGE = re.compile(r"^OEBPS/pages/p(\d{6})\.png$")
_mupdf_lock = threading.RLock()
_build_lock = threading.Lock()


def _engine():
    try:
        import pymupdf
    except ImportError as exc:
        raise EpubError("the PDF renderer is missing", kind="unsupported",
                        detail="This desktop build requires PyMuPDF for PDF reading") from exc
    return pymupdf


def _open_pdf(path: str):
    try:
        document = _engine().open(path)
    except FileNotFoundError:
        raise
    except Exception as exc:
        detail = str(exc)
        # MuPDF's failed constructor can remain in its exception traceback and
        # keep the source file open on Windows. Release those constructor frames.
        nested = exc
        seen = set()
        while nested is not None and id(nested) not in seen:
            seen.add(id(nested))
            traceback.clear_frames(nested.__traceback__)
            nested.__traceback__ = None
            following = nested.__cause__ or nested.__context__
            nested.__cause__ = nested.__context__ = None
            nested = following
        import gc
        gc.collect()
        raise EpubError("the PDF cannot be read", kind="corrupt", detail=detail) from None
    if document.needs_pass:
        document.close()
        raise EpubError("this PDF requires a password", kind="password")
    if not document.is_pdf or document.page_count < 1:
        document.close()
        raise EpubError("the PDF has no readable pages", kind="corrupt")
    if document.page_count > 25_000:
        document.close()
        raise EpubError("the PDF has too many pages", kind="too_large")
    return document


def _xml(value) -> str:
    return escape(re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value or "")))


def _page_name(index: int, suffix: str = "xhtml") -> str:
    return f"p{index + 1:06d}.{suffix}"


def _rect_css(rect) -> str:
    return (f"left:{rect.x0 * _CSS_SCALE:.3f}px;top:{rect.y0 * _CSS_SCALE:.3f}px;"
            f"width:{max(.1, rect.width * _CSS_SCALE):.3f}px;"
            f"height:{max(.1, rect.height * _CSS_SCALE):.3f}px;")


def _page_document(page, number: int, count: int, engine) -> str:
    width, height = page.rect.width * _CSS_SCALE, page.rect.height * _CSS_SCALE
    if not all(math.isfinite(v) and v > 0 for v in (width, height)):
        raise EpubError("the PDF page has invalid dimensions", kind="corrupt")
    words = []
    previous_line = None
    for word in page.get_text("words", sort=True):
        text = _xml(word[4])
        if not text.strip():
            continue
        rect = engine.Rect(word[:4])
        line = tuple(word[5:7])
        if words:
            words.append("\n" if line != previous_line else " ")
        words.append(f'<span class="pdf-word" style="{_rect_css(rect)}'
                     f'font-size:{max(1, rect.height * _CSS_SCALE * .8):.3f}px">{text}</span>')
        previous_line = line
    links = []
    for link in page.get_links():
        href = ""
        if link.get("kind") == engine.LINK_GOTO and 0 <= link.get("page", -1) < count:
            href = _page_name(link["page"])
        elif link.get("kind") == engine.LINK_URI:
            uri = str(link.get("uri") or "")
            if uri.lower().startswith(("https://", "http://", "mailto:")):
                href = uri
        if href and link.get("from"):
            rect = engine.Rect(link["from"])
            attr = _xml(href).replace('"', "&quot;")
            links.append(f'<a class="pdf-link" href="{attr}" '
                         f'style="{_rect_css(rect)}" aria-label="Link"></a>')
    label = page.get_label() or str(number + 1)
    css = ("html,body{margin:0;padding:0}"
           f".pdf-page{{position:absolute;left:0;top:0;width:{width:.3f}px;height:{height:.3f}px}}"
           ".pdf-word{position:absolute;color:transparent;white-space:pre;line-height:1;overflow:hidden}"
           ".pdf-link{position:absolute;display:block;background:transparent}")
    matrix = page.rotation_matrix
    transform = (f"matrix({matrix.a:.6f},{matrix.b:.6f},{matrix.c:.6f},{matrix.d:.6f},"
                 f"{matrix.e * _CSS_SCALE:.3f},{matrix.f * _CSS_SCALE:.3f})")
    return ('<?xml version="1.0" encoding="utf-8"?><!DOCTYPE html>'
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><meta charset="utf-8"/>'
            f'<meta name="viewport" content="width={width:.3f}, height={height:.3f}"/>'
            f'<title>{_xml(label)}</title><style>{css}</style></head><body>'
            f'<img class="pdf-page" src="{_page_name(number, "png")}" alt="" '
            f'width="{width:.3f}" height="{height:.3f}"/>'
            + '<div style="position:absolute;left:0;top:0;transform-origin:0 0;transform:'
            + transform + '">' + "".join(words) + "</div>" + "".join(links) + "</body></html>")


def _navigation(document, title: str) -> str:
    entries = [(max(0, int(level) - 1), str(label), int(page) - 1)
               for level, label, page in document.get_toc(simple=True)
               if label and isinstance(page, int) and 1 <= page <= document.page_count]
    if not entries:
        entries = [(0, document.load_page(i).get_label() or str(i + 1), i)
                   for i in range(document.page_count)]
    out = ['<?xml version="1.0" encoding="utf-8"?>',
           '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">',
           f'<head><title>{_xml(title)}</title></head><body><nav epub:type="toc">']
    depth = -1
    for level, label, page in entries:
        level = max(0, min(level, depth + 1))
        if level > depth:
            out.append("<ol>" * (level - depth))
        else:
            out.append("</li>" + "</ol></li>" * (depth - level))
        out.append(f'<li><a href="pages/{_page_name(page)}">{_xml(label)}</a>')
        depth = level
    out.append("</li>" + "</ol></li>" * depth + "</ol></nav></body></html>")
    return "\n".join(out)


def _build_epub(path: str, key: str) -> bytes:
    with _mupdf_lock:
        engine = _engine()
        document = _open_pdf(path)
        try:
            metadata = document.metadata or {}
            title = metadata.get("title") or os.path.splitext(os.path.basename(path))[0]
            manifest = ['<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>']
            spine = []
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
                archive.writestr("META-INF/container.xml",
                    '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">'
                    '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                    'media-type="application/oebps-package+xml"/></rootfiles></container>')
                for i in range(document.page_count):
                    page = document.load_page(i)
                    name, image = _page_name(i), _page_name(i, "png")
                    archive.writestr("OEBPS/pages/" + name, _page_document(page, i, document.page_count, engine))
                    archive.writestr("OEBPS/pages/" + image, b"", compress_type=zipfile.ZIP_STORED)
                    manifest.append(f'<item id="p{i}" href="pages/{name}" media-type="application/xhtml+xml"/>')
                    cover = ' properties="cover-image"' if i == 0 else ""
                    manifest.append(f'<item id="i{i}" href="pages/{image}" media-type="image/png"{cover}/>')
                    spine.append(f'<itemref idref="p{i}"/>')
                opf = ('<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" '
                       'prefix="rendition: http://www.idpf.org/vocab/rendition/#">'
                       '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                       f'<dc:identifier id="bookid">urn:pdf:{key}</dc:identifier><dc:title>{_xml(title)}</dc:title>'
                       f'<dc:creator>{_xml(metadata.get("author"))}</dc:creator><dc:language>und</dc:language>'
                       '<meta property="dcterms:modified">2000-01-01T00:00:00Z</meta>'
                       '<meta property="rendition:layout">pre-paginated</meta>'
                       '<meta property="rendition:spread">none</meta><meta name="cover" content="i0"/>'
                       '</metadata><manifest>' + "".join(manifest) + '</manifest><spine>'
                       + "".join(spine) + '</spine></package>')
                archive.writestr("OEBPS/content.opf", opf)
                archive.writestr("OEBPS/nav.xhtml", _navigation(document, title))
            return buffer.getvalue()
        finally:
            document.close()


class PdfBook(EpubBook):
    """A PDF-backed book; one fixed-layout spine document per physical page."""
    _document = None

    def attach_source(self, path: str) -> None:
        with _mupdf_lock:
            self._document = _open_pdf(path)
        self._pages: OrderedDict[str, bytes] = OrderedDict()
        self._page_bytes = 0

    def _read_entry(self, name: str) -> bytes:
        match = _IMAGE.fullmatch(name)
        if match is None or self._document is None:
            return super()._read_entry(name)
        with _mupdf_lock:
            if self._closed or self._document is None:
                raise EpubError("the PDF is closed", kind="corrupt")
            if name in self._pages:
                self._pages.move_to_end(name)
                return self._pages[name]
            index = int(match[1]) - 1
            if not 0 <= index < self._document.page_count:
                raise EpubError("the PDF page does not exist", kind="corrupt")
            page = self._document.load_page(index)
            width, height = page.rect.width, page.rect.height
            scale = min(RENDER_WIDTH / width, 5000 / height,
                        math.sqrt(MAX_RENDER_PIXELS / (width * height)))
            pixmap = page.get_pixmap(matrix=_engine().Matrix(scale, scale), alpha=False)
            data = pixmap.tobytes("png")
            self._pages[name] = data
            self._page_bytes += len(data)
            while len(self._pages) > PAGE_CACHE_COUNT or self._page_bytes > PAGE_CACHE_BYTES:
                _old, removed = self._pages.popitem(last=False)
                self._page_bytes -= len(removed)
            return data

    def close(self) -> None:
        with _mupdf_lock:
            if self._document is not None:
                self._document.close()
                self._document = None
            if hasattr(self, "_pages"):
                self._pages.clear()
                self._page_bytes = 0
        super().close()


def open_book(path: str, *, cache_root: str | None = None, content_key: str | None = None) -> PdfBook:
    import bookformats
    source = os.path.abspath(path)
    key = content_key or bookformats._content_key(source)
    target = os.path.join(bookformats.converted_dir(cache_root), f"{key}-pdf-v{PDF_ADAPTER_VERSION}.epub")
    if not os.path.isfile(target) or os.path.getsize(target) == 0:
        with _build_lock:
            if not os.path.isfile(target) or os.path.getsize(target) == 0:
                bookformats._write_atomic(target, _build_epub(source, key))
    book = PdfBook.open(target)
    book.path = source
    book.source_format = "pdf"
    try:
        book.attach_source(source)
    except BaseException:
        book.close()
        raise
    return book
