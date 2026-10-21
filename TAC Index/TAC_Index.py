# -*- coding: utf-8 -*-
"""
TAC Index / Baltic Exchange Airfreight Rate Index Crawler
GitHub Actions + GitHub Pages static dashboard version (Bloomberg Terminal Style)

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
            data_date      TEXT NOT NULL UNIQUE,
            data_hash      TEXT NOT NULL,
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
    if isinstance(cell, dict):
        for key in ("value", "formattedValue", "label", "text"):
            if key in cell:
                return cell[key]
        return ""

    return cell

def normalize_sheet(sheet: Any) -> List[List[Any]]:
    normalized_rows: List[List[Any]] = []

    if not isinstance(sheet, list):
        return normalized_rows

    for row in sheet:
        if not isinstance(row, list):
            continue

        normalized_rows.append([get_cell_value(cell) for cell in row])

    return normalized_rows

def get_chart_sheets(entity: Dict[str, Any]) -> List[List[List[Any]]]:
    props = entity.get("props", {})
    chart_data = props.get("chartData", {})

    raw_data = chart_data.get("data", [])

    sheets: List[List[List[Any]]] = []

    if isinstance(raw_data, list):
        for candidate in raw_data:
            normalized = normalize_sheet(candidate)
            if normalized:
                sheets.append(normalized)

    if not sheets and isinstance(chart_data.get("sheets"), list):
        for candidate in chart_data["sheets"]:
            normalized = normalize_sheet(candidate)
            if normalized:
                sheets.append(normalized)

    return sheets

def iter_chart_entities(data: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
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
    if len(rows) < 2:
        return -10000

    header_text = " ".join(normalize_text(v).lower() for v in rows[0])
    whole_text = " ".join(
        normalize_text(v).lower()
        for row in rows[:10]
        for v in row
    )

    score = 0

    if len(rows[0]) >= 3:
        score += 10

    if len(rows) >= 10:
        score += 5

    for keyword in EXPECTED_ROUTE_KEYWORDS:
        if keyword in header_text:
            score += 50
        elif keyword in whole_text:
            score += 10

    if "baltic" in whole_text:
        score += 10
    if "airfreight" in whole_text or "air freight" in whole_text:
        score += 10

    return score

def extract_best_chart_rows(data: Dict[str, Any]) -> List[List[Any]]:
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
    normalized = normalize_route_name(route_name)

    if "-" in normalized:
        origin, destination = normalized.split("-", 1)
        return origin.strip(), destination.strip()

    return normalized, ""

def parse_source_date(label: str) -> Optional[str]:
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
            return parsed.strftime("%Y-%m-%d")
        except ValueError:
            continue

    matched = re.search(r"(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})", raw)
    if matched:
        return parse_source_date(matched.group(1))

    matched = re.search(r"(\d{1,2}-[A-Za-z]{3}-\d{4})", raw)
    if matched:
        try:
            parsed = datetime.strptime(matched.group(1), "%d-%b-%Y")
            return parsed.strftime("%Y-%m-%d")
        except ValueError:
            pass

    return None

def build_dataset(rows: List[List[Any]]) -> Dict[str, Any]:
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

def get_previous_valid_value(
    values: List[Optional[float]],
    from_index: int,
) -> Optional[float]:
    for index in range(from_index - 1, -1, -1):
        if index < len(values) and values[index] is not None:
            return values[index]
    return None

def make_snapshot_hash(data_date: str, details: List[Dict[str, Any]]) -> str:
    normalized_payload = {
        "data_date": data_date,
        "details": sorted(details, key=lambda row: row["route_code"]),
    }
    raw = json.dumps(
        normalized_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def build_all_snapshot_bundles(dataset: Dict[str, Any]) -> List[Dict[str, Any]]:
    """모든 과거 시계열 포인트를 순회하며 날짜별 스냅샷 번들을 구축합니다."""
    labels = dataset["labels"]
    source_dates = dataset["source_dates"]
    series = dataset["series"]

    bundles: List[Dict[str, Any]] = []

    for idx, raw_label in enumerate(labels):
        parsed_date = source_dates[idx] or raw_label

        detail_rows: List[Dict[str, Any]] = []
        valid_values: List[float] = []
        change_values: List[float] = []

        for item in series:
            route_name = item["name"]
            values = item["values"]

            val = values[idx] if idx < len(values) else None
            if val is None:
                continue

            prev_val = get_previous_valid_value(values, idx)
            weekly_change: Optional[float] = None
            if prev_val not in (None, 0):
                weekly_change = ((val - prev_val) / prev_val) * 100
                change_values.append(weekly_change)

            origin, destination = split_route(route_name)
            detail_rows.append({
                "route_code": route_name,
                "origin": origin,
                "destination": destination,
                "index_value": val,
                "weekly_change": weekly_change,
                "monthly_change": None,
                "ytd_change": None,
            })
            valid_values.append(val)

        if not detail_rows:
            continue

        overall_index = sum(valid_values) / len(valid_values)
        average_change = sum(change_values) / len(change_values) if change_values else None

        bundles.append({
            "data_date": parsed_date,
            "source_label": raw_label,
            "overall_index": overall_index,
            "weekly_change": average_change,
            "details": detail_rows,
        })

    return bundles

# =============================================================================
# 7. Save snapshot (전체 시계열 누적 저장 지원)
# =============================================================================
def save_all_snapshots(bundles: List[Dict[str, Any]]) -> Tuple[int, int, Optional[int], Optional[Dict[str, Any]]]:
    """
    모든 날짜별 스냅샷을 DB에 누적 저장합니다.
    - 이미 존재하는 data_date는 해시 일치 여부 확인 후 스킵 또는 업데이트
    - 신규 data_date는 신규 INSERT
    """
    conn = get_db_connection()
    new_count = 0
    updated_count = 0
    latest_snapshot_id = None
    latest_bundle = None

    captured_at = get_now_string()

    try:
        for bundle in bundles:
            data_date = bundle["data_date"]
            details = bundle["details"]
            data_hash = make_snapshot_hash(data_date, details)

            existing = conn.execute(
                "SELECT snapshot_id, data_hash FROM tac_master WHERE data_date = ?",
                (data_date,),
            ).fetchone()

            if existing:
                s_id = int(existing["snapshot_id"])
                latest_snapshot_id = s_id
                latest_bundle = bundle

                if existing["data_hash"] != data_hash:
                    # 데이터 내용이 달라진 경우 업데이트
                    with conn:
                        conn.execute(
                            """
                            UPDATE tac_master
                            SET captured_at = ?, data_hash = ?, overall_index = ?, weekly_change = ?, route_count = ?
                            WHERE snapshot_id = ?
                            """,
                            (
                                captured_at,
                                data_hash,
                                bundle["overall_index"],
                                bundle["weekly_change"],
                                len(details),
                                s_id,
                            ),
                        )
                        conn.execute("DELETE FROM tac_detail WHERE snapshot_id = ?", (s_id,))
                        conn.executemany(
                            """
                            INSERT INTO tac_detail (
                                snapshot_id, route_code, origin, destination,
                                index_value, weekly_change, monthly_change, ytd_change
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            [
                                (
                                    s_id,
                                    r["route_code"],
                                    r["origin"],
                                    r["destination"],
                                    r["index_value"],
                                    r["weekly_change"],
                                    r["monthly_change"],
                                    r["ytd_change"],
                                )
                                for r in details
                            ],
                        )
                    updated_count += 1
                continue

            # 신규 삽입
            with conn:
                cursor = conn.execute(
                    """
                    INSERT INTO tac_master (
                        captured_at, data_date, data_hash, overall_index, weekly_change, route_count
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        captured_at,
                        data_date,
                        data_hash,
                        bundle["overall_index"],
                        bundle["weekly_change"],
                        len(details),
                    ),
                )
                s_id = int(cursor.lastrowid)
                latest_snapshot_id = s_id
                latest_bundle = bundle

                conn.executemany(
                    """
                    INSERT INTO tac_detail (
                        snapshot_id, route_code, origin, destination,
                        index_value, weekly_change, monthly_change, ytd_change
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            s_id,
                            r["route_code"],
                            r["origin"],
                            r["destination"],
                            r["index_value"],
                            r["weekly_change"],
                            r["monthly_change"],
                            r["ytd_change"],
                        )
                        for r in details
                    ],
                )
            new_count += 1

        log_info(
            f"[DB] Bulk sync completed: {new_count} new, {updated_count} updated, "
            f"total snapshots processed={len(bundles)}"
        )
        return new_count, updated_count, latest_snapshot_id, latest_bundle

    finally:
        conn.close()

