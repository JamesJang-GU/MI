# -*- coding: utf-8 -*-
"""
Alphaliner TOP 100 Liner Operators Static Dashboard Crawler (GitHub Optimized)
- Direct ExtJS Store extraction (Top100description)
- Official Global Figures + TOP 100 Capacity Separation
- Historical snapshot comparison & Full Time-travel Delta analysis
- Authentic Alphaliner 2-tier Grouped Table & UTF-8 BOM CSV Export
- Bloomberg MI Terminal Look & Feel
- Auto DB schema migration & robust column mapping

Output files:
- alphaliner_history.db
- alphaliner_data.json
- index.html
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from threading import Lock

import pandas as pd
from flask import Flask, Response, send_from_directory
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait

try:
    from webdriver_manager.chrome import ChromeDriverManager
    HAS_WEBDRIVER_MANAGER = True
except Exception:
    HAS_WEBDRIVER_MANAGER = False

# =========================================================
# 1. Basic Configuration
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
logger = logging.getLogger("Alphaliner")

app = Flask(__name__, static_folder=str(BASE_DIR))
db_lock = Lock()

# =========================================================
# 2. Database Initialization & Dynamic Schema Adaptation
# =========================================================
def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_table_columns(conn, table_name: str) -> set:
    try:
        cur = conn.cursor()
        cur.execute(f"PRAGMA table_info({table_name})")
        return {row["name"] for row in cur.fetchall()}
    except Exception:
        return set()

def init_db():
    with db_lock:
        with get_db_connection() as conn:
            cur = conn.cursor()

            # 1. snapshot_meta table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS snapshot_meta (
                    snapshot_date TEXT PRIMARY KEY,
                    global_teu INTEGER,
                    global_ships INTEGER,
                    top100_teu INTEGER,
                    top100_ships INTEGER,
                    top10_share REAL,
                    orderbook_teu INTEGER,
                    extracted_at TEXT,
                    source_url TEXT,
                    raw_hash TEXT,
                    notes TEXT,
                    updated_at TEXT
                )
            """)

            # 2. operator_snapshots table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS operator_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_date TEXT,
                    rank INTEGER,
                    operator_name TEXT,
                    total_teu INTEGER,
                    total_ships INTEGER,
                    market_share REAL,
                    owned_teu INTEGER,
                    owned_ships INTEGER,
                    owned_share REAL,
                    chartered_teu INTEGER,
                    chartered_ships INTEGER,
                    chartered_share REAL,
                    orderbook_teu INTEGER,
                    orderbook_ships INTEGER,
                    orderbook_share REAL,
                    country TEXT,
                    created_at TEXT,
                    UNIQUE(snapshot_date, rank)
                )
            """)

            # Schema Migration if existing DB has different column names
            op_cols = get_table_columns(conn, "operator_snapshots")
            if "operator_name" not in op_cols and "operator" in op_cols:
                try:
                    cur.execute("ALTER TABLE operator_snapshots ADD COLUMN operator_name TEXT")
                    cur.execute("UPDATE operator_snapshots SET operator_name = operator WHERE operator_name IS NULL")
                except Exception as e:
                    logger.warning(f"Schema migration warning (operator_name): {e}")

            if "operator" not in op_cols and "operator_name" in op_cols:
                try:
                    cur.execute("ALTER TABLE operator_snapshots ADD COLUMN operator TEXT")
                    cur.execute("UPDATE operator_snapshots SET operator = operator_name WHERE operator IS NULL")
                except Exception as e:
                    logger.warning(f"Schema migration warning (operator): {e}")

            conn.commit()

# =========================================================
# 3. Selenium Web Crawler
# =========================================================
def build_chrome_options():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

    chrome_bin = os.environ.get("CHROME_BINARY") or os.environ.get("GOOGLE_CHROME_BIN")
    if chrome_bin and os.path.exists(chrome_bin):
        options.binary_location = chrome_bin
    return options

def get_driver():
    options = build_chrome_options()
    chromedriver_bin = os.environ.get("CHROMEDRIVER_PATH")

    if chromedriver_bin and os.path.exists(chromedriver_bin):
        service = Service(executable_path=chromedriver_bin)
        return webdriver.Chrome(service=service, options=options)

    if HAS_WEBDRIVER_MANAGER:
        try:
            return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
        except Exception:
            pass

    return webdriver.Chrome(options=options)

def fetch_alphaliner_data():
    logger.info(f"Navigating to {TARGET_URL}")
    driver = None
    try:
        driver = get_driver()
        driver.set_page_load_timeout(60)
        driver.get(TARGET_URL)

        # Wait for ExtJS Ext.data.StoreManager to load Top100description
        WebDriverWait(driver, 40).until(
            lambda d: d.execute_script("""
                return typeof Ext !== 'undefined' &&
                       Ext.data &&
                       Ext.data.StoreManager &&
                       Ext.data.StoreManager.lookup('Top100description') &&
                       Ext.data.StoreManager.lookup('Top100description').getCount() > 0;
            """)
        )

        time.sleep(2)

        records = driver.execute_script("""
            var store = Ext.data.StoreManager.lookup('Top100description');
            if (!store) return [];
            var data = [];
            store.each(function(rec) {
                data.push(rec.data);
            });
            return data;
        """)

        logger.info(f"Successfully extracted {len(records)} records from Store 'Top100description'.")
        if records:
            keys = list(records[0].keys())
            logger.info(f"Detected source fields: {', '.join(keys)}")
            logger.info(f"First raw record: {json.dumps(records[0])}")
        return records

    except Exception as e:
        logger.error(f"Error fetching data from Alphaliner: {e}")
        raise
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

# =========================================================
# 4. Data Parsing & Quality Validation
# =========================================================
def clean_int(val, default=0):
    if val is None:
        return default
    try:
        if isinstance(val, (int, float)):
            return int(val)
        s = str(val).replace(",", "").strip()
        if not s or s == "-":
            return default
        return int(float(s))
    except Exception:
        return default

def clean_float(val, default=0.0):
    if val is None:
        return default
    try:
        if isinstance(val, (int, float)):
            return float(val)
        s = str(val).replace(",", "").replace("%", "").strip()
        if not s or s == "-":
            return default
        return float(s)
    except Exception:
        return default

def parse_records(raw_records):
    parsed = []
    for rank_idx, r in enumerate(raw_records, start=1):
        operator = r.get("operator") or r.get("comp_name") or r.get("comp_cle_oper") or f"Operator {rank_idx}"

        owned_teu = clean_int(r.get("ownedTeu") or r.get("owned_teu"))
        owned_ships = clean_int(r.get("ownedShips") or r.get("owned_ships"))

        chartered_teu = clean_int(r.get("chartTeu") or r.get("chartered_teu"))
        chartered_ships = clean_int(r.get("chartShips") or r.get("chartered_ships"))

        total_teu = clean_int(r.get("totalTeu") or r.get("total_teu"))
        if total_teu == 0 and (owned_teu > 0 or chartered_teu > 0):
            total_teu = owned_teu + chartered_teu

        total_ships = clean_int(r.get("totalShips") or r.get("total_ships"))
        if total_ships == 0 and (owned_ships > 0 or chartered_ships > 0):
            total_ships = owned_ships + chartered_ships

        market_share = clean_float(r.get("percent") or r.get("market_share"))
        owned_share = clean_float(r.get("ownedProcents") or r.get("owned_share"))
        if owned_share == 0.0 and total_teu > 0:
            owned_share = round((owned_teu / total_teu) * 100, 2)

        chartered_share = clean_float(r.get("chartProcent") or r.get("chartered_share"))
        if chartered_share == 0.0 and total_teu > 0:
            chartered_share = round((chartered_teu / total_teu) * 100, 2)

        order_teu = clean_int(r.get("orderTeu") or r.get("orderbook_teu"))
        order_ships = clean_int(r.get("orderShips") or r.get("orderbook_ships"))
        order_share = clean_float(r.get("orderProcents") or r.get("orderbook_share"))

        country = r.get("carrier_flag", "")
        if country and "/" in country:
            country = country.split("/")[-1].replace(".png", "").upper()

        parsed.append({
            "rank": rank_idx,
            "operator_name": operator,
            "operator": operator,
            "total_teu": total_teu,
            "total_ships": total_ships,
            "market_share": market_share,
            "owned_teu": owned_teu,
            "owned_ships": owned_ships,
            "owned_share": owned_share,
            "chartered_teu": chartered_teu,
            "chartered_ships": chartered_ships,
            "chartered_share": chartered_share,
            "orderbook_teu": order_teu,
            "orderbook_ships": order_ships,
            "orderbook_share": order_share,
            "country": country
        })
    return parsed

def validate_candidate_metrics(parsed_ops):
    """
    엄격한 품질 검증:
    - 선사 수 90개 이상
    - 총 TEU 2,000만 이상
    - 총 척수 3,000척 이상
    """
    if len(parsed_ops) < 90:
        return False, f"Operator count too low: {len(parsed_ops)}"

    total_teu = sum(o["total_teu"] for o in parsed_ops)
    total_ships = sum(o["total_ships"] for o in parsed_ops)

    if total_teu < 20_000_000:
        return False, f"Total TEU too low: {total_teu:,}"
    if total_ships < 3_000:
        return False, f"Total Ships too low: {total_ships:,}"

    return True, "Valid snapshot"

# =========================================================
# 5. Persistence & Schema Safe Insertion
# =========================================================
def persist_snapshot(snapshot_date, parsed_ops, raw_records):
    raw_json_str = json.dumps(raw_records, sort_keys=True)
    raw_hash = hashlib.sha256(raw_json_str.encode("utf-8")).hexdigest()
    now_str = datetime.now().isoformat()

    top100_teu = sum(o["total_teu"] for o in parsed_ops)
    top100_ships = sum(o["total_ships"] for o in parsed_ops)
    top10_teu = sum(o["total_teu"] for o in parsed_ops[:10])
    top10_share = round((top10_teu / top100_teu * 100), 2) if top100_teu > 0 else 0.0
    orderbook_teu = sum(o["orderbook_teu"] for o in parsed_ops)

    with db_lock:
        with get_db_connection() as conn:
            cur = conn.cursor()

            # Upsert meta
            cur.execute("""
                INSERT INTO snapshot_meta (
                    snapshot_date, global_teu, global_ships,
                    top100_teu, top100_ships, top10_share, orderbook_teu,
                    extracted_at, source_url, raw_hash, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(snapshot_date) DO UPDATE SET
                    global_teu=excluded.global_teu,
                    global_ships=excluded.global_ships,
                    top100_teu=excluded.top100_teu,
                    top100_ships=excluded.top100_ships,
                    top10_share=excluded.top10_share,
                    orderbook_teu=excluded.orderbook_teu,
                    extracted_at=excluded.extracted_at,
                    source_url=excluded.source_url,
                    raw_hash=excluded.raw_hash,
                    updated_at=excluded.updated_at
            """, (
                snapshot_date, top100_teu, top100_ships,
                top100_teu, top100_ships, top10_share, orderbook_teu,
                now_str, TARGET_URL, raw_hash, now_str
            ))

            # Delete existing records for this date
            cur.execute("DELETE FROM operator_snapshots WHERE snapshot_date = ?", (snapshot_date,))

            # Detect available columns in operator_snapshots
            op_cols = get_table_columns(conn, "operator_snapshots")

            # Build insert query dynamically based on existing columns
            cols_to_insert = [
                "snapshot_date", "rank", "total_teu", "total_ships", "market_share",
                "owned_teu", "owned_ships", "owned_share",
                "chartered_teu", "chartered_ships", "chartered_share",
                "orderbook_teu", "orderbook_ships", "orderbook_share",
                "country", "created_at"
            ]

            if "operator_name" in op_cols:
                cols_to_insert.append("operator_name")
            if "operator" in op_cols:
                cols_to_insert.append("operator")

            placeholders = ", ".join(["?"] * len(cols_to_insert))
            col_names = ", ".join(cols_to_insert)
            insert_sql = f"INSERT INTO operator_snapshots ({col_names}) VALUES ({placeholders})"

            rows_data = []
            for op in parsed_ops:
                row = []
                for c in cols_to_insert:
                    if c == "snapshot_date":
                        row.append(snapshot_date)
                    elif c == "created_at":
                        row.append(now_str)
                    elif c in ("operator_name", "operator"):
                        row.append(op["operator_name"])
                    else:
                        row.append(op.get(c))
                rows_data.append(tuple(row))

            cur.executemany(insert_sql, rows_data)
            conn.commit()
            logger.info(f"Persisted VALID snapshot {snapshot_date} with {len(parsed_ops)} operators to DB.")

def process_and_persist():
    raw_records = fetch_alphaliner_data()
    parsed_ops = parse_records(raw_records)

    top100_teu = sum(o["total_teu"] for o in parsed_ops)
    top100_ships = sum(o["total_ships"] for o in parsed_ops)
    top10_teu = sum(o["total_teu"] for o in parsed_ops[:10])
    top10_share = round((top10_teu / top100_teu * 100), 2) if top100_teu > 0 else 0.0
    orderbook_teu = sum(o["orderbook_teu"] for o in parsed_ops)

    logger.info(
        f"Candidate snapshot metrics | operators={len(parsed_ops)} | "
        f"total_teu={top100_teu:,} | total_ships={top100_ships:,} | "
        f"top10_share={top10_share}% | orderbook_teu={orderbook_teu:,}"
    )

    is_valid, reason = validate_candidate_metrics(parsed_ops)
    if not is_valid:
        logger.error(f"Scrape validation rejected: {reason}. No corrupt data written to DB.")
        return False

    today_str = datetime.now().strftime("%Y-%m-%d")
    persist_snapshot(today_str, parsed_ops, raw_records)
    return True

# =========================================================
# 6. JSON Export & Dashboard Contract Generation
# =========================================================
def export_json_and_html():
    with get_db_connection() as conn:
        cur = conn.cursor()

        # Meta dates
        meta_rows = cur.execute("""
            SELECT * FROM snapshot_meta
            WHERE top100_teu > 20000000 AND top100_ships > 3000
            ORDER BY snapshot_date DESC
        """).fetchall()

        if not meta_rows:
            logger.warning("No valid snapshots found in DB to export.")
            return

        dates = [r["snapshot_date"] for r in meta_rows]
        snapshots = {}

        op_cols = get_table_columns(conn, "operator_snapshots")
        name_field = "operator_name" if "operator_name" in op_cols else "operator"

        for m in meta_rows:
            d = m["snapshot_date"]
            cur.execute(f"""
                SELECT rank, {name_field} AS operator_name, total_teu, total_ships, market_share,
                       owned_teu, owned_ships, owned_share,
                       chartered_teu, chartered_ships, chartered_share,
                       orderbook_teu, orderbook_ships, orderbook_share,
                       country
                FROM operator_snapshots
                WHERE snapshot_date = ?
                ORDER BY rank ASC
            """, (d,))
            ops = [dict(row) for row in cur.fetchall()]

            snapshots[d] = {
                "meta": {
                    "snapshot_date": d,
                    "global_teu": m["global_teu"] or m["top100_teu"],
                    "global_ships": m["global_ships"] or m["top100_ships"],
                    "top100_teu": m["top100_teu"],
                    "top100_ships": m["top100_ships"],
                    "top10_share": m["top10_share"],
                    "orderbook_teu": m["orderbook_teu"],
                    "extracted_at": m["extracted_at"]
                },
                "operators": ops
            }

    data = {
        "generated_at": datetime.now().isoformat(),
        "dates": dates,
        "latest_date": dates[0] if dates else None,
        "snapshots": snapshots
    }

    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    logger.info(f"Exported valid JSON contract: {JSON_PATH}")

    generate_static_html()

def generate_static_html():
    """Generates the static Bloomberg-style HTML viewer matching the Shipping Index visual language."""
    html_content = """<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Alphaliner TOP 100 Liner Operators</title>
  <style>
    :root {
      --bg: #0d1117;
      --card-bg: #161b22;
      --border: #30363d;
      --text-main: #c9d1d9;
      --text-muted: #8b949e;
      --primary: #58a6ff;
      --accent: #f0883e;
      --success: #3fb950;
      --danger: #f85149;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background-color: var(--bg);
      color: var(--text-main);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace, sans-serif;
      padding: 16px;
      font-size: 13px;
    }
    .header-bar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 10px 16px;
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 6px;
      margin-bottom: 16px;
    }
    .title { font-size: 16px; font-weight: 700; color: #fff; letter-spacing: 0.5px; }
    .kpi-container {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 12px;
      margin-bottom: 16px;
    }
    .kpi-card {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 14px;
    }
    .kpi-title { font-size: 11px; text-transform: uppercase; color: var(--text-muted); margin-bottom: 6px; }
    .kpi-value { font-size: 20px; font-weight: 700; color: #fff; font-family: monospace; }
    .kpi-delta { font-size: 11px; margin-top: 4px; font-weight: 600; }
    .up { color: var(--success); }
    .down { color: var(--danger); }
    .flat { color: var(--text-muted); }
    .controls {
      display: flex;
      gap: 10px;
      align-items: center;
      margin-bottom: 12px;
    }
    select, input, button {
      background: var(--card-bg);
      border: 1px solid var(--border);
      color: var(--text-main);
      padding: 6px 12px;
      border-radius: 4px;
      font-size: 12px;
    }
    button { cursor: pointer; background: #21262d; }
    button:hover { background: #30363d; }
    .table-container {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 6px;
      overflow-x: auto;
      max-height: 70vh;
    }
    table { width: 100%; border-collapse: collapse; text-align: right; }
    th, td { padding: 8px 12px; border-bottom: 1px solid var(--border); font-family: monospace; font-size: 12px; }
    th { background: #21262d; color: var(--text-muted); font-size: 11px; text-transform: uppercase; position: sticky; top: 0; z-index: 10; }
    tr:hover { background: #1c2128; }
    .text-left { text-align: left; }
    .text-center { text-align: center; }
    .op-name { font-weight: 600; color: #fff; }
  </style>
</head>
<body>
  <div class="header-bar">
    <div class="title">ALPHALINER TOP 100 LINER OPERATORS</div>
    <div id="last-updated" style="color: var(--text-muted); font-size: 11px;"></div>
  </div>

  <div class="kpi-container">
    <div class="kpi-card">
      <div class="kpi-title">1. Total Ships</div>
      <div class="kpi-value" id="kpi-ships">-</div>
      <div class="kpi-delta" id="delta-ships"></div>
    </div>
    <div class="kpi-card">
      <div class="kpi-title">2. Total Fleet TEU</div>
      <div class="kpi-value" id="kpi-teu">-</div>
      <div class="kpi-delta" id="delta-teu"></div>
    </div>
    <div class="kpi-card">
      <div class="kpi-title">3. Top 10 Concentration</div>
      <div class="kpi-value" id="kpi-top10">-</div>
      <div class="kpi-delta" id="delta-top10"></div>
    </div>
    <div class="kpi-card">
      <div class="kpi-title">4. Orderbook TEU</div>
      <div class="kpi-value" id="kpi-orderbook">-</div>
      <div class="kpi-delta" id="delta-orderbook"></div>
    </div>
  </div>

  <div class="controls">
    <label style="color: var(--text-muted); font-size: 12px;">Snapshot:</label>
    <select id="date-select"></select>
    <label style="color: var(--text-muted); font-size: 12px; margin-left: 10px;">Compare with:</label>
    <select id="compare-select"></select>
    <input type="text" id="search-box" placeholder="Filter operator..." style="margin-left: auto; width: 200px;">
    <button id="btn-export">Export CSV (BOM)</button>
  </div>

  <div class="table-container">
    <table id="operator-table">
      <thead>
        <tr>
          <th class="text-center">Rank</th>
          <th class="text-left">Operator</th>
          <th>Total TEU</th>
          <th>Total Ships</th>
          <th>Share (%)</th>
          <th>Owned TEU</th>
          <th>Owned Ships</th>
          <th>Chart TEU</th>
          <th>Chart Ships</th>
          <th>Orderbook TEU</th>
          <th>Orderbook Ships</th>
        </tr>
      </thead>
      <tbody></tbody>
    </table>
  </div>

  <script>
    let appData = null;

    async function init() {
      try {
        const res = await fetch('alphaliner_data.json?v=' + Date.now());
        appData = await res.json();

        const dateSelect = document.getElementById('date-select');
        const compareSelect = document.getElementById('compare-select');

        dateSelect.innerHTML = '';
        compareSelect.innerHTML = '<option value="">None</option>';

        appData.dates.forEach((d, idx) => {
          dateSelect.innerHTML += `<option value="${d}">${d}</option>`;
          compareSelect.innerHTML += `<option value="${d}" ${idx===1?'selected':''}>${d}</option>`;
        });

        dateSelect.addEventListener('change', render);
        compareSelect.addEventListener('change', render);
        document.getElementById('search-box').addEventListener('input', renderTable);
        document.getElementById('btn-export').addEventListener('click', exportCSV);

        render();
      } catch (e) {
        console.error("Failed to load dashboard data:", e);
      }
    }

    function formatNumber(num) {
      if (!num && num !== 0) return "-";
      return Number(num).toLocaleString();
    }

    function renderDelta(curr, prev, isPercent=false) {
      if (prev === null || prev === undefined || !curr) return `<span class="flat">-</span>`;
      const diff = curr - prev;
      if (diff === 0) return `<span class="flat">0.00 (0%)</span>`;
      const cls = diff > 0 ? "up" : "down";
      const sign = diff > 0 ? "+" : "";
      const pDiff = prev > 0 ? ((diff / prev) * 100).toFixed(2) : "0.00";
      return `<span class="${cls}">${sign}${isPercent ? diff.toFixed(2) + '%' : formatNumber(diff)} (${sign}${pDiff}%)</span>`;
    }

    function render() {
      const curDate = document.getElementById('date-select').value;
      const compDate = document.getElementById('compare-select').value;

      const curSnap = appData.snapshots[curDate];
      const compSnap = compDate ? appData.snapshots[compDate] : null;

      if (!curSnap) return;

      document.getElementById('last-updated').innerText = 'Generated: ' + appData.generated_at.replace('T', ' ').substring(0, 19);

      const m = curSnap.meta;
      const cm = compSnap ? compSnap.meta : null;

      document.getElementById('kpi-ships').innerText = formatNumber(m.top100_ships);
      document.getElementById('delta-ships').innerHTML = renderDelta(m.top100_ships, cm ? cm.top100_ships : null);

      document.getElementById('kpi-teu').innerText = formatNumber(m.top100_teu);
      document.getElementById('delta-teu').innerHTML = renderDelta(m.top100_teu, cm ? cm.top100_teu : null);

      document.getElementById('kpi-top10').innerText = (m.top10_share || 0).toFixed(2) + '%';
      document.getElementById('delta-top10').innerHTML = renderDelta(m.top10_share, cm ? cm.top10_share : null, true);

      document.getElementById('kpi-orderbook').innerText = formatNumber(m.orderbook_teu);
      document.getElementById('delta-orderbook').innerHTML = renderDelta(m.orderbook_teu, cm ? cm.orderbook_teu : null);

      renderTable();
    }

    function renderTable() {
      const curDate = document.getElementById('date-select').value;
      const curSnap = appData.snapshots[curDate];
      if (!curSnap) return;

      const filter = document.getElementById('search-box').value.toLowerCase();
      const tbody = document.querySelector('#operator-table tbody');
      tbody.innerHTML = '';

      curSnap.operators.forEach(op => {
        const name = op.operator_name || op.operator || '';
        if (filter && !name.toLowerCase().includes(filter)) return;

        const row = `
          <tr>
            <td class="text-center">${op.rank}</td>
            <td class="text-left op-name">${name}</td>
            <td>${formatNumber(op.total_teu)}</td>
            <td>${formatNumber(op.total_ships)}</td>
            <td>${(op.market_share || 0).toFixed(1)}%</td>
            <td>${formatNumber(op.owned_teu)}</td>
            <td>${formatNumber(op.owned_ships)}</td>
            <td>${formatNumber(op.chartered_teu)}</td>
            <td>${formatNumber(op.chartered_ships)}</td>
            <td>${formatNumber(op.orderbook_teu)}</td>
            <td>${formatNumber(op.orderbook_ships)}</td>
          </tr>
        `;
        tbody.innerHTML += row;
      });
    }

    function exportCSV() {
      const curDate = document.getElementById('date-select').value;
      const curSnap = appData.snapshots[curDate];
      if (!curSnap) return;

      let csv = "\uFEFFRank,Operator,Total TEU,Total Ships,Market Share (%),Owned TEU,Owned Ships,Chartered TEU,Chartered Ships,Orderbook TEU,Orderbook Ships\\n";
      curSnap.operators.forEach(op => {
        const name = `"${(op.operator_name || op.operator || '').replace(/"/g, '""')}"`;
        csv += `${op.rank},${name},${op.total_teu},${op.total_ships},${op.market_share},${op.owned_teu},${op.owned_ships},${op.chartered_teu},${op.chartered_ships},${op.orderbook_teu},${op.orderbook_ships}\\n`;
      });

      const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `Alphaliner_TOP100_${curDate}.csv`;
      a.click();
    }

    window.onload = init;
  </script>
</body>
</html>
"""
    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html_content)
    logger.info(f"Generated dashboard HTML: {HTML_PATH}")

# =========================================================
# 7. Web Server Routing
# =========================================================
@app.route("/")
def index():
    return send_from_directory(str(BASE_DIR), "index.html")

@app.route("/alphaliner_data.json")
def data_json():
    return send_from_directory(str(BASE_DIR), "alphaliner_data.json")

# =========================================================
# 8. Main Entrypoint
# =========================================================
def main():
    parser = argparse.ArgumentParser(description="Alphaliner Static Terminal Crawler")
    parser.add_argument("--cron", action="store_true", help="Run once to crawl and export without starting the web server")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    init_db()

    if args.cron:
        logger.info("Executing Alphaliner Cron Task...")
        success = process_and_persist()
        export_json_and_html()
        if not success:
            logger.warning("Cron run completed without saving a new snapshot (rejected or unchanged).")
        sys.exit(0)

    # Server mode
    try:
        with get_db_connection() as conn:
            cnt = conn.cursor().execute("SELECT count(*) FROM snapshot_meta").fetchone()[0]
            if cnt == 0:
                logger.info("Database is empty. Initiating initial scrape...")
                process_and_persist()
    except Exception as e:
        logger.warning(f"Initial scrape attempt warning: {e}")

    export_json_and_html()
    logger.info(f"Starting Alphaliner Web Server on http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)

if __name__ == "__main__":
    main()
