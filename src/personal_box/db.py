import sqlite3
from contextlib import contextmanager
from typing import Optional

DB_PATH = "newsletters.db"


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS newsletters (
                id           TEXT PRIMARY KEY,
                subject      TEXT,
                sender_name  TEXT,
                sender_email TEXT,
                date         TEXT,
                category     TEXT,
                html_body    TEXT,
                text_body    TEXT,
                is_read      INTEGER DEFAULT 0,
                folder       TEXT
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_category ON newsletters(category)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_date ON newsletters(date)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sync_state (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)


def upsert_newsletter(data: dict):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO newsletters (id, subject, sender_name, sender_email, date,
                                     category, html_body, text_body, is_read, folder)
            VALUES (:id, :subject, :sender_name, :sender_email, :date,
                    :category, :html_body, :text_body, :is_read, :folder)
            ON CONFLICT(id) DO UPDATE SET
                subject      = excluded.subject,
                sender_name  = excluded.sender_name,
                sender_email = excluded.sender_email,
                date         = excluded.date,
                category     = excluded.category,
                html_body    = excluded.html_body,
                text_body    = excluded.text_body,
                folder       = excluded.folder
        """, data)


def get_categories_with_counts() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT category, COUNT(*) as count
            FROM newsletters
            GROUP BY category
            ORDER BY count DESC
        """).fetchall()
        return [dict(r) for r in rows]


def get_by_category(category: str, limit: int = 50, offset: int = 0) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT id, subject, sender_name, sender_email, date, category, is_read
            FROM newsletters
            WHERE category = ?
            ORDER BY date DESC
            LIMIT ? OFFSET ?
        """, (category, limit, offset)).fetchall()
        return [dict(r) for r in rows]


def get_by_date_range(
    start: str, end: str, category: Optional[str] = None,
    limit: int = 50, offset: int = 0
) -> list[dict]:
    with get_conn() as conn:
        if category:
            rows = conn.execute("""
                SELECT id, subject, sender_name, sender_email, date, category, is_read
                FROM newsletters
                WHERE date >= ? AND date <= ? AND category = ?
                ORDER BY date DESC
                LIMIT ? OFFSET ?
            """, (start, end, category, limit, offset)).fetchall()
        else:
            rows = conn.execute("""
                SELECT id, subject, sender_name, sender_email, date, category, is_read
                FROM newsletters
                WHERE date >= ? AND date <= ?
                ORDER BY date DESC
                LIMIT ? OFFSET ?
            """, (start, end, limit, offset)).fetchall()
        return [dict(r) for r in rows]


def get_recent(days: int = 30, limit: int = 100) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT id, subject, sender_name, sender_email, date, category, is_read
            FROM newsletters
            WHERE date >= date('now', ?)
            ORDER BY date DESC
            LIMIT ?
        """, (f"-{days} days", limit)).fetchall()
        return [dict(r) for r in rows]


def get_newsletter(id: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("""
            SELECT * FROM newsletters WHERE id = ?
        """, (id,)).fetchone()
        return dict(row) if row else None


def mark_read(id: str):
    with get_conn() as conn:
        conn.execute("UPDATE newsletters SET is_read = 1 WHERE id = ?", (id,))


def get_sync_state(key: str) -> Optional[str]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM sync_state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None


def set_sync_state(key: str, value: str):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO sync_state (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (key, value))
