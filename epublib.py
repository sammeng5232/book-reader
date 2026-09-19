# -*- coding: utf-8 -*-
"""From-scratch EPUB 2/3 parser for Book Reader. Standard library only.

This module is the foundation every other module reads through. It owns:

* opening an OCF (zip) container and classifying every way it can fail
  (:class:`EpubError` with a machine-readable ``kind``),
* the package document (OPF): metadata, manifest, spine,
* the table of contents from an EPUB 3 nav document, else an EPUB 2 NCX,
  else synthesised from the spine,
* href -> zip-entry resolution, which is the single most bug-prone piece of
  an EPUB reader,
* transparent de-obfuscation of IDPF/Adobe "encrypted" fonts (which are NOT
  DRM and must never be reported as such),
* :meth:`EpubBook.plain_text`, the flattened document text that anchors every
  reading position, highlight and search hit in the app.

Design rules that are load-bearing, each one proven on this machine against
the user's own books (see ``docs/research/epub-spec.md``):

* Zip entry names are POSIX paths. ``posixpath`` is used everywhere; using
  ``os.path`` on Windows turns ``OEBPS/text/../a.xhtml`` into ``OEBPS\\a.xhtml``,
  which is never a zip entry.
* Bytes are decoded BEFORE they are parsed. Handing raw bytes to an XML/HTML
  parser makes it guess latin-1 for these books and produces mojibake with
  zero CJK characters.
* Every element is matched by its LOCAL NAME. Real books put the OPF in the
  wrong namespace, or in none at all.
* NCX ``playOrder`` is informational only and may be hexadecimal on real
  files. Document order is authoritative.
* ``META-INF/encryption.xml`` does not mean DRM: the IDPF and Adobe font
  obfuscation algorithms use the same file and are keyless.
* Nothing except a zip bomb, a broken archive, a missing container or an
  unusable package document is fatal. Everything else lands in
  :attr:`EpubBook.warnings` and the book still opens.

THE CROSS-ENGINE INVARIANT (CONTRACT.md section 2.1)
----------------------------------------------------
:meth:`EpubBook.plain_text` must return character-for-character the same
string that ``assets/reader.js`` builds in Chromium for the same document.
The Python side is implemented to match this JavaScript exactly::

    document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
      acceptNode(n) {
        if (!n.nodeValue || !n.parentElement) return NodeFilter.FILTER_REJECT;
        const t = n.parentElement.tagName;
        if (t === 'SCRIPT' || t === 'STYLE' || t === 'NOSCRIPT')
          return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      }})

i.e. walk text nodes of ``<body>`` in document order, reject text whose
immediate parent is ``<script>``/``<style>``/``<noscript>``, concatenate raw
``nodeValue`` with no separator, no whitespace collapsing and no trimming.
``display:none`` is deliberately NOT excluded: Python cannot see computed
style, so JavaScript must not either.

webhost.py serves content documents as ``text/html``, so the DOM that walker
sees is built by Chromium's HTML5 parser. :func:`flatten_html` therefore
models the HTML5 rules that change that text (CR LF normalisation, implied
<body>, the dropped LF after <pre>, ``<script/>`` swallowing the document,
<template>, SVG/MathML, NUL handling, table foster parenting, character
references). Offsets are Python ``str`` indices, i.e. Unicode code points;
reader.js converts its UTF-16 indices to code points at the bridge.

``python epublib.py --plain-text <book.epub> <zip_name>`` prints the Python
side as JSON for a diff against ``epubReader.flatText()``. See
``docs/api-epublib.md``.
"""

from __future__ import annotations

import hashlib
import mimetypes
import posixpath
import re
import threading
import unicodedata
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Iterable, Iterator, Sequence
from urllib.parse import unquote, urlsplit

import html.entities as _html_entities
import os

__all__ = [
    "EpubError",
    "SpineItem",
    "TocEntry",
    "SearchHit",
    "EpubBook",
    "flatten_html",
    "html5_unescape",
    "normalize_newlines",
    "fold_for_search",
    "plain_text_report",
    "count_units",
    "decode_bytes",
    "split_href",
    "is_remote",
    "normalize_zip_path",
    "obfuscation_key",
    "deobfuscate",
    "OBFUSCATION_ALGORITHMS",
    "OBFUS_IDPF",
    "OBFUS_ADOBE",
]

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

NS_OCF = "urn:oasis:names:tc:opendocument:xmlns:container"
NS_OPF = "http://www.idpf.org/2007/opf"
NS_DC = "http://purl.org/dc/elements/1.1/"
NS_NCX = "http://www.daisy.org/z3986/2005/ncx/"
NS_XHTML = "http://www.w3.org/1999/xhtml"
NS_EPUB = "http://www.idpf.org/2007/ops"
NS_XML = "http://www.w3.org/XML/1998/namespace"

#: IDPF font obfuscation (EPUB 3.3 sec 4.4.5). Keyless. NOT DRM.
OBFUS_IDPF = "http://www.idpf.org/2008/embedding"
#: Adobe font obfuscation. Keyless. NOT DRM. Both spellings are seen in the
#: wild; the user's own "50人的二十年" uses the first.
OBFUS_ADOBE = "http://ns.adobe.com/pdf/enc#RC"
OBFUS_ADOBE_RC4 = "http://ns.adobe.com/pdf/enc#RC4"

#: Everything outside this allowlist in encryption.xml is real DRM.
OBFUSCATION_ALGORITHMS = frozenset((OBFUS_IDPF, OBFUS_ADOBE, OBFUS_ADOBE_RC4))

_XHTML_TYPES = frozenset((
    "application/xhtml+xml", "text/html", "application/xml", "text/xml",
))
_NCX_TYPE = "application/x-dtbncx+xml"

#: Zip-bomb guard thresholds, verified to refuse a 407 KB file holding 400 MB.
_MAX_UNCOMPRESSED = 4 * 1024 ** 3
_BOMB_RATIO = 200
_BOMB_MIN_BYTES = 256 * 1024 ** 2

#: One "unit" is one CJK character or one Latin word. Calibrated against the
#: user's two real books (145,962 and 162,499 units => 8.1 h and 9.0 h).
_CJK_RE = re.compile(
    "[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002ebef]"
)
_LATIN_RE = re.compile(r"[A-Za-z0-9\u2019'\-]+")

_ASCII_WS_RE = re.compile(r"[ \t\r\n\f\v]+")
_ASCII_WS = " \t\r\n\f\v"

_MIME_EXTRA = {
    ".xhtml": "application/xhtml+xml",
    ".ncx": _NCX_TYPE,
    ".opf": "application/oebps-package+xml",
    ".otf": "font/otf",
    ".ttf": "font/ttf",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
}


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


