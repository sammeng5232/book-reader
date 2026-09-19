# Research: packaging

## Summary

PyInstaller 6.21.0 officially supports Python 3.14 (Requires-Python <3.16,>=3.8, with 3.14/3.15 classifiers) and packages PySide6 6.11.1 QtWebEngine correctly out of the box — no hook patching, no extra flags. I proved the whole path by building a throwaway PySide6 QtWebEngine app (with a custom `epub://` URL scheme handler, i.e. the actual planned architecture) end to end, launching the resulting .exe non-interactively, and reading the rendered DOM text back out. All PySide6/QtWebEngine hook logic lives in PyInstaller's own `hooks/` directory; pyinstaller-hooks-contrib 2026.6 contributes nothing for Qt (its stdhooks only hold `hook-pyqtgraph.py` and `hook-qtmodern.py`). onedir wins decisively over onefile: 2.7 s first launch / ~1.0 s warm, versus 12.7–14.0 s on EVERY onefile launch because it re-inflates ~560 MB into %TEMP%\_MEIxxxxxx each time. The biggest real-world trap is not Qt at all — it is MAX_PATH: PyInstaller's Win32 icon/resource stamping (`EndUpdateResourceW`) is not long-path aware and kills the build with `WinError 122` after four minutes of analysis if the output .exe path exceeds ~260 chars, even with `LongPathsEnabled=1`. CJK exe names work on this machine, but I build under an ASCII name and rename afterwards (verified safe) so the script is not hostage to the shell's ANSI code page. A verified post-build prune drops the bundle from 560.3 MB to 421.9 MB with the app still rendering. build_exe.ps1, tools/make_icon.py and tools/install-file-association.ps1 are written; build_exe.ps1 was run end to end against the throwaway entry point and all throwaway build output has been deleted.


## Verified facts

1. PyInstaller 6.21.0 supports Python 3.14. Proven by reading installed metadata: Requires-Python = '<3.16,>=3.8' and classifiers include 'Programming Language :: Python :: 3.14' and ':: 3.15'. Also proven empirically — multiple successful builds on C:\Users\mengz\AppData\Local\Programs\Python\Python314\python.exe (3.14.6).

2. pyinstaller-hooks-contrib 2026.6 contributes NOTHING for Qt. Proven by listing C:\Users\mengz\AppData\Roaming\Python\Python314\site-packages\_pyinstaller_hooks_contrib\stdhooks\ — the only Qt-adjacent entries are hook-pyqtgraph.py and hook-qtmodern.py. Every PySide6 hook (hook-PySide6.py, hook-PySide6.QtWebEngineCore.py, hook-PySide6.QtWebEngineWidgets.py, etc.) lives in PyInstaller\hooks\.

3. hook-PySide6.QtWebEngineCore.py (read on disk) hard-fails with SystemExit if the Qt version is < 6.2.2; we are on 6.11.1 so it proceeds. It calls add_qt6_dependencies(), then pyside6_library_info.collect_qtwebengine_files(), and appends hiddenimport 'PySide6.QtPrintSupport'.

4. collect_qtwebengine_files() (PyInstaller/utils/hooks/qt/__init__.py, line 1056) on Windows collects exactly four things, all into the 'PySide6' subdirectory (qt_rel_dir == 'PySide6'): (1) the whole TranslationsPath/qtwebengine_locales directory -> PySide6/translations/qtwebengine_locales, 53 .pak files; (2) the whole DataPath/resources directory -> PySide6/resources (icudtl.dat, qtwebengine_resources*.pak INCLUDING the .debug.pak variants, v8_context_snapshot.bin and .debug.bin) = 101.4 MB as collected; (3) the glob 'QtWebEngineProcess*' from LibraryExecutablesPath as a BINARY, with the destination forcibly overridden to '.' for PySide on Windows (line ~1181), i.e. PySide6/QtWebEngineProcess.exe; (4) a qt.conf next to that helper.

