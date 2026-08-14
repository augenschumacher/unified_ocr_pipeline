param(
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$ReleaseDir = Join-Path $Root "release"
$PayloadRoot = Join-Path $ReleaseDir "payload"
$PayloadDir = Join-Path $PayloadRoot "UnifiedOCR"
$PayloadZip = Join-Path $ReleaseDir "unifiedocr_payload.zip"
$InstallerBuild = Join-Path $ReleaseDir "installer_build"
$InstallerDist = Join-Path $ReleaseDir "installer_dist"
$InstallerSpec = Join-Path $ReleaseDir "UnifiedOCR_Setup.spec"
$FinalInstaller = Join-Path $ReleaseDir "UnifiedOCR_Setup.exe"

Set-Location $Root

function Assert-InProject {
    param([Parameter(Mandatory = $true)][string]$Path)

    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $rootWithSeparator = $Root.TrimEnd('\') + '\'
    if (-not $fullPath.StartsWith($rootWithSeparator, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to operate outside project root: $fullPath"
    }
    return $fullPath
}

function Remove-DirectoryInsideProject {
    param([Parameter(Mandatory = $true)][string]$Path)

    $fullPath = Assert-InProject $Path
    if (Test-Path -LiteralPath $fullPath) {
        $resolvedPath = (Resolve-Path -LiteralPath $fullPath).Path
        Assert-InProject $resolvedPath | Out-Null
        Remove-Item -LiteralPath $resolvedPath -Recurse -Force
    }
}

if (-not $SkipTests) {
    py -3.10 -m pytest unified_ocr_app
    py -3.10 unified_ocr_app\release_check.py
}

py -3.10 -m pip install --upgrade pyinstaller

Remove-DirectoryInsideProject $ReleaseDir
New-Item -ItemType Directory -Force -Path $ReleaseDir | Out-Null

py -3.10 -m PyInstaller --clean --noconfirm (Join-Path $Root "packaging\windows\UnifiedOCR.spec") --distpath (Join-Path $ReleaseDir "app_dist") --workpath (Join-Path $ReleaseDir "app_build")

New-Item -ItemType Directory -Force -Path $PayloadRoot | Out-Null
New-Item -ItemType Directory -Force -Path $PayloadDir | Out-Null
Copy-Item -Path (Join-Path $ReleaseDir "app_dist\UnifiedOCR\*") -Destination $PayloadDir -Recurse -Force

Compress-Archive -Path (Join-Path $PayloadDir "*") -DestinationPath $PayloadZip -Force

py -3.10 -m PyInstaller --clean --noconfirm --onefile --windowed `
    --name UnifiedOCR_Setup `
    --add-data "$PayloadZip;." `
    --add-data "$(Join-Path $Root "unified_ocr_app\resources\ollama_model_recommendations.json");." `
    --distpath $InstallerDist `
    --workpath $InstallerBuild `
    --specpath $ReleaseDir `
    (Join-Path $Root "packaging\windows\installer.py")

Copy-Item -Path (Join-Path $InstallerDist "UnifiedOCR_Setup.exe") -Destination $FinalInstaller -Force

Write-Host ""
Write-Host "Build fertig:"
Write-Host "  App:       $PayloadDir"
Write-Host "  Installer: $FinalInstaller"