class EpubError(Exception):
    """A book could not be opened, or a resource could not be served.

    ``kind`` is what the UI switches on; ``detail`` is the technical text for
    the 技术细节 disclosure and is never shown by default.

    kind
        ``'not_epub'``  a readable zip that is not an EPUB (no container.xml)
        ``'corrupt'``   not a zip at all, truncated, or empty
        ``'drm'``       genuinely encrypted (``drm_scheme`` names it)
        ``'no_container'`` reserved; see ``docs/api-epublib.md``
        ``'bad_opf'``   container.xml exists but no usable package document
        ``'too_large'`` refused by the zip-bomb guard
    """

    KINDS = ("not_epub", "corrupt", "drm", "no_container", "bad_opf", "too_large")

    def __init__(
        self,
        message: str,
        kind: str = "corrupt",
        detail: str = "",
        drm_scheme: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.detail = detail or message
        self.drm_scheme = drm_scheme

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "EpubError(kind=%r, %r)" % (self.kind, self.message)


# --------------------------------------------------------------------------
# public data model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SpineItem:
    """One document in reading order. ``linear=False`` items are INCLUDED."""

    id: str
    href: str            # as written in the manifest, relative to the OPF
    media_type: str
    linear: bool
    zip_name: str        # the exact archive entry name


@dataclass(frozen=True)
class TocEntry:
    """One table-of-contents node.

    ``href`` is the href exactly as written in the TOC document, with the
    fragment split off into ``fragment``; it is therefore still
    percent-encoded and still relative TO THE TOC DOCUMENT. ``zip_name`` is
    the already-resolved archive entry ("" when the entry is a grouping
    header with no target, or points outside the container).
    """

    title: str
    href: str
    fragment: str
    zip_name: str
    children: list["TocEntry"] = field(default_factory=list)


@dataclass(frozen=True)
class SearchHit:
    """One book-wide search result.

    ``gpos``/``length`` are offsets into ``plain_text(zip_name)`` and are what
    gets handed to ``reader.js``. ``before``/``after`` are whitespace-collapsed
    for display only; ``match`` is the exact matched substring.
    """

    spine_index: int
    zip_name: str
    gpos: int
    length: int
    before: str
    match: str
    after: str
    chapter_title: str


# --------------------------------------------------------------------------
# href / zip-entry resolution
# --------------------------------------------------------------------------


def split_href(href: str | None) -> tuple[str, str]:
    """Split a raw href into ``(path_part, fragment)``.

    The fragment comes back percent-decoded and without the leading ``#``;
    query strings are dropped (nothing in a zip answers a query). The split
    happens BEFORE percent-decoding so that ``a%23b.xhtml`` keeps its literal
    ``#`` instead of being read as a fragment marker.

    >>> split_href("text/ch1.xhtml#sec%201")
    ('text/ch1.xhtml', 'sec 1')
    >>> split_href("text/a%23b.xhtml")
    ('text/a%23b.xhtml', '')
    >>> split_href("#top")
    ('', 'top')
    """
    if not href:
        return "", ""
    path, _, frag = href.strip().partition("#")
    return path.partition("?")[0], unquote(frag)


def is_remote(href: str | None) -> bool:
    """True for anything that is not a resource inside the container.

    >>> [is_remote(u) for u in ("https://x/y", "//x/y", "data:,a", "a/b", "C:/x")]
    [True, True, True, False, True]
    """
    if not href:
        return False
    if href.startswith("//"):
        return True
    try:
        return bool(urlsplit(href).scheme)
    except ValueError:
        return False


def _norm_key(name: str) -> str:
    """Forgiving lookup key: backslash -> slash, collapse '//', NFC, casefold."""
    name = name.replace("\\", "/")
    while "//" in name:
        name = name.replace("//", "/")
    return unicodedata.normalize("NFC", name).casefold()


def normalize_zip_path(base_dir: str, href_path: str) -> str:
    """Join a decoded href onto ``base_dir`` and normalise to a POSIX path.

    ``base_dir`` is container-root relative with no trailing slash ('' is the
    root). ``..`` is clamped at the root rather than raising (OCF 4.2.5).

    >>> normalize_zip_path("OEBPS/text", "../images/a.png")
    'OEBPS/images/a.png'
    >>> normalize_zip_path("", "text/../style.css")
    'style.css'
    >>> normalize_zip_path("OEBPS", "../../../etc/passwd")
    'etc/passwd'
    """
    p = (href_path or "").replace("\\", "/")
    if p.startswith("/"):
        joined = p.lstrip("/")
    else:
        joined = posixpath.join(base_dir, p) if base_dir else p
    out = posixpath.normpath(joined)
    while out.startswith("../"):
        out = out[3:]
    if out in ("..", ".", "/"):
        out = ""
    return out.lstrip("/")


class _ZipIndex:
    """Entry-name index for one open container. Built once per book."""

    __slots__ = ("names", "exact", "_loose")

    def __init__(self, zf: zipfile.ZipFile) -> None:
        # Directory entries read as 0 bytes; an href of 'images/' must not
        # resolve to one.
        self.names: list[str] = [n for n in zf.namelist() if not n.endswith("/")]
        self.exact = set(self.names)
        self._loose: dict[str, str] = {}
        for n in self.names:
            self._loose.setdefault(_norm_key(n), n)

    def lookup(self, candidate: str) -> str | None:
        if candidate in self.exact:          # spec-correct, case sensitive
            return candidate
        return self._loose.get(_norm_key(candidate))   # malformed-file rescue


def _resolve(index: _ZipIndex, base_dir: str, href: str) -> tuple[str | None, str, bool]:
    """-> (entry_or_None, fragment, is_remote). See docs/api-epublib.md."""
    path, frag = split_href(href)
    if is_remote(href):
        return None, frag, True
    if not path:
        return None, frag, False             # bare '#frag' -> same document

    cand = normalize_zip_path(base_dir, unquote(path))
    hit = index.lookup(cand)
    if hit:
        return hit, frag, False

    raw = normalize_zip_path(base_dir, path)   # literal '%' in the entry name
    if raw != cand:
        hit = index.lookup(raw)
        if hit:
            return hit, frag, False

    try:                                       # cp1252/latin-1 percent escapes
        alt = normalize_zip_path(base_dir, unquote(path, encoding="latin-1"))
        if alt not in (cand, raw):
            hit = index.lookup(alt)
            if hit:
                return hit, frag, False
    except Exception:
        pass

    want = _norm_key(posixpath.basename(cand))  # unique basename, last resort
    if want:
        matches = [n for n in index.names
                   if _norm_key(posixpath.basename(n)) == want]
        if len(matches) == 1:
            return matches[0], frag, False

    return None, frag, False


# --------------------------------------------------------------------------
# decoding
# --------------------------------------------------------------------------

_XML_DECL_ENC = re.compile(rb"""<\?xml[^>]*?encoding\s*=\s*["']([A-Za-z0-9_.:\-]+)["']""")
_META_CHARSET = re.compile(rb"""<meta[^>]*?charset\s*=\s*["']?([A-Za-z0-9_.:\-]+)""", re.I)


def decode_bytes(raw: bytes, *, warnings: list[str] | None = None,
                 what: str = "") -> str:
    """Decode a text resource. UTF-8 first, BOM-aware, never a sniffer.

    EPUB mandates UTF-8 or UTF-16. A declared legacy encoding is honoured as
    a last resort (and warned about) rather than producing mojibake.
    """
    if raw.startswith(b"\xff\xfe\x00\x00") or raw.startswith(b"\x00\x00\xfe\xff"):
        return raw.decode("utf-32")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", "replace")
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    head = raw[:2048]
    declared = _XML_DECL_ENC.search(head) or _META_CHARSET.search(head)
    order = []
    if declared:
        order.append(declared.group(1).decode("ascii", "replace"))
    order += ["gb18030", "cp1252", "latin-1"]
    for enc in order:
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        if warnings is not None:
            warnings.append("%s is not UTF-8; decoded as %s" % (what or "resource", enc))
        return text
    if warnings is not None:
        warnings.append("%s could not be decoded; replaced bad bytes" % (what or "resource"))
    return raw.decode("utf-8", "replace")


# --------------------------------------------------------------------------
# lenient XML parsing (container.xml / OPF / NCX / nav)
# --------------------------------------------------------------------------


def _local_name(el: ET.Element) -> str:
    tag = el.tag
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _kids(el: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in el if _local_name(c) == name]


def _find_deep(root: ET.Element, name: str) -> list[ET.Element]:
    return [e for e in root.iter() if _local_name(e) == name]


def _attr(el: ET.Element, name: str, *namespaces: str) -> str | None:
    """Attribute by local name: unprefixed first, then each namespace."""
    v = el.get(name)
    if v is not None:
        return v
    for ns in namespaces:
        v = el.get("{%s}%s" % (ns, name))
        if v is not None:
            return v
    for key, val in el.attrib.items():
        if key.rsplit("}", 1)[-1] == name:
            return val
    return None


#: Search results show this many code points of context on each side.
_SEARCH_CONTEXT = 40

#: ``QUOTES`` in reader.js: typographic quotes and dashes search as ASCII.
_SEARCH_FOLD = str.maketrans({
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-",
    "—": "-", "―": "-",
})


def fold_for_search(text: str) -> str:
    """Length-preserving search fold, identical to ``fold()`` in reader.js.

    Every code point is lower-cased only if it stays ONE code point, so index
    ``i`` of the result is index ``i`` of ``text``; then typographic quotes
    and dashes become ASCII. ``str.lower()`` on a whole string is not used
    as-is where it differs: it expands U+0130 and applies Greek final-sigma
    context, and reader.js does neither.

    >>> fold_for_search("Don\\u2019t \\u2014 \\u0130STANBUL \\u03a3\\u03a3")
    "don't - \\u0130stanbul \\u03c3\\u03c3"
    """
    lowered = text.lower()
    if len(lowered) != len(text) or "Σ" in text:
        lowered = "".join(low if len(low := ch.lower()) == 1 else ch for ch in text)
    return lowered.translate(_SEARCH_FOLD)


def _context(text: str) -> str:
    """Search-result context: ASCII whitespace runs shown as one space."""
    return _ASCII_WS_RE.sub(" ", text)


def _norm_ws(text: str) -> str:
    """Collapse ASCII whitespace runs only.

    Deliberately not ``\\s`` — that also eats U+3000 IDEOGRAPHIC SPACE, which
    is a real character inside Chinese chapter titles such as
    "第一章\u3000横排正文".
    """
    return _ASCII_WS_RE.sub(" ", text).strip(_ASCII_WS)


def _text_of(el: ET.Element) -> str:
    """Concatenated label text of an element subtree, including img/@alt."""
    parts: list[str] = []
    for node in el.iter():
        if node is not el and _local_name(node) in ("img", "image"):
            alt = node.get("alt") or node.get("title")
            if alt:
                parts.append(alt)
        if node.text:
            parts.append(node.text)
        if node is not el and node.tail:
            parts.append(node.tail)
    return _norm_ws("".join(parts))


def _strip_preamble(data: bytes) -> bytes:
    if data[:3] == b"\xef\xbb\xbf":
        data = data[3:]
    i = data.find(b"<")
    return data[i:] if i > 0 else data


_ENTITY_RE = re.compile(rb"&(?!#|amp;|lt;|gt;|quot;|apos;)([A-Za-z][A-Za-z0-9]*);")


def _stub_entities(data: bytes) -> bytes:
    """&nbsp; and friends are a hard XML error without a DOCTYPE."""

    def sub(match: "re.Match[bytes]") -> bytes:
        name = match.group(1).decode("ascii", "replace")
        cp = _html_entities.name2codepoint.get(name)
        return ("&#%d;" % cp).encode("ascii") if cp else b"&amp;" + match.group(1) + b";"

    return _ENTITY_RE.sub(sub, data)


class _TreeHTMLParser(HTMLParser):
    """Last-resort recovery parser: tag soup -> ElementTree.

    ``html.parser`` is chosen over lxml on purpose: stdlib, never raises, and
    no libxml2 behaviour differences. Attribute names keep their RAW spelling,
    so ``epub:type`` stays the literal key ``'epub:type'`` — always check both
    spellings (see :func:`_nav_type`).
    """

    VOID = frozenset((
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    ))

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = ET.Element("html")
        self.stack: list[ET.Element] = [self.root]

    def handle_starttag(self, tag, attrs):  # noqa: D102
        el = ET.SubElement(self.stack[-1], tag,
                           {k: (v if v is not None else "") for k, v in attrs})
        if tag not in self.VOID:
            self.stack.append(el)

    def handle_startendtag(self, tag, attrs):  # noqa: D102
        ET.SubElement(self.stack[-1], tag,
                      {k: (v if v is not None else "") for k, v in attrs})

    def handle_endtag(self, tag):  # noqa: D102
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):  # noqa: D102
        cur = self.stack[-1]
        if len(cur):
            cur[-1].tail = (cur[-1].tail or "") + data
        else:
            cur.text = (cur.text or "") + data


def _html_to_element(data: bytes) -> ET.Element | None:
    text = decode_bytes(data)
    parser = _TreeHTMLParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        pass
    return parser.root if len(parser.root) else None


def parse_xml_lenient(data: bytes, what: str = "") -> ET.Element:
    """Parse XML, repairing progressively until something works.

    Ladder: strict -> strip BOM/leading junk -> substitute undeclared named
    entities -> html.parser tree builder. Raises :class:`ValueError` only if
    every stage fails.
    """
    attempts = [data]
    stripped = _strip_preamble(data)
    if stripped is not data:
        attempts.append(stripped)
    attempts.append(_stub_entities(stripped))
    for attempt in attempts:
        try:
            return ET.fromstring(attempt)
        except ET.ParseError:
            continue
        except ValueError:
            continue
    tree = _html_to_element(data)
    if tree is None:
        raise ValueError("cannot parse %s" % (what or "document"))
    return tree


# --------------------------------------------------------------------------
# the flattened-text walker -- CONTRACT.md section 2.1
# --------------------------------------------------------------------------
#
# webhost.py serves every content document as ``text/html``, so Chromium
# builds the DOM with the HTML5 parser, NOT an XML parser. The walker below
# reproduces exactly the parts of HTML5 tokenisation and tree construction
# that change the concatenated text of ``document.body``. Every rule was
# measured against QtWebEngine 6.11.1 (Chromium 140) on this machine; see
# ``docs/api-epublib.md`` and ``tests/test_epublib_extra.py``.

#: HTML "ASCII whitespace" (the WHATWG definition, which excludes U+000B).
_HTML_WS = "\t\n\f\r "

