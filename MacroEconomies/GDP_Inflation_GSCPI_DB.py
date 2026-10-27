# -*- coding: utf-8 -*-
"""
MacroEconomies Static Dashboard Generator
(GDP Growth, Inflation & GSCPI)
Standardized for Bloomberg/MI Terminal UI
"""

import argparse
import datetime
import io
import json
import logging
import os
import re
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("MacroEconomies")

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "gdp_inflation_gscpi_cache.db"
MACRO_JSON_PATH = BASE_DIR / "macro_data.json"
GSCPI_JSON_PATH = BASE_DIR / "gscpi_data.json"
META_JSON_PATH = BASE_DIR / "dashboard_meta.json"
HTML_PATH = BASE_DIR / "index.html"

COUNTRIES = [
    {"code": "WLD", "name": "World", "group": "Global"},
    {"code": "USA", "name": "United States", "group": "Advanced"},
    {"code": "CHN", "name": "China", "group": "Emerging"},
    {"code": "KOR", "name": "South Korea", "group": "Asia"},
    {"code": "JPN", "name": "Japan", "group": "Advanced"},
    {"code": "DEU", "name": "Germany", "group": "Europe"},
    {"code": "GBR", "name": "United Kingdom", "group": "Europe"},
    {"code": "FRA", "name": "France", "group": "Europe"},
    {"code": "IND", "name": "India", "group": "Emerging"},
    {"code": "VNM", "name": "Vietnam", "group": "Asia"},
    {"code": "SGP", "name": "Singapore", "group": "Asia"},
    {"code": "BRA", "name": "Brazil", "group": "Emerging"},
    {"code": "MEX", "name": "Mexico", "group": "Emerging"},
    {"code": "EUU", "name": "European Union", "group": "Europe"}
]

WB_INDICATORS = {
    "GDP_GROWTH": "NY.GDP.MKTP.KD.ZG",
    "CPI_INFLATION": "FP.CPI.TOTL.ZG"
}

