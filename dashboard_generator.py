#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Market Intelligence Portal Dashboard Generator
- Data Sources:
  Shipping Index, Linerlytica, Alphaliner, Forwarder,
  TAC Index, Bunker Price / Crude Oil
- Output:
  index.html, dashboard_summary.json
- Theme:
  Bloomberg / MI Terminal Dark Theme
"""

import os
import sys
import json
import sqlite3
import datetime
import html
from pathlib import Path

# ==============================================================================
# Paths Configuration
# ==============================================================================

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

# ==============================================================================
# Common Database / Utility Functions
# ==============================================================================

def q(identifier):
    """SQLite identifier quote."""
    return '"' + str(identifier).replace('"', '""') + '"'

def safe_float(value, default=None):
    """Safely convert values such as '1,234.50', '$123.5', '12.4%' into float."""
    if value is None:
        return default

    try:
        cleaned = (
            str(value)
            .replace(",", "")
            .replace("$", "")
            .replace("%", "")
            .strip()
        )
        return float(cleaned)
    except (TypeError, ValueError):
        return default

def fmt_number(value, decimals=0, default="-"):
    number = safe_float(value)
    if number is None:
        return default
    return f"{number:,.{decimals}f}"

def fmt_pct(value, decimals=2, default="-"):
    number = safe_float(value)
    if number is None:
        return default
    return f"{number:.{decimals}f}%"

def trend_info(current, previous, decimals=2):
    """
    Returns:
    {
        'delta': '+1.20 (+2.34%)',
        'dir': 'up' | 'down' | 'neutral',
        'arrow': '↗' | '↘' | '→'
    }
    """
    curr = safe_float(current)
    prev = safe_float(previous)

    if curr is None or prev is None:
        return {
            "delta": "-",
            "dir": "neutral",
            "arrow": "→",
        }

    diff = curr - prev
    pct = (diff / prev * 100) if prev != 0 else 0.0

    if diff > 0:
        direction = "up"
        arrow = "↗"
        sign = "+"
    elif diff < 0:
        direction = "down"
        arrow = "↘"
        sign = ""
    else:
        direction = "neutral"
        arrow = "→"
        sign = ""

    return {
        "delta": f"{arrow} {sign}{diff:,.{decimals}f} ({sign}{pct:.{decimals}f}%)",
        "dir": direction,
        "arrow": arrow,
    }

def get_db_connection(db_path: Path):
    """Open SQLite DB and display explicit diagnostics."""
    print(f"[INFO] DB path: {db_path}")
    print(f"[INFO] DB exists: {db_path.exists()}")

    if not db_path.exists():
        print(f"[ERROR] Database file not found: {db_path}")
        return None

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn
    except Exception as exc:
        print(f"[ERROR] Failed to connect DB: {db_path} | {exc}")
        return None

def get_tables(conn):
    """Return all user table names."""
    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()
    return [row["name"] for row in rows]

def get_columns(conn, table_name):
    """Return a lowercase->actual-name column mapping."""
    rows = conn.execute(f"PRAGMA table_info({q(table_name)})").fetchall()
    return {row["name"].lower(): row["name"] for row in rows}

def find_column(columns, candidates):
    """Find actual column name from candidate names, case-insensitively."""
    for candidate in candidates:
        if candidate.lower() in columns:
            return columns[candidate.lower()]
    return None

def print_schema(conn, source_name):
    """Print DB table / column structures to GitHub Actions logs."""
    try:
        tables = get_tables(conn)
        print(f"[DEBUG] {source_name} tables: {tables}")

        for table_name in tables:
            columns = get_columns(conn, table_name)
            print(
                f"[DEBUG] {source_name}.{table_name} columns: "
                f"{list(columns.values())}"
            )
    except Exception as exc:
        print(f"[ERROR] Failed schema inspection for {source_name}: {exc}")

def find_table(conn, preferred_names, required_column_groups=None):
    """
    1. Prefer exact candidate table names.
    2. If unavailable, find a table containing at least one column
       from every required column group.
    """
    tables = get_tables(conn)
    lower_map = {table.lower(): table for table in tables}

    for preferred_name in preferred_names:
        if preferred_name.lower() in lower_map:
            return lower_map[preferred_name.lower()]

    if not required_column_groups:
        return None

    for table_name in tables:
        columns = get_columns(conn, table_name)

        is_match = True
        for candidate_group in required_column_groups:
            if not find_column(columns, candidate_group):
                is_match = False
                break

        if is_match:
            return table_name

    return None

def normalize_route_name(value):
    """Normalize TAC route names for flexible comparison."""
    if value is None:
        return ""

    return (
        str(value)
        .lower()
        .replace("–", "-")
        .replace("—", "-")
        .replace("-", "")
        .replace(" ", "")
        .strip()
    )

# ==============================================================================
# Data Loaders
# ==============================================================================

def load_shipping_index_data():
    data = {
        "scfi": {"current": "-", "change": "-", "dir": "neutral"},
        "bdi": {"current": "-", "change": "-", "dir": "neutral"},
    }

    conn = get_db_connection(DB_PATHS["shipping"])
    if not conn:
        return data

    try:
        print_schema(conn, "Shipping Index")

        cur = conn.cursor()

        cur.execute(
            """
            SELECT dt, value
            FROM index_raw
            WHERE index_name = 'SCFI'
              AND region = '종합'
            ORDER BY dt DESC
            LIMIT 2
            """
        )
        scfi_rows = cur.fetchall()

        if scfi_rows:
            current = safe_float(scfi_rows[0]["value"])
            previous = safe_float(scfi_rows[1]["value"]) if len(scfi_rows) > 1 else None

            data["scfi"]["current"] = fmt_number(current, 2)
            trend = trend_info(current, previous, 2)
            data["scfi"]["change"] = trend["delta"]
            data["scfi"]["dir"] = trend["dir"]

        cur.execute(
            """
            SELECT dt, value
            FROM index_raw
            WHERE index_name = 'BDI'
              AND region = 'BDI'
            ORDER BY dt DESC
            LIMIT 2
            """
        )
        bdi_rows = cur.fetchall()

        if bdi_rows:
            current = safe_float(bdi_rows[0]["value"])
            previous = safe_float(bdi_rows[1]["value"]) if len(bdi_rows) > 1 else None

            data["bdi"]["current"] = fmt_number(current, 0)
            trend = trend_info(current, previous, 0)
            data["bdi"]["change"] = trend["delta"]
            data["bdi"]["dir"] = trend["dir"]

        print(
            f"[INFO] Shipping Index loaded | "
            f"SCFI={data['scfi']['current']} | "
            f"BDI={data['bdi']['current']}"
        )

    except Exception as exc:
        print(f"[ERROR] load_shipping_index_data: {exc}")
    finally:
        conn.close()

    return data

def load_linerlytica_data():
    data = {"date": "-", "fleet": []}

    conn = get_db_connection(DB_PATHS["linerlytica"])
    if not conn:
        return data

    try:
        print_schema(conn, "Linerlytica")

        cur = conn.cursor()
        cur.execute("SELECT MAX(snapshot_date) AS latest_date FROM fleet_snapshot")
        latest_date = cur.fetchone()["latest_date"]

        if not latest_date:
            print("[WARNING] Linerlytica fleet_snapshot has no snapshot_date.")
            return data

        data["date"] = latest_date

        rows = cur.execute(
            """
            SELECT ship_status, ships, teu
            FROM fleet_snapshot
            WHERE snapshot_date = ?
            ORDER BY ships DESC
            """,
            (latest_date,),
        ).fetchall()

        for row in rows:
            data["fleet"].append(
                {
                    "status": str(row["ship_status"]),
                    "ships": fmt_number(row["ships"], 0),
                    "teu": fmt_number(row["teu"], 0),
                }
            )

        print(
            f"[INFO] Linerlytica loaded | "
            f"snapshot_date={latest_date} | rows={len(data['fleet'])}"
        )

    except Exception as exc:
        print(f"[ERROR] load_linerlytica_data: {exc}")
    finally:
        conn.close()

    return data

def load_alphaliner_data():
    data = {"date": "-", "top5": []}

    conn = get_db_connection(DB_PATHS["alphaliner"])
    if not conn:
        return data

    try:
        print_schema(conn, "Alphaliner")

        table_name = find_table(
            conn,
            preferred_names=[
                "top_carriers",
                "alphaliner_top_carriers",
                "carrier_ranking",
                "carriers",
            ],
            required_column_groups=[
                ["rank", "ranking", "rank_no", "rank_number"],
                ["carrier_name", "carrier", "company_name", "operator_name", "name"],
                ["total_teu", "teu", "capacity_teu", "fleet_teu"],
            ],
        )

        if not table_name:
            print("[ERROR] Alphaliner ranking table not found.")
            return data

        columns = get_columns(conn, table_name)

        snapshot_col = find_column(
            columns,
            ["snapshot_date", "data_date", "date", "snapshot_dt", "as_of_date"],
        )
        rank_col = find_column(columns, ["rank", "ranking", "rank_no", "rank_number"])
        carrier_col = find_column(
            columns,
            ["carrier_name", "carrier", "company_name", "operator_name", "name"],
        )
        teu_col = find_column(columns, ["total_teu", "teu", "capacity_teu", "fleet_teu"])
        share_col = find_column(
            columns,
            ["share_pct", "share", "market_share", "market_share_pct"],
        )

        if not all([rank_col, carrier_col, teu_col]):
            print(
                f"[ERROR] Alphaliner required columns unavailable | "
                f"table={table_name} | columns={list(columns.values())}"
            )
            return data

        cur = conn.cursor()
        where_clause = ""
        params = []

        if snapshot_col:
            latest_date_row = cur.execute(
                f"""
                SELECT MAX({q(snapshot_col)}) AS latest_date
                FROM {q(table_name)}
                """
            ).fetchone()

            latest_date = latest_date_row["latest_date"]

            if latest_date is not None:
                data["date"] = str(latest_date)
                where_clause = f"WHERE {q(snapshot_col)} = ?"
                params = [latest_date]

        share_select = f"{q(share_col)} AS share_value" if share_col else "NULL AS share_value"

        rows = cur.execute(
            f"""
            SELECT
                {q(rank_col)} AS rank_value,
                {q(carrier_col)} AS carrier_value,
                {q(teu_col)} AS teu_value,
                {share_select}
            FROM {q(table_name)}
            {where_clause}
            ORDER BY CAST({q(rank_col)} AS INTEGER) ASC
            LIMIT 5
            """,
            params,
        ).fetchall()

        for row in rows:
            share_value = safe_float(row["share_value"])

            data["top5"].append(
                {
                    "rank": row["rank_value"],
                    "carrier": str(row["carrier_value"]),
                    "teu": fmt_number(row["teu_value"], 0),
                    "share": fmt_pct(share_value, 1) if share_value is not None else "-",
                }
            )

        print(
            f"[INFO] Alphaliner loaded | "
            f"table={table_name} | date={data['date']} | rows={len(data['top5'])}"
        )

    except Exception as exc:
        print(f"[ERROR] load_alphaliner_data: {exc}")
    finally:
        conn.close()

    return data

def load_forwarder_data():
    data = {"date": "-", "top5": []}

    conn = get_db_connection(DB_PATHS["forwarder"])
    if not conn:
        return data

    try:
        print_schema(conn, "Forwarder")

        table_name = find_table(
            conn,
            preferred_names=[
                "forwarders_ranking",
                "freight_forwarders",
                "forwarder_ranking",
                "rankings",
            ],
            required_column_groups=[
                ["rank", "ranking", "rank_no", "rank_number"],
                ["company_name", "forwarder_name", "company", "name"],
            ],
        )

        if not table_name:
            print("[ERROR] Forwarder ranking table not found.")
            return data

        columns = get_columns(conn, table_name)

        year_col = find_column(
            columns,
            ["snapshot_year", "year", "data_year", "snapshot_date", "data_date"],
        )
        rank_col = find_column(columns, ["rank", "ranking", "rank_no", "rank_number"])
        company_col = find_column(
            columns,
            ["company_name", "forwarder_name", "company", "name"],
        )
        ocean_col = find_column(
            columns,
            ["ocean_teu", "sea_teu", "ocean_volume", "ocean_freight_teu"],
        )
        air_col = find_column(
            columns,
            ["air_metric_tons", "air_tons", "airfreight_tons", "air_volume"],
        )

        if not all([rank_col, company_col]):
            print(
                f"[ERROR] Forwarder required columns unavailable | "
                f"table={table_name} | columns={list(columns.values())}"
            )
            return data

        cur = conn.cursor()
        where_clause = ""
        params = []

        if year_col:
            latest_row = cur.execute(
                f"""
                SELECT MAX({q(year_col)}) AS latest_period
                FROM {q(table_name)}
                """
            ).fetchone()

            latest_period = latest_row["latest_period"]

            if latest_period is not None:
                data["date"] = str(latest_period)
                where_clause = f"WHERE {q(year_col)} = ?"
                params = [latest_period]

        ocean_select = f"{q(ocean_col)} AS ocean_value" if ocean_col else "NULL AS ocean_value"
        air_select = f"{q(air_col)} AS air_value" if air_col else "NULL AS air_value"

        rows = cur.execute(
            f"""
            SELECT
                {q(rank_col)} AS rank_value,
                {q(company_col)} AS company_value,
                {ocean_select},
                {air_select}
            FROM {q(table_name)}
            {where_clause}
            ORDER BY CAST({q(rank_col)} AS INTEGER) ASC
            LIMIT 5
            """,
            params,
        ).fetchall()

        for row in rows:
            data["top5"].append(
                {
                    "rank": row["rank_value"],
                    "company": str(row["company_value"]),
                    "ocean_teu": fmt_number(row["ocean_value"], 0),
                    "air_tons": fmt_number(row["air_value"], 0),
                }
            )

        print(
            f"[INFO] Forwarder loaded | "
            f"table={table_name} | date={data['date']} | rows={len(data['top5'])}"
        )

    except Exception as exc:
        print(f"[ERROR] load_forwarder_data: {exc}")
    finally:
        conn.close()

    return data

def load_tac_index_data():
    data = {"date": "-", "routes": []}

    conn = get_db_connection(DB_PATHS["tac"])
    if not conn:
        return data

    requested_routes = {
        normalize_route_name("Frankfurt - North America"): "Frankfurt - North America",
        normalize_route_name("Hong Kong - Europe"): "Hong Kong - Europe",
        normalize_route_name("Hong Kong - North America"): "Hong Kong - North America",
    }

    try:
        print_schema(conn, "TAC Index")

        master_table = find_table(
            conn,
            preferred_names=["tac_master"],
            required_column_groups=[
                ["data_date", "date", "snapshot_date"],
            ],
        )

        detail_table = find_table(
            conn,
            preferred_names=["tac_detail"],
            required_column_groups=[
                ["route_name", "route", "lane_name"],
                ["rate_usd", "rate", "price", "value"],
            ],
        )

        if not master_table or not detail_table:
            print(
                f"[ERROR] TAC required tables unavailable | "
                f"master={master_table} | detail={detail_table}"
            )
            return data

        master_cols = get_columns(conn, master_table)
        detail_cols = get_columns(conn, detail_table)

        master_date_col = find_column(master_cols, ["data_date", "date", "snapshot_date"])
        master_key_col = find_column(master_cols, ["snapshot_id", "id", "master_id"])
        detail_fk_col = find_column(detail_cols, ["snapshot_id", "master_id", "tac_master_id"])
        route_col = find_column(detail_cols, ["route_name", "route", "lane_name"])
        rate_col = find_column(detail_cols, ["rate_usd", "rate", "price", "value"])
        mom_col = find_column(detail_cols, ["change_mom", "mom_change", "change", "delta"])
        pct_col = find_column(detail_cols, ["change_pct", "mom_pct", "pct_change", "change_percent"])

        if not all([master_date_col, master_key_col, detail_fk_col, route_col, rate_col]):
            print(
                "[ERROR] TAC required columns unavailable | "
                f"master columns={list(master_cols.values())} | "
                f"detail columns={list(detail_cols.values())}"
            )
            return data

        cur = conn.cursor()

        # 동일 data_date의 중복 수집본이 있을 수 있으므로 가장 마지막 rowid의 스냅샷을 사용.
        latest_snapshot = cur.execute(
            f"""
            SELECT
                {q(master_key_col)} AS snapshot_key,
                {q(master_date_col)} AS data_date
            FROM {q(master_table)}
            WHERE {q(master_date_col)} IS NOT NULL
            ORDER BY {q(master_date_col)} DESC, rowid DESC
            LIMIT 1
            """
        ).fetchone()

        if not latest_snapshot:
            print("[WARNING] TAC master table has no valid snapshot.")
            return data

        snapshot_key = latest_snapshot["snapshot_key"]
        data["date"] = str(latest_snapshot["data_date"])

        mom_select = f"{q(mom_col)} AS mom_value" if mom_col else "NULL AS mom_value"
        pct_select = f"{q(pct_col)} AS pct_value" if pct_col else "NULL AS pct_value"

        rows = cur.execute(
            f"""
            SELECT
                {q(route_col)} AS route_value,
                {q(rate_col)} AS rate_value,
                {mom_select},
                {pct_select}
            FROM {q(detail_table)}
            WHERE {q(detail_fk_col)} = ?
            """,
            (snapshot_key,),
        ).fetchall()

        route_map = {}

        for row in rows:
            normalized = normalize_route_name(row["route_value"])

            if normalized not in requested_routes:
                continue

            rate = safe_float(row["rate_value"])
            mom = safe_float(row["mom_value"])
            pct = safe_float(row["pct_value"])

            if mom is not None:
                if mom > 0:
                    direction = "up"
                    arrow = "↗"
                    sign = "+"
                elif mom < 0:
                    direction = "down"
                    arrow = "↘"
                    sign = ""
                else:
                    direction = "neutral"
                    arrow = "→"
                    sign = ""

                pct_text = f"{sign}{pct:.1f}%" if pct is not None else "-"
                delta = f"{arrow} {sign}{mom:.2f} ({pct_text})"
            else:
                direction = "neutral"
                delta = "-"

            route_map[normalized] = {
                "name": requested_routes[normalized],
                "rate": f"${rate:.2f}/kg" if rate is not None else "-",
                "delta": delta,
                "dir": direction,
            }

        # 요청한 노선 순서 고정
        for normalized_route in requested_routes:
            if normalized_route in route_map:
                data["routes"].append(route_map[normalized_route])

        print(
            f"[INFO] TAC loaded | "
            f"date={data['date']} | snapshot_key={snapshot_key} | "
            f"rows={len(data['routes'])}"
        )

    except Exception as exc:
        print(f"[ERROR] load_tac_index_data: {exc}")
    finally:
        conn.close()

    return data

def load_bunker_and_crude_data():
    data = {
        "vlsfo": {"date": "-", "ports": []},
        "crude": {"date": "-", "benchmarks": []},
    }

    conn = get_db_connection(DB_PATHS["bunker"])
    if not conn:
        return data

    try:
        print_schema(conn, "BunkerPrice")

        cur = conn.cursor()

        # ----------------------------------------------------------------------
        # VLSFO
        # ----------------------------------------------------------------------
        vlsfo_table = find_table(
            conn,
            preferred_names=["product_vlsfo", "vlsfo_prices", "vlsfo"],
            required_column_groups=[
                ["date", "data_date", "price_date"],
                ["port", "location", "hub"],
                ["price", "value", "close"],
            ],
        )

        if not vlsfo_table:
            print("[ERROR] VLSFO table not found.")
        else:
            cols = get_columns(conn, vlsfo_table)

            date_col = find_column(cols, ["date", "data_date", "price_date"])
            port_col = find_column(cols, ["port", "location", "hub"])
            price_col = find_column(cols, ["price", "value", "close"])

            if not all([date_col, port_col, price_col]):
                print(
                    f"[ERROR] VLSFO required columns unavailable | "
                    f"table={vlsfo_table} | columns={list(cols.values())}"
                )
            else:
                latest_date_row = cur.execute(
                    f"""
                    SELECT MAX({q(date_col)}) AS latest_date
                    FROM {q(vlsfo_table)}
                    """
                ).fetchone()

                if latest_date_row and latest_date_row["latest_date"]:
                    data["vlsfo"]["date"] = str(latest_date_row["latest_date"])

                for port_name in ["Rotterdam", "Fujairah", "Singapore"]:
                    rows = cur.execute(
                        f"""
                        SELECT
                            {q(date_col)} AS price_date,
                            {q(price_col)} AS price_value
                        FROM {q(vlsfo_table)}
                        WHERE LOWER({q(port_col)}) = LOWER(?)
                        ORDER BY {q(date_col)} DESC
                        LIMIT 2
                        """,
                        (port_name,),
                    ).fetchall()

                    if not rows:
                        print(f"[WARNING] VLSFO missing port data: {port_name}")
                        continue

                    current = safe_float(rows[0]["price_value"])
                    previous = safe_float(rows[1]["price_value"]) if len(rows) > 1 else None
                    trend = trend_info(current, previous, 1)

                    data["vlsfo"]["ports"].append(
                        {
                            "port": port_name,
                            "price": f"${current:,.1f}" if current is not None else "-",
                            "delta": trend["delta"],
                            "dir": trend["dir"],
                        }
                    )

        # ----------------------------------------------------------------------
        # Crude Oil
        # ----------------------------------------------------------------------
        crude_table = find_table(
            conn,
            preferred_names=["oil_prices", "crude_prices", "oil_price"],
            required_column_groups=[
                ["date", "data_date"],
            ],
        )

        if not crude_table:
            print("[ERROR] Crude oil table not found.")
        else:
            cols = get_columns(conn, crude_table)

            date_col = find_column(cols, ["date", "data_date"])
            dubai_col = find_column(cols, ["dubai", "dubai_crude"])
            brent_col = find_column(cols, ["brent", "brent_crude"])
            wti_col = find_column(cols, ["wti", "wti_crude"])

            if not date_col:
                print(
                    f"[ERROR] Crude date column unavailable | "
                    f"table={crude_table} | columns={list(cols.values())}"
                )
            else:
                selected_columns = [f"{q(date_col)} AS price_date"]

                if dubai_col:
                    selected_columns.append(f"{q(dubai_col)} AS dubai_value")
                else:
                    selected_columns.append("NULL AS dubai_value")

                if brent_col:
                    selected_columns.append(f"{q(brent_col)} AS brent_value")
                else:
                    selected_columns.append("NULL AS brent_value")

                if wti_col:
                    selected_columns.append(f"{q(wti_col)} AS wti_value")
                else:
                    selected_columns.append("NULL AS wti_value")

                crude_rows = cur.execute(
                    f"""
                    SELECT {", ".join(selected_columns)}
                    FROM {q(crude_table)}
                    ORDER BY {q(date_col)} DESC
                    LIMIT 2
                    """
                ).fetchall()

                if crude_rows:
                    current_row = crude_rows[0]
                    previous_row = crude_rows[1] if len(crude_rows) > 1 else None

                    data["crude"]["date"] = str(current_row["price_date"])

                    benchmark_map = [
                        ("Dubai", "dubai_value"),
                        ("Brent", "brent_value"),
                        ("WTI", "wti_value"),
                    ]

                    for benchmark_name, field_name in benchmark_map:
                        current = safe_float(current_row[field_name])
                        previous = (
                            safe_float(previous_row[field_name])
                            if previous_row is not None
                            else None
                        )
                        trend = trend_info(current, previous, 2)

                        data["crude"]["benchmarks"].append(
                            {
                                "name": benchmark_name,
                                "price": f"${current:,.2f}" if current is not None else "-",
                                "delta": trend["delta"],
                                "dir": trend["dir"],
                            }
                        )

        print(
            f"[INFO] Bunker loaded | "
            f"VLSFO={len(data['vlsfo']['ports'])} rows | "
            f"Crude={len(data['crude']['benchmarks'])} rows"
        )

    except Exception as exc:
        print(f"[ERROR] load_bunker_and_crude_data: {exc}")
    finally:
        conn.close()

    return data

# ==============================================================================
# HTML Renderer
# ==============================================================================

def esc(value):
    return html.escape(str(value if value is not None else "-"))

def generate_portal_html(data: dict) -> str:
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    fleet_rows = ""
    for item in data["linerlytica"]["fleet"]:
        fleet_rows += f"""
        <tr>
            <td class="status-col">{esc(item["status"])}</td>
            <td class="num-col">{esc(item["ships"])}</td>
            <td class="num-col">{esc(item["teu"])}</td>
        </tr>
        """

    if not fleet_rows:
        fleet_rows = "<tr><td colspan='3' class='empty-row'>데이터가 없습니다.</td></tr>"

    alphaliner_rows = ""
    for item in data["alphaliner"]["top5"]:
        alphaliner_rows += f"""
        <tr>
            <td class="rank-col">{esc(item["rank"])}</td>
            <td class="carrier-col">{esc(item["carrier"])}</td>
            <td class="num-col">{esc(item["teu"])}</td>
            <td class="num-col highlight-orange">{esc(item["share"])}</td>
        </tr>
        """

    if not alphaliner_rows:
        alphaliner_rows = "<tr><td colspan='4' class='empty-row'>데이터가 없습니다.</td></tr>"

    forwarder_rows = ""
    for item in data["forwarder"]["top5"]:
        forwarder_rows += f"""
        <tr>
            <td class="rank-col">{esc(item["rank"])}</td>
            <td class="carrier-col">{esc(item["company"])}</td>
            <td class="num-col">{esc(item["ocean_teu"])}</td>
            <td class="num-col highlight-blue">{esc(item["air_tons"])}</td>
        </tr>
        """

    if not forwarder_rows:
        forwarder_rows = "<tr><td colspan='4' class='empty-row'>데이터가 없습니다.</td></tr>"

    tac_items = ""
    for route in data["tac"]["routes"]:
        tac_items += f"""
        <div class="kpi-metric-box">
            <div class="metric-label">{esc(route["name"])}</div>
            <div class="metric-val">{esc(route["rate"])}</div>
            <div class="metric-delta {esc(route["dir"])}">{esc(route["delta"])}</div>
        </div>
        """

    if not tac_items:
        tac_items = "<div class='empty-row'>TAC Air 운임 데이터를 조회할 수 없습니다.</div>"

    vlsfo_items = ""
    for port in data["bunker"]["vlsfo"]["ports"]:
        vlsfo_items += f"""
        <div class="kpi-metric-box">
            <div class="metric-label">{esc(port["port"])} VLSFO</div>
            <div class="metric-val">{esc(port["price"])}</div>
            <div class="metric-delta {esc(port["dir"])}">{esc(port["delta"])}</div>
        </div>
        """

    if not vlsfo_items:
        vlsfo_items = "<div class='empty-row'>VLSFO 데이터를 조회할 수 없습니다.</div>"

    crude_items = ""
    for crude in data["bunker"]["crude"]["benchmarks"]:
        crude_items += f"""
        <div class="kpi-metric-box">
            <div class="metric-label">{esc(crude["name"])} Crude</div>
            <div class="metric-val">{esc(crude["price"])}</div>
            <div class="metric-delta {esc(crude["dir"])}">{esc(crude["delta"])}</div>
        </div>
        """

    if not crude_items:
        crude_items = "<div class='empty-row'>원유 데이터를 조회할 수 없습니다.</div>"

    return f"""<!DOCTYPE html>
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
            --font-mono: Consolas, "Courier New", monospace;
            --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        }}

        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}

        body {{
            background: var(--bg);
            color: var(--text-primary);
            font-family: var(--font-sans);
            padding: 20px;
            font-size: 13px;
        }}

        .terminal-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 12px 20px;
            background: var(--panel);
            border: 1px solid var(--panel-border);
            border-left: 4px solid var(--orange);
            margin-bottom: 20px;
        }}

        .brand-title {{
            color: var(--orange);
            font: bold 16px var(--font-mono);
            letter-spacing: 1px;
        }}

        .header-meta {{
            color: var(--text-secondary);
            font: 12px var(--font-mono);
        }}

        .section-title {{
            color: var(--orange);
            font: bold 14px var(--font-mono);
            letter-spacing: 1px;
            margin-bottom: 12px;
            display: flex;
            align-items: center;
            gap: 8px;
        }}

        .section-title::after {{
            content: "";
            flex: 1;
            height: 1px;
            background: var(--panel-border);
        }}

        .grid-ocean {{
            display: grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            gap: 16px;
            margin-bottom: 24px;
        }}

        .grid-air-macro {{
            display: grid;
            grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
            gap: 16px;
            margin-bottom: 24px;
        }}

        .grid-inner-macro {{
            display: grid;
            grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
            gap: 12px;
        }}

        .card {{
            background: var(--panel);
            border: 1px solid var(--panel-border);
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }}

        .card-header {{
            background: var(--panel-header);
            padding: 10px 14px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--panel-border);
        }}

        .card-title {{
            font: bold 12px var(--font-mono);
            color: var(--text-primary);
        }}

        .card-link {{
            color: var(--orange);
            text-decoration: none;
            font: 11px var(--font-mono);
        }}

        .card-link:hover {{
            text-decoration: underline;
        }}

        .card-body {{
            padding: 14px;
            flex: 1;
        }}

        table {{
            width: 100%;
            border-collapse: collapse;
            font: 12px var(--font-mono);
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
            border-bottom: 1px solid rgba(19, 34, 56, .4);
            color: var(--text-primary);
        }}

        .rank-col {{
            width: 28px;
            color: var(--orange);
        }}

        .status-col {{
            color: var(--text-secondary);
        }}

        .carrier-col {{
            font-weight: 500;
        }}

        .num-col {{
            text-align: right;
        }}

        .highlight-orange {{
            color: var(--orange);
            font-weight: bold;
        }}

        .highlight-blue {{
            color: var(--blue);
            font-weight: bold;
        }}

        .kpi-row {{
            display: flex;
            gap: 12px;
        }}

        .kpi-box {{
            flex: 1;
            background: rgba(12, 23, 38, .5);
            padding: 12px;
            border: 1px solid var(--panel-border);
            text-align: center;
        }}

        .kpi-label {{
            color: var(--text-secondary);
            font: 11px var(--font-mono);
            margin-bottom: 6px;
        }}

        .kpi-value {{
            color: #fff;
            font: bold 20px var(--font-mono);
            margin-bottom: 4px;
        }}

        .kpi-metric-box {{
            display: grid;
            grid-template-columns: minmax(0, 1fr) auto auto;
            gap: 10px;
            align-items: center;
            background: rgba(12, 23, 38, .5);
            border: 1px solid var(--panel-border);
            padding: 10px 12px;
            margin-bottom: 8px;
        }}

        .kpi-metric-box:last-child {{
            margin-bottom: 0;
        }}

        .metric-label {{
            color: var(--text-secondary);
            font: 11px var(--font-mono);
        }}

        .metric-val {{
            color: #fff;
            font: bold 14px var(--font-mono);
            white-space: nowrap;
        }}

        .metric-delta {{
            font: bold 11px var(--font-mono);
            white-space: nowrap;
        }}

        .up {{
            color: var(--green);
        }}

        .down {{
            color: var(--red);
        }}

        .neutral {{
            color: var(--text-muted);
        }}

        .empty-row {{
            text-align: center;
            color: var(--text-muted);
            padding: 16px 0;
            font: 11px var(--font-mono);
        }}

        .terminal-footer {{
            text-align: center;
            color: var(--text-muted);
            font: 11px var(--font-mono);
            margin-top: 30px;
            padding-top: 15px;
            border-top: 1px solid var(--panel-border);
        }}

        @media (max-width: 1200px) {{
            .grid-ocean {{
                grid-template-columns: repeat(2, minmax(0, 1fr));
            }}

            .grid-air-macro {{
                grid-template-columns: 1fr;
            }}
        }}

        @media (max-width: 768px) {{
            body {{
                padding: 12px;
            }}

            .terminal-header {{
                align-items: flex-start;
                flex-direction: column;
                gap: 6px;
            }}

            .grid-ocean,
            .grid-inner-macro {{
                grid-template-columns: 1fr;
            }}

            .kpi-metric-box {{
                grid-template-columns: 1fr auto;
            }}

            .metric-delta {{
                grid-column: 2;
            }}
        }}
    </style>
</head>
<body>

    <div class="terminal-header">
        <div class="brand-title">MI&lt;GO&gt; | MARKET INTELLIGENCE PORTAL</div>
        <div class="header-meta">SYS.TIME: {esc(now_str)} KST | TERMINAL ACTIVE</div>
    </div>

    <div class="section-title">01. OCEAN INTELLIGENCE</div>

    <div class="grid-ocean">
        <div class="card">
            <div class="card-header">
                <span class="card-title">SHIPPING INDEX</span>
                <a href="Shipping Index/index.html" class="card-link">[DETAIL]</a>
            </div>
            <div class="card-body">
                <div class="kpi-row">
                    <div class="kpi-box">
                        <div class="kpi-label">SCFI COMPOSITE</div>
                        <div class="kpi-value">{esc(data["shipping"]["scfi"]["current"])}</div>
                        <div class="metric-delta {esc(data["shipping"]["scfi"]["dir"])}">
                            {esc(data["shipping"]["scfi"]["change"])}
                        </div>
                    </div>
                    <div class="kpi-box">
                        <div class="kpi-label">BDI COMPOSITE</div>
                        <div class="kpi-value">{esc(data["shipping"]["bdi"]["current"])}</div>
                        <div class="metric-delta {esc(data["shipping"]["bdi"]["dir"])}">
                            {esc(data["shipping"]["bdi"]["change"])}
                        </div>
                    </div>
                </div>
            </div>
        </div>

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

    <div class="grid-air-macro">
        <div class="card">
            <div class="card-header">
                <span class="card-title" style="color: var(--orange);">02. AIR FREIGHT (TAC INDEX)</span>
                <a href="TAC Index/index.html" class="card-link">[DETAIL]</a>
            </div>
            <div class="card-body">
                {tac_items}
            </div>
        </div>

        <div class="card">
            <div class="card-header">
                <span class="card-title" style="color: var(--orange);">03. MACRO &amp; ENERGY</span>
                <a href="BunkerPrice/index.html" class="card-link">[DETAIL]</a>
            </div>
            <div class="card-body">
                <div class="grid-inner-macro">
                    <div>
                        <div class="kpi-label" style="text-align:left; font-weight:bold; color:var(--text-primary);">
                            VLSFO BENCHMARKS
                        </div>
                        {vlsfo_items}
                    </div>
                    <div>
                        <div class="kpi-label" style="text-align:left; font-weight:bold; color:var(--text-primary);">
                            CRUDE BENCHMARKS
                        </div>
                        {crude_items}
                    </div>
                </div>
            </div>
        </div>
    </div>

    <div class="terminal-footer">
        MI TERMINAL PLATFORM &bull; AUTOMATED INTELLIGENCE AGGREGATOR &bull;
        &copy; {datetime.datetime.now().year}
    </div>

