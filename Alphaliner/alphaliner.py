# -*- coding: utf-8 -*-
"""
Alphaliner TOP 100 Liner Operators Static Dashboard Crawler

Features
--------
- ExtJS Store 직접 추출: Top100description
- store.getModel() 미사용
- Chrome / ChromeDriver GitHub Actions 환경 호환
- 저장 전 데이터 품질 검증
- 불완전 스냅샷 DB 저장 차단
- 유효 스냅샷만 JSON / HTML Dashboard에 반영
- SQLite 기반 히스토리 유지
- --cron 실행 및 Flask 로컬 서버 지원

Output files
------------
- Alphaliner/alphaliner_history.db
- Alphaliner/alphaliner_data.json
- Alphaliner/index.html
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, Response, send_from_directory
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
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

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"

logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    handlers=[logging.StreamHandler(sys.stdout)],
)

logger = logging.getLogger("alphaliner")

app = Flask(__name__)

# =========================================================
# 2. Data Quality Thresholds
# =========================================================

MIN_OPERATOR_COUNT = 80
MIN_TOTAL_FLEET_TEU = 10_000_000
MIN_TOTAL_FLEET_SHIPS = 500
MIN_TOP10_TEU = 3_000_000
MIN_ORDERBOOK_TEU = 500_000

MAX_OPERATOR_COUNT = 150
MAX_TOP10_SHARE = 0.95

# =========================================================
# 3. Utility Functions
# =========================================================

def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")

def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")

def normalize_key(value: Any) -> str:
    """필드명을 비교 가능한 형태로 정규화한다."""
    if value is None:
        return ""

    return re.sub(
        r"[^a-z0-9]",
        "",
        str(value).strip().lower()
    )

def parse_number(value: Any) -> Optional[float]:
    """문자열/숫자를 float으로 안전하게 변환한다."""
    if value is None:
        return None

    if isinstance(value, bool):
        return float(value)

    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()

    if not text:
        return None

    text = text.replace(",", "")
    text = text.replace(" ", "")
    text = text.replace("\xa0", "")

    text = text.replace("TEU", "")
    text = text.replace("Ships", "")
    text = text.replace("ships", "")
    text = text.replace("%", "")

    if text in {"-", "–", "—", "N/A", "n/a", "null", "None"}:
        return None

    match = re.search(r"-?\d+(?:\.\d+)?", text)

    if not match:
        return None

    try:
        return float(match.group(0))
    except Exception:
        return None

def parse_int(value: Any, default: int = 0) -> int:
    parsed = parse_number(value)

    if parsed is None:
        return default

    return int(round(parsed))

def parse_float(value: Any, default: float = 0.0) -> float:
    parsed = parse_number(value)

    if parsed is None:
        return default

    return float(parsed)

def first_value(
    record: Dict[str, Any],
    aliases: List[str],
    default: Any = None,
) -> Any:
    """
    ExtJS Store 레코드의 실제 컬럼명이 달라져도
    별칭(alias) 기준으로 값을 찾는다.
    """
    normalized_record = {
        normalize_key(key): value
        for key, value in record.items()
    }

    for alias in aliases:
        normalized_alias = normalize_key(alias)

        if normalized_alias in normalized_record:
            return normalized_record[normalized_alias]

    for alias in aliases:
        normalized_alias = normalize_key(alias)

        for key, value in normalized_record.items():
            if normalized_alias and normalized_alias in key:
                return value

    return default

def safe_json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)

# =========================================================
# 4. Database Functions
# =========================================================

def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def column_exists(
    conn: sqlite3.Connection,
    table_name: str,
    column_name: str,
) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()

    return any(row["name"] == column_name for row in rows)

def add_column_if_missing(
    conn: sqlite3.Connection,
    table_name: str,
    column_definition: str,
) -> None:
    column_name = column_definition.split()[0]

    if not column_exists(conn, table_name, column_name):
        logger.info(
            "[DB MIGRATE] Adding missing column '%s' to '%s'",
            column_name,
            table_name,
        )

        conn.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {column_definition}"
        )

def init_database() -> None:
    conn = get_connection()

    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS snapshot_meta (
                snapshot_date TEXT PRIMARY KEY,
                global_total_teu INTEGER,
                global_total_ships INTEGER,
                top100_total_teu INTEGER,
                top100_total_ships INTEGER,
                top10_total_teu INTEGER,
                top10_share REAL,
                orderbook_total_teu INTEGER,
                extracted_at TEXT,
                source_url TEXT,
                raw_hash TEXT,
                global_teu INTEGER,
                global_ships INTEGER,
                top100_teu INTEGER,
                top100_ships INTEGER,
                total_teu INTEGER,
                total_ships INTEGER,
                created_at TEXT,
                updated_at TEXT,
                notes TEXT,
                is_valid INTEGER DEFAULT 1,
                validation_error TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS operator_snapshots (
                snapshot_date TEXT NOT NULL,
                rank INTEGER NOT NULL,
                operator TEXT,
                fleet_teu INTEGER,
                fleet_ships INTEGER,
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
                teu INTEGER,
                teu_share REAL,
                ships INTEGER,
                orderbook_teu_share REAL,
                created_at TEXT,
                PRIMARY KEY (snapshot_date, rank)
            )
        """)

        # 기존 DB와의 호환성을 위한 누락 컬럼 보완
        meta_columns = [
            "global_total_teu INTEGER",
            "global_total_ships INTEGER",
            "top100_total_teu INTEGER",
            "top100_total_ships INTEGER",
            "top10_total_teu INTEGER",
            "top10_share REAL",
            "orderbook_total_teu INTEGER",
            "extracted_at TEXT",
            "source_url TEXT",
            "raw_hash TEXT",
            "global_teu INTEGER",
            "global_ships INTEGER",
            "top100_teu INTEGER",
            "top100_ships INTEGER",
            "total_teu INTEGER",
            "total_ships INTEGER",
            "created_at TEXT",
            "updated_at TEXT",
            "notes TEXT",
            "is_valid INTEGER DEFAULT 1",
            "validation_error TEXT",
        ]

        for definition in meta_columns:
            add_column_if_missing(conn, "snapshot_meta", definition)

        operator_columns = [
            "fleet_teu INTEGER",
            "fleet_ships INTEGER",
            "owned_teu INTEGER",
            "owned_ships INTEGER",
            "owned_share REAL",
            "chartered_teu INTEGER",
            "chartered_ships INTEGER",
            "chartered_share REAL",
            "orderbook_teu INTEGER",
            "orderbook_ships INTEGER",
            "orderbook_share REAL",
            "country TEXT",
            "teu INTEGER",
            "teu_share REAL",
            "ships INTEGER",
            "orderbook_teu_share REAL",
            "created_at TEXT",
        ]

        for definition in operator_columns:
            add_column_if_missing(conn, "operator_snapshots", definition)

        conn.commit()

    finally:
        conn.close()

