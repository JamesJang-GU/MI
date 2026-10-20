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
DEFAULT_URL = (
    "https://www.3plogistics.com/3pl-market-info-resources/"
    "3pl-market-information/aas-top-25-global-freight-forwarders-list/"
)
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
    """음수 및 특수문자가 포함된 숫자 값을 float으로 정제한다."""
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

        content = (" ".join(map(str, dataframe.columns)) + " " + 
                   " ".join(map(str, dataframe.head(5).values.ravel()))).lower()
        score = 0
        for keyword in ["rank", "provider", "headquarters", "hq", "revenue", "teu", "air"]:
            if keyword in content: score += 5
        if dataframe.shape[1] >= 6: score += 3
        if dataframe.shape[0] >= 20: score += 5
        if any(co in content for co in ["dhl", "kuehne", "db schenker", "dsv", "ceva"]): score += 3
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

    # 데이터 검증
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
    finally:
        connection.close()

def refresh_data(url=DEFAULT_URL):
    current_dataframe = fetch_top_25_data(url=url)
    save_today_data(current_dataframe)
    return current_dataframe

def load_history():
    connection = get_connection()
    try:
        return pd.read_sql_query("SELECT date, rank, provider, hq, revenue, teu, air_tons, source_url, created_at FROM forwarders ORDER BY date DESC, rank ASC", connection)
    finally:
        connection.close()

def get_last_run_meta():
    connection = get_connection()
    try:
        row = connection.execute("SELECT run_at, status, message, row_count FROM run_log ORDER BY run_id DESC LIMIT 1").fetchone()
        return {"run_at": row[0], "status": row[1], "message": row[2], "row_count": row[3]} if row else {"run_at": "-", "status": "-", "message": "-", "row_count": 0}
    finally:
        connection.close()

# =========================================================
# 5. Dashboard HTML Template
# =========================================================
HTML = r'''<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Forwarder Dashboard</title>
  <style>
    body {
      margin: 0;
      padding: 30px;
      background: #07111c;
      color: #e8f1f8;
      font-family: Arial, "Noto Sans KR", sans-serif;
    }

    h1 { color: #ff8a00; }

    .box {
      max-width: 900px;
      padding: 24px;
      background: #0d1b29;
      border: 1px solid #29455e;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      margin-top: 20px;
    }

    th, td {
      padding: 10px;
      border: 1px solid #29455e;
      text-align: left;
    }

    th {
      color: #ffb04a;
      background: #13283a;
    }
  </style>
</head>
<body>
  <div class="box">
    <h1>A&A Top 25 Global Freight Forwarders</h1>

    <p>
      Last run: {{ meta.run_at }}
      / Status: {{ meta.status }}
      / Rows: {{ meta.row_count }}
    </p>

    {% if is_empty %}
      <p>표시할 데이터가 없습니다.</p>
    {% else %}
      <p>Latest Date: <strong>{{ latest_date }}</strong></p>
      <p>Total Revenue: <strong>{{ total_revenue }}</strong></p>
      <p>Total Ocean TEU: <strong>{{ total_teu }}</strong></p>
      <p>Total Air Tons: <strong>{{ total_air }}</strong></p>

      <table>
        <thead>
          <tr>
            <th>Rank</th>
            <th>Provider</th>
            <th>HQ</th>
            <th>Revenue ($M)</th>
            <th>Ocean TEU</th>
            <th>Air Tons</th>
          </tr>
        </thead>
        <tbody id="rankingTableBody"></tbody>
      </table>
    {% endif %}
  </div>

  {% if not is_empty %}
  <script>
    const dataByDate = {{ data_json | safe }};
    const latestDate = "{{ latest_date }}";
    const rows = dataByDate[latestDate] || [];

    function escapeHtml(value) {
      return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
    }

    function formatNumber(value) {
      return new Intl.NumberFormat("en-US").format(Number(value || 0));
    }

    document.getElementById("rankingTableBody").innerHTML = rows.map(row => `
      <tr>
        <td>${formatNumber(row.rank)}</td>
        <td>${escapeHtml(row.provider)}</td>
        <td>${escapeHtml(row.hq)}</td>
        <td>${formatNumber(row.revenue)}</td>
        <td>${formatNumber(row.teu)}</td>
        <td>${formatNumber(row.air_tons)}</td>
      </tr>
    `).join("");
  </script>
  {% endif %}
</body>
</html>
'''

