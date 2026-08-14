"""Shared helpers for selecting and staging user input documents."""

from __future__ import annotations

import shutil
from pathlib import Path

from core.file_types import SUPPORTED_INPUT_SUFFIXES


def supported_suffixes_text() -> str:
    return ", ".join(sorted(SUPPORTED_INPUT_SUFFIXES))


def supported_file_dialog_patterns() -> str:
    return " ".join(f"*{suffix}" for suffix in sorted(SUPPORTED_INPUT_SUFFIXES))


def validate_supported_file(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"Eingabedatei '{path}' existiert nicht.")
    if not path.is_file():
        raise ValueError(f"Eingabepfad '{path}' ist keine Datei.")
    if path.suffix.lower() not in SUPPORTED_INPUT_SUFFIXES:
        raise ValueError(
            f"Nicht unterstuetzter Dateityp '{path.suffix or '<ohne Endung>'}'. "
            f"Unterstuetzt werden: {supported_suffixes_text()}."
        )


def collect_cli_inputs(file_path: str | None = None, dir_path: str | None = None) -> list[Path]:
    if file_path:
        path = Path(file_path)
        validate_supported_file(path)
        return [path]

    if dir_path:
        directory = Path(dir_path)
        if not directory.exists() or not directory.is_dir():
            raise NotADirectoryError(f"Eingabeverzeichnis '{dir_path}' existiert nicht.")
        return [
            candidate
            for candidate in sorted(directory.iterdir(), key=lambda item: item.name.lower())
            if candidate.is_file() and candidate.suffix.lower() in SUPPORTED_INPUT_SUFFIXES
        ]

    return []


def _path_key(path: Path) -> str:
    try:
        return str(path.resolve(strict=False)).lower()
    except OSError:
        return str(path.absolute()).lower()


def collect_supported_input_files(paths: list[Path]) -> tuple[list[Path], list[Path]]:
    files: list[Path] = []
    rejected: list[Path] = []
    seen = set()

    for path in paths:
        candidates: list[Path]
        if path.is_dir():
            try:
                candidates = sorted(
                    (child for child in path.iterdir() if child.is_file()),
                    key=lambda item: item.name.lower(),
                )
            except OSError:
                rejected.append(path)
                continue
        else:
            candidates = [path]

        accepted_in_group = False
        for candidate in candidates:
            if candidate.is_file() and candidate.suffix.lower() in SUPPORTED_INPUT_SUFFIXES:
                key = _path_key(candidate)
                if key not in seen:
                    files.append(candidate)
                    seen.add(key)
                accepted_in_group = True
            elif not path.is_dir():
                rejected.append(candidate)
        if path.is_dir() and not accepted_in_group:
            rejected.append(path)

    return files, rejected


def unique_path_for(directory: Path, source: Path, *, max_attempts: int = 1000) -> Path:
    target = directory / source.name
    if not target.exists():
        return target

    stem = source.stem or "dokument"
    suffix = source.suffix
    for index in range(1, max_attempts):
        candidate = directory / f"{stem}_{index:03d}{suffix}"
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"Kein freier Dateiname in {directory} fuer {source.name}.")


def stage_input_file(source: Path, config) -> Path:
    if config.source_consume_dir_for(source):
        return source
    target = unique_path_for(config.consume_dir, source)
    shutil.copy2(source, target)
    return target
