# -*- coding: utf-8 -*-
"""
Alphaliner TOP 100 Liner Operators Static Dashboard Crawler (GitHub Optimized)
- Direct ExtJS Store extraction (Top100description)
- Official Global Figures + TOP 100 Capacity Separation
- Historical snapshot comparison & Full Time-travel Delta analysis
- Authentic Alphaliner 2-tier Grouped Table & UTF-8 BOM CSV Export
- Bloomberg MI Terminal Look & Feel (Aligned with BunkerPrice)

Output files:
- alphaliner_history.db
- alphaliner_data.json
- index.html
"""

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from io import StringIO
from pathlib import Path
from threading import Lock

import pandas as pd
from flask import Flask, Response, send_from_directory
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

try:
    from webdriver_manager.chrome import ChromeDriverManager
    HAS_WEBDRIVER_MANAGER = True
except Exception:
    HAS_WEBDRIVER_MANAGER = False

# =========================================================
# 1. Basic Configuration & Logger Setup
# =========================================================
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "alphaliner_history.db"
JSON_PATH = BASE_DIR / "alphaliner_data.json"
HTML_PATH = BASE_DIR / "index.html"

TARGET_URL = "https://alphaliner.axsmarine.com/PublicTop100/"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("AlphalinerCrawler")

db_lock = Lock()

