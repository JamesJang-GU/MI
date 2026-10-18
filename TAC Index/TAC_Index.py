# -*- coding: utf-8 -*-
"""
TAC Air Freight Rate Index Static Dashboard
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

DB_PATH = BASE_DIR / "TAC_Index.db"
JSON_PATH = BASE_DIR / "tac_index_data.json"
HTML_PATH = BASE_DIR / "index.html"
TEMPLATE_PATH = BASE_DIR / "TAC_Index.html"
LOG_PATH = BASE_DIR / "tac_index_crawl.log"

TAC_INDEX_URL = "https://www.tacindex.com/"

PAGE_LOAD_WAIT_SECONDS = 10
SELENIUM_WAIT_SECONDS = 30

app = Flask(__name__)
crawl_lock = Lock()

# =========================================================
# 2. Logging
# =========================================================
logger = logging.getLogger("tac_index")
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

def normalize_route_code(value):
    return normalize_text(value).upper()

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

def parse_date(value):
    text = normalize_text(value)
    if not text:
        return None
    formats = [
        "%d %b %Y",
        "%d %B %Y",
        "%d-%b-%Y",
        "%d-%B-%Y",
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%d/%m/%Y",
        "%m/%d/%Y",
        "%d.%m.%Y",
        "%Y.%m.%d",
    ]
    for date_format in formats:
        try:
            return datetime.strptime(text, date_format).date()
        except ValueError:
            continue
    parsed = pd.to_datetime(text, errors="coerce", dayfirst=True)
    return None if pd.isna(parsed) else parsed.date()

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
# 4. SQLite Database (자동 생성 지원)
# =========================================================
def get_db_conn():
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=30000")

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS tac_master (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            captured_at TEXT NOT NULL,
            data_date TEXT NOT NULL,
            data_hash TEXT NOT NULL UNIQUE,
            overall_index REAL,
            weekly_change REAL,
            route_count INTEGER NOT NULL
        )
        """
    )

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS tac_detail (
            snapshot_id INTEGER NOT NULL,
            route_code TEXT NOT NULL,
            route_name TEXT NOT NULL,
            index_value REAL NOT NULL,
            weekly_change REAL,
            monthly_change REAL,
            PRIMARY KEY (snapshot_id, route_code),
            FOREIGN KEY (snapshot_id) REFERENCES tac_master(snapshot_id)
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
            route_count INTEGER,
            data_date TEXT
        )
        """
    )

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_tac_master_data_date
        ON tac_master (data_date)
        """
    )

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_tac_detail_route
        ON tac_detail (route_code)
        """
    )

    connection.commit()
    return connection

def get_total_snapshot_count():
    connection = get_db_conn()
    try:
        row = connection.execute(
            "SELECT COUNT(*) FROM tac_master"
        ).fetchone()
        return int(row[0] if row else 0)
    finally:
        connection.close()

def get_last_sync_meta():
    connection = get_db_conn()
    try:
        row = connection.execute(
            """
            SELECT started_at, finished_at, status, message, snapshot_id, route_count, data_date
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
            "route_count": row[5],
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
    route_count=0,
    data_date=None,
):
    connection = get_db_conn()
    try:
        connection.execute(
            """
            INSERT INTO crawl_audit (started_at, finished_at, status, message, snapshot_id, route_count, data_date)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                started_at,
                finished_at,
                status,
                str(message)[:1000],
                snapshot_id,
                int(route_count or 0),
                str(data_date or ""),
            ),
        )
        connection.commit()
    finally:
        connection.close()

def make_data_hash(dataframe, summary):
    if dataframe is None:
        dataframe = pd.DataFrame()
    working = dataframe.copy()
    required_columns = [
        "route_code",
        "route_name",
        "index_value",
        "weekly_change",
        "monthly_change",
    ]
    for column in required_columns:
        if column not in working.columns:
            working[column] = None
    working = working[required_columns].sort_values(
        ["route_code", "route_name"]
    ).reset_index(drop=True)

    payload = {
        "latest_date": str(summary.get("latest_date", "")),
        "overall_index": summary.get("overall_index"),
        "weekly_change": summary.get("weekly_change"),
        "routes": dataframe_to_records(working),
    }
    text = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def save_snapshot(bundle):
    dataframe = bundle.get("df", pd.DataFrame()).copy()
    summary = bundle.get("summary", {}).copy()

    if dataframe.empty:
        raise RuntimeError("저장할 TAC 노선 데이터가 없습니다.")

    required_columns = [
        "route_code",
        "route_name",
        "index_value",
        "weekly_change",
        "monthly_change",
    ]
    for column in required_columns:
        if column not in dataframe.columns:
            dataframe[column] = None

    dataframe["route_code"] = dataframe["route_code"].apply(
        normalize_route_code
    )
    dataframe["route_name"] = dataframe["route_name"].apply(normalize_text)
    dataframe["index_value"] = pd.to_numeric(
        dataframe["index_value"], errors="coerce"
    )
    dataframe["weekly_change"] = pd.to_numeric(
        dataframe["weekly_change"], errors="coerce"
    )
    dataframe["monthly_change"] = pd.to_numeric(
        dataframe["monthly_change"], errors="coerce"
    )

    dataframe = dataframe.dropna(
        subset=["route_code", "route_name", "index_value"]
    )
    dataframe = dataframe[dataframe["route_code"].str.len() > 0]
    dataframe = dataframe.drop_duplicates(
        subset=["route_code"], keep="last"
    ).reset_index(drop=True)

    if dataframe.empty:
        raise RuntimeError(
            "정제 후 저장 가능한 TAC 노선 데이터가 없습니다."
        )

    latest_date = parse_date(summary.get("latest_date"))
    if latest_date is None:
        latest_date = datetime.now().date()
    summary["latest_date"] = latest_date.isoformat()

    data_hash = make_data_hash(dataframe, summary)
    connection = get_db_conn()

    try:
        cursor = connection.cursor()
        existing = cursor.execute(
            "SELECT snapshot_id FROM tac_master WHERE data_hash = ?",
            (data_hash,),
        ).fetchone()

        if existing:
            log(
                f"[DB] 동일 TAC 데이터 중복 (snapshot_id={existing[0]})"
            )
            return int(existing[0]), False

        captured_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(
            """
            INSERT INTO tac_master (captured_at, data_date, data_hash, overall_index, weekly_change, route_count)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                captured_at,
                summary["latest_date"],
                data_hash,
                safe_float(summary.get("overall_index")),
                safe_float(summary.get("weekly_change")),
                int(len(dataframe)),
            ),
        )
        snapshot_id = cursor.lastrowid

        insert_rows = []
        for _, row in dataframe.iterrows():
            insert_rows.append(
                (
                    snapshot_id,
                    str(row["route_code"]),
                    str(row["route_name"]),
                    float(row["index_value"]),
                    (
                        float(row["weekly_change"])
                        if pd.notna(row["weekly_change"])
                        else None
                    ),
                    (
                        float(row["monthly_change"])
                        if pd.notna(row["monthly_change"])
                        else None
                    ),
                )
            )

        cursor.executemany(
            """
            INSERT INTO tac_detail (snapshot_id, route_code, route_name, index_value, weekly_change, monthly_change)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            insert_rows,
        )
        connection.commit()
        log(
            f"[DB] 신규 Snapshot 저장 완료: id={snapshot_id}, routes={len(dataframe)}"
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
            SELECT snapshot_id, captured_at, data_date, overall_index, weekly_change, route_count
            FROM tac_master
            ORDER BY data_date ASC, snapshot_id ASC
            """,
            connection,
        )
    finally:
        connection.close()

    if dataframe.empty:
        return pd.DataFrame(
            columns=[
                "snapshot_id",
                "captured_at",
                "data_date",
                "overall_index",
                "weekly_change",
                "route_count",
            ]
        )

    dataframe["data_date"] = pd.to_datetime(
        dataframe["data_date"], errors="coerce"
    )
    dataframe["captured_at"] = pd.to_datetime(
        dataframe["captured_at"], errors="coerce"
    )
    dataframe["overall_index"] = pd.to_numeric(
        dataframe["overall_index"], errors="coerce"
    )
    dataframe["weekly_change"] = pd.to_numeric(
        dataframe["weekly_change"], errors="coerce"
    )
    dataframe = dataframe.dropna(subset=["data_date"])
    dataframe = dataframe.sort_values(
        ["data_date", "snapshot_id"]
    ).drop_duplicates(subset=["data_date"], keep="last")
    return dataframe.reset_index(drop=True)

