import argparse
import datetime as dt
import html
import importlib.util
import os
import sqlite3
import subprocess
import sys
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

def format_delta_text(val):
    if val is None: return "N/A"
    # SyntaxError 방지: f-string 내부에서 조건문을 쓰지 않고 밖에서 계산된 값을 받음
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
    log(f"[FETCH] {url}")
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
        raise
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
                "rank": int(r["rank"]), "provider": str(r["provider"]), "hq": str(r["hq"]),
                "revenue": float(r["revenue"]), "teu": float(r["teu"]), "air_tons": float(r["air_tons"]),
                "revenue_delta": delta
            })
        return data, l_date, p_date
    finally: conn.close()

# =====================================================
# 5. Dashboard Generation
# =====================================================
def build_static_dashboard():
    # [FIXED] data_list -> data
    data, ref_date, comp_date = get_dashboard_data()
    
    if not data: 
        raise RuntimeError("No data for dashboard.")
        
    rows_html = ""
    for item in data:
        d_val = item["revenue_delta"]
        # 색상 클래스 결정
        if d_val is not None and d_val > 0:
            d_cls = "delta-up"
        elif d_val is not None and d_val < 0:
            d_cls = "delta-down"
        else:
            d_cls = "delta-neutral"
            
        # [FIXED] f-string 내부 백슬래시 제거: 미리 계산된 d_text 사용
        d_text = format_delta_text(d_val)
        
        rows_html += f"""
            <tr>
                <td class="rank">{item['rank']:02d}</td>
                <td><div class="provider">{html.escape(item['provider'])}</div><div class="hq">{html.escape(item['hq'])}</div></td>
                <td class="number">{format_compact_number(item['revenue'])}</td>
                <td class="number">{format_compact_number(item['teu'])}</td>
                <td class="number">{format_compact_number(item['air_tons'])}</td>
                <td class="number {d_cls}">{d_text}</td>
            </tr>
        """

    # [FIXED] data_list -> data
    total_rev = sum(i["revenue"] for i in data)
    total_teu = sum(i["teu"] for i in data)
    total_air = sum(i["air_tons"] for i in data)
    total_providers = len(data)

    comp_txt = f"VS {comp_date}" if comp_date else "NO PRIOR SNAPSHOT"
    
    html_template = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>FORWARDER MONITOR | MI Terminal</title>
    <style>
        :root {{ --bg: #07111c; --panel: #0d1c2b; --panel-2: #10253a; --line: #27415b; --text: #eef5fb; --muted: #8fa5b8; --cyan: #00d8ff; --green: #41e29a; --red: #ff647c; --orange: #ffab40; }}
        body {{ margin: 0; background: var(--bg); color: var(--text); font-family: Arial, sans-serif; }}
        .page {{ max-width: 1480px; margin: 0 auto; padding: 24px; }}
        .terminal-header {{ display: flex; justify-content: space-between; align-items: flex-end; border-bottom: 2px solid var(--cyan); padding-bottom: 18px; }}
        .terminal-header h1 {{ margin: 0; color: var(--cyan); font-size: 32px; }}
        .reference {{ text-align: right; color: var(--orange); font-family: monospace; font-size: 13px; }}
        .kpi-strip {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 18px 0; }}
        .kpi {{ background: var(--panel); border: 1px solid var(--line); padding: 16px; }}
        .kpi .stat {{ display: block; color: var(--cyan); font-size: 25px; font-weight: 700; font-family: monospace; }}
        .kpi .label {{ display: block; margin-top: 8px; color: var(--muted); font-size: 11px; text-transform: uppercase; }}
        .table-wrap {{ background: var(--panel); border: 1px solid var(--line); overflow-x: auto; }}
        table {{ width: 100%; border-collapse: collapse; }}
        thead {{ background: var(--panel-2); }}
        th {{ padding: 13px; color: var(--cyan); text-align: left; font-size: 11px; text-transform: uppercase; border-bottom: 1px solid var(--line); }}
        td {{ padding: 14px; border-bottom: 1px solid rgba(39, 65, 91, 0.75); font-size: 13px; }}
        .rank {{ color: var(--orange); font-family: monospace; font-weight: 700; }}
        .provider {{ font-weight: 700; }}
        .hq {{ color: var(--muted); font-size: 11px; }}
        .number {{ text-align: right; font-family: monospace; }}
        .delta-up {{ color: var(--green); }}
        .delta-down {{ color: var(--red); }}
        .delta-neutral {{ color: var(--muted); }}
    </style>
</head>
<body>
    <div class="page">
        <div class="terminal-header">
            <h1>A&A Top 25 Global Freight Forwarders</h1>
            <div class="reference">DATA AS OF: {ref_date} | {comp_txt}</div>
        </div>
        
        <div class="kpi-strip">
            <div class="kpi">
                <span class="stat"{len(data)}</span>
                <span class="label">Total Providers</span>
            </div>
            <div class="kpi">
                <span class="stat"{format_compact_number(total_rev)}</span>
                <span class="label">Total Revenue</span>
            </div>
            <div class="kpi">
                <span class="stat"{format_compact_number(total_teu)}</span>
                <span class="label">Total Ocean TEU</span>
            </div>
            <div class="kpi">
                <span class="stat"{format_compact_number(total_air)}</span>
                <span class="label">Total Air Tons</span>
            </div>
        </div>

        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th>Rank</th>
                        <th>Provider / HQ</th>
                        <th>Revenue</th>
                        <th>Ocean TEU</th>
                        <th>Air Tons</th>
                        <th>Rev. Delta</th>
                    </tr>
                </thead>
                <tbody>
                    {rows_html}
                </tbody>
            </table>
        </div>
    </div>
</body>
</html>"""
    
    with open(STATIC_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html_template)
    log(f"[SUCCESS] Dashboard generated: {STATIC_HTML_PATH}")

# =====================================================
# 5. Main Execution
# =====================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["crawl", "dashboard"], default="crawl")
    args = parser.parse_args()

    if args.mode == "crawl":
        log("Starting crawl mode...")
        df = fetch_top_25_data(DEFAULT_URL)
        save_today_data(df)
        log("Crawl completed successfully.")
    elif args.mode == "dashboard":
        log("Starting dashboard generation mode...")
        build_static_dashboard()
        log("Dashboard generation completed successfully.")
