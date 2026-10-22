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

환경변수
----------------------------------------------------------------
LINERLYTICA_URL
    기본값: https://www.linerlytica.com/

LINERLYTICA_START_DATE
    백필 시작일.
    기본값: 2024-01-17

LINERLYTICA_DATE_INPUT_SELECTOR
    날짜 입력 selector를 강제 지정할 때 사용.
    기본값: input.dx-texteditor-input 자동 사용

LINERLYTICA_DATE_INPUT_FORMAT
    날짜 입력 형식.
    기본값: %m/%d/%Y

LINERLYTICA_STORAGE_STATE
    로그인 세션이 필요할 경우 Playwright storage-state JSON 파일 경로.

LINERLYTICA_HEADLESS
    true / false
    기본값: true
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

# Linerlytica DevExtreme DateBox의 visible input 기본 selector
DATE_INPUT_SELECTOR = os.getenv(
    "LINERLYTICA_DATE_INPUT_SELECTOR",
    "input.dx-texteditor-input",
).strip()

# Linerlytica 영문 DevExtreme DateBox 표시 형식
DATE_INPUT_FORMAT = os.getenv(
    "LINERLYTICA_DATE_INPUT_FORMAT",
    "%m/%d/%Y",
).strip()

STORAGE_STATE_PATH = os.getenv("LINERLYTICA_STORAGE_STATE", "").strip()

HEADLESS = os.getenv("LINERLYTICA_HEADLESS", "true").strip().lower() not in {
    "false",
    "0",
    "no",
}

PAGE_TIMEOUT_MS = 60_000
DATE_CHANGE_WAIT_MS = 3_000
DATA_RENDER_WAIT_MS = 2_000

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
# 3. 예외 및 공통 유틸리티
# =============================================================================

class DateSelectionError(Exception):
    """웹사이트에서 목표 날짜를 선택하지 못한 경우 발생."""

class IncompleteSnapshotError(Exception):
    """필수 상태가 모두 수집되지 않은 경우 발생."""

def utc_now_string() -> str:
    """UTC 기준 수집 시각을 ISO 형식으로 반환한다."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def normalize_space(value: str) -> str:
    """연속 공백·개행을 단일 공백으로 표준화한다."""
    return re.sub(r"\s+", " ", value or "").strip()

def normalize_status(value: str) -> Optional[str]:
    """웹페이지 상태명을 SQLite 표준 상태명으로 변환한다."""
    return STATUS_ALIASES.get(normalize_space(value).lower())

def parse_integer(value: str) -> Optional[int]:
    """
    문자열에서 정수값을 안전하게 추출한다.

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
    """시작일과 종료일 사이의 매월 17일 목록을 반환한다."""
    result: List[str] = []

    year = start_date.year
    month = start_date.month

    while True:
        target = date(year, month, 17)

        if start_date <= target <= end_date:
            result.append(target.isoformat())

        if year == end_date.year and month == end_date.month:
            break

        month += 1

        if month == 13:
            month = 1
            year += 1

    return result

def safe_percent_change(
    current: Optional[int],
    previous: Optional[int],
) -> Optional[float]:
    """직전 값 대비 증감률을 계산한다."""
    if current is None or previous in (None, 0):
        return None

    return ((current - previous) / previous) * 100

def format_number(value: Optional[int]) -> str:
    """숫자를 천 단위 콤마 문자열로 표시한다."""
    return "-" if value is None else f"{value:,}"

def format_delta(
    current: Optional[int],
    previous: Optional[int],
) -> str:
    """증감 수치와 증감률을 HTML 문자열로 반환한다."""
    if current is None or previous is None:
        return '<span class="neutral">-</span>'

    delta = current - previous
    pct = safe_percent_change(current, previous)
    pct_text = f"{pct:+.1f}%" if pct is not None else "-"

    if delta > 0:
        return f'<span class="up">↗ +{delta:,} ({pct_text})</span>'

    if delta < 0:
        return f'<span class="down">↘ {delta:,} ({pct_text})</span>'

    return '<span class="neutral">→ 0 (0.0%)</span>'

# =============================================================================
# 4. SQLite 관리
# =============================================================================

def init_db() -> sqlite3.Connection:
    """SQLite DB 및 테이블을 초기화한다."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    conn.execute(
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

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_fleet_snapshot_date
        ON fleet_snapshot(snapshot_date)
        """
    )

    conn.commit()

    return conn