def load_route_history_df():
    connection = get_db_conn()
    try:
        dataframe = pd.read_sql_query(
            """
            SELECT m.snapshot_id, m.data_date, m.captured_at, d.route_code, d.route_name, d.index_value, d.weekly_change, d.monthly_change
            FROM tac_master AS m
            INNER JOIN tac_detail AS d ON m.snapshot_id = d.snapshot_id
            ORDER BY m.data_date ASC, d.route_code ASC
            """,
            connection,
        )
    finally:
        connection.close()

    if dataframe.empty:
        return pd.DataFrame(
            columns=[
                "snapshot_id",
                "data_date",
                "captured_at",
                "route_code",
                "route_name",
                "index_value",
                "weekly_change",
                "monthly_change",
            ]
        )

    dataframe["data_date"] = pd.to_datetime(
        dataframe["data_date"], errors="coerce"
    )
    dataframe["captured_at"] = pd.to_datetime(
        dataframe["captured_at"], errors="coerce"
    )
    for col in ["index_value", "weekly_change", "monthly_change"]:
        dataframe[col] = pd.to_numeric(dataframe[col], errors="coerce")
    dataframe["route_code"] = dataframe["route_code"].apply(
        normalize_route_code
    )
    dataframe["route_name"] = dataframe["route_name"].apply(normalize_text)
    dataframe = dataframe.dropna(
        subset=["data_date", "route_code", "index_value"]
    )
    dataframe = dataframe.sort_values(
        ["data_date", "snapshot_id"]
    ).drop_duplicates(subset=["data_date", "route_code"], keep="last")
    return dataframe.reset_index(drop=True)

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
    options.add_argument("--window-size=1600,1200")

    if headless:
        options.add_argument("--headless=new")

    chrome_binary = os.getenv("CHROME_BINARY", "").strip()
    if chrome_binary:
        options.binary_location = chrome_binary

    if driver_path:
        return webdriver.Chrome(
            service=Service(driver_path), options=options
        )

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

