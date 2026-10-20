# -*- coding: utf-8 -*-
"""
A&A Top 25 Dashboard
Armstrong & Associates Top 25 Global Freight Forwarders Tracker & MI Terminal Dashboard
"""

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
        print(f"[SETUP] '{pip_name}' 패키지가 없어 설치를 시도합니다...", flush=True)
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", pip_name])
        except Exception:
            subprocess.check_call([sys.executable, "-m", "pip", "install", pip_name, "--break-system-packages"])

setup_environment()

import pandas as pd
import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, redirect, render_template_string, request, url_for

# =========================================================
# 1. Configuration
# =========================================================
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_URL = "https://www.3plogistics.com/3pl-market-info-resources/3pl-market-information/aas-top-25-global-freight-forwarders-list/"
DB_PATH = SCRIPT_DIR / "freight_forwarders_data.db"
STATIC_HTML_PATH = SCRIPT_DIR / "index.html"
STATIC_CSV_PATH = SCRIPT_DIR / "freight_forwarders_history.csv"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}
EXPECTED_COLUMNS = ["rank", "provider", "hq", "revenue", "teu", "air_tons"]

app = Flask(__name__)
RUNTIME_CONFIG = None

# =========================================================
# 2. Common Utilities
# =========================================================
def log(message):
    print(message, flush=True)

def clean_number(value):
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "-", "n/a", "na"}:
        return 0.0
    is_negative = text.startswith("-") or (text.startswith("(") and text.endswith(")"))
    text = text.replace(",", "").replace("(", "").replace(")", "")
    text = "".join(char for char in text if char.isdigit() or char == ".")
    if not text:
        return 0.0
    try:
        num = float(text)
        return -num if is_negative else num
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
    candidates = [preferred, 8080, 8081, 8082, 8000, 5000, 8501, 7860, 3000]
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

        content = (" ".join(map(str, dataframe.columns)) + " " + " ".join(map(str, dataframe.head(5).values.ravel()))).lower()
        score = 0
        for keyword in ["rank", "provider", "headquarters", "hq", "revenue", "teu", "air"]:
            if keyword in content:
                score += 5
        if dataframe.shape[1] >= 6:
            score += 3
        if dataframe.shape[0] >= 20:
            score += 5
        if any(co in content for co in ["dhl", "kuehne", "db schenker", "dsv", "ceva"]):
            score += 3
        candidates.append((score, index, dataframe))

    if not candidates:
        raise RuntimeError("pandas로 읽을 수 있는 테이블을 찾지 못했습니다.")

    candidates.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    best_score, _, best_dataframe = candidates[0]
    if best_score <= 0:
        raise RuntimeError("Top 25 Freight Forwarder 표를 식별하지 못했습니다.")
    return best_dataframe

def fetch_top_25_data(url=DEFAULT_URL):
    log("[FETCH] 최신 A&A Top 25 Global Freight Forwarders 데이터 수집 중...")
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()

    raw_dataframe = select_best_table(response.text)
    if isinstance(raw_dataframe.columns, pd.MultiIndex):
        raw_dataframe.columns = [" ".join(str(v) for v in c if str(v).lower() != "nan").strip() for c in raw_dataframe.columns]
    else:
        raw_dataframe.columns = [str(c).strip() for c in raw_dataframe.columns]

    if raw_dataframe.shape[1] < 6:
        raise RuntimeError(f"예상보다 컬럼 수가 적습니다. 현재 shape={raw_dataframe.shape}")

    dataframe = raw_dataframe.iloc[:, :6].copy()
    dataframe.columns = EXPECTED_COLUMNS

    for column in ["rank", "revenue", "teu", "air_tons"]:
        dataframe[column] = dataframe[column].apply(clean_number)

    dataframe["rank"] = dataframe["rank"].astype(int)
    dataframe["provider"] = dataframe["provider"].apply(normalize_provider_name)
    dataframe["hq"] = dataframe["hq"].astype(str).str.strip()
    dataframe["date"] = dt.date.today().isoformat()
    dataframe["source_url"] = url

    dataframe = dataframe[dataframe["provider"].astype(str).str.len() > 0].sort_values("rank").reset_index(drop=True)
    if dataframe.empty or len(dataframe) < 20:
        raise RuntimeError(f"데이터 검증 실패: 수집된 업체 수가 너무 적습니다. ({len(dataframe)} rows)")

    log(f"[FETCH] 수집 완료: {len(dataframe)} rows")
    return dataframe

