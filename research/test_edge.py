# -*- coding: utf-8 -*-
"""Edge-case battery for the resolver and the lenient XML/HTML fallback."""
import io, os, sys, zipfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from epub_resolve import ZipIndex, resolve_href, split_href, is_remote
from epub_model import parse_xml_lenient, text_of, find_deep, ln, nav_type

FAIL = 0


def check(label, got, want):
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(" %s %-52s got=%r%s" % ("OK " if ok else "FAIL", label, got,
                                  "" if ok else "  want=%r" % (want,)))


# ---------------------------------------------------------------- resolver
buf = io.BytesIO()
NAMES = [
    "OEBPS/content.opf",
    "OEBPS/text/ch 1.xhtml",          # real space
    "OEBPS/text/100%25 real.xhtml",   # literal '%25' in the NAME
    "OEBPS/Images/Pic.JPG",           # mixed case
    "OEBPS/css/style.css",
    "OEBPS/text/sub/deep.xhtml",
    "Misplaced\\Win\\page.xhtml",     # backslash separators
    "OEBPS/unicode/été.xhtml",  # e-acute, NFC
    "OEBPS/empty/",                   # a directory entry
]
with zipfile.ZipFile(buf, "w") as z:
    for n in NAMES:
        z.writestr(n, b"x")
zf = zipfile.ZipFile(buf)
ix = ZipIndex(zf)

print("=== resolver ===")
B = "OEBPS"


def R(base, href):
    return resolve_href(ix, base, href)[0]


check("plain", R(B, "css/style.css"), "OEBPS/css/style.css")
check("percent-encoded space", R(B, "text/ch%201.xhtml"), "OEBPS/text/ch 1.xhtml")
check("un-encoded space", R(B, "text/ch 1.xhtml"), "OEBPS/text/ch 1.xhtml")
check("'+' is not a space", R(B, "text/ch+1.xhtml"), None)
check("literal %25 in name", R(B, "text/100%2525 real.xhtml"),
      "OEBPS/text/100%25 real.xhtml")
check("raw %25 (double-decode trap)", R(B, "text/100%25 real.xhtml"),
      "OEBPS/text/100%25 real.xhtml")
check("dot-dot up", R("OEBPS/text", "../css/style.css"), "OEBPS/css/style.css")
check("dot-dot round trip", R(B, "text/../css/style.css"), "OEBPS/css/style.css")
check("dot-dot escapes root -> clamped",
      R(B, "../../../../css/style.css"), "OEBPS/css/style.css")
check("leading slash", R(B, "/OEBPS/css/style.css"), "OEBPS/css/style.css")
check("double slash", R(B, "css//style.css"), "OEBPS/css/style.css")
check("./ prefix", R(B, "./css/style.css"), "OEBPS/css/style.css")
check("wrong case", R(B, "images/pic.jpg"), "OEBPS/Images/Pic.JPG")
# NOTE: zipfile._sanitize_filename() rewrites os.sep -> "/" on Windows for BOTH
# writing and reading, so the entry above is actually stored/reported as
# "Misplaced/Win/page.xhtml". On Linux a genuine backslash entry survives into
# namelist(); the resolver's backslash->slash normalisation covers both, and
# covers the far more common case of a backslash inside the HREF.
check("backslash in href", R(B, "..\\Misplaced\\Win\\page.xhtml"),
      "Misplaced/Win/page.xhtml")
check("backslash href, root base", R("", "Misplaced\\Win\\page.xhtml"),
      "Misplaced/Win/page.xhtml")
check("unicode NFC", R(B, "unicode/%C3%A9t%C3%A9.xhtml"),
      "OEBPS/unicode/été.xhtml")
check("deep relative", R("OEBPS/text/sub", "../ch%201.xhtml"),
      "OEBPS/text/ch 1.xhtml")
check("directory entry never matches", R(B, "empty/"), None)
check("missing file", R(B, "nope.xhtml"), None)
check("basename rescue (wrong dir)", R(B, "wrong/dir/style.css"),
      "OEBPS/css/style.css")
check("root base dir", R("", "OEBPS/content.opf"), "OEBPS/content.opf")

print("\n=== split_href / is_remote ===")
check("frag decoded", split_href("a.xhtml#sec%201"), ("a.xhtml", "sec 1"))
check("query dropped", split_href("a.xhtml?v=2#f"), ("a.xhtml", "f"))
check("bare frag", split_href("#top"), ("", "top"))
check("http remote", is_remote("https://x.org/a.css"), True)
check("protocol-relative", is_remote("//x.org/a.css"), True)
check("data url", is_remote("data:image/gif;base64,R0lGOD"), True)
check("mailto", is_remote("mailto:a@b.c"), True)
check("relative not remote", is_remote("text/a.xhtml"), False)
check("frag not remote", is_remote("#x"), False)

# ------------------------------------------------- lenient XML/HTML fallback
print("\n=== lenient parsing ===")
CASES = {
    "well-formed xhtml":
        b'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
        b'<body><p>ok</p></body></html>',
    "BOM + leading junk":
        b'\xef\xbb\xbf\n\n<?xml version="1.0"?><html><body><p>ok</p></body></html>',
    "undeclared entity &nbsp;":
        b'<html><body><p>a&nbsp;b&mdash;c</p></body></html>',
    "raw ampersand":
        b'<html><body><p>Tom &amp; Jerry &copy; AT&T</p></body></html>',
    "unclosed tags (tag soup)":
        b'<html><body><p>one<br><p>two<i>three</p></body></html>',
    "mismatched nesting":
        b'<html><body><b><i>x</b></i></body></html>',
    "html5 doctype, no ns":
        b'<!DOCTYPE html><html><body><p>ok</p></body></html>',
}
for name, data in CASES.items():
    try:
        root = parse_xml_lenient(data, name)
        ps = find_deep(root, "p")
        check(name, bool(root is not None), True)
    except Exception as e:
        check(name, "EXC:%s" % e, True)

print("\n=== nav parsing through the HTML recovery path ===")
# A nav document that is NOT well-formed XML -> must still yield a TOC.
NAV = (b'<!DOCTYPE html><html xmlns:epub="http://www.idpf.org/2007/ops">'
       b'<body><nav epub:type="toc"><ol>'
       b'<li><a href="c1.xhtml">One &amp; only<br></a>'
       b'<ol><li><a href="c1.xhtml#s">1.1 <b>deep</b></a></ol></li>'
       b'<li hidden><a href="c2.xhtml">Two</a></li>'
       b'</ol></nav></body></html>')
root = parse_xml_lenient(NAV, "nav")
navs = find_deep(root, "nav")
check("found <nav>", len(navs), 1)
check("epub:type readable after HTML recovery", nav_type(navs[0]), "toc")
anchors = find_deep(navs[0], "a")
check("anchor count", len(anchors), 3)
check("label concatenation", text_of(anchors[0]), "One & only")
check("nested markup label", text_of(anchors[1]), "1.1 deep")
lis = find_deep(navs[0], "li")
check("hidden boolean attr present", "hidden" in lis[-1].attrib, True)

print("\n%s  (%d failures)" % ("ALL PASS" if not FAIL else "FAILURES", FAIL))
sys.exit(1 if FAIL else 0)
