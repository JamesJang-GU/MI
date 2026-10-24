# -*- coding: utf-8 -*-
"""
Alphaliner TOP 100 Liner Operators Static Dashboard Crawler
- Direct ExtJS Store extraction
- Historical snapshot storage
- Static JSON / HTML dashboard generation
- Chartered fleet field mapping and derived-value fallback enabled

Output files:
- alphaliner_history.db
- alphaliner_data.json
- index.html
"""

import argparse
import html
import json
import logging
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from threading import Lock

from flask import Flask, jsonify, send_from_directory
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
    datefmt="%Y-%m-%d %H:%M:%S",
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

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_op_snap_date
            ON operator_snapshots(snapshot_date)
        """)

        conn.commit()
        conn.close()

def has_existing_data_in_db():
    if not DB_PATH.exists():
        return False

    try:
        with db_lock:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()

            cur.execute("SELECT COUNT(*) FROM snapshot_meta")
            snapshot_count = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM operator_snapshots")
            operator_count = cur.fetchone()[0]

            conn.close()

        return (
            snapshot_count > 0
            and operator_count >= MIN_EXPECTED_OPERATOR_COUNT
        )

    except sqlite3.Error as exc:
        logger.warning(
            "Unable to check existing database fallback status: %s",
            exc,
        )
        return False

def save_snapshot_to_db(meta, operators):
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()

        snapshot_date = meta["snapshot_date"]

        cur.execute("""
            INSERT OR REPLACE INTO snapshot_meta (
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
            snapshot_date,
            meta.get("global_total_teu", 0),
            meta.get("global_total_ships", 0),
            meta.get("top100_total_teu", 0),
            meta.get("top100_total_ships", 0),
            meta.get("top10_total_teu", 0),
            meta.get("top10_share", 0.0),
            meta.get("orderbook_total_teu", 0),
            meta.get("collected_at", datetime.now().isoformat()),
        ))

        # 동일 일자 재실행 시 기존 행을 제거하여 정정된 값을 저장
        cur.execute(
            "DELETE FROM operator_snapshots WHERE snapshot_date = ?",
            (snapshot_date,),
        )

        for op in operators:
            cur.execute("""
                INSERT INTO operator_snapshots (
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
                snapshot_date,
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
                op["orderbook_share"],
            ))

        conn.commit()
        conn.close()

    logger.info(
        "Database successfully updated for snapshot date: %s",
        snapshot_date,
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
            "snapshots": {},
        }

        for meta_row in meta_rows:
            snapshot_date = meta_row["snapshot_date"]

            cur.execute("""
                SELECT *
                FROM operator_snapshots
                WHERE snapshot_date = ?
                ORDER BY rank ASC
            """, (snapshot_date,))
            operator_rows = cur.fetchall()

            result["dates"].append(snapshot_date)
            result["snapshots"][snapshot_date] = {
                "meta": dict(meta_row),
                "operators": [dict(row) for row in operator_rows],
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
        ["enable-automation", "enable-logging"],
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
            logger.info("Using Chrome binary: %s", chrome_binary_path)
        else:
            logger.warning(
                "Configured Chrome binary was not found: %s",
                chrome_binary,
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
                    chromedriver_file,
                )
                driver = webdriver.Chrome(
                    service=Service(str(chromedriver_file)),
                    options=options,
                )
            else:
                logger.warning(
                    "Configured ChromeDriver was not found: %s. "
                    "Falling back to Selenium default.",
                    chromedriver_path,
                )
                driver = webdriver.Chrome(options=options)
        else:
            driver = webdriver.Chrome(options=options)

        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": """
                    Object.defineProperty(
                        navigator,
                        'webdriver',
                        { get: () => undefined }
                    );

                    Object.defineProperty(
                        navigator,
                        'languages',
                        { get: () => ['en-US', 'en'] }
                    );

                    Object.defineProperty(
                        navigator,
                        'plugins',
                        { get: () => [1, 2, 3, 4, 5] }
                    );
                """,
            },
        )

        driver.set_page_load_timeout(60)
        driver.implicitly_wait(2)

        return driver

    except WebDriverException as exc:
        logger.exception("Failed to initialize Selenium Driver.")

        raise RuntimeError(
            "Chrome/ChromeDriver initialization failed. "
            "Verify Chrome and ChromeDriver configuration."
        ) from exc

def wait_for_extjs_store(driver):
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

                        if (
                            candidate &&
                            candidate.getCount &&
                            candidate.getCount() > 0
                        ) {{
                            store = candidate;
                            source = 'gridpanel[' + i + ']';
                            break;
                        }}
                    }}
                }}
            }}

            var count = store && store.getCount
                ? store.getCount()
                : 0;

            return {{
                ready: Boolean(
                    store && count >= {MIN_EXPECTED_OPERATOR_COUNT}
                ),
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
        "source": "Not checked",
    }

    def store_is_ready(browser):
        nonlocal last_status

        last_status = browser.execute_script(readiness_script)

        if last_status.get("ready"):
            logger.info(
                "ExtJS data store is ready: source=%s, records=%s",
                last_status.get("source"),
                last_status.get("count"),
            )
            return True

        logger.info(
            "Waiting for ExtJS store: source=%s, records=%s",
            last_status.get("source"),
            last_status.get("count"),
        )
        return False

    try:
        WebDriverWait(
            driver,
            EXTJS_WAIT_TIMEOUT_SECONDS,
            poll_frequency=EXTJS_POLL_INTERVAL_SECONDS,
        ).until(store_is_ready)

    except TimeoutException as exc:
        raise RuntimeError(
            "ExtJS store loading timeout. "
            f"Last status: source={last_status.get('source')}, "
            f"records={last_status.get('count')}, "
            f"expected>={MIN_EXPECTED_OPERATOR_COUNT}."
        ) from exc

def normalize_key(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())

def parse_numeric_value(value):
    if value is None:
        return 0

    if isinstance(value, bool):
        return int(value)

    if isinstance(value, (int, float)):
        return value

    numeric_text = re.sub(r"[^\d.\-]", "", str(value))

    if not numeric_text or numeric_text in {"-", ".", "-."}:
        return 0

    try:
        return float(numeric_text) if "." in numeric_text else int(numeric_text)
    except ValueError:
        return 0

def clean_operator_name(value):
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def safe_percent(numerator, denominator):
    if not denominator:
        return 0.0

    return round((float(numerator) / float(denominator)) * 100, 2)

def crawl_alphaliner():
    logger.info("Initializing Selenium Driver for Alphaliner...")
    driver = None

    try:
        driver = get_chrome_driver(headless=True)

        logger.info("Loading Alphaliner URL: %s", TARGET_URL)
        driver.get(TARGET_URL)

        logger.info(
            "Waiting for ExtJS store to contain at least %s records...",
            MIN_EXPECTED_OPERATOR_COUNT,
        )
        wait_for_extjs_store(driver)

        js_extractor = """
            var result = {
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
                    store = Ext.data.StoreManager.lookup(
                        'Top100description'
                    );

                    if (store) {
                        result.source = 'Top100description';
                    }
                }

                if (
                    !store &&
                    typeof Ext !== 'undefined' &&
                    Ext.ComponentQuery
                ) {
                    var grids = Ext.ComponentQuery.query('gridpanel');

                    for (var i = 0; i < grids.length; i++) {
                        if (grids[i] && grids[i].getStore) {
                            var candidate = grids[i].getStore();

                            if (
                                candidate &&
                                candidate.getCount &&
                                candidate.getCount() > 0
                            ) {
                                store = candidate;
                                result.source = 'gridpanel[' + i + ']';
                                break;
                            }
                        }
                    }
                }

                if (store) {
                    store.each(function(rec) {
                        result.records.push(rec.data || rec);
                    });
                }

                result.rawText = document.body
                    ? document.body.innerText
                    : '';

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
            len(records),
        )

        if data.get("error"):
            logger.warning(
                "ExtJS extractor warning: %s",
                data.get("error"),
            )

        if not records:
            raise RuntimeError(
                "ExtJS store was ready but data extraction returned zero records."
            )

        if isinstance(records[0], dict):
            logger.info(
                "Sample raw ExtJS keys: %s",
                list(records[0].keys()),
            )

        snapshot_date = datetime.now().strftime("%Y-%m-%d")
        operators = []
        orderbook_total_teu = 0

        for idx, record in enumerate(records, start=1):
            if not isinstance(record, dict):
                continue

            norm_rec = {
                normalize_key(key): value
                for key, value in record.items()
            }

            def get_field(candidates, default=None):
                """
                Returns:
                    value, matched_normalized_field_name, found
                """
                for candidate in candidates:
                    normalized_candidate = normalize_key(candidate)

                    if (
                        normalized_candidate in norm_rec
                        and norm_rec[normalized_candidate] is not None
                        and str(norm_rec[normalized_candidate]).strip() != ""
                    ):
                        return (
                            norm_rec[normalized_candidate],
                            normalized_candidate,
                            True,
                        )

                return default, None, False

            rank_raw, rank_key, _ = get_field([
                "rank",
                "ranking",
                "pos",
                "position",
                "id",
                "idx",
                "num",
                "no",
            ], idx)

            rank_value = parse_numeric_value(rank_raw)
            rank = int(rank_value) if rank_value > 0 else idx

            name_raw, name_key, _ = get_field([
                "operator",
                "liner",
                "operatorname",
                "linername",
                "name",
                "carrier",
                "company",
                "label",
                "title",
            ], f"Operator {rank}")

            operator_name = clean_operator_name(name_raw) or f"Operator {rank}"

            total_teu_raw, total_teu_key, total_teu_found = get_field([
                "totteu",
                "totalteu",
                "totalcapacity",
                "fleetcapacity",
                "teu",
                "capacity",
            ])

            total_ships_raw, total_ships_key, total_ships_found = get_field([
                "totships",
                "totalships",
                "totalvessels",
                "fleetships",
                "ships",
                "vessels",
            ])

            market_share_raw, market_share_key, market_share_found = get_field([
                "share",
                "marketshare",
                "market_share",
                "totalshare",
                "fleetshare",
                "totalpercent",
                "pctfleet",
                "percentage",
            ])

            owned_teu_raw, owned_teu_key, owned_teu_found = get_field([
                "ownedteu",
                "ownteu",
                "own_teu",
                "ownedcapacity",
                "ownerteu",
            ])

            owned_ships_raw, owned_ships_key, owned_ships_found = get_field([
                "ownedships",
                "ownships",
                "own_ships",
                "ownedvessels",
                "ownervessels",
            ])

            owned_share_raw, owned_share_key, owned_share_found = get_field([
                "ownedshare",
                "ownshare",
                "own_share",
                "ownedpercent",
                "ownedpercentage",
                "pctowned",
                "pctown",
            ])

            # 실제 Alphaliner ExtJS 필드명 변형을 폭넓게 지원
            chartered_teu_raw, chartered_teu_key, chartered_teu_found = get_field([
                "charteredteu",
                "charterteu",
                "chateu",
                "chartteu",
                "chart_teu",
                "charteu",
                "chtteu",
                "leasedteu",
            ])

            chartered_ships_raw, chartered_ships_key, chartered_ships_found = get_field([
                "charteredships",
                "charterships",
                "chaships",
                "chartships",
                "chart_ships",
                "chartship",
                "charships",
                "chtships",
                "leasedships",
            ])

            chartered_share_raw, chartered_share_key, chartered_share_found = get_field([
                "charteredshare",
                "chartershare",
                "chashare",
                "chartshare",
                "chart_share",
                "chartpercent",
                "charteredpercent",
                "charterpercent",
                "pctchartered",
                "pctchart",
            ])

            orderbook_teu_raw, orderbook_teu_key, orderbook_teu_found = get_field([
                "orderbookteu",
                "orderbookcapacity",
                "ordteu",
                "orderteu",
                "obteu",
            ])

            orderbook_ships_raw, orderbook_ships_key, orderbook_ships_found = get_field([
                "orderbookships",
                "orderbookvessels",
                "ordships",
                "orderships",
                "obships",
            ])

            orderbook_share_raw, orderbook_share_key, orderbook_share_found = get_field([
                "orderbookshare",
                "orderbookpercent",
                "ordshare",
                "ordershare",
                "orderpercent",
                "pctorderbook",
                "existingfleetpercent",
            ])

            total_teu = int(parse_numeric_value(total_teu_raw))
            total_ships = int(parse_numeric_value(total_ships_raw))
            market_share = float(parse_numeric_value(market_share_raw))

            owned_teu = int(parse_numeric_value(owned_teu_raw))
            owned_ships = int(parse_numeric_value(owned_ships_raw))
            owned_share = float(parse_numeric_value(owned_share_raw))

            chartered_teu = int(parse_numeric_value(chartered_teu_raw))
            chartered_ships = int(parse_numeric_value(chartered_ships_raw))
            chartered_share = float(parse_numeric_value(chartered_share_raw))

            orderbook_teu = int(parse_numeric_value(orderbook_teu_raw))
            orderbook_ships = int(parse_numeric_value(orderbook_ships_raw))
            orderbook_share = float(parse_numeric_value(orderbook_share_raw))

            # -------------------------------------------------
            # Chartered fallback
            # 원본 Chartered 필드가 없을 때만 Total - Owned로 계산.
            # 실제 값 0이 명시되어 있는 경우는 유지.
            # -------------------------------------------------
            if (
                not chartered_teu_found
                and total_teu > 0
                and owned_teu_found
            ):
                chartered_teu = max(total_teu - owned_teu, 0)

            if (
                not chartered_ships_found
                and total_ships > 0
                and owned_ships_found
            ):
                chartered_ships = max(total_ships - owned_ships, 0)

            # 반대로 Owned 필드가 없고 Chartered만 확보된 경우 보정
            if (
                not owned_teu_found
                and total_teu > 0
                and chartered_teu_found
            ):
                owned_teu = max(total_teu - chartered_teu, 0)

            if (
                not owned_ships_found
                and total_ships > 0
                and chartered_ships_found
            ):
                owned_ships = max(total_ships - chartered_ships, 0)

            # 비율값은 원본 필드가 없을 경우에만 TEU 기준으로 계산
            if not owned_share_found and total_teu > 0:
                owned_share = safe_percent(owned_teu, total_teu)

            if not chartered_share_found and total_teu > 0:
                chartered_share = safe_percent(chartered_teu, total_teu)

            if not market_share_found and total_teu > 0:
                market_share = 0.0

            if not orderbook_share_found and total_teu > 0:
                orderbook_share = safe_percent(orderbook_teu, total_teu)

            # 총 TEU가 없는 레코드는 다른 Grid/비정상 행일 수 있으므로 제외
            if total_teu <= 0:
                logger.warning(
                    "Skipped invalid operator row: index=%s, "
                    "rank=%s, operator=%s, total_teu=%s",
                    idx,
                    rank,
                    operator_name,
                    total_teu,
                )
                continue

            if idx == 1:
                logger.info(
                    "First raw ExtJS record: %s",
                    json.dumps(record, ensure_ascii=False, default=str),
                )

                logger.info(
                    "First record field mapping: %s",
                    json.dumps({
                        "rank": rank_key,
                        "operator": name_key,
                        "total_teu": total_teu_key,
                        "total_ships": total_ships_key,
                        "market_share": market_share_key,
                        "owned_teu": owned_teu_key,
                        "owned_ships": owned_ships_key,
                        "owned_share": owned_share_key,
                        "chartered_teu": chartered_teu_key,
                        "chartered_ships": chartered_ships_key,
                        "chartered_share": chartered_share_key,
                        "orderbook_teu": orderbook_teu_key,
                        "orderbook_ships": orderbook_ships_key,
                        "orderbook_share": orderbook_share_key,
                    }, ensure_ascii=False),
                )

                logger.info(
                    "First record resolved values: %s",
                    json.dumps({
                        "rank": rank,
                        "operator": operator_name,
                        "total_teu": total_teu,
                        "total_ships": total_ships,
                        "owned_teu": owned_teu,
                        "owned_ships": owned_ships,
                        "owned_share": owned_share,
                        "chartered_teu": chartered_teu,
                        "chartered_ships": chartered_ships,
                        "chartered_share": chartered_share,
                        "orderbook_teu": orderbook_teu,
                        "orderbook_ships": orderbook_ships,
                        "orderbook_share": orderbook_share,
                    }, ensure_ascii=False),
                )

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
                "orderbook_share": orderbook_share,
            })

        operators = sorted(operators, key=lambda item: item["rank"])

        if len(operators) < MIN_EXPECTED_OPERATOR_COUNT:
            raise RuntimeError(
                "Alphaliner validation failed: "
                f"extracted {len(operators)} valid operators; "
                f"expected >= {MIN_EXPECTED_OPERATOR_COUNT}."
            )

        chartered_operator_count = sum(
            1 for op in operators
            if op["chartered_teu"] > 0
        )

        logger.info(
            "Chartered fleet validation: %s / %s operators have "
            "non-zero Chartered TEU.",
            chartered_operator_count,
            len(operators),
        )

        if chartered_operator_count == 0:
            logger.warning(
                "All Chartered TEU values are zero. "
                "Review the 'First raw ExtJS record' and "
                "'First record field mapping' GitHub Actions logs."
            )

        top100_teu = sum(op["total_teu"] for op in operators)
        top100_ships = sum(op["total_ships"] for op in operators)
        top10_teu = sum(op["total_teu"] for op in operators[:10])

        raw_text = data.get("rawText", "")

        global_teu_match = re.search(
            r"Total:\s*([\d,]+)\s*TEU",
            raw_text,
            flags=re.IGNORECASE,
        )

        global_ships_match = re.search(
            r"Total:\s*[\d,]+\s*TEU\s*\(\s*([\d,]+)\s*Ships\)",
            raw_text,
            flags=re.IGNORECASE,
        )

        global_total_teu = (
            int(re.sub(r"[^\d]", "", global_teu_match.group(1)))
            if global_teu_match
            else int(top100_teu / 0.95)
            if top100_teu > 0
            else 0
        )

        global_total_ships = (
            int(re.sub(r"[^\d]", "", global_ships_match.group(1)))
            if global_ships_match
            else int(top100_ships / 0.90)
            if top100_ships > 0
            else 0
        )

        top10_share = (
            round((top10_teu / global_total_teu) * 100, 2)
            if global_total_teu > 0
            else 0.0
        )

        meta = {
            "snapshot_date": snapshot_date,
            "global_total_teu": global_total_teu,
            "global_total_ships": global_total_ships,
            "top100_total_teu": top100_teu,
            "top100_total_ships": top100_ships,
            "top10_total_teu": top10_teu,
            "top10_share": top10_share,
            "orderbook_total_teu": orderbook_total_teu,
            "collected_at": datetime.now().isoformat(),
        }

        save_snapshot_to_db(meta, operators)

        logger.info(
            "Crawl completed successfully: %s operators processed.",
            len(operators),
        )

        return True

    except Exception as exc:
        logger.error(
            "Alphaliner crawl failed: %s",
            exc,
            exc_info=True,
        )
        return False

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