# =========================================================
# 4. SQLite Database
# =========================================================
def get_connection():
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
    conn = get_connection()
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn.execute(
            "INSERT INTO run_log (run_at, status, message, row_count) VALUES (?, ?, ?, ?)",
            (now, status, str(message)[:1000], row_count)
        )
        conn.commit()
    finally:
        conn.close()

def save_today_data(current_dataframe):
    connection = get_connection()
    today = dt.date.today().isoformat()
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        save_dataframe = current_dataframe.copy()
        save_dataframe["created_at"] = now
        connection.execute("DELETE FROM forwarders WHERE date = ?", (today,))
        save_dataframe.to_sql("forwarders", connection, if_exists="append", index=False)
        connection.execute(
            "INSERT INTO run_log (run_at, status, message, row_count) VALUES (?, ?, ?, ?)",
            (now, "SUCCESS", "OK", len(save_dataframe))
        )
        connection.commit()
        log(f"[DB] 저장 완료: {DB_PATH} / rows={len(save_dataframe)}")
    except Exception as error:
        connection.rollback()
        connection.execute(
            "INSERT INTO run_log (run_at, status, message, row_count) VALUES (?, ?, ?, ?)",
            (now, "ERROR", str(error)[:1000], 0)
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
            "SELECT date, rank, provider, hq, revenue, teu, air_tons, source_url, created_at FROM forwarders ORDER BY date DESC, rank ASC",
            connection
        )
    finally:
        connection.close()

