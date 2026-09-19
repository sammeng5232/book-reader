# -*- coding: utf-8 -*-
"""Build adversarial EPUB fixtures + prove the Windows path gotchas."""
import os, sys, zipfile, hashlib, re, posixpath, ntpath
from urllib.parse import unquote

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
os.makedirs(OUT, exist_ok=True)

print("### GOTCHA 1: ntpath.normpath mangles zip paths on Windows")
print("  os.path is ntpath here?", os.path is ntpath)
print("  os.path.normpath('OEBPS/text/../a.xhtml') =", repr(os.path.normpath("OEBPS/text/../a.xhtml")))
print("  posixpath.normpath(same)                  =", repr(posixpath.normpath("OEBPS/text/../a.xhtml")))
print("  os.path.join('OEBPS','a.xhtml')           =", repr(os.path.join("OEBPS", "a.xhtml")))
print("  posixpath.join(same)                      =", repr(posixpath.join("OEBPS", "a.xhtml")))

print("\n### GOTCHA 2: '+' is NOT a space in a URL path")
print("  unquote('a+b%20c.xhtml') =", repr(unquote("a+b%20c.xhtml")))

print("\n### GOTCHA 3: zipfile with duplicate names")
print("\n### GOTCHA 4: ZipFile.namelist() does NOT normalize separators")


def wr(z, name, data, stored=False):
    if isinstance(data, str):
        data = data.encode("utf-8")
    zi = zipfile.ZipInfo(name)
    zi.compress_type = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
    zi.external_attr = 0o600 << 16
    z.writestr(zi, data)


XHTML = ('<?xml version="1.0" encoding="utf-8"?>\n'
         '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
         '<head><title>%s</title></head><body id="top"><h1>%s</h1>'
         '<p id="sec 1">body</p></body></html>')

# ---------------------------------------------------------------- fixture A
# EPUB3: nav doc, rtl, percent-encoded href with a space, '..' escape,
# duplicate manifest ids, dangling spine idref, fixed-layout override,
# IDPF-obfuscated font, non-linear spine item, nested nav + hidden + landmarks.
pathA = os.path.join(OUT, "a_epub3_nasty.epub")
UID = "urn:uuid:00000000-1111-2222-3333-444444444444"


WS = tuple(chr(c) for c in (0x20, 0x09, 0x0D, 0x0A))
def idpf_key(uid):
    s = "".join(ch for ch in uid if ch not in WS)

    return hashlib.sha1(s.encode("utf-8")).digest()


def obfuscate(data, key, n):
    n = min(n, len(data))
    b = bytearray(data[:n])
    for i in range(n):
        b[i] ^= key[i % len(key)]
    return bytes(b) + data[n:]


