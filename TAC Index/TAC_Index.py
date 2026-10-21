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
    """Convert '$2.35', '2,345.00', '-1.5%' etc. to float."""
    if value is None:
        return None

    raw = normalize_text(value)

    if raw.lower() in {"", "-", "--", "n/a", "na", "null", "none"}:
        return None

    negative = raw.startswith("-") or (
        raw.startswith("(") and raw.endswith(")")
    )

    cleaned = raw.replace(",", "")
    cleaned = cleaned.replace("$", "")
    cleaned = cleaned.replace("£", "")
    cleaned = cleaned.replace("€", "")
    cleaned = cleaned.replace("%", "")
    cleaned = re.sub(r"[^0-9.]", "", cleaned)

    if not cleaned:
        return None

    try:
        result = float(cleaned)
        return -result if negative else result
    except ValueError:
        return None

def atomic_write_text(path: Path, content: str) -> None:
    """Prevent partially generated static files."""
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(path)

def get_now_string() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# =============================================================================
# 4. Database management and migration
# =============================================================================
def cleanup_duplicate_data_dates(conn: sqlite3.Connection) -> int:
    """
    Legacy DB에 동일 data_date가 중복된 경우 가장 최근 snapshot_id만 보존한다.

    정상적인 누적 구조에서는 날짜당 Master 레코드가 하나여야 한다.
    """
    duplicate_rows = conn.execute(
        """
        SELECT data_date
        FROM tac_master
        GROUP BY data_date
        HAVING COUNT(*) > 1
        """
    ).fetchall()

    deleted_count = 0

    for row in duplicate_rows:
        data_date = row["data_date"]

        snapshots = conn.execute(
            """
            SELECT snapshot_id
            FROM tac_master
            WHERE data_date = ?
            ORDER BY snapshot_id DESC
            """,
            (data_date,),
        ).fetchall()

        keep_snapshot_id = int(snapshots[0]["snapshot_id"])
        delete_snapshot_ids = [
            int(snapshot["snapshot_id"])
            for snapshot in snapshots[1:]
        ]

        for snapshot_id in delete_snapshot_ids:
            conn.execute(
                "DELETE FROM tac_master WHERE snapshot_id = ?",
                (snapshot_id,),
            )
            deleted_count += 1

        log_warning(
            "[DB] Duplicate data_date cleanup completed: "
            f"date={data_date}, kept_snapshot_id={keep_snapshot_id}, "
            f"deleted={len(delete_snapshot_ids)}"
        )

    return deleted_count

