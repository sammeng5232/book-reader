<#
================================================================================
 install-file-association.ps1  --  make .epub open with EPUB Reader
================================================================================

 SCOPE: CURRENT USER ONLY. Every key this script touches lives under HKCU.
 It never writes HKLM, never touches Windows security, UAC, SmartScreen,
 Defender, policy or any system setting, and never needs administrator rights.

 WHAT IT CREATES  (all under HKEY_CURRENT_USER)

   Software\Classes\EPUBReader.Epub.1                     <- the ProgId
       (default)                  = "EPUB 电子书"
       FriendlyTypeName           = "EPUB 电子书"
       DefaultIcon                = "<exe>,0"
       shell\open\(default)       = "Open with EPUB Reader" (in the Windows display language)
       shell\open\command         = "<exe>" "%1"

   Software\Classes\.epub
       OpenWithProgids\EPUBReader.Epub.1 = ""             <- puts us in "Open with"
       (the .epub key itself is created only if it does not already exist;
        an existing (default) value is left untouched)

   Software\Classes\Applications\EPUB Reader.exe          <- friendly name in the
       FriendlyAppName            = "EPUB Reader"            "Open with" picker
       shell\open\command         = "<exe>" "%1"
       SupportedTypes\.epub       = ""

   Software\EPUBReader\Capabilities                     <- Settings > Default apps
       ApplicationName / ApplicationDescription
       FileAssociations\.epub     = "EPUBReader.Epub.1"
   Software\RegisteredApplications
       EPUBReader                 = "Software\EPUBReader\Capabilities"

   Software\EPUBReader\Install                          <- bookkeeping so the
       ExePath, InstalledAt, CreatedExtKey                 uninstall is precise

 Then it calls SHChangeNotify(SHCNE_ASSOCCHANGED) so Explorer picks the change
 up immediately instead of after a sign-out.

 WHAT IT DELIBERATELY DOES NOT DO

   It does not write HKCU\...\Explorer\FileExts\.epub\UserChoice. On Windows 10
   and 11 that value is protected by a per-user, per-extension hash; forging it
   is an anti-tamper bypass, and Windows silently discards or resets a forged
   value anyway. So this script makes EPUB Reader *available* and the first
   double-click (or right-click > Open with > Choose another app > Always) makes
   it the default. That one click is the user's, as it should be.

 HOW TO UNDO -- fully reversible, same script:

     .\install-file-association.ps1 -Uninstall

   That removes exactly the keys listed above (and, only if it points at our own
   ProgId, the UserChoice the user picked). It leaves a pre-existing .epub
   association belonging to some other reader alone.

   To preview without changing anything:

     .\install-file-association.ps1 -DryRun
     .\install-file-association.ps1 -Uninstall -DryRun

   To inspect by hand afterwards:

     reg query HKCU\Software\Classes\EPUBReader.Epub.1 /s
     reg query HKCU\Software\Classes\.epub /s

================================================================================
#>

