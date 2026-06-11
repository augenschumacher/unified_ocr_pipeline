from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_user_facing_text_has_no_common_mojibake_markers():
    files = [
        ROOT / "README.md",
        ROOT / "unified_ocr_app" / "README.md",
        ROOT / "unified_ocr_app" / "app.py",
        ROOT / "unified_ocr_app" / "core" / "settings.py",
    ]
    markers = ["\u00c3", "\u00c2", "\u00e2\u20ac", "\u00f0\u0178", "\ufffd"]

    offenders = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for marker in markers:
            if marker in text:
                offenders.append(f"{path.relative_to(ROOT)} contains {marker!r}")

    assert offenders == []
