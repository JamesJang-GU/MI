# -*- coding: utf-8 -*-
import argparse
import datetime as dt
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
from io import StringIO
from pathlib import Path

# =========================================================
# 0. Runtime package setup
# =========================================================
def setup_environment():
    required = {
        "requests": "requests",
        "pandas": "pandas",
        "bs4": "beautifulsoup4",
        "lxml": "lxml",
        "flask": "flask",
    }

    for import_name, pip_name in required.items():
        if importlib.util.find_spec(import_name) is not None:
            continue

        print(
            f"[SETUP] '{pip_name}' 패키지가 없어 설치를 시도합니다...",
            flush=True,
        )

        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", pip_name]
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
from bs4 import BeautifulSoup
from flask import Flask, Response, redirect, render_template_string, request, url_for

# =========================================================
# 1. Configuration
# =========================================================
SCRIPT_DIR = Path(__file__).resolve().parent

DEFAULT_URL = (
    "https://www.3plogistics.com/3pl-market-info-resources/"
    "3pl-market-information/aas-top-25-global-freight-forwarders-list/"
)

DB_PATH = SCRIPT_DIR / "freight_forwarders_data.db"
STATIC_HTML_PATH = SCRIPT_DIR / "index.html"
STATIC_CSV_PATH = SCRIPT_DIR / "freight_forwarders_history.csv"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

EXPECTED_COLUMNS = [
    "rank",
    "provider",
    "hq",
    "revenue",
    "teu",
    "air_tons",
]

app = Flask(__name__)
RUNTIME_CONFIG = None

# =========================================================
# 2. Common Utilities
# =========================================================
def log(message):
    print(message, flush=True)

def clean_number(value):
    """문자열 숫자를 float로 정제한다. 음수·괄호·쉼표를 처리한다."""
    text = str(value).strip()

    if not text or text.lower() in {"nan", "none", "-", "n/a", "na"}:
        return 0.0

    is_negative = text.startswith("-") or (
        text.startswith("(") and text.endswith(")")
    )

    text = text.replace(",", "").replace("(", "").replace(")", "")
    text = "".join(
        char for char in text if char.isdigit() or char == "."
    )

    if not text:
        return 0.0

    try:
        number = float(text)
        return -number if is_negative else number
    except ValueError:
        return 0.0

def normalize_provider_name(value):
    return " ".join(str(value).strip().split())

