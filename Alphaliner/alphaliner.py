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
from selenium.common.exceptions import TimeoutException, WebDriverException
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
EXTJS_WAIT_TIMEOUT_SECONDS = 35
EXTJS_POLL_INTERVAL_SECONDS = 1.0

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

        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_op_snap_date "
            "ON operator_snapshots(snapshot_date)"
        )

        conn.commit()
        conn.close()

def has_existing_data_in_db():
    """기존 DB에 대시보드 생성 가능한 스냅샷 데이터가 있는지 확인합니다."""
    if not DB_PATH.exists():
        return False

    try:
        with db_lock:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()

            cur.execute("""
                SELECT COUNT(*)
                FROM snapshot_meta
            """)
            snapshot_count = cur.fetchone()[0]

            cur.execute("""
                SELECT COUNT(*)
                FROM operator_snapshots
            """)
            operator_count = cur.fetchone()[0]

            conn.close()

        return snapshot_count > 0 and operator_count >= MIN_EXPECTED_OPERATOR_COUNT

    except sqlite3.Error as exc:
        logger.warning("Unable to check existing database fallback status: %s", exc)
        return False

def save_snapshot_to_db(meta, operators):
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        snap_date = meta["snapshot_date"]

        cur.execute("""
            INSERT OR REPLACE INTO snapshot_meta
            (
                snapshot_date,
                global_total_teu,
                global_total_ships,
                top100_total_teu,
                top100_total_ships,
                top10_total_teu,
                top10_share,
                orderbook_total_teu,
                collected_at
            )
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

        cur.execute(
            "DELETE FROM operator_snapshots WHERE snapshot_date = ?",
            (snap_date,)
        )

        for op in operators:
            cur.execute("""
                INSERT INTO operator_snapshots
                (
                    snapshot_date,
                    rank,
                    operator,
                    total_teu,
                    total_ships,
                    market_share,
                    owned_teu,
                    owned_ships,
                    owned_share,
                    chartered_teu,
                    chartered_ships,
                    chartered_share,
                    orderbook_teu,
                    orderbook_ships,
                    orderbook_share
                )
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

        logger.info(
            "Database successfully updated for snapshot date: %s",
            snap_date
        )

def get_all_snapshots_data():
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute("""
            SELECT *
            FROM snapshot_meta
            ORDER BY snapshot_date DESC
        """)
        meta_rows = cur.fetchall()

        result = {
            "dates": [],
            "snapshots": {}
        }

        for meta_row in meta_rows:
            snapshot_date = meta_row["snapshot_date"]
            result["dates"].append(snapshot_date)

            cur.execute("""
                SELECT *
                FROM operator_snapshots
                WHERE snapshot_date = ?
                ORDER BY rank ASC
            """, (snapshot_date,))

            operator_rows = cur.fetchall()

            result["snapshots"][snapshot_date] = {
                "meta": dict(meta_row),
                "operators": [dict(row) for row in operator_rows]
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
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-infobars")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--lang=en-US,en")
    options.add_argument("--remote-allow-origins=*")

    options.add_argument(
        "--user-agent=Mozilla/5.0 "
        "(Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    options.add_experimental_option(
        "excludeSwitches",
        ["enable-automation", "enable-logging"]
    )
    options.add_experimental_option("useAutomationExtension", False)

    chrome_binary = (
        os.getenv("CHROME_BINARY", "").strip()
        or os.getenv("GOOGLE_CHROME_BIN", "").strip()
    )

    if chrome_binary:
        chrome_binary_path = Path(chrome_binary)

        if chrome_binary_path.exists():
            options.binary_location = str(chrome_binary_path)
            logger.info(
                "Using Chrome binary: %s",
                chrome_binary_path
            )
        else:
            logger.warning(
                "Configured Chrome binary was not found: %s",
                chrome_binary
            )

    chromedriver_path = (
        os.getenv("CHROMEDRIVER", "").strip()
        or os.getenv("CHROMEDRIVER_PATH", "").strip()
    )

    try:
        if chromedriver_path:
            chromedriver_file = Path(chromedriver_path)

            if chromedriver_file.exists():
                logger.info(
                    "Using ChromeDriver from environment: %s",
                    chromedriver_file
                )

                driver = webdriver.Chrome(
                    service=Service(str(chromedriver_file)),
                    options=options
                )
            else:
                logger.warning(
                    "Configured ChromeDriver was not found: %s. "
                    "Falling back to Selenium Manager.",
                    chromedriver_path
                )

                driver = webdriver.Chrome(options=options)
        else:
            driver = webdriver.Chrome(options=options)

        # 페이지가 로드되기 전에 webdriver 속성을 처리해야 하므로
        # execute_script가 아닌 CDP NewDocument 스크립트를 사용합니다.
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": """
                    Object.defineProperty(navigator, 'webdriver', {
                        get: () => undefined
                    });

                    Object.defineProperty(navigator, 'languages', {
                        get: () => ['en-US', 'en']
                    });

                    Object.defineProperty(navigator, 'plugins', {
                        get: () => [1, 2, 3, 4, 5]
                    });
                """
            }
        )

        driver.set_page_load_timeout(60)
        driver.implicitly_wait(2)

        return driver

    except WebDriverException as exc:
        logger.exception("Failed to initialize Selenium Driver.")

        raise RuntimeError(
            "Chrome/ChromeDriver initialization failed. "
            "Verify Chrome, ChromeDriver, and GitHub Actions environment configuration."
        ) from exc

def wait_for_extjs_store(driver):
    """
    ExtJS가 준비되고 Top100description Store 또는 Grid Store에
    최소 MIN_EXPECTED_OPERATOR_COUNT 건 이상의 레코드가 적재될 때까지 대기합니다.
    """
    readiness_script = f"""
        try {{
            if (typeof Ext === 'undefined' || !Ext.isReady) {{
                return {{
                    ready: false,
                    count: 0,
                    source: 'Ext not ready'
                }};
            }}

            var store = null;
            var source = '';

            if (
                Ext.data &&
                Ext.data.StoreManager &&
                Ext.data.StoreManager.lookup
            ) {{
                store = Ext.data.StoreManager.lookup('Top100description');

                if (store) {{
                    source = 'Top100description';
                }}
            }}

            if (!store && Ext.ComponentQuery) {{
                var grids = Ext.ComponentQuery.query('gridpanel');

                for (var i = 0; i < grids.length; i++) {{
                    if (grids[i] && grids[i].getStore) {{
                        var candidate = grids[i].getStore();

                        if (candidate && candidate.getCount() > 0) {{
                            store = candidate;
                            source = 'gridpanel[' + i + ']';
                            break;
                        }}
                    }}
                }}
            }}

            var count = store && store.getCount ? store.getCount() : 0;

            return {{
                ready: Boolean(store && count >= {MIN_EXPECTED_OPERATOR_COUNT}),
                count: count,
                source: source || 'No store found'
            }};
        }} catch (e) {{
            return {{
                ready: false,
                count: 0,
                source: 'Error: ' + e.toString()
            }};
        }}
    """

    last_status = {
        "ready": False,
        "count": 0,
        "source": "Not checked"
    }

    def store_is_ready(browser):
        nonlocal last_status

        last_status = browser.execute_script(readiness_script)

        if last_status.get("ready"):
            logger.info(
                "ExtJS data store is ready: source=%s, records=%s",
                last_status.get("source"),
                last_status.get("count")
            )
            return True

        logger.info(
            "Waiting for ExtJS store: source=%s, records=%s",
            last_status.get("source"),
            last_status.get("count")
        )

        return False

    try:
        WebDriverWait(
            driver,
            EXTJS_WAIT_TIMEOUT_SECONDS,
            poll_frequency=EXTJS_POLL_INTERVAL_SECONDS
        ).until(store_is_ready)

    except TimeoutException as exc:
        raise RuntimeError(
            "ExtJS store loading timeout. "
            f"Last status: source={last_status.get('source')}, "
            f"records={last_status.get('count')}, "
            f"expected>={MIN_EXPECTED_OPERATOR_COUNT}."
        ) from exc

def parse_numeric_value(value):
    if value is None:
        return 0

    if isinstance(value, (int, float)):
        return value

    numeric_text = re.sub(r"[^\d.]", "", str(value))

    if not numeric_text:
        return 0

    return float(numeric_text) if "." in numeric_text else int(numeric_text)

def crawl_alphaliner():
    logger.info("Initializing Selenium Driver for Alphaliner...")

    driver = None

    try:
        driver = get_chrome_driver(headless=True)

        logger.info("Loading Alphaliner URL: %s", TARGET_URL)
        driver.get(TARGET_URL)

        logger.info(
            "Waiting for ExtJS store to contain at least %s records...",
            MIN_EXPECTED_OPERATOR_COUNT
        )

        wait_for_extjs_store(driver)

        js_extractor = """
        var result = {
            meta: {},
            records: [],
            rawText: '',
            source: ''
        };

        try {
            var store = null;

            if (
                typeof Ext !== 'undefined' &&
                Ext.data &&
                Ext.data.StoreManager &&
                Ext.data.StoreManager.lookup
            ) {
                store = Ext.data.StoreManager.lookup('Top100description');

                if (store) {
                    result.source = 'Top100description';
                }
            }

            if (!store && typeof Ext !== 'undefined' && Ext.ComponentQuery) {
                var grids = Ext.ComponentQuery.query('gridpanel');

                for (var i = 0; i < grids.length; i++) {
                    if (grids[i] && grids[i].getStore) {
                        var candidate = grids[i].getStore();

                        if (candidate && candidate.getCount() > 0) {
                            store = candidate;
                            result.source = 'gridpanel[' + i + ']';
                            break;
                        }
                    }
                }
            }

            if (store) {
                store.each(function(rec) {
                    result.records.push(rec.data);
                });
            }

            result.rawText = document.body ? document.body.innerText : '';
        } catch (e) {
            result.error = e.toString();
        }

        return result;
        """

        data = driver.execute_script(js_extractor)
        records = data.get("records", [])

        logger.info(
            "ExtJS extraction completed: source=%s, raw_records=%s",
            data.get("source", "unknown"),
            len(records)
        )

        if not records:
            raise RuntimeError(
                "ExtJS store was ready but data extraction returned zero records."
            )

        today_str = datetime.now().strftime("%Y-%m-%d")
        operators = []
        orderbook_total_teu = 0

        for record in records:
            rank = int(parse_numeric_value(
                record.get("rank")
                or record.get("Rank")
                or record.get("id")
                or 0
            ))

            if rank <= 0 or rank > 100:
                continue

            operator_name = str(
                record.get("operator")
                or record.get("Operator")
                or record.get("liner")
                or record.get("name")
                or f"Operator {rank}"
            ).strip()

            total_teu = int(parse_numeric_value(
                record.get("tot_teu")
                or record.get("total_teu")
                or record.get("teu")
                or 0
            ))

            total_ships = int(parse_numeric_value(
                record.get("tot_ships")
                or record.get("total_ships")
                or record.get("ships")
                or 0
            ))

            market_share = float(parse_numeric_value(
                record.get("share")
                or record.get("market_share")
                or 0.0
            ))

            owned_teu = int(parse_numeric_value(
                record.get("owned_teu")
                or 0
            ))

            owned_ships = int(parse_numeric_value(
                record.get("owned_ships")
                or 0
            ))

            owned_share = float(parse_numeric_value(
                record.get("owned_share")
                or 0.0
            ))

            chartered_teu = int(parse_numeric_value(
                record.get("chartered_teu")
                or record.get("cha_teu")
                or 0
            ))

            chartered_ships = int(parse_numeric_value(
                record.get("chartered_ships")
                or record.get("cha_ships")
                or 0
            ))

            chartered_share = float(parse_numeric_value(
                record.get("chartered_share")
                or record.get("cha_share")
                or 0.0
            ))

            orderbook_teu = int(parse_numeric_value(
                record.get("orderbook_teu")
                or record.get("ord_teu")
                or 0
            ))

            orderbook_ships = int(parse_numeric_value(
                record.get("orderbook_ships")
                or record.get("ord_ships")
                or 0
            ))

            orderbook_share = float(parse_numeric_value(
                record.get("orderbook_share")
                or record.get("ord_share")
                or 0.0
            ))

            orderbook_total_teu += orderbook_teu

            operators.append({
                "rank": rank,
                "operator": operator_name,
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

        operators = sorted(operators, key=lambda item: item["rank"])

        if len(operators) < MIN_EXPECTED_OPERATOR_COUNT:
            raise RuntimeError(
                "Alphaliner validation failed: "
                f"extracted {len(operators)} valid operators; "
                f"expected >= {MIN_EXPECTED_OPERATOR_COUNT}."
            )

        top100_teu = sum(operator["total_teu"] for operator in operators)
        top100_ships = sum(operator["total_ships"] for operator in operators)
        top10_teu = sum(operator["total_teu"] for operator in operators[:10])

        raw_text = data.get("rawText", "")

        global_teu_match = re.search(
            r"Total:\s*([\d,]+)\s*TEU",
            raw_text
        )

        global_ships_match = re.search(
            r"Total:\s*[\d,]+\s*TEU\s*\(\s*([\d,]+)\s*Ships\)",
            raw_text
        )

        global_total_teu = (
            int(re.sub(r"[^\d]", "", global_teu_match.group(1)))
            if global_teu_match
            else int(top100_teu / 0.95)
        )

        global_total_ships = (
            int(re.sub(r"[^\d]", "", global_ships_match.group(1)))
            if global_ships_match
            else int(top100_ships / 0.90)
        )

        top10_share = round(
            (top10_teu / global_total_teu * 100),
            2
        ) if global_total_teu > 0 else 0.0

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

        logger.info(
            "Crawl completed successfully: %s operators processed.",
            len(operators)
        )

        return True

    except Exception as exc:
        logger.error(
            "Alphaliner crawl failed: %s",
            exc,
            exc_info=True
        )
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

select,
input[type="text"] {
  background: var(--bg-tertiary);
  border: 1px solid var(--border-subtle);
  color: var(--text-main);
  font-family: var(--font-mono);
  font-size: 12px;
  padding: 4px 8px;
  border-radius: 2px;
  outline: none;
}

select:focus,
input[type="text"]:focus {
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

.col-chart {
  grid-column: span 12;
  height: 280px;
}

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

th.align-left,
td.align-left {
  text-align: left;
}

th.align-center,
td.align-center {
  text-align: center;
}

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
  .col-kpi {
    grid-column: span 6;
  }
}

@media (max-width: 768px) {
  .col-kpi {
    grid-column: span 12;
  }

  .terminal-header {
    flex-direction: column;
    gap: 8px;
    align-items: flex-start;
  }

  .terminal-controls {
    flex-wrap: wrap;
    width: 100%;
    justify-content: space-between;
  }
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

  <section class="panel col-chart">
    <div class="panel-header">
      <span>TOP 10 OPERATOR CAPACITY DISTRIBUTION (TEU)</span>
      <span id="chartSubTitle" style="color: var(--accent-orange);"></span>
    </div>

    <div class="panel-body" style="position: relative; height: 100%;">
      <canvas id="topChart"></canvas>
    </div>
  </section>

  <section class="panel col-table">
    <div class="panel-header">
      <span>TOP 100 LINER OPERATORS BREAKDOWN</span>
      <input
        type="text"
        id="filterInput"
        placeholder="Filter operator..."
        onkeyup="filterTable()"
        style="width: 180px;"
      >
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
  return Number(n).toLocaleString("en-US");
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
    const response = await fetch("alphaliner_data.json?t=" + Date.now());
    DATA_STORE = await response.json();

    setupDateSelectors();
    renderAll();
  } catch (error) {
    console.error("Dashboard initialization error:", error);

    document.getElementById("systemStatus").innerText =
      "STATUS: DATA LOAD FAILURE";

    document.getElementById("systemStatus")
      .previousElementSibling
      .style
      .background = "var(--accent-red)";
  }
}

function setupDateSelectors() {
  const dateSelect = document.getElementById("dateSelect");
  const compareSelect = document.getElementById("compareSelect");

  dateSelect.innerHTML = "";
  compareSelect.innerHTML = "";

  if (!DATA_STORE.dates || DATA_STORE.dates.length === 0) {
    return;
  }

  DATA_STORE.dates.forEach(date => {
    dateSelect.add(new Option(date, date));
  });

  compareSelect.add(new Option("NONE", "none"));

  DATA_STORE.dates.forEach(date => {
    compareSelect.add(new Option(date, date));
  });

  if (DATA_STORE.dates.length > 1) {
    compareSelect.selectedIndex = 2;
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
  const compareDate = document.getElementById("compareSelect").value;

  if (!DATA_STORE.snapshots[activeDate]) {
    return;
  }

  const currentSnapshot = DATA_STORE.snapshots[activeDate];

  const compareSnapshot =
    compareDate !== "none" && DATA_STORE.snapshots[compareDate]
      ? DATA_STORE.snapshots[compareDate]
      : null;

  renderKPIs(currentSnapshot, compareSnapshot);
  renderChart(currentSnapshot);
  renderTable(currentSnapshot, compareSnapshot);

  document.getElementById("collectedTimestamp").innerText =
    `DATA COLLECTED: ${currentSnapshot.meta.collected_at || activeDate}`;
}

function renderKPIs(currentSnapshot, compareSnapshot) {
  const currentMeta = currentSnapshot.meta;
  const previousMeta = compareSnapshot ? compareSnapshot.meta : null;

  document.getElementById("kpiGlobalTeu").innerText =
    `${fmt(currentMeta.global_total_teu)} TEU`;

  document.getElementById("kpiGlobalShips").innerText =
    `${fmt(currentMeta.global_total_ships)} Ships`;

  document.getElementById("deltaGlobalTeu").innerHTML =
    previousMeta
      ? fmtDelta(currentMeta.global_total_teu - previousMeta.global_total_teu) + " TEU"
      : "";

  document.getElementById("kpiTop100Teu").innerText =
    `${fmt(currentMeta.top100_total_teu)} TEU`;

  document.getElementById("kpiTop100Ships").innerText =
    `${fmt(currentMeta.top100_total_ships)} Ships`;

  document.getElementById("deltaTop100Teu").innerHTML =
    previousMeta
      ? fmtDelta(currentMeta.top100_total_teu - previousMeta.top100_total_teu) + " TEU"
      : "";

  document.getElementById("kpiTop10Share").innerText =
    `${currentMeta.top10_share ? currentMeta.top10_share.toFixed(1) : "0.0"} %`;

  document.getElementById("kpiTop10Teu").innerText =
    `${fmt(currentMeta.top10_total_teu)} TEU`;

  document.getElementById("deltaTop10Share").innerHTML =
    previousMeta
      ? fmtDelta(currentMeta.top10_share - previousMeta.top10_share, true)
      : "";

  document.getElementById("kpiOrderbookTeu").innerText =
    `${fmt(currentMeta.orderbook_total_teu)} TEU`;

  const orderbookRatio =
    currentMeta.top100_total_teu > 0
      ? ((currentMeta.orderbook_total_teu / currentMeta.top100_total_teu) * 100).toFixed(1)
      : "0.0";

  document.getElementById("kpiOrderbookRatio").innerText =
    `${orderbookRatio} % of active`;

  document.getElementById("deltaOrderbookTeu").innerHTML =
    previousMeta
      ? fmtDelta(
          currentMeta.orderbook_total_teu -
          previousMeta.orderbook_total_teu
        ) + " TEU"
      : "";
}

function renderChart(snapshot) {
  const top10 = snapshot.operators.slice(0, 10);

  const labels = top10.map(operator =>
    `${operator.rank}. ${operator.operator}`
  );

  const owned = top10.map(operator => operator.owned_teu);
  const chartered = top10.map(operator => operator.chartered_teu);
  const orderbook = top10.map(operator => operator.orderbook_teu);

  document.getElementById("chartSubTitle").innerText =
    `AS OF ${snapshot.meta.snapshot_date}`;

  if (currentChart) {
    currentChart.destroy();
  }

  const context = document.getElementById("topChart").getContext("2d");

  currentChart = new Chart(context, {
    type: "bar",
    data: {
      labels: labels,
      datasets: [
        {
          label: "Owned TEU",
          data: owned,
          backgroundColor: "#45a9f5",
          stack: "active"
        },
        {
          label: "Chartered TEU",
          data: chartered,
          backgroundColor: "#f6a623",
          stack: "active"
        },
        {
          label: "Orderbook TEU",
          data: orderbook,
          backgroundColor: "rgba(187, 134, 252, 0.4)",
          borderColor: "#bb86fc",
          borderWidth: 1,
          stack: "orderbook"
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          position: "top",
          labels: {
            color: "#879caf",
            font: {
              family: "JetBrains Mono",
              size: 11
            }
          }
        }
      },
      scales: {
        x: {
          stacked: true,
          ticks: {
            color: "#879caf",
            font: {
              family: "JetBrains Mono",
              size: 10
            }
          },
          grid: {
            color: "rgba(27, 48, 67, 0.5)"
          }
        },
        y: {
          stacked: true,
          ticks: {
            color: "#879caf",
            font: {
              family: "JetBrains Mono",
              size: 10
            },
            callback: value => (value / 1000000).toFixed(1) + "M"
          },
          grid: {
            color: "rgba(27, 48, 67, 0.5)"
          }
        }
      }
    }
  });
}

function renderTable(currentSnapshot, compareSnapshot) {
  const tableBody = document.getElementById("tableBody");
  tableBody.innerHTML = "";

  const compareMap = {};

  if (compareSnapshot && compareSnapshot.operators) {
    compareSnapshot.operators.forEach(operator => {
      compareMap[operator.operator.toLowerCase()] = operator;
    });
  }

  currentSnapshot.operators.forEach(operator => {
    const previous = compareMap[operator.operator.toLowerCase()];
    const row = document.createElement("tr");

    let rankDelta = "";
    let teuDelta = "";
    let shipsDelta = "";

    if (previous) {
      const rankDifference = previous.rank - operator.rank;

      if (rankDifference !== 0) {
        const cssClass = rankDifference > 0 ? "delta-pos" : "delta-neg";
        const symbol = rankDifference > 0 ? "▲" : "▼";

        rankDelta =
          `<span class="${cssClass} delta-inline">` +
          `${symbol}${Math.abs(rankDifference)}</span>`;
      }

      teuDelta =
        `<div class="delta-inline">` +
        `${fmtDelta(operator.total_teu - previous.total_teu)}` +
        `</div>`;

      shipsDelta =
        `<div class="delta-inline">` +
        `${fmtDelta(operator.total_ships - previous.total_ships)}` +
        `</div>`;
    }

    const rankClass =
      operator.rank <= 10
        ? "rank-badge rank-top10"
        : "rank-badge";

    row.innerHTML = `
      <td class="align-center">
        <span class="${rankClass}">${operator.rank}</span>${rankDelta}
      </td>
      <td class="align-left" style="font-weight: 600;">
        ${operator.operator}
      </td>
      <td>
        <strong>${fmt(operator.total_teu)}</strong>${teuDelta}
      </td>
      <td>
        ${fmt(operator.total_ships)}${shipsDelta}
      </td>
      <td>${Number(operator.market_share || 0).toFixed(1)}%</td>
      <td>${fmt(operator.owned_teu)}</td>
      <td>${fmt(operator.owned_ships)}</td>
      <td>${Number(operator.owned_share || 0).toFixed(1)}%</td>
      <td>${fmt(operator.chartered_teu)}</td>
      <td>${fmt(operator.chartered_ships)}</td>
      <td>${Number(operator.chartered_share || 0).toFixed(1)}%</td>
      <td style="color: var(--accent-purple);">
        ${fmt(operator.orderbook_teu)}
      </td>
      <td style="color: var(--accent-purple);">
        ${fmt(operator.orderbook_ships)}
      </td>
      <td style="color: var(--accent-purple);">
        ${Number(operator.orderbook_share || 0).toFixed(1)}%
      </td>
    `;

    tableBody.appendChild(row);
  });
}

function filterTable() {
  const query = document.getElementById("filterInput").value.toLowerCase();
  const rows = document.querySelectorAll("#tableBody tr");

  rows.forEach(row => {
    const operator = row.cells[1].innerText.toLowerCase();
    row.style.display = operator.includes(query) ? "" : "none";
  });
}

function exportCSV() {
  const activeDate = document.getElementById("dateSelect").value;

  if (!DATA_STORE || !DATA_STORE.snapshots[activeDate]) {
    return;
  }

  const snapshot = DATA_STORE.snapshots[activeDate];

  let csv = "\uFEFF";

  csv +=
    "Rank,Operator,Total TEU,Total Ships,Market Share %," +
    "Owned TEU,Owned Ships,Owned Share %," +
    "Chartered TEU,Chartered Ships,Chartered Share %," +
    "Orderbook TEU,Orderbook Ships,Orderbook % of Fleet\n";

  snapshot.operators.forEach(operator => {
    csv +=
      `"${operator.rank}",` +
      `"${operator.operator.replace(/"/g, '""')}",` +
      `${operator.total_teu},` +
      `${operator.total_ships},` +
      `${operator.market_share},` +
      `${operator.owned_teu},` +
      `${operator.owned_ships},` +
      `${operator.owned_share},` +
      `${operator.chartered_teu},` +
      `${operator.chartered_ships},` +
      `${operator.chartered_share},` +
      `${operator.orderbook_teu},` +
      `${operator.orderbook_ships},` +
      `${operator.orderbook_share}\n`;
  });

  const blob = new Blob(
    [csv],
    { type: "text/csv;charset=utf-8;" }
  );

  const link = document.createElement("a");

  link.href = URL.createObjectURL(blob);

  link.setAttribute(
    "download",
    `Alphaliner_TOP100_${activeDate}.csv`
  );

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
        logger.warning(
            "No snapshot records found in database. "
            "Skipping static artifact generation."
        )
        return False

    with open(JSON_PATH, "w", encoding="utf-8") as file:
        json.dump(
            data,
            file,
            ensure_ascii=False,
            indent=2
        )

    logger.info("Generated static data JSON: %s", JSON_PATH)

    with open(HTML_PATH, "w", encoding="utf-8") as file:
        file.write(HTML_TEMPLATE)

    logger.info("Generated static HTML dashboard: %s", HTML_PATH)

    return True

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
        if has_existing_data_in_db():
            generate_static_files()

            return jsonify({
                "status": "fallback",
                "message": (
                    "Live crawl failed, but static artifacts were rebuilt "
                    "from the existing historical database."
                )
            }), 200

        return jsonify({
            "status": "error",
            "message": (
                "Crawler failed and no usable historical snapshot exists "
                "for fallback dashboard generation."
            )
        }), 502

    generate_static_files()

    return jsonify({
        "status": "success",
        "message": "Crawler executed and static files refreshed."
    })

# =========================================================
# 7. Main Execution Router
# =========================================================
def main():
    parser = argparse.ArgumentParser(
        description="Alphaliner TOP 100 Bloomberg MI Terminal Dashboard"
    )

    parser.add_argument(
        "--no-crawl",
        action="store_true",
        help="Skip crawling and generate files from DB"
    )

    parser.add_argument(
        "--cron",
        action="store_true",
        help="Run crawl once, generate static files and exit"
    )

    parser.add_argument(
        "--port",
        type=int,
        default=8050,
        help="Port to run Flask server on (default: 8050)"
    )

    parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="Host IP to bind (default: 0.0.0.0)"
    )

    args = parser.parse_args()

    init_db()

    if args.no_crawl:
        logger.info(
            "Crawling skipped by --no-crawl flag. "
            "Generating static artifacts from existing database."
        )

        generate_static_files()

    else:
        logger.info("Executing scheduled crawl routine...")

        crawl_success = crawl_alphaliner()

        if crawl_success:
            generate_static_files()

        else:
            logger.warning(
                "Live crawler validation failed. "
                "Attempting static artifact generation from existing database."
            )

            if has_existing_data_in_db():
                generate_static_files()

                logger.warning(
                    "Fallback dashboard generation completed using "
                    "the previous valid database snapshot."
                )

            else:
                logger.error(
                    "No usable historical database snapshot exists. "
                    "Static files cannot be generated."
                )

                if args.cron:
                    sys.exit(1)

    if args.cron:
        logger.info("Cron execution complete.")
        return

    logger.info(
        "Starting Bloomberg MI Terminal Server on http://%s:%s",
        args.host,
        args.port
    )

    app.run(
        host=args.host,
        port=args.port,
        debug=False
    )

if __name__ == "__main__":
    main()
