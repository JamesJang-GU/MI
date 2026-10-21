# -*- coding: utf-8 -*-
"""
Market Intelligence Portal - Main Dashboard Generator
통합 포털 index.html 생성 스크립트
Bloomberg / MI Terminal Single-Screen Layout
"""

import html
import json
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# =========================================================
# 1. 설정 및 경로 정의
# =========================================================
BASE_DIR = Path(__file__).resolve().parent

OUTPUT_HTML = BASE_DIR / "index.html"
OUTPUT_JSON = BASE_DIR / "dashboard_summary.json"

DB_PATHS = {
    "shipping": BASE_DIR / "Shipping Index" / "index_history.db",
    "alphaliner": BASE_DIR / "Alphaliner" / "alphaliner_data.db",
    "bunker": BASE_DIR / "BunkerPrice" / "bunker_price_data.db",
    "forwarder": BASE_DIR / "Forwarder" / "freight_forwarders_data.db",
    "tac": BASE_DIR / "TAC Index" / "TAC_Index.db",
    "macro": BASE_DIR / "GDP&Inflation" / "macro_data.db",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

logger = logging.getLogger("dashboard_generator")

# =========================================================
# 2. 공통 유틸리티
# =========================================================
def table_exists(cursor, table_name):
    """SQLite 테이블 존재 여부를 확인한다."""
    row = cursor.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        """,
        (table_name,),
    ).fetchone()

    return row is not None

def safe_html(value):
    """HTML 템플릿 치환 시 특수문자를 안전하게 변환한다."""
    return html.escape(str(value if value is not None else "-"))

def get_latest_available_date(*dates):
    """
    문자열 날짜 목록에서 가장 최신 날짜를 반환한다.
    ISO 형식 YYYY-MM-DD 기준으로 비교한다.
    """
    valid_dates = [
        date
        for date in dates
        if date and str(date).strip() != "-"
    ]

    return max(valid_dates) if valid_dates else "-"

def format_compact_number(value, suffix=""):
    """
    큰 숫자를 Terminal 카드용 축약 표기로 변환한다.

    예시:
    1250000 -> 1.25M
    24500   -> 24.5K
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"

    if number == 0:
        return "-"

    absolute_number = abs(number)

    if absolute_number >= 1_000_000_000:
        return f"{number / 1_000_000_000:,.2f}B{suffix}"

    if absolute_number >= 1_000_000:
        return f"{number / 1_000_000:,.2f}M{suffix}"

    if absolute_number >= 1_000:
        return f"{number / 1_000:,.1f}K{suffix}"

    return f"{number:,.0f}{suffix}"

def format_price(value, suffix=" USD/MT"):
    """
    벙커 가격 표시용 포맷.
    일반적으로 USD/MT 단위 가격은 축약하지 않고 소수점 두 자리까지 표시한다.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"

    if number == 0:
        return "-"

    return f"{number:,.2f}{suffix}"

def quote_identifier(identifier):
    """SQLite 테이블 및 컬럼 식별자를 안전하게 감싼다."""
    return '"' + str(identifier).replace('"', '""') + '"'

def get_table_columns(cursor, table_name):
    """
    SQLite 테이블의 실제 컬럼을 반환한다.

    반환 예시:
    {
        "date": "date",
        "port": "port",
        "vlsfo": "VLSFO"
    }
    """
    rows = cursor.execute(
        f"PRAGMA table_info({quote_identifier(table_name)})"
    ).fetchall()

    return {
        str(row[1]).lower(): str(row[1])
        for row in rows
    }

def find_first_column(columns, candidates):
    """
    후보 컬럼명 가운데 실제 DB에 존재하는 첫 컬럼을 반환한다.
    """
    for candidate in candidates:
        actual_column = columns.get(candidate.lower())

        if actual_column:
            return actual_column

    return None

def normalize_port_name(value):
    """주요 벙커 항만명을 표준화한다."""
    original_value = str(value or "").strip()
    normalized = original_value.upper()

    mapping = {
        "SINGAPORE": "Singapore",
        "SIN": "Singapore",
        "SINGAPORE PORT": "Singapore",
        "ROTTERDAM": "Rotterdam",
        "RDM": "Rotterdam",
        "ROTTERDAM PORT": "Rotterdam",
        "HOUSTON": "Houston",
        "HOU": "Houston",
        "HOUSTON PORT": "Houston",
    }

    return mapping.get(normalized, original_value)

def normalize_fuel_name(value):
    """유종명을 카드 표시 기준으로 정규화한다."""
    original_value = str(value or "").strip()
    normalized = (
        original_value.upper()
        .replace("-", "")
        .replace("_", "")
        .replace(" ", "")
    )

    mapping = {
        "VLSFO": "VLSFO",
        "VLSFO05": "VLSFO",
        "VLSFO0.5": "VLSFO",
        "VLSFO0,5": "VLSFO",
        "ULSVLSFO": "VLSFO",
        "LSMGO": "LSMGO",
        "MGO": "LSMGO",
        "DMA": "LSMGO",
        "LSGO": "LSMGO",
        "IFO380": "IFO380",
        "IFO380CST": "IFO380",
        "HSFO380": "IFO380",
        "RMG380": "IFO380",
    }

    return mapping.get(normalized, original_value)

def get_status_class(status):
    """포털 상태에 따른 CSS 클래스를 반환한다."""
    return "active" if status == "Active" else "nodata"

# =========================================================
# 3. 개별 모듈 데이터 요약 수집기
# =========================================================
def get_tac_summary():
    """TAC Index (Air Freight) 요약 정보 조회"""
    summary = {
        "title": "Air Freight (TAC Index)",
        "path": "TAC Index/index.html",
        "category": "Air",
        "latest_date": "-",
        "overall_index": "-",
        "weekly_change": "-",
        "route_count": "-",
        "status": "No Data",
    }

    db_path = DB_PATHS["tac"]

    if not db_path.exists():
        logger.warning(f"TAC Index DB not found: {db_path}")
        return summary

    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            cursor = conn.cursor()

            if not table_exists(cursor, "tac_master"):
                logger.warning("TAC table not found: tac_master")
                return summary

            row = cursor.execute(
                """
                SELECT
                    data_date,
                    overall_index,
                    weekly_change,
                    route_count
                FROM tac_master
                ORDER BY data_date DESC, snapshot_id DESC
                LIMIT 1
                """
            ).fetchone()

        if row:
            data_date, overall_index, weekly_change, route_count = row

            summary["latest_date"] = str(data_date or "-")
            summary["overall_index"] = (
                f"{float(overall_index):,.2f}"
                if overall_index is not None
                else "-"
            )
            summary["weekly_change"] = (
                f"{float(weekly_change):+.2f}%"
                if weekly_change is not None
                else "-"
            )
            summary["route_count"] = (
                str(route_count)
                if route_count is not None
                else "-"
            )
            summary["status"] = "Active"

    except Exception as error:
        logger.exception(f"Failed to read TAC Index DB: {error}")

    return summary

def get_shipping_summary():
    """SCFI / BDI Shipping Index 요약 정보 조회"""
    summary = {
        "title": "Shipping Index (SCFI / BDI)",
        "path": "Shipping Index/index.html",
        "category": "Sea",
        "scfi_latest": "-",
        "scfi_date": "-",
        "bdi_latest": "-",
        "bdi_date": "-",
        "status": "No Data",
    }

    db_path = DB_PATHS["shipping"]

    if not db_path.exists():
        logger.warning(f"Shipping Index DB not found: {db_path}")
        return summary

    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            cursor = conn.cursor()

            if not table_exists(cursor, "index_raw"):
                logger.warning(
                    f"Shipping Index table 'index_raw' not found: {db_path}"
                )
                return summary

            scfi_row = cursor.execute(
                """
                SELECT
                    dt,
                    value,
                    region
                FROM index_raw
                WHERE index_name = 'SCFI'
                  AND UPPER(TRIM(region)) IN (
                      'SCFI',
                      'COMPOSITE',
                      'COMPOSITE INDEX',
                      'SHANGHAI CONTAINERIZED FREIGHT INDEX',
                      '종합',
                      '종합지수'
                  )
                ORDER BY
                    dt DESC,
                    CASE UPPER(TRIM(region))
                        WHEN 'SCFI' THEN 1
                        WHEN 'COMPOSITE' THEN 2
                        WHEN 'COMPOSITE INDEX' THEN 3
                        WHEN 'SHANGHAI CONTAINERIZED FREIGHT INDEX' THEN 4
                        WHEN '종합지수' THEN 5
                        WHEN '종합' THEN 6
                        ELSE 99
                    END
                LIMIT 1
                """
            ).fetchone()

            bdi_row = cursor.execute(
                """
                SELECT
                    dt,
                    value,
                    region
                FROM index_raw
                WHERE index_name = 'BDI'
                  AND UPPER(TRIM(region)) = 'BDI'
                ORDER BY dt DESC
                LIMIT 1
                """
            ).fetchone()

        if scfi_row:
            scfi_date, scfi_value, _ = scfi_row

            summary["scfi_date"] = str(scfi_date or "-")
            summary["scfi_latest"] = (
                f"{float(scfi_value):,.1f}"
                if scfi_value is not None
                else "-"
            )

        if bdi_row:
            bdi_date, bdi_value, _ = bdi_row

            summary["bdi_date"] = str(bdi_date or "-")
            summary["bdi_latest"] = (
                f"{float(bdi_value):,.0f}"
                if bdi_value is not None
                else "-"
            )

        if scfi_row or bdi_row:
            summary["status"] = "Active"

    except Exception as error:
        logger.exception(f"Failed to read Shipping Index DB: {error}")

    return summary

def get_alphaliner_summary():
    """Alphaliner TOP 100 요약 정보 조회"""
    summary = {
        "title": "Alphaliner TOP 100",
        "path": "Alphaliner/index.html",
        "category": "Sea",
        "latest_date": "-",
        "total_teu": "-",
        "total_ships": "-",
        "top1_operator": "-",
        "status": "No Data",
    }

    db_path = DB_PATHS["alphaliner"]

    if not db_path.exists():
        logger.warning(f"Alphaliner DB not found: {db_path}")
        return summary

    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            cursor = conn.cursor()

            if not table_exists(cursor, "alphaliner_master"):
                logger.warning("Alphaliner master table not found.")
                return summary

            if not table_exists(cursor, "alphaliner_detail"):
                logger.warning("Alphaliner detail table not found.")
                return summary

            m_row = cursor.execute(
                """
                SELECT
                    snapshot_id,
                    data_date,
                    total_teu,
                    total_ships
                FROM alphaliner_master
                ORDER BY data_date DESC, snapshot_id DESC
                LIMIT 1
                """
            ).fetchone()

            if not m_row:
                return summary

            snapshot_id, data_date, total_teu, total_ships = m_row

            summary["latest_date"] = str(data_date or "-")
            summary["total_teu"] = (
                f"{int(total_teu):,}"
                if total_teu is not None
                else "-"
            )
            summary["total_ships"] = (
                f"{int(total_ships):,}"
                if total_ships is not None
                else "-"
            )

            d_row = cursor.execute(
                """
                SELECT
                    operator_name,
                    market_share
                FROM alphaliner_detail
                WHERE snapshot_id = ?
                  AND rank = 1
                """,
                (snapshot_id,),
            ).fetchone()

            if d_row:
                operator_name, market_share = d_row

                if market_share is None:
                    summary["top1_operator"] = str(operator_name or "-")
                else:
                    summary["top1_operator"] = (
                        f"{operator_name} ({float(market_share):.2f}%)"
                    )

            summary["status"] = "Active"

    except Exception as error:
        logger.exception(f"Failed to read Alphaliner DB: {error}")

    return summary

def get_forwarder_summary():
    """
    A&A Global Top 25 Forwarders 요약 정보 조회

    실제 Forwarder DB 구조:
    - DB: Forwarder/freight_forwarders_data.db
    - Table: forwarders
    - Columns:
      date, rank, provider, hq, revenue, teu, air_tons,
      source_url, created_at

    최신 Snapshot의 Global Rank 1위 업체를 포털 카드에 표시한다.
    """
    summary = {
        "title": "Global Top 25 Forwarders",
        "path": "Forwarder/index.html",
        "category": "Sea",
        "latest_date": "-",
        "rank": "-",
        "provider": "-",
        "revenue": "-",
        "teu": "-",
        "air_tons": "-",
        "status": "No Data",
    }

    db_path = DB_PATHS["forwarder"]

    if not db_path.exists():
        logger.warning(f"Forwarder DB not found: {db_path}")
        return summary

    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            cursor = conn.cursor()

            if not table_exists(cursor, "forwarders"):
                logger.warning(
                    "Forwarder table not found: forwarders "
                    f"(DB: {db_path})"
                )
                return summary

            latest_date_row = cursor.execute(
                """
                SELECT MAX(date)
                FROM forwarders
                """
            ).fetchone()

            latest_date = (
                latest_date_row[0]
                if latest_date_row and latest_date_row[0]
                else None
            )

            if not latest_date:
                logger.warning(
                    "Forwarder DB exists but no latest snapshot date was found."
                )
                return summary

            top_forwarder = cursor.execute(
                """
                SELECT
                    rank,
                    provider,
                    revenue,
                    teu,
                    air_tons
                FROM forwarders
                WHERE date = ?
                ORDER BY rank ASC
                LIMIT 1
                """,
                (latest_date,),
            ).fetchone()

        if not top_forwarder:
            logger.warning(
                "Forwarder latest snapshot exists but ranking records are missing."
            )
            return summary

        rank, provider, revenue, teu, air_tons = top_forwarder

        summary["latest_date"] = str(latest_date)
        summary["rank"] = f"#{int(rank):02d}" if rank is not None else "-"
        summary["provider"] = str(provider or "-")
        summary["revenue"] = format_compact_number(revenue, " USD")
        summary["teu"] = format_compact_number(teu, " TEU")
        summary["air_tons"] = format_compact_number(air_tons, " Tons")
        summary["status"] = "Active"

        logger.info(
            "Forwarder summary loaded: "
            f"{summary['provider']} / {summary['latest_date']}"
        )

    except Exception as error:
        logger.exception(f"Failed to read Forwarder DB: {error}")

    return summary

def get_bunker_summary():
    """
    Bunker Price 요약 정보 조회.

    포털 표시 기준:
    1. VLSFO / 주요 항만
       - Singapore
       - Rotterdam
       - Houston

    2. Singapore / 유종별
       - VLSFO
       - LSMGO
       - IFO380

    지원 DB 구조:
    A. Wide Format
       date / port / vlsfo / lsmgo / ifo380

    B. Long Format
       date / port / fuel 또는 product / price 또는 value

    현재 BunkerPrice DB가 VLSFO 데이터만 보유해도,
    VLSFO 가격이 존재하면 Active 상태로 전환한다.
    """
    summary = {
        "title": "Bunker Price Monitor",
        "path": "BunkerPrice/index.html",
        "category": "Sea",
        "latest_date": "-",
        "singapore_vlsfo": "-",
        "rotterdam_vlsfo": "-",
        "houston_vlsfo": "-",
        "vlsfo": "-",
        "lsmgo": "-",
        "ifo380": "-",
        "status": "No Data",
    }

    db_path = DB_PATHS["bunker"]

    if not db_path.exists():
        logger.warning(f"Bunker DB not found: {db_path}")
        return summary

    target_ports = {"Singapore", "Rotterdam", "Houston"}
    target_fuels = {"VLSFO", "LSMGO", "IFO380"}

    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            cursor = conn.cursor()

            table_rows = cursor.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                  AND name NOT LIKE 'sqlite_%'
                """
            ).fetchall()

            candidates = []

            for (table_name,) in table_rows:
                columns = get_table_columns(cursor, table_name)

                date_column = find_first_column(
                    columns,
                    [
                        "date",
                        "data_date",
                        "snapshot_date",
                        "record_date",
                        "dt",
                    ],
                )

                port_column = find_first_column(
                    columns,
                    [
                        "port",
                        "port_name",
                        "location",
                        "city",
                        "market",
                    ],
                )

                vlsfo_column = find_first_column(
                    columns,
                    [
                        "vlsfo",
                        "vlsfo_price",
                        "vlsfo_usd",
                        "vlsfo_usd_mt",
                    ],
                )

                lsmgo_column = find_first_column(
                    columns,
                    [
                        "lsmgo",
                        "lsmgo_price",
                        "lsmgo_usd",
                        "mgo",
                        "mgo_price",
                    ],
                )

                ifo380_column = find_first_column(
                    columns,
                    [
                        "ifo380",
                        "ifo380_price",
                        "ifo_380",
                        "hsfo380",
                        "hsfo_380",
                    ],
                )

                fuel_column = find_first_column(
                    columns,
                    [
                        "fuel",
                        "fuel_type",
                        "product",
                        "grade",
                        "bunker_type",
                        "fuel_grade",
                    ],
                )

                price_column = find_first_column(
                    columns,
                    [
                        "price",
                        "value",
                        "bunker_price",
                        "price_usd",
                        "usd_per_mt",
                        "usd_mt",
                        "rate",
                    ],
                )

                is_wide_format = bool(
                    date_column
                    and port_column
                    and (
                        vlsfo_column
                        or lsmgo_column
                        or ifo380_column
                    )
                )

                is_long_format = bool(
                    date_column
                    and port_column
                    and fuel_column
                    and price_column
                )

                if not is_wide_format and not is_long_format:
                    continue

                score = 0

                if is_wide_format:
                    score += 10

                    if vlsfo_column:
                        score += 10

                    if lsmgo_column:
                        score += 3

                    if ifo380_column:
                        score += 3

                if is_long_format:
                    score += 8

                candidates.append({
                    "table_name": table_name,
                    "columns": columns,
                    "date_column": date_column,
                    "port_column": port_column,
                    "vlsfo_column": vlsfo_column,
                    "lsmgo_column": lsmgo_column,
                    "ifo380_column": ifo380_column,
                    "fuel_column": fuel_column,
                    "price_column": price_column,
                    "is_wide_format": is_wide_format,
                    "is_long_format": is_long_format,
                    "score": score,
                })

            if not candidates:
                logger.warning(
                    "No compatible bunker table was found. "
                    "Expected date/port/vlsfo columns or date/port/fuel/price columns."
                )
                return summary

            candidates.sort(
                key=lambda item: item["score"],
                reverse=True,
            )

            selected = candidates[0]

            table_name = selected["table_name"]
            date_column = selected["date_column"]
            port_column = selected["port_column"]

            latest_date_row = cursor.execute(
                f"""
                SELECT MAX({quote_identifier(date_column)})
                FROM {quote_identifier(table_name)}
                """
            ).fetchone()

            latest_date = (
                latest_date_row[0]
                if latest_date_row and latest_date_row[0]
                else None
            )

            if not latest_date:
                logger.warning(
                    f"Bunker table '{table_name}' has no valid latest date."
                )
                return summary

            summary["latest_date"] = str(latest_date)

            # =================================================
            # A. Wide Format
            # =================================================
            if selected["is_wide_format"]:
                vlsfo_column = selected["vlsfo_column"]
                lsmgo_column = selected["lsmgo_column"]
                ifo380_column = selected["ifo380_column"]

                select_parts = [
                    f"{quote_identifier(port_column)} AS port_name",
                ]

                if vlsfo_column:
                    select_parts.append(
                        f"{quote_identifier(vlsfo_column)} AS vlsfo_value"
                    )
                else:
                    select_parts.append("NULL AS vlsfo_value")

                if lsmgo_column:
                    select_parts.append(
                        f"{quote_identifier(lsmgo_column)} AS lsmgo_value"
                    )
                else:
                    select_parts.append("NULL AS lsmgo_value")

                if ifo380_column:
                    select_parts.append(
                        f"{quote_identifier(ifo380_column)} AS ifo380_value"
                    )
                else:
                    select_parts.append("NULL AS ifo380_value")

                query = f"""
                    SELECT
                        {", ".join(select_parts)}
                    FROM {quote_identifier(table_name)}
                    WHERE {quote_identifier(date_column)} = ?
                """

                rows = cursor.execute(
                    query,
                    (latest_date,),
                ).fetchall()

                for (
                    port_name,
                    vlsfo_value,
                    lsmgo_value,
                    ifo380_value,
                ) in rows:
                    normalized_port = normalize_port_name(port_name)

                    if normalized_port not in target_ports:
                        continue

                    if normalized_port == "Singapore":
                        summary["singapore_vlsfo"] = format_price(vlsfo_value)
                        summary["vlsfo"] = format_price(vlsfo_value)
                        summary["lsmgo"] = format_price(lsmgo_value)
                        summary["ifo380"] = format_price(ifo380_value)

                    elif normalized_port == "Rotterdam":
                        summary["rotterdam_vlsfo"] = format_price(vlsfo_value)

                    elif normalized_port == "Houston":
                        summary["houston_vlsfo"] = format_price(vlsfo_value)

            # =================================================
            # B. Long Format
            # =================================================
            elif selected["is_long_format"]:
                fuel_column = selected["fuel_column"]
                price_column = selected["price_column"]

                query = f"""
                    SELECT
                        {quote_identifier(port_column)} AS port_name,
                        {quote_identifier(fuel_column)} AS fuel_name,
                        {quote_identifier(price_column)} AS price_value
                    FROM {quote_identifier(table_name)}
                    WHERE {quote_identifier(date_column)} = ?
                """

                rows = cursor.execute(
                    query,
                    (latest_date,),
                ).fetchall()

                for port_name, fuel_name, price_value in rows:
                    normalized_port = normalize_port_name(port_name)
                    normalized_fuel = normalize_fuel_name(fuel_name)

                    if normalized_fuel == "VLSFO":
                        if normalized_port == "Singapore":
                            summary["singapore_vlsfo"] = format_price(price_value)

                        elif normalized_port == "Rotterdam":
                            summary["rotterdam_vlsfo"] = format_price(price_value)

                        elif normalized_port == "Houston":
                            summary["houston_vlsfo"] = format_price(price_value)

                    if (
                        normalized_port == "Singapore"
                        and normalized_fuel in target_fuels
                    ):
                        if normalized_fuel == "VLSFO":
                            summary["vlsfo"] = format_price(price_value)

                        elif normalized_fuel == "LSMGO":
                            summary["lsmgo"] = format_price(price_value)

                        elif normalized_fuel == "IFO380":
                            summary["ifo380"] = format_price(price_value)

        valid_price_count = sum(
            price != "-"
            for price in [
                summary["singapore_vlsfo"],
                summary["rotterdam_vlsfo"],
                summary["houston_vlsfo"],
                summary["vlsfo"],
                summary["lsmgo"],
                summary["ifo380"],
            ]
        )

        if valid_price_count > 0:
            summary["status"] = "Active"

            logger.info(
                "Bunker summary loaded: "
                f"table={table_name}, date={summary['latest_date']}, "
                f"valid_price_count={valid_price_count}"
            )
        else:
            logger.warning(
                "Bunker DB was found but no usable latest price values were loaded."
            )

    except Exception as error:
        logger.exception(f"Failed to read Bunker DB: {error}")

    return summary