def extract_tac_payload(driver):
    script = r"""
    function cleanText(value) { return String(value || "").replace(/\s+/g, " ").trim(); }
    function scoreTable(table) {
        const text = cleanText(table.innerText).toLowerCase();
        let score = 0;
        if (table.querySelectorAll("tbody tr").length > 0) score += 10;
        if (text.includes("rate") || text.includes("index") || text.includes("route")) score += 15;
        if (text.includes("weekly") || text.includes("w/w")) score += 10;
        return score;
    }
    function getHeaders(table, maxColumns) {
        const headerRows = Array.from(table.querySelectorAll("thead tr"));
        const headers = [];
        for (let i = 0; i < maxColumns; i += 1) {
            let header = "";
            for (let r = headerRows.length - 1; r >= 0; r -= 1) {
                const cells = Array.from(headerRows[r].querySelectorAll("th, td"));
                if (i < cells.length) {
                    const candidate = cleanText(cells[i].innerText);
                    if (candidate) { header = candidate; break; }
                }
            }
            headers.push(header || ("COL_" + (i + 1)));
        }
        return headers;
    }
    const output = {
        page_text: cleanText(document.body.innerText || ""),
        tables: []
    };
    for (const table of Array.from(document.querySelectorAll("table"))) {
        const rows = Array.from(table.querySelectorAll("tbody tr")).map(row => {
            return Array.from(row.querySelectorAll("th, td")).map(cell => cleanText(cell.innerText));
        }).filter(row => row.length > 0);
        if (!rows.length) continue;
        const maxColumns = Math.max(...rows.map(row => row.length));
        output.tables.push({ score: scoreTable(table), headers: getHeaders(table, maxColumns), rows: rows });
    }
    output.tables.sort((a, b) => b.score - a.score);
    return output;
    """
    return driver.execute_script(script)

