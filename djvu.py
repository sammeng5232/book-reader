# -*- coding: utf-8 -*-
"""DjVu books, opened through a from-scratch decoder.

A DjVu file is a stack of scanned pages.  Decoding one (JB2 bi-level masks, IW44
wavelet colour layers, BZZ-compressed text) takes tens of millions of arithmetic
decoder steps per page, far too slow for Python, so the decoder is a small C#
program (``djvutool/*.cs``, compiled with the C# compiler that ships with Windows)
kept running as a helper process.

The book is presented to the reader as a cached fixed-layout EPUB: one XHTML page
per scanned page, holding the page image and the page's OCR text as invisible,
positioned words over it.  Search, selection, copying, highlights and positions
therefore work on scans exactly as on any other book.  Page images are not in the
cached EPUB: :class:`DjvuBook` renders them on demand (with a small in-memory
cache and next/previous-page prefetch).

Public API::

    open_book(path, cache_root=None, content_key=None) -> DjvuBook
    tool_path() -> str               # the helper, compiled on first use if needed
    DJVU_CONVERTER_VERSION
"""

from __future__ import annotations

import io
import json
import os
import re
import struct
import subprocess
import sys
import threading
import uuid
import zipfile
from collections import OrderedDict
from xml.sax.saxutils import escape

from epublib import EpubBook, EpubError

__all__ = ["DjvuBook", "open_book", "tool_path", "DJVU_CONVERTER_VERSION", "DjvuTool"]

#: Part of the conversion cache key: bump whenever the produced EPUB changes.
DJVU_CONVERTER_VERSION = 1
#: Width in pixels of rendered page images (Chromium scales them to the window).
RENDER_WIDTH = 1800
PAGE_CACHE = 12

_PAGE_IMG = re.compile(r"^OEBPS/pages/p(\d{4,6})\.(jpg|png)$")
_build_lock = threading.Lock()


# ==========================================================================
# the helper process
# ==========================================================================

def _source_dir() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "djvutool")


def tool_path() -> str:
    """Path of ``djvutool.exe``; built from ``djvutool/*.cs`` when absent or stale.

    Frozen builds carry the compiled helper in their assets.  From source it is
    compiled with the .NET Framework C# compiler that every Windows 10/11 has.
    """
    import webhost
    bundled = webhost.asset_path("djvutool.exe")
    src = _source_dir()
    sources = [os.path.join(src, n) for n in ("Codecs.cs", "Program.cs", "ZPTable.cs")]
    if getattr(sys, "frozen", False) or not all(os.path.isfile(s) for s in sources):
        if os.path.isfile(bundled):
            return bundled
        raise EpubError("the DjVu decoder is missing", kind="unsupported", detail=bundled)
    newest = max(os.path.getmtime(s) for s in sources)
    if os.path.isfile(bundled) and os.path.getmtime(bundled) >= newest:
        return bundled
    with _build_lock:
        if os.path.isfile(bundled) and os.path.getmtime(bundled) >= newest:
            return bundled
        _compile(sources, bundled)
    return bundled


def _compile(sources: list[str], out: str) -> None:
    windir = os.environ.get("WINDIR", r"C:\Windows")
    csc = None
    for fw in ("Framework64", "Framework"):
        cand = os.path.join(windir, "Microsoft.NET", fw, "v4.0.30319", "csc.exe")
        if os.path.isfile(cand):
            csc = cand
            break
    if csc is None:
        raise EpubError("the DjVu decoder cannot be built", kind="unsupported",
                        detail="no .NET Framework C# compiler (csc.exe) found")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + ".building.exe"
    cmd = [csc, "-nologo", "-optimize+", "-target:exe", "-out:" + tmp, "-r:System.Drawing.dll",
           "-codepage:65001"] + sources
    res = subprocess.run(cmd, capture_output=True, text=True, creationflags=_NO_WINDOW)
    if res.returncode != 0 or not os.path.isfile(tmp):
        raise EpubError("the DjVu decoder failed to build", kind="unsupported",
                        detail=(res.stdout + res.stderr)[-2000:])
    os.replace(tmp, out)