def get_last_run_meta():
    connection = get_connection()
    try:
        row = connection.execute(
            "SELECT run_at, status, message, row_count FROM run_log ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        if row:
            return {"run_at": row[0], "status": row[1], "message": row[2], "row_count": row[3]}
        return {"run_at": "-", "status": "-", "message": "-", "row_count": 0}
    finally:
        connection.close()

# =========================================================
# 5. Bloomberg / MI Terminal Dashboard HTML Template
# =========================================================
HTML = r'''
<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Forwarder Dashboard | A&A Top 25 Global Freight Forwarders</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Noto+Sans+KR:wght@300;400;500;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #0a0e14;
      --panel: #111822;
      --panel-2: #16202c;
      --border: #243242;
      --border-bright: #3a4d63;
      --text: #e6edf3;
      --text-muted: #8b949e;
      --text-dim: #545d68;
      --accent: #ff9e3b;
      --accent-soft: rgba(255, 158, 59, 0.12);
      --cyan: #39bae6;
      --cyan-soft: rgba(57, 186, 230, 0.12);
      --green: #7fd962;
      --green-soft: rgba(127, 217, 98, 0.12);
      --red: #f26d78;
      --red-soft: rgba(242, 109, 120, 0.12);
      --font-mono: 'JetBrains Mono', 'Menlo', 'Consolas', monospace;
      --font-sans: 'Noto Sans KR', -apple-system, BlinkMacSystemFont, sans-serif;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background-color: var(--bg);
      color: var(--text);
      font-family: var(--font-sans);
      font-size: 13px;
      line-height: 1.5;
      padding: 16px;
      -webkit-font-smoothing: antialiased;
    }
    .terminal-shell {
      max-width: 1680px;
      margin: 0 auto;
      border: 1px solid var(--border);
      background: var(--panel);
      box-shadow: 0 20px 40px rgba(0,0,0,0.45);
    }
    .topbar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 8px 16px;
      background: #06090e;
      border-bottom: 1px solid var(--border);
      font-family: var(--font-mono);
      font-size: 11px;
    }
    .brand { display: flex; align-items: center; gap: 10px; }
    .brand-mark { background: var(--accent); color: #000; font-weight: 700; padding: 2px 6px; letter-spacing: 0.5px; }
    .brand-title { color: var(--text); font-weight: 700; letter-spacing: 1px; }
    .live-status { display: flex; align-items: center; gap: 8px; color: var(--green); font-size: 11px; }
    .live-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--green); box-shadow: 0 0 8px var(--green); }
    .function-bar {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      padding: 8px 16px;
      background: var(--panel-2);
      border-bottom: 1px solid var(--border);
      align-items: center;
      font-family: var(--font-mono);
      font-size: 11px;
    }
    .function-label { color: var(--text-dim); margin-right: 6px; font-weight: 700; }
    .function-btn, .function-link {
      background: var(--panel);
      color: var(--text);
      border: 1px solid var(--border);
      padding: 4px 10px;
      font-family: var(--font-mono);
      font-size: 11px;
      cursor: pointer;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      gap: 4px;
      transition: all 0.15s ease;
    }
    .function-btn:hover, .function-link:hover {
      background: var(--accent-soft);
      border-color: var(--accent);
      color: var(--accent);
    }
    .page-heading {
      padding: 16px;
      border-bottom: 1px solid var(--border);
      display: flex;
      justify-content: space-between;
      align-items: flex-end;
      flex-wrap: wrap;
      gap: 12px;
      background: linear-gradient(180deg, rgba(22,32,44,0.4) 0%, rgba(17,24,34,0) 100%);
    }
    .eyebrow { font-family: var(--font-mono); font-size: 10px; color: var(--accent); letter-spacing: 1.5px; margin-bottom: 4px; }
    h1 { font-family: var(--font-mono); font-size: 20px; font-weight: 700; letter-spacing: -0.5px; color: #fff; }
    .heading-meta { font-family: var(--font-mono); font-size: 11px; color: var(--text-muted); text-align: right; }
    .heading-meta strong { color: var(--cyan); }
    .kpi-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 1px;
      background: var(--border);
      border-bottom: 1px solid var(--border);
    }
    .kpi-card { background: var(--panel); padding: 14px 16px; }
    .kpi-label { font-family: var(--font-mono); font-size: 10px; color: var(--text-muted); letter-spacing: 0.5px; text-transform: uppercase; margin-bottom: 6px; display: block; }
    .kpi-value { font-family: var(--font-mono); font-size: 22px; font-weight: 700; color: #fff; }
    .kpi-sub { font-family: var(--font-mono); font-size: 11px; color: var(--text-dim); margin-top: 4px; }
    .filter-strip {
      padding: 12px 16px;
      background: var(--panel-2);
      border-bottom: 1px solid var(--border);
      display: flex;
      flex-wrap: wrap;
      gap: 14px;
      align-items: center;
      font-family: var(--font-mono);
      font-size: 11px;
    }
    .filter-group { display: flex; align-items: center; gap: 8px; }
    .filter-label { color: var(--accent); font-weight: 700; }
    select, input[type="text"] {
      background: var(--bg);
      border: 1px solid var(--border);
      color: var(--text);
      padding: 4px 8px;
      font-family: var(--font-mono);
      font-size: 11px;
      outline: none;
    }
    select:focus, input[type="text"]:focus { border-color: var(--accent); }
    .table-container { width: 100%; overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; font-family: var(--font-mono); font-size: 12px; }
    th {
      background: #0d131a;
      color: var(--text-muted);
      text-align: right;
      padding: 8px 10px;
      border-bottom: 1px solid var(--border-bright);
      font-weight: 500;
      white-space: nowrap;
      user-select: none;
    }
    th.sortable { cursor: pointer; }
    th.sortable:hover { color: var(--accent); background: #121a24; }
    th.text-left, td.text-left { text-align: left; }
    th.text-center, td.text-center { text-align: center; }
    td { padding: 8px 10px; border-bottom: 1px solid var(--border); white-space: nowrap; text-align: right; color: var(--text); }
    tr:hover td { background: var(--panel-2); }
    .provider-cell { font-family: var(--font-sans); font-weight: 500; color: #fff; }
    .hq-badge {
      display: inline-block;
      padding: 1px 5px;
      background: rgba(255,255,255,0.05);
      border: 1px solid var(--border);
      color: var(--text-muted);
      font-size: 10px;
      font-family: var(--font-mono);
    }
    .delta-badge {
      display: inline-block;
      min-width: 50px;
      font-size: 11px;
      padding: 1px 4px;
      border-radius: 2px;
      font-family: var(--font-mono);
    }
    .delta-up { color: var(--green); background: var(--green-soft); }
    .delta-down { color: var(--red); background: var(--red-soft); }
    .delta-zero { color: var(--text-dim); }
    .rank-cell { font-weight: 700; color: var(--accent); }
    .footer {
      padding: 12px 16px;
      background: #06090e;
      border-top: 1px solid var(--border);
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-family: var(--font-mono);
      font-size: 10px;
      color: var(--text-dim);
    }
    .footer a { color: var(--cyan); text-decoration: none; }
    .footer a:hover { text-decoration: underline; }
    .terminal-message {
      padding: 10px 16px;
      background: var(--cyan-soft);
      border-left: 3px solid var(--cyan);
      border-bottom: 1px solid var(--border);
      font-family: var(--font-mono);
      font-size: 12px;
      color: var(--cyan);
    }
    .empty-state { padding: 60px 20px; text-align: center; color: var(--text-muted); font-family: var(--font-mono); }
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
        <span class="live-dot"></span>DATABASE {{ meta.status }}
      </div>
    </header>

    <nav class="function-bar">
      <span class="function-label">FUNCTIONS / TIME TRAVEL</span>
      <a class="function-link" href="./" rel="noopener noreferrer">[F1] DASHBOARD</a>
      <a class="function-link" href="{{ csv_filename }}" rel="noopener noreferrer">[F2] DOWNLOAD CSV</a>
      {% if not static_mode %}
      <a class="function-link" href="/refresh" rel="noopener noreferrer">[F5] REFRESH DATA</a>
      {% endif %}
      <button class="function-btn" onclick="window.print()">[P] PRINT</button>
    </nav>

    {% if message %}
    <div class="terminal-message">
      <strong>SYSTEM MESSAGE //</strong> {{ message }}
    </div>
    {% endif %}

    <section class="page-heading">
      <div>
        <div class="eyebrow">SEA / FORWARDER / RANKING INTELLIGENCE</div>
        <h1>A&A Top 25 Global Freight Forwarders</h1>
      </div>
      <div class="heading-meta">
        LAST RUN <strong>{{ meta.run_at }}</strong><br>
        SNAPSHOTS <strong id="snapshotCount">0</strong>
      </div>
    </section>

    {% if is_empty %}
      <div class="empty-state">NO DATA AVAILABLE</div>
    {% else %}
    <section class="kpi-grid">
      <div class="kpi-card">
        <span class="kpi-label">TOTAL 2024 GROSS REV</span>
        <div class="kpi-value" id="kpiTotalRevenue">-</div>
        <div class="kpi-sub" id="kpiRevenueDelta">vs base date</div>
      </div>
      <div class="kpi-card">
        <span class="kpi-label">TOTAL OCEAN TEU</span>
        <div class="kpi-value" id="kpiTotalTeu">-</div>
        <div class="kpi-sub" id="kpiTeuDelta">vs base date</div>
      </div>
      <div class="kpi-card">
        <span class="kpi-label">TOTAL AIR METRIC TONS</span>
        <div class="kpi-value" id="kpiTotalAir">-</div>
        <div class="kpi-sub" id="kpiAirDelta">vs base date</div>
      </div>
      <div class="kpi-card">
        <span class="kpi-label">MARKET LEADER (#1)</span>
        <div class="kpi-value" id="kpiLeader" style="font-size:16px; color:var(--accent);">-</div>
        <div class="kpi-sub" id="kpiLeaderRev">Revenue Leader</div>
      </div>
    </section>

    <section class="filter-strip">
      <div class="filter-group">
        <span class="filter-label">CURRENT SNAPSHOT</span>
        <select id="latestDateSelect"></select>
      </div>
      <div class="filter-group">
        <span class="filter-label">COMPARE BASE</span>
        <select id="baseDateSelect"></select>
      </div>
      <div class="filter-group">
        <span class="filter-label">SEARCH</span>
        <input type="text" id="searchInput" placeholder="Filter provider or HQ..." autocomplete="off">
      </div>
    </section>

    <div class="table-container">
      <table id="rankingTable">
        <thead>
          <tr>
            <th class="sortable text-center" data-col="rank">RANK</th>
            <th class="sortable text-left" data-col="provider">PROVIDER</th>
            <th class="sortable text-center" data-col="hq">HQ</th>
            <th class="sortable" data-col="revenue">GROSS REV ($M)</th>
            <th>REV Δ</th>
            <th class="sortable" data-col="teu">OCEAN TEU</th>
            <th>TEU Δ</th>
            <th class="sortable" data-col="air_tons">AIR METRIC TONS</th>
            <th>AIR Δ</th>
          </tr>
        </thead>
        <tbody id="rankingTableBody"></tbody>
      </table>
    </div>
    {% endif %}

    <footer class="footer">
      <div>DATA SOURCE: <a href="https://www.3plogistics.com" target="_blank" rel="noopener noreferrer">Armstrong & Associates (A&A)</a> &bull; MI MARKET INTELLIGENCE</div>
      <div>TERMINAL ID: FWD-25 &bull; ARCHITECTURE: SQLITE / GITHUB ACTIONS STATIC BUILD</div>
    </footer>
  </main>

  <script>
    const RAW_DATA = {{ payload | tojson | safe }};

    let latestDate = "";
    let baseDate = "";
    let currentSort = { col: "rank", asc: true };
    let searchQuery = "";

    function init() {
      if (!RAW_DATA || RAW_DATA.length === 0) return;

      const dates = Array.from(new Set(RAW_DATA.map(d => d.date))).sort().reverse();
      document.getElementById("snapshotCount").textContent = dates.length;

      const latestSelect = document.getElementById("latestDateSelect");
      const baseSelect = document.getElementById("baseDateSelect");

      latestSelect.innerHTML = "";
      baseSelect.innerHTML = "";

      dates.forEach((date, i) => {
        const opt1 = new Option(date, date, false, i === 0);
        const opt2 = new Option(date, date, false, i === (dates.length > 1 ? 1 : 0));
        latestSelect.add(opt1);
        baseSelect.add(opt2);
      });

      latestDate = dates[0];
      baseDate = dates.length > 1 ? dates[1] : dates[0];

      latestSelect.addEventListener("change", (e) => {
        latestDate = e.target.value;
        render();
      });

      baseSelect.addEventListener("change", (e) => {
        baseDate = e.target.value;
        render();
      });

      document.getElementById("searchInput").addEventListener("input", (e) => {
        searchQuery = e.target.value.toLowerCase().trim();
        renderTableOnly();
      });

      document.querySelectorAll("th.sortable").forEach(th => {
        th.addEventListener("click", () => {
          const col = th.dataset.col;
          if (currentSort.col === col) {
            currentSort.asc = !currentSort.asc;
          } else {
            currentSort.col = col;
            currentSort.asc = (col === "provider" || col === "hq" || col === "rank");
          }
          renderTableOnly();
        });
      });

      render();
    }

    function formatNumber(num) {
      if (num === null || num === undefined || isNaN(num) || num === 0) return "-";
      return Number(num).toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 0 });
    }

    function formatDelta(delta, isPercent = false) {
      if (delta === 0 || isNaN(delta) || delta === null) {
        return '<span class="delta-badge delta-zero">-</span>';
      }
      const sign = delta > 0 ? "+" : "";
      const cls = delta > 0 ? "delta-up" : "delta-down";
      const formatted = sign + Number(delta).toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 1 });
      return `<span class="delta-badge ${cls}">${formatted}${isPercent ? '%' : ''}</span>`;
    }

    function calculateData() {
      const latestRows = RAW_DATA.filter(d => d.date === latestDate);
      const baseRows = RAW_DATA.filter(d => d.date === baseDate);
      const baseMap = new Map();
      baseRows.forEach(r => baseMap.set(r.provider, r));

      return latestRows.map(row => {
        const base = baseMap.get(row.provider);
        return {
          rank: row.rank,
          provider: row.provider,
          hq: row.hq,
          revenue: row.revenue,
          teu: row.teu,
          air_tons: row.air_tons,
          rank_delta: base ? base.rank - row.rank : 0,
          revenue_delta: base ? row.revenue - base.revenue : 0,
          teu_delta: base ? row.teu - base.teu : 0,
          air_delta: base ? row.air_tons - base.air_tons : 0,
        };
      });
    }

    function renderKPIs(rows) {
      const totalRev = rows.reduce((s, r) => s + (r.revenue || 0), 0);
      const totalTeu = rows.reduce((s, r) => s + (r.teu || 0), 0);
      const totalAir = rows.reduce((s, r) => s + (r.air_tons || 0), 0);

      const totalRevDelta = rows.reduce((s, r) => s + (r.revenue_delta || 0), 0);
      const totalTeuDelta = rows.reduce((s, r) => s + (r.teu_delta || 0), 0);
      const totalAirDelta = rows.reduce((s, r) => s + (r.air_delta || 0), 0);

      document.getElementById("kpiTotalRevenue").textContent = "$" + formatNumber(totalRev) + "M";
      document.getElementById("kpiTotalTeu").textContent = formatNumber(totalTeu) + " TEU";
      document.getElementById("kpiTotalAir").textContent = formatNumber(totalAir) + " MT";

      document.getElementById("kpiRevenueDelta").innerHTML = "vs " + baseDate + " " + formatDelta(totalRevDelta);
      document.getElementById("kpiTeuDelta").innerHTML = "vs " + baseDate + " " + formatDelta(totalTeuDelta);
      document.getElementById("kpiAirDelta").innerHTML = "vs " + baseDate + " " + formatDelta(totalAirDelta);

      const leader = rows.find(r => r.rank === 1);
      if (leader) {
        document.getElementById("kpiLeader").textContent = leader.provider;
        document.getElementById("kpiLeaderRev").textContent = "$" + formatNumber(leader.revenue) + "M (" + leader.hq + ")";
      }
    }

    function renderTableOnly() {
      const rows = calculateData();
      let filtered = rows;

      if (searchQuery) {
        filtered = filtered.filter(r => 
          r.provider.toLowerCase().includes(searchQuery) ||
          r.hq.toLowerCase().includes(searchQuery)
        );
      }

      filtered.sort((a, b) => {
        let v1 = a[currentSort.col];
        let v2 = b[currentSort.col];
        if (typeof v1 === "string") v1 = v1.toLowerCase();
        if (typeof v2 === "string") v2 = v2.toLowerCase();
        if (v1 < v2) return currentSort.asc ? -1 : 1;
        if (v1 > v2) return currentSort.asc ? 1 : -1;
        return 0;
      });

      const tbody = document.getElementById("rankingTableBody");
      tbody.innerHTML = "";

      filtered.forEach(r => {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td class="rank-cell text-center">${r.rank}</td>
          <td class="provider-cell text-left">${r.provider}</td>
          <td class="text-center"><span class="hq-badge">${r.hq || '-'}</span></td>
          <td>${formatNumber(r.revenue)}</td>
          <td>${formatDelta(r.revenue_delta)}</td>
          <td>${formatNumber(r.teu)}</td>
          <td>${formatDelta(r.teu_delta)}</td>
          <td>${formatNumber(r.air_tons)}</td>
          <td>${formatDelta(r.air_delta)}</td>
        `;
        tbody.appendChild(tr);
      });
    }

    function render() {
      const rows = calculateData();
      renderKPIs(rows);
      renderTableOnly();
    }

    document.addEventListener("DOMContentLoaded", init);
  </script>
</body>
</html>
'''

# =========================================================
# 6. Static Output Generator
# =========================================================
def build_static_dashboard(output_html_path=STATIC_HTML_PATH, output_csv_path=STATIC_CSV_PATH):
    history_df = load_history()
    meta = get_last_run_meta()

    if history_df.empty:
        payload = []
        is_empty = True
    else:
        payload = history_df.to_dict(orient="records")
        is_empty = False

    rendered_html = render_template_string(
        HTML,
        payload=payload,
        meta=meta,
        is_empty=is_empty,
        static_mode=True,
        csv_filename=Path(output_csv_path).name,
        message=None
    )

    output_html_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_html_path, "w", encoding="utf-8") as file:
        file.write(rendered_html)

    if not history_df.empty:
        history_df.to_csv(output_csv_path, index=False, encoding="utf-8-sig")

    log(f"[STATIC] Generated: {output_html_path}")
    log(f"[STATIC] Exported CSV: {output_csv_path}")

