# -*- coding: utf-8 -*-
"""
Market Intelligence Portal - Main Dashboard Generator
통합 포털 index.html 생성 스크립트 (Bloomberg Terminal Single-Screen Layout)
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

# 각 모듈별 DB 경로
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
    ISO 형식(YYYY-MM-DD) 기준으로 비교한다.
    """
    valid_dates = [date for date in dates if date and date != "-"]
    return max(valid_dates) if valid_dates else "-"

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
            scfi_date, scfi_value, scfi_region = scfi_row
            summary["scfi_date"] = str(scfi_date or "-")
            summary["scfi_latest"] = (
                f"{float(scfi_value):,.1f}"
                if scfi_value is not None
                else "-"
            )

        if bdi_row:
            bdi_date, bdi_value, bdi_region = bdi_row
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
    """Forwarder Top 25 요약 정보 조회"""
    summary = {
        "title": "Global Top 25 Forwarders",
        "path": "Forwarder/index.html",
        "category": "Sea",
        "latest_year": "-",
        "top1_ocean": "-",
        "top1_air": "-",
        "status": "No Data",
    }

    db_path = DB_PATHS["forwarder"]

    if not db_path.exists():
        logger.warning(f"Forwarder DB not found: {db_path}")
        return summary

    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            cursor = conn.cursor()

            if not table_exists(cursor, "forwarder_rankings"):
                logger.warning("Forwarder table not found: forwarder_rankings")
                return summary

            top_ocean = cursor.execute(
                """
                SELECT
                    company_name,
                    ocean_teu
                FROM forwarder_rankings
                WHERE ocean_rank = 1
                LIMIT 1
                """
            ).fetchone()

            top_air = cursor.execute(
                """
                SELECT
                    company_name,
                    air_tons
                FROM forwarder_rankings
                WHERE air_rank = 1
                LIMIT 1
                """
            ).fetchone()

        if top_ocean:
            company_name, ocean_teu = top_ocean
            summary["top1_ocean"] = (
                f"{company_name} ({float(ocean_teu):,.0f} TEU)"
                if ocean_teu is not None
                else str(company_name or "-")
            )

        if top_air:
            company_name, air_tons = top_air
            summary["top1_air"] = (
                f"{company_name} ({float(air_tons):,.0f} MT)"
                if air_tons is not None
                else str(company_name or "-")
            )

        if top_ocean or top_air:
            summary["status"] = "Active"

    except Exception as error:
        logger.exception(f"Failed to read Forwarder DB: {error}")

    return summary

