# -*- coding: utf-8 -*-
"""
Market Intelligence Portal - Main Dashboard Generator
통합 포털 index.html 생성 스크립트
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
    # shipping_index.py에서 실제 생성하는 파일
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
    """
    SCFI / BDI Shipping Index 요약 정보 조회.

    shipping_index.py의 실제 DB 구조:
    - DB 파일: index_history.db
    - 테이블: index_raw
    - 컬럼:
        dt          : 날짜
        region      : 항로 또는 지수명
        value       : 지수값
        index_name  : SCFI 또는 BDI
    """
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
                    "Shipping Index table 'index_raw' not found: "
                    f"{db_path}"
                )
                return summary

            # -------------------------------------------------
            # SCFI 종합지수 최신값 조회
            #
            # 실제 KSG 화면의 종합지수 헤더 표기를 고려하여
            # 주요 후보를 모두 허용한다.
            # -------------------------------------------------
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

            # -------------------------------------------------
            # BDI 종합지수 최신값 조회
            #
            # shipping_index.py의 BDI_REGION_PRIORITY에서
            # BDI는 종합 Baltic Dry Index로 사용된다.
            # -------------------------------------------------
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

            # SCFI 종합지수 후보를 찾지 못한 경우,
            # GitHub Actions 로그에서 실제 region명을 확인할 수 있도록 출력
            if not scfi_row:
                scfi_regions = cursor.execute(
                    """
                    SELECT DISTINCT region
                    FROM index_raw
                    WHERE index_name = 'SCFI'
                    ORDER BY region
                    """
                ).fetchall()

                logger.warning(
                    "SCFI composite region not found. "
                    f"Available SCFI regions: "
                    f"{[row[0] for row in scfi_regions]}"
                )

            # BDI 데이터가 없는 경우 실제 저장 region을 출력
            if not bdi_row:
                bdi_regions = cursor.execute(
                    """
                    SELECT DISTINCT region
                    FROM index_raw
                    WHERE index_name = 'BDI'
                    ORDER BY region
                    """
                ).fetchall()

                logger.warning(
                    "BDI composite region not found. "
                    f"Available BDI regions: "
                    f"{[row[0] for row in bdi_regions]}"
                )

        if scfi_row:
            scfi_date, scfi_value, scfi_region = scfi_row

            summary["scfi_date"] = str(scfi_date or "-")
            summary["scfi_latest"] = (
                f"{float(scfi_value):,.1f}"
                if scfi_value is not None
                else "-"
            )

            logger.info(
                "[Shipping] SCFI loaded: "
                f"date={scfi_date}, "
                f"value={scfi_value}, "
                f"region={scfi_region}"
            )

        if bdi_row:
            bdi_date, bdi_value, bdi_region = bdi_row

            summary["bdi_date"] = str(bdi_date or "-")
            summary["bdi_latest"] = (
                f"{float(bdi_value):,.0f}"
                if bdi_value is not None
                else "-"
            )

            logger.info(
                "[Shipping] BDI loaded: "
                f"date={bdi_date}, "
                f"value={bdi_value}, "
                f"region={bdi_region}"
            )

        if scfi_row or bdi_row:
            summary["status"] = "Active"

    except Exception as error:
        logger.exception(
            f"Failed to read Shipping Index DB: {error}"
        )

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
                logger.warning(
                    "Forwarder table not found: forwarder_rankings"
                )
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
    """Bunker Price 요약 정보 조회

    현재는 실제 DB 테이블을 읽지 않으며 BunkerPrice 폴더의 존재 여부만 확인한다.
    """
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
        "category": "Macro",
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
# 4. HTML 템플릿
# =========================================================
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Market Intelligence Portal</title>
  <style>
    :root {
      --bg: #0f172a;
      --card-bg: #1e293b;
      --card-border: #334155;
      --text-main: #f8fafc;
      --text-muted: #94a3b8;
      --accent-blue: #38bdf8;
      --accent-emerald: #34d399;
      --badge-bg: #334155;
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }

    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
        Roboto, "Helvetica Neue", Arial, sans-serif;
      background-color: var(--bg);
      color: var(--text-main);
      padding: 2.5rem 1.5rem;
      min-height: 100vh;
    }

    .container {
      max-width: 1200px;
      margin: 0 auto;
    }

    header {
      margin-bottom: 2.5rem;
      border-bottom: 1px solid var(--card-border);
      padding-bottom: 1.5rem;
      display: flex;
      justify-content: space-between;
      align-items: flex-end;
      flex-wrap: wrap;
      gap: 1rem;
    }

    h1 {
      font-size: 2rem;
      font-weight: 700;
      letter-spacing: -0.025em;
      background: linear-gradient(135deg, #38bdf8, #818cf8);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }

    .updated-at {
      font-size: 0.875rem;
      color: var(--text-muted);
    }

    .section-title {
      font-size: 1.25rem;
      font-weight: 600;
      color: var(--text-muted);
      margin: 2rem 0 1rem;
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }

    .section-title::before {
      content: "";
      display: inline-block;
      width: 4px;
      height: 1.2rem;
      background: var(--accent-blue);
      border-radius: 2px;
    }

    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(350px, 1fr));
      gap: 1.25rem;
    }

    .card {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 12px;
      padding: 1.5rem;
      text-decoration: none;
      color: inherit;
      transition: transform 0.2s, border-color 0.2s, box-shadow 0.2s;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
    }

    .card:hover {
      transform: translateY(-4px);
      border-color: var(--accent-blue);
      box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.4);
    }

    .card-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 0.75rem;
      margin-bottom: 1rem;
    }

    .card-title {
      font-size: 1.15rem;
      font-weight: 600;
      color: var(--text-main);
    }

    .badge {
      flex-shrink: 0;
      font-size: 0.75rem;
      padding: 0.25rem 0.6rem;
      border-radius: 9999px;
      font-weight: 500;
    }

    .badge.active {
      background: rgba(52, 211, 153, 0.15);
      color: var(--accent-emerald);
      border: 1px solid rgba(52, 211, 153, 0.3);
    }

    .badge.nodata {
      background: var(--badge-bg);
      color: var(--text-muted);
    }

    .kpi-group {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 0.75rem;
      margin: 1rem 0;
    }

    .kpi-item {
      background: rgba(15, 23, 42, 0.6);
      padding: 0.75rem;
      border-radius: 8px;
      border: 1px solid rgba(255, 255, 255, 0.05);
    }

    .kpi-label {
      font-size: 0.75rem;
      color: var(--text-muted);
      margin-bottom: 0.25rem;
    }

    .kpi-val {
      font-size: 1.1rem;
      font-weight: 700;
      color: var(--text-main);
      overflow-wrap: anywhere;
    }

    .small-value {
      font-size: 0.95rem;
    }

    .card-footer {
      font-size: 0.8rem;
      color: var(--text-muted);
      margin-top: 1rem;
      display: flex;
      justify-content: space-between;
      gap: 0.75rem;
      border-top: 1px solid rgba(255, 255, 255, 0.05);
      padding-top: 0.75rem;
    }

    .arrow {
      flex-shrink: 0;
      color: var(--accent-blue);
      font-weight: bold;
    }

    @media (max-width: 500px) {
      body {
        padding: 1.25rem 1rem;
      }

      .grid {
        grid-template-columns: 1fr;
      }

      .kpi-group {
        grid-template-columns: 1fr;
      }

      .card-footer {
        flex-direction: column;
      }
    }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div>
        <h1>Market Intelligence Portal</h1>
        <p style="color: var(--text-muted); font-size: 0.95rem; margin-top: 0.25rem;">
          글로벌 해운, 항공, 거시경제 실시간 대시보드
        </p>
      </div>
      <div class="updated-at">갱신 시각: __GENERATED_AT__</div>
    </header>

    <div class="section-title">Sea Logistics (해운)</div>

    <div class="grid">
      <a href="__SHIPPING_PATH__" class="card">
        <div>
          <div class="card-header">
            <span class="card-title">Shipping Index (SCFI / BDI)</span>
            <span class="badge __SHIPPING_STATUS_CLASS__">__SHIPPING_STATUS__</span>
          </div>

          <div class="kpi-group">
            <div class="kpi-item">
              <div class="kpi-label">SCFI 종합지수</div>
              <div class="kpi-val">__SHIPPING_SCFI__</div>
            </div>

            <div class="kpi-item">
              <div class="kpi-label">BDI 종합지수</div>
              <div class="kpi-val">__SHIPPING_BDI__</div>
            </div>
          </div>
        </div>

        <div class="card-footer">
          <span>최신 일자: __SHIPPING_DATE__</span>
          <span class="arrow">대시보드 보기 &rarr;</span>
        </div>
      </a>

      <a href="__ALPHALINER_PATH__" class="card">
        <div>
          <div class="card-header">
            <span class="card-title">Alphaliner TOP 100</span>
            <span class="badge __ALPHALINER_STATUS_CLASS__">__ALPHALINER_STATUS__</span>
          </div>

          <div class="kpi-group">
            <div class="kpi-item">
              <div class="kpi-label">전세계 총 선복량 (TEU)</div>
              <div class="kpi-val">__ALPHALINER_TEU__</div>
            </div>

            <div class="kpi-item">
              <div class="kpi-label">1위 선사 점유율</div>
              <div class="kpi-val small-value">__ALPHALINER_TOP1__</div>
            </div>
          </div>
        </div>

        <div class="card-footer">
          <span>최신 일자: __ALPHALINER_DATE__</span>
          <span class="arrow">대시보드 보기 &rarr;</span>
        </div>
      </a>

      <a href="__BUNKER_PATH__" class="card">
        <div>
          <div class="card-header">
            <span class="card-title">Bunker Price (VLSFO)</span>
            <span class="badge __BUNKER_STATUS_CLASS__">__BUNKER_STATUS__</span>
          </div>

          <div class="kpi-group">
            <div class="kpi-item">
              <div class="kpi-label">주요 항만 가격</div>
              <div class="kpi-val small-value">Rotterdam / Singapore</div>
            </div>

            <div class="kpi-item">
              <div class="kpi-label">유종</div>
              <div class="kpi-val small-value">VLSFO / LSMGO</div>
            </div>
          </div>
        </div>

        <div class="card-footer">
          <span>최신 가격 트렌드 확인</span>
          <span class="arrow">대시보드 보기 &rarr;</span>
        </div>
      </a>

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
              <div class="kpi-val small-value">__FORWARDER_AIR__</div>
            </div>
          </div>
        </div>

        <div class="card-footer">
          <span>A&A 글로벌 랭킹 분석</span>
          <span class="arrow">대시보드 보기 &rarr;</span>
        </div>
      </a>
    </div>

    <div class="section-title">Air Cargo & Macro (항공 및 거시경제)</div>

    <div class="grid">
      <a href="__TAC_PATH__" class="card">
        <div>
          <div class="card-header">
            <span class="card-title">Air Freight (TAC Index)</span>
            <span class="badge __TAC_STATUS_CLASS__">__TAC_STATUS__</span>
          </div>

          <div class="kpi-group">
            <div class="kpi-item">
              <div class="kpi-label">종합 지수 (Overall)</div>
              <div class="kpi-val">__TAC_INDEX__</div>
            </div>

            <div class="kpi-item">
              <div class="kpi-label">주간 변동률 (W/W)</div>
              <div class="kpi-val">__TAC_WEEKLY__</div>
            </div>
          </div>
        </div>

        <div class="card-footer">
          <span>최신 일자: __TAC_DATE__ (노선 수: __TAC_ROUTES__)</span>
          <span class="arrow">대시보드 보기 &rarr;</span>
        </div>
      </a>

      <a href="__MACRO_PATH__" class="card">
        <div>
          <div class="card-header">
            <span class="card-title">Macro Economies & GSCPI</span>
            <span class="badge __MACRO_STATUS_CLASS__">__MACRO_STATUS__</span>
          </div>

          <div class="kpi-group">
            <div class="kpi-item">
              <div class="kpi-label">글로벌 공급망 압력 (GSCPI)</div>
              <div class="kpi-val">__MACRO_GSCPI__</div>
            </div>

            <div class="kpi-item">
              <div class="kpi-label">거시 지표 범위</div>
              <div class="kpi-val small-value">WorldBank GDP / CPI</div>
            </div>
          </div>
        </div>

        <div class="card-footer">
          <span>최신 일자: __MACRO_DATE__</span>
          <span class="arrow">대시보드 보기 &rarr;</span>
        </div>
      </a>
    </div>
  </div>
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

    # SCFI 및 BDI 중 실제 최신 일자를 카드 하단에 표시
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
    html_output = html_output.replace(
        "__SHIPPING_PATH__",
        safe_html(shipping["path"]),
    )
    html_output = html_output.replace(
        "__SHIPPING_STATUS__",
        safe_html(shipping["status"]),
    )
    html_output = html_output.replace(
        "__SHIPPING_STATUS_CLASS__",
        "active" if shipping["status"] == "Active" else "nodata",
    )
    html_output = html_output.replace(
        "__SHIPPING_SCFI__",
        safe_html(shipping["scfi_latest"]),
    )
    html_output = html_output.replace(
        "__SHIPPING_BDI__",
        safe_html(shipping["bdi_latest"]),
    )
    html_output = html_output.replace(
        "__SHIPPING_DATE__",
        safe_html(shipping_latest_date),
    )

    # Alphaliner
    html_output = html_output.replace(
        "__ALPHALINER_PATH__",
        safe_html(alphaliner["path"]),
    )
    html_output = html_output.replace(
        "__ALPHALINER_STATUS__",
        safe_html(alphaliner["status"]),
    )
    html_output = html_output.replace(
        "__ALPHALINER_STATUS_CLASS__",
        "active" if alphaliner["status"] == "Active" else "nodata",
    )
    html_output = html_output.replace(
        "__ALPHALINER_TEU__",
        safe_html(alphaliner["total_teu"]),
    )
    html_output = html_output.replace(
        "__ALPHALINER_TOP1__",
        safe_html(alphaliner["top1_operator"]),
    )
    html_output = html_output.replace(
        "__ALPHALINER_DATE__",
        safe_html(alphaliner["latest_date"]),
    )

    # Bunker
    html_output = html_output.replace(
        "__BUNKER_PATH__",
        safe_html(bunker["path"]),
    )
    html_output = html_output.replace(
        "__BUNKER_STATUS__",
        safe_html(bunker["status"]),
    )
    html_output = html_output.replace(
        "__BUNKER_STATUS_CLASS__",
        "active" if bunker["status"] == "Active" else "nodata",
    )

    # Forwarder
    html_output = html_output.replace(
        "__FORWARDER_PATH__",
        safe_html(forwarder["path"]),
    )
    html_output = html_output.replace(
        "__FORWARDER_STATUS__",
        safe_html(forwarder["status"]),
    )
    html_output = html_output.replace(
        "__FORWARDER_STATUS_CLASS__",
        "active" if forwarder["status"] == "Active" else "nodata",
    )
    html_output = html_output.replace(
        "__FORWARDER_OCEAN__",
        safe_html(forwarder["top1_ocean"]),
    )
    html_output = html_output.replace(
        "__FORWARDER_AIR__",
        safe_html(forwarder["top1_air"]),
    )

    # TAC Index
    html_output = html_output.replace(
        "__TAC_PATH__",
        safe_html(tac["path"]),
    )
    html_output = html_output.replace(
        "__TAC_STATUS__",
        safe_html(tac["status"]),
    )
    html_output = html_output.replace(
        "__TAC_STATUS_CLASS__",
        "active" if tac["status"] == "Active" else "nodata",
    )
    html_output = html_output.replace(
        "__TAC_INDEX__",
        safe_html(tac["overall_index"]),
    )
    html_output = html_output.replace(
        "__TAC_WEEKLY__",
        safe_html(tac["weekly_change"]),
    )
    html_output = html_output.replace(
        "__TAC_DATE__",
        safe_html(tac["latest_date"]),
    )
    html_output = html_output.replace(
        "__TAC_ROUTES__",
        safe_html(tac["route_count"]),
    )

    # Macro
    html_output = html_output.replace(
        "__MACRO_PATH__",
        safe_html(macro["path"]),
    )
    html_output = html_output.replace(
        "__MACRO_STATUS__",
        safe_html(macro["status"]),
    )
    html_output = html_output.replace(
        "__MACRO_STATUS_CLASS__",
        "active" if macro["status"] == "Active" else "nodata",
    )
    html_output = html_output.replace(
        "__MACRO_GSCPI__",
        safe_html(macro["gscpi_latest"]),
    )
    html_output = html_output.replace(
        "__MACRO_DATE__",
        safe_html(macro["latest_date"]),
    )

    # index.html 원자적 저장
    temp_path = OUTPUT_HTML.with_suffix(".tmp")
    temp_path.write_text(html_output, encoding="utf-8")
    temp_path.replace(OUTPUT_HTML)

    # API 또는 외부 활용용 JSON 요약 저장
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
    logger.info(
        "[Shipping Summary] "
        f"SCFI={shipping['scfi_latest']} ({shipping['scfi_date']}), "
        f"BDI={shipping['bdi_latest']} ({shipping['bdi_date']})"
    )

if __name__ == "__main__":
    main()
