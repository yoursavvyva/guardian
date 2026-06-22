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
                trash_result TEXT,
                trash_acknowledged INTEGER NOT NULL DEFAULT 0,
                trash_acknowledged_at TEXT,
                trash_acknowledged_by TEXT,
                answered_attempt_number INTEGER,
                escalation_sent INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                acknowledged INTEGER NOT NULL DEFAULT 0,
                acknowledged_at TEXT,
                acknowledged_by TEXT,
                reminder_count INTEGER NOT NULL DEFAULT 0,
                last_reminder_at TEXT,
                source TEXT NOT NULL DEFAULT 'scheduled',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS guardian_meta (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS guardian_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                action TEXT NOT NULL,
                actor TEXT,
                chat_id TEXT,
                checkin_id INTEGER,
                detail TEXT
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
        # Additive migrations for pre-existing databases.
        cols = [r[1] for r in c.execute("PRAGMA table_info(guardian_checkins)").fetchall()]
        if "wellness_result" not in cols:                                  # ANGEL-05
            c.execute("ALTER TABLE guardian_checkins ADD COLUMN wellness_result TEXT")
        for col, ddl in [                                                  # trash-day rider + its ack
            ("trash_result", "ALTER TABLE guardian_checkins ADD COLUMN trash_result TEXT"),
            ("trash_acknowledged", "ALTER TABLE guardian_checkins ADD COLUMN trash_acknowledged INTEGER NOT NULL DEFAULT 0"),
            ("trash_acknowledged_at", "ALTER TABLE guardian_checkins ADD COLUMN trash_acknowledged_at TEXT"),
            ("trash_acknowledged_by", "ALTER TABLE guardian_checkins ADD COLUMN trash_acknowledged_by TEXT"),
        ]:
            if col not in cols:
                c.execute(ddl)
        for col, ddl in [                                                  # ANGEL-06 (call-back ack)
            ("acknowledged", "ALTER TABLE guardian_checkins ADD COLUMN acknowledged INTEGER NOT NULL DEFAULT 0"),
            ("acknowledged_at", "ALTER TABLE guardian_checkins ADD COLUMN acknowledged_at TEXT"),
            ("acknowledged_by", "ALTER TABLE guardian_checkins ADD COLUMN acknowledged_by TEXT"),
            ("reminder_count", "ALTER TABLE guardian_checkins ADD COLUMN reminder_count INTEGER NOT NULL DEFAULT 0"),
            ("last_reminder_at", "ALTER TABLE guardian_checkins ADD COLUMN last_reminder_at TEXT"),
            ("source", "ALTER TABLE guardian_checkins ADD COLUMN source TEXT NOT NULL DEFAULT 'scheduled'"),  # ANGEL-08
        ]:
            if col not in cols:
                c.execute(ddl)


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


def record_inbound_checkin(scheduled_time_iso, final_status, wellness_result=None, source="inbound"):
    """ANGEL-08: persist a check-in that originated from Mom CALLING Angel (inbound),
    not from a scheduled outbound attempt. No call attempts are attached."""
    with _LOCK, _conn() as c:
        cur = c.execute(
            "INSERT INTO guardian_checkins "
            "(scheduled_time, final_status, wellness_result, source, answered_attempt_number, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 0, ?, ?)",
            (scheduled_time_iso, final_status, wellness_result, source, _now(), _now()),
        )
        return cur.lastrowid


# ---- meta key/value (ANGEL-08: last_callback_time / last_callback_outcome for reporting) ----
def set_meta(key, value):
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT INTO guardian_meta (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, _now()),
        )


def get_meta(key, default=None):
    with _LOCK, _conn() as c:
        r = c.execute("SELECT value FROM guardian_meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default


def get_meta_all():
    with _LOCK, _conn() as c:
        return {r["key"]: r["value"] for r in c.execute("SELECT key, value FROM guardian_meta").fetchall()}


# ---- audit log (ANGEL-10: who tapped which control button, and when) ----
def add_audit(action, actor=None, chat_id=None, checkin_id=None, detail=None):
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT INTO guardian_audit (ts, action, actor, chat_id, checkin_id, detail) VALUES (?, ?, ?, ?, ?, ?)",
            (_now(), action, actor, str(chat_id) if chat_id is not None else None, checkin_id, detail),
        )
    print(f"[guardian.audit] {action} by={actor} chat={chat_id} checkin={checkin_id} detail={detail}", flush=True)


