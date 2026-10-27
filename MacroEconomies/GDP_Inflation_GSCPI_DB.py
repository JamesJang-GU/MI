# -*- coding: utf-8 -*-
"""
GDP, Inflation & GSCPI Static Dashboard Generator
-------------------------------------------------
수집 파이프라인:
    1. World Bank API: GDP Growth Rate (NY.GDP.MKTP.KD.ZG)
    2. World Bank API: CPI Inflation Rate (FP.CPI.TOTL.ZG)
    3. New York Fed Playwright Headless: GSCPI Chart Network JSON Extraction
       (수집 실패 시 기존 SQLite 캐시 보존 및 빈 JSON 배포 방지)

실행 모드:
    - 로컬 개발 모드:
        python GDP_Inflation_GSCPI_DB.py
    - 정적 생성 모드 (GitHub Actions / 배포용):
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
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone, timedelta
from io import BytesIO, StringIO
from pathlib import Path
from urllib.parse import urlparse

# =========================================================
# 0. Dependency Setup & Imports
# =========================================================

def setup_environment():
    """
    로컬 PC 및 GitHub Actions 환경에서 필요한 패키지를 점검하고 설치한다.
    """
    required_packages = {
        "pandas": "pandas",
        "requests": "requests",
        "flask": "flask",
        "xlrd": "xlrd",
        "openpyxl": "openpyxl",
        "playwright": "playwright"
    }

    missing_packages = []
    for module_name, package_name in required_packages.items():
        if importlib.util.find_spec(module_name) is None:
            missing_packages.append(package_name)

    if missing_packages:
        print(f"[INIT] 누락된 패키지 감지 및 자동 설치 진행: {missing_packages}")
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", *missing_packages],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            print("[INIT] 필수 패키지 설치 완료.")
        except Exception as exc:
            print(f"[WARN] 패키지 자동 설치 실패: {exc}", file=sys.stderr)

setup_environment()

import pandas as pd
import requests
from flask import Flask, jsonify, send_from_directory
from playwright.sync_api import sync_playwright

# =========================================================
# 1. Global Configurations & Path Standards
# =========================================================

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "gdp_inflation_gscpi_cache.db"
INDEX_HTML_PATH = BASE_DIR / "index.html"
MACRO_JSON_PATH = BASE_DIR / "macro_data.json"
GSCPI_JSON_PATH = BASE_DIR / "gscpi_data.json"
META_JSON_PATH = BASE_DIR / "dashboard_meta.json"

KST_TIMEZONE = timezone(timedelta(hours=9))

GSCPI_PAGE_URL = (
    "https://www.newyorkfed.org/research/policy/"
    "global-supply-chain-pressure-index"
)

# World Bank 주요 관측 대상 국가 (ISO-2/ISO-3 매핑)
TARGET_COUNTRIES = {
    "WLD": {"name": "World", "region": "Global", "iso2": "1W"},
    "USA": {"name": "United States", "region": "Americas", "iso2": "US"},
    "CHN": {"name": "China", "region": "Asia", "iso2": "CN"},
    "KOR": {"name": "South Korea", "region": "Asia", "iso2": "KR"},
    "JPN": {"name": "Japan", "region": "Asia", "iso2": "JP"},
    "DEU": {"name": "Germany", "region": "Europe", "iso2": "DE"},
    "GBR": {"name": "United Kingdom", "region": "Europe", "iso2": "GB"},
    "FRA": {"name": "France", "region": "Europe", "iso2": "FR"},
    "IND": {"name": "India", "region": "Asia", "iso2": "IN"},
    "VNM": {"name": "Vietnam", "region": "Asia", "iso2": "VN"},
    "SGP": {"name": "Singapore", "region": "Asia", "iso2": "SG"},
    "BRA": {"name": "Brazil", "region": "Americas", "iso2": "BR"},
    "MEX": {"name": "Mexico", "region": "Americas", "iso2": "MX"},
    "EUU": {"name": "European Union", "region": "Europe", "iso2": "EU"}
}

def log(message: str):
    timestamp = datetime.now(KST_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S KST")
    print(f"[{timestamp}] {message}", flush=True)

def now_kst_iso() -> str:
    return datetime.now(KST_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")

# =========================================================
# 2. Database Schema & Migration
# =========================================================

def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    try:
        cur = conn.cursor()

        # 거시경제 지표 시계열 테이블 (GDP, Inflation)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS macro_series (
                country_code TEXT NOT NULL,
                country_name TEXT NOT NULL,
                indicator_code TEXT NOT NULL,
                indicator_name TEXT NOT NULL,
                year INTEGER NOT NULL,
                value REAL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (country_code, indicator_code, year)
            )
        """)

        # GSCPI 월별 시계열 테이블
        cur.execute("""
            CREATE TABLE IF NOT EXISTS gscpi_series (
                period TEXT PRIMARY KEY,
                gscpi_value REAL NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        # 메타데이터/캐시 제어 테이블
        cur.execute("""
            CREATE TABLE IF NOT EXISTS metadata_cache (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        conn.commit()
    finally:
        conn.close()

# =========================================================
# 3. GSCPI Data Ingestion (Playwright Headless + JSON Sniffer)
# =========================================================

def parse_gscpi_period(value):
    """다양한 날짜 형식을 표준 월 단위(YYYY-MM)로 정규화한다."""
    if value is None:
        return None

    try:
        if isinstance(value, (int, float)):
            numeric = float(value)
            if numeric > 10_000_000_000:
                parsed = datetime.fromtimestamp(numeric / 1000, tz=timezone.utc)
                return parsed.strftime("%Y-%m")
            if numeric > 1_000_000_000:
                parsed = datetime.fromtimestamp(numeric, tz=timezone.utc)
                return parsed.strftime("%Y-%m")

        raw = str(value).strip()
        if not raw:
            return None

        parsed = pd.to_datetime(raw, errors="coerce")
        if pd.isna(parsed):
            return None

        return parsed.strftime("%Y-%m")
    except Exception:
        return None

def parse_gscpi_value(value):
    """숫자 및 문자열을 float으로 정규화하고 유효 범위를 점검한다."""
    if value is None:
        return None
    try:
        if isinstance(value, str):
            value = value.strip().replace(",", "")
        numeric = float(value)
        if pd.isna(numeric) or numeric < -100 or numeric > 100:
            return None
        return round(numeric, 4)
    except (ValueError, TypeError):
        return None

def normalize_gscpi_payload(payload):
    """New York Fed 차트 JSON 응답 구조를 재귀 탐색하여 시계열을 추출한다."""
    candidate_lists = []

    def collect_lists(obj):
        if isinstance(obj, list):
            candidate_lists.append(obj)
            for item in obj:
                collect_lists(item)
        elif isinstance(obj, dict):
            for v in obj.values():
                collect_lists(v)

    collect_lists(payload)

    best_series = []
    date_keys = ["date", "Date", "DATE", "period", "Period", "time", "Time", "timestamp", "Timestamp", "x"]
    value_keys = ["gscpi", "GSCPI", "gscpi_value", "GSCPI_VALUE", "value", "Value", "VALUE", "index", "Index", "pressure", "Pressure", "y"]

    for rows in candidate_lists:
        normalized_rows = []
        for row in rows:
            raw_date = None
            raw_value = None

            if isinstance(row, dict):
                for k in date_keys:
                    if k in row:
                        raw_date = row[k]
                        break
                for k in value_keys:
                    if k in row:
                        raw_value = row[k]
                        break
            elif isinstance(row, (list, tuple)) and len(row) >= 2:
                raw_date = row[0]
                raw_value = row[1]

            period = parse_gscpi_period(raw_date)
            numeric_val = parse_gscpi_value(raw_value)

            if period is not None and numeric_val is not None:
                normalized_rows.append({"period": period, "gscpi_value": numeric_val})

        if len(normalized_rows) > len(best_series):
            best_series = normalized_rows

    deduped = {}
    for row in best_series:
        deduped[row["period"]] = row["gscpi_value"]

    return [{"period": p, "gscpi_value": v} for p, v in sorted(deduped.items())]

def inspect_gscpi_response(response, captured_payloads):
    """Playwright response 이벤트에서 XHR/JSON 시계열 후보를 추출한다."""
    try:
        content_type = response.headers.get("content-type", "").lower()
        response_url = response.url
        url_lower = response_url.lower()

        is_candidate = (
            "json" in content_type
            or "gscpi" in url_lower
            or "supply-chain" in url_lower
            or "chart" in url_lower
            or "series" in url_lower
            or "interactive" in url_lower
        )

        if not is_candidate:
            return

        body = response.body()
        if not body:
            return

        text = body.decode("utf-8", errors="ignore").strip()
        if not text.startswith("{") and not text.startswith("["):
            return

        payload = json.loads(text)
        captured_payloads.append({
            "url": response_url,
            "content_type": content_type,
            "payload": payload
        })
        log(f"[GSCPI][CHART] JSON 후보 탐지: {response_url}")
    except Exception:
        return

def fetch_gscpi_from_chart():
    """Headless Chromium으로 New York Fed 인터랙티브 차트 데이터를 스니핑한다."""
    captured_payloads = []
    log("[GSCPI][CHART] New York Fed 인터랙티브 차트 데이터 수집 시작...")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 1200},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
            locale="en-US"
        )
        page = context.new_page()
        page.on("response", lambda res: inspect_gscpi_response(res, captured_payloads))

        try:
            page.goto(GSCPI_PAGE_URL, wait_until="domcontentloaded", timeout=90_000)
            page.wait_for_timeout(12_000)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.55);")
            page.wait_for_timeout(5_000)
        finally:
            browser.close()

    log(f"[GSCPI][CHART] 네트워크 JSON 후보 {len(captured_payloads)}개 확보")

    best_series = []
    best_source = None

    for candidate in captured_payloads:
        normalized = normalize_gscpi_payload(candidate["payload"])
        if len(normalized) > len(best_series):
            best_series = normalized
            best_source = candidate["url"]

    if len(best_series) < 100:
        raise RuntimeError(
            f"유효한 GSCPI 월별 시계열을 차트 응답에서 발견하지 못했습니다. (후보 수: {len(best_series)}건)"
        )

    latest = best_series[-1]
    log(
        f"[GSCPI][CHART] 유효 시계열 확인: {len(best_series)}건 / "
        f"latest={latest['period']} / value={latest['gscpi_value']:.4f} / source={best_source}"
    )

    return best_series, best_source

