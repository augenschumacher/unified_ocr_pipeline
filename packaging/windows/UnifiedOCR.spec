# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules


ROOT = Path(SPECPATH).parents[1]
APP_DIR = ROOT / "unified_ocr_app"


block_cipher = None


a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT), str(APP_DIR)],
    binaries=[],
    datas=[
        (str(ROOT / "LICENSE"), "."),
        (str(ROOT / "README.md"), "."),
        (str(APP_DIR / "README.md"), "unified_ocr_app"),
        (str(APP_DIR / "SECURITY.md"), "unified_ocr_app"),
        (str(APP_DIR / "THIRD_PARTY_LICENSES.md"), "unified_ocr_app"),
        (str(APP_DIR / "resources" / "ollama_model_recommendations.json"), "unified_ocr_app/resources"),
        *collect_data_files("tkinterdnd2"),
    ],
    hiddenimports=[
        "customtkinter",
        "tkinterdnd2",
        *collect_submodules("ocrmypdf"),
        "PIL.Image",
        "PIL.ImageOps",
        "PIL.ImageSequence",
        "pillow_heif",
        "fitz",
        "yaml",
        "googleapiclient.discovery",
        "google_auth_oauthlib.flow",
        "google.auth.transport.requests",
        "pystray",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pytest",
        "unittest",
        "tests",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="UnifiedOCR",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="UnifiedOCR",
)
