# -*- coding: utf-8 -*-
"""
Alphaliner TOP 100 Liner Operators Static Dashboard Crawler (GitHub Optimized)
- Direct ExtJS Store extraction (Top100description)
- Historical snapshot comparison & Delta analysis

Output files:
- alphaliner_data.db
- alphaliner_data.json
- index.html
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
from threading import Lock

import pandas as pd
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

DB_PATH = BASE_DIR / "alphaliner_data.db"
JSON_PATH = BASE_DIR / "alphaliner_data.json"
HTML_PATH = BASE_DIR / "index.html"
LOG_PATH = BASE_DIR / "alphaliner_crawl.log"

ALPHALINER_URL = "https://alphaliner.axsmarine.com/PublicTop100/"

PAGE_LOAD_WAIT_SECONDS = 8
SELENIUM_WAIT_SECONDS = 30

app = Flask(__name__)
crawl_lock = Lock()

# =========================================================
# 2. Logging
# =========================================================
logger = logging.getLogger("alphaliner")
logger.setLevel(logging.INFO)
logger.handlers.clear()

formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

def activate_file_logging():
    """Enable file logging once."""
    if any(isinstance(handler, logging.FileHandler) for handler in logger.handlers):
        return

    try:
        file_handler = logging.FileHandler(LOG_PATH, mode="a", encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except Exception:
        pass

def log(message):
    logger.info(message)

# =========================================================
# 3. Common Utilities
# =========================================================
def normalize_text(value):
    return " ".join(str(value or "").strip().split())

def safe_int(value):
    """Convert integer-like text/number to int safely."""
    if value is None:
        return None

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if pd.isna(value):
            return None
        return int(round(float(value)))

    text = str(value).strip()
    if not text or text.lower() in {"-", "n/a", "na", "none", "null", "--"}:
        return None

    text = text.replace(",", "").replace(" ", "")
    text = re.sub(r"[^0-9.\-]", "", text)

    if not text or text in {"-", ".", "-."}:
        return None

    try:
        return int(round(float(text)))
    except (TypeError, ValueError):
        return None

def safe_float(value):
    text = str(value or "").strip()
    if not text or text.lower() in {"-", "n/a", "na", "none", "null", "--"}:
        return None

    is_negative = text.startswith("-") or (text.startswith("(") and text.endswith(")"))
    cleaned = text.replace(",", "")
    cleaned = re.sub(r"[^0-9.]", "", cleaned)

    if not cleaned:
        return None

    try:
        number = float(cleaned)
        return -number if is_negative else number
    except ValueError:
        return None

def atomic_write_text(path, text):
    """Write a file atomically to prevent incomplete dashboard files."""
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(text, encoding="utf-8")
    temporary_path.replace(path)

def dataframe_to_records(dataframe, date_columns=None):
    """Convert DataFrame to JSON-safe record list."""
    if dataframe is None or dataframe.empty:
        return []

    output = dataframe.copy()
    if date_columns:
        for column in date_columns:
            if column in output.columns:
                output[column] = pd.to_datetime(
                    output[column],
                    errors="coerce",
                ).dt.strftime("%Y-%m-%d")

    output = output.replace([float("inf"), float("-inf")], pd.NA)
    output = output.where(pd.notnull(output), None)

    return json.loads(
        output.to_json(
            orient="records",
            force_ascii=False,
            date_format="iso",
        )
    )

# =========================================================
# 4. SQLite Database
# =========================================================
def get_db_conn():
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=30000")

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS alphaliner_master (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            captured_at TEXT NOT NULL,
            data_date TEXT NOT NULL,
            data_hash TEXT NOT NULL UNIQUE,
            total_teu INTEGER,
            total_ships INTEGER,
            operator_count INTEGER NOT NULL
        )
        """
    )

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS alphaliner_detail (
            snapshot_id INTEGER NOT NULL,
            rank INTEGER NOT NULL,
            operator_name TEXT NOT NULL,
            total_teu INTEGER NOT NULL,
            total_ships INTEGER,
            market_share REAL,
            existing_owned_teu INTEGER,
            existing_owned_ships INTEGER,
            existing_chartered_teu INTEGER,
            existing_chartered_ships INTEGER,
            orderbook_teu INTEGER,
            orderbook_ships INTEGER,
            orderbook_pct_existing REAL,
            PRIMARY KEY (snapshot_id, rank),
            FOREIGN KEY (snapshot_id) REFERENCES alphaliner_master(snapshot_id)
        )
        """
    )

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS crawl_audit (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            status TEXT NOT NULL,
            message TEXT,
            snapshot_id INTEGER,
            operator_count INTEGER,
            data_date TEXT
        )
        """
    )

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_alphaliner_master_data_date
        ON alphaliner_master (data_date)
        """
    )

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_alphaliner_detail_operator
        ON alphaliner_detail (operator_name)
        """
    )

    connection.commit()
    return connection

def get_total_snapshot_count():
    connection = get_db_conn()
    try:
        row = connection.execute("SELECT COUNT(*) FROM alphaliner_master").fetchone()
        return int(row[0] if row else 0)
    finally:
        connection.close()

def get_last_sync_meta():
    connection = get_db_conn()
    try:
        row = connection.execute(
            """
            SELECT started_at, finished_at, status, message, snapshot_id, operator_count, data_date
            FROM crawl_audit ORDER BY audit_id DESC LIMIT 1
            """
        ).fetchone()

        if not row:
            return {}

        return {
            "started_at": row[0],
            "finished_at": row[1],
            "status": row[2],
            "message": row[3],
            "snapshot_id": row[4],
            "operator_count": row[5],
            "data_date": row[6],
        }
    finally:
        connection.close()

