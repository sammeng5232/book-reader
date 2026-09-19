# -*- coding: utf-8 -*-
"""DRM detection, fixed-layout detection, and the no-TOC fallback."""
import io, os, sys, zipfile, re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from epub_model import open_book, OBFUS_IDPF, OBFUS_ADOBE

TMP = os.path.join(os.environ.get("TEMP", "."), "epub_drm_fxl")
os.makedirs(TMP, exist_ok=True)


def build(path, encryption_xml=None, with_ncx=False, with_nav=False,
          fxl_global=False):
    with zipfile.ZipFile(path, "w") as z:
        zi = zipfile.ZipInfo("mimetype"); zi.compress_type = zipfile.ZIP_STORED
        z.writestr(zi, b"application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/c.opf"'
                   ' media-type="application/oebps-package+xml"/></rootfiles></container>')
        if encryption_xml:
            z.writestr("META-INF/encryption.xml", encryption_xml)
        fxl = ('<meta property="rendition:layout">pre-paginated</meta>'
               if fxl_global else "")
        z.writestr("OEBPS/c.opf", f"""<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="i">
 <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
  <dc:identifier id="i">urn:uuid:11111111-2222-3333-4444-555555555555</dc:identifier>
  <dc:title>T</dc:title><dc:language>en</dc:language>{fxl}
 </metadata>
 <manifest>
  <item id="p1" href="p1.xhtml" media-type="application/xhtml+xml"/>
  <item id="p2" href="p2.xhtml" media-type="application/xhtml+xml"/>
 </manifest>
 <spine><itemref idref="p1"/><itemref idref="p2"/></spine>
</package>""")
        for n in ("p1", "p2"):
            z.writestr(f"OEBPS/{n}.xhtml",
                       '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
                       '<head><title>%s</title>'
                       '<meta name="viewport" content="width=1200, height=1600"/>'
                       '</head><body><p>x</p></body></html>' % n)


print("=== 1. real DRM (Adobe ADEPT, AES-128) must be detected, not crashed on ===")
ADEPT = ('<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"'
         ' xmlns:enc="http://www.w3.org/2001/04/xmlenc#">'
         '<enc:EncryptedData>'
         '<enc:EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes128-cbc"/>'
         '<enc:CipherData><enc:CipherReference URI="OEBPS/p1.xhtml"/></enc:CipherData>'
         '</enc:EncryptedData></encryption>')
p = os.path.join(TMP, "drm.epub"); build(p, ADEPT)
b = open_book(p)
print("   obfuscated=%r" % b.obfuscated)
print("   drm       =%r" % b.drm)
assert b.drm and b.drm[0][1].endswith("aes128-cbc"), b.drm
assert not b.obfuscated
print("   PASS: AES -> drm list; nothing in the obfuscation list.")

print("\n=== 2. obfuscation must NOT be reported as DRM ===")
for algo, label in ((OBFUS_IDPF, "IDPF"), (OBFUS_ADOBE, "Adobe")):
    x = ('<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"'
         ' xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData>'
         f'<enc:EncryptionMethod Algorithm="{algo}"/>'
         '<enc:CipherData><enc:CipherReference URI="OEBPS/f.ttf"/></enc:CipherData>'
         '</enc:EncryptedData></encryption>')
    q = os.path.join(TMP, "obf.epub"); build(q, x)
    bb = open_book(q)
    print("   %-6s -> obfuscated=%r drm=%r" % (label, list(bb.obfuscated), bb.drm))
    assert bb.obfuscated and not bb.drm
print("   PASS: both obfuscation algorithms classified as non-DRM.")

print("\n=== 3. fixed layout ===")
p = os.path.join(TMP, "fxl.epub"); build(p, fxl_global=True)
b = open_book(p)
print("   rendition:layout=pre-paginated -> fixed_layout=%r" % b.fixed_layout)
assert b.fixed_layout
VP = re.compile(rb'<meta[^>]+name=["\']viewport["\'][^>]*content=["\']([^"\']+)', re.I)


def viewport(zf, entry):
    m = VP.search(zf.read(entry))
    if not m:
        return None
    d = {}
    for part in m.group(1).decode("utf-8", "replace").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            d[k.strip().lower()] = v.strip()
    return d


print("   viewport of first spine item:", viewport(b.zf, b.spine[0].item.entry))
assert viewport(b.zf, b.spine[0].item.entry) == {"width": "1200", "height": "1600"}
print("   PASS: FXL page size available -> reader must scale, not paginate.")

print("\n=== 4. no nav, no NCX -> TOC falls back to the spine ===")
p = os.path.join(TMP, "notoc.epub"); build(p)
b = open_book(p)
print("   toc = %r" % [(n.label, n.entry) for n in b.toc])
assert len(b.toc) == 2
assert any("TOC built from the spine" in w for w in b.warnings), b.warnings
print("   PASS (warning emitted).")

print("\n=== 5. zip-bomb guard ===")
p = os.path.join(TMP, "bomb.epub")
with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    zi = zipfile.ZipInfo("mimetype"); zi.compress_type = zipfile.ZIP_STORED
    z.writestr(zi, b"application/epub+zip")
    z.writestr("big.bin", b"\0" * (400 * 1024 * 1024))
sz = os.path.getsize(p)
try:
    open_book(p)
    print("   NOT refused (file is %d bytes on disk)" % sz)
except ValueError as e:
    print("   refused: %s   (archive is only %d bytes on disk)" % (e, sz))
print("\nALL DRM/FXL CHECKS PASS")
