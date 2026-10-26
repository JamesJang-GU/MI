# -*- coding: utf-8 -*-
"""
GDP, Inflation & GSCPI Static Dashboard
---------------------------------------
로컬 개발 모드:
    python GDP_Inflation_GSCPI_DB.py

GitHub Actions / GitHub Pages 정적 생성 모드:
    python GDP_Inflation_GSCPI_DB.py --cron

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
    로컬 PC 및 GitHub Actions 환경에서 필요한 패키지를 점검하고 설치한다.
    GSCPI xls/xlsx 파일 처리를 위해 xlrd, openpyxl을 포함한다.
    """
    required_packages = {
        "pandas": "pandas",
        "requests": "requests",
        "flask": "flask",
        "xlrd": "xlrd>=2.0.1",
        "openpyxl": "openpyxl",
    }

    missing_packages = []
    for module_name, pip_name in required_packages.items():
        if importlib.util.find_spec(module_name) is None:
            missing_packages.append(pip_name)

    if missing_packages:
        print(f"[INIT] 필수 패키지 설치 진행: {', '.join(missing_packages)}")
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", *missing_packages]
            )
            print("[INIT] 필수 패키지 설치 완료")
        except Exception as exc:
            print(f"[WARN] 패키지 자동 설치 실패: {exc}")

setup_environment()

import pandas as pd
import requests
from flask import Flask, Response, request, send_from_directory

# =========================================================
# 1. Configuration & Constants
# =========================================================
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "gdp_inflation_gscpi_cache.db"
STATIC_HTML_PATH = BASE_DIR / "index.html"
MACRO_JSON_PATH = BASE_DIR / "macro_data.json"
GSCPI_JSON_PATH = BASE_DIR / "gscpi_data.json"
META_JSON_PATH = BASE_DIR / "dashboard_meta.json"

WORLD_BANK_INDICATORS = {
    "gdp_growth": "NY.GDP.MKTP.KD.ZG",  # Real GDP growth (annual %)
    "inflation": "FP.CPI.TOTL.ZG",      # Inflation, consumer prices (annual %)
}

GSCPI_EXCEL_URL = "https://www.newyorkfed.org/medialibrary/research/interactives/gscpi/downloads/gscpi_data.xlsx"
GSCPI_XLS_FALLBACK_URL = "https://www.newyorkfed.org/medialibrary/research/interactives/gscpi/downloads/gscpi_data.xls"

COUNTRIES_CONFIG = [
    {"code": "USA", "name_kr": "미국", "name_en": "United States", "region": "북미"},
    {"code": "CHN", "name_kr": "중국", "name_en": "China", "region": "아시아"},
    {"code": "KOR", "name_kr": "한국", "name_en": "South Korea", "region": "아시아"},
    {"code": "JPN", "name_kr": "일본", "name_en": "Japan", "region": "아시아"},
    {"code": "DEU", "name_kr": "독일", "name_en": "Germany", "region": "유럽"},
    {"code": "GBR", "name_kr": "영국", "name_en": "United Kingdom", "region": "유럽"},
    {"code": "FRA", "name_kr": "프랑스", "name_en": "France", "region": "유럽"},
    {"code": "IND", "name_kr": "인도", "name_en": "India", "region": "아시아"},
    {"code": "VNM", "name_kr": "베트남", "name_en": "Vietnam", "region": "아시아"},
    {"code": "TWN", "name_kr": "대만", "name_en": "Taiwan", "region": "아시아"},
    {"code": "SGP", "name_kr": "싱가포르", "name_en": "Singapore", "region": "아시아"},
    {"code": "MEX", "name_kr": "멕시코", "name_en": "Mexico", "region": "중남미"},
    {"code": "BRA", "name_kr": "브라질", "name_en": "Brazil", "region": "중남미"},
    {"code": "WLD", "name_kr": "전세계(World)", "name_en": "World", "region": "글로벌"},
    {"code": "EMU", "name_kr": "유로존(Euro Area)", "name_en": "Euro Area", "region": "유럽"},
]

COUNTRY_DICT = {c["code"]: c for c in COUNTRIES_CONFIG}

START_YEAR = 2000
CURRENT_YEAR = datetime.now().year

app = Flask(__name__)

# =========================================================
# 2. Logging & File Helper
# =========================================================
def log(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}")

def atomic_write_text(path: Path, text: str):
    """
    중간 저장 실패로 인한 정적 파일 배포 손상을 방지한다.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(text, encoding="utf-8")
    temp_path.replace(path)

def atomic_write_json(path: Path, data: dict):
    """
    JSON 직렬화 및 원자적 쓰기를 수행한다.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    temp_path.replace(path)

