<#
================================================================================
 build_exe.ps1  --  package Book Reader as a double-clickable Windows app
================================================================================

 Produces a ONEDIR bundle:   dist\Book Reader\Book Reader.exe

 Why onedir and not onefile (measured on this machine, 2026-09-16):

     onedir   first launch 3.7 s   |  every later launch ~1.2 s
     onefile  first launch 12.7 s  |  every later launch  ~14 s

 A onefile QtWebEngine bundle re-inflates ~560 MB into %TEMP%\_MEIxxxxxx on
 EVERY launch -- there is no warm path, and a killed process leaves the temp
 tree behind. For an app the user double-clicks, onedir is ~10x faster and
 does not churn half a gigabyte of disk per run. Use -OneFile only if you
 explicitly want a single portable file and accept the startup cost.

 Usage
 -----
   .\build_exe.ps1                       # normal build
   .\build_exe.ps1 -Entry .\src\app.py   # explicit entry point
   .\build_exe.ps1 -CleanCache           # also wipe PyInstaller's global cache
   .\build_exe.ps1 -NoPrune              # keep Chromium debug/devtools payload
   .\build_exe.ps1 -Run                  # launch the exe when the build is done
   .\build_exe.ps1 -OneFile              # single .exe (slow start; see above)

================================================================================
#>

#Requires -Version 5.1
[CmdletBinding()]
param(
    # The Python 3.14 install that actually has PySide6 6.11 + PyInstaller 6.21.
    # NOT "python" on PATH -- that is a different 3.13 interpreter.
    [string] $Python = 'C:\Users\mengz\AppData\Local\Programs\Python\Python314\python.exe',

    # Entry-point script. Auto-detected from a short candidate list when omitted.
    [string] $Entry = '',

    # Final, user-visible name. The build itself runs under $BuildName (ASCII)
    # and the result is renamed; see "CJK naming" below.
    [string] $AppName = 'Book Reader',
    [string] $BuildName = 'BookReader',

    # Build beside an active installation without touching its directory.
    [ValidatePattern('^$|^-[A-Za-z0-9][A-Za-z0-9_-]*$')]
    [string] $OutputSuffix = '',

    [switch] $OneFile,
    [switch] $CleanCache,
    [switch] $NoPrune,
    [switch] $KeepAsciiName,
    [switch] $Run
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# --- console must be UTF-8 or the CJK name prints as mojibake -----------------
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }

$script:StartedAt = Get-Date

function Info  ($m) { Write-Host "  $m" }
function Step  ($m) { Write-Host ""; Write-Host "==> $m" -ForegroundColor Cyan }
function Note  ($m) { Write-Host "  ! $m" -ForegroundColor Yellow }
function Fail  ($m) {
    Write-Host ""
    Write-Host "BUILD FAILED" -ForegroundColor Red
    Write-Host "  $m" -ForegroundColor Red
    Write-Host ""
    exit 1
}
function MB ($bytes) { return [math]::Round($bytes / 1MB, 1) }
function TreeBytes ($path) {
    if (-not (Test-Path -LiteralPath $path)) { return 0 }
    $f = Get-ChildItem -LiteralPath $path -Recurse -File -ErrorAction SilentlyContinue
    if (-not $f) { return 0 }
    return ($f | Measure-Object -Property Length -Sum).Sum
}

# =============================================================================
# 0. Paths
# =============================================================================
$Root     = Split-Path -Parent $MyInvocation.MyCommand.Definition
$DistDir  = Join-Path $Root ('dist' + $OutputSuffix)
$WorkDir  = Join-Path $Root ('build' + $OutputSuffix)
$AssetDir = Join-Path $Root 'assets'
$IcoPath  = Join-Path $AssetDir 'app.ico'
$IconGen  = Join-Path $Root 'tools\make_icon.py'

Write-Host ""
Write-Host "EPUB reader -- Windows packaging" -ForegroundColor White
Info "project root : $Root"

