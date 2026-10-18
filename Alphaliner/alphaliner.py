# -*- coding: utf-8 -*-
"""
Alphaliner TOP 100 Liner Operators Static Dashboard Crawler
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
from selenium.common.exceptions import TimeoutException, WebDriverException
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
TEMPLATE_PATH = BASE_DIR / "alphaliner.html"
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
    if any(
        isinstance(handler, logging.FileHandler) for handler in logger.handlers
    ):
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
    text = str(value or "").strip()
    if not text or text.lower() in {"-", "n/a", "na", "none", "null", "--"}:
        return None
    cleaned = text.replace(",", "")
    cleaned = re.sub(r"[^0-9\-]", "", cleaned)
    if not cleaned or cleaned == "-":
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None

def safe_float(value):
    text = str(value or "").strip()
    if not text or text.lower() in {"-", "n/a", "na", "none", "null", "--"}:
        return None
    is_negative = text.startswith("-") or (
        text.startswith("(") and text.endswith(")")
    )
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
                output[column] = pd.to_datetime(
                    output[column], errors="coerce"
                ).dt.strftime("%Y-%m-%d")
    output = output.replace([float("inf"), float("-inf")], pd.NA)
    output = output.where(pd.notnull(output), None)
    return json.loads(
        output.to_json(orient="records", force_ascii=False, date_format="iso")
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
        row = connection.execute(
            "SELECT COUNT(*) FROM alphaliner_master"
        ).fetchone()
        return int(row[0] if row else 0)
    finally:
        connection.close()

def get_last_sync_meta():
    connection = get_db_conn()
    try:
        row = connection.execute(
            """
            SELECT started_at, finished_at, status, message, snapshot_id, operator_count, data_date
            FROM crawl_audit
            ORDER BY audit_id DESC
            LIMIT 1
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

def save_crawl_audit(
    started_at,
    finished_at,
    status,
    message,
    snapshot_id=None,
    operator_count=0,
    data_date=None,
):
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
    text = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
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
            log(
                f"[DB] 동일 Alphaliner 데이터 중복 (snapshot_id={existing[0]})"
            )
            return int(existing[0]), False

        captured_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        total_teu = int(dataframe["total_teu"].sum()) if "total_teu" in dataframe else 0
        total_ships = int(dataframe["total_ships"].sum()) if "total_ships" in dataframe else 0

        cursor.execute(
            """
            INSERT INTO alphaliner_master (captured_at, data_date, data_hash, total_teu, total_ships, operator_count)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                captured_at,
                data_date,
                data_hash,
                total_teu,
                total_ships,
                int(len(dataframe)),
            ),
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
        log(
            f"[DB] 신규 Alphaliner Snapshot 저장 완료: id={snapshot_id}, operators={len(dataframe)}"
        )
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
            FROM alphaliner_master
            ORDER BY data_date ASC, snapshot_id ASC
            """,
            connection,
        )
    finally:
        connection.close()

    if dataframe.empty:
        return pd.DataFrame()

    dataframe["data_date"] = pd.to_datetime(dataframe["data_date"], errors="coerce")
    dataframe["captured_at"] = pd.to_datetime(dataframe["captured_at"], errors="coerce")
    return dataframe.dropna(subset=["data_date"]).sort_values(
        ["data_date", "snapshot_id"]
    ).drop_duplicates(subset=["data_date"], keep="last").reset_index(drop=True)

def load_latest_detail_df():
    connection = get_db_conn()
    try:
        dataframe = pd.read_sql_query(
            """
            SELECT d.*, m.data_date, m.captured_at
            FROM alphaliner_detail AS d
            INNER JOIN alphaliner_master AS m ON d.snapshot_id = m.snapshot_id
            WHERE d.snapshot_id = (SELECT snapshot_id FROM alphaliner_master ORDER BY data_date DESC, snapshot_id DESC LIMIT 1)
            ORDER BY d.rank ASC
            """,
            connection,
        )
    finally:
        connection.close()
    return dataframe

# =========================================================
# 5. Selenium Driver & Parser
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
        return webdriver.Chrome(
            service=Service(installed_driver), options=options
        )

def wait_for_page_ready(driver):
    wait = WebDriverWait(driver, SELENIUM_WAIT_SECONDS)
    wait.until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )
    wait.until(
        lambda d: len(
            d.execute_script("return (document.body.innerText || '').trim()")
        )
        > 50
    )

def extract_alphaliner_payload(driver):
    script = r"""
    function cleanText(value) { return String(value || "").replace(/\s+/g, " ").trim(); }
    const rows = [];
    const table = document.querySelector("table.table, table");
    if (!table) {
        return { error: "No table found", rows: [] };
    }
    const trList = table.querySelectorAll("tbody tr, tr");
    trList.forEach(tr => {
        const cells = Array.from(tr.querySelectorAll("th, td")).map(td => cleanText(td.innerText));
        if (cells.length >= 4) {
            rows.push(cells);
        }
    });
    return {
        page_text: cleanText(document.body.innerText || ""),
        rows: rows
    };
    """
    return driver.execute_script(script)

