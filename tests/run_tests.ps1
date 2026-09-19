<#
.SYNOPSIS
    Run the book-reader test suite with the correct interpreter and PYTHONPATH.

.DESCRIPTION
    Uses Python 3.14 (NOT the 3.13 on PATH), puts the project root on
    PYTHONPATH so `import epublib` resolves, regenerates the fixture corpus if
    it is missing, runs stdlib unittest discovery over tests\, and prints a
    clean pass/fail summary.

    Exit codes:  0 = all green, 1 = failures/errors, 2 = could not run.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File C:\Users\mengz\book-reader\tests\run_tests.ps1

.EXAMPLE
    .\run_tests.ps1 -Regenerate -Detailed

.EXAMPLE
    .\run_tests.ps1 -Filter OddPaths
#>

[CmdletBinding()]
param(
    # The exact interpreter the project targets. "python" on PATH is a different 3.13.
    [string] $Python = "$env:LOCALAPPDATA\Programs\Python\Python314\python.exe",

    # unittest discovery pattern.
    [string] $Pattern = "test_*.py",

    # Substring filter passed to unittest -k (e.g. "OddPaths", "toc", "drm").
    [string] $Filter = "",

    # Extra directory to prepend to PYTHONPATH, e.g. to test a candidate
    # epublib.py that does not live in the project root yet.
    [string] $ExtraPythonPath = "",

    # Rebuild the fixture corpus from scratch and verify it before testing.
    [switch] $Regenerate,

    # Per-test output instead of dots.
    [switch] $Detailed,

    # Only print the summary block.
    [switch] $Quiet
)

$ErrorActionPreference = "Stop"

$TestsDir    = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $TestsDir
$FixturesDir = Join-Path $TestsDir "fixtures"
$OutDir      = Join-Path $TestsDir "out"

function Write-Line([string] $Text, [string] $Colour = "Gray") {
    if (-not $Quiet) { Write-Host $Text -ForegroundColor $Colour }
}

function Write-Rule([string] $Title) {
    Write-Host ""
    Write-Host ("=" * 68) -ForegroundColor DarkGray
    if ($Title) { Write-Host "  $Title" -ForegroundColor Cyan }
    Write-Host ("=" * 68) -ForegroundColor DarkGray
}

# --- preflight -------------------------------------------------------------

if (-not (Test-Path $Python)) {
    Write-Host "Interpreter not found: $Python" -ForegroundColor Red
    Write-Host "Pass -Python <path> to override." -ForegroundColor Red
    exit 2
}

$versionLine = & $Python -c "import sys; print('%d.%d.%d' % sys.version_info[:3])"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Could not run $Python" -ForegroundColor Red
    exit 2
}
if (-not $versionLine.StartsWith("3.14")) {
    Write-Host "WARNING: expected Python 3.14.x, got $versionLine" -ForegroundColor Yellow
}

if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir | Out-Null }

# --- fixtures --------------------------------------------------------------

$maker = Join-Path $TestsDir "make_fixtures.py"
$needFixtures = $Regenerate -or -not (Test-Path (Join-Path $FixturesDir "epub2_ncx.epub"))

if ($ExtraPythonPath) {
    $env:PYTHONPATH = "$ExtraPythonPath;$ProjectRoot"
} else {
    $env:PYTHONPATH = $ProjectRoot
}
$env:PYTHONIOENCODING = "utf-8"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

if ($needFixtures) {
    Write-Rule "Building fixtures"
    if ($Regenerate) {
        & $Python $maker --clean --verify
    } else {
        & $Python $maker --verify
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Fixture generation FAILED" -ForegroundColor Red
        exit 2
    }
}

$fixtureFiles = @(Get-ChildItem -Path $FixturesDir -Filter *.epub -ErrorAction SilentlyContinue)
$fixtureBytes = ($fixtureFiles | Measure-Object -Property Length -Sum).Sum
if (-not $fixtureBytes) { $fixtureBytes = 0 }

# --- run -------------------------------------------------------------------

$unittestArgs = @("-m", "unittest", "discover", "-s", $TestsDir, "-p", $Pattern)
if ($Detailed) { $unittestArgs += "-v" }
if ($Filter)   { $unittestArgs += @("-k", $Filter) }

$stdoutFile = Join-Path $env:TEMP ("epub-tests-out-{0}.txt" -f $PID)
$stderrFile = Join-Path $env:TEMP ("epub-tests-err-{0}.txt" -f $PID)

