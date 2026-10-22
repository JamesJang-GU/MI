#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Linerlytica Public Web Intelligence Collector
----------------------------------------------
기능:
1. robots.txt 준수
2. sitemap.xml 및 사이트 내부 링크 탐색
3. 공개 HTML의 제목, 본문, 헤딩, 표, 이미지 ALT, 링크 수집
4. SQLite 이력 DB 저장
5. Bloomberg/MI Terminal 스타일 index.html 생성

주의:
- 로그인, 유료 구독, CAPTCHA, 접근 제한 우회 기능은 포함하지 않습니다.
- robots.txt가 금지한 경로는 수집하지 않습니다.
- 사이트 이용약관과 데이터 재배포 조건을 별도로 확인해야 합니다.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET

from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# =============================================================================
# 1. 기본 설정
# =============================================================================

BASE_DIR = Path(__file__).resolve().parent

DB_PATH = BASE_DIR / "Linerlytica.db"
HTML_PATH = BASE_DIR / "index.html"

BASE_URL = "https://www.linerlytica.com/"
ALLOWED_DOMAIN = "www.linerlytica.com"

# 너무 큰 크롤링을 방지하는 안전장치입니다.
# 전체 사이트 규모를 확인한 뒤 단계적으로 늘리는 것을 권장합니다.
MAX_PAGES = int(os.getenv("LINERLYTICA_MAX_PAGES", "100"))

# 서버 부하 방지용 요청 간격(초)
REQUEST_DELAY_SECONDS = float(os.getenv("LINERLYTICA_REQUEST_DELAY", "2.0"))

# HTML 응답 크기 제한: 10MB
MAX_RESPONSE_BYTES = 10 * 1024 * 1024

# 실행 시 기존 URL 전체를 다시 조회할지 여부
# 1이면 DB에 이미 있는 URL도 다시 확인하여 변경 이력을 갱신합니다.
REFRESH_EXISTING_URLS = os.getenv("LINERLYTICA_REFRESH_EXISTING", "1") == "1"

# 수집 식별용 User-Agent
USER_AGENT = os.getenv(
    "LINERLYTICA_USER_AGENT",
    "Market-Intelligence-Research-Bot/1.0 "
    "(public-content-collection; contact: admin@example.com)"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# 크롤링 우선 시드
SEED_URLS = [
    BASE_URL,
    urljoin(BASE_URL, "sitemap.xml"),
    urljoin(BASE_URL, "sitemap_index.xml"),
]

# 수집 제외 확장자
EXCLUDED_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico",
    ".pdf", ".zip", ".rar", ".7z",
    ".mp3", ".mp4", ".avi", ".mov", ".webm",
    ".css", ".js", ".json", ".xml",
    ".woff", ".woff2", ".ttf", ".eot",
}

# 추적 파라미터 제거
TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "msclkid",
    "mc_cid", "mc_eid",
    "_ga", "_gl",
}

# 본문에서 불필요한 UI 요소 제거 후보
REMOVE_TAGS = [
    "script", "style", "noscript", "iframe",
    "svg", "canvas", "form", "button",
]

REMOVE_CLASS_KEYWORDS = [
    "cookie",
    "consent",
    "newsletter",
    "subscribe",
    "popup",
    "modal",
    "breadcrumb",
    "social-share",
    "share-buttons",
    "comment",
]

# =============================================================================
# 2. 공통 유틸리티
# =============================================================================

def utc_now() -> str:
    """UTC ISO-8601 시각 반환"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def normalize_whitespace(value: str) -> str:
    """연속 공백/개행을 정리"""
    return re.sub(r"\s+", " ", value or "").strip()

def sha256_text(value: str) -> str:
    """문자열 SHA-256 해시"""
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()

def clean_url(url: str) -> Optional[str]:
    """
    URL 정규화:
    - fragment 제거
    - 추적 파라미터 제거
    - 허용 도메인 외 URL 제거
    - 이미지/PDF/JS 등 비HTML 자원 제거
    """
    if not url:
        return None

    try:
        parsed = urlparse(url.strip())

        if parsed.scheme not in {"http", "https"}:
            return None

        hostname = (parsed.hostname or "").lower()

        # www 유무를 통일할 필요가 있다면 여기에서 처리 가능
        if hostname != ALLOWED_DOMAIN:
            return None

        path = parsed.path or "/"
        lower_path = path.lower()

        if any(lower_path.endswith(ext) for ext in EXCLUDED_EXTENSIONS):
            return None

        filtered_query = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            key_lower = key.lower()

            if key_lower in TRACKING_PARAMS:
                continue

            if key_lower.startswith("utm_"):
                continue

            filtered_query.append((key, value))

        normalized = urlunparse((
            "https",
            ALLOWED_DOMAIN,
            path,
            "",
            urlencode(filtered_query, doseq=True),
            "",
        ))

        return normalized

    except Exception:
        return None

def make_session() -> requests.Session:
    """재시도 정책이 적용된 requests Session 생성"""
    session = requests.Session()

    retries = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )

    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    session.headers.update(HEADERS)
    return session

# =============================================================================
# 3. SQLite DB
# =============================================================================

def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn

def init_database(conn: sqlite3.Connection) -> None:
    """필요 테이블 생성"""

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS crawl_runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL,
            pages_attempted INTEGER DEFAULT 0,
            pages_collected INTEGER DEFAULT 0,
            pages_changed INTEGER DEFAULT 0,
            error_message TEXT
        );

        CREATE TABLE IF NOT EXISTS pages (
            page_id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL UNIQUE,
            canonical_url TEXT,
            title TEXT,
            description TEXT,
            author TEXT,
            published_at TEXT,
            modified_at TEXT,
            page_type TEXT,
            language TEXT,
            visible_text TEXT,
            headings_json TEXT,
            tables_json TEXT,
            images_json TEXT,
            links_json TEXT,
            content_hash TEXT,
            http_status INTEGER,
            first_collected_at TEXT NOT NULL,
            last_collected_at TEXT NOT NULL,
            last_success_at TEXT,
            last_error TEXT,
            is_active INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS page_snapshots (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            page_id INTEGER NOT NULL,
            run_id INTEGER,
            collected_at TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            title TEXT,
            visible_text TEXT,
            headings_json TEXT,
            tables_json TEXT,
            images_json TEXT,
            http_status INTEGER,
            FOREIGN KEY(page_id) REFERENCES pages(page_id),
            FOREIGN KEY(run_id) REFERENCES crawl_runs(run_id),
            UNIQUE(page_id, content_hash)
        );

        CREATE TABLE IF NOT EXISTS crawl_errors (
            error_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER,
            url TEXT,
            error_type TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES crawl_runs(run_id)
        );

        CREATE INDEX IF NOT EXISTS idx_pages_last_success
            ON pages(last_success_at DESC);

        CREATE INDEX IF NOT EXISTS idx_pages_published_at
            ON pages(published_at DESC);

        CREATE INDEX IF NOT EXISTS idx_snapshots_page_id
            ON page_snapshots(page_id);
    """)

    conn.commit()

