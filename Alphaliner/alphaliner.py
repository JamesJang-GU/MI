# -*- coding: utf-8 -*-
"""
Alphaliner TOP 100 Liner Operators Static Dashboard Crawler (GitHub Optimized)
- Direct ExtJS Store extraction (Top100description)
- Official Global Figures + TOP 100 Capacity Separation
- Historical snapshot comparison & Full Time-travel Delta analysis
- Authentic Alphaliner 2-tier Grouped Table & UTF-8 BOM CSV Export
- Bloomberg MI Terminal Look & Feel

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
from flask import Flask, Response, jsonify, send_from_directory
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait

# =========================================================
# 1. Basic Configuration & Paths
# =========================================================
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "alphaliner_history.db"
JSON_PATH = BASE_DIR / "alphaliner_data.json"
HTML_PATH = BASE_DIR / "index.html"
TARGET_URL = "https://alphaliner.axsmarine.com/PublicTop100/"

MIN_EXPECTED_OPERATOR_COUNT = 50

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("AlphalinerCrawler")

db_lock = Lock()
app = Flask(__name__)

# =========================================================
# 2. Database Management
# =========================================================
def init_db():
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS snapshot_meta (
                snapshot_date TEXT PRIMARY KEY,
                global_total_teu INTEGER,
                global_total_ships INTEGER,
                top100_total_teu INTEGER,
                top100_total_ships INTEGER,
                top10_total_teu INTEGER,
                top10_share REAL,
                orderbook_total_teu INTEGER,
                collected_at TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS operator_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_date TEXT,
                rank INTEGER,
                operator TEXT,
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
                FOREIGN KEY (snapshot_date) REFERENCES snapshot_meta(snapshot_date),
                UNIQUE(snapshot_date, rank)
            )
        """)

        cur.execute("CREATE INDEX IF NOT EXISTS idx_op_snap_date ON operator_snapshots(snapshot_date)")
        conn.commit()
        conn.close()

def save_snapshot_to_db(meta, operators):
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        snap_date = meta["snapshot_date"]

        cur.execute("""
            INSERT OR REPLACE INTO snapshot_meta
            (snapshot_date, global_total_teu, global_total_ships, top100_total_teu,
             top100_total_ships, top10_total_teu, top10_share, orderbook_total_teu, collected_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            snap_date,
            meta.get("global_total_teu", 0),
            meta.get("global_total_ships", 0),
            meta.get("top100_total_teu", 0),
            meta.get("top100_total_ships", 0),
            meta.get("top10_total_teu", 0),
            meta.get("top10_share", 0.0),
            meta.get("orderbook_total_teu", 0),
            meta.get("collected_at", datetime.now().isoformat())
        ))

        cur.execute("DELETE FROM operator_snapshots WHERE snapshot_date = ?", (snap_date,))

        for op in operators:
            cur.execute("""
                INSERT INTO operator_snapshots
                (snapshot_date, rank, operator, total_teu, total_ships, market_share,
                 owned_teu, owned_ships, owned_share, chartered_teu, chartered_ships,
                 chartered_share, orderbook_teu, orderbook_ships, orderbook_share)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                snap_date,
                op["rank"],
                op["operator"],
                op["total_teu"],
                op["total_ships"],
                op["market_share"],
                op["owned_teu"],
                op["owned_ships"],
                op["owned_share"],
                op["chartered_teu"],
                op["chartered_ships"],
                op["chartered_share"],
                op["orderbook_teu"],
                op["orderbook_ships"],
                op["orderbook_share"]
            ))

        conn.commit()
        conn.close()
        logger.info(f"Database successfully updated for snapshot date: {snap_date}")

def get_all_snapshots_data():
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute("SELECT * FROM snapshot_meta ORDER BY snapshot_date DESC")
        meta_rows = cur.fetchall()

        result = {"dates": [], "snapshots": {}}

        for m in meta_rows:
            d = m["snapshot_date"]
            result["dates"].append(d)

            cur.execute("SELECT * FROM operator_snapshots WHERE snapshot_date = ? ORDER BY rank ASC", (d,))
            op_rows = cur.fetchall()

            result["snapshots"][d] = {
                "meta": dict(m),
                "operators": [dict(r) for r in op_rows]
            }

        conn.close()
        return result

