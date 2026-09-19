#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate the EPUB test corpus for the epub-reader project.

stdlib only. Deterministic: same bytes every run (fixed zip timestamps, no
randomness, no hashes of wall-clock data). Run with the 3.14 interpreter:

    C:\\Users\\mengz\\AppData\\Local\\Programs\\Python\\Python314\\python.exe ^
        C:\\Users\\mengz\\epub-reader\\tests\\make_fixtures.py

Options:
    --list      print the fixture inventory and exit
    --verify    re-open every fixture with zipfile and sanity-check it
    --clean     delete tests/fixtures first
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _assets  # noqa: E402  (local helper, stdlib only)

FIXTURES = HERE / "fixtures"

# A fixed, pre-1980-safe DOS timestamp so zip bytes never change.
ZIP_DATE = (1980, 1, 1, 0, 0, 0)

OCF_NS = "urn:oasis:names:tc:opendocument:xmlns:container"

# Every fixture, and the single sentence explaining what it exists to break.
INVENTORY = [
    ("epub2_ncx.epub", "EPUB 2.0.1: toc.ncx with 3-level navPoints, guide/cover reference, OPF under OEBPS/, a linear=no spine item, two dc:creator entries"),
    ("epub3_nav.epub", "EPUB 3.0: nav.xhtml properties=\"nav\" with nested <ol>, landmarks + page-list, cover-image property, refines-based author roles"),
    ("cjk_vertical.epub", "Chinese prose with CJK punctuation, a writing-mode: vertical-rl chapter, page-progression-direction=rtl, CJK TOC titles"),
    ("images_fonts.epub", "relative hrefs 1 and 2 dirs up/down, @font-face resolved relative to the CSS (not the XHTML), real TTF + WOFF, SVG cover, JPEG and PNG"),
    ("odd_paths.epub", "percent-encoded hrefs (space, Chinese, %23), OPF at zip root, ../ segments in hrefs, a manifest href whose case does not match the zip entry"),
    ("no_toc.epub", "no NCX and no nav document: reader must synthesise a TOC from spine order + <title>, including a chapter with an empty <title>"),
    ("fixed_layout.epub", "rendition:layout pre-paginated, viewport meta, page-spread-left/right spine properties"),
    ("big_book.epub", "150 chapters / ~2 MB of uncompressed text with searchable sentinels, for navigation, search and memory behaviour"),
    ("broken_xml.epub", "chapters that are NOT well-formed XML: unclosed tags, raw &, unquoted attributes, undefined entities - the parser must recover"),
    ("drm_fake.epub", "META-INF/encryption.xml using real AES encryption URIs plus rights.xml: must be reported as DRM, not crashed on"),
    ("obfuscated_fonts.epub", "META-INF/encryption.xml using the IDPF font-obfuscation algorithm: must NOT be misreported as DRM"),
    ("not_an_epub.epub", "a perfectly valid zip with no mimetype and no META-INF/container.xml"),
    ("no_container.epub", "correct mimetype but META-INF/container.xml is missing"),
    ("truncated.epub", "the first 55% of a valid EPUB: a corrupt zip"),
    ("not_a_zip.epub", "plain UTF-8 text wearing an .epub extension"),
    ("empty.epub", "a zero-byte file"),
]


# --------------------------------------------------------------------------
# zip builder
# --------------------------------------------------------------------------


class EpubBuilder:
    """Builds a byte-for-byte reproducible OCF zip container."""

    def __init__(self, filename: str, mimetype: str | None = "application/epub+zip"):
        self.filename = filename
        self.mimetype = mimetype
        self._entries: list[tuple[str, bytes, int]] = []

    def add(self, name: str, data, compress: bool = True) -> "EpubBuilder":
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._entries.append(
            (name, data, zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED)
        )
        return self

    def container(self, opf_path: str) -> "EpubBuilder":
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<container version="1.0" xmlns="%s">\n'
            "  <rootfiles>\n"
            '    <rootfile full-path="%s" media-type="application/oebps-package+xml"/>\n'
            "  </rootfiles>\n"
            "</container>\n" % (OCF_NS, opf_path)
        )
        return self.add("META-INF/container.xml", xml)

    def to_bytes(self) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            if self.mimetype is not None:
                # OCF: first entry, stored, no extra field.
                zi = zipfile.ZipInfo("mimetype", date_time=ZIP_DATE)
                zi.compress_type = zipfile.ZIP_STORED
                zi.create_system = 0
                zi.external_attr = 0o600 << 16
                zf.writestr(zi, self.mimetype.encode("ascii"))
            for name, data, comp in self._entries:
                zi = zipfile.ZipInfo(name, date_time=ZIP_DATE)
                zi.compress_type = comp
                zi.create_system = 0
                zi.external_attr = 0o600 << 16
                zf.writestr(zi, data)
        return buf.getvalue()

    def save(self) -> Path:
        path = FIXTURES / self.filename
        path.write_bytes(self.to_bytes())
        return path


# --------------------------------------------------------------------------
# markup helpers
# --------------------------------------------------------------------------


def xhtml(title: str, body: str, *, lang: str = "en", head: str = "",
          css: str | None = None, body_attrs: str = "") -> str:
    link = '\n  <link rel="stylesheet" type="text/css" href="%s"/>' % css if css else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="%s" lang="%s">\n'
        "<head>\n"
        '  <meta charset="utf-8"/>\n'
        "  <title>%s</title>%s%s\n"
        "</head>\n"
        "<body%s>\n%s\n</body>\n</html>\n"
        % (lang, lang, title, link, head, body_attrs, body)
    )


def manifest_item(iid: str, href: str, mtype: str, props: str = "") -> str:
    p = ' properties="%s"' % props if props else ""
    return '    <item id="%s" href="%s" media-type="%s"%s/>\n' % (iid, href, mtype, p)


MT = {
    ".xhtml": "application/xhtml+xml",
    ".html": "application/xhtml+xml",
    ".css": "text/css",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ttf": "application/font-sfnt",
    ".woff": "application/font-woff",
    ".ncx": "application/x-dtbncx+xml",
}


def media_type_for(href: str) -> str:
    return MT.get(os.path.splitext(href.split("#")[0])[1].lower(), "application/octet-stream")


# --------------------------------------------------------------------------
# 1. epub2_ncx.epub
# --------------------------------------------------------------------------

EPUB2_ID = "urn:uuid:0000e2nc-0000-4000-8000-00000000ncx1"

CSS_BASIC = """\
body { margin: 1em; font-family: serif; line-height: 1.5; }
h1 { font-size: 1.6em; border-bottom: 2px solid #c44; }
.note { color: #555; font-style: italic; }
"""


def build_epub2_ncx() -> EpubBuilder:
    b = EpubBuilder("epub2_ncx.epub")
    b.container("OEBPS/content.opf")

    pages = {
        "cover.xhtml": ("Front Cover", """\
  <h1>Front Cover</h1>
  <p>FIXTURE: <strong>epub2_ncx.epub</strong> &#8212; EPUB 2.0.1 with a toc.ncx.</p>
  <p><img src="images/cover.png" alt="cover"/></p>
  <p class="note">Expected: this page is reachable via the guide's cover reference
  and is spine item 0.</p>"""),
        "ch1.xhtml": ("Chapter One", """\
  <h1>Chapter One</h1>
  <p>NCX-TOP-LEVEL-2. This chapter has one child and one grandchild in the NCX,
  so the TOC tree must be three levels deep.</p>
  <p>Quotes &amp; ampersands &#8212; em dashes &#8212; and &lt;escaped&gt; angle brackets
  all belong to this paragraph.</p>"""),
        "ch1s1.xhtml": ("Section 1.1", """\
  <h1>Section 1.1</h1>
  <p>NCX-DEPTH-2 marker. Nested one level under Chapter One.</p>"""),
        "ch1s1a.xhtml": ("Subsection 1.1.1", """\
  <h1 id="sub">Subsection 1.1.1</h1>
  <p>NCX-DEPTH-3 marker. The NCX points here with the fragment <code>#sub</code>.</p>"""),
        "ch2.xhtml": ("Chapter Two", """\
  <h1>Chapter Two</h1>
  <p>NCX-TOP-LEVEL-3. Last linear chapter.</p>
  <p>A relative link one directory down: <img src="images/cover.png" alt="again"/></p>"""),
        "notes.xhtml": ("Endnotes", """\
  <h1>Endnotes</h1>
  <p>LINEAR-NO-SENTINEL. This item is in the spine with linear="no" and must be
  reported as linear=False.</p>"""),
    }
    for name, (title, body) in pages.items():
        b.add("OEBPS/" + name, xhtml(title, body, css="style.css"))
    b.add("OEBPS/style.css", CSS_BASIC)
    b.add("OEBPS/images/cover.png", _assets.png_checkerboard(), compress=False)

    ncx = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN"
  "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd">
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head>
    <meta name="dtb:uid" content="%s"/>
    <meta name="dtb:depth" content="3"/>
    <meta name="dtb:totalPageCount" content="0"/>
    <meta name="dtb:maxPageNumber" content="0"/>
  </head>
  <docTitle><text>EPUB 2 With NCX</text></docTitle>
  <navMap>
    <navPoint id="np-cover" playOrder="1">
      <navLabel><text>Front Cover</text></navLabel>
      <content src="cover.xhtml"/>
    </navPoint>
    <navPoint id="np-ch1" playOrder="2">
      <navLabel><text>Chapter One</text></navLabel>
      <content src="ch1.xhtml"/>
      <navPoint id="np-ch1s1" playOrder="3">
        <navLabel><text>Section 1.1</text></navLabel>
        <content src="ch1s1.xhtml"/>
        <navPoint id="np-ch1s1a" playOrder="4">
          <navLabel><text>Subsection 1.1.1</text></navLabel>
          <content src="ch1s1a.xhtml#sub"/>
        </navPoint>
      </navPoint>
    </navPoint>
    <navPoint id="np-ch2" playOrder="5">
      <navLabel><text>Chapter Two</text></navLabel>
      <content src="ch2.xhtml"/>
    </navPoint>
  </navMap>
</ncx>
""" % EPUB2_ID
    b.add("OEBPS/toc.ncx", ncx)

    opf = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"
            xmlns:opf="http://www.idpf.org/2007/opf">
    <dc:title>EPUB 2 With NCX</dc:title>
    <dc:creator opf:role="aut" opf:file-as="Lovelace, Ada">Ada Lovelace</dc:creator>
    <dc:creator opf:role="aut" opf:file-as="Hopper, Grace">Grace Hopper</dc:creator>
    <dc:contributor opf:role="edt">Edith Editor</dc:contributor>
    <dc:publisher>Fixture Press</dc:publisher>
    <dc:date opf:event="publication">2001-01-01</dc:date>
    <dc:language>en</dc:language>
    <dc:identifier id="bookid" opf:scheme="UUID">%s</dc:identifier>
    <dc:description>A deliberately ordinary EPUB 2.0.1 used to pin down NCX parsing.</dc:description>
    <dc:rights>Public domain fixture.</dc:rights>
    <meta name="cover" content="cover-image"/>
  </metadata>
  <manifest>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>
    <item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="ch1s1" href="ch1s1.xhtml" media-type="application/xhtml+xml"/>
    <item id="ch1s1a" href="ch1s1a.xhtml" media-type="application/xhtml+xml"/>
    <item id="ch2" href="ch2.xhtml" media-type="application/xhtml+xml"/>
    <item id="notes" href="notes.xhtml" media-type="application/xhtml+xml"/>
    <item id="css" href="style.css" media-type="text/css"/>
    <item id="cover-image" href="images/cover.png" media-type="image/png"/>
  </manifest>
  <spine toc="ncx">
    <itemref idref="cover"/>
    <itemref idref="ch1"/>
    <itemref idref="ch1s1"/>
    <itemref idref="ch1s1a"/>
    <itemref idref="ch2"/>
    <itemref idref="notes" linear="no"/>
  </spine>
  <guide>
    <reference type="cover" href="cover.xhtml" title="Cover"/>
    <reference type="text" href="ch1.xhtml" title="Beginning"/>
  </guide>
</package>
""" % EPUB2_ID
    b.add("OEBPS/content.opf", opf)
    return b


# --------------------------------------------------------------------------
# 2. epub3_nav.epub
# --------------------------------------------------------------------------

EPUB3_ID = "urn:uuid:0000e3na-0000-4000-8000-0000000000n3"


def build_epub3_nav() -> EpubBuilder:
    b = EpubBuilder("epub3_nav.epub")
    b.container("EPUB/package.opf")

    pages = {
        "intro.xhtml": ("Introduction", """\
  <h1>Introduction</h1>
  <p>FIXTURE: <strong>epub3_nav.epub</strong> &#8212; EPUB 3 with an XHTML nav document.</p>
  <p>NAV-TOP-LEVEL-1.</p>
  <style type="text/css">
    /* STYLE-MUST-NOT-BE-SEARCHABLE */
    .decoy { color: red; }
  </style>
  <script type="text/javascript">
    /* SCRIPT-MUST-NOT-BE-SEARCHABLE */
    var decoy = "SCRIPT-MUST-NOT-BE-SEARCHABLE";
  </script>
  <p>Text after the script block: AFTER-SCRIPT-SENTINEL.</p>
  <!-- COMMENT-MUST-NOT-BE-SEARCHABLE -->
  <p>Entities: caf&#233; &amp; cr&#232;me, 3 &lt; 5, &#8220;quoted&#8221;.</p>"""),
        "part1.xhtml": ("Part One", """\
  <h1>Part One</h1>
  <p>NAV-TOP-LEVEL-2. Two children below.</p>"""),
        "alpha.xhtml": ("Chapter Alpha", """\
  <h1>Chapter Alpha</h1>
  <p>NAV-DEPTH-2-A.</p>"""),
        "beta.xhtml": ("Chapter Beta", """\
  <h1>Chapter Beta</h1>
  <p>NAV-DEPTH-2-B.</p>
  <h2 id="bsub">Beta Subsection</h2>
  <p>NAV-DEPTH-3 marker, addressed by fragment.</p>"""),
        "colophon.xhtml": ("Colophon", """\
  <h1>Colophon</h1>
  <p>NAV-TOP-LEVEL-3. Set in nothing in particular.</p>"""),
    }
    for name, (title, body) in pages.items():
        b.add("EPUB/text/" + name, xhtml(title, body, css="../css/main.css"))
    b.add("EPUB/css/main.css", CSS_BASIC)
    b.add("EPUB/images/cover.png", _assets.png_checkerboard(96, 128, 16), compress=False)

    nav_body = """\
  <h1>Table of Contents</h1>
  <nav epub:type="toc" id="toc" role="doc-toc">
    <h2>Contents</h2>
    <ol>
      <li><a href="text/intro.xhtml">Introduction</a></li>
      <li><a href="text/part1.xhtml">Part One</a>
        <ol>
          <li><a href="text/alpha.xhtml">Chapter Alpha</a></li>
          <li><a href="text/beta.xhtml">Chapter Beta</a>
            <ol>
              <li><a href="text/beta.xhtml#bsub">Beta Subsection</a></li>
            </ol>
          </li>
        </ol>
      </li>
      <li><a href="text/colophon.xhtml">Colophon</a></li>
    </ol>
  </nav>
  <nav epub:type="landmarks" hidden="hidden">
    <h2>Landmarks</h2>
    <ol>
      <li><a epub:type="cover" href="text/intro.xhtml">Cover</a></li>
      <li><a epub:type="toc" href="nav.xhtml#toc">Table of Contents</a></li>
      <li><a epub:type="bodymatter" href="text/part1.xhtml">Start of Content</a></li>
    </ol>
  </nav>
  <nav epub:type="page-list" hidden="hidden">
    <ol>
      <li><a href="text/intro.xhtml">1</a></li>
      <li><a href="text/part1.xhtml">2</a></li>
    </ol>
  </nav>"""
    b.add("EPUB/nav.xhtml", xhtml("Table of Contents", nav_body, css="css/main.css"))

    opf = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" xml:lang="en"
         unique-identifier="pub-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="pub-id">%s</dc:identifier>
    <dc:title id="t1">EPUB 3 With Nav Document</dc:title>
    <meta refines="#t1" property="title-type">main</meta>
    <dc:creator id="c1">Alan Turing</dc:creator>
    <meta refines="#c1" property="role" scheme="marc:relators">aut</meta>
    <meta refines="#c1" property="file-as">Turing, Alan</meta>
    <dc:creator id="c2">Barbara Liskov</dc:creator>
    <meta refines="#c2" property="role" scheme="marc:relators">aut</meta>
    <dc:contributor id="c3">Carl Contributor</dc:contributor>
    <meta refines="#c3" property="role" scheme="marc:relators">edt</meta>
    <dc:language>en-GB</dc:language>
    <dc:publisher>Nav Documents Ltd</dc:publisher>
    <dc:date>2019-07-04</dc:date>
    <dc:description>EPUB 3 fixture exercising nested nav, landmarks and cover-image.</dc:description>
    <meta property="dcterms:modified">2019-07-04T00:00:00Z</meta>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="cover-img" href="images/cover.png" media-type="image/png" properties="cover-image"/>
    <item id="intro" href="text/intro.xhtml" media-type="application/xhtml+xml"/>
    <item id="part1" href="text/part1.xhtml" media-type="application/xhtml+xml"/>
    <item id="alpha" href="text/alpha.xhtml" media-type="application/xhtml+xml"/>
    <item id="beta" href="text/beta.xhtml" media-type="application/xhtml+xml"/>
    <item id="colophon" href="text/colophon.xhtml" media-type="application/xhtml+xml"/>
    <item id="css" href="css/main.css" media-type="text/css"/>
  </manifest>
  <spine>
    <itemref idref="intro"/>
    <itemref idref="part1"/>
    <itemref idref="alpha"/>
    <itemref idref="beta"/>
    <itemref idref="colophon"/>
  </spine>
