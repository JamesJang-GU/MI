# -*- coding: utf-8 -*-
"""
Market Intelligence Portal - Main Dashboard Generator

통합 Market Intelligence Portal의 루트 index.html 및 summary json을 생성한다.

연결 모듈:
- Sea
  - Shipping Index (SCFI / BDI with WoW Delta ↗/↘)
  - Alphaliner TOP 100 (Total Ships, Total TEU, Top 10 M/S with units)
  - Bunker Price (Rotterdam, Fujairah, Singapore VLSFO with Delta ↗/↘)
  - Crude Oil (Dubai, Brent, WTI with Delta ↗/↘)
  - Global Top 25 Forwarders
- Air
  - TAC Air Freight Index
    * Overall Index & Weekly Delta ↗/↘
    * Frankfurt-North America (Latest Rate & MoM Delta ↗/↘)
    * Hong Kong-Europe (Latest Rate & MoM Delta ↗/↘)
    * Hong Kong-North America (Latest Rate & MoM Delta ↗/↘)
- Global
  - Macro Economies & GSCPI
"""

import html
import json
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# =============================================================================
# 1. Configuration
# =============================================================================

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

# =============================================================================
# 2. Common Utilities & Safety Checks
# =============================================================================

def validate_environment():
    """
    루트 포털 생성기가 하위 모듈 폴더 내에서 오실행되어
    개별 모듈의 index.html을 덮어쓰는 사고를 원천 방지한다.
    """
    forbidden_module_names = {
        "tac index",
        "shipping index",
        "alphaliner",
        "bunkerprice",
        "forwarder",
        "gdp&inflation",
    }

    if BASE_DIR.name.lower() in forbidden_module_names:
        error_msg = (
            f"[CRITICAL ERROR] dashboard_generator.py is located inside a submodule folder: {BASE_DIR}. "
            "It must be placed at the repository root to prevent overwriting module-specific index.html files."
        )
        logger.error(error_msg)
        raise RuntimeError(error_msg)

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

def quote_identifier(identifier):
    """SQLite 식별자(테이블명/컬럼명)를 안전하게 감싼다."""
    return '"' + str(identifier).replace('"', '""') + '"'

def get_table_columns(cursor, table_name):
    """테이블의 실제 컬럼명을 lower-case key 기준 Dictionary로 반환한다."""
    rows = cursor.execute(
        f"PRAGMA table_info({quote_identifier(table_name)})"
    ).fetchall()
    return {str(row[1]).lower(): str(row[1]) for row in rows}

def find_first_column(columns, candidates):
    """후보 컬럼명 중 실제 존재하는 첫 번째 컬럼을 반환한다."""
    for candidate in candidates:
        actual_column = columns.get(candidate.lower())
        if actual_column:
            return actual_column
    return None

def safe_html(value):
    """HTML 삽입 시 특수문자를 이스케이프한다."""
    if value is None:
        return "-"
    return html.escape(str(value))

def replace_template_value(html_output, placeholder, value):
    """HTML Placeholder를 안전한 문자열로 대체한다."""
    return html_output.replace(placeholder, safe_html(value))

def get_status_class(status):
    """모듈 상태에 따른 CSS 클래스를 반환한다."""
    return "active" if status == "Active" else "nodata"

def get_latest_available_date(*dates):
    """유효 날짜 문자열 중 가장 최신 값을 반환한다."""
    valid_dates = [
        str(date)[:10]
        for date in dates
        if date and str(date).strip() != "-"
    ]
    return max(valid_dates) if valid_dates else "-"

