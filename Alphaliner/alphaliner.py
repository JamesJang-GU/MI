# -*- coding: utf-8 -*-
"""
Alphaliner TOP 100 Liner Operators Static Dashboard Crawler
- ExtJS Store extraction
- Snapshot quality validation before persistence
- Invalid historical snapshot exclusion during JSON export
- SQLite historical storage
- Static dashboard generation

Output files:
- alphaliner_history.db
- alphaliner_data.json
- index.html
"""

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from threading import Lock

from flask import Flask, send_from_directory
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait

try:
    from webdriver_manager.chrome import ChromeDriverManager
    HAS_WEBDRIVER_MANAGER = True
except Exception:
    HAS_WEBDRIVER_MANAGER = False

# =========================================================
# 1. Basic Configuration
# =========================================================
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "alphaliner_history.db"
JSON_PATH = BASE_DIR / "alphaliner_data.json"
HTML_PATH = BASE_DIR / "index.html"

TARGET_URL = "https://alphaliner.axsmarine.com/PublicTop100/"

# ---------------------------------------------------------
# Snapshot Validation Thresholds
# ---------------------------------------------------------
# Alphaliner Top 100 데이터의 일반적 규모를 기준으로 설정.
# 실제 시장 변동은 허용하면서, 0 / null / 일부 컬럼 누락 스냅샷을 차단한다.
MIN_OPERATOR_COUNT = 95
MIN_UNIQUE_OPERATOR_COUNT = 95
MIN_TOTAL_TEU = 20_000_000
MIN_TOTAL_SHIPS = 4_500
MIN_TOP10_TEU = 12_000_000
MIN_TOP10_SHARE = 45.0
MIN_ORDERBOOK_TEU = 1_000_000
MIN_ORDERBOOK_SHIPS = 50
MIN_NONZERO_ORDERBOOK_OPERATORS = 5

# 직전 정상 데이터 대비 허용 가능한 급격한 변동 범위
# 시장 데이터의 정상 업데이트를 막지 않기 위한 안전 범위다.
MAX_TOTAL_TEU_CHANGE_RATIO = 0.20
MAX_TOTAL_SHIPS_CHANGE_RATIO = 0.20
MAX_ORDERBOOK_TEU_CHANGE_RATIO = 0.70

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)]
)

logger = logging.getLogger("Alphaliner")
db_lock = Lock()

# =========================================================
# 2. Exceptions
# =========================================================
class SnapshotValidationError(RuntimeError):
    """크롤링 결과가 저장 기준을 충족하지 못할 때 발생."""

