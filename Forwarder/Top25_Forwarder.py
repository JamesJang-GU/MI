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
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}
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
        c, p = float(current), float(previous)
        return ((c - p) / abs(p)) * 100 if p != 0 else None
    except Exception:
        return None

def get_delta_text(val):
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
    if not tables:
        raise RuntimeError("페이지에서 <table> 태그를 찾지 못했습니다.")
    candidates = []
    for i, table in enumerate(tables):
        try:
            df = pd.read_html(StringIO(str(table)))[0]
            if df.empty:
                continue
            cols_str = str(df.columns).lower()
            score = sum(5 for k in ["rank", "provider", "revenue", "gross"] if k in cols_str)
            candidates.append((score, i, df))
        except Exception:
            continue
    if not candidates:
        raise RuntimeError("적합한 데이터 테이블을 파싱하지 못했습니다.")
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][2]

def fetch_top_25_data(url):
    log(f"Fetching URL: {url}")
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
    finally:
        conn.close()

def get_dashboard_data():
    conn = get_connection()
    try:
        latest_res = conn.execute("SELECT MAX(date) FROM forwarders").fetchone()
        if not latest_res or not latest_res[0]:
            return [], None, None
        l_date = latest_res[0]
        prev_res = conn.execute("SELECT MAX(date) FROM forwarders WHERE date < ?", (l_date,)).fetchone()
        p_date = prev_res[0] if (prev_res and prev_res[0]) else None

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
    finally:
        conn.close()