#: Elements with no content: never pushed on any stack.
_VOID_TAGS = frozenset((
    "area", "base", "basefont", "bgsound", "br", "col", "embed", "frame", "hr",
    "image", "img", "input", "keygen", "link", "meta", "param", "source",
    "track", "wbr",
))
#: HTML elements whose start tag switches the tokenizer (HTML namespace only).
#: ``noscript`` is raw text because scripting is enabled in the reader page.
_RAWTEXT_TAGS = frozenset((
    "script", "style", "xmp", "iframe", "noembed", "noframes", "noscript",
))
_RCDATA_TAGS = frozenset(("title", "textarea"))
#: reader.js rejects a text node when ``parentElement.closest('script,style,
#: noscript')`` matches: any ancestor with one of these local names, at any
#: depth, in ANY namespace (so SVG <style> text is rejected too -- measured).
_REJECT_TAGS = frozenset(("script", "style", "noscript"))
#: Start tags that stay in <head> ("in head" insertion mode).
_HEAD_TAGS = frozenset((
    "base", "basefont", "bgsound", "link", "meta", "title", "noscript",
    "noframes", "style", "script", "template",
))
#: Start tags that are put back into <head> in "after head" mode.
_AFTER_HEAD_TAGS = _HEAD_TAGS - {"noscript"}
#: A LF straight after these start tags is dropped by the parser.
_SKIP_LF_TAGS = frozenset(("pre", "listing", "textarea"))
#: HTML start tags that break out of SVG/MathML content.
_BREAKOUT_TAGS = frozenset((
    "b", "big", "blockquote", "body", "br", "center", "code", "dd", "div", "dl",
    "dt", "em", "embed", "h1", "h2", "h3", "h4", "h5", "h6", "head", "hr", "i",
    "img", "li", "listing", "menu", "meta", "nobr", "ol", "p", "pre", "ruby",
    "s", "small", "span", "strong", "strike", "sub", "sup", "table", "tt", "u",
    "ul", "var",
))
#: Start tags a <table> accepts in place; any other element is
#: foster-parented, i.e. moved in front of the table together with its text.
_TABLE_TAGS = frozenset((
    "caption", "colgroup", "col", "tbody", "thead", "tfoot", "tr", "td", "th",
    "table", "script", "style", "template", "input", "form",
))
#: Start tags that close an open cell or caption and restructure the table.
_TABLE_STRUCTURE_TAGS = frozenset((
    "caption", "col", "colgroup", "tbody", "td", "tfoot", "th", "thead", "tr",
))
_CELL_TAGS = frozenset(("td", "th", "caption"))
#: HTML "formatting" elements: re-created by the parser when still active.
_FORMATTING_TAGS = frozenset((
    "a", "b", "big", "code", "em", "font", "i", "nobr", "s", "small", "strike",
    "strong", "tt", "u",
))
#: "in body" start tags that do NOT reconstruct active formatting elements.
_NO_RECONSTRUCT_TAGS = frozenset((
    "address", "article", "aside", "blockquote", "center", "details", "dialog",
    "dir", "div", "dl", "fieldset", "figcaption", "figure", "footer", "header",
    "hgroup", "main", "menu", "nav", "ol", "p", "search", "section", "summary",
    "ul", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "listing", "form", "li",
    "dd", "dt", "plaintext", "table", "hr", "textarea", "iframe", "noembed",
    "noscript", "param", "source", "track", "rb", "rp", "rt", "rtc", "frameset",
    "body", "html", "head", "base", "basefont", "bgsound", "link", "meta",
    "noframes", "script", "style", "template", "title", "caption", "col",
    "colgroup", "frame", "tbody", "td", "tfoot", "th", "thead", "tr",
))

# The insertion modes that decide whether text lands in <body>. "in body"
# also stands for "after body" / "after after body": text there is appended
# to <body> too.
_M_INITIAL, _M_IN_HEAD, _M_AFTER_HEAD, _M_IN_BODY, _M_FRAMESET = range(5)

_CHARREF_RE = re.compile(
    r"&(#[0-9]+;?|#[xX][0-9a-fA-F]+;?|[^\t\n\f <&#;]{1,32};?)")


def _html5_charref(match: "re.Match[str]") -> str:
    s = match.group(1)
    if s[0] == "#":
        try:
            if s[1] in "xX":
                num = int(s[2:].rstrip(";"), 16)
            else:
                num = int(s[1:].rstrip(";"))
        except ValueError:              # more digits than int() will parse
            return "�"
        if num == 0 or num > 0x10FFFF or 0xD800 <= num <= 0xDFFF:
            return "�"
        if 0x80 <= num <= 0x9F:         # the WHATWG table is exactly cp1252
            try:
                return bytes((num,)).decode("cp1252")
            except UnicodeDecodeError:
                return chr(num)
        # Controls and noncharacters are KEPT (a parse error, not a deletion).
        # html.unescape() deletes them: the one place it is not HTML5.
        return chr(num)
    table = _html_entities.html5
    if s in table:
        return table[s]
    for x in range(len(s) - 1, 1, -1):  # longest legacy name, e.g. &notit;
        if s[:x] in table:
            return table[s[:x]] + s[x:]
    return "&" + s


def html5_unescape(text: str) -> str:
    """Resolve character references exactly as the HTML5 tokenizer does.

    Identical to :func:`html.unescape` except that numeric references to
    control characters and noncharacters are kept instead of deleted.

    >>> html5_unescape("&copy 2020 &notit; &#x80; &#1;")
    '\\xa9 2020 \\xacit; \\u20ac \\x01'
    """
    if "&" not in text:
        return text
    return _CHARREF_RE.sub(_html5_charref, text)


def _html5_goahead():
    """``HTMLParser.goahead`` resolving references with :func:`html5_unescape`.

    ``html.parser`` calls its module-global ``unescape``. A copy of the SAME
    code object, bound to a private globals dict whose ``unescape`` is
    HTML5-exact, changes nothing else and touches no shared state. If it
    cannot be built the stock method is used, and only numeric references to
    control characters differ from Chromium.
    """
    import html.parser as html_parser_module
    import types

    base = HTMLParser.goahead
    try:
        env = dict(vars(html_parser_module))
        if "unescape" not in env:
            return base
        env["unescape"] = html5_unescape
        return types.FunctionType(base.__code__, env, base.__name__,
                                  base.__defaults__, base.__closure__)
    except Exception:  # pragma: no cover - defensive
        return base


class _Table:
    """One open <table>: just enough state to know where its text goes."""

    __slots__ = ("at", "section", "row", "cell", "foster", "active")

    def __init__(self, at: int) -> None:
        self.at = at                    # index in parts where fostered text goes
        self.section = False            # a <tbody>/<thead>/<tfoot> is open
        self.row = False                # a <tr> is open
        self.cell: str | None = None    # 'td' | 'th' | 'caption' of THIS table
        self.foster: list[str] = []     # open elements that were foster-parented
        self.active: list[str] = []     # fostered formatting elements still active

    def reconstruct(self) -> None:
        """Re-open active formatting elements the table structure closed.

        The clones are inserted at the foster location, so whatever follows
        them in table context is fostered too -- measured.
        """
        if not self.active:
            return
        last_open = -1
        for k in range(len(self.active) - 1, -1, -1):
            if self.active[k] in self.foster:
                last_open = k
                break
        self.foster.extend(self.active[last_open + 1:])


