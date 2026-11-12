"""Filesystem locations for Sentinel's runtime data (audit DB, trash, logs).

Resolution order:
1. ``SENTINEL_HOME`` environment variable, if set.
2. The repository root, when running from a source checkout (keeps the
   existing ``logs/`` and ``.trash/`` layout for development).
3. ``~/.sentinel`` for an installed package (e.g. ``pipx install``).
"""

import os
from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _PACKAGE_DIR.parent


def data_dir() -> Path:
    env = os.environ.get("SENTINEL_HOME")
    if env:
        return Path(env).expanduser()
    if (_REPO_ROOT / "pyproject.toml").exists():
        return _REPO_ROOT
    return Path.home() / ".sentinel"


def audit_db_path() -> Path:
    return data_dir() / "logs" / "audit.db"


def trash_dir() -> Path:
    return data_dir() / ".trash"
