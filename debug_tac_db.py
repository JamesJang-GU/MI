# -*- coding: utf-8 -*-
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "TAC Index" / "TAC_Index.db"

def quote_identifier(identifier):
    return '"' + str(identifier).replace('"', '""') + '"'

with sqlite3.connect(DB_PATH) as conn:
    cursor = conn.cursor()

    tables = cursor.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()

    print(f"\nDB: {DB_PATH}\n")

    for (table_name,) in tables:
        print("=" * 100)
        print(f"TABLE: {table_name}")

        columns = cursor.execute(
            f"PRAGMA table_info({quote_identifier(table_name)})"
        ).fetchall()

        column_names = [row[1] for row in columns]
        print("COLUMNS:", column_names)

        row_count = cursor.execute(
            f"SELECT COUNT(*) FROM {quote_identifier(table_name)}"
        ).fetchone()[0]

        print("ROW COUNT:", row_count)

        preview_rows = cursor.execute(
            f"""
            SELECT *
            FROM {quote_identifier(table_name)}
            LIMIT 5
            """
        ).fetchall()

        print("SAMPLE:")
        for row in preview_rows:
            print("  ", row)

    print("=" * 100)