# =========================================================
# 4. Dashboard HTML
# =========================================================
# 기존에 사용 중인 HTML_TEMPLATE은 그대로 유지하십시오.
# 아래 템플릿은 별도 변경이 필요 없으며,
# 기존 Bloomberg MI Terminal HTML_TEMPLATE 전체를 이 위치에 유지하면 됩니다.

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Alphaliner TOP 100 Liner Operators | Bloomberg MI Terminal</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
:root {
  --bg-primary:#02060b;
  --bg-secondary:#07111c;
  --bg-tertiary:#0d1d2b;
  --border-subtle:#1b3043;
  --text-main:#f3f7fb;
  --text-muted:#879caf;
  --text-dim:#536c80;
  --accent-orange:#f6a623;
  --accent-blue:#45a9f5;
  --accent-green:#36ca82;
  --accent-red:#ff6262;
  --accent-purple:#bb86fc;
}
* { box-sizing:border-box; margin:0; padding:0; }
body {
  background:var(--bg-primary);
  color:var(--text-main);
  font-family:Arial, sans-serif;
  font-size:13px;
}
.terminal-header {
  display:flex;
  justify-content:space-between;
  align-items:center;
  gap:12px;
  padding:8px 16px;
  background:var(--bg-secondary);
  border-bottom:1px solid var(--border-subtle);
}
.brand-tag {
  display:inline-block;
  padding:2px 6px;
  margin-right:12px;
  font-weight:700;
  font-size:11px;
  color:#000;
  background:var(--accent-orange);
}
.brand-title { font-weight:600; text-transform:uppercase; }
.terminal-controls { display:flex; align-items:center; gap:10px; }
select, input, button {
  padding:5px 8px;
  color:var(--text-main);
  background:var(--bg-tertiary);
  border:1px solid var(--border-subtle);
}
button { cursor:pointer; color:var(--accent-orange); }
.container {
  display:grid;
  grid-template-columns:repeat(12, 1fr);
  gap:12px;
  max-width:1920px;
  padding:16px;
  margin:0 auto;
}
.panel {
  background:var(--bg-secondary);
  border:1px solid var(--border-subtle);
}
.panel-header, .table-toolbar {
  display:flex;
  justify-content:space-between;
  padding:8px 12px;
  border-bottom:1px solid var(--border-subtle);
  font-size:11px;
  font-weight:700;
}
.panel-body { padding:12px; }
.col-kpi { grid-column:span 3; }
.col-chart { grid-column:span 12; height:280px; }
.col-table { grid-column:span 12; }
.kpi-title { color:var(--text-dim); font-size:11px; }
.kpi-value { margin:5px 0; font-size:22px; font-weight:700; }
.kpi-sub { color:var(--text-muted); font-size:11px; }
.table-container { overflow:auto; max-height:600px; }
table {
  width:100%;
  border-collapse:collapse;
  font-family:monospace;
  font-size:12px;
}
th, td {
  padding:6px 10px;
  text-align:right;
  white-space:nowrap;
  border-bottom:1px solid var(--border-subtle);
}
th {
  position:sticky;
  top:0;
  z-index:2;
  color:var(--text-muted);
  background:var(--bg-tertiary);
}
.align-left { text-align:left; }
.align-center { text-align:center; }
.border-left { border-left:1px solid var(--border-subtle); }
.group-header { text-align:center; }
.delta-pos { color:var(--accent-green); }
.delta-neg { color:var(--accent-red); }
.rank-top10 { color:var(--accent-orange); font-weight:700; }
.footer-status {
  display:flex;
  justify-content:space-between;
  padding:12px 16px;
  color:var(--text-dim);
  background:var(--bg-secondary);
  border-top:1px solid var(--border-subtle);
  font-size:11px;
}
</style>
</head>
<body>
<header class="terminal-header">
  <div>
    <span class="brand-tag">ALPHALINER</span>
    <span class="brand-title">TOP 100 Global Liner Operators Terminal</span>
  </div>
  <div class="terminal-controls">
    <label>SNAPSHOT:
      <select id="dateSelect" onchange="renderAll()"></select>
    </label>
    <label>COMPARE TO:
      <select id="compareSelect" onchange="renderAll()"></select>
    </label>
    <button onclick="exportCSV()">EXPORT CSV</button>
  </div>
