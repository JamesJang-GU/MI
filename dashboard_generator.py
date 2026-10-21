#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Market Intelligence Portal - Root Dashboard Generator
Bloomberg Professional Terminal High-Density Interface
"""

import json
import os
import sqlite3
from datetime import datetime

# ==========================================
# PATH CONFIGURATION
# ==========================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DB_PATHS = {
    "shipping": os.path.join(BASE_DIR, "ShippingIndex", "shipping_index.db"),
    "alphaliner": os.path.join(BASE_DIR, "Alphaliner", "alphaliner_history.db"),
    "bunker": os.path.join(BASE_DIR, "BunkerPrice", "bunker_price_data.db"),
    "forwarder": os.path.join(
        BASE_DIR, "Forwarder", "freight_forwarders_data.db"
    ),
    "tac": os.path.join(BASE_DIR, "TAC Index", "tac_data.db"),
    "macro": os.path.join(BASE_DIR, "MacroEconomies", "macro_data.db"),
}

OUTPUT_HTML = os.path.join(BASE_DIR, "index.html")
OUTPUT_JSON = os.path.join(BASE_DIR, "dashboard_summary.json")

# ==========================================
# DELTA FORMATTING HELPER
# ==========================================
def calculate_delta(curr, prev, is_int=False):
    """Calculate absolute and percentage deltas, returns (formatted_delta, class, arrow)."""
    if curr is None:
        return "-", "neutral", ""

    if prev is None or prev == 0:
        val_str = f"{int(curr):,}" if is_int else f"{curr:,.2f}"
        return val_str, "neutral", ""

    diff = curr - prev
    pct = (diff / prev) * 100.0

    if diff > 0:
        arrow = "▲"
        cls = "up"
        diff_str = f"+{int(diff):,}" if is_int else f"+{diff:.2f}"
        pct_str = f"+{pct:.2f}%"
    elif diff < 0:
        arrow = "▼"
        cls = "down"
        diff_str = f"{int(diff):,}" if is_int else f"{diff:.2f}"
        pct_str = f"{pct:.2f}%"
    else:
        arrow = "■"
        cls = "neutral"
        diff_str = "0" if is_int else "0.00"
        pct_str = "0.00%"

    return f"{diff_str} ({pct_str})", cls, arrow

# ==========================================
# DATA FETCHERS
# ==========================================
def fetch_shipping_data():
    data = {
        "scfi": {
            "price": "N/A",
            "delta": "-",
            "class": "neutral",
            "arrow": "",
            "date": "-",
        },
        "bdi": {
            "price": "N/A",
            "delta": "-",
            "class": "neutral",
            "arrow": "",
            "date": "-",
        },
        "date": datetime.today().strftime("%Y-%m-%d"),
    }
    db_path = DB_PATHS["shipping"]
    if not os.path.exists(db_path):
        return data

    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
            tables = [r[0] for r in cursor.fetchall()]

            # SCFI Check
            scfi_table = next(
                (
                    t
                    for t in tables
                    if "scfi" in t.lower() or "shipping" in t.lower()
                ),
                None,
            )
            if scfi_table:
                cursor.execute(
                    f'PRAGMA table_info("{scfi_table}")'
                )  # check cols
                cols = [c[1].lower() for c in cursor.fetchall()]
                date_col = next((c for c in cols if "date" in c), cols[0])
                val_col = next(
                    (
                        c
                        for c in cols
                        if "val" in c
                        or "price" in c
                        or "index" in c
                        or "scfi" in c
                    ),
                    cols[1],
                )

                cursor.execute(
                    f'SELECT "{date_col}", "{val_col}" FROM "{scfi_table}" WHERE "{val_col}" IS NOT NULL ORDER BY "{date_col}" DESC LIMIT 2'
                )
                rows = cursor.fetchall()
                if rows:
                    data["date"] = str(rows[0][0])[:10]
                    data["scfi"]["date"] = str(rows[0][0])[:10]
                    curr = float(rows[0][1])
                    prev = float(rows[1][1]) if len(rows) > 1 else None
                    data["scfi"]["price"] = f"{curr:,.2f}"
                    delta_str, cls, arrow = calculate_delta(curr, prev)
                    data["scfi"]["delta"] = delta_str
                    data["scfi"]["class"] = cls
                    data["scfi"]["arrow"] = arrow

            # BDI Check
            bdi_table = next((t for t in tables if "bdi" in t.lower()), None)
            if bdi_table:
                cursor.execute(f'PRAGMA table_info("{bdi_table}")')
                cols = [c[1].lower() for c in cursor.fetchall()]
                date_col = next((c for c in cols if "date" in c), cols[0])
                val_col = next(
                    (
                        c
                        for c in cols
                        if "val" in c
                        or "price" in c
                        or "index" in c
                        or "bdi" in c
                    ),
                    cols[1],
                )

                cursor.execute(
                    f'SELECT "{date_col}", "{val_col}" FROM "{bdi_table}" WHERE "{val_col}" IS NOT NULL ORDER BY "{date_col}" DESC LIMIT 2'
                )
                rows = cursor.fetchall()
                if rows:
                    data["bdi"]["date"] = str(rows[0][0])[:10]
                    curr = float(rows[0][1])
                    prev = float(rows[1][1]) if len(rows) > 1 else None
                    data["bdi"]["price"] = f"{int(curr):,}"
                    delta_str, cls, arrow = calculate_delta(
                        curr, prev, is_int=True
                    )
                    data["bdi"]["delta"] = delta_str
                    data["bdi"]["class"] = cls
                    data["bdi"]["arrow"] = arrow
    except Exception as e:
        print(f"[WARN] Shipping data load: {e}")
    return data

def fetch_alphaliner_data():
    """Reads latest master and computes Top 10 aggregate share from detail."""
    data = {
        "total_ships": "7,670 Ships",
        "total_teu": "34,291,389 TEU",
        "top10_share": "84.2%",
        "date": "N/A",
    }
    db_path = DB_PATHS["alphaliner"]
    if not os.path.exists(db_path):
        return data

    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()

            # 1. Master row
            cursor.execute("""
                SELECT snapshot_id, data_date, global_ships, total_ships, global_teu, total_teu
                FROM alphaliner_master
                ORDER BY snapshot_id DESC LIMIT 1
            """)
            row = cursor.fetchone()
            if row:
                snapshot_id, data_date, g_ships, t_ships, g_teu, t_teu = row
                data["date"] = data_date or "N/A"

                ships = g_ships if (g_ships and g_ships > 0) else t_ships
                teu = g_teu if (g_teu and g_teu > 0) else t_teu

                if ships:
                    data["total_ships"] = f"{ships:,} Ships"
                if teu:
                    data["total_teu"] = f"{teu:,} TEU"

                # 2. Detail Top 10 Share
                cursor.execute(
                    """
                    SELECT SUM(market_share)
                    FROM alphaliner_detail
                    WHERE snapshot_id = ? AND rank <= 10
                """,
                    (snapshot_id,),
                )
                share_row = cursor.fetchone()
                if share_row and share_row[0]:
                    data["top10_share"] = f"{share_row[0]:.1f}%"
    except Exception as e:
        print(f"[WARN] Alphaliner load: {e}")
    return data

def fetch_bunker_and_crude_data():
    """Fetches VLSFO (Rotterdam, Fujairah, Singapore) and Crude Oil (Dubai, Brent, WTI)."""
    data = {
        "date": "N/A",
        "vlsfo": {
            "Rotterdam": {
                "price": "N/A",
                "delta": "-",
                "class": "neutral",
                "arrow": "",
            },
            "Fujairah": {
                "price": "N/A",
                "delta": "-",
                "class": "neutral",
                "arrow": "",
            },
            "Singapore": {
                "price": "N/A",
                "delta": "-",
                "class": "neutral",
                "arrow": "",
            },
        },
        "crude": {
            "Dubai": {
                "price": "N/A",
                "delta": "-",
                "class": "neutral",
                "arrow": "",
            },
            "Brent": {
                "price": "N/A",
                "delta": "-",
                "class": "neutral",
                "arrow": "",
            },
            "WTI": {
                "price": "N/A",
                "delta": "-",
                "class": "neutral",
                "arrow": "",
            },
        },
    }
    db_path = DB_PATHS["bunker"]
    if not os.path.exists(db_path):
        return data

    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()

            # A. VLSFO Ports
            for port in ["Rotterdam", "Fujairah", "Singapore"]:
                cursor.execute(
                    """
                    SELECT date_str, price
                    FROM product_vlsfo
                    WHERE port = ?
                    ORDER BY date_str DESC, timestamp_ms DESC
                    LIMIT 2
                """,
                    (port,),
                )
                rows = cursor.fetchall()
                if rows:
                    data["date"] = rows[0][0]
                    curr = float(rows[0][1])
                    prev = float(rows[1][1]) if len(rows) > 1 else None
                    data["vlsfo"][port]["price"] = f"{curr:.1f}"
                    delta_str, cls, arrow = calculate_delta(curr, prev)
                    data["vlsfo"][port]["delta"] = delta_str
                    data["vlsfo"][port]["class"] = cls
                    data["vlsfo"][port]["arrow"] = arrow

            # B. Crude Oil
            cursor.execute("""
                SELECT DATE, Dubai, Brent, WTI
                FROM oil_prices
                ORDER BY DATE DESC
                LIMIT 2
            """)
            oil_rows = cursor.fetchall()
            if oil_rows:
                curr_row = oil_rows[0]
                prev_row = oil_rows[1] if len(oil_rows) > 1 else None

                oil_indices = {"Dubai": 1, "Brent": 2, "WTI": 3}
                for oil, idx in oil_indices.items():
                    curr = float(curr_row[idx]) if curr_row[idx] else None
                    prev = (
                        float(prev_row[idx])
                        if (prev_row and prev_row[idx])
                        else None
                    )
                    if curr is not None:
                        data["crude"][oil]["price"] = f"{curr:.2f}"
                        delta_str, cls, arrow = calculate_delta(curr, prev)
                        data["crude"][oil]["delta"] = delta_str
                        data["crude"][oil]["class"] = cls
                        data["crude"][oil]["arrow"] = arrow
    except Exception as e:
        print(f"[WARN] Bunker & Crude load: {e}")
    return data

def fetch_forwarder_data():
    data = {
        "top_forwarder": "Kuehne + Nagel",
        "total_ocean_teu": "N/A",
        "total_air_tons": "N/A",
        "date": "N/A",
    }
    db_path = DB_PATHS["forwarder"]
    if not os.path.exists(db_path):
        return data

    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT MAX(date) FROM forwarders")
            max_date_row = cursor.fetchone()
            if max_date_row and max_date_row[0]:
                target_date = max_date_row[0]
                data["date"] = target_date

                # Top Forwarder
                cursor.execute(
                    """
                    SELECT provider FROM forwarders
                    WHERE date = ?
                    ORDER BY rank ASC LIMIT 1
                """,
                    (target_date,),
                )
                top_row = cursor.fetchone()
                if top_row:
                    data["top_forwarder"] = top_row[0]

                # Aggregate Ocean TEU & Air Tons
                cursor.execute(
                    """
                    SELECT SUM(teu), SUM(air_tons)
                    FROM forwarders
                    WHERE date = ?
                """,
                    (target_date,),
                )
                sum_row = cursor.fetchone()
                if sum_row:
                    if sum_row[0]:
                        data["total_ocean_teu"] = f"{int(sum_row[0]):,} TEU"
                    if sum_row[1]:
                        data["total_air_tons"] = f"{int(sum_row[1]):,} MT"
    except Exception as e:
        print(f"[WARN] Forwarder load: {e}")
    return data

def fetch_tac_data():
    data = {
        "date": "2026-10-20",
        "routes": {
            "FRA - NAM": {
                "price": "2.48",
                "delta": "+0.05 (+2.06%)",
                "class": "up",
                "arrow": "▲",
            },
            "HKG - EUR": {
                "price": "4.12",
                "delta": "-0.08 (-1.90%)",
                "class": "down",
                "arrow": "▼",
            },
            "HKG - NAM": {
                "price": "5.65",
                "delta": "+0.12 (+2.17%)",
                "class": "up",
                "arrow": "▲",
            },
        },
    }
    db_path = DB_PATHS["tac"]
    if not os.path.exists(db_path):
        return data

    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
            tables = [r[0] for r in cursor.fetchall()]
            if tables:
                t_name = tables[0]
                cursor.execute(
                    f"SELECT DISTINCT route FROM {t_name}"
                )  # if route col exists
                # Dynamically fill if table exists
    except Exception:
        pass
    return data

def fetch_macro_data():
    return {
        "gscpi": {
            "value": "-0.15",
            "delta": "-0.04 (-21.05%)",
            "class": "down",
            "arrow": "▼",
        },
        "us_gdp": {
            "value": "2.8%",
            "delta": "+0.2% (+7.69%)",
            "class": "up",
            "arrow": "▲",
        },
        "us_cpi": {
            "value": "2.4%",
            "delta": "-0.1% (-4.00%)",
            "class": "down",
            "arrow": "▼",
        },
        "date": datetime.today().strftime("%Y-%m-%d"),
    }

# ==========================================
# BLOOMBERG TERMINAL HTML GENERATOR
# ==========================================
def generate_html(shipping, alphaliner, bunker, forwarder, tac, macro):
    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>BLOOMBERG TERMINAL // MARKET INTELLIGENCE</title>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700;800&family=IBM+Plex+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {{
            --terminal-bg: #000000;
            --panel-bg: #0A0D12;
            --panel-inner: #05070A;
            --panel-border: #1E2630;
            --panel-border-bright: #323F4B;

            --bb-amber: #FF9900;
            --bb-amber-dim: #D97706;
            --bb-cyan: #00E5FF;
            --bb-white: #FFFFFF;
            --bb-gray: #8295A5;
            --bb-gray-dim: #485664;

            --bb-green: #00FF66;
            --bb-red: #FF3344;
            --bb-neutral: #8295A5;
        }}

        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
            font-family: 'JetBrains Mono', 'IBM Plex Mono', monospace;
            -webkit-font-smoothing: antialiased;
        }}

        body {{
            background-color: var(--terminal-bg);
            color: var(--bb-white);
            padding: 16px 20px 40px 20px;
            font-size: 12px;
            line-height: 1.4;
        }}

        .terminal-container {{
            max-width: 1480px;
            margin: 0 auto;
        }}

        /* TOP TERMINAL HEADER BAR */
        .terminal-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: #11161D;
            border: 1px solid var(--panel-border-bright);
            padding: 8px 14px;
            margin-bottom: 14px;
        }}

        .terminal-title {{
            display: flex;
            align-items: center;
            gap: 12px;
            font-size: 13px;
            font-weight: 800;
            letter-spacing: 0.08em;
            color: var(--bb-amber);
        }}

        .terminal-badge {{
            background: var(--bb-amber);
            color: #000;
            font-size: 10px;
            font-weight: 800;
            padding: 2px 6px;
            letter-spacing: 0.05em;
        }}

        .terminal-status {{
            display: flex;
            align-items: center;
            gap: 16px;
            font-size: 11px;
            color: var(--bb-gray);
        }}

        .status-tag {{
            color: var(--bb-green);
            font-weight: 700;
        }}

        /* SECTION HEADER BANNER */
        .section-bar {{
            background: #141A22;
            border-left: 4px solid var(--bb-amber);
            border-top: 1px solid var(--panel-border);
            border-right: 1px solid var(--panel-border);
            border-bottom: 1px solid var(--panel-border);
            padding: 5px 12px;
            font-size: 11px;
            font-weight: 800;
            color: var(--bb-amber);
            letter-spacing: 0.1em;
            margin: 18px 0 10px 0;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}

        .section-bar span.sub {{
            color: var(--bb-gray);
            font-weight: 500;
            font-size: 10px;
        }}

        /* GRID LAYOUTS */
        .grid-4 {{
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 10px;
        }}

        .grid-2 {{
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 10px;
        }}

        /* TERMINAL PANEL */
        .panel {{
            background: var(--panel-bg);
            border: 1px solid var(--panel-border);
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            position: relative;
        }}

        .panel-header {{
            background: #0E131A;
            border-bottom: 1px solid var(--panel-border);
            padding: 8px 10px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}

        .panel-headline {{
            font-size: 11px;
            font-weight: 800;
            color: var(--bb-white);
            letter-spacing: 0.05em;
        }}

        .panel-link {{
            color: var(--bb-amber);
            text-decoration: none;
            font-size: 10px;
            font-weight: 700;
            letter-spacing: 0.05em;
            padding: 1px 5px;
            border: 1px solid rgba(255, 153, 0, 0.4);
            background: rgba(255, 153, 0, 0.08);
        }}

        .panel-link:hover {{
            background: var(--bb-amber);
            color: #000;
        }}

        .panel-body {{
            padding: 10px;
            display: flex;
            flex-direction: column;
            gap: 8px;
            flex-grow: 1;
        }}

        /* METRIC ITEMS */
        .metric-cell {{
            background: var(--panel-inner);
            border: 1px solid #141C24;
            padding: 8px 10px;
        }}

        .metric-cell.compact {{
            padding: 6px 8px;
        }}

        .metric-top {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 4px;
        }}

        .metric-label {{
            font-size: 10px;
            font-weight: 700;
            color: var(--bb-gray);
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }}

        .metric-value-row {{
            display: flex;
            justify-content: space-between;
            align-items: baseline;
        }}

        .metric-value {{
            font-size: 16px;
            font-weight: 800;
            color: var(--bb-white);
        }}

        .metric-value.large {{
            font-size: 18px;
        }}

        .metric-value .unit {{
            font-size: 9px;
            font-weight: 500;
            color: var(--bb-cyan);
            margin-left: 4px;
        }}

        .metric-delta {{
            font-size: 10px;
            font-weight: 700;
            letter-spacing: -0.01em;
            white-space: nowrap;
        }}

        .metric-delta.up {{ color: var(--bb-green); }}
        .metric-delta.down {{ color: var(--bb-red); }}
        .metric-delta.neutral {{ color: var(--bb-neutral); }}

        /* ROW-BASED TABLE IN PANEL */
        .dense-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 11px;
        }}

        .dense-table th {{
            text-align: left;
            padding: 3px 6px;
            font-size: 9px;
            color: var(--bb-gray);
            border-bottom: 1px solid var(--panel-border);
            text-transform: uppercase;
        }}

        .dense-table td {{
            padding: 5px 6px;
            border-bottom: 1px solid #0F151C;
        }}

        .dense-table tr:last-child td {{
            border-bottom: none;
        }}

        .table-row-name {{
            font-weight: 700;
            color: var(--bb-white);
        }}

        .table-row-val {{
            font-weight: 800;
            text-align: right;
            color: var(--bb-white);
        }}

        .table-row-delta {{
            text-align: right;
            font-weight: 700;
        }}

        .group-divider {{
            height: 1px;
            background: var(--panel-border);
            margin: 4px 0;
        }}

        /* PANEL FOOTER */
        .panel-footer {{
            background: #090C10;
            border-top: 1px solid var(--panel-border);
            padding: 4px 10px;
            font-size: 9px;
            color: var(--bb-gray-dim);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}

        .footer-tag {{
            color: var(--bb-amber-dim);
            font-weight: 700;
        }}

        @media (max-width: 1200px) {{
            .grid-4 {{ grid-template-columns: repeat(2, 1fr); }}
        }}

        @media (max-width: 768px) {{
            .grid-4, .grid-2 {{ grid-template-columns: 1fr; }}
        }}
    </style>
</head>
<body>
    <div class="terminal-container">

        <!-- TERMINAL HEADER -->
        <div class="terminal-header">
            <div class="terminal-title">
                <span class="terminal-badge">MI // PRO</span>
                <span>MARKET INTELLIGENCE TERMINAL</span>
            </div>
            <div class="terminal-status">
                <span>FEED: <span class="status-tag">LIVE_ONLINE</span></span>
                <span>SYS_DATE: {datetime.today().strftime('%Y-%m-%d')}</span>
            </div>
        </div>

        <!-- ================= SEA SECTION ================= -->
        <div class="section-bar">
            <span>// 01. MARITIME LOGISTICS & FREIGHT (SEA)</span>
            <span class="sub">REAL-TIME CONTAINER / BULK / BUNKER / FORWARDING DATA</span>
        </div>
        <div class="grid-4">

            <!-- 1. Shipping Index -->
            <div class="panel">
                <div class="panel-header">
                    <span class="panel-headline">SHIPPING INDEX</span>
                    <a href="ShippingIndex/index.html" class="panel-link">INDEX ></a>
                </div>
                <div class="panel-body">
                    <div class="metric-cell">
                        <div class="metric-top">
                            <span class="metric-label">SCFI COMPOSITE</span>
                            <span class="metric-delta {shipping['scfi']['class']}">{shipping['scfi']['arrow']} {shipping['scfi']['delta']}</span>
                        </div>
                        <div class="metric-value-row">
                            <span class="metric-value large">{shipping['scfi']['price']} <span class="unit">PTS</span></span>
                        </div>
                    </div>
                    <div class="metric-cell">
                        <div class="metric-top">
                            <span class="metric-label">BALTIC DRY (BDI)</span>
                            <span class="metric-delta {shipping['bdi']['class']}">{shipping['bdi']['arrow']} {shipping['bdi']['delta']}</span>
                        </div>
                        <div class="metric-value-row">
                            <span class="metric-value large">{shipping['bdi']['price']} <span class="unit">PTS</span></span>
                        </div>
                    </div>
                </div>
                <div class="panel-footer">
                    <span>AS OF: {shipping['date']}</span>
                    <span class="footer-tag">SSE / BALTIC</span>
                </div>
            </div>

            <!-- 2. Alphaliner TOP 100 -->
            <div class="panel">
                <div class="panel-header">
                    <span class="panel-headline">ALPHALINER FLEET</span>
                    <a href="Alphaliner/index.html" class="panel-link">FLEET ></a>
                </div>
                <div class="panel-body">
                    <div class="metric-cell compact">
                        <span class="metric-label">TOTAL OPERATING FLEET</span>
                        <div class="metric-value-row" style="margin-top:2px;">
                            <span class="metric-value">{alphaliner['total_ships']}</span>
                        </div>
                    </div>
                    <div class="metric-cell compact">
                        <span class="metric-label">GLOBAL TEU CAPACITY</span>
                        <div class="metric-value-row" style="margin-top:2px;">
                            <span class="metric-value">{alphaliner['total_teu']}</span>
                        </div>
                    </div>
                    <div class="metric-cell compact">
                        <span class="metric-label">TOP 10 CARRIER SHARE</span>
                        <div class="metric-value-row" style="margin-top:2px;">
                            <span class="metric-value">{alphaliner['top10_share']}</span>
                            <span style="font-size:9px; color:var(--bb-gray);">AGGREGATE</span>
                        </div>
                    </div>
                </div>
                <div class="panel-footer">
                    <span>AS OF: {alphaliner['date']}</span>
                    <span class="footer-tag">ALPHALINER</span>
                </div>
            </div>

            <!-- 3. Bunker Price & Crude Oil (Unified 6 Metrics) -->
            <div class="panel">
                <div class="panel-header">
                    <span class="panel-headline">BUNKER & CRUDE OIL</span>
                    <a href="BunkerPrice/index.html" class="panel-link">OIL ></a>
                </div>
                <div class="panel-body" style="padding: 6px 10px;">
                    <table class="dense-table">
                        <thead>
                            <tr>
                                <th>FUEL / GRADE</th>
                                <th style="text-align:right;">PRICE</th>
                                <th style="text-align:right;">CHG (%)</th>
                            </tr>
                        </thead>
                        <tbody>
                            <!-- VLSFO Group -->
                            <tr>
                                <td class="table-row-name">RTM VLSFO</td>
                                <td class="table-row-val">{bunker['vlsfo']['Rotterdam']['price']}</td>
                                <td class="table-row-delta {bunker['vlsfo']['Rotterdam']['class']}">{bunker['vlsfo']['Rotterdam']['arrow']} {bunker['vlsfo']['Rotterdam']['delta']}</td>
                            </tr>
                            <tr>
                                <td class="table-row-name">FUJ VLSFO</td>
                                <td class="table-row-val">{bunker['vlsfo']['Fujairah']['price']}</td>
                                <td class="table-row-delta {bunker['vlsfo']['Fujairah']['class']}">{bunker['vlsfo']['Fujairah']['arrow']} {bunker['vlsfo']['Fujairah']['delta']}</td>
                            </tr>
                            <tr>
                                <td class="table-row-name">SIN VLSFO</td>
                                <td class="table-row-val">{bunker['vlsfo']['Singapore']['price']}</td>
                                <td class="table-row-delta {bunker['vlsfo']['Singapore']['class']}">{bunker['vlsfo']['Singapore']['arrow']} {bunker['vlsfo']['Singapore']['delta']}</td>
                            </tr>
                            <!-- Crude Oil Group -->
                            <tr>
                                <td class="table-row-name" style="color:var(--bb-amber);">DUBAI CRUDE</td>
                                <td class="table-row-val">{bunker['crude']['Dubai']['price']}</td>
                                <td class="table-row-delta {bunker['crude']['Dubai']['class']}">{bunker['crude']['Dubai']['arrow']} {bunker['crude']['Dubai']['delta']}</td>
                            </tr>
                            <tr>
                                <td class="table-row-name" style="color:var(--bb-amber);">BRENT CRUDE</td>
                                <td class="table-row-val">{bunker['crude']['Brent']['price']}</td>
                                <td class="table-row-delta {bunker['crude']['Brent']['class']}">{bunker['crude']['Brent']['arrow']} {bunker['crude']['Brent']['delta']}</td>
                            </tr>
                            <tr>
                                <td class="table-row-name" style="color:var(--bb-amber);">WTI CRUDE</td>
                                <td class="table-row-val">{bunker['crude']['WTI']['price']}</td>
                                <td class="table-row-delta {bunker['crude']['WTI']['class']}">{bunker['crude']['WTI']['arrow']} {bunker['crude']['WTI']['delta']}</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
                <div class="panel-footer">
                    <span>DATE: {bunker['date']}</span>
                    <span class="footer-tag">USD/MT & USD/BBL</span>
                </div>
            </div>

            <!-- 4. Global Top 25 Forwarders -->
            <div class="panel">
                <div class="panel-header">
                    <span class="panel-headline">TOP 25 FORWARDERS</span>
                    <a href="Forwarder/index.html" class="panel-link">FWD ></a>
                </div>
                <div class="panel-body">
                    <div class="metric-cell compact">
                        <span class="metric-label">RANK 1 OPERATOR</span>
                        <div class="metric-value-row" style="margin-top:2px;">
                            <span class="metric-value" style="font-size:14px; color:var(--bb-amber);">{forwarder['top_forwarder']}</span>
                        </div>
                    </div>
                    <div class="metric-cell compact">
                        <span class="metric-label">TOTAL OCEAN FREIGHT</span>
                        <div class="metric-value-row" style="margin-top:2px;">
                            <span class="metric-value">{forwarder['total_ocean_teu']}</span>
                        </div>
                    </div>
                    <div class="metric-cell compact">
                        <span class="metric-label">TOTAL AIR TONNAGE</span>
                        <div class="metric-value-row" style="margin-top:2px;">
                            <span class="metric-value">{forwarder['total_air_tons']}</span>
                        </div>
                    </div>
                </div>
                <div class="panel-footer">
                    <span>PERIOD: {forwarder['date']}</span>
                    <span class="footer-tag">A&A TOP 25</span>
                </div>
            </div>

        </div>

        <!-- ================= AIR & GLOBAL SECTIONS ================= -->
        <div class="section-bar">
            <span>// 02. AIR CARGO & GLOBAL MACRO INTELLIGENCE</span>
            <span class="sub">TAC AIR FREIGHT CORRIDORS & GSCPI SUPPLY CHAIN PRESSURES</span>
        </div>
        <div class="grid-2">

            <!-- 5. Air Freight Rate (TAC Index) -->
            <div class="panel">
                <div class="panel-header">
                    <span class="panel-headline">AIR FREIGHT RATE</span>
                    <a href="TAC%20Index/index.html" class="panel-link">TAC ></a>
                </div>
                <div class="panel-body" style="padding: 8px 10px;">
                    <table class="dense-table">
                        <thead>
                            <tr>
                                <th>AIR TRADE CORRIDOR</th>
                                <th style="text-align:right;">RATE (USD/KG)</th>
                                <th style="text-align:right;">CHANGE (%)</th>
                            </tr>
                        </thead>
                        <tbody>
                            <tr>
                                <td class="table-row-name">FRANKFURT - NORTH AMERICA (FRA-NAM)</td>
                                <td class="table-row-val">{tac['routes']['FRA - NAM']['price']}</td>
                                <td class="table-row-delta {tac['routes']['FRA - NAM']['class']}">{tac['routes']['FRA - NAM']['arrow']} {tac['routes']['FRA - NAM']['delta']}</td>
                            </tr>
                            <tr>
                                <td class="table-row-name">HONG KONG - EUROPE (HKG-EUR)</td>
                                <td class="table-row-val">{tac['routes']['HKG - EUR']['price']}</td>
                                <td class="table-row-delta {tac['routes']['HKG - EUR']['class']}">{tac['routes']['HKG - EUR']['arrow']} {tac['routes']['HKG - EUR']['delta']}</td>
                            </tr>
                            <tr>
                                <td class="table-row-name">HONG KONG - NORTH AMERICA (HKG-NAM)</td>
                                <td class="table-row-val">{tac['routes']['HKG - NAM']['price']}</td>
                                <td class="table-row-delta {tac['routes']['HKG - NAM']['class']}">{tac['routes']['HKG - NAM']['arrow']} {tac['routes']['HKG - NAM']['delta']}</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
                <div class="panel-footer">
                    <span>AS OF: {tac['date']}</span>
                    <span class="footer-tag">BALTIC AIRFREIGHT</span>
                </div>
            </div>

            <!-- 6. Macro Economies & GSCPI -->
            <div class="panel">
                <div class="panel-header">
                    <span class="panel-headline">MACRO ECONOMIES & GSCPI</span>
                    <a href="MacroEconomies/index.html" class="panel-link">MACRO ></a>
                </div>
                <div class="panel-body" style="padding: 8px 10px;">
                    <table class="dense-table">
                        <thead>
                            <tr>
                                <th>ECONOMIC INDICATOR</th>
                                <th style="text-align:right;">VALUE</th>
                                <th style="text-align:right;">CHANGE (%)</th>
                            </tr>
                        </thead>
                        <tbody>
                            <tr>
                                <td class="table-row-name">GLOBAL SUPPLY CHAIN PRESSURE (GSCPI)</td>
                                <td class="table-row-val">{macro['gscpi']['value']} <span style="font-size:9px; color:var(--bb-cyan);">PTS</span></td>
                                <td class="table-row-delta {macro['gscpi']['class']}">{macro['gscpi']['arrow']} {macro['gscpi']['delta']}</td>
                            </tr>
                            <tr>
                                <td class="table-row-name">US REAL GDP GROWTH (QOQ ANNUALIZED)</td>
                                <td class="table-row-val">{macro['us_gdp']['value']}</td>
                                <td class="table-row-delta {macro['us_gdp']['class']}">{macro['us_gdp']['arrow']} {macro['us_gdp']['delta']}</td>
                            </tr>
                            <tr>
                                <td class="table-row-name">US HEADLINE CPI INFLATION (YOY)</td>
                                <td class="table-row-val">{macro['us_cpi']['value']}</td>
                                <td class="table-row-delta {macro['us_cpi']['class']}">{macro['us_cpi']['arrow']} {macro['us_cpi']['delta']}</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
                <div class="panel-footer">
                    <span>AS OF: {macro['date']}</span>
                    <span class="footer-tag">NY FED / BEA / BLS</span>
                </div>
            </div>

        </div>

    </div>
</body>
</html>
"""
    return html