def get_cached_gscpi_count() -> int:
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS count FROM gscpi_series")
        row = cur.fetchone()
        return int(row["count"]) if row else 0
    finally:
        conn.close()

def save_gscpi_series_to_db(series, source_url):
    if not series:
        raise ValueError("저장할 GSCPI 시계열이 없습니다.")

    conn = get_db_connection()
    cur = conn.cursor()
    now_str = now_kst_iso()
    inserted_count = 0

    try:
        for row in series:
            period = row.get("period")
            val = row.get("gscpi_value")
            if not period or val is None:
                continue

            cur.execute("""
                INSERT INTO gscpi_series (period, gscpi_value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(period) DO UPDATE SET
                    gscpi_value = excluded.gscpi_value,
                    updated_at = excluded.updated_at
            """, (period, float(val), now_str))
            inserted_count += 1

        cur.execute("""
            INSERT INTO metadata_cache (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
        """, ("gscpi_source_url", source_url, now_str))

        cur.execute("""
            INSERT INTO metadata_cache (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
        """, ("gscpi_collection_method", "NY_FED_CHART_NETWORK_JSON", now_str))

        conn.commit()
        latest = series[-1]
        log(f"[GSCPI] SQLite 적재 완료: {inserted_count}건 / latest={latest['period']} / value={latest['gscpi_value']:.4f}")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def fetch_gscpi_series():
    """
    GSCPI 데이터 수집 파이프라인.
    실패 시 기존 캐시를 보존하여 불완전한 빈 JSON 배포를 차단한다.
    """
    log("[GSCPI] NY Fed GSCPI 수집 파이프라인 시작...")
    try:
        series, source_url = fetch_gscpi_from_chart()
        if len(series) < 100:
            raise RuntimeError(f"GSCPI 관측치 부족: {len(series)}건")
        save_gscpi_series_to_db(series, source_url)
        log("[GSCPI] 차트 네트워크 JSON 기반 수집 및 캐싱 완료.")
    except Exception as exc:
        cached_count = get_cached_gscpi_count()
        log(f"[GSCPI WARN] 차트 기반 수집 실패: {exc}")
        if cached_count > 0:
            log(f"[GSCPI] 기존 SQLite 캐시 {cached_count}건을 유지합니다. 빈 데이터로 덮어쓰지 않습니다.")
            return
        raise RuntimeError(
            "GSCPI 차트 기반 수집 실패 및 기존 DB 캐시 부재로 배포를 중단합니다."
        ) from exc