def parse_summary_from_page_text(page_text):
    text = normalize_text(page_text)
    summary = {
        "latest_date": None,
        "overall_index": None,
        "weekly_change": None,
    }

    date_patterns = [
        r"\b(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})\b",
        r"\b(\d{1,2}\s+[A-Za-z]+\s+\d{4})\b",
        r"\b(\d{4}-\d{2}-\d{2})\b",
        r"\b(\d{2}/\d{2}/\d{4})\b",
    ]
    for pattern in date_patterns:
        match = re.search(pattern, text)
        if match:
            parsed = parse_date(match.group(1))
            if parsed:
                summary["latest_date"] = parsed.isoformat()
                break

    index_patterns = [
        r"(?:overall\s+index|tac\s+index|index)\s*[:\-]?\s*([0-9][0-9,]*(?:\.\d+)?)",
        r"(?:index\s+level)\s*[:\-]?\s*([0-9][0-9,]*(?:\.\d+)?)",
    ]
    for pattern in index_patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            summary["overall_index"] = safe_float(match.group(1))
            break

    weekly_patterns = [
        r"(?:weekly\s+change|week\s+on\s+week|w/w)\s*[:\-]?\s*([+\-]?[0-9][0-9,]*(?:\.\d+)?)\s*%",
        r"([+\-]?[0-9][0-9,]*(?:\.\d+)?)\s*%\s*(?:weekly|w/w)",
    ]
    for pattern in weekly_patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            summary["weekly_change"] = safe_float(match.group(1))
            break

    if not summary["latest_date"]:
        summary["latest_date"] = datetime.now().strftime("%Y-%m-%d")

    return summary

def parse_route_table(table_data):
    headers = table_data.get("headers", [])
    rows = table_data.get("rows", [])
    if not rows or len(headers) < 2:
        return pd.DataFrame()

    def find_idx(keywords):
        for i, h in enumerate(
            [normalize_text(h).lower() for h in headers]
        ):
            if any(k in h for k in keywords):
                return i
        return None

    route_idx = find_idx(
        ["route", "lane", "market", "origin", "sector", "index name"]
    ) or 0
    val_idx = find_idx(["index", "rate", "value", "price", "current"]) or 1
    w_idx = find_idx(["weekly", "week on week", "w/w", "1w"])
    m_idx = find_idx(["monthly", "month on month", "m/m", "1m"])

    output = []
    for raw_row in rows:
        row = list(raw_row) + [""] * (len(headers) - len(raw_row))
        if route_idx >= len(row) or val_idx >= len(row):
            continue
        route_name = normalize_text(row[route_idx])
        index_value = safe_float(row[val_idx])
        if not route_name or index_value is None:
            continue

        output.append(
            {
                "route_code": normalize_route_code(route_name),
                "route_name": route_name,
                "index_value": index_value,
                "weekly_change": (
                    safe_float(row[w_idx])
                    if w_idx is not None and w_idx < len(row)
                    else None
                ),
                "monthly_change": (
                    safe_float(row[m_idx])
                    if m_idx is not None and m_idx < len(row)
                    else None
                ),
            }
        )

    df = pd.DataFrame(output)
    return (
        df.drop_duplicates(subset=["route_code"], keep="last").reset_index(
            drop=True
        )
        if not df.empty
        else df
    )

def fetch_tac_index(driver_path="", headless=True):
    driver = None
    try:
        driver = get_driver(driver_path=driver_path, headless=headless)
        log("[FETCH] TAC Index 웹사이트 접속 중...")
        driver.get(TAC_INDEX_URL)
        wait_for_page_ready(driver)
        time.sleep(PAGE_LOAD_WAIT_SECONDS)

        payload = extract_tac_payload(driver)
        summary = parse_summary_from_page_text(
            payload.get("page_text", "")
        )

        best_df = pd.DataFrame()
        for tbl in payload.get("tables", []):
            df = parse_route_table(tbl)
            if len(df) > len(best_df):
                best_df = df

        if best_df.empty:
            raise RuntimeError(
                "TAC 사이트에서 노선별 테이블을 추출하지 못했습니다."
            )

        if summary.get("overall_index") is None:
            summary["overall_index"] = float(best_df["index_value"].mean())

        log(
            f"[FETCH] 수집 성공: routes={len(best_df)}, date={summary.get('latest_date')}"
        )
        return {"df": best_df, "summary": summary}
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

