# -*- coding: utf-8 -*-
"""
Alphaliner TOP 100 Liner Operators Static Dashboard Crawler

Features
--------
- ExtJS Store directly extraction: Top100description
- Does not use store.getModel()
- GitHub Actions Chrome / ChromeDriver compatible
- Explicit Alphaliner field mapping:
  totalTeu, totalShips, ownedTeu, ownedShips,
  chartTeu, chartShips, orderTeu, orderShips
- Quality validation before persistence
- Invalid / incomplete snapshots are never persisted
- JSON exports only valid historical snapshots
- SQLite-based historical storage
- Bloomberg / MI Terminal static dashboard
- Date selector, search, grouped table, UTF-8 BOM CSV export
- --cron execution and Flask local server mode

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
MAX_OPERATOR_COUNT = 150

MIN_TOTAL_FLEET_TEU = 10_000_000
MIN_TOTAL_FLEET_SHIPS = 500
MIN_TOP10_TEU = 3_000_000
MIN_ORDERBOOK_TEU = 500_000

MAX_TOP10_SHARE = 0.95

# =========================================================
# 3. Utility Functions
# =========================================================

def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")

def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")

def normalize_key(value: Any) -> str:
    """필드명을 비교 가능한 소문자 영숫자 형태로 정규화한다."""
    if value is None:
        return ""

    return re.sub(
        r"[^a-z0-9]",
        "",
        str(value).strip().lower(),
    )

def parse_number(value: Any) -> Optional[float]:
    """문자열 또는 숫자를 float으로 안전하게 변환한다."""
    if value is None:
        return None

    if isinstance(value, bool):
        return float(value)

    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()

    if not text:
        return None

    text = (
        text.replace(",", "")
        .replace(" ", "")
        .replace("\xa0", "")
        .replace("TEU", "")
        .replace("teu", "")
        .replace("Ships", "")
        .replace("ships", "")
        .replace("%", "")
    )

    if text in {"-", "–", "—", "N/A", "n/a", "null", "None", ""}:
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
    return int(round(parsed)) if parsed is not None else default

def parse_float(value: Any, default: float = 0.0) -> float:
    parsed = parse_number(value)
    return float(parsed) if parsed is not None else default

def first_value(
    record: Dict[str, Any],
    aliases: List[str],
    default: Any = None,
) -> Any:
    """
    별칭 기준으로 정확히 일치하는 필드를 찾는다.

    중요:
    - 부분 문자열 매칭을 하지 않는다.
    - 예: 'teu'가 'chartTeu'에 잘못 매칭되는 문제를 방지한다.
    """
    normalized_record = {
        normalize_key(key): value
        for key, value in record.items()
    }

    for alias in aliases:
        normalized_alias = normalize_key(alias)

        if normalized_alias in normalized_record:
            return normalized_record[normalized_alias]

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
        ["enable-automation"],
    )
    options.add_experimental_option(
        "useAutomationExtension",
        False,
    )

    chrome_binary = get_chrome_binary()

    if chrome_binary:
        logger.info("Using Chrome binary: %s", chrome_binary)
        options.binary_location = chrome_binary
    else:
        logger.warning("Chrome binary was not explicitly found.")

    chromedriver_path = os.getenv("CHROMEDRIVER_PATH")

    if chromedriver_path and os.path.exists(chromedriver_path):
        logger.info("Using CHROMEDRIVER_PATH: %s", chromedriver_path)
        driver = webdriver.Chrome(
            service=Service(chromedriver_path),
            options=options,
        )

    elif HAS_WEBDRIVER_MANAGER:
        logger.info("Using webdriver-manager fallback.")
        driver_path = ChromeDriverManager().install()

        driver = webdriver.Chrome(
            service=Service(driver_path),
            options=options,
        )

    else:
        logger.info("Using Selenium default ChromeDriver resolution.")
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
    ExtJS Store 'Top100description'에서 데이터를 추출한다.

    store.getModel()은 사용하지 않는다.
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
                    if (typeof Ext === 'undefined') return false;
                    if (!Ext.data || !Ext.data.StoreManager) return false;

                    var store =
                        Ext.data.StoreManager.lookup('Top100description');

                    if (!store) return false;

                    if (typeof store.getCount === 'function') {
                        return store.getCount() > 0;
                    }

                    return !!(
                        store.data &&
                        store.data.items &&
                        store.data.items.length > 0
                    );

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

                var fields = records.length && records[0]
                    ? Object.keys(records[0])
                    : [];

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
                text: document.body && document.body.innerText
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
    Alphaliner Top100description 레코드를 표준 구조로 변환한다.

    우선순위:
    1. totalTeu / totalShips
    2. ownedTeu + chartTeu / ownedShips + chartShips
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
        "operatorName",
        "company",
        "name",
        "liner",
        "line",
    ])

    operator = (
        str(operator).strip()
        if operator
        else f"Unknown Operator {rank}"
    )

    owned_teu = parse_int(first_value(raw, [
        "ownedTeu",
        "owned_teu",
        "ownedCapacity",
    ]))

    owned_ships = parse_int(first_value(raw, [
        "ownedShips",
        "owned_ships",
        "ownedVessels",
    ]))

    # Alphaliner 실제 원본 필드: chartTeu / chartShips
    chartered_teu = parse_int(first_value(raw, [
        "chartTeu",
        "charteredTeu",
        "charterTeu",
        "chartered_teu",
    ]))

    chartered_ships = parse_int(first_value(raw, [
        "chartShips",
        "charteredShips",
        "charterShips",
        "chartered_ships",
    ]))

    # Alphaliner 실제 원본 필드: totalTeu / totalShips
    direct_fleet_teu = parse_int(first_value(raw, [
        "totalTeu",
        "fleetTeu",
        "fleet_teu",
        "total_teu",
        "totalCapacity",
        "capacity",
    ]))

    direct_fleet_ships = parse_int(first_value(raw, [
        "totalShips",
        "fleetShips",
        "fleet_ships",
        "total_ships",
        "vessels",
    ]))

    fleet_teu = (
        direct_fleet_teu
        if direct_fleet_teu > 0
        else owned_teu + chartered_teu
    )

    fleet_ships = (
        direct_fleet_ships
        if direct_fleet_ships > 0
        else owned_ships + chartered_ships
    )

    # Alphaliner 실제 원본 필드: orderTeu / orderShips
    orderbook_teu = parse_int(first_value(raw, [
        "orderTeu",
        "orderbookTeu",
        "orderbook_teu",
        "onOrderTeu",
    ]))

    orderbook_ships = parse_int(first_value(raw, [
        "orderShips",
        "orderbookShips",
        "orderbook_ships",
        "onOrderShips",
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
        if fleet_teu > 0
        else 0.0
    )

    chartered_share = (
        round(chartered_teu / fleet_teu * 100, 2)
        if fleet_teu > 0
        else 0.0
    )

    orderbook_share = (
        round(orderbook_teu / fleet_teu * 100, 2)
        if fleet_teu > 0
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

        # 기존 DB/JSON 호환 필드
        "teu": fleet_teu,
        "ships": fleet_ships,
        "teu_share": 0.0,
        "orderbook_teu_share": orderbook_share,
    }

def normalize_records(
    raw_records: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    중복 Rank가 있을 경우 Fleet TEU가 큰 행을 유지한다.
    """
    best_by_rank: Dict[int, Dict[str, Any]] = {}

    for index, raw in enumerate(raw_records, start=1):
        if not isinstance(raw, dict):
            continue

        record = normalize_operator_record(raw, fallback_rank=index)

        if record["rank"] <= 0:
            continue

        if record["fleet_teu"] <= 0:
            logger.warning(
                "Skipping invalid row: rank=%s operator=%s fleet_teu=%s",
                record["rank"],
                record["operator"],
                record["fleet_teu"],
            )
            continue

        existing = best_by_rank.get(record["rank"])

        if existing is None or record["fleet_teu"] > existing["fleet_teu"]:
            best_by_rank[record["rank"]] = record

    return sorted(
        best_by_rank.values(),
        key=lambda row: row["rank"],
    )

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

        # 기존 DB / JSON 호환
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

    if (
        metrics["top10_share"] <= 0
        or metrics["top10_share"] > MAX_TOP10_SHARE * 100
    ):
        errors.append(
            f"Top 10 concentration invalid: "
            f"{metrics['top10_share']:.2f}%"
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

        rows = [
            (
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
            )
            for operator in operators
        ]

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
              AND COALESCE(total_teu, global_total_teu, 0) >= ?
              AND COALESCE(total_ships, global_total_ships, 0) >= ?
            ORDER BY snapshot_date DESC
        """, (
            MIN_TOTAL_FLEET_TEU,
            MIN_TOTAL_FLEET_SHIPS,
        )).fetchall()

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
                    "Skipping snapshot %s from JSON export: operators=%s",
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
                "No usable valid snapshots remain. "
                "Existing JSON will not be overwritten."
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

        logger.info("Exported valid JSON contract: %s", JSON_PATH)
        return True

    finally:
        conn.close()

# =========================================================
# 11. Static HTML Dashboard
# =========================================================

def generate_html() -> None:
    html = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Alphaliner TOP 100 | MI Terminal</title>
<style>
:root {
    --bg: #090d12;
    --surface: #10161e;
    --surface-2: #151e29;
    --surface-3: #1a2532;
    --border: #263443;
    --border-soft: #1d2935;
    --text: #e7edf4;
    --muted: #8190a0;
    --dim: #5f6d7b;
    --amber: #ff9f1c;
    --cyan: #42c8e8;
    --blue: #5faeff;
    --green: #3ddc97;
    --red: #ff6875;
}
* { box-sizing: border-box; }
body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: Arial, Helvetica, sans-serif;
}
.header {
    border-bottom: 1px solid var(--border);
    background: #0c1118;
    padding: 18px 26px 16px;
}
.terminal-label {
    color: var(--amber);
    font-family: Consolas, Monaco, monospace;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 1.5px;
}
h1 {
    margin: 7px 0 4px;
    font-size: 22px;
    line-height: 1.2;
    letter-spacing: .5px;
}
.subtitle {
    color: var(--muted);
    font-size: 12px;
    letter-spacing: .2px;
}
.container {
    padding: 20px 26px 36px;
    max-width: 1800px;
    margin: 0 auto;
}
.toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 16px;
}
.control-group {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
}
select, input, button {
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
    font-size: 12px;
    height: 34px;
    padding: 0 10px;
}
select:focus, input:focus {
    outline: 1px solid var(--cyan);
    border-color: var(--cyan);
}
input { width: 230px; }
button {
    cursor: pointer;
    color: var(--amber);
    font-weight: 700;
    letter-spacing: .5px;
}
button:hover {
    background: var(--surface-3);
    border-color: var(--amber);
}
.status {
    font-family: Consolas, Monaco, monospace;
    color: var(--muted);
    font-size: 11px;
    text-align: right;
}
.kpis {
    display: grid;
    grid-template-columns: repeat(4, minmax(180px, 1fr));
    gap: 12px;
    margin-bottom: 16px;
}
.card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-top: 2px solid var(--amber);
    min-height: 126px;
    padding: 14px 16px;
}
.card-label {
    color: var(--muted);
    font-family: Consolas, Monaco, monospace;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: .7px;
    text-transform: uppercase;
}
.card-value {
    margin-top: 14px;
    color: var(--text);
    font-family: Consolas, Monaco, monospace;
    font-size: 25px;
    font-weight: 700;
}
.card-subvalue {
    margin-top: 5px;
    color: var(--cyan);
    font-family: Consolas, Monaco, monospace;
    font-size: 11px;
}
.card-delta {
    margin-top: 8px;
    font-family: Consolas, Monaco, monospace;
    font-size: 11px;
}
.up { color: var(--green); }
.down { color: var(--red); }
.flat { color: var(--muted); }
.table-shell {
    border: 1px solid var(--border);
    background: var(--surface);
    overflow-x: auto;
}
table {
    width: 100%;
    min-width: 1380px;
    border-collapse: collapse;
}
thead tr:first-child th {
    background: #0c131b;
    color: var(--amber);
    border-bottom: 1px solid var(--border);
    font-family: Consolas, Monaco, monospace;
    font-size: 10px;
    letter-spacing: .7px;
    text-transform: uppercase;
}
thead tr:nth-child(2) th {
    background: var(--surface-2);
    color: #aebdcc;
    border-bottom: 1px solid var(--border);
    font-family: Consolas, Monaco, monospace;
    font-size: 10px;
}
th, td {
    padding: 10px 10px;
    border-bottom: 1px solid var(--border-soft);
    text-align: right;
    white-space: nowrap;
}
th { font-weight: 700; }
td {
    color: #d9e1e9;
    font-family: Consolas, Monaco, monospace;
    font-size: 11px;
}
th.left, td.left { text-align: left; }
tbody tr:hover { background: var(--surface-3); }
.rank { color: var(--amber); font-weight: 700; }
.operator {
    color: var(--text);
    font-family: Arial, Helvetica, sans-serif;
    font-size: 12px;
    font-weight: 700;
}
.dim { color: var(--dim); }
.error {
    padding: 16px;
    color: #ffd0d4;
    background: #35151b;
    border: 1px solid #73303a;
}
@media (max-width: 950px) {
    .container { padding: 16px 12px 28px; }
    .header { padding: 16px 12px; }
    .toolbar { align-items: flex-start; flex-direction: column; }
    .status { text-align: left; }
    .kpis { grid-template-columns: repeat(2, minmax(150px, 1fr)); }
    input { width: 190px; }
}
</style>
</head>
<body>
<header class="header">
    <div class="terminal-label">MI TERMINAL // SHIPPING INTELLIGENCE</div>
    <h1>ALPHALINER TOP 100 LINER OPERATORS</h1>
    <div class="subtitle">Fleet capacity, ownership structure, charter exposure and orderbook monitoring</div>
</header>

<main class="container">
    <div class="toolbar">
        <div class="control-group">
            <select id="dateSelect" aria-label="Snapshot date"></select>
            <input id="searchInput" type="search" placeholder="Search operator..." autocomplete="off">
            <button id="csvButton" type="button">CSV EXPORT</button>
        </div>
        <div class="status" id="status">LOADING DATA...</div>
    </div>

    <section class="kpis">
        <article class="card">
            <div class="card-label">Total Ships</div>
            <div class="card-value" id="totalShips">-</div>
            <div class="card-delta" id="totalShipsDelta">-</div>
        </article>
        <article class="card">
            <div class="card-label">Total Fleet TEU</div>
            <div class="card-value" id="totalTeu">-</div>
            <div class="card-delta" id="totalTeuDelta">-</div>
        </article>
        <article class="card">
            <div class="card-label">Top 10 Concentration</div>
            <div class="card-value" id="top10Share">-</div>
            <div class="card-subvalue" id="top10Teu">-</div>
            <div class="card-delta" id="top10ShareDelta">-</div>
        </article>
        <article class="card">
            <div class="card-label">Orderbook</div>
            <div class="card-value" id="orderbook">-</div>
            <div class="card-subvalue" id="orderbookShare">-</div>
            <div class="card-delta" id="orderbookDelta">-</div>
        </article>
    </section>

    <div class="table-shell">
        <table>
            <thead>
                <tr>
                    <th rowspan="2" class="left">Rank</th>
                    <th rowspan="2" class="left">Operator</th>
                    <th colspan="3">Total Fleet</th>
                    <th colspan="3">Owned</th>
                    <th colspan="3">Chartered</th>
                    <th colspan="3">Orderbook</th>
                </tr>
                <tr>
                    <th>TEU</th>
                    <th>Ships</th>
                    <th>Share</th>
                    <th>TEU</th>
                    <th>Ships</th>
                    <th>% Fleet</th>
                    <th>TEU</th>
                    <th>Ships</th>
                    <th>% Fleet</th>
                    <th>TEU</th>
                    <th>Ships</th>
                    <th>% Fleet</th>
                </tr>
            </thead>
            <tbody id="tableBody"></tbody>
        </table>
    </div>
</main>

<script>
const nf = new Intl.NumberFormat('en-US');
let dashboardData = null;
let selectedDate = null;

function num(value) {
    return Number(value || 0);
}

function fmt(value) {
    return nf.format(Math.round(num(value)));
}

function fmtPct(value) {
    return num(value).toFixed(2) + '%';
}

function getFleetTeu(row) {
    return num(row.fleet_teu || row.teu);
}

function getFleetShips(row) {
    return num(row.fleet_ships || row.ships);
}

function getMeta(snapshot) {
    return snapshot && snapshot.meta ? snapshot.meta : {};
}

function metaTeu(meta) {
    return num(meta.total_teu || meta.global_total_teu || meta.top100_total_teu);
}

function metaShips(meta) {
    return num(meta.total_ships || meta.global_total_ships || meta.top100_total_ships);
}

function deltaNumber(current, previous, unit = '') {
    if (previous === null || previous === undefined) {
        return '<span class="flat">NO PRIOR SNAPSHOT</span>';
    }

    const diff = num(current) - num(previous);

    if (diff === 0) {
        return '<span class="flat">UNCHANGED VS PRIOR</span>';
    }

    const cls = diff > 0 ? 'up' : 'down';
    const sign = diff > 0 ? '+' : '';

    return '<span class="' + cls + '">' +
        sign + fmt(diff) + unit + ' VS PRIOR</span>';
}

function deltaBps(current, previous) {
    if (previous === null || previous === undefined) {
        return '<span class="flat">NO PRIOR SNAPSHOT</span>';
    }

    const bps = Math.round((num(current) - num(previous)) * 100);

    if (bps === 0) {
        return '<span class="flat">UNCHANGED VS PRIOR</span>';
    }

    const cls = bps > 0 ? 'up' : 'down';
    const sign = bps > 0 ? '+' : '';

    return '<span class="' + cls + '">' +
        sign + nf.format(bps) + ' BPS VS PRIOR</span>';
}

function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

function getFilteredRows() {
    if (!dashboardData || !selectedDate) return [];

    const snapshot = dashboardData.snapshots[selectedDate];
    const query = document.getElementById('searchInput').value
        .trim()
        .toLowerCase();

    const rows = snapshot.operators || [];

    if (!query) return rows;

    return rows.filter(row => {
        return String(row.operator || '').toLowerCase().includes(query) ||
            String(row.rank || '').includes(query);
    });
}

function renderTable() {
    const body = document.getElementById('tableBody');
    const rows = getFilteredRows();

    body.innerHTML = rows.map(row => {
        const fleetTeu = getFleetTeu(row);
        const fleetShare = fleetTeu > 0
            ? (fleetTeu / currentTotalTeu() * 100)
            : 0;

        return `
            <tr>
                <td class="left rank">${escapeHtml(row.rank)}</td>
                <td class="left operator">${escapeHtml(row.operator)}</td>
                <td>${fmt(fleetTeu)}</td>
                <td>${fmt(getFleetShips(row))}</td>
                <td>${fmtPct(fleetShare)}</td>
                <td>${fmt(row.owned_teu)}</td>
                <td>${fmt(row.owned_ships)}</td>
                <td>${fmtPct(row.owned_share)}</td>
                <td>${fmt(row.chartered_teu)}</td>
                <td>${fmt(row.chartered_ships)}</td>
                <td>${fmtPct(row.chartered_share)}</td>
                <td>${fmt(row.orderbook_teu)}</td>
                <td>${fmt(row.orderbook_ships)}</td>
                <td>${fmtPct(row.orderbook_share || row.orderbook_teu_share)}</td>
            </tr>
        `;
    }).join('');

    if (!rows.length) {
        body.innerHTML = `
            <tr>
                <td colspan="14" class="dim left">
                    NO MATCHING OPERATORS
                </td>
            </tr>
        `;
    }
}

function currentTotalTeu() {
    if (!dashboardData || !selectedDate) return 0;
    const snapshot = dashboardData.snapshots[selectedDate];
    return metaTeu(getMeta(snapshot));
}

function renderSnapshot() {
    const snapshot = dashboardData.snapshots[selectedDate];
    const meta = getMeta(snapshot);
    const dates = dashboardData.dates || [];

    const currentIndex = dates.indexOf(selectedDate);
    const priorDate = currentIndex >= 0 ? dates[currentIndex + 1] : null;
    const priorMeta = priorDate
        ? getMeta(dashboardData.snapshots[priorDate])
        : null;

    const totalTeu = metaTeu(meta);
    const totalShips = metaShips(meta);
    const orderbookTeu = num(meta.orderbook_total_teu);
    const orderbookShare = totalTeu > 0
        ? orderbookTeu / totalTeu * 100
        : 0;

    document.getElementById('totalShips').textContent = fmt(totalShips);
    document.getElementById('totalTeu').textContent = fmt(totalTeu);
    document.getElementById('top10Share').textContent = fmtPct(meta.top10_share);
    document.getElementById('top10Teu').textContent =
        'TOP 10: ' + fmt(meta.top10_total_teu) + ' TEU';
    document.getElementById('orderbook').textContent = fmt(orderbookTeu);
    document.getElementById('orderbookShare').textContent =
        fmtPct(orderbookShare) + ' OF FLEET';

    document.getElementById('totalShipsDelta').innerHTML =
        deltaNumber(
            totalShips,
            priorMeta ? metaShips(priorMeta) : null
        );

    document.getElementById('totalTeuDelta').innerHTML =
        deltaNumber(
            totalTeu,
            priorMeta ? metaTeu(priorMeta) : null
        );

    document.getElementById('top10ShareDelta').innerHTML =
        deltaBps(
            meta.top10_share,
            priorMeta ? priorMeta.top10_share : null
        );

    document.getElementById('orderbookDelta').innerHTML =
        deltaNumber(
            orderbookTeu,
            priorMeta ? priorMeta.orderbook_total_teu : null
        );

    renderTable();

    document.getElementById('status').textContent =
        'SNAPSHOT ' + selectedDate +
        ' // OPERATORS ' + (snapshot.operators || []).length +
        ' // EXTRACTED ' + (meta.extracted_at || '-');
}

function csvCell(value) {
    const text = String(value ?? '');
    return '"' + text.replace(/"/g, '""') + '"';
}

function exportCsv() {
    const rows = getFilteredRows();

    const header = [
        'Rank', 'Operator',
        'Fleet TEU', 'Fleet Ships', 'Fleet Share %',
        'Owned TEU', 'Owned Ships', 'Owned Share %',
        'Chartered TEU', 'Chartered Ships', 'Chartered Share %',
        'Orderbook TEU', 'Orderbook Ships', 'Orderbook Share %'
    ];

    const totalTeu = currentTotalTeu();

    const lines = [header.map(csvCell).join(',')];

    rows.forEach(row => {
        const fleetTeu = getFleetTeu(row);
        const fleetShare = totalTeu > 0
            ? fleetTeu / totalTeu * 100
            : 0;

        lines.push([
            row.rank,
            row.operator,
            fleetTeu,
            getFleetShips(row),
            fleetShare.toFixed(2),
            num(row.owned_teu),
            num(row.owned_ships),
            num(row.owned_share).toFixed(2),
            num(row.chartered_teu),
            num(row.chartered_ships),
            num(row.chartered_share).toFixed(2),
            num(row.orderbook_teu),
            num(row.orderbook_ships),
            num(row.orderbook_share || row.orderbook_teu_share).toFixed(2)
        ].map(csvCell).join(','));
    });

    const blob = new Blob(
        ['\uFEFF' + lines.join('\n')],
        { type: 'text/csv;charset=utf-8;' }
    );

    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download =
        'alphaliner_top100_' + selectedDate + '.csv';

    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(link.href);
}

async function init() {
    try {
        const response = await fetch(
            './alphaliner_data.json?ts=' + Date.now()
        );

        if (!response.ok) {
            throw new Error('Could not load alphaliner_data.json');
        }

        dashboardData = await response.json();

        const dates = dashboardData.dates || [];

        if (!dates.length) {
            throw new Error('No valid snapshots available.');
        }

        const select = document.getElementById('dateSelect');

        dates.forEach(date => {
            const option = document.createElement('option');
            option.value = date;
            option.textContent = date;
            select.appendChild(option);
        });

        selectedDate = dashboardData.latest_date || dates[0];
        select.value = selectedDate;

        select.addEventListener('change', function() {
            selectedDate = this.value;
            renderSnapshot();
        });

        document.getElementById('searchInput')
            .addEventListener('input', renderTable);

        document.getElementById('csvButton')
            .addEventListener('click', exportCsv);

        renderSnapshot();

    } catch (error) {
        document.querySelector('.container').innerHTML =
            '<div class="error">DASHBOARD DATA COULD NOT BE LOADED: ' +
            escapeHtml(error.message) +
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
        "Candidate snapshot metrics | operators=%s | total_teu=%s | "
        "total_ships=%s | top10_share=%.2f%% | orderbook_teu=%s",
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

        # 기존 정상 JSON/HTML이 있으면 유지한다.
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
