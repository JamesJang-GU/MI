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

BDI_REGION_PRIORITY = [
    "BDI",
    "BCI",
    "BPI",
    "BSI",
    "BHSI",
    "BDTI",
    "BCTI"
]

INVALID_REGION_NAMES = {
    "",
    "일자",
    "날짜",
    "날 자",
    "DATE",
    "Date",
    "date",
    "YEAR",
    "Year",
    "year",
    "INDEX_NAME",
    "CRAWL_PAGE"
}

# =============================================================================
# 2. Common Functions
# =============================================================================

def log(message):
    """Print timestamped console log."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}")

def now_string():
    """Return current timestamp string."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def atomic_write_text(file_path, content):
    """
    Write a file atomically.
    Prevents partial output files if execution is interrupted.
    """
    file_path.parent.mkdir(parents=True, exist_ok=True)

    temp_path = file_path.with_suffix(file_path.suffix + ".tmp")

    with open(temp_path, "w", encoding="utf-8", newline="\n") as file:
        file.write(content)

    temp_path.replace(file_path)

def normalize_index_name(index_name):
    """Normalize index name."""
    index_name = str(index_name or "").strip().upper()

    if index_name in SUPPORTED_INDEXES:
        return index_name

    return "SCFI"

def is_valid_region_for_index(region, index_name):
    """
    Filter invalid table headers / region names.

    BDI accepts only standard Baltic sub-index names.
    """
    region = str(region or "").strip()
    index_name = normalize_index_name(index_name)

    if region in INVALID_REGION_NAMES:
        return False

    if index_name == "BDI":
        return region in BDI_REGION_PRIORITY

    return True

def parse_date_value(value):
    """Parse date values from KSG table."""
    value = str(value or "").strip()

    date_formats = [
        "%Y-%m-%d",
        "%Y.%m.%d",
        "%Y/%m/%d",
        "%y-%m-%d",
        "%y.%m.%d",
        "%y/%m/%d"
    ]

    for date_format in date_formats:
        try:
            return datetime.strptime(value, date_format).date()
        except ValueError:
            continue

    return value

def clean_number(value):
    """Convert displayed numeric values to float."""
    value = str(value or "").strip().replace(",", "")

    if not value:
        return None

    if re.fullmatch(r"[+-]?\d+(\.\d+)?", value):
        return float(value)

    return None

# =============================================================================
# 3. SQLite Database Functions
# =============================================================================

def get_db_connection():
    """
    Create and return SQLite connection.

    index_raw:
        Raw long-format data collected from KSG.

    crawl_audit:
        Execution log for each SCFI / BDI crawl.
    """
    connection = sqlite3.connect(DB_PATH)

    connection.execute("""
        CREATE TABLE IF NOT EXISTS index_raw (
            dt TEXT NOT NULL,
            region TEXT NOT NULL,
            value REAL,
            index_name TEXT NOT NULL,
            source_file TEXT,
            created_at TEXT,
            PRIMARY KEY (dt, region, index_name)
        )
    """)

    connection.execute("""
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
    """)

    connection.commit()

    return connection

def get_last_saved_date(index_name):
    """Return latest stored date for incremental crawl."""
    index_name = normalize_index_name(index_name)

    query = """
        SELECT MAX(dt)
        FROM index_raw
        WHERE index_name = ?
    """

    with get_db_connection() as connection:
        row = connection.execute(
            query,
            (index_name,)
        ).fetchone()

    if not row or not row[0]:
        return None

    try:
        return datetime.strptime(row[0], "%Y-%m-%d").date()
    except ValueError:
        return None

def get_total_record_count(index_name):
    """Return total row count for one index."""
    index_name = normalize_index_name(index_name)

    with get_db_connection() as connection:
        row = connection.execute(
            """
            SELECT COUNT(*)
            FROM index_raw
            WHERE index_name = ?
            """,
            (index_name,)
        ).fetchone()

    return int(row[0] or 0)

def cleanup_invalid_regions():
    """
    Remove legacy invalid rows, such as:
    - 일자
    - DATE
    - incorrect BDI column names
    """
    try:
        with get_db_connection() as connection:
            cursor = connection.cursor()

            invalid_names = sorted(INVALID_REGION_NAMES)
            invalid_placeholders = ",".join(
                ["?"] * len(invalid_names)
            )

            cursor.execute(
                f"""
                DELETE FROM index_raw
                WHERE TRIM(region) IN ({invalid_placeholders})
                """,
                invalid_names
            )

            bdi_placeholders = ",".join(
                ["?"] * len(BDI_REGION_PRIORITY)
            )

            cursor.execute(
                f"""
                DELETE FROM index_raw
                WHERE index_name = 'BDI'
                AND TRIM(region) NOT IN ({bdi_placeholders})
                """,
                BDI_REGION_PRIORITY
            )

            connection.commit()

        log("[DB] Invalid region cleanup complete.")

    except Exception as error:
        log(f"[WARNING] Invalid region cleanup failed: {error}")

