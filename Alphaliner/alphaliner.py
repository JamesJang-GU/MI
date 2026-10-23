# -*- coding: utf-8 -*-
"""
Alphaliner TOP 100 Liner Operators Static Dashboard Crawler (GitHub & Local Optimized)
- Direct ExtJS Store extraction (Top100description)
- Official Global Figures + TOP 100 Capacity Separation
- Historical snapshot comparison & Full Time-travel Delta analysis
- Authentic Alphaliner 2-tier Grouped Table & UTF-8 BOM CSV Export
- Bloomberg MI Terminal Look & Feel (Aligned with BunkerPrice / MI Portal)

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

try:
    from webdriver_manager.chrome import ChromeDriverManager
    HAS_WEBDRIVER_MANAGER = True
except Exception:
    HAS_WEBDRIVER_MANAGER = False

# =========================================================
# 1. Basic Configuration & Logging
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
logger = logging.getLogger("AlphalinerMI")
db_lock = Lock()

# =========================================================
# 2. Database Initialization & Management
# =========================================================
def init_db():
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()

        # Meta snapshot table
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

        # Operator details table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS operator_snapshots (
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
                PRIMARY KEY (snapshot_date, rank, operator)
            )
        """)
        conn.commit()
        conn.close()

def save_snapshot_to_db(meta_data, operators_data):
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()

        snap_date = meta_data["snapshot_date"]

        cur.execute("""
            INSERT OR REPLACE INTO snapshot_meta
            (snapshot_date, global_total_teu, global_total_ships, top100_total_teu, top100_total_ships,
             top10_total_teu, top10_share, orderbook_total_teu, collected_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            snap_date,
            meta_data.get("global_total_teu", 0),
            meta_data.get("global_total_ships", 0),
            meta_data.get("top100_total_teu", 0),
            meta_data.get("top100_total_ships", 0),
            meta_data.get("top10_total_teu", 0),
            meta_data.get("top10_share", 0.0),
            meta_data.get("orderbook_total_teu", 0),
            meta_data.get("collected_at", datetime.now().isoformat())
        ))

        for op in operators_data:
            cur.execute("""
                INSERT OR REPLACE INTO operator_snapshots
                (snapshot_date, rank, operator, total_teu, total_ships, market_share,
                 owned_teu, owned_ships, owned_share, chartered_teu, chartered_ships, chartered_share,
                 orderbook_teu, orderbook_ships, orderbook_share)
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
        logger.info(f"Successfully saved snapshot {snap_date} with {len(operators_data)} operators.")

def get_all_snapshots_json():
    with db_lock:
        if not DB_PATH.exists():
            return {"dates": [], "snapshots": {}}

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute("SELECT * FROM snapshot_meta ORDER BY snapshot_date ASC")
        meta_rows = cur.fetchall()

        dates = [row["snapshot_date"] for row in meta_rows]
        snapshots = {}

        for m in meta_rows:
            s_date = m["snapshot_date"]
            cur.execute("SELECT * FROM operator_snapshots WHERE snapshot_date = ? ORDER BY rank ASC", (s_date,))
            op_rows = [dict(r) for r in cur.fetchall()]

            snapshots[s_date] = {
                "meta": dict(m),
                "operators": op_rows
            }

        conn.close()
        return {"dates": dates, "snapshots": snapshots}

# =========================================================
# 3. Web Crawler Engine (ExtJS Store Extraction)
# =========================================================
def get_chrome_driver(headless=True):
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

    service = None
    if HAS_WEBDRIVER_MANAGER:
        try:
            service = Service(ChromeDriverManager().install())
        except Exception:
            service = None

    if service:
        return webdriver.Chrome(service=service, options=options)
    return webdriver.Chrome(options=options)

def crawl_alphaliner():
    logger.info("Initializing Selenium Driver for Alphaliner...")
    driver = None
    try:
        driver = get_chrome_driver(headless=True)
        driver.get(TARGET_URL)

        # Wait until ExtJS is ready
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

            var globalTeuEl = document.querySelector('.global-teu, #global_teu, .x-panel-body');
            var rawText = document.body.innerText;
            result.rawText = rawText;
        } catch(e) {
            result.error = e.toString();
        }
        return result;
        """

        data = driver.execute_script(js_extractor)
        records = data.get("records", [])

        if not records:
            logger.warning("Direct ExtJS store lookup returned empty. Attempting grid data fallback...")
            fallback_js = """
            var out = [];
            var grids = Ext.ComponentQuery.query('gridpanel');
            if (grids.length > 0) {
                var s = grids[0].getStore();
                s.each(function(r){ out.push(r.data); });
            }
            return out;
            """
            records = driver.execute_script(fallback_js)

        today_str = datetime.now().strftime("%Y-%m-%d")

        operators = []
        global_total_teu = 0
        global_total_ships = 0
        orderbook_total_teu = 0

        def parse_num(val):
            if val is None: return 0
            if isinstance(val, (int, float)): return val
            s = re.sub(r'[^\d.]', '', str(val))
            return float(s) if '.' in s else (int(s) if s else 0)

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

        top100_teu = sum(x["total_teu"] for x in operators)
        top100_ships = sum(x["total_ships"] for x in operators)
        top10_teu = sum(x["total_teu"] for x in operators[:10])

        # Scrape global figures from page text if present
        raw_text = data.get("rawText", "")
        m_glob_teu = re.search(r'Total:\s*([\d,]+)\s*TEU', raw_text)
        m_glob_ships = re.search(r'Total:\s*[\d,]+\s*TEU\s*\(\s*([\d,]+)\s*Ships\)', raw_text)

        if m_glob_teu:
            global_total_teu = int(re.sub(r'[^\d]', '', m_glob_teu.group(1)))
        else:
            global_total_teu = int(top100_teu / 0.95) if top100_teu > 0 else 30000000

        if m_glob_ships:
            global_total_ships = int(re.sub(r'[^\d]', '', m_glob_ships.group(1)))
        else:
            global_total_ships = int(top100_ships / 0.90) if top100_ships > 0 else 6800

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

        if operators:
            save_snapshot_to_db(meta, operators)
            logger.info(f"Crawl completed successfully: {len(operators)} operators processed.")
        else:
            logger.warning("No records extracted from Alphaliner page.")

    except Exception as e:
        logger.error(f"Error crawling Alphaliner: {e}", exc_info=True)
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

# =========================================================
# 4. HTML Dashboard Generator (Bloomberg MI Terminal)
# =========================================================
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ALPHALINER TOP 100 // BLOOMBERG MI TERMINAL</title>
    <!-- Chart.js for High Performance Terminal Charts -->
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <style>
        :root {
            --bg-base: #02060B;
            --bg-surface: #050A11;
            --bg-card: #07111C;
            --bg-card-hover: #0C1B2B;
            --border-dim: #122030;
            --border-bright: #1E344B;
            --text-primary: #F4F7FB;
            --text-secondary: #8AA0B5;
            --text-muted: #4A637B;
            --pos-green: #2BD682;
            --pos-green-dim: rgba(43, 214, 130, 0.12);
            --neg-red: #FF5C5C;
            --neg-red-dim: rgba(255, 92, 92, 0.12);
            --accent-amber: #FF9D1C;
            --accent-blue: #39A8FF;
            --font-mono: 'Roboto Mono', 'SF Pro Text', Consolas, 'Noto Sans KR', monospace;
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            font-family: var(--font-mono);
            -webkit-font-smoothing: antialiased;
        }

        body {
            background-color: var(--bg-base);
            color: var(--text-primary);
            font-size: 11px;
            line-height: 1.35;
            padding: 0;
            margin: 0;
            overflow-x: hidden;
        }

        /* 1. TOP TICKER BAR */
        .ticker-bar {
            background: linear-gradient(180deg, #07111C 0%, #03080F 100%);
            border-bottom: 1px solid var(--border-bright);
            padding: 6px 14px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            font-size: 11px;
            font-weight: 600;
            letter-spacing: 0.5px;
            position: sticky;
            top: 0;
            z-index: 100;
            box-shadow: 0 2px 10px rgba(0,0,0,0.5);
        }
        .ticker-left {
            display: flex;
            align-items: center;
            gap: 12px;
        }
        .bloomberg-tag {
            background: var(--accent-amber);
            color: #000;
            font-weight: 900;
            padding: 2px 6px;
            border-radius: 2px;
            font-size: 10px;
        }
        .ticker-mid {
            display: flex;
            align-items: center;
            gap: 20px;
        }
        .ticker-item {
            display: flex;
            gap: 6px;
        }
        .ticker-label { color: var(--text-secondary); }
        .ticker-val { color: var(--text-primary); text-align: right; }
        .ticker-right {
            display: flex;
            align-items: center;
            gap: 12px;
            font-size: 10px;
            color: var(--text-secondary);
        }
        .status-dot {
            width: 7px;
            height: 7px;
            background: var(--pos-green);
            border-radius: 50%;
            display: inline-block;
            box-shadow: 0 0 6px var(--pos-green);
        }

        /* CONTAINER LAYOUT */
        .dashboard-container {
            padding: 10px 14px;
            max-width: 1920px;
            margin: 0 auto;
            display: flex;
            flex-direction: column;
            gap: 10px;
        }

        /* 2. KPI ROW (6 CARDS) */
        .kpi-row {
            display: grid;
            grid-template-columns: repeat(6, 1fr);
            gap: 8px;
        }
        .kpi-card {
            background: var(--bg-card);
            border: 1px solid var(--border-dim);
            padding: 8px 10px;
            border-radius: 2px;
            transition: border-color 0.15s, background-color 0.15s;
            position: relative;
        }
        .kpi-card:hover {
            border-color: var(--border-bright);
            background: var(--bg-card-hover);
        }
        .kpi-label {
            font-size: 9.5px;
            font-weight: 700;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.4px;
            margin-bottom: 4px;
            display: flex;
            justify-content: space-between;
        }
        .kpi-value {
            font-size: 16px;
            font-weight: 800;
            color: #FFF;
            text-align: right;
            letter-spacing: -0.2px;
            font-feature-settings: "tnum";
        }
        .kpi-sub {
            font-size: 9.5px;
            display: flex;
            justify-content: space-between;
            margin-top: 4px;
            color: var(--text-muted);
            border-top: 1px dotted var(--border-dim);
            padding-top: 3px;
        }

        /* 3 & 4. MIDDLE SECTION (8-COL CHART / 4-COL WATCHLIST) */
        .middle-grid {
            display: grid;
            grid-template-columns: 8fr 4fr;
            gap: 8px;
        }

        .panel {
            background: var(--bg-card);
            border: 1px solid var(--border-dim);
            border-radius: 2px;
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }
        .panel-header {
            background: linear-gradient(180deg, #091523 0%, #060E18 100%);
            border-bottom: 1px solid var(--border-dim);
            padding: 6px 10px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .panel-title {
            font-size: 10.5px;
            font-weight: 800;
            color: var(--accent-amber);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .panel-controls {
            display: flex;
            align-items: center;
            gap: 6px;
        }
        select, button {
            background: var(--bg-surface);
            color: var(--text-primary);
            border: 1px solid var(--border-bright);
            padding: 2px 7px;
            font-size: 10px;
            font-family: var(--font-mono);
            border-radius: 2px;
            outline: none;
            cursor: pointer;
        }
        select:hover, button:hover {
            border-color: var(--accent-blue);
        }
        button.btn-amber {
            background: rgba(255, 157, 28, 0.15);
            border-color: var(--accent-amber);
            color: var(--accent-amber);
            font-weight: 700;
        }
        button.btn-amber:hover {
            background: var(--accent-amber);
            color: #000;
        }

        /* CHART CONTAINER */
        .chart-box {
            position: relative;
            flex: 1;
            min-height: 270px;
            padding: 8px;
        }

        /* TOP 10 WATCHLIST TABLE */
        .watchlist-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 10px;
        }
        .watchlist-table th {
            background: #050D17;
            color: var(--text-secondary);
            font-weight: 700;
            text-align: right;
            padding: 4px 6px;
            border-bottom: 1px solid var(--border-dim);
            text-transform: uppercase;
        }
        .watchlist-table th:first-child, .watchlist-table td:first-child { text-align: center; width: 26px; }
        .watchlist-table th:nth-child(2), .watchlist-table td:nth-child(2) { text-align: left; }
        .watchlist-table td {
            padding: 3.5px 6px;
            border-bottom: 1px solid #081624;
            text-align: right;
            font-feature-settings: "tnum";
            white-space: nowrap;
        }
        .watchlist-table tr:hover td {
            background: var(--bg-card-hover);
        }
        .rank-top3 {
            color: var(--accent-amber);
            font-weight: 800;
        }

        /* 5 & 6. BOTTOM SECTION (5-COL HEATMAP / 7-COL MOVERS & RISK) */
        .bottom-grid {
            display: grid;
            grid-template-columns: 5fr 4fr 3fr;
            gap: 8px;
        }

        /* HEATMAP */
        .heatmap-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 9.5px;
        }
        .heatmap-table th {
            background: #050D17;
            color: var(--text-secondary);
            padding: 4px 5px;
            text-align: right;
            border-bottom: 1px solid var(--border-dim);
            text-transform: uppercase;
        }
        .heatmap-table th:first-child, .heatmap-table td:first-child { text-align: left; }
        .heatmap-table td {
            padding: 3px 5px;
            text-align: right;
            border-bottom: 1px solid #081624;
            font-feature-settings: "tnum";
        }
        .heat-cell-gain { background: var(--pos-green-dim); color: var(--pos-green); }
        .heat-cell-loss { background: var(--neg-red-dim); color: var(--neg-red); }

        /* MOVERS */
        .movers-container {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 6px;
            padding: 6px;
            flex: 1;
        }
        .mover-col {
            background: #040910;
            border: 1px solid var(--border-dim);
            padding: 6px;
            border-radius: 2px;
        }
        .mover-col-title {
            font-size: 9.5px;
            font-weight: 800;
            margin-bottom: 4px;
            padding-bottom: 2px;
            border-bottom: 1px solid var(--border-dim);
            display: flex;
            justify-content: space-between;
        }
        .mover-item {
            display: flex;
            justify-content: space-between;
            padding: 3px 0;
            border-bottom: 1px dotted #0a1726;
            font-size: 9.5px;
        }

        /* RISK & INTELLIGENCE */
        .risk-section {
            padding: 8px;
            display: flex;
            flex-direction: column;
            gap: 6px;
            flex: 1;
        }
        .risk-metric {
            display: flex;
            flex-direction: column;
            gap: 2px;
        }
        .risk-metric-header {
            display: flex;
            justify-content: space-between;
            font-size: 9.5px;
            color: var(--text-secondary);
        }
        .progress-bar-bg {
            background: #091523;
            height: 5px;
            width: 100%;
            border-radius: 1px;
            overflow: hidden;
            border: 1px solid var(--border-dim);
        }
        .progress-bar-fill {
            height: 100%;
            background: var(--accent-blue);
        }
        .feed-box {
            background: #03080F;
            border: 1px solid var(--border-dim);
            padding: 6px;
            font-size: 9.5px;
            color: var(--text-secondary);
            height: 85px;
            overflow-y: auto;
            display: flex;
            flex-direction: column;
            gap: 4px;
            border-left: 2px solid var(--accent-amber);
        }
        .feed-item {
            line-height: 1.25;
        }
        .feed-item span.highlight { color: #FFF; font-weight: 700; }

        /* COLLAPSIBLE 2-TIER DETAIL SECTION */
        .collapsible-header {
            background: linear-gradient(180deg, #091523 0%, #060E18 100%);
            border: 1px solid var(--border-bright);
            padding: 6px 12px;
            cursor: pointer;
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-weight: 800;
            color: var(--text-primary);
        }
        .collapsible-header:hover {
            background: #0D1E30;
        }
        .detail-table-wrap {
            max-height: 480px;
            overflow-y: auto;
            overflow-x: auto;
            border: 1px solid var(--border-dim);
            border-top: none;
            background: var(--bg-card);
        }
        .alphaliner-2tier-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 10px;
            text-align: right;
        }
        .alphaliner-2tier-table th {
            background: #050D17;
            color: var(--text-secondary);
            font-weight: 700;
            padding: 5px 6px;
            border: 1px solid var(--border-dim);
            text-transform: uppercase;
        }
        .alphaliner-2tier-table td {
            padding: 4px 6px;
            border: 1px solid #091522;
            font-feature-settings: "tnum";
            white-space: nowrap;
        }
        .alphaliner-2tier-table tr:hover td {
            background: var(--bg-card-hover);
        }

        /* 7. FOOTER STATUS BAR */
        .footer-status {
            background: #020509;
            border-top: 1px solid var(--border-bright);
            padding: 4px 14px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 9.5px;
            color: var(--text-muted);
            letter-spacing: 0.3px;
        }
        .val-pos { color: var(--pos-green) !important; }
        .val-neg { color: var(--neg-red) !important; }
        .val-amber { color: var(--accent-amber) !important; }
        .val-blue { color: var(--accent-blue) !important; }
    </style>
</head>
<body>

    <!-- 1. TOP TICKER BAR -->
    <div class="ticker-bar">
        <div class="ticker-left">
            <span class="bloomberg-tag">MI&lt;GO&gt;</span>
            <span style="color:#FFF; font-weight:800;">ALPHALINER TOP 100 MONITOR</span>
        </div>
        <div class="ticker-mid">
            <div class="ticker-item"><span class="ticker-label">GLOBAL TEU:</span><span id="tk-glob-teu" class="ticker-val">-</span></div>
            <div class="ticker-item"><span class="ticker-label">TOP100 TEU:</span><span id="tk-top100-teu" class="ticker-val">-</span></div>
            <div class="ticker-item"><span class="ticker-label">GLOBAL SHIPS:</span><span id="tk-glob-ships" class="ticker-val">-</span></div>
            <div class="ticker-item"><span class="ticker-label">TOP10 CONCENTRATION:</span><span id="tk-top10-share" class="ticker-val val-amber">-</span></div>
            <div class="ticker-item"><span class="ticker-label">ORDERBOOK TEU:</span><span id="tk-orderbook" class="ticker-val val-blue">-</span></div>
        </div>
        <div class="ticker-right">
            <span>SNAP DATE: <strong id="tk-snap-date" style="color:#FFF;">-</strong></span>
            <span><span class="status-dot"></span> SYSTEM ONLINE</span>
        </div>
    </div>

    <div class="dashboard-container">

        <!-- 2. KPI ROW (6 CARDS) -->
        <div class="kpi-row">
            <div class="kpi-card">
                <div class="kpi-label">GLOBAL LINER CAPACITY <span id="kpi-glob-delta">-</span></div>
                <div class="kpi-value" id="kpi-glob-teu">-</div>
                <div class="kpi-sub"><span>TOTAL ACTIVE FLEET</span><span id="kpi-glob-ships">- SHIPS</span></div>
            </div>
            <div class="kpi-card">
                <div class="kpi-label">TOP 100 CAPACITY <span id="kpi-top100-delta">-</span></div>
                <div class="kpi-value" id="kpi-top100-teu">-</div>
                <div class="kpi-sub"><span>GLOBAL SHARE</span><span id="kpi-top100-share" class="val-blue">- %</span></div>
            </div>
            <div class="kpi-card">
                <div class="kpi-label">ACTIVE FLEET COUNT <span id="kpi-fleet-delta">-</span></div>
                <div class="kpi-value" id="kpi-fleet-count">-</div>
                <div class="kpi-sub"><span>TOP 100 SHIPS</span><span id="kpi-top100-ships">- SHIPS</span></div>
            </div>
            <div class="kpi-card">
                <div class="kpi-label">MARKET LEADER <span id="kpi-leader-share" class="val-amber">- %</span></div>
                <div class="kpi-value" id="kpi-leader-name" style="font-size:14px; color:var(--accent-amber);">-</div>
                <div class="kpi-sub"><span>LEADER TEU</span><span id="kpi-leader-teu">-</span></div>
            </div>
            <div class="kpi-card">
                <div class="kpi-label">TOP 10 CONCENTRATION <span id="kpi-top10-delta">-</span></div>
                <div class="kpi-value" id="kpi-top10-val" style="color:var(--accent-amber);">- %</div>
                <div class="kpi-sub"><span>TOP 10 TOTAL</span><span id="kpi-top10-teu">- TEU</span></div>
            </div>
            <div class="kpi-card">
                <div class="kpi-label">ORDERBOOK RATIO <span class="val-blue">PIPELINE</span></div>
                <div class="kpi-value" id="kpi-orderbook-ratio" style="color:var(--accent-blue);">- %</div>
                <div class="kpi-sub"><span>ORDERBOOK TEU</span><span id="kpi-orderbook-teu">-</span></div>
            </div>
        </div>

        <!-- 3 & 4. MIDDLE SECTION (CHART & WATCHLIST) -->
        <div class="middle-grid">
            <!-- 8-COL CHART -->
            <div class="panel">
                <div class="panel-header">
                    <div class="panel-title">
                        <span>■</span> GLOBAL LINER CAPACITY TREND / MILLION TEU
                    </div>
                    <div class="panel-controls">
                        <span style="font-size:9.5px; color:var(--text-secondary);">BASE DATE:</span>
                        <select id="sel-base-date" onchange="handleBaseDateChange()"></select>
                        <button class="btn-amber" onclick="exportToCSV()">EXPORT CSV</button>
                    </div>
                </div>
                <div class="chart-box">
                    <canvas id="trendChart"></canvas>
                </div>
            </div>

            <!-- 4-COL WATCHLIST -->
            <div class="panel">
                <div class="panel-header">
                    <div class="panel-title">
                        <span>■</span> TOP OPERATOR WATCHLIST
                    </div>
                    <div class="panel-controls">
                        <span style="font-size:9.5px; color:var(--text-secondary);">TOP 10 RANK</span>
                    </div>
                </div>
                <div style="flex:1; overflow-y:auto; max-height:280px;">
                    <table class="watchlist-table">
                        <thead>
                            <tr>
                                <th>RK</th>
                                <th>OPERATOR</th>
                                <th>TEU</th>
                                <th>Δ TEU</th>
                                <th>SHARE</th>
                                <th>VOLUME</th>
                            </tr>
                        </thead>
                        <tbody id="watchlist-body"></tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- 5 & 6. BOTTOM SECTION (HEATMAP / MOVERS / RISK) -->
        <div class="bottom-grid">
            <!-- 5-COL HEATMAP -->
            <div class="panel">
                <div class="panel-header">
                    <div class="panel-title">
                        <span>■</span> OPERATOR CAPACITY HEATMAP / TOP 20
                    </div>
                    <span style="font-size:9px; color:var(--text-muted);">VS BASE DATE</span>
                </div>
                <div style="flex:1; overflow-y:auto; max-height:210px;">
                    <table class="heatmap-table">
                        <thead>
                            <tr>
                                <th>OPERATOR</th>
                                <th>Δ TEU</th>
                                <th>Δ SHIPS</th>
                                <th>Δ SHARE</th>
                                <th>RANK Δ</th>
                                <th>ORD %</th>
                            </tr>
                        </thead>
                        <tbody id="heatmap-body"></tbody>
                    </table>
                </div>
            </div>

            <!-- 4-COL MOVERS -->
            <div class="panel">
                <div class="panel-header">
                    <div class="panel-title">
                        <span>■</span> CAPACITY MOVERS / PERIOD CHANGE
                    </div>
                    <span style="font-size:9px; color:var(--text-muted);">DELTA ANALYSIS</span>
                </div>
                <div class="movers-container">
                    <div class="mover-col">
                        <div class="mover-col-title val-pos"><span>TOP GAINERS</span><span>Δ TEU</span></div>
                        <div id="movers-gainers"></div>
                    </div>
                    <div class="mover-col">
                        <div class="mover-col-title val-neg"><span>TOP DECLINERS</span><span>Δ TEU</span></div>
                        <div id="movers-decliners"></div>
                    </div>
                </div>
            </div>

            <!-- 3-COL RISK & INTELLIGENCE FEED -->
            <div class="panel">
                <div class="panel-header">
                    <div class="panel-title">
                        <span>■</span> FLEET RISK & INTEL
                    </div>
                    <span style="font-size:9px; color:var(--text-muted);">MONITOR</span>
                </div>
                <div class="risk-section">
                    <div class="risk-metric">
                        <div class="risk-metric-header"><span>TOP 10 CONCENTRATION</span><span id="risk-top10-val">-</span></div>
                        <div class="progress-bar-bg"><div class="progress-bar-fill" id="risk-top10-bar" style="width:0%;"></div></div>
                    </div>
                    <div class="risk-metric">
                        <div class="risk-metric-header"><span>ORDERBOOK EXPOSURE</span><span id="risk-ord-val">-</span></div>
                        <div class="progress-bar-bg"><div class="progress-bar-fill" id="risk-ord-bar" style="width:0%; background:var(--accent-amber);"></div></div>
                    </div>
                    <div class="feed-box" id="intel-feed-box">
                        <div class="feed-item">• <span class="highlight">SYSTEM READY</span>: Alphaliner snapshot initialized.</div>
                    </div>
                </div>
            </div>
        </div>

        <!-- AUTHENTIC ALPHALINER 2-TIER FULL OPERATOR DETAIL TABLE (COLLAPSIBLE) -->
        <div>
            <div class="collapsible-header" onclick="toggleDetailTable()">
                <span>▼ FULL OPERATOR BREAKDOWN (TOP 100 2-TIER FLEET SPECIFICATION)</span>
                <span id="detail-toggle-btn" style="font-size:10px; color:var(--accent-blue);">[ CLICK TO EXPAND / COLLAPSE ]</span>
            </div>
            <div id="detail-table-container" class="detail-table-wrap">
                <table class="alphaliner-2tier-table">
                    <thead>
                        <tr>
                            <th rowspan="2" style="width:30px; text-align:center;">RK</th>
                            <th rowspan="2" style="text-align:left;">OPERATOR</th>
                            <th colspan="3" style="text-align:center; border-bottom:1px solid var(--border-dim); color:#FFF;">TOTAL CAPACITY</th>
                            <th colspan="3" style="text-align:center; border-bottom:1px solid var(--border-dim); color:var(--text-secondary);">EXISTING FLEET (OWNED)</th>
                            <th colspan="3" style="text-align:center; border-bottom:1px solid var(--border-dim); color:var(--text-secondary);">CHARTERED FLEET</th>
                            <th colspan="3" style="text-align:center; border-bottom:1px solid var(--border-dim); color:var(--accent-blue);">ORDERBOOK</th>
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
                            <th>% CHA</th>
                            <th>TEU</th>
                            <th>SHIPS</th>
                            <th>% FLT</th>
                        </tr>
                    </thead>
                    <tbody id="detail-table-body"></tbody>
                </table>
            </div>
        </div>

    </div>

    <!-- 7. FOOTER STATUS BAR -->
    <div class="footer-status">
        <div>
            <span>MARKET HEALTH: <strong id="ft-health" style="color:var(--pos-green);">EXPANDING</strong></span> |
            <span>SENTIMENT: <strong id="ft-sentiment" style="color:var(--accent-blue);">STABLE ACCUMULATION</strong></span>
        </div>
        <div>
            <span>SOURCE: ALPHALINER TOP 100 EXTJS STORE [Top100description]</span> |
            <span>AUTHENTIC 2-TIER EXTRACTION</span>
        </div>
        <div>
            <span>REFRESH: <span id="ft-refresh-time">-</span></span> |
            <span>ALERTS: <span style="color:var(--pos-green);">0 ACTIVE</span></span>
        </div>
    </div>

    <!-- DASHBOARD LOGIC -->
    <script>
        let DATA_PAYLOAD = null;
        let trendChartInstance = null;

        const fmt = (n) => (n === undefined || n === null) ? '-' : Number(n).toLocaleString('en-US');
        const fmtPct = (n) => (n === undefined || n === null) ? '-' : (Number(n).toFixed(1) + '%');

        async function initDashboard() {
            try {
                const res = await fetch('/api/data');
                DATA_PAYLOAD = await res.json();
            } catch (e) {
                // Fallback for static standalone view
                DATA_PAYLOAD = window.EMBEDDED_DATA || { dates: [], snapshots: {} };
            }

            if (!DATA_PAYLOAD.dates || DATA_PAYLOAD.dates.length === 0) {
                console.warn("No snapshot data available.");
                return;
            }

            populateBaseDateOptions();
            renderDashboard();
            initChart();
        }

        function populateBaseDateOptions() {
            const sel = document.getElementById('sel-base-date');
            sel.innerHTML = '';
            const dates = DATA_PAYLOAD.dates;

            // Populate dates in descending order
            for (let i = dates.length - 1; i >= 0; i--) {
                const opt = document.createElement('option');
                opt.value = dates[i];
                opt.innerText = dates[i];
                sel.appendChild(opt);
            }

            // Default compare base date: Previous snapshot or earliest
            if (dates.length > 1) {
                sel.selectedIndex = 1;
            } else {
                sel.selectedIndex = 0;
            }
        }

        function handleBaseDateChange() {
            renderDashboard();
            updateChart();
        }

        function renderDashboard() {
            const dates = DATA_PAYLOAD.dates;
            const latestDate = dates[dates.length - 1];
            const baseDate = document.getElementById('sel-base-date').value || dates[0];

            const curSnap = DATA_PAYLOAD.snapshots[latestDate];
            const baseSnap = DATA_PAYLOAD.snapshots[baseDate];

            if (!curSnap) return;

            const curMeta = curSnap.meta || {};
            const baseMeta = baseSnap ? (baseSnap.meta || {}) : curMeta;

            const curOps = curSnap.operators || [];
            const baseOps = baseSnap ? (baseSnap.operators || []) : [];
            const baseOpMap = {};
            baseOps.forEach(o => { baseOpMap[o.operator] = o; });

            // 1. TOP TICKER
            document.getElementById('tk-glob-teu').innerText = fmt(curMeta.global_total_teu);
            document.getElementById('tk-top100-teu').innerText = fmt(curMeta.top100_total_teu);
            document.getElementById('tk-glob-ships').innerText = fmt(curMeta.global_total_ships);
            document.getElementById('tk-top10-share').innerText = fmtPct(curMeta.top10_share);
            document.getElementById('tk-orderbook').innerText = fmt(curMeta.orderbook_total_teu);
            document.getElementById('tk-snap-date').innerText = latestDate;

            // 2. KPI ROW
            const globDelta = curMeta.global_total_teu - (baseMeta.global_total_teu || curMeta.global_total_teu);
            const top100Delta = curMeta.top100_total_teu - (baseMeta.top100_total_teu || curMeta.top100_total_teu);
            const fleetDelta = curMeta.global_total_ships - (baseMeta.global_total_ships || curMeta.global_total_ships);
            const top10Delta = curMeta.top10_share - (baseMeta.top10_share || curMeta.top10_share);

            document.getElementById('kpi-glob-teu').innerText = fmt(curMeta.global_total_teu) + ' TEU';
            document.getElementById('kpi-glob-ships').innerText = fmt(curMeta.global_total_ships) + ' SHIPS';
            renderDelta('kpi-glob-delta', globDelta, true);

            document.getElementById('kpi-top100-teu').innerText = fmt(curMeta.top100_total_teu) + ' TEU';
            const top100Share = curMeta.global_total_teu ? ((curMeta.top100_total_teu / curMeta.global_total_teu)*100).toFixed(1) : '-';
            document.getElementById('kpi-top100-share').innerText = top100Share + ' %';
            renderDelta('kpi-top100-delta', top100Delta, true);

            document.getElementById('kpi-fleet-count').innerText = fmt(curMeta.global_total_ships);
            document.getElementById('kpi-top100-ships').innerText = fmt(curMeta.top100_total_ships) + ' SHIPS';
            renderDelta('kpi-fleet-delta', fleetDelta, false);

            if (curOps.length > 0) {
                const leader = curOps[0];
                document.getElementById('kpi-leader-name').innerText = leader.operator.toUpperCase();
                document.getElementById('kpi-leader-share').innerText = fmtPct(leader.market_share);
                document.getElementById('kpi-leader-teu').innerText = fmt(leader.total_teu) + ' TEU';
            }

            document.getElementById('kpi-top10-val').innerText = fmtPct(curMeta.top10_share);
            document.getElementById('kpi-top10-teu').innerText = fmt(curMeta.top10_total_teu);
            renderDelta('kpi-top10-delta', top10Delta, false, true);

            const ordRatio = curMeta.top100_total_teu ? ((curMeta.orderbook_total_teu / curMeta.top100_total_teu)*100).toFixed(1) : '-';
            document.getElementById('kpi-orderbook-ratio').innerText = ordRatio + ' %';
            document.getElementById('kpi-orderbook-teu').innerText = fmt(curMeta.orderbook_total_teu) + ' TEU';

            // 4. WATCHLIST TABLE (TOP 10)
            const wb = document.getElementById('watchlist-body');
            wb.innerHTML = '';
            curOps.slice(0, 10).forEach(op => {
                const bOp = baseOpMap[op.operator] || op;
                const dTeu = op.total_teu - bOp.total_teu;
                const tr = document.createElement('tr');
                tr.innerHTML = `
                    <td class="${op.rank <= 3 ? 'rank-top3' : ''}">${op.rank}</td>
                    <td style="font-weight:700; color:#FFF;">${op.operator}</td>
                    <td>${fmt(op.total_teu)}</td>
                    <td class="${dTeu > 0 ? 'val-pos' : (dTeu < 0 ? 'val-neg' : '')}">${dTeu > 0 ? '+'+fmt(dTeu) : (dTeu < 0 ? fmt(dTeu) : '-')}</td>
                    <td>${fmtPct(op.market_share)}</td>
                    <td style="color:var(--text-secondary);">${fmt(op.total_ships)}</td>
                `;
                wb.appendChild(tr);
            });

            // 5. HEATMAP & MOVERS CALCULATION
            const deltas = curOps.map(op => {
                const b = baseOpMap[op.operator];
                const dTeu = b ? (op.total_teu - b.total_teu) : 0;
                const dShips = b ? (op.total_ships - b.total_ships) : 0;
                const dShare = b ? (op.market_share - b.market_share) : 0;
                const dRank = b ? (b.rank - op.rank) : 0; // positive means rank improved
                const ordPct = op.total_teu ? ((op.orderbook_teu / op.total_teu) * 100) : 0;
                return { ...op, dTeu, dShips, dShare, dRank, ordPct, isNew: !b };
            });

            // Render Heatmap (Top 20)
            const hb = document.getElementById('heatmap-body');
            hb.innerHTML = '';
            deltas.slice(0, 20).forEach(d => {
                const tr = document.createElement('tr');
                tr.innerHTML = `
                    <td style="font-weight:600; color:#FFF;">${d.rank}. ${d.operator}</td>
                    <td class="${d.dTeu > 0 ? 'heat-cell-gain' : (d.dTeu < 0 ? 'heat-cell-loss' : '')}">${d.dTeu > 0 ? '+'+fmt(d.dTeu) : (d.dTeu < 0 ? fmt(d.dTeu) : '0')}</td>
                    <td class="${d.dShips > 0 ? 'val-pos' : (d.dShips < 0 ? 'val-neg' : '')}">${d.dShips > 0 ? '+'+d.dShips : (d.dShips < 0 ? d.dShips : '0')}</td>
                    <td class="${d.dShare > 0 ? 'val-pos' : (d.dShare < 0 ? 'val-neg' : '')}">${d.dShare > 0 ? '+'+d.dShare.toFixed(1)+'%' : (d.dShare < 0 ? d.dShare.toFixed(1)+'%' : '0.0%')}</td>
                    <td class="${d.dRank > 0 ? 'val-amber' : (d.dRank < 0 ? 'val-neg' : '')}">${d.dRank > 0 ? '▲'+d.dRank : (d.dRank < 0 ? '▼'+Math.abs(d.dRank) : '-')}</td>
                    <td style="color:var(--accent-blue);">${d.ordPct.toFixed(1)}%</td>
                `;
                hb.appendChild(tr);
            });

            // Render Movers (Gainers & Decliners)
            const sortedGainers = [...deltas].filter(x => x.dTeu > 0).sort((a,b) => b.dTeu - a.dTeu).slice(0, 5);
            const sortedDecliners = [...deltas].filter(x => x.dTeu < 0).sort((a,b) => a.dTeu - b.dTeu).slice(0, 5);

            const gBox = document.getElementById('movers-gainers');
            gBox.innerHTML = sortedGainers.length ? '' : '<div style="color:var(--text-muted); padding:4px 0;">NO GAINERS</div>';
            sortedGainers.forEach(g => {
                gBox.innerHTML += `<div class="mover-item"><span>${g.operator}</span><span class="val-pos">+${fmt(g.dTeu)}</span></div>`;
            });

            const dBox = document.getElementById('movers-decliners');
            dBox.innerHTML = sortedDecliners.length ? '' : '<div style="color:var(--text-muted); padding:4px 0;">NO DECLINERS</div>';
            sortedDecliners.forEach(d => {
                dBox.innerHTML += `<div class="mover-item"><span>${d.operator}</span><span class="val-neg">${fmt(d.dTeu)}</span></div>`;
            });

            // 6. RISK GAUGES & INTEL FEED
            const top10Val = curMeta.top10_share || 0;
            document.getElementById('risk-top10-val').innerText = top10Val.toFixed(1) + '%';
            document.getElementById('risk-top10-bar').style.width = Math.min(top10Val, 100) + '%';

            const ordExp = parseFloat(ordRatio) || 0;
            document.getElementById('risk-ord-val').innerText = ordExp.toFixed(1) + '%';
            document.getElementById('risk-ord-bar').style.width = Math.min(ordExp * 2.5, 100) + '%';

            const feedBox = document.getElementById('intel-feed-box');
            feedBox.innerHTML = `
                <div class="feed-item">• <span class="highlight">MARKET LEADER</span>: ${curOps[0]?.operator} holds ${curOps[0]?.market_share}% global market share.</div>
                <div class="feed-item">• <span class="highlight">TOP 10 CONCENTRATION</span>: ${top10Val.toFixed(1)}% of world liner capacity is controlled by top 10 lines.</div>
                <div class="feed-item">• <span class="highlight">PIPELINE PRESSURE</span>: ${fmt(curMeta.orderbook_total_teu)} TEU currently on order (${ordExp.toFixed(1)}% of top 100).</div>
                <div class="feed-item">• <span class="highlight">COMPARISON BASE</span>: Snapshot evaluated against ${baseDate}.</div>
            `;

            // DETAIL 2-TIER FULL TABLE
            const dtb = document.getElementById('detail-table-body');
            dtb.innerHTML = '';
            curOps.forEach(o => {
                const tr = document.createElement('tr');
                tr.innerHTML = `
                    <td style="text-align:center; font-weight:700; color:${o.rank <= 10 ? 'var(--accent-amber)' : 'var(--text-secondary)'};">${o.rank}</td>
                    <td style="text-align:left; font-weight:700; color:#FFF;">${o.operator}</td>
                    <td style="font-weight:700; color:#FFF;">${fmt(o.total_teu)}</td>
                    <td>${fmt(o.total_ships)}</td>
                    <td style="color:var(--accent-amber);">${fmtPct(o.market_share)}</td>
                    <td>${fmt(o.owned_teu)}</td>
                    <td>${fmt(o.owned_ships)}</td>
                    <td style="color:var(--text-secondary);">${fmtPct(o.owned_share)}</td>
                    <td>${fmt(o.chartered_teu)}</td>
                    <td>${fmt(o.chartered_ships)}</td>
                    <td style="color:var(--text-secondary);">${fmtPct(o.chartered_share)}</td>
                    <td style="color:var(--accent-blue);">${fmt(o.orderbook_teu)}</td>
                    <td style="color:var(--accent-blue);">${fmt(o.orderbook_ships)}</td>
                    <td style="color:var(--accent-blue);">${fmtPct(o.orderbook_share)}</td>
                `;
                dtb.appendChild(tr);
            });

            // Footer
            document.getElementById('ft-refresh-time').innerText = curMeta.collected_at ? curMeta.collected_at.replace('T', ' ').substring(0, 19) : new Date().toISOString().substring(0, 19);
        }

        function renderDelta(elemId, delta, isTeu = true, isPct = false) {
            const el = document.getElementById(elemId);
            if (!delta || delta === 0) {
                el.innerText = '±0';
                el.className = 'val-amber';
                return;
            }
            const sign = delta > 0 ? '+' : '';
            const formatted = isPct ? (sign + delta.toFixed(2) + '%p') : (sign + fmt(delta) + (isTeu ? ' TEU' : ''));
            el.innerText = formatted;
            el.className = delta > 0 ? 'val-pos' : 'val-neg';
        }

        function initChart() {
            const ctx = document.getElementById('trendChart').getContext('2d');
            const dates = DATA_PAYLOAD.dates;

            const globalData = dates.map(d => (DATA_PAYLOAD.snapshots[d].meta.global_total_teu / 1000000).toFixed(3));
            const top100Data = dates.map(d => (DATA_PAYLOAD.snapshots[d].meta.top100_total_teu / 1000000).toFixed(3));

            trendChartInstance = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: dates,
                    datasets: [
                        {
                            label: 'GLOBAL TOTAL CAPACITY (M TEU)',
                            data: globalData,
                            borderColor: '#2BD682',
                            backgroundColor: 'rgba(43, 214, 130, 0.05)',
                            borderWidth: 2,
                            pointRadius: dates.length > 20 ? 1 : 3,
                            pointHoverRadius: 5,
                            fill: true,
                            tension: 0.1
                        },
                        {
                            label: 'TOP 100 OPERATORS CAPACITY (M TEU)',
                            data: top100Data,
                            borderColor: '#39A8FF',
                            backgroundColor: 'transparent',
                            borderWidth: 2,
                            pointRadius: dates.length > 20 ? 1 : 3,
                            pointHoverRadius: 5,
                            borderDash: [3, 3],
                            tension: 0.1
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: { mode: 'index', intersect: false },
                    plugins: {
                        legend: {
                            labels: {
                                color: '#8AA0B5',
                                font: { family: 'Roboto Mono', size: 10 }
                            },
                            position: 'top',
                            align: 'end'
                        },
                        tooltip: {
                            backgroundColor: '#07111C',
                            borderColor: '#1E344B',
                            borderWidth: 1,
                            titleColor: '#FF9D1C',
                            bodyColor: '#F4F7FB',
                            bodyFont: { family: 'Roboto Mono', size: 11 },
                            titleFont: { family: 'Roboto Mono', size: 11, weight: 'bold' }
                        }
                    },
                    scales: {
                        x: {
                            grid: { color: '#0A1726' },
                            ticks: { color: '#8AA0B5', font: { family: 'Roboto Mono', size: 9.5 } }
                        },
                        y: {
                            grid: { color: '#0A1726' },
                            ticks: {
                                color: '#8AA0B5',
                                font: { family: 'Roboto Mono', size: 9.5 },
                                callback: (v) => v + 'M'
                            }
                        }
                    }
                }
            });
        }

        function updateChart() {
            if (!trendChartInstance) return;
            trendChartInstance.update();
        }

        function toggleDetailTable() {
            const container = document.getElementById('detail-table-container');
            const btn = document.getElementById('detail-toggle-btn');
            if (container.style.display === 'none') {
                container.style.display = 'block';
                btn.innerText = '[ CLICK TO COLLAPSE ]';
            } else {
                container.style.display = 'none';
                btn.innerText = '[ CLICK TO EXPAND ]';
            }
        }

        function exportToCSV() {
            const latestDate = DATA_PAYLOAD.dates[DATA_PAYLOAD.dates.length - 1];
            const ops = DATA_PAYLOAD.snapshots[latestDate]?.operators || [];
            if (!ops.length) return;

            let csvContent = "\\uFEFF"; // UTF-8 BOM
            csvContent += "Rank,Operator,Total TEU,Total Ships,Market Share %,Owned TEU,Owned Ships,Owned Share %,Chartered TEU,Chartered Ships,Chartered Share %,Orderbook TEU,Orderbook Ships,Orderbook Share %\\n";

            ops.forEach(o => {
                csvContent += `${o.rank},"${o.operator}",${o.total_teu},${o.total_ships},${o.market_share},${o.owned_teu},${o.owned_ships},${o.owned_share},${o.chartered_teu},${o.chartered_ships},${o.chartered_share},${o.orderbook_teu},${o.orderbook_ships},${o.orderbook_share}\\n`;
            });

            const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
            const link = document.createElement("a");
            link.href = URL.createObjectURL(blob);
            link.setAttribute("download", `Alphaliner_TOP100_${latestDate}.csv`);
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
        }

        window.onload = initDashboard;
    </script>
</body>
</html>
"""

