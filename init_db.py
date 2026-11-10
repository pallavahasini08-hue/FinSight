"""Create any FinSight table, column or index that is missing (money is stored in paise).

Safe to run any time, on a new or an existing database: it only adds what is
missing and never deletes or changes your data. The app also runs it on start-up.

    python init_db.py
"""
import sqlite3

from database import DB_PATH
from schema import TABLES, INDEXES, SCHEMA_VERSION


def add_missing_columns(conn):
    """Add columns that older databases do not have yet."""
    user_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    if "vault_pin_hash" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN vault_pin_hash TEXT")
    if "vault_failed" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN vault_failed INTEGER NOT NULL DEFAULT 0")
    if "vault_retry_at" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN vault_retry_at REAL NOT NULL DEFAULT 0")
    goal_columns = {row[1] for row in conn.execute("PRAGMA table_info(goals)")}
    if "milestone_celebrated" not in goal_columns:
        conn.execute("ALTER TABLE goals ADD COLUMN milestone_celebrated INTEGER NOT NULL DEFAULT 0")
        # Dreams already past a milestone should not celebrate it again later.
        for milestone in (25, 50, 75, 100):
            conn.execute(
                "UPDATE goals SET milestone_celebrated = ? "
                "WHERE target_amount > 0 AND current_amount * 100 >= target_amount * ?",
                (milestone, milestone)
            )


def init_db(verbose=True):
    conn = sqlite3.connect(DB_PATH)
    had_tables = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()[0]
    version = conn.execute("PRAGMA user_version").fetchone()[0]

    for name, sql in TABLES.items():
        conn.execute(sql.format(name=name))
    add_missing_columns(conn)
    for sql in INDEXES:
        conn.execute(sql)

    # Stamp the version on a new database, or on one already stored in paise.
    # An old rupee database (version 0 with tables) is left unstamped on purpose.
    if not had_tables or version >= 1:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    conn.close()
    if verbose:
        print("Database initialized.")


if __name__ == "__main__":
    init_db()