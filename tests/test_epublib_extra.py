#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Extra proofs for ``epublib`` (owner A): CONTRACT.md section 2 items that
``test_epub_parsing.py`` does not pin down.

    1. the user's three real books open, with real CJK metadata and TOCs, and
       the hexadecimal-playOrder book keeps DOCUMENT order;
    2. obfuscated fonts (Adobe on a real book, IDPF on a fixture) come back as
       valid TrueType that FreeType can load;
    3. performance of open() + total_units() + a 2-character Chinese search;
    4. search(): literal metacharacters, empty queries, exact context, and the
       reader.js-identical fold;
    5. the section 2.1 invariant, Python side: the five rules, the HTML5
       parsing behaviour table MEASURED in QtWebEngine 6.11.1 (Chromium 140),
       the code-point unit, and the ``--plain-text`` CLI.

stdlib ``unittest`` only. Real-book tests skip cleanly when the files are
absent. The books are opened read-only and never modified, moved or copied.

The live Chromium cross-check (fixtures + real books against the real
``assets/reader.js``) is opt-in because it starts QtWebEngine offscreen::

    set EPUB_READER_CHROMIUM=1
    C:\\Users\\mengz\\AppData\\Local\\Programs\\Python\\Python314\\python.exe ^
        -m unittest tests.test_epublib_extra -v

``python tests\\test_epublib_extra.py --report`` prints the real-book summary.
"""

from __future__ import annotations

import glob
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
for _p in (str(PROJECT_ROOT), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import make_fixtures  # noqa: E402
import epublib  # noqa: E402
from epublib import EpubBook, EpubError, flatten_html, fold_for_search  # noqa: E402

FIXTURES = make_fixtures.ensure_fixtures()

# ---------------------------------------------------------------------------
# the user's real books (read-only test material)
# ---------------------------------------------------------------------------

DESKTOP = Path(os.path.expanduser("~")) / "Desktop" / "文件"
BOOK_50REN = DESKTOP / "Econ Books" / "50人的二十年_樊纲 易纲等.epub"
BOOK_CIAN = (DESKTOP / "Econ Books" / "Econ Books_China"
             / "从此岸到彼岸_人民币汇率如何实现清洁浮动_缪延亮.epub")
_lxk = sorted(glob.glob(str(DESKTOP / "CUHK Notes" / "CUHK_BFRM" / "Fin Books"
                            / "*(李向科) (Z-Library).epub")))
BOOK_LXK = Path(_lxk[0]) if _lxk else DESKTOP / "missing-李向科.epub"

#: What each real book must look like. Measured on this machine.
REAL_BOOKS = {
    "50ren": dict(
        path=BOOK_50REN, title="50人的二十年", authors=["樊纲 易纲 吴晓灵 许善达 蔡昉 主编"],
        spine=80, toc_top=5, toc_total=43, toc_depth=2, cover="cover.jpeg",
        total_units=226409, crlf=False, word="经济",
        first5=["版权信息", "序言 改革开放四十年： 理论探索与改革实践携手前进",
                "第一篇 我与中国经济50人论坛", "中国经济50人论坛与高质量发展", "一晃二十年"],
    ),
    "cian": dict(
        path=BOOK_CIAN, title="从此岸到彼岸", authors=["缪延亮"],
        spine=24, toc_top=24, toc_total=86, toc_depth=2, cover="ops/images/cover.jpg",
        total_units=145506, crlf=True, word="汇率",
        first5=["封面", "扉页", "版权信息", "中国金融四十人论坛书系", "序一"],
    ),
    "lxk": dict(
        path=BOOK_LXK, title="金融计量方法系列教材•数理金融学:金融衍生品定价、对冲和套利分析",
        authors=["李向科"], spine=30, toc_top=29, toc_total=114, toc_depth=2,
        cover="OEBPS/Image01200.jpg", total_units=161964, crlf=True, word="期权",
        first5=["目录", "作者简介", "扉页", "版权页", "前言"],
    ),
}

_CJK = re.compile("[\u3400-\u4dbf\u4e00-\u9fff]")
#: What mojibake of UTF-8 Chinese looks like after a latin-1/cp1252/cp437 guess.
_MOJIBAKE = re.compile("[\u00c0-\u00ff][\u0080-\u00bf\u2000-\u206f]|[\u2500-\u257f]|\ufffd")


def _flat(entries):
    for e in entries:
        yield e
        yield from _flat(e.children)


def _depth(entries):
    return 0 if not entries else 1 + max(_depth(e.children) for e in entries)


def _need(path: Path) -> None:
    if not path.exists():
        raise unittest.SkipTest("real book not present: %s" % path)


def _stat(path: Path):
    st = path.stat()
    return st.st_size, st.st_mtime_ns


# ===========================================================================
# 1. the three real books
# ===========================================================================


class TestRealBooks(unittest.TestCase):
    def _check(self, key):
        spec = REAL_BOOKS[key]
        _need(spec["path"])
        before = _stat(spec["path"])
        with EpubBook.open(spec["path"]) as book:
            self.assertEqual(book.metadata["title"], spec["title"])
            self.assertEqual(book.metadata["authors"], spec["authors"])
            self.assertEqual(len(book.spine), spec["spine"])
            self.assertEqual(len(book.toc), spec["toc_top"])
            entries = list(_flat(book.toc))
            self.assertEqual(len(entries), spec["toc_total"])
            self.assertEqual(_depth(book.toc), spec["toc_depth"])
            self.assertFalse(book.toc_is_synthetic)
            self.assertEqual([e.title for e in entries[:5]], spec["first5"])
            for entry in entries:
                with self.subTest(toc=entry.title):
                    self.assertTrue(entry.title.strip(), "empty TOC title")
                    self.assertRegex(entry.title, _CJK, "TOC title has no CJK")
                    self.assertNotRegex(entry.title, _MOJIBAKE, "TOC title is mojibake")
                    self.assertTrue(entry.zip_name and book.has(entry.zip_name))
            self.assertRegex(book.metadata["title"], _CJK)
            self.assertEqual(book.cover, spec["cover"])
            self.assertTrue(book.cover_bytes().startswith(b"\xff\xd8\xff"))
            self.assertEqual(book.total_units(), spec["total_units"])
            self.assertEqual(book.warnings, [])
        self.assertEqual(_stat(spec["path"]), before, "the real book was modified")

    def test_50ren(self):
        self._check("50ren")

    def test_cong_ci_an(self):
        self._check("cian")

    def test_li_xiang_ke(self):
        self._check("lxk")

    def test_hex_play_order_book_keeps_document_order(self):
        _need(BOOK_LXK)
        with zipfile.ZipFile(BOOK_LXK) as zf:
            ncx_name = next(n for n in zf.namelist() if n.lower().endswith(".ncx"))
            root = ET.fromstring(zf.read(ncx_name))
        ns = "{http://www.daisy.org/z3986/2005/ncx/}"
        points = list(root.iter(ns + "navPoint"))      # iter() is document order
        orders = [p.get("playOrder") for p in points]
        hexy = [o for o in orders if not o.isdigit()]
        self.assertTrue(hexy, "fixture assumption: this book uses hex playOrder")
        self.assertIn("1D", hexy)
        expected = [re.sub(r"[ \t\r\n]+", " ", "".join(
            p.find(ns + "navLabel").find(ns + "text").itertext())).strip()
            for p in points]
        with EpubBook.open(BOOK_LXK) as book:
            got = [e.title for e in _flat(book.toc)]
            self.assertEqual(got, expected)
            # and document order is reading order: spine positions never go back
            positions = [book.spine_index(e.zip_name) for e in _flat(book.toc)]
            self.assertEqual(positions, sorted(positions))
            # sorting by playOrder as hex would NOT be document order here
            by_hex = [t for _o, t in sorted(zip((int(o, 16) for o in orders), expected))]
            self.assertNotEqual(by_hex, got)

    def test_crlf_books_have_no_cr_in_plain_text(self):
        """Chromium's HTML parser turns CR LF into LF; so must plain_text()."""
        for key in ("cian", "lxk"):
            spec = REAL_BOOKS[key]
            _need(spec["path"])
            with EpubBook.open(spec["path"]) as book:
                crlf_docs = 0
                for item in book.spine:
                    raw = book.read_text(item.zip_name)
                    crlf_docs += "\r" in raw
                    with self.subTest(book=key, doc=item.zip_name):
                        self.assertNotIn("\r", book.plain_text(item.zip_name))
                self.assertGreater(crlf_docs, 20, "fixture assumption: CRLF sources")