</header>

<main class="container">
  <section class="panel col-kpi">
    <div class="panel-header">GLOBAL CAPACITY</div>
    <div class="panel-body">
      <div class="kpi-title">TOTAL FLEET TEU</div>
      <div class="kpi-value" id="kpiGlobalTeu">--</div>
      <div class="kpi-sub" id="kpiGlobalShips">-- Ships</div>
    </div>
  </section>

  <section class="panel col-kpi">
    <div class="panel-header">TOP 100 SHARE</div>
    <div class="panel-body">
      <div class="kpi-title">TOP 100 TOTAL TEU</div>
      <div class="kpi-value" id="kpiTop100Teu">--</div>
      <div class="kpi-sub" id="kpiTop100Ships">-- Ships</div>
    </div>
  </section>

  <section class="panel col-kpi">
    <div class="panel-header">CONCENTRATION</div>
    <div class="panel-body">
      <div class="kpi-title">TOP 10 MARKET SHARE</div>
      <div class="kpi-value" id="kpiTop10Share">--</div>
      <div class="kpi-sub" id="kpiTop10Teu">-- TEU</div>
    </div>
  </section>

  <section class="panel col-kpi">
    <div class="panel-header">ORDERBOOK</div>
    <div class="panel-body">
      <div class="kpi-title">PIPELINE CAPACITY</div>
      <div class="kpi-value" id="kpiOrderbookTeu">--</div>
      <div class="kpi-sub" id="kpiOrderbookRatio">-- % of active</div>
    </div>
  </section>

  <section class="panel col-chart">
    <div class="panel-header">
      <span>TOP 10 OPERATOR CAPACITY PROFILE (TEU)</span>
      <span id="chartSubTitle">--</span>
    </div>
    <div class="panel-body" style="height:240px;">
      <canvas id="topChart"></canvas>
    </div>
  </section>

  <section class="panel col-table">
    <div class="table-toolbar">
      <span>TOP 100 DETAILED FLEET BREAKDOWN</span>
      <input
        id="filterInput"
        type="text"
        placeholder="Filter operator..."
        onkeyup="filterTable()"
      >
    </div>

    <div class="table-container">
      <table>
        <thead>
          <tr>
            <th rowspan="2" class="align-center">#</th>
            <th rowspan="2" class="align-left">OPERATOR</th>
            <th colspan="3" class="group-header">TOTAL CAPACITY</th>
            <th colspan="3" class="group-header">OWNED FLEET</th>
            <th colspan="3" class="group-header">CHARTERED FLEET</th>
            <th colspan="3" class="group-header">ORDERBOOK</th>
          </tr>
          <tr>
            <th>TEU</th>
            <th>SHIPS</th>
            <th>SHARE</th>
            <th class="border-left">TEU</th>
            <th>SHIPS</th>
            <th>% OWN</th>
            <th class="border-left">TEU</th>
            <th>SHIPS</th>
            <th>% CHT</th>
            <th class="border-left">TEU</th>
            <th>SHIPS</th>
            <th>% FLEET</th>
          </tr>
        </thead>
        <tbody id="tableBody"></tbody>
      </table>
    </div>
  </section>
