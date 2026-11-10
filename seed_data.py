"""Add sample income and expenses for the current and previous month.

Only fills a month that has no income or expense entries yet, and never deletes
anything, so it is safe to run on a database that already has your own data.

    python seed_data.py                 # first user in the database
    python seed_data.py SomeUsername    # a specific user
"""
import sqlite3
import sys
from datetime import date

from database import DB_PATH
from money import to_paise
from schema import TABLES, INDEXES


def month_start(today, back):
    """First day of the month `back` months before today's month."""
    year, month = today.year, today.month - back
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1)


# (kind, label, amount in rupees, day of month, note)
THIS_MONTH = [
    ("income", "Salary", "65000", 1, "Monthly salary"),
    ("income", "Freelance", "2000", 9, "Side project"),
    ("expense", "Shopping", "7000", 5, "Clothes + electronics"),
    ("expense", "Food", "2500", 8, "Groceries"),
    ("expense", "Transport", "3000", 10, "Cab + fuel"),
    ("expense", "Food", "3000", 14, "Dining"),
]
LAST_MONTH = [
    ("income", "Salary", "65000", 1, "Monthly salary"),
    ("expense", "Shopping", "6500", 5, "Clothes + electronics"),
    ("expense", "Food", "5000", 8, "Groceries + dining"),
    ("expense", "Transport", "3500", 10, "Cab + fuel"),
]


def seed(username=None):
    conn = sqlite3.connect(DB_PATH)
    for name, sql in TABLES.items():        # make sure tables exist
        conn.execute(sql.format(name=name))
    for sql in INDEXES:
        conn.execute(sql)

    if username:
        user = conn.execute("SELECT id, username FROM users WHERE username = ?", (username,)).fetchone()
    else:
        user = conn.execute("SELECT id, username FROM users ORDER BY id LIMIT 1").fetchone()
    if not user:
        print("No user found. Sign up in the app first, then run this again.")
        conn.close()
        return
    user_id, name = user

    today = date.today()
    for back, rows in ((1, LAST_MONTH), (0, THIS_MONTH)):
        start = month_start(today, back)
        end = month_start(today, back - 1) if back else date(
            start.year + (start.month == 12), start.month % 12 + 1, 1)
        has_data = conn.execute(
            """SELECT (SELECT COUNT(*) FROM income   WHERE user_id=? AND date>=? AND date<?)
                    + (SELECT COUNT(*) FROM expenses WHERE user_id=? AND date>=? AND date<?)""",
            (user_id, start.isoformat(), end.isoformat(), user_id, start.isoformat(), end.isoformat()),
        ).fetchone()[0]
        label = start.strftime("%B %Y")
        if has_data:
            print(f"{label}: already has entries, skipped.")
            continue

        for kind, what, rupees, day, note in rows:
            # in the current month, never date an entry in the future
            day = min(day, today.day) if back == 0 else day
            when = date(start.year, start.month, day).isoformat()
            amount = to_paise(rupees)
            if kind == "income":
                conn.execute(
                    "INSERT INTO income (user_id, source, amount, date) VALUES (?,?,?,?)",
                    (user_id, what, amount, when))
            else:
                conn.execute(
                    "INSERT INTO expenses (user_id, category, amount, description, date) VALUES (?,?,?,?,?)",
                    (user_id, what, amount, note, when))
        print(f"{label}: sample entries added for {name}.")

    conn.commit()
    conn.close()


if __name__ == "__main__":
    seed(sys.argv[1] if len(sys.argv) > 1 else None)