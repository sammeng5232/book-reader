"""Deterministic binary asset builders (stdlib only).

Everything here is byte-for-byte reproducible: no timestamps, no randomness.
Used by make_fixtures.py to embed a *real* TrueType font, a real WOFF, a real
PNG and a real baseline JPEG inside the generated EPUB fixtures, so that the
renderer is exercised for real rather than against placeholder bytes.
"""

from __future__ import annotations

import struct
import zlib

# --------------------------------------------------------------------------
# PNG
# --------------------------------------------------------------------------


def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def png_checkerboard(width: int = 64, height: int = 64, cell: int = 8) -> bytes:
    """Truecolour (RGB8) PNG, magenta/teal checkerboard. Impossible to miss."""
    rows = bytearray()
    for y in range(height):
        rows.append(0)  # filter type 0 (None)
        for x in range(width):
            if ((x // cell) + (y // cell)) % 2 == 0:
                rows += b"\xE0\x28\x8C"  # magenta-ish
            else:
                rows += b"\x18\xA0\xA0"  # teal-ish
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(bytes(rows), 9)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )


# --------------------------------------------------------------------------
# JPEG (baseline, single grayscale component, DC-only blocks)
# --------------------------------------------------------------------------

# Standard JPEG luminance DC Huffman table (Annex K).
_DC_BITS = [0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0]
_DC_VALS = list(range(12))
# Tiny AC table: only EOB (0x00) and ZRL (0xF0), both 2-bit codes.
_AC_BITS = [0, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
_AC_VALS = [0x00, 0xF0]


def _huff_codes(bits, vals):
    """Canonical Huffman code assignment -> {symbol: (code, length)}."""
    codes = {}
    code = 0
    k = 0
    for length in range(1, 17):
        for _ in range(bits[length - 1]):
            codes[vals[k]] = (code, length)
            code += 1
            k += 1
        code <<= 1
    return codes


class _BitWriter:
    def __init__(self):
        self.out = bytearray()
        self.acc = 0
        self.nbits = 0

    def put(self, code: int, length: int) -> None:
        for i in range(length - 1, -1, -1):
            self.acc = (self.acc << 1) | ((code >> i) & 1)
            self.nbits += 1
            if self.nbits == 8:
                byte = self.acc & 0xFF
                self.out.append(byte)
                if byte == 0xFF:  # byte stuffing
                    self.out.append(0x00)
                self.acc = 0
                self.nbits = 0

    def flush(self) -> bytes:
        while self.nbits:
            self.put(1, 1)
        return bytes(self.out)


def _dc_category(diff: int):
    if diff == 0:
        return 0, 0, 0
    mag = abs(diff)
    cat = mag.bit_length()
    bits = diff if diff > 0 else diff + (1 << cat) - 1
    return cat, bits, cat


def _marker(tag: int, payload: bytes) -> bytes:
    return bytes((0xFF, tag)) + struct.pack(">H", len(payload) + 2) + payload


def jpeg_gradient(width: int = 64, height: int = 64) -> bytes:
    """Baseline grayscale JPEG: horizontal 8-step gradient, DC coefficients only."""
    assert width % 8 == 0 and height % 8 == 0
    bw, bh = width // 8, height // 8
    qdc = 16
    quant = bytes([qdc] * 64)

    dc_codes = _huff_codes(_DC_BITS, _DC_VALS)
    ac_codes = _huff_codes(_AC_BITS, _AC_VALS)

    bw_writer = _BitWriter()
    prev_dc = 0
    for by in range(bh):
        for bx in range(bw):
            # target luma for this block column, 40..248
            target = 40 + int(bx * (208 / max(bw - 1, 1)))
            dc = int(round((target - 128) * 8 / qdc))
            diff = dc - prev_dc
            prev_dc = dc
            cat, extra, nextra = _dc_category(diff)
            code, length = dc_codes[cat]
            bw_writer.put(code, length)
            if nextra:
                bw_writer.put(extra & ((1 << nextra) - 1), nextra)
            code, length = ac_codes[0x00]  # EOB
            bw_writer.put(code, length)
    scan = bw_writer.flush()

    def dht(cls: int, ident: int, bits, vals) -> bytes:
        return _marker(
            0xC4, bytes([(cls << 4) | ident]) + bytes(bits) + bytes(vals)
        )

    return b"".join(
        [
            b"\xFF\xD8",
            _marker(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"),
            _marker(0xDB, b"\x00" + quant),
            _marker(0xC0, struct.pack(">BHHB", 8, height, width, 1) + b"\x01\x11\x00"),
            dht(0, 0, _DC_BITS, _DC_VALS),
            dht(1, 0, _AC_BITS, _AC_VALS),
            _marker(0xDA, b"\x01\x01\x00\x00\x3F\x00"),
            scan,
            b"\xFF\xD9",
        ]
    )


# --------------------------------------------------------------------------
# TrueType font
# --------------------------------------------------------------------------

_FAMILY = "EpubReaderTest"
_UPEM = 1000
_NUM_GLYPHS = 3  # .notdef, space, filled box


def _pad4(data: bytes) -> bytes:
    return data + b"\x00" * (-len(data) % 4)


def _sfnt_checksum(data: bytes) -> int:
    data = _pad4(data)
    return sum(struct.unpack(">%dI" % (len(data) // 4), data)) & 0xFFFFFFFF


def _t_head() -> bytes:
    return struct.pack(
        ">IIIIHHqqhhhhHHhhh",
        0x00010000,  # version
        0x00010000,  # fontRevision
        0x00000000,  # checkSumAdjustment (patched later)
        0x5F0F3CF5,  # magicNumber
        0x0003,  # flags
        _UPEM,
        0,  # created
        0,  # modified
        100,
        0,
        900,
        700,  # bbox
        0,  # macStyle
        8,  # lowestRecPPEM
        2,  # fontDirectionHint
        0,  # indexToLocFormat (short)
        0,  # glyphDataFormat
    )


def _t_hhea() -> bytes:
    return struct.pack(
        ">IhhhHhhhhhhhhhhhH",
        0x00010000,
        800,  # ascender
        -200,  # descender
        0,  # lineGap
        1000,  # advanceWidthMax
        0,  # minLeftSideBearing
        0,  # minRightSideBearing
        900,  # xMaxExtent
        1,
        0,
        0,  # caret slope/offset
        0,
        0,
        0,
        0,  # reserved
        0,  # metricDataFormat
        3,  # numberOfHMetrics
    )


def _t_maxp() -> bytes:
    return struct.pack(
        ">IHHHHHHHHHHHHHH",
        0x00010000,
        _NUM_GLYPHS,
        4,  # maxPoints
        1,  # maxContours
        0,
        0,
        2,  # maxZones
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
    )


def _t_hmtx() -> bytes:
    return struct.pack(">HhHhHh", 1000, 0, 500, 0, 1000, 100)


def _t_os2() -> bytes:
    return (
        struct.pack(
            ">HhHHHhhhhhhhhhhh",
            4,  # version
            700,  # xAvgCharWidth
            400,  # usWeightClass
            5,  # usWidthClass
            0,  # fsType (installable)
            650,
            700,
            0,
            140,  # subscript
            650,
            700,
            0,
            480,  # superscript
            50,
            300,  # strikeout
            0,  # sFamilyClass
        )
        + bytes([2, 0, 5, 0, 0, 0, 0, 0, 0, 0])  # panose
        + struct.pack(">IIII", 0x00000003, 0, 0, 0)  # unicode ranges (Latin)
        + b"EPRT"  # achVendID
        + struct.pack(
            ">HHHhhhHHIIhhHHH",
            0x0040,  # fsSelection = REGULAR
            0x0020,  # usFirstCharIndex
            0x007E,  # usLastCharIndex
            800,
            -200,
            0,  # typo asc/desc/gap
            800,
            200,  # win asc/desc
            0x00000001,
            0,  # codepage ranges (Latin-1)
            500,
            700,  # sxHeight, sCapHeight
            0,
            0x0020,  # usDefaultChar, usBreakChar
            1,  # usMaxContext
        )
    )


def _t_post() -> bytes:
    return struct.pack(">IihhIIIII", 0x00030000, 0, -100, 50, 0, 0, 0, 0, 0)


def _glyph_box() -> bytes:
    pts = [(100, 0), (900, 0), (900, 700), (100, 700)]
    data = struct.pack(">hhhhh", 1, 100, 0, 900, 700)  # numContours + bbox
    data += struct.pack(">H", len(pts) - 1)  # endPtsOfContours
    data += struct.pack(">H", 0)  # instructionLength
    data += bytes([0x01] * len(pts))  # flags: all on-curve, int16 deltas
    px = 0
    for x, _ in pts:
        data += struct.pack(">h", x - px)
        px = x
    py = 0
    for _, y in pts:
        data += struct.pack(">h", y - py)
        py = y
    return _pad4(data)


def _t_glyf_loca():
    box = _glyph_box()
    glyf = box  # glyph 0 and 1 are empty (zero-length)
    offsets = [0, 0, 0, len(box)]
    loca = b"".join(struct.pack(">H", o // 2) for o in offsets)
    return glyf, loca


def _t_cmap() -> bytes:
    # Segment 1 maps U+0020 -> glyph 1 with a plain idDelta.
    # Segment 2 maps every printable ASCII char to the SAME glyph (2), which
    # idDelta cannot express (it maps a range onto *consecutive* glyph ids), so
    # it needs idRangeOffset + glyphIdArray.
    glyph_id_array = [2] * (0x7E - 0x21 + 1)
    segs = [
        # (start, end, idDelta, idRangeOffset)
        (0x0020, 0x0020, (1 - 0x20) & 0xFFFF, 0),
        (0x0021, 0x007E, 0, None),  # None -> point into glyphIdArray
        (0xFFFF, 0xFFFF, 1, 0),
    ]
    seg_count = len(segs)
    search_range = 2 * (2 ** (seg_count.bit_length() - 1))
    entry_selector = (search_range // 2).bit_length() - 1
    range_shift = 2 * seg_count - search_range

    # idRangeOffset is measured in bytes from the address of the entry itself.
    range_offsets = []
    for i, seg in enumerate(segs):
        if seg[3] is None:
            range_offsets.append(2 * (seg_count - i))  # -> glyphIdArray[0]
        else:
            range_offsets.append(seg[3])

    body = struct.pack(
        ">HHHH", seg_count * 2, search_range, entry_selector, range_shift
    )
    body += b"".join(struct.pack(">H", s[1]) for s in segs)  # endCode
    body += struct.pack(">H", 0)  # reservedPad
    body += b"".join(struct.pack(">H", s[0]) for s in segs)  # startCode
    body += b"".join(struct.pack(">H", s[2]) for s in segs)  # idDelta
    body += b"".join(struct.pack(">H", o) for o in range_offsets)
    body += b"".join(struct.pack(">H", g) for g in glyph_id_array)
    sub = struct.pack(">HHH", 4, len(body) + 6, 0) + body

    records = [(0, 3), (3, 1)]
    offset = 4 + 8 * len(records)
    header = struct.pack(">HH", 0, len(records))
    for plat, enc in records:
        header += struct.pack(">HHI", plat, enc, offset)
    return header + sub


def _t_name() -> bytes:
    strings = [
        (1, _FAMILY),
        (2, "Regular"),
        (3, _FAMILY + ";Regular;deterministic-test-font"),
        (4, _FAMILY + " Regular"),
        (5, "Version 1.000"),
        (6, _FAMILY + "-Regular"),
    ]
    records = b""
    storage = b""
    for name_id, text in strings:
        blob = text.encode("utf-16-be")
        records += struct.pack(">HHHHHH", 3, 1, 0x0409, name_id, len(blob), len(storage))
        storage += blob
    header = struct.pack(">HHH", 0, len(strings), 6 + 12 * len(strings))
    return header + records + storage


def truetype_font() -> bytes:
    """A tiny but structurally complete TTF. Every printable ASCII char maps to
    a solid box glyph, so applying the font is instantly visible."""
    glyf, loca = _t_glyf_loca()
    tables = {
        b"OS/2": _t_os2(),
        b"cmap": _t_cmap(),
        b"glyf": glyf,
        b"head": _t_head(),
        b"hhea": _t_hhea(),
        b"hmtx": _t_hmtx(),
        b"loca": loca,
        b"maxp": _t_maxp(),
        b"name": _t_name(),
        b"post": _t_post(),
    }
    tags = sorted(tables)
    num = len(tags)
    search_range = 16 * (2 ** (num.bit_length() - 1))
    entry_selector = (search_range // 16).bit_length() - 1
    range_shift = num * 16 - search_range

    offset = 12 + 16 * num
    directory = b""
    body = b""
    head_file_offset = None
    for tag in tags:
        data = tables[tag]
        if tag == b"head":
            head_file_offset = offset + len(body)
        # sfnt TableRecord is: tag, checkSum, offset, length  (in that order)
        directory += struct.pack(
            ">4sIII", tag, _sfnt_checksum(data), offset + len(body), len(data)
        )
        body += _pad4(data)

    font = bytearray(
        struct.pack(">IHHHH", 0x00010000, num, search_range, entry_selector, range_shift)
        + directory
        + body
    )
    adjustment = (0xB1B0AFBA - _sfnt_checksum(bytes(font))) & 0xFFFFFFFF
    struct.pack_into(">I", font, head_file_offset + 8, adjustment)
    return bytes(font)


def woff_font(sfnt: bytes | None = None) -> bytes:
    """Wrap an sfnt (TTF) in a WOFF 1.0 container, per W3C WOFF spec."""
    if sfnt is None:
        sfnt = truetype_font()
    (version, num_tables) = struct.unpack(">IH", sfnt[:6])
    entries = []
    for i in range(num_tables):
        tag, csum, off, length = struct.unpack_from(">4sIII", sfnt, 12 + 16 * i)
        entries.append((tag, off, length, csum))
    entries.sort(key=lambda e: e[0])

    header_size = 44 + 20 * num_tables
    directory = b""
    body = b""
    for tag, off, length, csum in entries:
        raw = sfnt[off : off + length]
        comp = zlib.compress(raw, 9)
        stored = comp if len(comp) < len(raw) else raw
        directory += struct.pack(
            ">4sIIII", tag, header_size + len(body), len(stored), length, csum
        )
        body += _pad4(stored)

    header = struct.pack(
        ">4sIIHHIHHIIIII",
        b"wOFF",
        version,
        header_size + len(body),
        num_tables,
        0,  # reserved
        len(sfnt),  # totalSfntSize
        1,
        0,  # major/minor version
        0,
        0,
        0,  # meta
        0,
        0,  # private
    )
    return header + directory + body


def unwrap_woff(woff: bytes) -> bytes:
    """Reverse woff_font(): rebuild the sfnt. Used only to self-verify."""
    assert woff[:4] == b"wOFF"
    num_tables = struct.unpack_from(">H", woff, 12)[0]
    tables = {}
    for i in range(num_tables):
        tag, off, comp_len, orig_len, _ = struct.unpack_from(">4sIIII", woff, 44 + 20 * i)
        blob = woff[off : off + comp_len]
        tables[tag] = blob if comp_len == orig_len else zlib.decompress(blob)
    tags = sorted(tables)
    num = len(tags)
    search_range = 16 * (2 ** (num.bit_length() - 1))
    entry_selector = (search_range // 16).bit_length() - 1
    range_shift = num * 16 - search_range
    offset = 12 + 16 * num
    directory = b""
    body = b""
    for tag in tags:
        data = tables[tag]
        directory += struct.pack(
            ">4sIII", tag, _sfnt_checksum(data), offset + len(body), len(data)
        )
        body += _pad4(data)
    return (
        struct.pack(">IHHHH", 0x00010000, num, search_range, entry_selector, range_shift)
        + directory
        + body
    )


# --------------------------------------------------------------------------
# SVG
# --------------------------------------------------------------------------


def svg_cover(title: str = "SVG COVER") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" width="600" height="800" '
        'viewBox="0 0 600 800">\n'
        '  <rect width="600" height="800" fill="#1d3557"/>\n'
        '  <circle cx="300" cy="300" r="180" fill="#e63946"/>\n'
        '  <text x="300" y="640" font-size="48" fill="#f1faee" '
        'text-anchor="middle" font-family="serif">' + title + "</text>\n"
        '  <text x="300" y="700" font-size="24" fill="#a8dadc" '
        'text-anchor="middle" font-family="serif">vector cover, not a bitmap</text>\n'
        "</svg>\n"
    )
