# `epublib.py` — API reference (owner A)

A from-scratch EPUB 2/3 parser for EPUB Reader. Standard library only (no lxml,
no third-party EPUB library). Every other module reads books through it.

Verification:

| What | Command |
|---|---|
| Contract suite (156 tests) | `python -m unittest tests.test_epub_parsing` |
| Extra proofs: real books, fonts, performance, search, §2.1 invariant, CLI | `python -m unittest tests.test_epublib_extra` |
| Live Chromium cross-check against the real `assets/reader.js` (offscreen, opt-in) | `set EPUB_READER_CHROMIUM=1` then the line above |
| Human-readable summary of the user's three books | `python tests\test_epublib_extra.py --report` |
| Doctests | `python -c "import epublib, doctest; print(doctest.testmod(epublib))"` |

`python` here always means `C:\Users\mengz\AppData\Local\Programs\Python\Python314\python.exe`.
Real-book tests skip cleanly when the books are absent. The books are only ever opened read-only.

---

## 1. Opening a book

### `EpubBook.open(path: str | os.PathLike) -> EpubBook`
Opens a book read-only and parses the container, OPF, TOC, cover and layout. It raises
`EpubError` for every EPUB-level failure. A path that does not exist raises
`FileNotFoundError` unchanged: "missing" is a different problem from "broken", and the library
page treats it differently.

```python
from epublib import EpubBook, EpubError
try:
    with EpubBook.open(r"C:\books\50人的二十年.epub") as book:
        print(book.metadata["title"], len(book.spine))
except FileNotFoundError:
    ...                                   # show the missing-file badge
except EpubError as exc:
    print(exc.kind, exc.drm_scheme)       # e.g. 'drm', 'adept'
```

### `EpubBook.close() -> None`, `with EpubBook.open(...) as book:`
Closes the archive. It is idempotent. Reading after `close()` raises `EpubError(kind='corrupt')`.
Zip reads are serialised with a lock, so a thumbnail worker and the reader can share one book.

```python
book = EpubBook.open(path)
try:
    data = book.cover_bytes()
finally:
    book.close()
```

### `class EpubError(Exception)`
| attribute | meaning |
|---|---|
| `kind` | `'not_epub'` (a zip with no `META-INF/container.xml`), `'corrupt'` (not a zip, truncated, empty), `'drm'`, `'bad_opf'` (a container with no usable package document), `'too_large'` (zip-bomb guard), `'no_container'` (reserved, never raised; see Deviations) |
| `detail` | Technical text for the 技术细节 disclosure. Never shown by default. |
| `drm_scheme` | `'adept'` / `'lcp'` / `'unknown'` when `kind == 'drm'`, else `None` |
| `message` | Same as `str(exc)` |

```python
except EpubError as exc:
    card = S("error.drm") if exc.kind == "drm" else S("error.corrupt")
    log.info("open failed: %s (%s)", exc.kind, exc.detail)
```

Font obfuscation (IDPF `http://www.idpf.org/2008/embedding`, Adobe `http://ns.adobe.com/pdf/enc#RC`)
is **not** DRM: those books open normally. The user's own *50人的二十年* is one of them.

---

## 2. Book attributes

| attribute | type | notes |
|---|---|---|
| `path` | `str` | as passed to `open()` |
| `opf_path` | `str` | zip entry of the package document |
| `opf_dir` | `str` | posix; `''` when the OPF is at the zip root (*50人的二十年*) |
| `version` | `str` | `package/@version`, e.g. `'2.0'` |
| `metadata` | `dict` | `title`, `authors: list[str]` (dc:creator with role absent/`aut`), `language`, `identifier` (the unique-identifier), `publisher`, `date`, `description`, `subjects: list[str]` |
| `spine` | `list[SpineItem]` | document order, `linear=False` items included |
| `toc` | `list[TocEntry]` | nested; nav.xhtml, else NCX (DOCUMENT order, never `playOrder`), else from the spine |
| `toc_is_synthetic` | `bool` | True when built from the spine (the UI shows a note) |
| `cover` | `str \| None` | zip entry of the cover image |
| `is_fixed_layout` | `bool` | global `rendition:layout` or any pre-paginated itemref |
| `page_direction` | `str` | `'ltr'` or `'rtl'` |
| `warnings` | `list[str]` | non-fatal problems, for logs only |

