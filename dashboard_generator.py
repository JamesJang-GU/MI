# -*- coding: utf-8 -*-
"""
Market Intelligence Portal - Main Dashboard Generator

통합 Market Intelligence Portal의 루트 index.html을 생성한다.

연결 모듈:
- Sea
  - Shipping Index (SCFI / BDI) with WoW Delta & Arrow (↗ / ↘)
  - Alphaliner TOP 100 (Total Ships, Total TEU, Top 10 M/S with units)
  - Bunker Price (Ship & Bunker VLSFO)
  - Global Top 25 Forwarders
- Air
  - TAC Air Freight Index
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
# 2. Common Utilities
# =============================================================================

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
    """벙커유 가격 표시 형식."""
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

def calculate_delta_display(current_val, prev_val, decimals=1):
    """
    현재 값과 이전 값을 비교하여 증감 텍스트, 화살표(↗/↘), CSS 클래스를 반환한다.
    반환: (display_html_str, raw_diff_str)
    """
    if current_val is None or prev_val is None:
        return '<span class="delta-neutral">-</span>', "-"

    try:
        cur = float(current_val)
        prv = float(prev_val)
    except (ValueError, TypeError):
        return '<span class="delta-neutral">-</span>', "-"

    diff = cur - prv
    pct = (diff / prv * 100.0) if prv != 0 else 0.0

    if diff > 0:
        arrow = "↗"
        cls = "delta-up"
        sign = "+"
    elif diff < 0:
        arrow = "↘"
        cls = "delta-down"
        sign = ""
    else:
        arrow = ""
        cls = "delta-neutral"
        sign = ""

    fmt = f"{{:{sign},.{decimals}f}}"
    diff_str = fmt.format(diff)
    pct_str = f"{sign}{pct:.1f}%"

    html_str = f'<span class="{cls}">WoW {diff_str} ({pct_str}) {arrow}</span>'
    return html_str, f"{diff_str} ({pct_str})"

# =============================================================================
# 3. Shipping Index Summary (SCFI / BDI with WoW Delta & Arrow)
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
        logger.warning(f"Shipping Index DB not found: {db_path}")
        return summary

    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            cursor = conn.cursor()

            if not table_exists(cursor, "index_raw"):
                logger.warning("Shipping Index table not found: index_raw")
                return summary

            # SCFI 최신 2개 레코드 (Composite 기준)
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

            # BDI 최신 2개 레코드
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
            summary["scfi_delta_html"], _ = calculate_delta_display(latest[1], prev_val, decimals=1)

        if bdi_rows:
            latest = bdi_rows[0]
            summary["bdi_date"] = str(latest[0] or "-")[:10]
            summary["bdi_latest"] = f"{float(latest[1]):,.0f}" if latest[1] is not None else "-"

            prev_val = bdi_rows[1][1] if len(bdi_rows) > 1 else None
            summary["bdi_delta_html"], _ = calculate_delta_display(latest[1], prev_val, decimals=0)

        if scfi_rows or bdi_rows:
            summary["status"] = "Active"

    except Exception as error:
        logger.exception(f"Failed to read Shipping Index DB: {error}")

    return summary

# =============================================================================
# 4. Alphaliner Summary (Total Ships, Total TEU, Top 10 M/S with units)
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
        logger.warning(f"Alphaliner DB not found: {db_path}")
        return summary

    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            cursor = conn.cursor()

            if not table_exists(cursor, "alphaliner_master") or not table_exists(cursor, "alphaliner_detail"):
                logger.warning("Alphaliner tables missing.")
                return summary

            master_row = cursor.execute(
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

            if not master_row:
                return summary

            snapshot_id, data_date, total_teu, total_ships = master_row

            summary["latest_date"] = str(data_date or "-")[:10]
            summary["total_teu"] = f"{int(total_teu):,} TEU" if total_teu is not None else "-"
            summary["total_ships"] = f"{int(total_ships):,} Ships" if total_ships is not None else "-"

            # Top 10 선사 M/S 합계 계산
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

            # #01 Operator
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
    """Ship & Bunker VLSFO 최신 가격을 읽는다."""
    summary = {
        "title": "Bunker Price (VLSFO)",
        "path": "BunkerPrice/index.html",
        "category": "Sea",
        "latest_date": "-",
        "rotterdam": "-",
        "fujairah": "-",
        "singapore": "-",
        "market_average": "-",
        "available_ports": 0,
        "status": "No Data",
    }

    db_path = DB_PATHS["bunker"]

    if not db_path.exists():
        logger.warning(f"Bunker DB not found: {db_path}")
        return summary

    target_ports = ["Rotterdam", "Fujairah", "Singapore"]

    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            cursor = conn.cursor()

            if not table_exists(cursor, "product_vlsfo"):
                logger.warning(f"Bunker table not found: product_vlsfo (DB: {db_path})")
                return summary

            columns = get_table_columns(cursor, "product_vlsfo")
            port_column = find_first_column(columns, ["port"])
            price_column = find_first_column(columns, ["price"])
            date_column = find_first_column(columns, ["date_str", "date"])
            timestamp_column = find_first_column(columns, ["timestamp_ms"])

            if not port_column or not price_column or not date_column:
                logger.warning("Bunker schema mismatch.")
                return summary

            port_prices = {}
            port_dates = {}

            for target_port in target_ports:
                order_by_columns = [f"{quote_identifier(date_column)} DESC"]
                if timestamp_column:
                    order_by_columns.append(f"{quote_identifier(timestamp_column)} DESC")

                row = cursor.execute(
                    f"""
                    SELECT
                        {quote_identifier(port_column)},
                        {quote_identifier(price_column)},
                        {quote_identifier(date_column)}
                    FROM {quote_identifier("product_vlsfo")}
                    WHERE UPPER(TRIM({quote_identifier(port_column)})) = UPPER(TRIM(?))
                    ORDER BY {", ".join(order_by_columns)}
                    LIMIT 1
                    """,
                    (target_port,),
                ).fetchone()

                if not row:
                    continue

                actual_port, price, data_date = row
                try:
                    price_value = float(price)
                except (TypeError, ValueError):
                    continue

                normalized_port = normalize_port_name(actual_port)
                if normalized_port in target_ports:
                    port_prices[normalized_port] = price_value
                    port_dates[normalized_port] = str(data_date or "-")[:10]

            summary["rotterdam"] = format_price(port_prices.get("Rotterdam"))
            summary["fujairah"] = format_price(port_prices.get("Fujairah"))
            summary["singapore"] = format_price(port_prices.get("Singapore"))
            summary["available_ports"] = len(port_prices)

            if port_dates:
                summary["latest_date"] = max(port_dates.values())

            if port_prices:
                market_average = sum(port_prices.values()) / len(port_prices)
                summary["market_average"] = format_price(market_average)
                summary["status"] = "Active"

    except Exception as error:
        logger.exception(f"Failed to read Bunker DB: {error}")

    return summary

# =============================================================================
# 6. Forwarder Summary
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
        logger.warning(f"Forwarder DB not found: {db_path}")
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
# 7. TAC Air Freight Summary
# =============================================================================

def get_tac_summary():
    """TAC Air Freight Index 최신 데이터를 읽는다."""
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
        return summary

    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            cursor = conn.cursor()

            if not table_exists(cursor, "tac_master"):
                return summary

            row = cursor.execute(
                """
                SELECT data_date, overall_index, weekly_change, route_count
                FROM tac_master
                ORDER BY data_date DESC, snapshot_id DESC
                LIMIT 1
                """
            ).fetchone()

        if row:
            data_date, overall_index, weekly_change, route_count = row
            summary["latest_date"] = str(data_date or "-")[:10]
            summary["overall_index"] = f"{float(overall_index):,.2f}" if overall_index is not None else "-"
            summary["weekly_change"] = f"{float(weekly_change):+.2f}%" if weekly_change is not None else "-"
            summary["route_count"] = str(route_count) if route_count is not None else "-"
            summary["status"] = "Active"

    except Exception as error:
        logger.exception(f"Failed to read TAC DB: {error}")

    return summary

# =============================================================================
# 8. Macro Summary
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
# 9. HTML Template
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

    .grid-4 {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
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
      min-height: 205px;
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

    /* Delta & Arrow Styles */
    .delta-sub {
      margin-top: 4px;
      font-family: var(--mono);
      font-size: 9.5px;
      font-weight: 700;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .delta-up { color: #00e676; }
    .delta-down { color: #ff5252; }
    .delta-neutral { color: var(--text-dim); }

    .bunker-section-label {
      margin: 8px 0 5px;
      color: var(--amber);
      font-family: var(--mono);
      font-size: 8.5px;
      font-weight: 800;
      letter-spacing: 0.5px;
    }

    .bunker-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 5px;
    }
    .bunker-grid .kpi-item { padding: 6px; }
    .bunker-grid .kpi-label { font-size: 8px; }
    .bunker-grid .kpi-val { font-size: 9.5px; letter-spacing: -0.4px; }

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

    @media (max-width: 1240px) {
      .grid-4 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }

    @media (max-width: 760px) {
      body { padding: 8px; }
      .grid-4, .grid-2 { grid-template-columns: 1fr; }
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

      <div class="grid-4">

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

        <!-- 3. Bunker Price -->
        <a href="__BUNKER_PATH__" class="card">
          <div>
            <div class="card-header">
              <span class="card-title">Bunker Price (VLSFO)</span>
              <span class="badge __BUNKER_STATUS_CLASS__">__BUNKER_STATUS__</span>
            </div>

            <div class="bunker-section-label">SHIP &amp; BUNKER / LATEST VLSFO</div>
            <div class="bunker-grid">
              <div class="kpi-item">
                <div class="kpi-label">Rotterdam</div>
                <div class="kpi-val highlight">__BUNKER_ROTTERDAM__</div>
              </div>
              <div class="kpi-item">
                <div class="kpi-label">Fujairah</div>
                <div class="kpi-val">__BUNKER_FUJAIRAH__</div>
              </div>
              <div class="kpi-item">
                <div class="kpi-label">Singapore</div>
                <div class="kpi-val amber">__BUNKER_SINGAPORE__</div>
              </div>
            </div>

            <div class="bunker-section-label">THREE-PORT MARKET INDICATOR</div>
            <div class="kpi-group">
              <div class="kpi-item">
                <div class="kpi-label">VLSFO Average</div>
                <div class="kpi-val highlight">__BUNKER_AVERAGE__</div>
              </div>
              <div class="kpi-item">
                <div class="kpi-label">Available Ports</div>
                <div class="kpi-val amber">__BUNKER_PORT_COUNT__ / 3</div>
              </div>
            </div>
          </div>

          <div class="card-footer">
            <span>Date: __BUNKER_DATE__</span>
            <span class="arrow">OPEN &rarr;</span>
          </div>
        </a>

        <!-- 4. Top 25 Forwarders -->
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
      <section class="section-group">
        <div class="section-title">Air</div>

        <a href="__TAC_PATH__" class="card">
          <div>
            <div class="card-header">
              <span class="card-title">Air Freight (TAC Index)</span>
              <span class="badge __TAC_STATUS_CLASS__">__TAC_STATUS__</span>
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
# 10. Dashboard Build
# =============================================================================

def main():
    """모든 모듈 DB에서 최신 요약을 읽고 루트 포털을 생성한다."""
    logger.info("Building Market Intelligence Portal Dashboard...")

    shipping = get_shipping_summary()
    alphaliner = get_alphaliner_summary()
    bunker = get_bunker_summary()
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
    html_output = replace_template_value(html_output, "__BUNKER_FUJAIRAH__", bunker["fujairah"])
    html_output = replace_template_value(html_output, "__BUNKER_SINGAPORE__", bunker["singapore"])
    html_output = replace_template_value(html_output, "__BUNKER_AVERAGE__", bunker["market_average"])
    html_output = replace_template_value(html_output, "__BUNKER_PORT_COUNT__", bunker["available_ports"])
    html_output = replace_template_value(html_output, "__BUNKER_DATE__", bunker["latest_date"])

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
    # TAC Air Freight
    # -------------------------------------------------------------------------
    html_output = replace_template_value(html_output, "__TAC_PATH__", tac["path"])
    html_output = replace_template_value(html_output, "__TAC_STATUS__", tac["status"])
    html_output = html_output.replace("__TAC_STATUS_CLASS__", get_status_class(tac["status"]))
    html_output = replace_template_value(html_output, "__TAC_INDEX__", tac["overall_index"])
    html_output = replace_template_value(html_output, "__TAC_WEEKLY__", tac["weekly_change"])
    html_output = replace_template_value(html_output, "__TAC_DATE__", tac["latest_date"])
    html_output = replace_template_value(html_output, "__TAC_ROUTES__", tac["route_count"])

    # -------------------------------------------------------------------------
    # Macro / GSCPI
    # -------------------------------------------------------------------------
    html_output = replace_template_value(html_output, "__MACRO_PATH__", macro["path"])
    html_output = replace_template_value(html_output, "__MACRO_STATUS__", macro["status"])
    html_output = html_output.replace("__MACRO_STATUS_CLASS__", get_status_class(macro["status"]))
    html_output = replace_template_value(html_output, "__MACRO_GSCPI__", macro["gscpi_latest"])
    html_output = replace_template_value(html_output, "__MACRO_DATE__", macro["latest_date"])

    # -------------------------------------------------------------------------
    # Write Files
    # -------------------------------------------------------------------------
    temp_html_path = OUTPUT_HTML.with_suffix(".html.tmp")
    temp_html_path.write_text(html_output, encoding="utf-8")
    temp_html_path.replace(OUTPUT_HTML)

    summary_json = {
        "generated_at": generated_at,
        "shipping": shipping,
        "alphaliner": alphaliner,
        "bunker": bunker,
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
