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
    """Generate GitHub Pages index.html."""
    log("[HTML] index.html 생성 중...")

    html_content = r"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
  >
  <meta
    name="description"
    content="SCFI 및 BDI 해운 운임·시황 지수 정적 대시보드"
  >

  <title>Shipping Index Dashboard | SCFI & BDI</title>

  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>

  <style>
    :root {
      --navy: #102a43;
      --blue: #1f5fa8;
      --light-blue: #eef5fb;
      --border: #d9e2ec;
      --text: #243b53;
      --muted: #627d98;
      --bg: #f5f7fa;
      --white: #ffffff;
      --danger: #b42318;
      --success: #027a48;
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Arial, "Noto Sans KR", "Malgun Gothic", sans-serif;
    }

    header {
      padding: 30px 20px;
      color: var(--white);
      background: linear-gradient(135deg, #102a43, #1f5fa8);
    }

    header .inner,
    main {
      width: 100%;
      max-width: 1280px;
      margin: 0 auto;
    }

    .header-top {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 15px;
    }

    h1 {
      margin: 0;
      font-size: 32px;
      letter-spacing: -0.5px;
    }

    header p {
      margin: 9px 0 0;
      color: #d9eafd;
      font-size: 15px;
    }

    .btn-download {
      background-color: var(--white);
      color: var(--blue);
      border: 1px solid var(--white);
      padding: 10px 18px;
      border-radius: 8px;
      font-weight: 700;
      cursor: pointer;
      font-size: 14px;
      transition: all 0.2s;
      box-shadow: 0 2px 6px rgba(0,0,0,0.1);
    }

    .btn-download:hover {
      background-color: var(--light-blue);
      border-color: var(--light-blue);
    }

    main {
      padding: 22px 16px 40px;
    }

    .panel,
    .status,
    .kpi {
      border: 1px solid var(--border);
      border-radius: 10px;
      background: var(--white);
      box-shadow: 0 3px 12px rgba(16, 42, 67, 0.06);
    }

    .status {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
      margin-bottom: 16px;
      padding: 13px 16px;
      font-size: 13px;
    }

    .status-left {
      display: flex;
      align-items: center;
      gap: 8px;
    }

    .status-dot {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: var(--success);
    }

    .status-dot.error {
      background: var(--danger);
    }

    .status-text {
      color: var(--muted);
    }

    .panel {
      margin-bottom: 18px;
      padding: 20px;
    }

    h2 {
      margin: 0 0 16px;
      color: var(--navy);
      font-size: 20px;
    }

    .section-description {
      margin: -6px 0 18px;
      color: var(--muted);
      font-size: 14px;
      line-height: 1.6;
    }

    .index-tabs {
      display: flex;
      gap: 10px;
      margin-bottom: 18px;
    }

    .tab-button {
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px 18px;
      color: var(--text);
      background: var(--white);
      cursor: pointer;
      font-size: 14px;
      font-weight: 700;
    }

    .tab-button:hover {
      border-color: var(--blue);
      color: var(--blue);
    }

    .tab-button.active {
      border-color: var(--blue);
      color: var(--white);
      background: var(--blue);
    }

    .kpi-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
      margin-bottom: 20px;
    }

    .kpi {
      min-height: 118px;
      padding: 16px;
    }

    .kpi-label {
      display: block;
      margin-bottom: 10px;
      color: var(--muted);
      font-size: 13px;
    }

    .kpi-value {
      display: block;
      color: var(--navy);
      font-size: 27px;
      font-weight: 700;
      letter-spacing: -0.5px;
    }

    .kpi-sub {
      display: block;
      margin-top: 8px;
      color: var(--muted);
      font-size: 12px;
    }

    .positive {
      color: var(--success);
    }

    .negative {
      color: var(--danger);
    }

    .neutral {
      color: var(--muted);
    }

    .chart {
      width: 100%;
      min-height: 440px;
      margin-top: 20px;
    }

    .controls {
      display: flex;
      gap: 20px;
      flex-wrap: wrap;
      margin-bottom: 20px;
      background: #f8fafc;
      padding: 15px;
      border-radius: 8px;
      border: 1px solid var(--border);
    }

    .control-group {
      display: flex;
      flex-direction: column;
      gap: 7px;
    }

    .control-group label {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }

    select {
      min-width: 160px;
      border: 1px solid var(--border);
      border-radius: 7px;
      padding: 10px 12px;
      color: var(--text);
      background: var(--white);
      font-size: 14px;
    }

    .checkbox-container {
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      max-height: 100px;
      overflow-y: auto;
      padding: 5px;
      background: var(--white);
      border: 1px solid var(--border);
      border-radius: 7px;
      padding: 8px 12px;
    }

    .checkbox-label {
      display: flex;
      align-items: center;
      gap: 5px;
      font-size: 13px;
      cursor: pointer;
      user-select: none;
    }

    .table-wrap {
      overflow-x: auto;
      max-height: 500px;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }

    th,
    td {
      padding: 12px;
      border-bottom: 1px solid var(--border);
      text-align: left;
      white-space: nowrap;
    }

    th {
      color: var(--muted);
      background: #f8fafc;
      font-size: 12px;
      font-weight: 700;
      position: sticky;
      top: 0;
      z-index: 10;
    }

    td.numeric,
    th.numeric {
      text-align: right;
    }

    .empty-message {
      padding: 30px 18px;
      border: 1px dashed var(--border);
      border-radius: 8px;
      color: var(--muted);
      background: #f8fafc;
      text-align: center;
      line-height: 1.7;
    }

    footer {
      padding: 20px;
      color: var(--muted);
      font-size: 12px;
      text-align: center;
    }

    @media (max-width: 900px) {
      .kpi-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
    }

    @media (max-width: 560px) {
      header {
        padding: 24px 16px;
      }

      h1 {
        font-size: 25px;
      }

      .panel {
        padding: 16px;
      }

      .kpi-grid {
        grid-template-columns: 1fr;
      }

      .chart {
        min-height: 370px;
      }
    }
  </style>