# =========================================================
# 3. Selenium Web Extraction
# =========================================================
def get_chrome_driver(headless=True):
    options = Options()

    if headless:
        options.add_argument("--headless=new")

    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-notifications")
    options.add_argument("--lang=en-US")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    chrome_binary = os.getenv("CHROME_BINARY", "").strip()
    if chrome_binary:
        chrome_binary_path = Path(chrome_binary)
        if chrome_binary_path.exists():
            options.binary_location = str(chrome_binary_path)
            logger.info("Using Chrome binary from CHROME_BINARY: %s", chrome_binary_path)
        else:
            logger.warning("CHROME_BINARY configured but not found: %s", chrome_binary)

    chromedriver_path = os.getenv("CHROMEDRIVER", "").strip()

    try:
        if chromedriver_path:
            chromedriver_file = Path(chromedriver_path)
            if chromedriver_file.exists():
                logger.info("Using ChromeDriver from CHROMEDRIVER: %s", chromedriver_file)
                driver = webdriver.Chrome(service=Service(str(chromedriver_file)), options=options)
            else:
                logger.warning("CHROMEDRIVER configured but not found. Falling back to Selenium Manager.")
                driver = webdriver.Chrome(options=options)
        else:
            driver = webdriver.Chrome(options=options)

        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        driver.set_page_load_timeout(60)
        driver.implicitly_wait(3)
        return driver

    except WebDriverException as exc:
        logger.exception("Failed to initialize Selenium Driver.")
        raise RuntimeError(
            "Chrome/ChromeDriver initialization failed. Verify browser and driver configuration."
        ) from exc

def crawl_alphaliner():
    logger.info("Initializing Selenium Driver for Alphaliner...")
    driver = None
    try:
        driver = get_chrome_driver(headless=True)
        driver.get(TARGET_URL)

        WebDriverWait(driver, 25).until(
            lambda d: d.execute_script("return typeof Ext !== 'undefined' && Ext.isReady === true;")
        )
        time.sleep(3)

        js_extractor = """
        var result = { meta: {}, records: [] };
        try {
            var store = Ext.data.StoreManager.lookup('Top100description') ||
                        (Ext.ComponentQuery.query('grid')[0] && Ext.ComponentQuery.query('grid')[0].getStore());
            if (store) {
                store.each(function(rec) {
                    result.records.push(rec.data);
                });
            }
            result.rawText = document.body.innerText;
        } catch(e) {
            result.error = e.toString();
        }
        return result;
        """

        data = driver.execute_script(js_extractor)
        records = data.get("records", [])

        if not records:
            logger.warning("Direct ExtJS store lookup returned empty. Attempting grid fallback...")
            fallback_js = """
            var out = [];
            var grids = Ext.ComponentQuery.query('gridpanel');
            if (grids.length > 0) {
                var s = grids[0].getStore();
                if (s) {
                    s.each(function(r) { out.push(r.data); });
                }
            }
            return out;
            """
            records = driver.execute_script(fallback_js)

        today_str = datetime.now().strftime("%Y-%m-%d")
        operators = []
        orderbook_total_teu = 0

        def parse_num(val):
            if val is None:
                return 0
            if isinstance(val, (int, float)):
                return val
            numeric_text = re.sub(r"[^\d.]", "", str(val))
            if not numeric_text:
                return 0
            return float(numeric_text) if "." in numeric_text else int(numeric_text)

        for r in records:
            rank = int(parse_num(r.get("rank") or r.get("Rank") or r.get("id") or 0))
            if rank <= 0 or rank > 100:
                continue

            op_name = str(r.get("operator") or r.get("Operator") or r.get("liner") or f"Operator {rank}").strip()
            tot_teu = int(parse_num(r.get("tot_teu") or r.get("total_teu") or r.get("teu") or 0))
            tot_ships = int(parse_num(r.get("tot_ships") or r.get("total_ships") or r.get("ships") or 0))
            m_share = float(parse_num(r.get("share") or r.get("market_share") or 0.0))

            owned_teu = int(parse_num(r.get("owned_teu") or 0))
            owned_ships = int(parse_num(r.get("owned_ships") or 0))
            owned_share = float(parse_num(r.get("owned_share") or 0.0))

            cha_teu = int(parse_num(r.get("chartered_teu") or r.get("cha_teu") or 0))
            cha_ships = int(parse_num(r.get("chartered_ships") or r.get("cha_ships") or 0))
            cha_share = float(parse_num(r.get("chartered_share") or r.get("cha_share") or 0.0))

            ord_teu = int(parse_num(r.get("orderbook_teu") or r.get("ord_teu") or 0))
            ord_ships = int(parse_num(r.get("orderbook_ships") or r.get("ord_ships") or 0))
            ord_share = float(parse_num(r.get("orderbook_share") or r.get("ord_share") or 0.0))

            orderbook_total_teu += ord_teu

            operators.append({
                "rank": rank,
                "operator": op_name,
                "total_teu": tot_teu,
                "total_ships": tot_ships,
                "market_share": m_share,
                "owned_teu": owned_teu,
                "owned_ships": owned_ships,
                "owned_share": owned_share,
                "chartered_teu": cha_teu,
                "chartered_ships": cha_ships,
                "chartered_share": cha_share,
                "orderbook_teu": ord_teu,
                "orderbook_ships": ord_ships,
                "orderbook_share": ord_share
            })

        operators = sorted(operators, key=lambda x: x["rank"])

        if len(operators) < MIN_EXPECTED_OPERATOR_COUNT:
            raise RuntimeError(
                f"Alphaliner validation failed: Extracted {len(operators)} operators; "
                f"expected >= {MIN_EXPECTED_OPERATOR_COUNT}. Preserving existing database."
            )

        top100_teu = sum(x["total_teu"] for x in operators)
        top100_ships = sum(x["total_ships"] for x in operators)
        top10_teu = sum(x["total_teu"] for x in operators[:10])

        raw_text = data.get("rawText", "")
        m_glob_teu = re.search(r"Total:\s*([\d,]+)\s*TEU", raw_text)
        m_glob_ships = re.search(r"Total:\s*[\d,]+\s*TEU\s*\(\s*([\d,]+)\s*Ships\)", raw_text)

        global_total_teu = int(re.sub(r"[^\d]", "", m_glob_teu.group(1))) if m_glob_teu else int(top100_teu / 0.95)
        global_total_ships = int(re.sub(r"[^\d]", "", m_glob_ships.group(1))) if m_glob_ships else int(top100_ships / 0.90)

        top10_share = round((top10_teu / global_total_teu * 100), 2) if global_total_teu > 0 else 0.0

        meta = {
            "snapshot_date": today_str,
            "global_total_teu": global_total_teu,
            "global_total_ships": global_total_ships,
            "top100_total_teu": top100_teu,
            "top100_total_ships": top100_ships,
            "top10_total_teu": top10_teu,
            "top10_share": top10_share,
            "orderbook_total_teu": orderbook_total_teu,
            "collected_at": datetime.now().isoformat()
        }

        save_snapshot_to_db(meta, operators)
        logger.info(f"Crawl completed successfully: {len(operators)} operators processed.")
        return True

    except Exception as exc:
        logger.error(f"Alphaliner crawl failed: {exc}", exc_info=True)
        return False

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

