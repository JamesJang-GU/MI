# -*- coding: utf-8 -*-
"""
Alphaliner TOP 100 Liner Operators Static Dashboard Crawler (GitHub Optimized)
Modified to extract data directly from ExtJS Store for full field availability.
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
                output[column] = pd.to_datetime(output[column], errors="coerce").dt.strftime("%Y-%m-%d")
    output = output.replace([float("inf"), float("-inf")], pd.NA)
    output = output.where(pd.notnull(output), None)
    return json.loads(output.to_json(orient="records", force_ascii=False, date_format="iso"))

# =========================================================
# 4. SQLite Database
# =========================================================
def get_db_conn():
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS alphaliner_master (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            captured_at TEXT NOT NULL,
            data_date TEXT NOT NULL,
            data_hash TEXT NOT NULL UNIQUE,
            total_teu INTEGER,
            total_ships INTEGER,
            operator_count INTEGER NOT NULL
        )
    """)
    connection.execute("""
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
    """)
    connection.execute("""
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
    """)
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
        row = connection.execute("""
            SELECT started_at, finished_at, status, message, snapshot_id, operator_count, data_date
            FROM crawl_audit ORDER BY audit_id DESC LIMIT 1
        """).fetchone()
        if not row: return {}
        return {
            "started_at": row[0], "finished_at": row[1], "status": row[2],
            "message": row[3], "snapshot_id": row[4], "operator_count": row[5], "data_date": row[6],
        }
    finally:
        connection.close()

