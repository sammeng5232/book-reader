# Research: epub-spec

## Summary

I found 3 unique .epub files on this machine (4 paths, one duplicated) — all under C:\Users\mengz\Desktop\文件. All three are EPUB 2.0 with NCX-only TOCs, no EPUB 3 nav document, none DRM'd, none fixed-layout, all ltr. Because the real-file sample has no EPUB3/nav/rtl/FXL/DRM variety, I built two adversarial fixtures plus five synthetic books to cover everything the spec allows, and verified every rule by running code. I wrote a complete stdlib-only reference parser (epub_model.py) and resolver (epub_resolve.py) in C:\Users\mengz\epub-reader\research\; all three real books parse with zero warnings and zero unresolvable in-document hrefs, and the full test suite is green. Three findings are load-bearing and non-obvious. (1) One of the user's own books ("50人的二十年") carries META-INF/encryption.xml — but it is Adobe *font obfuscation*, not DRM; treating encryption.xml as DRM would refuse a book the user owns. I verified de-obfuscation produces a valid TTF. (2) Another of the user's books has NCX playOrder values in HEXADECIMAL ("1D", "2A", "2F") — a naive int(playOrder) raises on a real file, and sorting by the values that do parse scrambles the TOC; document order must be authoritative. (3) On Windows, os.path.normpath("OEBPS/text/../a.xhtml") returns "OEBPS\\a.xhtml", which is never a valid zip entry — the resolver must use posixpath exclusively. I also proved zipfile decodes non-UTF-8 entry names as cp437 (mojibake for CJK filenames) and that metadata_encoding= is the stdlib fix.


## Verified facts

1. REAL FILES: exactly 3 unique .epub files exist on this machine (found via `find /c/Users/mengz/{Desktop,Downloads,Documents} -iname '*.epub'`; Downloads and Documents are empty). Paths: 'Desktop\文件\CUHK Notes\CUHK_BFRM\Fin Books\金融计量方法系列教材•数理金融学...(李向科) (Z-Library).epub' (6.4MB, 1252 entries), 'Desktop\文件\Econ Books\50人的二十年_樊纲 易纲等.epub' (151 entries, also duplicated under Econ Books_China\), 'Desktop\文件\Econ Books\Econ Books_China\从此岸到彼岸_人民币汇率如何实现清洁浮动_缪延亮.epub' (241 entries). Proven by running a stdlib zipfile+ElementTree probe over each.

2. STRUCTURAL VARIETY OF THE REAL FILES (measured, not assumed): all 3 are package/@version="2.0"; all 3 have an NCX and ZERO have an EPUB3 nav document (no manifest item carries properties="nav"); all 3 use `<meta name="cover" content="ID"/>` for the cover and none uses properties="cover-image"; none has page-progression-direction; none has any rendition:* metadata (no fixed layout); all 3 have a correct STORED, extra-field-free, first-in-archive `mimetype` entry. OPF depth varies usefully: 'OEBPS/content.opf', 'ops/content.opf', and 'content.opf' AT THE CONTAINER ROOT (base_dir == '', an easy case to get wrong).

3. DRM vs OBFUSCATION — the biggest trap. 'Desktop\文件\Econ Books\50人的二十年_樊纲 易纲等.epub' contains META-INF/encryption.xml with two EncryptedData entries, Algorithm="http://ns.adobe.com/pdf/enc#RC", CipherReference URI="fonts/00060.ttf" and "fonts/00059.ttf". This is Adobe FONT OBFUSCATION, not DRM. I de-obfuscated both by XOR-ing the first 1024 bytes with the 16-byte key derived from dc:identifier 'urn:uuid:4e3c703b-3148-4bc7-a91c-fb637f44b8c3' (strip 'urn:uuid:', strip hyphens, bytes.fromhex of the 32 hex chars = 4e3c703b31484bc7a91cfb637f44b8c3) and got signature b'\x00\x01\x00\x00' = valid TrueType, on both 3.9MB and 3.7MB files. The IDPF key applied to the same data yields b'\xa6R\x82\x14' (garbage), confirming the two algorithms are genuinely distinct.

