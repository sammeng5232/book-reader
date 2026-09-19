# Research: test-corpus

## Summary

I built the full test corpus and harness in C:\Users\mengz\epub-reader\tests\ and verified it by running it, not by inspection. tests\make_fixtures.py (stdlib only) deterministically generates 16 EPUB fixtures totalling 266 KB, including hand-built, genuinely valid binary assets: a TrueType font, a WOFF wrapping it, a baseline JPEG and a PNG — all constructed byte-by-byte from struct+zlib, with no third-party libraries. tests\test_epub_parsing.py is a 156-test stdlib unittest suite written against the agreed epublib interface, imported lazily so every test currently FAILS with an explanatory message rather than erroring at import. To prove the suite is satisfiable and its expected values are correct, I wrote a throwaway reference implementation of epublib in my scratchpad (NOT in the project) — the suite goes 156/156 green against it. To prove the suite is not vacuous, I ran 14 targeted mutations against that reference implementation; all 14 were caught, zero survivors. tests\run_tests.ps1 builds fixtures on demand, runs discovery with the right interpreter and PYTHONPATH, and prints a pass/fail summary (verified in both FAIL and PASS states). tests\smoke_gui.py locates the future app, opens a fixture, waits for QWebEngineView load, screenshots with PySide6 only, and flags blank output; I proved the happy path by pointing it at a scratch stand-in app that serves zip entries through a custom URL scheme — all 8 smoke fixtures rendered and screenshotted correctly.


## Verified facts

1. All 16 fixtures generate and pass structural verification: `make_fixtures.py --clean --verify` prints 'all structural checks passed'. It checks mimetype is entry 0 and ZIP_STORED, container.xml parses and its rootfile exists, the OPF is well-formed XML, big_book contains ~2 MB of xhtml, and every builder produces byte-identical output on a rebuild.

2. Generation is byte-for-byte deterministic. Proven by SHA-256 hashing all 16 files, regenerating with --clean, and re-hashing: identical. Also asserted inside the suite (test_generation_is_deterministic).

3. The hand-built TrueType font is genuinely valid: Qt accepts it (QRawFont(...).isValid() is True, familyName 'EpubReaderTest', QFontDatabase.addApplicationFontFromData returns id 0). It initially failed because the sfnt TableRecord layout is tag, checkSum, offset, length — I had offset and checksum swapped — and because cmap format 4 idDelta maps a range onto CONSECUTIVE glyph ids, so 'A' resolved to glyph 34 in a 3-glyph font; that segment needs idRangeOffset + glyphIdArray.

4. The TTF and the hand-built WOFF both survive Chromium's OpenType Sanitizer. Proven by rendering both via @font-face in a QWebEngineView and screenshotting: every letter draws as the solid box glyph in both cases.

5. The hand-built JPEG and PNG are real images. Pillow decodes the JPEG as JPEG/(64,64)/L with the intended 8-step gradient [40,68,100,128,158,188,218,248] and the PNG as PNG/(64,64)/RGB with the intended checkerboard. Both also render in Chromium.

6. QWidget.grab() DOES capture QWebEngineView content on Qt 6.11 / PySide6 6.11.1 / Windows. Verified by grabbing a window containing a loaded QWebEngineView and reading back the PNG — text, images and fonts all present. QScreen.grabWindow(winId) and view.grab() also work; grab() is the simplest and works when the window is occluded.

7. broken_xml.epub really is malformed: xml.etree.ElementTree raises ParseError on broken1.xhtml ('mismatched tag: line 7') and broken2.xhtml ('undefined entity: line 8'), while ok.xhtml, nav.xhtml and content.opf parse cleanly. So a strict XML parser genuinely cannot read it.

8. obfuscated_fonts.epub uses the real IDPF font-obfuscation algorithm (SHA-1 of the whitespace-stripped unique identifier, XORed over the first 1040 bytes). Verified: bytes beyond offset 1040 match the plain font exactly, and re-applying the transform reproduces the plain font byte-for-byte.

