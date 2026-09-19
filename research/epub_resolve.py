# -*- coding: utf-8 -*-
"""
epub_resolve.py -- href -> zip-entry resolution for an EPUB container.

This is the single most bug-prone piece of an EPUB reader. Rules encoded here:

 * Zip entry names are POSIX-ish, '/'-separated, declared UTF-8 (OCF 4.3.2).
   NEVER use os.path / ntpath: on Windows ntpath.normpath rewrites '/' to '\\'
   and "OEBPS/a.xhtml" becomes "OEBPS\\a.xhtml", which is not a zip entry.
   Always posixpath.
 * hrefs in the OPF are URLs relative to the *package document's directory*
   (EPUB 3.3 sec 5.2: parse with the content URL of the package document as base).
 * hrefs inside a content document are relative to *that document's* directory.
 * A fragment ('#id') and a query ('?x') are never part of the entry name.
 * Percent-decoding: '%20' -> ' '. '+' is NOT a space in a path (that is
   application/x-www-form-urlencoded only) -- urllib.parse.unquote leaves it.
 * '..' must never escape the container root (OCF 4.2.5: parsing ".." against
   the container root URL returns the container root URL). Clamp, do not raise.
 * Malformed files: backslash separators, wrong case, stray leading '/',
   double slashes, a literal '%' that was never meant as an escape, and NFD vs
   NFC Unicode names. Handle all of these with an index of normalized keys,
   built once per book.
"""

from __future__ import annotations

import posixpath
import unicodedata
import zipfile
from urllib.parse import unquote, urlsplit


# --------------------------------------------------------------------------
# href splitting
# --------------------------------------------------------------------------

def split_href(href: str) -> tuple[str, str]:
    """Split a raw href into (path_part, fragment).

    The fragment is returned WITHOUT the leading '#', already percent-decoded,
    because callers scroll to an element id. Query strings are discarded --
    nothing inside a zip answers a query.

    >>> split_href("text/ch1.xhtml#sec%201")
    ('text/ch1.xhtml', 'sec 1')
    >>> split_href("#top")
    ('', 'top')
    >>> split_href("ch1.xhtml?x=1#f")
    ('ch1.xhtml', 'f')
    """
    if href is None:
        return "", ""
    href = href.strip()
    path, _, frag = href.partition("#")
    path = path.partition("?")[0]
    return path, unquote(frag)


def is_remote(href: str) -> bool:
    """True for anything that is not a resource inside the container.

    Covers http(s)://, //cdn.example.com/x.css, data:, mailto:, file:, blob:.
    EPUB 3.3 sec 3.6 allows remote audio/video/fonts; a reader must not try to
    look those up in the zip.

    >>> [is_remote(u) for u in ("https://x/y.css", "//x/y.css", "data:image/png;base64,AA", "a/b.css", "C:/x")]
    [True, True, True, False, True]
    """
    if not href:
        return False
    s = urlsplit(href)
    if s.scheme and s.netloc:
        return True
    if href.startswith("//"):
        return True
    # scheme with no authority: data:, mailto:, javascript:, tel:, urn:
    # NB a Windows drive letter ("C:/x") parses as scheme 'c' -- also not in-zip.
    if s.scheme:
        return True
    return False


# --------------------------------------------------------------------------
# normalization used by the fallback index
# --------------------------------------------------------------------------

def _norm_key(name: str) -> str:
    """Aggressive key for the forgiving fallback lookup.

    Backslash -> slash, collapse '//', NFC-fold, casefold. Only used when the
    exact (spec-correct, case-sensitive) lookup has already failed.
    """
    name = name.replace("\\", "/")
    while "//" in name:
        name = name.replace("//", "/")
    return unicodedata.normalize("NFC", name).casefold()


def normalize_zip_path(base_dir: str, href_path: str) -> str:
    """Join a (already fragment-stripped, percent-decoded) href onto base_dir
    and normalize to a container-root-relative POSIX path.

    base_dir is '' for the container root, else e.g. 'OEBPS' or 'OEBPS/text'.

    >>> normalize_zip_path("OEBPS", "images/../cover.jpg")
    'OEBPS/cover.jpg'
    >>> normalize_zip_path("OEBPS/text", "../images/a.png")
    'OEBPS/images/a.png'
    >>> normalize_zip_path("OEBPS", "/abs/x.css")      # stray leading slash
    'abs/x.css'
    >>> normalize_zip_path("OEBPS", "../../../../etc/passwd")   # clamped
    'etc/passwd'
    >>> normalize_zip_path("", "./a//b.xhtml")
    'a/b.xhtml'
    >>> normalize_zip_path("OEBPS", "sub\\\\page.xhtml")   # Windows-style, malformed
    'OEBPS/sub/page.xhtml'
    """
    p = (href_path or "").replace("\\", "/")

    if p.startswith("/"):
        # An absolute-looking href. OCF says parsing '/' against the container
        # root URL yields the container root, so treat it as root-relative.
        joined = p.lstrip("/")
    else:
        joined = posixpath.join(base_dir, p) if base_dir else p

    # posixpath.normpath collapses '.', '..' and '//'. It keeps leading '..'
    # when the path escapes -- strip those (clamp to root), never raise.
    out = posixpath.normpath(joined)
    while out.startswith("../"):
        out = out[3:]
    if out in ("..", ".", "/"):
        out = ""
    return out.lstrip("/")


