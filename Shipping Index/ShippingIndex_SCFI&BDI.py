import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

# =============================================================================
# 1. Path Configuration
# =============================================================================

SCRIPT_DIR = Path(__file__).resolve().parent

DB_PATH = SCRIPT_DIR / "index_history.db"
JSON_PATH = SCRIPT_DIR / "shipping_index_data.json"
HTML_PATH = SCRIPT_DIR / "index.html"

SUPPORTED_INDEXES = ["SCFI", "BDI"]

# =============================================================================
# 2. Common Functions
# =============================================================================

def log(message):
    """Print timestamped log message."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}")

def now_string():
    """Return current timestamp string."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def atomic_write_text(file_path, content):
    """
    Write output to a temporary file first, then replace the target file.
    This avoids leaving partially written files when execution is interrupted.
    """
    file_path.parent.mkdir(parents=True, exist_ok=True)

    temp_path = file_path.with_suffix(file_path.suffix + ".tmp")

    with open(temp_path, "w", encoding="utf-8", newline="\n") as file:
        file.write(content)

    temp_path.replace(file_path)

# =============================================================================
# 3. Database Read and Data Cleaning
# =============================================================================

def load_index_data(index_name):
    """Load SCFI or BDI records from SQLite database."""

    empty_df = pd.DataFrame(
        columns=["DATE", "REGION", "VALUE", "INDEX_NAME"]
    )

    if not DB_PATH.exists():
        log(f"[ERROR] Database file not found: {DB_PATH}")
        return empty_df

    query = """
        SELECT
            DATE,
            REGION,
            VALUE,
            INDEX_NAME
        FROM shipping_indices
        WHERE UPPER(INDEX_NAME) = UPPER(?)
        ORDER BY DATE ASC
    """

    try:
        with sqlite3.connect(DB_PATH) as connection:
            df = pd.read_sql_query(
                query,
                connection,
                params=[index_name]
            )

        log(f"[DB] {index_name} loaded: {len(df):,} rows")
        return df

    except Exception as error:
        log(f"[ERROR] Failed to load {index_name}: {error}")
        return empty_df

def clean_data(df, index_name):
    """
    Clean index data for JSON serialization and dashboard display.

    Expected database columns:
    - DATE
    - REGION
    - VALUE
    - INDEX_NAME
    """

    if df.empty:
        return pd.DataFrame(
            columns=["DATE", "REGION", "VALUE", "INDEX_NAME"]
        )

    cleaned = df.copy()

    cleaned["DATE"] = pd.to_datetime(
        cleaned["DATE"],
        errors="coerce"
    )

    cleaned["REGION"] = (
        cleaned["REGION"]
        .fillna("Unknown")
        .astype(str)
        .str.strip()
    )

    cleaned["VALUE"] = pd.to_numeric(
        cleaned["VALUE"],
        errors="coerce"
    )

    cleaned["INDEX_NAME"] = index_name

    cleaned = cleaned.dropna(
        subset=["DATE", "VALUE"]
    )

    if cleaned.empty:
        return pd.DataFrame(
            columns=["DATE", "REGION", "VALUE", "INDEX_NAME"]
        )

    # Same date / region duplicates: retain latest stored row.
    cleaned = cleaned.drop_duplicates(
        subset=["DATE", "REGION"],
        keep="last"
    )

    cleaned = cleaned.sort_values(
        by=["DATE", "REGION"]
    ).reset_index(drop=True)

    cleaned["DATE"] = cleaned["DATE"].dt.strftime("%Y-%m-%d")
    cleaned["VALUE"] = cleaned["VALUE"].astype(float)

    return cleaned[
        ["DATE", "REGION", "VALUE", "INDEX_NAME"]
    ]

def load_and_clean_data(index_name):
    """Load raw data from DB and return cleaned data."""
    raw_df = load_index_data(index_name)
    return clean_data(raw_df, index_name)

# =============================================================================
# 4. JSON Generator
# =============================================================================

