# -*- coding: utf-8 -*-
"""
TAC Index / Baltic Exchange Airfreight Rate Index Crawler
GitHub Actions + GitHub Pages static dashboard version

Data source:
- Air Cargo News Data Hub
- Infogram embedded chart

Output:
- TAC Index/TAC_Index.db
- TAC Index/tac_index_data.json
- TAC Index/index.html

Database schema:
- tac_master
- tac_detail
- crawl_audit
"""

import argparse
import hashlib
import html as html_lib
import json
import logging
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests
except ImportError:
    print("ERROR: 'requests' library is not installed. Run: pip install requests")
    sys.exit(1)

# =============================================================================
# 1. Basic configuration
# =============================================================================
BASE_DIR = Path(__file__).resolve().parent

DB_PATH = BASE_DIR / "TAC_Index.db"
JSON_PATH = BASE_DIR / "tac_index_data.json"
HTML_PATH = BASE_DIR / "index.html"
LOG_PATH = BASE_DIR / "tac_index_crawl.log"

INFOGRAM_ID = "SuAWaj6s2iFahJI4zPJw"

PARENT_URL = (
    "https://www.aircargonews.net/data-hub/industry-data/"
    "baltic-exchange-airfreight-rate-index/"
)

EMBED_URL = f"https://e.infogram.com/_/{INFOGRAM_ID}?src=embed"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
    "Referer": PARENT_URL,
}

REQUEST_TIMEOUT_SECONDS = 40

ROUTE_KO = {
    "Frankfurt-North America": "프랑크푸르트-북미",
    "Hong Kong-Europe": "홍콩-유럽",
    "Hong Kong-North America": "홍콩-북미",
}

EXPECTED_ROUTE_KEYWORDS = [
    "frankfurt",
    "hong kong",
    "north america",
    "europe",
]

# =============================================================================
# 2. Logging
# =============================================================================
logger = logging.getLogger("tac_index")
logger.setLevel(logging.INFO)
logger.handlers.clear()

_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(_formatter)
logger.addHandler(_console_handler)

try:
    _file_handler = logging.FileHandler(LOG_PATH, mode="a", encoding="utf-8")
    _file_handler.setFormatter(_formatter)
    logger.addHandler(_file_handler)
except OSError:
    pass

def log_info(message: str) -> None:
    logger.info(message)

def log_warning(message: str) -> None:
    logger.warning(message)

# =============================================================================
# 3. Common utility functions
# =============================================================================
def normalize_text(value: Any) -> str:
    """Normalize whitespace and HTML entities."""
    text = html_lib.unescape(str(value or ""))
    return " ".join(text.replace("\xa0", " ").strip().split())

def safe_float(value: Any) -> Optional[float]:
    """Convert source values such as '$2.35', '2,345.00', '-1.5%' into float."""
    if value is None:
        return None

    raw = normalize_text(value)
    if raw.lower() in {"", "-", "--", "n/a", "na", "null", "none"}:
        return None

    negative = raw.startswith("-") or (
        raw.startswith("(") and raw.endswith(")")
    )

    cleaned = raw.replace(",", "").replace("$", "").replace("£", "")
    cleaned = cleaned.replace("€", "").replace("%", "")
    cleaned = re.sub(r"[^0-9.]", "", cleaned)

    if not cleaned:
        return None

    try:
        result = float(cleaned)
        return -result if negative else result
    except ValueError:
        return None

def atomic_write_text(path: Path, content: str) -> None:
    """Write safely to prevent partially generated static files."""
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(path)

def json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")

def get_now_string() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# =============================================================================
# 4. Database management
# =============================================================================
def get_db_connection() -> sqlite3.Connection:
    """Create and migrate the SQLite database."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tac_master (
            snapshot_id    INTEGER PRIMARY KEY AUTOINCREMENT,
            captured_at    TEXT NOT NULL,
            data_date      TEXT NOT NULL,
            data_hash      TEXT NOT NULL UNIQUE,
            overall_index  REAL,
            weekly_change  REAL,
            route_count    INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tac_detail (
            snapshot_id     INTEGER NOT NULL,
            route_code      TEXT NOT NULL,
            origin          TEXT,
            destination     TEXT,
            index_value     REAL,
            weekly_change   REAL,
            monthly_change  REAL,
            ytd_change      REAL,
            PRIMARY KEY (snapshot_id, route_code),
            FOREIGN KEY (snapshot_id)
                REFERENCES tac_master(snapshot_id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS crawl_audit (
            audit_id     INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at   TEXT NOT NULL,
            finished_at  TEXT NOT NULL,
            status       TEXT NOT NULL,
            message      TEXT,
            snapshot_id  INTEGER,
            route_count  INTEGER,
            data_date    TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_tac_master_data_date
            ON tac_master(data_date);

        CREATE INDEX IF NOT EXISTS idx_tac_detail_snapshot_id
            ON tac_detail(snapshot_id);

        CREATE INDEX IF NOT EXISTS idx_crawl_audit_finished_at
            ON crawl_audit(finished_at);
        """
    )

    conn.commit()
    return conn