def save_crawl_audit(started_at, finished_at, status, message, snapshot_id=None, operator_count=0, data_date=None):
    connection = get_db_conn()
    try:
        connection.execute(
            """
            INSERT INTO crawl_audit (started_at, finished_at, status, message, snapshot_id, operator_count, data_date)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                started_at,
                finished_at,
                status,
                str(message)[:1000],
                snapshot_id,
                int(operator_count or 0),
                str(data_date or ""),
            ),
        )
        connection.commit()
    finally:
        connection.close()

def make_data_hash(dataframe, data_date):
    if dataframe is None:
        dataframe = pd.DataFrame()

    working = dataframe.copy().sort_values("rank").reset_index(drop=True)
    payload = {
        "data_date": str(data_date or ""),
        "rows": dataframe_to_records(working),
    }

    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def save_snapshot(bundle):
    dataframe = bundle.get("df", pd.DataFrame()).copy()
    data_date = bundle.get("data_date", datetime.now().strftime("%Y-%m-%d"))

    if dataframe.empty:
        raise RuntimeError("저장할 Alphaliner 데이터가 없습니다.")

    data_hash = make_data_hash(dataframe, data_date)
    connection = get_db_conn()

    try:
        cursor = connection.cursor()
        existing = cursor.execute(
            "SELECT snapshot_id FROM alphaliner_master WHERE data_hash = ?",
            (data_hash,),
        ).fetchone()

        if existing:
            log(f"[DB] 동일 Alphaliner 데이터 중복 (snapshot_id={existing[0]})")
            return int(existing[0]), False

        captured_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        total_teu = int(dataframe["total_teu"].sum()) if "total_teu" in dataframe.columns else 0
        total_ships = int(dataframe["total_ships"].sum()) if "total_ships" in dataframe.columns else 0

        cursor.execute(
            """
            INSERT INTO alphaliner_master (captured_at, data_date, data_hash, total_teu, total_ships, operator_count)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (captured_at, data_date, data_hash, total_teu, total_ships, int(len(dataframe))),
        )

        snapshot_id = cursor.lastrowid
        insert_rows = []

        for _, row in dataframe.iterrows():
            insert_rows.append(
                (
                    snapshot_id,
                    int(row["rank"]),
                    str(row["operator_name"]),
                    int(row["total_teu"]),
                    int(row.get("total_ships", 0)) if pd.notna(row.get("total_ships")) else None,
                    float(row.get("market_share", 0.0)) if pd.notna(row.get("market_share")) else None,
                    safe_int(row.get("existing_owned_teu")),
                    safe_int(row.get("existing_owned_ships")),
                    safe_int(row.get("existing_chartered_teu")),
                    safe_int(row.get("existing_chartered_ships")),
                    safe_int(row.get("orderbook_teu")),
                    safe_int(row.get("orderbook_ships")),
                    safe_float(row.get("orderbook_pct_existing")),
                )
            )

        cursor.executemany(
            """
            INSERT INTO alphaliner_detail (
                snapshot_id, rank, operator_name, total_teu, total_ships, market_share,
                existing_owned_teu, existing_owned_ships, existing_chartered_teu, existing_chartered_ships,
                orderbook_teu, orderbook_ships, orderbook_pct_existing
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            insert_rows,
        )

        connection.commit()
        log(f"[DB] 신규 Alphaliner Snapshot 저장 완료: id={snapshot_id}, operators={len(dataframe)}")
        return int(snapshot_id), True

    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

def load_history_df():
    connection = get_db_conn()
    try:
        dataframe = pd.read_sql_query(
            """
            SELECT snapshot_id, captured_at, data_date, total_teu, total_ships, operator_count
            FROM alphaliner_master ORDER BY data_date ASC, snapshot_id ASC
            """,
            connection,
        )
    finally:
        connection.close()

    if dataframe.empty:
        return pd.DataFrame()

    dataframe["data_date"] = pd.to_datetime(dataframe["data_date"], errors="coerce")
    dataframe["captured_at"] = pd.to_datetime(dataframe["captured_at"], errors="coerce")

    return (
        dataframe.dropna(subset=["data_date"])
        .sort_values(["data_date", "snapshot_id"])
        .drop_duplicates(subset=["data_date"], keep="last")
        .reset_index(drop=True)
    )

def load_latest_detail_df():
    connection = get_db_conn()
    try:
        dataframe = pd.read_sql_query(
            """
            SELECT d.*, m.data_date, m.captured_at
            FROM alphaliner_detail AS d
            INNER JOIN alphaliner_master AS m ON d.snapshot_id = m.snapshot_id
            WHERE d.snapshot_id = (
                SELECT snapshot_id FROM alphaliner_master
                ORDER BY data_date DESC, snapshot_id DESC LIMIT 1
            )
            ORDER BY d.rank ASC
            """,
            connection,
        )
    finally:
        connection.close()

    return dataframe

def load_previous_detail_df():
    """Load the second latest snapshot for delta comparison."""
    connection = get_db_conn()
    try:
        # 직전 snapshot_id 탐색
        cur = connection.cursor()
        rows = cur.execute(
            """
            SELECT snapshot_id, data_date, captured_at
            FROM alphaliner_master
            ORDER BY data_date DESC, snapshot_id DESC
            LIMIT 2
            """
        ).fetchall()

        if len(rows) < 2:
            return pd.DataFrame()

        previous_snapshot_id = rows[1][0]

        dataframe = pd.read_sql_query(
            """
            SELECT d.*, m.data_date, m.captured_at
            FROM alphaliner_detail AS d
            INNER JOIN alphaliner_master AS m ON d.snapshot_id = m.snapshot_id
            WHERE d.snapshot_id = ?
            ORDER BY d.rank ASC
            """,
            connection,
            params=(previous_snapshot_id,),
        )
    finally:
        connection.close()

    return dataframe

# =========================================================
# 5. Selenium Driver & ExtJS Parser
# =========================================================
def get_driver(driver_path="", headless=True):
    options = Options()
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--lang=en-US")
    options.add_argument("--window-size=1920,1080")

    if headless:
        options.add_argument("--headless=new")

    chrome_binary = os.getenv("CHROME_BINARY", "").strip()
    if chrome_binary:
        options.binary_location = chrome_binary

    if driver_path:
        return webdriver.Chrome(service=Service(driver_path), options=options)

    try:
        return webdriver.Chrome(options=options)
    except WebDriverException as first_error:
        if not HAS_WEBDRIVER_MANAGER:
            raise first_error
        installed_driver = ChromeDriverManager().install()
        return webdriver.Chrome(service=Service(installed_driver), options=options)

def wait_for_page_ready(driver):
    wait = WebDriverWait(driver, SELENIUM_WAIT_SECONDS)
    wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
    wait.until(lambda d: len(d.execute_script("return (document.body.innerText || '').trim()")) > 50)

def extract_alphaliner_payload(driver):
    script = r"""
    function cleanText(value) {
        return String(value || "").replace(/\s+/g, " ").trim();
    }

    const out = {
        page_text: cleanText(document.body ? document.body.innerText : ""),
        summary: {},
        regional: {},
        store_id: null,
        rows: [],
        available_stores: []
    };

    try {
        const text = out.page_text;
        const activeShips = text.match(/([0-9,]+)\s+active ships/i);
        const totalTeu = text.match(/([0-9,]+)\s+TEU/i);

        out.summary.total_active_ships = activeShips ? activeShips[1] : null;
        out.summary.total_teu = totalTeu ? totalTeu[1] : null;

        const regionalBlock = text.match(
            /Regional Trades[\s\S]*?weekly capacities[\s\S]*?Trans-Atlantic[\s\S]*?Trans-Pacific[\s\S]*?Feast-Europe[\s\S]*?([0-9,]+)\s+TEU[\s\S]*?([0-9,]+)\s+TEU[\s\S]*?([0-9,]+)\s+TEU/i
        );

        if (regionalBlock) {
            out.regional.trans_atlantic = regionalBlock[1];
            out.regional.trans_pacific = regionalBlock[2];
            out.regional.feast_europe = regionalBlock[3];
        }
    } catch (error) {
        out.summary_error = String(error);
    }

    try {
        if (typeof Ext !== "undefined" && Ext.StoreManager && Ext.StoreManager.items) {
            let targetStore = null;
            Ext.StoreManager.items.forEach(function(store) {
                try {
                    if (store && store.storeId) {
                        out.available_stores.push(store.storeId);
                    }
                    if (!targetStore && store && store.storeId === "Top100description") {
                        targetStore = store;
                    }
                } catch (ignore) {}
            });

            if (targetStore) {
                out.store_id = targetStore.storeId;
                for (let i = 0; i < targetStore.getCount(); i++) {
                    const record = targetStore.getAt(i);
                    if (record && record.data) {
                        out.rows.push(record.data);
                    }
                }
            }
        }
    } catch (error) {
        out.store_error = String(error);
    }

    return out;
    """
    return driver.execute_script(script)

def parse_alphaliner_table(raw_rows):
    if not raw_rows:
        return pd.DataFrame()

    parsed = []
    for index, raw in enumerate(raw_rows, start=1):
        if not isinstance(raw, dict):
            continue

        rank_val = safe_int(raw.get("rank")) or safe_int(raw.get("Rank")) or safe_int(raw.get("ranking")) or index
        operator_name = normalize_text(raw.get("operator") or raw.get("Operator") or raw.get("name"))
        total_teu = safe_int(raw.get("totalTeu") or raw.get("total_teu") or raw.get("Total_TEU"))

        if not operator_name or total_teu is None or total_teu <= 0:
            continue

        parsed.append(
            {
                "rank": rank_val,
                "operator_name": operator_name,
                "total_teu": total_teu,
                "total_ships": safe_int(raw.get("totalShips") or raw.get("total_ships") or raw.get("Total_Ships")),
                "market_share": safe_float(raw.get("percent") or raw.get("marketShare") or raw.get("market_share") or raw.get("Market_Share")),
                "existing_owned_teu": safe_int(raw.get("ownedTeu") or raw.get("owned_teu") or raw.get("Owned_TEU")),
                "existing_owned_ships": safe_int(raw.get("ownedShips") or raw.get("owned_ships") or raw.get("Owned_Ships")),
                "existing_chartered_teu": safe_int(raw.get("chartTeu") or raw.get("charteredTeu") or raw.get("chartered_teu") or raw.get("Chartered_TEU")),
                "existing_chartered_ships": safe_int(raw.get("chartShips") or raw.get("charteredShips") or raw.get("chartered_ships") or raw.get("Chartered_Ships")),
                "orderbook_teu": safe_int(raw.get("orderTeu") or raw.get("orderbookTeu") or raw.get("orderbook_teu") or raw.get("Orderbook_TEU")),
                "orderbook_ships": safe_int(raw.get("orderShips") or raw.get("orderbookShips") or raw.get("orderbook_ships") or raw.get("Orderbook_Ships")),
                "orderbook_pct_existing": safe_float(raw.get("orderProcents") or raw.get("orderPercent") or raw.get("orderbookPercent") or raw.get("orderbook_pct_existing") or raw.get("Orderbook_Percent")),
            }
        )

    dataframe = pd.DataFrame(parsed)
    if dataframe.empty:
        return pd.DataFrame()

    return dataframe.drop_duplicates(subset=["rank"], keep="first").sort_values("rank").reset_index(drop=True)

def fetch_alphaliner(driver_path="", headless=True):
    driver = None
    try:
        driver = get_driver(driver_path=driver_path, headless=headless)
        log("[FETCH] Alphaliner TOP 100 웹사이트 접속 중...")
        driver.get(ALPHALINER_URL)

        wait_for_page_ready(driver)

        # ExtJS Store 로딩 완료 대기
        wait = WebDriverWait(driver, SELENIUM_WAIT_SECONDS)
        wait.until(
            lambda d: d.execute_script(
                """
                try {
                    if (typeof Ext === "undefined" || !Ext.StoreManager || !Ext.StoreManager.items) {
                        return false;
                    }
                    let store = null;
                    Ext.StoreManager.items.forEach(function(item) {
                        if (!store && item && item.storeId === "Top100description") {
                            store = item;
                        }
                    });
                    return !!store && store.getCount() >= 90;
                } catch (error) {
                    return false;
                }
                """
            )
        )

        time.sleep(2)

        payload = extract_alphaliner_payload(driver)
        raw_rows = payload.get("rows", [])

        log(f"[FETCH] ExtJS Store 추출 결과: store={payload.get('store_id')}, raw_rows={len(raw_rows)}")

        dataframe = parse_alphaliner_table(raw_rows)

        if dataframe.empty:
            stores = ", ".join(payload.get("available_stores", [])[:30])
            raise RuntimeError(f"ExtJS Store 파싱 실패. 확인된 Store: {stores or '없음'}")

        if len(dataframe) < 90:
            raise RuntimeError(f"데이터 불완전: {len(dataframe)}건 수집 (최소 90건 필요)")

        dataframe = dataframe.sort_values("rank").head(100).reset_index(drop=True)
        data_date = datetime.now().strftime("%Y-%m-%d")

        populated_columns = {
            "total_ships": int(dataframe["total_ships"].notna().sum()),
            "market_share": int(dataframe["market_share"].notna().sum()),
            "owned_teu": int(dataframe["existing_owned_teu"].notna().sum()),
            "chartered_teu": int(dataframe["existing_chartered_teu"].notna().sum()),
            "orderbook_teu": int(dataframe["orderbook_teu"].notna().sum()),
        }

        log(f"[FETCH] 수집 성공: operators={len(dataframe)}, date={data_date}, populated={populated_columns}")

        return {
            "df": dataframe,
            "data_date": data_date,
        }

    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

# =========================================================
# 6. Pipeline Execution
# =========================================================
def run_alphaliner_sync(driver_path="", headless=True):
    with crawl_lock:
        started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            bundle = fetch_alphaliner(driver_path=driver_path, headless=headless)
            snapshot_id, is_new = save_snapshot(bundle)

            finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            dataframe = bundle.get("df", pd.DataFrame())

            save_crawl_audit(
                started_at=started_at,
                finished_at=finished_at,
                status="SUCCESS",
                message="NEW_SNAPSHOT" if is_new else "DUPLICATE_SNAPSHOT",
                snapshot_id=snapshot_id,
                operator_count=len(dataframe),
                data_date=bundle.get("data_date"),
            )

            return {
                "ok": True,
                "snapshot_id": snapshot_id,
                "is_new": is_new,
                "operator_count": len(dataframe),
            }

        except Exception as error:
            activate_file_logging()
            logger.exception(f"[ERROR] Alphaliner 수집 실패: {error}")
            finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            try:
                save_crawl_audit(
                    started_at=started_at,
                    finished_at=finished_at,
                    status="ERROR",
                    message=str(error),
                )
            except Exception:
                pass

            return {
                "ok": False,
                "error": str(error),
            }

def build_static_json():
    """Create data file consumed by the static dashboard."""
    history = load_history_df()
    latest_detail = load_latest_detail_df()
    previous_detail = load_previous_detail_df()

    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": {
            "name": "Alphaliner TOP 100",
            "url": ALPHALINER_URL,
        },
        "last_sync": get_last_sync_meta(),
        "history": dataframe_to_records(history, date_columns=["data_date", "captured_at"]),
        "latest_detail": dataframe_to_records(latest_detail, date_columns=["data_date", "captured_at"]),
        "previous_detail": dataframe_to_records(previous_detail, date_columns=["data_date", "captured_at"]),
    }

    atomic_write_text(
        JSON_PATH,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
    )
    log(f"[STATIC] JSON 생성 완료: {JSON_PATH.name}")

def generate_static_html():
    """Build a standalone static dashboard in index.html with Delta Comparison."""
    content = r"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Alphaliner TOP 100 Dashboard</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
  <style>
    :root {
      --navy: #102a43;
      --blue: #1677c8;
      --blue-light: #eaf4fb;
      --sky: #38a3db;
      --green: #16876e;
      --red: #c23b3b;
      --text: #243b53;
      --muted: #627d98;
      --border: #d9e2ec;
      --background: #f5f8fb;
      --white: #ffffff;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--background);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans KR", Arial, sans-serif;
    }
    .header {
      background: linear-gradient(120deg, #102a43, #1677c8);
      color: #ffffff;
      padding: 30px 24px;
    }
    .header-inner { max-width: 1440px; margin: 0 auto; }
    .header h1 { margin: 0; font-size: 28px; font-weight: 700; }
    .header p { margin: 9px 0 0; color: #d9efff; font-size: 14px; }
    .container { max-width: 1440px; margin: 0 auto; padding: 24px; }
    .status {
      display: flex;
      gap: 10px;
      align-items: center;
      margin-bottom: 20px;
      color: var(--muted);
      font-size: 13px;
    }
    .status-dot { width: 10px; height: 10px; border-radius: 50%; background: #94a3b8; }
    .status-dot.success { background: #16a34a; }
    .status-dot.error { background: #dc2626; }
    .section-title {
      font-size: 18px;
      font-weight: 700;
      color: var(--navy);
      margin: 28px 0 14px 0;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .section-title::before {
      content: "";
      display: inline-block;
      width: 4px;
      height: 18px;
      background: var(--blue);
      border-radius: 2px;
    }
    .kpi-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 16px;
      margin-bottom: 20px;
    }
    .card {
      background: var(--white);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 20px;
      box-shadow: 0 2px 8px rgba(16, 42, 67, 0.05);
    }
    .kpi-label { color: var(--muted); font-size: 13px; font-weight: 600; margin-bottom: 10px; }
    .kpi-value { color: var(--navy); font-size: 24px; font-weight: 700; }
    .kpi-sub { margin-top: 8px; color: var(--muted); font-size: 12px; }
    .chart-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 16px;
      margin-bottom: 20px;
    }
    .chart-card { min-height: 390px; }
    .chart-title { margin: 0 0 6px; color: var(--navy); font-size: 17px; }
    .chart-note { margin: 0 0 18px; color: var(--muted); font-size: 12px; }
    .chart-wrap { position: relative; height: 285px; }
    .table-card { overflow: hidden; padding: 0; margin-bottom: 24px; }
    .table-heading { padding: 20px 20px 12px; }
    .table-heading h2 { margin: 0; color: var(--navy); font-size: 18px; }
    .table-heading p { margin: 7px 0 0; color: var(--muted); font-size: 12px; }
    .table-scroll { overflow-x: auto; max-height: 600px; overflow-y: auto; }
    table { width: 100%; border-collapse: collapse; min-width: 900px; font-size: 13px; }
    thead { position: sticky; top: 0; z-index: 2; }
    th {
      background: #eaf4fb;
      color: #334e68;
      padding: 12px 14px;
      text-align: left;
      white-space: nowrap;
      border-bottom: 1px solid var(--border);
    }
    td { padding: 11px 14px; border-bottom: 1px solid #edf2f7; white-space: nowrap; }
    tbody tr:hover { background: #f8fbfd; }
    .number { text-align: right; }
    .rank { color: var(--blue); font-weight: 700; }

    /* Delta Badges & Colors */
    .delta-pos { color: #dc2626; font-weight: 700; }
    .delta-neg { color: #2563eb; font-weight: 700; }
    .delta-zero { color: #64748b; font-weight: 600; }
    .badge-rank-up {
      display: inline-block;
      padding: 2px 6px;
      border-radius: 4px;
      background: #fee2e2;
      color: #dc2626;
      font-size: 11px;
      font-weight: 700;
    }
    .badge-rank-down {
      display: inline-block;
      padding: 2px 6px;
      border-radius: 4px;
      background: #dbeafe;
      color: #2563eb;
      font-size: 11px;
      font-weight: 700;
    }

    .empty-message, .error-message, .info-box {
      padding: 20px;
      border-radius: 10px;
      background: var(--white);
      border: 1px solid var(--border);
      color: var(--muted);
      font-size: 13px;
    }
    .error-message { border-color: #fecaca; color: #b91c1c; background: #fef2f2; }
    .info-box { background: #f8fafc; border-color: #e2e8f0; margin-bottom: 16px; }
    .footer { padding: 20px 0 5px; color: var(--muted); font-size: 12px; text-align: right; }
    @media (max-width: 980px) { .kpi-grid, .chart-grid { grid-template-columns: 1fr 1fr; } }
    @media (max-width: 620px) {
      .container { padding: 16px; }
      .header { padding: 24px 16px; }
      .header h1 { font-size: 22px; }
      .kpi-grid, .chart-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header class="header">
    <div class="header-inner">
      <h1>Alphaliner TOP 100 Liner Operators</h1>
      <p>Global container liner operator capacity, fleet, orderbook & snapshot delta monitoring</p>
    </div>
  </header>

  <main class="container">
    <div class="status">
      <span id="statusDot" class="status-dot"></span>
      <span id="statusText">Alphaliner 데이터를 불러오는 중입니다.</span>
    </div>

    <section id="dashboard" style="display:none;">
      <!-- Highlights KPI -->
      <div class="kpi-grid">
        <article class="card">
          <div class="kpi-label">TOP 100 총 선복량</div>
          <div id="totalTeu" class="kpi-value">-</div>
          <div class="kpi-sub">Total Capacity</div>
        </article>
        <article class="card">
          <div class="kpi-label">운항 선사 수</div>
          <div id="operatorCount" class="kpi-value">-</div>
          <div class="kpi-sub">Operators in latest snapshot</div>
        </article>
        <article class="card">
          <div class="kpi-label">총 선박 수</div>
          <div id="totalShips" class="kpi-value">-</div>
          <div class="kpi-sub">Total Ships</div>
        </article>
        <article class="card">
          <div class="kpi-label">1위 선사</div>
          <div id="leaderName" class="kpi-value">-</div>
          <div id="leaderSub" class="kpi-sub">-</div>
        </article>
      </div>

      <!-- Delta Comparison Summary -->
      <div class="section-title">직전 스냅샷 대비 변동 요약 (Snapshot Delta Summary)</div>
      <div id="deltaSummaryContainer">
        <div class="kpi-grid">
          <article class="card">
            <div class="kpi-label">총 선복량 증감 (Δ TEU)</div>
            <div id="deltaTeuTotal" class="kpi-value">-</div>
            <div id="deltaTeuSub" class="kpi-sub">vs previous snapshot</div>
          </article>
          <article class="card">
            <div class="kpi-label">총 선박 수 증감 (Δ Ships)</div>
            <div id="deltaShipsTotal" class="kpi-value">-</div>
            <div id="deltaShipsSub" class="kpi-sub">vs previous snapshot</div>
          </article>
          <article class="card">
            <div class="kpi-label">최대 선복량 증가 선사</div>
            <div id="topGainName" class="kpi-value">-</div>
            <div id="topGainVal" class="kpi-sub">-</div>
          </article>
          <article class="card">
            <div class="kpi-label">최대 선복량 감소 선사</div>
            <div id="topLossName" class="kpi-value">-</div>
            <div id="topLossVal" class="kpi-sub">-</div>
          </article>
        </div>
      </div>

      <!-- Charts -->
      <div class="chart-grid">
        <section class="card chart-card">
          <h2 class="chart-title">상위 10개 선사 선복량</h2>
          <p class="chart-note">Latest snapshot · 단위: TEU</p>
          <div class="chart-wrap">
            <canvas id="topOperatorsChart"></canvas>
          </div>
        </section>

        <section class="card chart-card">
          <h2 class="chart-title">TOP 100 총 선복량 추이</h2>
          <p class="chart-note">Historical snapshots · 단위: Million TEU</p>
          <div class="chart-wrap">
            <canvas id="historyChart"></canvas>
          </div>
        </section>
      </div>

      <!-- Delta Comparison Table -->
      <section class="card table-card" id="deltaTableCard">
        <div class="table-heading">
          <h2>직전 스냅샷 대비 선사별 상세 변동 (Delta Detail)</h2>
          <p id="deltaTableNote">비교 대상 스냅샷 데이터를 불러오는 중...</p>
        </div>
        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Rank</th>
                <th>순위 변동</th>
                <th>Operator</th>
                <th class="number">현재 TEU</th>
                <th class="number">직전 TEU</th>
                <th class="number">증감 (Δ TEU)</th>
                <th class="number">현재 점유율</th>
                <th class="number">직전 점유율</th>
                <th class="number">점유율 증감 (Δ %p)</th>
              </tr>
            </thead>
            <tbody id="deltaTableBody"></tbody>
          </table>
        </div>
      </section>

      <!-- Latest TOP 100 Full Table -->
      <section class="card table-card">
        <div class="table-heading">
          <h2>최신 TOP 100 선사 전체 순위</h2>
          <p id="tableNote">-</p>
        </div>
        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Rank</th>
                <th>Operator</th>
                <th class="number">Total TEU</th>
                <th class="number">Ships</th>
                <th class="number">Market Share</th>
                <th class="number">Orderbook TEU</th>
                <th class="number">Orderbook / Existing</th>
              </tr>
            </thead>
            <tbody id="operatorTableBody"></tbody>
          </table>
        </div>
      </section>
    </section>

    <section id="messageBox"></section>

    <div class="footer">
      Source: <a href="https://alphaliner.axsmarine.com/PublicTop100/" target="_blank" rel="noopener">Alphaliner TOP 100</a>
      · Generated: <span id="generatedAt">-</span>
    </div>
  </main>

  <script>
    let topOperatorsChart = null;
    let historyChart = null;

    function numberFormat(value) {
      const number = Number(value || 0);
      return new Intl.NumberFormat("en-US").format(number);
    }

    function decimalFormat(value, digits = 2) {
      const number = Number(value || 0);
      return number.toLocaleString("en-US", {
        minimumFractionDigits: digits,
        maximumFractionDigits: digits
      });
    }

    function fmtDelta(val, isPct = false) {
      if (val === null || val === undefined || isNaN(val)) return "-";
      const num = Number(val);
      const sign = num > 0 ? "+" : "";
      if (isPct) {
        return sign + num.toFixed(2) + "%p";
      }
      return sign + numberFormat(num);
    }

    function deltaSpan(val, isPct = false) {
      if (val === null || val === undefined || isNaN(val) || val === 0) {
        return '<span class="delta-zero">-</span>';
      }
      const num = Number(val);
      const cls = num > 0 ? "delta-pos" : "delta-neg";
      const arrow = num > 0 ? "▲ " : "▼ ";
      return `<span class="${cls}">${arrow}${fmtDelta(num, isPct)}</span>`;
    }

    function rankMoveBadge(diff) {
      if (!diff || isNaN(diff) || diff === 0) {
        return '<span class="delta-zero">-</span>';
      }
      if (diff > 0) {
        return `<span class="badge-rank-up">▲ Up ${diff}</span>`;
      }
      return `<span class="badge-rank-down">▼ Down ${Math.abs(diff)}</span>`;
    }

    function escapeHtml(value) {
      return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
    }

    function showError(message) {
      document.getElementById("statusDot").className = "status-dot error";
      document.getElementById("statusText").textContent = "데이터 로드 오류";
      document.getElementById("messageBox").innerHTML =
        '<div class="error-message"><strong>Alphaliner Dashboard 데이터를 표시할 수 없습니다.</strong><br>' +
        escapeHtml(message) +
        '<br><br>GitHub 저장소의 <code>Alphaliner/alphaliner_data.json</code> 파일 생성 여부를 확인해 주세요.</div>';
    }

    function renderKpis(rows) {
      const totalTeu = rows.reduce((sum, row) => sum + Number(row.total_teu || 0), 0);
      const totalShips = rows.reduce((sum, row) => sum + Number(row.total_ships || 0), 0);
      const leader = rows.length ? rows[0] : null;

      document.getElementById("totalTeu").textContent = numberFormat(totalTeu) + " TEU";
      document.getElementById("operatorCount").textContent = numberFormat(rows.length);
      document.getElementById("totalShips").textContent = totalShips > 0 ? numberFormat(totalShips) : "-";

      if (leader) {
        document.getElementById("leaderName").textContent = leader.operator_name || "-";
        const shareText = leader.market_share ? (decimalFormat(leader.market_share, 2) + "% share") : "";
        document.getElementById("leaderSub").textContent =
          numberFormat(leader.total_teu) + " TEU" + (shareText ? (" · " + shareText) : "");
      }
    }

    function renderDeltaAnalysis(latestRows, prevRows) {
      if (!prevRows || !prevRows.length) {
        document.getElementById("deltaSummaryContainer").innerHTML =
          '<div class="info-box">비교 가능한 이전 스냅샷이 아직 누적되지 않았습니다. (스냅샷이 2회 이상 저장되면 자동으로 활성화됩니다)</div>';
        document.getElementById("deltaTableCard").style.display = "none";
        return;
      }

      document.getElementById("deltaTableCard").style.display = "block";

      const prevMap = new Map();
      prevRows.forEach(r => prevMap.set(r.operator_name, r));

      const curTotalTeu = latestRows.reduce((sum, r) => sum + Number(r.total_teu || 0), 0);
      const prevTotalTeu = prevRows.reduce((sum, r) => sum + Number(r.total_teu || 0), 0);
      const diffTotalTeu = curTotalTeu - prevTotalTeu;

      const curTotalShips = latestRows.reduce((sum, r) => sum + Number(r.total_ships || 0), 0);
      const prevTotalShips = prevRows.reduce((sum, r) => sum + Number(r.total_ships || 0), 0);
      const diffTotalShips = curTotalShips - prevTotalShips;

      // Delta 요약 카드
      const deltaTeuEl = document.getElementById("deltaTeuTotal");
      deltaTeuEl.innerHTML = deltaSpan(diffTotalTeu);

      const deltaShipsEl = document.getElementById("deltaShipsTotal");
      deltaShipsEl.innerHTML = deltaSpan(diffTotalShips);

      const deltaList = [];
      latestRows.forEach(cur => {
        const prev = prevMap.get(cur.operator_name);
        const prevTeu = prev ? Number(prev.total_teu || 0) : 0;
        const curTeu = Number(cur.total_teu || 0);
        const diffTeu = prev ? (curTeu - prevTeu) : curTeu;

        const prevShare = prev ? Number(prev.market_share || 0) : 0;
        const curShare = Number(cur.market_share || 0);
        const diffShare = prev ? (curShare - prevShare) : curShare;

        const prevRank = prev ? Number(prev.rank) : null;
        const rankDiff = prevRank ? (prevRank - Number(cur.rank)) : 0; // 양수면 순위 상승

        deltaList.push({
          rank: cur.rank,
          operator_name: cur.operator_name,
          curTeu: curTeu,
          prevTeu: prevTeu,
          diffTeu: diffTeu,
          curShare: curShare,
          prevShare: prevShare,
          diffShare: diffShare,
          rankDiff: rankDiff,
          hasPrev: !!prev
        });
      });

      // 최대 증가 / 감소 선사
      const sortedByGain = [...deltaList].filter(d => d.hasPrev).sort((a, b) => b.diffTeu - a.diffTeu);
      if (sortedByGain.length) {
        const topGain = sortedByGain[0];
        const topLoss = sortedByGain[sortedByGain.length - 1];

        document.getElementById("topGainName").textContent = topGain.operator_name;
        document.getElementById("topGainVal").innerHTML = deltaSpan(topGain.diffTeu) + " TEU";

        document.getElementById("topLossName").textContent = topLoss.operator_name;
        document.getElementById("topLossVal").innerHTML = deltaSpan(topLoss.diffTeu) + " TEU";
      }

      // Delta 테이블 렌더링
      const prevDate = prevRows[0]?.data_date || "-";
      document.getElementById("deltaTableNote").textContent =
        `기준일: ${latestRows[0]?.data_date || "-"} vs 직전 기준일: ${prevDate}`;

      const tbody = document.getElementById("deltaTableBody");
      tbody.innerHTML = deltaList.map(item => {
        return `
          <tr>
            <td class="rank">${escapeHtml(item.rank)}</td>
            <td>${rankMoveBadge(item.rankDiff)}</td>
            <td><strong>${escapeHtml(item.operator_name)}</strong></td>
            <td class="number">${numberFormat(item.curTeu)}</td>
            <td class="number">${item.hasPrev ? numberFormat(item.prevTeu) : "-"}</td>
            <td class="number">${item.hasPrev ? deltaSpan(item.diffTeu) : '<span class="badge-rank-up">NEW</span>'}</td>
            <td class="number">${decimalFormat(item.curShare, 2)}%</td>
            <td class="number">${item.hasPrev ? decimalFormat(item.prevShare, 2) + "%" : "-"}</td>
            <td class="number">${item.hasPrev ? deltaSpan(item.diffShare, true) : "-"}</td>
          </tr>
        `;
      }).join("");
    }

    function renderTopOperatorsChart(rows) {
      const top10 = rows.slice(0, 10);
      if (topOperatorsChart) topOperatorsChart.destroy();

      topOperatorsChart = new Chart(
        document.getElementById("topOperatorsChart"),
        {
          type: "bar",
          data: {
            labels: top10.map(row => row.operator_name),
            datasets: [{
              label: "Total TEU",
              data: top10.map(row => Number(row.total_teu || 0)),
              backgroundColor: "#1677c8",
              borderRadius: 5
            }]
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            indexAxis: "y",
            plugins: {
              legend: { display: false },
              tooltip: {
                callbacks: {
                  label: context => numberFormat(context.raw) + " TEU"
                }
              }
            },
            scales: {
              x: {
                ticks: {
                  callback: value => (Number(value) / 1000000).toFixed(1) + "M"
                },
                grid: { color: "#edf2f7" }
              },
              y: { grid: { display: false } }
            }
          }
        }
      );
    }

    function renderHistoryChart(history) {
      if (historyChart) historyChart.destroy();

      const labels = history.map(row => row.data_date || "");
      const values = history.map(row => Number(row.total_teu || 0) / 1000000);

      historyChart = new Chart(
        document.getElementById("historyChart"),
        {
          type: "line",
          data: {
            labels: labels,
            datasets: [{
              label: "Total Capacity",
              data: values,
              borderColor: "#16876e",
              backgroundColor: "rgba(22, 135, 110, 0.15)",
              fill: true,
              tension: 0.3,
              pointRadius: 3,
              pointHoverRadius: 5
            }]
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
              legend: { display: false },
              tooltip: {
                callbacks: {
                  label: context => decimalFormat(context.raw, 2) + " Million TEU"
                }
              }
            },
            scales: {
              y: {
                ticks: { callback: value => value + "M" },
                grid: { color: "#edf2f7" }
              },
              x: { grid: { display: false } }
            }
          }
        }
      );
    }

    function renderTable(rows, latestDate) {
      const tbody = document.getElementById("operatorTableBody");

      tbody.innerHTML = rows.map(row => {
        const marketShare = row.market_share === null || row.market_share === undefined
          ? "-"
          : decimalFormat(row.market_share, 2) + "%";

        const orderbookPct = row.orderbook_pct_existing === null || row.orderbook_pct_existing === undefined
          ? "-"
          : decimalFormat(row.orderbook_pct_existing, 1) + "%";

        return `
          <tr>
            <td class="rank">${escapeHtml(row.rank)}</td>
            <td><strong>${escapeHtml(row.operator_name)}</strong></td>
            <td class="number">${numberFormat(row.total_teu)}</td>
            <td class="number">${row.total_ships === null || row.total_ships === undefined ? "-" : numberFormat(row.total_ships)}</td>
            <td class="number">${marketShare}</td>
            <td class="number">${row.orderbook_teu === null || row.orderbook_teu === undefined ? "-" : numberFormat(row.orderbook_teu)}</td>
            <td class="number">${orderbookPct}</td>
          </tr>
        `;
      }).join("");

      document.getElementById("tableNote").textContent =
        "기준일: " + (latestDate || "-") + " · " + numberFormat(rows.length) + "개 선사";
    }

    async function loadDashboard() {
      try {
        const response = await fetch("./alphaliner_data.json?v=" + Date.now(), { cache: "no-store" });
        if (!response.ok) {
          throw new Error("alphaliner_data.json HTTP " + response.status);
        }

        const data = await response.json();
        const latestRows = Array.isArray(data.latest_detail) ? data.latest_detail : [];
        const prevRows = Array.isArray(data.previous_detail) ? data.previous_detail : [];
        const history = Array.isArray(data.history) ? data.history : [];

        if (!latestRows.length) {
          throw new Error("latest_detail 데이터가 0건입니다.");
        }

        const lastSync = data.last_sync || {};
        const latestDate = latestRows[0]?.data_date || lastSync.data_date || "-";

        renderKpis(latestRows);
        renderDeltaAnalysis(latestRows, prevRows);
        renderTopOperatorsChart(latestRows);
        renderHistoryChart(history);
        renderTable(latestRows, latestDate);

        document.getElementById("generatedAt").textContent = data.generated_at || "-";
        document.getElementById("statusDot").className = "status-dot success";
        document.getElementById("statusText").textContent =
          "정상 로드 · 기준일: " + latestDate + " · 최종 실행 상태: " + (lastSync.status || "N/A");

        document.getElementById("dashboard").style.display = "block";
      } catch (error) {
        console.error(error);
        showError(error.message || String(error));
      }
    }

    loadDashboard();
  </script>
</body>
</html>
"""
    atomic_write_text(HTML_PATH, content)
    log(f"[STATIC] HTML 생성 완료: {HTML_PATH.name}")

# =========================================================
# 7. Local Flask App
# =========================================================
@app.route("/")
def index():
    if not HTML_PATH.exists():
        generate_static_html()
    return Response(HTML_PATH.read_text(encoding="utf-8"), mimetype="text/html; charset=utf-8")

@app.route("/alphaliner_data.json")
def serve_json():
    return send_from_directory(BASE_DIR, JSON_PATH.name, mimetype="application/json")

# =========================================================
# 8. Main Entrypoint
# =========================================================
def run_cron_job(args):
    log("=" * 60)
    log("[CRON] Alphaliner Dashboard 갱신 시작")
    log("=" * 60)

    result = run_alphaliner_sync(driver_path=args.driver_path, headless=not args.show_browser)

    build_static_json()
    generate_static_html()

    if result.get("ok"):
        log("[CRON] Alphaliner Dashboard 갱신 완료")
    else:
        log("[WARN] 최신 Alphaliner 수집 실패. 기존 DB 데이터 기반으로 정적 파일을 빌드했습니다.")

    log("=" * 60)
    return result

def parse_args():
    parser = argparse.ArgumentParser(description="Alphaliner TOP 100 Liner Operators Dashboard")
    parser.add_argument("--cron", action="store_true")
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--driver-path", default=os.getenv("CHROMEDRIVER_PATH", ""))
    parser.add_argument("--show-browser", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    return parser.parse_args()

def main():
    args = parse_args()
    if not args.cron and not args.local:
        args.cron = True

    activate_file_logging()

    connection = get_db_conn()
    connection.close()

    result = run_cron_job(args)

    if args.cron:
        if not result.get("ok") and get_total_snapshot_count() == 0:
            log("[ERROR] Alphaliner 수집 실패 및 기존 Snapshot 데이터 없음")
            sys.exit(1)

        log("[SYSTEM] Alphaliner batch process completed. Process exiting.")
        sys.exit(0)

    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