# =========================================================
# 4. World Bank Data Ingestion (GDP & Inflation)
# =========================================================

def fetch_world_bank_indicator(indicator_code: str, label: str):
    """World Bank API에서 주요국 연간 시계열 데이터를 수집하여 DB에 적재한다."""
    log(f"[WORLD_BANK] {label} ({indicator_code}) 수집 시작...")

    conn = get_db_connection()
    cur = conn.cursor()
    now_str = now_kst_iso()
    success_count = 0

    try:
        for ccode, cinfo in TARGET_COUNTRIES.items():
            iso2 = cinfo["iso2"]
            url = f"https://api.worldbank.org/v2/country/{iso2}/indicator/{indicator_code}?format=json&per_page=100"

            try:
                resp = requests.get(url, timeout=15)
                if resp.status_code != 200:
                    continue

                payload = resp.json()
                if not isinstance(payload, list) or len(payload) < 2:
                    continue

                records = payload[1]
                if not records:
                    continue

                for rec in records:
                    raw_year = rec.get("date")
                    raw_val = rec.get("value")

                    if not raw_year or raw_val is None:
                        continue

                    try:
                        year = int(raw_year)
                        val = round(float(raw_val), 4)
                    except (ValueError, TypeError):
                        continue

                    cur.execute("""
                        INSERT INTO macro_series (
                            country_code, country_name, indicator_code,
                            indicator_name, year, value, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(country_code, indicator_code, year) DO UPDATE SET
                            value = excluded.value,
                            updated_at = excluded.updated_at
                    """, (
                        ccode,
                        cinfo["name"],
                        indicator_code,
                        label,
                        year,
                        val,
                        now_str
                    ))
                    success_count += 1

            except Exception as e:
                log(f"[WORLD_BANK WARN] {ccode} {label} 수집 중 오류: {e}")
                continue

        conn.commit()
        log(f"[WORLD_BANK] {label} 적재 완료 ({success_count}건 레코드)")
    finally:
        conn.close()

# =========================================================
# 5. Static Assets Exporter (JSON & SQLite Serializer)
# =========================================================