# =========================================================
# 3. SQLite Database Layer
# =========================================================
def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_database():
    """
    SQLite 영속화 스키마를 초기화한다.
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS macro_annual (
                country_code TEXT NOT NULL,
                indicator TEXT NOT NULL,
                year INTEGER NOT NULL,
                value REAL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (country_code, indicator, year)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS gscpi_monthly (
                date TEXT PRIMARY KEY,
                value REAL,
                updated_at TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS dashboard_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.commit()

# =========================================================
# 4. Data Collection (World Bank & NY Fed GSCPI)
# =========================================================
def fetch_world_bank_data():
    """
    World Bank Open API를 통해 연간 실질 GDP 성장률 및 인플레이션 데이터를 수집한다.
    """
    log("[WB] World Bank 거시경제 지표 수집 시작...")
    records = []
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    country_codes = [c["code"] for c in COUNTRIES_CONFIG]
    country_query = ";".join(country_codes)

    for indicator_name, indicator_code in WORLD_BANK_INDICATORS.items():
        url = (
            f"https://api.worldbank.org/v2/country/{country_query}/indicator/{indicator_code}"
            f"?date={START_YEAR}:{CURRENT_YEAR}&format=json&per_page=1000"
        )
        try:
            response = requests.get(url, timeout=30)
            if response.status_code != 200:
                log(f"[WARN] World Bank API 응답 에러 ({indicator_name}): HTTP {response.status_code}")
                continue

            data = response.json()
            if len(data) < 2 or not isinstance(data[1], list):
                log(f"[WARN] World Bank 수신 데이터 이상 ({indicator_name})")
                continue

            for item in data[1]:
                c_code = item.get("countryiso3code") or item.get("country", {}).get("id")
                year_val = item.get("date")
                val = item.get("value")

                if c_code and year_val and c_code in COUNTRY_DICT:
                    try:
                        records.append({
                            "country_code": c_code,
                            "indicator": indicator_name,
                            "year": int(year_val),
                            "value": float(val) if val is not None else None,
                            "updated_at": now_str
                        })
                    except (ValueError, TypeError):
                        continue
        except Exception as exc:
            log(f"[ERROR] World Bank 수집 실패 ({indicator_name}): {exc}")

    if records:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.executemany("""
                INSERT OR REPLACE INTO macro_annual (country_code, indicator, year, value, updated_at)
                VALUES (:country_code, :indicator, :year, :value, :updated_at)
            """, records)
            conn.commit()
        log(f"[WB] 총 {len(records)}건의 거시경제 데이터 갱신 완료")

def fetch_gscpi_data():
    """
    New York Fed 공식 공급망 압력 지수(GSCPI) 시계열을 수집한다.
    """
    log("[GSCPI] New York Fed GSCPI 데이터 수집 시작...")
    records = []
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    content = None
    is_xlsx = True

    try:
        res = requests.get(GSCPI_EXCEL_URL, headers=headers, timeout=30)
        if res.status_code == 200 and len(res.content) > 1000:
            content = res.content
        else:
            log("[WARN] XLSX 수신 실패, XLS 대체 URL 시도...")
            res_xls = requests.get(GSCPI_XLS_FALLBACK_URL, headers=headers, timeout=30)
            if res_xls.status_code == 200:
                content = res_xls.content
                is_xlsx = False
    except Exception as exc:
        log(f"[ERROR] GSCPI 다운로드 요청 실패: {exc}")
        return

    if not content:
        log("[ERROR] GSCPI 원천 파일을 다운로드하지 못했습니다.")
        return

    try:
        engine = "openpyxl" if is_xlsx else "xlrd"
        df = pd.read_excel(BytesIO(content), sheet_name=0, engine=engine)

        date_col = None
        gscpi_col = None

        for col in df.columns:
            c_str = str(col).strip().lower()
            if "date" in c_str or "month" in c_str or "time" in c_str:
                date_col = col
            if "gscpi" in c_str:
                gscpi_col = col

        if date_col is None or gscpi_col is None:
            # 헤더 인덱스 보정 탐색
            for i in range(min(10, len(df))):
                row_vals = [str(x).strip().lower() for x in df.iloc[i].values]
                if any("date" in x for x in row_vals) and any("gscpi" in x for x in row_vals):
                    df.columns = df.iloc[i]
                    df = df.iloc[i + 1:].reset_index(drop=True)
                    for col in df.columns:
                        c_str = str(col).strip().lower()
                        if "date" in c_str or "month" in c_str:
                            date_col = col
                        if "gscpi" in c_str:
                            gscpi_col = col
                    break

        if date_col is None or gscpi_col is None:
            # 1, 2열을 기본 fallback으로 매핑
            date_col = df.columns[0]
            gscpi_col = df.columns[1]

        for _, row in df.iterrows():
            raw_date = row[date_col]
            raw_val = row[gscpi_col]

            if pd.isna(raw_date) or pd.isna(raw_val):
                continue

            try:
                if isinstance(raw_date, (datetime, pd.Timestamp)):
                    date_str = raw_date.strftime("%Y-%m")
                else:
                    date_str = pd.to_datetime(str(raw_date).strip()).strftime("%Y-%m")
                val_float = float(raw_val)
                records.append({
                    "date": date_str,
                    "value": round(val_float, 4),
                    "updated_at": now_str
                })
            except Exception:
                continue

        if records:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.executemany("""
                    INSERT OR REPLACE INTO gscpi_monthly (date, value, updated_at)
                    VALUES (:date, :value, :updated_at)
                """, records)
                conn.commit()
            log(f"[GSCPI] 총 {len(records)}개 월별 GSCPI 레코드 갱신 완료")
    except Exception as exc:
        log(f"[ERROR] GSCPI 파싱 실패: {exc}")

def update_dashboard_metadata():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO dashboard_meta (key, value, updated_at)
            VALUES ('last_updated', :val, :now)
        """, {"val": now_str, "now": now_str})
        conn.commit()