</main>

<footer class="footer-status">
  <div>
    SYSTEM STATUS:
    <span style="color:var(--accent-green);">ONLINE</span>
    | SOURCE: ALPHALINER PUBLIC TOP 100
  </div>
  <div id="collectedTimestamp">DATA COLLECTED: --</div>
</footer>

<script>
let DATA_STORE = { dates: [], snapshots: {} };
let currentChart = null;

function fmt(value) {
  if (value === null || value === undefined || isNaN(value)) return "--";
  return Number(value).toLocaleString();
}

async function initDashboard() {
  const response = await fetch("alphaliner_data.json");

  if (!response.ok) {
    throw new Error("Failed to load alphaliner_data.json");
  }

  DATA_STORE = await response.json();

  if (!DATA_STORE.dates || DATA_STORE.dates.length === 0) {
    return;
  }

  const dateSelect = document.getElementById("dateSelect");
  const compareSelect = document.getElementById("compareSelect");

  DATA_STORE.dates.forEach((date, index) => {
    const dateOption = document.createElement("option");
    dateOption.value = date;
    dateOption.textContent = date + (index === 0 ? " (Latest)" : "");
    dateSelect.appendChild(dateOption);

    const compareOption = document.createElement("option");
    compareOption.value = date;
    compareOption.textContent = date;
    compareSelect.appendChild(compareOption);
  });

  const noneOption = document.createElement("option");
  noneOption.value = "none";
  noneOption.textContent = "None";
  compareSelect.insertBefore(noneOption, compareSelect.firstChild);

  if (DATA_STORE.dates.length > 1) {
    compareSelect.value = DATA_STORE.dates[1];
  } else {
    compareSelect.value = "none";
  }

  renderAll();
}

