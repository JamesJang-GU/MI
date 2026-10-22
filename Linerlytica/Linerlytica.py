#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Linerlytica Global Containership Fleet Snapshot Crawler & Dashboard Generator
- 대상: 매월 17일자 스냅샷 (Global Containership Fleet Snapshot)
- 저장소: Linerlytica.db (SQLite)
- 결과물: index.html (Static Bloomberg/MI Terminal-style Dashboard)
"""

import os
import re
import json
import sqlite3
import datetime
from datetime import datetime as dt
from dateutil.relativedelta import relativedelta
from bs4 import BeautifulSoup

# Headless 브라우저 (Playwright 우선, 미설치 시 fallback/requests 안내)
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    import requests

# ==========================================
# 1. 환경 및 경로 설정
# ==========================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "Linerlytica.db")
HTML_PATH = os.path.join(BASE_DIR, "index.html")
TARGET_URL = "https://www.linerlytica.com/"  # 또는 실제 스냅샷 대시보드 페이지 URL

START_DATE = "2024-01-17"  # 백필 시작 기준일 (필요 시 수정 가능)

# ==========================================
# 2. SQLite 데이터베이스 관리
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS fleet_snapshot (
            snapshot_date TEXT NOT NULL,
            ship_status TEXT NOT NULL,
            ships INTEGER NOT NULL,
            teu INTEGER NOT NULL,
            source_url TEXT,
            collected_at TEXT NOT NULL,
            PRIMARY KEY (snapshot_date, ship_status)
        );
    """)
    conn.commit()
    return conn

def get_existing_dates(conn):
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT snapshot_date FROM fleet_snapshot")
    rows = cur.fetchall()
    return set(r[0] for r in rows)

