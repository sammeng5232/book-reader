# -*- coding: utf-8 -*-
"""Kindle (MOBI / AZW3) conversion: mobi.py and bookformats.py.

Synthetic books cover what no real sample here has (HUFF/CDIC compression,
DRM, MOBI/KF8 combo files, KFX/Topaz); the Project Gutenberg samples under
tests/samples (public domain) cover real-world structure.  Those tests skip
when the samples are absent.

    python -m unittest tests.test_mobi
"""

from __future__ import annotations

import heapq
import io
import os
import re
import shutil
import struct
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import bookformats  # noqa: E402
import mobi  # noqa: E402
from epublib import EpubBook, EpubError  # noqa: E402

SAMPLES = os.path.join(HERE, "samples")


# ==========================================================================
# a minimal MOBI writer, for synthetic books
# ==========================================================================

def _pdb(records: list[bytes], name: bytes = b"Test_Book") -> bytes:
    n = len(records)
    header = bytearray(78)
    header[:len(name)] = name
    header[60:68] = b"BOOKMOBI"
    struct.pack_into(">H", header, 76, n)
    table = bytearray()
    offset = 78 + 8 * n + 2
    for i, rec in enumerate(records):
        table += struct.pack(">IB3s", offset, 0, (2 * i).to_bytes(3, "big"))
        offset += len(rec)
    return bytes(header) + bytes(table) + b"\0\0" + b"".join(records)


def _record0(*, text_len: int, text_records: int, compression: int = 1, encryption: int = 0,
             title: str = "Synthetic", first_resource: int = 0xFFFFFFFF, version: int = 6,
             huff: tuple[int, int] = (0, 0), exth: dict[int, bytes] | None = None) -> bytes:
    hlen = 0xE8
    r0 = bytearray(16 + hlen)
    struct.pack_into(">HHIHHH", r0, 0, compression, 0, text_len, text_records, 4096, encryption)
    r0[16:20] = b"MOBI"
    struct.pack_into(">IIIII", r0, 20, hlen, 2, 65001, 1234, version)
    for off in range(0x28, 0x50, 4):
        struct.pack_into(">I", r0, off, 0xFFFFFFFF)
    struct.pack_into(">I", r0, 0x6C, first_resource)
    struct.pack_into(">II", r0, 0x70, *huff)
    struct.pack_into(">I", r0, 0xF4, 0xFFFFFFFF)          # no NCX
    struct.pack_into(">H", r0, 0xF2, 0)                   # no trailing entries
    blob = bytearray()
    if exth:
        struct.pack_into(">I", r0, 0x80, 0x40)
        items = b"".join(struct.pack(">II", t, 8 + len(v)) + v for t, v in exth.items())
        blob += b"EXTH" + struct.pack(">II", 12 + len(items), len(exth)) + items
    name = title.encode("utf-8")
    struct.pack_into(">II", r0, 0x54, len(r0) + len(blob), len(name))
    return bytes(r0) + bytes(blob) + name + b"\0\0"


def _split(text: bytes) -> list[bytes]:
    return [text[i:i + 4096] for i in range(0, len(text), 4096)] or [b""]


def make_mobi(html: str, **kw) -> bytes:
    text = html.encode("utf-8")
    recs = _split(text)
    return _pdb([_record0(text_len=len(text), text_records=len(recs), **kw)] + recs)


# ---- a HUFF/CDIC encoder following the format's canonical-code rules --------------------

def _code_lengths(freq: dict[int, int]) -> dict[int, int]:
    heap = [(f, i, (sym,)) for i, (sym, f) in enumerate(sorted(freq.items()))]
    heapq.heapify(heap)
    lengths = {sym: 0 for sym in freq}
    if len(heap) == 1:
        return {next(iter(freq)): 1}
    k = len(heap)
    while len(heap) > 1:
        f1, _, a = heapq.heappop(heap)
        f2, _, b = heapq.heappop(heap)
        for s in a + b:
            lengths[s] += 1
        heapq.heappush(heap, (f1 + f2, k, a + b))
        k += 1
    return lengths


