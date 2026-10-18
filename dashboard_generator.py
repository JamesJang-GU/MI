# -*- coding: utf-8 -*-
"""
Market Intelligence Portal - Main Dashboard Generator
통합 포털 index.html 생성 스크립트
"""

import json
import logging
import os
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

# 각 모듈별 DB 및 데이터 파일 경로
DB_PATHS = {
    "shipping": BASE_DIR / "Shipping Index" / "shipping_index_data.db",
    "alphaliner": BASE_DIR / "Alphaliner" / "alphaliner_data.db",
    "bunker": BASE_DIR / "BunkerPrice" / "bunker_price_data.db",
    "forwarder": BASE_DIR / "Forwarder" / "freight_forwarders_data.db",
    "tac": BASE_DIR / "TAC Index" / "TAC_Index.db",
    "macro": BASE_DIR / "GDP&Inflation" / "macro_data.db",
}

JSON_PATHS = {
    "shipping": BASE_DIR / "Shipping Index" / "shipping_index_data.json",
    "alphaliner": BASE_DIR / "Alphaliner" / "alphaliner_data.json",
    "forwarder": BASE_DIR / "Forwarder" / "freight_forwarders_data.json",
    "tac": BASE_DIR / "TAC Index" / "tac_index_data.json",
    "macro": BASE_DIR / "GDP&Inflation" / "macro_data.json",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("dashboard_generator")

# =========================================================
# 2. 개별 모듈 데이터 요약 수집기
# =========================================================
def get_tac_summary():
    """TAC Index (Air Freight) 요약 정보 조회 - data_date 컬럼 사용"""
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
        conn = sqlite3.connect(db_path, timeout=10)
        cursor = conn.cursor()
        # [수정] latest_date 대신 data_date 컬럼 조회
        row = cursor.execute(
            """
            SELECT data_date, overall_index, weekly_change, route_count
            FROM tac_master
            ORDER BY data_date DESC, snapshot_id DESC
            LIMIT 1
            """
        ).fetchone()
        conn.close()

        if row:
            d_date, idx_val, w_change, r_count = row
            summary["latest_date"] = str(d_date or "-")
            summary["overall_index"] = f"{idx_val:,.2f}" if idx_val is not None else "-"
            summary["weekly_change"] = (
                f"{w_change:+.2f}%" if w_change is not None else "-"
            )
            summary["route_count"] = str(r_count or "-")
            summary["status"] = "Active"
    except Exception as e:
        logger.error(f"Failed to read TAC Index DB: {e}")

    return summary

def get_shipping_summary():
    """Shipping Index (SCFI & BDI) 요약 정보 조회"""
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
        return summary

    try:
        conn = sqlite3.connect(db_path, timeout=10)
        cursor = conn.cursor()

        # SCFI 종합지수 최신값
        scfi_row = cursor.execute(
            """
            SELECT date, index_value, change_value
            FROM scfi_index
            WHERE region = '종합지수' OR region = 'Composite' OR region IS NULL
            ORDER BY date DESC LIMIT 1
            """
        ).fetchone()

        # BDI 종합지수 최신값
        bdi_row = cursor.execute(
            """
            SELECT date, index_value, change_value
            FROM bdi_index
            WHERE vessel_type = '종합' OR vessel_type = 'BDI' OR vessel_type IS NULL
            ORDER BY date DESC LIMIT 1
            """
        ).fetchone()
        conn.close()

        if scfi_row:
            summary["scfi_date"] = str(scfi_row[0] or "-")
            summary["scfi_latest"] = f"{scfi_row[1]:,.1f}" if scfi_row[1] is not None else "-"
        if bdi_row:
            summary["bdi_date"] = str(bdi_row[0] or "-")
            summary["bdi_latest"] = f"{bdi_row[1]:,.0f}" if bdi_row[1] is not None else "-"

        if scfi_row or bdi_row:
            summary["status"] = "Active"
    except Exception as e:
        logger.error(f"Failed to read Shipping Index DB: {e}")

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
        return summary

    try:
        conn = sqlite3.connect(db_path, timeout=10)
        cursor = conn.cursor()
        m_row = cursor.execute(
            """
            SELECT snapshot_id, data_date, total_teu, total_ships
            FROM alphaliner_master
            ORDER BY data_date DESC, snapshot_id DESC
            LIMIT 1
            """
        ).fetchone()

        if m_row:
            snap_id, d_date, t_teu, t_ships = m_row
            summary["latest_date"] = str(d_date or "-")
            summary["total_teu"] = f"{t_teu:,}" if t_teu else "-"
            summary["total_ships"] = f"{t_ships:,}" if t_ships else "-"

            d_row = cursor.execute(
                """
                SELECT operator_name, market_share
                FROM alphaliner_detail
                WHERE snapshot_id = ? AND rank = 1
                """,
                (snap_id,),
            ).fetchone()
            if d_row:
                summary["top1_operator"] = f"{d_row[0]} ({d_row[1]}%)"

            summary["status"] = "Active"
        conn.close()
    except Exception as e:
        logger.error(f"Failed to read Alphaliner DB: {e}")

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
        return summary

    try:
        conn = sqlite3.connect(db_path, timeout=10)
        cursor = conn.cursor()
        # 테이블 존재 여부 확인 후 조회
        row = cursor.execute(
            """
            SELECT name FROM sqlite_master WHERE type='table' AND name='forwarder_rankings'
            """
        ).fetchone()
        if row:
            top_ocean = cursor.execute(
                "SELECT company_name, ocean_teu FROM forwarder_rankings WHERE ocean_rank = 1 LIMIT 1"
            ).fetchone()
            top_air = cursor.execute(
                "SELECT company_name, air_tons FROM forwarder_rankings WHERE air_rank = 1 LIMIT 1"
            ).fetchone()
            if top_ocean:
                summary["top1_ocean"] = f"{top_ocean[0]} ({top_ocean[1]:,.0f} TEU)" if top_ocean[1] else top_ocean[0]
            if top_air:
                summary["top1_air"] = f"{top_air[0]} ({top_air[1]:,.0f} MT)" if top_air[1] else top_air[0]
            summary["status"] = "Active"
        conn.close()
    except Exception as e:
        logger.error(f"Failed to read Forwarder DB: {e}")

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
        "status": "Active",
    }
    # BunkerPrice 폴더 존재 시 활성화
    bunker_dir = BASE_DIR / "BunkerPrice"
    if not bunker_dir.exists():
        summary["status"] = "No Data"
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
        return summary

    try:
        conn = sqlite3.connect(db_path, timeout=10)
        cursor = conn.cursor()
        row = cursor.execute(
            """
            SELECT date, gscpi_value
            FROM newyorkfed_gscpi
            ORDER BY date DESC LIMIT 1
            """
        ).fetchone()
        if row:
            summary["latest_date"] = str(row[0] or "-")
            summary["gscpi_latest"] = f"{row[1]:,.2f}" if row[1] is not None else "-"
            summary["status"] = "Active"
        conn.close()
    except Exception as e:
        logger.error(f"Failed to read Macro DB: {e}")

    return summary