def parse_alphaliner_table(rows):
    if not rows:
        return pd.DataFrame()

    parsed = []
    for r in rows:
        if not r or len(r) < 4:
            continue
        rank_val = safe_int(r[0])
        if rank_val is None:
            continue

        op_name = normalize_text(r[1])
        if not op_name:
            continue

        teu_val = safe_int(r[2])
        if teu_val is None and len(r) > 3:
            teu_val = safe_int(r[3])

        if teu_val is None:
            continue

        ships_val = safe_int(r[4]) if len(r) > 4 else None
        share_val = safe_float(r[5]) if len(r) > 5 else None

        parsed.append(
            {
                "rank": rank_val,
                "operator_name": op_name,
                "total_teu": teu_val,
                "total_ships": ships_val,
                "market_share": share_val,
                "existing_owned_teu": safe_int(r[6]) if len(r) > 6 else None,
                "existing_owned_ships": safe_int(r[7]) if len(r) > 7 else None,
                "existing_chartered_teu": safe_int(r[8]) if len(r) > 8 else None,
                "existing_chartered_ships": safe_int(r[9]) if len(r) > 9 else None,
                "orderbook_teu": safe_int(r[10]) if len(r) > 10 else None,
                "orderbook_ships": safe_int(r[11]) if len(r) > 11 else None,
                "orderbook_pct_existing": safe_float(r[12]) if len(r) > 12 else None,
            }
        )

    df = pd.DataFrame(parsed)
    if not df.empty:
        df = df.drop_duplicates(subset=["rank"]).sort_values("rank").reset_index(drop=True)
    return df

def fetch_alphaliner(driver_path="", headless=True):
    driver = None
    try:
        driver = get_driver(driver_path=driver_path, headless=headless)
        log("[FETCH] Alphaliner TOP 100 웹사이트 접속 중...")
        driver.get(ALPHALINER_URL)
        wait_for_page_ready(driver)
        time.sleep(PAGE_LOAD_WAIT_SECONDS)

        payload = extract_alphaliner_payload(driver)
        rows = payload.get("rows", [])
        df = parse_alphaliner_table(rows)

        if df.empty:
            raise RuntimeError("Alphaliner 사이트에서 랭킹 데이터를 파싱하지 못했습니다.")

        data_date = datetime.now().strftime("%Y-%m-%d")
        log(f"[FETCH] 수집 성공: operators={len(df)}, date={data_date}")
        return {"df": df, "data_date": data_date}
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
            bundle = fetch_alphaliner(
                driver_path=driver_path, headless=headless
            )
            snapshot_id, is_new = save_snapshot(bundle)
            finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            df = bundle.get("df", pd.DataFrame())

            save_crawl_audit(
                started_at=started_at,
                finished_at=finished_at,
                status="SUCCESS",
                message="NEW_SNAPSHOT" if is_new else "DUPLICATE_SNAPSHOT",
                snapshot_id=snapshot_id,
                operator_count=len(df),
                data_date=bundle.get("data_date"),
            )
            return {
                "ok": True,
                "snapshot_id": snapshot_id,
                "is_new": is_new,
                "operator_count": len(df),
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
            return {"ok": False, "error": str(error)}

def build_static_json():
    history = load_history_df()
    latest_detail = load_latest_detail_df()

    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": {"name": "Alphaliner TOP 100", "url": ALPHALINER_URL},
        "last_sync": get_last_sync_meta(),
        "history": dataframe_to_records(
            history, date_columns=["data_date", "captured_at"]
        ),
        "latest_detail": dataframe_to_records(
            latest_detail, date_columns=["data_date", "captured_at"]
        ),
    }

    atomic_write_text(
        JSON_PATH,
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ),
    )
    log(f"[STATIC] JSON 생성 완료: {JSON_PATH.name}")

def generate_static_html():
    if TEMPLATE_PATH.exists():
        content = TEMPLATE_PATH.read_text(encoding="utf-8")
    else:
        content = "<h1>Alphaliner Dashboard Template (alphaliner.html) Missing</h1>"
    atomic_write_text(HTML_PATH, content)
    log(f"[STATIC] HTML 생성 완료: {HTML_PATH.name}")

# =========================================================
# 7. Local Flask App
# =========================================================
@app.route("/")
def index():
    if not HTML_PATH.exists():
        generate_static_html()
    return Response(
        HTML_PATH.read_text(encoding="utf-8"),
        mimetype="text/html; charset=utf-8",
    )

@app.route("/alphaliner_data.json")
def serve_json():
    return send_from_directory(
        BASE_DIR, JSON_PATH.name, mimetype="application/json"
    )

# =========================================================
# 8. Main Entrypoint
# =========================================================
def run_cron_job(args):
    log("=" * 60)
    log("[CRON] Alphaliner Dashboard 갱신 시작")
    log("=" * 60)
    result = run_alphaliner_sync(
        driver_path=args.driver_path, headless=not args.show_browser
    )
    build_static_json()
    generate_static_html()
    if result.get("ok"):
        log("[CRON] Alphaliner Dashboard 갱신 완료")
    else:
        log(
            "[WARN] 최신 Alphaliner 수집 실패. 기존 DB 데이터 기반으로 정적 파일을 빌드했습니다."
        )
    log("=" * 60)
    return result

def parse_args():
    parser = argparse.ArgumentParser(
        description="Alphaliner TOP 100 Liner Operators Dashboard"
    )
    parser.add_argument("--cron", action="store_true")
    parser.add_argument("--local", action="store_true")
    parser.add_argument(
        "--driver-path", default=os.getenv("CHROMEDRIVER_PATH", "")
    )
    parser.add_argument("--show-browser", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    return parser.parse_args()

def main():
    args = parse_args()
    if not args.cron and not args.local:
        args.cron = True

    activate_file_logging()

    conn = get_db_conn()
    conn.close()

    result = run_cron_job(args)

    if args.cron:
        if not result.get("ok") and get_total_snapshot_count() == 0:
            log("[ERROR] Alphaliner 수집 실패 및 기존 Snapshot 데이터 없음")
            sys.exit(1)
        log("[SYSTEM] Alphaliner batch process completed. Process exiting.")
        sys.exit(0)

    app.run(
        host=args.host, port=args.port, debug=False, use_reloader=False
    )

if __name__ == "__main__":
    main()