class _FlatTextParser(HTMLParser):
    """Build the text of ``document.body`` the way Chromium's HTML5 parser does.

    reader.js walks ``document.body`` after HTML5 tree construction and keeps
    every text node except those inside a ``<script>``, ``<style>`` or
    ``<noscript>`` element (any depth, any namespace). The rules reproduced
    here, each measured in QtWebEngine:

    * CR LF and lone CR become LF before tokenising;
    * adjacent character data is one run (html.parser splits "a < b" at the
      "<"; the HTML5 tokenizer does not, and table foster parenting decides
      per run);
    * U+0000 is dropped from HTML text before anything else looks at it;
      it becomes U+FFFD in raw text and in foreign content;
    * text lands in <body> by insertion mode: whitespace before <body> is
      dropped, any other character in <head> or after </head> implies <body>,
      and text after </body> or </html> still goes into <body>;
    * a LF straight after <pre>, <listing> or <textarea> is dropped;
    * the self-closing flag is ignored on HTML elements, so ``<script/>``,
      ``<title/>`` or ``<iframe/>`` swallow the rest of the document;
    * <template> contents are not part of the walked tree;
    * inside SVG/MathML, <style>/<script> do not switch the tokenizer (their
      markup is parsed, and still rejected), and ``<![CDATA[...]]>`` is
      text; in HTML it is a bogus comment;
    * text and elements misplaced directly inside a <table> are moved in
      front of it (foster parenting), including formatting elements such as
      <a>/<b> that the parser re-creates there;
    * ``<!-->`` and ``<!--->`` are complete comments;
    * character references follow :func:`html5_unescape`;
    * in a <frameset> document (``document.body`` is the frameset) only
      whitespace and <noframes> text survive.

    ``display:none`` is NOT excluded: Python cannot see computed style.
    """

    goahead = _html5_goahead()

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True, scripting=True)
        self._parts: list[str] = []
        self._pending: list[str] = []
        self._mode = _M_INITIAL
        self._skip_lf = False
        self._cdata_ok = True
        self._raw_in_head = False
        self._template = 0
        self._frameset = 0
        self._tables: list[_Table] = []
        # Open elements, tracked only while inside SVG/MathML:
        # (tag, namespace 'html' | 'svg' | 'math', is_integration_point)
        self._stack: list[tuple[str, str, bool]] = []
        self._sync_cdata_sections()

    # -- helpers ------------------------------------------------------------

    def _foreign(self) -> str | None:
        """Namespace of the current node when it is SVG/MathML content."""
        if not self._stack:
            return None
        _tag, ns, integration = self._stack[-1]
        if ns == "html" or integration:
            return None
        return ns

    def _in_rejected_foreign(self) -> bool:
        """An open SVG/MathML-tracked element is named script/style/noscript."""
        return bool(self._stack) and any(e[0] in _REJECT_TAGS for e in self._stack)

    def _sync_cdata_sections(self) -> None:
        setter = getattr(self, "_set_support_cdata", None)
        if setter is not None:
            setter(self._foreign() is not None)

    def _emit(self, text: str) -> None:
        if self._tables:
            table = self._tables[-1]
            if table.cell is None and (table.foster or text.strip(_HTML_WS)):
                table.reconstruct()
                self._parts.insert(table.at, text)
                table.at += 1
                return
        self._parts.append(text)

    def set_cdata_mode(self, elem, *, escapable=False):  # noqa: D102
        if not self._cdata_ok:
            return                      # SVG/MathML never switch the tokenizer
        self._raw_in_head = self._mode not in (_M_IN_BODY, _M_FRAMESET)
        super().set_cdata_mode(elem, escapable=escapable)

    def parse_comment(self, i, report=True):  # noqa: D102
        rawdata = self.rawdata
        if rawdata.startswith(">", i + 4) or rawdata.startswith("->", i + 4):
            self._flush()                          # <!--> and <!--->
            self._skip_lf = False
            return i + (5 if rawdata[i + 4] == ">" else 6)
        return super().parse_comment(i, report)

    def close(self):  # noqa: D102
        super().close()
        self._flush()

    # -- start tags ---------------------------------------------------------

    def handle_starttag(self, tag, attrs):  # noqa: D102
        self._start(tag, attrs, False)

    def handle_startendtag(self, tag, attrs):  # noqa: D102
        self._start(tag, attrs, True)

    def _start(self, tag: str, attrs, self_closing: bool) -> None:
        self._flush()
        self._skip_lf = False
        self._cdata_ok = True
        ns = self._foreign()
        if ns is not None:
            under_annotation = self._stack[-1][0] == "annotation-xml"
            breakout = tag in _BREAKOUT_TAGS or (
                tag == "font" and any(k in ("color", "face", "size") for k, _v in attrs))
            if not breakout and not (tag == "svg" and under_annotation):
                self._cdata_ok = False
                if not self_closing:
                    # A foreign child takes its parent's namespace: <svg> inside
                    # <math> is a MathML element (only annotation-xml differs,
                    # handled above).
                    child_ns = ns
                    if child_ns == "svg":
                        integration = tag in ("foreignobject", "desc", "title")
                    else:
                        encoding = next((v for k, v in attrs if k == "encoding"), "") or ""
                        integration = tag in ("mi", "mo", "mn", "ms", "mtext") or (
                            tag == "annotation-xml" and encoding.strip().lower()
                            in ("text/html", "application/xhtml+xml"))
                    self._stack.append((tag, child_ns, integration))
                    self._sync_cdata_sections()
                return
            if breakout:
                while self._foreign() is not None:
                    self._stack.pop()
                self._sync_cdata_sections()
        self._start_html(tag, attrs, self_closing)

    def _start_html(self, tag: str, attrs, self_closing: bool) -> None:
        mode = self._mode
        if mode == _M_INITIAL:
            if tag == "html":
                return
            self._mode = mode = _M_IN_HEAD
            if tag == "head":
                return
        if mode == _M_IN_HEAD:
            if tag in _HEAD_TAGS:
                self._open_head_element(tag, self_closing)
                return
            if tag in ("head", "html"):
                return
            self._mode = mode = _M_AFTER_HEAD
        if mode == _M_AFTER_HEAD:
            if tag == "body":
                self._mode = _M_IN_BODY
                return
            if tag == "frameset":
                self._mode = _M_FRAMESET
                self._frameset = 1
                return
            if tag in _AFTER_HEAD_TAGS:
                self._open_head_element(tag, self_closing)
                return
            if tag in ("head", "html"):
                return
            self._mode = mode = _M_IN_BODY
        if mode == _M_FRAMESET:
            self._cdata_ok = tag == "noframes"
            if tag == "frameset" and self._frameset:
                self._frameset += 1
            elif tag == "noframes" and self_closing:
                self._self_closed_raw(tag)
            return

        # -- in body --------------------------------------------------------
        if tag in ("html", "body", "head", "frameset"):
            return
        if tag == "template":
            self._template += 1
        if not self._template:
            self._table_start(tag)
        if tag in ("svg", "math"):
            if not self_closing:
                self._stack.append((tag, tag, False))
                self._sync_cdata_sections()
            return
        if self._stack and tag not in _VOID_TAGS:
            self._stack.append((tag, "html", False))
        if tag in _SKIP_LF_TAGS:
            self._skip_lf = True
        if self_closing:
            self._self_closed_raw(tag)

    def _open_head_element(self, tag: str, self_closing: bool) -> None:
        if tag == "template":
            self._template += 1
        if self_closing:
            self._self_closed_raw(tag)

    def _self_closed_raw(self, tag: str) -> None:
        # html.parser honours "/>" and never enters raw-text mode for it; the
        # HTML5 parser ignores the flag, so <script/> eats the document.
        if tag in _RAWTEXT_TAGS or tag == "plaintext":
            self.set_cdata_mode(tag, escapable=False)
        elif tag in _RCDATA_TAGS:
            self.set_cdata_mode(tag, escapable=True)

    def _table_start(self, tag: str) -> None:
        table = self._tables[-1] if self._tables else None
        if tag == "table":
            inherited: list[str] = []
            if table is not None and table.cell is None:
                self._tables.pop()      # <table> in table context closes it...
                inherited = table.active  # ...but its active formatting stays active
            fresh = _Table(len(self._parts))
            fresh.active = list(inherited)
            self._tables.append(fresh)
            return
        if table is None:
            return
        if table.cell is not None:
            if tag not in _TABLE_STRUCTURE_TAGS:
                return                  # ordinary content of a cell/caption
            table.cell = None           # "in cell"/"in caption": close, reprocess
        if tag == "caption":
            table.section = table.row = False
            table.cell = "caption"
        elif tag in ("colgroup", "col"):
            table.section = table.row = False
        elif tag in ("tbody", "thead", "tfoot"):
            table.section, table.row = True, False
        elif tag == "tr":
            table.section = table.row = True
        elif tag in ("td", "th"):
            table.section = table.row = True
            table.cell = tag
        else:
            if table.foster or tag not in _TABLE_TAGS:
                # Processed with the "in body" rules at the foster location.
                if "select" in table.foster and tag in ("select", "input", "keygen", "textarea"):
                    self._close_fostered(table, "select")   # "in select": acts as </select>
                    if tag == "select":
                        return
                if tag == "a" and "a" in table.active:
                    self._close_fostered(table, "a")
                if tag not in _NO_RECONSTRUCT_TAGS:
                    table.reconstruct()
                if tag not in _VOID_TAGS:
                    table.foster.append(tag)
                    if tag in _FORMATTING_TAGS:
                        table.active.append(tag)
            return
        table.foster.clear()

    @staticmethod
    def _close_fostered(table: _Table, tag: str) -> None:
        if tag in table.active:
            del table.active[len(table.active) - 1 - table.active[::-1].index(tag)]
        if tag in table.foster:
            del table.foster[len(table.foster) - 1 - table.foster[::-1].index(tag):]

    # -- end tags -----------------------------------------------------------

    def handle_endtag(self, tag):  # noqa: D102
        self._flush()
        self._skip_lf = False
        if self._stack:
            if self._foreign() is not None and tag in ("br", "p"):
                while self._foreign() is not None:
                    self._stack.pop()
                self._sync_cdata_sections()
            else:
                for k in range(len(self._stack) - 1, -1, -1):
                    name, ns, _integration = self._stack[k]
                    if name == tag:
                        del self._stack[k:]
                        self._sync_cdata_sections()
                        if ns != "html":
                            if not self._template:
                                self._table_end(tag)
                            return
                        break
                    if ns == "html":
                        break
        self._end_html(tag)

    def _end_html(self, tag: str) -> None:
        mode = self._mode
        if mode == _M_INITIAL:
            if tag not in ("head", "body", "html", "br"):
                return
            self._mode = mode = _M_IN_HEAD
        if mode == _M_IN_HEAD:
            if tag == "head":
                self._mode = _M_AFTER_HEAD
                return
            if tag == "template":
                self._template = max(0, self._template - 1)
                return
            if tag not in ("body", "html", "br"):
                return
            self._mode = mode = _M_AFTER_HEAD
        if mode == _M_AFTER_HEAD:
            if tag == "template":
                self._template = max(0, self._template - 1)
                return
            if tag in ("body", "html", "br"):
                self._mode = _M_IN_BODY
            return
        if mode == _M_FRAMESET:
            if tag == "frameset":
                self._frameset = max(0, self._frameset - 1)
            return
        if tag == "template":
            self._template = max(0, self._template - 1)
            return
        if not self._template:
            self._table_end(tag)

    def _table_end(self, tag: str) -> None:
        if not self._tables:
            return
        table = self._tables[-1]
        if tag == "table":
            self._tables.pop()
            return
        if table.cell is not None:
            if tag == table.cell:
                table.cell = None
                return
            if table.cell == "caption":
                return                  # </tr>, </td>... are ignored in a caption
            if not ((tag == "tr" and table.row)
                    or (tag in ("tbody", "thead", "tfoot") and table.section)):
                return                  # an ordinary end tag inside the cell
            table.cell = None           # closes the cell, then reprocess below
        if tag == "tr":
            if table.row:
                table.row = False
                table.foster.clear()
        elif tag in ("tbody", "thead", "tfoot"):
            if table.section:
                table.section = table.row = False
                table.foster.clear()
        elif tag in _CELL_TAGS or tag in ("colgroup", "col", "body", "html"):
            pass                        # ignored in table/section/row context
        elif tag in table.foster or tag in table.active:
            self._close_fostered(table, tag)

    # -- everything else ----------------------------------------------------

    def handle_comment(self, data):  # noqa: D102
        self._flush()
        self._skip_lf = False

    def handle_decl(self, decl):  # noqa: D102
        self._flush()
        self._skip_lf = False

    def handle_pi(self, data):  # noqa: D102
        self._flush()
        self._skip_lf = False

    def unknown_decl(self, data):  # noqa: D102
        self._flush()
        self._skip_lf = False
        # Only reached for "<![CDATA[" while in SVG/MathML (see
        # _sync_cdata_sections); in HTML it is a bogus comment instead.
        if (data.startswith("CDATA[") and self._foreign() is not None
                and not self._template and not self._in_rejected_foreign()):
            text = data[6:].replace("\x00", "�")
            if text:
                self._emit(text)

    # -- character data -----------------------------------------------------

    def handle_data(self, data):  # noqa: D102
        self._pending.append(data)      # one run until the next real token

    def _flush(self) -> None:
        if not self._pending:
            return
        data = self._pending[0] if len(self._pending) == 1 else "".join(self._pending)
        self._pending.clear()
        self._text(data)

    def _text(self, data: str) -> None:
        raw = self.cdata_elem is not None
        if "\x00" in data:
            if raw or self._foreign() is not None:
                data = data.replace("\x00", "�")
            else:
                data = data.replace("\x00", "")
        if self._skip_lf:
            self._skip_lf = False
            if data.startswith("\n"):
                data = data[1:]
        if not data or self._template:
            return
        if self._stack and self._in_rejected_foreign():
            return                      # (the stack is only ever used in <body>)
        if raw:
            if (self._raw_in_head or self.cdata_elem in _REJECT_TAGS
                    or (self._mode == _M_FRAMESET and not self._frameset)):
                return
            self._emit(data)
            return
        if self._mode != _M_IN_BODY:
            if self._mode == _M_FRAMESET:
                if self._frameset:
                    data = "".join(c for c in data if c in _HTML_WS)
                    if data:
                        self._emit(data)
                return
            data = data.lstrip(_HTML_WS)
            if not data:
                return
            self._mode = _M_IN_BODY     # any other character implies <body>
        self._emit(data)

    def result(self) -> str:
        self._flush()
        return "".join(self._parts)


def normalize_newlines(source: str) -> str:
    """HTML input-stream preprocessing: CR LF and lone CR become LF.

    Also turns ``<`` + U+0000 into ``<`` + U+FFFD: Blink skips NUL only in
    the tokenizer's data states, and after ``<`` it is in "tag open" state,
    where NUL becomes a replacement character that is then emitted as text.

    >>> normalize_newlines("a\\r\\nb\\rc")
    'a\\nb\\nc'
    """
    if "\r" in source:
        source = source.replace("\r\n", "\n").replace("\r", "\n")
    if "<\x00" in source:
        source = source.replace("<\x00", "<�")
    return source


def flatten_html(source: str) -> str:
    """Flatten an (X)HTML document to the string ``reader.js`` produces.

    CONTRACT.md section 2.1: the text nodes of ``document.body`` in document
    order, without text whose parent is ``<script>``/``<style>``/
    ``<noscript>``, concatenated with no separators, no whitespace collapsing
    and no trimming, character references resolved -- as built by Chromium's
    HTML5 parser, because that is how webhost.py serves the document.

    >>> flatten_html("<html><head><title>T</title></head><body><p>a&amp;b</p></body></html>")
    'a&b'
    >>> flatten_html("<body>x<script>HIDDEN</script>y</body>")
    'xy'
    >>> flatten_html("<body><p>a\\r\\nb</p><pre>\\ncode</pre></body>")
    'a\\nbcode'
    """
    parser = _FlatTextParser()
    parser.feed(normalize_newlines(source))
    parser.close()
    return parser.result()


