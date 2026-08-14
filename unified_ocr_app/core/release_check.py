"""Local release-readiness checks for publishing the project safely."""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


REQUIRED_FILES = [
    ".github/workflows/ci.yml",
    "README.md",
    "LICENSE",
    "requirements.txt",
    "unified_ocr_app/__init__.py",
    "unified_ocr_app/pyproject.toml",
    "unified_ocr_app/requirements.txt",
    "unified_ocr_app/SECURITY.md",
    "unified_ocr_app/THIRD_PARTY_LICENSES.md",
]

REQUIRED_GITIGNORE_PATTERNS = [
    ".env",
    ".env.*",
    "*.key",
    "*.pem",
    "credentials.json",
    "token.json",
    "google_drive_token.json",
    "client_secret*.json",
    "llm_config.yaml",
    "settings.json",
    "settings.json.bak",
    "folder_registry.json",
    "folder_registry.backup.json",
    "classification_memory.json",
    "unified_ocr.sqlite3",
    "*.sqlite3",
    "cache.db",
    "*.log",
    "*.tmp",
]

SENSITIVE_FILE_PATTERNS = [
    ".env",
    ".env.*",
    "*.key",
    "*.pem",
    "*.p12",
    "*.pfx",
    "credentials.json",
    "token.json",
    "google_drive_token.json",
    "client_secret*.json",
    "llm_config.yaml",
    "settings.json",
    "settings.json.bak",
    "settings.json.tmp",
    "folder_registry.json",
    "folder_registry.backup.json",
    "classification_memory.json",
    "unified_ocr.sqlite3",
    "*.sqlite3",
    "cache.db",
    "*.log",
    "*.tmp",
]

TEXT_SUFFIXES = {
    ".bat",
    ".cfg",
    ".ini",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}

SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "env",
    "release",
    "venv",
}

SECRET_PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |)?PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{24,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{24,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{24,}\b"),
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret)\b\s*[:=]\s*[\"']?[A-Za-z0-9_./+=-]{24,}"),
]


@dataclass
class CheckResult:
    name: str
    status: str
    details: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "details": list(self.details)}


def _as_posix(path: Path) -> str:
    return path.as_posix()


def _resolve_project_root(root: str | Path) -> Path:
    root_path = Path(root).resolve()
    if (root_path / "unified_ocr_app" / "pyproject.toml").exists():
        return root_path
    if (
        root_path.name == "unified_ocr_app"
        and (root_path / "pyproject.toml").exists()
        and (root_path.parent / "unified_ocr_app") == root_path
    ):
        return root_path.parent
    return root_path


def _iter_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.is_file():
            yield path


def _matches_any(relative_path: str, patterns: Iterable[str]) -> bool:
    name = Path(relative_path).name
    return any(fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(relative_path, pattern) for pattern in patterns)


def _read_text(path: Path) -> str | None:
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return None
    try:
        if path.stat().st_size > 2_000_000:
            return None
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None


def _normalize_requirement(line: str) -> str | None:
    value = line.strip()
    if not value or value.startswith("#"):
        return None
    return re.sub(r"\s+", "", value.split("#", 1)[0]).lower()


def _read_requirements(path: Path) -> set[str]:
    if not path.exists():
        return set()
    result = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        normalized = _normalize_requirement(line)
        if normalized:
            result.add(normalized)
    return result


def _extract_quoted_array(text: str, key: str, *, section: str | None = None) -> list[str]:
    lines = text.splitlines()
    in_section = section is None
    collecting = False
    values = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_section = stripped == f"[{section}]" if section else True
            collecting = False
            continue
        if not in_section:
            continue
        if not collecting and stripped.startswith(f"{key} = ["):
            collecting = True
            remainder = stripped.split("[", 1)[1]
            if "]" in remainder:
                collecting = False
                remainder = remainder.split("]", 1)[0]
            values.extend(re.findall(r'"([^"]+)"', remainder))
            continue
        if collecting:
            if "]" in stripped:
                collecting = False
                stripped = stripped.split("]", 1)[0]
            values.extend(re.findall(r'"([^"]+)"', stripped))

    return values


def _extract_project_version(pyproject_text: str) -> str | None:
    in_project = False
    for line in pyproject_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_project = stripped == "[project]"
            continue
        if in_project:
            match = re.match(r'version\s*=\s*"([^"]+)"', stripped)
            if match:
                return match.group(1)
    return None


def _extract_init_version(init_text: str) -> str | None:
    match = re.search(r'__version__\s*=\s*"([^"]+)"', init_text)
    return match.group(1) if match else None


def check_required_files(root: Path) -> CheckResult:
    missing = [path for path in REQUIRED_FILES if not (root / path).exists()]
    return CheckResult(
        name="required_files",
        status="ok" if not missing else "failed",
        details=[f"Missing required release file: {path}" for path in missing],
    )