def create_crawl_run(conn: sqlite3.Connection) -> int:
    cursor = conn.execute("""
        INSERT INTO crawl_runs (started_at, status)
        VALUES (?, ?)
    """, (utc_now(), "RUNNING"))

    conn.commit()
    return int(cursor.lastrowid)

def finish_crawl_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    pages_attempted: int,
    pages_collected: int,
    pages_changed: int,
    error_message: Optional[str] = None,
) -> None:
    conn.execute("""
        UPDATE crawl_runs
        SET completed_at = ?,
            status = ?,
            pages_attempted = ?,
            pages_collected = ?,
            pages_changed = ?,
            error_message = ?
        WHERE run_id = ?
    """, (
        utc_now(),
        status,
        pages_attempted,
        pages_collected,
        pages_changed,
        error_message,
        run_id,
    ))
    conn.commit()

def log_error(
    conn: sqlite3.Connection,
    run_id: int,
    url: str,
    error_type: str,
    error_message: str,
) -> None:
    conn.execute("""
        INSERT INTO crawl_errors (
            run_id, url, error_type, error_message, created_at
        )
        VALUES (?, ?, ?, ?, ?)
    """, (
        run_id,
        url,
        error_type,
        error_message[:2000],
        utc_now(),
    ))
    conn.commit()

# =============================================================================
# 4. robots.txt 및 sitemap 처리
# =============================================================================

def load_robots_parser(session: requests.Session) -> RobotFileParser:
    """
    robots.txt를 직접 요청하여 RobotFileParser에 전달합니다.
    읽기 실패 시에는 안전하게 전체 수집을 허용하지 않고,
    홈페이지 URL만 대상으로 제한하는 방식이 더 보수적일 수 있습니다.

    여기서는 robots.txt 부재(404) 시 일반적 관행에 따라 허용으로 처리합니다.
    """

    robots_url = urljoin(BASE_URL, "robots.txt")
    parser = RobotFileParser()
    parser.set_url(robots_url)

    try:
        response = session.get(robots_url, timeout=30)

        if response.status_code == 200:
            parser.parse(response.text.splitlines())
            print(f"[ROBOTS] Loaded: {robots_url}")

        elif response.status_code == 404:
            # robots.txt가 없을 경우 빈 규칙 = 허용
            parser.parse([])
            print("[ROBOTS] robots.txt not found (404). Using empty rules.")

        else:
            # 명확하지 않은 경우 보수적으로 빈 정책 대신
            # 실제 운영에서는 담당자 확인 후 실행 권장
            parser.parse([])
            print(f"[ROBOTS] Unexpected status {response.status_code}. Using empty rules.")

    except Exception as exc:
        parser.parse([])
        print(f"[ROBOTS] Failed to load robots.txt: {exc}")

    return parser

def is_allowed_by_robots(robots: RobotFileParser, url: str) -> bool:
    try:
        return robots.can_fetch(USER_AGENT, url)
    except Exception:
        return False

def parse_sitemap_xml(xml_text: str) -> list[str]:
    """sitemap 또는 sitemap index에서 loc URL을 추출"""
    urls: list[str] = []

    try:
        root = ET.fromstring(xml_text)

        for element in root.iter():
            if element.tag.lower().endswith("loc") and element.text:
                value = element.text.strip()
                if value:
                    urls.append(value)

    except ET.ParseError:
        pass

    return urls

def discover_sitemap_urls(
    session: requests.Session,
    robots: RobotFileParser,
) -> set[str]:
    """
    기본 sitemap.xml, sitemap_index.xml 및 robots.txt 내 Sitemap 지시문을 탐색.
    sitemap index는 최대 2단계까지 재귀 처리합니다.
    """

    sitemap_candidates = {
        urljoin(BASE_URL, "sitemap.xml"),
        urljoin(BASE_URL, "sitemap_index.xml"),
    }

    try:
        robots_url = urljoin(BASE_URL, "robots.txt")
        response = session.get(robots_url, timeout=30)

        if response.status_code == 200:
            for line in response.text.splitlines():
                if line.lower().startswith("sitemap:"):
                    sitemap_url = line.split(":", 1)[1].strip()
                    if sitemap_url:
                        sitemap_candidates.add(sitemap_url)
    except Exception:
        pass

    discovered_pages: set[str] = set()
    processed_sitemaps: set[str] = set()
    sitemap_queue = deque(sitemap_candidates)

    while sitemap_queue and len(processed_sitemaps) < 50:
        sitemap_url = sitemap_queue.popleft()
        sitemap_url = clean_url(sitemap_url) or sitemap_url

        if sitemap_url in processed_sitemaps:
            continue

        processed_sitemaps.add(sitemap_url)

        try:
            time.sleep(REQUEST_DELAY_SECONDS)

            response = session.get(sitemap_url, timeout=45)

            if response.status_code != 200:
                continue

            urls = parse_sitemap_xml(response.text)

            for found_url in urls:
                normalized = clean_url(found_url)

                # sitemap index에 포함된 sitemap일 가능성
                if found_url.lower().endswith(".xml"):
                    if found_url not in processed_sitemaps:
                        sitemap_queue.append(found_url)
                    continue

                if normalized and is_allowed_by_robots(robots, normalized):
                    discovered_pages.add(normalized)

        except Exception as exc:
            print(f"[SITEMAP] Failed: {sitemap_url} | {exc}")

    print(f"[SITEMAP] Discovered {len(discovered_pages)} candidate pages.")
    return discovered_pages