def save_crawl_audit(
    started_at: str,
    finished_at: str,
    status: str,
    message: str,
    snapshot_id: Optional[int] = None,
    route_count: int = 0,
    data_date: Optional[str] = None,
) -> None:
    conn = get_db_connection()
    try:
        conn.execute(
            """
            INSERT INTO crawl_audit (
                started_at,
                finished_at,
                status,
                message,
                snapshot_id,
                route_count,
                data_date
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                started_at,
                finished_at,
                status,
                str(message)[:2000],
                snapshot_id,
                int(route_count or 0),
                data_date,
            ),
        )
        conn.commit()
    finally:
        conn.close()

def get_total_snapshot_count() -> int:
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT COUNT(*) AS cnt FROM tac_master").fetchone()
        return int(row["cnt"]) if row else 0
    finally:
        conn.close()

def get_last_sync_meta() -> Dict[str, Any]:
    conn = get_db_connection()
    try:
        row = conn.execute(
            """
            SELECT
                started_at,
                finished_at,
                status,
                message,
                snapshot_id,
                route_count,
                data_date
            FROM crawl_audit
            ORDER BY audit_id DESC
            LIMIT 1
            """
        ).fetchone()

        if row is None:
            return {}

        return dict(row)
    finally:
        conn.close()

# =============================================================================
# 5. Infogram extraction
# =============================================================================
def fetch_infogram_html() -> str:
    """
    Retrieve the Infogram embed HTML.

    The previous local implementation used this direct request approach,
    which is retained because it avoids Selenium DOM timing issues.
    """
    log_info(f"[FETCH] Infogram embed request: {EMBED_URL}")

    response = requests.get(
        EMBED_URL,
        headers=HEADERS,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()

    log_info(
        f"[FETCH] Infogram response received: "
        f"HTTP {response.status_code}, size={len(response.text):,} bytes"
    )

    return response.text

def extract_balanced_json_object(source_text: str, start_position: int) -> Dict[str, Any]:
    """
    Extract a full JSON object by tracking braces, strings and escaped characters.

    This is safer than a regular expression because the Infogram payload can
    contain nested objects, long arrays and escaped strings.
    """
    object_start = source_text.find("{", start_position)
    if object_start < 0:
        raise RuntimeError("infographicData JSON object start was not found.")

    depth = 0
    in_string = False
    escaped = False

    for index in range(object_start, len(source_text)):
        char = source_text[index]

        if escaped:
            escaped = False
            continue

        if char == "\\":
            escaped = True
            continue

        if char == '"':
            in_string = not in_string
            continue

        if in_string:
            continue

        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1

            if depth == 0:
                json_text = source_text[object_start : index + 1]
                try:
                    return json.loads(json_text)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        f"Infogram JSON decoding failed: {exc}"
                    ) from exc

    raise RuntimeError("Infogram JSON object end was not found.")

def extract_infographic_data(embed_html: str) -> Dict[str, Any]:
    """Extract window.infographicData from the Infogram embed page."""
    log_info("[PARSE] Extracting Infogram infographicData...")

    patterns = [
        r"window\.infographicData\s*=",
        r"infographicData\s*=",
    ]

    for pattern in patterns:
        match = re.search(pattern, embed_html)
        if match:
            data = extract_balanced_json_object(embed_html, match.end())
            log_info("[PARSE] infographicData extraction completed.")
            return data

    raise RuntimeError(
        "Infogram infographicData was not found. "
        "The embed-page structure may have changed."
    )

def get_cell_value(cell: Any) -> Any:
    """Return a usable scalar from Infogram cell object / raw value."""
    if isinstance(cell, dict):
        for key in ("value", "formattedValue", "label", "text"):
            if key in cell:
                return cell[key]
        return ""

    return cell

def normalize_sheet(sheet: Any) -> List[List[Any]]:
    """Convert one Infogram sheet to a simple two-dimensional list."""
    normalized_rows: List[List[Any]] = []

    if not isinstance(sheet, list):
        return normalized_rows

    for row in sheet:
        if not isinstance(row, list):
            continue

        normalized_rows.append([get_cell_value(cell) for cell in row])

    return normalized_rows

def get_chart_sheets(entity: Dict[str, Any]) -> List[List[List[Any]]]:
    """Extract chart sheets from multiple possible Infogram structures."""
    props = entity.get("props", {})
    chart_data = props.get("chartData", {})

    raw_data = chart_data.get("data", [])

    sheets: List[List[List[Any]]] = []

    # Typical structure: chartData.data = [sheet1, sheet2, ...]
    if isinstance(raw_data, list):
        for candidate in raw_data:
            normalized = normalize_sheet(candidate)
            if normalized:
                sheets.append(normalized)

    # Fallback structure for certain embed variants.
    if not sheets and isinstance(chart_data.get("sheets"), list):
        for candidate in chart_data["sheets"]:
            normalized = normalize_sheet(candidate)
            if normalized:
                sheets.append(normalized)

    return sheets

def iter_chart_entities(data: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    """Find all CHART entities in the standard Infogram content tree."""
    entities = (
        data.get("elements", {})
        .get("content", {})
        .get("content", {})
        .get("entities", {})
    )

    if not isinstance(entities, dict):
        return []

    charts: List[Tuple[str, Dict[str, Any]]] = []

    for entity_id, entity in entities.items():
        if not isinstance(entity, dict):
            continue

        if str(entity.get("type", "")).upper() == "CHART":
            charts.append((str(entity_id), entity))

    return charts

def score_chart_sheet(rows: List[List[Any]]) -> int:
    """
    Score candidates to identify the Baltic airfreight chart.

    A sheet containing expected route terms gets the highest score.
    This prevents selecting an unrelated first chart from the infographic.
    """
    if len(rows) < 2:
        return -10000

    header_text = " ".join(normalize_text(v).lower() for v in rows[0])
    whole_text = " ".join(
        normalize_text(v).lower()
        for row in rows[:10]
        for v in row
    )

    score = 0

    # Data-quality score
    if len(rows[0]) >= 3:
        score += 10

    if len(rows) >= 10:
        score += 5

    # Route recognition score
    for keyword in EXPECTED_ROUTE_KEYWORDS:
        if keyword in header_text:
            score += 50
        elif keyword in whole_text:
            score += 10

    # BAI / Baltic related bonus
    if "baltic" in whole_text:
        score += 10
    if "airfreight" in whole_text or "air freight" in whole_text:
        score += 10

    return score

def extract_best_chart_rows(data: Dict[str, Any]) -> List[List[Any]]:
    """Find the most likely BAI route-level chart from all chart entities."""
    log_info("[PARSE] Searching BAI route-level chart data...")

    chart_entities = iter_chart_entities(data)
    if not chart_entities:
        raise RuntimeError("No CHART entity was found in infographicData.")

    candidates: List[Tuple[int, str, int, List[List[Any]]]] = []

    for entity_id, entity in chart_entities:
        sheets = get_chart_sheets(entity)

        for sheet_index, sheet in enumerate(sheets):
            score = score_chart_sheet(sheet)
            candidates.append((score, entity_id, sheet_index, sheet))

    if not candidates:
        raise RuntimeError("No usable chart sheet was found in Infogram data.")

    candidates.sort(key=lambda item: item[0], reverse=True)

    best_score, best_entity_id, best_sheet_index, best_rows = candidates[0]

    if best_score < 20:
        preview = []
        for score, entity_id, sheet_index, rows in candidates[:5]:
            first_row = rows[0] if rows else []
            preview.append(
                f"entity={entity_id}, sheet={sheet_index}, "
                f"score={score}, header={first_row}"
            )

        raise RuntimeError(
            "Unable to identify the Baltic route-level chart. "
            f"Candidate preview: {' | '.join(preview)}"
        )

    log_info(
        "[PARSE] Selected chart: "
        f"entity={best_entity_id}, sheet={best_sheet_index}, "
        f"score={best_score}, rows={len(best_rows)}"
    )

    return best_rows

# =============================================================================
# 6. Dataset transformation
# =============================================================================
def normalize_route_name(route_name: str) -> str:
    """Normalize source route spelling to stable DB route codes."""
    cleaned = normalize_text(route_name)
    cleaned = re.sub(r"\s*-\s*", "-", cleaned)

    replacements = {
        "Frankfurt - North America": "Frankfurt-North America",
        "Hong Kong - Europe": "Hong Kong-Europe",
        "Hong Kong - North America": "Hong Kong-North America",
        "Frankfurt to North America": "Frankfurt-North America",
        "Hong Kong to Europe": "Hong Kong-Europe",
        "Hong Kong to North America": "Hong Kong-North America",
    }

    return replacements.get(cleaned, cleaned)

def split_route(route_name: str) -> Tuple[str, str]:
    """Split route text into origin and destination."""
    normalized = normalize_route_name(route_name)

    if "-" in normalized:
        origin, destination = normalized.split("-", 1)
        return origin.strip(), destination.strip()

    return normalized, ""

def parse_source_date(label: str) -> Optional[str]:
    """
    Convert source labels into ISO data_date format.

    Supports common source label formats:
    - 2026-10-18
    - 18/10/2026
    - 18 Oct 2026
    - Oct 2026
    - Oct-26
    - October 2026
    """
    raw = normalize_text(label)

    date_formats = [
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%d/%m/%Y",
        "%m/%d/%Y",
        "%d-%m-%Y",
        "%d %b %Y",
        "%d %B %Y",
        "%b %Y",
        "%B %Y",
        "%b-%y",
        "%b-%Y",
        "%Y-%m",
        "%Y/%m",
    ]

    for fmt in date_formats:
        try:
            parsed = datetime.strptime(raw, fmt)

            # Monthly source labels are normalized to the first day of month.
            return parsed.strftime("%Y-%m-%d")
        except ValueError:
            continue

    # Example: "Week ending 18 October 2026"
    matched = re.search(
        r"(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})",
        raw,
    )
    if matched:
        return parse_source_date(matched.group(1))

    # Example: "18-Oct-2026"
    matched = re.search(
        r"(\d{1,2}-[A-Za-z]{3}-\d{4})",
        raw,
    )
    if matched:
        try:
            parsed = datetime.strptime(matched.group(1), "%d-%b-%Y")
            return parsed.strftime("%Y-%m-%d")
        except ValueError:
            pass

    return None

def build_dataset(rows: List[List[Any]]) -> Dict[str, Any]:
    """
    Convert Infogram rows into:
    {
      labels: [...],
      source_dates: [...],
      series: [
        {name: route, values: [...]}
      ]
    }
    """
    if not rows or len(rows) < 2:
        raise RuntimeError("The selected TAC chart dataset is empty.")

    header = rows[0]
    body = rows[1:]

    if len(header) < 2:
        raise RuntimeError(f"Unexpected chart header format: {header}")

    labels: List[str] = []
    source_dates: List[Optional[str]] = []

    for row in body:
        if not row:
            continue

        label = normalize_text(row[0])
        if not label:
            continue

        labels.append(label)
        source_dates.append(parse_source_date(label))

    if not labels:
        raise RuntimeError("No date labels were found in the TAC chart dataset.")

    series: List[Dict[str, Any]] = []

    for column_index in range(1, len(header)):
        route_name = normalize_route_name(str(header[column_index]))
        values: List[Optional[float]] = []

        valid_value_count = 0

        for row in body:
            if not row or not normalize_text(row[0]):
                continue

            raw_value = row[column_index] if column_index < len(row) else None
            numeric_value = safe_float(raw_value)

            if numeric_value is not None:
                valid_value_count += 1

            values.append(numeric_value)

        # Exclude empty / irrelevant columns.
        if valid_value_count == 0:
            continue

        series.append(
            {
                "name": route_name,
                "values": values,
            }
        )

    if not series:
        raise RuntimeError("No numeric TAC route series were extracted.")

    log_info(
        "[PARSE] Dataset created: "
        f"labels={len(labels)}, routes={len(series)}, "
        f"route_names={[series_item['name'] for series_item in series]}"
    )

    return {
        "labels": labels,
        "source_dates": source_dates,
        "series": series,
    }

def get_latest_data_position(dataset: Dict[str, Any]) -> int:
    """Find the latest point with at least one valid route value."""
    labels = dataset.get("labels", [])
    series = dataset.get("series", [])

    for index in range(len(labels) - 1, -1, -1):
        for item in series:
            values = item.get("values", [])
            if index < len(values) and values[index] is not None:
                return index

    raise RuntimeError("No valid latest data point was found.")

def get_previous_valid_value(
    values: List[Optional[float]],
    from_index: int,
) -> Optional[float]:
    """Find prior available value for period-on-period calculation."""
    for index in range(from_index - 1, -1, -1):
        if index < len(values) and values[index] is not None:
            return values[index]

    return None

def build_snapshot_bundle(dataset: Dict[str, Any]) -> Dict[str, Any]:
    """
    Create the latest snapshot record.

    overall_index:
        Mean of the latest observed rate across displayed routes.
        This is a dashboard convenience metric, not an official TAC composite index.

    weekly_change:
        Mean percentage change versus the preceding available source period
        across routes with valid consecutive values.
    """
    latest_index = get_latest_data_position(dataset)
    labels = dataset["labels"]
    source_dates = dataset["source_dates"]
    series = dataset["series"]

    latest_label = labels[latest_index]
    parsed_date = source_dates[latest_index]

    if parsed_date is None:
        raise RuntimeError(
            "The latest source label could not be converted to a date. "
            f"Latest label: '{latest_label}'"
        )

    detail_rows: List[Dict[str, Any]] = []
    latest_values: List[float] = []
    change_values: List[float] = []

    for item in series:
        route_name = item["name"]
        values = item["values"]

        latest_value = (
            values[latest_index]
            if latest_index < len(values)
            else None
        )

        if latest_value is None:
            continue

        previous_value = get_previous_valid_value(values, latest_index)

        weekly_change: Optional[float] = None
        if previous_value not in (None, 0):
            weekly_change = ((latest_value - previous_value) / previous_value) * 100
            change_values.append(weekly_change)

        origin, destination = split_route(route_name)

        detail_rows.append(
            {
                "route_code": route_name,
                "origin": origin,
                "destination": destination,
                "index_value": latest_value,
                "weekly_change": weekly_change,
                "monthly_change": None,
                "ytd_change": None,
            }
        )

        latest_values.append(latest_value)

    if not detail_rows:
        raise RuntimeError("No route-level values exist for the latest TAC date.")

    overall_index = sum(latest_values) / len(latest_values)
    average_change = (
        sum(change_values) / len(change_values)
        if change_values
        else None
    )

    return {
        "data_date": parsed_date,
        "source_label": latest_label,
        "overall_index": overall_index,
        "weekly_change": average_change,
        "details": detail_rows,
    }

def make_snapshot_hash(bundle: Dict[str, Any]) -> str:
    """Create a deterministic hash to prevent duplicate snapshots."""
    normalized_payload = {
        "data_date": bundle["data_date"],
        "source_label": bundle["source_label"],
        "details": sorted(
            bundle["details"],
            key=lambda row: row["route_code"],
        ),
    }

    raw = json.dumps(
        normalized_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

# =============================================================================
# 7. Save snapshot
# =============================================================================
def save_snapshot(bundle: Dict[str, Any]) -> Tuple[int, bool]:
    """Save the latest TAC snapshot. Return (snapshot_id, is_new_snapshot)."""
    data_hash = make_snapshot_hash(bundle)
    captured_at = get_now_string()
    details = bundle["details"]

    conn = get_db_connection()

    try:
        existing = conn.execute(
            """
            SELECT snapshot_id
            FROM tac_master
            WHERE data_hash = ?
            """,
            (data_hash,),
        ).fetchone()

        if existing:
            snapshot_id = int(existing["snapshot_id"])
            log_info(
                f"[DB] Duplicate TAC snapshot detected. "
                f"Using existing snapshot_id={snapshot_id}"
            )
            return snapshot_id, False

        with conn:
            cursor = conn.execute(
                """
                INSERT INTO tac_master (
                    captured_at,
                    data_date,
                    data_hash,
                    overall_index,
                    weekly_change,
                    route_count
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    captured_at,
                    bundle["data_date"],
                    data_hash,
                    bundle["overall_index"],
                    bundle["weekly_change"],
                    len(details),
                ),
            )

            snapshot_id = int(cursor.lastrowid)

            conn.executemany(
                """
                INSERT INTO tac_detail (
                    snapshot_id,
                    route_code,
                    origin,
                    destination,
                    index_value,
                    weekly_change,
                    monthly_change,
                    ytd_change
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        snapshot_id,
                        row["route_code"],
                        row["origin"],
                        row["destination"],
                        row["index_value"],
                        row["weekly_change"],
                        row["monthly_change"],
                        row["ytd_change"],
                    )
                    for row in details
                ],
            )

        log_info(
            f"[DB] New TAC snapshot saved: "
            f"id={snapshot_id}, "
            f"date={bundle['data_date']}, "
            f"routes={len(details)}"
        )

        return snapshot_id, True

    finally:
        conn.close()

# =============================================================================
# 8. Static JSON data build
# =============================================================================
def load_history() -> List[Dict[str, Any]]:
    """Load all historical snapshot summaries."""
    conn = get_db_connection()

    try:
        rows = conn.execute(
            """
            SELECT
                snapshot_id,
                captured_at,
                data_date,
                overall_index,
                weekly_change,
                route_count
            FROM tac_master
            ORDER BY data_date ASC, snapshot_id ASC
            """
        ).fetchall()

        return [dict(row) for row in rows]

    finally:
        conn.close()

def load_latest_details() -> List[Dict[str, Any]]:
    """Load detail rows for the newest snapshot."""
    conn = get_db_connection()

    try:
        rows = conn.execute(
            """
            SELECT
                d.snapshot_id,
                d.route_code,
                d.origin,
                d.destination,
                d.index_value,
                d.weekly_change,
                d.monthly_change,
                d.ytd_change,
                m.data_date,
                m.captured_at
            FROM tac_detail d
            INNER JOIN tac_master m
                ON d.snapshot_id = m.snapshot_id
            WHERE d.snapshot_id = (
                SELECT snapshot_id
                FROM tac_master
                ORDER BY data_date DESC, snapshot_id DESC
                LIMIT 1
            )
            ORDER BY d.route_code ASC
            """
        ).fetchall()

        return [dict(row) for row in rows]

    finally:
        conn.close()

def build_static_json() -> Dict[str, Any]:
    """Generate tac_index_data.json from SQLite data."""
    history = load_history()
    latest_detail = load_latest_details()
    last_sync = get_last_sync_meta()

    payload = {
        "generated_at": get_now_string(),
        "source": {
            "name": "Baltic Exchange Airfreight Rate Index",
            "publisher": "Air Cargo News",
            "parent_url": PARENT_URL,
            "embed_url": EMBED_URL,
            "infogram_id": INFOGRAM_ID,
        },
        "definition": {
            "overall_index": (
                "Dashboard-derived average of latest available route rates. "
                "It is not an official TAC composite index."
            ),
            "weekly_change": (
                "Dashboard-derived average percentage change versus the "
                "preceding available source period across routes."
            ),
            "unit": "USD/kg",
        },
        "last_sync": last_sync,
        "history": history,
        "latest_detail": latest_detail,
    }

    atomic_write_text(
        JSON_PATH,
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            default=json_default,
        ),
    )

    log_info(f"[STATIC] JSON generated: {JSON_PATH.name}")
    return payload

# =============================================================================
# 9. Static HTML dashboard
# =============================================================================
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>TAC Index | Baltic Exchange Airfreight Rate Index</title>
  <style>
    :root {
      --navy: #0f172a;
      --navy-2: #1e293b;
      --blue: #0ea5e9;
      --blue-dark: #0369a1;
      --bg: #f1f5f9;
      --card: #ffffff;
      --line: #e2e8f0;
      --text: #0f172a;
      --muted: #64748b;
      --green: #059669;
      --red: #dc2626;
      --amber: #d97706;
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      font-family: Arial, "Apple SD Gothic Neo", "Malgun Gothic", sans-serif;
      color: var(--text);
      background: var(--bg);
    }

    .hero {
      padding: 42px 24px;
      color: #fff;
      background: linear-gradient(135deg, #0f172a, #0c4a6e);
    }

    .hero-inner,
    .container {
      max-width: 1240px;
      margin: 0 auto;
    }

    h1 {
      margin: 0;
      font-size: 30px;
      letter-spacing: -0.4px;
    }

    .subtitle {
      margin: 10px 0 0;
      font-size: 15px;
      line-height: 1.6;
      color: #cbd5e1;
    }

    .container {
      padding: 28px 24px 52px;
    }

    .meta {
      display: flex;
      flex-wrap: wrap;
      gap: 8px 18px;
      margin: 0 0 22px;
      color: var(--muted);
      font-size: 13px;
    }

    .meta a {
      color: var(--blue-dark);
      text-decoration: none;
    }

    .status {
      padding: 14px 16px;
      border-radius: 9px;
      margin-bottom: 20px;
      font-size: 14px;
      line-height: 1.5;
    }

    .status.success {
      color: #065f46;
      background: #d1fae5;
      border: 1px solid #a7f3d0;
    }

    .status.warning {
      color: #92400e;
      background: #fef3c7;
      border: 1px solid #fde68a;
    }

    .kpis {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 16px;
      margin-bottom: 24px;
    }

    .kpi,
    .card {
      border: 1px solid var(--line);
      border-radius: 12px;
      background: var(--card);
      box-shadow: 0 2px 8px rgba(15, 23, 42, 0.04);
    }

    .kpi {
      padding: 18px;
    }

    .kpi-label {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.3px;
    }

    .kpi-value {
      margin-top: 9px;
      font-size: 25px;
      font-weight: 800;
      letter-spacing: -0.3px;
    }

    .kpi-note {
      margin-top: 8px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
    }

    .card {
      padding: 22px;
      margin-bottom: 22px;
    }

    .card h2 {
      margin: 0 0 8px;
      font-size: 20px;
    }

    .description {
      margin: 0 0 18px;
      color: var(--muted);
      font-size: 14px;
      line-height: 1.6;
    }

    .toolbar {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
      margin-bottom: 18px;
    }

    .route-button {
      padding: 8px 12px;
      color: #334155;
      border: 1px solid #cbd5e1;
      border-radius: 999px;
      background: #fff;
      font-size: 13px;
      cursor: pointer;
    }

    .route-button.active {
      color: #fff;
      border-color: var(--blue-dark);
      background: var(--blue-dark);
    }

    .chart-area {
      position: relative;
      height: 430px;
    }

    .table-wrap {
      overflow-x: auto;
    }

    table {
      width: 100%;
      min-width: 680px;
      border-collapse: collapse;
      font-size: 14px;
    }

    th,
    td {
      padding: 12px 10px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      white-space: nowrap;
    }

    th {
      color: #475569;
      background: #f8fafc;
      font-size: 12px;
    }

    .positive {
      color: var(--green);
      font-weight: 700;
    }

    .negative {
      color: var(--red);
      font-weight: 700;
    }

    .muted {
      color: var(--muted);
    }

    .footnote {
      margin: 18px 0 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.6;
    }

    @media (max-width: 850px) {
      .kpis {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .chart-area {
        height: 350px;
      }
    }

    @media (max-width: 520px) {
      .hero {
        padding: 32px 18px;
      }

      .container {
        padding: 20px 18px 42px;
      }

      .kpis {
        grid-template-columns: 1fr;
      }

      h1 {
        font-size: 24px;
      }
    }
  </style>
</head>
<body>
  <section class="hero">
    <div class="hero-inner">
      <h1>TAC Index · Baltic Exchange Airfreight Rate Index</h1>
      <p class="subtitle">
        Air Cargo News에 게시된 Baltic Exchange Airfreight Rate Index의 주요 노선별 운임 추이
      </p>
    </div>
  </section>

  <main class="container">
    <div class="meta">
      <span>Source: <a href="__PARENT_URL__" target="_blank" rel="noopener">Air Cargo News</a></span>
      <span>Last dashboard build: __GENERATED_AT__</span>
      <span>Unit: USD/kg</span>
    </div>

    <div id="sync-status"></div>
    <section class="kpis" id="kpi-area"></section>

    <section class="card">
      <h2>Route Rate Trend</h2>
      <p class="description">
        노선을 선택하여 운임 시계열을 비교할 수 있습니다. 그래프의 값은 원천 데이터의 USD/kg 기준입니다.
      </p>

      <div class="toolbar" id="route-buttons"></div>
      <div class="chart-area">
        <canvas id="rate-chart"></canvas>
      </div>
    </section>

    <section class="card">
      <h2>Latest Route Rates</h2>
      <p class="description">
        최신 원천 데이터 시점의 노선별 운임과 이전 가용 시점 대비 변동률입니다.
      </p>

      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Route</th>
              <th>Origin</th>
              <th>Destination</th>
              <th>Latest Rate (USD/kg)</th>
              <th>Change vs. Previous Period</th>
              <th>Data Date</th>
            </tr>
          </thead>
          <tbody id="detail-table-body"></tbody>
        </table>
      </div>

      <p class="footnote">
        주의: 포털의 “Overall Index”는 최신 가용 노선 운임의 단순 평균이며, 공식 TAC 복합지수를 의미하지 않습니다.
        “Weekly Change” 역시 각 노선의 이전 가용 관측치 대비 변동률 평균으로 계산한 참고 지표입니다.
      </p>
    </section>
  </main>

  <script type="application/json" id="tac-data">
__JSON_PAYLOAD__
  </script>

  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
  <script>
    (function () {
      "use strict";

      var dataElement = document.getElementById("tac-data");
      var DATA = JSON.parse(dataElement.textContent);
      var chart = null;
      var selectedRoutes = {};

      var colors = [
        "#0284c7",
        "#dc2626",
        "#059669",
        "#d97706",
        "#7c3aed",
        "#0891b2"
      ];

      function formatNumber(value, digits) {
        if (value === null || value === undefined || Number.isNaN(Number(value))) {
          return "-";
        }

        return Number(value).toLocaleString("en-US", {
          minimumFractionDigits: digits,
          maximumFractionDigits: digits
        });
      }

      function formatPercent(value) {
        if (value === null || value === undefined || Number.isNaN(Number(value))) {
          return "-";
        }

        var number = Number(value);
        return (number >= 0 ? "+" : "") + number.toFixed(2) + "%";
      }

      function getChangeClass(value) {
        if (value === null || value === undefined) {
          return "muted";
        }

        return Number(value) >= 0 ? "positive" : "negative";
      }

      function renderStatus() {
        var lastSync = DATA.last_sync || {};
        var target = document.getElementById("sync-status");

        if (!lastSync.status) {
          target.innerHTML =
            '<div class="status warning">수집 이력이 아직 없습니다.</div>';
          return;
        }

        var isSuccess = lastSync.status === "SUCCESS";
        var message = lastSync.message || "-";
        var finishedAt = lastSync.finished_at || "-";

        target.innerHTML =
          '<div class="status ' + (isSuccess ? "success" : "warning") + '">' +
          '<strong>Last Sync Status: ' + (isSuccess ? "SUCCESS" : "WARNING") + '</strong>' +
          '<br>Finished: ' + finishedAt +
          '<br>Message: ' + message +
          "</div>";
      }

      function renderKpis() {
        var history = DATA.history || [];
        var latest = history.length ? history[history.length - 1] : null;
        var latestDetails = DATA.latest_detail || [];

        var cards = [
          {
            label: "Latest Data Date",
            value: latest ? latest.data_date : "-",
            note: "원천 차트의 최신 관측 시점"
          },
          {
            label: "Overall Index",
            value: latest ? formatNumber(latest.overall_index, 2) : "-",
            note: "최신 노선 운임의 단순 평균"
          },
          {
            label: "Change vs. Previous",
            value: latest ? formatPercent(latest.weekly_change) : "-",
            note: "노선별 이전 가용 시점 대비 평균"
          },
          {
            label: "Route Count",
            value: latest ? latest.route_count : latestDetails.length,
            note: "최신 데이터가 있는 노선 수"
          }
        ];

        document.getElementById("kpi-area").innerHTML = cards.map(function (card) {
          return (
            '<article class="kpi">' +
              '<div class="kpi-label">' + card.label + "</div>" +
              '<div class="kpi-value">' + card.value + "</div>" +
              '<div class="kpi-note">' + card.note + "</div>" +
            "</article>"
          );
        }).join("");
      }

      function getRouteSeries() {
        var routeMap = {};

        (DATA.raw_dataset && DATA.raw_dataset.series || []).forEach(function (series) {
          routeMap[series.name] = series;
        });

        return routeMap;
      }

      function renderRouteButtons() {
        var routeMap = getRouteSeries();
        var routes = Object.keys(routeMap);
        var target = document.getElementById("route-buttons");

        if (!routes.length) {
          target.innerHTML = '<span class="muted">No historical route series available.</span>';
          return;
        }

        routes.forEach(function (route) {
          if (selectedRoutes[route] === undefined) {
            selectedRoutes[route] = true;
          }
        });

        target.innerHTML = routes.map(function (route) {
          var active = selectedRoutes[route] ? " active" : "";
          return (
            '<button class="route-button' + active + '" data-route="' + route + '">' +
            route +
            "</button>"
          );
        }).join("");

        target.querySelectorAll(".route-button").forEach(function (button) {
          button.addEventListener("click", function () {
            var route = button.getAttribute("data-route");
            selectedRoutes[route] = !selectedRoutes[route];

            var activeCount = Object.keys(selectedRoutes).filter(function (key) {
              return selectedRoutes[key];
            }).length;

            if (activeCount === 0) {
              selectedRoutes[route] = true;
            }

            renderRouteButtons();
            renderChart();
          });
        });
      }

      function renderChart() {
        var routeMap = getRouteSeries();
        var labels = (DATA.raw_dataset && DATA.raw_dataset.labels) || [];
        var routes = Object.keys(routeMap).filter(function (route) {
          return selectedRoutes[route];
        });

        if (typeof Chart === "undefined") {
          return;
        }

        var datasets = routes.map(function (route, index) {
          var series = routeMap[route];

          return {
            label: route,
            data: series.values,
            borderColor: colors[index % colors.length],
            backgroundColor: colors[index % colors.length] + "22",
            borderWidth: 2.5,
            tension: 0.25,
            pointRadius: 1.8,
            pointHoverRadius: 5,
            spanGaps: true
          };
        });

        var canvas = document.getElementById("rate-chart");

        if (chart) {
          chart.data.labels = labels;
          chart.data.datasets = datasets;
          chart.update();
          return;
        }

        chart = new Chart(canvas.getContext("2d"), {
          type: "line",
          data: {
            labels: labels,
            datasets: datasets
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: {
              mode: "index",
              intersect: false
            },
            plugins: {
              legend: {
                position: "top"
              },
              tooltip: {
                callbacks: {
                  label: function (context) {
                    var value = context.parsed.y;
                    return context.dataset.label + ": " +
                      (value === null ? "-" : Number(value).toFixed(2) + " USD/kg");
                  }
                }
              }
            },
            scales: {
              x: {
                ticks: {
                  maxTicksLimit: 16
                },
                grid: {
                  display: false
                }
              },
              y: {
                title: {
                  display: true,
                  text: "USD / kg"
                },
                ticks: {
                  callback: function (value) {
                    return Number(value).toFixed(2);
                  }
                }
              }
            }
          }
        });
      }

      function renderDetails() {
        var rows = DATA.latest_detail || [];
        var target = document.getElementById("detail-table-body");

        if (!rows.length) {
          target.innerHTML =
            '<tr><td colspan="6" class="muted">No latest route data available.</td></tr>';
          return;
        }

        target.innerHTML = rows.map(function (row) {
          return (
            "<tr>" +
              "<td>" + (row.route_code || "-") + "</td>" +
              "<td>" + (row.origin || "-") + "</td>" +
              "<td>" + (row.destination || "-") + "</td>" +
              "<td>" + formatNumber(row.index_value, 2) + "</td>" +
              '<td class="' + getChangeClass(row.weekly_change) + '">' +
                formatPercent(row.weekly_change) +
              "</td>" +
              "<td>" + (row.data_date || "-") + "</td>" +
            "</tr>"
          );
        }).join("");
      }

      renderStatus();
      renderKpis();
      renderRouteButtons();
      renderChart();
      renderDetails();
    })();
  </script>
</body>
</html>
"""

def build_static_html(
    json_payload: Dict[str, Any],
    raw_dataset: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Build the GitHub Pages dashboard.

    raw_dataset is included when a fresh crawl succeeds. If a crawl fails,
    the HTML is still generated from existing DB history and latest details.
    """
    payload = dict(json_payload)
    payload["raw_dataset"] = raw_dataset or {
        "labels": [],
        "series": [],
    }

    # Reconstruct chart data from existing database history when the fresh
    # source request did not succeed.
    if not raw_dataset:
        payload["raw_dataset"] = build_raw_dataset_from_db()

    json_payload_text = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
    ).replace("</", "<\\/")

    page = (
        HTML_TEMPLATE
        .replace("__PARENT_URL__", PARENT_URL)
        .replace("__GENERATED_AT__", get_now_string())
        .replace("__JSON_PAYLOAD__", json_payload_text)
    )

    atomic_write_text(HTML_PATH, page)
    log_info(f"[STATIC] HTML generated: {HTML_PATH.name}")

def build_raw_dataset_from_db() -> Dict[str, Any]:
    """
    Recreate route series from snapshots for dashboard rendering when
    no fresh Infogram dataset is available in the current execution.
    """
    conn = get_db_connection()

    try:
        master_rows = conn.execute(
            """
            SELECT snapshot_id, data_date
            FROM tac_master
            ORDER BY data_date ASC, snapshot_id ASC
            """
        ).fetchall()

        if not master_rows:
            return {
                "labels": [],
                "series": [],
            }

        labels = [row["data_date"] for row in master_rows]
        snapshot_ids = [row["snapshot_id"] for row in master_rows]

        placeholders = ",".join("?" for _ in snapshot_ids)

        detail_rows = conn.execute(
            f"""
            SELECT snapshot_id, route_code, index_value
            FROM tac_detail
            WHERE snapshot_id IN ({placeholders})
            ORDER BY route_code, snapshot_id
            """,
            snapshot_ids,
        ).fetchall()

        route_values: Dict[str, Dict[int, Optional[float]]] = {}

        for row in detail_rows:
            route_values.setdefault(row["route_code"], {})
            route_values[row["route_code"]][row["snapshot_id"]] = row["index_value"]

        series = []

        for route_code in sorted(route_values):
            values = [
                route_values[route_code].get(snapshot_id)
                for snapshot_id in snapshot_ids
            ]
            series.append(
                {
                    "name": route_code,
                    "values": values,
                }
            )

        return {
            "labels": labels,
            "series": series,
        }

    finally:
        conn.close()

# =============================================================================
# 10. Main crawling process
# =============================================================================
def run_tac_sync() -> Dict[str, Any]:
    """
    Fetch, parse and save a fresh TAC snapshot.

    No synthetic fallback values are created when the source cannot be read.
    """
    started_at = get_now_string()

    try:
        embed_html = fetch_infogram_html()
        infographic_data = extract_infographic_data(embed_html)
        chart_rows = extract_best_chart_rows(infographic_data)
        dataset = build_dataset(chart_rows)
        bundle = build_snapshot_bundle(dataset)

        snapshot_id, is_new = save_snapshot(bundle)

        finished_at = get_now_string()

        save_crawl_audit(
            started_at=started_at,
            finished_at=finished_at,
            status="SUCCESS",
            message=(
                "NEW_SNAPSHOT"
                if is_new
                else "DUPLICATE_SNAPSHOT"
            ),
            snapshot_id=snapshot_id,
            route_count=len(bundle["details"]),
            data_date=bundle["data_date"],
        )

        log_info(
            "[SUCCESS] TAC Index extraction completed: "
            f"date={bundle['data_date']}, "
            f"source_label={bundle['source_label']}, "
            f"routes={len(bundle['details'])}"
        )

        return {
            "ok": True,
            "snapshot_id": snapshot_id,
            "is_new": is_new,
            "dataset": dataset,
            "bundle": bundle,
        }

    except Exception as exc:
        finished_at = get_now_string()

        logger.exception(f"[ERROR] TAC collection failed: {exc}")

        try:
            save_crawl_audit(
                started_at=started_at,
                finished_at=finished_at,
                status="ERROR",
                message=str(exc),
            )
        except Exception as audit_exc:
            logger.error(f"[ERROR] Failed to save crawl audit: {audit_exc}")

        return {
            "ok": False,
            "error": str(exc),
        }

# =============================================================================
# 11. Entry point
# =============================================================================
def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="TAC Index / Baltic Exchange Airfreight Rate Index crawler"
    )

    parser.add_argument(
        "--cron",
        action="store_true",
        help="Run as a scheduled GitHub Actions task.",
    )

    parser.add_argument(
        "--allow-empty-success",
        action="store_true",
        help=(
            "Return exit code 0 even when initial source collection fails and "
            "no historical snapshot exists. Use only when uninterrupted "
            "pipeline deployment is more important than strict source validation."
        ),
    )

    return parser.parse_args()

def main() -> int:
    args = parse_arguments()

    # Default behavior is cron/static mode for GitHub Actions.
    log_info("=" * 72)
    log_info("[CRON] TAC Index Dashboard update started")
    log_info("=" * 72)

    # Ensure database/tables are initialized before the crawl.
    conn = get_db_connection()
    conn.close()

    result = run_tac_sync()

    # Build static files even if source collection failed.
    # This preserves any pre-existing snapshot history in GitHub Pages.
    payload = build_static_json()

    raw_dataset = result.get("dataset") if result.get("ok") else None
    build_static_html(payload, raw_dataset=raw_dataset)

    if result.get("ok"):
        log_info("[CRON] TAC Index Dashboard update completed successfully.")
        log_info("=" * 72)
        return 0

    historical_count = get_total_snapshot_count()

    if historical_count > 0:
        log_warning(
            "[WARN] Fresh TAC collection failed, but existing snapshot data "
            "was retained and static files were rebuilt."
        )
        log_info("=" * 72)
        return 0

    if args.allow_empty_success:
        log_warning(
            "[WARN] Fresh TAC collection failed and no historical snapshot "
            "exists. Empty dashboard files were generated by explicit option."
        )
        log_info("=" * 72)
        return 0

    log_warning(
        "[ERROR] TAC collection failed and no historical snapshot exists. "
        "Returning exit code 1."
    )
    log_info("=" * 72)
    return 1

if __name__ == "__main__":
    sys.exit(main())