# =========================================================
# 2. Database Management
# =========================================================
def init_db(db_file: Path = DB_PATH):
    """Initialize SQLite database schema."""
    with db_lock:
        conn = sqlite3.connect(str(db_file))
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS snapshot_meta (
                snapshot_date TEXT PRIMARY KEY,
                extracted_at TEXT NOT NULL,
                global_teu INTEGER NOT NULL,
                global_ships INTEGER NOT NULL,
                top100_teu INTEGER NOT NULL,
                top100_ships INTEGER NOT NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS operator_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_date TEXT NOT NULL,
                rank INTEGER NOT NULL,
                operator_name TEXT NOT NULL,
                total_teu INTEGER NOT NULL,
                total_ships INTEGER NOT NULL,
                market_share REAL NOT NULL,
                owned_teu INTEGER NOT NULL,
                owned_ships INTEGER NOT NULL,
                owned_share REAL NOT NULL,
                chartered_teu INTEGER NOT NULL,
                chartered_ships INTEGER NOT NULL,
                chartered_share REAL NOT NULL,
                orderbook_teu INTEGER NOT NULL,
                orderbook_ships INTEGER NOT NULL,
                orderbook_share REAL NOT NULL,
                FOREIGN KEY (snapshot_date) REFERENCES snapshot_meta(snapshot_date),
                UNIQUE(snapshot_date, rank)
            )
        """)

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_op_date ON operator_snapshots(snapshot_date);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_op_name ON operator_snapshots(operator_name);")

        conn.commit()
        conn.close()

# =========================================================
# 3. Crawler Core Logic
# =========================================================
def get_chrome_driver():
    """Build Chrome WebDriver compatible with GitHub Actions & Local execution."""
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    if os.environ.get("CHROME_BIN"):
        chrome_options.binary_location = os.environ.get("CHROME_BIN")

    service = None
    if HAS_WEBDRIVER_MANAGER:
        try:
            service = Service(ChromeDriverManager().install())
        except Exception as e:
            logger.warning(f"ChromeDriverManager failed: {e}. Falling back to default system chromedriver.")

    if service:
        return webdriver.Chrome(service=service, options=chrome_options)
    return webdriver.Chrome(options=chrome_options)

def fetch_alphaliner_data():
    """Fetch raw data directly via ExtJS Store memory inspection."""
    logger.info(f"Connecting to Alphaliner Top 100: {TARGET_URL}")
    driver = None
    try:
        driver = get_chrome_driver()
        driver.set_page_load_timeout(60)
        driver.get(TARGET_URL)

        wait = WebDriverWait(driver, 30)
        wait.until(lambda d: d.execute_script("return typeof Ext !== 'undefined' && Ext.isReady;"))
        time.sleep(3)  # ExtJS Store 메모리 동기화 대기

        js_extractor = """
        var results = {
            rows: [],
            global_summary: {}
        };

        if (typeof Ext !== 'undefined' && Ext.data && Ext.data.StoreManager) {
            Ext.data.StoreManager.each(function(store) {
                if (store && store.getCount && store.getCount() > 0) {
                    var first = store.getAt(0);
                    if (first && (first.get('operator') || first.get('ranking') || first.get('teu') || first.get('tot_teu'))) {
                        store.each(function(rec) {
                            results.rows.push(rec.data);
                        });
                    }
                }
            });
        }

        var bodyText = document.body ? document.body.innerText : "";
        results.raw_text = bodyText;

        return results;
        """

        data = driver.execute_script(js_extractor)
        return data

    except Exception as e:
        logger.error(f"Error while scraping Alphaliner: {e}")
        raise
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

def parse_int(val):
    if val is None or val == "":
        return 0
    if isinstance(val, (int, float)):
        return int(val)
    clean = re.sub(r"[^\d]", "", str(val))
    return int(clean) if clean else 0

def parse_float(val):
    if val is None or val == "":
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    clean = re.sub(r"[^\d.]", "", str(val))
    try:
        return float(clean) if clean else 0.0
    except ValueError:
        return 0.0

def process_and_persist(raw_data, db_file: Path = DB_PATH):
    """Normalize extracted data, resolve fleet splits, and save to SQLite."""
    today_str = datetime.now().strftime("%Y-%m-%d")
    now_iso = datetime.now().isoformat()

    rows = raw_data.get("rows", [])
    raw_text = raw_data.get("raw_text", "")

    if not rows:
        raise ValueError("No operator data extracted from ExtJS stores.")

    extracted_global_teu = 0
    extracted_global_ships = 0

    m = re.search(r"TOTAL\s+FLEET\s*[:\s]*([\d,]+)\s*TEU\s*\(?([\d,]+)\s*Ships\)?", raw_text, re.IGNORECASE)
    if m:
        extracted_global_teu = parse_int(m.group(1))
        extracted_global_ships = parse_int(m.group(2))
    else:
        m2 = re.search(r"([\d,]+)\s*TEU\s*\(([\d,]+)\s*Ships\)", raw_text)
        if m2:
            extracted_global_teu = parse_int(m2.group(1))
            extracted_global_ships = parse_int(m2.group(2))

    operators = []
    seen_ranks = set()

    for row in rows:
        rank = parse_int(row.get("ranking") or row.get("rank") or row.get("rk") or row.get("id"))
        if rank <= 0 or rank in seen_ranks or rank > 100:
            continue
        seen_ranks.add(rank)

        name = str(row.get("operator") or row.get("operator_name") or row.get("liner") or f"Operator {rank}").strip()
        tot_teu = parse_int(row.get("teu") or row.get("tot_teu") or row.get("total_teu") or row.get("capacity"))
        tot_ships = parse_int(row.get("ships") or row.get("tot_ships") or row.get("total_ships") or row.get("vessels"))

        owned_teu = parse_int(
            row.get("owned_teu")
            or row.get("own_teu")
            or row.get("owned_capacity")
            or row.get("ownedTEU")
        )
        owned_ships = parse_int(
            row.get("owned_ships")
            or row.get("own_ships")
            or row.get("owned_vessels")
            or row.get("ownedShips")
        )

        explicit_cht_teu = parse_int(
            row.get("chartered_teu")
            or row.get("charter_teu")
            or row.get("cht_teu")
            or row.get("chartered_capacity")
            or row.get("charteredTEU")
        )
        explicit_cht_ships = parse_int(
            row.get("chartered_ships")
            or row.get("charter_ships")
            or row.get("cht_ships")
            or row.get("chartered_vessels")
            or row.get("charteredShips")
        )

        # Chartered 미제공 시 Total - Owned 자동 역산
        cht_teu = (
            explicit_cht_teu
            if explicit_cht_teu > 0
            else max(0, tot_teu - owned_teu)
        )
        cht_ships = (
            explicit_cht_ships
            if explicit_cht_ships > 0
            else max(0, tot_ships - owned_ships)
        )

        order_teu = parse_int(
            row.get("orderbook_teu")
            or row.get("ord_teu")
            or row.get("order_teu")
            or row.get("orderbook_capacity")
            or row.get("orderbookTEU")
        )
        order_ships = parse_int(
            row.get("orderbook_ships")
            or row.get("ord_ships")
            or row.get("order_ships")
            or row.get("orderbook_vessels")
            or row.get("orderbookShips")
        )

        owned_share = round((owned_teu / tot_teu * 100), 2) if tot_teu > 0 else 0.0
        cht_share = round((cht_teu / tot_teu * 100), 2) if tot_teu > 0 else 0.0
        order_share = round((order_teu / tot_teu * 100), 2) if tot_teu > 0 else 0.0

        operators.append({
            "rank": rank,
            "operator_name": name,
            "total_teu": tot_teu,
            "total_ships": tot_ships,
            "market_share": 0.0,
            "owned_teu": owned_teu,
            "owned_ships": owned_ships,
            "owned_share": owned_share,
            "chartered_teu": cht_teu,
            "chartered_ships": cht_ships,
            "chartered_share": cht_share,
            "orderbook_teu": order_teu,
            "orderbook_ships": order_ships,
            "orderbook_share": order_share
        })

    operators.sort(key=lambda x: x["rank"])

    top100_teu = sum(o["total_teu"] for o in operators)
    top100_ships = sum(o["total_ships"] for o in operators)

    global_teu = extracted_global_teu if extracted_global_teu > 0 else top100_teu
    global_ships = extracted_global_ships if extracted_global_ships > 0 else top100_ships

    for op in operators:
        op["market_share"] = round((op["total_teu"] / global_teu) * 100, 2) if global_teu > 0 else 0.0

    init_db(db_file)

    with db_lock:
        conn = sqlite3.connect(str(db_file))
        cursor = conn.cursor()

        cursor.execute("""
            INSERT OR REPLACE INTO snapshot_meta (snapshot_date, extracted_at, global_teu, global_ships, top100_teu, top100_ships)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (today_str, now_iso, global_teu, global_ships, top100_teu, top100_ships))

        cursor.execute("DELETE FROM operator_snapshots WHERE snapshot_date = ?", (today_str,))

        for op in operators:
            cursor.execute("""
                INSERT INTO operator_snapshots (
                    snapshot_date, rank, operator_name, total_teu, total_ships, market_share,
                    owned_teu, owned_ships, owned_share, chartered_teu, chartered_ships, chartered_share,
                    orderbook_teu, orderbook_ships, orderbook_share
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                today_str, op["rank"], op["operator_name"], op["total_teu"], op["total_ships"], op["market_share"],
                op["owned_teu"], op["owned_ships"], op["owned_share"], op["chartered_teu"], op["chartered_ships"], op["chartered_share"],
                op["orderbook_teu"], op["orderbook_ships"], op["orderbook_share"]
            ))

        conn.commit()
        conn.close()

    logger.info(f"Persisted snapshot {today_str} ({len(operators)} operators). Global TEU: {global_teu:,}, Top100 TEU: {top100_teu:,}")
    return today_str

# =========================================================
# 4. JSON Serialization Engine
# =========================================================
def export_dataset_json(db_file: Path = DB_PATH, out_json: Path = JSON_PATH):
    """Serialize entire SQLite time-series dataset into structured JSON."""
    init_db(db_file)

    with db_lock:
        conn = sqlite3.connect(str(db_file))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM snapshot_meta ORDER BY snapshot_date ASC")
        meta_rows = cursor.fetchall()

        dates = [row["snapshot_date"] for row in meta_rows]
        snapshots = {}

        for m in meta_rows:
            s_date = m["snapshot_date"]
            cursor.execute("SELECT * FROM operator_snapshots WHERE snapshot_date = ? ORDER BY rank ASC", (s_date,))
            op_rows = [dict(r) for r in cursor.fetchall()]

            snapshots[s_date] = {
                "meta": {
                    "extracted_at": m["extracted_at"],
                    "global_teu": m["global_teu"],
                    "global_ships": m["global_ships"],
                    "top100_teu": m["top100_teu"],
                    "top100_ships": m["top100_ships"]
                },
                "operators": op_rows
            }

        conn.close()

    payload = {
        "dates": dates,
        "snapshots": snapshots,
        "updated_at": datetime.now().isoformat()
    }

    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    logger.info(f"Exported JSON dataset: {out_json}")
    return payload

# =========================================================
# 5. Bloomberg Terminal Static HTML Generator
# =========================================================
def build_static_dashboard(json_data, out_html: Path = HTML_PATH):
    """Render high-density Bloomberg MI Terminal Dashboard."""
    raw_json_str = json.dumps(json_data, ensure_ascii=False)

    html_content = f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>ALPHALINER TOP 100 // MARKET INTELLIGENCE TERMINAL</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700;800&family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
  <style>
    :root {{
      --bg: #02060b;
      --bg-panel: #07111c;
      --bg-panel-sub: #0a1724;
      --bg-hover: #0f2236;
      --line: #1b3043;
      --line-bright: #29445c;
      --text: #f3f7fb;
      --text-muted: #879caf;
      --text-dim: #536c80;
      --orange: #f6a623;
      --amber: #ffb000;
      --blue: #45a9f5;
      --cyan: #00d2ff;
      --green: #36ca82;
      --red: #ff6262;
      --purple: #a78bfa;
      --font-mono: 'JetBrains Mono', monospace;
      --font-sans: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    }}

    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: radial-gradient(circle at 50% -20%, rgba(34, 106, 154, 0.15), transparent 45%), var(--bg);
      color: var(--text);
      font-family: var(--font-sans);
      font-size: 12px;
      line-height: 1.4;
      padding: 12px;
      -webkit-font-smoothing: antialiased;
    }}

    .mono {{ font-family: var(--font-mono); }}
    .num {{ font-family: var(--font-mono); text-align: right; }}

    .ticker-bar {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      background: var(--bg-panel);
      border: 1px solid var(--line);
      border-left: 3px solid var(--orange);
      padding: 8px 14px;
      margin-bottom: 10px;
    }}
    .brand {{
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 13px;
      font-weight: 800;
      letter-spacing: 0.05em;
    }}
    .tag-live {{
      background: rgba(54, 202, 130, 0.15);
      color: var(--green);
      border: 1px solid rgba(54, 202, 130, 0.4);
      padding: 1px 6px;
      font-size: 10px;
      font-weight: 700;
    }}
    .controls {{
      display: flex;
      align-items: center;
      gap: 12px;
    }}
    select, button {{
      background: var(--bg-panel-sub);
      color: var(--text);
      border: 1px solid var(--line-bright);
      padding: 4px 10px;
      font-size: 11px;
      font-family: var(--font-mono);
      outline: none;
      cursor: pointer;
    }}
    select:focus, button:hover {{
      border-color: var(--orange);
    }}
    .btn-export {{
      background: rgba(246, 166, 35, 0.15);
      color: var(--orange);
      border: 1px solid rgba(246, 166, 35, 0.4);
      font-weight: 700;
    }}
    .btn-export:hover {{
      background: var(--orange);
      color: #000;
    }}

    .kpi-row {{
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 10px;
      margin-bottom: 10px;
    }}
    .kpi-card {{
      background: var(--bg-panel);
      border: 1px solid var(--line);
      border-top: 2px solid var(--line-bright);
      padding: 10px 12px;
    }}
    .kpi-card.accent {{ border-top-color: var(--orange); }}
    .kpi-card.blue {{ border-top-color: var(--blue); }}
    .kpi-card.green {{ border-top-color: var(--green); }}
    .kpi-card.purple {{ border-top-color: var(--purple); }}

    .kpi-label {{
      font-size: 10px;
      font-weight: 700;
      color: var(--text-dim);
      text-transform: uppercase;
      letter-spacing: 0.05em;
      margin-bottom: 4px;
    }}
    .kpi-val {{
      font-size: 20px;
      font-weight: 800;
      font-family: var(--font-mono);
      color: var(--text);
    }}
    .kpi-sub {{
      display: flex;
      justify-content: space-between;
      font-size: 10px;
      color: var(--text-muted);
      margin-top: 4px;
      font-family: var(--font-mono);
    }}

    .up {{ color: var(--green); }}
    .down {{ color: var(--red); }}
    .flat {{ color: var(--text-dim); }}

    .charts-row {{
      display: grid;
      grid-template-columns: 2fr 1fr;
      gap: 10px;
      margin-bottom: 10px;
    }}
    .panel {{
      background: var(--bg-panel);
      border: 1px solid var(--line);
      padding: 12px;
      display: flex;
      flex-direction: column;
    }}
    .panel-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid var(--line);
      padding-bottom: 6px;
      margin-bottom: 10px;
      font-size: 11px;
      font-weight: 700;
      color: var(--text-muted);
      letter-spacing: 0.05em;
      text-transform: uppercase;
    }}
    .chart-box {{
      position: relative;
      height: 260px;
      width: 100%;
    }}

    .table-container {{
      max-height: 520px;
      overflow-y: auto;
      overflow-x: auto;
      border: 1px solid var(--line);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 11px;
      white-space: nowrap;
    }}
    th {{
      position: sticky;
      top: 0;
      background: var(--bg-panel-sub);
      color: var(--text-muted);
      font-size: 10px;
      font-weight: 700;
      text-transform: uppercase;
      padding: 6px 8px;
      border-bottom: 1px solid var(--line-bright);
      border-right: 1px solid rgba(27, 48, 67, 0.4);
      z-index: 10;
    }}
    th.group-th {{
      text-align: center;
      background: #0d1e30;
      border-bottom: 1px solid var(--line);
    }}
    td {{
      padding: 5px 8px;
      border-bottom: 1px solid rgba(27, 48, 67, 0.6);
      border-right: 1px solid rgba(27, 48, 67, 0.4);
    }}
    tr:hover td {{
      background: var(--bg-hover) !important;
    }}
    .op-name {{
      font-weight: 600;
      color: var(--text);
    }}
    .rank-cell {{
      font-weight: 800;
      color: var(--orange);
      text-align: center;
      background: rgba(246, 166, 35, 0.03);
    }}
    .share-bar-cell {{
      width: 80px;
      padding: 2px 6px;
    }}
    .progress-bar-bg {{
      background: rgba(255, 255, 255, 0.05);
      height: 6px;
      width: 100%;
      border-radius: 2px;
      overflow: hidden;
    }}
    .progress-bar-fill {{
      height: 100%;
      background: var(--orange);
    }}

    ::-webkit-scrollbar {{ width: 6px; height: 6px; }}
    ::-webkit-scrollbar-track {{ background: var(--bg); }}
    ::-webkit-scrollbar-thumb {{ background: var(--line-bright); }}
    ::-webkit-scrollbar-thumb:hover {{ background: var(--orange); }}

    .status-bar {{
      margin-top: 10px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 6px 12px;
      background: var(--bg-panel);
      border: 1px solid var(--line);
      font-size: 10px;
      color: var(--text-dim);
      font-family: var(--font-mono);
    }}
  </style>
</head>
<body>

  <!-- TOP TICKER BAR -->
  <div class="ticker-bar">
    <div class="brand">
      <span style="color:var(--orange)">ALPHALINER</span> TOP 100 LINER OPERATORS
      <span class="tag-live">INTELLIGENCE</span>
    </div>
    <div class="controls">
      <label class="mono" style="color:var(--text-muted); font-size:10px;">SNAPSHOT:</label>
      <select id="dateSelect" onchange="onDateChange()"></select>

      <label class="mono" style="color:var(--text-muted); font-size:10px; margin-left:8px;">VS DELTA:</label>
      <select id="compareSelect" onchange="renderAll()"></select>

      <button class="btn-export" onclick="exportCSV()">EXPORT CSV (BOM)</button>
    </div>
  </div>

  <!-- KPI SUMMARY ROW -->
  <div class="kpi-row">
    <div class="kpi-card accent">
      <div class="kpi-label">Global Active Fleet</div>
      <div class="kpi-val" id="kpi-global-teu">0 TEU</div>
      <div class="kpi-sub">
        <span id="kpi-global-ships">0 Ships</span>
        <span id="kpi-global-delta" class="flat">--</span>
      </div>
    </div>
    <div class="kpi-card blue">
      <div class="kpi-label">Top 100 Aggregate Fleet</div>
      <div class="kpi-val" id="kpi-top100-teu">0 TEU</div>
      <div class="kpi-sub">
        <span id="kpi-top100-ships">0 Ships</span>
        <span id="kpi-top100-share">0.0% of Global</span>
      </div>
    </div>
    <div class="kpi-card green">
      <div class="kpi-label">Top 10 Liner Capacity</div>
      <div class="kpi-val" id="kpi-top10-teu">0 TEU</div>
      <div class="kpi-sub">
        <span id="kpi-top10-share">0.0% Market Share</span>
      </div>
    </div>
    <div class="kpi-card purple">
      <div class="kpi-label">Total Orderbook Pipeline</div>
      <div class="kpi-val" id="kpi-orderbook-teu">0 TEU</div>
      <div class="kpi-sub">
        <span id="kpi-orderbook-ships">0 Ships</span>
        <span id="kpi-orderbook-ratio">0.0% of Existing</span>
      </div>
    </div>
  </div>

  <!-- CHARTS ROW -->
  <div class="charts-row">
    <div class="panel">
      <div class="panel-header">
        <span>Top 15 Carriers Capacity & Orderbook Breakdown</span>
        <span class="mono">TEU CAPACITY</span>
      </div>
      <div class="chart-box">
        <canvas id="topChart"></canvas>
      </div>
    </div>
    <div class="panel">
      <div class="panel-header">
        <span>Fleet Structure Ratio</span>
        <span class="mono">GLOBAL TOP 100</span>
      </div>
      <div class="chart-box">
        <canvas id="ratioChart"></canvas>
      </div>
    </div>
  </div>

  <!-- MAIN OPERATOR TABLE PANEL -->
  <div class="panel">
    <div class="panel-header">
      <span>Official Alphaliner Top 100 Ranking & Detailed Breakdown</span>
      <span class="mono" id="table-record-count">100 OPERATORS</span>
    </div>
    <div class="table-container">
      <table>
        <thead>
          <tr>
            <th rowspan="2">Rank</th>
            <th rowspan="2" style="text-align:left;">Operator Name</th>
            <th colspan="3" class="group-th">Existing Fleet Total</th>
            <th colspan="2" class="group-th">Owned Fleet</th>
            <th colspan="2" class="group-th">Chartered Fleet</th>
            <th colspan="2" class="group-th">Orderbook</th>
            <th rowspan="2">Global Share</th>
          </tr>
          <tr>
            <th class="num">TEU</th>
            <th class="num">Ships</th>
            <th class="num">Share %</th>
            <th class="num">TEU</th>
            <th class="num">% Total</th>
            <th class="num">TEU</th>
            <th class="num">% Total</th>
            <th class="num">TEU</th>
            <th class="num">% Fleet</th>
          </tr>
        </thead>
        <tbody id="operatorTableBody"></tbody>
      </table>
    </div>
  </div>

  <!-- STATUS FOOTER BAR -->
  <div class="status-bar">
    <div>SYSTEM: <span style="color:var(--green)">ONLINE</span> | PROVIDER: AXS MARINE / ALPHALINER</div>
    <div id="footer-sync-time">LAST SYNC: --</div>
  </div>

  <script>
    const DATASET = {raw_json_str};
    let topChartInstance = null;
    let ratioChartInstance = null;

    function formatNumber(val) {{
      if (val === null || val === undefined) return "0";
      return Number(val).toLocaleString();
    }}

    function initSelectors() {{
      const dSel = document.getElementById("dateSelect");
      const cSel = document.getElementById("compareSelect");
      dSel.innerHTML = "";
      cSel.innerHTML = "<option value='none'>None (Current Only)</option>";

      const dates = DATASET.dates || [];
      if (dates.length === 0) return;

      dates.slice().reverse().forEach((d, idx) => {{
        const opt = document.createElement("option");
        opt.value = d;
        opt.textContent = d + (idx === 0 ? " (Latest)" : "");
        dSel.appendChild(opt);

        const optC = document.createElement("option");
        optC.value = d;
        optC.textContent = d;
        cSel.appendChild(optC);
      }});

      if (dates.length > 1) {{
        cSel.selectedIndex = 1;
      }}
    }}

    function onDateChange() {{
      renderAll();
    }}

    function renderAll() {{
      const curDate = document.getElementById("dateSelect").value;
      const cmpDate = document.getElementById("compareSelect").value;

      if (!curDate || !DATASET.snapshots[curDate]) return;

      const current = DATASET.snapshots[curDate];
      const compare = (cmpDate !== 'none' && DATASET.snapshots[cmpDate]) ? DATASET.snapshots[cmpDate] : null;

      renderKPIs(current, compare);
      renderCharts(current);
      renderTable(current, compare);

      document.getElementById("footer-sync-time").textContent = "LAST SYNC: " + (current.meta.extracted_at || curDate);
    }}

    function renderKPIs(current, compare) {{
      const meta = current.meta;
      const ops = current.operators || [];

      document.getElementById("kpi-global-teu").textContent = formatNumber(meta.global_teu) + " TEU";
      document.getElementById("kpi-global-ships").textContent = formatNumber(meta.global_ships) + " Ships";

      document.getElementById("kpi-top100-teu").textContent = formatNumber(meta.top100_teu) + " TEU";
      document.getElementById("kpi-top100-ships").textContent = formatNumber(meta.top100_ships) + " Ships";

      const top100Share = meta.global_teu > 0 ? ((meta.top100_teu / meta.global_teu) * 100).toFixed(1) : "0.0";
      document.getElementById("kpi-top100-share").textContent = top100Share + "% of Global";

      const top10Ops = ops.slice(0, 10);
      const top10Teu = top10Ops.reduce((acc, x) => acc + x.total_teu, 0);
      const top10Share = meta.global_teu > 0 ? ((top10Teu / meta.global_teu) * 100).toFixed(1) : "0.0";

      document.getElementById("kpi-top10-teu").textContent = formatNumber(top10Teu) + " TEU";
      document.getElementById("kpi-top10-share").textContent = top10Share + "% Market Share";

      const obTeu = ops.reduce((acc, x) => acc + x.orderbook_teu, 0);
      const obShips = ops.reduce((acc, x) => acc + x.orderbook_ships, 0);
      const obRatio = meta.top100_teu > 0 ? ((obTeu / meta.top100_teu) * 100).toFixed(1) : "0.0";

      document.getElementById("kpi-orderbook-teu").textContent = formatNumber(obTeu) + " TEU";
      document.getElementById("kpi-orderbook-ships").textContent = formatNumber(obShips) + " Ships";
      document.getElementById("kpi-orderbook-ratio").textContent = obRatio + "% of Existing";

      const deltaEl = document.getElementById("kpi-global-delta");
      if (compare && compare.meta) {{
        const diff = meta.global_teu - compare.meta.global_teu;
        const sign = diff > 0 ? "+" : "";
        deltaEl.className = diff > 0 ? "up" : (diff < 0 ? "down" : "flat");
        deltaEl.textContent = sign + formatNumber(diff) + " TEU vs " + (compare.meta.snapshot_date || "BASE");
      }} else {{
        deltaEl.className = "flat";
        deltaEl.textContent = "HISTORICAL BASE";
      }}
    }}

    function renderCharts(current) {{
      const ops = (current.operators || []).slice(0, 15);

      const labels = ops.map(o => o.operator_name.length > 14 ? o.operator_name.substring(0, 13) + '..' : o.operator_name);
      const ownedTeu = ops.map(o => o.owned_teu);
      const chtTeu = ops.map(o => o.chartered_teu);
      const ordTeu = ops.map(o => o.orderbook_teu);

      if (topChartInstance) topChartInstance.destroy();
      const ctx1 = document.getElementById("topChart").getContext("2d");
      topChartInstance = new Chart(ctx1, {{
        type: 'bar',
        data: {{
          labels: labels,
          datasets: [
            {{
              label: 'Owned TEU',
              data: ownedTeu,
              backgroundColor: '#45a9f5',
              stack: 'Stack 0'
            }},
            {{
              label: 'Chartered TEU',
              data: chtTeu,
              backgroundColor: '#f6a623',
              stack: 'Stack 0'
            }},
            {{
              label: 'Orderbook TEU',
              data: ordTeu,
              backgroundColor: 'rgba(167, 139, 250, 0.45)',
              borderColor: '#a78bfa',
              borderWidth: 1,
              stack: 'Stack 1'
            }}
          ]
        }},
        options: {{
          responsive: true,
          maintainAspectRatio: false,
          plugins: {{
            legend: {{
              position: 'top',
              labels: {{ color: '#879caf', font: {{ size: 10, family: 'JetBrains Mono' }} }}
            }},
            tooltip: {{
              mode: 'index',
              intersect: false
            }}
          }},
          scales: {{
            x: {{
              ticks: {{ color: '#879caf', font: {{ size: 10, family: 'Inter' }} }},
              grid: {{ color: 'rgba(27, 48, 67, 0.4)' }}
            }},
            y: {{
              ticks: {{
                color: '#879caf',
                font: {{ size: 10, family: 'JetBrains Mono' }},
                callback: function(v) {{ return (v / 1000000).toFixed(1) + 'M'; }}
              }},
              grid: {{ color: 'rgba(27, 48, 67, 0.4)' }}
            }}
          }}
        }}
      }});

      const allOps = current.operators || [];
      const totalOwned = allOps.reduce((acc, x) => acc + x.owned_teu, 0);
      const totalCht = allOps.reduce((acc, x) => acc + x.chartered_teu, 0);
      const totalOrder = allOps.reduce((acc, x) => acc + x.orderbook_teu, 0);

      if (ratioChartInstance) ratioChartInstance.destroy();
      const ctx2 = document.getElementById("ratioChart").getContext("2d");
      ratioChartInstance = new Chart(ctx2, {{
        type: 'doughnut',
        data: {{
          labels: ['Owned Capacity', 'Chartered Capacity', 'Orderbook Pipeline'],
          datasets: [{{
            data: [totalOwned, totalCht, totalOrder],
            backgroundColor: ['#45a9f5', '#f6a623', '#a78bfa'],
            borderWidth: 1,
            borderColor: '#02060b'
          }}]
        }},
        options: {{
          responsive: true,
          maintainAspectRatio: false,
          plugins: {{
            legend: {{
              position: 'right',
              labels: {{ color: '#879caf', font: {{ size: 11, family: 'Inter' }} }}
            }}
          }},
          cutout: '68%'
        }}
      }});
    }}

    function renderTable(current, compare) {{
      const tbody = document.getElementById("operatorTableBody");
      tbody.innerHTML = "";

      const ops = current.operators || [];
      document.getElementById("table-record-count").textContent = ops.length + " OPERATORS MONITORED";

      ops.forEach(op => {{
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td class="rank-cell mono">${{op.rank}}</td>
          <td class="op-name">${{op.operator_name}}</td>
          <td class="num" style="font-weight:700; color:var(--text);">${{formatNumber(op.total_teu)}}</td>
          <td class="num">${{formatNumber(op.total_ships)}}</td>
          <td class="num" style="color:var(--orange); font-weight:700;">${{op.market_share.toFixed(1)}}%</td>
          <td class="num">${{formatNumber(op.owned_teu)}}</td>
          <td class="num" style="color:var(--blue);">${{op.owned_share.toFixed(1)}}%</td>
          <td class="num">${{formatNumber(op.chartered_teu)}}</td>
          <td class="num" style="color:var(--amber);">${{op.chartered_share.toFixed(1)}}%</td>
          <td class="num">${{formatNumber(op.orderbook_teu)}}</td>
          <td class="num" style="color:var(--purple);">${{op.orderbook_share.toFixed(1)}}%</td>
          <td class="share-bar-cell">
            <div class="progress-bar-bg">
              <div class="progress-bar-fill" style="width:${{Math.min(100, op.market_share * 5)}}%;"></div>
            </div>
          </td>
        `;
        tbody.appendChild(tr);
      }});
    }}

    function exportCSV() {{
      const curDate = document.getElementById("dateSelect").value;
      if (!curDate || !DATASET.snapshots[curDate]) return;

      const ops = DATASET.snapshots[curDate].operators;
      const headers = [
        "Ranking", "Operator", "Total TEU", "Total Ships", "Market Share (%)",
        "Owned TEU", "Owned Ships", "Owned Share (%)",
        "Chartered TEU", "Chartered Ships", "Chartered Share (%)",
        "Orderbook TEU", "Orderbook Ships", "Orderbook % Existing"
      ];

      let csv = "\\uFEFF" + headers.join(",") + "\\n";

      ops.forEach(o => {{
        const row = [
          o.rank,
          `"${{o.operator_name.replace(/"/g, '""')}}"`,
          o.total_teu,
          o.total_ships,
          o.market_share,
          o.owned_teu,
          o.owned_ships,
          o.owned_share,
          o.chartered_teu,
          o.chartered_ships,
          o.chartered_share,
          o.orderbook_teu,
          o.orderbook_ships,
          o.orderbook_share
        ];
        csv += row.join(",") + "\\n";
      }});

      const blob = new Blob([csv], {{ type: 'text/csv;charset=utf-8;' }});
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.setAttribute("download", `alphaliner_top100_${{curDate}}.csv`);
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
    }}

    window.onload = function() {{
      initSelectors();
      renderAll();
    }};
  </script>
</body>
</html>
"""

    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html_content)

    logger.info(f"Generated static terminal dashboard: {out_html}")

