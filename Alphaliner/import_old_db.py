# -*- coding: utf-8 -*-
"""
Alphaliner Legacy DB Importer & Integrator
과거 DB 파일의 스냅샷 데이터를 현재 alphaliner_history.db로 병합 및 덮어씁니다.
"""

import sys
import os
import sqlite3
import json
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).resolve().parent
TARGET_DB = BASE_DIR / "alphaliner_history.db"
JSON_PATH = BASE_DIR / "alphaliner_data.json"

# 과거 DB 파일명을 지정합니다 (필요 시 수정하거나 인자로 전달)
DEFAULT_OLD_DB = BASE_DIR / "old_alphaliner.db"

def init_target_db(conn):
    """현재 타깃 DB 테이블 스키마 보장"""
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS snapshot_meta (
            snapshot_date TEXT PRIMARY KEY,
            global_teu INTEGER,
            global_ships INTEGER,
            top100_teu INTEGER,
            top100_ships INTEGER,
            top10_share REAL,
            orderbook_teu INTEGER,
            updated_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS operator_snapshots (
            snapshot_date TEXT,
            rank INTEGER,
            operator_name TEXT,
            total_teu INTEGER,
            total_ships INTEGER,
            market_share REAL,
            owned_teu INTEGER,
            owned_ships INTEGER,
            owned_share REAL,
            chartered_teu INTEGER,
            chartered_ships INTEGER,
            chartered_share REAL,
            orderbook_teu INTEGER,
            orderbook_ships INTEGER,
            orderbook_share REAL,
            country TEXT,
            created_at TEXT,
            PRIMARY KEY (snapshot_date, rank)
        )
    """)
    conn.commit()

def detect_source_table_and_columns(old_conn):
    """과거 DB의 테이블 및 컬럼 구조를 자동 감지"""
    cur = old_conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [row[0] for row in cur.fetchall()]

    candidate_tables = ["operator_snapshots", "alphaliner", "top100", "operators", "history"]
    target_table = None

    for cand in candidate_tables:
        if cand in tables:
            target_table = cand
            break

    if not target_table and tables:
        target_table = tables[0]

    if not target_table:
        raise ValueError("과거 DB에 유효한 테이블이 존재하지 않습니다.")

    cur.execute(f"PRAGMA table_info({target_table})")
    cols = [row[1].lower() for row in cur.fetchall()]
    return target_table, cols

def get_field(row, col_candidates, default=None):
    """다양한 버전의 컬럼명에서 유효한 값을 추출"""
    for cand in col_candidates:
        if cand in row.keys() and row[cand] is not None:
            return row[cand]
    return default

def import_legacy_data(old_db_path):
    old_path = Path(old_db_path)
    if not old_path.exists():
        print(f"[오류] 과거 DB 파일을 찾을 수 없습니다: {old_path}")
        print("과거 DB 파일 경로를 확인해주세요.")
        return

    print(f"과거 DB 로드 중: {old_path}")
    old_conn = sqlite3.connect(old_path)
    old_conn.row_factory = sqlite3.Row
    old_cur = old_conn.cursor()

    target_table, cols = detect_source_table_and_columns(old_conn)
    print(f"감지된 소스 테이블: '{target_table}' | 컬럼 수: {len(cols)}")

    target_conn = sqlite3.connect(TARGET_DB)
    init_target_db(target_conn)
    target_cur = target_conn.cursor()

    old_cur.execute(f"SELECT * FROM {target_table}")
    rows = old_cur.fetchall()
    print(f"총 {len(rows):,}개의 레코드를 가져왔습니다. 변환 및 삽입 시작...")

    inserted_count = 0
    imported_dates = set()

    for r in rows:
        row = dict(r)

        # 날짜 식별
        dt = get_field(row, ["snapshot_date", "date", "dt", "scraped_at", "created_at"])
        if not dt:
            continue
        # 날짜 포맷 표준화 (YYYY-MM-DD)
        dt = str(dt)[:10]

        rank = get_field(row, ["rank", "ranking", "id"], 0)
        try:
            rank = int(rank)
        except (ValueError, TypeError):
            continue

        name = get_field(row, ["operator_name", "operator", "name", "carrier"], "Unknown")

        # 수치 추출 및 보정
        owned_teu = int(get_field(row, ["owned_teu", "ownedteu"], 0) or 0)
        owned_ships = int(get_field(row, ["owned_ships", "ownedships"], 0) or 0)
        chartered_teu = int(get_field(row, ["chartered_teu", "charteredteu", "chart_teu", "chartteu"], 0) or 0)
        chartered_ships = int(get_field(row, ["chartered_ships", "charteredships", "chart_ships", "chartships"], 0) or 0)

        total_teu = get_field(row, ["total_teu", "totalteu", "teu"])
        if total_teu is None or int(total_teu) == 0:
            total_teu = owned_teu + chartered_teu
        else:
            total_teu = int(total_teu)

        total_ships = get_field(row, ["total_ships", "totalships", "ships"])
        if total_ships is None or int(total_ships) == 0:
            total_ships = owned_ships + chartered_ships
        else:
            total_ships = int(total_ships)

        market_share = float(get_field(row, ["market_share", "marketshare", "share", "percent"], 0.0) or 0.0)
        owned_share = float(get_field(row, ["owned_share", "ownedshare"], 0.0) or 0.0)
        chartered_share = float(get_field(row, ["chartered_share", "charteredshare", "chart_share"], 0.0) or 0.0)

        orderbook_teu = int(get_field(row, ["orderbook_teu", "orderbookteu", "order_teu", "orderteu"], 0) or 0)
        orderbook_ships = int(get_field(row, ["orderbook_ships", "orderbookships", "order_ships", "orderships"], 0) or 0)
        orderbook_share = float(get_field(row, ["orderbook_share", "orderbookshare", "order_share"], 0.0) or 0.0)
        country = get_field(row, ["country", "nation"], "")

        # Target DB에 UPSERT (기존에 동일 날짜/순위가 있으면 덮어씀)
        target_cur.execute("""
            INSERT INTO operator_snapshots (
                snapshot_date, rank, operator_name,
                total_teu, total_ships, market_share,
                owned_teu, owned_ships, owned_share,
                chartered_teu, chartered_ships, chartered_share,
                orderbook_teu, orderbook_ships, orderbook_share,
                country, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_date, rank) DO UPDATE SET
                operator_name = excluded.operator_name,
                total_teu = excluded.total_teu,
                total_ships = excluded.total_ships,
                market_share = excluded.market_share,
                owned_teu = excluded.owned_teu,
                owned_ships = excluded.owned_ships,
                owned_share = excluded.owned_share,
                chartered_teu = excluded.chartered_teu,
                chartered_ships = excluded.chartered_ships,
                chartered_share = excluded.chartered_share,
                orderbook_teu = excluded.orderbook_teu,
                orderbook_ships = excluded.orderbook_ships,
                orderbook_share = excluded.orderbook_share,
                country = excluded.country
        """, (
            dt, rank, name, total_teu, total_ships, market_share,
            owned_teu, owned_ships, owned_share,
            chartered_teu, chartered_ships, chartered_share,
            orderbook_teu, orderbook_ships, orderbook_share,
            country, datetime.now().isoformat()
        ))
        inserted_count += 1
        imported_dates.add(dt)

    target_conn.commit()
    print(f"선사 스냅샷 삽입/갱신 완료: {inserted_count:,}건 (총 {len(imported_dates)}개 날짜)")

    # ----------------------------------------------------
    # 메타데이터(snapshot_meta) 재집계 및 덮어쓰기
    # ----------------------------------------------------
    print("\n메타데이터 집계 갱신 시작...")
    for dt in sorted(imported_dates):
        target_cur.execute("""
            SELECT rank, total_teu, total_ships, orderbook_teu
            FROM operator_snapshots
            WHERE snapshot_date = ?
            ORDER BY rank ASC
        """, (dt,))
        ops = target_cur.fetchall()
        if not ops:
            continue

        top100_teu = sum(o[1] or 0 for o in ops)
        top100_ships = sum(o[2] or 0 for o in ops)
        top10_teu = sum(o[1] or 0 for o in ops[:10])
        top10_share = round((top10_teu / top100_teu * 100), 2) if top100_teu > 0 else 0.0
        orderbook_teu = sum(o[3] or 0 for o in ops)

        if top100_teu >= 10_000_000:
            target_cur.execute("""
                INSERT INTO snapshot_meta (
                    snapshot_date, global_teu, global_ships,
                    top100_teu, top100_ships, top10_share, orderbook_teu, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(snapshot_date) DO UPDATE SET
                    global_teu = excluded.global_teu,
                    global_ships = excluded.global_ships,
                    top100_teu = excluded.top100_teu,
                    top100_ships = excluded.top100_ships,
                    top10_share = excluded.top10_share,
                    orderbook_teu = excluded.orderbook_teu,
                    updated_at = excluded.updated_at
            """, (
                dt, top100_teu, top100_ships,
                top100_teu, top100_ships, top10_share, orderbook_teu,
                datetime.now().isoformat()
            ))
            print(f" -> [{dt}] 메타 갱신 완료 (TEU: {top100_teu:,})")

    target_conn.commit()
    target_conn.close()
    old_conn.close()

    print("\nDB 병합 완료! 이제 alphaliner_data.json 대시보드 데이터를 새로 내보냅니다...")
    regenerate_json_and_dashboard()

def regenerate_json_and_dashboard():
    """병합된 DB를 기준으로 alphaliner_data.json 재생성"""
    try:
        from Alphaliner import export_json_and_html
        export_json_and_html()
        print("대시보드 JSON 및 HTML 재생성 완료.")
    except ImportError:
        print("[참고] 'python Alphaliner/Alphaliner.py --cron'을 실행하여 대시보드 JSON을 새로 생성해주세요.")

if __name__ == "__main__":
    old_db = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_OLD_DB
    import_legacy_data(old_db)
