# -*- coding: utf-8 -*-
"""
GDP, Inflation & GSCPI Static Dashboard
---------------------------------------
로컬 개발 모드:
    python macro_economies.py

GitHub Actions / GitHub Pages 정적 생성 모드:
    python macro_economies.py --cron

생성 파일:
    - index.html
    - macro_data.json
    - gscpi_data.json
    - dashboard_meta.json
    - gdp_inflation_gscpi_cache.db
"""

import argparse
import importlib.util
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from io import BytesIO, StringIO
from pathlib import Path

# =========================================================
# 0. Dependency Setup
# =========================================================
def setup_environment():
    """
    로컬 PC 및 GitHub Actions 환경에서 필요한 패키지를 점검한다.
    GSCPI xls 파일 읽기를 위해 xlrd가 필요하다.
    """
    required_packages = {
        "pandas": "pandas",
        "requests": "requests",
        "flask": "flask",
        "xlrd": "xlrd>=2.0.1",
    }

    for import_name, pip_name in required_packages.items():
        if importlib.util.find_spec(import_name) is not None:
            continue

        print(
            f"[SETUP] '{pip_name}' 패키지가 없어 설치를 시도합니다...",
            flush=True,
        )

        try:
            subprocess.check_call(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    pip_name,
                ]
            )
        except Exception:
            subprocess.check_call(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    pip_name,
                    "--break-system-packages",
                ]
            )

setup_environment()

import pandas as pd
import requests
from flask import Flask, Response, send_from_directory

# =========================================================
# 1. Basic Configuration
# =========================================================
BASE_DIR = Path(__file__).resolve().parent

DB_PATH = BASE_DIR / "gdp_inflation_gscpi_cache.db"
STATIC_HTML_PATH = BASE_DIR / "index.html"
MACRO_JSON_PATH = BASE_DIR / "macro_data.json"
GSCPI_JSON_PATH = BASE_DIR / "gscpi_data.json"
META_JSON_PATH = BASE_DIR / "dashboard_meta.json"

try:
    CACHE_TTL_HOURS = float(
        os.environ.get("CACHE_TTL_HOURS", "24")
    )
except ValueError:
    CACHE_TTL_HOURS = 24.0

WB_GDP_INDICATOR = "NY.GDP.MKTP.KD.ZG"
WB_INFLATION_INDICATOR = "FP.CPI.TOTL.ZG"

WB_GDP_API_URL = (
    "https://api.worldbank.org/v2/country/all/indicator/"
    f"{WB_GDP_INDICATOR}?format=json&per_page=25000"
)

WB_INFLATION_API_URL = (
    "https://api.worldbank.org/v2/country/all/indicator/"
    f"{WB_INFLATION_INDICATOR}?format=json&per_page=25000"
)

COUNTRY_META_URL = (
    "https://api.worldbank.org/v2/country?format=json&per_page=400"
)

GSCPI_XLS_URL = (
    "https://www.newyorkfed.org/medialibrary/research/"
    "interactives/gscpi/downloads/gscpi_data.xls"
)

DEFAULT_COUNTRIES = [
    "Canada",
    "France",
    "Germany",
    "Italy",
    "Japan",
    "United Kingdom",
    "United States",
    "Korea, Rep.",
]

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

REQUEST_TIMEOUT_SECONDS = 90

app = Flask(__name__)

# =========================================================
# 2. Logging and Local Server Utilities
# =========================================================
def log(message):
    print(message, flush=True)