def save_crawl_audit(started_at, finished_at, status, message, snapshot_id=None, operator_count=0, data_date=None):
    connection = get_db_conn()
    try:
        connection.execute("""
            INSERT INTO crawl_audit (started_at, finished_at, status, message, snapshot_id, operator_count, data_date)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (started_at, finished_at, status, str(message)[:1000], snapshot_id, int(operator_count or 0), str(data_date or "")))
        connection.commit()
    finally:
        connection.close()

def make_data_hash(dataframe, data_date):
    if dataframe is None: dataframe = pd.DataFrame()
    working = dataframe.copy().sort_values("rank").reset_index(drop=True)
    payload = {"data_date": str(data_date or ""), "rows": dataframe_to_records(working)}
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def save_snapshot(bundle):
    dataframe = bundle.get("df", pd.DataFrame()).copy()
    data_date = bundle.get("data_date", datetime.now().strftime("%Y-%m-%d"))
    if dataframe.empty: raise RuntimeError("저장할 Alphaliner 데이터가 없습니다.")
    data_hash = make_data_hash(dataframe, data_date)
    connection = get_db_conn()
    try:
        cursor = connection.cursor()
        existing = cursor.execute("SELECT snapshot_id FROM alphaliner_master WHERE data_hash = ?", (data_hash,)).fetchone()
        if existing:
            log(f"[DB] 동일 Alphaliner 데이터 중복 (snapshot_id={existing[0]})")
            return int(existing[0]), False
        captured_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        total_teu = int(dataframe["total_teu"].sum()) if "total_teu" in dataframe.columns else 0
        total_ships = int(dataframe["total_ships"].sum()) if "total_ships" in dataframe.columns else 0
        cursor.execute("""
            INSERT INTO alphaliner_master (captured_at, data_date, data_hash, total_teu, total_ships, operator_count)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (captured_at, data_date, data_hash, total_teu, total_ships, int(len(dataframe))))
        snapshot_id = cursor.lastrowid
        insert_rows = []
        for _, row in dataframe.iterrows():
            insert_rows.append((
                snapshot_id, int(row["rank"]), str(row["operator_name"]), int(row["total_teu"]),
                safe_int(row.get("total_ships")), safe_float(row.get("market_share")),
                safe_int(row.get("existing_owned_teu")), safe_int(row.get("existing_owned_ships")),
                safe_int(row.get("existing_chartered_teu")), safe_int(row.get("existing_chartered_ships")),
                safe_int(row.get("orderbook_teu")), safe_int(row.get("orderbook_ships")),
                safe_float(row.get("orderbook_pct_existing"))
            ))
        cursor.executemany("""
            INSERT INTO alphaliner_detail (snapshot_id, rank, operator_name, total_teu, total_ships, market_share,
            existing_owned_teu, existing_owned_ships, existing_chartered_teu, existing_chartered_ships,
            orderbook_teu, orderbook_ships, orderbook_pct_existing) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, insert_rows)
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
        dataframe = pd.read_sql_query("""
            SELECT snapshot_id, captured_at, data_date, total_teu, total_ships, operator_count
            FROM alphaliner_master ORDER BY data_date ASC, snapshot_id ASC
        """, connection)
    finally:
        connection.close()
    if dataframe.empty: return pd.DataFrame()
    dataframe["data_date"] = pd.to_datetime(dataframe["data_date"], errors="coerce")
    dataframe["captured_at"] = pd.to_datetime(dataframe["captured_at"], errors="coerce")
    return dataframe.dropna(subset=["data_date"]).sort_values(["data_date", "snapshot_id"]).drop_duplicates(subset=["data_date"], keep="last").reset_index(drop=True)

def load_latest_detail_df():
    connection = get_db_conn()
    try:
        dataframe = pd.read_sql_query("""
            SELECT d.*, m.data_date, m.captured_at
            FROM alphaliner_detail AS d
            INNER JOIN alphaliner_master AS m ON d.snapshot_id = m.snapshot_id
            WHERE d.snapshot_id = (SELECT snapshot_id FROM alphaliner_master ORDER BY data_date DESC, snapshot_id DESC LIMIT 1)
            ORDER BY d.rank ASC
        """, connection)
    finally:
        connection.close()
    return dataframe

# =========================================================
# 5. Selenium Driver & Parser (EXTJS OPTIMIZED)
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
    if headless: options.add_argument("--headless=new")
    chrome_binary = os.getenv("CHROME_BINARY", "").strip()
    if chrome_binary: options.binary_location = chrome_binary
    if driver_path:
        return webdriver.Chrome(service=Service(driver_path), options=options)
    try:
        return webdriver.Chrome(options=options)
    except WebDriverException as first_error:
        if not HAS_WEBDRIVER_MANAGER: raise first_error
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
        summary: {}, regional: {}, store_id: null, rows: [], available_stores: []
    };
    try {
        const text = out.page_text;
        const activeShips = text.match(/([0-9,]+)\s+active ships/i);
        const totalTeu = text.match(/([0-9,]+)\s+TEU/i);
        out.summary.total_active_ships = activeShips ? activeShips[1] : null;
        out.summary.total_teu = totalTeu ? totalTeu[1] : null;
        const regBlock = text.match(/Regional Trades[\s\S]*?weekly capacities[\s\S]*?Trans-Atlantic[\s\S]*?Trans-Pacific[\s\S]*?Feast-Europe[\s\S]*?([0-9,]+)\s+TEU[\s\S]*?([0-9,]+)\s+TEU[\s\S]*?([0-9,]+)\s+TEU/i);
        if (regBlock) {
            out.regional.trans_atlantic = regBlock[1];
            out.regional.trans_pacific = regBlock[2];
            out.regional.feast_europe = regBlock[3];
        }
    } catch(e) { out.summary_error = String(e); }

    try {
        if (typeof Ext !== "undefined" && Ext.StoreManager && Ext.StoreManager.items) {
            let targetStore = null;
            Ext.StoreManager.items.forEach(function(store) {
                try {
                    if (store && store.storeId) out.available_stores.push(store.storeId);
                    if (!targetStore && store && store.storeId === "Top100description") targetStore = store;
                } catch(ie) {}
            });
            if (targetStore) {
                out.store_id = targetStore.storeId;
                for (let i=0; i= 90;
            } catch (e) { return false; }
        """))
        time.sleep(2)
        payload = extract_alphaliner_payload(driver)
        raw_rows = payload.get("rows", [])
        log(f"[FETCH] ExtJS Store 추출 결과: store={payload.get('store_id')}, raw_rows={len(raw_rows)}")
        dataframe = parse_alphaliner_table(raw_rows)
        if dataframe.empty:
            raise RuntimeError("Alphaliner ExtJS Store에서 랭킹 데이터를 파싱하지 못했습니다.")
        if len(dataframe) < 90:
            raise RuntimeError(f"Alphaliner 수집 데이터가 불완전합니다. 수집 건수={len(dataframe)}건, 기대 건수=TOP 100")
        dataframe = dataframe.sort_values("rank").head(100).reset_index(drop=True)
        data_date = datetime.now().strftime("%Y-%m-%d")
        log(f"[FETCH] 수집 성공: operators={len(dataframe)}, date={data_date}")
        return {"df": dataframe, "data_date": data_date}
    finally:
        if driver: driver.quit()

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
            save_crawl_audit(started_at, finished_at, "SUCCESS", "NEW_SNAPSHOT" if is_new else "DUPLICATE_SNAPSHOT", snapshot_id, len(dataframe), bundle.get("data_date"))
            return {"ok": True, "snapshot_id": snapshot_id, "is_new": is_new, "operator_count": len(dataframe)}
        except Exception as error:
            activate_file_logging()
            logger.exception(f"[ERROR] Alphaliner 수집 실패: {error}")
            finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try: save_crawl_audit(started_at, finished_at, "ERROR", str(error))
            except: pass
            return {"ok": False, "error": str(error)}