function renderAll() {
  const activeDate = document.getElementById("dateSelect").value;
  const compareDate = document.getElementById("compareSelect").value;

  const snapshot = DATA_STORE.snapshots[activeDate];
  const compareSnapshot = compareDate !== "none"
    ? DATA_STORE.snapshots[compareDate]
    : null;

  if (!snapshot) return;

  const meta = snapshot.meta;

  document.getElementById("kpiGlobalTeu").textContent =
    fmt(meta.global_total_teu) + " TEU";

  document.getElementById("kpiGlobalShips").textContent =
    fmt(meta.global_total_ships) + " Ships";

  document.getElementById("kpiTop100Teu").textContent =
    fmt(meta.top100_total_teu) + " TEU";

  document.getElementById("kpiTop100Ships").textContent =
    fmt(meta.top100_total_ships) + " Ships";

  document.getElementById("kpiTop10Share").textContent =
    Number(meta.top10_share || 0).toFixed(1) + "%";

  document.getElementById("kpiTop10Teu").textContent =
    fmt(meta.top10_total_teu) + " TEU";

  document.getElementById("kpiOrderbookTeu").textContent =
    fmt(meta.orderbook_total_teu) + " TEU";

  const ratio = meta.top100_total_teu > 0
    ? ((meta.orderbook_total_teu / meta.top100_total_teu) * 100).toFixed(1)
    : "0.0";

  document.getElementById("kpiOrderbookRatio").textContent =
    ratio + "% of active";

  document.getElementById("collectedTimestamp").textContent =
    "DATA COLLECTED: " + (meta.collected_at || activeDate);

  renderChart(snapshot);
  renderTable(snapshot, compareSnapshot);
}

