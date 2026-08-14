param(
    [string]$InstallerPath,
    [string]$CertificateThumbprint,
    [string]$TimestampServer = "http://timestamp.digicert.com"
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

if (-not $InstallerPath) {
    $InstallerPath = Join-Path $Root "release\UnifiedOCR_Setup.exe"
}
$InstallerPath = [System.IO.Path]::GetFullPath($InstallerPath)

if (-not (Test-Path -LiteralPath $InstallerPath)) {
    throw "Installer nicht gefunden: $InstallerPath"
}
if (-not $CertificateThumbprint) {
    throw "CertificateThumbprint fehlt. Beispiel: .\packaging\windows\sign_release.ps1 -CertificateThumbprint ABC123..."
}

$signtool = Get-Command signtool.exe -ErrorAction SilentlyContinue
if (-not $signtool) {
    throw "signtool.exe wurde nicht gefunden. Installiere Windows SDK oder oeffne eine Developer PowerShell."
}

& $signtool.Source sign `
    /fd SHA256 `
    /tr $TimestampServer `
    /td SHA256 `
    /sha1 $CertificateThumbprint `
    $InstallerPath

& $signtool.Source verify /pa /v $InstallerPath

Get-AuthenticodeSignature -LiteralPath $InstallerPath | Format-List
