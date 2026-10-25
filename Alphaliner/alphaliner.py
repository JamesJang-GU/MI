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
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("Alphaliner")
db_lock = Lock()

# =========================================================
# 2. Database Schema & Migration Utilities
# =========================================================
def init_db():
    with db_lock, sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()

        # 1) snapshot_meta 테이블 생성
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS snapshot_meta (
                snapshot_date TEXT PRIMARY KEY,
                updated_at TEXT,
                total_teu INTEGER,
                total_ships INTEGER,
                top100_teu INTEGER,
                top100_ships INTEGER,
                top100_share REAL,
                orderbook_teu INTEGER,
                orderbook_ships INTEGER
            )
        """)

        # 2) operator_snapshots 테이블 생성
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS operator_snapshots (
                snapshot_date TEXT,
                rank INTEGER,
                name TEXT,
                country TEXT,
                teu INTEGER,
                teu_share REAL,
                ships INTEGER,
                chartered_teu INTEGER,
                chartered_share REAL,
                chartered_ships INTEGER,
                owned_teu INTEGER,
                owned_share REAL,
                owned_ships INTEGER,
                orderbook_teu INTEGER,
                orderbook_teu_share REAL,
                orderbook_ships INTEGER,
                created_at TEXT,
                PRIMARY KEY (snapshot_date, rank)
            )
        """)

        # 3) 기존 DB 컬럼 누락 방지를 위한 자동 마이그레이션 (ALTER TABLE)
        cursor.execute("PRAGMA table_info(snapshot_meta)")
        existing_meta_cols = {col[1] for col in cursor.fetchall()}

        expected_meta_cols = {
            "updated_at": "TEXT",
            "total_teu": "INTEGER",
            "total_ships": "INTEGER",
            "top100_teu": "INTEGER",
            "top100_ships": "INTEGER",
            "top100_share": "REAL",
            "orderbook_teu": "INTEGER",
            "orderbook_ships": "INTEGER"
        }
        for col_name, col_type in expected_meta_cols.items():
            if col_name not in existing_meta_cols:
                logger.info(f"[DB MIGRATE] Adding missing column '{col_name}' to snapshot_meta")
                cursor.execute(f"ALTER TABLE snapshot_meta ADD COLUMN {col_name} {col_type}")

        cursor.execute("PRAGMA table_info(operator_snapshots)")
        existing_op_cols = {col[1] for col in cursor.fetchall()}

        expected_op_cols = {
            "country": "TEXT",
            "teu": "INTEGER",
            "teu_share": "REAL",
            "ships": "INTEGER",
            "chartered_teu": "INTEGER",
            "chartered_share": "REAL",
            "chartered_ships": "INTEGER",
            "owned_teu": "INTEGER",
            "owned_share": "REAL",
            "owned_ships": "INTEGER",
            "orderbook_teu": "INTEGER",
            "orderbook_teu_share": "REAL",
            "orderbook_ships": "INTEGER",
            "created_at": "TEXT"
        }
        for col_name, col_type in expected_op_cols.items():
            if col_name not in existing_op_cols:
                logger.info(f"[DB MIGRATE] Adding missing column '{col_name}' to operator_snapshots")
                cursor.execute(f"ALTER TABLE operator_snapshots ADD COLUMN {col_name} {col_type}")

        conn.commit()

def parse_int(val):
    if val is None or val == "":
        return 0
    if isinstance(val, (int, float)):
        return int(val)
    cleaned = re.sub(r"[^\d\-]", "", str(val))
    try:
        return int(cleaned) if cleaned else 0
    except ValueError:
        return 0

def parse_float(val):
    if val is None or val == "":
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    cleaned = re.sub(r"[^\d\.\-]", "", str(val))
    try:
        return float(cleaned) if cleaned else 0.0
    except ValueError:
        return 0.0

def get_field_val(record, candidates, default=""):
    for c in candidates:
        if c in record and record[c] is not None:
            return record[c]
    return default

# =========================================================
# 3. Selenium & ExtJS Scraping
# =========================================================
def get_chrome_driver():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

    chrome_bin = os.getenv("CHROME_BINARY") or os.getenv("CHROME_BIN") or os.getenv("GOOGLE_CHROME_BIN")
    if chrome_bin and os.path.exists(chrome_bin):
        options.binary_location = chrome_bin

    chromedriver_path = os.getenv("CHROMEDRIVER_PATH")
    if chromedriver_path and os.path.exists(chromedriver_path):
        service = Service(chromedriver_path)
        return webdriver.Chrome(service=service, options=options)

    if HAS_WEBDRIVER_MANAGER:
        try:
            service = Service(ChromeDriverManager().install())
            return webdriver.Chrome(service=service, options=options)
        except Exception as e:
            logger.warning(f"WebDriverManager failed: {e}. Falling back to default PATH driver.")

    return webdriver.Chrome(options=options)