def is_port_available(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(
            socket.SOL_SOCKET,
            socket.SO_REUSEADDR,
            1,
        )

        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False

def find_available_port(host="127.0.0.1"):
    candidate_ports = [
        8000,
        8001,
        8002,
        8080,
        8081,
        8082,
        5000,
    ]

    for port in candidate_ports:
        if is_port_available(host, port):
            return port

    for port in range(8100, 8201):
        if is_port_available(host, port):
            return port

    raise RuntimeError("사용 가능한 포트를 찾지 못했습니다.")

def wait_and_open_browser(url, host, port, timeout=20):
    """
    Flask 서버가 실제로 실행된 뒤 브라우저를 연다.
    """
    check_host = "127.0.0.1" if host == "0.0.0.0" else host
    start_time = time.time()

    while time.time() - start_time < timeout:
        try:
            with socket.create_connection(
                (check_host, port),
                timeout=1,
            ):
                webbrowser.open_new(url)
                return
        except Exception:
            time.sleep(0.4)

    log(f"[WARN] 브라우저 자동 실행 실패. 직접 접속하세요: {url}")

# =========================================================
# 3. SQLite Cache
# =========================================================
def init_cache_db():
    """
    SQLite 캐시 DB와 테이블을 생성한다.
    """
    with sqlite3.connect(DB_PATH, timeout=30) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS dataframe_cache (
                cache_key TEXT PRIMARY KEY,
                cached_at REAL NOT NULL,
                row_count INTEGER NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )

        connection.commit()

def save_dataframe_cache(cache_key, dataframe):
    """
    DataFrame을 SQLite JSON 캐시 형태로 저장한다.
    """
    if dataframe is None:
        dataframe = pd.DataFrame()

    init_cache_db()

    payload = dataframe.to_json(
        orient="table",
        date_format="iso",
        force_ascii=False,
    )

    with sqlite3.connect(DB_PATH, timeout=30) as connection:
        connection.execute("PRAGMA busy_timeout=30000")

        connection.execute(
            """
            INSERT INTO dataframe_cache (
                cache_key,
                cached_at,
                row_count,
                payload
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                cached_at = excluded.cached_at,
                row_count = excluded.row_count,
                payload = excluded.payload
            """,
            (
                cache_key,
                time.time(),
                int(len(dataframe)),
                payload,
            ),
        )

        connection.commit()

def load_dataframe_cache(cache_key, allow_expired=False):
    """
    SQLite에서 DataFrame 캐시를 읽는다.
    allow_expired=True이면 만료된 캐시도 사용할 수 있다.
    """
    init_cache_db()

    with sqlite3.connect(DB_PATH, timeout=30) as connection:
        connection.execute("PRAGMA busy_timeout=30000")

        row = connection.execute(
            """
            SELECT cached_at, payload
            FROM dataframe_cache
            WHERE cache_key = ?
            """,
            (cache_key,),
        ).fetchone()

    if row is None:
        return None

    cached_at, payload = row

    age_hours = max(
        0.0,
        (time.time() - float(cached_at)) / 3600.0,
    )

    if (
        not allow_expired
        and CACHE_TTL_HOURS >= 0
        and age_hours > CACHE_TTL_HOURS
    ):
        return None

    try:
        dataframe = pd.read_json(
            StringIO(payload),
            orient="table",
        )

        dataframe.attrs["cache_age_hours"] = age_hours

        return dataframe

    except Exception as error:
        log(f"[WARN] 캐시 읽기 실패 - {cache_key}: {error}")
        return None

def get_cached_or_fetch(cache_key, fetch_function, force_refresh=False):
    """
    유효 캐시가 있으면 캐시를 사용한다.
    최신 수집 실패 시 만료된 기존 캐시를 fallback으로 사용한다.
    """
    if not force_refresh:
        cached = load_dataframe_cache(cache_key)

        if cached is not None:
            log(
                f"[CACHE] {cache_key}: "
                f"{len(cached):,}행 로드 "
                f"({cached.attrs.get('cache_age_hours', 0):.1f}시간 경과)"
            )
            return cached

    log(f"[FETCH] {cache_key}: 최신 데이터 조회 중...")

    try:
        dataframe = fetch_function()

        if dataframe is None:
            raise RuntimeError("수집 결과가 없습니다.")

        save_dataframe_cache(cache_key, dataframe)

        log(
            f"[DB] {cache_key}: "
            f"{len(dataframe):,}행 저장 완료"
        )

        return dataframe

    except Exception as error:
        stale = load_dataframe_cache(
            cache_key,
            allow_expired=True,
        )

        if stale is not None:
            log(
                f"[WARN] {cache_key} 최신 수집 실패. "
                f"기존 DB 캐시를 사용합니다: {error}"
            )
            return stale

        raise RuntimeError(
            f"{cache_key} 수집 실패 및 대체 캐시 없음: {error}"
        ) from error

# =========================================================
# 4. Data Collection
# =========================================================
def safe_get_json(url):
    """
    JSON API 호출 함수.
    """
    response = requests.get(
        url,
        headers=REQUEST_HEADERS,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )

    response.raise_for_status()

    try:
        return response.json()
    except ValueError as error:
        raise RuntimeError(
            f"JSON 응답 파싱 실패: {url}"
        ) from error

def get_country_meta(force_refresh=False):
    """
    World Bank 국가 및 지역 메타데이터를 가져온다.

    World Bank의 `id`는 ISO3 코드이며, indicator API의
    `countryiso3code`와 연결하기 위해 이 값을 사용한다.
    """

    def fetch():
        data = safe_get_json(COUNTRY_META_URL)

        rows = (
            data[1]
            if isinstance(data, list) and len(data) > 1
            else []
        )

        output = []

        for item in rows:
            region_data = item.get("region") or {}

            region = (
                region_data.get("value")
                if isinstance(region_data, dict)
                else None
            )

            country = item.get("name")
            iso3_code = str(item.get("id") or "").strip()

            if (
                iso3_code
                and country
                and region
                and region != "Aggregates"
            ):
                output.append(
                    {
                        "code": iso3_code,
                        "country": country,
                        "region": region,
                    }
                )

        dataframe = pd.DataFrame(output)

        if dataframe.empty:
            raise RuntimeError(
                "World Bank 국가 메타데이터가 비어 있습니다."
            )

        return (
            dataframe
            .drop_duplicates(subset=["code"])
            .sort_values(["region", "country"])
            .reset_index(drop=True)
        )

    return get_cached_or_fetch(
        "worldbank_country_meta",
        fetch,
        force_refresh=force_refresh,
    )

def get_worldbank_indicator(
    url,
    value_name,
    force_refresh=False,
):
    """
    World Bank GDP 또는 Inflation 시계열을 가져온다.
    """

    def fetch():
        data = safe_get_json(url)

        rows = (
            data[1]
            if isinstance(data, list) and len(data) > 1
            else []
        )

        output = []

        for item in rows:
            country_data = item.get("country") or {}

            country = (
                country_data.get("value")
                if isinstance(country_data, dict)
                else None
            )

            code = str(
                item.get("countryiso3code") or ""
            ).strip()

            year = item.get("date")
            value = item.get("value")

            if not (country and code and year and value is not None):
                continue

            try:
                output.append(
                    {
                        "code": code,
                        "country": country,
                        "year": int(year),
                        value_name: float(value),
                    }
                )
            except (TypeError, ValueError):
                continue

        dataframe = pd.DataFrame(output)

        if dataframe.empty:
            raise RuntimeError(
                f"World Bank {value_name} 데이터가 비어 있습니다."
            )

        return (
            dataframe
            .drop_duplicates(
                subset=["code", "country", "year"]
            )
            .sort_values(["country", "year"])
            .reset_index(drop=True)
        )

    if WB_GDP_INDICATOR in url:
        cache_key = "worldbank_gdp_" + value_name
    elif WB_INFLATION_INDICATOR in url:
        cache_key = "worldbank_inflation_" + value_name
    else:
        cache_key = "worldbank_" + value_name

    return get_cached_or_fetch(
        cache_key,
        fetch,
        force_refresh=force_refresh,
    )

def get_gscpi_data(force_refresh=False):
    """
    New York Fed Global Supply Chain Pressure Index 데이터를 가져온다.
    """

    def fetch():
        response = requests.get(
            GSCPI_XLS_URL,
            headers=REQUEST_HEADERS,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

        response.raise_for_status()

        content = response.content

        if not content:
            raise RuntimeError("GSCPI Excel 파일이 비어 있습니다.")

        excel_file = pd.ExcelFile(BytesIO(content))

        if not excel_file.sheet_names:
            raise RuntimeError("GSCPI Excel 시트를 찾지 못했습니다.")

        target_sheet = None

        for sheet_name in excel_file.sheet_names:
            normalized_name = str(sheet_name).strip().lower()

            if "monthly" in normalized_name and "data" in normalized_name:
                target_sheet = sheet_name
                break

        if target_sheet is None:
            target_sheet = excel_file.sheet_names[-1]

        dataframe = pd.read_excel(
            BytesIO(content),
            sheet_name=target_sheet,
        )

        if dataframe.empty or dataframe.shape[1] < 2:
            raise RuntimeError(
                "GSCPI Excel 데이터의 컬럼 수가 부족합니다."
            )

        normalized_columns = {
            str(column).strip().lower(): column
            for column in dataframe.columns
        }

        date_column = normalized_columns.get(
            "date",
            dataframe.columns[0],
        )

        value_column = normalized_columns.get(
            "gscpi",
            dataframe.columns[1],
        )

        output = dataframe[
            [date_column, value_column]
        ].copy()

        output.columns = ["date", "gscpi"]

        output["date"] = pd.to_datetime(
            output["date"],
            errors="coerce",
        )

        output["gscpi"] = pd.to_numeric(
            output["gscpi"],
            errors="coerce",
        )

        output = output.dropna(
            subset=["date", "gscpi"]
        )

        if output.empty:
            raise RuntimeError(
                "정제 후 GSCPI 데이터가 비어 있습니다."
            )

        output["year"] = output["date"].dt.year

        return (
            output
            .sort_values("date")
            .drop_duplicates(subset=["date"])
            .reset_index(drop=True)
        )

    dataframe = get_cached_or_fetch(
        "newyorkfed_gscpi",
        fetch,
        force_refresh=force_refresh,
    )

    if dataframe.empty:
        return dataframe

    dataframe = dataframe.copy()

    dataframe["date"] = pd.to_datetime(
        dataframe["date"],
        errors="coerce",
    )

    dataframe["gscpi"] = pd.to_numeric(
        dataframe["gscpi"],
        errors="coerce",
    )

    dataframe = dataframe.dropna(
        subset=["date", "gscpi"]
    )

    dataframe["year"] = (
        pd.to_numeric(
            dataframe["year"],
            errors="coerce",
        )
        .astype("Int64")
    )

    return dataframe.sort_values("date").reset_index(drop=True)

# =========================================================
# 5. Static Data Generation
# =========================================================
def dataframe_to_records(dataframe, date_columns=None):
    """
    DataFrame을 정적 JSON 파일용 record 배열로 바꾼다.
    NaN, Infinity, Timestamp를 JSON 호환 형식으로 정리한다.
    """
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

    output = output.where(pd.notnull(output), None)

    return json.loads(
        output.to_json(
            orient="records",
            force_ascii=False,
            date_format="iso",
        )
    )

def collect_dashboard_data(force_refresh=False):
    """
    모든 원천 데이터를 수집하고 Dashboard용 구조로 정리한다.
    """
    meta = get_country_meta(force_refresh=force_refresh)

    gdp = get_worldbank_indicator(
        WB_GDP_API_URL,
        "gdp_growth",
        force_refresh=force_refresh,
    )

    inflation = get_worldbank_indicator(
        WB_INFLATION_API_URL,
        "inflation",
        force_refresh=force_refresh,
    )

    gscpi = get_gscpi_data(
        force_refresh=force_refresh,
    )

    gdp = gdp.merge(
        meta,
        on=["code", "country"],
        how="left",
        validate="many_to_one",
    )

    inflation = inflation.merge(
        meta,
        on=["code", "country"],
        how="left",
        validate="many_to_one",
    )

    missing_gdp_region = int(gdp["region"].isna().sum())
    missing_inflation_region = int(
        inflation["region"].isna().sum()
    )

    if missing_gdp_region:
        log(
            "[WARN] GDP 지역 메타데이터 미매칭 행 수: "
            f"{missing_gdp_region:,}"
        )

    if missing_inflation_region:
        log(
            "[WARN] Inflation 지역 메타데이터 미매칭 행 수: "
            f"{missing_inflation_region:,}"
        )

    gdp = gdp.dropna(
        subset=["country", "year", "gdp_growth"]
    )

    inflation = inflation.dropna(
        subset=["country", "year", "inflation"]
    )

    gdp = gdp.sort_values(
        ["country", "year"]
    ).reset_index(drop=True)

    inflation = inflation.sort_values(
        ["country", "year"]
    ).reset_index(drop=True)

    gscpi = gscpi.sort_values(
        "date"
    ).reset_index(drop=True)

    return {
        "countries": dataframe_to_records(meta),
        "gdp": dataframe_to_records(gdp),
        "inflation": dataframe_to_records(inflation),
        "gscpi": dataframe_to_records(
            gscpi,
            date_columns=["date"],
        ),
    }

def atomic_write_text(path, text):
    """
    중간 저장 실패로 인한 불완전한 정적 파일 배포를 방지한다.
    """
    temporary_path = path.with_suffix(
        path.suffix + ".tmp"
    )

    temporary_path.write_text(
        text,
        encoding="utf-8",
    )

    temporary_path.replace(path)

def write_static_json_files(data):
    """
    GitHub Pages용 JSON 데이터 파일을 생성한다.
    """
    macro_payload = {
        "countries": data["countries"],
        "gdp": data["gdp"],
        "inflation": data["inflation"],
    }

    gscpi_payload = {
        "gscpi": data["gscpi"],
    }

    fetched_at = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    meta_payload = {
        "generated_at": fetched_at,
        "sources": {
            "gdp": "World Bank Open Data",
            "inflation": "World Bank Open Data",
            "gscpi": "Federal Reserve Bank of New York",
        },
        "record_count": {
            "countries": len(data["countries"]),
            "gdp": len(data["gdp"]),
            "inflation": len(data["inflation"]),
            "gscpi": len(data["gscpi"]),
        },
    }

    atomic_write_text(
        MACRO_JSON_PATH,
        json.dumps(
            macro_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ),
    )

    atomic_write_text(
        GSCPI_JSON_PATH,
        json.dumps(
            gscpi_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ),
    )

    atomic_write_text(
        META_JSON_PATH,
        json.dumps(
            meta_payload,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        ),
    )

    log(f"[STATIC] 생성 완료: {MACRO_JSON_PATH.name}")
    log(f"[STATIC] 생성 완료: {GSCPI_JSON_PATH.name}")
    log(f"[STATIC] 생성 완료: {META_JSON_PATH.name}")

# =========================================================
# 6. Static HTML
# =========================================================
STATIC_HTML = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MACRO ECONOMIES | MI Terminal</title>

<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>

<style>
:root {
    --bg-core: #01050a;
    --bg-base: #030911;
    --bg-panel: #07111b;
    --bg-panel-2: #0a1723;
    --bg-panel-3: #0d1d2b;
    --line: #163047;
    --line-strong: #294964;

    --text: #f3f7fb;
    --text-soft: #b0c1cf;
    --muted: #71879a;
    --dim: #40596c;

    --amber: #f6a623;
    --amber-soft: #ffbf58;
    --blue: #43a7f5;
    --cyan: #38c6df;
    --green: #37ca82;
    --red: #ff6666;
    --purple: #aa8cf6;

    --shadow:
        0 5px 22px rgba(0, 0, 0, .60),
        0 0 18px rgba(37, 124, 181, .08);
}

* {
    box-sizing: border-box;
}

html,
body {
    min-height: 100%;
    background: var(--bg-core);
}

body {
    margin: 0;
    min-width: 320px;
    color: var(--text);
    background:
        radial-gradient(
            circle at 50% -20%,
            rgba(41, 122, 177, .15),
            transparent 42%
        ),
        linear-gradient(
            180deg,
            rgba(246, 166, 35, .025),
            transparent 260px
        ),
        var(--bg-core);
    font-family:
        "IBM Plex Mono",
        "Roboto Mono",
        "SFMono-Regular",
        Consolas,
        "Liberation Mono",
        monospace;
    font-size: 11px;
    line-height: 1.35;
    -webkit-font-smoothing: antialiased;
}

button,
select {
    font: inherit;
}

/* =========================================================
   TERMINAL SHELL
   ========================================================= */
.wrap {
    width: min(1920px, calc(100% - 16px));
    max-width: 1920px;
    margin: 8px auto 14px;
    padding: 0;
}

.hero {
    min-height: 54px;
    display: flex;
    align-items: stretch;
    justify-content: space-between;
    gap: 16px;
    margin: 0 0 6px;
    padding: 0;
    overflow: hidden;
    border: 1px solid var(--line-strong);
    border-radius: 0;
    background:
        linear-gradient(
            180deg,
            rgba(13, 32, 49, .98),
            rgba(3, 9, 15, .98)
        );
    box-shadow: var(--shadow);
}

.hero::before {
    display: flex;
    align-items: center;
    justify-content: center;
    min-width: 52px;
    padding: 0 10px;
    color: #02060b;
    content: "MI";
    background: var(--amber);
    font-size: 13px;
    font-weight: 900;
    letter-spacing: .08em;
    box-shadow: 0 0 14px rgba(246, 166, 35, .35);
}

.hero h1 {
    display: flex;
    align-items: center;
    flex: 1;
    margin: 0;
    color: #ffffff;
    font-size: 14px;
    font-weight: 800;
    letter-spacing: .10em;
    text-transform: uppercase;
    white-space: nowrap;
}

.meta {
    min-width: min(58%, 760px);
    display: flex;
    flex-direction: column;
    justify-content: center;
    gap: 3px;
    padding: 6px 14px;
    color: var(--muted);
    border-left: 1px solid var(--line);
    font-size: 8px;
    font-weight: 700;
    letter-spacing: .05em;
    line-height: 1.45;
    text-align: right;
    text-transform: uppercase;
}

.meta b {
    color: var(--text-soft);
    font-weight: 800;
}

.source-link {
    color: var(--blue);
    text-decoration: none;
}

.source-link:hover {
    color: var(--amber-soft);
    text-decoration: underline;
}

.tabs {
    display: flex;
    gap: 0;
    margin: 0 0 6px;
    overflow: hidden;
    border: 1px solid var(--line);
    background: #02070d;
    box-shadow: 0 2px 10px rgba(0, 0, 0, .35);
}

.tab {
    min-width: 180px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 8px 14px;
    color: var(--muted);
    border-right: 1px solid var(--line);
    background: transparent;
    cursor: pointer;
    font-size: 9px;
    font-weight: 800;
    letter-spacing: .08em;
    text-decoration: none;
    text-transform: uppercase;
    transition: .15s ease;
}

.tab:hover {
    color: var(--text);
    background: rgba(67, 167, 245, .06);
}

.tab.active {
    color: #071018;
    background: var(--amber);
    box-shadow: 0 0 14px rgba(246, 166, 35, .2);
}

.warning {
    margin: 0 0 6px;
    padding: 9px 12px;
    color: var(--amber-soft);
    border: 1px solid rgba(246, 166, 35, .55);
    border-left: 3px solid var(--amber);
    border-radius: 0;
    background: rgba(246, 166, 35, .08);
    font-size: 9px;
    font-weight: 700;
    letter-spacing: .04em;
    text-transform: uppercase;
}

.loading,
.empty {
    padding: 44px 20px;
    color: var(--muted);
    font-size: 10px;
    font-weight: 800;
    letter-spacing: .08em;
    text-align: center;
    text-transform: uppercase;
}

/* =========================================================
   STRICT 12-COLUMN GRID
   ========================================================= */
#macroSection,
#gscpiSection {
    display: grid;
    grid-template-columns: repeat(12, minmax(0, 1fr));
    gap: 6px;
}

#macroSection.hidden,
#gscpiSection.hidden,
.hidden {
    display: none !important;
}

#macroSection > .panel:first-child,
#gscpiSection > .panel:first-child {
    grid-column: span 12;
}