#Requires -Version 5.1
[CmdletBinding()]
param(
    # Path to the built executable. Defaults to the onedir bundle next to the repo.
    [string] $ExePath = '',

    [switch] $Uninstall,
    [switch] $DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# --- identity ----------------------------------------------------------------
$ProgId       = 'EPUBReader.Epub.1'        # = store.PROG_ID; ProgIds stay ASCII
$AppRegName   = 'EPUBReader'               # RegisteredApplications entry
$AppName      = 'EPUB Reader'              # must equal the exe's file name (Applications\<name>.exe)
$Extension    = '.epub'

# Explorer labels follow the Windows display language, like the app's own 'auto' language.
$uiCulture = (Get-UICulture).Name
switch -Regex ($uiCulture) {
    '^zh-(TW|HK|MO)|^zh-Hant' { $TypeName = 'EPUB 電子書'; $VerbName = "使用 $AppName 開啟"; $AppDesc = 'EPUB 電子書閱讀器'; break }
    '^zh'                     { $TypeName = 'EPUB 电子书'; $VerbName = "使用 $AppName 打开"; $AppDesc = 'EPUB 电子书阅读器'; break }
    '^ja'                     { $TypeName = 'EPUB 電子書籍'; $VerbName = "$AppName で開く"; $AppDesc = 'EPUB 電子書籍リーダー'; break }
    default                   { $TypeName = 'EPUB Book'; $VerbName = "Open with $AppName"; $AppDesc = 'EPUB e-book reader' }
}

$K_Classes    = 'HKCU:\Software\Classes'
$K_ProgId     = "$K_Classes\$ProgId"
$K_Ext        = "$K_Classes\$Extension"
$K_ExtProgIds = "$K_Ext\OpenWithProgids"
$K_App        = "$K_Classes\Applications\$AppName.exe"
$K_Vendor     = 'HKCU:\Software\EPUBReader'
$K_Caps       = "$K_Vendor\Capabilities"
$K_Install    = "$K_Vendor\Install"
$K_RegApps    = 'HKCU:\Software\RegisteredApplications'
$K_FileExts   = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\$Extension"

# --- output helpers ----------------------------------------------------------
function Step ($m) { Write-Host ""; Write-Host "==> $m" -ForegroundColor Cyan }
function Info ($m) { Write-Host "  $m" }
function Note ($m) { Write-Host "  ! $m" -ForegroundColor Yellow }
function Fail ($m) { Write-Host ""; Write-Host "FAILED: $m" -ForegroundColor Red; Write-Host ""; exit 1 }

$script:Changes = 0

function Ensure-Key ([string] $Path) {
    if (Test-Path -LiteralPath $Path) { return $false }
    if ($DryRun) { Info "[dry-run] create key   $Path"; return $true }
    New-Item -Path $Path -Force | Out-Null
    Info "create key   $Path"
    $script:Changes++
    return $true
}

function Set-Value ([string] $Path, [string] $Name, [string] $Value) {
    $shown = $Name
    if ($shown -eq '') { $shown = '(default)' }
    if ($DryRun) { Info "[dry-run] set value    $Path :: $shown = `"$Value`""; return }
    if (-not (Test-Path -LiteralPath $Path)) { New-Item -Path $Path -Force | Out-Null }
    New-ItemProperty -LiteralPath $Path -Name $Name -Value $Value -PropertyType String -Force | Out-Null
    Info "set value    $Path :: $shown"
    $script:Changes++
}

function Remove-Key ([string] $Path) {
    if (-not (Test-Path -LiteralPath $Path)) { Info "absent       $Path"; return }
    if ($DryRun) { Info "[dry-run] delete key   $Path"; return }
    Remove-Item -LiteralPath $Path -Recurse -Force
    Info "deleted      $Path"
    $script:Changes++
}

function Remove-Value ([string] $Path, [string] $Name) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $p = Get-ItemProperty -LiteralPath $Path -ErrorAction SilentlyContinue
    if (-not $p -or -not ($p.PSObject.Properties.Name -contains $Name)) { return }
    if ($DryRun) { Info "[dry-run] delete value $Path :: $Name"; return }
    Remove-ItemProperty -LiteralPath $Path -Name $Name -Force
    Info "deleted      $Path :: $Name"
    $script:Changes++
}

function Get-StringValue ([string] $Path, [string] $Name) {
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $p = Get-ItemProperty -LiteralPath $Path -ErrorAction SilentlyContinue
    if (-not $p -or -not ($p.PSObject.Properties.Name -contains $Name)) { return $null }
    return [string] $p.$Name
}

# Tell Explorer the association table changed, so the context menu / icon
# refresh now rather than at the next sign-in.
function Notify-Explorer {
    if ($DryRun) { Info "[dry-run] SHChangeNotify(SHCNE_ASSOCCHANGED)"; return }
    if (-not ('EpubReaderShell.Api' -as [type])) {
        Add-Type -Namespace 'EpubReaderShell' -Name 'Api' -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("shell32.dll", CharSet = System.Runtime.InteropServices.CharSet.Auto, SetLastError = true)]
public static extern void SHChangeNotify(int wEventId, uint uFlags, System.IntPtr dwItem1, System.IntPtr dwItem2);
'@
    }
    $SHCNE_ASSOCCHANGED = 0x08000000
    $SHCNF_IDLIST       = 0x0000
    [EpubReaderShell.Api]::SHChangeNotify($SHCNE_ASSOCCHANGED, $SHCNF_IDLIST, [IntPtr]::Zero, [IntPtr]::Zero)
    Info "Explorer notified (SHCNE_ASSOCCHANGED)"
}

Write-Host ""
Write-Host "$AppName -- .epub file association (current user only)" -ForegroundColor White
if ($DryRun) { Note "DRY RUN: nothing will be written." }

# =============================================================================
# UNINSTALL
# =============================================================================
if ($Uninstall) {
    Step "Removing the association"

    # Only drop the UserChoice if it is ours; never touch another app's default.
    $uc = Get-StringValue "$K_FileExts\UserChoice" 'ProgId'
    if ($uc -eq $ProgId) {
        Info "UserChoice currently points at $ProgId -- clearing it"
        Remove-Key "$K_FileExts\UserChoice"
    }
    elseif ($uc) {
        Info "UserChoice belongs to '$uc' -- left alone"
    }

    Remove-Value "$K_FileExts\OpenWithProgids" $ProgId
    Remove-Value $K_ExtProgIds $ProgId

    # Remove the .epub key itself only if WE created it and it is now empty.
    $createdExt = Get-StringValue $K_Install 'CreatedExtKey'
    if ($createdExt -eq '1' -and (Test-Path -LiteralPath $K_Ext)) {
        $sub = @(Get-ChildItem -LiteralPath $K_Ext -ErrorAction SilentlyContinue)
        $props = @()
        $pp = Get-ItemProperty -LiteralPath $K_Ext -ErrorAction SilentlyContinue
        if ($pp) { $props = @($pp.PSObject.Properties.Name | Where-Object { $_ -notlike 'PS*' }) }
        if ($sub.Count -eq 0 -and $props.Count -eq 0) { Remove-Key $K_Ext }
        else { Info "left $K_Ext in place (not empty)" }
    }

    Remove-Key $K_ProgId
    Remove-Key $K_App
    Remove-Value $K_RegApps $AppRegName
    Remove-Key $K_Vendor

    Notify-Explorer

    Write-Host ""
    Write-Host "  Removed. $script:Changes registry change(s)." -ForegroundColor Green
    Write-Host "  .epub files are no longer associated with $AppName for this user."
    Write-Host ""
    exit 0
}

# =============================================================================
# INSTALL
# =============================================================================
Step "Locating the executable"

if (-not $ExePath) {
    $root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)
    $ExePath = Join-Path $root "dist\$AppName\$AppName.exe"
}
if (-not [System.IO.Path]::IsPathRooted($ExePath)) {
    $ExePath = Join-Path (Get-Location).Path $ExePath
}
if (-not (Test-Path -LiteralPath $ExePath -PathType Leaf)) {
    Fail ("No executable at`n    $ExePath`n" +
          "  Build it first with .\build_exe.ps1, or pass -ExePath <path to the .exe>.")
}
$ExePath = (Resolve-Path -LiteralPath $ExePath).ProviderPath
Info "exe : $ExePath"

$Command = '"{0}" "%1"' -f $ExePath
$Icon    = '{0},0' -f $ExePath

Step "Registering the ProgId"
Set-Value $K_ProgId ''                  $TypeName
Set-Value $K_ProgId 'FriendlyTypeName'  $TypeName
Set-Value "$K_ProgId\DefaultIcon" ''    $Icon
Set-Value "$K_ProgId\shell\open" ''     $VerbName
Set-Value "$K_ProgId\shell\open\command" '' $Command

Step "Advertising the extension"
$createdExt = '0'
if (-not (Test-Path -LiteralPath $K_Ext)) {
    Ensure-Key $K_Ext | Out-Null
    $createdExt = '1'
}
else {
    Info "$K_Ext already exists -- leaving its (default) value alone"
}
# The empty-string value under OpenWithProgids is what Windows 11 reads to build
# the "Open with" list. Do NOT overwrite the extension key's (default) value:
# that would hijack whatever reader the user already has.
Set-Value $K_ExtProgIds $ProgId ''

Step "Registering the application"
Set-Value $K_App 'FriendlyAppName' $AppName
Set-Value "$K_App\shell\open\command" '' $Command
Set-Value "$K_App\SupportedTypes" $Extension ''

Step "Listing in Settings > Default apps"
Set-Value $K_Caps 'ApplicationName'        $AppName
Set-Value $K_Caps 'ApplicationDescription' $AppDesc
Set-Value "$K_Caps\FileAssociations" $Extension $ProgId
Set-Value $K_RegApps $AppRegName 'Software\EPUBReader\Capabilities'

Step "Recording bookkeeping for a clean uninstall"
Set-Value $K_Install 'ExePath'       $ExePath
Set-Value $K_Install 'InstalledAt'   (Get-Date).ToString('o')
Set-Value $K_Install 'CreatedExtKey' $createdExt

Step "Refreshing Explorer"
Notify-Explorer

Write-Host ""
Write-Host "  Done. $script:Changes registry change(s), all under HKEY_CURRENT_USER." -ForegroundColor Green
Write-Host ""
Write-Host "  $AppName now appears under right-click > Open with." -ForegroundColor White
Write-Host "  To make it the default, do it once from the shell (Windows will not let"
Write-Host "  a script set the default for you):"
Write-Host "      right-click an .epub > Open with > Choose another app > $AppName > Always"
Write-Host ""
Write-Host "  Undo at any time:  .\tools\install-file-association.ps1 -Uninstall"
Write-Host ""