5. PyInstaller SYNTHESIZES the helper's qt.conf. Qt >= 6.3 no longer ships one, so the hook writes '[Paths]\nPrefix = .' into the workpath and collects it. Verified in the produced bundle: dist\<app>\_internal\PySide6\qt.conf contains exactly '[Paths]' / 'Prefix = .'.

6. ICU is NOT collected as DLLs on PySide6 6.11.1. collect_extra_binaries() in hook-PySide6.py globs for icudt??.dll / icuin??.dll / icuuc??.dll, libEGL.dll+libGLESv2.dll (+d3dcompiler_??.dll), and opengl32sw.dll. Proven by listing the wheel: only opengl32sw.dll (19.7 MB) exists. There are no ANGLE DLLs and no ICU DLLs; Qt 6.11 ships ICU data as resources/icudtl.dat (10.0 MB) instead.

7. The PySide6 runtime hook (hooks/rthooks/pyi_rth_pyside6.py) sets QT_PLUGIN_PATH to sys._MEIPASS/PySide6/plugins, sets QML2_IMPORT_PATH, prepends sys._MEIPASS to PATH (so QtNetwork finds OpenSSL), and creates an embedded qt.conf. No manual env fiddling is needed in app code.

8. A frozen PySide6 QtWebEngine app WORKS, including a custom QWebEngineUrlScheme handler — the exact planned architecture. Proven: the throwaway app registered scheme 'epub' (Syntax.Host, SecureScheme|LocalAccessAllowed|ViewSourceAllowed) before QApplication, served HTML from a QBuffer, loaded epub://book/index.html, and runJavaScript read back the DOM text 'PACKAGING OK 中文' from the frozen .exe. CJK renders correctly in the frozen Chromium.

9. MEASURED onedir (untouched, PySide6 6.11.1 + QtWebEngine): 560.3 MB across 2,977 files; bootstrap .exe itself is 2,056,412 bytes (2.0 MB). Clean build 285.3 s; incremental rebuild without --clean 198.4 s.

10. MEASURED onefile: single 209.7 MB .exe, build 271.5 s. Startup measured twice: 12.69 s and 14.03 s wall (10.97 s / 12.79 s to rendered DOM). There is NO warm path — the second run was slower than the first. It re-extracts the full ~560 MB payload to %TEMP%\_MEIxxxxxx on every launch.

11. MEASURED onedir startup, final pruned + renamed bundle: first launch 2.741 s wall (2.576 s to rendered DOM); warm launches 1.098 s and 1.037 s wall (0.957 s and 0.871 s to DOM). The untouched 560 MB bundle measured 3.737 s first / 1.237 s warm. For reference, running the same app unfrozen from source took 2.564 s.

12. MAX_PATH kills the build. Reproduced deterministically: building into a directory where the produced exe path was 283 chars failed after ~240 s of analysis with `win32ctypes.pywin32.pywintypes.error: (122, 'EndUpdateResourceW', ...)`. HKLM\SYSTEM\CurrentControlSet\Control\FileSystem\LongPathsEnabled is 1 on this machine and does NOT help. Isolated it properly: ascii-name+no-icon, ascii+icon, cjk+no-icon and cjk+icon ALL failed at that path, and a trivial ascii build in a 36-char directory succeeded — so it is path length, not the icon and not the CJK name.

13. A CJK exe name DOES work on this machine. `--name "EPUB阅读器"` produced a working dist\EPUB阅读器\EPUB阅读器.exe. The system ANSI code page here is 65001 (verified via [System.Text.Encoding]::Default.CodePage and chcp), which is why the argv survives.

14. Renaming a onedir exe (and its folder) is safe. Verified by renaming EPUB阅读器.exe to Renamed.exe and launching it: it ran, loaded the page and exited rc=0. The PyInstaller 6 bootloader locates its `_internal` contents directory relative to the executable's DIRECTORY, not its name. This makes the ASCII-build-then-rename fallback real, not theoretical.