# ===========================================================================
# 2. obfuscated fonts
# ===========================================================================

_FONT_SIGNATURES = (b"\x00\x01\x00\x00", b"OTTO", b"true", b"typ1", b"wOFF", b"wOF2", b"ttcf")


def _encryption_entries(path):
    with zipfile.ZipFile(path) as zf:
        root = ET.fromstring(zf.read("META-INF/encryption.xml"))
    out = []
    for data in root.iter("{http://www.w3.org/2001/04/xmlenc#}EncryptedData"):
        algo = data.find("{http://www.w3.org/2001/04/xmlenc#}EncryptionMethod").get("Algorithm")
        uri = data.find(".//{http://www.w3.org/2001/04/xmlenc#}CipherReference").get("URI")
        out.append((uri, algo))
    return out


class TestObfuscatedFonts(unittest.TestCase):
    def _assert_fonts(self, path, algorithm, head_len):
        entries = _encryption_entries(path)
        self.assertTrue(entries)
        with EpubBook.open(path) as book, zipfile.ZipFile(path) as zf:
            for uri, algo in entries:
                self.assertEqual(algo, algorithm)
                name = uri.lstrip("/")          # CipherReference is container-root relative
                self.assertTrue(book.has(name), name)
                with self.subTest(font=name):
                    raw = zf.read(name)
                    clear = book.read(name)
                    self.assertNotIn(raw[:4], _FONT_SIGNATURES, "stored font is not obfuscated?")
                    self.assertIn(clear[:4], _FONT_SIGNATURES)
                    self.assertEqual(len(clear), len(raw))
                    self.assertEqual(clear[head_len:], raw[head_len:])
                    self.assertNotEqual(clear[:head_len], raw[:head_len])
                    num_tables = int.from_bytes(clear[4:6], "big")
                    tags = [clear[12 + 16 * i:16 + 16 * i] for i in range(num_tables)]
                    self.assertIn(b"glyf", tags)
                    self.assertIn(b"cmap", tags)
                    try:
                        from PIL import ImageFont
                    except ImportError:  # pragma: no cover
                        continue
                    ImageFont.truetype(io.BytesIO(clear), 24)          # FreeType accepts it
                    with self.assertRaises(OSError):
                        ImageFont.truetype(io.BytesIO(raw), 24)       # and rejects the stored bytes

    def test_adobe_obfuscation_on_50ren(self):
        _need(BOOK_50REN)
        self._assert_fonts(BOOK_50REN, "http://ns.adobe.com/pdf/enc#RC", 1024)

    def test_idpf_obfuscation_fixture(self):
        self._assert_fonts(FIXTURES / "obfuscated_fonts.epub",
                           "http://www.idpf.org/2008/embedding", 1040)


# ===========================================================================
# 3. performance
# ===========================================================================