```python
with EpubBook.open(path) as book:
    header = "%s — %s" % (book.metadata["title"], "、".join(book.metadata["authors"]))
    if book.toc_is_synthetic:
        show_note()
```

### `@dataclass(frozen=True) class SpineItem`
`id: str`, `href: str` (as written in the manifest, relative to the OPF),
`media_type: str`, `linear: bool`, `zip_name: str` (the exact archive entry).

```python
chapters = [s.zip_name for s in book.spine if s.linear]
```

### `@dataclass(frozen=True) class TocEntry`
`title: str` (whitespace-normalised; U+3000 is kept), `href: str` (raw, relative to the TOC
document, fragment removed), `fragment: str` (percent-decoded, no `#`, `''` if none),
`zip_name: str` (resolved entry; `''` for a grouping header or an external link),
`children: list[TocEntry]`.

```python
def walk(entries, depth=0):
    for e in entries:
        add_row(depth, e.title, e.zip_name, e.fragment)
        walk(e.children, depth + 1)
walk(book.toc)
```

---

## 3. Reading resources

### `read(zip_name: str) -> bytes`
Raw bytes of one entry. Obfuscated fonts are de-obfuscated transparently. A missing entry raises
`KeyError`, so a scheme handler can answer 404 without a special case. Lookup forgives case and
backslashes.

```python
font = book.read("fonts/00060.ttf")          # b'\x00\x01\x00\x00...' (valid TrueType)
```

### `read_text(zip_name: str) -> str`
Decoded text: BOM-aware, UTF-8 first, then the declared legacy encoding, then gb18030/cp1252.
It never guesses from bytes the way lxml does. That guess is the verified mojibake bug.

```python
css = book.read_text("OEBPS/css/main.css")
```

### `has(zip_name: str) -> bool`
```python
if book.has("META-INF/encryption.xml"): ...
```

### `resolve(href: str, base: str = "") -> tuple[str, str | None]`
Resolves an href to `(zip_name, fragment)`. `base` is the zip entry the href appeared in. For a
stylesheet's `url()` that is the stylesheet. `""` means the OPF. `fragment` is `None` when absent
and never contains `#`. A remote URL returns `("", fragment)`. An unresolvable href returns the
normalised candidate path. Percent-decoding happens exactly once, `+` is never a space, and `..`
is clamped at the root.

```python
zip_name, frag = book.resolve("../images/pic%201.png#x", "OEBPS/text/ch1.xhtml")
# ('OEBPS/images/pic 1.png', 'x')
```

### `spine_index(zip_name: str) -> int | None`
```python
i = book.spine_index(entry.zip_name)          # None if not in the spine
```

### `cover_bytes() -> bytes | None`
```python
data = book.cover_bytes()                     # JPEG/PNG bytes, or None
```

### `media_type(zip_name: str) -> str`
The manifest's declared type, else a guess from the suffix.
```python
book.media_type("OEBPS/fonts/a.woff2")        # 'font/woff2'
```

### `is_pre_paginated(zip_name: str) -> bool`, `viewport(zip_name: str) -> tuple[int, int] | None`
Fixed-layout helpers. The viewport comes from the first `<meta name="viewport">`, else the SVG viewBox.
```python
if book.is_pre_paginated(z):
    w, h = book.viewport(z) or (1200, 1600)
```

---

## 4. Text, units and search

### `plain_text(zip_name: str) -> str` — **CONTRACT §2.1**
The flattened document text. Every `gpos` in the app is an index into this string. It is
character-for-character what `epubReader.flatText()` returns in QtWebEngine for the same
document. An unreadable document returns `""` and adds a warning. Results are cached (48 documents).

```python
text = book.plain_text("ops/chapter1.xhtml")
assert text.startswith("\n第一章\u3000总论\n")
```

