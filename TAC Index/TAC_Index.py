# -*- coding: utf-8 -*-
"""
TAC Air Freight Rate Index Static Dashboard
===========================================

로컬 실행 (Flask 서버 띄우기):
    python TAC_Index/TAC_Index.py --local

GitHub Actions / GitHub Pages / run_all.py 배치 실행:
    python TAC_Index/TAC_Index.py
    python TAC_Index/TAC_Index.py --cron

필수 패키지:
    pip install pandas selenium webdriver-manager flask

생성 파일:
    - TAC_Index.db
    - tac_index_data.json
    - index.html
    - tac_index_crawl.log
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
from datetime import datetime, timedelta
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
LOG_PATH = BASE_DIR / "tac_index_crawl.log"

TAC_INDEX_URL = "https://www.tacindex.com/"

OVERLAP_DAYS = 14
PAGE_LOAD_WAIT_SECONDS = 10
SELENIUM_WAIT_SECONDS = 30
DEFAULT_RANGE_DAYS = 365

app = Flask(__name__)
crawl_lock = Lock()

# =========================================================
# 2. Logging
# =========================================================
logger = logging.getLogger("tac_index")
logger.setLevel(logging.INFO)
logger.handlers.clear()

formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s"
)

stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

def activate_file_logging():
    """파일 로그 기록을 활성화한다."""
    if any(
        isinstance(handler, logging.FileHandler)
        for handler in logger.handlers
    ):
        return

    try:
        file_handler = logging.FileHandler(
            LOG_PATH,
            mode="a",
            encoding="utf-8",
        )
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
    """공백·개행·탭을 하나의 공백으로 정리한다."""
    return " ".join(str(value or "").strip().split())

def normalize_route_code(value):
    """노선 코드 또는 항목명을 DB 키로 사용 가능한 형태로 정리한다."""
    return normalize_text(value).upper()

def safe_float(value):
    """%, 쉼표, 통화 기호, 괄호형 음수 등이 포함된 값을 float으로 변환한다."""
    text = str(value or "").strip()

    if not text:
        return None

    if text.lower() in {
        "-",
        "n/a",
        "na",
        "none",
        "null",
        "--",
    }:
        return None

    is_negative = (
        text.startswith("-")
        or (
            text.startswith("(")
            and text.endswith(")")
        )
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
    """TAC 사이트에서 표현될 수 있는 날짜 형식을 ISO 날짜로 정규화한다."""
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
            return datetime.strptime(
                text,
                date_format,
            ).date()
        except ValueError:
            continue

    parsed = pd.to_datetime(
        text,
        errors="coerce",
        dayfirst=True,
    )

    if pd.isna(parsed):
        return None

    return parsed.date()

def atomic_write_text(path, text):
    """임시 파일 작성 후 원자적으로 교체한다."""
    temporary_path = path.with_suffix(
        path.suffix + ".tmp"
    )

    temporary_path.write_text(
        text,
        encoding="utf-8",
    )

    temporary_path.replace(path)

def dataframe_to_records(dataframe, date_columns=None):
    """DataFrame을 JSON 저장용 records 구조로 변환한다."""
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

    output = output.replace(
        [float("inf"), float("-inf")],
        pd.NA,
    )

    output = output.where(
        pd.notnull(output),
        None,
    )

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
    """SQLite 연결 및 테이블 초기화."""
    connection = sqlite3.connect(
        DB_PATH,
        timeout=30,
    )

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
            FOREIGN KEY (snapshot_id)
                REFERENCES tac_master(snapshot_id)
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
    """저장된 TAC 스냅샷 수를 반환한다."""
    connection = get_db_conn()

    try:
        row = connection.execute(
            "SELECT COUNT(*) FROM tac_master"
        ).fetchone()

        return int(row[0] if row else 0)

    finally:
        connection.close()

def get_last_sync_meta():
    """가장 최근 크롤링 실행 결과를 반환한다."""
    connection = get_db_conn()

    try:
        row = connection.execute(
            """
            SELECT
                started_at,
                finished_at,
                status,
                message,
                snapshot_id,
                route_count,
                data_date
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
    """수집 실행 성공·실패 이력을 저장한다."""
    connection = get_db_conn()

    try:
        connection.execute(
            """
            INSERT INTO crawl_audit (
                started_at,
                finished_at,
                status,
                message,
                snapshot_id,
                route_count,
                data_date
            )
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
    """고유 Hash를 생성해 동일 데이터의 중복 스냅샷 저장을 방지한다."""
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

    working = working[
        required_columns
    ].sort_values(
        ["route_code", "route_name"]
    ).reset_index(drop=True)

    payload = {
        "latest_date": str(summary.get("latest_date", "")),
        "overall_index": summary.get("overall_index"),
        "weekly_change": summary.get("weekly_change"),
        "routes": dataframe_to_records(working),
    }

    text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    return hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest()

def save_snapshot(bundle):
    """신규 TAC 데이터를 master/detail 테이블에 저장한다."""
    dataframe = bundle.get("df", pd.DataFrame()).copy()
    summary = bundle.get("summary", {}).copy()

    if dataframe.empty:
        raise RuntimeError(
            "저장할 TAC 노선 데이터가 없습니다."
        )

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

    dataframe["route_code"] = dataframe[
        "route_code"
    ].apply(normalize_route_code)

    dataframe["route_name"] = dataframe[
        "route_name"
    ].apply(normalize_text)

    dataframe["index_value"] = pd.to_numeric(
        dataframe["index_value"],
        errors="coerce",
    )

    dataframe["weekly_change"] = pd.to_numeric(
        dataframe["weekly_change"],
        errors="coerce",
    )

    dataframe["monthly_change"] = pd.to_numeric(
        dataframe["monthly_change"],
        errors="coerce",
    )

    dataframe = dataframe.dropna(
        subset=["route_code", "route_name", "index_value"]
    )

    dataframe = dataframe[
        dataframe["route_code"].str.len() > 0
    ]

    dataframe = dataframe.drop_duplicates(
        subset=["route_code"],
        keep="last",
    ).reset_index(drop=True)

    if dataframe.empty:
        raise RuntimeError(
            "정제 후 저장 가능한 TAC 노선 데이터가 없습니다."
        )

    latest_date = parse_date(
        summary.get("latest_date")
    )

    if latest_date is None:
        latest_date = datetime.now().date()

    summary["latest_date"] = latest_date.isoformat()

    data_hash = make_data_hash(
        dataframe,
        summary,
    )

    connection = get_db_conn()

    try:
        cursor = connection.cursor()

        existing = cursor.execute(
            """
            SELECT snapshot_id
            FROM tac_master
            WHERE data_hash = ?
            """,
            (data_hash,),
        ).fetchone()

        if existing:
            log(
                "[DB] 동일 TAC 데이터가 이미 저장되어 있습니다. "
                f"snapshot_id={existing[0]}"
            )
            return int(existing[0]), False

        captured_at = datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        cursor.execute(
            """
            INSERT INTO tac_master (
                captured_at,
                data_date,
                data_hash,
                overall_index,
                weekly_change,
                route_count
            )
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
            INSERT INTO tac_detail (
                snapshot_id,
                route_code,
                route_name,
                index_value,
                weekly_change,
                monthly_change
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            insert_rows,
        )

        connection.commit()

        log(
            "[DB] 신규 TAC Snapshot 저장 완료: "
            f"snapshot_id={snapshot_id}, "
            f"routes={len(dataframe):,}"
        )

        return int(snapshot_id), True

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()

def load_history_df():
    """전체 지수 기준의 스냅샷 이력을 읽는다."""
    connection = get_db_conn()

    try:
        dataframe = pd.read_sql_query(
            """
            SELECT
                snapshot_id,
                captured_at,
                data_date,
                overall_index,
                weekly_change,
                route_count
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
        dataframe["data_date"],
        errors="coerce",
    )

    dataframe["captured_at"] = pd.to_datetime(
        dataframe["captured_at"],
        errors="coerce",
    )

    dataframe["overall_index"] = pd.to_numeric(
        dataframe["overall_index"],
        errors="coerce",
    )

    dataframe["weekly_change"] = pd.to_numeric(
        dataframe["weekly_change"],
        errors="coerce",
    )

    dataframe = dataframe.dropna(
        subset=["data_date"]
    )

    dataframe = dataframe.sort_values(
        ["data_date", "snapshot_id"]
    ).drop_duplicates(
        subset=["data_date"],
        keep="last",
    )

    return dataframe.reset_index(drop=True)

def load_route_history_df():
    """노선별 지수 이력을 Snapshot 날짜와 결합하여 읽는다."""
    connection = get_db_conn()

    try:
        dataframe = pd.read_sql_query(
            """
            SELECT
                m.snapshot_id,
                m.data_date,
                m.captured_at,
                d.route_code,
                d.route_name,
                d.index_value,
                d.weekly_change,
                d.monthly_change
            FROM tac_master AS m
            INNER JOIN tac_detail AS d
                ON m.snapshot_id = d.snapshot_id
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
        dataframe["data_date"],
        errors="coerce",
    )

    dataframe["captured_at"] = pd.to_datetime(
        dataframe["captured_at"],
        errors="coerce",
    )

    for column in [
        "index_value",
        "weekly_change",
        "monthly_change",
    ]:
        dataframe[column] = pd.to_numeric(
            dataframe[column],
            errors="coerce",
        )

    dataframe["route_code"] = dataframe[
        "route_code"
    ].apply(normalize_route_code)

    dataframe["route_name"] = dataframe[
        "route_name"
    ].apply(normalize_text)

    dataframe = dataframe.dropna(
        subset=[
            "data_date",
            "route_code",
            "index_value",
        ]
    )

    dataframe = dataframe.sort_values(
        ["data_date", "snapshot_id"]
    ).drop_duplicates(
        subset=["data_date", "route_code"],
        keep="last",
    )

    return dataframe.reset_index(drop=True)

# =========================================================
# 5. Selenium Driver and TAC Data Collection
# =========================================================
def get_driver(driver_path="", headless=True):
    """Chrome Selenium Driver를 생성한다."""
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

    chrome_binary = os.getenv(
        "CHROME_BINARY",
        "",
    ).strip()

    if chrome_binary:
        options.binary_location = chrome_binary

    if driver_path:
        return webdriver.Chrome(
            service=Service(driver_path),
            options=options,
        )

    try:
        return webdriver.Chrome(options=options)

    except WebDriverException as first_error:
        if not HAS_WEBDRIVER_MANAGER:
            raise first_error

        log(
            "[DRIVER] Selenium Manager 실패. "
            "webdriver-manager fallback 실행"
        )

        installed_driver = ChromeDriverManager().install()

        return webdriver.Chrome(
            service=Service(installed_driver),
            options=options,
        )

def wait_for_page_ready(driver):
    """문서 로드와 기본 텍스트 렌더링까지 대기한다."""
    wait = WebDriverWait(
        driver,
        SELENIUM_WAIT_SECONDS,
    )

    wait.until(
        lambda current_driver: current_driver.execute_script(
            "return document.readyState"
        ) == "complete"
    )

    wait.until(
        lambda current_driver: len(
            current_driver.execute_script(
                "return (document.body.innerText || '').trim()"
            )
        ) > 50
    )

def extract_tac_payload(driver):
    """TAC 웹페이지 DOM에서 Summary와 Route 테이블 후보를 추출한다."""
    script = r"""
    function cleanText(value) {
        return String(value || "")
            .replace(/\s+/g, " ")
            .trim();
    }

    function parseNumber(value) {
        const text = cleanText(value);

        if (!text) return null;

        const negative =
            text.startsWith("-") ||
            (text.startsWith("(") && text.endsWith(")"));

        const cleaned = text
            .replace(/,/g, "")
            .replace(/[^0-9.]/g, "");

        if (!cleaned) return null;

        const number = Number(cleaned);

        if (!Number.isFinite(number)) return null;

        return negative ? -number : number;
    }

    function scoreTable(table) {
        const text = cleanText(table.innerText).toLowerCase();
        const headers = Array.from(
            table.querySelectorAll("thead th, thead td")
        ).map(cell => cleanText(cell.innerText).toLowerCase());

        const rowCount = table.querySelectorAll("tbody tr").length;
        let score = 0;

        if (rowCount > 0) score += 10;
        if (rowCount >= 3) score += 10;
        if (headers.length >= 2) score += 5;

        if (
            text.includes("rate") ||
            text.includes("index") ||
            text.includes("route")
        ) score += 15;

        if (
            text.includes("weekly") ||
            text.includes("week on week") ||
            text.includes("w/w")
        ) score += 10;

        if (
            text.includes("monthly") ||
            text.includes("month on month") ||
            text.includes("m/m")
        ) score += 5;

        return score;
    }

    function getHeaders(table, maxColumns) {
        const headerRows = Array.from(
            table.querySelectorAll("thead tr")
        );

        const headers = [];

        for (let i = 0; i < maxColumns; i += 1) {
            let header = "";

            for (let r = headerRows.length - 1; r >= 0; r -= 1) {
                const cells = Array.from(
                    headerRows[r].querySelectorAll("th, td")
                );

                if (i < cells.length) {
                    const candidate = cleanText(cells[i].innerText);

                    if (candidate) {
                        header = candidate;
                        break;
                    }
                }
            }

            headers.push(header || ("COL_" + (i + 1)));
        }

        return headers;
    }

    const output = {
        page_title: document.title || "",
        page_text: cleanText(document.body.innerText || ""),
        tables: []
    };

    const tables = Array.from(
        document.querySelectorAll("table")
    );

    for (const table of tables) {
        const rows = Array.from(
            table.querySelectorAll("tbody tr")
        ).map(row => {
            return Array.from(
                row.querySelectorAll("th, td")
            ).map(cell => cleanText(cell.innerText));
        }).filter(row => row.length > 0);

        if (!rows.length) continue;

        const maxColumns = Math.max(
            ...rows.map(row => row.length)
        );

        output.tables.push({
            score: scoreTable(table),
            headers: getHeaders(table, maxColumns),
            rows: rows
        });
    }

    output.tables.sort((a, b) => b.score - a.score);

    return output;
    """

    return driver.execute_script(script)

def parse_summary_from_page_text(page_text):
    """페이지 텍스트에서 최신 발표일, 전체 지수, 주간 변동률을 찾는다."""
    text = normalize_text(page_text)

    summary = {
        "latest_date": None,
        "overall_index": None,
        "weekly_change": None,
    }

    date_patterns = [
        r"\b(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})\b",
        r"\b(\d{1,2}\s+[A-Za-z]+\s+\d{4})\b",
        r"\b(\d{4}-\d{2}-\d{2})\