def check_dependency_metadata(root: Path) -> CheckResult:
    details = []
    pyproject_path = root / "unified_ocr_app" / "pyproject.toml"
    init_path = root / "unified_ocr_app" / "__init__.py"
    root_requirements_path = root / "requirements.txt"
    package_requirements_path = root / "unified_ocr_app" / "requirements.txt"

    try:
        pyproject_text = pyproject_path.read_text(encoding="utf-8")
    except OSError as exc:
        return CheckResult("dependency_metadata", "failed", [f"Cannot read pyproject.toml: {exc}"])

    init_text = init_path.read_text(encoding="utf-8") if init_path.exists() else ""
    pyproject_version = _extract_project_version(pyproject_text)
    init_version = _extract_init_version(init_text)
    if not pyproject_version:
        details.append("Project version missing in unified_ocr_app/pyproject.toml")
    if not init_version:
        details.append("Package __version__ missing in unified_ocr_app/__init__.py")
    if pyproject_version and init_version and pyproject_version != init_version:
        details.append(f"Version mismatch: pyproject={pyproject_version}, __init__={init_version}")

    runtime_deps = {
        dep for dep in (_normalize_requirement(item) for item in _extract_quoted_array(pyproject_text, "dependencies", section="project"))
        if dep
    }
    dev_deps = {
        dep for dep in (_normalize_requirement(item) for item in _extract_quoted_array(pyproject_text, "dev", section="project.optional-dependencies"))
        if dep
    }
    expected_requirements = runtime_deps | dev_deps
    root_requirements = _read_requirements(root_requirements_path)
    package_requirements = _read_requirements(package_requirements_path)

    for label, actual in [("requirements.txt", root_requirements), ("unified_ocr_app/requirements.txt", package_requirements)]:
        missing = sorted(expected_requirements - actual)
        extra = sorted(actual - expected_requirements)
        if missing:
            details.append(f"{label} missing dependencies from pyproject/dev: {', '.join(missing)}")
        if extra:
            details.append(f"{label} has dependencies not declared in pyproject/dev: {', '.join(extra)}")

    for module in _extract_quoted_array(pyproject_text, "py-modules", section="tool.setuptools"):
        module_path = root / "unified_ocr_app" / f"{module}.py"
        if not module_path.exists():
            details.append(f"pyproject declares missing top-level module: {module}.py")

    return CheckResult(
        name="dependency_metadata",
        status="ok" if not details else "failed",
        details=details,
    )


def check_gitignore_rules(root: Path) -> CheckResult:
    gitignore_files = [root / ".gitignore", root / "unified_ocr_app" / ".gitignore"]
    lines = []
    for gitignore in gitignore_files:
        if gitignore.exists():
            lines.extend(line.strip() for line in gitignore.read_text(encoding="utf-8").splitlines())
    present = {line for line in lines if line and not line.startswith("#")}
    missing = [pattern for pattern in REQUIRED_GITIGNORE_PATTERNS if pattern not in present]
    return CheckResult(
        name="gitignore_rules",
        status="ok" if not missing else "warning",
        details=[f"Gitignore should include: {pattern}" for pattern in missing],
    )


def check_sensitive_files(root: Path) -> CheckResult:
    offenders = []
    for path in _iter_files(root):
        relative = _as_posix(path.relative_to(root))
        if _matches_any(relative, SENSITIVE_FILE_PATTERNS):
            offenders.append(f"Sensitive/local file present: {relative}")
    return CheckResult(
        name="sensitive_files",
        status="ok" if not offenders else "failed",
        details=offenders,
    )


def check_secret_markers(root: Path) -> CheckResult:
    offenders = []
    for path in _iter_files(root):
        relative = _as_posix(path.relative_to(root))
        if _matches_any(relative, SENSITIVE_FILE_PATTERNS):
            continue
        text = _read_text(path)
        if text is None:
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if any(pattern.search(line) for pattern in SECRET_PATTERNS):
                offenders.append(f"Potential secret in {relative}:{line_no}")
                break
    return CheckResult(
        name="secret_markers",
        status="ok" if not offenders else "failed",
        details=offenders,
    )


def run_release_check(root: str | Path = ".") -> dict:
    root_path = _resolve_project_root(root)
    checks = [
        check_required_files(root_path),
        check_dependency_metadata(root_path),
        check_gitignore_rules(root_path),
        check_sensitive_files(root_path),
        check_secret_markers(root_path),
    ]
    has_failed = any(check.status == "failed" for check in checks)
    has_warning = any(check.status == "warning" for check in checks)
    status = "failed" if has_failed else ("warning" if has_warning else "ok")
    return {
        "schema": "unified_ocr_release_check_v1",
        "root": str(root_path),
        "status": status,
        "checks": [check.to_dict() for check in checks],
    }


def format_release_check(result: dict) -> str:
    lines = [f"Release-Check: {result.get('status', 'unknown').upper()}"]
    for check in result.get("checks", []):
        lines.append(f"- {check['name']}: {check['status']}")
        for detail in check.get("details", []):
            lines.append(f"  - {detail}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run local release-readiness checks.")
    parser.add_argument("root", nargs="?", default=".", help="Project root to check.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args(argv)

    result = run_release_check(args.root)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(format_release_check(result))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