# =============================================================================
# 5. HTML 본문 추출
# =============================================================================

def get_meta_content(soup: BeautifulSoup, *names: str) -> Optional[str]:
    """meta name/property 값 검색"""
    for name in names:
        tag = soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return normalize_whitespace(tag["content"])

        tag = soup.find("meta", attrs={"property": name})
        if tag and tag.get("content"):
            return normalize_whitespace(tag["content"])

    return None

def remove_unwanted_elements(soup: BeautifulSoup) -> None:
    """스크립트, 스타일, 쿠키 배너 등 대시보드에 불필요한 요소 제거"""

    for tag_name in REMOVE_TAGS:
        for element in soup.find_all(tag_name):
            element.decompose()

    for element in soup.find_all(True):
        class_values = " ".join(element.get("class", [])).lower()
        element_id = (element.get("id") or "").lower()
        identifier = f"{class_values} {element_id}"

        if any(keyword in identifier for keyword in REMOVE_CLASS_KEYWORDS):
            element.decompose()

def select_main_content(soup: BeautifulSoup):
    """
    본문 우선순위:
    article > main > role=main > 일반 body
    """

    selectors = [
        "article",
        "main",
        "[role='main']",
        ".entry-content",
        ".post-content",
        ".article-content",
        ".elementor-location-single",
        "#content",
        ".content",
    ]

    for selector in selectors:
        element = soup.select_one(selector)
        if element:
            return element

    return soup.body or soup

def extract_headings(container) -> list[dict[str, str]]:
    results = []

    for tag in container.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        text = normalize_whitespace(tag.get_text(" ", strip=True))

        if text:
            results.append({
                "level": tag.name.upper(),
                "text": text,
            })

    return results

def extract_tables(container) -> list[dict[str, Any]]:
    """
    HTML 표를 최대 20개, 표당 100행까지 추출.
    지나치게 큰 표는 DB/HTML 비대화를 방지합니다.
    """

    tables: list[dict[str, Any]] = []

    for table_index, table in enumerate(container.find_all("table")):
        if table_index >= 20:
            break

        rows = []

        for tr in table.find_all("tr")[:100]:
            cells = [
                normalize_whitespace(cell.get_text(" ", strip=True))
                for cell in tr.find_all(["th", "td"])
            ]

            if cells:
                rows.append(cells)

        if rows:
            caption = table.find("caption")
            tables.append({
                "caption": normalize_whitespace(
                    caption.get_text(" ", strip=True)
                ) if caption else "",
                "rows": rows,
            })

    return tables

def extract_images(container, page_url: str) -> list[dict[str, str]]:
    images = []

    for image in container.find_all("img")[:100]:
        src = image.get("src") or image.get("data-src") or ""
        alt = normalize_whitespace(image.get("alt") or "")
        title = normalize_whitespace(image.get("title") or "")

        if src:
            src = urljoin(page_url, src)

        if src or alt or title:
            images.append({
                "src": src,
                "alt": alt,
                "title": title,
            })

    return images

def extract_internal_links(container, page_url: str) -> list[str]:
    """본문 내 내부 링크 추출"""
    links: set[str] = set()

    for anchor in container.find_all("a", href=True):
        href = urljoin(page_url, anchor["href"])
        normalized = clean_url(href)

        if normalized:
            links.add(normalized)

    return sorted(links)

