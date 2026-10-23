#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Linerlytica Global Containership Fleet Snapshot Crawler & Dashboard Generator

기능
----------------------------------------------------------------
1. Linerlytica Global Containership Fleet Snapshot을 수집한다.
2. 매월 17일자 데이터만 SQLite에 누적 저장한다.
3. 누락 또는 불완전한 월은 Backfill 방식으로 재수집한다.
4. 필수 상태 5개가 모두 확보된 경우에만 DB에 저장한다.
5. SQLite 이력을 기반으로 Linerlytica/index.html을 생성한다.

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
    날짜 입력 selector.
    기본값: input.dx-texteditor-input

LINERLYTICA_DATE_INPUT_FORMAT
    날짜 입력 표시 형식.
    기본값: %m/%d/%Y

LINERLYTICA_STORAGE_STATE
    로그인 세션이 필요할 때 Playwright storage state JSON 파일 경로.

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

DATE_INPUT_SELECTOR = os.getenv(
    "LINERLYTICA_DATE_INPUT_SELECTOR",
    "input.dx-texteditor-input",
).strip()

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
# 2. 상태 정의
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
# 3. 예외 및 유틸리티
# =============================================================================

class DateSelectionError(Exception):
    """웹페이지에서 목표 날짜 선택에 실패한 경우."""

class IncompleteSnapshotError(Exception):
    """필수 Fleet Snapshot 상태가 모두 확보되지 않은 경우."""