# =========================================================
# 4. Bloomberg MI Terminal HTML Template
# =========================================================
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Alphaliner TOP 100 Liner Operators | Bloomberg MI Terminal</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>

<style>
:root {
  --bg-primary: #02060b;
  --bg-secondary: #07111c;
  --bg-tertiary: #0d1d2b;
  --border-subtle: #1b3043;
  --border-focus: #29445c;
  --text-main: #f3f7fb;
  --text-muted: #879caf;
  --text-dim: #536c80;
  --accent-orange: #f6a623;
  --accent-blue: #45a9f5;
  --accent-green: #36ca82;
  --accent-red: #ff6262;
  --accent-purple: #bb86fc;
  --font-mono: 'JetBrains Mono', monospace;
  --font-sans: 'Inter', sans-serif;
}

* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background-color: var(--bg-primary);
  color: var(--text-main);
  font-family: var(--font-sans);
  font-size: 13px;
  line-height: 1.4;
  overflow-x: hidden;
}

/* 1. Header Ticker */
.terminal-header {
  background: var(--bg-secondary);
  border-bottom: 1px solid var(--border-subtle);
  padding: 8px 16px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  position: sticky;
  top: 0;
  z-index: 100;
}
.brand {
  display: flex;
  align-items: center;
  gap: 12px;
}
.brand-tag {
  background: var(--accent-orange);
  color: #000;
  font-weight: 700;
  font-size: 11px;
  padding: 2px 6px;
  border-radius: 2px;
  font-family: var(--font-mono);
}
.brand-title {
  font-size: 14px;
  font-weight: 600;
  letter-spacing: 0.5px;
  text-transform: uppercase;
}
.terminal-controls {
  display: flex;
  align-items: center;
  gap: 12px;
}
.select-wrapper {
  display: flex;
  align-items: center;
  gap: 6px;
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-muted);
}
select, input[type="text"] {
  background: var(--bg-tertiary);
  border: 1px solid var(--border-subtle);
  color: var(--text-main);
  font-family: var(--font-mono);
  font-size: 12px;
  padding: 4px 8px;
  border-radius: 2px;
  outline: none;
}
select:focus, input[type="text"]:focus {
  border-color: var(--accent-blue);
}
.btn {
  background: var(--bg-tertiary);
  border: 1px solid var(--border-subtle);
  color: var(--text-main);
  padding: 4px 10px;
  font-family: var(--font-mono);
  font-size: 11px;
  cursor: pointer;
  border-radius: 2px;
  transition: all 0.15s ease;
}
.btn:hover {
  background: var(--border-subtle);
  border-color: var(--border-focus);
}
.btn-primary {
  background: rgba(246, 166, 35, 0.15);
  border-color: var(--accent-orange);
  color: var(--accent-orange);
}
.btn-primary:hover {
  background: var(--accent-orange);
  color: #000;
}

