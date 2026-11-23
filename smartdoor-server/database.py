"""Small SQLite event and member store with deterministic connection closing."""
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

DB_PATH = Path(__file__).resolve().parent / "smartdoor.db"


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def _session():
    conn = connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db():
    with _session() as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS members (
                user_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                user_id TEXT,
                display_name TEXT,
                result TEXT NOT NULL,
                face_score REAL,
                spoof_score REAL,
                voice_score REAL,
                image_path TEXT
            )"""
        )


def upsert_member(user_id, display_name=None):
    name = (display_name or "").strip() or user_id
    with _session() as db:
        db.execute(
            """INSERT INTO members(user_id, display_name, created_at) VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET display_name=excluded.display_name""",
            (user_id, name, datetime.now(timezone.utc).isoformat()),
        )


def ensure_member(user_id, display_name=None):
    """Đảm bảo user_id có mặt trong bảng members mà không ghi đè tên đã đặt."""
    name = (display_name or "").strip() or user_id
    with _session() as db:
        db.execute(
            "INSERT OR IGNORE INTO members(user_id, display_name, created_at) VALUES (?, ?, ?)",
            (user_id, name, datetime.now(timezone.utc).isoformat()),
        )


def get_member_name(user_id):
    if not user_id:
        return None
    with _session() as db:
        row = db.execute("SELECT display_name FROM members WHERE user_id=?", (user_id,)).fetchone()
        return row["display_name"] if row else user_id


def member_name_map():
    with _session() as db:
        return {r["user_id"]: r["display_name"] for r in db.execute("SELECT user_id, display_name FROM members")}


def delete_member(user_id):
    with _session() as db:
        db.execute("DELETE FROM members WHERE user_id=?", (user_id,))


def list_members():
    with _session() as db:
        return [dict(r) for r in db.execute("SELECT * FROM members ORDER BY display_name, user_id")]


def add_event(
    result,
    user_id=None,
    display_name=None,
    face_score=None,
    spoof_score=None,
    voice_score=None,
    image_path=None,
):
    with _session() as db:
        cur = db.execute(
            """INSERT INTO events(timestamp, user_id, display_name, result, face_score, spoof_score, voice_score, image_path)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now(timezone.utc).isoformat(),
                user_id,
                display_name,
                result,
                face_score,
                spoof_score,
                voice_score,
                image_path,
            ),
        )
        return cur.lastrowid


def recent_events(limit=100):
    bounded = min(max(int(limit), 1), 500)
    with _session() as db:
        return [dict(r) for r in db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (bounded,))]