def build_json():
    """Create shipping_index_data.json for the static dashboard."""

    log("[JSON] shipping_index_data.json 생성 중...")
    log(f"[INFO] DB_PATH   : {DB_PATH}")
    log(f"[INFO] JSON_PATH : {JSON_PATH}")
    log(f"[INFO] HTML_PATH : {HTML_PATH}")
    log(f"[INFO] DB Exists : {DB_PATH.exists()}")

    now_str = now_string()
    indexes_dict = {}

    for index_name in SUPPORTED_INDEXES:
        df = load_and_clean_data(index_name)

        records = (
            df.to_dict(orient="records")
            if not df.empty
            else []
        )

        regions = (
            sorted(df["REGION"].dropna().unique().tolist())
            if not df.empty
            else []
        )

        latest_date = None

        if not df.empty:
            latest_date = df["DATE"].max()

        indexes_dict[index_name] = {
            "regions": regions,
            "records": records,
            "record_count": len(records),
            "latest_date": latest_date,
            "last_sync": now_str
        }

        log(
            f"[JSON] {index_name}: "
            f"records={len(records):,}, regions={len(regions):,}"
        )

    payload = {
        "status": "success",
        "generated_at": now_str,
        "source_database": str(DB_PATH.name),
        "indexes": indexes_dict
    }

    json_content = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2
    )

    atomic_write_text(JSON_PATH, json_content)

    log(f"[JSON] 저장 완료: {JSON_PATH}")

# =============================================================================
# 5. HTML Generator
# =============================================================================

def generate_html_file():
    """
    Create GitHub Pages dashboard index.html.

    The HTML is stored in a raw triple-quoted string.
    JavaScript braces do not need to be escaped because this is NOT an f-string.
    """

    log("[HTML] index.html 생성 중...")

    html_content = r"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
  >
  <meta
    name="description"
    content="SCFI 및 BDI 해운 운임·시황 지수 정적 대시보드"
  >

  <title>Shipping Index Dashboard | SCFI & BDI</title>

  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>

  <style>
    :root {
      --navy: #102a43;
      --blue: #1f5fa8;
      --light-blue: #eef5fb;
      --border: #d9e2ec;
      --text: #243b53;
      --muted: #627d98;
      --bg: #f5f7fa;
      --white: #ffffff;
      --danger: #b42318;
      --success: #027a48;
      --warning: #b54708;
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Arial, "Noto Sans KR", "Malgun Gothic", sans-serif;
    }

    header {
      padding: 30px 20px;
      color: var(--white);
      background: linear-gradient(135deg, #102a43, #1f5fa8);
    }

    header .inner,
    main {
      width: 100%;
      max-width: 1280px;
      margin: 0 auto;
    }

    h1 {
      margin: 0;
      font-size: 32px;
      letter-spacing: -0.5px;
    }

    header p {
      margin: 9px 0 0;
      color: #d9eafd;
      font-size: 15px;
    }

    main {
      padding: 22px 16px 40px;
    }

    .panel,
    .status,
    .kpi {
      border: 1px solid var(--border);
      border-radius: 10px;
      background: var(--white);
      box-shadow: 0 3px 12px rgba(16, 42, 67, 0.06);
    }

    .status {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
      margin-bottom: 16px;
      padding: 13px 16px;
      font-size: 13px;
    }

    .status-left {
      display: flex;
      align-items: center;
      gap: 8px;
    }

    .status-dot {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: var(--success);
    }

    .status-dot.error {
      background: var(--danger);
    }

    .status-text {
      color: var(--muted);
    }

    .panel {
      margin-bottom: 18px;
      padding: 20px;
    }

    h2 {
      margin: 0 0 16px;
      color: var(--navy);
      font-size: 20px;
    }

    .section-description {
      margin: -6px 0 18px;
      color: var(--muted);
      font-size: 14px;
      line-height: 1.6;
    }

    .index-tabs {
      display: flex;
      gap: 10px;
      margin-bottom: 18px;
    }

    .tab-button {
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px 18px;
      color: var(--text);
      background: var(--white);
      cursor: pointer;
      font-size: 14px;
      font-weight: 700;
      transition: all 0.2s ease;
    }

    .tab-button:hover {
      border-color: var(--blue);
      color: var(--blue);
    }

    .tab-button.active {
      border-color: var(--blue);
      color: var(--white);
      background: var(--blue);
    }

    .kpi-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
      margin-bottom: 20px;
    }

    .kpi {
      min-height: 118px;
      padding: 16px;
    }

    .kpi-label {
      display: block;
      margin-bottom: 10px;
      color: var(--muted);
      font-size: 13px;
    }

    .kpi-value {
      display: block;
      color: var(--navy);
      font-size: 27px;
      font-weight: 700;
      letter-spacing: -0.5px;
    }

    .kpi-sub {
      display: block;
      margin-top: 8px;
      color: var(--muted);
      font-size: 12px;
    }

    .positive {
      color: var(--success);
    }

    .negative {
      color: var(--danger);
    }

    .neutral {
      color: var(--muted);
    }

    .chart {
      width: 100%;
      min-height: 440px;
    }

    .controls {
      display: flex;
      align-items: end;
      gap: 14px;
      flex-wrap: wrap;
      margin-bottom: 16px;
    }

    .control-group {
      display: flex;
      flex-direction: column;
      gap: 7px;
    }

    .control-group label {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }

    select {
      min-width: 200px;
      border: 1px solid var(--border);
      border-radius: 7px;
      padding: 10px 12px;
      color: var(--text);
      background: var(--white);
      font-size: 14px;
    }

    .table-wrap {
      overflow-x: auto;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }

    th,
    td {
      padding: 12px;
      border-bottom: 1px solid var(--border);
      text-align: left;
      white-space: nowrap;
    }

    th {
      color: var(--muted);
      background: #f8fafc;
      font-size: 12px;
      font-weight: 700;
    }

    td.numeric,
    th.numeric {
      text-align: right;
    }

    .empty-message {
      padding: 30px 18px;
      border: 1px dashed var(--border);
      border-radius: 8px;
      color: var(--muted);
      background: #f8fafc;
      text-align: center;
      line-height: 1.7;
    }

    footer {
      padding: 20px;
      color: var(--muted);
      font-size: 12px;
      text-align: center;
    }

    @media (max-width: 900px) {
      .kpi-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
    }

    @media (max-width: 560px) {
      header {
        padding: 24px 16px;
      }

      h1 {
        font-size: 25px;
      }

      .panel {
        padding: 16px;
      }

      .kpi-grid {
        grid-template-columns: 1fr;
      }

      .chart {
        min-height: 370px;
      }
    }
  </style>
