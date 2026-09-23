# -*- coding: utf-8 -*-
"""Books to LaTeX, and through XeLaTeX to PDF.

Any book the reader opens as an EPUB (EPUB itself, and MOBI / AZW / AZW3 through
their cached conversion) can be written out as a LaTeX project::

    <destination>/<title>/<title>.tex     the source, readable and editable
    <destination>/<title>/images/...      every picture the text uses
    <destination>/<title>/<title>.pdf     typeset by XeLaTeX, when one is installed

The converter walks the spine in reading order and maps the XHTML to LaTeX:
the book's contents list becomes ``\\chapter*`` / ``\\section*`` headings (with
PDF bookmarks and a table of contents), paragraphs, emphasis, lists, block
quotes, tables (``longtable``), pictures, footnotes (EPUB 3 ``noteref`` /
``footnote`` and the common numbered-link pattern), links and ruby.  A small
CSS reader picks up what simple class rules say about italics, bold, small
capitals, centring and hidden elements.

Fonts are chosen per book: Chinese uses ``ctexbook`` (SimSun, SimHei, KaiTi on
Windows), Japanese and Korean use xeCJK with a local font, and the Latin font is
the first of Latin Modern, Cambria and Times New Roman that has every letter in
the book.  Characters no chosen font has are sent to a fallback font found by
reading the installed fonts' character maps, so text does not silently vanish.

Typesetting runs in a private temporary folder; only the PDF is copied back, so
no ``.aux`` / ``.log`` / ``.out`` files are ever left beside the source.

Public API::

    ExportOptions, ExportResult, ExportCancelled
    export_book(book, destination, options=None, *, progress=None, cancelled=None) -> ExportResult
    write_latex(book, folder, stem, options=None, *, progress=None, cancelled=None) -> ExportResult
    compile_pdf(tex_path, pdf_path, *, progress=None, cancelled=None, engine=None) -> CompileResult
    find_xelatex() -> str | None
"""

from __future__ import annotations

import bisect
import glob
import html.entities
import io
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Callable, Iterator
from urllib.parse import unquote

from lxml import etree
from lxml import html as lxml_html

from epublib import EpubBook, is_remote

__all__ = [
    "ExportOptions", "ExportResult", "CompileResult", "ExportCancelled", "PAPERS",
    "export_book", "write_latex", "compile_pdf", "find_xelatex", "safe_stem",
]

Progress = Callable[[str, int, int], None]
Cancelled = Callable[[], bool]
#: A stand-in for XeLaTeX: (tex, pdf, progress=…, cancelled=…) -> CompileResult.
Typesetter = Callable[..., "CompileResult"]


class ExportCancelled(Exception):
    """The user cancelled; nothing half-written is left behind."""


#: name -> (width, height, inner, outer, top, bottom) in millimetres
PAPERS: dict[str, tuple[float, float, float, float, float, float]] = {
    "a4": (210, 297, 25, 25, 25, 27),
    "a5": (148, 210, 17, 15, 18, 20),
    "b5": (176, 250, 20, 18, 20, 22),
    "letter": (215.9, 279.4, 25, 25, 25, 27),
    "6x9": (152.4, 228.6, 18, 16, 18, 20),
}
_PT_PER_MM = 72.27 / 25.4

#: Raster pictures inside EPUBs are drawn for the browser's default ~16 px text
#: context (= 12 pt at 96 dpi), so their pixel sizes are relative to that font,
#: not to the export's.  Formulas are commonly shipped as little GIF/PNG images;
#: scaling every picture by ``font_size / 12`` keeps them consistent with the
#: running text whatever point size the reader chose.
_EPUB_REF_FONT_PT = 12.0


@dataclass
class ExportOptions:
    paper: str = "a5"
    font_size: int = 11          # 10, 11 or 12 (pt)
    cover: bool = True           # the cover picture as the first page
    contents: bool = True        # a table of contents after the cover
    compile_pdf: bool = True     # run XeLaTeX (when installed)
    #: Which fonts the document may name.  "system" uses the fonts installed on this
    #: computer (Windows); "texlive" uses only fonts that come with the TeX
    #: distribution itself, which is what the Android build has.
    fonts: str = "system"


@dataclass
class CompileResult:
    ok: bool                     # a PDF was produced
    pdf_path: str | None
    pages: int = 0
    passes: int = 0
    seconds: float = 0.0
    engine: str | None = None
    problems: list[str] = field(default_factory=list)   # TeX errors (the PDF may still be fine)
    missing_chars: int = 0


@dataclass
class ExportResult:
    folder: str
    tex_path: str
    pdf_path: str | None = None
    images: int = 0
    chapters: int = 0
    pages: int = 0
    engine: str | None = None            # the XeLaTeX that typeset it; None when not compiled
    compiled: bool = False               # compilation was attempted
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    estimate_pages: int = 0                # a rough page count, for progress before the first pass


# ==========================================================================
# names and small helpers
# ==========================================================================

_BAD_FILE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_stem(name: str, fallback: str = "book") -> str:
    """A file/folder name for ``name`` that Windows accepts (at most 80 characters)."""
    s = _BAD_FILE_CHARS.sub(" ", unicodedata.normalize("NFC", name or ""))
    s = " ".join(s.split()).strip(" .")
    if len(s) > 80:
        s = s[:80].rstrip(" .")
    if s.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)),
                                    *(f"LPT{i}" for i in range(10))}:
        s = "_" + s
    return s or fallback


def _unique_dir(parent: str, stem: str) -> str:
    path = os.path.join(parent, stem)
    n = 2
    while os.path.exists(path):
        path = os.path.join(parent, f"{stem} ({n})")
        n += 1
    return path


def _check(cancelled: Cancelled | None) -> None:
    if cancelled is not None and cancelled():
        raise ExportCancelled()


XHTML_NS = "http://www.w3.org/1999/xhtml"
EPUB_NS = "http://www.idpf.org/2007/ops"
SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"


def _local(el: etree._Element) -> str:
    tag = el.tag
    if not isinstance(tag, str):
        return ""
    if tag[:1] == "{":
        tag = tag.split("}", 1)[1]
    elif ":" in tag:
        tag = tag.split(":", 1)[1]
    return tag.lower()


def _epub_types(el: etree._Element) -> set[str]:
    v = el.get(f"{{{EPUB_NS}}}type") or el.get("epub:type") or ""
    kinds = set(v.split())
    for r in (el.get("role") or "").split():
        if r.startswith("doc-"):
            kinds.add(r[4:])
    return kinds


def _el_id(el: etree._Element) -> str | None:
    v = el.get("id")
    if not v and _local(el) == "a":
        v = el.get("name")
    return v or None


def _href(el: etree._Element) -> str | None:
    return el.get("href") or el.get(f"{{{XLINK_NS}}}href") or el.get("xlink:href")


def _norm_title(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).casefold()
    return "".join(ch for ch in s if unicodedata.category(ch)[0] in "LN")


# ==========================================================================
# parsing
# ==========================================================================

_XML_DECL = re.compile(r"^\s*<\?xml[^>]*\?>", re.S)
_ENTITY = re.compile(r"&([A-Za-z][A-Za-z0-9]{1,31});")
_XML_ENTITIES = {"lt", "gt", "amp", "quot", "apos"}


def _numeric_entities(text: str) -> str:
    def sub(m: "re.Match[str]") -> str:
        name = m.group(1)
        if name in _XML_ENTITIES:
            return m.group(0)
        v = html.entities.html5.get(name + ";")
        if v is None:
            return m.group(0)
        return "".join(f"&#{ord(c)};" for c in v)
    return _ENTITY.sub(sub, text)


def parse_document(text: str) -> etree._Element:
    """XHTML with the XML parser; anything malformed with the forgiving HTML parser."""
    body = _numeric_entities(_XML_DECL.sub("", text, count=1))
    try:
        parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True,
                                 load_dtd=False, recover=False)
        return etree.fromstring(body.encode("utf-8"), parser)
    except etree.XMLSyntaxError:
        pass
    try:
        return lxml_html.document_fromstring(body)
    except (etree.ParserError, ValueError):
        return lxml_html.document_fromstring("<html><body></body></html>")


def _body(root: etree._Element) -> etree._Element:
    for el in root.iter():
        if _local(el) == "body":
            return el
    return root


# ==========================================================================
# a small CSS reader (simple selectors, the properties that matter on paper)
# ==========================================================================

_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_SIMPLE_SEL = re.compile(r"^([a-zA-Z][\w-]*|\*)?((?:[.#][\w-]+)*)$")
_KEEP_PROPS = {"font-style", "font-weight", "font-variant", "text-align", "display", "font-size",
               "text-decoration", "text-decoration-line", "vertical-align", "page-break-before",
               "break-before", "page-break-after", "break-after", "list-style-type", "list-style",
               "font-family", "visibility", "width"}


def _css_blocks(css: str) -> Iterator[tuple[str, str]]:
    i, n = 0, len(css)
    while i < n:
        j = css.find("{", i)
        if j < 0:
            return
        prelude = css[i:j]
        if ";" in prelude:                       # @import / @charset statements before it
            prelude = prelude.rsplit(";", 1)[1]
        prelude = prelude.strip()
        depth, k = 1, j + 1
        while k < n and depth:
            c = css[k]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            k += 1
        inner = css[j + 1:k - 1]
        low = prelude.lower()
        if low.startswith("@"):
            if (low.startswith("@media") or low.startswith("@supports")) and "amzn-mobi" not in low:
                yield from _css_blocks(inner)
        else:
            yield prelude, inner
        i = k