</package>
""" % EPUB3_ID
    b.add("EPUB/package.opf", opf)
    return b


# --------------------------------------------------------------------------
# 3. cjk_vertical.epub
# --------------------------------------------------------------------------

CJK_ID = "urn:uuid:0000cjkv-0000-4000-8000-0000000000zh"

CSS_CJK = """\
@charset "utf-8";
body { font-family: "Noto Serif CJK SC", "Source Han Serif SC", "SimSun", serif;
       line-height: 1.8; margin: 1em; }
h1 { font-size: 1.5em; }
.vertical { writing-mode: vertical-rl; -webkit-writing-mode: vertical-rl;
            -epub-writing-mode: vertical-rl; text-orientation: upright;
            height: 30em; }
.vertical p { text-indent: 2em; }
ruby rt { font-size: 0.5em; }
"""


def build_cjk_vertical() -> EpubBuilder:
    b = EpubBuilder("cjk_vertical.epub")
    b.container("OPS/content.opf")
    b.add("OPS/css/cjk.css", CSS_CJK)

    horiz = """\
  <h1>第一章　横排正文</h1>
  <p>这一章使用普通的横排方式，用来和后面的竖排章节作对比。如果两章看起来完全一样，
  那么竖排样式就没有生效。</p>
  <p>标点符号测试：逗号，句号。顿号、分号；冒号：问号？感叹号！省略号……破折号——
  引号“双引号”和‘单引号’，书名号《红楼梦》，以及直角引号「这样」和『这样』。</p>
  <p>中英混排：Chromium 在渲染 CJK 文本时需要正确处理断行，例如 QWebEngineView
  与「网页视图」这两个词之间不应出现多余的空格。</p>
  <p>CJK-HORIZONTAL-SENTINEL 横排标记</p>"""

    vert = """\
  <h1>第二章　竖排正文</h1>
  <div class="vertical">
    <p>此段落使用 writing-mode: vertical-rl，文字应当自上而下、自右向左排列。</p>
    <p>若阅读器注入的主题样式覆盖了 writing-mode，这一段就会退回横排，
    这正是本夹具要暴露的问题。</p>
    <p>竖排时标点的位置也不同：句号。逗号，以及省略号……都应当出现在字格的正确位置。</p>
    <p>CJK-VERTICAL-SENTINEL 竖排标记</p>
  </div>"""

    punct = """\
  <h1>第三章　标点与注音</h1>
  <p>全角与半角混排：ＡＢＣ 对 ABC，１２３ 对 123。</p>
  <p>注音：<ruby>漢<rt>hàn</rt></ruby><ruby>字<rt>zì</rt></ruby>。</p>
  <p>繁體字測試：這是一段繁體中文，用來檢查字型回退是否正常。</p>
  <p>日本語も混ぜる：ひらがな、カタカナ、漢字。</p>
  <p>한국어도 섞는다: 한글 텍스트.</p>
  <p>CJK-PUNCT-SENTINEL 标点标记</p>"""

    for name, (title, body) in {
        "ch1.xhtml": ("第一章　横排正文", horiz),
        "ch2.xhtml": ("第二章　竖排正文", vert),
        "ch3.xhtml": ("第三章　标点与注音", punct),
    }.items():
        b.add("OPS/text/" + name, xhtml(title, body, lang="zh-Hans", css="../css/cjk.css"))

    nav_body = """\
  <h1>目录</h1>
  <nav epub:type="toc">
    <ol>
      <li><a href="text/ch1.xhtml">第一章　横排正文</a>
        <ol><li><a href="text/ch1.xhtml">标点符号测试</a></li></ol>
      </li>
      <li><a href="text/ch2.xhtml">第二章　竖排正文（直排）</a></li>
      <li><a href="text/ch3.xhtml">第三章　標點與注音</a></li>
    </ol>
  </nav>"""
    b.add("OPS/nav.xhtml", xhtml("目录", nav_body, lang="zh-Hans", css="css/cjk.css"))

    opf = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" xml:lang="zh-Hans"
         unique-identifier="pub-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="pub-id">%s</dc:identifier>
    <dc:title>中文竖排测试书</dc:title>
    <dc:creator id="c1">鲁迅</dc:creator>
    <meta refines="#c1" property="file-as">周树人</meta>
    <dc:creator id="c2">曹雪芹</dc:creator>
    <dc:language>zh-Hans</dc:language>
    <dc:publisher>测试出版社</dc:publisher>
    <dc:date>2020-10-01</dc:date>
    <dc:description>用于检验 CJK 排版、竖排与标点渲染的夹具。</dc:description>
    <meta property="dcterms:modified">2020-10-01T00:00:00Z</meta>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="ch1" href="text/ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="ch2" href="text/ch2.xhtml" media-type="application/xhtml+xml"/>
    <item id="ch3" href="text/ch3.xhtml" media-type="application/xhtml+xml"/>
    <item id="css" href="css/cjk.css" media-type="text/css"/>
  </manifest>
  <spine page-progression-direction="rtl">
    <itemref idref="ch1"/>
    <itemref idref="ch2"/>
    <itemref idref="ch3"/>
  </spine>
</package>
""" % CJK_ID
    b.add("OPS/content.opf", opf)
    return b


# --------------------------------------------------------------------------
# 4. images_fonts.epub
# --------------------------------------------------------------------------

IMG_ID = "urn:uuid:0000imgf-0000-4000-8000-000000000img"

