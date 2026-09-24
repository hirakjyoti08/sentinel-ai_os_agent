import sys
from pathlib import Path

# Ensure project root is in sys.path when invoked directly as `python src/main.py`
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.ui.tui import run_tui


def main():
    """Entry point for the AI OS Agent."""
    run_tui()


if __name__ == "__main__":
    main()