#macroSection > .grid,
#gscpiSection > .grid {
    grid-column: span 12;
}

#macroSection > .row2,
#gscpiSection > .row2 {
    grid-column: span 12;
}

#macroSection > .panel,
#gscpiSection > .panel {
    grid-column: span 12;
    margin: 0;
}

.panel {
    min-width: 0;
    margin: 0;
    padding: 0;
    overflow: hidden;
    border: 1px solid var(--line);
    border-radius: 0;
    background:
        linear-gradient(
            145deg,
            rgba(14, 33, 49, .72),
            rgba(4, 11, 18, .98)
        );
    box-shadow: var(--shadow);
}

.panel::before {
    display: block;
    height: 2px;
    content: "";
    background: linear-gradient(
        90deg,
        var(--amber),
        rgba(246, 166, 35, .18) 42%,
        transparent 78%
    );
}

.section-title {
    min-height: 30px;
    display: flex;
    align-items: center;
    margin: 0;
    padding: 0 10px;
    color: var(--amber);
    border-bottom: 1px solid var(--line);
    background:
        linear-gradient(
            180deg,
            rgba(13, 31, 47, .92),
            rgba(7, 17, 27, .90)
        );
    font-size: 9px;
    font-weight: 900;
    letter-spacing: .09em;
    text-transform: uppercase;
}

/* =========================================================
   FILTERS / CONTROL PANEL
   ========================================================= */
.panel .grid:first-of-type {
    padding: 10px;
}

.grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 6px;
}

label {
    display: block;
    margin: 0 0 5px;
    color: var(--muted);
    font-size: 8px;
    font-weight: 800;
    letter-spacing: .08em;
    text-transform: uppercase;
}