FONT = b"\x00\x01\x00\x00" + bytes(range(256)) * 8   # fake TTF, 2052 bytes
with zipfile.ZipFile(pathA, "w") as z:
    wr(z, "mimetype", "application/epub+zip", stored=True)
    wr(z, "META-INF/container.xml",
       '<?xml version="1.0"?>\n'
       '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
       '<rootfiles>'
       '<rootfile full-path="EPUB/pkg other.opf" media-type="application/oebps-package+xml"/>'
       '<rootfile full-path="EPUB/alt.opf" media-type="application/oebps-package+xml"/>'
       '</rootfiles></container>')
    wr(z, "META-INF/encryption.xml",
       '<?xml version="1.0"?>\n'
       '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"'
       ' xmlns:enc="http://www.w3.org/2001/04/xmlenc#">'
       '<enc:EncryptedData><enc:EncryptionMethod Algorithm="http://www.idpf.org/2008/embedding"/>'
       '<enc:CipherData><enc:CipherReference URI="EPUB/fonts/My%20Font.ttf"/></enc:CipherData>'
       '</enc:EncryptedData></encryption>')
    wr(z, "EPUB/fonts/My Font.ttf", obfuscate(FONT, idpf_key(UID), 1040))
    wr(z, "EPUB/pkg other.opf", """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="pub-id"
         xml:lang="ar" dir="rtl">
 <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
  <dc:identifier id="pub-id">%s</dc:identifier>
  <dc:title id="t1">Main Title</dc:title>
  <meta refines="#t1" property="title-type">main</meta>
  <dc:title id="t2">A Subtitle</dc:title>
  <meta refines="#t2" property="title-type">subtitle</meta>
  <dc:creator id="c1">E. H. Shepard</dc:creator>
  <meta refines="#c1" property="role" scheme="marc:relators">ill</meta>
  <meta refines="#c1" property="file-as">Shepard, E. H.</meta>
  <dc:creator id="c2">A. A. Milne</dc:creator>
  <meta refines="#c2" property="role" scheme="marc:relators">aut</meta>
  <dc:language>ar</dc:language>
  <dc:publisher>Test Press</dc:publisher>
  <dc:date>2020-01-02</dc:date>
  <meta property="dcterms:modified">2020-01-02T03:04:05Z</meta>
  <meta property="rendition:layout">reflowable</meta>
 </metadata>
 <manifest>
  <item id="nav"   href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
  <item id="ch1"   href="text/ch%%201.xhtml" media-type="application/xhtml+xml"/>
  <item id="ch2"   href="text/../text/ch2.xhtml" media-type="application/xhtml+xml"/>
  <item id="ch1"   href="text/DUPLICATE.xhtml" media-type="application/xhtml+xml"/>
  <item id="fxl"   href="text/fixed.xhtml" media-type="application/xhtml+xml"/>
  <item id="notes" href="text/notes.xhtml" media-type="application/xhtml+xml"/>
  <item id="cov"   href="img/cover.png" media-type="image/png" properties="cover-image"/>
  <item id="fnt"   href="fonts/My%%20Font.ttf" media-type="font/ttf"/>
  <item id="rem"   href="https://example.com/remote.css" media-type="text/css"/>
  <item id="ghost" href="text/does-not-exist.xhtml" media-type="application/xhtml+xml"/>
 </manifest>
 <spine page-progression-direction="rtl" toc="ncx-that-does-not-exist">
  <itemref idref="ch1"/>
  <itemref idref="ch2"/>
  <itemref idref="fxl" properties="rendition:layout-pre-paginated page-spread-left"/>
  <itemref idref="notes" linear="no"/>
  <itemref idref="MISSING-ID"/>
  <itemref idref="ghost"/>
 </spine>
</package>
""" % UID)
    wr(z, "EPUB/alt.opf", '<package xmlns="http://www.idpf.org/2007/opf" version="3.0"/>')
    wr(z, "EPUB/nav.xhtml", """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>nav</title></head><body>
<nav epub:type="toc" id="toc"><h1>Contents</h1><ol>
 <li><a href="text/ch%201.xhtml">Chapter <b>One</b> &amp; a half</a>
     <ol><li><a href="text/ch%201.xhtml#sec%201">1.1 Deep</a></li></ol></li>
 <li><span>Part Two</span><ol>
     <li><a href="text/ch2.xhtml">Chapter Two</a></li>
     <li hidden=""><a href="text/notes.xhtml">Hidden Notes</a></li></ol></li>
 <li><a href="text/fixed.xhtml">Fixed page</a></li>
</ol></nav>
<nav epub:type="landmarks" hidden="hidden"><h2>Guide</h2><ol>
 <li><a epub:type="toc" href="nav.xhtml#toc">Table of Contents</a></li>
 <li><a epub:type="bodymatter" href="text/ch%201.xhtml">Start</a></li>
 <li><a epub:type="cover" href="text/ch2.xhtml">Cover</a></li>
</ol></nav>
<nav epub:type="page-list"><ol>
 <li><a href="text/ch%201.xhtml#p1">1</a></li>
 <li><a href="text/ch2.xhtml#p2">2</a></li>
</ol></nav>
</body></html>""")
    wr(z, "EPUB/text/ch 1.xhtml", XHTML % ("Ch1", "Chapter One"))
    wr(z, "EPUB/text/ch2.xhtml", XHTML % ("Ch2", "Chapter Two"))
    wr(z, "EPUB/text/DUPLICATE.xhtml", XHTML % ("Dup", "Dup"))
    wr(z, "EPUB/text/notes.xhtml", XHTML % ("Notes", "Notes"))
    wr(z, "EPUB/text/fixed.xhtml",
       '<?xml version="1.0" encoding="utf-8"?>\n<html xmlns="http://www.w3.org/1999/xhtml">'
       '<head><title>fx</title><meta name="viewport" content="width=1200, height=1600"/>'
       '</head><body><p>fixed</p></body></html>')
    wr(z, "EPUB/img/cover.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)

# ---------------------------------------------------------------- fixture B
# EPUB2-ish disaster: no mimetype, OPF with NO namespace, backslash zip entry,
# wrong-case href, NCX with nesting + weird playOrder, meta name=cover,
# guide reference, XHTML that is not well-formed XML.
pathB = os.path.join(OUT, "b_broken_epub2.epub")
with zipfile.ZipFile(pathB, "w") as z:
    # deliberately NO mimetype entry, and container.xml is not first
    wr(z, "OPS\\chapters\\c1.html",                     # backslash separator!
       '<html><head><title>C1</title></head><body><p>unclosed<br>'
       '<img src="../images/PIC.JPG"> &nbsp; &copy; raw & ampersand</body></html>')
    wr(z, "META-INF/container.xml",
       '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
       '<rootfiles><rootfile full-path="OPS/content.opf"'
       ' media-type="application/oebps-package+xml"/></rootfiles></container>')
    wr(z, "OPS/content.opf", """<?xml version="1.0" encoding="utf-8"?>
<package version="2.0" unique-identifier="BookId">
 <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"
           xmlns:opf="http://www.idpf.org/2007/opf">
  <dc:identifier id="BookId" opf:scheme="ISBN">978-0-00-000000-0</dc:identifier>
  <dc:title>Broken Book</dc:title>
  <dc:creator opf:role="aut" opf:file-as="Doe, Jane">Jane Doe</dc:creator>
  <dc:language>en-US</dc:language>
  <dc:description>&lt;p&gt;HTML in description&lt;/p&gt;</dc:description>
  <meta name="cover" content="coverimg"/>
 </metadata>
 <manifest>
  <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
  <item id="c1" href="chapters/c1.html" media-type="application/xhtml+xml"/>
  <item id="c2" href="chapters/c2.html" media-type="text/html"/>
  <item id="coverimg" href="images/pic.jpg" media-type="image/jpeg"/>
 </manifest>
 <spine toc="ncx"><itemref idref="c1"/><itemref idref="c2"/></spine>
 <guide>
  <reference type="cover" href="chapters/c1.html" title="Cover"/>
  <reference type="toc" href="chapters/c2.html" title="TOC"/>
 </guide>
</package>""")
    wr(z, "OPS/toc.ncx", """<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
 <head><meta name="dtb:uid" content="978-0-00-000000-0"/></head>
 <docTitle><text>Broken Book</text></docTitle>
 <navMap>
  <navPoint id="n2" playOrder="002"><navLabel><text>  Second  </text></navLabel>
   <content src="chapters/c2.html"/></navPoint>
  <navPoint id="n1" playOrder="1"><navLabel><text>First</text></navLabel>
   <content src="chapters/c1.html"/>
   <navPoint id="n1a" playOrder="1"><navLabel><text>First.A</text></navLabel>
    <content src="chapters/c1.html#top"/></navPoint>
  </navPoint>
  <navPoint id="n3"><navLabel><text>No playOrder</text></navLabel>
   <content src="chapters/MISSING.html"/></navPoint>
 </navMap>
 <pageList><pageTarget type="normal" value="1"><navLabel><text>1</text></navLabel>
   <content src="chapters/c1.html"/></pageTarget></pageList>
</ncx>""")
    wr(z, "OPS/chapters/c2.html",
       '<!DOCTYPE html><html><head><title>C2</title></head><body>'
       '<p>fine</p><p>tag soup <i>italic</p></body></html>')
    wr(z, "OPS/images/PIC.JPG", b"\xff\xd8\xff\xe0" + b"\x00" * 20)  # case differs!

print("\nwrote:")
for p in (pathA, pathB):
    print("  ", p, os.path.getsize(p), "bytes")