# =========================================================
# 3. Database Schema & Migration
# =========================================================
def init_db():
    with db_lock, sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS snapshot_meta (
                snapshot_date TEXT PRIMARY KEY,
                updated_at TEXT,
                total_teu INTEGER,
                total_ships INTEGER,
                top100_teu INTEGER,
                top100_ships INTEGER,
                top100_share REAL,
                orderbook_teu INTEGER,
                orderbook_ships INTEGER,
                is_valid INTEGER DEFAULT 1,
                validation_error TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS operator_snapshots (
                snapshot_date TEXT,
                rank INTEGER,
                name TEXT,
                country TEXT,
                teu INTEGER,
                teu_share REAL,
                ships INTEGER,
                chartered_teu INTEGER,
                chartered_share REAL,
                chartered_ships INTEGER,
                owned_teu INTEGER,
                owned_share REAL,
                owned_ships INTEGER,
                orderbook_teu INTEGER,
                orderbook_teu_share REAL,
                orderbook_ships INTEGER,
                created_at TEXT,
                PRIMARY KEY (snapshot_date, rank)
            )
        """)

        cursor.execute("PRAGMA table_info(snapshot_meta)")
        existing_meta_cols = {row[1] for row in cursor.fetchall()}

        expected_meta_cols = {
            "updated_at": "TEXT",
            "total_teu": "INTEGER",
            "total_ships": "INTEGER",
            "top100_teu": "INTEGER",
            "top100_ships": "INTEGER",
            "top100_share": "REAL",
            "orderbook_teu": "INTEGER",
            "orderbook_ships": "INTEGER",
            "is_valid": "INTEGER DEFAULT 1",
            "validation_error": "TEXT"
        }

        for col_name, col_type in expected_meta_cols.items():
            if col_name not in existing_meta_cols:
                logger.info(
                    "[DB MIGRATE] Adding missing column '%s' to snapshot_meta",
                    col_name
                )
                cursor.execute(
                    f"ALTER TABLE snapshot_meta ADD COLUMN {col_name} {col_type}"
                )

        cursor.execute("PRAGMA table_info(operator_snapshots)")
        existing_op_cols = {row[1] for row in cursor.fetchall()}

        expected_op_cols = {
            "country": "TEXT",
            "teu": "INTEGER",
            "teu_share": "REAL",
            "ships": "INTEGER",
            "chartered_teu": "INTEGER",
            "chartered_share": "REAL",
            "chartered_ships": "INTEGER",
            "owned_teu": "INTEGER",
            "owned_share": "REAL",
            "owned_ships": "INTEGER",
            "orderbook_teu": "INTEGER",
            "orderbook_teu_share": "REAL",
            "orderbook_ships": "INTEGER",
            "created_at": "TEXT"
        }

        for col_name, col_type in expected_op_cols.items():
            if col_name not in existing_op_cols:
                logger.info(
                    "[DB MIGRATE] Adding missing column '%s' to operator_snapshots",
                    col_name
                )
                cursor.execute(
                    f"ALTER TABLE operator_snapshots ADD COLUMN {col_name} {col_type}"
                )

        conn.commit()

# =========================================================
# 4. Utility Functions
# =========================================================
def parse_int(val):
    if val is None or val == "":
        return 0

    if isinstance(val, (int, float)):
        return int(val)

    cleaned = re.sub(r"[^\d\-]", "", str(val))

    try:
        return int(cleaned) if cleaned else 0
    except ValueError:
        return 0

def parse_float(val):
    if val is None or val == "":
        return 0.0

    if isinstance(val, (int, float)):
        return float(val)

    cleaned = re.sub(r"[^\d.\-]", "", str(val))

    try:
        return float(cleaned) if cleaned else 0.0
    except ValueError:
        return 0.0

def get_field_val(record, candidates, default=""):
    for candidate in candidates:
        if candidate in record and record[candidate] is not None:
            return record[candidate]
    return default

def percent_change(current, previous):
    if previous is None or previous == 0:
        return None
    return (current - previous) / previous

# =========================================================
# 5. Selenium & ExtJS Scraping
# =========================================================
def get_chrome_driver():
    options = Options()

    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")

    options.add_argument(
        "--user-agent=Mozilla/5.0 "
        "(Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    chrome_bin = (
        os.getenv("CHROME_BINARY")
        or os.getenv("CHROME_BIN")
        or os.getenv("GOOGLE_CHROME_BIN")
    )

    if chrome_bin and os.path.exists(chrome_bin):
        options.binary_location = chrome_bin

    chromedriver_path = os.getenv("CHROMEDRIVER_PATH")

    if chromedriver_path and os.path.exists(chromedriver_path):
        return webdriver.Chrome(
            service=Service(chromedriver_path),
            options=options
        )

    if HAS_WEBDRIVER_MANAGER:
        try:
            return webdriver.Chrome(
                service=Service(ChromeDriverManager().install()),
                options=options
            )
        except Exception as exc:
            logger.warning(
                "WebDriverManager failed: %s. Falling back to PATH driver.",
                exc
            )

    return webdriver.Chrome(options=options)

def fetch_alphaliner_data():
    logger.info("Navigating to %s", TARGET_URL)

    driver = None

    try:
        driver = get_chrome_driver()
        driver.get(TARGET_URL)

        wait = WebDriverWait(driver, 25)

        wait.until(
            lambda d: d.execute_script("""
                return (
                    typeof Ext !== 'undefined' &&
                    Ext.data &&
                    Ext.data.StoreManager &&
                    Ext.data.StoreManager.lookup('Top100description') !== undefined
                );
            """)
        )

        time.sleep(2)

        raw_data = driver.execute_script("""
            var store = Ext.data.StoreManager.lookup('Top100description');

            if (!store) {
                return null;
            }

            var records = [];

            store.each(function(rec) {
                records.push(rec.data);
            });

            return {
                records: records,
                count: store.getCount(),
                fields: store.getModel().getFields().map(function(field) {
                    return field.name;
                })
            };
        """)

        if not raw_data or not raw_data.get("records"):
            raise ValueError(
                "ExtJS Store Top100description is empty or unavailable."
            )

        logger.info(
            "Successfully extracted %s records from ExtJS Store.",
            len(raw_data["records"])
        )

        logger.info(
            "Detected ExtJS fields: %s",
            ", ".join(raw_data.get("fields", []))
        )

        return raw_data["records"]

    except Exception as exc:
        logger.error("Error fetching Alphaliner data: %s", exc)
        raise

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

# =========================================================
# 6. Record Normalization
# =========================================================
def normalize_records(records, snapshot_date, now_iso):
    operators = []

    for record in records:
        rank = parse_int(
            get_field_val(
                record,
                ["Rank", "rank", "Position", "position", "id"]
            )
        )

        if rank <= 0 or rank > 100:
            continue

        name = str(
            get_field_val(
                record,
                ["Operator", "operator", "Name", "name", "Liner", "liner"],
                "Unknown"
            )
        ).strip()

        country = str(
            get_field_val(
                record,
                ["Country", "country", "Flag", "flag"],
                ""
            )
        ).strip()

        teu = parse_int(
            get_field_val(
                record,
                ["TotalTeu", "totalTeu", "TEU", "teu", "Capacity", "capacity"]
            )
        )

        teu_share = parse_float(
            get_field_val(
                record,
                [
                    "TotalShare",
                    "totalShare",
                    "Share",
                    "share",
                    "MarketShare",
                    "marketShare",
                    "percent"
                ]
            )
        )

        ships = parse_int(
            get_field_val(
                record,
                [
                    "TotalShips",
                    "totalShips",
                    "Ships",
                    "ships",
                    "NbShips",
                    "nbShips"
                ]
            )
        )

        chartered_teu = parse_int(
            get_field_val(
                record,
                [
                    "CharteredTeu",
                    "charteredTeu",
                    "CharterTeu",
                    "charter_teu",
                    "chartTeu"
                ]
            )
        )

        chartered_share = parse_float(
            get_field_val(
                record,
                [
                    "CharteredShare",
                    "charteredShare",
                    "CharterShare",
                    "charter_share",
                    "chartProcent"
                ]
            )
        )

        chartered_ships = parse_int(
            get_field_val(
                record,
                [
                    "CharteredShips",
                    "charteredShips",
                    "CharterShips",
                    "charter_ships",
                    "chartShips"
                ]
            )
        )

        owned_teu = parse_int(
            get_field_val(
                record,
                [
                    "OwnedTeu",
                    "ownedTeu",
                    "OwnTeu",
                    "own_teu"
                ]
            )
        )

        owned_share = parse_float(
            get_field_val(
                record,
                [
                    "OwnedShare",
                    "ownedShare",
                    "OwnShare",
                    "own_share"
                ]
            )
        )

        if owned_share == 0.0 and teu > 0 and owned_teu > 0:
            owned_share = round((owned_teu / teu) * 100, 2)

        owned_ships = parse_int(
            get_field_val(
                record,
                [
                    "OwnedShips",
                    "ownedShips",
                    "OwnShips",
                    "own_ships"
                ]
            )
        )

        orderbook_teu = parse_int(
            get_field_val(
                record,
                [
                    "TEUOrderbook",
                    "TeuOrderbook",
                    "teu_orderbook",
                    "teuorderbook",
                    "OrderbookTeu",
                    "orderbookTeu",
                    "OrderBookTEU",
                    "OrderbookTEU",
                    "orderbook_teu",
                    "orderbookteu",
                    "Orderbook_Teu",
                    "OBTeu",
                    "OBTEU",
                    "orderTeu"
                ]
            )
        )

        orderbook_ships = parse_int(
            get_field_val(
                record,
                [
                    "ShipsOrderbook",
                    "ShipOrderbook",
                    "ships_orderbook",
                    "shipsorderbook",
                    "OrderbookShips",
                    "orderbookShips",
                    "OrderBookShips",
                    "OrderBookNb",
                    "OrderbookNb",
                    "orderbook_ships",
                    "orderbookships",
                    "Orderbook_Ships",
                    "OBShips",
                    "OBNb",
                    "orderShips"
                ]
            )
        )

        orderbook_teu_share = parse_float(
            get_field_val(
                record,
                [
                    "OrderbookShare",
                    "orderbookShare",
                    "OrderBookShare",
                    "TEUOrderbookShare",
                    "orderbook_share",
                    "OBShare",
                    "orderProcents"
                ]
            )
        )

        if orderbook_teu_share == 0.0 and teu > 0 and orderbook_teu > 0:
            orderbook_teu_share = round((orderbook_teu / teu) * 100, 2)

        operators.append({
            "snapshot_date": snapshot_date,
            "rank": rank,
            "name": name,
            "country": country,
            "teu": teu,
            "teu_share": teu_share,
            "ships": ships,
            "chartered_teu": chartered_teu,
            "chartered_share": chartered_share,
            "chartered_ships": chartered_ships,
            "owned_teu": owned_teu,
            "owned_share": owned_share,
            "owned_ships": owned_ships,
            "orderbook_teu": orderbook_teu,
            "orderbook_teu_share": orderbook_teu_share,
            "orderbook_ships": orderbook_ships,
            "created_at": now_iso
        })

    operators.sort(key=lambda row: row["rank"])

    return operators

def build_snapshot_metrics(operators):
    total_teu = sum(op["teu"] for op in operators)
    total_ships = sum(op["ships"] for op in operators)

    orderbook_teu = sum(op["orderbook_teu"] for op in operators)
    orderbook_ships = sum(op["orderbook_ships"] for op in operators)

    top10 = sorted(operators, key=lambda row: row["rank"])[:10]
    top10_teu = sum(op["teu"] for op in top10)

    top10_share = (
        round((top10_teu / total_teu) * 100, 2)
        if total_teu > 0
        else 0.0
    )

    nonzero_teu_rows = sum(1 for op in operators if op["teu"] > 0)
    nonzero_ships_rows = sum(1 for op in operators if op["ships"] > 0)
    nonzero_orderbook_rows = sum(
        1 for op in operators if op["orderbook_teu"] > 0
    )

    return {
        "total_teu": total_teu,
        "total_ships": total_ships,
        "top100_teu": total_teu,
        "top100_ships": total_ships,
        "top100_share": 100.0,
        "orderbook_teu": orderbook_teu,
        "orderbook_ships": orderbook_ships,
        "top10_teu": top10_teu,
        "top10_share": top10_share,
        "nonzero_teu_rows": nonzero_teu_rows,
        "nonzero_ships_rows": nonzero_ships_rows,
        "nonzero_orderbook_rows": nonzero_orderbook_rows
    }

# =========================================================
# 7. Snapshot Quality Validation
# =========================================================
def get_latest_valid_snapshot_metrics(conn, exclude_date=None):
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    sql = """
        SELECT
            snapshot_date,
            total_teu,
            total_ships,
            orderbook_teu,
            orderbook_ships
        FROM snapshot_meta
        WHERE COALESCE(is_valid, 1) = 1
    """

    params = []

    if exclude_date:
        sql += " AND snapshot_date <> ?"
        params.append(exclude_date)

    sql += " ORDER BY snapshot_date DESC LIMIT 1"

    cursor.execute(sql, params)
    row = cursor.fetchone()

    return dict(row) if row else None

def validate_snapshot(operators, metrics, previous_metrics=None):
    errors = []

    ranks = [op["rank"] for op in operators]
    names = [op["name"].strip().lower() for op in operators if op["name"].strip()]

    unique_ranks = set(ranks)
    unique_names = set(names)

    missing_core_names = [
        op["rank"]
        for op in operators
        if not op["name"] or op["name"].strip().lower() == "unknown"
    ]

    # 1. 행 수 / 순위 무결성
    if len(operators) < MIN_OPERATOR_COUNT:
        errors.append(
            f"Operator row count too low: {len(operators)} "
            f"(minimum: {MIN_OPERATOR_COUNT})"
        )

    if len(unique_ranks) < MIN_OPERATOR_COUNT:
        errors.append(
            f"Unique rank count too low: {len(unique_ranks)} "
            f"(minimum: {MIN_OPERATOR_COUNT})"
        )

    if len(unique_names) < MIN_UNIQUE_OPERATOR_COUNT:
        errors.append(
            f"Unique operator name count too low: {len(unique_names)} "
            f"(minimum: {MIN_UNIQUE_OPERATOR_COUNT})"
        )

    if missing_core_names:
        errors.append(
            f"Unknown or empty operator names found at ranks: "
            f"{missing_core_names[:10]}"
        )

    expected_ranks = set(range(1, 101))
    missing_ranks = sorted(expected_ranks - unique_ranks)

    if missing_ranks and len(missing_ranks) > 5:
        errors.append(
            f"Too many missing ranks: {missing_ranks}"
        )

    # 2. Fleet Capacity 검증
    if metrics["total_teu"] < MIN_TOTAL_TEU:
        errors.append(
            f"Total TEU invalid: {metrics['total_teu']:,} "
            f"(minimum: {MIN_TOTAL_TEU:,})"
        )

    if metrics["total_ships"] < MIN_TOTAL_SHIPS:
        errors.append(
            f"Total ships invalid: {metrics['total_ships']:,} "
            f"(minimum: {MIN_TOTAL_SHIPS:,})"
        )

    if metrics["nonzero_teu_rows"] < MIN_OPERATOR_COUNT:
        errors.append(
            f"Operators with nonzero TEU too low: "
            f"{metrics['nonzero_teu_rows']}"
        )

    if metrics["nonzero_ships_rows"] < MIN_OPERATOR_COUNT:
        errors.append(
            f"Operators with nonzero ships too low: "
            f"{metrics['nonzero_ships_rows']}"
        )

    # 3. CR10 검증
    if metrics["top10_teu"] < MIN_TOP10_TEU:
        errors.append(
            f"Top 10 TEU invalid: {metrics['top10_teu']:,} "
            f"(minimum: {MIN_TOP10_TEU:,})"
        )

    if metrics["top10_share"] < MIN_TOP10_SHARE:
        errors.append(
            f"Top 10 concentration invalid: "
            f"{metrics['top10_share']:.2f}% "
            f"(minimum: {MIN_TOP10_SHARE:.2f}%)"
        )

    # 4. Orderbook 검증
    # 이번 장애의 직접 원인이므로 0 또는 전체 누락을 명확히 차단한다.
    if metrics["orderbook_teu"] < MIN_ORDERBOOK_TEU:
        errors.append(
            f"Orderbook TEU invalid: {metrics['orderbook_teu']:,} "
            f"(minimum: {MIN_ORDERBOOK_TEU:,})"
        )

    if metrics["orderbook_ships"] < MIN_ORDERBOOK_SHIPS:
        errors.append(
            f"Orderbook ships invalid: {metrics['orderbook_ships']:,} "
            f"(minimum: {MIN_ORDERBOOK_SHIPS:,})"
        )

    if metrics["nonzero_orderbook_rows"] < MIN_NONZERO_ORDERBOOK_OPERATORS:
        errors.append(
            f"Operators with nonzero orderbook too low: "
            f"{metrics['nonzero_orderbook_rows']} "
            f"(minimum: {MIN_NONZERO_ORDERBOOK_OPERATORS})"
        )

    # 5. 직전 정상 스냅샷 대비 급격한 이상 변동 검증
    if previous_metrics:
        teu_change = percent_change(
            metrics["total_teu"],
            previous_metrics["total_teu"]
        )

        ships_change = percent_change(
            metrics["total_ships"],
            previous_metrics["total_ships"]
        )

        ob_change = percent_change(
            metrics["orderbook_teu"],
            previous_metrics["orderbook_teu"]
        )

        if teu_change is not None and abs(teu_change) > MAX_TOTAL_TEU_CHANGE_RATIO:
            errors.append(
                "Total TEU change versus prior valid snapshot is abnormal: "
                f"{teu_change:.2%}"
            )

        if ships_change is not None and abs(ships_change) > MAX_TOTAL_SHIPS_CHANGE_RATIO:
            errors.append(
                "Total ships change versus prior valid snapshot is abnormal: "
                f"{ships_change:.2%}"
            )

        if ob_change is not None and abs(ob_change) > MAX_ORDERBOOK_TEU_CHANGE_RATIO:
            errors.append(
                "Orderbook TEU change versus prior valid snapshot is abnormal: "
                f"{ob_change:.2%}"
            )

    if errors:
        raise SnapshotValidationError(
            "Snapshot quality validation failed. "
            "No database write was performed.\n- "
            + "\n- ".join(errors)
        )

def is_snapshot_valid_for_export(meta, operators):
    """
    과거에 is_valid 컬럼 없이 저장된 레코드도 방어적으로 재검증한다.
    따라서 이미 저장된 0 KPI 스냅샷도 JSON 최신값으로 노출되지 않는다.
    """
    if not meta:
        return False

    if meta.get("is_valid") == 0:
        return False

    try:
        metrics = build_snapshot_metrics(operators)
        validate_snapshot(
            operators=operators,
            metrics=metrics,
            previous_metrics=None
        )
        return True

    except SnapshotValidationError as exc:
        logger.warning(
            "Excluding invalid historical snapshot %s from export: %s",
            meta.get("snapshot_date"),
            str(exc).replace("\n", " | ")
        )
        return False

# =========================================================
# 8. Atomic Persistence
# =========================================================
def persist_snapshot(snapshot_date, now_iso, operators, metrics):
    """
    검증이 통과한 데이터만 이 함수로 전달된다.

    동일 날짜 재실행 시에는 기존 해당 날짜 데이터를 트랜잭션 내에서
    완전 교체한다. 중간 오류가 발생하면 rollback되어 기존 데이터가 유지된다.
    """
    with db_lock, sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()

        try:
            cursor.execute("BEGIN IMMEDIATE")

            cursor.execute("""
                DELETE FROM operator_snapshots
                WHERE snapshot_date = ?
            """, (snapshot_date,))

            cursor.execute("""
                DELETE FROM snapshot_meta
                WHERE snapshot_date = ?
            """, (snapshot_date,))

            cursor.execute("""
                INSERT INTO snapshot_meta (
                    snapshot_date,
                    updated_at,
                    total_teu,
                    total_ships,
                    top100_teu,
                    top100_ships,
                    top100_share,
                    orderbook_teu,
                    orderbook_ships,
                    is_valid,
                    validation_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                snapshot_date,
                now_iso,
                metrics["total_teu"],
                metrics["total_ships"],
                metrics["top100_teu"],
                metrics["top100_ships"],
                metrics["top100_share"],
                metrics["orderbook_teu"],
                metrics["orderbook_ships"],
                1,
                None
            ))

            cursor.executemany("""
                INSERT INTO operator_snapshots (
                    snapshot_date,
                    rank,
                    name,
                    country,
                    teu,
                    teu_share,
                    ships,
                    chartered_teu,
                    chartered_share,
                    chartered_ships,
                    owned_teu,
                    owned_share,
                    owned_ships,
                    orderbook_teu,
                    orderbook_teu_share,
                    orderbook_ships,
                    created_at
                ) VALUES (
                    :snapshot_date,
                    :rank,
                    :name,
                    :country,
                    :teu,
                    :teu_share,
                    :ships,
                    :chartered_teu,
                    :chartered_share,
                    :chartered_ships,
                    :owned_teu,
                    :owned_share,
                    :owned_ships,
                    :orderbook_teu,
                    :orderbook_teu_share,
                    :orderbook_ships,
                    :created_at
                )
            """, operators)

            conn.commit()

        except Exception:
            conn.rollback()
            raise