def extract_page_data(page_url: str, response: requests.Response) -> dict[str, Any]:
    """
    공개 HTML에서 화면 텍스트 및 주요 메타데이터 추출.
    """

    soup = BeautifulSoup(response.text, "html.parser")

    page_language = (
        soup.html.get("lang", "").strip()
        if soup.html else ""
    )

    canonical_tag = soup.find("link", rel=lambda x: x and "canonical" in x)
    canonical_url = (
        urljoin(page_url, canonical_tag.get("href"))
        if canonical_tag and canonical_tag.get("href")
        else page_url
    )

    title = normalize_whitespace(
        get_meta_content(soup, "og:title", "twitter:title")
        or (soup.title.get_text(" ", strip=True) if soup.title else "")
    )

    description = normalize_whitespace(
        get_meta_content(
            soup,
            "description",
            "og:description",
            "twitter:description",
        ) or ""
    )

    author = get_meta_content(soup, "author", "article:author")
    published_at = get_meta_content(
        soup,
        "article:published_time",
        "date",
        "publish_date",
        "datePublished",
    )
    modified_at = get_meta_content(
        soup,
        "article:modified_time",
        "modified_time",
        "dateModified",
    )

    # JSON-LD에서 발행일/작성자 보완 추출
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            raw = json.loads(script.get_text(strip=True))

            candidates = raw if isinstance(raw, list) else [raw]

            for item in candidates:
                if not isinstance(item, dict):
                    continue

                if not published_at:
                    published_at = item.get("datePublished") or published_at

                if not modified_at:
                    modified_at = item.get("dateModified") or modified_at

                if not author:
                    json_author = item.get("author")

                    if isinstance(json_author, dict):
                        author = json_author.get("name")

                    elif isinstance(json_author, str):
                        author = json_author

        except Exception:
            continue

    remove_unwanted_elements(soup)
    main_content = select_main_content(soup)

    headings = extract_headings(main_content)
    tables = extract_tables(main_content)
    images = extract_images(main_content, page_url)
    internal_links = extract_internal_links(main_content, page_url)

    visible_text = normalize_whitespace(
        main_content.get_text(" ", strip=True)
    )

    # 본문이 비정상적으로 짧으면 body 텍스트로 보완
    if len(visible_text) < 100 and soup.body:
        visible_text = normalize_whitespace(
            soup.body.get_text(" ", strip=True)
        )

    # URL 형태를 기준으로 간단히 분류
    path = urlparse(page_url).path.lower()

    if "/category/" in path:
        page_type = "Category"

    elif "/tag/" in path:
        page_type = "Tag"

    elif "/author/" in path:
        page_type = "Author"

    elif any(token in path for token in ["/report", "/news", "/analysis", "/insight"]):
        page_type = "Article"

    else:
        page_type = "Page"

    content_for_hash = json.dumps({
        "title": title,
        "description": description,
        "visible_text": visible_text,
        "headings": headings,
        "tables": tables,
    }, ensure_ascii=False, sort_keys=True)

    return {
        "url": page_url,
        "canonical_url": canonical_url,
        "title": title,
        "description": description,
        "author": author or "",
        "published_at": published_at or "",
        "modified_at": modified_at or "",
        "page_type": page_type,
        "language": page_language,
        "visible_text": visible_text,
        "headings": headings,
        "tables": tables,
        "images": images,
        "links": internal_links,
        "content_hash": sha256_text(content_for_hash),
        "http_status": response.status_code,
    }

# =============================================================================
# 6. DB 저장 및 변경 이력 관리
# =============================================================================

def save_page(
    conn: sqlite3.Connection,
    run_id: int,
    data: dict[str, Any],
) -> tuple[bool, int]:
    """
    페이지 저장.

    Returns:
        (changed 여부, page_id)
    """

    now = utc_now()
    existing = conn.execute("""
        SELECT page_id, content_hash
        FROM pages
        WHERE url = ?
    """, (data["url"],)).fetchone()

    headings_json = json.dumps(data["headings"], ensure_ascii=False)
    tables_json = json.dumps(data["tables"], ensure_ascii=False)
    images_json = json.dumps(data["images"], ensure_ascii=False)
    links_json = json.dumps(data["links"], ensure_ascii=False)

    changed = False

    if existing is None:
        cursor = conn.execute("""
            INSERT INTO pages (
                url, canonical_url, title, description, author,
                published_at, modified_at, page_type, language,
                visible_text, headings_json, tables_json,
                images_json, links_json, content_hash, http_status,
                first_collected_at, last_collected_at, last_success_at,
                last_error, is_active
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data["url"],
            data["canonical_url"],
            data["title"],
            data["description"],
            data["author"],
            data["published_at"],
            data["modified_at"],
            data["page_type"],
            data["language"],
            data["visible_text"],
            headings_json,
            tables_json,
            images_json,
            links_json,
            data["content_hash"],
            data["http_status"],
            now,
            now,
            now,
            None,
            1,
        ))

        page_id = int(cursor.lastrowid)
        changed = True

    else:
        page_id = int(existing["page_id"])
        changed = existing["content_hash"] != data["content_hash"]

        conn.execute("""
            UPDATE pages
            SET canonical_url = ?,
                title = ?,
                description = ?,
                author = ?,
                published_at = ?,
                modified_at = ?,
                page_type = ?,
                language = ?,
                visible_text = ?,
                headings_json = ?,
                tables_json = ?,
                images_json = ?,
                links_json = ?,
                content_hash = ?,
                http_status = ?,
                last_collected_at = ?,
                last_success_at = ?,
                last_error = NULL,
                is_active = 1
            WHERE page_id = ?
        """, (
            data["canonical_url"],
            data["title"],
            data["description"],
            data["author"],
            data["published_at"],
            data["modified_at"],
            data["page_type"],
            data["language"],
            data["visible_text"],
            headings_json,
            tables_json,
            images_json,
            links_json,
            data["content_hash"],
            data["http_status"],
            now,
            now,
            page_id,
        ))

    # 신규 또는 변경된 경우에만 Snapshot 생성
    if changed:
        conn.execute("""
            INSERT OR IGNORE INTO page_snapshots (
                page_id, run_id, collected_at, content_hash,
                title, visible_text, headings_json, tables_json,
                images_json, http_status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            page_id,
            run_id,
            now,
            data["content_hash"],
            data["title"],
            data["visible_text"],
            headings_json,
            tables_json,
            images_json,
            data["http_status"],
        ))

    conn.commit()
    return changed, page_id

def mark_page_error(
    conn: sqlite3.Connection,
    url: str,
    error_message: str,
) -> None:
    """기존 페이지의 마지막 오류 기록"""

    conn.execute("""
        UPDATE pages
        SET last_collected_at = ?,
            last_error = ?
        WHERE url = ?
    """, (
        utc_now(),
        error_message[:2000],
        url,
    ))

    conn.commit()

# =============================================================================
# 7. 크롤러
# =============================================================================