select {
    width: 100%;
    min-height: 28px;
    padding: 5px 8px;
    color: var(--text-soft);
    border: 1px solid #21415a;
    border-radius: 0;
    outline: none;
    background: #020911;
    font-size: 9px;
    font-weight: 700;
}

select:focus {
    color: var(--amber-soft);
    border-color: var(--amber);
    box-shadow: 0 0 0 1px rgba(246, 166, 35, .15);
}

select[multiple] {
    min-height: 145px;
    padding: 4px;
}

select option {
    padding: 4px;
    color: var(--text-soft);
    background: #07111b;
}

button {
    min-height: 26px;
    padding: 6px 10px;
    color: var(--text);
    border: 1px solid #28506e;
    border-radius: 0;
    outline: none;
    background: #0a1a28;
    cursor: pointer;
    font-size: 8px;
    font-weight: 800;
    letter-spacing: .06em;
    text-transform: uppercase;
    transition: .15s ease;
}

button:hover {
    color: #071018;
    border-color: var(--amber);
    background: var(--amber);
}

.select-tools,
.btn-row {
    display: flex;
    gap: 4px;
    margin: 0 0 7px;
    flex-wrap: wrap;
}

.select-tools button {
    min-height: 24px;
    padding: 4px 8px;
    color: var(--text-soft);
    border-color: #21415a;
    background: #0a1723;
    font-size: 8px;
}

.select-tools button:first-child,
.icon-csv {
    color: #071018;
    border-color: var(--amber);
    background: var(--amber);
}

.icon-csv:hover,
.select-tools button:first-child:hover {
    color: #ffffff;
    background: #c97d0a;
}

.subnote {
    margin: 0;
    padding: 8px 10px;
    color: var(--muted);
    border-top: 1px solid #102638;
    background: rgba(2, 8, 14, .58);
    font-size: 8px;
    line-height: 1.65;
    letter-spacing: .02em;
}

/* =========================================================
   KPI ROW
   ========================================================= */
#macroSection > .grid:nth-of-type(2),
#gscpiSection > .grid:nth-of-type(2) {
    grid-template-columns: repeat(4, minmax(0, 1fr));
}

.kpi {
    min-height: 86px;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    padding: 9px 10px;
    border: 1px solid var(--line);
    border-top: 2px solid #2b638b;
    border-left: 1px solid var(--line);
    border-radius: 0;
    background:
        linear-gradient(
            135deg,
            rgba(34, 100, 143, .18),
            transparent 62%
        ),
        var(--bg-panel);
    box-shadow: var(--shadow);
}

.kpi:nth-child(2) {
    border-top-color: var(--blue);
}

.kpi:nth-child(3) {
    border-top-color: var(--green);
}

.kpi:nth-child(4) {
    border-top-color: var(--purple);
}

.kpi .label {
    color: var(--muted);
    font-size: 8px;
    font-weight: 800;
    letter-spacing: .08em;
    text-transform: uppercase;
}

.kpi .value {
    margin: 7px 0 0;
    overflow: hidden;
    color: #ffffff;
    font-size: clamp(16px, 1.5vw, 24px);
    font-weight: 800;
    font-variant-numeric: tabular-nums;
    letter-spacing: -.06em;
    text-align: right;
    text-overflow: ellipsis;
    white-space: nowrap;
}

/* =========================================================
   MAIN CHART / WATCHLIST STRUCTURE
   ========================================================= */
.row2 {
    display: grid;
    grid-template-columns: repeat(12, minmax(0, 1fr));
    gap: 6px;
}

.row2 > .panel {
    min-width: 0;
    margin: 0;
}

.row2 > .panel:first-child {
    grid-column: span 8;
}

.row2 > .panel:last-child {
    grid-column: span 4;
}

.chart {
    width: 100%;
    min-height: 390px;
    padding: 2px;
}

.js-plotly-plot .plotly .modebar {
    background: rgba(3, 9, 15, .88) !important;
    border: 1px solid var(--line) !important;
}

.js-plotly-plot .plotly .modebar-btn path {
    fill: var(--muted) !important;
}

.js-plotly-plot .plotly .modebar-btn:hover path {
    fill: var(--amber) !important;
}

/* =========================================================
   INSIGHT / NEWS / RISK PANELS
   ========================================================= */
#macroSection > .row2:last-child > .panel:first-child,
#gscpiSection > .panel:last-child {
    border-left: 3px solid var(--blue);
}

#macroSection > .row2:last-child > .panel:last-child {
    border-left: 3px solid var(--red);
}

ul.exec {
    min-height: 205px;
    margin: 0;
    padding: 7px 12px 12px 28px;
    color: var(--text-soft);
    background:
        linear-gradient(
            180deg,
            rgba(4, 12, 20, .38),
            rgba(2, 7, 12, .15)
        );
}

ul.exec li {
    margin: 0;
    padding: 7px 2px;
    border-bottom: 1px solid #102536;
    font-size: 9px;
    line-height: 1.5;
}

ul.exec li::marker {
    color: var(--amber);
}

ul.exec li:last-child {
    border-bottom: 0;
}

/* =========================================================
   STATUS BAR
   ========================================================= */
.wrap::after {
    min-height: 28px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-top: 6px;
    padding: 0 10px;
    color: var(--muted);
    border: 1px solid var(--line);
    border-left: 3px solid var(--green);
    content:
        "SYSTEM STATUS: NORMAL  |  MARKET HEALTH: MONITORED  |  SENTIMENT: DATA-DRIVEN  |  ALERTS: NO CRITICAL EXCEPTIONS";
    background: #02070d;
    font-size: 8px;
    font-weight: 800;
    letter-spacing: .06em;
    text-transform: uppercase;
}

/* =========================================================
   RESPONSIVE
   ========================================================= */
@media (max-width: 1180px) {
    .hero {
        min-height: auto;
        flex-wrap: wrap;
    }

    .hero h1 {
        min-height: 44px;
    }

    .meta {
        width: 100%;
        min-width: 100%;
        padding: 8px 12px;
        border-top: 1px solid var(--line);
        border-left: 0;
        text-align: left;
    }

    .grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    #macroSection > .grid:nth-of-type(2),
    #gscpiSection > .grid:nth-of-type(2) {
        grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .row2 > .panel:first-child,
    .row2 > .panel:last-child {
        grid-column: span 12;
    }
}

@media (max-width: 720px) {
    .wrap {
        width: calc(100% - 10px);
        margin-top: 5px;
    }

    .hero::before {
        min-width: 42px;
    }

    .hero h1 {
        padding: 0 8px;
        font-size: 11px;
        letter-spacing: .06em;
    }

    .tabs {
        overflow-x: auto;
    }

    .tab {
        min-width: 160px;
    }

    .grid,
    #macroSection > .grid:nth-of-type(2),
    #gscpiSection > .grid:nth-of-type(2) {
        grid-template-columns: 1fr;
    }

    .panel .grid:first-of-type {
        padding: 8px;
    }

    .kpi {
        min-height: 72px;
    }

    .chart {
        min-height: 320px;
    }

    .meta {
        font-size: 7px;
    }
}
</style>
</head>

