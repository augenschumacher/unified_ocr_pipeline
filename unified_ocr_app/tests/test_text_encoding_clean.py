from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "unified_ocr_app"

# Doppelt kodierte Umlaute sind in der App bereits mehrfach in
# benutzersichtbaren Fehlermeldungen gelandet.  Der Test prueft deshalb den
# gesamten Quellcode und nicht mehr nur eine Handvoll Dateien.  Die Marker
# werden aus Codepoints gebaut, damit diese Datei nicht auf sich selbst
# anschlaegt.
MOJIBAKE_MARKERS = [
    chr(0x00C3),                 # A-Tilde: Praefix doppelt kodierter Umlaute
    chr(0x00C2),                 # A-Zirkumflex: Praefix doppelt kodierter Zeichen
    chr(0x00E2) + chr(0x20AC),   # typografische Zeichen, doppelt kodiert
    chr(0x00F0) + chr(0x0178),   # Emoji, doppelt kodiert
    chr(0xFFFD),                 # Replacement Character aus verlorener Kodierung
]

SKIPPED_DIRECTORIES = {"__pycache__", ".git", ".pytest_cache", "release", "build", "dist"}


def _source_files():
    documents = [ROOT / "README.md", PACKAGE / "README.md"]
    for path in sorted(PACKAGE.rglob("*.py")):
        if any(part in SKIPPED_DIRECTORIES for part in path.parts):
            continue
        documents.append(path)
    return [path for path in documents if path.is_file()]


def test_user_facing_text_has_no_common_mojibake_markers():
    offenders = []
    for path in _source_files():
        text = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            for marker in MOJIBAKE_MARKERS:
                if marker in line:
                    offenders.append(
                        f"{path.relative_to(ROOT)}:{line_number} enthaelt {marker!r}"
                    )

    assert offenders == []


def test_every_source_file_is_valid_utf8():
    undecodable = []
    for path in _source_files():
        try:
            path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            undecodable.append(f"{path.relative_to(ROOT)}: {exc}")

    assert undecodable == []