def write_crawl_audit(
    index_name,
    started_at,
    status,
    message,
    rows_added,
    total_rows
):
    """Write crawl result to audit table."""
    index_name = normalize_index_name(index_name)

    with get_db_connection() as connection:
        connection.execute(
            """
            INSERT INTO crawl_audit (
                started_at,
                finished_at,
                status,
                message,
                rows_added,
                total_rows,
                index_name
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                started_at,
                now_string(),
                status,
                message,
                rows_added,
                total_rows,
                index_name
            )
        )

        connection.commit()

# =============================================================================
# 4. Selenium / KSG Login Functions
# =============================================================================

def get_driver():
    """
    Initialize Chrome WebDriver.

    GitHub Actions ubuntu-latest normally provides Chrome and ChromeDriver.
    A custom driver path can optionally be supplied through CHROMEDRIVER_PATH.
    """
    options = webdriver.ChromeOptions()

    options.add_argument("--headless=new")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--lang=ko-KR")
    options.add_argument("--window-size=1600,1000")

    chrome_binary = os.getenv("CHROME_BINARY", "").strip()

    if chrome_binary:
        options.binary_location = chrome_binary

    driver_path = os.getenv("CHROMEDRIVER_PATH", "").strip()

    if driver_path:
        log(f"[DRIVER] Custom ChromeDriver path: {driver_path}")

        return webdriver.Chrome(
            service=Service(driver_path),
            options=options
        )

    return webdriver.Chrome(options=options)

def handle_login_alerts(driver, timeout=1.0, max_alerts=3):
    """Accept browser alert messages after login."""
    for _ in range(max_alerts):
        try:
            WebDriverWait(driver, timeout).until(
                EC.alert_is_present()
            )

            alert = driver.switch_to.alert

            log(f"[ALERT] {alert.text}")

            alert.accept()

            time.sleep(0.2)

        except (TimeoutException, NoAlertPresentException):
            return

def login_ksg(driver):
    """
    Login to Korea Shipping Gazette.

    Credentials must come from:
    - GitHub Actions Secrets
    - local environment variables

    KSG_ID
    KSG_PW
    """
    user_id = os.getenv("KSG_ID", "").strip()
    password = os.getenv("KSG_PW", "").strip()

    if not user_id or not password:
        raise RuntimeError(
            "KSG_ID 또는 KSG_PW 환경변수가 없습니다. "
            "GitHub Actions Secrets 설정을 확인하세요."
        )

    log("[LOGIN] KSG 로그인 페이지 접속 중...")

    driver.get(BASE_URL)

    wait = WebDriverWait(driver, 20)

    wait.until(
        EC.element_to_be_clickable(
            (By.LINK_TEXT, "로그인")
        )
    ).click()

    id_input = wait.until(
        EC.presence_of_element_located(
            (By.ID, "loginId")
        )
    )

    password_input = wait.until(
        EC.presence_of_element_located(
            (By.ID, "loginPass")
        )
    )

    id_input.clear()
    id_input.send_keys(user_id)

    password_input.clear()
    password_input.send_keys(password)

    driver.execute_script(
        "loginSubmitForm(document.form1);"
    )

    handle_login_alerts(driver)

    time.sleep(1.0)

    # Login input still present generally means login failed.
    visible_login_inputs = [
        element
        for element in driver.find_elements(By.ID, "loginId")
        if element.is_displayed()
    ]

    if visible_login_inputs:
        raise RuntimeError(
            "KSG 로그인에 실패했습니다. "
            "KSG_ID / KSG_PW를 확인하세요."
        )

    log("[LOGIN] KSG 로그인 성공")

def go_to_index_page(driver, index_name):
    """Navigate to SCFI or BDI page."""
    index_name = normalize_index_name(index_name)

    log(f"[CRAWL] {index_name} 메인 페이지 이동 중...")

    wait = WebDriverWait(driver, 20)

    wait.until(
        EC.element_to_be_clickable(
            (By.LINK_TEXT, index_name)
        )
    ).click()

    try:
        wait.until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "table")
            )
        )
    except TimeoutException:
        pass

    time.sleep(0.8)

    log(f"[CRAWL] {index_name} 메인 페이지 진입 완료")

# =============================================================================
# 5. KSG Table Parsing / Pagination
# =============================================================================

def parse_current_table(driver, index_name):
    """
    Extract currently visible KSG table.

    Returns a wide-format DataFrame.
    First column is expected to be date.
    Remaining columns are SCFI routes or BDI sub-indexes.
    """
    index_name = normalize_index_name(index_name)

    script = f"""
    const INDEX_NAME = "{index_name}";
    const tables = Array.from(document.querySelectorAll("table"));
    let target = null;

    for (const table of tables) {{
      const caption = (
        table.querySelector("caption")?.innerText || ""
      ).toUpperCase();

      const text = (table.innerText || "").toUpperCase();

      if (
        (
          caption.includes(INDEX_NAME) ||
          text.includes(INDEX_NAME) ||
          text.includes("일자")
        ) &&
        table.querySelector("tbody tr")
      ) {{
        target = table;
        break;
      }}
    }}

    if (!target) {{
      let maximumRowCount = 0;

      for (const table of tables) {{
        const rowCount = table.querySelectorAll("tbody tr").length;

        if (rowCount > maximumRowCount) {{
          maximumRowCount = rowCount;
          target = table;
        }}
      }}
    }}

    if (!target) {{
      return {{
        headers: [],
        rows: []
      }};
    }}

    const headerRows = Array.from(
      target.querySelectorAll("thead tr")
    );

    let headers = [];

    if (headerRows.length > 0) {{
      headers = Array.from(
        headerRows[headerRows.length - 1]
          .querySelectorAll("th, td")
      ).map(cell => (cell.innerText || "").trim());
    }}

    const rows = Array.from(
      target.querySelectorAll("tbody tr")
    ).map(row =>
      Array.from(
        row.querySelectorAll("th, td")
      ).map(cell => (cell.innerText || "").trim())
    ).filter(row => row.length > 0);

    return {{
      headers: headers,
      rows: rows
    }};
    """

    data = driver.execute_script(script)

    headers = data.get("headers", [])
    rows = data.get("rows", [])

    if not rows:
        return pd.DataFrame()

    maximum_columns = max(len(row) for row in rows)

    if len(headers) < maximum_columns:
        headers.extend(
            [
                f"COL_{number}"
                for number in range(
                    len(headers) + 1,
                    maximum_columns + 1
                )
            ]
        )

    normalized_rows = []

    for raw_row in rows:
        raw_row = list(raw_row) + [""] * (
            len(headers) - len(raw_row)
        )

        row = {}

        for position, value in enumerate(raw_row):
            column_name = headers[position] or f"COL_{position + 1}"

            if position == 0:
                row[column_name] = parse_date_value(value)
            else:
                row[column_name] = clean_number(value)

        normalized_rows.append(row)

    return pd.DataFrame(normalized_rows)

def get_table_signature(driver):
    """
    Get page/table signature to prevent duplicate page loops.
    """
    script = """
    const tables = Array.from(document.querySelectorAll("table"));

    let target = null;
    let maximumRowCount = 0;

    for (const table of tables) {
      const rowCount = table.querySelectorAll("tbody tr").length;

      if (rowCount > maximumRowCount) {
        maximumRowCount = rowCount;
        target = table;
      }
    }

    if (!target) return "";

    const firstRow = target.querySelector("tbody tr");

    const rowSignature = firstRow
      ? Array.from(firstRow.querySelectorAll("th, td"))
          .slice(0, 4)
          .map(cell => (cell.innerText || "").trim())
          .join("|")
      : "";

    const pageSelectors = [
      ".paginate strong",
      ".paging strong",
      ".pagination .active",
      ".paginate .on"
    ];

    let currentPage = "";

    for (const selector of pageSelectors) {
      const element = document.querySelector(selector);

      if (element) {
        currentPage = (element.innerText || "").trim();

        if (currentPage) break;
      }
    }

    return currentPage + "||" + rowSignature;
    """

    try:
        return str(
            driver.execute_script(script) or ""
        ).strip()

    except Exception:
        return ""

def wait_for_table_change(driver, previous_signature, timeout=10):
    """Wait until table/pagination data changes."""
    start_time = time.time()

    while time.time() - start_time < timeout:
        current_signature = get_table_signature(driver)

        if (
            current_signature
            and current_signature != previous_signature
        ):
            return True

        time.sleep(0.2)

    return False

def get_current_page_number(driver):
    """Read selected pagination page number."""
    selectors = [
        ".paginate strong",
        ".paging strong",
        ".pagination .active",
        ".paginate .on"
    ]

    for selector in selectors:
        try:
            elements = driver.find_elements(
                By.CSS_SELECTOR,
                selector
            )

            for element in elements:
                text = element.text.strip()

                if text.isdigit():
                    return int(text)

        except Exception:
            continue

    return None

def try_javascript_click(driver, element):
    """Safely click an element using JavaScript."""
    try:
        if not element.is_displayed():
            return False

        driver.execute_script(
            "arguments[0].scrollIntoView({block:'center'});",
            element
        )

        driver.execute_script(
            "arguments[0].click();",
            element
        )

        return True

    except Exception:
        return False

def move_to_next_page(driver):
    """Move to next KSG table page."""
    current_page = get_current_page_number(driver)

    if current_page is not None:
        target_page = current_page + 1

        try:
            has_go_page = driver.execute_script(
                "return typeof goPage === 'function';"
            )

            if has_go_page:
                driver.execute_script(
                    f"goPage({target_page});"
                )

                return True

        except Exception:
            pass

        xpaths = [
            f"//a[normalize-space(text())='{target_page}']",
            f"//span[normalize-space(text())='{target_page}']/parent::a"
        ]

        for xpath in xpaths:
            try:
                candidates = driver.find_elements(
                    By.XPATH,
                    xpath
                )

                for candidate in candidates:
                    if try_javascript_click(driver, candidate):
                        return True

            except Exception:
                continue

    selectors = [
        (By.CSS_SELECTOR, "a.next"),
        (By.CSS_SELECTOR, ".paginate a.next"),
        (By.CSS_SELECTOR, ".paging a.next"),
        (By.LINK_TEXT, "다음"),
        (By.XPATH, "//a[contains(text(), '다음')]")
    ]

    for by, selector in selectors:
        try:
            candidates = driver.find_elements(by, selector)

            for candidate in candidates:
                if try_javascript_click(driver, candidate):
                    return True

        except Exception:
            continue

    return False

def crawl_index_pages(
    driver,
    index_name,
    stop_date=None,
    max_pages=100000,
    pause_seconds=0.3
):
    """
    Crawl pages incrementally.

    When a previously stored date is found, crawling stops.
    """
    index_name = normalize_index_name(index_name)

    log(
        f"[CRAWL] {index_name} 수집 시작 / "
        f"증분 기준일: {stop_date or '없음'}"
    )

    all_frames = []
    seen_signatures = set()

    for page_number in range(1, max_pages + 1):
        df = pd.DataFrame()

        for _ in range(4):
            try:
                df = parse_current_table(driver, index_name)

                if not df.empty:
                    break

            except Exception:
                pass

            time.sleep(0.5)

        if df.empty:
            log(
                f"[CRAWL] {index_name} "
                f"{page_number}페이지: 데이터 없음"
            )
            break

        date_column = df.columns[0]

        df_dates = pd.to_datetime(
            df[date_column],
            errors="coerce"
        )

        # Incremental crawling:
        # The table normally starts from latest records.
        if stop_date is not None:
            newer_records = df[
                df_dates.dt.date > stop_date
            ].copy()

            # Existing date found on this page.
            if len(newer_records) < len(df):
                if not newer_records.empty:
                    newer_records["CRAWL_PAGE"] = page_number
                    all_frames.append(newer_records)

                log(
                    f"[CRAWL] {index_name} "
                    f"{page_number}페이지에서 기존 기준일 발견, 종료"
                )
                break

            df = newer_records

        signature = get_table_signature(driver)

        if not signature:
            first_date = str(df.iloc[0, 0]) if not df.empty else ""
            signature = f"{page_number}||{first_date}"

        if signature in seen_signatures:
            log(
                f"[CRAWL] {index_name}: "
                "반복 페이지 감지, 종료"
            )
            break

        seen_signatures.add(signature)

        df["CRAWL_PAGE"] = page_number
        all_frames.append(df)

        log(
            f"[CRAWL] {index_name} "
            f"{page_number}페이지 완료: {len(df):,}행"
        )

        previous_signature = signature

        if not move_to_next_page(driver):
            log(
                f"[CRAWL] {index_name}: "
                "다음 페이지 없음"
            )
            break

        if not wait_for_table_change(
            driver,
            previous_signature,
            timeout=10
        ):
            log(
                f"[CRAWL] {index_name}: "
                "페이지 전환 시간 초과"
            )
            break

        if pause_seconds > 0:
            time.sleep(pause_seconds)

    if not all_frames:
        return pd.DataFrame()

    return pd.concat(all_frames, ignore_index=True)

# =============================================================================
# 6. Data Conversion / DB Upsert
# =============================================================================

def convert_wide_to_long(df, index_name):
    """
    Convert KSG table from wide format to long format.

    Wide example:
        DATE        Europe    US West
        2026-01-01  2000      1500

    Long result:
        DATE        REGION    VALUE    INDEX_NAME
        2026-01-01  Europe    2000     SCFI
        2026-01-01  US West   1500     SCFI
    """
    index_name = normalize_index_name(index_name)

    empty_df = pd.DataFrame(
        columns=["DATE", "REGION", "VALUE", "INDEX_NAME"]
    )

    if df is None or df.empty:
        return empty_df

    working = df.copy()
    date_column = working.columns[0]

    working[date_column] = pd.to_datetime(
        working[date_column],
        errors="coerce"
    )

    working = working.dropna(subset=[date_column])

    if working.empty:
        return empty_df

    excluded_columns = {
        date_column,
        "INDEX_NAME",
        "CRAWL_PAGE"
    }

    value_columns = [
        column
        for column in working.columns
        if column not in excluded_columns
        and is_valid_region_for_index(column, index_name)
    ]

    numeric_columns = []

    for column in value_columns:
        numeric_values = pd.to_numeric(
            working[column],
            errors="coerce"
        )

        if numeric_values.notna().any():
            working[column] = numeric_values
            numeric_columns.append(column)

    if not numeric_columns:
        return empty_df

    long_df = working.melt(
        id_vars=[date_column],
        value_vars=numeric_columns,
        var_name="REGION",
        value_name="VALUE"
    )

    long_df = long_df.rename(
        columns={date_column: "DATE"}
    )

    long_df = long_df.dropna(
        subset=["DATE", "VALUE"]
    )

    long_df["REGION"] = (
        long_df["REGION"]
        .astype(str)
        .str.strip()
    )

    long_df = long_df[
        long_df["REGION"].apply(
            lambda region: is_valid_region_for_index(
                region,
                index_name
            )
        )
    ]

    if long_df.empty:
        return empty_df

    long_df["DATE"] = pd.to_datetime(
        long_df["DATE"]
    ).dt.strftime("%Y-%m-%d")

    long_df["VALUE"] = pd.to_numeric(
        long_df["VALUE"],
        errors="coerce"
    )

    long_df = long_df.dropna(
        subset=["VALUE"]
    )

    long_df["VALUE"] = long_df["VALUE"].astype(float)
    long_df["INDEX_NAME"] = index_name

    long_df = long_df[
        ["DATE", "REGION", "VALUE", "INDEX_NAME"]
    ].drop_duplicates(
        subset=["DATE", "REGION", "INDEX_NAME"],
        keep="last"
    )

    return long_df.sort_values(
        by=["DATE", "REGION"]
    ).reset_index(drop=True)

def upsert_index_data(long_df, index_name):
    """Insert/update long-format index data in index_raw."""
    index_name = normalize_index_name(index_name)

    if long_df is None or long_df.empty:
        return 0, get_total_record_count(index_name)

    processed_rows = 0
    created_at = now_string()

    with get_db_connection() as connection:
        cursor = connection.cursor()

        for _, row in long_df.iterrows():
            cursor.execute(
                """
                INSERT INTO index_raw (
                    dt,
                    region,
                    value,
                    index_name,
                    source_file,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)

                ON CONFLICT(dt, region, index_name)
                DO UPDATE SET
                    value = excluded.value,
                    source_file = excluded.source_file,
                    created_at = excluded.created_at
                """,
                (
                    row["DATE"],
                    row["REGION"],
                    float(row["VALUE"]),
                    index_name,
                    "Korea Shipping Gazette",
                    created_at
                )
            )

            processed_rows += 1

        connection.commit()

    total_rows = get_total_record_count(index_name)

    return processed_rows, total_rows

# =============================================================================
# 7. Crawler Execution
# =============================================================================

def crawl_and_save_index(index_name):
    """Crawl and save one index: SCFI or BDI."""
    index_name = normalize_index_name(index_name)

    started_at = now_string()
    driver = None

    try:
        stop_date = get_last_saved_date(index_name)

        log("=" * 72)
        log(f"[CRAWL] {index_name} 크롤링 시작")
        log(f"[CRAWL] 기존 DB 최종일: {stop_date or '없음'}")
        log("=" * 72)

        driver = get_driver()

        login_ksg(driver)

        go_to_index_page(driver, index_name)

        wide_df = crawl_index_pages(
            driver=driver,
            index_name=index_name,
            stop_date=stop_date
        )

        long_df = convert_wide_to_long(
            wide_df,
            index_name
        )

        rows_added, total_rows = upsert_index_data(
            long_df,
            index_name
        )

        if total_rows == 0:
            raise RuntimeError(
                f"{index_name} DB 누적 데이터가 0건입니다. "
                "KSG 로그인, 메뉴, 테이블 구조 및 컬럼명을 확인하세요."
            )

        write_crawl_audit(
            index_name=index_name,
            started_at=started_at,
            status="SUCCESS",
            message="OK",
            rows_added=rows_added,
            total_rows=total_rows
        )

        log(
            f"[CRAWL] {index_name} 완료 / "
            f"이번 처리: {rows_added:,}건 / "
            f"누적: {total_rows:,}건"
        )

        return {
            "ok": True,
            "index_name": index_name,
            "rows_added": rows_added,
            "total_rows": total_rows
        }

    except Exception as error:
        total_rows = get_total_record_count(index_name)

        write_crawl_audit(
            index_name=index_name,
            started_at=started_at,
            status="ERROR",
            message=str(error)[:1000],
            rows_added=0,
            total_rows=total_rows
        )

        log(f"[ERROR] {index_name} 크롤링 실패: {error}")

        return {
            "ok": False,
            "index_name": index_name,
            "error": str(error),
            "total_rows": total_rows
        }

    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

def crawl_all_indexes():
    """Crawl SCFI and BDI sequentially."""
    log("[CRAWL] SCFI / BDI 통합 크롤링 시작")

    results = []

    for index_name in SUPPORTED_INDEXES:
        result = crawl_and_save_index(index_name)
        results.append(result)

    failed_results = [
        result
        for result in results
        if not result.get("ok")
    ]

    if failed_results:
        error_message = " | ".join(
            f"{result['index_name']}: "
            f"{result.get('error', 'Unknown error')}"
            for result in failed_results
        )

        raise RuntimeError(
            f"일부 지수 크롤링 실패: {error_message}"
        )

    log("[CRAWL] SCFI / BDI 통합 크롤링 완료")

# =============================================================================
# 8. Database Read / JSON Data Cleaning
# =============================================================================

def load_index_data(index_name):
    """Load SCFI or BDI records from index_raw."""
    empty_df = pd.DataFrame(
        columns=["DATE", "REGION", "VALUE", "INDEX_NAME"]
    )

    if not DB_PATH.exists():
        log(f"[ERROR] Database file not found: {DB_PATH}")
        return empty_df

    query = """
        SELECT
            dt AS DATE,
            region AS REGION,
            value AS VALUE,
            index_name AS INDEX_NAME
        FROM index_raw
        WHERE UPPER(index_name) = UPPER(?)
        ORDER BY dt ASC, region ASC
    """

    try:
        with sqlite3.connect(DB_PATH) as connection:
            df = pd.read_sql_query(
                query,
                connection,
                params=[index_name]
            )

        log(
            f"[DB] {index_name} loaded from index_raw: "
            f"{len(df):,} rows"
        )

        return df

    except Exception as error:
        log(f"[ERROR] Failed to load {index_name}: {error}")
        return empty_df

def clean_data(df, index_name):
    """Clean data for JSON serialization and dashboard display."""
    index_name = normalize_index_name(index_name)

    empty_df = pd.DataFrame(
        columns=["DATE", "REGION", "VALUE", "INDEX_NAME"]
    )

    if df is None or df.empty:
        return empty_df

    cleaned = df.copy()

    cleaned["DATE"] = pd.to_datetime(
        cleaned["DATE"],
        errors="coerce"
    )

    cleaned["REGION"] = (
        cleaned["REGION"]
        .fillna("Unknown")
        .astype(str)
        .str.strip()
    )

    cleaned["VALUE"] = pd.to_numeric(
        cleaned["VALUE"],
        errors="coerce"
    )

    cleaned["INDEX_NAME"] = index_name

    cleaned = cleaned.dropna(
        subset=["DATE", "VALUE"]
    )

    cleaned = cleaned[
        cleaned["REGION"].apply(
            lambda region: is_valid_region_for_index(
                region,
                index_name
            )
        )
    ]

    if cleaned.empty:
        return empty_df

    cleaned = cleaned.drop_duplicates(
        subset=["DATE", "REGION"],
        keep="last"
    )

    cleaned = cleaned.sort_values(
        by=["DATE", "REGION"]
    ).reset_index(drop=True)

    cleaned["DATE"] = cleaned["DATE"].dt.strftime("%Y-%m-%d")
    cleaned["VALUE"] = cleaned["VALUE"].astype(float)

    return cleaned[
        ["DATE", "REGION", "VALUE", "INDEX_NAME"]
    ]

def load_and_clean_data(index_name):
    """Load raw DB data and return cleaned data."""
    raw_df = load_index_data(index_name)

    return clean_data(raw_df, index_name)

# =============================================================================
# 9. JSON Generator
# =============================================================================

def build_json():
    """Generate shipping_index_data.json."""
    log("[JSON] shipping_index_data.json 생성 중...")
    log(f"[INFO] DB_PATH   : {DB_PATH}")
    log(f"[INFO] JSON_PATH : {JSON_PATH}")
    log(f"[INFO] HTML_PATH : {HTML_PATH}")
    log(f"[INFO] DB Exists : {DB_PATH.exists()}")

    generated_at = now_string()
    indexes_dict = {}

    for index_name in SUPPORTED_INDEXES:
        df = load_and_clean_data(index_name)

        records = (
            df.to_dict(orient="records")
            if not df.empty
            else []
        )

        regions = (
            sorted(df["REGION"].dropna().unique().tolist())
            if not df.empty
            else []
        )

        latest_date = (
            df["DATE"].max()
            if not df.empty
            else None
        )

        indexes_dict[index_name] = {
            "regions": regions,
            "records": records,
            "record_count": len(records),
            "latest_date": latest_date,
            "last_sync": generated_at
        }

        log(
            f"[JSON] {index_name}: "
            f"records={len(records):,}, "
            f"regions={len(regions):,}, "
            f"latest_date={latest_date or '-'}"
        )

    payload = {
        "status": "success",
        "generated_at": generated_at,
        "source_database": DB_PATH.name,
        "indexes": indexes_dict
    }

    json_content = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2
    )

    atomic_write_text(JSON_PATH, json_content)

    log(f"[JSON] 저장 완료: {JSON_PATH}")

# =============================================================================
# 10. GitHub Pages Static HTML Generator
# =============================================================================

def generate_html_file():
    """Generate Bloomberg-style GitHub Pages index.html."""
    log("[HTML] Bloomberg-style index.html 생성 중...")

    html_content = r"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="description" content="SCFI 및 BDI 해운 지수 대시보드">

  <title>MI Terminal | Shipping Index</title>

  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>

  <style>
    :root {
      --bg: #070b12;
      --panel: #0e1621;
      --panel-2: #131e2b;
      --line: #263648;
      --line-soft: #1b2938;
      --text: #edf3f8;
      --muted: #8ea2b7;
      --orange: #ff8a00;
      --orange-soft: #ffb14a;
      --blue: #36a3ff;
      --green: #2ec27e;
      --red: #ff5c5c;
      --white: #ffffff;
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      min-width: 320px;
      background: var(--bg);
      color: var(--text);
      font-family: "Roboto Mono", "SFMono-Regular", Consolas,
        "Noto Sans KR", monospace;
      font-size: 13px;
    }

    button, input {
      font: inherit;
    }

    .topbar {
      position: sticky;
      top: 0;
      z-index: 100;
      display: flex;
      align-items: center;
      justify-content: space-between;
      min-height: 54px;
      padding: 0 22px;
      border-bottom: 1px solid var(--line);
      background: #090f18;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
      white-space: nowrap;
    }

    .brand-mark {
      color: var(--orange);
      font-size: 19px;
      font-weight: 900;
      letter-spacing: -1px;
    }

    .brand-title {
      color: var(--white);
      font-size: 14px;
      font-weight: 700;
      letter-spacing: 0.6px;
    }

    .brand-divider {
      width: 1px;
      height: 19px;
      background: var(--line);
    }

    .brand-subtitle {
      color: var(--muted);
      font-size: 11px;
      letter-spacing: 0.4px;
    }

    .top-status {
      color: var(--muted);
      font-size: 11px;
    }

    .terminal {
      max-width: 1600px;
      margin: 0 auto;
      padding: 18px 22px 36px;
    }

    .status-strip {
      display: flex;
      justify-content: space-between;
      gap: 15px;
      margin-bottom: 16px;
      padding: 10px 12px;
      color: var(--muted);
      border: 1px solid var(--line);
      background: var(--panel);
      font-size: 11px;
    }

    .status-left {
      display: flex;
      gap: 9px;
      align-items: center;
    }

    .status-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--green);
      box-shadow: 0 0 8px rgba(46, 194, 126, 0.65);
    }

    .status-dot.error {
      background: var(--red);
      box-shadow: 0 0 8px rgba(255, 92, 92, 0.65);
    }

    .workspace {
      display: grid;
      grid-template-columns: minmax(250px, 300px) minmax(0, 1fr);
      gap: 16px;
    }

    .panel {
      border: 1px solid var(--line);
      background: var(--panel);
    }

    .panel-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      min-height: 40px;
      padding: 0 13px;
      border-bottom: 1px solid var(--line);
      background: var(--panel-2);
    }

    .panel-title {
      color: var(--orange);
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.8px;
    }

    .filter-panel {
      height: fit-content;
    }

    .filter-section {
      padding: 14px;
      border-bottom: 1px solid var(--line-soft);
    }

    .filter-section:last-child {
      border-bottom: 0;
    }

    .label {
      display: block;
      margin-bottom: 8px;
      color: var(--muted);
      font-size: 10px;
      font-weight: 700;
      letter-spacing: 0.7px;
    }

    .index-switch {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 6px;
    }

    .terminal-btn {
      min-height: 32px;
      padding: 7px 10px;
      border: 1px solid var(--line);
      border-radius: 0;
      color: var(--muted);
      background: #0a111a;
      cursor: pointer;
      font-size: 11px;
      font-weight: 700;
      transition: 0.15s ease;
    }

    .terminal-btn:hover {
      border-color: var(--orange);
      color: var(--orange-soft);
    }

    .terminal-btn.active {
      border-color: var(--orange);
      color: #111;
      background: var(--orange);
    }

    .period-grid {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 5px;
    }

    .period-grid .terminal-btn:last-child {
      grid-column: span 3;
    }

    .search-input {
      width: 100%;
      padding: 9px 10px;
      border: 1px solid var(--line);
      outline: 0;
      color: var(--text);
      background: #080e16;
      font-size: 12px;
    }

    .search-input:focus {
      border-color: var(--orange);
    }

    .selection-actions {
      display: flex;
      gap: 6px;
      margin: 9px 0;
    }

    .small-action {
      padding: 0;
      border: 0;
      color: var(--blue);
      background: transparent;
      cursor: pointer;
      font-size: 10px;
    }

    .small-action:hover {
      color: var(--orange-soft);
    }

    .selected-summary {
      min-height: 32px;
      margin-bottom: 9px;
      padding: 6px;
      border: 1px solid var(--line-soft);
      background: #09111a;
    }

    .chip {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      margin: 2px;
      padding: 3px 5px;
      border: 1px solid #66502d;
      color: var(--orange-soft);
      background: #231b10;
      font-size: 10px;
    }

    .chip button {
      padding: 0;
      border: 0;
      color: var(--orange-soft);
      background: transparent;
      cursor: pointer;
      font-size: 12px;
      line-height: 1;
    }

    .route-list {
      max-height: 250px;
      overflow-y: auto;
      border: 1px solid var(--line-soft);
      background: #080e16;
    }

    .route-item {
      display: flex;
      gap: 8px;
      align-items: center;
      padding: 8px 9px;
      border-bottom: 1px solid #14202c;
      color: #c6d3df;
      cursor: pointer;
      font-size: 11px;
    }

    .route-item:last-child {
      border-bottom: 0;
    }

    .route-item:hover {
      background: #152234;
    }

    .route-item input {
      accent-color: var(--orange);
    }

    .main-area {
      min-width: 0;
    }

    .main-titlebar {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      margin-bottom: 12px;
    }

    .instrument-title {
      margin: 0;
      color: var(--white);
      font-size: 20px;
      font-weight: 700;
      letter-spacing: -0.4px;
    }

    .instrument-meta {
      margin-top: 5px;
      color: var(--muted);
      font-size: 11px;
    }

    .download-btn {
      border-color: #34526e;
      color: var(--blue);
      white-space: nowrap;
    }

    .kpi-grid {
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 9px;
      margin-bottom: 12px;
    }

    .kpi {
      min-height: 104px;
      padding: 13px;
      border: 1px solid var(--line);
      background: var(--panel);
    }

    .kpi-label {
      display: block;
      min-height: 25px;
      color: var(--muted);
      font-size: 10px;
      letter-spacing: 0.4px;
      line-height: 1.3;
    }

    .kpi-value {
      display: block;
      margin-top: 10px;
      color: var(--white);
      font-size: 20px;
      font-weight: 700;
      letter-spacing: -0.5px;
    }

    .kpi-sub {
      display: block;
      margin-top: 6px;
      color: var(--muted);
      font-size: 10px;
    }

    .positive { color: var(--green); }
    .negative { color: var(--red); }
    .neutral { color: var(--white); }

    .chart-panel {
      margin-bottom: 12px;
    }

    #chart {
      width: 100%;
      min-height: 520px;
    }

    .table-wrap {
      max-height: 470px;
      overflow: auto;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 11px;
    }

    th, td {
      padding: 10px 12px;
      border-bottom: 1px solid var(--line-soft);
      white-space: nowrap;
      text-align: right;
    }

    th {
      position: sticky;
      top: 0;
      z-index: 2;
      color: var(--orange-soft);
      background: #111c29;
      font-size: 10px;
      letter-spacing: 0.4px;
    }

    th:first-child, td:first-child {
      text-align: left;
    }

    td {
      color: #d5e0e9;
    }

    tr:hover td {
      background: #132031;
    }

    .empty-message {
      padding: 28px;
      color: var(--muted);
      text-align: center;
    }

    @media (max-width: 1100px) {
      .workspace { grid-template-columns: 1fr; }
      .kpi-grid { grid-template-columns: repeat(3, 1fr); }
    }

    @media (max-width: 680px) {
      .top-status, .brand-subtitle, .brand-divider { display: none; }
      .topbar { padding: 0 14px; }
      .terminal { padding: 12px; }
      .status-strip { display: block; line-height: 1.8; }
      .kpi-grid { grid-template-columns: repeat(2, 1fr); }
      .main-titlebar { align-items: flex-start; }
      .instrument-title { font-size: 17px; }
    }
  </style>
</head>

<body>
  <header class="topbar">
    <div class="brand">
      <span class="brand-mark">MI&lt;GO&gt;</span>
      <span class="brand-divider"></span>
      <span class="brand-title">SHIPPING INDEX</span>
      <span class="brand-subtitle">SCFI / BDI MONITOR</span>
    </div>
    <div id="lastUpdated" class="top-status">DATA LOADING...</div>
  </header>

  <main class="terminal">
    <section class="status-strip">
      <div class="status-left">
        <span id="statusDot" class="status-dot"></span>
        <strong id="statusTitle">SYSTEM / DATA LOADING</strong>
      </div>
      <span id="periodDisplay">PERIOD: -</span>
    </section>

    <div class="workspace">
      <aside class="panel filter-panel">
        <div class="panel-header">
          <span class="panel-title">FUNCTIONS / FILTERS</span>
        </div>

        <section class="filter-section">
          <span class="label">INDEX</span>
          <div class="index-switch">
            <button id="scfiTab" class="terminal-btn active" onclick="selectIndex('SCFI')">SCFI</button>
            <button id="bdiTab" class="terminal-btn" onclick="selectIndex('BDI')">BDI</button>
          </div>
        </section>

        <section class="filter-section">
          <span class="label">TIME RANGE</span>
          <div class="period-grid">
            <button class="terminal-btn" data-period="1Y" onclick="setPreset('1Y')">1Y</button>
            <button class="terminal-btn" data-period="3Y" onclick="setPreset('3Y')">3Y</button>
            <button class="terminal-btn" data-period="5Y" onclick="setPreset('5Y')">5Y</button>
            <button class="terminal-btn" data-period="YTD" onclick="setPreset('YTD')">YTD</button>
            <button class="terminal-btn" data-period="ALL" onclick="setPreset('ALL')">ALL HISTORY</button>
          </div>
        </section>

        <section class="filter-section">
          <span class="label">SELECT SERIES</span>

          <input
            id="routeSearch"
            class="search-input"
            type="search"
            placeholder="항로/지수 검색..."
            oninput="renderRouteList()"
          >

          <div class="selection-actions">
            <button class="small-action" onclick="selectVisibleRoutes()">VISIBLE ALL</button>
            <button class="small-action" onclick="clearRoutes()">CLEAR</button>
          </div>

          <div id="selectedSummary" class="selected-summary"></div>
          <div id="routeList" class="route-list"></div>
        </section>
      </aside>

      <section class="main-area">
        <div class="main-titlebar">
          <div>
            <h1 id="dashboardTitle" class="instrument-title">SCFI</h1>
            <div id="dashboardDescription" class="instrument-meta">
              Shanghai Containerized Freight Index
            </div>
          </div>
          <button class="terminal-btn download-btn" onclick="downloadCSV()">EXPORT CSV</button>
        </div>

        <section class="kpi-grid">
          <div class="kpi">
            <span class="kpi-label">LATEST<br>SELECTED SERIES AVG</span>
            <span id="latestValue" class="kpi-value">-</span>
            <span id="latestValueDate" class="kpi-sub">-</span>
          </div>

          <div class="kpi">
            <span class="kpi-label">PERIOD<br>AVERAGE</span>
            <span id="periodAverage" class="kpi-value">-</span>
            <span id="periodAverageSub" class="kpi-sub">-</span>
          </div>

          <div class="kpi">
            <span class="kpi-label">PERIOD<br>HIGH / LOW</span>
            <span id="highLowValue" class="kpi-value">-</span>
            <span id="highLowSub" class="kpi-sub">-</span>
          </div>

          <div class="kpi">
            <span class="kpi-label">CHANGE<br>FIRST → LAST</span>
            <span id="changeValue" class="kpi-value neutral">-</span>
            <span id="changeRate" class="kpi-sub">-</span>
          </div>

          <div class="kpi">
            <span class="kpi-label">OBSERVATIONS<br> / SERIES</span>
            <span id="recordCount" class="kpi-value">-</span>
            <span id="regionCount" class="kpi-sub">-</span>
          </div>
        </section>

        <section class="panel chart-panel">
          <div class="panel-header">
            <span class="panel-title">PRICE HISTORY / RANGE SLIDER ENABLED</span>
            <span class="panel-title">DRAG BELOW CHART TO CHANGE PERIOD</span>
          </div>
          <div id="chart"></div>
        </section>

        <section class="panel">
          <div class="panel-header">
            <span class="panel-title">DATA GRID / SELECTED PERIOD</span>
          </div>
          <div class="table-wrap">
            <table>
              <thead id="dataTableHead"></thead>
              <tbody id="dataTableBody">
                <tr><td class="empty-message">DATA LOADING...</td></tr>
              </tbody>
            </table>
          </div>
        </section>
      </section>
    </div>
  </main>

  <script>
    let dashboardData = null;
    let currentIndex = "SCFI";
    let selectedRegions = new Set();
    let currentRange = null; // { start: "YYYY-MM-DD", end: "YYYY-MM-DD" } 또는 null(전체)
    let currentPreset = "ALL";

    const INDEX_INFO = {
      SCFI: {
        title: "SCFI / SHANGHAI CONTAINERIZED FREIGHT INDEX",
        description: "컨테이너 운임 항로별 시계열 및 선택 항로 평균 모니터링"
      },
      BDI: {
        title: "BDI / BALTIC DRY INDEX",
        description: "벌크선 시황 대표지수 및 세부 선형별 시계열 모니터링"
      }
    };

    const SERIES_COLORS = [
      "#ff8a00", "#36a3ff", "#2ec27e", "#be8cff",
      "#ff5c5c", "#f5d547", "#5bd6c7", "#d7e2eb"
    ];

    function formatNumber(value, decimals = 2) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
      return Number(value).toLocaleString("ko-KR", {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals
      });
    }

    function formatInteger(value) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
      return Number(value).toLocaleString("ko-KR");
    }

    function formatDate(value) {
      if (!value) return "-";
      const date = new Date(value + "T00:00:00");
      if (Number.isNaN(date.getTime())) return value;
      return date.toLocaleDateString("ko-KR", {
        year: "numeric", month: "2-digit", day: "2-digit"
      });
    }

    function escapeHtml(value) {
      return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }

    function setStatus(ok, title, message) {
      document.getElementById("statusDot").className = ok ? "status-dot" : "status-dot error";
      document.getElementById("statusTitle").textContent = title;
      document.getElementById("lastUpdated").textContent = message;
    }

    function getIndexData() {
      if (!dashboardData || !dashboardData.indexes) {
        return { regions: [], records: [] };
      }
      return dashboardData.indexes[currentIndex] || { regions: [], records: [] };
    }

    function getAllRecords() {
      return [...getIndexData().records].sort(
        (a, b) => String(a.DATE).localeCompare(String(b.DATE))
      );
    }

    function getDefaultRegions(regions) {
      if (currentIndex === "BDI" && regions.includes("BDI")) return ["BDI"];
      return regions.slice(0, Math.min(3, regions.length));
    }

    function renderRouteList() {
      const indexData = getIndexData();
      const keyword = document.getElementById("routeSearch").value.trim().toLowerCase();
      const visibleRegions = indexData.regions.filter(region =>
        region.toLowerCase().includes(keyword)
      );

      document.getElementById("routeList").innerHTML = visibleRegions.length
        ? visibleRegions.map(region => `
            <label class="route-item">
              <input
                type="checkbox"
                value="${escapeHtml(region)}"
                ${selectedRegions.has(region) ? "checked" : ""}
                onchange="toggleRegion(this.value, this.checked)"
              >
              <span>${escapeHtml(region)}</span>
            </label>
          `).join("")
        : `<div class="empty-message">NO MATCHING SERIES</div>`;

      renderSelectedSummary();
    }

    function renderSelectedSummary() {
      const container = document.getElementById("selectedSummary");

      if (!selectedRegions.size) {
        container.innerHTML = `<span style="color:#8ea2b7;font-size:10px;">NO SERIES SELECTED</span>`;
        return;
      }

      container.innerHTML = [...selectedRegions].map(region => `
        <span class="chip">
          ${escapeHtml(region)}
          <button onclick="removeRegion('${String(region).replaceAll("'", "\\'")}')">×</button>
        </span>
      `).join("");
    }

    function toggleRegion(region, checked) {
      if (checked) selectedRegions.add(region);
      else selectedRegions.delete(region);

      renderRouteList();
      renderDashboard();
    }

    function removeRegion(region) {
      selectedRegions.delete(region);
      renderRouteList();
      renderDashboard();
    }

    function selectVisibleRoutes() {
      const keyword = document.getElementById("routeSearch").value.trim().toLowerCase();
      getIndexData().regions
        .filter(region => region.toLowerCase().includes(keyword))
        .forEach(region => selectedRegions.add(region));

      renderRouteList();
      renderDashboard();
    }

    function clearRoutes() {
      selectedRegions.clear();
      renderRouteList();
      renderDashboard();
    }

    function getDateBounds() {
      const records = getAllRecords();
      if (!records.length) return { min: null, max: null };
      return { min: records[0].DATE, max: records[records.length - 1].DATE };
    }

    function addYears(dateString, years) {
      const date = new Date(dateString + "T00:00:00");
      date.setFullYear(date.getFullYear() - years);
      return date.toISOString().slice(0, 10);
    }

    function setPreset(preset) {
      const bounds = getDateBounds();
      if (!bounds.max) return;

      currentPreset = preset;

      if (preset === "ALL") {
        currentRange = null;
      } else if (preset === "YTD") {
        currentRange = {
          start: `${bounds.max.slice(0, 4)}-01-01`,
          end: bounds.max
        };
      } else {
        const years = Number(preset.replace("Y", ""));
        currentRange = {
          start: addYears(bounds.max, years),
          end: bounds.max
        };
      }

      updatePeriodButtons();
      renderDashboard();
    }

    function updatePeriodButtons() {
      document.querySelectorAll("[data-period]").forEach(button => {
        button.classList.toggle(
          "active",
          button.getAttribute("data-period") === currentPreset
        );
      });
    }

    function getFilteredRecords() {
      let records = getAllRecords();

      if (!selectedRegions.size) return [];

      records = records.filter(record => selectedRegions.has(record.REGION));

      if (currentRange) {
        records = records.filter(record =>
          record.DATE >= currentRange.start && record.DATE <= currentRange.end
        );
      }

      return records;
    }

    function getDailyAverages(records) {
      const byDate = {};

      records.forEach(record => {
        if (!byDate[record.DATE]) byDate[record.DATE] = [];
        byDate[record.DATE].push(Number(record.VALUE));
      });

      return Object.keys(byDate).sort().map(date => ({
        date,
        value: byDate[date].reduce((sum, value) => sum + value, 0) / byDate[date].length
      }));
    }

    function updateKpis(records) {
      const selectedCount = selectedRegions.size;
      document.getElementById("recordCount").textContent = formatInteger(records.length);
      document.getElementById("regionCount").textContent = `${formatInteger(selectedCount)} SERIES SELECTED`;

      if (!records.length) {
        ["latestValue", "periodAverage", "highLowValue", "changeValue"].forEach(id => {
          document.getElementById(id).textContent = "-";
        });
        document.getElementById("latestValueDate").textContent = "NO DATA";
        document.getElementById("periodAverageSub").textContent = "-";
        document.getElementById("highLowSub").textContent = "-";
        document.getElementById("changeRate").textContent = "-";
        return;
      }

      const daily = getDailyAverages(records);
      const latest = daily[daily.length - 1];
      const first = daily[0];

      // 기간 평균: 선택된 모든 관측값의 단순 산술평균
      const periodAverage = records.reduce(
        (sum, record) => sum + Number(record.VALUE), 0
      ) / records.length;

      const values = records.map(record => Number(record.VALUE));
      const high = Math.max(...values);
      const low = Math.min(...values);

      const change = latest.value - first.value;
      const changeRate = first.value !== 0 ? (change / first.value) * 100 : 0;
      const changeClass = change > 0 ? "positive" : change < 0 ? "negative" : "neutral";

      document.getElementById("latestValue").textContent = formatNumber(latest.value);
      document.getElementById("latestValueDate").textContent = `${formatDate(latest.date)} AVG`;

      document.getElementById("periodAverage").textContent = formatNumber(periodAverage);
      document.getElementById("periodAverageSub").textContent = "SELECTED PERIOD / SIMPLE AVG";

      document.getElementById("highLowValue").textContent =
        `${formatNumber(high, 0)} / ${formatNumber(low, 0)}`;
      document.getElementById("highLowSub").textContent = "HIGH / LOW";

      const sign = change > 0 ? "+" : "";
      const changeElement = document.getElementById("changeValue");
      changeElement.textContent = `${sign}${formatNumber(change)}`;
      changeElement.className = `kpi-value ${changeClass}`;
      document.getElementById("changeRate").textContent =
        `${sign}${formatNumber(changeRate)}% / ${formatDate(first.date)} → ${formatDate(latest.date)}`;
    }

    function renderChart(records) {
      const chart = document.getElementById("chart");
      const allRecords = getAllRecords();
      const bounds = getDateBounds();

      if (!records.length) {
        Plotly.purge(chart);
        chart.innerHTML = `<div class="empty-message">SELECT AT LEAST ONE SERIES TO DISPLAY DATA</div>`;
        return;
      }

      const grouped = {};
      records.forEach(record => {
        if (!grouped[record.REGION]) grouped[record.REGION] = [];
        grouped[record.REGION].push(record);
      });

      const traces = Object.entries(grouped).map(([region, rows], index) => ({
        x: rows.map(row => row.DATE),
        y: rows.map(row => Number(row.VALUE)),
        type: "scatter",
        mode: "lines",
        name: region,
        line: {
          color: SERIES_COLORS[index % SERIES_COLORS.length],
          width: region === "BDI" ? 2.8 : 1.8
        },
        hovertemplate:
          `<b>${escapeHtml(region)}</b>` +
          "<br>DATE: %{x}<br>VALUE: %{y:,.2f}<extra></extra>"
      }));

      const xRange = currentRange
        ? [currentRange.start, currentRange.end]
        : [bounds.min, bounds.max];

      const layout = {
        margin: { l: 72, r: 26, t: 20, b: 66 },
        paper_bgcolor: "#0e1621",
        plot_bgcolor: "#0e1621",
        font: {
          family: '"Roboto Mono", Consolas, monospace',
          color: "#c9d6e2",
          size: 11
        },
        hovermode: "x unified",
        hoverlabel: {
          bgcolor: "#111c29",
          bordercolor: "#ff8a00",
          font: { color: "#edf3f8" }
        },
        legend: {
          orientation: "h",
          y: 1.12,
          x: 0,
          font: { size: 10, color: "#c9d6e2" }
        },
        xaxis: {
          type: "date",
          range: xRange,
          gridcolor: "#1f3040",
          linecolor: "#405568",
          tickfont: { color: "#8ea2b7" },
          rangeslider: {
            visible: true,
            bgcolor: "#09111a",
            bordercolor: "#405568",
            thickness: 0.09
          }
        },
        yaxis: {
          title: { text: "INDEX VALUE", font: { size: 10, color: "#8ea2b7" } },
          gridcolor: "#1f3040",
          zerolinecolor: "#2c4052",
          linecolor: "#405568",
          tickfont: { color: "#8ea2b7" }
        }
      };

      Plotly.newPlot(chart, traces, layout, {
        responsive: true,
        displaylogo: false,
        modeBarButtonsToRemove: ["select2d", "lasso2d"]
      });

      chart.removeAllListeners?.("plotly_relayout");
      chart.on("plotly_relayout", eventData => {
        const start = eventData["xaxis.range[0]"];
        const end = eventData["xaxis.range[1]"];

        if (start && end) {
          currentRange = {
            start: String(start).slice(0, 10),
            end: String(end).slice(0, 10)
          };
          currentPreset = "CUSTOM";
          updatePeriodButtons();
          renderDashboard(false);
        }

        if (eventData["xaxis.autorange"]) {
          currentRange = null;
          currentPreset = "ALL";
          updatePeriodButtons();
          renderDashboard(false);
        }
      });
    }

    function renderTable(records) {
      const tableHead = document.getElementById("dataTableHead");
      const tableBody = document.getElementById("dataTableBody");
      const regions = [...selectedRegions];

      if (!records.length || !regions.length) {
        tableHead.innerHTML = "<tr><th>DATE</th></tr>";
        tableBody.innerHTML = `<tr><td class="empty-message">NO DATA FOR SELECTED CONDITIONS</td></tr>`;
        return;
      }

      const pivot = {};
      records.forEach(record => {
        if (!pivot[record.DATE]) pivot[record.DATE] = {};
        pivot[record.DATE][record.REGION] = record.VALUE;
      });

      tableHead.innerHTML = `
        <tr>
          <th>DATE</th>
          ${regions.map(region => `<th>${escapeHtml(region)}</th>`).join("")}
        </tr>
      `;

      tableBody.innerHTML = Object.keys(pivot).sort().reverse().map(date => `
        <tr>
          <td>${escapeHtml(formatDate(date))}</td>
          ${regions.map(region => `
            <td>${pivot[date][region] !== undefined ? formatNumber(pivot[date][region]) : "-"}</td>
          `).join("")}
        </tr>
      `).join("");
    }

    function updatePeriodDisplay() {
      const bounds = getDateBounds();
      let text = "PERIOD: -";

      if (bounds.min && bounds.max) {
        text = currentRange
          ? `PERIOD: ${currentRange.start} → ${currentRange.end}`
          : `PERIOD: ${bounds.min} → ${bounds.max} / ALL HISTORY`;
      }

      document.getElementById("periodDisplay").textContent = text;
    }

    function renderDashboard(renderChartToo = true) {
      const records = getFilteredRecords();
      updatePeriodDisplay();
      updateKpis(records);

      if (renderChartToo) renderChart(records);
      renderTable(records);
    }

    function selectIndex(indexName) {
      currentIndex = indexName;
      currentRange = null;
      currentPreset = "ALL";

      const indexData = getIndexData();
      selectedRegions = new Set(getDefaultRegions(indexData.regions));

      document.getElementById("scfiTab").classList.toggle("active", indexName === "SCFI");
      document.getElementById("bdiTab").classList.toggle("active", indexName === "BDI");
      document.getElementById("dashboardTitle").textContent = INDEX_INFO[indexName].title;
      document.getElementById("dashboardDescription").textContent = INDEX_INFO[indexName].description;
      document.getElementById("routeSearch").value = "";

      updatePeriodButtons();
      renderRouteList();
      renderDashboard();
    }

    function downloadCSV() {
      const records = getFilteredRecords();
      const regions = [...selectedRegions];

      if (!records.length || !regions.length) {
        alert("다운로드할 데이터가 없습니다.");
        return;
      }

      const pivot = {};
      records.forEach(record => {
        if (!pivot[record.DATE]) pivot[record.DATE] = {};
        pivot[record.DATE][record.REGION] = record.VALUE;
      });

      const rows = [
        ["DATE", ...regions].join(","),
        ...Object.keys(pivot).sort().reverse().map(date => [
          date,
          ...regions.map(region => pivot[date][region] ?? "")
        ].join(","))
      ];

      const blob = new Blob(
        ["\uFEFF" + rows.join("\n")],
        { type: "text/csv;charset=utf-8;" }
      );

      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${currentIndex}_shipping_index_${currentPreset}.csv`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
    }

    async function loadDashboardData() {
      try {
        const response = await fetch("shipping_index_data.json?ts=" + Date.now(), {
          cache: "no-store"
        });

        if (!response.ok) {
          throw new Error(`HTTP ${response.status}`);
        }

        dashboardData = await response.json();

        setStatus(
          true,
          "SYSTEM / DATA ONLINE",
          `LAST REFRESH: ${dashboardData.generated_at || "-"}`
        );

        selectIndex("SCFI");

      } catch (error) {
        console.error(error);
        setStatus(
          false,
          "SYSTEM / DATA ERROR",
          "shipping_index_data.json LOAD FAILED"
        );
      }
    }

    document.addEventListener("DOMContentLoaded", loadDashboardData);
  </script>
</body>
</html>
"""

    atomic_write_text(HTML_PATH, html_content)
    log(f"[HTML] 저장 완료: {HTML_PATH}")

    
# =============================================================================
# 11. Main Execution
# =============================================================================

def main():
    """
    Run complete workflow:
    1. SCFI / BDI crawl
    2. SQLite upsert
    3. JSON generation
    4. Static HTML generation
    """
    log("=" * 72)
    log("[CRON] Shipping Index 크롤링 및 정적 대시보드 갱신 시작")
    log(f"[INFO] Script directory: {SCRIPT_DIR}")
    log(f"[INFO] DB path: {DB_PATH}")
    log("=" * 72)

    try:
        get_db_connection().close()

        cleanup_invalid_regions()

        # 1. KSG에서 SCFI / BDI 수집 및 DB 저장
        crawl_all_indexes()

        # 2. SQLite index_raw → shipping_index_data.json
        build_json()

        # 3. GitHub Pages용 index.html 생성
        generate_html_file()

        log("=" * 72)
        log("[CRON] Shipping Index 크롤링 및 정적 대시보드 갱신 완료")
        log("=" * 72)

    except Exception as error:
        log("=" * 72)
        log(f"[CRITICAL] Shipping Index 처리 실패: {error}")
        log("=" * 72)
        raise

if __name__ == "__main__":
    main()