<body>
<div class="wrap">

    <div class="hero">
        <h1>MACRO ECONOMIES | GDP · INFLATION · GSCPI</h1>

        <div class="meta">
            <div>
                <b>DATA SOURCES</b> |
                GDP / INFLATION:
                <a class="source-link"
                   href="https://data.worldbank.org/"
                   target="_blank"
                   rel="noopener noreferrer">
                    WORLD BANK OPEN DATA
                </a>
                |
                GSCPI:
                <a class="source-link"
                   href="https://www.newyorkfed.org/research/policy/gscpi"
                   target="_blank"
                   rel="noopener noreferrer">
                    FEDERAL RESERVE BANK OF NEW YORK
                </a>
            </div>
            <div>
                <b>LAST REFRESH</b> |
                <span id="generatedAt">LOADING DATA...</span>
            </div>
        </div>
    </div>

    <div id="warning" class="warning"></div>

    <div class="tabs">
        <a id="macroTab" class="tab active" onclick="showTab('macro')">
            MACRO ECONOMIES
        </a>

        <a id="gscpiTab" class="tab" onclick="showTab('gscpi')">
            GLOBAL SUPPLY CHAIN PRESSURE INDEX
        </a>
    </div>

    <div id="loading" class="loading">
        INITIALIZING MARKET DATA FEED...
    </div>

    <div id="macroSection" class="hidden">

        <div class="panel">
            <h3 class="section-title">01. DATA UNIVERSE &amp; TIME HORIZON</h3>

            <div class="grid">
                <div>
                    <label for="regionSelect">REGION FILTER</label>
                    <select id="regionSelect" onchange="onRegionChange()"></select>
                </div>

                <div>
                    <label for="yearsSelect">OBSERVATION WINDOW</label>
                    <select id="yearsSelect" onchange="renderMacroDashboard()">
                        <option value="5">LAST 5 YEARS</option>
                        <option value="10" selected>LAST 10 YEARS</option>
                        <option value="20">LAST 20 YEARS</option>
                    </select>
                </div>

                <div style="grid-column:span 2;">
                    <label for="countrySelect">COUNTRY UNIVERSE</label>

                    <div class="select-tools">
                        <button type="button" onclick="setAllCountries(true)">
                            SELECT REGION UNIVERSE
                        </button>
                        <button type="button" onclick="setAllCountries(false)">
                            CLEAR SELECTION
                        </button>
                    </div>

                    <select id="countrySelect"
                            multiple
                            size="8"
                            onchange="renderMacroDashboard()">
                    </select>
                </div>
            </div>

            <div class="subnote">
                DEFAULT UNIVERSE: G7 ECONOMIES AND REPUBLIC OF KOREA.
                USE CTRL OR SHIFT TO MODIFY INDIVIDUAL COUNTRY SELECTIONS.
            </div>
        </div>

        <div class="grid">
            <div class="kpi">
                <div class="label">SELECTED COUNTRIES</div>
                <div class="value" id="selectedCountriesKpi">-</div>
            </div>

            <div class="kpi">
                <div class="label">GDP LATEST OBSERVATION</div>
                <div class="value" id="latestGdpYearKpi">-</div>
            </div>

            <div class="kpi">
                <div class="label">INFLATION LATEST OBSERVATION</div>
                <div class="value" id="latestInflationYearKpi">-</div>
            </div>

            <div class="kpi">
                <div class="label">ACTIVE REGION</div>
                <div class="value" id="selectedRegionKpi">-</div>
            </div>
        </div>

        <div class="row2">
            <div class="panel">
                <h3 class="section-title">
                    02. GDP GROWTH TREND | PRIMARY MACRO CHART
                </h3>

                <div class="btn-row" style="padding:8px 10px 0;">
                    <button class="icon-csv" onclick="downloadMacroData('gdp')">
                        EXPORT GDP CSV
                    </button>
                </div>

                <div id="gdpLine" class="chart"></div>
            </div>

            <div class="panel">
                <h3 class="section-title">
                    03. INFLATION WATCHLIST | CROSS-COUNTRY MONITOR
                </h3>

                <div class="btn-row" style="padding:8px 10px 0;">
                    <button class="icon-csv" onclick="downloadMacroData('inflation')">
                        EXPORT INFLATION CSV
                    </button>
                </div>

                <div id="inflationLine" class="chart"></div>
            </div>
        </div>

        <div class="panel">
            <h3 class="section-title">
                04. GDP / INFLATION SPREAD | MACRO REGIME COMPARISON
            </h3>

            <div class="btn-row" style="padding:8px 10px 0;">
                <button class="icon-csv" onclick="downloadMacroData('macro_compare')">
                    EXPORT MACRO COMPARISON CSV
                </button>
            </div>

            <div id="macroCompareChart" class="chart"></div>
        </div>

        <div class="row2">
            <div class="panel">
                <h3 class="section-title">
                    05. GDP HEATMAP PROXY | LATEST COUNTRY GROWTH
                </h3>
                <div id="gdpBar" class="chart"></div>
            </div>

            <div class="panel">
                <h3 class="section-title">
                    06. INFLATION MOVERS | LATEST COUNTRY COMPARISON
                </h3>
                <div id="inflationBar" class="chart"></div>
            </div>
        </div>

        <div class="row2">
            <div class="panel">
                <h3 class="section-title">
                    07. GDP MARKET INTELLIGENCE
                </h3>
                <ul class="exec" id="gdpInsights"></ul>
            </div>

            <div class="panel">
                <h3 class="section-title">
                    RISK INDICATORS | INFLATION INTELLIGENCE
                </h3>
                <ul class="exec" id="inflationInsights"></ul>
            </div>
        </div>
    </div>

    <div id="gscpiSection" class="hidden">

        <div class="panel">
            <h3 class="section-title">
                01. GSCPI OBSERVATION PARAMETERS
            </h3>

            <div class="grid">
                <div>
                    <label for="gscpiYearsSelect">OBSERVATION WINDOW</label>
                    <select id="gscpiYearsSelect"
                            onchange="renderGscpiDashboard()">
                        <option value="5">LAST 5 YEARS</option>
                        <option value="10" selected>LAST 10 YEARS</option>
                        <option value="20">LAST 20 YEARS</option>
                    </select>
                </div>
            </div>
        </div>

        <div class="grid">
            <div class="kpi">
                <div class="label">LATEST OBSERVATION DATE</div>
                <div class="value" id="latestGscpiKpi">-</div>
            </div>

            <div class="kpi">
                <div class="label">ROLLING 12M AVERAGE</div>
                <div class="value" id="avgGscpiKpi">-</div>
            </div>

            <div class="kpi">
                <div class="label">ROLLING 12M HIGH</div>
                <div class="value" id="maxGscpiKpi">-</div>
            </div>

            <div class="kpi">
                <div class="label">ROLLING 12M LOW</div>
                <div class="value" id="minGscpiKpi">-</div>
            </div>
        </div>

        <div class="row2">
            <div class="panel">
                <h3 class="section-title">
                    02. GLOBAL SUPPLY CHAIN PRESSURE INDEX | PRIMARY TREND
                </h3>

                <div class="btn-row" style="padding:8px 10px 0;">
                    <button class="icon-csv" onclick="downloadGscpiData()">
                        EXPORT GSCPI CSV
                    </button>
                </div>

                <div id="gscpiLine" class="chart"></div>
            </div>

            <div class="panel">
                <h3 class="section-title">
                    03. GSCPI WATCHLIST | LAST 12 MONTHS
                </h3>
                <div id="gscpiBar" class="chart"></div>
            </div>
        </div>

        <div class="panel">
            <h3 class="section-title">
                04. SUPPLY CHAIN RISK INTELLIGENCE
            </h3>
            <ul class="exec" id="gscpiInsights"></ul>
        </div>
    </div>
</div>

<script>
const DEFAULT_COUNTRIES = [
    "Canada",
    "France",
    "Germany",
    "Italy",
    "Japan",
    "United Kingdom",
    "United States",
    "Korea, Rep."
];

let macroData = {
    countries: [],
    gdp: [],
    inflation: []
};

let gscpiData = {
    gscpi: []
};

let activeTab = "macro";

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

function chartLayout(title, yAxisTitle) {
    return {
        title: {
            text: title || "",
            font: {
                size: 12,
                color: "#f3f7fb",
                family: "IBM Plex Mono, Roboto Mono, monospace"
            }
        },
        paper_bgcolor: "#07111b",
        plot_bgcolor: "#07111b",
        font: {
            color: "#b0c1cf",
            family: "IBM Plex Mono, Roboto Mono, monospace",
            size: 10
        },
        height: 440,
        margin: {
            l: 58,
            r: 46,
            t: title ? 56 : 26,
            b: 58
        },
        xaxis: {
            gridcolor: "#142a3d",
            linecolor: "#294964",
            tickfont: {
                color: "#71879a",
                size: 9
            },
            zerolinecolor: "#294964"
        },
        yaxis: {
            title: {
                text: yAxisTitle || "",
                font: {
                    color: "#71879a",
                    size: 9
                }
            },
            gridcolor: "#142a3d",
            linecolor: "#294964",
            tickfont: {
                color: "#71879a",
                size: 9
            },
            zerolinecolor: "#294964"
        },
        legend: {
            orientation: "h",
            yanchor: "bottom",
            y: 1.03,
            xanchor: "right",
            x: 1,
            font: {
                color: "#b0c1cf",
                size: 9
            },
            bgcolor: "rgba(0,0,0,0)"
        }
    };
}

function emptyChart(elementId, message) {
    const element = document.getElementById(elementId);
    element.innerHTML = "";

    const box = document.createElement("div");
    box.className = "empty";
    box.textContent = message;

    element.appendChild(box);
}

function getSelectedCountries() {
    const select = document.getElementById("countrySelect");

    return Array.from(select.selectedOptions).map(
        option => option.value
    );
}