Write-Rule "Running tests"
Write-Line ("  {0} {1}" -f $Python, ($unittestArgs -join " ")) "DarkGray"

$sw = [System.Diagnostics.Stopwatch]::StartNew()
$proc = Start-Process -FilePath $Python -ArgumentList $unittestArgs `
        -WorkingDirectory $ProjectRoot -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput $stdoutFile -RedirectStandardError $stderrFile
$sw.Stop()

$stdoutText = ""
$stderrText = ""
if (Test-Path $stdoutFile) { $stdoutText = [System.IO.File]::ReadAllText($stdoutFile, [System.Text.Encoding]::UTF8) }
if (Test-Path $stderrFile) { $stderrText = [System.IO.File]::ReadAllText($stderrFile, [System.Text.Encoding]::UTF8) }
Remove-Item $stdoutFile, $stderrFile -ErrorAction SilentlyContinue

$all = ($stdoutText + "`n" + $stderrText)
$lines = $all -split "`r?`n"

# --- parse -----------------------------------------------------------------

$ran = 0; $took = ""
$m = [regex]::Match($all, "Ran (\d+) tests? in ([\d.]+)s")
if ($m.Success) { $ran = [int]$m.Groups[1].Value; $took = $m.Groups[2].Value }

$failCount = 0; $errCount = 0; $skipCount = 0
$m = [regex]::Match($all, "failures=(\d+)");  if ($m.Success) { $failCount = [int]$m.Groups[1].Value }
$m = [regex]::Match($all, "errors=(\d+)");    if ($m.Success) { $errCount  = [int]$m.Groups[1].Value }
$m = [regex]::Match($all, "skipped=(\d+)");   if ($m.Success) { $skipCount = [int]$m.Groups[1].Value }

$badNames = @()
foreach ($line in $lines) {
    if ($line -match "^(FAIL|ERROR): (\S+)") { $badNames += $matches[2] }
}
$badNames = $badNames | Select-Object -Unique

$epublibMissing = $all -match "epublib is not importable yet"
$passed = $ran - $failCount - $errCount - $skipCount

# --- report ----------------------------------------------------------------

if (-not $Quiet) {
    $body = ($all.Trim())
    if ($body) { Write-Host $body }
}

Write-Rule "Summary"
Write-Host ("  interpreter  : {0}  (Python {1})" -f $Python, $versionLine)
Write-Host ("  PYTHONPATH   : {0}" -f $env:PYTHONPATH)
Write-Host ("  fixtures     : {0} files, {1:N0} bytes" -f $fixtureFiles.Count, $fixtureBytes)
Write-Host ("  tests run    : {0}  ({1}s inside unittest, {2:N1}s wall)" -f $ran, $took, $sw.Elapsed.TotalSeconds)
Write-Host ("  passed       : {0}" -f $passed) -ForegroundColor Green
if ($failCount) { Write-Host ("  failed       : {0}" -f $failCount) -ForegroundColor Red }
if ($errCount)  { Write-Host ("  errors       : {0}" -f $errCount)  -ForegroundColor Red }
if ($skipCount) { Write-Host ("  skipped      : {0}" -f $skipCount) -ForegroundColor Yellow }

if ($epublibMissing) {
    Write-Host ""
    Write-Host "  NOTE: epublib is not importable yet, so every test that needs it" -ForegroundColor Yellow
    Write-Host "        fails by design. Drop epublib.py into $ProjectRoot and rerun." -ForegroundColor Yellow
    Write-Host "        The passing tests above are the fixture-corpus canaries." -ForegroundColor Yellow
} elseif ($badNames.Count) {
    Write-Host ""
    Write-Host "  failing tests:" -ForegroundColor Red
    foreach ($n in ($badNames | Select-Object -First 20)) {
        Write-Host ("    - {0}" -f $n) -ForegroundColor Red
    }
    if ($badNames.Count -gt 20) {
        Write-Host ("    ... and {0} more" -f ($badNames.Count - 20)) -ForegroundColor Red
    }
}

Write-Host ""
if ($ran -eq 0) {
    Write-Host "  RESULT: NO TESTS RAN" -ForegroundColor Red
    exit 2
} elseif ($proc.ExitCode -eq 0) {
    Write-Host "  RESULT: PASS" -ForegroundColor Green
    exit 0
} else {
    Write-Host "  RESULT: FAIL" -ForegroundColor Red
    exit 1
}
