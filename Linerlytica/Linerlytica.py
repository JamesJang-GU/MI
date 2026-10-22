#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Linerlytica Global Containership Fleet Snapshot Crawler & Dashboard Generator

기능
----------------------------------------------------------------
1. Linerlytica의 Global Containership Fleet Snapshot을 수집한다.
2. 매월 17일자 데이터만 SQLite에 누적 저장한다.
3. 과거 누락 월은 Backfill 방식으로 재시도한다.
4. 필수 상태 5개가 모두 수집된 경우에만 해당 월을 완료 처리한다.
5. SQLite 이력을 기반으로 static index.html 대시보드를 생성한다.

필수 패키지
----------------------------------------------------------------
pip install beautifulsoup4 playwright
python -m playwright install chromium

환경변수 선택 설정
----------------------------------------------------------------
LINERLYTICA_URL
    크롤링 대상 URL
    기본값: https://www.linerlytica.com/

LINERLYTICA_START_DATE
    백필 시작일. 반드시 YYYY-MM-17 형식 권장.
    기본값: 2024-01-17

LINERLYTICA_DATE_INPUT_SELECTOR
    실제 사이트의 날짜 input selector.
    예:
    input[type='date']
    input[name='date']
    input[placeholder*='Data']
    기본값: 자동 탐색

LINERLYTICA_DATE_INPUT_FORMAT
    날짜 입력 UI가 요구하는 포맷.
    기본값: %Y-%m-%d
    예: %d/%m/%Y 또는 %m/%d/%Y

LINERLYTICA_STORAGE_STATE
    로그인 세션이 필요한 경우 Playwright storage state JSON 파일 경로.
    예: Linerlytica/auth.json

실행
----------------------------------------------------------------
python Linerlytica/Linerlytica.py
"""

from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from bs4 import BeautifulSoup

try:
    from playwright.sync_api import (
        Browser,
        Error as PlaywrightError,
        Page,
        TimeoutError as PlaywrightTimeoutError,
        sync_playwright,
    )
except ImportError:
    print(
        "[FATAL] Playwright가 설치되어 있지 않습니다.\n"
        "다음 명령을 실행하세요:\n"
        "  pip install playwright\n"
        "  python -m playwright install chromium"
    )
    sys.exit(1)

# =============================================================================
# 1. 기본 설정
# =============================================================================

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "Linerlytica.db"
HTML_PATH = BASE_DIR / "index.html"

TARGET_URL = os.getenv("LINERLYTICA_URL", "https://www.linerlytica.com/")
START_DATE_STR = os.getenv("LINERLYTICA_START_DATE", "2024-01-17")

DATE_INPUT_SELECTOR = os.getenv("LINERLYTICA_DATE_INPUT_SELECTOR", "").strip()
DATE_INPUT_FORMAT = os.getenv("LINERLYTICA_DATE_INPUT_FORMAT", "%Y-%m-%d").strip()

STORAGE_STATE_PATH = os.getenv("LINERLYTICA_STORAGE_STATE", "").strip()

HEADLESS = os.getenv("LINERLYTICA_HEADLESS", "true").strip().lower() not in {
    "false",
    "0",
    "no",
}

PAGE_TIMEOUT_MS = 60_000
DATA_REFRESH_WAIT_MS = 3_000

# =============================================================================
# 2. 상태 표준화 정의
# =============================================================================

REQUIRED_STATUSES: Tuple[str, ...] = (
    "Ships at port",
    "Active Ships",
    "Inactive Ships",
    "Ships at anchorage",
    "Ships in shipyard",
)

STATUS_ALIASES = {
    "ships at port": "Ships at port",
    "ships at ports": "Ships at port",
    "ships in port": "Ships at port",
    "ships in ports": "Ships at port",
    "active ships": "Active Ships",
    "inactive ships": "Inactive Ships",
    "ships at anchorage": "Ships at anchorage",
    "ships at anchor": "Ships at anchorage",
    "ships in shipyard": "Ships in shipyard",
    "ships in shipyards": "Ships in shipyard",
    "ships at shipyard": "Ships in shipyard",
}

# =============================================================================
# 3. 공통 유틸리티
# =============================================================================

class DateSelectionError(Exception):
    """웹사이트에서 목표 날짜를 선택하지 못한 경우 발생."""

class IncompleteSnapshotError(Exception):
    """필수 상태가 모두 수집되지 않은 경우 발생."""

def utc_now_string() -> str:
    """UTC 기준 수집 시각을 ISO 형식으로 반환한다."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()

def normalize_status(value: str) -> Optional[str]:
    """웹페이지의 상태명을 DB 표준 상태명으로 변환한다."""
    normalized = normalize_space(value).lower()
    return STATUS_ALIASES.get(normalized)