class _DocTitleParser(HTMLParser):
    """Pull ``<title>`` and the first ``<h1>``-``<h3>`` out of a document."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.heading = ""
        self._want: str | None = None
        self._buf: list[str] = []
        self._done_title = False

    def handle_starttag(self, tag, attrs):  # noqa: D102
        if tag == "title" and not self._done_title:
            self._want, self._buf = "title", []
        elif tag in ("h1", "h2", "h3") and not self.heading:
            self._want, self._buf = "heading", []

    def handle_endtag(self, tag):  # noqa: D102
        if self._want == "title" and tag == "title":
            self.title = _norm_ws("".join(self._buf))
            self._done_title = True
            self._want = None
        elif self._want == "heading" and tag in ("h1", "h2", "h3"):
            self.heading = _norm_ws("".join(self._buf))
            self._want = None

    def handle_data(self, data):  # noqa: D102
        if self._want:
            self._buf.append(data)


_VIEWPORT_NUM = re.compile(r"(width|height)\s*=\s*([0-9.]+)", re.I)


class _ViewportParser(HTMLParser):
    """First ``<meta name="viewport">`` in the head; later ones are ignored."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.content: str | None = None
        self.svg_viewbox: str | None = None

    def handle_starttag(self, tag, attrs):  # noqa: D102
        d = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta" and d.get("name", "").lower() == "viewport":
            if self.content is None:
                self.content = d.get("content", "")
        elif tag == "svg" and self.svg_viewbox is None:
            self.svg_viewbox = d.get("viewbox")

    def handle_startendtag(self, tag, attrs):  # noqa: D102
        self.handle_starttag(tag, attrs)


# --------------------------------------------------------------------------
# units
# --------------------------------------------------------------------------


def count_units(text: str) -> int:
    """One unit = one CJK character OR one Latin word.

    Mixing them this way keeps reading-time estimates stable for a bilingual
    library. Calibrated on the user's own books.

    >>> count_units("hello world")
    2
    >>> count_units("\u4e2d\u6587 test")
    3
    """
    return len(_CJK_RE.findall(text)) + len(_LATIN_RE.findall(text))


# --------------------------------------------------------------------------
# font obfuscation (NOT DRM)
# --------------------------------------------------------------------------


def obfuscation_key(algorithm: str, unique_identifier: str) -> bytes:
    """Derive the keyless de-obfuscation key for one of the two algorithms."""
    if algorithm == OBFUS_IDPF:
        # EPUB 3.3 sec 4.4.3: strip U+0020/09/0D/0A, SHA-1 of the UTF-8 form.
        stripped = "".join(
            c for c in unique_identifier
            if c not in ("\u0020", "\u0009", "\u000d", "\u000a")
        )
        return hashlib.sha1(stripped.encode("utf-8")).digest()
    if algorithm in (OBFUS_ADOBE, OBFUS_ADOBE_RC4):
        uid = unique_identifier.strip()
        if uid.lower().startswith("urn:uuid:"):
            uid = uid[9:]
        uid = "".join(c for c in uid if c in "0123456789abcdefABCDEF")
        if len(uid) < 32:
            raise ValueError("Adobe obfuscation needs 32 hex digits, got %d" % len(uid))
        return bytes.fromhex(uid[:32])
    raise ValueError("not an obfuscation algorithm: %r" % (algorithm,))


def deobfuscate(data: bytes, algorithm: str, key: bytes) -> bytes:
    """XOR the head of a font back to plaintext. Self-inverse.

    IDPF mangles the first 1040 bytes; Adobe the first 1024.
    """
    if not key:
        return data
    n = min(1040 if algorithm == OBFUS_IDPF else 1024, len(data))
    head = bytearray(data[:n])
    klen = len(key)
    for i in range(n):
        head[i] ^= key[i % klen]
    return bytes(head) + data[n:]


# --------------------------------------------------------------------------
# internal manifest record
# --------------------------------------------------------------------------


@dataclass
class _Item:
    id: str
    href: str
    media_type: str
    properties: frozenset
    zip_name: str | None


# --------------------------------------------------------------------------
# the book
# --------------------------------------------------------------------------


