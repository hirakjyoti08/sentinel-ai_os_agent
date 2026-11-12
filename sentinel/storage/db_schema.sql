-- Database Schema for Audit Log
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    action TEXT NOT NULL,
    params TEXT NOT NULL,           -- JSON
    tier TEXT NOT NULL,
    result TEXT,                    -- JSON
    undo_data TEXT,                 -- JSON for reversal
    status TEXT NOT NULL DEFAULT 'executed'  -- 'executed' | 'failed' | 'undone'
);

CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);
CREATE INDEX IF NOT EXISTS idx_audit_status ON audit_log(status);