def _css_decls(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in text.split(";"):
        if ":" not in part:
            continue
        k, v = part.split(":", 1)
        k = k.strip().lower()
        if k in _KEEP_PROPS:
            out[k] = v.replace("!important", "").strip().lower()
    return out


class _Styles:
    """Rules from one set of style sheets; ``style(el)`` gives the matched properties."""

    def __init__(self) -> None:
        self._rules: list[tuple[tuple[int, int, int], int, str | None, frozenset[str], str | None,
                                dict[str, str]]] = []
        self._cache: dict[tuple, dict[str, str]] = {}

    def add(self, css: str) -> None:
        css = _CSS_COMMENT.sub("", css)
        for prelude, inner in _css_blocks(css):
            decls = _css_decls(inner)
            if not decls:
                continue
            for sel in prelude.split(","):
                m = _SIMPLE_SEL.match(sel.strip())
                if not m or not sel.strip():
                    continue
                tag = (m.group(1) or "").lower()
                tag = None if tag in ("", "*") else tag
                parts = re.findall(r"[.#][\w-]+", m.group(2) or "")
                classes = frozenset(p[1:] for p in parts if p[0] == ".")
                ident = next((p[1:] for p in parts if p[0] == "#"), None)
                spec = (1 if ident else 0, len(classes), 1 if tag else 0)
                self._rules.append((spec, len(self._rules), tag, classes, ident, decls))

    def style(self, el: etree._Element, tag: str) -> dict[str, str]:
        classes = frozenset((el.get("class") or "").split())
        ident = el.get("id")
        key = (tag, classes, ident)
        hit = self._cache.get(key)
        if hit is None:
            matched = [r for r in self._rules
                       if (r[2] is None or r[2] == tag) and r[3] <= classes and (r[4] is None or r[4] == ident)]
            matched.sort(key=lambda r: (r[0], r[1]))
            hit = {}
            for r in matched:
                # a bare tag rule never sets a size or alignment for the whole book's body text
                d = r[5]
                if r[0] == (0, 0, 1) and tag in ("p", "div", "body", "html", "span"):
                    d = {k: v for k, v in d.items() if k not in ("font-size", "text-align")}
                hit.update(d)
            self._cache[key] = hit
        inline = el.get("style")
        if inline:
            merged = dict(hit)
            merged.update(_css_decls(inline))
            return merged
        return hit


_SIZE_WORDS = {"xx-small": 0.6, "x-small": 0.7, "small": 0.85, "smaller": 0.85, "medium": 1.0,
               "large": 1.2, "larger": 1.2, "x-large": 1.5, "xx-large": 2.0}


def _size_cmd(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip()
    ratio = _SIZE_WORDS.get(v)
    if ratio is None:
        m = re.match(r"^([\d.]+)\s*(em|rem|%)$", v)
        if not m:
            return None
        try:
            ratio = float(m.group(1)) / (100.0 if m.group(2) == "%" else 1.0)
        except ValueError:
            return None
    if ratio < 0.72:
        return r"\footnotesize"
    if ratio < 0.92:
        return r"\small"
    if ratio <= 1.1:
        return None
    if ratio < 1.3:
        return r"\large"
    if ratio < 1.6:
        return r"\Large"
    if ratio < 2.0:
        return r"\LARGE"
    return r"\huge"


_FONT_SIZE_ATTR = {"1": r"\footnotesize", "2": r"\small", "4": r"\large", "5": r"\Large",
                   "6": r"\LARGE", "7": r"\huge", "-2": r"\footnotesize", "-1": r"\small",
                   "+1": r"\large", "+2": r"\Large", "+3": r"\LARGE", "+4": r"\huge"}


# ==========================================================================
# fonts: which installed font has which characters
# ==========================================================================

def _ranges_from_font(path: str, index: int = 0) -> list[tuple[int, int]]:
    """Code point ranges in a TrueType/OpenType font (or one font of a collection)."""
    with open(path, "rb") as f:
        head = f.read(12)
        base = 0
        if head[:4] == b"ttcf":
            count = struct.unpack(">I", head[8:12])[0]
            f.seek(12 + 4 * max(0, min(index, count - 1)))
            base = struct.unpack(">I", f.read(4))[0]
            f.seek(base)
            head = f.read(12)
        num = struct.unpack(">H", head[4:6])[0]
        f.seek(base + 12)
        table = f.read(16 * num)
        cmap_off = cmap_len = None
        for i in range(num):
            tag, _chk, off, length = struct.unpack(">4sIII", table[16 * i:16 * i + 16])
            if tag == b"cmap":
                cmap_off, cmap_len = off, length
        if cmap_off is None:
            return []
        f.seek(cmap_off)
        cmap = f.read(cmap_len)
    nsub = struct.unpack(">H", cmap[2:4])[0]
    best = None
    rank = {(3, 10): 0, (0, 4): 1, (0, 6): 1, (3, 1): 2, (0, 3): 3, (0, 1): 4, (3, 0): 5}
    for i in range(nsub):
        plat, enc, off = struct.unpack(">HHI", cmap[4 + 8 * i:12 + 8 * i])
        r = rank.get((plat, enc))
        if r is not None and (best is None or r < best[0]):
            best = (r, off)
    if best is None:
        return []
    off = best[1]
    fmt = struct.unpack(">H", cmap[off:off + 2])[0]
    ranges: list[tuple[int, int]] = []
    if fmt == 12:
        ngroups = struct.unpack(">I", cmap[off + 12:off + 16])[0]
        for g in range(ngroups):
            s, e, _gid = struct.unpack(">III", cmap[off + 16 + 12 * g:off + 28 + 12 * g])
            ranges.append((s, e))
    elif fmt == 4:
        segx2 = struct.unpack(">H", cmap[off + 6:off + 8])[0]
        seg = segx2 // 2
        ends = struct.unpack(f">{seg}H", cmap[off + 14:off + 14 + segx2])
        starts = struct.unpack(f">{seg}H", cmap[off + 16 + segx2:off + 16 + 2 * segx2])
        for s, e in zip(starts, ends):
            if s != 0xFFFF:
                ranges.append((s, e))
    ranges.sort()
    return ranges


class _Font:
    def __init__(self, family: str, path: str, index: int) -> None:
        self.family, self.path, self.index = family, path, index
        self._starts: list[int] | None = None
        self._ends: list[int] = []

    def _load(self) -> None:
        try:
            r = _ranges_from_font(self.path, self.index)
        except (OSError, struct.error, ValueError):
            r = []
        self._starts = [a for a, _ in r]
        self._ends = [b for _, b in r]

    def has(self, cp: int) -> bool:
        if self._starts is None:
            self._load()
        assert self._starts is not None
        i = bisect.bisect_right(self._starts, cp) - 1
        return i >= 0 and cp <= self._ends[i]


_catalog_lock = threading.Lock()
_catalog: dict[str, _Font] | None = None


def _font_catalog() -> dict[str, _Font]:
    """Installed font families (Windows registry) plus Latin Modern from the TeX tree."""
    global _catalog
    with _catalog_lock:
        if _catalog is not None:
            return _catalog
        fonts: dict[str, _Font] = {}
        if sys.platform == "win32":
            import winreg
            fonts_dir = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
            sub = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    key = winreg.OpenKey(hive, sub)
                except OSError:
                    continue
                with key:
                    i = 0
                    while True:
                        try:
                            name, value, _t = winreg.EnumValue(key, i)
                        except OSError:
                            break
                        i += 1
                        if not isinstance(value, str) or not value.lower().endswith((".ttf", ".ttc", ".otf")):
                            continue
                        path = value if os.path.isabs(value) else os.path.join(fonts_dir, value)
                        clean = re.sub(r"\s*\((TrueType|OpenType)\)\s*$", "", name)
                        for idx, fam in enumerate(clean.split(" & ")):
                            fam = fam.strip()
                            for alias in (fam, re.sub(r"\s+Regular$", "", fam)):
                                fonts.setdefault(alias, _Font(alias, path, idx))
        lm = _kpsewhich("lmroman10-regular.otf")
        if lm:
            fonts["Latin Modern Roman"] = _Font("Latin Modern Roman", lm, 0)
        _catalog = fonts
        return fonts


_kpse_cache: dict[str, str | None] = {}


def _kpsewhich(name: str) -> str | None:
    if name in _kpse_cache:
        return _kpse_cache[name]
    found = None
    engine = find_xelatex()
    if engine:
        exe = os.path.join(os.path.dirname(engine), "kpsewhich" + (".exe" if sys.platform == "win32" else ""))
        if os.path.isfile(exe):
            try:
                res = subprocess.run([exe, name], capture_output=True, text=True, timeout=30,
                                     creationflags=_NO_WINDOW)
                p = res.stdout.strip()
                if p and os.path.isfile(p):
                    found = os.path.normpath(p)
            except (OSError, subprocess.SubprocessError):
                pass
    _kpse_cache[name] = found
    return found


def _is_cjk(cp: int) -> bool:
    """Characters xeCJK typesets with the CJK font."""
    return (0x1100 <= cp <= 0x11FF or 0x2E80 <= cp <= 0x2FFF or 0x3000 <= cp <= 0x9FFF
            or 0xA960 <= cp <= 0xA97F or 0xAC00 <= cp <= 0xD7FF or 0xF900 <= cp <= 0xFAFF
            or 0xFE10 <= cp <= 0xFE1F or 0xFE30 <= cp <= 0xFE4F or 0xFF00 <= cp <= 0xFFEF
            or 0x20000 <= cp <= 0x3FFFF)


_CJK_PUNCT = {0x2014, 0x2018, 0x2019, 0x201C, 0x201D, 0x2026}   # full width in CJK books
_LATIN_CANDIDATES = ("Latin Modern Roman", "Cambria", "Times New Roman", "Georgia", "Segoe UI")
_FALLBACK_CANDIDATES = ("Cambria", "Segoe UI Symbol", "Cambria Math", "Segoe UI", "Arial", "Times New Roman",
                        "Segoe UI Historic", "Nirmala UI", "Ebrima", "Gadugi", "Leelawadee UI",
                        "Microsoft Himalaya", "Mongolian Baiti", "Microsoft Yi Baiti", "Myanmar Text",
                        "Javanese Text", "Sylfaen", "Segoe UI Emoji", "SimSun", "Malgun Gothic")
_CJK_FONTS = {
    "zh-hans": ("SimSun", "Microsoft YaHei", "DengXian", "SimHei"),
    "zh-hant": ("PMingLiU", "MingLiU", "SimSun", "Microsoft JhengHei"),   # a Ming/Song face first
    "ja": ("Yu Mincho", "MS Mincho", "Yu Gothic", "MS Gothic", "Meiryo", "SimSun"),
    "ko": ("Batang", "Malgun Gothic", "Gulim", "SimSun"),
}
_CJK_BOLD = {"zh-hans": ("SimHei", "Microsoft YaHei"), "zh-hant": ("Microsoft JhengHei",),
             "ja": ("Yu Gothic", "MS Gothic", "Meiryo"), "ko": ("Malgun Gothic",)}
_CJK_EXT_FALLBACK = ("SimSun-ExtB", "SimSun-ExtG", "MingLiU-ExtB", "PMingLiU-ExtB")
#: Fonts that come with TeX itself, named by file so no font library is needed:
#: (main, bold, italic).  Verified present in the Tectonic/TeX Live package tree.
_TEX_CJK_FONTS = {
    "zh-hans": ("FandolSong-Regular.otf", "FandolHei-Regular.otf", "FandolKai-Regular.otf"),
    "zh-hant": ("bsmi00lp.ttf", None, "bkai00mp.ttf"),          # arphic Mingti / Kaiti (Big5)
    "ja": ("HaranoAjiMincho-Regular.otf", "HaranoAjiGothic-Regular.otf", None),
    "ko": ("batang.ttf", None, None),                            # baekmuk
}
_TEX_LATIN_WIDE = "texgyretermes-regular.otf"                    # Latin, Greek and Cyrillic
_IGNORED_CHARS = {0xA0, 0xAD, 0x200B, 0x200C, 0x200D, 0xFEFF, 0x2028, 0x2029, 0xFE0E, 0xFE0F}


# ==========================================================================
# escaping
# ==========================================================================

_BASE_ESC = {
    "\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "$": r"\$", "&": r"\&", "#": r"\#",
    "^": r"\textasciicircum{}", "_": r"\_", "%": r"\%", "~": r"\textasciitilde{}",
    "`": r"\textasciigrave{}", "\xa0": "~", "\u00ad": r"\-", "\u200b": r"\hspace{0pt}",
    "\ufeff": "", "\u2028": " ", "\u2029": " ", "\ufe0e": "", "\ufe0f": "", "\t": " ",
}
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff]")
_LIGS = re.compile(r"-(?=-)|'(?=')|,(?=,)|<(?=<)|>(?=>)")
_WS = re.compile(r"[ \t\n\r\f]+")


def _lig_break(m: "re.Match[str]") -> str:
    return m.group(0) + "{}"


# ==========================================================================
# XeLaTeX
# ==========================================================================

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def find_xelatex() -> str | None:
    """The XeLaTeX executable (MiKTeX or TeX Live), or None when none is installed."""
    exe = shutil.which("xelatex")
    if exe:
        return os.path.normpath(exe)
    cands = []
    for env in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
        root = os.environ.get(env)
        if root:
            sub = ("Programs",) if env == "LOCALAPPDATA" else ()
            cands.append(os.path.join(root, *sub, "MiKTeX", "miktex", "bin", "x64", "xelatex.exe"))
            cands.append(os.path.join(root, *sub, "MiKTeX", "miktex", "bin", "xelatex.exe"))
    for tl in sorted(glob.glob(r"C:\texlive\20*"), reverse=True):
        for arch in ("windows", "win64", "win32"):
            cands.append(os.path.join(tl, "bin", arch, "xelatex.exe"))
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


_RERUN = re.compile(r"Rerun to get|Label\(s\) may have changed|There were undefined references|"
                    r"Rerun LaTeX|has changed\. Rerun")
_PAGE = re.compile(rb"\[(\d+)")


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True,
                           creationflags=_NO_WINDOW, timeout=15)
        else:
            proc.kill()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
        proc.wait(timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def _log_problems(log: str) -> tuple[list[str], int]:
    problems: list[str] = []
    seen: set[str] = set()
    lines = log.splitlines()
    for i, line in enumerate(lines):
        msg = None
        if line.startswith("! "):
            msg = line[2:].strip()
        else:
            m = re.match(r"^\S*\.tex:(\d+): (.*)$", line)
            if m:
                msg = f"line {m.group(1)}: {m.group(2).strip()}"
        if msg and msg not in seen:
            seen.add(msg)
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
            problems.append(msg + (f" ({nxt[:120]})" if nxt.startswith("l.") else ""))
        if len(problems) >= 40:
            break
    missing = len(re.findall(r"^Missing character: There is no", log, re.M))
    return problems, missing


def compile_pdf(tex_path: str, pdf_path: str, *, progress: Progress | None = None,
                cancelled: Cancelled | None = None, engine: str | None = None,
                min_passes: int = 2, max_passes: int = 3, estimate_pages: int = 0) -> CompileResult:
    """Typeset ``tex_path`` with XeLaTeX in a temporary folder and copy only the PDF to ``pdf_path``.

    The source folder (with its ``images/``) is copied, so nothing the TeX run writes
    ever lands beside the user's files.  ``progress("typeset<n>", page, estimate)``.
    """
    engine = engine or find_xelatex()
    res = CompileResult(ok=False, pdf_path=None, engine=engine)
    if not engine:
        res.problems.append("XeLaTeX is not installed")
        return res
    started = time.monotonic()
    src_dir = os.path.dirname(os.path.abspath(tex_path))
    work = tempfile.mkdtemp(prefix="book-reader-tex-")
    try:
        shutil.copyfile(tex_path, os.path.join(work, "book.tex"))
        img = os.path.join(src_dir, "images")
        if os.path.isdir(img):
            shutil.copytree(img, os.path.join(work, "images"))
        pages = 0
        log = ""
        for n in range(1, max_passes + 1):
            _check(cancelled)
            cmd = [engine, "-interaction=nonstopmode", "-file-line-error", "-synctex=0", "book.tex"]
            proc = subprocess.Popen(cmd, cwd=work, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, creationflags=_NO_WINDOW)
            q: "queue.Queue[bytes | None]" = queue.Queue()

            def pump(stream=proc.stdout) -> None:
                try:
                    assert stream is not None
                    while True:
                        chunk = stream.read1(8192) if hasattr(stream, "read1") else stream.read(8192)
                        if not chunk:
                            break
                        q.put(chunk)
                finally:
                    q.put(None)
            reader = threading.Thread(target=pump, name="xelatex-output", daemon=True)
            reader.start()
            page, tail = 0, b""
            total = pages or estimate_pages
            while True:
                if cancelled is not None and cancelled():
                    _kill_tree(proc)
                    reader.join(timeout=5)
                    if proc.stdout:
                        proc.stdout.close()
                    raise ExportCancelled()
                try:
                    chunk = q.get(timeout=0.2)
                except queue.Empty:
                    continue
                if chunk is None:
                    break
                buf = tail + chunk
                for m in _PAGE.finditer(buf):
                    try:
                        v = int(m.group(1))
                    except ValueError:
                        continue
                    if v == page + 1 or (page < v <= page + 3):
                        page = v
                tail = buf[-16:]
                if progress is not None:
                    progress(f"typeset{n}", page, max(total, page))
            proc.wait()
            reader.join(timeout=5)
            if proc.stdout:
                proc.stdout.close()
            res.passes = n
            log_path = os.path.join(work, "book.log")
            try:
                with open(log_path, "rb") as f:
                    log = f.read().decode("utf-8", "replace")
            except OSError:
                log = ""
            m = re.search(r"Output written on .*?\((\d+) pages?", log, re.S)
            if m:
                pages = int(m.group(1))
            if not os.path.isfile(os.path.join(work, "book.pdf")):
                break
            if n >= min_passes and not _RERUN.search(log):
                break
        res.problems, res.missing_chars = _log_problems(log)
        out_pdf = os.path.join(work, "book.pdf")
        if os.path.isfile(out_pdf) and os.path.getsize(out_pdf) > 0:
            tmp = pdf_path + ".part"
            shutil.copyfile(out_pdf, tmp)
            os.replace(tmp, pdf_path)
            res.ok, res.pdf_path, res.pages = True, pdf_path, pages
        elif not res.problems:
            res.problems.append("XeLaTeX produced no PDF")
    finally:
        shutil.rmtree(work, ignore_errors=True)
        res.seconds = time.monotonic() - started
    return res


# ==========================================================================
# images
# ==========================================================================

def _image_kind(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:2] == b"BM":
        return "bmp"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "tif"
    head = data[:2048].lstrip()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in data[:4096]) \
            or b"<svg" in head[:512]:
        return "svg"
    return None


