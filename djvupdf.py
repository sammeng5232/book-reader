# -*- coding: utf-8 -*-
"""DjVu -> PDF: a direct, mixed-raster conversion that keeps the scan's quality.

DjVu pages are layered: a full-resolution bi-level text mask (JB2), coloured by a
palette (FGbz) or a low-resolution foreground image (FG44/FGjp), over a
low-resolution background photo (BG44/BGjp).  PDF can express the same structure,
so nothing is flattened into one big JPEG:

* bi-level pages become one full-resolution 1-bit image (CCITT Group 4);
* the background becomes a JPEG at the background layer's own resolution;
* the mask becomes Group 4 stencil masks (``/ImageMask``), one per ink colour, or
  the foreground image masked by the full-resolution stencil (``/Mask``);
* pages with very many ink colours are composed by the decoder and stored as a
  single JPEG of at most 300 dpi.

The OCR text is added as invisible text (render mode 3), one positioned and
horizontally scaled run per word, in a tiny embedded "glyphless" TrueType font
whose CIDs are Unicode code points (the approach Tesseract uses), so searching,
selecting and copying work in any PDF viewer, in any script.  The DjVu outline
becomes the PDF outline.  Pages are written to disk as they are produced.

The decoding happens in the C# helper (``djvutool serve``, see :mod:`djvu`); this
module asks it for each page's layers with the ``layers`` request.

Public API::

    convert_djvu_to_pdf(src, dst, *, progress=None, cancelled=None, title=None) -> dict
    ExportCancelled
"""

from __future__ import annotations

import io
import os
import struct
import subprocess
import tempfile
import time
import zlib
from typing import BinaryIO, Callable

from djvu import DjvuTool, _title_from_filename
from epublib import EpubError

__all__ = ["convert_djvu_to_pdf", "ExportCancelled", "MAX_INK_COLOURS", "FALLBACK_DPI", "JPEG_QUALITY"]

#: More distinct ink colours than this on a page: the page is composed as one JPEG instead.
MAX_INK_COLOURS = 32
#: Resolution cap of such composed pages.
FALLBACK_DPI = 300
JPEG_QUALITY = 85
#: A foreground (ink colour) image whose channels vary less than this is painted as one colour.
FG_TOLERANCE = 24
PRODUCER = "Book Reader"

_FONT = "/F1"


class ExportCancelled(Exception):
    """The caller's ``cancelled()`` returned true; no output file was left behind."""


# ==========================================================================
# the helper: one extra request
# ==========================================================================

class _Plane:
    __slots__ = ("rgb", "from_fg", "x", "y", "w", "h", "bits")

    def __init__(self, rgb: tuple[int, int, int], from_fg: bool, x: int, y: int, w: int, h: int,
                 bits: bytes) -> None:
        self.rgb, self.from_fg = rgb, from_fg
        self.x, self.y, self.w, self.h, self.bits = x, y, w, h, bits


class _Layers:
    __slots__ = ("w", "h", "dpi", "rot", "has_mask", "too_many", "planes", "bg", "fg")

    def __init__(self) -> None:
        self.w = self.h = self.dpi = self.rot = 0
        self.has_mask = self.too_many = False
        self.planes: list[_Plane] = []
        self.bg: tuple[int, int, bytes] | None = None
        self.fg: tuple[int, int, bytes] | None = None


def _parse_layers(data: bytes) -> _Layers:
    """Parse the reply to ``layers`` (layout documented at ``Document.Layers`` in Program.cs)."""
    if data[:2] != b"L\x01":
        raise ValueError("unexpected layers reply")
    mv = memoryview(data)
    lay = _Layers()
    lay.w, lay.h, lay.dpi, lay.rot, flags, n = struct.unpack_from(">IIHHBH", data, 2)
    lay.has_mask, lay.too_many = bool(flags & 1), bool(flags & 2)
    pos = 2 + 4 + 4 + 2 + 2 + 1 + 2
    for _ in range(n):
        r, g, b, src, x, y, w, h, length = struct.unpack_from(">BBBBIIIII", data, pos)
        pos += 24
        lay.planes.append(_Plane((r, g, b), src == 1, x, y, w, h, bytes(mv[pos:pos + length])))
        pos += length
    for which in ("bg", "fg"):
        w, h = struct.unpack_from(">II", data, pos)
        pos += 8
        if w and h:
            setattr(lay, which, (w, h, bytes(mv[pos:pos + w * h * 3])))
            pos += w * h * 3
    if pos > len(data):
        raise ValueError("short layers reply")
    return lay


