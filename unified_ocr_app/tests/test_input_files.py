from pathlib import Path

import pytest

from core.config import AppConfig
from core.input_files import (
    collect_cli_inputs,
    collect_supported_input_files,
    stage_input_file,
    supported_file_dialog_patterns,
    supported_suffixes_text,
    unique_path_for,
)


def test_collect_cli_inputs_validates_single_file(tmp_path):
    supported = tmp_path / "scan.pdf"
    unsupported = tmp_path / "notes.txt"
    supported.write_bytes(b"pdf")
    unsupported.write_text("text", encoding="utf-8")

    assert collect_cli_inputs(file_path=str(supported)) == [supported]
    with pytest.raises(ValueError):
        collect_cli_inputs(file_path=str(unsupported))


def test_collect_supported_input_files_deduplicates_and_filters_directories(tmp_path):
    input_dir = tmp_path / "drop"
    input_dir.mkdir()
    pdf = input_dir / "b.pdf"
    image = input_dir / "a.png"
    ignored = input_dir / "notes.txt"
    pdf.write_bytes(b"pdf")
    image.write_bytes(b"png")
    ignored.write_text("text", encoding="utf-8")

    files, rejected = collect_supported_input_files([input_dir, pdf, ignored])

    assert files == [image, pdf]
    assert rejected == [ignored]


def test_stage_input_file_copies_external_file_with_unique_name(tmp_path):
    config = AppConfig(tmp_path)
    config.ensure_directories()
    source = tmp_path / "outside.pdf"
    source.write_bytes(b"first")
    (config.consume_dir / "outside.pdf").write_bytes(b"existing")

    staged = stage_input_file(source, config)

    assert staged == config.consume_dir / "outside_001.pdf"
    assert staged.read_bytes() == b"first"
    assert source.exists()


def test_stage_input_file_keeps_files_already_in_any_consume_dir(tmp_path):
    extra = tmp_path / "scanner"
    config = AppConfig(tmp_path, additional_consume_dirs=[extra])
    config.ensure_directories()
    source = extra / "scan.pdf"
    source.write_bytes(b"pdf")

    assert stage_input_file(source, config) == source


def test_unique_path_for_preserves_existing_files(tmp_path):
    source = Path("doc.pdf")
    (tmp_path / "doc.pdf").write_text("existing", encoding="utf-8")

    assert unique_path_for(tmp_path, source) == tmp_path / "doc_001.pdf"


def test_supported_suffix_helpers_are_ui_ready():
    assert ".pdf" in supported_suffixes_text()
    assert "*.pdf" in supported_file_dialog_patterns()