# =========================================================
# 3. HTML 템플릿 렌더링
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
      --accent-purple: #c084fc;
      --accent-amber: #fbbf24;
      --badge-bg: #334155;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
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
      margin: 2rem 0 1rem 0;
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
      margin-bottom: 1rem;
    }
    .card-title {
      font-size: 1.15rem;
      font-weight: 600;
      color: var(--text-main);
    }
    .badge {
      font-size: 0.75rem;
      padding: 0.25rem 0.6rem;
      border-radius: 9999px;
      font-weight: 500;
    }
    .badge.active { background: rgba(52, 211, 153, 0.15); color: var(--accent-emerald); border: 1px solid rgba(52, 211, 153, 0.3); }
    .badge.nodata { background: var(--badge-bg); color: var(--text-muted); }
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
    }
    .card-footer {
      font-size: 0.8rem;
      color: var(--text-muted);
      margin-top: 1rem;
      display: flex;
      justify-content: space-between;
      border-top: 1px solid rgba(255, 255, 255, 0.05);
      padding-top: 0.75rem;
    }
    .arrow {
      color: var(--accent-blue);
      font-weight: bold;
    }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div>
        <h1>Market Intelligence Portal</h1>
        <p style="color: var(--text-muted); font-size: 0.95rem; margin-top: 0.25rem;">글로벌 해운, 항공, 거시경제 실시간 대시보드</p>
      </div>
      <div class="updated-at">갱신 시각: __GENERATED_AT__</div>
    </header>

    <div class="section-title">Sea Logistics (해운)</div>
    <div class="grid">
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

      <!-- Alphaliner -->
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
              <div class="kpi-val" style="font-size: 0.95rem;">__ALPHALINER_TOP1__</div>
            </div>
          </div>
        </div>
        <div class="card-footer">
          <span>최신 일자: __ALPHALINER_DATE__</span>
          <span class="arrow">대시보드 보기 &rarr;</span>
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
              <div class="kpi-val">Rotterdam / Singapore</div>
            </div>
            <div class="kpi-item">
              <div class="kpi-label">유종</div>
              <div class="kpi-val">VLSFO / LSMGO</div>
            </div>
          </div>
        </div>
        <div class="card-footer">
          <span>최신 가격 트렌드 확인</span>
          <span class="arrow">대시보드 보기 &rarr;</span>
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
              <div class="kpi-val" style="font-size: 0.95rem;">__FORWARDER_OCEAN__</div>
            </div>
            <div class="kpi-item">
              <div class="kpi-label">항공 1위 포워더</div>
              <div class="kpi-val" style="font-size: 0.95rem;">__FORWARDER_AIR__</div>
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
      <!-- TAC Index -->
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

      <!-- Macro Economies & GSCPI -->
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
              <div class="kpi-val" style="font-size: 0.95rem;">WorldBank GDP / CPI</div>
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
# 4. 메인 빌드 로직
# =========================================================
def main():
    logger.info("Building Portal Dashboard HTML (index.html)...")

    # 요약 정보 수집
    shipping = get_shipping_summary()
    alphaliner = get_alphaliner_summary()
    bunker = get_bunker_summary()
    forwarder = get_forwarder_summary()
    tac = get_tac_summary()
    macro = get_macro_summary()

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 템플릿 치환
    html = HTML_TEMPLATE.replace("__GENERATED_AT__", now_str)

    # Shipping 치환
    html = html.replace("__SHIPPING_PATH__", shipping["path"])
    html = html.replace("__SHIPPING_STATUS__", shipping["status"])
    html = html.replace("__SHIPPING_STATUS_CLASS__", "active" if shipping["status"] == "Active" else "nodata")
    html = html.replace("__SHIPPING_SCFI__", shipping["scfi_latest"])
    html = html.replace("__SHIPPING_BDI__", shipping["bdi_latest"])
    html = html.replace("__SHIPPING_DATE__", shipping["scfi_date"] if shipping["scfi_date"] != "-" else shipping["bdi_date"])

    # Alphaliner 치환
    html = html.replace("__ALPHALINER_PATH__", alphaliner["path"])
    html = html.replace("__ALPHALINER_STATUS__", alphaliner["status"])
    html = html.replace("__ALPHALINER_STATUS_CLASS__", "active" if alphaliner["status"] == "Active" else "nodata")
    html = html.replace("__ALPHALINER_TEU__", alphaliner["total_teu"])
    html = html.replace("__ALPHALINER_TOP1__", alphaliner["top1_operator"])
    html = html.replace("__ALPHALINER_DATE__", alphaliner["latest_date"])

    # Bunker 치환
    html = html.replace("__BUNKER_PATH__", bunker["path"])
    html = html.replace("__BUNKER_STATUS__", bunker["status"])
    html = html.replace("__BUNKER_STATUS_CLASS__", "active" if bunker["status"] == "Active" else "nodata")

    # Forwarder 치환
    html = html.replace("__FORWARDER_PATH__", forwarder["path"])
    html = html.replace("__FORWARDER_STATUS__", forwarder["status"])
    html = html.replace("__FORWARDER_STATUS_CLASS__", "active" if forwarder["status"] == "Active" else "nodata")
    html = html.replace("__FORWARDER_OCEAN__", forwarder["top1_ocean"])
    html = html.replace("__FORWARDER_AIR__", forwarder["top1_air"])

    # TAC Index 치환
    html = html.replace("__TAC_PATH__", tac["path"])
    html = html.replace("__TAC_STATUS__", tac["status"])
    html = html.replace("__TAC_STATUS_CLASS__", "active" if tac["status"] == "Active" else "nodata")
    html = html.replace("__TAC_INDEX__", tac["overall_index"])
    html = html.replace("__TAC_WEEKLY__", tac["weekly_change"])
    html = html.replace("__TAC_DATE__", tac["latest_date"])
    html = html.replace("__TAC_ROUTES__", tac["route_count"])

    # Macro 치환
    html = html.replace("__MACRO_PATH__", macro["path"])
    html = html.replace("__MACRO_STATUS__", macro["status"])
    html = html.replace("__MACRO_STATUS_CLASS__", "active" if macro["status"] == "Active" else "nodata")
    html = html.replace("__MACRO_GSCPI__", macro["gscpi_latest"])
    html = html.replace("__MACRO_DATE__", macro["latest_date"])

    # index.html 파일 저장
    temp_path = OUTPUT_HTML.with_suffix(".tmp")
    temp_path.write_text(html, encoding="utf-8")
    temp_path.replace(OUTPUT_HTML)

    # JSON 저장 (API 연동용)
    summary_data = {
        "generated_at": now_str,
        "shipping": shipping,
        "alphaliner": alphaliner,
        "bunker": bunker,
        "forwarder": forwarder,
        "tac": tac,
        "macro": macro,
    }
    OUTPUT_JSON.write_text(json.dumps(summary_data, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info(f"[SUCCESS] Portal HTML built: {OUTPUT_HTML}")

if __name__ == "__main__":
    main()