# =========================================================
# 6. Web Server & Execution Engine
# =========================================================
app = Flask(__name__, static_folder=str(BASE_DIR))

@app.route("/")
def serve_dashboard():
    return send_from_directory(str(BASE_DIR), "index.html")

@app.route("/alphaliner_data.json")
def serve_json():
    return send_from_directory(str(BASE_DIR), "alphaliner_data.json")

def main():
    parser = argparse.ArgumentParser(description="Alphaliner Top 100 Static Dashboard Engine")
    parser.add_argument("--cron", action="store_true", help="Run in headless crawler/export batch mode and exit")
    parser.add_argument("--host", default="0.0.0.0", help="Flask bind address")
    parser.add_argument("--port", type=int, default=8050, help="Flask bind port")
    args = parser.parse_args()

    init_db(DB_PATH)

    logger.info("Executing automated crawler ingestion cycle...")
    try:
        raw_data = fetch_alphaliner_data()
        process_and_persist(raw_data, DB_PATH)
    except Exception as e:
        logger.error(f"Live scraping cycle encountered an exception: {e}")
        if not DB_PATH.exists():
            logger.critical("Fatal: No historical database exists to render dashboard.")
            sys.exit(1)

    json_payload = export_dataset_json(DB_PATH, JSON_PATH)
    build_static_dashboard(json_payload, HTML_PATH)

    if args.cron:
        logger.info("Batch execution finished successfully. Exiting.")
        sys.exit(0)

    logger.info(f"Starting Bloomberg Terminal Interactive Server on http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)

if __name__ == "__main__":
    main()