class EpubBook:
    """An open EPUB container, read-only.

    Never mutates the file. Use as a context manager, or remember to
    :meth:`close`. Zip reads are serialised with a lock so a background
    thumbnail worker and the reader can share one book safely.
    """

    _PLAIN_TEXT_CACHE = 48

    # -- construction -------------------------------------------------------

    def __init__(self, path: str, zf: zipfile.ZipFile) -> None:
        self.path: str = path
        self._zf: zipfile.ZipFile = zf
        self._lock = threading.RLock()
        self._closed = False

        self.warnings: list[str] = []
        self.opf_path: str = ""
        self.opf_dir: str = ""
        self.version: str = ""
        self.metadata: dict = {}
        self.spine: list[SpineItem] = []
        self.toc: list[TocEntry] = []
        self.toc_is_synthetic: bool = False
        self.cover: str | None = None
        self.is_fixed_layout: bool = False
        self.page_direction: str = "ltr"

        self._index: _ZipIndex = _ZipIndex(zf)
        self._items: dict[str, _Item] = {}
        self._by_zip_name: dict[str, _Item] = {}
        self._obfuscated: dict[str, str] = {}
        self._unique_id: str = ""
        self._pre_paginated: set[str] = set()

        self._text_cache: dict[str, str] = {}
        self._text_order: list[str] = []
        self._units_cache: dict[str, int] = {}
        self._title_cache: dict[str, str] = {}
        self._spine_pos: dict[str, int] = {}
        self._chapter_titles: dict[str, str] | None = None
        self._total_units: int | None = None

    @classmethod
    def open(cls, path: "str | os.PathLike[str]") -> "EpubBook":
        """Open a book read-only.

        Raises :class:`EpubError` for every EPUB-level failure. A path that
        does not exist raises the underlying :class:`OSError`
        (``FileNotFoundError``) unchanged, because "missing" is not the same
        problem as "broken" and the library page treats it differently.
        """
        spath = os.fspath(path)
        if isinstance(spath, bytes):
            spath = spath.decode("utf-8", "replace")

        zf = _open_container(spath)
        try:
            book = cls(spath, zf)
            book._load()
        except BaseException:
            try:
                zf.close()
            except Exception:
                pass
            raise
        return book

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        """Close the archive. Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._zf.close()
            except Exception:
                pass
            self._text_cache.clear()
            self._text_order.clear()

    def __enter__(self) -> "EpubBook":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<EpubBook %r spine=%d toc=%d>" % (
            self.metadata.get("title", ""), len(self.spine), len(self.toc))

    # ------------------------------------------------------------------
    # loading
    # ------------------------------------------------------------------

    def _load(self) -> None:
        warn = self.warnings
        _guard_zip_bomb(self._zf, self.path)
        _check_mimetype(self._zf, warn)

        # DRM first: a locked book must be reported as locked whatever else
        # is wrong with it.
        obfuscated, drm = self._read_encryption()
        if drm:
            scheme = _drm_scheme(self._index)
            algos = sorted({a for _, a in drm if a})
            raise EpubError(
                "book is encrypted (%s)" % (", ".join(algos) or "unknown algorithm"),
                kind="drm",
                detail="META-INF/encryption.xml lists %d encrypted resource(s): %s"
                       % (len(drm), "; ".join("%s <- %s" % (e, a) for e, a in drm[:6])),
                drm_scheme=scheme,
            )
        self._obfuscated = obfuscated

        self.opf_path = self._find_opf()
        self.opf_dir = posixpath.dirname(self.opf_path)
        try:
            raw = self._read_entry(self.opf_path)
        except KeyError as exc:
            raise EpubError("package document is unreadable", kind="bad_opf",
                            detail="%s: %r" % (type(exc).__name__, self.opf_path)) from exc
        try:
            pkg = parse_xml_lenient(raw, self.opf_path)
        except Exception as exc:
            raise EpubError("package document could not be parsed", kind="bad_opf",
                            detail="%s: %s" % (type(exc).__name__, exc)) from exc
        if _local_name(pkg) != "package":
            warn.append("OPF root element is <%s>, not <package>" % _local_name(pkg))

        self.version = (pkg.get("version") or "").strip()
        meta_records = self._parse_metadata(pkg)
        self._parse_manifest(pkg)
        toc_idref = self._parse_spine(pkg)
        if not self.version:
            self.version = "3.0" if any("nav" in i.properties
                                        for i in self._items.values()) else "2.0"
            warn.append("package/@version missing; assuming %s" % self.version)

        self._build_toc(toc_idref)
        self.cover = self._find_cover(pkg)
        self._detect_layout(meta_records)

        for pos, item in enumerate(self.spine):
            self._spine_pos.setdefault(item.zip_name, pos)

    # -- container ----------------------------------------------------------

    def _read_encryption(self) -> tuple[dict[str, str], list[tuple[str, str]]]:
        """Partition META-INF/encryption.xml into obfuscation vs real DRM."""
        obf: dict[str, str] = {}
        drm: list[tuple[str, str]] = []
        name = self._index.lookup("META-INF/encryption.xml")
        if not name:
            return obf, drm
        try:
            root = parse_xml_lenient(self._read_entry(name), name)
        except Exception as exc:
            self.warnings.append("encryption.xml unparsable: %s" % exc)
            return obf, drm
        for data in _find_deep(root, "EncryptedData"):
            algo = ""
            for method in _find_deep(data, "EncryptionMethod"):
                algo = (method.get("Algorithm") or "").strip()
            for ref in _find_deep(data, "CipherReference"):
                uri = ref.get("URI") or ""
                entry, _frag, _remote = _resolve(self._index, "", uri)
                entry = entry or normalize_zip_path("", unquote(uri))
                if algo in OBFUSCATION_ALGORITHMS:
                    obf[entry] = algo
                else:
                    drm.append((entry, algo))
        return obf, drm

    def _find_opf(self) -> str:
        """Locate the package document. Raises EpubError when there is none."""
        warn = self.warnings
        container = self._index.lookup("META-INF/container.xml")
        if not container:
            raise EpubError(
                "not an EPUB: META-INF/container.xml is missing",
                kind="not_epub",
                detail="the archive opened but has no OCF container document",
            )
        if container != "META-INF/container.xml":
            warn.append("container.xml found only case-insensitively: %r" % container)

        rootfiles: list[ET.Element] = []
        try:
            root = parse_xml_lenient(self._read_entry(container), container)
            rootfiles = _find_deep(root, "rootfile")
        except Exception as exc:
            warn.append("container.xml unparsable (%s); scanning for an OPF" % exc)

        if len(rootfiles) > 1:
            warn.append("container.xml lists %d rootfiles; using the first"
                        % len(rootfiles))
        preferred = [r for r in rootfiles
                     if (r.get("media-type") or "") == "application/oebps-package+xml"]
        for rootfile in (preferred or rootfiles):
            full_path = rootfile.get("full-path")
            if not full_path:
                continue
            # full-path is relative to the container ROOT, never to META-INF.
            entry, _frag, _remote = _resolve(self._index, "", full_path)
            if entry:
                return entry
            warn.append("rootfile full-path %r is not in the archive" % full_path)

        candidates = [n for n in self._index.names if n.lower().endswith(".opf")]
        if not candidates:
            raise EpubError(
                "no package document in the container",
                kind="bad_opf",
                detail="container.xml names no usable rootfile and there is no *.opf",
            )
        candidates.sort(key=lambda n: (n.count("/"), len(n)))
        warn.append("recovered the package document by scanning: %r" % candidates[0])
        return candidates[0]

    # -- OPF ----------------------------------------------------------------

    def _parse_metadata(self, pkg: ET.Element) -> list[dict]:
        warn = self.warnings
        md_el = next((c for c in pkg if _local_name(c) == "metadata"), None)
        fields = ("title", "creator", "contributor", "language", "identifier",
                  "date", "publisher", "description", "subject", "rights")
        buckets: dict[str, list[dict]] = {k: [] for k in fields}
        metas: list[dict] = []
        if md_el is None:
            warn.append("OPF has no <metadata>")
            self.metadata = _empty_metadata()
            return metas

        by_id: dict[str, dict] = {}
        records: list[tuple[str, dict]] = []
        for el in md_el:
            name = _local_name(el)
            if name == "meta":
                prop = el.get("property")
                if prop is not None:                       # EPUB 3 form
                    metas.append({
                        "property": prop.strip(),
                        "value": (el.text or "").strip(),
                        "refines": (el.get("refines") or "").strip(),
                        "name": None,
                        "content": None,
                    })
                else:                                      # EPUB 2 form
                    nm = el.get("name")
                    if nm:
                        metas.append({
                            "property": None,
                            "value": "",
                            "refines": "",
                            "name": nm.strip(),
                            "content": (el.get("content") or "").strip(),
                        })
                continue
            if name not in buckets:
                continue
            rec = {
                "value": _norm_ws(el.text or ""),
                "id": el.get("id"),
                "file_as": _attr(el, "file-as", NS_OPF),
                "role": (_attr(el, "role", NS_OPF) or "").strip().lower() or None,
                "scheme": _attr(el, "scheme", NS_OPF),
                "event": (_attr(el, "event", NS_OPF) or "").strip().lower() or None,
            }
            records.append((name, rec))
            if rec["id"]:
                by_id[rec["id"]] = rec

        for meta in metas:                                 # EPUB 3 refinements
            ref = meta.get("refines") or ""
            if not ref.startswith("#"):
                continue
            target = by_id.get(ref[1:])
            if target is None:
                continue
            prop, val = meta["property"], meta["value"]
            if prop == "file-as":
                target["file_as"] = val
            elif prop == "role":
                target["role"] = val.strip().lower()
            elif prop == "title-type":
                target["title_type"] = val.strip().lower()

        for name, rec in records:
            buckets[name].append(rec)

        uid_ref = pkg.get("unique-identifier")
        unique_id = ""
        if uid_ref:
            for rec in buckets["identifier"]:
                if rec["id"] == uid_ref:
                    unique_id = rec["value"]
                    break
            if not unique_id:
                warn.append("unique-identifier=%r matches no dc:identifier" % uid_ref)
        if not unique_id and buckets["identifier"]:
            unique_id = buckets["identifier"][0]["value"]
        self._unique_id = unique_id

        titles = buckets["title"]
        main = next((t for t in titles if t.get("title_type") == "main"), None)
        title = (main or (titles[0] if titles else {})).get("value", "") or ""

        authors = [c["value"] for c in buckets["creator"]
                   if c.get("role") in (None, "aut")]
        if not authors:
            authors = [c["value"] for c in buckets["creator"]]
        authors = [a for a in authors if a]

        dates = buckets["date"]
        dated = next((d for d in dates
                      if d.get("event") in ("publication", "original-publication")), None)
        date = (dated or (dates[0] if dates else {})).get("value", "") or ""

        self.metadata = {
            "title": title,
            "authors": authors,
            "language": (buckets["language"][0]["value"] if buckets["language"] else ""),
            "identifier": unique_id,
            "publisher": (buckets["publisher"][0]["value"] if buckets["publisher"] else ""),
            "date": date,
            "description": (buckets["description"][0]["value"]
                            if buckets["description"] else ""),
            "subjects": [s["value"] for s in buckets["subject"] if s["value"]],
        }
        return metas

    def _parse_manifest(self, pkg: ET.Element) -> None:
        warn = self.warnings
        man = next((c for c in pkg if _local_name(c) == "manifest"), None)
        if man is None:
            warn.append("OPF has no <manifest>")
            return
        for el in _kids(man, "item"):
            iid = el.get("id")
            href = _attr(el, "href", NS_OPF) or ""
            media_type = (el.get("media-type") or "").strip().lower()
            props = frozenset((el.get("properties") or "").split())
            zip_name: str | None = None
            if href and not is_remote(href):
                zip_name, _frag, _remote = _resolve(self._index, self.opf_dir, href)
                if zip_name is None:
                    warn.append("manifest item %r href %r resolves to nothing"
                                % (iid, href))
            if not iid:
                warn.append("manifest item with no id (href=%r) skipped" % href)
                continue
            if iid in self._items:
                # Illegal but real. FIRST wins: the spine's idref was almost
                # certainly authored against the first occurrence.
                warn.append("duplicate manifest id %r; keeping the first" % iid)
                continue
            item = _Item(iid, href, media_type, props, zip_name)
            self._items[iid] = item
            if zip_name:
                self._by_zip_name.setdefault(zip_name, item)

    def _parse_spine(self, pkg: ET.Element) -> str | None:
        warn = self.warnings
        sp = next((c for c in pkg if _local_name(c) == "spine"), None)
        if sp is None:
            warn.append("OPF has no <spine>")
            return None
        seen: set[str] = set()
        for el in _kids(sp, "itemref"):
            idref = el.get("idref") or ""
            item = self._items.get(idref)
            if item is None:
                warn.append("spine itemref idref=%r is not in the manifest; dropped"
                            % idref)
                continue
            if not item.zip_name:
                warn.append("spine item %r points at a missing file %r; dropped"
                            % (idref, item.href))
                continue
            if idref in seen:
                warn.append("spine references %r more than once" % idref)
            seen.add(idref)
            linear = (el.get("linear") or "yes").strip().lower() != "no"
            props = frozenset((el.get("properties") or "").split())
            if "rendition:layout-pre-paginated" in props:
                self._pre_paginated.add(item.zip_name)
            self.spine.append(SpineItem(idref, item.href, item.media_type,
                                        linear, item.zip_name))
        direction = (sp.get("page-progression-direction") or "").strip().lower()
        self.page_direction = direction if direction in ("ltr", "rtl") else "ltr"
        return sp.get("toc")

    # -- TOC ----------------------------------------------------------------

    def _build_toc(self, toc_idref: str | None) -> None:
        nav_item = next((i for i in self._items.values()
                         if "nav" in i.properties and i.zip_name), None)
        if nav_item and nav_item.zip_name:
            entries = self._parse_nav(nav_item.zip_name)
            if entries:
                self.toc = entries
                return

        ncx_name = self._find_ncx(toc_idref)
        if ncx_name:
            entries = self._parse_ncx(ncx_name)
            if entries:
                self.toc = entries
                return

        self.warnings.append("no nav document and no usable NCX; "
                             "the table of contents was built from the spine")
        self.toc = self._toc_from_spine()
        self.toc_is_synthetic = True

    def _find_ncx(self, toc_idref: str | None) -> str | None:
        item = self._items.get(toc_idref) if toc_idref else None
        if item is None or not item.zip_name:
            item = next((i for i in self._items.values()
                         if i.media_type == _NCX_TYPE and i.zip_name), None)
        if item is not None and item.zip_name:
            return item.zip_name
        loose = next((n for n in self._index.names if n.lower().endswith(".ncx")), None)
        if loose:
            self.warnings.append("NCX is not in the manifest; recovered %r" % loose)
        return loose

    def _parse_nav(self, nav_name: str) -> list[TocEntry]:
        try:
            root = parse_xml_lenient(self._read_entry(nav_name), nav_name)
        except Exception as exc:
            self.warnings.append("nav document unparsable: %s" % exc)
            return []
        base = posixpath.dirname(nav_name)

        def walk(ol: ET.Element) -> list[TocEntry]:
            out: list[TocEntry] = []
            for li in _kids(ol, "li"):
                anchor = next((c for c in li if _local_name(c) in ("a", "span")), None)
                if anchor is None:
                    continue
                href = anchor.get("href") or ""
                path, fragment = split_href(href)
                zip_name = ""
                if path:
                    hit, _f, remote = _resolve(self._index, base, href)
                    if hit and not remote:
                        zip_name = hit
                children: list[TocEntry] = []
                for sub in _kids(li, "ol"):
                    children.extend(walk(sub))
                out.append(TocEntry(
                    title=_text_of(anchor) or _fallback_label(path),
                    href=path,
                    fragment=fragment,
                    zip_name=zip_name,
                    children=children,
                ))
            return out

        navs = _find_deep(root, "nav")
        for nav in navs:
            if _nav_type(nav) != "toc":
                continue
            ols = _kids(nav, "ol") or _find_deep(nav, "ol")
            if ols:
                return walk(ols[0])
        # A single untyped <nav> is common in sloppy EPUB 3; better than
        # showing the reader nothing. landmarks / page-list are never used.
        untyped = [n for n in navs if not _nav_type(n)]
        if len(untyped) >= 1:
            ols = _kids(untyped[0], "ol") or _find_deep(untyped[0], "ol")
            if ols:
                self.warnings.append("nav document has no epub:type='toc'; "
                                     "using the first untyped <nav>")
                return walk(ols[0])
        return []

    def _parse_ncx(self, ncx_name: str) -> list[TocEntry]:
        try:
            root = parse_xml_lenient(self._read_entry(ncx_name), ncx_name)
        except Exception as exc:
            self.warnings.append("NCX unparsable: %s" % exc)
            return []
        base = posixpath.dirname(ncx_name)

        def label_of(np: ET.Element) -> str:
            for nav_label in _kids(np, "navLabel"):
                for text in _kids(nav_label, "text"):
                    return _text_of(text)
            return ""

        def src_of(np: ET.Element) -> str:
            for content in _kids(np, "content"):
                return content.get("src") or ""
            return ""

        def walk(parent: ET.Element) -> list[TocEntry]:
            out: list[TocEntry] = []
            # DOCUMENT ORDER IS AUTHORITATIVE. playOrder is informational and
            # is hexadecimal on one of the user's own books, where sorting by
            # the values that do parse actively scrambles the chapters.
            for np in _kids(parent, "navPoint"):
                href = src_of(np)
                path, fragment = split_href(href)
                zip_name = ""
                if path:
                    hit, _f, remote = _resolve(self._index, base, href)
                    if hit and not remote:
                        zip_name = hit
                out.append(TocEntry(
                    title=label_of(np) or _fallback_label(path),
                    href=path,
                    fragment=fragment,
                    zip_name=zip_name,
                    children=walk(np),          # navPoints nest
                ))
            return out

        for nav_map in _find_deep(root, "navMap"):
            return walk(nav_map)
        return []

    def _toc_from_spine(self) -> list[TocEntry]:
        """One entry per linear spine item, labelled from the document."""
        out: list[TocEntry] = []
        for item in self.spine:
            if not item.linear:
                continue
            out.append(TocEntry(
                title=self.doc_title(item.zip_name),
                href=item.href,          # relative to the OPF: there is no TOC doc
                fragment="",
                zip_name=item.zip_name,
                children=[],
            ))
        return out

    # -- cover --------------------------------------------------------------

    def _find_cover(self, pkg: ET.Element) -> str | None:
        # 1. EPUB 3: manifest properties="cover-image"
        for item in self._items.values():
            if "cover-image" in item.properties and item.zip_name:
                return item.zip_name

        # 2. EPUB 2: <meta name="cover" content="ITEM-ID"/>. All three of the
        #    user's real books need this step, so it is not optional legacy.
        meta_el = next((c for c in pkg if _local_name(c) == "metadata"), None)
        if meta_el is not None:
            for el in _kids(meta_el, "meta"):
                if (el.get("name") or "").strip().lower() != "cover":
                    continue
                cid = (el.get("content") or "").strip()
                item = self._items.get(cid)
                if item and item.zip_name:
                    if item.media_type.startswith("image/"):
                        return item.zip_name
                    if item.media_type in _XHTML_TYPES:
                        got = self._first_image_in(item.zip_name)
                        if got:
                            return got
                hit, _f, remote = _resolve(self._index, self.opf_dir, cid)
                if hit and not remote:
                    return hit

        # 3. EPUB 2 guide: <reference type="cover" href="..."/>
        guide = next((c for c in pkg if _local_name(c) == "guide"), None)
        if guide is not None:
            for ref in _kids(guide, "reference"):
                if (ref.get("type") or "").strip().lower() not in ("cover", "coverimagestandard"):
                    continue
                hit, _f, remote = _resolve(self._index, self.opf_dir, ref.get("href") or "")
                if not hit or remote:
                    continue
                if hit.lower().endswith((".xhtml", ".html", ".htm", ".xml")):
                    got = self._first_image_in(hit)
                    if got:
                        return got
                else:
                    return hit

        # 4. filename / id heuristics, images only
        images = [i for i in self._items.values()
                  if i.zip_name and i.media_type.startswith("image/")]
        for item in images:
            base = posixpath.basename(item.zip_name or "").lower()
            if base.startswith("cover") or item.id.lower() in (
                    "cover", "cover-image", "coverimage"):
                return item.zip_name
        for item in images:
            if "cover" in ((item.zip_name or "") + " " + item.id).lower():
                return item.zip_name
        return None

    _IMG_RE = re.compile(
        rb"""<(?:img|image)\b[^>]*?\b(?:src|xlink:href|href)\s*=\s*["']([^"']+)["']""",
        re.I | re.S)

    def _first_image_in(self, zip_name: str) -> str | None:
        try:
            raw = self._read_entry(zip_name)
        except Exception:
            return None
        match = self._IMG_RE.search(raw)
        if not match:
            return None
        href = match.group(1).decode("utf-8", "replace")
        hit, _f, remote = _resolve(self._index, posixpath.dirname(zip_name), href)
        return None if remote else hit

    # -- layout -------------------------------------------------------------

    def _detect_layout(self, metas: Sequence[dict]) -> None:
        layout = next((m["value"] for m in metas
                       if m.get("property") == "rendition:layout"), "reflowable")
        self.is_fixed_layout = (layout or "").strip().lower() == "pre-paginated"
        if self.is_fixed_layout:
            self._pre_paginated.update(s.zip_name for s in self.spine)
        elif self._pre_paginated:
            self.is_fixed_layout = True

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def has(self, zip_name: str) -> bool:
        """True when the archive holds this entry (case-insensitive rescue)."""
        return self._index.lookup(zip_name or "") is not None

    def read(self, zip_name: str) -> bytes:
        """Raw bytes of one archive entry, de-obfuscating fonts transparently.

        Raises :class:`KeyError` when the entry does not exist — matching
        ``zipfile`` and letting a URL scheme handler answer 404 without
        special-casing.
        """
        name = self._index.lookup(zip_name or "")
        if name is None:
            raise KeyError("no such entry in %s: %r" % (self.path, zip_name))
        data = self._read_entry(name)
        algorithm = self._obfuscated.get(name)
        if algorithm:
            try:
                key = obfuscation_key(algorithm, self._unique_id)
            except ValueError as exc:
                self.warnings.append("cannot de-obfuscate %r: %s" % (name, exc))
                return data
            return deobfuscate(data, algorithm, key)
        return data

    def read_text(self, zip_name: str) -> str:
        """Decoded text of one entry. BOM-aware, UTF-8 first, never sniffed."""
        return decode_bytes(self.read(zip_name), warnings=self.warnings,
                            what=zip_name)

    def resolve(self, href: str, base: str = "") -> tuple[str, str | None]:
        """Resolve an href to ``(zip_name, fragment)``.

        ``base`` is the archive entry name of the document the href appeared
        in (for a stylesheet's ``url()`` that is the stylesheet, not the page
        that links it). The default "" means the package document, i.e. the
        base for manifest hrefs.

        ``fragment`` is ``None`` when the href has none and never carries the
        leading ``#``. An href that does not exist in the container comes back
        as the normalised candidate path so the caller can report it; a remote
        URL comes back as ``("", fragment)``.
        """
        href = href or ""
        base_dir = posixpath.dirname(base) if base else self.opf_dir
        path, fragment = split_href(href)
        if is_remote(href):
            return "", (fragment or None)
        if not path:
            # A bare '#anchor' addresses the base document itself.
            target = self._index.lookup(base) or base
            return target, (fragment or None)
        hit, _frag, remote = _resolve(self._index, base_dir, href)
        if remote:
            return "", (fragment or None)
        if hit is None:
            hit = normalize_zip_path(base_dir, unquote(path))
        return hit, (fragment or None)

    def spine_index(self, zip_name: str) -> int | None:
        """Position of a document in the spine, or None if it is not in it."""
        name = self._index.lookup(zip_name or "") or zip_name
        return self._spine_pos.get(name)

    def plain_text(self, zip_name: str) -> str:
        """Flattened document text — CONTRACT.md section 2.1.

        Character-for-character what ``reader.js`` produces in Chromium for
        the same document. A document that cannot be read comes back as "" and
        adds a line to :attr:`warnings`: one bad chapter never breaks a book.
        """
        name = self._index.lookup(zip_name or "") or zip_name
        cached = self._text_cache.get(name)
        if cached is not None:
            return cached
        try:
            source = self.read_text(name)
            text = flatten_html(source)
        except Exception as exc:
            self.warnings.append("cannot extract text from %r: %s: %s"
                                 % (zip_name, type(exc).__name__, exc))
            text = ""
        self._text_cache[name] = text
        self._text_order.append(name)
        if len(self._text_order) > self._PLAIN_TEXT_CACHE:
            self._text_cache.pop(self._text_order.pop(0), None)
        return text

    def doc_title(self, zip_name: str) -> str:
        """A usable label for one document: <title>, else h1-h3, else name."""
        name = self._index.lookup(zip_name or "") or zip_name
        cached = self._title_cache.get(name)
        if cached is not None:
            return cached
        title = ""
        try:
            parser = _DocTitleParser()
            parser.feed(self.read_text(name))
            parser.close()
            title = parser.title or parser.heading
        except Exception as exc:
            self.warnings.append("cannot read a title from %r: %s" % (zip_name, exc))
        if not title:
            title = _fallback_label(name)
        self._title_cache[name] = title
        return title

    def units(self, zip_name: str) -> int:
        """Reading units in one document: CJK characters + Latin words."""
        name = self._index.lookup(zip_name or "") or zip_name
        cached = self._units_cache.get(name)
        if cached is None:
            cached = count_units(self.plain_text(name))
            self._units_cache[name] = cached
        return cached

    def total_units(self) -> int:
        """Reading units across the whole spine. Cached after the first call."""
        if self._total_units is None:
            self._total_units = sum(self.units(s.zip_name) for s in self.spine)
        return self._total_units

    def cover_bytes(self) -> bytes | None:
        """Bytes of the cover image, or None when the book has no cover."""
        if not self.cover:
            return None
        try:
            return self.read(self.cover)
        except Exception as exc:
            self.warnings.append("cover %r is unreadable: %s" % (self.cover, exc))
            return None

    def search(self, query: str, limit: int = 2000) -> list[SearchHit]:
        """Book-wide literal search in spine order, folded like ``reader.js``.

        The query is plain text: regular-expression metacharacters have no
        meaning. Surrounding whitespace is stripped from it, and an empty (or
        whitespace-only) query returns ``[]``. Both sides are compared through
        :func:`fold_for_search`, which is exactly ``fold()`` in reader.js
        (case-insensitive, curly quotes and dashes match their ASCII forms),
        so this finds what the in-page search finds.

        ``gpos``/``length`` are code-point offsets into :meth:`plain_text` of
        ``zip_name`` and go straight to ``reader.js``. ``match`` is the exact
        original text; ``before``/``after`` are up to 40 code points either
        side with whitespace runs shown as one space. Stops after ``limit``
        hits; matches do not overlap.
        """
        needle = fold_for_search((query or "").strip())
        if not needle or limit <= 0:
            return []
        size = len(needle)
        titles = self._chapter_title_map()
        hits: list[SearchHit] = []
        for index, item in enumerate(self.spine):
            text = self.plain_text(item.zip_name)
            if len(text) < size:
                continue
            haystack = fold_for_search(text)
            start = haystack.find(needle)
            if start < 0:
                continue
            chapter = titles.get(item.zip_name) or self.doc_title(item.zip_name)
            while start >= 0:
                end = start + size
                hits.append(SearchHit(
                    spine_index=index,
                    zip_name=item.zip_name,
                    gpos=start,
                    length=size,
                    before=_context(text[max(0, start - _SEARCH_CONTEXT):start]).lstrip(" "),
                    match=text[start:end],
                    after=_context(text[end:end + _SEARCH_CONTEXT]).rstrip(" "),
                    chapter_title=chapter,
                ))
                if len(hits) >= limit:
                    return hits
                start = haystack.find(needle, end)
        return hits

    # -- extras (documented in docs/api-epublib.md) -------------------------

    def media_type(self, zip_name: str) -> str:
        """The manifest's declared media type, else a guess from the suffix."""
        name = self._index.lookup(zip_name or "") or zip_name
        item = self._by_zip_name.get(name)
        if item and item.media_type:
            return item.media_type
        ext = posixpath.splitext(name)[1].lower()
        if ext in _MIME_EXTRA:
            return _MIME_EXTRA[ext]
        return mimetypes.guess_type(name)[0] or "application/octet-stream"

    def is_pre_paginated(self, zip_name: str) -> bool:
        """True when this document must be scaled to fit, never paginated."""
        name = self._index.lookup(zip_name or "") or zip_name
        return name in self._pre_paginated

    def viewport(self, zip_name: str) -> tuple[int, int] | None:
        """Fixed-layout page size from the FIRST viewport meta, or SVG viewBox."""
        try:
            parser = _ViewportParser()
            parser.feed(self.read_text(zip_name))
            parser.close()
        except Exception:
            return None
        if parser.content:
            found = {k.lower(): v for k, v in _VIEWPORT_NUM.findall(parser.content)}
            if "width" in found and "height" in found:
                try:
                    return int(float(found["width"])), int(float(found["height"]))
                except ValueError:
                    return None
        if parser.svg_viewbox:
            parts = parser.svg_viewbox.replace(",", " ").split()
            if len(parts) == 4:
                try:
                    return int(float(parts[2])), int(float(parts[3]))
                except ValueError:
                    return None
        return None

    def flat_text_fingerprint(self, zip_name: str) -> dict:
        """Everything the cross-engine check needs to diff Python against JS.

        ``reader.js`` exposes ``epubReader.flatText()``; comparing ``length``
        and ``sha256`` first localises a mismatch far faster than diffing 40 KB
        of Chinese prose, and ``head``/``tail`` show where it starts.
        """
        text = self.plain_text(zip_name)
        return {
            "zip_name": self._index.lookup(zip_name or "") or zip_name,
            "length": len(text),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "head": text[:120],
            "tail": text[-120:],
        }

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _read_entry(self, name: str) -> bytes:
        with self._lock:
            if self._closed:
                raise EpubError("the book is closed", kind="corrupt",
                                detail="read(%r) after close()" % name)
            return self._zf.read(name)

    def _chapter_title_map(self) -> dict[str, str]:
        if self._chapter_titles is None:
            mapping: dict[str, str] = {}

            def walk(entries: Iterable[TocEntry]) -> None:
                for entry in entries:
                    if entry.zip_name and entry.zip_name not in mapping and entry.title:
                        mapping[entry.zip_name] = entry.title
                    walk(entry.children)

            walk(self.toc)
            self._chapter_titles = mapping
        return self._chapter_titles


