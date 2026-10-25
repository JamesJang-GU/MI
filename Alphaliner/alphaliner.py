# -*- coding: utf-8 -*-
"""
Alphaliner TOP 100 Liner Operators Static Dashboard Crawler (GitHub Optimized)
- Direct ExtJS Store extraction (Top100description) + Frame Traversal
- Official Global Figures + TOP 100 Capacity Separation
- Historical snapshot comparison & Full Time-travel Delta analysis
- Authentic Alphaliner 2-tier Grouped Table & UTF-8 BOM CSV Export
- Bloomberg MI Terminal Look & Feel (Aligned with MI Standards)

Output files:
- alphaliner_history.db
- alphaliner_data.json
- index.html
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from threading import Lock

import pandas as pd
from flask import Flask, Response, send_from_directory
from selenium import webdriver
from selenium.common.exceptions import WebDriverException, TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By

try:
    from webdriver_manager.chrome import ChromeDriverManager
    HAS_WEBDRIVER_MANAGER = True
except Exception:
    HAS_WEBDRIVER_MANAGER = False

# =========================================================
# 1. Basic Configuration
# =========================================================
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "alphaliner_history.db"
JSON_PATH = BASE_DIR / "alphaliner_data.json"
INDEX_HTML_PATH = BASE_DIR / "index.html"

SOURCE_URL = "https://infographics.alphaliner.com/infographics/top-100-liner-operators"
APP_TITLE = "Alphaliner TOP 100 Liner Operators"

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S"
)

db_lock = Lock()
app = Flask(__name__)

# =========================================================
# 2. Presentation Layer: Terminal HTML & Client Engine
# =========================================================
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Alphaliner TOP 100 Liner Operators | Bloomberg MI Terminal</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
:root {
  --bg-primary:#02060b;
  --bg-secondary:#07111c;
  --bg-tertiary:#0d1d2b;
  --border-subtle:#1b3043;
  --text-main:#f3f7fb;
  --text-muted:#879caf;
  --text-dim:#536c80;
  --accent-orange:#f6a623;
  --accent-blue:#45a9f5;
  --accent-green:#36ca82;
  --accent-red:#ff6262;
  --accent-purple:#bb86fc;
}

* {
  box-sizing:border-box;
  margin:0;
  padding:0;
}

body {
  background:var(--bg-primary);
  color:var(--text-main);
  font-family:Arial, sans-serif;
  font-size:13px;
}

.terminal-header {
  display:flex;
  justify-content:space-between;
  align-items:center;
  gap:12px;
  padding:8px 16px;
  background:var(--bg-secondary);
  border-bottom:1px solid var(--border-subtle);
}

.brand-tag {
  display:inline-block;
  padding:2px 6px;
  margin-right:12px;
  font-weight:700;
  font-size:11px;
  color:#000;
  background:var(--accent-orange);
}

.brand-title {
  font-weight:600;
  text-transform:uppercase;
}

.terminal-controls {
  display:flex;
  align-items:center;
  gap:10px;
}

select,
input,
button {
  padding:5px 8px;
  color:var(--text-main);
  background:var(--bg-tertiary);
  border:1px solid var(--border-subtle);
}

button {
  cursor:pointer;
  color:var(--accent-orange);
}

.container {
  display:grid;
  grid-template-columns:repeat(12, 1fr);
  gap:12px;
  max-width:1920px;
  padding:16px;
  margin:0 auto;
}

.panel {
  background:var(--bg-secondary);
  border:1px solid var(--border-subtle);
}

.panel-header,
.table-toolbar {
  display:flex;
  justify-content:space-between;
  padding:8px 12px;
  border-bottom:1px solid var(--border-subtle);
  font-size:11px;
  font-weight:700;
}

.panel-body {
  padding:12px;
}

.col-kpi {
  grid-column:span 3;
}

.col-chart {
  grid-column:span 12;
  height:280px;
}

.col-table {
  grid-column:span 12;
}

.kpi-title {
  color:var(--text-dim);
  font-size:11px;
}

.kpi-value {
  margin:5px 0;
  font-size:22px;
  font-weight:700;
}

.kpi-sub {
  min-height:16px;
  color:var(--text-muted);
  font-size:11px;
}

.delta-pos {
  color:var(--accent-green);
}

.delta-neg {
  color:var(--accent-red);
}

.delta-flat {
  color:var(--text-muted);
}

.delta-base {
  color:var(--text-dim);
}

.table-container {
  overflow:auto;
  max-height:600px;
}

table {
  width:100%;
  border-collapse:collapse;
  font-family:monospace;
  font-size:12px;
}

th,
td {
  padding:6px 10px;
  text-align:right;
  white-space:nowrap;
  border-bottom:1px solid var(--border-subtle);
}

th {
  position:sticky;
  top:0;
  z-index:2;
  color:var(--text-muted);
  background:var(--bg-tertiary);
}

.align-left {
  text-align:left;
}

.align-center {
  text-align:center;
}

.border-left {
  border-left:1px solid var(--border-subtle);
}

.group-header {
  text-align:center;
}

.rank-top10 {
  color:var(--accent-orange);
  font-weight:700;
}

.footer-status {
  display:flex;
  justify-content:space-between;
  padding:12px 16px;
  color:var(--text-dim);
  background:var(--bg-secondary);
  border-top:1px solid var(--border-subtle);
  font-size:11px;
}

@media (max-width:1000px) {
  .col-kpi {
    grid-column:span 6;
  }
}

@media (max-width:640px) {
  .terminal-header,
  .terminal-controls,
  .footer-status {
    flex-direction:column;
    align-items:flex-start;
  }

  .col-kpi {
    grid-column:span 12;
  }
}
</style>
</head>

<body>
<header class="terminal-header">
  <div>
    <span class="brand-tag">ALPHALINER</span>
    <span class="brand-title">TOP 100 Global Liner Operators Terminal</span>
  </div>

  <div class="terminal-controls">
    <label>
      SNAPSHOT:
      <select id="dateSelect" onchange="renderAll()"></select>
    </label>

    <label>
      COMPARE TO:
      <select id="compareSelect" onchange="renderAll()"></select>
    </label>

    <button onclick="exportCSV()">EXPORT CSV</button>
  </div>
</header>

<main class="container">
  <section class="panel col-kpi">
    <div class="panel-header">GLOBAL FLEET COUNT</div>
    <div class="panel-body">
      <div class="kpi-title">TOTAL SHIPS</div>
      <div class="kpi-value" id="kpiGlobalShips">--</div>
      <div class="kpi-sub" id="deltaGlobalShips">BASE SNAPSHOT</div>
    </div>
  </section>

  <section class="panel col-kpi">
    <div class="panel-header">GLOBAL CAPACITY</div>
    <div class="panel-body">
      <div class="kpi-title">TOTAL FLEET TEU</div>
      <div class="kpi-value" id="kpiGlobalTeu">--</div>
      <div class="kpi-sub" id="deltaGlobalTeu">BASE SNAPSHOT</div>
    </div>
  </section>

  <section class="panel col-kpi">
    <div class="panel-header">CONCENTRATION</div>
    <div class="panel-body">
      <div class="kpi-title">TOP 10 MARKET SHARE</div>
      <div class="kpi-value" id="kpiTop10Share">--</div>
      <div class="kpi-sub" id="deltaTop10Share">BASE SNAPSHOT</div>
    </div>
  </section>

  <section class="panel col-kpi">
    <div class="panel-header">ORDERBOOK</div>
    <div class="panel-body">
      <div class="kpi-title">PIPELINE CAPACITY</div>
      <div class="kpi-value" id="kpiOrderbookTeu">--</div>
      <div class="kpi-sub" id="deltaOrderbookTeu">BASE SNAPSHOT</div>
    </div>
  </section>

  <section class="panel col-chart">
    <div class="panel-header">
      <span>TOP 10 OPERATOR CAPACITY PROFILE (TEU)</span>
      <span id="chartSubTitle">--</span>
    </div>

    <div class="panel-body" style="height:240px;">
      <canvas id="topChart"></canvas>
    </div>
  </section>

  <section class="panel col-table">
    <div class="table-toolbar">
      <span>TOP 100 DETAILED FLEET BREAKDOWN</span>
      <input
        id="filterInput"
        type="text"
        placeholder="Filter operator..."
        onkeyup="filterTable()"
      >
    </div>

    <div class="table-container">
      <table>
        <thead>
          <tr>
            <th rowspan="2" class="align-center">#</th>
            <th rowspan="2" class="align-left">OPERATOR</th>
            <th colspan="3" class="group-header">TOTAL CAPACITY</th>
            <th colspan="3" class="group-header">OWNED FLEET</th>
            <th colspan="3" class="group-header">CHARTERED FLEET</th>
            <th colspan="3" class="group-header">ORDERBOOK</th>
          </tr>
          <tr>
            <th>TEU</th>
            <th>SHIPS</th>
            <th>SHARE</th>
            <th class="border-left">TEU</th>
            <th>SHIPS</th>
            <th>% OWN</th>
            <th class="border-left">TEU</th>
            <th>SHIPS</th>
            <th>% CHT</th>
            <th class="border-left">TEU</th>
            <th>SHIPS</th>
            <th>% FLEET</th>
          </tr>
        </thead>
        <tbody id="tableBody"></tbody>
      </table>
    </div>
  </section>
</main>

<footer class="footer-status">
  <div>
    SYSTEM STATUS:
    <span style="color:var(--accent-green);">ONLINE</span>
    | SOURCE: ALPHALINER PUBLIC TOP 100
  </div>
  <div id="collectedTimestamp">DATA COLLECTED: --</div>
</footer>

<script>
let DATA_STORE = { dates: [], snapshots: {} };
let currentChart = null;

function fmt(value) {
  if (value === null || value === undefined || isNaN(value)) {
    return "--";
  }
  return Number(value).toLocaleString();
}

function numericValue(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function renderQuantityDelta(elementId, currentValue, previousValue, unit, compareDate) {
  const element = document.getElementById(elementId);
  if (!element) return;

  if (previousValue === null || previousValue === undefined || compareDate === "none" || !compareDate) {
    element.className = "kpi-sub delta-base";
    element.textContent = "BASE SNAPSHOT";
    return;
  }

  const current = numericValue(currentValue);
  const previous = numericValue(previousValue);
  const difference = current - previous;

  if (difference === 0) {
    element.className = "kpi-sub delta-flat";
    element.textContent = "— NO CHANGE vs " + compareDate;
    return;
  }

  const direction = difference > 0 ? "▲" : "▼";
  const sign = difference > 0 ? "+" : "";
  const colorClass = difference > 0 ? "delta-pos" : "delta-neg";

  const percentChange = previous !== 0 ? (difference / previous) * 100 : null;
  const percentText = percentChange === null ? "N/A" : sign + percentChange.toFixed(2) + "%";

  element.className = "kpi-sub " + colorClass;
  element.textContent = direction + " " + sign + fmt(difference) + " " + unit + " (" + percentText + ") vs " + compareDate;
}

function renderPercentagePointDelta(elementId, currentValue, previousValue, compareDate) {
  const element = document.getElementById(elementId);
  if (!element) return;

  if (previousValue === null || previousValue === undefined || compareDate === "none" || !compareDate) {
    element.className = "kpi-sub delta-base";
    element.textContent = "BASE SNAPSHOT";
    return;
  }

  const current = numericValue(currentValue);
  const previous = numericValue(previousValue);
  const difference = current - previous;

  if (difference === 0) {
    element.className = "kpi-sub delta-flat";
    element.textContent = "— NO CHANGE vs " + compareDate;
    return;
  }

  const direction = difference > 0 ? "▲" : "▼";
  const sign = difference > 0 ? "+" : "";
  const colorClass = difference > 0 ? "delta-pos" : "delta-neg";

  element.className = "kpi-sub " + colorClass;
  element.textContent = direction + " " + sign + difference.toFixed(2) + "%p vs " + compareDate;
}

async function initDashboard() {
  const response = await fetch("alphaliner_data.json");
  if (!response.ok) throw new Error("Failed to load alphaliner_data.json");

  DATA_STORE = await response.json();
  if (!DATA_STORE.dates || DATA_STORE.dates.length === 0) return;

  const dateSelect = document.getElementById("dateSelect");
  const compareSelect = document.getElementById("compareSelect");

  DATA_STORE.dates.forEach((date, index) => {
    const dateOption = document.createElement("option");
    dateOption.value = date;
    dateOption.textContent = date + (index === 0 ? " (Latest)" : "");
    dateSelect.appendChild(dateOption);

    const compareOption = document.createElement("option");
    compareOption.value = date;
    compareOption.textContent = date;
    compareSelect.appendChild(compareOption);
  });

  const noneOption = document.createElement("option");
  noneOption.value = "none";
  noneOption.textContent = "None";
  compareSelect.insertBefore(noneOption, compareSelect.firstChild);

  if (DATA_STORE.dates.length > 1) {
    compareSelect.value = DATA_STORE.dates[1];
  } else {
    compareSelect.value = "none";
  }

  renderAll();
}

function renderAll() {
  const activeDate = document.getElementById("dateSelect").value;
  const compareDate = document.getElementById("compareSelect").value;

  const snapshot = DATA_STORE.snapshots[activeDate];
  const compareSnapshot = compareDate !== "none" ? DATA_STORE.snapshots[compareDate] : null;

  if (!snapshot) return;

  const meta = snapshot.meta;
  const previousMeta = compareSnapshot ? compareSnapshot.meta : null;

  document.getElementById("kpiGlobalShips").textContent = fmt(meta.global_total_ships) + " Ships";
  renderQuantityDelta("deltaGlobalShips", meta.global_total_ships, previousMeta ? previousMeta.global_total_ships : null, "Ships", compareDate);

  document.getElementById("kpiGlobalTeu").textContent = fmt(meta.global_total_teu) + " TEU";
  renderQuantityDelta("deltaGlobalTeu", meta.global_total_teu, previousMeta ? previousMeta.global_total_teu : null, "TEU", compareDate);

  document.getElementById("kpiTop10Share").textContent = numericValue(meta.top10_share).toFixed(1) + "%";
  renderPercentagePointDelta("deltaTop10Share", meta.top10_share, previousMeta ? previousMeta.top10_share : null, compareDate);

  document.getElementById("kpiOrderbookTeu").textContent = fmt(meta.orderbook_total_teu) + " TEU";
  renderQuantityDelta("deltaOrderbookTeu", meta.orderbook_total_teu, previousMeta ? previousMeta.orderbook_total_teu : null, "TEU", compareDate);

  document.getElementById("collectedTimestamp").textContent = "DATA COLLECTED: " + (meta.collected_at || activeDate);

  renderChart(snapshot);
  renderTable(snapshot, compareSnapshot);
}

function renderChart(snapshot) {
  const top10 = snapshot.operators.slice(0, 10);
  if (currentChart) currentChart.destroy();

  currentChart = new Chart(
    document.getElementById("topChart"),
    {
      type: "bar",
      data: {
        labels: top10.map(op => op.rank + ". " + op.operator),
        datasets: [
          { label: "Owned TEU", data: top10.map(op => op.owned_teu), backgroundColor: "#45a9f5", stack: "active" },
          { label: "Chartered TEU", data: top10.map(op => op.chartered_teu), backgroundColor: "#f6a623", stack: "active" },
          { label: "Orderbook TEU", data: top10.map(op => op.orderbook_teu), backgroundColor: "rgba(187,134,252,.45)", borderColor: "#bb86fc", borderWidth: 1, stack: "orderbook" }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { labels: { color: "#879caf" } } },
        scales: {
          x: { stacked: true, ticks: { color: "#879caf" }, grid: { color: "rgba(27,48,67,.5)" } },
          y: { stacked: true, ticks: { color: "#879caf", callback: value => (value / 1000000).toFixed(1) + "M" }, grid: { color: "rgba(27,48,67,.5)" } }
        }
      }
    }
  );

  document.getElementById("chartSubTitle").textContent = "AS OF " + snapshot.meta.snapshot_date;
}

function renderTable(snapshot, compareSnapshot) {
  const body = document.getElementById("tableBody");
  body.innerHTML = "";

  const compareMap = {};
  if (compareSnapshot && compareSnapshot.operators) {
    compareSnapshot.operators.forEach(op => {
      compareMap[op.operator.toLowerCase()] = op;
    });
  }

  snapshot.operators.forEach(op => {
    const row = document.createElement("tr");
    const previous = compareMap[op.operator.toLowerCase()];
    let rankDelta = "";

    if (previous && previous.rank !== op.rank) {
      const diff = previous.rank - op.rank;
      const cssClass = diff > 0 ? "delta-pos" : "delta-neg";
      const arrow = diff > 0 ? "▲" : "▼";
      rankDelta = `<span class="${cssClass}"> ${arrow}${Math.abs(diff)}</span>`;
    }

    row.innerHTML = `
      <td class="align-center">
        <span class="${op.rank <= 10 ? "rank-top10" : ""}">${op.rank}</span>
        ${rankDelta}
      </td>
      <td class="align-left"><strong>${op.operator}</strong></td>
      <td><strong>${fmt(op.total_teu)}</strong></td>
      <td>${fmt(op.total_ships)}</td>
      <td>${Number(op.market_share || 0).toFixed(1)}%</td>
      <td class="border-left">${fmt(op.owned_teu)}</td>
      <td>${fmt(op.owned_ships)}</td>
      <td>${Number(op.owned_share || 0).toFixed(1)}%</td>
      <td class="border-left">${fmt(op.chartered_teu)}</td>
      <td>${fmt(op.chartered_ships)}</td>
      <td>${Number(op.chartered_share || 0).toFixed(1)}%</td>
      <td class="border-left">${fmt(op.orderbook_teu)}</td>
      <td>${fmt(op.orderbook_ships)}</td>
      <td>${Number(op.orderbook_share || 0).toFixed(1)}%</td>
    `;
    body.appendChild(row);
  });
}

function filterTable() {
  const query = document.getElementById("filterInput").value.toLowerCase();
  document.querySelectorAll("#tableBody tr").forEach(row => {
    const operator = row.cells[1].innerText.toLowerCase();
    row.style.display = operator.includes(query) ? "" : "none";
  });
}

function exportCSV() {
  const activeDate = document.getElementById("dateSelect").value;
  const snapshot = DATA_STORE.snapshots[activeDate];
  if (!snapshot) return;

  let csv = "\uFEFFRank,Operator,Total TEU,Total Ships,Market Share %,Owned TEU,Owned Ships,Owned Share %,Chartered TEU,Chartered Ships,Chartered Share %,Orderbook TEU,Orderbook Ships,Orderbook % of Fleet\n";
  snapshot.operators.forEach(op => {
    const escapedName = String(op.operator).replace(/"/g, '""');
    csv += [
      op.rank,
      `"${escapedName}"`,
      op.total_teu,
      op.total_ships,
      op.market_share,
      op.owned_teu,
      op.owned_ships,
      op.owned_share,
      op.chartered_teu,
      op.chartered_ships,
      op.chartered_share,
      op.orderbook_teu,
      op.orderbook_ships,
      op.orderbook_share
    ].join(",") + "\n";
  });

  const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = "Alphaliner_TOP100_" + activeDate + ".csv";
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
}

document.addEventListener("DOMContentLoaded", () => {
  initDashboard().catch(error => {
    console.error("Dashboard initialization failed:", error);
  });
});
</script>
</body>
</html>
"""