def get_db_connection() -> sqlite3.Connection:
    """
    Create/migrate SQLite DB.

    data_date는 논리적 Natural Key다.
    즉 하나의 원천 관측일에는 하나의 TAC Master Snapshot만 존재해야 한다.
    """
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

        CREATE INDEX IF NOT EXISTS idx_tac_detail_snapshot_id
            ON tac_detail(snapshot_id);

        CREATE INDEX IF NOT EXISTS idx_crawl_audit_finished_at
            ON crawl_audit(finished_at);
        """
    )

    # 기존 DB 중복 데이터 정리 후 날짜별 유일성 강제
    with conn:
        cleanup_duplicate_data_dates(conn)

        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_tac_master_data_date
            ON tac_master(data_date)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tac_master_data_date
            ON tac_master(data_date)
            """
        )

        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_tac_master_data_hash
            ON tac_master(data_hash)
            """
        )

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
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM tac_master"
        ).fetchone()

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

        return dict(row) if row else {}
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
        "[FETCH] Infogram response received: "
        f"HTTP {response.status_code}, size={len(response.text):,} bytes"
    )

    return response.text

def extract_balanced_json_object(
    source_text: str,
    start_position: int,
) -> Dict[str, Any]:
    """Extract a balanced JSON object beginning after infographicData assignment."""
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
                json_text = source_text[object_start:index + 1]

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
    if not isinstance(sheet, list):
        return []

    rows: List[List[Any]] = []

    for row in sheet:
        if not isinstance(row, list):
            continue

        rows.append([get_cell_value(cell) for cell in row])

    return rows

def get_chart_sheets(entity: Dict[str, Any]) -> List[List[List[Any]]]:
    props = entity.get("props", {})
    chart_data = props.get("chartData", {})

    sheets: List[List[List[Any]]] = []

    raw_data = chart_data.get("data", [])

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

def iter_chart_entities(
    data: Dict[str, Any],
) -> List[Tuple[str, Dict[str, Any]]]:
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
        if (
            isinstance(entity, dict)
            and str(entity.get("type", "")).upper() == "CHART"
        ):
            charts.append((str(entity_id), entity))

    return charts

def score_chart_sheet(rows: List[List[Any]]) -> int:
    if len(rows) < 2:
        return -10000

    header_text = " ".join(
        normalize_text(value).lower()
        for value in rows[0]
    )

    whole_text = " ".join(
        normalize_text(value).lower()
        for row in rows[:10]
        for value in row
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
            preview.append(
                f"entity={entity_id}, sheet={sheet_index}, "
                f"score={score}, header={rows[0] if rows else []}"
            )

        raise RuntimeError(
            "Unable to identify Baltic route-level chart. "
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

    if "-" not in normalized:
        return normalized, ""

    origin, destination = normalized.split("-", 1)
    return origin.strip(), destination.strip()

def parse_source_date(label: str) -> Optional[str]:
    """Convert Infogram date labels into ISO-8601 date strings."""
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

def build_snapshot_bundle(
    dataset: Dict[str, Any],
    data_index: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Build a single date-level snapshot bundle.

<<<<<<< Updated upstream
    If data_index is omitted, use the latest valid observation.
    If data_index is supplied, build a historical snapshot for that position.
=======
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

def get_latest_data_position(dataset: Dict[str, Any]) -> int:
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
    for index in range(from_index - 1, -1, -1):
        if index < len(values) and values[index] is not None:
            return values[index]

    return None

def build_snapshot_bundle(
    dataset: Dict[str, Any],
    data_index: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Build one date-level TAC snapshot from the parsed Infogram time series.

    - data_index=None: build the latest valid source observation.
    - data_index=int : build the historical observation at that position.
>>>>>>> Stashed changes
    """
    labels = dataset["labels"]
    source_dates = dataset["source_dates"]
    series = dataset["series"]

    if data_index is None:
        data_index = get_latest_data_position(dataset)

    if data_index < 0 or data_index >= len(labels):
        raise RuntimeError(f"Invalid TAC data position: {data_index}")

    source_label = labels[data_index]
    parsed_date = source_dates[data_index]

    if parsed_date is None:
        raise RuntimeError(
            "The source label could not be converted to a date. "
            f"Label: '{source_label}'"
        )

    detail_rows: List[Dict[str, Any]] = []
    current_values: List[float] = []
    change_values: List[float] = []

    for item in series:
        route_name = item["name"]
        values = item["values"]

        current_value = (
            values[data_index]
            if data_index < len(values)
            else None
        )

<<<<<<< Updated upstream
        # 해당 날짜에 노선 값이 없으면 해당 노선은 저장하지 않음
=======
        # 해당 기준일에 값이 없는 노선은 저장 대상에서 제외한다.
>>>>>>> Stashed changes
        if current_value is None:
            continue

        previous_value = get_previous_valid_value(values, data_index)

        weekly_change: Optional[float] = None
        if previous_value not in (None, 0):
            weekly_change = (
                (current_value - previous_value) / previous_value
            ) * 100
            change_values.append(weekly_change)

        origin, destination = split_route(route_name)

        detail_rows.append(
            {
                "route_code": route_name,
                "origin": origin,
                "destination": destination,
                "index_value": current_value,
                "weekly_change": weekly_change,
                "monthly_change": None,
                "ytd_change": None,
            }
        )

        current_values.append(current_value)

    if not detail_rows:
        raise RuntimeError(
            f"No route-level TAC values exist for '{source_label}'."
        )

    overall_index = sum(current_values) / len(current_values)

    average_change = (
        sum(change_values) / len(change_values)
        if change_values
        else None
    )

    return {
        "data_date": parsed_date,
        "source_label": source_label,
        "overall_index": overall_index,
        "weekly_change": average_change,
        "details": detail_rows,
    }

def build_all_snapshot_bundles(
    dataset: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
<<<<<<< Updated upstream
    Convert every valid Infogram date row into an independently storable
    TAC historical snapshot.
=======
    Build a separate database snapshot bundle for every valid date row
    available in the Infogram time-series dataset.
>>>>>>> Stashed changes
    """
    labels = dataset.get("labels", [])
    source_dates = dataset.get("source_dates", [])

    bundles: List[Dict[str, Any]] = []

<<<<<<< Updated upstream
    for index, _label in enumerate(labels):
=======
    for index, source_label in enumerate(labels):
>>>>>>> Stashed changes
        parsed_date = (
            source_dates[index]
            if index < len(source_dates)
            else None
        )

<<<<<<< Updated upstream
        # 날짜로 해석되지 않는 보조 행, 빈 행 등은 제외
        if parsed_date is None:
=======
        # 날짜로 인식되지 않는 빈 행 또는 보조 라벨은 제외한다.
        if parsed_date is None:
            log_warning(
                "[PARSE] Historical TAC row skipped because its label "
                f"cannot be parsed as a date: index={index}, "
                f"label='{source_label}'"
            )
>>>>>>> Stashed changes
            continue

        try:
            bundle = build_snapshot_bundle(
                dataset=dataset,
                data_index=index,
            )
            bundles.append(bundle)

        except RuntimeError as exc:
            log_warning(
<<<<<<< Updated upstream
                f"[PARSE] Historical TAC row skipped: "
                f"index={index}, label='{_label}', reason={exc}"
=======
                "[PARSE] Historical TAC row skipped: "
                f"index={index}, label='{source_label}', reason={exc}"
>>>>>>> Stashed changes
            )

    if not bundles:
        raise RuntimeError(
            "No valid historical TAC snapshots were created "
            "from the Infogram dataset."
        )

<<<<<<< Updated upstream
    bundles.sort(key=lambda row: row["data_date"])
=======
    bundles.sort(key=lambda bundle: bundle["data_date"])
>>>>>>> Stashed changes

    log_info(
        "[PARSE] Historical snapshot bundles created: "
        f"count={len(bundles)}, "
        f"first={bundles[0]['data_date']}, "
        f"last={bundles[-1]['data_date']}"
    )

    return bundles

<<<<<<< Updated upstream

def make_snapshot_hash(
    data_date: str,
    details: List[Dict[str, Any]],
) -> str:
    """Create a deterministic hash for source revision detection."""
=======
def make_snapshot_hash(bundle: Dict[str, Any]) -> str:
>>>>>>> Stashed changes
    normalized_payload = {
        "data_date": data_date,
        "details": sorted(
            details,
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

def build_all_snapshot_bundles(
    dataset: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    원천 차트의 모든 기간을 날짜별 Snapshot Bundle로 변환한다.

    예:
    - 121개 날짜
    - 날짜별 tac_master 1건
    - 각 날짜의 3개 노선은 tac_detail에 적재
    """
    labels = dataset["labels"]
    source_dates = dataset["source_dates"]
    series = dataset["series"]

    bundles: List[Dict[str, Any]] = []
    seen_dates = set()

    for index, source_label in enumerate(labels):
        data_date = source_dates[index]

        if not data_date:
            log_warning(
                "[PARSE] Skipping row due to unparseable date: "
                f"label={source_label}"
            )
            continue

        if data_date in seen_dates:
            log_warning(
                "[PARSE] Duplicate source date skipped: "
                f"date={data_date}, label={source_label}"
            )
            continue

        seen_dates.add(data_date)

        details: List[Dict[str, Any]] = []
        route_values: List[float] = []
        route_changes: List[float] = []

        for item in series:
            route_name = item["name"]
            values = item["values"]

            current_value = (
                values[index]
                if index < len(values)
                else None
            )

            if current_value is None:
                continue

            previous_value = get_previous_valid_value(values, index)

            periodic_change: Optional[float] = None

            if previous_value not in (None, 0):
                periodic_change = (
                    (current_value - previous_value)
                    / previous_value
                    * 100
                )
                route_changes.append(periodic_change)

            origin, destination = split_route(route_name)

            details.append(
                {
                    "route_code": route_name,
                    "origin": origin,
                    "destination": destination,
                    "index_value": current_value,
                    "weekly_change": periodic_change,
                    "monthly_change": None,
                    "ytd_change": None,
                }
            )

            route_values.append(current_value)

        if not details:
            continue

        overall_index = sum(route_values) / len(route_values)

        average_change = (
            sum(route_changes) / len(route_changes)
            if route_changes
            else None
        )

        bundles.append(
            {
                "data_date": data_date,
                "source_label": source_label,
                "overall_index": overall_index,
                "weekly_change": average_change,
                "details": details,
            }
        )

    bundles.sort(key=lambda row: row["data_date"])

    log_info(
        "[PARSE] Historical snapshot bundles created: "
        f"{len(bundles)} periods"
    )

    return bundles

# =============================================================================
# 7. Historical snapshot persistence
# =============================================================================
def save_all_snapshots(
    bundles: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    전체 시계열 데이터를 날짜 기준으로 누적 저장한다.

    Decision logic:
    1. DB에 data_date가 없으면 신규 INSERT
    2. data_date가 있고 hash가 같으면 데이터 유지
    3. data_date가 있고 hash가 다르면 원천 정정으로 간주하고 UPDATE
    """
    if not bundles:
        raise RuntimeError("No TAC snapshot bundles were provided for DB sync.")

    conn = get_db_connection()
    captured_at = get_now_string()

    new_count = 0
    updated_count = 0
    unchanged_count = 0

    latest_snapshot_id: Optional[int] = None
    latest_bundle: Optional[Dict[str, Any]] = None

    try:
        with conn:
            for bundle in bundles:
                data_date = bundle["data_date"]
                details = bundle["details"]

                data_hash = make_snapshot_hash(data_date, details)

                existing = conn.execute(
                    """
                    SELECT snapshot_id, data_hash
                    FROM tac_master
                    WHERE data_date = ?
                    LIMIT 1
                    """,
                    (data_date,),
                ).fetchone()

                # -----------------------------------------------------------------
                # 1. 신규 날짜: INSERT
                # -----------------------------------------------------------------
                if existing is None:
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
                            data_date,
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

                    new_count += 1

                # -----------------------------------------------------------------
                # 2. 기존 날짜: 동일 hash면 Skip, 다르면 Update
                # -----------------------------------------------------------------
                else:
                    snapshot_id = int(existing["snapshot_id"])

                    if existing["data_hash"] == data_hash:
                        unchanged_count += 1

                    else:
                        conn.execute(
                            """
                            UPDATE tac_master
                            SET
                                captured_at = ?,
                                data_hash = ?,
                                overall_index = ?,
                                weekly_change = ?,
                                route_count = ?
                            WHERE snapshot_id = ?
                            """,
                            (
                                captured_at,
                                data_hash,
                                bundle["overall_index"],
                                bundle["weekly_change"],
                                len(details),
                                snapshot_id,
                            ),
                        )

                        conn.execute(
                            """
                            DELETE FROM tac_detail
                            WHERE snapshot_id = ?
                            """,
                            (snapshot_id,),
                        )

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

                        updated_count += 1

                latest_snapshot_id = snapshot_id
                latest_bundle = bundle

        log_info(
            "[DB] Historical TAC sync completed: "
            f"new={new_count}, "
            f"updated={updated_count}, "
            f"unchanged={unchanged_count}, "
            f"processed={len(bundles)}"
        )

        return {
            "new_count": new_count,
            "updated_count": updated_count,
            "unchanged_count": unchanged_count,
            "latest_snapshot_id": latest_snapshot_id,
            "latest_bundle": latest_bundle,
        }

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

def build_raw_dataset_from_db() -> Dict[str, Any]:
    """
    HTML 그래프에 전달할 데이터를 DB 전체 누적 이력에서 재구축한다.

    크롤링 실패 시에도 기존 DB 이력을 기반으로 대시보드가 유지된다.
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

        snapshot_ids = [
            int(row["snapshot_id"])
            for row in master_rows
        ]

        labels = [
            row["data_date"]
            for row in master_rows
        ]

        placeholders = ",".join("?" for _ in snapshot_ids)

        detail_rows = conn.execute(
            f"""
            SELECT
                snapshot_id,
                route_code,
                index_value
            FROM tac_detail
            WHERE snapshot_id IN ({placeholders})
            ORDER BY route_code, snapshot_id
            """,
            snapshot_ids,
        ).fetchall()

        route_values: Dict[str, Dict[int, Optional[float]]] = {}

        for row in detail_rows:
            route_code = row["route_code"]
            snapshot_id = int(row["snapshot_id"])

            route_values.setdefault(route_code, {})
            route_values[route_code][snapshot_id] = row["index_value"]

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

def build_static_json() -> Dict[str, Any]:
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
                "Dashboard-derived arithmetic average of available "
                "route-level airfreight rates. "
                "This is not an official TAC composite index."
            ),
            "weekly_change": (
                "Dashboard-derived average percentage change compared "
                "with the preceding available source period."
            ),
            "unit": "USD/kg",
        },
        "last_sync": get_last_sync_meta(),
        "history": load_history(),
        "latest_detail": load_latest_details(),
        "raw_dataset": build_raw_dataset_from_db(),
    }

    atomic_write_text(
        JSON_PATH,
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
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
      --bg-alt: #171a22;
      --border: #252b38;
      --text: #edf2f7;
      --muted: #7d8798;
      --amber: #ff9900;
      --cyan: #00e5ff;
      --green: #00e676;
      --red: #ff334b;
      --purple: #bb86fc;
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }

    body {
      background: var(--bg-root);
      color: var(--text);
      font-family: Inter, Arial, sans-serif;
      font-size: 13px;
      padding-bottom: 40px;
    }

    .mono {
      font-family: "JetBrains Mono", Consolas, monospace;
    }

    .header {
      position: sticky;
      top: 0;
      z-index: 10;
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 11px 20px;
      border-bottom: 2px solid var(--amber);
      background: var(--bg-panel);
    }

    .title {
      font-size: 15px;
      font-weight: 700;
      letter-spacing: .5px;
    }

    .command {
      display: inline-block;
      margin-right: 10px;
      padding: 3px 6px;
      color: #000;
      background: var(--amber);
      font-size: 11px;
      font-weight: 800;
    }

    .meta {
      color: var(--muted);
      font-size: 11px;
    }

    .meta a {
      color: var(--cyan);
      text-decoration: none;
    }

    .container {
      max-width: 1400px;
      margin: 18px auto;
      padding: 0 16px;
    }

    .warning {
      margin-bottom: 14px;
      padding: 10px 12px;
      border: 1px solid var(--amber);
      color: var(--amber);
      background: rgba(255, 153, 0, .08);
      font-size: 11px;
    }

    .kpis {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 12px;
      margin-bottom: 16px;
    }

    .kpi {
      padding: 13px 14px;
      border: 1px solid var(--border);
      border-left: 3px solid var(--amber);
      background: var(--bg-panel);
    }

    .kpi.cyan { border-left-color: var(--cyan); }
    .kpi.green { border-left-color: var(--green); }
    .kpi.purple { border-left-color: var(--purple); }

    .kpi-label {
      margin-bottom: 7px;
      color: var(--muted);
      font-size: 10px;
      font-weight: 700;
    }

    .kpi-value {
      font-size: 22px;
      font-weight: 700;
    }

    .kpi-sub {
      margin-top: 6px;
      color: var(--muted);
      font-size: 10px;
    }

    .panel {
      margin-bottom: 16px;
      border: 1px solid var(--border);
      background: var(--bg-panel);
    }

    .panel-head {
      display: flex;
      justify-content: space-between;
      padding: 10px 14px;
      border-bottom: 1px solid var(--border);
      background: var(--bg-alt);
    }

    .panel-head h2 {
      color: var(--amber);
      font-size: 13px;
      letter-spacing: .4px;
    }

    .panel-desc {
      color: var(--muted);
      font-size: 10px;
    }

    .panel-body {
      padding: 14px;
    }

    .buttons {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-bottom: 14px;
    }

    .route-btn {
      padding: 5px 9px;
      border: 1px solid var(--border);
      color: var(--muted);
      background: #0f1117;
      cursor: pointer;
      font-family: "JetBrains Mono", monospace;
      font-size: 11px;
    }

    .route-btn.active {
      border-color: var(--amber);
      color: var(--amber);
      background: rgba(255, 153, 0, .12);
    }

    .chart-wrap {
      height: 420px;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
    }

    th {
      padding: 9px 12px;
      background: #0d0f15;
      border-bottom: 1px solid var(--border);
      color: var(--muted);
      font-size: 10px;
      text-align: left;
    }

    td {
      padding: 10px 12px;
      border-bottom: 1px solid #1d222c;
    }

    tr:hover {
      background: rgba(255, 153, 0, .04);
    }

    .right {
      text-align: right;
    }

    .route {
      padding: 2px 6px;
      border: 1px solid #303849;
      color: var(--cyan);
      background: #1c212b;
      font-size: 11px;
    }

    .up { color: var(--green); font-weight: 700; }
    .down { color: var(--red); font-weight: 700; }
    .flat { color: var(--muted); }

    .note {
      padding: 12px;
      border: 1px dashed var(--border);
      color: var(--muted);
      background: rgba(23, 26, 34, .5);
      font-size: 11px;
      line-height: 1.7;
    }

    .note strong {
      color: var(--amber);
    }

    @media (max-width: 900px) {
      .kpis { grid-template-columns: repeat(2, 1fr); }
      .meta { display: none; }
    }

    @media (max-width: 560px) {
      .kpis { grid-template-columns: 1fr; }
      .header { padding: 10px 12px; }
      .chart-wrap { height: 320px; }
    }
  </style>