class TestPerformance(unittest.TestCase):
    BUDGET_MS = 3000.0          # measured ~80-170 ms; generous for a busy machine

    def _time(self, key):
        spec = REAL_BOOKS[key]
        _need(spec["path"])
        t0 = time.perf_counter()
        book = EpubBook.open(spec["path"])
        try:
            units = book.total_units()
            hits = book.search(spec["word"], limit=10 ** 9)
        finally:
            book.close()
        ms = (time.perf_counter() - t0) * 1000.0
        self.assertGreater(units, 100000)
        self.assertTrue(hits)
        self.assertTrue(all(h.match == spec["word"] for h in hits))
        if os.environ.get("EPUB_READER_REPORT"):
            print("\n  %s: open+total_units+search(%s) = %.1f ms, %d hits"
                  % (key, spec["word"], ms, len(hits)), file=sys.stderr)
        self.assertLess(ms, self.BUDGET_MS)

    def test_largest_file_cong_ci_an(self):     # 13.1 MB
        self._time("cian")

    def test_most_text_50ren(self):             # 259,494 code points of text
        self._time("50ren")

    def test_li_xiang_ke(self):
        self._time("lxk")


# ===========================================================================
# 4. search()
# ===========================================================================

_SEARCH_BODY = (
    "<p>a.b axb a+b aab (x) [y] \\d $5 ^up *star* q? a|b {2} a\\b</p>\n"
    "<p>hello   world\n\tagain world</p>"
    "<p>Don\u2019t \u201cquote\u201d \u2014 dash DON'T \u0130stanbul Straße</p>"
    "<p>aaaa \U00020BB7\u91ce\u5bb6 \u6c47\u7387\u3000\u6c47\u7387</p>"
)


def _make_book(directory: str, chapters: dict[str, str], title="Search Test") -> str:
    """A minimal valid EPUB 3 in ``directory``. Returns its path."""
    path = os.path.join(directory, "book.epub")
    manifest = "".join('<item id="c%d" href="%s" media-type="application/xhtml+xml"/>' % (i, n)
                       for i, n in enumerate(chapters))
    spine = "".join('<itemref idref="c%d"/>' % i for i in range(len(chapters)))
    nav = ("<?xml version='1.0' encoding='utf-8'?><html xmlns='http://www.w3.org/1999/xhtml' "
           "xmlns:epub='http://www.idpf.org/2007/ops'><head><title>nav</title></head><body>"
           "<nav epub:type='toc'><ol>%s</ol></nav></body></html>"
           % "".join("<li><a href='%s'>Chapter %d</a></li>" % (n, i + 1) for i, n in enumerate(chapters)))
    opf = ("<?xml version='1.0' encoding='utf-8'?><package xmlns='http://www.idpf.org/2007/opf' "
           "version='3.0' unique-identifier='id'><metadata xmlns:dc='http://purl.org/dc/elements/1.1/'>"
           "<dc:identifier id='id'>urn:uuid:12345678-1234-4234-8234-123456789abc</dc:identifier>"
           "<dc:title>%s</dc:title><dc:language>en</dc:language></metadata><manifest>"
           "<item id='nav' href='nav.xhtml' media-type='application/xhtml+xml' properties='nav'/>%s"
           "</manifest><spine>%s</spine></package>" % (title, manifest, spine))
    container = ("<?xml version='1.0'?><container version='1.0' "
                 "xmlns='urn:oasis:names:tc:opendocument:xmlns:container'><rootfiles>"
                 "<rootfile full-path='content.opf' media-type='application/oebps-package+xml'/>"
                 "</rootfiles></container>")
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip")
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("content.opf", opf)
        zf.writestr("nav.xhtml", nav)
        for name, body in chapters.items():
            zf.writestr(name, "<?xml version='1.0' encoding='utf-8'?><html "
                        "xmlns='http://www.w3.org/1999/xhtml'><head><title>%s</title></head>"
                        "<body>%s</body></html>" % (name, body))
    return path


