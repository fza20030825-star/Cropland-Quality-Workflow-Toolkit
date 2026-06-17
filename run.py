"""Project entry point.

Run from VS Code or terminal:
    python run.py
"""

from pathlib import Path
import sys


WORKSPACE_ROOT = Path(__file__).resolve().parent
SRC_DIR = WORKSPACE_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cropland_quality_update.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