# =========================================================
# 5. Chrome / Selenium Functions
# =========================================================

def get_chrome_binary() -> Optional[str]:
    candidates = [
        os.getenv("CHROME_BINARY"),
        os.getenv("GOOGLE_CHROME_BIN"),
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ]

    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate

    return None

def get_chrome_driver() -> webdriver.Chrome:
    options = Options()

    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--window-size=1920,2200")
    options.add_argument("--lang=en-US")
    options.add_argument("--disable-blink-features=AutomationControlled")

    options.add_experimental_option(
        "excludeSwitches",
        ["enable-automation"]
    )

    options.add_experimental_option(
        "useAutomationExtension",
        False
    )

    chrome_binary = get_chrome_binary()

    if chrome_binary:
        logger.info("Using Chrome binary: %s", chrome_binary)
        options.binary_location = chrome_binary
    else:
        logger.warning(
            "Chrome binary was not explicitly found. "
            "Using Selenium default Chrome discovery."
        )

    chromedriver_path = os.getenv("CHROMEDRIVER_PATH")

    if chromedriver_path and os.path.exists(chromedriver_path):
        logger.info(
            "Using CHROMEDRIVER_PATH: %s",
            chromedriver_path
        )

        driver = webdriver.Chrome(
            service=Service(chromedriver_path),
            options=options,
        )

    elif HAS_WEBDRIVER_MANAGER:
        logger.info(
            "CHROMEDRIVER_PATH not found. "
            "Using webdriver-manager fallback."
        )

        driver_path = ChromeDriverManager().install()

        driver = webdriver.Chrome(
            service=Service(driver_path),
            options=options,
        )

    else:
        logger.info(
            "Using Selenium default ChromeDriver resolution."
        )

        driver = webdriver.Chrome(options=options)

    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {
            "source": """
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
            """
        },
    )

    return driver

# =========================================================
# 6. ExtJS Data Extraction
# =========================================================