def process_and_persist(records):
    init_db()

    snapshot_date = datetime.now().strftime("%Y-%m-%d")
    now_iso = datetime.now().isoformat()

    operators = normalize_records(
        records=records,
        snapshot_date=snapshot_date,
        now_iso=now_iso
    )

    metrics = build_snapshot_metrics(operators)

    with db_lock, sqlite3.connect(DB_PATH) as conn:
        previous_metrics = get_latest_valid_snapshot_metrics(
            conn=conn,
            exclude_date=snapshot_date
        )

    logger.info(
        "Candidate snapshot metrics | "
        "operators=%s | total_teu=%s | total_ships=%s | "
        "top10_teu=%s | top10_share=%.2f%% | "
        "orderbook_teu=%s | orderbook_ships=%s | "
        "nonzero_orderbook_operators=%s",
        len(operators),
        f"{metrics['total_teu']:,}",
        f"{metrics['total_ships']:,}",
        f"{metrics['top10_teu']:,}",
        metrics["top10_share"],
        f"{metrics['orderbook_teu']:,}",
        f"{metrics['orderbook_ships']:,}",
        metrics["nonzero_orderbook_rows"]
    )

    # 가장 중요:
    # 검증 실패 시 persist_snapshot()까지 도달하지 않으므로
    # 기존 정상 DB/JSON 스냅샷이 절대로 덮어써지지 않는다.
    validate_snapshot(
        operators=operators,
        metrics=metrics,
        previous_metrics=previous_metrics
    )

    persist_snapshot(
        snapshot_date=snapshot_date,
        now_iso=now_iso,
        operators=operators,
        metrics=metrics
    )

    logger.info(
        "Persisted VALID snapshot %s: %s operators "
        "(TEU: %s | Ships: %s | Orderbook: %s TEU / %s ships).",
        snapshot_date,
        len(operators),
        f"{metrics['total_teu']:,}",
        f"{metrics['total_ships']:,}",
        f"{metrics['orderbook_teu']:,}",
        f"{metrics['orderbook_ships']:,}"
    )

    return snapshot_date

