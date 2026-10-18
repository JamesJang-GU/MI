# -*- coding: utf-8 -*-
"""
TAC Air Freight Rate Index Static Dashboard
===========================================

실행 모드:
1) GitHub Actions / run_all.py (배치 모드 - 기본값):
    python TAC_Index/TAC_Index.py
    python TAC_Index/TAC_Index.py --cron

2) 로컬 개발 (Flask 웹서버 구동):
    python TAC_Index/TAC_Index.py --local

특징:
    - TAC_Index.db 파일이 없으면 자동 생성 (CREATE TABLE IF NOT EXISTS)
    - 수집 후 tac_index_data.json 및 index.html 정적 대시보드 자동 빌드
    - 배치 실행 시 프로세스 정상 종료 (sys.exit(0))
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
# 4. SQLite Database (없으면 자동 생성)
# =========================================================
def get_db_conn():
    """SQLite 연결 및 테이블 초기화 (DB 파일 부재 시 자동 생성)."""
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
    """
    페이지 전체 텍스트에서 최신 발표일, 전체 지수, 주간 변동률을 찾는다.
    """
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
        (
            r"(?:overall\s+index|tac\s+index|index)"
            r"\s*[:\-]?\s*"
            r"([0-9][0-9,]*(?:\.\d+)?)"
        ),
        (
            r"(?:index\s+level)"
            r"\s*[:\-]?\s*"
            r"([0-9][0-9,]*(?:\.\d+)?)"
        ),
    ]

    for pattern in index_patterns:
        match = re.search(
            pattern,
            text,
            flags=re.IGNORECASE,
        )

        if match:
            summary["overall_index"] = safe_float(
                match.group(1)
            )
            break

    weekly_patterns = [
        (
            r"(?:weekly\s+change|week\s+on\s+week|w/w)"
            r"\s*[:\-]?\s*"
            r"([+\-]?[0-9][0-9,]*(?:\.\d+)?)\s*%"
        ),
        (
            r"([+\-]?[0-9][0-9,]*(?:\.\d+)?)\s*%"
            r"\s*(?:weekly|w/w)"
        ),
    ]

    for pattern in weekly_patterns:
        match = re.search(
            pattern,
            text,
            flags=re.IGNORECASE,
        )

        if match:
            summary["weekly_change"] = safe_float(
                match.group(1)
            )
            break

    if not summary["latest_date"]:
        summary["latest_date"] = datetime.now().strftime(
            "%Y-%m-%d"
        )

    return summary

def find_header_index(headers, keywords):
    """헤더 목록에서 키워드와 일치하는 첫 번째 컬럼 위치를 찾는다."""
    normalized_headers = [
        normalize_text(header).lower()
        for header in headers
    ]

    for index, header in enumerate(normalized_headers):
        if any(
            keyword in header
            for keyword in keywords
        ):
            return index

    return None

def parse_route_table(table_data):
    """하나의 테이블 후보를 TAC 노선 데이터 DataFrame으로 변환한다."""
    headers = table_data.get("headers", [])
    rows = table_data.get("rows", [])

    if not rows or len(headers) < 2:
        return pd.DataFrame()

    route_index = find_header_index(
        headers,
        [
            "route",
            "lane",
            "market",
            "origin",
            "sector",
            "index name",
        ],
    )

    value_index = find_header_index(
        headers,
        [
            "index",
            "rate",
            "value",
            "price",
            "current",
        ],
    )

    weekly_index = find_header_index(
        headers,
        [
            "weekly",
            "week on week",
            "w/w",
            "1w",
        ],
    )

    monthly_index = find_header_index(
        headers,
        [
            "monthly",
            "month on month",
            "m/m",
            "1m",
        ],
    )

    if route_index is None:
        route_index = 0

    if value_index is None:
        value_index = 1 if len(headers) >= 2 else None

    if value_index is None:
        return pd.DataFrame()

    output = []

    for raw_row in rows:
        row = list(raw_row) + [""] * (
            len(headers) - len(raw_row)
        )

        if route_index >= len(row) or value_index >= len(row):
            continue

        route_name = normalize_text(row[route_index])
        index_value = safe_float(row[value_index])

        if not route_name or index_value is None:
            continue

        weekly_change = (
            safe_float(row[weekly_index])
            if weekly_index is not None
            and weekly_index < len(row)
            else None
        )

        monthly_change = (
            safe_float(row[monthly_index])
            if monthly_index is not None
            and monthly_index < len(row)
            else None
        )

        output.append(
            {
                "route_code": normalize_route_code(route_name),
                "route_name": route_name,
                "index_value": index_value,
                "weekly_change": weekly_change,
                "monthly_change": monthly_change,
            }
        )

    dataframe = pd.DataFrame(output)

    if dataframe.empty:
        return dataframe

    return dataframe.drop_duplicates(
        subset=["route_code"],
        keep="last",
    ).reset_index(drop=True)

def choose_best_route_dataframe(tables):
    """가장 많은 유효 Route 데이터를 갖는 테이블을 선택한다."""
    best_dataframe = pd.DataFrame()

    for table in tables:
        dataframe = parse_route_table(table)

        if len(dataframe) > len(best_dataframe):
            best_dataframe = dataframe

    return best_dataframe

def fetch_tac_index(
    driver_path="",
    headless=True,
):
    """TAC 사이트에서 최신 지수와 노선별 데이터를 수집한다."""
    driver = None

    try:
        driver = get_driver(
            driver_path=driver_path,
            headless=headless,
        )

        log("[FETCH] TAC Index 웹사이트 접속 중")

        driver.get(TAC_INDEX_URL)

        wait_for_page_ready(driver)

        time.sleep(PAGE_LOAD_WAIT_SECONDS)

        payload = extract_tac_payload(driver)

        page_text = payload.get("page_text", "")
        tables = payload.get("tables", [])

        summary = parse_summary_from_page_text(page_text)

        dataframe = choose_best_route_dataframe(tables)

        if dataframe.empty:
            raise RuntimeError(
                "TAC 사이트에서 유효한 노선별 지수 테이블을 "
                "추출하지 못했습니다."
            )

        if summary.get("overall_index") is None:
            summary["overall_index"] = float(
                dataframe["index_value"].mean()
            )

            log(
                "[WARN] 페이지에서 Overall Index를 직접 찾지 못해 "
                "노선 지수 평균을 fallback으로 사용합니다."
            )

        log(
            "[FETCH] TAC 데이터 수집 완료: "
            f"routes={len(dataframe):,}, "
            f"data_date={summary.get('latest_date')}"
        )

        return {
            "df": dataframe,
            "summary": summary,
        }

    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

# =========================================================
# 6. Crawl and Data Refresh
# =========================================================
def run_tac_sync(
    driver_path="",
    headless=True,
):
    """TAC 데이터를 수집하고 신규 Snapshot을 DB에 저장한다."""
    with crawl_lock:
        started_at = datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        try:
            bundle = fetch_tac_index(
                driver_path=driver_path,
                headless=headless,
            )

            snapshot_id, is_new = save_snapshot(bundle)

            finished_at = datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            )

            dataframe = bundle.get("df", pd.DataFrame())
            summary = bundle.get("summary", {})

            save_crawl_audit(
                started_at=started_at,
                finished_at=finished_at,
                status="SUCCESS",
                message=(
                    "NEW_SNAPSHOT"
                    if is_new
                    else "DUPLICATE_SNAPSHOT"
                ),
                snapshot_id=snapshot_id,
                route_count=len(dataframe),
                data_date=summary.get("latest_date"),
            )

            log(
                "[SUCCESS] TAC 수집 완료: "
                f"snapshot_id={snapshot_id}, "
                f"new={is_new}, "
                f"routes={len(dataframe):,}"
            )

            return {
                "ok": True,
                "snapshot_id": snapshot_id,
                "is_new": is_new,
                "route_count": len(dataframe),
                "data_date": summary.get("latest_date"),
            }

        except Exception as error:
            activate_file_logging()

            logger.exception(
                f"[ERROR] TAC 수집 실패: {error}"
            )

            finished_at = datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            )

            try:
                save_crawl_audit(
                    started_at=started_at,
                    finished_at=finished_at,
                    status="ERROR",
                    message=str(error),
                    snapshot_id=None,
                    route_count=0,
                    data_date=None,
                )
            except Exception as audit_error:
                logger.error(
                    "[ERROR] 수집 실패 로그 저장 실패: "
                    f"{audit_error}"
                )

            return {
                "ok": False,
                "error": str(error),
            }

# =========================================================
# 7. Static JSON Export
# =========================================================
def build_static_json():
    """SQLite의 누적 Snapshot 데이터를 GitHub Pages용 JSON으로 생성한다."""
    history = load_history_df()
    routes = load_route_history_df()

    route_list = []

    if not routes.empty:
        route_list = (
            routes[
                ["route_code", "route_name"]
            ]
            .drop_duplicates()
            .sort_values(
                ["route_name", "route_code"]
            )
            .to_dict(orient="records")
        )

    latest_summary = {}

    if not history.empty:
        latest_row = history.iloc[-1]

        latest_summary = {
            "data_date": (
                latest_row["data_date"]
                .strftime("%Y-%m-%d")
                if pd.notna(latest_row["data_date"])
                else None
            ),
            "captured_at": (
                latest_row["captured_at"]
                .strftime("%Y-%m-%d %H:%M:%S")
                if pd.notna(latest_row["captured_at"])
                else None
            ),
            "overall_index": (
                float(latest_row["overall_index"])
                if pd.notna(latest_row["overall_index"])
                else None
            ),
            "weekly_change": (
                float(latest_row["weekly_change"])
                if pd.notna(latest_row["weekly_change"])
                else None
            ),
            "route_count": int(
                latest_row["route_count"]
            ),
        }

    payload = {
        "generated_at": datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        "source": {
            "name": "TAC Index",
            "url": TAC_INDEX_URL,
        },
        "last_sync": get_last_sync_meta(),
        "latest_summary": latest_summary,
        "route_list": route_list,
        "overall_history": dataframe_to_records(
            history,
            date_columns=["data_date", "captured_at"],
        ),
        "route_history": dataframe_to_records(
            routes,
            date_columns=["data_date", "captured_at"],
        ),
        "record_count": {
            "snapshots": int(len(history)),
            "route_points": int(len(routes)),
            "routes": int(len(route_list)),
        },
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

# =========================================================
# 8. Static HTML
# =========================================================
STATIC_HTML = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">

<title>TAC Air Freight Rate Index Dashboard</title>

<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>

<style>
:root {
    --navy: #12355b;
    --blue: #0284c7;
    --teal: #0f766e;
    --red: #b91c1c;
    --bg: #f3f6fb;
    --card: #ffffff;
    --line: #d9e2ec;
    --text: #1f2937;
    --muted: #64748b;
    --warning-bg: #fff8e1;
    --warning-text: #7a5a00;
}

* {
    box-sizing: border-box;
}

body {
    margin: 0;
    font-family: "Segoe UI", Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
}

.wrap {
    max-width: 1560px;
    margin: 0 auto;
    padding: 24px;
}

.hero {
    background: linear-gradient(135deg, var(--navy), var(--teal));
    color: #ffffff;
    border-radius: 18px;
    padding: 28px;
    margin-bottom: 20px;
}

.hero h1 {
    margin: 0 0 10px 0;
    font-size: 30px;
}

.hero-meta {
    font-size: 13px;
    line-height: 1.8;
    opacity: 0.96;
}

.hero-meta a {
    color: inherit;
    text-decoration: underline;
}

.layout {
    display: grid;
    grid-template-columns: 300px minmax(0, 1fr);
    gap: 20px;
    align-items: start;
}

.sidebar,
.panel {
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 16px;
    box-shadow: 0 4px 14px rgba(18, 53, 91, 0.06);
}

.sidebar {
    padding: 18px;
    position: sticky;
    top: 18px;
}

.panel {
    padding: 18px;
    margin-bottom: 18px;
}

.sidebar-section {
    border-bottom: 1px solid #e5edf5;
    margin-bottom: 18px;
    padding-bottom: 18px;
}

.sidebar-section:last-child {
    border-bottom: none;
    margin-bottom: 0;
    padding-bottom: 0;
}

.sidebar-title {
    margin-bottom: 10px;
    color: var(--navy);
    font-size: 13px;
    font-weight: 800;
    letter-spacing: 0.4px;
    text-transform: uppercase;
}

.field {
    margin-bottom: 14px;
}

label {
    display: block;
    margin-bottom: 6px;
    color: var(--muted);
    font-size: 12px;
    font-weight: 700;
}

select,
input[type="date"] {
    width: 100%;
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 10px;
    background: #ffffff;
}

select[multiple] {
    min-height: 240px;
}

button {
    border: none;
    border-radius: 10px;
    padding: 10px 12px;
    cursor: pointer;
    background: var(--navy);
    color: #ffffff;
    font-weight: 700;
}

button:hover {
    opacity: 0.9;
}

.button-row {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
}

.button-secondary {
    background: #475569;
}

.button-csv {
    background: #2563eb;
}

.button-png {
    background: var(--teal);
}

.kpi-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 14px;
    margin-bottom: 18px;
}

.kpi {
    background: linear-gradient(180deg, #ffffff, #f8fbff);
    border: 1px solid var(--line);
    border-left: 6px solid var(--blue);
    border-radius: 14px;
    padding: 16px;
}

.kpi-label {
    color: var(--muted);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.4px;
    text-transform: uppercase;
}

.kpi-value {
    margin-top: 8px;
    color: var(--navy);
    font-size: 23px;
    font-weight: 800;
    word-break: break-word;
}

.section-title {
    margin: 0 0 14px 0;
    color: var(--navy);
    font-size: 18px;
}

.section-note {
    color: var(--muted);
    font-size: 13px;
    line-height: 1.6;
}

.toolbar {
    display: flex;
    gap: 12px;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    margin-bottom: 12px;
}

.chart {
    min-height: 500px;
    width: 100%;
}

.table-wrap {
    overflow-x: auto;
}

table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
}

th,
td {
    border: 1px solid #e5e7eb;
    padding: 10px 12px;
    text-align: left;
    white-space: nowrap;
}

th {
    background: #f1f5f9;
    color: #334155;
}

.positive {
    color: #15803d;
    font-weight: 800;
}

.negative {
    color: var(--red);
    font-weight: 800;
}

.warning {
    display: none;
    margin-bottom: 18px;
    padding: 14px;
    border: 1px solid #f5d76e;
    border-radius: 12px;
    background: var(--warning-bg);
    color: var(--warning-text);
}

.loading,
.empty {
    padding: 50px;
    color: var(--muted);
    text-align: center;
    font-weight: 700;
}

.hidden {
    display: none;
}

@media (max-width: 1150px) {
    .layout {
        grid-template-columns: 1fr;
    }

    .sidebar {
        position: static;
    }
}

@media (max-width: 900px) {
    .kpi-grid {
        grid-template-columns: repeat(2, 1fr);
    }
}

@media (max-width: 600px) {
    .wrap {
        padding: 14px;
    }

    .hero {
        padding: 20px;
    }

    .hero h1 {
        font-size: 24px;
    }

    .kpi-grid {
        grid-template-columns: 1fr;
    }
}
</style>
</head>

<body>
<div class="wrap">

    <div class="hero">
        <h1>✈️ TAC Air Freight Rate Index</h1>

        <div class="hero-meta">
            <div>
                <strong>Source:</strong>
                <a href="https://www.tacindex.com/"
                   target="_blank"
                   rel="noopener noreferrer">
                    TAC Index
                </a>
            </div>

            <div>
                <strong>Latest Data Date:</strong>
                <span id="dataDate">-</span>
                &nbsp;|&nbsp;
                <strong>Last Sync:</strong>
                <span id="lastSync">-</span>
                &nbsp;|&nbsp;
                <strong>Generated:</strong>
                <span id="generatedAt">-</span>
            </div>
        </div>
    </div>

    <div id="warning" class="warning"></div>

    <div id="loading" class="panel loading">
        GitHub Pages 데이터 파일을 불러오는 중입니다.
    </div>

    <div id="dashboard" class="hidden">
        <div class="layout">

            <aside class="sidebar">
                <div class="sidebar-section">
                    <div class="sidebar-title">Period</div>

                    <div class="field">
                        <label for="startDate">시작일</label>
                        <input id="startDate"
                               type="date"
                               onchange="renderDashboard()">
                    </div>

                    <div class="field">
                        <label for="endDate">종료일</label>
                        <input id="endDate"
                               type="date"
                               onchange="renderDashboard()">
                    </div>

                    <div class="field">
                        <label for="rangeSelect">기간 빠른 선택</label>
                        <select id="rangeSelect"
                                onchange="applyRangeAndRender()">
                            <option value="0">전체 기간</option>
                            <option value="90">최근 3개월</option>
                            <option value="180">최근 6개월</option>
                            <option value="365" selected>최근 1년</option>
                            <option value="730">최근 2년</option>
                            <option value="1825">최근 5년</option>
                        </select>
                    </div>
                </div>

                <div class="sidebar-section">
                    <div class="sidebar-title">Routes</div>

                    <div class="field">
                        <label for="routeSelect">노선 복수 선택</label>
                        <select id="routeSelect"
                                multiple
                                onchange="renderDashboard()">
                        </select>
                    </div>

                    <div class="button-row">
                        <button onclick="selectAllRoutes()">
                            전체 선택
                        </button>

                        <button class="button-secondary"
                                onclick="clearRoutes()">
                            선택 해제
                        </button>
                    </div>
                </div>

                <div class="sidebar-section">
                    <div class="sidebar-title">Download</div>

                    <div class="button-row">
                        <button class="button-csv"
                                onclick="downloadCsv()">
                            CSV 다운로드
                        </button>

                        <button class="button-png"
                                onclick="downloadChartPng()">
                            차트 PNG
                        </button>
                    </div>
                </div>
            </aside>

            <main>
                <div class="kpi-grid">
                    <div class="kpi">
                        <div class="kpi-label">Latest Overall Index</div>
                        <div id="overallIndexKpi"
                             class="kpi-value">-</div>
                    </div>

                    <div class="kpi">
                        <div class="kpi-label">Weekly Change</div>
                        <div id="weeklyChangeKpi"
                             class="kpi-value">-</div>
                    </div>

                    <div class="kpi">
                        <div class="kpi-label">Selected Routes</div>
                        <div id="routeCountKpi"
                             class="kpi-value">-</div>
                    </div>

                    <div class="kpi">
                        <div class="kpi-label">Displayed Records</div>
                        <div id="recordCountKpi"
                             class="kpi-value">-</div>
                    </div>
                </div>

                <div class="panel">
                    <div class="toolbar">
                        <div>
                            <h3 class="section-title">
                                Overall TAC Index Trend
                            </h3>
                            <div class="section-note">
                                TAC 사이트에서 추출한 전체 지수의
                                누적 Snapshot 추이입니다.
                            </div>
                        </div>
                    </div>

                    <div id="overallChart" class="chart"></div>
                </div>

                <div class="panel">
                    <div class="toolbar">
                        <div>
                            <h3 class="section-title">
                                Route Index Comparison
                            </h3>
                            <div class="section-note">
                                선택된 노선의 항공운임 지수 추이입니다.
                            </div>
                        </div>
                    </div>

                    <div id="routeChart" class="chart"></div>
                </div>

                <div class="panel">
                    <h3 class="section-title">
                        Latest Route Breakdown
                    </h3>

                    <div id="routeTable" class="table-wrap"></div>
                </div>
            </main>
        </div>
    </div>
</div>

<script>
let dashboardData = null;
let filteredRouteRecords = [];
let filteredOverallRecords = [];

const COLORS = [
    "#1d4ed8",
    "#0f766e",
    "#b91c1c",
    "#7c3aed",
    "#c2410c",
    "#0369a1",
    "#15803d",
    "#be123c",
    "#9333ea",
    "#0891b2"
];

function showWarning(message) {
    const warning = document.getElementById("warning");
    warning.textContent = message;
    warning.style.display = "block";
}

function hideWarning() {
    document.getElementById("warning").style.display = "none";
}

function numberFormat(value, digits = 2) {
    const number = Number(value);

    if (!Number.isFinite(number)) {
        return "-";
    }

    return number.toLocaleString(
        undefined,
        {
            minimumFractionDigits: digits,
            maximumFractionDigits: digits
        }
    );
}

function percentFormat(value) {
    const number = Number(value);

    if (!Number.isFinite(number)) {
        return "-";
    }

    const prefix = number > 0 ? "+" : "";

    return prefix + numberFormat(number, 2) + "%";
}

function getSelectedRoutes() {
    const select = document.getElementById("routeSelect");

    return Array.from(select.selectedOptions).map(
        option => option.value
    );
}

function selectAllRoutes() {
    const select = document.getElementById("routeSelect");

    Array.from(select.options).forEach(option => {
        option.selected = true;
    });

    renderDashboard();
}

function clearRoutes() {
    const select = document.getElementById("routeSelect");

    Array.from(select.options).forEach(option => {
        option.selected = false;
    });

    renderDashboard();
}

function getDateBounds() {
    const dates = dashboardData.route_history
        .map(item => item.data_date)
        .filter(Boolean)
        .sort();

    if (!dates.length) {
        return {
            min: "",
            max: ""
        };
    }

    return {
        min: dates[0],
        max: dates[dates.length - 1]
    };
}

function populateRoutes() {
    const select = document.getElementById("routeSelect");

    select.innerHTML = "";

    const routeList = dashboardData.route_list || [];

    routeList.forEach(route => {
        const label = route.route_name
            ? route.route_name
            : route.route_code;

        const option = new Option(
            label,
            route.route_code
        );

        option.selected = true;

        select.add(option);
    });
}

function setInitialDates() {
    const bounds = getDateBounds();
    const startDate = document.getElementById("startDate");
    const endDate = document.getElementById("endDate");

    startDate.min = bounds.min;
    startDate.max = bounds.max;
    endDate.min = bounds.min;
    endDate.max = bounds.max;

    if (!bounds.max) {
        return;
    }

    endDate.value = bounds.max;

    const end = new