# =====================================================
# 5. Dashboard Generation (f-string 제거 및 치환 방식 적용)
# =====================================================
def build_static_dashboard():
    data, ref_date, comp_date = get_dashboard_data()

    if not data:
        raise RuntimeError("대시보드를 생성할 데이터가 DB에 없습니다.")

    row_list = []
    for item in data:
        d_val = item["revenue_delta"]
        if d_val is not None and d_val > 0:
            d_cls = "delta-up"
        elif d_val is not None and d_val < 0:
            d_cls = "delta-down"
        else:
            d_cls = "delta-neutral"

        d_text = get_delta_text(d_val)
        p_name = html.escape(item["provider"])
        hq_name = html.escape(item["hq"])
        rev_str = format_compact_number(item["revenue"])
        teu_str = format_compact_number(item["teu"])
        air_str = format_compact_number(item["air_tons"])
        rank_str = f"{item['rank']:02d}"

        row_html = (
            "<tr>"
            + f"<td class=\"rank-cell\">{rank_str}</td>"
            + f"<td><div class=\"provider\">{p_name}</div><div class=\"hq\">{hq_name}</div></td>"
            + f"<td class=\"num-cell\">{rev_str}</td>"
            + f"<td class=\"num-cell\">{teu_str}</td>"
            + f"<td class=\"num-cell\">{air_str}</td>"
            + f"<td class=\"delta-cell {d_cls}\">{d_text}</td>"
            + "</tr>"
        )
        row_list.append(row_html)

    rows_html = "\n".join(row_list)
    total_rev = sum(i["revenue"] for i in data)
    total_teu = sum(i["teu"] for i in data)
    total_air = sum(i["air_tons"] for i in data)
    comp_txt = f"VS {comp_date}" if comp_date else "NO PRIOR SNAPSHOT"

    raw_html = """<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <title>FORWARDER MONITOR | MI Terminal</title>
    <style>
        :root {
            --bg: #0a0e14;
            --panel: #111822;
            --border: #1e293b;
            --text: #e2e8f0;
            --muted: #64748b;
            --accent: #38bdf8;
            --green: #22c55e;
            --red: #ef4444;
        }
        body {
            margin: 0;
            padding: 24px;
            background: var(--bg);
            color: var(--text);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        }
        .page { max-width: 1200px; margin: 0 auto; }
        .terminal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 2px solid var(--accent);
            padding-bottom: 12px;
            margin-bottom: 24px;
        }
        .terminal-header h1 { margin: 0; font-size: 20px; letter-spacing: 1px; color: var(--accent); }
        .reference { font-size: 13px; color: var(--muted); }
        .kpi-strip {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 16px;
            margin-bottom: 24px;
        }
        .kpi {
            background: var(--panel);
            border: 1px solid var(--border);
            padding: 16px;
            border-radius: 6px;
        }
        .kpi .stat { display: block; font-size: 22px; font-weight: bold; color: var(--accent); }
        .kpi .label { display: block; font-size: 12px; color: var(--muted); margin-top: 4px; }
        .table-wrap {
            background: var(--panel);
            border: 1px solid var(--border);
            border-radius: 6px;
            overflow-x: auto;
        }
        table { width: 100%; border-collapse: collapse; font-size: 14px; text-align: left; }
        th { background: rgba(0,0,0,0.2); padding: 12px; color: var(--muted); font-size: 12px; border-bottom: 1px solid var(--border); }
        td { padding: 12px; border-bottom: 1px solid var(--border); }
        .rank-cell { font-weight: bold; color: var(--accent); }
        .provider { font-weight: 600; }
        .hq { font-size: 12px; color: var(--muted); }
        .num-cell { text-align: right; font-variant-numeric: tabular-nums; }
        .delta-cell { text-align: right; font-weight: bold; font-variant-numeric: tabular-nums; }
        .delta-up { color: var(--green); }
        .delta-down { color: var(--red); }
        .delta-neutral { color: var(--muted); }
    </style>
</head>
<body>
    <div class="page">
        <div class="terminal-header">
            <h1>FORWARDER MONITOR</h1>
            <div class="reference">DATA DATE: __REF_DATE__ | __COMP_TXT__</div>
        </div>
        <div class="kpi-strip">
            <div class="kpi"><span class="stat">__TOTAL_REV__</span><span class="label">Total Revenue</span></div>
            <div class="kpi"><span class="stat">__TOTAL_TEU__</span><span class="label">Total Ocean TEU</span></div>
            <div class="kpi"><span class="stat">__TOTAL_AIR__</span><span class="label">Total Air Tons</span></div>
            <div class="kpi"><span class="stat">__PROVIDER_COUNT__</span><span class="label">Providers</span></div>
        </div>
        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th>Rank</th>
                        <th>Provider / HQ</th>
                        <th style="text-align: right;">Revenue (USD)</th>
                        <th style="text-align: right;">Ocean (TEU)</th>
                        <th style="text-align: right;">Air (Tons)</th>
                        <th style="text-align: right;">Rev Delta</th>
                    </tr>
                </thead>
                <tbody>
__ROWS_HTML__
                </tbody>
            </table>
        </div>
    </div>
</body>
</html>"""

    final_html = (
        raw_html
        .replace("__REF_DATE__", str(ref_date))
        .replace("__COMP_TXT__", comp_txt)
        .replace("__TOTAL_REV__", format_compact_number(total_rev))
        .replace("__TOTAL_TEU__", format_compact_number(total_teu))
        .replace("__TOTAL_AIR__", format_compact_number(total_air))
        .replace("__PROVIDER_COUNT__", str(len(data)))
        .replace("__ROWS_HTML__", rows_html)
    )

    with open(STATIC_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(final_html)
    log(f"대시보드 생성 완료: {STATIC_HTML_PATH}")

# =====================================================
# 6. Main Execution
# =====================================================
def main():
    try:
        log("Top25 Forwarder 크롤링 및 대시보드 빌드 시작")
        df = fetch_top_25_data(DEFAULT_URL)
        save_today_data(df)
        build_static_dashboard()
        log("모든 작업이 정상적으로 완료되었습니다.")
    except Exception as e:
        print("\n" + "="*50, file=sys.stderr)
        print("!!! 실행 중 치명적 오류 발생 !!!", file=sys.stderr)
        print(f"Error Type: {type(e).__name__}", file=sys.stderr)
        print(f"Error Message: {str(e)}", file=sys.stderr)
        print("="*50, file=sys.stderr)
        traceback.print_exc()
        print("="*50 + "\n", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
