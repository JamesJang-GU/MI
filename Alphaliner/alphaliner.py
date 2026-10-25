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
from flask import Flask, Response, request, send_from_directory
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
# 1. Basic Configuration & Environment Setup
# =========================================================
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "alphaliner_history.db"
JSON_PATH = BASE_DIR / "alphaliner_data.json"
HTML_PATH = BASE_DIR / "index.html"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("AlphalinerCrawler")

db_lock = Lock()
app = Flask(__name__, static_folder=str(BASE_DIR))

# =========================================================
# 2. SQLite Database Schema & Migration
# =========================================================
def init_db(db_file: Path = DB_PATH):
    """
    Initializes the SQLite schema and performs auto-migration for legacy databases.
    """
    with db_lock:
        conn = sqlite3.connect(str(db_file))
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS snapshot_meta (
                snapshot_date TEXT PRIMARY KEY,
                created_at TEXT,
                global_teu INTEGER,
                global_ships INTEGER,
                top100_teu INTEGER,
                top100_ships INTEGER,
                notes TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS operator_snapshots (
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
                PRIMARY KEY (snapshot_date, rank),
                FOREIGN KEY (snapshot_date) REFERENCES snapshot_meta(snapshot_date)
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_operator_date_rank
            ON operator_snapshots (snapshot_date, rank)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_operator_name
            ON operator_snapshots (operator_name)
        """)

        # Auto-migration: Check missing columns in existing snapshot_meta
        cursor.execute("PRAGMA table_info(snapshot_meta)")
        existing_cols = {row[1] for row in cursor.fetchall()}
        required_cols = {
            "created_at": "TEXT",
            "global_teu": "INTEGER",
            "global_ships": "INTEGER",
            "top100_teu": "INTEGER",
            "top100_ships": "INTEGER",
            "notes": "TEXT"
        }
        for col_name, col_type in required_cols.items():
            if col_name not in existing_cols:
                cursor.execute(f"ALTER TABLE snapshot_meta ADD COLUMN {col_name} {col_type}")
                logger.info(f"[DB MIGRATION] Added missing column '{col_name}' to snapshot_meta.")

        conn.commit()
        conn.close()

# =========================================================
# 3. Crawler Core Logic (ExtJS Direct Extraction)
# =========================================================
def get_chrome_driver():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

    if HAS_WEBDRIVER_MANAGER:
        try:
            service = Service(ChromeDriverManager().install())
            return webdriver.Chrome(service=service, options=options)
        except Exception as e:
            logger.warning(f"ChromeDriverManager initialization failed: {e}. Falling back to default.")

    return webdriver.Chrome(options=options)

def fetch_alphaliner_data():
    url = "https://alphaliner.axsmarine.com/PublicTop100/"
    driver = None
    logger.info(f"Navigating to {url}...")

    try:
        driver = get_chrome_driver()
        driver.set_page_load_timeout(45)
        driver.get(url)

        wait = WebDriverWait(driver, 25)
        wait.until(lambda d: d.execute_script("return typeof Ext !== 'undefined' && Ext.isReady === true;"))
        logger.info("ExtJS runtime detected. Waiting for data store population...")

        store_script = """
            var targetStore = null;
            if (typeof Ext !== 'undefined' && Ext.data && Ext.data.StoreManager) {
                targetStore = Ext.data.StoreManager.lookup('Top100description');
                if (!targetStore || targetStore.getCount() === 0) {
                    Ext.data.StoreManager.each(function(store) {
                        if (store.getCount() >= 100) {
                            targetStore = store;
                            return false;
                        }
                    });
                }
            }
            if (targetStore && targetStore.getCount() > 0) {
                var records = [];
                targetStore.each(function(rec) {
                    records.push(rec.data);
                });
                return {
                    success: true,
                    count: records.length,
                    data: records
                };
            }
            return { success: false, count: 0, data: [] };
        """

        raw_result = None
        for attempt in range(1, 13):
            time.sleep(1.5)
            res = driver.execute_script(store_script)
            if res and res.get("success") and res.get("count", 0) >= 80:
                raw_result = res
                logger.info(f"Extracted {res['count']} operator records from ExtJS Store on attempt {attempt}.")
                break
            logger.info(f"Awaiting ExtJS Store hydration... (Attempt {attempt}/12)")

        if not raw_result or not raw_result.get("data"):
            raise ValueError("Failed to retrieve valid operator records from ExtJS StoreManager.")

        # Harvest global total figures from DOM
        global_summary = {"global_teu": None, "global_ships": None}
        try:
            body_text = driver.execute_script("return document.body.innerText;") or ""
            m = re.search(r"Total\s*:\s*([\d,]+)\s*TEU\s*\(([\d,]+)\s*ships?\)", body_text, re.IGNORECASE)
            if m:
                global_summary["global_teu"] = int(m.group(1).replace(",", ""))
                global_summary["global_ships"] = int(m.group(2).replace(",", ""))
                logger.info(f"Harvested official Global Total: {global_summary['global_teu']:,} TEU across {global_summary['global_ships']:,} ships.")
        except Exception as e:
            logger.warning(f"Could not parse official Global Total from DOM: {e}")

        return {
            "records": raw_result["data"],
            "global_summary": global_summary
        }

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

def parse_int(val):
    if val is None:
        return 0
    if isinstance(val, (int, float)):
        return int(val)
    s = re.sub(r"[^\d\-]", "", str(val))
    return int(s) if s and s != "-" else 0

def parse_float(val):
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = re.sub(r"[^\d\.\-]", "", str(val))
    return float(s) if s and s != "-" else 0.0

def get_field_val(record: dict, aliases: list, default=None):
    """Case-insensitive and multi-alias dictionary field search."""
    normalized = {k.lower().replace("_", ""): v for k, v in record.items()}
    for alias in aliases:
        norm_alias = alias.lower().replace("_", "")
        if norm_alias in normalized and normalized[norm_alias] is not None:
            return normalized[norm_alias]
    return default

def process_and_persist(raw_data, db_file: Path = DB_PATH):
    records = raw_data.get("records", [])
    summary = raw_data.get("global_summary", {})
    if not records:
        raise ValueError("Cannot persist empty record set.")

    snapshot_date = datetime.now().strftime("%Y-%m-%d")
    now_iso = datetime.now().isoformat()

    operators = []
    for idx, r in enumerate(records):
        # Flexible multi-alias field extraction
        rank_val = get_field_val(r, ["Rank", "rank", "rnk", "pos", "position", "id", "rowNum"])
        rank = parse_int(rank_val) if rank_val is not None else (idx + 1)
        if rank <= 0:
            rank = idx + 1

        operator_name = str(get_field_val(r, ["Operator", "operator", "Carrier", "carrier", "Lines", "lines", "Name", "name", "Company"], f"Operator_{rank}")).strip()

        total_teu = parse_int(get_field_val(r, ["Teu", "TotalTeu", "totalteu", "TEU", "Total_Teu", "TotTeu", "fleet_teu"]))
        total_ships = parse_int(get_field_val(r, ["Ships", "TotalShips", "totalships", "NbVessels", "Total_Ships", "TotShips", "fleet_ships"]))

        market_share = parse_float(get_field_val(r, ["Share", "MarketShare", "marketshare", "TEUShare", "Share_Teu", "market_share"]))

        owned_teu = parse_int(get_field_val(r, ["OwnedTeu", "ownedTeu", "OwnedTEU", "owned_teu", "ownedteu"]))
        owned_ships = parse_int(get_field_val(r, ["OwnedShips", "ownedShips", "OwnedNb", "owned_ships", "ownedships"]))

        chartered_teu = parse_int(get_field_val(r, ["CharteredTeu", "charteredTeu", "CharteredTEU", "chartered_teu", "charteredteu"]))
        chartered_ships = parse_int(get_field_val(r, ["CharteredShips", "charteredShips", "CharteredNb", "chartered_ships", "charteredships"]))

        orderbook_teu = parse_int(get_field_val(r, ["OrderbookTeu", "orderbookTeu", "OrderBookTEU", "orderbook_teu", "orderbookteu", "Orderbook_Teu"]))
        orderbook_ships = parse_int(get_field_val(r, ["OrderbookShips", "orderbookShips", "OrderBookNb", "orderbook_ships", "orderbookships", "Orderbook_Ships"]))

        if chartered_teu == 0 and total_teu > owned_teu:
            chartered_teu = total_teu - owned_teu
        if chartered_ships == 0 and total_ships > owned_ships:
            chartered_ships = total_ships - owned_ships

        owned_share = round((owned_teu / total_teu * 100), 2) if total_teu > 0 else 0.0
        chartered_share = round((chartered_teu / total_teu * 100), 2) if total_teu > 0 else 0.0
        orderbook_share = round((orderbook_teu / total_teu * 100), 2) if total_teu > 0 else 0.0

        if total_teu > 0 or total_ships > 0:
            operators.append({
                "rank": rank,
                "operator_name": operator_name,
                "total_teu": total_teu,
                "total_ships": total_ships,
                "market_share": market_share,
                "owned_teu": owned_teu,
                "owned_ships": owned_ships,
                "owned_share": owned_share,
                "chartered_teu": chartered_teu,
                "chartered_ships": chartered_ships,
                "chartered_share": chartered_share,
                "orderbook_teu": orderbook_teu,
                "orderbook_ships": orderbook_ships,
                "orderbook_share": orderbook_share
            })

    operators.sort(key=lambda x: x["rank"])

    if len(operators) < 80:
        if records:
            logger.warning(f"Sample raw record keys: {list(records[0].keys())}")
            logger.warning(f"Sample raw record data: {records[0]}")
        raise ValueError(
            f"Invalid Top 100 extraction: only {len(operators)} valid operator rows found. "
            "Snapshot persistence has been blocked to protect historical data."
        )

    top100_teu = sum(o["total_teu"] for o in operators)
    top100_ships = sum(o["total_ships"] for o in operators)

    global_teu = summary.get("global_teu") or top100_teu
    global_ships = summary.get("global_ships") or top100_ships

    for o in operators:
        if global_teu > 0:
            o["market_share"] = round((o["total_teu"] / global_teu) * 100, 2)

    with db_lock:
        conn = sqlite3.connect(str(db_file))
        cursor = conn.cursor()

        cursor.execute("""
            INSERT OR REPLACE INTO snapshot_meta
            (snapshot_date, created_at, global_teu, global_ships, top100_teu, top100_ships, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            snapshot_date,
            now_iso,
            global_teu,
            global_ships,
            top100_teu,
            top100_ships,
            f"Extracted {len(operators)} operators via ExtJS direct pipeline."
        ))

        for o in operators:
            cursor.execute("""
                INSERT OR REPLACE INTO operator_snapshots
                (snapshot_date, rank, operator_name, total_teu, total_ships, market_share,
                 owned_teu, owned_ships, owned_share, chartered_teu, chartered_ships, chartered_share,
                 orderbook_teu, orderbook_ships, orderbook_share)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                snapshot_date,
                o["rank"],
                o["operator_name"],
                o["total_teu"],
                o["total_ships"],
                o["market_share"],
                o["owned_teu"],
                o["owned_ships"],
                o["owned_share"],
                o["chartered_teu"],
                o["chartered_ships"],
                o["chartered_share"],
                o["orderbook_teu"],
                o["orderbook_ships"],
                o["orderbook_share"]
            ))

        conn.commit()
        conn.close()

    logger.info(f"Successfully persisted snapshot for {snapshot_date} with {len(operators)} operators.")
    return snapshot_date

# =========================================================
# 3-1. Historical Snapshot Validation & Repair
# =========================================================
def repair_historical_snapshots(db_file: Path = DB_PATH):
    init_db(db_file)

    with db_lock:
        conn = sqlite3.connect(str(db_file))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("""
            SELECT snapshot_date, global_teu, global_ships
            FROM snapshot_meta
            ORDER BY snapshot_date ASC
        """)
        meta_rows = [dict(r) for r in cursor.fetchall()]

        removed_dates = []
        repaired_dates = []

        for meta_row in meta_rows:
            snapshot_date = meta_row["snapshot_date"]

            cursor.execute("""
                SELECT *
                FROM operator_snapshots
                WHERE snapshot_date = ?
                ORDER BY rank ASC
            """, (snapshot_date,))
            operators = [dict(row) for row in cursor.fetchall()]

            if len(operators) < 80:
                logger.warning(
                    f"Removing invalid historical snapshot {snapshot_date}: "
                    f"{len(operators)} operator rows found."
                )
                cursor.execute("DELETE FROM operator_snapshots WHERE snapshot_date = ?", (snapshot_date,))
                cursor.execute("DELETE FROM snapshot_meta WHERE snapshot_date = ?", (snapshot_date,))
                removed_dates.append(snapshot_date)
                continue

            top100_teu = sum(parse_int(op.get("total_teu")) for op in operators)
            top100_ships = sum(parse_int(op.get("total_ships")) for op in operators)

            existing_global_teu = parse_int(meta_row.get("global_teu"))
            existing_global_ships = parse_int(meta_row.get("global_ships"))

            global_teu = existing_global_teu if existing_global_teu > 0 else top100_teu
            global_ships = existing_global_ships if existing_global_ships > 0 else top100_ships

            cursor.execute("""
                UPDATE snapshot_meta
                SET global_teu = ?,
                    global_ships = ?,
                    top100_teu = ?,
                    top100_ships = ?
                WHERE snapshot_date = ?
            """, (global_teu, global_ships, top100_teu, top100_ships, snapshot_date))

            for op in operators:
                rank = parse_int(op.get("rank"))
                total_teu = parse_int(op.get("total_teu"))
                total_ships = parse_int(op.get("total_ships"))
                owned_teu = parse_int(op.get("owned_teu"))
                owned_ships = parse_int(op.get("owned_ships"))
                chartered_teu = parse_int(op.get("chartered_teu"))
                chartered_ships = parse_int(op.get("chartered_ships"))
                orderbook_teu = parse_int(op.get("orderbook_teu"))
                orderbook_ships = parse_int(op.get("orderbook_ships"))

                if chartered_teu == 0 and total_teu > owned_teu:
                    chartered_teu = total_teu - owned_teu
                if chartered_ships == 0 and total_ships > owned_ships:
                    chartered_ships = total_ships - owned_ships

                market_share = round((total_teu / global_teu) * 100, 2) if global_teu > 0 else 0.0
                owned_share = round((owned_teu / total_teu) * 100, 2) if total_teu > 0 else 0.0
                chartered_share = round((chartered_teu / total_teu) * 100, 2) if total_teu > 0 else 0.0
                orderbook_share = round((orderbook_teu / total_teu) * 100, 2) if total_teu > 0 else 0.0

                cursor.execute("""
                    UPDATE operator_snapshots
                    SET market_share = ?,
                        chartered_teu = ?,
                        chartered_ships = ?,
                        owned_share = ?,
                        chartered_share = ?,
                        orderbook_share = ?
                    WHERE snapshot_date = ? AND rank = ?
                """, (market_share, chartered_teu, chartered_ships, owned_share, chartered_share, orderbook_share, snapshot_date, rank))

            repaired_dates.append(snapshot_date)

        conn.commit()
        conn.close()

    if removed_dates:
        logger.info(f"Removed invalid historical snapshots: {', '.join(removed_dates)}")
    if repaired_dates:
        logger.info(f"Repaired historical snapshots: {', '.join(repaired_dates)}")

# =========================================================
# 4. JSON Serialization Engine
# =========================================================
def export_dataset_json(db_file: Path = DB_PATH, out_json: Path = JSON_PATH):
    init_db(db_file)
    with db_lock:
        conn = sqlite3.connect(str(db_file))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM snapshot_meta ORDER BY snapshot_date ASC")
        raw_meta = cursor.fetchall()

        if not raw_meta:
            conn.close()
            return {"dates": [], "snapshots": {}}

        # Safe dictionary conversion to eliminate missing key IndexErrors
        meta_rows = [dict(r) for r in raw_meta]
        dates = [r["snapshot_date"] for r in meta_rows]
        snapshots = {}

        for r in meta_rows:
            d = r["snapshot_date"]
            cursor.execute("SELECT * FROM operator_snapshots WHERE snapshot_date = ? ORDER BY rank ASC", (d,))
            op_rows = [dict(x) for x in cursor.fetchall()]

            snapshots[d] = {
                "meta": {
                    "snapshot_date": d,
                    "created_at": r.get("created_at") or d,
                    "global_teu": r.get("global_teu") or 0,
                    "global_ships": r.get("global_ships") or 0,
                    "top100_teu": r.get("top100_teu") or 0,
                    "top100_ships": r.get("top100_ships") or 0,
                    "notes": r.get("notes") or ""
                },
                "operators": op_rows
            }

        conn.close()

    payload = {
        "generated_at": datetime.now().isoformat(),
        "dates": dates,
        "snapshots": snapshots
    }

    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    logger.info(f"Exported static data contract to {out_json}")
    return payload

# =========================================================
# 5. Bloomberg Terminal Static HTML Builder
# =========================================================
def build_static_dashboard(json_payload, out_html: Path = HTML_PATH):
    json_str = json.dumps(json_payload, ensure_ascii=False)

    html_content = f"""<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ALPHALINER TOP 100 // INTELLIGENCE TERMINAL</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700&family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
    <style>
        :root {{
            --bg-base: #060a11;
            --bg-card: rgba(11, 19, 32, 0.85);
            --bg-card-hover: rgba(16, 28, 48, 0.95);
            --border-dim: #132238;
            --border-glow: #1f3a60;
            --text-primary: #e2e8f0;
            --text-secondary: #8fa0b5;
            --text-muted: #52637a;
            --amber-bloomberg: #f59e0b;
            --amber-dim: rgba(245, 158, 11, 0.15);
            --blue-accent: #38bdf8;
            --blue-dim: rgba(56, 189, 248, 0.15);
            --green-up: #10b981;
            --red-down: #ef4444;
            --font-mono: 'JetBrains Mono', monospace;
            --font-sans: 'Inter', sans-serif;
        }}

        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            border-color: var(--border-dim);
        }}

        body {{
            background-color: var(--bg-base);
            background-image:
                radial-gradient(circle at 50% 0%, rgba(31, 58, 96, 0.18) 0%, transparent 60%),
                linear-gradient(rgba(19, 34, 56, 0.25) 1px, transparent 1px),
                linear-gradient(90deg, rgba(19, 34, 56, 0.25) 1px, transparent 1px);
            background-size: 100% 100%, 32px 32px, 32px 32px;
            color: var(--text-primary);
            font-family: var(--font-sans);
            font-size: 13px;
            line-height: 1.4;
            overflow-x: hidden;
            padding-bottom: 24px;
        }}

        .ticker-bar {{
            background: rgba(6, 10, 17, 0.95);
            border-bottom: 1px solid var(--border-dim);
            backdrop-filter: blur(8px);
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 8px 20px;
            position: sticky;
            top: 0;
            z-index: 1000;
        }}

        .brand-cluster {{
            display: flex;
            align-items: center;
            gap: 12px;
        }}

        .brand-code {{
            font-family: var(--font-mono);
            font-weight: 700;
            font-size: 13px;
            letter-spacing: 0.12em;
            color: var(--amber-bloomberg);
            border: 1px solid var(--amber-bloomberg);
            padding: 2px 6px;
            border-radius: 2px;
            background: var(--amber-dim);
        }}

        .brand-title {{
            font-weight: 700;
            font-size: 14px;
            letter-spacing: 0.05em;
            color: #fff;
        }}

        .top-controls {{
            display: flex;
            align-items: center;
            gap: 12px;
        }}

        .date-control-group {{
            display: flex;
            align-items: center;
            background: #09121f;
            border: 1px solid var(--border-glow);
            border-radius: 4px;
            padding: 2px 8px;
            gap: 8px;
        }}

        .date-control-label {{
            font-family: var(--font-mono);
            font-size: 11px;
            color: var(--text-secondary);
            text-transform: uppercase;
        }}

        select.terminal-select {{
            background: transparent;
            border: none;
            color: var(--blue-accent);
            font-family: var(--font-mono);
            font-weight: 600;
            font-size: 12px;
            outline: none;
            cursor: pointer;
            padding: 2px 4px;
        }}

        select.terminal-select option {{
            background: #0b1320;
            color: var(--text-primary);
        }}

        .btn-export {{
            background: rgba(56, 189, 248, 0.1);
            border: 1px solid var(--blue-accent);
            color: var(--blue-accent);
            font-family: var(--font-mono);
            font-size: 11px;
            font-weight: 600;
            padding: 4px 10px;
            border-radius: 3px;
            cursor: pointer;
            transition: all 0.2s ease;
            text-decoration: none;
            display: inline-flex;
            align-items: center;
            gap: 4px;
        }}

        .btn-export:hover {{
            background: var(--blue-accent);
            color: #060a11;
        }}

        .terminal-container {{
            max-width: 1720px;
            margin: 16px auto;
            padding: 0 16px;
            display: flex;
            flex-direction: column;
            gap: 16px;
        }}

        .grid-kpi {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
            gap: 12px;
        }}

        .kpi-card {{
            background: var(--bg-card);
            border: 1px solid var(--border-dim);
            border-radius: 6px;
            padding: 14px 16px;
            display: flex;
            flex-direction: column;
            gap: 6px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.3);
        }}

        .kpi-header {{
            font-family: var(--font-mono);
            font-size: 11px;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            display: flex;
            justify-content: space-between;
        }}

        .kpi-value {{
            font-family: var(--font-mono);
            font-size: 20px;
            font-weight: 700;
            color: #fff;
        }}

        .kpi-sub {{
            font-size: 11px;
            color: var(--text-muted);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}

        .delta-tag {{
            font-family: var(--font-mono);
            font-size: 11px;
            font-weight: 600;
            padding: 1px 4px;
            border-radius: 2px;
        }}
        .delta-up {{ color: var(--green-up); background: rgba(16, 185, 129, 0.1); }}
        .delta-down {{ color: var(--red-down); background: rgba(239, 68, 68, 0.1); }}
        .delta-flat {{ color: var(--text-muted); background: rgba(82, 99, 122, 0.1); }}

        .grid-charts {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 12px;
        }}

        .chart-card {{
            background: var(--bg-card);
            border: 1px solid var(--border-dim);
            border-radius: 6px;
            padding: 14px;
            height: 280px;
            display: flex;
            flex-direction: column;
        }}

        .chart-title {{
            font-family: var(--font-mono);
            font-size: 12px;
            font-weight: 600;
            color: var(--text-secondary);
            margin-bottom: 8px;
            text-transform: uppercase;
        }}

        .chart-wrapper {{
            flex: 1;
            position: relative;
        }}

        .table-card {{
            background: var(--bg-card);
            border: 1px solid var(--border-dim);
            border-radius: 6px;
            overflow: hidden;
            display: flex;
            flex-direction: column;
        }}

        .table-toolbar {{
            padding: 10px 16px;
            border-bottom: 1px solid var(--border-dim);
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: rgba(9, 18, 31, 0.6);
        }}

        .search-box {{
            background: #09121f;
            border: 1px solid var(--border-dim);
            color: var(--text-primary);
            padding: 4px 10px;
            border-radius: 3px;
            font-family: var(--font-mono);
            font-size: 12px;
            width: 240px;
            outline: none;
        }}
        .search-box:focus {{
            border-color: var(--blue-accent);
        }}

        .table-responsive {{
            overflow-x: auto;
            max-height: 640px;
        }}

        table.terminal-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 12px;
            white-space: nowrap;
        }}

        table.terminal-table th {{
            position: sticky;
            top: 0;
            background: #09121f;
            color: var(--text-secondary);
            font-family: var(--font-mono);
            font-weight: 600;
            font-size: 11px;
            text-transform: uppercase;
            padding: 8px 10px;
            border-bottom: 1px solid var(--border-glow);
            border-right: 1px solid rgba(19, 34, 56, 0.4);
            z-index: 10;
        }}

        table.terminal-table th.th-group {{
            text-align: center;
            background: #060b13;
            border-bottom: 1px solid var(--border-dim);
        }}

        table.terminal-table td {{
            padding: 6px 10px;
            border-bottom: 1px solid rgba(19, 34, 56, 0.3);
            border-right: 1px solid rgba(19, 34, 56, 0.2);
        }}

        table.terminal-table tr:hover td {{
            background: var(--bg-card-hover);
        }}

        .text-center {{ text-align: center; }}
        .text-right {{ text-align: right; }}
        .num-font {{ font-family: var(--font-mono); }}

        .rank-badge {{
            background: rgba(245, 158, 11, 0.1);
            color: var(--amber-bloomberg);
            font-family: var(--font-mono);
            font-weight: 700;
            padding: 1px 6px;
            border-radius: 3px;
        }}

        .carrier-name {{
            font-weight: 600;
            color: #fff;
            display: flex;
            align-items: center;
            gap: 6px;
        }}

        .share-bar-container {{
            width: 100%;
            height: 4px;
            background: rgba(19, 34, 56, 0.8);
            border-radius: 2px;
            margin-top: 3px;
            overflow: hidden;
        }}
        .share-bar-fill {{
            height: 100%;
            background: var(--blue-accent);
        }}

        @media (max-width: 1024px) {{
            .grid-charts {{
                grid-template-columns: 1fr;
            }}
        }}
    </style>