function uniqueRegions() {
    const regions = macroData.countries
        .map(item => item.region)
        .filter(Boolean);

    return [...new Set(regions)].sort();
}

function populateRegionSelect() {
    const select = document.getElementById("regionSelect");
    select.innerHTML = "";

    select.add(new Option("전체", "전체"));

    uniqueRegions().forEach(region => {
        select.add(new Option(region, region));
    });
}

function populateCountrySelect(selectedRegion, selectedCountries) {
    const countrySelect = document.getElementById("countrySelect");

    let countries = macroData.countries;

    if (selectedRegion !== "전체") {
        countries = countries.filter(
            item => item.region === selectedRegion
        );
    }

    const names = countries
        .map(item => item.country)
        .filter(Boolean)
        .sort();

    countrySelect.innerHTML = "";

    names.forEach(country => {
        const option = new Option(country, country);
        option.selected = selectedCountries.includes(country);
        countrySelect.add(option);
    });
}

function setAllCountries(selected) {
    const countrySelect = document.getElementById("countrySelect");

    Array.from(countrySelect.options).forEach(option => {
        option.selected = selected;
    });

    renderMacroDashboard();
}

function onRegionChange() {
    const selectedRegion = document.getElementById(
        "regionSelect"
    ).value;

    const countries = macroData.countries
        .filter(item => {
            return (
                selectedRegion === "전체"
                || item.region === selectedRegion
            );
        })
        .map(item => item.country);

    populateCountrySelect(selectedRegion, countries);
    renderMacroDashboard();
}

function yearsFilter(data, years) {
    if (!data || !data.length) {
        return [];
    }

    const maxYear = Math.max(
        ...data.map(item => Number(item.year))
    );

    const minYear = maxYear - years + 1;

    return data.filter(item => Number(item.year) >= minYear);
}

function selectedMacroData(metric) {
    const countries = getSelectedCountries();
    const years = Number(
        document.getElementById("yearsSelect").value
    );

    const source = metric === "gdp"
        ? macroData.gdp
        : macroData.inflation;

    return yearsFilter(
        source.filter(item => countries.includes(item.country)),
        years
    );
}

function latestByCountry(data, fieldName) {
    const map = new Map();

    data.forEach(item => {
        const oldValue = map.get(item.country);

        if (
            !oldValue
            || Number(item.year) > Number(oldValue.year)
        ) {
            map.set(item.country, item);
        }
    });

    return Array.from(map.values()).sort(
        (a, b) => Number(b[fieldName]) - Number(a[fieldName])
    );
}

function groupByCountry(data) {
    const grouped = {};

    data.forEach(item => {
        if (!grouped[item.country]) {
            grouped[item.country] = [];
        }

        grouped[item.country].push(item);
    });

    Object.keys(grouped).forEach(country => {
        grouped[country].sort(
            (a, b) => Number(a.year) - Number(b.year)
        );
    });

    return grouped;
}

function drawLineChart(
    elementId,
    data,
    fieldName,
    yAxisTitle,
    colors
) {
    if (!data.length) {
        emptyChart(elementId, "데이터 없음");
        return;
    }

    const grouped = groupByCountry(data);

    const traces = Object.keys(grouped).map(
        (country, index) => {
            const rows = grouped[country];

            return {
                x: rows.map(item => item.year),
                y: rows.map(item => Number(item[fieldName])),
                mode: "lines+markers",
                type: "scatter",
                name: country,
                line: {
                    width: 2.5,
                    color: colors[index % colors.length]
                },
                marker: {
                    size: 6
                }
            };
        }
    );

    Plotly.react(
        elementId,
        traces,
        chartLayout("", yAxisTitle),
        {
            responsive: true,
            displaylogo: false
        }
    );
}

function drawBarChart(
    elementId,
    data,
    fieldName,
    yAxisTitle,
    color
) {
    if (!data.length) {
        emptyChart(elementId, "데이터 없음");
        return;
    }

    const sorted = [...data].sort(
        (a, b) => Number(a[fieldName]) - Number(b[fieldName])
    );

    const trace = {
        x: sorted.map(item => Number(item[fieldName])),
        y: sorted.map(item => item.country),
        type: "bar",
        orientation: "h",
        marker: {
            color: color
        },
        text: sorted.map(
            item => numberFormat(item[fieldName], 2)
        ),
        textposition: "auto",
        hovertemplate:
            "%{y}<br>"
            + yAxisTitle
            + ": %{x:.2f}%<extra></extra>"
    };

    const layout = chartLayout("", yAxisTitle);
    layout.margin.l = 145;

    Plotly.react(
        elementId,
        [trace],
        layout,
        {
            responsive: true,
            displaylogo: false
        }
    );
}

function drawMacroCompareChart(gdp, inflation) {
    const makeAverage = function(data, fieldName) {
        const map = {};

        data.forEach(item => {
            if (!map[item.year]) {
                map[item.year] = [];
            }

            map[item.year].push(Number(item[fieldName]));
        });

        const years = Object.keys(map)
            .map(Number)
            .sort((a, b) => a - b);

        return {
            years: years,
            values: years.map(year => {
                const values = map[year];

                return values.reduce(
                    (sum, value) => sum + value,
                    0
                ) / values.length;
            })
        };
    };

    const gdpAverage = makeAverage(gdp, "gdp_growth");
    const inflationAverage = makeAverage(
        inflation,
        "inflation"
    );

    if (
        !gdpAverage.years.length
        && !inflationAverage.years.length
    ) {
        emptyChart("macroCompareChart", "데이터 없음");
        return;
    }

    const traces = [];

    if (gdpAverage.years.length) {
        traces.push({
            x: gdpAverage.years,
            y: gdpAverage.values,
            mode: "lines+markers",
            type: "scatter",
            name: "GDP 평균",
            line: {
                color: "#43a7f5",
                width: 3
            },
            marker: {
                size: 7
            },
            yaxis: "y"
        });
    }

    if (inflationAverage.years.length) {
        traces.push({
            x: inflationAverage.years,
            y: inflationAverage.values,
            mode: "lines+markers",
            type: "scatter",
            name: "Inflation 평균",
            line: {
                color: "#ff6666",
                width: 3
            },
            marker: {
                size: 7
            },
            yaxis: "y2"
        });
    }

    const layout = chartLayout("", "GDP 성장률 (%)");

    layout.yaxis2 = {
        title: {
            text: "Inflation (%)",
            font: {
                color: "#71879a",
                size: 9
            }
        },
        overlaying: "y",
        side: "right",
        gridcolor: "#142a3d",
        tickfont: {
            color: "#71879a",
            size: 9
        }
    };

    Plotly.react(
        "macroCompareChart",
        traces,
        layout,
        {
            responsive: true,
            displaylogo: false
        }
    );
}

function buildMacroInsights(data, fieldName, metricName) {
    if (!data.length) {
        return [
            "관련 데이터가 없어 정량적 시사점 도출이 제한됩니다."
        ];
    }

    const latest = latestByCountry(data, fieldName);

    const values = latest.map(
        item => Number(item[fieldName])
    );

    const average = values.reduce(
        (sum, value) => sum + value,
        0
    ) / values.length;

    const maximum = latest.reduce(
        (current, item) => {
            return Number(item[fieldName]) > Number(current[fieldName])
                ? item
                : current;
        },
        latest[0]
    );

    const minimum = latest.reduce(
        (current, item) => {
            return Number(item[fieldName]) < Number(current[fieldName])
                ? item
                : current;
        },
        latest[0]
    );

    const yearly = {};

    data.forEach(item => {
        if (!yearly[item.year]) {
            yearly[item.year] = [];
        }

        yearly[item.year].push(
            Number(item[fieldName])
        );
    });

    const years = Object.keys(yearly)
        .map(Number)
        .sort((a, b) => a - b);

    let trend = "비교 가능한 연도 수 부족";

    if (years.length >= 2) {
        const recentYear = years[years.length - 1];
        const previousYear = years[years.length - 2];

        const recentAverage = yearly[recentYear].reduce(
            (sum, value) => sum + value,
            0
        ) / yearly[recentYear].length;

        const previousAverage = yearly[previousYear].reduce(
            (sum, value) => sum + value,
            0
        ) / yearly[previousYear].length;

        const difference = recentAverage - previousAverage;

        trend = difference > 0
            ? "전년 대비 평균 " + metricName + " "
                + numberFormat(difference, 2) + "p 상승"
            : difference < 0
                ? "전년 대비 평균 " + metricName + " "
                    + numberFormat(Math.abs(difference), 2) + "p 하락"
                : "전년 대비 평균 " + metricName + " 유사한 수준 유지";
    }

    return [
        "최신 평균 " + metricName + ": "
            + numberFormat(average, 2) + "%",
        "최고 국가: " + maximum.country + " ("
            + numberFormat(maximum[fieldName], 2) + "%)",
        "최저 국가: " + minimum.country + " ("
            + numberFormat(minimum[fieldName], 2) + "%)",
        "국가 간 격차: "
            + numberFormat(
                Number(maximum[fieldName])
                - Number(minimum[fieldName]),
                2
            )
            + "p",
        trend
    ];
}

