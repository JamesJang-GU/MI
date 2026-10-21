import argparse
import datetime as dt
import html
import importlib.util
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
            subprocess.check_call([sys.executable, "-m", "pip", "install", pip_name])
        except Exception:
            # GitHub Actions 환경 대응
            subprocess.check_call([sys.executable, "-m", "pip", "install", pip_name, "--break-system-packages"])

setup_environment()

import pandas as pd
import requests
from bs4 import BeautifulSoup

# =====================================================
# 2. Configuration
# =====================================================
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_URL = "https://www.3plogistics.com/3pl-market-info-resources/3pl-market-information/aas-top-25-global-freight-forwarders-list/"
DB_PATH = SCRIPT_DIR / "freight_forwarders_data.db"
STATIC_HTML_PATH = SCRIPT_DIR / "index.html"
STATIC_CSV_PATH = SCRIPT_DIR / "freight_forwarders_history.csv"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
EXPECTED_COLUMNS = ["rank", "provider", "hq", "revenue", "teu", "air_tons"]

# =====================================================
# 3. Utilities
# =====================================================
def log(message):
    print(f"[LOG] {message}", flush=True)

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

def format_compact_number(value, suffix=""):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "--"
    if number == 0: return "--"
    if abs(number) >= 1_000_000_000: return f"{number / 1_000_000_000:,.2f}B{suffix}"
    if abs(number) >= 1_000_000: return f"{number / 1_000_000:,.2f}M{suffix}"
    if abs(number) >= 1_000: return f"{number / 1_000:,.1f}K{suffix}"
    return f"{number:,.0f}{suffix}"

def calculate_percentage_change(current, previous):
    try:
        c, p = float(current), float(previous)
        return ((c - p) / abs(p)) * 100 if p != 0 else None
    except: return None

def get_delta_text(val):
    # f-string 내부에서 조건문을 쓰지 않기 위해 외부에서 처리
    if val is None:
        return "N/A"
    prefix = "+" if val > 0 else ""
    return f"{prefix}{val:.1f}%"

# =====================================================
# 4. Web Crawling & DB
# =====================================================
def select_best_table(html_text):
    soup = BeautifulSoup(html_text, "html.parser")
    tables = soup.find_all("table")
    if not tables: raise RuntimeError("No tables found.")
    candidates = []
    for i, table in enumerate(tables):
        try:
            df = pd.read_html(StringIO(str(table)))[0]
            if df.empty: continue
            score = sum(5 for k in ["rank", "provider", "revenue"] if k in str(df.columns).lower())
            candidates.append((score, i, df))
        except: continue
    if not candidates: raise RuntimeError("No suitable table.")
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][2]

def fetch_top_25_data(url):
    log(f"Fetching: {url}")
    res = requests.get(url, headers=HEADERS, timeout=30)
    res.raise_for_status()
    df = select_best_table(res.text)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [" ".join(str(c) for c in col if str(c).lower() != "nan").strip() for col in df.columns]
    else:
        df.columns = [str(c).strip() for c in df.columns]
    
    df = df.iloc[:, :6].copy()
    df.columns = EXPECTED_COLUMNS
    for col in ["rank", "revenue", "teu", "air_tons"]:
        df[col] = df[col].apply(clean_number)
    df["rank"] = df["rank"].fillna(0).astype(int)
    df["provider"] = df["provider"].apply(normalize_provider_name)
    df["hq"] = df["hq"].astype(str).str.strip()
    df["date"] = dt.date.today().isoformat()
    df["source_url"] = url
    return df.sort_values("rank").reset_index(drop=True)

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS forwarders (date TEXT, rank INTEGER, provider TEXT, hq TEXT, revenue REAL, teu REAL, air_tons REAL, source_url TEXT, created_at TEXT, PRIMARY KEY (date, provider))")
    conn.execute("CREATE TABLE IF NOT EXISTS run_log (run_id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT, status TEXT, message TEXT, row_count INTEGER)")
    conn.commit()
    return conn