function renderChart(snapshot) {
  const top10 = snapshot.operators.slice(0, 10);

  if (currentChart) {
    currentChart.destroy();
  }

  currentChart = new Chart(
    document.getElementById("topChart"),
    {
      type: "bar",
      data: {
        labels: top10.map(op => op.rank + ". " + op.operator),
        datasets: [
          {
            label: "Owned TEU",
            data: top10.map(op => op.owned_teu),
            backgroundColor: "#45a9f5",
            stack: "active"
          },
          {
            label: "Chartered TEU",
            data: top10.map(op => op.chartered_teu),
            backgroundColor: "#f6a623",
            stack: "active"
          },
          {
            label: "Orderbook TEU",
            data: top10.map(op => op.orderbook_teu),
            backgroundColor: "rgba(187,134,252,.45)",
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
            labels: { color: "#879caf" }
          }
        },
        scales: {
          x: {
            stacked: true,
            ticks: { color: "#879caf" },
            grid: { color: "rgba(27,48,67,.5)" }
          },
          y: {
            stacked: true,
            ticks: {
              color: "#879caf",
              callback: value => (value / 1000000).toFixed(1) + "M"
            },
            grid: { color: "rgba(27,48,67,.5)" }
          }
        }
      }
    }
  );

  document.getElementById("chartSubTitle").textContent =
    "AS OF " + snapshot.meta.snapshot_date;
}