class TestSearch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.path = _make_book(cls._tmp.name, {"one.xhtml": _SEARCH_BODY,
                                              "two.xhtml": "<p>a.b again, and (x) twice</p>"})
        cls.book = EpubBook.open(cls.path)

    @classmethod
    def tearDownClass(cls):
        cls.book.close()
        cls._tmp.cleanup()

    def _texts(self, query):
        return [(h.zip_name, h.match) for h in self.book.search(query)]

    def assertContextExact(self, hits, query):
        for h in hits:
            text = self.book.plain_text(h.zip_name)
            self.assertEqual(text[h.gpos:h.gpos + h.length], h.match)
            self.assertEqual(fold_for_search(h.match), fold_for_search(query.strip()))
            raw_before = text[max(0, h.gpos - 40):h.gpos]
            raw_after = text[h.gpos + h.length:h.gpos + h.length + 40]
            self.assertEqual(h.before, re.sub(r"[ \t\r\n\f\v]+", " ", raw_before).lstrip(" "))
            self.assertEqual(h.after, re.sub(r"[ \t\r\n\f\v]+", " ", raw_after).rstrip(" "))
            self.assertEqual(self.book.spine[h.spine_index].zip_name, h.zip_name)

    def test_regex_metacharacters_are_literal(self):
        for query, count in ((".", 2), ("a.b", 2), ("a+b", 1), ("(x)", 2), ("[y]", 1),
                             ("\\d", 1), ("$5", 1), ("^up", 1), ("*star*", 1), ("q?", 1),
                             ("a|b", 1), ("{2}", 1), ("a\\b", 1)):
            with self.subTest(query=query):
                hits = self.book.search(query)
                self.assertEqual(len(hits), count, [h.match for h in hits])
                self.assertContextExact(hits, query)
        # "a.b" must not behave like the regex a.b (which would match "axb"/"aab")
        self.assertNotIn(("one.xhtml", "axb"), self._texts("a.b"))

    def test_empty_and_whitespace_queries_return_nothing(self):
        for query in ("", "   ", "\t\n", "\u3000", None):
            with self.subTest(query=query):
                self.assertEqual(self.book.search(query), [])
        self.assertEqual(self.book.search("a.b", limit=0), [])

    def test_context_is_exact_and_keeps_the_space_next_to_the_match(self):
        hits = self.book.search("world")
        self.assertEqual(len(hits), 2)
        self.assertContextExact(hits, "world")
        self.assertTrue(hits[0].before.endswith("hello "), hits[0].before)
        self.assertTrue(hits[1].before.endswith("again "), hits[1].before)
        self.assertNotIn("\n", hits[1].before)
        self.assertEqual(hits[0].chapter_title, "Chapter 1")

    def test_context_is_at_most_forty_code_points(self):
        for h in self.book.search("\u6c47\u7387") + self.book.search("a"):
            self.assertLessEqual(len(h.before), 40)
            self.assertLessEqual(len(h.after), 40)

    def test_fold_matches_reader_js(self):
        self.assertEqual(len(self.book.search("don't")), 2)          # ASCII and U+2019
        self.assertEqual(len(self.book.search('"quote"')), 1)        # U+201C/U+201D
        self.assertEqual(len(self.book.search("- dash")), 1)         # U+2014
        self.assertEqual(len(self.book.search("\u0130stanbul")), 1)  # length-preserving
        self.assertEqual(len(self.book.search("i\u0307stanbul")), 0)
        self.assertEqual(len(self.book.search("STRASSE")), 0)        # no length-changing folds
        self.assertEqual(fold_for_search("\u03a3\u03a3 \u039f\u03a3"), "\u03c3\u03c3 \u03bf\u03c3")
        for s in ("\u0130\u03a3x\u2019\U00020BB7", _SEARCH_BODY):
            self.assertEqual(len(fold_for_search(s)), len(s))

    def test_matches_do_not_overlap_and_limit_is_respected(self):
        self.assertEqual([h.gpos for h in self.book.search("aa")][:2],
                         [self.book.plain_text("one.xhtml").find("aab"),
                          self.book.plain_text("one.xhtml").find("aaaa")])
        text = self.book.plain_text("one.xhtml")
        aaaa = text.find("aaaa")
        self.assertEqual([h.gpos for h in self.book.search("aa") if h.gpos >= aaaa][:2],
                         [aaaa, aaaa + 2])
        self.assertEqual(len(self.book.search("a", limit=3)), 3)

    def test_offsets_are_code_points(self):
        hits = self.book.search("\U00020BB7\u91ce")
        self.assertEqual(len(hits), 1)
        h = hits[0]
        self.assertEqual(h.length, 2)                    # one astral + one BMP code point
        text = self.book.plain_text("one.xhtml")
        self.assertEqual(text[h.gpos], "\U00020BB7")
        self.assertEqual(h.gpos, text.index("\U00020BB7"))

    def test_hits_are_in_spine_order_across_documents(self):
        hits = self.book.search("a.b")
        self.assertEqual([h.zip_name for h in hits], ["one.xhtml", "two.xhtml"])
        self.assertEqual([h.spine_index for h in hits], [0, 1])

    def test_chinese_search_on_a_real_book(self):
        _need(BOOK_CIAN)
        with EpubBook.open(BOOK_CIAN) as book:
            hits = book.search("\u6c47\u7387", limit=10 ** 9)
            self.assertGreater(len(hits), 1000)
            total = sum(book.plain_text(s.zip_name).count("\u6c47\u7387") for s in book.spine)
            self.assertEqual(len(hits), total)
            for h in hits[:200] + hits[-200:]:
                text = book.plain_text(h.zip_name)
                self.assertEqual(text[h.gpos:h.gpos + 2], "\u6c47\u7387")
                self.assertEqual(h.before, re.sub(r"[ \t\r\n\f\v]+", " ",
                                                  text[max(0, h.gpos - 40):h.gpos]).lstrip(" "))
                self.assertTrue(h.chapter_title)


# ===========================================================================
# 5. the section 2.1 invariant, Python side
# ===========================================================================