# =============================================================================
# 8. Static JSON data build
# =============================================================================
def load_history() -> List[Dict[str, Any]]:
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
  <title>TAC INDEX // BALTIC AIRFREIGHT TERMINAL</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700;800&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg-root: #0a0b0e;
      --bg-panel: #12141a;
      --bg-panel-alt: #171a22;
      --border-subtle: #1e222d;
      --border-strong: #2a303f;
      --text-main: #f0f3f6;
      --text-muted: #737c8e;
      --text-dim: #454d5d;

      --bb-amber: #ff9900;
      --bb-amber-dim: #b36b00;
      --bb-cyan: #00e5ff;
      --bb-green: #00e676;
      --bb-red: #ff334b;
      --bb-purple: #bb86fc;
      --bb-yellow: #ffd600;
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }

    body {
      background-color: var(--bg-root);
      color: var(--text-main);
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      font-size: 13px;
      line-height: 1.5;
      -webkit-font-smoothing: antialiased;
      padding-bottom: 40px;
    }

    .mono {
      font-family: 'JetBrains Mono', Consolas, Menlo, monospace;
      letter-spacing: -0.2px;
    }

    .terminal-header {
      background-color: var(--bg-panel);
      border-bottom: 2px solid var(--bb-amber);
      padding: 10px 20px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      position: sticky;
      top: 0;
      z-index: 100;
      box-shadow: 0 4px 16px rgba(0,0,0,0.4);
    }

    .brand-section {
      display: flex;
      align-items: center;
      gap: 12px;
    }

    .terminal-cmd {
      background: var(--bb-amber);
      color: #000;
      font-weight: 800;
      font-size: 11px;
      padding: 3px 6px;
      border-radius: 2px;
    }

    .terminal-title {
      font-size: 15px;
      font-weight: 700;
      color: #fff;
      letter-spacing: 0.5px;
    }

    .terminal-title span {
      color: var(--bb-amber);
      font-weight: 400;
    }

    .meta-ticker {
      display: flex;
      align-items: center;
      gap: 16px;
      font-size: 11px;
      color: var(--text-muted);
    }

    .meta-ticker a {
      color: var(--bb-cyan);
      text-decoration: none;
    }
    .meta-ticker a:hover {
      text-decoration: underline;
    }

    .status-dot {
      display: inline-block;
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: var(--bb-green);
      margin-right: 5px;
      box-shadow: 0 0 8px var(--bb-green);
    }
    .status-dot.warn {
      background: var(--bb-amber);
      box-shadow: 0 0 8px var(--bb-amber);
    }

    .container {
      max-width: 1400px;
      margin: 18px auto 0;
      padding: 0 16px;
    }

    .kpi-matrix {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 12px;
      margin-bottom: 16px;
    }

    .kpi-box {
      background: var(--bg-panel);
      border: 1px solid var(--border-subtle);
      border-left: 3px solid var(--bb-amber);
      padding: 12px 14px;
      border-radius: 2px;
      position: relative;
    }

    .kpi-box.cyan { border-left-color: var(--bb-cyan); }
    .kpi-box.green { border-left-color: var(--bb-green); }
    .kpi-box.purple { border-left-color: var(--bb-purple); }

    .kpi-tag {
      font-size: 10px;
      text-transform: uppercase;
      font-weight: 700;
      color: var(--text-muted);
      margin-bottom: 6px;
      display: flex;
      justify-content: space-between;
    }

    .kpi-val {
      font-size: 24px;
      font-weight: 700;
      color: #ffffff;
      line-height: 1.1;
    }

    .kpi-sub {
      margin-top: 6px;
      font-size: 11px;
      color: var(--text-muted);
    }

    .panel {
      background: var(--bg-panel);
      border: 1px solid var(--border-subtle);
      border-radius: 2px;
      margin-bottom: 16px;
      overflow: hidden;
    }

    .panel-head {
      background: var(--bg-panel-alt);
      border-bottom: 1px solid var(--border-subtle);
      padding: 10px 14px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }

    .panel-head h2 {
      font-size: 13px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: var(--bb-amber);
      display: flex;
      align-items: center;
      gap: 8px;
    }

    .panel-desc {
      font-size: 11px;
      color: var(--text-muted);
    }

    .panel-body {
      padding: 14px;
    }

    .filter-bar {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-bottom: 14px;
      align-items: center;
    }

    .filter-label {
      font-size: 11px;
      color: var(--text-muted);
      text-transform: uppercase;
      margin-right: 4px;
      font-weight: 600;
    }

    .term-btn {
      background: #0f1117;
      border: 1px solid var(--border-strong);
      color: var(--text-muted);
      padding: 4px 10px;
      font-size: 11px;
      border-radius: 2px;
      cursor: pointer;
      transition: all 0.15s ease;
      font-family: 'JetBrains Mono', Consolas, monospace;
    }

    .term-btn:hover {
      border-color: var(--bb-amber);
      color: #fff;
    }

    .term-btn.active {
      background: rgba(255, 153, 0, 0.15);
      border-color: var(--bb-amber);
      color: var(--bb-amber);
      font-weight: 600;
    }

    .chart-box {
      height: 400px;
      position: relative;
    }

    .data-table-wrap {
      overflow-x: auto;
    }

    table.terminal-tbl {
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
      text-align: left;
    }

    table.terminal-tbl th {
      background: #0c0e13;
      color: var(--text-muted);
      font-weight: 600;
      text-transform: uppercase;
      font-size: 10.5px;
      letter-spacing: 0.4px;
      padding: 8px 12px;
      border-bottom: 1px solid var(--border-strong);
    }

    table.terminal-tbl td {
      padding: 9px 12px;
      border-bottom: 1px solid var(--border-subtle);
      color: #e2e8f0;
    }

    table.terminal-tbl tbody tr:hover {
      background: rgba(255, 153, 0, 0.04);
    }

    .text-right { text-align: right; }

    .badge-route {
      display: inline-block;
      padding: 2px 6px;
      background: #1e222d;
      border: 1px solid #2d3343;
      border-radius: 2px;
      font-size: 11px;
      font-weight: 600;
      color: var(--bb-cyan);
    }

    .delta-up { color: var(--bb-green); font-weight: 600; }
    .delta-down { color: var(--bb-red); font-weight: 600; }
    .delta-flat { color: var(--text-dim); }

    .terminal-footer {
      margin-top: 18px;
      padding: 12px;
      background: rgba(23, 26, 34, 0.6);
      border: 1px dashed var(--border-strong);
      border-radius: 2px;
      font-size: 11px;
      color: var(--text-muted);
      line-height: 1.6;
    }

    .terminal-footer strong { color: var(--bb-amber); }

    @media (max-width: 900px) {
      .kpi-matrix { grid-template-columns: repeat(2, 1fr); }
      .meta-ticker { display: none; }
      .chart-box { height: 320px; }
    }

    @media (max-width: 560px) {
      .kpi-matrix { grid-template-columns: 1fr; }
      .terminal-header { padding: 8px 12px; }
    }
  </style>
