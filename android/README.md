# Book Reader for Android

The same reader, on a phone: it reads EPUB / MOBI / AZW / AZW3 in a WebView running
the desktop's `reader.js` engine, and it converts books to LaTeX + PDF (and DjVu
straight to PDF) **entirely offline**, with a TeX engine carried inside the APK.

The conversion itself is not a rewrite. `epublib.py`, `mobi.py`, `bookformats.py`,
`store.py`, `djvu.py`, `djvupdf.py` and `latexexport.py` are the desktop modules,
copied into the build by the `copySharedPython` Gradle task and run on the device by
[Chaquopy](https://chaquo.com/chaquopy/) (Python 3.13). Editing them in the project
root changes both apps.

## What is in the APK

| Piece | What it is |
|---|---|
| `libbookreadertex.so` | [Tectonic](https://github.com/tectonic-typesetting/tectonic) — XeTeX + xdvipdfmx — built for `arm64-v8a` and `x86_64`, ~45 MB each, loaded as a **JNI library** (never exec'd, so Android's rules about running packaged binaries do not apply) |
| `assets/texbundle/` | ~93 MB of LaTeX packages and fonts as a Tectonic *directory bundle*; nothing is downloaded, ever |
| Python 3.13 + lxml + Pillow | Chaquopy, for both ABIs |
| `com.bookreader.djvu` | The DjVu decoder, ported from the desktop's C# |

Fonts come from the TeX bundle, since a phone has no Windows font library: Fandol
(Simplified Chinese), arphic (Traditional), Harano Aji (Japanese), baekmuk (Korean),
TeX Gyre Termes (Greek/Cyrillic), Latin Modern otherwise. The converter picks them
through `ExportOptions.fonts = "texlive"`.

## Building

Native work happens on a Linux host (WSL Ubuntu here); the APK is assembled on Windows.

```bash
# once: Rust targets, cargo-ndk, the NDK, and the C stack per ABI
rustup target add aarch64-linux-android x86_64-linux-android
cargo install cargo-ndk tectonic          # tectonic itself is only needed to build the bundle
export ANDROID_NDK_HOME=~/androidtex/android-ndk-r28c
cd ~/vcpkg && ./vcpkg install --triplet x64-android \
    "harfbuzz[core,freetype,graphite2,icu,png]" freetype graphite2 icu libpng fontconfig
cd ~/vcpkg && ./vcpkg install --triplet arm64-android \
    "harfbuzz[core,freetype,graphite2,icu,png]" freetype graphite2 icu libpng fontconfig

# the engine (writes app/src/main/jniLibs/<abi>/)
./native/build-native.sh                  # both ABIs; pass one to build just that

# the offline LaTeX bundle (writes app/src/main/assets/texbundle/)
./native/make-bundle.sh
```

```powershell
# the APK
$env:JAVA_HOME = "C:/Program Files/Microsoft/jdk-17.0.20.8-hotspot"   # Windows-style path
.\gradlew.bat :app:assembleDebug
```

`native/build-native.sh` follows the recipe proved by
[TeXslate](https://github.com/thobgg/TeXslate), including the fix for a Tectonic bug
that only bites on Android (closing an output twice panics across a C boundary, which
aborts the whole app). It also refuses to build if an older upstream bug comes back.

`native/make-bundle.sh` fills the offline bundle by compiling the documents in
`native/probes/`, which are real converter output. **When the converter starts emitting
a package the probes do not use, add a probe and rebuild the bundle** — otherwise that
package is simply missing on the device.

## Testing on the emulator

```powershell
$sdk = "$env:LOCALAPPDATA\Android\Sdk"
& "$sdk\cmdline-tools\latest\bin\avdmanager.bat" create avd -n BookReader35 `
    -k "system-images;android-35;google_apis;x86_64" -d pixel_7
# then raise hw.ramSize to 4096 and disk.dataPartition.size to 12G in the AVD's config.ini
& "$sdk\emulator\emulator.exe" -avd BookReader35 -no-snapshot-load -gpu swiftshader_indirect
& "$sdk\platform-tools\adb.exe" install -r app\build\outputs\apk\debug\app-debug.apk
```

The harness screen has buttons for the two paths, plus a **Read a book** button that
opens a book from `files/books/` in the reader (tap the screen edges to turn pages,
the centre to bring up the toolbar; the drawer is the table of contents).  From the
command line:

```powershell
$adb = "$env:LOCALAPPDATA\Android\Sdk\platform-tools\adb.exe"
& $adb shell am start -n com.bookreader/.MainActivity --es autorun typeset     # the engine alone
& $adb shell am start -n com.bookreader/.MainActivity --es autorun convert     # a sample book
& $adb shell am start -n com.bookreader/.MainActivity --es autorun /storage/emulated/0/Android/data/com.bookreader/files/books/some.azw3
& $adb shell am start -n com.bookreader/.MainActivity --es autorun read --es file /data/user/0/com.bookreader/files/books/some.epub
& $adb logcat -s BookReader:I
```

Measured on the emulator (x86_64, API 35): sample EPUB 7.4 s; *Alice* (MOBI) 101 pages
in 6.3 s; 紅樓夢 (AZW3) **1,149 pages in 3.5 minutes**, output matching the desktop.

## Gotchas worth keeping

- `JAVA_HOME` must be a Windows path when Gradle runs from Git Bash. Setting
  `MSYS_NO_PATHCONV=1` (handy for `adb` paths) stops the automatic conversion and the
  build then fails with "JAVA_HOME is set to an invalid directory".
- Pull files with `adb pull` or `adb exec-out`; `adb shell cat` mangles binaries.
- Chaquopy needs AGP ≤ 9.2 and a host Python matching the target version (3.13).
