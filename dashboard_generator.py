#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Market Intelligence Portal Dashboard Generator
- Data Sources: Shipping Index, Linerlytica, Alphaliner, Forwarder, TAC Index, Bunker Price / Crude Oil
- Output: index.html (Root Portal Dashboard), dashboard_summary.json
- Theme: Bloomberg / MI Terminal Dark Theme
"""

import os
import sys
import json
import sqlite3
import datetime
from pathlib import Path

# Paths Configuration
BASE_DIR = Path(__file__).resolve().parent

DB_PATHS = {
    "shipping": BASE_DIR / "Shipping Index" / "index_history.db",
    "linerlytica": BASE_DIR / "Linerlytica" / "Linerlytica.db",
    "alphaliner": BASE_DIR / "Alphaliner" / "alphaliner_data.db",
    "forwarder": BASE_DIR / "Forwarder" / "freight_forwarders_data.db",
    "tac": BASE_DIR / "TAC Index" / "TAC_Index.db",
    "bunker": BASE_DIR / "BunkerPrice" / "bunker_price_data.db",
}

OUTPUT_HTML = BASE_DIR / "index.html"
OUTPUT_JSON = BASE_DIR / "dashboard_summary.json"

def get_db_connection(db_path: Path):
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn
    except Exception as e:
        print(f"[Warning] Failed to connect to {db_path}: {e}")
        return None

# ==============================================================================
# Data Loaders
# ==============================================================================

def load_shipping_index_data():
    data = {"scfi": {"current": "-", "change": "-", "dir": ""}, "bdi": {"current": "-", "change": "-", "dir": ""}}
    conn = get_db_connection(DB_PATHS["shipping"])
    if not conn:
        return data

    try:
        cur = conn.cursor()
        # SCFI 종합
        cur.execute("""
            SELECT dt, value FROM index_raw
            WHERE index_name = 'SCFI' AND region = '종합'
            ORDER BY dt DESC LIMIT 2
        """)
        scfi_rows = cur.fetchall()
        if scfi_rows:
            curr_val = float(scfi_rows[0]["value"])
            data["scfi"]["current"] = f"{curr_val:,.2f}"
            if len(scfi_rows) > 1:
                prev_val = float(scfi_rows[1]["value"])
                diff = curr_val - prev_val
                pct = (diff / prev_val) * 100 if prev_val else 0
                sign = "+" if diff > 0 else ""
                data["scfi"]["change"] = f"{sign}{diff:,.2f} ({sign}{pct:.2f}%)"
                data["scfi"]["dir"] = "up" if diff > 0 else ("down" if diff < 0 else "neutral")

        # BDI 종합
        cur.execute("""
            SELECT dt, value FROM index_raw
            WHERE index_name = 'BDI' AND region = 'BDI'
            ORDER BY dt DESC LIMIT 2
        """)
        bdi_rows = cur.fetchall()
        if bdi_rows:
            curr_val = float(bdi_rows[0]["value"])
            data["bdi"]["current"] = f"{curr_val:,.0f}"
            if len(bdi_rows) > 1:
                prev_val = float(bdi_rows[1]["value"])
                diff = curr_val - prev_val
                pct = (diff / prev_val) * 100 if prev_val else 0
                sign = "+" if diff > 0 else ""
                data["bdi"]["change"] = f"{sign}{diff:,.0f} ({sign}{pct:.2f}%)"
                data["bdi"]["dir"] = "up" if diff > 0 else ("down" if diff < 0 else "neutral")
    except Exception as e:
        print(f"[Error] load_shipping_index_data: {e}")
    finally:
        conn.close()
    return data

def load_linerlytica_data():
    data = {"date": "-", "fleet": []}
    conn = get_db_connection(DB_PATHS["linerlytica"])
    if not conn:
        return data

    try:
        cur = conn.cursor()
        cur.execute("SELECT MAX(snapshot_date) FROM fleet_snapshot")
        latest_date = cur.fetchone()[0]
        if latest_date:
            data["date"] = latest_date
            cur.execute("""
                SELECT ship_status, ships, teu
                FROM fleet_snapshot
                WHERE snapshot_date = ?
                ORDER BY ships DESC
            """, (latest_date,))
            for row in cur.fetchall():
                data["fleet"].append({
                    "status": row["ship_status"],
                    "ships": f"{row['ships']:,}",
                    "teu": f"{row['teu']:,}"
                })
    except Exception as e:
        print(f"[Error] load_linerlytica_data: {e}")
    finally:
        conn.close()
    return data

def load_alphaliner_data():
    data = {"date": "-", "top5": []}
    conn = get_db_connection(DB_PATHS["alphaliner"])
    if not conn:
        return data

    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='top_carriers'")
        if cur.fetchone():
            cur.execute("SELECT MAX(snapshot_date) FROM top_carriers")
            latest_date = cur.fetchone()[0]
            if latest_date:
                data["date"] = latest_date
                cur.execute("""
                    SELECT rank, carrier_name, total_teu, share_pct
                    FROM top_carriers
                    WHERE snapshot_date = ?
                    ORDER BY rank ASC LIMIT 5
                """, (latest_date,))
                for row in cur.fetchall():
                    data["top5"].append({
                        "rank": row["rank"],
                        "carrier": row["carrier_name"],
                        "teu": f"{row['total_teu']:,}" if isinstance(row['total_teu'], (int, float)) else str(row['total_teu']),
                        "share": f"{row['share_pct']}%"
                    })
    except Exception as e:
        print(f"[Error] load_alphaliner_data: {e}")
    finally:
        conn.close()
    return data

def load_forwarder_data():
    data = {"date": "-", "top5": []}
    conn = get_db_connection(DB_PATHS["forwarder"])
    if not conn:
        return data

    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='forwarders_ranking'")
        if cur.fetchone():
            cur.execute("SELECT MAX(snapshot_year) FROM forwarders_ranking")
            latest_year = cur.fetchone()[0]
            if latest_year:
                data["date"] = str(latest_year)
                cur.execute("""
                    SELECT rank, company_name, ocean_teu, air_metric_tons
                    FROM forwarders_ranking
                    WHERE snapshot_year = ?
                    ORDER BY rank ASC LIMIT 5
                """, (latest_year,))
                for row in cur.fetchall():
                    data["top5"].append({
                        "rank": row["rank"],
                        "company": row["company_name"],
                        "ocean_teu": f"{row['ocean_teu']:,}" if isinstance(row['ocean_teu'], (int, float)) else str(row['ocean_teu']),
                        "air_tons": f"{row['air_metric_tons']:,}" if isinstance(row['air_metric_tons'], (int, float)) else str(row['air_metric_tons'])
                    })
    except Exception as e:
        print(f"[Error] load_forwarder_data: {e}")
    finally:
        conn.close()
    return data

def load_tac_index_data():
    data = {"date": "-", "routes": []}
    conn = get_db_connection(DB_PATHS["tac"])
    if not conn:
        return data

    try:
        cur = conn.cursor()
        cur.execute("SELECT MAX(data_date) FROM tac_master")
        latest_date = cur.fetchone()[0]
        if latest_date:
            data["date"] = latest_date
            cur.execute("""
                SELECT d.route_name, d.rate_usd, d.change_mom, d.change_pct
                FROM tac_detail d
                JOIN tac_master m ON d.snapshot_id = m.id
                WHERE m.data_date = ?
                AND d.route_name IN ('Frankfurt - North America', 'Hong Kong - Europe', 'Hong Kong - North America')
                ORDER BY d.route_name ASC
            """, (latest_date,))
            for row in cur.fetchall():
                rate = float(row["rate_usd"]) if row["rate_usd"] is not None else 0.0
                mom = float(row["change_mom"]) if row["change_mom"] is not None else 0.0
                pct = float(row["change_pct"]) if row["change_pct"] is not None else 0.0

                sign = "+" if mom > 0 else ""
                dir_cls = "up" if mom > 0 else ("down" if mom < 0 else "neutral")
                arrow = "↗" if mom > 0 else ("↘" if mom < 0 else "→")

                data["routes"].append({
                    "name": row["route_name"],
                    "rate": f"${rate:.2f}/kg",
                    "delta": f"{arrow} {sign}{mom:.2f} ({sign}{pct:.1f}%)",
                    "dir": dir_cls
                })
    except Exception as e:
        print(f"[Error] load_tac_index_data: {e}")
    finally:
        conn.close()
    return data

def load_bunker_and_crude_data():
    data = {
        "vlsfo": {"date": "-", "ports": []},
        "crude": {"date": "-", "benchmarks": []}
    }
    conn = get_db_connection(DB_PATHS["bunker"])
    if not conn:
        return data

    try:
        cur = conn.cursor()
        # VLSFO
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='product_vlsfo'")
        if cur.fetchone():
            cur.execute("SELECT MAX(date) FROM product_vlsfo")
            latest_vlsfo_date = cur.fetchone()[0]
            if latest_vlsfo_date:
                data["vlsfo"]["date"] = latest_vlsfo_date
                for port in ["Rotterdam", "Fujairah", "Singapore"]:
                    cur.execute("""
                        SELECT price, change, change_pct
                        FROM product_vlsfo
                        WHERE port = ? AND date = ?
                    """, (port, latest_vlsfo_date))
                    row = cur.fetchone()
                    if row:
                        price = float(row["price"])
                        change = float(row["change"]) if row["change"] is not None else 0.0
                        pct = float(row["change_pct"]) if row["change_pct"] is not None else 0.0
                        arrow = "↗" if change > 0 else ("↘" if change < 0 else "→")
                        dir_cls = "up" if change > 0 else ("down" if change < 0 else "neutral")
                        sign = "+" if change > 0 else ""
                        data["vlsfo"]["ports"].append({
                            "port": port,
                            "price": f"${price:,.1f}",
                            "delta": f"{arrow} {sign}{change:,.1f} ({sign}{pct:.1f}%)",
                            "dir": dir_cls
                        })

        # Crude Oil
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='oil_prices'")
        if cur.fetchone():
            cur.execute("SELECT DATE, Dubai, Brent, WTI FROM oil_prices ORDER BY DATE DESC LIMIT 2")
            crude_rows = cur.fetchall()
            if crude_rows:
                curr_row = crude_rows[0]
                prev_row = crude_rows[1] if len(crude_rows) > 1 else None
                data["crude"]["date"] = curr_row["DATE"]

                for benchmark in ["Dubai", "Brent", "WTI"]:
                    curr_val = float(curr_row[benchmark]) if curr_row[benchmark] is not None else 0.0
                    if prev_row and prev_row[benchmark] is not None:
                        prev_val = float(prev_row[benchmark])
                        diff = curr_val - prev_val
                        pct = (diff / prev_val) * 100 if prev_val else 0
                        sign = "+" if diff > 0 else ""
                        arrow = "↗" if diff > 0 else ("↘" if diff < 0 else "→")
                        dir_cls = "up" if diff > 0 else ("down" if diff < 0 else "neutral")
                        delta_str = f"{arrow} {sign}{diff:,.2f} ({sign}{pct:.2f}%)"
                    else:
                        delta_str = "-"
                        dir_cls = "neutral"

                    data["crude"]["benchmarks"].append({
                        "name": benchmark,
                        "price": f"${curr_val:,.2f}",
                        "delta": delta_str,
                        "dir": dir_cls
                    })
    except Exception as e:
        print(f"[Error] load_bunker_and_crude_data: {e}")
    finally:
        conn.close()
    return data

# ==============================================================================
# HTML Template Renderer
# ==============================================================================

def generate_portal_html(data: dict) -> str:
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Linerlytica Table
    fleet_rows = ""
    for item in data["linerlytica"]["fleet"]:
        fleet_rows += f"""
        <tr>
            <td class="status-col">{item['status']}</td>
            <td class="num-col">{item['ships']}</td>
            <td class="num-col">{item['teu']}</td>
        </tr>
        """
    if not fleet_rows:
        fleet_rows = "<tr><td colspan='3' class='empty-row'>데이터가 없습니다.</td></tr>"

    # Alphaliner Table
    alphaliner_rows = ""
    for item in data["alphaliner"]["top5"]:
        alphaliner_rows += f"""
        <tr>
            <td class="rank-col">{item['rank']}</td>
            <td class="carrier-col">{item['carrier']}</td>
            <td class="num-col">{item['teu']}</td>
            <td class="num-col highlight-orange">{item['share']}</td>
        </tr>
        """
    if not alphaliner_rows:
        alphaliner_rows = "<tr><td colspan='4' class='empty-row'>데이터가 없습니다.</td></tr>"

    # Forwarder Table
    forwarder_rows = ""
    for item in data["forwarder"]["top5"]:
        forwarder_rows += f"""
        <tr>
            <td class="rank-col">{item['rank']}</td>
            <td class="carrier-col">{item['company']}</td>
            <td class="num-col">{item['ocean_teu']}</td>
            <td class="num-col highlight-blue">{item['air_tons']}</td>
        </tr>
        """
    if not forwarder_rows:
        forwarder_rows = "<tr><td colspan='4' class='empty-row'>데이터가 없습니다.</td></tr>"

    # TAC Index Rows
    tac_items = ""
    for r in data["tac"]["routes"]:
        tac_items += f"""
        <div class="kpi-metric-box">
            <div class="metric-label">{r['name']}</div>
            <div class="metric-val">{r['rate']}</div>
            <div class="metric-delta {r['dir']}">{r['delta']}</div>
        </div>
        """
    if not tac_items:
        tac_items = "<div class='empty-row'>TAC Air 운임 데이터를 조회할 수 없습니다.</div>"

    # VLSFO Items
    vlsfo_items = ""
    for p in data["bunker"]["vlsfo"]["ports"]:
        vlsfo_items += f"""
        <div class="kpi-metric-box">
            <div class="metric-label">{p['port']} VLSFO</div>
            <div class="metric-val">{p['price']}</div>
            <div class="metric-delta {p['dir']}">{p['delta']}</div>
        </div>
        """
    if not vlsfo_items:
        vlsfo_items = "<div class='empty-row'>VLSFO 데이터를 조회할 수 없습니다.</div>"

    # Crude Oil Items
    crude_items = ""
    for c in data["bunker"]["crude"]["benchmarks"]:
        crude_items += f"""
        <div class="kpi-metric-box">
            <div class="metric-label">{c['name']} Crude</div>
            <div class="metric-val">{c['price']}</div>
            <div class="metric-delta {c['dir']}">{c['delta']}</div>
        </div>
        """
    if not crude_items:
        crude_items = "<div class='empty-row'>원유 데이터를 조회할 수 없습니다.</div>"

    html_content = f"""<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MI GLOBAL TERMINAL</title>
    <style>
        :root {{
            --bg: #02060b;
            --panel: #070d15;
            --panel-border: #132238;
            --panel-header: #0c1726;
            --orange: #ff8a00;
            --green: #00d26a;
            --red: #ff4d4f;
            --blue: #1890ff;
            --text-primary: #e6edf3;
            --text-secondary: #8b949e;
            --text-muted: #546274;
            --font-mono: 'Consolas', 'Courier New', Courier, monospace;
            --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        }}

        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}

        body {{
            background-color: var(--bg);
            color: var(--text-primary);
            font-family: var(--font-sans);
            padding: 20px;
            font-size: 13px;
        }}

        /* Terminal Top Bar */
        .terminal-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 12px 20px;
            background-color: var(--panel);
            border: 1px solid var(--panel-border);
            border-left: 4px solid var(--orange);
            margin-bottom: 20px;
        }}

        .brand-title {{
            font-family: var(--font-mono);
            font-size: 16px;
            font-weight: bold;
            color: var(--orange);
            letter-spacing: 1px;
        }}

        .header-meta {{
            font-family: var(--font-mono);
            color: var(--text-secondary);
            font-size: 12px;
        }}

        /* Section Headings */
        .section-title {{
            font-family: var(--font-mono);
            font-size: 14px;
            font-weight: bold;
            color: var(--orange);
            margin-bottom: 12px;
            text-transform: uppercase;
            letter-spacing: 1px;
            display: flex;
            align-items: center;
            gap: 8px;
        }}

        .section-title::after {{
            content: '';
            flex: 1;
            height: 1px;
            background: var(--panel-border);
        }}

        /* Layout Grids */
        .grid-ocean {{
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 16px;
            margin-bottom: 24px;
        }}

        /* 02. AIR and 03. MACRO Side-by-Side */
        .grid-air-macro {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 16px;
            margin-bottom: 24px;
        }}

        .grid-inner-macro {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 12px;
        }}

        @media (max-width: 1200px) {{
            .grid-ocean {{
                grid-template-columns: repeat(2, 1fr);
            }}
            .grid-air-macro {{
                grid-template-columns: 1fr;
            }}
        }}

        @media (max-width: 768px) {{
            .grid-ocean {{
                grid-template-columns: 1fr;
            }}
            .grid-inner-macro {{
                grid-template-columns: 1fr;
            }}
        }}

        /* Terminal Panel / Card */
        .card {{
            background-color: var(--panel);
            border: 1px solid var(--panel-border);
            border-radius: 2px;
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }}

        .card-header {{
            background-color: var(--panel-header);
            padding: 10px 14px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--panel-border);
        }}

        .card-title {{
            font-family: var(--font-mono);
            font-size: 12px;
            font-weight: bold;
            color: var(--text-primary);
        }}

        .card-link {{
            color: var(--orange);
            text-decoration: none;
            font-family: var(--font-mono);
            font-size: 11px;
        }}
        .card-link:hover {{
            text-decoration: underline;
        }}

        .card-body {{
            padding: 14px;
            flex: 1;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
        }}

        /* Tables */
        table {{
            width: 100%;
            border-collapse: collapse;
            font-family: var(--font-mono);
            font-size: 12px;
        }}

        th {{
            color: var(--text-secondary);
            font-weight: normal;
            text-align: left;
            padding-bottom: 8px;
            border-bottom: 1px solid var(--panel-border);
        }}

        td {{
            padding: 8px 0;
            border-bottom: 1px solid rgba(19, 34, 56, 0.4);
            color: var(--text-primary);
        }}

        .rank-col {{ width: 28px; color: var(--orange); }}
        .status-col {{ color: var(--text-secondary); }}
        .carrier-col {{ font-weight: 500; }}
        .num-col {{ text-align: right; }}
        .highlight-orange {{ color: var(--orange); font-weight: bold; }}
        .highlight-blue {{ color: var(--blue); font-weight: bold; }}

        /* KPI Boxes */
        .kpi-row {{
            display: flex;
            gap: 12px;
        }}

        .kpi-box {{
            flex: 1;
            background: rgba(12, 23, 38, 0.5);
            padding: 12px;
            border: 1px solid var(--panel-border);
            text-align: center;
        }}

        .kpi-label {{
            font-family: var(--font-mono);
            font-size: 11px;
            color: var(--text-secondary);
            margin-bottom: 6px;
        }}

        .kpi-value {{
            font-family: var(--font-mono);
            font-size: 20px;
            font-weight: bold;
            color: #fff;
            margin-bottom: 4px;
        }}

        .metric-delta {{
            font-family: var(--font-mono);
            font-size: 11px;
            font-weight: bold;
        }}

        .up {{ color: var(--green); }}
        .down {{ color: var(--red); }}
        .neutral {{ color: var(--text-muted); }}

        /* Metric Stack */
        .kpi-metric-box {{
            background: rgba(12, 23, 38, 0.5);
            border: 1px solid var(--panel-border);
            padding: 10px 12px;
            margin-bottom: 8px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .kpi-metric-box:last-child {{
            margin-bottom: 0;
        }}

        .metric-label {{
            font-family: var(--font-mono);
            font-size: 11px;
            color: var(--text-secondary);
        }}

        .metric-val {{
            font-family: var(--font-mono);
            font-size: 14px;
            font-weight: bold;
            color: #fff;
        }}

        .empty-row {{
            text-align: center;
            color: var(--text-muted);
            padding: 16px 0;
            font-family: var(--font-mono);
            font-size: 11px;
        }}

        /* Footer */
        .terminal-footer {{
            text-align: center;
            font-family: var(--font-mono);
            font-size: 11px;
            color: var(--text-muted);
            margin-top: 30px;
            padding-top: 15px;
            border-top: 1px solid var(--panel-border);
        }}
    </style>
</head>
<body>

    <!-- Header -->
    <div class="terminal-header">
        <div class="brand-title">MI&lt;GO&gt; | MARKET INTELLIGENCE PORTAL</div>
        <div class="header-meta">SYS.TIME: {now_str} KST | TERMINAL ACTIVE</div>
    </div>

    <!-- 01. OCEAN SECTION -->
    <div class="section-title">01. OCEAN INTELLIGENCE</div>
    <div class="grid-ocean">
        <!-- Card 1: Shipping Index -->
        <div class="card">
            <div class="card-header">
                <span class="card-title">SHIPPING INDEX</span>
                <a href="Shipping Index/index.html" class="card-link">[DETAIL]</a>
            </div>
            <div class="card-body">
                <div class="kpi-row">
                    <div class="kpi-box">
                        <div class="kpi-label">SCFI COMPOSITE</div>
                        <div class="kpi-value">{data['shipping']['scfi']['current']}</div>
                        <div class="metric-delta {data['shipping']['scfi']['dir']}">{data['shipping']['scfi']['change']}</div>
                    </div>
                    <div class="kpi-box">
                        <div class="kpi-label">BDI COMPOSITE</div>
                        <div class="kpi-value">{data['shipping']['bdi']['current']}</div>
                        <div class="metric-delta {data['shipping']['bdi']['dir']}">{data['shipping']['bdi']['change']}</div>
                    </div>
                </div>
            </div>
        </div>

        <!-- Card 2: Linerlytica -->
        <div class="card">
            <div class="card-header">
                <span class="card-title">LINERLYTICA FLEET SNAPSHOT</span>
                <a href="Linerlytica/index.html" class="card-link">[DETAIL]</a>
            </div>
            <div class="card-body">
                <table>
                    <thead>
                        <tr>
                            <th>Status</th>
                            <th class="num-col">Ships</th>
                            <th class="num-col">TEU</th>
                        </tr>
                    </thead>
                    <tbody>
                        {fleet_rows}
                    </tbody>
                </table>
            </div>
        </div>

        <!-- Card 3: Alphaliner -->
        <div class="card">
            <div class="card-header">
                <span class="card-title">ALPHALINER TOP 5</span>
                <a href="Alphaliner/index.html" class="card-link">[DETAIL]</a>
            </div>
            <div class="card-body">
                <table>
                    <thead>
                        <tr>
                            <th>#</th>
                            <th>Carrier</th>
                            <th class="num-col">TEU</th>
                            <th class="num-col">Share</th>
                        </tr>
                    </thead>
                    <tbody>
                        {alphaliner_rows}
                    </tbody>
                </table>
            </div>
        </div>

        <!-- Card 4: Forwarder -->
        <div class="card">
            <div class="card-header">
                <span class="card-title">FORWARDER TOP 5</span>
                <a href="Forwarder/index.html" class="card-link">[DETAIL]</a>
            </div>
            <div class="card-body">
                <table>
                    <thead>
                        <tr>
                            <th>#</th>
                            <th>Forwarder</th>
                            <th class="num-col">Ocean TEU</th>
                            <th class="num-col">Air (T)</th>
                        </tr>
                    </thead>
                    <tbody>
                        {forwarder_rows}
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <!-- 02. AIR & 03. MACRO SIDE-BY-SIDE GRID -->
    <div class="grid-air-macro">
        <!-- 02. AIR -->
        <div class="card">
            <div class="card-header">
                <span class="card-title" style="color: var(--orange);">02. AIR FREIGHT (TAC INDEX)</span>
                <a href="TAC Index/index.html" class="card-link">[DETAIL]</a>
            </div>
            <div class="card-body">
                {tac_items}
            </div>
        </div>

        <!-- 03. MACRO & ENERGY -->
        <div class="card">
            <div class="card-header">
                <span class="card-title" style="color: var(--orange);">03. MACRO & ENERGY</span>
                <a href="BunkerPrice/index.html" class="card-link">[DETAIL]</a>
            </div>
            <div class="card-body">
                <div class="grid-inner-macro">
                    <div>
                        <div class="kpi-label" style="text-align: left; margin-bottom: 6px; font-weight: bold; color: var(--text-primary);">VLSFO BENCHMARKS</div>
                        {vlsfo_items}
                    </div>
                    <div>
                        <div class="kpi-label" style="text-align: left; margin-bottom: 6px; font-weight: bold; color: var(--text-primary);">CRUDE BENCHMARKS</div>
                        {crude_items}
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- Footer -->
    <div class="terminal-footer">
        MI TERMINAL PLATFORM &bull; AUTOMATED INTELLIGENCE AGGREGATOR &bull; &copy; {datetime.datetime.now().year}
    </div>

</body>
</html>
"""
    return html_content

# ==============================================================================
# Main Orchestrator
# ==============================================================================

def main():
    print("=" * 60)
    print("Generating MI Portal Dashboard & Summary...")
    print("=" * 60)

    # 1. Load Data
    dashboard_data = {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "shipping": load_shipping_index_data(),
        "linerlytica": load_linerlytica_data(),
        "alphaliner": load_alphaliner_data(),
        "forwarder": load_forwarder_data(),
        "tac": load_tac_index_data(),
        "bunker": load_bunker_and_crude_data(),
    }

    # 2. Write JSON Summary
    try:
        with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
            json.dump(dashboard_data, f, ensure_ascii=False, indent=2)
        print(f"[Success] Generated JSON: {OUTPUT_JSON}")
    except Exception as e:
        print(f"[Error] Failed to write JSON: {e}")

    # 3. Generate & Write HTML Dashboard
    try:
        html_rendered = generate_portal_html(dashboard_data)
        with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
            f.write(html_rendered)
        print(f"[Success] Generated Dashboard: {OUTPUT_HTML}")
    except Exception as e:
        print(f"[Error] Failed to write HTML: {e}")
        sys.exit(1)

    print("=" * 60)
    print("MI Portal Dashboard Build Completed Successfully.")
    print("=" * 60)

if __name__ == "__main__":
    main()