4. IDPF OBFUSCATION ALGORITHM — quoted from the W3C EPUB 3.3 Recommendation, section 4.4.3/4.4.4/4.4.5 (fetched https://www.w3.org/TR/epub-33/ and extracted the normative text): key = SHA-1 of the UTF-8 unique identifier after removing 'the Unicode code points U+0020, U+0009, U+000D and U+000A' -> 20 bytes; algorithm modifies 'the first 1040 bytes (~1KB) of the font file' by XOR with the cycling key (pseudo-code: 52 outer x 20 inner); encryption.xml Algorithm attribute MUST be 'http://www.idpf.org/2008/embedding'. NOTE a WebFetch summarizer garbled this into 'http://www.idpf.org/2008/obfuscation/idpf' and 'first 1040 bytes of the unique identifier' — both WRONG; I went to the raw spec HTML to confirm. I then built a fixture with an IDPF-obfuscated font and round-tripped it back to b'\x00\x01\x00\x00'.

5. NCX playOrder IS NOT RELIABLY DECIMAL. In 'Desktop\文件\CUHK Notes\...(李向科) (Z-Library).epub' the navMap sibling playOrder values are hexadecimal: depth0 = ['00','01',...], depth1 groups = ['1D','1E','1F','20'], ['28','29','2A'], ['2B','2C','2D','2E'], ['2F','30','31']. int('1D') raises ValueError. Worse, int('20') succeeds as decimal 20 and would sort BEFORE int('1D')-as-29, actively corrupting the TOC. Measured across all 3 real books: in every sibling group where all values parsed as decimal, sorted order already equalled document order — so sorting by playOrder has zero upside and real downside.

6. WINDOWS PATH HAZARD (measured on this interpreter): os.path IS ntpath here. os.path.normpath('OEBPS/text/../a.xhtml') == 'OEBPS\\a.xhtml' and os.path.join('OEBPS','a.xhtml') == 'OEBPS\\a.xhtml'. Neither is a zip entry name. posixpath.normpath / posixpath.join give 'OEBPS/a.xhtml'. Every path operation on zip entry names must use posixpath.

7. zipfile NORMALIZES BACKSLASHES ON BOTH WRITE AND READ ON WINDOWS. Lib/zipfile/__init__.py line 404 `_sanitize_filename` replaces os.sep and os.altsep with '/'; it is called from ZipInfo.__init__ (line 455), and the READ path builds ZipInfo at line 1583 (`x = ZipInfo(filename)` inside _RealGetContents). I forged an archive whose raw bytes literally contain 'A\\B\\c.xhtml' and namelist() still returned 'A/B/c.xhtml'. On Linux/macOS os.sep=='/' so the replace is skipped and backslash names DO survive. Either way the resolver must handle backslashes, because the common real-world case is a backslash inside the HREF (src="images\\pic.jpg"), which zipfile never touches.

8. NON-UTF-8 ZIP FILENAMES PRODUCE MOJIBAKE. If the general-purpose flag bit 0x800 is clear, zipfile decodes entry names with cp437 (Lib/zipfile line ~1578). I forged an archive with GBK-encoded '目录/第一章.xhtml' and default ZipFile returned '─┐┬╝/╡┌╥╗╒┬.xhtml'; zipfile.ZipFile(path, metadata_encoding='gbk') returned '目录/第一章.xhtml' correctly. This matters directly: the user's library is Chinese-language books.

9. MIMETYPE RULES (W3C EPUB 3.3 sec 4.3.3, quoted): mimetype MUST be the first file; contents MUST be exactly 'application/epub+zip' in US-ASCII; MUST NOT have leading/trailing padding or whitespace; MUST NOT begin with a BOM; MUST NOT be compressed or encrypted; MUST NOT include an extra field in its ZIP header. Sec 4.3.2: only stored (0) and Deflate (8) are allowed; names MUST be UTF-8; ZIP encryption MUST NOT be used; ZIP64 is allowed.

10. CONTAINER.XML RULES (W3C EPUB 3.3 sec 4.2.6.3.1, quoted): namespace is 'urn:oasis:names:tc:opendocument:xmlns:container'; container/@version MUST be '1.0'; rootfiles is the REQUIRED first child and contains ONE OR MORE rootfile elements; rootfile/@full-path MUST be a path-relative-scheme-less-URL relative to the ROOT directory (not to META-INF); rootfile/@media-type MUST be 'application/oebps-package+xml'. On multiple rootfiles the spec explicitly says: 'this specification does not define how to interpret, or select from, the available options' — so picking the first is a legitimate choice, not a bug.

11. SPINE RULES (W3C EPUB 3.3 sec 5.7.1/5.7.2, quoted): itemref/@idref is required and 'item element IDs MUST NOT be referenced more than once'; linear defaults to 'yes' when absent or invalid, and linear='no' means auxiliary content the reader 'can access out of sequence' (notes, answer keys) — 'reading systems may present non-linear content where it occurs in the spine ... or may skip it until users reach the end'; page-progression-direction allows ltr | rtl | default, where 'default' means no preference; the `toc` attribute is explicitly marked LEGACY and takes an IDREF to the NCX manifest item.

12. MANIFEST PROPERTIES (W3C EPUB 3.3 sec 5.6.2.1 + appendix D.6, quoted): properties is a SPACE-SEPARATED list; 'EPUB creators MUST declare exactly one item as the EPUB navigation document using the nav property'; cover-image is OPTIONAL ('it is recommended to set the cover-image property, but setting this property is OPTIONAL'); scripted means the doc contains scripting and/or HTML form elements; remote-resources means it references resources outside the container; mathml / svg / switch likewise. These do NOT apply recursively through iframes.

13. FIXED LAYOUT (W3C EPUB 3.3 sec 8.2.2.1 + 8.2.2.6, quoted): global switch is `<meta property="rendition:layout">pre-paginated</meta>` (default 'reflowable'); it MUST NOT be declared more than once and MUST NOT use refines. Per-spine-item overrides are the itemref properties `rendition:layout-pre-paginated` and `rendition:layout-reflowable`. pre-paginated means 'reading systems produce exactly one page per spine itemref'. XHTML FXL docs get their size from the REQUIRED width and height in the FIRST viewport meta tag in the head ('Reading systems will ignore subsequent viewport meta tags'); SVG FXL docs use the viewBox attribute. I verified end-to-end on a fixture that the global meta and the per-item override are both detected and that the viewport parses to {'width':'1200','height':'1600'}.

14. NAV DOCUMENT RULES (W3C EPUB 3.3 sec 7.2/7.3/7.4, quoted): a valid nav doc 'MUST include exactly one toc nav element'; content model is nav > (h1-h6 [0 or 1], ol [exactly 1]); ol > li [1+]; li > (span | a) [exactly 1] then optional nested ol; 'An ol element MUST follow a span element (span elements cannot occur in leaf li elements)'. Labels must be the concatenation of all child content including title/alt text on non-textual descendants. page-list and landmarks are OPTIONAL and MUST NOT occur more than once, and both SHOULD be flat (single ol, no nesting). epub:type lives in namespace 'http://www.idpf.org/2007/ops'. In landmarks, epub:type is REQUIRED on each descendant <a>.

15. RESOLVER CORRECTNESS: my resolve_href passes a 21-case battery plus 10 doctests, covering percent-encoded space, un-encoded space, '+' correctly NOT treated as space (urllib.parse.unquote('a+b%20c.xhtml') == 'a+b c.xhtml'), a literal '%25' inside a real entry name (both double- and single-encoded href forms), '..' upward, '..' round-trip, '..' escaping the root (clamped, never raises), stray leading '/', '//', './', wrong case, backslashes in the href, NFC Unicode, deep relative bases, directory entries never matching, and a unique-basename last-resort rescue. Against all 3 real books, scanning every src/href/xlink:href in every spine document, unresolvable in-container hrefs = 0.

16. LENIENT PARSING WORKS WITH STDLIB ONLY. xml.etree.ElementTree.fromstring fails hard on: undeclared named entities (&nbsp;, &mdash;), raw ampersands, unclosed tags, and mismatched nesting. A 3-stage fallback (strip BOM/leading junk -> substitute named entities with numeric refs via html.entities.name2codepoint -> html.parser-based tree builder) parses all 7 malformed cases I tested. Critically, after the html.parser recovery path, attribute names keep their RAW spelling, so epub:type is the literal key 'epub:type' and NOT '{http://www.idpf.org/2007/ops}type' — any nav_type() helper must check both. I verified a non-well-formed nav document still yields a correct nested TOC with labels concatenated across nested markup ('One & only', '1.1 deep').

17. ZIP BOMB GUARD WORKS: a 407,903-byte .epub containing a single 400MB zero-filled entry is refused by a ratio check (1029:1). Guard must run on infolist() metadata BEFORE reading any entry.


## Pitfalls

1. USING os.path ANYWHERE ON ZIP ENTRY NAMES. On this Windows box os.path.normpath('OEBPS/text/../a.xhtml') == 'OEBPS\\a.xhtml' and os.path.join('OEBPS','a.xhtml') == 'OEBPS\\a.xhtml'. Neither exists in any zip. Use posixpath exclusively. This will silently break every relative href in every book with a subdirectory.

2. TREATING META-INF/encryption.xml AS DRM. One of the user's own three books would be refused. Check the Algorithm attribute: 'http://www.idpf.org/2008/embedding' (IDPF) and 'http://ns.adobe.com/pdf/enc#RC' (Adobe) are keyless font obfuscation and must be de-obfuscated and served normally. Anything else (xmlenc#aes128-cbc, aes256-cbc, Adobe ADEPT, Readium LCP) is real DRM — report it clearly by name and refuse that resource, but still open the book and show what is readable.

3. int(navPoint.get('playOrder')) WITHOUT try/except. Raises ValueError on a real book on this machine whose playOrder values are hex ('1D','2A','2F'). And SORTING by playOrder is worse than not parsing it: '20' parses as decimal 20 and sorts before '1D'-as-29, scrambling the chapter order. Use document order, always.

4. DOUBLE-DECODING PERCENT ESCAPES. If you unquote() an href and then unquote() again, 'a%2520b' collapses wrongly. Equally, if you never unquote, 'ch%201.xhtml' will not find the entry 'ch 1.xhtml'. Decode exactly once, then fall back to the RAW string as a second attempt, because some entry names genuinely contain '%'.

5. TREATING '+' AS A SPACE. '+' means space only in application/x-www-form-urlencoded, not in URL paths. urllib.parse.unquote correctly leaves it alone; urllib.parse.unquote_plus does NOT. Never use unquote_plus on an href.

6. RESOLVING rootfile/@full-path RELATIVE TO META-INF. It is relative to the container ROOT. 'OEBPS/content.opf' means OEBPS/content.opf, not META-INF/OEBPS/content.opf.

7. ASSUMING THE OPF HAS A DIRECTORY. One of the user's books has its OPF at 'content.opf' in the container root, so opf_dir == ''. posixpath.join('', 'text/a.xhtml') and posixpath.join('OEBPS', 'text/a.xhtml') must both work — guard the empty-base case explicitly.

8. ASSUMING THE OPF IS IN THE OPF NAMESPACE. Real books ship <package> with no xmlns at all, or the wrong one. Match every element by LOCAL NAME. Namespaces are only needed to tell dc:* apart from opf:* attributes (opf:role, opf:file-as, opf:scheme, opf:event) and to find epub:type.

9. SERVING CONTENT AS application/xhtml+xml. Chromium parses that media type with a draconian XML parser: one unescaped '&' or one unclosed <br> and the user sees a yellow XML parse error page instead of the chapter. Book 1 on this machine declares media-type application/xhtml+xml for files named *.html. (I measured that all three real books' content docs happen to be well-formed, but that is luck, not a guarantee.) Serve text/html from the scheme handler.

10. FEEDING XHTML TO ElementTree WITHOUT A FALLBACK. &nbsp; and &mdash; with no DOCTYPE are a hard ParseError, and they are extremely common. You need the 3-stage lenient parser.

11. AFTER html.parser RECOVERY, epub:type IS NOT NAMESPACED. It is the literal attribute key 'epub:type'. A nav_type() that only checks '{http://www.idpf.org/2007/ops}type' silently returns '' for every recovered nav document and you get no TOC.

12. DUPLICATE MANIFEST ids: keep the FIRST, not the last. The spine's idref was almost certainly authored against the first occurrence. Python dict building naturally keeps the last — that is the wrong direction.

13. SPINE itemrefs POINTING AT MISSING ids OR MISSING FILES. Both occur. Drop the entry and warn; do not index-shift the rest of the spine, and do not crash.

14. ZIP DIRECTORY ENTRIES (names ending in '/'). They read as 0 bytes. If they stay in the lookup index, an href of 'images/' resolves to a bogus empty resource. Filter them out when building the index.

15. NON-UTF-8 ENTRY NAMES. When the 0x800 flag bit is clear, zipfile decodes names as cp437 and Chinese filenames become '─┐┬╝/╡┌╥╗╒┬.xhtml', which no href will ever match. Detect and reopen with metadata_encoding.

16. PAGINATING FIXED-LAYOUT CONTENT. pre-paginated means exactly one page per spine itemref. Column-based pagination on an FXL page produces garbage. Detect the global meta AND the per-itemref rendition:layout-pre-paginated override, read width/height from the FIRST viewport meta tag, and scale-to-fit instead.

17. ASSUMING manifest count ~ spine count. Book 1 has 1247 manifest items and 30 spine items. Do not build UI or progress state off the manifest.

18. TOC ENTRIES WITH NO href. A nav <li><span>Part Two</span><ol>... is a grouping header with no target. Its TocNode must be non-clickable rather than resolving to None and navigating nowhere.

19. LOSING THE FRAGMENT. NCX content/@src and nav a/@href are frequently 'chapter1.xhtml#sec_3' — several real TOC entries in the user's books point into the MIDDLE of a spine document. Keep the fragment, percent-decode it, and scroll to it after load.


## Recommendations

1. Use the two modules I already wrote and verified as the parsing layer: C:\Users\mengz\epub-reader\research\epub_resolve.py (path/URL resolution) and C:\Users\mengz\epub-reader\research\epub_model.py (OPF/NCX/nav/cover/encryption). They are stdlib-only, pass a 40-case suite, and parse all three of the user's real books with zero warnings and zero unresolvable hrefs. Move them into the app package rather than reimplementing.

2. DISCOVERY CHAIN, in order: (1) open the zip; run the zip-bomb guard on infolist() before reading anything; (2) check `mimetype` (first entry, STORED, no extra field, exact bytes 'application/epub+zip') and record every violation as a WARNING only — never refuse, since real books violate these constantly; (3) read META-INF/container.xml in namespace urn:oasis:names:tc:opendocument:xmlns:container, find all <rootfile>, prefer media-type='application/oebps-package+xml', take the FIRST (the spec explicitly declines to define selection), and resolve its full-path relative to the CONTAINER ROOT; (4) if container.xml is missing or unparsable, fall back to scanning for any '*.opf', shallowest path first, and warn.

3. SET opf_dir = posixpath.dirname(opf_entry) ONCE and make it the base for every manifest href. Make base_dir a required parameter of the resolver so it is impossible to forget; for hrefs found INSIDE a content document the base is posixpath.dirname(that document's entry), not the OPF's.

4. PARSE EVERY ELEMENT BY LOCAL NAME. Use a `ln(el)` helper that strips '{ns}'. Use namespaces only for: distinguishing dc:* children of <metadata>, reading opf:role / opf:file-as / opf:scheme / opf:event on EPUB2 refinements, and matching epub:type (check BOTH '{http://www.idpf.org/2007/ops}type' and the raw key 'epub:type').

5. METADATA: do two passes. Pass 1 collects dc:* records keyed by @id and records EPUB2 opf:-prefixed refinements inline. Pass 2 applies EPUB3 `<meta refines="#id" property="...">` subexpressions (file-as, role, title-type, display-seq, alternate-script) onto those records. Choose the display title as the dc:title refined with title-type='main', else the first. Choose authors as dc:creator entries whose role is absent or 'aut'. Resolve THE identifier via package/@unique-identifier -> the matching dc:identifier @id — this exact string is the obfuscation key input, so getting it wrong silently breaks fonts.

6. TOC: build one nested model from three sources in priority order. (1) EPUB3 nav: the manifest item with properties containing 'nav'; inside it take nav[epub:type=toc], walk ol>li>(a|span)+nested ol; label = concatenated text including img/@alt; li/@hidden is an HTML boolean attribute (present, even empty, means hidden — but 'false' should be honoured); also capture epub:type=landmarks (keep each <a>'s own epub:type, that is what you dispatch on) and epub:type=page-list. If no nav carries epub:type='toc' but exactly one untyped <nav> exists, use it. (2) NCX: locate via spine/@toc idref, else any manifest item with media-type 'application/x-dtbncx+xml', else any '*.ncx' in the zip; walk navMap>navPoint recursively (navPoints nest), label from navLabel>text, target from content/@src. (3) Fall back to one node per LINEAR spine item labelled from its <title>, else its first <h1>-<h3>, else the filename — and warn.

7. USE DOCUMENT ORDER FOR THE NCX. Never sort by playOrder. Parse it only as an informational field, and only inside a helper that tries base 10 then base 16 then returns None, because a real book on this machine uses hex.

8. COVER DETECTION, in this exact priority order: (1) manifest item with properties containing 'cover-image'; (2) `<meta name="cover" content="ITEM-ID"/>` — and handle the two real-world deviations: if that id points at an XHTML page rather than an image, pull the first <img src> out of that page; if the content value is a href rather than an id, resolve it as a href; (3) guide <reference type="cover">, again dereferencing to the first <img> if it points at a page; (4) filename/id heuristics over image items only (basename starts with 'cover', or id in {cover, cover-image, coverimage}), then a loose 'cover' substring. All three of the user's books need step 2, so do not skip it as legacy.

9. ENCRYPTION: parse META-INF/encryption.xml and PARTITION by Algorithm. Put 'http://www.idpf.org/2008/embedding' and 'http://ns.adobe.com/pdf/enc#RC' in an `obfuscated` map; everything else in a `drm` list. DE-OBFUSCATE — it is ~12 lines, I verified it against a real book the user owns, and skipping it means that book renders in a fallback font. Do it lazily in the URL scheme handler: if the requested entry is in the obfuscated map, XOR the head before serving. For real DRM, name the scheme in the UI ('This book is protected by Adobe DRM and cannot be opened'), still open the book, and serve whatever is not encrypted.

10. FIXED LAYOUT: compute a per-spine-item layout. Global = `<meta property="rendition:layout">` (default reflowable); per-item overrides = itemref properties 'rendition:layout-pre-paginated' / 'rendition:layout-reflowable'. For a pre-paginated item, read width/height from the FIRST `<meta name="viewport">` in the head (or the SVG viewBox), disable the pagination/column CSS and the user font-size control entirely, and scale-to-fit the viewport instead. Also honour page-spread-left / page-spread-right / rendition:page-spread-center if you ever do two-up.

11. MALFORMED XML: implement parse_xml_lenient with a 4-stage ladder — strict ET.fromstring; strip BOM and leading junk; substitute undeclared named entities using html.entities.name2codepoint; finally an html.parser-based tree builder. Use html.parser, NOT lxml.html: it is stdlib, it never raises, and it keeps the project free of the parsing behaviour differences lxml introduces. (lxml 6.1.1 is installed and would also work via etree.HTMLParser(recover=True), but there is no reason to take the dependency for this.)

12. SERVE CONTENT DOCUMENTS AS text/html, not application/xhtml+xml, from the QWebEngineUrlSchemeHandler. Chromium's XHTML path is a draconian XML parser and a single unescaped '&' replaces the chapter with an error page. Serve the manifest's declared media-type verbatim for CSS, images, fonts, and audio — but override the XHTML/HTML ones to text/html. Note: I did not measure this inside QtWebEngine, only established the media-type semantics; it is worth a 5-minute confirmation with one deliberately malformed chapter.

13. OPEN THE ZIP VIA open_zip_with_encoding() (in epub_resolve.py) rather than zipfile.ZipFile() directly, so cp437-mojibake CJK filenames get repaired. This matters specifically for this user's Chinese-language library.

14. COLLECT WARNINGS, NEVER CRASH. Every defect I encoded (bad mimetype, multiple rootfiles, duplicate manifest ids, dangling idrefs, missing files, unparsable nav/NCX, recovered OPF) produces a string in book.warnings and a working book. Surface them behind a 'Book details' or debug pane; refuse only for a zip bomb or a genuinely absent package document.

15. TEST WITH THE ARTEFACTS I LEFT. `make_fixtures.py` regenerates the two adversarial books; `run_checks.py <paths>` prints a full structural dump; `test_edge.py`, `test_backslash.py`, `test_drm_fxl.py` are the regression suite. Run them with C:\Users\mengz\AppData\Local\Programs\Python\Python314\python.exe and set PYTHONIOENCODING=utf-8 or the CJK output will fail on the Windows console codepage.


## Open questions

1. I did not measure QtWebEngine's behaviour on media type application/xhtml+xml vs text/html. My recommendation to serve text/html rests on standard Chromium XML parsing semantics, not an experiment in this app. Worth a 5-minute check with one deliberately malformed chapter once the scheme handler exists — the failure mode (yellow XML error page replacing the chapter) is severe enough to be worth confirming.

2. No EPUB 3 books exist on this machine, so my nav-document, landmarks, page-list, rtl and fixed-layout rules are verified only against fixtures I built from the spec, not against books in the wild. The fixtures are deliberately nastier than typical real EPUB3 files, so I expect this to be conservative rather than optimistic, but the implementer should retest when a real EPUB3 arrives.

3. The multiple-rootfile case: I take the first, which the spec permits because it 'does not define how to interpret, or select from, the available options'. If the user ever opens a multiple-rendition book (EPUB Multiple-Rendition Publications 1.1), a rendition picker would be needed. I judged that out of scope; confirm that is acceptable.

4. I clamp '..' that escapes the container root rather than rejecting the href. That is what OCF 4.2.5 implies (parsing '..' against the container root URL returns the container root) and it maximises the number of broken books that render, but it does mean a malicious href cannot be distinguished from a sloppy one. Since nothing is ever written and the zip is the only namespace, I judged this safe — but flag it if the reader ever gains write or export features.

5. I did not implement media overlays (SMIL read-aloud) or EPUB3 manifest fallback chains. Neither appears in any of the user's books. Media overlays are a large feature; fallback chains are small and could be added cheaply if a book ever uses a foreign content document in the spine.


## Verified code snippets


### THE RESOLUTION ALGORITHM (href -> zip entry). The single most bug-prone piece. Full tested version at C:\Users\mengz\epub-reader\research\epub_resolve.py — 10 doctests + 21 battery cases green, 0 unresolvable hrefs across all 3 real books.

```python
import posixpath, unicodedata, zipfile
from urllib.parse import unquote, urlsplit

def split_href(href):
    """('text/ch1.xhtml#sec%201') -> ('text/ch1.xhtml', 'sec 1'). Query dropped."""
    if not href:
        return "", ""
    path, _, frag = href.strip().partition("#")
    return path.partition("?")[0], unquote(frag)

def is_remote(href):
    """http(s)://, //cdn/, data:, mailto: -- never look these up in the zip."""
    if not href:
        return False
    if href.startswith("//"):
        return True
    return bool(urlsplit(href).scheme)   # also catches a 'C:/...' drive letter

def _norm_key(name):
    """Forgiving key: backslash->slash, collapse '//', NFC, casefold."""
    name = name.replace("\\", "/")
    while "//" in name:
        name = name.replace("//", "/")
    return unicodedata.normalize("NFC", name).casefold()

def normalize_zip_path(base_dir, href_path):
    """Join + normalize to a container-root-relative POSIX path.
    MUST be posixpath: on Windows os.path.normpath('OEBPS/x/../a') returns
    'OEBPS\\a', which is never a zip entry."""
    p = (href_path or "").replace("\\", "/")
    if p.startswith("/"):
        joined = p.lstrip("/")          # OCF: '/' resolves to the container root
    else:
        joined = posixpath.join(base_dir, p) if base_dir else p
    out = posixpath.normpath(joined)
    while out.startswith("../"):        # OCF: '..' can never escape the root
        out = out[3:]                   # clamp, never raise
    if out in ("..", ".", "/"):
        out = ""
    return out.lstrip("/")

class ZipIndex:
    """Build ONCE per book."""
    __slots__ = ("names", "_exact", "_loose")
    def __init__(self, zf):
        # drop directory entries: an href of 'images/' must not match one
        self.names = [n for n in zf.namelist() if not n.endswith("/")]
        self._exact = set(self.names)
        self._loose = {}
        for n in self.names:
            self._loose.setdefault(_norm_key(n), n)   # first wins
    def lookup(self, cand):
        if cand in self._exact:                       # spec-correct, case sensitive
            return cand
        return self._loose.get(_norm_key(cand))       # malformed-file rescue

def resolve_href(index, base_dir, href):
    """-> (entry_name_or_None, fragment, is_remote_flag)
    base_dir is posixpath.dirname() of the document the href appeared in,
    container-root relative, '' for the root."""
    path, frag = split_href(href)
    if is_remote(href):
        return None, frag, True
    if not path:
        return None, frag, False          # bare '#frag' -> same document

    # 1. spec-correct: percent-decode exactly once, as UTF-8
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

    # 3. escapes that are cp1252/latin-1 rather than UTF-8
    try:
        alt = normalize_zip_path(base_dir, unquote(path, encoding="latin-1"))
        if alt not in (cand, raw):
            hit = index.lookup(alt)
            if hit:
                return hit, frag, False
    except Exception:
        pass

    # 4. last resort: unique basename anywhere in the archive
    want = _norm_key(posixpath.basename(cand))
    if want:
        m = [n for n in index.names if _norm_key(posixpath.basename(n)) == want]
        if len(m) == 1:
            return m[0], frag, False

    return None, frag, False
```

### Font de-obfuscation, both algorithms. VERIFIED: the Adobe branch turns the user's own 50人的二十年 fonts/00060.ttf into a valid TTF; the IDPF branch round-trips a fixture. Call this lazily from the URL scheme handler.

```python
import hashlib

OBFUS_IDPF  = "http://www.idpf.org/2008/embedding"   # EPUB 3.3 sec 4.4.5
OBFUS_ADOBE = "http://ns.adobe.com/pdf/enc#RC"

def obfuscation_key(algorithm, unique_identifier):
    if algorithm == OBFUS_IDPF:
        # spec 4.4.3: strip U+0020/09/0D/0A, SHA-1 of the UTF-8 form -> 20 bytes
        ws = (chr(0x20), chr(0x09), chr(0x0D), chr(0x0A))
        s = "".join(c for c in unique_identifier if c not in ws)
        return hashlib.sha1(s.encode("utf-8")).digest()
    if algorithm == OBFUS_ADOBE:
        # strip 'urn:uuid:' and hyphens, take 32 hex chars -> 16 bytes
        uid = unique_identifier.strip()
        if uid.lower().startswith("urn:uuid:"):
            uid = uid[9:]
        uid = "".join(c for c in uid if c in "0123456789abcdefABCDEF")
        return bytes.fromhex(uid[:32])
    raise ValueError("not an obfuscation algorithm: %r" % algorithm)

def deobfuscate(data, algorithm, key):
    """IDPF mangles the first 1040 bytes; Adobe the first 1024."""
    n = min(1040 if algorithm == OBFUS_IDPF else 1024, len(data))
    head = bytearray(data[:n])
    klen = len(key)
    for i in range(n):
        head[i] ^= key[i % klen]
    return bytes(head) + data[n:]

# Classify encryption.xml -- obfuscation is NOT DRM:
#   algo in (OBFUS_IDPF, OBFUS_ADOBE) -> de-obfuscate and serve normally
#   anything else (xmlenc#aes128-cbc, ADEPT, LCP) -> real DRM, report by name
```

### Lenient XML parsing with an html.parser recovery stage. ET.fromstring dies on &nbsp;, raw '&', unclosed tags and mismatched nesting — all routine in real EPUBs. Verified on 7 malformed cases.

```python
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
import html.entities as _he

_ENT = re.compile(rb"&(?!#|amp;|lt;|gt;|quot;|apos;)([A-Za-z][A-Za-z0-9]*);")

def _strip_preamble(data):
    if data[:3] == b"\xef\xbb\xbf":
        data = data[3:]
    i = data.find(b"<")
    return data[i:] if i > 0 else data

def _stub_entities(data):
    def sub(m):
        cp = _he.name2codepoint.get(m.group(1).decode("ascii"))
        return ("&#%d;" % cp).encode() if cp else b"&amp;" + m.group(1) + b";"
    return _ENT.sub(sub, data)

class _TreeHTMLParser(HTMLParser):
    VOID = {"area","base","br","col","embed","hr","img","input",
            "link","meta","param","source","track","wbr"}
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = ET.Element("html"); self.stack = [self.root]
    def handle_starttag(self, tag, attrs):
        el = ET.SubElement(self.stack[-1], tag,
                           {k: (v or "") for k, v in attrs})
        if tag not in self.VOID:
            self.stack.append(el)
    def handle_startendtag(self, tag, attrs):
        ET.SubElement(self.stack[-1], tag, {k: (v or "") for k, v in attrs})
    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]; return
    def handle_data(self, data):
        cur = self.stack[-1]
        if len(cur):
            cur[-1].tail = (cur[-1].tail or "") + data
        else:
            cur.text = (cur.text or "") + data

def parse_xml_lenient(data, what=""):
    for attempt in (data, _strip_preamble(data),
                    _stub_entities(_strip_preamble(data))):
        try:
            return ET.fromstring(attempt)
        except ET.ParseError:
            continue
    for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            text = data.decode(enc); break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("cannot decode %s" % what)
    p = _TreeHTMLParser()
    try:
        p.feed(text); p.close()
    except Exception:
        pass
    if not len(p.root):
        raise ValueError("cannot parse %s" % what)
    return p.root

# CRITICAL: after the html.parser path, attribute names keep their RAW
# spelling. epub:type is the literal key 'epub:type', NOT the namespaced
# '{http://www.idpf.org/2007/ops}type'. Always check both:
NS_EPUB = "http://www.idpf.org/2007/ops"
def nav_type(el):
    v = (el.get("{%s}type" % NS_EPUB) or el.get("epub:type")
         or el.get("type") or "")
    return v.strip().lower()
```

### NCX playOrder handling. A real book on this machine uses HEXADECIMAL playOrder ('1D','2A','2F'), so int() raises; and sorting by the values that do parse corrupts the TOC. Document order is authoritative.

```python
def _play_order(raw):
    """Informational ONLY. Never let this raise: real books use '00', '002',
    ' 3 ', and (measured on this machine) bare hex like '1D' and '2A'."""
    if not raw or not raw.strip():
        return None
    s = raw.strip()
    for base in (10, 16):
        try:
            return int(s, base)
        except ValueError:
            continue
    return None

# In the navMap walker, build children in DOCUMENT ORDER and return them
# unsorted. Measured across all 3 real books: wherever every sibling's
# playOrder parsed as decimal, document order already equalled sorted order,
# so sorting has zero upside; and where it does not parse (hex), sorting the
# subset that does parse actively scrambles chapters.
#   navPoints NEST (navPoint > navPoint) -- recurse.
#   label  = navPoint > navLabel > text   (whitespace-normalised)
#   target = navPoint > content/@src      (keep the #fragment!)
```

### Open an EPUB repairing cp437-mojibake entry names. Needed for this user's Chinese-language library: a GBK-named entry decodes to '─┐┬╝/╡┌╥╗╒┬.xhtml' by default and no href will ever match it.

```python
import zipfile

def open_zip_with_encoding(path):
    """OCF requires UTF-8 names, but old Windows zip tools clear the UTF-8
    general-purpose flag (bit 11 / 0x800) and write raw GBK/Shift-JIS bytes.
    zipfile then decodes them as cp437 -> mojibake."""
    zf = zipfile.ZipFile(path)
    suspect = any(not (i.flag_bits & 0x800)
                  and any(ord(c) > 127 for c in i.filename)
                  for i in zf.infolist())
    if not suspect:
        return zf
    for enc in ("utf-8", "gbk", "shift_jis", "cp949", "cp1252"):
        try:
            cand = zipfile.ZipFile(path, metadata_encoding=enc)
        except (LookupError, ValueError):
            continue
        # the right encoding leaves no cp437 box-drawing junk / replacement chars
        bad = sum(1 for i in cand.infolist() for c in i.filename
                  if "─" <= c <= "◿" or c == "�")
        if bad == 0:
            zf.close()
            return cand
        cand.close()
    return zf
```

### Zip-bomb guard. Run on infolist() metadata BEFORE reading any entry. Verified: refuses a 407KB .epub holding a 400MB entry (1029:1).

```python
def guard_zip_bomb(zf):
    infos = [i for i in zf.infolist() if not i.is_dir()]
    total = sum(i.file_size for i in infos)
    comp  = sum(i.compress_size for i in infos) or 1
    if total > 4 * 1024 ** 3:
        raise ValueError("refusing: %.1f GB uncompressed" % (total / 1024 ** 3))
    if total / comp > 200 and total > 256 * 1024 ** 2:
        raise ValueError("refusing: compression ratio %.0f:1 looks like a zip bomb"
                         % (total / comp))
```

## Files written

- `C:\Users\mengz\epub-reader\research\epub_resolve.py`

- `C:\Users\mengz\epub-reader\research\epub_model.py`

- `C:\Users\mengz\epub-reader\research\run_checks.py`

- `C:\Users\mengz\epub-reader\research\test_edge.py`

- `C:\Users\mengz\epub-reader\research\test_backslash.py`

- `C:\Users\mengz\epub-reader\research\test_drm_fxl.py`

- `C:\Users\mengz\epub-reader\research\make_fixtures.py`