15. --add-data uses ';' as the separator on Windows and bundled data lands INSIDE the contents directory: `--add-data "C:\...\assets;assets"` produced dist\<app>\_internal\assets\{app.ico,reader.css,reader.js,shell.html}. At runtime that is sys._MEIPASS + '/assets'.

16. The Chromium payload can be pruned by 138.4 MB with the app still rendering. Verified stepwise, launching the exe after each step: (a) resources\*.debug.pak + *.debug.bin (~77 MB, incl. a single 72.3 MB qtwebengine_devtools_resources.debug.pak) — still renders; (b) qtwebengine_locales trimmed from 53 to 3-4 .pak (~42 MB) — still renders; (c) qtwebengine_devtools_resources.pak (11.1 MB) — still renders; (d) Qt's own .qm translations trimmed to *_en/*_zh_CN (8.7 MB) — still renders. Final: 560.3 MB -> 421.9 MB across 2,778 files.

17. Size breakdown of the untouched bundle (measured): Qt6WebEngineCore.dll 195.3 MB, PySide6\resources 101.4 MB, PySide6\translations 52.6 MB, PySide6\qml 23.7 MB, opengl32sw.dll 19.7 MB, PySide6\plugins 6.4 MB, icudtl.dat 10.0 MB, python314.dll 6.5 MB.

18. build_exe.ps1 works end to end. Ran against the throwaway entry point on the real project root: toolchain probe OK, assets check emitted the expected warnings for missing reader.css/reader.js, PyInstaller finished in 240.1 s, rename to 'EPUB阅读器' succeeded, prune removed 200 files / 138.4 MB, final report printed dist path + 2.0 MB exe + 421.9 MB bundle, exit code 0. The produced exe then launched and rendered. Its fail-loud path was also exercised: with no entry point present it prints the candidate list and exits 1.

19. The generated assets\app.ico is a true multi-size icon: Pillow reports frames [(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)], 35,331 bytes. Pillow 12.3.0's ICO _save honours `append_images` and uses an exact size match verbatim instead of downscaling (read IcoImagePlugin.py line 57-92), so each size is the natively-rendered one. The embedded icon survives into the exe — System.Drawing.Icon.ExtractAssociatedIcon on the built exe returned a 32x32 icon.

20. Both PowerShell deliverables parse cleanly under the PowerShell AST parser and are saved UTF-8 WITH BOM (bytes 239,187,191), which is required for Windows PowerShell 5.1 to read the CJK string literals correctly rather than as ANSI mojibake.


## Pitfalls

1. MAX_PATH will silently waste 4 minutes. If the produced .exe path exceeds ~260 chars, PyInstaller completes the entire analysis and then dies at the very last step with `OSError: [WinError 122]` / `EndUpdateResourceW`. LongPathsEnabled=1 does not help because the Win32 UpdateResource family is not long-path aware. build_exe.ps1 now pre-checks this and refuses up front with a useful message. Never run the build from a deep temp/scratch path.

2. Windows PowerShell strips quote characters out of MULTI-LINE arguments passed to a native exe. `& $Python -c $multiLineString` arrives at Python with every `"` removed, producing a bogus SyntaxError. I hit this for real in build_exe.ps1's version probe; the fix is to write the snippet to a temp .py file and run that. This will bite anyone shelling out to python from PowerShell.

3. PyInstaller logs to STDERR. With `$ErrorActionPreference = 'Stop'` in a script, that can be turned into a terminating NativeCommandError. Set it to 'Continue' around the invocation and check `$LASTEXITCODE` yourself.

4. onefile is a trap for QtWebEngine. 12.7–14.0 s to first paint on EVERY launch, ~560 MB written to %TEMP% each time, and the temp tree leaks if the process is killed — I found a stale 112.5 MB `_MEI113842` on this machine dated 2026-09-13. Do not ship onefile for a double-click app.

5. Windows PowerShell 5.1 encodes native-command arguments using the system ANSI code page. `--name "EPUB阅读器"` works here only because the ACP happens to be 65001. On a 936/1252 machine the name would be mangled. Build with an ASCII --name and rename afterwards.