def is_port_available(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False

def find_available_port(host="127.0.0.1", preferred=8080):
    candidates = [
        preferred,
        8080,
        8081,
        8082,
        8000,
        5000,
        8501,
        7860,
        3000,
    ]

    checked = set()

    for port in candidates:
        if port in checked:
            continue

        checked.add(port)

        if is_port_available(host, port):
            return port

    for port in range(8000, 8101):
        if port not in checked and is_port_available(host, port):
            return port

    raise RuntimeError("사용 가능한 포트를 찾지 못했습니다.")

def wait_and_open_browser(url, host, port, timeout=20):
    start_time = time.time()

    while time.time() - start_time < timeout:
        try:
            with socket.create_connection((host, port), timeout=1):
                webbrowser.open_new(url)
                return
        except Exception:
            time.sleep(0.4)

# =========================================================
# 3. Web Crawling
# =========================================================
def select_best_table(html_text):
    """웹페이지 내 테이블 중 A&A Top 25 Forwarders 테이블을 식별한다."""
    soup = BeautifulSoup(html_text, "html.parser")
    tables = soup.find_all("table")

    if not tables:
        raise RuntimeError("웹페이지에서 table 태그를 찾지 못했습니다.")

    candidates = []

    for index, table in enumerate(tables):
        try:
            dataframe = pd.read_html(StringIO(str(table)))[0]
        except Exception:
            continue

        if dataframe is None or dataframe.empty:
            continue

        content = (
            " ".join(map(str, dataframe.columns))
            + " "
            + " ".join(map(str, dataframe.head(5).values.ravel()))
        ).lower()

        score = 0

        for keyword in [
            "rank",
            "provider",
            "headquarters",
            "hq",
            "revenue",
            "teu",
            "air",
        ]:
            if keyword in content:
                score += 5

        if dataframe.shape[1] >= 6:
            score += 3

        if dataframe.shape[0] >= 20:
            score += 5

        if any(
            company in content
            for company in [
                "dhl",
                "kuehne",
                "db schenker",
                "dsv",
                "ceva",
            ]
        ):
            score += 3

        candidates.append((score, index, dataframe))

    if not candidates:
        raise RuntimeError(
            "pandas로 읽을 수 있는 테이블을 찾지 못했습니다."
        )

    candidates.sort(
        key=lambda item: (item[0], -item[1]),
        reverse=True,
    )

    best_score, _, best_dataframe = candidates[0]

    if best_score <= 0:
        raise RuntimeError(
            "Top 25 Freight Forwarder 표를 식별하지 못했습니다."
        )

    return best_dataframe

def fetch_top_25_data(url=DEFAULT_URL):
    """A&A Top 25 Global Freight Forwarders의 최신 데이터를 수집한다."""
    log("[FETCH] 최신 A&A Top 25 Global Freight Forwarders 데이터 수집 중...")

    response = requests.get(
        url,
        headers=HEADERS,
        timeout=30,
    )
    response.raise_for_status()

    raw_dataframe = select_best_table(response.text)

    if isinstance(raw_dataframe.columns, pd.MultiIndex):
        raw_dataframe.columns = [
            " ".join(
                str(value)
                for value in column
                if str(value).lower() != "nan"
            ).strip()
            for column in raw_dataframe.columns
        ]
    else:
        raw_dataframe.columns = [
            str(column).strip()
            for column in raw_dataframe.columns
        ]

    if raw_dataframe.shape[1] < 6:
        raise RuntimeError(
            f"예상보다 컬럼 수가 적습니다. 현재 shape={raw_dataframe.shape}"
        )

    dataframe = raw_dataframe.iloc[:, :6].copy()
    dataframe.columns = EXPECTED_COLUMNS

    for column in ["rank", "revenue", "teu", "air_tons"]:
        dataframe[column] = dataframe[column].apply(clean_number)

    dataframe["rank"] = dataframe["rank"].astype(int)
    dataframe["provider"] = dataframe["provider"].apply(
        normalize_provider_name
    )
    dataframe["hq"] = dataframe["hq"].astype(str).str.strip()
    dataframe["date"] = dt.date.today().isoformat()
    dataframe["source_url"] = url

    dataframe = (
        dataframe[
            dataframe["provider"].astype(str).str.len() > 0
        ]
        .sort_values("rank")
        .reset_index(drop=True)
    )

    if dataframe.empty or len(dataframe) < 20:
        raise RuntimeError(
            f"데이터 검증 실패: 수집된 업체 수가 너무 적습니다. "
            f"({len(dataframe)} rows)"
        )

    log(f"[FETCH] 수집 완료: {len(dataframe)} rows")
    return dataframe

# =========================================================
# 4. SQLite Database
# =========================================================
def get_connection():
    """SQLite 연결 및 필수 테이블을 생성한다."""
    connection = sqlite3.connect(DB_PATH)

    connection.execute("""
        CREATE TABLE IF NOT EXISTS forwarders (
            date TEXT NOT NULL,
            rank INTEGER,
            provider TEXT NOT NULL,
            hq TEXT,
            revenue REAL,
            teu REAL,
            air_tons REAL,
            source_url TEXT,
            created_at TEXT,
            PRIMARY KEY (date, provider)
        )
    """)

    connection.execute("""
        CREATE TABLE IF NOT EXISTS run_log (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TEXT,
            status TEXT,
            message TEXT,
            row_count INTEGER
        )
    """)

    connection.commit()
    return connection

def save_run_log(status, message, row_count=0):
    connection = get_connection()
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        connection.execute(
            """
            INSERT INTO run_log (
                run_at,
                status,
                message,
                row_count
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                now,
                status,
                str(message)[:1000],
                row_count,
            ),
        )
        connection.commit()
    finally:
        connection.close()

def save_today_data(current_dataframe):
    """당일 데이터를 교체 저장한다."""
    connection = get_connection()
    today = dt.date.today().isoformat()
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        save_dataframe = current_dataframe.copy()
        save_dataframe["created_at"] = now

        connection.execute(
            "DELETE FROM forwarders WHERE date = ?",
            (today,),
        )

        save_dataframe.to_sql(
            "forwarders",
            connection,
            if_exists="append",
            index=False,
        )

        connection.execute(
            """
            INSERT INTO run_log (
                run_at,
                status,
                message,
                row_count
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                now,
                "SUCCESS",
                "OK",
                len(save_dataframe),
            ),
        )

        connection.commit()

        log(
            f"[DB] 저장 완료: {DB_PATH} / rows={len(save_dataframe)}"
        )

    except Exception as error:
        connection.rollback()

        connection.execute(
            """
            INSERT INTO run_log (
                run_at,
                status,
                message,
                row_count
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                now,
                "ERROR",
                str(error)[:1000],
                0,
            ),
        )

        connection.commit()
        raise

    finally:
        connection.close()

def refresh_data(url=DEFAULT_URL):
    current_dataframe = fetch_top_25_data(url=url)
    save_today_data(current_dataframe)
    return current_dataframe

def load_history():
    connection = get_connection()

    try:
        return pd.read_sql_query(
            """
            SELECT
                date,
                rank,
                provider,
                hq,
                revenue,
                teu,
                air_tons,
                source_url,
                created_at
            FROM forwarders
            ORDER BY date DESC, rank ASC
            """,
            connection,
        )
    finally:
        connection.close()

def get_last_run_meta():
    connection = get_connection()

    try:
        row = connection.execute(
            """
            SELECT
                run_at,
                status,
                message,
                row_count
            FROM run_log
            ORDER BY run_id DESC
            LIMIT 1
            """
        ).fetchone()

        if row:
            return {
                "run_at": row[0],
                "status": row[1],
                "message": row[2],
                "row_count": row[3],
            }

        return {
            "run_at": "-",
            "status": "-",
            "message": "-",
            "row_count": 0,
        }

    finally:
        connection.close()

# =========================================================
# 5. Bloomberg / MI Terminal Dashboard HTML Template
# =========================================================
HTML = r'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
  >
  <title>MI Terminal | Global Freight Forwarders</title>

  <style>
    :root {
      --bg: #020406;
      --panel: #071016;
      --panel-2: #0a161e;
      --line: #20343f;
      --line-strong: #36515f;
      --text: #e6edf1;
      --muted: #91a1ab;
      --amber: #ffb000;
      --amber-soft: #ffd15c;
      --cyan: #00d8e6;
      --green: #39e58c;
      --red: #ff5c70;
      --white: #ffffff;
    }

    * {
      box-sizing: border-box;
    }

    html {
      background: var(--bg);
    }

    body {
      min-width: 320px;
      margin: 0;
      background:
        linear-gradient(
          rgba(255, 255, 255, 0.012) 1px,
          transparent 1px
        ),
        linear-gradient(
          90deg,
          rgba(255, 255, 255, 0.009) 1px,
          transparent 1px
        ),
        var(--bg);
      background-size: 28px 28px;
      color: var(--text);
      font-family:
        "Roboto Mono",
        "SFMono-Regular",
        Consolas,
        "Liberation Mono",
        Menlo,
        monospace;
      font-size: 13px;
      line-height: 1.45;
    }

    button,
    select,
    a {
      font: inherit;
    }

    a {
      color: inherit;
      text-decoration: none;
    }

    .terminal-shell {
      max-width: 1680px;
      margin: 0 auto;
      padding: 16px;
    }

    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      min-height: 52px;
      padding: 9px 13px;
      border: 1px solid var(--line-strong);
      border-left: 4px solid var(--amber);
      background: rgba(7, 16, 22, 0.96);
    }

    .brand {
      display: flex;
      align-items: baseline;
      gap: 12px;
      min-width: 0;
    }

    .brand-mark {
      color: var(--amber);
      font-size: 18px;
      font-weight: 800;
      letter-spacing: -1px;
      white-space: nowrap;
    }

    .brand-title {
      overflow: hidden;
      color: var(--white);
      font-size: 14px;
      font-weight: 700;
      letter-spacing: 0.06em;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .live-status {
      color: var(--green);
      font-size: 11px;
      letter-spacing: 0.08em;
      white-space: nowrap;
    }

    .live-dot {
      display: inline-block;
      width: 8px;
      height: 8px;
      margin-right: 6px;
      border-radius: 50%;
      background: var(--green);
      box-shadow: 0 0 10px rgba(57, 229, 140, 0.8);
    }

    .function-bar {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 10px;
      padding: 10px 12px;
      border: 1px solid var(--line);
      background: rgba(7, 16, 22, 0.94);
    }

    .function-label {
      margin-right: 4px;
      color: var(--muted);
      font-size: 11px;
      letter-spacing: 0.1em;
      line-height: 30px;
    }

    .function-link,
    .function-button {
      display: inline-flex;
      align-items: center;
      min-height: 30px;
      padding: 5px 10px;
      border: 1px solid var(--line-strong);
      background: #0a171f;
      color: var(--cyan);
      cursor: pointer;
      font-size: 11px;
      letter-spacing: 0.05em;
      text-transform: uppercase;
    }

    .function-link:hover,
    .function-button:hover {
      border-color: var(--amber);
      color: var(--amber);
    }

    .function-button {
      color: var(--amber-soft);
    }

    .terminal-message {
      margin-top: 10px;
      padding: 10px 12px;
      border: 1px solid rgba(255, 92, 112, 0.55);
      background: rgba(255, 92, 112, 0.08);
      color: #ffc2ca;
    }

    .page-heading {
      display: flex;
      justify-content: space-between;
      gap: 20px;
      margin: 22px 0 12px;
      padding: 0 2px;
    }

    .eyebrow {
      margin-bottom: 5px;
      color: var(--cyan);
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.13em;
    }

    h1 {
      margin: 0;
      color: var(--white);
      font-size: clamp(21px, 3vw, 34px);
      letter-spacing: -0.045em;
      line-height: 1.1;
    }

    .heading-meta {
      align-self: flex-end;
      color: var(--muted);
      font-size: 11px;
      text-align: right;
      white-space: nowrap;
    }

    .heading-meta strong {
      color: var(--amber-soft);
      font-weight: 700;
    }

    .kpi-grid {
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 8px;
      margin-bottom: 10px;
    }

    .kpi-card {
      min-height: 102px;
      padding: 13px;
      border: 1px solid var(--line);
      background: linear-gradient(
        145deg,
        rgba(12, 29, 38, 0.97),
        rgba(5, 12, 17, 0.96)
      );
    }

    .kpi-label {
      display: block;
      min-height: 30px;
      color: var(--muted);
      font-size: 10px;
      font-weight: 700;
      letter-spacing: 0.09em;
    }

    .kpi-value {
      display: block;
      overflow: hidden;
      margin-top: 7px;
      color: var(--white);
      font-size: clamp(17px, 2vw, 27px);
      font-variant-numeric: tabular-nums;
      font-weight: 700;
      letter-spacing: -0.06em;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .kpi-value.amber {
      color: var(--amber);
    }

    .kpi-value.cyan {
      color: var(--cyan);
    }

    .kpi-note {
      display: block;
      margin-top: 7px;
      color: var(--muted);
      font-size: 10px;
    }

    .panel {
      margin-top: 10px;
      border: 1px solid var(--line);
      background: rgba(5, 13, 18, 0.96);
    }

    .panel-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      min-height: 45px;
      padding: 9px 12px;
      border-bottom: 1px solid var(--line);
      background: rgba(11, 27, 35, 0.96);
    }

    .panel-title {
      color: var(--amber);
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.07em;
      text-transform: uppercase;
    }

    .panel-subtitle {
      margin-top: 2px;
      color: var(--muted);
      font-size: 10px;
    }

    .controls {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 8px;
    }

    .control-label {
      color: var(--muted);
      font-size: 10px;
      font-weight: 700;
      letter-spacing: 0.05em;
    }

    select {
      min-height: 29px;
      border: 1px solid var(--line-strong);
      outline: none;
      background: #02080c;
      color: var(--amber-soft);
      cursor: pointer;
      padding: 4px 8px;
    }

    select:focus {
      border-color: var(--cyan);
    }

    .comparison-summary {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      border-bottom: 1px solid var(--line);
    }

    .comparison-stat {
      min-height: 74px;
      padding: 11px 12px;
      border-right: 1px solid var(--line);
    }

    .comparison-stat:last-child {
      border-right: 0;
    }

    .comparison-stat-label {
      display: block;
      color: var(--muted);
      font-size: 10px;
      letter-spacing: 0.07em;
    }

    .comparison-stat-value {
      display: block;
      margin-top: 7px;
      color: var(--white);
      font-size: 18px;
      font-variant-numeric: tabular-nums;
      font-weight: 700;
    }

    .table-wrap {
      overflow-x: auto;
    }

    table {
      width: 100%;
      min-width: 1050px;
      border-collapse: collapse;
      font-size: 12px;
    }

    thead th {
      position: sticky;
      top: 0;
      z-index: 1;
      padding: 10px 9px;
      border-bottom: 1px solid var(--line-strong);
      background: #101f28;
      color: var(--amber-soft);
      font-size: 10px;
      font-weight: 700;
      letter-spacing: 0.05em;
      text-align: right;
      text-transform: uppercase;
      white-space: nowrap;
    }

    thead th.left,
    tbody td.left {
      text-align: left;
    }

    tbody td {
      padding: 9px;
      border-bottom: 1px solid rgba(47, 72, 84, 0.58);
      color: #d7e1e6;
      font-variant-numeric: tabular-nums;
      text-align: right;
      white-space: nowrap;
    }

    tbody tr:hover {
      background: rgba(0, 216, 230, 0.06);
    }

    tbody tr:last-child td {
      border-bottom: 0;
    }

    .rank {
      color: var(--amber);
      font-weight: 700;
    }

    .provider {
      color: var(--white);
      font-weight: 700;
    }

    .hq {
      color: var(--muted);
    }

    .delta {
      font-weight: 700;
    }

    .delta.up {
      color: var(--green);
    }

    .delta.down {
      color: var(--red);
    }

    .delta.flat {
      color: var(--muted);
    }

    .delta.rank-up {
      color: var(--green);
    }

    .delta.rank-down {
      color: var(--red);
    }

    .chart-grid {
      display: grid;
      grid-template-columns: 1.2fr 0.8fr;
      gap: 10px;
      margin-top: 10px;
    }

    .chart-body {
      padding: 13px;
    }

    .bar-chart {
      display: flex;
      flex-direction: column;
      gap: 9px;
    }

    .bar-row {
      display: grid;
      grid-template-columns: minmax(135px, 0.9fr) minmax(100px, 2fr) 86px;
      align-items: center;
      gap: 10px;
    }

    .bar-name {
      overflow: hidden;
      color: #d9e5e9;
      font-size: 11px;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .bar-track {
      overflow: hidden;
      height: 10px;
      background: #14232b;
    }

    .bar-fill {
      height: 100%;
      background: linear-gradient(90deg, #d98400, var(--amber));
    }

    .bar-value {
      color: var(--amber-soft);
      font-size: 11px;
      text-align: right;
    }

    .history-list {
      display: flex;
      flex-direction: column;
      gap: 0;
    }

    .history-item {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 0;
      border-bottom: 1px solid rgba(47, 72, 84, 0.58);
    }

    .history-item:last-child {
      border-bottom: 0;
    }

    .history-date {
      color: var(--cyan);
      font-size: 11px;
      font-weight: 700;
    }

    .history-value {
      color: var(--white);
      font-size: 12px;
      font-variant-numeric: tabular-nums;
      text-align: right;
    }

    .history-note {
      color: var(--muted);
      font-size: 10px;
    }

    .footer {
      display: flex;
      justify-content: space-between;
      gap: 14px;
      margin: 10px 0 2px;
      padding: 12px 2px 0;
      color: var(--muted);
      font-size: 10px;
    }

    .footer strong {
      color: var(--cyan);
    }

    .empty-state {
      padding: 42px 20px;
      color: var(--muted);
      text-align: center;
    }

    @media (max-width: 1050px) {
      .kpi-grid {
        grid-template-columns: repeat(3, minmax(0, 1fr));
      }

      .chart-grid {
        grid-template-columns: 1fr;
      }
    }

    @media (max-width: 720px) {
      body {
        font-size: 12px;
      }

      .terminal-shell {
        padding: 9px;
      }

      .topbar,
      .page-heading,
      .panel-header {
        align-items: flex-start;
        flex-direction: column;
      }

      .heading-meta {
        align-self: auto;
        text-align: left;
      }

      .kpi-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .comparison-summary {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .comparison-stat:nth-child(2) {
        border-right: 0;
      }

      .comparison-stat:nth-child(-n + 2) {
        border-bottom: 1px solid var(--line);
      }

      .footer {
        flex-direction: column;
      }
    }

    @media print {
      body {
        background: #ffffff;
        color: #111111;
      }

      .function-bar,
      .controls,
      .function-button {
        display: none !important;
      }

      .terminal-shell {
        max-width: none;
        padding: 0;
      }

      .topbar,
      .panel,
      .kpi-card {
        border-color: #888888;
        background: #ffffff;
      }

      .brand-title,
      h1,
      .provider,
      .kpi-value,
      .comparison-stat-value,
      .history-value {
        color: #111111;
      }

      tbody td,
      .bar-name {
        color: #222222;
      }
    }
  </style>
</head>

<body>
  <main class="terminal-shell">

    <header class="topbar">
      <div class="brand">
        <span class="brand-mark">MI&lt;GO&gt;</span>
        <span class="brand-title">GLOBAL FREIGHT FORWARDER MONITOR</span>
      </div>

      <div class="live-status">
        <span class="live-dot"></span>
        DATABASE {{ meta.status }}
      </div>
    </header>

    <nav class="function-bar" aria-label="Dashboard functions">
      <span class="function-label">FUNCTIONS / TIME TRAVEL</span>
      <a class="function-link" href="./">[F1] DASHBOARD</a>
      <a class="function-link" href="{{ csv_filename }}">[F2] DOWNLOAD CSV</a>
      {% if not static_mode %}
      <a class="function-link" href="/refresh">[F5] REFRESH DATA</a>
      {% endif %}
      <button class="function-button" type="button" onclick="window.print()">
        [P] PRINT
      </button>
    </nav>

    {% if message %}
    <div class="terminal-message">
      <strong>SYSTEM MESSAGE //</strong> {{ message }}
    </div>
    {% endif %}

    <section class="page-heading">
      <div>
        <div class="eyebrow">SEA / FORWARDER / RANKING INTELLIGENCE</div>
        <h1>A&amp;A TOP 25 GLOBAL FREIGHT FORWARDERS</h1>
      </div>

      <div class="heading-meta">
        LAST RUN <strong>{{ meta.run_at }}</strong><br>
        ROWS <strong>{{ meta.row_count }}</strong> /
        SNAPSHOTS <strong id="snapshotCount">0</strong>
      </div>
    </section>

    {% if is_empty %}
    <section class="panel">
      <div class="empty-state">
        <strong>NO DATA AVAILABLE</strong><br><br>
        Run the crawler using:<br>
        <span style="color:#ffb000;">
          python Forwarder/Top25_Forwarder.py --crawl-only
        </span>
      </div>
    </section>

    {% else %}

    <section class="kpi-grid" aria-label="Latest KPI summary">
      <article class="kpi-card">
        <span class="kpi-label">LATEST SNAPSHOT</span>
        <span class="kpi-value amber" id="latestDateKpi">{{ latest_date }}</span>
        <span class="kpi-note">Current reporting period</span>
      </article>

      <article class="kpi-card">
        <span class="kpi-label">TOTAL REVENUE</span>
        <span class="kpi-value" id="totalRevenueKpi">{{ total_revenue }}</span>
        <span class="kpi-note">USD million / Top 25 aggregate</span>
      </article>

      <article class="kpi-card">
        <span class="kpi-label">TOTAL OCEAN VOLUME</span>
        <span class="kpi-value cyan" id="totalTeuKpi">{{ total_teu }}</span>
        <span class="kpi-note">Ocean freight / TEU</span>
      </article>

      <article class="kpi-card">
        <span class="kpi-label">TOTAL AIR VOLUME</span>
        <span class="kpi-value cyan" id="totalAirKpi">{{ total_air }}</span>
        <span class="kpi-note">Air freight / metric tons</span>
      </article>

      <article class="kpi-card">
        <span class="kpi-label">COMPARISON BASE DATE</span>
        <span class="kpi-value amber" id="baseDateKpi">-</span>
        <span class="kpi-note" id="comparisonWindowNote">Select prior snapshot</span>
      </article>
    </section>

    <section class="panel">
      <div class="panel-header">
        <div>
          <div class="panel-title">
            Delta Analysis / Latest vs Base Date
          </div>
          <div class="panel-subtitle">
            Rank, revenue, ocean TEU and air tons are calculated provider by provider.
          </div>
        </div>

        <div class="controls">
          <label class="control-label" for="baseDateSelect">
            BASE DATE
          </label>

          <select id="baseDateSelect" aria-label="Comparison base date"></select>

          <button
            class="function-button"
            type="button"
            id="resetBaseDate"
          >
            RESET
          </button>
        </div>
      </div>

      <div class="comparison-summary">
        <div class="comparison-stat">
          <span class="comparison-stat-label">REVENUE DELTA</span>
          <span class="comparison-stat-value" id="revenueDeltaSummary">-</span>
        </div>

        <div class="comparison-stat">
          <span class="comparison-stat-label">OCEAN TEU DELTA</span>
          <span class="comparison-stat-value" id="teuDeltaSummary">-</span>
        </div>

        <div class="comparison-stat">
          <span class="comparison-stat-label">AIR TONS DELTA</span>
          <span class="comparison-stat-value" id="airDeltaSummary">-</span>
        </div>

        <div class="comparison-stat">
          <span class="comparison-stat-label">MATCHED PROVIDERS</span>
          <span class="comparison-stat-value" id="matchedProviderSummary">-</span>
        </div>
      </div>

      <div class="table-wrap">
        <table aria-label="Forwarder ranking comparison table">
          <thead>
            <tr>
              <th>Rank</th>
              <th class="left">Provider</th>
              <th class="left">HQ</th>
              <th>Rank Δ</th>
              <th>Revenue ($M)</th>
              <th>Revenue Δ</th>
              <th>Ocean TEU</th>
              <th>TEU Δ</th>
              <th>Air Tons</th>
              <th>Air Δ</th>
            </tr>
          </thead>

          <tbody id="rankingTableBody"></tbody>
        </table>
      </div>
    </section>

    <section class="chart-grid">
      <section class="panel">
        <div class="panel-header">
          <div>
            <div class="panel-title">Top 10 / Revenue Ranking</div>
            <div class="panel-subtitle">
              Latest snapshot / USD million
            </div>
          </div>
        </div>

        <div class="chart-body">
          <div class="bar-chart" id="revenueBarChart"></div>
        </div>
      </section>

      <section class="panel">
        <div class="panel-header">
          <div>
            <div class="panel-title">Snapshot History</div>
            <div class="panel-subtitle">
              Top 25 aggregate revenue by saved date
            </div>
          </div>
        </div>

        <div class="chart-body">
          <div class="history-list" id="historyList"></div>
        </div>
      </section>
    </section>

    <footer class="footer">
      <div>
        <strong>SOURCE //</strong>
        <span id="sourceUrl">{{ source_url }}</span>
      </div>

      <div>
        DATASET: A&amp;A TOP 25 GLOBAL FREIGHT FORWARDERS /
        STATIC BUILD: {{ "ON" if static_mode else "OFF" }}
      </div>
    </footer>

    {% endif %}
  </main>

  {% if not is_empty %}
  <script>
    const dataByDate = {{ data_json | safe }};
    const availableDates = {{ dates_json | safe }};
    const latestDate = "{{ latest_date }}";

    const numberFormatter = new Intl.NumberFormat("en-US", {
      maximumFractionDigits: 0
    });

    const compactFormatter = new Intl.NumberFormat("en-US", {
      notation: "compact",
      maximumFractionDigits: 1
    });

    function escapeHtml(value) {
      return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
    }

    function numeric(value) {
      const result = Number(value);
      return Number.isFinite(result) ? result : 0;
    }

    function formatNumber(value) {
      return numberFormatter.format(numeric(value));
    }

    function formatCurrency(value) {
      return "$" + numberFormatter.format(numeric(value)) + "M";
    }

    function formatSigned(value, prefix = "", suffix = "") {
      const number = numeric(value);

      if (number === 0) {
        return "—";
      }

      const sign = number > 0 ? "+" : "−";
      return sign + prefix + numberFormatter.format(Math.abs(number)) + suffix;
    }

    function percentageChange(current, base) {
      if (!base) {
        return null;
      }

      return ((current - base) / Math.abs(base)) * 100;
    }

    function deltaClass(value) {
      if (numeric(value) > 0) {
        return "up";
      }

      if (numeric(value) < 0) {
        return "down";
      }

      return "flat";
    }

    function rankDeltaClass(value) {
      if (numeric(value) < 0) {
        return "rank-up";
      }

      if (numeric(value) > 0) {
        return "rank-down";
      }

      return "flat";
    }

    function formatRankDelta(value) {
      const number = numeric(value);

      if (number === 0) {
        return "—";
      }

      if (number < 0) {
        return "▲ " + Math.abs(number);
      }

      return "▼ " + number;
    }

    function getRows(dateValue) {
      return Array.isArray(dataByDate[dateValue])
        ? dataByDate[dateValue]
        : [];
    }

    function aggregate(rows) {
      return rows.reduce(
        (result, row) => {
          result.revenue += numeric(row.revenue);
          result.teu += numeric(row.teu);
          result.air += numeric(row.air_tons);
          return result;
        },
        {
          revenue: 0,
          teu: 0,
          air: 0
        }
      );
    }

    function createBaseMap(rows) {
      const map = new Map();

      rows.forEach((row) => {
        const provider = String(row.provider || "").trim().toLowerCase();

        if (provider) {
          map.set(provider, row);
        }
      });

      return map;
    }

    function renderSelectOptions() {
      const select = document.getElementById("baseDateSelect");

      select.innerHTML = availableDates
        .map((dateValue, index) => {
          const selected = index === 1 ? " selected" : "";
          return `<option value="${escapeHtml(dateValue)}"${selected}>${escapeHtml(dateValue)}</option>`;
        })
        .join("");

      if (availableDates.length === 1) {
        select.value = latestDate;
      }
    }

    function renderKpis(latestRows, baseDate) {
      const latestTotal = aggregate(latestRows);
      const baseRows = getRows(baseDate);
      const baseTotal = aggregate(baseRows);

      document.getElementById("latestDateKpi").textContent = latestDate;
      document.getElementById("totalRevenueKpi").textContent = formatCurrency(latestTotal.revenue);
      document.getElementById("totalTeuKpi").textContent = formatNumber(latestTotal.teu);
      document.getElementById("totalAirKpi").textContent = formatNumber(latestTotal.air);
      document.getElementById("baseDateKpi").textContent = baseDate || "-";

      if (baseDate === latestDate) {
        document.getElementById("comparisonWindowNote").textContent =
          "Same-period comparison";
      } else {
        document.getElementById("comparisonWindowNote").textContent =
          "Latest vs selected history";
      }

      const revenueDelta = latestTotal.revenue - baseTotal.revenue;
      const teuDelta = latestTotal.teu - baseTotal.teu;
      const airDelta = latestTotal.air - baseTotal.air;

      const revenueElement = document.getElementById("revenueDeltaSummary");
      const teuElement = document.getElementById("teuDeltaSummary");
      const airElement = document.getElementById("airDeltaSummary");

      revenueElement.textContent = formatSigned(revenueDelta, "$", "M");
      teuElement.textContent = formatSigned(teuDelta);
      airElement.textContent = formatSigned(airDelta);

      revenueElement.className =
        "comparison-stat-value delta " + deltaClass(revenueDelta);
      teuElement.className =
        "comparison-stat-value delta " + deltaClass(teuDelta);
      airElement.className =
        "comparison-stat-value delta " + deltaClass(airDelta);
    }

    function renderTable(latestRows, baseDate) {
      const body = document.getElementById("rankingTableBody");
      const baseRows = getRows(baseDate);
      const baseMap = createBaseMap(baseRows);

      let matchedProviderCount = 0;

      body.innerHTML = latestRows
        .slice()
        .sort((a, b) => numeric(a.rank) - numeric(b.rank))
        .map((row) => {
          const providerKey = String(row.provider || "").trim().toLowerCase();
          const baseRow = baseMap.get(providerKey);

          const rankDelta = baseRow
            ? numeric(row.rank) - numeric(baseRow.rank)
            : 0;

          const revenueDelta = baseRow
            ? numeric(row.revenue) - numeric(baseRow.revenue)
            : 0;

          const teuDelta = baseRow
            ? numeric(row.teu) - numeric(baseRow.teu)
            : 0;

          const airDelta = baseRow
            ? numeric(row.air_tons) - numeric(baseRow.air_tons)
            : 0;

          if (baseRow) {
            matchedProviderCount += 1;
          }

          const comparisonUnavailable = baseRow ? "" : "N/A";

          return `
            <tr>
              <td class="rank">${formatNumber(row.rank)}</td>
              <td class="left provider">${escapeHtml(row.provider)}</td>
              <td class="left hq">${escapeHtml(row.hq)}</td>

              <td class="delta ${baseRow ? rankDeltaClass(rankDelta) : "flat"}">
                ${baseRow ? formatRankDelta(rankDelta) : comparisonUnavailable}
              </td>

              <td>${formatCurrency(row.revenue)}</td>

              <td class="delta ${baseRow ? deltaClass(revenueDelta) : "flat"}">
                ${baseRow ? formatSigned(revenueDelta, "$", "M") : comparisonUnavailable}
              </td>

              <td>${formatNumber(row.teu)}</td>

              <td class="delta ${baseRow ? deltaClass(teuDelta) : "flat"}">
                ${baseRow ? formatSigned(teuDelta) : comparisonUnavailable}
              </td>

              <td>${formatNumber(row.air_tons)}</td>

              <td class="delta ${baseRow ? deltaClass(airDelta) : "flat"}">
                ${baseRow ? formatSigned(airDelta) : comparisonUnavailable}
              </td>
            </tr>
          `;
        })
        .join("");

      document.getElementById("matchedProviderSummary").textContent =
        matchedProviderCount + " / " + latestRows.length;
    }

    function renderRevenueBars(latestRows) {
      const chart = document.getElementById("revenueBarChart");

      const topRows = latestRows
        .slice()
        .sort((a, b) => numeric(b.revenue) - numeric(a.revenue))
        .slice(0, 10);

      const maximumRevenue = Math.max(
        ...topRows.map((row) => numeric(row.revenue)),
        1
      );

      chart.innerHTML = topRows
        .map((row) => {
          const percent =
            (numeric(row.revenue) / maximumRevenue) * 100;

          return `
            <div class="bar-row">
              <div class="bar-name">
                ${formatNumber(row.rank)}. ${escapeHtml(row.provider)}
              </div>

              <div class="bar-track">
                <div
                  class="bar-fill"
                  style="width:${Math.max(percent, 1)}%"
                ></div>
              </div>

              <div class="bar-value">
                ${formatCurrency(row.revenue)}
              </div>
            </div>
          `;
        })
        .join("");
    }

    function renderHistory() {
      const historyList = document.getElementById("historyList");

      const history = availableDates
        .slice(0, 8)
        .map((dateValue) => {
          const rows = getRows(dateValue);
          const total = aggregate(rows);

          return {
            date: dateValue,
            count: rows.length,
            revenue: total.revenue,
            teu: total.teu
          };
        });

      const latestRevenue = history.length
        ? history[0].revenue
        : 0;

      historyList.innerHTML = history
        .map((item, index) => {
          let note = "Current snapshot";

          if (index > 0) {
            const change = percentageChange(latestRevenue, item.revenue);

            note = change === null
              ? "Revenue base unavailable"
              : `Latest vs this date: ${change >= 0 ? "+" : ""}${change.toFixed(1)}%`;
          }

          return `
            <div class="history-item">
              <div>
                <div class="history-date">${escapeHtml(item.date)}</div>
                <div class="history-note">
                  ${item.count} providers / ${escapeHtml(note)}
                </div>
              </div>

              <div class="history-value">
                ${formatCurrency(item.revenue)}<br>
                <span class="history-note">${compactFormatter.format(item.teu)} TEU</span>
              </div>
            </div>
          `;
        })
        .join("");
    }

    function updateDashboard() {
      const baseDate = document.getElementById("baseDateSelect").value;
      const latestRows = getRows(latestDate);

      renderKpis(latestRows, baseDate);
      renderTable(latestRows, baseDate);
      renderRevenueBars(latestRows);
    }

    function initialiseDashboard() {
      document.getElementById("snapshotCount").textContent =
        availableDates.length;

      renderSelectOptions();
      renderHistory();
      updateDashboard();

      document
        .getElementById("baseDateSelect")
        .addEventListener("change", updateDashboard);

      document
        .getElementById("resetBaseDate")
        .addEventListener("click", () => {
          const select = document.getElementById("baseDateSelect");

          select.value = availableDates.length > 1
            ? availableDates[1]
            : latestDate;

          updateDashboard();
        });
    }

    initialiseDashboard();
  </script>
  {% endif %}
</body>
</html>
'''

# =========================================================
# 6. Dashboard Rendering
# =========================================================
def render_dashboard_html(
    message="",
    static_mode=False,
    source_url=DEFAULT_URL,
):
    """DB 이력 데이터를 HTML 대시보드로 렌더링한다."""
    dataframe = load_history()
    meta = get_last_run_meta()

    if dataframe.empty:
        return render_template_string(
            HTML,
            is_empty=True,
            message=message,
            meta=meta,
            source_url=source_url,
            data_json="{}",
            dates_json="[]",
            latest_date="-",
            total_revenue="-",
            total_teu="-",
            total_air="-",
            static_mode=static_mode,
            csv_filename=STATIC_CSV_PATH.name,
        )

    numeric_columns = [
        "rank",
        "revenue",
        "teu",
        "air_tons",
    ]

    for column in numeric_columns:
        dataframe[column] = pd.to_numeric(
            dataframe[column],
            errors="coerce",
        ).fillna(0)

    dataframe["rank"] = dataframe["rank"].astype(int)
    dataframe["date"] = dataframe["date"].astype(str)
    dataframe["provider"] = dataframe["provider"].fillna("").astype(str)
    dataframe["hq"] = dataframe["hq"].fillna("").astype(str)

    all_dates = sorted(
        dataframe["date"].dropna().unique().tolist(),
        reverse=True,
    )

    latest_date = all_dates[0]
    data_by_date = {}

    for date_value in all_dates:
        date_dataframe = (
            dataframe[dataframe["date"] == date_value]
            .sort_values(["rank", "provider"])
            .copy()
        )

        data_by_date[date_value] = date_dataframe[
            [
                "rank",
                "provider",
                "hq",
                "revenue",
                "teu",
                "air_tons",
            ]
        ].to_dict(orient="records")

    latest_dataframe = dataframe[
        dataframe["date"] == latest_date
    ].copy()

    return render_template_string(
        HTML,
        is_empty=False,
        message=message,
        meta=meta,
        source_url=source_url,
        data_json=json.dumps(
            data_by_date,
            ensure_ascii=False,
        ),
        dates_json=json.dumps(
            all_dates,
            ensure_ascii=False,
        ),
        latest_date=latest_date,
        total_revenue=f"${latest_dataframe['revenue'].sum():,.0f}M",
        total_teu=f"{latest_dataframe['teu'].sum():,.0f}",
        total_air=f"{latest_dataframe['air_tons'].sum():,.0f}",
        static_mode=static_mode,
        csv_filename=STATIC_CSV_PATH.name,
    )

# =========================================================
# 7. Flask Routes
# =========================================================
@app.route("/")
def index():
    return render_dashboard_html(
        message=request.args.get("msg", ""),
        static_mode=False,
        source_url=(
            RUNTIME_CONFIG.url
            if RUNTIME_CONFIG
            else DEFAULT_URL
        ),
    )

@app.route("/refresh")
def refresh():
    try:
        refresh_data(
            url=(
                RUNTIME_CONFIG.url
                if RUNTIME_CONFIG
                else DEFAULT_URL
            )
        )

        return redirect(
            url_for(
                "index",
                msg="데이터 수집 및 DB 저장 완료",
            )
        )

    except Exception as error:
        return redirect(
            url_for(
                "index",
                msg=f"데이터 수집 실패: {error}",
            )
        )

@app.route("/download_csv")
def download_csv():
    dataframe = load_history()

    return Response(
        dataframe.to_csv(
            index=False,
            encoding="utf-8-sig",
        ),
        mimetype="text/csv",
        headers={
            "Content-Disposition": (
                "attachment; "
                "filename=Freight_Forwarders_History.csv"
            )
        },
    )

# =========================================================
# 8. Static File Generation
# =========================================================
def export_static_csv():
    """DB 이력을 GitHub Pages용 CSV로 내보낸다."""
    dataframe = load_history()

    if dataframe.empty:
        return

    dataframe_copy = dataframe.copy()

    if "date" in dataframe_copy.columns:
        dataframe_copy["date"] = (
            pd.to_datetime(
                dataframe_copy["date"],
                errors="coerce",
            )
            .dt.strftime("%Y-%m-%d")
        )

    dataframe_copy.to_csv(
        STATIC_CSV_PATH,
        index=False,
        encoding="utf-8-sig",
    )

    log(f"[CSV] 저장 완료: {STATIC_CSV_PATH}")

def run_cron_job(url=DEFAULT_URL):
    """
    GitHub Actions 또는 --crawl-only 실행 시:
    1) 최신 크롤링
    2) SQLite 저장
    3) CSV 생성
    4) Bloomberg 스타일 index.html 생성
    """
    log("[FORWARDER] Static dashboard build started.")

    get_connection().close()
    crawl_message = ""

    try:
        dataframe = refresh_data(url=url)
        log(
            "[FORWARDER] 최신 데이터 수집 성공: "
            f"{len(dataframe)} rows"
        )

    except Exception as error:
        crawl_message = f"데이터 수집 실패: {error}"
        log(f"[FORWARDER] Crawl error: {error}")

        if load_history().empty:
            raise RuntimeError(
                "크롤링에 실패했고, 표시할 기존 데이터도 없습니다."
            )

    export_static_csv()

    with app.app_context():
        html_content = render_dashboard_html(
            message=crawl_message,
            static_mode=True,
            source_url=url,
        )

    STATIC_HTML_PATH.write_text(
        html_content,
        encoding="utf-8",
    )

    log(f"[HTML] 저장 완료: {STATIC_HTML_PATH}")
    log("[FORWARDER] Static dashboard build completed.")

# =========================================================
# 9. Runtime Arguments & Main
# =========================================================
def parse_args():
    parser = argparse.ArgumentParser(
        description="A&A Top 25 Global Freight Forwarders Dashboard"
    )

    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help="A&A Top 25 source URL",
    )

    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Flask host",
    )

    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Flask port",
    )

    parser.add_argument(
        "--no-open",
        action="store_true",
        help="브라우저를 자동으로 열지 않음",
    )

    parser.add_argument(
        "--no-fetch-on-start",
        action="store_true",
        help="Flask 시작 전 최신 데이터 수집을 생략",
    )

    parser.add_argument(
        "--crawl-only",
        action="store_true",
        help="크롤링·DB·CSV·정적 HTML 생성 후 종료",
    )

    return parser.parse_args()

def main():
    global RUNTIME_CONFIG

    args = parse_args()
    RUNTIME_CONFIG = args

    get_connection().close()

    # GitHub Actions에서는 항상 정적 HTML을 생성하고 종료한다.
    if args.crawl_only or os.environ.get("GITHUB_ACTIONS") == "true":
        try:
            run_cron_job(url=args.url)
            sys.exit(0)

        except Exception as error:
            save_run_log("ERROR", error)
            log(f"[FORWARDER] Build failed: {error}")
            sys.exit(1)

    # 로컬 Flask 실행 시에는 최신 수집을 시도한다.
    if not args.no_fetch_on_start:
        try:
            refresh_data(url=args.url)
        except Exception as error:
            log(f"[WARN] 시작 데이터 수집 실패: {error}")

    port = find_available_port(args.host, args.port)
    local_url = f"http://{args.host}:{port}"

    log(f"[INFO] Flask 시작: {local_url}")

    if not args.no_open:
        threading.Thread(
            target=wait_and_open_browser,
            args=(local_url, args.host, port),
            daemon=True,
        ).start()

    app.run(
        host=args.host,
        port=port,
        debug=False,
        use_reloader=False,
    )

if __name__ == "__main__":
    main()