def _svg_to_png(data: bytes) -> tuple[bytes, int, int] | None:
    """Rasterise an SVG with Qt (needs a running QGuiApplication for text)."""
    try:
        from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
        from PySide6.QtGui import QGuiApplication, QImage, QPainter
        from PySide6.QtSvg import QSvgRenderer
    except ImportError:
        return None
    if QGuiApplication.instance() is None:
        return None
    r = QSvgRenderer(QByteArray(data))
    if not r.isValid():
        return None
    size = r.defaultSize()
    w, h = size.width(), size.height()
    if w <= 0 or h <= 0:
        vb = r.viewBoxF()
        w, h = int(vb.width()) or 600, int(vb.height()) or 600
    scale = max(1.0, 1600.0 / max(w, h))
    W, H = max(1, int(w * scale)), max(1, int(h * scale))
    img = QImage(W, H, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.white)
    p = QPainter(img)
    r.render(p)
    p.end()
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data()), w, h


def _normalise_image(data: bytes) -> tuple[bytes, str, int, int] | None:
    """(bytes, extension, css-pixel width, height) in a format XeLaTeX reads, or None."""
    kind = _image_kind(data)
    if kind == "svg":
        got = _svg_to_png(data)
        return (got[0], "png", got[1], got[2]) if got else None
    if kind is None:
        return None
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        w, h = im.size
        if kind == "jpg" and im.mode in ("RGB", "L") and getattr(im, "bits", 8) == 8:
            return data, "jpg", w, h
        if kind == "png" and im.mode in ("RGB", "RGBA", "L", "LA", "P", "1", "I;16", "I"):
            return data, "png", w, h
        im.seek(0) if hasattr(im, "seek") else None
        if kind == "jpg":
            out = io.BytesIO()
            im.convert("RGB").save(out, "JPEG", quality=92)
            return out.getvalue(), "jpg", w, h
        mode = "RGBA" if im.mode in ("RGBA", "LA", "P", "PA") else "RGB"
        out = io.BytesIO()
        im.convert(mode).save(out, "PNG")
        return out.getvalue(), "png", w, h
    except Exception:  # noqa: BLE001 - an unreadable picture is skipped, never fatal
        return None


# ==========================================================================
# the writer
# ==========================================================================

_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_BLOCKS = {"p", "div", "section", "article", "aside", "header", "footer", "main", "nav", "blockquote",
           "figure", "figcaption", "ul", "ol", "li", "dl", "dt", "dd", "table", "thead", "tbody", "tfoot",
           "tr", "td", "th", "caption", "hr", "pre", "address", "center", "body", "html", "form",
           "fieldset", "details", "summary", "hgroup", "dialog", "legend", "menu", "dir", "listing",
           "xmp", "plaintext", "search", *_HEADINGS}
_SKIP = {"head", "script", "style", "title", "meta", "link", "template", "noscript", "iframe", "video",
         "audio", "canvas", "input", "button", "select", "textarea", "embed", "map", "area", "track",
         "source", "param", "rp", "base", "colgroup", "col"}
_SECT = ("chapter", "section", "subsection", "subsubsection", "paragraph")
_NOTE_TYPES = {"footnote", "endnote", "rearnote", "note", "footnotes", "endnotes", "rearnotes"}
_NOTE_TEXT = re.compile(r"^\s*[\[\(（【〔]?\s*(\d{1,4}|[*†‡§¶]+|[①-⑳⑴-⒇❶-❿]|注\s*\d{1,4}|[a-z])\s*"
                        r"[\]\)）】〕]?\s*$")
_NOTE_HINT = re.compile(r"note|fn|foot|endn|comment|annot|zhu", re.I)
_SAFE_TEX = re.compile(r"\\(def|edef|gdef|xdef|let|input|include|write|immediate|openout|openin|read|"
                       r"catcode|csname|endcsname|expandafter|documentclass|usepackage|newcommand|"
                       r"renewcommand|end\{document)", re.I)
_CONTENTS_NAME = {"en": "Contents", "fr": "Table des matières", "de": "Inhalt", "es": "Índice",
                  "it": "Indice", "pt": "Sumário", "nl": "Inhoud", "ru": "Содержание", "pl": "Spis treści",
                  "sv": "Innehåll", "da": "Indhold", "nb": "Innhold", "no": "Innhold", "fi": "Sisällys",
                  "cs": "Obsah", "tr": "İçindekiler", "el": "Περιεχόμενα", "uk": "Зміст",
                  "zh-hans": "目录", "zh-hant": "目錄", "ja": "目次", "ko": "차례"}


def _lang_key(tag: str) -> str:
    t = (tag or "").strip().lower().replace("_", "-")
    if t.startswith("zh"):
        if any(x in t for x in ("hant", "-tw", "-hk", "-mo")):
            return "zh-hant"
        return "zh-hans"
    return t.split("-")[0] if t else ""


# common characters written differently in the two scripts, for books tagged only "zh"
_SIMPLIFIED_ONLY = "这们来说为时国会学对过还发经动开关问长门见车东马书头钱让写觉应该实现么里个听气钟宝乐"
_TRADITIONAL_ONLY = "這們來說為時國會學對過還發經動開關問長門見車東馬書頭錢讓寫覺應該實現麼裡個聽氣鐘寶樂"


def _script_votes(text: str) -> tuple[int, int]:
    """(simplified, traditional) character counts in ``text``."""
    return (sum(text.count(c) for c in _SIMPLIFIED_ONLY), sum(text.count(c) for c in _TRADITIONAL_ONLY))


class _NodeSet:
    """A set of lxml elements.

    lxml hands out a new Python proxy for a node whenever no proxy is alive, so a
    set of ``id(element)`` forgets its members.  Holding the elements keeps their
    proxies (and therefore their identities) stable.
    """

    def __init__(self) -> None:
        self._d: dict[int, etree._Element] = {}

    def add(self, el: etree._Element) -> None:
        self._d[id(el)] = el

    def discard(self, el: etree._Element) -> None:
        self._d.pop(id(el), None)

    def __contains__(self, el: object) -> bool:
        return self._d.get(id(el)) is el