def format_compact_number(value, suffix=""):
    """큰 숫자를 K / M / B 단위로 축약한다."""
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
    """가격 표시 형식."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    return f"{number:,.2f}{suffix}"

def normalize_port_name(value):
    """수집 데이터의 항만명을 포털 표준 명칭으로 정규화한다."""
    original_value = str(value or "").strip()
    normalized = original_value.upper()

    mapping = {
        "ROTTERDAM": "Rotterdam",
        "ROTTERDAM PORT": "Rotterdam",
        "RDM": "Rotterdam",
        "FUJAIRAH": "Fujairah",
        "FUJAIRAH PORT": "Fujairah",
        "FJR": "Fujairah",
        "SINGAPORE": "Singapore",
        "SINGAPORE PORT": "Singapore",
        "SIN": "Singapore",
    }
    return mapping.get(normalized, original_value)

def calculate_delta_display(current_value, previous_value, decimals=2, prefix=""):
    """
    최신값과 직전값을 비교해 HTML 증감 표시를 반환한다.
    반환 예: <span class="delta-up">WoW +12.50 (+1.62%) ↗</span>
    """
    if current_value is None or previous_value is None:
        return '<span class="delta-neutral">-</span>'

    try:
        cur = float(current_value)
        prv = float(previous_value)
    except (TypeError, ValueError):
        return '<span class="delta-neutral">-</span>'

    diff = cur - prv
    pct = (diff / prv * 100.0) if prv != 0 else 0.0

    if diff > 0:
        cls = "delta-up"
        arrow = "↗"
        diff_str = f"+{diff:,.{decimals}f}"
        pct_str = f"+{pct:.2f}%"
    elif diff < 0:
        cls = "delta-down"
        arrow = "↘"
        diff_str = f"{diff:,.{decimals}f}"
        pct_str = f"{pct:.2f}%"
    else:
        cls = "delta-neutral"
        arrow = "→"
        diff_str = f"{diff:,.{decimals}f}"
        pct_str = "0.00%"

    prefix_str = f"{prefix} " if prefix else ""
    return f'<span class="{cls}">{prefix_str}{diff_str} ({pct_str}) {arrow}</span>'

# =============================================================================
# 3. Shipping Index Summary
# =============================================================================

def get_shipping_summary():
    """Shipping Index (SCFI / BDI) 최신 요약 및 전주 대비 증감 데이터를 읽는다."""
    summary = {
        "title": "Shipping Index (SCFI / BDI)",
        "path": "Shipping Index/index.html",
        "category": "Sea",
        "scfi_latest": "-",
        "scfi_date": "-",
        "scfi_delta_html": '<span class="delta-neutral">-</span>',
        "bdi_latest": "-",
        "bdi_date": "-",
        "bdi_delta_html": '<span class="delta-neutral">-</span>',
        "status": "No Data",
    }

    db_path = DB_PATHS["shipping"]
    if not db_path.exists():
        return summary

    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            cursor = conn.cursor()
            if not table_exists(cursor, "index_raw"):
                return summary

            scfi_rows = cursor.execute(
                """
                SELECT dt, value
                FROM index_raw
                WHERE index_name = 'SCFI'
                  AND UPPER(TRIM(region)) IN (
                      'SCFI', 'COMPOSITE', 'COMPOSITE INDEX',
                      'SHANGHAI CONTAINERIZED FREIGHT INDEX', '종합', '종합지수'
                  )
                ORDER BY dt DESC
                LIMIT 2
                """
            ).fetchall()

            bdi_rows = cursor.execute(
                """
                SELECT dt, value
                FROM index_raw
                WHERE index_name = 'BDI'
                  AND UPPER(TRIM(region)) = 'BDI'
                ORDER BY dt DESC
                LIMIT 2
                """
            ).fetchall()

        if scfi_rows:
            latest = scfi_rows[0]
            summary["scfi_date"] = str(latest[0] or "-")[:10]
            summary["scfi_latest"] = f"{float(latest[1]):,.1f}" if latest[1] is not None else "-"
            prev_val = scfi_rows[1][1] if len(scfi_rows) > 1 else None
            summary["scfi_delta_html"] = calculate_delta_display(latest[1], prev_val, decimals=1, prefix="WoW")

        if bdi_rows:
            latest = bdi_rows[0]
            summary["bdi_date"] = str(latest[0] or "-")[:10]
            summary["bdi_latest"] = f"{float(latest[1]):,.0f}" if latest[1] is not None else "-"
            prev_val = bdi_rows[1][1] if len(bdi_rows) > 1 else None
            summary["bdi_delta_html"] = calculate_delta_display(latest[1], prev_val, decimals=0, prefix="WoW")

        if scfi_rows or bdi_rows:
            summary["status"] = "Active"

    except Exception as error:
        logger.exception(f"Failed to read Shipping Index DB: {error}")

    return summary

# =============================================================================
# 4. Alphaliner Summary
# =============================================================================

def get_alphaliner_summary():
    """Alphaliner TOP 100 최신 Snapshot 및 Top 10 M/S 합계를 읽는다."""
    summary = {
        "title": "Alphaliner TOP 100",
        "path": "Alphaliner/index.html",
        "category": "Sea",
        "latest_date": "-",
        "total_ships": "-",
        "total_teu": "-",
        "top10_share": "-",
        "top1_operator": "-",
        "status": "No Data",
    }

    db_path = DB_PATHS["alphaliner"]
    if not db_path.exists():
        return summary

    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            cursor = conn.cursor()
            if not table_exists(cursor, "alphaliner_master") or not table_exists(cursor, "alphaliner_detail"):
                return summary

            master_row = cursor.execute(
                """
                SELECT snapshot_id, data_date, total_teu, total_ships
                FROM alphaliner_master
                ORDER BY data_date DESC, snapshot_id DESC
                LIMIT 1
                """
            ).fetchone()

            if not master_row:
                return summary

            snapshot_id, data_date, total_teu, total_ships = master_row
            summary["latest_date"] = str(data_date or "-")[:10]
            summary["total_teu"] = f"{int(total_teu):,} TEU" if total_teu is not None else "-"
            summary["total_ships"] = f"{int(total_ships):,} Ships" if total_ships is not None else "-"

            top10_row = cursor.execute(
                """
                SELECT SUM(market_share)
                FROM alphaliner_detail
                WHERE snapshot_id = ?
                  AND rank <= 10
                """,
                (snapshot_id,),
            ).fetchone()

            if top10_row and top10_row[0] is not None:
                summary["top10_share"] = f"{float(top10_row[0]):.2f}%"

            top_operator_row = cursor.execute(
                """
                SELECT operator_name, market_share
                FROM alphaliner_detail
                WHERE snapshot_id = ?
                  AND rank = 1
                LIMIT 1
                """,
                (snapshot_id,),
            ).fetchone()

            if top_operator_row:
                operator_name, market_share = top_operator_row
                if market_share is not None:
                    summary["top1_operator"] = f"{operator_name} ({float(market_share):.1f}%)"
                else:
                    summary["top1_operator"] = str(operator_name or "-")

            summary["status"] = "Active"

    except Exception as error:
        logger.exception(f"Failed to read Alphaliner DB: {error}")

    return summary

# =============================================================================
# 5. Bunker Price Summary
# =============================================================================

def get_bunker_summary():
    """Ship & Bunker VLSFO 최신 가격 및 직전일 대비 증감을 읽는다."""
    summary = {
        "title": "Bunker Price",
        "path": "BunkerPrice/index.html",
        "category": "Sea",
        "latest_date": "-",
        "rotterdam": "-",
        "rotterdam_delta_html": '<span class="delta-neutral">-</span>',
        "fujairah": "-",
        "fujairah_delta_html": '<span class="delta-neutral">-</span>',
        "singapore": "-",
        "singapore_delta_html": '<span class="delta-neutral">-</span>',
        "status": "No Data",
    }

    db_path = DB_PATHS["bunker"]
    if not db_path.exists():
        return summary

    target_ports = ["Rotterdam", "Fujairah", "Singapore"]

    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            cursor = conn.cursor()
            if not table_exists(cursor, "product_vlsfo"):
                return summary

            columns = get_table_columns(cursor, "product_vlsfo")
            port_column = find_first_column(columns, ["port"])
            price_column = find_first_column(columns, ["price"])
            date_column = find_first_column(columns, ["date_str", "date"])
            timestamp_column = find_first_column(columns, ["timestamp_ms"])

            if not port_column or not price_column or not date_column:
                return summary

            port_latest_dates = {}
            port_latest_prices = {}

            for target_port in target_ports:
                order_by = [f"{quote_identifier(date_column)} DESC"]
                if timestamp_column:
                    order_by.append(f"{quote_identifier(timestamp_column)} DESC")

                rows = cursor.execute(
                    f"""
                    SELECT
                        {quote_identifier(date_column)} AS data_date,
                        {quote_identifier(price_column)} AS price
                    FROM {quote_identifier("product_vlsfo")}
                    WHERE UPPER(TRIM({quote_identifier(port_column)})) = UPPER(TRIM(?))
                    ORDER BY {", ".join(order_by)}
                    """,
                    (target_port,),
                ).fetchall()

                if not rows:
                    continue

                latest_date = None
                latest_price = None
                previous_date = None
                previous_price = None

                for data_date, price in rows:
                    try:
                        price = float(price)
                    except (TypeError, ValueError):
                        continue

                    norm_date = str(data_date or "")[:10]
                    if not norm_date:
                        continue

                    if latest_date is None:
                        latest_date = norm_date
                        latest_price = price
                        continue

                    if norm_date != latest_date:
                        previous_date = norm_date
                        previous_price = price
                        break

                if latest_price is None:
                    continue

                normalized_port = normalize_port_name(target_port)
                port_latest_prices[normalized_port] = latest_price
                port_latest_dates[normalized_port] = latest_date

                delta_html = calculate_delta_display(latest_price, previous_price, decimals=2)

                if normalized_port == "Rotterdam":
                    summary["rotterdam"] = format_price(latest_price)
                    summary["rotterdam_delta_html"] = delta_html
                elif normalized_port == "Fujairah":
                    summary["fujairah"] = format_price(latest_price)
                    summary["fujairah_delta_html"] = delta_html
                elif normalized_port == "Singapore":
                    summary["singapore"] = format_price(latest_price)
                    summary["singapore_delta_html"] = delta_html

            if port_latest_dates:
                summary["latest_date"] = max(port_latest_dates.values())

            if port_latest_prices:
                summary["status"] = "Active"

    except Exception as error:
        logger.exception(f"Failed to read Bunker DB: {error}")

    return summary

# =============================================================================
# 6. Crude Oil Summary
# =============================================================================

def get_oil_summary():
    """Opinet 국제 원유 가격 최신값 및 직전일 대비 증감을 읽는다."""
    summary = {
        "title": "Crude Oil",
        "path": "BunkerPrice/OilPrice/index.html",
        "category": "Sea",
        "latest_date": "-",
        "dubai": "-",
        "dubai_delta_html": '<span class="delta-neutral">-</span>',
        "brent": "-",
        "brent_delta_html": '<span class="delta-neutral">-</span>',
        "wti": "-",
        "wti_delta_html": '<span class="delta-neutral">-</span>',
        "status": "No Data",
    }

    db_path = DB_PATHS["bunker"]
    if not db_path.exists():
        return summary

    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            cursor = conn.cursor()
            if not table_exists(cursor, "oil_prices"):
                return summary

            columns = get_table_columns(cursor, "oil_prices")
            date_col = find_first_column(columns, ["date", "DATE"])
            dubai_col = find_first_column(columns, ["dubai", "Dubai"])
            brent_col = find_first_column(columns, ["brent", "Brent"])
            wti_col = find_first_column(columns, ["wti", "WTI"])

            if not all([date_col, dubai_col, brent_col, wti_col]):
                return summary

            rows = cursor.execute(
                f"""
                SELECT
                    {quote_identifier(date_col)} AS data_date,
                    {quote_identifier(dubai_col)} AS dubai,
                    {quote_identifier(brent_col)} AS brent,
                    {quote_identifier(wti_col)} AS wti
                FROM {quote_identifier("oil_prices")}
                ORDER BY {quote_identifier(date_col)} DESC
                """
            ).fetchall()

        if not rows:
            return summary

        oil_mapping = {
            "Dubai": {"index": 1, "value_key": "dubai", "delta_key": "dubai_delta_html"},
            "Brent": {"index": 2, "value_key": "brent", "delta_key": "brent_delta_html"},
            "WTI": {"index": 3, "value_key": "wti", "delta_key": "wti_delta_html"},
        }

        latest_dates = []

        for oil_name, cfg in oil_mapping.items():
            latest_date = None
            latest_price = None
            previous_date = None
            previous_price = None

            for row in rows:
                data_date = str(row[0] or "")[:10]
                raw_price = row[cfg["index"]]

                try:
                    price = float(raw_price)
                except (TypeError, ValueError):
                    continue

                if not data_date:
                    continue

                if latest_date is None:
                    latest_date = data_date
                    latest_price = price
                    latest_dates.append(data_date)
                    continue

                if data_date != latest_date:
                    previous_date = data_date
                    previous_price = price
                    break

            if latest_price is not None:
                summary[cfg["value_key"]] = f"{latest_price:,.2f} USD/BBL"
                summary[cfg["delta_key"]] = calculate_delta_display(latest_price, previous_price, decimals=2)

        if latest_dates:
            summary["latest_date"] = max(latest_dates)
            summary["status"] = "Active"

    except Exception as error:
        logger.exception(f"Failed to read Oil DB: {error}")

    return summary

# =============================================================================
# 7. Forwarder Summary
# =============================================================================

def get_forwarder_summary():
    """A&A Global Top 25 Forwarders 최신 Snapshot을 읽는다."""
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
        return summary

    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            cursor = conn.cursor()
            if not table_exists(cursor, "forwarders"):
                return summary

            latest_date_row = cursor.execute("SELECT MAX(date) FROM forwarders").fetchone()
            latest_date = latest_date_row[0] if latest_date_row and latest_date_row[0] else None

            if not latest_date:
                return summary

            top_forwarder_row = cursor.execute(
                """
                SELECT rank, provider, revenue, teu, air_tons
                FROM forwarders
                WHERE date = ?
                ORDER BY rank ASC
                LIMIT 1
                """,
                (latest_date,),
            ).fetchone()

        if top_forwarder_row:
            rank, provider, revenue, teu, air_tons = top_forwarder_row
            summary["latest_date"] = str(latest_date)[:10]
            summary["rank"] = f"#{int(rank):02d}" if rank is not None else "-"
            summary["provider"] = str(provider or "-")
            summary["revenue"] = format_compact_number(revenue, " USD")
            summary["teu"] = format_compact_number(teu, " TEU")
            summary["air_tons"] = format_compact_number(air_tons, " Tons")
            summary["status"] = "Active"

    except Exception as error:
        logger.exception(f"Failed to read Forwarder DB: {error}")

    return summary

# =============================================================================
# 8. TAC Air Freight Summary
# =============================================================================

def normalize_tac_corridor_name(value):
    """
    TAC DB 내 Route / Corridor 표기를 포털 표준 명칭으로 통일한다.
    """
    raw_value = str(value or "").strip()

    normalized = (
        raw_value.upper()
        .replace("–", "-")
        .replace("—", "-")
        .replace("_", " ")
        .replace("/", " ")
        .replace("-", " ")
        .replace(" TO ", " ")
    )

    compact = "".join(char for char in normalized if char.isalnum())

    mapping = {
        # Frankfurt - North America
        "FRANKFURTNORTHAMERICA": "Frankfurt-North America",
        "FRANORTHAMERICA": "Frankfurt-North America",
        "FRAUS": "Frankfurt-North America",
        "FRAUSA": "Frankfurt-North America",
        "FRANKFURTUSA": "Frankfurt-North America",
        "FRANKFURTNORTHAMERICAINDEX": "Frankfurt-North America",

        # Hong Kong - Europe
        "HONGKONGEUROPE": "Hong Kong-Europe",
        "HKGEUROPE": "Hong Kong-Europe",
        "HKEUROPE": "Hong Kong-Europe",
        "HONGKONGEUROPEINDEX": "Hong Kong-Europe",

        # Hong Kong - North America
        "HONGKONGNORTHAMERICA": "Hong Kong-North America",
        "HKGNORTHAMERICA": "Hong Kong-North America",
        "HKNORTHAMERICA": "Hong Kong-North America",
        "HKGUS": "Hong Kong-North America",
        "HKGUSA": "Hong Kong-North America",
        "HONGKONGUSA": "Hong Kong-North America",
        "HONGKONGNORTHAMERICAINDEX": "Hong Kong-North America",
    }

    return mapping.get(compact, raw_value)

def normalize_tac_period(value):
    """
    TAC 기간 값을 비교 가능한 월 기준 형식으로 정규화한다.

    지원 예시:
    - 2026-09-01
    - 2026-09
    - 20260901
    - 2026/09/01
    - Sep-26
    - Sep 2026
    - September-2026

    반환:
    {
        "date": "YYYY-MM-01",
        "month": "YYYY-MM",
        "sort_key": 202609
    }

    변환 불가 시 None 반환.
    """
    raw_value = str(value or "").strip()

    if not raw_value:
        return None

    normalized = (
        raw_value
        .replace("/", "-")
        .replace(".", "-")
        .replace("_", "-")
    )

    formats = [
        "%Y-%m-%d",
        "%Y-%m",
        "%Y%m%d",
        "%Y%m",
        "%d-%m-%Y",
        "%d-%b-%Y",
        "%d-%b-%y",
        "%b-%y",
        "%b-%Y",
        "%b %y",
        "%b %Y",
        "%B-%Y",
        "%B %Y",
    ]

    parsed_date = None

    for date_format in formats:
        try:
            parsed_date = datetime.strptime(normalized, date_format)
            break
        except ValueError:
            continue

    if parsed_date is None:
        # datetime 문자열 등 YYYY-MM-DD로 시작하는 데이터 대응
        try:
            parsed_date = datetime.strptime(normalized[:10], "%Y-%m-%d")
        except ValueError:
            return None

    return {
        "date": parsed_date.strftime("%Y-%m-01"),
        "month": parsed_date.strftime("%Y-%m"),
        "sort_key": parsed_date.year * 100 + parsed_date.month,
    }

def calculate_rate_delta_display(current_value, previous_value, decimals=2):
    """
    최신 항공운임과 직전 월 운임을 비교하여
    절대 증감, 증감률, 방향 화살표를 HTML 형태로 반환한다.
    """
    if current_value is None or previous_value is None:
        return '<span class="delta-neutral">-</span>'

    try:
        current = float(current_value)
        previous = float(previous_value)
    except (TypeError, ValueError):
        return '<span class="delta-neutral">-</span>'

    difference = current - previous
    percentage = (difference / previous * 100.0) if previous != 0 else 0.0

    if difference > 0:
        return (
            f'<span class="delta-up">'
            f'+{difference:,.{decimals}f} (+{percentage:.2f}%) ↗'
            f'</span>'
        )

    if difference < 0:
        return (
            f'<span class="delta-down">'
            f'{difference:,.{decimals}f} ({percentage:.2f}%) ↘'
            f'</span>'
        )

    return (
        f'<span class="delta-neutral">'
        f'±0.00 (±0.00%) →'
        f'</span>'
    )

def get_sqlite_table_names(cursor):
    """현재 SQLite DB에 존재하는 일반 테이블 목록을 반환한다."""
    rows = cursor.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()

    return [row[0] for row in rows]

def find_tac_route_table(cursor):
    """
    TAC Route 시계열 데이터 테이블을 탐색한다.

    핵심 변경:
    - period / month / report_period 등 실제 운임 기준기간 컬럼을
      data_date보다 우선한다.
    - data_date는 저장·수집일일 수 있어 시계열 비교 기준으로 부적합할 수 있다.
    """
    table_names = get_sqlite_table_names(cursor)

    preferred_table_names = [
        "tac_detail",
        "tac_details",
        "tac_routes",
        "tac_route_data",
        "tac_corridors",
        "tac_rates",
        "route_rates",
        "airfreight_rates",
        "tac_data",
    ]

    candidates = []

    for table_name in table_names:
        if table_name.lower() == "tac_master":
            continue

        columns = get_table_columns(cursor, table_name)

        rate_col = find_first_column(
            columns,
            [
                "latest_rate",
                "rate",
                "rate_value",
                "price",
                "price_usd",
                "usd_per_kg",
                "value",
                "index_value",
            ],
        )

        corridor_col = find_first_column(
            columns,
            [
                "route_code",
                "corridor",
                "corridor_name",
                "route",
                "route_name",
                "lane",
                "lane_name",
                "market",
                "market_name",
            ],
        )

        origin_col = find_first_column(
            columns,
            [
                "origin",
                "origin_city",
                "origin_name",
                "from_city",
                "from_location",
                "departure",
            ],
        )

        destination_col = find_first_column(
            columns,
            [
                "destination",
                "destination_city",
                "destination_name",
                "to_city",
                "to_location",
                "arrival",
            ],
        )

        date_col = find_first_column(
            columns,
            [
                "period",
                "report_period",
                "rate_period",
                "month",
                "rate_month",
                "report_month",
                "period_month",
                "observation_date",
                "rate_date",
                "report_date",
                "snapshot_date",
                "data_date",
                "date",
            ],
        )

        snapshot_col = find_first_column(
            columns,
            [
                "snapshot_id",
                "master_id",
                "tac_master_id",
            ],
        )

        has_route_identifier = bool(corridor_col or (origin_col and destination_col))
        has_date_source = bool(date_col or snapshot_col)

        if not rate_col or not has_route_identifier or not has_date_source:
            continue

        score = 0

        if table_name.lower() in preferred_table_names:
            score += 100

        if "tac" in table_name.lower():
            score += 20

        if "route" in table_name.lower() or "corridor" in table_name.lower():
            score += 10

        if corridor_col:
            score += 5

        # 실제 시계열 기간 컬럼이면 강하게 우선
        if date_col and date_col.lower() in {
            "period",
            "report_period",
            "rate_period",
            "month",
            "rate_month",
            "report_month",
            "period_month",
        }:
            score += 30
        elif date_col:
            score += 5

        candidates.append(
            {
                "table_name": table_name,
                "columns": columns,
                "rate_col": rate_col,
                "corridor_col": corridor_col,
                "origin_col": origin_col,
                "destination_col": destination_col,
                "date_col": date_col,
                "snapshot_col": snapshot_col,
                "score": score,
            }
        )

    if not candidates:
        return None

    candidates.sort(key=lambda item: item["score"], reverse=True)
    return candidates[0]

def get_tac_route_rows(cursor):
    """
    TAC Route별 시계열 데이터를 추출한다.

    반환 형식:
    [
        (period_value, corridor_name, rate),
        ...
    ]
    """
    route_table_info = find_tac_route_table(cursor)

    if not route_table_info:
        logger.warning(
            "TAC Route 데이터 테이블을 찾지 못했습니다. tables=%s",
            get_sqlite_table_names(cursor),
        )
        return []

    table_name = route_table_info["table_name"]
    rate_col = route_table_info["rate_col"]
    corridor_col = route_table_info["corridor_col"]
    origin_col = route_table_info["origin_col"]
    destination_col = route_table_info["destination_col"]
    date_col = route_table_info["date_col"]
    snapshot_col = route_table_info["snapshot_col"]

    logger.info(
        "TAC source selected | table=%s | rate=%s | corridor=%s | "
        "origin=%s | destination=%s | period=%s | snapshot=%s",
        table_name,
        rate_col,
        corridor_col,
        origin_col,
        destination_col,
        date_col,
        snapshot_col,
    )

    if corridor_col:
        corridor_expression = f"detail.{quote_identifier(corridor_col)}"
    else:
        corridor_expression = (
            f"TRIM(COALESCE(detail.{quote_identifier(origin_col)}, '')) "
            f"|| '-' || "
            f"TRIM(COALESCE(detail.{quote_identifier(destination_col)}, ''))"
        )

    # 상세 테이블에 period/month/date가 있으면 해당 값이 최우선이다.
    if date_col:
        query = f"""
            SELECT
                detail.{quote_identifier(date_col)} AS period_value,
                {corridor_expression} AS corridor_name,
                detail.{quote_identifier(rate_col)} AS rate
            FROM {quote_identifier(table_name)} AS detail
            WHERE detail.{quote_identifier(rate_col)} IS NOT NULL
              AND TRIM(COALESCE(detail.{quote_identifier(date_col)}, '')) <> ''
        """

        rows = cursor.execute(query).fetchall()

        logger.info(
            "TAC rows loaded from direct period column | table=%s | rows=%s",
            table_name,
            len(rows),
        )

        return rows

    # period/date 컬럼이 없고 snapshot_id만 있을 때만 tac_master 기준일을 사용한다.
    if not snapshot_col or not table_exists(cursor, "tac_master"):
        logger.warning(
            "TAC period column 및 tac_master 연결을 모두 사용할 수 없습니다. table=%s",
            table_name,
        )
        return []

    master_columns = get_table_columns(cursor, "tac_master")

    master_snapshot_col = find_first_column(
        master_columns,
        ["snapshot_id", "id"],
    )

    master_date_col = find_first_column(
        master_columns,
        [
            "period",
            "report_period",
            "month",
            "data_date",
            "snapshot_date",
            "date",
        ],
    )

    if not master_snapshot_col or not master_date_col:
        logger.warning("tac_master의 snapshot/date 컬럼을 찾지 못했습니다.")
        return []

    query = f"""
        SELECT
            master.{quote_identifier(master_date_col)} AS period_value,
            {corridor_expression} AS corridor_name,
            detail.{quote_identifier(rate_col)} AS rate
        FROM {quote_identifier(table_name)} AS detail
        INNER JOIN {quote_identifier("tac_master")} AS master
            ON detail.{quote_identifier(snapshot_col)}
             = master.{quote_identifier(master_snapshot_col)}
        WHERE detail.{quote_identifier(rate_col)} IS NOT NULL
    """

    rows = cursor.execute(query).fetchall()

    logger.info(
        "TAC rows loaded through tac_master | table=%s | rows=%s",
        table_name,
        len(rows),
    )

    return rows

def get_tac_summary():
    """
    루트 포털 Air 카드용 TAC Route 요약 데이터를 반환한다.

    비교 기준:
    - 최신 월의 Rate
    - 직전 월의 Rate
    """
    target_corridors = [
        "Frankfurt-North America",
        "Hong Kong-Europe",
        "Hong Kong-North America",
    ]

    summary = {
        "title": "Air Freight (TAC Index)",
        "path": "TAC Index/index.html",
        "category": "Air",
        "latest_date": "-",

        "frankfurt_north_america_rate": "-",
        "frankfurt_north_america_mom_html": (
            '<span class="delta-neutral">-</span>'
        ),

        "hong_kong_europe_rate": "-",
        "hong_kong_europe_mom_html": (
            '<span class="delta-neutral">-</span>'
        ),

        "hong_kong_north_america_rate": "-",
        "hong_kong_north_america_mom_html": (
            '<span class="delta-neutral">-</span>'
        ),

        "status": "No Data",
    }

    db_path = DB_PATHS["tac"]

    if not db_path.exists():
        logger.warning("TAC DB가 존재하지 않습니다: %s", db_path)
        return summary

    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            cursor = conn.cursor()
            corridor_rows = get_tac_route_rows(cursor)

        if not corridor_rows:
            logger.warning("TAC Route 시계열 조회 결과가 없습니다.")
            return summary

        corridor_history = {
            corridor: []
            for corridor in target_corridors
        }

        for raw_period, raw_corridor, raw_rate in corridor_rows:
            normalized_corridor = normalize_tac_corridor_name(raw_corridor)

            if normalized_corridor not in corridor_history:
                continue

            try:
                rate = float(raw_rate)
            except (TypeError, ValueError):
                continue

            period_info = normalize_tac_period(raw_period)

            if not period_info:
                logger.warning(
                    "TAC 기간 값을 해석하지 못했습니다 | corridor=%s | raw_period=%s",
                    normalized_corridor,
                    raw_period,
                )
                continue

            corridor_history[normalized_corridor].append(
                {
                    "date": period_info["date"],
                    "month": period_info["month"],
                    "sort_key": period_info["sort_key"],
                    "rate": rate,
                }
            )

        summary_mapping = {
            "Frankfurt-North America": {
                "rate_key": "frankfurt_north_america_rate",
                "mom_key": "frankfurt_north_america_mom_html",
            },
            "Hong Kong-Europe": {
                "rate_key": "hong_kong_europe_rate",
                "mom_key": "hong_kong_europe_mom_html",
            },
            "Hong Kong-North America": {
                "rate_key": "hong_kong_north_america_rate",
                "mom_key": "hong_kong_north_america_mom_html",
            },
        }

        latest_dates = []

        for corridor_name, rows in corridor_history.items():
            if not rows:
                logger.warning(
                    "TAC 대상 Route 데이터를 찾지 못했습니다: %s",
                    corridor_name,
                )
                continue

            # 동일 월 중복 데이터가 존재할 수 있으므로,
            # 월별로 1개만 보관한다.
            #
            # 데이터가 여러 개일 경우 마지막 행을 우선하되,
            # TAC 차트용 역사 데이터 구조에서는 일반적으로 월별 1행이다.
            rate_by_month = {}

            for row in rows:
                month_key = row["month"]

                if month_key not in rate_by_month:
                    rate_by_month[month_key] = row

            # 실제 연월 sort_key 기준 내림차순.
            # 문자열 기준 정렬이 아니라 Sep-26, Oct-25 등의 형식도 안전하게 처리한다.
            distinct_month_rows = sorted(
                rate_by_month.values(),
                key=lambda item: item["sort_key"],
                reverse=True,
            )

            latest_row = distinct_month_rows[0]

            previous_row = (
                distinct_month_rows[1]
                if len(distinct_month_rows) >= 2
                else None
            )

            config = summary_mapping[corridor_name]

            summary[config["rate_key"]] = (
                f"${latest_row['rate']:,.2f} USD/kg"
            )

            summary[config["mom_key"]] = calculate_rate_delta_display(
                latest_row["rate"],
                previous_row["rate"] if previous_row else None,
                decimals=2,
            )

            latest_dates.append(latest_row["date"])

            logger.info(
                "TAC corridor summary | corridor=%s | available_months=%s | "
                "latest=%s (%s) | previous=%s (%s)",
                corridor_name,
                [
                    (item["month"], item["rate"])
                    for item in distinct_month_rows[:5]
                ],
                latest_row["rate"],
                latest_row["month"],
                previous_row["rate"] if previous_row else None,
                previous_row["month"] if previous_row else None,
            )

        if latest_dates:
            summary["latest_date"] = max(latest_dates)
            summary["status"] = "Active"

        logger.info("TAC summary final: %s", summary)

    except Exception as error:
        logger.exception("Failed to read TAC DB: %s", error)

    return summary


# =============================================================================
# 9. Macro Summary
# =============================================================================

def get_macro_summary():
    """Macro Economies 및 New York Fed GSCPI 최신 데이터를 읽는다."""
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
        return summary

    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            cursor = conn.cursor()
            if not table_exists(cursor, "newyorkfed_gscpi"):
                return summary

            row = cursor.execute(
                """
                SELECT date, gscpi_value
                FROM newyorkfed_gscpi
                ORDER BY date DESC
                LIMIT 1
                """
            ).fetchone()

        if row:
            data_date, gscpi_value = row
            summary["latest_date"] = str(data_date or "-")[:10]
            summary["gscpi_latest"] = f"{float(gscpi_value):,.2f}" if gscpi_value is not None else "-"
            summary["status"] = "Active"

    except Exception as error:
        logger.exception(f"Failed to read Macro DB: {error}")

    return summary

# =============================================================================
# 10. HTML Template
# =============================================================================

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
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
      --red: #ff5252;
      --text: #e7edf4;
      --text-muted: #8798aa;
      --text-dim: #5d6d7d;
      --border: #243342;
      --border-light: #405064;
      --mono: "IBM Plex Mono", "Roboto Mono", Consolas, monospace;
      --sans: -apple-system, BlinkMacSystemFont, "Segoe UI",
              Roboto, "Noto Sans KR", Arial, sans-serif;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }

    html, body {
      min-height: 100%;
      background:
        linear-gradient(180deg, rgba(255, 138, 0, 0.04) 0%, rgba(2, 6, 11, 0) 260px),
        var(--bg-dark);
      color: var(--text);
      font-family: var(--sans);
      font-size: 12px;
    }

    body { padding: 10px 16px; }

    .container {
      width: 100%;
      max-width: 1600px;
      margin: 0 auto;
    }

    .top-bar {
      min-height: 36px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      padding: 0 10px;
      border: 1px solid var(--border-light);
      background: var(--bg-panel);
      font-family: var(--mono);
    }

    .top-left {
      display: flex;
      align-items: center;
      gap: 10px;
      min-width: 0;
    }

    .badge-term {
      padding: 3px 6px;
      background: var(--amber);
      color: #000;
      font-size: 10px;
      font-weight: 900;
      letter-spacing: 0.4px;
    }

    .title-main {
      overflow: hidden;
      color: #fff;
      font-size: 13px;
      font-weight: 800;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .updated-at {
      flex-shrink: 0;
      color: var(--text-dim);
      font-size: 10px;
    }
    .updated-at span { color: var(--amber-bright); }

    .section-group { margin-top: 12px; }

    .section-title {
      margin-bottom: 7px;
      color: var(--amber);
      font-family: var(--mono);
      font-size: 11px;
      font-weight: 800;
      letter-spacing: 0.8px;
      text-transform: uppercase;
    }
    .section-title::before {
      content: "// ";
      color: var(--text-dim);
    }

    /* Sea 영역: 5개 카드 배치 */
    .grid-sea {
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 10px;
    }

    .grid-2 {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }

    .card {
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      min-height: 185px;
      padding: 10px 12px;
      border: 1px solid var(--border);
      border-top: 2px solid var(--border-light);
      background:
        linear-gradient(135deg, rgba(255, 138, 0, 0.035), transparent 60%),
        var(--bg-panel);
      color: inherit;
      text-decoration: none;
      transition: 0.15s ease;
    }

    .card:hover {
      border-color: var(--amber);
      border-top-color: var(--amber);
      background: var(--bg-hover);
      transform: translateY(-2px);
    }

    .card-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 8px;
    }

    .card-title {
      overflow: hidden;
      color: #fff;
      font-family: var(--mono);
      font-size: 11px;
      font-weight: 800;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .badge {
      flex-shrink: 0;
      padding: 2px 5px;
      border: 1px solid var(--border);
      color: var(--text-dim);
      font-family: var(--mono);
      font-size: 9px;
      font-weight: 800;
    }
    .badge.active {
      border-color: var(--green);
      background: rgba(0, 210, 106, 0.08);
      color: var(--green);
    }
    .badge.nodata {
      background: rgba(93, 109, 125, 0.08);
      color: var(--text-dim);
    }

    .kpi-group {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 6px;
    }

    .kpi-item {
      min-width: 0;
      padding: 7px 8px;
      border: 1px solid rgba(64, 80, 100, 0.55);
      background: var(--bg-item);
    }

    .kpi-label {
      margin-bottom: 4px;
      overflow: hidden;
      color: var(--text-muted);
      font-family: var(--mono);
      font-size: 9px;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .kpi-val {
      overflow: hidden;
      color: #fff;
      font-family: var(--mono);
      font-size: 12px;
      font-weight: 800;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .kpi-val.highlight { color: var(--cyan-bright); }
    .kpi-val.amber { color: var(--amber-bright); }
    .small-value { font-size: 10px; }

    /* 3열 카드 전용 (Bunker / Crude Oil) */
    .section-sublabel {
      margin: 4px 0 6px;
      color: var(--amber);
      font-family: var(--mono);
      font-size: 8.5px;
      font-weight: 800;
      letter-spacing: 0.5px;
    }

    .grid-triple {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 5px;
    }
    .grid-triple .kpi-item { padding: 6px 5px; }
    .grid-triple .kpi-label { font-size: 8px; }
    .grid-triple .kpi-val { font-size: 10px; letter-spacing: -0.3px; }

    /* TAC Corridor 전용 그리드 레이아웃 */
    .tac-corridor-label {
      margin: 8px 0 6px;
      color: var(--amber);
      font-family: var(--mono);
      font-size: 8.5px;
      font-weight: 800;
      letter-spacing: 0.5px;
    }

    .tac-corridor-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 5px;
    }

    .tac-corridor-grid .kpi-item {
      padding: 6px 6px;
    }

    .tac-corridor-grid .kpi-label {
      min-height: 22px;
      margin-bottom: 4px;
      overflow: visible;
      white-space: normal;
      font-size: 8px;
      line-height: 1.3;
    }

    .tac-corridor-grid .kpi-val {
      font-size: 10px;
      letter-spacing: -0.35px;
    }

    .tac-corridor-grid .delta-sub {
      font-size: 8px;
      letter-spacing: -0.45px;
    }

    .tac-card {
      min-height: 285px;
    }

    /* Delta & Arrow Styles */
    .delta-sub {
      margin-top: 4px;
      font-family: var(--mono);
      font-size: 8.5px;
      font-weight: 700;
      line-height: 1.3;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      letter-spacing: -0.3px;
    }
    .delta-up { color: #00e676; }
    .delta-down { color: #ff5252; }
    .delta-neutral { color: var(--text-dim); }

    .card-footer {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 6px;
      margin-top: 9px;
      padding-top: 6px;
      border-top: 1px solid var(--border);
      color: var(--text-dim);
      font-family: var(--mono);
      font-size: 9px;
    }

    .arrow {
      flex-shrink: 0;
      color: var(--amber);
      font-weight: 800;
    }

    footer {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      max-width: 1600px;
      margin: 12px auto 0;
      padding-top: 8px;
      border-top: 1px solid var(--border);
      color: var(--text-dim);
      font-family: var(--mono);
      font-size: 9px;
    }

    @media (max-width: 1400px) {
      .grid-sea { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    }

    @media (max-width: 900px) {
      .grid-sea { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .grid-2 { grid-template-columns: 1fr; }
    }

    @media (max-width: 600px) {
      body { padding: 8px; }
      .grid-sea, .grid-2 { grid-template-columns: 1fr; }
      .tac-corridor-grid { grid-template-columns: 1fr; }
      .tac-card { min-height: 0; }
      .top-bar {
        align-items: flex-start;
        flex-direction: column;
        gap: 6px;
        padding: 8px 10px;
      }
      footer { align-items: flex-start; flex-direction: column; }
    }
  </style>
</head>

<body>
  <div class="container">
    <header class="top-bar">
      <div class="top-left">
        <span class="badge-term">MI&lt;GO&gt;</span>
        <span class="title-main">MARKET INTELLIGENCE // MAIN PORTAL</span>
      </div>
      <div class="updated-at">
        UPDATED: <span>__GENERATED_AT__</span>
      </div>
    </header>

    <section class="section-group">
      <div class="section-title">Sea</div>

      <div class="grid-sea">

        <!-- 1. Shipping Index -->
        <a href="__SHIPPING_PATH__" class="card">
          <div>
            <div class="card-header">
              <span class="card-title">Shipping Index (SCFI / BDI)</span>
              <span class="badge __SHIPPING_STATUS_CLASS__">__SHIPPING_STATUS__</span>
            </div>

            <div class="kpi-group">
              <div class="kpi-item">
                <div class="kpi-label">SCFI Composite</div>
                <div class="kpi-val highlight">__SHIPPING_SCFI__</div>
                <div class="delta-sub">__SHIPPING_SCFI_DELTA__</div>
              </div>

              <div class="kpi-item">
                <div class="kpi-label">BDI Composite</div>
                <div class="kpi-val amber">__SHIPPING_BDI__</div>
                <div class="delta-sub">__SHIPPING_BDI_DELTA__</div>
              </div>
            </div>
          </div>

          <div class="card-footer">
            <span>Date: __SHIPPING_DATE__</span>
            <span class="arrow">OPEN &rarr;</span>
          </div>
        </a>

        <!-- 2. Alphaliner TOP 100 -->
        <a href="__ALPHALINER_PATH__" class="card">
          <div>
            <div class="card-header">
              <span class="card-title">Alphaliner TOP 100</span>
              <span class="badge __ALPHALINER_STATUS_CLASS__">__ALPHALINER_STATUS__</span>
            </div>

            <div class="kpi-group">
              <div class="kpi-item">
                <div class="kpi-label">Total Ships</div>
                <div class="kpi-val highlight">__ALPHALINER_SHIPS__</div>
              </div>

              <div class="kpi-item">
                <div class="kpi-label">Total TEU</div>
                <div class="kpi-val highlight">__ALPHALINER_TEU__</div>
              </div>

              <div class="kpi-item">
                <div class="kpi-label">Top 10 M/S</div>
                <div class="kpi-val amber">__ALPHALINER_TOP10_SHARE__</div>
              </div>

              <div class="kpi-item">
                <div class="kpi-label">#01 Operator</div>
                <div class="kpi-val small-value">__ALPHALINER_TOP1__</div>
              </div>
            </div>
          </div>

          <div class="card-footer">
            <span>Date: __ALPHALINER_DATE__</span>
            <span class="arrow">OPEN &rarr;</span>
          </div>
        </a>

        <!-- 3. Bunker Price (VLSFO Rotterdam / Fujairah / Singapore) -->
        <a href="__BUNKER_PATH__" class="card">
          <div>
            <div class="card-header">
              <span class="card-title">Bunker Price</span>
              <span class="badge __BUNKER_STATUS_CLASS__">__BUNKER_STATUS__</span>
            </div>

            <div class="section-sublabel">VLSFO / LATEST PRICE &amp; CHANGE</div>
            <div class="grid-triple">
              <div class="kpi-item">
                <div class="kpi-label">Rotterdam</div>
                <div class="kpi-val highlight">__BUNKER_ROTTERDAM__</div>
                <div class="delta-sub">__BUNKER_ROTTERDAM_DELTA__</div>
              </div>

              <div class="kpi-item">
                <div class="kpi-label">Fujairah</div>
                <div class="kpi-val">__BUNKER_FUJAIRAH__</div>
                <div class="delta-sub">__BUNKER_FUJAIRAH_DELTA__</div>
              </div>

              <div class="kpi-item">
                <div class="kpi-label">Singapore</div>
                <div class="kpi-val amber">__BUNKER_SINGAPORE__</div>
                <div class="delta-sub">__BUNKER_SINGAPORE_DELTA__</div>
              </div>
            </div>
          </div>

          <div class="card-footer">
            <span>Date: __BUNKER_DATE__</span>
            <span class="arrow">OPEN &rarr;</span>
          </div>
        </a>

        <!-- 4. Crude Oil (Dubai / Brent / WTI) -->
        <a href="__OIL_PATH__" class="card">
          <div>
            <div class="card-header">
              <span class="card-title">Crude Oil</span>
              <span class="badge __OIL_STATUS_CLASS__">__OIL_STATUS__</span>
            </div>

            <div class="section-sublabel">OPINET / LATEST PRICE &amp; CHANGE</div>
            <div class="grid-triple">
              <div class="kpi-item">
                <div class="kpi-label">Dubai</div>
                <div class="kpi-val highlight">__OIL_DUBAI__</div>
                <div class="delta-sub">__OIL_DUBAI_DELTA__</div>
              </div>

              <div class="kpi-item">
                <div class="kpi-label">Brent</div>
                <div class="kpi-val">__OIL_BRENT__</div>
                <div class="delta-sub">__OIL_BRENT_DELTA__</div>
              </div>

              <div class="kpi-item">
                <div class="kpi-label">WTI</div>
                <div class="kpi-val amber">__OIL_WTI__</div>
                <div class="delta-sub">__OIL_WTI_DELTA__</div>
              </div>
            </div>
          </div>

          <div class="card-footer">
            <span>Date: __OIL_DATE__</span>
            <span class="arrow">OPEN &rarr;</span>
          </div>
        </a>

        <!-- 5. Top 25 Forwarders -->
        <a href="__FORWARDER_PATH__" class="card">
          <div>
            <div class="card-header">
              <span class="card-title">Global Top 25 Forwarders</span>
              <span class="badge __FORWARDER_STATUS_CLASS__">__FORWARDER_STATUS__</span>
            </div>

            <div class="kpi-group">
              <div class="kpi-item">
                <div class="kpi-label">#01 Provider</div>
                <div class="kpi-val small-value highlight">__FORWARDER_PROVIDER__</div>
              </div>
              <div class="kpi-item">
                <div class="kpi-label">Revenue</div>
                <div class="kpi-val amber">__FORWARDER_REVENUE__</div>
              </div>
              <div class="kpi-item">
                <div class="kpi-label">Ocean Volume</div>
                <div class="kpi-val small-value">__FORWARDER_TEU__</div>
              </div>
              <div class="kpi-item">
                <div class="kpi-label">Air Volume</div>
                <div class="kpi-val small-value">__FORWARDER_AIR_TONS__</div>
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

    <div class="grid-2">
     <!-- Air Section -->
<section class="section-group">
  <div class="section-title">Air</div>

  <a href="__TAC_PATH__" class="card tac-card">
    <div>
      <div class="card-header">
        <span class="card-title">Air Freight (TAC Index)</span>
        <span class="badge __TAC_STATUS_CLASS__">__TAC_STATUS__</span>
      </div>

      <div class="tac-corridor-label">LATEST RATE / MONTH-ON-MONTH CHANGE</div>

      <div class="tac-corridor-grid">
        <div class="kpi-item">
          <div class="kpi-label">Frankfurt-North America</div>
          <div class="kpi-val highlight">__TAC_FRA_NA_RATE__</div>
          <div class="delta-sub">__TAC_FRA_NA_MOM__</div>
        </div>

        <div class="kpi-item">
          <div class="kpi-label">Hong Kong-Europe</div>
          <div class="kpi-val">__TAC_HKG_EU_RATE__</div>
          <div class="delta-sub">__TAC_HKG_EU_MOM__</div>
        </div>

        <div class="kpi-item">
          <div class="kpi-label">Hong Kong-North America</div>
          <div class="kpi-val amber">__TAC_HKG_NA_RATE__</div>
          <div class="delta-sub">__TAC_HKG_NA_MOM__</div>
        </div>
      </div>
    </div>

    <div class="card-footer">
      <span>Date: __TAC_DATE__</span>
      <span class="arrow">OPEN &rarr;</span>
    </div>
  </a>
</section>

      <!-- Global Section -->
      <section class="section-group">
        <div class="section-title">Global</div>

        <a href="__MACRO_PATH__" class="card tac-card">
          <div>
            <div class="card-header">
              <span class="card-title">Macro Economies &amp; GSCPI</span>
              <span class="badge __MACRO_STATUS_CLASS__">__MACRO_STATUS__</span>
            </div>

            <div class="kpi-group">
              <div class="kpi-item">
                <div class="kpi-label">Supply Chain Pressure</div>
                <div class="kpi-val highlight">__MACRO_GSCPI__</div>
              </div>
              <div class="kpi-item">
                <div class="kpi-label">Macro Coverage</div>
                <div class="kpi-val small-value">WorldBank GDP / CPI</div>
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
    <div>MARKET INTELLIGENCE PLATFORM // LOGISTICS &amp; MACRO DASHBOARDS</div>
    <div>TERMINAL FEED: ACTIVE</div>
  </footer>
</body>
</html>
"""