def recent_audit(limit=20):
    with _LOCK, _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM guardian_audit ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()]


# ---- ANGEL-10: manual resolution of an active check-in via a Telegram button ----
def resolve_manual_ok(checkin_id, by="darcee"):
    """Mark a check-in manually confirmed OK (Darcee tapped 'Mom is OK'). Sets the
    distinct status + source so the record shows a human resolved it, and clears any
    pending retry. Returns the updated row (or None)."""
    with _LOCK, _conn() as c:
        c.execute(
            "UPDATE guardian_checkins SET final_status='manually_confirmed_ok', wellness_result='okay', "
            "source='telegram_darcee', acknowledged=1, acknowledged_at=?, acknowledged_by=?, "
            "next_attempt_at=NULL, updated_at=? WHERE id=?",
            (_now(), by, _now(), checkin_id),
        )
        r = c.execute("SELECT * FROM guardian_checkins WHERE id=?", (checkin_id,)).fetchone()
        return dict(r) if r else None


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


# ---- ANGEL-06: call-back acknowledgments ----
def unacked_needs_darcee():
    """needs_darcee check-ins Darcee hasn't acknowledged calling back yet (oldest first)."""
    with _LOCK, _conn() as c:
        rows = c.execute(
            "SELECT * FROM guardian_checkins "
            "WHERE final_status='needs_darcee' AND COALESCE(acknowledged,0)=0 "
            "ORDER BY scheduled_time ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def acknowledge_checkin(checkin_id, by="darcee"):
    """Mark a needs_darcee check-in as called-back. Returns the updated row (or None)."""
    with _LOCK, _conn() as c:
        cur = c.execute(
            "UPDATE guardian_checkins SET acknowledged=1, acknowledged_at=?, acknowledged_by=?, "
            "next_attempt_at=NULL, updated_at=? "
            "WHERE id=? AND final_status='needs_darcee'",
            (_now(), by, _now(), checkin_id),
        )
        if cur.rowcount == 0:
            r = c.execute("SELECT * FROM guardian_checkins WHERE id=?", (checkin_id,)).fetchone()
            return dict(r) if r else None
        r = c.execute("SELECT * FROM guardian_checkins WHERE id=?", (checkin_id,)).fetchone()
        return dict(r) if r else None


def latest_trash_answer():
    """Most recent check-in that actually collected a trash answer (newest first), or None."""
    with _LOCK, _conn() as c:
        r = c.execute(
            "SELECT * FROM guardian_checkins "
            "WHERE trash_result IS NOT NULL AND trash_result != '' "
            "ORDER BY scheduled_time DESC LIMIT 1"
        ).fetchone()
        return dict(r) if r else None


def acknowledge_trash_checkin(checkin_id, by="sister"):
    """Mark a trash-day answer as received/acknowledged. Returns the updated row (or None)."""
    with _LOCK, _conn() as c:
        c.execute(
            "UPDATE guardian_checkins SET trash_acknowledged=1, trash_acknowledged_at=?, "
            "trash_acknowledged_by=?, updated_at=? "
            "WHERE id=? AND COALESCE(trash_acknowledged,0)=0",
            (_now(), by, _now(), checkin_id),
        )
        r = c.execute("SELECT * FROM guardian_checkins WHERE id=?", (checkin_id,)).fetchone()
        return dict(r) if r else None


def mark_reminded(checkin_id):
    with _LOCK, _conn() as c:
        c.execute(
            "UPDATE guardian_checkins SET reminder_count=COALESCE(reminder_count,0)+1, "
            "last_reminder_at=?, updated_at=? WHERE id=?",
            (_now(), _now(), checkin_id),
        )


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