def parse_integer(value: str) -> Optional[int]:
    """
    문자열에서 정수값을 안전하게 파싱한다.

    예:
    '5,751'       -> 5751
    '24,488,025'  -> 24488025
    '-'           -> None
    """
    if value is None:
        return None

    cleaned = re.sub(r"[^\d]", "", str(value))
    if not cleaned:
        return None

    try:
        return int(cleaned)
    except ValueError:
        return None

def month_17th_dates(start_date: date, end_date: date) -> List[str]:
    """
    start_date부터 end_date까지 매월 17일을 YYYY-MM-DD 형식으로 반환한다.

    현재 월은 당일이 17일 이전이면 포함하지 않는다.
    """
    result: List[str] = []

    year = start_date.year
    month = start_date.month

    while True:
        target = date(year, month, 17)

        if target >= start_date and target <= end_date:
            result.append(target.isoformat())

        if year == end_date.year and month == end_date.month:
            break

        month += 1
        if month == 13:
            month = 1
            year += 1

    return result

def safe_percent_change(current: Optional[int], previous: Optional[int]) -> Optional[float]:
    """직전 값 대비 증감률을 계산한다."""
    if current is None or previous in (None, 0):
        return None
    return ((current - previous) / previous) * 100

def format_number(value: Optional[int]) -> str:
    """숫자를 천 단위 콤마 문자열로 표시한다."""
    if value is None:
        return "-"
    return f"{value:,}"

def format_delta(current: Optional[int], previous: Optional[int]) -> str:
    """증감 수치와 증감률을 HTML 문자열로 반환한다."""
    if current is None or previous is None:
        return '<span class="neutral">-</span>'

    delta = current - previous
    pct = safe_percent_change(current, previous)

    if delta > 0:
        pct_text = f"{pct:+.1f}%" if pct is not None else "-"
        return f'<span class="up">↗ +{delta:,} ({pct_text})</span>'

    if delta < 0:
        pct_text = f"{pct:+.1f}%" if pct is not None else "-"
        return f'<span class="down">↘ {delta:,} ({pct_text})</span>'

    return '<span class="neutral">→ 0 (0.0%)</span>'

# =============================================================================
# 4. SQLite 관리
# =============================================================================

def init_db() -> sqlite3.Connection:
    """SQLite DB와 테이블을 초기화한다."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS fleet_snapshot (
            snapshot_date TEXT NOT NULL,
            ship_status TEXT NOT NULL,
            ships INTEGER NOT NULL,
            teu INTEGER NOT NULL,
            source_url TEXT,
            collected_at TEXT NOT NULL,
            PRIMARY KEY (snapshot_date, ship_status)
        )
        """
    )

    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_fleet_snapshot_date
        ON fleet_snapshot(snapshot_date)
        """
    )

    conn.commit()
    return conn

def get_completed_dates(conn: sqlite3.Connection) -> Set[str]:
    """
    필수 상태 5개가 모두 존재하는 날짜만 '수집 완료' 날짜로 판단한다.

    일부 상태만 저장된 날짜는 다음 실행 시 재수집 대상이 된다.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT snapshot_date, ship_status
        FROM fleet_snapshot
        """
    )

    status_by_date: Dict[str, Set[str]] = {}

    for row in cur.fetchall():
        snapshot_date = row["snapshot_date"]
        ship_status = row["ship_status"]
        status_by_date.setdefault(snapshot_date, set()).add(ship_status)

    required = set(REQUIRED_STATUSES)

    return {
        snapshot_date
        for snapshot_date, statuses in status_by_date.items()
        if required.issubset(statuses)
    }

def delete_snapshot_date(conn: sqlite3.Connection, snapshot_date: str) -> None:
    """특정 날짜의 기존 데이터를 삭제한다."""
    conn.execute(
        "DELETE FROM fleet_snapshot WHERE snapshot_date = ?",
        (snapshot_date,),
    )

def save_snapshot(
    conn: sqlite3.Connection,
    snapshot_date: str,
    data_rows: List[Dict[str, int]],
    source_url: str,
) -> None:
    """
    하나의 월별 스냅샷을 저장한다.

    저장 전 해당 날짜 데이터를 삭제하므로, 재수집 시에도 중복 또는
    과거 불완전 데이터가 남지 않는다.
    """
    found_statuses = {row["ship_status"] for row in data_rows}
    required_statuses = set(REQUIRED_STATUSES)
    missing_statuses = required_statuses - found_statuses

    if missing_statuses:
        raise IncompleteSnapshotError(
            f"{snapshot_date} 데이터가 불완전합니다. "
            f"누락 상태: {', '.join(sorted(missing_statuses))}"
        )

    collected_at = utc_now_string()

    with conn:
        delete_snapshot_date(conn, snapshot_date)

        for row in data_rows:
            conn.execute(
                """
                INSERT INTO fleet_snapshot (
                    snapshot_date,
                    ship_status,
                    ships,
                    teu,
                    source_url,
                    collected_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_date,
                    row["ship_status"],
                    int(row["ships"]),
                    int(row["teu"]),
                    source_url,
                    collected_at,
                ),
            )

    print(
        f"  [DB Saved] {snapshot_date} | "
        f"{len(data_rows)} status rows | collected_at={collected_at}"
    )