# =========================================================
# 9. Invalid Historical Snapshot Cleanup
# =========================================================
def purge_invalid_snapshots(dry_run=False):
    init_db()

    invalid_dates = []

    with db_lock, sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("""
            SELECT *
            FROM snapshot_meta
            ORDER BY snapshot_date ASC
        """)

        meta_rows = cursor.fetchall()

        for meta_row in meta_rows:
            meta = dict(meta_row)
            snapshot_date = meta["snapshot_date"]

            cursor.execute("""
                SELECT *
                FROM operator_snapshots
                WHERE snapshot_date = ?
                ORDER BY rank ASC
            """, (snapshot_date,))

            operators = [dict(row) for row in cursor.fetchall()]

            if not is_snapshot_valid_for_export(meta, operators):
                invalid_dates.append(snapshot_date)

        if not invalid_dates:
            logger.info("No invalid snapshots found.")
            return []

        logger.warning(
            "Invalid snapshots identified: %s",
            ", ".join(invalid_dates)
        )

        if dry_run:
            logger.info("Dry-run mode enabled. No records were deleted.")
            return invalid_dates

        try:
            cursor.execute("BEGIN IMMEDIATE")

            for snapshot_date in invalid_dates:
                cursor.execute("""
                    DELETE FROM operator_snapshots
                    WHERE snapshot_date = ?
                """, (snapshot_date,))

                cursor.execute("""
                    DELETE FROM snapshot_meta
                    WHERE snapshot_date = ?
                """, (snapshot_date,))

            conn.commit()

        except Exception:
            conn.rollback()
            raise

    logger.info(
        "Deleted %s invalid snapshot(s): %s",
        len(invalid_dates),
        ", ".join(invalid_dates)
    )

    return invalid_dates