</head>
<body>
  <header class="terminal-header">
    <div class="brand-section">
      <div class="terminal-cmd mono">AIR &lt;GO&gt;</div>
      <div class="terminal-title">TAC INDEX <span>// BALTIC AIRFREIGHT</span></div>
    </div>
    <div class="meta-ticker mono">
      <span id="sync-dot-container"><span class="status-dot"></span>LIVE</span>
      <span>SRC: <a href="__PARENT_URL__" target="_blank" rel="noopener">AIR CARGO NEWS</a></span>
      <span>UPDATED: <span id="build-timestamp" style="color: #fff;">__GENERATED_AT__</span></span>
      <span>UNIT: <strong style="color: var(--bb-amber);">USD / kg</strong></span>
    </div>
  </header>

  <main class="container">
    <div id="sync-banner"></div>
    <section class="kpi-matrix mono" id="kpi-area"></section>

    <section class="panel">
      <div class="panel-head">
        <h2><span>■</span> ROUTE RATE TIME-SERIES MONITOR</h2>
        <span class="panel-desc mono">USD / KG INDEXED BENCHMARK</span>
      </div>
      <div class="panel-body">
        <div class="filter-bar">
          <span class="filter-label mono">SELECT ROUTE:</span>
          <div id="route-buttons" style="display: flex; flex-wrap: wrap; gap: 6px;"></div>
        </div>
        <div class="chart-box">
          <canvas id="rate-chart"></canvas>
        </div>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h2><span>■</span> SPOT RATE & PERIODIC DELTA BREAKDOWN</h2>
        <span class="panel-desc mono">LATEST OBSERVED BENCHMARKS</span>
      </div>
      <div class="panel-body" style="padding: 0;">
        <div class="data-table-wrap">
          <table class="terminal-tbl mono">
            <thead>
              <tr>
                <th>ROUTE CODE</th>
                <th>ORIGIN</th>
                <th>DESTINATION</th>
                <th class="text-right">LATEST SPOT (USD/KG)</th>
                <th class="text-right">PERIOD CHANGE (%)</th>
                <th class="text-right">OBSERVATION DATE</th>
              </tr>
            </thead>
            <tbody id="detail-table-body"></tbody>
          </table>
        </div>
      </div>
    </section>

    <footer class="terminal-footer mono">
      <strong>[TERMINAL NOTE]</strong><br>
      1. <strong>Overall Index</strong>는 원천 차트에서 식별된 주요 개별 노선 스팟 운임의 단순 산술 평균치이며 공식 TAC Composite Benchmark를 대변하지 않습니다.<br>
      2. <strong>Change vs. Prev</strong>는 결측치를 제외한 직전 가용 관측치와의 노선별 변동률 평균을 산출한 지표입니다.<br>
      3. 데이터 파이프라인은 Air Cargo News 공식 Infogram 임베드 소스로부터 실시간 동기화됩니다.
    </footer>
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

      var terminalColors = [
        "#00e5ff",
        "#ff9900",
        "#00e676",
        "#ff334b",
        "#bb86fc",
        "#ffd600"
      ];

      function formatNumber(value, digits) {
        if (value === null || value === undefined || Number.isNaN(Number(value))) {
          return "--";
        }
        return Number(value).toLocaleString("en-US", {
          minimumFractionDigits: digits,
          maximumFractionDigits: digits
        });
      }

      function formatPercent(value) {
        if (value === null || value === undefined || Number.isNaN(Number(value))) {
          return "--";
        }
        var num = Number(value);
        return (num >= 0 ? "+" : "") + num.toFixed(2) + "%";
      }

      function getDeltaClass(value) {
        if (value === null || value === undefined) return "delta-flat";
        var num = Number(value);
        if (num > 0) return "delta-up";
        if (num < 0) return "delta-down";
        return "delta-flat";
      }

      function getDeltaArrow(value) {
        if (value === null || value === undefined) return "";
        var num = Number(value);
        if (num > 0) return "▲ ";
        if (num < 0) return "▼ ";
        return "";
      }

      function renderStatus() {
        var lastSync = DATA.last_sync || {};
        var target = document.getElementById("sync-banner");
        var dot = document.querySelector(".status-dot");

        if (!lastSync.status || lastSync.status !== "SUCCESS") {
          if (dot) dot.className = "status-dot warn";
          if (target) {
            target.innerHTML =
              '<div style="background: rgba(255, 153, 0, 0.1); border: 1px solid var(--bb-amber); color: var(--bb-amber); padding: 8px 12px; margin-bottom: 14px; font-size: 11px;" class="mono">' +
              '<strong>[WARN]</strong> Pipeline sync status: ' + (lastSync.status || "NO_AUDIT_LOG") +
              ' | Message: ' + (lastSync.message || "Historical cache in use.") +
              '</div>';
          }
        }
      }

      function renderKpis() {
        var history = DATA.history || [];
        var latest = history.length ? history[history.length - 1] : null;
        var latestDetails = DATA.latest_detail || [];

        var cards = [
          {
            tag: "LATEST DATE",
            val: latest ? latest.data_date : "--",
            sub: "Latest Chart Period",
            cls: "cyan"
          },
          {
            tag: "OVERALL INDEX",
            val: (latest ? "$" + formatNumber(latest.overall_index, 2) : "--") + " <span style='font-size:12px; color:var(--text-muted);'>USD/kg</span>",
            sub: "Arithmetic Route Avg",
            cls: "amber"
          },
          {
            tag: "CHANGE VS PREV",
            val: (latest && latest.weekly_change !== null)
                 ? "<span class='" + getDeltaClass(latest.weekly_change) + "'>" + getDeltaArrow(latest.weekly_change) + formatPercent(latest.weekly_change) + "</span>"
                 : "--",
            sub: "Period-over-Period Delta",
            cls: latest && latest.weekly_change >= 0 ? "green" : "purple"
          },
          {
            tag: "ACTIVE ROUTES",
            val: (latest ? latest.route_count : latestDetails.length) + " <span style='font-size:12px; color:var(--text-muted);'>PAIRS</span>",
            sub: "Tracked O-D Air Corridors",
            cls: "cyan"
          }
        ];

        document.getElementById("kpi-area").innerHTML = cards.map(function (c) {
          return (
            '<div class="kpi-box ' + c.cls + '">' +
              '<div class="kpi-tag"><span>' + c.tag + '</span></div>' +
              '<div class="kpi-val">' + c.val + '</div>' +
              '<div class="kpi-sub mono">' + c.sub + '</div>' +
            '</div>'
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
          target.innerHTML = '<span class="mono" style="color:var(--text-dim);">No series available</span>';
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
            '<button class="term-btn' + active + '" data-route="' + route + '">' +
            route +
            '</button>'
          );
        }).join("");

        target.querySelectorAll(".term-btn").forEach(function (button) {
          button.addEventListener("click", function () {
            var route = button.getAttribute("data-route");
            selectedRoutes[route] = !selectedRoutes[route];

            var activeCount = Object.keys(selectedRoutes).filter(function (k) {
              return selectedRoutes[k];
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

        if (typeof Chart === "undefined") return;

        var datasets = routes.map(function (route, index) {
          var series = routeMap[route];
          var col = terminalColors[index % terminalColors.length];

          return {
            label: route,
            data: series.values,
            borderColor: col,
            backgroundColor: col + "15",
            borderWidth: 2,
            tension: 0.2,
            pointRadius: 2,
            pointHoverRadius: 5,
            pointBackgroundColor: col,
            pointBorderColor: "#0a0b0e",
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
                position: "top",
                align: "end",
                labels: {
                  boxWidth: 10,
                  boxHeight: 10,
                  color: "#a0aec0",
                  font: {
                    family: "'JetBrains Mono', Consolas, monospace",
                    size: 11
                  }
                }
              },
              tooltip: {
                backgroundColor: "#171a22",
                titleColor: "#ff9900",
                bodyColor: "#f0f3f6",
                borderColor: "#2a303f",
                borderWidth: 1,
                padding: 10,
                titleFont: { family: "'JetBrains Mono', monospace", size: 12 },
                bodyFont: { family: "'JetBrains Mono', monospace", size: 11 },
                callbacks: {
                  label: function (context) {
                    var val = context.parsed.y;
                    return " " + context.dataset.label + ": " +
                      (val === null ? "--" : "$" + Number(val).toFixed(2) + " / kg");
                  }
                }
              }
            },
            scales: {
              x: {
                ticks: {
                  color: "#64748b",
                  font: { family: "'JetBrains Mono', monospace", size: 10.5 },
                  maxTicksLimit: 14
                },
                grid: {
                  color: "#1e222d",
                  drawBorder: false
                }
              },
              y: {
                title: {
                  display: true,
                  text: "USD / KG",
                  color: "#737c8e",
                  font: { family: "'JetBrains Mono', monospace", size: 10 }
                },
                ticks: {
                  color: "#64748b",
                  font: { family: "'JetBrains Mono', monospace", size: 10.5 },
                  callback: function (val) {
                    return "$" + Number(val).toFixed(2);
                  }
                },
                grid: {
                  color: "#1e222d",
                  drawBorder: false
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
            '<tr><td colspan="6" style="text-align:center; color:var(--text-dim); padding:20px;">No latest route data available.</td></tr>';
          return;
        }

        target.innerHTML = rows.map(function (row) {
          var deltaCls = getDeltaClass(row.weekly_change);
          var arrow = getDeltaArrow(row.weekly_change);

          return (
            "<tr>" +
              '<td><span class="badge-route">' + (row.route_code || "--") + "</span></td>" +
              "<td>" + (row.origin || "--") + "</td>" +
              "<td>" + (row.destination || "--") + "</td>" +
              '<td class="text-right" style="font-weight:700; color:#fff;">$' + formatNumber(row.index_value, 2) + "</td>" +
              '<td class="text-right ' + deltaCls + '">' + arrow + formatPercent(row.weekly_change) + "</td>" +
              '<td class="text-right" style="color:var(--text-muted);">' + (row.data_date || "--") + "</td>" +
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

def build_raw_dataset_from_db() -> Dict[str, Any]:
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

def build_static_html(
    json_payload: Dict[str, Any],
    raw_dataset: Optional[Dict[str, Any]] = None,
) -> None:
    payload = dict(json_payload)
    payload["raw_dataset"] = raw_dataset or build_raw_dataset_from_db()

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

# =============================================================================
# 10. Main crawling process
# =============================================================================
def run_tac_sync() -> Dict[str, Any]:
    started_at = get_now_string()

    try:
        embed_html = fetch_infogram_html()
        infographic_data = extract_infographic_data(embed_html)
        chart_rows = extract_best_chart_rows(infographic_data)
        dataset = build_dataset(chart_rows)

        # 전체 과거 시계열 스냅샷 목록 생성
        bundles = build_all_snapshot_bundles(dataset)
        if not bundles:
            raise RuntimeError("No valid snapshot bundles could be constructed from dataset.")

        new_count, updated_count, latest_snapshot_id, latest_bundle = save_all_snapshots(bundles)

        finished_at = get_now_string()

        latest_date = latest_bundle["data_date"] if latest_bundle else None
        latest_route_count = len(latest_bundle["details"]) if latest_bundle else 0

        save_crawl_audit(
            started_at=started_at,
            finished_at=finished_at,
            status="SUCCESS",
            message=f"SYNC_OK (new:{new_count}, upd:{updated_count}, total:{len(bundles)})",
            snapshot_id=latest_snapshot_id,
            route_count=latest_route_count,
            data_date=latest_date,
        )

        log_info(
            f"[SUCCESS] TAC Index extraction and bulk sync completed: "
            f"new={new_count}, updated={updated_count}, latest_date={latest_date}"
        )

        return {
            "ok": True,
            "snapshot_id": latest_snapshot_id,
            "dataset": dataset,
            "bundle": latest_bundle,
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

    log_info("=" * 72)
    log_info("[CRON] TAC Index Dashboard update started")
    log_info("=" * 72)

    conn = get_db_connection()
    conn.close()

    result = run_tac_sync()

    payload = build_static_json()

    # DB에서 최신 시계열을 온전히 빌드하여 HTML 생성
    raw_dataset = build_raw_dataset_from_db()
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