#: (name, served HTML, text MEASURED from assets/reader.js flatText() in
#: QtWebEngine 6.11.1 / Chromium 140, served as text/html;charset=utf-8).
CHROMIUM_MEASURED = [
    ('search_fold',
     '<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml">\n<head><title>T</title></head>\n<body>\n<p>Don\u2019t STOP \u2014 \u0130stanbul \u039f\u0394\u039f\u03a3 \u201cquoted\u201d \U00020bb7\u91ce \U0002a6a5 the The THE \U00020bb7\u91ce</p><svg><style>svgcss</style><text>T</text></svg>\n</body>\n</html>\n',
     '\nDon\u2019t STOP \u2014 \u0130stanbul \u039f\u0394\u039f\u03a3 \u201cquoted\u201d \U00020bb7\u91ce \U0002a6a5 the The THE \U00020bb7\u91ceT\n\n\n'),
    ('lt_nul',
     '<body>a<\x00b',
     'a<\ufffdb'),
    ('crlf',
     '<html>\r\n<head>\r\n<title>t</title>\r\n</head>\r\n<body>\r\n<p>a\r\nb\rc</p>\r\n</body>\r\n</html>\r\n',
     '\na\nb\nc\n\n\n'),
    ('pre_lf',
     '<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml">\n<head><title>T</title></head>\n<body>\n<pre>\nline1\nline2</pre><pre>\n\nx</pre><textarea>\nta</textarea><listing>\nli</listing>\n</body>\n</html>\n',
     '\nline1\nline2\nxtali\n\n\n'),
    ('pre_crlf',
     '<body><pre>\r\nline</pre></body>',
     'line'),
    ('pre_comment_lf',
     '<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml">\n<head><title>T</title></head>\n<body>\n<pre><!--c-->\nkeep</pre>\n</body>\n</html>\n',
     '\n\nkeep\n\n\n'),
    ('pre_entity_lf',
     '<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml">\n<head><title>T</title></head>\n<body>\n<pre>&#10;x</pre>\n</body>\n</html>\n',
     '\nx\n\n\n'),
    ('selfclose_script_head',
     '<html><head><script type="text/javascript" src="a.js"/></head><body><p>BODY</p></body></html>',
     ''),
    ('selfclose_title_head',
     '<html><head><title/><link rel="stylesheet" href="a.css"/></head><body><p>BODY</p></body></html>',
     ''),
    ('selfclose_style_head',
     '<html><head><style/></head><body><p>BODY</p></body></html>',
     ''),
    ('selfclose_script_body',
     '<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml">\n<head><title>T</title></head>\n<body>\n<p>a</p><script src="x.js"/><p>b</p></script><p>c</p>\n</body>\n</html>\n',
     '\nac\n\n\n'),
    ('selfclose_nonvoid',
     '<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml">\n<head><title>T</title></head>\n<body>\n<p>x<a id="n1"/>y<span/>z<div/>w</p>\n</body>\n</html>\n',
     '\nxyzw\n\n\n'),
    ('noscript_body',
     '<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml">\n<head><title>T</title></head>\n<body>\n<p>a</p><noscript><p>NS</p></noscript><p>b</p>\n</body>\n</html>\n',
     '\nab\n\n\n'),
    ('noscript_head',
     '<html><head><noscript><style>x</style></noscript></head><body>B</body></html>',
     'B'),
    ('entities',
     '<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml">\n<head><title>T</title></head>\n<body>\n<p>&copy 2020 &amp &nbspx &notit; &AMP; &#x80; &#0; &#xD800; &#1114112; &#1; &#x7f; &#xFFFE; &#x81; &#13; &lt;&gt; &unknown; &#x1F600; &#128512 &ampx</p>\n</body>\n</html>\n',
     '\n\xa9 2020 & \xa0x \xacit; & \u20ac \ufffd \ufffd \ufffd \x01 \x7f \ufffe \x81 \r <> &unknown; \U0001f600 \U0001f600 &x\n\n\n'),
    ('cdata_html',
     '<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml">\n<head><title>T</title></head>\n<body>\n<p><![CDATA[x<y]]>z</p>\n</body>\n</html>\n',
     '\nz\n\n\n'),
    ('cdata_svg',
     '<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml">\n<head><title>T</title></head>\n<body>\n<svg><text><![CDATA[a<b]]></text></svg>\n</body>\n</html>\n',
     '\na<b\n\n\n'),
    ('nul',
     '<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml">\n<head><title>T</title></head>\n<body>\n<p>a\x00b</p><title>t\x00t</title><textarea>q\x00q</textarea>\n</body>\n</html>\n',
     '\nabt\ufffdtq\ufffdq\n\n\n'),
    ('after_head_ws',
     '<html><head><title>t</title></head>\n  \n<body>B</body></html>',
     'B'),
    ('after_head_text',
     '<html><head><title>t</title></head>JUNK<body>B</body></html>',
     'JUNKB'),
    ('head_text',
     '<html><head>HX<title>t</title></head><body>B</body></html>',
     'HXtB'),
    ('head_p',
     '<html><head><meta charset="utf-8"/><p>oops</p></head><body>B</body></html>',
     'oopsB'),
    ('after_body',
     '<html><head></head><body><p>b</p></body>\nAFTER\n</html>\nEND\n',
     'b\nAFTER\n\nEND\n'),
    ('no_body',
     '<html><head><title>t</title></head>\n<p>para</p>\n</html>\n',
     'para\n\n'),
    ('no_body_no_head',
     '<p>frag</p> tail',
     'frag tail'),
    ('bare_text',
     '  hello <b>w</b>',
     'hello w'),
    ('table_foster',
     '<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml">\n<head><title>T</title></head>\n<body>\n<table>foo<tr><td>cell</td></tr>bar<tr><td>c2</td></tr></table>\n</body>\n</html>\n',
     '\nfoobarcellc2\n\n\n'),
    ('table_foster_ws',
     '<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml">\n<head><title>T</title></head>\n<body>\n<table>\n <tr>\n <td>cell</td>\n </tr>\n</table>\n</body>\n</html>\n',
     '\n\n \n cell\n \n\n\n\n'),
    ('table_foster_elem',
     '<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml">\n<head><title>T</title></head>\n<body>\n<p>pre</p><table><tr><span>SP</span><td>cell</td></tr></table>\n</body>\n</html>\n',
     '\npreSPcell\n\n\n'),
    ('astral',
     '<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml">\n<head><title>T</title></head>\n<body>\n<p>\U00020bb7\u91ce\u5bb6 \U0001f600 end</p>\n</body>\n</html>\n',
     '\n\U00020bb7\u91ce\u5bb6 \U0001f600 end\n\n\n'),
    ('comments',
     '<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml">\n<head><title>T</title></head>\n<body>\n<p>a<!-- -- -->b<!-->c<!--->d<!-- x --!>e<!-- unterminated\n</body>\n</html>\n',
     '\nabcde'),
    ('pi_body',
     '<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml">\n<head><title>T</title></head>\n<body>\n<p>a<?php echo 1 ?>b</p>\n</body>\n</html>\n',
     '\nab\n\n\n'),
    ('cond_comment',
     '<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml">\n<head><title>T</title></head>\n<body>\n<p>a<![if gte mso 9]>b<![endif]>c</p>\n</body>\n</html>\n',
     '\nabc\n\n\n'),
    ('uppercase',
     '<HTML><HEAD><TITLE>t</TITLE></HEAD><BODY><P>A</P><SCRIPT>s</SCRIPT><STYLE>y</STYLE>B</BODY></HTML>',
     'AB'),
    ('svg_style',
     '<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml">\n<head><title>T</title></head>\n<body>\n<svg><style>svgcss</style><title>svgtitle</title><script>svgscript</script><text>T</text></svg><style>htmlcss</style>\n</body>\n</html>\n',
     '\nsvgtitleT\n\n\n'),
    ('svg_foreignobject',
     '<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml">\n<head><title>T</title></head>\n<body>\n<svg><foreignObject><style>fo</style><p>FO</p></foreignObject></svg>\n</body>\n</html>\n',
     '\nFO\n\n\n'),
    ('math',
     '<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml">\n<head><title>T</title></head>\n<body>\n<math><mi>x</mi><mo>=</mo><mn>1</mn></math>\n</body>\n</html>\n',
     '\nx=1\n\n\n'),
    ('template',
     '<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml">\n<head><title>T</title></head>\n<body>\n<p>a</p><template><p>TPL</p></template><p>b</p>\n</body>\n</html>\n',
     '\nab\n\n\n'),
    ('lt_text',
     '<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml">\n<head><title>T</title></head>\n<body>\n<p>a < b <3 c </3 d </ e> f </> g</p>\n</body>\n</html>\n',
     '\na < b <3 c  f  g\n\n\n'),
    ('xml_decl',
     '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n<html><head><title>t</title></head><body><p>x</p></body></html>',
     'x'),
    ('body_twice',
     '<html><body><p>a</p><body><p>b</p></body></html>',
     'ab'),
    ('iframe_selfclose',
     '<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml">\n<head><title>T</title></head>\n<body>\n<p>a</p><iframe src="x"/><p>b</p></iframe><p>c</p>\n</body>\n</html>\n',
     '\na<p>b</p>c\n\n\n'),
    ('plaintext',
     '<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml">\n<head><title>T</title></head>\n<body>\n<p>a</p><plaintext><b>raw</b></plaintext>\n</body>\n</html>\n',
     '\na<b>raw</b></plaintext>\n</body>\n</html>\n'),
    ('ideographic_space',
     '<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml">\n<head><title>T</title></head>\n<body>\n<p>\u3000\u3000\u6bb5\u843d</p>\n\t<p>x</p>\n</body>\n</html>\n',
     '\n\u3000\u3000\u6bb5\u843d\n\tx\n\n\n'),
    ('nbsp_head',
     '<html><head>&nbsp;<title>t</title></head><body>B</body></html>',
     '\xa0tB'),
    ('script_in_p',
     '<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml">\n<head><title>T</title></head>\n<body>\n<p>x<script>var a = \'</p>\';</script>y</p>\n</body>\n</html>\n',
     '\nxy\n\n\n'),
    ('style_nested_tag',
     '<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml">\n<head><title>T</title></head>\n<body>\n<style>p{}</style><p>q</p><noscript><noscript>n</noscript>after</noscript>z\n</body>\n</html>\n',
     '\nqafterz\n\n\n'),
    ('select',
     '<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml">\n<head><title>T</title></head>\n<body>\n<select><option>o1</option>stray<b>bold</b></select>\n</body>\n</html>\n',
     '\no1straybold\n\n\n'),
    ('frameset_like',
     '<html><head></head><frameset><frame src="a"/></frameset></html>',
     ''),
    ('bom_mid',
     '<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml">\n<head><title>T</title></head>\n<body>\n<p>a\ufeffb</p>\n</body>\n</html>\n',
     '\na\ufeffb\n\n\n'),
    ('raw_lt_in_title_body',
     '<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml">\n<head><title>T</title></head>\n<body>\n<title>a<b>c</b></title>\n</body>\n</html>\n',
     '\na<b>c</b>\n\n\n'),
]