</head>
<body>
    <div class="ticker-bar">
        <div class="brand-cluster">
            <span class="brand-code">ALPH&lt;GO&gt;</span>
            <span class="brand-title">ALPHALINER TOP 100 FLEET MONITOR</span>
        </div>
        <div class="top-controls">
            <div class="date-control-group">
                <span class="date-control-label">Snapshot:</span>
                <select id="select-active-date" class="terminal-select" onchange="onDateChange()"></select>
            </div>
            <div class="date-control-group">
                <span class="date-control-label">Compare Base:</span>
                <select id="select-compare-date" class="terminal-select" onchange="onCompareChange()"></select>
            </div>
            <button class="btn-export" onclick="exportActiveTableToCSV()">EXPORT CSV</button>
        </div>
    </div>

    <div class="terminal-container">
        <!-- KPI Row -->
        <div class="grid-kpi">
            <div class="kpi-card">
                <div class="kpi-header">
                    <span>Global Fleet (Official)</span>
                    <span id="kpi-global-ships">-</span>
                </div>
                <div class="kpi-value" id="kpi-global-teu">-</div>
                <div class="kpi-sub">
                    <span>100% Industry Baseline</span>
                    <span id="kpi-global-delta" class="delta-tag delta-flat">-</span>
                </div>
            </div>

            <div class="kpi-card">
                <div class="kpi-header">
                    <span>Top 100 Capacity</span>
                    <span id="kpi-top100-ships">-</span>
                </div>
                <div class="kpi-value" id="kpi-top100-teu">-</div>
                <div class="kpi-sub">
                    <span id="kpi-top100-share">-</span>
                    <span id="kpi-top100-delta" class="delta-tag delta-flat">-</span>
                </div>
            </div>

            <div class="kpi-card">
                <div class="kpi-header">
                    <span>Top 10 Concentration</span>
                    <span id="kpi-top10-share">-</span>
                </div>
                <div class="kpi-value" id="kpi-top10-teu">-</div>
                <div class="kpi-sub">
                    <span>Top 10 Lines TEU</span>
                    <span id="kpi-top10-delta" class="delta-tag delta-flat">-</span>
                </div>
            </div>

            <div class="kpi-card">
                <div class="kpi-header">
                    <span>Orderbook Pipeline</span>
                    <span id="kpi-orderbook-ratio">-</span>
                </div>
                <div class="kpi-value" id="kpi-orderbook-teu" style="color: var(--amber-bloomberg);">-</div>
                <div class="kpi-sub">
                    <span>Top 100 On-Order</span>
                    <span id="kpi-orderbook-delta" class="delta-tag delta-flat">-</span>
                </div>
            </div>
        </div>

        <!-- Charts Row -->
        <div class="grid-charts">
            <div class="chart-card">
                <div class="chart-title">Top 10 Lines // Existing Fleet vs Orderbook Pipeline</div>
                <div class="chart-wrapper">
                    <canvas id="chartTop10"></canvas>
                </div>
            </div>
            <div class="chart-card">
                <div class="chart-title">Top 10 Lines // Fleet Ownership Structure (Owned vs Chartered)</div>
                <div class="chart-wrapper">
                    <canvas id="chartOwnership"></canvas>
                </div>
            </div>
        </div>

        <!-- Detailed Table -->
        <div class="table-card">
            <div class="table-toolbar">
                <input type="text" id="table-search-input" class="search-box" placeholder="Filter carrier / operator..." onkeyup="filterTable()">
                <div style="font-family: var(--font-mono); font-size: 11px; color: var(--text-secondary);">
                    Comparison Delta vs <span id="label-compare-date" style="color: var(--blue-accent);">-</span>
                </div>
            </div>
            <div class="table-responsive">
                <table class="terminal-table">
                    <thead>
                        <tr>
                            <th rowspan="2" style="width: 50px;">#</th>
                            <th rowspan="2" style="min-width: 180px;">Operator</th>
                            <th colspan="3" class="th-group">Total Fleet</th>
                            <th colspan="3" class="th-group">Owned Fleet</th>
                            <th colspan="3" class="th-group">Chartered Fleet</th>
                            <th colspan="3" class="th-group">Orderbook Pipeline</th>
                            <th rowspan="2" style="min-width: 90px;" class="text-right">TEU Δ</th>
                        </tr>
                        <tr>
                            <th class="text-right">TEU</th>
                            <th class="text-right">Ships</th>
                            <th class="text-right">Share</th>
                            <th class="text-right">TEU</th>
                            <th class="text-right">Ships</th>
                            <th class="text-right">%</th>
                            <th class="text-right">TEU</th>
                            <th class="text-right">Ships</th>
                            <th class="text-right">%</th>
                            <th class="text-right">TEU</th>
                            <th class="text-right">Ships</th>
                            <th class="text-right">%</th>
                        </tr>
                    </thead>
                    <tbody id="table-body"></tbody>
                </table>
            </div>
        </div>
    </div>

    <script>
        const RAW_DATASET = {json_str};
        let currentActiveDate = null;
        let currentCompareDate = null;
        let chartTop10Instance = null;
        let chartOwnershipInstance = null;

        function fmtNum(n) {{
            if (n === null || n === undefined || isNaN(n)) return "0";
            return Number(n).toLocaleString();
        }}

        function initApp() {{
            const dates = RAW_DATASET.dates || [];
            if (!dates.length) return;

            const selActive = document.getElementById('select-active-date');
            const selCompare = document.getElementById('select-compare-date');

            selActive.innerHTML = '';
            selCompare.innerHTML = '';

            [...dates].reverse().forEach((d, idx) => {{
                selActive.innerHTML += `<option value="${{d}}">${{d}}</option>`;
                selCompare.innerHTML += `<option value="${{d}}">${{d}}</option>`;
            }});

            currentActiveDate = dates[dates.length - 1];
            currentCompareDate = dates.length > 1 ? dates[dates.length - 2] : dates[dates.length - 1];

            selActive.value = currentActiveDate;
            selCompare.value = currentCompareDate;

            renderDashboard();
        }}

        function onDateChange() {{
            currentActiveDate = document.getElementById('select-active-date').value;
            renderDashboard();
        }}

        function onCompareChange() {{
            currentCompareDate = document.getElementById('select-compare-date').value;
            renderDashboard();
        }}

        function renderDashboard() {{
            const current = RAW_DATASET.snapshots[currentActiveDate];
            const compare = RAW_DATASET.snapshots[currentCompareDate];
            if (!current) return;

            document.getElementById('label-compare-date').innerText = currentCompareDate;

            renderKPIs(current, compare);
            renderCharts(current);
            renderTable(current, compare);
        }}

        function renderKPIs(current, compare) {{
            const cMeta = current.meta || {{}};
            const pMeta = compare ? compare.meta : cMeta;

            const gTeuDelta = (cMeta.global_teu || 0) - (pMeta.global_teu || 0);
            document.getElementById('kpi-global-teu').innerText = `${{fmtNum(cMeta.global_teu)}} TEU`;
            document.getElementById('kpi-global-ships').innerText = `${{fmtNum(cMeta.global_ships)}} Ships`;
            renderDeltaTag('kpi-global-delta', gTeuDelta, 'TEU');

            const tTeuDelta = (cMeta.top100_teu || 0) - (pMeta.top100_teu || 0);
            const t100Share = cMeta.global_teu ? ((cMeta.top100_teu / cMeta.global_teu) * 100).toFixed(1) : "0.0";
            document.getElementById('kpi-top100-teu').innerText = `${{fmtNum(cMeta.top100_teu)}} TEU`;
            document.getElementById('kpi-top100-ships').innerText = `${{fmtNum(cMeta.top100_ships)}} Ships`;
            document.getElementById('kpi-top100-share').innerText = `${{t100Share}}% of Global Fleet`;
            renderDeltaTag('kpi-top100-delta', tTeuDelta, 'TEU');

            const top10Ops = (current.operators || []).slice(0, 10);
            const top10TEU = top10Ops.reduce((sum, o) => sum + (o.total_teu || 0), 0);
            const top10Share = cMeta.global_teu ? ((top10TEU / cMeta.global_teu) * 100).toFixed(1) : "0.0";

            const pTop10Ops = (compare.operators || []).slice(0, 10);
            const pTop10TEU = pTop10Ops.reduce((sum, o) => sum + (o.total_teu || 0), 0);
            const top10Delta = top10TEU - pTop10TEU;

            document.getElementById('kpi-top10-share').innerText = `${{top10Share}}%`;
            document.getElementById('kpi-top10-teu').innerText = `${{fmtNum(top10TEU)}} TEU`;
            renderDeltaTag('kpi-top10-delta', top10Delta, 'TEU');

            const totalOrderbook = (current.operators || []).reduce((sum, o) => sum + (o.orderbook_teu || 0), 0);
            const pTotalOrderbook = (compare.operators || []).reduce((sum, o) => sum + (o.orderbook_teu || 0), 0);
            const obRatio = cMeta.top100_teu ? ((totalOrderbook / cMeta.top100_teu) * 100).toFixed(2) : "0.00";
            const obDelta = totalOrderbook - pTotalOrderbook;

            document.getElementById('kpi-orderbook-teu').innerText = `${{fmtNum(totalOrderbook)}} TEU`;
            document.getElementById('kpi-orderbook-ratio').innerText = `${{obRatio}}% of Top 100`;
            renderDeltaTag('kpi-orderbook-delta', obDelta, 'TEU');
        }}

        function renderDeltaTag(elementId, deltaValue, unit) {{
            const el = document.getElementById(elementId);
            if (!el) return;

            if (deltaValue > 0) {{
                el.className = "delta-tag delta-up";
                el.innerText = `+${{fmtNum(deltaValue)}} ${{unit}}`;
            }} else if (deltaValue < 0) {{
                el.className = "delta-tag delta-down";
                el.innerText = `${{fmtNum(deltaValue)}} ${{unit}}`;
            }} else {{
                el.className = "delta-tag delta-flat";
                el.innerText = `0 ${{unit}}`;
            }}
        }}

        function renderCharts(currentSnap) {{
            const top10 = (currentSnap.operators || []).slice(0, 10);
            const labels = top10.map(o => o.operator_name.length > 14 ? o.operator_name.substring(0, 12) + '..' : o.operator_name);

            const ctx1 = document.getElementById('chartTop10').getContext('2d');
            if (chartTop10Instance) chartTop10Instance.destroy();

            chartTop10Instance = new Chart(ctx1, {{
                type: 'bar',
                data: {{
                    labels: labels,
                    datasets: [
                        {{
                            label: 'Existing TEU',
                            data: top10.map(o => o.total_teu),
                            backgroundColor: '#38bdf8',
                            borderRadius: 2
                        }},
                        {{
                            label: 'Orderbook TEU',
                            data: top10.map(o => o.orderbook_teu),
                            backgroundColor: '#f59e0b',
                            borderRadius: 2
                        }}
                    ]
                }},
                options: {{
                    responsive: true,
                    maintainAspectRatio: false,
                    scales: {{
                        x: {{
                            stacked: true,
                            grid: {{ color: 'rgba(19, 34, 56, 0.4)' }},
                            ticks: {{ color: '#8fa0b5', font: {{ family: 'JetBrains Mono', size: 10 }} }}
                        }},
                        y: {{
                            stacked: true,
                            grid: {{ color: 'rgba(19, 34, 56, 0.4)' }},
                            ticks: {{
                                color: '#8fa0b5',
                                font: {{ family: 'JetBrains Mono', size: 10 }},
                                callback: val => (val / 1000000).toFixed(1) + 'M'
                            }}
                        }}
                    }},
                    plugins: {{
                        legend: {{
                            position: 'top',
                            labels: {{ color: '#e2e8f0', font: {{ family: 'Inter', size: 11 }} }}
                        }}
                    }}
                }}
            }});

            const ctx2 = document.getElementById('chartOwnership').getContext('2d');
            if (chartOwnershipInstance) chartOwnershipInstance.destroy();

            chartOwnershipInstance = new Chart(ctx2, {{
                type: 'bar',
                data: {{
                    labels: labels,
                    datasets: [
                        {{
                            label: 'Owned TEU',
                            data: top10.map(o => o.owned_teu),
                            backgroundColor: '#10b981',
                            borderRadius: 2
                        }},
                        {{
                            label: 'Chartered TEU',
                            data: top10.map(o => o.chartered_teu),
                            backgroundColor: '#6366f1',
                            borderRadius: 2
                        }}
                    ]
                }},
                options: {{
                    responsive: true,
                    maintainAspectRatio: false,
                    scales: {{
                        x: {{
                            stacked: true,
                            grid: {{ color: 'rgba(19, 34, 56, 0.4)' }},
                            ticks: {{ color: '#8fa0b5', font: {{ family: 'JetBrains Mono', size: 10 }} }}
                        }},
                        y: {{
                            stacked: true,
                            grid: {{ color: 'rgba(19, 34, 56, 0.4)' }},
                            ticks: {{
                                color: '#8fa0b5',
                                font: {{ family: 'JetBrains Mono', size: 10 }},
                                callback: val => (val / 1000000).toFixed(1) + 'M'
                            }}
                        }}
                    }},
                    plugins: {{
                        legend: {{
                            position: 'top',
                            labels: {{ color: '#e2e8f0', font: {{ family: 'Inter', size: 11 }} }}
                        }}
                    }}
                }}
            }});
        }}

        function renderTable(currentSnap, compareSnap) {{
            const tbody = document.getElementById('table-body');
            const ops = currentSnap.operators || [];

            const baseMap = new Map();
            (compareSnap.operators || []).forEach(o => {{
                baseMap.set(o.operator_name, o);
            }});

            let rowsHtml = '';
            ops.forEach(op => {{
                const baseOp = baseMap.get(op.operator_name);
                const deltaTEU = baseOp ? (op.total_teu - baseOp.total_teu) : 0;
                let deltaHtml = '<span class="num-font" style="color: var(--text-muted);">-</span>';

                if (deltaTEU > 0) {{
                    deltaHtml = `<span class="num-font" style="color: var(--green-up);">+${{fmtNum(deltaTEU)}}</span>`;
                }} else if (deltaTEU < 0) {{
                    deltaHtml = `<span class="num-font" style="color: var(--red-down);">${{fmtNum(deltaTEU)}}</span>`;
                }}

                rowsHtml += `
                    <tr>
                        <td class="text-center"><span class="rank-badge">${{op.rank}}</span></td>
                        <td>
                            <div class="carrier-name">
                                <span>${{op.operator_name}}</span>
                            </div>
                        </td>
                        <td class="text-right num-font" style="color: #fff; font-weight:600;">${{fmtNum(op.total_teu)}}</td>
                        <td class="text-right num-font">${{fmtNum(op.total_ships)}}</td>
                        <td class="text-right num-font" style="color: var(--blue-accent);">
                            ${{op.market_share.toFixed(1)}}%
                            <div class="share-bar-container"><div class="share-bar-fill" style="width: ${{Math.min(op.market_share * 4.5, 100)}}%;"></div></div>
                        </td>
                        <td class="text-right num-font">${{fmtNum(op.owned_teu)}}</td>
                        <td class="text-right num-font">${{fmtNum(op.owned_ships)}}</td>
                        <td class="text-right num-font" style="color: var(--text-secondary);">${{op.owned_share.toFixed(1)}}%</td>
                        <td class="text-right num-font">${{fmtNum(op.chartered_teu)}}</td>
                        <td class="text-right num-font">${{fmtNum(op.chartered_ships)}}</td>
                        <td class="text-right num-font" style="color: var(--text-secondary);">${{op.chartered_share.toFixed(1)}}%</td>
                        <td class="text-right num-font" style="color: var(--amber-bloomberg);">${{fmtNum(op.orderbook_teu)}}</td>
                        <td class="text-right num-font">${{fmtNum(op.orderbook_ships)}}</td>
                        <td class="text-right num-font" style="color: var(--amber-bloomberg);">${{op.orderbook_share.toFixed(1)}}%</td>
                        <td class="text-right">${{deltaHtml}}</td>
                    </tr>
                `;
            }});

            tbody.innerHTML = rowsHtml;
        }}

        function filterTable() {{
            const term = document.getElementById('table-search-input').value.toLowerCase();
            const rows = document.querySelectorAll('#table-body tr');
            rows.forEach(r => {{
                const text = r.children[1].innerText.toLowerCase();
                r.style.display = text.includes(term) ? '' : 'none';
            }});
        }}

        function exportActiveTableToCSV() {{
            const currentSnap = RAW_DATASET.snapshots[currentActiveDate];
            if (!currentSnap || !currentSnap.operators) return;

            const headers = [
                "Rank", "Operator", "Total_TEU", "Total_Ships", "Market_Share_Pct",
                "Owned_TEU", "Owned_Ships", "Owned_Share_Pct",
                "Chartered_TEU", "Chartered_Ships", "Chartered_Share_Pct",
                "Orderbook_TEU", "Orderbook_Ships", "Orderbook_Share_Pct"
            ];

            let csvContent = "\uFEFF" + headers.join(",") + "\\n";

            currentSnap.operators.forEach(o => {{
                const row = [
                    o.rank,
                    `"${{(o.operator_name || "").replace(/"/g, '""')}}"`,
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
                csvContent += row.join(",") + "\\n";
            }});

            const blob = new Blob([csvContent], {{ type: 'text/csv;charset=utf-8;' }});
            const link = document.createElement("a");
            const url = URL.createObjectURL(blob);
            link.setAttribute("href", url);
            link.setAttribute("download", `Alphaliner_TOP100_${{currentActiveDate}}.csv`);
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
        }}

        window.addEventListener('DOMContentLoaded', initApp);
    </script>
</body>
</html>
"""

    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html_content)

    logger.info(f"Built Bloomberg Static Terminal dashboard: {out_html}")

# =========================================================
# 6. Interactive Web Server & API Endpoints
# =========================================================
@app.route("/")
def index_route():
    if not HTML_PATH.exists():
        payload = export_dataset_json(DB_PATH, JSON_PATH)
        build_static_dashboard(payload, HTML_PATH)
    return send_from_directory(str(BASE_DIR), "index.html")

@app.route("/api/data")
def api_data_route():
    if not JSON_PATH.exists():
        payload = export_dataset_json(DB_PATH, JSON_PATH)
        return Response(json.dumps(payload), mimetype="application/json")
    return send_from_directory(str(BASE_DIR), "alphaliner_data.json", mimetype="application/json")

@app.route("/api/export/csv")
def api_export_csv():
    target_date = request.args.get("date")
    init_db(DB_PATH)

    with db_lock:
        conn = sqlite3.connect(str(DB_PATH))
        if not target_date:
            cur = conn.cursor()
            cur.execute("SELECT MAX(snapshot_date) FROM snapshot_meta")
            row = cur.fetchone()
            target_date = row[0] if row else None

        if not target_date:
            conn.close()
            return Response("No data available", status=404)

        df = pd.read_sql_query(
            "SELECT * FROM operator_snapshots WHERE snapshot_date = ? ORDER BY rank ASC",
            conn,
            params=(target_date,)
        )
        conn.close()

    csv_data = "\uFEFF" + df.to_csv(index=False)
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=Alphaliner_TOP100_{target_date}.csv"}
    )

# =========================================================
# 7. Main Execution Pipeline
# =========================================================
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

    # Perform repair & validation before JSON/HTML build
    repair_historical_snapshots(DB_PATH)

    json_payload = export_dataset_json(DB_PATH, JSON_PATH)
    build_static_dashboard(json_payload, HTML_PATH)

    if args.cron:
        logger.info("Batch execution finished successfully. Exiting.")
        sys.exit(0)

    logger.info(f"Starting Bloomberg Terminal Interactive Server on http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)

if __name__ == "__main__":
    main()