def save_snapshot(conn, snapshot_date, data_rows, source_url):
    cur = conn.cursor()
    collected_at = dt.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    for row in data_rows:
        cur.execute("""
            INSERT OR REPLACE INTO fleet_snapshot
            (snapshot_date, ship_status, ships, teu, source_url, collected_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            snapshot_date,
            row["ship_status"],
            int(row["ships"]),
            int(row["teu"]),
            source_url,
            collected_at
        ))
    conn.commit()
    print(f"  [DB Saved] {snapshot_date}: {len(data_rows)} status rows updated.")

# ==========================================
# 3. 크롤러 로직 (Playwright 기준)
# ==========================================
def parse_snapshot_table_html(html_text):
    """
    HTML 문자열에서 'Global Containership Fleet Snapshot' 표 데이터를 추출
    """
    soup = BeautifulSoup(html_text, "html.parser")
    rows_data = []

    # 테이블 검색 (클래스나 구조에 따라 유연하게 매칭)
    tables = soup.find_all("table")
    for table in tables:
        # 헤더나 본문에 Ships / TEU 관련 키워드가 있는지 확인
        text = table.get_text()
        if "Ships at port" in text or "Active Ships" in text:
            for tr in table.find_all("tr"):
                tds = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
                if len(tds) >= 3:
                    status = tds[0]
                    # 정규식을 통해 숫자 및 콤마만 파싱
                    ships_match = re.sub(r"[^\d]", "", tds[1])
                    teu_match = re.sub(r"[^\d]", "", tds[2])

                    if ships_match and teu_match and status not in ["Ship Status", "Total", ""]:
                        rows_data.append({
                            "ship_status": status,
                            "ships": int(ships_match),
                            "teu": int(teu_match)
                        })
            if rows_data:
                break

    # 테이블 형태가 아닌 div/card 리스트인 경우 fallback 파싱
    if not rows_data:
        target_statuses = [
            "Ships at port",
            "Active Ships",
            "Inactive Ships",
            "Ships at anchorage",
            "Ships in shipyard"
        ]
        for st in target_statuses:
            pattern = re.compile(rf"{re.escape(st)}[\s\S]*?([\d,]+)[\s\S]*?([\d,]+)", re.IGNORECASE)
            match = pattern.search(soup.get_text())
            if match:
                ships_val = int(match.group(1).replace(",", ""))
                teu_val = int(match.group(2).replace(",", ""))
                rows_data.append({
                    "ship_status": st,
                    "ships": ships_val,
                    "teu": teu_val
                })

    return rows_data

def crawl_and_backfill(conn):
    existing_dates = get_existing_dates(conn)

    # 시작일부터 현재까지의 매월 17일 리스트 계산
    start_dt = dt.strptime(START_DATE, "%Y-%m-%d")
    today = dt.utcnow()

    target_dates = []
    curr = start_dt
    while curr <= today:
        target_dates.append(curr.strftime("%Y-%m-%d"))
        curr += relativedelta(months=1)

    missing_dates = [d for d in target_dates if d not in existing_dates]

    if not missing_dates:
        print("[Crawler] All monthly 17th snapshots are up to date.")
        return

    print(f"[Crawler] Found {len(missing_dates)} missing snapshot dates: {missing_dates}")

    if not PLAYWRIGHT_AVAILABLE:
        print("[Warning] Playwright is not installed. Trying simple HTTP GET for current page...")
        res = requests.get(TARGET_URL, headers={"User-Agent": "Mozilla/5.0"})
        if res.status_code == 200:
            parsed = parse_snapshot_table_html(res.text)
            if parsed:
                # 당월 17일 또는 최신 스냅샷
                latest_target = missing_dates[-1]
                save_snapshot(conn, latest_target, parsed, TARGET_URL)
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        try:
            page.goto(TARGET_URL, wait_until="networkidle", timeout=45000)

            for target_date in missing_dates:
                print(f"[Crawler] Processing target date: {target_date}...")

                # 날짜 선택기(Datepicker/Calendar/Select) 조작
                # 1) input 태그가 존재하는 경우 날짜 입력 시도
                date_input = page.query_selector("input[type='date'], input.datepicker, input#date")
                if date_input:
                    date_input.fill(target_date)
                    page.keyboard.press("Enter")
                    page.wait_for_timeout(3000)

                # 2) 페이지 내용 파싱
                html_content = page.content()
                data_rows = parse_snapshot_table_html(html_content)

                if data_rows:
                    save_snapshot(conn, target_date, data_rows, TARGET_URL)
                else:
                    print(f"  [Notice] No available table data for {target_date} (Site may not have published this date).")

        except Exception as e:
            print(f"[Error during crawling] {e}")
        finally:
            browser.close()

# ==========================================
# 4. 블룸버그/MI 터미널 스타일 HTML 대시보드 생성
# ==========================================
def generate_dashboard_html(conn):
    cur = conn.cursor()

    # 1) 사용 가능한 모든 날짜 목록 (내림차순)
    cur.execute("SELECT DISTINCT snapshot_date FROM fleet_snapshot ORDER BY snapshot_date DESC")
    all_dates = [r[0] for r in cur.fetchall()]

    if not all_dates:
        print("[Dashboard] No data in DB to render dashboard.")
        return

    latest_date = all_dates[0]
    prev_date = all_dates[1] if len(all_dates) > 1 else None

    # 2) 최신 및 이전 스냅샷 데이터 조회
    cur.execute("SELECT ship_status, ships, teu FROM fleet_snapshot WHERE snapshot_date = ?", (latest_date,))
    latest_rows = {r[0]: {"ships": r[1], "teu": r[2]} for r in cur.fetchall()}

    prev_rows = {}
    if prev_date:
        cur.execute("SELECT ship_status, ships, teu FROM fleet_snapshot WHERE snapshot_date = ?", (prev_date,))
        prev_rows = {r[0]: {"ships": r[1], "teu": r[2]} for r in cur.fetchall()}

    # 3) 시계열 트렌드 데이터 (전체 날짜 기준, 오름차순)
    cur.execute("""
        SELECT snapshot_date, ship_status, ships, teu
        FROM fleet_snapshot
        ORDER BY snapshot_date ASC
    """)
    trend_raw = cur.fetchall()

    trend_dates = sorted(list(set(r[0] for r in trend_raw)))
    statuses = ["Ships at port", "Active Ships", "Inactive Ships", "Ships at anchorage", "Ships in shipyard"]

    # Chart.js용 Series 데이터 매핑
    teu_series = {st: [] for st in statuses}
    ships_series = {st: [] for st in statuses}

    data_map = {(r[0], r[1]): (r[2], r[3]) for r in trend_raw}
    for d in trend_dates:
        for st in statuses:
            val = data_map.get((d, st), (0, 0))
            ships_series[st].append(val[0])
            teu_series[st].append(val[1])

    # 4) 카드 및 테이블 HTML 생성
    table_rows_html = ""
    total_ships = sum(v["ships"] for v in latest_rows.values())
    total_teu = sum(v["teu"] for v in latest_rows.values())

    for st in statuses:
        latest_val = latest_rows.get(st, {"ships": 0, "teu": 0})
        prev_val = prev_rows.get(st, {"ships": 0, "teu": 0})

        # Delta 계산
        diff_ships = latest_val["ships"] - prev_val["ships"] if prev_date else 0
        diff_teu = latest_val["teu"] - prev_val["teu"] if prev_date else 0

        def format_delta(delta):
            if not prev_date or delta == 0:
                return '<span class="neutral">-</span>'
            elif delta > 0:
                return f'<span class="up">▲ +{delta:,}</span>'
            else:
                return f'<span class="down">▼ {delta:,}</span>'

        table_rows_html += f"""
        <tr>
            <td class="status-name">{st}</td>
            <td class="num">{latest_val['ships']:,}</td>
            <td class="delta">{format_delta(diff_ships)}</td>
            <td class="num">{latest_val['teu']:,}</td>
            <td class="delta">{format_delta(diff_teu)}</td>
        </tr>
        """

    # HTML 템플릿 (블룸버그 터미널 다크 테마)
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Linerlytica | Fleet Snapshot Terminal</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        :root {{
            --bg-color: #0c1017;
            --card-bg: #151b26;
            --border-color: #242c3d;
            --text-main: #e2e8f0;
            --text-muted: #8492a6;
            --accent-orange: #ff9800;
            --accent-blue: #00bcd4;
            --up-green: #00e676;
            --down-red: #ff5252;
            --font-mono: 'SF Pro Mono', 'Consolas', 'Menlo', monospace;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            background-color: var(--bg-color);
            color: var(--text-main);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            padding: 24px;
        }}
        .header {{
            display: flex;
            justify-content: space-between;
            align-items: flex-end;
            border-bottom: 2px solid var(--accent-orange);
            padding-bottom: 12px;
            margin-bottom: 24px;
        }}
        .title-area h1 {{
            font-size: 22px;
            font-weight: 700;
            letter-spacing: 0.5px;
            color: #ffffff;
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        .title-area h1::before {{
            content: "";
            display: inline-block;
            width: 10px;
            height: 10px;
            background: var(--accent-orange);
        }}
        .subtitle {{
            font-size: 13px;
            color: var(--text-muted);
            margin-top: 4px;
            font-family: var(--font-mono);
        }}
        .grid {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
        }}
        @media (max-width: 1024px) {{
            .grid {{ grid-template-columns: 1fr; }}
        }}
        .card {{
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 4px;
            padding: 18px;
        }}
        .card-header {{
            font-size: 14px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.8px;
            color: var(--accent-orange);
            margin-bottom: 14px;
            display: flex;
            justify-content: space-between;
        }}
        .summary-bar {{
            display: flex;
            gap: 16px;
            margin-bottom: 16px;
        }}
        .kpi {{
            flex: 1;
            background: rgba(255, 255, 255, 0.03);
            border-left: 3px solid var(--accent-blue);
            padding: 10px 14px;
        }}
        .kpi-label {{
            font-size: 11px;
            color: var(--text-muted);
            text-transform: uppercase;
        }}
        .kpi-value {{
            font-size: 20px;
            font-weight: 700;
            font-family: var(--font-mono);
            color: #fff;
            margin-top: 2px;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
            font-family: var(--font-mono);
        }}
        th {{
            text-align: left;
            padding: 8px 10px;
            background: rgba(255, 255, 255, 0.02);
            color: var(--text-muted);
            font-weight: 600;
            border-bottom: 1px solid var(--border-color);
        }}
        td {{
            padding: 10px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.04);
        }}
        .num {{ text-align: right; }}
        .delta {{ text-align: right; font-size: 11px; width: 90px; }}
        .up {{ color: var(--up-green); }}
        .down {{ color: var(--down-red); }}
        .neutral {{ color: var(--text-muted); }}
        .status-name {{ font-weight: 600; color: #fff; }}
        .chart-container {{
            position: relative;
            height: 320px;
            width: 100%;
        }}
    </style>
</head>
<body>
    <div class="header">
        <div class="title-area">
            <h1>LINERLYTICA FLEET SNAPSHOT</h1>
            <div class="subtitle">MONTHLY 17TH CYCLE TRACKER | SOURCE: LINERLYTICA.COM</div>
        </div>
        <div class="subtitle">
            LATEST: <strong>{latest_date}</strong> (PREV: {prev_date or 'N/A'})
        </div>
    </div>

    <div class="summary-bar">
        <div class="kpi">
            <div class="kpi-label">Total Fleet Tracked</div>
            <div class="kpi-value">{total_ships:,} Ships</div>
        </div>
        <div class="kpi">
            <div class="kpi-label">Total Fleet Capacity</div>
            <div class="kpi-value">{total_teu:,} TEU</div>
        </div>
        <div class="kpi">
            <div class="kpi-label">Active Ratio</div>
            <div class="kpi-value">{(latest_rows.get('Active Ships', {}).get('teu', 0) / (total_teu or 1) * 100):.1f}%</div>
        </div>
    </div>

    <div class="grid">
        <div class="card">
            <div class="card-header">
                <span>Current Fleet Status</span>
                <span>As of {latest_date}</span>
            </div>
            <table>
                <thead>
                    <tr>
                        <th>Ship Status</th>
                        <th class="num">Ships</th>
                        <th class="delta">MoM</th>
                        <th class="num">TEU</th>
                        <th class="delta">MoM</th>
                    </tr>
                </thead>
                <tbody>
                    {table_rows_html}
                </tbody>
            </table>
        </div>

        <div class="card">
            <div class="card-header">
                <span>Historical Capacity Trend (TEU)</span>
                <span>Monthly 17th History</span>
            </div>
            <div class="chart-container">
                <canvas id="teuChart"></canvas>
            </div>
        </div>
    </div>

    <script>
        const ctx = document.getElementById('teuChart').getContext('2d');
        const trendDates = {json.dumps(trend_dates)};

        new Chart(ctx, {{
            type: 'line',
            data: {{
                labels: trendDates,
                datasets: [
                    {{
                        label: 'Active Ships',
                        data: {json.dumps(teu_series['Active Ships'])},
                        borderColor: '#00e676',
                        backgroundColor: 'transparent',
                        borderWidth: 2,
                        tension: 0.1
                    }},
                    {{
                        label: 'Ships at port',
                        data: {json.dumps(teu_series['Ships at port'])},
                        borderColor: '#ff9800',
                        backgroundColor: 'transparent',
                        borderWidth: 1.5,
                        tension: 0.1
                    }},
                    {{
                        label: 'Ships at anchorage',
                        data: {json.dumps(teu_series['Ships at anchorage'])},
                        borderColor: '#00bcd4',
                        backgroundColor: 'transparent',
                        borderWidth: 1.5,
                        tension: 0.1
                    }},
                    {{
                        label: 'Inactive Ships',
                        data: {json.dumps(teu_series['Inactive Ships'])},
                        borderColor: '#ff5252',
                        backgroundColor: 'transparent',
                        borderWidth: 1.5,
                        tension: 0.1
                    }}
                ]
            }},
            options: {{
                responsive: true,
                maintainAspectRatio: false,
                scales: {{
                    x: {{
                        grid: {{ color: 'rgba(255,255,255,0.05)' }},
                        ticks: {{ color: '#8492a6', font: {{ family: 'monospace' }} }}
                    }},
                    y: {{
                        grid: {{ color: 'rgba(255,255,255,0.05)' }},
                        ticks: {{
                            color: '#8492a6',
                            font: {{ family: 'monospace' }},
                            callback: function(val) {{ return (val / 1000000).toFixed(1) + 'M'; }}
                        }}
                    }}
                }},
                plugins: {{
                    legend: {{
                        labels: {{ color: '#e2e8f0', boxWidth: 12 }}
                    }}
                }}
            }}
        }});
    </script>
</body>
</html>
"""

    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"[Dashboard] Successfully generated static dashboard at: {HTML_PATH}")

# ==========================================
# 5. 엔트리포인트 실행
# ==========================================
if __name__ == "__main__":
    print("=" * 60)
    print("[Linerlytica Module Execution Started]")
    print(f"Working Directory: {BASE_DIR}")

    # 1. DB 초기화
    conn = init_db()

    # 2. 17일 기준 백필 및 크롤링 실행
    crawl_and_backfill(conn)

    # 3. 최신 데이터를 기반으로 HTML 대시보드 재생성
    generate_dashboard_html(conn)

    conn.close()
    print("[Linerlytica Module Execution Completed]")
    print("=" * 60)
