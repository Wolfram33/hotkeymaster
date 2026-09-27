# Baut hotkey-master.exe (Nuitka) und den Installer dist\Hotkey-Master-Setup.exe (Inno Setup 6).
# Wird lokal und von der GitHub-Action (.github/workflows/release.yml) verwendet.
#
#   pwsh ./build.ps1              # .exe + Installer
#   pwsh ./build.ps1 -SkipSetup   # nur die .exe
#
# Voraussetzungen: python -m pip install -r requirements.txt nuitka zstandard
#                  Visual Studio Build Tools (C++), Inno Setup 6 für den Installer
param([switch]$SkipSetup)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

# Einzige Quelle der Versionsnummer ist APP_VERSION in hotkey-master.py
$match = Select-String -Path hotkey-master.py -Pattern "^APP_VERSION = '(\d+\.\d+\.\d+)'"
if (-not $match) { throw 'APP_VERSION in hotkey-master.py nicht gefunden.' }
$version = $match.Matches[0].Groups[1].Value
Write-Host "Baue Hotkey-Master $version"

# Außerhalb des Projekts bauen: Virenscanner sperren sonst gern die frische .exe,
# und Nuitka bricht mit "Failed to add resources ... error code 22" ab.
$buildDir = Join-Path ([IO.Path]::GetTempPath()) 'hotkey-master-build'

python -m nuitka --onefile --output-dir="$buildDir" --assume-yes-for-downloads `
    --enable-plugin=pyqt6 --windows-console-mode=disable `
    --windows-icon-from-ico=icon.ico --include-data-file=icon.ico=icon.ico `
    --include-module=win32api --include-module=win32con --include-module=pynput `
    --product-name='Hotkey-Master' --product-version=$version --file-version=$version `
    --file-description='Hotkey-Master - systemweite Tastenkuerzel' `
    --company-name='Wolfram Consult GmbH & Co. KG' `
    --copyright='Copyright 2026 Wolfram Consult GmbH & Co. KG - Apache-2.0' `
    hotkey-master.py
if ($LASTEXITCODE -ne 0) { throw "Nuitka ist fehlgeschlagen (Exitcode $LASTEXITCODE)." }
Copy-Item (Join-Path $buildDir 'hotkey-master.exe') . -Force
Write-Host 'Fertig: hotkey-master.exe'

if ($SkipSetup) { return }

$iscc = (Get-Command iscc.exe -ErrorAction SilentlyContinue).Source
if (-not $iscc) {
    $iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe") |
        Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $iscc) {
    Write-Warning 'Inno Setup 6 nicht gefunden – Installer übersprungen. Download: https://jrsoftware.org/isdl.php'
    return
}
& $iscc "/DMyAppVersion=$version" 'setup\setup.iss'
if ($LASTEXITCODE -ne 0) { throw "Inno Setup ist fehlgeschlagen (Exitcode $LASTEXITCODE)." }
Write-Host 'Fertig: dist\Hotkey-Master-Setup.exe'