def get_existing_urls(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT url FROM pages").fetchall()
    return {row["url"] for row in rows}

def crawl_public_pages(
    conn: sqlite3.Connection,
    run_id: int,
) -> dict[str, int]:
    """
    sitemap + 페이지 내부 링크를 통해 공개 페이지를 수집합니다.
    """

    session = make_session()
    robots = load_robots_parser(session)

    sitemap_urls = discover_sitemap_urls(session, robots)
    existing_urls = get_existing_urls(conn)

    queue: deque[str] = deque()
    queued: set[str] = set()
    visited: set[str] = set()

    def enqueue(url: str) -> None:
        normalized = clean_url(url)

        if not normalized:
            return

        if normalized in queued or normalized in visited:
            return

        if not is_allowed_by_robots(robots, normalized):
            return

        # 기존 URL 재확인이 꺼진 경우 신규 URL만 수집
        if not REFRESH_EXISTING_URLS and normalized in existing_urls:
            return

        queued.add(normalized)
        queue.append(normalized)

    # 홈페이지는 sitemap이 없어도 반드시 시도
    enqueue(BASE_URL)

    for url in sorted(sitemap_urls):
        enqueue(url)

    stats = {
        "attempted": 0,
        "collected": 0,
        "changed": 0,
    }

    while queue and stats["attempted"] < MAX_PAGES:
        url = queue.popleft()

        if url in visited:
            continue

        visited.add(url)
        stats["attempted"] += 1

        try:
            print(f"[{stats['attempted']:03d}/{MAX_PAGES}] Collecting: {url}")

            time.sleep(REQUEST_DELAY_SECONDS)

            response = session.get(
                url,
                timeout=45,
                allow_redirects=True,
                stream=True,
            )

            content_type = response.headers.get("Content-Type", "").lower()

            if response.status_code != 200:
                message = f"HTTP {response.status_code}"
                print(f"  └─ Skip: {message}")

                mark_page_error(conn, url, message)
                log_error(conn, run_id, url, "HTTP_ERROR", message)
                continue

            if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
                message = f"Non-HTML content: {content_type}"
                print(f"  └─ Skip: {message}")
                continue

            content_length = response.headers.get("Content-Length")

            if content_length and int(content_length) > MAX_RESPONSE_BYTES:
                message = f"Response too large: {content_length} bytes"
                print(f"  └─ Skip: {message}")

                log_error(conn, run_id, url, "SIZE_LIMIT", message)
                continue

            response.content  # stream=True 응답을 실제 로드

            if len(response.content) > MAX_RESPONSE_BYTES:
                message = f"Response exceeds limit: {len(response.content)} bytes"
                print(f"  └─ Skip: {message}")

                log_error(conn, run_id, url, "SIZE_LIMIT", message)
                continue

            # 리디렉션 최종 URL도 동일 도메인인지 검증
            final_url = clean_url(response.url)

            if not final_url:
                message = f"Redirected outside allowed domain: {response.url}"
                print(f"  └─ Skip: {message}")

                log_error(conn, run_id, url, "REDIRECT_OUTSIDE_DOMAIN", message)
                continue

            data = extract_page_data(final_url, response)
            changed, _ = save_page(conn, run_id, data)

            stats["collected"] += 1
            stats["changed"] += int(changed)

            content_state = "NEW/UPDATED" if changed else "UNCHANGED"
            print(
                f"  └─ {content_state} | "
                f"{len(data['visible_text']):,} chars | "
                f"{len(data['links'])} internal links"
            )

            # 페이지 내부 링크를 추가 발견
            for link in data["links"]:
                enqueue(link)

        except requests.RequestException as exc:
            error_message = f"Request error: {exc}"
            print(f"  └─ ERROR: {error_message}")

            mark_page_error(conn, url, error_message)
            log_error(conn, run_id, url, "REQUEST_ERROR", error_message)

        except Exception as exc:
            error_message = f"Unexpected error: {exc}"
            print(f"  └─ ERROR: {error_message}")

            mark_page_error(conn, url, error_message)
            log_error(conn, run_id, url, "PARSER_ERROR", error_message)

    return stats

# =============================================================================
# 8. 대시보드 데이터 조회
# =============================================================================

def safe_json_loads(value: Optional[str], default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except Exception:
        return default

def get_dashboard_data(conn: sqlite3.Connection) -> dict[str, Any]:
    """
    index.html 출력용 데이터 준비.
    전체 본문은 SQLite에 저장하되, HTML에는 요약 중심으로 노출합니다.
    """

    summary = conn.execute("""
        SELECT
            COUNT(*) AS total_pages,
            COUNT(DISTINCT page_type) AS page_types,
            MAX(last_success_at) AS latest_update
        FROM pages
        WHERE is_active = 1
    """).fetchone()

    latest_run = conn.execute("""
        SELECT *
        FROM crawl_runs
        ORDER BY run_id DESC
        LIMIT 1
    """).fetchone()

    pages = conn.execute("""
        SELECT
            page_id,
            url,
            title,
            description,
            author,
            published_at,
            modified_at,
            page_type,
            visible_text,
            headings_json,
            tables_json,
            images_json,
            last_success_at,
            content_hash
        FROM pages
        WHERE is_active = 1
        ORDER BY
            CASE WHEN published_at IS NULL OR published_at = '' THEN 1 ELSE 0 END,
            published_at DESC,
            last_success_at DESC
        LIMIT 300
    """).fetchall()

    page_records = []

    for row in pages:
        visible_text = row["visible_text"] or ""
        description = row["description"] or ""

        excerpt_source = description if len(description) >= 50 else visible_text
        excerpt = normalize_whitespace(excerpt_source)[:450]

        headings = safe_json_loads(row["headings_json"], [])
        tables = safe_json_loads(row["tables_json"], [])
        images = safe_json_loads(row["images_json"], [])

        heading_texts = [
            item.get("text", "")
            for item in headings[:8]
            if item.get("text")
        ]

        table_previews = []

        for table in tables[:3]:
            rows = table.get("rows", [])
            if rows:
                table_previews.append({
                    "caption": table.get("caption", ""),
                    "rows": rows[:8],
                })

        page_records.append({
            "page_id": row["page_id"],
            "url": row["url"],
            "title": row["title"] or "Untitled Page",
            "description": description,
            "author": row["author"] or "",
            "published_at": row["published_at"] or "",
            "modified_at": row["modified_at"] or "",
            "page_type": row["page_type"] or "Page",
            "excerpt": excerpt,
            "headings": heading_texts,
            "tables": table_previews,
            "image_count": len(images),
            "last_success_at": row["last_success_at"] or "",
        })

    changed_pages = conn.execute("""
        SELECT COUNT(*) AS changed_count
        FROM page_snapshots
        WHERE collected_at >= datetime('now', '-7 days')
    """).fetchone()

    return {
        "generated_at": utc_now(),
        "summary": {
            "total_pages": summary["total_pages"] if summary else 0,
            "page_types": summary["page_types"] if summary else 0,
            "latest_update": summary["latest_update"] if summary else "",
            "changed_last_7_days": changed_pages["changed_count"] if changed_pages else 0,
            "last_run_status": latest_run["status"] if latest_run else "N/A",
            "last_run_collected": latest_run["pages_collected"] if latest_run else 0,
        },
        "pages": page_records,
    }

# =============================================================================
# 9. Bloomberg Terminal 스타일 HTML 생성
# =============================================================================

def generate_dashboard_html(data: dict[str, Any]) -> None:
    """
    정적 HTML 생성.
    본문 전체는 DB에 저장하고, 화면에서는 검색 가능한 요약/표/헤딩을 제공합니다.
    """

    data_json = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")

    html_output = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow">
<title>Linerlytica | Market Intelligence Terminal</title>

<style>
:root {{
    --bg: #070b10;
    --panel: #0e151d;
    --panel-2: #111b25;
    --line: #243545;
    --text: #d8e2eb;
    --muted: #7f96a8;
    --cyan: #26d9e8;
    --amber: #ffb000;
    --green: #49d17d;
    --red: #ff5c6c;
    --white: #f6fbff;
}}

* {{
    box-sizing: border-box;
}}

body {{
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: "Consolas", "SFMono-Regular", "Menlo", monospace;
    font-size: 13px;
}}

a {{
    color: var(--cyan);
    text-decoration: none;
}}

a:hover {{
    text-decoration: underline;
}}

.header {{
    border-bottom: 1px solid var(--line);
    background: #0a1118;
    padding: 14px 22px;
    position: sticky;
    top: 0;
    z-index: 20;
}}

.header-top {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 20px;
}}

.brand {{
    color: var(--amber);
    font-size: 18px;
    font-weight: 800;
    letter-spacing: .6px;
}}

.brand span {{
    color: var(--cyan);
}}

.timestamp {{
    color: var(--muted);
    font-size: 11px;
    text-align: right;
}}

.search-row {{
    margin-top: 12px;
    display: flex;
    gap: 10px;
}}

.search-box {{
    width: 100%;
    color: var(--white);
    background: #05090d;
    border: 1px solid #294051;
    padding: 11px 12px;
    outline: none;
    font-family: inherit;
}}

.search-box:focus {{
    border-color: var(--cyan);
    box-shadow: 0 0 0 2px rgba(38, 217, 232, .1);
}}

.filter-button {{
    padding: 10px 12px;
    white-space: nowrap;
    color: var(--text);
    background: var(--panel);
    border: 1px solid var(--line);
    cursor: pointer;
    font-family: inherit;
}}

.filter-button.active {{
    border-color: var(--amber);
    color: var(--amber);
}}

.container {{
    width: min(1600px, calc(100% - 32px));
    margin: 18px auto 50px;
}}

.section-label {{
    margin: 21px 0 9px;
    color: var(--amber);
    font-size: 11px;
    font-weight: 800;
    letter-spacing: 1.2px;
}}

.kpi-grid {{
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 10px;
}}

.kpi {{
    min-height: 88px;
    padding: 14px;
    border: 1px solid var(--line);
    border-top: 2px solid var(--cyan);
    background: var(--panel);
}}

.kpi-label {{
    color: var(--muted);
    font-size: 10px;
    letter-spacing: .8px;
}}

.kpi-value {{
    margin-top: 11px;
    color: var(--white);
    font-size: 22px;
    font-weight: 800;
}}

.kpi-value.good {{
    color: var(--green);
}}

.kpi-value.amber {{
    color: var(--amber);
}}

.kpi-sub {{
    margin-top: 6px;
    color: var(--muted);
    font-size: 10px;
}}

.status-strip {{
    border: 1px solid var(--line);
    padding: 9px 12px;
    color: var(--muted);
    background: var(--panel);
    font-size: 11px;
}}

.status-ok {{
    color: var(--green);
}}

.status-warning {{
    color: var(--amber);
}}

.result-meta {{
    display: flex;
    justify-content: space-between;
    gap: 10px;
    color: var(--muted);
    font-size: 11px;
    margin-bottom: 9px;
}}

.result-grid {{
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 10px;
}}

.card {{
    display: flex;
    flex-direction: column;
    min-height: 250px;
    padding: 15px;
    border: 1px solid var(--line);
    background: var(--panel);
}}

.card:hover {{
    background: var(--panel-2);
    border-color: #3a586e;
}}

.card-top {{
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
}}

.card-title {{
    color: var(--white);
    font-size: 15px;
    line-height: 1.45;
    font-weight: 800;
}}

.tag {{
    flex: 0 0 auto;
    border: 1px solid #426176;
    color: var(--cyan);
    padding: 3px 6px;
    font-size: 9px;
    letter-spacing: .5px;
}}

.card-date {{
    margin-top: 9px;
    color: var(--muted);
    font-size: 10px;
}}

.card-excerpt {{
    margin: 14px 0 10px;
    color: #b7c6d2;
    line-height: 1.65;
}}

.heading-list {{
    margin: 5px 0 0;
    padding: 0;
    list-style: none;
}}

.heading-list li {{
    overflow: hidden;
    padding: 3px 0;
    color: #91b9c4;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: 11px;
}}

.heading-list li::before {{
    content: "› ";
    color: var(--amber);
}}

.table-preview {{
    overflow: auto;
    margin-top: 10px;
    border: 1px solid #213441;
    max-height: 175px;
}}

.table-preview table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 10px;
}}