def get_macro_summary():
    """Macro Economies (GDP, Inflation & GSCPI) 요약 정보 조회"""
    summary = {
        "title": "Macro Economies & GSCPI",
        "path": "GDP&Inflation/index.html",
        "category": "Global",
        "latest_date": "-",
        "gscpi_latest": "-",
        "status": "No Data",
    }

    db_path = DB_PATHS["macro"]

    if not db_path.exists():
        logger.warning(f"Macro DB not found: {db_path}")
        return summary

    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            cursor = conn.cursor()

            if not table_exists(cursor, "newyorkfed_gscpi"):
                logger.warning("Macro table not found: newyorkfed_gscpi")
                return summary

            row = cursor.execute(
                """
                SELECT
                    date,
                    gscpi_value
                FROM newyorkfed_gscpi
                ORDER BY date DESC
                LIMIT 1
                """
            ).fetchone()

        if row:
            data_date, gscpi_value = row

            summary["latest_date"] = str(data_date or "-")
            summary["gscpi_latest"] = (
                f"{float(gscpi_value):,.2f}"
                if gscpi_value is not None
                else "-"
            )
            summary["status"] = "Active"

    except Exception as error:
        logger.exception(f"Failed to read Macro DB: {error}")

    return summary

# =========================================================
# 4. Bloomberg / MI Terminal HTML Template
# =========================================================
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
  >
  <title>MARKET INTELLIGENCE // TERMINAL PORTAL</title>

  <style>
    :root {
      --bg-dark: #02060b;
      --bg-panel: #070d15;
      --bg-item: #0b121c;
      --bg-hover: #101a27;
      --amber: #ff8a00;
      --amber-bright: #ffb14e;
      --cyan: #42c6e8;
      --cyan-bright: #75daf2;
      --green: #00d26a;
      --red: #ff4d4f;
      --text: #e7edf4;
      --text-muted: #8798aa;
      --text-dim: #5d6d7d;
      --border: #243342;
      --border-light: #405064;
      --mono: "IBM Plex Mono", "Roboto Mono", "Consolas", "Courier New", monospace;
      --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans KR", Arial, sans-serif;
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }

    html,
    body {
      min-height: 100%;
      background:
        linear-gradient(
          180deg,
          rgba(255, 138, 0, 0.035) 0%,
          rgba(2, 6, 11, 0) 260px
        ),
        var(--bg-dark);
      color: var(--text);
      font-family: var(--sans);
      font-size: 12px;
      line-height: 1.35;
      overflow-x: hidden;
    }

    body {
      display: flex;
      flex-direction: column;
      min-height: 100vh;
      padding: 10px 16px;
    }

    .container {
      width: 100%;
      max-width: 1600px;
      margin: 0 auto;
      flex: 1;
      display: flex;
      flex-direction: column;
      gap: 10px;
    }

    /* =====================================================
       Top Bloomberg Terminal Bar
       ===================================================== */
    .top-bar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      min-height: 34px;
      padding: 0 10px;
      border: 1px solid var(--border-light);
      background: var(--bg-panel);
      font-family: var(--mono);
      font-size: 11px;
    }

    .top-left {
      display: flex;
      align-items: center;
      gap: 10px;
      min-width: 0;
    }

    .badge-term {
      display: inline-block;
      padding: 2px 6px;
      background: var(--amber);
      color: #050505;
      font-weight: 800;
      font-size: 10px;
      letter-spacing: 0.4px;
    }

    .title-main {
      color: #ffffff;
      font-size: 14px;
      font-weight: 800;
      letter-spacing: 0.1px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .updated-at {
      flex-shrink: 0;
      color: var(--text-dim);
      font-size: 10px;
    }

    .updated-at span {
      color: var(--amber-bright);
    }

    /* =====================================================
       Section Layout
       ===================================================== */
    .section-group {
      display: flex;
      flex-direction: column;
      gap: 6px;
    }

    .section-title {
      display: flex;
      align-items: center;
      gap: 6px;
      color: var(--amber);
      font-family: var(--mono);
      font-size: 11px;
      font-weight: 800;
      letter-spacing: 0.65px;
      text-transform: uppercase;
    }

    .section-title::before {
      content: "//";
      color: var(--text-dim);
      font-weight: 800;
    }

    .grid-4 {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
    }

    .grid-split {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }

    /* =====================================================
       Terminal Cards
       ===================================================== */
    .card {
      position: relative;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      min-height: 148px;
      padding: 10px 12px;
      overflow: hidden;
      border: 1px solid var(--border);
      border-top: 2px solid var(--border-light);
      background:
        linear-gradient(
          135deg,
          rgba(255, 138, 0, 0.035),
          transparent 55%
        ),
        var(--bg-panel);
      color: inherit;
      text-decoration: none;
      transition: all 0.15s ease;
    }

    .card:hover {
      border-color: var(--amber);
      border-top-color: var(--amber);
      background: var(--bg-hover);
      transform: translateY(-2px);
      box-shadow: 0 5px 14px rgba(0, 0, 0, 0.45);
    }

    .bunker-card {
      min-height: 204px;
    }

    .card-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 7px;
      margin-bottom: 7px;
    }

    .card-title {
      min-width: 0;
      overflow: hidden;
      color: #ffffff;
      font-family: var(--mono);
      font-size: 11.5px;
      font-weight: 800;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .badge {
      flex-shrink: 0;
      padding: 1px 5px;
      border: 1px solid var(--border);
      color: var(--text-dim);
      font-family: var(--mono);
      font-size: 9px;
      font-weight: 800;
      letter-spacing: 0.2px;
    }

    .badge.active {
      border-color: var(--green);
      background: rgba(0, 210, 106, 0.1);
      color: var(--green);
    }

    .badge.nodata {
      border-color: var(--border);
      background: rgba(93, 109, 125, 0.12);
      color: var(--text-dim);
    }

    /* =====================================================
       KPI Components
       ===================================================== */
    .kpi-group {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 6px;
      margin: 4px 0 6px;
    }

    .kpi-item {
      min-width: 0;
      padding: 6px 8px;
      border: 1px solid rgba(64, 80, 100, 0.55);
      background: var(--bg-item);
    }

    .kpi-label {
      margin-bottom: 3px;
      overflow: hidden;
      color: var(--text-muted);
      font-family: var(--mono);
      font-size: 9.5px;
      letter-spacing: 0.1px;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .kpi-val {
      overflow: hidden;
      color: #ffffff;
      font-family: var(--mono);
      font-size: 13px;
      font-weight: 800;
      font-variant-numeric: tabular-nums;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .kpi-val.highlight {
      color: var(--cyan-bright);
    }

    .kpi-val.amber {
      color: var(--amber-bright);
    }

    .small-value {
      font-size: 10.5px;
      letter-spacing: -0.2px;
    }

    /* =====================================================
       Bunker-Specific Compact KPI Layout
       ===================================================== */
    .bunker-section-label {
      margin: 7px 0 4px;
      color: var(--amber);
      font-family: var(--mono);
      font-size: 8.5px;
      font-weight: 800;
      letter-spacing: 0.45px;
    }

    .bunker-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 5px;
      margin-bottom: 4px;
    }

    .bunker-grid .kpi-item {
      padding: 5px 6px;
    }

    .bunker-grid .kpi-label {
      font-size: 8.5px;
    }

    .bunker-grid .kpi-val {
      font-size: 9.5px;
      letter-spacing: -0.45px;
    }

    /* =====================================================
       Card Footer
       ===================================================== */
    .card-footer {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 6px;
      margin-top: 5px;
      padding-top: 6px;
      border-top: 1px solid var(--border);
      color: var(--text-dim);
      font-family: var(--mono);
      font-size: 9.5px;
    }

    .arrow {
      flex-shrink: 0;
      color: var(--amber);
      font-weight: 800;
    }

    /* =====================================================
       Portal Footer
       ===================================================== */
    footer {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      width: 100%;
      max-width: 1600px;
      margin: 10px auto 0;
      padding-top: 8px;
      border-top: 1px solid var(--border);
      color: var(--text-dim);
      font-family: var(--mono);
      font-size: 9.5px;
    }

    @media (max-width: 1250px) {
      .grid-4 {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
    }

    @media (max-width: 760px) {
      body {
        padding: 8px;
      }

      .grid-4,
      .grid-split {
        grid-template-columns: 1fr;
      }

      .top-bar {
        align-items: flex-start;
        flex-direction: column;
        gap: 5px;
        padding: 8px 10px;
      }

      .top-left {
        width: 100%;
      }

      .updated-at {
        padding-left: 1px;
      }

      footer {
        align-items: flex-start;
        flex-direction: column;
      }
    }
  </style>
</head>

<body>
  <div class="container">
    <header class="top-bar">
      <div class="top-left">
        <span class="badge-term">MI&lt;GO&gt;</span>
        <span class="title-main">
          MARKET INTELLIGENCE // MAIN PORTAL
        </span>
      </div>
      <div class="updated-at">
        UPDATED: <span>__GENERATED_AT__</span>
      </div>
    </header>

    <!-- =================================================
         1. Sea Section
         ================================================= -->
    <section class="section-group">
      <div class="section-title">Sea</div>

      <div class="grid-4">
        <!-- Shipping Index -->
        <a href="__SHIPPING_PATH__" class="card">
          <div>
            <div class="card-header">
              <span class="card-title">Shipping Index (SCFI / BDI)</span>
              <span class="badge __SHIPPING_STATUS_CLASS__">
                __SHIPPING_STATUS__
              </span>
            </div>

            <div class="kpi-group">
              <div class="kpi-item">
                <div class="kpi-label">SCFI Composite</div>
                <div class="kpi-val highlight">__SHIPPING_SCFI__</div>
              </div>

              <div class="kpi-item">
                <div class="kpi-label">BDI Composite</div>
                <div class="kpi-val amber">__SHIPPING_BDI__</div>
              </div>
            </div>
          </div>

          <div class="card-footer">
            <span>Date: __SHIPPING_DATE__</span>
            <span class="arrow">OPEN &rarr;</span>
          </div>
        </a>

        <!-- Alphaliner -->
        <a href="__ALPHALINER_PATH__" class="card">
          <div>
            <div class="card-header">
              <span class="card-title">Alphaliner TOP 100</span>
              <span class="badge __ALPHALINER_STATUS_CLASS__">
                __ALPHALINER_STATUS__
              </span>
            </div>

            <div class="kpi-group">
              <div class="kpi-item">
                <div class="kpi-label">Total Fleet Capacity</div>
                <div class="kpi-val">__ALPHALINER_TEU__</div>
              </div>

              <div class="kpi-item">
                <div class="kpi-label">#01 Operator / Share</div>
                <div class="kpi-val small-value highlight">
                  __ALPHALINER_TOP1__
                </div>
              </div>
            </div>
          </div>

          <div class="card-footer">
            <span>Date: __ALPHALINER_DATE__</span>
            <span class="arrow">OPEN &rarr;</span>
          </div>
        </a>

        <!-- Bunker Price -->
        <a href="__BUNKER_PATH__" class="card bunker-card">
          <div>
            <div class="card-header">
              <span class="card-title">Bunker Price Monitor</span>
              <span class="badge __BUNKER_STATUS_CLASS__">
                __BUNKER_STATUS__
              </span>
            </div>

            <div class="bunker-section-label">
              VLSFO / MAJOR PORTS
            </div>

            <div class="bunker-grid">
              <div class="kpi-item">
                <div class="kpi-label">Singapore</div>
                <div class="kpi-val highlight">
                  __BUNKER_SINGAPORE_VLSFO__
                </div>
              </div>

              <div class="kpi-item">
                <div class="kpi-label">Rotterdam</div>
                <div class="kpi-val">
                  __BUNKER_ROTTERDAM_VLSFO__
                </div>
              </div>

              <div class="kpi-item">
                <div class="kpi-label">Houston</div>
                <div class="kpi-val">
                  __BUNKER_HOUSTON_VLSFO__
                </div>
              </div>
            </div>

            <div class="bunker-section-label">
              SINGAPORE / FUEL GRADES
            </div>

            <div class="bunker-grid">
              <div class="kpi-item">
                <div class="kpi-label">VLSFO</div>
                <div class="kpi-val amber">
                  __BUNKER_VLSFO__
                </div>
              </div>

              <div class="kpi-item">
                <div class="kpi-label">LSMGO</div>
                <div class="kpi-val">
                  __BUNKER_LSMGO__
                </div>
              </div>

              <div class="kpi-item">
                <div class="kpi-label">IFO380</div>
                <div class="kpi-val">
                  __BUNKER_IFO380__
                </div>
              </div>
            </div>
          </div>

          <div class="card-footer">
            <span>Date: __BUNKER_DATE__</span>
            <span class="arrow">OPEN &rarr;</span>
          </div>
        </a>

        <!-- Forwarder Top 25 -->
        <a href="__FORWARDER_PATH__" class="card">
          <div>
            <div class="card-header">
              <span class="card-title">Global Top 25 Forwarders</span>
              <span class="badge __FORWARDER_STATUS_CLASS__">
                __FORWARDER_STATUS__
              </span>
            </div>

            <div class="kpi-group">
              <div class="kpi-item">
                <div class="kpi-label">#01 Provider</div>
                <div class="kpi-val small-value highlight">
                  __FORWARDER_PROVIDER__
                </div>
              </div>

              <div class="kpi-item">
                <div class="kpi-label">Revenue</div>
                <div class="kpi-val amber">
                  __FORWARDER_REVENUE__
                </div>
              </div>

              <div class="kpi-item">
                <div class="kpi-label">Ocean Volume</div>
                <div class="kpi-val small-value">
                  __FORWARDER_TEU__
                </div>
              </div>

              <div class="kpi-item">
                <div class="kpi-label">Air Volume</div>
                <div class="kpi-val small-value">
                  __FORWARDER_AIR_TONS__
                </div>
              </div>
            </div>
          </div>

          <div class="card-footer">
            <span>Snapshot: __FORWARDER_DATE__</span>
            <span class="arrow">OPEN &rarr;</span>
          </div>
        </a>
      </div>
    </section>

    <!-- =================================================
         2. Air & Global Section
         ================================================= -->
    <div class="grid-split">
      <section class="section-group">
        <div class="section-title">Air</div>

        <a href="__TAC_PATH__" class="card">
          <div>
            <div class="card-header">
              <span class="card-title">Air Freight (TAC Index)</span>
              <span class="badge __TAC_STATUS_CLASS__">
                __TAC_STATUS__
              </span>
            </div>

            <div class="kpi-group">
              <div class="kpi-item">
                <div class="kpi-label">Overall Index</div>
                <div class="kpi-val highlight">__TAC_INDEX__</div>
              </div>

              <div class="kpi-item">
                <div class="kpi-label">Weekly Change</div>
                <div class="kpi-val amber">__TAC_WEEKLY__</div>
              </div>
            </div>
          </div>

          <div class="card-footer">
            <span>Date: __TAC_DATE__ / Routes: __TAC_ROUTES__</span>
            <span class="arrow">OPEN &rarr;</span>
          </div>
        </a>
      </section>

      <section class="section-group">
        <div class="section-title">Global</div>

        <a href="__MACRO_PATH__" class="card">
          <div>
            <div class="card-header">
              <span class="card-title">Macro Economies &amp; GSCPI</span>
              <span class="badge __MACRO_STATUS_CLASS__">
                __MACRO_STATUS__
              </span>
            </div>

            <div class="kpi-group">
              <div class="kpi-item">
                <div class="kpi-label">Supply Chain Pressure</div>
                <div class="kpi-val highlight">__MACRO_GSCPI__</div>
              </div>

              <div class="kpi-item">
                <div class="kpi-label">Macro Coverage</div>
                <div class="kpi-val small-value">
                  WorldBank GDP / CPI
                </div>
              </div>
            </div>
          </div>

          <div class="card-footer">
            <span>Date: __MACRO_DATE__</span>
            <span class="arrow">OPEN &rarr;</span>
          </div>
        </a>
      </section>
    </div>
  </div>

  <footer>
    <div>
      MARKET INTELLIGENCE PLATFORM // LOGISTICS &amp; MACRO DASHBOARDS
    </div>
    <div>TERMINAL FEED: ACTIVE</div>
  </footer>
</body>
</html>
"""

# =========================================================
# 5. 템플릿 치환 유틸리티
# =========================================================
def replace_template_value(html_output, key, value):
    """템플릿의 단일 Placeholder를 안전한 HTML 문자열로 치환한다."""
    return html_output.replace(key, safe_html(value))

# =========================================================
# 6. 메인 빌드 로직
# =========================================================
def main():
    logger.info("Building Market Intelligence Portal Dashboard...")

    shipping = get_shipping_summary()
    alphaliner = get_alphaliner_summary()
    bunker = get_bunker_summary()
    forwarder = get_forwarder_summary()
    tac = get_tac_summary()
    macro = get_macro_summary()

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    shipping_latest_date = get_latest_available_date(
        shipping["scfi_date"],
        shipping["bdi_date"],
    )

    html_output = HTML_TEMPLATE

    # =====================================================
    # Header
    # =====================================================
    html_output = replace_template_value(
        html_output,
        "__GENERATED_AT__",
        now_str,
    )

    # =====================================================
    # Shipping
    # =====================================================
    html_output = replace_template_value(
        html_output,
        "__SHIPPING_PATH__",
        shipping["path"],
    )
    html_output = replace_template_value(
        html_output,
        "__SHIPPING_STATUS__",
        shipping["status"],
    )
    html_output = html_output.replace(
        "__SHIPPING_STATUS_CLASS__",
        get_status_class(shipping["status"]),
    )
    html_output = replace_template_value(
        html_output,
        "__SHIPPING_SCFI__",
        shipping["scfi_latest"],
    )
    html_output = replace_template_value(
        html_output,
        "__SHIPPING_BDI__",
        shipping["bdi_latest"],
    )
    html_output = replace_template_value(
        html_output,
        "__SHIPPING_DATE__",
        shipping_latest_date,
    )

    # =====================================================
    # Alphaliner
    # =====================================================
    html_output = replace_template_value(
        html_output,
        "__ALPHALINER_PATH__",
        alphaliner["path"],
    )
    html_output = replace_template_value(
        html_output,
        "__ALPHALINER_STATUS__",
        alphaliner["status"],
    )
    html_output = html_output.replace(
        "__ALPHALINER_STATUS_CLASS__",
        get_status_class(alphaliner["status"]),
    )
    html_output = replace_template_value(
        html_output,
        "__ALPHALINER_TEU__",
        alphaliner["total_teu"],
    )
    html_output = replace_template_value(
        html_output,
        "__ALPHALINER_TOP1__",
        alphaliner["top1_operator"],
    )
    html_output = replace_template_value(
        html_output,
        "__ALPHALINER_DATE__",
        alphaliner["latest_date"],
    )

    # =====================================================
    # Bunker Price
    # =====================================================
    html_output = replace_template_value(
        html_output,
        "__BUNKER_PATH__",
        bunker["path"],
    )
    html_output = replace_template_value(
        html_output,
        "__BUNKER_STATUS__",
        bunker["status"],
    )
    html_output = html_output.replace(
        "__BUNKER_STATUS_CLASS__",
        get_status_class(bunker["status"]),
    )
    html_output = replace_template_value(
        html_output,
        "__BUNKER_SINGAPORE_VLSFO__",
        bunker["singapore_vlsfo"],
    )
    html_output = replace_template_value(
        html_output,
        "__BUNKER_ROTTERDAM_VLSFO__",
        bunker["rotterdam_vlsfo"],
    )
    html_output = replace_template_value(
        html_output,
        "__BUNKER_HOUSTON_VLSFO__",
        bunker["houston_vlsfo"],
    )
    html_output = replace_template_value(
        html_output,
        "__BUNKER_VLSFO__",
        bunker["vlsfo"],
    )
    html_output = replace_template_value(
        html_output,
        "__BUNKER_LSMGO__",
        bunker["lsmgo"],
    )
    html_output = replace_template_value(
        html_output,
        "__BUNKER_IFO380__",
        bunker["ifo380"],
    )
    html_output = replace_template_value(
        html_output,
        "__BUNKER_DATE__",
        bunker["latest_date"],
    )

    # =====================================================
    # Forwarder
    # =====================================================
    html_output = replace_template_value(
        html_output,
        "__FORWARDER_PATH__",
        forwarder["path"],
    )
    html_output = replace_template_value(
        html_output,
        "__FORWARDER_STATUS__",
        forwarder["status"],
    )
    html_output = html_output.replace(
        "__FORWARDER_STATUS_CLASS__",
        get_status_class(forwarder["status"]),
    )
    html_output = replace_template_value(
        html_output,
        "__FORWARDER_PROVIDER__",
        forwarder["provider"],
    )
    html_output = replace_template_value(
        html_output,
        "__FORWARDER_REVENUE__",
        forwarder["revenue"],
    )
    html_output = replace_template_value(
        html_output,
        "__FORWARDER_TEU__",
        forwarder["teu"],
    )
    html_output = replace_template_value(
        html_output,
        "__FORWARDER_AIR_TONS__",
        forwarder["air_tons"],
    )
    html_output = replace_template_value(
        html_output,
        "__FORWARDER_DATE__",
        forwarder["latest_date"],
    )

    # =====================================================
    # TAC Index
    # =====================================================
    html_output = replace_template_value(
        html_output,
        "__TAC_PATH__",
        tac["path"],
    )
    html_output = replace_template_value(
        html_output,
        "__TAC_STATUS__",
        tac["status"],
    )
    html_output = html_output.replace(
        "__TAC_STATUS_CLASS__",
        get_status_class(tac["status"]),
    )
    html_output = replace_template_value(
        html_output,
        "__TAC_INDEX__",
        tac["overall_index"],
    )
    html_output = replace_template_value(
        html_output,
        "__TAC_WEEKLY__",
        tac["weekly_change"],
    )
    html_output = replace_template_value(
        html_output,
        "__TAC_DATE__",
        tac["latest_date"],
    )
    html_output = replace_template_value(
        html_output,
        "__TAC_ROUTES__",
        tac["route_count"],
    )

    # =====================================================
    # Macro
    # =====================================================
    html_output = replace_template_value(
        html_output,
        "__MACRO_PATH__",
        macro["path"],
    )
    html_output = replace_template_value(
        html_output,
        "__MACRO_STATUS__",
        macro["status"],
    )
    html_output = html_output.replace(
        "__MACRO_STATUS_CLASS__",
        get_status_class(macro["status"]),
    )
    html_output = replace_template_value(
        html_output,
        "__MACRO_GSCPI__",
        macro["gscpi_latest"],
    )
    html_output = replace_template_value(
        html_output,
        "__MACRO_DATE__",
        macro["latest_date"],
    )

    # =====================================================
    # 미치환 Placeholder 점검
    # =====================================================
    unresolved_placeholders = []

    for token in [
        "__SHIPPING_",
        "__ALPHALINER_",
        "__BUNKER_",
        "__FORWARDER_",
        "__TAC_",
        "__MACRO_",
        "__GENERATED_",
    ]:
        if token in html_output:
            unresolved_placeholders.append(token)

    if unresolved_placeholders:
        logger.warning(
            "Unresolved template placeholders found: "
            + ", ".join(unresolved_placeholders)
        )

    # =====================================================
    # index.html 원자적 저장
    # =====================================================
    temp_path = OUTPUT_HTML.with_suffix(".tmp")

    temp_path.write_text(
        html_output,
        encoding="utf-8",
    )

    temp_path.replace(OUTPUT_HTML)

    # =====================================================
    # JSON 요약 저장
    # =====================================================
    summary_data = {
        "generated_at": now_str,
        "shipping": shipping,
        "alphaliner": alphaliner,
        "bunker": bunker,
        "forwarder": forwarder,
        "tac": tac,
        "macro": macro,
    }

    OUTPUT_JSON.write_text(
        json.dumps(
            summary_data,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    logger.info(f"[SUCCESS] Portal HTML built: {OUTPUT_HTML}")
    logger.info(f"[SUCCESS] Summary JSON built: {OUTPUT_JSON}")

    logger.info(
        "Module status | "
        f"Shipping={shipping['status']} | "
        f"Alphaliner={alphaliner['status']} | "
        f"Bunker={bunker['status']} | "
        f"Forwarder={forwarder['status']} | "
        f"TAC={tac['status']} | "
        f"Macro={macro['status']}"
    )

if __name__ == "__main__":
    main()
