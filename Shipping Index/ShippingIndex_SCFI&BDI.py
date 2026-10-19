import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

# =============================================================================
# 1. Path Configuration
# =============================================================================

SCRIPT_DIR = Path(__file__).resolve().parent

DB_PATH = SCRIPT_DIR / "index_history.db"
JSON_PATH = SCRIPT_DIR / "shipping_index_data.json"
HTML_PATH = SCRIPT_DIR / "index.html"

SUPPORTED_INDEXES = ["SCFI", "BDI"]

# =============================================================================
# 2. Common Functions
# =============================================================================

def log(message):
    """Print timestamped log message."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}")

def now_string():
    """Return current timestamp string."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def atomic_write_text(file_path, content):
    """
    Write to a temporary file first, then replace the target.
    This prevents incomplete output files.
    """
    file_path.parent.mkdir(parents=True, exist_ok=True)

    temp_path = file_path.with_suffix(file_path.suffix + ".tmp")

    with open(temp_path, "w", encoding="utf-8", newline="\n") as file:
        file.write(content)

    temp_path.replace(file_path)

# =============================================================================
# 3. Database Read and Data Cleaning
# =============================================================================

def load_index_data(index_name):
    """Load SCFI or BDI data from the SQLite database."""

    empty_df = pd.DataFrame(
        columns=["DATE", "REGION", "VALUE", "INDEX_NAME"]
    )

    if not DB_PATH.exists():
        log(f"[ERROR] Database file not found: {DB_PATH}")
        return empty_df

    query = """
        SELECT
            DATE,
            REGION,
            VALUE,
            INDEX_NAME
        FROM shipping_indices
        WHERE UPPER(INDEX_NAME) = UPPER(?)
        ORDER BY DATE ASC
    """

    try:
        with sqlite3.connect(DB_PATH) as connection:
            df = pd.read_sql_query(
                query,
                connection,
                params=[index_name]
            )

        log(f"[DB] {index_name} loaded: {len(df):,} rows")
        return df

    except Exception as error:
        log(f"[ERROR] Failed to load {index_name}: {error}")
        return empty_df

def clean_data(df, index_name):
    """Clean database data for JSON serialization."""

    if df.empty:
        return pd.DataFrame(
            columns=["DATE", "REGION", "VALUE", "INDEX_NAME"]
        )

    cleaned = df.copy()

    cleaned["DATE"] = pd.to_datetime(
        cleaned["DATE"],
        errors="coerce"
    ).dt.strftime("%Y-%m-%d")

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

    cleaned = cleaned.dropna(subset=["DATE", "VALUE"])

    # Keep the most recently stored record for same date and region.
    cleaned = cleaned.drop_duplicates(
        subset=["DATE", "REGION"],
        keep="last"
    )

    cleaned = cleaned.sort_values(
        by=["DATE", "REGION"]
    ).reset_index(drop=True)

    cleaned["VALUE"] = cleaned["VALUE"].astype(float)

    return cleaned[
        ["DATE", "REGION", "VALUE", "INDEX_NAME"]
    ]

# =============================================================================
# 4. JSON 파일 생성
# =============================================================================
def build_json():

    log("[JSON] shipping_index_data.json 생성 중...")

    log(f"DB_PATH   : {DB_PATH}")
    log(f"JSON_PATH : {JSON_PATH}")
    log(f"HTML_PATH : {HTML_PATH}")
    log(f"DB Exists : {DB_PATH.exists()}")

    now_str = get_now()
    indexes_dict = {}

    for idx in SUPPORTED_INDEXES:
        df = load_and_clean_data(idx)

        records = (
            df.to_dict(orient="records")
            if not df.empty else []
        )

        regions = (
            sorted(df["REGION"].unique().tolist())
            if not df.empty else []
        )

        indexes_dict[idx] = {
            "regions": regions,
            "records": records,
            "last_sync": now_str
        }

    payload = {
        "status": "success",
        "generated_at": now_str,
        "indexes": indexes_dict
    }

    atomic_write(
        JSON_PATH,
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2
        )
    )

    log(f"[JSON] 저장 완료: {JSON_PATH}")

# =============================================================================
# 5. HTML Generator
# =============================================================================

def generate_html_file():
    """
    Create GitHub Pages dashboard index.html.

    Important:
    This is a plain raw Python string, not an f-string.
    Therefore JavaScript braces do not conflict with Python formatting.
    """

    log("[HTML] Building index.html...")

    html_content = r"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
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
      font-family: Arial, "Noto Sans KR", sans-serif;
    }

    header {
      padding: 30px 20px;
      color: var(--white);
      background: linear-gradient(135deg, #102a43, #1f5fa8);
    }

    header .inner,
    main {
      max-width: 1280px;
      margin: 0 auto;
    }

    h1 {
      margin: 0;
      font-size: 32px;
    }

    header p {
      margin: 9px 0 0;
      color: #d9eafd;
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
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
      margin-bottom: 16px;
      padding: 13px 16px;
      font-size: 13px;
    }

    .panel {
      margin-bottom: 18px;
      padding: 20px;
    }

    h2 {
      margin: 0 0 16px;
      color: var(--navy);
