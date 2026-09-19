# -*- coding: utf-8 -*-
"""Kindle books (MOBI / PRC / AZW / AZW3) converted to EPUB, from scratch.

The reader renders EPUB, so a Kindle book is turned into an equivalent EPUB
once and cached; every reader feature (positions, search, highlights, themes)
then works unchanged.  Nothing here writes next to the user's file.

Supported:

* PalmDB container, MOBI header, EXTH metadata (title, authors, publisher,
  language, description, subjects, date, ISBN/ASIN, cover, page direction)
* text compression: none, PalmDOC (LZ77), HUFF/CDIC (Huffman + dictionary)
* trailing-entry stripping (multibyte and TBS extra data)
* MOBI 6 ("classic" Mobipocket): chapter split at ``mbp:pagebreak``,
  ``filepos`` links, ``recindex`` images, NCX index table of contents
* KF8 (AZW3, and the KF8 half of MOBI/KF8 combo files): FDST flows, skeleton
  and fragment reconstruction, ``kindle:pos`` / ``kindle:embed`` /
  ``kindle:flow`` links, CSS flows, embedded fonts (zlib + XOR obfuscation),
  NCX index table of contents

Refused with :class:`MobiError` (the reader shows the matching card):

* DRM (Mobipocket encryption type 1 or 2) - ``kind="drm"``; never decrypted
* KFX and Topaz, which are not MOBI at all - ``kind="unsupported"``
* anything structurally broken - ``kind="corrupt"``

Public API::

    detect(data_or_path) -> "mobi" | "kfx" | "topaz" | None
    convert(path_or_bytes) -> bytes          # a complete EPUB 3 archive
    CONVERTER_VERSION                        # bump when output changes
"""

from __future__ import annotations

import html
import io
import os
import posixpath
import re
import struct
import uuid
import zlib
import zipfile
from dataclasses import dataclass, field
from typing import Iterable

__all__ = ["MobiError", "detect", "convert", "CONVERTER_VERSION", "palmdoc_decompress",
           "HuffCdic"]

#: Part of the conversion cache key: bump whenever the produced EPUB changes.
CONVERTER_VERSION = 2

NULL_INDEX = 0xFFFFFFFF
MAX_TEXT = 256 * 1024 * 1024          # refuse absurd declared text sizes


