import argparse
import datetime as dt
import html
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import traceback
from io import StringIO
from pathlib import Path

# [IDENTIFIER] A&A Top 25 Dashboard - Required for GitHub Actions search

# =====================================================
# 1. Environment Setup
# =====================================================
def setup_environment():
    required = {
        "requests": "requests",
        "pandas": "pandas",
        "bs4": "beautifulsoup4",
        "lxml": "lxml",
    }

    for import_name, pip_name in required.items():
        if importlib.util.find_spec(import_name) is not None:
            continue

        print(f"[SETUP] {pip_name} 패키지가 없어 설치를 시도합니다...", flush=True)

        try:
            subprocess.check_call([
                sys.executable,
                "-m",
                "pip",
                "install",
                pip_name
            ])
        except Exception:
            subprocess.check_call([
                sys.executable,
                "-m",
                "pip",
                "install",
                pip_name,
                "--break-system-packages"
            ])

setup_environment()

import pandas as pd
import requests
from bs4 import BeautifulSoup

# =====================================================
# 2. Configuration
# =====================================================
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
    "air_tons"
]

# =====================================================
# 3. Utilities
# =====================================================
def log(message):
    print(f"[LOG] {message}", flush=True)

def clean_number(value):
    text = str(value).strip()

    if not text or text.lower() in {"nan", "none", "-", "n/a", "na"}:
        return 0.0

    is_negative = (
        text.startswith("-")
        or (text.startswith("(") and text.endswith(")"))
    )

    text = (
        text.replace(",", "")
        .replace("(", "")
        .replace(")", "")
        .replace("$", "")
        .replace("€", "")
        .replace("£", "")
    )

    text = "".join(
        char for char in text
        if char.isdigit() or char == "."
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

def format_compact_number(value, suffix=""):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "--"

    if number == 0:
        return "--"

    if abs(number) >= 1_000_000_000:
        return f"{number / 1_000_000_000:,.2f}B{suffix}"

    if abs(number) >= 1_000_000:
        return f"{number / 1_000_000:,.2f}M{suffix}"

    if abs(number) >= 1_000:
        return f"{number / 1_000:,.1f}K{suffix}"

    return f"{number:,.0f}{suffix}"

def calculate_percentage_change(current, previous):
    try:
        if previous is None:
            return None

        current_value = float(current)
        previous_value = float(previous)

        if previous_value == 0:
            return None

        return ((current_value - previous_value) / abs(previous_value)) * 100

    except Exception:
        return None

def get_delta_text(value):
    if value is None:
        return "N/A"

    prefix = "+" if value > 0 else ""
    return f"{prefix}{value:.1f}%"

def safe_float(value):
    try:
        if value is None:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0

# =====================================================
# 4. Web Crawling
# =====================================================
def select_best_table(html_text):
    soup = BeautifulSoup(html_text, "html.parser")
    tables = soup.find_all("table")

    if not tables:
        raise RuntimeError("페이지에서 <table> 태그를 찾지 못했습니다.")

    candidates = []

    for index, table in enumerate(tables):
        try:
            df = pd.read_html(StringIO(str(table)))[0]

            if df.empty:
                continue

            columns_text = str(df.columns).lower()

            score = sum(
                5 for keyword in ["rank", "provider", "revenue", "gross"]
                if keyword in columns_text
            )

            candidates.append((score, index, df))

        except Exception:
            continue

    if not candidates:
        raise RuntimeError("적합한 데이터 테이블을 파싱하지 못했습니다.")

    candidates.sort(key=lambda item: item[0], reverse=True)

    return candidates[0][2]

def fetch_top_25_data(url):
    log(f"Fetching URL: {url}")

    response = requests.get(
        url,
        headers=HEADERS,
        timeout=30
    )

    response.raise_for_status()

    df = select_best_table(response.text)

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [
            " ".join(
                str(column)
                for column in multi_column
                if str(column).lower() != "nan"
            ).strip()
            for multi_column in df.columns
        ]
    else:
        df.columns = [str(column).strip() for column in df.columns]

    df = df.iloc[:, :6].copy()
    df.columns = EXPECTED_COLUMNS

    for column in ["rank", "revenue", "teu", "air_tons"]:
        df[column] = df[column].apply(clean_number)

    df["rank"] = df["rank"].fillna(0).astype(int)
    df["provider"] = df["provider"].apply(normalize_provider_name)
    df["hq"] = df["hq"].astype(str).str.strip()

    df["date"] = dt.date.today().isoformat()
    df["source_url"] = url

    return df.sort_values("rank").reset_index(drop=True)

# =====================================================
# 5. Database Management
# =====================================================
def get_connection():
    conn = sqlite3.connect(DB_PATH)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS forwarders (
            date TEXT,
            rank INTEGER,
            provider TEXT,
            hq TEXT,
            revenue REAL,
            teu REAL,
            air_tons REAL,
            source_url TEXT,
            created_at TEXT,
            PRIMARY KEY (date, provider)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS run_log (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TEXT,
            status TEXT,
            message TEXT,
            row_count INTEGER
        )
    """)

    conn.commit()

    return conn

def save_today_data(df):
    conn = get_connection()
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        df["created_at"] = now

        conn.execute(
            "DELETE FROM forwarders WHERE date = ?",
            (df["date"].iloc[0],)
        )

        df.to_sql(
            "forwarders",
            conn,
            if_exists="append",
            index=False
        )

        conn.execute("""
            INSERT INTO run_log (
                run_at,
                status,
                message,
                row_count
            )
            VALUES (?, 'SUCCESS', 'OK', ?)
        """, (now, len(df)))

        conn.commit()

        history_df = pd.read_sql_query("""
            SELECT *
            FROM forwarders
            ORDER BY date DESC, rank ASC
        """, conn)

        history_df.to_csv(
            STATIC_CSV_PATH,
            index=False,
            encoding="utf-8-sig"
        )

        log(f"DB 저장 및 CSV 생성 완료: {STATIC_CSV_PATH}")

    except Exception as error:
        conn.execute("""
            INSERT INTO run_log (
                run_at,
                status,
                message,
                row_count
            )
            VALUES (?, 'ERROR', ?, 0)
        """, (now, str(error)))

        conn.commit()
        raise error

    finally:
        conn.close()

def get_dashboard_payload():
    """
    모든 저장 스냅샷을 반환한다.
    화면에서 최신 기준 데이터와 비교 기준일을 선택할 수 있도록
    snapshot_data 구조로 변환한다.
    """
    conn = get_connection()

    try:
        df = pd.read_sql_query("""
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
        """, conn)

        if df.empty:
            return {}, [], None, None, None

        available_dates = sorted(
            df["date"].dropna().unique().tolist(),
            reverse=True
        )

        latest_date = available_dates[0]
        previous_date = (
            available_dates[1]
            if len(available_dates) > 1
            else None
        )

        source_url = ""
        latest_source = df.loc[
            df["date"] == latest_date,
            "source_url"
        ]

        if not latest_source.empty:
            source_url = str(latest_source.iloc[0])

        snapshots = {}

        for snapshot_date in available_dates:
            snapshot_df = df[df["date"] == snapshot_date].copy()
            snapshot_df = snapshot_df.sort_values("rank")

            records = []

            for _, row in snapshot_df.iterrows():
                records.append({
                    "rank": int(row["rank"]),
                    "provider": str(row["provider"]),
                    "hq": str(row["hq"]),
                    "revenue": safe_float(row["revenue"]),
                    "teu": safe_float(row["teu"]),
                    "air_tons": safe_float(row["air_tons"])
                })

            snapshots[snapshot_date] = records

        return (
            snapshots,
            available_dates,
            latest_date,
            previous_date,
            source_url
        )

    finally:
        conn.close()

# =====================================================
# 6. Static Dashboard Generation
# =====================================================
def build_static_dashboard():
    (
        snapshots,
        available_dates,
        latest_date,
        previous_date,
        source_url
    ) = get_dashboard_payload()

    if not snapshots or not latest_date:
        raise RuntimeError("대시보드를 생성할 데이터가 DB에 없습니다.")

    latest_data = snapshots.get(latest_date, [])

    total_revenue = sum(item["revenue"] for item in latest_data)
    total_teu = sum(item["teu"] for item in latest_data)
    total_air_tons = sum(item["air_tons"] for item in latest_data)

    generated_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    snapshot_json = json.dumps(
        snapshots,
        ensure_ascii=False
    ).replace("</", "<\\/")

    dates_json = json.dumps(
        available_dates,
        ensure_ascii=False
    )

    raw_html = """<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta
        name="viewport"
        content="width=device-width, initial-scale=1.0"
    >
    <title>FORWARDER MONITOR | MI Terminal</title>

    <!-- FORWARDER_BLOOMBERG_TEMPLATE_V1 -->

    <style>
        :root {
            --bg: #02060b;
            --panel: #070d15;
            --panel-2: #0b121c;
            --panel-3: #101a27;
            --border: #243342;
            --border-strong: #405064;
            --text: #e7edf4;
            --muted: #8798aa;
            --muted-dark: #5d6d7d;
            --orange: #ff8a00;
            --orange-dark: #a95600;
            --orange-soft: #ffb14e;
            --green: #00d26a;
            --red: #ff4d4f;
            --yellow: #ffd000;
            --cyan: #42c6e8;
        }

        * {
            box-sizing: border-box;
        }

        html {
            background: var(--bg);
        }

        body {
            margin: 0;
            min-width: 320px;
            background:
                linear-gradient(
                    180deg,
                    rgba(255, 138, 0, 0.035) 0%,
                    rgba(2, 6, 11, 0) 240px
                ),
                var(--bg);
            color: var(--text);
            font-family:
                "IBM Plex Mono",
                "Roboto Mono",
                "Consolas",
                "Menlo",
                "Monaco",
                monospace;
            font-size: 13px;
            line-height: 1.4;
        }

        .terminal-shell {
            width: min(1600px, calc(100% - 32px));
            margin: 16px auto 36px;
            border: 1px solid var(--border-strong);
            background: var(--panel);
            box-shadow: 0 0 0 1px rgba(0, 0, 0, 0.65);
        }

        .terminal-topbar {
            min-height: 34px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 16px;
            padding: 0 12px;
            background: var(--orange);
            color: #050505;
            font-size: 11px;
            font-weight: 800;
            letter-spacing: 0.55px;
        }

        .topbar-left,
        .topbar-right {
            display: flex;
            align-items: center;
            gap: 12px;
            white-space: nowrap;
        }

        .terminal-code {
            padding-right: 12px;
            border-right: 1px solid rgba(0, 0, 0, 0.45);
        }

        .live-dot {
            display: inline-block;
            width: 7px;
            height: 7px;
            margin-right: 6px;
            background: #111;
            border-radius: 50%;
        }

        .terminal-header {
            display: flex;
            align-items: flex-end;
            justify-content: space-between;
            gap: 20px;
            padding: 22px 20px 18px;
            border-bottom: 1px solid var(--border-strong);
            background:
                linear-gradient(
                    90deg,
                    rgba(255, 138, 0, 0.08),
                    transparent 42%
                ),
                var(--panel);
        }

        .header-title {
            margin: 0;
            color: var(--orange);
            font-size: clamp(20px, 2.4vw, 30px);
            line-height: 1;
            font-weight: 800;
            letter-spacing: 1.3px;
        }

        .header-subtitle {
            margin-top: 8px;
            color: var(--muted);
            font-size: 11px;
            letter-spacing: 0.55px;
        }

        .header-status {
            text-align: right;
            color: var(--muted);
            font-size: 11px;
            line-height: 1.75;
        }

        .header-status strong {
            color: var(--text);
            font-weight: 700;
        }

        .command-bar {
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 12px 20px;
            border-bottom: 1px solid var(--border);
            background: #050a10;
        }

        .command-label {
            color: var(--orange);
            font-size: 11px;
            font-weight: 800;
            letter-spacing: 0.6px;
        }

        .command-value {
            color: var(--text);
            font-size: 11px;
        }

        .dashboard-content {
            padding: 18px 20px 22px;
        }

        .control-grid {
            display: grid;
            grid-template-columns: minmax(220px, 1fr) minmax(220px, 1fr) auto;
            gap: 12px;
            margin-bottom: 16px;
        }

        .control-box {
            padding: 10px 12px;
            border: 1px solid var(--border);
            background: var(--panel-2);
        }

        .control-label {
            display: block;
            margin-bottom: 6px;
            color: var(--muted);
            font-size: 10px;
            font-weight: 700;
            letter-spacing: 0.65px;
            text-transform: uppercase;
        }

        select {
            width: 100%;
            border: 1px solid var(--border-strong);
            outline: none;
            padding: 8px 9px;
            background: #03070c;
            color: var(--orange-soft);
            font-family: inherit;
            font-size: 12px;
            cursor: pointer;
        }

        select:focus {
            border-color: var(--orange);
        }

        .comparison-note {
            align-self: end;
            padding: 12px;
            color: var(--muted);
            font-size: 10px;
            text-align: right;
        }

        .kpi-grid {
            display: grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            gap: 10px;
            margin-bottom: 16px;
        }

        .kpi-panel {
            min-height: 112px;
            padding: 13px 14px;
            border: 1px solid var(--border);
            border-top: 2px solid var(--orange-dark);
            background:
                linear-gradient(
                    135deg,
                    rgba(255, 138, 0, 0.06),
                    transparent 62%
                ),
                var(--panel-2);
        }

        .kpi-label {
            color: var(--muted);
            font-size: 10px;
            font-weight: 700;
            letter-spacing: 0.6px;
            text-transform: uppercase;
        }

        .kpi-value {
            margin-top: 13px;
            color: var(--orange);
            font-size: clamp(19px, 2.15vw, 27px);
            font-weight: 800;
            letter-spacing: -0.8px;
            font-variant-numeric: tabular-nums;
        }

        .kpi-subtext {
            margin-top: 7px;
            color: var(--muted-dark);
            font-size: 10px;
        }

        .section-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            padding: 9px 11px;
            border: 1px solid var(--border-strong);
            border-bottom: 0;
            background: var(--panel-3);
        }

        .section-title {
            color: var(--orange);
            font-size: 11px;
            font-weight: 800;
            letter-spacing: 0.7px;
        }

        .section-meta {
            color: var(--muted);
            font-size: 10px;
            text-align: right;
        }

        .table-container {
            overflow-x: auto;
            border: 1px solid var(--border-strong);
            background: #04090f;
        }

        table {
            width: 100%;
            min-width: 1030px;
            border-collapse: collapse;
            font-size: 12px;
        }

        thead {
            background: #0a111a;
        }

        th {
            padding: 10px 11px;
            border-bottom: 1px solid var(--border-strong);
            color: var(--orange-soft);
            font-size: 10px;
            font-weight: 800;
            letter-spacing: 0.55px;
            text-align: left;
            text-transform: uppercase;
            white-space: nowrap;
        }

        th.number-heading {
            text-align: right;
        }

        td {
            padding: 10px 11px;
            border-bottom: 1px solid rgba(64, 80, 100, 0.46);
            color: var(--text);
            vertical-align: middle;
        }

        tbody tr {
            transition: background 0.12s ease;
        }

        tbody tr:hover {
            background: rgba(255, 138, 0, 0.065);
        }

        tbody tr:last-child td {
            border-bottom: 0;
        }

        .rank-cell {
            width: 68px;
            color: var(--orange);
            font-size: 14px;
            font-weight: 800;
            font-variant-numeric: tabular-nums;
        }

        .provider-name {
            color: #f0f4f8;
            font-size: 12px;
            font-weight: 800;
        }

        .provider-hq {
            margin-top: 3px;
            color: var(--muted);
            font-size: 10px;
        }

        .number-cell {
            text-align: right;
            font-variant-numeric: tabular-nums;
            white-space: nowrap;
        }

        .metric-value {
            color: var(--text);
            font-weight: 700;
        }

        .metric-delta {
            margin-top: 3px;
            font-size: 10px;
        }

        .delta-positive {
            color: var(--green);
        }

        .delta-negative {
            color: var(--red);
        }

        .delta-neutral {
            color: var(--muted-dark);
        }

        .rank-up {
            color: var(--green);
        }

        .rank-down {
            color: var(--red);
        }

        .rank-flat {
            color: var(--muted-dark);
        }

        .empty-state {
            padding: 36px 20px;
            color: var(--muted);
            text-align: center;
        }

        .terminal-footer {
            display: flex;
            justify-content: space-between;
            gap: 16px;
            padding: 13px 20px;
            border-top: 1px solid var(--border);
            background: #03070c;
            color: var(--muted-dark);
            font-size: 10px;
        }

        .terminal-footer a {
            color: var(--orange-soft);
            text-decoration: none;
        }

        .terminal-footer a:hover {
            color: var(--orange);
            text-decoration: underline;
        }

        @media (max-width: 900px) {
            .terminal-shell {
                width: min(100% - 16px, 1600px);
                margin-top: 8px;
            }

            .terminal-header {
                align-items: flex-start;
                flex-direction: column;
            }

            .header-status {
                text-align: left;
            }

            .control-grid,
            .kpi-grid {
                grid-template-columns: repeat(2, minmax(0, 1fr));
            }

            .comparison-note {
                grid-column: span 2;
                text-align: left;
            }
        }

        @media (max-width: 560px) {
            .terminal-topbar,
            .terminal-footer {
                align-items: flex-start;
                flex-direction: column;
                gap: 5px;
                padding-top: 9px;
                padding-bottom: 9px;
            }

            .dashboard-content {
                padding: 12px;
            }

            .terminal-header,
            .command-bar {
                padding-left: 12px;
                padding-right: 12px;
            }

            .control-grid,
            .kpi-grid {
                grid-template-columns: 1fr;
            }

            .comparison-note {
                grid-column: auto;
            }
        }
    </style>
</head>

<body>
    <main class="terminal-shell">
        <div class="terminal-topbar">
            <div class="topbar-left">
                <span class="terminal-code">MI&lt;GO&gt;</span>
                <span>GLOBAL LOGISTICS INTELLIGENCE</span>
                <span><span class="live-dot"></span>STATIC MARKET SNAPSHOT</span>
            </div>
            <div class="topbar-right">
                <span>FORWARDER / TOP 25</span>
            </div>
        </div>

        <header class="terminal-header">
            <div>
                <h1 class="header-title">FORWARDER MONITOR</h1>
                <div class="header-subtitle">
                    A&amp;A TOP 25 GLOBAL FREIGHT FORWARDERS
                </div>
            </div>

            <div class="header-status">
                CURRENT SNAPSHOT&nbsp;&nbsp;
                <strong id="headerCurrentDate">--</strong><br>
                COMPARISON SNAPSHOT&nbsp;&nbsp;
                <strong id="headerCompareDate">--</strong>
            </div>
        </header>

        <div class="command-bar">
            <span class="command-label">FUNCTION</span>
            <span class="command-value">
                RANKING / REVENUE / OCEAN TEU / AIR TONNAGE / HISTORICAL COMPARISON
            </span>
        </div>

        <section class="dashboard-content">
            <div class="control-grid">
                <div class="control-box">
                    <label class="control-label" for="currentDateSelect">
                        Current Snapshot
                    </label>
                    <select id="currentDateSelect"></select>
                </div>

                <div class="control-box">
                    <label class="control-label" for="compareDateSelect">
                        Compare Against
                    </label>
                    <select id="compareDateSelect"></select>
                </div>

                <div class="comparison-note">
                    DELTA = CURRENT SNAPSHOT VS SELECTED COMPARISON DATE
                </div>
            </div>

            <div class="kpi-grid">
                <div class="kpi-panel">
                    <div class="kpi-label">Total Revenue</div>
                    <div class="kpi-value" id="totalRevenue">--</div>
                    <div class="kpi-subtext">SUM OF AVAILABLE TOP 25 RECORDS</div>
                </div>

                <div class="kpi-panel">
                    <div class="kpi-label">Total Ocean Volume</div>
                    <div class="kpi-value" id="totalTeu">--</div>
                    <div class="kpi-subtext">OCEAN FREIGHT / TEU</div>
                </div>

                <div class="kpi-panel">
                    <div class="kpi-label">Total Air Volume</div>
                    <div class="kpi-value" id="totalAir">--</div>
                    <div class="kpi-subtext">AIR FREIGHT / TONS</div>
                </div>

                <div class="kpi-panel">
                    <div class="kpi-label">Tracked Providers</div>
                    <div class="kpi-value" id="providerCount">--</div>
                    <div class="kpi-subtext">A&amp;A GLOBAL FORWARDER RANKING</div>
                </div>
            </div>

            <div class="section-header">
                <div class="section-title">
                    GLOBAL FREIGHT FORWARDER RANKING
                </div>
                <div class="section-meta" id="tableMeta">
                    LOADING SNAPSHOT DATA...
                </div>
            </div>

            <div class="table-container">
                <table>
                    <thead>
                        <tr>
                            <th>Rank</th>
                            <th>Provider / Headquarters</th>
                            <th class="number-heading">Revenue</th>
                            <th class="number-heading">Ocean TEU</th>
                            <th class="number-heading">Air Tons</th>
                            <th class="number-heading">Rank Move</th>
                        </tr>
                    </thead>
                    <tbody id="rankingTableBody"></tbody>
                </table>
            </div>
        </section>

        <footer class="terminal-footer">
            <div>
                SOURCE:
                <a
                    href="__SOURCE_URL__"
                    target="_blank"
                    rel="noopener noreferrer"
                >A&amp;A / 3PL Logistics Top 25 Global Freight Forwarders</a>
            </div>
            <div>GENERATED: __GENERATED_AT__</div>
        </footer>
    </main>

    <script>
        const SNAPSHOTS = __SNAPSHOT_JSON__;
        const AVAILABLE_DATES = __DATES_JSON__;
        const DEFAULT_CURRENT_DATE = "__LATEST_DATE__";
        const DEFAULT_COMPARE_DATE = "__PREVIOUS_DATE__";

        const currentDateSelect = document.getElementById("currentDateSelect");
        const compareDateSelect = document.getElementById("compareDateSelect");
        const rankingTableBody = document.getElementById("rankingTableBody");

        function escapeHtml(value) {
            return String(value ?? "")
                .replace(/&/g, "&amp;")
                .replace(/</g, "&lt;")
                .replace(/>/g, "&gt;")
                .replace(/"/g, "&quot;")
                .replace(/'/g, "&#039;");
        }

        function compactNumber(value) {
            const number = Number(value);

            if (!Number.isFinite(number) || number === 0) {
                return "--";
            }

            const absolute = Math.abs(number);

            if (absolute >= 1000000000) {
                return `${(number / 1000000000).toFixed(2)}B`;
            }

            if (absolute >= 1000000) {
                return `${(number / 1000000).toFixed(2)}M`;
            }

            if (absolute >= 1000) {
                return `${(number / 1000).toFixed(1)}K`;
            }

            return Math.round(number).toLocaleString("en-US");
        }

        function percentageChange(current, previous) {
            const currentNumber = Number(current);
            const previousNumber = Number(previous);

            if (
                !Number.isFinite(currentNumber)
                || !Number.isFinite(previousNumber)
                || previousNumber === 0
            ) {
                return null;
            }

            return ((currentNumber - previousNumber) / Math.abs(previousNumber)) * 100;
        }

        function deltaClass(value) {
            if (value === null || value === undefined || !Number.isFinite(value)) {
                return "delta-neutral";
            }

            if (value > 0) {
                return "delta-positive";
            }

            if (value < 0) {
                return "delta-negative";
            }

            return "delta-neutral";
        }

        function deltaText(value) {
            if (value === null || value === undefined || !Number.isFinite(value)) {
                return "N/A";
            }

            if (value > 0) {
                return `+${value.toFixed(1)}%`;
            }

            return `${value.toFixed(1)}%`;
        }

        function metricCell(value, previousValue) {
            const delta = percentageChange(value, previousValue);

            return `
                <td class="number-cell">
                    <div class="metric-value">${compactNumber(value)}</div>
                    <div class="metric-delta ${deltaClass(delta)}">
                        ${deltaText(delta)}
                    </div>
                </td>
            `;
        }

        function rankMoveCell(currentRank, previousRecord) {
            if (!previousRecord) {
                return `
                    <td class="number-cell">
                        <div class="metric-delta delta-neutral">N/A</div>
                    </td>
                `;
            }

            const movement = Number(previousRecord.rank) - Number(currentRank);

            if (movement > 0) {
                return `
                    <td class="number-cell">
                        <div class="rank-up">▲ +${movement}</div>
                    </td>
                `;
            }

            if (movement < 0) {
                return `
                    <td class="number-cell">
                        <div class="rank-down">▼ ${movement}</div>
                    </td>
                `;
            }

            return `
                <td class="number-cell">
                    <div class="rank-flat">— 0</div>
                </td>
            `;
        }

        function populateDateSelectors() {
            currentDateSelect.innerHTML = "";
            compareDateSelect.innerHTML = "";

            AVAILABLE_DATES.forEach((date) => {
                const currentOption = document.createElement("option");
                currentOption.value = date;
                currentOption.textContent = date;
                currentDateSelect.appendChild(currentOption);

                const compareOption = document.createElement("option");
                compareOption.value = date;
                compareOption.textContent = date;
                compareDateSelect.appendChild(compareOption);
            });

            const noComparisonOption = document.createElement("option");
            noComparisonOption.value = "";
            noComparisonOption.textContent = "NO COMPARISON";
            compareDateSelect.insertBefore(
                noComparisonOption,
                compareDateSelect.firstChild
            );

            currentDateSelect.value = DEFAULT_CURRENT_DATE;

            if (DEFAULT_COMPARE_DATE) {
                compareDateSelect.value = DEFAULT_COMPARE_DATE;
            } else {
                compareDateSelect.value = "";
            }
        }

        function updateDashboard() {
            const currentDate = currentDateSelect.value;
            const compareDate = compareDateSelect.value;

            const currentData = SNAPSHOTS[currentDate] || [];
            const compareData = compareDate ? (SNAPSHOTS[compareDate] || []) : [];

            const compareMap = new Map(
                compareData.map((item) => [item.provider, item])
            );

            const totalRevenue = currentData.reduce(
                (sum, item) => sum + Number(item.revenue || 0),
                0
            );

            const totalTeu = currentData.reduce(
                (sum, item) => sum + Number(item.teu || 0),
                0
            );

            const totalAir = currentData.reduce(
                (sum, item) => sum + Number(item.air_tons || 0),
                0
            );

            document.getElementById("totalRevenue").textContent =
                compactNumber(totalRevenue);

            document.getElementById("totalTeu").textContent =
                compactNumber(totalTeu);

            document.getElementById("totalAir").textContent =
                compactNumber(totalAir);

            document.getElementById("providerCount").textContent =
                String(currentData.length).padStart(2, "0");

            document.getElementById("headerCurrentDate").textContent =
                currentDate || "--";

            document.getElementById("headerCompareDate").textContent =
                compareDate || "NO COMPARISON";

            document.getElementById("tableMeta").textContent =
                compareDate
                    ? `CURRENT: ${currentDate}  |  VS: ${compareDate}`
                    : `CURRENT: ${currentDate}  |  NO PRIOR SNAPSHOT SELECTED`;

            if (!currentData.length) {
                rankingTableBody.innerHTML = `
                    <tr>
                        <td colspan="6" class="empty-state">
                            NO DATA AVAILABLE FOR THE SELECTED SNAPSHOT.
                        </td>
                    </tr>
                `;
                return;
            }

            rankingTableBody.innerHTML = currentData.map((item) => {
                const previousRecord = compareMap.get(item.provider);

                return `
                    <tr>
                        <td class="rank-cell">
                            ${String(item.rank).padStart(2, "0")}
                        </td>

                        <td>
                            <div class="provider-name">
                                ${escapeHtml(item.provider)}
                            </div>
                            <div class="provider-hq">
                                ${escapeHtml(item.hq)}
                            </div>
                        </td>

                        ${metricCell(
                            item.revenue,
                            previousRecord ? previousRecord.revenue : null
                        )}

                        ${metricCell(
                            item.teu,
                            previousRecord ? previousRecord.teu : null
                        )}

                        ${metricCell(
                            item.air_tons,
                            previousRecord ? previousRecord.air_tons : null
                        )}

                        ${rankMoveCell(item.rank, previousRecord)}
                    </tr>
                `;
            }).join("");
        }

        currentDateSelect.addEventListener("change", () => {
            const selectedCurrentDate = currentDateSelect.value;

            if (compareDateSelect.value === selectedCurrentDate) {
                const alternativeDate = AVAILABLE_DATES.find(
                    (date) => date !== selectedCurrentDate
                );

                compareDateSelect.value = alternativeDate || "";
            }

            updateDashboard();
        });

        compareDateSelect.addEventListener("change", updateDashboard);

        populateDateSelectors();
        updateDashboard();
    </script>
</body>
</html>
"""

    final_html = (
        raw_html
        .replace("__SOURCE_URL__", html.escape(source_url, quote=True))
        .replace("__GENERATED_AT__", generated_at)
        .replace("__SNAPSHOT_JSON__", snapshot_json)
        .replace("__DATES_JSON__", dates_json)
        .replace("__LATEST_DATE__", latest_date or "")
        .replace("__PREVIOUS_DATE__", previous_date or "")
    )

    with open(STATIC_HTML_PATH, "w", encoding="utf-8") as file:
        file.write(final_html)

    log(f"Bloomberg/MI Terminal 대시보드 생성 완료: {STATIC_HTML_PATH}")

# =====================================================
# 7. Main Execution
# =====================================================
def main():
    parser = argparse.ArgumentParser(
        description="A&A Top 25 Forwarder crawler and MI Terminal dashboard generator"
    )

    parser.add_argument(
        "--build-static",
        action="store_true",
        help="기존 DB 데이터를 바탕으로 index.html만 재생성합니다."
    )

    parser.add_argument(
        "--refresh",
        action="store_true",
        help="데이터 크롤링 후 DB, CSV, 정적 HTML을 갱신합니다."
    )

    args = parser.parse_args()

    try:
        if args.build_static and not args.refresh:
            log("기존 DB 기준으로 Forwarder 정적 대시보드를 재생성합니다.")
            build_static_dashboard()
            log("정적 대시보드 재생성이 완료되었습니다.")
            return

        log("Top25 Forwarder 크롤링 및 Bloomberg/MI Terminal 대시보드 빌드를 시작합니다.")

        df = fetch_top_25_data(DEFAULT_URL)

        if df.empty:
            raise RuntimeError("크롤링 결과가 비어 있습니다.")

        save_today_data(df)
        build_static_dashboard()

        log("모든 작업이 정상적으로 완료되었습니다.")

    except Exception as error:
        print("\\n" + "=" * 60, file=sys.stderr)
        print("!!! 실행 중 치명적 오류 발생 !!!", file=sys.stderr)
        print(f"Error Type: {type(error).__name__}", file=sys.stderr)
        print(f"Error Message: {str(error)}", file=sys.stderr)
        print("=" * 60, file=sys.stderr)

        traceback.print_exc()

        print("=" * 60 + "\\n", file=sys.stderr)

        sys.exit(1)

if __name__ == "__main__":
    main()