/* 2. Grid Container */
.container {
  padding: 16px;
  max-width: 1920px;
  margin: 0 auto;
  display: grid;
  grid-template-columns: repeat(12, 1fr);
  gap: 12px;
}

.panel {
  background: var(--bg-secondary);
  border: 1px solid var(--border-subtle);
  border-radius: 2px;
  display: flex;
  flex-direction: column;
}
.panel-header {
  padding: 8px 12px;
  border-bottom: 1px solid var(--border-subtle);
  font-family: var(--font-mono);
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: var(--text-muted);
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.panel-body {
  padding: 12px;
  flex: 1;
}

/* KPI Summary Cards */
.col-kpi {
  grid-column: span 3;
}
.kpi-card {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.kpi-title {
  font-size: 11px;
  color: var(--text-dim);
  text-transform: uppercase;
  font-family: var(--font-mono);
}
.kpi-value {
  font-size: 22px;
  font-weight: 700;
  font-family: var(--font-mono);
  color: var(--text-main);
}
.kpi-sub {
  font-size: 11px;
  font-family: var(--font-mono);
  display: flex;
  gap: 6px;
}
.delta-pos { color: var(--accent-green); }
.delta-neg { color: var(--accent-red); }
.delta-zero { color: var(--text-dim); }

/* Chart Section */
.col-chart {
  grid-column: span 12;
  height: 280px;
}

/* Table Section */
.col-table {
  grid-column: span 12;
}
.table-wrapper {
  overflow-x: auto;
  max-height: 650px;
}
table {
  width: 100%;
  border-collapse: collapse;
  font-family: var(--font-mono);
  font-size: 12px;
  text-align: right;
  white-space: nowrap;
}
th {
  background: var(--bg-tertiary);
  color: var(--text-muted);
  font-weight: 600;
  padding: 6px 10px;
  border: 1px solid var(--border-subtle);
  position: sticky;
  top: 0;
  z-index: 10;
  text-transform: uppercase;
  font-size: 11px;
}
th.th-group {
  text-align: center;
  border-bottom: 1px solid var(--border-focus);
}
th.align-left, td.align-left { text-align: left; }
th.align-center, td.align-center { text-align: center; }

td {
  padding: 5px 10px;
  border: 1px solid rgba(27, 48, 67, 0.4);
  color: var(--text-main);
}
tr:nth-child(even) {
  background: rgba(13, 29, 43, 0.3);
}
tr:hover {
  background: rgba(69, 169, 245, 0.08);
}
.rank-badge {
  display: inline-block;
  min-width: 24px;
  text-align: center;
  padding: 1px 4px;
  background: var(--bg-tertiary);
  border-radius: 2px;
  color: var(--accent-blue);
  font-weight: 600;
}
.rank-top10 {
  background: rgba(246, 166, 35, 0.2);
  color: var(--accent-orange);
}
.delta-inline {
  font-size: 10px;
  margin-left: 4px;
}

/* Footer Status Bar */
.terminal-footer {
  background: var(--bg-secondary);
  border-top: 1px solid var(--border-subtle);
  padding: 6px 16px;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-dim);
  display: flex;
  justify-content: space-between;
  align-items: center;
  position: fixed;
  bottom: 0;
  width: 100%;
  z-index: 100;
}
.status-indicator {
  display: inline-block;
  width: 8px;
  height: 8px;
  background: var(--accent-green);
  border-radius: 50%;
  margin-right: 6px;
}

@media (max-width: 1200px) {
  .col-kpi { grid-column: span 6; }
}
@media (max-width: 768px) {
  .col-kpi { grid-column: span 12; }
  .terminal-header { flex-direction: column; gap: 8px; align-items: flex-start; }
  .terminal-controls { flex-wrap: wrap; width: 100%; justify-content: space-between; }
}
</style>
</head>
<body>

<header class="terminal-header">
  <div class="brand">
    <span class="brand-tag">MI-TERM</span>
    <span class="brand-title">ALPHALINER TOP 100 / FLEET CAPACITY DEPLOYMENT</span>
  </div>
  <div class="terminal-controls">
    <div class="select-wrapper">
      <span>SNAPSHOT:</span>
      <select id="dateSelect" onchange="switchSnapshot()"></select>
    </div>
    <div class="select-wrapper">
      <span>COMPARE TO:</span>
      <select id="compareSelect" onchange="recalculateDeltas()"></select>
    </div>
    <button class="btn btn-primary" onclick="exportCSV()">EXPORT CSV</button>
  </div>
</header>

<main class="container" style="margin-bottom: 30px;">
  <!-- KPI Panels -->
  <section class="panel col-kpi">
    <div class="panel-header">TOTAL GLOBAL FLEET</div>
    <div class="panel-body kpi-card">
      <div class="kpi-title">Capacity & Ships</div>
      <div class="kpi-value" id="kpiGlobalTeu">-- TEU</div>
      <div class="kpi-sub">
        <span id="kpiGlobalShips">-- Ships</span>
        <span id="deltaGlobalTeu" class="delta-zero"></span>
      </div>
    </div>
  </section>

  <section class="panel col-kpi">
    <div class="panel-header">TOP 100 OPERATORS</div>
    <div class="panel-body kpi-card">
      <div class="kpi-title">Deployed Capacity</div>
      <div class="kpi-value" id="kpiTop100Teu">-- TEU</div>
      <div class="kpi-sub">
        <span id="kpiTop100Ships">-- Ships</span>
        <span id="deltaTop100Teu" class="delta-zero"></span>
      </div>
    </div>
  </section>

  <section class="panel col-kpi">
    <div class="panel-header">TOP 10 CONCENTRATION</div>
    <div class="panel-body kpi-card">
      <div class="kpi-title">Global Share</div>
      <div class="kpi-value" id="kpiTop10Share">-- %</div>
      <div class="kpi-sub">
        <span id="kpiTop10Teu">-- TEU</span>
        <span id="deltaTop10Share" class="delta-zero"></span>
      </div>
    </div>
  </section>

  <section class="panel col-kpi">
    <div class="panel-header">ORDERBOOK (TOP 100)</div>
    <div class="panel-body kpi-card">
      <div class="kpi-title">On Order Capacity</div>
      <div class="kpi-value" id="kpiOrderbookTeu">-- TEU</div>
      <div class="kpi-sub">
        <span id="kpiOrderbookRatio">-- % of active</span>
        <span id="deltaOrderbookTeu" class="delta-zero"></span>
      </div>
    </div>
  </section>

  <!-- Top 10 Market Share Chart -->
  <section class="panel col-chart">
    <div class="panel-header">
      <span>TOP 10 OPERATOR CAPACITY DISTRIBUTION (TEU)</span>
      <span id="chartSubTitle" style="color: var(--accent-orange);"></span>
    </div>
    <div class="panel-body" style="position: relative; height: 100%;">
      <canvas id="topChart"></canvas>
    </div>
  </section>

  <!-- Full Table -->
  <section class="panel col-table">
    <div class="panel-header">
      <span>TOP 100 LINER OPERATORS BREAKDOWN</span>
      <input type="text" id="filterInput" placeholder="Filter operator..." onkeyup="filterTable()" style="width: 180px;">
    </div>
    <div class="table-wrapper">
      <table id="operatorTable">
        <thead>
          <tr>
            <th rowspan="2" class="align-center" style="width: 50px;">RANK</th>
            <th rowspan="2" class="align-left">OPERATOR</th>
            <th colspan="3" class="th-group">TOTAL FLEET</th>
            <th colspan="3" class="th-group">OWNED</th>
            <th colspan="3" class="th-group">CHARTERED</th>
            <th colspan="3" class="th-group">ORDERBOOK</th>
          </tr>
          <tr>
            <th>TEU</th>
            <th>SHIPS</th>
            <th>SHARE</th>
            <th>TEU</th>
            <th>SHIPS</th>
            <th>% OWN</th>
            <th>TEU</th>
            <th>SHIPS</th>
            <th>% CHT</th>
            <th>TEU</th>
            <th>SHIPS</th>
            <th>% O.B.</th>
          </tr>
        </thead>
        <tbody id="tableBody"></tbody>
      </table>
    </div>
  </section>
</main>

<footer class="terminal-footer">
  <div>
    <span class="status-indicator"></span>
    <span id="systemStatus">STATUS: TERMINAL CONNECTED (PORT 8050)</span>
  </div>
  <div id="collectedTimestamp">DATA COLLECTED: --</div>
</footer>

<script>
let DATA_STORE = null;
let currentChart = null;

function fmt(n) {
  if (n === null || n === undefined || isNaN(n)) return "0";
  return Number(n).toLocaleString('en-US');
}

function fmtDelta(diff, isPercent = false) {
  if (diff === null || diff === undefined || isNaN(diff) || diff === 0) {
    return `<span class="delta-zero">-</span>`;
  }
  const cls = diff > 0 ? "delta-pos" : "delta-neg";
  const sign = diff > 0 ? "+" : "";
  const val = isPercent ? diff.toFixed(2) + "%" : fmt(diff);
  return `<span class="${cls}">${sign}${val}</span>`;
}

async function initDashboard() {
  try {
    const res = await fetch('alphaliner_data.json?t=' + Date.now());
    DATA_STORE = await res.json();
    setupDateSelectors();
    renderAll();
  } catch (err) {
    console.error("Dashboard initialization error:", err);
    document.getElementById("systemStatus").innerText = "STATUS: DATA LOAD FAILURE";
    document.getElementById("systemStatus").previousElementSibling.style.background = "var(--accent-red)";
  }
}

function setupDateSelectors() {
  const dateSel = document.getElementById("dateSelect");
  const compSel = document.getElementById("compareSelect");
  dateSel.innerHTML = "";
  compSel.innerHTML = "";

  if (!DATA_STORE.dates || DATA_STORE.dates.length === 0) return;

  DATA_STORE.dates.forEach((d, idx) => {
    const opt = new Option(d, d);
    dateSel.add(opt);
  });

  const noneOpt = new Option("NONE", "none");
  compSel.add(noneOpt);

  DATA_STORE.dates.forEach((d, idx) => {
    const opt = new Option(d, d);
    compSel.add(opt);
  });

  if (DATA_STORE.dates.length > 1) {
    compSel.selectedIndex = 2; // Default to previous snapshot
  }
}

function switchSnapshot() {
  renderAll();
}

function recalculateDeltas() {
  renderAll();
}

function renderAll() {
  const activeDate = document.getElementById("dateSelect").value;
  const compDate = document.getElementById("compareSelect").value;

  if (!DATA_STORE.snapshots[activeDate]) return;

  const currentSnap = DATA_STORE.snapshots[activeDate];
  const compSnap = (compDate !== "none" && DATA_STORE.snapshots[compDate]) ? DATA_STORE.snapshots[compDate] : null;

  renderKPIs(currentSnap, compSnap);
  renderChart(currentSnap);
  renderTable(currentSnap, compSnap);

  document.getElementById("collectedTimestamp").innerText =
    `DATA COLLECTED: ${currentSnap.meta.collected_at || activeDate}`;
}

function renderKPIs(cur, comp) {
  const cm = cur.meta;
  const pm = comp ? comp.meta : null;

  document.getElementById("kpiGlobalTeu").innerText = `${fmt(cm.global_total_teu)} TEU`;
  document.getElementById("kpiGlobalShips").innerText = `${fmt(cm.global_total_ships)} Ships`;
  document.getElementById("deltaGlobalTeu").innerHTML = pm
    ? fmtDelta(cm.global_total_teu - pm.global_total_teu) + " TEU" : "";

  document.getElementById("kpiTop100Teu").innerText = `${fmt(cm.top100_total_teu)} TEU`;
  document.getElementById("kpiTop100Ships").innerText = `${fmt(cm.top100_total_ships)} Ships`;
  document.getElementById("deltaTop100Teu").innerHTML = pm
    ? fmtDelta(cm.top100_total_teu - pm.top100_total_teu) + " TEU" : "";

  document.getElementById("kpiTop10Share").innerText = `${cm.top10_share ? cm.top10_share.toFixed(1) : 0.0} %`;
  document.getElementById("kpiTop10Teu").innerText = `${fmt(cm.top10_total_teu)} TEU`;
  document.getElementById("deltaTop10Share").innerHTML = pm
    ? fmtDelta(cm.top10_share - pm.top10_share, true) : "";

  document.getElementById("kpiOrderbookTeu").innerText = `${fmt(cm.orderbook_total_teu)} TEU`;
  const ratio = cm.top100_total_teu > 0 ? ((cm.orderbook_total_teu / cm.top100_total_teu) * 100).toFixed(1) : 0.0;
  document.getElementById("kpiOrderbookRatio").innerText = `${ratio} % of active`;
  document.getElementById("deltaOrderbookTeu").innerHTML = pm
    ? fmtDelta(cm.orderbook_total_teu - pm.orderbook_total_teu) + " TEU" : "";
}

function renderChart(snap) {
  const top10 = snap.operators.slice(0, 10);
  const labels = top10.map(o => `${o.rank}. ${o.operator}`);
  const owned = top10.map(o => o.owned_teu);
  const chartered = top10.map(o => o.chartered_teu);
  const orderbook = top10.map(o => o.orderbook_teu);

  document.getElementById("chartSubTitle").innerText = `AS OF ${snap.meta.snapshot_date}`;

  if (currentChart) {
    currentChart.destroy();
  }

  const ctx = document.getElementById("topChart").getContext("2d");
  currentChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: labels,
      datasets: [
        {
          label: 'Owned TEU',
          data: owned,
          backgroundColor: '#45a9f5',
          stack: 'active'
        },
        {
          label: 'Chartered TEU',
          data: chartered,
          backgroundColor: '#f6a623',
          stack: 'active'
        },
        {
          label: 'Orderbook TEU',
          data: orderbook,
          backgroundColor: 'rgba(187, 134, 252, 0.4)',
          borderColor: '#bb86fc',
          borderWidth: 1,
          stack: 'orderbook'
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          position: 'top',
          labels: { color: '#879caf', font: { family: 'JetBrains Mono', size: 11 } }
        }
      },
      scales: {
        x: {
          stacked: true,
          ticks: { color: '#879caf', font: { family: 'JetBrains Mono', size: 10 } },
          grid: { color: 'rgba(27, 48, 67, 0.5)' }
        },
        y: {
          stacked: true,
          ticks: {
            color: '#879caf',
            font: { family: 'JetBrains Mono', size: 10 },
            callback: val => (val / 1000000).toFixed(1) + 'M'
          },
          grid: { color: 'rgba(27, 48, 67, 0.5)' }
        }
      }
    }
  });
}