# =========================================================
# 10. Static Export & Contract Serialization
# =========================================================
def export_json_and_html():
    init_db()

    snapshots = {}
    valid_dates = []

    with db_lock, sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("""
            SELECT *
            FROM snapshot_meta
            ORDER BY snapshot_date DESC
        """)

        meta_rows = cursor.fetchall()

        for meta_row in meta_rows:
            meta = dict(meta_row)
            snapshot_date = meta["snapshot_date"]

            cursor.execute("""
                SELECT *
                FROM operator_snapshots
                WHERE snapshot_date = ?
                ORDER BY rank ASC
            """, (snapshot_date,))

            operators = [dict(row) for row in cursor.fetchall()]

            # 기존에 잘못 저장된 0 / null / incomplete snapshot도 제외
            if not is_snapshot_valid_for_export(meta, operators):
                continue

            valid_dates.append(snapshot_date)

            snapshots[snapshot_date] = {
                "meta": meta,
                "operators": operators
            }

    if not valid_dates:
        logger.warning(
            "No valid snapshots found. Existing JSON file will not be overwritten."
        )
        return

    data_payload = {
        "generated_at": datetime.now().isoformat(),
        "dates": valid_dates,
        "latest_date": valid_dates[0],
        "snapshots": snapshots
    }

    with open(JSON_PATH, "w", encoding="utf-8") as file:
        json.dump(
            data_payload,
            file,
            ensure_ascii=False,
            indent=2
        )

    logger.info(
        "Exported valid contract JSON to %s "
        "(snapshots: %s, latest: %s)",
        JSON_PATH,
        len(valid_dates),
        valid_dates[0]
    )

    generate_static_html(data_payload)

