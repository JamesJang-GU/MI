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
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
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
    if not text: return 0.0
    try:
        num = float(text)
        return -num if is_negative else num
    except ValueError: return 0.0

def normalize_provider_name(value):
    return " ".join(str(value).strip().split())

def is_port_available(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError: return False

def find_available_port(host="127.0.0.1", preferred=8080):
    candidates = [preferred, 8080, 8081, 8082, 8000, 5000, 8501, 7860, 3000]
    checked = set()
    for port in candidates:
        if port in checked: continue
        checked.add(port)
        if is_port_available(host, port): return port
    for port in range(8000, 8101):
        if port not in checked and is_port_available(host, port): return port
    raise RuntimeError("사용 가능한 포트를 찾지 못했습니다.")

def wait_and_open_browser(url, host, port, timeout=20):
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            with socket.create_connection((host, port), timeout=1):
                webbrowser.open_new(url)
                return
        except Exception: time.sleep(0.4)

# =========================================================
# 3. Web Crawling
# =========================================================
def select_best_table(html_text):
    soup = BeautifulSoup(html_text, "html.parser")
    tables = soup.find_all("table")
    if not tables: raise RuntimeError("웹페이지에서 table 태그를 찾지 못했습니다.")
    candidates = []
    for index, table in enumerate(tables):
        try:
            dataframe = pd.read_html(StringIO(str(table)))[0]
        except Exception: continue
        if dataframe is None or dataframe.empty: continue
        content = (" ".join(map(str, dataframe.columns)) + " " + " ".join(map(str, dataframe.head(5).values.ravel()))).lower()
        score = 0
        for keyword in ["rank", "provider", "headquarters", "hq", "revenue", "teu", "air"]:
            if keyword in content: score += 5
        if dataframe.shape[1] >= 6: score += 3
        if dataframe.shape[0] >= 20: score += 5
        if any(co in content for co in ["dhl", "kuehne", "db schenker", "dsv", "ceva"]): score += 3
        candidates.append((score, index, dataframe))
    if not candidates: raise RuntimeError("pandas로 읽을 수 있는 테이블을 찾지 못했습니다.")
    candidates.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    best_score, _, best_dataframe = candidates[0]
    if best_score <= 0: raise RuntimeError("Top 25 Freight Forwarder 표를 식별하지 못했습니다.")
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
    if raw_dataframe.shape[1] < 6: raise RuntimeError(f"예상보다 컬럼 수가 적습니다. 현재 shape={raw_dataframe.shape}")
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
    if dataframe.empty or len(dataframe) < 20: raise RuntimeError(f"데이터 검증 실패: 수집된 업체 수가 너무 적습니다. ({len(dataframe)} rows)")
    log(f"[FETCH] 수집 완료: {len(dataframe)} rows")
    return dataframe

# =========================================================
# 4. SQLite Database
# =========================================================
def get_connection():
    connection = sqlite3.connect(DB_PATH)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS forwarders (
            date TEXT NOT NULL, rank INTEGER, provider TEXT NOT NULL, hq TEXT,
            revenue REAL, teu REAL, air_tons REAL, source_url TEXT, created_at TEXT,
            PRIMARY KEY (date, provider)
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS run_log (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT, status TEXT, message TEXT, row_count INTEGER
        )
    """)
    connection.commit()
    return connection

def save_run_log(status, message, row_count=0):
    conn = get_connection()
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn.execute("INSERT INTO run_log (run_at, status, message, row_count) VALUES (?, ?, ?, ?)",
                     (now, status, str(message)[:1000], row_count))
        conn.commit()
    finally: conn.close()

def save_today_data(current_dataframe):
    connection = get_connection()
    today = dt.date.today().isoformat()
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        save_dataframe = current_dataframe.copy()
        save_dataframe["created_at"] = now
        connection.execute("DELETE FROM forwarders WHERE date = ?", (today,))
        save_dataframe.to_sql("forwarders", connection, if_exists="append", index=False)
        connection.execute("INSERT INTO run_log (run_at, status, message, row_count) VALUES (?, ?, ?, ?)",
                           (now, "SUCCESS", "OK", len(save_dataframe)))
        connection.commit()
        log(f"[DB] 저장 완료: {DB_PATH} / rows={len(save_dataframe)}")
    except Exception as error:
        connection.rollback()
        connection.execute("INSERT INTO run_log (run_at, status, message, row_count) VALUES (?, ?, ?, ?)",
                           (now, "ERROR", str(error)[:1000], 0))
        connection.commit()
        raise
    finally: connection.close()

def refresh_data(url=DEFAULT_URL):
    current_dataframe = fetch_top_25_data(url=url)
    save_today_data(current_dataframe)
    return current_dataframe

def load_history():
    connection = get_connection()
    try:
        return pd.read_sql_query("SELECT date, rank, provider, hq, revenue, teu, air_tons, source_url, created_at FROM forwarders ORDER BY date DESC, rank ASC", connection)
    finally: connection.close()

def get_last_run_meta():
    connection = get_connection()
    try:
        row = connection.execute("SELECT run_at, status, message, row_count FROM run_log ORDER BY run_id DESC LIMIT 1").fetchone()
        return {"run_at": row[0], "status": row[1], "message": row[2], "row_count": row[3]} if row else {"run_at": "-", "status": "-", "message": "-", "row_count": 0}
    finally: connection.close()

# =========================================================
# 5. Bloomberg / MI Terminal Dashboard HTML Template
# =========================================================
HTML = r'''


  
  
  Forwarder Dashboard | A&A Top 25 Global Freight Forwarders
  


  

    

      
MI<GO>GLOBAL FREIGHT FORWARDER MONITOR

      
DATABASE {{ meta.status }}

    

    

      FUNCTIONS / TIME TRAVEL
      [F1] DASHBOARD
      [F2] DOWNLOAD CSV
      {% if not static_mode %}[F5] REFRESH DATA{% endif %}
      [P] PRINT
    

    {% if message %}
SYSTEM MESSAGE // {{ message }}
{% endif %}
    

      
SEA / FORWARDER / RANKING INTELLIGENCE
A&A Top 25 Global Freight Forwarders

      
LAST RUN {{ meta.run_at }}
SNAPSHOTS 0

    

    {% if is_empty %}
      
NO DATA AVAILABLE

    {% else %}
    

      
LATEST SNAPSHOT{{ latest_date }}

      
TOTAL REVENUE{{ total_revenue }}

      
TOTAL OCEAN TEU{{ total_teu }}

      
TOTAL AIR TONS{{ total_air }}

      
BASE DATE-

    

    

      

        
Delta Analysis / Latest vs Base Date

        

          BASE DATE
          
          RESET
        

      

      

        
REVENUE Δ-

        
OCEAN TEU Δ-

        
AIR TONS Δ-

        
MATCHED-

      

      

        

          
            Rank	Provider	HQ	Rank Δ	Revenue ($M)	Rev Δ	TEU	TEU Δ	Air	Air Δ

          
          
        

      

    

    

      

        
Top 10 Revenue Ranking

        

      

      

        
Snapshot History

        

      

    

    

      
SOURCE // {{ source_url }}

      
DATASET: A&A TOP 25 / STATIC: {{ "ON" if static_mode else "OFF" }}

    

    {% endif %}
    
  {% endif %}
  