function renderTable(cur, comp) {
  const tbody = document.getElementById("tableBody");
  tbody.innerHTML = "";

  const compMap = {};
  if (comp && comp.operators) {
    comp.operators.forEach(o => {
      compMap[o.operator.toLowerCase()] = o;
    });
  }

  cur.operators.forEach(o => {
    const prev = compMap[o.operator.toLowerCase()];
    const tr = document.createElement("tr");

    let rankDelta = "";
    let teuDelta = "";
    let shipsDelta = "";

    if (prev) {
      const rDiff = prev.rank - o.rank;
      if (rDiff !== 0) {
        const cls = rDiff > 0 ? "delta-pos" : "delta-neg";
        const sign = rDiff > 0 ? "▲" : "▼";
        rankDelta = `<span class="${cls} delta-inline">${sign}${Math.abs(rDiff)}</span>`;
      }
      teuDelta = `<div class="delta-inline">${fmtDelta(o.total_teu - prev.total_teu)}</div>`;
      shipsDelta = `<div class="delta-inline">${fmtDelta(o.total_ships - prev.total_ships)}</div>`;
    }

    const rankCls = o.rank <= 10 ? "rank-badge rank-top10" : "rank-badge";

    tr.innerHTML = `
      <td class="align-center"><span class="${rankCls}">${o.rank}</span>${rankDelta}</td>
      <td class="align-left" style="font-weight: 600;">${o.operator}</td>
      <td><strong>${fmt(o.total_teu)}</strong>${teuDelta}</td>
      <td>${fmt(o.total_ships)}${shipsDelta}</td>
      <td>${o.market_share.toFixed(1)}%</td>
      <td>${fmt(o.owned_teu)}</td>
      <td>${fmt(o.owned_ships)}</td>
      <td>${o.owned_share.toFixed(1)}%</td>
      <td>${fmt(o.chartered_teu)}</td>
      <td>${fmt(o.chartered_ships)}</td>
      <td>${o.chartered_share.toFixed(1)}%</td>
      <td style="color: var(--accent-purple);">${fmt(o.orderbook_teu)}</td>
      <td style="color: var(--accent-purple);">${fmt(o.orderbook_ships)}</td>
      <td style="color: var(--accent-purple);">${o.orderbook_share.toFixed(1)}%</td>
    `;
    tbody.appendChild(tr);
  });
}