</head>
<body>
  <header class="header">
    <div class="title">
      <span class="command mono">AIR &lt;GO&gt;</span>
      TAC INDEX <span style="color:#ff9900;font-weight:400;">// BALTIC AIRFREIGHT</span>
    </div>
    <div class="meta mono">
      SRC:
      <a href="__PARENT_URL__" target="_blank" rel="noopener">AIR CARGO NEWS</a>
      &nbsp; | &nbsp;
      UPDATED: __GENERATED_AT__
      &nbsp; | &nbsp;
      UNIT: <strong style="color:#ff9900;">USD/kg</strong>
    </div>
  </header>

  <main class="container">
    <div id="warning-area"></div>
    <section class="kpis mono" id="kpi-area"></section>

    <section class="panel">
      <div class="panel-head">
        <h2>■ ROUTE RATE TIME-SERIES MONITOR</h2>
        <span class="panel-desc mono">USD / KG</span>
      </div>
      <div class="panel-body">
        <div class="buttons" id="route-buttons"></div>
        <div class="chart-wrap">
          <canvas id="chart"></canvas>
        </div>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h2>■ LATEST SPOT RATE & PERIODIC DELTA</h2>
        <span class="panel-desc mono">LATEST OBSERVATION</span>
      </div>
      <div style="overflow-x:auto;">
        <table class="mono">
          <thead>
            <tr>
              <th>ROUTE</th>
              <th>ORIGIN</th>
              <th>DESTINATION</th>
              <th class="right">LATEST USD/KG</th>
              <th class="right">CHANGE VS PREV</th>
              <th class="right">DATE</th>
            </tr>
          </thead>
          <tbody id="detail-body"></tbody>
        </table>
      </div>
    </section>

    <footer class="note mono">
      <strong>[TERMINAL NOTE]</strong><br>
      Overall Index는 개별 노선 운임의 단순 산술평균이며 공식 TAC Composite Benchmark가 아닙니다.<br>
      Change vs Prev는 각 노선의 직전 가용 관측값 대비 변동률입니다.<br>
      데이터는 Air Cargo News의 Baltic Exchange Airfreight Rate Index Infogram 임베드에서 수집됩니다.
    </footer>
  </main>

  <script type="application/json" id="tac-data">