# =============================================================================
# 5. HTML 테이블 파싱
# =============================================================================

def get_candidate_tables(soup: BeautifulSoup) -> List:
    """
    'Global Containership Fleet Snapshot' 제목 주변의 테이블을 우선 탐색한다.
    찾지 못하면 전체 테이블을 후보로 반환한다.
    """
    heading_pattern = re.compile(
        r"global\s+containership\s+fleet\s+snapshot",
        re.IGNORECASE,
    )

    heading = soup.find(
        lambda tag: tag
        and tag.name in {"h1", "h2", "h3", "h4", "h5", "div", "span", "p"}
        and heading_pattern.search(normalize_space(tag.get_text(" ", strip=True)))
    )

    candidate_tables = []

    if heading:
        parent = heading.parent

        if parent:
            candidate_tables.extend(parent.find_all("table"))

        for sibling in heading.find_all_next(limit=30):
            if sibling.name == "table":
                candidate_tables.append(sibling)
                break

    if candidate_tables:
        return candidate_tables

    return soup.find_all("table")

def get_header_map(table) -> Dict[str, int]:
    """
    테이블 헤더에서 status / ships / teu 열 인덱스를 찾는다.
    """
    header_map: Dict[str, int] = {}

    header_row = table.find("tr")
    if not header_row:
        return header_map

    cells = header_row.find_all(["th", "td"])
    header_texts = [
        normalize_space(cell.get_text(" ", strip=True)).lower()
        for cell in cells
    ]

    for index, text in enumerate(header_texts):
        if any(keyword in text for keyword in ("status", "ship status", "fleet status")):
            header_map["status"] = index
        elif text == "ships" or text.startswith("ships"):
            header_map["ships"] = index
        elif "teu" in text:
            header_map["teu"] = index

    return header_map

def parse_snapshot_table_html(html_text: str) -> List[Dict[str, int]]:
    """
    HTML에서 Global Containership Fleet Snapshot 표를 찾아
    표준 상태명 / Ships / TEU 구조로 변환한다.

    반환 예:
    [
        {"ship_status": "Active Ships", "ships": 5751, "teu": 24488025},
        ...
    ]
    """
    soup = BeautifulSoup(html_text, "html.parser")
    parsed: Dict[str, Dict[str, int]] = {}

    for table in get_candidate_tables(soup):
        table_text = normalize_space(table.get_text(" ", strip=True)).lower()

        has_status_signal = any(
            key in table_text
            for key in (
                "active ships",
                "inactive ships",
                "ships at port",
                "ships at anchorage",
                "ships in shipyard",
            )
        )

        has_metric_signal = "teu" in table_text and "ships" in table_text

        if not (has_status_signal and has_metric_signal):
            continue

        header_map = get_header_map(table)

        for tr in table.find_all("tr"):
            cells = [
                normalize_space(cell.get_text(" ", strip=True))
                for cell in tr.find_all(["td", "th"])
            ]

            if len(cells) < 3:
                continue

            status = None
            ships = None
            teu = None

            if (
                "status" in header_map
                and "ships" in header_map
                and "teu" in header_map
                and max(header_map.values()) < len(cells)
            ):
                status = normalize_status(cells[header_map["status"]])
                ships = parse_integer(cells[header_map["ships"]])
                teu = parse_integer(cells[header_map["teu"]])
            else:
                # 일반적인 구조: Status | Ships | TEU
                status = normalize_status(cells[0])
                ships = parse_integer(cells[1])
                teu = parse_integer(cells[2])

            if status and ships is not None and teu is not None:
                parsed[status] = {
                    "ship_status": status,
                    "ships": ships,
                    "teu": teu,
                }

    return [parsed[status] for status in REQUIRED_STATUSES if status in parsed]

# =============================================================================
# 6. Playwright 날짜 선택 및 크롤링
# =============================================================================

def candidate_date_selectors() -> List[str]:
    """
    환경변수 selector가 있으면 우선 사용하고,
    없으면 일반적인 date input selector를 순차 시도한다.
    """
    selectors = []

    if DATE_INPUT_SELECTOR:
        selectors.append(DATE_INPUT_SELECTOR)

    selectors.extend(
        [
            "input[type='date']",
            "input[name='date']",
            "input[name='snapshot_date']",
            "input[name='data_date']",
            "input[id*='date' i]",
            "input[class*='date' i]",
            "input[placeholder*='date' i]",
            "input[placeholder*='as at' i]",
        ]
    )

    seen = set()
    deduplicated = []

    for selector in selectors:
        if selector not in seen:
            seen.add(selector)
            deduplicated.append(selector)

    return deduplicated

def select_target_date(page: Page, target_date: str) -> str:
    """
    페이지 내 날짜 input을 찾아 목표 날짜를 입력한다.

    성공 시 실제로 사용된 selector를 반환한다.
    실패 시 DateSelectionError를 발생시킨다.

    주의:
    실제 Linerlytica UI가 일반 input이 아닌 React 달력/버튼형 위젯이라면
    LINERLYTICA_DATE_INPUT_SELECTOR를 실제 selector로 지정하거나,
    이 함수에 해당 사이트의 클릭 로직을 추가해야 한다.
    """
    target_dt = datetime.strptime(target_date, "%Y-%m-%d")
    formatted_date = target_dt.strftime(DATE_INPUT_FORMAT)

    errors = []

    for selector in candidate_date_selectors():
        try:
            locator = page.locator(selector).first

            if locator.count() == 0:
                continue

            try:
                locator.scroll_into_view_if_needed(timeout=5_000)
            except PlaywrightError:
                pass

            # fill() 우선 시도
            try:
                locator.fill(formatted_date, timeout=8_000)
            except PlaywrightError:
                # 날짜 input이 readonly 또는 커스텀 위젯인 경우
                # JavaScript setter 및 event dispatch를 시도한다.
                locator.evaluate(
                    """
                    (element, value) => {
                        const setter = Object.getOwnPropertyDescriptor(
                            HTMLInputElement.prototype,
                            'value'
                        ).set;

                        setter.call(element, value);

                        element.dispatchEvent(
                            new Event('input', { bubbles: true })
                        );
                        element.dispatchEvent(
                            new Event('change', { bubbles: true })
                        );
                        element.dispatchEvent(
                            new Event('blur', { bubbles: true })
                        );
                    }
                    """,
                    formatted_date,
                )

            try:
                locator.press("Enter", timeout=3_000)
            except PlaywrightError:
                pass

            page.wait_for_timeout(DATA_REFRESH_WAIT_MS)

            # 값이 target date와 일치하는지 가능한 범위에서 검증한다.
            current_value = locator.input_value(timeout=5_000)

            if current_value:
                normalized_current = current_value.strip()

                valid_values = {
                    target_date,
                    formatted_date,
                    target_dt.strftime("%Y-%m-%d"),
                }

                if normalized_current not in valid_values:
                    # 일부 date picker는 표시 형식이 다를 수 있으므로,
                    # 페이지는 계속 파싱하되 로그에서 확인 가능하도록 한다.
                    print(
                        f"  [Warning] Date input value differs. "
                        f"selector={selector} | expected={formatted_date} | "
                        f"actual={normalized_current}"
                    )

            return selector

        except Exception as exc:
            errors.append(f"{selector}: {exc}")

    error_text = " | ".join(errors) if errors else "No matching date input found."

    raise DateSelectionError(
        f"날짜 선택 실패: {target_date}\n"
        f"시도 selector: {candidate_date_selectors()}\n"
        f"상세: {error_text}\n"
        f"필요하면 LINERLYTICA_DATE_INPUT_SELECTOR 환경변수에 "
        f"실제 날짜 input selector를 지정하세요."
    )

def crawl_one_snapshot(page: Page, target_date: str) -> List[Dict[str, int]]:
    """특정 날짜의 페이지를 열고, 날짜를 선택한 후 표 데이터를 추출한다."""
    page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)

    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except PlaywrightTimeoutError:
        # 사이트에 지속 요청이 있으면 networkidle이 끝나지 않을 수 있으므로 무시
        pass

    used_selector = select_target_date(page, target_date)

    print(f"  [Date Selected] {target_date} | selector={used_selector}")

    # 날짜 변경 후 API/렌더링 완료를 위한 대기
    page.wait_for_timeout(DATA_REFRESH_WAIT_MS)

    html_content = page.content()
    data_rows = parse_snapshot_table_html(html_content)

    found_statuses = {row["ship_status"] for row in data_rows}
    missing_statuses = set(REQUIRED_STATUSES) - found_statuses

    if missing_statuses:
        raise IncompleteSnapshotError(
            f"{target_date} 표 데이터 불완전. "
            f"수집 상태={sorted(found_statuses)} | "
            f"누락 상태={sorted(missing_statuses)}"
        )

    return data_rows