# =========================================================
# 5. Static Exporters (JSON & HTML)
# =========================================================
def export_static_json():
    """
    SQLite 캐시 데이터를 기반으로 프론트엔드 소비용 JSON 파일을 생성한다.
    """
    with get_db_connection() as conn:
        # 1. Macro JSON
        macro_df = pd.read_sql_query(
            "SELECT country_code, indicator, year, value FROM macro_annual ORDER BY year ASC",
            conn
        )

        gdp_list = []
        inflation_list = []

        for _, row in macro_df.iterrows():
            item = {
                "country": COUNTRY_DICT.get(row["country_code"], {}).get("name_kr", row["country_code"]),
                "country_code": row["country_code"],
                "region": COUNTRY_DICT.get(row["country_code"], {}).get("region", "기타"),
                "year": int(row["year"]),
                "value": row["value"]
            }
            if row["indicator"] == "gdp_growth":
                gdp_list.append(item)
            elif row["indicator"] == "inflation":
                inflation_list.append(item)

        macro_payload = {
            "countries": COUNTRIES_CONFIG,
            "gdp": gdp_list,
            "inflation": inflation_list
        }
        atomic_write_json(MACRO_JSON_PATH, macro_payload)
        log(f"[STATIC] 생성 완료: {MACRO_JSON_PATH.name}")

        # 2. GSCPI JSON
        gscpi_df = pd.read_sql_query(
            "SELECT date, value FROM gscpi_monthly ORDER BY date ASC",
            conn
        )
        gscpi_list = [
            {"date": r["date"], "value": r["value"]}
            for _, r in gscpi_df.iterrows()
        ]
        atomic_write_json(GSCPI_JSON_PATH, {"gscpi": gscpi_list})
        log(f"[STATIC] 생성 완료: {GSCPI_JSON_PATH.name}")

        # 3. Meta JSON
        meta_row = conn.execute(
            "SELECT value FROM dashboard_meta WHERE key='last_updated'"
        ).fetchone()
        generated_at = meta_row["value"] if meta_row else datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        atomic_write_json(META_JSON_PATH, {
            "generated_at": generated_at,
            "total_countries": len(COUNTRIES_CONFIG),
            "macro_records": len(macro_df),
            "gscpi_records": len(gscpi_list)
        })
        log(f"[STATIC] 생성 완료: {META_JSON_PATH.name}")

def generate_static_html():
    """
    정적 index.html을 현재 Python 파일과 동일한 디렉터리에 생성한다.
    """
    atomic_write_text(STATIC_HTML_PATH, STATIC_HTML)
    log(f"[STATIC] 생성 완료: {STATIC_HTML_PATH.name}")

