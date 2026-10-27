# -*- coding: utf-8 -*-
"""
Shipping Index Static Dashboard
--------------------------------
Korea Shipping Gazette(KSG)에서 SCFI / BDI를 증분 수집하고,
SQLite DB(index_history.db)에 저장한 뒤 GitHub Pages용 정적 파일을 생성합니다.

Output:
- index_history.db
- shipping_index_data.json
- index.html

Required environment variables:
- KSG_ID
- KSG_PW
"""

import json
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
from selenium import webdriver
from selenium.common.exceptions import NoAlertPresentException, TimeoutException
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# =============================================================================
# 1. Path / Configuration
# =============================================================================

SCRIPT_DIR = Path(__file__).resolve().parent

DB_PATH = SCRIPT_DIR / "index_history.db"
JSON_PATH = SCRIPT_DIR / "shipping_index_data.json"
HTML_PATH = SCRIPT_DIR / "index.html"

BASE_URL = "https://www.ksg.co.kr/"

SUPPORTED_INDEXES = ["SCFI", "BDI"]

SCFI_REGION_PRIORITY = ["종합", "종합지수"]

BDI_REGION_PRIORITY = [
    "BDI",
    "BCI",
    "BPI",
    "BSI",
    "BHSI",
    "Cape(4TC)",
    "Pana(4TC)",
    "Pana(5TC)",
    "Pana(82_5TC)",
    "Supra(6TC)",
    "Supra(58_10TC)",
    "Supra(63_11TC)",
    "Handy(6TC)",
    "Handy(38_7TC)",
]

# =============================================================================
# 2. Database Management
# =============================================================================

