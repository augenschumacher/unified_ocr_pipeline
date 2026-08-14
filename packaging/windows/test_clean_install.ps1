param(
    [string]$InstallerPath,
    [switch]$RunInstaller
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Root

if (-not $InstallerPath) {
    $InstallerPath = Join-Path $Root "release\UnifiedOCR_Setup.exe"
}
$InstallerPath = [System.IO.Path]::GetFullPath($InstallerPath)

if (-not (Test-Path -LiteralPath $InstallerPath)) {
    throw "Installer nicht gefunden: $InstallerPath"
}

$PayloadPath = Join-Path $Root "release\unifiedocr_payload.zip"
$result = [ordered]@{
    installer = $InstallerPath
    installerExists = $true
    installerSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $InstallerPath).Hash
    payloadExists = (Test-Path -LiteralPath $PayloadPath)
    wingetAvailable = [bool](Get-Command winget.exe -ErrorAction SilentlyContinue)
    pythonLauncherAvailable = [bool](Get-Command py.exe -ErrorAction SilentlyContinue)
}

if ($result.pythonLauncherAvailable -and $result.payloadExists) {
    $preflightJson = py -3.10 packaging\windows\installer.py --preflight --payload $PayloadPath
    $result.preflight = $preflightJson | ConvertFrom-Json
}

$result | ConvertTo-Json -Depth 8

if ($RunInstaller) {
    Write-Host ""
    Write-Host "Starte interaktiven Installer. In einer Clean-VM bitte danach pruefen:"
    Write-Host "- App startet"
    Write-Host "- Systemcheck zeigt Tesseract, Ghostscript, QPDF und OCRmyPDF als verfuegbar"
    Write-Host "- Ein kleines PDF/Bild kann verarbeitet werden"
    Write-Host "- Deinstallation entfernt Programmdateien und Verknuepfungen"
    Start-Process -FilePath $InstallerPath -Wait
}