9. odd_paths.epub's zip entries carry the correct names and the UTF-8 name flag (flag_bits & 0x800) for the CJK entry; entry names read back as 'text/chapter one.xhtml', '文本/第一章.xhtml', 'text/a#b.xhtml', 'Text/MiXeD.xhtml', 'images/图片 1.png'.

10. The suite is satisfiable: 156/156 pass against a scratch reference implementation of the agreed interface (run via run_tests.ps1 -ExtraPythonPath <scratch>).

11. The suite is non-vacuous: 14 mutations of the reference implementation (no case-insensitive lookup, no percent-decoding, no ../ normalisation, script/style text leaking into plain_text, contributors counted as authors, any encryption.xml treated as DRM, TOC taken from the first <nav> rather than epub:type='toc', fragment left in .href, linear ignored, fixed-layout never detected, no TOC fallback, EPUB2 meta name=cover ignored, corrupt reported as not_epub, entities not decoded) were ALL caught. Survivors: 0/14.

12. run_tests.ps1 works in both states: exit 1 with 6 passed / 150 failed and a plain-English note while epublib is absent, exit 0 with 156 passed once it is on PYTHONPATH. -Filter OddPaths correctly narrows to 17 tests. It is pure ASCII (0 bytes > 127), so Windows PowerShell 5.1 reads it correctly without a BOM.

13. smoke_gui.py exits 2 with a precise diagnostic when the app is absent, and exits 0 after screenshotting all 8 smoke fixtures against a scratch stand-in app. Screenshots confirmed by eye: the embedded WOFF, JPEG, PNG, SVG and a CSS background-image (path relative to the stylesheet) all render through a zip-backed custom URL scheme, and the percent-encoded / CJK / mixed-case paths in odd_paths.epub all resolve.

14. cjk_vertical.epub's writing-mode: vertical-rl chapter renders correctly in QtWebEngine (text flows top-to-bottom, right-to-left, CJK punctuation correctly placed), verified by screenshot — so it is a real regression target for injected theme CSS.


## Pitfalls

1. QWebEngineUrlScheme.registerScheme() must be called BEFORE QApplication is constructed / QtWebEngine initialises. Registering it later silently does nothing and the scheme resolves as an unknown/opaque scheme. smoke_gui.py therefore imports the app module before creating QApplication, and calls a module-level register_url_schemes() / register_schemes() hook if one exists. Put the registration at module import time or in such a hook, not inside main() after the app object exists.

2. Do NOT call scheme.setDefaultPort(QWebEngineUrlScheme.SpecialPort.PortUnspecified) in PySide6 — SpecialPort is not an IntEnum, so both the enum member and int(member) raise TypeError. The default is already PortUnspecified; omit the call.

3. A custom scheme must declare LocalAccessAllowed and CorsEnabled (plus SecureScheme) or Chromium blocks the stylesheet, font and XHR subresource loads from inside the book with no obvious error.

4. In QWebEngineUrlSchemeHandler.requestStarted, the QBuffer holding the reply must outlive the call — parent it to the job (QBuffer(job)). A locally-scoped buffer gets collected and the request fails or returns empty.

5. Use url.path(QUrl.ComponentFormattingOption.FullyDecoded) to get the zip entry name. QUrl.path() percent-decodes by default, which is what you want, but a filename containing a literal '#' (odd_paths.epub has one) only survives if the href was written %23 — splitting the fragment BEFORE unquoting is mandatory, otherwise 'text/a%23b.xhtml' turns into path 'text/a' + fragment 'b.xhtml'.

6. Percent-decode and then normalise ../ — in that order, and normalise with posixpath.normpath on the joined path, not by string munging. odd_paths.epub has a manifest href 'text/../style.css' at the zip root plus '../text/./chapter%20one.xhtml#top' inside a chapter.

7. Real books use zip entry names whose case does not match the OPF href. Build a lowercased name index once at open time and fall back to it; without that, odd_paths.epub loses a whole chapter. A naive implementation also fails to read anything at all for 3 other tests.