6. A .ps1 containing CJK literals MUST be saved UTF-8 with BOM. Windows PowerShell 5.1 reads BOM-less files as ANSI and the app name silently becomes mojibake. Most editors and most file-writing tools default to BOM-less UTF-8 — re-save explicitly.

7. Do NOT prune PySide6\qml (23.7 MB) or opengl32sw.dll (19.7 MB) to save space. QWebEngineView's render path is QtQuick-based, and opengl32sw.dll is the software-GL fallback for machines with no usable GPU driver. I did not test removing either; the 138 MB prune in the script is the part I verified.

8. `--clean` wipes PyInstaller's GLOBAL cache (%APPDATA%\pyinstaller), not just the work dir, and costs ~85 s per build (285 s vs 198 s). build_exe.ps1 deletes build/ and dist/ itself and only passes --clean behind an opt-in switch.

9. Bundled data lands INSIDE the contents directory (`_internal`), not next to the exe. App code must resolve assets via `sys._MEIPASS` when frozen — a naive `Path(__file__).parent / 'assets'` will work from source and break in the bundle.

10. You must ship the whole dist\EPUB阅读器\ folder. The 2 MB .exe alone does nothing. Any installer/zip step has to take the directory.

11. The .epub default-app choice cannot be scripted. HKCU\...\Explorer\FileExts\.epub\UserChoice is protected by a per-user hash on Windows 10/11; forging it is an anti-tamper bypass and Windows discards it. The association script registers the ProgId + OpenWithProgids so the app appears under "Open with", and the user makes it the default with one click.

12. One benign build-time warning is expected and can be ignored: `WARNING: QtLibraryInfo(PySide6): QML plugin binary '...qml\Qt\labs\assetdownloader\qmlassetdownloaderprivateplugin.dll' does not exist!` — a gap in the PySide6 6.11.1 wheel, not a packaging error.

13. Antivirus is the other classic cause of WinError 122 at the EndUpdateResourceW step (it holds a handle on the just-written .exe). If the path is short and it still fails, retry once before investigating.


## Recommendations

1. Ship ONEDIR. Evidence: 2.7 s first launch / ~1.0 s warm versus 12.7–14.0 s on every single onefile launch. `-OneFile` exists in the script but is documented as a bad idea for this app.

2. Name the entry point `main.py` at the project root. build_exe.ps1 auto-detects from main.py, app.py, epub_reader.py, src\main.py, src\app.py, epub_reader\__main__.py — and deliberately FAILS on ambiguity rather than guessing. Anything else needs `-Entry`.

3. In the app, resolve bundled assets through sys._MEIPASS (snippet provided). Register the custom URL scheme BEFORE constructing QApplication — that ordering is what the verified probe used and it works frozen.

4. Keep the post-build prune on (default). It is worth 138.4 MB and every step was verified by relaunching the exe. `-NoPrune` restores DevTools + all locales if remote debugging is ever needed.

5. Do not bother with a .spec file. Everything needed (`--windowed`, `--icon`, `--add-data`, `--exclude-module`, `--contents-directory`) is available on the CLI in 6.21.0, and the prune is a post-build step that I verified directly rather than relying on unverified TOC-filtering semantics inside a spec.

6. Expect ~4-5 minutes per build (285 s clean, 198–240 s incremental). Do not add `--clean` to routine builds.

7. Move the repo if it ever ends up on a deep path. Keep the produced exe path comfortably under 260 chars; C:\Users\mengz\epub-reader is fine (the built exe path is ~60 chars).

8. Run tools\make_icon.py again if the palette changes; build_exe.ps1 auto-invokes it when assets\app.ico is missing, so the icon is never a build blocker.

9. Run tools\install-file-association.ps1 -DryRun first to review the exact registry writes, then without the flag. Undo is `-Uninstall`, which also clears the UserChoice only if it points at our own ProgId.