def build_static_json():
    history = load_history_df()
    latest_detail = load_latest_detail_df()
    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": {"name": "Alphaliner TOP 100", "url": ALPHALINER_URL},
        "last_sync": get_last_sync_meta(),
        "history": dataframe_to_records(history, date_columns=["data_date", "captured_at"]),
        "latest_detail": dataframe_to_records(latest_detail, date_columns=["data_date", "captured_at"]),
    }
    atomic_write_text(JSON_PATH, json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
    log(f"[STATIC] JSON 생성 완료: {JSON_PATH.name}")

def generate_static_html():
    content = r"""


  
  
  Alphaliner TOP 100 Dashboard
  
  


  

    

      
Alphaliner TOP 100 Liner Operators

      
Global container liner operator capacity, fleet and orderbook monitoring dashboard


    

  

  

    

      
      Alphaliner 데이터를 불러오는 중입니다.
    

    

      

        
TOP 100 총 선복량
-
Total Capacity

        
운항 선사 수
-
Operators in latest snapshot

        
총 선박 수
-
Total Ships

        
1위 선사
-
-

      

      

        
상위 10개 선사 선복량
Latest snapshot · 단위: TEU


        
TOP 100 총 선복량 추이
Historical snapshots · 단위: Million TEU


      

      

        
최신 TOP 100 선사 순위
-


        

          
Rank	Operator	Total TEU	Ships	Market Share	Orderbook TEU	Orderbook / Existing

        

      

    

    

    
Source: Alphaliner TOP 100 · Generated: -

  

  


"""
    atomic_write_text(HTML_PATH, content)
    log(f"[STATIC] HTML 생성 완료: {HTML_PATH.name}")

# =========================================================
# 7. Local Flask App
# =========================================================
@app.route("/")
def index():
    if not HTML_PATH.exists(): generate_static_html()
    return Response(HTML_PATH.read_text(encoding="utf-8"), mimetype="text/html; charset=utf-8")

@app.route("/alphaliner_data.json")
def serve_json():
    return send_from_directory(BASE_DIR, JSON_PATH.name, mimetype="application/json")

# =========================================================
# 8. Main Entrypoint
# =========================================================
def run_cron_job(args):
    log("=" * 60 + "\n[CRON] Alphaliner Dashboard 갱신 시작\n" + "=" * 60)
    result = run_alphaliner_sync(driver_path=args.driver_path, headless=not args.show_browser)
    build_static_json()
    generate_static_html()
    if result.get("ok"): log("[CRON] Alphaliner Dashboard 갱신 완료")
    else log("[WARN] 최신 수집 실패. 기존 DB 기반 빌드.")
    log("=" * 60)
    return result

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cron", action="store_true")
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--driver-path", default=os.getenv("CHROMEDRIVER_PATH", ""))
    parser.add_argument("--show-browser", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    return parser.parse_args()

def main():
    args = parse_args()
    if not args.cron and not args.local: args.cron = True
    activate_file_logging()
    get_db_conn().close()
    result = run_cron_job(args)
    if args.cron:
        if not result.get("ok") and get_total_snapshot_count() == 0: sys.exit(1)
        sys.exit(0)
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
