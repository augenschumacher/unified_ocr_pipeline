from core.error_messages import friendly_error_message


def test_friendly_error_message_translates_winerror_145():
    text = friendly_error_message("OSError: [WinError 145] The directory is not empty")

    assert "Ordner konnte nicht vollstaendig ersetzt werden" in text
    assert "Installation erneut starten" in text


def test_friendly_error_message_translates_missing_qpdf():
    text = friendly_error_message("qpdf not found", context="OCR fehlgeschlagen.")

    assert text.startswith("OCR fehlgeschlagen.")
    assert "QPDF wurde nicht gefunden" in text