def crawl_and_backfill(conn: sqlite3.Connection) -> None:
    """
    DB에서 완전하게 저장되지 않은 매월 17일자를 찾아 수집한다.
    """
    try:
        start_date = datetime.strptime(START_DATE_STR, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(
            "LINERLYTICA_START_DATE는 YYYY-MM-DD 형식이어야 합니다. "
            f"현재 값: {START_DATE_STR}"
        ) from exc

    current_utc_date = datetime.now(timezone.utc).date()

    if current_utc_date.day < 17:
        end_date = date(current_utc_date.year, current_utc_date.month, 1)
        if end_date.month == 1:
            end_date = date(end_date.year - 1, 12, 17)
        else:
            end_date = date(end_date.year, end_date.month - 1, 17)
    else:
        end_date = current_utc_date

    target_dates = month_17th_dates(start_date, end_date)
    completed_dates = get_completed_dates(conn)

    missing_dates = [
        snapshot_date
        for snapshot_date in target_dates
        if snapshot_date not in completed_dates
    ]

    if not missing_dates:
        print("[Crawler] 모든 월별 17일자 스냅샷이 최신 상태입니다.")
        return

    print(
        f"[Crawler] Missing / incomplete snapshot dates: "
        f"{len(missing_dates)}"
    )
    print(f"[Crawler] Targets: {missing_dates}")

    storage_state = None
    if STORAGE_STATE_PATH:
        candidate = Path(STORAGE_STATE_PATH)

        if not candidate.is_absolute():
            candidate = BASE_DIR.parent / candidate

        if candidate.exists():
            storage_state = str(candidate)
            print(f"[Crawler] Using Playwright storage state: {storage_state}")
        else:
            print(
                f"[Warning] LINERLYTICA_STORAGE_STATE file not found: "
                f"{candidate}"
            )

    with sync_playwright() as playwright:
        try:
            browser: Browser = playwright.chromium.launch(headless=HEADLESS)
        except PlaywrightError as exc:
            raise RuntimeError(
                "Chromium 브라우저를 실행할 수 없습니다.\n"
                "다음 명령을 실행하세요:\n"
                "  python -m playwright install chromium\n"
                f"원본 오류: {exc}"
            ) from exc

        context_kwargs = {
            "user_agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
            "viewport": {
                "width": 1600,
                "height": 1200,
            },
            "locale": "en-US",
            "timezone_id": "UTC",
        }

        if storage_state:
            context_kwargs["storage_state"] = storage_state

        context = browser.new_context(**context_kwargs)
        page = context.new_page()
        page.set_default_timeout(PAGE_TIMEOUT_MS)

        try:
            for target_date in missing_dates:
                print(f"[Crawler] Processing: {target_date}")

                try:
                    data_rows = crawl_one_snapshot(page, target_date)

                    save_snapshot(
                        conn=conn,
                        snapshot_date=target_date,
                        data_rows=data_rows,
                        source_url=page.url or TARGET_URL,
                    )

                except DateSelectionError as exc:
                    print(f"  [Skip: Date Selection Failed] {exc}")

                except IncompleteSnapshotError as exc:
                    print(f"  [Skip: Incomplete Data] {exc}")

                except PlaywrightTimeoutError as exc:
                    print(f"  [Skip: Timeout] {target_date} | {exc}")

                except Exception as exc:
                    print(
                        f"  [Skip: Unexpected Error] {target_date} | "
                        f"{type(exc).__name__}: {exc}"
                    )

        finally:
            context.close()
            browser.close()

# =============================================================================
# 7. Dashboard 생성
# =============================================================================

def get_snapshot_data(
    conn: sqlite3.Connection,
    snapshot_date: str,
) -> Dict[str, Dict[str, int]]:
    """특정 날짜의 상태별 Ships/TEU 데이터를 조회한다."""
    cur = conn.cursor()

    cur.execute(
        """
        SELECT ship_status, ships, teu
        FROM fleet_snapshot
        WHERE snapshot_date = ?
        """,
        (snapshot_date,),
    )

    return {
        row["ship_status"]: {
            "ships": row["ships"],
            "teu": row["teu"],
        }
        for row in cur.fetchall()
    }

def generate_dashboard_html(conn: sqlite3.Connection) -> None:
    """SQLite 이력을 기반으로 static index.html을 생성한다."""
    completed_dates = sorted(get_completed_dates(conn))

    if not completed_dates:
        print("[Dashboard] DB에 완전한 스냅샷 데이터가 없어 index.html을 생성하지 않습니다.")
        return

    latest_date = completed_dates[-1]
    previous_date = completed_dates[-2] if len(completed_dates) >= 2 else None

    latest_data = get_snapshot_data(conn, latest_date)
    previous_data = (
        get_snapshot_data(conn, previous_date)
        if previous_date
        else {}
    )

    active_latest = latest_data.get("Active Ships", {})
    active_previous = previous_data.get("Active Ships", {})

    cur = conn.cursor()
    cur.execute(
        """
        SELECT snapshot_date, ship_status, ships, teu
        FROM fleet_snapshot
        ORDER BY snapshot_date ASC, ship_status ASC
        """
    )

    raw_history = cur.fetchall()

    history_map: Dict[Tuple[str, str], Dict[str, int]] = {}

    for row in raw_history:
        history_map[(row["snapshot_date"], row["ship_status"])] = {
            "ships": row["ships"],
            "teu": row["teu"],
        }

    chart_dates = completed_dates

    teu_series: Dict[str, List[Optional[int]]] = {
        status: [] for status in REQUIRED_STATUSES
    }

    ships_series: Dict[str, List[Optional[int]]] = {
        status: [] for status in REQUIRED_STATUSES
    }

    for snapshot_date in chart_dates:
        for status in REQUIRED_STATUSES:
            point = history_map.get((snapshot_date, status))
            teu_series[status].append(point["teu"] if point else None)
            ships_series[status].append(point["ships"] if point else None)

    table_rows_html = ""

    for status in REQUIRED_STATUSES:
        latest = latest_data.get(status, {})
        previous = previous_data.get(status, {})

        latest_ships = latest.get("ships")
        latest_teu = latest.get("teu")

        previous_ships = previous.get("ships")
        previous_teu = previous.get("teu")

        table_rows_html += f"""
            <tr>
                <td class="status-name">{html.escape(status)}</td>
                <td class="num">{format_number(latest_ships)}</td>
                <td class="delta">{format_delta(latest_ships, previous_ships)}</td>
                <td class="num">{format_number(latest_teu)}</td>
                <td class="delta">{format_delta(latest_teu, previous_teu)}</td>
            </tr>
        """

    latest_active_ships = active_latest.get("ships")
    latest_active_teu = active_latest.get("teu")

    previous_active_ships = active_previous.get("ships")
    previous_active_teu = active_previous.get("teu")

    active_ship_delta = format_delta(latest_active_ships, previous_active_ships)
    active_teu_delta = format_delta(latest_active_teu, previous_active_teu)

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Linerlytica | Fleet Snapshot Terminal</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.6/dist/chart.umd.min.js"></script>
    <style>
        :root {{
            --bg: #090d14;
            --panel: #111824;
            --panel-2: #0d131d;
            --line: #293548;
            --text: #e7edf6;
            --muted: #8c9aad;
            --orange: #ff9d00;
            --blue: #29b6f6;
            --green: #00d084;
            --red: #ff5c6c;
            --yellow: #ffd166;
            --font-mono: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
        }}

        * {{
            box-sizing: border-box;
        }}

        body {{
            margin: 0;
            background: var(--bg);
            color: var(--text);
            font-family: Arial, Helvetica, sans-serif;
            padding: 24px;
        }}

        .container {{
            max-width: 1600px;
            margin: 0 auto;
        }}

        .header {{
            display: flex;
            align-items: flex-end;
            justify-content: space-between;
            gap: 20px;
            padding: 0 0 15px 0;
            margin-bottom: 20px;
            border-bottom: 2px solid var(--orange);
        }}

        .title {{
            display: flex;
            align-items: center;
            gap: 10px;
            font-weight: 800;
            font-size: 23px;
            letter-spacing: 0.5px;
        }}

        .title-marker {{
            width: 11px;
            height: 11px;
            background: var(--orange);
            display: inline-block;
        }}

        .subtitle,
        .meta {{
            margin-top: 6px;
            color: var(--muted);
            font-family: var(--font-mono);
            font-size: 12px;
        }}

        .meta {{
            text-align: right;
            line-height: 1.6;
        }}

        .kpi-grid {{
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 16px;
            margin-bottom: 20px;
        }}

        .kpi {{
            background: var(--panel);
            border: 1px solid var(--line);
            border-left: 4px solid var(--blue);
            padding: 16px;
            min-height: 120px;
        }}

        .kpi-label {{
            color: var(--muted);
            font-family: var(--font-mono);
            font-size: 11px;
            letter-spacing: 0.6px;
            text-transform: uppercase;
        }}

        .kpi-value {{
            font-family: var(--font-mono);
            color: #ffffff;
            font-size: 25px;
            font-weight: 700;
            margin-top: 11px;
        }}

        .kpi-delta {{
            min-height: 18px;
            margin-top: 7px;
            font-family: var(--font-mono);
            font-size: 11px;
        }}

        .main-grid {{
            display: grid;
            grid-template-columns: minmax(650px, 1.1fr) minmax(500px, 1fr);
            gap: 20px;
        }}

        .card {{
            background: var(--panel);
            border: 1px solid var(--line);
            padding: 18px;
        }}

        .card-header {{
            display: flex;
            justify-content: space-between;
            gap: 12px;
            margin-bottom: 16px;
            color: var(--orange);
            font-family: var(--font-mono);
            font-size: 13px;
            font-weight: 700;
            letter-spacing: 0.5px;
            text-transform: uppercase;
        }}

        .card-date {{
            color: var(--muted);
            font-size: 11px;
            font-weight: 400;
        }}

        table {{
            border-collapse: collapse;
            width: 100%;
            font-family: var(--font-mono);
            font-size: 12px;
        }}

        th {{
            background: var(--panel-2);
            color: var(--muted);
            border-bottom: 1px solid var(--line);
            font-weight: 600;
            padding: 10px;
            text-align: left;
        }}

        td {{
            border-bottom: 1px solid rgba(255, 255, 255, 0.06);
            padding: 12px 10px;
        }}

        tr:last-child td {{
            border-bottom: 0;
        }}

        .status-name {{
            color: #ffffff;
            font-weight: 700;
        }}

        .num {{
            text-align: right;
        }}

        .delta {{
            min-width: 125px;
            text-align: right;
            white-space: nowrap;
        }}

        .up {{
            color: var(--green);
        }}

        .down {{
            color: var(--red);
        }}

        .neutral {{
            color: var(--muted);
        }}

        .chart-wrap {{
            height: 325px;
            position: relative;
        }}

        .footer {{
            color: var(--muted);
            font-family: var(--font-mono);
            font-size: 11px;
            margin-top: 18px;
        }}

        @media (max-width: 1100px) {{
            .main-grid {{
                grid-template-columns: 1fr;
            }}
        }}

        @media (max-width: 720px) {{
            body {{
                padding: 14px;
            }}

            .header {{
                align-items: flex-start;
                flex-direction: column;
            }}

            .meta {{
                text-align: left;
            }}

            .kpi-grid {{
                grid-template-columns: 1fr;
            }}

            .table-wrap {{
                overflow-x: auto;
            }}
        }}
    </style>
</head>

<body>
    <div class="container">
        <header class="header">
            <div>
                <div class="title">
                    <span class="title-marker"></span>
                    LINERLYTICA FLEET SNAPSHOT
                </div>
                <div class="subtitle">
                    GLOBAL CONTAINERSHIP FLEET STATUS | MONTHLY 17TH HISTORY
                </div>
            </div>

            <div class="meta">
                LATEST SNAPSHOT: <strong>{html.escape(latest_date)}</strong><br>
                PREVIOUS SNAPSHOT: <strong>{html.escape(previous_date or "N/A")}</strong>
            </div>
        </header>

        <section class="kpi-grid">
            <div class="kpi">
                <div class="kpi-label">Active Ships</div>
                <div class="kpi-value">{format_number(latest_active_ships)} Ships</div>
                <div class="kpi-delta">{active_ship_delta}</div>
            </div>

            <div class="kpi">
                <div class="kpi-label">Active Capacity</div>
                <div class="kpi-value">{format_number(latest_active_teu)} TEU</div>
                <div class="kpi-delta">{active_teu_delta}</div>
            </div>

            <div class="kpi">
                <div class="kpi-label">Historical Snapshots</div>
                <div class="kpi-value">{len(completed_dates)} Months</div>
                <div class="kpi-delta">
                    <span class="neutral">Monthly 17th records only</span>
                </div>
            </div>
        </section>

        <main class="main-grid">
            <section class="card">
                <div class="card-header">
                    <span>Current Fleet Status</span>
                    <span class="card-date">AS OF {html.escape(latest_date)}</span>
                </div>

                <div class="table-wrap">
                    <table>
                        <thead>
                            <tr>
                                <th>Ship Status</th>
                                <th class="num">Ships</th>
                                <th class="delta">vs Previous</th>
                                <th class="num">TEU</th>
                                <th class="delta">vs Previous</th>
                            </tr>
                        </thead>
                        <tbody>
                            {table_rows_html}
                        </tbody>
                    </table>
                </div>
            </section>

            <section class="card">
                <div class="card-header">
                    <span>Capacity Trend</span>
                    <span class="card-date">TEU</span>
                </div>
                <div class="chart-wrap">
                    <canvas id="teuChart"></canvas>
                </div>
            </section>
        </main>

        <div class="footer">
            SOURCE: LINERLYTICA | DATA POLICY: MONTHLY SNAPSHOT ON THE 17TH |
            GENERATED FROM Linerlytica.db
        </div>
    </div>

    <script>
        const trendDates = {json.dumps(chart_dates, ensure_ascii=False)};

        const chartContext = document.getElementById("teuChart");

        new Chart(chartContext, {{
            type: "line",
            data: {{
                labels: trendDates,
                datasets: [
                    {{
                        label: "Active Ships",
                        data: {json.dumps(teu_series["Active Ships"])},
                        borderColor: "#00d084",
                        backgroundColor: "rgba(0, 208, 132, 0.08)",
                        borderWidth: 2.2,
                        pointRadius: 2,
                        pointHoverRadius: 5,
                        tension: 0.2
                    }},
                    {{
                        label: "Ships at Port",
                        data: {json.dumps(teu_series["Ships at port"])},
                        borderColor: "#ff9d00",
                        backgroundColor: "transparent",
                        borderWidth: 1.7,
                        pointRadius: 1.5,
                        pointHoverRadius: 4,
                        tension: 0.2
                    }},
                    {{
                        label: "Ships at Anchorage",
                        data: {json.dumps(teu_series["Ships at anchorage"])},
                        borderColor: "#29b6f6",
                        backgroundColor: "transparent",
                        borderWidth: 1.7,
                        pointRadius: 1.5,
                        pointHoverRadius: 4,
                        tension: 0.2
                    }},
                    {{
                        label: "Inactive Ships",
                        data: {json.dumps(teu_series["Inactive Ships"])},
                        borderColor: "#ff5c6c",
                        backgroundColor: "transparent",
                        borderWidth: 1.5,
                        pointRadius: 1.5,
                        pointHoverRadius: 4,
                        tension: 0.2
                    }},
                    {{
                        label: "Ships in Shipyard",
                        data: {json.dumps(teu_series["Ships in shipyard"])},
                        borderColor: "#ffd166",
                        backgroundColor: "transparent",
                        borderWidth: 1.5,
                        pointRadius: 1.5,
                        pointHoverRadius: 4,
                        tension: 0.2
                    }}
                ]
            }},
            options: {{
                responsive: true,
                maintainAspectRatio: false,
                interaction: {{
                    mode: "index",
                    intersect: false
                }},
                plugins: {{
                    legend: {{
                        position: "bottom",
                        labels: {{
                            color: "#dce4f0",
                            boxWidth: 12,
                            boxHeight: 12,
                            padding: 14,
                            font: {{
                                family: "Consolas, monospace",
                                size: 11
                            }}
                        }}
                    }},
                    tooltip: {{
                        backgroundColor: "#111824",
                        borderColor: "#293548",
                        borderWidth: 1,
                        titleColor: "#ffffff",
                        bodyColor: "#dce4f0",
                        callbacks: {{
                            label: function(context) {{
                                const value = context.raw;
                                if (value === null || value === undefined) {{
                                    return context.dataset.label + ": N/A";
                                }}
                                return context.dataset.label + ": " +
                                    Number(value).toLocaleString() + " TEU";
                            }}
                        }}
                    }}
                }},
                scales: {{
                    x: {{
                        grid: {{
                            color: "rgba(255,255,255,0.05)"
                        }},
                        ticks: {{
                            color: "#8c9aad",
                            maxRotation: 0,
                            autoSkip: true,
                            maxTicksLimit: 8,
                            font: {{
                                family: "Consolas, monospace",
                                size: 10
                            }}
                        }}
                    }},
                    y: {{
                        grid: {{
                            color: "rgba(255,255,255,0.07)"
                        }},
                        ticks: {{
                            color: "#8c9aad",
                            font: {{
                                family: "Consolas, monospace",
                                size: 10
                            }},
                            callback: function(value) {{
                                return (value / 1000000).toFixed(1) + "M";
                            }}
                        }}
                    }}
                }}
            }}
        }});
    </script>
</body>
</html>
"""

    HTML_PATH.write_text(html_content, encoding="utf-8")

    print(
        f"[Dashboard] Generated: {HTML_PATH} | "
        f"latest={latest_date} | history={len(completed_dates)} snapshots"
    )

# =============================================================================
# 8. Main
# =============================================================================

def main() -> None:
    print("=" * 72)
    print("[Linerlytica Module Execution Started]")
    print(f"Working Directory: {BASE_DIR}")
    print(f"Database Path: {DB_PATH}")
    print(f"Dashboard Path: {HTML_PATH}")
    print(f"Target URL: {TARGET_URL}")
    print(f"Backfill Start Date: {START_DATE_STR}")
    print("=" * 72)

    conn: Optional[sqlite3.Connection] = None

    try:
        conn = init_db()
        crawl_and_backfill(conn)
        generate_dashboard_html(conn)

    except KeyboardInterrupt:
        print("\n[Interrupted] 사용자에 의해 실행이 중단되었습니다.")

    except Exception as exc:
        print(f"\n[FATAL] {type(exc).__name__}: {exc}")
        raise

    finally:
        if conn is not None:
            conn.close()

        print("=" * 72)
        print("[Linerlytica Module Execution Completed]")
        print("=" * 72)

if __name__ == "__main__":
    main()
