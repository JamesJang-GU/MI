# -*- coding: utf-8 -*-
"""
Alphaliner TOP 100 Liner Operators Static Dashboard Crawler (GitHub Optimized)
- Direct ExtJS Store extraction (Top100description)
- Official Global Figures + TOP 100 Capacity Separation
- Historical snapshot comparison & Full Time-travel Delta analysis
- Authentic Alphaliner 2-tier Grouped Table & UTF-8 BOM CSV Export
- Bloomberg MI Terminal Look & Feel (Aligned with BunkerPrice)

Output files:
- alphaliner_history.db
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
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(text, encoding="utf-8")
    temporary_path.replace(path)

def dataframe_to_records(dataframe, date_columns=None):
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

# =========================================================
# 7. Bloomberg Terminal Static HTML Generator
# =========================================================
def generate_static_html():
    """Build a standalone static dashboard with BunkerPrice Bloomberg Terminal Look & Feel."""
    content = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MI Terminal | Alphaliner TOP 100</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<style>
:root {
  --bg: #02060b;
  --panel: #07111c;
  --panel2: #0b1825;
  --panel3: #0e2131;
  --line: #23394d;
  --text: #dce9f5;
  --muted: #8aa0b5;
  --orange: #ff8a00;
  --blue: #39a8ff;
  --green: #2bd682;
  --red: #ff5c5c;
  --purple: #be7bff;
}
* { box-sizing: border-box; }
html, body { min-height: 100%; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: "Roboto Mono", Consolas, "Noto Sans KR", monospace;
  font-size: 12px;
}
button, input, select { font: inherit; }
a { color: inherit; text-decoration: none; }

.topbar {
  min-height: 54px;
  padding: 0 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 15px;
  border-bottom: 1px solid var(--line);
  background: #050a11;
}
.brand {
  display: flex;
  align-items: center;
  gap: 12px;
}
.brand-mark {
  color: var(--orange);
  font-size: 18px;
  font-weight: 900;
}
.brand-title {
  font-size: 13px;
  color: white;
  letter-spacing: .8px;
  font-weight: 700;
}
.nav-link {
  padding: 7px 11px;
  border: 1px solid #34526e;
  color: var(--blue);
  font-size: 11px;
}
.nav-link:hover {
  color: #fff;
  background: #12344f;
}

.terminal {
  max-width: 1720px;
  margin: 0 auto;
  padding: 18px 20px 32px;
}
.status {
  padding: 9px 12px;
  display: flex;
  justify-content: space-between;
  gap: 12px;
  color: var(--muted);
  border: 1px solid var(--line);
  background: var(--panel);
  font-size: 10px;
}
.workspace {
  margin-top: 14px;
  display: grid;
  grid-template-columns: 260px minmax(0, 1fr);
  gap: 14px;
}
.panel {
  border: 1px solid var(--line);
  background: var(--panel);
}
.panel-header {
  min-height: 39px;
  padding: 0 12px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 9px;
  background: var(--panel2);
  border-bottom: 1px solid var(--line);
}
.panel-title {
  color: var(--orange);
  font-size: 10px;
  letter-spacing: .8px;
  font-weight: 700;
}
.filter-section {
  padding: 13px;
  border-bottom: 1px solid #172a3a;
}
.label {
  display: block;
  color: var(--muted);
  font-size: 10px;
  letter-spacing: .65px;
  margin-bottom: 8px;
  font-weight: 700;
}

.custom-select {
  width: 100%;
  background: #040910;
  border: 1px solid var(--line);
  color: var(--blue);
  padding: 7px 8px;
  font-size: 11px;
  outline: none;
}
.custom-select:focus {
  border-color: var(--orange);
}

.btn {
  min-height: 31px;
  padding: 6px 8px;
  border: 1px solid var(--line);
  background: #07101a;
  color: var(--muted);
  cursor: pointer;
  font-size: 10px;
  font-weight: 700;
  width: 100%;
  display: block;
  text-align: center;
}
.btn:hover {
  border-color: var(--orange);
  color: var(--orange);
}
.btn.active {
  border-color: var(--orange);
  background: var(--orange);
  color: #0a0a0a;
}

.heading {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 12px;
}
h1 {
  color: #fff;
  margin: 0;
  font-size: 20px;
  letter-spacing: .2px;
}
.subtitle {
  color: var(--muted);
  font-size: 10px;
  margin-top: 6px;
}
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 9px;
  margin-bottom: 12px;
}
.kpi {
  min-height: 94px;
  padding: 12px;
  border: 1px solid var(--line);
  background: var(--panel);
}
.kpi-label {
  display: block;
  min-height: 25px;
  color: var(--muted);
  font-size: 9px;
  line-height: 1.3;
}
.kpi-value {
  display: block;
  margin-top: 10px;
  color: #fff;
  font-size: 18px;
  font-weight: 700;
}
.kpi-sub {
  display: block;
  margin-top: 5px;
  color: var(--muted);
  font-size: 9px;
}
.positive { color: var(--green); }
.negative { color: var(--red); }

.chart-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
  margin-bottom: 14px;
}
.chart-wrap {
  position: relative;
  height: 280px;
  padding: 12px;
}

.table-wrap {
  overflow-x: auto;
  padding: 12px;
  max-height: 600px;
}
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 11px;
  min-width: 1100px;
}
th, td {
  padding: 7px 9px;
  text-align: right;
  border-bottom: 1px solid #1c3041;
  border-right: 1px solid #142230;
  white-space: nowrap;
}
th {
  color: var(--muted);
  background: #0a1723;
  font-size: 10px;
  position: sticky;
  top: 0;
  z-index: 2;
}

/* 2-tier Group Headers */
tr.group-header th {
  text-align: center;
  font-weight: 700;
  border-bottom: 1px solid #1c3041;
}
th.group-main { background: #0c1825; color: var(--orange); }
th.group-total { background: #0a2136; color: var(--blue); }
th.group-owned { background: #0a1f2f; color: #a5c7e5; }
th.group-chartered { background: #0f2438; color: #a5c7e5; }
th.group-orderbook { background: #1c2618; color: var(--green); }

.rank-cell { color: var(--orange); font-weight: 700; text-align: center; width: 45px; }
.operator-cell { text-align: left; font-weight: 700; color: #fff; }
.sub-col { color: #6b849c; }

.delta-pos { color: var(--green); font-weight: 700; }
.delta-neg { color: var(--red); font-weight: 700; }
.delta-zero { color: var(--muted); }

.badge-rank-up {
  color: var(--green);
  border: 1px solid #155e3b;
  background: #062416;
  padding: 1px 4px;
  font-size: 9px;
  font-weight: 700;
}
.badge-rank-down {
  color: var(--red);
  border: 1px solid #6b2222;
  background: #240a0a;
  padding: 1px 4px;
  font-size: 9px;
  font-weight: 700;
}

.empty {
  padding: 40px 15px;
  color: var(--muted);
  text-align: center;
}

@media (max-width: 1100px) {
  .workspace { grid-template-columns: 1fr; }
  .kpi-grid { grid-template-columns: repeat(2, 1fr); }
  .chart-grid { grid-template-columns: 1fr; }
}
@media (max-width: 650px) {
  .terminal { padding: 12px; }
  .kpi-grid { grid-template-columns: 1fr; }
  .status { display: block; line-height: 1.8; }
}
</style>
</head>

<body>
<header class="topbar">
  <div class="brand">
    <span class="brand-mark">MI&lt;GO&gt;</span>
    <span class="brand-title">ALPHALINER TOP 100 MONITOR</span>
  </div>
  <a class="nav-link" href="../index.html">← MAIN PORTAL</a>
</header>

<main class="terminal">
  <section class="status">
    <span id="statusText">SYSTEM / ALPHALINER DATA ONLINE</span>
    <span>LAST REFRESH: <span id="generatedAt">-</span></span>
  </section>

  <div class="workspace">
    <aside class="panel">
      <div class="panel-header">
        <span class="panel-title">FUNCTIONS / TIME TRAVEL</span>
      </div>

      <section class="filter-section">
        <span class="label">COMPARISON BASE DATE</span>
        <select id="snapshotSelect" class="custom-select">
          <option value="">Loading historical dates...</option>
        </select>
      </section>

      <section class="filter-section">
        <span class="label">EXPORT DATA</span>
        <button class="btn" style="border-color:#34526e;color:#39a8ff;" onclick="exportToCsv()">
          EXPORT TOP 100 CSV
        </button>
      </section>

      <section class="filter-section" style="border-bottom:none;">
        <span class="label">DATA SOURCE</span>
        <div style="color:var(--muted);font-size:10px;line-height:1.5;">
          Official ExtJS Store: Top100description<br>
          Historical SQLite snapshots with Delta time-travel analysis.
        </div>
      </section>
    </aside>

    <section>
      <div class="heading">
        <div>
          <h1>TOP 100 CONTAINER LINER OPERATORS</h1>
          <div class="subtitle">Global Fleet Capacity, Orderbook & Historical Snapshot Delta</div>
        </div>
      </div>

      <!-- Highlights KPI -->
      <section class="kpi-grid">
        <div class="kpi">
          <span class="kpi-label">GLOBAL TOTAL<br>CAPACITY (TEU)</span>
          <span class="kpi-value" id="globalTeu">-</span>
          <span class="kpi-sub" id="top100TeuSub">TOP 100: -</span>
        </div>
        <div class="kpi">
          <span class="kpi-label">GLOBAL ACTIVE<br>SHIPS (FLEET)</span>
          <span class="kpi-value" id="globalShips">-</span>
          <span class="kpi-sub" id="top100ShipsSub">TOP 100: -</span>
        </div>
        <div class="kpi">
          <span class="kpi-label">MARKET LEADER<br>RANK #1</span>
          <span class="kpi-value" id="leaderName" style="color:var(--orange);">-</span>
          <span class="kpi-sub" id="leaderSub">-</span>
        </div>
        <div class="kpi">
          <span class="kpi-label">TOP 10 SHARE<br>CONCENTRATION</span>
          <span class="kpi-value" id="top10Share" style="color:var(--blue);">-</span>
          <span class="kpi-sub" id="top10TeuSub">-</span>
        </div>
      </section>

      <!-- Delta Comparison Summary -->
      <section class="panel" style="margin-bottom:14px;">
        <div class="panel-header">
          <span class="panel-title">DELTA ANALYSIS / VS BASE DATE</span>
          <span class="panel-title" id="deltaSubTitle">-</span>
        </div>
        <div style="padding:10px;">
          <div class="kpi-grid" id="deltaKpiContainer" style="margin-bottom:0;">
            <div class="kpi">
              <span class="kpi-label">TOP 100 Δ CAPACITY<br>(PERIOD CHANGE)</span>
              <span class="kpi-value" id="deltaTeuTotal">-</span>
              <span class="kpi-sub" id="deltaTeuSub">-</span>
            </div>
            <div class="kpi">
              <span class="kpi-label">TOP 100 Δ SHIPS<br>(PERIOD CHANGE)</span>
              <span class="kpi-value" id="deltaShipsTotal">-</span>
              <span class="kpi-sub" id="deltaShipsSub">-</span>
            </div>
            <div class="kpi">
              <span class="kpi-label">TOP GAINER<br>(MAX CAPACITY UP)</span>
              <span class="kpi-value" id="topGainName">-</span>
              <span class="kpi-sub" id="topGainVal">-</span>
            </div>
            <div class="kpi">
              <span class="kpi-label">TOP LOSER<br>(MAX CAPACITY DOWN)</span>
              <span class="kpi-value" id="topLossName">-</span>
              <span class="kpi-sub" id="topLossVal">-</span>
            </div>
          </div>
        </div>
      </section>

      <!-- Charts -->
      <div class="chart-grid">
        <section class="panel">
          <div class="panel-header">
            <span class="panel-title">TOP 10 OPERATOR CAPACITY / TEU</span>
          </div>
          <div class="chart-wrap">
            <canvas id="topOperatorsChart"></canvas>
          </div>
        </section>

        <section class="panel">
          <div class="panel-header">
            <span class="panel-title">GLOBAL VS TOP 100 CAPACITY TREND / MILLION TEU</span>
          </div>
          <div class="chart-wrap">
            <canvas id="historyChart"></canvas>
          </div>
        </section>
      </div>

      <!-- Delta Comparison Table -->
      <section class="panel" id="deltaTableCard" style="margin-bottom:14px;">
        <div class="panel-header">
          <span class="panel-title">OPERATOR CAPACITY & SHARE DELTA DETAIL</span>
          <span class="panel-title" id="deltaTableNote">-</span>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th style="text-align:center; width:45px;">RANK</th>
                <th style="text-align:center;">RANK Δ</th>
                <th style="text-align:left;">OPERATOR</th>
                <th>LATEST TEU</th>
                <th>BASE TEU</th>
                <th>Δ TEU</th>
                <th>LATEST %</th>
                <th>BASE %</th>
                <th>Δ %P</th>
              </tr>
            </thead>
            <tbody id="deltaTableBody"></tbody>
          </table>
        </div>
      </section>

      <!-- 2-Tier Authentic Alphaliner Official Table -->
      <section class="panel">
        <div class="panel-header">
          <span class="panel-title">ALPHALINER OFFICIAL TOP 100 FLEET BREAKDOWN</span>
          <span class="panel-title" id="tableNote">-</span>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr class="group-header">
                <th colspan="2" class="group-main">OPERATOR</th>
                <th colspan="3" class="group-total">TOTAL</th>
                <th colspan="3" class="group-owned">EXISTING (OWNED)</th>
                <th colspan="3" class="group-chartered">EXISTING (CHARTERED)</th>
                <th colspan="3" class="group-orderbook">ORDERBOOK</th>
              </tr>
              <tr>
                <th style="text-align:center; width:45px;">RANK</th>
                <th style="text-align:left;">OPERATOR NAME</th>
                <th>TEU</th>
                <th>SHIPS</th>
                <th>SHARE</th>
                <th>TEU</th>
                <th>SHIPS</th>
                <th>% OWN</th>
                <th>TEU</th>
                <th>SHIPS</th>
                <th>% CHAR</th>
                <th>TEU</th>
                <th>SHIPS</th>
                <th>% EXIST</th>
              </tr>
            </thead>
            <tbody id="operatorTableBody"></tbody>
          </table>
        </div>
      </section>
    </section>
  </div>
</main>

<script>
let topOperatorsChart = null;
let historyChart = null;
let allSnapshots = {};
let snapshotList = [];
let latestSnapshotId = null;

function numberFormat(val) {
  if (val === null || val === undefined || isNaN(val)) return "-";
  return new Intl.NumberFormat("en-US").format(Number(val));
}

function decimalFormat(val, digits = 2) {
  if (val === null || val === undefined || isNaN(val)) return "-";
  return Number(val).toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits
  });
}

function fmtDelta(val, isPct = false) {
  if (val === null || val === undefined || isNaN(val)) return "-";
  const num = Number(val);
  const sign = num > 0 ? "+" : "";
  if (isPct) return sign + num.toFixed(2) + "%p";
  return sign + numberFormat(num);
}

function deltaSpan(val, isPct = false) {
  if (val === null || val === undefined || isNaN(val) || val === 0) {
    return '<span class="delta-zero">-</span>';
  }
  const num = Number(val);
  const cls = num > 0 ? "delta-pos" : "delta-neg";
  const arrow = num > 0 ? "+ " : "";
  return `<span class="${cls}">${arrow}${fmtDelta(num, isPct)}</span>`;
}

function rankMoveBadge(diff) {
  if (!diff || isNaN(diff) || diff === 0) {
    return '<span class="delta-zero">-</span>';
  }
  if (diff > 0) {
    return `<span class="badge-rank-up">▲ UP ${diff}</span>`;
  }
  return `<span class="badge-rank-down">▼ DN ${Math.abs(diff)}</span>`;
}

function escapeHtml(val) {
  return String(val ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function renderKpis(snapshot) {
  const rows = snapshot.rows || [];
  const top100Teu = rows.reduce((sum, r) => sum + Number(r.total_teu || 0), 0);
  const top100Ships = rows.reduce((sum, r) => sum + Number(r.total_ships || 0), 0);

  const globalTeu = snapshot.global_teu || top100Teu;
  const globalShips = snapshot.global_ships || top100Ships;

  document.getElementById("globalTeu").textContent = numberFormat(globalTeu);
  document.getElementById("top100TeuSub").textContent = `TOP 100: ${numberFormat(top100Teu)} TEU`;

  document.getElementById("globalShips").textContent = numberFormat(globalShips);
  document.getElementById("top100ShipsSub").textContent = `TOP 100: ${numberFormat(top100Ships)} SHIPS`;

  const leader = rows.length ? rows[0] : null;
  if (leader) {
    document.getElementById("leaderName").textContent = leader.operator_name || "-";
    const shareText = leader.market_share ? (decimalFormat(leader.market_share, 2) + "% SHARE") : "";
    document.getElementById("leaderSub").textContent =
      numberFormat(leader.total_teu) + " TEU · " + shareText;
  }

  const top10Teu = rows.slice(0, 10).reduce((sum, r) => sum + Number(r.total_teu || 0), 0);
  const top10Share = globalTeu > 0 ? ((top10Teu / globalTeu) * 100) : 0;
  document.getElementById("top10Share").textContent = decimalFormat(top10Share, 2) + "%";
  document.getElementById("top10TeuSub").textContent = `TOP 10 TOTAL: ${numberFormat(top10Teu)} TEU`;
}

function renderDeltaAnalysis(latestSnapshot, compareSnapshot) {
  const latestRows = latestSnapshot.rows || [];
  const prevRows = compareSnapshot ? (compareSnapshot.rows || []) : [];

  if (!prevRows.length || latestSnapshot.snapshot_id === compareSnapshot.snapshot_id) {
    document.getElementById("deltaSubTitle").textContent = "SAME SNAPSHOT SELECTED";
    document.getElementById("deltaTeuTotal").textContent = "-";
    document.getElementById("deltaShipsTotal").textContent = "-";
    document.getElementById("topGainName").textContent = "-";
    document.getElementById("topGainVal").textContent = "-";
    document.getElementById("topLossName").textContent = "-";
    document.getElementById("topLossVal").textContent = "-";
    document.getElementById("deltaTableCard").style.display = "none";
    return;
  }

  document.getElementById("deltaTableCard").style.display = "block";
  document.getElementById("deltaSubTitle").textContent = `${latestSnapshot.data_date} vs ${compareSnapshot.data_date}`;

  const prevMap = new Map();
  prevRows.forEach(r => prevMap.set(r.operator_name, r));

  const curTotalTeu = latestRows.reduce((sum, r) => sum + Number(r.total_teu || 0), 0);
  const prevTotalTeu = prevRows.reduce((sum, r) => sum + Number(r.total_teu || 0), 0);
  const diffTotalTeu = curTotalTeu - prevTotalTeu;

  const curTotalShips = latestRows.reduce((sum, r) => sum + Number(r.total_ships || 0), 0);
  const prevTotalShips = prevRows.reduce((sum, r) => sum + Number(r.total_ships || 0), 0);
  const diffTotalShips = curTotalShips - prevTotalShips;

  document.getElementById("deltaTeuTotal").innerHTML = deltaSpan(diffTotalTeu);
  document.getElementById("deltaTeuSub").textContent = `VS ${compareSnapshot.data_date}`;

  document.getElementById("deltaShipsTotal").innerHTML = deltaSpan(diffTotalShips);
  document.getElementById("deltaShipsSub").textContent = `VS ${compareSnapshot.data_date}`;

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
      curTeu,
      prevTeu,
      diffTeu,
      curShare,
      prevShare,
      diffShare,
      rankDiff,
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
    `BASE: ${compareSnapshot.data_date} → LATEST: ${latestSnapshot.data_date}`;

  const tbody = document.getElementById("deltaTableBody");
  tbody.innerHTML = deltaList.map(item => `
    <tr>
      <td class="rank-cell">${escapeHtml(item.rank)}</td>
      <td style="text-align:center;">${rankMoveBadge(item.rankDiff)}</td>
      <td class="operator-cell">${escapeHtml(item.operator_name)}</td>
      <td>${numberFormat(item.curTeu)}</td>
      <td class="sub-col">${item.hasPrev ? numberFormat(item.prevTeu) : "-"}</td>
      <td>${item.hasPrev ? deltaSpan(item.diffTeu) : '<span class="badge-rank-up">NEW</span>'}</td>
      <td>${decimalFormat(item.curShare, 2)}%</td>
      <td class="sub-col">${item.hasPrev ? decimalFormat(item.prevShare, 2) + "%" : "-"}</td>
      <td>${item.hasPrev ? deltaSpan(item.diffShare, true) : "-"}</td>
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
        labels: top10.map(r => r.operator_name),
        datasets: [{
          label: "Capacity (TEU)",
          data: top10.map(r => Number(r.total_teu || 0)),
          backgroundColor: "#ff8a00",
          borderColor: "#ff8a00",
          borderWidth: 1,
          borderRadius: 1
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        indexAxis: "y",
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: "#07111c",
            titleColor: "#ff8a00",
            bodyColor: "#dce9f5",
            borderColor: "#23394d",
            borderWidth: 1,
            callbacks: {
              label: ctx => numberFormat(ctx.raw) + " TEU"
            }
          }
        },
        scales: {
          x: {
            ticks: {
              color: "#8aa0b5",
              font: { family: '"Roboto Mono", monospace', size: 10 },
              callback: v => (Number(v) / 1000000).toFixed(1) + "M"
            },
            grid: { color: "#172a3a" }
          },
          y: {
            ticks: {
              color: "#dce9f5",
              font: { family: '"Roboto Mono", monospace', size: 10, weight: "bold" }
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

  const labels = history.map(r => r.data_date || "");
  const globalValues = history.map(r => Number(r.global_teu || r.total_teu || 0) / 1000000);
  const top100Values = history.map(r => Number(r.total_teu || 0) / 1000000);

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
            borderColor: "#2bd682",
            backgroundColor: "rgba(43, 214, 130, 0.08)",
            fill: true,
            tension: 0.1,
            pointRadius: 2,
            pointBackgroundColor: "#2bd682"
          },
          {
            label: "TOP 100 Total",
            data: top100Values,
            borderColor: "#39a8ff",
            backgroundColor: "transparent",
            borderDash: [3, 3],
            tension: 0.1,
            pointRadius: 2,
            pointBackgroundColor: "#39a8ff"
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
              color: "#8aa0b5",
              font: { family: '"Roboto Mono", monospace', size: 10 }
            }
          },
          tooltip: {
            backgroundColor: "#07111c",
            titleColor: "#39a8ff",
            bodyColor: "#dce9f5",
            borderColor: "#23394d",
            borderWidth: 1,
            callbacks: {
              label: ctx => `${ctx.dataset.label}: ${decimalFormat(ctx.raw, 2)}M TEU`
            }
          }
        },
        scales: {
          y: {
            ticks: {
              color: "#8aa0b5",
              font: { family: '"Roboto Mono", monospace', size: 10 },
              callback: v => v + "M"
            },
            grid: { color: "#172a3a" }
          },
          x: {
            ticks: {
              color: "#8aa0b5",
              font: { family: '"Roboto Mono", monospace', size: 10 }
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

  tbody.innerHTML = rows.map(r => {
    const totalTeu = Number(r.total_teu || 0);
    const ownedTeu = Number(r.existing_owned_teu || 0);
    const chartTeu = Number(r.existing_chartered_teu || 0);

    const ownedPct = totalTeu > 0 && r.existing_owned_teu !== null
      ? ((ownedTeu / totalTeu) * 100).toFixed(1) + "%"
      : "-";

    const chartPct = totalTeu > 0 && r.existing_chartered_teu !== null
      ? ((chartTeu / totalTeu) * 100).toFixed(1) + "%"
      : "-";

    const shareStr = r.market_share !== null && r.market_share !== undefined
      ? decimalFormat(r.market_share, 2) + "%"
      : "-";

    const orderbookPct = r.orderbook_pct_existing !== null && r.orderbook_pct_existing !== undefined
      ? decimalFormat(r.orderbook_pct_existing, 1) + "%"
      : "-";

    return `
      <tr>
        <td class="rank-cell">${escapeHtml(r.rank)}</td>
        <td class="operator-cell">${escapeHtml(r.operator_name)}</td>

        <!-- TOTAL -->
        <td style="color:var(--blue);font-weight:700;">${numberFormat(r.total_teu)}</td>
        <td>${numberFormat(r.total_ships)}</td>
        <td style="color:var(--blue);">${shareStr}</td>

        <!-- EXISTING OWNED -->
        <td>${numberFormat(r.existing_owned_teu)}</td>
        <td class="sub-col">${numberFormat(r.existing_owned_ships)}</td>
        <td class="sub-col">${ownedPct}</td>

        <!-- EXISTING CHARTERED -->
        <td>${numberFormat(r.existing_chartered_teu)}</td>
        <td class="sub-col">${numberFormat(r.existing_chartered_ships)}</td>
        <td class="sub-col">${chartPct}</td>

        <!-- ORDERBOOK -->
        <td style="color:var(--green);font-weight:700;">${numberFormat(r.orderbook_teu)}</td>
        <td class="sub-col">${numberFormat(r.orderbook_ships)}</td>
        <td class="sub-col">${orderbookPct}</td>
      </tr>
    `;
  }).join("");

  document.getElementById("tableNote").textContent =
    `SNAPSHOT: ${snapshotDate || "-"} · ${numberFormat(rows.length)} OPERATORS`;
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

  targetSnapshot.rows.forEach(r => {
    const totalTeu = Number(r.total_teu || 0);
    const ownedTeu = Number(r.existing_owned_teu || 0);
    const chartTeu = Number(r.existing_chartered_teu || 0);

    const ownedPct = totalTeu > 0 && r.existing_owned_teu !== null
      ? ((ownedTeu / totalTeu) * 100).toFixed(2)
      : "";
    const chartPct = totalTeu > 0 && r.existing_chartered_teu !== null
      ? ((chartTeu / totalTeu) * 100).toFixed(2)
      : "";

    const opName = `"${String(r.operator_name || '').replace(/"/g, '""')}"`;

    const values = [
      r.rank ?? "",
      opName,
      r.total_teu ?? "",
      r.total_ships ?? "",
      r.market_share ?? "",
      r.existing_owned_teu ?? "",
      r.existing_owned_ships ?? "",
      ownedPct,
      r.existing_chartered_teu ?? "",
      r.existing_chartered_ships ?? "",
      chartPct,
      r.orderbook_teu ?? "",
      r.orderbook_ships ?? "",
      r.orderbook_pct_existing ?? ""
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

    snapshotList = Object.values(allSnapshots).sort((a, b) => a.snapshot_id - b.snapshot_id);

    if (!snapshotList.length) {
      throw new Error("저장된 스냅샷 데이터가 없습니다.");
    }

    const latestSnapshot = snapshotList[snapshotList.length - 1];
    latestSnapshotId = latestSnapshot.snapshot_id;

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
        opt.selected = true;
      } else {
        opt.textContent = `${s.data_date}`;
      }
      select.appendChild(opt);
    }

    select.addEventListener("change", handleSnapshotChange);

    renderHistoryChart(history);
    handleSnapshotChange();

    document.getElementById("generatedAt").textContent = data.generated_at || "-";
  } catch (error) {
    console.error(error);
    document.getElementById("statusText").textContent = "DATA LOAD ERROR: " + error.message;
  }
}

document.addEventListener("DOMContentLoaded", loadDashboard);
</script>
</body>
</html>
"""
    atomic_write_text(HTML_PATH, content)
    log(f"[STATIC] HTML 생성 완료: {HTML_PATH.name}")

# =========================================================
# 8. Local Flask App
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
# 9. Main Entrypoint
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
