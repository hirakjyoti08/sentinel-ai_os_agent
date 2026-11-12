import sys
from pathlib import Path

# Allow `python sentinel/main.py` from a source checkout (the console script
# and `python -m sentinel` don't need this).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sentinel.ui.tui import run_tui  # noqa: E402


def main():
    """Entry point for the AI OS Agent."""
    run_tui()


if __name__ == "__main__":
    main()