8. @font-face and background-image URLs in CSS resolve relative to the STYLESHEET, not to the XHTML that links it. images_fonts.epub is built specifically to catch this (OEBPS/styles/main.css referencing ../fonts/ and ../images/). If you resolve relative to the document, the font silently falls back and nobody notices.

9. EPUB 3 nav documents contain several <nav> elements. The TOC is the one with epub:type="toc"; landmarks and page-list must be ignored. Grabbing the first <nav> or every <ol> produces a TOC containing 'Cover', 'Start of Content', '1', '2' — epub3_nav.epub catches this.

10. META-INF/encryption.xml does NOT mean DRM. The IDPF font-obfuscation algorithm (http://www.idpf.org/2008/embedding) and Adobe's font mangling (http://ns.adobe.com/pdf/enc#RC) use the same file and appear in a large share of legitimate commercial EPUBs. Only treat an EncryptionMethod Algorithm outside that allowlist as DRM. obfuscated_fonts.epub exists solely to catch this false positive.

11. dc:contributor is not an author. Both epub2_ncx.epub and epub3_nav.epub carry a contributor that must stay out of metadata['authors'].

12. plain_text() must drop <script>, <style>, <head>/<title> and comments, or their contents pollute search results. epub3_nav.epub's intro chapter contains SCRIPT-MUST-NOT-BE-SEARCHABLE, STYLE-MUST-NOT-BE-SEARCHABLE and COMMENT-MUST-NOT-BE-SEARCHABLE sentinels for exactly this.

13. unittest discovery fails with 'Start directory is not importable' if you pass -t <project root> with -s tests and tests/ has no __init__.py. Use -s tests with the default top-level dir and put the project root on PYTHONPATH instead (run_tests.ps1 does this).

14. Opening big_book.epub must not eagerly parse all 150 chapters — the suite budgets 5 s for open() and 25 s for a full plain_text scan of all 150. Parse the OPF and TOC at open, read chapter bodies lazily.

15. An .epub that is a valid zip but has no container.xml is 'not_epub', not 'corrupt'; a truncated/garbage/zero-byte file is 'corrupt'. The suite pins these kinds exactly, and one mutation showed that conflating them fails 3 tests.


## Recommendations

1. Run everything with: powershell -ExecutionPolicy Bypass -File C:\Users\mengz\epub-reader\tests\run_tests.ps1 . It builds fixtures if missing, uses the 3.14 interpreter, sets PYTHONPATH to the project root, and exits 0/1/2. Add -Regenerate to rebuild fixtures from scratch, -Detailed for per-test output, -Filter <substring> to narrow (e.g. -Filter OddPaths), -ExtraPythonPath <dir> to test an epublib that is not yet in the project root.

2. Put epublib.py directly in C:\Users\mengz\epub-reader\ (project root) exporting EpubBook and EpubError. The 150 currently-failing tests will start passing incrementally; the 6 that pass today are the fixture-corpus canaries and must stay green.

3. Honour the four contract decisions the suite pins down, which the original brief left ambiguous: (1) zip_name is the exact archive entry name, already percent-decoded and ../-normalised, and is what read() takes; (2) resolve() returns (zip_name, fragment) with fragment=None when absent and never containing '#'; (3) metadata['authors'] is dc:creator only; (4) TocEntry.href is the raw href as written in the TOC document with the fragment split into .fragment, so it is still percent-encoded and still relative TO THE TOC DOCUMENT (for a synthesised fallback TOC there is no TOC document, so hrefs are the manifest hrefs, relative to the OPF).

4. Implement EpubError as `class EpubError(Exception)` with `__init__(self, message, kind=...)` setting self.kind. The suite requires kinds 'drm', 'corrupt' and 'not_epub' exactly; other kinds (e.g. 'missing' for an unknown entry) are free.

5. For DRM detection, allowlist the non-DRM algorithms rather than blocklisting DRM ones: OBFUSCATION_ALGORITHMS = {'http://www.idpf.org/2008/embedding', 'http://ns.adobe.com/pdf/enc#RC'}. Raise EpubError(kind='drm') only if encryption.xml names an EncryptionMethod outside that set.

6. Use html.parser.HTMLParser (stdlib, lenient) with convert_charrefs=True for plain_text() and for the nav document, not xml.etree. It recovers from every defect in broken_xml.epub (unclosed tags, raw &, unquoted attributes, undefined entities) and decodes &nbsp;/&mdash; for free. Reserve ElementTree for container.xml, the OPF and the NCX, which are XML by spec.

7. Run the GUI smoke test as soon as a window class exists: python314 tests\smoke_gui.py --all . Name the window class one of MainWindow / ReaderWindow / EpubReaderWindow / EpubReader / ReaderMainWindow / Window in a module named main / app / reader / epub_reader, or pass --entry module:Class. Give the window either a constructor taking the epub path or an open_book(path) method. Screenshots land in tests\out\smoke_<fixture>.png and the script flags a blank grab (fewer than 3 distinct colours) with exit code 3.

8. Expose a module-level register_url_schemes() in the app module that calls QWebEngineUrlScheme.registerScheme() and is invoked at import time. smoke_gui.py calls it before constructing QApplication; the real main() can call it too (it is idempotent in my stand-in).

9. When you touch the injected theme/pagination CSS, re-run the smoke test and look at smoke_cjk_vertical.png. The vertical-rl chapter is the canary: if your injected stylesheet sets writing-mode, line-height or height unconditionally, that page flattens to horizontal and it is instantly visible.

10. Keep tests\fixtures\ and tests\out\ out of git (tests\.gitignore already does this). make_fixtures.py is the single source of truth and is deterministic, so fixtures can always be rebuilt identically.

11. Do not hand-edit the fixture .epub files. Change make_fixtures.py and regenerate; test_generation_is_deterministic will fail if on-disk bytes drift from a fresh build.


## Open questions

1. I decided that an archive with a correct mimetype but NO META-INF/container.xml raises EpubError(kind='not_epub') rather than recovering by scanning the zip for a .opf. OCF requires container.xml, so this is defensible, but some readers do recover. If the implementer prefers recovery, change TestErrorHandling.test_missing_container_xml_is_not_an_epub and the no_container.epub fixture together.

2. TocEntry.href for a SYNTHESISED fallback TOC (no NCX, no nav) has no TOC document to be relative to; I specified it is the manifest href, i.e. relative to the OPF. If the implementer would rather make href the resolved zip_name in that case, TestNoTocFallback.test_toc_hrefs_match_spine needs adjusting.

3. The label for a chapter with an empty <title> (no_toc.epub chapter5) is only asserted to be non-empty, since 'Epsilon Chapter' from the <h1> and 'chapter5' from the filename are both reasonable. Tighten it if the team wants one specific rule.

4. The agreed interface has no hook for de-obfuscating IDPF-obfuscated fonts, so obfuscated_fonts.epub currently only asserts 'not DRM' and that the text is readable. If the reader should actually de-obfuscate so the font renders, that needs an extra API (e.g. read() transparently de-obfuscating entries listed in encryption.xml) plus a test; the working algorithm is in the code snippets.

5. The performance budgets (5 s to open a 150-chapter book, 25 s to plain_text-scan all of it) are generous headroom on this machine — the reference implementation does both in well under a second. Tighten them once the real implementation's numbers are known.

6. smoke_gui.py guesses the app's entry point from a list of module and class names. If the app ends up structured differently (e.g. a package with the window behind a factory that starts its own event loop), either add the name to MODULE_CANDIDATES/WINDOW_CLASS_NAMES or always invoke it with --entry module:Class.


## Verified code snippets


### Register the custom scheme BEFORE QApplication exists. This is the single most common way to lose an evening on this architecture. Note the omitted setDefaultPort and the three required flags.

```python
from PySide6.QtWebEngineCore import QWebEngineUrlScheme

SCHEME = b"epub"
_registered = False

def register_url_schemes():
    """MUST run before QApplication / QtWebEngine initialisation."""
    global _registered
    if _registered:
        return
    scheme = QWebEngineUrlScheme(SCHEME)
    scheme.setSyntax(QWebEngineUrlScheme.Syntax.Host)
    # Do NOT call setDefaultPort(QWebEngineUrlScheme.SpecialPort.PortUnspecified):
    # SpecialPort is not an IntEnum, both the member and int(member) raise
    # TypeError in PySide6. PortUnspecified is already the default.
    scheme.setFlags(
        QWebEngineUrlScheme.Flag.SecureScheme
        | QWebEngineUrlScheme.Flag.LocalAccessAllowed
        | QWebEngineUrlScheme.Flag.CorsEnabled
    )
    QWebEngineUrlScheme.registerScheme(scheme)
    _registered = True

register_url_schemes()   # at import time
```

### Serve zip entries straight out of the book. Verified rendering all 8 smoke fixtures including the embedded WOFF, a CSS background-image, and percent-encoded/CJK/mixed-case entry names. The QBuffer parenting is load-bearing.

```python
import mimetypes, posixpath
from PySide6.QtCore import QBuffer, QByteArray, QUrl
from PySide6.QtWebEngineCore import QWebEngineUrlRequestJob, QWebEngineUrlSchemeHandler

MIME_OVERRIDES = {
    ".xhtml": "application/xhtml+xml", ".ncx": "application/x-dtbncx+xml",
    ".opf": "application/oebps-package+xml", ".otf": "font/otf",
    ".ttf": "font/ttf", ".woff": "font/woff", ".woff2": "font/woff2",
    ".svg": "image/svg+xml",
}

def guess_mime(zip_name):
    ext = posixpath.splitext(zip_name)[1].lower()
    if ext in MIME_OVERRIDES:
        return MIME_OVERRIDES[ext].encode("ascii")
    return (mimetypes.guess_type(zip_name)[0] or "application/octet-stream").encode("ascii")

class ZipSchemeHandler(QWebEngineUrlSchemeHandler):
    def __init__(self, book, parent=None):
        super().__init__(parent)
        self.book = book

    def requestStarted(self, job: QWebEngineUrlRequestJob) -> None:
        url = job.requestUrl()
        zip_name = url.path(QUrl.ComponentFormattingOption.FullyDecoded).lstrip("/")
        try:
            data = self.book.read(zip_name)
        except Exception:
            job.fail(QWebEngineUrlRequestJob.Error.UrlNotFound)
            return
        buf = QBuffer(job)          # parent to the job so it outlives the reply
        buf.setData(QByteArray(data))
        buf.open(QBuffer.OpenModeFlag.ReadOnly)
        job.reply(QByteArray(guess_mime(zip_name)), buf)

def book_url(zip_name, fragment=None):
    url = QUrl()
    url.setScheme("epub")
    url.setHost("book")
    url.setPath("/" + zip_name)      # setPath percent-encodes for us
    if fragment:
        url.setFragment(fragment)
    return url
```

### The href resolution algorithm the suite pins down. Fragment is split BEFORE percent-decoding (so a literal '#' written as %23 survives), then ../ is normalised, then a lowercased index provides the case-insensitive fallback.

```python
import posixpath
from urllib.parse import unquote

# built once at open(): {name.lower(): actual_name} over zf.namelist()
# self._lower = {}
# for n in self._names: self._lower.setdefault(n.lower(), n)

def resolve(self, href, base_zip_name):
    href = href or ""
    path, sep, frag = href.partition("#")      # split fragment FIRST
    fragment = unquote(frag) if sep and frag else None
    path = unquote(path)                        # then percent-decode
    if not path:
        target = base_zip_name                  # bare "#anchor"
    else:
        base_dir = posixpath.dirname(base_zip_name or "")
        target = posixpath.normpath(posixpath.join(base_dir, path))
        target = target.lstrip("/")
    real = self._names_exact.get(target) or self._lower.get(target.lower())
    return (real if real is not None else target), fragment
```

### Screenshotting a QWebEngineView with PySide6 only. QWidget.grab() captures web content on Qt 6.11/Windows — verified, no OS capture APIs needed.

```python
pixmap = window.grab()            # captures QWebEngineView content too
image  = pixmap.toImage()
pixmap.save(str(out_path), "PNG")

# cheap blank-detector: count distinct colours on a sampled grid
colours = set()
for y in range(0, image.height(), max(1, image.height() // 60)):
    for x in range(0, image.width(), max(1, image.width() // 60)):
        colours.add(image.pixel(x, y) & 0xFFFFFF)
if len(colours) < 3:
    raise SystemExit("blank render")
```

### IDPF font de-obfuscation, if you choose to support obfuscated_fonts.epub properly rather than just not misreporting it as DRM. Verified to round-trip exactly against the fixture.

```python
import hashlib

def idpf_deobfuscate(font_bytes, unique_identifier):
    """XOR the first 1040 bytes with the repeated SHA-1 of the
    whitespace-stripped unique identifier. Self-inverse."""
    key = hashlib.sha1("".join(unique_identifier.split()).encode("utf-8")).digest()
    out = bytearray(font_bytes)
    for i in range(min(1040, len(out))):
        out[i] ^= key[i % 20]
    return bytes(out)
```

## Files written

- `C:\Users\mengz\epub-reader\tests\make_fixtures.py`

- `C:\Users\mengz\epub-reader\tests\_assets.py`

- `C:\Users\mengz\epub-reader\tests\test_epub_parsing.py`

- `C:\Users\mengz\epub-reader\tests\run_tests.ps1`

- `C:\Users\mengz\epub-reader\tests\smoke_gui.py`

- `C:\Users\mengz\epub-reader\tests\.gitignore`

- `C:\Users\mengz\epub-reader\tests\fixtures\epub2_ncx.epub`

- `C:\Users\mengz\epub-reader\tests\fixtures\epub3_nav.epub`

- `C:\Users\mengz\epub-reader\tests\fixtures\cjk_vertical.epub`

- `C:\Users\mengz\epub-reader\tests\fixtures\images_fonts.epub`

- `C:\Users\mengz\epub-reader\tests\fixtures\odd_paths.epub`

- `C:\Users\mengz\epub-reader\tests\fixtures\no_toc.epub`

- `C:\Users\mengz\epub-reader\tests\fixtures\fixed_layout.epub`

- `C:\Users\mengz\epub-reader\tests\fixtures\big_book.epub`

- `C:\Users\mengz\epub-reader\tests\fixtures\broken_xml.epub`

- `C:\Users\mengz\epub-reader\tests\fixtures\drm_fake.epub`

- `C:\Users\mengz\epub-reader\tests\fixtures\obfuscated_fonts.epub`

- `C:\Users\mengz\epub-reader\tests\fixtures\not_an_epub.epub`

- `C:\Users\mengz\epub-reader\tests\fixtures\no_container.epub`

- `C:\Users\mengz\epub-reader\tests\fixtures\truncated.epub`

- `C:\Users\mengz\epub-reader\tests\fixtures\not_a_zip.epub`

- `C:\Users\mengz\epub-reader\tests\fixtures\empty.epub`

- `C:\Users\mengz\epub-reader\tests\out\smoke_epub2_ncx.png`

- `C:\Users\mengz\epub-reader\tests\out\smoke_epub3_nav.png`

- `C:\Users\mengz\epub-reader\tests\out\smoke_cjk_vertical.png`

- `C:\Users\mengz\epub-reader\tests\out\smoke_images_fonts.png`

- `C:\Users\mengz\epub-reader\tests\out\smoke_odd_paths.png`

- `C:\Users\mengz\epub-reader\tests\out\smoke_fixed_layout.png`

- `C:\Users\mengz\epub-reader\tests\out\smoke_big_book.png`

- `C:\Users\mengz\epub-reader\tests\out\smoke_broken_xml.png`
