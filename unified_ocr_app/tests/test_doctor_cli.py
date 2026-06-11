import json
from pathlib import Path
from unittest.mock import patch

from doctor import build_doctor_report, main


def test_build_doctor_report_uses_settings_file(tmp_path):
    settings_path = tmp_path / "settings.json"
    base_dir = tmp_path / "work"
    settings_path.write_text(
        json.dumps({
            "base_dir": str(base_dir),
            "gdrive_credentials_path": str(tmp_path / "credentials.json"),
            "gdrive_token_path": str(tmp_path / "token.json"),
        }),
        encoding="utf-8",
    )

    report = build_doctor_report(settings_path=str(settings_path))

    assert "python" in report
    assert report["directories"][0]["name"] == str(base_dir)
    assert report["google_drive"]["credentials_exists"] is False


def test_doctor_main_outputs_json(tmp_path, capsys):
    with patch("doctor.build_doctor_report", return_value={"ok": True, "python": {"version": "3.10"}}):
        exit_code = main(["--base-dir", str(tmp_path), "--json"])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["ok"] is True


def test_doctor_main_returns_nonzero_for_failed_check(tmp_path, capsys):
    with patch("doctor.build_doctor_report", return_value={"ok": False, "python": {"version": "3.11", "message": "wrong"}, "commands": [], "directories": [], "google_drive": {}}):
        exit_code = main(["--base-dir", str(tmp_path)])

    assert exit_code == 1
    assert "Gesamtstatus" in capsys.readouterr().out
