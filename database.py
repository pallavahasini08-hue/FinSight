import os
import sqlite3

# The database file sits next to this file, so the app and the scripts find it
# no matter which folder they are started from. Set FINSIGHT_DB to use another file.
DB_PATH = os.environ.get(
    "FINSIGHT_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "finsight.db"),
)


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn