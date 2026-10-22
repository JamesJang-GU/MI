#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Market Intelligence Portal - Root Dashboard Generator
Bloomberg Professional Terminal High-Density Interface

Reads verified module SQLite databases and generates:
- index.html
- dashboard_summary.json
"""

import html
import json
import os
import sqlite3
from datetime import datetime, timedelta

# ==========================================
# PATH CONFIGURATION
# ==========================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DB_PATHS = {
    # index_raw(dt, region, value, index_name, source_file, created_at)
    "shipping": os.path.join(
        BASE_DIR,
        "Shipping Index",
        "index_history.db",
    ),
    # alphaliner_master / alphaliner_detail
    "alphaliner": os.path.join(
        BASE_DIR,
        "Alphaliner",
        "alphaliner_history.db",
    ),
    # fleet_snapshot(snapshot_date, ship_status, ships, teu, source_url, collected_at)
    "linerlytica": os.path.join(
        BASE_DIR,
        "Linerlytica",
        "Linerlytica.db",
    ),
    # product_vlsfo / oil_prices
    "bunker": os.path.join(
        BASE_DIR,
        "BunkerPrice",
        "bunker_price_data.db",
    ),
    # forwarders(date, rank, provider, teu, air_tons, ...)
    "forwarder": os.path.join(
        BASE_DIR,
        "Forwarder",
        "freight_forwarders_data.db",
    ),
    # tac_master / tac_detail
    "tac": os.path.join(
        BASE_DIR,
        "TAC Index",
        "TAC_Index.db",
    ),
    "macro": os.path.join(
        BASE_DIR,
        "MacroEconomies",
        "macro_data.db",
    ),
}

OUTPUT_HTML = os.path.join(BASE_DIR, "index.html")
OUTPUT_JSON = os.path.join(BASE_DIR, "dashboard_summary.json")

# ==========================================
# COMMON DATABASE HELPERS
# ==========================================
def table_exists(cursor, table_name):
    """Return True when the SQLite table exists."""
    cursor.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        LIMIT 1
        """,
        (table_name,),
    )
    return cursor.fetchone() is not None

def get_table_columns(cursor, table_name):
    """Return the table columns as a lower-case set."""
    cursor.execute(f'PRAGMA table_info("{table_name}")')
    return {row[1].lower() for row in cursor.fetchall()}

def safe_float(value):
    """Safely convert value to float or return None."""
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None

# ==========================================
# DELTA & SPARKLINE FORMATTING HELPERS
# ==========================================
def calculate_delta(curr, prev, is_int=False):
    """
    Calculate absolute and percentage deltas
    with Bloomberg-style directional indicators.
    """
    if curr is None:
        return "-", "neutral", ""

    if prev is None or prev == 0:
        return "-", "neutral", ""

    diff = curr - prev
    pct = (diff / prev) * 100.0

    if diff > 0:
        arrow = "▲"
        css_class = "up"
        diff_str = f"+{int(diff):,}" if is_int else f"+{diff:.2f}"
        pct_str = f"+{pct:.2f}%"

    elif diff < 0:
        arrow = "▼"
        css_class = "down"
        diff_str = f"{int(diff):,}" if is_int else f"{diff:.2f}"
        pct_str = f"{pct:.2f}%"

    else:
        arrow = "■"
        css_class = "neutral"
        diff_str = "0" if is_int else "0.00"
        pct_str = "0.00%"

    return f"{diff_str} ({pct_str})", css_class, arrow

def build_sparkline_svg(values, color, chart_name):
    """
    Build a compact one-year SVG sparkline.
    Values must be ordered from oldest to newest.
    """
    if not values:
        return """
        <div class="sparkline-empty">
            NO 1Y HISTORY
        </div>
        """

    chart_width = 100
    chart_height = 44
    padding_top = 3
    padding_bottom = 4
    usable_height = chart_height - padding_top - padding_bottom

    min_value = min(values)
    max_value = max(values)
    value_range = max_value - min_value

    if value_range == 0:
        value_range = 1.0

    point_count = len(values)
    points = []

    for index, value in enumerate(values):
        if point_count == 1:
            x = chart_width / 2
        else:
            x = (index / (point_count - 1)) * chart_width

        y = padding_top + ((max_value - value) / value_range) * usable_height
        points.append(f"{x:.2f},{y:.2f}")

    point_string = " ".join(points)
    area_points = f"0,{chart_height} {point_string} {chart_width},{chart_height}"

    return f"""
    <svg
        class="shipping-sparkline-svg"
        viewBox="0 0 {chart_width} {chart_height}"
        preserveAspectRatio="none"
        role="img"
        aria-label="{html.escape(chart_name)} one-year trend"
    >
        <line x1="0" y1="11" x2="{chart_width}" y2="11" class="sparkline-grid" />
        <line x1="0" y1="22" x2="{chart_width}" y2="22" class="sparkline-grid" />
        <line x1="0" y1="33" x2="{chart_width}" y2="33" class="sparkline-grid" />

        <polygon
            points="{area_points}"
            fill="{color}"
            fill-opacity="0.10"
        />

        <polyline
            points="{point_string}"
            fill="none"
            stroke="{color}"
            stroke-width="1.6"
            vector-effect="non-scaling-stroke"
            stroke-linecap="square"
            stroke-linejoin="round"
        />
    </svg>
    """

# ==========================================
# DATA FETCHERS
# ==========================================
def fetch_shipping_data():
    """
    Fetch SCFI Composite and BDI Composite from index_raw table.
    """
    data = {
        "scfi": {
            "price": "N/A",
            "delta": "-",
            "class": "neutral",
            "arrow": "",
            "date": "-",
            "sparkline": "",
            "history_points": 0,
        },
        "bdi": {
            "price": "N/A",
            "delta": "-",
            "class": "neutral",
            "arrow": "",
            "date": "-",
            "sparkline": "",
            "history_points": 0,
        },
        "date": "-",
    }

    db_path = DB_PATHS["shipping"]

    if not os.path.exists(db_path):
        print(f"[WARN] Shipping DB not found: {db_path}")
        return data

    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()

            if not table_exists(cursor, "index_raw"):
                print("[WARN] Shipping table 'index_raw' not found.")
                return data

            def parse_observation_date(raw_date):
                if raw_date is None:
                    return None
                date_text = str(raw_date)[:10]
                try:
                    return datetime.strptime(date_text, "%Y-%m-%d")
                except ValueError:
                    return None

            def load_index(index_name, region, is_int=False, color="#FF9900"):
                cursor.execute(
                    """
                    SELECT dt, value, created_at
                    FROM index_raw
                    WHERE index_name = ?
                      AND region = ?
                      AND value IS NOT NULL
                    ORDER BY dt ASC, created_at DESC
                    """,
                    (index_name, region),
                )

                rows = cursor.fetchall()
                if not rows:
                    print(f"[WARN] Shipping data not found: {index_name} {region}")
                    return None

                observations_by_date = {}
                for raw_date, raw_value, _created_at in rows:
                    parsed_date = parse_observation_date(raw_date)
                    if parsed_date is None or raw_value is None:
                        continue

                    date_key = parsed_date.strftime("%Y-%m-%d")
                    if date_key not in observations_by_date:
                        observations_by_date[date_key] = {
                            "date": parsed_date,
                            "value": float(raw_value),
                        }

                observations = sorted(
                    observations_by_date.values(),
                    key=lambda item: item["date"],
                )

                if not observations:
                    return None

                latest = observations[-1]
                previous = observations[-2] if len(observations) > 1 else None

                latest_date = latest["date"].strftime("%Y-%m-%d")
                current_value = latest["value"]
                previous_value = previous["value"] if previous else None

                delta_str, css_class, arrow = calculate_delta(
                    current_value,
                    previous_value,
                    is_int=is_int,
                )

                price_str = f"{int(current_value):,}" if is_int else f"{current_value:,.2f}"

                one_year_ago = latest["date"] - timedelta(days=365)
                one_year_values = [
                    item["value"]
                    for item in observations
                    if item["date"] >= one_year_ago
                ]

                sparkline_svg = build_sparkline_svg(
                    values=one_year_values,
                    color=color,
                    chart_name=f"{index_name} {region}",
                )

                return {
                    "price": price_str,
                    "delta": delta_str,
                    "class": css_class,
                    "arrow": arrow,
                    "date": latest_date,
                    "sparkline": sparkline_svg,
                    "history_points": len(one_year_values),
                }

            scfi_result = load_index(index_name="SCFI", region="종합", is_int=False, color="#FF9900")
            bdi_result = load_index(index_name="BDI", region="BDI", is_int=True, color="#00E5FF")

            if scfi_result:
                data["scfi"] = scfi_result
                data["date"] = scfi_result["date"]

            if bdi_result:
                data["bdi"] = bdi_result
                if data["date"] == "-":
                    data["date"] = bdi_result["date"]

    except Exception as error:
        print(f"[WARN] Shipping data load error: {error}")

    return data

def fetch_alphaliner_data():
    """
    Read the latest Alphaliner snapshot and Top 10 aggregate market share.
    """
    data = {
        "total_ships": "N/A",
        "total_teu": "N/A",
        "top10_share": "N/A",
        "date": "N/A",
    }

    db_path = DB_PATHS["alphaliner"]

    if not os.path.exists(db_path):
        print(f"[WARN] Alphaliner DB not found: {db_path}")
        return data

    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()

            if not table_exists(cursor, "alphaliner_master") or not table_exists(cursor, "alphaliner_detail"):
                print("[WARN] Alphaliner required tables not found.")
                return data

            cursor.execute(
                """
                SELECT
                    snapshot_id,
                    data_date,
                    global_ships,
                    total_ships,
                    global_teu,
                    total_teu
                FROM alphaliner_master
                ORDER BY data_date DESC, snapshot_id DESC
                LIMIT 1
                """
            )

            row = cursor.fetchone()
            if not row:
                return data

            (
                snapshot_id,
                data_date,
                global_ships,
                total_ships,
                global_teu,
                total_teu,
            ) = row

            data["date"] = str(data_date)[:10] if data_date else "N/A"

            ships = global_ships if (global_ships is not None and global_ships > 0) else total_ships
            teu = global_teu if (global_teu is not None and global_teu > 0) else total_teu

            if ships is not None:
                data["total_ships"] = f"{int(ships):,} Ships"

            if teu is not None:
                data["total_teu"] = f"{int(teu):,} TEU"

            cursor.execute(
                """
                SELECT SUM(market_share)
                FROM alphaliner_detail
                WHERE snapshot_id = ?
                  AND rank <= 10
                """,
                (snapshot_id,),
            )

            share_row = cursor.fetchone()
            if share_row and share_row[0] is not None:
                data["top10_share"] = f"{float(share_row[0]):.1f}%"

    except Exception as error:
        print(f"[WARN] Alphaliner data load error: {error}")

    return data

def fetch_linerlytica_data():
    """
    Fetch the latest Linerlytica Global Containership Fleet Snapshot.

    Verified schema:
        fleet_snapshot(
            snapshot_date TEXT,
            ship_status TEXT,
            ships INTEGER,
            teu INTEGER,
            source_url TEXT,
            collected_at TEXT,
            PRIMARY KEY(snapshot_date, ship_status)
        )

    Target categories:
    - Active Ships
    - Ships at port
    - Ships at anchorage
    - Ships in shipyard
    - Inactive Ships

    Sort order:
    - Ships DESC, TEU DESC
    """
    data = {
        "date": "N/A",
        "rows": [],
        "source_table": "fleet_snapshot",
    }

    db_path = DB_PATHS["linerlytica"]

    if not os.path.exists(db_path):
        print(f"[WARN] Linerlytica DB not found: {db_path}")
        return data

    target_statuses = [
        "Ships at port",
        "Active Ships",
        "Inactive Ships",
        "Ships at anchorage",
        "Ships in shipyard",
    ]

    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()

            if not table_exists(cursor, "fleet_snapshot"):
                print("[WARN] Linerlytica table 'fleet_snapshot' not found.")
                return data

            cursor.execute(
                """
                SELECT MAX(snapshot_date)
                FROM fleet_snapshot
                WHERE snapshot_date IS NOT NULL
                """
            )

            latest_date_row = cursor.fetchone()
            latest_date = latest_date_row[0] if latest_date_row else None

            if not latest_date:
                print("[WARN] Linerlytica fleet_snapshot contains no snapshot date.")
                return data

            placeholders = ", ".join(["?"] * len(target_statuses))

            cursor.execute(
                f"""
                SELECT
                    ship_status,
                    ships,
                    teu
                FROM fleet_snapshot
                WHERE snapshot_date = ?
                  AND ship_status IN ({placeholders})
                ORDER BY
                    ships DESC,
                    teu DESC,
                    ship_status ASC
                """,
                (latest_date, *target_statuses),
            )

            rows = cursor.fetchall()
            if not rows:
                print("[WARN] Linerlytica latest snapshot has no matching status records.")
                return data

            data["date"] = str(latest_date)[:10]
            data["rows"] = [
                {
                    "status": str(ship_status).strip(),
                    "ships": int(ships) if ships is not None else 0,
                    "teu": int(teu) if teu is not None else 0,
                }
                for ship_status, ships, teu in rows
            ]

            print(
                f"[INFO] Linerlytica latest fleet snapshot loaded: "
                f"date={data['date']}, records={len(data['rows'])}"
            )

    except sqlite3.Error as error:
        print(f"[WARN] Linerlytica SQLite load error: {error}")
    except Exception as error:
        print(f"[WARN] Unexpected Linerlytica data error: {error}")

    return data

def fetch_forwarder_data():
    """
    Fetch latest Top 25 Forwarder results from forwarders table.
    """
    data = {
        "top_forwarder": "N/A",
        "total_ocean_teu": "N/A",
        "total_air_tons": "N/A",
        "date": "N/A",
    }

    db_path = DB_PATHS["forwarder"]

    if not os.path.exists(db_path):
        print(f"[WARN] Forwarder DB not found: {db_path}")
        return data

    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()

            if not table_exists(cursor, "forwarders"):
                print("[WARN] Forwarder table 'forwarders' not found.")
                return data

            cursor.execute("SELECT MAX(date) FROM forwarders")
            max_date_row = cursor.fetchone()

            if not max_date_row or not max_date_row[0]:
                return data

            target_date = max_date_row[0]
            data["date"] = str(target_date)[:10]

            cursor.execute(
                """
                SELECT provider
                FROM forwarders
                WHERE date = ?
                ORDER BY rank ASC
                LIMIT 1
                """,
                (target_date,),
            )

            top_row = cursor.fetchone()
            if top_row and top_row[0]:
                data["top_forwarder"] = str(top_row[0])

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
                if sum_row[0] is not None:
                    data["total_ocean_teu"] = f"{int(sum_row[0]):,} TEU"
                if sum_row[1] is not None:
                    data["total_air_tons"] = f"{int(sum_row[1]):,} MT"

    except Exception as error:
        print(f"[WARN] Forwarder data load error: {error}")

    return data

def fetch_tac_data():
    """
    Fetch three TAC air freight corridors sorted chronologically by tac_master.data_date.
    """
    data = {
        "date": "N/A",
        "routes": {
            "FRA - NAM": {
                "name": "Frankfurt - North America",
                "price": "N/A",
                "delta": "-",
                "class": "neutral",
                "arrow": "",
            },
            "HKG - EUR": {
                "name": "Hong Kong - Europe",
                "price": "N/A",
                "delta": "-",
                "class": "neutral",
                "arrow": "",
            },
            "HKG - NAM": {
                "name": "Hong Kong - North America",
                "price": "N/A",
                "delta": "-",
                "class": "neutral",
                "arrow": "",
            },
        },
    }

    db_path = DB_PATHS["tac"]

    if not os.path.exists(db_path):
        print(f"[WARN] TAC DB not found: {db_path}")
        return data

    corridor_targets = {
        "FRA - NAM": {
            "origin": "frankfurt",
            "destination": "north america",
        },
        "HKG - EUR": {
            "origin": "hong kong",
            "destination": "europe",
        },
        "HKG - NAM": {
            "origin": "hong kong",
            "destination": "north america",
        },
    }

    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()

            if not table_exists(cursor, "tac_master") or not table_exists(cursor, "tac_detail"):
                print("[WARN] TAC tables not found.")
                return data

            for route_key, target in corridor_targets.items():
                cursor.execute(
                    """
                    SELECT
                        m.data_date,
                        d.index_value
                    FROM tac_detail d
                    JOIN tac_master m
                      ON d.snapshot_id = m.snapshot_id
                    WHERE d.index_value IS NOT NULL
                      AND LOWER(COALESCE(d.origin, '')) LIKE ?
                      AND LOWER(COALESCE(d.destination, '')) LIKE ?
                    ORDER BY m.data_date DESC, m.snapshot_id DESC
                    """,
                    (f"%{target['origin']}%", f"%{target['destination']}%"),
                )

                raw_rows = cursor.fetchall()
                if not raw_rows:
                    continue

                unique_rows = []
                seen_dates = set()

                for row_date, row_value in raw_rows:
                    normalized_date = str(row_date)[:10]
                    if normalized_date in seen_dates:
                        continue

                    seen_dates.add(normalized_date)
                    unique_rows.append((normalized_date, row_value))

                    if len(unique_rows) >= 2:
                        break

                if not unique_rows:
                    continue

                latest_date = unique_rows[0][0]
                current_value = float(unique_rows[0][1])

                previous_value = None
                if len(unique_rows) > 1 and unique_rows[1][1] is not None:
                    previous_value = float(unique_rows[1][1])

                delta_str, css_class, arrow = calculate_delta(current_value, previous_value)

                data["routes"][route_key] = {
                    "name": data["routes"][route_key]["name"],
                    "price": f"{current_value:.2f}",
                    "delta": delta_str,
                    "class": css_class,
                    "arrow": arrow,
                }

                if data["date"] == "N/A":
                    data["date"] = latest_date

    except Exception as error:
        print(f"[WARN] TAC data load error: {error}")

    return data

def fetch_bunker_and_crude_data():
    """
    Fetch VLSFO prices (Rotterdam, Fujairah, Singapore)
    and Crude oil prices (Dubai, Brent, WTI).
    """
    data = {
        "date": "N/A",
        "vlsfo": {
            "Rotterdam": {"price": "N/A", "delta": "-", "class": "neutral", "arrow": ""},
            "Fujairah": {"price": "N/A", "delta": "-", "class": "neutral", "arrow": ""},
            "Singapore": {"price": "N/A", "delta": "-", "class": "neutral", "arrow": ""},
        },
        "crude": {
            "Dubai": {"price": "N/A", "delta": "-", "class": "neutral", "arrow": ""},
            "Brent": {"price": "N/A", "delta": "-", "class": "neutral", "arrow": ""},
            "WTI": {"price": "N/A", "delta": "-", "class": "neutral", "arrow": ""},
        },
    }

    db_path = DB_PATHS["bunker"]

    if not os.path.exists(db_path):
        print(f"[WARN] Bunker DB not found: {db_path}")
        return data

    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()

            if table_exists(cursor, "product_vlsfo"):
                for port in ["Rotterdam", "Fujairah", "Singapore"]:
                    cursor.execute(
                        """
                        SELECT date_str, price
                        FROM product_vlsfo
                        WHERE port = ?
                          AND price IS NOT NULL
                        ORDER BY date_str DESC, timestamp_ms DESC
                        LIMIT 2
                        """,
                        (port,),
                    )

                    rows = cursor.fetchall()
                    if not rows:
                        continue

                    latest_date = str(rows[0][0])[:10]
                    current_value = float(rows[0][1])

                    previous_value = None
                    if len(rows) > 1 and rows[1][1] is not None:
                        previous_value = float(rows[1][1])

                    delta_str, css_class, arrow = calculate_delta(current_value, previous_value)

                    data["vlsfo"][port] = {
                        "price": f"{current_value:.1f}",
                        "delta": delta_str,
                        "class": css_class,
                        "arrow": arrow,
                    }

                    if data["date"] == "N/A":
                        data["date"] = latest_date

            if table_exists(cursor, "oil_prices"):
                cursor.execute(
                    """
                    SELECT DATE, Dubai, Brent, WTI
                    FROM oil_prices
                    ORDER BY DATE DESC
                    LIMIT 2
                    """
                )

                oil_rows = cursor.fetchall()
                if oil_rows:
                    current_row = oil_rows[0]
                    previous_row = oil_rows[1] if len(oil_rows) > 1 else None

                    oil_indices = {"Dubai": 1, "Brent": 2, "WTI": 3}

                    for oil_name, column_index in oil_indices.items():
                        current_value = safe_float(current_row[column_index])
                        previous_value = safe_float(previous_row[column_index]) if previous_row else None

                        if current_value is None:
                            continue

                        delta_str, css_class, arrow = calculate_delta(current_value, previous_value)

                        data["crude"][oil_name] = {
                            "price": f"{current_value:.2f}",
                            "delta": delta_str,
                            "class": css_class,
                            "arrow": arrow,
                        }

    except Exception as error:
        print(f"[WARN] Bunker & Crude load error: {error}")

    return data