def utc_now_string() -> str:
    """UTC 기준 현재 시간을 문자열로 반환한다."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def normalize_space(value: str) -> str:
    """연속 공백, 줄바꿈, 탭을 단일 공백으로 표준화한다."""
    return re.sub(r"\s+", " ", value or "").strip()

def normalize_status(value: str) -> Optional[str]:
    """페이지 원문 상태명을 표준 상태명으로 변환한다."""
    return STATUS_ALIASES.get(normalize_space(value).lower())

def parse_integer(value: str) -> Optional[int]:
    """콤마 포함 숫자 문자열을 int로 변환한다."""
    if value is None:
        return None

    cleaned = re.sub(r"[^\d]", "", str(value))

    if not cleaned:
        return None

    try:
        return int(cleaned)
    except ValueError:
        return None

def format_number(value: Optional[int]) -> str:
    """숫자를 천 단위 구분자 형식으로 표시한다."""
    return "-" if value is None else f"{value:,}"

def safe_percent_change(
    current: Optional[int],
    previous: Optional[int],
) -> Optional[float]:
    """직전 값 대비 증감률을 계산한다."""
    if current is None or previous in (None, 0):
        return None

    return ((current - previous) / previous) * 100

def format_delta(
    current: Optional[int],
    previous: Optional[int],
) -> str:
    """현재값과 이전값의 증감 HTML을 생성한다."""
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

def month_17th_dates(start_date: date, end_date: date) -> List[str]:
    """시작일부터 종료일까지 매월 17일 목록을 반환한다."""
    results: List[str] = []
    year = start_date.year
    month = start_date.month

    while True:
        candidate = date(year, month, 17)

        if start_date <= candidate <= end_date:
            results.append(candidate.isoformat())

        if year == end_date.year and month == end_date.month:
            break

        month += 1

        if month == 13:
            year += 1
            month = 1

    return results

# =============================================================================
# 4. SQLite 관리
# =============================================================================

def init_db() -> sqlite3.Connection:
    """SQLite 데이터베이스 및 테이블을 초기화한다."""
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
    """필수 5개 상태가 모두 존재하는 완료 스냅샷 날짜를 조회한다."""
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

def save_snapshot(
    conn: sqlite3.Connection,
    snapshot_date: str,
    data_rows: List[Dict[str, int]],
    source_url: str,
) -> None:
    """검증된 월별 Fleet Snapshot을 저장한다."""
    statuses = {row["ship_status"] for row in data_rows}
    missing = set(REQUIRED_STATUSES) - statuses

    if missing:
        raise IncompleteSnapshotError(
            f"{snapshot_date} DB 저장 불가. "
            f"누락 상태: {', '.join(sorted(missing))}"
        )

    collected_at = utc_now_string()

    with conn:
        conn.execute(
            """
            DELETE FROM fleet_snapshot
            WHERE snapshot_date = ?
            """,
            (snapshot_date,),
        )

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
# 5. 데이터 파싱
# =============================================================================

def parse_snapshot_table_html(html_text: str) -> List[Dict[str, int]]:
    """일반 HTML table 구조에서 Fleet Snapshot 데이터를 파싱한다."""
    soup = BeautifulSoup(html_text, "html.parser")
    parsed: Dict[str, Dict[str, int]] = {}

    for table in soup.find_all("table"):
        table_text = normalize_space(table.get_text(" ", strip=True)).lower()

        if "ship status" not in table_text:
            continue

        if "ships" not in table_text or "teu" not in table_text:
            continue

        for tr in table.find_all("tr"):
            cells = [
                normalize_space(cell.get_text(" ", strip=True))
                for cell in tr.find_all(["td", "th"])
            ]

            if len(cells) < 3:
                continue

            status = normalize_status(cells[0])
            ships = parse_integer(cells[1])
            teu = parse_integer(cells[2])

            if status and ships is not None and teu is not None:
                parsed[status] = {
                    "ship_status": status,
                    "ships": ships,
                    "teu": teu,
                }

    return [
        parsed[status]
        for status in REQUIRED_STATUSES
        if status in parsed
    ]

def extract_snapshot_section(page_text: str) -> str:
    """
    화면 전체 텍스트에서 Global Containership Fleet Snapshot 구간만 추출한다.

    Port Congestion 표에도 Ships / TEU 숫자가 반복되므로, Fleet Snapshot과
    다음 섹션인 Global Containership Port Congestion 사이만 사용한다.
    """
    text = normalize_space(page_text)

    start_match = re.search(
        r"Global\s+Containership\s+Fleet\s+Snapshot\s+as\s+of",
        text,
        flags=re.IGNORECASE,
    )

    if not start_match:
        return text

    section = text[start_match.start():]

    end_match = re.search(
        r"Global\s+Containership\s+Port\s+Congestion\s+as\s+of",
        section,
        flags=re.IGNORECASE,
    )

    if end_match:
        section = section[:end_match.start()]

    return normalize_space(section)

def parse_snapshot_from_text(page_text: str) -> List[Dict[str, int]]:
    """
    DevExtreme Grid 화면 텍스트에서 Fleet Snapshot 데이터를 파싱한다.

    핵심 수정:
    ----------------------------------------------------------------
    기존 r"active\\s+ships" 패턴은 "Inactive Ships"의 일부 문자열인
    "active ships"를 매칭할 수 있다.

    따라서 Active Ships는 (?<!in)active와 word boundary를 사용하여
    Inactive Ships와 명확히 분리한다.

    실제 대상 텍스트 예:
        Ship Status Ships TEU Total TEU
        Ships at port 1,129 5,639,764
        Active Ships 5,751 24,488,025
        Inactive Ships 74 177,760
        Ships at anchorage 968 4,182,132
        Ships in shipyard 235 832,537
    """
    snapshot_text = extract_snapshot_section(page_text)
    parsed: Dict[str, Dict[str, int]] = {}

    status_patterns = {
        "Ships at port": r"\bships\s+at\s+port(?:s)?\b",
        # (?<!in)은 "Inactive Ships" 내부의 active 매칭 방지
        "Active Ships": r"(?<!in)\bactive\s+ships\b",
        "Inactive Ships": r"\binactive\s+ships\b",
        "Ships at anchorage": r"\bships\s+at\s+anchorage\b",
        "Ships in shipyard": r"\bships\s+in\s+shipyard(?:s)?\b",
    }

    status_matches: List[Tuple[int, int, str]] = []

    for status, pattern in status_patterns.items():
        match = re.search(pattern, snapshot_text, flags=re.IGNORECASE)

        if match:
            status_matches.append((match.start(), match.end(), status))

    status_matches.sort(key=lambda item: item[0])

    for index, (_, match_end, status) in enumerate(status_matches):
        next_start = (
            status_matches[index + 1][0]
            if index + 1 < len(status_matches)
            else len(snapshot_text)
        )

        # 현재 상태명 종료 직후부터 다음 상태명 직전까지가 숫자 영역이다.
        value_segment = snapshot_text[match_end:next_start]

        number_tokens = re.findall(
            r"(?<![\d,])\d{1,3}(?:,\d{3})+(?![\d,])"
            r"|(?<![\d,])\d+(?![\d,])",
            value_segment,
        )

        values: List[int] = []

        for token in number_tokens:
            number = parse_integer(token)

            if number is not None:
                values.append(number)

        if len(values) < 2:
            continue

        ships = values[0]
        teu = values[1]

        if ships <= 0 or teu <= 0:
            continue

        parsed[status] = {
            "ship_status": status,
            "ships": ships,
            "teu": teu,
        }

    return [
        parsed[status]
        for status in REQUIRED_STATUSES
        if status in parsed
    ]

def parse_snapshot_data(
    html_text: str,
    page_text: str,
) -> List[Dict[str, int]]:
    """HTML table 우선, 화면 텍스트 fallback 순으로 데이터를 파싱한다."""
    html_rows = parse_snapshot_table_html(html_text)
    html_statuses = {row["ship_status"] for row in html_rows}

    if set(REQUIRED_STATUSES).issubset(html_statuses):
        return html_rows

    text_rows = parse_snapshot_from_text(page_text)

    merged: Dict[str, Dict[str, int]] = {
        row["ship_status"]: row
        for row in html_rows
    }

    for row in text_rows:
        merged[row["ship_status"]] = row

    return [
        merged[status]
        for status in REQUIRED_STATUSES
        if status in merged
    ]

# =============================================================================
# 6. Playwright 제어
# =============================================================================

def dismiss_cookie_banner(page: Page) -> None:
    """Cookie Consent 배너를 닫는다."""
    try:
        button = page.locator("#rcc-confirm-button")

        if button.count() > 0 and button.is_visible():
            button.click(timeout=5_000)
            page.wait_for_timeout(500)
            print("  [Cookie] Consent banner accepted.")

    except Exception as exc:
        print(f"  [Cookie] Banner handling skipped: {exc}")

def wait_for_loading_complete(page: Page) -> None:
    """Loading... 문구가 사라질 때까지 대기한다."""
    try:
        page.wait_for_function(
            """
            () => !document.body.innerText.includes('Loading...')
            """,
            timeout=20_000,
        )
    except PlaywrightTimeoutError:
        print("  [Warning] Loading... 문구가 남아 있어 계속 진행합니다.")

def candidate_date_selectors() -> List[str]:
    """날짜 입력 후보 selector 목록을 반환한다."""
    candidates = [
        DATE_INPUT_SELECTOR,
        "input.dx-texteditor-input",
        "input[type='date']",
        "input[name='date']",
        "input[name='snapshot_date']",
        "input[name='data_date']",
        "input[id*='date' i]",
        "input[class*='date' i]",
    ]

    seen = set()
    result = []

    for selector in candidates:
        if selector and selector not in seen:
            seen.add(selector)
            result.append(selector)

    return result

def get_hidden_values(page: Page) -> List[str]:
    """모든 hidden input의 value 목록을 반환한다."""
    values: List[str] = []
    locator = page.locator("input[type='hidden']")

    for index in range(locator.count()):
        try:
            value = locator.nth(index).get_attribute("value")

            if value:
                values.append(value.strip())
        except PlaywrightError:
            continue

    return values

def select_target_date(page: Page, target_date: str) -> str:
    """DevExtreme DateBox에 목표 날짜를 입력하고 변경 여부를 검증한다."""
    target_dt = datetime.strptime(target_date, "%Y-%m-%d")
    displayed_date = target_dt.strftime(DATE_INPUT_FORMAT)

    errors: List[str] = []

    for selector in candidate_date_selectors():
        try:
            locator = page.locator(selector).first

            if locator.count() == 0:
                continue

            locator.wait_for(state="visible", timeout=15_000)
            locator.scroll_into_view_if_needed(timeout=5_000)
            locator.click(timeout=8_000)
            locator.press("Control+A", timeout=5_000)

            try:
                locator.fill(displayed_date, timeout=8_000)
            except PlaywrightError:
                locator.evaluate(
                    """
                    (element, value) => {
                        const setter = Object.getOwnPropertyDescriptor(
                            HTMLInputElement.prototype,
                            "value"
                        ).set;

                        setter.call(element, value);

                        element.dispatchEvent(
                            new Event("input", { bubbles: true })
                        );
                        element.dispatchEvent(
                            new Event("change", { bubbles: true })
                        );
                        element.dispatchEvent(
                            new Event("blur", { bubbles: true })
                        );
                    }
                    """,
                    displayed_date,
                )

            try:
                locator.press("Enter", timeout=5_000)
            except PlaywrightError:
                pass

            try:
                locator.press("Tab", timeout=5_000)
            except PlaywrightError:
                pass

            page.wait_for_timeout(DATE_CHANGE_WAIT_MS)
            wait_for_loading_complete(page)
            page.wait_for_timeout(DATA_RENDER_WAIT_MS)

            visible_value = ""

            try:
                visible_value = locator.input_value(timeout=5_000).strip()
            except PlaywrightError:
                pass

            hidden_values = get_hidden_values(page)

            allowed_visible_values = {
                target_date,
                displayed_date,
                target_dt.strftime("%Y-%m-%d"),
            }

            if (
                visible_value not in allowed_visible_values
                and target_date not in hidden_values
            ):
                raise DateSelectionError(
                    f"날짜 변경 검증 실패: "
                    f"visible={visible_value!r}, "
                    f"hidden={hidden_values}"
                )

            return selector

        except Exception as exc:
            errors.append(f"{selector}: {exc}")

    raise DateSelectionError(
        f"날짜 선택 실패: {target_date}\n"
        f"시도 selector: {candidate_date_selectors()}\n"
        f"상세: {' | '.join(errors)}"
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

    selector = select_target_date(page, target_date)

    print(f"  [Date Selected] {target_date} | selector={selector}")

    html_text = page.content()
    page_text = page.locator("body").inner_text(timeout=15_000)

    data_rows = parse_snapshot_data(
        html_text=html_text,
        page_text=page_text,
    )

    found = {row["ship_status"] for row in data_rows}
    missing = set(REQUIRED_STATUSES) - found

    if missing:
        snapshot_preview = extract_snapshot_section(page_text)[:900]

        raise IncompleteSnapshotError(
            f"{target_date} 표 데이터 불완전. "
            f"수집 상태={sorted(found)} | "
            f"누락 상태={sorted(missing)} | "
            f"Snapshot 미리보기={snapshot_preview!r}"
        )

    return data_rows

def crawl_and_backfill(conn: sqlite3.Connection) -> None:
    """DB에서 누락되거나 불완전한 월별 17일자 데이터를 수집한다."""
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

    today = datetime.now(timezone.utc).date()

    if today.day < 17:
        if today.month == 1:
            end_date = date(today.year - 1, 12, 17)
        else:
            end_date = date(today.year, today.month - 1, 17)
    else:
        end_date = today

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
        state_path = Path(STORAGE_STATE_PATH)

        if not state_path.is_absolute():
            state_path = BASE_DIR.parent / state_path

        if state_path.exists():
            storage_state = str(state_path)
            print(f"[Crawler] Using storage state: {storage_state}")
        else:
            print(f"[Warning] Storage state file not found: {state_path}")

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
    """특정 스냅샷 날짜의 상태별 데이터를 조회한다."""
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
    """SQLite 데이터를 기반으로 정적 HTML 대시보드를 생성한다."""
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

    ships_series: Dict[str, List[Optional[int]]] = {
        status: [] for status in REQUIRED_STATUSES
    }

    for snapshot_date in completed_dates:
        for status in REQUIRED_STATUSES:
            data = history_map.get((snapshot_date, status))

            teu_series[status].append(
                data["teu"] if data else None
            )

            ships_series[status].append(
                data["ships"] if data else None
            )

    sorted_statuses = sorted(
        REQUIRED_STATUSES,
        key=lambda status: latest_data.get(status, {}).get("ships", 0),
        reverse=True,
    )

    table_rows = ""

    for status in sorted_statuses:
        latest = latest_data.get(status, {})
        previous = previous_data.get(status, {})

        table_rows += f"""
        <tr>
            <td class="status">{html.escape(status)}</td>
            <td class="number">{format_number(latest.get("ships"))}</td>
            <td class="number">{format_number(latest.get("teu"))}</td>
            <td class="number">
                {format_delta(latest.get("ships"), previous.get("ships"))}
            </td>
        </tr>
        """

    ticker_items = ""

    for status in REQUIRED_STATUSES:
        latest = latest_data.get(status, {})
        previous = previous_data.get(status, {})

        ships = latest.get("ships")
        prior_ships = previous.get("ships")

        delta = (
            ships - prior_ships
            if ships is not None and prior_ships is not None
            else None
        )

        if delta is None:
            delta_class = "neutral"
            delta_text = "N/A"
        elif delta > 0:
            delta_class = "up"
            delta_text = f"↗ +{delta:,}"
        elif delta < 0:
            delta_class = "down"
            delta_text = f"↘ {delta:,}"
        else:
            delta_class = "neutral"
            delta_text = "→ 0"

        ticker_items += f"""
        <div class="ticker-item">
            <span class="ticker-label">{html.escape(status)}</span>
            <span class="ticker-value">{format_number(ships)}</span>
            <span class="ticker-delta {delta_class}">{delta_text}</span>
        </div>
        """

    active_latest = latest_data.get("Active Ships", {})
    active_previous = previous_data.get("Active Ships", {})

    port_latest = latest_data.get("Ships at port", {})
    port_previous = previous_data.get("Ships at port", {})

    anchorage_latest = latest_data.get("Ships at anchorage", {})
    anchorage_previous = previous_data.get("Ships at anchorage", {})

    inactive_latest = latest_data.get("Inactive Ships", {})
    inactive_previous = previous_data.get("Inactive Ships", {})

    shipyard_latest = latest_data.get("Ships in shipyard", {})
    shipyard_previous = previous_data.get("Ships in shipyard", {})

    total_latest_ships = sum(
        latest_data.get(status, {}).get("ships", 0)
        for status in REQUIRED_STATUSES
    )

    total_previous_ships = sum(
        previous_data.get(status, {}).get("ships", 0)
        for status in REQUIRED_STATUSES
    )

    total_latest_teu = sum(
        latest_data.get(status, {}).get("teu", 0)
        for status in REQUIRED_STATUSES
    )

    total_previous_teu = sum(
        previous_data.get(status, {}).get("teu", 0)
        for status in REQUIRED_STATUSES
    )

    heatmap_statuses = sorted(
        REQUIRED_STATUSES,
        key=lambda status: latest_data.get(status, {}).get("teu", 0),
        reverse=True,
    )

    heatmap_rows = ""

    max_teu = max(
        [
            latest_data.get(status, {}).get("teu", 0)
            for status in REQUIRED_STATUSES
        ]
        or [1]
    )

    for status in heatmap_statuses:
        latest = latest_data.get(status, {})
        previous = previous_data.get(status, {})

        current_teu = latest.get("teu", 0)
        previous_teu = previous.get("teu")

        width = (
            (current_teu / max_teu) * 100
            if max_teu > 0
            else 0
        )

        delta = (
            current_teu - previous_teu
            if previous_teu is not None
            else None
        )

        if delta is None:
            change_class = "neutral"
            change_text = "N/A"
        elif delta > 0:
            change_class = "up"
            change_text = f"+{delta:,}"
        elif delta < 0:
            change_class = "down"
            change_text = f"{delta:,}"
        else:
            change_class = "neutral"
            change_text = "0"

        heatmap_rows += f"""
        <div class="heatmap-row">
            <div class="heatmap-header">
                <span>{html.escape(status)}</span>
                <span>{format_number(current_teu)} TEU</span>
            </div>
            <div class="heatmap-track">
                <div class="heatmap-bar" style="width:{width:.1f}%"></div>
            </div>
            <div class="heatmap-footer">
                <span class="{change_class}">MOM: {change_text}</span>
                <span>{width:.1f}% OF MAX</span>
            </div>
        </div>
        """

    latest_active_teu = active_latest.get("teu")
    previous_active_teu = active_previous.get("teu")

    active_teu_change = (
        latest_active_teu - previous_active_teu
        if latest_active_teu is not None
        and previous_active_teu is not None
        else None
    )

    inactive_teu_change = (
        inactive_latest.get("teu") - inactive_previous.get("teu")
        if inactive_latest.get("teu") is not None
        and inactive_previous.get("teu") is not None
        else None
    )

    port_teu_change = (
        port_latest.get("teu") - port_previous.get("teu")
        if port_latest.get("teu") is not None
        and port_previous.get("teu") is not None
        else None
    )

    latest_active_share = (
        (active_latest.get("teu", 0) / total_latest_teu) * 100
        if total_latest_teu > 0
        else 0
    )

    latest_inactive_share = (
        (inactive_latest.get("teu", 0) / total_latest_teu) * 100
        if total_latest_teu > 0
        else 0
    )

    latest_port_share = (
        (port_latest.get("teu", 0) / total_latest_teu) * 100
        if total_latest_teu > 0
        else 0
    )

    active_trend_text = (
        "ACTIVE FLEET CAPACITY EXPANDED MONTH-ON-MONTH."
        if active_teu_change is not None and active_teu_change > 0
        else (
            "ACTIVE FLEET CAPACITY CONTRACTED MONTH-ON-MONTH."
            if active_teu_change is not None and active_teu_change < 0
            else "ACTIVE FLEET CAPACITY WAS BROADLY UNCHANGED."
        )
    )

    port_trend_text = (
        "PORT-BASED CAPACITY INCREASED, INDICATING HIGHER PORT INVENTORY."
        if port_teu_change is not None and port_teu_change > 0
        else (
            "PORT-BASED CAPACITY DECLINED VERSUS THE PREVIOUS SNAPSHOT."
            if port_teu_change is not None and port_teu_change < 0
            else "PORT-BASED CAPACITY WAS BROADLY UNCHANGED."
        )
    )

    inactive_trend_text = (
        "INACTIVE CAPACITY ROSE, WARRANTING SUPPLY-SIDE MONITORING."
        if inactive_teu_change is not None and inactive_teu_change > 0
        else (
            "INACTIVE CAPACITY DECLINED, SUPPORTING UTILIZATION CONDITIONS."
            if inactive_teu_change is not None and inactive_teu_change < 0
            else "INACTIVE CAPACITY WAS BROADLY UNCHANGED."
        )
    )

    dashboard_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Linerlytica | MI Fleet Terminal</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.6/dist/chart.umd.min.js"></script>

<style>
:root {{
    --bg-core: #02060b;
    --bg-base: #040a11;
    --panel: #07111c;
    --panel-2: #0a1724;
    --panel-3: #0d1d2b;
    --line: #193348;
    --line-strong: #294b66;

    --text: #f3f7fb;
    --text-soft: #b5c5d2;
    --muted: #8298aa;
    --dim: #4c6579;

    --amber: #f6a623;
    --amber-soft: #ffc05e;
    --blue: #42a8f5;
    --cyan: #37c7e0;
    --green: #36ca82;
    --red: #ff6666;
    --yellow: #ffd166;
    --purple: #b18cff;

    --shadow:
        0 6px 22px rgba(0, 0, 0, .58),
        0 0 18px rgba(38, 127, 184, .08);
}}

* {{
    box-sizing: border-box;
}}

html,
body {{
    min-height: 100%;
}}

body {{
    margin: 0;
    color: var(--text);
    background:
        radial-gradient(
            circle at 50% -20%,
            rgba(38, 112, 163, .15),
            transparent 44%
        ),
        linear-gradient(
            180deg,
            rgba(246, 166, 35, .025),
            transparent 250px
        ),
        var(--bg-core);
    font-family:
        "IBM Plex Mono",
        "Roboto Mono",
        "SFMono-Regular",
        Consolas,
        "Liberation Mono",
        monospace;
    font-size: 11px;
    line-height: 1.35;
    -webkit-font-smoothing: antialiased;
}}

.container {{
    width: min(1920px, calc(100% - 16px));
    max-width: 1920px;
    margin: 8px auto 14px;
}}

.terminal-header {{
    min-height: 54px;
    display: flex;
    align-items: stretch;
    justify-content: space-between;
    gap: 14px;
    overflow: hidden;
    border: 1px solid var(--line-strong);
    background:
        linear-gradient(
            180deg,
            rgba(13, 32, 49, .98),
            rgba(3, 9, 15, .98)
        );
    box-shadow: var(--shadow);
}}

.terminal-brand {{
    min-width: 58px;
    display: flex;
    align-items: center;
    justify-content: center;
    color: #02060b;
    background: var(--amber);
    font-size: 13px;
    font-weight: 900;
    letter-spacing: .10em;
    box-shadow: 0 0 16px rgba(246, 166, 35, .35);
}}

.terminal-title {{
    flex: 1;
    display: flex;
    flex-direction: column;
    justify-content: center;
    min-width: 0;
}}

.terminal-title h1 {{
    margin: 0;
    color: #ffffff;
    font-size: 14px;
    font-weight: 900;
    letter-spacing: .10em;
    text-transform: uppercase;
}}

.terminal-title p {{
    margin: 4px 0 0;
    color: var(--muted);
    font-size: 8px;
    font-weight: 800;
    letter-spacing: .07em;
    text-transform: uppercase;
}}

.header-meta {{
    min-width: 320px;
    display: flex;
    flex-direction: column;
    justify-content: center;
    gap: 3px;
    padding: 7px 14px;
    color: var(--muted);
    border-left: 1px solid var(--line);
    font-size: 8px;
    font-weight: 800;
    letter-spacing: .05em;
    line-height: 1.5;
    text-align: right;
    text-transform: uppercase;
}}

.header-meta strong {{
    color: var(--text-soft);
}}

.ticker-bar {{
    display: flex;
    margin-top: 6px;
    overflow-x: auto;
    border: 1px solid var(--line);
    background: #02070d;
    box-shadow: 0 2px 10px rgba(0, 0, 0, .32);
}}

.ticker-heading {{
    min-width: 116px;
    display: flex;
    align-items: center;
    padding: 0 12px;
    color: #071018;
    background: var(--amber);
    font-size: 8px;
    font-weight: 900;
    letter-spacing: .09em;
    text-transform: uppercase;
}}

.ticker-item {{
    min-width: 230px;
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 3px 10px;
    padding: 7px 12px;
    border-right: 1px solid var(--line);
    background:
        linear-gradient(
            180deg,
            rgba(13, 29, 43, .68),
            rgba(2, 8, 14, .88)
        );
}}

.ticker-label {{
    overflow: hidden;
    color: var(--muted);
    font-size: 8px;
    font-weight: 800;
    letter-spacing: .06em;
    text-overflow: ellipsis;
    text-transform: uppercase;
    white-space: nowrap;
}}

.ticker-value {{
    color: var(--text);
    font-size: 11px;
    font-weight: 800;
    font-variant-numeric: tabular-nums;
    text-align: right;
}}

.ticker-delta {{
    grid-column: 1 / -1;
    font-size: 9px;
    font-weight: 800;
    text-align: right;
}}

.dashboard-grid {{
    display: grid;
    grid-template-columns: repeat(12, minmax(0, 1fr));
    gap: 6px;
    margin-top: 6px;
}}

.panel {{
    min-width: 0;
    overflow: hidden;
    border: 1px solid var(--line);
    background:
        linear-gradient(
            145deg,
            rgba(14, 33, 49, .76),
            rgba(4, 11, 18, .98)
        );
    box-shadow: var(--shadow);
}}

.panel::before {{
    display: block;
    height: 2px;
    content: "";
    background: linear-gradient(
        90deg,
        var(--amber),
        rgba(246, 166, 35, .18) 42%,
        transparent 78%
    );
}}

.panel-title {{
    min-height: 31px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    margin: 0;
    padding: 0 10px;
    color: var(--amber);
    border-bottom: 1px solid var(--line);
    background:
        linear-gradient(
            180deg,
            rgba(13, 31, 47, .94),
            rgba(7, 17, 27, .90)
        );
    font-size: 9px;
    font-weight: 900;
    letter-spacing: .09em;
    text-transform: uppercase;
}}

.panel-title span:last-child {{
    color: var(--muted);
    font-size: 8px;
    font-weight: 700;
    text-align: right;
}}

.kpi-grid {{
    grid-column: span 12;
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 6px;
}}

.kpi {{
    min-height: 91px;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    padding: 9px 10px;
    border: 1px solid var(--line);
    border-top: 2px solid var(--blue);
    background:
        linear-gradient(
            135deg,
            rgba(38, 108, 155, .18),
            transparent 62%
        ),
        var(--panel);
    box-shadow: var(--shadow);
}}

.kpi:nth-child(2) {{
    border-top-color: var(--green);
}}

.kpi:nth-child(3) {{
    border-top-color: var(--amber);
}}

.kpi:nth-child(4) {{
    border-top-color: var(--purple);
}}

.kpi-label {{
    color: var(--muted);
    font-size: 8px;
    font-weight: 800;
    letter-spacing: .08em;
    text-transform: uppercase;
}}

.kpi-value {{
    margin-top: 8px;
    overflow: hidden;
    color: #ffffff;
    font-size: clamp(17px, 1.55vw, 25px);
    font-weight: 900;
    font-variant-numeric: tabular-nums;
    letter-spacing: -.06em;
    text-align: right;
    text-overflow: ellipsis;
    white-space: nowrap;
}}

.kpi-sub {{
    min-height: 15px;
    margin-top: 5px;
    font-size: 9px;
    font-weight: 800;
    text-align: right;
}}

.main-chart-panel {{
    grid-column: span 8;
}}

.watchlist-panel {{
    grid-column: span 4;
}}

.chart-wrap {{
    position: relative;
    min-height: 389px;
    padding: 10px;
}}

.watchlist-table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 10px;
}}

.watchlist-table th {{
    padding: 9px 10px;
    color: var(--muted);
    border-bottom: 1px solid var(--line);
    background: rgba(2, 8, 14, .58);
    font-size: 8px;
    font-weight: 800;
    letter-spacing: .06em;
    text-align: right;
    text-transform: uppercase;
}}

.watchlist-table th:first-child {{
    text-align: left;
}}

.watchlist-table td {{
    padding: 11px 10px;
    color: var(--text-soft);
    border-bottom: 1px solid rgba(39, 71, 95, .52);
    font-variant-numeric: tabular-nums;
    text-align: right;
}}

.watchlist-table td:first-child {{
    text-align: left;
}}

.watchlist-table tr:hover {{
    background: rgba(66, 168, 245, .05);
}}

.status {{
    color: #ffffff !important;
    font-weight: 800;
}}

.number {{
    color: var(--text) !important;
}}

.watchlist-note {{
    padding: 9px 10px;
    color: var(--muted);
    border-top: 1px solid var(--line);
    background: rgba(2, 8, 14, .58);
    font-size: 8px;
    font-weight: 700;
    letter-spacing: .03em;
    line-height: 1.6;
    text-transform: uppercase;
}}

.heatmap-panel {{
    grid-column: span 4;
}}

.news-panel {{
    grid-column: span 4;
}}

.risk-panel {{
    grid-column: span 4;
}}

.heatmap-content {{
    padding: 9px 10px 10px;
}}

.heatmap-row {{
    padding: 7px 0;
    border-bottom: 1px solid rgba(39, 71, 95, .48);
}}

.heatmap-row:last-child {{
    border-bottom: 0;
}}

.heatmap-header,
.heatmap-footer {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
}}

.heatmap-header {{
    color: var(--text-soft);
    font-size: 9px;
    font-weight: 800;
}}

.heatmap-header span:last-child {{
    color: #ffffff;
    font-variant-numeric: tabular-nums;
    text-align: right;
}}

.heatmap-track {{
    height: 6px;
    margin: 6px 0;
    overflow: hidden;
    background: #02070d;
    border: 1px solid #173249;
}}

.heatmap-bar {{
    height: 100%;
    min-width: 2px;
    background:
        linear-gradient(
            90deg,
            #1676b5,
            var(--blue),
            var(--cyan)
        );
    box-shadow: 0 0 10px rgba(55, 199, 224, .35);
}}

.heatmap-footer {{
    color: var(--dim);
    font-size: 8px;
    font-weight: 700;
    text-transform: uppercase;
}}

.terminal-list {{
    min-height: 264px;
    margin: 0;
    padding: 7px 11px 10px 28px;
    color: var(--text-soft);
    background:
        linear-gradient(
            180deg,
            rgba(4, 12, 20, .34),
            rgba(2, 7, 12, .12)
        );
}}

.terminal-list li {{
    padding: 8px 2px;
    border-bottom: 1px solid rgba(39, 71, 95, .52);
    font-size: 9px;
    font-weight: 700;
    letter-spacing: .01em;
    line-height: 1.5;
}}

.terminal-list li:last-child {{
    border-bottom: 0;
}}

.terminal-list li::marker {{
    color: var(--amber);
}}

.risk-metrics {{
    padding: 8px 10px 10px;
}}

.risk-row {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 9px 0;
    border-bottom: 1px solid rgba(39, 71, 95, .52);
}}

.risk-row:last-child {{
    border-bottom: 0;
}}

.risk-label {{
    color: var(--muted);
    font-size: 8px;
    font-weight: 800;
    letter-spacing: .06em;
    text-transform: uppercase;
}}

.risk-value {{
    color: #ffffff;
    font-size: 10px;
    font-weight: 900;
    font-variant-numeric: tabular-nums;
    text-align: right;
}}

.risk-meter {{
    width: 82px;
    height: 5px;
    overflow: hidden;
    border: 1px solid #1c3a52;
    background: #02070d;
}}

.risk-meter span {{
    display: block;
    height: 100%;
    background: var(--amber);
    box-shadow: 0 0 9px rgba(246, 166, 35, .40);
}}

.footer-status {{
    min-height: 30px;
    grid-column: span 12;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 0 10px;
    color: var(--muted);
    border: 1px solid var(--line);
    border-left: 3px solid var(--green);
    background: #02070d;
    font-size: 8px;
    font-weight: 800;
    letter-spacing: .06em;
    text-transform: uppercase;
}}

.footer-status strong {{
    color: var(--text-soft);
}}

.up {{
    color: var(--green) !important;
}}

.down {{
    color: var(--red) !important;
}}

.neutral {{
    color: var(--muted) !important;
}}

@media (max-width: 1200px) {{
    .terminal-header {{
        flex-wrap: wrap;
    }}

    .header-meta {{
        width: 100%;
        min-width: 100%;
        border-top: 1px solid var(--line);
        border-left: 0;
        text-align: left;
    }}

    .main-chart-panel {{
        grid-column: span 12;
    }}

    .watchlist-panel {{
        grid-column: span 12;
    }}

    .heatmap-panel,
    .news-panel,
    .risk-panel {{
        grid-column: span 12;
    }}
}}

@media (max-width: 760px) {{
    .container {{
        width: calc(100% - 10px);
        margin-top: 5px;
    }}

    .terminal-brand {{
        min-width: 46px;
    }}

    .terminal-title h1 {{
        font-size: 11px;
        letter-spacing: .06em;
    }}

    .terminal-title p {{
        font-size: 7px;
    }}

    .kpi-grid {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
    }}

    .ticker-heading {{
        min-width: 95px;
    }}

    .ticker-item {{
        min-width: 205px;
    }}

    .footer-status {{
        align-items: flex-start;
        flex-direction: column;
        justify-content: center;
        padding: 7px 10px;
    }}
}}

@media (max-width: 480px) {{
    .kpi-grid {{
        grid-template-columns: 1fr;
    }}

    .watchlist-panel {{
        overflow-x: auto;
    }}

    .watchlist-table {{
        min-width: 580px;
    }}
}}
</style>
</head>

<body>
<div class="container">

    <header class="terminal-header">
        <div class="terminal-brand">MI</div>

        <div class="terminal-title">
            <h1>Linerlytica Global Containership Fleet Terminal</h1>
            <p>
                Fleet Supply Intelligence | Monthly 17th Snapshot History |
                Ships and Capacity Monitoring
            </p>
        </div>

        <div class="header-meta">
            <div>
                LATEST SNAPSHOT:
                <strong>{html.escape(latest_date)}</strong>
            </div>
            <div>
                PREVIOUS SNAPSHOT:
                <strong>{html.escape(previous_date or "N/A")}</strong>
            </div>
            <div>
                SOURCE:
                <strong>LINERLYTICA</strong>
            </div>
        </div>
    </header>

    <section class="ticker-bar">
        <div class="ticker-heading">Fleet Ticker</div>
        {ticker_items}
    </section>

    <main class="dashboard-grid">

        <section class="kpi-grid">
            <div class="kpi">
                <div class="kpi-label">Total Fleet Ships</div>
                <div class="kpi-value">
                    {format_number(total_latest_ships)}
                </div>
                <div class="kpi-sub">
                    {format_delta(total_latest_ships, total_previous_ships)}
                </div>
            </div>

            <div class="kpi">
                <div class="kpi-label">Total Fleet Capacity</div>
                <div class="kpi-value">
                    {format_number(total_latest_teu)}
                </div>
                <div class="kpi-sub">
                    {format_delta(total_latest_teu, total_previous_teu)}
                </div>
            </div>

            <div class="kpi">
                <div class="kpi-label">Active Fleet Capacity</div>
                <div class="kpi-value">
                    {format_number(active_latest.get("teu"))}
                </div>
                <div class="kpi-sub">
                    {format_delta(
                        active_latest.get("teu"),
                        active_previous.get("teu")
                    )}
                </div>
            </div>

            <div class="kpi">
                <div class="kpi-label">Historical Observations</div>
                <div class="kpi-value">
                    {len(completed_dates)} MOS
                </div>
                <div class="kpi-sub">
                    <span class="neutral">17TH OF EACH MONTH</span>
                </div>
            </div>
        </section>

        <section class="panel main-chart-panel">
            <h2 class="panel-title">
                <span>01. Fleet Capacity Trend | TEU</span>
                <span>{html.escape(completed_dates[0])} — {html.escape(latest_date)}</span>
            </h2>

            <div class="chart-wrap">
                <canvas id="teuChart"></canvas>
            </div>
        </section>

        <section class="panel watchlist-panel">
            <h2 class="panel-title">
                <span>02. Fleet Status Watchlist</span>
                <span>Latest Snapshot</span>
            </h2>

            <table class="watchlist-table">
                <thead>
                    <tr>
                        <th>Fleet Segment</th>
                        <th>Ships</th>
                        <th>Capacity</th>
                        <th>MoM Change</th>
                    </tr>
                </thead>
                <tbody>
                    {table_rows}
                </tbody>
            </table>

            <div class="watchlist-note">
                CHANGE METRIC REPRESENTS MONTH-ON-MONTH SHIP COUNT MOVEMENT.
                CAPACITY VALUES ARE REPORTED IN TEU.
            </div>
        </section>

        <section class="panel heatmap-panel">
            <h2 class="panel-title">
                <span>03. Capacity Heatmap</span>
                <span>TEU Distribution</span>
            </h2>

            <div class="heatmap-content">
                {heatmap_rows}
            </div>
        </section>

        <section class="panel news-panel">
            <h2 class="panel-title">
                <span>04. Market Intelligence</span>
                <span>Supply Narrative</span>
            </h2>

            <ul class="terminal-list">
                <li>
                    ACTIVE FLEET REPRESENTS
                    {latest_active_share:.1f}% OF THE LATEST OBSERVED
                    FLEET CAPACITY.
                </li>
                <li>
                    {active_trend_text}
                </li>
                <li>
                    {port_trend_text}
                </li>
                <li>
                    SHIPS AT ANCHORAGE ACCOUNT FOR
                    {(anchorage_latest.get("teu", 0) / total_latest_teu * 100 if total_latest_teu > 0 else 0):.1f}%
                    OF OBSERVED CAPACITY.
                </li>
                <li>
                    SHIPYARD CAPACITY STANDS AT
                    {format_number(shipyard_latest.get("teu"))} TEU.
                </li>
            </ul>
        </section>

        <section class="panel risk-panel">
            <h2 class="panel-title">
                <span>05. Risk Indicators</span>
                <span>Fleet Utilization Signals</span>
            </h2>

            <div class="risk-metrics">
                <div class="risk-row">
                    <div>
                        <div class="risk-label">Active Capacity Share</div>
                        <div class="risk-value">
                            {latest_active_share:.1f}% OF TOTAL TEU
                        </div>
                    </div>
                    <div class="risk-meter">
                        <span style="width:{min(latest_active_share, 100):.1f}%"></span>
                    </div>
                </div>

                <div class="risk-row">
                    <div>
                        <div class="risk-label">Port Capacity Share</div>
                        <div class="risk-value">
                            {latest_port_share:.1f}% OF TOTAL TEU
                        </div>
                    </div>
                    <div class="risk-meter">
                        <span style="width:{min(latest_port_share, 100):.1f}%"></span>
                    </div>
                </div>

                <div class="risk-row">
                    <div>
                        <div class="risk-label">Inactive Capacity Share</div>
                        <div class="risk-value">
                            {latest_inactive_share:.1f}% OF TOTAL TEU
                        </div>
                    </div>
                    <div class="risk-meter">
                        <span style="width:{min(latest_inactive_share, 100):.1f}%"></span>
                    </div>
                </div>

                <div class="risk-row">
                    <div>
                        <div class="risk-label">Inactive Fleet Signal</div>
                        <div class="risk-value">
                            {format_number(inactive_latest.get("teu"))} TEU
                        </div>
                    </div>
                    <div class="risk-meter">
                        <span style="width:{min(latest_inactive_share * 4, 100):.1f}%"></span>
                    </div>
                </div>

                <div class="risk-row">
                    <div>
                        <div class="risk-label">Risk Assessment</div>
                        <div class="risk-value">
                            {"ELEVATED" if latest_inactive_share >= 3 else "MONITORED"}
                        </div>
                    </div>
                    <div class="risk-meter">
                        <span style="width:{min(max(latest_inactive_share * 15, 18), 100):.1f}%"></span>
                    </div>
                </div>

                <div class="risk-row">
                    <div>
                        <div class="risk-label">Capacity Signal</div>
                        <div class="risk-value">
                            {"EXPANDING" if active_teu_change is not None and active_teu_change > 0 else "CONTRACTING" if active_teu_change is not None and active_teu_change < 0 else "STABLE"}
                        </div>
                    </div>
                    <div class="risk-meter">
                        <span style="width:{70 if active_teu_change is not None and active_teu_change > 0 else 45 if active_teu_change is not None and active_teu_change < 0 else 55}%"></span>
                    </div>
                </div>
            </div>

            <div class="watchlist-note">
                {inactive_trend_text}
            </div>
        </section>

        <footer class="footer-status">
            <div>
                SYSTEM STATUS:
                <strong>NORMAL</strong>
                | MARKET HEALTH:
                <strong>MONITORED</strong>
                | SENTIMENT:
                <strong>DATA-DRIVEN</strong>
            </div>
            <div>
                ALERTS:
                <strong>NO CRITICAL DATA EXCEPTIONS</strong>
                | GENERATED FROM LINERLYTICA.DB
            </div>
        </footer>

    </main>
</div>

<script>
const labels = {json.dumps(completed_dates, ensure_ascii=False)};

new Chart(document.getElementById("teuChart"), {{
    type: "line",
    data: {{
        labels: labels,
        datasets: [
            {{
                label: "ACTIVE SHIPS",
                data: {json.dumps(teu_series["Active Ships"])},
                borderColor: "#36ca82",
                backgroundColor: "rgba(54, 202, 130, .08)",
                borderWidth: 2.5,
                pointRadius: 2,
                pointHoverRadius: 4,
                fill: true,
                tension: 0.20
            }},
            {{
                label: "SHIPS AT PORT",
                data: {json.dumps(teu_series["Ships at port"])},
                borderColor: "#f6a623",
                backgroundColor: "transparent",
                borderWidth: 1.8,
                pointRadius: 1.5,
                pointHoverRadius: 3,
                fill: false,
                tension: 0.20
            }},
            {{
                label: "SHIPS AT ANCHORAGE",
                data: {json.dumps(teu_series["Ships at anchorage"])},
                borderColor: "#42a8f5",
                backgroundColor: "transparent",
                borderWidth: 1.7,
                pointRadius: 1.5,
                pointHoverRadius: 3,
                fill: false,
                tension: 0.20
            }},
            {{
                label: "INACTIVE SHIPS",
                data: {json.dumps(teu_series["Inactive Ships"])},
                borderColor: "#ff6666",
                backgroundColor: "transparent",
                borderWidth: 1.6,
                pointRadius: 1.5,
                pointHoverRadius: 3,
                fill: false,
                tension: 0.20
            }},
            {{
                label: "SHIPS IN SHIPYARD",
                data: {json.dumps(teu_series["Ships in shipyard"])},
                borderColor: "#ffd166",
                backgroundColor: "transparent",
                borderWidth: 1.6,
                pointRadius: 1.5,
                pointHoverRadius: 3,
                fill: false,
                tension: 0.20
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
                    color: "#b5c5d2",
                    boxWidth: 12,
                    boxHeight: 2,
                    padding: 14,
                    font: {{
                        family: "Consolas, monospace",
                        size: 9,
                        weight: "700"
                    }}
                }}
            }},
            tooltip: {{
                backgroundColor: "rgba(5, 14, 23, .96)",
                titleColor: "#f3f7fb",
                bodyColor: "#b5c5d2",
                borderColor: "#294b66",
                borderWidth: 1,
                padding: 10,
                titleFont: {{
                    family: "Consolas, monospace",
                    size: 10,
                    weight: "700"
                }},
                bodyFont: {{
                    family: "Consolas, monospace",
                    size: 10
                }},
                callbacks: {{
                    label: function(context) {{
                        if (
                            context.raw === null ||
                            context.raw === undefined
                        ) {{
                            return context.dataset.label + ": N/A";
                        }}

                        return context.dataset.label + ": " +
                            Number(context.raw).toLocaleString() + " TEU";
                    }}
                }}
            }}
        }},
        scales: {{
            x: {{
                grid: {{
                    color: "rgba(43, 80, 108, .32)",
                    drawBorder: false
                }},
                ticks: {{
                    color: "#8298aa",
                    maxRotation: 0,
                    maxTicksLimit: 9,
                    font: {{
                        family: "Consolas, monospace",
                        size: 9
                    }}
                }}
            }},
            y: {{
                grid: {{
                    color: "rgba(43, 80, 108, .38)",
                    drawBorder: false
                }},
                ticks: {{
                    color: "#8298aa",
                    font: {{
                        family: "Consolas, monospace",
                        size: 9
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

    HTML_PATH.write_text(dashboard_html, encoding="utf-8")

    print(
        f"[Dashboard] Generated: {HTML_PATH} | "
        f"latest={latest_date} | "
        f"history={len(completed_dates)} snapshots"
    )

# =============================================================================
# 8. Main
# =============================================================================

def main() -> None:
    """모듈 실행 진입점."""
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