def huff_cdic_encode(records: list[bytes]) -> tuple[list[bytes], bytes, bytes]:
    """-> (compressed text records, HUFF record, CDIC record); one literal phrase per byte."""
    freq: dict[int, int] = {}
    for rec in records:
        for b in rec:
            freq[b] = freq.get(b, 0) + 1
    lengths = _code_lengths(freq)
    assert max(lengths.values()) < 32
    # dictionary order: by length, then symbol; codes within a length descend from the top
    order = sorted(freq, key=lambda s: (lengths[s], s))
    by_len: dict[int, list[int]] = {}
    for s in order:
        by_len.setdefault(lengths[s], []).append(s)
    code: dict[int, tuple[int, int]] = {}
    min_code, max_code, base_index = {}, {}, {}
    below = 1                      # left-aligned boundary, expressed at length 0
    prev_len, index = 0, 0
    for L in sorted(by_len):
        top = below * (1 << (L - prev_len))       # first value above this length's codes
        n = len(by_len[L])
        max_code[L], min_code[L] = top - 1, top - n
        base_index[L] = index
        for k, s in enumerate(by_len[L]):
            code[s] = (max_code[L] - k, L)
        index += n
        below, prev_len = min_code[L], L
    assert below == 0, "code lengths must form a complete prefix code"
    # dict2: (mincode, maxcode) per length; maxcode offset so (maxcode - code) is the global index
    dict2 = []
    for L in range(1, 33):
        if L in by_len:
            dict2 += [min_code[L], max_code[L] + base_index[L]]
        else:
            dict2 += [(1 << L) - 1 if L == 32 else (1 << L), 0]     # never >= : skipped
    dict1 = []
    for t in range(256):
        hit = None
        for L in sorted(by_len):
            if L <= 8 and min_code[L] <= (t >> (8 - L)) <= max_code[L]:
                hit = L
                break
        if hit:
            dict1.append(hit | 0x80 | ((max_code[hit] + base_index[hit]) << 8))
        else:
            dict1.append(9)
    # 24-byte header: magic, header length, offset of dict1, offset of dict2, 8 reserved bytes
    huff = b"HUFF" + struct.pack(">III", 24, 24, 24 + 1024) + b"\0" * 8 + struct.pack(">256I", *dict1) + \
        struct.pack(">64I", *dict2)
    offsets, bodies = [], bytearray()
    table_len = 2 * len(order)
    for s in order:
        offsets.append(table_len + len(bodies))
        bodies += struct.pack(">H", 1 | 0x8000) + bytes([s])
    cdic = b"CDIC" + struct.pack(">III", 16, len(order), 16) + struct.pack(f">{len(order)}H", *offsets) + bytes(bodies)
    out = []
    for rec in records:
        bits = "".join(format(code[b][0], f"0{code[b][1]}b") for b in rec)
        bits += "0" * (-len(bits) % 8)
        out.append(int(bits, 2).to_bytes(len(bits) // 8, "big") if bits else b"")
    return out, huff, cdic


# ==========================================================================
# tests
# ==========================================================================

class DecompressionTests(unittest.TestCase):
    def test_palmdoc_every_opcode(self) -> None:
        # literal 'A', 3-byte literal run, space+char (0xC1 -> ' A'), back reference dist 4 len 3
        data = b"A" + b"\x03xyz" + b"\xc1" + bytes([0x80 | (4 >> 5), ((4 << 3) & 0xFF) | 0])
        self.assertEqual(mobi.palmdoc_decompress(data), b"Axyz A" + b"yz ")   # dist 4 from the end

    def test_palmdoc_overlapping_copy_repeats(self) -> None:
        # 'ab' then copy dist 2 len 6 -> 'ababab' appended
        pair = (2 << 3) | (6 - 3)
        data = b"ab" + bytes([0x80 | (pair >> 8), pair & 0xFF])
        self.assertEqual(mobi.palmdoc_decompress(data), b"ab" + b"ababab")

    def test_palmdoc_bad_reference_is_skipped(self) -> None:
        self.assertEqual(mobi.palmdoc_decompress(b"\x80\xff" + b"ok"), b"ok")

    def test_trailing_entries(self) -> None:
        # text + multibyte byte (flag bit 0: 1 extra byte) + TBS entry of size 3 (flag bit 1)
        rec = b"hello" + b"\x00" + b"\x01\x02\x83"
        self.assertEqual(mobi._trailing_size(rec, 0b11), 4)
        self.assertEqual(mobi._trailing_size(b"hello\x02", 0b01), 3)   # low 2 bits = extra bytes, +1
        self.assertEqual(mobi._trailing_size(b"hello\x00", 0b01), 1)
        self.assertEqual(mobi._trailing_size(b"hello", 0), 0)

    def test_huff_cdic_round_trip(self) -> None:
        text = ("<html><body><p>" + "HUFF/CDIC round trip — 红楼梦 " * 40 + "</p></body></html>").encode("utf-8")
        recs = _split(text)
        packed, huff, cdic = huff_cdic_encode(recs)
        hc = mobi.HuffCdic(huff, [cdic])
        self.assertEqual(b"".join(hc.decompress(r) for r in packed), text)


class SyntheticBookTests(unittest.TestCase):
    def test_uncompressed_book_converts(self) -> None:
        html = ('<html><head><guide></guide></head><body><h1>One</h1><p>First <a filepos=0000000999>jump</a>'
                "</p><mbp:pagebreak/><h1>Two</h1><p>Second 章节</p></body></html>")
        data = mobi.convert(make_mobi(html, title="合成书"))
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            self.assertEqual(z.infolist()[0].filename, "mimetype")
            self.assertEqual(z.infolist()[0].compress_type, zipfile.ZIP_STORED)
        book = _open_bytes(data)
        self.assertEqual(book.metadata["title"], "合成书")
        self.assertEqual(len(book.spine), 2)
        self.assertIn("Second 章节", book.plain_text(book.spine[1].zip_name))
        self.assertEqual(book.warnings, [])

    def test_huff_cdic_book_converts(self) -> None:
        html = "<html><body>" + "".join(f"<p>段落 {i}：HUFF 压缩的正文。</p>" for i in range(400)) + "</body></html>"
        text = html.encode("utf-8")
        packed, huff, cdic = huff_cdic_encode(_split(text))
        r0 = _record0(text_len=len(text), text_records=len(packed), compression=17480,
                      huff=(1 + len(packed), 2), title="HUFF")
        book = _open_bytes(mobi.convert(_pdb([r0] + packed + [huff, cdic])))
        self.assertIn("段落 399：HUFF 压缩的正文。", book.plain_text(book.spine[0].zip_name))

    def test_drm_is_refused_not_decrypted(self) -> None:
        with self.assertRaises(mobi.MobiError) as cm:
            mobi.convert(make_mobi("<html><body><p>secret</p></body></html>", encryption=2))
        self.assertEqual(cm.exception.kind, "drm")
        self.assertEqual(cm.exception.drm_scheme, "mobipocket")

    def test_drm_through_bookformats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "locked.azw")
            open(p, "wb").write(make_mobi("<html><body><p>x</p></body></html>", encryption=1))
            with self.assertRaises(EpubError) as cm:
                bookformats.open_book(p, cache_root=tmp)
            self.assertEqual((cm.exception.kind, cm.exception.drm_scheme), ("drm", "mobipocket"))

    def test_kfx_and_topaz_are_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for name, head in (("book.kfx", b"CONT\x02\x00" + b"\0" * 200), ("book.azw1", b"TPZ0" + b"\0" * 200)):
                p = os.path.join(tmp, name)
                open(p, "wb").write(head)
                with self.assertRaises(EpubError) as cm:
                    bookformats.open_book(p, cache_root=tmp)
                self.assertEqual(cm.exception.kind, "unsupported", name)

    def test_truncated_file_is_corrupt(self) -> None:
        data = make_mobi("<html><body><p>" + "x" * 9000 + "</p></body></html>")
        with self.assertRaises(mobi.MobiError) as cm:
            mobi.convert(data[:300])
        self.assertEqual(cm.exception.kind, "corrupt")

    def test_heading_toc_for_books_without_one(self) -> None:
        body = "".join(f"<p>第{n}回 标题{n}</p><p>正文。第{n}回中提到的事。</p>" for n in "一二三四")
        book = _open_bytes(mobi.convert(make_mobi(f"<html><body>{body}</body></html>")))
        titles = [e.title for e in book.toc]
        self.assertEqual(titles, ["第一回 标题一", "第二回 标题二", "第三回 标题三", "第四回 标题四"])
        text = book.plain_text(book.spine[0].zip_name)
        self.assertEqual(text.count("第一回中提到的事"), 1)     # running text is not a heading


class SampleTests(unittest.TestCase):
    """Project Gutenberg samples (public domain) in tests/samples."""

    CASES = {
        "gb11.mobi": ("Alice's Adventures in Wonderland", "mobi", 16),
        "gb11_kf8.azw3": ("Alice's Adventures in Wonderland", "azw3", 16),
        "gb24264.mobi": ("紅樓夢", "mobi", 100),
        "gb24264_kf8.azw3": ("紅樓夢", "azw3", 120),
    }

    def setUp(self) -> None:
        if not all(os.path.isfile(os.path.join(SAMPLES, n)) for n in self.CASES):
            self.skipTest("Kindle samples not present")
        self.cache = tempfile.mkdtemp(prefix="mobi-cache-")

    def tearDown(self) -> None:
        shutil.rmtree(self.cache, ignore_errors=True)

    def test_samples_convert_cleanly(self) -> None:
        for name, (title, fmt, min_toc) in self.CASES.items():
            with self.subTest(name):
                book = bookformats.open_book(os.path.join(SAMPLES, name), cache_root=self.cache)
                self.assertEqual(book.metadata["title"], title)
                self.assertEqual(book.source_format, fmt)
                self.assertEqual(os.path.normcase(book.path), os.path.normcase(os.path.join(SAMPLES, name)))
                self.assertEqual(book.warnings, [])
                self.assertTrue(book.cover and book.cover_bytes())
                flat = []

                def walk(es):
                    for e in es:
                        flat.append(e)
                        walk(e.children)
                walk(book.toc)
                self.assertGreaterEqual(len(flat), min_toc)
                for e in flat:                     # every entry lands on a real anchor
                    self.assertTrue(book.has(e.zip_name), e.title)
                    if e.fragment:
                        self.assertIn(f'id="{e.fragment}"', book.read_text(e.zip_name))
                book.close()

    def test_chinese_text_is_not_mojibake(self) -> None:
        book = bookformats.open_book(os.path.join(SAMPLES, "gb24264_kf8.azw3"), cache_root=self.cache)
        text = "".join(book.plain_text(s.zip_name) for s in book.spine)
        cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
        self.assertGreater(cjk, 700_000)
        self.assertNotIn("�", text)
        self.assertGreater(len(book.search("寶玉")), 1000)
        book.close()

    def test_conversion_is_cached(self) -> None:
        p = os.path.join(SAMPLES, "gb11.mobi")
        bookformats.open_book(p, cache_root=self.cache).close()
        cached = os.listdir(bookformats.converted_dir(self.cache))
        self.assertEqual(len(cached), 1)
        target = os.path.join(bookformats.converted_dir(self.cache), cached[0])
        before = os.stat(target).st_mtime_ns
        bookformats.open_book(p, cache_root=self.cache).close()
        self.assertEqual(os.stat(target).st_mtime_ns, before)       # reused, not rewritten

    def test_combo_file_prefers_kf8(self) -> None:
        """A MOBI 6 + KF8 combo (as kindlegen writes) is read through its KF8 half."""
        m6 = mobi.PalmDB(open(os.path.join(SAMPLES, "gb11.mobi"), "rb").read())
        k8 = mobi.PalmDB(open(os.path.join(SAMPLES, "gb11_kf8.azw3"), "rb").read())
        recs = [m6.record(i) for i in range(len(m6))] + [b"BOUNDARY"] + [k8.record(i) for i in range(len(k8))]
        book = _open_bytes(mobi.convert(_pdb(recs, name=b"Alice_combo")))
        self.assertEqual(len(book.spine), len(_open_bytes(mobi.convert(k8.data)).spine))
        self.assertIn("Rabbit", "".join(book.plain_text(s.zip_name) for s in book.spine))

    def test_user_file_is_never_modified(self) -> None:
        p = os.path.join(SAMPLES, "gb24264.mobi")
        before = (os.stat(p).st_size, os.stat(p).st_mtime_ns, open(p, "rb").read()[:4096])
        bookformats.open_book(p, cache_root=self.cache).close()
        self.assertEqual(before, (os.stat(p).st_size, os.stat(p).st_mtime_ns, open(p, "rb").read()[:4096]))


_open_dirs: list[str] = []


def _open_bytes(data: bytes) -> EpubBook:
    d = tempfile.mkdtemp(prefix="mobi-t-")
    _open_dirs.append(d)
    p = os.path.join(d, "b.epub")
    open(p, "wb").write(data)
    return EpubBook.open(p)


def tearDownModule() -> None:
    for d in _open_dirs:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
