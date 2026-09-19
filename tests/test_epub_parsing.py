#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test suite for the epub-reader parsing layer (`epublib`).

stdlib `unittest` only - no pytest. Run it with::

    powershell -ExecutionPolicy Bypass -File C:\\Users\\mengz\\epub-reader\\tests\\run_tests.ps1

or directly::

    C:\\Users\\mengz\\AppData\\Local\\Programs\\Python\\Python314\\python.exe ^
        -m unittest discover -s C:\\Users\\mengz\\epub-reader\\tests -v

`epublib` does not exist yet. It is imported lazily so that, until it does,
every test FAILS with an explanatory message instead of the whole module
blowing up with an ImportError at collection time.

THE CONTRACT UNDER TEST
-----------------------
    from epublib import EpubBook, EpubError

    book = EpubBook.open(path)     # EpubError(.kind) for drm / corrupt / not_epub
    book.metadata                  # dict: title, authors[list], language,
                                   #       identifier, publisher, date, description
    book.spine                     # [SpineItem(id, href, media_type, linear, zip_name)]
    book.toc                       # [TocEntry(title, href, fragment, children)]
    book.cover                     # zip entry name, or None
    book.read(zip_name)            # -> bytes
    book.resolve(href, base_zip_name)   # -> (zip_name, fragment)
    book.plain_text(zip_name)      # -> str, tags stripped, for search
    book.is_fixed_layout           # -> bool

Conventions this suite pins down, because they are the ones that silently go
wrong:
  * `zip_name` is the EXACT name of the entry inside the zip archive - already
    percent-DECODED, already normalised for `..`, and it is what `read()` takes.
  * `resolve()` returns `(zip_name, fragment)` where `fragment` is None when the
    href has none, and never includes the leading '#'.
  * `TocEntry.href` is the href EXACTLY as written in the TOC document (NCX or
    nav.xhtml) with the fragment split off into `.fragment`, so it is still
    percent-encoded and still relative *to the TOC document*. `TocEntry.href`
    therefore never contains a '#'.
  * `metadata["authors"]` holds dc:creator only. dc:contributor is NOT an author.
  * `toc` reflects the epub:type="toc" nav (or the NCX navMap) - never the
    landmarks or page-list nav.