# --------------------------------------------------------------------------
# the resolver
# --------------------------------------------------------------------------

def open_zip_with_encoding(path: str) -> zipfile.ZipFile:
    """Open an EPUB, repairing cp437-mojibake entry names.

    OCF requires UTF-8 names, but old Windows zip tools omit the UTF-8 general
    purpose flag (bit 11 / 0x800) and write raw GBK/Shift-JIS/cp1252 bytes.
    zipfile then decodes them as cp437, producing names like
    '─┐┬╝/╡┌╥╗╒┬.xhtml' that no href will ever match.

    Detect that case and reopen with an explicit metadata_encoding.
    """
    zf = zipfile.ZipFile(path)
    suspects = [i for i in zf.infolist()
                if not (i.flag_bits & 0x800) and any(ord(c) > 127 for c in i.filename)]
    if not suspects:
        return zf
    for enc in ("utf-8", "gbk", "shift_jis", "cp949", "cp1252"):
        try:
            cand = zipfile.ZipFile(path, metadata_encoding=enc)
        except (LookupError, ValueError):
            continue
        names = [i.filename for i in cand.infolist()]
        # Heuristic: the right encoding produces names without cp437 box-drawing
        # junk (U+2500..U+25FF) and without replacement chars.
        bad = sum(1 for n in names
                  for c in n if "─" <= c <= "◿" or c == "�")
        if bad == 0:
            zf.close()
            return cand
        cand.close()
    return zf


class ZipIndex:
    """Entry-name index for one open EPUB zip. Build once per book."""

    __slots__ = ("names", "_exact", "_loose")

    def __init__(self, zf: zipfile.ZipFile):
        # Directory entries (trailing '/') are never resources -- drop them so
        # a href of 'images/' can't resolve to a 0-byte 'directory'.
        self.names = [n for n in zf.namelist() if not n.endswith("/")]
        self._exact = set(self.names)
        # Last writer wins is wrong; FIRST writer wins matches zipfile.read(),
        # which returns the last entry for a duplicate name... but for *distinct*
        # names that merely collide after casefolding, first is the stable pick.
        self._loose: dict[str, str] = {}
        for n in self.names:
            self._loose.setdefault(_norm_key(n), n)

    def lookup(self, candidate: str) -> str | None:
        if candidate in self._exact:            # spec-correct, case sensitive
            return candidate
        return self._loose.get(_norm_key(candidate))   # malformed-file rescue


def resolve_href(index: ZipIndex, base_dir: str, href: str
                 ) -> tuple[str | None, str, bool]:
    """Resolve one href to a zip entry name.

    Returns (entry_name_or_None, fragment, is_remote_flag).

    base_dir: directory of the document the href appeared in, container-root
    relative, no trailing slash ('' for root). For hrefs read out of the OPF
    this is posixpath.dirname(opf_path).

    Resolution order (each step only runs if the previous failed):
      1. percent-decoded path          -- the spec-correct interpretation
      2. raw, undecoded path           -- rescues entries whose name really
                                          contains a '%' character
      3. latin-1 re-decode             -- rescues names percent-encoded from
                                          cp1252/latin-1 instead of UTF-8
      4. basename match, if unique     -- last resort for books whose hrefs
                                          have the wrong directory prefix
    """
    path, frag = split_href(href)
    if is_remote(href):
        return None, frag, True
    if not path:
        return None, frag, False        # pure '#fragment' -> same document

    # 1. spec-correct: percent-decode as UTF-8
    cand = normalize_zip_path(base_dir, unquote(path))
    hit = index.lookup(cand)
    if hit:
        return hit, frag, False

    # 2. the href was never really percent-encoded (literal '%' in the name)
    raw = normalize_zip_path(base_dir, path)
    if raw != cand:
        hit = index.lookup(raw)
        if hit:
            return hit, frag, False

    # 3. percent-escapes that are not UTF-8 (legacy cp1252 producers)
    try:
        alt = unquote(path, encoding="latin-1")
        alt = normalize_zip_path(base_dir, alt)
        if alt not in (cand, raw):
            hit = index.lookup(alt)
            if hit:
                return hit, frag, False
    except Exception:
        pass

    # 4. unique basename anywhere in the zip
    want = _norm_key(posixpath.basename(cand))
    if want:
        matches = [n for n in index.names if _norm_key(posixpath.basename(n)) == want]
        if len(matches) == 1:
            return matches[0], frag, False

    return None, frag, False


if __name__ == "__main__":
    import doctest
    fails, run = doctest.testmod(verbose=False)
    print("doctests: %d run, %d failed" % (run, fails))
    raise SystemExit(1 if fails else 0)