def generate_static_html(payload):
    """
    중요:
    사용자가 올려주신 기존 generate_static_html(payload) 함수 전체를
    이 위치에 그대로 유지하면 됩니다.

    UI HTML/CSS/JavaScript는 데이터 계약인 아래 구조를 그대로 사용하므로
    품질 검증 로직 추가와 충돌하지 않습니다.

    payload = {
        "generated_at": "...",
        "dates": [...],
        "latest_date": "...",
        "snapshots": {
            "YYYY-MM-DD": {
                "meta": {...},
                "operators": [...]
            }
        }
    }
    """

    # -----------------------------------------------------
    # 여기에는 기존 파일의 generate_static_html(payload)
    # 함수 내용을 그대로 붙여 넣으세요.
    # -----------------------------------------------------
    #
    # 기존 UI 생성 함수를 변경하지 않아도 됩니다.
    #
    # 단, 아래처럼 빈 파일을 만들면 안 됩니다.
    # 현재 사용 중인 html_template = f"""...""" 전체를 유지해야 합니다.
    #
    raise NotImplementedError(
        "기존 generate_static_html(payload) 함수의 HTML 템플릿을 "
        "이 위치에 유지해야 합니다."
    )

# =========================================================
# 11. Flask Web Server
# =========================================================
app = Flask(__name__, static_folder=str(BASE_DIR))