# =============================================================================
# 11. Dashboard Build
# =============================================================================

def main():
    """모든 모듈 DB에서 최신 요약을 읽고 루트 포털을 생성한다."""
    validate_environment()

    logger.info("Building Market Intelligence Portal Dashboard...")

    shipping = get_shipping_summary()
    alphaliner = get_alphaliner_summary()
    bunker = get_bunker_summary()
    oil = get_oil_summary()
    forwarder = get_forwarder_summary()
    tac = get_tac_summary()
    macro = get_macro_summary()

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    html_output = HTML_TEMPLATE

    # -------------------------------------------------------------------------
    # Header
    # -------------------------------------------------------------------------
    html_output = replace_template_value(html_output, "__GENERATED_AT__", generated_at)

    # -------------------------------------------------------------------------
    # Shipping Index
    # -------------------------------------------------------------------------
    html_output = replace_template_value(html_output, "__SHIPPING_PATH__", shipping["path"])
    html_output = replace_template_value(html_output, "__SHIPPING_STATUS__", shipping["status"])
    html_output = html_output.replace("__SHIPPING_STATUS_CLASS__", get_status_class(shipping["status"]))
    html_output = replace_template_value(html_output, "__SHIPPING_SCFI__", shipping["scfi_latest"])
    html_output = html_output.replace("__SHIPPING_SCFI_DELTA__", shipping["scfi_delta_html"])
    html_output = replace_template_value(html_output, "__SHIPPING_BDI__", shipping["bdi_latest"])
    html_output = html_output.replace("__SHIPPING_BDI_DELTA__", shipping["bdi_delta_html"])
    html_output = replace_template_value(
        html_output,
        "__SHIPPING_DATE__",
        get_latest_available_date(shipping["scfi_date"], shipping["bdi_date"]),
    )

    # -------------------------------------------------------------------------
    # Alphaliner
    # -------------------------------------------------------------------------
    html_output = replace_template_value(html_output, "__ALPHALINER_PATH__", alphaliner["path"])
    html_output = replace_template_value(html_output, "__ALPHALINER_STATUS__", alphaliner["status"])
    html_output = html_output.replace("__ALPHALINER_STATUS_CLASS__", get_status_class(alphaliner["status"]))
    html_output = replace_template_value(html_output, "__ALPHALINER_SHIPS__", alphaliner["total_ships"])
    html_output = replace_template_value(html_output, "__ALPHALINER_TEU__", alphaliner["total_teu"])
    html_output = replace_template_value(html_output, "__ALPHALINER_TOP10_SHARE__", alphaliner["top10_share"])
    html_output = replace_template_value(html_output, "__ALPHALINER_TOP1__", alphaliner["top1_operator"])
    html_output = replace_template_value(html_output, "__ALPHALINER_DATE__", alphaliner["latest_date"])

    # -------------------------------------------------------------------------
    # Bunker Price
    # -------------------------------------------------------------------------
    html_output = replace_template_value(html_output, "__BUNKER_PATH__", bunker["path"])
    html_output = replace_template_value(html_output, "__BUNKER_STATUS__", bunker["status"])
    html_output = html_output.replace("__BUNKER_STATUS_CLASS__", get_status_class(bunker["status"]))
    html_output = replace_template_value(html_output, "__BUNKER_ROTTERDAM__", bunker["rotterdam"])
    html_output = html_output.replace("__BUNKER_ROTTERDAM_DELTA__", bunker["rotterdam_delta_html"])
    html_output = replace_template_value(html_output, "__BUNKER_FUJAIRAH__", bunker["fujairah"])
    html_output = html_output.replace("__BUNKER_FUJAIRAH_DELTA__", bunker["fujairah_delta_html"])
    html_output = replace_template_value(html_output, "__BUNKER_SINGAPORE__", bunker["singapore"])
    html_output = html_output.replace("__BUNKER_SINGAPORE_DELTA__", bunker["singapore_delta_html"])
    html_output = replace_template_value(html_output, "__BUNKER_DATE__", bunker["latest_date"])

    # -------------------------------------------------------------------------
    # Crude Oil
    # -------------------------------------------------------------------------
    html_output = replace_template_value(html_output, "__OIL_PATH__", oil["path"])
    html_output = replace_template_value(html_output, "__OIL_STATUS__", oil["status"])
    html_output = html_output.replace("__OIL_STATUS_CLASS__", get_status_class(oil["status"]))
    html_output = replace_template_value(html_output, "__OIL_DUBAI__", oil["dubai"])
    html_output = html_output.replace("__OIL_DUBAI_DELTA__", oil["dubai_delta_html"])
    html_output = replace_template_value(html_output, "__OIL_BRENT__", oil["brent"])
    html_output = html_output.replace("__OIL_BRENT_DELTA__", oil["brent_delta_html"])
    html_output = replace_template_value(html_output, "__OIL_WTI__", oil["wti"])
    html_output = html_output.replace("__OIL_WTI_DELTA__", oil["wti_delta_html"])
    html_output = replace_template_value(html_output, "__OIL_DATE__", oil["latest_date"])

    # -------------------------------------------------------------------------
    # Forwarder
    # -------------------------------------------------------------------------
    html_output = replace_template_value(html_output, "__FORWARDER_PATH__", forwarder["path"])
    html_output = replace_template_value(html_output, "__FORWARDER_STATUS__", forwarder["status"])
    html_output = html_output.replace("__FORWARDER_STATUS_CLASS__", get_status_class(forwarder["status"]))
    html_output = replace_template_value(html_output, "__FORWARDER_PROVIDER__", forwarder["provider"])
    html_output = replace_template_value(html_output, "__FORWARDER_REVENUE__", forwarder["revenue"])
    html_output = replace_template_value(html_output, "__FORWARDER_TEU__", forwarder["teu"])
    html_output = replace_template_value(html_output, "__FORWARDER_AIR_TONS__", forwarder["air_tons"])
    html_output = replace_template_value(html_output, "__FORWARDER_DATE__", forwarder["latest_date"])

    # -------------------------------------------------------------------------
    # TAC Air Freight - Route-level data only
    # -------------------------------------------------------------------------
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
        "__TAC_DATE__",
        tac["latest_date"],
    )

    # Frankfurt - North America
    html_output = replace_template_value(
        html_output,
        "__TAC_FRA_NA_RATE__",
        tac["frankfurt_north_america_rate"],
    )
    html_output = html_output.replace(
        "__TAC_FRA_NA_MOM__",
        tac["frankfurt_north_america_mom_html"],
    )

    # Hong Kong - Europe
    html_output = replace_template_value(
        html_output,
        "__TAC_HKG_EU_RATE__",
        tac["hong_kong_europe_rate"],
    )
    html_output = html_output.replace(
        "__TAC_HKG_EU_MOM__",
        tac["hong_kong_europe_mom_html"],
    )

    # Hong Kong - North America
    html_output = replace_template_value(
        html_output,
        "__TAC_HKG_NA_RATE__",
        tac["hong_kong_north_america_rate"],
    )
    html_output = html_output.replace(
        "__TAC_HKG_NA_MOM__",
        tac["hong_kong_north_america_mom_html"],
    )

    # -------------------------------------------------------------------------
    # Macro / GSCPI
    # -------------------------------------------------------------------------
    html_output = replace_template_value(html_output, "__MACRO_PATH__", macro["path"])
    html_output = replace_template_value(html_output, "__MACRO_STATUS__", macro["status"])
    html_output = html_output.replace("__MACRO_STATUS_CLASS__", get_status_class(macro["status"]))
    html_output = replace_template_value(html_output, "__MACRO_GSCPI__", macro["gscpi_latest"])
    html_output = replace_template_value(html_output, "__MACRO_DATE__", macro["latest_date"])

    # -------------------------------------------------------------------------
    # Write Files (Atomic write)
    # -------------------------------------------------------------------------
    temp_html_path = OUTPUT_HTML.with_suffix(".html.tmp")
    temp_html_path.write_text(html_output, encoding="utf-8")
    temp_html_path.replace(OUTPUT_HTML)

    summary_json = {
        "generated_at": generated_at,
        "shipping": shipping,
        "alphaliner": alphaliner,
        "bunker": bunker,
        "oil": oil,
        "forwarder": forwarder,
        "tac": tac,
        "macro": macro,
    }

    OUTPUT_JSON.write_text(
        json.dumps(summary_json, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    logger.info(f"[SUCCESS] Portal HTML created: {OUTPUT_HTML}")
    logger.info(f"[SUCCESS] Summary JSON created: {OUTPUT_JSON}")

if __name__ == "__main__":
    main()