class MobiError(Exception):
    """A Kindle file that cannot be converted.

    ``kind`` is ``"drm"``, ``"unsupported"`` or ``"corrupt"``; ``detail`` is
    technical text for the reader's details disclosure.
    """

    def __init__(self, message: str, *, kind: str = "corrupt", detail: str = "",
                 drm_scheme: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.detail = detail or message
        self.drm_scheme = drm_scheme


# ==========================================================================
# detection
# ==========================================================================

def detect(source: "bytes | str | os.PathLike[str]") -> str | None:
    """Classify a file by its magic bytes, not its extension.

    ``"mobi"`` for any PalmDB book with a MOBI or TEXt header (MOBI, PRC, AZW,
    AZW3), ``"kfx"`` for KFX containers, ``"topaz"`` for Topaz, else ``None``.
    """
    if isinstance(source, (bytes, bytearray)):
        head = bytes(source[:96])
    else:
        with open(source, "rb") as fh:
            head = fh.read(96)
    if len(head) >= 68 and head[60:68] in (b"BOOKMOBI", b"TEXtREAd"):
        return "mobi"
    if head[:4] == b"TPZ0" or head[:3] == b"TPZ":
        return "topaz"
    if head[:4] in (b"CONT", b"\xeaDRM") or head[:8] == b"\xeaDRMION\xee":
        return "kfx"
    return None


# ==========================================================================
# PalmDB
# ==========================================================================

class PalmDB:
    """The record container every MOBI-family file sits in."""

    def __init__(self, data: bytes) -> None:
        if len(data) < 78:
            raise MobiError("file too short for a PalmDB header", detail=f"{len(data)} bytes")
        self.data = data
        self.name = data[:32].split(b"\0", 1)[0]
        self.type_creator = data[60:68]
        count = struct.unpack_from(">H", data, 76)[0]
        if 78 + 8 * count > len(data):
            raise MobiError("record list runs past the end of the file")
        offsets = [struct.unpack_from(">I", data, 78 + 8 * i)[0] for i in range(count)]
        offsets.append(len(data))
        for a, b in zip(offsets, offsets[1:]):
            if a > b or b > len(data):
                raise MobiError("record offsets are not increasing", detail=f"{a} > {b}")
        self._offsets = offsets

    def __len__(self) -> int:
        return len(self._offsets) - 1

    def record(self, i: int) -> bytes:
        if not 0 <= i < len(self):
            raise MobiError("record index out of range", detail=f"record {i} of {len(self)}")
        return self.data[self._offsets[i]:self._offsets[i + 1]]


# ==========================================================================
# decompression
# ==========================================================================

def palmdoc_decompress(src: bytes) -> bytes:
    """PalmDOC LZ77: literals, 1-8 byte literal runs, back references, space+char."""
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        i += 1
        if c == 0 or 0x09 <= c <= 0x7F:
            out.append(c)
        elif c <= 0x08:                       # 0x01..0x08: copy the next c bytes verbatim
            out += src[i:i + c]
            i += c
        elif c <= 0xBF:                       # 0x80..0xBF: 11-bit distance, 3-bit length
            if i >= n:
                break
            pair = ((c << 8) | src[i]) & 0x3FFF
            i += 1
            dist, length = pair >> 3, (pair & 7) + 3
            if dist == 0 or dist > len(out):
                continue                      # corrupt reference: skip, do not crash
            start = len(out) - dist
            if dist >= length:
                out += out[start:start + length]
            else:                             # overlapping copy repeats a short run
                for k in range(length):
                    out.append(out[start + k])
        else:                                 # 0xC0..0xFF: a space followed by c ^ 0x80
            out.append(0x20)
            out.append(c ^ 0x80)
    return bytes(out)


class HuffCdic:
    """HUFF/CDIC decompression (Mobipocket's Huffman coding over a phrase dictionary).

    The HUFF record gives a 256-entry lookup keyed by the next 8 code bits and
    per-length code ranges; each decoded symbol indexes a CDIC phrase that may
    itself be compressed (decoded recursively on first use).
    """

    def __init__(self, huff: bytes, cdics: Iterable[bytes]) -> None:
        if huff[:8] != b"HUFF\x00\x00\x00\x18":
            raise MobiError("bad HUFF record")
        off1, off2 = struct.unpack_from(">II", huff, 8)
        self._fast = []
        for v in struct.unpack_from(">256I", huff, off1):
            codelen, terminal, maxcode = v & 0x1F, bool(v & 0x80), v >> 8
            if codelen == 0:
                raise MobiError("HUFF table has a zero code length")
            self._fast.append((codelen, terminal, ((maxcode + 1) << (32 - codelen)) - 1))
        pairs = struct.unpack_from(">64I", huff, off2)
        self._mincode = [0] + [pairs[2 * (L - 1)] << (32 - L) for L in range(1, 33)]
        self._maxcode = [0] + [((pairs[2 * (L - 1) + 1] + 1) << (32 - L)) - 1 for L in range(1, 33)]
        self._phrases: list[list] = []
        for cdic in cdics:
            if cdic[:8] != b"CDIC\x00\x00\x00\x10":
                raise MobiError("bad CDIC record")
            total, bits = struct.unpack_from(">II", cdic, 8)
            count = min(1 << bits, total - len(self._phrases))
            for off in struct.unpack_from(f">{count}H", cdic, 16):
                blen = struct.unpack_from(">H", cdic, 16 + off)[0]
                body = cdic[18 + off:18 + off + (blen & 0x7FFF)]
                self._phrases.append([body, bool(blen & 0x8000)])   # flag: already literal

    def decompress(self, data: bytes, depth: int = 0) -> bytes:
        if depth > 32:
            raise MobiError("HUFF/CDIC phrase recursion too deep")
        bits_left = len(data) * 8
        padded = data + b"\0" * 8
        pos, n = 0, 32
        x = struct.unpack_from(">Q", padded, 0)[0]
        out = bytearray()
        fast, mincode, maxcode, phrases = self._fast, self._mincode, self._maxcode, self._phrases
        while True:
            if n <= 0:
                pos += 4
                x = struct.unpack_from(">Q", padded, pos)[0]
                n += 32
            code = (x >> n) & 0xFFFFFFFF
            codelen, terminal, mx = fast[code >> 24]
            if not terminal:
                while codelen < 32 and code < mincode[codelen]:
                    codelen += 1
                mx = maxcode[codelen]
            n -= codelen
            bits_left -= codelen
            if bits_left < 0:
                break
            idx = (mx - code) >> (32 - codelen)
            if idx >= len(phrases):
                raise MobiError("HUFF code points past the CDIC dictionary")
            entry = phrases[idx]
            if not entry[1]:
                entry[0] = self.decompress(entry[0], depth + 1)
                entry[1] = True
            out += entry[0]
        return bytes(out)


def _trailing_size(rec: bytes, flags: int) -> int:
    """Bytes of trailing data (TBS indexing, multibyte overlap) at a text record's end."""
    size = len(rec)
    total = 0
    bits = flags >> 1
    while bits:
        if bits & 1:
            # a backward variable-width integer: the byte furthest from the end has bit 7 set
            end = size - total
            value, shift, p = 0, 0, end
            while p > 0:
                b = rec[p - 1]
                value |= (b & 0x7F) << shift
                shift += 7
                p -= 1
                if b & 0x80 or shift >= 28:
                    break
            total += value
        bits >>= 1
    if flags & 1 and size - total - 1 >= 0:
        total += (rec[size - total - 1] & 0x3) + 1
    return min(total, size)


# ==========================================================================
# headers
# ==========================================================================

EXTH_NAMES = {100: "author", 101: "publisher", 103: "description", 104: "isbn",
              105: "subject", 106: "date", 108: "contributor", 109: "rights",
              113: "asin", 121: "kf8_boundary", 125: "resource_count", 129: "kf8_cover_uri",
              201: "cover_offset", 202: "thumb_offset", 503: "title", 524: "language",
              525: "writing_mode", 527: "page_direction"}


@dataclass
class Header:
    """One MOBI header (a combo file has two: MOBI 6 and KF8)."""
    start: int                    # record index of this header's record 0
    compression: int
    text_length: int
    text_records: int
    encryption: int
    mobi_type: int
    codepage: int
    version: int
    first_resource: int
    huff_offset: int
    huff_count: int
    extra_flags: int
    ncx_index: int
    fdst_index: int
    skel_index: int
    frag_index: int
    guide_index: int
    title: str
    exth: dict = field(default_factory=dict)      # tag -> list[bytes]

    @property
    def encoding(self) -> str:
        return "utf-8" if self.codepage == 65001 else "cp1252"

    @property
    def is_kf8(self) -> bool:
        return self.version >= 8

    def exth_text(self, tag: int) -> list[str]:
        return [v.decode(self.encoding, "replace").strip() for v in self.exth.get(tag, [])]

    def exth_int(self, tag: int) -> int | None:
        v = self.exth.get(tag)
        if v and len(v[0]) == 4:
            return struct.unpack(">I", v[0])[0]
        return None


def _u32(buf: bytes, off: int, default: int = NULL_INDEX) -> int:
    return struct.unpack_from(">I", buf, off)[0] if off + 4 <= len(buf) else default


def parse_header(pdb: PalmDB, start: int) -> Header:
    r0 = pdb.record(start)
    if len(r0) < 16:
        raise MobiError("record 0 is too short")
    compression, _, text_length, text_records, _, encryption = struct.unpack_from(">HHIHHH", r0, 0)
    is_mobi = r0[16:20] == b"MOBI"
    hlen = _u32(r0, 20, 0) if is_mobi else 0
    mobi_type = _u32(r0, 24, 0) if is_mobi else 0
    codepage = _u32(r0, 28, 1252) if is_mobi else 1252
    version = _u32(r0, 36, 1) if is_mobi else 1

    def field_at(off: int) -> int:
        # fields beyond the declared header length do not exist
        return _u32(r0, off) if is_mobi and off + 4 <= 16 + hlen else NULL_INDEX

    extra_flags = 0
    if is_mobi and hlen >= 0xE4 and len(r0) >= 0xF4:
        extra_flags = struct.unpack_from(">H", r0, 0xF2)[0]
    title = ""
    exth: dict[int, list[bytes]] = {}
    if is_mobi:
        name_off, name_len = _u32(r0, 0x54, 0), _u32(r0, 0x58, 0)
        enc = "utf-8" if codepage == 65001 else "cp1252"
        if 0 < name_off < len(r0) and name_len < 4096:
            title = r0[name_off:name_off + name_len].decode(enc, "replace").strip()
        if _u32(r0, 0x80, 0) & 0x40:
            p = 16 + hlen
            if r0[p:p + 4] == b"EXTH":
                count = _u32(r0, p + 8, 0)
                q = p + 12
                for _ in range(min(count, 10000)):
                    if q + 8 > len(r0):
                        break
                    tag, length = struct.unpack_from(">II", r0, q)
                    if length < 8:
                        break
                    exth.setdefault(tag, []).append(r0[q + 8:q + length])
                    q += length
    if not title:
        title = pdb.name.replace(b"_", b" ").decode("latin-1", "replace").strip()
    h = Header(start=start, compression=compression, text_length=text_length,
               text_records=text_records, encryption=encryption, mobi_type=mobi_type,
               codepage=codepage, version=version, first_resource=field_at(0x6C),
               huff_offset=field_at(0x70), huff_count=field_at(0x74), extra_flags=extra_flags,
               ncx_index=field_at(0xF4), fdst_index=field_at(0xC0) if version >= 8 else NULL_INDEX,
               skel_index=field_at(0xFC) if version >= 8 else NULL_INDEX,
               frag_index=field_at(0xF8) if version >= 8 else NULL_INDEX,
               guide_index=field_at(0x104) if version >= 8 else NULL_INDEX,
               title=title, exth=exth)
    exth_title = h.exth_text(503)
    if exth_title and exth_title[0]:
        h.title = exth_title[0]
    return h


def _abs(h: Header, idx: int) -> int:
    """KF8 index fields are relative to the header's own record 0."""
    return NULL_INDEX if idx == NULL_INDEX else idx + h.start


def read_text(pdb: PalmDB, h: Header) -> bytes:
    if h.encryption:
        raise MobiError("this Kindle book is DRM-protected", kind="drm",
                        drm_scheme="mobipocket", detail=f"MOBI encryption type {h.encryption}")
    if h.text_length > MAX_TEXT:
        raise MobiError("declared text size is implausible", detail=str(h.text_length))
    if h.compression == 1:
        decode = lambda b: b                                   # noqa: E731
    elif h.compression == 2:
        decode = palmdoc_decompress
    elif h.compression == 17480:                               # b"DH"
        if h.huff_offset == NULL_INDEX or not h.huff_count:
            raise MobiError("HUFF/CDIC book without HUFF records")
        base = h.start + h.huff_offset
        huff = HuffCdic(pdb.record(base), (pdb.record(base + k) for k in range(1, h.huff_count)))
        decode = huff.decompress
    else:
        raise MobiError("unknown text compression", kind="unsupported", detail=str(h.compression))
    parts = []
    for i in range(1, h.text_records + 1):
        rec = pdb.record(h.start + i)
        trail = _trailing_size(rec, h.extra_flags)
        parts.append(decode(rec[:len(rec) - trail] if trail else rec))
    text = b"".join(parts)
    return text[:h.text_length] if 0 < h.text_length <= len(text) else text


# ==========================================================================
# resources
# ==========================================================================

IMAGE_MAGIC = ((b"\xff\xd8\xff", "jpg", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
               (b"GIF87a", "gif", "image/gif"), (b"GIF89a", "gif", "image/gif"),
               (b"BM", "bmp", "image/bmp"))


def image_kind(data: bytes) -> tuple[str, str] | None:
    for magic, ext, mime in IMAGE_MAGIC:
        if data.startswith(magic):
            if ext == "bmp" and len(data) < 26:
                return None
            return ext, mime
    return None


def decode_font(rec: bytes) -> tuple[bytes, str, str] | None:
    """A KF8 ``FONT`` record -> (font bytes, extension, media type)."""
    if rec[:4] != b"FONT" or len(rec) < 24:
        return None
    _usize, flags, dstart, xor_len, xor_start = struct.unpack_from(">IIIII", rec, 4)
    body = bytearray(rec[dstart:])
    if flags & 2 and xor_len:
        key = rec[xor_start:xor_start + xor_len]
        for i in range(min(1040, len(body))):
            body[i] ^= key[i % xor_len]
    data = bytes(body)
    if flags & 1:
        try:
            data = zlib.decompress(data)
        except zlib.error:
            return None
    if data[:4] == b"OTTO":
        return data, "otf", "font/otf"
    if data[:4] in (b"\x00\x01\x00\x00", b"true"):
        return data, "ttf", "font/ttf"
    if data[:4] == b"wOFF":
        return data, "woff", "font/woff"
    return data, "ttf", "font/ttf"


# ==========================================================================
# INDX (NCX, skeleton, fragment, guide indexes)
# ==========================================================================

def _fwd_varint(buf: bytes, pos: int) -> tuple[int, int]:
    """Forward variable-width integer: 7 bits per byte, the LAST byte has bit 7 set."""
    value = 0
    start = pos
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        value = (value << 7) | (b & 0x7F)
        if b & 0x80:
            break
    return pos - start, value


def _count_bits(v: int) -> int:
    return bin(v).count("1")


def read_index(pdb: PalmDB, idx: int, encoding: str) -> tuple[list[tuple[bytes, dict]], dict[int, str]]:
    """Entries ``(key, {tag: [values]})`` of one INDX table, plus its CNCX strings by offset."""
    if idx == NULL_INDEX or idx >= len(pdb):
        return [], {}
    main = pdb.record(idx)
    if main[:4] != b"INDX":
        raise MobiError("index record is not INDX", detail=f"record {idx}")
    words = struct.unpack_from(">13I", main, 4)
    hlen, index_count, ncncx = words[0], words[5], words[12]
    # ORDT remapping of entry keys (rare; seen in some sample books)
    ordt2 = None
    if len(main) >= 0xB8:
        ocnt, oentries, _op1, op2, _otagx = struct.unpack_from(">5I", main, 0xA4)
        if (words[6] == 0xFDEA or ocnt) and oentries and main[op2:op2 + 4] == b"ORDT":
            ordt2 = struct.unpack_from(f">{oentries}H", main, op2 + 4)
    # TAGX: the per-entry tag table
    control_bytes, tag_table = 0, []
    if main[hlen:hlen + 4] == b"TAGX":
        first = struct.unpack_from(">I", main, hlen + 4)[0]
        control_bytes = struct.unpack_from(">I", main, hlen + 8)[0]
        for p in range(hlen + 12, hlen + first, 4):
            tag_table.append(tuple(main[p:p + 4]))
    # CNCX strings follow the index records
    cncx: dict[int, str] = {}
    for j in range(ncncx):
        rec = pdb.record(idx + index_count + 1 + j)
        off = 0
        while off < len(rec) and rec[off] != 0:
            here = off
            used, ln = _fwd_varint(rec, off)
            off += used
            cncx[here + j * 0x10000] = rec[off:off + ln].decode(encoding, "replace")
            off += ln
    entries: list[tuple[bytes, dict]] = []
    for r in range(idx + 1, idx + 1 + index_count):
        rec = pdb.record(r)
        if rec[:4] != b"INDX":
            continue
        idxt, count = struct.unpack_from(">I", rec, 20)[0], struct.unpack_from(">I", rec, 24)[0]
        positions = [struct.unpack_from(">H", rec, idxt + 4 + 2 * k)[0] for k in range(count)]
        positions.append(idxt)
        for k in range(count):
            p, end = positions[k], positions[k + 1]
            klen = rec[p]
            key = rec[p + 1:p + 1 + klen]
            if ordt2 is not None:
                key = bytes(ordt2[b] & 0xFF for b in key)
            entries.append((key, _tag_map(control_bytes, tag_table, rec, p + 1 + klen, end)))
    return entries, cncx


def _tag_map(control_bytes: int, table: list, rec: bytes, start: int, end: int) -> dict[int, list[int]]:
    found = []
    cb_index = 0
    data = start + control_bytes
    for tag, per_entry, mask, end_flag in table:
        if end_flag == 1:
            cb_index += 1
            continue
        if start + cb_index >= len(rec):
            break
        value = rec[start + cb_index] & mask
        if not value:
            continue
        if value == mask and _count_bits(mask) > 1:
            used, nbytes = _fwd_varint(rec, data)       # a byte count, not a value count
            data += used
            found.append((tag, None, nbytes, per_entry))
        else:
            if value == mask:
                count = 1
            else:
                m = mask
                while m & 1 == 0:
                    m >>= 1
                    value >>= 1
                count = value
            found.append((tag, count, None, per_entry))
    out: dict[int, list[int]] = {}
    for tag, count, nbytes, per_entry in found:
        vals: list[int] = []
        if count is not None:
            for _ in range(count * per_entry):
                used, v = _fwd_varint(rec, data)
                data += used
                vals.append(v)
        else:
            consumed = 0
            while consumed < nbytes:
                used, v = _fwd_varint(rec, data)
                data += used
                consumed += used
                vals.append(v)
        out[tag] = vals
    return out


@dataclass
class NavPoint:
    label: str
    target: tuple            # MOBI6: (filepos,)   KF8: (fid, offset)
    level: int
    parent: int
    children: list = field(default_factory=list)


def read_ncx(pdb: PalmDB, h: Header) -> list[NavPoint]:
    idx = _abs(h, h.ncx_index)
    if idx == NULL_INDEX:
        return []
    try:
        entries, cncx = read_index(pdb, idx, h.encoding)
    except (MobiError, struct.error, IndexError):
        return []
    points = []
    for _key, tags in entries:
        label = cncx.get(tags.get(3, [None])[0], "") if 3 in tags else ""
        if h.is_kf8 and 6 in tags and len(tags[6]) >= 2:
            target = (tags[6][0], tags[6][1])
        elif 1 in tags:
            target = (tags[1][0],)
        else:
            continue
        points.append(NavPoint(label=label.strip(), target=target,
                               level=tags.get(4, [0])[0], parent=tags.get(21, [-1])[0]))
    return points


# ==========================================================================
# EPUB assembly (shared by both converters)
# ==========================================================================

XHTML_NS = "http://www.w3.org/1999/xhtml"
_INVALID_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


@dataclass
class EpubParts:
    title: str
    authors: list[str]
    language: str
    metadata: dict
    docs: list[tuple[str, str]] = field(default_factory=list)          # (name, xhtml text)
    resources: list[tuple[str, bytes, str]] = field(default_factory=list)  # (name, data, mime)
    toc: list[tuple[int, str, str]] = field(default_factory=list)      # (level, label, href)
    cover: str | None = None
    page_direction: str = ""


def _xml_text(s: str) -> str:
    return html.escape(_INVALID_XML.sub("", s), quote=True)


def build_epub(p: EpubParts) -> bytes:
    """Write a valid EPUB 3 (mimetype first and stored; OPF; nav) from converted parts."""
    ident = p.metadata.get("identifier") or f"urn:uuid:{uuid.uuid4()}"
    meta = [f'<dc:identifier id="bookid">{_xml_text(ident)}</dc:identifier>',
            f"<dc:title>{_xml_text(p.title or 'Untitled')}</dc:title>",
            f"<dc:language>{_xml_text(p.language or 'und')}</dc:language>",
            '<meta property="dcterms:modified">2000-01-01T00:00:00Z</meta>']
    for a in p.authors:
        meta.append(f"<dc:creator>{_xml_text(a)}</dc:creator>")
    for key in ("publisher", "description", "date", "rights"):
        if p.metadata.get(key):
            meta.append(f"<dc:{key}>{_xml_text(p.metadata[key])}</dc:{key}>")
    for s in p.metadata.get("subjects", []):
        meta.append(f"<dc:subject>{_xml_text(s)}</dc:subject>")
    manifest = ['<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>']
    spine = []
    for i, (name, _text) in enumerate(p.docs):
        manifest.append(f'<item id="d{i}" href="{_xml_text(name)}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="d{i}"/>')
    for i, (name, _data, mime) in enumerate(p.resources):
        props = ' properties="cover-image"' if name == p.cover else ""
        manifest.append(f'<item id="r{i}" href="{_xml_text(name)}" media-type="{mime}"{props}/>')
        if name == p.cover:
            meta.append(f'<meta name="cover" content="r{i}"/>')
    ppd = f' page-progression-direction="{p.page_direction}"' if p.page_direction in ("ltr", "rtl") else ""
    opf = ('<?xml version="1.0" encoding="utf-8"?>\n'
           '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">\n'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n' + "\n".join(meta) + "\n</metadata>\n"
           "<manifest>\n" + "\n".join(manifest) + "\n</manifest>\n"
           f"<spine{ppd}>\n" + "\n".join(spine) + "\n</spine>\n</package>\n")
    nav = _nav_document(p)
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
        for name, text in p.docs:
            z.writestr("OEBPS/" + name, text.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)
        for name, data, mime in p.resources:
            kind = zipfile.ZIP_STORED if mime.startswith("image/") and not mime.endswith("bmp") \
                else zipfile.ZIP_DEFLATED
            z.writestr("OEBPS/" + name, data, compress_type=kind)
    return buf.getvalue()


def _nav_document(p: EpubParts) -> str:
    out = ['<?xml version="1.0" encoding="utf-8"?>',
           f'<html xmlns="{XHTML_NS}" xmlns:epub="http://www.idpf.org/2007/ops">',
           f"<head><title>{_xml_text(p.title)}</title></head><body>",
           '<nav epub:type="toc" id="toc">']
    entries = p.toc or [(0, _xml_text(p.title or name), name) for name, _ in p.docs[:1]]
    depth = -1
    for level, label, href in entries:
        level = max(0, min(level, depth + 1))
        if level > depth:
            out.append("<ol>" * (level - depth))
        else:
            out.append("</li>" + "</ol></li>" * (depth - level))
        out.append(f'<li><a href="{_xml_text(href)}">{_xml_text(label) or "&#8203;"}</a>')
        depth = level
    if depth >= 0:
        out.append("</li>" + "</ol></li>" * depth + "</ol>")
    out.append("</nav></body></html>")
    return "\n".join(out)


def xhtml_document(title: str, body: str, *, head_extra: str = "", lang: str = "") -> str:
    lang_attr = f' xml:lang="{_xml_text(lang)}" lang="{_xml_text(lang)}"' if lang else ""
    return ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
            f'<html xmlns="{XHTML_NS}"{lang_attr}><head><meta charset="utf-8"/>'
            f"<title>{_xml_text(title)}</title>{head_extra}</head>\n"
            f"<body>{body}</body></html>\n")


def html_fragment_to_xhtml(fragment: str) -> str:
    """Parse lenient (MOBI 6) HTML and serialize it as well-formed XHTML body content.

    Both the reader's Python parser and Chromium then see the same XML tree,
    which the position/search invariant depends on.
    """
    from lxml import etree
    from lxml import html as lhtml

    fragment = _INVALID_XML.sub("", fragment)
    try:
        root = lhtml.document_fromstring("<html><body>" + fragment + "</body></html>")
    except (etree.ParserError, ValueError):
        return "<p>" + _xml_text(re.sub(r"<[^>]*>", "", fragment)) + "</p>"
    body = root.find("body")
    if body is None:
        body = root
    for el in list(body.iter()):
        if el is body or el.getparent() is None:
            continue
        if not isinstance(el.tag, str):              # comments, processing instructions
            _drop_keep_tail(el)
        elif el.tag in ("script", "style", "guide", "reference", "head", "meta", "title", "link"):
            el.drop_tree()                           # metadata: remove with its content
        elif ":" in el.tag or el.tag in ("html", "body"):
            el.drop_tag()                            # mbp:* and stray wrappers: keep content
        else:
            for attr in list(el.attrib):
                if ":" in attr or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", attr) \
                        or attr.lower().startswith("on"):
                    del el.attrib[attr]
    # Serialized without a namespace, the elements inherit the default XHTML
    # namespace declared on the wrapping document's <html>.
    parts = [_xml_text(body.text)] if body.text else []
    for child in body:
        parts.append(etree.tostring(child, method="xml", encoding="unicode", with_tail=True))
    return "".join(parts)


def _drop_keep_tail(el) -> None:
    parent = el.getparent()
    prev = el.getprevious()
    if prev is not None:
        prev.tail = (prev.tail or "") + (el.tail or "")
    else:
        parent.text = (parent.text or "") + (el.tail or "")
    parent.remove(el)


# A chapter heading at the start of a line: the token must be followed by space,
# a dash, or the end of the line, so running text such as "第四回中…" is not one.
_HEADING = re.compile(
    r"(?m)^[ \t　]*(?P<tok>第[ \t　]*[0-9０-９一二三四五六七八九十百千零〇两廿卅]+[ \t　]*[回章节節卷部篇集幕]"
    r"|(?:CHAPTER|Chapter|BOOK|Book|PART|Part)[ \t]+(?:[0-9]+|[IVXLCDM]+)\b\.?"
    r"|楔子|尾声|尾聲|后记|後記|前言|引子)(?=[ \t　\r\n—\-－.:：]|$)")
# The same CJK token in mid-paragraph, where plain-text sources whose line breaks
# became spaces put their headings.  Only three unambiguous positions count: at the
# end of the paragraph ("…看時- 第三回</p>"), right before a dash rule
# ("… 第六回 ————"), or right after the classic chapter-ending formula
# ("…且听下回分解． 第十回 …").
_CJK_TOKEN = r"第[ \t　]*[0-9０-９一二三四五六七八九十百千零〇两廿卅]+[ \t　]*[回章节節卷部篇集幕]"
_HEADING_AT_END = re.compile(
    # token (+ a title of up to 40 non-sentence characters) then a dash rule or paragraph end
    r"(?<=[ \t　\r\n])(?P<tok>" + _CJK_TOKEN + r")"
    r"(?=[^，。：；！？“”「」\n]{0,40}?(?:$|[—\-－=＝]{3,}))"
    # or: the chapter-ending formula, possibly broken by a wrapped line, then the token
    r"|下回[ \t　\r\n]*分解[ \t　\r\n]*[．。.]?[ \t　\r\n]*(?P<tok2>" + _CJK_TOKEN + r")")
_DASHES = re.compile(r"[—\-－=＝_＿·•*＊]{3,}")


def _heading_label(token: str, rest: str) -> str:
    """The heading token plus the chapter title that follows it (dash rules skipped)."""
    lines = [ln for ln in (_DASHES.sub(" ", x).strip() for x in rest.split("\n")) if ln]
    title = lines[0] if lines else ""
    # a chapter title has no sentence punctuation: stop where running text begins
    title = re.split(r"[，。：；！？“”「」『』,;!?\"]|話說|话说|卻說|却说|且說|且说", title, maxsplit=1)[0]
    label = " ".join((token.strip() + " " + title).split())
    return label[:60]


def add_heading_toc(parts: EpubParts, *, min_entries: int = 3) -> None:
    """Give a book without a usable table of contents one built from its chapter headings.

    Runs only when the book's own index has fewer than two entries.  Headings are
    found at line starts anywhere in the text (plain-text-derived books put them
    inside long paragraphs); an empty ``<a id>`` is inserted in front of each so
    the entry lands on it.  An empty element adds no characters, so the
    Python/Chromium flattened-text invariant is untouched.  Failing that, each
    document becomes one entry titled by its first line.
    """
    if len(parts.toc) >= 2:
        return
    from lxml import etree

    ns = "{%s}" % XHTML_NS
    headings: list[tuple[int, str, str]] = []
    firsts: list[tuple[int, str, str]] = []
    new_docs = []
    serial = 0
    for name, text in parts.docs:
        try:
            root = etree.fromstring(text.encode("utf-8"))
        except etree.XMLSyntaxError:
            new_docs.append((name, text))
            continue
        body = root.find(ns + "body")
        if body is None:
            new_docs.append((name, text))
            continue
        first = " ".join("".join(body.itertext()).split())[:40]
        if first:
            firsts.append((0, first, name))
        def slots(el):
            """Text slots in document order: el.text, then each child's subtree and tail."""
            yield el, "text"
            for child in el:
                if isinstance(child.tag, str):
                    yield from slots(child)
                yield child, "tail"

        doc_heads: list[tuple[str, str]] = []
        all_slots = list(slots(body))
        texts = [(el.text if w == "text" else el.tail) or "" for el, w in all_slots]
        for si, (el, which) in enumerate(all_slots):
            s = texts[si]
            if not s:
                continue
            by_pos: dict[int, tuple[int, int, str]] = {}
            for pattern in (_HEADING, _HEADING_AT_END):
                for m in pattern.finditer(s):
                    g = "tok" if m.groupdict().get("tok") else "tok2"
                    by_pos.setdefault(m.start(g), (m.start(g), m.end(g), m.group(g)))
            matches = [by_pos[k] for k in sorted(by_pos)]
            if not matches:
                continue
            found = []
            cut = len(s)
            for at, tok_end, token in reversed(matches):   # split from the end: offsets stay valid
                serial += 1
                anchor = etree.Element(ns + "a")
                anchor.set("id", f"toc-{serial}")
                anchor.tail = s[at:cut]           # exactly the text up to the next heading
                cut = at
                if which == "text":
                    el.insert(0, anchor)          # the earliest ends up first
                else:
                    el.addnext(anchor)            # the earliest ends up nearest el
                # the title may follow in the next paragraphs (after dash rules)
                ahead = s[tok_end:] + "\n" + "\n".join(texts[si + 1:si + 8])
                found.append((_heading_label(token, ahead[:400]), f"toc-{serial}"))
            if which == "text":
                el.text = s[:cut]
            else:
                el.tail = s[:cut]
            doc_heads.extend(reversed(found))
        for label, ident in doc_heads:
            headings.append((0, label, f"{name}#{ident}"))
        if doc_heads:
            title = root.find(f"{ns}head/{ns}title")
            if title is not None:
                title.text = doc_heads[0][0]
        new_docs.append((name, etree.tostring(root, xml_declaration=True, encoding="utf-8").decode("utf-8")))
    parts.docs = new_docs
    if len(headings) >= min_entries:
        parts.toc = headings
    elif len(firsts) >= 2:
        parts.toc = firsts


def _metadata(h: Header) -> tuple[list[str], str, dict]:
    authors = [a for a in h.exth_text(100) if a]
    language = (h.exth_text(524) or [""])[0]
    meta: dict = {"subjects": [s for s in h.exth_text(105) if s]}
    for tag, key in ((101, "publisher"), (103, "description"), (106, "date"), (109, "rights")):
        v = h.exth_text(tag)
        if v and v[0]:
            meta[key] = re.sub(r"<[^>]+>", "", v[0]).strip()
    isbn, asin = h.exth_text(104), h.exth_text(113)
    if isbn and isbn[0]:
        meta["identifier"] = "urn:isbn:" + isbn[0]
    elif asin and asin[0]:
        meta["identifier"] = "urn:asin:" + asin[0]
    return authors, language, meta


def _page_direction(h: Header) -> str:
    v = (h.exth_text(527) or [""])[0].lower()
    return v if v in ("ltr", "rtl") else ""


# ==========================================================================
# MOBI 6
# ==========================================================================

_PAGEBREAK = re.compile(rb"<\s*mbp:pagebreak[^>]*>", re.I)
_FILEPOS = re.compile(rb"""filepos\s*=\s*['"]?0*(\d+)['"]?""", re.I)
_RECINDEX = re.compile(rb"""(?:hi|lo)?recindex\s*=\s*['"]?0*(\d+)['"]?""", re.I)


def _collect_images(pdb: PalmDB, first: int, limit: int) -> dict[int, tuple[str, bytes, str]]:
    """1-based image number -> (name, data, mime), counting every record from ``first``."""
    images: dict[int, tuple[str, bytes, str]] = {}
    if first == NULL_INDEX:
        return images
    for n, rec_no in enumerate(range(first, limit), start=1):
        data = pdb.record(rec_no)
        kind = image_kind(data)
        if kind:
            ext, mime = kind
            if ext == "bmp":
                data, ext, mime = _bmp_to_png(data)
            images[n] = (f"images/img{n:05d}.{ext}", data, mime)
    return images


def _bmp_to_png(data: bytes) -> tuple[bytes, str, str]:
    try:
        from PIL import Image
        out = io.BytesIO()
        Image.open(io.BytesIO(data)).save(out, "PNG")
        return out.getvalue(), "png", "image/png"
    except Exception:  # noqa: BLE001 - keep the original rather than lose the image
        return data, "bmp", "image/bmp"


def _content_end(pdb: PalmDB, h: Header) -> int:
    """First record after the book's resources (stops at BOUNDARY / EOF markers)."""
    for i in range(h.start + 1, len(pdb)):
        head = pdb.record(i)[:8]
        if head in (b"BOUNDARY", b"\xe9\x8e\r\n") or head[:4] in (b"FLIS", b"FCIS", b"SRCS", b"DATP"):
            if i > (h.first_resource if h.first_resource != NULL_INDEX else 0):
                return i
    return len(pdb)


def convert_mobi6(pdb: PalmDB, h: Header) -> EpubParts:
    raw = read_text(pdb, h)
    first_img = h.first_resource if h.first_resource != NULL_INDEX else NULL_INDEX
    images = _collect_images(pdb, first_img, _content_end(pdb, h)) if first_img != NULL_INDEX else {}
    ncx = read_ncx(pdb, h)

    # anchors for every filepos target (links and NCX), inserted from the end
    targets = {int(m.group(1)) for m in _FILEPOS.finditer(raw)}
    targets |= {p.target[0] for p in ncx if len(p.target) == 1}
    targets = {t for t in targets if 0 <= t <= len(raw)}
    body_start = raw.lower().find(b"<body")
    buf = bytearray(raw)
    for pos in sorted(targets, reverse=True):
        at = pos
        lt, gt = buf.rfind(b"<", 0, at), buf.rfind(b">", 0, at)
        if lt > gt:                                   # inside a tag: move before it
            at = lt
        if 0 <= body_start and at < body_start:
            at = body_start
        buf[at:at] = b'<a id="fp%d"></a>' % pos
    text = bytes(buf)
    # body only; split at page breaks
    lower = text.lower()
    b0 = lower.find(b"<body")
    if b0 >= 0:
        text = text[text.find(b">", b0) + 1:]
    b1 = text.lower().rfind(b"</body>")
    if b1 >= 0:
        text = text[:b1]
    chunks = [c for c in _PAGEBREAK.split(text)]
    chunks = [c for c in chunks if c.strip()] or [b""]

    names = [f"text/part{i + 1:04d}.xhtml" for i in range(len(chunks))]
    where: dict[int, str] = {}
    for name, chunk in zip(names, chunks):
        for m in re.finditer(rb'<a id="fp(\d+)"></a>', chunk):
            where[int(m.group(1))] = name

    def link(m: re.Match) -> bytes:
        pos = int(m.group(1))
        target = where.get(pos)
        return b'href="%s#fp%d"' % ((posixpath.basename(target) if target else "").encode(), pos) \
            if target else b'href="#"'

    def img(m: re.Match) -> bytes:
        n = int(m.group(1))
        return b'src="../%s"' % images[n][0].encode() if n in images else b'data-missing-image="%d"' % n

    parts = EpubParts(title=h.title, authors=[], language="", metadata={})
    parts.authors, parts.language, parts.metadata = _metadata(h)
    for name, chunk in zip(names, chunks):
        chunk = _FILEPOS.sub(link, chunk)
        chunk = _RECINDEX.sub(img, chunk)
        body = html_fragment_to_xhtml(chunk.decode(h.encoding, "replace"))
        parts.docs.append((name, xhtml_document(h.title, body, lang=parts.language)))
    parts.resources = [(name, data, mime) for name, data, mime in images.values()]
    cover = h.exth_int(201)
    if cover is not None and cover != NULL_INDEX and (cover + 1) in images:
        parts.cover = images[cover + 1][0]
    parts.page_direction = _page_direction(h)

    for p in ncx:
        target = where.get(p.target[0])
        if target:
            parts.toc.append((p.level, p.label, f"{target}#fp{p.target[0]}"))
    return parts


# ==========================================================================
# KF8
# ==========================================================================

_KINDLE_POS = re.compile(rb"""kindle:pos:fid:([0-9A-Va-v]+):off:([0-9A-Va-v]+)""")
_KINDLE_EMBED = re.compile(rb"""kindle:embed:([0-9A-Va-v]+)(?:\?mime=[^'")\s]*)?""")
_KINDLE_FLOW = re.compile(rb"""kindle:flow:([0-9A-Va-v]+)(?:\?mime=[^'")\s]*)?""")
_ID_ATTR = re.compile(rb"""<[^>]*\s(?:id|name)\s*=\s*['"]([^'"]*)['"]""", re.I)
_AID_ATTR = re.compile(rb"""<[^>]+\said\s*=\s*['"]([^'"]+)['"]""", re.I)


def convert_kf8(pdb: PalmDB, h: Header) -> EpubParts:
    raw = read_text(pdb, h)
    # flows: 0 = markup, 1.. = CSS / SVG
    flows = [raw]
    fdst = _abs(h, h.fdst_index)
    if fdst != NULL_INDEX and fdst < len(pdb):
        rec = pdb.record(fdst)
        if rec[:4] == b"FDST":
            count = struct.unpack_from(">I", rec, 8)[0]
            bounds = struct.unpack_from(f">{2 * count}I", rec, 12)
            flows = [raw[bounds[2 * k]:bounds[2 * k + 1]] for k in range(count)]
    text = flows[0]
    skel, _ = read_index(pdb, _abs(h, h.skel_index), h.encoding)
    frags, frag_cncx = read_index(pdb, _abs(h, h.frag_index), h.encoding)
    frag_table = []                      # (insertpos, selector, file, seq, start, length)
    for key, tags in frags:
        try:
            frag_table.append((int(key), frag_cncx.get(tags[2][0], ""), tags[3][0], tags[4][0],
                               tags[6][0], tags[6][1]))
        except (KeyError, IndexError, ValueError):
            continue
    if not skel:
        raise MobiError("KF8 book without a skeleton index", detail="skeleton index empty")

    # reconstruct each part: skeleton text with its fragments inserted
    parts_text: list[bytes] = []
    part_span: list[tuple[int, int]] = []          # (skeleton start, end) in flow-0 positions
    fptr = 0
    for _key, tags in skel:
        frag_count = tags.get(1, [0])[0]
        skel_pos, skel_len = tags[6][0], tags[6][1]
        base = skel_pos + skel_len
        doc = text[skel_pos:base]
        for _ in range(frag_count):
            if fptr >= len(frag_table):
                break
            insert, _sel, _file, _seq, _start, length = frag_table[fptr]
            piece = text[base:base + length]
            at = insert - skel_pos
            doc = doc[:at] + piece + doc[at:]
            base += length
            fptr += 1
        parts_text.append(doc)
        part_span.append((skel_pos, base))
    names = [f"text/part{i:04d}.xhtml" for i in range(len(parts_text))]

    # resources
    first = _abs(h, h.first_resource) if h.first_resource != NULL_INDEX else NULL_INDEX
    if first != NULL_INDEX and first < len(pdb) and h.start and \
            not (image_kind(pdb.record(first)) or pdb.record(first)[:4] in (b"FONT", b"RESC")):
        first = h.first_resource                    # combo file with absolute resource numbering
    resources: dict[int, tuple[str, bytes, str]] = {}
    if first != NULL_INDEX:
        for n, rec_no in enumerate(range(first, len(pdb)), start=1):
            data = pdb.record(rec_no)
            if data[:8] in (b"BOUNDARY", b"\xe9\x8e\r\n"):
                break
            kind = image_kind(data)
            if kind:
                ext, mime = kind
                if ext == "bmp":
                    data, ext, mime = _bmp_to_png(data)
                resources[n] = (f"images/img{n:05d}.{ext}", data, mime)
            elif data[:4] == b"FONT":
                font = decode_font(data)
                if font:
                    resources[n] = (f"fonts/font{n:05d}.{font[1]}", font[0], font[2])
    styles = {}
    for k in range(1, len(flows)):
        body = flows[k].lstrip()
        if body.startswith(b"<svg") or body.startswith(b"<?xml"):
            styles[k] = (f"flows/flow{k:04d}.svg", flows[k], "image/svg+xml")
        else:
            styles[k] = (f"styles/flow{k:04d}.css", flows[k], "text/css")

    # kindle:pos targets -> (part, id)
    linked_aids: set[bytes] = set()

    def locate(fid: int, off: int) -> tuple[int, str]:
        if fid >= len(frag_table):
            return 0, ""
        pos = frag_table[fid][0] + off
        for i, (a, b) in enumerate(part_span):
            if a <= pos < b:
                doc = parts_text[i]
                npos = min(pos - a, len(doc))
                lt, gt = doc.find(b"<", npos), doc.find(b">", npos)
                if lt == npos or (gt >= 0 and (lt < 0 or gt < lt)):
                    npos = gt + 1
                head = doc[:npos]
                for m in reversed(list(re.finditer(rb"<[^<>]+>", head))):
                    tag = m.group(0)
                    if tag.startswith(b"<body"):
                        return i, ""
                    if tag.startswith(b"<meta"):
                        continue
                    mm = _ID_ATTR.match(tag)
                    if mm:
                        return i, mm.group(1).decode("utf-8", "replace")
                    ma = _AID_ATTR.match(tag)
                    if ma:
                        linked_aids.add(ma.group(1))
                        return i, "aid-" + ma.group(1).decode("utf-8", "replace")
                return i, ""
        return 0, ""

    def res_href(prefix: str, name: str) -> str:
        return posixpath.relpath(name, posixpath.dirname(prefix))

    def inline_svg(doc_name: str, blob: bytes) -> bytes:
        """``<img src="kindle:flow:N?mime=image/svg+xml">`` -> the SVG itself, inline.

        An SVG loaded through <img> may not fetch anything (browsers block it), so
        a cover SVG wrapping a JPEG would show only its background.  Inline, its
        image links resolve against the page like any other.
        """
        def sub(m: re.Match) -> bytes:
            k = int(m.group(1), 32)
            if k not in styles or styles[k][2] != "image/svg+xml":
                return m.group(0)
            svg = styles[k][1]
            svg = re.sub(rb"<\?xml[^>]*\?>|<!DOCTYPE[^>]*>", b"", svg).strip()
            if b"xmlns=" not in svg[:300]:
                svg = svg.replace(b"<svg", b'<svg xmlns="http://www.w3.org/2000/svg"', 1)
            if b"xlink:" in svg and b"xmlns:xlink" not in svg[:600]:
                svg = svg.replace(b"<svg", b'<svg xmlns:xlink="http://www.w3.org/1999/xlink"', 1)
            return rewrite(doc_name, svg)
        return re.sub(rb"""<img\b[^>]*?src\s*=\s*['"]kindle:flow:([0-9A-Va-v]+)\?mime=image/svg\+xml['"][^>]*>""",
                      sub, blob, flags=re.I)

    def rewrite(doc_name: str, blob: bytes) -> bytes:
        def pos_sub(m: re.Match) -> bytes:
            i, ident = locate(int(m.group(1), 32), int(m.group(2), 32))
            target = posixpath.basename(names[i])
            if names[i] == doc_name:
                target = ""
            return (target + ("#" + ident if ident else "")).encode() or b"#"

        def embed_sub(m: re.Match) -> bytes:
            n = int(m.group(1), 32)
            return res_href(doc_name, resources[n][0]).encode() if n in resources else b""

        def flow_sub(m: re.Match) -> bytes:
            k = int(m.group(1), 32)
            return res_href(doc_name, styles[k][0]).encode() if k in styles else b""
        blob = _KINDLE_POS.sub(pos_sub, blob)
        blob = _KINDLE_EMBED.sub(embed_sub, blob)
        return _KINDLE_FLOW.sub(flow_sub, blob)

    out = EpubParts(title=h.title, authors=[], language="", metadata={})
    out.authors, out.language, out.metadata = _metadata(h)
    rewritten = [rewrite(names[i], inline_svg(names[i], parts_text[i])) for i in range(len(parts_text))]
    # calibre-made KF8 wraps some titles in literal quotes: <title>"Cover"</title>
    rewritten = [re.sub(rb"<title>\s*\"([^\"<]*)\"\s*</title>", rb"<title>\1</title>", d) for d in rewritten]
    for i, doc in enumerate(rewritten):
        if linked_aids:
            def add_id(m: re.Match) -> bytes:
                tag = m.group(0)
                aid = m.group(1)
                if aid in linked_aids and not re.search(rb"\sid\s*=", tag):
                    return tag.replace(b" aid=", b' id="aid-' + aid + b'" aid=', 1)
                return tag
            doc = re.sub(rb"""<[^>]+\said\s*=\s*['"]([^'"]+)['"][^>]*>""", add_id, doc)
        doc = re.sub(rb"""\s+aid\s*=\s*['"][^'"]*['"]""", b"", doc)
        out.docs.append((names[i], _ensure_xhtml(doc.decode("utf-8", "replace"), h.title, out.language)))
    out.resources = [v for v in resources.values()]
    for k, (name, data, mime) in styles.items():
        out.resources.append((name, rewrite(name, data), mime))
    cover = h.exth_int(201)
    if cover is not None and cover != NULL_INDEX and (cover + 1) in resources:
        out.cover = resources[cover + 1][0]
    out.page_direction = _page_direction(h)

    ncx = read_ncx(pdb, h)
    for p in ncx:
        if len(p.target) != 2:
            continue
        i, ident = locate(p.target[0], p.target[1])
        out.toc.append((p.level, p.label, names[i] + ("#" + ident if ident else "")))
    return out


def _ensure_xhtml(doc: str, title: str, lang: str) -> str:
    """KF8 parts are XHTML already; repair the rare one that is not well-formed."""
    from lxml import etree
    try:
        etree.fromstring(doc.encode("utf-8"))
        return doc
    except etree.XMLSyntaxError:
        m = re.search(r"<body[^>]*>(.*)</body>", doc, re.S | re.I)
        return xhtml_document(title, html_fragment_to_xhtml(m.group(1) if m else doc), lang=lang)


# ==========================================================================
# entry point
# ==========================================================================

def _kf8_start(pdb: PalmDB, h0: Header) -> int | None:
    """Record index of the KF8 header inside a MOBI/KF8 combo file, if any."""
    if h0.is_kf8:
        return 0
    boundary = h0.exth_int(121)
    candidates = []
    if boundary is not None and 0 < boundary < len(pdb):
        candidates += [boundary, boundary + 1]
    for i in range(1, len(pdb)):
        if pdb.record(i)[:8] == b"BOUNDARY":
            candidates.append(i + 1)
            break
    for c in candidates:
        if c < len(pdb):
            r = pdb.record(c)
            if r[16:20] == b"MOBI" and _u32(r, 36, 0) >= 8:
                return c
    return None


def convert(source: "bytes | str | os.PathLike[str]") -> bytes:
    """Convert a Kindle file to a complete EPUB 3 archive (bytes).

    Raises :class:`MobiError`; ``OSError`` for unreadable paths passes through.
    """
    if isinstance(source, (bytes, bytearray)):
        data = bytes(source)
    else:
        with open(source, "rb") as fh:
            data = fh.read()
    kind = detect(data)
    if kind == "kfx":
        raise MobiError("KFX Kindle books are not supported", kind="unsupported", detail="KFX container")
    if kind == "topaz":
        raise MobiError("Topaz Kindle books are not supported", kind="unsupported", detail="Topaz container")
    if kind != "mobi":
        raise MobiError("not a Kindle book", kind="corrupt", detail=repr(data[60:68]))
    try:
        pdb = PalmDB(data)
        h0 = parse_header(pdb, 0)
        if h0.encryption:
            raise MobiError("this Kindle book is DRM-protected", kind="drm", drm_scheme="mobipocket",
                            detail=f"MOBI encryption type {h0.encryption}")
        k8 = _kf8_start(pdb, h0)
        if k8 is not None:
            try:
                h8 = h0 if k8 == 0 else parse_header(pdb, k8)
                parts = convert_kf8(pdb, h8)
            except MobiError:
                if k8 == 0:
                    raise
                parts = convert_mobi6(pdb, h0)     # combo file: fall back to the MOBI 6 half
        else:
            parts = convert_mobi6(pdb, h0)
        if not parts.docs:
            raise MobiError("the book has no text")
        add_heading_toc(parts)
        return build_epub(parts)
    except MobiError:
        raise
    except (struct.error, IndexError, KeyError, ValueError, zlib.error) as exc:
        raise MobiError("the Kindle file is damaged", kind="corrupt", detail=repr(exc)) from exc