def get_completed_dates(conn: sqlite3.Connection) -> Set[str]:
    """필수 5개 상태가 모두 존재하는 날짜만 완료된 스냅샷으로 반환한다."""
    rows = conn.execute(
        """
        SELECT snapshot_date, ship_status
        FROM fleet_snapshot
        """
    ).fetchall()

    status_by_date: Dict[str, Set[str]] = {}

    for row in rows:
        status_by_date.setdefault(row["snapshot_date"], set()).add(
            row["ship_status"]
        )

    required = set(REQUIRED_STATUSES)

    return {
        snapshot_date
        for snapshot_date, statuses in status_by_date.items()
        if required.issubset(statuses)
    }

def delete_snapshot_date(conn: sqlite3.Connection, snapshot_date: str) -> None:
    """지정 날짜의 기존 스냅샷을 삭제한다."""
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
    """월별 5개 상태 데이터를 저장한다."""
    found_statuses = {row["ship_status"] for row in data_rows}
    missing_statuses = set(REQUIRED_STATUSES) - found_statuses

    if missing_statuses:
        raise IncompleteSnapshotError(
            f"{snapshot_date} 저장 불가. 누락 상태: "
            f"{', '.join(sorted(missing_statuses))}"
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
        f"{len(data_rows)} statuses | collected_at={collected_at}"
    )

# =============================================================================
# 5. HTML / 화면 텍스트 데이터 파싱
# =============================================================================

def get_candidate_tables(soup: BeautifulSoup) -> List:
    """Fleet Snapshot 제목 주변 테이블을 우선 탐색한다."""
    heading_pattern = re.compile(
        r"global\s+containership\s+fleet\s+snapshot",
        re.IGNORECASE,
    )

    heading = soup.find(
        lambda tag: (
            tag
            and tag.name in {"h1", "h2", "h3", "h4", "h5", "div", "span", "p"}
            and heading_pattern.search(
                normalize_space(tag.get_text(" ", strip=True))
            )
        )
    )

    candidates = []

    if heading:
        parent = heading.parent

        if parent:
            candidates.extend(parent.find_all("table"))

        for sibling in heading.find_all_next(limit=100):
            if sibling.name == "table":
                candidates.append(sibling)
                break

    return candidates or soup.find_all("table")

def get_header_map(table) -> Dict[str, int]:
    """테이블 헤더에서 Status / Ships / TEU 열 위치를 탐색한다."""
    header_map: Dict[str, int] = {}
    header_row = table.find("tr")

    if not header_row:
        return header_map

    cells = header_row.find_all(["th", "td"])

    for index, cell in enumerate(cells):
        text = normalize_space(cell.get_text(" ", strip=True)).lower()

        if "status" in text:
            header_map["status"] = index
        elif text == "ships" or text.startswith("ships"):
            header_map["ships"] = index
        elif "teu" in text:
            header_map["teu"] = index

    return header_map

def parse_snapshot_table_html(html_text: str) -> List[Dict[str, int]]:
    """전통적 HTML table 구조에서 Fleet Snapshot 데이터를 파싱한다."""
    soup = BeautifulSoup(html_text, "html.parser")
    parsed: Dict[str, Dict[str, int]] = {}

    for table in get_candidate_tables(soup):
        table_text = normalize_space(table.get_text(" ", strip=True)).lower()

        if "teu" not in table_text or "ships" not in table_text:
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
                {"status", "ships", "teu"}.issubset(header_map)
                and max(header_map.values()) < len(cells)
            ):
                status = normalize_status(cells[header_map["status"]])
                ships = parse_integer(cells[header_map["ships"]])
                teu = parse_integer(cells[header_map["teu"]])
            else:
                status = normalize_status(cells[0])
                ships = parse_integer(cells[1])
                teu = parse_integer(cells[2])

            if status and ships is not None and teu is not None:
                parsed[status] = {
                    "ship_status": status,
                    "ships": ships,
                    "teu": teu,
                }

    return [parsed[s] for s in REQUIRED_STATUSES if s in parsed]