10. If code signing is ever added, sign dist\EPUB阅读器\EPUB阅读器.exe AFTER the rename step, not before — renaming does not invalidate a signature, but signing the ASCII-named intermediate and then renaming leaves the OriginalFilename mismatched.


## Open questions

1. What is the entry point actually going to be called? build_exe.ps1 auto-detects main.py / app.py / epub_reader.py / src\main.py / src\app.py / epub_reader\__main__.py and fails loudly (listing all of them) if none or more than one exists. If the app agent settles on something else, either rename it or the default candidate list needs one line changed.

2. Should the shipped bundle keep remote DevTools? I prune qtwebengine_devtools_resources.pak (11.1 MB) by default and verified rendering is unaffected, but if you ever want to attach Chrome DevTools to the reader via --remote-debugging-port you need to build with -NoPrune.

3. Is 421.9 MB acceptable to ship, or does this need an installer/compression step? Qt6WebEngineCore.dll alone is 195.3 MB and cannot be reduced. A 7-Zip/NSIS self-extracting installer would cut the download but not the installed footprint. Nobody has said how this gets delivered to the user.

4. Code signing: unsigned, this will trip Windows SmartScreen on first run on any machine other than this one. Out of scope for me and it needs a certificate the user would have to supply.

5. opengl32sw.dll (19.7 MB) and PySide6\qml (23.7 MB) are the two remaining large removable-looking items, together 43 MB. I deliberately did NOT test removing them because QWebEngineView's render path is QtQuick-based and opengl32sw is the no-GPU fallback. If size becomes critical someone should test those two specifically, on a machine with and without a GPU driver.


## Verified code snippets


### The exact, verified build invocation build_exe.ps1 issues (onedir, windowed, icon, assets bundled). Shown with the project's real paths.

```powershell
& "C:\Users\mengz\AppData\Local\Programs\Python\Python314\python.exe" -m PyInstaller `
    --noconfirm `
    --onedir `
    --windowed `
    --name EPUBReader `
    --icon  C:\Users\mengz\epub-reader\assets\app.ico `
    --add-data "C:\Users\mengz\epub-reader\assets;assets" `
    --paths C:\Users\mengz\epub-reader `
    --exclude-module tkinter `
    --distpath C:\Users\mengz\epub-reader\dist `
    --workpath C:\Users\mengz\epub-reader\build `
    --specpath C:\Users\mengz\epub-reader\build `
    --log-level INFO `
    C:\Users\mengz\epub-reader\main.py

# then rename (Unicode-safe, no code page involved):
Rename-Item .\dist\EPUBReader\EPUBReader.exe 'EPUB阅读器.exe'
Rename-Item .\dist\EPUBReader 'EPUB阅读器'
```

### Asset path resolution the app MUST use — bundled data lives under sys._MEIPASS/assets in the frozen build, not beside the .py file. Verified against the real bundle layout dist\<app>\_internal\assets\.

```python
import sys
from pathlib import Path

def asset_dir() -> Path:
    """assets/ both from source and from the PyInstaller onedir bundle."""
    if getattr(sys, "frozen", False):
        # onedir: sys._MEIPASS is the `_internal` directory next to the .exe
        return Path(sys._MEIPASS) / "assets"
    return Path(__file__).resolve().parent / "assets"

READER_CSS = asset_dir() / "reader.css"
READER_JS  = asset_dir() / "reader.js"
```

### Custom URL scheme registration order that is verified to work in the frozen exe. registerScheme MUST happen before QApplication is constructed.

```python
from PySide6.QtWebEngineCore import QWebEngineUrlScheme, QWebEngineProfile
from PySide6.QtWidgets import QApplication

# --- module level, BEFORE QApplication(...) ---
_scheme = QWebEngineUrlScheme(b"epub")
_scheme.setSyntax(QWebEngineUrlScheme.Syntax.Host)
_scheme.setFlags(
    QWebEngineUrlScheme.Flag.SecureScheme
    | QWebEngineUrlScheme.Flag.LocalAccessAllowed
    | QWebEngineUrlScheme.Flag.ViewSourceAllowed
)
QWebEngineUrlScheme.registerScheme(_scheme)