def save_today_data(df):
    conn = get_connection()
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        df["created_at"] = now
        conn.execute("DELETE FROM forwarders WHERE date = ?", (df["date"].iloc[0],))
        df.to_sql("forwarders", conn, if_exists="append", index=False)
        conn.execute("INSERT INTO run_log (run_at, status, message, row_count) VALUES (?, 'SUCCESS', 'OK', ?)", (now, len(df)))
        conn.commit()
        hist = pd.read_sql_query("SELECT * FROM forwarders ORDER BY date DESC, rank ASC", conn)
        hist.to_csv(STATIC_CSV_PATH, index=False, encoding="utf-8-sig")
    except Exception as e:
        conn.execute("INSERT INTO run_log (run_at, status, message, row_count) VALUES (?, 'ERROR', ?, 0)", (now, str(e)))
        conn.commit()
        raise e
    finally: conn.close()

def get_dashboard_data():
    conn = get_connection()
    try:
        latest_res = conn.execute("SELECT MAX(date) FROM forwarders").fetchone()
        if not latest_res[0]: return [], None, None
        l_date = latest_res[0]
        prev_res = conn.execute("SELECT MAX(date) FROM forwarders WHERE date < ?", (l_date,)).fetchone()
        p_date = prev_res[0] if prev_res[0] else None
        
        l_df = pd.read_sql_query("SELECT * FROM forwarders WHERE date = ? ORDER BY rank ASC", conn, params=(l_date,))
        
        if p_date:
            p_df = pd.read_sql_query("SELECT provider, revenue as prev_rev FROM forwarders WHERE date = ?", conn, params=(p_date,))
            l_df = l_df.merge(p_df, on="provider", how="left")
        else:
            l_df["prev_rev"] = None
            
        data = []
        for _, r in l_df.iterrows():
            delta = calculate_percentage_change(r["revenue"], r.get("prev_rev"))
            data.append({
                "rank": int(r["rank"]),
                "provider": str(r["provider"]),
                "hq": str(r["hq"]),
                "revenue": float(r["revenue"]),
                "teu": float(r["teu"]),
                "air_tons": float(r["air_tons"]),
                "revenue_delta": delta
            })
        return data, l_date, p_date
    finally: conn.close()

# =====================================================
# 5. Dashboard Generation
# =====================================================
def build_static_dashboard():
    data, ref_date, comp_date = get_dashboard_data()
    
    if not data: 
        raise RuntimeError("No data found in database to build dashboard.")
        
    rows_html = ""
    for item in data:
        d_val = item["revenue_delta"]
        
        # 1. Delta color class (Pre-calculate outside f-string)
        if d_val is not None and d_val > 0:
            d_cls = "delta-up"
        elif d_val is not None and d_val < 0:
            d_cls = "delta-down"
        else:
            d_cls = "delta-neutral"
            
        # 2. Delta text (Pre-calculate outside f-string)
        d_text = get_delta_text(d_val)
        
        # 3. HTML Construction
        rows_html += f"""
            
                {item['rank']:02d}
                
{html.escape(item['provider'])}
{html.escape(item['hq'])}

                {format_compact_number(item['revenue'])}
                {format_compact_number(item['teu'])}
                {format_compact_number(item['air_tons'])}
                {d_text}
            
        """

    total_rev = sum(i["revenue"] for i in data)
    total_teu = sum(i["teu"] for i in data)
    total_air = sum(i["air_tons"] for i in data)
    
    comp_txt = f"VS {comp_date}" if comp_date else "NO PRIOR SNAPSHOT"
    
    html_template = f"""
<!DOCTYPE html>
<html>
<head>
<title>Forwarder Dashboard</title>
</head>
<body>

<h1>FORWARDER MONITOR</h1>

{rows_html}

</body>
</html>
"""


    
    FORWARDER MONITOR | MI Terminal