class _Writer:
    def __init__(self, book: EpubBook, options: ExportOptions, folder: str,
                 progress: Progress | None, cancelled: Cancelled | None) -> None:
        self.book = book
        self.opt = options
        self.folder = folder
        self.progress = progress
        self.cancelled = cancelled
        self.warnings: list[str] = []
        w, h, inner, outer, top, bottom = PAPERS.get(options.paper, PAPERS["a5"])
        self.textwidth = (w - inner - outer) * _PT_PER_MM
        self.textheight = (h - top - bottom) * _PT_PER_MM
        self.px2pt = 0.75 * options.font_size / _EPUB_REF_FONT_PT
        # documents in reading order: linear ones, then non-linear ones at the end
        spine = [s for s in book.spine if s.zip_name and book.has(s.zip_name)
                 and ("html" in (s.media_type or "").lower()
                      or s.zip_name.lower().endswith((".xhtml", ".html", ".htm", ".xml")))]
        order = [s.zip_name for s in spine if s.linear] + [s.zip_name for s in spine if not s.linear]
        self.docs: list[str] = list(dict.fromkeys(order))
        self.doc_index = {d: i for i, d in enumerate(self.docs)}
        self.trees: dict[str, etree._Element] = {}
        self.styles: dict[str, _Styles] = {}
        self._sheet_cache: dict[str, str] = {}
        self.ids: dict[str, dict[str, etree._Element]] = {}
        # results of the survey
        self.consumed = _NodeSet()                       # rendered elsewhere, or not at all
        self.notes: dict[tuple[str, str], etree._Element] = {}     # link target -> note body element
        self.noterefs = _NodeSet()                       # <a> that become footnotes
        self.backlinks = _NodeSet()                      # links in notes back to their reference
        self.note_docs: set[str] = set()                 # documents that hold notes turned into footnotes
        self.link_targets: set[tuple[str, str]] = set()  # (doc, id or "") that links point to
        self.toc_at: dict[tuple[str, str], list[tuple[int, str]]] = {}
        self.toc_ids: dict[str, set[str]] = {}
        self.skip_docs: set[str] = set()
        self.chars: set[str] = set()
        self.script_votes = [0, 0]                       # simplified, traditional (first ~300k chars)
        self._votes_left = 300_000
        self.cover_file: str | None = None
        # images
        self.images: dict[str, tuple[str, int, int] | None] = {}
        self.image_count = 0
        # output state
        self.out: list[str] = []
        self.para_open = False
        self.fresh_para = False
        self.fresh_page = True
        self.pending: list[str] = []
        self.mode = "normal"                             # normal | heading | arg
        self.nest = 0
        self.list_depth = {"itemize": 0, "enumerate": 0, "description": 0}
        self.table_depth = 0
        self.in_note = 0
        self.chapters = 0
        self.cur = ""
        self.esc_map: dict[int, str] = {}

    # ------------------------------------------------------------------ survey
    def tree(self, doc: str) -> etree._Element:
        t = self.trees.get(doc)
        if t is None:
            try:
                t = parse_document(self.book.read_text(doc))
            except Exception as exc:  # noqa: BLE001 - one bad document never fails the book
                self.warnings.append(f"{doc}: {exc!r}")
                t = lxml_html.document_fromstring("<html><body></body></html>")
            self.trees[doc] = t
            ids: dict[str, etree._Element] = {}
            for el in t.iter():
                if isinstance(el.tag, str):
                    i = _el_id(el)
                    if i and i not in ids:
                        ids[i] = el
            self.ids[doc] = ids
            self.styles[doc] = self._styles_for(doc, t)
        return t

    def _styles_for(self, doc: str, root: etree._Element) -> _Styles:
        st = _Styles()
        for el in root.iter():
            tag = _local(el)
            if tag == "link" and "stylesheet" in (el.get("rel") or "").lower():
                href = el.get("href")
                if href and not is_remote(href):
                    try:
                        name, _frag = self.book.resolve(href, doc)
                    except Exception:  # noqa: BLE001
                        continue
                    if name and self.book.has(name):
                        css = self._sheet_cache.get(name)
                        if css is None:
                            try:
                                css = self.book.read_text(name)
                            except Exception:  # noqa: BLE001
                                css = ""
                            self._sheet_cache[name] = css
                        st.add(css)
            elif tag == "style" and el.text:
                st.add(el.text)
        return st

    def style(self, el: etree._Element, tag: str) -> dict[str, str]:
        return self.styles[self.cur].style(el, tag) if self.cur in self.styles else {}

    def resolve(self, href: str, base: str) -> tuple[str, str] | None:
        if not href or is_remote(href) or href.startswith(("mailto:", "javascript:", "data:")):
            return None
        try:
            name, frag = self.book.resolve(href, base)
        except Exception:  # noqa: BLE001
            return None
        if not name:
            return None
        return name, unquote(frag or "")

    def _hidden(self, el: etree._Element, tag: str) -> bool:
        if tag in _SKIP:
            return True
        if el.get("hidden") is not None or (el.get("aria-hidden") == "true" and tag not in ("img", "svg")):
            return tag not in ("img", "svg")
        st = self.style(el, tag)
        return st.get("display") == "none" or st.get("visibility") == "hidden"

    def survey(self) -> None:
        n = len(self.docs)
        for i, doc in enumerate(self.docs):
            _check(self.cancelled)
            self.cur = doc
            root = self.tree(doc)
            for a in root.iter():
                if _local(a) not in ("a", "area"):
                    continue
                href = _href(a)
                if not href:
                    continue
                tgt = self.resolve(href, doc)
                if tgt and tgt[0] in self.doc_index:
                    self.link_targets.add(tgt)
            if self.progress:
                self.progress("read", i + 1, n)
        self._find_notes()
        self._map_toc()
        self._decide_docs()

    def _find_notes(self) -> None:
        for doc in self.docs:
            self.cur = doc
            for a in self.trees[doc].iter():
                if _local(a) != "a":
                    continue
                href = _href(a)
                tgt = self.resolve(href, doc) if href else None
                if not tgt or not tgt[1] or tgt[0] not in self.doc_index:
                    continue
                target = self.ids.get(tgt[0], {}).get(tgt[1])
                if target is None:
                    continue
                kinds = _epub_types(a)
                body = self._note_body(target)
                if body is None:
                    continue
                explicit = "noteref" in kinds
                if not explicit:
                    text = "".join(a.itertext())
                    hint = " ".join(filter(None, [tgt[1], body.get("class"), body.get("id"),
                                                  target.get("class"), a.get("class")]))
                    if not (_NOTE_TEXT.match(text) and (_NOTE_HINT.search(hint)
                                                         or _epub_types(body) & _NOTE_TYPES
                                                         or _local(body) == "aside")):
                        continue
                    if body is target and _local(body) in ("h1", "h2", "h3", "h4", "h5", "h6"):
                        continue
                self.noterefs.add(a)
                self.notes[tgt] = body
                self.consumed.add(body)
                self.note_docs.add(tgt[0])
        # links inside the notes that point back at a reference are dropped
        ref_ids = set()
        for doc in self.docs:
            for a in self.trees[doc].iter():
                if a in self.noterefs:
                    i = _el_id(a) or (a.getparent().get("id") if a.getparent() is not None else None)
                    if i:
                        ref_ids.add((doc, i))
        for (doc, _i), body in self.notes.items():
            self.cur = doc
            for a in body.iter():
                if _local(a) == "a" and _href(a):
                    tgt = self.resolve(_href(a), doc)
                    if tgt and tgt in ref_ids:
                        self.backlinks.add(a)

    def _note_body(self, target: etree._Element) -> etree._Element | None:
        """The element holding a note: the target itself, or the block around an empty anchor."""
        el = target
        for _ in range(4):
            tag = _local(el)
            if _epub_types(el) & _NOTE_TYPES or tag in ("aside", "li", "p", "div", "dd", "section", "td"):
                if tag in ("body", "html") or el.getparent() is None:
                    return None
                if tag in ("section", "div") and len("".join(el.itertext())) > 6000:
                    return None
                return el
            el = el.getparent()
            if el is None:
                return None
        return None

    def _map_toc(self) -> None:
        entries: list[tuple[int, str, str, str]] = []

        def first_target(items) -> tuple[str, str] | None:
            for e in items:
                if e.zip_name in self.doc_index:
                    return e.zip_name, unquote(e.fragment or "")
                got = first_target(e.children)
                if got:
                    return got
            return None

        def walk(items, level: int) -> None:
            for e in items:
                title = " ".join((e.title or "").split())
                if e.zip_name in self.doc_index:
                    doc, frag = e.zip_name, unquote(e.fragment or "")
                else:                                  # a grouping header: where its first child is
                    doc, frag = first_target(e.children) or ("", "")
                if title and doc:
                    entries.append((level, title, doc, frag))
                walk(e.children, level + 1 if title else level)
        synthetic = bool(getattr(self.book, "toc_is_synthetic", False))
        if not synthetic:
            walk(self.book.toc, 0)
        entries = [e for e in entries if e[2]]
        if synthetic or (len(entries) <= 1 and len(self.docs) > 3):
            entries = self._heading_toc() or entries
        for level, title, doc, frag in entries:
            if frag and frag not in self.ids.get(doc, {}):
                frag = ""
            self.toc_at.setdefault((doc, frag), []).append((min(level, 4), title))
            if frag:
                self.toc_ids.setdefault(doc, set()).add(frag)

    def _heading_toc(self) -> list[tuple[int, str, str, str]]:
        found: list[tuple[int, str, str, etree._Element]] = []
        for doc in self.docs:
            for el in self.trees[doc].iter():
                lv = _HEADINGS.get(_local(el))
                if lv:
                    t = " ".join("".join(el.itertext()).split())
                    if t:
                        found.append((lv, t, doc, el))
        if not found:
            return []
        top = min(f[0] for f in found)
        out = []
        n = 0
        for lv, t, doc, el in found:
            if lv > top + 2:
                continue
            i = _el_id(el)
            if not i:
                n += 1
                i = f"er-h{n}"
                el.set("id", i)
                self.ids[doc][i] = el
            out.append((lv - top, t, doc, i))
        return out

    def _visible_text(self, el: etree._Element, doc: str) -> tuple[int, int, int, int]:
        """(characters, pictures, characters inside internal links, internal links) outside skipped parts."""
        chars = pics = linked = links = 0
        stack = [(el, False)]
        while stack:
            node, in_link = stack.pop()
            tag = _local(node)
            if not tag or node in self.consumed or self._hidden(node, tag):
                continue
            if tag == "nav" and _epub_types(node) & {"toc", "landmarks", "page-list"}:
                continue
            if tag in ("img", "image", "svg", "hr", "math"):
                pics += 1
                if tag == "svg":
                    continue
            link = in_link
            if tag == "a" and _href(node) and not is_remote(_href(node) or ""):
                link = True
                links += 1
            t = len((node.text or "").strip())
            chars += t
            if link:
                linked += t
            for c in node:
                stack.append((c, link))
                tt = len((c.tail or "").strip())
                chars += tt
                if in_link:
                    linked += tt
        return chars, pics, linked, links

    def _decide_docs(self) -> None:
        cover = self.book.cover if self.opt.cover else None
        for k, doc in enumerate(self.docs):
            self.cur = doc
            body = _body(self.trees[doc])
            for t in body.itertext():                  # every character, notes included, for the fonts
                self.chars.update(t)
                if self._votes_left > 0:
                    s, h = _script_votes(t)
                    self.script_votes[0] += s
                    self.script_votes[1] += h
                    self._votes_left -= len(t)
            chars, pics, linked, links = self._visible_text(body, doc)
            imgs = [e for e in body.iter() if _local(e) in ("img", "image")]
            if doc in self.note_docs and pics == 0:
                # a notes page whose notes all became footnotes: only its headings are left
                heads = sum(len("".join(h.itertext()).strip()) for h in body.iter()
                            if _local(h) in _HEADINGS and h not in self.consumed)
                if chars - heads <= 0:
                    chars = 0
            if chars == 0 and pics == 0:
                self.skip_docs.add(doc)
            elif chars == 0 and len(imgs) == 1 and self.opt.cover and k <= 2:
                src = _href(imgs[0]) or imgs[0].get("src")
                tgt = self.resolve(src or "", doc)
                if tgt and (tgt[0] == cover or (cover is None and k == 0)):
                    self.skip_docs.add(doc)
                    if cover is None:
                        self.cover_file = tgt[0]
            elif links >= 3 and linked >= 0.8 * chars and chars < 20000:
                self.skip_docs.add(doc)                  # the book's own HTML contents page
        if self.opt.cover and cover and self.cover_file is None:
            self.cover_file = cover
        for key in list(self.toc_at):
            if key[0] in self.skip_docs:
                del self.toc_at[key]
        for d in self.skip_docs:
            self.toc_ids.pop(d, None)
        for entries in self.toc_at.values():
            for _lv, t in entries:
                self.chars.update(t)
        md = self.book.metadata or {}
        self.chars.update(str(md.get("title") or ""))
        for a in md.get("authors") or []:
            self.chars.update(str(a))

    # ------------------------------------------------------------------ fonts & escaping
    def plan_fonts(self) -> dict:
        md = self.book.metadata or {}
        tag = str(md.get("language") or "").strip().lower().replace("_", "-")
        lang = _lang_key(tag)
        cps = {ord(c) for c in self.chars}
        if tag in ("zh", "zh-cn", "chi", "zho", "chinese", "") or lang == "zh-hans":
            simp, trad = self.script_votes
            if trad > 2 * simp and trad >= 20 and not any(x in tag for x in ("hans", "-cn", "-sg")):
                lang = "zh-hant"
            elif lang == "" and simp >= 20 and simp > 2 * trad:
                lang = "zh-hans"
        cjk = [cp for cp in cps if _is_cjk(cp)]
        letters = sum(1 for cp in cps if chr(cp).isalpha())
        if lang in ("", "und", "mul", "zxx") and cjk and letters and len(cjk) >= 0.3 * letters:
            kana = any(0x3040 <= cp <= 0x30FF for cp in cjk)
            hangul = any(0xAC00 <= cp <= 0xD7AF for cp in cjk)
            lang = "ja" if kana else "ko" if hangul else "zh-hans"
        cjk_primary = lang in _CJK_FONTS
        plan: dict = {"lang": lang, "cjk_primary": cjk_primary, "cjk": bool(cjk), "main": None,
                      "cjk_font": None, "cjk_bold": None, "cjk_fallback": None, "fallbacks": {},
                      "ctex_windows": False, "by_file": False}
        if self.opt.fonts == "texlive":
            return self._plan_texlive_fonts(plan, cps, cjk, cjk_primary)
        cat = _font_catalog()
        if cjk:
            key = lang if lang in _CJK_FONTS else "zh-hans"
            for fam in _CJK_FONTS[key]:
                if fam in cat:
                    plan["cjk_font"] = fam
                    break
            for fam in _CJK_BOLD.get(key, ()):
                if fam in cat:
                    plan["cjk_bold"] = fam
                    break
            if key == "zh-hans" and all(f in cat for f in ("SimSun", "SimHei", "KaiTi", "FangSong")):
                plan["ctex_windows"] = True
            main_cjk = cat.get(plan["cjk_font"] or "")
            if main_cjk is not None:
                missing = [cp for cp in cjk if not main_cjk.has(cp)]
                if missing:
                    for fam in _CJK_EXT_FALLBACK:
                        f = cat.get(fam)
                        if f is not None and any(f.has(cp) for cp in missing):
                            plan["cjk_fallback"] = fam
                            break
        latin_needed = sorted(cp for cp in cps if cp > 0x7E and cp not in _IGNORED_CHARS and not _is_cjk(cp)
                              and not (cjk_primary and cp in _CJK_PUNCT)
                              and unicodedata.category(chr(cp))[0] not in "CZ")
        best, best_miss = None, None
        for fam in _LATIN_CANDIDATES:
            f = cat.get(fam)
            if f is None:
                continue
            miss = [cp for cp in latin_needed if not f.has(cp)]
            if best_miss is None or len(miss) < len(best_miss):
                best, best_miss = fam, miss
            if not miss:
                break
        plan["main"] = best
        fallbacks: dict[int, str] = {}
        for cp in best_miss or []:
            for fam in _FALLBACK_CANDIDATES:
                f = cat.get(fam)
                if f is not None and fam != best and f.has(cp):
                    fallbacks[cp] = fam
                    break
            else:
                self.warnings.append(f"no installed font has U+{cp:04X}")
        plan["fallbacks"] = fallbacks
        fams = sorted(set(fallbacks.values()))
        plan["fallback_macros"] = {fam: "\\erfb" + "".join(chr(ord("A") + int(d)) for d in str(i))
                                   for i, fam in enumerate(fams)}
        self.esc_map = {ord(k): v for k, v in _BASE_ESC.items()}
        for cp, fam in fallbacks.items():
            self.esc_map[cp] = "{" + plan["fallback_macros"][fam] + " " + chr(cp) + "}"
        self.plan = plan
        return plan

    def _plan_texlive_fonts(self, plan: dict, cps: set[int], cjk: list[int], cjk_primary: bool) -> dict:
        """Fonts that ship with the TeX distribution, for machines with no font library.

        Every one of these is free and lives in the same package tree as the macros,
        so a document made this way typesets anywhere the tree does (the phone included).
        """
        lang = plan["lang"]
        if cjk:
            key = lang if lang in _TEX_CJK_FONTS else "zh-hans"
            main, bold, italic = _TEX_CJK_FONTS[key]
            plan.update(cjk_font=main, cjk_bold=bold, cjk_italic=italic, by_file=True)
        # Latin Modern (the default) has no Greek or Cyrillic; TeX Gyre Termes has both.
        wide = any(0x0370 <= cp <= 0x058F or 0x0590 <= cp <= 0x05FF or 0x1F00 <= cp <= 0x1FFF
                   for cp in cps)
        plan["main"] = _TEX_LATIN_WIDE if wide else None
        plan["fallbacks"] = {}
        plan["fallback_macros"] = {}
        self.esc_map = {ord(k): v for k, v in _BASE_ESC.items()}
        self.plan = plan
        return plan

    def esc(self, s: str) -> str:
        s = _CTRL.sub("", s).translate(self.esc_map)
        return _LIGS.sub(_lig_break, s)

    # ------------------------------------------------------------------ output primitives
    def raw(self, s: str) -> None:
        self.out.append(s)

    def start_para(self) -> None:
        if not self.para_open:
            self.para_open = True
            self.fresh_para = True
        self.fresh_page = False
        if self.pending and self.mode == "normal":
            # \leavevmode: an anchor must not open an empty line of its own (table cells)
            self.out.append("\\leavevmode" + self.anchors_tex())

    def text(self, s: str) -> None:
        if not s:
            return
        s = _WS.sub(" ", s)
        if not self.para_open or self.fresh_para:
            s = s.lstrip(" \u3000\xa0")
            if not s:
                return
        self.start_para()
        self.fresh_para = False
        self.out.append(self.esc(s))

    _NL = "\\newline\n"

    def _trim(self) -> None:
        while self.out and (self.out[-1] in (self._NL, "\\mbox{}" + self._NL, " ", r"\\ ")
                            or not self.out[-1]):
            self.out.pop()

    def par(self) -> None:
        if self.mode == "heading":
            if self.para_open:
                self.out.append(r"\\ ")
                self.para_open = False
            return
        if self.mode == "arg":
            self.text(" ")
            return
        if not self.para_open:
            return
        self._trim()
        self.out.append("\n\n")
        self.para_open = False
        self.fresh_para = False

    def capture(self, fn: Callable[[], None], mode: str | None = None) -> str:
        saved = (self.out, self.para_open, self.fresh_para, self.mode)
        self.out, self.para_open, self.fresh_para = [], False, False
        if mode:
            self.mode = mode
        try:
            fn()
            self._trim()
            text = "".join(self.out)
        finally:
            self.out, self.para_open, self.fresh_para, self.mode = saved
        text = text.strip()
        while text.endswith(r"\\"):
            text = text[:-2].rstrip()
        return text

    def anchor_name(self, doc: str, frag: str) -> str:
        i = self.doc_index.get(doc, 0)
        if not frag:
            return f"d{i}"
        safe = frag if re.fullmatch(r"[A-Za-z0-9_.-]+", frag) else frag.encode("utf-8").hex()
        return f"d{i}.{safe}"

    def anchor(self, el: etree._Element) -> None:
        i = _el_id(el)
        if i and (self.cur, i) in self.link_targets:
            self.pending.append(self.anchor_name(self.cur, i))

    # ------------------------------------------------------------------ document
    def render(self) -> str:
        n = len(self.docs)
        for k, doc in enumerate(self.docs):
            _check(self.cancelled)
            if self.progress:
                self.progress("convert", k + 1, n)
            if doc in self.skip_docs:
                continue
            self.cur = doc
            body = _body(self.trees[doc])
            self.par()
            self.raw(f"\n%% ---- {doc} ----\n")
            if (doc, "") in self.link_targets:
                self.pending.append(self.anchor_name(doc, ""))
            entries = self.toc_at.pop((doc, ""), None)
            if entries:
                self.sections(entries, self.first_text_block(body))
            self.children(body)
            self.par()
            self.flush_vertical()
        return "".join(self.out)

    def anchors_tex(self) -> str:
        text = "".join(f"\\hypertarget{{{a}}}{{}}" for a in self.pending)
        self.pending.clear()
        return text

    def flush_vertical(self) -> None:
        """Anchors with no text after them: placed between paragraphs, where they take no room."""
        if self.pending and self.mode == "normal":
            self.par()
            self.raw(self.anchors_tex() + "\n")

    def first_text_block(self, root: etree._Element) -> etree._Element | None:
        """The first block that holds text, when nothing else comes before it."""
        if (root.text or "").strip():
            return None
        for el in root:
            tag = _local(el)
            if not tag or el in self.consumed or self._hidden(el, tag):
                if tag and (el.tail or "").strip():
                    return None
                continue
            if tag in ("img", "svg", "image", "br", "hr", "a") and not "".join(el.itertext()).strip():
                if (el.tail or "").strip():
                    return None
                continue
            if tag not in _BLOCKS:
                return None
            if tag in ("table", "ul", "ol", "dl", "pre", "blockquote"):
                return None
            has_block = any(_local(c) in _BLOCKS for c in el)
            if not has_block:
                return el if "".join(el.itertext()).strip() else None
            return self.first_text_block(el)
        return None

    def _next_block(self, el: etree._Element) -> etree._Element | None:
        """The first text block after an empty anchor, when only empty inline nodes come between."""
        if (el.tail or "").strip():
            return None
        for sib in el.itersiblings():
            tag = _local(sib)
            if not tag or sib in self.consumed or self._hidden(sib, tag):
                if tag and (sib.tail or "").strip():
                    return None
                continue
            if tag not in _BLOCKS:
                if "".join(sib.itertext()).strip() or (sib.tail or "").strip():
                    return None
                continue
            if tag in ("table", "ul", "ol", "dl", "pre", "blockquote"):
                return None
            if not any(_local(c) in _BLOCKS for c in sib):
                return sib if "".join(sib.itertext()).strip() else None
            return self.first_text_block(sib)
        return None

    @staticmethod
    def _title_match(el: etree._Element, title: str) -> bool:
        """Whether a short block says the same as a contents entry (spacing and punctuation aside)."""
        text = " ".join("".join(el.itertext()).split())
        a, b = _norm_title(text), _norm_title(title)
        if not a or not b or len(text) > max(2 * len(title) + 10, 40):
            return False
        return a == b or a in b or b in a

    def sections(self, entries: list[tuple[int, str]], candidate: etree._Element | None) -> None:
        """Emit the headings for contents entries; the book's own heading becomes the displayed title.

        ``candidate`` is the first block at that point.  It is taken as the heading when its
        text matches an entry, or when it is an ``h1``/``h2`` (then for the last entry).
        """
        use = None
        if candidate is not None and candidate not in self.consumed:
            for i, (_lv, title) in enumerate(entries):
                if self._title_match(candidate, title):
                    use = i
            text = " ".join("".join(candidate.itertext()).split())
            if use is None and _local(candidate) in ("h1", "h2") and 0 < len(text) <= 300:
                use = len(entries) - 1
        for i, (level, title) in enumerate(entries):
            display = ""
            if i == use and candidate is not None:
                self.consumed.add(candidate)
                for el in candidate.iter():
                    self.anchor(el) if isinstance(el.tag, str) else None
                display = self.capture(lambda c=candidate: self.children(c), mode="heading")
            self.emit_section(level, title, display)
        self.flush_vertical()

    def emit_section(self, level: int, title: str, display: str) -> None:
        self.par()
        cmd = _SECT[min(level, 4)]
        t = self.esc(title)
        d = display or t
        if cmd == "chapter":
            short = self.esc(title if len(title) <= 40 else title[:38].rstrip() + "…")
            self.raw(f"\n\\clearpage\\phantomsection\n\\addcontentsline{{toc}}{{chapter}}{{{t}}}\n"
                     f"\\chapter*{{{d}}}\n\\markboth{{{short}}}{{{short}}}\n")
            self.chapters += 1
            self.fresh_page = True
        else:
            self.raw(f"\n\\phantomsection\\addcontentsline{{toc}}{{{cmd}}}{{{t}}}\n\\{cmd}*{{{d}}}\n")

    def block_sections(self, el: etree._Element) -> None:
        """Contents entries that point at this block (or at inline anchors inside it)."""
        ids = self.toc_ids.get(self.cur)
        if not ids:
            return
        found: list[str] = []
        stack = [el]
        while stack:
            node = stack.pop()
            i = _el_id(node)
            if i and i in ids and (self.cur, i) in self.toc_at:
                found.append(i)
            stack.extend(c for c in reversed(node) if isinstance(c.tag, str) and _local(c) not in _BLOCKS)
        if not found:
            return
        entries: list[tuple[int, str]] = []
        for i in found:
            entries.extend(self.toc_at.pop((self.cur, i), []))
        if not entries:
            return
        tag = _local(el)
        has_block = any(_local(c) in _BLOCKS for c in el)
        cand = el if (tag in _HEADINGS or not has_block) else self.first_text_block(el)
        self.sections(entries, cand)

    # ------------------------------------------------------------------ elements
    def children(self, el: etree._Element) -> None:
        if el.text:
            self.text(el.text)
        for c in el:
            self.node(c)
            if c.tail:
                self.text(c.tail)

    def node(self, el: etree._Element) -> None:
        tag = _local(el)
        if not tag or el in self.consumed:
            return
        if self._hidden(el, tag):
            return
        kinds = _epub_types(el)
        if tag == "nav" and kinds & {"toc", "landmarks", "page-list"}:
            return
        if "pagebreak" in kinds and not "".join(el.itertext()).strip():
            self.anchor(el)
            return
        if tag in _BLOCKS and self.mode == "normal":
            self.block_sections(el)
            if el in self.consumed:
                return
        elif self.mode == "normal" and not self.in_note and self.toc_ids.get(self.cur):
            i = _el_id(el)
            if i and (self.cur, i) in self.toc_at:
                # an anchor between blocks (Kindle books): the next block is the heading
                entries = self.toc_at.pop((self.cur, i))
                cand = None
                if not self.para_open and not "".join(el.itertext()).strip():
                    cand = self._next_block(el)
                self.sections(entries, cand)
        fn = getattr(self, "el_" + tag, None)
        if fn is not None:
            fn(el, tag)
        elif tag in _BLOCKS:
            self.block(el, tag)
        else:
            self.inline(el, tag)

    # -- generic block and inline ------------------------------------------------------
    def _decls(self, el: etree._Element, tag: str, block: bool) -> list[str]:
        st = self.style(el, tag)
        d: list[str] = []
        align = st.get("text-align") or (el.get("align") or "").lower()
        if block and self.mode == "normal":
            if align == "center" or tag == "center":
                d.append(r"\centering")
            elif align == "right":
                d.append(r"\raggedleft")
        fs = st.get("font-style")
        if fs in ("italic", "oblique"):
            d.append(r"\itshape")
        elif fs == "normal" and tag not in ("em", "i", "cite", "var", "dfn"):
            d.append(r"\upshape")
        fw = st.get("font-weight", "")
        if fw in ("bold", "bolder") or (fw.isdigit() and int(fw) >= 600):
            d.append(r"\bfseries")
        elif fw in ("normal", "lighter") or (fw.isdigit() and int(fw) < 500):
            if tag not in ("b", "strong"):
                d.append(r"\mdseries")
        if "small-caps" in st.get("font-variant", ""):
            d.append(r"\scshape")
        if "monospace" in st.get("font-family", "") or "courier" in st.get("font-family", ""):
            d.append(r"\ttfamily")
        if self.mode == "normal":
            size = _size_cmd(st.get("font-size")) if tag not in ("html", "body") else None
            if tag == "font" and el.get("size"):
                size = _FONT_SIZE_ATTR.get(el.get("size", "").strip(), size)
            if size:
                d.append(size)
        return d

    def _page_break(self, el: etree._Element, tag: str, before: bool) -> bool:
        st = self.style(el, tag)
        keys = ("page-break-before", "break-before") if before else ("page-break-after", "break-after")
        return any(st.get(k) in ("always", "page", "left", "right", "recto", "verso") for k in keys)

    def block(self, el: etree._Element, tag: str) -> None:
        if self.mode != "normal":
            self.par()
            self.anchor(el)
            self.children(el)
            self.par()
            return
        self.par()
        if self._page_break(el, tag, True) and not self.fresh_page:
            self.raw("\\clearpage\n")
            self.fresh_page = True
        d = self._decls(el, tag, True)
        if d:
            self.raw("{" + "".join(d) + " ")
        self.anchor(el)
        before = len(self.out)
        self.children(el)
        if tag == "p" and len(self.out) == before and not self.para_open and not self.pending:
            self.raw("\\vspace{\\baselineskip}\n")       # an empty paragraph: a blank line
        self.par()
        if d:
            self.raw("}\n")
        if self._page_break(el, tag, False):
            self.raw("\\clearpage\n")
            self.fresh_page = True

    def inline(self, el: etree._Element, tag: str) -> None:
        d = self._decls(el, tag, False)
        self.anchor(el)
        if d:
            self.raw("{" + "".join(d) + " ")
            self.children(el)
            self.raw(r"\/}" if r"\itshape" in d else "}")
        else:
            self.children(el)

    def _styled(self, el: etree._Element, tag: str, decl: str) -> None:
        d = [decl] + [x for x in self._decls(el, tag, False) if x != decl]
        self.anchor(el)
        self.raw("{" + "".join(d) + " ")
        self.children(el)
        self.raw(r"\/}" if decl in (r"\em", r"\itshape") else "}")

    def _has_block(self, el: etree._Element) -> bool:
        return any(isinstance(c.tag, str) and (_local(c) in _BLOCKS or _local(c) == "br") for c in el.iter()
                   if c is not el)

    def _command(self, el: etree._Element, tag: str, cmd: str) -> None:
        """A one-argument command (superscript, underline...): only around inline content."""
        if self._has_block(el) or self.mode == "heading" and cmd in ("\\uline", "\\sout", "\\CJKunderline",
                                                                      "\\CJKsout"):
            self.inline(el, tag)
            return
        self.anchor(el)
        self.start_para()
        saved = self.mode
        self.mode = "arg" if self.mode == "normal" else self.mode
        self.raw(cmd + "{")
        self.children(el)
        self.raw("}")
        self.mode = saved

    def el_em(self, el, tag):
        self._styled(el, tag, r"\em")
    el_i = el_cite = el_var = el_dfn = el_address = el_em

    def el_strong(self, el, tag):
        self._styled(el, tag, r"\bfseries")
    el_b = el_strong

    def el_code(self, el, tag):
        self._styled(el, tag, r"\ttfamily")
    el_kbd = el_samp = el_tt = el_code

    def el_small(self, el, tag):
        self._styled(el, tag, r"\small") if self.mode == "normal" else self.inline(el, tag)

    def el_big(self, el, tag):
        self._styled(el, tag, r"\large") if self.mode == "normal" else self.inline(el, tag)

    def el_sup(self, el, tag):
        if any(a in self.noterefs for a in el.iter()):
            self.inline(el, tag)
        else:
            self._command(el, tag, r"\textsuperscript")

    def el_sub(self, el, tag):
        self._command(el, tag, r"\textsubscript")

    def el_u(self, el, tag):
        self._command(el, tag, r"\CJKunderline" if self.plan["cjk"] else r"\uline")
    el_ins = el_u

    def el_s(self, el, tag):
        self._command(el, tag, r"\CJKsout" if self.plan["cjk"] else r"\sout")
    el_strike = el_del = el_s

    def el_wbr(self, el, tag):
        self.raw(r"\hspace{0pt}")

    def el_br(self, el, tag):
        if self.mode == "arg":
            self.text(" ")
        elif self.mode == "heading":
            if self.para_open:
                self.raw(r"\\ ")
        elif self.para_open:
            if self.out and self.out[-1] in (self._NL, "\\mbox{}" + self._NL):
                self.raw("\\mbox{}" + self._NL)
            else:
                self.raw(self._NL)
        else:
            self.raw("\\vspace{\\baselineskip}\n")

    def el_hr(self, el, tag):
        if self.mode != "normal":
            return
        self.par()
        self.anchor(el)
        self.raw("\\par\\medskip{\\centering\\rule{0.3\\linewidth}{0.4pt}\\par}\\medskip\n")

    def el_center(self, el, tag):
        self.block(el, tag)

    def el_body(self, el, tag):
        self.children(el)
    el_html = el_body

    def el_object(self, el, tag):
        self.children(el)

    def el_ruby(self, el, tag):
        pairs: list[tuple[str, str]] = []
        base: list[str] = []
        if el.text:
            base.append(el.text)
        for c in el:
            t = _local(c)
            if t == "rt":
                pairs.append(("".join(base), "".join(c.itertext())))
                base = []
            elif t == "rp":
                pass
            elif t:
                base.append("".join(c.itertext()))
            if c.tail:
                base.append(c.tail)
        if not pairs:
            self.inline(el, tag)
            return
        self.anchor(el)
        for b, r in pairs:
            b, r = " ".join(b.split()), " ".join(r.split())
            if b:
                self.start_para()
                self.fresh_para = False
                self.raw(f"\\erruby{{{self.esc(b)}}}{{{self.esc(r)}}}" if r and self.mode == "normal"
                         else self.esc(b))
        rest = " ".join("".join(base).split())
        if rest:
            self.text(rest)

    def el_math(self, el, tag):
        tex = None
        for a in el.iter():
            if _local(a) == "annotation" and "tex" in (a.get("encoding") or "").lower():
                tex = (a.text or "").strip()
        if tex and not _SAFE_TEX.search(tex) and tex.count("{") == tex.count("}") \
                and "$" not in tex and "\n\n" not in tex:
            depth = 0
            ok = True
            for ch in tex:
                depth += (ch == "{") - (ch == "}")
                if depth < 0:
                    ok = False
                    break
            if ok:
                self.start_para()
                self.fresh_para = False
                self.raw("\\(" + tex + "\\)")
                return
        alt = el.get("alttext")
        text = " ".join("".join(t for a in el.iter() if _local(a) != "annotation"
                                for t in [a.text or ""]).split()) or alt or ""
        if text:
            self.raw("{\\itshape ")
            self.text(text)
            self.raw("\\/}")

    # -- headings ----------------------------------------------------------------------
    def _heading(self, el, tag):
        if self.mode != "normal" or self.in_note or self.table_depth:
            self.raw("{\\bfseries ")
            self.children(el)
            self.raw("}")
            self.par()
            return
        level = _HEADINGS[tag]
        self.par()
        d = [x for x in self._decls(el, tag, True) if x not in (r"\bfseries",) and not x.startswith(r"\L")
             and not x.startswith(r"\l") and x not in (r"\huge", r"\small", r"\footnotesize")]
        self.anchor(el)
        body = self.capture(lambda: self.children(el), mode="heading")
        if not body:
            self.flush_vertical()
            return
        anchors = self.anchors_tex()
        # a space after the declarations: XeTeX reads CJK characters as letters, so
        # "\centering附录" would be one (undefined) command name
        decl = "".join(d) + " " if d else ""
        self.raw(f"\\erhead{{{level}}}{{{decl}{anchors}{body}}}\n")
        self.fresh_page = False
    el_h1 = el_h2 = el_h3 = el_h4 = el_h5 = el_h6 = _heading

    # -- lists ------------------------------------------------------------------------
    def _list(self, el, tag, kind: str):
        if self.mode != "normal":
            self.block(el, tag)
            return
        items = [c for c in el if _local(c) in ("li", "dt", "dd")]
        if not items and not "".join(el.itertext()).strip():
            return
        self.par()
        self.anchor(el)
        if self.nest >= 5 or self.list_depth[kind] >= 4:
            n = int(el.get("start") or 1) if (el.get("start") or "").isdigit() else 1
            for c in el:
                t = _local(c)
                if t in ("li", "dd", "dt"):
                    self.par()
                    self.raw("\\noindent ")
                    if kind == "enumerate":
                        self.text(f"{n}. ")
                        n += 1
                    elif kind == "itemize":
                        self.text("– ")
                    self.anchor(c)
                    self.children(c) if t != "dt" else self._styled(c, t, r"\bfseries")
                    self.par()
                elif t:
                    self.node(c)
            return
        opts: list[str] = []
        st = self.style(el, tag)
        lst = st.get("list-style-type") or st.get("list-style", "")
        if kind == "enumerate":
            start = el.get("start") or ""
            if start.lstrip("-").isdigit() and int(start) != 1:
                opts.append(f"start={int(start)}")
            typ = el.get("type") or ""
            label = {"a": r"\alph*.", "A": r"\Alph*.", "i": r"\roman*.", "I": r"\Roman*."}.get(typ)
            for key, lab in (("lower-alpha", r"\alph*."), ("lower-latin", r"\alph*."),
                             ("upper-alpha", r"\Alph*."), ("upper-latin", r"\Alph*."),
                             ("lower-roman", r"\roman*."), ("upper-roman", r"\Roman*.")):
                if key in lst:
                    label = lab
            if label:
                opts.append("label=" + label)
        if "none" in lst.split() and kind != "description":
            opts.append("label={}")
        self.raw(f"\\begin{{{kind}}}" + (f"[{','.join(opts)}]" if opts else "") + "\n")
        self.nest += 1
        self.list_depth[kind] += 1
        started = False

        def ensure() -> None:
            nonlocal started
            if not started:
                self.raw("\\item[] " if kind == "description" else "\\item{} ")
                started = True
                self.para_open = False
        if (el.text or "").strip():
            ensure()
            self.text(el.text)
        for c in el:
            t = _local(c)
            if t == "li" or (kind == "description" and t == "dt"):
                self.par()
                if t == "dt":
                    label = self.capture(lambda c=c: self.children(c), mode="arg")
                    self.raw(f"\\item[{{{label}}}] ")
                else:
                    self.raw("\\item{} ")
                started = True
                self.para_open = False
                self.anchor(c)
                if t == "li":
                    d = self._decls(c, t, False)
                    if d:
                        self.raw("{" + "".join(d) + " ")
                        self.children(c)
                        self.raw("}")
                    else:
                        self.children(c)
            elif t == "dd":
                if not started or (kind == "description" and self._prev_is_dd(c)):
                    self.par()
                    self.raw("\\item[] ")
                    started = True
                    self.para_open = False
                self.anchor(c)
                self.children(c)
            elif t:
                ensure()
                self.node(c)
            if (c.tail or "").strip():
                ensure()
                self.text(c.tail)
        ensure()
        self.par()
        self.list_depth[kind] -= 1
        self.nest -= 1
        self.raw(f"\\end{{{kind}}}\n")
        self.fresh_page = False

    def _prev_is_dd(self, el) -> bool:
        prev = el.getprevious()
        while prev is not None and not isinstance(prev.tag, str):
            prev = prev.getprevious()
        return prev is not None and _local(prev) == "dd"

    def el_ul(self, el, tag):
        self._list(el, tag, "itemize")
    el_menu = el_dir = el_ul

    def el_ol(self, el, tag):
        self._list(el, tag, "enumerate")

    def el_dl(self, el, tag):
        self._list(el, tag, "description")

    def el_blockquote(self, el, tag):
        if self.mode != "normal" or self.nest >= 5:
            self.block(el, tag)
            return
        self.par()
        self.raw("\\begin{quote}\n")
        self.nest += 1
        self.block(el, "div")
        self.nest -= 1
        self.par()
        self.raw("\\end{quote}\n")

    def el_pre(self, el, tag):
        if self.mode != "normal":
            self.block(el, tag)
            return
        self.par()
        self.anchor(el)
        text = "".join(el.itertext()).expandtabs(4).strip("\n")
        if not text.strip():
            return
        lines = []
        for line in text.split("\n"):
            lead = len(line) - len(line.lstrip(" "))
            body = self.esc(line.lstrip(" "))
            body = re.sub(r"  +", lambda m: "\\ " * len(m.group(0)), body)
            lines.append("\\ " * lead + body if body else "\\mbox{}")
        self.start_para()
        self.raw("{\\ttfamily\\small\\raggedright\\noindent " + "\\newline\n".join(lines) + "\\par}\n")
        self.para_open = False

    # -- links and notes -------------------------------------------------------------
    def el_a(self, el, tag):
        if el in self.backlinks:
            self.anchor(el)
            return
        href = _href(el)
        if el in self.noterefs and href:
            tgt = self.resolve(href, self.cur)
            body = self.notes.get(tgt) if tgt else None
            if body is not None and self.mode == "normal" and not self.in_note:
                self.footnote(el, body, tgt[0] if tgt else self.cur)
                return
            self.anchor(el)
            text = " ".join("".join(el.itertext()).split())
            if text and self.mode != "heading":
                self.start_para()
                self.raw("\\textsuperscript{" + self.esc(text.strip("[]()（）【】")) + "}")
            return
        if not href or self.mode == "heading":
            self.inline(el, tag)
            return
        if self._has_block(el):
            self.inline(el, tag)
            return
        if is_remote(href) or href.startswith("mailto:"):
            url = href.replace("\\", "%5C").replace("{", "%7B").replace("}", "%7D").replace(" ", "%20")
            url = url.replace("~", "%7E").replace("%", "\\%").replace("#", "\\#")
            self.anchor(el)
            self.start_para()
            saved = self.mode
            self.mode = "arg"
            self.raw(f"\\href{{{url}}}{{")
            self.children(el)
            self.raw("}")
            self.mode = saved
            return
        tgt = self.resolve(href, self.cur)
        if tgt and tgt[0] in self.doc_index and tgt[0] not in self.skip_docs \
                and (not tgt[1] or tgt[1] in self.ids.get(tgt[0], {})):
            self.anchor(el)
            self.start_para()
            saved = self.mode
            self.mode = "arg"
            self.raw(f"\\hyperlink{{{self.anchor_name(*tgt)}}}{{")
            self.children(el)
            self.raw("}")
            self.mode = saved
            return
        self.inline(el, tag)

    def footnote(self, ref: etree._Element, body: etree._Element, doc: str) -> None:
        self.anchor(ref)
        self.start_para()
        self.fresh_para = False
        saved_cur, saved_pending = self.cur, self.pending
        self.cur, self.pending = doc, []
        self.in_note += 1
        self.consumed.discard(body)
        ref_text = _norm_title("".join(ref.itertext()))
        try:
            def fill() -> None:
                skipped_label = False
                if body.text:
                    t = body.text
                    if ref_text and _norm_title(t.split()[0] if t.split() else "") == ref_text:
                        t = t.split(None, 1)[1] if len(t.split(None, 1)) > 1 else ""
                        skipped_label = True
                    self.text(t)
                for c in body:
                    if not skipped_label and _local(c) in ("a", "span", "sup", "b", "strong") \
                            and _norm_title("".join(c.itertext())) == ref_text and not self.para_open:
                        skipped_label = True
                        self.anchor(c)
                        t = (c.tail or "").lstrip(" .:)）]】")
                        self.text(t)
                        continue
                    self.node(c)
                    if c.tail:
                        self.text(c.tail)
            content = self.capture(fill)
        finally:
            self.consumed.add(body)
            self.in_note -= 1
            self.cur, self.pending = saved_cur, saved_pending
        if content:
            self.raw("\\footnote{" + content + "}")

    # -- pictures ------------------------------------------------------------------
    def image_file(self, zip_name: str) -> tuple[str, int, int] | None:
        if zip_name in self.images:
            return self.images[zip_name]
        got = None
        try:
            data = self.book.read(zip_name)
        except Exception:  # noqa: BLE001
            data = b""
        norm = _normalise_image(data) if data else None
        if norm is None:
            if data:
                self.warnings.append(f"picture skipped: {zip_name}")
        else:
            blob, ext, w, h = norm
            self.image_count += 1
            name = f"img{self.image_count:04d}.{ext}"
            os.makedirs(os.path.join(self.folder, "images"), exist_ok=True)
            with open(os.path.join(self.folder, "images", name), "wb") as f:
                f.write(blob)
            got = (f"images/{name}", w, h)
        self.images[zip_name] = got
        return got

    def _css_px(self, el: etree._Element, attr: str) -> float | None:
        v = (el.get(attr) or "").strip().lower().removesuffix("px")
        try:
            f = float(v)
            return f if f > 0 else None
        except ValueError:
            return None

    def picture(self, el: etree._Element, zip_name: str, block: bool) -> None:
        got = self.image_file(zip_name)
        if got is None or self.mode == "heading":
            return
        path, w, h = got
        st = self.style(el, _local(el))
        width = st.get("width", "")
        opts: str
        m = re.match(r"^([\d.]+)%$", width)
        aw = self._css_px(el, "width")
        ah = self._css_px(el, "height")
        if m:
            opts = f"width={min(float(m.group(1)), 100) / 100:.3f}\\linewidth"
        else:
            if aw and ah:
                w, h = aw, ah
            elif aw and w:
                h, w = h * aw / w, aw
            elif ah and h:
                w, h = w * ah / h, ah
            opts = f"width={max(1.0, w * self.px2pt):.1f}pt"
        opts += ",max width=\\linewidth,max height=0.8\\textheight"
        cmd = f"\\includegraphics[{opts}]{{{path}}}"
        if block and self.mode == "normal" and not self.table_depth:
            self.par()
            self.raw("{\\centering ")
            self.start_para()
            self.raw(cmd + "\\par}\n")
            self.para_open = False
        else:
            self.start_para()
            self.fresh_para = False
            self.raw(cmd)

    def _alone(self, el: etree._Element) -> bool:
        """Whether a picture stands on its own (no text in its paragraph)."""
        p = el.getparent()
        while p is not None and _local(p) not in _BLOCKS:
            p = p.getparent()
        if p is None:
            return True
        return not "".join(t for t in p.itertext() if t).strip()

    def el_img(self, el, tag):
        self.anchor(el)
        src = el.get("src") or _href(el)
        tgt = self.resolve(src or "", self.cur)
        if tgt and self.book.has(tgt[0]):
            self.picture(el, tgt[0], self._alone(el))
        elif (el.get("alt") or "").strip() and self.mode == "normal":
            self.text(el.get("alt"))
    el_image = el_img

    def el_svg(self, el, tag):
        self.anchor(el)
        images = [c for c in el.iter() if _local(c) == "image"]
        if images:
            for im in images:
                tgt = self.resolve(_href(im) or "", self.cur)
                if tgt and self.book.has(tgt[0]):
                    self.picture(el, tgt[0], True)
            return
        if self.mode == "heading":
            return
        # an inline drawing: rasterised as it is
        data = etree.tostring(el, encoding="utf-8")
        if b"xmlns=" not in data[:400]:
            data = data.replace(b"<svg", b'<svg xmlns="http://www.w3.org/2000/svg"', 1)
        norm = _normalise_image(data)
        if norm is None:
            return
        blob, ext, w, h = norm
        self.image_count += 1
        name = f"img{self.image_count:04d}.{ext}"
        os.makedirs(os.path.join(self.folder, "images"), exist_ok=True)
        with open(os.path.join(self.folder, "images", name), "wb") as f:
            f.write(blob)
        cmd = (f"\\includegraphics[width={max(1.0, w * self.px2pt):.1f}pt,max width=\\linewidth,"
               f"max height=0.8\\textheight]{{images/{name}}}")
        if self.table_depth or not self._alone(el):
            self.start_para()
            self.fresh_para = False
            self.raw(cmd)
            return
        self.par()
        self.raw("{\\centering ")
        self.start_para()
        self.raw(cmd + "\\par}\n")
        self.para_open = False

    def el_figcaption(self, el, tag):
        if self.mode != "normal":
            self.block(el, tag)
            return
        self.par()
        self.raw("{\\centering\\small ")
        self.anchor(el)
        self.children(el)
        self.par()
        self.raw("}\n")
    el_caption = el_figcaption

    # -- tables ----------------------------------------------------------------------
    def el_table(self, el, tag):
        rows: list[list[tuple[etree._Element | None, int, bool]]] = []
        pending_spans: dict[int, int] = {}
        caption = None
        for r in el.iter():
            t = _local(r)
            if t == "caption" and caption is None:
                caption = r
            if t != "tr":
                continue
            # only rows of this table, not of tables nested in its cells
            anc = r.getparent()
            while anc is not None and _local(anc) != "table":
                anc = anc.getparent()
            if anc is not el:
                continue
            row: list[tuple[etree._Element | None, int, bool]] = []
            col = 0
            for c in r:
                ct = _local(c)
                if ct not in ("td", "th"):
                    continue
                while pending_spans.get(col, 0) > 0:
                    pending_spans[col] -= 1
                    row.append((None, 1, False))
                    col += 1
                try:
                    span = max(1, min(50, int(c.get("colspan") or 1)))
                except ValueError:
                    span = 1
                try:
                    rspan = max(1, min(500, int(c.get("rowspan") or 1)))
                except ValueError:
                    rspan = 1
                row.append((c, span, ct == "th"))
                for k in range(span):
                    if rspan > 1:
                        pending_spans[col + k] = rspan - 1
                col += span
            while pending_spans.get(col, 0) > 0:
                pending_spans[col] -= 1
                row.append((None, 1, False))
                col += 1
            if row:
                rows.append(row)
        ncols = max((sum(s for _c, s, _h in row) for row in rows), default=0)
        self.anchor(el)
        if ncols == 0:
            return
        if ncols == 1 or self.mode != "normal":
            for row in rows:
                for c, _s, _h in row:
                    if c is not None:
                        self.par()
                        self.anchor(c)
                        self.children(c)
                        self.par()
            return
        weights = [3.0] * ncols
        for row in rows:
            col = 0
            for c, span, _h in row:
                if c is not None and span == 1:
                    txt = " ".join("".join(c.itertext()).split())
                    ln = sum(2 if _is_cjk(ord(ch)) else 1 for ch in txt)
                    weights[col] = max(weights[col], min(float(ln), 40.0))
                col += span
        weights = [w + 3 for w in weights]
        nested = self.table_depth > 0 or self.in_note > 0 or self.nest > 0
        avail = self.textwidth - 24.0 * self.nest - 12.0 * (ncols - 1)
        if nested:
            avail *= 0.9
        total = sum(weights)
        widths = [max(18.0, avail * w / total) for w in weights]
        env = "tabular" if nested else "longtable"
        self.par()
        if caption is not None:
            cap = self.capture(lambda: self.children(caption))
            if cap:
                self.raw("{\\centering\\small " + cap + "\\par}\\nobreak\n")
        spec = "".join(f"p{{{w:.1f}pt}}" for w in widths)
        self.raw(f"\\begin{{{env}}}{{@{{}}{spec}@{{}}}}\n\\toprule\n")
        self.table_depth += 1
        header_rows = 0
        for row in rows:
            if all(h for c, _s, h in row if c is not None):
                header_rows += 1
            else:
                break
        header_rows = header_rows if header_rows < len(rows) else 0
        for ri, row in enumerate(rows):
            _check(self.cancelled)
            cells: list[str] = []
            col = 0
            for c, span, head in row:
                content = ""
                if c is not None:
                    content = self.capture(lambda c=c: (self.anchor(c), self.children(c)))
                    content = re.sub(r"\n{2,}", "\\\\par ", content)
                    if head:
                        content = "\\bfseries " + content if content else ""
                if span > 1:
                    w = sum(widths[col:col + span]) + 12.0 * (span - 1)
                    edge_l = "@{}" if col == 0 else ""
                    edge_r = "@{}" if col + span == ncols else ""
                    content = f"\\multicolumn{{{span}}}{{{edge_l}p{{{w:.1f}pt}}{edge_r}}}{{{content}}}"
                cells.append(content)
                col += span
            while col < ncols:
                cells.append("")
                col += 1
            if cells and cells[0].lstrip().startswith(("[", "*")):
                cells[0] = "{}" + cells[0]
            self.raw(" & ".join(cells) + " \\\\\n")
            if header_rows and ri == header_rows - 1:
                self.raw("\\midrule\n" + ("\\endhead\n" if env == "longtable" else ""))
        self.table_depth -= 1
        self.raw(f"\\bottomrule\n\\end{{{env}}}\n")
        self.para_open = False
        self.fresh_page = False

    # ------------------------------------------------------------------ preamble
    def preamble(self) -> str:
        o = self.opt
        plan = self.plan
        md = self.book.metadata or {}
        lang = plan["lang"]
        w, h, inner, outer, top, bottom = PAPERS.get(o.paper, PAPERS["a5"])
        size = o.font_size if o.font_size in (10, 11, 12) else 11
        L: list[str] = []
        zh = lang in ("zh-hans", "zh-hant")
        ctex = zh and plan["cjk_font"] is not None
        if ctex:
            fontset = "windows" if (lang == "zh-hans" and plan["ctex_windows"]) else "none"
            L.append(f"\\documentclass[{size}pt,openany,oneside,fontset={fontset}]{{ctexbook}}")
        else:
            L.append(f"\\documentclass[{size}pt,openany,oneside]{{book}}")
        L.append("% Converted by Book Reader.  Typeset with XeLaTeX:  xelatex <this file>  (twice, for the contents)")
        L.append(f"\\usepackage[paperwidth={w}mm,paperheight={h}mm,inner={inner}mm,outer={outer}mm,"
                 f"top={top}mm,bottom={bottom}mm,headsep=5mm,footskip=9mm]{{geometry}}")
        if not ctex:
            L.append("\\usepackage{fontspec}")
        if plan["main"] and plan["main"] != "Latin Modern Roman":
            L.append(f"\\setmainfont{{{plan['main']}}}")
        for fam, macro in plan.get("fallback_macros", {}).items():
            L.append(f"\\newfontfamily{macro}{{{fam}}}")
        cjk_font = plan["cjk_font"]
        if plan["cjk"] and cjk_font:
            bold = plan["cjk_bold"]
            italic = plan.get("cjk_italic")
            if ctex and fontset == "windows":
                pass
            else:
                if not ctex:
                    L.append("\\usepackage{xeCJK}")
                parts = []
                if bold and bold != cjk_font:
                    parts.append(f"BoldFont={{{bold}}}")
                else:
                    parts.append("AutoFakeBold=2")
                if italic:
                    parts.append(f"ItalicFont={{{italic}}}")
                else:
                    parts.append("AutoFakeSlant=0.2")
                L.append(f"\\setCJKmainfont{{{cjk_font}}}[{','.join(parts)}]")
                L.append(f"\\setCJKsansfont{{{bold or cjk_font}}}")
                L.append(f"\\setCJKmonofont{{{cjk_font}}}")
            if plan["cjk_fallback"]:
                L.append("\\xeCJKsetup{AutoFallBack=true}")
                L.append(f"\\setCJKfallbackfamilyfont{{\\CJKrmdefault}}{{{plan['cjk_fallback']}}}")
            if not plan["cjk_primary"]:
                L.append('\\xeCJKDeclareCharClass{Default}{"2014,"2018,"2019,"201C,"201D,"2026}')
            L.append("\\usepackage{xeCJKfntef}")
        else:
            L.append("\\usepackage[normalem]{ulem}")
            L.append("\\newcommand\\CJKunderline{\\uline}\\newcommand\\CJKsout{\\sout}")
        L.append("\\usepackage{graphicx}")
        L.append("\\usepackage[export]{adjustbox}")
        L.append("\\usepackage{longtable,booktabs,array}")
        L.append("\\usepackage{enumitem}")
        L.append("\\usepackage{amsmath,amssymb}")
        L.append("\\usepackage{fancyhdr}")
        L.append("\\usepackage{eso-pic}")
        if ctex:
            L.append("\\ctexset{chapter={format=\\centering\\LARGE\\bfseries,beforeskip=0.08\\textheight,"
                     "afterskip=2em,pagestyle=plain},section={format=\\Large\\bfseries\\raggedright},"
                     "subsection={format=\\large\\bfseries\\raggedright}}")
            if lang == "zh-hant":
                L.append("\\ctexset{contentsname={目錄}}")
        else:
            L.append("\\usepackage{titlesec}")
            L.append("\\titleformat{\\chapter}[block]{\\normalfont\\LARGE\\bfseries\\centering}{}{0pt}{}")
            L.append("\\titlespacing*{\\chapter}{0pt}{0.08\\textheight}{2em}")
            L.append("\\titleformat{\\section}{\\normalfont\\Large\\bfseries}{}{0pt}{}")
            L.append("\\titleformat{\\subsection}{\\normalfont\\large\\bfseries}{}{0pt}{}")
            L.append("\\titleformat{\\subsubsection}{\\normalfont\\normalsize\\bfseries}{}{0pt}{}")
            name = _CONTENTS_NAME.get(lang) or _CONTENTS_NAME.get(lang.split("-")[0]) or "Contents"
            L.append(f"\\renewcommand\\contentsname{{{self.esc(name)}}}")
            if lang == "ja":
                L.append("\\setlength{\\parindent}{1em}")
        L.append("\\setcounter{tocdepth}{3}")
        L.append("\\pagestyle{fancy}\\fancyhf{}\\renewcommand{\\headrulewidth}{0pt}")
        L.append("\\fancyhead[C]{\\small\\itshape\\nouppercase{\\leftmark}}\\fancyfoot[C]{\\small\\thepage}")
        L.append("\\fancypagestyle{plain}{\\fancyhf{}\\fancyfoot[C]{\\small\\thepage}"
                 "\\renewcommand{\\headrulewidth}{0pt}}")
        L.append("\\setlength{\\emergencystretch}{3em}\\raggedbottom")
        # copy and search in the PDF give back the text as written (not a font's glyph names:
        # Cambria's hyphen would otherwise come back as U+2011, "ά" as "αʆ")
        L.append("\\XeTeXgenerateactualtext=1")
        L.append("\\setlist{itemsep=0.2ex,topsep=0.6ex}")
        L.append("\\newcommand\\erhead[2]{\\par\\addvspace{1.2ex plus 0.4ex}{\\noindent\\normalfont\\bfseries"
                 "\\ifcase#1\\or\\LARGE\\or\\Large\\or\\large\\else\\normalsize\\fi #2\\par}\\nobreak"
                 "\\addvspace{0.8ex}}")
        L.append("\\newcommand\\erruby[2]{\\leavevmode\\begin{tabular}[b]{@{}c@{}}{\\tiny #2}\\\\[-0.4ex]#1"
                 "\\end{tabular}}")
        title = str(md.get("title") or "").strip()
        authors = ", ".join(str(a) for a in md.get("authors") or [] if str(a).strip())
        pdf_lang = (str(md.get("language") or "")).strip()
        L.append("\\usepackage[hidelinks,bookmarksopen=true,bookmarksopenlevel=0]{hyperref}")
        hs = [f"pdftitle={{{self.esc(title)}}}", f"pdfauthor={{{self.esc(authors)}}}",
              "pdfcreator={Book Reader}"]
        if re.fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*", pdf_lang):
            hs.append(f"pdflang={{{pdf_lang}}}")
        L.append("\\hypersetup{" + ",".join(hs) + "}")
        return "\n".join(L) + "\n"

    def front(self) -> str:
        md = self.book.metadata or {}
        title = str(md.get("title") or "").strip() or os.path.splitext(os.path.basename(self.book.path))[0]
        authors = [str(a).strip() for a in md.get("authors") or [] if str(a).strip()]
        publisher = str(md.get("publisher") or "").strip()
        F: list[str] = ["\\begin{document}"]
        cover = None
        if self.opt.cover and self.cover_file:
            got = self.image_file(self.cover_file)
            if got:
                cover = got[0]
        if cover:
            F.append("\\begin{titlepage}\n\\AddToShipoutPictureBG*{\\AtPageCenter{\\makebox[0pt]{\\raisebox{-0.5\\height}"
                     f"{{\\includegraphics[width=\\paperwidth,height=\\paperheight,keepaspectratio]{{{cover}}}}}}}}}}}"
                     "\n\\mbox{}\n\\end{titlepage}")
        else:
            F.append("\\begin{titlepage}\n\\centering\n\\vspace*{0.2\\textheight}\n"
                     f"{{\\Huge\\bfseries {self.esc(title)}\\par}}\n\\vspace{{2em}}\n"
                     + (f"{{\\Large {self.esc(', '.join(authors))}\\par}}\n" if authors else "")
                     + "\\vfill\n" + (f"{{\\large {self.esc(publisher)}\\par}}\n" if publisher else "")
                     + "\\end{titlepage}")
        if self.opt.contents and self.chapters_expected():
            F.append("\\tableofcontents\\clearpage")
        return "\n".join(F) + "\n"

    def chapters_expected(self) -> bool:
        return bool(self.toc_at)