def parse_snapshot_from_text(page_text: str) -> List[Dict[str, int]]:
    """
    DevExtreme Grid처럼 HTML table의 td 텍스트가 비어 있는 경우를 위한
    화면 텍스트 기반 fallback 파서.

    예상 화면 텍스트 예:
        Active Ships 5,751 24,488,025
        Inactive Ships 80 365,000
    """
    text = normalize_space(page_text)
    parsed: Dict[str, Dict[str, int]] = {}

    status_patterns = {
        "Ships at port": r"ships\s+at\s+port(?:s)?",
        "Active Ships": r"active\s+ships",
        "Inactive Ships": r"inactive\s+ships",
        "Ships at anchorage": r"ships\s+at\s+anchorage",
        "Ships in shipyard": r"ships\s+in\s+shipyard(?:s)?",
    }

    found_positions: List[Tuple[int, str]] = []

    for status, pattern in status_patterns.items():
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            found_positions.append((match.start(), status))

    found_positions.sort(key=lambda item: item[0])

    for index, (start_position, status) in enumerate(found_positions):
        end_position = (
            found_positions[index + 1][0]
            if index + 1 < len(found_positions)
            else min(len(text), start_position + 250)
        )

        segment = text[start_position:end_position]

        number_tokens = re.findall(
            r"(?<![\d,])\d{1,3}(?:,\d{3})+(?![\d,])|(?<![\d,])\d+(?![\d,])",
            segment,
        )

        numeric_values = []

        for token in number_tokens:
            number = parse_integer(token)

            if number is not None:
                numeric_values.append(number)

        # 첫 번째 숫자 = Ships, 두 번째 숫자 = TEU라는 화면 구조를 가정.
        # 날짜나 다른 숫자가 앞에 존재하는 경우를 완화하기 위해
        # TEU 후보는 일반적으로 Ships보다 큰 숫자를 선택한다.
        if len(numeric_values) < 2:
            continue

        ships = numeric_values[0]
        teu = numeric_values[1]

        for candidate in numeric_values[1:]:
            if candidate >= ships:
                teu = candidate
                break

        # 비현실적인 값 및 헤더 오인식 방지
        if ships <= 0 or teu <= 0:
            continue

        parsed[status] = {
            "ship_status": status,
            "ships": ships,
            "teu": teu,
        }

    return [parsed[s] for s in REQUIRED_STATUSES if s in parsed]

def parse_snapshot_data(
    html_text: str,
    page_text: str,
) -> List[Dict[str, int]]:
    """HTML table 파싱 후 부족한 경우 화면 텍스트 fallback을 수행한다."""
    table_rows = parse_snapshot_table_html(html_text)

    table_statuses = {row["ship_status"] for row in table_rows}

    if set(REQUIRED_STATUSES).issubset(table_statuses):
        return table_rows

    text_rows = parse_snapshot_from_text(page_text)
    merged: Dict[str, Dict[str, int]] = {
        row["ship_status"]: row
        for row in table_rows
    }

    for row in text_rows:
        merged[row["ship_status"]] = row

    return [
        merged[status]
        for status in REQUIRED_STATUSES
        if status in merged
    ]

# =============================================================================
# 6. Playwright 날짜 선택 및 크롤링
# =============================================================================

def dismiss_cookie_banner(page: Page) -> None:
    """초기 Cookie Consent 배너를 닫는다."""
    try:
        button = page.locator("#rcc-confirm-button")

        if button.count() > 0 and button.is_visible():
            button.click(timeout=5_000)
            page.wait_for_timeout(500)
            print("  [Cookie] Consent banner accepted.")

    except Exception as exc:
        print(f"  [Cookie] Banner handling skipped: {exc}")

def candidate_date_selectors() -> List[str]:
    """DevExtreme DateBox selector를 우선으로 날짜 입력 후보를 반환한다."""
    selectors = []

    if DATE_INPUT_SELECTOR:
        selectors.append(DATE_INPUT_SELECTOR)

    selectors.extend(
        [
            "input.dx-texteditor-input",
            "input[type='date']",
            "input[name='date']",
            "input[name='snapshot_date']",
            "input[name='data_date']",
            "input[id*='date' i]",
            "input[class*='date' i]",
            "input[placeholder*='date' i]",
        ]
    )

    seen = set()
    result = []

    for selector in selectors:
        if selector and selector not in seen:
            seen.add(selector)
            result.append(selector)

    return result