.table-preview td {{
    padding: 5px 6px;
    border-bottom: 1px solid #1b2b37;
    color: #b9c8d2;
}}

.table-preview tr:first-child td {{
    position: sticky;
    top: 0;
    color: var(--amber);
    background: #0a1118;
    font-weight: 700;
}}

.card-footer {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
    margin-top: auto;
    padding-top: 14px;
    color: var(--muted);
    font-size: 10px;
}}

.empty {{
    grid-column: 1 / -1;
    padding: 40px;
    border: 1px solid var(--line);
    color: var(--muted);
    text-align: center;
    background: var(--panel);
}}

.footer {{
    margin-top: 24px;
    border-top: 1px solid var(--line);
    padding: 16px 0;
    color: var(--muted);
    font-size: 10px;
    line-height: 1.7;
}}

.hidden {{
    display: none !important;
}}

@media (max-width: 1050px) {{
    .kpi-grid {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
    }}

    .result-grid {{
        grid-template-columns: 1fr;
    }}
}}

@media (max-width: 600px) {{
    .container {{
        width: min(100% - 20px, 1600px);
    }}

    .header {{
        padding: 12px 10px;
    }}

    .header-top {{
        align-items: flex-start;
        flex-direction: column;
    }}

    .timestamp {{
        text-align: left;
    }}

    .kpi-grid {{
        grid-template-columns: 1fr;
    }}

    .search-row {{
        flex-wrap: wrap;
    }}
}}
</style>
</head>