# =========================================================
# 6. Dashboard Rendering
# =========================================================
def render_dashboard_html(message="", static_mode=False, source_url=DEFAULT_URL):
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

    numeric_columns = ["rank", "revenue", "teu", "air_tons"]

    for column in numeric_columns:
        dataframe[column] = pd.to_numeric(
            dataframe[column],
            errors="coerce",
        ).fillna(0)

    dataframe["date"] = dataframe["date"].astype(str)

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
            ["rank", "provider", "hq", "revenue", "teu", "air_tons"]
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
        data_json=json.dumps(data_by_date, ensure_ascii=False),
        dates_json=json.dumps(all_dates, ensure_ascii=False),
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
    return render_dashboard_html(message=request.args.get("msg", ""), static_mode=False, source_url=(RUNTIME_CONFIG.url if RUNTIME_CONFIG else DEFAULT_URL))

@app.route("/refresh")
def refresh():
    try:
        refresh_data(url=(RUNTIME_CONFIG.url if RUNTIME_CONFIG else DEFAULT_URL))
        return redirect(url_for("index", msg="데이터 수집 및 DB 저장 완료"))
    except Exception as error:
        return redirect(url_for("index", msg=f"데이터 수집 실패: {error}"))

@app.route("/download_csv")
def download_csv():
    df = load_history()
    return Response(df.to_csv(index=False, encoding="utf-8-sig"), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=Freight_Forwarders_History.csv"})

# =========================================================
# 8. Static File Generation
# =========================================================
def export_static_csv():
    df = load_history()
    if not df.empty:
        df_copy = df.copy()
        if "date" in df_copy.columns:
            df_copy["date"] = pd.to_datetime(df_copy["date"], errors="coerce").dt.strftime("%Y-%m-%d")
        df_copy.to_csv(STATIC_CSV_PATH, index=False, encoding="utf-8-sig")

def run_cron_job(url=DEFAULT_URL):
    log("[FORWARDER] Static dashboard build started.")
    get_connection().close()
    crawl_message = ""
    try:
        df = refresh_data(url=url)
        log(f"[FORWARDER] 최신 데이터 수집 성공: {len(df)} rows")
    except Exception as error:
        crawl_message = f"데이터 수집 실패: {error}"
        log(f"[FORWARDER] Crawl error: {error}")
        if load_history().empty: raise RuntimeError("기존 데이터도 없습니다.")
    export_static_csv()
    with app.app_context():
        html_content = render_dashboard_html(message=crawl_message, static_mode=True, source_url=url)
    STATIC_HTML_PATH.write_text(html_content, encoding="utf-8")
    log("[FORWARDER] Static dashboard build completed.")

# =========================================================
# 9. Runtime Arguments & Main
# =========================================================
def parse_args():
    parser = argparse.ArgumentParser(description="A&A Top 25 Dashboard")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--no-fetch-on-start", action="store_true")
    parser.add_argument("--crawl-only", action="store_true")
    return parser.parse_args()

def main():
    global RUNTIME_CONFIG
    args = parse_args()
    RUNTIME_CONFIG = args
    get_connection().close()

    if args.crawl_only or os.environ.get("GITHUB_ACTIONS") == "true":
        try:
            run_cron_job(url=args.url)
            sys.exit(0)
        except Exception as error:
            save_run_log("ERROR", error)
            log(f"[FORWARDER] Build failed: {error}")
            sys.exit(1)

    if not args.no_fetch_on_start:
        try:
            refresh_data(url=args.url)
        except Exception as error:
            log(f"[WARN] 시작 데이터 수집 실패: {error}")

    port = find_available_port(args.host, args.port)
    local_url = f"http://{args.host}:{port}"
    log(f"[INFO] Flask 시작: {local_url}")

    if not args.no_open:
        threading.Thread(target=wait_and_open_browser, args=(local_url, args.host, port), daemon=True).start()

    app.run(host=args.host, port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