# MAX_PATH guard. PyInstaller stamps the icon into the bootloader via
# BeginUpdateResourceW/EndUpdateResourceW, and those Win32 calls are NOT
# long-path aware: if the produced .exe path is over ~260 chars the build dies
# with `OSError: [WinError 122]` from EndUpdateResourceW, even when
# HKLM\...\FileSystem\LongPathsEnabled is 1. Verified on this machine.
$probe = Join-Path $WorkDir "$BuildName\$BuildName.exe"
if ($probe.Length -ge 250) {
    Fail ("Project path is too deep: the build would write`n" +
          "    $probe`n" +
          "  ($($probe.Length) chars). PyInstaller's Win32 resource update is not long-path`n" +
          "  aware and fails with WinError 122 past ~260 chars.`n" +
          "  Move the project somewhere shorter, e.g. C:\src\book-reader.")
}

# =============================================================================
# 1. Toolchain
# =============================================================================
Step "Checking toolchain"

if (-not (Test-Path -LiteralPath $Python)) {
    Fail ("Python interpreter not found:`n    $Python`n" +
          "  Pass a different one with -Python <path>. Do not use `"python`" from PATH;`n" +
          "  on this machine that resolves to a 3.13 install without PySide6.")
}

$probeSrc = @'
import sys, json
info = {"py": "%d.%d.%d" % sys.version_info[:3]}
try:
    import PyInstaller; info["pyinstaller"] = PyInstaller.__version__
except Exception as e: info["pyinstaller_error"] = repr(e)
try:
    import PySide6; info["pyside6"] = PySide6.__version__
except Exception as e: info["pyside6_error"] = repr(e)
try:
    import PySide6.QtWebEngineWidgets; info["webengine"] = True
except Exception as e: info["webengine_error"] = repr(e)
try:
    import PIL; info["pillow"] = PIL.__version__
except Exception as e: info["pillow_error"] = repr(e)
print(json.dumps(info))
'@

# Windows PowerShell strips quote characters out of multi-line arguments handed
# to a native exe, so `python -c $probeSrc` would arrive mangled. Write the
# probe to a file instead.
$probeFile = Join-Path ([System.IO.Path]::GetTempPath()) ("epubreader_probe_{0}.py" -f [guid]::NewGuid().ToString('N'))
[System.IO.File]::WriteAllText($probeFile, $probeSrc, (New-Object System.Text.UTF8Encoding($false)))

$ErrorActionPreference = 'Continue'
$probeOut = & $Python $probeFile 2>&1 | Out-String
$probeRc = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
[System.IO.File]::Delete($probeFile)

if ($probeRc -ne 0) { Fail "Could not run the interpreter:`n$probeOut" }

try { $tool = $probeOut.Trim() | ConvertFrom-Json } catch { Fail "Unexpected probe output:`n$probeOut" }

foreach ($k in @('pyinstaller', 'pyside6', 'webengine')) {
    if (-not ($tool.PSObject.Properties.Name -contains $k)) {
        $err = $probeOut.Trim()
        Fail "The chosen interpreter cannot build this app (missing: $k).`n  Probe said: $err"
    }
}
Info "python       : $($tool.py)   ($Python)"
Info "PyInstaller  : $($tool.pyinstaller)"
Info "PySide6      : $($tool.pyside6)   QtWebEngine: ok"

# =============================================================================
# 2. Entry point -- fail loudly and usefully
# =============================================================================
Step "Locating entry point"

$candidates = @(
    'main.py',
    'app.py',
    'epub_reader.py',
    'src\main.py',
    'src\app.py',
    'epub_reader\__main__.py'
)

if ($Entry) {
    if (-not [System.IO.Path]::IsPathRooted($Entry)) { $Entry = Join-Path $Root $Entry }
    if (-not (Test-Path -LiteralPath $Entry -PathType Leaf)) {
        Fail "-Entry was given as`n    $Entry`n  but there is no such file."
    }
    $EntryPath = (Resolve-Path -LiteralPath $Entry).ProviderPath
}
else {
    $found = @()
    foreach ($c in $candidates) {
        $p = Join-Path $Root $c
        if (Test-Path -LiteralPath $p -PathType Leaf) { $found += $p }
    }
    if ($found.Count -eq 0) {
        $list = ($candidates | ForEach-Object { "      $_" }) -join "`n"
        Fail ("No application entry point found under`n    $Root`n" +
              "  Looked for:`n$list`n" +
              "  Write the app first, or point at it explicitly:`n" +
              "      .\build_exe.ps1 -Entry .\path\to\your_main.py")
    }
    if ($found.Count -gt 1) {
        $list = ($found | ForEach-Object { "      $_" }) -join "`n"
        Fail ("Ambiguous entry point -- several candidates exist:`n$list`n" +
              "  Pick one explicitly:  .\build_exe.ps1 -Entry .\main.py")
    }
    $EntryPath = $found[0]
}
Info "entry        : $EntryPath"