def export_static_json_payloads():
    """SQLite 캐시 데이터를 정적 배포용 JSON 파일들로 직렬화한다."""
    log("[EXPORT] 정적 JSON 데이터 직렬화 시작...")
    conn = get_db_connection()

    try:
        cur = conn.cursor()

        # 1. Macro JSON
        cur.execute("""
            SELECT country_code, country_name, indicator_name, year, value
            FROM macro_series
            ORDER BY country_code, indicator_name, year ASC
        """)
        macro_rows = cur.fetchall()

        macro_payload = {}
        for r in macro_rows:
            cc = r["country_code"]
            cname = r["country_name"]
            ind = r["indicator_name"]
            yr = r["year"]
            val = r["value"]

            if cc not in macro_payload:
                macro_payload[cc] = {
                    "name": cname,
                    "region": TARGET_COUNTRIES.get(cc, {}).get("region", "Global"),
                    "GDP_GROWTH": {},
                    "CPI_INFLATION": {}
                }

            if ind in macro_payload[cc]:
                macro_payload[cc][ind][str(yr)] = val

        with open(MACRO_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(macro_payload, f, ensure_ascii=False, indent=2)

        # 2. GSCPI JSON
        cur.execute("""
            SELECT period, gscpi_value
            FROM gscpi_series
            ORDER BY period ASC
        """)
        gscpi_rows = cur.fetchall()

        gscpi_payload = [
            {"period": r["period"], "value": r["gscpi_value"]}
            for r in gscpi_rows
        ]

        with open(GSCPI_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(gscpi_payload, f, ensure_ascii=False, indent=2)

        # 3. Meta JSON
        cur.execute("SELECT value FROM metadata_cache WHERE key = 'gscpi_source_url'")
        source_row = cur.fetchone()
        cur.execute("SELECT value FROM metadata_cache WHERE key = 'gscpi_collection_method'")
        method_row = cur.fetchone()

        meta_payload = {
            "generated_at": now_kst_iso(),
            "total_countries": len(macro_payload),
            "gscpi_points": len(gscpi_payload),
            "gscpi_source_url": source_row["value"] if source_row else None,
            "gscpi_collection_method": method_row["value"] if method_row else "CACHE",
            "status": "ONLINE" if len(gscpi_payload) > 0 else "DEGRADED"
        }

        with open(META_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(meta_payload, f, ensure_ascii=False, indent=2)

        log(f"[EXPORT] JSON 파일 생성 완료 (국가: {len(macro_payload)}개, GSCPI: {len(gscpi_payload)}포인트)")
    finally:
        conn.close()

# =========================================================
# 6. Bloomberg/MI Terminal Standard HTML Template
# =========================================================

STATIC_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>MI Terminal - Macro Economies & GSCPI</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg-base: #0a0e14;
      --bg-panel: #121820;
      --bg-card: #18222d;
      --border-panel: #202b38;
      --border-light: #2c3b4d;
      --amber: #ff9900;
      --amber-dim: rgba(255, 153, 0, 0.15);
      --green: #00e676;
      --green-dim: rgba(0, 230, 118, 0.12);
      --red: #ff5252;
      --red-dim: rgba(255, 82, 82, 0.12);
      --cyan: #00b0ff;
      --cyan-dim: rgba(0, 176, 255, 0.12);
      --text-main: #cfd8dc;
      --text-bright: #ffffff;
      --text-muted: #607d8b;
      --font-mono: 'JetBrains Mono', monospace;
      --font-sans: 'Inter', -apple-system, sans-serif;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      background-color: var(--bg-base);
      color: var(--text-main);
      font-family: var(--font-sans);
      font-size: 13px;
      line-height: 1.5;
      padding: 12px 16px 32px 16px;
    }

    /* MI Standard Bloomberg Header */
    .terminal-header {
      background-color: var(--bg-panel);
      border: 1px solid var(--border-panel);
      border-left: 4px solid var(--amber);
      padding: 10px 16px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 12px;
    }

    .header-left {
      display: flex;
      flex-direction: column;
      gap: 2px;
    }

    .module-title {
      font-family: var(--font-mono);
      font-size: 14px;
      font-weight: 700;
      color: var(--amber);
      letter-spacing: 0.5px;
      display: flex;
      align-items: center;
      gap: 8px;
    }

    .module-subtitle {
      font-size: 11px;
      color: var(--text-muted);
      font-family: var(--font-mono);
    }

    .header-right {
      display: flex;
      align-items: center;
      gap: 16px;
      font-family: var(--font-mono);
      font-size: 11px;
    }

    .status-badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 3px 8px;
      background: var(--green-dim);
      border: 1px solid var(--green);
      color: var(--green);
      font-weight: 700;
    }

    .status-dot {
      width: 6px;
      height: 6px;
      background-color: var(--green);
      border-radius: 50%;
      box-shadow: 0 0 6px var(--green);
    }

    .clock-display {
      color: var(--cyan);
      font-weight: 700;
      border: 1px solid var(--border-light);
      background: var(--bg-card);
      padding: 3px 8px;
    }

    .portal-link {
      color: var(--text-bright);
      text-decoration: none;
      background: var(--amber-dim);
      border: 1px solid var(--amber);
      padding: 3px 10px;
      font-weight: 700;
      transition: all 0.2s ease;
    }

    .portal-link:hover {
      background: var(--amber);
      color: #000;
    }

    /* KPI Summary Cards */
    .kpi-row {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 12px;
      margin-bottom: 12px;
    }

    .kpi-card {
      background-color: var(--bg-panel);
      border: 1px solid var(--border-panel);
      padding: 12px 14px;
      display: flex;
      flex-direction: column;
      gap: 4px;
    }

    .kpi-label {
      font-family: var(--font-mono);
      font-size: 11px;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }

    .kpi-value {
      font-family: var(--font-mono);
      font-size: 20px;
      font-weight: 700;
      color: var(--text-bright);
    }

    .kpi-sub {
      font-size: 11px;
      color: var(--text-muted);
      display: flex;
      justify-content: space-between;
    }

    /* Main Grid Layout */
    .layout-grid {
      display: grid;
      grid-template-columns: 2fr 1fr;
      gap: 12px;
      margin-bottom: 12px;
    }

    .panel {
      background-color: var(--bg-panel);
      border: 1px solid var(--border-panel);
      display: flex;
      flex-direction: column;
    }

    .panel-header {
      padding: 8px 12px;
      background-color: var(--bg-card);
      border-bottom: 1px solid var(--border-panel);
      display: flex;
      justify-content: space-between;
      align-items: center;
    }

    .panel-title {
      font-family: var(--font-mono);
      font-size: 12px;
      font-weight: 700;
      color: var(--amber);
      display: flex;
      align-items: center;
      gap: 6px;
    }

    .panel-controls {
      display: flex;
      gap: 8px;
    }

    .btn-select {
      background: var(--bg-base);
      color: var(--text-bright);
      border: 1px solid var(--border-light);
      padding: 3px 8px;
      font-family: var(--font-mono);
      font-size: 11px;
      outline: none;
    }

    .btn-select:focus {
      border-color: var(--amber);
    }

    .panel-body {
      padding: 12px;
      flex: 1;
      position: relative;
    }

    .chart-container {
      position: relative;
      height: 320px;
      width: 100%;
    }

    /* Data Tables */
    .table-container {
      overflow-x: auto;
      max-height: 320px;
    }

    table.terminal-table {
      width: 100%;
      border-collapse: collapse;
      font-family: var(--font-mono);
      font-size: 11px;
      text-align: right;
    }

    table.terminal-table th, table.terminal-table td {
      padding: 6px 10px;
      border-bottom: 1px solid var(--border-panel);
    }

    table.terminal-table th {
      background-color: var(--bg-card);
      color: var(--text-muted);
      font-weight: 700;
      position: sticky;
      top: 0;
      z-index: 1;
    }

    table.terminal-table th:first-child, table.terminal-table td:first-child {
      text-align: left;
    }

    table.terminal-table tr:hover {
      background-color: rgba(255, 153, 0, 0.05);
    }

    .val-positive { color: var(--green); }
    .val-negative { color: var(--red); }
    .val-neutral { color: var(--text-bright); }

    /* Footer */
    .terminal-footer {
      margin-top: 12px;
      padding: 8px 12px;
      background: var(--bg-panel);
      border: 1px solid var(--border-panel);
      display: flex;
      justify-content: space-between;
      font-family: var(--font-mono);
      font-size: 11px;
      color: var(--text-muted);
    }
  </style>
</head>
<body>

  <!-- MI Bloomberg Header -->
  <header class="terminal-header">
    <div class="header-left">
      <div class="module-title">
        <span>[03. MACRO]</span> MACRO ECONOMIES & GSCPI TERMINAL
      </div>
      <div class="module-subtitle">WORLD BANK GDP / CPI & NY FED GLOBAL SUPPLY CHAIN PRESSURE INDEX</div>
    </div>
    <div class="header-right">
      <div class="status-badge"><span class="status-dot"></span><span id="sys-status">LIVE FEED</span></div>
      <div class="clock-display" id="live-clock">--:--:-- KST</div>
      <a href="../index.html" class="portal-link">&larr; MI PORTAL</a>
    </div>
  </header>

  <!-- KPI Row -->
  <div class="kpi-row">
    <div class="kpi-card">
      <span class="kpi-label">LATEST GSCPI INDEX</span>
      <span class="kpi-value" id="kpi-gscpi-val">--</span>
      <div class="kpi-sub">
        <span>PERIOD: <b id="kpi-gscpi-date">--</b></span>
        <span id="kpi-gscpi-status">STANDARD DEV</span>
      </div>
    </div>
    <div class="kpi-card">
      <span class="kpi-label">US GDP GROWTH (LATEST)</span>
      <span class="kpi-value" id="kpi-us-gdp">--</span>
      <div class="kpi-sub">
        <span>ANNUAL REAL GDP</span>
        <span id="kpi-us-gdp-year">--</span>
      </div>
    </div>
    <div class="kpi-card">
      <span class="kpi-label">CHINA GDP GROWTH (LATEST)</span>
      <span class="kpi-value" id="kpi-cn-gdp">--</span>
      <div class="kpi-sub">
        <span>ANNUAL REAL GDP</span>
        <span id="kpi-cn-gdp-year">--</span>
      </div>
    </div>
    <div class="kpi-card">
      <span class="kpi-label">US CPI INFLATION</span>
      <span class="kpi-value" id="kpi-us-cpi">--</span>
      <div class="kpi-sub">
        <span>ANNUAL HEADLINE CPI</span>
        <span id="kpi-us-cpi-year">--</span>
      </div>
    </div>
  </div>

  <!-- Primary Charts Layout -->
  <div class="layout-grid">
    <!-- GSCPI Chart -->
    <div class="panel">
      <div class="panel-header">
        <span class="panel-title">&#9632; NY FED GLOBAL SUPPLY CHAIN PRESSURE INDEX (GSCPI)</span>
        <div class="panel-controls">
          <select id="gscpi-range-select" class="btn-select" onchange="updateGSCPIChart()">
            <option value="ALL">ALL HISTORY</option>
            <option value="10Y">LAST 10 YEARS</option>
            <option value="5Y" selected>LAST 5 YEARS</option>
            <option value="3Y">LAST 3 YEARS</option>
          </select>
        </div>
      </div>
      <div class="panel-body">
        <div class="chart-container">
          <canvas id="gscpiChart"></canvas>
        </div>
      </div>
    </div>

    <!-- GSCPI Table -->
    <div class="panel">
      <div class="panel-header">
        <span class="panel-title">&#9632; RECENT GSCPI OBSERVATIONS</span>
      </div>
      <div class="panel-body" style="padding: 0;">
        <div class="table-container">
          <table class="terminal-table" id="gscpi-recent-table">
            <thead>
              <tr>
                <th>PERIOD</th>
                <th>INDEX</th>
                <th>MoM CHANGE</th>
              </tr>
            </thead>
            <tbody>
              <tr><td colspan="3" style="text-align:center;">LOADING...</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>

  <!-- Secondary Macro Panel -->
  <div class="panel" style="margin-bottom: 12px;">
    <div class="panel-header">
      <span class="panel-title">&#9632; MAJOR ECONOMIES MACRO INDICATORS COMPARISON</span>
      <div class="panel-controls">
        <select id="macro-indicator-select" class="btn-select" onchange="renderMacroTable()">
          <option value="GDP_GROWTH" selected>GDP REAL GROWTH RATE (%)</option>
          <option value="CPI_INFLATION">CPI INFLATION RATE (%)</option>
        </select>
      </div>
    </div>
    <div class="panel-body" style="padding: 0;">
      <div class="table-container" style="max-height: 380px;">
        <table class="terminal-table" id="macro-matrix-table">
          <thead>
            <tr id="macro-matrix-header">
              <th>COUNTRY</th>
              <!-- Dynamic Year Headers -->
            </tr>
          </thead>
          <tbody id="macro-matrix-body">
            <tr><td colspan="6" style="text-align:center;">LOADING...</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- Terminal Footer -->
  <footer class="terminal-footer">
    <div>SOURCES: WORLD BANK OPEN DATA API, FEDERAL RESERVE BANK OF NEW YORK</div>
    <div>DATA REFRESH: <span id="meta-refreshed">--</span> | CACHE ENGINE: SQLITE / PLAYWRIGHT</div>
  </footer>

  <script>
    // Live KST Clock
    function updateKSTClock() {
      const now = new Date();
      const utc = now.getTime() + (now.getTimezoneOffset() * 60000);
      const kstDate = new Date(utc + (3600000 * 9));
      const hours = String(kstDate.getHours()).padStart(2, '0');
      const mins = String(kstDate.getMinutes()).padStart(2, '0');
      const secs = String(kstDate.getSeconds()).padStart(2, '0');
      document.getElementById('live-clock').innerText = `${hours}:${mins}:${secs} KST`;
    }
    setInterval(updateKSTClock, 1000);
    updateKSTClock();

    let gscpiRawData = [];
    let macroRawData = {};
    let gscpiChartInstance = null;

    async function loadData() {
      try {
        const [macroRes, gscpiRes, metaRes] = await Promise.all([
          fetch('macro_data.json'),
          fetch('gscpi_data.json'),
          fetch('dashboard_meta.json')
        ]);

        macroRawData = await macroRes.json();
        gscpiRawData = await gscpiRes.json();
        const meta = await metaRes.json();

        document.getElementById('meta-refreshed').innerText = meta.generated_at + ' KST';
        if (meta.status === 'ONLINE') {
          document.getElementById('sys-status').innerText = 'FEED ONLINE';
        } else {
          document.getElementById('sys-status').innerText = 'FEED DEGRADED';
          document.getElementById('sys-status').parentElement.style.borderColor = '#ff5252';
          document.getElementById('sys-status').parentElement.style.color = '#ff5252';
        }

        renderKPICards();
        initGSCPIChart();
        renderGSCPITable();
        renderMacroTable();

      } catch (err) {
        console.error("데이터 로드 실패:", err);
      }
    }

    function renderKPICards() {
      // 1. GSCPI KPI
      if (gscpiRawData && gscpiRawData.length > 0) {
        const latest = gscpiRawData[gscpiRawData.length - 1];
        const valElem = document.getElementById('kpi-gscpi-val');
        valElem.innerText = latest.value.toFixed(2);
        valElem.className = 'kpi-value ' + (latest.value > 0 ? 'val-negative' : 'val-positive');
        document.getElementById('kpi-gscpi-date').innerText = latest.period;
      }

      // 2. US GDP
      if (macroRawData['USA'] && macroRawData['USA']['GDP_GROWTH']) {
        const years = Object.keys(macroRawData['USA']['GDP_GROWTH']).sort();
        const latestYr = years[years.length - 1];
        const val = macroRawData['USA']['GDP_GROWTH'][latestYr];
        const elem = document.getElementById('kpi-us-gdp');
        elem.innerText = val.toFixed(1) + '%';
        elem.className = 'kpi-value ' + (val >= 0 ? 'val-positive' : 'val-negative');
        document.getElementById('kpi-us-gdp-year').innerText = latestYr;
      }

      // 3. China GDP
      if (macroRawData['CHN'] && macroRawData['CHN']['GDP_GROWTH']) {
        const years = Object.keys(macroRawData['CHN']['GDP_GROWTH']).sort();
        const latestYr = years[years.length - 1];
        const val = macroRawData['CHN']['GDP_GROWTH'][latestYr];
        const elem = document.getElementById('kpi-cn-gdp');
        elem.innerText = val.toFixed(1) + '%';
        elem.className = 'kpi-value ' + (val >= 0 ? 'val-positive' : 'val-negative');
        document.getElementById('kpi-cn-gdp-year').innerText = latestYr;
      }

      // 4. US CPI
      if (macroRawData['USA'] && macroRawData['USA']['CPI_INFLATION']) {
        const years = Object.keys(macroRawData['USA']['CPI_INFLATION']).sort();
        const latestYr = years[years.length - 1];
        const val = macroRawData['USA']['CPI_INFLATION'][latestYr];
        const elem = document.getElementById('kpi-us-cpi');
        elem.innerText = val.toFixed(1) + '%';
        elem.className = 'kpi-value ' + (val > 3.0 ? 'val-negative' : 'val-neutral');
        document.getElementById('kpi-us-cpi-year').innerText = latestYr;
      }
    }

    function initGSCPIChart() {
      const ctx = document.getElementById('gscpiChart').getContext('2d');
      gscpiChartInstance = new Chart(ctx, {
        type: 'line',
        data: { labels: [], datasets: [] },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: 'index', intersect: false },
          plugins: {
            legend: { display: false },
            tooltip: {
              backgroundColor: '#121820',
              titleColor: '#ff9900',
              bodyColor: '#cfd8dc',
              borderColor: '#2c3b4d',
              borderWidth: 1,
              padding: 10,
              displayColors: false
            }
          },
          scales: {
            x: {
              grid: { color: 'rgba(44, 59, 77, 0.4)' },
              ticks: { color: '#607d8b', font: { family: "'JetBrains Mono', monospace", size: 10 }, maxTicksLimit: 12 }
            },
            y: {
              grid: { color: 'rgba(44, 59, 77, 0.4)' },
              ticks: { color: '#607d8b', font: { family: "'JetBrains Mono', monospace", size: 10 } }
            }
          }
        }
      });
      updateGSCPIChart();
    }

    function updateGSCPIChart() {
      if (!gscpiChartInstance || !gscpiRawData.length) return;
      const range = document.getElementById('gscpi-range-select').value;

      let filtered = [...gscpiRawData];
      if (range === '10Y') filtered = filtered.slice(-120);
      else if (range === '5Y') filtered = filtered.slice(-60);
      else if (range === '3Y') filtered = filtered.slice(-36);

      const labels = filtered.map(d => d.period);
      const values = filtered.map(d => d.value);

      gscpiChartInstance.data = {
        labels: labels,
        datasets: [
          {
            label: 'GSCPI',
            data: values,
            borderColor: '#00b0ff',
            backgroundColor: 'rgba(0, 176, 255, 0.08)',
            borderWidth: 1.8,
            pointRadius: 0,
            pointHoverRadius: 4,
            fill: true,
            tension: 0.1
          },
          {
            label: 'Baseline (0.0)',
            data: labels.map(() => 0),
            borderColor: 'rgba(255, 153, 0, 0.5)',
            borderWidth: 1,
            borderDash: [4, 4],
            pointRadius: 0,
            fill: false
          }
        ]
      };
      gscpiChartInstance.update();
    }

    function renderGSCPITable() {
      const tbody = document.querySelector('#gscpi-recent-table tbody');
      if (!gscpiRawData.length) return;

      const recent = gscpiRawData.slice(-12).reverse();
      tbody.innerHTML = recent.map((item, idx) => {
        let diffHtml = '--';
        if (idx < recent.length - 1) {
          const prev = recent[idx + 1].value;
          const diff = item.value - prev;
          const cls = diff > 0 ? 'val-negative' : (diff < 0 ? 'val-positive' : 'val-neutral');
          const sign = diff > 0 ? '+' : '';
          diffHtml = `<span class="${cls}">${sign}${diff.toFixed(2)}</span>`;
        }
        const valCls = item.value > 0 ? 'val-negative' : 'val-positive';
        return `
          <tr>
            <td>${item.period}</td>
            <td class="${valCls}">${item.value.toFixed(2)}</td>
            <td>${diffHtml}</td>
          </tr>
        `;
      }).join('');
    }

    function renderMacroTable() {
      const indicator = document.getElementById('macro-indicator-select').value;
      const theadTr = document.getElementById('macro-matrix-header');
      const tbody = document.getElementById('macro-matrix-body');

      const allYears = new Set();
      Object.values(macroRawData).forEach(c => {
        if (c[indicator]) {
          Object.keys(c[indicator]).forEach(y => allYears.add(y));
        }
      });

      const sortedYears = Array.from(allYears).sort().slice(-6);

      theadTr.innerHTML = `<th>COUNTRY / REGION</th>` +
        sortedYears.map(y => `<th>${y}</th>`).join('');

      const rowsHtml = Object.keys(macroRawData).map(cc => {
        const c = macroRawData[cc];
        const series = c[indicator] || {};
        const cells = sortedYears.map(y => {
          const val = series[y];
          if (val === undefined || val === null) return `<td>--</td>`;
          const cls = indicator === 'GDP_GROWTH'
            ? (val >= 0 ? 'val-positive' : 'val-negative')
            : (val > 3.0 ? 'val-negative' : 'val-neutral');
          return `<td class="${cls}">${val.toFixed(1)}%</td>`;
        }).join('');

        return `
          <tr>
            <td><b>${c.name}</b> <span style="color:var(--text-muted); font-size:10px;">(${cc})</span></td>
            ${cells}
          </tr>
        `;
      }).join('');

      tbody.innerHTML = rowsHtml;
    }

    window.onload = loadData;
  </script>