<body>
<header class="header">
    <div class="header-top">
        <div class="brand">
            LINERLYTICA <span>/ MARKET INTELLIGENCE</span>
        </div>
        <div class="timestamp" id="generatedAt"></div>
    </div>

    <div class="search-row">
        <input
            id="searchInput"
            class="search-box"
            type="search"
            placeholder="SEARCH TITLE / SUMMARY / HEADING / URL ..."
            autocomplete="off"
        >
        <button class="filter-button active" data-filter="ALL">ALL</button>
        <button class="filter-button" data-filter="Article">ARTICLE</button>
        <button class="filter-button" data-filter="Page">PAGE</button>
        <button class="filter-button" data-filter="Category">CATEGORY</button>
    </div>
</header>

<main class="container">
    <div class="section-label">SYSTEM OVERVIEW</div>

    <section class="kpi-grid">
        <div class="kpi">
            <div class="kpi-label">PUBLIC PAGES INDEXED</div>
            <div class="kpi-value" id="totalPages">-</div>
            <div class="kpi-sub">SQLite current-page records</div>
        </div>

        <div class="kpi">
            <div class="kpi-label">CONTENT TYPES</div>
            <div class="kpi-value amber" id="pageTypes">-</div>
            <div class="kpi-sub">Article / Page / Category etc.</div>
        </div>

        <div class="kpi">
            <div class="kpi-label">CHANGED SNAPSHOTS · 7D</div>
            <div class="kpi-value good" id="changedPages">-</div>
            <div class="kpi-sub">New or updated public content</div>
        </div>

        <div class="kpi">
            <div class="kpi-label">LAST COLLECTION</div>
            <div class="kpi-value" style="font-size:14px" id="lastUpdate">-</div>
            <div class="kpi-sub" id="runStatus">-</div>
        </div>
    </section>

    <div class="section-label">COLLECTION STATUS</div>
    <div class="status-strip" id="statusStrip">Loading dashboard data...</div>

    <div class="section-label">PUBLIC CONTENT INDEX</div>
    <div class="result-meta">
        <span id="resultCount">0 records</span>
        <span>Source: linerlytica.com · Click title to open original page</span>
    </div>

    <section class="result-grid" id="resultGrid"></section>

    <footer class="footer">
        <strong>DATA NOTICE:</strong>
        This dashboard indexes publicly accessible HTML metadata and text for internal market-intelligence workflow.
        Validate Linerlytica terms of use, robots.txt directives, subscription conditions, copyright and redistribution rights
        before publishing, sharing or commercially using collected content.
        Restricted, authenticated, paid or CAPTCHA-protected content is not collected by this script.
    </footer>
</main>

<script>
const DASHBOARD_DATA = {data_json};

const state = {{
    filter: "ALL",
    query: ""
}};