def get_bunker_summary():
    """Bunker Price 요약 정보 조회"""
    summary = {
        "title": "Bunker Price (VLSFO)",
        "path": "BunkerPrice/index.html",
        "category": "Sea",
        "latest_date": "-",
        "rotterdam": "-",
        "singapore": "-",
        "status": "No Data",
    }

    bunker_dir = BASE_DIR / "BunkerPrice"
    if bunker_dir.exists():
        summary["status"] = "Active"

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
# 4. Bloomberg Style HTML Template (Single Screen Layout)
# =========================================================
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>MARKET INTELLIGENCE // TERMINAL PORTAL</title>
  <style>
    :root {
      --bg-dark: #0a0e17;
      --bg-panel: #111827;
      --bg-item: #0d131f;
      --bg-hover: #162235;
      --amber: #f59e0b;
      --amber-bright: #fbbf24;
      --cyan: #06b6d4;
      --cyan-bright: #22d3ee;
      --green: #10b981;
      --red: #f43f5e;
      --text: #e2e8f0;
      --text-muted: #94a3b8;
      --text-dim: #64748b;
      --border: #1e293b;
      --border-light: #334155;
      --mono: "Roboto Mono", "Consolas", "Courier New", monospace;
      --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans KR", Arial, sans-serif;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }

    html, body {
      height: 100%;
      background: var(--bg-dark);
      color: var(--text);
      font-family: var(--sans);
      font-size: 12px;
      line-height: 1.35;
      overflow-x: hidden;
    }

    body {
      display: flex;
      flex-direction: column;
      padding: 10px 16px;
      min-height: 100vh;
      justify-content: space-between;
    }

    .container {
      max-width: 1600px;
      width: 100%;
      margin: 0 auto;
      flex: 1;
      display: flex;
      flex-direction: column;
      justify-content: flex-start;
      gap: 10px;
    }

    /* Top Bloomberg Bar & Header */
    .top-bar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-bottom: 6px;
      border-bottom: 1px solid var(--border);
      font-family: var(--mono);
      font-size: 11px;
    }

    .top-left {
      display: flex;
      align-items: center;
      gap: 8px;
    }

    .badge-term {
      background: var(--amber);
      color: #000;
      font-weight: 800;
      padding: 1px 5px;
      border-radius: 2px;
      font-size: 10px;
    }

    .title-main {
      font-size: 16px;
      font-weight: 800;
      color: #ffffff;
      letter-spacing: -0.3px;
    }

    .updated-at {
      color: var(--text-dim);
    }
    .updated-at span { color: var(--amber); }

    /* Section Groups */
    .section-group {
      display: flex;
      flex-direction: column;
      gap: 6px;
    }

    .section-title {
      font-size: 12px;
      font-weight: 700;
      color: var(--amber);
      font-family: var(--mono);
      letter-spacing: 0.5px;
      display: flex;
      align-items: center;
      gap: 6px;
      text-transform: uppercase;
    }
    .section-title::before { content: "//"; color: var(--text-dim); font-weight: 800; }

    /* Compact Grids */
    .grid-4 {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 10px;
    }

    .grid-2 {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 10px;
    }

    .grid-split {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
    }

    /* Bloomberg Cards */
    .card {
      background: var(--bg-panel);
      border: 1px solid var(--border);
      border-radius: 4px;
      padding: 10px 12px;
      text-decoration: none;
      color: inherit;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      position: relative;
      transition: all 0.15s ease;
      overflow: hidden;
    }

    .card::before {
      content: "";
      position: absolute;
      top: 0;
      left: 0;
      width: 100%;
      height: 2px;
      background: var(--border-light);
    }

    .card:hover {
      background: var(--bg-hover);
      border-color: var(--amber);
      transform: translateY(-2px);
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
    }
    .card:hover::before { background: var(--amber); }

    .card-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 6px;
      margin-bottom: 6px;
    }

    .card-title {
      font-size: 12.5px;
      font-weight: 700;
      color: #ffffff;
      font-family: var(--mono);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .badge {
      font-size: 9.5px;
      padding: 1px 5px;
      border-radius: 2px;
      font-weight: 700;
      font-family: var(--mono);
    }

    .badge.active {
      background: rgba(16, 185, 129, 0.15);
      color: var(--green);
      border: 1px solid var(--green);
    }

    .badge.nodata {
      background: rgba(100, 116, 139, 0.15);
      color: var(--text-dim);
      border: 1px solid var(--border);
    }

    /* KPI Rows */
    .kpi-group {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 6px;
      margin: 4px 0 6px 0;
    }

    .kpi-item {
      background: var(--bg-item);
      padding: 6px 8px;
      border-radius: 3px;
      border: 1px solid rgba(255, 255, 255, 0.04);
    }

    .kpi-label {
      font-size: 10px;
      color: var(--text-muted);
      font-family: var(--mono);
      margin-bottom: 2px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .kpi-val {
      font-size: 14px;
      font-weight: 800;
      color: #ffffff;
      font-family: var(--mono);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .kpi-val.highlight { color: var(--cyan-bright); }
    .kpi-val.amber { color: var(--amber); }

    .small-value { font-size: 11px; }

    /* Card Footer */
    .card-footer {
      font-size: 10px;
      color: var(--text-dim);
      font-family: var(--mono);
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-top: 1px solid var(--border);
      padding-top: 5px;
      margin-top: 4px;
    }

    .arrow {
      color: var(--amber);
      font-weight: 800;
    }

    /* Bottom Status Bar */
    footer {
      padding-top: 8px;
      border-top: 1px solid var(--border);
      display: flex;
      justify-content: space-between;
      color: var(--text-dim);
      font-family: var(--mono);
      font-size: 10px;
    }

    @media (max-width: 1200px) {
      .grid-4 { grid-template-columns: repeat(2, 1fr); }
      .grid-split { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="container">
    <header class="top-bar">
      <div class="top-left">
        <span class="badge-term">TERMINAL</span>
        <span class="title-main">MARKET INTELLIGENCE // MAIN PORTAL</span>
      </div>
      <div class="updated-at">UPDATED: <span>__GENERATED_AT__</span></div>
    </header>

    <!-- 1. Sea Section (4 Cards) -->
    <section class="section-group">
      <div class="section-title">Sea</div>
      <div class="grid-4">
        <!-- Shipping Index -->
        <a href="__SHIPPING_PATH__" class="card">
          <div>
            <div class="card-header">
              <span class="card-title">Shipping Index (SCFI / BDI)</span>
              <span class="badge __SHIPPING_STATUS_CLASS__">__SHIPPING_STATUS__</span>
            </div>
            <div class="kpi-group">
              <div class="kpi-item">
                <div class="kpi-label">SCFI 종합지수</div>
                <div class="kpi-val highlight">__SHIPPING_SCFI__</div>
              </div>
              <div class="kpi-item">
                <div class="kpi-label">BDI 종합지수</div>
                <div class="kpi-val amber">__SHIPPING_BDI__</div>
              </div>
            </div>
          </div>
          <div class="card-footer">
            <span>Date: __SHIPPING_DATE__</span>
            <span class="arrow">OPEN &rarr;</span>
          </div>
        </a>

        <!-- Alphaliner TOP 100 -->
        <a href="__ALPHALINER_PATH__" class="card">
          <div>
            <div class="card-header">
              <span class="card-title">Alphaliner TOP 100</span>
              <span class="badge __ALPHALINER_STATUS_CLASS__">__ALPHALINER_STATUS__</span>
            </div>
            <div class="kpi-group">
              <div class="kpi-item">
                <div class="kpi-label">총 선복량 (TEU)</div>
                <div class="kpi-val">__ALPHALINER_TEU__</div>
              </div>
              <div class="kpi-item">
                <div class="kpi-label">1위 선사 점유율</div>
                <div class="kpi-val small-value highlight">__ALPHALINER_TOP1__</div>
              </div>
            </div>
          </div>
          <div class="card-footer">
            <span>Date: __ALPHALINER_DATE__</span>
            <span class="arrow">OPEN &rarr;</span>
          </div>
        </a>

        <!-- Bunker Price -->
        <a href="__BUNKER_PATH__" class="card">
          <div>
            <div class="card-header">
              <span class="card-title">Bunker Price (VLSFO)</span>
              <span class="badge __BUNKER_STATUS_CLASS__">__BUNKER_STATUS__</span>
            </div>
            <div class="kpi-group">
              <div class="kpi-item">
                <div class="kpi-label">주요 항만 가격</div>
                <div class="kpi-val small-value">RDM / SIN</div>
              </div>
              <div class="kpi-item">
                <div class="kpi-label">유종 모니터링</div>
                <div class="kpi-val small-value amber">VLSFO / LSMGO</div>
              </div>
            </div>
          </div>
          <div class="card-footer">
            <span>Fuel Oil Price Trends</span>
            <span class="arrow">OPEN &rarr;</span>
          </div>
        </a>

        <!-- Forwarder Top 25 -->
        <a href="__FORWARDER_PATH__" class="card">
          <div>
            <div class="card-header">
              <span class="card-title">Global Top 25 Forwarders</span>
              <span class="badge __FORWARDER_STATUS_CLASS__">__FORWARDER_STATUS__</span>
            </div>
            <div class="kpi-group">
              <div class="kpi-item">
                <div class="kpi-label">해상 1위 포워더</div>
                <div class="kpi-val small-value">__FORWARDER_OCEAN__</div>
              </div>
              <div class="kpi-item">
                <div class="kpi-label">항공 1위 포워더</div>
                <div class="kpi-val small-value highlight">__FORWARDER_AIR__</div>
              </div>
            </div>
          </div>
          <div class="card-footer">
            <span>A&A Global Ranking Analysis</span>
            <span class="arrow">OPEN &rarr;</span>
          </div>
        </a>
      </div>
    </section>

    <!-- 2. Air & Global Split Section -->
    <div class="grid-split">
      <!-- Air Section (1 Card) -->
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
                <div class="kpi-label">종합 지수 (Overall)</div>
                <div class="kpi-val highlight">__TAC_INDEX__</div>
              </div>
              <div class="kpi-item">
                <div class="kpi-label">주간 변동률 (W/W)</div>
                <div class="kpi-val amber">__TAC_WEEKLY__</div>
              </div>
            </div>
          </div>
          <div class="card-footer">
            <span>Date: __TAC_DATE__ (Routes: __TAC_ROUTES__)</span>
            <span class="arrow">OPEN &rarr;</span>
          </div>
        </a>
      </section>

      <!-- Global Section (1 Card) -->
      <section class="section-group">
        <div class="section-title">Global</div>
        <a href="__MACRO_PATH__" class="card">
          <div>
            <div class="card-header">
              <span class="card-title">Macro Economies & GSCPI</span>
              <span class="badge __MACRO_STATUS_CLASS__">__MACRO_STATUS__</span>
            </div>
            <div class="kpi-group">
              <div class="kpi-item">
                <div class="kpi-label">공급망 압력 (GSCPI)</div>
                <div class="kpi-val highlight">__MACRO_GSCPI__</div>
              </div>
              <div class="kpi-item">
                <div class="kpi-label">거시 지표 범위</div>
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
    <div>MARKET INTELLIGENCE PLATFORM // REAL-TIME LOGISTICS & MACRO DASHBOARDS</div>
    <div>TERMINAL FEED: ACTIVE</div>
  </footer>
</body>
</html>
"""

# =========================================================
# 5. 메인 빌드 로직
# =========================================================
def main():
    logger.info("Building Portal Dashboard HTML (index.html)...")

    # 개별 모듈 데이터 요약 수집
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

    # 템플릿 치환
    html_output = HTML_TEMPLATE.replace(
        "__GENERATED_AT__",
        safe_html(now_str),
    )

    # Shipping
    html_output = html_output.replace("__SHIPPING_PATH__", safe_html(shipping["path"]))
    html_output = html_output.replace("__SHIPPING_STATUS__", safe_html(shipping["status"]))
    html_output = html_output.replace(
        "__SHIPPING_STATUS_CLASS__",
        "active" if shipping["status"] == "Active" else "nodata",
    )
    html_output = html_output.replace("__SHIPPING_SCFI__", safe_html(shipping["scfi_latest"]))
    html_output = html_output.replace("__SHIPPING_BDI__", safe_html(shipping["bdi_latest"]))
    html_output = html_output.replace("__SHIPPING_DATE__", safe_html(shipping_latest_date))

    # Alphaliner
    html_output = html_output.replace("__ALPHALINER_PATH__", safe_html(alphaliner["path"]))
    html_output = html_output.replace("__ALPHALINER_STATUS__", safe_html(alphaliner["status"]))
    html_output = html_output.replace(
        "__ALPHALINER_STATUS_CLASS__",
        "active" if alphaliner["status"] == "Active" else "nodata",
    )
    html_output = html_output.replace("__ALPHALINER_TEU__", safe_html(alphaliner["total_teu"]))
    html_output = html_output.replace("__ALPHALINER_TOP1__", safe_html(alphaliner["top1_operator"]))
    html_output = html_output.replace("__ALPHALINER_DATE__", safe_html(alphaliner["latest_date"]))

    # Bunker
    html_output = html_output.replace("__BUNKER_PATH__", safe_html(bunker["path"]))
    html_output = html_output.replace("__BUNKER_STATUS__", safe_html(bunker["status"]))
    html_output = html_output.replace(
        "__BUNKER_STATUS_CLASS__",
        "active" if bunker["status"] == "Active" else "nodata",
    )

    # Forwarder
    html_output = html_output.replace("__FORWARDER_PATH__", safe_html(forwarder["path"]))
    html_output = html_output.replace("__FORWARDER_STATUS__", safe_html(forwarder["status"]))
    html_output = html_output.replace(
        "__FORWARDER_STATUS_CLASS__",
        "active" if forwarder["status"] == "Active" else "nodata",
    )
    html_output = html_output.replace("__FORWARDER_OCEAN__", safe_html(forwarder["top1_ocean"]))
    html_output = html_output.replace("__FORWARDER_AIR__", safe_html(forwarder["top1_air"]))

    # TAC Index
    html_output = html_output.replace("__TAC_PATH__", safe_html(tac["path"]))
    html_output = html_output.replace("__TAC_STATUS__", safe_html(tac["status"]))
    html_output = html_output.replace(
        "__TAC_STATUS_CLASS__",
        "active" if tac["status"] == "Active" else "nodata",
    )
    html_output = html_output.replace("__TAC_INDEX__", safe_html(tac["overall_index"]))
    html_output = html_output.replace("__TAC_WEEKLY__", safe_html(tac["weekly_change"]))
    html_output = html_output.replace("__TAC_DATE__", safe_html(tac["latest_date"]))
    html_output = html_output.replace("__TAC_ROUTES__", safe_html(tac["route_count"]))

    # Macro
    html_output = html_output.replace("__MACRO_PATH__", safe_html(macro["path"]))
    html_output = html_output.replace("__MACRO_STATUS__", safe_html(macro["status"]))
    html_output = html_output.replace(
        "__MACRO_STATUS_CLASS__",
        "active" if macro["status"] == "Active" else "nodata",
    )
    html_output = html_output.replace("__MACRO_GSCPI__", safe_html(macro["gscpi_latest"]))
    html_output = html_output.replace("__MACRO_DATE__", safe_html(macro["latest_date"]))

    # index.html 원자적 저장
    temp_path = OUTPUT_HTML.with_suffix(".tmp")
    temp_path.write_text(html_output, encoding="utf-8")
    temp_path.replace(OUTPUT_HTML)

    # JSON 요약 저장
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

if __name__ == "__main__":
    main()
