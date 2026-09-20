# -*- coding: utf-8 -*-
"""Books to LaTeX + PDF (latexexport.py) and the convert dialog (convert_dialog.py).

The converter is checked on the generated fixtures, the Kindle samples, and a
"kitchen sink" EPUB built here (notes, tables, lists, pictures, CSS, scripts).
Typesetting tests run XeLaTeX and read the PDFs back with QtPdf; they are skipped
when no XeLaTeX is installed.

    python -m unittest tests.test_latexexport
"""

from __future__ import annotations

import io
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
import uuid
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import webhost  # noqa: E402,F401  (registers epub:// before any Qt application exists)
import bookformats  # noqa: E402
import latexexport as L  # noqa: E402
from epublib import EpubBook  # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures")
SAMPLES = os.path.join(HERE, "samples")
XELATEX = L.find_xelatex()
AUX = (".aux", ".log", ".out", ".toc", ".fls", ".fdb_latexmk", ".synctex.gz", ".xdv")

_app = None


def setUpModule() -> None:
    global _app
    from PySide6.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication([])


def _png(w: int, h: int, colour=(200, 40, 40)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), colour).save(buf, "PNG")
    return buf.getvalue()


def _gif(w: int, h: int) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("P", (w, h), 3).save(buf, "GIF")
    return buf.getvalue()


def _xhtml(title: str, body: str, head: str = "", lang: str = "en") -> str:
    return ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
            f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
            f'xml:lang="{lang}" lang="{lang}"><head><title>{title}</title>'
            f'<link rel="stylesheet" type="text/css" href="style.css"/>{head}</head>'
            f"<body>{body}</body></html>")


DEEP = "".join("<ul><li>level%d" % i for i in range(1, 8)) + "</li></ul>" * 7

CH1 = _xhtml("Chapter One", f"""
<section epub:type="chapter">
<h1>Chapter One</h1>
<p>Special: \\ {{ }} $ &amp; # ^ _ % ~ -- `` '' ‘quoted’ — café, Ελληνικά, Кириллица, ✓ check, 𝔸 letter.</p>
<p class="c">A centred line CENTRED-LINE</p>
<p>Styles <em>em</em> <strong>strong</strong> <span class="it">css-italic</span> x<sup>2</sup> H<sub>2</sub>O
<u>under</u> <s>strike</s> <code>x = 1</code>. An explicit note<a epub:type="noteref" href="notes.xhtml#n1">1</a>
and a numbered one<sup><a href="#fn2" id="r2">[2]</a></sup>.</p>
<p>Links: <a href="https://example.com/a%20b?x=1&amp;y=2#frag">outside</a> and
<a href="ch2.xhtml#target">to chapter two</a>.</p>
<ul><li>one</li><li>two<ul><li>nested</li></ul></li></ul>
<ol start="3"><li>three</li><li><p>four para</p><p>second para</p></li></ol>
{DEEP}
<blockquote><p>A quotation QUOTE-TEXT.</p></blockquote>
<pre>  indented {{code}}
    more $x and 100%</pre>
<table><caption>Table caption</caption>
<tr><th>Head A</th><th>Head B</th><th>Head C</th></tr>
<tr><td colspan="2">SPANNED-CELL</td><td>c</td></tr>
<tr><td rowspan="2">r</td><td>a</td><td>b</td></tr>
<tr><td>x</td><td>[y] &amp; z</td></tr>
</table>
<p><img src="img/pic.png" alt="picture"/></p>
<p>Inline <img src="img/icon.gif" alt="i"/> gif.</p>
<h2 id="sec2">Section Two</h2>
<p>Ruby: <ruby>漢<rt>かん</rt>字<rt>じ</rt></ruby>.</p>
<div style="display:none">HIDDEN-TEXT</div>
<div class="gone">ALSO-HIDDEN</div>
<script>var SCRIPT_TEXT = 1;</script>
<p><svg xmlns="http://www.w3.org/2000/svg" width="60" height="40"><rect width="60" height="40" fill="blue"/></svg></p>
<p id="fn2-host"><a id="fn2" href="#r2">[2]</a> Numbered footnote FN2-TEXT.</p>
</section>""")