def get_hidden_input_values(page: Page) -> List[Optional[str]]:
    """현재 페이지의 hidden input value 목록을 반환한다."""
    values: List[Optional[str]] = []
    hidden_inputs = page.locator("input[type='hidden']")

    for index in range(hidden_inputs.count()):
        try:
            values.append(hidden_inputs.nth(index).get_attribute("value"))
        except PlaywrightError:
            continue

    return values

def wait_for_loading_complete(page: Page) -> None:
    """페이지의 Loading... 문구가 사라질 때까지 대기한다."""
    try:
        page.wait_for_function(
            """
            () => !document.body.innerText.includes('Loading...')
            """,
            timeout=20_000,
        )
    except PlaywrightTimeoutError:
        print("  [Warning] Loading... 문구가 대기 시간 내 남아 있습니다.")

def select_target_date(page: Page, target_date: str) -> str:
    """
    Linerlytica DevExtreme DateBox에 목표 날짜를 입력한다.

    검증 기준:
    1. visible input 값
    2. hidden input 중 YYYY-MM-DD 목표 날짜 존재 여부
    """
    target_dt = datetime.strptime(target_date, "%Y-%m-%d")
    formatted_date = target_dt.strftime(DATE_INPUT_FORMAT)

    errors = []

    for selector in candidate_date_selectors():
        try:
            locator = page.locator(selector).first

            if locator.count() == 0:
                continue

            locator.wait_for(state="visible", timeout=15_000)

            try:
                locator.scroll_into_view_if_needed(timeout=5_000)
            except PlaywrightError:
                pass

            locator.click(timeout=8_000)
            locator.press("Control+A", timeout=5_000)

            try:
                locator.fill(formatted_date, timeout=8_000)
            except PlaywrightError:
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

            # DevExtreme DateBox는 Enter 또는 blur 시점에 내부 value를 확정한다.
            try:
                locator.press("Enter", timeout=5_000)
            except PlaywrightError:
                pass

            try:
                locator.press("Tab", timeout=5_000)
            except PlaywrightError:
                pass

            page.wait_for_timeout(DATE_CHANGE_WAIT_MS)

            hidden_values = get_hidden_input_values(page)
            visible_value = ""

            try:
                visible_value = locator.input_value(timeout=5_000).strip()
            except PlaywrightError:
                pass

            valid_visible_values = {
                target_date,
                formatted_date,
                target_dt.strftime("%Y-%m-%d"),
            }

            hidden_date_matched = target_date in hidden_values
            visible_date_matched = visible_value in valid_visible_values

            if not hidden_date_matched and not visible_date_matched:
                raise DateSelectionError(
                    f"날짜 값 검증 실패. "
                    f"visible={visible_value!r}, hidden={hidden_values}"
                )

            # 헤더가 목표 날짜로 바뀌는지 확인한다.
            expected_header_date = target_dt.strftime("%b %d, %Y").replace(
                " 0",
                " ",
            )

            try:
                page.get_by_text(
                    f"Global Containership Fleet Snapshot as of "
                    f"{expected_header_date}",
                    exact=False,
                ).first.wait_for(state="visible", timeout=15_000)
            except PlaywrightTimeoutError:
                print(
                    "  [Warning] Snapshot header의 날짜 문구를 "
                    "명시적으로 검증하지 못했습니다."
                )

            wait_for_loading_complete(page)
            page.wait_for_timeout(DATA_RENDER_WAIT_MS)

            return selector

        except Exception as exc:
            errors.append(f"{selector}: {exc}")

    raise DateSelectionError(
        f"날짜 선택 실패: {target_date}\n"
        f"시도 selector: {candidate_date_selectors()}\n"
        f"상세: {' | '.join(errors) if errors else 'No matching input found.'}"
    )

def crawl_one_snapshot(page: Page, target_date: str) -> List[Dict[str, int]]:
    """특정 날짜의 Fleet Snapshot을 수집한다."""
    page.goto(
        TARGET_URL,
        wait_until="domcontentloaded",
        timeout=PAGE_TIMEOUT_MS,
    )

    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except PlaywrightTimeoutError:
        pass

    dismiss_cookie_banner(page)
    wait_for_loading_complete(page)

    used_selector = select_target_date(page, target_date)

    print(
        f"  [Date Selected] {target_date} | "
        f"selector={used_selector}"
    )

    html_content = page.content()
    page_text = page.locator("body").inner_text(timeout=15_000)

    data_rows = parse_snapshot_data(
        html_text=html_content,
        page_text=page_text,
    )

    found_statuses = {row["ship_status"] for row in data_rows}
    missing_statuses = set(REQUIRED_STATUSES) - found_statuses

    if missing_statuses:
        preview = normalize_space(page_text)[:800]

        raise IncompleteSnapshotError(
            f"{target_date} 표 데이터 불완전. "
            f"수집 상태={sorted(found_statuses)} | "
            f"누락 상태={sorted(missing_statuses)} | "
            f"페이지 텍스트 미리보기={preview!r}"
        )

    return data_rows

