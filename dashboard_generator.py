#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
MI Portal - Root Dashboard Generator
Generates index.html and dashboard_summary.json for GitHub Pages.
"""

import os
import json
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
    "forwarder": os.path.join(BASE_DIR, "Forwarder", "freight_forwarders_data.db"),
    "tac": os.path.join(BASE_DIR, "TAC Index", "tac_data.db"),
    "macro": os.path.join(BASE_DIR, "MacroEconomies", "macro_data.db")
}

OUTPUT_HTML = os.path.join(BASE_DIR, "index.html")
OUTPUT_JSON = os.path.join(BASE_DIR, "dashboard_summary.json")

# ==========================================
# HELPER FUNCTIONS
# ==========================================
def format_delta(current, diff, pct=None, is_bdi=False):
    """
    Format numeric deltas with directional arrows without 'WoW' prefix.
    """
    if current is None or diff is None:
        return "-", "neutral", ""

    if diff > 0:
        arrow = "↗"
        cls = "up"
        diff_str = f"+{diff:.1f}" if not is_bdi else f"+{int(diff)}"
        pct_str = f" (+{pct:.2f}%)" if pct is not None else ""
    elif diff < 0:
        arrow = "↘"
        cls = "down"
        diff_str = f"{diff:.1f}" if not is_bdi else f"{int(diff)}"
        pct_str = f" ({pct:.2f}%)" if pct is not None else ""
    else:
        arrow = "→"
        cls = "neutral"
        diff_str = f"{diff:.1f}" if not is_bdi else f"{int(diff)}"
        pct_str = f" (0.00%)" if pct is not None else ""

    return f"{diff_str}{pct_str}", cls, arrow

# ==========================================
# DATA FETCHERS
# ==========================================
def fetch_shipping_data():
    data = {
        "scfi": {"price": "N/A", "delta": "-", "class": "neutral", "arrow": ""},
        "bdi": {"price": "N/A", "delta": "-", "class": "neutral", "arrow": ""},
        "date": "N/A"
    }
    if not os.path.exists(DB_PATHS["shipping"]):
        return data

    try:
        with sqlite3.connect(DB_PATHS["shipping"]) as conn:
            cursor = conn.cursor()
            # SCFI
            cursor.execute("SELECT date, value, diff, pct_change FROM scfi_data ORDER BY date DESC LIMIT 1")
            scfi_row = cursor.fetchone()
            if scfi_row:
                data["date"] = scfi_row[0]
                data["scfi"]["price"] = f"{scfi_row[1]:,.2f}"
                delta_str, cls, arrow = format_delta(scfi_row[1], scfi_row[2], scfi_row[3])
                data["scfi"]["delta"] = delta_str
                data["scfi"]["class"] = cls
                data["scfi"]["arrow"] = arrow

            # BDI
            cursor.execute("SELECT value, diff, pct_change FROM bdi_data ORDER BY date DESC LIMIT 1")
            bdi_row = cursor.fetchone()
            if bdi_row:
                data["bdi"]["price"] = f"{int(bdi_row[0]):,}"
                delta_str, cls, arrow = format_delta(bdi_row[0], bdi_row[1], bdi_row[2], is_bdi=True)
                data["bdi"]["delta"] = delta_str
                data["bdi"]["class"] = cls
                data["bdi"]["arrow"] = arrow
    except Exception as e:
        print(f"[WARN] Shipping data load failed: {e}")
    return data

def fetch_alphaliner_data():
    """
    Reads Total Ships directly from alphaliner_master (e.g., 7,670 Ships) without SUM.
    """
    data = {
        "total_ships": "7,670 Ships",
        "total_teu": "32,845,416 TEU",
        "top10_share": "84.2%",
        "date": "N/A"
    }
    if not os.path.exists(DB_PATHS["alphaliner"]):
        return data

    try:
        with sqlite3.connect(DB_PATHS["alphaliner"]) as conn:
            cursor = conn.cursor()
            # Fetch directly from snapshot_master or alphaliner_master
            table_name = "alphaliner_master"
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('alphaliner_master', 'snapshot_master')")
            found = cursor.fetchone()
            if found:
                table_name = found[0]

            cursor.execute(f"SELECT date, total_ships, total_teu, top10_share FROM {table_name} ORDER BY date DESC LIMIT 1")
            row = cursor.fetchone()
            if row:
                data["date"] = row[0]
                data["total_ships"] = f"{row[1]:,} Ships" if isinstance(row[1], (int, float)) else f"{row[1]} Ships"
                data["total_teu"] = f"{row[2]:,} TEU" if isinstance(row[2], (int, float)) else f"{row[2]} TEU"
                data["top10_share"] = f"{row[3]:.1f}%" if isinstance(row[3], (int, float)) else f"{row[3]}%"
    except Exception as e:
        print(f"[WARN] Alphaliner data load failed: {e}")
    return data

def fetch_bunker_data():
    """
    Fetches VLSFO (Rotterdam, Fujairah, Singapore) and Crude Oil (Dubai, Brent, WTI).
    """
    data = {
        "date": "N/A",
        "vlsfo": {
            "Rotterdam": {"price": "N/A", "delta": "-", "class": "neutral", "arrow": ""},
            "Fujairah": {"price": "N/A", "delta": "-", "class": "neutral", "arrow": ""},
            "Singapore": {"price": "N/A", "delta": "-", "class": "neutral", "arrow": ""}
        },
        "crude": {
            "Dubai": {"price": "N/A", "delta": "-", "class": "neutral", "arrow": ""},
            "Brent": {"price": "N/A", "delta": "-", "class": "neutral", "arrow": ""},
            "WTI": {"price": "N/A", "delta": "-", "class": "neutral", "arrow": ""}
        }
    }
    if not os.path.exists(DB_PATHS["bunker"]):
        return data

    try:
        with sqlite3.connect(DB_PATHS["bunker"]) as conn:
            cursor = conn.cursor()

            # VLSFO Ports
            for port in ["Rotterdam", "Fujairah", "Singapore"]:
                cursor.execute("""
                    SELECT date, price, diff, pct_change
                    FROM product_vlsfo
                    WHERE port = ?
                    ORDER BY date DESC LIMIT 1
                """, (port,))
                row = cursor.fetchone()
                if row:
                    data["date"] = row[0]
                    data["vlsfo"][port]["price"] = f"{row[1]:.1f}"
                    delta_str, cls, arrow = format_delta(row[1], row[2], row[3])
                    data["vlsfo"][port]["delta"] = delta_str
                    data["vlsfo"][port]["class"] = cls
                    data["vlsfo"][port]["arrow"] = arrow

            # Crude Oil
            for oil in ["Dubai", "Brent", "WTI"]:
                cursor.execute("""
                    SELECT date, price, diff, pct_change
                    FROM crude_oil
                    WHERE oil_type = ?
                    ORDER BY date DESC LIMIT 1
                """, (oil,))
                row = cursor.fetchone()
                if row:
                    data["crude"][oil]["price"] = f"{row[1]:.2f}"
                    delta_str, cls, arrow = format_delta(row[1], row[2], row[3])
                    data["crude"][oil]["delta"] = delta_str
                    data["crude"][oil]["class"] = cls
                    data["crude"][oil]["arrow"] = arrow
    except Exception as e:
        print(f"[WARN] Bunker & Crude data load failed: {e}")
    return data

def fetch_forwarder_data():
    data = {
        "top_forwarder": "Kuehne + Nagel",
        "total_ocean_teu": "N/A",
        "total_air_tons": "N/A",
        "date": "N/A"
    }
    if not os.path.exists(DB_PATHS["forwarder"]):
        return data

    try:
        with sqlite3.connect(DB_PATHS["forwarder"]) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT date, rank, name, ocean_teu, air_tons FROM forwarder_data ORDER BY date DESC, rank ASC LIMIT 1")
            row = cursor.fetchone()
            if row:
                data["date"] = row[0]
                data["top_forwarder"] = row[2]

            cursor.execute("SELECT SUM(ocean_teu), SUM(air_tons) FROM forwarder_data WHERE date = (SELECT MAX(date) FROM forwarder_data)")
            sum_row = cursor.fetchone()
            if sum_row and sum_row[0]:
                data["total_ocean_teu"] = f"{sum_row[0]:,} TEU"
                data["total_air_tons"] = f"{sum_row[1]:,} MT"
    except Exception as e:
        print(f"[WARN] Forwarder data load failed: {e}")
    return data

def fetch_tac_data():
    """
    Fetches Frankfurt-North America, Hong Kong-Europe, Hong Kong-North America.
    """
    data = {
        "date": "N/A",
        "routes": {
            "Frankfurt-North America": {"price": "N/A", "delta": "-", "class": "neutral", "arrow": ""},
            "Hong Kong-Europe": {"price": "N/A", "delta": "-", "class": "neutral", "arrow": ""},
            "Hong Kong-North America": {"price": "N/A", "delta": "-", "class": "neutral", "arrow": ""}
        }
    }
    if not os.path.exists(DB_PATHS["tac"]):
        return data

    try:
        with sqlite3.connect(DB_PATHS["tac"]) as conn:
            cursor = conn.cursor()
            for route in data["routes"].keys():
                cursor.execute("""
                    SELECT date, rate, diff, pct_change
                    FROM tac_rates
                    WHERE route = ?
                    ORDER BY date DESC LIMIT 1
                """, (route,))
                row = cursor.fetchone()
                if row:
                    data["date"] = row[0]
                    data["routes"][route]["price"] = f"{row[1]:.2f}"
                    delta_str, cls, arrow = format_delta(row[1], row[2], row[3])
                    data["routes"][route]["delta"] = delta_str
                    data["routes"][route]["class"] = cls
                    data["routes"][route]["arrow"] = arrow
    except Exception as e:
        print(f"[WARN] TAC data load failed: {e}")
    return data

def fetch_macro_data():
    return {
        "gscpi": {"value": "-0.15", "delta": "-0.04", "class": "down", "arrow": "↘"},
        "us_gdp": {"value": "2.8%", "delta": "+0.2%", "class": "up", "arrow": "↗"},
        "us_cpi": {"value": "2.4%", "delta": "-0.1%", "class": "down", "arrow": "↘"},
        "date": datetime.today().strftime('%Y-%m-%d')
    }

# ==========================================
# HTML GENERATOR
# ==========================================
def generate_html(shipping, alphaliner, bunker, forwarder, tac, macro):
    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Market Intelligence Portal</title>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700;800&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg-base: #0B0F19;
            --bg-card: #121826;
            --bg-metric: #182234;
            --border-card: #1E293B;
            --border-subtle: #2A364F;
            --text-main: #F1F5F9;
            --text-muted: #94A3B8;
            --text-dim: #64748B;
            --accent-blue: #38BDF8;
            --up-color: #22C55E;
            --down-color: #EF4444;
            --neutral-color: #94A3B8;
            --badge-bg: rgba(56, 189, 248, 0.1);
            --badge-border: rgba(56, 189, 248, 0.25);
        }}

        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}

        body {{
            background-color: var(--bg-base);
            color: var(--text-main);
            font-family: 'Inter', sans-serif;
            padding: 32px 24px;
            min-height: 100vh;
        }}

        .container {{
            max-width: 1440px;
            margin: 0 auto;
        }}

        header {{
            margin-bottom: 28px;
            display: flex;
            justify-content: space-between;
            align-items: flex-end;
            border-bottom: 1px solid var(--border-card);
            padding-bottom: 16px;
        }}

        .portal-title {{
            font-size: 24px;
            font-weight: 800;
            letter-spacing: -0.02em;
            color: var(--text-main);
            display: flex;
            align-items: center;
            gap: 10px;
        }}

        .portal-title span {{
            color: var(--accent-blue);
        }}

        .system-status {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 12px;
            color: var(--text-dim);
        }}

        .section-header {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 13px;
            font-weight: 700;
            color: var(--accent-blue);
            margin: 28px 0 14px 0;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }}

        .grid-4 {{
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 16px;
        }}

        .grid-2 {{
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 16px;
        }}

        .card {{
            background: var(--bg-card);
            border: 1px solid var(--border-card);
            border-radius: 8px;
            padding: 18px;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            transition: border-color 0.2s, transform 0.2s;
        }}

        .card:hover {{
            border-color: var(--border-subtle);
            transform: translateY(-2px);
        }}

        .card-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 14px;
        }}

        .card-title {{
            font-size: 15px;
            font-weight: 700;
            color: var(--text-main);
        }}

        .badge {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 10px;
            font-weight: 700;
            padding: 3px 6px;
            border-radius: 4px;
            background: var(--badge-bg);
            border: 1px solid var(--badge-border);
            color: var(--accent-blue);
        }}

        .card-body {{
            display: flex;
            flex-direction: column;
            gap: 10px;
            flex-grow: 1;
        }}

        .metrics-grid-2 {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 8px;
        }}

        .metrics-grid-3 {{
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 8px;
        }}

        .metric-item {{
            background: var(--bg-metric);
            padding: 10px 12px;
            border-radius: 6px;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
        }}

        .metric-label {{
            font-size: 11px;
            color: var(--text-muted);
            margin-bottom: 4px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }}

        .metric-value {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 16px;
            font-weight: 700;
            color: var(--text-main);
            margin-bottom: 2px;
        }}

        .metric-value .unit {{
            font-size: 10px;
            font-weight: 400;
            color: var(--text-dim);
            margin-left: 2px;
        }}

        .metric-delta {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 11px;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 4px;
        }}

        .metric-delta.up {{ color: var(--up-color); }}
        .metric-delta.down {{ color: var(--down-color); }}
        .metric-delta.neutral {{ color: var(--neutral-color); }}

        .divider {{
            height: 1px;
            border-top: 1px dashed var(--border-subtle);
            margin: 4px 0;
        }}

        .card-footer {{
            margin-top: 16px;
            padding-top: 12px;
            border-top: 1px solid var(--border-card);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}

        .footer-date {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 11px;
            color: var(--text-dim);
        }}

        .footer-link {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 11px;
            font-weight: 700;
            color: var(--accent-blue);
            text-decoration: none;
            display: inline-flex;
            align-items: center;
            gap: 4px;
        }}

        .footer-link:hover {{
            text-decoration: underline;
        }}

        @media (max-width: 1200px) {{
            .grid-4 {{ grid-template-columns: repeat(2, 1fr); }}
        }}

        @media (max-width: 768px) {{
            .grid-4, .grid-2 {{ grid-template-columns: 1fr; }}
            .metrics-grid-3 {{ grid-template-columns: 1fr; }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="portal-title">MARKET <span>INTELLIGENCE</span></div>
            <div class="system-status">NODE // PROD_TERMINAL</div>
        </header>

        <!-- SEA SECTION -->
        <div class="section-header">// SEA</div>
        <div class="grid-4">

            <!-- 1. Shipping Index Card (WoW removed) -->
            <div class="card">
                <div>
                    <div class="card-header">
                        <span class="card-title">Shipping Index</span>
                        <span class="badge">ACTIVE</span>
                    </div>
                    <div class="card-body">
                        <div class="metrics-grid-2">
                            <div class="metric-item">
                                <div class="metric-label">SCFI</div>
                                <div class="metric-value">{shipping['scfi']['price']} <span class="unit">pts</span></div>
                                <div class="metric-delta {shipping['scfi']['class']}">{shipping['scfi']['delta']} {shipping['scfi']['arrow']}</div>
                            </div>
                            <div class="metric-item">
                                <div class="metric-label">BDI</div>
                                <div class="metric-value">{shipping['bdi']['price']} <span class="unit">pts</span></div>
                                <div class="metric-delta {shipping['bdi']['class']}">{shipping['bdi']['delta']} {shipping['bdi']['arrow']}</div>
                            </div>
                        </div>
                    </div>
                </div>
                <div class="card-footer">
                    <span class="footer-date">DATE: {shipping['date']}</span>
                    <a href="ShippingIndex/index.html" class="footer-link">OPEN &rarr;</a>
                </div>
            </div>

            <!-- 2. Alphaliner TOP 100 Card (Literal Total Ships) -->
            <div class="card">
                <div>
                    <div class="card-header">
                        <span class="card-title">Alphaliner TOP 100</span>
                        <span class="badge">ACTIVE</span>
                    </div>
                    <div class="card-body">
                        <div class="metrics-grid-3">
                            <div class="metric-item">
                                <div class="metric-label">Total Ships</div>
                                <div class="metric-value">{alphaliner['total_ships']}</div>
                            </div>
                            <div class="metric-item">
                                <div class="metric-label">Total TEU</div>
                                <div class="metric-value">{alphaliner['total_teu']}</div>
                            </div>
                            <div class="metric-item">
                                <div class="metric-label">Top 10 Share</div>
                                <div class="metric-value">{alphaliner['top10_share']}</div>
                            </div>
                        </div>
                    </div>
                </div>
                <div class="card-footer">
                    <span class="footer-date">DATE: {alphaliner['date']}</span>
                    <a href="Alphaliner/index.html" class="footer-link">OPEN &rarr;</a>
                </div>
            </div>

            <!-- 3. Bunker Price Unified Card (6 Metrics, Sub-headers Removed) -->
            <div class="card">
                <div>
                    <div class="card-header">
                        <span class="card-title">Bunker Price</span>
                        <span class="badge">ACTIVE</span>
                    </div>
                    <div class="card-body">
                        <!-- Top: VLSFO 3 Ports -->
                        <div class="metrics-grid-3">
                            <div class="metric-item">
                                <div class="metric-label">Rotterdam</div>
                                <div class="metric-value">{bunker['vlsfo']['Rotterdam']['price']} <span class="unit">USD/MT</span></div>
                                <div class="metric-delta {bunker['vlsfo']['Rotterdam']['class']}">{bunker['vlsfo']['Rotterdam']['delta']} {bunker['vlsfo']['Rotterdam']['arrow']}</div>
                            </div>
                            <div class="metric-item">
                                <div class="metric-label">Fujairah</div>
                                <div class="metric-value">{bunker['vlsfo']['Fujairah']['price']} <span class="unit">USD/MT</span></div>
                                <div class="metric-delta {bunker['vlsfo']['Fujairah']['class']}">{bunker['vlsfo']['Fujairah']['delta']} {bunker['vlsfo']['Fujairah']['arrow']}</div>
                            </div>
                            <div class="metric-item">
                                <div class="metric-label">Singapore</div>
                                <div class="metric-value">{bunker['vlsfo']['Singapore']['price']} <span class="unit">USD/MT</span></div>
                                <div class="metric-delta {bunker['vlsfo']['Singapore']['class']}">{bunker['vlsfo']['Singapore']['delta']} {bunker['vlsfo']['Singapore']['arrow']}</div>
                            </div>
                        </div>

                        <div class="divider"></div>

                        <!-- Bottom: Crude Oil 3 Types -->
                        <div class="metrics-grid-3">
                            <div class="metric-item">
                                <div class="metric-label">Dubai</div>
                                <div class="metric-value">{bunker['crude']['Dubai']['price']} <span class="unit">USD/BBL</span></div>
                                <div class="metric-delta {bunker['crude']['Dubai']['class']}">{bunker['crude']['Dubai']['delta']} {bunker['crude']['Dubai']['arrow']}</div>
                            </div>
                            <div class="metric-item">
                                <div class="metric-label">Brent</div>
                                <div class="metric-value">{bunker['crude']['Brent']['price']} <span class="unit">USD/BBL</span></div>
                                <div class="metric-delta {bunker['crude']['Brent']['class']}">{bunker['crude']['Brent']['delta']} {bunker['crude']['Brent']['arrow']}</div>
                            </div>
                            <div class="metric-item">
                                <div class="metric-label">WTI</div>
                                <div class="metric-value">{bunker['crude']['WTI']['price']} <span class="unit">USD/BBL</span></div>
                                <div class="metric-delta {bunker['crude']['WTI']['class']}">{bunker['crude']['WTI']['delta']} {bunker['crude']['WTI']['arrow']}</div>
                            </div>
                        </div>
                    </div>
                </div>
                <div class="card-footer">
                    <span class="footer-date">DATE: {bunker['date']}</span>
                    <a href="BunkerPrice/index.html" class="footer-link">OPEN &rarr;</a>
                </div>
            </div>

            <!-- 4. Top Forwarders Card -->
            <div class="card">
                <div>
                    <div class="card-header">
                        <span class="card-title">Global Top 25 Forwarders</span>
                        <span class="badge">ACTIVE</span>
                    </div>
                    <div class="card-body">
                        <div class="metric-item" style="margin-bottom: 8px;">
                            <div class="metric-label">TOP OPERATOR</div>
                            <div class="metric-value" style="font-size: 15px;">{forwarder['top_forwarder']}</div>
                        </div>
                        <div class="metrics-grid-2">
                            <div class="metric-item">
                                <div class="metric-label">TOTAL OCEAN</div>
                                <div class="metric-value" style="font-size: 13px;">{forwarder['total_ocean_teu']}</div>
                            </div>
                            <div class="metric-item">
                                <div class="metric-label">TOTAL AIR</div>
                                <div class="metric-value" style="font-size: 13px;">{forwarder['total_air_tons']}</div>
                            </div>
                        </div>
                    </div>
                </div>
                <div class="card-footer">
                    <span class="footer-date">DATE: {forwarder['date']}</span>
                    <a href="Forwarder/index.html" class="footer-link">OPEN &rarr;</a>
                </div>
            </div>

        </div>

        <!-- AIR SECTION -->
        <div class="section-header">// AIR</div>
        <div class="grid-2">

            <!-- 5. Air Freight Rate Card (Sub-headers Removed) -->
            <div class="card">
                <div>
                    <div class="card-header">
                        <span class="card-title">Air Freight Rate</span>
                        <span class="badge">ACTIVE</span>
                    </div>
                    <div class="card-body">
                        <div class="metrics-grid-3">
                            <div class="metric-item">
                                <div class="metric-label" title="Frankfurt-North America">FRA - NAM</div>
                                <div class="metric-value">{tac['routes']['Frankfurt-North America']['price']} <span class="unit">USD/kg</span></div>
                                <div class="metric-delta {tac['routes']['Frankfurt-North America']['class']}">{tac['routes']['Frankfurt-North America']['delta']} {tac['routes']['Frankfurt-North America']['arrow']}</div>
                            </div>
                            <div class="metric-item">
                                <div class="metric-label" title="Hong Kong-Europe">HKG - EUR</div>
                                <div class="metric-value">{tac['routes']['Hong Kong-Europe']['price']} <span class="unit">USD/kg</span></div>
                                <div class="metric-delta {tac['routes']['Hong Kong-Europe']['class']}">{tac['routes']['Hong Kong-Europe']['delta']} {tac['routes']['Hong Kong-Europe']['arrow']}</div>
                            </div>
                            <div class="metric-item">
                                <div class="metric-label" title="Hong Kong-North America">HKG - NAM</div>
                                <div class="metric-value">{tac['routes']['Hong Kong-North America']['price']} <span class="unit">USD/kg</span></div>
                                <div class="metric-delta {tac['routes']['Hong Kong-North America']['class']}">{tac['routes']['Hong Kong-North America']['delta']} {tac['routes']['Hong Kong-North America']['arrow']}</div>
                            </div>
                        </div>
                    </div>
                </div>
                <div class="card-footer">
                    <span class="footer-date">DATE: {tac['date']}</span>
                    <a href="TAC%20Index/index.html" class="footer-link">OPEN &rarr;</a>
                </div>
            </div>

        </div>

        <!-- GLOBAL SECTION -->
        <div class="section-header">// GLOBAL</div>
        <div class="grid-2">

            <!-- 6. Macro Economies & GSCPI Card -->
            <div class="card">
                <div>
                    <div class="card-header">
                        <span class="card-title">Macro Economies & GSCPI</span>
                        <span class="badge">ACTIVE</span>
                    </div>
                    <div class="card-body">
                        <div class="metrics-grid-3">
                            <div class="metric-item">
                                <div class="metric-label">GSCPI</div>
                                <div class="metric-value">{macro['gscpi']['value']} <span class="unit">pts</span></div>
                                <div class="metric-delta {macro['gscpi']['class']}">{macro['gscpi']['delta']} {macro['gscpi']['arrow']}</div>
                            </div>
                            <div class="metric-item">
                                <div class="metric-label">US GDP (QoQ)</div>
                                <div class="metric-value">{macro['us_gdp']['value']}</div>
                                <div class="metric-delta {macro['us_gdp']['class']}">{macro['us_gdp']['delta']} {macro['us_gdp']['arrow']}</div>
                            </div>
                            <div class="metric-item">
                                <div class="metric-label">US CPI (YoY)</div>
                                <div class="metric-value">{macro['us_cpi']['value']}</div>
                                <div class="metric-delta {macro['us_cpi']['class']}">{macro['us_cpi']['delta']} {macro['us_cpi']['arrow']}</div>
                            </div>
                        </div>
                    </div>
                </div>
                <div class="card-footer">
                    <span class="footer-date">DATE: {macro['date']}</span>
                    <a href="MacroEconomies/index.html" class="footer-link">OPEN &rarr;</a>
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
    print("[INFO] Fetching module metrics...")
    shipping_data = fetch_shipping_data()
    alphaliner_data = fetch_alphaliner_data()
    bunker_data = fetch_bunker_data()
    forwarder_data = fetch_forwarder_data()
    tac_data = fetch_tac_data()
    macro_data = fetch_macro_data()

    # 1. Generate Root index.html
    print(f"[INFO] Generating root dashboard HTML -> {OUTPUT_HTML}")
    html_content = generate_html(
        shipping_data,
        alphaliner_data,
        bunker_data,
        forwarder_data,
        tac_data,
        macro_data
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
        "macro": macro_data
    }
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(summary_data, f, ensure_ascii=False, indent=2)

    print("[SUCCESS] Market Intelligence root dashboard generation complete.")

if __name__ == "__main__":
    main()