CH2 = _xhtml("第二章", """
<h1>第二章 中文</h1>
<p id="target">中文段落CJK-PARA，包含“引号”和（括号）。</p>
<p>日本語のかなカナ。</p>
""")

NOTES = _xhtml("Notes", """
<h1>Notes</h1>
<aside epub:type="footnote" id="n1"><p>Explicit footnote FN1-TEXT with <em>style</em>.</p></aside>
""")

COVER = _xhtml("Cover", '<div><img src="img/cover.png" alt="cover"/></div>')

NAV = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>toc</title></head>
<body><nav epub:type="toc"><ol>
<li><a href="ch1.xhtml">Chapter One</a><ol><li><a href="ch1.xhtml#sec2">Section Two</a></li></ol></li>
<li><a href="ch2.xhtml">第二章 中文</a></li>
<li><a href="notes.xhtml">Notes</a></li>
</ol></nav></body></html>"""

CSS = ".c { text-align: center } .it { font-style: italic } .gone { display: none }"


def build_kitchen_sink(path: str, lang: str = "en") -> str:
    files = {
        "OEBPS/cover.xhtml": COVER, "OEBPS/ch1.xhtml": CH1, "OEBPS/ch2.xhtml": CH2,
        "OEBPS/notes.xhtml": NOTES, "OEBPS/nav.xhtml": NAV, "OEBPS/style.css": CSS,
    }
    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="uid">urn:uuid:{uuid.uuid4()}</dc:identifier><dc:title>Kitchen Sink &amp; Co</dc:title>
<dc:creator>Ada Lovelace</dc:creator><dc:language>{lang}</dc:language>
<meta property="dcterms:modified">2026-01-01T00:00:00Z</meta></metadata>
<manifest>
<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
<item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>
<item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml" properties="svg"/>
<item id="ch2" href="ch2.xhtml" media-type="application/xhtml+xml"/>
<item id="notes" href="notes.xhtml" media-type="application/xhtml+xml"/>
<item id="css" href="style.css" media-type="text/css"/>
<item id="cimg" href="img/cover.png" media-type="image/png" properties="cover-image"/>
<item id="pic" href="img/pic.png" media-type="image/png"/>
<item id="icon" href="img/icon.gif" media-type="image/gif"/>
</manifest>
<spine><itemref idref="cover"/><itemref idref="ch1"/><itemref idref="ch2"/><itemref idref="notes"/></spine>
</package>"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:'
                   'xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" '
                   'media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("OEBPS/content.opf", opf)
        for name, text in files.items():
            z.writestr(name, text)
        z.writestr("OEBPS/img/cover.png", _png(300, 450, (30, 60, 120)))
        z.writestr("OEBPS/img/pic.png", _png(640, 200))
        z.writestr("OEBPS/img/icon.gif", _gif(16, 16))
    return path


def _body(tex: str) -> str:
    return tex.split("\\begin{document}", 1)[1]


def _unescaped_braces(tex: str) -> tuple[int, int]:
    t = re.sub(r"\\[{}]", "", tex.replace("\\\\", ""))
    t = re.sub(r"(?m)^%.*$", "", t)
    return t.count("{"), t.count("}")


def _max_list_depth(tex: str) -> int:
    depth = best = 0
    for m in re.finditer(r"\\(begin|end)\{(itemize|enumerate|description|quote)\}", tex):
        depth += 1 if m.group(1) == "begin" else -1
        best = max(best, depth)
    return best


class _Temp(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="latexexport-test-")
        self.cache = os.path.join(self.tmp, "cache")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def open(self, path: str):
        book = bookformats.open_book(path, cache_root=self.cache)
        self.addCleanup(book.close)
        return book

    def write(self, path: str, **opts) -> tuple[L.ExportResult, str]:
        book = self.open(path)
        folder = os.path.join(self.tmp, "out", os.path.basename(path))
        res = L.write_latex(book, folder, "book", L.ExportOptions(**opts))
        with open(res.tex_path, encoding="utf-8") as f:
            return res, f.read()


# ==========================================================================
# helpers
# ==========================================================================

class Helpers(unittest.TestCase):
    def test_safe_stem(self) -> None:
        self.assertEqual(L.safe_stem('a<b>:c"d/e\\f|g?h*i'), "a b c d e f g h i")
        self.assertEqual(L.safe_stem("  ..  "), "book")
        self.assertEqual(L.safe_stem("CON"), "_CON")
        self.assertEqual(len(L.safe_stem("x" * 300)), 80)
        self.assertEqual(L.safe_stem("紅樓夢"), "紅樓夢")

    def test_css_simple_selectors_only(self) -> None:
        st = L._Styles()
        st.add("/* c */ p.c, .x { text-align: center; font-style: italic !important }"
               " div p { font-weight: bold } @media amzn-mobi { .x { display: none } }"
               " @media screen { .y { display: none } } h1 { font-size: 2em }")
        from lxml import etree
        el = etree.fromstring('<p class="c x"/>')
        self.assertEqual(st.style(el, "p").get("text-align"), "center")
        self.assertEqual(st.style(el, "p").get("font-style"), "italic")
        self.assertNotIn("font-weight", st.style(el, "p"))          # descendant rule skipped
        self.assertNotIn("display", st.style(el, "p"))               # amzn-mobi block skipped
        self.assertEqual(st.style(etree.fromstring('<b class="y"/>'), "b").get("display"), "none")
        self.assertEqual(L._size_cmd("2em"), r"\huge")
        self.assertEqual(L._size_cmd("80%"), r"\small")
        self.assertIsNone(L._size_cmd("1em"))
        self.assertIsNone(L._size_cmd("12px"))

    def test_parse_malformed_falls_back(self) -> None:
        root = L.parse_document("<html><body><p>a &nbsp; b<br></p><p>unclosed</body></html>")
        text = "".join(L._body(root).itertext())
        self.assertIn("unclosed", text)
        root = L.parse_document('<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
                                "<body><p>&mdash;&eacute;</p></body></html>")
        self.assertEqual("".join(L._body(root).itertext()), "—é")

    def test_font_cmap_reader(self) -> None:
        cat = L._font_catalog()
        if "Cambria" not in cat:
            self.skipTest("Cambria is not installed")
        f = cat["Cambria"]
        self.assertTrue(f.has(ord("A")) and f.has(0x03B1) and f.has(0x0416))   # Latin, Greek, Cyrillic
        self.assertFalse(f.has(0x4E2D))                                         # no Chinese
        if "SimSun" in cat:
            self.assertTrue(cat["SimSun"].has(0x4E2D))

    def test_escape(self) -> None:
        w = L._Writer.__new__(L._Writer)
        w.esc_map = {ord(k): v for k, v in L._BASE_ESC.items()}
        out = w.esc("\\{}$&#^_%~ -- '' ,, x\u00a0y\u00ad\x07")
        self.assertEqual(out, r"\textbackslash{}\{\}\$\&\#\textasciicircum{}\_\%\textasciitilde{} "
                              r"-{}- '{}' ,{}, x~y\-")

    def test_script_votes(self) -> None:
        s, t = L._script_votes("這們來說：他們這樣說過")
        self.assertGreater(t, s)
        s, t = L._script_votes("这们来说：他们这样说过")
        self.assertGreater(s, t)


# ==========================================================================
# conversion (no typesetting)
# ==========================================================================

class Convert(_Temp):
    def test_kitchen_sink_source(self) -> None:
        path = build_kitchen_sink(os.path.join(self.tmp, "sink.epub"))
        res, tex = self.write(path)
        body = _body(tex)
        # headings: the book's own h1 becomes the chapter title, once
        self.assertEqual(body.count("\\chapter*{Chapter One}"), 1)
        self.assertNotIn("\\erhead{1}{Chapter One}", body)
        self.assertIn("\\section*{Section Two}", body)
        self.assertIn("\\addcontentsline{toc}{section}{Section Two}", body)
        self.assertIn("\\chapter*{第二章 中文}", body)
        # the notes page was emptied into footnotes, so it (and its contents entry) is gone
        self.assertNotIn("{Notes}", body)
        foot = re.findall(r"\\footnote\{(.*?)\}\s", body, re.S)
        self.assertTrue(any("FN1-TEXT" in f for f in foot), foot)
        self.assertTrue(any("FN2-TEXT" in f for f in foot), foot)
        self.assertEqual(body.count("FN2-TEXT"), 1)
        self.assertNotIn("[2]", body.split("FN2-TEXT")[0][-40:])        # the back-link label is dropped
        # hidden and non-content text is not written
        for gone in ("HIDDEN-TEXT", "ALSO-HIDDEN", "SCRIPT_TEXT"):
            self.assertNotIn(gone, body)
        # special characters are escaped
        self.assertIn(r"\textbackslash{} \{ \} \$ \& \# \textasciicircum{} \_ \% \textasciitilde{} -{}-", body)
        self.assertIn("{\\centering A centred line CENTRED-LINE", body)
        self.assertIn("{\\itshape css-italic\\/}", body)
        self.assertIn("\\textsuperscript{2}", body)
        self.assertIn("\\textsubscript{2}", body)
        self.assertIn("\\href{https://example.com/a\\%20b?x=1&y=2\\#frag}{outside}", body)
        self.assertRegex(body, r"\\hyperlink\{d\d\.target\}\{to chapter two\}")
        self.assertRegex(body, r"\\hypertarget\{d\d\.target\}\{\}")
        self.assertIn("\\begin{enumerate}[start=3]", body)
        self.assertLessEqual(_max_list_depth(body), 5)
        self.assertIn("level7", body)
        self.assertIn("\\begin{quote}", body)
        self.assertIn("\\multicolumn{2}", body)
        self.assertIn("\\begin{longtable}", body)
        self.assertIn("\\bfseries Head A", body)
        self.assertIn("\\erruby{漢}{かん}", body)
        self.assertIn("\\ttfamily", body)
        # pictures: cover, png, gif (converted), inline svg (rasterised)
        imgs = sorted(os.listdir(os.path.join(res.folder, "images")))
        self.assertEqual(res.images, len(imgs))
        self.assertGreaterEqual(len(imgs), 4, imgs)
        self.assertTrue(all(n.endswith((".png", ".jpg")) for n in imgs), imgs)
        self.assertIn("\\AddToShipoutPictureBG*", tex)                   # the cover page
        self.assertNotIn("cover.xhtml ----", body)                       # the cover document is not repeated
        # the source is well formed
        o, c = _unescaped_braces(body)
        self.assertEqual(o, c)
        self.assertEqual(re.findall(r"\\[A-Za-z]+[\u3000-\u9fff]", tex), [])
        # fonts: Greek/Cyrillic need a wider Latin font than Latin Modern; CJK via xeCJK
        self.assertIn("\\usepackage{xeCJK}", tex)
        if "Cambria" in L._font_catalog():
            self.assertIn("\\setmainfont{Cambria}", tex)

    def test_fixtures_convert_cleanly(self) -> None:
        for name in ("epub2_ncx", "epub3_nav", "cjk_vertical", "images_fonts", "odd_paths", "no_toc",
                     "fixed_layout", "broken_xml", "obfuscated_fonts", "big_book"):
            path = os.path.join(FIXTURES, name + ".epub")
            if not os.path.isfile(path):
                continue
            with self.subTest(name):
                res, tex = self.write(path)
                o, c = _unescaped_braces(_body(tex))
                self.assertEqual(o, c)
                self.assertEqual(re.findall(r"\\[A-Za-z]+[\u3000-\u9fff]", tex), [])
                self.assertTrue(tex.rstrip().endswith("\\end{document}"))
                # no chapter title is printed twice
                for title in re.findall(r"\\chapter\*\{([^{}]+)\}", tex):
                    self.assertNotIn("\\erhead{1}{" + title + "}", tex)

    def test_epub3_nav_structure(self) -> None:
        res, tex = self.write(os.path.join(FIXTURES, "epub3_nav.epub"))
        self.assertEqual(res.chapters, 3)
        self.assertIn("\\addcontentsline{toc}{subsection}{Beta Subsection}", tex)
        self.assertIn("\\tableofcontents", tex)
        self.assertIn(r"café \& crème", tex)

    def test_cjk_book_uses_ctex(self) -> None:
        res, tex = self.write(os.path.join(FIXTURES, "cjk_vertical.epub"))
        self.assertIn("{ctexbook}", tex.splitlines()[0])

    @unittest.skipUnless(os.path.isfile(os.path.join(SAMPLES, "gb11.mobi")), "Kindle samples absent")
    def test_kindle_chapters(self) -> None:
        for name in ("gb11.mobi", "gb11_kf8.azw3"):
            with self.subTest(name):
                res, tex = self.write(os.path.join(SAMPLES, name))
                heads = re.findall(r"\\addcontentsline\{toc\}\{chapter\}\{(CHAPTER [IVX]+\.[^{}]*)\}", tex)
                self.assertEqual(len(heads), 12, heads)

    @unittest.skipUnless(os.path.isfile(os.path.join(SAMPLES, "gb24264_kf8.azw3")), "Kindle samples absent")
    def test_traditional_chinese_detected(self) -> None:
        res, tex = self.write(os.path.join(SAMPLES, "gb24264_kf8.azw3"))
        self.assertIn("contentsname={目錄}", tex)
        self.assertIn("fontset=none", tex.splitlines()[0])
        self.assertGreaterEqual(res.chapters, 100)

    def test_options(self) -> None:
        path = build_kitchen_sink(os.path.join(self.tmp, "sink.epub"))
        _res, tex = self.write(path, paper="a4", font_size=12, cover=False, contents=False)
        self.assertIn("[12pt,", tex.splitlines()[0])
        self.assertIn("paperwidth=210mm,paperheight=297mm", tex)
        self.assertNotIn("\\tableofcontents", tex)
        self.assertNotIn("AddToShipoutPictureBG", tex)
        self.assertIn("\\begin{titlepage}", tex)                   # a text title page instead
        self.assertIn("Kitchen Sink \\& Co", tex)

    def test_cancel_removes_the_folder(self) -> None:
        book = self.open(os.path.join(FIXTURES, "big_book.epub"))
        calls = []

        def cancelled() -> bool:
            calls.append(1)
            return len(calls) > 20
        with self.assertRaises(L.ExportCancelled):
            L.export_book(book, os.path.join(self.tmp, "dest"), L.ExportOptions(compile_pdf=False),
                          cancelled=cancelled)
        self.assertEqual(os.listdir(os.path.join(self.tmp, "dest")), [])

    def test_unique_folders_and_book_untouched(self) -> None:
        src = os.path.join(FIXTURES, "epub2_ncx.epub")
        copy = os.path.join(self.tmp, "b.epub")
        shutil.copyfile(src, copy)
        with open(copy, "rb") as f:
            before = f.read()
        book = self.open(copy)
        a = L.export_book(book, self.tmp, L.ExportOptions(compile_pdf=False))
        b = L.export_book(book, self.tmp, L.ExportOptions(compile_pdf=False))
        self.assertNotEqual(a.folder, b.folder)
        self.assertTrue(b.folder.endswith("(2)"))
        with open(copy, "rb") as f:
            self.assertEqual(f.read(), before)


# ==========================================================================
# typesetting
# ==========================================================================

def _pdf_text(path: str) -> tuple[int, str]:
    from PySide6.QtPdf import QPdfDocument
    doc = QPdfDocument()
    doc.load(path)
    n = doc.pageCount()
    text = "\n".join(doc.getAllText(i).text() for i in range(n))
    doc.close()
    return n, text


@unittest.skipIf(XELATEX is None, "XeLaTeX is not installed")
class Typeset(_Temp):
    def export(self, path: str, **opts) -> L.ExportResult:
        book = self.open(path)
        return L.export_book(book, os.path.join(self.tmp, "out"), L.ExportOptions(**opts))

    def assert_clean_folder(self, res: L.ExportResult) -> None:
        left = [n for n in os.listdir(res.folder) if n.endswith(AUX)]
        self.assertEqual(left, [], "TeX byproducts must never be left beside the source")

    def test_kitchen_sink_pdf(self) -> None:
        path = build_kitchen_sink(os.path.join(self.tmp, "sink.epub"))
        res = self.export(path)
        self.assertTrue(res.pdf_path and os.path.isfile(res.pdf_path), res.problems)
        self.assertEqual(res.problems, [])
        self.assert_clean_folder(res)
        pages, text = _pdf_text(res.pdf_path)
        self.assertEqual(pages, res.pages)
        flat = re.sub(r"\s+", "", text)
        for needle in ("ChapterOne", "SectionTwo", "FN1-TEXT", "FN2-TEXT", "SPANNED-CELL", "QUOTE-TEXT",
                       "CENTRED-LINE", "中文段落CJK-PARA", "Ελληνικά", "Кириллица", "level7"):
            self.assertIn(needle, flat)
        self.assertNotIn("HIDDEN-TEXT", flat)

    def test_fixtures_typeset_without_errors(self) -> None:
        for name in ("epub3_nav", "cjk_vertical", "images_fonts", "odd_paths"):
            path = os.path.join(FIXTURES, name + ".epub")
            with self.subTest(name):
                res = self.export(path)
                self.assertTrue(res.pdf_path, res.problems)
                self.assertEqual(res.problems, [])
                self.assert_clean_folder(res)

    @unittest.skipUnless(os.path.isfile(os.path.join(SAMPLES, "gb11.mobi")), "Kindle samples absent")
    def test_kindle_book_pdf(self) -> None:
        res = self.export(os.path.join(SAMPLES, "gb11.mobi"))
        self.assertEqual(res.problems, [])
        pages, text = _pdf_text(res.pdf_path)
        self.assertGreater(pages, 60)
        self.assertIn("Down the Rabbit-Hole", text)
        self.assertIn("Alice", text)

    def test_cancel_while_typesetting(self) -> None:
        book = self.open(os.path.join(FIXTURES, "big_book.epub"))
        seen = threading.Event()

        def progress(stage: str, done: int, total: int) -> None:
            if stage.startswith("typeset"):
                seen.set()
        with self.assertRaises(L.ExportCancelled):
            L.export_book(book, os.path.join(self.tmp, "dest"), L.ExportOptions(),
                          progress=progress, cancelled=seen.is_set)
        self.assertEqual(os.listdir(os.path.join(self.tmp, "dest")), [])


# ==========================================================================
# the dialog and the background job
# ==========================================================================

class Dialog(_Temp):
    def test_dialog_defaults_and_preview(self) -> None:
        import convert_dialog as C

        class FakeStore(dict):
            cache_root = None

            def get(self, key, default=None):
                return dict.get(self, key, default)

            def set(self, key, value):
                self[key] = value
        store = FakeStore({"convert.paper": "a4", "convert.font_size": 12})
        os.makedirs(os.path.join(self.tmp, "Book Title"))
        dlg = C.ConvertDialog(None, title="Book Title", source_format="epub", destination=self.tmp,
                              store=store, engine="xelatex.exe")
        self.assertEqual(dlg.options().paper, "a4")
        self.assertEqual(dlg.options().font_size, 12)
        self.assertTrue(dlg.options().compile_pdf)
        self.assertTrue(dlg.target_path().endswith("Book Title (2)"))
        self.assertIn("Book Title (2)", dlg.preview.text())
        dlg.paper.setCurrentIndex(dlg.paper.findData("letter"))
        dlg._accept()
        self.assertEqual(store["convert.paper"], "letter")
        self.assertEqual(store["convert.last_dir"], os.path.normpath(self.tmp))
        dlg.deleteLater()
        # no XeLaTeX: the PDF option is off and explained
        dlg = C.ConvertDialog(None, title="B", source_format="mobi", destination=self.tmp, engine=None)
        self.assertFalse(dlg.options().compile_pdf)
        self.assertFalse(dlg.compile.isEnabled())
        self.assertIsNotNone(dlg.findChild(type(dlg.preview), "convert-no-tex"))
        dlg.deleteLater()
        # DjVu: a PDF file, no layout options
        dlg = C.ConvertDialog(None, title="Scan", source_format="djvu", destination=self.tmp, engine=None)
        self.assertTrue(dlg.target_path().endswith("Scan.pdf"))
        self.assertTrue(dlg.paper.isHidden())
        dlg.deleteLater()

    def _run_job(self, job) -> dict:
        from PySide6.QtCore import QEventLoop, QTimer
        loop = QEventLoop()
        out: dict = {}
        job.succeeded.connect(lambda r: (out.update(r), loop.quit()))
        job.failed.connect(lambda d: (out.update(error=d), loop.quit()))
        job.cancelled.connect(lambda: (out.update(cancelled=True), loop.quit()))
        QTimer.singleShot(240_000, loop.quit)
        job.start()
        loop.exec()
        job.wait(10)
        return out

    def test_job_latex_source_only(self) -> None:
        import convert_dialog as C
        job = C.ConvertJob(path=os.path.join(FIXTURES, "epub2_ncx.epub"), source_format="epub",
                           destination=self.tmp, title="Two", cache_root=self.cache,
                           options=L.ExportOptions(compile_pdf=False))
        stages = []
        job.progressed.connect(lambda s, d, t: stages.append(s))
        out = self._run_job(job)
        self.assertEqual(out.get("kind"), "latex", out)
        self.assertTrue(os.path.isfile(out["tex"]))
        self.assertIsNone(out["pdf"])
        self.assertIn("convert", stages)
        main, extra, _details = C.result_text(out)
        self.assertIn(out["folder"], main)

    @unittest.skipUnless(os.path.isfile(os.path.join(SAMPLES, "ia_jstor_20637537.djvu")), "DjVu sample absent")
    def test_job_djvu_to_pdf(self) -> None:
        import convert_dialog as C
        job = C.ConvertJob(path=os.path.join(SAMPLES, "ia_jstor_20637537.djvu"), source_format="djvu",
                           destination=self.tmp, title="Jstor Scan", cache_root=self.cache)
        out = self._run_job(job)
        self.assertEqual(out.get("kind"), "djvu", out)
        self.assertTrue(out["pdf"].endswith("Jstor Scan.pdf"))
        pages, text = _pdf_text(out["pdf"])
        self.assertEqual(pages, out["pages"])
        self.assertGreater(len(text.strip()), 100)

    def test_job_failure_is_reported(self) -> None:
        import convert_dialog as C
        job = C.ConvertJob(path=os.path.join(FIXTURES, "not_an_epub.epub"), source_format="epub",
                           destination=self.tmp, title="Bad", cache_root=self.cache,
                           options=L.ExportOptions(compile_pdf=False))
        out = self._run_job(job)
        self.assertIn("error", out)
        self.assertEqual([n for n in os.listdir(self.tmp) if n != "cache"], [])


if __name__ == "__main__":
    unittest.main()