# =========================================================
# 6. Pipeline Execution
# =========================================================
def run_tac_sync(driver_path="", headless=True):
    with crawl_lock:
        started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            bundle = fetch_tac_index(
                driver_path=driver_path, headless=headless
            )
            snapshot_id, is_new = save_snapshot(bundle)
            finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            df = bundle.get("df", pd.DataFrame())
            summary = bundle.get("summary", {})

            save_crawl_audit(
                started_at=started_at,
                finished_at=finished_at,
                status="SUCCESS",
                message="NEW_SNAPSHOT" if is_new else "DUPLICATE_SNAPSHOT",
                snapshot_id=snapshot_id,
                route_count=len(df),
                data_date=summary.get("latest_date"),
            )
            return {
                "ok": True,
                "snapshot_id": snapshot_id,
                "is_new": is_new,
                "route_count": len(df),
            }
        except Exception as error:
            activate_file_logging()
            logger.exception(f"[ERROR] TAC 수집 실패: {error}")
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
    routes = load_route_history_df()

    route_list = []
    if not routes.empty:
        route_list = (
            routes[["route_code", "route_name"]]
            .drop_duplicates()
            .sort_values(["route_name", "route_code"])
            .to_dict(orient="records")
        )

    latest_summary = {}
    if not history.empty:
        r = history.iloc[-1]
        latest_summary = {
            "data_date": (
                r["data_date"].strftime("%Y-%m-%d")
                if pd.notna(r["data_date"])
                else None
            ),
            "captured_at": (
                r["captured_at"].strftime("%Y-%m-%d %H:%M:%S")
                if pd.notna(r["captured_at"])
                else None
            ),
            "overall_index": (
                float(r["overall_index"])
                if pd.notna(r["overall_index"])
                else None
            ),
            "weekly_change": (
                float(r["weekly_change"])
                if pd.notna(r["weekly_change"])
                else None
            ),
            "route_count": int(r["route_count"]),
        }

    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": {"name": "TAC Index", "url": TAC_INDEX_URL},
        "last_sync": get_last_sync_meta(),
        "latest_summary": latest_summary,
        "route_list": route_list,
        "overall_history": dataframe_to_records(
            history, date_columns=["data_date", "captured_at"]
        ),
        "route_history": dataframe_to_records(
            routes, date_columns=["data_date", "captured_at"]
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
        content = "<h1>TAC Index Dashboard Template (TAC_Index.html) Missing</h1>"
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

@app.route("/tac_index_data.json")
def serve_json():
    return send_from_directory(
        BASE_DIR, JSON_PATH.name, mimetype="application/json"
    )

# =========================================================
# 8. Main Entrypoint
# =========================================================
def run_cron_job(args):
    log("=" * 60)
    log("[CRON] TAC Index Dashboard 갱신 시작")
    log("=" * 60)
    result = run_tac_sync(
        driver_path=args.driver_path, headless=not args.show_browser
    )
    build_static_json()
    generate_static_html()
    if result.get("ok"):
        log("[CRON] TAC Index Dashboard 갱신 완료")
    else:
        log(
            "[WARN] 최신 TAC 수집 실패. 기존 DB 데이터 기반으로 정적 파일을 빌드했습니다."
        )
    log("=" * 60)
    return result

def parse_args():
    parser = argparse.ArgumentParser(
        description="TAC Air Freight Rate Index Dashboard"
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

    # DB 초기화 (없으면 파일/테이블 자동 생성)
    conn = get_db_conn()
    conn.close()

    result = run_cron_job(args)

    if args.cron:
        if not result.get("ok") and get_total_snapshot_count() == 0:
            log("[ERROR] TAC 수집 실패 및 기존 Snapshot 데이터 없음")
            sys.exit(1)
        log("[SYSTEM] TAC batch process completed. Process exiting.")
        sys.exit(0)

    app.run(
        host=args.host, port=args.port, debug=False, use_reloader=False
    )

if __name__ == "__main__":
    main()