function renderInsightList(elementId, insights) {
    const element = document.getElementById(elementId);
    element.innerHTML = "";

    insights.forEach(insight => {
        const item = document.createElement("li");
        item.textContent = insight;
        element.appendChild(item);
    });
}

function renderMacroDashboard() {
    if (!macroData.gdp.length && !macroData.inflation.length) {
        return;
    }

    const selectedRegion = document.getElementById(
        "regionSelect"
    ).value;

    const selectedCountries = getSelectedCountries();

    const gdp = selectedMacroData("gdp");
    const inflation = selectedMacroData("inflation");

    document.getElementById("selectedCountriesKpi").textContent =
        selectedCountries.length;

    document.getElementById("selectedRegionKpi").textContent =
        selectedRegion;

    document.getElementById("latestGdpYearKpi").textContent =
        gdp.length
            ? Math.max(...gdp.map(item => Number(item.year)))
            : "-";

    document.getElementById(
        "latestInflationYearKpi"
    ).textContent = inflation.length
        ? Math.max(...inflation.map(item => Number(item.year)))
        : "-";

    const colors = [
        "#43a7f5",
        "#37ca82",
        "#ff6666",
        "#aa8cf6",
        "#f6a623",
        "#38c6df",
        "#61c98a",
        "#f28585"
    ];

    drawLineChart(
        "gdpLine",
        gdp,
        "gdp_growth",
        "GDP 성장률 (%)",
        colors
    );

    drawLineChart(
        "inflationLine",
        inflation,
        "inflation",
        "Inflation (%)",
        colors.slice().reverse()
    );

    drawMacroCompareChart(gdp, inflation);

    drawBarChart(
        "gdpBar",
        latestByCountry(gdp, "gdp_growth"),
        "gdp_growth",
        "GDP 성장률 (%)",
        "#43a7f5"
    );

    drawBarChart(
        "inflationBar",
        latestByCountry(inflation, "inflation"),
        "inflation",
        "Inflation (%)",
        "#ff6666"
    );

    renderInsightList(
        "gdpInsights",
        buildMacroInsights(gdp, "gdp_growth", "GDP 성장률")
    );

    renderInsightList(
        "inflationInsights",
        buildMacroInsights(inflation, "inflation", "Inflation")
    );
}

function selectedGscpiData() {
    const years = Number(
        document.getElementById("gscpiYearsSelect").value
    );

    if (!gscpiData.gscpi.length) {
        return [];
    }

    const maxYear = Math.max(
        ...gscpiData.gscpi.map(item => Number(item.year))
    );

    const minYear = maxYear - years + 1;

    return gscpiData.gscpi.filter(
        item => Number(item.year) >= minYear
    );
}

function buildGscpiInsights(data) {
    if (!data.length) {
        return [
            "관련 데이터가 없어 공급망 시사점 도출이 제한됩니다."
        ];
    }

    const sorted = [...data].sort(
        (a, b) => new Date(a.date) - new Date(b.date)
    );

    const latest = sorted[sorted.length - 1];
    const last12 = sorted.slice(-12);

    const values = last12.map(item => Number(item.gscpi));

    const average = values.reduce(
        (sum, value) => sum + value,
        0
    ) / values.length;

    const maximum = Math.max(...values);
    const minimum = Math.min(...values);

    let trend = "1년 비교 판단: 데이터 부족";

    if (sorted.length >= 13) {
        const previous = Number(
            sorted[sorted.length - 13].gscpi
        );

        const difference = Number(latest.gscpi) - previous;

        trend = difference > 0
            ? "1년 전 대비 GSCPI "
                + numberFormat(difference, 2) + "p 상승"
            : difference < 0
                ? "1년 전 대비 GSCPI "
                    + numberFormat(Math.abs(difference), 2) + "p 하락"
                : "1년 전 대비 GSCPI 유사한 수준 유지";
    }

    return [
        "최신 시점: " + latest.date.substring(0, 7),
        "최신 GSCPI: " + numberFormat(latest.gscpi, 2),
        "최근 12개월 평균: " + numberFormat(average, 2),
        "최근 12개월 범위: "
            + numberFormat(minimum, 2)
            + " ~ "
            + numberFormat(maximum, 2),
        "최근 수준 평가: "
            + (
                Number(latest.gscpi) > average
                    ? "최근 12개월 평균 상회"
                    : Number(latest.gscpi) < average
                        ? "최근 12개월 평균 하회"
                        : "최근 12개월 평균과 유사"
            ),
        trend
    ];
}

function renderGscpiDashboard() {
    const data = selectedGscpiData();

    if (!data.length) {
        emptyChart("gscpiLine", "데이터 없음");
        emptyChart("gscpiBar", "데이터 없음");
        return;
    }

    const sorted = [...data].sort(
        (a, b) => new Date(a.date) - new Date(b.date)
    );

    const latest12 = sorted.slice(-12);
    const values = latest12.map(item => Number(item.gscpi));
    const latest = sorted[sorted.length - 1];

    const average = values.reduce(
        (sum, value) => sum + value,
        0
    ) / values.length;

    document.getElementById("latestGscpiKpi").textContent =
        latest.date.substring(0, 7);

    document.getElementById("avgGscpiKpi").textContent =
        numberFormat(average, 2);

    document.getElementById("maxGscpiKpi").textContent =
        numberFormat(Math.max(...values), 2);

    document.getElementById("minGscpiKpi").textContent =
        numberFormat(Math.min(...values), 2);

    Plotly.react(
        "gscpiLine",
        [{
            x: sorted.map(item => item.date),
            y: sorted.map(item => Number(item.gscpi)),
            type: "scatter",
            mode: "lines+markers",
            name: "GSCPI",
            line: {
                color: "#37ca82",
                width: 3
            },
            marker: {
                size: 5
            },
            hovertemplate:
                "%{x|%Y-%m}<br>GSCPI: %{y:.2f}<extra></extra>"
        }],
        chartLayout("", "GSCPI"),
        {
            responsive: true,
            displaylogo: false
        }
    );

    Plotly.react(
        "gscpiBar",
        [{
            x: latest12.map(item => item.date.substring(0, 7)),
            y: latest12.map(item => Number(item.gscpi)),
            type: "bar",
            marker: {
                color: "#37ca82"
            },
            hovertemplate:
                "%{x}<br>GSCPI: %{y:.2f}<extra></extra>"
        }],
        chartLayout("", "GSCPI"),
        {
            responsive: true,
            displaylogo: false
        }
    );

    renderInsightList(
        "gscpiInsights",
        buildGscpiInsights(sorted)
    );
}

function csvEscape(value) {
    const text = String(value ?? "");
    return '"' + text.replaceAll('"', '""') + '"';
}

function downloadCsv(rows, fileName) {
    if (!rows.length) {
        showWarning("다운로드할 데이터가 없습니다.");
        return;
    }

    const columns = Object.keys(rows[0]);

    const csv = [
        columns.join(","),
        ...rows.map(row => {
            return columns.map(
                column => csvEscape(row[column])
            ).join(",");
        })
    ].join("\n");

    const blob = new Blob(
        ["\ufeff" + csv],
        {
            type: "text/csv;charset=utf-8;"
        }
    );

    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");

    anchor.href = url;
    anchor.download = fileName;

    document.body.appendChild(anchor);
    anchor.click();
    document.body.removeChild(anchor);

    URL.revokeObjectURL(url);
}