class TestInvariantRules(unittest.TestCase):
    """The five rules of CONTRACT.md section 2.1, one at a time."""

    def test_rule1_document_order(self):
        self.assertEqual(flatten_html("<body><div>1<p>2<b>3</b>4</p>5</div><p>6</p></body>"), "123456")

    def test_rule2_script_style_noscript_rejected_display_none_kept(self):
        html = ("<body>a<script>S</script>b<style>T</style>c<noscript><p>N</p></noscript>d"
                "<p style='display:none'>hidden</p><svg><style>svgcss</style><text>t</text></svg></body>")
        self.assertEqual(flatten_html(html), "abcdhiddent")

    def test_rule3_no_separators_collapsing_or_trimming(self):
        html = "<body>\n  <p>  two  spaces  </p>\n\t<p>x</p>\n</body>\n"
        self.assertEqual(flatten_html(html), "\n  " + "  two  spaces  " + "\n\t" + "x" + "\n\n")

    def test_rule4_character_references(self):
        self.assertEqual(flatten_html("<body>&amp;&#8212;&nbsp;&#x4E2D;&lt;&copy</body>"),
                         "&\u2014\u00a0\u4e2d<\u00a9")

    def test_rule5_code_points(self):
        text = flatten_html("<body>\U00020BB7\U0002A6A5\u91ce\U0001F600</body>")
        self.assertEqual(len(text), 4)                              # Python/gpos unit
        self.assertEqual(len(text.encode("utf-16-le")) // 2, 7)     # JS String.length

    def test_chromium_measured_table(self):
        self.assertGreaterEqual(len(CHROMIUM_MEASURED), 50)
        for name, html, expected in CHROMIUM_MEASURED:
            with self.subTest(case=name):
                self.assertEqual(flatten_html(html), expected)

    def test_html5_unescape_keeps_control_references(self):
        import html as html_module
        self.assertEqual(epublib.html5_unescape("&#1;&#x7f;&#xFFFE;"), "\x01\x7f\ufffe")
        self.assertEqual(html_module.unescape("&#1;&#x7f;&#xFFFE;"), "")     # why it exists
        self.assertEqual(epublib.html5_unescape("&#x80;&#x81;&#0;&#xD800;&#1114112;"),
                         "\u20ac\x81\ufffd\ufffd\ufffd")

    def test_normalize_newlines(self):
        self.assertEqual(epublib.normalize_newlines("a\r\nb\rc\n"), "a\nb\nc\n")
        self.assertEqual(epublib.normalize_newlines("<\x00"), "<\ufffd")

    def test_every_fixture_document_is_consistent(self):
        """plain_text() == flatten_html(read_text()) and never contains CR."""
        for path in sorted(FIXTURES.glob("*.epub")):
            try:
                book = EpubBook.open(path)
            except EpubError:
                continue
            with book:
                for item in book.spine:
                    with self.subTest(book=path.name, doc=item.zip_name):
                        text = book.plain_text(item.zip_name)
                        self.assertEqual(text, flatten_html(book.read_text(item.zip_name)))
                        self.assertNotIn("\r", text)


class TestPlainTextCli(unittest.TestCase):
    def _run(self, *args):
        env = dict(os.environ, PYTHONIOENCODING="cp437")   # hostile console encoding
        proc = subprocess.run([sys.executable, str(PROJECT_ROOT / "epublib.py"), "--plain-text", *args],
                              capture_output=True, env=env, timeout=120)
        return proc.returncode, json.loads(proc.stdout.decode("ascii"))

    def test_single_document(self):
        path = FIXTURES / "cjk_vertical.epub"
        with EpubBook.open(path) as book:
            name = book.spine[1].zip_name
            expected = book.plain_text(name)
        code, data = self._run(str(path), name)
        self.assertEqual(code, 0)
        self.assertEqual(data["text"], expected)
        self.assertEqual(data["zip_name"], name)
        self.assertEqual(data["unit"], "codepoint")
        self.assertEqual(data["length"], len(expected))
        self.assertEqual(data["utf16_length"], len(expected.encode("utf-16-le")) // 2)
        self.assertEqual(data["sha256"], hashlib.sha256(expected.encode("utf-8")).hexdigest())
        self.assertRegex(data["text"], _CJK)

    def test_astral_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _make_book(tmp, {"a.xhtml": "<p>\U00020BB7\u91ce\u5bb6</p>"})
            code, data = self._run(path, "a.xhtml")
        self.assertEqual(code, 0)
        self.assertEqual(data["text"], "\U00020BB7\u91ce\u5bb6")
        self.assertEqual((data["length"], data["utf16_length"], data["astral"]), (3, 4, 1))

    def test_whole_spine(self):
        path = FIXTURES / "epub2_ncx.epub"
        code, data = self._run(str(path))
        self.assertEqual(code, 0)
        with EpubBook.open(path) as book:
            self.assertEqual([d["zip_name"] for d in data], [s.zip_name for s in book.spine])
            self.assertEqual([d["text"] for d in data], [book.plain_text(s.zip_name) for s in book.spine])

    def test_errors(self):
        code, data = self._run(str(FIXTURES / "epub2_ncx.epub"), "no/such.xhtml")
        self.assertEqual((code, data["kind"]), (2, "no_entry"))
        code, data = self._run(str(FIXTURES / "drm_fake.epub"), "x")
        self.assertEqual((code, data["kind"]), (2, "drm"))
        code, data = self._run(str(FIXTURES / "does-not-exist.epub"), "x")
        self.assertEqual((code, data["kind"]), (2, "os_error"))

    def test_real_book_document(self):
        _need(BOOK_CIAN)
        code, data = self._run(str(BOOK_CIAN), "ops/chapter1.xhtml")
        self.assertEqual(code, 0)
        self.assertTrue(data["text"].startswith("\n\u7b2c\u4e00\u7ae0\u3000\u603b\u8bba\n"))
        self.assertNotIn("\r", data["text"])


# ===========================================================================
# 6. opt-in: live Chromium against the real assets/reader.js
# ===========================================================================

_CHROMIUM_HARNESS = textwrap.dedent(r'''
    import json, os, sys
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QTimer, QUrl
    from PySide6.QtWebEngineCore import (QWebEngineUrlScheme, QWebEngineUrlSchemeHandler,
        QWebEngineUrlRequestJob, QWebEngineProfile, QWebEnginePage, QWebEngineSettings)
    from PySide6.QtWidgets import QApplication
    s = QWebEngineUrlScheme(b"ertest"); s.setSyntax(QWebEngineUrlScheme.Syntax.Host)
    F = QWebEngineUrlScheme.Flag
    s.setFlags(F.SecureScheme | F.LocalScheme | F.LocalAccessAllowed | F.CorsEnabled)
    QWebEngineUrlScheme.registerScheme(s)
    jobs = json.load(open(sys.argv[1], encoding="utf-8"))
    reader_js = open(sys.argv[3], encoding="utf-8").read()
    CSP = b"default-src 'none'; style-src 'self' 'unsafe-inline'; script-src 'none'"
    class H(QWebEngineUrlSchemeHandler):
        def requestStarted(self, job):
            i = int(job.requestUrl().path().lstrip("/"))
            buf = QBuffer(job); buf.setData(QByteArray(jobs[i]["html"].encode("utf-8")))
            buf.open(QIODevice.OpenModeFlag.ReadOnly)
            job.setAdditionalResponseHeaders({QByteArray(b"Content-Security-Policy"): [CSP]})
            job.reply(b"text/html; charset=utf-8", buf)
    app = QApplication(sys.argv[:1])
    profile = QWebEngineProfile(); handler = H(); profile.installUrlSchemeHandler(b"ertest", handler)
    page = QWebEnginePage(profile)
    page.settings().setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
    out, state = [], {"i": -1}
    PROBE = ("(function(){var R=window.epubReader;if(!R){return JSON.stringify({error:'no epubReader'});}"
             "var t=R.flatText();try{R.init({settings:{},locator:null,mode:'scroll'});}catch(e){}"
             "return JSON.stringify({text:t,search:%s.map(function(q){return R.search(q);})});})()")
    def nxt():
        state["i"] += 1
        if state["i"] >= len(jobs):
            json.dump(out, open(sys.argv[2], "w", encoding="utf-8")); app.quit(); return
        page.load(QUrl("ertest://h/%d" % state["i"]))
    def loaded(ok):
        q = json.dumps(jobs[state["i"]]["queries"])
        page.runJavaScript(reader_js, 0, lambda _r: page.runJavaScript(
            PROBE % q, 0, lambda r: (out.append(json.loads(r) if r else {"error": "none"}),
                                     QTimer.singleShot(0, nxt))))
    page.loadFinished.connect(loaded)
    QTimer.singleShot(0, nxt); QTimer.singleShot(900000, app.quit)
    app.exec()
''')


@unittest.skipUnless(os.environ.get("EPUB_READER_CHROMIUM"), "set EPUB_READER_CHROMIUM=1 to run")
class TestLiveChromium(unittest.TestCase):
    QUERIES = ["\u7ecf\u6d4e", "\u6c47\u7387", "the", "\U00020BB7\u91ce", "\u201c", "-"]

    def test_plain_text_and_search_match_reader_js(self):
        reader_js = PROJECT_ROOT / "assets" / "reader.js"
        self.assertTrue(reader_js.exists())
        books = sorted(FIXTURES.glob("*.epub")) + [b["path"] for b in REAL_BOOKS.values() if b["path"].exists()]
        jobs, expected = [], []
        for path in books:
            try:
                book = EpubBook.open(path)
            except EpubError:
                continue
            with book:
                for item in book.spine:
                    html = book.read_text(item.zip_name)
                    jobs.append({"html": html[1:] if html.startswith("\ufeff") else html,
                                 "queries": self.QUERIES})
                    expected.append((path.name, item.zip_name, book.plain_text(item.zip_name)))
        for name, html, _text in CHROMIUM_MEASURED:
            jobs.append({"html": html, "queries": self.QUERIES})
            expected.append(("measured", name, flatten_html(html)))
        with tempfile.TemporaryDirectory() as tmp:
            harness = os.path.join(tmp, "harness.py")
            with open(harness, "w", encoding="utf-8") as fh:
                fh.write(_CHROMIUM_HARNESS)
            jp, op = os.path.join(tmp, "jobs.json"), os.path.join(tmp, "out.json")
            with open(jp, "w", encoding="utf-8") as fh:
                json.dump(jobs, fh, ensure_ascii=True)
            subprocess.run([sys.executable, harness, jp, op, str(reader_js)],
                           check=True, timeout=900, capture_output=True, cwd=tmp)
            with open(op, encoding="utf-8") as fh:
                got = json.load(fh)
        self.assertEqual(len(got), len(expected))
        for (book, doc, text), js in zip(expected, got):
            with self.subTest(book=book, doc=doc):
                self.assertNotIn("error", js)
                self.assertEqual(js["text"], text)
                for query, js_hits in zip(self.QUERIES, js["search"]):
                    hay, needle = fold_for_search(text), fold_for_search(query)
                    py_hits, i = [], hay.find(needle)
                    while i >= 0:
                        py_hits.append({"gpos": i, "length": len(needle)})
                        i = hay.find(needle, i + len(needle))
                    self.assertEqual(js_hits, py_hits[:5000], query)


# ---------------------------------------------------------------------------


def _report() -> None:  # pragma: no cover - human-readable summary
    for key, spec in REAL_BOOKS.items():
        if not spec["path"].exists():
            print("%s: absent" % key)
            continue
        t0 = time.perf_counter()
        with EpubBook.open(spec["path"]) as book:
            t1 = time.perf_counter()
            units = book.total_units()
            t2 = time.perf_counter()
            hits = book.search(spec["word"], limit=10 ** 9)
            t3 = time.perf_counter()
            entries = list(_flat(book.toc))
            print("%s\n  title   %s\n  authors %s\n  spine   %d\n  toc     %d top-level, %d total, depth %d"
                  % (spec["path"].name, book.metadata["title"], book.metadata["authors"], len(book.spine),
                     len(book.toc), len(entries), _depth(book.toc)))
            for e in entries[:5]:
                print("          - %s  -> %s%s" % (e.title, e.zip_name, "#" + e.fragment if e.fragment else ""))
            print("  units   %d\n  cover   %s\n  timing  open %.1f ms, total_units %.1f ms, search(%s) %.1f ms "
                  "(%d hits), sum %.1f ms" % (units, book.cover, (t1 - t0) * 1e3, (t2 - t1) * 1e3,
                                               spec["word"], (t3 - t2) * 1e3, len(hits), (t3 - t0) * 1e3))


if __name__ == "__main__":
    if "--report" in sys.argv:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
        _report()
    else:
        unittest.main(verbosity=2)