</body>
</html>
"""

# ==============================================================================
# Main Orchestrator
# ==============================================================================

def main():
    print("=" * 72)
    print("Generating MI Portal Dashboard & Summary...")
    print("=" * 72)

    dashboard_data = {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "shipping": load_shipping_index_data(),
        "linerlytica": load_linerlytica_data(),
        "alphaliner": load_alphaliner_data(),
        "forwarder": load_forwarder_data(),
        "tac": load_tac_index_data(),
        "bunker": load_bunker_and_crude_data(),
    }

    try:
        with open(OUTPUT_JSON, "w", encoding="utf-8") as file:
            json.dump(dashboard_data, file, ensure_ascii=False, indent=2)

        print(f"[SUCCESS] Generated JSON: {OUTPUT_JSON}")

    except Exception as exc:
        print(f"[ERROR] Failed to write JSON: {exc}")

    try:
        html_rendered = generate_portal_html(dashboard_data)

        with open(OUTPUT_HTML, "w", encoding="utf-8") as file:
            file.write(html_rendered)

        print(f"[SUCCESS] Generated Dashboard: {OUTPUT_HTML}")

    except Exception as exc:
        print(f"[ERROR] Failed to write HTML: {exc}")
        sys.exit(1)

    print("=" * 72)
    print("MI Portal Dashboard Build Completed Successfully.")
    print("=" * 72)

if __name__ == "__main__":
    main()