# =========================================================
# 6. Embedded Static HTML Template (Bloomberg/MI Terminal)
# =========================================================
STATIC_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Global Macro & GSCPI Dashboard</title>
    <!-- Chart.js & Plugins -->
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/luxon@3.4.4/build/global/luxon.min.js"></script>
    <style>
        :root {
            --bg-primary: #0a0e17;
            --bg-secondary: #131a29;
            --bg-card: #182234;
            --border-color: #26354d;
            --text-primary: #f0f4f8;
            --text-secondary: #8b9bb4;
            --text-muted: #57677f;
            --accent-blue: #0088ff;
            --accent-cyan: #00d2ff;
            --accent-green: #00e676;
            --accent-red: #ff3d71;
            --accent-amber: #ffaa00;
            --font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            --font-mono: "SF Pro Text Mono", "Fira Code", Consolas, monospace;
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }

        body {
            background-color: var(--bg-primary);
            color: var(--text-primary);
            font-family: var(--font-family);
            font-size: 13px;
            line-height: 1.5;
            padding: 16px;
        }

        /* Top Header */
        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 12px;
            margin-bottom: 16px;
        }

        .header-title-group h1 {
            font-size: 18px;
            font-weight: 700;
            letter-spacing: 0.5px;
            color: var(--text-primary);
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .header-title-group .badge {
            font-size: 10px;
            padding: 2px 6px;
            border-radius: 4px;
            background-color: var(--border-color);
            color: var(--accent-cyan);
            font-family: var(--font-mono);
            font-weight: 600;
        }

        .meta-info {
            font-size: 11px;
            color: var(--text-secondary);
            font-family: var(--font-mono);
            text-align: right;
        }

        /* Nav Tabs */
        .tab-bar {
            display: flex;
            gap: 8px;
            margin-bottom: 16px;
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 8px;
        }

        .tab-btn {
            background-color: var(--bg-secondary);
            border: 1px solid var(--border-color);
            color: var(--text-secondary);
            padding: 6px 14px;
            border-radius: 4px;
            font-size: 12px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s ease;
        }

        .tab-btn:hover {
            background-color: var(--border-color);
            color: var(--text-primary);
        }

        .tab-btn.active {
            background-color: var(--accent-blue);
            border-color: var(--accent-blue);
            color: #ffffff;
        }

        /* Filter Controls */
        .controls-card {
            background-color: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 6px;
            padding: 12px 16px;
            margin-bottom: 16px;
            display: flex;
            flex-wrap: wrap;
            gap: 16px;
            align-items: center;
        }

        .control-group {
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .control-label {
            font-size: 11px;
            color: var(--text-secondary);
            font-weight: 600;
            text-transform: uppercase;
        }

        select, input[type="range"] {
            background-color: var(--bg-secondary);
            border: 1px solid var(--border-color);
            color: var(--text-primary);
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 12px;
            outline: none;
        }

        select:focus {
            border-color: var(--accent-blue);
        }

        .country-chips {
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
            max-width: 600px;
        }

        .chip {
            font-size: 11px;
            background-color: var(--bg-secondary);
            border: 1px solid var(--border-color);
            color: var(--text-secondary);
            padding: 2px 8px;
            border-radius: 12px;
            cursor: pointer;
            user-select: none;
            transition: all 0.15s;
        }

        .chip.active {
            background-color: #1a365d;
            border-color: var(--accent-blue);
            color: var(--accent-cyan);
            font-weight: 600;
        }

        .btn-action {
            margin-left: auto;
            background-color: var(--bg-secondary);
            border: 1px solid var(--border-color);
            color: var(--text-secondary);
            padding: 6px 12px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
        }

        .btn-action:hover {
            color: var(--text-primary);
            border-color: var(--accent-cyan);
        }

        /* Chart & KPI Layout */
        .dashboard-grid {
            display: grid;
            grid-template-columns: 1fr;
            gap: 16px;
            margin-bottom: 16px;
        }

        .card {
            background-color: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 6px;
            padding: 16px;
            position: relative;
        }

        .card-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 12px;
        }

        .card-title {
            font-size: 13px;
            font-weight: 700;
            color: var(--text-primary);
            display: flex;
            align-items: center;
            gap: 6px;
        }

        .chart-container {
            position: relative;
            height: 380px;
            width: 100%;
        }

        /* Data Table */
        .table-container {
            overflow-x: auto;
            max-height: 400px;
            border: 1px solid var(--border-color);
            border-radius: 4px;
        }

        table {
            width: 100%;
            border-collapse: collapse;
            font-family: var(--font-mono);
            font-size: 11px;
            text-align: right;
        }

        th, td {
            padding: 6px 10px;
            border-bottom: 1px solid var(--border-color);
        }

        th {
            background-color: var(--bg-secondary);
            color: var(--text-secondary);
            font-weight: 600;
            position: sticky;
            top: 0;
            z-index: 2;
        }

        th:first-child, td:first-child {
            text-align: left;
            position: sticky;
            left: 0;
            background-color: var(--bg-card);
            z-index: 1;
        }

        th:first-child {
            z-index: 3;
            background-color: var(--bg-secondary);
        }

        tbody tr:hover {
            background-color: rgba(255, 255, 255, 0.02);
        }

        .val-positive { color: var(--accent-green); }
        .val-negative { color: var(--accent-red); }

        /* Status & Alert */
        #loading {
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background-color: rgba(10, 14, 23, 0.85);
            display: flex;
            justify-content: center;
            align-items: center;
            font-size: 14px;
            font-weight: 600;
            color: var(--accent-cyan);
            z-index: 9999;
        }

        .hidden { display: none !important; }

        .banner-warning {
            background-color: rgba(255, 61, 113, 0.15);
            border: 1px solid var(--accent-red);
            color: #ff94af;
            padding: 8px 12px;
            border-radius: 4px;
            margin-bottom: 12px;
            font-size: 12px;
        }
    </style>