def fetch_alphaliner_data() -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Alphaliner ExtJS Store에서 Top 100 데이터를 추출한다.

    중요:
    - store.getModel()을 호출하지 않는다.
    - Alphaliner Store 구현에 getModel() 메서드가 없을 수 있다.
    """
    logger.info("Navigating to %s", TARGET_URL)

    driver = None

    try:
        driver = get_chrome_driver()
        driver.set_page_load_timeout(90)
        driver.get(TARGET_URL)

        wait = WebDriverWait(driver, 45)

        wait.until(
            lambda d: d.execute_script("""
                try {
                    if (typeof Ext === 'undefined') {
                        return false;
                    }

                    if (!Ext.data || !Ext.data.StoreManager) {
                        return false;
                    }

                    var store =
                        Ext.data.StoreManager.lookup('Top100description');

                    if (!store) {
                        return false;
                    }

                    if (typeof store.getCount === 'function') {
                        return store.getCount() > 0;
                    }

                    if (store.data && store.data.items) {
                        return store.data.items.length > 0;
                    }

                    return false;

                } catch (e) {
                    return false;
                }
            """)
        )

        time.sleep(2)

        extraction_result = driver.execute_script("""
            try {
                var store =
                    Ext.data.StoreManager.lookup('Top100description');

                if (!store) {
                    return {
                        ok: false,
                        error: 'Top100description Store was not found.',
                        records: [],
                        fields: []
                    };
                }

                var records = [];

                if (typeof store.each === 'function') {
                    store.each(function(rec) {
                        if (rec && rec.data) {
                            records.push(rec.data);
                        }
                    });
                } else if (
                    store.data &&
                    store.data.items &&
                    Array.isArray(store.data.items)
                ) {
                    store.data.items.forEach(function(rec) {
                        if (rec && rec.data) {
                            records.push(rec.data);
                        }
                    });
                }

                var fields = [];

                if (records.length > 0 && records[0]) {
                    fields = Object.keys(records[0]);
                }

                return {
                    ok: true,
                    count: records.length,
                    store_id: store.storeId || 'Top100description',
                    fields: fields,
                    records: records
                };

            } catch (e) {
                return {
                    ok: false,
                    error: String(e),
                    records: [],
                    fields: []
                };
            }
        """)

        if not extraction_result:
            raise RuntimeError(
                "No JavaScript response was returned during ExtJS extraction."
            )

        if not extraction_result.get("ok"):
            raise RuntimeError(
                "ExtJS extraction failed: "
                f"{extraction_result.get('error', 'Unknown error')}"
            )

        records = extraction_result.get("records") or []

        if not records:
            raise RuntimeError(
                "Top100description Store was detected, "
                "but no records were extracted."
            )

        logger.info(
            "Successfully extracted %s records from Store '%s'.",
            len(records),
            extraction_result.get("store_id", "Top100description"),
        )

        logger.info(
            "Detected source fields: %s",
            ", ".join(extraction_result.get("fields", [])),
        )

        logger.info(
            "First raw record: %s",
            safe_json_dumps(records[0]),
        )

        page_info = driver.execute_script("""
            return {
                title: document.title || '',
                url: window.location.href || '',
                text: (document.body && document.body.innerText)
                    ? document.body.innerText.substring(0, 5000)
                    : ''
            };
        """)

        return records, page_info

    except WebDriverException as exc:
        logger.error("Selenium/WebDriver error: %s", exc)
        raise

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
# 7. Record Normalization
# =========================================================

def normalize_operator_record(
    raw: Dict[str, Any],
    fallback_rank: int,
) -> Dict[str, Any]:
    """
    Alphaliner ExtJS Store의 필드명이 달라져도
    주요 수치를 표준 DB 구조로 정규화한다.
    """

    rank = parse_int(
        first_value(raw, [
            "rank",
            "ranking",
            "position",
            "no",
            "number",
        ]),
        fallback_rank,
    )

    operator = first_value(raw, [
        "operator",
        "operatorname",
        "company",
        "name",
        "liner",
    ])

    operator = str(operator).strip() if operator else f"Unknown Operator {rank}"

    fleet_teu = parse_int(first_value(raw, [
        "fleet_teu",
        "fleet teu",
        "total_teu",
        "total teu",
        "teu",
        "capacity",
        "totalcapacity",
    ]))

    fleet_ships = parse_int(first_value(raw, [
        "fleet_ships",
        "fleet ships",
        "total_ships",
        "total ships",
        "ships",
        "vessels",
        "fleet",
    ]))

    owned_teu = parse_int(first_value(raw, [
        "owned_teu",
        "owned teu",
        "ownedcapacity",
        "owned",
    ]))

    owned_ships = parse_int(first_value(raw, [
        "owned_ships",
        "owned ships",
        "ownedvessels",
    ]))

    chartered_teu = parse_int(first_value(raw, [
        "chartered_teu",
        "chartered teu",
        "charter_teu",
        "charteredcapacity",
        "chartered",
    ]))

    chartered_ships = parse_int(first_value(raw, [
        "chartered_ships",
        "chartered ships",
        "charter_ships",
        "charteredvessels",
    ]))

    orderbook_teu = parse_int(first_value(raw, [
        "orderbook_teu",
        "orderbook teu",
        "orderbook",
        "order_book_teu",
        "onorderteu",
    ]))

    orderbook_ships = parse_int(first_value(raw, [
        "orderbook_ships",
        "orderbook ships",
        "order_book_ships",
        "onorderships",
    ]))

    country = first_value(raw, [
        "country",
        "nationality",
        "flag",
        "headquarters",
    ])

    country = str(country).strip() if country else None

    owned_share = (
        round(owned_teu / fleet_teu * 100, 2)
        if fleet_teu > 0 and owned_teu > 0
        else 0.0
    )

    chartered_share = (
        round(chartered_teu / fleet_teu * 100, 2)
        if fleet_teu > 0 and chartered_teu > 0
        else 0.0
    )

    orderbook_share = (
        round(orderbook_teu / fleet_teu * 100, 2)
        if fleet_teu > 0 and orderbook_teu > 0
        else 0.0
    )

    return {
        "rank": rank,
        "operator": operator,
        "fleet_teu": fleet_teu,
        "fleet_ships": fleet_ships,
        "owned_teu": owned_teu,
        "owned_ships": owned_ships,
        "owned_share": owned_share,
        "chartered_teu": chartered_teu,
        "chartered_ships": chartered_ships,
        "chartered_share": chartered_share,
        "orderbook_teu": orderbook_teu,
        "orderbook_ships": orderbook_ships,
        "orderbook_share": orderbook_share,
        "country": country,

        # 기존 JSON / DB 호환용 별칭
        "teu": fleet_teu,
        "ships": fleet_ships,
        "teu_share": 0.0,
        "orderbook_teu_share": orderbook_share,
    }

def normalize_records(
    raw_records: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    normalized = []

    for index, raw in enumerate(raw_records, start=1):
        if not isinstance(raw, dict):
            continue

        record = normalize_operator_record(raw, fallback_rank=index)

        if record["rank"] <= 0:
            continue

        if record["fleet_teu"] <= 0:
            continue

        normalized.append(record)

    normalized.sort(key=lambda row: row["rank"])

    seen_ranks = set()
    deduplicated = []

    for record in normalized:
        rank = record["rank"]

        if rank in seen_ranks:
            continue

        seen_ranks.add(rank)
        deduplicated.append(record)

    return deduplicated

# =========================================================
# 8. Metrics and Quality Validation
# =========================================================

def calculate_metrics(
    operators: List[Dict[str, Any]],
) -> Dict[str, Any]:
    total_teu = sum(row["fleet_teu"] for row in operators)
    total_ships = sum(row["fleet_ships"] for row in operators)

    top10 = sorted(
        operators,
        key=lambda row: row["fleet_teu"],
        reverse=True,
    )[:10]

    top10_total_teu = sum(row["fleet_teu"] for row in top10)
    orderbook_total_teu = sum(row["orderbook_teu"] for row in operators)

    top10_share = (
        round(top10_total_teu / total_teu * 100, 2)
        if total_teu > 0
        else 0.0
    )

    return {
        "global_total_teu": total_teu,
        "global_total_ships": total_ships,
        "top100_total_teu": total_teu,
        "top100_total_ships": total_ships,
        "top10_total_teu": top10_total_teu,
        "top10_share": top10_share,
        "orderbook_total_teu": orderbook_total_teu,

        # 기존 DB/JSON 호환 필드
        "global_teu": total_teu,
        "global_ships": total_ships,
        "top100_teu": total_teu,
        "top100_ships": total_ships,
        "total_teu": total_teu,
        "total_ships": total_ships,
    }

def validate_snapshot(
    operators: List[Dict[str, Any]],
    metrics: Dict[str, Any],
) -> Tuple[bool, List[str]]:
    errors = []

    operator_count = len(operators)

    if operator_count < MIN_OPERATOR_COUNT:
        errors.append(
            f"Operator count too low: {operator_count} "
            f"(minimum: {MIN_OPERATOR_COUNT})"
        )

    if operator_count > MAX_OPERATOR_COUNT:
        errors.append(
            f"Operator count too high: {operator_count} "
            f"(maximum: {MAX_OPERATOR_COUNT})"
        )

    if metrics["total_teu"] < MIN_TOTAL_FLEET_TEU:
        errors.append(
            f"Total fleet TEU invalid: {metrics['total_teu']:,} "
            f"(minimum: {MIN_TOTAL_FLEET_TEU:,})"
        )

    if metrics["total_ships"] < MIN_TOTAL_FLEET_SHIPS:
        errors.append(
            f"Total fleet ships invalid: {metrics['total_ships']:,} "
            f"(minimum: {MIN_TOTAL_FLEET_SHIPS:,})"
        )

    if metrics["top10_total_teu"] < MIN_TOP10_TEU:
        errors.append(
            f"Top 10 total TEU invalid: {metrics['top10_total_teu']:,} "
            f"(minimum: {MIN_TOP10_TEU:,})"
        )

    if metrics["orderbook_total_teu"] < MIN_ORDERBOOK_TEU:
        errors.append(
            f"Orderbook TEU invalid: {metrics['orderbook_total_teu']:,} "
            f"(minimum: {MIN_ORDERBOOK_TEU:,})"
        )

    if metrics["top10_share"] <= 0 or metrics["top10_share"] > MAX_TOP10_SHARE * 100:
        errors.append(
            f"Top 10 concentration invalid: "
            f"{metrics['top10_share']:.2f}%"
        )

    positive_teu_rows = [
        row for row in operators
        if row["fleet_teu"] > 0
    ]

    if len(positive_teu_rows) < MIN_OPERATOR_COUNT:
        errors.append(
            "Too few operators contain positive fleet TEU values."
        )

    unique_operators = {
        row["operator"].strip().lower()
        for row in operators
        if row["operator"]
    }

    if len(unique_operators) < MIN_OPERATOR_COUNT:
        errors.append(
            "Too few unique operator names were extracted."
        )

    return len(errors) == 0, errors

# =========================================================
# 9. Persistence
# =========================================================

def persist_snapshot(
    snapshot_date: str,
    operators: List[Dict[str, Any]],
    metrics: Dict[str, Any],
    source_url: str,
    raw_hash: str,
) -> None:
    conn = get_connection()

    try:
        extracted_at = now_iso()

        conn.execute("BEGIN")

        conn.execute("""
            INSERT INTO snapshot_meta (
                snapshot_date,
                global_total_teu,
                global_total_ships,
                top100_total_teu,
                top100_total_ships,
                top10_total_teu,
                top10_share,
                orderbook_total_teu,
                extracted_at,
                source_url,
                raw_hash,
                global_teu,
                global_ships,
                top100_teu,
                top100_ships,
                total_teu,
                total_ships,
                created_at,
                updated_at,
                notes,
                is_valid,
                validation_error
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(snapshot_date) DO UPDATE SET
                global_total_teu = excluded.global_total_teu,
                global_total_ships = excluded.global_total_ships,
                top100_total_teu = excluded.top100_total_teu,
                top100_total_ships = excluded.top100_total_ships,
                top10_total_teu = excluded.top10_total_teu,
                top10_share = excluded.top10_share,
                orderbook_total_teu = excluded.orderbook_total_teu,
                extracted_at = excluded.extracted_at,
                source_url = excluded.source_url,
                raw_hash = excluded.raw_hash,
                global_teu = excluded.global_teu,
                global_ships = excluded.global_ships,
                top100_teu = excluded.top100_teu,
                top100_ships = excluded.top100_ships,
                total_teu = excluded.total_teu,
                total_ships = excluded.total_ships,
                updated_at = excluded.updated_at,
                notes = excluded.notes,
                is_valid = excluded.is_valid,
                validation_error = excluded.validation_error
        """, (
            snapshot_date,
            metrics["global_total_teu"],
            metrics["global_total_ships"],
            metrics["top100_total_teu"],
            metrics["top100_total_ships"],
            metrics["top10_total_teu"],
            metrics["top10_share"],
            metrics["orderbook_total_teu"],
            extracted_at,
            source_url,
            raw_hash,
            metrics["global_teu"],
            metrics["global_ships"],
            metrics["top100_teu"],
            metrics["top100_ships"],
            metrics["total_teu"],
            metrics["total_ships"],
            extracted_at,
            extracted_at,
            "Validated Alphaliner Top 100 snapshot",
            1,
            None,
        ))

        conn.execute(
            "DELETE FROM operator_snapshots WHERE snapshot_date = ?",
            (snapshot_date,),
        )

        rows = []

        for operator in operators:
            rows.append((
                snapshot_date,
                operator["rank"],
                operator["operator"],
                operator["fleet_teu"],
                operator["fleet_ships"],
                operator["owned_teu"],
                operator["owned_ships"],
                operator["owned_share"],
                operator["chartered_teu"],
                operator["chartered_ships"],
                operator["chartered_share"],
                operator["orderbook_teu"],
                operator["orderbook_ships"],
                operator["orderbook_share"],
                operator["country"],
                operator["teu"],
                operator["teu_share"],
                operator["ships"],
                operator["orderbook_teu_share"],
                extracted_at,
            ))

        conn.executemany("""
            INSERT INTO operator_snapshots (
                snapshot_date,
                rank,
                operator,
                fleet_teu,
                fleet_ships,
                owned_teu,
                owned_ships,
                owned_share,
                chartered_teu,
                chartered_ships,
                chartered_share,
                orderbook_teu,
                orderbook_ships,
                orderbook_share,
                country,
                teu,
                teu_share,
                ships,
                orderbook_teu_share,
                created_at
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
        """, rows)

        conn.commit()

        logger.info(
            "Persisted VALID snapshot %s | operators=%s | fleet_teu=%s | fleet_ships=%s",
            snapshot_date,
            len(operators),
            f"{metrics['total_teu']:,}",
            f"{metrics['total_ships']:,}",
        )

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()

# =========================================================
# 10. JSON Export
# =========================================================

def row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {key: row[key] for key in row.keys()}

def export_json() -> bool:
    conn = get_connection()

    try:
        snapshot_rows = conn.execute("""
            SELECT *
            FROM snapshot_meta
            WHERE COALESCE(is_valid, 1) = 1
              AND COALESCE(total_teu, global_total_teu, 0) > 0
            ORDER BY snapshot_date DESC
        """).fetchall()

        if not snapshot_rows:
            logger.warning(
                "No valid snapshots are available. "
                "Existing JSON file will not be overwritten."
            )
            return False

        snapshots: Dict[str, Any] = {}
        dates: List[str] = []

        for snapshot_row in snapshot_rows:
            snapshot = row_to_dict(snapshot_row)
            snapshot_date = snapshot["snapshot_date"]

            operator_rows = conn.execute("""
                SELECT *
                FROM operator_snapshots
                WHERE snapshot_date = ?
                ORDER BY rank ASC
            """, (snapshot_date,)).fetchall()

            operators = [
                row_to_dict(operator_row)
                for operator_row in operator_rows
            ]

            if len(operators) < MIN_OPERATOR_COUNT:
                logger.warning(
                    "Skipping snapshot %s from JSON export because operator count is only %s.",
                    snapshot_date,
                    len(operators),
                )
                continue

            snapshots[snapshot_date] = {
                "meta": snapshot,
                "operators": operators,
            }

            dates.append(snapshot_date)

        if not dates:
            logger.warning(
                "No usable valid snapshots remain after operator validation. "
                "Existing JSON file will not be overwritten."
            )
            return False

        payload = {
            "generated_at": now_iso(),
            "latest_date": dates[0],
            "dates": dates,
            "snapshots": snapshots,
        }

        temporary_path = JSON_PATH.with_suffix(".json.tmp")

        with open(temporary_path, "w", encoding="utf-8") as file:
            json.dump(
                payload,
                file,
                ensure_ascii=False,
                indent=2,
            )

        temporary_path.replace(JSON_PATH)

        logger.info(
            "Exported valid JSON contract: %s",
            JSON_PATH,
        )

        return True

    finally:
        conn.close()

# =========================================================
# 11. Static HTML Dashboard
# =========================================================

def generate_html() -> None:
    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Alphaliner Top 100 | MI Terminal</title>
<style>
:root {
    --bg: #07111f;
    --panel: #0d1b2a;
    --panel2: #10243a;
    --line: #203852;
    --text: #e6edf5;
    --muted: #89a0b8;
    --blue: #42a5f5;
    --green: #22c55e;
    --red: #ef4444;
    --yellow: #fbbf24;
}
* { box-sizing: border-box; }
body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: Arial, Helvetica, sans-serif;
}
header {
    padding: 20px 28px;
    border-bottom: 1px solid var(--line);
    background: #081522;
}
h1 {
    margin: 0;
    font-size: 22px;
    letter-spacing: .5px;
}
.subtitle {
    color: var(--muted);
    margin-top: 6px;
    font-size: 13px;
}
.container {
    padding: 22px 28px 40px;
}
.toolbar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 18px;
    gap: 12px;
}
select {
    color: var(--text);
    background: var(--panel);
    border: 1px solid var(--line);
    padding: 9px 12px;
}
.status {
    color: var(--muted);
    font-size: 12px;
}
.kpis {
    display: grid;
    grid-template-columns: repeat(4, minmax(180px, 1fr));
    gap: 14px;
    margin-bottom: 20px;
}
.card {
    background: var(--panel);
    border: 1px solid var(--line);
    padding: 16px;
}
.card-label {
    color: var(--muted);
    font-size: 12px;
    text-transform: uppercase;
}
.card-value {
    margin-top: 10px;
    font-size: 25px;
    font-weight: bold;
}
.card-delta {
    margin-top: 8px;
    font-size: 12px;
}
.up { color: var(--green); }
.down { color: var(--red); }
.flat { color: var(--muted); }
.table-wrap {
    overflow-x: auto;
    background: var(--panel);
    border: 1px solid var(--line);
}
table {
    width: 100%;
    border-collapse: collapse;
    min-width: 1100px;
}
th {
    text-align: right;
    color: #9eb4cc;
    background: var(--panel2);
    font-size: 11px;
    padding: 11px 10px;
    border-bottom: 1px solid var(--line);
    white-space: nowrap;
}
td {
    text-align: right;
    padding: 10px;
    border-bottom: 1px solid #162c43;
    font-size: 12px;
}
th:nth-child(1), td:nth-child(1),
th:nth-child(2), td:nth-child(2),
th:nth-child(3), td:nth-child(3) {
    text-align: left;
}
tr:hover { background: #122940; }
.rank {
    color: var(--blue);
    font-weight: bold;
}
.operator {
    font-weight: bold;
}
.error {
    color: #fecaca;
    background: #450a0a;
    border: 1px solid #7f1d1d;
    padding: 16px;
}
@media (max-width: 900px) {
    .kpis { grid-template-columns: repeat(2, 1fr); }
    .container { padding: 18px 14px; }
}
</style>
</head>
<body>
<header>
    <h1>ALPHALINER TOP 100 LINER OPERATORS</h1>
    <div class="subtitle">Maritime Intelligence Terminal · Fleet Capacity, Concentration and Orderbook</div>
</header>

<div class="container">
    <div class="toolbar">
        <select id="dateSelect"></select>
        <div class="status" id="status">Loading data...</div>
    </div>

    <div class="kpis">
        <div class="card">
            <div class="card-label">Total Ships</div>
            <div class="card-value" id="totalShips">-</div>
            <div class="card-delta" id="totalShipsDelta">-</div>
        </div>
        <div class="card">
            <div class="card-label">Total Fleet TEU</div>
            <div class="card-value" id="totalTeu">-</div>
            <div class="card-delta" id="totalTeuDelta">-</div>
        </div>
        <div class="card">
            <div class="card-label">Top 10 Concentration</div>
            <div class="card-value" id="top10Share">-</div>
            <div class="card-delta" id="top10ShareDelta">-</div>
        </div>
        <div class="card">
            <div class="card-label">Orderbook</div>
            <div class="card-value" id="orderbook">-</div>
            <div class="card-delta" id="orderbookDelta">-</div>
        </div>
    </div>

    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th>Rank</th>
                    <th>Operator</th>
                    <th>Fleet TEU</th>
                    <th>Ships</th>
                    <th>Owned TEU</th>
                    <th>Chartered TEU</th>
                    <th>Orderbook TEU</th>
                    <th>Orderbook / Fleet</th>
                </tr>
            </thead>
            <tbody id="tableBody"></tbody>
        </table>
    </div>
</div>

<script>
const nf = new Intl.NumberFormat('en-US');

function number(value) {
    return Number(value || 0);
}

function fmt(value) {
    return nf.format(Math.round(number(value)));
}

function fmtPct(value) {
    return number(value).toFixed(2) + '%';
}

function deltaText(current, previous, suffix = '') {
    if (previous === null || previous === undefined) {
        return '<span class="flat">No prior snapshot</span>';
    }

    const diff = number(current) - number(previous);

    if (diff === 0) {
        return '<span class="flat">0' + suffix + ' vs prior snapshot</span>';
    }

    const cls = diff > 0 ? 'up' : 'down';
    const sign = diff > 0 ? '+' : '';

    return '<span class="' + cls + '">' +
        sign + fmt(diff) + suffix + ' vs prior snapshot</span>';
}

function getMeta(snapshot) {
    return snapshot && snapshot.meta ? snapshot.meta : {};
}

function renderSnapshot(data, selectedDate) {
    const snapshot = data.snapshots[selectedDate];
    const meta = getMeta(snapshot);
    const dates = data.dates || [];
    const currentIndex = dates.indexOf(selectedDate);
    const priorDate = currentIndex >= 0 ? dates[currentIndex + 1] : null;
    const priorMeta = priorDate ? getMeta(data.snapshots[priorDate]) : null;

    document.getElementById('totalShips').textContent =
        fmt(meta.total_ships || meta.global_total_ships);

    document.getElementById('totalTeu').textContent =
        fmt(meta.total_teu || meta.global_total_teu);

    document.getElementById('top10Share').textContent =
        fmtPct(meta.top10_share);

    document.getElementById('orderbook').textContent =
        fmt(meta.orderbook_total_teu);

    document.getElementById('totalShipsDelta').innerHTML = deltaText(
        meta.total_ships || meta.global_total_ships,
        priorMeta ? (priorMeta.total_ships || priorMeta.global_total_ships) : null
    );

    document.getElementById('totalTeuDelta').innerHTML = deltaText(
        meta.total_teu || meta.global_total_teu,
        priorMeta ? (priorMeta.total_teu || priorMeta.global_total_teu) : null
    );

    document.getElementById('top10ShareDelta').innerHTML = deltaText(
        meta.top10_share,
        priorMeta ? priorMeta.top10_share : null,
        ' bps'
    );

    document.getElementById('orderbookDelta').innerHTML = deltaText(
        meta.orderbook_total_teu,
        priorMeta ? priorMeta.orderbook_total_teu : null
    );

    const body = document.getElementById('tableBody');
    body.innerHTML = '';

    (snapshot.operators || []).forEach(row => {
        const tr = document.createElement('tr');

        tr.innerHTML = `
            <td class="rank">${row.rank ?? ''}</td>
            <td class="operator">${row.operator ?? ''}</td>
            <td>${fmt(row.fleet_teu || row.teu)}</td>
            <td>${fmt(row.fleet_ships || row.ships)}</td>
            <td>${fmt(row.owned_teu)}</td>
            <td>${fmt(row.chartered_teu)}</td>
            <td>${fmt(row.orderbook_teu)}</td>
            <td>${fmtPct(row.orderbook_share || row.orderbook_teu_share)}</td>
        `;

        body.appendChild(tr);
    });

    document.getElementById('status').textContent =
        'Snapshot: ' + selectedDate +
        ' · Operators: ' + (snapshot.operators || []).length +
        ' · Updated: ' + (meta.extracted_at || '-');
}

async function init() {
    try {
        const response = await fetch('./alphaliner_data.json?ts=' + Date.now());

        if (!response.ok) {
            throw new Error('Could not load alphaliner_data.json');
        }

        const data = await response.json();
        const dates = data.dates || [];

        if (!dates.length) {
            throw new Error('No valid snapshots available.');
        }

        const select = document.getElementById('dateSelect');

        dates.forEach(date => {
            const option = document.createElement('option');
            option.value = date;
            option.textContent = date;

            if (date === data.latest_date) {
                option.selected = true;
            }

            select.appendChild(option);
        });

        renderSnapshot(data, select.value);

        select.addEventListener('change', function() {
            renderSnapshot(data, this.value);
        });

    } catch (error) {
        document.querySelector('.container').innerHTML =
            '<div class="error">Dashboard data could not be loaded: ' +
            error.message +
            '</div>';
    }
}

init();
</script>
</body>
</html>
"""

    with open(HTML_PATH, "w", encoding="utf-8") as file:
        file.write(html)

    logger.info("Generated dashboard HTML: %s", HTML_PATH)

# =========================================================
# 12. Main Processing Flow
# =========================================================

def process_and_persist() -> bool:
    raw_records, page_info = fetch_alphaliner_data()

    operators = normalize_records(raw_records)
    metrics = calculate_metrics(operators)

    logger.info(
        "Candidate snapshot metrics | operators=%s | total_teu=%s | total_ships=%s | top10_share=%.2f%% | orderbook_teu=%s",
        len(operators),
        f"{metrics['total_teu']:,}",
        f"{metrics['total_ships']:,}",
        metrics["top10_share"],
        f"{metrics['orderbook_total_teu']:,}",
    )

    is_valid, validation_errors = validate_snapshot(
        operators,
        metrics,
    )

    if not is_valid:
        logger.error("Snapshot quality validation failed.")

        for error in validation_errors:
            logger.error("- %s", error)

        logger.error(
            "Scrape rejected. No invalid data has been written to the database."
        )

        return False

    raw_hash = hashlib.sha256(
        json.dumps(
            raw_records,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()

    persist_snapshot(
        snapshot_date=today_str(),
        operators=operators,
        metrics=metrics,
        source_url=page_info.get("url") or TARGET_URL,
        raw_hash=raw_hash,
    )

    return True

def export_json_and_html() -> None:
    json_exported = export_json()

    if json_exported:
        generate_html()
    else:
        logger.warning(
            "JSON export skipped. Existing dashboard files were retained."
        )

# =========================================================
# 13. Flask Routes
# =========================================================

@app.route("/")
def index() -> Response:
    return send_from_directory(BASE_DIR, "index.html")

@app.route("/alphaliner_data.json")
def data_json() -> Response:
    return send_from_directory(BASE_DIR, "alphaliner_data.json")

# =========================================================
# 14. Main
# =========================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Alphaliner Top 100 crawler and static dashboard generator."
    )

    parser.add_argument(
        "--cron",
        action="store_true",
        help="Run crawler once, export files, and exit.",
    )

    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Flask server host.",
    )

    parser.add_argument(
        "--port",
        type=int,
        default=5000,
        help="Flask server port.",
    )

    args = parser.parse_args()

    init_database()

    try:
        success = process_and_persist()

        if not success:
            logger.warning(
                "Crawler did not produce a valid new snapshot. "
                "Existing valid dashboard data will be retained."
            )

    except Exception as exc:
        logger.exception(
            "Alphaliner crawler execution failed: %s",
            exc,
        )

        # 기존 정상 JSON/HTML이 있으면 그것을 유지한다.
        export_json_and_html()

        if args.cron:
            sys.exit(1)

    export_json_and_html()

    if args.cron:
        logger.info("Cron execution completed.")
        return

    logger.info(
        "Starting Alphaliner Web Server on http://%s:%s",
        args.host,
        args.port,
    )

    app.run(
        host=args.host,
        port=args.port,
        debug=False,
    )

if __name__ == "__main__":
    main()