__JSON_PAYLOAD__
  </script>

  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>

  <script>
    (function () {
      "use strict";

      var DATA = JSON.parse(document.getElementById("tac-data").textContent);
      var selectedRoutes = {};
      var chart = null;

      var colors = [
        "#00e5ff",
        "#ff9900",
        "#00e676",
        "#ff334b",
        "#bb86fc",
        "#ffd600"
      ];

      function number(value, digits) {
        if (value === null || value === undefined || isNaN(Number(value))) {
          return "--";
        }

        return Number(value).toLocaleString("en-US", {
          minimumFractionDigits: digits,
          maximumFractionDigits: digits
        });
      }

      function percent(value) {
        if (value === null || value === undefined || isNaN(Number(value))) {
          return "--";
        }

        var num = Number(value);
        return (num >= 0 ? "+" : "") + num.toFixed(2) + "%";
      }

      function deltaClass(value) {
        if (value === null || value === undefined) return "flat";
        if (Number(value) > 0) return "up";
        if (Number(value) < 0) return "down";
        return "flat";
      }

      function arrow(value) {
        if (value === null || value === undefined) return "";
        if (Number(value) > 0) return "▲ ";
        if (Number(value) < 0) return "▼ ";
        return "";
      }

      function renderWarning() {
        var sync = DATA.last_sync || {};

        if (sync.status === "SUCCESS") return;

        document.getElementById("warning-area").innerHTML =
          '<div class="warning mono">' +
          '<strong>[WARN]</strong> Pipeline status: ' +
          (sync.status || "NO_AUDIT_LOG") +
          ' | ' +
          (sync.message || "Historical database cache is displayed.") +
          '</div>';
      }

      function renderKpi() {
        var history = DATA.history || [];
        var latest = history.length ? history[history.length - 1] : null;
        var detail = DATA.latest_detail || [];

        var cards = [
          {
            label: "LATEST DATE",
            value: latest ? latest.data_date : "--",
            sub: "Latest source period",
            cls: "cyan"
          },
          {
            label: "ROUTE AVERAGE",
            value: latest
              ? "$" + number(latest.overall_index, 2) + " <small>USD/kg</small>"
              : "--",
            sub: "Arithmetic route average",
            cls: ""
          },
          {
            label: "CHANGE VS PREV",
            value: latest && latest.weekly_change !== null
              ? '<span class="' + deltaClass(latest.weekly_change) + '">' +
                arrow(latest.weekly_change) +
                percent(latest.weekly_change) +
                "</span>"
              : "--",
            sub: "Average route movement",
            cls: latest && latest.weekly_change >= 0 ? "green" : "purple"
          },
          {
            label: "ACTIVE ROUTES",
            value: (latest ? latest.route_count : detail.length) + " <small>PAIRS</small>",
            sub: "Tracked air corridors",
            cls: "cyan"
          }
        ];

        document.getElementById("kpi-area").innerHTML = cards.map(function (card) {
          return (
            '<div class="kpi ' + card.cls + '">' +
              '<div class="kpi-label">' + card.label + '</div>' +
              '<div class="kpi-value">' + card.value + '</div>' +
              '<div class="kpi-sub">' + card.sub + '</div>' +
            '</div>'
          );
        }).join("");
      }

      function getSeriesMap() {
        var map = {};

        ((DATA.raw_dataset || {}).series || []).forEach(function (item) {
          map[item.name] = item;
        });

        return map;
      }

      function renderButtons() {
        var routeMap = getSeriesMap();
        var routes = Object.keys(routeMap);
        var target = document.getElementById("route-buttons");

        routes.forEach(function (route) {
          if (selectedRoutes[route] === undefined) {
            selectedRoutes[route] = true;
          }
        });

        target.innerHTML = routes.map(function (route) {
          return (
            '<button class="route-btn ' +
            (selectedRoutes[route] ? "active" : "") +
            '" data-route="' + route + '">' +
            route +
            '</button>'
          );
        }).join("");

        target.querySelectorAll(".route-btn").forEach(function (button) {
          button.addEventListener("click", function () {
            var route = button.getAttribute("data-route");
            selectedRoutes[route] = !selectedRoutes[route];

            var activeCount = Object.keys(selectedRoutes).filter(function (key) {
              return selectedRoutes[key];
            }).length;

            if (activeCount === 0) {
              selectedRoutes[route] = true;
            }

            renderButtons();
            renderChart();
          });
        });
      }

      function renderChart() {
        if (typeof Chart === "undefined") return;

        var routeMap = getSeriesMap();
        var labels = ((DATA.raw_dataset || {}).labels || []);

        var activeRoutes = Object.keys(routeMap).filter(function (route) {
          return selectedRoutes[route];
        });

        var datasets = activeRoutes.map(function (route, index) {
          var color = colors[index % colors.length];

          return {
            label: route,
            data: routeMap[route].values,
            borderColor: color,
            backgroundColor: color + "20",
            borderWidth: 2,
            tension: 0.2,
            spanGaps: true,
            pointRadius: 1.8,
            pointHoverRadius: 5,
            pointBackgroundColor: color
          };
        });

        if (chart) {
          chart.data.labels = labels;
          chart.data.datasets = datasets;
          chart.update();
          return;
        }

        chart = new Chart(
          document.getElementById("chart").getContext("2d"),
          {
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
                    color: "#a0aec0",
                    boxWidth: 10,
                    font: {
                      family: "JetBrains Mono",
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
                  callbacks: {
                    label: function (context) {
                      return " " + context.dataset.label + ": $" +
                        number(context.parsed.y, 2) + " / kg";
                    }
                  }
                }
              },
              scales: {
                x: {
                  ticks: {
                    color: "#738096",
                    maxTicksLimit: 14,
                    font: { family: "JetBrains Mono", size: 10 }
                  },
                  grid: {
                    color: "#202632"
                  }
                },
                y: {
                  title: {
                    display: true,
                    text: "USD / KG",
                    color: "#7d8798",
                    font: { family: "JetBrains Mono", size: 10 }
                  },
                  ticks: {
                    color: "#738096",
                    callback: function (value) {
                      return "$" + Number(value).toFixed(2);
                    }
                  },
                  grid: {
                    color: "#202632"
                  }
                }
              }
            }
          }
        );
      }

      function renderDetails() {
        var rows = DATA.latest_detail || [];
        var target = document.getElementById("detail-body");

        if (!rows.length) {
          target.innerHTML =
            '<tr><td colspan="6" class="flat" style="text-align:center;padding:22px;">No route data available.</td></tr>';
          return;
        }

        target.innerHTML = rows.map(function (row) {
          return (
            "<tr>" +
              '<td><span class="route">' + (row.route_code || "--") + "</span></td>" +
              "<td>" + (row.origin || "--") + "</td>" +
              "<td>" + (row.destination || "--") + "</td>" +
              '<td class="right" style="font-weight:700;">$' + number(row.index_value, 2) + "</td>" +
              '<td class="right ' + deltaClass(row.weekly_change) + '">' +
                arrow(row.weekly_change) +
                percent(row.weekly_change) +
              "</td>" +
              '<td class="right flat">' + (row.data_date || "--") + "</td>" +
            "</tr>"
          );
        }).join("");
      }

      renderWarning();
      renderKpi();
      renderButtons();
      renderChart();
      renderDetails();
    })();
  </script>