function filterTable() {
  const q = document.getElementById("filterInput").value.toLowerCase();
  const rows = document.querySelectorAll("#tableBody tr");
  rows.forEach(r => {
    const op = r.cells[1].innerText.toLowerCase();
    r.style.display = op.includes(q) ? "" : "none";
  });
}

function exportCSV() {
  const activeDate = document.getElementById("dateSelect").value;
  if (!DATA_STORE || !DATA_STORE.snapshots[activeDate]) return;

  const snap = DATA_STORE.snapshots[activeDate];
  let csv = "\uFEFF"; // UTF-8 BOM
  csv += "Rank,Operator,Total TEU,Total Ships,Market Share %,Owned TEU,Owned Ships,Owned Share %,Chartered TEU,Chartered Ships,Chartered Share %,Orderbook TEU,Orderbook Ships,Orderbook % of Fleet\n";

  snap.operators.forEach(o => {
    csv += `"${o.rank}","${o.operator.replace(/"/g, '""')}",${o.total_teu},${o.total_ships},${o.market_share},${o.owned_teu},${o.owned_ships},${o.owned_share},${o.chartered_teu},${o.chartered_ships},${o.chartered_share},${o.orderbook_teu},${o.orderbook_ships},${o.orderbook_share}\n`;
  });

  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.setAttribute("download", `Alphaliner_TOP100_${activeDate}.csv`);
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
}