function escapeHtml(value) {{
    return String(value || "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}}

function formatDate(value) {{
    if (!value) return "N/A";

    try {{
        const date = new Date(value);

        if (Number.isNaN(date.getTime())) {{
            return value;
        }}

        return date.toLocaleString("ko-KR", {{
            year: "numeric",
            month: "2-digit",
            day: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
            hour12: false
        }});
    }} catch (_) {{
        return value;
    }}
}}

function tableToHtml(table) {{
    if (!table || !table.rows || !table.rows.length) return "";

    const caption = table.caption
        ? `<div style="padding:6px;color:#7f96a8;font-size:10px">${{escapeHtml(table.caption)}}</div>`
        : "";

    const rows = table.rows.map(row => {{
        const cells = row.map(cell => `<td>${{escapeHtml(cell)}}</td>`).join("");
        return `<tr>${{cells}}</tr>`;
    }}).join("");

    return `
        <div class="table-preview">
            ${{caption}}
            <table><tbody>${{rows}}</tbody></table>
        </div>
    `;
}}

function buildCard(page) {{
    const headings = (page.headings || [])
        .slice(0, 5)
        .map(item => `<li>${{escapeHtml(item)}}</li>`)
        .join("");

    const headingBlock = headings
        ? `<ul class="heading-list">${{headings}}</ul>`
        : "";

    const tableBlock = (page.tables || [])
        .slice(0, 1)
        .map(tableToHtml)
        .join("");

    const dateValue = page.published_at || page.modified_at || page.last_success_at;

    return `
        <article class="card"
            data-type="${{escapeHtml(page.page_type)}}"
            data-search="${{escapeHtml([
                page.title,
                page.description,
                page.excerpt,
                page.url,
                ...(page.headings || [])
            ].join(" ").toLowerCase())}}">
            <div class="card-top">
                <a class="card-title"
                    href="${{escapeHtml(page.url)}}"
                    target="_blank"
                    rel="noopener noreferrer">
                    ${{escapeHtml(page.title)}}
                </a>
                <span class="tag">${{escapeHtml(page.page_type)}}</span>
            </div>

            <div class="card-date">
                DATE: ${{escapeHtml(formatDate(dateValue))}}
                ${{page.author ? ` | AUTHOR: ${{escapeHtml(page.author)}}` : ""}}
            </div>

            <div class="card-excerpt">${{escapeHtml(page.excerpt || "No extractable visible text.")}}</div>

            ${{headingBlock}}
            ${{tableBlock}}

            <div class="card-footer">
                <span>IMAGES DETECTED: ${{page.image_count || 0}}</span>
                <a href="${{escapeHtml(page.url)}}" target="_blank" rel="noopener noreferrer">
                    OPEN SOURCE ↗
                </a>
            </div>
        </article>
    `;
}}

function render() {{
    const grid = document.getElementById("resultGrid");
    const query = state.query.trim().toLowerCase();

    const filtered = DASHBOARD_DATA.pages.filter(page => {{
        const typeMatch = state.filter === "ALL" || page.page_type === state.filter;

        const searchable = [
            page.title,
            page.description,
            page.excerpt,
            page.url,
            ...(page.headings || [])
        ].join(" ").toLowerCase();

        const textMatch = !query || searchable.includes(query);

        return typeMatch && textMatch;
    }});

    document.getElementById("resultCount").textContent =
        `${{filtered.length.toLocaleString()}} / ${{DASHBOARD_DATA.pages.length.toLocaleString()}} records`;

    if (!filtered.length) {{
        grid.innerHTML = `
            <div class="empty">
                NO MATCHING RECORDS.<br>
                Try a different keyword or content filter.
            </div>
        `;
        return;
    }}

    grid.innerHTML = filtered.map(buildCard).join("");
}}

function initialize() {{
    const summary = DASHBOARD_DATA.summary || {{}};

    document.getElementById("generatedAt").textContent =
        `DASHBOARD GENERATED: ${{formatDate(DASHBOARD_DATA.generated_at)}}`;

    document.getElementById("totalPages").textContent =
        Number(summary.total_pages || 0).toLocaleString();

    document.getElementById("pageTypes").textContent =
        Number(summary.page_types || 0).toLocaleString();

    document.getElementById("changedPages").textContent =
        Number(summary.changed_last_7_days || 0).toLocaleString();

    document.getElementById("lastUpdate").textContent =
        formatDate(summary.latest_update);

    document.getElementById("runStatus").textContent =
        `LAST RUN: ${{summary.last_run_status || "N/A"}} | COLLECTED: ${{summary.last_run_collected || 0}}`;

    const statusClass = summary.last_run_status === "SUCCESS"
        ? "status-ok"
        : "status-warning";

    document.getElementById("statusStrip").innerHTML = `
        <span class="${{statusClass}}">
            STATUS: ${{escapeHtml(summary.last_run_status || "N/A")}}
        </span>
        &nbsp;|&nbsp;
        Indexed public pages are stored in <strong>Linerlytica.db</strong>.
        The dashboard displays excerpts and extracted structural elements; source links remain authoritative.
    `;

    document.getElementById("searchInput").addEventListener("input", event => {{
        state.query = event.target.value;
        render();
    }});

    document.querySelectorAll(".filter-button").forEach(button => {{
        button.addEventListener("click", () => {{
            state.filter = button.dataset.filter;

            document.querySelectorAll(".filter-button").forEach(item => {{
                item.classList.remove("active");
            }});

            button.classList.add("active");
            render();
        }});
    }});

    render();
}}

initialize();
</script>
</body>
</html>
"""

    HTML_PATH.write_text(html_output, encoding="utf-8")
    print(f"[DASHBOARD] Generated: {HTML_PATH}")

# =============================================================================
# 10. 메인 실행
# =============================================================================

def main() -> None:
    print("=" * 78)
    print("LINERLYTICA PUBLIC WEB INTELLIGENCE COLLECTOR")
    print("=" * 78)
    print(f"Base URL       : {BASE_URL}")
    print(f"Database       : {DB_PATH}")
    print(f"Dashboard      : {HTML_PATH}")
    print(f"Max Pages      : {MAX_PAGES}")
    print(f"Request Delay  : {REQUEST_DELAY_SECONDS} sec")
    print("=" * 78)

    conn = get_connection()
    init_database(conn)

    run_id = create_crawl_run(conn)

    try:
        stats = crawl_public_pages(conn, run_id)

        finish_crawl_run(
            conn=conn,
            run_id=run_id,
            status="SUCCESS",
            pages_attempted=stats["attempted"],
            pages_collected=stats["collected"],
            pages_changed=stats["changed"],
        )

        dashboard_data = get_dashboard_data(conn)
        generate_dashboard_html(dashboard_data)

        print("=" * 78)
        print("COMPLETED")
        print(f"Attempted : {stats['attempted']}")
        print(f"Collected : {stats['collected']}")
        print(f"Changed   : {stats['changed']}")
        print("=" * 78)

    except Exception as exc:
        error_message = str(exc)

        finish_crawl_run(
            conn=conn,
            run_id=run_id,
            status="FAILED",
            pages_attempted=0,
            pages_collected=0,
            pages_changed=0,
            error_message=error_message,
        )

        print(f"[FATAL] {error_message}", file=sys.stderr)
        raise

    finally:
        conn.close()

if __name__ == "__main__":
    main()