</head>

<body>
  <header>
    <div class="inner">
      <h1>Shipping Index Dashboard</h1>
      <p>Shanghai Containerized Freight Index (SCFI) · Baltic Dry Index (BDI)</p>
    </div>
  </header>

  <main>
    <section class="status">
      <div class="status-left">
        <span id="statusDot" class="status-dot"></span>
        <strong id="statusTitle">데이터 로딩 중</strong>
      </div>

      <div id="lastUpdated" class="status-text">
        shipping_index_data.json 확인 중...
      </div>
    </section>

    <section class="panel">
      <div class="index-tabs">
        <button
          id="scfiTab"
          class="tab-button active"
          type="button"
          onclick="selectIndex('SCFI')"
        >
          SCFI
        </button>

        <button
          id="bdiTab"
          class="tab-button"
          type="button"
          onclick="selectIndex('BDI')"
        >
          BDI
        </button>
      </div>

      <h2 id="dashboardTitle">SCFI Dashboard</h2>
      <p id="dashboardDescription" class="section-description">
        데이터를 불러오는 중입니다.
      </p>

      <div class="controls">
        <div class="control-group">
          <label for="regionSelect">지역 / 항로 선택</label>
          <select id="regionSelect" onchange="renderDashboard()">
            <option value="">전체</option>
          </select>
        </div>
      </div>

      <div class="kpi-grid">
        <div class="kpi">
          <span class="kpi-label">최신 지수</span>
          <span id="latestValue" class="kpi-value">-</span>
          <span id="latestValueDate" class="kpi-sub">-</span>
        </div>

        <div class="kpi">
          <span class="kpi-label">직전 값</span>
          <span id="previousValue" class="kpi-value">-</span>
          <span id="previousValueDate" class="kpi-sub">-</span>
        </div>

        <div class="kpi">
          <span class="kpi-label">기간 변화</span>
          <span id="changeValue" class="kpi-value neutral">-</span>
          <span id="changeRate" class="kpi-sub">-</span>
        </div>

        <div class="kpi">
          <span class="kpi-label">데이터 건수</span>
          <span id="recordCount" class="kpi-value">-</span>
          <span id="regionCount" class="kpi-sub">-</span>
        </div>
      </div>

      <div id="chart" class="chart"></div>
    </section>

    <section class="panel">
      <h2>최근 데이터</h2>
      <p class="section-description">
        선택된 지수 및 지역 기준으로 최신 20개 데이터를 표시합니다.
      </p>

      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>일자</th>
              <th>지수</th>
              <th>지역 / 항로</th>
              <th class="numeric">값</th>
            </tr>
          </thead>
          <tbody id="dataTableBody">
            <tr>
              <td colspan="4" class="empty-message">데이터 로딩 중...</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </main>

  <footer>
    Market Intelligence Dashboard · Data source: Internal Shipping Index Database
  </footer>

  <script>
    let dashboardData = null;
    let currentIndex = "SCFI";

    const INDEX_DESCRIPTIONS = {
      SCFI: "상하이발 컨테이너 운임 흐름을 지역 및 항로별로 확인합니다.",
      BDI: "벌크선 시황을 대표하는 Baltic Dry Index 및 세부 지수 흐름을 확인합니다."
    };

    const INDEX_COLORS = {
      SCFI: "#1f5fa8",
      BDI: "#027a48"
    };

    function formatNumber(value, decimals = 2) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) {
        return "-";
      }

      return Number(value).toLocaleString("ko-KR", {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals
      });
    }

    function formatInteger(value) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) {
        return "-";
      }

      return Number(value).toLocaleString("ko-KR");
    }

    function formatDate(dateString) {
      if (!dateString) {
        return "-";
      }

      const date = new Date(dateString + "T00:00:00");

      if (Number.isNaN(date.getTime())) {
        return dateString;
      }

      return date.toLocaleDateString("ko-KR", {
        year: "numeric",
        month: "2-digit",
        day: "2-digit"
      });
    }

    function escapeHtml(value) {
      return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }

    function setStatus(isSuccess, title, message) {
      const statusDot = document.getElementById("statusDot");
      const statusTitle = document.getElementById("statusTitle");
      const lastUpdated = document.getElementById("lastUpdated");

      statusDot.className = isSuccess
        ? "status-dot"
        : "status-dot error";

      statusTitle.textContent = title;
      lastUpdated.textContent = message;
    }

    function getCurrentIndexData() {
      if (!dashboardData || !dashboardData.indexes) {
        return {
          regions: [],
          records: [],
          record_count: 0,
          latest_date: null,
          last_sync: null
        };
      }

      return dashboardData.indexes[currentIndex] || {
        regions: [],
        records: [],
        record_count: 0,
        latest_date: null,
        last_sync: null
      };
    }

    function populateRegionSelect() {
      const select = document.getElementById("regionSelect");
      const indexData = getCurrentIndexData();
      const currentValue = select.value;

      select.innerHTML = '<option value="">전체</option>';

      indexData.regions.forEach(function(region) {
        const option = document.createElement("option");
        option.value = region;
        option.textContent = region;
        select.appendChild(option);
      });

      if (indexData.regions.includes(currentValue)) {
        select.value = currentValue;
      }
    }

    function getFilteredRecords() {
      const indexData = getCurrentIndexData();
      const region = document.getElementById("regionSelect").value;

      let records = Array.isArray(indexData.records)
        ? [...indexData.records]
        : [];

      if (region) {
        records = records.filter(function(record) {
          return record.REGION === region;
        });
      }

      records.sort(function(a, b) {
        return String(a.DATE).localeCompare(String(b.DATE));
      });

      return records;
    }

    function updateKpis(records) {
      const latestValueElement = document.getElementById("latestValue");
      const latestValueDateElement = document.getElementById("latestValueDate");
      const previousValueElement = document.getElementById("previousValue");
      const previousValueDateElement = document.getElementById("previousValueDate");
      const changeValueElement = document.getElementById("changeValue");
      const changeRateElement = document.getElementById("changeRate");
      const recordCountElement = document.getElementById("recordCount");
      const regionCountElement = document.getElementById("regionCount");

      const indexData = getCurrentIndexData();

      recordCountElement.textContent = formatInteger(records.length);
      regionCountElement.textContent =
        "지역 / 항로 " + formatInteger(indexData.regions.length) + "개";

      if (records.length === 0) {
        latestValueElement.textContent = "-";
        latestValueDateElement.textContent = "데이터 없음";
        previousValueElement.textContent = "-";
        previousValueDateElement.textContent = "데이터 없음";
        changeValueElement.textContent = "-";
        changeRateElement.textContent = "-";
        changeValueElement.className = "kpi-value neutral";
        return;
      }

      const latest = records[records.length - 1];
      const previous = records.length >= 2
        ? records[records.length - 2]
        : null;

      latestValueElement.textContent = formatNumber(latest.VALUE);
      latestValueDateElement.textContent = formatDate(latest.DATE);

      if (!previous) {
        previousValueElement.textContent = "-";
        previousValueDateElement.textContent = "비교 데이터 없음";
        changeValueElement.textContent = "-";
        changeRateElement.textContent = "비교 데이터 없음";
        changeValueElement.className = "kpi-value neutral";
        return;
      }

      const difference = Number(latest.VALUE) - Number(previous.VALUE);
      const percentChange = Number(previous.VALUE) !== 0
        ? (difference / Number(previous.VALUE)) * 100
        : null;

      const sign = difference > 0 ? "+" : "";
      const styleClass = difference > 0
        ? "positive"
        : difference < 0
          ? "negative"
          : "neutral";

      previousValueElement.textContent = formatNumber(previous.VALUE);
      previousValueDateElement.textContent = formatDate(previous.DATE);

      changeValueElement.textContent = sign + formatNumber(difference);
      changeValueElement.className = "kpi-value " + styleClass;

      if (percentChange === null) {
        changeRateElement.textContent = "변화율 계산 불가";
      } else {
        changeRateElement.textContent =
          sign + formatNumber(percentChange, 2) + "%";
      }
    }

    function renderChart(records) {
      const chartElement = document.getElementById("chart");

      if (records.length === 0) {
        Plotly.purge(chartElement);

        chartElement.innerHTML = `
          <div class="empty-message">
            선택한 조건에 해당하는 데이터가 없습니다.<br>
            데이터베이스의 shipping_indices 테이블 및 REGION 값을 확인해 주세요.
          </div>
        `;

        return;
      }

      const selectedRegion = document.getElementById("regionSelect").value;

      const groupedRecords = {};

      records.forEach(function(record) {
        const groupName = selectedRegion
          ? selectedRegion
          : record.REGION;

        if (!groupedRecords[groupName]) {
          groupedRecords[groupName] = [];
        }

        groupedRecords[groupName].push(record);
      });

      const traces = Object.keys(groupedRecords).map(function(region, index) {
        const regionRecords = groupedRecords[region];

        return {
          x: regionRecords.map(function(record) {
            return record.DATE;
          }),
          y: regionRecords.map(function(record) {
            return Number(record.VALUE);
          }),
          type: "scatter",
          mode: "lines+markers",
          name: region,
          line: {
            width: 2.5,
            color: selectedRegion
              ? INDEX_COLORS[currentIndex]
              : undefined
          },
          marker: {
            size: 5
          },
          hovertemplate:
            "<b>" + escapeHtml(region) + "</b><br>" +
            "일자: %{x}<br>" +
            "값: %{y:,.2f}<extra></extra>"
        };
      });

      const layout = {
        margin: {
          l: 70,
          r: 30,
          t: 30,
          b: 60
        },
        paper_bgcolor: "#ffffff",
        plot_bgcolor: "#ffffff",
        hovermode: "x unified",
        showlegend: !selectedRegion,
        legend: {
          orientation: "h",
          y: -0.25
        },
        xaxis: {
          title: "일자",
          showgrid: false,
          tickformat: "%Y-%m-%d"
        },
        yaxis: {
          title: "Index Value",
          gridcolor: "#e5edf5",
          zerolinecolor: "#d9e2ec"
        }
      };

      const config = {
        responsive: true,
        displaylogo: false,
        modeBarButtonsToRemove: [
          "lasso2d",
          "select2d",
          "autoScale2d"
        ]
      };

      Plotly.newPlot(chartElement, traces, layout, config);
    }

    function renderTable(records) {
      const tableBody = document.getElementById("dataTableBody");

      if (records.length === 0) {
        tableBody.innerHTML = `
          <tr>
            <td colspan="4" class="empty-message">
              표시할 데이터가 없습니다.
            </td>
          </tr>
        `;
        return;
      }

      const latestRecords = [...records]
        .sort(function(a, b) {
          return String(b.DATE).localeCompare(String(a.DATE));
        })
        .slice(0, 20);

      tableBody.innerHTML = latestRecords.map(function(record) {
        return `
          <tr>
            <td>${escapeHtml(formatDate(record.DATE))}</td>
            <td>${escapeHtml(record.INDEX_NAME)}</td>
            <td>${escapeHtml(record.REGION)}</td>
            <td class="numeric">${escapeHtml(formatNumber(record.VALUE))}</td>
          </tr>
        `;
      }).join("");
    }

    function renderDashboard() {
      const indexData = getCurrentIndexData();
      const records = getFilteredRecords();

      document.getElementById("dashboardTitle").textContent =
        currentIndex + " Dashboard";

      document.getElementById("dashboardDescription").textContent =
        INDEX_DESCRIPTIONS[currentIndex];

      if (!indexData.records || indexData.records.length === 0) {
        updateKpis([]);
        renderChart([]);
        renderTable([]);
        return;
      }

      updateKpis(records);
      renderChart(records);
      renderTable(records);
    }

    function selectIndex(indexName) {
      currentIndex = indexName;

      document.getElementById("scfiTab").classList.toggle(
        "active",
        indexName === "SCFI"
      );

      document.getElementById("bdiTab").classList.toggle(
        "active",
        indexName === "BDI"
      );

      populateRegionSelect();
      renderDashboard();
    }

    async function loadDashboardData() {
      try {
        const response = await fetch(
          "shipping_index_data.json?ts=" + Date.now(),
          { cache: "no-store" }
        );

        if (!response.ok) {
          throw new Error(
            "JSON request failed: HTTP " + response.status
          );
        }

        dashboardData = await response.json();

        const generatedAt = dashboardData.generated_at
          ? dashboardData.generated_at
          : "-";

        setStatus(
          true,
          "데이터 로드 완료",
          "최종 갱신: " + generatedAt
        );

        populateRegionSelect();
        renderDashboard();

      } catch (error) {
        console.error(error);

        setStatus(
          false,
          "데이터 로드 실패",
          "shipping_index_data.json 파일을 불러올 수 없습니다."
        );

        document.getElementById("dashboardDescription").textContent =
          "대시보드 데이터를 불러오는 중 오류가 발생했습니다.";

        updateKpis([]);
        renderChart([]);
        renderTable([]);
      }
    }

    document.addEventListener("DOMContentLoaded", function() {
      loadDashboardData();
    });
  </script>
</body>
</html>
"""

    atomic_write_text(HTML_PATH, html_content)

    log(f"[HTML] 저장 완료: {HTML_PATH}")

# =============================================================================
# 6. Main Execution
# =============================================================================

def main():
    """Run Shipping Index static dashboard generation."""

    log("=" * 72)
    log("[CRON] Shipping Index Dashboard 갱신 시작")
    log(f"[INFO] Script directory: {SCRIPT_DIR}")
    log("=" * 72)

    try:
        build_json()
        generate_html_file()

        log("=" * 72)
        log("[CRON] Shipping Index Dashboard 갱신 완료")
        log("=" * 72)

    except Exception as error:
        log(f"[CRITICAL] Shipping Index Dashboard 생성 실패: {error}")
        raise

if __name__ == "__main__":
    main()
