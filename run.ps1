<#
.SYNOPSIS
    Launch EPUB Reader from source.

.DESCRIPTION
    Uses Python 3.14 (NOT the 3.13 on PATH, which has no PySide6) and passes every
    argument straight through to epub_reader.py, so a book path opens that book.
    Uses pythonw.exe when available so no console window lingers behind the app.

.EXAMPLE
    .\run.ps1

.EXAMPLE
    .\run.ps1 "C:\Books\some book.epub"

.EXAMPLE
    .\run.ps1 --help
#>

$ErrorActionPreference = 'Stop'

$pyDir  = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python314'
$python = Join-Path $pyDir 'python.exe'
$pythonw = Join-Path $pyDir 'pythonw.exe'
$entry  = Join-Path $PSScriptRoot 'epub_reader.py'

if (-not (Test-Path -LiteralPath $python)) {
    Write-Error "Python 3.14 not found at $python"
    exit 2
}
if (-not (Test-Path -LiteralPath $entry)) {
    Write-Error "Entry point not found: $entry"
    exit 2
}

# --help prints to the console, so it needs python.exe; everything else runs windowed.
$wantsConsole = $args | Where-Object { $_ -in @('-h', '--help') }
$exe = if (-not $wantsConsole -and (Test-Path -LiteralPath $pythonw)) { $pythonw } else { $python }

& $exe $entry @args
exit $LASTEXITCODE
