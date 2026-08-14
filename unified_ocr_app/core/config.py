import os
import logging
from pathlib import Path

logger = logging.getLogger("UnifiedOCR")

def setup_paths():
    # Add Ghostscript
    program_files = [Path(os.environ.get("ProgramFiles", r"C:\Program Files"))]
    if os.environ.get("ProgramFiles(x86)"):
        program_files.append(Path(os.environ.get("ProgramFiles(x86)")))
    candidates = []
    for root in program_files:
        gs_root = root / "gs"
        if gs_root.exists():
            candidates.extend(sorted(gs_root.glob("gs*\\bin"), reverse=True))
    existing_path = os.environ.get("PATH", "")
    for candidate in candidates:
        if (candidate / "gswin64c.exe").exists() or (candidate / "gswin32c.exe").exists():
            if str(candidate).lower() not in existing_path.lower():
                os.environ["PATH"] = str(candidate) + os.pathsep + existing_path
            break
            
    # Add Tesseract
    tess_candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tesseract-OCR",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Tesseract-OCR",
    ]
    existing_path = os.environ.get("PATH", "")
    for candidate in tess_candidates:
        if (candidate / "tesseract.exe").exists():
            if str(candidate).lower() not in existing_path.lower():
                os.environ["PATH"] = str(candidate) + os.pathsep + existing_path
            break

    # Add QPDF for OCRmyPDF on Windows.
    qpdf_candidates = []
    for root in program_files:
        qpdf_root = root / "qpdf"
        if qpdf_root.exists():
            qpdf_candidates.extend(sorted(qpdf_root.glob("**\\bin"), reverse=True))
        qpdf_candidates.extend(sorted(root.glob("qpdf*\\bin"), reverse=True))
    existing_path = os.environ.get("PATH", "")
    for candidate in qpdf_candidates:
        if (candidate / "qpdf.exe").exists():
            if str(candidate).lower() not in existing_path.lower():
                os.environ["PATH"] = str(candidate) + os.pathsep + existing_path
            break

    # Add Microsoft Store app execution aliases such as winget.exe.
    windows_apps = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps"
    existing_path = os.environ.get("PATH", "")
    if (windows_apps / "winget.exe").exists() and str(windows_apps).lower() not in existing_path.lower():
        os.environ["PATH"] = str(windows_apps) + os.pathsep + existing_path

def setup_logging(base_dir: Path) -> logging.Logger:
    log_dir = base_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "pipeline.log"
    
    logger = logging.getLogger("UnifiedOCR")
    logger.setLevel(logging.DEBUG)
    
    # Verhindern, dass Handler doppelt hinzugefügt werden
    if not logger.handlers:
        # File Handler (schreibt detailliert)
        fh = logging.FileHandler(str(log_file), encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(name)s - %(message)s')
        fh.setFormatter(formatter)
        logger.addHandler(fh)
        
        # Console Handler (schreibt Fehler auf stderr)
        ch = logging.StreamHandler()
        ch.setLevel(logging.WARNING)
        ch.setFormatter(formatter)
        logger.addHandler(ch)
        
    return logger

class AppConfig:
    def __init__(self, base_dir: str, additional_consume_dirs=None, large_pdf_page_limit: int = 20):
        self.base_dir = Path(base_dir)
        self.consume_dir = self.base_dir / "consume"
        self.original_dir = self.base_dir / "original"
        self.final_dir = self.base_dir / "final"
        self.error_dir = self.base_dir / "error"
        self.log_dir = self.base_dir / "logs"
        self.work_dir = self.base_dir / "work"
        self.large_pdf_page_limit = self._coerce_large_pdf_page_limit(large_pdf_page_limit)
        self.additional_consume_dirs = []
        self.set_additional_consume_dirs(additional_consume_dirs or [])

    @staticmethod
    def _coerce_large_pdf_page_limit(value) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = 20
        return max(1, min(parsed, 1000))

    def _path_key(self, path: Path) -> str:
        try:
            return str(path.resolve(strict=False)).lower()
        except Exception:
            return str(path.absolute()).lower()

    @property
    def consume_dirs(self) -> list[Path]:
        dirs = [self.consume_dir]
        seen = {self._path_key(self.consume_dir)}
        for directory in self.additional_consume_dirs:
            key = self._path_key(directory)
            if key not in seen:
                dirs.append(directory)
                seen.add(key)
        return dirs

    def set_additional_consume_dirs(self, directories):
        primary_key = self._path_key(self.consume_dir)
        cleaned = []
        seen = {primary_key}
        for raw in directories or []:
            if not raw:
                continue
            path = Path(str(raw)).expanduser()
            if not path.is_absolute():
                path = self.base_dir / path
            key = self._path_key(path)
            if key in seen:
                continue
            cleaned.append(path)
            seen.add(key)
        self.additional_consume_dirs = cleaned

    def source_consume_dir_for(self, file_path: Path) -> Path | None:
        try:
            parent = Path(file_path).parent.resolve(strict=False)
        except Exception:
            parent = Path(file_path).parent
        for directory in self.consume_dirs:
            try:
                if parent == directory.resolve(strict=False):
                    return directory
            except Exception:
                if parent == directory:
                    return directory
        return None
        
    def ensure_directories(self):
        for d in [*self.consume_dirs, self.original_dir, self.final_dir, self.error_dir, self.log_dir, self.work_dir]:
            d.mkdir(parents=True, exist_ok=True)
            
    def cleanup_error_dir(self, preserve=None, *, max_entries: int | None = None):
        """Explicit legacy retention helper.

        Error artifacts can contain the only recoverable copy of an input.  They
        are therefore never removed merely because another job failed.  Callers
        must opt in with a positive ``max_entries`` value; the normal pipeline
        intentionally never does so.
        """
        if max_entries is None:
            return []
        max_entries = max(0, int(max_entries))
        preserve_keys = {self._path_key(Path(path)) for path in (preserve or [])}
        removed = []
        try:
            files = sorted(self.error_dir.glob("*"), key=lambda f: f.stat().st_mtime, reverse=True)
            kept = 0
            for f in files:
                if self._path_key(f) in preserve_keys:
                    continue
                kept += 1
                if kept <= max_entries:
                    continue
                try:
                    if f.is_file():
                        f.unlink()
                    elif f.is_dir():
                        import shutil
                        shutil.rmtree(f)
                    removed.append(f)
                except Exception:
                    logger.warning(
                        "Alt-Artefakt %s konnte nicht entfernt werden.", f, exc_info=True
                    )
        except Exception:
            logger.warning(
                "Aufraeumen alter Artefakte wurde abgebrochen.", exc_info=True
            )
        return removed
