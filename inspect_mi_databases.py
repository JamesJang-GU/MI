#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DB_CANDIDATES = [
    os.path.join(BASE_DIR, "ShippingIndex", "shipping_index.db"),
    os.path.join(BASE_DIR, "Alphaliner", "alphaliner_history.db"),
    os.path.join(BASE_DIR, "Alphaliner", "alphaliner_data.db"),
    os.path.join(BASE_DIR, "BunkerPrice", "bunker_price_data.db"),
    os.path.join(BASE_DIR, "BunkerPrice", "OilPrice", "oil_price_data.db"),
    os.path.join(BASE_DIR, "BunkerPrice", "OilPrice", "crude_oil_data.db"),
    os.path.join(BASE_DIR, "Forwarder", "freight_forwarders_data.db"),
    os.path.join(BASE_DIR, "TAC Index", "tac_data.db"),
    os.path.join(BASE_DIR, "MacroEconomies", "macro_data.db"),
]

def inspect_database(db_path):
    print("\n" + "=" * 100)
    print(f"DB: {db_path}")
    print("=" * 100)

    if not os.path.exists(db_path):
        print("[NOT FOUND]")
        return

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        cursor.execute("""
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name
        """)
        tables = [row[0] for row in cursor.fetchall()]

        if not tables:
            print("[NO TABLES]")
            return

        for table in tables:
            print(f"\n[TABLE] {table}")

            cursor.execute(f'PRAGMA table_info("{table}")')
            columns = cursor.fetchall()

            print("Columns:")
            for column in columns:
                cid, name, col_type, notnull, default, pk = column
                print(f"  - {name} ({col_type})")

            cursor.execute(f'SELECT COUNT(*) FROM "{table}"')
            count = cursor.fetchone()[0]
            print(f"Rows: {count}")

            if count > 0:
                cursor.execute(f'SELECT * FROM "{table}" LIMIT 3')
                samples = cursor.fetchall()
                print("Sample rows:")
                for sample in samples:
                    print(f"  {sample}")

        conn.close()

    except Exception as exc:
        print(f"[ERROR] {exc}")

def main():
    print("MI DATABASE SCHEMA INSPECTION")
    print("")

    found_any = False
    inspected = set()

    for db_path in DB_CANDIDATES:
        normalized = os.path.normpath(db_path)

        if normalized in inspected:
            continue

        inspected.add(normalized)

        if os.path.exists(db_path):
            found_any = True
            inspect_database(db_path)

    if not found_any:
        print("No configured SQLite database was found.")

    print("\n" + "=" * 100)
    print("INSPECTION COMPLETE")
    print("=" * 100)

if __name__ == "__main__":
    main()