</head>
<body>

    <div id="loading">데이터 세트를 초기화하고 있습니다...</div>

    <header>
        <div class="header-title-group">
            <h1>GLOBAL MACRO & SUPPLY CHAIN INTELLIGENCE</h1>
            <span class="badge">PRO TERMINAL</span>
        </div>
        <div class="meta-info">
            LAST SYNC: <span id="generatedAt">-</span><br>
            SOURCES: WORLD BANK / NY FED GSCPI
        </div>
    </header>

    <div id="warningBanner" class="banner-warning hidden"></div>

    <div class="tab-bar">
        <button class="tab-btn active" onclick="switchTab('gdp')">실질 GDP 성장률 (Real GDP Growth)</button>
        <button class="tab-btn" onclick="switchTab('inflation')">소비자물가 인플레이션 (CPI Inflation)</button>
        <button class="tab-btn" onclick="switchTab('gscpi')">글로벌 공급망 압력지수 (NY Fed GSCPI)</button>
    </div>

    <!-- Macro Filters -->
    <div id="macroControls" class="controls-card">
        <div class="control-group">
            <span class="control-label">권역 필터:</span>
            <select id="regionSelect" onchange="onRegionChange()">
                <option value="전체">전체 권역</option>
            </select>
        </div>
        <div class="control-group">
            <span class="control-label">국가 선택:</span>
            <div id="countryChips" class="country-chips"></div>
        </div>
        <button class="btn-action" onclick="exportCurrentViewCSV()">CSV 다운로드</button>
    </div>

    <!-- GSCPI Filters -->
    <div id="gscpiControls" class="controls-card hidden">
        <div class="control-group">
            <span class="control-label">조회 기간:</span>
            <select id="gscpiRangeSelect" onchange="renderGSCPIView()">
                <option value="all">전체 (2000 ~ 현재)</option>
                <option value="10y">최근 10년</option>
                <option value="5y">최근 5년</option>
                <option value="3y" selected>최근 3년</option>
            </select>
        </div>
        <button class="btn-action" onclick="exportGscpiCSV()">GSCPI CSV 다운로드</button>
    </div>

    <!-- Main View Grid -->
    <div class="dashboard-grid">
        <div class="card">
            <div class="card-header">
                <span class="card-title" id="chartTitle">시계열 트렌드 차트</span>
            </div>
            <div class="chart-container">
                <canvas id="mainChart"></canvas>
            </div>
        </div>

        <div class="card">
            <div class="card-header">
                <span class="card-title" id="tableTitle">데이터 매트릭스</span>
            </div>
            <div class="table-container" id="tableContainer">
                <table id="dataTable">
                    <thead id="tableHead"></thead>
                    <tbody id="tableBody"></tbody>
                </table>
            </div>
        </div>
    </div>

    <script>
        const DEFAULT_COUNTRIES = ["USA", "CHN", "KOR", "DEU", "JPN", "WLD"];
        const COLOR_PALETTE = [
            "#00d2ff", "#00e676", "#ffaa00", "#ff3d71",
            "#b388ff", "#ffd600", "#ff6e40", "#00b0ff",
            "#1de9b6", "#ff4081", "#7c4dff", "#c6ff00"
        ];

        let activeTab = "gdp";
        let macroData = { countries: [], gdp: [], inflation: [] };
        let gscpiData = { gscpi: [] };
        let selectedCountries = new Set(DEFAULT_COUNTRIES);
        let mainChartInstance = null;

        function getStaticAssetUrl(fileName) {
            return new URL(fileName, document.baseURI).href;
        }

        async function fetchStaticJson(fileName) {
            const url = getStaticAssetUrl(fileName);
            const response = await fetch(url, {
                cache: "no-store",
                headers: { "Accept": "application/json" }
            });
            if (!response.ok) {
                throw new Error(`${fileName} 로드 실패 (HTTP ${response.status}) | ${url}`);
            }
            return await response.json();
        }

        function showWarning(msg) {
            const w = document.getElementById("warningBanner");
            w.textContent = msg;
            w.classList.remove("hidden");
        }

        function hideWarning() {
            document.getElementById("warningBanner").classList.add("hidden");
        }

        async function initializeDashboard() {
            try {
                hideWarning();
                const [loadedMacro, loadedGscpi, metaData] = await Promise.all([
                    fetchStaticJson("macro_data.json"),
                    fetchStaticJson("gscpi_data.json"),
                    fetchStaticJson("dashboard_meta.json")
                ]);

                macroData = {
                    countries: loadedMacro.countries || [],
                    gdp: loadedMacro.gdp || [],
                    inflation: loadedMacro.inflation || []
                };
                gscpiData = {
                    gscpi: loadedGscpi.gscpi || []
                };

                document.getElementById("generatedAt").textContent = metaData.generated_at || "-";

                populateRegionSelect();
                populateCountryChips("전체");

                document.getElementById("loading").classList.add("hidden");
                switchTab("gdp");

            } catch (error) {
                const loading = document.getElementById("loading");
                loading.textContent = "대시보드 데이터 로드 실패. GitHub Pages 배포 상태 및 JSON 파일을 확인하십시오.";
                showWarning("데이터 로드 에러: " + error.message);
                console.error("[INIT ERROR]", error);
            }
        }

        function populateRegionSelect() {
            const select = document.getElementById("regionSelect");
            const regions = Array.from(new Set(macroData.countries.map(c => c.region)));
            select.innerHTML = '<option value="전체">전체 권역</option>';
            regions.forEach(r => {
                if (r) {
                    const opt = document.createElement("option");
                    opt.value = r;
                    opt.textContent = r;
                    select.appendChild(opt);
                }
            });
        }

        function populateCountryChips(region) {
            const container = document.getElementById("countryChips");
            container.innerHTML = "";
            const filtered = region === "전체"
                ? macroData.countries
                : macroData.countries.filter(c => c.region === region);

            filtered.forEach(c => {
                const chip = document.createElement("div");
                chip.className = "chip" + (selectedCountries.has(c.code) ? " active" : "");
                chip.textContent = c.name_kr;
                chip.onclick = () => toggleCountry(c.code);
                container.appendChild(chip);
            });
        }

        function onRegionChange() {
            const region = document.getElementById("regionSelect").value;
            populateCountryChips(region);
        }

        function toggleCountry(code) {
            if (selectedCountries.has(code)) {
                if (selectedCountries.size > 1) selectedCountries.delete(code);
            } else {
                selectedCountries.add(code);
            }
            populateCountryChips(document.getElementById("regionSelect").value);
            renderMacroView();
        }

        function switchTab(tab) {
            activeTab = tab;
            document.querySelectorAll(".tab-btn").forEach((btn, idx) => {
                btn.classList.toggle("active",
                    (tab === "gdp" && idx === 0) ||
                    (tab === "inflation" && idx === 1) ||
                    (tab === "gscpi" && idx === 2)
                );
            });

            if (tab === "gscpi") {
                document.getElementById("macroControls").classList.add("hidden");
                document.getElementById("gscpiControls").classList.remove("hidden");
                renderGSCPIView();
            } else {
                document.getElementById("macroControls").classList.remove("hidden");
                document.getElementById("gscpiControls").classList.add("hidden");
                renderMacroView();
            }
        }

        function renderMacroView() {
            const isGDP = activeTab === "gdp";
            document.getElementById("chartTitle").textContent = isGDP
                ? "연간 실질 GDP 성장률 (%) - Real GDP Growth Rate"
                : "연간 소비자물가 상승률 (%) - CPI Inflation Rate";
            document.getElementById("tableTitle").textContent = isGDP
                ? "실질 GDP 성장률 매트릭스"
                : "소비자물가 상승률 매트릭스";

            const rawList = isGDP ? macroData.gdp : macroData.inflation;
            const years = Array.from(new Set(rawList.map(d => d.year))).sort((a, b) => a - b);
            const activeCountryList = macroData.countries.filter(c => selectedCountries.has(c.code));

            // Chart Dataset Prep
            const datasets = activeCountryList.map((c, idx) => {
                const cRecords = rawList.filter(d => d.country_code === c.code);
                const valMap = new Map(cRecords.map(r => [r.year, r.value]));
                const data = years.map(y => valMap.has(y) ? valMap.get(y) : null);
                const color = COLOR_PALETTE[idx % COLOR_PALETTE.length];

                return {
                    label: c.name_kr,
                    data: data,
                    borderColor: color,
                    backgroundColor: color,
                    borderWidth: 2,
                    tension: 0.2,
                    pointRadius: 2,
                    spanGaps: true
                };
            });

            renderChart(years, datasets, "%");
            renderMacroTable(years, activeCountryList, rawList);
        }

        function renderGSCPIView() {
            document.getElementById("chartTitle").textContent = "글로벌 공급망 압력지수 (NY Fed GSCPI) - 월별 시계열";
            document.getElementById("tableTitle").textContent = "GSCPI 데이터 이력 (0 = 평균 수준)";

            const range = document.getElementById("gscpiRangeSelect").value;
            let list = [...gscpiData.gscpi].sort((a, b) => a.date.localeCompare(b.date));

            if (range === "3y") list = list.slice(-36);
            else if (range === "5y") list = list.slice(-60);
            else if (range === "10y") list = list.slice(-120);

            const labels = list.map(d => d.date);
            const values = list.map(d => d.value);

            const datasets = [{
                label: "GSCPI 지수",
                data: values,
                borderColor: "#00d2ff",
                backgroundColor: "rgba(0, 210, 255, 0.1)",
                borderWidth: 2,
                fill: true,
                tension: 0.1,
                pointRadius: values.length > 60 ? 0 : 2
            }];

            renderChart(labels, datasets, " Index");
            renderGscpiTable(list);
        }

        function renderChart(labels, datasets, unit) {
            const ctx = document.getElementById("mainChart").getContext("2d");
            if (mainChartInstance) mainChartInstance.destroy();

            mainChartInstance = new Chart(ctx, {
                type: "line",
                data: { labels, datasets },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: { mode: "index", intersect: false },
                    plugins: {
                        legend: {
                            position: "top",
                            labels: { color: "#8b9bb4", font: { size: 11 } }
                        },
                        tooltip: {
                            callbacks: {
                                label: (ctx) => ` ${ctx.dataset.label}: ${ctx.raw !== null ? ctx.raw.toFixed(2) + unit : 'N/A'}`
                            }
                        }
                    },
                    scales: {
                        x: {
                            grid: { color: "rgba(38, 53, 77, 0.5)" },
                            ticks: { color: "#57677f", font: { size: 10 } }
                        },
                        y: {
                            grid: { color: "rgba(38, 53, 77, 0.5)" },
                            ticks: {
                                color: "#57677f",
                                font: { size: 10 },
                                callback: (v) => v + unit
                            }
                        }
                    }
                }
            });
        }

        function renderMacroTable(years, countries, rawList) {
            const thead = document.getElementById("tableHead");
            const tbody = document.getElementById("tableBody");
            thead.innerHTML = "";
            tbody.innerHTML = "";

            const headRow = document.createElement("tr");
            headRow.innerHTML = "<th>국가 / 지표</th>" + years.map(y => `<th>${y}</th>`).join("");
            thead.appendChild(headRow);

            countries.forEach(c => {
                const tr = document.createElement("tr");
                const cRecords = rawList.filter(d => d.country_code === c.code);
                const valMap = new Map(cRecords.map(r => [r.year, r.value]));

                let rowHtml = `<td>${c.name_kr} (${c.code})</td>`;
                years.forEach(y => {
                    if (valMap.has(y) && valMap.get(y) !== null) {
                        const val = valMap.get(y);
                        const cls = val > 0 ? "val-positive" : (val < 0 ? "val-negative" : "");
                        rowHtml += `<td class="${cls}">${val.toFixed(2)}%</td>`;
                    } else {
                        rowHtml += `<td style="color:#415168">-</td>`;
                    }
                });
                tr.innerHTML = rowHtml;
                tbody.appendChild(tr);
            });
        }

        function renderGscpiTable(list) {
            const thead = document.getElementById("tableHead");
            const tbody = document.getElementById("tableBody");
            thead.innerHTML = "<tr><th>연월 (YYYY-MM)</th><th>GSCPI 압력 지수</th><th>상태 판별 (0 = Normal)</th></tr>";
            tbody.innerHTML = "";

            [...list].reverse().forEach(row => {
                const tr = document.createElement("tr");
                const val = row.value;
                const cls = val > 0 ? "val-negative" : "val-positive";
                const status = val > 1.0 ? "공급망 압력 매우 높음" : (val > 0.5 ? "공급망 압력 상승" : (val < -0.5 ? "공급망 완화" : "정상 범위"));
                tr.innerHTML = `
                    <td>${row.date}</td>
                    <td class="${cls}">${val.toFixed(4)}</td>
                    <td style="color:var(--text-secondary)">${status}</td>
                `;
                tbody.appendChild(tr);
            });
        }

        function exportCurrentViewCSV() {
            const isGDP = activeTab === "gdp";
            const rawList = isGDP ? macroData.gdp : macroData.inflation;
            const years = Array.from(new Set(rawList.map(d => d.year))).sort((a, b) => a - b);
            const activeCountries = macroData.countries.filter(c => selectedCountries.has(c.code));

            let csv = "Country,Code,Region," + years.join(",") + "\\n";
            activeCountries.forEach(c => {
                const cRecords = rawList.filter(d => d.country_code === c.code);
                const valMap = new Map(cRecords.map(r => [r.year, r.value]));
                const row = [c.name_kr, c.code, c.region];
                years.forEach(y => {
                    row.push(valMap.has(y) && valMap.get(y) !== null ? valMap.get(y) : "");
                });
                csv += row.join(",") + "\\n";
            });

            downloadCSV(csv, `${activeTab}_matrix.csv`);
        }

        function exportGscpiCSV() {
            let csv = "Date,GSCPI\\n";
            gscpiData.gscpi.forEach(r => {
                csv += `${r.date},${r.value}\\n`;
            });
            downloadCSV(csv, "ny_fed_gscpi.csv");
        }

        function downloadCSV(csvContent, fileName) {
            const blob = new Blob(["\\ufeff" + csvContent], { type: "text/csv;charset=utf-8;" });
            const link = document.createElement("a");
            link.href = URL.createObjectURL(blob);
            link.download = fileName;
            link.click();
        }

        document.addEventListener("DOMContentLoaded", initializeDashboard);
    </script>