class _Tool(DjvuTool):
    """:class:`djvu.DjvuTool` plus the ``layers`` and single-page ``text`` requests.

    ``DjvuTool._start`` calls ``close()`` when the file cannot be opened, while
    ``request()`` holds the tool's (non-reentrant) lock: here that ``close()``
    stops the helper without taking the lock, so a bad file raises instead of hanging.
    """

    _starting = False

    def _start(self) -> None:
        self._starting = True
        try:
            super()._start()
        finally:
            self._starting = False

    def close(self) -> None:
        if not self._starting:
            super().close()
            return
        proc, self._proc = self._proc, None     # the caller holds the lock
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass
            for pipe in (proc.stdin, proc.stdout):
                try:
                    if pipe:
                        pipe.close()
                except OSError:
                    pass

    def layers(self, page: int, max_planes: int) -> _Layers:
        data = self.request(f"layers\t{page}\t{max_planes}", binary=True)
        if not data or data[:1] != b"L":
            raise EpubError("a DjVu page could not be decoded", kind="corrupt",
                            detail=data[1:].decode("utf-8", "replace") if data else "empty reply")
        return _parse_layers(data)

    def text(self, page: int) -> dict:
        res = self.request(f"text\t{page}")
        return res if isinstance(res, dict) and "error" not in res else {}


# ==========================================================================
# the glyphless font
# ==========================================================================

def _glyphless_ttf() -> bytes:
    """A TrueType font with two empty glyphs (.notdef and one blank, 500/1000 em wide)."""
    upem, adv, asc, desc = 1000, 500, 800, -200

    def name_table() -> bytes:
        names = {1: "GlyphLessFont", 2: "Regular", 3: "BookReader:GlyphLessFont",
                 4: "GlyphLessFont", 5: "Version 1.0", 6: "GlyphLessFont"}
        recs, blob = b"", b""
        for nid, text in names.items():
            enc = text.encode("utf-16-be")
            recs += struct.pack(">HHHHHH", 3, 1, 0x409, nid, len(enc), len(blob))
            blob += enc
        return struct.pack(">HHH", 0, len(names), 6 + 12 * len(names)) + recs + blob

    cmap_sub = struct.pack(">HHHHHHH", 4, 32, 0, 4, 4, 1, 0)       # format 4, segCountX2 = 4
    cmap_sub += struct.pack(">HH", 0x0020, 0xFFFF)                   # endCode
    cmap_sub += struct.pack(">H", 0)                                 # reservedPad
    cmap_sub += struct.pack(">HH", 0x0020, 0xFFFF)                   # startCode
    cmap_sub += struct.pack(">hh", 1 - 0x20, 1)                      # idDelta: ' ' -> glyph 1
    cmap_sub += struct.pack(">HH", 0, 0)                             # idRangeOffset
    tables = {
        b"OS/2": struct.pack(">HhHHH" + "h" * 10 + "h" + "10s" + "IIII" + "4s" + "HHH" + "hhh" + "HH" + "II"
                             + "hhHHH",
                             4, adv, 400, 5, 0, 650, 700, 0, 140, 650, 700, 0, 480, 50, 250, 0,
                             b"\0" * 10, 0, 0, 0, 0, b"NONE", 0x40, 0x20, 0xFFFF,
                             asc, desc, 0, asc, -desc, 1, 0, 500, 700, 0, 0x20, 1),
        b"cmap": struct.pack(">HHHHI", 0, 1, 3, 1, 12) + cmap_sub,
        b"glyf": b"\0\0\0\0",
        b"head": struct.pack(">IIIIHHQQhhhhHHhhh", 0x00010000, 0x00010000, 0, 0x5F0F3CF5, 0x000B, upem,
                             0, 0, 0, desc, adv, asc, 0, 8, 2, 0, 0),
        b"hhea": struct.pack(">IhhhHhhhhhhhhhhhH", 0x00010000, asc, desc, 0, adv, 0, 0, 0, 1, 0, 0,
                             0, 0, 0, 0, 0, 2),
        b"hmtx": struct.pack(">HhHh", adv, 0, adv, 0),
        b"loca": struct.pack(">HHH", 0, 0, 0),
        b"maxp": struct.pack(">IHHHHHHHHHHHHHH", 0x00010000, 2, 0, 0, 0, 0, 2, 0, 0, 0, 0, 0, 0, 0, 0),
        b"name": name_table(),
        b"post": struct.pack(">IIhhIIIII", 0x00030000, 0, -100, 50, 0, 0, 0, 0, 0),
    }

    def checksum(data: bytes) -> int:
        data += b"\0" * (-len(data) % 4)
        return sum(struct.unpack(f">{len(data) // 4}I", data)) & 0xFFFFFFFF

    tags = sorted(tables)
    n = len(tags)
    es = max(k for k in range(8) if 1 << k <= n)
    head = struct.pack(">IHHHH", 0x00010000, n, 16 << es, es, 16 * n - (16 << es))
    offset = 12 + 16 * n
    directory, body = b"", b""
    for tag in tags:
        data = tables[tag]
        directory += struct.pack(">4sIII", tag, checksum(data), offset + len(body), len(data))
        body += data + b"\0" * (-len(data) % 4)
    font = bytearray(head + directory + body)
    head_at = offset + sum(len(tables[t]) + (-len(tables[t]) % 4) for t in tags[:tags.index(b"head")])
    adjust = (0xB1B0AFBA - checksum(bytes(font))) & 0xFFFFFFFF
    font[head_at + 8:head_at + 12] = struct.pack(">I", adjust)
    return bytes(font)


