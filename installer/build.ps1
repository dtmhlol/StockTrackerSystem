<#
  Builds Stock Tracker into a Windows installer.

    powershell -ExecutionPolicy Bypass -File installer\build.ps1

  Steps: build environment -> icon -> PyInstaller (dist\StockTracker\StockTracker.exe)
  -> self-test of the packaged app -> Inno Setup (installer\Output\StockTracker-Setup-<version>.exe)

  -SkipInstaller  stops after the packaged app (useful if Inno Setup isn't installed yet)
#>
param([switch]$SkipInstaller)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"

function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Run($exe, $arguments) {
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $exe $($arguments -join ' ')" }
}

Step "Build environment (.venv)"
if (-not (Test-Path $py)) { Run "python" @("-m", "venv", ".venv") }
Run $py @("-m", "pip", "install", "--quiet", "--upgrade", "pip")
Run $py @("-m", "pip", "install", "--quiet", "-r", "requirements.txt", "pyinstaller")

Step "Icon"
Run $py @("installer\make_icon.py")

$version = (& $py -c "import sys; sys.path.insert(0, 'src'); import app_paths; print(app_paths.APP_VERSION)").Trim()
Write-Host "Version $version"

Step "Packaging with PyInstaller"
Run $py @("-m", "PyInstaller", "installer\stock_tracker.spec", "--noconfirm", "--clean", "--distpath", "dist", "--workpath", "build")

Step "Self-test of the packaged app"
$report = Join-Path $env:TEMP "stocktracker-selftest.txt"
Remove-Item $report -ErrorAction SilentlyContinue
$process = Start-Process "dist\StockTracker\StockTracker.exe" -ArgumentList @("--selftest", "`"$report`"") -Wait -PassThru
if (-not (Test-Path $report)) { throw "Self-test produced no report. The packaged app did not start." }
Get-Content $report
if ($process.ExitCode -ne 0) { throw "Self-test FAILED: the package is missing something (see above)." }

if ($SkipInstaller) { Write-Host "`nPackaged app ready: dist\StockTracker\StockTracker.exe" -ForegroundColor Green; return }

Step "Installer with Inno Setup"
$candidates = @(
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
)
$iscc = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
    throw "Inno Setup 6 was not found. Install it (winget install JRSoftware.InnoSetup), then run this script again."
}
Run $iscc @("/DAppVersion=$version", "installer\StockTracker.iss")

Write-Host "`nDone: installer\Output\StockTracker-Setup-$version.exe" -ForegroundColor Green