# --------------------------------------------------------------------------
# module-level helpers used by EpubBook.open
# --------------------------------------------------------------------------


def _empty_metadata() -> dict:
    return {"title": "", "authors": [], "language": "", "identifier": "",
            "publisher": "", "date": "", "description": "", "subjects": []}


def _fallback_label(zip_name: str) -> str:
    base = posixpath.basename(zip_name or "")
    return posixpath.splitext(base)[0] or (base or "(untitled)")


def _nav_type(el: ET.Element) -> str:
    """epub:type, across namespaced and raw-attribute parses.

    After the html.parser recovery path the attribute keeps its RAW spelling,
    so it is the literal key 'epub:type'. Checking only the namespaced form
    silently yields no TOC for every recovered nav document.
    """
    value = (el.get("{%s}type" % NS_EPUB) or el.get("epub:type")
             or el.get("type") or "")
    return value.strip().lower()


def _drm_scheme(index: _ZipIndex) -> str:
    if index.lookup("META-INF/license.lcpl"):
        return "lcp"
    if index.lookup("META-INF/rights.xml"):
        return "adept"
    return "unknown"


def _open_container(path: str) -> zipfile.ZipFile:
    """Open the archive, repairing cp437-mojibake CJK entry names.

    OCF requires UTF-8 names, but old Windows zip tools clear the UTF-8
    general-purpose flag (bit 11 / 0x800) and write raw GBK/Shift-JIS bytes;
    zipfile then decodes them as cp437 and no href will ever match.
    """
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise EpubError("the file is not a readable zip archive", kind="corrupt",
                        detail="BadZipFile: %s" % exc) from exc
    except zipfile.LargeZipFile as exc:
        raise EpubError("the archive needs ZIP64 support", kind="corrupt",
                        detail="LargeZipFile: %s" % exc) from exc

    try:
        suspect = any(not (i.flag_bits & 0x800) and any(ord(c) > 127 for c in i.filename)
                      for i in zf.infolist())
    except Exception as exc:  # a central directory we cannot even walk
        zf.close()
        raise EpubError("the archive directory is damaged", kind="corrupt",
                        detail="%s: %s" % (type(exc).__name__, exc)) from exc
    if not suspect:
        return zf

    for encoding in ("utf-8", "gbk", "shift_jis", "cp949", "cp1252"):
        try:
            candidate = zipfile.ZipFile(path, metadata_encoding=encoding)
        except (LookupError, ValueError, zipfile.BadZipFile):
            continue
        bad = sum(1 for info in candidate.infolist() for c in info.filename
                  if "\u2500" <= c <= "\u25ff" or c == "\ufffd")
        if bad == 0:
            zf.close()
            return candidate
        candidate.close()
    return zf