</body>
</html>
"""

# =========================================================
# 7. Flask Server Routes (Local Development)
# =========================================================
@app.route("/")
def index():
    if not STATIC_HTML_PATH.exists():
        generate_static_html()
    return send_from_directory(BASE_DIR, "index.html", mimetype="text/html")

@app.route("/macro_data.json")
@app.route("/gscpi_data.json")
@app.route("/dashboard_meta.json")
def serve_json_file():
    """
    로컬 Flask 개발 환경에서 정적 JSON 파일을 안전하게 서빙한다.
    """
    allowed_files = {
        "macro_data.json",
        "gscpi_data.json",
        "dashboard_meta.json",
    }
    filename = Path(request.path).name

    if filename not in allowed_files:
        return Response("File not found", status=404, mimetype="text/plain")

    target_path = BASE_DIR / filename
    if not target_path.exists():
        export_static_json()

    return send_from_directory(
        BASE_DIR,
        filename,
        mimetype="application/json",
        as_attachment=False
    )

# =========================================================
# 8. Execution Pipelines (Cron vs Local)
# =========================================================
def run_cron_pipeline():
    """
    GitHub Actions 배치 실행 파이프라인
    """
    log("[CRON] 정적 배치 갱신 프로세스 시작")
    init_database()
    fetch_world_bank_data()
    fetch_gscpi_data()
    update_dashboard_metadata()
    export_static_json()
    generate_static_html()
    log("[CRON] 대시보드 정적 아티팩트 빌드 완료")

def wait_and_open_browser(url, host, port):
    time.sleep(1.0)
    for _ in range(30):
        try:
            with socket.create_connection((host, port), timeout=1):
                webbrowser.open(url)
                return
        except OSError:
            time.sleep(0.5)

def main():
    parser = argparse.ArgumentParser(description="Macro Economics & GSCPI Analytics Service")
    parser.add_argument("--cron", action="store_true", help="GitHub Actions 정적 빌드 모드")
    parser.add_argument("--host", default="127.0.0.1", help="Flask 바인딩 호스트")
    parser.add_argument("--port", type=int, default=8000, help="Flask 실행 포트")
    parser.add_argument("--no-open", action="store_true", help="브라우저 자동 열기 비활성화")
    args = parser.parse_args()

    if args.cron:
        run_cron_pipeline()
        return

    # 로컬 개발 모드
    init_database()
    if not DB_PATH.exists() or not MACRO_JSON_PATH.exists() or not GSCPI_JSON_PATH.exists():
        log("[LOCAL] 초기 데이터 수집 실행...")
        fetch_world_bank_data()
        fetch_gscpi_data()
        update_dashboard_metadata()
        export_static_json()

    generate_static_html()

    local_url = f"http://{args.host}:{args.port}"
    log(f"[INFO] SQLite DB: {DB_PATH}")
    log(f"[INFO] Flask 로컬 서버 시작: {local_url}")

    if not args.no_open:
        threading.Thread(
            target=wait_and_open_browser,
            args=(local_url, args.host, args.port),
            daemon=True
        ).start()

    app.run(
        host=args.host,
        port=args.port,
        debug=False,
        use_reloader=False
    )

if __name__ == "__main__":
    main()