def fetch_alphaliner_data():
    logger.info(f"Navigating to {TARGET_URL}...")
    driver = None
    try:
        driver = get_chrome_driver()
        driver.get(TARGET_URL)

        wait = WebDriverWait(driver, 25)
        wait.until(lambda d: d.execute_script(
            "return typeof Ext !== 'undefined' && Ext.data && Ext.data.StoreManager && Ext.data.StoreManager.lookup('Top100description') !== undefined;"
        ))
        time.sleep(2)

        raw_data = driver.execute_script("""
            var store = Ext.data.StoreManager.lookup('Top100description');
            if (!store) return null;
            var records = [];
            store.each(function(rec) {
                records.push(rec.data);
            });
            return {
                records: records,
                count: store.getCount()
            };
        """)

        if not raw_data or not raw_data.get("records"):
            raise ValueError("ExtJS Store Top100description is empty or unavailable.")

        logger.info(f"Successfully extracted {len(raw_data['records'])} records from ExtJS Store.")
        return raw_data["records"]

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
# 4. Processing & Persistence
# =========================================================
def process_and_persist(records):
    init_db()
    snapshot_date = datetime.now().strftime("%Y-%m-%d")
    now_iso = datetime.now().isoformat()

    operators = []
    tot_teu = 0
    tot_ships = 0
    tot_ob_teu = 0
    tot_ob_ships = 0

    for r in records:
        rank = parse_int(get_field_val(r, ["Rank", "rank", "Position", "position", "id"]))
        if rank <= 0 or rank > 100:
            continue

        name = str(get_field_val(r, ["Operator", "operator", "Name", "name", "Liner", "liner"], "Unknown")).strip()
        country = str(get_field_val(r, ["Country", "country", "Flag", "flag"], "")).strip()

        teu = parse_int(get_field_val(r, ["TotalTeu", "totalTeu", "TEU", "teu", "Capacity", "capacity"]))
        teu_share = parse_float(get_field_val(r, ["TotalShare", "totalShare", "Share", "share", "MarketShare", "marketShare", "percent"]))
        ships = parse_int(get_field_val(r, ["TotalShips", "totalShips", "Ships", "ships", "NbShips", "nbShips"]))

        chartered_teu = parse_int(get_field_val(r, ["CharteredTeu", "charteredTeu", "CharterTeu", "charter_teu", "chartTeu"]))
        chartered_share = parse_float(get_field_val(r, ["CharteredShare", "charteredShare", "CharterShare", "charter_share", "chartProcent"]))
        chartered_ships = parse_int(get_field_val(r, ["CharteredShips", "charteredShips", "CharterShips", "charter_ships", "chartShips"]))

        owned_teu = parse_int(get_field_val(r, ["OwnedTeu", "ownedTeu", "OwnTeu", "own_teu"]))
        owned_share = parse_float(get_field_val(r, ["OwnedShare", "ownedShare", "OwnShare", "own_share"]))
        if owned_share == 0.0 and teu > 0 and owned_teu > 0:
            owned_share = round((owned_teu / teu) * 100, 1)

        owned_ships = parse_int(get_field_val(r, ["OwnedShips", "ownedShips", "OwnShips", "own_ships"]))

        orderbook_teu = parse_int(get_field_val(r, [
            "TEUOrderbook", "TeuOrderbook", "teu_orderbook", "teuorderbook",
            "OrderbookTeu", "orderbookTeu", "OrderBookTEU", "OrderbookTEU",
            "orderbook_teu", "orderbookteu", "Orderbook_Teu", "OBTeu", "OBTEU", "orderTeu"
        ]))

        orderbook_ships = parse_int(get_field_val(r, [
            "ShipsOrderbook", "ShipOrderbook", "ships_orderbook", "shipsorderbook",
            "OrderbookShips", "orderbookShips", "OrderBookShips", "OrderBookNb",
            "OrderbookNb", "orderbook_ships", "orderbookships", "Orderbook_Ships",
            "OBShips", "OBNb", "orderShips"
        ]))

        orderbook_teu_share = parse_float(get_field_val(r, [
            "OrderbookShare", "orderbookShare", "OrderBookShare",
            "TEUOrderbookShare", "orderbook_share", "OBShare", "orderProcents"
        ]))
        if orderbook_teu_share == 0.0 and teu > 0 and orderbook_teu > 0:
            orderbook_teu_share = round((orderbook_teu / teu) * 100, 2)

        tot_teu += teu
        tot_ships += ships
        tot_ob_teu += orderbook_teu
        tot_ob_ships += orderbook_ships

        operators.append({
            "snapshot_date": snapshot_date,
            "rank": rank,
            "name": name,
            "country": country,
            "teu": teu,
            "teu_share": teu_share,
            "ships": ships,
            "chartered_teu": chartered_teu,
            "chartered_share": chartered_share,
            "chartered_ships": chartered_ships,
            "owned_teu": owned_teu,
            "owned_share": owned_share,
            "owned_ships": owned_ships,
            "orderbook_teu": orderbook_teu,
            "orderbook_teu_share": orderbook_teu_share,
            "orderbook_ships": orderbook_ships,
            "created_at": now_iso
        })

    operators.sort(key=lambda x: x["rank"])

    with db_lock, sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO snapshot_meta (
                snapshot_date, updated_at, total_teu, total_ships,
                top100_teu, top100_ships, top100_share, orderbook_teu, orderbook_ships
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            snapshot_date,
            now_iso,
            tot_teu,
            tot_ships,
            tot_teu,
            tot_ships,
            100.0,
            tot_ob_teu,
            tot_ob_ships
        ))

        for op in operators:
            cursor.execute("""
                INSERT OR REPLACE INTO operator_snapshots (
                    snapshot_date, rank, name, country, teu, teu_share, ships,
                    chartered_teu, chartered_share, chartered_ships,
                    owned_teu, owned_share, owned_ships,
                    orderbook_teu, orderbook_teu_share, orderbook_ships, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                op["snapshot_date"], op["rank"], op["name"], op["country"],
                op["teu"], op["teu_share"], op["ships"],
                op["chartered_teu"], op["chartered_share"], op["chartered_ships"],
                op["owned_teu"], op["owned_share"], op["owned_ships"],
                op["orderbook_teu"], op["orderbook_teu_share"], op["orderbook_ships"],
                op["created_at"]
            ))
        conn.commit()

    logger.info(f"Persisted snapshot {snapshot_date}: {len(operators)} operators (Orderbook TEU: {tot_ob_teu:,}).")
    return snapshot_date

# =========================================================
# 5. Static Export & Contract Serialization
# =========================================================
def export_json_and_html():
    init_db()
    with db_lock, sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("SELECT DISTINCT snapshot_date FROM snapshot_meta ORDER BY snapshot_date DESC")
        date_rows = cursor.fetchall()
        dates = [r["snapshot_date"] for r in date_rows]

        if not dates:
            logger.warning("No snapshot dates found in database.")
            return

        snapshots = {}
        for d in dates:
            cursor.execute("SELECT * FROM snapshot_meta WHERE snapshot_date = ?", (d,))
            meta_row = cursor.fetchone()
            meta = dict(meta_row) if meta_row else {}

            cursor.execute("SELECT * FROM operator_snapshots WHERE snapshot_date = ? ORDER BY rank ASC", (d,))
            ops = [dict(r) for r in cursor.fetchall()]

            snapshots[d] = {
                "meta": meta,
                "operators": ops
            }

    data_payload = {
        "generated_at": datetime.now().isoformat(),
        "dates": dates,
        "latest_date": dates[0] if dates else None,
        "snapshots": snapshots
    }

    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data_payload, f, ensure_ascii=False, indent=2)

    logger.info(f"Exported contract JSON to {JSON_PATH}")
    generate_static_html(data_payload)

def generate_static_html(payload):
    html_template = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ALPHALINER TOP 100 LINER OPERATORS | MI TERMINAL</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700;800&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root {{
  --bg-primary: #02060b;
  --bg-secondary: #07111c;
  --bg-card: #0a1724;
  --bg-card-hover: #0e1e30;
  --border-dim: #132738;
  --border-light: #1f3d59;
  --border-accent: #29537a;

  --text-primary: #f0f6fc;
  --text-secondary: #9db2c6;
  --text-muted: #546e85;
  --text-dim: #384e63;

  --accent-blue: #388bfd;
  --accent-cyan: #39c5cf;
  --accent-amber: #f0883e;
  --accent-orange: #ff9800;
  --accent-green: #3fb950;
  --accent-red: #f85149;
  --accent-purple: #a371f7;

  --font-mono: 'JetBrains Mono', monospace;
  --font-sans: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  --radius-sm: 4px;
  --radius-md: 6px;
}}

* {{
  box-sizing: border-box;
  margin: 0;
  padding: 0;
}}

body {{
  background-color: var(--bg-primary);
  color: var(--text-primary);
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.4;
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  -webkit-font-smoothing: antialiased;
}}

.top-command-bar {{
  background: var(--bg-secondary);
  border-bottom: 1px solid var(--border-dim);
  padding: 8px 16px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  position: sticky;
  top: 0;
  z-index: 100;
  height: 48px;
}}

.brand-section {{
  display: flex;
  align-items: center;
  gap: 12px;
}}

.terminal-tag {{
  background: var(--accent-orange);
  color: #000;
  font-weight: 800;
  padding: 2px 6px;
  border-radius: var(--radius-sm);
  font-size: 10px;
  letter-spacing: 0.5px;
}}

.title-text {{
  font-weight: 700;
  font-size: 13px;
  letter-spacing: 0.5px;
  color: var(--text-primary);
}}

.controls-section {{
  display: flex;
  align-items: center;
  gap: 12px;
}}

.time-travel-wrap {{
  display: flex;
  align-items: center;
  gap: 6px;
  background: var(--bg-primary);
  border: 1px solid var(--border-dim);
  padding: 3px 8px;
  border-radius: var(--radius-sm);
}}

.time-travel-wrap label {{
  color: var(--text-muted);
  font-size: 10px;
  font-weight: 600;
}}

select, input[type="text"] {{
  background: transparent;
  color: var(--text-primary);
  border: none;
  font-family: var(--font-mono);
  font-size: 11px;
  outline: none;
}}

select option {{
  background: var(--bg-secondary);
  color: var(--text-primary);
}}

.search-input {{
  background: var(--bg-primary);
  border: 1px solid var(--border-dim);
  padding: 4px 10px;
  border-radius: var(--radius-sm);
  color: var(--text-primary);
  width: 180px;
  font-size: 11px;
}}

.search-input:focus {{
  border-color: var(--accent-blue);
}}

.btn {{
  background: var(--bg-card);
  color: var(--text-secondary);
  border: 1px solid var(--border-dim);
  padding: 4px 10px;
  border-radius: var(--radius-sm);
  font-family: var(--font-mono);
  font-size: 11px;
  cursor: pointer;
  transition: all 0.15s ease;
  text-decoration: none;
  display: inline-flex;
  align-items: center;
  gap: 4px;
}}

.btn:hover {{
  background: var(--bg-card-hover);
  color: var(--text-primary);
  border-color: var(--border-light);
}}

.btn-accent {{
  background: rgba(56, 139, 253, 0.15);
  border-color: var(--accent-blue);
  color: var(--accent-blue);
}}

.btn-accent:hover {{
  background: var(--accent-blue);
  color: #fff;
}}

.main-content {{
  padding: 16px;
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 16px;
  max-width: 1920px;
  margin: 0 auto;
  width: 100%;
}}

.kpi-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 12px;
}}

.kpi-card {{
  background: var(--bg-secondary);
  border: 1px solid var(--border-dim);
  border-radius: var(--radius-md);
  padding: 12px 14px;
  position: relative;
  overflow: hidden;
}}

.kpi-card::before {{
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 2px;
  background: var(--border-light);
}}

.kpi-card.accent-blue::before {{ background: var(--accent-blue); }}
.kpi-card.accent-amber::before {{ background: var(--accent-amber); }}
.kpi-card.accent-green::before {{ background: var(--accent-green); }}
.kpi-card.accent-purple::before {{ background: var(--accent-purple); }}

.kpi-header {{
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 6px;
}}

.kpi-title {{
  color: var(--text-muted);
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}}

.kpi-sub {{
  color: var(--text-dim);
  font-size: 10px;
}}

.kpi-value {{
  font-size: 20px;
  font-weight: 800;
  color: var(--text-primary);
  letter-spacing: -0.5px;
}}

.kpi-meta {{
  display: flex;
  gap: 8px;
  margin-top: 4px;
  font-size: 11px;
  color: var(--text-secondary);
}}

.delta-badge {{
  font-weight: 600;
  padding: 0 4px;
  border-radius: 2px;
}}

.delta-up {{ color: var(--accent-green); background: rgba(63, 185, 80, 0.1); }}
.delta-down {{ color: var(--accent-red); background: rgba(248, 81, 73, 0.1); }}
.delta-zero {{ color: var(--text-dim); background: rgba(255, 255, 255, 0.05); }}

.table-panel {{
  background: var(--bg-secondary);
  border: 1px solid var(--border-dim);
  border-radius: var(--radius-md);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}}

.panel-toolbar {{
  padding: 10px 14px;
  border-bottom: 1px solid var(--border-dim);
  display: flex;
  justify-content: space-between;
  align-items: center;
  background: var(--bg-card);
}}

.panel-title-group {{
  display: flex;
  align-items: center;
  gap: 8px;
}}

.panel-title {{
  font-size: 12px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}}

.count-badge {{
  background: var(--bg-primary);
  color: var(--text-muted);
  padding: 1px 6px;
  border-radius: var(--radius-sm);
  font-size: 10px;
}}

.table-responsive {{
  overflow-x: auto;
  max-height: calc(100vh - 310px);
}}

table.alphaliner-table {{
  width: 100%;
  border-collapse: collapse;
  text-align: right;
  white-space: nowrap;
}}

table.alphaliner-table th,
table.alphaliner-table td {{
  padding: 6px 10px;
  border-bottom: 1px solid var(--border-dim);
  border-right: 1px solid rgba(19, 39, 56, 0.5);
  font-size: 11px;
}}

table.alphaliner-table th:last-child,
table.alphaliner-table td:last-child {{
  border-right: none;
}}

table.alphaliner-table thead tr:first-child th {{
  background: #091522;
  color: var(--text-secondary);
  font-weight: 700;
  text-transform: uppercase;
  font-size: 10px;
  letter-spacing: 0.5px;
  padding: 6px 10px;
  border-bottom: 1px solid var(--border-light);
}}

table.alphaliner-table thead tr:last-child th {{
  background: #050d17;
  color: var(--text-muted);
  font-weight: 600;
  font-size: 10px;
  cursor: pointer;
  user-select: none;
}}

table.alphaliner-table thead tr:last-child th:hover {{
  color: var(--text-primary);
  background: #0a1b2d;
}}

.th-left, .td-left {{
  text-align: left !important;
}}

.th-center, .td-center {{
  text-align: center !important;
}}

table.alphaliner-table tbody tr {{
  transition: background 0.1s ease;
}}

table.alphaliner-table tbody tr:nth-child(even) {{
  background: rgba(255, 255, 255, 0.015);
}}

table.alphaliner-table tbody tr:hover {{
  background: rgba(56, 139, 253, 0.08);
}}

.op-rank {{
  font-weight: 700;
  color: var(--accent-orange);
}}

.op-name {{
  font-weight: 600;
  color: var(--text-primary);
  letter-spacing: -0.2px;
}}

.op-country {{
  color: var(--text-muted);
  font-size: 10px;
}}

.num-primary {{
  font-weight: 700;
  color: var(--text-primary);
}}

.num-secondary {{
  color: var(--text-secondary);
}}

.num-muted {{
  color: var(--text-muted);
}}

.share-bar-cell {{
  position: relative;
}}

.share-bar-bg {{
  position: absolute;
  top: 4px;
  bottom: 4px;
  right: 0;
  background: rgba(56, 139, 253, 0.12);
  border-left: 2px solid var(--accent-blue);
  pointer-events: none;
  z-index: 1;
}}

.share-bar-val {{
  position: relative;
  z-index: 2;
}}

.delta-val {{
  font-size: 9px;
  margin-left: 4px;
}}

.terminal-footer {{
  background: var(--bg-secondary);
  border-top: 1px solid var(--border-dim);
  padding: 6px 16px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 10px;
  color: var(--text-muted);
}}

.status-indicator {{
  display: flex;
  align-items: center;
  gap: 6px;
}}

.status-dot {{
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent-green);
  box-shadow: 0 0 6px var(--accent-green);
}}
</style>
</head>
<body>

<header class="top-command-bar">
  <div class="brand-section">
    <span class="terminal-tag">ALPHALINER</span>
    <span class="title-text">TOP 100 LINER OPERATORS INTELLIGENCE</span>
  </div>

  <div class="controls-section">
    <input type="text" id="filterInput" class="search-input" placeholder="Search operator / flag..." onkeyup="filterTable()">

    <div class="time-travel-wrap">
      <label for="snapshotSelect">ACTIVE SNAPSHOT:</label>
      <select id="snapshotSelect" onchange="changeSnapshot(this.value)"></select>
    </div>

    <div class="time-travel-wrap">
      <label for="compareSelect">COMPARE TO:</label>
      <select id="compareSelect" onchange="changeCompare(this.value)">
        <option value="">(None - Absolute)</option>
      </select>
    </div>

    <button class="btn btn-accent" onclick="exportCSV()">
      <span>↓ CSV</span>
    </button>
  </div>
</header>

<main class="main-content">
  <div class="kpi-grid">
    <div class="kpi-card accent-blue">
      <div class="kpi-header">
        <span class="kpi-title">Total Ships</span>
        <span class="kpi-sub">GLOBAL TOP 100</span>
      </div>
      <div class="kpi-value" id="kpiTotalShips">--</div>
      <div class="kpi-meta" id="kpiTotalShipsMeta">--</div>
    </div>

    <div class="kpi-card accent-green">
      <div class="kpi-header">
        <span class="kpi-title">Total Fleet TEU</span>
        <span class="kpi-sub">GLOBAL TOP 100</span>
      </div>
      <div class="kpi-value" id="kpiTotalTeu">--</div>
      <div class="kpi-meta" id="kpiTotalTeuMeta">--</div>
    </div>

    <div class="kpi-card accent-amber">
      <div class="kpi-header">
        <span class="kpi-title">Top 10 Concentration</span>
        <span class="kpi-sub">MARKET SHARE (CR10)</span>
      </div>
      <div class="kpi-value" id="kpiTop10Share">--</div>
      <div class="kpi-meta" id="kpiTop10Teu">--</div>
    </div>

    <div class="kpi-card accent-purple">
      <div class="kpi-header">
        <span class="kpi-title">Orderbook</span>
        <span class="kpi-sub">PIPELINE CAPACITY</span>
      </div>
      <div class="kpi-value" id="kpiOrderbookTeu">--</div>
      <div class="kpi-meta">
        <span id="kpiOrderbookShips">--</span>
        <span id="kpiOrderbookShare">--</span>
      </div>
    </div>
  </div>

  <div class="table-panel">
    <div class="panel-toolbar">
      <div class="panel-title-group">
        <span class="panel-title">TOP 100 OPERATOR RANKINGS & FLEET DECOMPOSITION</span>
        <span class="count-badge" id="rowCountBadge">100 OPERATORS</span>
      </div>
      <div id="snapshotTimestamp" style="color: var(--text-dim); font-size: 10px;"></div>
    </div>

    <div class="table-responsive">
      <table class="alphaliner-table" id="operatorTable">
        <thead>
          <tr>
            <th colspan="3" class="th-center">OPERATOR IDENTIFIER</th>
            <th colspan="3" class="th-center" style="background:#0c1e31;">TOTAL FLEET CAPACITY</th>
            <th colspan="3" class="th-center" style="background:#091929;">OWNED FLEET</th>
            <th colspan="3" class="th-center" style="background:#071524;">CHARTERED FLEET</th>
            <th colspan="3" class="th-center" style="background:#13112c;">ORDERBOOK PIPELINE</th>
          </tr>
          <tr>
            <th class="th-center" onclick="sortTable(0)">RK</th>
            <th class="th-left" onclick="sortTable(1)">OPERATOR</th>
            <th class="th-center" onclick="sortTable(2)">FLAG</th>
            <th onclick="sortTable(3)">TOTAL TEU</th>
            <th onclick="sortTable(4)">SHARE %</th>
            <th onclick="sortTable(5)">SHIPS</th>
            <th onclick="sortTable(6)">OWN TEU</th>
            <th onclick="sortTable(7)">OWN %</th>
            <th onclick="sortTable(8)">SHIPS</th>
            <th onclick="sortTable(9)">CHT TEU</th>
            <th onclick="sortTable(10)">CHT %</th>
            <th onclick="sortTable(11)">SHIPS</th>
            <th onclick="sortTable(12)" style="color:var(--accent-purple);">OB TEU</th>
            <th onclick="sortTable(13)">% FLEET</th>
            <th onclick="sortTable(14)">SHIPS</th>
          </tr>
        </thead>
        <tbody id="tableBody"></tbody>
      </table>
    </div>
  </div>
</main>

<footer class="terminal-footer">
  <div class="status-indicator">
    <div class="status-dot"></div>
    <span>ALPHALINER TERMINAL ENGINE: ACTIVE</span>
  </div>
  <div id="footerMeta"></div>
</footer>

<script>
let DATA = null;
let currentSnapshotKey = null;
let compareSnapshotKey = null;
let currentSortCol = 0;
let sortAsc = true;

async function init() {{
  try {{
    const res = await fetch('alphaliner_data.json');
    if (!res.ok) throw new Error('Failed to load alphaliner_data.json');
    DATA = await res.json();
    setupUI();
  }} catch(e) {{
    console.error(e);
  }}
}}

function setupUI() {{
  if (!DATA || !DATA.dates || DATA.dates.length === 0) return;
  currentSnapshotKey = DATA.latest_date;

  const snapSelect = document.getElementById('snapshotSelect');
  const compSelect = document.getElementById('compareSelect');

  snapSelect.innerHTML = '';
  compSelect.innerHTML = '<option value="">(None - Absolute)</option>';

  DATA.dates.forEach((d, idx) => {{
    const opt1 = document.createElement('option');
    opt1.value = d;
    opt1.textContent = d + (idx === 0 ? ' (Latest)' : '');
    snapSelect.appendChild(opt1);

    const opt2 = document.createElement('option');
    opt2.value = d;
    opt2.textContent = d;
    compSelect.appendChild(opt2);
  }});

  snapSelect.value = currentSnapshotKey;
  renderDashboard();
}}

function changeSnapshot(val) {{
  currentSnapshotKey = val;
  renderDashboard();
}}

function changeCompare(val) {{
  compareSnapshotKey = val || null;
  renderDashboard();
}}

function renderDashboard() {{
  const activeSnap = DATA.snapshots[currentSnapshotKey];
  if (!activeSnap) return;

  const compareSnap = compareSnapshotKey ? DATA.snapshots[compareSnapshotKey] : null;
  const compareMap = new Map();
  if (compareSnap && compareSnap.operators) {{
    compareSnap.operators.forEach(op => compareMap.set(op.name, op));
  }}

  const operators = activeSnap.operators || [];

  let totalTeu = 0;
  let totalShips = 0;
  let totalCharteredTeu = 0;
  let totalObTeu = 0;
  let totalObShips = 0;
  let top10Teu = 0;

  operators.forEach((op, idx) => {{
    totalTeu += op.teu;
    totalShips += op.ships;
    totalCharteredTeu += op.chartered_teu;
    totalObTeu += op.orderbook_teu;
    totalObShips += op.orderbook_ships;
    if (idx < 10) top10Teu += op.teu;
  }});

  let compTotalTeu = 0, compTotalShips = 0, compTop10Teu = 0, compObTeu = 0, compObShips = 0;
  if (compareSnap && compareSnap.operators) {{
    compareSnap.operators.forEach((op, idx) => {{
      compTotalTeu += op.teu;
      compTotalShips += op.ships;
      compObTeu += op.orderbook_teu;
      compObShips += op.orderbook_ships;
      if (idx < 10) compTop10Teu += op.teu;
    }});
  }}

  document.getElementById('kpiTotalShips').innerHTML = totalShips.toLocaleString() + ' Ships' + (compareSnapshotKey ? formatDelta(totalShips - compTotalShips) : '');
  document.getElementById('kpiTotalShipsMeta').textContent = operators.length + ' Operators Monitored';

  document.getElementById('kpiTotalTeu').innerHTML = totalTeu.toLocaleString() + ' TEU' + (compareSnapshotKey ? formatDelta(totalTeu - compTotalTeu) : '');
  document.getElementById('kpiTotalTeuMeta').textContent = 'Global Active Capacity';

  const cr10Share = totalTeu > 0 ? ((top10Teu / totalTeu) * 100).toFixed(1) : 0;
  const compCr10Share = compTotalTeu > 0 ? ((compTop10Teu / compTotalTeu) * 100).toFixed(1) : 0;
  const cr10Diff = compareSnapshotKey ? (parseFloat(cr10Share) - parseFloat(compCr10Share)) : 0;

  document.getElementById('kpiTop10Share').innerHTML = cr10Share + '%' + (compareSnapshotKey ? formatDelta(cr10Diff, true) : '');
  document.getElementById('kpiTop10Teu').textContent = top10Teu.toLocaleString() + ' TEU in Top 10';

  document.getElementById('kpiOrderbookTeu').innerHTML = totalObTeu.toLocaleString() + ' TEU' + (compareSnapshotKey ? formatDelta(totalObTeu - compObTeu) : '');
  document.getElementById('kpiOrderbookShips').textContent = totalObShips.toLocaleString() + ' Ships';
  const obRatio = totalTeu > 0 ? ((totalObTeu / totalTeu) * 100).toFixed(1) : 0;
  document.getElementById('kpiOrderbookShare').textContent = ' (' + obRatio + '% of Fleet)';

  document.getElementById('snapshotTimestamp').textContent = 'SNAPSHOT: ' + currentSnapshotKey + (compareSnapshotKey ? ' | VS ' + compareSnapshotKey : '');
  document.getElementById('footerMeta').textContent = 'TOTAL SNAPSHOTS: ' + DATA.dates.length + ' | UPDATED: ' + (DATA.generated_at || '');

  renderTable(operators, compareMap);
}}

function formatDelta(diff, isShare = false) {{
  if (!compareSnapshotKey || diff === undefined || diff === 0) return '';
  const sign = diff > 0 ? '+' : '';
  const cls = diff > 0 ? 'delta-up' : (diff < 0 ? 'delta-down' : 'delta-zero');
  const formatted = isShare ? diff.toFixed(1) + '%' : diff.toLocaleString();
  return `<span class="delta-val ${{cls}}">${{sign}}${{formatted}}</span>`;
}}

function renderTable(operators, compareMap) {{
  const tbody = document.getElementById('tableBody');
  tbody.innerHTML = '';

  const maxTeu = operators.length > 0 ? operators[0].teu : 1;

  operators.forEach(op => {{
    const tr = document.createElement('tr');
    const compOp = compareMap.get(op.name);

    const teuDiff = compOp ? op.teu - compOp.teu : 0;
    const shareDiff = compOp ? op.teu_share - compOp.teu_share : 0;
    const shipsDiff = compOp ? op.ships - compOp.ships : 0;

    const barWidth = Math.min(100, (op.teu / maxTeu) * 100).toFixed(1);

    tr.innerHTML = `
      <td class="td-center op-rank">${{op.rank}}</td>
      <td class="td-left op-name">${{op.name}}</td>
      <td class="td-center op-country">${{op.country || '--'}}</td>

      <td class="num-primary share-bar-cell">
        <div class="share-bar-bg" style="width: ${{barWidth}}%;"></div>
        <span class="share-bar-val">${{op.teu.toLocaleString()}}${{formatDelta(teuDiff)}}</span>
      </td>
      <td class="num-secondary">${{op.teu_share.toFixed(1)}}%${{formatDelta(shareDiff, true)}}</td>
      <td class="num-muted">${{op.ships.toLocaleString()}}${{formatDelta(shipsDiff)}}</td>

      <td class="num-secondary">${{op.owned_teu.toLocaleString()}}</td>
      <td class="num-muted">${{op.owned_share.toFixed(1)}}%</td>
      <td class="num-muted">${{op.owned_ships.toLocaleString()}}</td>

      <td class="num-secondary">${{op.chartered_teu.toLocaleString()}}</td>
      <td class="num-muted">${{op.chartered_share.toFixed(1)}}%</td>
      <td class="num-muted">${{op.chartered_ships.toLocaleString()}}</td>

      <td class="num-primary" style="color:var(--accent-purple);">${{op.orderbook_teu > 0 ? op.orderbook_teu.toLocaleString() : '-'}}</td>
      <td class="num-secondary">${{op.orderbook_teu_share > 0 ? op.orderbook_teu_share.toFixed(1) + '%' : '-'}}</td>
      <td class="num-muted">${{op.orderbook_ships > 0 ? op.orderbook_ships.toLocaleString() : '-'}}</td>
    `;
    tbody.appendChild(tr);
  }});

  document.getElementById('rowCountBadge').textContent = operators.length + ' OPERATORS';
}}

function filterTable() {{
  const query = document.getElementById('filterInput').value.toLowerCase();
  const rows = document.querySelectorAll('#tableBody tr');
  let visible = 0;

  rows.forEach(row => {{
    const text = row.textContent.toLowerCase();
    if (text.includes(query)) {{
      row.style.display = '';
      visible++;
    }} else {{
      row.style.display = 'none';
    }}
  }});

  document.getElementById('rowCountBadge').textContent = visible + ' OPERATORS';
}}

function sortTable(colIndex) {{
  const activeSnap = DATA.snapshots[currentSnapshotKey];
  if (!activeSnap || !activeSnap.operators) return;

  if (currentSortCol === colIndex) {{
    sortAsc = !sortAsc;
  }} else {{
    currentSortCol = colIndex;
    sortAsc = true;
  }}

  const getVal = (op, col) => {{
    switch(col) {{
      case 0: return op.rank;
      case 1: return op.name.toLowerCase();
      case 2: return (op.country || '').toLowerCase();
      case 3: return op.teu;
      case 4: return op.teu_share;
      case 5: return op.ships;
      case 6: return op.owned_teu;
      case 7: return op.owned_share;
      case 8: return op.owned_ships;
      case 9: return op.chartered_teu;
      case 10: return op.chartered_share;
      case 11: return op.chartered_ships;
      case 12: return op.orderbook_teu;
      case 13: return op.orderbook_teu_share;
      case 14: return op.orderbook_ships;
      default: return 0;
    }}
  }};

  activeSnap.operators.sort((a, b) => {{
    const v1 = getVal(a, colIndex);
    const v2 = getVal(b, colIndex);
    if (v1 < v2) return sortAsc ? -1 : 1;
    if (v1 > v2) return sortAsc ? 1 : -1;
    return 0;
  }});

  renderDashboard();
}}

function exportCSV() {{
  const activeSnap = DATA.snapshots[currentSnapshotKey];
  if (!activeSnap || !activeSnap.operators) return;

  const headers = [
    "Rank", "Operator", "Country",
    "Total_TEU", "Total_Share_Pct", "Total_Ships",
    "Owned_TEU", "Owned_Share_Pct", "Owned_Ships",
    "Chartered_TEU", "Chartered_Share_Pct", "Chartered_Ships",
    "Orderbook_TEU", "Orderbook_Share_Pct", "Orderbook_Ships"
  ];

  let csvContent = "\\uFEFF" + headers.join(",") + "\\n";

  activeSnap.operators.forEach(op => {{
    const row = [
      op.rank,
      `"${{op.name.replace(/"/g, '""')}}"`,
      `"${{op.country || ''}}"`,
      op.teu,
      op.teu_share,
      op.ships,
      op.owned_teu,
      op.owned_share,
      op.owned_ships,
      op.chartered_teu,
      op.chartered_share,
      op.chartered_ships,
      op.orderbook_teu,
      op.orderbook_teu_share,
      op.orderbook_ships
    ];
    csvContent += row.join(",") + "\\n";
  }});

  const blob = new Blob([csvContent], {{ type: 'text/csv;charset=utf-8;' }});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `Alphaliner_TOP100_${{currentSnapshotKey}}.csv`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}}

window.addEventListener('DOMContentLoaded', init);
</script>
</body>
</html>"""

    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html_template)
    logger.info(f"Generated Bloomberg static dashboard to {HTML_PATH}")