# =========================================================
# 3. Database Layer (SQLite with Auto Migration)
# =========================================================
def init_db():
    with db_lock:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS snapshot_meta (
                    snapshot_date TEXT PRIMARY KEY,
                    collected_at TEXT NOT NULL,
                    global_total_teu INTEGER NOT NULL,
                    global_total_ships INTEGER NOT NULL,
                    top100_total_teu INTEGER NOT NULL,
                    top100_total_ships INTEGER NOT NULL,
                    top10_share REAL NOT NULL,
                    orderbook_total_teu INTEGER NOT NULL,
                    source_url TEXT,
                    raw_hash TEXT
                )
            """)

            # Dynamic migration for existing SQLite files
            cur.execute("PRAGMA table_info(snapshot_meta)")
            existing_cols = {row[1] for row in cur.fetchall()}

            if "source_url" not in existing_cols:
                cur.execute("ALTER TABLE snapshot_meta ADD COLUMN source_url TEXT DEFAULT ''")
                logging.info("[DB Migration] Added missing column 'source_url' to snapshot_meta")

            if "raw_hash" not in existing_cols:
                cur.execute("ALTER TABLE snapshot_meta ADD COLUMN raw_hash TEXT DEFAULT ''")
                logging.info("[DB Migration] Added missing column 'raw_hash' to snapshot_meta")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS operator_snapshots (
                    snapshot_date TEXT NOT NULL,
                    rank INTEGER NOT NULL,
                    operator TEXT NOT NULL,
                    total_teu INTEGER NOT NULL,
                    total_ships INTEGER NOT NULL,
                    market_share REAL NOT NULL,
                    owned_teu INTEGER NOT NULL,
                    owned_ships INTEGER NOT NULL,
                    owned_share REAL NOT NULL,
                    chartered_teu INTEGER NOT NULL,
                    chartered_ships INTEGER NOT NULL,
                    chartered_share REAL NOT NULL,
                    orderbook_teu INTEGER NOT NULL,
                    orderbook_ships INTEGER NOT NULL,
                    orderbook_share REAL NOT NULL,
                    PRIMARY KEY (snapshot_date, rank),
                    FOREIGN KEY (snapshot_date) REFERENCES snapshot_meta(snapshot_date)
                )
            """)
            conn.commit()

