# Database Schema as Python string for easy import

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    action TEXT NOT NULL,
    params TEXT NOT NULL,
    tier TEXT NOT NULL,
    result TEXT,
    undo_data TEXT,
    status TEXT NOT NULL DEFAULT 'executed'
);

CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);
CREATE INDEX IF NOT EXISTS idx_audit_status ON audit_log(status);
"""