</head>

<body>
  <header>
    <div class="inner">
      <div class="header-top">
        <div>
          <h1>Shipping Index Dashboard</h1>
          <p>Shanghai Containerized Freight Index (SCFI) · Baltic Dry Index (BDI)</p>
        </div>
        <button class="btn-download" type="button" onclick="downloadCSV()">📥 CSV 다운로드</button>
      </div>
    </div>
  </header>

  <main>
    <section class="status">
      <div class="status-left">
        <span id="statusDot" class="status-dot"></span>
        <strong id="statusTitle">데이터 로딩 중</strong>
      </div>

      <div id="lastUpdated" class="status-text">
        shipping_index_data.json 확인 중...
      </div>
    </section>

    <section class="panel">
      <div class="index-tabs">
        <button
          id="scfiTab"
          class="tab-button active"
          type="button"
          onclick="selectIndex('SCFI')"
        >
          SCFI
        </button>

        <button
          id="bdiTab"
          class="tab-button"
          type="button"
          onclick="selectIndex('BDI')"
        >
          BDI
        </button>
      </div>

      <h2 id="dashboardTitle">SCFI Dashboard</h2>

      <p id="dashboardDescription" class="section-description">
        데이터를 불러오는 중입니다.
      </p>

      <div class="controls">
        <div class="control-group">
          <label for="yearSelect">연도 선택 (기간)</label>
          <select id="yearSelect" onchange="renderDashboard()">
            <option value="">전체 연도</option>
          </select>
        </div>

        <div class="control-group" style="flex: 1; min-width: 280px;">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
            <label>항로 복수 선택</label>
            <div>
              <button type="button" onclick="toggleAllRegions(true)" style="font-size: 11px; cursor: pointer; border:1px solid var(--border); background:#fff; padding:2px 6px; border-radius:4px;">전체 선택</button>
              <button type="button" onclick="toggleAllRegions(false)" style="font-size: 11px; cursor: pointer; border:1px solid var(--border); background:#fff; padding:2px 6px; border-radius:4px;">해제</button>
            </div>
          </div>
          <div id="regionCheckboxes" class="checkbox-container">
            <!-- 체크박스 동적 생성 -->
          </div>
        </div>
      </div>

      <div class="kpi-grid">
        <div class="kpi">
          <span class="kpi-label">최신 지수 (평균/대표)</span>
          <span id="latestValue" class="kpi-value">-</span>
          <span id="latestValueDate" class="kpi-sub">-</span>
        </div>

        <div class="kpi">
          <span class="kpi-label">직전 값</span>
          <span id="previousValue" class="kpi-value">-</span>
          <span id="previousValueDate" class="kpi-sub">-</span>
        </div>

        <div class="kpi">
          <span class="kpi-label">기간 변화</span>
          <span id="changeValue" class="kpi-value neutral">-</span>
          <span id="changeRate" class="kpi-sub">-</span>
        </div>

        <div class="kpi">
          <span class="kpi-label">데이터 건수</span>
          <span id="recordCount" class="kpi-value">-</span>
          <span id="regionCount" class="kpi-sub">-</span>
        </div>
      </div>

      <div id="chart" class="chart"></div>
    </section>

    <section class="panel">
      <h2>날짜별 항로별 시세표</h2>

      <p class="section-description">
        세로축은 날짜, 가로축은 선택된 항로별 데이터로 나열됩니다.
      </p>

      <div class="table-wrap">
        <table>
          <thead id="dataTableHead">
            <tr>
              <th>일자</th>
            </tr>
          </thead>

          <tbody id="dataTableBody">
            <tr>
              <td class="empty-message">
                데이터 로딩 중...
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </main>

  <footer>
    Market Intelligence Dashboard · Data source: Korea Shipping Gazette
  </footer>

  <script>
    let dashboardData = null;
    let currentIndex = "SCFI";

    const INDEX_DESCRIPTIONS = {
      SCFI: "상하이발 컨테이너 운임 흐름을 지역 및 항로별로 확인합니다.",
      BDI: "벌크선 시황을 대표하는 Baltic Dry Index 및 세부 지수 흐름을 확인합니다."
    };

    const INDEX_COLORS = {
      SCFI: "#1f5fa8",
      BDI: "#027a48"
    };

    function formatNumber(value, decimals = 2) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) {
        return "-";
      }
      return Number(value).toLocaleString("ko-KR", {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals
      });
    }

    function formatInteger(value) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) {
        return "-";
      }
      return Number(value).toLocaleString("ko-KR");
    }

    function formatDate(dateString) {
      if (!dateString) return "-";
      const date = new Date(dateString + "T00:00:00");
      if (Number.isNaN(date.getTime())) return dateString;
      return date.toLocaleDateString("ko-KR", {
        year: "numeric",
        month: "2-digit",
        day: "2-digit"
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

    function setStatus(isSuccess, title, message) {
      const statusDot = document.getElementById("statusDot");
      const statusTitle = document.getElementById("statusTitle");
      const lastUpdated = document.getElementById("lastUpdated");

      statusDot.className = isSuccess ? "status-dot" : "status-dot error";
      statusTitle.textContent = title;
      lastUpdated.textContent = message;
    }

    function getCurrentIndexData() {
      if (!dashboardData || !dashboardData.indexes) {
        return { regions: [], records: [], record_count: 0, latest_date: null };
      }
      return dashboardData.indexes[currentIndex] || { regions: [], records: [], record_count: 0, latest_date: null };
    }

    function populateControls() {
      const indexData = getCurrentIndexData();
      
      // 연도 옵션 채우기
      const yearSelect = document.getElementById("yearSelect");
      const currentYearVal = yearSelect.value;
      const years = [...new Set(indexData.records.map(r => String(r.DATE).substring(0, 4)))].sort().reverse();

      yearSelect.innerHTML = '<option value="">전체 연도</option>';
      years.forEach(year => {
        const opt = document.createElement("option");
        opt.value = year;
        opt.textContent = year + "년";
        yearSelect.appendChild(opt);
      });
      if (years.includes(currentYearVal)) {
        yearSelect.value = currentYearVal;
      }

      // 항로 체크박스 채우기
      const container = document.getElementById("regionCheckboxes");
      // 기존에 체크되어 있던 항로 기억
      const checkedRegions = Array.from(container.querySelectorAll("input[type='checkbox']:checked")).map(cb => cb.value);
      
      container.innerHTML = "";
      indexData.regions.forEach((region, idx) => {
        const label = document.createElement("label");
        label.className = "checkbox-label";

        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.value = region;
        // 처음 로드시이거나 이전 선택 항목에 포함되어 있으면 체크
        if (checkedRegions.length === 0 || checkedRegions.includes(region)) {
          cb.checked = true;
        }
        cb.onchange = renderDashboard;

        label.appendChild(cb);
        label.appendChild(document.createTextNode(region));
        container.appendChild(label);
      });
    }

    function toggleAllRegions(checkStatus) {
      const checkboxes = document.querySelectorAll("#regionCheckboxes input[type='checkbox']");
      checkboxes.forEach(cb => cb.checked = checkStatus);
      renderDashboard();
    }

    function getSelectedRegions() {
      const checkboxes = document.querySelectorAll("#regionCheckboxes input[type='checkbox']:checked");
      return Array.from(checkboxes).map(cb => cb.value);
    }

    function getFilteredRecords() {
      const indexData = getCurrentIndexData();
      const selectedYear = document.getElementById("yearSelect").value;
      const selectedRegions = getSelectedRegions();

      let records = Array.isArray(indexData.records) ? [...indexData.records] : [];

      // 연도 필터
      if (selectedYear) {
        records = records.filter(r => String(r.DATE).startsWith(selectedYear));
      }

      // 항로 필터
      if (selectedRegions.length > 0) {
        records = records.filter(r => selectedRegions.includes(r.REGION));
      }

      records.sort((a, b) => String(a.DATE).localeCompare(String(b.DATE)));
      return records;
    }

    function updateKpis(records) {
      const latestValueElement = document.getElementById("latestValue");
      const latestValueDateElement = document.getElementById("latestValueDate");
      const previousValueElement = document.getElementById("previousValue");
      const previousValueDateElement = document.getElementById("previousValueDate");
      const changeValueElement = document.getElementById("changeValue");
      const changeRateElement = document.getElementById("changeRate");
      const recordCountElement = document.getElementById("recordCount");
      const regionCountElement = document.getElementById("regionCount");

      const selectedRegions = getSelectedRegions();
      recordCountElement.textContent = formatInteger(records.length);
      regionCountElement.textContent = "선택 항로 " + formatInteger(selectedRegions.length) + "개";

      if (records.length === 0) {
        latestValueElement.textContent = "-";
        latestValueDateElement.textContent = "데이터 없음";
        previousValueElement.textContent = "-";
        previousValueDateElement.textContent = "데이터 없음";
        changeValueElement.textContent = "-";
        changeRateElement.textContent = "-";
        changeValueElement.className = "kpi-value neutral";
        return;
      }

      // 날짜별로 그룹화하여 가장 최근 날짜의 평균값 계산 등을 수행
      const dateMap = {};
      records.forEach(r => {
        if (!dateMap[r.DATE]) dateMap[r.DATE] = [];
        dateMap[r.DATE].push(Number(r.VALUE));
      });

      const dates = Object.keys(dateMap).sort();
      const latestDate = dates[dates.length - 1];
      const previousDate = dates.length >= 2 ? dates[dates.length - 2] : null;

      const latestAvg = dateMap[latestDate].reduce((a, b) => a + b, 0) / dateMap[latestDate].length;
      latestValueElement.textContent = formatNumber(latestAvg);
      latestValueDateElement.textContent = formatDate(latestDate) + " (평균)";

      if (!previousDate) {
        previousValueElement.textContent = "-";
        previousValueDateElement.textContent = "비교 데이터 없음";
        changeValueElement.textContent = "-";
        changeRateElement.textContent = "-";
        changeValueElement.className = "kpi-value neutral";
        return;
      }

      const prevAvg = dateMap[previousDate].reduce((a, b) => a + b, 0) / dateMap[previousDate].length;
      previousValueElement.textContent = formatNumber(prevAvg);
      previousValueDateElement.textContent = formatDate(previousDate) + " (평균)";

      const diff = latestAvg - prevAvg;
      const rate = prevAvg !== 0 ? (diff / prevAvg) * 100 : 0;
      const sign = diff > 0 ? "+" : "";
      const styleClass = diff > 0 ? "positive" : diff < 0 ? "negative" : "neutral";

      changeValueElement.textContent = sign + formatNumber(diff);
      changeValueElement.className = "kpi-value " + styleClass;
      changeRateElement.textContent = sign + formatNumber(rate, 2) + "%";
    }

    function renderChart(records) {
      const chartElement = document.getElementById("chart");

      if (records.length === 0) {
        Plotly.purge(chartElement);
        chartElement.innerHTML = `
          <div class="empty-message">
            선택한 조건에 해당하는 데이터가 없습니다.
          </div>
        `;
        return;
      }

      const groupedRecords = {};
      records.forEach(record => {
        if (!groupedRecords[record.REGION]) {
          groupedRecords[record.REGION] = [];
        }
        groupedRecords[record.REGION].push(record);
      });

      const traces = Object.keys(groupedRecords).map(region => {
        const regRecords = groupedRecords[region];
        return {
          x: regRecords.map(r => r.DATE),
          y: regRecords.map(r => Number(r.VALUE)),
          type: "scatter",
          mode: "lines+markers",
          name: region,
          line: { width: 2 },
          marker: { size: 4 },
          hovertemplate: "<b>" + escapeHtml(region) + "</b><br>일자: %{x}<br>값: %{y:,.2f}<extra></extra>"
        };
      });

      const layout = {
        margin: { l: 70, r: 30, t: 30, b: 60 },
        paper_bgcolor: "#ffffff",
        plot_bgcolor: "#ffffff",
        hovermode: "x unified",
        showlegend: true,
        legend: { orientation: "h", y: -0.3 },
        xaxis: { title: "일자", showgrid: false },
        yaxis: { title: "Index Value", gridcolor: "#e5edf5", zerolinecolor: "#d9e2ec" }
      };

      Plotly.newPlot(chartElement, traces, layout, { responsive: true, displaylogo: false });
    }

    function renderTable(records) {
      const tableHead = document.getElementById("dataTableHead");
      const tableBody = document.getElementById("dataTableBody");

      const selectedRegions = getSelectedRegions();

      if (records.length === 0 || selectedRegions.length === 0) {
        tableHead.innerHTML = "<tr><th>일자</th></tr>";
        tableBody.innerHTML = '<tr><td class="empty-message">표시할 데이터가 없습니다.</td></tr>';
        return;
      }

      // 피벗 구조 만들기: 세로축 = 날짜, 가로축 = 항로
      const pivotData = {};
      records.forEach(r => {
        if (!pivotData[r.DATE]) pivotData[r.DATE] = {};
        pivotData[r.DATE][r.REGION] = r.VALUE;
      });

      const dates = Object.keys(pivotData).sort().reverse(); // 최신 날짜가 위로

      // 헤더 구성
      let headHtml = '<tr><th>일자</th>';
      selectedRegions.forEach(reg => {
        headHtml += `<th class="numeric">${escapeHtml(reg)}</th>`;
      });
      headHtml += '</tr>';
      tableHead.innerHTML = headHtml;

      // 바디 구성
      let bodyHtml = "";
      dates.forEach(date => {
        bodyHtml += `<tr><td>${escapeHtml(formatDate(date))}</td>`;
        selectedRegions.forEach(reg => {
          const val = pivotData[date][reg];
          bodyHtml += `<td class="numeric">${val !== undefined ? escapeHtml(formatNumber(val)) : "-"}</td>`;
        });
        bodyHtml += `</tr>`;
      });
      tableBody.innerHTML = bodyHtml;
    }

    function downloadCSV() {
      const records = getFilteredRecords();
      const selectedRegions = getSelectedRegions();

      if (records.length === 0 || selectedRegions.length === 0) {
        alert("다운로드할 데이터가 없습니다.");
        return;
      }

      const pivotData = {};
      records.forEach(r => {
        if (!pivotData[r.DATE]) pivotData[r.DATE] = {};
        pivotData[r.DATE][r.REGION] = r.VALUE;
      });

      const dates = Object.keys(pivotData).sort().reverse();

      let csvRows = [];
      // 헤더
      let headers = ["DATE", ...selectedRegions];
      csvRows.push(headers.join(","));

      // 데이터 행
      dates.forEach(date => {
        let row = [date];
        selectedRegions.forEach(reg => {
          let val = pivotData[date][reg];
          row.push(val !== undefined ? val : "");
        });
        csvRows.push(row.join(","));
      });

      const csvString = csvRows.join("\n");
      const blob = new Blob(["\uFEFF" + csvString], { type: "text/csv;charset=utf-8;" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.setAttribute("href", url);
      a.setAttribute("download", `${currentIndex}_dashboard_data.csv`);
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
    }

    function renderDashboard() {
      const records = getFilteredRecords();
      updateKpis(records);
      renderChart(records);
      renderTable(records);
    }

    function selectIndex(indexName) {
      currentIndex = indexName;

      document.getElementById("scfiTab").classList.toggle("active", indexName === "SCFI");
      document.getElementById("bdiTab").classList.toggle("active", indexName === "BDI");

      populateControls();
      renderDashboard();
    }

    async function loadDashboardData() {
      try {
        const response = await fetch("shipping_index_data.json?ts=" + Date.now(), {
          cache: "no-store"
        });

        if (!response.ok) {
          throw new Error("JSON request failed: HTTP " + response.status);
        }

        dashboardData = await response.json();
        const generatedAt = dashboardData.generated_at ? dashboardData.generated_at : "-";

        setStatus(true, "데이터 로드 완료", "최종 갱신: " + generatedAt);

        populateControls();
        renderDashboard();

      } catch (error) {
        console.error(error);
        setStatus(false, "데이터 로드 실패", "shipping_index_data.json 파일을 불러올 수 없습니다.");
        document.getElementById("dashboardDescription").textContent = "대시보드 데이터를 불러오는 중 오류가 발생했습니다.";
      }
    }

    document.addEventListener("DOMContentLoaded", function() {
      loadDashboardData();
    });
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