# =========================================================
# 7. Flask Web Server Routes
# =========================================================
@app.route("/")
def index():
    history_df = load_history()
    meta = get_last_run_meta()
    payload = history_df.to_dict(orient="records") if not history_df.empty else []
    message = request.args.get("message")
    return render_template_string(
        HTML,
        payload=payload,
        meta=meta,
        is_empty=(len(payload) == 0),
        static_mode=False,
        csv_filename="/download-csv",
        message=message
    )

@app.route("/refresh")
def route_refresh():
    try:
        current_dataframe = refresh_data(url=RUNTIME_CONFIG.url if RUNTIME_CONFIG else DEFAULT_URL)
        build_static_dashboard()
        return redirect(url_for("index", message=f"정상 갱신 완료 ({len(current_dataframe)}개사 수집)"))
    except Exception as error:
        return redirect(url_for("index", message=f"갱신 실패: {error}"))

@app.route("/download-csv")
def route_download_csv():
    history_df = load_history()
    csv_bytes = history_df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
    return Response(
        csv_bytes,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=freight_forwarders_history.csv"}
    )

# =========================================================
# 8. Service Runner
# =========================================================
def run_once_and_exit(url=DEFAULT_URL):
    try:
        log("[RUN] 단독 갱신 실행...")
        refresh_data(url=url)
        build_static_dashboard()
        log("[RUN] 완료.")
    except Exception as error:
        log(f"[RUN ERROR] {error}")
        save_run_log("ERROR", str(error), 0)
        sys.exit(1)