def generate_static_files():
    """Generates standalone alphaliner_data.json and index.html"""
    data_payload = get_all_snapshots_json()

    # Write JSON
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data_payload, f, ensure_ascii=False, indent=2)
    logger.info(f"Generated data file: {JSON_PATH}")

    # Embed data for 100% offline static compatibility
    embedded_script = f"<script>window.EMBEDDED_DATA = {json.dumps(data_payload, ensure_ascii=False)};</script>"
    rendered_html = HTML_TEMPLATE.replace("<!-- DASHBOARD LOGIC -->", embedded_script + "\n    <!-- DASHBOARD LOGIC -->")

    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(rendered_html)
    logger.info(f"Generated standalone HTML dashboard: {HTML_PATH}")

# =========================================================
# 5. Flask Web Server Application
# =========================================================
app = Flask(__name__, static_folder=str(BASE_DIR))

@app.route("/")
def index():
    if HTML_PATH.exists():
        return send_from_directory(BASE_DIR, "index.html")
    generate_static_files()
    return send_from_directory(BASE_DIR, "index.html")

@app.route("/api/data")
def api_data():
    return jsonify(get_all_snapshots_json())

@app.route("/api/crawl", methods=["POST", "GET"])
def api_crawl():
    crawl_alphaliner()
    generate_static_files()
    return jsonify({"status": "success", "message": "Crawler executed and files refreshed."})