# =========================================================
# 6. Flask Web Server
# =========================================================
app = Flask(__name__, static_folder=str(BASE_DIR))

@app.route("/")
def index():
    if not HTML_PATH.exists():
        export_json_and_html()
    return send_from_directory(BASE_DIR, "index.html")

@app.route("/alphaliner_data.json")
def get_data_json():
    if not JSON_PATH.exists():
        export_json_and_html()
    return send_from_directory(BASE_DIR, "alphaliner_data.json", mimetype="application/json")

@app.route("/api/scrape", methods=["POST", "GET"])
def trigger_scrape():
    try:
        records = fetch_alphaliner_data()
        date_key = process_and_persist(records)
        export_json_and_html()
        return {"status": "success", "date": date_key, "count": len(records)}
    except Exception as e:
        logger.error(f"API Scrape error: {e}")
        return {"status": "error", "message": str(e)}, 500

# =========================================================
# 7. Main Entry Point
# =========================================================
def main():
    parser = argparse.ArgumentParser(description="Alphaliner TOP 100 Scraper & MI Terminal Dashboard")
    parser.add_argument("--scrape-only", action="store_true", help="Scrape latest data, persist to DB/JSON/HTML, and exit")
    parser.add_argument("--cron", action="store_true", help="Run in cron/headless mode (scrape, export and exit)")
    parser.add_argument("--export-only", action="store_true", help="Export JSON and HTML from current DB without scraping")
    parser.add_argument("--host", default="0.0.0.0", help="Flask host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080, help="Flask port (default: 8080)")

    args, unknown = parser.parse_known_args()
    if unknown:
        logger.info(f"Ignored unknown CLI arguments: {unknown}")

    init_db()

    if args.export_only:
        export_json_and_html()
        sys.exit(0)

    if args.scrape_only or args.cron:
        try:
            records = fetch_alphaliner_data()
            process_and_persist(records)
            export_json_and_html()
            logger.info("Scrape and export completed successfully.")
            sys.exit(0)
        except Exception as e:
            logger.error(f"Scrape failed: {e}")
            sys.exit(1)

    # Standard run
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cnt = conn.cursor().execute("SELECT count(*) FROM snapshot_meta").fetchone()[0]
        if cnt == 0:
            logger.info("Database is empty. Initiating initial scrape...")
            records = fetch_alphaliner_data()
            process_and_persist(records)
    except Exception as e:
        logger.warning(f"Initial scrape attempt warning: {e}")

    export_json_and_html()
    logger.info(f"Starting Alphaliner Web Server on http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)

if __name__ == "__main__":
    main()