@app.route("/")
def index():
    if not HTML_PATH.exists():
        export_json_and_html()

    return send_from_directory(BASE_DIR, "index.html")

@app.route("/alphaliner_data.json")
def get_data_json():
    if not JSON_PATH.exists():
        export_json_and_html()

    return send_from_directory(
        BASE_DIR,
        "alphaliner_data.json",
        mimetype="application/json"
    )

@app.route("/api/scrape", methods=["POST", "GET"])
def trigger_scrape():
    try:
        records = fetch_alphaliner_data()
        snapshot_date = process_and_persist(records)
        export_json_and_html()

        return {
            "status": "success",
            "date": snapshot_date,
            "count": len(records)
        }

    except SnapshotValidationError as exc:
        logger.error("Snapshot rejected by quality gate: %s", exc)

        return {
            "status": "rejected",
            "message": str(exc)
        }, 422

    except Exception as exc:
        logger.error("API scrape error: %s", exc)

        return {
            "status": "error",
            "message": str(exc)
        }, 500

# =========================================================
# 12. Main Entry Point
# =========================================================
def main():
    parser = argparse.ArgumentParser(
        description="Alphaliner TOP 100 Scraper & MI Terminal Dashboard"
    )

    parser.add_argument(
        "--scrape-only",
        action="store_true",
        help="Scrape, validate, persist and export; then exit."
    )

    parser.add_argument(
        "--cron",
        action="store_true",
        help="Headless cron mode: scrape, validate, persist and export."
    )

    parser.add_argument(
        "--export-only",
        action="store_true",
        help="Export JSON/HTML from valid DB snapshots without scraping."
    )

    parser.add_argument(
        "--purge-invalid",
        action="store_true",
        help="Delete historical invalid snapshots, then regenerate JSON/HTML."
    )

    parser.add_argument(
        "--purge-invalid-dry-run",
        action="store_true",
        help="List historical invalid snapshots without deleting them."
    )

    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Flask host."
    )

    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Flask port."
    )

    args, unknown = parser.parse_known_args()

    if unknown:
        logger.info("Ignored unknown CLI arguments: %s", unknown)

    init_db()

    if args.purge_invalid_dry_run:
        purge_invalid_snapshots(dry_run=True)
        sys.exit(0)

    if args.purge_invalid:
        purge_invalid_snapshots(dry_run=False)
        export_json_and_html()
        sys.exit(0)

    if args.export_only:
        export_json_and_html()
        sys.exit(0)

    if args.scrape_only or args.cron:
        try:
            records = fetch_alphaliner_data()
            process_and_persist(records)
            export_json_and_html()

            logger.info("Scrape, validation and export completed successfully.")
            sys.exit(0)

        except SnapshotValidationError as exc:
            logger.error("Scrape rejected. Existing valid snapshot was retained.")
            logger.error(str(exc))
            sys.exit(1)

        except Exception as exc:
            logger.error("Scrape failed: %s", exc)
            sys.exit(1)

    try:
        with sqlite3.connect(DB_PATH) as conn:
            snapshot_count = conn.cursor().execute("""
                SELECT COUNT(*)
                FROM snapshot_meta
                WHERE COALESCE(is_valid, 1) = 1
            """).fetchone()[0]

        if snapshot_count == 0:
            logger.info("No valid snapshots exist. Starting initial scrape.")

            records = fetch_alphaliner_data()
            process_and_persist(records)

    except SnapshotValidationError as exc:
        logger.warning(
            "Initial scrape rejected by validation: %s",
            exc
        )

    except Exception as exc:
        logger.warning("Initial scrape attempt warning: %s", exc)

    export_json_and_html()

    logger.info(
        "Starting Alphaliner Web Server on http://%s:%s",
        args.host,
        args.port
    )

    app.run(
        host=args.host,
        port=args.port,
        debug=False
    )

if __name__ == "__main__":
    main()
