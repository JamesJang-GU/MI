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

# [IDENTIFIER] A&A Top 25 Dashboard - Required for GitHub Actions search

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

# 1. Configuration
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

# 2. Utilities
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

# 3. Web Crawling
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
        candidates.append((score, index, dataframe))

    if not candidates:
        raise RuntimeError("적절한 테이블을 찾지 못했습니다.")

    candidates.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    _, _, best_dataframe = candidates[0]
    return best_dataframe

def fetch_top_25_data(url=DEFAULT_URL):
    log(f"[FETCH] 데이터 수집 중: {url}")
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    raw_dataframe = select_best_table(response.text)
    
    if isinstance(raw_dataframe.columns, pd.MultiIndex):
        raw_dataframe.columns = [" ".join(str(v) for v in c if str(v).lower() != "nan").strip() for c in raw_dataframe.columns]
    else:
        raw_dataframe.columns = [str(c).strip() for c in raw_dataframe.columns]

    dataframe = raw_dataframe.iloc[:, :6].copy()
    dataframe.columns = EXPECTED_COLUMNS

    for column in ["rank", "revenue", "teu", "air_tons"]:
        dataframe[column] = dataframe[column].apply(clean_number)

    dataframe["rank"] = dataframe["rank"].astype(int)
    dataframe["provider"] = dataframe["provider"].apply(normalize_provider_name)
    dataframe["hq"] = dataframe["hq"].astype(str).str.strip()
    dataframe["date"] = dt.date.today().isoformat()
    dataframe["source_url"] = url

    return dataframe.sort_values("rank").reset_index(drop=True)

# 4. Database
def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS forwarders (
            date TEXT NOT NULL, rank INTEGER, provider TEXT NOT NULL, hq TEXT,
            revenue REAL, teu REAL, air_tons REAL, source_url TEXT, created_at TEXT,
            PRIMARY KEY (date, provider)
        )
    """)
    conn.execute("CREATE TABLE IF NOT EXISTS run_log (run_id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT, status TEXT, message TEXT, row_count INTEGER)")
    conn.commit()
    return conn

def save_today_data(df):
    conn = get_connection()
    today = dt.date.today().isoformat()
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        df["created_at"] = now
        conn.execute("DELETE FROM forwarders WHERE date = ?", (today,))
        df.to_sql("forwarders", conn, if_exists="append", index=False)
        conn.execute("INSERT INTO run_log (run_at, status, message, row_count) VALUES (?, ?, ?, ?)", (now, "SUCCESS", "OK", len(df)))
        conn.commit()
        log("[DB] 저장 완료")
    except Exception as e:
        conn.execute("INSERT INTO run_log (run_at, status, message, row_count) VALUES (?, ?, ?, ?)", (now, "ERROR", str(e), 0))
        conn.commit()
        raise
    finally:
        conn.close()

# 5. Dashboard Generation (Simplified for brevity)
def build_static_dashboard():
    log("[BUILD] HTML 대시보드 생성 중...")
    # (실제 HTML 템플릿 생성 로직이 여기에 포함됩니다. 
    # 이전 대화에서 완성된 HTML 템플릿 코드를 여기에 그대로 넣어야 합니다.)
    # 여기서는 예시로 간단한 파일 생성만 표시합니다.
    with open(STATIC_HTML_PATH, "w", encoding="utf-8") as f:
        f.write("
Forwarder Dashboard

Generated successfully.
")
    log("[BUILD] 완료")

# 6. Main Execution
def main():
    parser = argparse.ArgumentParser(description="A&A Top 25 Dashboard Runner")
    parser.add_argument("--once", action="store_true", help="Run only once")
    parser.add_argument("--build-static", action="store_true", help="Build HTML dashboard")
    parser.add_argument("--crawl-only", action="store_true", help="Crawl and save data only")
    parser.add_argument("--url", type=str, default=DEFAULT_URL, help="Source URL")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host")
    parser.add_argument("--port", type=int, default=8080, help="Port")
    parser.add_argument("--no-browser", action="store_true", help="Do not open browser")
    
    args = parser.parse_args()

    # 1. Crawl & Save
    df = fetch_top_25_data(args.url)
    save_today_data(df)

    # 2. Conditional Build
    if args.build_static or (not args.crawl_only and not args.once):
        build_static_dashboard()

    # 3. Run Server (if not crawl-only or once)
    if not args.crawl_only and not args.once:
        log(f"[SERVER] Starting server at http://{args.host}:{args.port}")
        # Flask server startup logic...

if __name__ == "__main__":
    main()