def crawl_and_backfill(conn: sqlite3.Connection) -> None:
    """완전하지 않거나 누락된 월별 17일자 스냅샷을 수집한다."""
    try:
        start_date = datetime.strptime(
            START_DATE_STR,
            "%Y-%m-%d",
        ).date()
    except ValueError as exc:
        raise ValueError(
            "LINERLYTICA_START_DATE는 YYYY-MM-DD 형식이어야 합니다. "
            f"현재 값: {START_DATE_STR}"
        ) from exc

    current_date = datetime.now(timezone.utc).date()

    if current_date.day < 17:
        if current_date.month == 1:
            end_date = date(current_date.year - 1, 12, 17)
        else:
            end_date = date(current_date.year, current_date.month - 1, 17)
    else:
        end_date = current_date

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

    print(f"[Crawler] Missing / incomplete dates: {len(missing_dates)}")
    print(f"[Crawler] Targets: {missing_dates}")

    storage_state = None

    if STORAGE_STATE_PATH:
        candidate = Path(STORAGE_STATE_PATH)

        if not candidate.is_absolute():
            candidate = BASE_DIR.parent / candidate

        if candidate.exists():
            storage_state = str(candidate)
            print(f"[Crawler] Storage state: {storage_state}")
        else:
            print(f"[Warning] Storage state file not found: {candidate}")

    with sync_playwright() as playwright:
        try:
            browser: Browser = playwright.chromium.launch(headless=HEADLESS)
        except PlaywrightError as exc:
            raise RuntimeError(
                "Chromium을 실행할 수 없습니다.\n"
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
            "viewport": {"width": 1600, "height": 1200},
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
    """특정 날짜의 상태별 Ships / TEU 데이터를 조회한다."""
    rows = conn.execute(
        """
        SELECT ship_status, ships, teu
        FROM fleet_snapshot
        WHERE snapshot_date = ?
        """,
        (snapshot_date,),
    ).fetchall()

    return {
        row["ship_status"]: {
            "ships": row["ships"],
            "teu": row["teu"],
        }
        for row in rows
    }

def generate_dashboard_html(conn: sqlite3.Connection) -> None:
    """SQLite 이력을 기반으로 static index.html을 생성한다."""
    completed_dates = sorted(get_completed_dates(conn))

    if not completed_dates:
        print("[Dashboard] 완전한 스냅샷 데이터가 없어 HTML 생성을 건너뜁니다.")
        return

    latest_date = completed_dates[-1]
    previous_date = completed_dates[-2] if len(completed_dates) >= 2 else None

    latest_data = get_snapshot_data(conn, latest_date)
    previous_data = (
        get_snapshot_data(conn, previous_date)
        if previous_date
        else {}
    )

    rows = conn.execute(
        """
        SELECT snapshot_date, ship_status, ships, teu
        FROM fleet_snapshot
        ORDER BY snapshot_date, ship_status
        """
    ).fetchall()

    history_map = {
        (row["snapshot_date"], row["ship_status"]): {
            "ships": row["ships"],
            "teu": row["teu"],
        }
        for row in rows
    }

    teu_series: Dict[str, List[Optional[int]]] = {
        status: [] for status in REQUIRED_STATUSES
    }

    for snapshot_date in completed_dates:
        for status in REQUIRED_STATUSES:
            point = history_map.get((snapshot_date, status))
            teu_series[status].append(point["teu"] if point else None)

    table_rows_html = ""

    for status in REQUIRED_STATUSES:
        latest = latest_data.get(status, {})
        previous = previous_data.get(status, {})

        table_rows_html += f"""
        <tr>
            <td class="status">{html.escape(status)}</td>
            <td>{format_number(latest.get("ships"))}</td>
            <td>{format_delta(latest.get("ships"), previous.get("ships"))}</td>
            <td>{format_number(latest.get("teu"))}</td>
            <td>{format_delta(latest.get("teu"), previous.get("teu"))}</td>
        </tr>
        """

    active_latest = latest_data.get("Active Ships", {})
    active_previous = previous_data.get("Active Ships", {})

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Linerlytica | Fleet Snapshot Terminal</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.6/dist/chart.umd.min.js"></script>
<style>
:root {{
    --bg:#090d14; --panel:#111824; --panel2:#0d131d; --line:#293548;
    --text:#e7edf6; --muted:#8c9aad; --orange:#ff9d00; --green:#00d084;
    --red:#ff5c6c; --blue:#29b6f6; --yellow:#ffd166;
}}
* {{ box-sizing:border-box; }}
body {{
    margin:0; padding:24px; background:var(--bg); color:var(--text);
    font-family:Arial, Helvetica, sans-serif;
}}
.container {{ max-width:1600px; margin:0 auto; }}
.header {{
    display:flex; justify-content:space-between; align-items:flex-end; gap:20px;
    border-bottom:2px solid var(--orange); padding-bottom:15px; margin-bottom:20px;
}}
.title {{ font-size:23px; font-weight:800; letter-spacing:.5px; }}
.title::before {{
    content:""; display:inline-block; width:11px; height:11px;
    background:var(--orange); margin-right:10px;
}}
.subtitle, .meta, .footer {{
    color:var(--muted); font-family:Consolas, monospace; font-size:12px;
}}
.subtitle {{ margin-top:7px; }}
.meta {{ text-align:right; line-height:1.7; }}
.kpis {{
    display:grid; grid-template-columns:repeat(3, 1fr); gap:16px; margin-bottom:20px;
}}
.kpi, .card {{
    background:var(--panel); border:1px solid var(--line); padding:18px;
}}
.kpi {{ border-left:4px solid var(--blue); }}
.kpi-label {{
    color:var(--muted); font:11px Consolas, monospace; text-transform:uppercase;
}}
.kpi-value {{
    margin-top:11px; color:#fff; font:700 25px Consolas, monospace;
}}
.kpi-delta {{ margin-top:7px; font:11px Consolas, monospace; }}
.grid {{
    display:grid; grid-template-columns:minmax(650px,1.1fr) minmax(500px,1fr); gap:20px;
}}
.card-title {{
    color:var(--orange); display:flex; justify-content:space-between; margin-bottom:16px;
    font:700 13px Consolas, monospace; text-transform:uppercase;
}}
.card-date {{ color:var(--muted); font-weight:400; font-size:11px; }}
table {{ width:100%; border-collapse:collapse; font:12px Consolas, monospace; }}
th {{
    background:var(--panel2); color:var(--muted); padding:10px; text-align:right;
    border-bottom:1px solid var(--line);
}}
th:first-child {{ text-align:left; }}
td {{ padding:12px 10px; text-align:right; border-bottom:1px solid rgba(255,255,255,.06); }}
td:first-child {{ text-align:left; }}
.status {{ color:#fff; font-weight:700; }}
.up {{ color:var(--green); }} .down {{ color:var(--red); }} .neutral {{ color:var(--muted); }}
.chart-wrap {{ height:325px; position:relative; }}
.footer {{ margin-top:18px; font-size:11px; }}
@media(max-width:1100px) {{ .grid {{ grid-template-columns:1fr; }} }}
@media(max-width:720px) {{
    body {{ padding:14px; }} .header {{ align-items:flex-start; flex-direction:column; }}
    .meta {{ text-align:left; }} .kpis {{ grid-template-columns:1fr; }}
    .table-wrap {{ overflow-x:auto; }}
}}
</style>
</head>
<body>
<div class="container">
    <header class="header">
        <div>
            <div class="title">LINERLYTICA FLEET SNAPSHOT</div>
            <div class="subtitle">GLOBAL CONTAINERSHIP FLEET STATUS | MONTHLY 17TH HISTORY</div>
        </div>
        <div class="meta">
            LATEST SNAPSHOT: <strong>{html.escape(latest_date)}</strong><br>
            PREVIOUS SNAPSHOT: <strong>{html.escape(previous_date or "N/A")}</strong>
        </div>
    </header>

    <section class="kpis">
        <div class="kpi">
            <div class="kpi-label">Active Ships</div>
            <div class="kpi-value">{format_number(active_latest.get("ships"))} Ships</div>
            <div class="kpi-delta">{format_delta(active_latest.get("ships"), active_previous.get("ships"))}</div>
        </div>
        <div class="kpi">
            <div class="kpi-label">Active Capacity</div>
            <div class="kpi-value">{format_number(active_latest.get("teu"))} TEU</div>
            <div class="kpi-delta">{format_delta(active_latest.get("teu"), active_previous.get("teu"))}</div>
        </div>
        <div class="kpi">
            <div class="kpi-label">Historical Snapshots</div>
            <div class="kpi-value">{len(completed_dates)} Months</div>
            <div class="kpi-delta"><span class="neutral">Monthly 17th records only</span></div>
        </div>
    </section>

    <main class="grid">
        <section class="card">
            <div class="card-title">
                <span>Current Fleet Status</span>
                <span class="card-date">AS OF {html.escape(latest_date)}</span>
            </div>
            <div class="table-wrap">
                <table>
                    <thead>
                        <tr>
                            <th>Ship Status</th>
                            <th>Ships</th>
                            <th>vs Previous</th>
                            <th>TEU</th>
                            <th>vs Previous</th>
                        </tr>
                    </thead>
                    <tbody>{table_rows_html}</tbody>
                </table>
            </div>
        </section>

        <section class="card">
            <div class="card-title">
                <span>Capacity Trend</span>
                <span class="card-date">TEU</span>
            </div>
            <div class="chart-wrap"><canvas id="teuChart"></canvas></div>
        </section>
    </main>

    <div class="footer">
        SOURCE: LINERLYTICA | DATA POLICY: MONTHLY SNAPSHOT ON THE 17TH |
        GENERATED FROM Linerlytica.db
    </div>
</div>

<script>
const labels = {json.dumps(completed_dates, ensure_ascii=False)};
new Chart(document.getElementById("teuChart"), {{
    type: "line",
    data: {{
        labels: labels,
        datasets: [
            {{
                label: "Active Ships",
                data: {json.dumps(teu_series["Active Ships"])},
                borderColor:"#00d084", borderWidth:2.2, tension:.2, pointRadius:2
            }},
            {{
                label: "Ships at Port",
                data: {json.dumps(teu_series["Ships at port"])},
                borderColor:"#ff9d00", borderWidth:1.7, tension:.2, pointRadius:1.5
            }},
            {{
                label: "Ships at Anchorage",
                data: {json.dumps(teu_series["Ships at anchorage"])},
                borderColor:"#29b6f6", borderWidth:1.7, tension:.2, pointRadius:1.5
            }},
            {{
                label: "Inactive Ships",
                data: {json.dumps(teu_series["Inactive Ships"])},
                borderColor:"#ff5c6c", borderWidth:1.5, tension:.2, pointRadius:1.5
            }},
            {{
                label: "Ships in Shipyard",
                data: {json.dumps(teu_series["Ships in shipyard"])},
                borderColor:"#ffd166", borderWidth:1.5, tension:.2, pointRadius:1.5
            }}
        ]
    }},
    options: {{
        responsive:true,
        maintainAspectRatio:false,
        interaction:{{ mode:"index", intersect:false }},
        plugins:{{
            legend:{{
                position:"bottom",
                labels:{{ color:"#dce4f0", boxWidth:12, padding:14 }}
            }},
            tooltip:{{
                backgroundColor:"#111824",
                borderColor:"#293548",
                borderWidth:1,
                callbacks:{{
                    label:function(context) {{
                        return context.dataset.label + ": " +
                            Number(context.raw).toLocaleString() + " TEU";
                    }}
                }}
            }}
        }},
        scales:{{
            x:{{
                grid:{{ color:"rgba(255,255,255,.05)" }},
                ticks:{{ color:"#8c9aad", maxRotation:0, maxTicksLimit:8 }}
            }},
            y:{{
                grid:{{ color:"rgba(255,255,255,.07)" }},
                ticks:{{
                    color:"#8c9aad",
                    callback:function(value) {{
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
    print(f"Date Input Selector: {DATE_INPUT_SELECTOR}")
    print(f"Date Input Format: {DATE_INPUT_FORMAT}")
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