def init_db(db_path: Path):
    """
    SQLite 데이터베이스 테이블 초기화
    - index_raw: SCFI / BDI 통합 원천 데이터 (Composite PK: dt, region, index_name)
    - crawl_audit: 수집 이력 감사 로그
    """
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS index_raw (
            dt TEXT NOT NULL,
            region TEXT NOT NULL,
            value REAL,
            index_name TEXT NOT NULL,
            source_file TEXT,
            PRIMARY KEY (dt, region, index_name)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS crawl_audit (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT,
            finished_at TEXT,
            status TEXT,
            message TEXT,
            rows_added INTEGER,
            total_rows INTEGER,
            index_name TEXT
        )
        """
    )

    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_raw_search ON index_raw(index_name, region, dt DESC)"
    )

    conn.commit()
    conn.close()

def get_latest_date_in_db(db_path: Path, index_name: str) -> str:
    """DB에 저장된 특정 인덱스의 최신 날짜 반환 (YYYY-MM-DD)"""
    if not db_path.exists():
        return "1900-01-01"

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT MAX(dt) FROM index_raw WHERE index_name = ? AND value IS NOT NULL",
        (index_name,),
    )
    row = cur.fetchone()
    conn.close()

    if row and row[0]:
        return row[0]
    return "1900-01-01"

def upsert_index_records(db_path: Path, records: list) -> int:
    """
    수집된 레코드를 index_raw 테이블에 INSERT OR REPLACE
    records: list of tuples (dt, region, value, index_name, source_file)
    """
    if not records:
        return 0

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.executemany(
        """
        INSERT OR REPLACE INTO index_raw (dt, region, value, index_name, source_file)
        VALUES (?, ?, ?, ?, ?)
        """,
        records,
    )

    inserted_count = cur.rowcount
    conn.commit()
    conn.close()
    return inserted_count

def log_audit(
    db_path: Path,
    started_at: str,
    finished_at: str,
    status: str,
    message: str,
    rows_added: int,
    total_rows: int,
    index_name: str,
):
    """크롤링 실행 감사 로그 기록"""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO crawl_audit (started_at, finished_at, status, message, rows_added, total_rows, index_name)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            started_at,
            finished_at,
            status,
            message,
            rows_added,
            total_rows,
            index_name,
        ),
    )
    conn.commit()
    conn.close()

# =============================================================================
# 3. Selenium Crawler Module
# =============================================================================

class KSGCrawler:

    def __init__(self, user_id: str, user_pw: str, headless: bool = True):
        self.user_id = user_id
        self.user_pw = user_pw
        self.driver = self._init_driver(headless)

    def _init_driver(self, headless: bool) -> webdriver.Chrome:
        options = webdriver.ChromeOptions()
        if headless:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

        # -------------------------------------------------------------
        # GitHub Actions 등 환경변수에 Chrome 바이너리 경로가 지정된 경우 적용
        # -------------------------------------------------------------
        chrome_bin = os.environ.get("CHROME_BINARY") or os.environ.get("GOOGLE_CHROME_BIN")
        if chrome_bin and os.path.exists(chrome_bin):
            options.binary_location = chrome_bin

        # 환경변수에 chromedriver 경로가 지정된 경우 적용
        chromedriver_path = os.environ.get("CHROMEDRIVER_PATH")
        if chromedriver_path and os.path.exists(chromedriver_path):
            service = Service(executable_path=chromedriver_path)
            return webdriver.Chrome(service=service, options=options)

        return webdriver.Chrome(options=options)


    def login(self) -> bool:
        try:
            print("[INFO] Navigating to KSG main page for login...")
            self.driver.get(BASE_URL)
            time.sleep(2)

            try:
                alert = self.driver.switch_to.alert
                alert.accept()
            except NoAlertPresentException:
                pass

            login_inputs = self.driver.find_elements(
                By.CSS_SELECTOR, "input[type='text'], input[name*='id' i]"
            )
            pw_inputs = self.driver.find_elements(
                By.CSS_SELECTOR, "input[type='password']"
            )

            if not login_inputs or not pw_inputs:
                login_btn_direct = self.driver.find_elements(
                    By.XPATH, "//a[contains(text(), '로그인')]"
                )
                if login_btn_direct:
                    login_btn_direct[0].click()
                    time.sleep(2)

            id_elem = WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located(
                    (
                        By.CSS_SELECTOR,
                        "input[name='uid'], input[id*='id' i], input[type='text']",
                    )
                )
            )
            pw_elem = self.driver.find_element(
                By.CSS_SELECTOR, "input[type='password']"
            )

            id_elem.clear()
            id_elem.send_keys(self.user_id)
            pw_elem.clear()
            pw_elem.send_keys(self.user_pw)

            submit_btn = self.driver.find_element(
                By.CSS_SELECTOR,
                "button[type='submit'], .btn_login_submit, input[type='submit']",
            )
            submit_btn.click()
            time.sleep(3)

            try:
                alert = self.driver.switch_to.alert
                alert.accept()
            except NoAlertPresentException:
                pass

            print("[INFO] Login routine executed successfully.")
            return True
        except Exception as e:
            print(f"[ERROR] Login failed: {e}")
            return False

    def scrape_scfi_recent(self, stop_date: str) -> list:
        records = []
        target_url = "https://www.ksg.co.kr/shipping_index/scfi.jsp"
        print(
            f"[INFO] Scraping SCFI from {target_url} (Incremental threshold: {stop_date})"
        )

        try:
            self.driver.get(target_url)
            time.sleep(3)

            tables = self.driver.find_elements(By.CSS_SELECTOR, "table")
            if not tables:
                print("[WARN] No tables found on SCFI page.")
                return records

            for table in tables:
                rows = table.find_elements(By.CSS_SELECTOR, "tbody tr")
                if not rows:
                    rows = table.find_elements(By.CSS_SELECTOR, "tr")

                for row in rows:
                    cols = [
                        c.text.strip().replace(",", "")
                        for c in row.find_elements(By.CSS_SELECTOR, "td, th")
                    ]
                    if not cols or len(cols) < 2:
                        continue

                    dt_match = re.search(r"\d{4}[-.]\d{2}[-.]\d{2}", cols[0])
                    if not dt_match:
                        continue

                    raw_dt = dt_match.group(0).replace(".", "-")
                    if raw_dt <= stop_date:
                        continue

                    val_match = re.search(r"^-?\d+(\.\d+)?$", cols[1])
                    if val_match:
                        val = float(cols[1])
                        records.append(
                            (raw_dt, "종합지수", val, "SCFI", "ksg_web_incremental")
                        )

            print(f"[INFO] Scraped {len(records)} new SCFI records.")
            return records
        except Exception as e:
            print(f"[ERROR] Failed scraping SCFI: {e}")
            return records

    def scrape_bdi_recent(self, stop_date: str) -> list:
        records = []
        target_url = "https://www.ksg.co.kr/shipping_index/bdi.jsp"
        print(
            f"[INFO] Scraping BDI from {target_url} (Incremental threshold: {stop_date})"
        )

        try:
            self.driver.get(target_url)
            time.sleep(3)

            tables = self.driver.find_elements(By.CSS_SELECTOR, "table")
            if not tables:
                print("[WARN] No tables found on BDI page.")
                return records

            for table in tables:
                headers = [
                    th.text.strip()
                    for th in table.find_elements(By.CSS_SELECTOR, "thead th")
                ]
                rows = table.find_elements(By.CSS_SELECTOR, "tbody tr")
                if not rows:
                    rows = table.find_elements(By.CSS_SELECTOR, "tr")

                for row in rows:
                    cols = [
                        c.text.strip().replace(",", "")
                        for c in row.find_elements(By.CSS_SELECTOR, "td")
                    ]
                    if not cols or len(cols) < 2:
                        continue

                    dt_match = re.search(r"\d{4}[-.]\d{2}[-.]\d{2}", cols[0])
                    if not dt_match:
                        continue

                    raw_dt = dt_match.group(0).replace(".", "-")
                    if raw_dt <= stop_date:
                        continue

                    if len(headers) == len(cols):
                        for idx, h_name in enumerate(headers[1:], start=1):
                            if idx < len(cols):
                                val_match = re.search(
                                    r"^-?\d+(\.\d+)?$", cols[idx]
                                )
                                if val_match:
                                    val = float(cols[idx])
                                    clean_region = (
                                        h_name
                                        if h_name
                                        else f"Region_{idx}"
                                    )
                                    records.append(
                                        (
                                            raw_dt,
                                            clean_region,
                                            val,
                                            "BDI",
                                            "ksg_web_incremental",
                                        )
                                    )
                    else:
                        val_match = re.search(r"^-?\d+(\.\d+)?$", cols[1])
                        if val_match:
                            val = float(cols[1])
                            records.append(
                                (
                                    raw_dt,
                                    "BDI",
                                    val,
                                    "BDI",
                                    "ksg_web_incremental",
                                )
                            )

            print(f"[INFO] Scraped {len(records)} new BDI records.")
            return records
        except Exception as e:
            print(f"[ERROR] Failed scraping BDI: {e}")
            return records

    def close(self):
        try:
            self.driver.quit()
        except Exception:
            pass

# =============================================================================
# 4. JSON Payload Generator & Dashboard Exporter
# =============================================================================

def fetch_payload_data(db_path: Path) -> dict:
    """
    index_history.db의 index_raw 테이블에서 전체 시계열 데이터를 추출하여
    프론트엔드용 JSON 딕셔너리로 직렬화
    """
    if not db_path.exists():
        return {
            "meta": {
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "total_records": 0,
            },
            "indexes": {},
        }

    conn = sqlite3.connect(db_path)

    query = """
        SELECT dt, region, value, index_name
        FROM index_raw
        WHERE value IS NOT NULL
        ORDER BY dt ASC
    """
    df = pd.read_sql_query(query, conn)
    conn.close()

    if df.empty:
        return {
            "meta": {
                "generated_at": datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S KST"
                ),
                "total_records": 0,
            },
            "indexes": {},
        }

    df["dt"] = df["dt"].astype(str)
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna(subset=["value"])

    payload = {
        "meta": {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S KST"),
            "total_records": int(len(df)),
        },
        "indexes": {},
    }

    for idx_name in SUPPORTED_INDEXES:
        sub_df = df[df["index_name"] == idx_name]
        if sub_df.empty:
            continue

        regions_raw = sub_df["region"].unique().tolist()

        if idx_name == "SCFI":
            regions = [r for r in SCFI_REGION_PRIORITY if r in regions_raw]
            for r in sorted(regions_raw):
                if r not in regions:
                    regions.append(r)
        elif idx_name == "BDI":
            regions = [r for r in BDI_REGION_PRIORITY if r in regions_raw]
            for r in regions_raw:
                if r not in regions:
                    regions.append(r)
        else:
            regions = sorted(regions_raw)

        idx_dict = {"regions": regions, "series": {}}

        for reg in regions:
            reg_df = sub_df[sub_df["region"] == reg].sort_values("dt")
            idx_dict["series"][reg] = {
                "dates": reg_df["dt"].tolist(),
                "values": [round(float(v), 2) for v in reg_df["value"]],
            }

        payload["indexes"][idx_name] = idx_dict

    return payload

def generate_static_files(db_path: Path, json_path: Path, html_path: Path):
    """JSON 데이터 및 표준 Bloomberg / MI Terminal CSS가 적용된 index.html 생성"""
    payload = fetch_payload_data(db_path)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(
        f"[INFO] Exported JSON: {json_path} (Total Records: {payload['meta']['total_records']})"
    )

    embedded_json = json.dumps(payload, ensure_ascii=False)

    html_content = f"""<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SHIPPING FREIGHT INDEX TERMINAL | MI</title>
    <!-- Chart Engine -->
    <script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
    <!-- Typography -->
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Noto+Sans+KR:wght@300;400;500;700&display=swap" rel="stylesheet">
    <style>
        /* =========================================================
           1. Terminal Design System Tokens
           ========================================================= */
        :root {{
            --bg-base: #0a0e14;
            --bg-surface: #0f151d;
            --bg-surface-elevated: #151c26;
            --bg-surface-hover: #1c2633;
            --bg-highlight: #222f3e;

            --border-subtle: #1b2430;
            --border-default: #243142;
            --border-accent: #ff9800;

            --text-primary: #e6edf3;
            --text-secondary: #8b949e;
            --text-muted: #545d68;
            --text-accent: #ff9800;
            --text-cyan: #00d2d3;
            --text-blue: #2e86de;

            --color-up: #ff4757;
            --color-down: #2ed573;
            --color-neutral: #8b949e;

            --font-mono: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            --font-sans: 'Noto Sans KR', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;

            --top-line-scfi: #ff9800;
            --top-line-bdi: #00d2d3;
        }}

        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }}

        body {{
            background-color: var(--bg-base);
            color: var(--text-primary);
            font-family: var(--font-sans);
            font-size: 13px;
            line-height: 1.5;
            -webkit-font-smoothing: antialiased;
            -moz-osx-font-smoothing: grayscale;
            min-height: 100vh;
        }}

        /* Scrollbar */
        ::-webkit-scrollbar {{
            width: 6px;
            height: 6px;
        }}
        ::-webkit-scrollbar-track {{
            background: var(--bg-base);
        }}
        ::-webkit-scrollbar-thumb {{
            background: var(--border-default);
            border-radius: 3px;
        }}
        ::-webkit-scrollbar-thumb:hover {{
            background: var(--text-muted);
        }}

        /* Selection */
        ::selection {{
            background: rgba(255, 152, 0, 0.3);
            color: var(--text-primary);
        }}

        /* =========================================================
           2. Bloomberg / MI Terminal Top Header (Standard Spec)
           ========================================================= */
        .mi-terminal-header {{
            position: sticky;
            top: 0;
            z-index: 1000;
            background: linear-gradient(180deg, #0f151d 0%, rgba(15, 21, 29, 0.95) 100%);
            border-bottom: 1px solid var(--border-default);
            backdrop-filter: blur(8px);
            padding: 0 16px;
            height: 48px;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }}

        .mi-header-left {{
            display: flex;
            align-items: center;
            gap: 12px;
        }}

        .mi-brand {{
            display: flex;
            align-items: center;
            gap: 8px;
            text-decoration: none;
        }}

        .mi-brand-tag {{
            font-family: var(--font-mono);
            font-size: 11px;
            font-weight: 700;
            background: var(--text-accent);
            color: #000000;
            padding: 2px 6px;
            border-radius: 2px;
            letter-spacing: 0.5px;
        }}

        .mi-module-name {{
            font-family: var(--font-mono);
            font-size: 13px;
            font-weight: 700;
            color: var(--text-primary);
            letter-spacing: 0.5px;
            text-transform: uppercase;
            border-right: 1px solid var(--border-default);
            padding-right: 12px;
        }}

        .mi-source-text {{
            font-size: 11px;
            color: var(--text-secondary);
            display: flex;
            align-items: center;
            gap: 6px;
        }}

        .mi-source-badge {{
            font-family: var(--font-mono);
            font-size: 10px;
            color: var(--text-muted);
            border: 1px solid var(--border-subtle);
            padding: 1px 4px;
            border-radius: 2px;
        }}

        .mi-header-right {{
            display: flex;
            align-items: center;
            gap: 16px;
        }}

        /* Live Ticker in Header */
        .mi-ticker-group {{
            display: flex;
            align-items: center;
            gap: 8px;
        }}

        .mi-ticker {{
            display: flex;
            align-items: center;
            gap: 6px;
            background: var(--bg-surface-elevated);
            border: 1px solid var(--border-subtle);
            border-radius: 3px;
            padding: 2px 8px;
            font-family: var(--font-mono);
            font-size: 11px;
        }}

        .mi-ticker-label {{
            color: var(--text-secondary);
            font-weight: 600;
        }}

        .mi-ticker-val {{
            color: var(--text-primary);
            font-weight: 700;
        }}

        .mi-ticker-chg {{
            font-size: 10px;
            font-weight: 600;
        }}

        .mi-status-indicator {{
            display: flex;
            align-items: center;
            gap: 6px;
            font-family: var(--font-mono);
            font-size: 11px;
            color: var(--text-secondary);
        }}

        .mi-pulse-dot {{
            width: 7px;
            height: 7px;
            border-radius: 50%;
            background: var(--color-down);
            box-shadow: 0 0 6px var(--color-down);
            animation: pulse-glow 2s infinite;
        }}

        @keyframes pulse-glow {{
            0%, 100% {{ opacity: 1; transform: scale(1); }}
            50% {{ opacity: 0.4; transform: scale(0.85); }}
        }}

        .mi-header-action {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 4px 10px;
            background: var(--bg-surface-elevated);
            border: 1px solid var(--border-default);
            border-radius: 3px;
            color: var(--text-primary);
            font-family: var(--font-mono);
            font-size: 11px;
            text-decoration: none;
            transition: all 0.15s ease;
        }}

        .mi-header-action:hover {{
            background: var(--bg-surface-hover);
            border-color: var(--text-accent);
            color: var(--text-accent);
        }}

        /* =========================================================
           3. Layout Container & Page Elements
           ========================================================= */
        .mi-page {{
            max-width: 1720px;
            margin: 0 auto;
            padding: 16px;
            display: flex;
            flex-direction: column;
            gap: 16px;
        }}

        /* Filter & Control Bar */
        .mi-control-bar {{
            background: var(--bg-surface);
            border: 1px solid var(--border-default);
            border-radius: 4px;
            padding: 10px 14px;
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
        }}

        .mi-control-group {{
            display: flex;
            align-items: center;
            gap: 10px;
            flex-wrap: wrap;
        }}

        .mi-control-label {{
            font-family: var(--font-mono);
            font-size: 11px;
            font-weight: 600;
            color: var(--text-secondary);
            text-transform: uppercase;
        }}

        .mi-btn-group {{
            display: inline-flex;
            background: var(--bg-base);
            border: 1px solid var(--border-default);
            border-radius: 3px;
            padding: 2px;
        }}

        .mi-btn-toggle {{
            background: transparent;
            border: none;
            color: var(--text-secondary);
            font-family: var(--font-mono);
            font-size: 11px;
            font-weight: 500;
            padding: 3px 10px;
            border-radius: 2px;
            cursor: pointer;
            transition: all 0.15s ease;
        }}

        .mi-btn-toggle:hover {{
            color: var(--text-primary);
        }}

        .mi-btn-toggle.active {{
            background: var(--bg-surface-hover);
            color: var(--text-accent);
            font-weight: 700;
            box-shadow: 0 1px 3px rgba(0,0,0,0.3);
        }}

        /* Dual Column Layout for SCFI and BDI */
        .mi-grid-dual {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 16px;
        }}

        @media (max-width: 1280px) {{
            .mi-grid-dual {{
                grid-template-columns: 1fr;
            }}
        }}

        /* Terminal Panel */
        .mi-panel {{
            background: var(--bg-surface);
            border: 1px solid var(--border-default);
            border-radius: 4px;
            display: flex;
            flex-direction: column;
            overflow: hidden;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
        }}

        .mi-panel-header {{
            background: var(--bg-surface-elevated);
            border-bottom: 1px solid var(--border-default);
            padding: 10px 14px;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }}

        .mi-panel-title-group {{
            display: flex;
            align-items: center;
            gap: 8px;
        }}

        .mi-panel-indicator {{
            width: 3px;
            height: 14px;
            border-radius: 1px;
        }}
        .indicator-scfi {{ background: var(--top-line-scfi); }}
        .indicator-bdi {{ background: var(--top-line-bdi); }}

        .mi-panel-title {{
            font-family: var(--font-mono);
            font-size: 12px;
            font-weight: 700;
            color: var(--text-primary);
            letter-spacing: 0.5px;
            text-transform: uppercase;
        }}

        .mi-panel-subtitle {{
            font-size: 11px;
            color: var(--text-muted);
            margin-left: 4px;
        }}

        /* Multi-select Chip Group */
        .mi-chips-wrapper {{
            background: var(--bg-surface-elevated);
            border-bottom: 1px solid var(--border-subtle);
            padding: 8px 14px;
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            gap: 6px;
        }}

        .mi-chips-title {{
            font-family: var(--font-mono);
            font-size: 10px;
            font-weight: 600;
            color: var(--text-muted);
            text-transform: uppercase;
            margin-right: 4px;
        }}

        .mi-chip {{
            background: var(--bg-base);
            border: 1px solid var(--border-default);
            color: var(--text-secondary);
            font-family: var(--font-mono);
            font-size: 11px;
            padding: 2px 8px;
            border-radius: 3px;
            cursor: pointer;
            user-select: none;
            transition: all 0.15s ease;
            display: inline-flex;
            align-items: center;
            gap: 4px;
        }}

        .mi-chip:hover {{
            border-color: var(--text-muted);
            color: var(--text-primary);
        }}

        .mi-chip.active {{
            background: var(--bg-highlight);
            border-color: var(--text-accent);
            color: var(--text-accent);
            font-weight: 600;
        }}

        .mi-chip-color-dot {{
            width: 6px;
            height: 6px;
            border-radius: 50%;
            display: inline-block;
        }}

        /* KPI Metric Grid inside Panel */
        .mi-kpi-grid {{
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            border-bottom: 1px solid var(--border-default);
            background: var(--bg-surface);
        }}

        @media (max-width: 640px) {{
            .mi-kpi-grid {{
                grid-template-columns: repeat(2, 1fr);
            }}
        }}

        .mi-kpi-card {{
            padding: 12px 14px;
            border-right: 1px solid var(--border-subtle);
            display: flex;
            flex-direction: column;
            gap: 4px;
        }}
        .mi-kpi-card:last-child {{
            border-right: none;
        }}

        .mi-kpi-lbl {{
            font-family: var(--font-mono);
            font-size: 10px;
            font-weight: 600;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}

        .mi-kpi-val-group {{
            display: flex;
            align-items: baseline;
            gap: 6px;
        }}

        .mi-kpi-val {{
            font-family: var(--font-mono);
            font-size: 18px;
            font-weight: 700;
            color: var(--text-primary);
            line-height: 1.2;
        }}

        .mi-kpi-sub {{
            font-family: var(--font-mono);
            font-size: 11px;
            font-weight: 600;
        }}

        .mi-kpi-date {{
            font-family: var(--font-mono);
            font-size: 10px;
            color: var(--text-muted);
        }}

        .val-up {{ color: var(--color-up) !important; }}
        .val-down {{ color: var(--color-down) !important; }}
        .val-neutral {{ color: var(--color-neutral) !important; }}

        /* Chart Viewport */
        .mi-chart-container {{
            height: 380px;
            width: 100%;
            background: var(--bg-surface);
            position: relative;
        }}

        /* Terminal Table */
        .mi-table-wrapper {{
            border-top: 1px solid var(--border-default);
            max-height: 260px;
            overflow-y: auto;
            background: var(--bg-surface);
        }}

        .mi-table {{
            width: 100%;
            border-collapse: collapse;
            font-family: var(--font-mono);
            font-size: 11px;
            text-align: left;
        }}

        .mi-table thead {{
            position: sticky;
            top: 0;
            background: var(--bg-surface-elevated);
            z-index: 10;
        }}

        .mi-table th {{
            color: var(--text-secondary);
            font-weight: 600;
            padding: 8px 12px;
            border-bottom: 1px solid var(--border-default);
            text-transform: uppercase;
            font-size: 10px;
            white-space: nowrap;
        }}

        .mi-table td {{
            padding: 6px 12px;
            border-bottom: 1px solid var(--border-subtle);
            color: var(--text-primary);
            white-space: nowrap;
        }}

        .mi-table tr:hover {{
            background: var(--bg-surface-hover);
        }}

        .mi-table th:not(:first-child),
        .mi-table td:not(:first-child) {{
            text-align: right;
        }}

        /* Status Footer */
        .mi-footer {{
            margin-top: 8px;
            padding: 12px 16px;
            background: var(--bg-surface);
            border: 1px solid var(--border-default);
            border-radius: 4px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            font-family: var(--font-mono);
            font-size: 11px;
            color: var(--text-secondary);
        }}

        .mi-footer a {{
            color: var(--text-accent);
            text-decoration: none;
        }}
        .mi-footer a:hover {{
            text-decoration: underline;
        }}

        .mi-empty {{
            display: flex;
            align-items: center;
            justify-content: center;
            height: 100%;
            color: var(--text-muted);
            font-family: var(--font-mono);
            font-size: 12px;
        }}
    </style>
</head>
<body>

    <!-- =========================================================
         1. Bloomberg / MI Terminal Top Header
         ========================================================= -->
    <header class="mi-terminal-header">
        <div class="mi-header-left">
            <a href="../index.html" class="mi-brand">
                <span class="mi-brand-tag">MI</span>
            </a>
            <span class="mi-module-name">SHIPPING FREIGHT INDEX</span>
            <div class="mi-source-text">
                <span class="mi-source-badge">FEED</span> KSG (KOREA SHIPPING GAZETTE)
            </div>
        </div>

        <div class="mi-header-right">
            <div class="mi-ticker-group">
                <div class="mi-ticker" id="ticker-scfi">
                    <span class="mi-ticker-label">SCFI</span>
                    <span class="mi-ticker-val" id="ticker-scfi-val">--</span>
                    <span class="mi-ticker-chg" id="ticker-scfi-chg">--</span>
                </div>
                <div class="mi-ticker" id="ticker-bdi">
                    <span class="mi-ticker-label">BDI</span>
                    <span class="mi-ticker-val" id="ticker-bdi-val">--</span>
                    <span class="mi-ticker-chg" id="ticker-bdi-chg">--</span>
                </div>
            </div>

            <div class="mi-status-indicator">
                <span class="mi-pulse-dot"></span>
                <span>SYNCED</span>
            </div>

            <a href="../index.html" class="mi-header-action">
                <span>PORTAL</span> &rarr;
            </a>
        </div>
    </header>

    <!-- =========================================================
         2. Main Application Body
         ========================================================= -->
    <main class="mi-page">

        <!-- Global Control Bar -->
        <div class="mi-control-bar">
            <div class="mi-control-group">
                <span class="mi-control-label">TIME HORIZON:</span>
                <div class="mi-btn-group" id="range-btn-group">
                    <button class="mi-btn-toggle" data-range="1M">1M</button>
                    <button class="mi-btn-toggle" data-range="3M">3M</button>
                    <button class="mi-btn-toggle" data-range="6M">6M</button>
                    <button class="mi-btn-toggle active" data-range="1Y">1Y</button>
                    <button class="mi-btn-toggle" data-range="3Y">3Y</button>
                    <button class="mi-btn-toggle" data-range="5Y">5Y</button>
                    <button class="mi-btn-toggle" data-range="ALL">MAX</button>
                </div>
            </div>

            <div class="mi-control-group">
                <span class="mi-control-label">DATA SNAPSHOT:</span>
                <span style="font-family: var(--font-mono); font-size: 11px; color: var(--text-accent);" id="global-sync-date">--</span>
            </div>
        </div>

        <!-- Dual Chart Matrix -->
        <div class="mi-grid-dual">

            <!-- SCFI Panel -->
            <section class="mi-panel" id="panel-scfi">
                <div class="mi-panel-header">
                    <div class="mi-panel-title-group">
                        <div class="mi-panel-indicator indicator-scfi"></div>
                        <span class="mi-panel-title">SCFI</span>
                        <span class="mi-panel-subtitle">Shanghai Containerized Freight Index</span>
                    </div>
                    <div style="display: flex; gap: 8px; align-items: center;">
                        <button class="mi-header-action" onclick="exportCSV('SCFI')">CSV EXPORT</button>
                    </div>
                </div>

                <!-- SCFI Multi-select Chips -->
                <div class="mi-chips-wrapper" id="chips-scfi">
                    <span class="mi-chips-title">SELECT ROUTES:</span>
                    <!-- Dynamic chips populated via JS -->
                </div>

                <div class="mi-kpi-grid">
                    <div class="mi-kpi-card">
                        <span class="mi-kpi-lbl">PRIMARY CLOSE</span>
                        <div class="mi-kpi-val-group">
                            <span class="mi-kpi-val" id="kpi-scfi-latest">--</span>
                        </div>
                        <span class="mi-kpi-date" id="kpi-scfi-date">--</span>
                    </div>
                    <div class="mi-kpi-card">
                        <span class="mi-kpi-lbl">W-O-W CHANGE</span>
                        <div class="mi-kpi-val-group">
                            <span class="mi-kpi-val" id="kpi-scfi-chg">--</span>
                            <span class="mi-kpi-sub" id="kpi-scfi-pct">--</span>
                        </div>
                        <span class="mi-kpi-date">VS PREV WEEK</span>
                    </div>
                    <div class="mi-kpi-card">
                        <span class="mi-kpi-lbl">PERIOD HIGH</span>
                        <div class="mi-kpi-val-group">
                            <span class="mi-kpi-val" id="kpi-scfi-high">--</span>
                        </div>
                        <span class="mi-kpi-date" id="kpi-scfi-high-date">--</span>
                    </div>
                    <div class="mi-kpi-card">
                        <span class="mi-kpi-lbl">PERIOD LOW</span>
                        <div class="mi-kpi-val-group">
                            <span class="mi-kpi-val" id="kpi-scfi-low">--</span>
                        </div>
                        <span class="mi-kpi-date" id="kpi-scfi-low-date">--</span>
                    </div>
                </div>

                <div class="mi-chart-container" id="chart-scfi"></div>

                <div class="mi-table-wrapper" id="table-wrap-scfi">
                    <table class="mi-table" id="table-scfi">
                        <thead>
                            <tr id="table-head-scfi">
                                <th>DATE</th>
                                <th>VALUE</th>
                            </tr>
                        </thead>
                        <tbody></tbody>
                    </table>
                </div>
            </section>

            <!-- BDI Panel -->
            <section class="mi-panel" id="panel-bdi">
                <div class="mi-panel-header">
                    <div class="mi-panel-title-group">
                        <div class="mi-panel-indicator indicator-bdi"></div>
                        <span class="mi-panel-title">BDI</span>
                        <span class="mi-panel-subtitle">Baltic Dry Index & Sub-Indices</span>
                    </div>
                    <div style="display: flex; gap: 8px; align-items: center;">
                        <button class="mi-header-action" onclick="exportCSV('BDI')">CSV EXPORT</button>
                    </div>
                </div>

                <!-- BDI Multi-select Chips -->
                <div class="mi-chips-wrapper" id="chips-bdi">
                    <span class="mi-chips-title">SELECT INDICES:</span>
                    <!-- Dynamic chips populated via JS -->
                </div>

                <div class="mi-kpi-grid">
                    <div class="mi-kpi-card">
                        <span class="mi-kpi-lbl">PRIMARY CLOSE</span>
                        <div class="mi-kpi-val-group">
                            <span class="mi-kpi-val" id="kpi-bdi-latest">--</span>
                        </div>
                        <span class="mi-kpi-date" id="kpi-bdi-date">--</span>
                    </div>
                    <div class="mi-kpi-card">
                        <span class="mi-kpi-lbl">D-O-D CHANGE</span>
                        <div class="mi-kpi-val-group">
                            <span class="mi-kpi-val" id="kpi-bdi-chg">--</span>
                            <span class="mi-kpi-sub" id="kpi-bdi-pct">--</span>
                        </div>
                        <span class="mi-kpi-date">VS PREV TRADING DAY</span>
                    </div>
                    <div class="mi-kpi-card">
                        <span class="mi-kpi-lbl">PERIOD HIGH</span>
                        <div class="mi-kpi-val-group">
                            <span class="mi-kpi-val" id="kpi-bdi-high">--</span>
                        </div>
                        <span class="mi-kpi-date" id="kpi-bdi-high-date">--</span>
                    </div>
                    <div class="mi-kpi-card">
                        <span class="mi-kpi-lbl">PERIOD LOW</span>
                        <div class="mi-kpi-val-group">
                            <span class="mi-kpi-val" id="kpi-bdi-low">--</span>
                        </div>
                        <span class="mi-kpi-date" id="kpi-bdi-low-date">--</span>
                    </div>
                </div>

                <div class="mi-chart-container" id="chart-bdi"></div>

                <div class="mi-table-wrapper" id="table-wrap-bdi">
                    <table class="mi-table" id="table-bdi">
                        <thead>
                            <tr id="table-head-bdi">
                                <th>DATE</th>
                                <th>VALUE</th>
                            </tr>
                        </thead>
                        <tbody></tbody>
                    </table>
                </div>
            </section>

        </div>

        <!-- Terminal Footer -->
        <footer class="mi-footer">
            <div>
                SOURCE: <a href="https://www.ksg.co.kr" target="_blank" rel="noopener">Korea Shipping Gazette</a> | AUTOMATED DAILY PIPELINE
            </div>
            <div>
                ENGINE: SQLITE3 &bull; PLOTLY.JS &bull; MI TERMINAL DESIGN SYSTEM
            </div>
        </footer>

    </main>

    <!-- =========================================================
         3. Application State & Script
         ========================================================= -->
    <script>
        const PAYLOAD_DATA = {embedded_json};

        let currentRange = '1Y';
        // Multi-select state: Array of selected region strings per index
        let selectedRegions = {{
            'SCFI': [],
            'BDI': []
        }};

        // Distinct vibrant color palette for multi-line charts
        const PALETTE = [
            '#ff9800', '#00d2d3', '#2ed573', '#ff4757', '#a29bfe',
            '#ffa502', '#70a1ff', '#eccc68', '#ff6b81', '#1dd1a1',
            '#54a0ff', '#5f27cd', '#c8d6e5', '#ff9ff3', '#00d8d6'
        ];

        document.addEventListener('DOMContentLoaded', () => {{
            initMultiSelectChips();
            initControls();
            renderAllPanels();
            updateTopTickers();
        }});

        function initMultiSelectChips() {{
            ['SCFI', 'BDI'].forEach(idx => {{
                const idxData = PAYLOAD_DATA.indexes[idx];
                const container = document.getElementById(`chips-${{idx.toLowerCase()}}`);
                if (!idxData || !idxData.regions || !idxData.regions.length) return;

                // Determine default selection
                let defaultReg = idxData.regions[0];
                if (idx === 'SCFI') {{
                    const pref = idxData.regions.find(r => r === '종합' || r === '종합지수');
                    if (pref) defaultReg = pref;
                }} else if (idx === 'BDI') {{
                    const pref = idxData.regions.find(r => r === 'BDI');
                    if (pref) defaultReg = pref;
                }}
                selectedRegions[idx] = [defaultReg];

                renderChipsUI(idx);
            }});
        }}

        function renderChipsUI(idx) {{
            const key = idx.toLowerCase();
            const idxData = PAYLOAD_DATA.indexes[idx];
            const container = document.getElementById(`chips-${{key}}`);
            if (!container || !idxData) return;

            // Preserve title label
            const titleText = idx === 'SCFI' ? 'SELECT ROUTES:' : 'SELECT INDICES:';
            container.innerHTML = `<span class="mi-chips-title">${{titleText}}</span>`;

            idxData.regions.forEach((reg, i) => {{
                const isSelected = selectedRegions[idx].includes(reg);
                const color = PALETTE[i % PALETTE.length];

                const chip = document.createElement('div');
                chip.className = `mi-chip ${{isSelected ? 'active' : ''}}`;

                const dot = document.createElement('span');
                dot.className = 'mi-chip-color-dot';
                dot.style.backgroundColor = isSelected ? color : 'var(--text-muted)';
                chip.appendChild(dot);

                const label = document.createElement('span');
                label.textContent = reg;
                chip.appendChild(label);

                chip.addEventListener('click', () => {{
                    toggleRegionSelection(idx, reg);
                }});

                container.appendChild(chip);
            }});
        }}

        function toggleRegionSelection(idx, reg) {{
            const currentList = selectedRegions[idx];
            const idxInList = currentList.indexOf(reg);

            if (idxInList >= 0) {{
                // Prevent unselecting all (at least 1 must remain)
                if (currentList.length > 1) {{
                    currentList.splice(idxInList, 1);
                }}
            }} else {{
                currentList.push(reg);
            }}

            renderChipsUI(idx);
            renderIndexPanel(idx);
        }}

        function initControls() {{
            document.querySelectorAll('#range-btn-group .mi-btn-toggle').forEach(btn => {{
                btn.addEventListener('click', (e) => {{
                    document.querySelectorAll('#range-btn-group .mi-btn-toggle').forEach(b => b.classList.remove('active'));
                    e.currentTarget.classList.add('active');
                    currentRange = e.currentTarget.getAttribute('data-range');
                    renderAllPanels();
                }});
            }});
        }}

        function renderAllPanels() {{
            ['SCFI', 'BDI'].forEach(idx => renderIndexPanel(idx));
        }}

        function filterSeriesByRange(dates, values, range) {{
            if (range === 'ALL' || !dates.length) return {{ dates, values }};

            const latestStr = dates[dates.length - 1];
            const latestDate = new Date(latestStr);
            const cutoff = new Date(latestDate);

            switch (range) {{
                case '1M': cutoff.setMonth(cutoff.getMonth() - 1); break;
                case '3M': cutoff.setMonth(cutoff.getMonth() - 3); break;
                case '6M': cutoff.setMonth(cutoff.getMonth() - 6); break;
                case '1Y': cutoff.setFullYear(cutoff.getFullYear() - 1); break;
                case '3Y': cutoff.setFullYear(cutoff.getFullYear() - 3); break;
                case '5Y': cutoff.setFullYear(cutoff.getFullYear() - 5); break;
                default: return {{ dates, values }};
            }}

            const cutoffStr = cutoff.toISOString().split('T')[0];
            const filteredDates = [];
            const filteredValues = [];

            for (let i = 0; i < dates.length; i++) {{
                if (dates[i] >= cutoffStr) {{
                    filteredDates.push(dates[i]);
                    filteredValues.push(values[i]);
                }}
            }}

            return {{ dates: filteredDates, values: filteredValues }};
        }}

        function renderIndexPanel(indexName) {{
            const key = indexName.toLowerCase();
            const idxData = PAYLOAD_DATA.indexes[indexName];
            const activeRegs = selectedRegions[indexName];

            if (!idxData || !idxData.series || !activeRegs || !activeRegs.length) {{
                renderEmptyPanel(indexName);
                return;
            }}

            const traces = [];
            const filteredDataByReg = {{}};
            const allDateSet = new Set();

            // 1. Process traces for each selected region
            activeRegs.forEach(reg => {{
                const rawSeries = idxData.series[reg];
                if (!rawSeries) return;

                const {{ dates, values }} = filterSeriesByRange(rawSeries.dates, rawSeries.values, currentRange);
                if (!dates.length) return;

                filteredDataByReg[reg] = {{ dates, values }};
                dates.forEach(d => allDateSet.add(d));

                const regIndex = idxData.regions.indexOf(reg);
                const color = PALETTE[regIndex >= 0 ? regIndex % PALETTE.length : 0];

                traces.push({{
                    x: dates,
                    y: values,
                    name: reg,
                    type: 'scatter',
                    mode: 'lines',
                    line: {{
                        color: color,
                        width: 2,
                        shape: 'spline',
                        smoothing: 0.3
                    }},
                    hoverinfo: 'x+y+name',
                    hovertemplate: `<b>${{reg}}</b><br>Date: %{{x}}<br>Value: %{{y:,.2f}}<extra></extra>`
                }});
            }});

            if (!traces.length) {{
                renderEmptyPanel(indexName);
                return;
            }}

            // 2. Primary KPI Metrics based on the first selected region
            const primaryReg = activeRegs[0];
            const primarySeries = filteredDataByReg[primaryReg];

            if (primarySeries && primarySeries.values.length) {{
                const vals = primarySeries.values;
                const dts = primarySeries.dates;
                const latestVal = vals[vals.length - 1];
                const latestDate = dts[dts.length - 1];
                const prevVal = vals.length > 1 ? vals[vals.length - 2] : latestVal;

                const chg = latestVal - prevVal;
                const pct = prevVal !== 0 ? (chg / prevVal) * 100 : 0;

                let highVal = -Infinity, lowVal = Infinity;
                let highDate = '', lowDate = '';

                for (let i = 0; i < vals.length; i++) {{
                    if (vals[i] > highVal) {{ highVal = vals[i]; highDate = dts[i]; }}
                    if (vals[i] < lowVal) {{ lowVal = vals[i]; lowDate = dts[i]; }}
                }}

                document.getElementById(`kpi-${{key}}-latest`).textContent = latestVal.toLocaleString(undefined, {{ minimumFractionDigits: 1, maximumFractionDigits: 2 }});
                document.getElementById(`kpi-${{key}}-date`).textContent = `${{primaryReg}} (${{latestDate}})`;

                const chgElem = document.getElementById(`kpi-${{key}}-chg`);
                const pctElem = document.getElementById(`kpi-${{key}}-pct`);

                const chgPrefix = chg > 0 ? '+' : '';
                chgElem.textContent = `${{chgPrefix}}${{chg.toFixed(2)}}`;
                pctElem.textContent = `(${{chgPrefix}}${{pct.toFixed(2)}}%)`;

                chgElem.className = 'mi-kpi-val ' + (chg > 0 ? 'val-up' : chg < 0 ? 'val-down' : 'val-neutral');
                pctElem.className = 'mi-kpi-sub ' + (chg > 0 ? 'val-up' : chg < 0 ? 'val-down' : 'val-neutral');

                document.getElementById(`kpi-${{key}}-high`).textContent = highVal.toLocaleString(undefined, {{ minimumFractionDigits: 1, maximumFractionDigits: 2 }});
                document.getElementById(`kpi-${{key}}-high-date`).textContent = highDate;
                document.getElementById(`kpi-${{key}}-low`).textContent = lowVal.toLocaleString(undefined, {{ minimumFractionDigits: 1, maximumFractionDigits: 2 }});
                document.getElementById(`kpi-${{key}}-low-date`).textContent = lowDate;

                // Sync Global Date
                if (indexName === 'SCFI' || document.getElementById('global-sync-date').textContent === '--') {{
                    document.getElementById('global-sync-date').textContent = latestDate;
                }}
            }}

            // 3. Render Plotly Multiline Chart
            const layout = {{
                paper_bgcolor: 'transparent',
                plot_bgcolor: 'transparent',
                margin: {{ l: 50, r: 20, t: 20, b: 40 }},
                autosize: true,
                showlegend: true,
                legend: {{
                    orientation: 'h',
                    x: 0,
                    y: 1.15,
                    font: {{ family: 'JetBrains Mono', size: 10, color: '#8b949e' }}
                }},
                xaxis: {{
                    type: 'date',
                    gridcolor: '#1b2430',
                    linecolor: '#243142',
                    tickfont: {{ family: 'JetBrains Mono', size: 10, color: '#8b949e' }},
                    showspikes: true,
                    spikethickness: 1,
                    spikedash: 'dot',
                    spikecolor: '#ff9800'
                }},
                yaxis: {{
                    gridcolor: '#1b2430',
                    linecolor: '#243142',
                    tickfont: {{ family: 'JetBrains Mono', size: 10, color: '#8b949e' }},
                    side: 'left',
                    showspikes: true,
                    spikethickness: 1,
                    spikedash: 'dot',
                    spikecolor: '#ff9800'
                }},
                hoverlabel: {{
                    bgcolor: '#151c26',
                    bordercolor: '#ff9800',
                    font: {{ family: 'JetBrains Mono', size: 11, color: '#e6edf3' }}
                }}
            }};

            const config = {{
                responsive: true,
                displayModeBar: false
            }};

            Plotly.newPlot(`chart-${{key}}`, traces, layout, config);

            // 4. Render Pivot Table
            renderMultiTable(indexName, activeRegs, filteredDataByReg, Array.from(allDateSet).sort());
        }}

        function renderMultiTable(indexName, activeRegs, filteredDataByReg, sortedDates) {{
            const key = indexName.toLowerCase();
            const headRow = document.getElementById(`table-head-${{key}}`);
            const tbody = document.querySelector(`#table-${{key}} tbody`);

            // Dynamically construct Table Headers
            headRow.innerHTML = '<th>DATE</th>';
            activeRegs.forEach(reg => {{
                const th = document.createElement('th');
                th.textContent = reg;
                headRow.appendChild(th);
            }});

            // Construct Quick Map per Region: date -> value
            const dateValMaps = {{}};
            activeRegs.forEach(reg => {{
                dateValMaps[reg] = {{}};
                if (filteredDataByReg[reg]) {{
                    const dts = filteredDataByReg[reg].dates;
                    const vals = filteredDataByReg[reg].values;
                    for (let i = 0; i < dts.length; i++) {{
                        dateValMaps[reg][dts[i]] = vals[i];
                    }}
                }}
            }});

            tbody.innerHTML = '';
            const tableLen = Math.min(sortedDates.length, 50);

            for (let i = sortedDates.length - 1; i >= sortedDates.length - tableLen; i--) {{
                const d = sortedDates[i];
                const tr = document.createElement('tr');

                let rowHtml = `<td style="color: var(--text-secondary);">${{d}}</td>`;
                activeRegs.forEach(reg => {{
                    const v = dateValMaps[reg][d];
                    if (v !== undefined) {{
                        rowHtml += `<td style="font-weight: 600;">${{v.toLocaleString(undefined, {{ minimumFractionDigits: 1, maximumFractionDigits: 2 }})}}</td>`;
                    }} else {{
                        rowHtml += `<td style="color: var(--text-muted);">-</td>`;
                    }}
                }});

                tr.innerHTML = rowHtml;
                tbody.appendChild(tr);
            }}
        }}

        function renderEmptyPanel(indexName) {{
            const key = indexName.toLowerCase();
            document.getElementById(`kpi-${{key}}-latest`).textContent = '--';
            document.getElementById(`kpi-${{key}}-date`).textContent = '--';
            document.getElementById(`kpi-${{key}}-chg`).textContent = '--';
            document.getElementById(`kpi-${{key}}-pct`).textContent = '--';
            document.getElementById(`chart-${{key}}`).innerHTML = '<div class="mi-empty">NO DATA AVAILABLE IN SELECTED RANGE</div>';
            document.querySelector(`#table-${{key}} tbody`).innerHTML = '';
        }}

        function updateTopTickers() {{
            ['SCFI', 'BDI'].forEach(idx => {{
                const key = idx.toLowerCase();
                const idxData = PAYLOAD_DATA.indexes[idx];
                if (!idxData || !idxData.regions || !idxData.regions.length) return;

                const primaryReg = idxData.regions[0];
                const series = idxData.series[primaryReg];
                if (!series || !series.values.length) return;

                const vals = series.values;
                const latest = vals[vals.length - 1];
                const prev = vals.length > 1 ? vals[vals.length - 2] : latest;
                const chg = latest - prev;
                const pct = prev !== 0 ? (chg / prev) * 100 : 0;

                const valElem = document.getElementById(`ticker-${{key}}-val`);
                const chgElem = document.getElementById(`ticker-${{key}}-chg`);

                valElem.textContent = latest.toLocaleString(undefined, {{ minimumFractionDigits: 1, maximumFractionDigits: 2 }});
                const prefix = chg > 0 ? '+' : '';
                chgElem.textContent = `${{prefix}}${{pct.toFixed(2)}}%`;
                chgElem.className = 'mi-ticker-chg ' + (chg > 0 ? 'val-up' : chg < 0 ? 'val-down' : 'val-neutral');
            }});
        }}

        function exportCSV(indexName) {{
            const idxData = PAYLOAD_DATA.indexes[indexName];
            const activeRegs = selectedRegions[indexName];
            if (!idxData || !idxData.series || !activeRegs || !activeRegs.length) {{
                alert('No data available to export.');
                return;
            }}

            let csvContent = 'data:text/csv;charset=utf-8,Date,Index,Region,Value\\n';

            activeRegs.forEach(reg => {{
                const series = idxData.series[reg];
                if (series) {{
                    for (let i = 0; i < series.dates.length; i++) {{
                        csvContent += `${{series.dates[i]}},${{indexName}},${{reg}},${{series.values[i]}}\\n`;
                    }}
                }}
            }});

            const encodedUri = encodeURI(csvContent);
            const link = document.createElement('a');
            link.setAttribute('href', encodedUri);
            link.setAttribute('download', `${{indexName}}_multi_series.csv`);
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
        }}
    </script>
</body>
</html>
"""

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"[SUCCESS] Dashboard exported successfully: {html_path}")

# =============================================================================
# 5. Main Execution Flow
# =============================================================================

def main():
    print(
        f"================================================================================"
    )
    print(f" Shipping Index Automated Pipeline (SQLite & Static Exporter)")
    print(f" Execution Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S KST')}")
    print(
        f"================================================================================"
    )

    init_db(DB_PATH)

    ksg_id = os.environ.get("KSG_ID", "").strip()
    ksg_pw = os.environ.get("KSG_PW", "").strip()

    if ksg_id and ksg_pw:
        print("[INFO] Credentials detected. Initializing KSG Crawler...")
        crawler = KSGCrawler(user_id=ksg_id, user_pw=ksg_pw, headless=True)

        login_success = crawler.login()
        if login_success:
            for idx_name in SUPPORTED_INDEXES:
                start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                latest_dt = get_latest_date_in_db(DB_PATH, idx_name)
                print(
                    f"[INFO] Index [{idx_name}] - Current latest date in DB: {latest_dt}"
                )

                if idx_name == "SCFI":
                    scraped_records = crawler.scrape_scfi_recent(
                        stop_date=latest_dt
                    )
                elif idx_name == "BDI":
                    scraped_records = crawler.scrape_bdi_recent(
                        stop_date=latest_dt
                    )
                else:
                    scraped_records = []

                rows_added = upsert_index_records(DB_PATH, scraped_records)
                finish_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                conn = sqlite3.connect(DB_PATH)
                cur = conn.cursor()
                cur.execute(
                    "SELECT COUNT(*) FROM index_raw WHERE index_name = ?",
                    (idx_name,),
                )
                total_cnt = cur.fetchone()[0]
                conn.close()

                log_audit(
                    DB_PATH,
                    started_at=start_time,
                    finished_at=finish_time,
                    status="SUCCESS" if rows_added >= 0 else "FAILED",
                    message=f"Added {rows_added} new records",
                    rows_added=rows_added,
                    total_rows=total_cnt,
                    index_name=idx_name,
                )
        else:
            print(
                "[WARN] Login failed. Skipping incremental scraping, using existing DB..."
            )

        crawler.close()
    else:
        print(
            "[WARN] KSG_ID/KSG_PW not provided. Skipping scraping, refreshing static assets from DB..."
        )

    generate_static_files(DB_PATH, JSON_PATH, HTML_PATH)

if __name__ == "__main__":
    main()