function downloadMacroData(dataset) {
    const gdp = selectedMacroData("gdp");
    const inflation = selectedMacroData("inflation");

    if (dataset === "gdp") {
        downloadCsv(gdp, "worldbank_gdp_growth.csv");
        return;
    }

    if (dataset === "inflation") {
        downloadCsv(
            inflation,
            "worldbank_inflation.csv"
        );
        return;
    }

    const map = new Map();

    gdp.forEach(item => {
        const key = item.code + "|" + item.country + "|" + item.year;

        map.set(key, {
            code: item.code,
            country: item.country,
            region: item.region,
            year: item.year,
            gdp_growth: item.gdp_growth,
            inflation: null
        });
    });

    inflation.forEach(item => {
        const key = item.code + "|" + item.country + "|" + item.year;

        if (!map.has(key)) {
            map.set(key, {
                code: item.code,
                country: item.country,
                region: item.region,
                year: item.year,
                gdp_growth: null,
                inflation: item.inflation
            });
        } else {
            map.get(key).inflation = item.inflation;
        }
    });

    const rows = Array.from(map.values()).sort((a, b) => {
        const countryOrder = a.country.localeCompare(b.country);

        return countryOrder !== 0
            ? countryOrder
            : Number(a.year) - Number(b.year);
    });

    downloadCsv(
        rows,
        "worldbank_gdp_inflation_comparison.csv"
    );
}

function downloadGscpiData() {
    downloadCsv(
        selectedGscpiData(),
        "newyorkfed_gscpi.csv"
    );
}

function showTab(tabName) {
    activeTab = tabName;

    const macroSection = document.getElementById("macroSection");
    const gscpiSection = document.getElementById("gscpiSection");
    const macroTab = document.getElementById("macroTab");
    const gscpiTab = document.getElementById("gscpiTab");

    if (tabName === "macro") {
        macroSection.classList.remove("hidden");
        gscpiSection.classList.add("hidden");
        macroTab.classList.add("active");
        gscpiTab.classList.remove("active");
        renderMacroDashboard();
        return;
    }

    macroSection.classList.add("hidden");
    gscpiSection.classList.remove("hidden");
    macroTab.classList.remove("active");
    gscpiTab.classList.add("active");
    renderGscpiDashboard();
}

async function initializeDashboard() {
    try {
        hideWarning();

        const responses = await Promise.all([
            fetch("./macro_data.json", { cache: "no-store" }),
            fetch("./gscpi_data.json", { cache: "no-store" }),
            fetch("./dashboard_meta.json", { cache: "no-store" })
        ]);

        if (!responses.every(response => response.ok)) {
            throw new Error(
                "정적 데이터 JSON 파일을 불러오지 못했습니다."
            );
        }

        macroData = await responses[0].json();
        gscpiData = await responses[1].json();

        const metaData = await responses[2].json();

        document.getElementById("generatedAt").textContent =
            metaData.generated_at || "-";

        populateRegionSelect();

        const initialCountries = DEFAULT_COUNTRIES.filter(country => {
            return macroData.countries.some(
                item => item.country === country
            );
        });

        populateCountrySelect("전체", initialCountries);

        document.getElementById("loading")
            .classList.add("hidden");

        showTab(activeTab);

    } catch (error) {
        const loading = document.getElementById("loading");

        loading.textContent =
            "대시보드 데이터 로드에 실패했습니다. "
            + "GitHub Actions 실행 결과와 JSON 파일 배포 여부를 확인하세요.";

        showWarning(
            "데이터를 불러오지 못했습니다: " + error.message
        );

        console.error(error);
    }
}

window.addEventListener(
    "DOMContentLoaded",
    initializeDashboard
);
</script>
</body>
</html>
"""


# =========================================================
# 7. Local Flask Development Mode
# =========================================================
@app.route("/")
def index():
    """
    로컬 검증용 화면.
    GitHub Pages 환경에서는 생성된 index.html을 직접 사용한다.
    """
    if not STATIC_HTML_PATH.exists():
        generate_static_html()

    return Response(
        STATIC_HTML,
        mimetype="text/html; charset=utf-8",
    )

@app.route("/macro_data.json")
@app.route("/gscpi_data.json")
@app.route("/dashboard_meta.json")
def serve_json_file():
    """
    중요 수정:
    Flask는 BASE_DIR의 정적 JSON 파일을 자동으로 제공하지 않는다.
    따라서 로컬 개발 모드에서 JSON 파일 404를 방지하기 위한 라우트다.
    """
    allowed_files = {
        "macro_data.json",
        "gscpi_data.json",
        "dashboard_meta.json",
    }

    filename = (
        os.path.basename(
            str(request_path_name())
        )
    )

    if filename not in allowed_files:
        return Response(
            "File not found",
            status=404,
            mimetype="text/plain",
        )

    path = BASE_DIR / filename

    if not path.exists():
        return Response(
            "File not found",
            status=404,
            mimetype="text/plain",
        )

    return send_from_directory(
        BASE_DIR,
        filename,
        mimetype="application/json",
        as_attachment=False,
    )

def request_path_name():
    """
    Flask request import을 전역에서 최소화하기 위한 보조 함수.
    """
    from flask import request

    return request.path.lstrip("/")

@app.route("/download_json/<filename>")
def download_json(filename):
    """
    로컬 개발 시 JSON 데이터 확인 및 다운로드용 엔드포인트.
    """
    allowed_files = {
        "macro_data.json",
        "gscpi_data.json",
        "dashboard_meta.json",
    }

    if filename not in allowed_files:
        return Response(
            "File not found",
            status=404,
            mimetype="text/plain",
        )

    path = BASE_DIR / filename

    if not path.exists():
        return Response(
            "File not found",
            status=404,
            mimetype="text/plain",
        )

    return send_from_directory(
        BASE_DIR,
        filename,
        mimetype="application/json",
        as_attachment=True,
        download_name=filename,
    )

# =========================================================
# 8. GitHub Actions Execution
# =========================================================
def run_cron_job():
    """
    GitHub Actions 전용 실행 함수.

    1) 원천 데이터 최신 수집
    2) SQLite 캐시 갱신
    3) macro_data.json 생성
    4) gscpi_data.json 생성
    5) dashboard_meta.json 생성
    6) index.html 생성
    """
    log("=" * 60)
    log("[CRON] Macro Economies Dashboard 갱신 시작")
    log("=" * 60)

    init_cache_db()

    try:
        data = collect_dashboard_data(force_refresh=True)

        write_static_json_files(data)
        generate_static_html()

        log("[CRON] 정적 대시보드 생성 완료")

    except Exception as error:
        log(f"[ERROR] 정적 대시보드 생성 실패: {error}")
        raise

    log("=" * 60)
    log("[CRON] Macro Economies Dashboard 갱신 종료")
    log("=" * 60)

# =========================================================
# 9. Program Entry Point
# =========================================================
def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "GDP, Inflation & GSCPI Dashboard "
            "(GitHub Pages / Local Flask)"
        )
    )

    parser.add_argument(
        "--cron",
        action="store_true",
        help=(
            "GitHub Actions용 실행 모드: "
            "데이터 수집, SQLite 저장, JSON 및 index.html 생성 후 종료"
        ),
    )

    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="로컬 Flask 서버 Host",
    )

    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help=(
            "로컬 Flask 서버 Port. "
            "0이면 사용 가능한 포트를 자동 탐색"
        ),
    )

    parser.add_argument(
        "--no-open",
        action="store_true",
        help="로컬 실행 시 브라우저 자동 실행 비활성화",
    )

    parser.add_argument(
        "--refresh",
        action="store_true",
        help="로컬 실행 전 최신 데이터를 강제로 수집",
    )

    return parser.parse_args()

def main():
    args = parse_args()

    init_cache_db()

    if args.cron or os.environ.get("GITHUB_ACTIONS") == "true":
        try:
            run_cron_job()
            sys.exit(0)
        except Exception as error:
            log(f"[ERROR] Cron 실행 실패: {error}")
            sys.exit(1)

    try:
        data = collect_dashboard_data(
            force_refresh=args.refresh
        )

        write_static_json_files(data)
        generate_static_html()

    except Exception as error:
        log(f"[WARN] 데이터 갱신 실패: {error}")
        log(
            "[WARN] 기존 정적 파일 또는 SQLite 캐시가 있으면 사용합니다."
        )

        if not STATIC_HTML_PATH.exists():
            generate_static_html()

    host = args.host

    port = (
        args.port
        if args.port > 0
        else find_available_port(host)
    )

    browser_host = "127.0.0.1" if host == "0.0.0.0" else host
    local_url = f"http://{browser_host}:{port}"

    log(f"[INFO] SQLite 캐시 DB: {DB_PATH}")
    log(f"[INFO] Flask 서버 시작: {local_url}")

    if not args.no_open:
        threading.Thread(
            target=wait_and_open_browser,
            args=(local_url, host, port),
            daemon=True,
        ).start()

    app.run(
        host=host,
        port=port,
        debug=False,
        use_reloader=False,
    )

if __name__ == "__main__":
    main()