</body>
</html>
"""

def build_static_html(payload: Dict[str, Any]) -> None:
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

<<<<<<< Updated upstream
        # 기존: 최신 1건만 생성
        # 변경: Infogram의 모든 유효 날짜를 스냅샷으로 생성
=======
        # Infogram이 제공하는 전체 시계열을 월별/기간별 스냅샷으로 적재한다.
>>>>>>> Stashed changes
        bundles = build_all_snapshot_bundles(dataset)

        new_count = 0
        duplicate_count = 0
        latest_snapshot_id: Optional[int] = None

        for bundle in bundles:
            snapshot_id, is_new = save_snapshot(bundle)

            if is_new:
                new_count += 1
            else:
                duplicate_count += 1

            latest_snapshot_id = snapshot_id

        latest_bundle = bundles[-1]
        finished_at = get_now_string()

        save_crawl_audit(
            started_at=started_at,
            finished_at=finished_at,
            status="SUCCESS",
            message=(
                "HISTORICAL_SYNC "
                f"new={new_count}, "
                f"duplicate={duplicate_count}, "
                f"total={len(bundles)}"
            ),
            snapshot_id=latest_snapshot_id,
            route_count=len(latest_bundle["details"]),
            data_date=latest_bundle["data_date"],
        )

        log_info(
            "[SUCCESS] TAC historical sync completed: "
            f"new={new_count}, "
            f"duplicate={duplicate_count}, "
            f"total={len(bundles)}, "
            f"latest_date={latest_bundle['data_date']}"
        )

        return {
            "ok": True,
            "snapshot_id": latest_snapshot_id,
            "is_new": new_count > 0,
            "new_count": new_count,
            "duplicate_count": duplicate_count,
            "dataset": dataset,
            "bundle": latest_bundle,
            "bundles": bundles,
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
            logger.error(
                f"[ERROR] Failed to save crawl audit: {audit_exc}"
            )

        return {
            "ok": False,
            "error": str(exc),
        }

<<<<<<< Updated upstream

# =============================================================================
# 11. Entry point
# =============================================================================
=======
>>>>>>> Stashed changes
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
            "Return exit code 0 even when collection fails and no historical "
            "snapshot exists. Use only for non-blocking pipeline deployment."
        ),
    )

    return parser.parse_args()

def main() -> int:
    args = parse_arguments()

    log_info("=" * 72)
    log_info("[CRON] TAC Index Dashboard update started")
    log_info("=" * 72)

    # DB 생성 및 Migration 사전 실행
    conn = get_db_connection()
    conn.close()

    result = run_tac_sync()

    # 성공/실패와 무관하게 현재 DB 누적 데이터를 정적 파일로 재생성
    payload = build_static_json()
    build_static_html(payload)

    if result.get("ok"):
        log_info("[CRON] TAC Index Dashboard update completed successfully.")
        log_info("=" * 72)
        return 0

    historical_count = get_total_snapshot_count()

    if historical_count > 0:
        log_warning(
            "[WARN] Fresh TAC collection failed, but historical DB data "
            "was retained and static files were rebuilt."
        )
        log_info("=" * 72)
        return 0

    if args.allow_empty_success:
        log_warning(
            "[WARN] Fresh TAC collection failed and no historical snapshot "
            "exists. Empty static files were generated by explicit option."
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