def _guard_zip_bomb(zf: zipfile.ZipFile, path: str) -> None:
    """Run on infolist() metadata BEFORE reading anything."""
    infos = [i for i in zf.infolist() if not i.is_dir()]
    total = sum(i.file_size for i in infos)
    packed = sum(i.compress_size for i in infos) or 1
    if total > _MAX_UNCOMPRESSED:
        raise EpubError(
            "the file would expand to %.1f GB and was not opened"
            % (total / 1024 ** 3),
            kind="too_large",
            detail="%d entries, %d bytes uncompressed" % (len(infos), total),
        )
    if total > _BOMB_MIN_BYTES and total / packed > _BOMB_RATIO:
        raise EpubError(
            "the file expands %.0f times and was not opened" % (total / packed),
            kind="too_large",
            detail="%d bytes packed -> %d bytes unpacked" % (packed, total),
        )


def _check_mimetype(zf: zipfile.ZipFile, warn: list[str]) -> None:
    """Every OCF mimetype rule, as warnings only. Real books break them all."""
    infos = zf.infolist()
    if not infos:
        warn.append("the archive is empty")
        return
    first = infos[0]
    if first.filename != "mimetype":
        warn.append("mimetype is not the first entry (first is %r)" % first.filename)
    else:
        if first.compress_type != zipfile.ZIP_STORED:
            warn.append("mimetype is compressed; it must be stored")
        if first.extra:
            warn.append("mimetype carries an extra field; it must have none")
    try:
        data = zf.read("mimetype")
    except KeyError:
        warn.append("mimetype entry is missing")
        return
    if data != b"application/epub+zip":
        if data.strip() == b"application/epub+zip":
            warn.append("mimetype has leading or trailing whitespace")
        else:
            warn.append("mimetype content is %r" % data[:40])


# --------------------------------------------------------------------------
# command line: dump flattened text for the cross-engine diff
# --------------------------------------------------------------------------


def _iter_spine(book: EpubBook, only: str | None) -> Iterator[str]:
    if only:
        yield book._index.lookup(only) or only
        return
    for item in book.spine:
        yield item.zip_name


def plain_text_report(book: EpubBook, zip_name: str) -> dict:
    """The §2.1 invariant record for one document, as ``--plain-text`` prints it.

    ``text`` is :meth:`EpubBook.plain_text`. ``length`` counts Unicode code
    points (the unit of every ``gpos``); ``utf16_length`` is what JavaScript's
    ``String.length`` reports for the same text, and ``astral`` is the number
    of code points above U+FFFF (the difference between the two).
    """
    name = book._index.lookup(zip_name or "") or zip_name
    text = book.plain_text(name)
    astral = sum(1 for ch in text if ord(ch) > 0xFFFF)
    return {
        "book": book.path,
        "zip_name": name,
        "unit": "codepoint",
        "length": len(text),
        "utf16_length": len(text) + astral,
        "astral": astral,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "text": text,
    }


def _plain_text_cli(argv: list[str]) -> int:
    """``--plain-text <book.epub> [zip_name]`` -> JSON on stdout.

    With a ``zip_name``: one :func:`plain_text_report` object. Without: a JSON
    array with one object per spine item, in spine order. The JSON is
    ASCII-only (``\\uXXXX`` escapes), so no console code page can corrupt the
    Chinese text on its way to the diff. Exit status 0 on success, 2 when the
    book cannot be opened or the entry does not exist (with an ``error``
    object on stdout).
    """
    import json
    import sys

    def emit(payload: object) -> None:
        data = json.dumps(payload, ensure_ascii=True, indent=None)
        sys.stdout.write(data + "\n")
        sys.stdout.flush()

    if len(argv) not in (1, 2):
        sys.stderr.write("usage: python epublib.py --plain-text <book.epub> [zip_name]\n")
        return 2
    try:
        book = EpubBook.open(argv[0])
    except EpubError as exc:
        emit({"error": str(exc), "kind": exc.kind, "detail": exc.detail})
        return 2
    except OSError as exc:
        emit({"error": str(exc), "kind": "os_error"})
        return 2
    with book:
        if len(argv) == 1:
            emit([plain_text_report(book, item.zip_name) for item in book.spine])
            return 0
        if not book.has(argv[1]):
            emit({"error": "no such entry in the book: %r" % argv[1], "kind": "no_entry"})
            return 2
        emit(plain_text_report(book, argv[1]))
    return 0


def _main(argv: list[str]) -> int:  # pragma: no cover - developer tool
    import argparse
    import json
    import sys

    if argv and argv[0] == "--plain-text":
        return _plain_text_cli(argv[1:])

    parser = argparse.ArgumentParser(
        prog="epublib",
        description="Inspect an EPUB and dump the flattened text that must "
                    "match assets/reader.js (CONTRACT.md section 2.1).")
    parser.add_argument("command", choices=("info", "flat", "fingerprint"))
    parser.add_argument("book")
    parser.add_argument("entry", nargs="?", default=None,
                        help="a single zip entry; default is the whole spine")
    parser.add_argument("--out", default=None,
                        help="directory to write one .txt per document into")
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    with EpubBook.open(args.book) as book:
        if args.command == "info":
            print(json.dumps({
                "path": book.path,
                "opf_path": book.opf_path,
                "opf_dir": book.opf_dir,
                "version": book.version,
                "metadata": book.metadata,
                "spine": len(book.spine),
                "toc_top_level": len(book.toc),
                "toc_is_synthetic": book.toc_is_synthetic,
                "cover": book.cover,
                "is_fixed_layout": book.is_fixed_layout,
                "page_direction": book.page_direction,
                "total_units": book.total_units(),
                "warnings": book.warnings,
            }, ensure_ascii=False, indent=2))
            return 0

        if args.command == "fingerprint":
            for name in _iter_spine(book, args.entry):
                print(json.dumps(book.flat_text_fingerprint(name),
                                 ensure_ascii=False))
            return 0

        for name in _iter_spine(book, args.entry):
            text = book.plain_text(name)
            if args.out:
                os.makedirs(args.out, exist_ok=True)
                safe = name.replace("/", "_").replace("\\", "_")
                target = os.path.join(args.out, safe + ".txt")
                with open(target, "w", encoding="utf-8", newline="") as handle:
                    handle.write(text)
                print("%s -> %s (%d chars)" % (name, target, len(text)))
            else:
                print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    raise SystemExit(_main(sys.argv[1:]))