document.addEventListener("DOMContentLoaded", initDashboard);
</script>
</body>
</html>
"""

# =========================================================
# 5. Static Generator & Output Artifacts
# =========================================================
def generate_static_files():
    data = get_all_snapshots_data()

    if not data["dates"]:
        logger.warning("No snapshot records found in database. Skipping file generation.")
        return

    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"Generated static data JSON: {JSON_PATH}")

    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(HTML_TEMPLATE)
    logger.info(f"Generated static HTML dashboard: {HTML_PATH}")

# =========================================================
# 6. Web & API Endpoints
# =========================================================
@app.route("/")
def index():
    if not HTML_PATH.exists():
        generate_static_files()
    return send_from_directory(BASE_DIR, "index.html")

@app.route("/alphaliner_data.json")
def get_json():
    if not JSON_PATH.exists():
        generate_static_files()
    return send_from_directory(BASE_DIR, "alphaliner_data.json")

@app.route("/api/snapshots")
def api_snapshots():
    return jsonify(get_all_snapshots_data())

@app.route("/api/crawl", methods=["POST", "GET"])
def api_crawl():
    crawl_success = crawl_alphaliner()
    if not crawl_success:
        return jsonify({
            "status": "error",
            "message": "Crawler failed integrity validation. Static artifacts preserved without overwrite."
        }), 502

    generate_static_files()
    return jsonify({"status": "success", "message": "Crawler executed and static files refreshed."})

# =========================================================
# 7. Main Execution Router
# =========================================================
def main():
    parser = argparse.ArgumentParser(description="Alphaliner TOP 100 Bloomberg MI Terminal Dashboard")
    parser.add_argument("--no-crawl", action="store_true", help="Skip crawling and generate files from DB")
    parser.add_argument("--cron", action="store_true", help="Run crawl once, generate static files and exit")
    parser.add_argument("--port", type=int, default=8050, help="Port to run Flask server on (default: 8050)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host IP to bind (default: 0.0.0.0)")

    args = parser.parse_args()

    init_db()

    if args.no_crawl:
        logger.info("Crawling skipped by --no-crawl flag. Generating static artifacts from existing database.")
        generate_static_files()
    else:
        logger.info("Executing scheduled crawl routine...")
        crawl_success = crawl_alphaliner()
        if crawl_success:
            generate_static_files()
        else:
            logger.error("Static artifact generation skipped because crawler validation failed.")
            if args.cron:
                sys.exit(1)

    if args.cron:
        logger.info("Cron execution complete.")
        return

    logger.info(f"Starting Bloomberg MI Terminal Server on http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)

if __name__ == "__main__":
    main()
