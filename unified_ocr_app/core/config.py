import os
import logging
from pathlib import Path

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
    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir)
        self.consume_dir = self.base_dir / "consume"
        self.original_dir = self.base_dir / "original"
        self.final_dir = self.base_dir / "final"
        self.error_dir = self.base_dir / "error"
        self.log_dir = self.base_dir / "logs"
        self.work_dir = self.base_dir / "work"
        self.large_pdf_page_limit = 20
        
    def ensure_directories(self):
        for d in [self.consume_dir, self.original_dir, self.final_dir, self.error_dir, self.log_dir, self.work_dir]:
            d.mkdir(parents=True, exist_ok=True)
            
    def cleanup_error_dir(self):
        try:
            files = sorted(self.error_dir.glob("*"), key=lambda f: f.stat().st_mtime, reverse=True)
            for f in files[3:]:
                try:
                    if f.is_file():
                        f.unlink()
                    elif f.is_dir():
                        import shutil
                        shutil.rmtree(f)
                except Exception:
                    pass
        except Exception:
            pass