_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class DjvuTool:
    """One ``djvutool serve`` process holding one open document."""

    def __init__(self, path: str) -> None:
        self.path = os.path.abspath(path)
        # re-entrant: a failed start calls close() while request() still holds the lock
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None

    def _start(self) -> None:
        self._proc = subprocess.Popen([tool_path(), "serve"], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                      creationflags=_NO_WINDOW)
        info = self._call("open\t" + self.path)
        if isinstance(info, dict) and "error" in info:
            self.close()
            raise EpubError("the DjVu file cannot be read", kind="corrupt", detail=info["error"])

    def _raw(self, line: str) -> bytes:
        proc = self._proc
        assert proc is not None and proc.stdin is not None and proc.stdout is not None
        proc.stdin.write((line + "\n").encode("utf-8"))
        proc.stdin.flush()
        head = proc.stdout.read(4)
        if len(head) < 4:
            raise EpubError("the DjVu decoder stopped", kind="corrupt", detail=line)
        n = struct.unpack(">I", head)[0]
        data = b""
        while len(data) < n:
            chunk = proc.stdout.read(n - len(data))
            if not chunk:
                raise EpubError("the DjVu decoder stopped", kind="corrupt", detail=line)
            data += chunk
        return data

    def _call(self, line: str):
        return json.loads(self._raw(line).decode("utf-8"))

    def request(self, line: str, binary: bool = False):
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._start()
            return self._raw(line) if binary else self._call(line)

    def info(self) -> dict:
        return self.request("info")

    def all_text(self) -> list:
        return self.request("alltext")

    def render(self, page: int, width: int, fmt: str) -> bytes:
        data = self.request(f"render\t{page}\t{width}\t{fmt}", binary=True)
        if not data or data[:1] == b"E":
            raise EpubError("a DjVu page could not be rendered", kind="corrupt",
                            detail=data[1:].decode("utf-8", "replace"))
        return data[1:]

    def close(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.poll() is None and proc.stdin:
                proc.stdin.write(b"quit\n")
                proc.stdin.flush()
            proc.wait(timeout=2)
        except Exception:  # noqa: BLE001 - a helper that will not quit is killed
            try:
                proc.kill()
                proc.wait(timeout=2)
            except (OSError, subprocess.SubprocessError):
                pass
        for pipe in (proc.stdin, proc.stdout):
            try:
                if pipe:
                    pipe.close()
            except OSError:
                pass


# ==========================================================================
# the book
# ==========================================================================

class DjvuBook(EpubBook):
    """A cached fixed-layout EPUB whose page images come from the DjVu decoder."""

    _source: str | None = None
    _tool: DjvuTool | None = None

    def attach_source(self, source: str) -> None:
        self._source = os.path.abspath(source)
        self._tool = DjvuTool(self._source)
        self._pages: OrderedDict[str, bytes] = OrderedDict()
        self._pages_lock = threading.Lock()
        self._prefetching: set[str] = set()

    def _read_entry(self, name: str) -> bytes:
        m = _PAGE_IMG.match(name)
        if m is None or self._tool is None:
            return super()._read_entry(name)
        data = self._page(name)
        self._prefetch(int(m.group(1)), m.group(2))
        return data

    def _page(self, name: str) -> bytes:
        with self._pages_lock:
            hit = self._pages.get(name)
            if hit is not None:
                self._pages.move_to_end(name)
                return hit
        m = _PAGE_IMG.match(name)
        assert m is not None and self._tool is not None
        data = self._tool.render(int(m.group(1)) - 1, RENDER_WIDTH, m.group(2))
        with self._pages_lock:
            self._pages[name] = data
            while len(self._pages) > PAGE_CACHE:
                self._pages.popitem(last=False)
        return data

    def _prefetch(self, number: int, _fmt: str) -> None:
        """Render the neighbouring pages in the background, so the next turn is instant."""
        wanted = []
        for n in (number + 1, number - 1):
            for ext in ("jpg", "png"):
                name = f"OEBPS/pages/p{n:04d}.{ext}"
                if self.has(name):
                    wanted.append(name)
        with self._pages_lock:
            wanted = [w for w in wanted if w not in self._pages and w not in self._prefetching]
            self._prefetching.update(wanted)
        if not wanted:
            return

        def work() -> None:
            for w in wanted:
                try:
                    self._page(w)
                except Exception:  # noqa: BLE001 - prefetch is best effort
                    pass
                finally:
                    with self._pages_lock:
                        self._prefetching.discard(w)
        threading.Thread(target=work, name="djvu-prefetch", daemon=True).start()

    def close(self) -> None:
        if self._tool is not None:
            self._tool.close()
        super().close()


# ==========================================================================
# conversion: DjVu -> fixed-layout EPUB (text layer, outline, placeholders)
# ==========================================================================

def open_book(path: str, *, cache_root: str | None = None, content_key: str | None = None) -> DjvuBook:
    import bookformats
    spath = os.path.abspath(path)
    key = content_key or bookformats._content_key(spath)
    target = os.path.join(bookformats.converted_dir(cache_root),
                          f"{key}-djvu-v{DJVU_CONVERTER_VERSION}.epub")
    if not (os.path.isfile(target) and os.path.getsize(target) > 0):
        tool = DjvuTool(spath)
        try:
            info = tool.info()
            if "error" in info:
                raise EpubError("the DjVu file cannot be read", kind="corrupt", detail=info["error"])
            texts = tool.all_text()
        finally:
            tool.close()
        data = build_epub(spath, info, texts if isinstance(texts, list) else [])
        bookformats._write_atomic(target, data)
    book = DjvuBook.open(target)
    book.path = spath
    book.source_format = "djvu"
    book.attach_source(spath)
    return book


def _title_from_filename(path: str) -> str:
    stem = os.path.splitext(os.path.basename(path))[0]
    return " ".join(stem.replace("_", " ").split()) or stem


def build_epub(source: str, info: dict, texts: list) -> bytes:
    if info.get("truncated"):
        raise EpubError("the DjVu file is incomplete", kind="corrupt",
                        detail="the file is shorter than its header declares (a partial download?)")
    pages = info.get("pages") or []
    if not pages:
        raise EpubError("the DjVu file has no pages", kind="corrupt", detail="0 pages")
    if not any(int(p.get("w") or 0) > 0 and int(p.get("h") or 0) > 0 for p in pages):
        # a truncated file still parses, but none of its pages has a size left
        raise EpubError("the DjVu file is damaged", kind="corrupt",
                        detail=f"{len(pages)} page(s), none with page information")
    title = _title_from_filename(source)
    docs: list[tuple[str, str]] = []
    images: list[tuple[str, str]] = []
    for i, pg in enumerate(pages):
        w, h = max(1, int(pg.get("w") or 1)), max(1, int(pg.get("h") or 1))
        dpi = int(pg.get("dpi") or 300) or 300
        if int(pg.get("rot") or 0) in (90, 270):
            w, h = h, w
        scale = 96.0 / dpi
        cw, ch = max(1, round(w * scale)), max(1, round(h * scale))
        ext = "jpg" if pg.get("color") else "png"
        img = f"p{i + 1:04d}.{ext}"
        images.append((img, "image/jpeg" if ext == "jpg" else "image/png"))
        text = texts[i] if i < len(texts) and isinstance(texts[i], dict) else {}
        layer = _text_layer(text, scale) if not int(pg.get("rot") or 0) else ""
        body = (f'<img class="pg" src="{img}" alt="" width="{cw}" height="{ch}"/>'
                + (f'<div class="tl">{layer}</div>' if layer else ""))
        css = (f"html,body{{margin:0;padding:0}}"
               f".pg{{position:absolute;left:0;top:0;width:{cw}px;height:{ch}px}}"
               ".t{position:absolute;color:transparent;white-space:pre;line-height:1;overflow:hidden}")
        doc = ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
               '<html xmlns="http://www.w3.org/1999/xhtml"><head><meta charset="utf-8"/>'
               f"<title>{i + 1}</title>"
               f'<meta name="viewport" content="width={cw}, height={ch}"/>'
               f"<style>{css}</style></head>\n<body>{body}</body></html>\n")
        docs.append((f"p{i + 1:04d}.xhtml", doc))
    nav = _nav(title, info.get("outline") or [], len(pages))
    return _package(title, docs, images, nav)