# =============================================================================
# 3. Assets + icon
# =============================================================================
Step "Checking assets"

# The DjVu decoder is C# (djvutool\*.cs), compiled with the .NET Framework compiler
# that ships with Windows into assets\djvutool.exe so it is bundled with the app.
# djvu.tool_path() rebuilds it only when a source file is newer.
Push-Location $Root
try {
    $djvuTool = (& $Python -c "import djvu; print(djvu.tool_path())" 2>&1 | Select-Object -Last 1)
} finally { Pop-Location }
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath (Join-Path $AssetDir 'djvutool.exe'))) {
    Fail ("Could not build the DjVu decoder (assets\djvutool.exe):`n    $djvuTool")
}
Info "djvu decoder : $djvuTool"

if (-not (Test-Path -LiteralPath $AssetDir -PathType Container)) {
    Fail ("Missing assets folder:`n    $AssetDir`n" +
          "  The reader needs reader.css / reader.js (and any HTML shell) bundled`n" +
          "  as data. Create the folder before building.")
}
$assetFiles = @(Get-ChildItem -LiteralPath $AssetDir -Recurse -File)
if ($assetFiles.Count -eq 0) { Fail "The assets folder exists but is empty: $AssetDir" }
Info "assets       : $($assetFiles.Count) file(s), $(MB (TreeBytes $AssetDir)) MB"

foreach ($want in @('reader.css', 'reader.js')) {
    if (-not (Test-Path -LiteralPath (Join-Path $AssetDir $want))) {
        Note "assets\$want is missing -- bundling anyway, but the reader will not theme correctly."
    }
}

if (-not (Test-Path -LiteralPath $IcoPath)) {
    if (Test-Path -LiteralPath $IconGen) {
        Info "assets\app.ico missing -- generating it with tools\make_icon.py"
        $ErrorActionPreference = 'Continue'
        & $Python $IconGen | Out-Null
        $icoRc = $LASTEXITCODE
        $ErrorActionPreference = 'Stop'
        if ($icoRc -ne 0 -or -not (Test-Path -LiteralPath $IcoPath)) {
            Fail "tools\make_icon.py did not produce $IcoPath"
        }
    }
    else {
        Fail ("No icon at`n    $IcoPath`n  and no tools\make_icon.py to generate one.")
    }
}
Info "icon         : $IcoPath"

# =============================================================================
# 4. Clean -- safely
# =============================================================================
Step "Cleaning previous build output"