function renderTable(snapshot, compareSnapshot) {
  const body = document.getElementById("tableBody");
  body.innerHTML = "";

  const compareMap = {};

  if (compareSnapshot && compareSnapshot.operators) {
    compareSnapshot.operators.forEach(op => {
      compareMap[op.operator.toLowerCase()] = op;
    });
  }

  snapshot.operators.forEach(op => {
    const row = document.createElement("tr");
    const previous = compareMap[op.operator.toLowerCase()];

    let rankDelta = "";

    if (previous && previous.rank !== op.rank) {
      const diff = previous.rank - op.rank;
      const cssClass = diff > 0 ? "delta-pos" : "delta-neg";
      const arrow = diff > 0 ? "▲" : "▼";

      rankDelta =
        `<span class="${cssClass}"> ${arrow}${Math.abs(diff)}</span>`;
    }

    row.innerHTML = `
      <td class="align-center">
        <span class="${op.rank <= 10 ? "rank-top10" : ""}">
          ${op.rank}
        </span>
        ${rankDelta}
      </td>
      <td class="align-left"><strong>${op.operator}</strong></td>
      <td><strong>${fmt(op.total_teu)}</strong></td>
      <td>${fmt(op.total_ships)}</td>
      <td>${Number(op.market_share || 0).toFixed(1)}%</td>
      <td class="border-left">${fmt(op.owned_teu)}</td>
      <td>${fmt(op.owned_ships)}</td>
      <td>${Number(op.owned_share || 0).toFixed(1)}%</td>
      <td class="border-left">${fmt(op.chartered_teu)}</td>
      <td>${fmt(op.chartered_ships)}</td>
      <td>${Number(op.chartered_share || 0).toFixed(1)}%</td>
      <td class="border-left">${fmt(op.orderbook_teu)}</td>
      <td>${fmt(op.orderbook_ships)}</td>
      <td>${Number(op.orderbook_share || 0).toFixed(1)}%</td>
    `;

    body.appendChild(row);
  });
}