**The unit is the Unicode code point** (a Python `str` index). reader.js measures in UTF-16
internally and converts at the bridge (`toCP`/`fromCP`). An astral character such as U+20BB7 𠮷
counts 1 here and in every offset crossing the bridge, and 2 in JS `String.length`. This was
decided jointly with owner C's `assets/reader.js`. It was verified live: Python offsets equal
`epubReader.search()` offsets on documents containing U+20BB7, U+2A6A5 and U+1F600.

**Why it is more than "strip the tags".** webhost.py serves content documents as `text/html`, so
Chromium builds the DOM with the **HTML5 parser**. `flatten_html` models every HTML5 rule that
changes `document.body`'s text. Each rule below was measured in QtWebEngine 6.11.1 (Chromium 140)
and is pinned by `CHROMIUM_MEASURED` in `tests/test_epublib_extra.py`:

| HTML5 behaviour | Example source | Flattened |
|---|---|---|
| CR LF and lone CR become LF (2 of the user's 3 books are CRLF; 53 documents were wrong before this rule) | `<p>a\r\nb\rc</p>` | `a\nb\nc` |
| One LF right after `<pre>`, `<listing>`, `<textarea>` is dropped | `<pre>\nx</pre>` | `x` |
| Whitespace before `<body>` is dropped; any other character in `<head>` or after `</head>` implies `<body>` | `<head>HX<title>t</title></head><body>B` | `HXtB` |
| Text after `</body>` or `</html>` still belongs to `<body>` | `</body>\nAFTER\n</html>\n` | `\nAFTER\n\n` |
| The self-closing flag is ignored on HTML elements | `<head><script src="a.js"/></head><body>B` | `` (script swallows it) |
| `<template>` contents are not in the walked tree | `a<template>T</template>b` | `ab` |
| Text inside `script`/`style`/`noscript` is rejected at any depth and in any namespace (reader.js uses `closest('script,style,noscript')`) | `<svg><style>c</style><text>T</text></svg>` | `T` |
| `<![CDATA[..]]>` is text in SVG/MathML and a bogus comment in HTML | `<svg><text><![CDATA[a<b]]></text></svg>` | `a<b` |
| U+0000 is dropped from HTML text, becomes U+FFFD in raw text or foreign content, and after `<` | `<p>a\0b</p><textarea>q\0q</textarea>` | `abq\uFFFDq` |
| Adjacent character data is one run | `<table>\t<` | `\t<` |
| Foster parenting: text or elements misplaced in a `<table>` move in front of it, including re-created `<a>`/`<b>` | `<table>foo<tr><td>cell</td></tr>bar</table>` | `foobarcell` |
| `<!-->` and `<!--->` are complete comments | `a<!-->c` | `ac` |
| Character references: legacy names without `;`, cp1252 remap of `&#x80;`–`&#x9F;`, U+FFFD for 0/surrogates/out of range, controls KEPT | `&copy 1 &notit; &#x80; &#1;` | `© 1 ¬it; € \x01` |
| `display:none` text is kept (Python cannot see computed style) | `<p style="display:none">h</p>` | `h` |

```python
from epublib import flatten_html
flatten_html("<html><head><title>T</title></head><body><p>a&amp;b</p></body></html>")  # 'a&b'
```

### `flatten_html(source: str) -> str`
The walker used by `plain_text`. `source` is the decoded document text (`read_text`).
```python
assert book.plain_text(z) == flatten_html(book.read_text(z))
```

### `html5_unescape(text: str) -> str`
HTML5-exact character-reference resolution. It is `html.unescape` except that control/noncharacter
references are kept instead of deleted.
```python
html5_unescape("&#x80;&#1;")        # '€\x01'   (html.unescape gives '€')
```

### `normalize_newlines(source: str) -> str`
HTML input-stream preprocessing. CR LF and CR become LF, and `<` + NUL becomes `<` + U+FFFD.
```python
normalize_newlines("a\r\nb")        # 'a\nb'
```

### `doc_title(zip_name: str) -> str`
`<title>`, else the first `<h1>`–`<h3>`, else the file name.
```python
label = book.doc_title("OEBPS/ch3.xhtml")
```

### `units(zip_name: str) -> int`, `total_units() -> int`, `count_units(text: str) -> int`
One unit is one CJK character or one Latin word. Used for reading-time estimates. Both are cached.
```python
minutes = book.total_units() / 450
count_units("中文 test")            # 3
```

### `search(query: str, limit: int = 2000) -> list[SearchHit]`
A book-wide literal search in spine order:
- The query is plain text. Regex metacharacters (`. + ( ) [ ] \ $ ^ * ? | { }`) mean themselves.
- Surrounding whitespace is stripped. An empty or whitespace-only query (or `limit <= 0`) returns `[]`.
- Matching goes through `fold_for_search` on both sides, which is `fold()` in reader.js:
  case-insensitive, and `‘ ’ “ ” ‐ ‑ ‒ – — ―` match `' " -`. Python search and the in-page search
  therefore find the same things. Verified: 16,169 hits were identical across 376 documents.
- Matches do not overlap. The search stops after `limit` hits.

```python
for hit in book.search("汇率", limit=500):
    add_row(hit.chapter_title, hit.before, hit.match, hit.after)
    # later: webhost call_reader("showMatches", [{"gpos": hit.gpos, "length": hit.length}], 0)
```

### `@dataclass(frozen=True) class SearchHit`
| field | meaning |
|---|---|
| `spine_index`, `zip_name` | the document |
| `gpos`, `length` | code-point offsets into `plain_text(zip_name)` (chapter-local) |
| `match` | the exact original text, `plain_text(zip_name)[gpos:gpos+length]` |
| `before`, `after` | up to 40 code points either side. ASCII whitespace runs are shown as one space, the outer ends are trimmed, and the space next to the match is kept. |
| `chapter_title` | the first TOC title pointing at the document, else `doc_title` |

```python
hit = book.search("don't")[0]      # also finds "Don’t"
assert book.plain_text(hit.zip_name)[hit.gpos:hit.gpos + hit.length] == hit.match
```

### `fold_for_search(text: str) -> str`
A length-preserving fold that is identical to reader.js `fold()`. Each code point is lower-cased
only if it stays one code point (so U+0130 İ is kept, and Greek Σ is folded without final-sigma
context). Typographic quotes and dashes then become ASCII.
```python
fold_for_search("Don’t — İSTANBUL")     # "don't - İstanbul"
```

---

## 5. Cross-engine diff tools

### `python epublib.py --plain-text <book.epub> [zip_name]`
Prints JSON to stdout. With `zip_name`, one object:
```json
{"book": "...", "zip_name": "ops/titlepage1.xhtml", "unit": "codepoint",
 "length": 37, "utf16_length": 37, "astral": 0, "sha256": "f635...", "text": "\n从此岸到彼岸..."}
```
Without `zip_name`, it prints an array with one object per spine item. The JSON is ASCII-only
(`\uXXXX` escapes), so a Windows console code page cannot corrupt the Chinese. Exit status is 0
on success, 2 on failure with `{"error": ..., "kind": "no_entry" | "os_error" | <EpubError.kind>}`.
Compare `text` with `epubReader.flatText()` and `utf16_length` with `flatTextInfo().utf16Length`.

```bat
python epublib.py --plain-text "C:\...\从此岸到彼岸_....epub" ops/chapter1.xhtml > py.json
```

### `plain_text_report(book: EpubBook, zip_name: str) -> dict`
The object the CLI prints.
```python
rec = plain_text_report(book, "ops/chapter1.xhtml")
assert rec["utf16_length"] == rec["length"] + rec["astral"]
```

### `EpubBook.flat_text_fingerprint(zip_name: str) -> dict`
`{zip_name, length, sha256, head, tail}` for a quick first comparison.
```python
fp = book.flat_text_fingerprint(z)
```

### `python epublib.py info|flat|fingerprint <book> [entry] [--out DIR]`
Developer dumps: metadata as JSON, raw flattened text (optionally one `.txt` per document), or
fingerprints.

---

## 6. Lower-level helpers

### `decode_bytes(raw: bytes, *, warnings: list[str] | None = None, what: str = "") -> str`
```python
text = decode_bytes(zip_bytes, warnings=log, what="OEBPS/ch1.xhtml")
```

### `split_href(href: str | None) -> tuple[str, str]`
```python
split_href("text/ch1.xhtml#sec%201")      # ('text/ch1.xhtml', 'sec 1')
```

### `is_remote(href: str | None) -> bool`
```python
is_remote("https://example.com/x")        # True
```

### `normalize_zip_path(base_dir: str, href_path: str) -> str`
Always posixpath, never `os.path`.
```python
normalize_zip_path("OEBPS/text", "../images/a.png")   # 'OEBPS/images/a.png'
```

### `obfuscation_key(algorithm: str, unique_identifier: str) -> bytes`, `deobfuscate(data: bytes, algorithm: str, key: bytes) -> bytes`
IDPF uses a SHA-1 of the whitespace-stripped identifier and XORs 1040 bytes. Adobe uses 16 bytes
from the UUID hex and XORs 1024 bytes. `read()` already applies both.
```python
key = obfuscation_key(OBFUS_ADOBE, "urn:uuid:4e3c703b-3148-4bc7-a91c-fb637f44b8c3")
clear = deobfuscate(raw_font, OBFUS_ADOBE, key)
```

### `OBFUS_IDPF`, `OBFUS_ADOBE`, `OBFUSCATION_ALGORITHMS`
The algorithm URIs that are **not** DRM. The frozenset also contains the `enc#RC4` spelling.
```python
if algo in OBFUSCATION_ALGORITHMS: ...    # de-obfuscate, never refuse
```

---

## 7. Measured on the user's books (2026-09-16, this machine)

| book | spine | TOC (top/total/depth) | cover | units | open + total_units + 2-char search |
|---|---|---|---|---|---|
| 50人的二十年 (5.8 MB, most text: 259,494 code points) | 80 | 5 / 43 / 2 | `cover.jpeg` | 226,409 | ~170 ms, `经济`: 2,346 hits |
| 从此岸到彼岸 (13.1 MB, largest file, CRLF) | 24 | 24 / 86 / 2 | `ops/images/cover.jpg` | 145,506 | ~90 ms, `汇率`: 2,347 hits |
| 数理金融学 (李向科) (6.4 MB, hex NCX `playOrder`, CRLF) | 30 | 29 / 114 / 2 | `OEBPS/Image01200.jpg` | 161,964 | ~140 ms, `期权`: 932 hits |

All three books open with zero warnings. Every TOC title contains CJK and none is mojibake. The
李向科 TOC equals its NCX in document order; sorting by `playOrder` as hex would not.

---

## 8. Deviations from CONTRACT §2 and known limits

- `resolve()` returns `(zip_name, None)` when there is no fragment. The contract writes
  `tuple[str, str]`; `tests/test_epub_parsing.py` pins `None`.
- `EpubError.kind == 'no_container'` is reserved and never raised. A readable zip without
  `META-INF/container.xml` is `'not_epub'`, as pinned by `test_missing_container_xml_is_not_an_epub`.
- `open()` on a missing path raises `FileNotFoundError`, not `EpubError`. `read()` of a missing
  entry raises `KeyError`.
- `search()` strips the query and folds quotes and dashes like reader.js. The contract only says
  "search".
- Extra public names beyond §2: `flatten_html`, `html5_unescape`, `normalize_newlines`,
  `fold_for_search`, `plain_text_report`, `count_units`, `decode_bytes`, the href helpers, the
  obfuscation helpers, `media_type`, `is_pre_paginated`, `viewport`, `flat_text_fingerprint`,
  `opf_path`, `version`.
- §2.1 limits. None occurs in any fixture or real book; all were found by random tag soup. Fuzzing
  3,900 random documents against Chromium left about 0.2% disagreeing:
  - An HTML end tag that implicitly closes an **unclosed** `<svg>`/`<math>` ancestor
    (`<div><math></div>…`). Getting this right needs the full stack of open elements; well-formed
    XHTML cannot produce it.
  - Frameset documents are modelled only for the simple case, and `<frameset>` replacing a body
    that is still empty is not modelled.
  - The invariant assumes webhost keeps serving `read_text()` re-encoded as UTF-8 with
    `Content-Type: text/html; charset=utf-8`, and that nothing inserts text nodes into `<body>`.