"""

from __future__ import annotations

import os
import sys
import time
import unittest
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
for p in (str(PROJECT_ROOT), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import make_fixtures  # noqa: E402

FIXTURES = make_fixtures.ensure_fixtures()


# ---------------------------------------------------------------------------
# lazy import of the module under test
# ---------------------------------------------------------------------------

_MOD = None
_IMPORT_ERROR: BaseException | None = None

_MISSING = (
    "epublib is not importable yet.\n"
    "  Expected: %s\\epublib.py (or an epublib/ package) exporting EpubBook and EpubError.\n"
    "  Import failed with: %r\n"
    "  This suite is written against the agreed interface; it will start passing\n"
    "  as soon as that module lands. Nothing is wrong with the fixtures."
) % (PROJECT_ROOT, "%r")


def _load():
    global _MOD, _IMPORT_ERROR
    if _MOD is None and _IMPORT_ERROR is None:
        try:
            import epublib  # noqa: F401
            _MOD = epublib
        except BaseException as exc:  # noqa: BLE001 - we want to report anything
            _IMPORT_ERROR = exc
    return _MOD


_BOOKS: dict[str, object] = {}


def flatten(entries, depth=0):
    """[(depth, title, href, fragment), ...] in document order."""
    out = []
    for e in entries:
        out.append((depth, e.title, e.href, e.fragment))
        out.extend(flatten(getattr(e, "children", []) or [], depth + 1))
    return out


def toc_depth(entries):
    if not entries:
        return 0
    return 1 + max(toc_depth(getattr(e, "children", []) or []) for e in entries)


class EpubTestCase(unittest.TestCase):
    """Base class: fails loudly (not errors) while epublib is missing."""

    def setUp(self):
        mod = _load()
        if mod is None:
            self.fail(_MISSING % (_IMPORT_ERROR,))
        for name in ("EpubBook", "EpubError"):
            if not hasattr(mod, name):
                self.fail(
                    "epublib imported but does not export %s. "
                    "Required public names: EpubBook, EpubError." % name
                )
        self.epublib = mod
        self.EpubBook = mod.EpubBook
        self.EpubError = mod.EpubError

    # -- helpers ----------------------------------------------------------

    def path(self, name: str) -> str:
        p = FIXTURES / name
        self.assertTrue(p.exists(), "fixture %s missing; run make_fixtures.py" % name)
        return str(p)

    def book(self, name: str):
        """Opened once per process; these objects must be read-only."""
        if name not in _BOOKS:
            _BOOKS[name] = self.EpubBook.open(self.path(name))
        return _BOOKS[name]

    def assertKind(self, exc, expected):
        self.assertTrue(
            hasattr(exc, "kind"),
            "EpubError must carry a .kind attribute; got %r" % (exc,),
        )
        if isinstance(expected, (set, tuple, list)):
            self.assertIn(exc.kind, expected, "wrong .kind for %s" % exc)
        else:
            self.assertEqual(exc.kind, expected, "wrong .kind for %s" % exc)

    def zip_names(self, fixture):
        with zipfile.ZipFile(self.path(fixture)) as z:
            return z.namelist()


# ===========================================================================
# 0. the corpus itself (runs green before epublib exists - a canary)
# ===========================================================================


class TestFixtureCorpus(unittest.TestCase):
    """Sanity checks on the generated corpus. Independent of epublib."""

    def test_every_fixture_exists(self):
        for name, _ in make_fixtures.INVENTORY:
            self.assertTrue((FIXTURES / name).exists(), "missing fixture: " + name)

    def test_expected_count(self):
        self.assertEqual(len(make_fixtures.INVENTORY), 16)

    def test_valid_fixtures_are_zips(self):
        for name, _ in make_fixtures.INVENTORY:
            if name in make_fixtures.EXPECT_BROKEN:
                continue
            self.assertTrue(
                zipfile.is_zipfile(FIXTURES / name), "%s is not a zip" % name
            )

    def test_broken_fixtures_are_not_zips(self):
        for name in make_fixtures.EXPECT_BROKEN:
            self.assertFalse(
                zipfile.is_zipfile(FIXTURES / name),
                "%s was supposed to be unreadable" % name,
            )

    def test_generation_is_deterministic(self):
        for builder in make_fixtures.BUILDERS:
            b = builder()
            self.assertEqual(
                b.to_bytes(),
                (FIXTURES / b.filename).read_bytes(),
                "%s on disk differs from a fresh build - regenerate" % b.filename,
            )

    def test_mimetype_is_first_and_stored(self):
        for name, _ in make_fixtures.INVENTORY:
            if name in make_fixtures.NO_OCF:
                continue
            with zipfile.ZipFile(FIXTURES / name) as z:
                self.assertEqual(z.namelist()[0], "mimetype", name)
                self.assertEqual(
                    z.getinfo("mimetype").compress_type, zipfile.ZIP_STORED, name
                )
                self.assertEqual(z.read("mimetype"), b"application/epub+zip", name)


# ===========================================================================
# 1. EPUB 2 + NCX
# ===========================================================================


class TestEpub2Ncx(EpubTestCase):
    FIXTURE = "epub2_ncx.epub"

    def test_opens(self):
        self.assertIsNotNone(self.book(self.FIXTURE))

    def test_metadata_title(self):
        self.assertEqual(self.book(self.FIXTURE).metadata["title"], "EPUB 2 With NCX")

    def test_metadata_multiple_authors(self):
        authors = self.book(self.FIXTURE).metadata["authors"]
        self.assertIsInstance(authors, list)
        self.assertEqual(authors, ["Ada Lovelace", "Grace Hopper"])

    def test_metadata_excludes_contributor_from_authors(self):
        self.assertNotIn("Edith Editor", self.book(self.FIXTURE).metadata["authors"])

    def test_metadata_language(self):
        self.assertEqual(self.book(self.FIXTURE).metadata["language"], "en")

    def test_metadata_identifier(self):
        self.assertEqual(
            self.book(self.FIXTURE).metadata["identifier"], make_fixtures.EPUB2_ID
        )

    def test_metadata_publisher(self):
        self.assertEqual(self.book(self.FIXTURE).metadata["publisher"], "Fixture Press")

    def test_metadata_date(self):
        self.assertIn("2001-01-01", str(self.book(self.FIXTURE).metadata["date"]))

    def test_metadata_description(self):
        self.assertIn("EPUB 2.0.1", self.book(self.FIXTURE).metadata["description"])

    def test_metadata_has_all_required_keys(self):
        md = self.book(self.FIXTURE).metadata
        for key in ("title", "authors", "language", "identifier",
                    "publisher", "date", "description"):
            self.assertIn(key, md, "metadata is missing %r" % key)

    def test_spine_length_and_order(self):
        spine = self.book(self.FIXTURE).spine
        self.assertEqual(
            [s.zip_name for s in spine],
            ["OEBPS/cover.xhtml", "OEBPS/ch1.xhtml", "OEBPS/ch1s1.xhtml",
             "OEBPS/ch1s1a.xhtml", "OEBPS/ch2.xhtml", "OEBPS/notes.xhtml"],
        )

    def test_spine_ids(self):
        self.assertEqual(
            [s.id for s in self.book(self.FIXTURE).spine],
            ["cover", "ch1", "ch1s1", "ch1s1a", "ch2", "notes"],
        )

    def test_spine_media_types(self):
        for s in self.book(self.FIXTURE).spine:
            self.assertEqual(s.media_type, "application/xhtml+xml")

    def test_spine_linear_flag(self):
        spine = self.book(self.FIXTURE).spine
        self.assertEqual([s.linear for s in spine[:5]], [True] * 5)
        self.assertFalse(spine[5].linear, "notes.xhtml is linear='no'")

    def test_toc_top_level(self):
        toc = self.book(self.FIXTURE).toc
        self.assertEqual(
            [e.title for e in toc], ["Front Cover", "Chapter One", "Chapter Two"]
        )

    def test_toc_nesting_depth_is_three(self):
        self.assertEqual(toc_depth(self.book(self.FIXTURE).toc), 3)

    def test_toc_nesting_shape(self):
        flat = flatten(self.book(self.FIXTURE).toc)
        self.assertEqual(
            [(d, t) for d, t, _, _ in flat],
            [(0, "Front Cover"),
             (0, "Chapter One"),
             (1, "Section 1.1"),
             (2, "Subsection 1.1.1"),
             (0, "Chapter Two")],
        )

    def test_toc_fragment_parsed(self):
        flat = flatten(self.book(self.FIXTURE).toc)
        entry = [f for f in flat if f[1] == "Subsection 1.1.1"][0]
        self.assertEqual(entry[3], "sub", "fragment must be 'sub', without the '#'")

    def test_toc_href_is_raw_and_relative_to_the_ncx(self):
        flat = flatten(self.book(self.FIXTURE).toc)
        by_title = {t: href for _, t, href, _ in flat}
        self.assertEqual(by_title["Chapter One"], "ch1.xhtml")
        self.assertEqual(
            by_title["Subsection 1.1.1"], "ch1s1a.xhtml",
            "the fragment belongs in .fragment, not in .href",
        )

    def test_no_toc_href_contains_a_fragment_marker(self):
        for _, title, href, _ in flatten(self.book(self.FIXTURE).toc):
            self.assertNotIn("#", href, "%r kept its fragment in .href" % title)

    def test_toc_hrefs_resolve_to_real_entries(self):
        book = self.book(self.FIXTURE)
        names = set(self.zip_names(self.FIXTURE))
        for _, title, href, _ in flatten(book.toc):
            zip_name, _frag = book.resolve(href, "OEBPS/toc.ncx")
            self.assertIn(zip_name, names, "TOC entry %r -> %r" % (title, zip_name))

    def test_cover_from_meta_name_cover(self):
        self.assertEqual(self.book(self.FIXTURE).cover, "OEBPS/images/cover.png")

    def test_read_returns_bytes(self):
        data = self.book(self.FIXTURE).read("OEBPS/ch1.xhtml")
        self.assertIsInstance(data, (bytes, bytearray))
        self.assertIn(b"NCX-TOP-LEVEL-2", bytes(data))

    def test_read_binary_entry(self):
        png = self.book(self.FIXTURE).read("OEBPS/images/cover.png")
        self.assertEqual(bytes(png[:8]), b"\x89PNG\r\n\x1a\n")

    def test_not_fixed_layout(self):
        self.assertFalse(self.book(self.FIXTURE).is_fixed_layout)


# ===========================================================================
# 2. EPUB 3 + nav document
# ===========================================================================


class TestEpub3Nav(EpubTestCase):
    FIXTURE = "epub3_nav.epub"

    def test_metadata_title(self):
        self.assertEqual(
            self.book(self.FIXTURE).metadata["title"], "EPUB 3 With Nav Document"
        )

    def test_metadata_two_authors_in_order(self):
        self.assertEqual(
            self.book(self.FIXTURE).metadata["authors"],
            ["Alan Turing", "Barbara Liskov"],
        )

    def test_metadata_contributor_not_an_author(self):
        self.assertNotIn("Carl Contributor", self.book(self.FIXTURE).metadata["authors"])

    def test_metadata_language_and_publisher(self):
        md = self.book(self.FIXTURE).metadata
        self.assertEqual(md["language"], "en-GB")
        self.assertEqual(md["publisher"], "Nav Documents Ltd")

    def test_metadata_identifier(self):
        self.assertEqual(
            self.book(self.FIXTURE).metadata["identifier"], make_fixtures.EPUB3_ID
        )

    def test_metadata_date(self):
        self.assertIn("2019-07-04", str(self.book(self.FIXTURE).metadata["date"]))

    def test_spine_order(self):
        self.assertEqual(
            [s.zip_name for s in self.book(self.FIXTURE).spine],
            ["EPUB/text/intro.xhtml", "EPUB/text/part1.xhtml", "EPUB/text/alpha.xhtml",
             "EPUB/text/beta.xhtml", "EPUB/text/colophon.xhtml"],
        )

    def test_nav_document_not_in_spine(self):
        names = [s.zip_name for s in self.book(self.FIXTURE).spine]
        self.assertNotIn("EPUB/nav.xhtml", names)

    def test_all_spine_items_linear(self):
        self.assertTrue(all(s.linear for s in self.book(self.FIXTURE).spine))

    def test_toc_top_level(self):
        self.assertEqual(
            [e.title for e in self.book(self.FIXTURE).toc],
            ["Introduction", "Part One", "Colophon"],
        )

    def test_toc_nested_ol_shape(self):
        self.assertEqual(
            [(d, t) for d, t, _, _ in flatten(self.book(self.FIXTURE).toc)],
            [(0, "Introduction"),
             (0, "Part One"),
             (1, "Chapter Alpha"),
             (1, "Chapter Beta"),
             (2, "Beta Subsection"),
             (0, "Colophon")],
        )

    def test_toc_depth(self):
        self.assertEqual(toc_depth(self.book(self.FIXTURE).toc), 3)

    def test_toc_fragment(self):
        entry = [f for f in flatten(self.book(self.FIXTURE).toc)
                 if f[1] == "Beta Subsection"][0]
        self.assertEqual(entry[3], "bsub")

    def test_toc_href_is_relative_to_the_nav_document(self):
        by_title = {t: href for _, t, href, _ in flatten(self.book(self.FIXTURE).toc)}
        self.assertEqual(by_title["Chapter Alpha"], "text/alpha.xhtml")
        self.assertEqual(by_title["Beta Subsection"], "text/beta.xhtml")

    def test_toc_hrefs_resolve_from_the_nav_document(self):
        book = self.book(self.FIXTURE)
        names = set(self.zip_names(self.FIXTURE))
        for _, title, href, _ in flatten(book.toc):
            z, _f = book.resolve(href, "EPUB/nav.xhtml")
            self.assertIn(z, names, "TOC %r -> %r" % (title, z))

    def test_toc_ignores_landmarks_nav(self):
        titles = [t for _, t, _, _ in flatten(self.book(self.FIXTURE).toc)]
        for bad in ("Cover", "Start of Content", "Table of Contents"):
            self.assertNotIn(
                bad, titles,
                "%r comes from the landmarks nav; toc must use epub:type='toc'" % bad,
            )

    def test_toc_ignores_page_list_nav(self):
        titles = [t for _, t, _, _ in flatten(self.book(self.FIXTURE).toc)]
        self.assertNotIn("1", titles, "'1' comes from the page-list nav")
        self.assertNotIn("2", titles, "'2' comes from the page-list nav")

    def test_cover_from_properties_cover_image(self):
        self.assertEqual(self.book(self.FIXTURE).cover, "EPUB/images/cover.png")

    def test_not_fixed_layout(self):
        self.assertFalse(self.book(self.FIXTURE).is_fixed_layout)


# ===========================================================================
# 3. CJK / vertical
# ===========================================================================


class TestCjkVertical(EpubTestCase):
    FIXTURE = "cjk_vertical.epub"

    def test_title_is_chinese(self):
        self.assertEqual(
            self.book(self.FIXTURE).metadata["title"], "\u4e2d\u6587\u7ad6\u6392\u6d4b\u8bd5\u4e66"
        )

    def test_authors_are_chinese(self):
        self.assertEqual(
            self.book(self.FIXTURE).metadata["authors"],
            ["\u9c81\u8fc5", "\u66f9\u96ea\u82b9"],
        )

    def test_publisher_is_chinese(self):
        self.assertEqual(
            self.book(self.FIXTURE).metadata["publisher"], "\u6d4b\u8bd5\u51fa\u7248\u793e"
        )

    def test_language_is_chinese(self):
        self.assertTrue(self.book(self.FIXTURE).metadata["language"].lower().startswith("zh"))

    def test_toc_titles_are_cjk(self):
        titles = [t for _, t, _, _ in flatten(self.book(self.FIXTURE).toc)]
        self.assertTrue(any(t.startswith("\u7b2c\u4e00\u7ae0") for t in titles), titles)
        self.assertTrue(any("\u7ad6\u6392" in t for t in titles), titles)
        for t in titles:
            self.assertNotIn("\\u", t, "TOC title looks like an escaped literal: %r" % t)

    def test_toc_is_nested(self):
        self.assertGreaterEqual(toc_depth(self.book(self.FIXTURE).toc), 2)

    def test_plain_text_preserves_cjk_punctuation(self):
        text = self.book(self.FIXTURE).plain_text("OPS/text/ch1.xhtml")
        for ch in "\u3002\uff0c\u3001\uff1b\uff1a\uff1f\uff01\u2026\u2014\u201c\u201d\u300c\u300d\u300a\u300b":
            self.assertIn(ch, text, "CJK punctuation %r was lost" % ch)

    def test_plain_text_contains_sentinel(self):
        text = self.book(self.FIXTURE).plain_text("OPS/text/ch2.xhtml")
        self.assertIn("CJK-VERTICAL-SENTINEL", text)

    def test_vertical_css_is_readable(self):
        css = self.book(self.FIXTURE).read("OPS/css/cjk.css").decode("utf-8")
        self.assertIn("vertical-rl", css)

    def test_spine_resolves_cjk_book(self):
        spine = self.book(self.FIXTURE).spine
        self.assertEqual(len(spine), 3)
        for s in spine:
            self.assertTrue(self.book(self.FIXTURE).read(s.zip_name))


# ===========================================================================
# 4. images, fonts, relative paths
# ===========================================================================


class TestImagesFonts(EpubTestCase):
    FIXTURE = "images_fonts.epub"

    def test_resolve_one_up_one_down(self):
        z, f = self.book(self.FIXTURE).resolve(
            "../images/photo.jpg", "OEBPS/text/chapter1.xhtml"
        )
        self.assertEqual(z, "OEBPS/images/photo.jpg")
        self.assertIsNone(f)

    def test_resolve_two_directories_up(self):
        z, _ = self.book(self.FIXTURE).resolve(
            "../../images/check.png", "OEBPS/text/sub/deep.xhtml"
        )
        self.assertEqual(z, "OEBPS/images/check.png")

    def test_resolve_one_directory_down(self):
        z, _ = self.book(self.FIXTURE).resolve(
            "sub/deep.xhtml", "OEBPS/text/chapter1.xhtml"
        )
        self.assertEqual(z, "OEBPS/text/sub/deep.xhtml")

    def test_resolve_font_relative_to_the_css_not_the_xhtml(self):
        """@font-face urls are relative to the stylesheet, one of the classic bugs."""
        z, _ = self.book(self.FIXTURE).resolve(
            "../fonts/TestFont.woff", "OEBPS/styles/main.css"
        )
        self.assertEqual(z, "OEBPS/fonts/TestFont.woff")

    def test_resolve_background_image_relative_to_css(self):
        z, _ = self.book(self.FIXTURE).resolve(
            "../images/check.png", "OEBPS/styles/main.css"
        )
        self.assertEqual(z, "OEBPS/images/check.png")

    def test_resolve_stylesheet_from_deep_xhtml(self):
        z, _ = self.book(self.FIXTURE).resolve(
            "../../styles/main.css", "OEBPS/text/sub/deep.xhtml"
        )
        self.assertEqual(z, "OEBPS/styles/main.css")

    def test_cover_is_the_svg(self):
        self.assertEqual(self.book(self.FIXTURE).cover, "OEBPS/images/cover.svg")

    def test_png_bytes(self):
        self.assertEqual(
            bytes(self.book(self.FIXTURE).read("OEBPS/images/check.png")[:8]),
            b"\x89PNG\r\n\x1a\n",
        )

    def test_jpeg_bytes(self):
        self.assertEqual(
            bytes(self.book(self.FIXTURE).read("OEBPS/images/photo.jpg")[:2]), b"\xff\xd8"
        )

    def test_svg_bytes(self):
        self.assertIn(b"<svg", bytes(self.book(self.FIXTURE).read("OEBPS/images/cover.svg")))

    def test_ttf_bytes(self):
        self.assertEqual(
            bytes(self.book(self.FIXTURE).read("OEBPS/fonts/TestFont.ttf")[:4]),
            b"\x00\x01\x00\x00",
        )

    def test_woff_bytes(self):
        self.assertEqual(
            bytes(self.book(self.FIXTURE).read("OEBPS/fonts/TestFont.woff")[:4]), b"wOFF"
        )

    def test_spine(self):
        self.assertEqual(
            [s.zip_name for s in self.book(self.FIXTURE).spine],
            ["OEBPS/text/chapter1.xhtml", "OEBPS/text/sub/deep.xhtml"],
        )


# ===========================================================================
# 5. awkward paths - the resolution algorithm
# ===========================================================================


class TestOddPaths(EpubTestCase):
    FIXTURE = "odd_paths.epub"
    OPF = "content.opf"

    def test_opf_at_zip_root(self):
        self.assertIn("content.opf", self.zip_names(self.FIXTURE))

    def test_spine_zip_names_are_decoded(self):
        self.assertEqual(
            [s.zip_name for s in self.book(self.FIXTURE).spine],
            [make_fixtures.ODD_ENTRIES["space"],
             make_fixtures.ODD_ENTRIES["cjk"],
             make_fixtures.ODD_ENTRIES["hash"],
             make_fixtures.ODD_ENTRIES["mixed"]],
        )

    def test_percent_encoded_space(self):
        z, f = self.book(self.FIXTURE).resolve("text/chapter%20one.xhtml", self.OPF)
        self.assertEqual(z, "text/chapter one.xhtml")
        self.assertIsNone(f)

    def test_percent_encoded_cjk(self):
        z, _ = self.book(self.FIXTURE).resolve(make_fixtures.ODD_HREFS["cjk"], self.OPF)
        self.assertEqual(z, make_fixtures.ODD_ENTRIES["cjk"])

    def test_percent_encoded_hash_is_not_a_fragment(self):
        z, f = self.book(self.FIXTURE).resolve("text/a%23b.xhtml", self.OPF)
        self.assertEqual(z, "text/a#b.xhtml")
        self.assertIsNone(f, "%23 is a literal '#' in the name, not a fragment marker")

    def test_dotdot_segments_are_normalised(self):
        z, _ = self.book(self.FIXTURE).resolve("text/../style.css", self.OPF)
        self.assertEqual(z, "style.css")

    def test_dot_and_dotdot_mixed(self):
        z, f = self.book(self.FIXTURE).resolve(
            "../text/./chapter%20one.xhtml#top", "text/chapter one.xhtml"
        )
        self.assertEqual(z, "text/chapter one.xhtml")
        self.assertEqual(f, "top")

    def test_leading_dot_slash(self):
        z, _ = self.book(self.FIXTURE).resolve("./nav.xhtml", self.OPF)
        self.assertEqual(z, "nav.xhtml")

    def test_case_insensitive_fallback(self):
        """The OPF says text/mixed.xhtml; the zip says Text/MiXeD.xhtml."""
        z, _ = self.book(self.FIXTURE).resolve("text/mixed.xhtml", self.OPF)
        self.assertEqual(z, "Text/MiXeD.xhtml")

    def test_fragment_is_split_off(self):
        z, f = self.book(self.FIXTURE).resolve(
            make_fixtures.ODD_HREFS["cjk"] + "#sec2", self.OPF
        )
        self.assertEqual(z, make_fixtures.ODD_ENTRIES["cjk"])
        self.assertEqual(f, "sec2")

    def test_fragment_only_href_keeps_the_base(self):
        z, f = self.book(self.FIXTURE).resolve("#top", "text/chapter one.xhtml")
        self.assertEqual(z, "text/chapter one.xhtml")
        self.assertEqual(f, "top")

    def test_relative_href_from_a_chapter(self):
        z, f = self.book(self.FIXTURE).resolve(
            "../" + make_fixtures.ODD_HREFS["cjk"] + "#sec2", "text/chapter one.xhtml"
        )
        self.assertEqual(z, make_fixtures.ODD_ENTRIES["cjk"])
        self.assertEqual(f, "sec2")

    def test_sibling_href_from_a_chapter(self):
        z, _ = self.book(self.FIXTURE).resolve("a%23b.xhtml", "text/chapter one.xhtml")
        self.assertEqual(z, "text/a#b.xhtml")

    def test_percent_encoded_image(self):
        z, _ = self.book(self.FIXTURE).resolve(
            make_fixtures.ODD_HREFS["image"], self.OPF
        )
        self.assertEqual(z, make_fixtures.ODD_ENTRIES["image"])

    def test_every_spine_item_is_readable(self):
        book = self.book(self.FIXTURE)
        for s in book.spine:
            self.assertTrue(book.read(s.zip_name), "empty read for " + s.zip_name)

    def test_toc_titles_are_decoded(self):
        titles = [t for _, t, _, _ in flatten(self.book(self.FIXTURE).toc)]
        self.assertIn("Chapter One (space)", titles)
        self.assertTrue(any("\u7b2c\u4e00\u7ae0" in t for t in titles), titles)

    def test_toc_hrefs_resolve(self):
        book = self.book(self.FIXTURE)
        names = set(self.zip_names(self.FIXTURE))
        for _, title, href, _ in flatten(book.toc):
            z, _f = book.resolve(href, "nav.xhtml")
            self.assertIn(z, names, "TOC %r -> %r not in zip" % (title, z))


# ===========================================================================
# 6. no TOC at all - the fallback
# ===========================================================================


class TestNoTocFallback(EpubTestCase):
    FIXTURE = "no_toc.epub"

    def test_book_really_has_no_toc_source(self):
        names = self.zip_names(self.FIXTURE)
        self.assertFalse([n for n in names if n.endswith(".ncx")])
        with zipfile.ZipFile(self.path(self.FIXTURE)) as z:
            opf = z.read("OEBPS/content.opf").decode("utf-8")
        self.assertNotIn('properties="nav"', opf)

    def test_toc_is_not_empty(self):
        self.assertTrue(
            self.book(self.FIXTURE).toc,
            "with no NCX and no nav, the TOC must be synthesised from the spine",
        )

    def test_toc_length_matches_spine(self):
        book = self.book(self.FIXTURE)
        self.assertEqual(len(book.toc), len(book.spine))

    def test_toc_is_flat(self):
        self.assertEqual(toc_depth(self.book(self.FIXTURE).toc), 1)

    def test_toc_follows_spine_order_not_manifest_order(self):
        titles = [e.title for e in self.book(self.FIXTURE).toc]
        self.assertEqual(
            titles[:4], ["Alpha Chapter", "Gamma Chapter", "Beta Chapter", "Delta Chapter"]
        )

    def test_toc_entry_for_empty_title_is_still_usable(self):
        last = self.book(self.FIXTURE).toc[-1]
        self.assertTrue(last.title and last.title.strip(),
                        "a chapter with an empty <title> still needs a label")

    def test_toc_hrefs_match_spine(self):
        """A synthesised TOC has no TOC document, so its hrefs are relative to
        the OPF - i.e. they are the manifest hrefs."""
        book = self.book(self.FIXTURE)
        for entry, item in zip(book.toc, book.spine):
            z, _ = book.resolve(entry.href, "OEBPS/content.opf")
            self.assertEqual(z, item.zip_name)

    def test_metadata_still_parsed(self):
        md = self.book(self.FIXTURE).metadata
        self.assertEqual(md["title"], "A Book With No Table Of Contents")
        self.assertEqual(md["authors"], ["Noel Navigation"])

    def test_no_cover(self):
        self.assertIsNone(self.book(self.FIXTURE).cover)


# ===========================================================================
# 7. fixed layout
# ===========================================================================


class TestFixedLayout(EpubTestCase):
    FIXTURE = "fixed_layout.epub"

    def test_is_fixed_layout(self):
        self.assertTrue(self.book(self.FIXTURE).is_fixed_layout)

    def test_reflowable_books_are_not_fixed_layout(self):
        for name in ("epub2_ncx.epub", "epub3_nav.epub", "cjk_vertical.epub",
                     "images_fonts.epub", "no_toc.epub"):
            self.assertFalse(self.book(name).is_fixed_layout, name)

    def test_spine(self):
        self.assertEqual(len(self.book(self.FIXTURE).spine), 4)

    def test_viewport_meta_is_present_in_content(self):
        html = self.book(self.FIXTURE).read("OEBPS/text/page1.xhtml").decode("utf-8")
        self.assertIn('name="viewport"', html)
        self.assertIn("width=1200", html)

    def test_toc(self):
        self.assertEqual(
            [e.title for e in self.book(self.FIXTURE).toc],
            ["Fixed Page 1", "Fixed Page 2", "Fixed Page 3", "Fixed Page 4"],
        )


# ===========================================================================
# 8. the big book - navigation, search, performance
# ===========================================================================


class TestBigBook(EpubTestCase):
    FIXTURE = "big_book.epub"
    OPEN_BUDGET_S = 5.0
    SCAN_BUDGET_S = 25.0

    def test_spine_has_150_chapters(self):
        self.assertEqual(len(self.book(self.FIXTURE).spine), 150)

    def test_toc_has_150_entries(self):
        self.assertEqual(len(self.book(self.FIXTURE).toc), 150)

    def test_toc_order(self):
        toc = self.book(self.FIXTURE).toc
        self.assertEqual(toc[0].title, "Chapter 1 of 150")
        self.assertEqual(toc[96].title, "Chapter 97 of 150")
        self.assertEqual(toc[-1].title, "Chapter 150 of 150")

    def test_spine_and_toc_line_up(self):
        book = self.book(self.FIXTURE)
        for entry, item in zip(book.toc, book.spine):
            z, _ = book.resolve(entry.href, "OEBPS/nav.xhtml")
            self.assertEqual(z, item.zip_name)

    def test_open_is_fast(self):
        start = time.perf_counter()
        self.EpubBook.open(self.path(self.FIXTURE))
        elapsed = time.perf_counter() - start
        self.assertLess(
            elapsed, self.OPEN_BUDGET_S,
            "opening a 150-chapter book took %.2fs; it must not eagerly parse "
            "every chapter" % elapsed,
        )

    def test_full_text_scan_finds_ascii_sentinel(self):
        book = self.book(self.FIXTURE)
        hits = [s.zip_name for s in book.spine
                if make_fixtures.BIG_SENTINEL in book.plain_text(s.zip_name)]
        self.assertEqual(hits, ["OEBPS/text/ch097.xhtml"])

    def test_full_text_scan_finds_cjk_sentinel(self):
        book = self.book(self.FIXTURE)
        hits = [s.zip_name for s in book.spine
                if make_fixtures.BIG_CJK_SENTINEL in book.plain_text(s.zip_name)]
        self.assertEqual(hits, ["OEBPS/text/ch042.xhtml"])

    def test_full_text_scan_is_within_budget(self):
        book = self.book(self.FIXTURE)
        start = time.perf_counter()
        total = sum(len(book.plain_text(s.zip_name)) for s in book.spine)
        elapsed = time.perf_counter() - start
        self.assertGreater(total, 1_500_000, "scan produced suspiciously little text")
        self.assertLess(
            elapsed, self.SCAN_BUDGET_S,
            "scanning ~2MB across 150 chapters took %.2fs" % elapsed,
        )

    def test_plain_text_has_no_markup(self):
        text = self.book(self.FIXTURE).plain_text("OEBPS/text/ch001.xhtml")
        for needle in ("<p>", "</p>", "<h1", "<html", "<body", "<?xml"):
            self.assertNotIn(needle, text, "markup leaked into plain_text: %r" % needle)


# ===========================================================================
# 9. malformed XML - the parser must recover
# ===========================================================================


class TestBrokenXml(EpubTestCase):
    FIXTURE = "broken_xml.epub"

    def test_book_opens_despite_broken_chapters(self):
        self.assertIsNotNone(self.book(self.FIXTURE))

    def test_fixture_really_is_malformed(self):
        import xml.etree.ElementTree as ET

        with zipfile.ZipFile(self.path(self.FIXTURE)) as z:
            for entry in ("OEBPS/text/broken1.xhtml", "OEBPS/text/broken2.xhtml"):
                with self.assertRaises(ET.ParseError, msg=entry + " should not parse"):
                    ET.fromstring(z.read(entry))

    def test_spine_intact(self):
        self.assertEqual(
            [s.zip_name for s in self.book(self.FIXTURE).spine],
            ["OEBPS/text/ok.xhtml", "OEBPS/text/broken1.xhtml", "OEBPS/text/broken2.xhtml"],
        )

    def test_good_chapter_unaffected(self):
        self.assertIn(
            "OK-CHAPTER-SENTINEL",
            self.book(self.FIXTURE).plain_text("OEBPS/text/ok.xhtml"),
        )

    def test_recovers_text_from_unclosed_tags(self):
        text = self.book(self.FIXTURE).plain_text("OEBPS/text/broken1.xhtml")
        self.assertIn("BROKEN-ONE-SENTINEL", text)
        self.assertIn("Unclosed div and span follow", text)

    def test_recovers_raw_ampersand(self):
        text = self.book(self.FIXTURE).plain_text("OEBPS/text/broken1.xhtml")
        self.assertIn("Tom & Jerry", text)

    def test_recovers_undefined_entities(self):
        text = self.book(self.FIXTURE).plain_text("OEBPS/text/broken2.xhtml")
        self.assertIn("BROKEN-TWO-SENTINEL", text)
        self.assertNotIn("&nbsp;", text, "&nbsp; must be decoded, not passed through")
        self.assertNotIn("&mdash;", text, "&mdash; must be decoded, not passed through")

    def test_no_markup_leaks_from_broken_chapter(self):
        text = self.book(self.FIXTURE).plain_text("OEBPS/text/broken1.xhtml")
        for needle in ("<p", "<div", "<img", "<br"):
            self.assertNotIn(needle, text, "markup leaked: %r" % needle)

    def test_raw_read_is_untouched(self):
        raw = self.book(self.FIXTURE).read("OEBPS/text/broken1.xhtml")
        self.assertIn(b"class=unquoted", bytes(raw))

    def test_toc_still_parsed(self):
        self.assertEqual(len(self.book(self.FIXTURE).toc), 3)


# ===========================================================================
# 10. DRM detection
# ===========================================================================


class TestDrmDetection(EpubTestCase):
    def test_encrypted_book_raises_with_kind_drm(self):
        with self.assertRaises(self.EpubError) as cm:
            self.EpubBook.open(self.path("drm_fake.epub"))
        self.assertKind(cm.exception, "drm")

    def test_drm_error_message_is_informative(self):
        with self.assertRaises(self.EpubError) as cm:
            self.EpubBook.open(self.path("drm_fake.epub"))
        msg = str(cm.exception)
        self.assertTrue(msg.strip(), "EpubError must have a human-readable message")
        self.assertNotIn("Traceback", msg)

    def test_drm_does_not_crash_the_process(self):
        for _ in range(3):
            try:
                self.EpubBook.open(self.path("drm_fake.epub"))
            except self.EpubError:
                pass

    def test_idpf_font_obfuscation_is_not_drm(self):
        """encryption.xml exists, but only for font obfuscation - a huge share of
        legitimate commercial EPUBs look like this and must still open."""
        try:
            book = self.EpubBook.open(self.path("obfuscated_fonts.epub"))
        except self.EpubError as exc:
            self.fail(
                "obfuscated_fonts.epub uses the IDPF font-obfuscation algorithm "
                "(%s), which is NOT DRM, but open() raised %s(kind=%r)"
                % (make_fixtures.IDPF_OBFUSCATION, type(exc).__name__,
                   getattr(exc, "kind", None))
            )
        self.assertEqual(book.metadata["title"], "Obfuscated Fonts Are Not DRM")
        self.assertEqual(len(book.spine), 1)

    def test_obfuscated_book_text_is_readable(self):
        book = self.EpubBook.open(self.path("obfuscated_fonts.epub"))
        self.assertIn("OBFUSCATED-SENTINEL", book.plain_text("OEBPS/text/ch1.xhtml"))


# ===========================================================================
# 11. corrupt / not-an-epub
# ===========================================================================


class TestErrorHandling(EpubTestCase):
    def test_epub_error_is_an_exception(self):
        self.assertTrue(issubclass(self.EpubError, Exception))

    def test_plain_zip_is_not_an_epub(self):
        with self.assertRaises(self.EpubError) as cm:
            self.EpubBook.open(self.path("not_an_epub.epub"))
        self.assertKind(cm.exception, "not_epub")

    def test_missing_container_xml_is_not_an_epub(self):
        with self.assertRaises(self.EpubError) as cm:
            self.EpubBook.open(self.path("no_container.epub"))
        self.assertKind(cm.exception, "not_epub")

    def test_truncated_zip_is_corrupt(self):
        with self.assertRaises(self.EpubError) as cm:
            self.EpubBook.open(self.path("truncated.epub"))
        self.assertKind(cm.exception, "corrupt")

    def test_plain_text_file_is_corrupt(self):
        with self.assertRaises(self.EpubError) as cm:
            self.EpubBook.open(self.path("not_a_zip.epub"))
        self.assertKind(cm.exception, "corrupt")

    def test_empty_file_is_corrupt(self):
        with self.assertRaises(self.EpubError) as cm:
            self.EpubBook.open(self.path("empty.epub"))
        self.assertKind(cm.exception, "corrupt")

    def test_nonexistent_path_raises_cleanly(self):
        missing = str(FIXTURES / "this-file-does-not-exist.epub")
        with self.assertRaises((self.EpubError, FileNotFoundError, OSError)):
            self.EpubBook.open(missing)

    def test_every_failure_mode_has_a_message(self):
        for name in ("not_an_epub.epub", "no_container.epub", "truncated.epub",
                     "not_a_zip.epub", "empty.epub", "drm_fake.epub"):
            try:
                self.EpubBook.open(self.path(name))
            except self.EpubError as exc:
                self.assertTrue(str(exc).strip(), "%s: empty error message" % name)
            else:
                self.fail("%s should not have opened" % name)

    def test_read_of_unknown_entry_raises(self):
        book = self.book("epub2_ncx.epub")
        with self.assertRaises((self.EpubError, KeyError, FileNotFoundError)):
            book.read("OEBPS/nope-not-here.xhtml")


# ===========================================================================
# 12. plain_text, read, resolve - cross-cutting
# ===========================================================================


class TestPlainText(EpubTestCase):
    def test_returns_str(self):
        self.assertIsInstance(
            self.book("epub2_ncx.epub").plain_text("OEBPS/ch1.xhtml"), str
        )

    def test_tags_are_stripped(self):
        text = self.book("epub2_ncx.epub").plain_text("OEBPS/ch1.xhtml")
        self.assertIn("Chapter One", text)
        self.assertNotIn("<strong>", text)
        self.assertNotIn("<h1>", text)

    def test_entities_are_decoded(self):
        text = self.book("epub3_nav.epub").plain_text("EPUB/text/intro.xhtml")
        self.assertIn("caf\u00e9", text)
        self.assertIn("cr\u00e8me", text)
        self.assertIn("3 < 5", text)
        self.assertNotIn("&amp;", text)
        self.assertNotIn("&#233;", text)

    def test_script_contents_are_not_searchable(self):
        text = self.book("epub3_nav.epub").plain_text("EPUB/text/intro.xhtml")
        self.assertNotIn(
            "SCRIPT-MUST-NOT-BE-SEARCHABLE", text,
            "<script> contents must not appear in search text",
        )

    def test_style_contents_are_not_searchable(self):
        text = self.book("epub3_nav.epub").plain_text("EPUB/text/intro.xhtml")
        self.assertNotIn(
            "STYLE-MUST-NOT-BE-SEARCHABLE", text,
            "<style> contents must not appear in search text",
        )

    def test_comments_are_not_searchable(self):
        text = self.book("epub3_nav.epub").plain_text("EPUB/text/intro.xhtml")
        self.assertNotIn("COMMENT-MUST-NOT-BE-SEARCHABLE", text)

    def test_text_after_a_script_block_survives(self):
        text = self.book("epub3_nav.epub").plain_text("EPUB/text/intro.xhtml")
        self.assertIn("AFTER-SCRIPT-SENTINEL", text)

    def test_no_runaway_whitespace(self):
        text = self.book("epub2_ncx.epub").plain_text("OEBPS/ch1.xhtml")
        self.assertNotIn("    ", text.replace("\n", " ").strip(),
                         "plain_text should collapse whitespace runs")


class TestReadResolveGeneral(EpubTestCase):
    def test_resolve_returns_a_two_tuple(self):
        result = self.book("epub2_ncx.epub").resolve("ch1.xhtml", "OEBPS/content.opf")
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)

    def test_resolve_fragment_has_no_hash(self):
        _z, frag = self.book("epub2_ncx.epub").resolve(
            "ch1s1a.xhtml#sub", "OEBPS/content.opf"
        )
        self.assertEqual(frag, "sub")
        self.assertFalse(str(frag).startswith("#"))

    def test_resolve_no_fragment_is_none(self):
        _z, frag = self.book("epub2_ncx.epub").resolve("ch1.xhtml", "OEBPS/content.opf")
        self.assertIsNone(frag)

    def test_resolve_result_is_readable(self):
        book = self.book("epub2_ncx.epub")
        z, _ = book.resolve("images/cover.png", "OEBPS/content.opf")
        self.assertTrue(book.read(z))

    def test_read_returns_bytes_not_str(self):
        self.assertIsInstance(
            self.book("epub3_nav.epub").read("EPUB/css/main.css"), (bytes, bytearray)
        )

    def test_read_is_repeatable(self):
        book = self.book("epub3_nav.epub")
        a = book.read("EPUB/text/beta.xhtml")
        b = book.read("EPUB/text/beta.xhtml")
        self.assertEqual(bytes(a), bytes(b))

    def test_open_accepts_a_pathlib_path(self):
        book = self.EpubBook.open(FIXTURES / "epub2_ncx.epub")
        self.assertEqual(book.metadata["title"], "EPUB 2 With NCX")

    def test_two_books_are_independent(self):
        a = self.EpubBook.open(self.path("epub2_ncx.epub"))
        b = self.EpubBook.open(self.path("epub3_nav.epub"))
        self.assertNotEqual(a.metadata["title"], b.metadata["title"])
        self.assertNotEqual(
            [s.zip_name for s in a.spine], [s.zip_name for s in b.spine]
        )

    def test_spine_items_expose_the_documented_fields(self):
        item = self.book("epub2_ncx.epub").spine[0]
        for field in ("id", "href", "media_type", "linear", "zip_name"):
            self.assertTrue(hasattr(item, field), "SpineItem lacks %r" % field)

    def test_toc_entries_expose_the_documented_fields(self):
        entry = self.book("epub2_ncx.epub").toc[0]
        for field in ("title", "href", "fragment", "children"):
            self.assertTrue(hasattr(entry, field), "TocEntry lacks %r" % field)

    def test_every_spine_item_of_every_good_book_is_readable(self):
        good = ["epub2_ncx.epub", "epub3_nav.epub", "cjk_vertical.epub",
                "images_fonts.epub", "odd_paths.epub", "no_toc.epub",
                "fixed_layout.epub", "broken_xml.epub"]
        for name in good:
            book = self.book(name)
            for item in book.spine:
                with self.subTest(book=name, item=item.zip_name):
                    self.assertTrue(book.read(item.zip_name))


if __name__ == "__main__":
    unittest.main(verbosity=2)