function filterTable() {
  const query = document
    .getElementById("filterInput")
    .value
    .toLowerCase();

  document.querySelectorAll("#tableBody tr").forEach(row => {
    const operator = row.cells[1].innerText.toLowerCase();
    row.style.display = operator.includes(query) ? "" : "none";
  });
}

function exportCSV() {
  const activeDate = document.getElementById("dateSelect").value;
  const snapshot = DATA_STORE.snapshots[activeDate];

  if (!snapshot) return;

  let csv =
    "\uFEFFRank,Operator,Total TEU,Total Ships,Market Share %,Owned TEU,"
    + "Owned Ships,Owned Share %,Chartered TEU,Chartered Ships,"
    + "Chartered Share %,Orderbook TEU,Orderbook Ships,"
    + "Orderbook % of Fleet\n";

  snapshot.operators.forEach(op => {
    const escapedName = String(op.operator).replace(/"/g, '""');

    csv += [
      op.rank,
      `"${escapedName}"`,
      op.total_teu,
      op.total_ships,
      op.market_share,
      op.owned_teu,
      op.owned_ships,
      op.owned_share,
      op.chartered_teu,
      op.chartered_ships,
      op.chartered_share,
      op.orderbook_teu,
      op.orderbook_ships,
      op.orderbook_share
    ].join(",") + "\n";
  });

  const blob = new Blob(
    [csv],
    { type: "text/csv;charset=utf-8;" }
  );

  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = "Alphaliner_TOP100_" + activeDate + ".csv";

  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
}

document.addEventListener("DOMContentLoaded", () => {
  initDashboard().catch(error => {
    console.error("Dashboard initialization failed:", error);
  });
});
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
            indent=2,
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
                ),
            }), 200

        return jsonify({
            "status": "error",
            "message": (
                "Crawler failed and no usable historical snapshot exists "
                "for fallback dashboard generation."
            ),
        }), 502

    generate_static_files()

    return jsonify({
        "status": "success",
        "message": "Crawler executed and static files refreshed.",
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
        help="Skip crawling and generate files from DB",
    )

    parser.add_argument(
        "--cron",
        action="store_true",
        help="Run crawl once, generate static files and exit",
    )

    parser.add_argument(
        "--port",
        type=int,
        default=8050,
        help="Port to run Flask server on (default: 8050)",
    )

    parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="Host IP to bind (default: 0.0.0.0)",
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
                "Attempting static artifact generation from existing DB."
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
        "Starting server on http://%s:%s",
        args.host,
        args.port,
    )

    app.run(
        host=args.host,
        port=args.port,
        debug=False,
    )

if __name__ == "__main__":
    main()
