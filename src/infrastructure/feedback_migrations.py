"""Versioned SQLite schema migrations for governed feedback."""

from __future__ import annotations

import sqlite3

FEEDBACK_SCHEMA_VERSION = 1


def migrate_feedback_schema(connection: sqlite3.Connection) -> None:
    current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if current_version > FEEDBACK_SCHEMA_VERSION:
        raise RuntimeError(
            "feedback database schema is newer than this application: "
            f"database={current_version}, supported={FEEDBACK_SCHEMA_VERSION}"
        )
    if current_version < 1:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS feedback_samples (
                signature TEXT PRIMARY KEY,
                tier TEXT NOT NULL CHECK (tier IN ('pending', 'gold', 'negative')),
                question TEXT NOT NULL,
                sql TEXT NOT NULL,
                candidate_tables_json TEXT NOT NULL DEFAULT '[]',
                candidate_score_reasons_json TEXT NOT NULL DEFAULT '{}',
                promotion_evidence_json TEXT NOT NULL DEFAULT '{}',
                quality_flags_json TEXT NOT NULL DEFAULT '[]',
                error_types_json TEXT NOT NULL DEFAULT '[]',
                execution_succeeded INTEGER NOT NULL DEFAULT 0,
                result_row_count INTEGER NOT NULL DEFAULT 0,
                approved INTEGER NOT NULL DEFAULT 0,
                user_validated INTEGER NOT NULL DEFAULT 0,
                quality_score INTEGER NOT NULL DEFAULT 0,
                capture_source TEXT NOT NULL,
                status TEXT NOT NULL,
                reviewer TEXT NOT NULL DEFAULT '',
                captured_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_feedback_tier_question
                ON feedback_samples(tier, question);
            CREATE TABLE IF NOT EXISTS feedback_reviews (
                id TEXT PRIMARY KEY,
                question TEXT NOT NULL,
                sql TEXT NOT NULL,
                validation_label TEXT NOT NULL,
                reviewer TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                submitted_at TEXT NOT NULL
            );
            PRAGMA user_version = 1;
            """
        )