function Remove-Tree ([string] $Path, [string] $Label) {
    if (-not (Test-Path -LiteralPath $Path)) { Info "$Label : nothing to clean"; return }
    $full = (Resolve-Path -LiteralPath $Path).ProviderPath.TrimEnd('\')
    $rootFull = (Resolve-Path -LiteralPath $Root).ProviderPath.TrimEnd('\')
    # Never delete anything that is not strictly inside the project root.
    if ($full.Length -le $rootFull.Length -or
        -not $full.StartsWith($rootFull + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        Fail "Refusing to delete '$full' -- it is not inside '$rootFull'."
    }
    try {
        Remove-Item -LiteralPath $full -Recurse -Force -ErrorAction Stop
        Info "$Label : removed"
    }
    catch {
        Fail ("Could not remove`n    $full`n  $($_.Exception.Message)`n" +
              "  The most common cause is that a previously built $AppName.exe is still`n" +
              "  running, or a file there is open in another program. Close it and retry.")
    }
}

Remove-Tree $DistDir 'dist'
Remove-Tree $WorkDir 'build'
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null

# =============================================================================
# 5. Build
# =============================================================================
Step "Running PyInstaller (this takes ~3-5 minutes for QtWebEngine)"

# CJK naming:
#   Building under an ASCII --name and renaming afterwards is deliberate.
#   Passing "Book Reader" straight to --name DOES work on this machine (the
#   system ANSI code page is UTF-8, verified: a direct CJK build produced a
#   working Book Reader.exe), but Windows PowerShell 5.1 encodes native-command
#   arguments with the ANSI code page, so on a machine where that is 936/1252
#   the name would be mangled. Renaming afterwards goes through .NET and is
#   always Unicode-clean. The PyInstaller onedir bootloader locates its
#   `_internal` folder relative to the executable's directory, not its name,
#   so renaming the exe (and/or its folder) is safe -- verified by launching a
#   renamed build.
$mode = '--onedir'
if ($OneFile) {
    $mode = '--onefile'
    Note "onefile requested: expect ~13 s startup on EVERY launch (see header)."
}

$piArgs = @(
    '-m', 'PyInstaller',
    '--noconfirm',
    $mode,
    '--windowed',                       # no console window
    '--name', $BuildName,
    '--icon', $IcoPath,
    '--add-data', "$AssetDir;assets",   # -> sys._MEIPASS\assets at runtime
    '--paths', $Root,
    '--exclude-module', 'tkinter',      # not installed on this 3.14 anyway
    '--distpath', $DistDir,
    '--workpath', $WorkDir,
    '--specpath', $WorkDir,             # keep the generated .spec out of the repo
    '--log-level', 'INFO'
)
if ($CleanCache) { $piArgs += '--clean' }
$piArgs += $EntryPath

Info "$Python $($piArgs -join ' ')"
Write-Host ""

$buildStart = Get-Date
$ErrorActionPreference = 'Continue'    # PyInstaller logs to stderr; that is not an error
# Resolve dependencies from this Python and Windows, not unrelated tools on PATH.
# Poppler/Conda ship an incompatible icuuc.dll with suffixed exports; Qt uses
# Windows' native ICU. Bundling the former produces a QtCore import failure.
$buildPathBefore = $env:PATH
$env:PATH = @((Split-Path -Parent $Python), (Join-Path $env:SystemRoot 'System32'), $env:SystemRoot) -join ';'
try {
    & $Python @piArgs
    $rc = $LASTEXITCODE
} finally {
    $env:PATH = $buildPathBefore
}
$ErrorActionPreference = 'Stop'
$buildSecs = [math]::Round(((Get-Date) - $buildStart).TotalSeconds, 1)

if ($rc -ne 0) {
    Fail ("PyInstaller exited with code $rc after $buildSecs s.`n" +
          "  Scroll up for the traceback. Two failures seen on Windows:`n" +
          "    * OSError [WinError 122] from EndUpdateResourceW -> output path over MAX_PATH,`n" +
          "      or an antivirus holding the freshly written .exe. Retry / shorten the path.`n" +
          "    * ModuleNotFoundError at build time -> add --hidden-import for it.")
}
Info "PyInstaller finished in $buildSecs s"

# =============================================================================
# 6. Rename to the CJK name
# =============================================================================
Step "Naming the result"

if ($OneFile) {
    $builtExe = Join-Path $DistDir "$BuildName.exe"
    if (-not (Test-Path -LiteralPath $builtExe)) { Fail "Expected $builtExe but it is not there." }
    $finalExe = Join-Path $DistDir "$AppName.exe"
    $finalDir = $DistDir
}
else {
    $builtDir = Join-Path $DistDir $BuildName
    $builtExe = Join-Path $builtDir "$BuildName.exe"
    if (-not (Test-Path -LiteralPath $builtExe)) { Fail "Expected $builtExe but it is not there." }
    $finalDir = Join-Path $DistDir $AppName
    $finalExe = Join-Path $finalDir "$AppName.exe"
}

if ($KeepAsciiName) {
    Info "keeping ASCII name '$BuildName' (-KeepAsciiName)"
    $finalExe = $builtExe
    if (-not $OneFile) { $finalDir = $builtDir }
}
else {
    Rename-Item -LiteralPath $builtExe -NewName "$AppName.exe" -Force
    if (-not $OneFile) {
        Rename-Item -LiteralPath $builtDir -NewName $AppName -Force
    }
    Info "renamed to '$AppName'"
}

# =============================================================================
# 7. Prune Chromium payload that is never loaded at runtime
# =============================================================================
if (-not $OneFile) {
    & $Python (Join-Path $Root 'tools\prune_incompatible_icu.py') $finalDir
    if ($LASTEXITCODE -ne 0) { Fail 'The frozen Qt/ICU dependency check failed.' }
}

if (-not $NoPrune -and -not $OneFile) {
    Step "Pruning unused Chromium payload"

    $internal = Join-Path $finalDir '_internal\PySide6'
    if (-not (Test-Path -LiteralPath $internal)) {
        Note "no _internal\PySide6 to prune (unexpected) -- skipping"
    }
    else {
        $sizeBefore = TreeBytes $finalDir
        $removed = 0

        # (a) Chromium *debug* resources. ~77 MB, only read by a debug build.
        $res = Join-Path $internal 'resources'
        if (Test-Path -LiteralPath $res) {
            Get-ChildItem -LiteralPath $res -File |
                Where-Object { $_.Name -like '*.debug.*' } |
                ForEach-Object { [System.IO.File]::Delete($_.FullName); $removed++ }

            # (b) Remote DevTools front-end. ~11 MB. Page rendering does not use it.
            $devtools = Join-Path $res 'qtwebengine_devtools_resources.pak'
            if (Test-Path -LiteralPath $devtools) {
                [System.IO.File]::Delete($devtools); $removed++
            }
        }

        # (c) Chromium UI locales: keep English + Chinese. ~42 MB.
        $keepLocales = @('en-US.pak', 'en-GB.pak', 'zh-CN.pak', 'zh-TW.pak')
        $locales = Join-Path $internal 'translations\qtwebengine_locales'
        if (Test-Path -LiteralPath $locales) {
            Get-ChildItem -LiteralPath $locales -File |
                Where-Object { $keepLocales -notcontains $_.Name } |
                ForEach-Object { [System.IO.File]::Delete($_.FullName); $removed++ }
        }

        # (d) Qt's own .qm translations: keep en + zh_CN. ~9 MB.
        $tr = Join-Path $internal 'translations'
        if (Test-Path -LiteralPath $tr) {
            Get-ChildItem -LiteralPath $tr -File -Filter '*.qm' |
                Where-Object { $_.BaseName -notmatch '_(zh_CN|en)$' } |
                ForEach-Object { [System.IO.File]::Delete($_.FullName); $removed++ }
        }

        $saved = $sizeBefore - (TreeBytes $finalDir)
        Info "removed $removed file(s), freed $(MB $saved) MB"
        Info "(use -NoPrune to keep DevTools, debug .pak files and all locales)"
    }
}

# =============================================================================
# 8. Report
# =============================================================================
Step "Done"

if (-not (Test-Path -LiteralPath $finalExe)) { Fail "Expected executable is missing: $finalExe" }

$exeBytes   = (Get-Item -LiteralPath $finalExe).Length
$totalBytes = TreeBytes $finalDir
$fileCount  = @(Get-ChildItem -LiteralPath $finalDir -Recurse -File).Count
$elapsed    = [math]::Round(((Get-Date) - $script:StartedAt).TotalSeconds, 1)

Write-Host ""
Write-Host "  dist path   : $finalDir" -ForegroundColor Green
Write-Host "  executable  : $finalExe" -ForegroundColor Green
Write-Host "  exe size    : $(MB $exeBytes) MB ($('{0:N0}' -f $exeBytes) bytes)"
Write-Host "  bundle size : $(MB $totalBytes) MB across $('{0:N0}' -f $fileCount) files"
Write-Host "  total time  : $elapsed s"
Write-Host ""
Write-Host "  Ship the whole '$(Split-Path -Leaf $finalDir)' folder -- the .exe alone will not run." -ForegroundColor Yellow
Write-Host "  To associate .epub with it:  .\tools\install-file-association.ps1"
Write-Host ""

if ($Run) {
    Step "Launching"
    Start-Process -FilePath $finalExe
}
