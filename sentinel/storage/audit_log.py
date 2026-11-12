# Audit Log - SQLite Storage for Actions and Undo

import json
import os
import sqlite3
from datetime import datetime
from typing import Any

from sentinel.paths import audit_db_path
from sentinel.storage.db_schema_sql import DB_SCHEMA

DB_PATH = str(audit_db_path())


def init_db():
    """Initialize the audit database."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(DB_SCHEMA)
    conn.commit()
    conn.close()


def log_action(action: str, params: dict[str, Any], tier: str, undo_data: dict[str, Any] = None) -> int:
    """Log an action before execution. Returns the log ID."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """INSERT INTO audit_log (timestamp, action, params, tier, undo_data, status)
           VALUES (?, ?, ?, ?, ?, 'executed')""",
        (datetime.now().isoformat(), action, json.dumps(params), tier, json.dumps(undo_data) if undo_data else None)
    )
    log_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return log_id


def update_result(log_id: int, result: dict[str, Any], undo_data: dict[str, Any] | None = None):
    """Update the result of an action after execution, storing undo_data if available."""
    if undo_data is None and isinstance(result, dict) and "undo_data" in result:
        undo_data = result["undo_data"]
        
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    if undo_data is not None:
        cursor.execute(
            "UPDATE audit_log SET result = ?, status = ?, undo_data = ? WHERE id = ?",
            (json.dumps(result), 'executed' if result.get('success') else 'failed', json.dumps(undo_data), log_id)
        )
    else:
        cursor.execute(
            "UPDATE audit_log SET result = ?, status = ? WHERE id = ?",
            (json.dumps(result), 'executed' if result.get('success') else 'failed', log_id)
        )
    conn.commit()
    conn.close()


def get_last_action(reversible_only: bool = True) -> dict[str, Any] | None:
    """Get the last executed action for undo."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    if reversible_only:
        cursor.execute(
            "SELECT * FROM audit_log WHERE status = 'executed' AND tier IN ('reversible', 'destructive') ORDER BY id DESC LIMIT 1"
        )
    else:
        cursor.execute(
            "SELECT * FROM audit_log WHERE status = 'executed' ORDER BY id DESC LIMIT 1"
        )
    row = cursor.fetchone()
    conn.close()
    if row:
        return dict(row)
    return None


def mark_undone(log_id: int):
    """Mark an action as undone."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE audit_log SET status = 'undone' WHERE id = ?", (log_id,))
    conn.commit()
    conn.close()


def get_recent_actions(limit: int = 10) -> list[dict[str, Any]]:
    """Get recent actions for display in TUI."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]