CSS_FONTS = """\
@font-face {
  font-family: "EpubReaderTest";
  /* Resolved relative to THIS css file, not to the xhtml that links it. */
  src: url("../fonts/TestFont.woff") format("woff"),
       url("../fonts/TestFont.ttf") format("truetype");
  font-weight: normal;
  font-style: normal;
}
body { margin: 1em; font-family: serif; }
.boxed { font-family: "EpubReaderTest", monospace; font-size: 28px; }
.bg {
  height: 80px;
  background-image: url("../images/check.png");
  background-repeat: repeat-x;
  border: 1px solid #333;
}
"""


def build_images_fonts() -> EpubBuilder:
    b = EpubBuilder("images_fonts.epub")
    b.container("OEBPS/content.opf")

    ch1 = """\
  <h1>Images and Fonts</h1>
  <p>FIXTURE: <strong>images_fonts.epub</strong>. This file lives in
  <code>OEBPS/text/</code>.</p>
  <p class="boxed">FONT TEST: every letter here must be a solid box.</p>
  <p>One up, one down (JPEG):<br/><img src="../images/photo.jpg" alt="jpeg gradient"/></p>
  <p>One up, one down (PNG):<br/><img src="../images/check.png" alt="png checkerboard"/></p>
  <p>One up, one down (SVG):<br/><img src="../images/cover.svg" alt="svg cover" width="120"/></p>
  <p>CSS background image, path relative to the CSS file:</p>
  <div class="bg"></div>
  <p>Down one directory: <a href="sub/deep.xhtml">deep.xhtml</a></p>
  <p>IMAGES-CH1-SENTINEL</p>"""
    b.add("OEBPS/text/chapter1.xhtml",
          xhtml("Images and Fonts", ch1, css="../styles/main.css"))

    deep = """\
  <h1>Two Directories Down</h1>
  <p>This file lives in <code>OEBPS/text/sub/</code>, so every asset reference
  climbs two levels.</p>
  <p class="boxed">FONT TEST FROM A DEEPER DIRECTORY</p>
  <p><img src="../../images/check.png" alt="png two up"/></p>
  <p><img src="../../images/photo.jpg" alt="jpeg two up"/></p>
  <p>Back up: <a href="../chapter1.xhtml">chapter1.xhtml</a></p>
  <p>IMAGES-DEEP-SENTINEL</p>"""
    b.add("OEBPS/text/sub/deep.xhtml",
          xhtml("Two Directories Down", deep, css="../../styles/main.css"))

    b.add("OEBPS/styles/main.css", CSS_FONTS)
    b.add("OEBPS/images/photo.jpg", _assets.jpeg_gradient(128, 64), compress=False)
    b.add("OEBPS/images/check.png", _assets.png_checkerboard(64, 64, 8), compress=False)
    b.add("OEBPS/images/cover.svg", _assets.svg_cover("SVG COVER"))
    ttf = _assets.truetype_font()
    b.add("OEBPS/fonts/TestFont.ttf", ttf, compress=False)
    b.add("OEBPS/fonts/TestFont.woff", _assets.woff_font(ttf), compress=False)

    nav_body = """\
  <h1>Contents</h1>
  <nav epub:type="toc">
    <ol>
      <li><a href="text/chapter1.xhtml">Images and Fonts</a></li>
      <li><a href="text/sub/deep.xhtml">Two Directories Down</a></li>
    </ol>
  </nav>"""
    b.add("OEBPS/nav.xhtml", xhtml("Contents", nav_body, css="styles/main.css"))

    opf = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="pub-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="pub-id">%s</dc:identifier>
    <dc:title>Images, Fonts and Relative Paths</dc:title>
    <dc:creator>Rel A. Tive</dc:creator>
    <dc:language>en</dc:language>
    <dc:publisher>Asset Resolution Press</dc:publisher>
    <dc:date>2022-02-22</dc:date>
    <dc:description>Every asset is referenced through a relative path that has to be resolved.</dc:description>
    <meta property="dcterms:modified">2022-02-22T00:00:00Z</meta>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="ch1" href="text/chapter1.xhtml" media-type="application/xhtml+xml"/>
    <item id="deep" href="text/sub/deep.xhtml" media-type="application/xhtml+xml"/>
    <item id="css" href="styles/main.css" media-type="text/css"/>
    <item id="jpg" href="images/photo.jpg" media-type="image/jpeg"/>
    <item id="png" href="images/check.png" media-type="image/png"/>
    <item id="svg" href="images/cover.svg" media-type="image/svg+xml" properties="cover-image"/>
    <item id="ttf" href="fonts/TestFont.ttf" media-type="application/font-sfnt"/>
    <item id="woff" href="fonts/TestFont.woff" media-type="application/font-woff"/>
  </manifest>
  <spine>
    <itemref idref="ch1"/>
    <itemref idref="deep"/>
  </spine>
</package>
""" % IMG_ID
    b.add("OEBPS/content.opf", opf)
    return b


# --------------------------------------------------------------------------
# 5. odd_paths.epub
# --------------------------------------------------------------------------

ODD_ID = "urn:uuid:0000oddp-0000-4000-8000-00000000path"

# zip entry names (the truth) vs. the hrefs used to reach them (percent-encoded,
# ../-laden, and in one case the wrong case entirely).
ODD_ENTRIES = {
    "space": "text/chapter one.xhtml",
    "cjk": "\u6587\u672c/\u7b2c\u4e00\u7ae0.xhtml",     # 文本/第一章.xhtml
    "hash": "text/a#b.xhtml",
    "mixed": "Text/MiXeD.xhtml",
    "image": "images/\u56fe\u7247 1.png",               # images/图片 1.png
}
ODD_HREFS = {
    "space": "text/chapter%20one.xhtml",
    "cjk": "%E6%96%87%E6%9C%AC/%E7%AC%AC%E4%B8%80%E7%AB%A0.xhtml",
    "hash": "text/a%23b.xhtml",
    "mixed": "text/mixed.xhtml",                        # case does NOT match
    "image": "images/%E5%9B%BE%E7%89%87%201.png",
}


def build_odd_paths() -> EpubBuilder:
    b = EpubBuilder("odd_paths.epub")
    b.container("content.opf")  # OPF at the zip root

    space_body = """\
  <h1>Chapter One (with a space in the filename)</h1>
  <p>FIXTURE: <strong>odd_paths.epub</strong>. This file's zip entry is
  <code>text/chapter one.xhtml</code> and every href to it is percent-encoded.</p>
  <p>Link to a Chinese filename:
  <a href="../%E6%96%87%E6%9C%AC/%E7%AC%AC%E4%B8%80%E7%AB%A0.xhtml#sec2">第一章 &#167;2</a></p>
  <p>Link to a filename containing a literal '#':
  <a href="a%23b.xhtml">a#b.xhtml</a></p>
  <p>Link with a redundant ../ segment:
  <a href="../text/./chapter%20one.xhtml#top">back to the top of this file</a></p>
  <p>Stylesheet is one level up: see the red border.</p>
  <p><img src="../images/%E5%9B%BE%E7%89%87%201.png" alt="image with a space and CJK"/></p>
  <p>ODD-SPACE-SENTINEL</p>"""
    b.add(ODD_ENTRIES["space"],
          xhtml("Chapter One", space_body, head='\n  <meta name="anchor" content="top"/>',
                css="../style.css", body_attrs=' id="top"'))

    cjk_body = """\
  <h1>第一章：中文文件名</h1>
  <p>本文件在 zip 中的条目名是 <code>文本/第一章.xhtml</code>，
  必须用 UTF-8 百分号编码才能引用。</p>
  <h2 id="sec2">第二节</h2>
  <p>ODD-CJK-SENTINEL 中文路径标记</p>"""
    b.add(ODD_ENTRIES["cjk"], xhtml("第一章", cjk_body, lang="zh-Hans", css="../style.css"))

    hash_body = """\
  <h1>A file with a # in its name</h1>
  <p>The zip entry is literally <code>text/a#b.xhtml</code>. An href of
  <code>text/a#b.xhtml</code> would be parsed as <code>text/a</code> plus the
  fragment <code>b.xhtml</code>, so it MUST be written <code>text/a%23b.xhtml</code>.</p>
  <p>ODD-HASH-SENTINEL</p>"""
    b.add(ODD_ENTRIES["hash"], xhtml("Hash Filename", hash_body, css="../style.css"))

    mixed_body = """\
  <h1>Mixed case entry</h1>
  <p>The zip entry is <code>Text/MiXeD.xhtml</code> but the OPF asks for
  <code>text/mixed.xhtml</code>. Real-world books do this; a case-insensitive
  fallback is required.</p>
  <p>ODD-MIXED-SENTINEL</p>"""
    b.add(ODD_ENTRIES["mixed"], xhtml("Mixed Case", mixed_body, css="../style.css"))

    b.add(ODD_ENTRIES["image"], _assets.png_checkerboard(48, 48, 6), compress=False)
    b.add("style.css", "body { border: 3px solid #c00; padding: 8px; }\n")

    nav_body = """\
  <h1>Contents</h1>
  <nav epub:type="toc">
    <ol>
      <li><a href="%(space)s">Chapter One (space)</a></li>
      <li><a href="%(cjk)s">第一章（中文路径）</a>
        <ol><li><a href="%(cjk)s#sec2">第二节</a></li></ol>
      </li>
      <li><a href="%(hash)s">Hash Filename</a></li>
      <li><a href="%(mixed)s">Mixed Case</a></li>
    </ol>
  </nav>""" % ODD_HREFS
    b.add("nav.xhtml", xhtml("Contents", nav_body, css="./style.css"))

    opf = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="pub-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="pub-id">%(id)s</dc:identifier>
    <dc:title>Awkward Paths &amp; Percent Encoding</dc:title>
    <dc:creator>Perce N. Tencoded</dc:creator>
    <dc:language>en</dc:language>
    <dc:publisher>Edge Case Books</dc:publisher>
    <dc:date>2023-03-03</dc:date>
    <dc:description>OPF at the zip root; hrefs with spaces, CJK, %%23 and ../ segments.</dc:description>
    <meta property="dcterms:modified">2023-03-03T00:00:00Z</meta>
  </metadata>
  <manifest>
    <item id="nav" href="./nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="space" href="%(space)s" media-type="application/xhtml+xml"/>
    <item id="cjk" href="%(cjk)s" media-type="application/xhtml+xml"/>
    <item id="hash" href="%(hash)s" media-type="application/xhtml+xml"/>
    <item id="mixed" href="%(mixed)s" media-type="application/xhtml+xml"/>
    <item id="img" href="%(image)s" media-type="image/png"/>
    <item id="css" href="text/../style.css" media-type="text/css"/>
  </manifest>
  <spine>
    <itemref idref="space"/>
    <itemref idref="cjk"/>
    <itemref idref="hash"/>
    <itemref idref="mixed"/>
  </spine>
</package>
""" % dict(ODD_HREFS, id=ODD_ID)
    b.add("content.opf", opf)
    return b