def _to_unicode(astral: dict[str, int]) -> bytes:
    """CID -> Unicode: CID = code point on the BMP; surrogate CIDs carry the astral characters."""
    lines = ["/CIDInit /ProcSet findresource begin", "12 dict begin", "begincmap",
             "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
             "/CMapName /BookReader-Identity-UCS def", "/CMapType 2 def",
             "1 begincodespacerange", "<0000> <FFFF>", "endcodespacerange"]
    ranges = [f"<{hi:02X}00> <{hi:02X}FF> <{hi:02X}00>" for hi in range(256) if not 0xD8 <= hi <= 0xDF]
    for i in range(0, len(ranges), 100):
        chunk = ranges[i:i + 100]
        lines += [f"{len(chunk)} beginbfrange", *chunk, "endbfrange"]
    chars = [f"<{cid:04X}> <{ch.encode('utf-16-be').hex().upper()}>" for ch, cid in sorted(astral.items())]
    for i in range(0, len(chars), 100):
        chunk = chars[i:i + 100]
        lines += [f"{len(chunk)} beginbfchar", *chunk, "endbfchar"]
    lines += ["endcmap", "CMapName currentdict /CMap defineresource pop", "end", "end"]
    return ("\n".join(lines) + "\n").encode("ascii")


# ==========================================================================
# the PDF writer (streamed; classic xref table)
# ==========================================================================

def _num(v: float) -> str:
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def _text_string(s: str) -> str:
    """A PDF text string: UTF-16BE with a byte order mark, as a hex string."""
    return "<FEFF" + s.encode("utf-16-be").hex().upper() + ">"


