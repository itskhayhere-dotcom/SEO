"""SQLite access + forward-only SQL migrations (app/migrations/NNN_*.sql)."""
from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from . import config

MIG_DIR = Path(__file__).parent / "migrations"

def connect() -> sqlite3.Connection:
    c = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA busy_timeout=30000")
    return c

def q(sql: str, args=()) -> list[sqlite3.Row]:
    c = connect()
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()

def one(sql: str, args=()):
    r = q(sql, args)
    return r[0] if r else None

def x(sql: str, args=()) -> int:
    c = connect()
    try:
        return c.execute(sql, args).lastrowid
    finally:
        c.close()

@contextmanager
def tx():
    """Write transaction (BEGIN IMMEDIATE) - serialises writers, rolls back on error."""
    c = connect()
    try:
        c.execute("BEGIN IMMEDIATE")
        yield c
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
    finally:
        c.close()

def migrate() -> list[str]:
    c = connect()
    applied = []
    try:
        c.execute("CREATE TABLE IF NOT EXISTS schema_migrations(name TEXT PRIMARY KEY, applied_at TEXT DEFAULT (datetime('now')))")
        done = {r[0] for r in c.execute("SELECT name FROM schema_migrations")}
        for f in sorted(MIG_DIR.glob("*.sql")):
            if f.name in done:
                continue
            c.executescript("BEGIN;\n" + f.read_text() + "\nCOMMIT;")
            c.execute("INSERT INTO schema_migrations(name) VALUES (?)", (f.name,))
            applied.append(f.name)
    finally:
        c.close()
    return applied