# ==========================================================================
# entry points
# ==========================================================================

def write_latex(book: EpubBook, folder: str, stem: str, options: ExportOptions | None = None, *,
                progress: Progress | None = None, cancelled: Cancelled | None = None) -> ExportResult:
    """Write ``<folder>/<stem>.tex`` and ``<folder>/images/`` for ``book`` (no typesetting)."""
    options = options or ExportOptions()
    os.makedirs(folder, exist_ok=True)
    w = _Writer(book, options, folder, progress, cancelled)
    w.survey()
    w.plan_fonts()
    front = w.front()
    body = w.render()
    tex = w.preamble() + "\n" + front + body.strip("\n") + "\n\n\\end{document}\n"
    tex = re.sub(r"\n{3,}", "\n\n", tex)
    tex_path = os.path.join(folder, stem + ".tex")
    tmp = tex_path + ".part"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(tex)
    os.replace(tmp, tex_path)
    res = ExportResult(folder=folder, tex_path=tex_path, images=w.image_count, chapters=w.chapters,
                       warnings=w.warnings)
    res.estimate_pages = _estimate_pages(w, options)
    return res


def _estimate_pages(w: _Writer, options: ExportOptions) -> int:
    cjk = sum(1 for c in w.chars if _is_cjk(ord(c)))
    total = 0
    for doc in w.docs:
        if doc in w.skip_docs:
            continue
        try:
            total += len(w.book.plain_text(doc))
        except Exception:  # noqa: BLE001
            pass
    area = w.textwidth * w.textheight / (138 * 184 * _PT_PER_MM * _PT_PER_MM)
    per_page = (550 if cjk > 200 else 1900) * area * (11 / max(10, options.font_size)) ** 2
    return max(1, int(total / max(1.0, per_page)) + w.image_count // 3 + w.chapters // 2 + 2)


def export_book(book: EpubBook, destination: str, options: ExportOptions | None = None, *,
                progress: Progress | None = None, cancelled: Cancelled | None = None,
                engine: str | None = None, stem: str | None = None,
                typesetter: "Typesetter | None" = None) -> ExportResult:
    """Convert ``book`` into a new folder under ``destination``: .tex, images and (when possible) PDF.

    ``typesetter`` replaces the search for an installed XeLaTeX: the Android build passes
    the TeX engine it carries inside the app.  It is called as
    ``typesetter(tex_path, pdf_path, progress=…, cancelled=…)`` and returns a
    :class:`CompileResult`.

    On cancellation the new folder is removed again.  The book's own file is never touched.
    """
    options = options or ExportOptions()
    md = book.metadata or {}
    stem = safe_stem(stem or str(md.get("title") or "") or
                     os.path.splitext(os.path.basename(book.path or "book"))[0])
    os.makedirs(destination, exist_ok=True)
    folder = _unique_dir(destination, stem)
    os.makedirs(folder)
    try:
        res = write_latex(book, folder, stem, options, progress=progress, cancelled=cancelled)
        if options.compile_pdf:
            pdf_path = os.path.join(folder, stem + ".pdf")
            cr = None
            if typesetter is not None:
                res.engine = getattr(typesetter, "name", "built-in")
                res.compiled = True
                cr = typesetter(res.tex_path, pdf_path, progress=progress, cancelled=cancelled)
            else:
                engine = engine or find_xelatex()
                res.engine = engine
                if engine:
                    res.compiled = True
                    cr = compile_pdf(res.tex_path, pdf_path, progress=progress,
                                     cancelled=cancelled, engine=engine,
                                     min_passes=2 if (options.contents or res.chapters) else 1,
                                     estimate_pages=res.estimate_pages)
            if cr is not None:
                res.pdf_path = cr.pdf_path
                res.pages = cr.pages
                res.problems = cr.problems
                if cr.missing_chars:
                    res.warnings.append(f"{cr.missing_chars} character(s) missing from the fonts")
        return res
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise


def _main(argv: list[str]) -> int:  # pragma: no cover - developer tool
    import argparse
    ap = argparse.ArgumentParser(description="Convert a book to LaTeX (and PDF with XeLaTeX).")
    ap.add_argument("book")
    ap.add_argument("--out", default=".")
    ap.add_argument("--paper", default="a5", choices=sorted(PAPERS))
    ap.add_argument("--size", type=int, default=11, choices=(10, 11, 12))
    ap.add_argument("--no-pdf", action="store_true")
    ap.add_argument("--no-cover", action="store_true")
    ap.add_argument("--fonts", default="system", choices=("system", "texlive"),
                    help="system: the fonts installed here; texlive: only fonts TeX itself ships "
                         "(what the Android build uses)")
    ap.add_argument("--cache", default=None, help="conversion cache folder (default: the app's)")
    a = ap.parse_args(argv)
    from PySide6.QtGui import QGuiApplication
    app = QGuiApplication.instance() or QGuiApplication(["latexexport"])  # noqa: F841 - SVG text needs it
    import bookformats
    book = bookformats.open_book(a.book, cache_root=a.cache)
    try:
        last: list[tuple[str, int]] = [("", -1)]

        def prog(stage: str, done: int, total: int) -> None:
            if (stage, done) != last[0]:
                last[0] = (stage, done)
                print(f"\r{stage:10s} {done}/{total}   ", end="", file=sys.stderr, flush=True)
        res = export_book(book, a.out, ExportOptions(paper=a.paper, font_size=a.size, cover=not a.no_cover,
                                                     compile_pdf=not a.no_pdf, fonts=a.fonts), progress=prog)
    finally:
        book.close()
    print(file=sys.stderr)
    print(f"tex: {res.tex_path}\npdf: {res.pdf_path}\npages: {res.pages}  chapters: {res.chapters}  "
          f"images: {res.images}")
    for p in res.problems[:20]:
        print("problem:", p)
    for wmsg in res.warnings[:20]:
        print("warning:", wmsg)
    return 0 if (res.pdf_path or a.no_pdf) else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main(sys.argv[1:]))
