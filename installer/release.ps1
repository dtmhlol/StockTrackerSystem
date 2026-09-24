<#
  Builds, signs and (optionally) publishes a Stock Tracker release.

    powershell -ExecutionPolicy Bypass -File installer\release.ps1            build + sign, then print upload steps
    powershell -ExecutionPolicy Bypass -File installer\release.ps1 -Publish   also create the GitHub release (needs the GitHub CLI, `gh`)

  Before running: raise APP_VERSION in src\app_paths.py and add a matching "## [x.y.z]" section to CHANGELOG.md.
  The release notes shown by the in-app updater come from that CHANGELOG section.
#>
param([switch]$Publish)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"

function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Run($exe, $arguments) {
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $exe $($arguments -join ' ')" }
}

Step "Build the app and installer"
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "build.ps1")
if ($LASTEXITCODE -ne 0) { throw "The build failed." }

$version = (& $py -c "import sys; sys.path.insert(0, 'src'); import app_paths; print(app_paths.APP_VERSION)").Trim()
$installer = Join-Path $root "installer\Output\StockTracker-Setup-$version.exe"
if (-not (Test-Path $installer)) { throw "Expected installer not found: $installer" }

Step "Sign the installer"
Run $py @("installer\release_tools.py", "sign", $installer)
Run $py @("installer\release_tools.py", "verify", $installer)

Step "Release notes from CHANGELOG.md"
$notesFile = Join-Path $root "installer\Output\release-notes-$version.md"
& $py installer\release_tools.py notes $version --out $notesFile
if ($LASTEXITCODE -ne 0) { throw "CHANGELOG.md has no section for version $version." }
Get-Content $notesFile -Encoding UTF8

$signature = "$installer.sig"
Write-Host "`nReady to publish version $version:" -ForegroundColor Green
Write-Host "  $installer"
Write-Host "  $signature"

if ($Publish) {
    Step "Publish to GitHub"
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        throw "The GitHub CLI (gh) isn't installed. Install it (winget install GitHub.cli), run 'gh auth login', or upload by hand as described below."
    }
    Run "gh" @("release", "create", "v$version", $installer, $signature, "--title", "Stock Tracker $version", "--notes-file", $notesFile)
    Write-Host "`nPublished v$version." -ForegroundColor Green
} else {
    Write-Host @"

To publish by hand:
  1. Open https://github.com/dtmhlol/StockTrackerSystem/releases/new
  2. Tag: v$version   (create it on publish, on the branch you use)
  3. Title: Stock Tracker $version
  4. Paste the notes from: $notesFile
  5. Attach BOTH files above (the .exe and the .exe.sig). Without the .sig, the app can't install it automatically.
  6. Publish release.
"@
}