# --------------------------------------------------------------------------
# 6. no_toc.epub
# --------------------------------------------------------------------------

NOTOC_ID = "urn:uuid:0000noto-0000-4000-8000-0000000notoc"


def build_no_toc() -> EpubBuilder:
    b = EpubBuilder("no_toc.epub")
    b.container("OEBPS/content.opf")

    chapters = [
        ("chapter1.xhtml", "Alpha Chapter", "NOTOC-1. Spine position 0."),
        ("chapter2.xhtml", "Beta Chapter", "NOTOC-3. Manifest position 1 but spine position 2."),
        ("chapter3.xhtml", "Gamma Chapter", "NOTOC-2. Manifest position 2 but spine position 1."),
        ("chapter4.xhtml", "Delta Chapter", "NOTOC-4. Spine position 3."),
        ("chapter5.xhtml", "", "NOTOC-5. This file has an EMPTY <title>; the fallback "
                               "must still produce a usable label."),
    ]
    for name, title, text in chapters:
        heading = title or "Epsilon Chapter"
        body = "  <h1>%s</h1>\n  <p>%s</p>" % (heading, text)
        b.add("OEBPS/" + name, xhtml(title, body))

    opf = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"
            xmlns:opf="http://www.idpf.org/2007/opf">
    <dc:title>A Book With No Table Of Contents</dc:title>
    <dc:creator>Noel Navigation</dc:creator>
    <dc:language>en</dc:language>
    <dc:identifier id="bookid">%s</dc:identifier>
    <dc:publisher>Fallback House</dc:publisher>
    <dc:date>2005-05-05</dc:date>
    <dc:description>No NCX, no nav document. The reader must invent a TOC.</dc:description>
  </metadata>
  <manifest>
    <item id="c1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>
    <item id="c2" href="chapter2.xhtml" media-type="application/xhtml+xml"/>
    <item id="c3" href="chapter3.xhtml" media-type="application/xhtml+xml"/>
    <item id="c4" href="chapter4.xhtml" media-type="application/xhtml+xml"/>
    <item id="c5" href="chapter5.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine>
    <itemref idref="c1"/>
    <itemref idref="c3"/>
    <itemref idref="c2"/>
    <itemref idref="c4"/>
    <itemref idref="c5"/>
  </spine>
