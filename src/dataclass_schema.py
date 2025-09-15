import sqlite3
from dataclasses import fields, is_dataclass


def sync_schema(conn, dataclass_type, table_name, create_sql, rebuild=False):
    """
    Ensure the SQLite table schema matches the dataclass fields.

    Args:
        conn: sqlite3.Connection
        dataclass_type: The @dataclass type to inspect
        table_name: Table name as string
        create_sql: Full CREATE TABLE statement as string
        rebuild: If True, drop and recreate the table if mismatched
    """
    if not is_dataclass(dataclass_type):
        raise TypeError("dataclass_type must be a dataclass")

    cur = conn.cursor()

    # Get current table schema (if exists)
    cur.execute(
        f"SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    )
    exists = cur.fetchone()

    if not exists:
        print(f"[schema-sync] Table '{table_name}' does not exist. Creating it...")
        cur.execute(create_sql)
        conn.commit()
        return

    # Table exists → get column info
    cur.execute(f"PRAGMA table_info({table_name})")
    existing_columns = [row[1] for row in cur.fetchall()]

    expected_columns = [f.name for f in fields(dataclass_type)]

    if existing_columns == expected_columns:
        print(f"[schema-sync] ✅ Table '{table_name}' matches dataclass schema.")
        return

    print(f"[schema-sync] ⚠️ Schema mismatch for '{table_name}'")
    print(f"Existing: {existing_columns}")
    print(f"Expected: {expected_columns}")

    if rebuild:
        print(f"[schema-sync] Dropping and recreating '{table_name}'...")
        cur.execute(f"DROP TABLE IF EXISTS {table_name}")
        cur.execute(create_sql)
        conn.commit()
        print(f"[schema-sync] ✅ Table '{table_name}' recreated.")
    else:
        print(f"[schema-sync] ❗ Table not rebuilt (set rebuild=True to recreate).")