def run_server(host="127.0.0.1", port=8080, open_browser=True, url=DEFAULT_URL):
    actual_port = find_available_port(host, port)
    local_url = f"http://{host}:{actual_port}"

    try:
        history_df = load_history()
        if history_df.empty:
            log("[INIT] 기존 데이터가 없어 초기 수집을 수행합니다...")
            refresh_data(url=url)
        build_static_dashboard()
    except Exception as error:
        log(f"[WARN] 초기 수집 중 오류: {error}")

    if open_browser:
        threading.Thread(target=wait_and_open_browser, args=(local_url, host, actual_port), daemon=True).start()

    log(f"==================================================")
    log(f" * Forwarder Terminal Running on: {local_url}")
    log(f"==================================================")
    app.run(host=host, port=actual_port, debug=False)

# =========================================================
# 9. CLI Arguments
# =========================================================
def parse_args():
    parser = argparse.ArgumentParser(description="A&A Top 25 Dashboard")
    parser.add_argument("--once", action="store_true", help="1회 수집 및 정적 파일 빌드 후 즉시 종료")
    parser.add_argument("--build-static", action="store_true", help="DB 기반으로 static HTML/CSV만 빌드")
    parser.add_argument("--url", default=DEFAULT_URL, help="A&A Top 25 페이지 URL")
    parser.add_argument("--host", default="127.0.0.1", help="바인딩 호스트")
    parser.add_argument("--port", type=int, default=8080, help="바인딩 포트")
    parser.add_argument("--no-browser", action="store_true", help="브라우저 자동 열기 비활성화")
    return parser.parse_args()

def main():
    global RUNTIME_CONFIG
    args = parse_args()
    RUNTIME_CONFIG = args

    if args.build_static:
        build_static_dashboard()
        return

    if args.once or os.environ.get("GITHUB_ACTIONS") == "true":
        run_once_and_exit(url=args.url)
        return

    run_server(
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
        url=args.url
    )

if __name__ == "__main__":
    main()