</package>
""" % NOTOC_ID
    b.add("OEBPS/content.opf", opf)
    return b


# --------------------------------------------------------------------------
# 7. fixed_layout.epub
# --------------------------------------------------------------------------

FXL_ID = "urn:uuid:0000fxl0-0000-4000-8000-00000000fxl0"

CSS_FXL = """\
html, body { margin: 0; padding: 0; width: 1200px; height: 1600px; overflow: hidden; }
.page { position: relative; width: 1200px; height: 1600px; background: #fdf6e3; }
.page h1 { position: absolute; top: 120px; left: 80px; font-size: 72px; margin: 0; }
.page p  { position: absolute; top: 320px; left: 80px; width: 1040px; font-size: 36px; }
.page img { position: absolute; bottom: 120px; left: 80px; width: 480px; }
"""

FXL_VIEWPORT = '\n  <meta name="viewport" content="width=1200, height=1600"/>'


def build_fixed_layout() -> EpubBuilder:
    b = EpubBuilder("fixed_layout.epub")
    b.container("OEBPS/content.opf")
    b.add("OEBPS/css/fxl.css", CSS_FXL)
    b.add("OEBPS/images/plate.png", _assets.png_checkerboard(120, 120, 15), compress=False)

    for i, side in enumerate(["left", "right", "left", "right"], start=1):
        body = """  <div class="page">
    <h1>Fixed Page %d</h1>
    <p>FIXTURE: <strong>fixed_layout.epub</strong>. rendition:layout is
    pre-paginated and the viewport is 1200x1600 CSS px. This page must be
    scaled to fit, never reflowed. Page-spread: %s.</p>
    <p style="top:640px">FXL-PAGE-%d-SENTINEL</p>
    <img src="../images/plate.png" alt="plate"/>
  </div>""" % (i, side, i)
        b.add("OEBPS/text/page%d.xhtml" % i,
              xhtml("Fixed Page %d" % i, body, head=FXL_VIEWPORT, css="../css/fxl.css"))

    nav_body = """\
  <h1>Contents</h1>
  <nav epub:type="toc">
    <ol>
      <li><a href="text/page1.xhtml">Fixed Page 1</a></li>
      <li><a href="text/page2.xhtml">Fixed Page 2</a></li>
      <li><a href="text/page3.xhtml">Fixed Page 3</a></li>
      <li><a href="text/page4.xhtml">Fixed Page 4</a></li>
    </ol>
  </nav>"""
    b.add("OEBPS/nav.xhtml", xhtml("Contents", nav_body))

    opf = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="pub-id"
         prefix="rendition: http://www.idpf.org/vocab/rendition/#">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="pub-id">%s</dc:identifier>
    <dc:title>A Fixed Layout Picture Book</dc:title>
    <dc:creator>Fix D. Layout</dc:creator>
    <dc:language>en</dc:language>
    <dc:publisher>Pre-Paginated Press</dc:publisher>
    <dc:date>2018-08-08</dc:date>
    <dc:description>Pre-paginated EPUB 3: pages must be scaled, not reflowed.</dc:description>
    <meta property="dcterms:modified">2018-08-08T00:00:00Z</meta>
    <meta property="rendition:layout">pre-paginated</meta>
    <meta property="rendition:orientation">auto</meta>
    <meta property="rendition:spread">landscape</meta>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="p1" href="text/page1.xhtml" media-type="application/xhtml+xml"/>
    <item id="p2" href="text/page2.xhtml" media-type="application/xhtml+xml"/>
    <item id="p3" href="text/page3.xhtml" media-type="application/xhtml+xml"/>
    <item id="p4" href="text/page4.xhtml" media-type="application/xhtml+xml"/>
    <item id="css" href="css/fxl.css" media-type="text/css"/>
    <item id="plate" href="images/plate.png" media-type="image/png"/>
  </manifest>
  <spine>
    <itemref idref="p1" properties="rendition:page-spread-left"/>
    <itemref idref="p2" properties="rendition:page-spread-right"/>
    <itemref idref="p3" properties="rendition:page-spread-left"/>
    <itemref idref="p4" properties="rendition:page-spread-right"/>
  </spine>
</package>
""" % FXL_ID
    b.add("OEBPS/content.opf", opf)
    return b


# --------------------------------------------------------------------------
# 8. big_book.epub
# --------------------------------------------------------------------------

BIG_ID = "urn:uuid:0000bigb-0000-4000-8000-0000000bigbk"
BIG_CHAPTERS = 150
BIG_PARAS = 22
BIG_WORDS_PER_PARA = 80  # tuned so the corpus lands at ~2.0 MB of uncompressed markup

# Deterministic filler vocabulary: no randomness anywhere.
_WORDS = (
    "alabaster bramble cistern dulcimer ember fathom girdle harrow inkwell "
    "jetsam kindling lantern marrow nettle obsidian parapet quarry rampart "
    "sextant thicket umber vellum wainscot xebec yarrow zephyr anvil brackish "
    "cairn dovetail escarpment furrow gantry hummock isthmus jackdaw kestrel "
    "lodestone mordant nacre oriel plinth quicklime reliquary sorrel tangent "
    "undertow vestibule whetstone xylem yoke ziggurat"
).split()

BIG_SENTINEL_CHAPTER = 97
BIG_SENTINEL = "ZYGOMORPHIC-SENTINEL-097"
BIG_CJK_CHAPTER = 42
BIG_CJK_SENTINEL = "\u72ec\u4e00\u65e0\u4e8c\u7684\u641c\u7d22\u6807\u8bb0"  # 独一无二的搜索标记


def _filler(seed: int, count: int) -> str:
    n = len(_WORDS)
    out = []
    for i in range(count):
        w = _WORDS[(seed * 31 + i * 17 + (i * i) % 7) % n]
        if i == 0:
            w = w.capitalize()
        out.append(w)
    return " ".join(out) + "."


def build_big_book() -> EpubBuilder:
    b = EpubBuilder("big_book.epub")
    b.container("OEBPS/content.opf")
    b.add("OEBPS/css/big.css", CSS_BASIC)

    manifest = []
    spine = []
    nav_items = []
    for n in range(1, BIG_CHAPTERS + 1):
        paras = []
        for p in range(BIG_PARAS):
            paras.append("  <p>" + _filler(n * 1000 + p, BIG_WORDS_PER_PARA) + "</p>")
        if n == BIG_SENTINEL_CHAPTER:
            paras.insert(3, "  <p>%s</p>" % BIG_SENTINEL)
        if n == BIG_CJK_CHAPTER:
            paras.insert(5, "  <p>%s</p>" % BIG_CJK_SENTINEL)
        title = "Chapter %d of %d" % (n, BIG_CHAPTERS)
        body = ('  <h1 id="c%d">%s</h1>\n' % (n, title)) + "\n".join(paras)
        href = "text/ch%03d.xhtml" % n
        b.add("OEBPS/" + href, xhtml(title, body, css="../css/big.css"))
        manifest.append(manifest_item("c%03d" % n, href, "application/xhtml+xml"))
        spine.append('    <itemref idref="c%03d"/>\n' % n)
        nav_items.append('      <li><a href="%s">%s</a></li>\n' % (href, title))

    nav_body = ("  <h1>Contents</h1>\n"
                '  <nav epub:type="toc">\n    <ol>\n'
                + "".join(nav_items)
                + "    </ol>\n  </nav>")
    b.add("OEBPS/nav.xhtml", xhtml("Contents", nav_body, css="css/big.css"))

    opf = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="pub-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="pub-id">%s</dc:identifier>
    <dc:title>A Very Long Book</dc:title>
    <dc:creator>Prolix Author</dc:creator>
    <dc:language>en</dc:language>
    <dc:publisher>Volume Press</dc:publisher>
    <dc:date>2015-11-11</dc:date>
    <dc:description>%d chapters of deterministic filler for performance testing.</dc:description>
    <meta property="dcterms:modified">2015-11-11T00:00:00Z</meta>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="css" href="css/big.css" media-type="text/css"/>
%s  </manifest>
  <spine>
%s  </spine>
</package>
""" % (BIG_ID, BIG_CHAPTERS, "".join(manifest), "".join(spine))
    b.add("OEBPS/content.opf", opf)
    return b


# --------------------------------------------------------------------------
# 9. broken_xml.epub
# --------------------------------------------------------------------------

BROKEN_ID = "urn:uuid:0000brkn-0000-4000-8000-0000000broke"

# NOTE: deliberately NOT well-formed. Do not "fix" these strings.
BROKEN_ONE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
  <meta charset="utf-8">
  <title>Broken Chapter One</title>
</head>
<body>
  <h1>Broken Chapter One
  <p>BROKEN-ONE-SENTINEL. The <h1> above is never closed.
  <p>Raw ampersand: Tom & Jerry & Co. Raw less-than in prose: 3 < 5.
  <p class=unquoted>This attribute value has no quotes.
  <br>
  <img src="../images/check.png" alt="unclosed img tag">
  <p><b>Mismatched <i>nesting</b> here</i>.
  <div>
    <span>Unclosed div and span follow.
</body>
</html>
"""

BROKEN_TWO = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><meta charset="utf-8"/><title>Broken Chapter Two</title></head>
<body>
  <h1>Broken Chapter Two</h1>
  <p>BROKEN-TWO-SENTINEL. HTML entities that are undefined in XML without a DTD:
  &nbsp; &mdash; &hellip; &copy; &eacute;</p>
  <p>A bare ampersand followed by a word: R&amp;D is fine but R&D is not.</p>
  <p>An unterminated entity: &#x41 and &notanentity;</p>
  <p>Stray markup: 5 &lt; 6 but also 5 < 6</p>
</body>
</html>
"""


def build_broken_xml() -> EpubBuilder:
    b = EpubBuilder("broken_xml.epub")
    b.container("OEBPS/content.opf")
    b.add("OEBPS/images/check.png", _assets.png_checkerboard(32, 32, 4), compress=False)
    b.add("OEBPS/text/broken1.xhtml", BROKEN_ONE)
    b.add("OEBPS/text/broken2.xhtml", BROKEN_TWO)
    ok = """\
  <h1>A Perfectly Fine Chapter</h1>
  <p>OK-CHAPTER-SENTINEL. This one is well-formed, so partial failure must not
  take the whole book down.</p>"""
    b.add("OEBPS/text/ok.xhtml", xhtml("A Perfectly Fine Chapter", ok))

    nav_body = """\
  <h1>Contents</h1>
  <nav epub:type="toc">
    <ol>
      <li><a href="text/ok.xhtml">A Perfectly Fine Chapter</a></li>
      <li><a href="text/broken1.xhtml">Broken Chapter One</a></li>
      <li><a href="text/broken2.xhtml">Broken Chapter Two</a></li>
    </ol>
  </nav>"""
    b.add("OEBPS/nav.xhtml", xhtml("Contents", nav_body))

    opf = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="pub-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="pub-id">%s</dc:identifier>
    <dc:title>Not Well Formed</dc:title>
    <dc:creator>Mal Formed</dc:creator>
    <dc:language>en</dc:language>
    <dc:publisher>Recovery Press</dc:publisher>
    <dc:date>2012-12-12</dc:date>
    <dc:description>Chapters that a strict XML parser will refuse.</dc:description>
    <meta property="dcterms:modified">2012-12-12T00:00:00Z</meta>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="ok" href="text/ok.xhtml" media-type="application/xhtml+xml"/>
    <item id="b1" href="text/broken1.xhtml" media-type="application/xhtml+xml"/>
    <item id="b2" href="text/broken2.xhtml" media-type="application/xhtml+xml"/>
    <item id="png" href="images/check.png" media-type="image/png"/>
  </manifest>
  <spine>
    <itemref idref="ok"/>
    <itemref idref="b1"/>
    <itemref idref="b2"/>
  </spine>
</package>
""" % BROKEN_ID
    b.add("OEBPS/content.opf", opf)
    return b


# --------------------------------------------------------------------------
# 10. drm_fake.epub
# --------------------------------------------------------------------------

DRM_ID = "urn:uuid:0000drmf-0000-4000-8000-00000000drm0"


def _scramble(data: bytes, key: bytes = b"not-really-aes") -> bytes:
    return bytes(byte ^ key[i % len(key)] for i, byte in enumerate(data))


def build_drm_fake() -> EpubBuilder:
    b = EpubBuilder("drm_fake.epub")
    b.container("OEBPS/content.opf")

    plain = xhtml("Encrypted Chapter", "  <h1>Encrypted Chapter</h1>\n"
                                       "  <p>DRM-SENTINEL: you should never see this text.</p>")
    b.add("OEBPS/text/ch1.xhtml", _scramble(plain.encode("utf-8")), compress=False)
    b.add("OEBPS/text/ch2.xhtml", _scramble(
        xhtml("Also Encrypted", "  <h1>Also Encrypted</h1>\n  <p>Nor this.</p>").encode("utf-8")),
        compress=False)

    encryption = """<?xml version="1.0" encoding="UTF-8"?>
<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"
            xmlns:enc="http://www.w3.org/2001/04/xmlenc#"
            xmlns:deenc="http://ns.adobe.com/digitaleditions/enc">
  <enc:EncryptedData>
    <enc:EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes128-cbc"/>
    <enc:CipherData>
      <enc:CipherReference URI="OEBPS/text/ch1.xhtml"/>
    </enc:CipherData>
    <deenc:Compression Method="8" OriginalLength="1024"/>
  </enc:EncryptedData>
  <enc:EncryptedData>
    <enc:EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes128-cbc"/>
    <enc:CipherData>
      <enc:CipherReference URI="OEBPS/text/ch2.xhtml"/>
    </enc:CipherData>
  </enc:EncryptedData>
</encryption>
"""
    b.add("META-INF/encryption.xml", encryption)
    b.add("META-INF/rights.xml",
          '<?xml version="1.0" encoding="UTF-8"?>\n'
          '<licenseRef xmlns="http://ns.adobe.com/adept">'
          "urn:uuid:fake-adept-license</licenseRef>\n")

    opf = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="pub-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="pub-id">%s</dc:identifier>
    <dc:title>A Locked Book</dc:title>
    <dc:creator>Rights Holder</dc:creator>
    <dc:language>en</dc:language>
    <dc:publisher>Walled Garden Media</dc:publisher>
    <dc:date>2016-06-06</dc:date>
    <dc:description>Carries META-INF/encryption.xml with real xmlenc AES URIs.</dc:description>
    <meta property="dcterms:modified">2016-06-06T00:00:00Z</meta>
  </metadata>
  <manifest>
    <item id="ch1" href="text/ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="ch2" href="text/ch2.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine>
    <itemref idref="ch1"/>
    <itemref idref="ch2"/>
  </spine>
</package>
""" % DRM_ID
    b.add("OEBPS/content.opf", opf)
    return b


# --------------------------------------------------------------------------
# 11. obfuscated_fonts.epub  (encryption.xml that is NOT DRM)
# --------------------------------------------------------------------------

OBF_ID = "urn:uuid:0000obfu-0000-4000-8000-00000000obfu"
IDPF_OBFUSCATION = "http://www.idpf.org/2008/embedding"


def _idpf_obfuscate(font: bytes, unique_id: str) -> bytes:
    """IDPF font obfuscation: XOR the first 1040 bytes with the repeated SHA-1
    of the whitespace-stripped unique identifier."""
    stripped = "".join(unique_id.split())
    key = hashlib.sha1(stripped.encode("utf-8")).digest()  # 20 bytes
    out = bytearray(font)
    for i in range(min(1040, len(out))):
        out[i] ^= key[i % 20]
    return bytes(out)


def build_obfuscated_fonts() -> EpubBuilder:
    b = EpubBuilder("obfuscated_fonts.epub")
    b.container("OEBPS/content.opf")

    ttf = _assets.truetype_font()
    b.add("OEBPS/fonts/Obfuscated.ttf", _idpf_obfuscate(ttf, OBF_ID), compress=False)
    b.add("OEBPS/fonts/Plain.ttf", ttf, compress=False)
    b.add("OEBPS/css/main.css", """\
@font-face { font-family: "ObfTest"; src: url("../fonts/Obfuscated.ttf") format("truetype"); }
@font-face { font-family: "PlainTest"; src: url("../fonts/Plain.ttf") format("truetype"); }
.obf { font-family: "ObfTest", monospace; font-size: 28px; }
.plain { font-family: "PlainTest", monospace; font-size: 28px; }
""")

    body = """\
  <h1>Obfuscated, Not Encrypted</h1>
  <p>FIXTURE: <strong>obfuscated_fonts.epub</strong>. This book has a
  META-INF/encryption.xml, but the algorithm is the IDPF font-obfuscation
  algorithm, not DRM. A reader that reports "this book is DRM protected" here
  is wrong: a large share of legitimate commercial EPUBs look exactly like this.</p>
  <p class="plain">PLAIN FONT: boxes.</p>
  <p class="obf">OBFUSCATED FONT: boxes only if de-obfuscated.</p>
  <p>OBFUSCATED-SENTINEL</p>"""
    b.add("OEBPS/text/ch1.xhtml", xhtml("Obfuscated, Not Encrypted", body, css="../css/main.css"))

    nav_body = """\
  <h1>Contents</h1>
  <nav epub:type="toc"><ol>
    <li><a href="text/ch1.xhtml">Obfuscated, Not Encrypted</a></li>
  </ol></nav>"""
    b.add("OEBPS/nav.xhtml", xhtml("Contents", nav_body))

    encryption = """<?xml version="1.0" encoding="UTF-8"?>
<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"
            xmlns:enc="http://www.w3.org/2001/04/xmlenc#">
  <enc:EncryptedData>
    <enc:EncryptionMethod Algorithm="%s"/>
    <enc:CipherData>
      <enc:CipherReference URI="OEBPS/fonts/Obfuscated.ttf"/>
    </enc:CipherData>
  </enc:EncryptedData>
</encryption>
""" % IDPF_OBFUSCATION
    b.add("META-INF/encryption.xml", encryption)

    opf = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="pub-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="pub-id">%s</dc:identifier>
    <dc:title>Obfuscated Fonts Are Not DRM</dc:title>
    <dc:creator>Ob F. Uscated</dc:creator>
    <dc:language>en</dc:language>
    <dc:publisher>Typeface Trust</dc:publisher>
    <dc:date>2021-01-21</dc:date>
    <dc:description>encryption.xml present, but only for IDPF font obfuscation.</dc:description>
    <meta property="dcterms:modified">2021-01-21T00:00:00Z</meta>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="ch1" href="text/ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="css" href="css/main.css" media-type="text/css"/>
    <item id="f1" href="fonts/Obfuscated.ttf" media-type="application/font-sfnt"/>
    <item id="f2" href="fonts/Plain.ttf" media-type="application/font-sfnt"/>
  </manifest>
  <spine><itemref idref="ch1"/></spine>
</package>
""" % OBF_ID
    b.add("OEBPS/content.opf", opf)
    return b


# --------------------------------------------------------------------------
# 12-16. the junk files
# --------------------------------------------------------------------------


def build_not_an_epub() -> EpubBuilder:
    b = EpubBuilder("not_an_epub.epub", mimetype=None)
    b.add("readme.txt", "This is an ordinary zip archive wearing an .epub extension.\n"
                        "There is no mimetype entry and no META-INF/container.xml.\n")
    b.add("data.json", '{"kind": "not an epub", "why": "no OCF structure at all"}\n')
    b.add("folder/notes.md", "# Notes\n\nNothing to see here.\n")
    return b


def build_no_container() -> EpubBuilder:
    b = EpubBuilder("no_container.epub")  # correct mimetype
    b.add("META-INF/metadata.xml", '<?xml version="1.0"?>\n<metadata/>\n')
    b.add("OEBPS/content.opf",
          '<?xml version="1.0" encoding="UTF-8"?>\n'
          '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
          'unique-identifier="pub-id">\n'
          '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
          '    <dc:identifier id="pub-id">urn:uuid:no-container</dc:identifier>\n'
          "    <dc:title>Unreachable</dc:title>\n"
          "    <dc:language>en</dc:language>\n"
          "  </metadata>\n"
          '  <manifest><item id="c1" href="ch1.xhtml" '
          'media-type="application/xhtml+xml"/></manifest>\n'
          '  <spine><itemref idref="c1"/></spine>\n'
          "</package>\n")
    b.add("OEBPS/ch1.xhtml", xhtml("Unreachable", "  <p>No container.xml points here.</p>"))
    return b


def write_junk_files(valid_epub_bytes: bytes) -> list[Path]:
    written = []

    p = FIXTURES / "truncated.epub"
    cut = int(len(valid_epub_bytes) * 0.55)
    p.write_bytes(valid_epub_bytes[:cut])
    written.append(p)

    p = FIXTURES / "not_a_zip.epub"
    p.write_bytes(
        "This file is plain UTF-8 text, not a zip archive.\n"
        "It exists so the reader is forced to fail cleanly on garbage input.\n"
        "\u4e2d\u6587\u4e5f\u6709\u3002\n".encode("utf-8")
    )
    written.append(p)

    p = FIXTURES / "empty.epub"
    p.write_bytes(b"")
    written.append(p)

    return written


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------

BUILDERS = [
    build_epub2_ncx,
    build_epub3_nav,
    build_cjk_vertical,
    build_images_fonts,
    build_odd_paths,
    build_no_toc,
    build_fixed_layout,
    build_big_book,
    build_broken_xml,
    build_drm_fake,
    build_obfuscated_fonts,
    build_not_an_epub,
    build_no_container,
]


def generate(clean: bool = False) -> list[Path]:
    if clean and FIXTURES.exists():
        for f in FIXTURES.iterdir():
            if f.is_file():
                f.unlink()
    FIXTURES.mkdir(parents=True, exist_ok=True)

    paths = []
    valid_bytes = None
    for fn in BUILDERS:
        builder = fn()
        data = builder.to_bytes()
        if builder.filename == "epub2_ncx.epub":
            valid_bytes = data
        path = FIXTURES / builder.filename
        path.write_bytes(data)
        paths.append(path)
    paths.extend(write_junk_files(valid_bytes))
    return paths


def ensure_fixtures() -> Path:
    """Generate the corpus if any fixture is missing. Used by the test suite."""
    missing = [n for n, _ in INVENTORY if not (FIXTURES / n).exists()]
    if missing:
        generate()
    return FIXTURES


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------

EXPECT_BROKEN = {"truncated.epub", "not_a_zip.epub", "empty.epub"}
NO_OCF = {"not_an_epub.epub", "no_container.epub"} | EXPECT_BROKEN


def verify() -> int:
    """Re-open everything with zipfile and assert the structural invariants."""
    problems = []
    for name, _ in INVENTORY:
        path = FIXTURES / name
        if not path.exists():
            problems.append("%s: missing" % name)
            continue
        if name in EXPECT_BROKEN:
            if zipfile.is_zipfile(path):
                problems.append("%s: expected to be an unreadable zip, but it opens" % name)
            continue
        if not zipfile.is_zipfile(path):
            problems.append("%s: not a readable zip" % name)
            continue
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
            if bad:
                problems.append("%s: corrupt entry %s" % (name, bad))
            names = zf.namelist()
            if name not in NO_OCF:
                if names[0] != "mimetype":
                    problems.append("%s: first entry is %r, not 'mimetype'" % (name, names[0]))
                elif zf.getinfo("mimetype").compress_type != zipfile.ZIP_STORED:
                    problems.append("%s: mimetype is compressed" % name)
                elif zf.read("mimetype") != b"application/epub+zip":
                    problems.append("%s: wrong mimetype content" % name)
                if "META-INF/container.xml" not in names:
                    problems.append("%s: no META-INF/container.xml" % name)
                else:
                    import xml.etree.ElementTree as ET

                    root = ET.fromstring(zf.read("META-INF/container.xml"))
                    rf = root.find(".//{%s}rootfile" % OCF_NS)
                    if rf is None:
                        problems.append("%s: container.xml has no rootfile" % name)
                    else:
                        opf = rf.get("full-path")
                        if opf not in names:
                            problems.append("%s: OPF %r not in zip" % (name, opf))
                        else:
                            try:
                                ET.fromstring(zf.read(opf))
                            except ET.ParseError as exc:
                                problems.append("%s: OPF not well-formed: %s" % (name, exc))

    # determinism: rebuilding must give identical bytes
    for fn in BUILDERS:
        b1 = fn().to_bytes()
        b2 = fn().to_bytes()
        if b1 != b2:
            problems.append("%s: build is not deterministic" % fn().filename)
        on_disk = (FIXTURES / fn().filename).read_bytes()
        if on_disk != b1:
            problems.append("%s: on-disk bytes differ from a fresh build" % fn().filename)

    # big_book really is ~2MB of text
    with zipfile.ZipFile(FIXTURES / "big_book.epub") as zf:
        total = sum(i.file_size for i in zf.infolist() if i.filename.endswith(".xhtml"))
    if not (1_800_000 <= total <= 2_400_000):
        problems.append("big_book.epub: %d bytes of xhtml, expected ~2MB" % total)

    for p in problems:
        print("  FAIL " + p)
    if not problems:
        print("  all structural checks passed")
    return len(problems)


def print_inventory() -> None:
    width = max(len(n) for n, _ in INVENTORY)
    total = 0
    for name, what in INVENTORY:
        path = FIXTURES / name
        size = path.stat().st_size if path.exists() else -1
        total += max(size, 0)
        size_s = "%8s" % ("%d B" % size if size >= 0 else "missing")
        print("  %-*s  %s  %s" % (width, name, size_s, what))
    print("  %-*s  %8d B total" % (width, "", total))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="print the inventory and exit")
    ap.add_argument("--verify", action="store_true", help="verify fixtures after building")
    ap.add_argument("--clean", action="store_true", help="delete fixtures first")
    args = ap.parse_args(argv)

    if args.list:
        print_inventory()
        return 0

    paths = generate(clean=args.clean)
    print("wrote %d fixtures to %s" % (len(paths), FIXTURES))
    print_inventory()
    if args.verify:
        print("verifying...")
        return 1 if verify() else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
