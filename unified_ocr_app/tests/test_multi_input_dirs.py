import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.config import AppConfig
from core.settings import SettingsManager
from core.watcher import DirectoryWatcher


def test_app_config_deduplicates_primary_and_additional_inputs(tmp_path):
    extra = tmp_path / "scanner"
    config = AppConfig(tmp_path, additional_consume_dirs=[tmp_path / "consume", extra, extra])

    assert config.consume_dirs == [tmp_path / "consume", extra]
    assert config.source_consume_dir_for(extra / "doc.pdf") == extra


def test_settings_validate_additional_input_dirs(tmp_path):
    settings_path = tmp_path / "settings.json"
    manager = SettingsManager(settings_path)
    settings = manager.settings
    settings["base_dir"] = str(tmp_path / "base")
    settings["additional_consume_dirs"] = [str(tmp_path / "base" / "scanner")]

    manager.save(settings)

    loaded = SettingsManager(settings_path).settings
    assert loaded["additional_consume_dirs"] == [str(tmp_path / "base" / "scanner")]


def test_settings_reject_reserved_additional_input_dir(tmp_path):
    manager = SettingsManager(tmp_path / "settings.json")
    settings = manager.settings
    settings["base_dir"] = str(tmp_path / "base")
    settings["additional_consume_dirs"] = [str(tmp_path / "base" / "final")]

    with pytest.raises(ValueError):
        manager.save(settings)


def test_settings_reject_nested_reserved_additional_input_dir(tmp_path):
    manager = SettingsManager(tmp_path / "settings.json")
    settings = manager.settings
    settings["base_dir"] = str(tmp_path / "base")
    settings["additional_consume_dirs"] = [
        str(tmp_path / "base" / "final" / "Fabio"),
        str(tmp_path / "base" / "work" / "incoming"),
    ]

    with pytest.raises(ValueError):
        manager.save(settings)


def test_settings_reject_base_dir_as_additional_input_dir(tmp_path):
    manager = SettingsManager(tmp_path / "settings.json")
    settings = manager.settings
    settings["base_dir"] = str(tmp_path / "base")
    settings["additional_consume_dirs"] = [str(tmp_path / "base")]

    with pytest.raises(ValueError):
        manager.save(settings)


def test_settings_save_creates_backup_of_previous_file(tmp_path):
    settings_path = tmp_path / "settings.json"
    manager = SettingsManager(settings_path)
    settings = manager.settings
    settings["base_dir"] = str(tmp_path / "first")
    manager.save(settings)

    updated = manager.load()
    updated["base_dir"] = str(tmp_path / "second")
    manager.save(updated)

    backup = manager.backup_path()
    assert backup.exists()
    assert json.loads(backup.read_text(encoding="utf-8"))["base_dir"] == str(tmp_path / "first")
    assert json.loads(settings_path.read_text(encoding="utf-8"))["base_dir"] == str(tmp_path / "second")


def test_settings_save_keeps_existing_file_when_replace_fails(tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.json"
    manager = SettingsManager(settings_path)
    settings = manager.settings
    settings["base_dir"] = str(tmp_path / "first")
    manager.save(settings)
    original_text = settings_path.read_text(encoding="utf-8")

    updated = manager.load()
    updated["base_dir"] = str(tmp_path / "second")

    def fail_replace(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr("core.settings.os.replace", fail_replace)

    with pytest.raises(RuntimeError):
        manager.save(updated)

    assert settings_path.read_text(encoding="utf-8") == original_text
    assert not settings_path.with_name(f"{settings_path.name}.tmp").exists()


def test_watcher_queues_files_from_additional_input_dir(tmp_path):
    extra = tmp_path / "scanner"
    extra.mkdir()
    source = extra / "scan.pdf"
    source.write_bytes(b"pdf")

    orchestrator = MagicMock()
    orchestrator.config = AppConfig(tmp_path, additional_consume_dirs=[extra])
    watcher = DirectoryWatcher(orchestrator)

    with patch("time.time", return_value=time.time() + 10):
        current = set()
        watcher._track_candidate(source, current)
        watcher._track_candidate(source, current)
        watcher._track_candidate(source, current)

    assert source in watcher.seen_files
    assert watcher.queue.get_nowait() == source
