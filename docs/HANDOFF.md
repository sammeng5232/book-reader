# Handoff: Book Reader (Windows app v1.2 + Android app)

Written 2026-09-21 for whoever picks this up next. Read this file, then
`docs/CONTRACT.md` (module contract), `docs/DECISIONS.md` (what the user decided and
why) and `android/README.md` (how the phone build works).

---

## 1. What this project is

A Windows e-book reader written from scratch — no third-party e-book library, nothing
downloaded — plus, since 2026-09-20, an Android version of it.

* **Windows app** (`C:\Users\mengz\epub-reader`): Python 3.14 + PySide6/QtWebEngine.
  Reads EPUB, Kindle (MOBI/AZW/AZW3) and DjVu, and converts books to **LaTeX + PDF**
  (DjVu goes straight to PDF). Shipped as `dist\Book Reader\Book Reader.exe`.
* **Android app** (`android\`): the same converters, with a TeX engine and the LaTeX
  packages carried inside the APK, so it converts **fully offline**. The reading
  interface does not exist yet.

The Python modules are shared: the Android build copies `epublib.py`, `mobi.py`,
`bookformats.py`, `store.py`, `djvu.py`, `djvupdf.py` and `latexexport.py` out of the
project root. **Edit them once, in the root** — never fork a second copy.

---

## 2. Rules the user set (do not violate)

1. **Never modify, move or delete the user's book files.** Conversion only ever writes
   new files elsewhere.
2. **No pip installs, no new dependencies** for the desktop app: stdlib, PySide6,
   Pillow, lxml only. (The Android app additionally uses Chaquopy, Tectonic and the
   vcpkg C stack — already set up.)
3. **Do not commit or push unless the user asks.** Nothing since `4d5e5a1` is
   committed; see §6.
4. **Do not run the registry script** (`tools/install-file-association.ps1`) except
   with `-DryRun`.
5. **LaTeX cleanup policy** (machine-wide rule in `~/.claude/rules/latex-cleanup.md`):
   after any LaTeX compile, no `.aux`/`.log`/`.out`/etc. may be left beside the `.tex`.
   `latexexport.compile_pdf()` already satisfies this by building in a temp folder and
   copying back only the PDF — keep it that way.
6. The user's email address must not be sent anywhere.

---

## 3. State: what is done and proven

### Windows app — v1.2 "convert" feature (complete, unreleased)

| Piece | File | State |
|---|---|---|
| Book → LaTeX | `latexexport.py` (~2,600 lines) | done; 23 tests in `tests/test_latexexport.py` |
| DjVu → PDF | `djvupdf.py` + `layers` command in `djvutool/Program.cs` | done; 19 tests in `tests/test_djvupdf.py` |
| UI (dialog, job, progress, result) | `convert_dialog.py`, hooks in `reader_page.py` / `library_page.py` | done |
| Strings (4 languages) | `i18n/*.py` | done, 485 keys each, parity checked |

Verified: 377 unit tests pass; 15 of 16 end-to-end app tests pass (the failure is a
known flaky single-instance timing test, unrelated). Real books: 50人的二十年 385 pp in
21 s, 紅樓夢 1,134 pp in 2 min, Alice (MOBI) 101 pp in 10 s, the user's 642-page physics
DjVu → 57 MB searchable PDF in 105 s.

### Android app — converter works, and a first reader exists

Proven on the user's **Samsung SM-S7210, Android 16, arm64** and on an emulator:

| Job | Phone |
|---|---|
| Typeset a test document | 1.6 s |
| Sample EPUB → LaTeX + PDF | 0.9 s |
| Alice (MOBI), 101 pages | 3.8 s |
| 紅樓夢 (AZW3), 1,149 pages, 121 chapters | 115 s |
| ia_indiansummer.djvu, 295 pages → searchable PDF | ~2.5 min |
| Read Alice (MOBI): paginate, turn pages, chapter advance | works (§4.4) |
| Read sample EPUB | works (§4.4) |

Architecture (details in `android/README.md`):

* **TeX engine**: Tectonic 0.17 (XeTeX + xdvipdfmx) built for `arm64-v8a` and `x86_64`
  by `android/native/build-native.sh` (cargo-ndk + vcpkg C stack), loaded as a **JNI
  library** — deliberately *not* an exec'd binary, which avoids Android's rules about
  running packaged executables.
* **LaTeX packages**: `android/app/src/main/assets/texbundle/` (~93 MB, a Tectonic
  *directory bundle*), built by `android/native/make-bundle.sh` from the probe
  documents in `android/native/probes/`. Nothing is downloaded at runtime.
* **Python**: Chaquopy 17, Python 3.13 (there are no cp314 wheels), with lxml and
  Pillow. Entry point `android/app/src/main/python/bookreader_android.py`; Kotlin side
  `Converter.kt`.
* **DjVu**: the C# decoder is ported to Kotlin (`com.bookreader.djvu`), verified
  byte-identical against the C# oracle on real books (see §4.1).
* **Fonts**: the phone has no Windows fonts, so `ExportOptions.fonts="texlive"` makes
  the converter name fonts that ship with TeX: Fandol (Simplified), arphic
  `bsmi00lp.ttf`/`bkai00mp.ttf` (Traditional), Harano Aji (Japanese), baekmuk
  (Korean), TeX Gyre Termes (Greek/Cyrillic), Latin Modern otherwise.

---

## 4. What is left, in order

### 4.1 Finish the DjVu decoder port (verified 2026-09-21)

The C# decoder (`djvutool/*.cs`, 2,600 lines) was ported to Kotlin so the phone needs
no subprocess. **Verified against the C# oracle on the user's phone (SM-S7210):**
`info`/`alltext`/`text` JSON byte-identical, `layers` binary byte-identical, PNG
renders pixel-identical, JPEG renders within encoder tolerance (mean grey diff ≤ 1.14)
for `tests/samples/ia_jstor_20637537.djvu` (2 pp) and `ia_indiansummer.djvu` (295 pp,
pages 0/1/2/10/100). Both books also converted to PDF on-device and the PDFs check out
with QtPdf (correct page counts, OCR text extractable — "INDIAN SUMMER. 81" on p. 100).

One bug was found and fixed during verification: Kotlin's `String.trim()` removes every
char ≤ U+0020, so a real OCR word consisting of U+001F was silently dropped where C#'s
`String.Trim()` (whitespace only) kept it. `DjvuDocument.isDotNetWhiteSpace` now mirrors
the .NET set exactly — neither Kotlin `trim()` nor Java `Char.isWhitespace` (which counts
U+001C–U+001F as whitespace and U+00A0 as none) matches.

The harness (`DjvuCheck.kt`, `autorun djvu` / `autorun djvupdf`) is still in place and
reusable; the golden data and the compare script are in the session scratchpad
`djvukotlin/` (regenerate with `assets/djvutool.exe serve` — see `gen_golden.py` there)
if that folder is gone.

* Watch the unsigned arithmetic when touching the codecs: C# `uint`/`>>` are logical,
  Kotlin `Int` is signed — use `ushr` and `and 0xFF`/`and 0xFFFF` masks. The ZP coder,
  BZZ and IW44 paths break subtly (pages decode as noise) if this is wrong.

### 4.2 Long conversions must survive Samsung's battery manager (done 2026-09-23)

Conversions now run under `ConvertService` — a minimal foreground service
(`dataSync` type) that starts right before the worker thread and stops when the
result is shown. It runs nothing itself; it holds the process at foreground-
service priority with an ongoing notification so Freecess-style app freezing
cannot kill a long typeset. `POST_NOTIFICATIONS` is requested once on Android
13+ (the service works even with the notification silenced).

### 4.2a TeX bundle: math fonts, stale copies (fixed 2026-09-23)

Two Android-only conversion bugs, both paid for with the Björk book
(`Arbitrage Thy in Ctus Time_Björk.epub`, 5,475 formula images):

* **No PDF after the .tex** — the engine died with
  `Font OML/cmm/m/it/14.4=cmmi12 … not loadable: Metric (TFM) file not found`.
  The bundle is filled by compiling `native/probes/*.tex`; their preambles load
  `amsmath,amssymb` but their *bodies* had no math, so the Computer Modern 12pt
  metrics (`cmmi12.tfm`, `cmr12.tfm`) were never pulled in. A math-heavy book
  needs them (superscripted letters at `\Large` reach full-size math fonts).
  Fix: `native/probes/math.tex` exercises real math; the bundle was rebuilt
  (392 files, fingerprint `e8fa6d0c…`).
* **App updates kept the OLD bundle** — `TexEngine.bundleDir` stamped `.unpacked`
  once and never refreshed, so an install-over kept the stale extracted copy (and
  the format built from it). The stamp now records the bundle's `SHA256SUM`
  fingerprint, and the format cache is cleared when it changes.

Also fixed in the same pass: `publishResults` flattened the converter's folder
layout (the .tex landed without its `images/` subfolder, uncompilable elsewhere);
typesetter errors were swallowed behind a success message reading "pages: 0";
and re-converting a book duplicated its files.  Replacement is done by sweeping
every row the book previously published (one `RELATIVE_PATH LIKE` pass) before
anything is inserted — interleaving per-file delete/insert races MediaProvider's
asynchronous file removal, and a 5,000-image book then lands in a parallel
`"title (2)"` folder.  Verified on the emulator (AVD BookReader35): the Björk
book converts to a 19.3 MB PDF with the folder layout intact, and re-converting
replaces the files in place.

Known limitation, not fixed: PDFs produced by the bundled engine have a poor text
layer (copy/search extract mojibake). Vanilla tectonic 0.17 on the host shows the
same, so it is upstream xdvipdfmx behaviour, not the custom build. Pages render
and print fine.

### 4.2b Formula images scale with the font size (fixed 2026-09-23)

Publishers ship formulas as little GIF/PNG images drawn for the browser's default
~16 px text context (= 12 pt at 96 dpi); `latexexport` used a flat `px × 0.75`
mapping regardless of the export's point size, so formulas sat ~20 % oversized
next to 10–11 pt text. Pictures are now scaled by `px × 0.75 × font_size / 12`
(`_EPUB_REF_FONT_PT` in `latexexport.py`), which restores the publisher's
intended formula-to-text proportion at every font size. Regenerating real LaTeX
from these images is not possible (no MathML, `alt="image"` only); when an EPUB
*does* carry MathML with a TeX annotation, `el_math` already emits `\(...\)`.

### 4.2c Browser-style tabs and multiple windows (fixed 2026-09-23)

The app now has browser-style tabs (Ctrl+T / Ctrl+Tab / Ctrl+Shift+Tab) and
multiple windows (Ctrl+N). Tabs can be moved between windows by dragging, and a
context menu on a tab offers "Move to new window". Session restore remembers
every window's tabs. The `MainWindow` class was refactored from a single-page
QStackedWidget into a tab manager over `ReaderPage` instances; `ReaderPage`
objects are pooled and reused. A `TabBar` subclass of `QTabBar` provides the
drag-and-drop protocol (`application/x-bookreader-tab`).

### 4.3 File picking (SAF) — done (2026-09-21)

`MainActivity` uses `ActivityResultContracts.OpenDocument` (`ACTION_OPEN_DOCUMENT`)
for both **Open** and **Convert**.  The picked file is copied into `files/books/`
with `ContentResolver.openInputStream(...).copyTo(...)` — the source is never touched
(the user's rule), and the Python side gets a real, seekable path regardless of whether
the provider's fd was one.  **DjVu is read directly**: `DjvuReaderActivity` decodes
pages on demand with the Kotlin `DjvuDocument.render` (no conversion, ~0.5–2 s a page);
`PdfReaderActivity` (platform `PdfRenderer`) handles any PDF the user opens.  Converting
DjVu to PDF is still available from the Convert button, just no longer on the read path.

### 4.4 The reading interface on Android (first version works, 2026-09-21)

`assets/reader.js` + `reader.css` (the pagination/position/highlight engine) run in an
Android WebView **unchanged**, copied into the APK by the `copyReaderAssets` Gradle task.
The pieces that had no Android equivalent were re-done:

* **Origin**: `WebViewAssetLoader` serves book entries from `https://<per-book-host>
  .bookreader.app/` (a secure, non-opaque origin), replacing the desktop's `epub://`
  scheme.  Per-book host = SHA-256 of path+size+mtime, same idea as `host_id_for`.
* **Bridge**: `addWebMessageListener` (object name `epubReaderHostPort`) +
  `postWebMessage` replace QWebChannel.  The injected boot facade
  (`BookHost.injectBoot`) presents the same `window.epubReaderHost` surface
  reader.js writes against.  **The injected listener object is `epubReaderHostPort`,
  NOT `epubReaderHost`** — the boot facade owns the latter name.
* **Injection**: `addDocumentStartJavaScript` runs boot+reader.js at document start in
  the main world (androidx.webkit exposes no isolated worlds, and `evaluateJavascript`
  shares the main world, so `callReader` sees `window.epubReader`).
* **CSP**: `script-src 'none'` still applies to book documents; the injected script
  executes anyway (same as Qt user scripts).  A book's own inline `<script>` is blocked
  by design — you will see that console error on books that ship scripts; it is not a bug.

Files: `java/com/bookreader/BookHost.kt` (serving + bridge + callReader),
`java/com/bookreader/ReaderActivity.kt` (tap-to-turn, page indicator, TOC drawer,
day/paper/night themes, font size), `python/reader_android.py` (open/read/resolve/spine/
toc as JSON over the shared `bookformats` book object).

**Verified on the phone**: Alice (MOBI) scrolls vertically through chapters (scroll is
the default mode; the toolbar toggle switches to paginated columns), a swipe at a
chapter's bottom/top edge pulls in the next/previous one, `positionChanged` flows, and
the sample EPUB renders.  DjVu reads as a continuous vertically-scrolling strip in
`DjvuReaderActivity` (RecyclerView, pages decoded on demand, pinch to zoom).  Position
is saved through `store` in the desktop's format.

**Traps paid for here**: (1) an EPUB "won't scroll" when you are on a one-page title
page — there is genuinely nothing to scroll; test on a real chapter.  (2) Gradle
incremental builds can report BUILD SUCCESSFUL while shipping a stale APK after a
Kotlin error — if behaviour doesn't change, run `:app:assembleDebug --rerun-tasks`
once and watch for a masked compile failure.  (3) `am start` directly at a
non-exported activity (`ReaderActivity`) is a Permission Denial after a force-stop —
drive it through `MainActivity`'s `autorun` extra instead.  (4) reader.js scroll mode
needs a real device-width CSS viewport: the WebView must set `useWideViewPort` and the
boot script injects `<meta viewport width=device-width>`.

**Not yet there** (still to do, in rough order): search UI, highlights/notes, the
four-pane dock, images-heavy and CJK book soak testing (font stacks are `serif`/
`sans-serif` placeholders — wire real Android CJK font names), `WebViewAssetLoader`
should be used with `setDomain` for the per-book host (currently a manual host check —
works, but the class's own path handling is bypassed), external-link handoff, and
DjVu reading (DjVu has no HTML spine; the Android reader is EPUB/MOBI/AZW/AZW3 only —
DjVu still goes through conversion to PDF).

### 4.5 Packaging

APK is ~136 MB with both ABIs (unsigned debug). Decide on ABI splits vs one APK, and
release signing, when the user asks for a shippable build.

---

## 5. How to build, run and test

### Windows app

```powershell
# run from source
.\run.ps1
# tests (GUI ones open real windows)
.\tests\run_tests.ps1
& "$env:LOCALAPPDATA\Programs\Python\Python314\python.exe" -m unittest tests.test_latexexport
# the exe
.\build_exe.ps1
```

Python is `C:\Users\mengz\AppData\Local\Programs\Python\Python314\python.exe` (NOT
`python` on PATH, which is a different 3.13). Child processes need
`PYTHONUSERBASE=<site.getuserbase()>` because PySide6 lives in the per-user site.

### Android app

Native parts build in WSL (Ubuntu); the APK is assembled on Windows.

```bash
# in WSL: engine (both ABIs) and the offline LaTeX bundle
~/androidtex/native/build-native.sh          # or android/native/build-native.sh
android/native/make-bundle.sh
```

```powershell
cd android
$env:JAVA_HOME = "C:/Program Files/Microsoft/jdk-17.0.20.8-hotspot"   # Windows-style!
.\gradlew.bat :app:assembleDebug
```

Driving the phone (serial `R5CX92E7H2F`; adb is
`$env:LOCALAPPDATA\Android\Sdk\platform-tools\adb.exe`):

```bash
adb -s R5CX92E7H2F install -r app/build/outputs/apk/debug/app-debug.apk
# put a book where the app can read it (see the trap about adb-created folders)
adb -s R5CX92E7H2F push book.azw3 /data/local/tmp/book.azw3
adb -s R5CX92E7H2F shell "run-as com.bookreader sh -c 'mkdir -p files/books && cp /data/local/tmp/book.azw3 files/books/'"
adb -s R5CX92E7H2F shell am force-stop com.bookreader
adb -s R5CX92E7H2F shell am start --user 0 -n com.bookreader/.MainActivity \
    --es autorun /data/user/0/com.bookreader/files/books/book.azw3
adb -s R5CX92E7H2F logcat -s BookReader:I
```

An emulator AVD `BookReader35` (API 35, google_apis, x86_64, 4 GB RAM, 12 GB data)
also exists; boot it headless with
`emulator -avd BookReader35 -no-snapshot-load -no-window -gpu swiftshader_indirect`.

---

## 6. Repository state and an open question for the user

* Last commit is `4d5e5a1` (v1.1.0). **Everything in §3 is uncommitted**: modified
  `README.md`, `djvu.py`, `djvutool/Program.cs`, `docs/DECISIONS.md`, `i18n/*`,
  `library_page.py`, `reader_page.py`, `store.py`, `tests/*`; new `latexexport.py`,
  `djvupdf.py`, `convert_dialog.py`, `tests/test_latexexport.py`,
  `tests/test_djvupdf.py`, and the whole `android/` tree.
* **Unanswered question:** the GitHub repo `sammeng5232/book-reader` was created
  private but is now **public**. The user was asked whether to make it private again
  before publishing the v1.1.0 release, and has not answered. A release ZIP
  (`Book-Reader-v1.1.0-win64.zip`, sha256 `e9724d9c…cb30`) was built but never
  uploaded. **Ask before publishing anything.**
* `.gitignore` for the Android tree already excludes `jniLibs/` and `assets/texbundle/`
  (both are build outputs, ~145 MB).

---

## 7. Traps already paid for (do not rediscover these)

**Shell / tooling**

* The Bash tool collapses `\\` inside heredocs — writing LaTeX or regex through a
  heredoc silently corrupts it. Use the Write/Edit tools for such files.
* Git Bash rewrites arguments that look like Unix paths: `adb shell am start … -n
  com.bookreader/.MainActivity` became `cmp=Files/Git/storage/…` and silently did
  nothing. Set `MSYS_NO_PATHCONV=1` for adb commands — **but** that same variable then
  breaks `JAVA_HOME` conversion for Gradle, which fails with "JAVA_HOME is set to an
  invalid directory". Use a Windows-style `JAVA_HOME`.
* Never filter a Gradle build's output so aggressively that its errors disappear; two
  builds "succeeded" for half an hour while actually failing.
* Background jobs started with `nohup` inside `wsl -e bash -lc` die with the session.
  Run long WSL builds as harness background tasks.
* `adb shell run-as … cat file.pdf` corrupts binaries on Windows; use `adb exec-out`
  or `adb pull`.

**The user's phone**

* It has **Samsung Dual App**: two copies of the app (user 0 and user 95). `am start`
  without `--user 0` can hit the wrong one and appear to do nothing. **Removing the
  clone with `pm uninstall --user 95` does not stick** — Dual App re-syncs the clone
  whenever the user-0 app is installed or updated. The durable removal is on the phone:
  Settings → Advanced features → Dual Messenger → take Book Reader off the list.
* A folder created by `adb shell mkdir` under `Android/data/<pkg>/` is owned by
  `shell`, and the app then cannot open files inside it (`FileNotFoundException`).
  Copy books in with `run-as` instead (see §5).
* Samsung freezes background apps; see §4.2.

**Code**

* lxml hands out a *new* Python proxy for a node whenever none is alive, so a set of
  `id(element)` forgets its members. `latexexport._NodeSet` holds the elements.
* XeTeX treats CJK characters as letters: `\centering附录` parses as one undefined
  command. Every command word emitted must be followed by a space or brace.
* `\hypertarget` at the very start of a table cell adds a blank line; prefix
  `\leavevmode`.
* `\XeTeXgenerateactualtext=1` is needed or copied text from the PDF comes back with a
  font's glyph names (Cambria's hyphen as U+2011, "ά" as "αʆ").
* `ReaderPage`'s toolbar calls page methods during `__init__`, so those methods must
  tolerate a half-built page.
* DjVu: `DjvuTool` used a non-reentrant lock and deadlocked when the helper failed to
  start — it is an `RLock` now.
* Tectonic on Android: `tectonic_bridge_core`'s double-close panic crosses an
  `extern "C"` frame and aborts the app; `build-native.sh` patches it and refuses to
  build if an older upstream bug returns. Recipe credit:
  <https://github.com/thobgg/TeXslate>.

---

## 8. Where the background research lives

Two research reports were produced while building this and are worth reading before
touching the Android reader or packaging:

* Android app stack (Chaquopy, WebView, storage, packaging, emulator):
  session scratchpad `androidapp/research.md`.
* Cross-compiling a TeX engine: superseded by the Tectonic approach actually used, but
  `android/README.md` records the working recipe.

If those scratchpad files are gone, the essentials are already summarised in
`android/README.md` and in this file.
