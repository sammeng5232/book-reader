# Book Reader

A Windows desktop e-book reader for **EPUB, Kindle (MOBI / AZW / AZW3) and DjVu**, written from
scratch: no third-party e-book library or decoder, nothing downloaded. Python 3.14 + PySide6 (Qt 6.11)
with Chromium (QtWebEngine) rendering the pages, so a book's own CSS, fonts and images display the way
the publisher intended.

- **Formats**
  - **EPUB 2 / 3**: read directly.
  - **MOBI, PRC, AZW, AZW3**: converted once to EPUB (from-scratch PalmDOC and HUFF/CDIC decompression,
    classic MOBI and KF8 structure, fonts, covers, contents) and cached, so every feature below works.
    Books without a real contents list get one built from their chapter headings (第…回 / CHAPTER …).
    DRM-protected Kindle books are refused, never decrypted; KFX and Topaz are not supported.
  - **DjVu**: a from-scratch decoder (JB2 masks with shared dictionaries, IW44 wavelet colour layers,
    BZZ, the Z'-coder), written in C# and compiled on first use with the compiler built into Windows.
    Scanned pages appear as fixed-layout pages with their OCR text laid invisibly over them, so search,
    selection, copying and highlights work on scans too. A DjVu outline becomes the contents list.
- **Opens real-world books**, including the awkward ones: EPUB 2 (NCX) and EPUB 3 (nav), Chinese text
  without mojibake, font obfuscation (not mistaken for DRM), hexadecimal NCX `playOrder`, broken XHTML,
  percent-encoded or Chinese file names inside the zip, fixed-layout books (scaled to fit).
- **Reading**: paginated or scrolling, instant page turns, and you land on the **same sentence** after a restart
  or after changing the font size.
- **Typography**: separate CJK and Latin fonts, size, line height, paragraph spacing, indent, margins,
  line length; light / sepia / dark / follow Windows; optional per-book overrides.
- **Panels**: contents (tracks the current chapter), bookmarks, highlights in four colours with notes
  (export to Markdown), and whole-book search (Chinese two-character queries work).
- **Library**: covers (or generated ones), continue-reading row, sort and search, add whole folders.
  Books stay where they are. The app never copies, moves or deletes your files.
- **Interface language**: 简体中文 / 繁體中文 / English / 日本語, switchable live in settings (or follow Windows).

## Run

```powershell
.\run.ps1                          # opens the library
.\run.ps1 "D:\Books\some book.epub" # opens a book (.epub .mobi .azw3 .azw .prc .djvu .djv)
```

Or, once built, double-click `dist\Book Reader\Book Reader.exe`.
A second launch hands its book to the window that is already open.

## Build the .exe

```powershell
.\build_exe.ps1
```

Produces a one-folder bundle at `dist\Book Reader\` (keep the folder together; `Book Reader.exe` is inside).

### Optional: open book files with it by double-clicking

```powershell
.\tools\install-file-association.ps1            # current user only (HKCU), no admin needed
.\tools\install-file-association.ps1 -DryRun    # show what it would change
.\tools\install-file-association.ps1 -Uninstall # undo exactly what it added
```

This makes Book Reader available under *Open with* for .epub, .mobi, .azw3, .azw, .prc, .djvu and .djv
(it never overrides an association another app already has). Windows then asks once per file type which
app to use by default.

## Where your data lives

| What | Where |
|---|---|
| Settings, library, positions, bookmarks, highlights | `%APPDATA%\Book Reader\` (plain JSON, atomic writes, `.bak` kept) |
| Cover thumbnails, and the cached EPUB versions of Kindle and DjVu books (safe to delete: rebuilt on demand) | `%LOCALAPPDATA%\Book Reader\cache\` |
| Log | `%APPDATA%\Book Reader\logs\book-reader.log` |

Books are identified by content hash, so a book you move or rename keeps its reading position and notes.

## Keyboard

Press **F1** in the app for the full list. The most useful ones:

| Keys | Action |
|---|---|
| Space / PageDown / → / ↓, or j | Next page |
| Shift+Space / PageUp / ← / ↑, or k | Previous page |
| Ctrl+→ / Ctrl+← | Next / previous chapter |
| Ctrl+G | Go to… |
| Ctrl+T · Ctrl+B · Ctrl+E | Contents · bookmarks · highlights |
| Ctrl+F or / · F3 / n · Shift+F3 / N | Search this book · next / previous result |
| Ctrl+, | Text and layout settings (including interface language) |
| Ctrl+D | Add or remove a bookmark |
| Ctrl+1 … Ctrl+4 | Highlight the selection yellow / green / blue / pink |
| Ctrl+C · Ctrl+Shift+C | Copy · copy with a citation |
| Ctrl+M | Paginated ⇄ scrolling |
| Ctrl+= / Ctrl+- / Ctrl+0 | Larger / smaller / reset text |
| Ctrl+Shift+D | Light ⇄ dark |
| F11 · Ctrl+Shift+F | Full screen · focus mode |
| Esc | Close a panel, leave focus mode / full screen, clear the selection (never closes the book) |
| Ctrl+O · Ctrl+Shift+L · Ctrl+W · Ctrl+Q | Open · library · close book · exit |

## Tests

```powershell
.\tests\run_tests.ps1                                   # every suite, including the GUI ones
.\tests\run_tests.ps1 -Filter OddPaths                  # a subset by name
& "$env:LOCALAPPDATA\Programs\Python\Python314\python.exe" tests\test_app.py   # end-to-end: drives the real app
```

The end-to-end suite runs the real app in child processes against a throwaway `APPDATA`, so your own
library is never touched. Windows flash briefly on screen while it runs. It opens the 16 generated fixtures
in `tests\fixtures\` and, if present, three real books from `Desktop\文件` (read-only).

## Layout

| File | Role |
|---|---|
| `epublib.py` | EPUB parser: container, OPF, NCX/nav, resolution, de-obfuscation, text for search |
| `webhost.py` | `epub://` scheme serving entries straight from the zip into Chromium; JS bridge |
| `assets/reader.js`, `reader.css` | In-page engine: pagination, position locator, theming, search highlights |
| `reader_page.py` | Reading screen: toolbar, dock panes, settings panel, status bar, error cards |
| `library_page.py` | Library screen |
| `store.py` | Persistence (JSON, atomic, versioned) |
| `theme.py` | Themes for both the Qt chrome and the page |
| `strings.py`, `i18n/` | UI text in four languages (`python strings.py --check` verifies parity) |
| `epub_reader.py` | Entry point: single instance, window, keyboard map |
| `bookformats.py` | Opens any supported file (by content, not extension) as an EPUB; conversion cache |
| `mobi.py` | Kindle MOBI / AZW / AZW3 to EPUB: PalmDB, PalmDOC, HUFF/CDIC, MOBI 6, KF8, heading-based contents |
| `djvu.py` | DjVu as a fixed-layout EPUB with an OCR text layer; drives the decoder, renders pages on demand |
| `djvutool/*.cs` | The DjVu decoder in C# (Z'-coder, BZZ, JB2, IW44, text zones, outline); `ZPTable.cs` is the spec's table, cross-checked against the reference decoder |

Design notes: `docs/CONTRACT.md`, `docs/DECISIONS.md`, and the research reports in `docs/research/`.
