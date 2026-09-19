"""Generate a multi-file EPUB-like zip for the rendering-core prototype.

Deliberately uses a deep, EPUB-realistic directory layout so that every
reference in the XHTML is RELATIVE and has to be resolved by the browser
against the document URL (and therefore round-trips through our custom
URL scheme handler).

    mimetype
    META-INF/container.xml
    OEBPS/content.opf
    OEBPS/toc.ncx
    OEBPS/text/ch1.xhtml        <- references ../styles/main.css, ../images/*, ../fonts/*
    OEBPS/text/ch2.xhtml        <- relative link target, own relative css
    OEBPS/styles/main.css       <- @font-face url(../fonts/proto.ttf)  (relative *from the CSS*)
    OEBPS/styles/ch2.css
    OEBPS/images/pic.png
    OEBPS/images/photo.jpg
    OEBPS/fonts/proto.ttf
"""

import io
import os
import zipfile

from PIL import Image

SYSTEM_FONT = r"C:\Windows\Fonts\MiriamMonoCLM-Book.ttf"

CONTAINER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

CONTENT_OPF = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">urn:uuid:proto-0001</dc:identifier>
    <dc:title>Proto Book</dc:title>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="ch1" href="text/ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="ch2" href="text/ch2.xhtml" media-type="application/xhtml+xml"/>
    <item id="css" href="styles/main.css" media-type="text/css"/>
    <item id="css2" href="styles/ch2.css" media-type="text/css"/>
    <item id="png" href="images/pic.png" media-type="image/png"/>
    <item id="jpg" href="images/photo.jpg" media-type="image/jpeg"/>
    <item id="fnt" href="fonts/proto.ttf" media-type="font/ttf"/>
  </manifest>
  <spine>
    <itemref idref="ch1"/>
    <itemref idref="ch2"/>
  </spine>
</package>
"""

MAIN_CSS = """@font-face {
  font-family: "ProtoFont";
  src: url("../fonts/proto.ttf") format("truetype");
  font-weight: normal;
  font-style: normal;
}
body {
  /* rgb(17, 34, 51) -- a value nothing else in the system would produce */
  background-color: rgb(17, 34, 51);
  color: rgb(238, 238, 238);
  margin: 24px;
}
#css-probe {
  /* rgb(1, 2, 3) proves main.css (../styles/main.css) was fetched + applied */
  color: rgb(1, 2, 3);
  letter-spacing: 3px;
}
.proto-font   { font-family: "ProtoFont", serif; font-size: 32px; }
.no-such-font { font-family: "__DefinitelyNotInstalled__", serif; font-size: 32px; }
#theme-probe  { color: rgb(9, 9, 9); }
"""

CH2_CSS = """#ch2-probe { color: rgb(4, 5, 6); }
"""

CH1_XHTML = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en" lang="en">
<head>
  <meta charset="utf-8"/>
  <title>Proto Chapter One</title>
  <link rel="stylesheet" type="text/css" href="../styles/main.css"/>
</head>
<body>
  <h1 id="css-probe">Chapter One</h1>
  <p id="theme-probe">Theme probe paragraph.</p>
  <p><img id="png" src="../images/pic.png" alt="png"/></p>
  <p><img id="jpg" src="../images/photo.jpg" alt="jpg"/></p>
  <p><span id="fspan" class="proto-font">HHHHHHHHHH</span></p>
  <p><span id="nspan" class="no-such-font">HHHHHHHHHH</span></p>
  <p><a id="next" href="ch2.xhtml">Go to chapter two</a></p>
  <script type="text/javascript">window.__inlineRan = true;</script>
</body>
</html>
"""

CH2_XHTML = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en" lang="en">
<head>
  <meta charset="utf-8"/>
  <title>Proto Chapter Two</title>
  <link rel="stylesheet" type="text/css" href="../styles/ch2.css"/>
</head>
<body>
  <h1 id="ch2-probe">Chapter Two</h1>
  <p><a id="prev" href="ch1.xhtml">Back</a></p>
  <script type="text/javascript">window.__inlineRan = true;</script>
</body>
</html>
"""

TOC_NCX = """<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="urn:uuid:proto-0001"/></head>
  <docTitle><text>Proto Book</text></docTitle>
  <navMap>
    <navPoint id="n1" playOrder="1"><navLabel><text>Chapter One</text></navLabel>
      <content src="text/ch1.xhtml"/></navPoint>
    <navPoint id="n2" playOrder="2"><navLabel><text>Chapter Two</text></navLabel>
      <content src="text/ch2.xhtml"/></navPoint>
  </navMap>
</ncx>
"""


def _png_bytes(w=120, h=60):
    img = Image.new("RGB", (w, h), (200, 30, 90))
    for x in range(w):
        for y in range(0, h, 7):
            img.putpixel((x, y), (255, 255, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _jpeg_bytes(w=90, h=45):
    img = Image.new("RGB", (w, h), (30, 140, 200))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


# Every path the browser must fetch through the scheme handler for ch1.
CH1_REQUIRED = [
    "OEBPS/text/ch1.xhtml",
    "OEBPS/styles/main.css",
    "OEBPS/images/pic.png",
    "OEBPS/images/photo.jpg",
    "OEBPS/fonts/proto.ttf",
]

PNG_W, PNG_H = 120, 60
JPG_W, JPG_H = 90, 45


def build(path: str) -> str:
    with open(SYSTEM_FONT, "rb") as fh:
        font = fh.read()

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        # EPUB requires 'mimetype' first and STORED.
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                   compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", CONTAINER_XML)
        z.writestr("OEBPS/content.opf", CONTENT_OPF)
        z.writestr("OEBPS/toc.ncx", TOC_NCX)
        z.writestr("OEBPS/text/ch1.xhtml", CH1_XHTML)
        z.writestr("OEBPS/text/ch2.xhtml", CH2_XHTML)
        z.writestr("OEBPS/styles/main.css", MAIN_CSS)
        z.writestr("OEBPS/styles/ch2.css", CH2_CSS)
        z.writestr("OEBPS/images/pic.png", _png_bytes(PNG_W, PNG_H))
        z.writestr("OEBPS/images/photo.jpg", _jpeg_bytes(JPG_W, JPG_H))
        z.writestr("OEBPS/fonts/proto.ttf", font)
    return path


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "proto_book.epub"
    build(out)
    with zipfile.ZipFile(out) as z:
        for n in z.namelist():
            print(f"{z.getinfo(n).file_size:>9}  {n}")