def save_snapshot_to_db(meta: dict, operators: list):
    with db_lock:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT OR REPLACE INTO snapshot_meta (
                    snapshot_date, collected_at, global_total_teu, global_total_ships,
                    top100_total_teu, top100_total_ships, top10_share, orderbook_total_teu,
                    source_url, raw_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                meta["snapshot_date"], meta["collected_at"], meta["global_total_teu"],
                meta["global_total_ships"], meta["top100_total_teu"], meta["top100_total_ships"],
                meta["top10_share"], meta["orderbook_total_teu"], meta.get("source_url", ""),
                meta.get("raw_hash", "")
            ))

            cur.execute("DELETE FROM operator_snapshots WHERE snapshot_date = ?", (meta["snapshot_date"],))

            rows = [
                (
                    meta["snapshot_date"], op["rank"], op["operator"], op["total_teu"],
                    op["total_ships"], op["market_share"], op["owned_teu"], op["owned_ships"],
                    op["owned_share"], op["chartered_teu"], op["chartered_ships"], op["chartered_share"],
                    op["orderbook_teu"], op["orderbook_ships"], op["orderbook_share"]
                )
                for op in operators
            ]

            cur.executemany("""
                INSERT INTO operator_snapshots (
                    snapshot_date, rank, operator, total_teu, total_ships, market_share,
                    owned_teu, owned_ships, owned_share, chartered_teu, chartered_ships,
                    chartered_share, orderbook_teu, orderbook_ships, orderbook_share
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)
            conn.commit()
    logging.info("[DB] Saved snapshot %s (%d operators)", meta["snapshot_date"], len(operators))

# =========================================================
# 4. JSON Serialization Layer (For Static Terminal Build)
# =========================================================
def build_and_save_json():
    with db_lock:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            cur.execute("SELECT * FROM snapshot_meta ORDER BY snapshot_date DESC")
            meta_rows = cur.fetchall()

            if not meta_rows:
                logging.warning("[JSON] No data found in snapshot_meta.")
                return

            dates = [row["snapshot_date"] for row in meta_rows]
            snapshots = {}

            for m in meta_rows:
                s_date = m["snapshot_date"]
                cur.execute(
                    "SELECT * FROM operator_snapshots WHERE snapshot_date = ? ORDER BY rank ASC",
                    (s_date,)
                )
                op_rows = cur.fetchall()

                snapshots[s_date] = {
                    "meta": dict(m),
                    "operators": [dict(r) for r in op_rows]
                }

    export_payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "dates": dates,
        "snapshots": snapshots
    }

    JSON_PATH.write_text(json.dumps(export_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info("[JSON] Serialized %d snapshot(s) -> %s", len(dates), JSON_PATH)

# =========================================================
# 5. Static HTML File Output
# =========================================================
def generate_static_dashboard():
    INDEX_HTML_PATH.write_text(HTML_TEMPLATE, encoding="utf-8")
    logging.info("[HTML] Static Dashboard generated: %s", INDEX_HTML_PATH)

# =========================================================
# 6. Extraction Engine (Selenium + ExtJS Stores + Fallback)
# =========================================================
def normalize_key(value):
    if value is None:
        return ""
    s = str(value).strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s

def flatten_record(row):
    """Convert a record object or dict into a plain dict for field extraction."""
    if row is None:
        return {}

    if isinstance(row, dict):
        flat = dict(row)
    else:
        flat = {}

        if hasattr(row, "data") and isinstance(row.data, dict):
            flat.update(row.data)
        if hasattr(row, "raw") and isinstance(row.raw, dict):
            flat.update(row.raw)

    if hasattr(row, "data") and isinstance(getattr(row, "data"), dict):
        flat.update(row.data)
    if hasattr(row, "raw") and isinstance(getattr(row, "raw"), dict):
        flat.update(row.raw)

    return flat

def variant_keys(name):
    n = str(name).strip()
    if not n:
        return set()
    base = normalize_key(n)
    variants = {base, base.replace("_", "")}
    # common variations
    variants.update({
        base + "_pct",
        base + "_percentage",
        base + "_share",
        base + "_teu",
        base + "_ships",
        base + "_count",
        base + "_total",
        base + "_capacity",
        base + "_fleet",
        base.replace("_", "") + "_pct",
        base.replace("_", "") + "_share",
        base.replace("_", "") + "_teu",
        base.replace("_", "") + "_ships",
    })
    return {v for v in variants if v}

def pick_first(record, *keys, default=None):
    """
    Accept dict-like row or ExtJS record-like object and find first matching candidate.
    """
    if record is None:
        return default

    flat = flatten_record(record)
    if not flat:
        if isinstance(record, dict):
            flat = record
        else:
            return default

    normalized_map = {}
    for k, v in flat.items():
        nk = normalize_key(k)
        normalized_map[nk] = v
        normalized_map[nk.replace("_", "")] = v

    for key in keys:
        for candidate in variant_keys(key):
            if candidate in normalized_map:
                value = normalized_map[candidate]
                if value is not None and value != "" and value != "null" and value != "None":
                    return value

    # Fallback: exact normalized form
    for key in keys:
        nk = normalize_key(key)
        if nk in normalized_map:
            value = normalized_map[nk]
            if value is not None and value != "" and value != "null" and value != "None":
                return value

    return default

def parse_int(val) -> int:
    if val is None:
        return 0
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, str):
        s = val.strip()
        if s == "":
            return 0
        # handle "1,234", "1,234.56", "1234.0", "1.234,56" (Europe)
        s = s.replace(" ", "")
        s = s.replace(",", "")
        if "." in s and "," in s:
            s = s.replace(".", "").replace(",", ".")
        elif "," in s and "." not in s:
            # likely European format -> remove comma and keep decimals if any
            if s.count(",") == 1 and len(s.split(",")[-1]) <= 2:
                s = s.replace(",", "")
            else:
                s = s.replace(",", "")
        s = re.sub(r"[^0-9.\\-]", "", s)
        if s in ("", "-", ".", "-.", ".-"):
            return 0
        # if value has decimal part, truncate
        if "." in s:
            s = s.split(".", 1)[0]
        try:
            return int(float(s))
        except ValueError:
            return 0
    return int(str(val))

def parse_float(val) -> float:
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if s == "":
        return 0.0
    s = s.replace(" ", "")
    s = s.replace("%", "")
    s = s.replace(",", "")
    if "." in s and "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s and "." not in s:
        s = s.replace(",", ".")
    s = re.sub(r"[^0-9.\\-]", "", s)
    try:
        return float(s)
    except ValueError:
        return 0.0

def setup_chrome_driver():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

    driver_path = os.getenv("CHROMEDRIVER_PATH")
    if driver_path and os.path.exists(driver_path):
        service = Service(executable_path=driver_path)
    elif HAS_WEBDRIVER_MANAGER:
        service = Service(ChromeDriverManager().install())
    else:
        service = Service()

    driver = webdriver.Chrome(service=service, options=options)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"}
    )
    return driver

def check_and_extract_store(driver):
    """Attempt to extract ExtJS store data from current active frame."""
    return driver.execute_script("""
        function safeGet(obj, path) {
            try {
                return path.split('.').reduce((o, k) => (o != null ? o[k] : undefined), obj);
            } catch (e) {
                return undefined;
            }
        }

        if (typeof Ext !== 'undefined' && Ext.StoreMgr) {
            var store = null;
            if (Ext.StoreMgr.lookup) {
                store = Ext.StoreMgr.lookup('Top100description');
            }
            if (!store) {
                var items = Ext.StoreMgr.items;
                for (var i = 0; i < items.length; i++) {
                    if (items[i] && items[i].getCount && items[i].getCount() >= 50) {
                        store = items[i];
                        break;
                    }
                }
            }

            if (store && store.getCount && store.getCount() > 0) {
                var records = [];
                for (var i = 0; i < store.getCount(); i++) {
                    var rec = store.getAt(i);
                    if (rec) {
                        var data = rec.data || rec.raw || {};
                        records.push(data);
                    }
                }

                return {
                    items: records,
                    bodyText: document.body ? document.body.innerText : ""
                };
            }
        }

        // additional fallback: search for store-like objects in page globals
        try {
            var candidate = null;
            for (var key in window) {
                if (key && key.toLowerCase().indexOf('top100') >= 0 && window[key] && window[key].getCount) {
                    candidate = window[key];
                    break;
                }
            }
            if (candidate && candidate.getCount && candidate.getCount() > 0) {
                var records2 = [];
                for (var i = 0; i < candidate.getCount(); i++) {
                    var rec2 = candidate.getAt(i);
                    if (rec2) {
                        records2.push(rec2.data || rec2.raw || {});
                    }
                }
                return { items: records2, bodyText: document.body ? document.body.innerText : "" };
            }
        } catch (err) {}

        return null;
    """)

def fetch_alphaliner_data():
    logging.info("[Crawler] Launching Headless Chrome to scrape %s", SOURCE_URL)
    driver = setup_chrome_driver()
    try:
        driver.set_page_load_timeout(60)
        driver.get(SOURCE_URL)

        raw_res = None
        max_attempts = 15

        for attempt in range(max_attempts):
            driver.switch_to.default_content()
            raw_res = check_and_extract_store(driver)
            if raw_res and raw_res.get("items"):
                logging.info("[Crawler] ExtJS store extracted from root frame.")
                break

            iframes = driver.find_elements(By.TAG_NAME, "iframe")
            for idx, frame in enumerate(iframes):
                try:
                    driver.switch_to.default_content()
                    driver.switch_to.frame(frame)
                    raw_res = check_and_extract_store(driver)
                    if raw_res and raw_res.get("items"):
                        logging.info("[Crawler] ExtJS store extracted from iframe index %d.", idx)
                        break
                except Exception:
                    continue

            if raw_res and raw_res.get("items"):
                break

            time.sleep(2)

        if not raw_res or not raw_res.get("items"):
            direct_url = "https://alphaliner.axsmarine.com/PublicTop100/"
            logging.warning("[Crawler] Root/iframe store extraction failed. Falling back to direct URL: %s", direct_url)
            driver.switch_to.default_content()
            driver.get(direct_url)
            time.sleep(5)
            raw_res = check_and_extract_store(driver)

        if not raw_res or not raw_res.get("items"):
            raise TimeoutException("Unable to locate ExtJS store 'Top100description' or carrier records across frames.")

        return raw_res
    finally:
        driver.quit()

def extract_global_totals(body_text):
    patterns = [
        r"TOTAL\s*[:\-]?\s*([\d,]+)\s*TEU",
        r"TOTAL\s*([\d,]+)\s*TEU",
        r"(\d[\d,]*)\s*TEU\s*TOTAL",
        r"TOTAL:\s*([\d,]+)\s*TEU",
    ]
    teu_match = None
    for p in patterns:
        teu_match = re.search(p, body_text, re.IGNORECASE)
        if teu_match:
            break

    ships_patterns = [
        r"TOTAL\s*[:\-]?\s*([\d,]+)\s*SHIPS",
        r"TOTAL\s*([\d,]+)\s*SHIPS",
        r"(\d[\d,]*)\s*SHIPS\s*TOTAL",
        r"TOTAL:\s*([\d,]+)\s*Ships",
    ]
    ships_match = None
    for p in ships_patterns:
        ships_match = re.search(p, body_text, re.IGNORECASE)
        if ships_match:
            break

    extracted_global_teu = parse_int(teu_match.group(1)) if teu_match else 0
    extracted_global_ships = parse_int(ships_match.group(1)) if ships_match else 0

    return extracted_global_teu, extracted_global_ships

def process_and_persist(raw_res):
    items = raw_res.get("items", [])
    body_text = raw_res.get("bodyText", "")

    if not items:
        raise ValueError("Extraction failure: ExtJS Store returned zero records.")

    sample_keys = list(items[0].keys()) if isinstance(items[0], dict) else []
    logging.info("[Crawler] Sample record keys: %s", sample_keys[:50])

    today_str = datetime.now().strftime("%Y-%m-%d")
    collected_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    extracted_global_teu, extracted_global_ships = extract_global_totals(body_text)

    operators = []
    for row in items:
        row_data = flatten_record(row)

        rank = parse_int(pick_first(
            row_data,
            "rank", "position", "id", "order", "rank_no", "rank_num", "sequence",
            "place", "index"
        ))
        if rank <= 0 or rank > 100:
            continue

        op_name = str(
            pick_first(
                row_data,
                "operator",
                "carrier",
                "name",
                "operator_name",
                "operator_name_en",
                "carrier_name",
                "company",
                "company_name",
                "shipping_line",
                "line",
                "operatorname",
                "title",
                default=f"Operator {rank}"
            )
        ).strip()

        total_teu = parse_int(pick_first(
            row_data,
            "total_teu", "teu", "total", "capacity_teu", "fleet_teu",
            "totalcapacityteu", "total_capacity_teu", "teu_total",
            "capacity", "fleetcapacity", "totalcapacity"
        ))
        total_ships = parse_int(pick_first(
            row_data,
            "total_ships", "ships", "ship_count", "num_ships", "fleet_ships",
            "totalships", "total_ship_count", "shipcount", "ships_total"
        ))

        market_share = parse_float(pick_first(
            row_data,
            "market_share", "share", "market", "share_pct", "market_share_pct",
            "marketshare", "share_percent", "market_share_percent", "pct_share",
            "total_market_share", "share_percentage", "market_percentage"
        ))

        owned_teu = parse_int(pick_first(
            row_data,
            "owned_teu", "own_teu", "owned", "owned_capacity_teu", "owned_fleet_teu",
            "ownedcapacityteu", "ownedteu", "owned_capacity", "ownedfleetteu",
            "own_teu_total", "own_capacity_teu", "owned_total_teu"
        ))
        owned_ships = parse_int(pick_first(
            row_data,
            "owned_ships", "own_ships", "owned_ship_count", "owned_fleet_ships",
            "ownedships", "ownships", "owned_count", "owned_total_ships"
        ))
        owned_share = parse_float(pick_first(
            row_data,
            "owned_share", "own_share", "owned_pct", "owned_share_pct",
            "own_share_pct", "ownedshare", "owned_share_percent", "own_pct"
        ))

        chartered_teu = parse_int(pick_first(
            row_data,
            "chartered_teu", "cht_teu", "charter_teu", "chartered", "leased_teu",
            "chartered_fleet_teu", "charteredcapacityteu", "charterteu",
            "chartered_total_teu", "chartered_capacity_teu"
        ))
        chartered_ships = parse_int(pick_first(
            row_data,
            "chartered_ships", "cht_ships", "charter_ships", "leased_ships",
            "charterships", "charteredships", "chartered_fleet_ships", "charter_total_ships"
        ))
        chartered_share = parse_float(pick_first(
            row_data,
            "chartered_share", "cht_share", "charter_share", "chartered_pct",
            "charter_share_pct", "charteredshare", "charter_pct"
        ))

        orderbook_teu = parse_int(pick_first(
            row_data,
            "orderbook_teu", "ord_teu", "order_teu", "orderbook", "newbuild_teu",
            "order_book_teu", "orderbook_total_teu", "orderbookcapacityteu",
            "orderbook_teu_total", "teu_on_order"
        ))
        order_ships = parse_int(pick_first(
            row_data,
            "orderbook_ships", "ord_ships", "order_ships", "newbuild_ships",
            "order_book_ships", "orderbook_ships_total", "ships_on_order"
        ))
        order_share = parse_float(pick_first(
            row_data,
            "orderbook_share", "ord_share", "order_pct", "orderbook_pct",
            "newbuild_pct", "order_book_share", "orderbook_share_pct",
            "order_share", "orderbook_percentage"
        ))

        if total_teu > 0:
            if owned_share == 0 and owned_teu > 0:
                owned_share = (owned_teu / total_teu) * 100
            if chartered_teu > 0 and chartered_share == 0:
                chartered_share = (chartered_teu / total_teu) * 100
            if orderbook_teu > 0 and order_share == 0:
                order_share = (orderbook_teu / total_teu) * 100
            if market_share == 0 and extracted_global_teu > 0:
                market_share = (total_teu / extracted_global_teu) * 100

        operators.append({
            "rank": rank,
            "operator": op_name,
            "total_teu": total_teu,
            "total_ships": total_ships,
            "market_share": round(market_share, 2),
            "owned_teu": owned_teu,
            "owned_ships": owned_ships,
            "owned_share": round(owned_share, 2),
            "chartered_teu": chartered_teu,
            "chartered_ships": chartered_ships,
            "chartered_share": round(chartered_share, 2),
            "orderbook_teu": orderbook_teu,
            "orderbook_ships": order_ships,
            "orderbook_share": round(order_share, 2)
        })

    operators.sort(key=lambda x: x["rank"])

    if extracted_global_teu == 0:
        extracted_global_teu = sum(o["total_teu"] for o in operators)
    if extracted_global_ships == 0:
        extracted_global_ships = sum(o["total_ships"] for o in operators)

    top100_teu = sum(o["total_teu"] for o in operators)
    top100_ships = sum(o["total_ships"] for o in operators)
    top10_teu = sum(o["total_teu"] for o in operators[:10])
    orderbook_teu = sum(o["orderbook_teu"] for o in operators)

    global_teu = extracted_global_teu if extracted_global_teu > 0 else top100_teu
    global_ships = extracted_global_ships if extracted_global_ships > 0 else top100_ships

    top10_share = (top10_teu / global_teu * 100.0) if global_teu > 0 else 0.0

    if extracted_global_teu > 0:
        for op in operators:
            if op["market_share"] == 0 and op["total_teu"] > 0:
                op["market_share"] = round((op["total_teu"] / extracted_global_teu) * 100.0, 2)

    raw_hash = hashlib.sha256(json.dumps(operators, sort_keys=True).encode("utf-8")).hexdigest()

    meta = {
        "snapshot_date": today_str,
        "collected_at": collected_at,
        "global_total_teu": global_teu,
        "global_total_ships": global_ships,
        "top100_total_teu": top100_teu,
        "top100_total_ships": top100_ships,
        "top10_share": round(top10_share, 2),
        "orderbook_total_teu": orderbook_teu,
        "source_url": SOURCE_URL,
        "raw_hash": raw_hash
    }

    latest_raw_hash = None
    with db_lock:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT raw_hash FROM snapshot_meta ORDER BY snapshot_date DESC LIMIT 1"
            ).fetchone()
            latest_raw_hash = row[0] if row else None

    if latest_raw_hash and latest_raw_hash == raw_hash:
        logging.info("[DB] Skipping snapshot save because values are unchanged from the latest crawl.")
        return

    save_snapshot_to_db(meta, operators)
    build_and_save_json()
    generate_static_dashboard()

# =========================================================
# 7. Flask Server Application
# =========================================================
@app.route("/")
def index():
    return Response(HTML_TEMPLATE, mimetype="text/html")

@app.route("/alphaliner_data.json")
def get_json():
    if not JSON_PATH.exists():
        build_and_save_json()
    return send_from_directory(BASE_DIR, "alphaliner_data.json", mimetype="application/json")

# =========================================================
# 8. Execution Entrypoint
# =========================================================
def main():
    parser = argparse.ArgumentParser(description="Alphaliner TOP 100 Liner Operators Pipeline")
    parser.add_argument("--cron", action="store_true", help="Run crawler once and generate static assets")
    parser.add_argument("--build-only", action="store_true", help="Generate JSON and HTML from existing SQLite DB")
    parser.add_argument("--host", default="0.0.0.0", help="Flask bind address")
    parser.add_argument("--port", type=int, default=5000, help="Flask bind port")
    args = parser.parse_args()

    init_db()

    if args.build_only:
        logging.info("[Build] Regenerating JSON and HTML from database...")
        build_and_save_json()
        generate_static_dashboard()
        return

    if args.cron:
        logging.info("[Pipeline] Starting Alphaliner scheduled crawler run...")
        raw_res = fetch_alphaliner_data()
        process_and_persist(raw_res)
        logging.info("[Pipeline] Run successfully finished.")
        return

    try:
        raw_res = fetch_alphaliner_data()
        process_and_persist(raw_res)
    except Exception as e:
        logging.error("[Error] Realtime scrape failed (%s). Checking fallback DB...", e)
        if DB_PATH.exists():
            build_and_save_json()
            generate_static_dashboard()

    logging.info("[Server] Starting Bloomberg MI Terminal on http://%s:%d", args.host, args.port)
    app.run(host=args.host, port=args.port, debug=False)

if __name__ == "__main__":
    main()
