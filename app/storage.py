"""
SQLite storage for Guardian (Phase 1). PMC must NOT read this file directly —
it consumes Guardian's HTTP API only. A clean boundary now eases a future move
to Postgres/another store.
"""
import os
import sqlite3
import threading
from datetime import datetime, timezone

from app.config import settings

_LOCK = threading.Lock()


def _conn():
    os.makedirs(os.path.dirname(settings.db_path), exist_ok=True)
    c = sqlite3.connect(settings.db_path, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with _LOCK, _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS guardian_checkins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scheduled_time TEXT NOT NULL,
                final_status TEXT NOT NULL DEFAULT 'pending',
                wellness_result TEXT,
                answered_attempt_number INTEGER,
                escalation_sent INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS guardian_call_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scheduled_check_id INTEGER NOT NULL,
                scheduled_time TEXT,
                attempt_number INTEGER NOT NULL,
                target_type TEXT,
                target_value TEXT,
                status TEXT,
                provider TEXT,
                started_at TEXT,
                ended_at TEXT,
                error_message TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_checkins_sched ON guardian_checkins(scheduled_time);
            CREATE INDEX IF NOT EXISTS ix_attempts_checkin ON guardian_call_attempts(scheduled_check_id);
            """
        )
        # ANGEL-05 migration: add wellness_result to pre-existing databases.
        cols = [r[1] for r in c.execute("PRAGMA table_info(guardian_checkins)").fetchall()]
        if "wellness_result" not in cols:
            c.execute("ALTER TABLE guardian_checkins ADD COLUMN wellness_result TEXT")


def _now():
    return datetime.now(timezone.utc).isoformat()


# ---- check-ins ----
def create_checkin(scheduled_time_iso):
    with _LOCK, _conn() as c:
        cur = c.execute(
            "INSERT INTO guardian_checkins (scheduled_time, final_status, created_at, updated_at) "
            "VALUES (?, 'pending', ?, ?)",
            (scheduled_time_iso, _now(), _now()),
        )
        return cur.lastrowid


def update_checkin(checkin_id, **fields):
    if not fields:
        return
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in fields)
    with _LOCK, _conn() as c:
        c.execute(f"UPDATE guardian_checkins SET {cols} WHERE id=?", (*fields.values(), checkin_id))


def get_checkin(checkin_id):
    with _LOCK, _conn() as c:
        r = c.execute("SELECT * FROM guardian_checkins WHERE id=?", (checkin_id,)).fetchone()
        return dict(r) if r else None


def checkin_exists_for(scheduled_time_iso):
    with _LOCK, _conn() as c:
        r = c.execute("SELECT id FROM guardian_checkins WHERE scheduled_time=?", (scheduled_time_iso,)).fetchone()
        return r["id"] if r else None


def open_checkins():
    """Pending check-ins (attempts still in progress)."""
    with _LOCK, _conn() as c:
        rows = c.execute("SELECT * FROM guardian_checkins WHERE final_status='pending'").fetchall()
        return [dict(r) for r in rows]


def checkins_between(date_from=None, date_to=None, limit=50):
    q = "SELECT * FROM guardian_checkins"
    clauses, args = [], []
    if date_from:
        clauses.append("scheduled_time >= ?"); args.append(date_from)
    if date_to:
        clauses.append("scheduled_time <= ?"); args.append(date_to)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY scheduled_time DESC LIMIT ?"
    args.append(int(limit))
    with _LOCK, _conn() as c:
        return [dict(r) for r in c.execute(q, args).fetchall()]


def last_checkin():
    with _LOCK, _conn() as c:
        r = c.execute("SELECT * FROM guardian_checkins ORDER BY scheduled_time DESC LIMIT 1").fetchone()
        return dict(r) if r else None


def last_answered():
    with _LOCK, _conn() as c:
        r = c.execute(
            "SELECT * FROM guardian_checkins WHERE final_status='answered' ORDER BY scheduled_time DESC LIMIT 1"
        ).fetchone()
        return dict(r) if r else None


# ---- attempts ----
def create_attempt(checkin_id, scheduled_time, attempt_number, target_type, target_value, provider):
    with _LOCK, _conn() as c:
        cur = c.execute(
            "INSERT INTO guardian_call_attempts "
            "(scheduled_check_id, scheduled_time, attempt_number, target_type, target_value, status, provider, started_at) "
            "VALUES (?, ?, ?, ?, ?, 'placed', ?, ?)",
            (checkin_id, scheduled_time, attempt_number, target_type, target_value, provider, _now()),
        )
        return cur.lastrowid


def finish_attempt(attempt_id, status, error_message=None):
    with _LOCK, _conn() as c:
        c.execute(
            "UPDATE guardian_call_attempts SET status=?, ended_at=?, error_message=? WHERE id=?",
            (status, _now(), error_message, attempt_id),
        )


def attempts_for(checkin_id):
    with _LOCK, _conn() as c:
        rows = c.execute(
            "SELECT * FROM guardian_call_attempts WHERE scheduled_check_id=? ORDER BY attempt_number", (checkin_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def attempts_between(checkin_id=None, date_from=None, date_to=None, limit=200):
    q = "SELECT * FROM guardian_call_attempts"
    clauses, args = [], []
    if checkin_id:
        clauses.append("scheduled_check_id = ?"); args.append(int(checkin_id))
    if date_from:
        clauses.append("started_at >= ?"); args.append(date_from)
    if date_to:
        clauses.append("started_at <= ?"); args.append(date_to)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    with _LOCK, _conn() as c:
        return [dict(r) for r in c.execute(q, args).fetchall()]