def _text_layer(text: dict, scale: float) -> str:
    """OCR words as transparent, absolutely positioned spans (selectable and searchable).

    Words are separated by spaces and lines by newlines, so the flattened text reads
    naturally for search and copying.
    """
    out = []
    for line in text.get("lines") or []:
        if len(line) < 5 or not line[4]:
            continue
        words = []
        for wd in line[4]:
            if len(wd) < 5 or not str(wd[4]).strip():
                continue
            x0, y0, x1, y1, t = wd[:5]
            left, top = x0 * scale, y0 * scale
            width, height = max(1.0, (x1 - x0) * scale), max(1.0, (y1 - y0) * scale)
            words.append(f'<span class="t" style="left:{left:.1f}px;top:{top:.1f}px;width:{width:.1f}px;'
                         f'height:{height:.1f}px;font-size:{height * 0.85:.1f}px">{escape(str(t).strip())}</span>')
        if words:
            out.append(" ".join(words))
    return "\n".join(out)


def _nav(title: str, outline: list, count: int) -> str:
    entries: list[tuple[int, str, str]] = []

    def walk(items: list, level: int) -> None:
        for it in items:
            label = str(it.get("t") or "").strip()
            page = it.get("p", -1)
            if isinstance(page, int) and 0 <= page < count and label:
                entries.append((level, label, f"p{page + 1:04d}.xhtml"))
                walk(it.get("c") or [], level + 1)
            else:
                walk(it.get("c") or [], level)
    walk(outline, 0)
    if not entries:                      # no outline: a plain page list (language-neutral numbers)
        entries = [(0, str(n + 1), f"p{n + 1:04d}.xhtml") for n in range(count)]
    out = ['<?xml version="1.0" encoding="utf-8"?>',
           '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">',
           f"<head><title>{escape(title)}</title></head><body>", '<nav epub:type="toc" id="toc">']
    depth = -1
    for level, label, href in entries:
        level = max(0, min(level, depth + 1))
        if level > depth:
            out.append("<ol>" * (level - depth))
        else:
            out.append("</li>" + "</ol></li>" * (depth - level))
        out.append(f'<li><a href="pages/{href}">{escape(label)}</a>')
        depth = level
    out.append("</li>" + "</ol></li>" * depth + "</ol>")
    out.append("</nav></body></html>")
    return "\n".join(out)


