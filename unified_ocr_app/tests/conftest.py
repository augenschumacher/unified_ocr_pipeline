from pathlib import Path
import sys


PACKAGE_DIR = Path(__file__).resolve().parents[1]
PARENT_DIR = PACKAGE_DIR.parent

for path in (str(PACKAGE_DIR), str(PARENT_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)
