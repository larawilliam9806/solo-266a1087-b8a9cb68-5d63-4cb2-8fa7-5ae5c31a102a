"""SQLite helpers and schema for the item-analysis app."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS forms (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

-- One row per (form, printed question number, printed option letter).
-- std_code / std_option are the canonical question code and option.
CREATE TABLE IF NOT EXISTS form_mappings (
    form_id    INTEGER NOT NULL REFERENCES forms(id) ON DELETE CASCADE,
    q_no       TEXT NOT NULL,
    letter     TEXT NOT NULL,
    std_code   TEXT NOT NULL,
    std_option TEXT NOT NULL,
    PRIMARY KEY (form_id, q_no, letter)
);
CREATE INDEX IF NOT EXISTS idx_form_mappings_code
    ON form_mappings(form_id, std_code);

CREATE TABLE IF NOT EXISTS answer_rows (
    card_no      TEXT NOT NULL,
    q_no         TEXT NOT NULL,
    letter       TEXT NOT NULL DEFAULT '',
    scanned_code TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (card_no, q_no)
);
CREATE INDEX IF NOT EXISTS idx_answer_rows_card ON answer_rows(card_no);

CREATE TABLE IF NOT EXISTS review_decisions (
    card_no    TEXT NOT NULL,
    std_code   TEXT NOT NULL,
    resolution TEXT NOT NULL,          -- opt:<value> | blank | skip
    note       TEXT NOT NULL DEFAULT '',
    decided_at TEXT NOT NULL,
    PRIMARY KEY (card_no, std_code)
);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    summary_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS run_question_stats (
    run_id     INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    std_code   TEXT NOT NULL,
    counted    INTEGER NOT NULL DEFAULT 0,
    blank      INTEGER NOT NULL DEFAULT 0,
    flagged    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, std_code)
);

CREATE TABLE IF NOT EXISTS run_option_counts (
    run_id     INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    std_code   TEXT NOT NULL,
    std_option TEXT NOT NULL,
    cnt        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, std_code, std_option)
);

CREATE TABLE IF NOT EXISTS run_card_results (
    run_id     INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    card_no    TEXT NOT NULL,
    std_code   TEXT NOT NULL,
    status     TEXT NOT NULL,          -- counted | blank | flagged | skipped
    std_option TEXT,
    detail     TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, card_no, std_code)
);
CREATE INDEX IF NOT EXISTS idx_run_card_results_status
    ON run_card_results(run_id, status);

CREATE TABLE IF NOT EXISTS run_unmatched (
    run_id  INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    card_no TEXT NOT NULL,
    detail  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, card_no)
);

CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    status      TEXT NOT NULL,          -- queued | running | succeeded | failed
    error       TEXT,
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT,
    run_id      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);

-- Scoring (成绩划线): one scoring table per replace; uploading a new table
-- never creates an analysis run, so the item-snapshot id stays unchanged.
CREATE TABLE IF NOT EXISTS score_tables (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pass_line   INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS score_items (
    score_id   INTEGER NOT NULL REFERENCES score_tables(id) ON DELETE CASCADE,
    std_code   TEXT NOT NULL,
    std_option TEXT NOT NULL,
    points     INTEGER NOT NULL,
    PRIMARY KEY (score_id, std_code)
);

-- One grading row per (snapshot, card).  A run with invalid score items is
-- persisted with paused = 1 and no card grades; the statistics snapshot itself
-- is published normally.
CREATE TABLE IF NOT EXISTS run_grades_meta (
    run_id      INTEGER PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    score_id    INTEGER NOT NULL REFERENCES score_tables(id),
    pass_line   INTEGER NOT NULL,
    paused      INTEGER NOT NULL DEFAULT 0,
    invalid_json TEXT NOT NULL DEFAULT '[]',
    graded_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_grades (
    run_id       INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    card_no      TEXT NOT NULL,
    min_score    INTEGER NOT NULL,
    max_score    INTEGER NOT NULL,
    min_form     TEXT NOT NULL DEFAULT '',
    max_form     TEXT NOT NULL DEFAULT '',
    verdict      TEXT NOT NULL,         -- pass | fail | rescan
    scores_json  TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (run_id, card_no)
);
CREATE INDEX IF NOT EXISTS idx_run_grades_verdict ON run_grades(run_id, verdict);
"""

DEFAULT_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "app.db")


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def connect(db_path: str) -> sqlite3.Connection:
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('active_run_id', '0')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('state_version', '0')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('active_score_id', '0')"
        )
        # Jobs interrupted by a restart are safe to replay: the payload is
        # persisted and each job's transaction either committed or rolled back.
        conn.execute(
            "UPDATE jobs SET status = 'queued', started_at = NULL "
            "WHERE status = 'running'"
        )
    finally:
        conn.close()


def get_meta(conn: sqlite3.Connection, key: str, default: str = "0") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def active_run_id(conn: sqlite3.Connection) -> int:
    try:
        return int(get_meta(conn, "active_run_id", "0"))
    except (TypeError, ValueError):
        return 0