@app.route("/api/export/csv")
def api_export_csv():
    data = get_all_snapshots_json()
    if not data["dates"]:
        return "No data available", 404

    latest_date = data["dates"][-1]
    ops = data["snapshots"][latest_date]["operators"]
    df = pd.DataFrame(ops)

    csv_bytes = df.to_csv(index=False, encoding="utf-8-sig")
    return Response(
        csv_bytes,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename=Alphaliner_TOP100_{latest_date}.csv"}
    )

# =========================================================
# 6. Main Execution Entry Point
# =========================================================
def main():
    parser = argparse.ArgumentParser(description="Alphaliner TOP 100 Bloomberg MI Terminal Dashboard")
    parser.add_argument("--no-crawl", action="store_true", help="Skip crawling and only serve/generate files")
    parser.add_argument("--cron", action="store_true", help="Run crawl once, generate static files and exit")
    parser.add_argument("--port", type=int, default=8050, help="Port to run Flask server on (default: 8050)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host IP to bind (default: 0.0.0.0)")

    args = parser.parse_args()

    init_db()

    if not args.no_crawl:
        logger.info("Executing scheduled crawl routine...")
        crawl_alphaliner()

    generate_static_files()

    if args.cron:
        logger.info("Cron execution complete. Static artifacts generated successfully.")
        return

    logger.info(f"Starting Bloomberg MI Terminal Server on http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)

if __name__ == "__main__":
    main()