app = QApplication(sys.argv)
# handler must outlive the profile - keep a reference on the window/app
handler = ZipSchemeHandler()
QWebEngineProfile.defaultProfile().installUrlSchemeHandler(b"epub", handler)
```

### The verified prune (what build_exe.ps1 does after COLLECT). 138.4 MB freed; the app was relaunched and still rendered after each step.

```powershell
$internal = "dist\EPUB阅读器\_internal\PySide6"

# (a) Chromium debug resources  ~77 MB  (one .pak alone is 72.3 MB)
Get-ChildItem "$internal\resources" -File |
    Where-Object { $_.Name -like '*.debug.*' } |
    ForEach-Object { [System.IO.File]::Delete($_.FullName) }

# (b) remote DevTools front-end  ~11 MB
[System.IO.File]::Delete("$internal\resources\qtwebengine_devtools_resources.pak")

# (c) Chromium UI locales: 53 -> 4  ~42 MB
$keep = @('en-US.pak','en-GB.pak','zh-CN.pak','zh-TW.pak')
Get-ChildItem "$internal\translations\qtwebengine_locales" -File |
    Where-Object { $keep -notcontains $_.Name } |
    ForEach-Object { [System.IO.File]::Delete($_.FullName) }

# (d) Qt's own .qm translations: keep en + zh_CN  ~9 MB
Get-ChildItem "$internal\translations" -File -Filter '*.qm' |
    Where-Object { $_.BaseName -notmatch '_(zh_CN|en)$' } |
    ForEach-Object { [System.IO.File]::Delete($_.FullName) }
```

### Non-interactive smoke test for the built exe — how I measured startup and proved the page actually rendered. Reuse this to regression-test real builds.

```powershell
$exe = "C:\Users\mengz\epub-reader\dist\EPUB阅读器\EPUB阅读器.exe"
$log = "$env:TEMP\epub_smoke.log"
if (Test-Path $log) { [IO.File]::Delete($log) }
$env:EPUB_SMOKE_LOG = $log
$ms = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
$env:EPUB_SMOKE_T0 = ($ms * 0.001).ToString("F3")   # note: NOT '/1000'
$t0 = Get-Date
$p  = Start-Process -FilePath $exe -PassThru
$ok = $p.WaitForExit(60000)
"exited=$ok rc=$($p.ExitCode) wall=$([math]::Round(((Get-Date)-$t0).TotalSeconds,3))s"
Get-Content $log
```

### PowerShell patterns build_exe.ps1 relies on that are non-obvious. Both were bugs I actually hit.

```powershell
# 1. PowerShell strips quotes from MULTI-LINE args to native exes.
#    `& $Python -c $multiLineSource` arrives mangled. Write a temp file instead:
$probeFile = Join-Path ([System.IO.Path]::GetTempPath()) ("probe_{0}.py" -f [guid]::NewGuid().ToString('N'))
[System.IO.File]::WriteAllText($probeFile, $probeSrc, (New-Object System.Text.UTF8Encoding($false)))
$out = & $Python $probeFile 2>&1 | Out-String
[System.IO.File]::Delete($probeFile)

# 2. PyInstaller logs to stderr; with ErrorActionPreference='Stop' that can throw.
$ErrorActionPreference = 'Continue'
& $Python @piArgs
$rc = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
if ($rc -ne 0) { throw "PyInstaller exited $rc" }

# 3. A .ps1 with CJK literals must be UTF-8 WITH BOM for PowerShell 5.1:
$txt = [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8)
[System.IO.File]::WriteAllText($path, $txt, (New-Object System.Text.UTF8Encoding($true)))
```

## Files written

- `C:\Users\mengz\epub-reader\build_exe.ps1`

- `C:\Users\mengz\epub-reader\tools\make_icon.py`

- `C:\Users\mengz\epub-reader\tools\install-file-association.ps1`

- `C:\Users\mengz\epub-reader\assets\app.ico`