</body>
</html>
"""

def generate_static_html():
    """정적 배포용 index.html을 작성한다."""
    with open(INDEX_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(STATIC_HTML)
    log(f"[HTML] 정적 대시보드 파일 생성 완료: {INDEX_HTML_PATH}")

# =========================================================
# 7. Local Flask Server & CLI Interface
# =========================================================

app = Flask(__name__, static_folder=str(BASE_DIR))

@app.route("/")
def serve_index():
    return send_from_directory(str(BASE_DIR), "index.html")

@app.route("/<path:filename>")
def serve_static(filename):
    return send_from_directory(str(BASE_DIR), filename)

@app.route("/api/macro")
def api_macro():
    if not MACRO_JSON_PATH.exists():
        return jsonify({"error": "macro_data.json not found"}), 404
    with open(MACRO_JSON_PATH, "r", encoding="utf-8") as f:
        return jsonify(json.load(f))

@app.route("/api/gscpi")
def api_gscpi():
    if not GSCPI_JSON_PATH.exists():
        return jsonify({"error": "gscpi_data.json not found"}), 404
    with open(GSCPI_JSON_PATH, "r", encoding="utf-8") as f:
        return jsonify(json.load(f))

def is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex((host, port)) == 0

def find_available_port(start_port: int = 8000, host: str = "127.0.0.1") -> int:
    port = start_port
    while is_port_in_use(port, host):
        port += 1
    return port

def wait_and_open_browser(url: str, host: str, port: int):
    for _ in range(30):
        if is_port_in_use(port, host):
            webbrowser.open(url)
            return
        time.sleep(0.5)

# =========================================================
# 8. Main Execution Entry Point
# =========================================================

def main():
    parser = argparse.ArgumentParser(description="GDP, Inflation & GSCPI Terminal Pipeline")
    parser.add_argument("--cron", action="store_true", help="GitHub Actions 정적 빌드 모드")
    parser.add_argument("--host", default="0.0.0.0", help="Flask 바인딩 호스트")
    parser.add_argument("--port", type=int, default=8000, help="Flask 바인딩 포트")
    parser.add_argument("--no-open", action="store_true", help="브라우저 자동 열기 비활성화")
    args = parser.parse_args()

    log("[PIPELINE] MacroEconomies 파이프라인 초기화...")
    init_db()

    # 1. World Bank 거시지표 수집
    fetch_world_bank_indicator("NY.GDP.MKTP.KD.ZG", "GDP_GROWTH")
    fetch_world_bank_indicator("FP.CPI.TOTL.ZG", "CPI_INFLATION")

    # 2. GSCPI 인터랙티브 차트 기반 수집 (안전장치 적용)
    try:
        fetch_gscpi_series()
    except Exception as e:
        log(f"[GSCPI ERROR] GSCPI 파이프라인 최종 오류: {e}")
        if get_cached_gscpi_count() == 0:
            log("[FATAL] 유효한 캐시가 없어 빌드를 중단합니다.")
            sys.exit(1)

    # 3. 정적 JSON 및 HTML 산출물 생성
    export_static_json_payloads()
    generate_static_html()

    if args.cron:
        log("[PIPELINE] --cron 정적 산출물 빌드 완료. 프로세스를 정상 종료합니다.")
        return

    # 로컬 Flask 개발 서버 구동
    target_port = args.port if not is_port_in_use(args.port, args.host) else find_available_port(args.port, args.host)
    local_url = f"http://localhost:{target_port}/"

    log(f"[SERVER] Flask 로컬 대시보드 서버 기동: {local_url}")

    if not args.no_open:
        threading.Thread(
            target=wait_and_open_browser,
            args=(local_url, args.host, target_port),
            daemon=True
        ).start()

    app.run(
        host=args.host,
        port=target_port,
        debug=False,
        use_reloader=False
    )

if __name__ == "__main__":
    main()