def init_db(conn: sqlite3.Connection):
    """DB 스키마 검증 및 자동 마이그레이션"""
    cursor = conn.cursor()

    # 1. macro_series 검증 및 생성
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS macro_series (
            indicator TEXT NOT NULL,
            country_code TEXT NOT NULL,
            country_name TEXT NOT NULL,
            year INTEGER NOT NULL,
            value REAL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (indicator, country_code, year)
        )
    """)

    # 레거시 스키마(country 컬럼 등) 점검 및 보정
    cursor.execute("PRAGMA table_info(macro_series)")
    columns = [row[1] for row in cursor.fetchall()]
    if "country_code" not in columns:
        logger.warning("[DB] macro_series 테이블 스키마가 구버전입니다. 마이그레이션을 진행합니다.")
        cursor.execute("DROP TABLE macro_series")
        cursor.execute("""
            CREATE TABLE macro_series (
                indicator TEXT NOT NULL,
                country_code TEXT NOT NULL,
                country_name TEXT NOT NULL,
                year INTEGER NOT NULL,
                value REAL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (indicator, country_code, year)
            )
        """)

    # 2. gscpi_series 검증 및 생성
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS gscpi_series (
            date TEXT PRIMARY KEY,
            value REAL NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()

def fetch_world_bank_data(conn: sqlite3.Connection):
    """World Bank Open API 시계열 수집"""
    logger.info("[WORLD_BANK] 거시경제 지표 수집 시작...")
    cursor = conn.cursor()
    current_year = datetime.datetime.now().year
    start_year = current_year - 25

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (MI-Platform/WorldBankCrawler)"})

    for ind_key, ind_code in WB_INDICATORS.items():
        total_records = 0
        for c in COUNTRIES:
            url = f"https://api.worldbank.org/v2/country/{c['code']}/indicator/{ind_code}?date={start_year}:{current_year}&format=json&per_page=100"
            try:
                res = session.get(url, timeout=10)
                if res.status_code == 200:
                    data = res.json()
                    if len(data) >= 2 and isinstance(data[1], list):
                        for row in data[1]:
                            year_val = int(row.get("date", 0))
                            val = row.get("value")
                            if year_val and val is not None:
                                cursor.execute("""
                                    INSERT OR REPLACE INTO macro_series (indicator, country_code, country_name, year, value, updated_at)
                                    VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                                """, (ind_key, c["code"], c["name"], year_val, float(val)))
                                total_records += 1
            except Exception as e:
                logger.warning(f"[WORLD_BANK WARN] {c['code']} {ind_key} 수집 오류: {e}")
        conn.commit()
        logger.info(f"[WORLD_BANK] {ind_key} 적재 완료 ({total_records}건 갱신)")

def fetch_gscpi_data(conn: sqlite3.Connection):
    """GSCPI 다계층 수집: 엑셀 직다운로드 -> Playwright 스니퍼 -> 기존 DB 캐시"""
    logger.info("[GSCPI] New York Fed GSCPI 수집 시작...")
    cursor = conn.cursor()
    records = []

    # Strategy 1: NY Fed 공식 엑셀/CSV 다운로드 시도
    excel_urls = [
        "https://www.newyorkfed.org/medialibrary/media/research/policy/gscpi/gscpi_data.xlsx",
        "https://www.newyorkfed.org/medialibrary/media/research/policy/gscpi/gscpi_data.xls",
        "https://www.newyorkfed.org/medialibrary/media/research/policy/gscpi/gscpi_data.csv"
    ]

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    for url in excel_urls:
        try:
            res = requests.get(url, headers=headers, timeout=15)
            if res.status_code == 200 and len(res.content) > 1000:
                if url.endswith(".csv"):
                    df = pd.read_csv(io.BytesIO(res.content))
                else:
                    df = pd.read_excel(io.BytesIO(res.content))

                # 시계열 데이터 행 파싱
                for _, row in df.iterrows():
                    d_str = str(row.iloc[0]).strip()
                    val = row.iloc[1] if len(row) > 1 else None
                    match = re.search(r"(\d{4})[-/.](\d{1,2})", d_str)
                    if match and pd.notnull(val):
                        try:
                            formatted_date = f"{int(match.group(1)):04d}-{int(match.group(2)):02d}"
                            records.append((formatted_date, float(val)))
                        except ValueError:
                            continue
                if records:
                    logger.info(f"[GSCPI] 공식 데이터셋 파일 파싱 성공: {len(records)}건 확보")
                    break
        except Exception as e:
            logger.debug(f"[GSCPI] Direct download attempt failed for {url}: {e}")

    # Strategy 2: Playwright 기반 웹 브라우저 스니핑
    if not records:
        try:
            from playwright.sync_api import sync_playwright
            logger.info("[GSCPI] Playwright 브라우저 엔진으로 실시간 차트 스니핑 진행...")
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                sniffed_data = []

                def handle_response(response):
                    if "gscpi" in response.url.lower() or "research" in response.url.lower():
                        try:
                            ct = response.headers.get("content-type", "")
                            if "json" in ct:
                                j = response.json()
                                sniffed_data.append(j)
                        except Exception:
                            pass

                page.on("response", handle_response)
                page.goto("https://www.newyorkfed.org/research/policy/gscpi#/interactive", timeout=30000, wait_until="networkidle")
                page.wait_for_timeout(3000)
                browser.close()

                for j in sniffed_data:
                    if isinstance(j, list):
                        for item in j:
                            if isinstance(item, dict) and "date" in item and "gscpi" in item:
                                records.append((str(item["date"])[:7], float(item["gscpi"])))
        except Exception as e:
            logger.warning(f"[GSCPI] Playwright 스니핑 실행 중 예외: {e}")

    # Strategy 3: DB 캐시 반영
    if records:
        for d, v in records:
            cursor.execute("INSERT OR REPLACE INTO gscpi_series (date, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)", (d, v))
        conn.commit()
        logger.info(f"[GSCPI] DB 갱신 완료 ({len(records)}건)")
    else:
        cursor.execute("SELECT COUNT(*) FROM gscpi_series")
        cnt = cursor.fetchone()[0]
        if cnt > 0:
            logger.warning(f"[GSCPI] 실시간 수집 실패. 기존 캐시된 {cnt}건의 데이터를 안전하게 유지합니다.")
        else:
            logger.warning("[GSCPI] 신규 수집 실패 및 캐시 데이터가 없습니다.")

def export_json_and_html(conn: sqlite3.Connection):
    """정적 JSON 및 Bloomberg 테마 HTML 생성"""
    cursor = conn.cursor()

    # 1. Macro JSON
    cursor.execute("SELECT indicator, country_code, country_name, year, value FROM macro_series ORDER BY indicator, country_code, year ASC")
    macro_rows = cursor.fetchall()
    macro_data = {"GDP_GROWTH": {}, "CPI_INFLATION": {}}

    for ind, code, name, year, val in macro_rows:
        if ind not in macro_data:
            macro_data[ind] = {}
        if code not in macro_data[ind]:
            macro_data[ind][code] = {"name": name, "series": []}
        macro_data[ind][code]["series"].append({"year": year, "value": val})

    with open(MACRO_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(macro_data, f, ensure_ascii=False, indent=2)

    # 2. GSCPI JSON
    cursor.execute("SELECT date, value FROM gscpi_series ORDER BY date ASC")
    gscpi_rows = cursor.fetchall()
    gscpi_data = [{"date": r[0], "value": r[1]} for r in gscpi_rows]

    with open(GSCPI_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(gscpi_data, f, ensure_ascii=False, indent=2)

    # 3. Meta JSON
    latest_gscpi = gscpi_data[-1] if gscpi_data else {"date": "N/A", "value": 0.0}
    meta_data = {
        "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "latest_gscpi": latest_gscpi,
        "country_count": len(COUNTRIES)
    }
    with open(META_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(meta_data, f, ensure_ascii=False, indent=2)

    # 4. Bloomberg Dashboard HTML
    html_content = generate_bloomberg_html(meta_data)
    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html_content)

    logger.info("[EXPORT] JSON 데이터 및 index.html 대시보드 빌드 완료.")

def generate_bloomberg_html(meta: dict) -> str:
    """Bloomberg Terminal Style Static Dashboard HTML Template"""
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>MI Terminal - Macro Economies & GSCPI</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Noto+Sans+KR:wght@400;600;700&display=swap" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    :root {{
      --bg-base: #0a0a0c;
      --bg-card: #121318;
      --bg-header: #181a20;
      --border: #282a36;
      --amber: #ff9800;
      --cyan: #00e5ff;
      --green: #00e676;
      --red: #ff5252;
      --text: #e0e2ec;
      --text-muted: #8b8e9b;
      --font-mono: 'JetBrains Mono', monospace;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background-color: var(--bg-base);
      color: var(--text);
      font-family: 'Noto Sans KR', sans-serif;
      padding: 16px;
      font-size: 13px;
    }}
    /* Top Terminal Header */
    .terminal-header {{
      background-color: var(--bg-header);
      border: 1px solid var(--border);
      border-radius: 4px;
      padding: 12px 16px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 16px;
    }}
    .terminal-title {{
      display: flex;
      align-items: center;
      gap: 12px;
    }}
    .terminal-title h1 {{
      font-family: var(--font-mono);
      font-size: 16px;
      font-weight: 700;
      color: var(--amber);
      letter-spacing: 0.5px;
    }}
    .terminal-badge {{
      background: #232733;
      color: var(--cyan);
      font-family: var(--font-mono);
      font-size: 11px;
      padding: 2px 8px;
      border-radius: 3px;
      border: 1px solid rgba(0, 229, 255, 0.3);
    }}
    .terminal-nav {{
      display: flex;
      align-items: center;
      gap: 16px;
      font-family: var(--font-mono);
      font-size: 12px;
    }}
    .nav-btn {{
      background: #1e222d;
      color: var(--amber);
      border: 1px solid var(--amber);
      padding: 4px 10px;
      border-radius: 3px;
      text-decoration: none;
      font-weight: 600;
    }}
    .nav-btn:hover {{
      background: var(--amber);
      color: #000;
    }}
    /* KPI Cards */
    .kpi-row {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }}
    .kpi-card {{
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-left: 3px solid var(--amber);
      border-radius: 4px;
      padding: 12px 14px;
    }}
    .kpi-label {{
      color: var(--text-muted);
      font-size: 11px;
      margin-bottom: 4px;
      font-family: var(--font-mono);
    }}
    .kpi-val {{
      font-family: var(--font-mono);
      font-size: 20px;
      font-weight: 700;
      color: var(--cyan);
    }}
    /* Main Layout */
    .grid-layout {{
      display: grid;
      grid-template-columns: 2fr 1fr;
      gap: 16px;
    }}
    @media (max-width: 1024px) {{
      .grid-layout {{ grid-template-columns: 1fr; }}
    }}
    .card {{
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 4px;
      padding: 14px;
      margin-bottom: 16px;
    }}
    .card-title {{
      font-family: var(--font-mono);
      font-size: 13px;
      font-weight: 700;
      color: var(--amber);
      margin-bottom: 12px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid var(--border);
      padding-bottom: 6px;
    }}
    .chart-container {{
      position: relative;
      height: 320px;
      width: 100%;
    }}
    /* Tables */
    table {{
      width: 100%;
      border-collapse: collapse;
      font-family: var(--font-mono);
      font-size: 12px;
    }}
    th, td {{
      padding: 8px 10px;
      text-align: right;
      border-bottom: 1px solid #1c1e26;
    }}
    th {{
      color: var(--text-muted);
      font-weight: 600;
      background: #15171e;
    }}
    th:first-child, td:first-child {{ text-align: left; }}
    .pos {{ color: var(--green); }}
    .neg {{ color: var(--red); }}
  </style>
</head>
<body>

  <!-- Top MI Header -->
  <div class="terminal-header">
    <div class="terminal-title">
      <h1>MI TERMINAL // MACRO & GSCPI</h1>
      <span class="terminal-badge">LIVE MONITOR</span>
    </div>
    <div class="terminal-nav">
      <span style="color: var(--text-muted)">SRC: World Bank, NY Fed</span>
      <span style="color: var(--cyan)" id="liveClock">--:--:-- KST</span>
      <a href="../index.html" class="nav-btn">PORTAL &lt;GO&gt;</a>
    </div>
  </div>

  <!-- KPI Section -->
  <div class="kpi-row">
    <div class="kpi-card">
      <div class="kpi-label">GLOBAL SUPPLY CHAIN PRESSURE (GSCPI)</div>
      <div class="kpi-val" id="kpiGscpi">--</div>
    </div>
    <div class="kpi-card" style="border-left-color: var(--cyan)">
      <div class="kpi-label">LATEST US GDP GROWTH (WB)</div>
      <div class="kpi-val" id="kpiUsGdp">--%</div>
    </div>
    <div class="kpi-card" style="border-left-color: var(--green)">
      <div class="kpi-label">LATEST US CPI INFLATION (WB)</div>
      <div class="kpi-val" id="kpiUsCpi">--%</div>
    </div>
  </div>

  <!-- Charts & Tables -->
  <div class="grid-layout">
    <div>
      <div class="card">
        <div class="card-title">
          <span>01. GLOBAL SUPPLY CHAIN PRESSURE INDEX (GSCPI)</span>
          <span style="font-size:11px; color:var(--text-muted);">Standard Deviations from Avg</span>
        </div>
        <div class="chart-container">
          <canvas id="gscpiChart"></canvas>
        </div>
      </div>

      <div class="card">
        <div class="card-title">
          <span>02. GDP GROWTH RATE (%) - MAJOR ECONOMIES</span>
        </div>
        <div class="chart-container">
          <canvas id="gdpChart"></canvas>
        </div>
      </div>
    </div>

    <div>
      <div class="card">
        <div class="card-title">
          <span>03. MACRO SNAPSHOT (LATEST)</span>
        </div>
        <table>
          <thead>
            <tr>
              <th>Country</th>
              <th>GDP (%)</th>
              <th>CPI (%)</th>
            </tr>
          </thead>
          <tbody id="macroTableBody">
            <tr><td colspan="3" style="text-align:center;">Loading...</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <script>
    // Live KST Clock
    function updateClock() {{
      const now = new Date();
      const utc = now.getTime() + (now.getTimezoneOffset() * 60000);
      const kst = new Date(utc + (3600000 * 9));
      document.getElementById('liveClock').textContent =
        kst.toISOString().replace('T', ' ').substring(0, 19) + ' KST';
    }}
    setInterval(updateClock, 1000);
    updateClock();

    // Data Load & Chart Initialization
    async function initDashboard() {{
      try {{
        const [macroRes, gscpiRes] = await Promise.all([
          fetch('macro_data.json'),
          fetch('gscpi_data.json')
        ]);
        const macroData = await macroRes.json();
        const gscpiData = await gscpiRes.json();

        // 1. GSCPI KPI & Chart
        if (gscpiData.length > 0) {{
          const latest = gscpiData[gscpiData.length - 1];
          const valEl = document.getElementById('kpiGscpi');
          valEl.textContent = `${{latest.value.toFixed(2)}}σ (${{latest.date}})`;
          valEl.className = 'kpi-val ' + (latest.value > 0 ? 'neg' : 'pos');

          const ctxGscpi = document.getElementById('gscpiChart').getContext('2d');
          new Chart(ctxGscpi, {{
            type: 'line',
            data: {{
              labels: gscpiData.slice(-60).map(d => d.date),
              datasets: [{{
                label: 'GSCPI (Standard Deviations)',
                data: gscpiData.slice(-60).map(d => d.value),
                borderColor: '#00e5ff',
                backgroundColor: 'rgba(0, 229, 255, 0.08)',
                fill: true,
                borderWidth: 1.5,
                pointRadius: 0
              }}]
            }},
            options: {{
              responsive: true,
              maintainAspectRatio: false,
              scales: {{
                x: {{ grid: {{ color: '#1c1e26' }}, ticks: {{ color: '#8b8e9b', font: {{ family: 'JetBrains Mono', size: 10 }} }} }},
                y: {{ grid: {{ color: '#1c1e26' }}, ticks: {{ color: '#8b8e9b', font: {{ family: 'JetBrains Mono', size: 10 }} }} }}
              }},
              plugins: {{ legend: {{ display: false }} }}
            }}
          }});
        }}

        // 2. Macro Table & KPI
        const gdpObj = macroData.GDP_GROWTH || {{}};
        const cpiObj = macroData.CPI_INFLATION || {{}};

        if (gdpObj.USA && gdpObj.USA.series.length > 0) {{
          const usGdp = gdpObj.USA.series[gdpObj.USA.series.length - 1];
          document.getElementById('kpiUsGdp').textContent = `${{usGdp.value.toFixed(2)}}% (${{usGdp.year}})`;
        }}
        if (cpiObj.USA && cpiObj.USA.series.length > 0) {{
          const usCpi = cpiObj.USA.series[cpiObj.USA.series.length - 1];
          document.getElementById('kpiUsCpi').textContent = `${{usCpi.value.toFixed(2)}}% (${{usCpi.year}})`;
        }}

        // Populate Table
        const tbody = document.getElementById('macroTableBody');
        tbody.innerHTML = '';
        const targetCountries = ['WLD', 'USA', 'CHN', 'KOR', 'JPN', 'DEU', 'IND', 'VNM'];

        targetCountries.forEach(code => {{
          const gdpSeries = gdpObj[code]?.series || [];
          const cpiSeries = cpiObj[code]?.series || [];
          const lastGdp = gdpSeries.length ? gdpSeries[gdpSeries.length - 1].value : null;
          const lastCpi = cpiSeries.length ? cpiSeries[cpiSeries.length - 1].value : null;
          const name = gdpObj[code]?.name || code;

          const tr = document.createElement('tr');
          tr.innerHTML = `
            <td style="color:#e0e2ec;">${{name}}</td>
            <td class="${{lastGdp >= 0 ? 'pos' : 'neg'}}">${{lastGdp !== null ? lastGdp.toFixed(1) + '%' : '-'}}</td>
            <td style="color:var(--amber);">${{lastCpi !== null ? lastCpi.toFixed(1) + '%' : '-'}}</td>
          `;
          tbody.appendChild(tr);
        }});

        // 3. GDP Chart
        const ctxGdp = document.getElementById('gdpChart').getContext('2d');
        const chartCountries = ['USA', 'CHN', 'KOR', 'DEU'];
        const colors = ['#00e5ff', '#ff5252', '#00e676', '#ff9800'];
        const years = gdpObj.USA?.series.slice(-10).map(s => s.year) || [];

        const datasets = chartCountries.map((c, idx) => {{
          const s = gdpObj[c]?.series.slice(-10) || [];
          return {{
            label: c,
            data: s.map(item => item.value),
            borderColor: colors[idx],
            borderWidth: 1.5,
            fill: false,
            pointRadius: 2
          }};
        }});

        new Chart(ctxGdp, {{
          type: 'line',
          data: {{ labels: years, datasets: datasets }},
          options: {{
            responsive: true,
            maintainAspectRatio: false,
            scales: {{
              x: {{ grid: {{ color: '#1c1e26' }}, ticks: {{ color: '#8b8e9b', font: {{ family: 'JetBrains Mono', size: 10 }} }} }},
              y: {{ grid: {{ color: '#1c1e26' }}, ticks: {{ color: '#8b8e9b', font: {{ family: 'JetBrains Mono', size: 10 }} }} }}
            }},
            plugins: {{
              legend: {{
                labels: {{ color: '#e0e2ec', font: {{ family: 'JetBrains Mono', size: 11 }} }}
              }}
            }}
          }}
        }});

      }} catch (err) {{
        console.error('Dashboard Init Error:', err);
      }}
    }}

    window.addEventListener('DOMContentLoaded', initDashboard);
  </script>
</body>
</html>"""

def main():
    parser = argparse.ArgumentParser(description="MacroEconomies Generator")
    parser.add_argument("--cron", action="store_true", help="Run in CI/Cron Batch Mode")
    args = parser.parse_args()

    logger.info("========================================================================")
    logger.info("[START] MacroEconomies 파이프라인 가동")
    logger.info(f"Target DB: {DB_PATH}")
    logger.info("========================================================================")

    conn = sqlite3.connect(DB_PATH)
    try:
        init_db(conn)
        fetch_world_bank_data(conn)
        fetch_gscpi_data(conn)
        export_json_and_html(conn)
        logger.info("[SUCCESS] MacroEconomies 대시보드 갱신 성공")
    except Exception as e:
        logger.error(f"[FATAL] 파이프라인 처리 중 치명적 오류 발생: {e}", exc_info=True)
        # CI 중단을 방지하기 위해 정적 파일이 최소 1회라도 생성되었는지 확인 후 종료
        if not HTML_PATH.exists():
            sys.exit(1)
    finally:
        conn.close()

if __name__ == "__main__":
    main()
