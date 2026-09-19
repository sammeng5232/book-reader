# -*- coding: utf-8 -*-
"""Backslash + filename-encoding behaviour of zipfile, measured on this box."""
import io, os, sys, zipfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from epub_resolve import ZipIndex, resolve_href

print("platform os.sep =", repr(os.sep), " os.altsep =", repr(os.altsep))

# ------------------------------------------------------------------ 1. write
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("A\\B\\c.xhtml", b"x")
print("\n1) writestr('A\\\\B\\\\c.xhtml') -> namelist %r"
      % zipfile.ZipFile(buf).namelist())
print("   zipfile._sanitize_filename() rewrites os.sep -> '/' in ZipInfo.__init__")

# ------------------------------------------------------------------ 2. read
raw = bytes(bytearray(buf.getvalue())).replace(b"A/B/c.xhtml", b"A\\B\\c.xhtml")
assert b"A\\B\\c.xhtml" in raw
zf = zipfile.ZipFile(io.BytesIO(raw))
print("\n2) archive bytes really contain 'A\\\\B\\\\c.xhtml'; namelist() = %r"
      % zf.namelist())
print("   _RealGetContents builds ZipInfo(filename) (Lib/zipfile/__init__.py:1583),")
print("   so the SAME sanitiser runs on read.")
print("   => ON WINDOWS zipfile hands you forward slashes either way.")
print("   => ON LINUX/macOS os.sep=='/' so the replace is skipped and a")
print("      backslash entry name SURVIVES into namelist().")

# --------------------------------------------- 3. the case that actually bites
print("\n3) The real-world hazard is a backslash in the HREF, not the entry:")
buf2 = io.BytesIO()
with zipfile.ZipFile(buf2, "w") as z:
    z.writestr("OEBPS/content.opf", b"x")
    z.writestr("OEBPS/images/pic.jpg", b"x")
zf2 = zipfile.ZipFile(buf2)
ix = ZipIndex(zf2)
for href in ("images\\pic.jpg", "..\\OEBPS\\images\\pic.jpg", "images/pic.jpg"):
    got = resolve_href(ix, "OEBPS", href)[0]
    print("   href=%-28r -> %r" % (href, got))
    assert got == "OEBPS/images/pic.jpg", (href, got)
print("   PASS: the resolver's backslash->slash step handles all three.")

# ------------------------------------------- 4. non-UTF8 filenames (mojibake)
print("\n4) Filename ENCODING: if the UTF-8 flag bit (0x800) is clear, zipfile")
print("   decodes names with cp437 -- mojibake for CJK names zipped by old tools.")
buf3 = io.BytesIO()
name = "目录/第一章.xhtml"          # 目录/第一章.xhtml
gbk = name.encode("gbk")
with zipfile.ZipFile(buf3, "w") as z:
    z.writestr("placeholder.xhtml", b"x")
b3 = bytearray(buf3.getvalue())
b3 = bytes(b3).replace(b"placeholder.xhtml", gbk + b"." * (17 - len(gbk)))
try:
    zf3 = zipfile.ZipFile(io.BytesIO(b3))
    print("   default decode      :", repr(zf3.namelist()[0]))
except Exception as e:
    print("   default decode raised:", e)
try:
    zf4 = zipfile.ZipFile(io.BytesIO(b3), metadata_encoding="gbk")
    print("   metadata_encoding=gbk:", repr(zf4.namelist()[0]))
    print("   -> ZipFile(path, metadata_encoding=...) is the stdlib escape hatch")
except Exception as e:
    print("   metadata_encoding=gbk raised:", e)

print("\nDONE")