class _PdfWriter:
    def __init__(self, fh: BinaryIO) -> None:
        self.fh = fh
        self.offsets: dict[int, int] = {}
        self.next = 1
        self.pos = 0
        self._write(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")

    def _write(self, data: bytes) -> None:
        self.fh.write(data)
        self.pos += len(data)

    def alloc(self) -> int:
        n = self.next
        self.next += 1
        return n

    def obj(self, num: int, body: str | bytes) -> int:
        self.offsets[num] = self.pos
        if isinstance(body, str):
            body = body.encode("latin-1")
        self._write(b"%d 0 obj\n" % num + body + b"\nendobj\n")
        return num

    def stream(self, num: int, entries: str, data: bytes, *, compress: bool = False) -> int:
        if compress:
            data = zlib.compress(data, 6)
            entries += " /Filter /FlateDecode"
        self.offsets[num] = self.pos
        self._write(b"%d 0 obj\n<< " % num + entries.encode("latin-1")
                    + b" /Length %d >>\nstream\n" % len(data))
        self._write(data)
        self._write(b"\nendstream\nendobj\n")
        return num

    def close(self, root: int, info: int) -> None:
        xref = self.pos
        size = self.next
        out = [b"xref\n0 %d\n" % size, b"0000000000 65535 f \n"]
        for n in range(1, size):
            off = self.offsets.get(n)
            out.append(b"%010d 00000 n \n" % off if off is not None else b"0000000000 65535 f \n")
        ident = os.urandom(16).hex().encode("ascii")
        out.append(b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R /ID [<%s> <%s>] >>\nstartxref\n%d\n%%%%EOF\n"
                   % (size, root, info, ident, ident, xref))
        self._write(b"".join(out))


# ==========================================================================
# page images
# ==========================================================================

def _image_1bit(bits: bytes, w: int, h: int) -> tuple[str, bytes]:
    """Packed rows (1 = no ink) -> (filter entries, data): CCITT Group 4, or Flate as a fallback."""
    try:
        from PIL import Image
        im = Image.frombytes("1", (w, h), bits)
        buf = io.BytesIO()
        im.save(buf, "TIFF", compression="group4", strip_size=1 << 30)
        buf.seek(0)
        with Image.open(buf) as tif:
            offs, counts = tif.tag_v2.get(273), tif.tag_v2.get(279)
            photometric = tif.tag_v2.get(262, 1)
        if offs and counts and len(offs) == 1:
            raw = buf.getvalue()
            data = raw[offs[0]:offs[0] + counts[0]]
            # the fax coder's "white" is a 0 bit: with BlackIsZero data (Pillow's "1") the
            # coded black runs are our paper, which PDF then decodes as 1 with /BlackIs1
            black_is_1 = "true" if photometric == 1 else "false"
            return (f"/Filter /CCITTFaxDecode /DecodeParms << /K -1 /Columns {w} /Rows {h} "
                    f"/BlackIs1 {black_is_1} >>", data)
    except Exception:  # noqa: BLE001 - Group 4 is an optimisation; Flate always works
        pass
    return "/Filter /FlateDecode", zlib.compress(bits, 6)


def _rgb_image(w: int, h: int, rgb: bytes):
    from PIL import Image
    return Image.frombytes("RGB", (w, h), rgb)


def _uniform(im, tolerance: int = 8) -> tuple[int, int, int] | None:
    """The colour of an image that is (nearly) one flat colour, else None."""
    from PIL import ImageStat
    ext = im.getextrema()
    if all(hi - lo <= tolerance for lo, hi in ext):
        return tuple(int(round(v)) for v in ImageStat.Stat(im).mean[:3])  # type: ignore[return-value]
    return None


def _jpeg(im) -> tuple[str, bytes]:
    """(colour space, JPEG bytes); grey images are stored as one-channel JPEGs."""
    from PIL import ImageChops
    r, g, b = im.split()
    grey = ImageChops.difference(r, g).getextrema()[1] <= 2 and ImageChops.difference(g, b).getextrema()[1] <= 2
    if grey:
        im = g
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return ("/DeviceGray" if grey else "/DeviceRGB"), buf.getvalue()


def _is_white(rgb: tuple[int, int, int]) -> bool:
    return min(rgb) >= 248


# ==========================================================================
# the text layer
# ==========================================================================

class _TextEncoder:
    """Characters -> 2-byte CIDs (the code point; astral characters get surrogate-range CIDs)."""

    def __init__(self) -> None:
        self.astral: dict[str, int] = {}

    def encode(self, text: str) -> tuple[str, int]:
        out = []
        for ch in text:
            cp = ord(ch)
            if cp < 0x20 or 0xD800 <= cp <= 0xDFFF:
                continue
            if cp > 0xFFFF:
                cid = self.astral.get(ch)
                if cid is None:
                    if len(self.astral) >= 0x800:
                        continue
                    cid = self.astral[ch] = 0xD800 + len(self.astral)
                cp = cid
            out.append(f"{cp:04X}")
        return "".join(out), len(out)


def _text_ops(text: dict, scale: float, page_h: float, enc: _TextEncoder) -> tuple[str, int]:
    """Invisible text for one page (top-down pixel boxes -> PDF points); returns (ops, words)."""
    ops: list[str] = []
    count = 0
    for line in text.get("lines") or []:
        if len(line) < 5 or not line[4]:
            continue
        words = [wd for wd in line[4] if len(wd) >= 5 and str(wd[4]).strip()]
        if not words:
            continue
        ly0 = min(line[1], *(wd[1] for wd in words))
        ly1 = max(line[3], *(wd[3] for wd in words))
        size = max(1.0, (ly1 - ly0) * scale)
        base = page_h - ly1 * scale + 0.2 * size
        for wd in words:
            x0, _y0, x1, _y1, t = wd[:5]
            hexs, n = enc.encode(str(t).strip())
            if not n:
                continue
            width = max(0.5, (x1 - x0) * scale)
            tz = 100.0 * width / (n * 0.5 * size)
            # a real space after every word, for copying; also after a line's last word,
            # or PDFium drops a one-letter word ("I", "a") that ends a line
            hexs += "0020"
            ops.append(f"{_num(size)} Tf {_num(tz)} Tz 1 0 0 1 {_num(x0 * scale)} {_num(base)} Tm <{hexs}> Tj")
            count += 1
    if not ops:
        return "", 0
    return f"BT 3 Tr {_FONT} 1 Tf\n" + "\n".join(f"{_FONT} {op}" for op in ops) + "\nET\n", count


# ==========================================================================
# conversion
# ==========================================================================

def _check_info(info: dict) -> list[dict]:
    """The same checks as :func:`djvu.build_epub`: truncated, no pages, no page with a size."""
    if not isinstance(info, dict) or "error" in info:
        raise EpubError("the DjVu file cannot be read", kind="corrupt",
                        detail=str(info.get("error") if isinstance(info, dict) else info))
    if info.get("truncated"):
        raise EpubError("the DjVu file is incomplete", kind="corrupt",
                        detail="the file is shorter than its header declares (a partial download?)")
    pages = info.get("pages") or []
    if not pages:
        raise EpubError("the DjVu file has no pages", kind="corrupt", detail="0 pages")
    if not any(int(p.get("w") or 0) > 0 and int(p.get("h") or 0) > 0 for p in pages):
        raise EpubError("the DjVu file is damaged", kind="corrupt",
                        detail=f"{len(pages)} page(s), none with page information")
    return pages


class _Converter:
    def __init__(self, tool: _Tool, pdf: _PdfWriter) -> None:
        self.tool, self.pdf = tool, pdf
        self.enc = _TextEncoder()
        self.pages_obj = pdf.alloc()
        self.font_obj = pdf.alloc()
        self.tounicode_obj = pdf.alloc()
        self.page_objs: list[int] = []
        self._write_font()

    def _write_font(self) -> None:
        pdf = self.pdf
        ttf = _glyphless_ttf()
        cid, desc, ff, gidmap = pdf.alloc(), pdf.alloc(), pdf.alloc(), pdf.alloc()
        pdf.obj(self.font_obj, f"<< /Type /Font /Subtype /Type0 /BaseFont /GlyphLessFont /Encoding /Identity-H "
                               f"/DescendantFonts [{cid} 0 R] /ToUnicode {self.tounicode_obj} 0 R >>")
        pdf.obj(cid, "<< /Type /Font /Subtype /CIDFontType2 /BaseFont /GlyphLessFont "
                     "/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
                     f"/FontDescriptor {desc} 0 R /DW 500 /CIDToGIDMap {gidmap} 0 R >>")
        pdf.obj(desc, "<< /Type /FontDescriptor /FontName /GlyphLessFont /Flags 5 /FontBBox [0 -200 500 800] "
                      f"/ItalicAngle 0 /Ascent 800 /Descent -200 /CapHeight 800 /StemV 80 /FontFile2 {ff} 0 R >>")
        pdf.stream(ff, f"/Length1 {len(ttf)}", ttf, compress=True)
        pdf.stream(gidmap, "", b"\x00\x01" * 65536, compress=True)

    def finish_font(self) -> None:
        self.pdf.stream(self.tounicode_obj, "", _to_unicode(self.enc.astral), compress=True)

    # ---- one page ----------------------------------------------------------
    def page(self, index: int, meta: dict, fallback_size: tuple[int, int, int]) -> tuple[bool, bool]:
        """Write page ``index``; returns (decoded, has_text)."""
        pdf = self.pdf
        w, h = int(meta.get("w") or 0), int(meta.get("h") or 0)
        dpi = int(meta.get("dpi") or 300) or 300
        rot = int(meta.get("rot") or 0) % 360
        sized = w > 0 and h > 0
        if not sized:
            w, h, dpi = fallback_size
        scale = 72.0 / dpi
        pw, ph = w * scale, h * scale
        xobjects: dict[str, int] = {}
        ops: list[str] = []
        ok = False
        if sized:
            try:
                ops = self._images(index, w, h, dpi, rot, scale, xobjects)
                ok = True
            except Exception:  # noqa: BLE001 - one bad page must not abort the book
                # (objects already written for it stay in the file, unreferenced)
                ops, xobjects = [], {}
        text_ops, words = "", 0
        if sized:
            try:
                text_ops, words = _text_ops(self.tool.text(index), scale, ph, self.enc)
            except Exception:  # noqa: BLE001 - the text layer is optional
                text_ops, words = "", 0
        content = ("\n".join(ops) + "\n" + text_ops).encode("latin-1")
        cobj = pdf.stream(pdf.alloc(), "", content, compress=True)
        res = f"/Font << {_FONT} {self.font_obj} 0 R >>"
        if xobjects:
            res += " /XObject << " + " ".join(f"/{k} {v} 0 R" for k, v in xobjects.items()) + " >>"
        pobj = pdf.alloc()
        pdf.obj(pobj, f"<< /Type /Page /Parent {self.pages_obj} 0 R /MediaBox [0 0 {_num(pw)} {_num(ph)}] "
                      + (f"/Rotate {rot} " if rot else "") + f"/Resources << {res} >> /Contents {cobj} 0 R >>")
        self.page_objs.append(pobj)
        return ok, words > 0

    def _images(self, index: int, w: int, h: int, dpi: int, rot: int, scale: float,
                xobjects: dict[str, int]) -> list[str]:
        pdf = self.pdf
        lay = self.tool.layers(index, MAX_INK_COLOURS)
        pw, ph = w * scale, h * scale
        ops: list[str] = []

        def add_image(entries: str, data: bytes) -> str:
            name = f"Im{len(xobjects)}"
            xobjects[name] = pdf.stream(pdf.alloc(), "/Type /XObject /Subtype /Image " + entries, data)
            return name

        def full_page(name: str) -> None:
            ops.append(f"q {_num(pw)} 0 0 {_num(ph)} 0 0 cm /{name} Do Q")

        if lay.too_many:
            self._composed(index, w, dpi, rot, full_page, add_image)
            return ops

        # ---- background ----
        bg_im = None
        if lay.bg is not None:
            bg_im = _rgb_image(*lay.bg)
            flat = _uniform(bg_im)
            if flat is not None:
                bg_im = None
                if not _is_white(flat):
                    ops.append(f"q {' '.join(_num(c / 255) for c in flat)} rg 0 0 {_num(pw)} {_num(ph)} re f Q")
        if bg_im is not None:
            cs, jpg = _jpeg(bg_im)
            name = add_image(f"/Width {bg_im.width} /Height {bg_im.height} /ColorSpace {cs} "
                             "/BitsPerComponent 8 /Filter /DCTDecode", jpg)
            full_page(name)
        # ---- foreground ----
        fg_colour = None
        fg_im = None
        if lay.fg is not None:
            fg_im = _rgb_image(*lay.fg)
            # the ink colour of scans is often "black, give or take a little": one colour will do
            fg_colour = _uniform(fg_im, FG_TOLERANCE)
            if fg_colour is not None:
                fg_im = None
        bilevel = (bg_im is None and not ops and len(lay.planes) == 1
                   and ((not lay.planes[0].from_fg and max(lay.planes[0].rgb) <= 8)
                        or (lay.planes[0].from_fg and fg_colour is not None and max(fg_colour) <= 8)))
        for pl in lay.planes:
            if pl.w <= 0 or pl.h <= 0:
                continue
            filt, data = _image_1bit(pl.bits, pl.w, pl.h)
            box = f"{_num(pl.w * scale)} 0 0 {_num(pl.h * scale)} {_num(pl.x * scale)} {_num((h - pl.y - pl.h) * scale)} cm"
            if bilevel:
                name = add_image(f"/Width {pl.w} /Height {pl.h} /ColorSpace /DeviceGray /BitsPerComponent 1 {filt}",
                                 data)
                ops.append(f"q {box} /{name} Do Q")
            elif pl.from_fg and fg_im is not None:
                mask = pdf.stream(pdf.alloc(), f"/Type /XObject /Subtype /Image /Width {pl.w} /Height {pl.h} "
                                               f"/ImageMask true /BitsPerComponent 1 {filt}", data)
                cs, jpg = _jpeg(fg_im)
                name = add_image(f"/Width {fg_im.width} /Height {fg_im.height} /ColorSpace {cs} /BitsPerComponent 8 "
                                 f"/Filter /DCTDecode /Mask {mask} 0 R", jpg)
                ops.append(f"q {box} /{name} Do Q")
            else:
                rgb = fg_colour if (pl.from_fg and fg_colour is not None) else pl.rgb
                name = add_image(f"/Width {pl.w} /Height {pl.h} /ImageMask true /BitsPerComponent 1 {filt}", data)
                ops.append(f"q {' '.join(_num(c / 255) for c in rgb)} rg {box} /{name} Do Q")
        return ops

    def _composed(self, index: int, w: int, dpi: int, rot: int,
                  full_page: Callable[[str], None], add_image: Callable[[str, bytes], str]) -> None:
        """A page with too many ink colours: the decoder composes it, as one JPEG of <= 300 dpi."""
        from PIL import Image
        width = max(1, min(w, round(w * FALLBACK_DPI / dpi)))
        data = self.tool.render(index, width, "jpg")
        with Image.open(io.BytesIO(data)) as im:
            if rot:                       # render() turned it upright; the PDF page rotates instead
                im = im.rotate(rot, expand=True)       # counter-clockwise by rot undoes it
                cs, data = _jpeg(im.convert("RGB"))
            else:
                cs = "/DeviceGray" if im.mode == "L" else "/DeviceRGB"
            iw, ih = im.size
        full_page(add_image(f"/Width {iw} /Height {ih} /ColorSpace {cs} /BitsPerComponent 8 /Filter /DCTDecode",
                            data))

    # ---- document structure ------------------------------------------------
    def outline(self, items: list) -> int | None:
        """Write the DjVu outline as PDF outline items; returns the /Outlines object or None."""
        count = len(self.page_objs)

        def clean(nodes: list) -> list[tuple[str, int, list]]:
            out: list[tuple[str, int, list]] = []
            for it in nodes or []:
                if not isinstance(it, dict):
                    continue
                label = " ".join(str(it.get("t") or "").split())
                page = it.get("p", -1)
                kids = clean(it.get("c") or [])
                if isinstance(page, int) and 0 <= page < count and label:
                    out.append((label, page, kids))
                else:
                    out.extend(kids)
            return out

        tree = clean(items)
        if not tree:
            return None
        pdf = self.pdf
        root = pdf.alloc()

        def write(nodes: list[tuple[str, int, list]], parent: int) -> tuple[int, int, int]:
            nums = [pdf.alloc() for _ in nodes]
            total = 0
            for i, (label, page, kids) in enumerate(nodes):
                entries = [f"/Title {_text_string(label)}", f"/Parent {parent} 0 R",
                           f"/Dest [{self.page_objs[page]} 0 R /XYZ null null null]"]
                if i > 0:
                    entries.append(f"/Prev {nums[i - 1]} 0 R")
                if i + 1 < len(nodes):
                    entries.append(f"/Next {nums[i + 1]} 0 R")
                if kids:
                    first, last, sub = write(kids, nums[i])
                    entries += [f"/First {first} 0 R", f"/Last {last} 0 R", f"/Count -{sub}"]
                pdf.obj(nums[i], "<< " + " ".join(entries) + " >>")
                total += 1
            return nums[0], nums[-1], total

        first, last, n = write(tree, root)
        pdf.obj(root, f"<< /Type /Outlines /First {first} 0 R /Last {last} 0 R /Count {n} >>")
        return root


def convert_djvu_to_pdf(src: str, dst: str, *, progress: Callable[[int, int], None] | None = None,
                        cancelled: Callable[[], bool] | None = None, title: str | None = None) -> dict:
    """Convert the DjVu book ``src`` into the PDF ``dst`` (written atomically).

    ``progress(done, total)`` is called after each page; ``cancelled()`` is polled
    between pages and, when true, stops the conversion with :class:`ExportCancelled`
    (no ``dst`` is written).  Unreadable or truncated files raise
    ``EpubError(kind="corrupt")``.  A page that cannot be decoded becomes a blank
    page of its size.  Returns ``{"pages", "bytes", "text_pages", "failed_pages",
    "seconds"}``.
    """
    started = time.monotonic()
    src = os.path.abspath(src)
    dst = os.path.abspath(dst)
    tool = _Tool(src)
    tmp: str | None = None
    try:
        info = tool.info()
        pages = _check_info(info)
        total = len(pages)
        first = next(p for p in pages if int(p.get("w") or 0) > 0 and int(p.get("h") or 0) > 0)
        fallback = (int(first["w"]), int(first["h"]), int(first.get("dpi") or 300) or 300)
        folder = os.path.dirname(dst) or "."
        fd, tmp = tempfile.mkstemp(prefix=".djvupdf-", suffix=".part", dir=folder)
        failed: list[int] = []
        text_pages = 0
        with os.fdopen(fd, "wb") as fh:
            pdf = _PdfWriter(fh)
            catalog, info_obj = pdf.alloc(), pdf.alloc()
            conv = _Converter(tool, pdf)
            for i, meta in enumerate(pages):
                if cancelled is not None and cancelled():
                    raise ExportCancelled()
                ok, has_text = conv.page(i, meta, fallback)
                if not ok:
                    failed.append(i)      # (a helper that died is restarted by the next request)
                if has_text:
                    text_pages += 1
                if progress is not None:
                    progress(i + 1, total)
            if cancelled is not None and cancelled():
                raise ExportCancelled()
            conv.finish_font()
            kids = " ".join(f"{p} 0 R" for p in conv.page_objs)
            pdf.obj(conv.pages_obj, f"<< /Type /Pages /Kids [{kids}] /Count {len(conv.page_objs)} >>")
            outlines = conv.outline(info.get("outline") or [])
            cat = f"<< /Type /Catalog /Pages {conv.pages_obj} 0 R"
            if outlines is not None:
                cat += f" /Outlines {outlines} 0 R /PageMode /UseOutlines"
            pdf.obj(catalog, cat + " >>")
            name = title if title and title.strip() else _title_from_filename(src)
            stamp = time.strftime("D:%Y%m%d%H%M%S")
            pdf.obj(info_obj, f"<< /Title {_text_string(name.strip())} /Producer ({PRODUCER}) "
                              f"/Creator ({PRODUCER}) /CreationDate ({stamp}) >>")
            pdf.close(catalog, info_obj)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dst)
        tmp = None
        return {"pages": total, "bytes": os.path.getsize(dst), "text_pages": text_pages,
                "failed_pages": failed, "seconds": round(time.monotonic() - started, 2)}
    finally:
        tool.close()
        if tmp is not None:
            try:
                os.remove(tmp)
            except OSError:
                pass