def fetch_macro_data():
    """
    Macro module placeholder values.
    """
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
def build_linerlytica_rows(linerlytica):
    """Build high-density HTML table rows for Linerlytica ship statuses."""
    if not linerlytica["rows"]:
        return """
        <tr>
            <td colspan="3" class="linerlytica-empty">
                DATA UNAVAILABLE
            </td>
        </tr>
        """

    output = []
    for row in linerlytica["rows"]:
        status = html.escape(row["status"].upper())
        ships = f"{row['ships']:,}"
        teu = f"{row['teu']:,}"

        output.append(
            f"""
            <tr>
                <td class="table-row-name">{status}</td>
                <td class="table-row-val">{ships}<span class="inline-unit">SHIPS</span></td>
                <td class="table-row-val">{teu}<span class="inline-unit">TEU</span></td>
            </tr>
            """
        )

    return "\n".join(output)

def generate_html(shipping, alphaliner, linerlytica, forwarder, tac, bunker, macro):
    linerlytica_rows_html = build_linerlytica_rows(linerlytica)

    html_content = f"""<!DOCTYPE html>
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

        .grid-4 {{
            display: grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            gap: 10px;
            align-items: stretch;
        }}

        .grid-2 {{
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 10px;
            align-items: stretch;
        }}

        .grid-1 {{
            display: grid;
            grid-template-columns: minmax(0, 1fr);
            gap: 10px;
        }}

        .panel {{
            background: var(--panel-bg);
            border: 1px solid var(--panel-border);
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            min-width: 0;
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

        .metric-cell {{
            background: var(--panel-inner);
            border: 1px solid #141C24;
            padding: 6px 8px;
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
            margin-top: 2px;
        }}

        .metric-value {{
            font-size: 16px;
            font-weight: 800;
            color: var(--bb-white);
        }}

        .dense-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 10px;
        }}

        .dense-table th {{
            text-align: left;
            padding: 4px 6px;
            font-size: 9px;
            color: var(--bb-gray);
            border-bottom: 1px solid var(--panel-border);
            text-transform: uppercase;
        }}

        .dense-table td {{
            padding: 6px;
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
            white-space: nowrap;
        }}

        .table-row-delta {{
            text-align: right;
            font-weight: 700;
            white-space: nowrap;
        }}

        .inline-unit {{
            font-size: 8px;
            font-weight: 600;
            color: var(--bb-cyan);
            margin-left: 3px;
        }}

        .up {{ color: var(--bb-green); }}
        .down {{ color: var(--bb-red); }}
        .neutral {{ color: var(--bb-neutral); }}

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

        /* SHIPPING INDEX SPLIT VIEW */
        .shipping-panel .panel-body {{
            padding: 8px;
            display: block;
            min-height: 0;
        }}

        .shipping-split {{
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            height: 100%;
        }}

        .shipping-index-column {{
            min-width: 0;
            display: flex;
            flex-direction: column;
            padding: 2px 8px 3px 8px;
        }}

        .shipping-index-column:first-child {{
            padding-left: 2px;
        }}

        .shipping-index-column + .shipping-index-column {{
            border-left: 1px solid var(--panel-border);
            padding-right: 2px;
        }}

        .shipping-index-label {{
            min-height: 26px;
            font-size: 10px;
            font-weight: 800;
            color: var(--bb-gray);
            letter-spacing: 0.04em;
            line-height: 1.3;
        }}

        .shipping-chart-meta {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin: 2px 0;
            font-size: 8px;
            font-weight: 700;
            color: var(--bb-gray-dim);
            letter-spacing: 0.06em;
        }}

        .shipping-sparkline {{
            height: 48px;
            margin-bottom: 6px;
            border-top: 1px solid #111820;
            border-bottom: 1px solid #111820;
            background: #06090D;
            overflow: hidden;
        }}

        .shipping-sparkline-svg {{
            display: block;
            width: 100%;
            height: 100%;
        }}

        .sparkline-grid {{
            stroke: #17212B;
            stroke-width: 0.45;
            vector-effect: non-scaling-stroke;
        }}

        .sparkline-empty {{
            height: 100%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 8px;
            font-weight: 700;
            color: var(--bb-gray-dim);
            letter-spacing: 0.05em;
        }}

        .shipping-metric-bottom {{
            margin-top: auto;
            padding-top: 2px;
        }}

        .shipping-metric-value {{
            display: block;
            color: var(--bb-white);
            font-size: 16px;
            font-weight: 800;
            letter-spacing: -0.03em;
            white-space: nowrap;
        }}

        .shipping-metric-value .unit {{
            margin-left: 2px;
            font-size: 8px;
            font-weight: 600;
            color: var(--bb-cyan);
            letter-spacing: 0;
        }}

        .shipping-metric-delta {{
            display: block;
            margin-top: 2px;
            font-size: 9px;
            font-weight: 700;
            white-space: nowrap;
        }}

        .linerlytica-empty {{
            text-align: center;
            color: var(--bb-gray);
            padding: 32px 8px !important;
        }}

        @media (max-width: 1200px) {{
            .grid-4 {{
                grid-template-columns: repeat(2, minmax(0, 1fr));
            }}
        }}

        @media (max-width: 768px) {{
            .grid-4,
            .grid-2 {{
                grid-template-columns: 1fr;
            }}

            .section-bar {{
                display: block;
            }}

            .section-bar span.sub {{
                display: block;
                margin-top: 4px;
            }}
        }}
    </style>
</head>
<body>
    <div class="terminal-container">

        <div class="terminal-header">
            <div class="terminal-title">
                <span class="terminal-badge">PRO</span>
                <span>MARKET INTELLIGENCE TERMINAL</span>
            </div>

            <div class="terminal-status">
                <span>FEED: <span class="status-tag">LIVE_ONLINE</span></span>
                <span>SYS_DATE: {datetime.today().strftime('%Y-%m-%d')}</span>
            </div>
        </div>

        <!-- ================================================= -->
        <!-- 01. SEA                                           -->
        <!-- ================================================= -->
        <div class="section-bar">
            <span>01. SEA</span>
            <span class="sub">MARITIME LOGISTICS & FREIGHT</span>
        </div>

        <div class="grid-4">

            <!-- 1. SHIPPING INDEX -->
            <div class="panel shipping-panel">
                <div class="panel-header">
                    <span class="panel-headline">SHIPPING INDEX</span>
                    <a href="Shipping%20Index/index.html" class="panel-link">INDEX &gt;</a>
                </div>

                <div class="panel-body">
                    <div class="shipping-split">

                        <!-- SCFI -->
                        <div class="shipping-index-column">
                            <div class="shipping-index-label">
                                SCFI COMPOSITE INDEX
                            </div>

                            <div class="shipping-chart-meta">
                                <span>1Y TREND</span>
                                <span>{shipping['scfi']['history_points']} OBS</span>
                            </div>

                            <div class="shipping-sparkline">
                                {shipping['scfi']['sparkline']}
                            </div>

                            <div class="shipping-metric-bottom">
                                <span class="shipping-metric-value">
                                    {shipping['scfi']['price']}
                                    <span class="unit">PTS</span>
                                </span>

                                <span class="shipping-metric-delta {shipping['scfi']['class']}">
                                    {shipping['scfi']['arrow']} {shipping['scfi']['delta']}
                                </span>
                            </div>
                        </div>

                        <!-- BDI -->
                        <div class="shipping-index-column">
                            <div class="shipping-index-label">
                                BALTIC DRY INDEX
                            </div>

                            <div class="shipping-chart-meta">
                                <span>1Y TREND</span>
                                <span>{shipping['bdi']['history_points']} OBS</span>
                            </div>

                            <div class="shipping-sparkline">
                                {shipping['bdi']['sparkline']}
                            </div>

                            <div class="shipping-metric-bottom">
                                <span class="shipping-metric-value">
                                    {shipping['bdi']['price']}
                                    <span class="unit">PTS</span>
                                </span>

                                <span class="shipping-metric-delta {shipping['bdi']['class']}">
                                    {shipping['bdi']['arrow']} {shipping['bdi']['delta']}
                                </span>
                            </div>
                        </div>

                    </div>
                </div>

                <div class="panel-footer">
                    <span>AS OF: {shipping['date']}</span>
                    <span class="footer-tag">SCFI / BALTIC · 1Y</span>
                </div>
            </div>

            <!-- 2. ALPHALINER FLEET -->
            <div class="panel">
                <div class="panel-header">
                    <span class="panel-headline">ALPHALINER FLEET</span>
                    <a href="Alphaliner/index.html" class="panel-link">FLEET &gt;</a>
                </div>

                <div class="panel-body">
                    <div class="metric-cell">
                        <span class="metric-label">TOTAL OPERATING FLEET</span>
                        <div class="metric-value-row">
                            <span class="metric-value">{alphaliner['total_ships']}</span>
                        </div>
                    </div>

                    <div class="metric-cell">
                        <span class="metric-label">GLOBAL TEU CAPACITY</span>
                        <div class="metric-value-row">
                            <span class="metric-value">{alphaliner['total_teu']}</span>
                        </div>
                    </div>

                    <div class="metric-cell">
                        <span class="metric-label">TOP 10 CARRIER SHARE</span>
                        <div class="metric-value-row">
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

            <!-- 3. LINERLYTICA -->
            <div class="panel">
                <div class="panel-header">
                    <span class="panel-headline">LINERLYTICA</span>
                    <a href="Linerlytica/index.html" class="panel-link">FLEET &gt;</a>
                </div>

                <div class="panel-body" style="padding: 6px 10px;">
                    <table class="dense-table">
                        <thead>
                            <tr>
                                <th>SHIP STATUS</th>
                                <th style="text-align:right;">SHIPS</th>
                                <th style="text-align:right;">CAPACITY</th>
                            </tr>
                        </thead>

                        <tbody>
                            {linerlytica_rows_html}
                        </tbody>
                    </table>
                </div>

                <div class="panel-footer">
                    <span>AS OF: {linerlytica['date']}</span>
                    <span class="footer-tag">LINERLYTICA</span>
                </div>
            </div>

            <!-- 4. TOP 25 FORWARDERS -->
            <div class="panel">
                <div class="panel-header">
                    <span class="panel-headline">TOP 25 FORWARDERS</span>
                    <a href="Forwarder/index.html" class="panel-link">FWD &gt;</a>
                </div>

                <div class="panel-body">
                    <div class="metric-cell">
                        <span class="metric-label">RANK 1 OPERATOR</span>
                        <div class="metric-value-row">
                            <span class="metric-value" style="font-size:14px; color:var(--bb-amber);">
                                {html.escape(forwarder['top_forwarder'])}
                            </span>
                        </div>
                    </div>

                    <div class="metric-cell">
                        <span class="metric-label">TOTAL OCEAN FREIGHT</span>
                        <div class="metric-value-row">
                            <span class="metric-value">{forwarder['total_ocean_teu']}</span>
                        </div>
                    </div>

                    <div class="metric-cell">
                        <span class="metric-label">TOTAL AIR TONNAGE</span>
                        <div class="metric-value-row">
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

        <!-- ================================================= -->
        <!-- 02. AIR                                           -->
        <!-- ================================================= -->
        <div class="section-bar">
            <span>02. AIR</span>
            <span class="sub">AIR FREIGHT RATE</span>
        </div>

        <div class="grid-1">

            <div class="panel">
                <div class="panel-header">
                    <span class="panel-headline">AIR FREIGHT RATE</span>
                    <a href="TAC%20Index/index.html" class="panel-link">TAC &gt;</a>
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
                                <td class="table-row-delta {tac['routes']['FRA - NAM']['class']}">
                                    {tac['routes']['FRA - NAM']['arrow']} {tac['routes']['FRA - NAM']['delta']}
                                </td>
                            </tr>

                            <tr>
                                <td class="table-row-name">HONG KONG - EUROPE (HKG-EUR)</td>
                                <td class="table-row-val">{tac['routes']['HKG - EUR']['price']}</td>
                                <td class="table-row-delta {tac['routes']['HKG - EUR']['class']}">
                                    {tac['routes']['HKG - EUR']['arrow']} {tac['routes']['HKG - EUR']['delta']}
                                </td>
                            </tr>

                            <tr>
                                <td class="table-row-name">HONG KONG - NORTH AMERICA (HKG-NAM)</td>
                                <td class="table-row-val">{tac['routes']['HKG - NAM']['price']}</td>
                                <td class="table-row-delta {tac['routes']['HKG - NAM']['class']}">
                                    {tac['routes']['HKG - NAM']['arrow']} {tac['routes']['HKG - NAM']['delta']}
                                </td>
                            </tr>
                        </tbody>
                    </table>
                </div>

                <div class="panel-footer">
                    <span>AS OF: {tac['date']}</span>
                    <span class="footer-tag">TAC INDEX</span>
                </div>
            </div>

        </div>

        <!-- ================================================= -->
        <!-- 03. MACRO                                         -->
        <!-- ================================================= -->
        <div class="section-bar">
            <span>03. MACRO</span>
            <span class="sub">ENERGY, MACROECONOMICS & SUPPLY CHAIN</span>
        </div>

        <div class="grid-2">

            <!-- 1. BUNKER & CRUDE OIL -->
            <div class="panel">
                <div class="panel-header">
                    <span class="panel-headline">BUNKER & CRUDE OIL</span>
                    <a href="BunkerPrice/index.html" class="panel-link">OIL &gt;</a>
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
                            <tr>
                                <td class="table-row-name">RTM VLSFO</td>
                                <td class="table-row-val">{bunker['vlsfo']['Rotterdam']['price']}</td>
                                <td class="table-row-delta {bunker['vlsfo']['Rotterdam']['class']}">
                                    {bunker['vlsfo']['Rotterdam']['arrow']} {bunker['vlsfo']['Rotterdam']['delta']}
                                </td>
                            </tr>

                            <tr>
                                <td class="table-row-name">FUJ VLSFO</td>
                                <td class="table-row-val">{bunker['vlsfo']['Fujairah']['price']}</td>
                                <td class="table-row-delta {bunker['vlsfo']['Fujairah']['class']}">
                                    {bunker['vlsfo']['Fujairah']['arrow']} {bunker['vlsfo']['Fujairah']['delta']}
                                </td>
                            </tr>

                            <tr>
                                <td class="table-row-name">SIN VLSFO</td>
                                <td class="table-row-val">{bunker['vlsfo']['Singapore']['price']}</td>
                                <td class="table-row-delta {bunker['vlsfo']['Singapore']['class']}">
                                    {bunker['vlsfo']['Singapore']['arrow']} {bunker['vlsfo']['Singapore']['delta']}
                                </td>
                            </tr>

                            <tr>
                                <td class="table-row-name" style="color:var(--bb-amber);">DUBAI CRUDE</td>
                                <td class="table-row-val">{bunker['crude']['Dubai']['price']}</td>
                                <td class="table-row-delta {bunker['crude']['Dubai']['class']}">
                                    {bunker['crude']['Dubai']['arrow']} {bunker['crude']['Dubai']['delta']}
                                </td>
                            </tr>

                            <tr>
                                <td class="table-row-name" style="color:var(--bb-amber);">BRENT CRUDE</td>
                                <td class="table-row-val">{bunker['crude']['Brent']['price']}</td>
                                <td class="table-row-delta {bunker['crude']['Brent']['class']}">
                                    {bunker['crude']['Brent']['arrow']} {bunker['crude']['Brent']['delta']}
                                </td>
                            </tr>

                            <tr>
                                <td class="table-row-name" style="color:var(--bb-amber);">WTI CRUDE</td>
                                <td class="table-row-val">{bunker['crude']['WTI']['price']}</td>
                                <td class="table-row-delta {bunker['crude']['WTI']['class']}">
                                    {bunker['crude']['WTI']['arrow']} {bunker['crude']['WTI']['delta']}
                                </td>
                            </tr>
                        </tbody>
                    </table>
                </div>

                <div class="panel-footer">
                    <span>AS OF: {bunker['date']}</span>
                    <span class="footer-tag">USD/MT & USD/BBL</span>
                </div>
            </div>

            <!-- 2. MACRO ECONOMIES & GSCPI -->
            <div class="panel">
                <div class="panel-header">
                    <span class="panel-headline">MACRO ECONOMIES & GSCPI</span>
                    <a href="MacroEconomies/index.html" class="panel-link">MACRO &gt;</a>
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
                                <td class="table-row-val">
                                    {macro['gscpi']['value']}
                                    <span class="inline-unit">PTS</span>
                                </td>
                                <td class="table-row-delta {macro['gscpi']['class']}">
                                    {macro['gscpi']['arrow']} {macro['gscpi']['delta']}
                                </td>
                            </tr>

                            <tr>
                                <td class="table-row-name">US REAL GDP GROWTH (QOQ ANNUALIZED)</td>
                                <td class="table-row-val">{macro['us_gdp']['value']}</td>
                                <td class="table-row-delta {macro['us_gdp']['class']}">
                                    {macro['us_gdp']['arrow']} {macro['us_gdp']['delta']}
                                </td>
                            </tr>

                            <tr>
                                <td class="table-row-name">US HEADLINE CPI INFLATION (YOY)</td>
                                <td class="table-row-val">{macro['us_cpi']['value']}</td>
                                <td class="table-row-delta {macro['us_cpi']['class']}">
                                    {macro['us_cpi']['arrow']} {macro['us_cpi']['delta']}
                                </td>
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
    return html_content

# ==========================================
# MAIN EXECUTION
# ==========================================
def main():
    print("[INFO] Fetching module metrics from verified DB schemas...")

    shipping_data = fetch_shipping_data()
    alphaliner_data = fetch_alphaliner_data()
    linerlytica_data = fetch_linerlytica_data()
    forwarder_data = fetch_forwarder_data()
    tac_data = fetch_tac_data()
    bunker_data = fetch_bunker_and_crude_data()
    macro_data = fetch_macro_data()

    print(f"[INFO] Generating Bloomberg Terminal HTML -> {OUTPUT_HTML}")

    html_content = generate_html(
        shipping=shipping_data,
        alphaliner=alphaliner_data,
        linerlytica=linerlytica_data,
        forwarder=forwarder_data,
        tac=tac_data,
        bunker=bunker_data,
        macro=macro_data,
    )

    with open(OUTPUT_HTML, "w", encoding="utf-8") as file:
        file.write(html_content)

    print(f"[INFO] Exporting summary JSON -> {OUTPUT_JSON}")

    summary_data = {
        "generated_at": datetime.now().isoformat(),
        "shipping": shipping_data,
        "alphaliner": alphaliner_data,
        "linerlytica": linerlytica_data,
        "forwarder": forwarder_data,
        "tac": tac_data,
        "bunker": bunker_data,
        "macro": macro_data,
    }

    with open(OUTPUT_JSON, "w", encoding="utf-8") as file:
        json.dump(
            summary_data,
            file,
            ensure_ascii=False,
            indent=2,
        )

    print("[SUCCESS] Market Intelligence root dashboard generated successfully.")

if __name__ == "__main__":
    main()