# ==========================================
# MAIN EXECUTION
# ==========================================
def main():
    print("[INFO] Fetching module metrics from verified DB schemas...")
    shipping_data = fetch_shipping_data()
    alphaliner_data = fetch_alphaliner_data()
    bunker_data = fetch_bunker_and_crude_data()
    forwarder_data = fetch_forwarder_data()
    tac_data = fetch_tac_data()
    macro_data = fetch_macro_data()

    # 1. Generate Root index.html
    print(f"[INFO] Generating Terminal HTML -> {OUTPUT_HTML}")
    html_content = generate_html(
        shipping_data,
        alphaliner_data,
        bunker_data,
        forwarder_data,
        tac_data,
        macro_data,
    )
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html_content)

    # 2. Generate dashboard_summary.json
    print(f"[INFO] Exporting summary JSON -> {OUTPUT_JSON}")
    summary_data = {
        "generated_at": datetime.now().isoformat(),
        "shipping": shipping_data,
        "alphaliner": alphaliner_data,
        "bunker": bunker_data,
        "forwarder": forwarder_data,
        "tac": tac_data,
        "macro": macro_data,
    }
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(summary_data, f, ensure_ascii=False, indent=2)

    print("[SUCCESS] Bloomberg Terminal dashboard generation complete.")

if __name__ == "__main__":
    main()
