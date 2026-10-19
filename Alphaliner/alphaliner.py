# -*- coding: utf-8 -*-
"""
Alphaliner TOP 100 Liner Operators Static Dashboard Crawler (GitHub Optimized)
- Direct ExtJS Store extraction (Top100description)
- Official Global Figures + TOP 100 Capacity Separation
- Historical snapshot comparison & Full Time-travel Delta analysis
- Authentic Alphaliner 2-tier Grouped Table & UTF-8 BOM CSV Export

Output files:
- alphaliner_history.db  <-- DB 파일명 수정 반영
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

# 요청하신 DB 파일명으로 변경
DB_PATH = BASE_DIR / "alphaliner_history.db"
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
        return 0

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if pd.isna(value):
            return 0
        return int(round(float(value)))

    text = str(value).strip()
    if not text or text.lower() in {"-", "n/a", "na", "none", "null", "--"}:
        return 0

    text = text.replace(",", "").replace(" ", "")
    text = re.sub(r"[^0-9.\-]", "", text)

    if not text or text in {"-", ".", "-."}:
        return 0

    try:
        return int(round(float(text)))
    except (TypeError, ValueError):
        return 0

def safe_float(value):
    text = str(value or "").strip()
    if not text or text.lower() in {"-", "n/a", "na", "none", "null", "--"}:
        return 0.0

    is_negative = text.startswith("-") or (text.startswith("(") and text.endswith(")"))
    cleaned = text.replace(",", "")
    cleaned = re.sub(r"[^0-9.]", "", cleaned)

    if not cleaned:
        return 0.0

    try:
        number = float(cleaned)
        return -number if is_negative else number
    except ValueError:
        return 0.0

def parse_alphaliner_date(raw_text):
    """Alphaliner TOP 100 / 19 Oct 2026 -> YYYY-MM-DD 포맷 변환"""
    try:
        match = re.search(r"TOP\s+100\s*/\s*(\d{1,2}\s+[A-Za-z]+\s+\d{4})", raw_text, re.IGNORECASE)
        if match:
            date_str = match.group(1).strip()
            dt = datetime.strptime(date_str, "%d %b %Y")
            return dt.strftime("%Y-%m-%d")
    except Exception:
        pass
    return datetime.now().strftime("%Y-%m-%d")

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
# 4. SQLite Database (alphaliner_history.db)
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
            operator_count INTEGER NOT NULL,
            global_teu INTEGER,
            global_ships INTEGER
        )
        """
    )

    # 컬럼 누락 대비 안전 migration
    try:
        connection.execute("ALTER TABLE alphaliner_master ADD COLUMN global_teu INTEGER")
        connection.execute("ALTER TABLE alphaliner_master ADD COLUMN global_ships INTEGER")
    except Exception:
        pass

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
    data_date = bundle.get("data_date")
    global_teu = bundle.get("global_teu")
    global_ships = bundle.get("global_ships")

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
            INSERT INTO alphaliner_master (captured_at, data_date, data_hash, total_teu, total_ships, operator_count, global_teu, global_ships)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (captured_at, data_date, data_hash, total_teu, total_ships, int(len(dataframe)), global_teu, global_ships),
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
                    safe_int(row.get("total_ships")),
                    safe_float(row.get("market_share")),
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
        log(f"[DB] 신규 Alphaliner Snapshot 저장 완료: id={snapshot_id}, date={data_date}, operators={len(dataframe)}")
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
            SELECT snapshot_id, captured_at, data_date, total_teu, total_ships, operator_count, global_teu, global_ships
            FROM alphaliner_master ORDER BY data_date ASC, snapshot_id ASC
            """,
            connection,
        )
    finally:
        connection.close()

    if dataframe.empty:
        return pd.DataFrame()

    return dataframe.reset_index(drop=True)

def load_all_snapshots_details():
    """DB에 존재하는 '모든 과거 스냅샷' 전체를 로드합니다."""
    connection = get_db_conn()
    try:
        masters = pd.read_sql_query(
            """
            SELECT snapshot_id, data_date, captured_at, total_teu, total_ships, operator_count, global_teu, global_ships
            FROM alphaliner_master
            ORDER BY data_date ASC, snapshot_id ASC
            """,
            connection,
        )

        if masters.empty:
            return {}

        details = pd.read_sql_query(
            """
            SELECT snapshot_id, rank, operator_name, total_teu, total_ships, market_share,
                   existing_owned_teu, existing_owned_ships, existing_chartered_teu, existing_chartered_ships,
                   orderbook_teu, orderbook_ships, orderbook_pct_existing
            FROM alphaliner_detail
            ORDER BY snapshot_id ASC, rank ASC
            """,
            connection,
        )

        snapshots_map = {}
        for _, m in masters.iterrows():
            s_id = int(m["snapshot_id"])
            m_date = str(m["data_date"])
            m_cap = str(m["captured_at"])

            sub_df = details[details["snapshot_id"] == s_id].copy()
            sub_records = dataframe_to_records(sub_df)

            snapshots_map[s_id] = {
                "snapshot_id": s_id,
                "data_date": m_date,
                "captured_at": m_cap,
                "total_teu": int(m["total_teu"] or 0),
                "total_ships": int(m["total_ships"] or 0),
                "global_teu": safe_int(m.get("global_teu")),
                "global_ships": safe_int(m.get("global_ships")),
                "operator_count": int(m["operator_count"] or 0),
                "rows": sub_records
            }

        return snapshots_map
    finally:
        connection.close()

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
        store_id: null,
        rows: [],
        available_stores: []
    };

    try {
        const text = out.page_text;
        const activeShips = text.match(/([0-9,]+)\s+active ships/i);
        const totalTeu = text.match(/([0-9,]+)\s+TEU/i);

        out.summary.global_active_ships = activeShips ? activeShips[1] : null;
        out.summary.global_total_teu = totalTeu ? totalTeu[1] : null;
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
        page_text = payload.get("page_text", "")

        dataframe = parse_alphaliner_table(raw_rows)

        if dataframe.empty:
            stores = ", ".join(payload.get("available_stores", [])[:30])
            raise RuntimeError(f"ExtJS Store 파싱 실패. 확인된 Store: {stores or '없음'}")

        if len(dataframe) < 90:
            raise RuntimeError(f"데이터 불완전: {len(dataframe)}건 수집 (최소 90건 필요)")

        dataframe = dataframe.sort_values("rank").head(100).reset_index(drop=True)
        data_date = parse_alphaliner_date(page_text)

        global_summary = payload.get("summary", {})
        global_teu = safe_int(global_summary.get("global_total_teu"))
        global_ships = safe_int(global_summary.get("global_active_ships"))

        log(f"[FETCH] 수집 성공: Date={data_date}, TOP100={len(dataframe)}, GlobalTEU={global_teu:,}, GlobalShips={global_ships:,}")

        return {
            "df": dataframe,
            "data_date": data_date,
            "global_teu": global_teu,
            "global_ships": global_ships,
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
    """Create data file containing all historical snapshots."""
    history = load_history_df()
    snapshots_map = load_all_snapshots_details()

    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": {
            "name": "Alphaliner TOP 100",
            "url": ALPHALINER_URL,
        },
        "last_sync": get_last_sync_meta(),
        "history": dataframe_to_records(history, date_columns=["data_date", "captured_at"]),
        "snapshots": snapshots_map,
    }

    atomic_write_text(
        JSON_PATH,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
    )
    log(f"[STATIC] JSON 생성 완료: {JSON_PATH.name} (누적 스냅샷 {len(snapshots_map)}개)")

def generate_static_html():
    """Build a standalone static dashboard in index.html with Bloomberg Terminal Style (Content 100% Preserved)."""
    content = r"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Alphaliner TOP 100 Liner Operators</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
  <style>
    :root {
      --bg-dark: #0a0e17;
      --bg-panel: #111827;
      --bg-panel-alt: #162032;
      --bg-hover: #1e293b;
      --amber: #f59e0b;
      --amber-bright: #fbbf24;
      --cyan: #06b6d4;
      --cyan-bright: #22d3ee;
      --green: #10b981;
      --red: #f43f5e;
      --blue: #3b82f6;
      --text: #e2e8f0;
      --text-muted: #94a3b8;
      --text-dim: #64748b;
      --border: #1e293b;
      --border-dark: #334155;
      --mono: "Roboto Mono", "Consolas", "Courier New", monospace;
      --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans KR", Arial, sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg-dark);
      color: var(--text);
      font-family: var(--sans);
      font-size: 13px;
      line-height: 1.45;
    }

    /* Header (Bloomberg Terminal Style) */
    .header {
      background: linear-gradient(180deg, #111827 0%, #0c121d 100%);
      color: #ffffff;
      padding: 18px 24px;
      border-bottom: 2px solid var(--amber);
      box-shadow: 0 4px 16px rgba(0, 0, 0, 0.4);
    }
    .header-inner {
      max-width: 1540px;
      margin: 0 auto;
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 16px;
    }
    .header h1 {
      margin: 0;
      font-size: 22px;
      font-weight: 800;
      letter-spacing: -0.3px;
      color: #ffffff;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .header h1::before {
      content: "■";
      color: var(--amber);
      font-size: 14px;
    }
    .header p {
      margin: 4px 0 0;
      color: var(--text-muted);
      font-size: 12px;
      font-family: var(--mono);
    }

    .header-controls {
      display: flex;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
    }

    /* Date Selection Box */
    .selector-box {
      display: flex;
      align-items: center;
      background: #000000;
      border: 1px solid var(--border-dark);
      border-radius: 4px;
      padding: 4px 10px;
      gap: 8px;
    }
    .selector-box label {
      font-size: 11px;
      font-weight: 700;
      color: var(--amber);
      font-family: var(--mono);
      text-transform: uppercase;
      letter-spacing: 0.5px;
      white-space: nowrap;
    }
    .selector-box select {
      background: #111827;
      color: var(--cyan-bright);
      font-family: var(--mono);
      font-size: 12px;
      font-weight: 600;
      border: 1px solid var(--border);
      border-radius: 3px;
      padding: 5px 10px;
      outline: none;
      cursor: pointer;
    }
    .selector-box select:focus {
      border-color: var(--amber);
    }

    /* Action Button (CSV) */
    .btn-csv {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: var(--amber);
      color: #000000;
      font-family: var(--mono);
      font-size: 12px;
      font-weight: 800;
      padding: 7px 14px;
      border-radius: 4px;
      border: none;
      cursor: pointer;
      box-shadow: 0 2px 6px rgba(0, 0, 0, 0.3);
      transition: all 0.15s ease;
      text-decoration: none;
      white-space: nowrap;
    }
    .btn-csv:hover {
      background: var(--amber-bright);
      box-shadow: 0 0 12px rgba(245, 158, 11, 0.4);
      transform: translateY(-1px);
    }
    .btn-csv svg { width: 14px; height: 14px; fill: currentColor; }

    /* Layout & Cards */
    .container { max-width: 1540px; margin: 0 auto; padding: 20px 24px; }
    .status {
      display: flex;
      gap: 8px;
      align-items: center;
      margin-bottom: 16px;
      color: var(--text-dim);
      font-family: var(--mono);
      font-size: 12px;
    }
    .status-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--text-dim); }
    .status-dot.success { background: var(--green); box-shadow: 0 0 8px var(--green); }
    .status-dot.error { background: var(--red); box-shadow: 0 0 8px var(--red); }

    .section-title {
      font-size: 14px;
      font-weight: 700;
      color: var(--amber);
      font-family: var(--mono);
      margin: 24px 0 12px 0;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .section-title::before {
      content: "//";
      color: var(--text-dim);
      font-weight: 800;
    }

    .kpi-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }
    .card {
      background: var(--bg-panel);
      border: 1px solid var(--border);
      border-radius: 4px;
      padding: 14px 16px;
      position: relative;
      overflow: hidden;
    }
    .card::before {
      content: "";
      position: absolute;
      top: 0;
      left: 0;
      width: 100%;
      height: 2px;
      background: var(--border-dark);
    }
    .kpi-label {
      color: var(--text-muted);
      font-size: 11.5px;
      font-family: var(--mono);
      font-weight: 600;
      margin-bottom: 6px;
    }
    .kpi-value {
      color: #ffffff;
      font-size: 21px;
      font-weight: 800;
      font-family: var(--mono);
    }
    .kpi-sub {
      margin-top: 4px;
      color: var(--cyan);
      font-size: 11px;
      font-family: var(--mono);
    }

    /* Charts */
    .chart-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
      margin-bottom: 20px;
    }
    .chart-card { min-height: 360px; }
    .chart-title {
      margin: 0 0 2px;
      color: #ffffff;
      font-size: 14px;
      font-weight: 700;
      font-family: var(--mono);
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .chart-title::before {
      content: "■";
      color: var(--amber);
      font-size: 10px;
    }
    .chart-note { margin: 0 0 12px; color: var(--text-dim); font-size: 11px; font-family: var(--mono); }
    .chart-wrap { position: relative; height: 270px; }

    /* Tables (Alphaliner 2-tier Bloomberg Grid) */
    .table-card { overflow: hidden; padding: 0; margin-bottom: 24px; border: 1px solid var(--border); }
    .table-heading {
      padding: 12px 16px;
      background: #0d131f;
      border-bottom: 1px solid var(--border);
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px;
    }
    .table-heading h2 {
      margin: 0;
      color: #ffffff;
      font-size: 14px;
      font-weight: 700;
      font-family: var(--mono);
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .table-heading h2::before {
      content: "■";
      color: var(--amber);
      font-size: 10px;
    }
    .table-heading p { margin: 0; color: var(--text-dim); font-size: 11px; font-family: var(--mono); }
    .table-scroll { overflow-x: auto; max-height: 640px; overflow-y: auto; }

    table.alpha-table {
      width: 100%;
      border-collapse: separate;
      border-spacing: 0;
      min-width: 1200px;
      font-size: 12px;
      background: var(--bg-panel);
    }
    table.alpha-table thead { position: sticky; top: 0; z-index: 3; }

    /* 2-tier Group Headers */
    table.alpha-table thead tr.group-header th {
      color: #ffffff;
      padding: 6px 10px;
      text-align: center;
      font-weight: 800;
      font-size: 11px;
      font-family: var(--mono);
      letter-spacing: 0.5px;
      border-right: 1px solid rgba(255, 255, 255, 0.08);
      border-bottom: 1px solid rgba(255, 255, 255, 0.1);
    }
    table.alpha-table thead tr.group-header th.group-main { background: #1e293b; color: var(--amber); }
    table.alpha-table thead tr.group-header th.group-total { background: #0f2c4a; color: var(--cyan-bright); }
    table.alpha-table thead tr.group-header th.group-owned { background: #132a3e; }
    table.alpha-table thead tr.group-header th.group-chartered { background: #1a2d42; }
    table.alpha-table thead tr.group-header th.group-orderbook { background: #242c38; color: #fbbf24; }

    table.alpha-table thead tr.col-header th {
      background: #0b111c;
      color: var(--text-muted);
      padding: 7px 10px;
      text-align: left;
      font-weight: 700;
      font-size: 11px;
      font-family: var(--mono);
      border-bottom: 1px solid var(--border-dark);
      border-right: 1px solid var(--border);
      white-space: nowrap;
    }
    table.alpha-table td {
      padding: 6px 10px;
      border-bottom: 1px solid #141e2e;
      border-right: 1px solid #141e2e;
      white-space: nowrap;
      font-family: var(--mono);
    }
    table.alpha-table tbody tr:nth-child(even) { background: #0e1522; }
    table.alpha-table tbody tr:hover { background: var(--bg-hover); }

    .number { text-align: right; }
    .rank-cell { color: var(--amber); font-weight: 800; text-align: center; width: 55px; }
    .operator-cell { font-weight: 700; color: #ffffff; font-family: var(--sans); }
    .sub-col { color: var(--text-dim); }

    /* Delta Indicators */
    .delta-pos { color: var(--red); font-weight: 700; }
    .delta-neg { color: var(--cyan); font-weight: 700; }
    .delta-zero { color: var(--text-dim); font-weight: 600; }
    .badge-rank-up {
      display: inline-block;
      padding: 1px 5px;
      border-radius: 2px;
      background: rgba(244, 63, 94, 0.15);
      border: 1px solid var(--red);
      color: var(--red);
      font-size: 10px;
      font-weight: 700;
      font-family: var(--mono);
    }
    .badge-rank-down {
      display: inline-block;
      padding: 1px 5px;
      border-radius: 2px;
      background: rgba(6, 182, 212, 0.15);
      border: 1px solid var(--cyan);
      color: var(--cyan);
      font-size: 10px;
      font-weight: 700;
      font-family: var(--mono);
    }

    .error-message, .info-box {
      padding: 14px 16px;
      border-radius: 4px;
      background: var(--bg-panel);
      border: 1px solid var(--border);
      font-size: 12px;
      font-family: var(--mono);
    }
    .error-message { border-color: var(--red); color: var(--red); background: rgba(244, 63, 94, 0.05); }
    .info-box { background: #0d131f; border-color: var(--border); margin-bottom: 16px; color: var(--text-muted); }
    .footer {
      padding: 16px 0 10px;
      color: var(--text-dim);
      font-size: 11px;
      font-family: var(--mono);
      text-align: right;
      border-top: 1px solid var(--border);
      margin-top: 20px;
    }
    .footer a { color: var(--amber); text-decoration: none; }
    .footer a:hover { text-decoration: underline; }

    @media (max-width: 1024px) {
      .kpi-grid, .chart-grid { grid-template-columns: 1fr 1fr; }
    }
    @media (max-width: 640px) {
      .container { padding: 12px; }
      .header h1 { font-size: 18px; }
      .kpi-grid, .chart-grid { grid-template-columns: 1fr; }
      .header-inner { flex-direction: column; align-items: flex-start; }
      .header-controls { width: 100%; justify-content: space-between; }
    }
  </style>
</head>
<body>
  <header class="header">
    <div class="header-inner">
      <div>
        <h1>Alphaliner TOP 100 Liner Operators</h1>
        <p>Global container liner operator capacity, fleet, orderbook & snapshot delta monitoring</p>
      </div>
      <div class="header-controls">
        <div class="selector-box">
          <label for="snapshotSelect">Comparison Base Date</label>
          <select id="snapshotSelect">
            <option value="">Loading historical dates...</option>
          </select>
        </div>
        <button id="btnExportCsv" class="btn-csv" title="TOP 100 선사 데이터 CSV 다운로드">
          <svg viewBox="0 0 24 24"><path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/></svg>
          Export CSV
        </button>
      </div>
    </div>
  </header>

  <main class="container">
    <div class="status">
      <span id="statusDot" class="status-dot"></span>
      <span id="statusText">Alphaliner 데이터를 불러오는 중입니다.</span>
    </div>

    <section id="dashboard" style="display:none;">
      <!-- Highlights KPI (전 세계 공식 집계 & TOP 100 분리) -->
      <div class="kpi-grid">
        <article class="card">
          <div class="kpi-label">Global Total TEU (전 세계 선복량)</div>
          <div id="globalTeu" class="kpi-value">-</div>
          <div id="top100TeuSub" class="kpi-sub">TOP 100: -</div>
        </article>
        <article class="card">
          <div class="kpi-label">Global Active Ships (운항 선박 수)</div>
          <div id="globalShips" class="kpi-value">-</div>
          <div id="top100ShipsSub" class="kpi-sub">TOP 100: -</div>
        </article>
        <article class="card">
          <div class="kpi-label">Market Leader (1위 선사)</div>
          <div id="leaderName" class="kpi-value" style="color:var(--amber);">-</div>
          <div id="leaderSub" class="kpi-sub">-</div>
        </article>
        <article class="card">
          <div class="kpi-label">Market Top 10 Share (상위 10사 점유율)</div>
          <div id="top10Share" class="kpi-value" style="color:var(--cyan-bright);">-</div>
          <div id="top10TeuSub" class="kpi-sub">-</div>
        </article>
      </div>

      <!-- Delta Comparison Summary -->
      <div class="section-title">선택 시점 대비 최신 변동 요약 (Snapshot Delta Summary)</div>
      <div id="deltaSummaryContainer">
        <div class="kpi-grid">
          <article class="card">
            <div class="kpi-label">TOP 100 선복량 증감 (Δ TEU)</div>
            <div id="deltaTeuTotal" class="kpi-value">-</div>
            <div id="deltaTeuSub" class="kpi-sub">vs Selected Date</div>
          </article>
          <article class="card">
            <div class="kpi-label">TOP 100 선박 수 증감 (Δ Ships)</div>
            <div id="deltaShipsTotal" class="kpi-value">-</div>
            <div id="deltaShipsSub" class="kpi-sub">vs Selected Date</div>
          </article>
          <article class="card">
            <div class="kpi-label">최대 선복량 증가 선사 (Top Gainer)</div>
            <div id="topGainName" class="kpi-value">-</div>
            <div id="topGainVal" class="kpi-sub">-</div>
          </article>
          <article class="card">
            <div class="kpi-label">최대 선복량 감소 선사 (Top Loser)</div>
            <div id="topLossName" class="kpi-value">-</div>
            <div id="topLossVal" class="kpi-sub">-</div>
          </article>
        </div>
      </div>

      <!-- Charts -->
      <div class="chart-grid">
        <section class="card chart-card">
          <h2 class="chart-title">상위 10개 선사 선복량 (Top 10 Capacity)</h2>
          <p class="chart-note">Latest snapshot · 단위: TEU</p>
          <div class="chart-wrap">
            <canvas id="topOperatorsChart"></canvas>
          </div>
        </section>

        <section class="card chart-card">
          <h2 class="chart-title">전 세계 및 TOP 100 총 선복량 추이 (Historical Capacity)</h2>
          <p class="chart-note">All historical snapshots · 단위: Million TEU</p>
          <div class="chart-wrap">
            <canvas id="historyChart"></canvas>
          </div>
        </section>
      </div>

      <!-- Delta Comparison Table -->
      <section class="card table-card" id="deltaTableCard">
        <div class="table-heading">
          <div>
            <h2>선택 시점 대비 선사별 상세 변동 (Delta Detail)</h2>
            <p id="deltaTableNote">비교 대상 스냅샷 데이터를 불러오는 중...</p>
          </div>
        </div>
        <div class="table-scroll">
          <table class="alpha-table">
            <thead>
              <tr class="col-header">
                <th style="text-align:center;">Rank</th>
                <th>순위 변동</th>
                <th>Operator</th>
                <th class="number">최신 TEU</th>
                <th class="number">선택 시점 TEU</th>
                <th class="number">증감 (Δ TEU)</th>
                <th class="number">최신 점유율</th>
                <th class="number">선택 시점 점유율</th>
                <th class="number">점유율 증감 (Δ %p)</th>
              </tr>
            </thead>
            <tbody id="deltaTableBody"></tbody>
          </table>
        </div>
      </section>

      <!-- Detailed Authentic Alphaliner 2-tier Group Table -->
      <section class="card table-card">
        <div class="table-heading">
          <div>
            <h2>TOP 100 선사별 전체 현황 (Alphaliner Official View)</h2>
            <p id="tableNote">-</p>
          </div>
        </div>
        <div class="table-scroll">
          <table class="alpha-table">
            <thead>
              <tr class="group-header">
                <th colspan="2" class="group-main">OPERATOR</th>
                <th colspan="3" class="group-total">TOTAL</th>
                <th colspan="3" class="group-owned">EXISTING (OWNED)</th>
                <th colspan="3" class="group-chartered">EXISTING (CHARTERED)</th>
                <th colspan="3" class="group-orderbook">ORDERBOOK</th>
              </tr>
              <tr class="col-header">
                <th style="text-align:center; width:50px;">Rank</th>
                <th>Operator Name</th>
                <th class="number">TEU</th>
                <th class="number">Ships</th>
                <th class="number">Share</th>
                <th class="number">TEU</th>
                <th class="number">Ships</th>
                <th class="number">% Own</th>
                <th class="number">TEU</th>
                <th class="number">Ships</th>
                <th class="number">% Char</th>
                <th class="number">TEU</th>
                <th class="number">Ships</th>
                <th class="number">% Exist</th>
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
      · Generated: <span id="generatedAt" style="color:var(--amber);">-</span>
    </div>
  </main>

  <script>
    let topOperatorsChart = null;
    let historyChart = null;
    let allSnapshots = {};
    let snapshotList = [];
    let latestSnapshotId = null;

    function numberFormat(value) {
      if (value === null || value === undefined || isNaN(value)) return "-";
      return new Intl.NumberFormat("en-US").format(Number(value));
    }

    function decimalFormat(value, digits = 2) {
      if (value === null || value === undefined || isNaN(value)) return "-";
      return Number(value).toLocaleString("en-US", {
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
        '<br><br>저장소의 <code>alphaliner_history.db</code> 및 <code>alphaliner_data.json</code> 파일 상태를 확인해 주세요.</div>';
    }

    function renderKpis(snapshot) {
      const rows = snapshot.rows || [];
      const top100Teu = rows.reduce((sum, row) => sum + Number(row.total_teu || 0), 0);
      const top100Ships = rows.reduce((sum, row) => sum + Number(row.total_ships || 0), 0);

      const globalTeu = snapshot.global_teu || top100Teu;
      const globalShips = snapshot.global_ships || top100Ships;

      document.getElementById("globalTeu").textContent = numberFormat(globalTeu) + " TEU";
      document.getElementById("top100TeuSub").textContent = `TOP 100 합계: ${numberFormat(top100Teu)} TEU`;

      document.getElementById("globalShips").textContent = numberFormat(globalShips) + " 척";
      document.getElementById("top100ShipsSub").textContent = `TOP 100 선박: ${numberFormat(top100Ships)} 척`;

      const leader = rows.length ? rows[0] : null;
      if (leader) {
        document.getElementById("leaderName").textContent = leader.operator_name || "-";
        const shareText = leader.market_share ? (decimalFormat(leader.market_share, 2) + "% share") : "";
        document.getElementById("leaderSub").textContent =
          numberFormat(leader.total_teu) + " TEU" + (shareText ? (" · " + shareText) : "");
      }

      const top10Teu = rows.slice(0, 10).reduce((sum, r) => sum + Number(r.total_teu || 0), 0);
      const top10Share = globalTeu > 0 ? ((top10Teu / globalTeu) * 100) : 0;
      document.getElementById("top10Share").textContent = decimalFormat(top10Share, 2) + "%";
      document.getElementById("top10TeuSub").textContent = `Top 10 합계: ${numberFormat(top10Teu)} TEU`;
    }

    function renderDeltaAnalysis(latestSnapshot, compareSnapshot) {
      const latestRows = latestSnapshot.rows || [];
      const prevRows = compareSnapshot ? (compareSnapshot.rows || []) : [];

      if (!prevRows.length || latestSnapshot.snapshot_id === compareSnapshot.snapshot_id) {
        document.getElementById("deltaSummaryContainer").innerHTML =
          '<div class="info-box">동일한 시점이 선택되었거나 비교 대상 데이터가 없습니다. 상단 드롭다운에서 다른 과거 날짜를 선택해 보세요.</div>';
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

      document.getElementById("deltaSummaryContainer").innerHTML = `
        <div class="kpi-grid">
          <article class="card">
            <div class="kpi-label">TOP 100 선복량 증감 (Δ TEU)</div>
            <div class="kpi-value">${deltaSpan(diffTotalTeu)}</div>
            <div class="kpi-sub">vs ${compareSnapshot.data_date}</div>
          </article>
          <article class="card">
            <div class="kpi-label">TOP 100 선박 수 증감 (Δ Ships)</div>
            <div class="kpi-value">${deltaSpan(diffTotalShips)}</div>
            <div class="kpi-sub">vs ${compareSnapshot.data_date}</div>
          </article>
          <article class="card">
            <div class="kpi-label">최대 선복량 증가 선사</div>
            <div class="kpi-value" id="topGainName">-</div>
            <div class="kpi-sub" id="topGainVal">-</div>
          </article>
          <article class="card">
            <div class="kpi-label">최대 선복량 감소 선사</div>
            <div id="topLossName" class="kpi-value">-</div>
            <div id="topLossVal" class="kpi-sub">-</div>
          </article>
        </div>
      `;

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
        const rankDiff = prevRank ? (prevRank - Number(cur.rank)) : 0;

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

      const sortedByGain = [...deltaList].filter(d => d.hasPrev).sort((a, b) => b.diffTeu - a.diffTeu);
      if (sortedByGain.length) {
        const topGain = sortedByGain[0];
        const topLoss = sortedByGain[sortedByGain.length - 1];

        document.getElementById("topGainName").textContent = topGain.operator_name;
        document.getElementById("topGainVal").innerHTML = deltaSpan(topGain.diffTeu) + " TEU";

        document.getElementById("topLossName").textContent = topLoss.operator_name;
        document.getElementById("topLossVal").innerHTML = deltaSpan(topLoss.diffTeu) + " TEU";
      }

      document.getElementById("deltaTableNote").textContent =
        `기준일: 최신 (${latestSnapshot.data_date}) vs 선택 기준일 (${compareSnapshot.data_date})`;

      const tbody = document.getElementById("deltaTableBody");
      tbody.innerHTML = deltaList.map(item => `
        <tr>
          <td class="rank-cell">${escapeHtml(item.rank)}</td>
          <td>${rankMoveBadge(item.rankDiff)}</td>
          <td class="operator-cell">${escapeHtml(item.operator_name)}</td>
          <td class="number">${numberFormat(item.curTeu)}</td>
          <td class="number">${item.hasPrev ? numberFormat(item.prevTeu) : "-"}</td>
          <td class="number">${item.hasPrev ? deltaSpan(item.diffTeu) : '<span class="badge-rank-up">NEW</span>'}</td>
          <td class="number">${decimalFormat(item.curShare, 2)}%</td>
          <td class="number">${item.hasPrev ? decimalFormat(item.prevShare, 2) + "%" : "-"}</td>
          <td class="number">${item.hasPrev ? deltaSpan(item.diffShare, true) : "-"}</td>
        </tr>
      `).join("");
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
              backgroundColor: "rgba(245, 158, 11, 0.85)",
              hoverBackgroundColor: "#fbbf24",
              borderColor: "#f59e0b",
              borderWidth: 1,
              borderRadius: 2
            }]
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            indexAxis: "y",
            plugins: {
              legend: { display: false },
              tooltip: {
                backgroundColor: "#000000",
                titleColor: "#f59e0b",
                bodyColor: "#ffffff",
                borderColor: "#334155",
                borderWidth: 1,
                callbacks: {
                  label: context => numberFormat(context.raw) + " TEU"
                }
              }
            },
            scales: {
              x: {
                ticks: {
                  color: "#94a3b8",
                  font: { family: "'Roboto Mono', monospace", size: 10 },
                  callback: value => (Number(value) / 1000000).toFixed(1) + "M"
                },
                grid: { color: "rgba(255, 255, 255, 0.05)" }
              },
              y: {
                ticks: {
                  color: "#e2e8f0",
                  font: { family: "'Roboto Mono', monospace", size: 11, weight: "bold" }
                },
                grid: { display: false }
              }
            }
          }
        }
      );
    }

    function renderHistoryChart(history) {
      if (historyChart) historyChart.destroy();

      const labels = history.map(row => row.data_date || "");
      const globalValues = history.map(row => Number(row.global_teu || row.total_teu || 0) / 1000000);
      const top100Values = history.map(row => Number(row.total_teu || 0) / 1000000);

      historyChart = new Chart(
        document.getElementById("historyChart"),
        {
          type: "line",
          data: {
            labels: labels,
            datasets: [
              {
                label: "Global Total",
                data: globalValues,
                borderColor: "#10b981",
                backgroundColor: "rgba(16, 185, 129, 0.08)",
                fill: true,
                tension: 0.2,
                pointRadius: 3,
                pointBackgroundColor: "#10b981"
              },
              {
                label: "TOP 100 Total",
                data: top100Values,
                borderColor: "#06b6d4",
                backgroundColor: "transparent",
                borderDash: [3, 3],
                tension: 0.2,
                pointRadius: 2,
                pointBackgroundColor: "#06b6d4"
              }
            ]
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
              legend: {
                display: true,
                position: "top",
                labels: {
                  color: "#94a3b8",
                  font: { family: "'Roboto Mono', monospace", size: 11 }
                }
              },
              tooltip: {
                backgroundColor: "#000000",
                titleColor: "#06b6d4",
                bodyColor: "#ffffff",
                borderColor: "#334155",
                borderWidth: 1,
                callbacks: {
                  label: context => `${context.dataset.label}: ${decimalFormat(context.raw, 2)}M TEU`
                }
              }
            },
            scales: {
              y: {
                ticks: {
                  color: "#94a3b8",
                  font: { family: "'Roboto Mono', monospace", size: 10 },
                  callback: value => value + "M"
                },
                grid: { color: "rgba(255, 255, 255, 0.05)" }
              },
              x: {
                ticks: {
                  color: "#94a3b8",
                  font: { family: "'Roboto Mono', monospace", size: 10 }
                },
                grid: { display: false }
              }
            }
          }
        }
      );
    }

    function renderAuthenticTable(rows, snapshotDate) {
      const tbody = document.getElementById("operatorTableBody");

      tbody.innerHTML = rows.map(row => {
        const totalTeu = Number(row.total_teu || 0);
        const ownedTeu = Number(row.existing_owned_teu || 0);
        const chartTeu = Number(row.existing_chartered_teu || 0);

        const ownedPct = totalTeu > 0 && row.existing_owned_teu !== null
          ? ((ownedTeu / totalTeu) * 100).toFixed(1) + "%"
          : "-";

        const chartPct = totalTeu > 0 && row.existing_chartered_teu !== null
          ? ((chartTeu / totalTeu) * 100).toFixed(1) + "%"
          : "-";

        const shareStr = row.market_share !== null && row.market_share !== undefined
          ? decimalFormat(row.market_share, 2) + "%"
          : "-";

        const orderbookPct = row.orderbook_pct_existing !== null && row.orderbook_pct_existing !== undefined
          ? decimalFormat(row.orderbook_pct_existing, 1) + "%"
          : "-";

        return `
          <tr>
            <td class="rank-cell">${escapeHtml(row.rank)}</td>
            <td class="operator-cell">${escapeHtml(row.operator_name)}</td>

            <!-- TOTAL -->
            <td class="number font-bold" style="color:var(--cyan-bright);">${numberFormat(row.total_teu)}</td>
            <td class="number">${numberFormat(row.total_ships)}</td>
            <td class="number" style="color:var(--cyan);">${shareStr}</td>

            <!-- EXISTING OWNED -->
            <td class="number">${numberFormat(row.existing_owned_teu)}</td>
            <td class="number sub-col">${numberFormat(row.existing_owned_ships)}</td>
            <td class="number sub-col">${ownedPct}</td>

            <!-- EXISTING CHARTERED -->
            <td class="number">${numberFormat(row.existing_chartered_teu)}</td>
            <td class="number sub-col">${numberFormat(row.existing_chartered_ships)}</td>
            <td class="number sub-col">${chartPct}</td>

            <!-- ORDERBOOK -->
            <td class="number" style="color:var(--amber);">${numberFormat(row.orderbook_teu)}</td>
            <td class="number sub-col">${numberFormat(row.orderbook_ships)}</td>
            <td class="number sub-col">${orderbookPct}</td>
          </tr>
        `;
      }).join("");

      document.getElementById("tableNote").textContent =
        "기준일: " + (snapshotDate || "-") + " · 총 " + numberFormat(rows.length) + "개 선사 전체 현황";
    }

    function exportToCsv() {
      const select = document.getElementById("snapshotSelect");
      const targetId = select.value ? parseInt(select.value) : latestSnapshotId;
      const targetSnapshot = allSnapshots[targetId];

      if (!targetSnapshot || !targetSnapshot.rows || !targetSnapshot.rows.length) {
        alert("내보낼 데이터가 없습니다.");
        return;
      }

      const headers = [
        "Rank", "Operator", "Total_TEU", "Total_Ships", "Market_Share_Pct",
        "Owned_TEU", "Owned_Ships", "Owned_Pct",
        "Chartered_TEU", "Chartered_Ships", "Chartered_Pct",
        "Orderbook_TEU", "Orderbook_Ships", "Orderbook_Pct_Existing"
      ];

      const csvRows = [headers.join(",")];

      targetSnapshot.rows.forEach(row => {
        const totalTeu = Number(row.total_teu || 0);
        const ownedTeu = Number(row.existing_owned_teu || 0);
        const chartTeu = Number(row.existing_chartered_teu || 0);

        const ownedPct = totalTeu > 0 && row.existing_owned_teu !== null
          ? ((ownedTeu / totalTeu) * 100).toFixed(2)
          : "";
        const chartPct = totalTeu > 0 && row.existing_chartered_teu !== null
          ? ((chartTeu / totalTeu) * 100).toFixed(2)
          : "";

        const opName = `"${String(row.operator_name || '').replace(/"/g, '""')}"`;

        const values = [
          row.rank ?? "",
          opName,
          row.total_teu ?? "",
          row.total_ships ?? "",
          row.market_share ?? "",
          row.existing_owned_teu ?? "",
          row.existing_owned_ships ?? "",
          ownedPct,
          row.existing_chartered_teu ?? "",
          row.existing_chartered_ships ?? "",
          chartPct,
          row.orderbook_teu ?? "",
          row.orderbook_ships ?? "",
          row.orderbook_pct_existing ?? ""
        ];

        csvRows.push(values.join(","));
      });

      const csvString = "\uFEFF" + csvRows.join("\r\n");
      const blob = new Blob([csvString], { type: "text/csv;charset=utf-8;" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      const filename = `Alphaliner_TOP100_${targetSnapshot.data_date || "latest"}.csv`;

      link.setAttribute("href", url);
      link.setAttribute("download", filename);
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);
    }

    function handleSnapshotChange() {
      const select = document.getElementById("snapshotSelect");
      const compareId = select.value ? parseInt(select.value) : (snapshotList.length > 1 ? snapshotList[snapshotList.length - 2].snapshot_id : latestSnapshotId);

      const latestSnapshot = allSnapshots[latestSnapshotId];
      const compareSnapshot = allSnapshots[compareId];

      if (!latestSnapshot) return;

      renderKpis(latestSnapshot);
      renderDeltaAnalysis(latestSnapshot, compareSnapshot);
      renderTopOperatorsChart(latestSnapshot.rows);
      renderAuthenticTable(latestSnapshot.rows, latestSnapshot.data_date);
    }

    async function loadDashboard() {
      try {
        const response = await fetch("./alphaliner_data.json?v=" + Date.now(), { cache: "no-store" });
        if (!response.ok) {
          throw new Error("alphaliner_data.json HTTP " + response.status);
        }

        const data = await response.json();
        allSnapshots = data.snapshots || {};
        const history = Array.isArray(data.history) ? data.history : [];

        // snapshot_id 및 data_date 기준 정렬
        snapshotList = Object.values(allSnapshots).sort((a, b) => a.snapshot_id - b.snapshot_id);

        if (!snapshotList.length) {
          throw new Error("저장된 스냅샷 데이터가 없습니다.");
        }

        const latestSnapshot = snapshotList[snapshotList.length - 1];
        latestSnapshotId = latestSnapshot.snapshot_id;

        // 드롭다운 옵션 채우기 (최신부터 가장 과거 순으로 전체 배치)
        const select = document.getElementById("snapshotSelect");
        select.innerHTML = "";

        for (let i = snapshotList.length - 1; i >= 0; i--) {
          const s = snapshotList[i];
          const opt = document.createElement("option");
          opt.value = s.snapshot_id;
          if (i === snapshotList.length - 1) {
            opt.textContent = `Latest (${s.data_date})`;
          } else if (i === snapshotList.length - 2) {
            opt.textContent = `Previous (${s.data_date})`;
            opt.selected = true; // 기본값으로 직전 스냅샷 선택
          } else {
            opt.textContent = `${s.data_date} (${s.captured_at ? s.captured_at.slice(0, 10) : ''})`;
          }
          select.appendChild(opt);
        }

        select.addEventListener("change", handleSnapshotChange);
        document.getElementById("btnExportCsv").addEventListener("click", exportToCsv);

        renderHistoryChart(history);
        handleSnapshotChange();

        document.getElementById("generatedAt").textContent = data.generated_at || "-";
        document.getElementById("statusDot").className = "status-dot success";
        document.getElementById("statusText").textContent =
          "정상 로드 · 총 " + snapshotList.length + "개 히스토리 스냅샷 (전체 과거 시점 탐색 가능)";

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