def _package(title: str, docs: list[tuple[str, str]], images: list[tuple[str, str]], nav: str) -> bytes:
    ident = f"urn:uuid:{uuid.uuid4()}"
    manifest = ['<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>']
    spine = []
    for i, (name, _doc) in enumerate(docs):
        manifest.append(f'<item id="d{i}" href="pages/{name}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="d{i}"/>')
    for i, (name, mime) in enumerate(images):
        cover = ' properties="cover-image"' if i == 0 else ""
        manifest.append(f'<item id="i{i}" href="pages/{name}" media-type="{mime}"{cover}/>')
    opf = ('<?xml version="1.0" encoding="utf-8"?>\n'
           '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" '
           'prefix="rendition: http://www.idpf.org/vocab/rendition/#">\n'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
           f'<dc:identifier id="bookid">{ident}</dc:identifier>\n<dc:title>{escape(title)}</dc:title>\n'
           '<dc:language>und</dc:language>\n<meta property="dcterms:modified">2000-01-01T00:00:00Z</meta>\n'
           '<meta property="rendition:layout">pre-paginated</meta>\n'
           '<meta property="rendition:spread">none</meta>\n<meta name="cover" content="i0"/>\n'
           "</metadata>\n<manifest>\n" + "\n".join(manifest) + "\n</manifest>\n"
           "<spine>\n" + "\n".join(spine) + "\n</spine>\n</package>\n")
    container = ('<?xml version="1.0" encoding="utf-8"?>\n'
                 '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                 '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                 'media-type="application/oebps-package+xml"/></rootfiles></container>\n')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/content.opf", opf, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/nav.xhtml", nav, compress_type=zipfile.ZIP_DEFLATED)
        for name, doc in docs:
            z.writestr("OEBPS/pages/" + name, doc.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)
        for name, _mime in images:                 # placeholders: rendered on demand
            z.writestr("OEBPS/pages/" + name, b"", compress_type=zipfile.ZIP_STORED)
    return buf.getvalue()
